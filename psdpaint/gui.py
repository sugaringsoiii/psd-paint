"""psd-paint 데스크톱 앱: PSD 여러 개를 넣고 순서대로 밑색을 칠한다."""
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QSpinBox, QSplitter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

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
            except Exception as e:  # 한 파일이 실패해도 다음 파일은 계속한다
                traceback.print_exc()
                self.failed_file.emit(row, str(e) or type(e).__name__)
        self.all_done.emit()


class FileList(QTreeWidget):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["파일", "상태", "결과"])
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAcceptDrops(True)
        h = self.header()
        h.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        h.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_INFO, QHeaderView.Stretch)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            paths += sorted(p.glob("*.psd")) if p.is_dir() else [p]
        self.files_dropped.emit([p for p in paths if p.suffix.lower() == ".psd"])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("psd-paint · 자동 밑색")
        self.resize(1100, 760)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.thread = None
        self.worker = None
        self.results = {}  # 행 번호 → Result
        self.done_count = 0

        # 왼쪽: 파일 목록 · 설정 · 실행 · 로그
        self.files = FileList()
        self.files.files_dropped.connect(self.add_files)
        self.files.currentItemChanged.connect(self.show_selected)
        add_btn = QPushButton("파일 추가…")
        add_btn.clicked.connect(self.pick_files)
        self.remove_btn = QPushButton("선택 제거")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn = QPushButton("모두 지우기")
        self.clear_btn.clicked.connect(self.files.clear)
        file_btns = QHBoxLayout()
        for b in (add_btn, self.remove_btn, self.clear_btn):
            file_btns.addWidget(b)
        file_btns.addStretch()
        hint = QLabel("PSD 파일이나 폴더를 여기로 끌어 놓으세요. PSD 안에 '참고' 폴더가 있어야 합니다.")
        hint.setStyleSheet("color: gray;")

        self.settings_box = self.build_settings()

        self.run_btn = QPushButton("▶  밑색 칠하기")
        self.run_btn.setMinimumHeight(36)
        self.run_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("중지")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.request_stop)
        self.total_bar = QProgressBar()
        self.total_bar.setFormat("%v / %m 파일")
        run_row = QHBoxLayout()
        run_row.addWidget(self.run_btn, 2)
        run_row.addWidget(self.stop_btn)
        run_row.addWidget(self.total_bar, 3)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(hint)
        lv.addWidget(self.files, 3)
        lv.addLayout(file_btns)
        lv.addWidget(self.settings_box)
        lv.addLayout(run_row)
        lv.addWidget(self.log, 2)

        # 오른쪽: 미리보기 · 결과 열기
        self.preview = QLabel("완료된 파일을 고르면 미리보기가 나옵니다")
        self.preview.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.preview_pix = None
        self.scroll = scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.preview)
        self.open_btn = QPushButton("파일 열기")
        self.open_btn.clicked.connect(lambda: self.open_result(reveal=False))
        self.reveal_btn = QPushButton("폴더에서 보기")
        self.reveal_btn.clicked.connect(lambda: self.open_result(reveal=True))
        for b in (self.open_btn, self.reveal_btn):
            b.setEnabled(False)
        open_row = QHBoxLayout()
        open_row.addWidget(self.open_btn)
        open_row.addWidget(self.reveal_btn)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(QLabel("미리보기"))
        rv.addWidget(scroll, 1)
        rv.addLayout(open_row)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([680, 420])
        self.setCentralWidget(split)

    # ---- 설정 ----
    def build_settings(self):
        s = self.settings
        box = QGroupBox("설정")
        form = QFormLayout(box)

        self.same_dir = QRadioButton("원본과 같은 폴더")
        self.other_dir = QRadioButton("지정한 폴더:")
        group = QButtonGroup(box)
        group.addButton(self.same_dir)
        group.addButton(self.other_dir)
        self.out_dir = QLineEdit(s.value("out_dir", ""))
        browse = QPushButton("찾기…")
        browse.clicked.connect(self.pick_out_dir)
        (self.other_dir if s.value("use_out_dir", False, bool) else self.same_dir).setChecked(True)
        out_row = QHBoxLayout()
        for w in (self.same_dir, self.other_dir, self.out_dir, browse):
            out_row.addWidget(w)
        form.addRow("결과 위치", out_row)

        d = Options()
        self.suffix = QLineEdit(s.value("suffix", "_밑색"))
        self.line_group = QLineEdit(s.value("line_group", d.line_group))
        self.hint_group = QLineEdit(s.value("hint_group", d.hint_group))
        self.boundary = QLineEdit(s.value("boundary", ", ".join(d.extra_boundary)))
        self.boundary.setToolTip("선화 외에 경계로 쓸 레이어 이름. 쉼표로 구분하고 * 를 쓸 수 있습니다 (예: 컷*, スマートフォン*)")
        self.offset = QSpinBox()
        self.offset.setRange(0, 10)
        self.offset.setSuffix(" px")
        self.offset.setValue(s.value("offset", d.offset_px, int))
        self.gap = QSpinBox()
        self.gap.setRange(0, 10)
        self.gap.setSuffix(" px")
        self.gap.setValue(s.value("gap", d.gap_close_px, int))

        names = QHBoxLayout()
        names.addWidget(QLabel("선화 폴더"))
        names.addWidget(self.line_group)
        names.addWidget(QLabel("참고 폴더"))
        names.addWidget(self.hint_group)
        names.addWidget(QLabel("파일 이름 뒤"))
        names.addWidget(self.suffix)
        form.addRow("이름", names)
        form.addRow("경계 레이어", self.boundary)
        nums = QHBoxLayout()
        nums.addWidget(QLabel("윤곽 안쪽으로"))
        nums.addWidget(self.offset)
        nums.addWidget(QLabel("선 틈 닫기"))
        nums.addWidget(self.gap)
        nums.addStretch()
        form.addRow("다듬기", nums)
        return box

    def save_settings(self):
        s = self.settings
        s.setValue("use_out_dir", self.other_dir.isChecked())
        s.setValue("out_dir", self.out_dir.text())
        s.setValue("suffix", self.suffix.text())
        s.setValue("line_group", self.line_group.text())
        s.setValue("hint_group", self.hint_group.text())
        s.setValue("boundary", self.boundary.text())
        s.setValue("offset", self.offset.value())
        s.setValue("gap", self.gap.value())

    def options(self):
        return Options(
            line_group=self.line_group.text().strip() or "선화",
            hint_group=self.hint_group.text().strip() or "참고",
            extra_boundary=[p.strip() for p in self.boundary.text().split(",") if p.strip()],
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
            item = QTreeWidgetItem([p.name, "대기", ""])
            item.setData(COL_FILE, Qt.UserRole, str(p))
            item.setToolTip(COL_FILE, str(p))
            self.files.addTopLevelItem(item)

    def remove_selected(self):
        for item in self.files.selectedItems():
            self.files.takeTopLevelItem(self.files.indexOfTopLevelItem(item))

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
            item.setText(COL_STATUS, "대기")
            item.setText(COL_INFO, "")
        self.results.clear()
        self.done_count = 0
        self.total_bar.setRange(0, n)
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
        self.stop_btn.setEnabled(running)
        for w in (self.settings_box, self.remove_btn, self.clear_btn):
            w.setEnabled(not running)
        self.files.setAcceptDrops(not running)

    def on_started(self, row):
        item = self.files.topLevelItem(row)
        self.files.setCurrentItem(item)
        self.log.appendPlainText(f"[{row + 1}] {item.text(COL_FILE)}")

    def on_progress(self, row, msg, frac):
        self.files.topLevelItem(row).setText(COL_STATUS, f"{int(frac * 100)}%  {msg}")

    def on_finished(self, row, result, secs):
        item = self.files.topLevelItem(row)
        self.results[row] = result
        item.setText(COL_STATUS, "✅ 완료")
        note = f"레이어 {len(result.layers)}개"
        item.setText(COL_INFO, note)
        item.setToolTip(COL_INFO, f"{', '.join(result.layers)}\n힌트 없이 비워 둔 큰 영역(배경 포함) {result.unhinted_large}개")
        self.log.appendPlainText(f"    → {result.out_path} ({secs:.1f}초) {note}")
        self.step()
        if self.files.currentItem() is item:
            self.show_selected(item)

    def on_failed(self, row, err):
        item = self.files.topLevelItem(row)
        item.setText(COL_STATUS, "❌ 실패")
        item.setText(COL_INFO, err)
        item.setToolTip(COL_INFO, err)
        self.log.appendPlainText(f"    실패: {err}")
        self.step()

    def step(self):
        self.done_count += 1
        self.total_bar.setValue(self.done_count)

    def on_all_done(self):
        ok = len(self.results)
        for row in range(self.files.topLevelItemCount()):
            item = self.files.topLevelItem(row)
            if item.text(COL_STATUS) == "대기":
                item.setText(COL_STATUS, "중지됨")
        self.log.appendPlainText(f"── 끝: 완료 {ok}개, 실패 {self.done_count - ok}개")
        self.set_running(False)
        self.worker = None

    # ---- 미리보기 · 열기 ----
    def show_selected(self, item, _prev=None):
        result = self.results.get(self.files.indexOfTopLevelItem(item)) if item else None
        for b in (self.open_btn, self.reveal_btn):
            b.setEnabled(result is not None)
        if result is None or result.preview is None:
            self.preview_pix = None
            self.preview.setPixmap(QPixmap())
            self.preview.setText("완료된 파일을 고르면 미리보기가 나옵니다")
            return
        self._qimage = ImageQt(result.preview.convert("RGB"))  # QImage가 쓰는 동안 버퍼를 살려 둔다
        self.preview_pix = QPixmap.fromImage(self._qimage)
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
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
