"""@brief Unit tests of `kicadgen.gui.canvas_grid`: pixel equality with the two legacy `drawBackground` implementations.

The legacy algorithms are FROZEN here (copied from `_SymbolView` and
`FootprintView` as they were before the shared module): the new `paint_grid`
must produce the same bytes for the parameters each view passes, and a
region-limited paint must equal the full one inside the clip region.
"""
import math
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6.QtCore import QLineF, QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QRegion
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView

from kicadgen.gui.canvas_grid import ExposedRegion, GridStyle, dots_legible, paint_grid

LINE = '#8892a0'
DOT = '#c03030'
BACKGROUND = QColor('#ffffff')


# ------------------------------------------------------------- frozen legacy code

class LegacySymbolView(QGraphicsView):
    """Frozen copy of `_SymbolView.paintEvent/_grid_indices/drawBackground` (pin grid + dots, origin 0)."""

    def __init__(self, scene, step, fine):
        super().__init__(scene)
        self.step, self.fine = step, fine          # fine None: no dots
        self._exposed_region = None

    def paintEvent(self, event):
        self._exposed_region = event.region()
        try:
            super().paintEvent(event)
        finally:
            self._exposed_region = None

    def _grid_indices(self, rect, step, axis):
        low, high = (rect.left(), rect.right()) if axis == 0 else (rect.top(), rect.bottom())
        indices = range(math.floor(low / step), math.ceil(high / step) + 1)
        region = self._exposed_region
        if region is None or region.isEmpty() or (
                region.rectCount() == 1 and region.boundingRect().contains(self.viewport().rect())):
            return list(indices)
        transform = self.viewportTransform()
        kept = []
        for index in indices:
            at = index * step
            line = (QRectF(at, rect.top(), 0.0, rect.height()) if axis == 0
                    else QRectF(rect.left(), at, rect.width(), 0.0))
            strip = transform.mapRect(line).toAlignedRect().adjusted(-2, -2, 2, 2)
            if region.intersects(strip):
                kept.append(index)
        return kept

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        painter.save()
        if self.fine is not None:
            fine = self.fine
            ys = [row * fine for row in self._grid_indices(rect, fine, 1)]
            dots = QPolygonF([QPointF(column * fine, y)
                              for column in self._grid_indices(rect, fine, 0) for y in ys])
            dot_pen = QPen(QColor(DOT), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            dot_pen.setCosmetic(True)
            antialiased = painter.testRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(dot_pen)
            painter.drawPoints(dots)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, antialiased)
        step = self.step
        pen = QPen(QColor(LINE))
        pen.setCosmetic(True)
        painter.setPen(pen)
        for index in self._grid_indices(rect, step, 0):
            x = index * step
            painter.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
        for index in self._grid_indices(rect, step, 1):
            y = index * step
            painter.drawLine(QLineF(rect.left(), y, rect.right(), y))
        painter.restore()


class LegacyFootprintView(QGraphicsView):
    """Frozen copy of `FootprintView.drawBackground/_draw_fine_dots` (lines from an origin, dots off the lines)."""

    def __init__(self, scene, step, origin, divisor, dots_visible):
        super().__init__(scene)
        self.step, self.origin, self.divisor, self.dots_visible = step, origin, divisor, dots_visible

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        step = self.step
        origin_x, origin_y = self.origin
        grid_pen = QPen(QColor(LINE))
        grid_pen.setCosmetic(True)
        painter.save()
        painter.setPen(grid_pen)
        for index in range(math.floor((rect.left() - origin_x) / step),
                           math.ceil((rect.right() - origin_x) / step) + 1):
            x = origin_x + index * step
            painter.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
        for index in range(math.floor((rect.top() - origin_y) / step),
                           math.ceil((rect.bottom() - origin_y) / step) + 1):
            y = origin_y + index * step
            painter.drawLine(QLineF(rect.left(), y, rect.right(), y))
        if self.dots_visible:
            n = self.divisor
            fine = step / n
            dot_pen = QPen(QColor(DOT), 1.5)
            dot_pen.setCosmetic(True)
            dot_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(dot_pen)
            i0, i1 = math.floor((rect.left() - origin_x) / fine), math.ceil((rect.right() - origin_x) / fine)
            j0, j1 = math.floor((rect.top() - origin_y) / fine), math.ceil((rect.bottom() - origin_y) / fine)
            xs = [origin_x + i * fine for i in range(i0, i1 + 1) if i % n]
            ys = [origin_y + j * fine for j in range(j0, j1 + 1) if j % n]
            painter.drawPoints([QPointF(x, y) for x in xs for y in ys])
        painter.restore()


