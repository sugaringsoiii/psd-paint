"""앱 테마: 어두운 바탕에 보라 강조색을 쓰는 Linear 풍 스타일."""

BG = "#0F1012"
PANEL = "#16171A"
RAISED = "#1C1D21"
BORDER = "#26282D"
BORDER_HI = "#34363C"
TEXT = "#E3E4E8"
MUTED = "#8A8F98"
FAINT = "#5C6068"
ACCENT = "#5E6AD2"
ACCENT_HI = "#6F7AE0"
SELECT = "#1E2030"
OK = "#4CB782"
FAIL = "#EB5757"
WARN = "#F2C94C"

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

QPushButton {{
    background: {RAISED};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: #23252A; }}
QPushButton:pressed {{ background: #2A2C32; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; background: {PANEL}; }}
QPushButton#primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT_HI};
    color: white;
    font-weight: 600;
    padding: 7px 16px;
}}
QPushButton#primary:hover {{ background: {ACCENT_HI}; }}
QPushButton#primary:disabled {{ background: #2C2F4A; border-color: #2C2F4A; color: #8C90B8; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED}; padding: 4px 8px; }}
QPushButton#ghost:hover {{ color: {TEXT}; background: {RAISED}; }}

QLineEdit, QSpinBox {{
    background: #111214;
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled {{ color: {FAINT}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none; background: transparent; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER_HI};
    background: #111214;
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT_HI}; }}

QTreeWidget {{
    background: transparent;
    border: none;
    alternate-background-color: transparent;
}}
QTreeWidget::item {{ height: 34px; border-bottom: 1px solid {BORDER}; padding-left: 4px; }}
QTreeWidget::item:selected {{ background: {SELECT}; color: {TEXT}; }}
QTreeWidget::item:hover:!selected {{ background: #191A1E; }}
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

QProgressBar {{
    background: {RAISED};
    border: none;
    border-radius: 2px;
    max-height: 4px;
    min-height: 4px;
    text-align: right;
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
QTabBar::tab:selected {{ background: {RAISED}; color: {TEXT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

QSplitter::handle {{ background: transparent; width: 10px; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2C2E34; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3A3D44; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2C2E34; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 6px; border-radius: 6px; }}
QMessageBox {{ background: {PANEL}; }}
"""
