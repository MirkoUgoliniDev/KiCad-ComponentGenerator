"""@brief Hit shapes of drawn outlines shared by the symbol editor and the footprint preview.

KiCad hits an UNFILLED graphic only near its outline and a FILLED one also
inside (`EDA_SHAPE::hitTest`, common/eda_shape.cpp: a circle by
``abs(radius - dist) <= maxdist`` unless filled, a rectangle by its four
sides unless filled, a polygon by ``CollideEdge`` unless filled, a segment or
a Bezier by ``TestSegmentHit``; ``maxdist = width / 2 + accuracy``). Qt's
default `QGraphicsItem.shape()` of a rectangle, ellipse, polygon or path
item holds the interior instead, and for an OPEN path also the area closed
by its chord, so a click inside an empty rectangle, or inside the bend of an
«L», took the element and hid what lies below it.

The items here return the KiCad hit as their Qt shape: the outline stroked
at the pen width (the ``width / 2`` term, round caps and joins as a distance
test gives), united with the interior only when the element is declared
filled (`set_interior_hit`). Everything Qt derives from the shape follows:
`QGraphicsView.items()` (the presses, right clicks, double clicks and
Ctrl+clicks of both tabs), the hover cursor and the tooltip. The
``accuracy`` term is the tab's own screen-pixel query margin
(`canvas_menus.CONTEXT_HIT_MARGIN_PX`), not part of the scene shape: a shape
cannot depend on the zoom without invalidating the scene index.

The interior is declared, never inferred from the brush: the symbol editor
paints an unfilled shape with a transparent SOLID brush, and a keepout zone
is hatched over its area but selected by its border.
"""
from __future__ import annotations

from typing import Callable, NamedTuple, Optional

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QCursor, QPainterPath, QPainterPathStroker
from PyQt6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem, QGraphicsPathItem, QGraphicsPolygonItem,
                             QGraphicsRectItem, QToolTip)

#: Narrowest hit stroke, item units: a zero-width (cosmetic) pen still has a
#: hittable outline instead of an empty shape.
MIN_HIT_STROKE = 1e-3


def outline_hit_shape(outline: QPainterPath, pen_width: float, interior: bool) -> QPainterPath:
    """@brief The KiCad hit area of an outline: its stroke, plus the inside when filled.

    @param outline The drawn geometry in item units (open or closed subpaths).
    @param pen_width The stroke width in item units; below MIN_HIT_STROKE the
        stroke is MIN_HIT_STROKE wide.
    @param interior True for a filled element: the area the outline encloses
        (an open path closed by its chord, as Qt and KiCad fill it) is hit too.
    @return A QPainterPath holding every point within pen_width / 2 of the
        outline (round caps and joins: a distance test), and the interior
        when asked.
    """
    stroker = QPainterPathStroker()
    stroker.setWidth(max(float(pen_width), MIN_HIT_STROKE))
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    shape = stroker.createStroke(outline)
    if interior:
        filled = QPainterPath(outline)
        filled.setFillRule(Qt.FillRule.WindingFill)
        # A boolean union: adding the subpaths instead could cancel windings
        # where the stroke and the outline turn opposite ways.
        shape = shape.united(filled)
    return shape


def items_near(view, pos, margin_px: int) -> list:
    """@brief The items within `margin_px` screen pixels of a viewport point, topmost first.

    @param view A QGraphicsView.
    @param pos Viewport point (QPoint).
    @param margin_px Half side of the square query, screen pixels at any zoom.
    @return `view.items()` of the square around the point: each item's Qt
        shape (the outline hit of the classes here) is tested against it, so
        this is the ``accuracy`` term of KiCad's hit test.
    """
    m = int(margin_px)
    return view.items(QRect(pos.x() - m, pos.y() - m, 2 * m + 1, 2 * m + 1))


#: Precedence of the targets of a left press, a right click, the Ctrl+click
#: toggle and the hover feedback, the same in both tabs (coordinator
#: decisions, 2026-11-09): the lower rank wins inside one pass. A drag handle
#: is the smallest, most deliberate target; a text (Reference/Value, a
#: property) comes next because it is a small target laid ON TOP of the
#: component, so a pad or a pin under it would otherwise make it unreachable
#: (verification 2026-11-09, D2: a Value moved over a 5 mm pad could be
#: neither dragged nor reset); a pin (symbol) or a pad (footprint), the part
#: of the component the element is drawn around, follows; the stroke of a
#: drawn element last.
RANK_HANDLE = 0
#: A property or footprint text (Reference/Value, its anchor square).
RANK_TEXT = 1
#: A symbol pin or a footprint pad.
RANK_PIN = 2
#: The stroke (or the filled inside) of a drawn element.
RANK_STROKE = 3


