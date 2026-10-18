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
from PyQt6.QtCore import Qt, QRect, QRectF, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGraphicsView

from .canvas_camera import Camera
from .canvas_clicktools import ClickToolRouter, PointCollector, ToolSpec
from .canvas_grid import ExposedRegion, GridStyle, dots_legible, paint_grid
from .canvas_gestures import (
    HOVER_MARGIN_PX, CancelOnEscapeAndHide, GestureController, HoverGroups, KeyRepeatFilter,
    install_canvas_filters,
)

#: Item data role carrying the key of a draggable text (Reference, Value).
TEXT_KEY_ROLE = 0x544558

#: Item data role carrying the index of a user drawing of the footprint.
DRAWING_KEY_ROLE = 0x445257

#: Item data role of a drawing handle: "<drawing index>:<point index>";
#: point index -1 means the WHOLE drawing (a press on its body).
HANDLE_KEY_ROLE = 0x48444C

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
GRID_DOT_COLOR = '#4a4a63'

#: Zoom limits of the footprint camera, view px per scene px. The scene is 20 px
#: per mm; the symbol editor's 10 units per mm has its own limits (plan D13).
ZOOM_LIMITS = (0.02, 40.0)

#: How the grid is painted (the shared `paint_grid`): thin lines, round dots,
#: the painter's antialiasing hint left as the view set it.
GRID_STYLE = GridStyle(QColor(GRID_LINE_COLOR), QColor(GRID_DOT_COLOR), 1.5, None)

#: Grab tolerance around the pointer, screen px: a press this close to a
#: handle or to a drawing stroke still takes it. A UI tolerance (strokes
#: and handles are a few px wide at any zoom), not geometry.
HIT_MARGIN_PX = 4

#: Handles stay visible while the pointer is within this many screen px of
#: the hovered drawing's bounds, as the symbol editor's HANDLE_HOVER_MARGIN:
#: without it the handles vanish on the way from the stroke to a handle.
#: The anchor square of a Reference/Value text uses the same margin around
#: the text and its anchor, as the symbol editor's property anchors. An
#: alias of the shared `canvas_gestures.HOVER_MARGIN_PX`.
HOVER_KEEP_MARGIN_PX = HOVER_MARGIN_PX


