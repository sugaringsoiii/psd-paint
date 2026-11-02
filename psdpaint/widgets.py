"""화면 공용 부품: 버튼·아이콘·카드, 둥근 팝업·드롭다운·다이얼로그·툴팁, 색 칸. 앱 상태는 모른다."""
import re
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QEventLoop,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from . import style

RIGHT_ROLE = Qt.UserRole + 2  # 팝업 목록 줄 오른쪽 글자 (색값)
MOD = "⌘" if sys.platform == "darwin" else "Ctrl"  # 툴팁 키캡에 쓰는 단축키 표시


def label(text, name=None):
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    return lb


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


def hbox0(*widgets):
    """간격 없이 붙인 가로 줄."""
    h = QHBoxLayout()
    h.setSpacing(0)
    for w in widgets:
        h.addWidget(w)
    return h


@lru_cache
def icon(name, color, off=style.FAINT, gap=3):
    """assets/icons 의 아이콘을 color 로 칠한다. 꺼진 버튼에서는 off 색.

    gap: 아이콘 오른쪽에 비워 둘 px. Qt 가 아이콘과 글자 사이에 두는 1px 와 합쳐 Figma 의 4px 가 된다.
    """
    svg = (Path(__file__).parent / "assets" / "icons" / f"{name}.svg").read_text()
    ic = QIcon()
    for mode, c in ((QIcon.Normal, color), (QIcon.Disabled, off)):
        renderer = QSvgRenderer(QByteArray(re.sub(r'fill="#000"', f'fill="{c}"', svg).encode()))
        size = renderer.defaultSize()
        pm = QPixmap((size.width() + gap) * 2, size.height() * 2)  # 레티나에서도 또렷하게 2배로 그린다
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p, QRectF(0, 0, size.width() * 2, size.height() * 2))
        p.end()
        pm.setDevicePixelRatio(2)
        ic.addPixmap(pm, mode)
    return ic


def button(text, kind=None, icon_name=None):
    """kind: None(기본)·"primary"·"ghost". 아이콘 색은 버튼 글자색을 따른다."""
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if icon_name:
        color = {"primary": "white", "ghost": style.MUTED}.get(kind, style.TEXT)
        b.setIcon(icon(icon_name, color, "white" if kind == "primary" else style.FAINT))
        b.setIconSize(QSize(19, 16))  # icon() 참고
    return b


def search_box(placeholder):
    """돋보기 아이콘이 붙은 검색 입력칸."""
    edit = QLineEdit()
    edit.setObjectName("search")
    edit.setPlaceholderText(placeholder)
    edit.addAction(icon("search", style.FAINT, gap=0), QLineEdit.LeadingPosition)
    # 지우기 버튼: Qt 기본(진한 회색) 대신 흐린 아이콘. 글자가 있을 때만 보인다
    clear = edit.addAction(icon("clear", style.FAINT, gap=0), QLineEdit.TrailingPosition)
    clear.setToolTip("지우기")
    clear.setVisible(False)
    clear.triggered.connect(edit.clear)
    edit.textChanged.connect(lambda t: clear.setVisible(bool(t)))
    return edit


def swatch(rgb, size=14):
    pm = QPixmap(size, size)
    pm.fill(QColor(*rgb))
    p = QPainter(pm)
    p.setPen(QColor(style.BORDER_HI))
    p.drawRect(0, 0, size - 1, size - 1)
    p.end()
    return QIcon(pm)


def parse_hex(text):
    """'#RRGGBB'(# 생략 가능) → (r, g, b). 틀리면 None"""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", text.strip())
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None


def to_hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


class RoundScroll(QScrollArea):
    """둥근 테두리 스크롤 영역. 그림과 스크롤바가 모서리를 덮지 않게 위에 모서리·테두리를 다시 그린다."""

    def __init__(self):
        super().__init__()
        self.corners = QWidget(self)
        self.corners.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.corners.paintEvent = self.paint_corners

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.corners.setGeometry(self.rect())
        self.corners.raise_()  # 스크롤바보다 위

    def paint_corners(self, e):
        p = QPainter(self.corners)
        p.setRenderHint(QPainter.Antialiasing)
        box = QPainterPath()
        box.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        outside = QPainterPath()
        outside.addRect(QRectF(self.rect()))
        p.fillPath(outside - box, QColor(style.PANEL))  # 모서리 밖은 카드 바탕색
        p.setPen(QColor(style.BORDER))
        p.drawPath(box)


