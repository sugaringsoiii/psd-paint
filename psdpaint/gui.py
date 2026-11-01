"""psd-paint 데스크톱 앱: PSD 여러 개를 넣고 순서대로 밑색을 칠한다."""
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QEvent, QEventLoop, QObject, QRect, QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import threading
from dataclasses import replace

from . import ai, style
from .cli import output_path
from .core import (AwaitingAnswer, Options, PaintError, next_options, paint, quick_preview, region_at,
                   repaint, set_region)

APP_NAME = "psd-paint"
COL_FILE, COL_STATUS, COL_INFO = range(3)
REF_ROLE = Qt.UserRole + 1  # 파일별 레퍼런스 경로 (없으면 설정 탭의 공통 참고 방식)
LABEL_W = 96  # 설정 탭 라벨 고정폭


def find_apps():
    """설치된 PSD 편집 앱. → {"clip": 경로 또는 None, "photoshop": 경로 또는 None}"""
    found = {"clip": None, "photoshop": None}
    if sys.platform == "darwin":
        def spotlight(name):
            q = f"kMDItemContentType == 'com.apple.application-bundle' && kMDItemDisplayName == '*{name}*'c"
            try:
                out = subprocess.run(["mdfind", q], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.TimeoutExpired):
                return []
            return [p for p in out.splitlines() if p.endswith(".app")]
        clip = [p for p in spotlight("CLIP STUDIO PAINT")]
        ps = sorted((p for p in spotlight("Photoshop") if "Beta" not in p), reverse=True)  # 최신판 우선
        found["clip"] = clip[0] if clip else None
        found["photoshop"] = ps[0] if ps else None
    elif sys.platform == "win32":
        import glob
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
        for root in filter(None, roots):
            clip = glob.glob(os.path.join(root, "CELSYS", "CLIP STUDIO*", "CLIP STUDIO PAINT", "CLIPStudioPaint.exe"))
            ps = sorted(glob.glob(os.path.join(root, "Adobe", "Adobe Photoshop*", "Photoshop.exe")), reverse=True)
            found["clip"] = found["clip"] or (clip[0] if clip else None)
            found["photoshop"] = found["photoshop"] or (ps[0] if ps else None)
    return found


def open_path(path, reveal=False, app=None):
    """파일을 연다. app(앱 경로)이 있으면 그 앱으로, 없으면 연결된 기본 앱으로.
    reveal이면 파일 탐색기(Finder)에서 위치를 보여 준다."""
    path = str(path)
    if sys.platform == "darwin":
        if reveal:
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["open", "-a", app, path] if app else ["open", path])
    elif sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", "/select,", path])
        elif app:
            subprocess.Popen([app, path])
        else:
            os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent) if reveal else path])


def card(*widgets, margins=(14, 12, 14, 12), spacing=10):
    w = QWidget()
    w.setObjectName("card")
    w.setAttribute(Qt.WA_StyledBackground, True)
    lay = QVBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for x in widgets:
        lay.addWidget(x) if isinstance(x, QWidget) else lay.addLayout(x)
    return w, lay


def label(text, name=None):
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    return lb


class Worker(QObject):
    """파일 목록을 순서대로 처리한다. 중지 요청은 지금 파일을 마친 뒤 반영한다."""
    started_file = Signal(int)
    progressed = Signal(int, str, float)
    finished_file = Signal(int, object, float)
    failed_file = Signal(int, str)
    waiting_file = Signal(int, str)
    all_done = Signal()

    def __init__(self, jobs, opts):
        super().__init__()
        self.jobs = jobs  # [(행 번호, 원본, 결과 경로, 파일별 레퍼런스 또는 None)]
        self.opts = opts
        self.stop = False

    def run(self):
        chained = self.opts
        for row, src, dst, ref in self.jobs:
            if self.stop:
                break
            self.started_file.emit(row)
            t = time.time()
            # 파일별 레퍼런스가 있으면 그것을, 없으면 공통 설정(또는 앞 파일 결과)을 쓴다
            opts = replace(self.opts, hint_source="psd" if ref.lower().endswith(".psd") else "image",
                           hint_path=ref) if ref else chained
            try:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                result = paint(src, dst, opts,
                               progress=lambda msg, frac, row=row: self.progressed.emit(row, msg, frac))
                self.finished_file.emit(row, result, time.time() - t)
                chained = next_options(opts, result)  # 연속 모드: 이 결과를 다음 파일의 레퍼런스로
            except AwaitingAnswer as e:
                self.waiting_file.emit(row, str(e))
            except PaintError as e:
                self.failed_file.emit(row, str(e))
            except FileNotFoundError:
                self.failed_file.emit(row, "파일을 찾을 수 없습니다")
            except Exception as e:  # 한 파일이 실패해도 다음 파일은 계속한다
                traceback.print_exc()
                self.failed_file.emit(row, str(e) or type(e).__name__)
        self.all_done.emit()


class FileList(QTreeWidget):
    """PSD 목록. 비어 있으면 끌어 놓기 안내를 그린다."""
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["파일", "상태", "결과"])
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.dragging = False
        h = self.header()
        h.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        h.setSectionResizeMode(COL_STATUS, QHeaderView.Fixed)
        h.setSectionResizeMode(COL_INFO, QHeaderView.Fixed)
        h.resizeSection(COL_STATUS, 170)
        h.resizeSection(COL_INFO, 150)
        h.setStretchLastSection(False)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.acceptDrops():
            self.dragging = True
            self.viewport().update()
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dragLeaveEvent(self, e):
        self.dragging = False
        self.viewport().update()

    def dropEvent(self, e):
        self.dragging = False
        self.viewport().update()
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            paths += sorted(p.glob("*.psd")) if p.is_dir() else [p]
        self.files_dropped.emit([p for p in paths if p.suffix.lower() == ".psd"])

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.topLevelItemCount() and not self.dragging:
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.viewport().rect()).adjusted(8, 8, -8, -8)
        pen = QPen(QColor(style.ACCENT if self.dragging else style.BORDER_HI))
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(QColor(style.ACCENT_SOFT) if self.dragging else Qt.NoBrush)
        p.drawRoundedRect(r, 10, 10)
        if not self.topLevelItemCount():
            p.setPen(QColor(style.TEXT))
            f = p.font()
            f.setPointSize(14)
            p.setFont(f)
            p.drawText(r.adjusted(0, -16, 0, -16), Qt.AlignCenter, "PSD 파일이나 폴더를 끌어 놓으세요")
            p.setPen(QColor(style.MUTED))
            f.setPointSize(11)
            p.setFont(f)
            p.drawText(r.adjusted(0, 22, 0, 22), Qt.AlignCenter,
                       "'참고' 폴더의 힌트대로 칠하고, 힌트가 없는 영역은 회색으로 자동 칠합니다")
        p.end()


