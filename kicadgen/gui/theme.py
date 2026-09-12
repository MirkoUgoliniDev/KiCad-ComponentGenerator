"""Tema Catppuccin Mocha condiviso, stessa palette usata in Pm3ToWav (C:\\Lavori\\Python\\Pm3ToWav).

Espone la QSS applicata all'intera applicazione (`APP_QSS`) e quella
specifica per i tab (`TAB_QSS`, identica a `_apply_tab_style()` in
Pm3ToWav/main.py) cosi' le due app hanno lo stesso look coerente anche
se una gira su Windows con tema chiaro di sistema.
"""

from pathlib import Path

_COMBO_ARROW = (Path(__file__).resolve().parent / "assets" / "combo-arrow.svg").as_posix()

# Palette Catppuccin Mocha (stessi valori esadecimali usati in Pm3ToWav)
BASE = "#1e1e2e"       # sfondo finestra
MANTLE = "#181825"     # sfondo piu' scuro (menu, barre)
SURFACE0 = "#313244"   # sfondo widget (input, tab non selezionati)
SURFACE1 = "#45475a"   # bordi
SURFACE2 = "#585b70"   # bordi hover
TEXT = "#cdd6f4"       # testo principale
SUBTEXT = "#a6adc8"    # testo secondario / disabled
BLUE = "#89b4fa"       # accento (selezione, focus, progress)
GREEN = "#a6e3a1"      # successo
RED = "#f38ba8"        # errore
YELLOW = "#f9e2af"     # warning


TAB_QSS = f"""
    QTabWidget::pane {{
        border: 1px solid {SURFACE1};
        border-radius: 6px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: {SURFACE0};
        color: {TEXT};
        border: 1px solid {SURFACE1};
        border-bottom: none;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        padding: 8px 18px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {BASE};
        border-bottom: 3px solid {BLUE};
        color: {TEXT};
    }}
    QTabBar::tab:hover:!selected {{
        background: #252536;
    }}
"""


APP_QSS = f"""
    QMainWindow, QWidget {{
        background: {BASE};
        color: {TEXT};
        font-size: 10pt;
    }}
    QMenuBar {{
        background: {MANTLE};
        color: {TEXT};
    }}
    QMenuBar::item:selected {{
        background: {SURFACE0};
    }}
    QMenu {{
        background: {MANTLE};
        color: {TEXT};
        border: 1px solid {SURFACE1};
    }}
    QMenu::item:selected {{
        background: {SURFACE0};
    }}
    QGroupBox {{
        border: 1px solid {SURFACE1};
        border-radius: 6px;
        margin-top: 10px;
        padding-top: 10px;
        font-weight: bold;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {SUBTEXT};
    }}
    QLabel {{
        color: {TEXT};
    }}
    QLabel:disabled {{
        color: {SUBTEXT};
    }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {SURFACE0};
        color: {TEXT};
        border: 1px solid {SURFACE1};
        border-radius: 4px;
        padding: 4px 6px;
        selection-background-color: {BLUE};
        selection-color: {BASE};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
        border: 1px solid {BLUE};
    }}
    QComboBox {{
        padding-right: 30px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        border: none;
        width: 26px;
    }}
    QComboBox::down-arrow {{
        image: url("{_COMBO_ARROW}");
        width: 12px;
        height: 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {SURFACE0};
        color: {TEXT};
        selection-background-color: {BLUE};
        selection-color: {BASE};
        border: 1px solid {SURFACE1};
    }}
    QPushButton {{
        background: {SURFACE0};
        color: {TEXT};
        border: 1px solid {SURFACE1};
        border-radius: 6px;
        padding: 6px 16px;
    }}
    QPushButton:hover {{
        background: {SURFACE2};
    }}
    QPushButton:pressed {{
        background: {SURFACE1};
    }}
    QPushButton:disabled {{
        color: {SUBTEXT};
        background: {MANTLE};
    }}
    QPushButton#primary {{
        background: {BLUE};
        color: {BASE};
        font-weight: bold;
        border: none;
    }}
    QPushButton#primary:hover {{
        background: #a7c8fb;
    }}
    QCheckBox {{
        color: {TEXT};
    }}
    QCheckBox::indicator {{
        width: 15px;
        height: 15px;
        border: 1px solid {SURFACE1};
        border-radius: 3px;
        background: {SURFACE0};
    }}
    QCheckBox::indicator:checked {{
        background: {BLUE};
        border: 1px solid {BLUE};
    }}
    QListWidget, QTableWidget {{
        background: {SURFACE0};
        color: {TEXT};
        border: 1px solid {SURFACE1};
        border-radius: 6px;
        gridline-color: {SURFACE1};
    }}
    QListWidget::item:selected, QTableWidget::item:selected {{
        background: {BLUE};
        color: {BASE};
    }}
    QHeaderView::section {{
        background: {MANTLE};
        color: {SUBTEXT};
        border: none;
        border-right: 1px solid {SURFACE1};
        border-bottom: 1px solid {SURFACE1};
        padding: 4px;
    }}
    QTableCornerButton::section {{
        background: {MANTLE};
        border: none;
    }}
    QProgressBar {{
        background: {SURFACE0};
        border: 1px solid {SURFACE1};
        border-radius: 6px;
        text-align: center;
        color: {TEXT};
    }}
    QProgressBar::chunk {{
        background: {BLUE};
        border-radius: 5px;
    }}
    QStatusBar {{
        background: {MANTLE};
        color: {SUBTEXT};
    }}
    QScrollBar:vertical {{
        background: {BASE};
        width: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {SURFACE1};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {SURFACE2};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
{TAB_QSS}
"""