def shadow_card(window, name, margins, blur, offset, alpha):
    """창을 투명하게 하고 안에 둥근 카드(objectName=name)와 그림자를 깐다. → 카드

    창이 사각이면 모서리가 각져 보이므로 창은 비우고 카드에만 모양을 준다. margins 는 그림자가 들어갈 자리.
    """
    window.setAttribute(Qt.WA_TranslucentBackground, True)
    outer = QVBoxLayout(window)
    outer.setContentsMargins(*margins)
    c = QFrame()
    c.setObjectName(name)
    c.setAttribute(Qt.WA_StyledBackground, True)
    shadow = QGraphicsDropShadowEffect(c)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, offset)
    shadow.setColor(QColor(0, 0, 0, alpha))
    c.setGraphicsEffect(shadow)
    outer.addWidget(c)
    return c


def clear_layout(layout):
    """레이아웃의 위젯을 모두 지운다. 지워지기 전까지 겹쳐 보이거나 크기 계산에 끼지 않게 먼저 숨긴다."""
    while layout.count():
        w = layout.takeAt(0).widget()
        w.hide()
        w.deleteLater()


class Tip(QWidget):
    """Linear 풍 툴팁: 흰 카드에 글자와 단축키 키캡. 툴팁 글자에 탭 뒤로 키를 적는다 (예: "되돌리기\t⌘ Z")."""

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.owner = self.full = None
        row = QHBoxLayout(shadow_card(self, "tip", (8, 4, 8, 10), 16, 3, 30))
        row.setContentsMargins(10, 6, 10, 6)  # 좌우 같게
        row.setSpacing(8)
        self.text = label("", "tipText")
        row.addWidget(self.text)
        self.keys = QHBoxLayout()
        self.keys.setSpacing(3)
        row.addLayout(self.keys)

    def show_for(self, owner, text, pos):
        body, _, keys = text.partition("\t")
        self.text.setText(body)
        clear_layout(self.keys)
        for k in keys.split():
            cap = label(k, "keycap")
            cap.setAlignment(Qt.AlignCenter)
            self.keys.addWidget(cap)
        self.owner, self.full = owner, text
        self.hide()
        self.layout().invalidate()  # 바뀐 글자 크기로 다시 잰다
        self.layout().activate()
        self.resize(self.sizeHint())
        area = (QApplication.screenAt(pos) or QApplication.primaryScreen()).availableGeometry()
        x = min(pos.x() - 8, area.right() - self.width())
        y = pos.y() + 18
        if y + self.height() > area.bottom():  # 아래 자리가 없으면 커서 위로
            y = pos.y() - self.height() - 4
        self.move(max(area.left(), x), y)
        self.show()


