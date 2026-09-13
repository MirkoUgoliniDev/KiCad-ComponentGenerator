"""Manual review of a raw, board-aligned PCB crop; depends only on PyQt6.

Integration::

    dialog = PhotoReviewDialog(png, components, body_length_mm, body_width_mm, parent)
    dialog.components_applied.connect(update_components)
    dialog.exec()

Only Apply emits a detached corrected list. ``get_components()`` returns a
detached snapshot of the working copy. Neither editing nor Cancel touches the
caller's dictionaries (including nested metadata).

The crop edges correspond to the PCB body edges: origin at the board center,
X right, Y down, positive rotation clockwise in the photo. Length and width
remain LOCAL dimensions, including at arbitrary angles. Source metadata is
preserved; an existing normalized full-board ``_box`` cache is refreshed after
geometry edits. Source-crop ``bbox`` metadata is left as detection provenance.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from html import escape
import math

from PyQt6.QtCore import QPointF, QRectF, QSignalBlocker, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGraphicsItem, QGraphicsPolygonItem, QGraphicsScene, QGraphicsSimpleTextItem,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QVBoxLayout, QWidget,
)


@dataclass(frozen=True)
class PhotoTransform:
    """Continuous edge coordinates: pixel (W/2, H/2) is millimeter (0, 0)."""

    pixel_width: int
    pixel_height: int
    body_length_mm: float
    body_width_mm: float

    def __post_init__(self):
        if not all(math.isfinite(v) and v > 0 for v in (
            self.pixel_width, self.pixel_height, self.body_length_mm, self.body_width_mm
        )):
            raise ValueError("Photo and PCB dimensions must be finite and positive")

    def mm_to_pixel(self, x: float, y: float) -> QPointF:
        """@brief Provide the mm to pixel operation for this workflow."""
        return QPointF((x / self.body_length_mm + .5) * self.pixel_width,
                       (y / self.body_width_mm + .5) * self.pixel_height)

    def pixel_to_mm(self, x: float, y: float) -> QPointF:
        """@brief Provide the pixel to mm operation for this workflow."""
        return QPointF((x / self.pixel_width - .5) * self.body_length_mm,
                       (y / self.pixel_height - .5) * self.body_width_mm)

    def local_polygon(self, component: dict) -> QPolygonF:
        """Rotate in millimeters BEFORE applying the independent pixel scales."""
        angle = math.radians(float(component.get("rotation_deg", 0)))
        c, s = math.cos(angle), math.sin(angle)
        half_l = float(component["length_mm"]) / 2
        half_w = float(component["width_mm"]) / 2
        sx = self.pixel_width / self.body_length_mm
        sy = self.pixel_height / self.body_width_mm
        return QPolygonF([
            QPointF((x*c - y*s)*sx, (x*s + y*c)*sy)
            for x, y in ((-half_l, -half_w), (half_l, -half_w),
                         (half_l, half_w), (-half_l, half_w))
        ])

    def normalized_box(self, component: dict) -> tuple[float, float, float, float]:
        """Axis-aligned full-board envelope for the inventory's ``_box`` cache."""
        center = self.mm_to_pixel(float(component["x_mm"]), float(component["y_mm"]))
        bounds = self.local_polygon(component).translated(center).boundingRect()
        return (bounds.left() / self.pixel_width, bounds.top() / self.pixel_height,
                bounds.right() / self.pixel_width, bounds.bottom() / self.pixel_height)


