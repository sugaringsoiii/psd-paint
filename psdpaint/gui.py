"""psd-paint 데스크톱 앱: PSD 여러 개를 넣고 순서대로 밑색을 칠한다."""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontDatabase, QFontMetrics, QIcon, QKeySequence, QPainter,
                           QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabBar,
    QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import threading
from dataclasses import replace

from . import __version__, ai, characters, style, updater
from .palette import read_palette
from .widgets import (MOD, ColorPicker, Combo, Dialog, RoundScroll, Tip, button, card, color_field,
                      Popup, clear_layout, hbox0, icon, label, search_box,
                      to_hex)
from .cli import output_path
from .core import (AwaitingAnswer, Options, PaintError, next_options, paint, quick_preview, region_at,
                   repaint, set_region)

APP_NAME = "psd-paint"
COL_FILE, COL_STATUS, COL_INFO = range(3)
REF_ROLE = Qt.UserRole + 1  # 파일별 레퍼런스 경로 (없으면 설정 탭의 공통 참고 방식)
DONE_ROLE = Qt.UserRole + 2  # 이번 목록에서 칠하기를 마친 파일
LABEL_W = 96  # 설정 탭 라벨 고정폭
PRESET_ROW = 26  # 프리셋 표 안의 색 칸·삭제 버튼 크기
CHECK_W = 16  # 프리셋 표 체크박스 칸: 상자 14 + 테두리가 잘리지 않을 여유 2
STATUS_ICON = {"done": style.OK, "fail": style.FAIL, "waiting": style.MUTED, "paused": style.MUTED}  # 상태 아이콘 색


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
                self.failed_file.emit(row, "파일을 찾을 수 없습니다.")
            except PermissionError as e:  # macOS 가 다운로드·데스크탑 같은 보호 폴더 쓰기를 막은 경우
                self.failed_file.emit(row, f"저장할 권한이 없습니다: {e.filename}. 시스템 설정 → 개인정보 보호 및 보안 → "
                                           "파일 및 폴더에서 psd-paint를 허용하거나, 설정에서 결과 폴더를 바꾸세요.")
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
        self.setItemDelegate(StatusDelegate(self))
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
        if self.dragging:
            pen = QPen(QColor(style.ACCENT))
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(style.ACCENT_SOFT))
            p.drawRoundedRect(r, 10, 10)
        if not self.topLevelItemCount():
            p.setPen(QColor(style.MUTED))
            f = QFont(p.font())
            f.setPixelSize(14)
            f.setWeight(QFont.Medium)
            p.setFont(f)
            p.drawText(r.adjusted(0, -22, 0, 0), Qt.AlignCenter, "PSD 파일이나 폴더를 끌어 놓으세요.")
            f.setPixelSize(13)
            f.setWeight(QFont.Normal)
            p.setFont(f)
            p.drawText(r.adjusted(0, 0, 0, 22), Qt.AlignCenter,
                       "'참고' 폴더의 힌트대로 칠하고, 힌트가 없는 영역은 회색으로 자동 칠합니다.")
        p.end()


class StatusDelegate(QStyledItemDelegate):
    """선택된 행에서도 상태·결과 칸의 글자색을 그대로 둔다. 상태 아이콘은 글자와 4px 띄워 직접 그린다."""

    def initStyleOption(self, opt, index):
        super().initStyleOption(opt, index)
        fg = index.data(Qt.ForegroundRole)
        opt.palette.setColor(QPalette.HighlightedText, fg.color() if fg else QColor(style.TEXT))

    def paint(self, painter, opt, index):
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, index)
        if o.icon.isNull():
            return super().paint(painter, opt, index)
        # Qt 는 아이콘과 글자 사이를 9px 가까이 띄워 Figma(4px)와 달라, 배경만 맡기고 아이콘·글자는 직접 그린다
        ic, text = QIcon(o.icon), o.text  # 복사해 둔다 (PySide 는 o.icon 을 참조로 돌려준다)
        o.icon, o.text = QIcon(), ""
        o.features &= ~QStyleOptionViewItem.HasDecoration
        st = o.widget.style()
        st.drawControl(QStyle.CE_ItemViewItem, o, painter, o.widget)
        r = st.subElementRect(QStyle.SE_ItemViewItemText, o, o.widget)
        painter.save()
        x = r.left() + 3  # Qt 가 글자 앞에 두는 여백과 같게
        ic.paint(painter, QRect(x, r.center().y() - 6, 14, 14))
        r.setLeft(x + 14 + 4)
        painter.setFont(o.font)
        painter.setPen(o.palette.color(QPalette.Text))
        painter.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, o.fontMetrics.elidedText(text, Qt.ElideRight, r.width()))
        painter.restore()


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