# ------------------------------------------------------------------ new views

class NewSymbolStyleView(QGraphicsView):
    """What the symbol editor will do: `paint_grid` with its parameters."""

    def __init__(self, scene, step, fine):
        super().__init__(scene)
        self.step, self.fine = step, fine
        self.exposed = ExposedRegion(self)
        self.seen_regions = []

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        self.seen_regions.append(None if self.exposed.region is None else QRegion(self.exposed.region))
        paint_grid(painter, rect, exposed=self.exposed, step=self.step, origin=(0.0, 0.0),
                   style=GridStyle(QColor(LINE), QColor(DOT), 1.5, False),
                   fine_step=self.fine, dots_first=True)


class NewFootprintStyleView(QGraphicsView):
    """What the footprint preview will do: lines from an origin, dots off the lines, painter's own hint."""

    def __init__(self, scene, step, origin, divisor, dots_visible):
        super().__init__(scene)
        self.step, self.origin, self.divisor, self.dots_visible = step, origin, divisor, dots_visible
        self.exposed = ExposedRegion(self)

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        paint_grid(painter, rect, exposed=self.exposed, step=self.step, origin=self.origin,
                   style=GridStyle(QColor(LINE), QColor(DOT), 1.5, None),
                   fine_step=self.step / self.divisor if self.dots_visible else None,
                   skip_dots_on_lines=True)


# ------------------------------------------------------------------- helpers

def _setup(view, zoom, centre, antialias=False):
    view.setBackgroundBrush(BACKGROUND)
    view.resize(320, 220)
    view.setRenderHint(QPainter.RenderHint.Antialiasing, antialias)
    view.scale(zoom, zoom)
    view.centerOn(*centre)
    view.show()


def _render(qapp, view, region=None):
    qapp.processEvents()
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
    image.fill(QColor('#ffff00'))              # a sentinel colour: unpainted pixels stay visible
    if region is None:
        view.viewport().render(image)
    else:
        view.viewport().render(image, region.boundingRect().topLeft(), region)
    return image


def _bytes(image):
    return bytes(image.constBits().asstring(image.sizeInBytes()))


def _dispose(*views):
    for v in views:
        v.hide()
        v.deleteLater()


def _inside(image, region):
    """The pixels of `image` that lie in `region`, as {(x, y): argb}."""
    out = {}
    for y in range(image.height()):
        for x in range(image.width()):
            if region.contains(QPoint(x, y)):
                out[(x, y)] = image.pixel(x, y)
    return out


SPARSE = QRegion(QRect(10, 10, 40, 30)) + QRegion(QRect(200, 120, 60, 50)) + QRegion(QRect(150, 5, 20, 20))


# ----------------------------------------------------- pixel equality, symbol style

@pytest.mark.parametrize('zoom', [0.4, 1.0, 2.5])
@pytest.mark.parametrize('fine', [None, 12.7, 5.08])
@pytest.mark.parametrize('antialias', [False, True])
def test_symbol_style_grid_is_byte_identical_to_the_legacy_view(qapp, zoom, fine, antialias):
    scene = QGraphicsScene(-1000, -1000, 2000, 2000)
    old = LegacySymbolView(scene, 25.4, fine)
    new = NewSymbolStyleView(scene, 25.4, fine)
    for v in (old, new):
        _setup(v, zoom, (33.3, -12.7), antialias)
    assert _bytes(_render(qapp, new)) == _bytes(_render(qapp, old))
    _dispose(old, new)


@pytest.mark.parametrize('zoom', [0.4, 2.5])
def test_symbol_style_sparse_paint_equals_the_legacy_sparse_paint_and_the_full_grid_inside_the_clip(qapp, zoom):
    scene = QGraphicsScene(-1000, -1000, 2000, 2000)
    old = LegacySymbolView(scene, 25.4, 12.7)
    new = NewSymbolStyleView(scene, 25.4, 12.7)
    for v in (old, new):
        _setup(v, zoom, (10.0, 10.0))
    full_old = _render(qapp, old)
    sparse_new = _render(qapp, new, SPARSE)
    sparse_old = _render(qapp, old, SPARSE)
    assert _inside(sparse_new, SPARSE) == _inside(full_old, SPARSE)
    assert _inside(sparse_new, SPARSE) == _inside(sparse_old, SPARSE)
    # The sparse paint did go through the region-limited path.
    assert any(r is not None and r.rectCount() > 1 for r in new.seen_regions)
    _dispose(old, new)


