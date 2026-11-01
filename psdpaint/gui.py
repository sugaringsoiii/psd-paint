"""psd-paint 데스크톱 앱: PSD 여러 개를 넣고 순서대로 밑색을 칠한다."""
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QRectF, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QSpinBox, QSplitter, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import style
from .cli import output_path
from .core import Options, PaintError, paint

APP_NAME = "psd-paint"
COL_FILE, COL_STATUS, COL_INFO = range(3)


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
        p.setBrush(QColor(30, 32, 48, 120) if self.dragging else Qt.NoBrush)
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
        outer.setContentsMargins(18, 16, 18, 18)
        outer.setSpacing(14)
        outer.addLayout(self.build_header())

        split = QSplitter()
        split.setChildrenCollapsible(False)
        split.addWidget(self.build_left())
        split.addWidget(self.build_right())
        split.setSizes([700, 460])
        outer.addWidget(split, 1)
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
        v.setSpacing(12)
        v.addWidget(files_card, 1)
        v.addWidget(log_card)
        return w

    def build_right(self):
        tabs = QTabWidget()

        # 미리보기 탭
        self.preview = QLabel("완료된 파일을 고르면\n미리보기가 나옵니다")
        self.preview.setObjectName("muted")
        self.preview.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.scroll = QScrollArea()
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
        s = self.settings
        d = Options()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(14)

        # 결과 위치
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
        radios = QHBoxLayout()
        radios.addWidget(self.same_dir)
        radios.addWidget(self.other_dir)
        radios.addStretch()
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir)
        out_row.addWidget(browse)
        f1 = QFormLayout()
        f1.addRow("파일 이름 뒤", self.suffix)
        v.addWidget(label("결과", "section"))
        v.addLayout(radios)
        v.addLayout(out_row)
        v.addLayout(f1)

        # 레이어 이름
        self.line_group = QLineEdit(s.value("line_group", d.line_group))
        self.hint_group = QLineEdit(s.value("hint_group", d.hint_group))
        self.boundary = QLineEdit(s.value("boundary", ", ".join(d.extra_boundary)))
        self.boundary.setToolTip("선화 외에 경계로 쓸 레이어 이름. 쉼표로 구분하고 * 를 쓸 수 있습니다 (예: 컷*, スマートフォン*)")
        self.background = QLineEdit(s.value("background", ", ".join(d.background)))
        self.background.setToolTip("배경 레이어·폴더 이름 패턴. 선화 폴더 안에 있어도 경계에서 빼고, 결과에서는 끕니다")
        self.hide_bg = QCheckBox("결과 PSD에서 배경 끄기")
        self.hide_bg.setChecked(s.value("hide_bg", d.hide_background, bool))
        f2 = QFormLayout()
        f2.addRow("선화 폴더", self.line_group)
        f2.addRow("참고 폴더", self.hint_group)
        f2.addRow("경계 레이어", self.boundary)
        f2.addRow("배경 이름", self.background)
        f2.addRow("", self.hide_bg)
        v.addWidget(label("레이어", "section"))
        v.addLayout(f2)

        # 칠하기
        self.auto_fill = QCheckBox("힌트 없는 영역 회색으로 자동 칠하기")
        self.auto_fill.setChecked(s.value("auto_fill", d.auto_fill, bool))
        self.max_layers = QSpinBox()
        self.max_layers.setRange(0, 50)
        self.max_layers.setSpecialValueText("제한 없음")
        self.max_layers.setValue(s.value("max_layers", d.max_layers, int))
        self.max_layers.setToolTip("넘으면 칠한 면적이 작은 레이어부터 '기타' 레이어로 합칩니다")
        self.offset = QSpinBox()
        self.offset.setRange(0, 10)
        self.offset.setSuffix(" px")
        self.offset.setValue(s.value("offset", d.offset_px, int))
        self.gap = QSpinBox()
        self.gap.setRange(0, 10)
        self.gap.setSuffix(" px")
        self.gap.setValue(s.value("gap", d.gap_close_px, int))
        f3 = QFormLayout()
        f3.addRow("", self.auto_fill)
        f3.addRow("레이어 최대", self.max_layers)
        f3.addRow("윤곽 안쪽으로", self.offset)
        f3.addRow("선 틈 닫기", self.gap)
        v.addWidget(label("칠하기", "section"))
        v.addLayout(f3)
        v.addStretch()

        for form in (f1, f2, f3):
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setHorizontalSpacing(14)
            form.setVerticalSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
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

    def options(self):
        def patterns(edit):
            return [p.strip() for p in edit.text().split(",") if p.strip()]
        return Options(
            line_group=self.line_group.text().strip() or "선화",
            hint_group=self.hint_group.text().strip() or "참고",
            extra_boundary=patterns(self.boundary),
            background=patterns(self.background),
            hide_background=self.hide_bg.isChecked(),
            auto_fill=self.auto_fill.isChecked(),
            max_layers=self.max_layers.value(),
            offset_px=self.offset.value(),
            gap_close_px=self.gap.value(),
        )

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

    def step(self):
        self.done_count += 1
        self.total_bar.setValue(self.done_count * 100)

    def on_all_done(self):
        ok = len(self.results)
        for row in range(self.files.topLevelItemCount()):
            item = self.files.topLevelItem(row)
            if item.text(COL_STATUS) == "대기":
                self.set_status(item, "중지됨", style.WARN)
        self.log.appendPlainText(f"── 끝: 완료 {ok}개, 실패 {self.done_count - ok}개")
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
            self.preview.setText("완료된 파일을 고르면\n미리보기가 나옵니다")
            self.preview_info.setText("")
            return
        self._qimage = ImageQt(result.preview.convert("RGB"))  # QImage가 쓰는 동안 버퍼를 살려 둔다
        self.preview_pix = QPixmap.fromImage(self._qimage)
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
    app.setStyle("Fusion")
    app.setStyleSheet(style.QSS)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