class PresetsTab(QWidget):
    """색상 프리셋 탭. 자주 쓰는 레이어 이름·색을 저장해 두면 미리보기 영역 메뉴 맨 위에 늘 나온다.

    presets: [{"name", "rgb"}]. 바꿀 때마다 설정(QSettings "presets")에 JSON 으로 저장한다.
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        try:
            self.presets = [{"name": p["name"], "rgb": [int(v) for v in p["rgb"]]}
                            for p in json.loads(self.settings.value("presets", "[]"))
                            if isinstance(p["name"], str)]  # 예전 [추가] 버그로 생긴 name=false 줄은 버린다
        except (ValueError, KeyError, TypeError):  # 망가진 값이면 빈 목록으로 시작
            self.presets = []
        self.selected = set()  # 체크한 프리셋 (dict 의 id)
        self.bulk_btn = button("", "ghost", "delete")
        self.bulk_btn.clicked.connect(self.remove_selected)
        add_btn = button("추가", "ghost", "plus")
        add_btn.clicked.connect(lambda: self.add())  # clicked 의 checked 값이 name 으로 들어가지 않게
        import_btn = button("가져오기", "ghost", "download")
        import_btn.setToolTip("포토샵 색상 견본(.aco·.ase)이나 클립스튜디오 컬러 세트(.cls)의 색을 한 번에 추가합니다.")
        import_btn.clicked.connect(self.import_palette)
        top = QHBoxLayout()
        top.addWidget(label("색상 프리셋", "section"))
        top.addStretch()
        top.addLayout(hbox0(self.bulk_btn, import_btn, add_btn))
        note = label("저장한 색은 미리보기에서 영역을 누르면 맨 위에 나옵니다. "
                     "머리·피부·눈처럼 자주 쓰는 색을 넣어 두세요.", "summary")
        note.setWordWrap(True)
        head = QWidget()  # 표 머리: 색 | 이름 | 값 — 파일 목록 표와 같은 모양, 칸 글자 시작점에 맞춘다
        head.setObjectName("tableHead")
        head.setAttribute(Qt.WA_StyledBackground, True)
        hh = QHBoxLayout(head)
        hh.setContentsMargins(4, 6, 4, 6)
        hh.setSpacing(8)
        self.all_check = QCheckBox()  # 보이는 줄 모두 고르기·풀기
        self.all_check.setFixedWidth(CHECK_W)
        self.all_check.clicked.connect(self.toggle_all)
        hh.addWidget(self.all_check)
        for text, w, indent in (("색", PRESET_ROW, 5), ("이름", 0, 7), ("값", 96, 7), ("", PRESET_ROW, 0)):
            lb = label(text, "section")
            lb.setContentsMargins(indent, 0, 0, 0)
            if w:
                lb.setFixedWidth(w)
            hh.addWidget(lb, 0 if w else 1)
        self.box = QVBoxLayout()
        self.box.setSpacing(0)
        self.empty = label("", "muted")
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 8)
        iv.setSpacing(12)
        iv.addLayout(self.box)
        iv.addStretch()  # 결과가 없으면 안내 글자가 남은 자리 가운데에 온다
        iv.addWidget(self.empty, 0, Qt.AlignCenter)
        iv.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        pv = QVBoxLayout(self)
        pv.setContentsMargins(0, 8, 0, 0)
        pv.setSpacing(12)
        self.search = search_box("이름·색 검색")
        self.search.textChanged.connect(self.filter)
        pv.addLayout(top)
        pv.addWidget(note)
        pv.addWidget(self.search)
        table = QVBoxLayout()
        table.setSpacing(0)
        table.addWidget(head)
        table.addWidget(scroll, 1)
        pv.addLayout(table, 1)
        self.render()

    def render(self):
        clear_layout(self.box)
        for p in self.presets:
            row = QWidget()
            row.setObjectName("tableRow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setAttribute(Qt.WA_Hover, True)
            row.setFixedHeight(34)  # 파일 목록 행과 같은 높이
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 0, 4, 0)
            h.setSpacing(8)
            check = QCheckBox()
            check.setFixedWidth(CHECK_W)
            check.setChecked(id(p) in self.selected)
            check.toggled.connect(lambda on, p=p: self.select(p, on))
            colors, btn, hex_edit = color_field(p["rgb"], PRESET_ROW)
            btn.setObjectName("cellSwatch")  # 표 안에서는 테두리 없이 색만
            name = QLineEdit(p["name"])
            name.setPlaceholderText("이름 (예: 주인공 머리)")
            for e in (name, hex_edit):
                e.setObjectName("cell")  # 평소엔 글자만, 올리거나 누르면 입력칸 테두리
            delete = button("", "ghost", "delete")
            delete.setFixedSize(PRESET_ROW, PRESET_ROW)
            delete.setIcon(icon("delete", style.MUTED, gap=0))  # 글자 없는 버튼이라 아이콘만 가운데
            delete.setStyleSheet("padding: 0;")
            delete.setToolTip("삭제")
            h.addWidget(check)
            h.addWidget(btn)
            h.addWidget(name, 1)
            h.addWidget(hex_edit)
            h.addWidget(delete)
            btn.changed.connect(lambda c, p=p: (p.__setitem__("rgb", list(c)), self.save()))
            name.editingFinished.connect(lambda p=p, e=name: (p.__setitem__("name", e.text().strip()),
                                                              self.save()))
            delete.clicked.connect(lambda _=False, p=p: self.remove(p))
            row.preset, row.check = p, check
            self.box.addWidget(row)
        self.filter()

    def filter(self):
        """검색어가 이름이나 #색에 든 줄만 보인다."""
        q = self.search.text().strip().lower()
        shown = 0
        for i in range(self.box.count()):
            row = self.box.itemAt(i).widget()
            p = row.preset
            hit = not q or q in p["name"].lower() or q in to_hex(p["rgb"]).lower()
            row.setVisible(hit)
            shown += hit
        self.empty.setText("검색 결과가 없어요." if self.presets else
                                  "아직 프리셋이 없어요. [추가]를 눌러 첫 색을 만들어 보세요.")
        self.empty.setVisible(not shown)
        self.update_bulk()

    def visible_rows(self):
        return [w for w in (self.box.itemAt(i).widget() for i in range(self.box.count())) if not w.isHidden()]

    def select(self, p, on):
        (self.selected.add if on else self.selected.discard)(id(p))
        self.update_bulk()

    def toggle_all(self):
        """보이는 줄이 다 골라져 있으면 모두 풀고, 아니면 모두 고른다."""
        rows = self.visible_rows()
        on = not all(w.check.isChecked() for w in rows)
        for w in rows:
            w.check.setChecked(on)

    def update_bulk(self):
        """[선택 삭제 n개] 버튼과 머리 체크박스(전부·일부·없음)를 고른 상태에 맞춘다."""
        n = len(self.selected)
        self.bulk_btn.setText(f"선택 삭제 {n}개")
        self.bulk_btn.setVisible(n > 0)
        rows = self.visible_rows()
        picked = sum(w.check.isChecked() for w in rows)
        self.all_check.setCheckState(Qt.Checked if rows and picked == len(rows) else
                                     Qt.PartiallyChecked if picked else Qt.Unchecked)

    def remove_selected(self):
        n = len(self.selected)
        if not n or not Dialog(self, f"프리셋 {n}개를 삭제할까요?", "삭제한 프리셋은 되돌릴 수 없어요.").exec():
            return
        self.presets = [p for p in self.presets if id(p) not in self.selected]
        self.selected.clear()
        self.save()
        self.render()

    def save(self):
        self.settings.setValue("presets", json.dumps(self.presets, ensure_ascii=False))

    def add(self, name="", rgb=(200, 200, 200)):
        if not name:
            self.search.clear()  # 검색 중이면 새 빈 줄이 걸러져 안 보인다
        self.presets.append({"name": name, "rgb": list(rgb)})
        self.save()
        self.render()
        if not name:  # 새 줄의 이름 칸으로 바로 입력
            edit = self.box.itemAt(self.box.count() - 1).widget().findChild(QLineEdit)
            QTimer.singleShot(0, edit.setFocus)

    def import_palette(self):
        path, _ = QFileDialog.getOpenFileName(self, "색상 견본 가져오기", self.settings.value("last_dir", ""),
                                              "색상 견본 (*.aco *.ase *.cls)")
        if not path:
            return
        try:
            colors = read_palette(path)
        except (ValueError, OSError) as e:
            Dialog(self, "가져오지 못했습니다.", str(e) or "파일을 읽을 수 없습니다.", cancel=False).exec()
            return
        new = [{"name": n, "rgb": list(c)} for n, c in colors]
        new = [p for i, p in enumerate(new) if p not in self.presets and p not in new[:i]]  # 같은 이름·색은 한 번만
        self.presets += new
        self.save()
        self.render()
        skipped = len(colors) - len(new)
        Dialog(self, f"색 {len(new)}개를 가져왔어요.", f"이미 있는 {skipped}개는 건너뛰었어요." if skipped else "",
               cancel=False).exec()

    def remove(self, p):
        self.presets.remove(p)
        self.selected.discard(id(p))
        self.save()
        self.render()


