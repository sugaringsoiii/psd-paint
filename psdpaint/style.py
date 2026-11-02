"""앱 테마: 밝은 바탕에 보라 강조색을 쓰는 Linear 풍 스타일."""
from pathlib import Path

ASSETS = (Path(__file__).parent / "assets").as_posix()

BG = "#FBFBFC"
PANEL = "#FFFFFF"
RAISED = "#FBFBFC"
HOVER = "#EDEEF3"
BORDER = "#0F1C1D20"  # #AARRGGBB: 검정 6%
BORDER_HI = "#D5D8DE"
TEXT = "#222222"
MUTED = "#7D7E82"
FAINT = "#AAADB3"
ACCENT = "#696EFF"
ACCENT_HI = "#8898FF"
ACCENT_SOFT = "#ECEEFB"
SELECT = "#F1F3FF"
OK = "#00AB5C"
FAIL = "#E85757"
WARN = "#F59C03"
CONTROL = "#D3D7DE"  # 체크박스·라디오 테두리
ACCENT_OFF = "#C5CEEE"  # 꺼진 기본 버튼
SCROLL = "#E4E6EA"
SEPARATOR = "#EEEFF1"

FONT = "Pretendard"  # assets 의 Regular·Medium·SemiBold 를 앱 시작 때 등록한다
GAP = 8  # 패널 사이 간격
INPUT_H = 32  # 인풋 높이: 여백 6px×2 + 글자 18px + 테두리 1px×2

