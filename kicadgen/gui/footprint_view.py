"""@brief Footprint navigation: camera changes never modify geometry; only the texts can be dragged."""
import math

from PyQt6.QtCore import Qt, QLineF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QGraphicsView

#: Item data role carrying the key of a draggable text (Reference, Value).
TEXT_KEY_ROLE = 0x544558


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

    def set_grid(self, step, origin=(0.0, 0.0), visible=True):
        """@brief Set an unbounded, scene-anchored reference grid in scene units.

        @param step Grid pitch in scene units (mm).
        @param origin Scene point the grid passes through (the first pad).
        @param visible False hides the grid without forgetting it.
        """
        self._grid = (float(step), tuple(map(float, origin)), bool(visible))
        self.viewport().update()

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
        grid_pen = QPen(QColor('#36364b'))
        grid_pen.setCosmetic(True)
        painter.save()
        for index in range(math.floor((rect.left() - origin_x) / step),
                           math.ceil((rect.right() - origin_x) / step) + 1):
            painter.setPen(grid_pen)
            x = origin_x + index * step
            painter.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
        for index in range(math.floor((rect.top() - origin_y) / step),
                           math.ceil((rect.bottom() - origin_y) / step) + 1):
            painter.setPen(grid_pen)
            y = origin_y + index * step
            painter.drawLine(QLineF(rect.left(), y, rect.right(), y))
        painter.restore()

    def zoom(self, factor, anchor=None):
        """@brief Scale the camera within limits while preserving the scene point under the anchor."""
        anchor = anchor if anchor is not None else self.viewport().rect().center()
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

    def _text_item_at(self, pos):
        """The draggable text group under a viewport point, with its key, or (None, None).

        The hit may be a child (the text or its anchor cross): the drag moves
        the top-level item, whose position is the anchor.
        """
        for item in self.scene().items(self.mapToScene(pos)):
            key = item.data(TEXT_KEY_ROLE)
            if key is not None:
                while item.parentItem() is not None:
                    item = item.parentItem()
                return item, str(key)
        return None, None

    def mousePressEvent(self, event):
        """@brief Begin a text drag on a text, else camera panning, on a left or middle press."""
        if event.button() == Qt.MouseButton.RightButton:
            item, key = self._text_item_at(event.pos())
            if item is not None:
                self.text_context_requested.emit(key)
                event.accept()
                return
            for item in self.scene().items(self.mapToScene(event.pos())):
                pad_index = item.data(0x504144)
                if pad_index is not None:
                    self.pad_context_requested.emit(str(pad_index))
                    event.accept()
                    return
        if event.button() == Qt.MouseButton.LeftButton:
            item, key = self._text_item_at(event.pos())
            if item is not None:
                self._text_drag = (item, key, self.mapToScene(event.pos()), item.pos())
                self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
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
            item.setPos(origin + (self.mapToScene(event.pos()) - start))
            event.accept()
            return
        if self._pan is not None:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = event.pos()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief Report the dropped text, or apply the final pan displacement; release the cursor."""
        if self._text_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            item, key, start, origin = self._text_drag
            self._text_drag = None
            item.setPos(origin + (self.mapToScene(event.pos()) - start))
            self.viewport().unsetCursor()
            self.text_moved.emit(key, item.pos().x(), item.pos().y())
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

    def hideEvent(self, event):
        """@brief Discard an unfinished camera or text drag when the view is hidden."""
        self._pan = None
        self._text_drag = None
        self.viewport().unsetCursor()
        super().hideEvent(event)
