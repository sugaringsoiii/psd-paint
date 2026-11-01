"""psd-paint 데스크톱 앱: PSD 여러 개를 넣고 순서대로 밑색을 칠한다."""
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QRectF, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import threading

from . import ai, style
from .cli import output_path
from .core import AwaitingAnswer, Options, PaintError, paint

APP_NAME = "psd-paint"
COL_FILE, COL_STATUS, COL_INFO = range(3)
LABEL_W = 96  # 설정 탭 라벨 고정폭


def open_path(path, reveal=False):
    """파일을 연결된 앱으로 연다. reveal이면 파일 탐색기(Finder)에서 위치를 보여 준다."""
    path = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    elif sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", "/select,", path])
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
        self.jobs = jobs  # [(행 번호, 원본, 결과 경로)]
        self.opts = opts
        self.stop = False

    def run(self):
        for row, src, dst in self.jobs:
            if self.stop:
                break
            self.started_file.emit(row)
            t = time.time()
            try:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                result = paint(src, dst, self.opts,
                               progress=lambda msg, frac, row=row: self.progressed.emit(row, msg, frac))
                self.finished_file.emit(row, result, time.time() - t)
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


class MainWindow(QMainWindow):
    ai_test_done = Signal(bool, str)
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

        body = QHBoxLayout()
        body.setSpacing(style.GAP)
        body.addWidget(self.build_left(), 3)
        body.addWidget(self.build_right(), 2)
        outer.addLayout(body, 1)
        self.setCentralWidget(root)
        self.update_summary()

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
        self.run_btn.clicked.connect(self.start)

        row = QHBoxLayout()
        row.addLayout(names)
        row.addStretch()
        row.addWidget(self.stop_btn)
        row.addWidget(self.run_btn)
        return row

    def build_left(self):
        self.files = FileList()
        self.files.files_dropped.connect(self.add_files)
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
        self.preview = QLabel("완료된 파일을 고르면\n미리보기가 나옵니다")
        self.preview.setObjectName("empty")
        self.preview.setAlignment(Qt.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("preview")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.preview)
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
        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 8, 0, 0)
        pl.addWidget(self.scroll, 1)
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
        self.ai_review.setChecked(s.value("ai_review", True, bool))
        add("검수", self.ai_review)
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
    def start(self):
        n = self.files.topLevelItemCount()
        if not n:
            QMessageBox.information(self, APP_NAME, "PSD 파일을 먼저 추가하세요.")
            return
        if self.other_dir.isChecked() and not self.out_dir.text().strip():
            QMessageBox.information(self, APP_NAME, "결과 폴더를 지정하세요.")
            return
        self.save_settings()
        out_dir = self.out_dir.text().strip() if self.other_dir.isChecked() else None
        jobs = []
        for row in range(n):
            item = self.files.topLevelItem(row)
            src = item.data(COL_FILE, Qt.UserRole)
            jobs.append((row, src, output_path(src, out_dir, self.suffix.text())))
            self.set_status(item, "대기", style.MUTED)
            item.setText(COL_INFO, "")
        self.results.clear()
        self.done_count = 0
        self.waiting = 0
        self.total_bar.setRange(0, n * 100)
        self.total_bar.setValue(0)
        self.set_running(True)
        self.log.appendPlainText(f"── {n}개 파일 시작")

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
        ok = len(self.results)
        for row in range(self.files.topLevelItemCount()):
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
            self.preview.setPixmap(QPixmap())
            self.preview.setAlignment(Qt.AlignCenter)
            self.preview.setText("완료된 파일을 고르면\n미리보기가 나옵니다")
            self.preview_info.setText("")
            return
        self._qimage = ImageQt(result.preview.convert("RGB"))  # QImage가 쓰는 동안 버퍼를 살려 둔다
        self.preview_pix = QPixmap.fromImage(self._qimage)
        self.preview.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.preview_info.setText(self.describe(result))
        self.fit_preview()

    def fit_preview(self):
        if self.preview_pix is None:
            return
        w = max(50, self.scroll.viewport().width() - 4)
        self.preview.setPixmap(self.preview_pix.scaledToWidth(w, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit_preview()

    def open_result(self, reveal):
        item = self.files.currentItem()
        result = self.results.get(self.files.indexOfTopLevelItem(item)) if item else None
        if result:
            open_path(result.out_path, reveal=reveal)

    def closeEvent(self, e):
        self.save_settings()
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