class PreviewCanvas(QWidget):
    """미리보기. 원본 이미지를 다시 확대해 만들지 않고, 화면에 보이는 부분만 원하는 배율로 그린다.

    확대·축소할 때 큰 이미지를 매번 새로 만들지 않으므로 핀치·스크롤 확대가 부드럽다.
    그림을 누르면 누른 위치를 그림 크기에 대한 비율(0~1)로 알려 준다.
    """
    clicked = Signal(float, float)

    def __init__(self, text):
        super().__init__()
        self.pix = None
        self.text = text
        self.disp = QSize(0, 0)
        self.fast = False  # 핀치 중에는 빠른 보간으로 그린다

    def set_image(self, pix):
        self.pix = pix
        self.update()

    def clear(self, text):
        self.pix, self.text, self.disp = None, text, QSize(0, 0)
        self.setMinimumSize(0, 0)
        self.update()

    def set_display(self, w, h):
        self.disp = QSize(w, h)
        self.setMinimumSize(w, h)  # 확대하면 스크롤 영역이 그만큼 커진다
        self.update()

    def image_rect(self):
        return QRect(max(0, (self.width() - self.disp.width()) // 2), 0, self.disp.width(), self.disp.height())

    def paintEvent(self, e):
        p = QPainter(self)
        if self.pix is None or self.disp.isEmpty():
            p.setPen(QColor(style.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, self.text)
            return
        r = self.image_rect()
        vis = r.intersected(e.rect())
        if vis.isEmpty():
            return
        sx, sy = self.pix.width() / r.width(), self.pix.height() / r.height()
        src = QRectF((vis.x() - r.x()) * sx, (vis.y() - r.y()) * sy, vis.width() * sx, vis.height() * sy)
        p.setRenderHint(QPainter.SmoothPixmapTransform, not self.fast)
        p.drawPixmap(QRectF(vis), self.pix, src)

    def mousePressEvent(self, e):
        if self.pix is None or e.button() != Qt.LeftButton:
            return
        r = self.image_rect()
        x, y = e.position().x() - r.x(), e.position().y() - r.y()
        if 0 <= x < r.width() and 0 <= y < r.height():
            self.clicked.emit(x / r.width(), y / r.height())


def rounded_menu(parent):
    """둥근 모서리가 제대로 보이는 메뉴 (창 배경을 투명하게 해 사각 모서리를 없앤다)."""
    menu = QMenu(parent)
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
    return menu


def swatch(rgb, size=14):
    pm = QPixmap(size, size)
    pm.fill(QColor(*rgb))
    p = QPainter(pm)
    p.setPen(QColor(style.BORDER_HI))
    p.drawRect(0, 0, size - 1, size - 1)
    p.end()
    return QIcon(pm)


class ColorPicker(QWidget):
    """조각 색 고르기 팝업. 상단 고정(지금 상태·비우기·새 레이어) + 한 열 색 목록(최대 높이 안에서 스크롤)."""
    MAX_H = 420

    def __init__(self, parent, current, entries, can_clear):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        # 창은 투명하게, 안쪽 카드에만 둥근 모서리와 그림자를 준다 (창이 사각이면 모서리가 각져 보인다)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.choice = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 14)
        card_ = QFrame()
        card_.setObjectName("picker")
        card_.setAttribute(Qt.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(card_)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 45))
        card_.setGraphicsEffect(shadow)
        outer.addWidget(card_)
        lay = QVBoxLayout(card_)
        lay.setContentsMargins(4, 6, 4, 4)
        lay.setSpacing(2)
        head = label(f"지금: {current}", "pickerHead")
        lay.addWidget(head)
        for text, data, enabled in (("칠하지 않음 (비우기)", "clear", can_clear), ("새 레이어…", "new", True)):
            btn = QPushButton(text)
            btn.setObjectName("pickerItem")
            btn.setEnabled(enabled)
            btn.clicked.connect(lambda _=False, d=data: self.done(d))
            lay.addWidget(btn)
        line = QFrame()
        line.setObjectName("pickerLine")
        line.setFixedHeight(1)
        lay.addWidget(line)
        self.list = QListWidget()
        self.list.setObjectName("pickerList")
        self.list.setIconSize(QSize(14, 14))
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for text, rgb, data, enabled in entries:
            it = QListWidgetItem(swatch(rgb), text)
            it.setData(Qt.UserRole, data)
            if not enabled:
                it.setFlags(Qt.NoItemFlags)  # 지금 색은 흐리게, 고를 수 없게
            self.list.addItem(it)
        self.list.itemClicked.connect(lambda it: self.done(it.data(Qt.UserRole)))
        lay.addWidget(self.list)
        # 목록 높이: 항목 수만큼, 전체가 MAX_H를 넘지 않게
        row_h = 30
        top_h = lay.sizeHint().height() - self.list.sizeHint().height()
        self.list.setFixedHeight(max(row_h, min(len(entries) * row_h + 6, self.MAX_H - top_h - 10)))
        card_.setFixedWidth(230)

    def done(self, data):
        self.choice = data
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def pick(self, pos):
        """pos(전역 좌표) 근처에 띄우고 고를 때까지 기다린다. → 고른 값, 취소면 False"""
        self.adjustSize()
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(pos.x(), area.right() - self.width())
        y = min(pos.y(), area.bottom() - self.height())
        self.move(max(area.left(), x), max(area.top(), y))
        loop = QEventLoop()
        self.destroyed.connect(loop.quit)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.show()
        while self.isVisible():
            loop.processEvents(QEventLoop.WaitForMoreEvents)
        return self.choice


class MainWindow(QMainWindow):
    ai_test_done = Signal(bool, str)
    repaint_done = Signal(int, object, str)
    models_loaded = Signal(list, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("psd-paint")
        self.resize(1180, 780)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.thread = None
        self.worker = None
        self.results = {}  # 행 번호 → Result
        self.done_count = 0
        self.preview_pix = None

        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 16, 20, 20)
        outer.setSpacing(style.GAP)
        outer.addLayout(self.build_header())

        # 왼쪽(파일·로그)과 오른쪽(미리보기·설정) 사이 간격을 끌어 폭을 조절한다
        self.split = QSplitter(Qt.Horizontal)
        self.split.setHandleWidth(style.GAP)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self.build_left())
        self.split.addWidget(self.build_right())
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        if self.settings.value("split"):
            self.split.restoreState(self.settings.value("split"))
        outer.addWidget(self.split, 1)
        self.setCentralWidget(root)
        self.update_summary()

        # 미리보기 이동: 키보드(+ - 0), 트랙패드 핀치, 스페이스를 누른 채 드래그
        for keys, act in ((("+", "="), lambda: self.set_zoom(self.zoom * 1.25)),
                          (("-",), lambda: self.set_zoom(self.zoom / 1.25)),
                          (("0",), lambda: self.set_zoom(1.0))):
            for k in keys:
                QShortcut(QKeySequence(k), self, act)
        self.space_down = False
        self.drag_from = None  # (마우스 전역 위치, 가로 스크롤, 세로 스크롤)
        self.preview.installEventFilter(self)
        QApplication.instance().installEventFilter(self)

    # ---- 화면 구성 ----
    def build_header(self):
        title = label("psd-paint", "title")
        sub = label("선화 PSD 자동 밑색", "subtitle")
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(title)
        names.addWidget(sub)

        self.stop_btn = QPushButton("중지")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.request_stop)
        self.run_btn = QPushButton("밑색 칠하기")
        self.run_btn.setObjectName("primary")
        self.run_btn.setShortcut("Ctrl+Return")
        self.run_btn.clicked.connect(lambda: self.start())

        row = QHBoxLayout()
        row.addLayout(names)
        row.addStretch()
        row.addWidget(self.stop_btn)
        row.addWidget(self.run_btn)
        return row

    def build_left(self):
        self.files = FileList()
        self.files.files_dropped.connect(self.add_files)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self.file_menu)
        self.files.currentItemChanged.connect(self.show_selected)

        add_btn = QPushButton("파일 추가")
        add_btn.clicked.connect(self.pick_files)
        self.remove_btn = QPushButton("선택 제거")
        self.remove_btn.setObjectName("ghost")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn = QPushButton("모두 지우기")
        self.clear_btn.setObjectName("ghost")
        self.clear_btn.clicked.connect(self.clear_files)
        self.summary = label("", "summary")
        bar = QHBoxLayout()
        bar.addWidget(label("파일", "section"))
        bar.addWidget(self.summary)
        bar.addStretch()
        bar.addWidget(self.remove_btn)
        bar.addWidget(self.clear_btn)
        bar.addWidget(add_btn)

        self.total_bar = QProgressBar()
        self.total_bar.setTextVisible(False)
        self.total_bar.setValue(0)
        files_card, _ = card(bar, self.files, self.total_bar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_card, _ = card(label("로그", "section"), self.log)
        log_card.setMaximumHeight(190)

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(style.GAP)
        v.addWidget(files_card, 1)
        v.addWidget(log_card)
        return w

    def build_right(self):
        tabs = QTabWidget()

        # 미리보기 탭
        self.preview = PreviewCanvas("완료된 파일을 고르면\n미리보기가 나옵니다")
        self.preview.clicked.connect(self.on_preview_click)
        self.sharpen = QTimer(self)  # 핀치가 멈추면 부드러운 보간으로 다시 그린다
        self.sharpen.setSingleShot(True)
        self.sharpen.setInterval(150)
        self.sharpen.timeout.connect(self.end_fast_zoom)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("preview")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setWidget(self.preview)
        self.scroll.viewport().installEventFilter(self)  # ⌘(Ctrl)+스크롤 확대·축소

        # 확대·축소: 배율은 '폭 맞춤'에 대한 비율이다
        self.zoom = 1.0
        self.zoom_out_btn = QPushButton("−")
        self.zoom_in_btn = QPushButton("+")
        self.zoom_fit_btn = QPushButton("맞춤")
        self.zoom_label = label("100%", "summary")
        self.zoom_label.setFixedWidth(44)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        for b in (self.zoom_out_btn, self.zoom_in_btn):
            b.setObjectName("ghost")
            b.setFixedWidth(30)
        self.zoom_fit_btn.setObjectName("ghost")
        self.zoom_out_btn.clicked.connect(lambda: self.set_zoom(self.zoom / 1.25))
        self.zoom_in_btn.clicked.connect(lambda: self.set_zoom(self.zoom * 1.25))
        self.zoom_fit_btn.clicked.connect(lambda: self.set_zoom(1.0))
        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.addStretch()
        for wdg in (self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.zoom_fit_btn):
            zoom_row.addWidget(wdg)
        self.open_btn = QPushButton("파일 열기")
        self.open_btn.clicked.connect(lambda: self.open_result(reveal=False))
        self.reveal_btn = QPushButton("폴더에서 보기")
        self.reveal_btn.clicked.connect(lambda: self.open_result(reveal=True))
        for b in (self.open_btn, self.reveal_btn):
            b.setEnabled(False)
        self.preview_info = label("", "summary")
        self.preview_info.setWordWrap(True)
        open_row = QHBoxLayout()
        open_row.addWidget(self.open_btn)
        open_row.addWidget(self.reveal_btn)
        # 클릭 수정: 미리보기에서 조각을 눌러 레이어·색을 바꾸고 다시 저장한다
        self.edit_info = label("조각을 누르면 레이어·색을 바꿀 수 있어요", "summary")
        self.undo_btn = QPushButton("되돌리기")
        self.undo_btn.setObjectName("ghost")
        self.undo_btn.clicked.connect(self.undo_edit)
        self.save_edit_btn = QPushButton("수정 저장")
        self.save_edit_btn.setObjectName("primary")
        self.save_edit_btn.clicked.connect(self.save_edits)
        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.addWidget(self.edit_info, 1)
        edit_row.addWidget(self.undo_btn)
        edit_row.addWidget(self.save_edit_btn)
        self.edit_bar = QWidget()
        self.edit_bar.setLayout(edit_row)
        self.edits = {}  # 행 번호 → [(영역, 바꾸기 전 키)]
        self.repaint_done.connect(self.on_repaint_done)

        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 8, 0, 0)
        pl.addLayout(zoom_row)
        pl.addWidget(self.scroll, 1)
        pl.addWidget(self.edit_bar)
        pl.addWidget(self.preview_info)
        pl.addLayout(open_row)
        tabs.addTab(pv, "미리보기")

        tabs.addTab(self.build_settings(), "설정")
        c, _ = card(tabs)
        return c

    def build_settings(self):
        """설정 탭. 모든 행을 '고정폭 라벨 | 입력' 두 칸 격자에 맞춘다."""
        s = self.settings
        d = Options()
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(0, 8, 8, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(0, LABEL_W)
        grid.setColumnStretch(1, 1)
        row = 0

        def section(text):
            nonlocal row
            lb = label(text, "section")
            if row:
                grid.setRowMinimumHeight(row, 10)
                row += 1
            grid.addWidget(lb, row, 0, 1, 2)
            row += 1

        def add(text, item, top=False):
            nonlocal row
            if text:
                lb = label(text, "field")
                lb.setFixedWidth(LABEL_W)
                if top:  # 여러 줄 항목은 첫 줄에 맞춘다
                    lb.setContentsMargins(0, 4, 0, 0)
                grid.addWidget(lb, row, 0, Qt.AlignLeft | (Qt.AlignTop if top else Qt.AlignVCenter))
            if isinstance(item, QWidget):
                grid.addWidget(item, row, 1)
            else:
                grid.addLayout(item, row, 1)
            row += 1

        def hbox(*items, stretch=False):
            h = QHBoxLayout()
            h.setSpacing(8)
            for it in items:
                h.addWidget(it)
            if stretch:
                h.addStretch()
            return h

        def vbox(*items):
            box = QVBoxLayout()
            box.setSpacing(4)
            for it in items:
                box.addWidget(it)
            return box

        def spin(lo, hi, value, suffix="", special=""):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            if suffix:
                sp.setSuffix(suffix)
            if special:
                sp.setSpecialValueText(special)
            sp.setValue(value)
            sp.setFixedWidth(120)
            # QSpinBox는 스타일시트 padding이 안쪽 입력칸에 먹지 않아 여백과 높이를 직접 맞춘다
            sp.lineEdit().setTextMargins(6, 0, 6, 0)
            sp.setFixedHeight(style.INPUT_H)
            return sp

        # 참고 방식
        self.src_layer = QRadioButton("파일 안 참고 레이어")
        self.src_layer.setToolTip("PSD 안의 '참고' 폴더(점·낙서 힌트) 또는 칠해 둔 '참고' 레이어를 봅니다.\n"
                                  "없으면 PSD 옆의 '파일이름_참고.png'를 찾고, 그것도 없으면 자동으로 칠합니다")
        self.src_image = QRadioButton("레퍼런스 이미지 (PNG·JPG) · AI")
        self.src_psd = QRadioButton("레퍼런스 PSD (캐릭터 시트·채색 참고) · AI")
        self.src_psd.setToolTip("밑색이 칠해진 PSD. 그 안의 '밑색'(또는 '참고') 폴더 레이어 이름과 색을 그대로 씁니다")
        src_group = QButtonGroup(w)
        for b in (self.src_layer, self.src_image, self.src_psd):
            src_group.addButton(b)
        {"image": self.src_image, "psd": self.src_psd}.get(s.value("hint_source", "layer"), self.src_layer).setChecked(True)
        self.hint_path = QLineEdit(s.value("hint_path", ""))
        self.hint_path.setPlaceholderText("레퍼런스 파일")
        self.hint_browse = QPushButton("찾기")
        self.hint_browse.clicked.connect(self.pick_hint_path)
        ref_note = label("다른 페이지·캐릭터 시트는 AI가 인물과 부위를 맞춰 칠합니다 (아래 AI 연결). "
                         "같은 페이지를 칠한 레퍼런스면 AI 없이 바로 칠합니다.", "summary")
        ref_note.setWordWrap(True)
        section("참고 방식")
        add("방식", vbox(self.src_layer, self.src_image, self.src_psd), top=True)
        add("레퍼런스", hbox(self.hint_path, self.hint_browse))
        add("", ref_note)
        for b in (self.src_layer, self.src_image, self.src_psd):
            b.toggled.connect(self.update_ref_enabled)
        self.update_ref_enabled()

        # 결과
        self.same_dir = QRadioButton("원본과 같은 폴더")
        self.other_dir = QRadioButton("지정한 폴더")
        group = QButtonGroup(w)
        group.addButton(self.same_dir)
        group.addButton(self.other_dir)
        (self.other_dir if s.value("use_out_dir", False, bool) else self.same_dir).setChecked(True)
        self.out_dir = QLineEdit(s.value("out_dir", ""))
        self.out_dir.setPlaceholderText("결과 폴더")
        browse = QPushButton("찾기")
        browse.clicked.connect(self.pick_out_dir)
        self.suffix = QLineEdit(s.value("suffix", "_밑색"))
        section("결과")
        add("저장 위치", hbox(self.same_dir, self.other_dir, stretch=True))
        add("결과 폴더", hbox(self.out_dir, browse))
        add("파일 이름 뒤", self.suffix)

        # [파일 열기]에 쓸 앱. 자동이면 클립스튜디오 → 포토샵 → 기본 앱 순서로 설치된 것을 쓴다
        self.apps = find_apps()
        self.open_app = QComboBox()
        for text, key, ok in (("자동 (클립스튜디오 → 포토샵 → 기본 앱)", "auto", True),
                              ("클립스튜디오" + ("" if self.apps["clip"] else " (설치 안 됨)"), "clip", bool(self.apps["clip"])),
                              ("포토샵" + ("" if self.apps["photoshop"] else " (설치 안 됨)"), "photoshop",
                               bool(self.apps["photoshop"])),
                              ("기본 앱 (macOS·Windows 연결 앱)", "default", True)):
            self.open_app.addItem(text, key)
            if not ok:
                self.open_app.model().item(self.open_app.count() - 1).setEnabled(False)
        self.open_app.setCurrentIndex(max(0, self.open_app.findData(s.value("open_app", "auto"))))
        self.open_app.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.open_app.setMinimumContentsLength(8)
        add("열 앱", self.open_app)

        # 레이어 이름
        self.line_group = QLineEdit(s.value("line_group", d.line_group))
        self.hint_group = QLineEdit(s.value("hint_group", d.hint_group))
        self.boundary = QLineEdit(s.value("boundary", ", ".join(d.extra_boundary)))
        self.boundary.setToolTip("선화 외에 경계로 쓸 레이어 이름. 쉼표로 구분하고 * 를 쓸 수 있습니다 (예: 컷*, スマートフォン*)")
        self.background = QLineEdit(s.value("background", ", ".join(d.background)))
        self.background.setToolTip("배경 레이어·폴더 이름 패턴. 선화 폴더 안에 있어도 경계에서 빼고, 결과에서는 끕니다")
        self.hide_bg = QCheckBox("결과 PSD에서 배경 레이어 끄기")
        self.hide_bg.setChecked(s.value("hide_bg", d.hide_background, bool))
        section("레이어")
        add("선화 폴더", self.line_group)
        add("참고 폴더", self.hint_group)
        add("경계 레이어", self.boundary)
        add("배경 이름", self.background)
        add("배경 끄기", self.hide_bg)

        # 칠하기
        self.auto_fill = QCheckBox("힌트 없는 영역을 회색으로 칠하기")
        self.auto_fill.setChecked(s.value("auto_fill", d.auto_fill, bool))
        self.max_layers = spin(0, 50, s.value("max_layers", d.max_layers, int), " 개", "제한 없음")
        cap_note = label("넘으면 면적이 작은 레이어부터 '기타'로 합칩니다. 0이면 제한하지 않습니다.", "summary")
        cap_note.setWordWrap(True)
        self.offset = spin(0, 10, s.value("offset", d.offset_px, int), " px")
        self.gap = spin(0, 10, s.value("gap", d.gap_close_px, int), " px")
        section("칠하기")
        add("자동 칠하기", self.auto_fill)
        add("레이어 상한", hbox(self.max_layers, stretch=True))
        add("", cap_note)
        add("윤곽 안쪽으로", hbox(self.offset, stretch=True))
        add("선 틈 닫기", hbox(self.gap, stretch=True))

        # AI 연결
        self.provider = QComboBox()
        for pid, info in ai.PROVIDERS.items():
            self.provider.addItem(info["label"], pid)
        self.provider.setCurrentIndex(max(0, self.provider.findData(s.value("provider", "claude"))))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.list_btn = QPushButton("목록 불러오기")
        self.list_btn.setToolTip("API 키로 쓸 수 있는 모델 이름을 받아 옵니다")
        self.list_btn.clicked.connect(self.fetch_models)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("API 키 (OS 키체인에 저장)")
        self.test_btn = QPushButton("연결 테스트")
        self.test_btn.clicked.connect(self.test_ai)
        self.ai_status = label("", "summary")
        self.ai_status.setWordWrap(True)
        self.provider.currentIndexChanged.connect(self.load_provider)
        self.api_key.editingFinished.connect(self.save_api_key)
        self.ai_mode = QComboBox()
        for text, mode in (("API로 바로 매칭", "api"), ("요청 폴더 만들기 (Claude Code·Codex에서 답하기)", "files"),
                           ("끄기", "off")):
            self.ai_mode.addItem(text, mode)
        self.ai_mode.setCurrentIndex(max(0, self.ai_mode.findData(s.value("ai_mode", "api"))))
        section("AI 연결 (선택)")
        # 긴 항목 때문에 설정 탭이 넓어지지 않게 콤보박스는 내용 폭에 맞추지 않는다
        for combo in (self.ai_mode, self.provider, self.model):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add("AI 매칭", self.ai_mode)
        self.ai_review = QCheckBox("칠한 결과를 한 번 더 보고 고치기 (요청 1회 추가)")
        self.ai_review.setChecked(s.value("ai_review", False, bool))
        add("검수", self.ai_review)
        self.ai_zoom = QCheckBox("인물마다 크게 잘라 부위를 다시 검토 (요청 1회 추가)")
        self.ai_zoom.setChecked(s.value("ai_zoom", True, bool))
        add("인물 확대", self.ai_zoom)
        add("제공자", self.provider)
        add("모델", hbox(self.model, self.list_btn))
        add("API 키", hbox(self.api_key, self.test_btn))
        add("", self.ai_status)
        self.ai_test_done.connect(self.on_ai_test)
        self.models_loaded.connect(self.on_models_loaded)
        self.load_provider()
        grid.setRowStretch(row, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(w)
        self.settings_page = w
        return scroll

    # ---- 설정 ----
    def save_settings(self):
        s = self.settings
        s.setValue("use_out_dir", self.other_dir.isChecked())
        s.setValue("out_dir", self.out_dir.text())
        s.setValue("suffix", self.suffix.text())
        s.setValue("open_app", self.open_app.currentData())
        s.setValue("line_group", self.line_group.text())
        s.setValue("hint_group", self.hint_group.text())
        s.setValue("boundary", self.boundary.text())
        s.setValue("background", self.background.text())
        s.setValue("hide_bg", self.hide_bg.isChecked())
        s.setValue("auto_fill", self.auto_fill.isChecked())
        s.setValue("max_layers", self.max_layers.value())
        s.setValue("offset", self.offset.value())
        s.setValue("gap", self.gap.value())
        s.setValue("hint_source", self.hint_source())
        s.setValue("hint_path", self.hint_path.text())
        pid = self.provider.currentData()
        s.setValue("provider", pid)
        s.setValue("ai_mode", self.ai_mode.currentData())
        s.setValue("ai_review", self.ai_review.isChecked())
        s.setValue("ai_zoom", self.ai_zoom.isChecked())
        s.setValue(f"model_{pid}", self.model.currentText())
        self.save_api_key()

    def options(self):
        def patterns(edit):
            return [p.strip() for p in edit.text().split(",") if p.strip()]
        return Options(
            line_group=self.line_group.text().strip() or "선화",
            hint_group=self.hint_group.text().strip() or "참고",
            hint_source=self.hint_source(),
            hint_path=self.hint_path.text().strip(),
            ai_mode=self.ai_mode.currentData(),
            ai_provider=self.provider.currentData(),
            ai_model=self.model.currentText().strip() or ai.PROVIDERS[self.provider.currentData()]["model"],
            ai_key=self.api_key.text().strip(),
            ai_review=self.ai_review.isChecked(),
            ai_zoom=self.ai_zoom.isChecked(),
            extra_boundary=patterns(self.boundary),
            background=patterns(self.background),
            hide_background=self.hide_bg.isChecked(),
            auto_fill=self.auto_fill.isChecked(),
            max_layers=self.max_layers.value(),
            offset_px=self.offset.value(),
            gap_close_px=self.gap.value(),
        )

    def hint_source(self):
        return "image" if self.src_image.isChecked() else "psd" if self.src_psd.isChecked() else "layer"

    def update_ref_enabled(self):
        on = not self.src_layer.isChecked()
        self.hint_path.setEnabled(on)
        self.hint_browse.setEnabled(on)

    def pick_hint_path(self):
        filt = "이미지 (*.png *.jpg *.jpeg *.webp)" if self.src_image.isChecked() else "Photoshop (*.psd)"
        path, _ = QFileDialog.getOpenFileName(self, "레퍼런스 파일", self.hint_path.text(), filt)
        if path:
            self.hint_path.setText(path)

    # ---- AI 연결 ----
    def load_provider(self):
        pid = self.provider.currentData()
        info = ai.PROVIDERS[pid]
        self.model.clear()
        self.model.addItems(info["models"])
        self.model.setCurrentText(self.settings.value(f"model_{pid}", info["model"]))
        self.model.lineEdit().setPlaceholderText("모델 이름")
        self.api_key.setText(ai.get_key(pid))
        self.ai_status.setText("")
        self._loaded_provider = pid

    def save_api_key(self):
        pid = getattr(self, "_loaded_provider", None)
        if pid:
            try:
                ai.set_key(pid, self.api_key.text().strip())
            except Exception as e:  # 키체인을 쓸 수 없는 환경
                self.ai_status.setText(f"키를 저장하지 못했습니다: {e}")

    def test_ai(self):
        self.save_api_key()
        self.test_btn.setEnabled(False)
        self.ai_status.setObjectName("summary")
        self.ai_status.setText("확인 중…")
        args = (self.provider.currentData(), self.api_key.text().strip(), self.model.currentText().strip())
        threading.Thread(target=lambda: self.ai_test_done.emit(*ai.test_connection(*args)), daemon=True).start()

    def fetch_models(self):
        pid, key = self.provider.currentData(), self.api_key.text().strip()
        if not key:
            self.on_ai_test(False, "API 키를 먼저 입력하세요")
            return
        self.save_api_key()
        self.list_btn.setEnabled(False)
        self.ai_status.setText("모델 목록을 받는 중…")

        def run():
            try:
                names = ai.list_models(pid, key)
                self.models_loaded.emit(names, "")
            except Exception as e:  # 네트워크·인증 오류를 화면에 보여 준다
                self.models_loaded.emit([], str(e) or type(e).__name__)
        threading.Thread(target=run, daemon=True).start()

    def on_models_loaded(self, names, err):
        self.list_btn.setEnabled(True)
        if err or not names:
            self.on_ai_test(False, f"모델 목록을 받지 못했습니다{': ' + err if err else ''}")
            return
        current = self.model.currentText()
        self.model.clear()
        self.model.addItems(names)
        self.model.setCurrentText(current if current in names else names[0])
        self.on_ai_test(True, f"모델 {len(names)}개를 불러왔습니다")

    def on_ai_test(self, ok, msg):
        self.test_btn.setEnabled(True)
        self.ai_status.setObjectName("ok" if ok else "fail")
        self.ai_status.setStyleSheet("")  # objectName 바뀐 스타일 다시 적용
        self.ai_status.setText(("✓ " if ok else "✕ ") + msg)

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "결과 폴더", self.out_dir.text())
        if d:
            self.out_dir.setText(d)
            self.other_dir.setChecked(True)

    # ---- 파일 목록 ----
    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "PSD 파일", self.settings.value("last_dir", ""),
                                                "Photoshop (*.psd)")
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
            self.add_files([Path(p) for p in paths])

    def add_files(self, paths):
        have = {self.files.topLevelItem(i).data(COL_FILE, Qt.UserRole) for i in range(self.files.topLevelItemCount())}
        for p in paths:
            if str(p) in have:
                continue
            have.add(str(p))
            item = QTreeWidgetItem([p.name, "", ""])
            item.setData(COL_FILE, Qt.UserRole, str(p))
            item.setToolTip(COL_FILE, str(p))
            self.set_status(item, "대기", style.MUTED)
            self.files.addTopLevelItem(item)
        self.update_summary()

    def file_menu(self, pos):
        """파일 목록 우클릭 메뉴: 선택한 파일만 칠하기, 파일별 레퍼런스 지정·해제, 결과 열기, 제거"""
        items = self.files.selectedItems()
        if not items:
            item = self.files.itemAt(pos)
            if item is None:
                return
            item.setSelected(True)
            items = [item]
        rows = [self.files.indexOfTopLevelItem(i) for i in items]
        running = self.worker is not None
        menu = rounded_menu(self)
        run = menu.addAction(f"선택한 파일만 칠하기 ({len(rows)}개)")
        run.setEnabled(not running)
        menu.addSeparator()
        set_ref = menu.addAction("레퍼런스 지정…")
        clear_ref = menu.addAction("레퍼런스 지정 해제")
        clear_ref.setEnabled(any(i.data(COL_FILE, REF_ROLE) for i in items))
        menu.addSeparator()
        open_res = menu.addAction("결과 파일 열기")
        open_res.setEnabled(len(rows) == 1 and rows[0] in self.results)
        remove = menu.addAction("목록에서 제거")
        remove.setEnabled(not running)
        chosen = menu.exec(self.files.viewport().mapToGlobal(pos))
        if chosen is run:
            self.start(rows)
        elif chosen is set_ref:
            path, _ = QFileDialog.getOpenFileName(self, "이 파일의 레퍼런스", self.settings.value("last_dir", ""),
                                                  "레퍼런스 (*.png *.jpg *.jpeg *.webp *.psd)")
            if path:
                for i in items:
                    self.set_item_ref(i, path)
        elif chosen is clear_ref:
            for i in items:
                self.set_item_ref(i, None)
        elif chosen is open_res:
            open_path(self.results[rows[0]].out_path, app=self.app_for_open())
        elif chosen is remove:
            self.remove_selected()

    @staticmethod
    def set_item_ref(item, path):
        """파일별 레퍼런스를 지정하고 파일 이름 옆에 표시한다."""
        item.setData(COL_FILE, REF_ROLE, path)
        name = Path(item.data(COL_FILE, Qt.UserRole)).name
        item.setText(COL_FILE, f"{name}   ↳ {Path(path).name}" if path else name)
        item.setToolTip(COL_FILE, item.data(COL_FILE, Qt.UserRole) + (f"\n레퍼런스: {path}" if path else ""))

    def remove_selected(self):
        for item in self.files.selectedItems():
            self.files.takeTopLevelItem(self.files.indexOfTopLevelItem(item))
        self.update_summary()

    def clear_files(self):
        self.files.clear()
        self.results.clear()
        self.update_summary()
        self.show_selected(None)

    def update_summary(self):
        n = self.files.topLevelItemCount()
        self.summary.setText(f"{n}개" if n else "")

    @staticmethod
    def set_status(item, text, color):
        item.setText(COL_STATUS, text)
        item.setForeground(COL_STATUS, QColor(color))

    # ---- 실행 ----
    def start(self, rows=None):
        """rows가 있으면 그 행만, 없으면 목록 전체를 칠한다."""
        n = self.files.topLevelItemCount()
        rows = list(range(n)) if rows is None else sorted(rows)
        if not n or not rows:
            QMessageBox.information(self, APP_NAME, "PSD 파일을 먼저 추가하세요.")
            return
        if self.other_dir.isChecked() and not self.out_dir.text().strip():
            QMessageBox.information(self, APP_NAME, "결과 폴더를 지정하세요.")
            return
        self.save_settings()
        out_dir = self.out_dir.text().strip() if self.other_dir.isChecked() else None
        jobs = []
        for row in rows:
            item = self.files.topLevelItem(row)
            src = item.data(COL_FILE, Qt.UserRole)
            jobs.append((row, src, output_path(src, out_dir, self.suffix.text()), item.data(COL_FILE, REF_ROLE)))
            self.set_status(item, "대기", style.MUTED)
            item.setText(COL_INFO, "")
            self.results.pop(row, None)
            self.edits.pop(row, None)
        self.done_count = 0
        self.waiting = 0
        self.job_rows = set(rows)
        self.total_bar.setRange(0, len(jobs) * 100)
        self.total_bar.setValue(0)
        self.set_running(True)
        self.log.appendPlainText(f"── {len(jobs)}개 파일 시작")

        self.thread = QThread()
        # macOS 기본 스레드 스택(512KB)으로는 psd-tools·scipy 처리 중 스택이 넘친다
        self.thread.setStackSize(64 * 1024 * 1024)
        self.worker = Worker(jobs, self.options())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.started_file.connect(self.on_started)
        self.worker.progressed.connect(self.on_progress)
        self.worker.finished_file.connect(self.on_finished)
        self.worker.failed_file.connect(self.on_failed)
        self.worker.waiting_file.connect(self.on_waiting)
        self.worker.all_done.connect(self.on_all_done)
        self.worker.all_done.connect(self.thread.quit)
        self.thread.start()

    def request_stop(self):
        if self.worker:
            self.worker.stop = True
            self.stop_btn.setEnabled(False)
            self.log.appendPlainText("중지 요청: 지금 파일을 마치고 멈춥니다")

    def set_running(self, running):
        self.run_btn.setEnabled(not running)
        self.run_btn.setText("칠하는 중…" if running else "밑색 칠하기")
        self.stop_btn.setEnabled(running)
        for w in (self.settings_page, self.remove_btn, self.clear_btn):
            w.setEnabled(not running)
        self.files.setAcceptDrops(not running)

    def on_started(self, row):
        item = self.files.topLevelItem(row)
        self.files.setCurrentItem(item)
        self.log.appendPlainText(f"[{row + 1}] {item.text(COL_FILE)}")

    def on_progress(self, row, msg, frac):
        self.set_status(self.files.topLevelItem(row), f"{int(frac * 100)}%  {msg}", style.ACCENT_HI)
        self.total_bar.setValue(self.done_count * 100 + int(frac * 100))

    def on_finished(self, row, result, secs):
        item = self.files.topLevelItem(row)
        self.results[row] = result
        self.set_status(item, "● 완료", style.OK)
        note = f"레이어 {len(result.layers)}개"
        item.setText(COL_INFO, note)
        item.setForeground(COL_INFO, QColor(style.MUTED))
        item.setToolTip(COL_INFO, self.describe(result))
        self.log.appendPlainText(f"    → {result.out_path.name} ({secs:.1f}초) {note}")
        if result.ai_usage:
            u = result.ai_usage
            self.log.appendPlainText(f"    AI 매칭: {ai.describe_usage(u)}")
        self.step()
        if self.files.currentItem() is item:
            self.show_selected(item)

    def on_failed(self, row, err):
        item = self.files.topLevelItem(row)
        self.set_status(item, "● 실패", style.FAIL)
        item.setText(COL_INFO, err)
        item.setForeground(COL_INFO, QColor(style.FAIL))
        item.setToolTip(COL_INFO, err)
        self.log.appendPlainText(f"    실패: {err}")
        self.step()

    def on_waiting(self, row, msg):
        item = self.files.topLevelItem(row)
        self.set_status(item, "● 답 대기", style.WARN)
        item.setText(COL_INFO, "AI 매칭 요청 폴더 생성")
        item.setForeground(COL_INFO, QColor(style.MUTED))
        item.setToolTip(COL_INFO, msg)
        self.log.appendPlainText(f"    {msg}")
        self.waiting += 1
        self.step()

    def step(self):
        self.done_count += 1
        self.total_bar.setValue(self.done_count * 100)

    def on_all_done(self):
        ok = sum(1 for r in self.job_rows if r in self.results)
        for row in self.job_rows:  # 이번에 돌린 파일 중 처리하지 못한 것만 표시한다
            item = self.files.topLevelItem(row)
            if item.text(COL_STATUS) == "대기":
                self.set_status(item, "중지됨", style.WARN)
        self.log.appendPlainText(f"── 끝: 완료 {ok}개, 답 대기 {self.waiting}개, 실패 {self.done_count - ok - self.waiting}개")
        self.set_running(False)
        self.worker = None

    # ---- 미리보기 · 열기 ----
    @staticmethod
    def describe(result):
        lines = [f"레이어: {', '.join(result.layers)}"]
        if result.auto:
            lines.append(f"자동으로 칠한 영역 {result.auto}개 ('자동' 레이어)")
        if result.merged:
            lines.append(f"'기타'로 합친 레이어: {', '.join(result.merged)}")
        return "\n".join(lines)

    def show_selected(self, item, _prev=None):
        result = self.results.get(self.files.indexOfTopLevelItem(item)) if item else None
        for b in (self.open_btn, self.reveal_btn):
            b.setEnabled(result is not None)
        if result is None or result.preview is None:
            self.preview_pix = None
            self.preview.clear("완료된 파일을 고르면\n미리보기가 나옵니다")
            self.preview_info.setText("")
            self.update_edit_bar()
            return
        self._qimage = ImageQt(result.preview.convert("RGB"))  # QImage가 쓰는 동안 버퍼를 살려 둔다
        self.preview_pix = QPixmap.fromImage(self._qimage)
        if _prev is not None:  # 다른 파일을 고르면 폭 맞춤으로 돌아간다
            self.zoom = 1.0
        self.preview_info.setText(self.describe(result))
        self.fit_preview()
        self.update_edit_bar()

    def fit_preview(self):
        if self.preview_pix is None:
            return
        w = max(50, round((self.scroll.viewport().width() - 4) * self.zoom))
        h = max(1, round(w * self.preview_pix.height() / self.preview_pix.width()))
        self.preview.set_image(self.preview_pix)
        self.preview.set_display(w, h)
        self.zoom_label.setText(f"{round(self.zoom * 100)}%")

    def end_fast_zoom(self):
        self.preview.fast = False
        self.preview.update()

    def set_zoom(self, zoom, anchor=None):
        """배율을 바꾼다. anchor(뷰포트 좌표)가 있으면 그 점이 제자리에 있게 스크롤을 맞춘다."""
        zoom = min(8.0, max(0.25, zoom))  # 폭 맞춤보다 작게도 볼 수 있다 (긴 웹툰 전체 보기)
        if self.preview_pix is None or abs(zoom - self.zoom) < 1e-6:
            self.zoom = zoom
            self.zoom_label.setText(f"{round(self.zoom * 100)}%")
            return
        h, v = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
        vp = self.scroll.viewport()
        ax, ay = (anchor.x(), anchor.y()) if anchor is not None else (vp.width() / 2, vp.height() / 2)
        fx = (h.value() + ax) / max(1, self.preview.width())
        fy = (v.value() + ay) / max(1, self.preview.height())
        self.zoom = zoom
        self.fit_preview()
        QApplication.processEvents()
        h.setValue(round(fx * self.preview.width() - ax))
        v.setValue(round(fy * self.preview.height() - ay))

    def typing(self):
        """글자를 입력하는 칸에 포커스가 있으면 단축키·스페이스 이동을 쓰지 않는다."""
        w = QApplication.focusWidget()
        return isinstance(w, (QLineEdit, QSpinBox, QPlainTextEdit)) or (isinstance(w, QComboBox) and w.isEditable())

    def over_preview(self):
        return self.preview_pix is not None and self.scroll.isVisible() and \
            self.scroll.rect().contains(self.scroll.mapFromGlobal(QCursor.pos()))

    def eventFilter(self, obj, e):
        t = e.type()
        # 스페이스를 누르고 있는 동안 미리보기를 손으로 잡아 이동한다
        if t in (QEvent.KeyPress, QEvent.KeyRelease) and e.key() == Qt.Key_Space and not e.isAutoRepeat():
            if t == QEvent.KeyPress and not self.typing() and self.over_preview():
                self.space_down = True
                self.preview.setCursor(Qt.OpenHandCursor)
                return True
            if t == QEvent.KeyRelease and self.space_down:
                self.space_down = False
                self.drag_from = None
                self.update_edit_bar()  # 손 모양을 원래 커서로 되돌린다
                return True
        if obj is self.preview and self.space_down:
            if t == QEvent.MouseButtonPress:
                h, v = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
                self.drag_from = (e.globalPosition(), h.value(), v.value())
                self.preview.setCursor(Qt.ClosedHandCursor)
                return True  # 이동 중에는 클릭 수정 메뉴를 띄우지 않는다
            if t == QEvent.MouseMove and self.drag_from:
                start, hx, vy = self.drag_from
                d = e.globalPosition() - start
                self.scroll.horizontalScrollBar().setValue(round(hx - d.x()))
                self.scroll.verticalScrollBar().setValue(round(vy - d.y()))
                return True
            if t == QEvent.MouseButtonRelease:
                self.drag_from = None
                self.preview.setCursor(Qt.OpenHandCursor)
                return True
        if obj in (self.scroll.viewport(), self.preview):
            # ⌘(Ctrl)+스크롤 확대·축소
            if t == QEvent.Wheel and e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
                step = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
                pos = self.scroll.viewport().mapFromGlobal(e.globalPosition().toPoint())
                self.set_zoom(self.zoom * step, pos)
                return True
            # 트랙패드 핀치 확대·축소 (macOS)
            if t == QEvent.NativeGesture and e.gestureType() == Qt.ZoomNativeGesture:
                pos = self.scroll.viewport().mapFromGlobal(e.globalPosition().toPoint())
                self.preview.fast = True  # 핀치 중에는 빠르게, 멈추면 다시 부드럽게
                self.sharpen.start()
                self.set_zoom(self.zoom * (1 + e.value()), pos)
                return True
        return super().eventFilter(obj, e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_preview()

    # ---- 클릭 수정 ----
    def current_row(self):
        item = self.files.currentItem()
        return self.files.indexOfTopLevelItem(item) if item else -1

    def update_edit_bar(self):
        row = self.current_row()
        result = self.results.get(row)
        has_state = bool(result and result.state)
        self.edit_bar.setVisible(has_state)
        n = len(self.edits.get(row, []))
        self.edit_info.setText(f"수정 {n}곳 · 저장하면 PSD에 반영돼요" if n else "조각을 누르면 레이어·색을 바꿀 수 있어요")
        self.undo_btn.setEnabled(n > 0)
        self.save_edit_btn.setEnabled(n > 0)
        self.preview.setCursor(Qt.PointingHandCursor if has_state else Qt.ArrowCursor)

    def set_preview_image(self, im):
        self._qimage = ImageQt(im.convert("RGB"))  # QImage가 쓰는 동안 버퍼를 살려 둔다
        self.preview_pix = QPixmap.fromImage(self._qimage)
        self.fit_preview()

    def edit_menu(self, st, region):
        """누른 조각에 쓸 레이어·색 메뉴. → 고른 (레이어, RGB), 비우기면 (None, None), 취소면 False"""
        cur = int(st["region_key"][region])
        # 이 파일에서 실제로 쓰인 색만, 레이어 이름 순서대로 보여 준다. 같은 이름이 여럿이면 번호를 붙인다
        used = set(int(k) for k in np.unique(st["region_key"]) if k >= 0) | ({cur} if cur >= 0 else set())
        entries = []
        for k in sorted(used, key=lambda k: (st["names"].index(st["keys"][k][0]) if st["keys"][k][0] in st["names"]
                                             else len(st["names"]), k)):
            name, rgb = st["keys"][k]
            rgb = tuple(int(v) for v in rgb)
            if (name, rgb) not in [(n, c) for n, c, _ in entries]:
                entries.append((name, rgb, k))
        count = {}
        for name, _, _ in entries:
            count[name] = count.get(name, 0) + 1
        nth, labels = {}, {}
        for name, rgb, k in entries:
            nth[name] = nth.get(name, 0) + 1
            labels[(name, rgb)] = f"{name} {nth[name]}" if count[name] > 1 else name

        cur_key = (st["keys"][cur][0], tuple(int(v) for v in st["keys"][cur][1])) if cur >= 0 else None
        cur_label = labels.get(cur_key) if cur_key else None
        items = [(f"{labels[(name, rgb)]}   #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}", rgb, [name, list(rgb)],
                  (name, rgb) != cur_key) for name, rgb, _ in entries]
        data = ColorPicker(self, cur_label or "칠하지 않음", items, cur >= 0).pick(QCursor.pos())
        if data is None or data is False:
            return False
        if data == "clear":
            return None, None
        if data == "new":
            name, ok = QInputDialog.getText(self, "새 레이어", "레이어 이름:")
            if not ok or not name.strip():
                return False
            gray, ok = QInputDialog.getInt(self, "새 레이어", "회색 값 (0 검정 ~ 255 흰색):", 180, 0, 255)
            return (name.strip(), (gray, gray, gray)) if ok else False
        return data[0], tuple(data[1])

    def on_preview_click(self, fx, fy):
        row = self.current_row()
        result = self.results.get(row)
        if not result or not result.state:
            return
        st = result.state
        h, w = st["labels"].shape
        region = region_at(st, fx * w, fy * h)
        if not region:
            return
        # 누른 조각을 빨갛게 표시하고 메뉴를 띄운다
        base = result.preview.convert("RGB")
        mask = Image.fromarray(((st["labels"] == region) * 255).astype("uint8")).resize(base.size, Image.NEAREST)
        self.set_preview_image(Image.composite(Image.new("RGB", base.size, (235, 70, 70)), base,
                                               mask.point(lambda v: 150 if v else 0)))
        picked = self.edit_menu(st, region)
        if picked is False:
            self.set_preview_image(result.preview)
            return
        self.edits.setdefault(row, []).append((region, int(st["region_key"][region])))
        set_region(st, region, *picked)
        result.preview = quick_preview(st)
        self.set_preview_image(result.preview)
        self.update_edit_bar()

    def undo_edit(self):
        row = self.current_row()
        result = self.results.get(row)
        if not result or not self.edits.get(row):
            return
        region, prev = self.edits[row].pop()
        result.state["region_key"][region] = prev
        result.preview = quick_preview(result.state)
        self.set_preview_image(result.preview)
        self.update_edit_bar()

    def save_edits(self):
        row = self.current_row()
        result = self.results.get(row)
        if not result or not self.edits.get(row):
            return
        self.save_edit_btn.setEnabled(False)
        self.edit_info.setText("저장하는 중…")
        state = result.state

        def run():
            try:
                self.repaint_done.emit(row, repaint(state), "")
            except Exception as e:  # 저장 실패를 화면에 알린다
                self.repaint_done.emit(row, None, str(e) or type(e).__name__)
        threading.Thread(target=run, daemon=True).start()

    def on_repaint_done(self, row, new_result, err):
        if err:
            self.log.appendPlainText(f"    수정 저장 실패: {err}")
            self.update_edit_bar()
            return
        n = len(self.edits.get(row, []))
        self.results[row] = new_result
        self.edits[row] = []
        self.log.appendPlainText(f"    수정 {n}곳 저장 → {new_result.out_path.name}")
        if self.current_row() == row:
            self.show_selected(self.files.topLevelItem(row))

    def app_for_open(self):
        choice = self.open_app.currentData()
        if choice == "auto":
            return self.apps["clip"] or self.apps["photoshop"]
        if choice in ("clip", "photoshop"):
            return self.apps[choice]
        return None

    def open_result(self, reveal):
        item = self.files.currentItem()
        result = self.results.get(self.files.indexOfTopLevelItem(item)) if item else None
        if result:
            open_path(result.out_path, reveal=reveal, app=self.app_for_open())

    def closeEvent(self, e):
        self.save_settings()
        self.settings.setValue("split", self.split.saveState())
        if self.thread and self.thread.isRunning():
            self.request_stop()
            self.thread.wait()
        super().closeEvent(e)


def main():
    # 깊은 import 사슬은 작업 스레드보다 메인 스레드에서 미리 끝내 둔다
    import psd_tools.composite  # noqa: F401
    import scipy.ndimage  # noqa: F401

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "icon.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(style.QSS)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
