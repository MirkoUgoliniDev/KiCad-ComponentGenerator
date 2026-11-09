"""@brief Footprint navigation: camera changes never modify geometry; texts and drawing handles are dragged through the shared controller.

The drags (a Reference/Value text live, a drawing point or a whole drawing
stepped on the grid) run through `canvas_gestures.GestureController`, the
same state machine as the symbol editor's: the view only decides WHAT was
pressed, the window (the `GestureHost`) snaps and commits. Escape and Hide
cancel through `CancelOnEscapeAndHide`; the text anchor squares are hover
groups (`HoverGroups`) with the symbol editor's reveal margin. The draw
tools collect their points through `canvas_clicktools` (a `PointCollector`
driven by a `ClickToolRouter`), the same state machine as the symbol
editor's wire and shape tools: the window is the `ClickToolHost`.
"""
from PyQt6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QPainterPath, QPainterPathStroker
from PyQt6.QtWidgets import (QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsPolygonItem, QGraphicsRectItem,
                             QGraphicsView)

from .canvas_camera import Camera
from .canvas_clicktools import ClickToolRouter, PointCollector, ToolSpec
from .canvas_menus import CONTEXT_HIT_MARGIN_PX
from .canvas_grid import DARK_GRID_DOT_COLOR, ExposedRegion, GridStyle, dots_legible, paint_grid
from .canvas_origin import paint_origin_marker
from .canvas_gestures import (
    HOVER_MARGIN_PX, CancelOnEscapeAndHide, GestureController, HoverGroups, KeyRepeatFilter,
    install_canvas_filters,
)
from .canvas_selection import RubberBand, is_click

#: Item data role carrying the key of a draggable text (Reference, Value).
TEXT_KEY_ROLE = 0x544558

#: Item data role carrying the index of a user drawing of the footprint.
DRAWING_KEY_ROLE = 0x445257

#: Item data role carrying the KIND of the record a drawing item was drawn
#: from («line», «text»...): a restyle skips an item whose kind no longer
#: matches the record at its index (a list replaced without a redraw), so a
#: stale scene is never styled with another kind's rules (review B3).
DRAWING_KIND_ROLE = 0x44524B

#: Item data role of a drawing handle: "<drawing index>:<point index>";
#: point index -1 means the WHOLE drawing (a press on its body).
HANDLE_KEY_ROLE = 0x48444C

#: Item data role of a pad's copper item: "header:<number>" for a pin-header
#: pad, "package:<pad index>:<number>" for a package pad.
PAD_KEY_ROLE = 0x504144

#: Item data role of the items drawn WITH a package pad (its drill hole, its
#: number, its function label): the pad index, so a pad drag moves them along.
PAD_PART_ROLE = 0x504150

#: Item data role of the outline of a drill hole in the preview (True).
HOLE_ROLE = 0x484F4C

#: Item data role naming the preview layer an item belongs to («pads»,
#: «holes», «numbers», «texts», «dimensions» or a KiCad layer such as
#: «F.SilkS»): the «Layer» panel shows or hides the items of one layer
#: without redrawing. Untagged items (grid, origin, handles, measures) are
#: always shown.
LAYER_ROLE = 0x4C4159

#: Item data role of a keepout zone's item: the copper layer («F.Cu» or
#: «B.Cu») it is drawn for. A zone on both layers has one item per layer,
#: each restyled in its own side's colour (plan F4).
ZONE_COPPER_ROLE = 0x5A4F4E

#: Colour of the footprint anchor marker (plan F2) when it is NOT at the
#: drawing origin (where the drawing-origin marker already marks it): a
#: contrast choice of this preview, not a KiCad theme value.
ANCHOR_MARKER_COLOR = "#fab387"

#: Item data role set (True) on an item the «Layer» panel hid, so that
#: showing the layer again shows only those items: an item hidden by its own
#: logic (a text's anchor square shown on hover only) stays hidden.
LAYER_HIDDEN_ROLE = 0x4C4148

#: Item data role of the editable package body: "move" on the outline (its
#: stroke moves the whole body), a compass part ("n", "s", "e", "w", "ne",
#: "nw", "se", "sw") on its eight resize handles (user, 2026-11-03: «muovere con
#: il mouse la posizione del contorno ... gli spigoli», move the outline with the mouse ... the corners).
BODY_PART_ROLE = 0x424F44

#: Resize parts of the body handles, in drawing order.
BODY_HANDLE_PARTS = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


