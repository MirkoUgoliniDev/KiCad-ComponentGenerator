"""@brief Shared Catppuccin Mocha theme: palette and style sheets of the application.

Espone la QSS applicata all'intera applicazione (`APP_QSS`) e quella delle
schede (`TAB_QSS`). La palette e' la stessa di Pm3ToWav; le schede non sono
piu' identiche alle sue (2026-09-24: scelta in grassetto e piu' chiara, le
altre scure, le disabilitate quasi nere). La scheda scelta si allarga di
qualche pixel per il grassetto: e' il prezzo accettato della scelta grafica.
"""

from pathlib import Path

_COMBO_ARROW = (Path(__file__).resolve().parent / "assets" / "combo-arrow.svg").as_posix()
#: Segno di spunta delle caselle accese: senza, una casella accesa era solo
#: un quadrato pieno di blu, che non si legge come «acceso» (utente, 2026-09-24).
_CHECK_MARK = (Path(__file__).resolve().parent / "assets" / "check-mark.svg").as_posix()

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

#: Schede: quella SCELTA e' la piu' evidente (sfondo chiaro, sottolineatura
#: blu, testo in grassetto); le altre sono attenuate, le disabilitate ancora di
#: piu'. Prima la scelta aveva lo sfondo scuro della pagina e le altre uno
#: piu' chiaro: con Anteprima e Modello 3D disabilitate all'avvio, la scheda
#: KiCad era la piu' chiara e sembrava lei quella scelta (utente, 2026-09-24).
TAB_QSS = f"""
    QTabWidget::pane {{
        border: 1px solid {SURFACE1};
        border-radius: 6px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: {MANTLE};
        color: {SUBTEXT};
        border: 1px solid {SURFACE1};
        border-bottom: none;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        padding: 8px 18px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {SURFACE0};
        border-bottom: 3px solid {BLUE};
        color: {TEXT};
        font-weight: 600;
    }}
    QTabBar::tab:hover:!selected {{
        background: #252536;
        color: {TEXT};
    }}
    QTabBar::tab:disabled {{
        background: #101017;
        color: #626477;
        border-color: #2b2c3b;
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
    QToolTip {{
        background: {MANTLE};
        color: {TEXT};
        border: 1px solid {SURFACE1};
        padding: 5px 8px;
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
        image: url("{_CHECK_MARK}");
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
    /* Barre di icone: il pulsante si vede al passaggio del mouse, e un
       interruttore acceso (es. numeri pin) resta evidenziato. */
    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 5px;
        padding: 3px;
    }}
    QToolButton:hover {{
        background: {SURFACE0};
        border: 1px solid {SURFACE2};
    }}
    QToolButton:checked {{
        background: {SURFACE1};
        border: 1px solid {BLUE};
    }}
{TAB_QSS}
"""