class CharactersTab(QWidget):
    """캐릭터 프로필 탭: 이름·대표 그림·생김새. AI가 누구인지 알아볼 때 함께 본다.

    색은 색상 프리셋의 '이름/부위'(예: 하늘/머리)에서 가져온다. 프로필은 앱 데이터 폴더에 저장한다 (MCP도 읽는다).
    """

    THUMB = 72

    def __init__(self, settings):
        super().__init__()
        self.profiles = characters.load_profiles()
        if not self.profiles:  # 예전 설정의 '캐릭터 생김새' 글을 프로필로 옮긴다
            looks = characters.parse_looks(settings.value("character_looks", ""))
            self.profiles = [{"name": n, "look": t, "image": ""} for n, t in looks.items()]
            if self.profiles:
                characters.save_profiles(self.profiles)
        add_btn = button("추가", "ghost", "plus")
        add_btn.clicked.connect(lambda: self.add())
        top = QHBoxLayout()
        top.addWidget(label("캐릭터", "section"))
        top.addStretch()
        top.addWidget(add_btn)
        note = label("참고 방식을 '캐릭터 프리셋 · AI'로 고르면, AI가 대표 그림과 생김새를 보고 누구인지 알아본 뒤 "
                     "색상 프리셋의 '이름/부위' 색(예: 하늘/머리, 하늘/재킷)으로 칠합니다. "
                     "생김새에는 다른 캐릭터와 구분되는 점(머리 모양·머리띠·귀걸이·안경)을 적으세요.", "summary")
        note.setWordWrap(True)
        self.box = QVBoxLayout()
        self.box.setSpacing(8)
        self.empty = label("등록한 캐릭터가 없어요. [추가]를 눌러 이름·대표 그림·생김새를 넣으세요.", "muted")
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 8)
        iv.addLayout(self.box)
        iv.addStretch()
        iv.addWidget(self.empty, 0, Qt.AlignCenter)
        iv.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        pv = QVBoxLayout(self)
        pv.setContentsMargins(0, 8, 0, 0)
        pv.setSpacing(12)
        pv.addLayout(top)
        pv.addWidget(note)
        pv.addWidget(scroll, 1)
        self.render()

    def render(self):
        clear_layout(self.box)
        for p in self.profiles:
            self.box.addWidget(self.row(p))
        self.empty.setVisible(not self.profiles)

    def row(self, p):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        thumb = QPushButton()
        thumb.setFixedSize(self.THUMB, self.THUMB)
        thumb.setToolTip("대표 그림 고르기 (캐릭터 시트·얼굴이 잘 보이는 그림)")
        if p.get("image"):
            pm = QPixmap(p["image"]).scaled(self.THUMB - 6, self.THUMB - 6, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            thumb.setIcon(QIcon(pm))
            thumb.setIconSize(pm.size())
        else:
            thumb.setText("그림")
        thumb.clicked.connect(lambda _=False, p=p: self.pick_image(p))
        name = QLineEdit(p["name"])
        name.setPlaceholderText("이름")
        name.setFixedWidth(120)
        name.editingFinished.connect(lambda p=p, e=name: self.set_field(p, "name", e.text().strip()))
        look = QLineEdit(p.get("look", ""))
        look.setPlaceholderText("생김새: 밝은 갈색 곱슬머리, 고양이 귀 머리띠, 귀 피어싱")
        look.editingFinished.connect(lambda p=p, e=look: self.set_field(p, "look", e.text().strip()))
        delete = button("", "ghost", "delete")
        delete.setToolTip("이 캐릭터 지우기")
        delete.clicked.connect(lambda _=False, p=p: self.remove(p))
        h.addWidget(thumb)
        h.addWidget(name, 0, Qt.AlignVCenter)
        h.addWidget(look, 1, Qt.AlignVCenter)
        h.addWidget(delete, 0, Qt.AlignVCenter)
        return w

    def save(self):
        self.profiles = [p for p in self.profiles if p["name"]]
        characters.save_profiles(self.profiles)
        self.profiles = characters.load_profiles()  # 그림 경로가 앱 데이터 폴더로 바뀐다

    def add(self):
        n = len(self.profiles) + 1
        while any(p["name"] == f"캐릭터{n}" for p in self.profiles):
            n += 1
        self.profiles.append({"name": f"캐릭터{n}", "look": "", "image": ""})
        self.save()
        self.render()

    def set_field(self, p, key, value):
        if key == "name" and (not value or any(q is not p and q["name"] == value for q in self.profiles)):
            self.render()  # 빈 이름·같은 이름은 되돌린다
            return
        if p.get(key) != value:
            p[key] = value
            self.save()

    def pick_image(self, p):
        path, _ = QFileDialog.getOpenFileName(self, f"{p['name']} 대표 그림", "", "그림 (*.png *.jpg *.jpeg *.webp)")
        if path:
            p["image"] = path
            self.save()
            self.render()

    def remove(self, p):
        self.profiles.remove(p)
        self.save()
        self.render()


class MainWindow(QMainWindow):
    ai_test_done = Signal(bool, str)
    repaint_done = Signal(int, object, str)
    models_loaded = Signal(list, str)
    update_found = Signal(str, str)  # 새 버전, 설치 파일 주소
    update_progress = Signal(float)
    update_done = Signal(str, str)  # 실패면 (받은 파일 또는 "", 이유), 성공이면 ("", "")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"psd-paint {__version__}")
        self.resize(1180, 780)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.tip = Tip()  # eventFilter 가 쓰므로 먼저 만든다
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
        self.update_found.connect(self.on_update_found)
        self.update_progress.connect(self.on_update_progress)
        self.update_done.connect(self.on_update_done)
        if updater.installed():  # 소스로 실행할 때는 확인하지 않는다
            QTimer.singleShot(1500, self.check_update)

        # 미리보기 이동: 키보드(+ - 0), 트랙패드 핀치, 스페이스를 누른 채 드래그
        for keys, act in ((("+", "="), lambda: self.set_zoom(self.zoom * 1.25)),
                          (("-",), lambda: self.set_zoom(self.zoom / 1.25)),
                          (("0",), lambda: self.set_zoom(1.0))):
            for k in keys:
                QShortcut(QKeySequence(k), self, act)
        # 미리보기 탭에서 클릭 수정 되돌리기·되살리기 (macOS 에서는 Ctrl 이 ⌘)
        for k, act in (("Ctrl+Z", self.undo_edit), ("Ctrl+Y", self.redo_edit)):
            QShortcut(QKeySequence(k), self, lambda act=act: self.tabs.currentIndex() == 0 and act())
        self.space_down = False
        self.drag_from = None  # (마우스 전역 위치, 가로 스크롤, 세로 스크롤)
        self.preview.installEventFilter(self)
        QApplication.instance().installEventFilter(self)

    # ---- 화면 구성 ----
    def build_header(self):
        title = label("PSD PAINT", "title")
        sub = label(f"선화 PSD 자동 밑색 · v{__version__}", "subtitle")
        names = QHBoxLayout()
        names.setSpacing(8)
        names.addWidget(title)
        names.addWidget(sub)
        # 새 버전이 있으면 부제 옆에 [vX.Y.Z 업데이트] 를 띄운다
        self.update_btn = button("", icon_name="download")
        self.update_btn.setObjectName("updatePill")
        self.update_btn.setIcon(icon("download", style.ACCENT, style.ACCENT, gap=2))  # 받는 중에도 보라
        self.update_btn.hide()
        self.update_btn.clicked.connect(self.run_update)
        names.addWidget(self.update_btn)

        self.stop_btn = button("중지", icon_name="pause")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.request_stop)
        self.run_btn = button("밑색 칠하기", "primary", "play")
        self.run_btn.setToolTip(f"아직 칠하지 않은 PSD를 차례로 칠하기\t{MOD} ↩")
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

        add_btn = button("파일 추가", icon_name="plus")
        add_btn.clicked.connect(self.pick_files)
        self.remove_btn = button("선택 제거", "ghost", "minus")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn = button("모두 지우기", "ghost", "delete")
        self.clear_btn.clicked.connect(self.clear_files)
        self.summary = label("", "summary")
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(label("파일", "heading"))
        bar.addWidget(self.summary)
        bar.addStretch()
        bar.addLayout(hbox0(self.remove_btn, self.clear_btn))  # 고스트 버튼끼리는 붙인다
        bar.addWidget(add_btn)

        self.total_bar = QProgressBar()
        self.total_bar.setTextVisible(False)
        self.total_bar.setValue(0)
        files_card, _ = card(bar, self.files, self.total_bar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_card, _ = card(label("로그", "heading"), self.log)
        log_card.setMaximumHeight(190)

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(style.GAP)
        v.addWidget(files_card, 1)
        v.addWidget(log_card)
        return w

    def build_right(self):
        tabs = self.tabs = QTabWidget()

        # 미리보기 탭
        self.preview = PreviewCanvas("완료된 파일을 선택하면\n미리보기가 표시됩니다.")
        self.preview.clicked.connect(self.on_preview_click)
        self.sharpen = QTimer(self)  # 핀치가 멈추면 부드러운 보간으로 다시 그린다
        self.sharpen.setSingleShot(True)
        self.sharpen.setInterval(150)
        self.sharpen.timeout.connect(self.end_fast_zoom)
        self.scroll = RoundScroll()
        self.scroll.setObjectName("preview")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setWidget(self.preview)
        self.scroll.viewport().installEventFilter(self)  # ⌘(Ctrl)+스크롤 확대·축소

        # 확대·축소: 배율은 '폭 맞춤'에 대한 비율이다
        self.zoom = 1.0
        self.zoom_out_btn = button("−", "ghost")
        self.zoom_in_btn = button("+", "ghost")
        self.zoom_fit_btn = button("맞춤", "ghost", "fit")
        self.zoom_label = label("100%", "zoom")
        self.zoom_label.setFixedWidth(40)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_out_btn.clicked.connect(lambda: self.set_zoom(self.zoom / 1.25))  # 가운데 기준
        self.zoom_in_btn.clicked.connect(lambda: self.set_zoom(self.zoom * 1.25))
        self.zoom_fit_btn.clicked.connect(lambda: self.set_zoom(1.0))
        for b, tip in ((self.zoom_out_btn, "축소\t−"), (self.zoom_in_btn, "확대\t+"), (self.zoom_fit_btn, "폭에 맞추기\t0")):
            b.setToolTip(tip)
        # 클릭 수정: 미리보기에서 영역을 눌러 레이어·색을 바꾸고 다시 저장한다
        self.undo_btn = button("되돌리기", "ghost", "undo")
        self.undo_btn.clicked.connect(self.undo_edit)
        self.redo_btn = button("되살리기", "ghost", "redo")
        self.redo_btn.clicked.connect(self.redo_edit)
        self.undo_btn.setToolTip(f"되돌리기\t{MOD} Z")
        self.redo_btn.setToolTip(f"되살리기\t{MOD} Y")
        self.save_edit_btn = button("수정 저장", "primary", "download")
        self.save_edit_btn.clicked.connect(self.save_edits)
        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.setSpacing(0)
        for wdg in (self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.zoom_fit_btn,
                    self.undo_btn, self.redo_btn):
            zoom_row.addWidget(wdg)
        zoom_row.addStretch()
        zoom_row.addWidget(self.save_edit_btn)
        self.edit_info = label("영역을 누르면 레이어·색을 바꿀 수 있어요.", "summary")
        self.edits = {}  # 행 번호 → [(영역, 바꾸기 전 키)]
        self.redos = {}  # 행 번호 → [(영역, 되돌리기 전 키)]
        self.repaint_done.connect(self.on_repaint_done)

        self.open_btn = button("파일 열기", icon_name="file")
        self.open_btn.clicked.connect(lambda: self.open_result(reveal=False))
        self.reveal_btn = button("폴더에서 보기", icon_name="folder")
        self.reveal_btn.clicked.connect(lambda: self.open_result(reveal=True))
        for b in (self.open_btn, self.reveal_btn):
            b.setEnabled(False)
        self.preview_info = label("", "summary")
        self.preview_info.setWordWrap(True)
        open_row = QHBoxLayout()
        open_row.setSpacing(6)
        open_row.addWidget(self.open_btn)
        open_row.addWidget(self.reveal_btn)

        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 10, 0, 0)
        pl.setSpacing(style.GAP)
        pl.addLayout(zoom_row)
        pl.addWidget(self.scroll, 1)
        pl.addWidget(self.edit_info)
        pl.addWidget(self.preview_info)
        pl.addSpacing(12)
        pl.addLayout(open_row)
        tabs.addTab(pv, "")

        tabs.addTab(self.build_settings(), "")
        self.presets = PresetsTab(self.settings)
        tabs.addTab(self.presets, "")
        self.characters_tab = CharactersTab(self.settings)
        tabs.addTab(self.characters_tab, "")
        # 탭 이름은 직접 그린다. Qt 탭은 아이콘과 글자 사이가 11px 가까이 벌어져 Figma(4px)와 다르다
        tab_parts = []
        for i, (name, text) in enumerate((("preview", "미리보기"), ("settings", "설정"), ("palette", "색상 프리셋"),
                                               ("user", "캐릭터"))):
            w = QWidget()
            w.setAttribute(Qt.WA_TransparentForMouseEvents)  # 누르면 탭이 받는다
            h = QHBoxLayout(w)
            h.setContentsMargins(12, 0, 8, 0)  # Qt 가 탭 오른쪽에 4px 를 더 둬 좌우 12 가 된다
            h.setSpacing(4)
            ic, tx = QLabel(), QLabel(text)
            ic.setFixedSize(16, 16)
            bold = QFont(tx.font())
            bold.setWeight(QFont.Medium)
            bold.setPixelSize(13)  # 스타일시트가 아직 안 먹은 때라 크기를 직접 정한다
            tx.setFixedWidth(QFontMetrics(bold).horizontalAdvance(text) + 1)  # 굵기가 바뀌어도 폭 고정
            h.addWidget(ic)
            h.addWidget(tx)
            tabs.tabBar().setTabButton(i, QTabBar.LeftSide, w)
            tab_parts.append((name, ic, tx))

        def paint_tabs(cur):  # 고른 탭만 진한 글자색·Medium
            for i, (name, ic, tx) in enumerate(tab_parts):
                on = i == cur
                ic.setPixmap(icon(name, style.TEXT if on else style.MUTED).pixmap(19, 16))
                tx.setStyleSheet(f"color: {style.TEXT if on else style.MUTED}; font-weight: {500 if on else 400};")
        tabs.currentChanged.connect(paint_tabs)
        paint_tabs(0)
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
        grid.setVerticalSpacing(12)
        grid.setColumnMinimumWidth(0, LABEL_W)
        grid.setColumnStretch(1, 1)
        row = 0

        def section(text):
            nonlocal row
            lb = label(text, "section")
            if row:  # 섹션 사이 구분선 (위아래 여백 8 + 줄 간격 12)
                line = QFrame()
                line.setObjectName("sep")
                line.setFixedHeight(1)
                wrap = QVBoxLayout()
                wrap.setContentsMargins(0, 8, 0, 8)
                wrap.addWidget(line)
                grid.addLayout(wrap, row, 0, 1, 2)
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
            if isinstance(item, QCheckBox):  # 한 줄 체크박스도 인풋 높이에 맞춰 줄 간격을 고르게
                item.setMinimumHeight(style.INPUT_H)
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
            sp.lineEdit().setTextMargins(8, 0, 8, 0)
            sp.setFixedHeight(style.INPUT_H)
            return sp

        # 참고 방식
        self.src_layer = QRadioButton("파일 안 참고 레이어")
        self.src_layer.setToolTip("PSD 안의 '참고' 폴더(점·낙서 힌트) 또는 칠해 둔 '참고' 레이어를 봅니다.\n"
                                  "없으면 PSD 옆의 '파일이름_참고.png'를 찾고, 그것도 없으면 자동으로 칠합니다.")
        self.src_image = QRadioButton("레퍼런스 이미지 (PNG·JPG) · AI")
        self.src_psd = QRadioButton("레퍼런스 PSD (캐릭터 시트·채색 참고) · AI")
        self.src_psd.setToolTip("밑색이 칠해진 PSD. 그 안의 '밑색'(또는 '참고') 폴더 레이어 이름과 색을 그대로 씁니다.")
        self.src_chars = QRadioButton("캐릭터 프리셋 · AI")
        self.src_chars.setToolTip("이미지 모델이 부위·인물을 나누고, AI가 누구인지 알아본 뒤 색상 프리셋의\n"
                                  "'캐릭터/부위' 색(예: 하늘/머리, 하늘/재킷)으로 칠합니다. Gemini API 키가 필요합니다.")
        src_group = QButtonGroup(w)
        for b in (self.src_layer, self.src_image, self.src_psd, self.src_chars):
            src_group.addButton(b)
        {"image": self.src_image, "psd": self.src_psd, "characters": self.src_chars}.get(
            s.value("hint_source", "layer"), self.src_layer).setChecked(True)
        self.hint_path = QLineEdit(s.value("hint_path", ""))
        self.hint_path.setPlaceholderText("레퍼런스 파일")
        self.hint_browse = QPushButton("찾기")
        self.hint_browse.clicked.connect(self.pick_hint_path)
        ref_note = label("다른 페이지·캐릭터 시트는 AI가 인물과 부위를 맞춰 칠합니다 (아래 AI 연결). "
                         "같은 페이지를 칠한 레퍼런스면 AI 없이 바로 칠합니다.", "summary")
        ref_note.setWordWrap(True)
        self.parts_method = Combo()
        for text, key in (("영역 확인 칸 — Claude 등 글 AI만으로", "tiles"),
                          ("Gemini 이미지 지도 — 더 정확, Gemini 키 필요", "image")):
            self.parts_method.addItem(text, key)
        self.parts_method.setCurrentIndex(max(0, self.parts_method.findData(s.value("parts_method", "tiles"))))
        self.parts_method.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.parts_method.setMinimumContentsLength(8)
        self.looks_note = label("캐릭터는 캐릭터 탭에, 색은 색상 프리셋 탭에 '이름/부위'"
                                "(머리·피부·재킷·조끼·셔츠·넥타이·바지 …)로 넣습니다.", "summary")
        self.looks_note.setWordWrap(True)
        section("참고 방식")
        add("방식", vbox(self.src_layer, self.src_image, self.src_psd, self.src_chars), top=True)
        add("레퍼런스", hbox(self.hint_path, self.hint_browse))
        add("", ref_note)
        add("부위 나누기", self.parts_method)
        parts_label = grid.itemAtPosition(row - 1, 0).widget()
        self.tile_min = spin(100, 20000, s.value("tile_min_area", Options.tile_min_area, int), " px")
        self.tile_min.setSingleStep(100)
        self.tile_min.setToolTip("영역 확인 칸에 넣을 최소 넓이입니다. 더 작은 영역은 묻지 않고 둘레 색을 따릅니다.\n"
                                 "올리면 칸·토큰이 줄고, 내리면 머리카락 틈 같은 작은 곳이 더 정확해집니다.")
        add("칸 최소 넓이", self.tile_min)
        tile_label = grid.itemAtPosition(row - 1, 0).widget()
        self.tile_auto_bg = QCheckBox("컷 배경은 묻지 않고 비우기 (칸·토큰 절약)")
        self.tile_auto_bg.setChecked(s.value("tile_auto_bg", True, bool))
        add("", self.tile_auto_bg)
        add("", self.looks_note)
        self.looks_row = (self.parts_method, parts_label, self.tile_min, tile_label, self.tile_auto_bg, self.looks_note)
        self.parts_method.currentIndexChanged.connect(self.update_image_model)
        for b in (self.src_layer, self.src_image, self.src_psd, self.src_chars):
            b.toggled.connect(self.update_ref_enabled)
        self.update_ref_enabled()

        # 결과
        self.same_dir = QRadioButton("원본과 같은 폴더")
        self.other_dir = QRadioButton("지정한 폴더")
        self.overwrite = QRadioButton("원본에 덮어쓰기")
        group = QButtonGroup(w)
        for b in (self.same_dir, self.other_dir, self.overwrite):
            group.addButton(b)
        mode = s.value("save_mode", "other" if s.value("use_out_dir", False, bool) else "same")
        {"same": self.same_dir, "other": self.other_dir, "overwrite": self.overwrite}.get(
            mode, self.same_dir).setChecked(True)
        self.out_dir = QLineEdit(s.value("out_dir", ""))
        self.out_dir.setPlaceholderText("결과 폴더")
        self.out_browse = QPushButton("찾기")
        self.out_browse.clicked.connect(self.pick_out_dir)
        self.suffix = QLineEdit(s.value("suffix", "_밑색"))
        self.overwrite_note = label("원본 PSD에 밑색 폴더를 넣어 저장합니다. 다시 칠하면 밑색 폴더만 새것으로 바뀝니다. "
                                    "처음 덮어쓸 때 원본을 '이름_백업.psd'로 남깁니다.", "summary")
        self.overwrite_note.setWordWrap(True)
        section("결과")
        add("저장 위치", vbox(self.same_dir, self.other_dir, self.overwrite), top=True)
        add("결과 폴더", hbox(self.out_dir, self.out_browse))
        add("파일 이름 뒤", self.suffix)
        add("", self.overwrite_note)
        for b in (self.same_dir, self.other_dir, self.overwrite):
            b.toggled.connect(self.update_out_enabled)
        self.update_out_enabled()

        # [파일 열기]에 쓸 앱. 자동이면 클립스튜디오 → 포토샵 → 기본 앱 순서로 설치된 것을 쓴다
        self.apps = find_apps()
        self.open_app = Combo()
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
        self.boundary.setToolTip("선화 외에 경계로 쓸 레이어 이름. 쉼표로 구분하고 * 를 쓸 수 있습니다 (예: 컷*, スマートフォン*).")
        self.background = QLineEdit(s.value("background", ", ".join(d.background)))
        self.background.setToolTip("배경 레이어·폴더 이름 패턴. 선화 폴더 안에 있어도 경계에서 빼고, 결과에서는 끕니다.")
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
        self.provider = Combo()
        for pid, info in ai.PROVIDERS.items():
            self.provider.addItem(info["label"], pid)
        self.provider.setCurrentIndex(max(0, self.provider.findData(s.value("provider", "claude"))))
        self.model = Combo()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.list_btn = QPushButton("목록 불러오기")
        self.list_btn.setToolTip("API 키로 쓸 수 있는 모델 이름을 받아 옵니다.")
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
        self.ai_mode = Combo()
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
        cost_note = label("API로 바로 매칭하면 요청마다 선택한 제공자의 API 사용 요금이 청구될 수 있습니다. "
                          "검수·인물 확대를 켜면 요청이 그만큼 늘어납니다.", "summary")
        cost_note.setWordWrap(True)
        add("", cost_note)
        self.ai_review = QCheckBox("칠한 결과를 한 번 더 보고 고치기 (요청 1회 추가)")
        self.ai_review.setChecked(s.value("ai_review", False, bool))
        add("검수", self.ai_review)
        self.ai_zoom = QCheckBox("인물마다 크게 잘라 부위를 다시 검토 (요청 1회 추가)")
        self.ai_zoom.setChecked(s.value("ai_zoom", True, bool))
        add("인물 확대", self.ai_zoom)
        self.ai_check = QCheckBox("영역을 하나씩 강조해 머리카락·피부·옷을 다시 확인 (요청 1회 추가)")
        self.ai_check.setChecked(s.value("ai_check", True, bool))
        add("영역 확인", self.ai_check)
        self.image_model = Combo()
        for m in characters.IMAGE_MODELS:
            self.image_model.addItem(m, m)
        self.image_model.setCurrentIndex(max(0, self.image_model.findData(s.value("image_model", characters.IMAGE_MODEL))))
        self.image_model.setToolTip("캐릭터 프리셋 방식에서 부위·인물을 칠해 나누는 Gemini 이미지 모델입니다.\n"
                                    "Lite가 가장 싸고 빠릅니다. 제공자와 상관없이 Gemini 키를 씁니다.")
        add("이미지 모델", self.image_model)
        self.update_image_model()
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
        page = QWidget()  # 스크롤해도 내용이 탭에 붙어 보이지 않게 탭 아래 8px 를 비운다
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 8, 0, 0)
        pv.addWidget(scroll)
        return page

    # ---- 설정 ----
    def save_settings(self):
        s = self.settings
        s.setValue("save_mode", "overwrite" if self.overwrite.isChecked() else
                   "other" if self.other_dir.isChecked() else "same")
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
        s.setValue("parts_method", self.parts_method.currentData())
        s.setValue("tile_min_area", self.tile_min.value())
        s.setValue("tile_auto_bg", self.tile_auto_bg.isChecked())
        s.setValue("image_model", self.image_model.currentData())
        pid = self.provider.currentData()
        s.setValue("provider", pid)
        s.setValue("ai_mode", self.ai_mode.currentData())
        s.setValue("ai_review", self.ai_review.isChecked())
        s.setValue("ai_zoom", self.ai_zoom.isChecked())
        s.setValue("ai_check", self.ai_check.isChecked())
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
            ai_check=self.ai_check.isChecked(),
            characters=characters.from_presets(self.presets.presets, profiles=self.characters_tab.profiles),
            ai_parts=self.parts_method.currentData(),
            tile_min_area=self.tile_min.value(),
            tile_auto_bg=self.tile_auto_bg.isChecked(),
            ai_image_model=self.image_model.currentData(),
            # 이미지 지도는 Gemini 이미지 모델이라 제공자와 상관없이 Gemini 키를 쓴다
            ai_image_key=((self.api_key.text().strip() if self.provider.currentData() == "gemini" else "")
                          or ai.get_key("gemini")) if self.parts_method.currentData() == "image" else "",
            extra_boundary=patterns(self.boundary),
            background=patterns(self.background),
            hide_background=self.hide_bg.isChecked(),
            auto_fill=self.auto_fill.isChecked(),
            max_layers=self.max_layers.value(),
            offset_px=self.offset.value(),
            gap_close_px=self.gap.value(),
        )

    def hint_source(self):
        return ("image" if self.src_image.isChecked() else "psd" if self.src_psd.isChecked()
                else "characters" if self.src_chars.isChecked() else "layer")

    def update_ref_enabled(self):
        on = self.src_image.isChecked() or self.src_psd.isChecked()
        self.hint_path.setEnabled(on)
        self.hint_browse.setEnabled(on)
        for widget in self.looks_row:
            widget.setVisible(self.src_chars.isChecked())

    def update_image_model(self):
        tiles = self.parts_method.currentData() == "tiles"
        self.image_model.setEnabled(not tiles)
        self.tile_min.setEnabled(tiles)
        self.tile_auto_bg.setEnabled(tiles)

    def update_out_enabled(self):
        over = self.overwrite.isChecked()
        self.suffix.setEnabled(not over)
        self.overwrite_note.setVisible(over)

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
            self.on_ai_test(False, "API 키를 먼저 입력하세요.")
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
        menu = Popup(self)
        menu.item(f"선택한 파일만 칠하기 ({len(rows)}개)", "run", not running)
        menu.line()
        menu.item("레퍼런스 지정…", "set_ref")
        menu.item("레퍼런스 지정 해제", "clear_ref", any(i.data(COL_FILE, REF_ROLE) for i in items))
        menu.line()
        menu.item("결과 파일 열기", "open", len(rows) == 1 and rows[0] in self.results)
        menu.item("목록에서 제거", "remove", not running)
        chosen = menu.pick(self.files.viewport().mapToGlobal(pos))
        if chosen == "run":
            self.start(rows)
        elif chosen == "set_ref":
            path, _ = QFileDialog.getOpenFileName(self, "이 파일의 레퍼런스", self.settings.value("last_dir", ""),
                                                  "레퍼런스 (*.png *.jpg *.jpeg *.webp *.psd)")
            if path:
                for i in items:
                    self.set_item_ref(i, path)
        elif chosen == "clear_ref":
            for i in items:
                self.set_item_ref(i, None)
        elif chosen == "open":
            open_path(self.results[rows[0]].out_path, app=self.app_for_open())
        elif chosen == "remove":
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
    def set_status(item, text, color=style.MUTED, icon_name=None):
        item.setText(COL_STATUS, text)
        item.setForeground(COL_STATUS, QColor(color))
        item.setIcon(COL_STATUS, icon(f"status-{icon_name}", STATUS_ICON[icon_name], gap=0) if icon_name else QIcon())

    # ---- 실행 ----
    def start(self, rows=None):
        """rows가 있으면 그 행만 (완료한 파일도 다시), 없으면 목록에서 아직 완료하지 않은 파일만 칠한다."""
        n = self.files.topLevelItemCount()
        skipped = 0
        if rows is None:
            rows = [r for r in range(n) if not self.files.topLevelItem(r).data(COL_STATUS, DONE_ROLE)]
            skipped = n - len(rows)
        rows = sorted(rows)
        if not n:
            Dialog(self, "PSD 파일을 먼저 추가하세요.", cancel=False).exec()
            return
        if not rows:
            Dialog(self, "목록의 파일을 모두 칠했어요.",
                   "다시 칠하려면 파일을 우클릭해 '선택한 파일만 칠하기'를 고르세요.", cancel=False).exec()
            return
        if self.other_dir.isChecked() and not self.out_dir.text().strip():
            Dialog(self, "결과 폴더를 지정하세요.", "설정 탭의 결과 폴더에서 고를 수 있습니다.", cancel=False).exec()
            return
        self.save_settings()
        out_dir = self.out_dir.text().strip() if self.other_dir.isChecked() else None
        jobs = []
        for row in rows:
            item = self.files.topLevelItem(row)
            src = item.data(COL_FILE, Qt.UserRole)
            dst = Path(src) if self.overwrite.isChecked() else output_path(src, out_dir, self.suffix.text())
            jobs.append((row, src, dst, item.data(COL_FILE, REF_ROLE)))
            self.set_status(item, "대기", style.MUTED)
            item.setData(COL_STATUS, DONE_ROLE, False)
            item.setText(COL_INFO, "")
            self.results.pop(row, None)
            self.edits.pop(row, None)
            self.redos.pop(row, None)
        self.done_count = 0
        self.waiting = 0
        self.job_rows = set(rows)
        self.total_bar.setRange(0, len(jobs) * 100)
        self.total_bar.setValue(0)
        self.set_running(True)
        self.log.appendPlainText(f"── {len(jobs)}개 파일 시작" + (f" (완료한 {skipped}개는 건너뜀)" if skipped else ""))

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
        self.set_status(self.files.topLevelItem(row), f"{int(frac * 100)}%  {msg}")
        self.total_bar.setValue(self.done_count * 100 + int(frac * 100))

    def on_finished(self, row, result, secs):
        item = self.files.topLevelItem(row)
        self.results[row] = result
        self.set_status(item, "완료", style.TEXT, "done")
        item.setData(COL_STATUS, DONE_ROLE, True)
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
        self.set_status(item, "실패", style.TEXT, "fail")
        item.setText(COL_INFO, err)
        item.setForeground(COL_INFO, QColor(style.FAIL))
        item.setToolTip(COL_INFO, err)
        self.log.appendPlainText(f"    실패: {err}")
        self.step()

    def on_waiting(self, row, msg):
        item = self.files.topLevelItem(row)
        self.set_status(item, "답 대기", style.MUTED, "waiting")
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
                self.set_status(item, "중지됨", style.MUTED, "paused")
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
            self.preview.clear("완료된 파일을 선택하면\n미리보기가 표시됩니다.")
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
        self._anchor_img = None  # 핀치가 끝나면 다음 확대는 새 지점을 잡는다
        self.preview.update()

    def set_zoom(self, zoom, anchor=None):
        """배율을 바꾼다. anchor(뷰포트 좌표)가 있으면 그 아래의 그림 지점이 제자리에 남게 스크롤을 맞춘다."""
        zoom = min(8.0, max(0.25, zoom))  # 폭 맞춤보다 작게도 볼 수 있다 (긴 웹툰 전체 보기)
        if self.preview_pix is None or abs(zoom - self.zoom) < 1e-6:
            self.zoom = zoom
            self.zoom_label.setText(f"{round(self.zoom * 100)}%")
            return
        h, v = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
        vp = self.scroll.viewport()
        ax, ay = (anchor.x(), anchor.y()) if anchor is not None else (vp.width() / 2, vp.height() / 2)
        # 기준점 아래의 그림 지점(그림 크기에 대한 비율). 한 번의 핀치 동안은 처음 잡은 지점을 그대로 써서
        # 단계마다 반올림 오차가 쌓이지 않게 한다
        held = getattr(self, "_anchor_img", None)
        if held and abs(held[0] - ax) < 4 and abs(held[1] - ay) < 4:
            fx, fy = held[2], held[3]
        else:
            r = self.preview.image_rect()
            fx = (h.value() + ax - r.x()) / max(1, r.width())
            fy = (v.value() + ay - r.y()) / max(1, r.height())
            self._anchor_img = (ax, ay, fx, fy)
        self.zoom = zoom
        self.fit_preview()
        # 스크롤 범위를 바로 갱신한다 (나중에 갱신되면 위치가 잘려 화면이 튄다)
        w, hh = self.preview.disp.width(), self.preview.disp.height()
        self.preview.resize(max(vp.width(), w), max(vp.height(), hh))
        r = self.preview.image_rect()
        h.setValue(round(r.x() + fx * r.width() - ax))
        v.setValue(round(r.y() + fy * r.height() - ay))

    def queue_zoom(self, factor, anchor):
        """핀치처럼 잦은 확대 요청을 모아 한 프레임에 한 번만 적용한다."""
        self._zoom_factor = getattr(self, "_zoom_factor", 1.0) * factor
        self._zoom_anchor = anchor
        if not getattr(self, "_zoom_pending", False):
            self._zoom_pending = True
            QTimer.singleShot(0, self.flush_zoom)

    def flush_zoom(self):
        self._zoom_pending = False
        factor, self._zoom_factor = self._zoom_factor, 1.0
        self.set_zoom(self.zoom * factor, self._zoom_anchor)

    def typing(self):
        """글자를 입력하는 칸에 포커스가 있으면 단축키·스페이스 이동을 쓰지 않는다."""
        w = QApplication.focusWidget()
        return isinstance(w, (QLineEdit, QSpinBox, QPlainTextEdit)) or (isinstance(w, QComboBox) and w.isEditable())

    def over_preview(self):
        return self.preview_pix is not None and self.scroll.isVisible() and \
            self.scroll.rect().contains(self.scroll.mapFromGlobal(QCursor.pos()))

    @staticmethod
    def tip_text(obj, pos):
        """obj 의 툴팁 글자. 목록 화면이면 pos 아래 칸의 툴팁"""
        view = obj.parent()
        if isinstance(view, QAbstractItemView) and obj is view.viewport():
            idx, d = view.indexAt(pos), view.itemDelegate()
            return (d.tip(view, idx) if hasattr(d, "tip") else "") or idx.data(Qt.ToolTipRole) or ""
        return obj.toolTip()

    def eventFilter(self, obj, e):
        t = e.type()
        # 툴팁은 Linear 풍 카드(Tip)로 띄운다. 목록 칸의 툴팁은 칸마다 다르다
        if t == QEvent.ToolTip and isinstance(obj, QWidget):
            text = self.tip_text(obj, e.pos())
            if text:
                self.tip.show_for(obj, text, e.globalPos())
                return True
        if obj is self.tip.owner and self.tip.isVisible():
            if t in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel, QEvent.KeyPress, QEvent.Hide):
                self.tip.hide()
            elif t == QEvent.MouseMove and self.tip_text(obj, e.position().toPoint()) != self.tip.full:
                self.tip.hide()  # 목록에서 다른 칸으로 옮겼다
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
                self.sharpen.start()  # 연속 스크롤도 한 동작으로 보고 기준점을 유지한다
                self.queue_zoom(step, pos)
                return True
            # 트랙패드 핀치 확대·축소 (macOS)
            if t == QEvent.NativeGesture and e.gestureType() == Qt.ZoomNativeGesture:
                pos = self.scroll.viewport().mapFromGlobal(e.globalPosition().toPoint())
                self.preview.fast = True  # 핀치 중에는 빠르게, 멈추면 다시 부드럽게
                self.sharpen.start()
                self.queue_zoom(1 + e.value(), pos)
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
        for w in (self.edit_info, self.undo_btn, self.redo_btn, self.save_edit_btn):
            w.setVisible(has_state)
        n = len(self.edits.get(row, []))
        self.edit_info.setText(f"수정 {n}곳 · 저장하면 PSD에 반영돼요." if n else "영역을 누르면 레이어·색을 바꿀 수 있어요.")
        self.undo_btn.setEnabled(n > 0)
        self.redo_btn.setEnabled(bool(self.redos.get(row)))
        self.save_edit_btn.setEnabled(n > 0)
        self.preview.setCursor(Qt.PointingHandCursor if has_state else Qt.ArrowCursor)

    def set_preview_image(self, im):
        self._qimage = ImageQt(im.convert("RGB"))  # QImage가 쓰는 동안 버퍼를 살려 둔다
        self.preview_pix = QPixmap.fromImage(self._qimage)
        self.fit_preview()

    def edit_menu(self, st, region):
        """누른 영역에 쓸 레이어·색 메뉴. → 고른 (레이어, RGB), 비우기면 (None, None), 취소면 False"""
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
        items = [(labels[(name, rgb)], rgb, [name, list(rgb)],
                  (name, rgb) != cur_key) for name, rgb, _ in entries]
        presets = [(p["name"], tuple(p["rgb"]), [p["name"], list(p["rgb"])],
                    (p["name"], tuple(p["rgb"])) != cur_key) for p in self.presets.presets if p["name"]]
        data = ColorPicker(self, cur_label or "칠하지 않음", items, cur >= 0, presets).pick(QCursor.pos())
        if data is None or data is False:
            return False
        if data == "clear":
            return None, None
        if data == "new":
            form = QWidget()
            fv = QVBoxLayout(form)
            fv.setContentsMargins(0, 0, 0, 0)
            fv.setSpacing(8)
            name = QLineEdit()
            name.setPlaceholderText("레이어 이름 (예: 주인공 머리)")
            colors, btn, _ = color_field((180, 180, 180))
            colors.addStretch()
            keep = QCheckBox("색상 프리셋에 저장해 다음에도 쓰기")
            keep.setChecked(True)
            fv.addWidget(name)
            fv.addLayout(colors)
            fv.addWidget(keep)
            dlg = Dialog(self, "새 레이어", "레이어 이름과 색을 정하세요.", form)
            name.setFocus()
            if not dlg.exec() or not name.text().strip():
                return False
            if keep.isChecked() and {"name": name.text().strip(), "rgb": list(btn.rgb)} not in self.presets.presets:
                self.presets.add(name.text().strip(), btn.rgb)
            return name.text().strip(), btn.rgb
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
        # 누른 영역을 빨갛게 표시하고 메뉴를 띄운다
        base = result.preview.convert("RGB")
        mask = Image.fromarray(((st["labels"] == region) * 255).astype("uint8")).resize(base.size, Image.NEAREST)
        self.set_preview_image(Image.composite(Image.new("RGB", base.size, (235, 70, 70)), base,
                                               mask.point(lambda v: 150 if v else 0)))
        picked = self.edit_menu(st, region)
        if picked is False:
            self.set_preview_image(result.preview)
            return
        self.edits.setdefault(row, []).append((region, int(st["region_key"][region])))
        self.redos.pop(row, None)  # 새로 고치면 되살릴 기록은 버린다
        set_region(st, region, *picked)
        result.preview = quick_preview(st)
        self.set_preview_image(result.preview)
        self.update_edit_bar()

    def undo_edit(self):
        row = self.current_row()
        result = self.results.get(row)
        if not result or not self.edits.get(row):
            return
        self.step_edit(row, result, self.edits, self.redos)

    def redo_edit(self):
        row = self.current_row()
        result = self.results.get(row)
        if not result or not self.redos.get(row):
            return
        self.step_edit(row, result, self.redos, self.edits)

    def step_edit(self, row, result, src, dst):
        """src 의 마지막 수정을 되감고 dst 에 기록한다 (되돌리기·되살리기 공용)."""
        region, key = src[row].pop()
        keys = result.state["region_key"]
        dst.setdefault(row, []).append((region, int(keys[region])))
        keys[region] = key
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
        self.redos.pop(row, None)
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

    # ---- 앱 안 업데이트 ----
    def check_update(self):
        def run():
            found = updater.check()
            if found:
                self.update_found.emit(*found)
        threading.Thread(target=run, daemon=True).start()

    def on_update_found(self, version, url):
        self.update_info = (version, url)
        self.update_btn.setText(f"v{version} 업데이트")
        self.update_btn.setToolTip("새 버전을 받아 바꾼 뒤 앱을 다시 켭니다.")
        self.update_btn.show()

    def run_update(self):
        version, url = self.update_info
        if self.worker is not None:
            Dialog(self, "칠하는 중에는 업데이트할 수 없어요.", "끝나거나 중지한 뒤 다시 눌러 주세요.", cancel=False).exec()
            return
        if not Dialog(self, f"v{version}로 업데이트할까요?",
                      "새 버전을 받아 지금 앱과 바꾼 뒤 다시 켭니다. 1~2분 걸릴 수 있어요.").exec():
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("받는 중…")

        def run():
            try:
                updater.update(url, self.update_progress.emit)
                self.update_done.emit("", "")
            except updater.InstallFailed as e:
                self.update_done.emit(str(e.path), str(e))
            except Exception as e:  # 받기 실패 (인터넷 끊김 등)
                self.update_done.emit("", str(e) or type(e).__name__)
        threading.Thread(target=run, daemon=True).start()

    def on_update_progress(self, frac):
        self.update_btn.setText(f"받는 중 {int(frac * 100)}%")

    def on_update_done(self, path, err):
        if not err:  # 새 앱이 곧 다시 켜진다
            self.close()
            QApplication.quit()
            return
        version, _ = self.update_info
        self.log.appendPlainText(f"업데이트 실패: {err}")  # 원인은 로그에만 남긴다
        self.update_btn.setEnabled(True)
        self.update_btn.setText(f"v{version} 업데이트")
        if path:  # 받기는 했다: 손으로 설치하게 연다
            Dialog(self, "자동으로 바꾸지 못했어요.",
                   "받은 설치 파일을 열게요. 지금 앱을 끄고 새 앱을 응용 프로그램 폴더로 옮겨 주세요.",
                   cancel=False).exec()
            open_path(path)
        else:
            Dialog(self, "업데이트를 받지 못했어요.", "인터넷 연결을 확인한 뒤 다시 눌러 주세요.", cancel=False).exec()

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
    for f in sorted((Path(__file__).parent / "assets").glob("Pretendard-*.otf")):
        QFontDatabase.addApplicationFont(str(f))
    font = QFont(style.FONT)  # QPainter 로 그리는 글자도 같은 폰트를 쓴다
    font.setLetterSpacing(QFont.PercentageSpacing, 99)  # 자간 -1%
    app.setFont(font)
    app.setStyleSheet(style.QSS)
    win = MainWindow()
    win.show()
    if os.environ.get("PSDPAINT_SELFTEST"):  # 빌드 확인용: 창을 띄운 뒤 바로 닫는다
        QTimer.singleShot(1500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