def test_a_dirty_region_makes_the_layout_smaller_than_the_full_one(qapp):
    scene = QGraphicsScene(-1000, -1000, 2000, 2000)
    view = NewSymbolStyleView(scene, 25.4, None)
    _setup(view, 1.0, (0.0, 0.0))
    qapp.processEvents()
    rect = QRectF(view.mapToScene(view.viewport().rect()).boundingRect())
    full = view.exposed.indices(rect, 25.4, 0)
    seen = []
    original = view.drawBackground

    def spy(painter, r):
        seen.append(len(view.exposed.indices(r, 25.4, 0)))
        original(painter, r)
    view.drawBackground = spy
    _render(qapp, view, QRegion(QRect(10, 10, 30, 30)) + QRegion(QRect(250, 150, 30, 30)))
    assert seen and max(seen) < len(full)
    _dispose(view)


# --------------------------------------------------- pixel equality, footprint style

@pytest.mark.parametrize('zoom', [0.5, 1.0, 3.0])
@pytest.mark.parametrize('origin', [(0.0, 0.0), (7.3, -3.1)])
@pytest.mark.parametrize('divisor,dots', [(4, True), (5, True), (1, False), (4, False)])
@pytest.mark.parametrize('antialias', [False, True])
def test_footprint_style_grid_is_byte_identical_to_the_legacy_view(qapp, zoom, origin, divisor, dots, antialias):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    old = LegacyFootprintView(scene, 20.0, origin, divisor, dots)
    new = NewFootprintStyleView(scene, 20.0, origin, divisor, dots)
    for v in (old, new):
        _setup(v, zoom, (5.0, 9.0), antialias)
    assert _bytes(_render(qapp, new)) == _bytes(_render(qapp, old))
    _dispose(old, new)


@pytest.mark.parametrize('origin', [(0.0, 0.0), (7.3, -3.1)])
def test_footprint_style_sparse_paint_equals_the_full_grid_inside_the_clip(qapp, origin):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    old = LegacyFootprintView(scene, 20.0, origin, 4, True)
    new = NewFootprintStyleView(scene, 20.0, origin, 4, True)
    for v in (old, new):
        _setup(v, 2.0, (5.0, 9.0))
    full_old = _render(qapp, old)
    sparse_new = _render(qapp, new, SPARSE)
    assert _inside(sparse_new, SPARSE) == _inside(full_old, SPARSE)
    _dispose(old, new)


class ConfigurableView(QGraphicsView):
    """A view painting the grid with arbitrary `paint_grid` keywords."""

    def __init__(self, scene, **kwargs):
        super().__init__(scene)
        self.kwargs = kwargs

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        paint_grid(painter, rect, exposed=None, **self.kwargs)


def _picture(qapp, **kwargs):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    view = ConfigurableView(scene, **kwargs)
    _setup(view, 3.0, (0.0, 0.0))
    data = _bytes(_render(qapp, view))
    _dispose(view)
    return data


def test_skip_dots_on_lines_changes_the_pixels(qapp):
    # Negative twin: with the skip off the dots sit on the main lines, so the picture differs.
    style = GridStyle(QColor(LINE), QColor(DOT), 3.0, None)
    skipped = _picture(qapp, step=20.0, style=style, fine_step=5.0, skip_dots_on_lines=True)
    kept = _picture(qapp, step=20.0, style=style, fine_step=5.0, skip_dots_on_lines=False)
    assert skipped != kept


def test_skip_does_nothing_when_the_fine_step_does_not_divide_the_step(qapp):
    style = GridStyle(QColor(LINE), QColor(DOT), 3.0, None)
    assert (_picture(qapp, step=20.0, style=style, fine_step=7.0, skip_dots_on_lines=True)
            == _picture(qapp, step=20.0, style=style, fine_step=7.0, skip_dots_on_lines=False))


def test_dot_order_changes_the_pixels_where_a_dot_meets_a_line(qapp):
    style = GridStyle(QColor(LINE), QColor(DOT), 3.0, False)
    assert (_picture(qapp, step=20.0, style=style, fine_step=10.0, dots_first=True)
            != _picture(qapp, step=20.0, style=style, fine_step=10.0, dots_first=False))