class Pick(NamedTuple):
    """@brief What a press at a viewport point takes: the target, its item and the pass that found it."""

    #: The caller's target value (`classify` result).
    target: object
    #: The scene item that carries it.
    item: QGraphicsItem
    #: True when found under the point exactly, False when within the margin only.
    exact: bool


def pick(view, pos, classify: Callable[[QGraphicsItem], Optional[tuple]], margin_px: int) -> Optional[Pick]:
    """@brief The target a press at a viewport point takes: exact pass first, then the margin pass, by rank.

    @param view A QGraphicsView.
    @param pos Viewport point (QPoint).
    @param classify Maps an item to ``(rank, target)`` (RANK_HANDLE ...
        RANK_STROKE, or any int: lower wins) or None for an item that is no
        target of this query (a view filters kinds here, e.g. the Ctrl+click
        toggle leaves texts out).
    @param margin_px Half side of the margin pass's square, screen pixels
        (`canvas_menus.CONTEXT_HIT_MARGIN_PX` in both tabs).
    @return The best `Pick`, or None when nothing is within the margin.
    @note ONE precedence for both tabs and for the left press, the right
        click and the hover (coordinator decision, 2026-11-09): the items
        exactly under the point are looked at first, and only when none is a
        target the items within the margin (KiCad's ``accuracy`` term of
        `EDA_SHAPE::hitTest`); inside a pass the lowest rank wins and, among
        equal ranks, the topmost item. So a stroke exactly under the pointer
        beats a pin two pixels away, a pin two pixels away beats a stroke
        three pixels away, and a text exactly over a pad beats the pad.
    """
    for exact, items in ((True, view.items(pos)), (False, items_near(view, pos, margin_px))):
        best = None
        for item in items:
            found = classify(item)
            if found is not None and (best is None or found[0] < best[0]):
                best = (found[0], found[1], item)
        if best is not None:
            return Pick(best[1], best[2], exact)
    return None


class ReachFeedback:
    """@brief Hover cursor and tooltip of the item a press would take, out to the press margin.

    Qt shows the cursor and the tooltip of the item EXACTLY under the
    pointer; a press takes a stroke up to the margin (`pick`). A view feeds
    its idle mouse moves here so that, three pixels off a thin stroke, the
    move cursor shows where the press takes it, and beyond the margin the
    arrow comes back (coordinator decision, 2026-11-09).

    @note Only for an IDLE view (no armed click tool, placement, pan, drag
        or rubber band): those own the cursor themselves, and the view calls
        `release` when one of them begins. A cursor set here is undone
        before the next base mouse move, so `QGraphicsView` never stores it
        as the "original" cursor of an item it enters.
    """

    def __init__(self, view):
        """@brief Bind the feedback to a view.

        @param view The QGraphicsView whose viewport cursor is driven.
        """
        self._view = view
        #: True while the viewport shows a cursor set here (not by Qt).
        self._own = False

    def before_move(self) -> None:
        """@brief Undo a cursor set here, BEFORE the view's base mouse move runs."""
        self.release()

    def release(self) -> None:
        """@brief Undo a cursor set here, when the view stops being idle.

        @note Called by each view whenever an interaction takes the pointer
            (`cancel_interaction`, which also runs before a glyph placement
            or a click tool is armed, and before a drag begins): the hover
            feedback only runs in an idle view, so without this the move
            cursor of the last hovered item stayed on the viewport through
            the whole placement (verification 2026-11-09, D3). A cursor the
            next owner sets afterwards (the tool's cross, the drag's cursor)
            is never touched.
        """
        if self._own:
            self._own = False
            self._view.viewport().unsetCursor()

    def after_move(self, item) -> None:
        """@brief Show the cursor of the item the press would take, AFTER the view's base mouse move.

        @param item `Pick.item` of the press query at the pointer, or None.
        @note Nothing is done when the item has no cursor or the viewport
            already shows it (Qt set it for an exact hit).
        """
        if item is None or not item.hasCursor():
            return
        viewport = self._view.viewport()
        cursor = item.cursor()
        if viewport.cursor().shape() != cursor.shape():
            viewport.setCursor(cursor)
            self._own = True

    def refresh(self, item, pos=None) -> None:
        """@brief Recompute the cursor and an open tooltip for a still pointer, after a camera change.

        @param item `Pick.item` of the press query at the pointer NOW, or None
            (nothing within the margin, or the pointer is not over the view).
        @param pos Viewport point of the pointer (QPoint) where an open
            tooltip is shown again, or None to only hide it.
        @note Only for an IDLE view, as the mouse moves (the views call it
            from their idle camera refresh, VER5 E): a zoom, a fit or a scroll
            moves the scene under a still pointer, and without a move neither
            Qt nor `after_move` re-ran, so the move cursor and the tooltip of
            an item the camera carried away stayed. In an idle view the
            viewport's cursor is only ever an item's (Qt's or the one set
            here), so it is unset when nothing with a cursor lies under the
            pointer now; Qt restores its own stored cursor on the next move.
        """
        viewport = self._view.viewport()
        self.release()
        if item is not None and item.hasCursor():
            self.after_move(item)
        elif viewport.testAttribute(Qt.WidgetAttribute.WA_SetCursor):
            viewport.unsetCursor()
        if QToolTip.isVisible():
            text = item.toolTip() if item is not None else ""
            if text and pos is not None:
                QToolTip.showText(viewport.mapToGlobal(pos), text, viewport)
            else:
                QToolTip.hideText()

    @staticmethod
    def show_tooltip(event, item) -> bool:
        """@brief Show the tooltip of the item a press would take, for a viewport ToolTip event.

        @param event The QHelpEvent of the viewport.
        @param item `Pick.item` of the press query at the event's point, or None.
        @return True when a tooltip was shown here (the view returns True from
            `viewportEvent`); False to let Qt answer (no target with a tooltip:
            Qt's exact query, which beyond the margin finds nothing and hides
            the tooltip).
        """
        text = item.toolTip() if item is not None else ""
        if not text:
            return False
        QToolTip.showText(event.globalPos(), text)
        return True