QSS = f"""
* {{
    color: {TEXT};
    font-family: "{FONT}";
    font-size: 13px;
    outline: none;
}}
QMainWindow, QWidget#root {{ background: {BG}; }}
QWidget#card {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 16px; font-weight: 600; letter-spacing: -0.3px; }}
QLabel#subtitle, QLabel#muted {{ color: {MUTED}; }}
QLabel#heading {{ color: {TEXT}; font-weight: 500; }}
QLabel#section {{ color: {MUTED}; font-size: 12px; font-weight: 600; letter-spacing: 0px; }}
QLabel#summary {{ color: {MUTED}; font-size: 12px; letter-spacing: 0px; }}
QLabel#field {{ color: {TEXT}; font-weight: 500; }}
QLabel#zoom {{ color: {MUTED}; font-weight: 500; }}
QFrame#sep {{ background: {SEPARATOR}; border: none; }}
QLabel#ok {{ color: {OK}; font-size: 12px; }}
QLabel#fail {{ color: {FAIL}; font-size: 12px; }}

QPushButton {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 12px;
    min-height: 18px;  /* 높이 32 = 여백 6×2 + 18 + 테두리 1×2 */
    font-weight: 500;
}}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; background: {RAISED}; font-weight: 400; }}
QPushButton#primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: white;
    font-weight: 600;
    padding: 7px 12px;
    min-height: 16px;  /* 높이 32 */
}}
QPushButton#primary:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#primary:disabled {{ background: {ACCENT_OFF}; border-color: {ACCENT_OFF}; color: white; }}
QPushButton#updatePill {{  /* 헤더의 [vX.Y.Z 업데이트] */
    background: {ACCENT_SOFT};
    border: none;
    color: {ACCENT};
    font-size: 12px;
    font-weight: 500;
    padding: 2px 8px;
    min-height: 18px;
    border-radius: 11px;
}}
QPushButton#updatePill:hover {{ background: {SELECT}; }}
QPushButton#updatePill:disabled {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 4px 8px; min-height: 18px; font-weight: 400; }}  /* 높이 26 */
QPushButton#ghost:hover {{ color: {TEXT}; background: {HOVER}; }}

QLineEdit, QSpinBox, QComboBox {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 8px;
    min-height: 18px;
    selection-background-color: {ACCENT};
    selection-color: white;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QPlainTextEdit#guideEdit {{ background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 6px; padding: 6px 8px; }}
QPlainTextEdit#guideEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {FAINT}; background: {RAISED}; }}
QSpinBox {{ padding: 0; min-height: {INPUT_H - 2}px; max-height: {INPUT_H - 2}px; }}
QSpinBox QLineEdit {{ padding: 0; border: none; background: transparent; min-height: 0; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QComboBox {{ padding-right: 32px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{ image: url({ASSETS}/chevron-down.svg); width: 16px; height: 16px; }}
QComboBox::down-arrow:on {{ top: 1px; }}

QCheckBox, QRadioButton {{ background: transparent; padding: 2px 0; }}
QCheckBox {{ spacing: 6px; }}
QRadioButton {{ spacing: 4px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {CONTROL};
    border-radius: 4px;
    background: {PANEL};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: url({ASSETS}/check.svg);
}}
QCheckBox::indicator:checked:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QCheckBox::indicator:indeterminate {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: url({ASSETS}/dash.svg);
}}
QRadioButton::indicator {{ width: 16px; height: 16px; image: url({ASSETS}/radio-off.svg); }}
QRadioButton::indicator:checked {{ image: url({ASSETS}/radio.svg); }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {FAINT}; }}

QTreeWidget {{
    background: transparent;
    border: none;
    alternate-background-color: transparent;
}}
QTreeWidget::item {{ height: 34px; border-bottom: 1px solid {BORDER}; padding-left: 4px; }}
QTreeWidget::item:selected {{ background: {SELECT}; }}
QTreeWidget::item:hover:!selected {{ background: {RAISED}; }}
QHeaderView {{ background: {PANEL}; border: none; }}
QHeaderView::section {{
    background: {PANEL};
    color: {MUTED};
    border: none;
    border-top: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px 6px 7px;  /* 셀 글자(여백 4 + Qt 글자 여백 3)와 시작점을 맞춘다 */
    font-size: 12px;
    font-weight: 600;
}}

QPlainTextEdit {{
    background: transparent;
    border: none;
    color: {MUTED};
    font-family: "SF Mono", "Menlo", "Consolas", monospace;
    font-size: 11px;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollArea#preview {{ background: {RAISED}; border: 1px solid transparent; }}  /* 테두리·둥근 모서리는 RoundScroll 이 그린다 */
QScrollArea#preview QAbstractScrollArea::corner, QAbstractScrollArea::corner {{ background: transparent; border: none; }}

QProgressBar {{
    background: {HOVER};
    border: none;
    border-radius: 2px;
    max-height: 4px;
    min-height: 4px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QTabWidget::pane {{ border: none; top: 0px; background: transparent; }}
QTabWidget::tab-bar {{ left: 0px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent;
    border: none;
    color: {MUTED};
    padding: 8px 0;  /* 높이 32. 좌우 여백은 gui.py 탭 이름 위젯이 갖는다 */
    margin-right: 0;
    border-radius: 6px;
}}
QTabBar::tab:selected {{ background: {SELECT}; color: {TEXT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {ACCENT_SOFT}; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SCROLL}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER_HI}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {SCROLL}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QPushButton#swatch {{ padding: 0; min-height: 0; }}  /* 색 칸 (gui.ColorButton) */
QPushButton#cellSwatch {{ padding: 0; min-height: 0; border: none; background: transparent; }}  /* 표 안 색 칸 */
QPushButton#cellSwatch:hover {{ background: {HOVER}; }}
QWidget#tableHead {{ border-top: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; }}  /* 파일 목록 표 머리와 같게 */
QWidget#tableRow {{ border-bottom: 1px solid {BORDER}; }}
QWidget#tableRow:hover {{ background: {RAISED}; }}
QLineEdit#cell {{ background: transparent; border-color: transparent; padding: 3px 6px; }}
QLineEdit#cell:hover {{ border-color: {BORDER_HI}; }}
QLineEdit#cell:focus {{ border-color: {ACCENT}; background: {PANEL}; }}
QFrame#dialog {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 12px;
}}
QLabel#dialogTitle {{ font-size: 14px; font-weight: 600; }}
QFrame#picker {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 8px;
}}
QLabel#pickerHead {{ color: {FAINT}; font-size: 12px; padding: 4px 8px 6px 4px; }}  /* 항목 글자와 시작점을 맞춘다 */
QPushButton#pickerItem {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 6px 8px;
    min-height: 18px;  /* 높이 30: 목록 항목과 같게 */
    font-weight: 400;
    text-align: left;
}}
QPushButton#pickerItem:hover {{ background: {SELECT}; }}
QPushButton#pickerItem:disabled {{ color: {FAINT}; background: transparent; }}
QFrame#pickerLine {{ background: {BORDER}; margin: 4px 6px; border: none; }}
QListWidget#pickerList {{ background: transparent; border: none; outline: none; }}
QListWidget#pickerList::item {{ height: 30px; padding-left: 4px; border-radius: 6px; color: {TEXT}; }}
QListWidget#pickerList::item:hover {{ background: {SELECT}; }}
QListWidget#pickerList::item:selected {{ background: {SELECT}; color: {TEXT}; }}
QListWidget#pickerList::item:disabled {{ color: {FAINT}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 4px 8px; }}
QFrame#tip {{ background: {PANEL}; border: 1px solid {BORDER_HI}; border-radius: 8px; }}  /* gui.Tip */
QLabel#tipText {{ color: {TEXT}; }}
QLabel#keycap {{
    color: {MUTED};
    font-size: 11px;
    border: 1px solid {BORDER_HI};
    border-radius: 4px;
    min-width: 12px;
    padding: 1px 4px;
}}
"""