def test_painter_state_is_restored(qapp):
    image = QImage(50, 50, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor('#123456'))
    painter.setPen(pen)
    paint_grid(painter, QRectF(0, 0, 50, 50), exposed=None, step=10.0,
               style=GridStyle(QColor(LINE), QColor(DOT), 1.5, False), fine_step=5.0)
    assert painter.testRenderHint(QPainter.RenderHint.Antialiasing)
    assert painter.pen().color() == QColor('#123456')
    painter.end()


def test_no_dots_without_a_fine_step_or_a_dot_colour(qapp):
    def drawn(style, fine):
        image = QImage(60, 60, QImage.Format.Format_ARGB32)
        image.fill(QColor('#ffffff'))
        painter = QPainter(image)
        paint_grid(painter, QRectF(0, 0, 60, 60), exposed=None, step=20.0, style=style, fine_step=fine)
        painter.end()
        return _bytes(image)

    lines_only = drawn(GridStyle(QColor(LINE), None), 5.0)
    assert lines_only == drawn(GridStyle(QColor(LINE), QColor(DOT)), None)
    assert lines_only != drawn(GridStyle(QColor(LINE), QColor(DOT)), 5.0)


# --------------------------------------------------------------- ExposedRegion

def test_region_is_seen_inside_drawBackground_and_cleared_after(qapp):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    view = NewSymbolStyleView(scene, 25.4, None)
    _setup(view, 1.0, (0.0, 0.0))
    qapp.processEvents()
    assert view.exposed.region is None
    view.seen_regions.clear()
    _render(qapp, view, SPARSE)
    assert view.seen_regions                    # drawBackground ran...
    assert all(r is not None for r in view.seen_regions)    # ...with the region ALREADY recorded
    assert any(r.rectCount() > 1 for r in view.seen_regions)    # Qt may split the 3 rects into bands
    assert view.exposed.region is None          # ...and it is gone afterwards
    _dispose(view)


def test_region_is_none_outside_a_paint_event(qapp):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    view = NewSymbolStyleView(scene, 25.4, None)
    _setup(view, 1.0, (0.0, 0.0))
    qapp.processEvents()
    view.seen_regions.clear()
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    view.scene().render(painter)                # scene render: no viewport paint event
    painter.end()
    view.grab()
    assert all(r is None or r.rectCount() == 1 for r in view.seen_regions)
    assert view.exposed.region is None
    _dispose(view)


def test_the_view_paints_once_per_event_and_events_are_not_swallowed(qapp):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    calls = []

    class Counting(QGraphicsView):
        def paintEvent(self, event):
            calls.append(event.region().boundingRect())
            super().paintEvent(event)

    view = Counting(scene)
    ExposedRegion(view)
    _setup(view, 1.0, (0.0, 0.0))
    qapp.processEvents()
    calls.clear()
    _render(qapp, view, QRegion(QRect(0, 0, 30, 30)))
    assert len(calls) == 1 and calls[0] == QRect(0, 0, 30, 30)
    _dispose(view)


def _indices_fixture(qapp):
    scene = QGraphicsScene(-1000, -1000, 2000, 2000)
    view = QGraphicsView(scene)
    view.resize(300, 200)
    view.show()
    qapp.processEvents()
    return view, ExposedRegion(view)


def test_indices_full_range_outside_a_paint(qapp):
    view, exposed = _indices_fixture(qapp)
    rect = QRectF(-100, -50, 200, 100)
    assert exposed.indices(rect, 25.0, 0) == list(range(-4, 5))
    assert exposed.indices(rect, 25.0, 1) == list(range(-2, 3))
    assert exposed.indices(rect, 25.0, 0, origin=10.0) == list(range(math.floor(-110 / 25), math.ceil(90 / 25) + 1))
    _dispose(view)


def test_indices_empty_and_whole_viewport_region_give_the_full_range(qapp):
    view, exposed = _indices_fixture(qapp)
    rect = QRectF(-100, -50, 200, 100)
    full = exposed.indices(rect, 25.0, 0)
    exposed.region = QRegion()
    assert exposed.indices(rect, 25.0, 0) == full
    exposed.region = QRegion(view.viewport().rect())
    assert exposed.indices(rect, 25.0, 0) == full
    exposed.region = QRegion(view.viewport().rect().adjusted(-10, -10, 10, 10))
    assert exposed.indices(rect, 25.0, 0) == full
    _dispose(view)


