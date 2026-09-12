"""The combo affordance must be visible even with a dark custom stylesheet."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QStyle, QStyleOptionComboBox

from kicadgen.gui.theme import APP_QSS


@pytest.mark.parametrize("editable", [False, True])
def test_arrow_is_rendered_and_opens_popup(editable):
    app = QApplication.instance() or QApplication([])
    combo = QComboBox()
    combo.setStyleSheet(APP_QSS)
    combo.setEditable(editable)
    combo.addItems(["Prima", "Seconda"])
    combo.resize(320, 36)
    combo.show()
    app.processEvents()
    try:
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        rect = combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxArrow, combo,
        )
        pixmap = combo.grab()
        image = pixmap.toImage()
        ratio = pixmap.devicePixelRatio()
        bright = sum(
            image.pixelColor(x, y).lightness() > 180
            for x in range(round(rect.left() * ratio), round((rect.right() + 1) * ratio))
            for y in range(round(rect.top() * ratio), round((rect.bottom() + 1) * ratio))
        )
        assert bright >= 15, "The dropdown triangle is missing or invisible"
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton, pos=rect.center())
        app.processEvents()
        assert combo.view().isVisible()
        combo.setCurrentIndex(1)
        assert combo.currentText() == "Seconda"
    finally:
        combo.hidePopup()
        combo.close()
