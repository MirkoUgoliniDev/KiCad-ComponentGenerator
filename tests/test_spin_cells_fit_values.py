"""@brief Number cells of «Modifica componenti» show their widest value whole (HANDOFF 138, C3).

The theme draws the spin box buttons itself (point 135). A right padding as
wide as the button column reserved it twice, and in the editor's default
100 px sections the six-decimal values were cut («1,600000» read «1,6000»).
The fix is general: the sheet no longer pads twice, and the editor sizes each
numeric column on what its spin box has to show (`spin_box_width_for_values`:
range, decimals, suffix, and the chrome of the style in use).

"Shown whole" is checked the way a reader sees it, not with the formula under
test: with the cursor at the start the line edit is not scrolled, and the
character position at its right edge must be past the last character.
"""
import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QDoubleSpinBox, QStyleFactory, QWidget

from kicadgen.gui.component_editor_dialog import ComponentEditorDialog
from kicadgen.gui.theme import APP_QSS, spin_box_width_for_values
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS

_NUMBER_COLUMNS = range(2, 7)


def _shows_whole(spin):
    """True when every character of the spin box text is inside its field."""
    edit = spin.lineEdit()
    edit.setCursorPosition(0)                     # no horizontal scroll
    last = edit.cursorPositionAt(QPoint(edit.width() - 1, edit.height() // 2))
    return last >= len(edit.text())


def _set_widest(spin):
    """Put the widest value of the range in the field (most characters)."""
    ends = (spin.minimum(), spin.maximum())
    spin.setValue(max(ends, key=lambda value: len(spin.textFromValue(value))))


@pytest.fixture(params=QStyleFactory.keys())
def editor(request, qapp):
    style = QStyleFactory.create(request.param)
    bodies = [dict(name="U1", kind="ic", x_mm=1.0, y_mm=2.0, length_mm=1.6, width_mm=3.0,
                   height_mm=1.0, _height_estimated=True),       # suffix « ~» on the height
              dict(name="J1", kind="connector", x_mm=0.0, y_mm=0.0, length_mm=1234.5,
                   width_mm=2.0, height_mm=0.8)]                  # range beyond 300 mm
    dialog = ComponentEditorDialog(bodies, COMPONENT_KINDS)
    dialog.setStyleSheet(APP_QSS)
    # QWidget.setStyle does not reach children: each cell gets the style, and
    # its column is fitted again from the default width, as `_add_row` does.
    for column in _NUMBER_COLUMNS:
        dialog.table.setColumnWidth(column, 100)
    for row in range(dialog.table.rowCount()):
        for column in _NUMBER_COLUMNS:
            spin = dialog.table.cellWidget(row, column)
            spin.setStyle(style)
            dialog._fit_column_to(column, spin)
    dialog.show()
    qapp.processEvents()
    yield dialog
    dialog.close()
    dialog.deleteLater()


def test_every_number_cell_shows_its_widest_value(editor):
    for row in range(editor.table.rowCount()):
        for column in _NUMBER_COLUMNS:
            spin = editor.table.cellWidget(row, column)
            _set_widest(spin)
            assert _shows_whole(spin), (spin.style().name(), row, column, spin.text(),
                                        spin.lineEdit().width())


def test_the_values_of_the_reported_case_are_not_cut(editor):
    spin = editor.table.cellWidget(0, 4)             # Lung. 1.6 mm, six decimals
    spin.setValue(1.6)
    assert spin.text() == spin.textFromValue(1.6)
    assert _shows_whole(spin)
    height = editor.table.cellWidget(0, 6)
    assert height.suffix() == " ~" and _shows_whole(height)


def test_a_range_beyond_300_mm_widens_its_column_only(editor):
    """Negative/positive: the long range grows Lung.; Larg. keeps the narrower fit."""
    assert editor.table.columnWidth(4) > editor.table.columnWidth(5)


def test_the_measure_follows_the_range_not_a_fixed_width(qapp):
    """Negative: a wider range needs a wider box; the same range, the same width."""
    narrow, wide, same = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
    for spin, top in ((narrow, 30.0), (wide, 30000.0), (same, 30.0)):
        spin.setStyleSheet(APP_QSS)
        spin.setDecimals(6)
        spin.setRange(0.0, top)
    assert spin_box_width_for_values(wide) > spin_box_width_for_values(narrow)
    assert spin_box_width_for_values(same) == spin_box_width_for_values(narrow)


def test_a_box_sized_a_little_short_does_cut(qapp):
    """Negative control of the check itself: `_shows_whole` does see a clipped value.

    The spin box is a child, not a window: a native top-level window has a
    minimum width of its own and would not shrink.
    """
    host = QWidget()
    host.resize(400, 60)
    spin = QDoubleSpinBox(host)
    spin.setStyleSheet(APP_QSS)
    spin.setDecimals(6)
    spin.setRange(-300.0, 300.0)
    spin.setValue(-300.0)
    host.show()
    try:
        spin.resize(spin_box_width_for_values(spin) - 12, 30)
        qapp.processEvents()
        assert not _shows_whole(spin)
        spin.resize(spin_box_width_for_values(spin), 30)
        qapp.processEvents()
        assert _shows_whole(spin)
    finally:
        host.close()
        host.deleteLater()
