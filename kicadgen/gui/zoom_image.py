"""@brief Read-only image viewer with zoom, used by the document preview.

Same controls as the component review canvas, so the two windows behave
alike: mouse wheel zooms around the pointer, dragging pans, «+» / «-» zoom,
«F» fits the whole image. The zoom percentage is relative to the whole image
in the window (100% = fitted), as in the review canvas.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
                             QLabel, QVBoxLayout, QWidget)

from .review_tools import ZOOM_STEP, tool_button

#: Zoom limits, relative to the fitted image.
MIN_ZOOM = 0.25
MAX_ZOOM = 32.0


class ZoomImageView(QGraphicsView):
    """@brief Graphics view showing one pixmap, fitted until the user zooms.

    @note While `auto_fit` is true the image follows the window size; any
        zoom turns it off, so a chosen scale survives resizes until «fit».
    """

    zoom_changed = pyqtSignal()

    def __init__(self, pixmap: QPixmap, parent=None):
        """@brief Show `pixmap` fitted to the view.

        @param pixmap Image to show; must not be null.
        @param parent Parent widget.
        """
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem(pixmap)
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene().addItem(self._item)
        self.scene().setSceneRect(self._item.boundingRect())
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#11111b"))
        self.auto_fit = True
        self._fit_scale = 1.0

    def _fitted_scale(self) -> float:
        rect = self._item.boundingRect()
        # Area senza barre di scorrimento: quando lo zoom le fa comparire, il
        # riferimento del 100% non deve cambiare (percentuale falsata).
        view = self.maximumViewportSize()
        if rect.isEmpty() or view.isEmpty():
            return 1.0
        return min(view.width() / rect.width(), view.height() / rect.height())

    def fit(self) -> None:
        """@brief Show the whole image and follow the window size again."""
        self.auto_fit = True
        self._fit_scale = self._fitted_scale()
        self.resetTransform()
        self.scale(self._fit_scale, self._fit_scale)
        self.centerOn(self._item)
        self.zoom_changed.emit()

    def zoom(self, factor: float, around_pointer: bool = False) -> None:
        """@brief Multiply the scale by `factor`, within MIN_ZOOM..MAX_ZOOM of fit.

        @param factor >1 zooms in, <1 zooms out.
        @param around_pointer True for the wheel: the point under the mouse
            stays put. Buttons and keys zoom around the centre of the view,
            where the pointer is not (anchoring to it slid the image away).
        """
        current = self.transform().m11()
        wanted = min(max(current * factor, self._fit_scale * MIN_ZOOM),
                     self._fit_scale * MAX_ZOOM)
        if current > 0 and wanted != current:
            self.setTransformationAnchor(
                QGraphicsView.ViewportAnchor.AnchorUnderMouse if around_pointer
                else QGraphicsView.ViewportAnchor.AnchorViewCenter)
            self.scale(wanted / current, wanted / current)
        self.auto_fit = False
        self.zoom_changed.emit()

    def zoom_percent(self) -> int:
        """@brief Current scale relative to the fitted image, in percent."""
        return round(100 * self.transform().m11() / self._fit_scale) if self._fit_scale else 100

    def wheelEvent(self, event):  # noqa: N802 - Qt override
        """@brief Zoom around the pointer, one step per wheel notch."""
        delta = event.angleDelta().y()
        if delta:
            self.zoom(ZOOM_STEP ** max(-4, min(4, delta / 120)), around_pointer=True)
        event.accept()

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        """@brief Keep the image fitted while the user has not zoomed."""
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit()
        else:
            self._fit_scale = self._fitted_scale()
            self.zoom_changed.emit()

    def showEvent(self, event):  # noqa: N802 - Qt override
        """@brief Fit on first show, when the viewport has its real size."""
        super().showEvent(event)
        if self.auto_fit:
            self.fit()


class ZoomImagePanel(QWidget):
    """@brief Toolbar (zoom -, percentage, zoom +, fit) above a ZoomImageView."""

    def __init__(self, pixmap: QPixmap, tr=None, parent=None):
        """@brief Build toolbar, view and keyboard shortcuts.

        @param pixmap Image to show; must not be null.
        @param tr Translation function for the tooltips; identity if None.
        @param parent Parent widget.
        """
        super().__init__(parent)
        translate = tr or (lambda text: text)
        self.view = ZoomImageView(pixmap)
        self.zoom_out_button = tool_button("zoom-out.svg", "Zoom indietro", "-", tr=tr)
        self.zoom_in_button = tool_button(
            "zoom-in.svg", "Zoom +", "+",
            "Anche con la rotella del mouse, attorno al puntatore.", tr=tr)
        self.fit_button = tool_button("view-fit.svg", "Adatta alla finestra", "F",
                                      "Mostra il documento intero nella finestra.", tr=tr)
        self.zoom_label = QLabel()
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(self.zoom_label.fontMetrics().horizontalAdvance("3200%") + 8)
        self.zoom_label.setToolTip(translate("Ingrandimento rispetto al documento intero"))
        self.zoom_label.setProperty("i18n_skip", True)
        self.zoom_out_button.clicked.connect(lambda: self.view.zoom(1 / ZOOM_STEP))
        self.zoom_in_button.clicked.connect(lambda: self.view.zoom(ZOOM_STEP))
        self.fit_button.clicked.connect(self.view.fit)
        self.view.zoom_changed.connect(self._update_label)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        for widget in (self.zoom_out_button, self.zoom_label, self.zoom_in_button,
                       self.fit_button):
            bar.addWidget(widget)
        bar.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)
        for keys, slot in ((("+", "="), lambda: self.view.zoom(ZOOM_STEP)),
                           (("-",), lambda: self.view.zoom(1 / ZOOM_STEP)),
                           (("F",), self.view.fit)):
            for key in keys:
                # Contesto finestra: valgono anche col fuoco sui pulsanti.
                QShortcut(QKeySequence(key), self).activated.connect(slot)
        self._update_label()

    def _update_label(self) -> None:
        self.zoom_label.setText(f"{self.view.zoom_percent()}%")
