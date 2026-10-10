"""@brief Read-only footprint navigation; camera changes never modify geometry."""
import math

from PyQt6.QtCore import Qt, QLineF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QGraphicsView


class FootprintView(QGraphicsView):
    """@brief Navigate a read-only footprint with anchored zoom and mouse panning."""
    pad_context_requested = pyqtSignal(str)

    def __init__(self, scene):
        super().__init__(scene)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setSceneRect(-1000000, -1000000, 2000000, 2000000)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pan = None
        self._grid = None

    def set_grid(self, step, origin=(0.0, 0.0), visible=True):
        """Set an unbounded, scene anchored reference grid in scene units."""
        self._grid = (float(step), tuple(map(float, origin)), bool(visible))
        self.viewport().update()

    def drawBackground(self, painter, rect):
        """Draw grid lines across the visible viewport, like the symbol editor."""
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

    def mousePressEvent(self, event):
        """@brief Begin camera panning on a left or middle button press."""
        if event.button() == Qt.MouseButton.RightButton:
            for item in self.scene().items(self.mapToScene(event.pos())):
                pad_index = item.data(0x504144)
                if pad_index is not None:
                    self.pad_context_requested.emit(str(pad_index))
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
        """@brief Pan the camera during a drag without changing footprint geometry."""
        if self._pan is not None:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = event.pos()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief Apply the final pan displacement and release the drag cursor."""
        if self._pan is not None and event.button() == self._pan_button:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = None
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        """@brief Discard an unfinished camera drag when the view is hidden."""
        self._pan = None
        self.viewport().unsetCursor()
        super().hideEvent(event)
