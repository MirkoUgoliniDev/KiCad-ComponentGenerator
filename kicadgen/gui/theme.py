"""@brief Shared Catppuccin Mocha theme: palette and style sheets of the application.

Exposes the QSS applied to the whole application (`APP_QSS`) and the one of
the tabs (`TAB_QSS`), and the measure of the width a spin box with those
buttons needs to show its widest value in full (`spin_box_width_for_values`).
The palette is the same as Pm3ToWav's; the tabs are no longer identical to
its (2026-09-24: the chosen one bold and lighter, the others dark, the
disabled ones almost black). The chosen tab widens by a few pixels because of
the bold face: that is the accepted price of the graphic choice.
"""

from pathlib import Path

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QStyle, QStyleOptionSpinBox

_COMBO_ARROW = (Path(__file__).resolve().parent / "assets" / "combo-arrow.svg").as_posix()
_SPIN_UP = (Path(__file__).resolve().parent / "assets" / "spin-arrow-up.svg").as_posix()
_SPIN_DOWN = (Path(__file__).resolve().parent / "assets" / "spin-arrow-down.svg").as_posix()
#: Check mark of the checked boxes: without it, a checked box was only a
#: square filled with blue, which does not read as «acceso» (on) (user, 2026-09-24).
_CHECK_MARK = (Path(__file__).resolve().parent / "assets" / "check-mark.svg").as_posix()

# Catppuccin Mocha palette (same hexadecimal values used in Pm3ToWav)
BASE = "#1e1e2e"       # window background
MANTLE = "#181825"     # darkest background (menus, bars)
SURFACE0 = "#313244"   # widget background (inputs, unselected tabs)
SURFACE1 = "#45475a"   # borders
SURFACE2 = "#585b70"   # hover borders
TEXT = "#cdd6f4"       # main text
SUBTEXT = "#a6adc8"    # secondary / disabled text
BLUE = "#89b4fa"       # accent (selection, focus, progress)
GREEN = "#a6e3a1"      # success
RED = "#f38ba8"        # error
YELLOW = "#f9e2af"     # warning

#: Tabs: the SELECTED one is the most prominent (light background, blue
#: underline, bold text); the others are dimmed, the disabled ones even
#: more. Previously the selected one had the dark page background and the
#: others a lighter one: with Anteprima (preview) and Modello 3D (3D model)
#: disabled at start-up, the KiCad tab was the lightest and looked like the
#: selected one (user, 2026-09-24).
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
    /* Voce spenta (per esempio i comandi KiCad senza footprint): senza questa
       regola sembrava uguale a una voce attiva. Stesso grigio delle schede
       spente. */
    QMenu::item:disabled {{
        color: #626477;
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
    /* Spin box buttons placed by this sheet, one above the other on the
       right (HANDOFF point 135). Left to the native style, Qt's «windows11»
       style puts them side by side while the style sheet still lays the text
       field out for stacked buttons: the field covered most of the up arrow
       and swallowed its clicks. With explicit geometry every style gets the
       same, non-overlapping layout (tests/test_spin_box_buttons.py).
       No extra right padding: the style sheet already ends the text field at
       the button column, so a padding as wide as the buttons reserved that
       column twice and clipped values in narrow cells (HANDOFF 138, C3). */
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 22px;
        border-left: 1px solid {SURFACE1};
        border-top-right-radius: 4px;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 22px;
        border-left: 1px solid {SURFACE1};
        border-top: 1px solid {SURFACE1};
        border-bottom-right-radius: 4px;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {SURFACE1};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url("{_SPIN_UP}");
        width: 10px;
        height: 6px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url("{_SPIN_DOWN}");
        width: 10px;
        height: 6px;
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


#: Width of the box used to measure a spin box's chrome: any width well above
#: the chrome itself gives the same difference, it only has to leave a field.
_SPIN_PROBE_WIDTH = 1000

#: Horizontal margin QLineEdit keeps on each side of its text (Qt's internal
#: `QLineEditPrivate::horizontalMargin`) plus one pixel for the text cursor.
#: Not exposed by the API; without it the last digit ends under the cursor.
_LINE_EDIT_TEXT_MARGIN = 2 * 2 + 1


def spin_box_width_for_values(spin):
    """@brief Width a spin box needs to show its widest value whole.

    @param spin QSpinBox or QDoubleSpinBox, already configured (range,
        decimals, prefix, suffix, special value text) and, when it lives in a
        container with its own style sheet, already placed in it.
    @return Width in pixels: the text of the widest value it can hold, as its
        own text field draws it, plus the chrome (frame, padding, button
        column) that its CURRENT style and style sheet put around the field.
    @note The chrome is measured, not assumed: the edit-field sub-control
        is asked of the style for a wide probe rectangle, so the same number
        is right for every Qt style and for any change of this sheet. The
        spin box's own `sizeHint` is not used because the «windows11» style
        sizes it for two buttons side by side, which the sheet does not draw
        (about 190 px for «-300,000000» against the ~110 px really needed).
    @note The widest value is taken among minimum and maximum (and the
        special value text): with the tabular digits of UI fonts, the value
        with the most characters is one of the two ends of the range.
    """
    spin.ensurePolished()
    option = QStyleOptionSpinBox()
    spin.initStyleOption(option)
    option.rect = QRect(0, 0, _SPIN_PROBE_WIDTH, max(spin.height(), spin.minimumSizeHint().height()))
    field = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                        QStyle.SubControl.SC_SpinBoxEditField, spin)
    chrome = _SPIN_PROBE_WIDTH - field.width()
    edit = spin.lineEdit()
    texts = [spin.prefix() + spin.textFromValue(value) + spin.suffix()
             for value in (spin.minimum(), spin.maximum())]
    if spin.specialValueText():
        texts.append(spin.specialValueText())
    metrics = edit.fontMetrics()
    text = max(metrics.horizontalAdvance(candidate) for candidate in texts)
    margins = edit.textMargins()
    contents = edit.contentsMargins()
    return (chrome + text + _LINE_EDIT_TEXT_MARGIN + margins.left() + margins.right()
            + contents.left() + contents.right())
