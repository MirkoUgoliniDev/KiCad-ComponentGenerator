"""@brief The symbol grid is laid out only where Qt paints, with the same pixels (2026-10-17).

Qt hands `drawBackground` the bounding rectangle of the exposed region and
clips the pixels to the region; during an edit at 900 pins that rectangle
was most of the viewport (a pin plus the resize handles at the body
corners) and the whole grid was laid out for a few visible pixels. The
view now keeps the paint event's region and lays out only the lines and
dots that reach one of its rectangles. The pictures must be identical.

Also here: the first geometry edit of an automatic layout rebuilds only
the pins that moved (the cache key is the pin's picture, not its row data).
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

import pytest
from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter, QRegion

from kicadgen.gui.symbol_tab import (FINE_GRID_MIN_PX, GRID_MM, GRID_PAINT_MARGIN_PX, SCENE_UNITS_PER_MM,
                                     SymbolTab)


def _tab(qapp, pins=80):
    tab = SymbolTab()
    tab.resize(1200, 800)
    sides = ("left", "right", "top", "bottom")
    tab.set_data("Board", [{"pin": str(i), "name": f"P{i}", "symbol_side": sides[i % 4]}
                           for i in range(1, pins + 1)], pad_ids=[str(i) for i in range(1, pins + 1)])
    tab.show()
    qapp.processEvents()
    return tab


def _sparse_region(view):
    """Two far-apart rectangles of the viewport, as two dirty items leave them: (region, rectangles)."""
    w, h = view.viewport().width(), view.viewport().height()
    rects = [QRect(w // 10, h // 10, 60, 40), QRect(w * 3 // 4, h * 3 // 4, 80, 50)]
    return QRegion(rects[0]) | QRegion(rects[1]), rects


class _Counting(QPainter):
    """A painter that records the grid lines and dots it is asked to draw."""

    def __init__(self, image):
        super().__init__(image)
        self.lines = []
        self.dots = 0

    def drawLine(self, *args):
        self.lines.append(args[0])
        super().drawLine(*args)

    def drawPoints(self, polygon):
        self.dots += polygon.count()
        super().drawPoints(polygon)


def _paint(view, region, exposed):
    """Paint the background into an image clipped to `region`, with or without the region known."""
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(view.theme["canvas"]))
    painter = _Counting(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRegion(region)
    painter.setWorldTransform(view.viewportTransform())
    view._exposed_region = region if exposed else None
    try:
        view.drawBackground(painter, view.mapToScene(region.boundingRect()).boundingRect())
    finally:
        view._exposed_region = None
        painter.end()
    return image, painter


def _bytes(image):
    return image.constBits().asstring(image.sizeInBytes())


def _show_dots(tab, qapp):
    view = tab.view
    view.auto_fit = False
    view.resetTransform()
    factor = (FINE_GRID_MIN_PX + 3) / (view.drawing_grid_mm * SCENE_UNITS_PER_MM)
    view.scale(factor, factor)
    qapp.processEvents()
    assert view.fine_grid_visible()


@pytest.mark.parametrize("dots", [False, True])
def test_a_sparse_region_paints_the_same_pixels_with_fewer_lines(qapp, dots):
    """@brief Positive: byte-identical picture; negative: lines reaching no rectangle are not drawn."""
    tab = _tab(qapp)
    try:
        view = tab.view
        if dots:
            _show_dots(tab, qapp)
        region, rects = _sparse_region(view)
        before, full = _paint(view, region, exposed=False)
        after, limited = _paint(view, region, exposed=True)
        assert _bytes(after) == _bytes(before)
        assert 0 < len(limited.lines) < len(full.lines)
        margin = GRID_PAINT_MARGIN_PX + 1
        grown = [r.adjusted(-margin, -margin, margin, margin) for r in rects]
        for line in limited.lines:
            device = view.viewportTransform().map(line)
            crossing = any(QRectF(r).intersects(QRectF(device.p1(), device.p2()).normalized()
                                                 .adjusted(-0.5, -0.5, 0.5, 0.5)) for r in grown)
            assert crossing, f"a line outside the region was laid out: {device}"
        if dots:
            assert 0 < limited.dots < full.dots
        else:
            assert limited.dots == full.dots == 0
    finally:
        tab.deleteLater()


def test_the_whole_viewport_region_lays_out_the_full_grid(qapp):
    """@brief Negative: one rectangle covering the viewport changes nothing, lines and dots alike."""
    tab = _tab(qapp)
    try:
        view = tab.view
        _show_dots(tab, qapp)
        region = QRegion(view.viewport().rect())
        before, full = _paint(view, region, exposed=False)
        after, limited = _paint(view, region, exposed=True)
        assert _bytes(after) == _bytes(before)
        assert len(limited.lines) == len(full.lines) and limited.dots == full.dots
    finally:
        tab.deleteLater()


def test_a_real_paint_event_hands_its_region_to_the_background(qapp):
    tab = _tab(qapp)
    try:
        view = tab.view
        seen = []
        original = view.drawBackground

        def spy(painter, rect):
            region = view._exposed_region
            seen.append(None if region is None else region.rectCount())
            original(painter, rect)
        view.drawBackground = spy
        region, _rects = _sparse_region(view)
        view.viewport().repaint(region)
        assert seen and seen[-1] == region.rectCount() == 2
        assert view._exposed_region is None, "cleared after the paint"
        view.viewport().repaint()
        assert seen[-1] == 1, "a full repaint is one rectangle"
    finally:
        tab.deleteLater()


def test_outside_a_paint_event_the_grid_is_laid_out_over_the_whole_rect(qapp):
    tab = _tab(qapp)
    try:
        view = tab.view
        rect = QRectF(-100, -100, 200, 200)
        step = GRID_MM * SCENE_UNITS_PER_MM
        full = list(range(math.floor(rect.left() / step), math.ceil(rect.right() / step) + 1))
        assert view._grid_indices(rect, step, 0) == full
        view._exposed_region = QRegion()
        assert view._grid_indices(rect, step, 0) == full, "an empty region means no restriction"
        view._exposed_region = QRegion(view.viewport().rect())
        assert view._grid_indices(rect, step, 0) == full, "the whole viewport means no restriction"
        view._exposed_region = None
    finally:
        tab.deleteLater()


# ── Pin cache keyed on the picture ────────────────────────────────────

def _roots(tab):
    return {number: id(entry[1]) for number, entry in tab._pin_items.items()}


def _rebuilt(tab, before):
    after = _roots(tab)
    return {number for number in after if before.get(number) != after[number]}


def test_the_first_nudge_of_an_automatic_layout_rebuilds_only_the_moved_pins(qapp):
    tab = _tab(qapp, pins=20)
    try:
        assert all(row.get("symbol_position") is None for row in tab.draft_rows()), "automatic layout"
        tab._select_pins({"3"}, "3")
        qapp.processEvents()
        before = _roots(tab)
        pin = next(p for p in tab._symbol.pins if p.pin.number == "3")
        assert tab.place_pin("3", pin.x, pin.y + GRID_MM)
        qapp.processEvents()
        assert all(row.get("symbol_position") is not None for row in tab.draft_rows()), "now a free layout"
        rebuilt = _rebuilt(tab, before)
        assert "3" in rebuilt and len(rebuilt) <= 2, rebuilt
    finally:
        tab.deleteLater()


@pytest.mark.parametrize("change", ["name", "hidden", "length"])
def test_a_change_of_what_the_pin_shows_still_rebuilds_it(qapp, change):
    """@brief Negative: the picture key is not blind to the name, the visibility or the length."""
    tab = _tab(qapp, pins=20)
    try:
        before = _roots(tab)
        if change == "name":
            row = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "7")
            tab.table.item(row, 1).setText("RENAMED")
        elif change == "hidden":
            assert tab.set_pin_properties("7", hidden=True)
        else:
            assert tab.set_pin_properties("7", length=5.08)
        qapp.processEvents()
        assert _rebuilt(tab, before) == {"7"}
    finally:
        tab.deleteLater()