class Popup(QWidget):
    """둥근 카드에 그림자를 둔 팝업. 영역 색 고르기·드롭다운·우클릭 메뉴가 모두 이 모양을 쓴다."""

    def __init__(self, parent, width=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.choice = False
        card_ = shadow_card(self, "picker", self.MARGINS, 24, 6, 45)
        self.body = QVBoxLayout(card_)
        self.body.setContentsMargins(4, 4, 4, 4)
        self.body.setSpacing(2)
        if width:
            card_.setFixedWidth(width)

    MARGINS = (10, 6, 10, 14)  # 그림자가 들어갈 자리 (왼·위·오른·아래)

    def item(self, text, data, enabled=True):
        btn = QPushButton(text)
        btn.setObjectName("pickerItem")
        btn.setEnabled(enabled)
        btn.clicked.connect(lambda _=False: self.done(data))
        self.body.addWidget(btn)
        return btn

    def line(self):
        ln = QFrame()
        ln.setObjectName("pickerLine")
        ln.setFixedHeight(1)
        self.body.addWidget(ln)

    def list(self, entries, current=-1, max_h=300, search=False):
        """entries: [(아이콘 또는 None, 글자, 값, 고를 수 있나[, 오른쪽 글자])] 를 한 열 목록으로 넣는다.

        값이 None 이면 구역 머리. 오른쪽 글자(색값)는 오른쪽 끝에 붙고, 이름이 길면 말줄임한다.

        search 면 위에 검색칸을 두고, 글자에 검색어가 든 항목만 보인다. Enter 는 첫 항목을 고른다.
        """
        lst = QListWidget()
        lst.setObjectName("pickerList")
        lst.setIconSize(QSize(14, 14))
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lst.setItemDelegate(TwoColumn(lst))
        for ic, text, data, enabled, *right in entries:
            it = QListWidgetItem(ic, text) if ic else QListWidgetItem(text)
            it.setData(Qt.UserRole, data)
            if right:
                it.setData(RIGHT_ROLE, right[0])
            if not enabled:
                it.setFlags(Qt.NoItemFlags)
            if data is None:  # 구역 머리 (프리셋 / 이 파일)
                f = QFont(lst.font())
                f.setPixelSize(12)
                it.setFont(f)
            lst.addItem(it)
        if current >= 0:
            lst.setCurrentRow(current)
        lst.itemClicked.connect(lambda it: self.done(it.data(Qt.UserRole)))
        if search:
            edit = search_box("이름·색 검색")
            max_h -= 36  # 검색칸만큼 목록을 줄인다

            def filter_(text):
                q = text.strip().lower()
                for i in range(lst.count()):
                    it = lst.item(i)
                    # 검색 중에는 구역 머리를 숨긴다
                    hay = (it.text() + " " + (it.data(RIGHT_ROLE) or "")).lower()
                    it.setHidden(bool(q) and (it.data(Qt.UserRole) is None or q not in hay))

            def first():
                for i in range(lst.count()):
                    it = lst.item(i)
                    if not it.isHidden() and it.flags() & Qt.ItemIsEnabled:
                        return self.done(it.data(Qt.UserRole))
            edit.textChanged.connect(filter_)
            edit.returnPressed.connect(first)
            self.body.addWidget(edit)
            QTimer.singleShot(0, edit.setFocus)  # 뜨자마자 바로 입력
        lst.setFixedHeight(max(30, min(len(entries) * 30 + 6, max_h)))  # 항목 높이 30
        self.body.addWidget(lst)
        return lst

    def done(self, data):
        self.choice = data
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def pick(self, pos):
        """카드 왼쪽 위를 pos(전역 좌표)에 맞춰 띄우고 고를 때까지 기다린다. → 고른 값, 취소면 False"""
        self.adjustSize()
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(pos.x() - self.MARGINS[0], area.right() - self.width())
        y = min(pos.y() - self.MARGINS[1], area.bottom() - self.height())
        self.move(max(area.left(), x), max(area.top(), y))
        loop = QEventLoop()
        self.destroyed.connect(loop.quit)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.show()
        while self.isVisible():
            loop.processEvents(QEventLoop.WaitForMoreEvents)
        return self.choice


class TwoColumn(QStyledItemDelegate):
    """팝업 목록 한 줄: 왼쪽 이름(길면 말줄임), 오른쪽 색값 고정. 이름이 잘리면 툴팁으로 전체 이름을 보인다."""

    def layout(self, opt, index):
        """→ (스타일 옵션, 이름 칸, 오른쪽 칸)"""
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, index)
        r = o.widget.style().subElementRect(QStyle.SE_ItemViewItemText, o, o.widget)
        rw = o.fontMetrics.horizontalAdvance(index.data(RIGHT_ROLE))
        right = QRect(r.right() - 8 - rw, r.top(), rw, r.height())  # 오른쪽 여백 8
        left = r.left() + 3  # Qt 가 글자 앞에 두는 여백과 같게
        return o, QRect(left, r.top(), right.left() - 12 - left, r.height()), right

    def paint(self, painter, opt, index):
        if not index.data(RIGHT_ROLE):
            return super().paint(painter, opt, index)
        o, name_r, right_r = self.layout(opt, index)
        name, o.text = o.text, ""
        o.widget.style().drawControl(QStyle.CE_ItemViewItem, o, painter, o.widget)  # 배경·색 칸
        on = bool(o.state & QStyle.State_Enabled)
        painter.save()
        painter.setFont(o.font)
        painter.setPen(QColor(style.TEXT if on else style.FAINT))
        painter.drawText(name_r, Qt.AlignVCenter | Qt.AlignLeft,
                         o.fontMetrics.elidedText(name, Qt.ElideRight, name_r.width()))
        painter.setPen(QColor(style.MUTED if on else style.FAINT))
        painter.drawText(right_r, Qt.AlignVCenter | Qt.AlignRight, index.data(RIGHT_ROLE))
        painter.restore()

    def tip(self, view, index):
        """이름이 잘린 줄이면 전체 이름, 아니면 빈 글자"""
        if not index.isValid() or not index.data(RIGHT_ROLE):
            return ""
        opt = QStyleOptionViewItem()
        opt.rect, opt.widget, opt.font = view.visualRect(index), view, view.font()
        opt.fontMetrics = view.fontMetrics()
        o, name_r, _ = self.layout(opt, index)
        name = index.data(Qt.DisplayRole)
        return name if o.fontMetrics.horizontalAdvance(name) > name_r.width() else ""


