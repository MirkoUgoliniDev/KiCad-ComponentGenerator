"""@brief Manual review of a raw, board-aligned PCB crop; depends only on PyQt6.

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
preserved except for evidence tied to an edited photograph rectangle:
``bbox_basis`` becomes unknown and ``package_evidence`` is discarded after a
position or size edit, while ``terminal_geometry`` is discarded after any
geometry edit including rotation. A rotation alone keeps the boundary
evidence: turning the outline does not change whether it traces the body or
the body with its leads. An existing normalized
full-board ``_box`` cache is refreshed. Source-crop ``bbox`` metadata is left
as detection provenance. Height and text edits do not change that rectangle.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from html import escape
import math
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, QSignalBlocker, Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGraphicsItem, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QVBoxLayout, QWidget, QApplication, QMessageBox,
)


class _MatchWorker(QThread):
    """@brief Compute CAD candidates off the UI thread from a detached snapshot."""
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, component):
        super().__init__(QApplication.instance())
        self.component = deepcopy(component)

    def run(self):
        """@brief Find candidates locally; no AI requests are made."""
        try:
            from ..cad3d.kicad_models import model_candidates
            # A previous rejection/selection controls generation, not whether
            # the user may reopen this dialog to explore alternatives.
            proposal = deepcopy(self.component)
            proposal.pop('model_selection', None)
            self.done.emit(model_candidates(proposal, limit=5))
        except Exception as exc:
            self.failed.emit(str(exc))


@dataclass(frozen=True)
class PhotoTransform:
    """@brief Board-crop calibration: pixel (W/2, H/2) is millimeter (0, 0).

    The crop edges are the PCB body edges, so the horizontal and vertical
    pixel scales are independent (an anisotropic photo is calibrated, not
    corrected). Coordinates are continuous edge coordinates, not pixel
    centers: pixel column 0 starts at -length/2 mm.
    """

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
        """@brief Map board millimeters (center origin, Y down) to crop pixels.

        @param x Millimeters along the board length, positive to the right.
        @param y Millimeters along the board width, positive downward.
        @return Continuous pixel position in the crop.
        """
        return QPointF((x / self.body_length_mm + .5) * self.pixel_width,
                       (y / self.body_width_mm + .5) * self.pixel_height)

    def pixel_to_mm(self, x: float, y: float) -> QPointF:
        """@brief Inverse of mm_to_pixel: crop pixels to board millimeters.

        @param x Horizontal pixel position in the crop.
        @param y Vertical pixel position in the crop.
        @return Board point in millimeters, origin at the board center.
        """
        return QPointF((x / self.pixel_width - .5) * self.body_length_mm,
                       (y / self.pixel_height - .5) * self.body_width_mm)

    def local_to_item_pixel(self, rotation_deg: float, u: float, v: float) -> QPointF:
        """@brief Map a point of the component's LOCAL frame to item pixels.

        @param rotation_deg Clockwise rotation of the component in the photo.
        @param u Coordinate along the local length axis, in millimeters.
        @param v Coordinate along the local width axis, in millimeters.
        @return Pixel offset from the component center (the item origin).
        @note The rotation is applied in millimeters BEFORE the independent
              horizontal/vertical pixel scales, so anisotropic photos keep
              the metric right angle between the two local axes.
        """
        angle = math.radians(float(rotation_deg))
        c, s = math.cos(angle), math.sin(angle)
        sx = self.pixel_width / self.body_length_mm
        sy = self.pixel_height / self.body_width_mm
        return QPointF((u*c - v*s)*sx, (u*s + v*c)*sy)

    def item_pixel_to_local(self, rotation_deg: float, x: float, y: float) -> QPointF:
        """@brief Inverse of local_to_item_pixel: item pixels to LOCAL millimeters.

        @param rotation_deg Clockwise rotation of the component in the photo.
        @param x Horizontal pixel offset from the component center.
        @param y Vertical pixel offset from the component center.
        @return Point (u, v) along the local length and width axes, in millimeters.
        """
        angle = math.radians(float(rotation_deg))
        c, s = math.cos(angle), math.sin(angle)
        dx = x / (self.pixel_width / self.body_length_mm)
        dy = y / (self.pixel_height / self.body_width_mm)
        return QPointF(dx*c + dy*s, -dx*s + dy*c)

    def local_polygon(self, component: dict) -> QPolygonF:
        """@brief Outline of a component around its own center, in item pixels.

        @param component Dictionary with length_mm, width_mm and rotation_deg.
        @return Four corners, from (-l/2, -w/2) clockwise in the local frame.
        @note The rectangle is rotated in millimeters BEFORE the independent
              pixel scales are applied, so it keeps its metric right angles
              on an anisotropic photo.
        """
        rotation = float(component.get("rotation_deg", 0))
        half_l = float(component["length_mm"]) / 2
        half_w = float(component["width_mm"]) / 2
        return QPolygonF([
            self.local_to_item_pixel(rotation, x, y)
            for x, y in ((-half_l, -half_w), (half_l, -half_w),
                         (half_l, half_w), (-half_l, half_w))
        ])

    def normalized_box(self, component: dict) -> tuple[float, float, float, float]:
        """@brief Axis-aligned envelope of the outline as fractions of the crop.

        @param component Dictionary with x_mm, y_mm, length_mm, width_mm, rotation_deg.
        @return (left, top, right, bottom) in [0, 1] of the crop size, the
                format of the inventory's normalized ``_box`` cache.
        """
        center = self.mm_to_pixel(float(component["x_mm"]), float(component["y_mm"]))
        bounds = self.local_polygon(component).translated(center).boundingRect()
        return (bounds.left() / self.pixel_width, bounds.top() / self.pixel_height,
                bounds.right() / self.pixel_width, bounds.bottom() / self.pixel_height)


class _PhotoView(QGraphicsView):
    """@brief Photo canvas: left drag moves outlines, middle drag pans, wheel zooms.

    The view auto-fits the photo until the user zooms or pans; a manual
    zoom is then preserved across window resizes until fit is requested.
    """

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
        """@brief Fit a scene rectangle in the viewport and re-enable auto-fit on resize.

        @param rect Scene rectangle to show whole, normally the photo bounds.
        """
        self._fit_rect = QRectF(rect)
        self._auto_fit = True
        self._apply_fit()

    def _apply_fit(self):
        if not self._auto_fit or self._fit_rect is None:
            return
        self.fitInView(self._fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = self.transform().m11()

    def resizeEvent(self, event):
        """@brief Keep the photo fitted on viewport resizes while auto-fit is on."""
        super().resizeEvent(event)
        if self._auto_fit:
            # Scrollbar visibility can resize the viewport during fitInView;
            # defer/coalesce the next fit instead of recursing in resizeEvent.
            self._resize_fit_timer.start(0)

    def zoom(self, factor):
        """@brief Scale the view by a factor, clamped between 1/8 and 64 times the fit scale.

        @param factor Multiplier applied to the current scale; >1 zooms in.
        @note Any zoom ends auto-fit: the user's scale survives resizes.
        """
        self._auto_fit = False
        current = self.transform().m11()
        target = min(self._fit_scale * 64, max(self._fit_scale / 8, current * factor))
        self.scale(target / current, target / current)

    def wheelEvent(self, event):
        """@brief Zoom around the pointer with the wheel; other wheel input scrolls."""
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(1.2 ** max(-4, min(4, delta / 120)))
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        """@brief Start a middle-button pan; other buttons reach the scene items."""
        if event.button() == Qt.MouseButton.MiddleButton:
            self._auto_fit = False
            self._pan_position = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """@brief Scroll the viewport by the pointer motion while panning."""
        if self._pan_position is not None:
            delta = event.position() - self._pan_position
            self._pan_position = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief End a middle-button pan and restore the cursor."""
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_position is not None:
            self._pan_position = None
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class _NumberLabel(QGraphicsSimpleTextItem):
    """@brief Screen-sized ID label with an opaque backing, readable on any PCB color."""

    def __init__(self, parent):
        super().__init__(parent)
        font = self.font()
        font.setBold(True)
        self.setFont(font)
        self.setBrush(QColor("white"))

    def paint(self, painter, option, widget=None):
        """@brief Paint a dark rounded backing behind the white text."""
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#101318"))
        painter.drawRoundedRect(self.boundingRect(), 2, 2)
        painter.restore()
        super().paint(painter, option, widget)