def _segment_distance(point, a, b) -> float:
    """Distance (scene units) from `point` to the segment a-b."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 <= 0 else max(0.0, min(1.0, ((point.x() - a.x()) * dx + (point.y() - a.y()) * dy) / length2))
    cx, cy = a.x() + t * dx, a.y() + t * dy
    return ((point.x() - cx) ** 2 + (point.y() - cy) ** 2) ** 0.5


class _HoleCut:
    """@brief Mixin of a copper item that leaves its drill hole unpainted.

    @note User, 2026-11-02: «perché dentro il pad non fa vedere il reticolo?»
        (why is the grid not visible inside the pad?). The hole used to be a
        disc filled with the background colour laid over the copper: it hid
        the grid (painted by the view's background) and every item under it.
        The copper now clips the hole away while it paints, so whatever lies
        beneath shows through; the item keeps its type, its rectangle or path
        and its full click area (a press in the hole still takes the pad).
    """

    def __init__(self, *args):
        """@brief Build the Qt item with the given arguments, without a hole yet.

        @param args The arguments of the Qt item class (rectangle or path).
        """
        super().__init__(*args)
        self._hole = None

    def set_hole(self, centre: QPointF, size) -> None:
        """@brief Leave a round or oval hole unpainted.

        @param centre Hole centre in ITEM coordinates (px).
        @param size Hole diameter, px, or (width, height) px of a slot along
            the item's own axes (the item turns with the pad, so the slot
            does too); a side of 0 or less removes the hole.
        """
        width, height = hole_size_pair(size)
        self._hole = (QPointF(centre), (width, height)) if width > 0 and height > 0 else None
        self.update()

    def hole(self):
        """@brief The hole as (centre, (width, height)) in item coordinates, or None.

        @return A tuple, or None for a pad without a hole. A round hole has
            width == height (CONTRACT CHANGED 2026-11-06: it used to be
            (centre, diameter); a slot needs both sides).
        """
        return self._hole

    def paint(self, painter, option, widget=None):
        """@brief Qt override: paint the copper with the hole clipped away.

        @param painter Painter in item coordinates.
        @param option Style option of the item.
        @param widget Widget painted on, or None.
        """
        if self._hole is None:
            super().paint(painter, option, widget)
            return
        centre, (width, height) = self._hole
        clip = QPainterPath()
        clip.addRect(self.boundingRect().adjusted(-2.0, -2.0, 2.0, 2.0))
        clip.addPath(hole_path(centre, width, height))
        clip.setFillRule(Qt.FillRule.OddEvenFill)
        painter.save()
        try:
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            super().paint(painter, option, widget)
        finally:
            painter.restore()


class HoledEllipseItem(_HoleCut, QGraphicsEllipseItem):
    """@brief A round or elliptic pad whose drill hole shows what lies beneath."""


class HoledRectItem(_HoleCut, QGraphicsRectItem):
    """@brief A rectangular pad whose drill hole shows what lies beneath."""


class HoledPathItem(_HoleCut, QGraphicsPathItem):
    """@brief A rounded or stadium pad whose drill hole shows what lies beneath."""


class ZoneOutlineItem(QGraphicsPolygonItem):
    """@brief A keepout zone's hatched polygon whose hit shape is its OUTLINE only.

    @note KiCad selects a rule area by its border (review 2026-11-09): the
        hatch is painted over the whole area, but the shape Qt hit-tests
        (view queries, tooltip, hover cursor) is the closed outline stroked at
        the pen width, so a pad, a body dimension or the background inside
        the zone stays reachable. The view's `_drawing_at` widens the reach to
        HIT_MARGIN_PX screen pixels through its query rectangle.
    """

    def shape(self):
        """@brief Qt override: the closed outline stroked at the pen width (at least 1e-3 scene px).

        @return A QPainterPath around the border, without the interior.
        """
        path = QPainterPath()
        path.addPolygon(self.polygon())
        path.closeSubpath()
        stroker = QPainterPathStroker()
        stroker.setWidth(max(self.pen().widthF(), 1e-3))
        return stroker.createStroke(path)


def hole_size_pair(size) -> tuple:
    """@brief (width, height) px of a hole given as a diameter or as a pair.

    @param size A number (round hole) or a (width, height) pair (slot).
    @return Two floats; a number gives (d, d).
    """
    if isinstance(size, (int, float)):
        return (float(size), float(size))
    width, height = size
    return (float(width), float(height))


def hole_path(centre: QPointF, width: float, height: float) -> QPainterPath:
    """@brief Outline of a hole: a circle, or a stadium (slot) for two different sides.

    @param centre Hole centre, in the coordinates the path is used in.
    @param width Hole size along X, px.
    @param height Hole size along Y, px.
    @return The closed path. KiCad draws an oval hole as a stadium (two
        half-discs joined by straight sides), not as an ellipse: a drill
        cannot cut an ellipse, it routes a slot of constant width.
    """
    path = QPainterPath()
    radius = min(width, height) / 2
    path.addRoundedRect(QRectF(centre.x() - width / 2, centre.y() - height / 2, width, height),
                        radius, radius)
    return path


def add_hole_outline(scene, centre: QPointF, size, pen, z: float, rotation_deg: float = 0.0):
    """@brief Draw the outline of a drill hole, unfilled, and return it.

    @param scene The preview scene.
    @param centre Hole centre, scene px.
    @param size Hole diameter, px, or (width, height) px of a slot along
        the pad's own axes.
    @param pen Outline pen.
    @param z Z value (above the copper it pierces).
    @param rotation_deg KiCad pad angle, degrees (counter-clockwise on
        screen): a slot turns with its pad; a round hole is unchanged by it.
    @return The ellipse item of a round hole, or the path item (a stadium)
        of a slot, marked with `HOLE_ROLE`.
    @note No brush: the copper leaves the hole unpainted (`_HoleCut`), so the
        grid and the items under it stay visible inside the outline.
    """
    width, height = hole_size_pair(size)
    if width == height:
        # A round hole keeps its ellipse item: its `rect()` is what the
        # drill tests and the pad tooltips read.
        item = scene.addEllipse(centre.x() - width / 2, centre.y() - height / 2, width, height, pen)
    else:
        item = scene.addPath(hole_path(centre, width, height), pen)
        if rotation_deg:
            item.setTransformOriginPoint(centre)
            item.setRotation(-float(rotation_deg))  # KiCad is counter-clockwise, Qt clockwise.
    item.setZValue(z)
    item.setData(HOLE_ROLE, True)
    return item

#: Colours of the centring crosshair of a dragged pad (user, 2026-11-02:
#: «un mirino al centro del pad per centrare ... si deve diversificare quando
#: sono nel reticolo a 2.54 oppure fuori» (a crosshair at the pad centre, different on and off the
#: grid)): green with the centre on a main-grid node,
#: orange (Catppuccin Mocha «peach») off the grid; light blue («sky») on a
#: visible dot of the drawing grid (user, 2026-11-02: «deve esserci un magnete
#: per ancorare anche nei pallini della griglia», a magnet for the grid dots too).
PAD_CROSSHAIR_ON_GRID = '#a6e3a1'
PAD_CROSSHAIR_ON_DOT = '#89dceb'
PAD_CROSSHAIR_OFF_GRID = '#fab387'

#: Screen pixels within which a dragged pad centre is caught by a main-grid
#: node or, failing that, by a visible fine-grid dot (the magnet). A pointing
#: tolerance of the display, like the measure reaches, not a geometric
#: quantity of the part.
PAD_GRID_MAGNET_PX = 8.0

#: Item data role marking the anchor square of a Reference/Value text (a
#: child of the text group). Hidden at rest; shown while the pointer is near
#: the text or the text is dragged, as the symbol editor's property anchors.
TEXT_ANCHOR_ROLE = 0x544158

#: Divisor of the main grid giving the fine drawing grid (user, 2026-10-13:
#: «una griglia puntinata a un decimo di quella attuale per ancorare gli elementi grafici»
#: (a dotted grid at one tenth of the current one to anchor the graphic
#: elements)). Pads and pins stay on the main grid.
DEFAULT_FINE_GRID_DIVISOR = 10

#: Fine-grid dots are drawn only when their spacing on screen reaches this
#: many pixels: below it the dots merge into a smear. A legibility
#: threshold of the display, not a geometric quantity.
FINE_GRID_MIN_SCREEN_PX = 5.0

#: Colours of the main grid lines and of the fine-grid dots (dark preview).
GRID_LINE_COLOR = '#36364b'
GRID_DOT_COLOR = DARK_GRID_DOT_COLOR

#: Zoom limits of the footprint camera, view px per scene px. The scene is 20 px
#: per mm; the symbol editor's 10 units per mm has its own limits (plan D13).
ZOOM_LIMITS = (0.02, 40.0)

#: How the grid is painted (the shared `paint_grid`): thin lines, round dots,
#: the painter's antialiasing hint left as the view set it.
GRID_STYLE = GridStyle(QColor(GRID_LINE_COLOR), QColor(GRID_DOT_COLOR), 1.5, None)

#: Grab tolerance around the pointer, screen px: a press this close to a
#: handle or to a drawing stroke still takes it. A UI tolerance (strokes
#: and handles are a few px wide at any zoom), not geometry. An alias of the
#: shared `canvas_menus.CONTEXT_HIT_MARGIN_PX` (parity review 2026-11-09):
#: one value for the right-click hit test of both tabs.
HIT_MARGIN_PX = CONTEXT_HIT_MARGIN_PX

#: Handles stay visible while the pointer is within this many screen px of
#: the hovered drawing's bounds, as the symbol editor's HANDLE_HOVER_MARGIN:
#: without it the handles vanish on the way from the stroke to a handle.
#: The anchor square of a Reference/Value text uses the same margin around
#: the text and its anchor, as the symbol editor's property anchors. An
#: alias of the shared `canvas_gestures.HOVER_MARGIN_PX`.
HOVER_KEEP_MARGIN_PX = HOVER_MARGIN_PX


class FootprintView(QGraphicsView):
    """@brief Navigate the footprint with anchored zoom and mouse panning; drag its texts, drawing handles and package pads.

    @note Geometry is never changed here: a drag only names its target
        (`('text', key)`, `('handle', drawing index, point index)` with point
        index -1 for the whole drawing, `('pad', package pad index)`) to the
        `GestureController`, whose host
        (the window, see `attach_host`) snaps, commits and redraws (user,
        2026-10-13). Without a host the view only pans and zooms.
    """
    pad_context_requested = pyqtSignal(str)
    #: Right click on a text: key.
    text_context_requested = pyqtSignal(str)
    #: Right click on a user drawing (outside draw mode): its index and the
    #: global screen position of the press (as `dimension_context_requested`),
    #: so the host picks the vertex under the click, not under a pointer
    #: that may have moved since (parity review 2026-11-09, D2).
    drawing_context_requested = pyqtSignal(int, QPoint)
    #: Outside draw mode: the pointer is over this drawing (-1: none).
    drawing_hovered = pyqtSignal(int)
    #: The text whose anchor square must show changed: its key ("" for none).
    text_hovered = pyqtSignal(str)
    #: Ctrl+click on a user drawing (body or handle): its index, to add or remove.
    drawing_toggled = pyqtSignal(int)
    #: Ctrl+click on a package pad: its index in the package, to add or remove.
    pad_toggled = pyqtSignal(int)
    #: A Shift+drag rubber band ended: its scene rectangle and whether Ctrl
    #: was held at its start (additive).
    band_selected = pyqtSignal(QRectF, bool)
    #: A left click (press and release without a drag) on the empty background.
    background_clicked = pyqtSignal()
    #: Escape with nothing else to cancel (no tool, pan, band or drag).
    escape_idle = pyqtSignal()
    #: Right click on the background (no measure, text, drawing or pad under
    #: the pointer, no tool armed): scene point and global screen position.
    background_context_requested = pyqtSignal(QPointF, QPoint)
    #: The pointer moved over the canvas: its scene point (as the symbol view).
    pointer_changed = pyqtSignal(object)
    #: The pointer left the viewport (as the symbol view).
    pointer_left = pyqtSignal()
    #: Zoom or pan changed (the shared camera's `changed`, as the symbol view).
    camera_changed = pyqtSignal()
    #: Right click on an editable dimension: its key (`DIMENSION_EDIT_ROLE`)
    #: and the global screen position of the menu.
    dimension_context_requested = pyqtSignal(str, QPoint)
    #: Left double click on an editable dimension: its key.
    dimension_edit_requested = pyqtSignal(str)
    #: Right click on the editable body outline or one of its handles: the
    #: scene point of the click (px) and the global screen position of the
    #: menu. The scene point lets the menu offer the drawing origin nearest
    #: to the click, as the background menu does.
    body_context_requested = pyqtSignal(QPointF, QPoint)

    def __init__(self, scene):
        """@brief A view without a gesture host: pan and zoom only until `attach_host`.

        @param scene The footprint scene.
        """
        super().__init__(scene)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setSceneRect(-1000000, -1000000, 2000000, 2000000)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        #: Zoom, wheel, pan and fit, shared with the symbol editor.
        self.camera = Camera(self, limits=ZOOM_LIMITS)
        self.camera.changed.connect(self.camera_changed)
        #: Where `drawBackground` learns which part of the viewport is being painted.
        self._exposed = ExposedRegion(self)
        #: Callable returning the scene rectangle the camera frames (or None):
        #: set by the window, which owns the framing rule (`set_framing_provider`).
        self._framing_provider = None
        self._grid = None
        #: The shared drag state machine, created by `attach_host`; None until then.
        self.gestures: GestureController | None = None
        #: Hover groups of the Reference/Value anchor squares, registered by
        #: the window when it draws the texts (owner `('text', key)`).
        self.hover = HoverGroups(self)
        #: Records whether a shortcut came from an auto-repeated key (D8):
        #: the window's deferred checkpoint reads it. Installed by `attach_host`.
        self.key_repeat = KeyRepeatFilter(self)
        self._cancel_filter = None
        #: The shared click-tool state machine and its event router, created
        #: by `attach_host` (the host receives the points); None until then.
        self.click_collector: PointCollector | None = None
        self.click_router: ClickToolRouter | None = None
        self._click_host = None
        #: The shared measuring tool (`canvas_measure.MeasureTool`), set by the
        #: window; its Escape ladder runs before the router's key handling.
        self.measure = None
        # Hover without a button pressed: the snap marker follows the pointer.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        #: Drawing index last reported as hovered (-1: none).
        self._hovered_drawing = -1
        #: Scene bounds of the hovered drawing (handles kept while the
        #: pointer stays within them plus HOVER_KEEP_MARGIN_PX), or None.
        self._hover_bounds = None
        #: Key of the text whose anchor square is shown ("" for none).
        self._hovered_text = ""
        #: Key of the editable dimension shown hovered (None: none).
        self._hovered_dimension = None
        #: Outline item of the editable package body (None: none drawn).
        self._body_outline = None
        #: Main grid step divided by this gives the drawing snap step.
        self._fine_divisor = DEFAULT_FINE_GRID_DIVISOR
        #: The shared selection rubber band (Shift+drag), as the symbol editor's.
        self.band = RubberBand(self)
        #: Viewport position of a left press on the background that started a
        #: pan; its release decides whether it was a click (`is_click`).
        self._background_press = None
        #: Scene point of the drawing origin marker (shared `canvas_origin`), or None.
        self._drawing_origin = None
        #: Scene point of the footprint anchor (plan F2), or None when no
        #: footprint is drawn; painted only where no drawing-origin marker is.
        self._anchor_marker = None

    @property
    def auto_fit(self):
        """@brief True while the camera follows the footprint (every redraw fits it).

        @return The camera's flag. False once the user zooms or pans, as the symbol
            editor's `auto_fit`: a redraw after an edit must not move the camera
            (user, 2026-10-13: «non mantiene lo zoom e lo cambia», it does not
            keep the zoom and changes it). Read and written by the window.
        """
        return self.camera.auto_fit

    @auto_fit.setter
    def auto_fit(self, value):
        """@brief Set whether the camera follows the footprint.

        @param value True to follow it (the next fit applies), False to keep the user's camera.
        """
        self.camera.auto_fit = bool(value)

    def set_framing_provider(self, provider):
        """@brief Tell the view how to find the rectangle its camera frames.

        @param provider Callable returning the scene `QRectF` to frame (the footprint
            without the grid, plus margin) or None when there is nothing to frame.
        @note Used by the resize refit while `auto_fit` is on (plan D15): the view
            never inspects the scene's items itself, the window owns the rule and
            `MainWindow._fit_preview` calls the same provider, so a resize frames
            exactly what a redraw frames.
        """
        self._framing_provider = provider

    def resizeEvent(self, event):
        """@brief Qt override: while the camera follows the footprint, re-frame it for the new viewport size.

        @param event Qt resize event, forwarded to the base class first.
        @note D15 (2026-10-17, as the symbol editor): before, a resize left the
            old scale, so the footprint could end up cropped or tiny. A user
            camera (`auto_fit` False) is never touched. The camera's memo keeps
            this free when the viewport size did not really change.
        """
        super().resizeEvent(event)
        if self.camera.auto_fit and self._framing_provider is not None:
            rect = self._framing_provider()
            if rect is not None and not rect.isEmpty():
                self.camera.fit(rect, reset=True)

    def attach_host(self, host):
        """@brief Bind the drags to their host and install the cancel and key-repeat filters.

        @param host The `canvas_gestures.GestureHost` (the main window):
            `gesture_begin` returns a live plan for `('text', key)` and a
            stepped plan for `('handle', index, point)`; `gesture_step` and
            `gesture_end` snap and commit.
        @note Qt's implicit grab is enough here (`grab_mouse=False`, as
            before the shared controller). Escape cancels a drag (decision
            D1) and so does Hide; the key filter goes in first so Hide
            cancels the drag before a deferred checkpoint is flushed.
        """
        self.gestures = GestureController(self, host, grab_mouse=False)
        self._click_host = host
        self.click_collector = PointCollector(host, self)
        self.click_router = ClickToolRouter(self, self.click_collector, self.camera,
                                            on_arm=self._on_click_tool_arm)
        self._cancel_filter = CancelOnEscapeAndHide(self.gestures, escape=True)
        install_canvas_filters(self, self.key_repeat, self._cancel_filter)

    def _on_click_tool_arm(self):
        """Router hook run before a tool is armed: end a drag, hide the text anchor square, the body handles and the dimension highlight."""
        self.cancel_interaction()
        self._report_text_hover("")
        self._report_body_hover(None)
        self._hover_dimension(None)

    @property
    def _draw_mode(self):
        """@brief Read-only: True while a draw tool is armed.

        @return Whether the collector holds an armed tool.
        """
        return self.click_collector is not None and self.click_collector.armed

    def _dragging(self):
        """True while the controller runs a drag."""
        return self.gestures is not None and self.gestures.active

    @property
    def _text_drag(self):
        """Read-only view of a running text drag: (item, key, press point, origin), or None.

        Kept for the tests that read the old attribute; the state lives in
        the controller. Assigning None cancels the drag (`cancel_interaction`).
        """
        gestures = self.gestures
        if gestures is None or not gestures.active or gestures.target[0] != 'text':
            return None
        items = gestures.live_items
        item, origin = items[0] if items else (None, None)
        return (item, gestures.target[1], gestures.start_point, origin)

    @_text_drag.setter
    def _text_drag(self, value):
        if value is not None:
            raise AttributeError("_text_drag is read-only: drags start with a press")
        if self._text_drag is not None:
            self.cancel_interaction()

    @property
    def _handle_drag(self):
        """Read-only view of a running handle drag: (drawing index, point index), or None.

        Kept for the tests that read the old attribute. Assigning None cancels the drag.
        """
        gestures = self.gestures
        if gestures is None or not gestures.active or gestures.target[0] != 'handle':
            return None
        return (gestures.target[1], gestures.target[2])

    @_handle_drag.setter
    def _handle_drag(self, value):
        if value is not None:
            raise AttributeError("_handle_drag is read-only: drags start with a press")
        if self._handle_drag is not None:
            self.cancel_interaction()

    def set_draw_mode(self, active, spec=None):
        """@brief Arm or disarm a draw tool; the router owns the cursor.

        @param active True arms `spec`; False cancels the armed tool.
        @param spec The `ToolSpec` to arm; None arms a generic open-ended tool.
        @note Arming cancels a pan and a drag, shows the CrossCursor on the
            viewport and takes the focus (all in `ClickToolRouter.arm`).
            Without a host (no `attach_host`) it does nothing.
        """
        if self.click_router is None:
            return
        if active:
            self.click_router.arm(spec if spec is not None else ToolSpec("draw"))
        else:
            self.click_router.disarm()

    def set_grid(self, step, origin=(0.0, 0.0), visible=True):
        """@brief Set an unbounded, scene-anchored reference grid in scene units.

        @param step Grid pitch in scene units (mm).
        @param origin Scene point the grid passes through (the first pad).
        @param visible False hides the grid without forgetting it.
        """
        self._grid = (float(step), tuple(map(float, origin)), bool(visible))
        self.viewport().update()

    def grid_step(self):
        """@brief The pitch of the visible main grid in scene units, or None when hidden/unset.

        @return Main grid pitch in scene units (mm), or None.
        """
        if self._grid and self._grid[2] and self._grid[0] > 0:
            return self._grid[0]
        return None

    def set_fine_divisor(self, divisor):
        """@brief Choose the drawing grid as a fraction of the main grid.

        @param divisor Positive integer: the fine step is main step / divisor
            (1 makes drawings snap to the main grid itself).
        """
        self._fine_divisor = max(1, int(divisor))
        self.viewport().update()

    def fine_divisor(self):
        """@brief The divisor of the main grid that gives the drawing grid.

        @return Positive integer divisor (1 = drawings snap to the main grid).
        """
        return self._fine_divisor

    def fine_step(self):
        """@brief The drawing-grid pitch in scene units, or None when the grid is hidden/unset.

        @return Fine grid pitch in scene units (mm), or None.
        """
        step = self.grid_step()
        return None if step is None else step / self._fine_divisor

    def snap_to_main_grid(self, x, y):
        """@brief Snap a scene point to the nearest node of the visible MAIN grid.

        @param x Scene x (px).
        @param y Scene y (px).
        @return The nearest crossing of the main grid lines shown on screen
            (same step and origin as the lines), or None when the grid is
            hidden or unset.
        @note Pads go through here (they keep the main grid, user,
            2026-10-13); graphic elements use the fine grid (`snap_to_grid`).
        """
        step = self.grid_step()
        if step is None:
            return None
        ox, oy = self._grid[1]
        return ox + round((x - ox) / step) * step, oy + round((y - oy) / step) * step

    def snap_to_grid(self, x, y, fallback_step):
        """@brief Snap a scene point to the nodes of the drawing (fine) grid.

        @param x Scene x (px).
        @param y Scene y (px).
        @param fallback_step Step in scene units when the grid is hidden or unset.
        @return The nearest node of the fine grid shown on screen (main step /
            divisor, same origin as the lines: every main crossing is also a
            fine node), else the nearest multiple of `fallback_step` from the
            scene origin.
        @note Graphic elements (drawings, texts, handles) go through here;
            pads and pins never do: they keep the main grid (user, 2026-10-13).
        """
        step, (ox, oy) = self.drawing_grid(fallback_step)
        if step <= 0:
            return float(x), float(y)
        return ox + round((x - ox) / step) * step, oy + round((y - oy) / step) * step

    def drawing_grid(self, fallback_step):
        """@brief The drawing (fine) grid `snap_to_grid` rounds to: its step and a node it passes through.

        @param fallback_step Step in scene units when the grid is hidden or unset.
        @return (step, (ox, oy)) in scene units: the shown grid's main step
            divided by the fine divisor and its origin, else `fallback_step`
            from the scene origin. The host's angle-constrained runs
            (`draw_constraints.constrain_to_grid`) use the same grid, so a
            free point and a constrained one land on one lattice (review M3).
        """
        if self._grid and self._grid[2] and self._grid[0] > 0:
            step, (ox, oy), _visible = self._grid
            return step / self._fine_divisor, (float(ox), float(oy))
        return float(fallback_step), (0.0, 0.0)

    def set_drawing_origin(self, point):
        """@brief Show the drawing origin marker at a scene point, or hide it.

        @param point Scene `QPointF` (px), or None to hide the marker.
        @note The marker is painted in `drawForeground`, not added to the
            scene: every redraw clears the scene, and an overlay must never
            enter the framing or the item hit tests.
        """
        point = None if point is None else QPointF(point)
        if point != self._drawing_origin:
            self._drawing_origin = point
            self.viewport().update()

    def drawing_origin(self):
        """@brief The scene point of the drawing origin marker, or None.

        @return A `QPointF` copy, or None when no marker is shown.
        """
        return None if self._drawing_origin is None else QPointF(self._drawing_origin)

    def set_anchor_marker(self, point):
        """@brief Show the footprint anchor (the point that is (0, 0) in the written file), or hide it.

        @param point Scene `QPointF` (px), or None to hide the marker.
        @note Painted in `drawForeground` like the drawing origin marker, with
            the same glyph (a circle with a cross, as KiCad's anchor) in
            `ANCHOR_MARKER_COLOR`; not painted where the drawing origin marker
            already is (the drawing origin «footprint origin» follows the
            anchor), so an unmoved anchor paints exactly what it painted before.
        """
        point = None if point is None else QPointF(point)
        if point != self._anchor_marker:
            self._anchor_marker = point
            self.viewport().update()

    def anchor_marker(self):
        """@brief The scene point of the footprint anchor marker, or None.

        @return A `QPointF` copy, or None when no anchor is shown.
        """
        return None if self._anchor_marker is None else QPointF(self._anchor_marker)

    def anchor_marker_painted(self) -> bool:
        """@brief Whether the anchor marker is painted: an anchor away from the drawing origin marker.

        @return True when an anchor is set and differs from the drawing origin point.
        """
        return self._anchor_marker is not None and self._anchor_marker != self._drawing_origin

    def drawForeground(self, painter, rect):
        """@brief Qt override: paint the drawing origin marker above the footprint.

        @param painter Painter in scene coordinates.
        @param rect Exposed scene rectangle (unused: the marker is tiny).
        """
        super().drawForeground(painter, rect)
        paint_origin_marker(painter, self, self._drawing_origin)
        if self.anchor_marker_painted():
            paint_origin_marker(painter, self, self._anchor_marker, ANCHOR_MARKER_COLOR)

    def drawBackground(self, painter, rect):
        """@brief Qt override: draw the grid lines (and fine dots) where Qt paints, like the symbol editor.

        @param painter Painter in scene coordinates.
        @param rect Exposed scene rectangle.
        @note Lines every main step from the grid origin, then the dots at the fine
            nodes (those on a main line are skipped). Only the lines and dots that
            reach the paint event's region are laid out (`ExposedRegion`, D16): the
            pixels are those of the whole-rectangle layout.
        """
        super().drawBackground(painter, rect)
        if not self._grid or not self._grid[2]:
            return
        step, origin, _ = self._grid
        if step <= 0:
            return
        paint_grid(painter, rect, exposed=self._exposed, step=step, origin=origin, style=GRID_STYLE,
                   fine_step=self.fine_step() if self.fine_dots_visible() else None,
                   skip_dots_on_lines=True)

    def fine_dots_visible(self):
        """@brief True when the fine-grid dots are drawn at the current zoom.

        @return True when the fine step on screen reaches FINE_GRID_MIN_SCREEN_PX.
        """
        fine = self.fine_step()
        return (fine is not None and self._fine_divisor > 1
                and dots_legible(fine, self.transform().m11(), FINE_GRID_MIN_SCREEN_PX))

    def zoom(self, factor, anchor=None):
        """@brief Scale the camera within limits while preserving the scene point under the anchor.

        @param factor Multiplier applied to the current scale (>1 zooms in);
            the resulting scale is clamped to `ZOOM_LIMITS`.
        @param anchor Viewport point (QPoint, px) kept fixed on screen;
            None uses the viewport centre.
        @return True when the scale changed (and `auto_fit` went off); False, with
            nothing touched, when the clamped scale equals the current one.
        """
        return self.camera.zoom(factor, anchor)

    def wheelEvent(self, event):
        """@brief Zoom toward the mouse position using the wheel delta; refused while a drag or a pan runs.

        @param event Qt wheel event; angle delta and pointer position are used.
        @note Decision D2 (2026-10-17, as the symbol editor): a rescale under
            a live drag would re-measure the drag's delta in the new scene
            and make the text jump, and a rescale under a pan would move the
            camera by a delta measured in two scales; the wheel waits for
            the drop (drag) or the release (pan). D14: one event zooms by at
            most four notches (`Camera.wheel`).
        """
        self.camera.wheel(event, blocked=self._dragging() or self.camera.pan_active)

    def _items_near(self, pos):
        """Items within HIT_MARGIN_PX of a viewport point, topmost first.

        Uses the view's own hit test, which honours the view transform:
        handles ignore transformations, so a scene-space query would shrink
        their grab area with the zoom (1 px at usual «fit» zooms).
        """
        m = HIT_MARGIN_PX
        return self.items(QRect(pos.x() - m, pos.y() - m, 2 * m + 1, 2 * m + 1))

    #: Hover-group owner of the body resize handles.
    BODY_OWNER = ('body',)

    def set_body_editor(self, outline, handles) -> None:
        """@brief Register the editable body drawn by the host: its outline and its eight handles.

        @param outline The outline polygon item (its stroke moves the body).
        @param handles The handle items, revealed by the SHARED hover rule
            with the symbol body's ring (`HoverGroups.add(ring=True)`): only
            near the outline, never anywhere over the body (second review
            2026-11-03, as the symbol editor, user 2026-10-12).
        @note The items are Python-built and die with the next scene clear:
            the host calls `forget_body_editor` (through `hover.clear()` and
            this) before every clear.
        """
        self._body_outline = outline
        handles = list(handles)
        if handles:
            self.hover.add(handles, outline.sceneBoundingRect(), True, self.BODY_OWNER)
        # A redraw under a still pointer (the drop of a body drag) shows the
        # handles at once, as the symbol editor re-applies its visibility.
        self.refresh_body_hover()

    def forget_body_editor(self) -> None:
        """@brief Forget the body outline (its items die with the scene clear that follows)."""
        self._body_outline = None

    def refresh_body_hover(self) -> None:
        """@brief Re-apply the body handles' visibility for the pointer where it is now."""
        from PyQt6.QtGui import QCursor
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        self._report_body_hover(pos if self.viewport().rect().contains(pos) else None)

    def _body_part_at(self, pos):
        """@brief Part of the editable body under a viewport point: a visible handle, "move" on the outline stroke, else None.

        @param pos Viewport position (QPoint).
        @return A compass part of BODY_HANDLE_PARTS, "move", or None.
        @note Only the STROKE of the outline moves the body, within
            HIT_MARGIN_PX screen pixels: a press inside the body pans, as on
            the background, so pads and drawings inside stay easy to reach.
        """
        for item in self._items_near(pos):
            part = item.data(BODY_PART_ROLE)
            if part and part != "move" and item.isVisible():
                return str(part)
        from .canvas_gestures import _alive
        outline = getattr(self, "_body_outline", None)
        if outline is None or not _alive(outline):
            return None
        return "move" if self._near_polygon_outline(outline, pos) else None

    def _near_polygon_outline(self, item, pos) -> bool:
        """@brief True when a viewport point lies within HIT_MARGIN_PX screen px of a polygon item's closed outline.

        @param item A QGraphicsPolygonItem (the body outline, a keepout zone).
        @param pos Viewport position (QPoint).
        @return True on the stroke (the closing side included), False inside
            or outside it: the interior is not part of the hit.
        @note The margin is in screen pixels at any zoom, as `_items_near`.
        """
        point = self.mapToScene(pos)
        scale = max(abs(self.transform().m11()), 1e-9)
        polygon = item.mapToScene(item.polygon())
        count = polygon.count()
        for index in range(count):
            a, b = polygon.at(index), polygon.at((index + 1) % count)
            if _segment_distance(point, a, b) * scale <= HIT_MARGIN_PX:
                return True
        return False

    def _report_body_hover(self, pos) -> None:
        """Show the body handles by the shared ring rule (or while a drag of the body runs); no scene scan."""
        from .canvas_gestures import _alive
        groups = [items for items, _base, _ring, owner in self.hover.groups if owner == self.BODY_OWNER]
        if not groups:
            return
        dragged = self.BODY_OWNER if (self._dragging() and self.gestures.target[0] == 'body') else None
        shown = self.hover.revealed(self.mapToScene(pos) if pos is not None else None, dragged)
        visible = self.BODY_OWNER in shown
        for items in groups:
            for item in items:
                if _alive(item) and item.isVisible() != visible:
                    item.setVisible(visible)

    def _pad_copper_at(self, pos):
        """The PACKAGE pad whose copper contains a viewport point exactly, or None (no hit margin)."""
        for item in self.scene().items(self.mapToScene(pos)):
            token = item.data(PAD_KEY_ROLE)
            if token is None:
                continue
            parts = str(token).split(":", 2)
            if parts[0] == "package" and len(parts) == 3:
                return int(parts[1])
        return None

    def _text_item_at(self, pos):
        """The draggable text group under a viewport point, with its key, or (None, None).

        The hit may be a child (the text or its anchor square): the drag moves
        the top-level item, whose position is the anchor.
        """
        for item in self._items_near(pos):
            key = item.data(TEXT_KEY_ROLE)
            if key is not None:
                while item.parentItem() is not None:
                    item = item.parentItem()
                return item, str(key)
        return None, None

    def _handle_at(self, pos):
        """The (drawing, point) indices of the handle under a viewport point, or None."""
        for item in self._items_near(pos):
            key = item.data(HANDLE_KEY_ROLE)
            if key is not None:
                drawing, point = str(key).split(":")
                return int(drawing), int(point)
        return None

    def _pad_at(self, pos):
        """The index of the PACKAGE pad under a viewport point, or None.

        Pin-header pads ("header:<number>") are not returned: their position
        is computed from pitch and row spacing, so there is nothing to drag.
        """
        for item in self._items_near(pos):
            token = item.data(PAD_KEY_ROLE)
            if token is None:
                continue
            parts = str(token).split(":", 2)
            if parts[0] == "package" and len(parts) == 3:
                return int(parts[1])
        return None

    def _drawing_at(self, pos):
        """The index of the drawing (or of the drawing a handle belongs to) under a point, else -1.

        A keepout zone answers on its OUTLINE only, as KiCad selects a rule
        area by its border (review 2026-11-09): its hatched item covers the
        area, and a hit on the interior would take every right click, drag,
        Ctrl+click and double click meant for a pad, a body dimension or the
        background inside it. A filled graphic keeps its interior hit, as
        KiCad's filled shapes do.
        """
        handle = self._handle_at(pos)
        if handle is not None:
            return handle[0]
        for item in self._items_near(pos):
            index = item.data(DRAWING_KEY_ROLE)
            if index is None:
                continue
            if item.data(DRAWING_KIND_ROLE) == "zone" and not self._near_polygon_outline(item, pos):
                continue
            return int(index)
        return -1

    def _drawing_bounds(self, index):
        """Scene bounds of all the items of a drawing, or None when it has none."""
        bounds = None
        for item in self.scene().items():
            if item.data(DRAWING_KEY_ROLE) == index:
                rect = item.sceneBoundingRect()
                bounds = rect if bounds is None else bounds.united(rect)
        return bounds

    def _near_scene_rect(self, rect, pos):
        """True when a viewport point lies within HOVER_KEEP_MARGIN_PX screen px of a scene rect.

        The margin is in screen pixels at any zoom, as the symbol editor's
        HANDLE_HOVER_MARGIN (divided there by the view scale).
        """
        area = self.mapFromScene(rect).boundingRect()
        m = HOVER_KEEP_MARGIN_PX
        return area.adjusted(-m, -m, m, m).contains(pos)

    def _within_hover_area(self, pos):
        """True while a viewport point stays near the hovered drawing's bounds."""
        if self._hover_bounds is None:
            return False
        return self._near_scene_rect(self._hover_bounds, pos)

    @staticmethod
    def _text_reveal_area(group):
        """Scene rect of a text group's glyphs united with its anchor point.

        The same base rect as the symbol editor's property anchors (text
        bounds plus anchor). The square itself is left out: it ignores the
        view transform, so its scene bounds are not a scene measure.
        """
        anchor = group.scenePos()
        area = QRectF(anchor, anchor)
        for child in group.childItems():
            if child.data(TEXT_ANCHOR_ROLE) is None:
                area = area.united(child.sceneBoundingRect())
        return area

    def _text_near(self, pos):
        """Key of the first registered text whose hover group reveals at a viewport point, or "".

        The window registers one group per text (`hover.add`, owner
        `('text', key)`, base = `_text_reveal_area`); the shared rule grows
        the base by HOVER_KEEP_MARGIN_PX screen px at any zoom.
        """
        shown = self.hover.revealed(self.mapToScene(pos), None)
        for _items, _base, _ring, owner in self.hover.groups:
            if owner in shown and owner[0] == 'text':
                return str(owner[1])
        return ""

    def _report_text_hover(self, key):
        """Emit `text_hovered` only when the text whose anchor shows changes."""
        if key != self._hovered_text:
            self._hovered_text = key
            self.text_hovered.emit(key)

    def hovered_text(self):
        """@brief The key of the text whose anchor square is shown ("" for none).

        @return "Reference", "Value" or "".
        """
        return self._hovered_text

    def _report_hover(self, index):
        """Emit `drawing_hovered` only when the hovered drawing changes."""
        if index != self._hovered_drawing:
            self._hovered_drawing = index
            self._hover_bounds = self._drawing_bounds(index) if index >= 0 else None
            self.drawing_hovered.emit(index)

    def hovered_drawing(self):
        """@brief The index of the drawing whose handles are shown (-1: none).

        @return Index into the window's drawings list, or -1.
        """
        return self._hovered_drawing

    def reset_hover(self):
        """@brief Forget the hovered drawing and its bounds, without emitting.

        @note For the window, after it changed the drawings list: the index
            and bounds remembered here describe the old list, so the next
            pointer move over the same index would otherwise emit nothing
            and the handles would not come back. No signal: the window has
            already reset its own hover state.
        """
        self._hovered_drawing = -1
        self._hover_bounds = None

    def cancel_interaction(self):
        """@brief Cancel the rubber band, the pan and a handle or text drag in progress, without reporting a drop.

        @note For the window, when the drawings list changes while the mouse
            button is still down (Delete, Ctrl+Z, Ctrl+Y, Ctrl+D): the drag
            holds indices into the old list, and its release would move a
            different drawing. The controller tells the host once
            (`gesture_end(commit=False)`: a dragged text goes back where it
            was, decision D5) and the release that follows does nothing.
        @note CONTRACT CHANGED (review 2026-10-29, C7, as the symbol editor's
            `cancel_interaction`): the rubber band, the camera pan and a
            pending background click are dropped too, so arming a draw tool
            (`_on_click_tool_arm`) or any other cancel leaves no band growing
            and no pan following the pointer. The armed draw tool itself is
            untouched (the router owns it).
        """
        self.band.cancel()
        self.camera.pan_cancel()
        self._background_press = None
        if self.gestures is not None:
            self.gestures.cancel()

    def _press_gesture(self, target, point) -> bool:
        """Offer a left press to the gesture controller with auto-fit switched off first.

        INVARIANT (plan docs/canvas-gestures-plan.md §1c D13, review
        2026-10-27 D2): auto-fit goes off BEFORE the host begins a drag, as
        the symbol editor does, so the redraws of the drag and of the drop
        never refit the camera under the user's hand (the scale changed at
        the drop while the pointer was still over the drawing); it goes
        back on when the host refuses the press, so a refused click changes
        nothing. Without a controller the press is refused and nothing changes.
        """
        if self.gestures is None:
            return False
        auto_fit, self.camera.auto_fit = self.camera.auto_fit, False
        if self.gestures.press(target, point):
            return True
        self.camera.auto_fit = auto_fit
        return False

    def mousePressEvent(self, event):
        """@brief Begin a text drag on a text, else camera panning, on a left or middle press.

        @param event Qt mouse press event.
        @note With a draw tool armed the router sees the press first: a left
            press is a drawing point and a right press closes the shape; a
            press during a camera pan is swallowed; the middle button falls
            through to the camera.
        @note A left or right press while a pan runs is swallowed (review
            2026-10-29, C2/C9): no drag and no context menu with the pan armed.
        @note Selection (shared `canvas_selection`): Shift+left press starts
            the rubber band, Ctrl+left press on a drawing emits
            `drawing_toggled`, on a package pad `pad_toggled`; a plain press
            on a drawing or a package pad is selected by the host when its
            drag begins (`gesture_begin`); a left press on the
            background pans, and its release without a drag emits
            `background_clicked` (not for a Ctrl click, which is additive).
            Any press while the band is drawn is swallowed.
        @note A press that begins a text or drawing drag switches auto-fit
            off first (`_press_gesture`, plan docs/canvas-gestures-plan.md
            D13), as the symbol editor does.
        """
        if (self.measure is not None and event.button() == Qt.MouseButton.RightButton
                and not self.band.active and not self.camera.pan_active and not self._dragging()
                and self.measure.context_menu(event.pos(), event.globalPosition().toPoint(), self)):
            # A right press on a measure opens its menu («Rimuovi misura»,
            # remove measure), never the pad or drawing menu underneath.
            event.accept()
            return
        if self.click_router is not None and self.click_router.press(event):
            return
        if self.band.active:
            # Another button pressed while the rubber band is drawn: no menu,
            # no pan, no selection change until the band is released.
            event.accept()
            return
        if self.camera.pan_active and event.button() != Qt.MouseButton.MiddleButton:
            # A left or right press while a pan runs is swallowed: no drag,
            # band, toggle or menu starts under a moving camera (review
            # 2026-10-29, C2/C9, the symbol editor's rule). A middle press
            # restarts the pan, as before.
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            if self._dragging():
                # Decision D3 (2026-10-17, as the symbol editor): no menu over
                # a drag in progress; the drop or Escape comes first.
                event.accept()
                return
            item, key = self._text_item_at(event.pos())
            if item is not None:
                self.text_context_requested.emit(key)
                event.accept()
                return
            # A handle belongs to its drawing: the handles cover the stroke
            # (a text's only handle sits on its bottom-left anchor corner,
            # 2026-10-14) while hovered.
            drawing_index = self._drawing_at(event.pos())
            if drawing_index >= 0:
                self.drawing_context_requested.emit(drawing_index, event.globalPosition().toPoint())
                event.accept()
                return
            for item in self.scene().items(self.mapToScene(event.pos())):
                pad_index = item.data(PAD_KEY_ROLE)
                if pad_index is not None:
                    self.pad_context_requested.emit(str(pad_index))
                    event.accept()
                    return
            if self._body_part_at(event.pos()) is not None:
                # The editable body outline (or a handle) has its own menu,
                # after texts, drawings and pads (approved plan 03/11, point 4).
                self.body_context_requested.emit(self.mapToScene(event.pos()),
                                                 event.globalPosition().toPoint())
                event.accept()
                return
            dimension = self._dimension_at(event.pos())
            if dimension is not None:
                # An editable dimension comes after texts, drawings and pads,
                # as on a left press (review 2026-11-03, C1): a Value text over
                # a body number keeps its own menu.
                self.dimension_context_requested.emit(dimension, event.globalPosition().toPoint())
                event.accept()
                return
            # Nothing of the footprint under the pointer: the background menu
            # (the drawing origin, user 2026-11-02).
            self.background_context_requested.emit(self.mapToScene(event.pos()),
                                                   event.globalPosition().toPoint())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and not self._dragging():
            # Selection gestures shared with the symbol editor (plan
            # docs/canvas-selection-plan.md): Shift+drag draws the rubber
            # band anywhere (Ctrl held: additive); Ctrl+click on a drawing
            # adds or removes it and starts no drag.
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self.band.begin(event.pos(), bool(modifiers & Qt.KeyboardModifier.ControlModifier))
                event.accept()
                return
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                # Same priority as a plain press: a drawing handle, then a
                # package pad, then the body of a drawing.
                pad = self._pad_at(event.pos()) if self._handle_at(event.pos()) is None else None
                if pad is not None:
                    # Ctrl+click on a pad, as on a pin of the symbol editor.
                    self.pad_toggled.emit(pad)
                    event.accept()
                    return
                index = self._drawing_at(event.pos())
                if index >= 0:
                    self.drawing_toggled.emit(index)
                    event.accept()
                    return
        if event.button() == Qt.MouseButton.LeftButton:
            # Priority: a visible handle (the smallest, most deliberate
            # target), then a text (Reference/Value sit above the drawings:
            # a label inside a rectangle must stay draggable), then a package
            # pad (a pad inside a drawn outline must stay draggable), then
            # the body of a drawing.
            handle = self._handle_at(event.pos())
            item, key = (None, None) if handle is not None else self._text_item_at(event.pos())
            point = self.mapToScene(event.pos())
            if item is not None and self._press_gesture(('text', key), point):
                # The dragged text keeps its anchor square wherever the
                # pointer goes, as a dragged property in the symbol editor.
                self._report_text_hover(key)
                event.accept()
                return
            # A body gesture never starts with Ctrl (Ctrl+click selects and
            # never drags, plan canvas-selection), and a pad whose COPPER is
            # under the pointer wins over a body handle on the same spot (a
            # pad on the outline of a QFN or a castellated edge).
            plain = not (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            body_part = (self._body_part_at(event.pos())
                         if handle is None and item is None and plain else None)
            if body_part not in (None, "move") and self._pad_copper_at(event.pos()) is not None:
                body_part = None
            if (body_part not in (None, "move")
                    and self._press_gesture(('body', body_part), point)):
                # A resize handle of the package body (user, 2026-11-03).
                event.accept()
                return
            pad = self._pad_at(event.pos()) if handle is None and item is None else None
            if pad is not None and self._press_gesture(('pad', pad), point):
                # The window moves the pad (or the selected pads) on the
                # grid, as the symbol editor moves its pins (user, 2026-11-02).
                event.accept()
                return
            if handle is None:
                # The body of a drawing moves the whole drawing, like an
                # element of the symbol editor (user, 2026-10-13).
                index = self._drawing_at(event.pos())
                handle = (index, -1) if index >= 0 else None
            if handle is not None and self._press_gesture(('handle', *handle), point):
                # The window moves the point (or the whole drawing) on the
                # grid and redraws the shape at every step.
                event.accept()
                return
            if body_part == "move" and self._press_gesture(('body', 'move'), point):
                # The outline stroke moves the whole body, last of all.
                event.accept()
                return
        if self._dragging() and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            # As the symbol editor: another button pressed over a running
            # drag neither pans nor starts a second gesture.
            event.accept()
            return
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            # Qt's implicit grab is enough; a click without a move keeps auto_fit.
            self.camera.pan_begin(event.button(), event.pos(), grab_mouse=False, keep_auto_fit=True)
            # A left press on the background: its release without a drag
            # clears the selection (`background_clicked`); a drag pans. An
            # additive (Ctrl) click that misses every drawing keeps the
            # selection: it was meant to add, not to start over.
            additive = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self._background_press = (event.pos() if event.button() == Qt.MouseButton.LeftButton
                                      and not additive else None)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """@brief Feed a drag, or pan the camera, without changing footprint geometry.

        @param event Qt mouse move event.
        @note A text follows live (the controller moves the item; a redraw
            under the drag re-binds the new item, decision D4); a handle
            step goes to the host, which snaps it and shows the ghost.
        """
        if self.measure is not None and event.buttons() == Qt.MouseButton.NoButton:
            # Hovering a measure highlights it (Delete / right click remove it).
            self.measure.hover_measure(event.pos())
        if event.buttons() == Qt.MouseButton.NoButton:
            # An editable dimension shows it answers a double click.
            self._hover_dimension(event.pos())
        # The coordinates follow the pointer in every state (drag, pan, tool).
        self.pointer_changed.emit(self.mapToScene(event.pos()))
        if self._dragging():
            self.gestures.move(self.mapToScene(event.pos()))
            event.accept()
            return
        if self.band.move(event.pos()):
            event.accept()
            return
        if self.camera.pan_active:
            self.camera.pan_move(event.pos())
            event.accept()
        elif self.click_router is not None and self.click_router.move(event):
            event.accept()
        else:
            # Hovering a drawing reveals its handles (user, 2026-10-13:
            # «come nell'altro tab», as in the other tab); they stay while the pointer remains near the
            # drawing (hysteresis, as the symbol editor's reveal area), so a
            # handle can be reached from the stroke.
            index = self._drawing_at(event.pos())
            if index < 0 and self._within_hover_area(event.pos()):
                index = self._hovered_drawing
            self._report_hover(index)
            # A text's anchor square shows while the pointer is near the
            # text, same margin as the symbol editor's property anchors.
            self._report_text_hover(self._text_near(event.pos()))
            self._report_body_hover(event.pos())
            super().mouseMoveEvent(event)
            if self.click_router is not None:
                self.click_router.settle_cursor(event)

    def leaveEvent(self, event):
        """@brief Qt override: the snap marker and the hover handles go away with the pointer.

        @param event Qt leave event, forwarded to the base class.
        """
        if self.click_router is not None and self._draw_mode:
            self.click_router.leave()
        if self.measure is not None:
            self.measure.hover_measure(None)
        self._hover_dimension(None)
        if not self._dragging():
            self._report_body_hover(None)
        if self._handle_drag is None:
            self._report_hover(-1)
        if self._text_drag is None:
            self._report_text_hover("")
        self.pointer_left.emit()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief Drop a drag through the controller, or apply the final pan displacement.

        @param event Qt mouse release event.
        @note With a draw tool armed a release away from its press is a DRAG
            (`ToolSpec.drag_adds_release_point`): the release point counts as
            the next drawing point, so a rectangle or a circle can be drawn
            press-move-release as in a CAD, not only click-click (user,
            2026-10-13: «non funziona»). A release at the press point is a
            plain click and adds nothing.
        @note A rubber band ends here (`band_selected`); a left pan whose
            release `is_click` of its press emits `background_clicked`.
        """
        if self._dragging() and event.button() == Qt.MouseButton.LeftButton:
            kind = self.gestures.target[0]
            # The release passes through a last move at the drop point, then
            # the host commits once (`gesture_end`).
            self.gestures.release(self.mapToScene(event.pos()))
            if kind == 'text':
                # After the drop the square follows the pointer rule again.
                self._report_text_hover(self._text_near(event.pos()))
            event.accept()
            return
        if self.band.active and event.button() == Qt.MouseButton.LeftButton:
            additive = self.band.additive
            rect = self.band.finish(event.pos())
            if rect is not None:
                self.band_selected.emit(rect, additive)
            event.accept()
            return
        if self.click_router is not None and self.click_router.release(event):
            return
        if self.camera.pan_active and event.button() == self.camera.pan_button:
            press, self._background_press = self._background_press, None
            self.camera.pan_end(event.pos())
            event.accept()
            if press is not None and event.button() == Qt.MouseButton.LeftButton and is_click(press, event.pos()):
                self.background_clicked.emit()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """@brief Qt override: a left double click on a measure edits its value; in draw mode it closes the shape.

        @param event Qt mouse double-click event.
        @note A fast second click reaches Qt as press, release, DOUBLE CLICK:
            the second press never arrives. The router therefore first counts
            it as that missing point (fixed-count shapes close on it), QUIETLY,
            then as the finish request (which ends an open line; it is a
            no-op when the point already closed the shape). A repeated point
            is not added twice and, being quiet, shows no refusal: a true
            double click on the last vertex of a line just finishes it.
        """
        if (self.measure is not None and event.button() == Qt.MouseButton.LeftButton
                and not self.camera.pan_active and not self.band.active
                and self.measure.edit_at(event.pos())):
            # A double click on a measure edits its value («Modifica quota…»).
            event.accept()
            return
        if self.click_router is not None and self.click_router.double_click(event):
            return
        if (event.button() == Qt.MouseButton.LeftButton and not self._dragging()
                and not self.camera.pan_active and not self.band.active
                and self._handle_at(event.pos()) is None
                and self._text_item_at(event.pos())[0] is None
                and self._pad_at(event.pos()) is None and self._drawing_at(event.pos()) < 0):
            dimension = self._dimension_at(event.pos())
            if dimension is not None:
                # The same edit for an editable dimension of the preview, in
                # the left press's priority order and never over a running pan
                # or band (review 2026-11-03, C1/C2).
                self.dimension_edit_requested.emit(dimension)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _dimension_at(self, pos):
        """@brief Key of the editable dimension under a viewport position, or None.

        @param pos Viewport position (QPoint).
        @return The `DIMENSION_EDIT_ROLE` value of an item within the
            measures' screen reach (`canvas_measure.editable_dimension_at`).
        """
        from .canvas_measure import editable_dimension_at
        return editable_dimension_at(self, pos)

    def forget_dimension_hover(self) -> None:
        """@brief Forget the hovered dimension: its items die with a scene clear, the next hover starts afresh."""
        self._hovered_dimension = None

    def _hover_dimension(self, pos) -> None:
        """Highlight the editable dimension under the pointer and restore the one left (C4)."""
        from .canvas_measure import set_dimension_highlight
        key = self._dimension_at(pos) if pos is not None else None
        if key != self._hovered_dimension:
            set_dimension_highlight(self.scene(), self._hovered_dimension, False)
            set_dimension_highlight(self.scene(), key, True)
            self._hovered_dimension = key

    def keyPressEvent(self, event):
        """@brief Qt override: in draw mode Esc cancels and Enter closes the shape.

        @param event Qt key press event.
        @note The router sees the key first (armed: Escape cancels a pan, else the
            tool; Enter finishes). Escape also cancels a camera pan in progress, as the symbol
            editor does (2026-10-17): the camera stays where it is, the
            release that follows does nothing, and the key is consumed, so
            it does not also cancel an armed drawing tool. With no pan
            running, Escape reaches the draw tool as before.
        @note Then Escape cancels a rubber band; with nothing at all to
            cancel (no tool, pan, band or drag) it emits `escape_idle`, on
            which the host clears the drawing selection.
        @note With the measuring tool armed its Escape ladder runs first
            (`MeasureTool.escape`, plan docs/canvas-measure-plan.md D-M3):
            the pending point, then the measures, then the router disarms.
        """
        if self.measure is not None and self.measure.escape(event, self.camera):
            return
        if self._restep_body_on_shift(event):
            return
        if self.click_router is not None and self.click_router.key(event):
            return
        if event.key() == Qt.Key.Key_Escape and self.camera.pan_cancel():
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.band.cancel():
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and not self._dragging():
            # Nothing else to cancel: the host clears the selection.
            self.escape_idle.emit()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """@brief Qt override: releasing Shift during a body drag re-steps it at once.

        @param event Qt key release event, forwarded to the base class otherwise.
        @note An armed click tool hears every release first
            (`ClickToolRouter.key_release`, review M2): a Shift release
            re-runs the last hover with the constraint and the object snap
            back on, so the ghost a click would add follows the key without a
            mouse move. The router never consumes the release.
        """
        if self.click_router is not None:
            self.click_router.key_release(event)
        if self._restep_body_on_shift(event):
            return
        super().keyReleaseEvent(event)

    def _restep_body_on_shift(self, event) -> bool:
        """Step a running body drag again when Shift changes, so the symmetric ghost follows the key, not the next move."""
        if (event.key() != Qt.Key.Key_Shift or not self._dragging()
                or self.gestures.target[0] != 'body'):
            return False
        point = self.gestures.last_point or self.gestures.start_point
        if point is not None:
            self.gestures.move(point)
        event.accept()
        return True

    def hideEvent(self, event):
        """@brief Discard an unfinished camera drag, and cancel a text or handle drag, when the view is hidden.

        @param event Qt hide event, forwarded to the base class.
        @note The cancel filter already cancels on the Hide event; the direct
            call here covers a `hideEvent` invoked without the event loop
            (tests) and is a no-op when the drag is already gone. The host
            puts the dragged text back and drops the handle ghost.
        """
        self.camera.pan_cancel()
        self.band.cancel()
        self._background_press = None
        self.cancel_interaction()
        self._report_text_hover("")
        self._report_body_hover(None)
        self._hover_dimension(None)
        if self.click_router is not None:
            # The host disarms the tool and cleans its click markers.
            self.click_router.hide(event.spontaneous())
        super().hideEvent(event)