class FootprintView(QGraphicsView):
    """@brief Navigate the footprint with anchored zoom and mouse panning; drag its texts and drawing handles.

    @note Geometry is never changed here: a drag only names its target
        (`('text', key)`, `('handle', drawing index, point index)` with point
        index -1 for the whole drawing) to the `GestureController`, whose host
        (the window, see `attach_host`) snaps, commits and redraws (user,
        2026-10-13). Without a host the view only pans and zooms.
    """
    pad_context_requested = pyqtSignal(str)
    #: Right click on a text: key.
    text_context_requested = pyqtSignal(str)
    #: Right click on a user drawing (outside draw mode): its index.
    drawing_context_requested = pyqtSignal(int)
    #: Outside draw mode: the pointer is over this drawing (-1: none).
    drawing_hovered = pyqtSignal(int)
    #: The text whose anchor square must show changed: its key ("" for none).
    text_hovered = pyqtSignal(str)

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
        #: Main grid step divided by this gives the drawing snap step.
        self._fine_divisor = DEFAULT_FINE_GRID_DIVISOR

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
        """Router hook run before a tool is armed: end a drag and hide the text anchor square."""
        self.cancel_interaction()
        self._report_text_hover("")

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
        if self._grid and self._grid[2] and self._grid[0] > 0:
            step, (ox, oy), _visible = self._grid
            step = step / self._fine_divisor
        else:
            step, ox, oy = float(fallback_step), 0.0, 0.0
        if step <= 0:
            return float(x), float(y)
        return ox + round((x - ox) / step) * step, oy + round((y - oy) / step) * step

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

    def _drawing_at(self, pos):
        """The index of the drawing (or of the drawing a handle belongs to) under a point, else -1."""
        handle = self._handle_at(pos)
        if handle is not None:
            return handle[0]
        for item in self._items_near(pos):
            index = item.data(DRAWING_KEY_ROLE)
            if index is not None:
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
        """@brief Cancel a handle or text drag in progress, without reporting a drop.

        @note For the window, when the drawings list changes while the mouse
            button is still down (Delete, Ctrl+Z, Ctrl+Y, Ctrl+D): the drag
            holds indices into the old list, and its release would move a
            different drawing. The controller tells the host once
            (`gesture_end(commit=False)`: a dragged text goes back where it
            was, decision D5) and the release that follows does nothing.
            Panning and draw mode are untouched: they hold no index.
        """
        if self.gestures is not None:
            self.gestures.cancel()

    def mousePressEvent(self, event):
        """@brief Begin a text drag on a text, else camera panning, on a left or middle press.

        @param event Qt mouse press event.
        @note With a draw tool armed the router sees the press first: a left
            press is a drawing point and a right press closes the shape; a
            press during a camera pan is swallowed; the middle button falls
            through to the camera.
        """
        if self.click_router is not None and self.click_router.press(event):
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
                self.drawing_context_requested.emit(drawing_index)
                event.accept()
                return
            for item in self.scene().items(self.mapToScene(event.pos())):
                pad_index = item.data(0x504144)
                if pad_index is not None:
                    self.pad_context_requested.emit(str(pad_index))
                    event.accept()
                    return
        if event.button() == Qt.MouseButton.LeftButton:
            # Priority: a visible handle (the smallest, most deliberate
            # target), then a text (Reference/Value sit above the drawings:
            # a label inside a rectangle must stay draggable), then the
            # body of a drawing.
            handle = self._handle_at(event.pos())
            item, key = (None, None) if handle is not None else self._text_item_at(event.pos())
            point = self.mapToScene(event.pos())
            if item is not None and self.gestures is not None and self.gestures.press(('text', key), point):
                # The dragged text keeps its anchor square wherever the
                # pointer goes, as a dragged property in the symbol editor.
                self._report_text_hover(key)
                event.accept()
                return
            if handle is None:
                # The body of a drawing moves the whole drawing, like an
                # element of the symbol editor (user, 2026-10-13).
                index = self._drawing_at(event.pos())
                handle = (index, -1) if index >= 0 else None
            if (handle is not None and self.gestures is not None
                    and self.gestures.press(('handle', *handle), point)):
                # The window moves the point (or the whole drawing) on the
                # grid and redraws the shape at every step.
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
        if self._dragging():
            self.gestures.move(self.mapToScene(event.pos()))
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
            super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """@brief Qt override: the snap marker and the hover handles go away with the pointer.

        @param event Qt leave event, forwarded to the base class.
        """
        if self.click_router is not None and self._draw_mode:
            self.click_router.leave()
        if self._handle_drag is None:
            self._report_hover(-1)
        if self._text_drag is None:
            self._report_text_hover("")
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
        if self.click_router is not None and self.click_router.release(event):
            return
        if self.camera.pan_active and event.button() == self.camera.pan_button:
            self.camera.pan_end(event.pos())
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """@brief Qt override: in draw mode a left double click closes the shape.

        @param event Qt mouse double-click event.
        @note A fast second click reaches Qt as press, release, DOUBLE CLICK:
            the second press never arrives. The router therefore first counts
            it as that missing point (fixed-count shapes close on it), QUIETLY,
            then as the finish request (which ends an open line; it is a
            no-op when the point already closed the shape). A repeated point
            is not added twice and, being quiet, shows no refusal: a true
            double click on the last vertex of a line just finishes it.
        """
        if self.click_router is not None and self.click_router.double_click(event):
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        """@brief Qt override: in draw mode Esc cancels and Enter closes the shape.

        @param event Qt key press event.
        @note The router sees the key first (armed: Escape cancels a pan, else the
            tool; Enter finishes). Escape also cancels a camera pan in progress, as the symbol
            editor does (2026-10-17): the camera stays where it is, the
            release that follows does nothing, and the key is consumed, so
            it does not also cancel an armed drawing tool. With no pan
            running, Escape reaches the draw tool as before.
        """
        if self.click_router is not None and self.click_router.key(event):
            return
        if event.key() == Qt.Key.Key_Escape and self.camera.pan_cancel():
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        """@brief Discard an unfinished camera drag, and cancel a text or handle drag, when the view is hidden.

        @param event Qt hide event, forwarded to the base class.
        @note The cancel filter already cancels on the Hide event; the direct
            call here covers a `hideEvent` invoked without the event loop
            (tests) and is a no-op when the drag is already gone. The host
            puts the dragged text back and drops the handle ghost.
        """
        self.camera.pan_cancel()
        self.cancel_interaction()
        self._report_text_hover("")
        if self.click_router is not None:
            # The host disarms the tool and cleans its click markers.
            self.click_router.hide(event.spontaneous())
        super().hideEvent(event)