def test_indices_partial_region_keeps_only_the_lines_that_reach_it(qapp):
    view, exposed = _indices_fixture(qapp)
    rect = QRectF(view.mapToScene(view.viewport().rect()).boundingRect())
    full = exposed.indices(rect, 25.0, 0)
    exposed.region = QRegion(QRect(100, 0, 20, 20))
    kept = exposed.indices(rect, 25.0, 0)
    assert 0 < len(kept) < len(full) and set(kept) <= set(full)
    for i in kept:                              # every kept line is within the region grown by the margin
        x = view.mapFromScene(QPointF(i * 25.0, 0)).x()
        assert 100 - 2 - 1 <= x <= 120 + 2 + 1
    exposed.region = None
    _dispose(view)


def test_indices_margin_decides_the_lines_just_outside_the_region(qapp):
    view, exposed = _indices_fixture(qapp)
    rect = QRectF(view.mapToScene(view.viewport().rect()).boundingRect())
    exposed.region = QRegion(QRect(100, 0, 20, 20)) + QRegion(QRect(250, 150, 10, 10))
    with_margin = exposed.indices(rect, 25.0, 0, margin_px=40)
    without = exposed.indices(rect, 25.0, 0, margin_px=0)
    assert set(without) < set(with_margin)
    exposed.region = None
    _dispose(view)


def test_indices_respect_the_axis(qapp):
    view, exposed = _indices_fixture(qapp)
    rect = QRectF(view.mapToScene(view.viewport().rect()).boundingRect())
    exposed.region = QRegion(QRect(10, 10, 10, 10)) + QRegion(QRect(260, 160, 10, 10))
    cols, rows = exposed.indices(rect, 25.0, 0), exposed.indices(rect, 25.0, 1)
    assert cols and rows and cols != rows
    exposed.region = None
    _dispose(view)


# ----------------------------------------------------------------- dots_legible

@pytest.mark.parametrize('fine,scale,minimum,expected', [
    (5.0, 1.0, 5.0, True),                     # exactly at the threshold
    (4.99, 1.0, 5.0, False),                   # just below
    (2.0, 3.0, 5.0, True),
    (2.0, 2.4, 5.0, False),
    (10.0, 0.5, 5.0, True),
    (10.0, 0.49, 5.0, False),
])
def test_dots_legible_rule(fine, scale, minimum, expected):
    assert dots_legible(fine, scale, minimum) is expected


class NestingView(QGraphicsView):
    """Grabs its own viewport from inside `drawBackground` during a partial paint."""

    def __init__(self, scene):
        super().__init__(scene)
        self.exposed = ExposedRegion(self)
        self.nested = None
        self.nested_region = None
        self.outer_after_nested = None
        self.armed = False
        self.grabbing = False

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if self.armed:
            self.armed = False
            outer = QRegion(self.exposed.region)
            self.grabbing = True
            self.nested = self.viewport().grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
            self.grabbing = False
            self.outer_after_nested = (outer, QRegion(self.exposed.region))
        elif self.grabbing:
            self.nested_region = QRegion(self.exposed.region)     # the nested paint's own region
        paint_grid(painter, rect, exposed=self.exposed, step=25.4, style=GridStyle(QColor(LINE)))


def _nesting_view(qapp):
    scene = QGraphicsScene(-500, -500, 1000, 1000)
    view = NestingView(scene)
    _setup(view, 1.0, (0.0, 0.0))
    qapp.processEvents()
    return view


def test_a_nested_grab_during_a_partial_paint_equals_a_full_grab(qapp):
    view = _nesting_view(qapp)
    full = view.viewport().grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
    view.armed = True
    _render(qapp, view, QRegion(QRect(5, 5, 5, 5)))
    assert view.nested is not None
    assert _bytes(view.nested) == _bytes(full)
    assert view.nested_region is not None and view.nested_region.boundingRect().contains(view.viewport().rect())
    assert view.exposed.region is None                      # nothing stale after the outer paint
    _dispose(view)


def test_the_outer_region_is_still_partial_after_the_nested_paint_returns(qapp):
    view = _nesting_view(qapp)
    view.armed = True
    _render(qapp, view, QRegion(QRect(5, 5, 5, 5)))
    outer_before, outer_after = view.outer_after_nested
    assert outer_before.boundingRect().width() < view.viewport().width()
    assert outer_after == outer_before
    _dispose(view)