class PointerTracker:
    """@brief Where the pointer is over a view, valid across camera AND layout changes without a mouse move.

    Shared by the symbol editor and the footprint preview (review 2026-11-09,
    points 1 and 3): the keyboard commands that act on "what lies under the
    pointer" (E, and the footprint's Delete, R, Ctrl+D, Ctrl+C and arrows
    with nothing selected), the coordinate readout and the hover handles
    read the pointer HERE, never a stored scene point.

    @note The physical pointer does not move when the camera does (an
        auto-fit after a delete, a scroll, a programmatic zoom) nor when the
        layout moves or resizes the viewport (a side panel expanding): only a
        SCREEN point is invariant under both. So each move stores the
        viewport point mapped to the screen (`QWidget.mapToGlobal` of the
        event's viewport point, never the event's own global position, which
        Qt fills with `QCursor.pos()` for an event built without one), and a
        query maps it back through the viewport's CURRENT geometry and the
        caller maps that through the CURRENT camera.
    @note When the last move was a real one (the platform cursor stood on
        the event's point, within `LIVE_TOLERANCE_PX`) and the viewport is
        still under the mouse, the platform cursor is read instead
        (`QCursor.pos()`), which also follows a move whose event never came.
        A synthetic move (tests, automation) does not match the platform
        cursor, so its stored point stays the answer.
    """

    #: Screen pixels between the platform cursor and a move's point within
    #: which the move counts as real (scaling may round a pixel each way).
    LIVE_TOLERANCE_PX = 2

    def __init__(self, view):
        """@brief Bind the tracker to a view; no pointer is known until the first move.

        @param view The QGraphicsView whose viewport the pointer moves over.
        """
        self._view = view
        #: Screen point (QPoint) of the last move inside the view, None once it left.
        self._screen = None
        #: True when the last move matched the platform cursor (a real move).
        self._live = False

    def moved(self, pos) -> None:
        """@brief Record a mouse move at a viewport point.

        @param pos Viewport point of the move (QPoint or QPointF).
        """
        viewport = self._view.viewport()
        screen = viewport.mapToGlobal(QPoint(round(pos.x()), round(pos.y())))
        self._screen = screen
        cursor = QCursor.pos()
        self._live = (viewport.underMouse()
                      and (cursor - screen).manhattanLength() <= self.LIVE_TOLERANCE_PX)

    def left(self) -> None:
        """@brief The pointer left the view: nothing is under it until the next move."""
        self._screen = None
        self._live = False

    def viewport_point(self) -> Optional[QPoint]:
        """@brief The pointer's viewport point NOW, through the viewport's current geometry.

        @return The viewport point (QPoint), or None when no move was seen
            since the pointer last left, or the point now falls outside the
            viewport (the layout moved the viewport away from under it).
        """
        if self._screen is None:
            return None
        viewport = self._view.viewport()
        screen = QCursor.pos() if (self._live and viewport.underMouse()) else self._screen
        point = viewport.mapFromGlobal(screen)
        return point if viewport.rect().contains(point) else None

    def scene_point(self):
        """@brief The scene point under the pointer NOW, through the current camera.

        @return QPointF, or None when `viewport_point` is None.
        """
        point = self.viewport_point()
        return None if point is None else self._view.mapToScene(point)


