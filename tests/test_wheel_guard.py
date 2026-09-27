"""@brief The wheel scrolls the page; drop-downs and numbers change only after a click (2026-09-26)."""
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QScrollArea, QVBoxLayout,
                             QWidget)

from kicadgen.gui import wheel_guard


@pytest.fixture
def page(qapp):
    """A scrolling page with a drop-down and a number field near the top."""
    app = qapp
    wheel_guard.install(app)
    area = QScrollArea()
    content = QWidget()
    layout = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["castellated", "passanti", "SMD", "THT"])
    spin = QDoubleSpinBox()
    spin.setRange(0, 100)
    spin.setValue(63.3)
    layout.addWidget(combo)
    layout.addWidget(spin)
    layout.addSpacing(3000)
    area.setWidget(content)
    area.resize(300, 200)
    area.show()
    app.processEvents()
    yield app, area, combo, spin
    area.close()


def _wheel(app, widget, steps=-1):
    event = QWheelEvent(QPointF(5, 5), QPointF(widget.mapToGlobal(QPoint(5, 5))), QPoint(0, 0),
                        QPoint(0, 120 * steps), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    app.sendEvent(widget, event)
    app.processEvents()


def test_the_wheel_over_an_untouched_drop_down_scrolls_the_page(page):
    app, area, combo, spin = page
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus
    before = area.verticalScrollBar().value()
    _wheel(app, combo)
    _wheel(app, spin)
    assert combo.currentIndex() == 0 and spin.value() == pytest.approx(63.3)
    assert area.verticalScrollBar().value() > before


def test_a_clicked_drop_down_in_a_scrolling_page_still_scrolls_the_page(page):
    """@brief Il caso reale del 26/09: tendina scelta con un clic (ha il fuoco), poi si scorre."""
    app, area, combo, _spin = page
    combo.setFocus()
    app.processEvents()
    before = area.verticalScrollBar().value()
    _wheel(app, combo)
    assert combo.currentIndex() == 0 and area.verticalScrollBar().value() > before


def test_outside_a_scrolling_page_only_the_focused_field_takes_the_wheel(qapp):
    """@brief Negativo: in una finestra piccola la rotellina resta utile sul campo attivo."""
    app = qapp
    wheel_guard.install(app)
    window = QWidget()
    layout = QVBoxLayout(window)
    first, second = QComboBox(), QComboBox()
    for combo in (first, second):
        combo.addItems(["a", "b", "c"])
        layout.addWidget(combo)
    window.show()
    first.setFocus()
    app.processEvents()
    if not first.hasFocus():
        pytest.skip("la piattaforma di test non assegna il fuoco")
    _wheel(app, second)
    _wheel(app, first)
    assert second.currentIndex() == 0 and first.currentIndex() == 1
    window.close()
