"""@brief Dotted drawing grid of the symbol canvas (user, 2026-10-13).

The drawing grid is one tenth of the pin grid by default and is painted as
dots sharing the pin grid's origin; the pins keep snapping to the 2.54 mm
crossings whatever the drawing grid is.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor

from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui.symbol_tab import (SymbolTab, SCENE_UNITS_PER_MM, GRID_MM, DEFAULT_DRAWING_GRID_MM,
                                     DRAWING_GRIDS_MM, FINE_GRID_MIN_PX, CANVAS_THEMES)


def _tab(qapp):
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)],
                 pad_ids=['1', '2', '3', '4', '5'])
    tab.show()
    qapp.processEvents()
    return tab


def _zoom_to_fine_pixels(tab, qapp, pixels):
    """Scale the view so one drawing-grid step spans `pixels` viewport pixels."""
    view = tab.view
    view.resetTransform()
    factor = pixels / (view.drawing_grid_mm * SCENE_UNITS_PER_MM)
    view.scale(factor, factor)
    view.centerOn(QPointF(0, 0))
    qapp.processEvents()


def _grab(tab):
    return tab.view.viewport().grab().toImage()


def _clear_fine_node(tab, image):
    """A drawing-grid node off every pin-grid line, with no item nearby, inside the image.

    Returns (scene point, viewport point). Indices that are multiples of the
    pin/drawing ratio lie on a pin line and are skipped.
    """
    view = tab.view
    fine = view.drawing_grid_mm * SCENE_UNITS_PER_MM
    ratio = round(GRID_MM / view.drawing_grid_mm)
    for i in range(1, 80):
        for j in range(1, 80):
            if i % ratio == 0 or j % ratio == 0:
                continue
            point = QPointF(i * fine, j * fine)
            if view.scene().items(QRectF(point.x() - fine, point.y() - fine, 2 * fine, 2 * fine)):
                continue
            pixel = view.mapFromScene(point)
            if 2 <= pixel.x() < image.width() - 2 and 2 <= pixel.y() < image.height() - 2:
                return point, pixel
    raise AssertionError('no free drawing-grid node inside the viewport')


def test_default_drawing_grid_is_one_tenth_of_the_pin_grid_and_the_combo_shows_it(qapp):
    tab = _tab(qapp)
    try:
        assert DEFAULT_DRAWING_GRID_MM == GRID_MM / 10 and DEFAULT_DRAWING_GRID_MM in DRAWING_GRIDS_MM
        assert tab.drawing_grid() == DEFAULT_DRAWING_GRID_MM
        assert tab.grid_combo.currentData() == DEFAULT_DRAWING_GRID_MM
        assert tab.view.drawing_grid_mm == DEFAULT_DRAWING_GRID_MM, 'the canvas knows the step from construction'
        tab.set_drawing_grid(1.27)
        assert tab.view.drawing_grid_mm == 1.27, 'the canvas follows the combo'
    finally:
        tab.deleteLater()


def test_shape_points_snap_to_the_fine_grid_while_pins_keep_the_pin_grid(qapp):
    tab = _tab(qapp)
    try:
        # A shape point: 7.5 mm is not on the pin grid (7.62) but is close to
        # the fine node 7.62 = 30 x 0.254; 5.2 falls on 5.08 = 20 x 0.254.
        # The negative case for the pin grid: 7.3 mm snaps to 7.366 (29 x
        # 0.254), a value the pin grid could never produce.
        tab.start_shape('rectangle')
        tab.add_shape_point(0, 0)
        tab.add_shape_point(7.3, 2.6)
        tab.view.wire_finished.emit(False)
        assert g.shape_handles(tab.graphics()[0])[1] == (7.366, 2.54)
        assert (7.366 / GRID_MM) % 1 != 0, 'the shape corner is off the pin grid: fine-grid snapping'
        # A pin dragged with the mouse to x = 1.2 mm lands on the pin grid
        # (0), never on the fine node 1.27 the drawing grid would give.
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        view = tab.view
        pin = tab._symbol.pins[0]
        start = view.mapFromScene(QPointF(pin.x * SCENE_UNITS_PER_MM, -pin.y * SCENE_UNITS_PER_MM))
        end = view.mapFromScene(QPointF(1.2 * SCENE_UNITS_PER_MM,
                                        -(tab._symbol.height / 2 + 2.54) * SCENE_UNITS_PER_MM))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        assert view.gesture == ('pin', '1')
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        x = tab.draft_rows()[0]['symbol_position'][0]
        assert x == 0 and x != 1.27, 'pins snap to GRID_MM whatever the drawing grid'
        # The negative case for the fine grid: with the drawing grid at the
        # pin grid the same shape corner snaps to 7.62.
        tab.set_drawing_grid(GRID_MM)
        assert tab.move_shape_point(0, 1, 7.3, 2.6) and g.shape_handles(tab.graphics()[0])[1] == (7.62, 2.54)
    finally:
        tab.deleteLater()


def test_fine_grid_dots_are_painted_only_when_legible(qapp):
    tab = _tab(qapp)
    try:
        theme = CANVAS_THEMES['dark']
        dots = QColor(theme['grid_dots']).name()
        canvas = QColor(theme['canvas']).name()
        assert dots not in (canvas, QColor(theme['grid']).name())
        # Above the legibility threshold: a dot at a node that is on no pin line.
        _zoom_to_fine_pixels(tab, qapp, FINE_GRID_MIN_PX + 3)
        assert tab.view.fine_grid_visible()
        image = _grab(tab)
        point, pixel = _clear_fine_node(tab, image)
        assert image.pixelColor(pixel).name() == dots
        fine = tab.view.drawing_grid_mm * SCENE_UNITS_PER_MM
        between = tab.view.mapFromScene(point + QPointF(fine / 2, fine / 2))
        assert image.pixelColor(between).name() == canvas, 'nothing between two nodes'
        # Below the threshold: the same node shows the bare canvas.
        _zoom_to_fine_pixels(tab, qapp, FINE_GRID_MIN_PX - 1)
        assert not tab.view.fine_grid_visible()
        image = _grab(tab)
        pixel = tab.view.mapFromScene(point)
        assert image.pixelColor(pixel).name() == canvas
        assert not any(image.pixelColor(x, y).name() == dots
                       for x in range(0, image.width(), 3) for y in range(0, image.height(), 3))
    finally:
        tab.deleteLater()


def test_no_dots_when_the_drawing_grid_equals_the_pin_grid(qapp):
    tab = _tab(qapp)
    try:
        dots = QColor(CANVAS_THEMES['dark']['grid_dots']).name()
        tab.set_drawing_grid(GRID_MM)
        _zoom_to_fine_pixels(tab, qapp, FINE_GRID_MIN_PX + 3)
        assert not tab.view.fine_grid_visible()
        image = _grab(tab)
        assert not any(image.pixelColor(x, y).name() == dots
                       for x in range(0, image.width(), 2) for y in range(0, image.height(), 2))
    finally:
        tab.deleteLater()


def test_both_themes_declare_the_dot_colour(qapp):
    for theme in CANVAS_THEMES.values():
        assert QColor(theme['grid_dots']).isValid()
        assert theme['grid_dots'] not in (theme['grid'], theme['canvas'])
