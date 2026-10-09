"""Read-only footprint navigation; camera changes never modify geometry."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsView


class FootprintView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setSceneRect(-1000000, -1000000, 2000000, 2000000)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pan = None

    def zoom(self, factor, anchor=None):
        anchor = anchor if anchor is not None else self.viewport().rect().center()
        before = self.mapToScene(anchor)
        current = self.transform().m11()
        target = max(.02, min(40., current * factor))
        self.scale(target / current, target / current)
        delta = before - self.mapToScene(anchor)
        self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)

    def wheelEvent(self, event):
        self.zoom(1.2 ** (event.angleDelta().y() / 120), event.position().toPoint())
        event.accept()

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self._pan = event.pos()
            self._pan_button = event.button()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan is not None:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = event.pos()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan is not None and event.button() == self._pan_button:
            delta = self.mapToScene(self._pan) - self.mapToScene(event.pos())
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
            self._pan = None
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        self._pan = None
        self.viewport().unsetCursor()
        super().hideEvent(event)
