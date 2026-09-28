"""@brief The arrows of every number field are clickable, whatever the Qt style (HANDOFF point 135).

The defect: with Qt's «windows11» style, which draws the two buttons side by
side, and the application style sheet (padding on spin boxes), the text field
covered most of the up arrow: a real click reached the QLineEdit inside the
spin box and the value never went up. QTest clicks sent to the spin box itself
did not see it, so the checks here click the widget that is REALLY under the
point (childAt), as the mouse does.
"""
import sys

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (QDoubleSpinBox, QSpinBox, QStyle, QStyleFactory,
                             QStyleOptionSpinBox)

from kicadgen.gui.theme import APP_QSS

_UP, _DOWN = QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown


def _rect(spin, control):
    option = QStyleOptionSpinBox()
    spin.initStyleOption(option)
    return spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, spin)


def _click(spin, control):
    """Click the centre of a button through whatever widget is really there."""
    centre = _rect(spin, control).center()
    target = spin.childAt(centre) or spin
    QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=target.mapFrom(spin, centre))


@pytest.fixture(params=QStyleFactory.keys())
def styled_spins(request, qapp):
    style = QStyleFactory.create(request.param)
    spins = []
    for kind in (QSpinBox, QDoubleSpinBox):
        spin = kind()
        spin.setStyle(style)
        spin.setStyleSheet(APP_QSS)
        spin.setRange(0, 100)
        spin.resize(150, 33)
        spin.show()
        spins.append(spin)
    qapp.processEvents()
    yield spins
    for spin in spins:
        spin.close()
        spin.deleteLater()


def test_the_text_field_never_covers_the_arrows(styled_spins):
    for spin in styled_spins:
        field = spin.lineEdit().geometry()
        for control in (_UP, _DOWN):
            assert not field.intersects(_rect(spin, control)), (spin.style().name(), control)


def test_the_button_column_is_reserved_once(styled_spins):
    """The text field reaches the buttons: no padding reserves their column again.

    HANDOFF 138, C3: a right padding as wide as the buttons, on top of the
    geometry the sheet already gives them, left a gap as wide as a button
    between field and arrows, and cut values in narrow table cells.
    """
    for spin in styled_spins:
        field = spin.lineEdit().geometry()
        up = _rect(spin, _UP)
        assert up.left() - field.right() < up.width(), (spin.style().name(), field, up)


def test_both_arrows_change_the_value_when_clicked(styled_spins):
    for spin in styled_spins:
        spin.setValue(50)
        _click(spin, _UP)
        assert spin.value() > 50, spin.style().name()
        spin.setValue(50)
        _click(spin, _DOWN)
        assert spin.value() < 50, spin.style().name()


def test_the_arrows_are_drawn_by_the_theme():
    """The geometry comes from the sheet, so the rule must stay in it."""
    for selector in ("QDoubleSpinBox::up-button", "QDoubleSpinBox::down-button",
                     "QSpinBox::up-arrow", "QSpinBox::down-arrow"):
        assert selector in APP_QSS


def test_the_windows11_style_is_really_among_the_checked_ones():
    """The parametrized checks cover «windows11» on Windows; they must not lose it silently.

    The fixture runs over `QStyleFactory.keys()`: if the style plugin went
    missing (a Qt update, a packaging change) the defective case would just
    disappear from the run and every check would still pass.
    """
    if sys.platform != "win32":
        pytest.skip("the «windows11» Qt style exists only on Windows")
    assert "windows11" in [key.lower() for key in QStyleFactory.keys()], QStyleFactory.keys()
