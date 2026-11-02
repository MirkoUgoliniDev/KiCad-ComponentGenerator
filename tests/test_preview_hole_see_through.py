"""@brief The drill hole of a preview pad shows what lies beneath (user, 2026-11-02).

«perché dentro il pad non fa vedere il reticolo?» (why is the grid not visible
inside the pad?): the hole was a disc filled with the background colour over
the copper. Now the copper leaves the hole unpainted. Proved on pixels: an item
placed UNDER the pad shows inside the hole and stays hidden under the ring.
"""
import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QBrush, QColor, QPen

from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.gui.theme import BLUE
from tests.test_photo_inventory_gui import QApplication, gui

PX = gui._FP_PREVIEW_MM_TO_PX
MARK = QColor("#ff0000")


def _pixel(window, x_mm, y_mm):
    view = window.preview_view
    image = view.viewport().grab().toImage()
    point = view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))
    return QColor(image.pixel(point.x(), point.y()))


def _under_the_pad(window, x_mm, y_mm, size_mm):
    """A red square below the copper (lower z), centred on a pad."""
    half = size_mm / 2 * PX
    item = window.preview_scene.addRect(QRectF(x_mm * PX - half, y_mm * PX - half, 2 * half, 2 * half),
                                        QPen(MARK), QBrush(MARK))
    item.setZValue(-5)
    QApplication.processEvents()


@pytest.mark.parametrize("shape", ["circle", "rect", "oval"])
def test_the_hole_shows_what_lies_beneath_and_the_ring_hides_it(window, shape):
    """@brief Positive: red inside the hole; negative: copper blue on the ring, red hidden there."""
    window.show()
    package = PackageGeometry(family="custom", body_length_mm=8, body_width_mm=8, pins=[
        PinSpec("1", 0.0, 0.0, 3.0, 3.0, pad_type="thru_hole", shape=shape, drill_mm=1.5)])
    window.package_geometry = package
    window._draw_preview_package(package)
    window.preview_view.camera.fit(QRectF(-3 * PX, -3 * PX, 6 * PX, 6 * PX), force=True, reset=True)
    _under_the_pad(window, 0.0, 0.0, 2.8)
    # Off the centre: the drawing-origin marker sits on (0, 0), above everything.
    assert _pixel(window, 0.35, 0.4).name() == MARK.name()
    ring = _pixel(window, 1.1, 0.0)
    assert ring.name() != MARK.name()
    assert ring.name() == QColor(BLUE).name()


def test_a_pin_header_hole_shows_what_lies_beneath(window, monkeypatch):
    """@brief Positive: the grid-header renderer cuts its holes too; negative: its ring stays copper."""
    window.show()
    params = gui.PinHeaderParams(name="H", rows=1, cols=1)
    monkeypatch.setattr(window, "_preview_params", lambda: params)
    window._draw_preview()
    radius = params.pad_diameter / 2
    window.preview_view.camera.fit(QRectF(-3 * PX, -3 * PX, 6 * PX, 6 * PX), force=True, reset=True)
    _under_the_pad(window, 0.0, 0.0, params.pad_diameter * 0.95)
    # Off the centre: the drawing-origin marker sits on pin 1 (the header's origin).
    offset = params.drill / 4
    assert _pixel(window, offset, offset).name() == MARK.name()
    ring = (params.drill / 2 + radius) / 2
    assert _pixel(window, ring, 0.0).name() != MARK.name()
