"""@brief Shared background grid of the symbol editor and the footprint preview.

Both views paint a grid of cosmetic lines plus optional dots in
`drawBackground`; the layout and the painting live here (plan
`docs/canvas-camera-grid-plan.md`, section 2):

- `ExposedRegion`: the region Qt is about to paint, so only the grid that reaches it is laid out.
- `GridStyle`: colours and dot pen of a grid.
- `paint_grid`: the lines and the dots, pixel-identical to the two legacy implementations.
- `dots_legible`: the shared legibility rule of the fine dots.

Each view keeps its grid MODEL (steps, origin, divisor, visibility) and passes
the numbers. The module is Qt-only: no model, no tab, no translated string.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QLineF, QObject, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF, QRegion
from PyQt6.QtWidgets import QApplication, QGraphicsView

#: Device pixels added around a grid line before it is tested against the paint region.
GRID_PAINT_MARGIN_PX = 2


class ExposedRegion(QObject):
    """@brief The region of the paint event being handled, available to `drawBackground`.

    @note Qt hands `drawBackground` only the BOUNDING rectangle of the exposed region
        and clips the pixels to the region itself; a few scattered dirty rectangles
        therefore make the grid be laid out over most of the viewport for a handful of
        visible pixels. The region is recorded here instead.
        HOOK: an event filter on the view's viewport. A filter runs before the
        viewport's own event handling (the scroll area's internal filter, which calls
        `paintEvent`), so on the first `Paint` event it records `event.region()`, re-sends
        the same event through `QApplication.sendEvent` (guarded against re-entry, so
        the other filters and the view's `paintEvent` run exactly once), clears the
        region when that returns and consumes the original delivery. The host
        subclasses nothing and overrides nothing. Outside a paint `region` is None;
        `viewport().grab()` and `QWidget.render` DO send Paint events through the filter
        (region = the whole viewport, which `indices` lays out in full), while
        `QGraphicsView.render` / `scene.render` send none. tests/test_canvas_grid.py proves the region is visible inside
        `drawBackground` and gone afterwards.
    """

    def __init__(self, view: QGraphicsView):
        """@brief Install the paint hook on the view's viewport.

        @param view The graphics view; also the Qt parent.
        """
        super().__init__(view)
        self._view = view
        #: The paint event's region while a paint runs, None otherwise.
        self.region: QRegion | None = None
        #: Addresses of the Paint events being re-sent (one per nesting level).
        self._inflight: set[int] = set()
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        """@brief Qt override: keep the paint region while the viewport paints.

        @param watched The viewport.
        @param event The event.
        @return True only for the original delivery of a Paint event (the re-sent copy
            does the painting); False for everything else.
        """
        if event.type() != QEvent.Type.Paint or watched is not self._view.viewport():
            return False
        address = sip.unwrapinstance(event)
        if address in self._inflight:           # the re-sent copy of THIS event: let it through
            return False
        previous = self.region                  # a paint nested in a paint keeps its own region
        self.region = QRegion(event.region())
        self._inflight.add(address)
        try:
            QApplication.sendEvent(watched, event)
        finally:
            self._inflight.discard(address)
            self.region = previous
        return True

    def indices(self, rect: QRectF, step: float, axis: int, origin: float = 0.0, margin_px: int = GRID_PAINT_MARGIN_PX) -> list[int]:
        """@brief Indices i of the grid positions `origin + i * step` inside `rect` that reach the region being painted.

        @param rect Exposed scene rectangle handed to `drawBackground`.
        @param step Grid step, scene units (> 0).
        @param axis 0 for x (columns, vertical lines), 1 for y (rows, horizontal lines).
        @param origin Scene coordinate of the grid node 0 along `axis`.
        @param margin_px Device pixels the line is grown by before the test.
        @return The full range (floor of the low edge to ceil of the high edge) outside a
            paint event, for an empty region, or when the region is the whole viewport;
            otherwise only the indices whose line, grown by `margin_px`, intersects the
            region (`QRegion.intersects`: PyQt6 gives no access to the region's rectangles).
        @note A line or dot that reaches no part of the region has no pixel inside the area
            Qt clips to, so leaving it out changes no pixel.
        """
        low, high = (rect.left(), rect.right()) if axis == 0 else (rect.top(), rect.bottom())
        full = range(math.floor((low - origin) / step), math.ceil((high - origin) / step) + 1)
        region = self.region
        if region is None or region.isEmpty() or (
                region.rectCount() == 1 and region.boundingRect().contains(self._view.viewport().rect())):
            return list(full)
        transform = self._view.viewportTransform()
        kept = []
        for index in full:
            at = origin + index * step
            line = (QRectF(at, rect.top(), 0.0, rect.height()) if axis == 0
                    else QRectF(rect.left(), at, rect.width(), 0.0))
            strip = transform.mapRect(line).toAlignedRect().adjusted(-margin_px, -margin_px, margin_px, margin_px)
            if region.intersects(strip):
                kept.append(index)
        return kept


@dataclass
class GridStyle:
    """@brief Colours and dot pen of a grid."""

    #: Colour of the cosmetic 1 px grid lines.
    line_color: QColor
    #: Colour of the fine dots; None draws no dots.
    dot_color: QColor | None = None
    #: Width, device px, of the round-cap cosmetic dot pen.
    dot_width: float = 1.5
    #: True/False forces the Antialiasing hint while the dots are drawn (restored after);
    #: None leaves the painter's hint untouched.
    dots_antialiased: bool | None = False


def dots_legible(fine_step: float, scale: float, min_px: float) -> bool:
    """@brief Whether fine dots `fine_step` apart (scene units) are legible at `scale`.

    @param fine_step Distance between two dots, scene units.
    @param scale View scale, device pixels per scene unit.
    @param min_px Smallest legible on-screen distance, device pixels.
    @return True when `fine_step * scale >= min_px`.
    """
    return fine_step * scale >= min_px


def paint_grid(painter: QPainter, rect: QRectF, *, exposed: ExposedRegion | None, step: float,
               origin: tuple[float, float] = (0.0, 0.0), style: GridStyle, fine_step: float | None = None,
               skip_dots_on_lines: bool = False, dots_first: bool = False) -> None:
    """@brief Paint the grid lines and the fine dots over `rect`.

    @param painter Painter in scene coordinates; its state is saved and restored.
    @param rect Exposed scene rectangle; every line spans it.
    @param exposed The region hook limiting the layout to what reaches the paint; None lays out the whole `rect`.
    @param step Distance between lines, scene units (> 0).
    @param origin Scene coordinates (x, y) of the line crossing the grid is anchored to.
    @param style Colours and dot pen.
    @param fine_step Distance between dots, scene units; None (or no `style.dot_color`) draws no dots.
        The caller applies its own legibility rule (`dots_legible`) before passing it.
    @param skip_dots_on_lines True skips the dots on a main line (the node index is a multiple of
        `round(step / fine_step)`); needs `fine_step` to divide `step`.
    @param dots_first True paints the dots BEFORE the lines (the symbol editor: a line crosses
        its dots); False paints them after (the footprint preview).
    @note Lines are cosmetic 1 px pens drawn in index order, columns then rows. Dots are one
        `drawPoints` call, columns outer and rows inner. With a paint region only the
        lines, columns and rows reaching it are laid out; the dots are a superset of those
        inside it. The pixels inside the clip equal those of the full grid.
    """
    ox, oy = origin
    painter.save()
    try:
        if dots_first:
            _paint_dots(painter, rect, exposed, step, ox, oy, style, fine_step, skip_dots_on_lines)
        pen = QPen(QColor(style.line_color))
        pen.setCosmetic(True)
        painter.setPen(pen)
        for index in _indices(exposed, rect, step, 0, ox):
            x = ox + index * step
            painter.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
        for index in _indices(exposed, rect, step, 1, oy):
            y = oy + index * step
            painter.drawLine(QLineF(rect.left(), y, rect.right(), y))
        if not dots_first:
            _paint_dots(painter, rect, exposed, step, ox, oy, style, fine_step, skip_dots_on_lines)
    finally:
        painter.restore()


def _indices(exposed: ExposedRegion | None, rect: QRectF, step: float, axis: int, origin: float) -> list[int]:
    """The region-limited indices, or the full range when there is no hook."""
    if exposed is not None:
        return exposed.indices(rect, step, axis, origin)
    low, high = (rect.left(), rect.right()) if axis == 0 else (rect.top(), rect.bottom())
    return list(range(math.floor((low - origin) / step), math.ceil((high - origin) / step) + 1))


def _paint_dots(painter, rect, exposed, step, ox, oy, style, fine_step, skip_on_lines) -> None:
    """Draw the fine dots with a round-cap cosmetic pen (nothing when not requested)."""
    if fine_step is None or style.dot_color is None or fine_step <= 0:
        return
    per_step = round(step / fine_step)
    skip = skip_on_lines and per_step >= 1 and math.isclose(per_step * fine_step, step, rel_tol=1e-9)
    columns = [i for i in _indices(exposed, rect, fine_step, 0, ox) if not (skip and i % per_step == 0)]
    rows = [j for j in _indices(exposed, rect, fine_step, 1, oy) if not (skip and j % per_step == 0)]
    ys = [oy + j * fine_step for j in rows]
    dots = QPolygonF([QPointF(ox + i * fine_step, y) for i in columns for y in ys])
    pen = QPen(QColor(style.dot_color), style.dot_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    pen.setCosmetic(True)
    previous = painter.testRenderHint(QPainter.RenderHint.Antialiasing)
    if style.dots_antialiased is not None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, bool(style.dots_antialiased))
    painter.setPen(pen)
    painter.drawPoints(dots)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, previous)