class OutlineHitMixin:
    """@brief Mixin giving a graphics item KiCad's hit: outline only, interior when declared filled.

    @note Put it BEFORE the Qt item class (``class X(OutlineHitMixin,
        QGraphicsRectItem)``) so its `shape` overrides Qt's. The subclass
        provides `outline_path`. The result is cached on the outline, the pen
        width and the interior flag, because Qt asks for the shape on every
        hover move.
    @note The pen must be NON-cosmetic (QPen's default for a width above 0):
        the hit stroke is `pen().widthF()` in ITEM units, while a cosmetic pen
        draws that width in screen pixels, so its hit band would grow and
        shrink with the zoom against what is painted. A cosmetic or
        zero-width pen gets the MIN_HIT_STROKE band; the screen-pixel reach
        comes from the query margin (`items_near`, `pick`), never from the pen.
    """

    _interior_hit = False
    _hit_cache = None

    def set_interior_hit(self, filled: bool) -> None:
        """@brief Declare whether the element is filled (hit inside) or not (hit on its outline only).

        @param filled True for a filled element.
        """
        self._interior_hit = bool(filled)

    def interior_hit(self) -> bool:
        """@brief Whether a point inside the outline hits the element.

        @return The flag given to `set_interior_hit` (False by default).
        """
        return self._interior_hit

    def outline_path(self) -> QPainterPath:
        """@brief The drawn geometry as a path in item units; provided by the subclass.

        @return The outline path.
        @throws NotImplementedError When the subclass does not provide it.
        """
        raise NotImplementedError

    def shape(self) -> QPainterPath:
        """@brief Qt override: the outline stroked at the pen width, united with the inside when filled.

        @return The hit area (`outline_hit_shape`).
        """
        outline = self.outline_path()
        width = self.pen().widthF()
        cache = self._hit_cache
        if cache is not None and cache[0] == width and cache[1] == self._interior_hit and cache[2] == outline:
            return cache[3]
        shape = outline_hit_shape(outline, width, self._interior_hit)
        self._hit_cache = (width, self._interior_hit, outline, shape)
        return shape

    def contains(self, point) -> bool:
        """@brief Qt override: a point is on the element when it lies in its hit shape.

        @param point Item-coordinate point (QPointF).
        @return True on the outline stroke, or inside a filled element.
        """
        return self.shape().contains(point)


class OutlineRectItem(OutlineHitMixin, QGraphicsRectItem):
    """@brief A rectangle hit on its four sides, and inside only when filled."""

    def outline_path(self) -> QPainterPath:
        """@brief The rectangle as a closed path.

        @return Path of `rect()`.
        """
        path = QPainterPath()
        path.addRect(self.rect())
        return path


class OutlineEllipseItem(OutlineHitMixin, QGraphicsEllipseItem):
    """@brief A circle or ellipse hit on its rim (a ring), and inside only when filled."""

    def outline_path(self) -> QPainterPath:
        """@brief The ellipse as a closed path.

        @return Path of the ellipse in `rect()`.
        """
        path = QPainterPath()
        path.addEllipse(self.rect())
        return path


class OutlinePolygonItem(OutlineHitMixin, QGraphicsPolygonItem):
    """@brief A closed polygon hit on its sides (the closing side included), and inside only when filled."""

    def outline_path(self) -> QPainterPath:
        """@brief The polygon as a closed path.

        @return Path of `polygon()` with its closing side.
        """
        path = QPainterPath()
        path.addPolygon(self.polygon())
        path.closeSubpath()
        return path


class OutlinePathItem(OutlineHitMixin, QGraphicsPathItem):
    """@brief A polyline, arc or curve hit along its stroke; an open path is never hit across its chord unless filled."""

    def outline_path(self) -> QPainterPath:
        """@brief The path as drawn.

        @return `path()`.
        """
        return self.path()