## Smallest local length/width (mm) a handle drag may leave behind. Not a
## limit on what the numeric fields accept: it only stops a stroke from
## collapsing the outline to nothing or pulling one edge past the opposite
## one, which would flip the rectangle. 0.2 mm is the narrowest standard chip
## package (01005: 0.4 x 0.2 mm), so no real body on a PCB is smaller and a
## shorter extent is an overshoot rather than a measurement.
MIN_HANDLE_SIZE_MM = 0.2

## Half side of a resize handle in device pixels: the handle ignores the view
## transform so it stays grabbable at every zoom level.
HANDLE_HALF_PX = 4.0

## Handle screen angles are quantized to 45 degree sectors: Qt only offers
## horizontal, vertical and the two diagonal resize cursors.
_CURSOR_SECTOR_DEG = 45.0

## Colour of the halo drawn behind the selected outline. White is chosen for
## contrast against the dark soldermask and the coloured status pen alike:
## the halo never carries status, it only says "this one is selected".
SELECTION_HALO_COLOR = "#ffffff"

## Cosmetic pen widths in device pixels: the halo must stay clearly wider
## than the status pen it surrounds at every zoom level, otherwise a thick
## status line would hide the highlight.
SELECTED_PEN_WIDTH_PX = 3
UNSELECTED_PEN_WIDTH_PX = 2
SELECTION_HALO_WIDTH_PX = 9


