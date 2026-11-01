"""앱 테마: 밝은 바탕에 보라 강조색을 쓰는 Linear 풍 스타일."""

BG = "#F4F5F7"
PANEL = "#FFFFFF"
RAISED = "#F7F8FA"
HOVER = "#EFF0F3"
BORDER = "#E4E6EA"
BORDER_HI = "#D5D8DE"
TEXT = "#1C1D21"
MUTED = "#6B6F78"
FAINT = "#A3A7AF"
ACCENT = "#5E6AD2"
ACCENT_HI = "#6F7AE0"
ACCENT_SOFT = "#ECEEFB"
SELECT = "#EEF0FB"
OK = "#2F9E6B"
FAIL = "#D64545"
WARN = "#C98A00"

GAP = 16  # 패널 사이 간격

QSS = f"""
* {{
    color: {TEXT};
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
QLabel#title {{ font-size: 16px; font-weight: 600; }}
QLabel#subtitle, QLabel#muted {{ color: {MUTED}; }}
QLabel#section {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }}
QLabel#summary {{ color: {MUTED}; font-size: 12px; }}
QLabel#ok {{ color: {OK}; font-size: 12px; }}
QLabel#fail {{ color: {FAIL}; font-size: 12px; }}

QPushButton {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; background: {RAISED}; }}
QPushButton#primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: white;
    font-weight: 600;
    padding: 7px 16px;
}}
QPushButton#primary:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#primary:disabled {{ background: #C5C9EE; border-color: #C5C9EE; color: white; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 4px 8px; }}
QPushButton#ghost:hover {{ color: {TEXT}; background: {HOVER}; }}

QLineEdit, QSpinBox, QComboBox {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: white;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {FAINT}; background: {RAISED}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; background: transparent; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {PANEL};
    border: 1px solid {BORDER_HI};
    selection-background-color: {SELECT};
    selection-color: {TEXT};
}}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER_HI};
    background: {PANEL};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QTreeWidget {{
    background: transparent;
    border: none;
    alternate-background-color: transparent;
}}
QTreeWidget::item {{ height: 34px; border-bottom: 1px solid {BORDER}; padding-left: 4px; }}
QTreeWidget::item:selected {{ background: {SELECT}; color: {TEXT}; }}
QTreeWidget::item:hover:!selected {{ background: {RAISED}; }}
QHeaderView {{ background: {PANEL}; border: none; }}
QHeaderView::section {{
    background: {PANEL};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px;
    font-size: 11px;
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
QScrollArea#preview {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; }}

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
    padding: 6px 12px;
    margin-right: 4px;
    border-radius: 6px;
}}
QTabBar::tab:selected {{ background: {HOVER}; color: {TEXT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #D3D6DC; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #BFC3CA; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #D3D6DC; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: {TEXT}; color: white; border: none; padding: 6px; border-radius: 6px; }}
"""