class _PhotoView(QGraphicsView):
    """Left drag moves components; middle drag pans without changing selection."""

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.RenderHint.Antialiasing |
                            QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#20242a"))
        self._pan_position = None
        self._fit_scale = 1.0
        self._fit_rect = None
        self._auto_fit = True
        self._resize_fit_timer = QTimer(self)
        self._resize_fit_timer.setSingleShot(True)
        self._resize_fit_timer.timeout.connect(self._apply_fit)

    def fit_photo(self, rect):
        """@brief Provide the fit photo operation for this workflow."""
        self._fit_rect = QRectF(rect)
        self._auto_fit = True
        self._apply_fit()

    def _apply_fit(self):
        if not self._auto_fit or self._fit_rect is None:
            return
        self.fitInView(self._fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = self.transform().m11()

    def resizeEvent(self, event):
        """@brief Provide the resizeEvent operation for this workflow."""
        super().resizeEvent(event)
        if self._auto_fit:
            # Scrollbar visibility can resize the viewport during fitInView;
            # defer/coalesce the next fit instead of recursing in resizeEvent.
            self._resize_fit_timer.start(0)

    def zoom(self, factor):
        """@brief Provide the zoom operation for this workflow."""
        self._auto_fit = False
        current = self.transform().m11()
        target = min(self._fit_scale * 64, max(self._fit_scale / 8, current * factor))
        self.scale(target / current, target / current)

    def wheelEvent(self, event):
        """@brief Provide the wheelEvent operation for this workflow."""
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(1.2 ** max(-4, min(4, delta / 120)))
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        """@brief Provide the mousePressEvent operation for this workflow."""
        if event.button() == Qt.MouseButton.MiddleButton:
            self._auto_fit = False
            self._pan_position = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """@brief Provide the mouseMoveEvent operation for this workflow."""
        if self._pan_position is not None:
            delta = event.position() - self._pan_position
            self._pan_position = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief Provide the mouseReleaseEvent operation for this workflow."""
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_position is not None:
            self._pan_position = None
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class _NumberLabel(QGraphicsSimpleTextItem):
    """Screen-sized ID with an opaque backing, readable on any PCB color."""

    def __init__(self, parent):
        super().__init__(parent)
        font = self.font()
        font.setBold(True)
        self.setFont(font)
        self.setBrush(QColor("white"))

    def paint(self, painter, option, widget=None):
        """@brief Provide the paint operation for this workflow."""
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#101318"))
        painter.drawRoundedRect(self.boundingRect(), 2, 2)
        painter.restore()
        super().paint(painter, option, widget)


class _ComponentItem(QGraphicsPolygonItem):
    def __init__(self, editor, component):
        super().__init__()
        self.editor = editor
        self.component = component
        self._syncing = False
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.number = _NumberLabel(self)
        self.number.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.number.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.sync_geometry()

    def sync_geometry(self):
        """@brief Provide the sync geometry operation for this workflow."""
        self._syncing = True
        try:
            self.setPolygon(self.editor.photo_transform.local_polygon(self.component))
            self.setPos(self.editor.photo_transform.mm_to_pixel(
                float(self.component["x_mm"]), float(self.component["y_mm"])))
            self.number.setPos(self.polygon().boundingRect().topLeft())
        finally:
            self._syncing = False

    def photo_bounds(self):
        """@brief Provide the photo bounds operation for this workflow."""
        # No pen or label padding: this is the physical package envelope.
        return self.mapToScene(self.polygon()).boundingRect()

    def itemChange(self, change, value):
        """@brief Provide the itemChange operation for this workflow."""
        result = super().itemChange(change, value)
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged
                and not self._syncing):
            self.editor._item_moved(self)
        return result


class PhotoReviewDialog(QDialog):
    """Edit photo inventory without modifying inputs or importing the 3D stack.

    ``photo_png`` must be raw PNG bytes of the full board crop, with body length
    horizontal and body width vertical. Invalid image/calibration/geometry raises
    ValueError. Required geometry keys are x_mm, y_mm, length_mm, width_mm;
    rotation_deg defaults to 0 and height_mm to 1 in the controls if absent.

    Public integration API: ``components_applied(list)``, ``get_components()``,
    and the standard QDialog accepted/rejected result. Apply closes the dialog.
    """

    components_applied = pyqtSignal(list)

    _GEOMETRY_FIELDS = ("x_mm", "y_mm", "length_mm", "width_mm", "rotation_deg")
    _NUMERIC_FIELDS = (
        ("x_mm", "Centro X"), ("y_mm", "Centro Y"),
        ("length_mm", "Lunghezza locale"), ("width_mm", "Larghezza locale"),
        ("height_mm", "Altezza"), ("rotation_deg", "Rotazione"),
    )

    def __init__(self, photo_png: bytes, components: list[dict],
                 body_length_mm: float, body_width_mm: float, parent=None):
        super().__init__(parent)
        self.i18n = getattr(parent, "i18n", None)
        pixmap = QPixmap()
        if not isinstance(photo_png, bytes) or not pixmap.loadFromData(photo_png, "PNG"):
            raise ValueError("A valid raw PCB crop PNG is required")
        try:
            self.photo_transform = PhotoTransform(pixmap.width(), pixmap.height(),
                                                  float(body_length_mm), float(body_width_mm))
            self._components = deepcopy(components)
            if not isinstance(self._components, list):
                raise ValueError("Components must be a list of dictionaries")
            for component in self._components:
                if not isinstance(component, dict):
                    raise ValueError("Components must be dictionaries")
                for key, _ in self._NUMERIC_FIELDS:
                    default = 0 if key == "rotation_deg" else 1
                    value = float(component.get(key, default) if key in ("height_mm", "rotation_deg")
                                  else component[key])
                    if not math.isfinite(value) or (key in ("length_mm", "width_mm", "height_mm") and value <= 0):
                        raise ValueError(f"Invalid component {key}")
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid PCB dimensions or component geometry") from exc
        self._items = []
        self._applied = False
        self.setWindowTitle("Revisione componenti da foto")
        self.resize(1120, 740)
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Seleziona un rettangolo o una voce; trascina per spostare. "
            "Modifica le quote per ridimensionare.\n"
            "Rotella: zoom · Tasto centrale: panoramica · "
            "Origine: centro PCB, X destra, Y basso; rotazione oraria.")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        toolbar = QHBoxLayout()
        self.add_button = QPushButton("Aggiungi componente")
        self.delete_button = QPushButton("Elimina selezionato")
        self.fit_button = QPushButton("Adatta foto")
        self.zoom_in_button = QPushButton("Zoom +")
        self.zoom_out_button = QPushButton("Zoom −")
        for button in (self.add_button, self.delete_button, self.fit_button,
                       self.zoom_in_button, self.zoom_out_button):
            button.setAutoDefault(False)
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.scene = QGraphicsScene(self)
        self.photo_item = self.scene.addPixmap(pixmap)
        self.photo_item.setZValue(-100)
        self.photo_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.photo_rect = QRectF(0, 0, pixmap.width(), pixmap.height())
        self.view = _PhotoView(self.scene, self)
        content = QHBoxLayout()
        content.addWidget(self.view, 1)
        sidebar = QVBoxLayout()
        self.component_list = QListWidget()
        self.component_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        sidebar.addWidget(self.component_list, 1)
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        self.text_fields = {}
        for key, label in (("name", "Nome"), ("kind", "Tipo"), ("marking", "Marcatura")):
            field = QLineEdit()
            field.setObjectName(key)
            field.editingFinished.connect(lambda key=key: self._text_edited(key))
            form.addRow(label, field)
            self.text_fields[key] = field
        self.fields = {}
        for key, label in self._NUMERIC_FIELDS:
            field = QDoubleSpinBox()
            field.setObjectName(key)
            field.setDecimals(6)
            field.setRange(.000001 if key in ("length_mm", "width_mm", "height_mm") else -1e9, 1e9)
            field.setSingleStep(1 if key == "rotation_deg" else .1)
            field.setSuffix(" °" if key == "rotation_deg" else " mm")
            field.setKeyboardTracking(False)
            field.valueChanged.connect(lambda value, key=key: self._number_edited(key, value))
            form.addRow(label, field)
            self.fields[key] = field
        sidebar.addWidget(self.form_widget)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        sidebar.addWidget(self.warning_label)
        legend = QLabel("Verde: rilevato · Arancione: da verificare\nRosso: fuori PCB · Tratteggio: selezionato")
        legend.setWordWrap(True)
        sidebar.addWidget(legend)
        side_widget = QWidget()
        side_widget.setLayout(sidebar)
        side_widget.setMinimumWidth(320)
        side_widget.setMaximumWidth(420)
        content.addWidget(side_widget)
        layout.addLayout(content, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply |
                                       QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Applica correzioni")
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.add_button.clicked.connect(self.add_component)
        self.delete_button.clicked.connect(self.delete_selected)
        self.fit_button.clicked.connect(self.fit_photo)
        self.zoom_in_button.clicked.connect(lambda: self.view.zoom(1.2))
        self.zoom_out_button.clicked.connect(lambda: self.view.zoom(1 / 1.2))
        self.component_list.currentRowChanged.connect(self._select_row)
        self.scene.selectionChanged.connect(self._selection_changed)
        for component in self._components:
            self._append_item(component)
        self._update_scene_rect()
        self._select_row(0 if self._items else -1)
        self._selection_changed()

    def get_components(self) -> list[dict]:
        """Return an independent snapshot of the current working inventory."""
        return deepcopy(self._components)

    def showEvent(self, event):
        """@brief Provide the showEvent operation for this workflow."""
        super().showEvent(event)
        self.fit_photo()

    def fit_photo(self):
        """@brief Provide the fit photo operation for this workflow."""
        self.view.fit_photo(self.photo_rect)

    def _append_item(self, component):
        item = _ComponentItem(self, component)
        self._items.append(item)
        self.scene.addItem(item)
        with QSignalBlocker(self.component_list):
            self.component_list.addItem("")
        self._refresh_item(item)
        return item

    def _selected_item(self):
        return next((item for item in self._items if item.isSelected()), None)

    def _select_row(self, row):
        with QSignalBlocker(self.scene):
            for index, item in enumerate(self._items):
                item.setSelected(index == row)
        self._selection_changed()
        if 0 <= row < len(self._items):
            self.view.ensureVisible(self._items[row])

    def _selection_changed(self):
        selected = self._selected_item()
        # Ctrl-click may select multiple graphics items; this editor edits one.
        if selected:
            with QSignalBlocker(self.scene):
                for item in self._items:
                    item.setSelected(item is selected)
        with QSignalBlocker(self.component_list):
            self.component_list.setCurrentRow(self._items.index(selected) if selected else -1)
        self.form_widget.setEnabled(selected is not None)
        self.delete_button.setEnabled(selected is not None)
        if selected:
            for key, field in self.fields.items():
                with QSignalBlocker(field):
                    field.setValue(float(selected.component.get(key, 0 if key == "rotation_deg" else 1)))
            for key, field in self.text_fields.items():
                field.setText(str(selected.component.get(key, "")))
        else:
            self.warning_label.setText(self._tr("Seleziona o aggiungi un componente."))
        for item in self._items:
            self._refresh_item(item)

    def _warnings(self, item):
        warnings = []
        if not self.photo_rect.adjusted(-1e-7, -1e-7, 1e-7, 1e-7).contains(item.photo_bounds()):
            warnings.append(self._tr("Fuori dai bordi PCB: verificare posizione e dimensioni."))
        component = item.component
        if component.get("_uncertain") or str(component.get("confidence", "")).lower() == "low":
            warnings.append(self._tr("Rilevamento incerto / bassa confidenza: verifica manuale richiesta."))
        if component.get("warnings"):
            warnings.append(str(component["warnings"]))
        return warnings

    def _refresh_item(self, item):
        index = self._items.index(item)
        warnings = self._warnings(item)
        outside = not self.photo_rect.adjusted(-1e-7, -1e-7, 1e-7, 1e-7).contains(item.photo_bounds())
        color = QColor("#ef5350" if outside else "#ffad33" if warnings else "#40dc80")
        pen = QPen(color, 3 if item.isSelected() else 2)
        pen.setCosmetic(True)
        if item.isSelected():
            pen.setStyle(Qt.PenStyle.DashLine)
        item.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(45 if item.isSelected() else 15)
        item.setBrush(QBrush(fill))
        item.setZValue(2 if item.isSelected() else 1)
        item.number.setText(str(index + 1))
        name = item.component.get("name") or item.component.get("kind") or self._tr("Componente")
        text = f"{index + 1}. {name}" + (" [!]" if warnings else "")
        self.component_list.item(index).setText(text)
        self.component_list.item(index).setForeground(color)
        # QGraphicsItem tooltips interpret HTML, so escape supplied metadata.
        item.setToolTip(escape(text + ("\n" + "\n".join(warnings) if warnings else "")))
        if item.isSelected():
            if item.component.get('_height_source'):
                warnings.append(self._tr("Altezza indicativa: verificare con vista laterale o misura."))
            self.warning_label.setText(
                "\n".join(warnings)
                if warnings else self._tr("Nessun avviso per questo componente.")
            )

    def _tr(self, source):
        """@brief Translate runtime status text without modifying component data."""
        return self.i18n.text(source) if self.i18n is not None else source

    def _update_scene_rect(self):
        rect = QRectF(self.photo_rect)
        for item in self._items:
            rect = rect.united(item.photo_bounds())
        margin = max(self.photo_rect.width(), self.photo_rect.height()) * .1
        self.scene.setSceneRect(rect.adjusted(-margin, -margin, margin, margin))

    def _geometry_changed(self, item):
        if "_box" in item.component:
            bounds = item.photo_bounds()
            item.component["_box"] = (
                bounds.left() / self.photo_rect.width(), bounds.top() / self.photo_rect.height(),
                bounds.right() / self.photo_rect.width(), bounds.bottom() / self.photo_rect.height(),
            )
        self._refresh_item(item)
        self._update_scene_rect()

    def _item_moved(self, item):
        point = self.photo_transform.pixel_to_mm(item.pos().x(), item.pos().y())
        item.component.update(x_mm=point.x(), y_mm=point.y())
        if item.isSelected():
            for key, value in (("x_mm", point.x()), ("y_mm", point.y())):
                with QSignalBlocker(self.fields[key]):
                    self.fields[key].setValue(value)
        self._geometry_changed(item)

    def _number_edited(self, key, value):
        item = self._selected_item()
        if item is None:
            return
        item.component[key] = value
        if key in self._GEOMETRY_FIELDS:
            item.sync_geometry()
            self._geometry_changed(item)

    def _text_edited(self, key):
        item = self._selected_item()
        if item is not None:
            value = self.text_fields[key].text()
            if value != str(item.component.get(key, "")):
                item.component[key] = value
                self._refresh_item(item)

    def add_component(self):
        """Add a generic package at the visible center, then select its controls."""
        center = self.view.mapToScene(self.view.viewport().rect().center())
        center = self.photo_transform.pixel_to_mm(
            min(self.photo_rect.width(), max(0, center.x())),
            min(self.photo_rect.height(), max(0, center.y())))
        length = min(2.0, self.photo_transform.body_length_mm / 10)
        width = min(1.0, self.photo_transform.body_width_mm / 10)
        center.setX(max(-self.photo_transform.body_length_mm / 2 + length / 2,
                        min(self.photo_transform.body_length_mm / 2 - length / 2, center.x())))
        center.setY(max(-self.photo_transform.body_width_mm / 2 + width / 2,
                        min(self.photo_transform.body_width_mm / 2 - width / 2, center.y())))
        component = dict(name=self._tr("Nuovo componente"), kind="generic", x_mm=center.x(), y_mm=center.y(),
                         length_mm=length, width_mm=width, height_mm=1.0, rotation_deg=0.0,
                         confidence="low", _uncertain=True)
        self._components.append(component)
        self._append_item(component)
        self._update_scene_rect()
        self._select_row(len(self._items) - 1)
        self.text_fields["name"].setFocus()
        self.text_fields["name"].selectAll()

    def delete_selected(self):
        """@brief Provide the delete selected operation for this workflow."""
        item = self._selected_item()
        if item is None:
            return
        index = self._items.index(item)
        with QSignalBlocker(self.scene), QSignalBlocker(self.component_list):
            self.scene.removeItem(item)
            self._items.pop(index)
            self._components.pop(index)
            self.component_list.takeItem(index)
        self._select_row(min(index, len(self._items) - 1))
        self._update_scene_rect()

    def accept(self):
        """@brief Provide the accept operation for this workflow."""
        if self._applied:
            return
        # Commit text still being typed, including calls made without focus loss.
        for field in self.fields.values():
            field.interpretText()
        for key in self.text_fields:
            self._text_edited(key)
        self._applied = True
        self.components_applied.emit(self.get_components())
        super().accept()