class _ResizeHandle(QGraphicsRectItem):
    """@brief Screen-sized grip on one edge or corner of a selected component.

    The handle is identified by its LOCAL edge signs ``(eu, ev)`` in
    {-1, 0, 1}: ``(1, 0)`` is the far end of the length axis, ``(0, -1)``
    the near side of the width axis, ``(1, 1)`` the corner between them.
    Handles follow the component rotation, so the same handle can sit on any
    side of the screen; the geometry it edits is always the local one.
    """

    ## All eight edge/corner identities, corners first so that a tiny
    ## rectangle still exposes them above the mid-edge grips.
    EDGES = ((-1, -1), (1, -1), (1, 1), (-1, 1), (1, 0), (-1, 0), (0, 1), (0, -1))

    def __init__(self, item, edge):
        super().__init__(-HANDLE_HALF_PX, -HANDLE_HALF_PX, 2*HANDLE_HALF_PX, 2*HANDLE_HALF_PX, item)
        self.item = item
        self.edge = edge
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        # Above the number label so the top-left grip is always visible.
        self.setZValue(1)
        pen = QPen(QColor("#101318"), 1)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor("white")))
        self.hide()

    def update_cursor(self):
        """@brief Pick the resize cursor that matches the on-screen edge normal.

        @note The outward local direction is mapped through the rotation and
              the anisotropic pixel scales, then quantized to 45 degree
              sectors: a corner of a 37 degree component may legitimately show
              a horizontal or vertical arrow.
        """
        rotation = float(self.item.component.get("rotation_deg", 0))
        direction = self.item.editor.photo_transform.local_to_item_pixel(
            rotation, self.edge[0], self.edge[1])
        angle = math.degrees(math.atan2(direction.y(), direction.x())) % 180
        sector = int(round(angle / _CURSOR_SECTOR_DEG)) % 4
        # Screen Y grows downward: 45 degrees is the "\" diagonal (SizeFDiag).
        self.setCursor((Qt.CursorShape.SizeHorCursor, Qt.CursorShape.SizeFDiagCursor,
                        Qt.CursorShape.SizeVerCursor, Qt.CursorShape.SizeBDiagCursor)[sector])

    def mousePressEvent(self, event):
        """@brief Start a resize; accepting here keeps the parent from moving."""
        self.item.begin_resize(self.edge, event.scenePos())
        event.accept()

    def mouseMoveEvent(self, event):
        """@brief Follow the pointer, editing the component live."""
        self.item.resize_to(event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event):
        """@brief Finish the resize stroke."""
        self.item.end_resize()
        event.accept()


