"""@brief Footprint navigation: camera changes never modify geometry; only the texts can be dragged."""
import math

from PyQt6.QtCore import Qt, QLineF, QPointF, QRect, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QGraphicsView

#: Item data role carrying the key of a draggable text (Reference, Value).
TEXT_KEY_ROLE = 0x544558

#: Item data role carrying the index of a user drawing of the footprint.
DRAWING_KEY_ROLE = 0x445257

#: Item data role of a drawing handle: "<drawing index>:<point index>";
#: point index -1 means the WHOLE drawing (a press on its body).
HANDLE_KEY_ROLE = 0x48444C

#: Divisor of the main grid giving the fine drawing grid (user, 2026-10-13:
#: «una griglia puntinata a un decimo di quella attuale per ancorare gli
#: elementi grafici»). Pads and pins stay on the main grid.
DEFAULT_FINE_GRID_DIVISOR = 10

#: Fine-grid dots are drawn only when their spacing on screen reaches this
#: many pixels: below it the dots merge into a smear. A legibility
#: threshold of the display, not a geometric quantity.
FINE_GRID_MIN_SCREEN_PX = 5.0

#: Colours of the main grid lines and of the fine-grid dots (dark preview).
GRID_LINE_COLOR = '#36364b'
GRID_DOT_COLOR = '#4a4a63'

#: Grab tolerance around the pointer, screen px: a press this close to a
#: handle or to a drawing stroke still takes it. A UI tolerance (strokes
#: and handles are a few px wide at any zoom), not geometry.
HIT_MARGIN_PX = 4

#: Handles stay visible while the pointer is within this many screen px of
#: the hovered drawing's bounds, as the symbol editor's HANDLE_HOVER_MARGIN:
#: without it the handles vanish on the way from the stroke to a handle.
HOVER_KEEP_MARGIN_PX = 12