class Combo(QComboBox):
    """목록을 Popup 모양으로 띄우는 콤보박스."""

    def showPopup(self):
        pop = Popup(self, self.width())
        model = self.model()
        pop.list([(self.itemIcon(i) if not self.itemIcon(i).isNull() else None, self.itemText(i), i,
                   bool(model.flags(model.index(i, 0)) & Qt.ItemIsEnabled)) for i in range(self.count())],
                 self.currentIndex(), search=self.count() > 8)  # 긴 목록(모델 등)만 검색칸
        i = pop.pick(self.mapToGlobal(QPoint(0, self.height() + 4)))
        if i is not False:
            self.setCurrentIndex(i)


class ColorPicker(Popup):
    """영역 색 고르기 팝업. 상단 고정(지금 상태·비우기·새 레이어) + 한 열 색 목록(최대 높이 안에서 스크롤)."""
    MAX_H = 520

    def __init__(self, parent, current, entries, can_clear, presets=()):
        """entries·presets: [(이름, RGB, 값, 고를 수 있나)]. 프리셋이 있으면 '프리셋'·'이 파일'로 나눠 보인다."""
        super().__init__(parent, 280)
        self.body.setContentsMargins(4, 6, 4, 4)
        self.body.addWidget(label(f"지금: {current}", "pickerHead"))
        self.item("칠하지 않음 (비우기)", "clear", can_clear)
        self.item("새 레이어…", "new")
        self.line()
        top_h = self.body.sizeHint().height()
        rows = [(swatch(rgb), text, data, enabled, to_hex(rgb)) for text, rgb, data, enabled in presets]
        if presets:
            rows.insert(0, (None, "프리셋", None, False))
            if entries:
                rows.append((None, "이 파일", None, False))
        rows += [(swatch(rgb), text, data, enabled, to_hex(rgb)) for text, rgb, data, enabled in entries]
        self.list(rows, max_h=self.MAX_H - top_h - 10, search=True)


class Dialog(QDialog):
    """Linear 풍 확인 창: 둥근 카드에 제목·설명·[취소][확인]. field 를 주면 입력칸을 함께 보인다."""

    def __init__(self, parent, title, text="", field=None, cancel=True):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        card_ = shadow_card(self, "dialog", (16, 12, 16, 20), 40, 8, 50)
        card_.setFixedWidth(360)
        lay = QVBoxLayout(card_)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(label(title, "dialogTitle"))
        if text:
            body = label(text, "muted")
            body.setWordWrap(True)
            lay.addWidget(body)
        if field is not None:
            lay.addWidget(field)
            field.setFocus()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        if cancel:
            no = button("취소")
            no.setAutoDefault(False)
            no.clicked.connect(self.reject)
            row.addWidget(no)
        ok = button("확인", "primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addSpacing(4)
        lay.addLayout(row)

    def showEvent(self, e):
        super().showEvent(e)
        if self.parent():  # 부모 창 가운데에 띄운다
            win = self.parent().window().frameGeometry()
            self.move(win.center() - self.rect().center())


class ColorButton(QPushButton):
    """누르면 색 고르기 창이 뜨는 색 칸. 바뀌면 changed(RGB)."""
    changed = Signal(tuple)

    def __init__(self, rgb, size=style.INPUT_H):
        super().__init__()
        self.setObjectName("swatch")
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("색 고르기")
        self.rgb = tuple(rgb)
        self.clicked.connect(self.choose)

    def set_rgb(self, rgb):
        self.rgb = tuple(rgb)
        self.update()

    def choose(self):
        c = QColorDialog.getColor(QColor(*self.rgb), self.window(), "색 고르기")
        if c.isValid():
            self.set_rgb((c.red(), c.green(), c.blue()))
            self.changed.emit(self.rgb)

    def paintEvent(self, e):
        super().paintEvent(e)  # 테두리·hover 는 스타일시트
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(style.BORDER))  # 흰색에 가까운 색도 보이게 옅은 테두리
        p.setBrush(QColor(*self.rgb))
        box = QRectF(0, 0, 16, 16)
        box.moveCenter(QRectF(self.rect()).center())
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)


def color_field(rgb, size=style.INPUT_H):
    """[색 칸][#RRGGBB] 한 줄. → (줄 레이아웃, 색 칸, 글자 칸). 둘 중 하나를 바꾸면 다른 쪽도 따라간다."""
    btn = ColorButton(rgb, size)
    hex_edit = QLineEdit(to_hex(rgb))
    hex_edit.setFixedWidth(96)
    btn.changed.connect(lambda c: hex_edit.setText(to_hex(c)))

    def typed():
        c = parse_hex(hex_edit.text())
        if c:
            btn.set_rgb(c)
            btn.changed.emit(c)
        hex_edit.setText(to_hex(btn.rgb))  # 틀린 값은 되돌린다
    hex_edit.editingFinished.connect(typed)
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(btn)
    row.addWidget(hex_edit)
    return row, btn, hex_edit