class _ComponentItem(QGraphicsPolygonItem):
    """@brief Selectable, movable and resizable outline of one component."""

    def __init__(self, editor, component):
        super().__init__()
        self.editor = editor
        self.component = component
        self._syncing = False
        self._resize = None
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.number = _NumberLabel(self)
        self.number.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.number.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # Selection halo: a wide, high-contrast outline drawn just behind the
        # status-coloured outline. A child of the item, so it moves, rotates
        # and stacks with it, but behind the parent so the status pen stays
        # legible on top of it.
        self.halo = QGraphicsPolygonItem(self)
        self.halo.setFlag(QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent)
        self.halo.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        halo_pen = QPen(QColor(SELECTION_HALO_COLOR), SELECTION_HALO_WIDTH_PX)
        halo_pen.setCosmetic(True)
        halo_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        self.halo.setPen(halo_pen)
        self.halo.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.halo.hide()
        self.handles = {edge: _ResizeHandle(self, edge) for edge in _ResizeHandle.EDGES}
        self.sync_geometry()

    def sync_geometry(self):
        """@brief Rebuild the item from the component dictionary (the single source of truth).

        Polygon, halo, position, number label and the eight resize handles
        are all derived from x_mm/y_mm/length_mm/width_mm/rotation_deg; the
        halo mirrors the outline exactly so the highlight never lags an edit,
        and the handles are re-placed on the rotated local edges with a
        cursor matching their on-screen direction.
        @note ``_syncing`` suppresses the position feedback in itemChange:
              a setPos issued here is a mirror of the data, not a user move.
        """
        self._syncing = True
        try:
            self.setPolygon(self.editor.photo_transform.local_polygon(self.component))
            self.halo.setPolygon(self.polygon())
            self.setPos(self.editor.photo_transform.mm_to_pixel(
                float(self.component["x_mm"]), float(self.component["y_mm"])))
            self.number.setPos(self.polygon().boundingRect().topLeft())
            rotation = float(self.component.get("rotation_deg", 0))
            half_l = float(self.component["length_mm"]) / 2
            half_w = float(self.component["width_mm"]) / 2
            for (eu, ev), handle in self.handles.items():
                handle.setPos(self.editor.photo_transform.local_to_item_pixel(
                    rotation, eu*half_l, ev*half_w))
                handle.update_cursor()
        finally:
            self._syncing = False

    def handle(self, edge):
        """@brief Return the grip for a local edge/corner, e.g. ``(1, 0)``.

        @param edge Pair of local signs ``(eu, ev)`` as in _ResizeHandle.EDGES.
        @return The _ResizeHandle child item.
        """
        return self.handles[edge]

    def begin_resize(self, edge, scene_pos):
        """@brief Record the fixed edges and the grab offset of a resize stroke.

        @param edge Local edge/corner signs ``(eu, ev)`` being dragged.
        @param scene_pos Scene point where the pointer grabbed the handle.
        @note The pointer rarely sits exactly on the edge, so the offset
              between the grab point and the edge is kept in LOCAL millimeters
              and the edge follows the pointer without an initial jump.
        """
        component = self.component
        rotation = float(component.get("rotation_deg", 0))
        half_l = float(component["length_mm"]) / 2
        half_w = float(component["width_mm"]) / 2
        origin = self.editor.photo_transform.mm_to_pixel(
            float(component["x_mm"]), float(component["y_mm"]))
        grab = self.editor.photo_transform.item_pixel_to_local(
            rotation, scene_pos.x() - origin.x(), scene_pos.y() - origin.y())
        self._resize = dict(
            edge=edge, rotation=rotation, origin=origin,
            center=(float(component["x_mm"]), float(component["y_mm"])),
            u=(-half_l, half_l), v=(-half_w, half_w),
            offset=(edge[0]*half_l - grab.x(), edge[1]*half_w - grab.y()))

    def resize_to(self, scene_pos):
        """@brief Move the grabbed edges to the pointer, keeping the others fixed.

        @param scene_pos Current pointer position in scene pixels.
        @note Works in the LOCAL frame of the stroke's starting rectangle:
              each grabbed axis gets a new extent, clamped at
              MIN_HANDLE_SIZE_MM from the opposite edge, and the new center is
              rotated back to board millimeters. Length and width therefore
              stay local at any rotation, cardinal or not.
        """
        if self._resize is None:
            return
        state = self._resize
        transform = self.editor.photo_transform
        pointer = transform.item_pixel_to_local(
            state["rotation"], scene_pos.x() - state["origin"].x(), scene_pos.y() - state["origin"].y())
        pointer = (pointer.x() + state["offset"][0], pointer.y() + state["offset"][1])
        extents = []
        for axis, sign in enumerate(state["edge"]):
            low, high = state[("u", "v")[axis]]
            if sign > 0:
                high = max(pointer[axis], low + MIN_HANDLE_SIZE_MM)
            elif sign < 0:
                low = min(pointer[axis], high - MIN_HANDLE_SIZE_MM)
            extents.append((low, high))
        (u0, u1), (v0, v1) = extents
        cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
        angle = math.radians(state["rotation"])
        c, s = math.cos(angle), math.sin(angle)
        values = dict(length_mm=u1 - u0, width_mm=v1 - v0,
                      x_mm=state["center"][0] + cu*c - cv*s,
                      y_mm=state["center"][1] + cu*s + cv*c)
        if all(values[key] == float(self.component[key]) for key in values):
            return
        self.component.update(values)
        self.sync_geometry()
        self.editor._item_resized(self, values)

    def end_resize(self):
        """@brief Drop the stroke state; the component already holds the result."""
        self._resize = None

    def photo_bounds(self):
        """@brief Axis-aligned scene envelope of the physical package outline.

        @return Bounding rectangle in scene (crop) pixels.
        """
        # No pen or label padding: this is the physical package envelope.
        return self.mapToScene(self.polygon()).boundingRect()

    def mousePressEvent(self, event):
        """@brief Select the clicked outline alone, with or without Ctrl.

        @note Qt treats Ctrl-click as "extend the selection", which would
              leave two outlines selected and the editor bound to the lowest
              list index rather than the clicked one. This editor edits one
              component at a time, so the click always resolves to the
              outline under the pointer; the default handling then only sets
              up the move.
        """
        if not self.isSelected():
            self.editor._select_row(self.editor._items.index(self), center=False)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief Keep the clicked outline selected after a Ctrl-click.

        @note Without movement Qt toggles the item on a Ctrl release, which
              would clear the selection of the outline that was just clicked.
        """
        super().mouseReleaseEvent(event)
        if not self.isSelected():
            self.editor._select_row(self.editor._items.index(self), center=False)

    def mouseDoubleClickEvent(self, event):
        """@brief Open KiCad alternatives for the clicked photographic component.

        @note The outline is already under the pointer: the view is not
              re-centred on it, that would move the photo under the user.
        """
        self.editor._select_row(self.editor._items.index(self), center=False)
        self.editor.choose_model()
        event.accept()

    def itemChange(self, change, value):
        """@brief Feed user moves back into the data and show grips only when selected.

        A position change caused by dragging (not by sync_geometry) updates
        the component's x_mm/y_mm through the editor; a selection change
        toggles the resize handles, because hidden handles take no mouse
        events and neighbours must be moved, never resized, by mistake.
        """
        result = super().itemChange(change, value)
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged
                and not self._syncing):
            self.editor._item_moved(self)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # Only the selected outline exposes grips: hidden handles take no
            # mouse events, so neighbours are moved, not resized, by mistake.
            for handle in self.handles.values():
                handle.setVisible(bool(value))
        return result


class PhotoReviewDialog(QDialog):
    """@brief Edit the photo inventory without modifying inputs or importing the 3D stack.

    ``photo_png`` must be raw PNG bytes of the full board crop, with body length
    horizontal and body width vertical. Invalid image/calibration/geometry raises
    ValueError. Required geometry keys are x_mm, y_mm, length_mm, width_mm;
    rotation_deg defaults to 0 and height_mm to 1 in the controls if absent.

    Public integration API: ``components_applied(list)``, ``get_components()``,
    and the standard QDialog accepted/rejected result. Apply closes the dialog.

    Layout contract: the sidebar is dominated by ``component_list`` with
    ``count_label`` ("Componenti: N") above it; the parameter form
    (``form_widget`` holding ``text_fields`` and ``fields``) lives in the
    window-modal ``parameters_dialog`` opened by ``parameters_button``
    or ``open_parameters()``. The field widgets are the same objects whether
    or not that modal is shown, and editing them updates the outline live.
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
        self._closing = False
        self._match_worker = None
        self.setWindowTitle("Revisione componenti da foto")
        self.resize(1120, 740)
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Seleziona un rettangolo o una voce; trascina per spostare. "
            "Trascina le maniglie per ridimensionare; «Parametri…» apre quote e testi.\n"
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
        # The list is the sidebar's main content: the count sits above it and
        # the list takes all the vertical space the compact status rows leave.
        self.count_label = QLabel()
        count_font = self.count_label.font()
        count_font.setBold(True)
        self.count_label.setFont(count_font)
        sidebar.addWidget(self.count_label)
        self.component_list = QListWidget()
        self.component_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        sidebar.addWidget(self.component_list, 1)
        # Parameter form: built once, then re-parented into the modal below.
        # ``fields``/``text_fields`` stay the same objects, so live editing
        # works whether or not the modal is shown.
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        self.text_fields = {}
        # Gli slot sono metodi legati, non closure: se l'applicazione esce
        # con la finestra ancora aperta, il distruttore C++ non passa da
        # hideEvent/closeEvent e il QLineEdit a fuoco emette editingFinished
        # dal proprio distruttore. PyQt scioglie da solo le connessioni a
        # metodi legati quando il ricevente muore, mentre una lambda con
        # default gia' smantellati verrebbe chiamata e farebbe abortire il
        # processo con TypeError. La chiave viene dall'objectName del mittente.
        for key, label in (("name", "Nome"), ("kind", "Tipo"), ("marking", "Marcatura")):
            field = QLineEdit()
            field.setObjectName(key)
            field.editingFinished.connect(self._text_field_finished)
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
            field.valueChanged.connect(self._number_field_changed)
            form.addRow(label, field)
            self.fields[key] = field
        self.parameters_dialog = QDialog(self)
        self.parameters_dialog.setWindowTitle("Parametri componente")
        parameters_layout = QVBoxLayout(self.parameters_dialog)
        parameters_layout.addWidget(self.form_widget)
        self.parameters_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        self.parameters_buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Applica")
        self.parameters_buttons.accepted.connect(self.parameters_dialog.accept)
        parameters_layout.addWidget(self.parameters_buttons)
        # Edits are live, so closing the modal by any route is a focus change,
        # not an undo: pending text is committed on OK and on Escape alike.
        self.parameters_dialog.finished.connect(self._commit_pending_text)
        self.parameters_button = QPushButton("Parametri…")
        self.parameters_button.setAutoDefault(False)
        self.parameters_button.clicked.connect(self.open_parameters)
        sidebar.addWidget(self.parameters_button)
        self.model_button = QPushButton("Scegli modello KiCad…")
        self.model_button.setAutoDefault(False)
        self.model_button.clicked.connect(self.choose_model)
        sidebar.addWidget(self.model_button)
        self.model_status = QLabel("Doppio clic su un componente per confrontare i modelli 3D.")
        self.model_status.setWordWrap(True)
        self.model_status.setTextFormat(Qt.TextFormat.PlainText)
        sidebar.addWidget(self.model_status)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        sidebar.addWidget(self.warning_label)
        legend = QLabel("Verde: rilevato · Arancione: da verificare\nRosso: fuori PCB · Bordo bianco spesso: selezionato")
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
        self.component_list.itemDoubleClicked.connect(lambda _item: self.choose_model())
        self.scene.selectionChanged.connect(self._selection_changed)
        for component in self._components:
            self._append_item(component)
        self._update_scene_rect()
        self._update_count()
        self._select_row(0 if self._items else -1)
        self._selection_changed()

    def get_components(self) -> list[dict]:
        """@brief Return an independent snapshot of the current working inventory.

        @return Deep copy of the components; edits to it never reach the dialog.
        """
        return deepcopy(self._components)

    def showEvent(self, event):
        """@brief Fit the whole photo the first time the editor is shown."""
        super().showEvent(event)
        self.fit_photo()

    def hideEvent(self, event):
        """@brief Take the parameter modal down when the editor itself is dismissed.

        @note Only for hides requested by the program (close, reject, hide):
              the window system also hides the editor on minimize, and a
              modal closed then would not come back with ``showNormal()``.
              Spontaneous hides leave the modal alone; Qt minimizes it with
              its parent and restores it with it.
        """
        super().hideEvent(event)
        if not event.spontaneous():
            self._close_parameters()

    def closeEvent(self, event):
        """@brief Close the parameter modal even when the editor was never shown."""
        self._close_parameters()
        super().closeEvent(event)

    def _close_parameters(self):
        """Hide the modal after moving focus out of its editors.

        The modal is a separate window: hiding the editor does not clear the
        focus inside it, so text still being typed would only be committed
        when the editor is destroyed, i.e. never for the user. Clearing focus
        here fires ``editingFinished`` while the outline can still take the
        edit. Crash safety does not depend on this: the slots are bound
        methods, dropped by PyQt when the dialog dies.
        """
        focused = self.parameters_dialog.focusWidget()
        if focused is not None:
            focused.clearFocus()
        self.parameters_dialog.hide()

    def fit_photo(self):
        """@brief Show the whole photo and re-enable auto-fit on resize."""
        self.view.fit_photo(self.photo_rect)

    def _append_item(self, component):
        item = _ComponentItem(self, component)
        self._items.append(item)
        self.scene.addItem(item)
        with QSignalBlocker(self.component_list):
            self.component_list.addItem("")
        self._refresh_item(item)
        self._update_count()
        return item

    def _update_count(self):
        """Show the live number of outlines above the list."""
        self.count_label.setText(self._tr("Componenti: {count}").format(count=len(self._items)))

    def _selected_item(self):
        return next((item for item in self._items if item.isSelected()), None)

    def _select_row(self, row, center=True):
        """Select one outline by list row and bring it into view.

        @param row Index in ``_items``; any other value clears the selection.
        @param center Centre the view on the outline. False keeps the current
               viewport, for selections made by pointing at the outline itself.
        """
        with QSignalBlocker(self.scene):
            for index, item in enumerate(self._items):
                item.setSelected(index == row)
        self._selection_changed()
        if center and 0 <= row < len(self._items):
            # centerOn, not ensureVisible: an outline already inside the
            # viewport would otherwise stay wherever it is, easy to miss on a
            # crowded board.
            self.view.centerOn(self._items[row])

    def _selection_changed(self):
        selected = self._selected_item()
        # Outline clicks already resolve to one item; a rubber band or a
        # programmatic multi-selection still collapses to the first one.
        if selected:
            with QSignalBlocker(self.scene):
                for item in self._items:
                    item.setSelected(item is selected)
        with QSignalBlocker(self.component_list):
            self.component_list.setCurrentRow(self._items.index(selected) if selected else -1)
        self.form_widget.setEnabled(selected is not None)
        self.parameters_button.setEnabled(selected is not None)
        self.delete_button.setEnabled(selected is not None)
        self.model_button.setEnabled(selected is not None and self._match_worker is None)
        if selected:
            self._reload_fields()
        else:
            self.warning_label.setText(self._tr("Seleziona o aggiungi un componente."))
        self._refresh_parameters_title()
        for item in self._items:
            self._refresh_item(item)

    def _reload_fields(self):
        """Show the selected component's values in the form without editing it."""
        selected = self._selected_item()
        if selected is None:
            return
        for key, field in self.fields.items():
            with QSignalBlocker(field):
                field.setValue(float(selected.component.get(key, 0 if key == "rotation_deg" else 1)))
        for key, field in self.text_fields.items():
            field.setText(str(selected.component.get(key, "")))

    def _commit_pending_text(self):
        """Store text and numbers still being typed, as a focus change would."""
        for field in self.fields.values():
            field.interpretText()
        for key in self.text_fields:
            self._text_edited(key)

    def _refresh_parameters_title(self):
        selected = self._selected_item()
        title = self._tr("Parametri componente")
        if selected is not None:
            index = self._items.index(selected)
            name = selected.component.get("name") or selected.component.get("kind") or self._tr("Componente")
            title += f" — {index + 1}. {name}"
        self.parameters_dialog.setWindowTitle(title)

    def open_parameters(self):
        """@brief Show the parameter form for the selected component in a modal.

        @note Window-modal via ``QDialog.open``: input to the editor is
              blocked while the modal is up, but no nested event loop runs,
              so the outline follows every edit live and callers such as
              tests can still drive the fields without the modal ever being
              shown.
        """
        if self._selected_item() is None:
            return
        self._refresh_parameters_title()
        self.parameters_dialog.open()
        self.text_fields["name"].setFocus()
        self.text_fields["name"].selectAll()

    def _warnings(self, item):
        warnings = []
        if not self.photo_rect.adjusted(-1e-7, -1e-7, 1e-7, 1e-7).contains(item.photo_bounds()):
            warnings.append(self._tr("Fuori dai bordi PCB: verificare posizione e dimensioni."))
        component = item.component
        selection = component.get('model_selection') or {}
        if (component.get('model_match') or {}).get('status') == 'import_failed':
            warnings.append(self._tr("Importazione STEP fallita: il 3D mostra un segnaposto, non il modello scelto."))
        if (component.get('model_match') or {}).get('status') == 'auto_matched' and selection.get('status') != 'confirmed':
            warnings.append(self._tr("Modello KiCad applicato automaticamente per compatibilità meccanica: identità e polarità non certificate; doppio clic per modificarlo."))
        elif (component.get('model_match') or {}).get('status') == 'provisional_matched' and selection.get('status') != 'confirmed':
            # Decisione dell'utente (1+3): il provvisorio torna ad agganciare
            # il modello, ma dichiarato come tale, cosi' chi rivede sa che
            # l'identita' viene dalla sola geometria e non da una marcatura.
            warnings.append(self._tr("Modello KiCad applicato provvisoriamente dalla sola geometria: identità non certificata; doppio clic per confermarlo o cambiarlo."))
        elif selection.get('status') != 'confirmed':
            warnings.append(self._tr("Modello KiCad non confermato: doppio clic per scegliere."))
        else:
            try:
                available = Path(selection.get('path', '')).is_file()
            except (OSError, TypeError, ValueError):
                available = False
            if not available:
                warnings.append(self._tr("Il file STEP scelto non è disponibile: scegliere nuovamente."))
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
        # The status pen keeps its colour when selected: the selection is
        # shown by the wide halo behind it, the thicker line and the fill.
        pen = QPen(color, SELECTED_PEN_WIDTH_PX if item.isSelected() else UNSELECTED_PEN_WIDTH_PX)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.halo.setVisible(item.isSelected())
        fill = QColor(color)
        fill.setAlpha(60 if item.isSelected() else 15)
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
                warnings.append(self._tr("Altezza indicativa: verificare con una misura diretta."))
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

    def _geometry_changed(self, item, rotation_only=False):
        # The original pixel region no longer supports its boundary evidence or
        # normalized terminal boxes. Keep package/mating identity, but do not
        # stretch or relocate observed metal as if it had been measured again.
        #
        # Una rotazione e' l'eccezione: sposta e ridimensiona invalidano cio'
        # che il rettangolo racchiude, ruotare no. `bbox_basis` risponde a "il
        # contorno segue il corpo o il corpo coi reofori", e girare lo stesso
        # contorno non cambia quella risposta; lo stesso vale per la
        # descrizione della morfologia osservata. Scriverci "unknown" non
        # significa "non lo so piu'": e' un'affermazione diversa, che manda il
        # componente sul ramo delle piazzole e gli nega il modello nativo.
        if not rotation_only:
            if "bbox_basis" in item.component:
                item.component["bbox_basis"] = "unknown"
            item.component.pop("package_evidence", None)
        item.component.pop("terminal_geometry", None)
        item.component.pop("model_match", None)
        item.component.pop("model_selection", None)
        if "_box" in item.component:
            bounds = item.photo_bounds()
            item.component["_box"] = (
                bounds.left() / self.photo_rect.width(), bounds.top() / self.photo_rect.height(),
                bounds.right() / self.photo_rect.width(), bounds.bottom() / self.photo_rect.height(),
            )
        self._refresh_item(item)
        self._update_scene_rect()

    def _item_moved(self, item):
        if item.pos() == self.photo_transform.mm_to_pixel(
                float(item.component["x_mm"]), float(item.component["y_mm"])):
            return
        point = self.photo_transform.pixel_to_mm(item.pos().x(), item.pos().y())
        item.component.update(x_mm=point.x(), y_mm=point.y())
        if item.isSelected():
            for key, value in (("x_mm", point.x()), ("y_mm", point.y())):
                with QSignalBlocker(self.fields[key]):
                    self.fields[key].setValue(value)
        self._geometry_changed(item)

    def _item_resized(self, item, values):
        """Mirror a handle resize into the side fields and invalidate evidence.

        Same contract as a size typed into the fields: the outline no longer
        encloses the region the photograph evidence was measured on.
        """
        if item.isSelected():
            for key, value in values.items():
                with QSignalBlocker(self.fields[key]):
                    self.fields[key].setValue(value)
        self._geometry_changed(item)

    def _field_key(self, fields):
        """Return the sender's key among ``fields``; None for a direct call or a foreign sender."""
        sender = self.sender()
        key = sender.objectName() if sender is not None else ""
        return key if key in fields else None

    def _number_field_changed(self, value):
        """@brief Store a numeric field edit; the field is recovered from the sender.

        @param value New value of the spin box, in millimeters or degrees.
        @note Bound-method slot on purpose: PyQt drops the connection when
              this dialog's wrapper dies, so a spin box destroyed during an
              abnormal teardown never calls in here (a closure would be
              called with its defaults already torn down and abort).
        """
        key = self._field_key(self.fields)
        if key is not None:
            self._number_edited(key, value)

    def _text_field_finished(self):
        """@brief Store a text field edit; the field is recovered from the sender.

        @note Bound-method slot on purpose: the focused line edit emits
              editingFinished from its destructor when the application quits
              with the modal up, and PyQt then simply drops the connection.
        """
        key = self._field_key(self.text_fields)
        if key is not None:
            self._text_edited(key)

    def _number_edited(self, key, value):
        item = self._selected_item()
        if item is None:
            return
        if value == float(item.component.get(key, 0 if key == "rotation_deg" else 1)):
            return
        item.component[key] = value
        if key in self._GEOMETRY_FIELDS:
            item.sync_geometry()
            self._geometry_changed(item, rotation_only=key == "rotation_deg")

    def _text_edited(self, key):
        item = self._selected_item()
        if item is not None:
            value = self.text_fields[key].text()
            if value != str(item.component.get(key, "")):
                item.component[key] = value
                if key == 'kind':
                    item.component.pop('model_match', None)
                    item.component.pop('model_selection', None)
                self._refresh_item(item)
                self._refresh_parameters_title()

    def choose_model(self):
        """@brief Resolve plausible local CAD models for the selected component asynchronously."""
        item = self._selected_item()
        if item is None or self._match_worker is not None:
            return
        snapshot = deepcopy(item.component)
        worker = _MatchWorker(snapshot)
        self._match_worker = worker
        self.model_button.setEnabled(False)
        self.model_status.setText("Ricerca candidati KiCad e misure STEP…")
        worker.done.connect(lambda report: self._show_model_candidates(item, snapshot, report))
        worker.failed.connect(self._match_failed)
        worker.finished.connect(self._match_finished)
        # A dismissed editor must not destroy a running measurement thread.
        application = QApplication.instance()
        application.aboutToQuit.connect(worker.wait)
        # ...ma la promessa vale finche' il thread esiste. Senza scioglierla,
        # alla chiusura Qt chiamerebbe wait su un oggetto gia' eliminato da
        # deleteLater e l'uscita finirebbe con RuntimeError: wrapped C/C++
        # object of type _MatchWorker has been deleted. Sciolta prima della
        # distruzione, perche' i gestori scattano nell'ordine di connessione.
        worker.finished.connect(lambda: application.aboutToQuit.disconnect(worker.wait))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _match_failed(self, message):
        if not self._closing:
            self.model_status.setText("Ricerca KiCad fallita: " + message)

    def _match_finished(self):
        self._match_worker = None
        if not self._closing:
            self.model_button.setEnabled(self._selected_item() is not None)

    def _show_model_candidates(self, item, snapshot, report):
        if self._closing or item not in self._items or item.component != snapshot:
            return
        from .model_candidates_dialog import ModelCandidatesDialog
        bounds = item.photo_bounds()
        # Context exposes missed leads/tabs and makes a too-small AI box visible.
        margin = max(12, max(bounds.width(), bounds.height()) * .30)
        crop = self.photo_item.pixmap().copy(
            bounds.adjusted(-margin, -margin, margin, margin).intersected(self.photo_rect).toAlignedRect())
        dialog = ModelCandidatesDialog(item.component, report, photo_crop=crop, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selection = deepcopy(dialog.selected_selection())
            previous = item.component.get('model_match') or {}
            same_failure = (previous.get('status') == 'import_failed'
                            and selection == item.component.get('model_selection'))
            if not same_failure:
                item.component['model_match'] = deepcopy(report)
            item.component['model_selection'] = selection
        self.model_status.setText("Candidati pronti. Premi Applica correzioni per salvare le scelte.")
        self._refresh_item(item)

    def reject(self):
        """@brief Discard the editing transaction without applying pending worker results."""
        self._closing = True
        super().reject()

    def add_component(self):
        """@brief Add a generic package at the visible center and select it for naming.

        The new outline is clamped inside the board, sized to a tenth of the
        board (at most 2 x 1 mm), and flagged uncertain: it is a placeholder
        for the user to place and measure, not a detection.
        """
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
        # A new outline has only a placeholder name: the parameter modal
        # follows automatically so the user can type it straight away. A
        # modal over an editor that is not on screen would be orphaned, so
        # a hidden editor (headless callers) only prepares the name field.
        if self.isVisible() and not self.parameters_dialog.isVisible():
            self.open_parameters()
        else:
            self.text_fields["name"].setFocus()
            self.text_fields["name"].selectAll()

    def delete_selected(self):
        """@brief Remove the selected outline and renumber; selection moves to its neighbour."""
        item = self._selected_item()
        if item is None:
            return
        index = self._items.index(item)
        with QSignalBlocker(self.scene), QSignalBlocker(self.component_list):
            self.scene.removeItem(item)
            self._items.pop(index)
            self._components.pop(index)
            self.component_list.takeItem(index)
        self._update_count()
        self._select_row(min(index, len(self._items) - 1))
        self._update_scene_rect()

    def accept(self):
        """@brief Apply: emit a detached corrected list once, then close.

        @note Text still being typed is committed first, so an Apply
              without a focus change loses nothing; a second call is a no-op.
        """
        if self._applied:
            return
        # Commit text still being typed, including calls made without focus loss.
        self._commit_pending_text()
        self._applied = True
        self._closing = True
        self.components_applied.emit(self.get_components())
        super().accept()