class FootprintView(QGraphicsView):
    """@brief Navigate the footprint with anchored zoom and mouse panning; drag its texts.

    @note Geometry is never changed here: a dragged text only moves its item
        and reports the drop point (`text_moved`); the window decides, saves
        and redraws (user, 2026-10-13).
    """
    pad_context_requested = pyqtSignal(str)
    #: A text was dropped: key and its anchor (the item's position), scene units.
    text_moved = pyqtSignal(str, float, float)
    #: Right click on a text: key.
    text_context_requested = pyqtSignal(str)
    #: Draw mode: a left click landed at this scene point.
    draw_clicked = pyqtSignal(float, float)
    #: Draw mode: the user asked to close the shape (double click, Enter, right click).
    draw_finish_requested = pyqtSignal()
    #: Draw mode: the user cancelled the shape in progress (Esc).
    draw_cancelled = pyqtSignal()
    #: Right click on a user drawing (outside draw mode): its index.
    drawing_context_requested = pyqtSignal(int)
    #: Draw mode: the pointer is at this scene point (snap marker and ghost shape).
    draw_hovered = pyqtSignal(float, float)
    #: Draw mode: the pointer left the viewport.
    draw_hover_left = pyqtSignal()
    #: Outside draw mode: the pointer is over this drawing (-1: none).
    drawing_hovered = pyqtSignal(int)
    #: A handle was pressed: drawing index, point index (-1: whole drawing), scene point.
    handle_pressed = pyqtSignal(int, int, float, float)
    #: A handle is being dragged: drawing index, point index, scene point.
    handle_dragged = pyqtSignal(int, int, float, float)
    #: A handle was dropped: drawing index, point index, scene point.
    handle_dropped = pyqtSignal(int, int, float, float)

    def __init__(self, scene):
        super().__init__(scene)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setSceneRect(-1000000, -1000000, 2000000, 2000000)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pan = None
        self._grid = None
        #: (item, key, press scene point, item position at press) while a text is dragged.
        self._text_drag = None
        #: True while a palette tool collects clicks: left clicks become
        #: points instead of pans, and the keyboard closes or cancels.
        self._draw_mode = False
        #: Viewport point of the left press in draw mode, to tell a drag
        #: (press here, release elsewhere) from a plain click.
        self._draw_press = None
        # Hover without a button pressed: the snap marker follows the pointer.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        #: (drawing index, point index) of the handle being dragged, or None.
        self._handle_drag = None
        #: Drawing index last reported as hovered (-1: none).
        self._hovered_drawing = -1
        #: Scene bounds of the hovered drawing (handles kept while the
        #: pointer stays within them plus HOVER_KEEP_MARGIN_PX), or None.
        self._hover_bounds = None
        #: Main grid step divided by this gives the drawing snap step.
        self._fine_divisor = DEFAULT_FINE_GRID_DIVISOR
        #: True while the camera follows the footprint (every redraw fits
        #: it); False once the user zooms or pans, as the symbol editor's
        #: `auto_fit`: a redraw after an edit must not move the camera
        #: (user, 2026-10-13: «non mantiene lo zoom e lo cambia»).
        self.auto_fit = True

    def set_draw_mode(self, active):
        """@brief Enter or leave drawing mode; the cursor shows the change.

        @param active True routes left clicks to `draw_clicked` and the
            keyboard to finish/cancel; False restores panning and drags.
        """
        self._draw_mode = bool(active)
        if self._draw_mode:
            self._pan = None
            self._text_drag = None
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.setFocus()
        else:
            self.viewport().unsetCursor()

    def set_grid(self, step, origin=(0.0, 0.0), visible=True):
        """@brief Set an unbounded, scene-anchored reference grid in scene units.

        @param step Grid pitch in scene units (mm).
        @param origin Scene point the grid passes through (the first pad).
        @param visible False hides the grid without forgetting it.
        """
        self._grid = (float(step), tuple(map(float, origin)), bool(visible))
        self.viewport().update()

    def grid_step(self):
        """@brief The pitch of the visible main grid in scene units, or None when hidden/unset."""
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
        """@brief The divisor of the main grid that gives the drawing grid."""
        return self._fine_divisor

    def fine_step(self):
        """@brief The drawing-grid pitch in scene units, or None when the grid is hidden/unset."""
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
        """@brief Qt override: draw grid lines across the visible viewport, like the symbol editor.

        @param painter Painter in scene coordinates.
        @param rect Exposed scene rectangle.
        """
        super().drawBackground(painter, rect)
        if not self._grid or not self._grid[2]:
            return
        step, (origin_x, origin_y), _ = self._grid
        if step <= 0:
            return
        grid_pen = QPen(QColor(GRID_LINE_COLOR))
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
        self._draw_fine_dots(painter, rect, step, origin_x, origin_y)
        painter.restore()

    def _draw_fine_dots(self, painter, rect, step, origin_x, origin_y):
        """Dots at the fine-grid nodes, only when legible on screen (see FINE_GRID_MIN_SCREEN_PX)."""
        if not self.fine_dots_visible():
            return
        n = self._fine_divisor
        fine = step / n
        dot_pen = QPen(QColor(GRID_DOT_COLOR), 1.5)
        dot_pen.setCosmetic(True)
        dot_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(dot_pen)
        # Nodes on a main line are marked by the line itself: skip them.
        i0, i1 = math.floor((rect.left() - origin_x) / fine), math.ceil((rect.right() - origin_x) / fine)
        j0, j1 = math.floor((rect.top() - origin_y) / fine), math.ceil((rect.bottom() - origin_y) / fine)
        xs = [origin_x + i * fine for i in range(i0, i1 + 1) if i % n]
        ys = [origin_y + j * fine for j in range(j0, j1 + 1) if j % n]
        painter.drawPoints([QPointF(x, y) for x in xs for y in ys])

    def fine_dots_visible(self):
        """@brief True when the fine-grid dots are drawn at the current zoom."""
        fine = self.fine_step()
        return (fine is not None and self._fine_divisor > 1
                and fine * self.transform().m11() >= FINE_GRID_MIN_SCREEN_PX)

    def zoom(self, factor, anchor=None):
        """@brief Scale the camera within limits while preserving the scene point under the anchor."""
        anchor = anchor if anchor is not None else self.viewport().rect().center()
        self.auto_fit = False
        before = self.mapToScene(anchor)
        current = self.transform().m11()
        target = max(.02, min(40., current * factor))
        self.scale(target / current, target / current)
        delta = before - self.mapToScene(anchor)
        self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)

    def wheelEvent(self, event):
        """@brief Zoom toward the mouse position using the wheel delta."""
        self.zoom(1.2 ** (event.angleDelta().y() / 120), event.position().toPoint())
        event.accept()

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

        The hit may be a child (the text or its anchor cross): the drag moves
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

    def _within_hover_area(self, pos):
        """True while a viewport point stays near the hovered drawing's bounds."""
        if self._hover_bounds is None:
            return False
        area = self.mapFromScene(self._hover_bounds).boundingRect()
        m = HOVER_KEEP_MARGIN_PX
        return area.adjusted(-m, -m, m, m).contains(pos)

    def _report_hover(self, index):
        """Emit `drawing_hovered` only when the hovered drawing changes."""
        if index != self._hovered_drawing:
            self._hovered_drawing = index
            self._hover_bounds = self._drawing_bounds(index) if index >= 0 else None
            self.drawing_hovered.emit(index)

    def hovered_drawing(self):
        """@brief The index of the drawing whose handles are shown (-1: none)."""
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
        """@brief End a handle or text drag in progress, without reporting a drop.

        @note For the window, when the drawings list changes while the mouse
            button is still down (Delete, Ctrl+Z, Ctrl+Y, Ctrl+D): the drag
            holds indices into the old list, and its release would move a
            different drawing. The release that follows then does nothing.
            Panning and draw mode are untouched: they hold no index.
        """
        dragging = self._handle_drag is not None or self._text_drag is not None
        self._handle_drag = None
        self._text_drag = None
        if dragging:
            self.viewport().unsetCursor()

    def mousePressEvent(self, event):
        """@brief Begin a text drag on a text, else camera panning, on a left or middle press.

        @note In draw mode a left press is a drawing point and a right press
            asks to close the shape; panning stays on the middle button.
        """
        if self._draw_mode:
            if event.button() == Qt.MouseButton.LeftButton:
                self._draw_press = event.pos()
                point = self.mapToScene(event.pos())
                self.draw_clicked.emit(point.x(), point.y())
                event.accept()
                return
            if event.button() == Qt.MouseButton.RightButton:
                self.draw_finish_requested.emit()
                event.accept()
                return
        if event.button() == Qt.MouseButton.RightButton:
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
            if item is not None:
                self._text_drag = (item, key, self.mapToScene(event.pos()), item.pos())
                self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
                event.accept()
                return
            if handle is None:
                # The body of a drawing moves the whole drawing, like an
                # element of the symbol editor (user, 2026-10-13).
                index = self._drawing_at(event.pos())
                handle = (index, -1) if index >= 0 else None
            if handle is not None:
                # The window moves the point (or the whole drawing) on the
                # grid and redraws the shape.
                self._handle_drag = handle
                self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
                point = self.mapToScene(event.pos())
                self.handle_pressed.emit(*handle, point.x(), point.y())
                event.accept()
                return
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self._pan = event.pos()
            self._pan_button = event.button()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """@brief Move the dragged text, or pan the camera, without changing footprint geometry."""
        if self._text_drag is not None:
            item, _key, start, origin = self._text_drag
            try:
                item.setPos(origin + (self.mapToScene(event.pos()) - start))
            except RuntimeError:
                # The scene was redrawn under the drag (crash report
                # 2026-10-13 08:29): the item is gone, so is the drag.
                self._text_drag = None
                self.viewport().unsetCursor()
            event.accept()
            return
        if self._pan is not None:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            if not delta.isNull():
                self.auto_fit = False       # the user placed the camera
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = event.pos()
            event.accept()
        elif self._draw_mode:
            point = self.mapToScene(event.pos())
            self.draw_hovered.emit(point.x(), point.y())
            event.accept()
        elif self._handle_drag is not None:
            point = self.mapToScene(event.pos())
            self.handle_dragged.emit(*self._handle_drag, point.x(), point.y())
            event.accept()
        else:
            # Hovering a drawing reveals its handles (user, 2026-10-13: «come
            # nell'altro tab»); they stay while the pointer remains near the
            # drawing (hysteresis, as the symbol editor's reveal area), so a
            # handle can be reached from the stroke.
            index = self._drawing_at(event.pos())
            if index < 0 and self._within_hover_area(event.pos()):
                index = self._hovered_drawing
            self._report_hover(index)
            super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """@brief Qt override: the snap marker and the hover handles go away with the pointer."""
        if self._draw_mode:
            self.draw_hover_left.emit()
        if self._handle_drag is None:
            self._report_hover(-1)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief Report the dropped text, or apply the final pan displacement; release the cursor.

        @note In draw mode a release away from its press is a DRAG: the
            release point counts as the next drawing point, so a rectangle
            or a circle can be drawn press-move-release as in a CAD, not
            only click-click (user, 2026-10-13: «non funziona»). A release
            at the press point is a plain click and adds nothing.
        """
        if self._handle_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            drawing, point_index = self._handle_drag
            self._handle_drag = None
            self.viewport().unsetCursor()
            point = self.mapToScene(event.pos())
            self.handle_dropped.emit(drawing, point_index, point.x(), point.y())
            event.accept()
            return
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            press, self._draw_press = self._draw_press, None
            if press is not None and (event.pos() - press).manhattanLength()                     >= QApplication.startDragDistance():
                point = self.mapToScene(event.pos())
                self.draw_clicked.emit(point.x(), point.y())
            event.accept()
            return
        if self._text_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            item, key, start, origin = self._text_drag
            self._text_drag = None
            self.viewport().unsetCursor()
            try:
                item.setPos(origin + (self.mapToScene(event.pos()) - start))
                self.text_moved.emit(key, item.pos().x(), item.pos().y())
            except RuntimeError:
                pass  # redrawn under the drag: nothing to report
            event.accept()
            return
        if self._pan is not None and event.button() == self._pan_button:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = None
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """@brief Qt override: in draw mode a left double click closes the shape.

        @note A fast second click reaches Qt as press, release, DOUBLE CLICK:
            the second press never arrives. The double click therefore first
            counts as that missing point (fixed-count shapes close on it),
            then as the finish request (which ends an open line; it is a
            no-op when the point already closed the shape). The window
            de-duplicates a repeated point, so a true double click on the
            last vertex of a line does not add it twice.
        """
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            point = self.mapToScene(event.pos())
            self.draw_clicked.emit(point.x(), point.y())
            self.draw_finish_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        """@brief Qt override: in draw mode Esc cancels and Enter closes the shape."""
        if self._draw_mode:
            if event.key() == Qt.Key.Key_Escape:
                self.draw_cancelled.emit()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.draw_finish_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        """@brief Discard an unfinished camera or text drag when the view is hidden."""
        self._pan = None
        self._text_drag = None
        self._handle_drag = None
        if self._draw_mode:
            # The window disarms the tool and cleans its click markers.
            self.draw_cancelled.emit()
        self.viewport().unsetCursor()
        super().hideEvent(event)
