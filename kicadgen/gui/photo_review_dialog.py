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

The photograph itself is a view setting, never data: ``photo_state()`` /
``set_photo_state()`` show it whole, attenuate it or take it away so that a
thin outline over a dark, crowded board can be read on its own. Whatever the
state, the board rectangle keeps being drawn and every edit, selection and
measurement behaves identically; the saved inventory is unaffected. The
control that drives it is the ``photo_button`` icon (key P): the icon draws
the state the canvas is in, the first tooltip line is the VERB of the next
step ("Attenua foto", "Nascondi foto", "Mostra foto") and the second line the
current state. See ``_apply_photo_state``.

Bodies the detection declined to type are surfaced here, because this is
where the fix costs one double click. ``type_undetermined()`` reads the
STRUCTURED declaration — ``class`` (or ``kind``) in the explicit unknown
family, or a ``class_confidence`` that does not support the family named —
never the free-text ``uncertainties``, which name components in prose. Such
a body gets its own outline status (UNDETERMINED_TYPE_COLOR), its own list
marker (UNDETERMINED_LIST_MARKER) and a warning naming the manual action.
The inventory's global ``uncertainties``, carried on every component as
``_global_inventory_uncertainties``, are reachable through
``model_uncertainties()`` and the ``uncertainties_button``; they are the
model's prose and are displayed as plain text, as data, never as
instructions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from html import escape
import math
from pathlib import Path

from PyQt6.QtCore import (
    QPointF, QRectF, QSignalBlocker, QStringListModel, Qt, QTimer, QThread,
    pyqtSignal,
)
from PyQt6.QtGui import (QBrush, QColor, QKeySequence, QPainter, QPen, QPixmap,
                         QPolygonF, QShortcut)
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout,
    QGraphicsItem, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget, QApplication, QMessageBox,
)


from .review_tools import (ZOOM_STEP, AnchoredPopup, InfoPopup, asset_icon,
                           give_combos_their_keys, set_tool_texts, tool_button,
                           toolbar_divider)


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

    While draw mode is armed (``set_draw_mode``) the left button no longer
    reaches the outlines, doubled or not: it rubber-bands a new rectangle over
    the photo and reports it through ``rectangle_drawn``. The gesture is
    deliberately inert unless it starts inside ``draw_bounds`` and travels more
    than DRAW_CLICK_PX on at least one axis, so a stray click on a crowded
    board creates nothing.
    """

    ## Scene rectangle the drawing gesture is confined to (the photo), set by
    ## the editor. None disables the check, which is only useful in isolation.
    draw_bounds = None

    ## Emitted on a completed drawing gesture with the scene rectangle drawn,
    ## already clipped to ``draw_bounds``.
    rectangle_drawn = pyqtSignal(QRectF)

    ## Emitted after every change of scale (zoom, fit): the toolbar shows the
    ## percentage of the fitted photo.
    zoom_changed = pyqtSignal()

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._draw_mode = False
        self._draw_origin = None
        self._draw_scene_origin = None
        self._draw_band = None
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
        self.zoom_changed.emit()

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
        self.zoom_changed.emit()

    def zoom_percent(self):
        """@brief Current scale as a percentage of the fitted photo (100 = fitted)."""
        return round(self.transform().m11() / self._fit_scale * 100) if self._fit_scale else 100

    def wheelEvent(self, event):
        """@brief Zoom around the pointer with the wheel; other wheel input scrolls."""
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(ZOOM_STEP ** max(-4, min(4, delta / 120)))
            event.accept()
        else:
            super().wheelEvent(event)

    def set_draw_mode(self, enabled):
        """@brief Arm or disarm the "draw a new component" gesture.

        @param enabled True to take the left button away from the outlines and
               use it to trace a rectangle instead.
        @note Disarming always drops a half-finished band: leaving a dangling
              rubber band on the scene would keep claiming a gesture that the
              user has abandoned.
        """
        self._draw_mode = bool(enabled)
        self._cancel_band()
        self._apply_draw_cursor()

    def _apply_draw_cursor(self):
        """Show the crosshair while armed; restore the normal pointer otherwise.

        Called again after a pan, which borrows the viewport cursor: without
        this, panning while armed would silently leave the pointer looking
        like the mode was off.
        """
        if self._draw_mode:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.viewport().unsetCursor()

    def draw_mode(self) -> bool:
        """@brief Whether the next left drag on the photo traces a new rectangle."""
        return self._draw_mode

    def cancel_draw(self):
        """@brief Abandon a half-traced rectangle, leaving the armed state alone.

        @note For interruptions that are not a decision about the mode: the
              editor being hidden or closed mid-stroke. The release that would
              have completed the gesture may never arrive (the pointer grab
              dies with an unmapped window), so the pending origin must go with
              the band: otherwise a later release would commit a rectangle the
              user never traced.
        """
        self._cancel_band()

    def _cancel_band(self):
        """Forget the current gesture and remove its rubber band from the scene."""
        self._draw_origin = None
        self._draw_scene_origin = None
        if self._draw_band is not None:
            # The band belongs to the scene, not to the component items: it
            # must disappear without disturbing any outline.
            self.scene().removeItem(self._draw_band)
            self._draw_band = None

    def _show_band(self, scene_pos):
        """Draw the dashed preview of the rectangle being traced."""
        if self._draw_band is None:
            self._draw_band = QGraphicsRectItem()
            pen = QPen(QColor(SELECTION_HALO_COLOR), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._draw_band.setPen(pen)
            self._draw_band.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self._draw_band.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            # Above every outline: the band is what the pointer is doing now.
            self._draw_band.setZValue(10)
            self.scene().addItem(self._draw_band)
        self._draw_band.setRect(QRectF(self._draw_scene_origin, scene_pos).normalized())

    def _clipped_to_bounds(self, rect):
        """Clamp each edge of a traced rectangle into ``draw_bounds``, per axis.

        Deliberately not ``QRectF.intersected``: a stroke drawn exactly
        vertically or horizontally on screen has a zero extent on one axis, and
        an empty rectangle intersects nothing, so the clip would return a null
        rectangle and the body would silently appear at the scene origin
        instead of under the pointer. Clamping the edges keeps a degenerate
        axis where the user put it; its size then comes from
        MIN_HANDLE_SIZE_MM, which is the floor's job.
        """
        if self.draw_bounds is None:
            return rect
        bounds = self.draw_bounds

        def clamp(value, low, high):
            return min(max(value, low), high)

        return QRectF(
            QPointF(clamp(rect.left(), bounds.left(), bounds.right()),
                    clamp(rect.top(), bounds.top(), bounds.bottom())),
            QPointF(clamp(rect.right(), bounds.left(), bounds.right()),
                    clamp(rect.bottom(), bounds.top(), bounds.bottom())))

    def mousePressEvent(self, event):
        """@brief Start a middle-button pan; other buttons reach the scene items.

        @note While draw mode is armed the left button starts a rectangle
              instead, and only if it goes down inside the photo: outside it
              there is nothing to measure, so the press is swallowed and the
              mode stays armed for the next attempt.
        """
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            if self.draw_bounds is None or self.draw_bounds.contains(scene_pos):
                self._draw_origin = event.position()
                self._draw_scene_origin = scene_pos
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._auto_fit = False
            self._pan_position = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """@brief Keep the left button on the drawing gesture, doubled or not.

        @note MouseButtonDblClick is a distinct event type from
              MouseButtonPress, so without this override the second press of a
              quick retry would fall through to the outline under the pointer,
              select it and open the model chooser instead of starting a
              stroke. That is exactly what happens after a stroke rejected as
              too short, when the user presses again on the same spot inside
              the double-click interval. While armed the button belongs to the
              gesture, so the doubled press starts one like a single press
              does: swallowing it outright would make the retry silently inert.
        """
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        """@brief Scroll the viewport by the pointer motion while panning.

        @note A gesture in progress updates its rubber band instead, so the
              user sees the rectangle before committing to it. The gesture is
              only alive while the left button is actually held: a move
              arriving with no button pressed means the release happened
              somewhere this view never saw it (a pointer grab broken by the
              window being unmapped, for instance), so the band is dropped
              rather than left following the bare pointer and rather than left
              to be committed by the next stray release.
        """
        if self._draw_scene_origin is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._show_band(self.mapToScene(event.position().toPoint()))
                event.accept()
                return
            self._cancel_band()
        if self._pan_position is not None:
            delta = event.position() - self._pan_position
            self._pan_position = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(delta.y()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """@brief End a middle-button pan and restore the cursor.

        @note A drawing gesture is completed here: only a stroke that stays
              below DRAW_CLICK_PX on BOTH axes is a click that missed, and
              nothing is reported for it. One axis clearing the threshold is
              enough, because a long stroke that is deliberately thin (a
              pin-header edge, a shield seam) is a measurement, not a slip;
              MIN_HANDLE_SIZE_MM then holds the thin side. The rectangle is
              clipped to the photo, so a stroke that ends past the board edge
              still describes a body on the board.
        """
        if (self._draw_mode and event.button() == Qt.MouseButton.LeftButton
                and self._draw_scene_origin is not None):
            origin = self._draw_origin
            rect = QRectF(self._draw_scene_origin,
                          self.mapToScene(event.position().toPoint())).normalized()
            self._cancel_band()
            travel = event.position() - origin
            if abs(travel.x()) >= DRAW_CLICK_PX or abs(travel.y()) >= DRAW_CLICK_PX:
                rect = self._clipped_to_bounds(rect)
                self.rectangle_drawn.emit(rect)
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_position is not None:
            self._pan_position = None
            self._apply_draw_cursor()
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
        """@brief Paint a dark rounded backing behind the white text.

        @note The backing is opaque and darker than both the photograph and
              the neutral board backdrop, so the number stays readable whether
              the photograph is shown, attenuated or taken away.
        """
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(NUMBER_LABEL_BACKING_COLOR))
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

## Minimum pointer travel, in viewport pixels, that makes a stroke on the
## photo a drawn rectangle instead of a click gone wrong. Derived from the
## grip size, not chosen by trial: a box narrower than one whole handle could
## not be grabbed again afterwards, so a stroke that small is a slip of the
## hand and not a measurement. The threshold is on the SCREEN, where the hand
## shakes, and is therefore independent of the photo calibration; the metric
## floor on the resulting body is MIN_HANDLE_SIZE_MM, a different question.
## One axis clearing it is enough: a body can legitimately be long and thin,
## and its thin side is held by MIN_HANDLE_SIZE_MM rather than by this.
##
## Declared consequence of measuring travel on the screen: the dead zone has a
## metric size that depends on the zoom,
##
##     dead zone [mm] = DRAW_CLICK_PX / view_scale * board_mm / crop_px,
##
## so it shrinks as the user zooms in and is at its worst at the minimum zoom
## ``zoom`` allows (fit/8). Measured on the 400 x 100 px crop of a 40 x 20 mm
## board used by the tests, at fit/8 a deliberate 3.3 x 6.7 mm stroke is still
## discarded, and in silence. That is accepted rather than tuned away: a
## smaller threshold would let a shaky click leave a body no grip could take
## hold of again. It is, however, the reason a stroke meant to measure
## something belongs at fit zoom or closer, and the reason this constant must
## not be reasoned about in millimeters.
DRAW_CLICK_PX = 2 * HANDLE_HALF_PX

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

## Opaque backing painted behind every number label. Darker than the board
## backdrop below, so the label reads as a label and not as part of the board.
NUMBER_LABEL_BACKING_COLOR = "#101318"

## Backdrop states of the review canvas, in the order the toolbar control
## cycles through them: the photograph whole, attenuated, then gone.
PHOTO_FULL = "full"
PHOTO_DIMMED = "dimmed"
PHOTO_HIDDEN = "hidden"
PHOTO_STATES = (PHOTO_FULL, PHOTO_DIMMED, PHOTO_HIDDEN)

## Opacity of the attenuated photograph. The board stays recognisable (which
## component each outline sits on is still judgeable, and that IS the review)
## while most of its contrast is given back to the outlines. Low enough that
## the composite over BOARD_BACKDROP_COLOR stays much closer to the backdrop
## than to the photograph, so the guarantee below is only mildly weakened;
## the state that guarantees it outright is PHOTO_HIDDEN.
PHOTO_DIMMED_OPACITY = .25

## Neutral fill drawn over the board area when the photograph is attenuated or
## taken away. Chosen for measurable contrast, not by eye: its relative
## luminance (~0.038) keeps the WCAG contrast ratio above 3:1 against the
## DARKEST of the status colours (red #ef5350, ratio ~3.4) and therefore
## against the other two and the white halo as well. The status colours carry
## meaning and are explained in the legend, so the backdrop is what gets
## chosen around them. It is also lighter than the view background, which is
## what makes the board area, and hence "outside the PCB", still readable.
BOARD_BACKDROP_COLOR = "#32373e"

## Board outline drawn with that backdrop. With the pixmap gone this is the
## only remaining evidence of where the PCB ends, so a component flagged
## "fuori dai bordi PCB" stays judgeable. Grey on purpose: it must not be
## mistaken for a status-coloured component outline nor for the white halo.
BOARD_BORDER_COLOR = "#8a93a0"
BOARD_BORDER_WIDTH_PX = 2

## Declared classes that mean "a body is here, I am not saying what family it
## belongs to". Not a vocabulary invented for this dialog: it is the same set
## ``cad3d.kicad_models`` uses to refuse to certify an identity, so the review
## and the generator agree on what "untyped" means. ``other`` and ``unknown``
## are the schema's own escape hatches; ``semiconductor_unknown`` is the one
## the model is told to use when it can see leads and a tab but will not
## choose between a transistor and a regulator.
UNDETERMINED_TYPE_CLASSES = ("semiconductor_unknown", "unknown", "other")

## Class confidences that do NOT support the family a component declares. The
## consequence downstream is the same as an unknown class: ``kicad_models``
## will not certify an identity, so the body ends up as a generic volume.
UNDETERMINED_TYPE_CONFIDENCES = ("low",)

## Fourth outline status: the model declined to determine this body's type.
## Chosen against the three that already carry meaning, not by taste. Its
## contrast against BOARD_BACKDROP_COLOR is 4.8:1, above the 3:1 floor and
## above the darkest status already accepted (red, 3.4:1). Its hue is the only
## one of the four with a dominant blue channel (0xf9 against 0x50, 0x33 and
## 0x80), so it stays a distinct signal where red and green do not, and it
## cannot be read as "fuori PCB" (red) nor as the generic "da verificare"
## (orange).
UNDETERMINED_TYPE_COLOR = "#c58af9"

## Icon drawing the canvas state, per photo state (same icons as the detector
## review, «Corpi rilevati»).
_PHOTO_STATE_ICONS = {"full": "photo-full.svg", "dimmed": "photo-dimmed.svg",
                      "hidden": "photo-hidden.svg"}

## Second line of the photo command's tooltip, after the current state.
_PHOTO_BUTTON_EXPLANATION = (
    "Su una foto scura e affollata i contorni si leggono meglio quando la fotografia "
    "si fa da parte; il bordo della PCB resta disegnato.")

## First paragraph of the «Come si usa» popup.
_INFO_INTRO = (
    "Correggi l'inventario: sposta, ridimensiona, aggiungi o elimina i rettangoli, "
    "poi «Applica correzioni». «Parametri…» apre quote e testi del componente scelto.")

## Coordinate convention, the last line of the popup.
_INFO_ORIGIN = "Origine: centro PCB, X destra, Y basso; rotazione oraria."

## Marker appended to the list row of such a body. Distinct from the existing
## "[!]", which only says "this row has at least one warning" and is therefore
## on nearly every row before a model is chosen: it could not single this one
## out. A body that is both keeps both markers.
UNDETERMINED_LIST_MARKER = "[?]"


class _BoardBackdrop(QGraphicsRectItem):
    """@brief Neutral board rectangle shown when the photograph steps back.

    Its own type, so that code enumerating the scene can tell it apart from
    the transient rubber band of a drawing gesture, which is a plain
    QGraphicsRectItem. It is inert: it takes no mouse button, so selecting,
    moving and drawing behave exactly as they do over the photograph.
    """

    def __init__(self, rect):
        super().__init__(QRectF(rect))
        pen = QPen(QColor(BOARD_BORDER_COLOR), BOARD_BORDER_WIDTH_PX)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(BOARD_BACKDROP_COLOR)))
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # Below the photograph: attenuating the photograph must fade it into
        # this neutral base rather than into the view's own background.
        self.setZValue(-101)
        self.hide()


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

    Layout contract: one bar of icons at the top (user decision 2026-09-24:
    add, delete, zoom, fit, photo, component details, model doubts, «i»),
    each named in its tooltip with its key; the sidebar is dominated by
    ``component_list`` with ``count_label`` ("Componenti: N") and the
    ``category_filter`` combo above it. Model status, warnings and the colour
    legend of the chosen component sit in ``details_popup`` under
    ``details_button``, which turns orange when there are warnings. The
    parameter form
    (``form_widget`` holding ``text_fields`` and ``fields``) lives in the
    window-modal ``parameters_dialog`` opened by ``parameters_button``
    or ``open_parameters()``. The field widgets are the same objects whether
    or not that modal is shown, and editing them updates the outline live.

    Adding a component is a drawing gesture: ``add_button`` is checkable and
    arms draw mode (``set_draw_mode``), the next drag on the photo becomes the
    new body and its type is asked straight away. ``add_component()`` is still
    the programmatic entry point and, called without geometry, keeps placing a
    placeholder at the visible centre for headless callers. Either way adding
    disarms the mode, and the body lands whole inside the board.

    A body whose type the detection declined to establish (``type_undetermined``)
    is carried by the mechanisms that already exist: a line in ``warning_label``
    (inside ``details_popup``)
    naming the manual action, a distinct outline status (UNDETERMINED_TYPE_COLOR,
    which yields to "fuori PCB" and wins over the generic "da verificare") and
    the UNDETERMINED_LIST_MARKER on its list row beside the existing "[!]".
    The inventory's own global doubts, when the components carry any, are behind
    ``uncertainties_button`` / ``show_uncertainties()`` and readable through
    ``model_uncertainties()``; the button is hidden when there are none.

    ``category_filter`` hides the list rows AND the rectangles on the photo
    whose ``kind`` does not match the chosen category (user decision
    2026-09-24: the filter filters the photo too). It hides, it never
    rebuilds: ``_components`` and every row index keep their meaning while a
    filter is active, and a hidden rectangle takes no click. Changing the
    filter deselects an out-of-category component; after a deletion the
    selection moves to the nearest component the filter shows, or to none.

    ``photo_button`` sits with the view controls and cycles the canvas
    backdrop (``photo_state()``, ``set_photo_state()``, ``cycle_photo_state()``)
    between the photograph whole, attenuated and gone. It reads as a command:
    the first line of its tooltip is the imperative of the next step, the
    second the state the canvas is in, and the icon draws that state. A dark, crowded board
    swallows a thin coloured outline; in the last two states the board area is
    filled with the neutral BOARD_BACKDROP_COLOR and framed with
    BOARD_BORDER_COLOR, so the outlines and the PCB edge stay readable. It is
    a view setting: it survives adding, deleting, filtering, zooming, fitting
    and the parameter modal, it defaults to PHOTO_FULL on every new dialog,
    and it changes nothing in ``get_components()`` or in what Apply emits.
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
        tr = self._tr
        # Barra di icone in alto, come «Corpi rilevati» e il tab 3D (scelta
        # dell'utente, 2026-09-24): nome, scorciatoia e spiegazione nei
        # tooltip; le istruzioni, che prima occupavano tre righe sopra la
        # foto, stanno nel popup dell'icona «i».
        # Checkable: adding is a mode, and the user must be able to see that
        # the next drag on the photo will create a body instead of moving one.
        self.add_button = tool_button(
            "tool-draw.svg", "Aggiungi componente", "R",
            "Traccia il rettangolo sulla foto, poi assegna la tipologia. Esc annulla.",
            checkable=True, tr=tr)
        self.delete_button = tool_button(
            "delete.svg", "Elimina selezionato", "Canc",
            "Toglie il componente scelto dall'inventario.", tr=tr)
        self.zoom_out_button = tool_button("zoom-out.svg", "Zoom indietro", "-", tr=tr)
        self.zoom_label = QLabel()
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(self.zoom_label.fontMetrics().horizontalAdvance("6400%") + 8)
        self.zoom_label.setToolTip(tr("Ingrandimento rispetto alla foto intera"))
        self.zoom_label.setProperty("i18n_skip", True)
        self.zoom_in_button = tool_button(
            "zoom-in.svg", "Zoom +", "+",
            "Anche con la rotella del mouse, attorno al puntatore.", tr=tr)
        self.fit_button = tool_button("view-fit.svg", "Adatta foto", "F",
                                      "Mostra la foto intera nella finestra.", tr=tr)
        # A view control, next to fit and zoom: it changes what the canvas
        # shows, never what is saved. One icon for three states: the drawing
        # is the state the canvas is in, the tooltip names the next step (see
        # `_apply_photo_state`). The icon has a fixed size, so pressing it can
        # no longer change the width of the toolbar and zoom the drawing, as
        # the old text button did with Windows fonts.
        self.photo_button = tool_button("photo-full.svg", "", checkable=True, tr=tr)
        # Dettagli del componente scelto (utente, 2026-09-24): stato del modello
        # KiCad, avvisi e legenda dei colori stanno in un popup invece che in
        # colonna. L'icona diventa arancione quando il componente scelto ha
        # avvisi, cosi' non spariscono alla vista (vedi `_update_details_button`).
        self.details_button = tool_button("component-details.svg", "", tr=tr)
        self._selected_has_warnings = False
        # Dubbi dichiarati dal modello: da pulsante in colonna a icona nella
        # barra (utente, 2026-09-24); apre la stessa finestra modale e, come
        # prima, esiste solo quando ci sono dubbi. Il numero sta nel tooltip.
        self.uncertainties_button = tool_button("model-doubts.svg", "", tr=tr)
        self.uncertainties_button.hide()
        self.info_button = tool_button(
            "info.svg", "Come si usa", "",
            "Istruzioni, colori e scorciatoie da tastiera di questa finestra.", tr=tr)
        self.info_popup = InfoPopup(self._info_html(), self)
        self.info_button.clicked.connect(lambda: self.info_popup.toggle_under(self.info_button))
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        for widget in (self.add_button, self.delete_button, toolbar_divider(),
                       self.zoom_out_button, self.zoom_label, self.zoom_in_button,
                       self.fit_button, toolbar_divider(), self.photo_button,
                       self.details_button, self.uncertainties_button):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        toolbar.addWidget(self.info_button)
        layout.addLayout(toolbar)
        self.draw_hint = QLabel(
            "Modalità disegno attiva: trascina sulla foto per tracciare il nuovo "
            "componente, poi indicane la tipologia. Esc o «Aggiungi componente» annulla.")
        self.draw_hint.setWordWrap(True)
        self.draw_hint.hide()
        layout.addWidget(self.draw_hint)

        self.scene = QGraphicsScene(self)
        self.photo_item = self.scene.addPixmap(pixmap)
        self.photo_item.setZValue(-100)
        self.photo_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.photo_rect = QRectF(0, 0, pixmap.width(), pixmap.height())
        self._photo_state = PHOTO_FULL
        # Always on the scene, shown only when the photograph steps back: the
        # board must never become an invisible region of empty canvas.
        self.board_backdrop = _BoardBackdrop(self.photo_rect)
        self.scene.addItem(self.board_backdrop)
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
        # Category filter directly above the list: on an inventory of dozens
        # of bodies, looking for "the capacitors" must not mean reading every
        # row. The vocabulary is taken from the components themselves, never
        # from a built-in taxonomy: this editor must not rename or reinterpret
        # what the detection wrote in ``kind``.
        self.category_filter = QComboBox()
        self.category_filter.setObjectName("category_filter")
        self.category_filter.setToolTip(
            "Filtra per categoria la lista e i rettangoli sulla foto.")
        self.category_filter.currentIndexChanged.connect(self._category_filter_changed)
        sidebar.addWidget(self.category_filter)
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
        # The type of a body the user has just drawn is asked, not guessed, and
        # the categories already present in the inventory are offered as a
        # convenience. A completer on the line edit, not a closed combo box:
        # the vocabulary must never become a constraint, since the next board
        # can carry a package nobody has named yet. The field stays a
        # QLineEdit, so every existing caller keeps working.
        self._kind_model = QStringListModel(self)
        self.kind_completer = QCompleter(self._kind_model, self.text_fields["kind"])
        self.kind_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.kind_completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.text_fields["kind"].setCompleter(self.kind_completer)
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
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        # La legenda dichiara anche la precedenza, perche' il colore e' un
        # canale solo: un corpo fuori PCB e senza tipo e' rosso, e il tipo
        # mancante resta scritto nell'avviso e nel marcatore della lista.
        # Pallino del colore vero + parola: il colore non e' l'unico canale.
        # Nel popup dei dettagli insieme a stato del modello e avvisi (utente,
        # 2026-09-24, che rovescia la scelta del pomeriggio di tenerla sempre
        # visibile in colonna).
        self.legend = QLabel(self._legend_html())
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.legend.setWordWrap(True)
        self.legend.setProperty("i18n_skip", True)
        self.details_popup = AnchoredPopup(
            [self.model_status, self.warning_label, self.legend], self)
        self.details_button.clicked.connect(
            lambda: self.details_popup.toggle_under(self.details_button))
        # I dubbi globali dell'inventario arrivavano solo al log. Qui stanno
        # dietro un pulsante che compare solo se ce ne sono: sono prosa del
        # modello, quindi vanno messi a disposizione senza occupare la barra
        # laterale e senza competere con gli avvisi del componente scelto.
        self.uncertainties_button.clicked.connect(self.show_uncertainties)
        self._uncertainties = []
        self.uncertainties_dialog = QDialog(self)
        self.uncertainties_dialog.setWindowTitle("Dubbi dichiarati dal modello")
        uncertainties_layout = QVBoxLayout(self.uncertainties_dialog)
        uncertainties_intro = QLabel(
            "Parole del modello, riportate come sono: sono dati da leggere, non istruzioni. "
            "Nessun componente viene cercato per nome in queste frasi.")
        uncertainties_intro.setWordWrap(True)
        uncertainties_intro.setTextFormat(Qt.TextFormat.PlainText)
        uncertainties_layout.addWidget(uncertainties_intro)
        # QPlainTextEdit e non QLabel/QTextEdit: non interpreta mai il rich
        # text, quindi una frase del modello che contiene <b> o &amp; resta la
        # frase del modello. `i18n_skip` perche' il contenuto e' un dato della
        # corsa, non un testo dell'interfaccia da tradurre.
        self.uncertainties_view = QPlainTextEdit()
        self.uncertainties_view.setReadOnly(True)
        self.uncertainties_view.setProperty("i18n_skip", True)
        uncertainties_layout.addWidget(self.uncertainties_view)
        self.uncertainties_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.uncertainties_buttons.rejected.connect(self.uncertainties_dialog.reject)
        uncertainties_layout.addWidget(self.uncertainties_buttons)
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
        # Apply carries ApplyRole, so this box has no AcceptRole button and Qt
        # has nothing to make the default one: Return then discarded the whole
        # review. Two things are needed, and both are about Return, not about
        # looks. The confirming action becomes the default button; and Cancel
        # gives up its auto-default, because the editor opens with the focus on
        # it and an auto-default button takes the default away while focused,
        # answering Return itself before any dialog-level handler sees it.
        # Cancel is still reachable by Space, by click and by Escape, which is
        # how a focused button and a rejection are supposed to be driven.
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).setDefault(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setAutoDefault(False)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.add_button.toggled.connect(self.set_draw_mode)
        self.view.draw_bounds = self.photo_rect
        self.view.rectangle_drawn.connect(self._component_from_rectangle)
        self.delete_button.clicked.connect(self.delete_selected)
        self.fit_button.clicked.connect(self.fit_photo)
        self.zoom_in_button.clicked.connect(lambda: self.view.zoom(ZOOM_STEP))
        self.zoom_out_button.clicked.connect(lambda: self.view.zoom(1 / ZOOM_STEP))
        self.view.zoom_changed.connect(self._update_zoom_label)
        self.photo_button.clicked.connect(self._photo_button_clicked)
        self._bind_shortcuts()
        # Un combo con il fuoco tiene le sue lettere (revisore 1, difetto 4).
        self._combo_keys = give_combos_their_keys(self)
        self._update_zoom_label()
        self._update_details_button()
        self._apply_photo_state()
        self.component_list.currentRowChanged.connect(self._select_row)
        self.component_list.itemDoubleClicked.connect(lambda _item: self.choose_model())
        self.scene.selectionChanged.connect(self._selection_changed)
        for component in self._components:
            self._append_item(component)
        self._update_scene_rect()
        self._refresh_categories()
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
        @note A half-traced rectangle is dropped on EVERY hide, spontaneous
              included: a minimize taken while the button is down kills the
              pointer grab with the window, so the release that would finish
              the gesture never arrives. Kept alive, the band would come back
              on restore and follow the bare pointer, and the next release
              would create a body nobody drew. The armed mode itself is not a
              casualty of the window being hidden, so it survives.
        """
        super().hideEvent(event)
        self.view.cancel_draw()
        if not event.spontaneous():
            self._close_parameters()

    def closeEvent(self, event):
        """@brief Close the parameter modal even when the editor was never shown."""
        self.view.cancel_draw()
        self._close_parameters()
        super().closeEvent(event)

    def _close_parameters(self):
        """Hide the child windows after moving focus out of the parameter editors.

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
        # Stessa ragione, nessun testo da salvare: una finestra figlia lasciata
        # aperta sopra un editor chiuso resta orfana sullo schermo.
        self.uncertainties_dialog.hide()

    def fit_photo(self):
        """@brief Show the whole photo and re-enable auto-fit on resize."""
        self.view.fit_photo(self.photo_rect)

    def photo_state(self) -> str:
        """@brief How much of the photograph the canvas is showing.

        @return One of PHOTO_FULL, PHOTO_DIMMED or PHOTO_HIDDEN.
        @note A view setting only: it never reaches ``get_components()`` nor
              the list emitted by Apply, and it is not remembered between two
              editors — a new dialog opens on the photograph, which is the
              reference the review starts from.
        """
        return self._photo_state

    def set_photo_state(self, state):
        """@brief Show the photograph whole, attenuate it, or take it away.

        @param state One of PHOTO_FULL, PHOTO_DIMMED, PHOTO_HIDDEN.
        @throws ValueError for any other value: a silently ignored state would
                leave the canvas and the button disagreeing about what the user
                is looking at, which is exactly what this control exists to
                make unambiguous.
        @note Nothing else changes. Selection, move, the eight grips, the halo,
              the list, the drawing gesture, zoom and pan keep working, and the
              full-resolution pixmap stays available for the model chooser's
              crop, which reads ``photo_item.pixmap()`` and not what is on
              screen.
        """
        if state not in PHOTO_STATES:
            raise ValueError(f"Unknown photo state: {state!r}")
        self._photo_state = state
        self._apply_photo_state()

    def cycle_photo_state(self):
        """@brief Step to the next backdrop state: whole, attenuated, hidden.

        @note Monotonic on purpose, each click taking one step further away
              from the photograph: a cycle that jumped straight to "hidden"
              would be quicker for the one case the user asked about, at the
              price of an order nobody can predict from the button alone.
        """
        self.set_photo_state(
            PHOTO_STATES[(PHOTO_STATES.index(self._photo_state) + 1) % len(PHOTO_STATES)])

    def _photo_button_clicked(self, _checked):
        """Cycle on click; the check state is a readout, not the input.

        A checkable button toggles itself before ``clicked`` arrives, so the
        state it reports is meaningless for a three-state control and is
        rewritten by ``_apply_photo_state``.
        """
        self.cycle_photo_state()

    def _apply_photo_state(self):
        """Mirror the state onto the canvas and onto the photo icon.

        The icon draws the state the canvas is in (three different drawings);
        the tooltip's first line, which is the command's name, is the
        imperative for the NEXT step of the cycle, and the second line is the
        current state. The accessible name carries both, because a screen
        reader announces the button alone and would otherwise hear a command
        with no context.
        """
        state = self._photo_state
        # Hiding, not a zero opacity: an invisible item is also out of every
        # paint and hit-test path, and it takes no mouse button either way.
        self.photo_item.setVisible(state != PHOTO_HIDDEN)
        self.photo_item.setOpacity(PHOTO_DIMMED_OPACITY if state == PHOTO_DIMMED else 1.0)
        self.board_backdrop.setVisible(state != PHOTO_FULL)
        action, current = self._photo_state_texts(state)
        with QSignalBlocker(self.photo_button):
            self.photo_button.setChecked(state != PHOTO_FULL)
        asset = _PHOTO_STATE_ICONS[state]
        self.photo_button.setIcon(asset_icon(asset))
        self.photo_button.setProperty("photo_asset", asset)
        set_tool_texts(self.photo_button, action, "P",
                       current + ". " + self._tr(_PHOTO_BUTTON_EXPLANATION))
        self.photo_button.setAccessibleName(
            self._tr("{action} (stato attuale: {state})").format(action=action, state=current))

    def _photo_state_texts(self, state):
        """Command for the next step and readout of ``state``, translated."""
        if state == PHOTO_HIDDEN:
            return self._tr("Mostra foto"), self._tr("Foto: nascosta")
        if state == PHOTO_DIMMED:
            return self._tr("Nascondi foto"), self._tr("Foto: attenuata")
        return self._tr("Attenua foto"), self._tr("Foto: intera")

    def _update_zoom_label(self):
        self.zoom_label.setText(f"{self.view.zoom_percent()}%")

    def _update_details_button(self):
        """Icona e tooltip dei dettagli: arancione quando il componente scelto ha avvisi.

        Gli avvisi stavano sempre sotto gli occhi in colonna; nel popup
        sparirebbero, quindi l'icona dice che ce ne sono e il tooltip quanti.
        Un popup aperto si ridimensiona sul testo nuovo.
        """
        warned = self._selected_has_warnings
        self.details_button.setIcon(asset_icon(
            "component-details-warn.svg" if warned else "component-details.svg"))
        self.details_button.setProperty("has_warnings", warned)
        set_tool_texts(
            self.details_button, self._tr("Dettagli del componente scelto"), "",
            self._tr("Il componente scelto ha avvisi: aprili qui." if warned else
                     "Stato del modello KiCad, avvisi e legenda dei colori."))
        self.details_popup.refit()

    def _bind_shortcuts(self):
        """Keyboard shortcuts, the same ones written in the tooltips and the popup."""
        bindings = (
            ("R", lambda: self.add_button.setChecked(not self.add_button.isChecked())),
            (QKeySequence(Qt.Key.Key_Delete), self.delete_selected),
            ("F", self.fit_photo),
            ("+", lambda: self.view.zoom(ZOOM_STEP)),
            ("=", lambda: self.view.zoom(ZOOM_STEP)),
            ("-", lambda: self.view.zoom(1 / ZOOM_STEP)),
            ("P", self.cycle_photo_state),
        )
        self._shortcuts = []
        for key, slot in bindings:
            sequence = key if isinstance(key, QKeySequence) else QKeySequence(key)
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _info_html(self):
        """@brief Instructions of the «Come si usa» popup, translated, as rich text."""
        tr = self._tr
        rows = [
            (tr("Clic"), tr("Sceglie un rettangolo; anche dalla lista a destra")),
            (tr("Trascina"), tr("Sposta il rettangolo; le maniglie lo ridimensionano")),
            ("R", tr("Aggiungi componente: traccia il rettangolo, poi assegna la tipologia")),
            (tr("Canc"), tr("Elimina il componente scelto")),
            (tr("Rotella"), tr("Zoom attorno al puntatore")),
            (tr("Tasto centrale"), tr("Sposta la vista")),
            ("+  -  F", tr("Zoom avanti, indietro, foto intera")),
            ("P", tr("Foto intera, attenuata, nascosta")),
            (tr("Doppio clic"), tr("Confronta i modelli 3D KiCad del componente")),
        ]
        table = "".join(
            "<tr><td style='padding:2px 12px 2px 0'><b>" + escape(key) + "</b></td>"
            "<td style='padding:2px 0'>" + escape(text) + "</td></tr>" for key, text in rows)
        return ("<p style='margin:0 0 8px 0'><b>" + escape(tr("Come si usa")) + "</b></p>"
                "<p style='margin:0 0 8px 0'>" + escape(tr(_INFO_INTRO)) + "</p>"
                "<table>" + table + "</table>"
                "<p style='margin:8px 0 0 0'>" + escape(tr(_INFO_ORIGIN)) + "</p>")

    def _legend_html(self):
        """@brief Compact colour legend: a dot in the real outline colour, then its words.

        @note The precedence stays written («prevale»): one outline carries one
              colour, so a body off the board and without a type shows red.
        """
        tr = self._tr
        entries = (("#40dc80", tr("Verde: rilevato")),
                   ("#ffad33", tr("Arancione: da verificare")),
                   (UNDETERMINED_TYPE_COLOR, tr("Viola") + f" {UNDETERMINED_LIST_MARKER}: "
                    + tr("tipo non stabilito")),
                   ("#ef5350", tr("Rosso: fuori PCB (prevale)")))
        dots = " &nbsp; ".join(
            f"<span style='white-space:nowrap'><span style='color:{color}'>&#9679;</span>"
            f"&nbsp;{escape(text)}</span>"
            for color, text in entries)
        return f"<span style='font-size:small'>{dots}</span>"

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
        """Show the live number of outlines above the list.

        With a category selected the label states both numbers, e.g.
        "Componenti: 7 di 39": the first is how many components the filter
        matches, the second how many the inventory holds. The matched count
        deliberately ignores the selected component kept visible out of
        category (see ``_apply_filter``): the label answers "how many are of
        this category", not "how many rows are on screen".
        """
        total = len(self._items)
        category = self.active_category()
        if category is None:
            self.count_label.setText(self._tr("Componenti: {count}").format(count=total))
            return
        matching = sum(1 for item in self._items if self._matches_category(item, category))
        self.count_label.setText(self._tr("Componenti: {count} di {total}").format(
            count=matching, total=total))

    def model_uncertainties(self) -> list[str]:
        """@brief The inventory's global doubts, in the model's own words.

        @return Detached list of the distinct ``_global_inventory_uncertainties``
                sentences carried by the working components, in the order they
                are first met.
        @note These are DATA to be shown, never instructions and never a source
              of rules: which body needs attention is decided by
              ``type_undetermined`` on structured fields, not by looking for a
              component's name inside these sentences.
        @note Only strings are kept. The list travels from an AI response
              through the inventory, so anything else in it is a malformed
              response, not a doubt.
        """
        return list(self._uncertainties)

    def _refresh_uncertainties(self):
        """Recollect the global doubts and show or hide the button that opens them.

        Called wherever the membership of the inventory changes: the sentences
        are attached to every component of the run, so deleting the last one
        that carries them must take the button away with them rather than leave
        a window onto an inventory that is no longer here.
        """
        seen = []
        for component in self._components:
            for sentence in component.get("_global_inventory_uncertainties") or ():
                if isinstance(sentence, str) and sentence.strip() and sentence not in seen:
                    seen.append(sentence)
        self._uncertainties = seen
        self.uncertainties_button.setVisible(bool(seen))
        set_tool_texts(
            self.uncertainties_button,
            self._tr("Dubbi dichiarati dal modello ({count})…").format(count=len(seen)), "",
            self._tr("Le frasi del modello sulle incertezze dell'inventario, in una finestra."))
        # Un elenco: numerarlo direbbe una priorita' che il modello non ha
        # dichiarato, quindi ogni frase sta su una riga con lo stesso segno.
        self.uncertainties_view.setPlainText("\n\n".join("• " + sentence for sentence in seen))

    def show_uncertainties(self):
        """@brief Show the model's declared doubts in a window-modal reader.

        @note ``QDialog.open``, as the parameter modal: input to the editor is
              blocked while it is up but no nested event loop runs, so a caller
              (or a test) is never suspended inside this call.
        """
        if not self._uncertainties:
            return
        self.uncertainties_dialog.open()

    def active_category(self):
        """@brief Category currently filtering the list.

        @return The selected ``kind`` value, or None for "all categories".
        """
        return self.category_filter.currentData()

    @staticmethod
    def _component_category(component):
        """Normalized ``kind`` of a component; empty when it declares none.

        Only a string is a category. ``kind`` is free text written by the
        detection, so a list, a number or a boolean is not a spelling any
        detector ever produced; coercing it with ``str()`` would publish
        ``"['ic']"`` or ``"True"`` in the filter combo and in the type
        completer, which is the opposite of taking the vocabulary from the
        components themselves. Such a value simply declares no category and
        therefore matches no filter. The stored value is left exactly as it
        is: this editor reports what the data says, it does not repair it.
        """
        kind = component.get("kind")
        return kind.strip() if isinstance(kind, str) else ""

    def _matches_category(self, item, category):
        """Whether an outline belongs to a category, comparing case-insensitively."""
        return self._component_category(item.component).casefold() == category.casefold()

    def _categories(self):
        """Categories present in the working inventory, as the components spell them.

        Sorted and de-duplicated case-insensitively, keeping the first spelling
        met: the list is a view of the data, so a value is never rewritten to
        fit a canonical form the detection never used. The one liberty taken is
        surrounding whitespace, dropped so that "resistor" and " resistor " are
        one entry instead of two indistinguishable ones; the component itself
        keeps its own spelling either way. Components whose ``kind`` is missing,
        blank or not a string contribute no category, and therefore match no
        filter.
        """
        spellings = {}
        for component in self._components:
            category = self._component_category(component)
            if category:
                spellings.setdefault(category.casefold(), category)
        return [spellings[key] for key in sorted(spellings)]

    def _refresh_categories(self):
        """Rebuild the filter vocabulary after the inventory or a ``kind`` changed.

        The current selection survives if that category still exists, otherwise
        the filter falls back to "all categories": a filter on a category that
        no longer has any component would hide the whole list with no way to
        tell why. Deleting the last resistor therefore shows everything again.
        """
        previous = self.active_category()
        categories = self._categories()
        with QSignalBlocker(self.category_filter):
            self.category_filter.clear()
            self.category_filter.addItem(self._tr("Tutte le categorie"), None)
            for category in categories:
                self.category_filter.addItem(category, category)
            index = self.category_filter.findData(previous) if previous else 0
            self.category_filter.setCurrentIndex(max(0, index))
        # The same vocabulary drives the type field of a newly drawn body.
        self._kind_model.setStringList(categories)
        # Stesso momento, stessa causa: e' qui che l'insieme dei componenti
        # cambia (costruzione, aggiunta, eliminazione).
        self._refresh_uncertainties()
        self._apply_filter()

    def _category_filter_changed(self, _index):
        """@brief Apply the category chosen in the combo to the list and the photo.

        @note A selected component outside the new category is DESELECTED
              (user decision 2026-09-24): the filter now hides rectangles too,
              and keeping an out-of-category selection would leave one odd row
              and one odd rectangle on screen.
        """
        category = self.active_category()
        selected = self._selected_item()
        if category is not None and selected is not None and not self._matches_category(selected, category):
            self.scene.clearSelection()
        self._apply_filter()

    def _apply_filter(self):
        """Hide the list rows AND the rectangles outside the active category.

        Rows and rectangles are hidden, never removed: ``_components``,
        ``_items`` and the list rows stay index-aligned, so every existing
        index-based path keeps working while a filter is active, and what the
        review returns never depends on the filter. A hidden rectangle takes
        no click (user decision 2026-09-24: the filter filters the photo too).

        The selected component stays visible even out of category, which only
        happens when its own type is edited while the filter is active (a
        filter CHANGE deselects it, see `_category_filter_changed`): it is the
        component being worked on, and editing something invisible is worse
        than one odd row.
        """
        category = self.active_category()
        selected = self._selected_item()
        for index, item in enumerate(self._items):
            row = self.component_list.item(index)
            # Rows and outlines are created and destroyed together, so a
            # missing row is a broken invariant, not a case to tolerate:
            # skipping it would leave rows hidden or shown by a stale index,
            # silently, while ``_refresh_item`` would already raise on the very
            # same state. Fail here too, and loudly.
            assert row is not None, (
                f"list row {index} missing for {len(self._items)} outlines: "
                "rows and outlines are no longer index-aligned")
            visible = (category is None or self._matches_category(item, category)
                       or item is selected)
            row.setHidden(not visible)
            item.setVisible(visible)
        self._update_count()

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
                if index == row and not item.isVisible():
                    # Qt non seleziona un oggetto nascosto: un componente
                    # appena aggiunto, o scelto dal tab 3D, fuori dal filtro
                    # attivo restava non scelto e la tipologia scritta dopo
                    # non andava a nessuno. Il componente su cui si lavora
                    # resta visibile (vedi _apply_filter).
                    item.setVisible(True)
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
        # Before the row becomes current: a hidden row cannot be shown as the
        # current one, and the selected component is always kept visible.
        self._apply_filter()
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
            self._selected_has_warnings = False
            self._update_details_button()
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

    def open_parameters(self, focus=None):
        """@brief Show the parameter form for the selected component in a modal.

        @param focus Key of the text field to focus and select, "name" by
               default. Anything that is not a text field key falls back to
               "name": this slot is also connected to a QPushButton, whose
               ``clicked`` signal passes its checked state as first argument.
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
        field = self.text_fields.get(focus if isinstance(focus, str) else "", None)
        if field is None:
            field = self.text_fields["name"]
        field.setFocus()
        field.selectAll()

    @staticmethod
    def type_undetermined(component) -> bool:
        """@brief Whether the detection declined to establish this body's type.

        @param component One component dictionary of the working inventory.
        @return True when the model's own STRUCTURED declaration says it did
                not settle on a family for this body.
        @note Two declarations mean that, and only these two are read:

              - the declared ``class`` (or, when no class was recorded, the
                ``kind``) is one of UNDETERMINED_TYPE_CLASSES, the schema's
                explicit "a body, family unresolved" values;
              - ``class_confidence`` is one of UNDETERMINED_TYPE_CONFIDENCES,
                i.e. the model named a family but declared the naming
                unsupported.

              Both have the same consequence downstream — ``kicad_models``
              refuses to certify an identity, the body falls back to a generic
              volume without terminals — which is exactly the state a manual
              model choice repairs, and is why they share one flag.
        @note Deliberately NOT read: the free-text ``uncertainties`` /
              ``_global_inventory_uncertainties``, which name components in
              prose. Matching a body by its name inside a sentence would be a
              name-based rule, fragile by construction and forbidden here.
        @note Deliberately NOT read either: an empty ``package``. The inventory
              empties that field whenever the OVERALL confidence is low or the
              outline basis is unresolved, so it follows from other doubts
              (already reported as "bassa confidenza") and from geometry, not
              from a statement about the type; using it would flag confidently
              typed passives whose outline basis was never settled.
        @note A body a person drew here declares no ``class`` and gets
              ``kind="generic"``, which is not one of the unknown values: the
              flag says the MODEL declined, and must not be raised by the
              user's own placeholder.
        """
        if not isinstance(component, dict):
            return False
        declared = component.get("class")
        if not isinstance(declared, str) or not declared.strip():
            declared = component.get("kind")
        if (isinstance(declared, str)
                and declared.strip().casefold() in UNDETERMINED_TYPE_CLASSES):
            return True
        class_confidence = component.get("class_confidence")
        return (isinstance(class_confidence, str)
                and class_confidence.strip().casefold() in UNDETERMINED_TYPE_CONFIDENCES)

    def _warnings(self, item):
        warnings = []
        if not self.photo_rect.adjusted(-1e-7, -1e-7, 1e-7, 1e-7).contains(item.photo_bounds()):
            warnings.append(self._tr("Fuori dai bordi PCB: verificare posizione e dimensioni."))
        if self.type_undetermined(item.component):
            # L'avviso dice cosa FARE, non che c'e' un dubbio: il dubbio del
            # modello e' gia' una risposta corretta (il progetto non inventa
            # identita' elettriche), mentre la scatola generica che ne segue e'
            # il difetto visibile, e si ripara qui in un doppio clic.
            warnings.append(self._tr(
                "Il modello non ha stabilito il tipo di questo corpo: senza una scelta "
                "manuale il 3D resta una scatola generica senza terminali. Doppio clic "
                "per scegliere il modello KiCad: la ricerca è locale e non costa nulla."))
        component = item.component
        selection = component.get('model_selection') or {}
        if (component.get('model_match') or {}).get('status') == 'import_failed':
            warnings.append(self._tr("Importazione STEP fallita: il 3D mostra un segnaposto, non il modello scelto."))
        if (component.get('model_match') or {}).get('status') == 'auto_matched' and selection.get('status') != 'confirmed':
            warnings.append(self._tr("Modello KiCad applicato automaticamente per compatibilità meccanica: identità e polarità non certificate; doppio clic per modificarlo."))
        elif (component.get('model_match') or {}).get('status') == 'library_matched' and selection.get('status') != 'confirmed':
            # Scelta dell'utente: «preseleziona, tu confermi». Il modello viene
            # da un abbinamento gia' approvato su un'altra scheda con la stessa
            # firma geometrica; il verso dipende da questa scheda.
            warnings.append(self._tr("Modello KiCad preso dall'archivio dei tuoi abbinamenti accettati (stessa forma e misure): verifica il verso; doppio clic per confermarlo o cambiarlo."))
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
        undetermined = self.type_undetermined(item.component)
        # Una sola linea porta un solo colore, quindi la precedenza va
        # dichiarata: fuori PCB prima di tutto, perche' un corpo che non e'
        # sulla scheda rende accademica ogni altra domanda; poi il tipo non
        # stabilito, che e' piu' specifico e piu' azionabile del generico «da
        # verificare»; poi gli altri avvisi. Il testo invece non ha questo
        # limite: warning_label e tooltip elencano TUTTE le righe che valgono,
        # quindi un corpo fuori PCB e senza tipo le mostra entrambe.
        color = QColor("#ef5350" if outside else UNDETERMINED_TYPE_COLOR if undetermined
                       else "#ffad33" if warnings else "#40dc80")
        # The status pen keeps its colour when selected: the selection is
        # shown by the wide halo behind it, the thicker line and the fill.
        pen = QPen(color, SELECTED_PEN_WIDTH_PX if item.isSelected() else UNSELECTED_PEN_WIDTH_PX)
        pen.setCosmetic(True)
        item.setPen(pen)
        item.halo.setVisible(item.isSelected())
        fill = QColor(color)
        fill.setAlpha(60 if item.isSelected() else 15)
        item.setBrush(QBrush(fill))
        # Fra rettangoli sovrapposti il clic va al PIU' PICCOLO: sopra i piu'
        # piccoli, a qualunque posto dell'elenco (utente, 2026-09-24: una
        # proposta spuria dentro un corpo grande non si poteva scegliere).
        # Solo l'area decide, anche per il rettangolo scelto: prima la scelta
        # lo portava sopra tutti, e scelto un corpo grande i piccoli dentro
        # non si prendevano piu' (revisore 1, difetto 2). La scelta si vede
        # dall'alone, dal tratto e dal riempimento, non dall'ordine.
        bounds = item.photo_bounds()
        smallness = 1.0 / (1.0 + max(0.0, bounds.width() * bounds.height()))
        item.setZValue(1 + smallness)
        item.number.setText(str(index + 1))
        name = item.component.get("name") or item.component.get("kind") or self._tr("Componente")
        text = (f"{index + 1}. {name}"
                + (f" {UNDETERMINED_LIST_MARKER}" if undetermined else "")
                + (" [!]" if warnings else ""))
        self.component_list.item(index).setText(text)
        self.component_list.item(index).setForeground(color)
        # QGraphicsItem tooltips interpret HTML, so escape supplied metadata.
        tooltip = escape(text + ("\n" + "\n".join(warnings) if warnings else ""))
        item.setToolTip(tooltip)
        # Stessa spiegazione sulla riga della lista: il marcatore e' corto per
        # forza, la frase che dice cosa fare deve restare raggiungibile anche
        # da chi sta leggendo l'elenco invece della foto. Non e' la correzione
        # da sola -- il colore e il marcatore si vedono senza puntare nulla.
        self.component_list.item(index).setToolTip(tooltip)
        if item.isSelected():
            if item.component.get('_height_source'):
                warnings.append(self._tr("Altezza indicativa: verificare con una misura diretta."))
            self.warning_label.setText(
                "\n".join(warnings)
                if warnings else self._tr("Nessun avviso per questo componente.")
            )
            self._selected_has_warnings = bool(warnings)
            self._update_details_button()

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
                    # Typing a type can create or retire a category.
                    self._refresh_categories()
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

    def set_draw_mode(self, enabled):
        """@brief Arm or disarm "add a component by drawing its rectangle".

        @param enabled True to arm: the button stays checked, the pointer
               becomes a crosshair over the photo and a hint is shown; the next
               drag on the photo creates the body. False disarms without
               creating anything.
        @note Idempotent and safe to call from the button's own ``toggled``
              signal: the button state is only written when it disagrees, and
              then without re-emitting.
        """
        enabled = bool(enabled)
        if self.add_button.isChecked() != enabled:
            with QSignalBlocker(self.add_button):
                self.add_button.setChecked(enabled)
        self.view.set_draw_mode(enabled)
        self.draw_hint.setVisible(enabled)

    def draw_mode(self) -> bool:
        """@brief Whether the next drag on the photo would create a component.

        @return True while the drawing mode is armed.
        """
        return self.view.draw_mode()

    def keyPressEvent(self, event):
        """@brief Let Escape disarm the drawing mode, and make Return apply.

        @note An armed mode is the innermost thing Escape can cancel: leaving
              the dialog while the user only wanted to give up on the rectangle
              would throw away the whole review. Escape with nothing armed
              still rejects, as in any dialog.
        @note Return/Enter confirms. This button box has no AcceptRole button
              (Apply carries ApplyRole), so Qt routed Return to Cancel and the
              most ordinary keystroke in a dialog discarded every correction.
              The button box is set up to match (see the constructor); this
              handler is what makes the guarantee independent of which widget
              holds the focus. The keypad modifier is ignored because keypad
              Enter is the same intent; any other modifier is left to the base
              class, which must stay free to handle shortcuts.
        """
        if event.key() == Qt.Key.Key_Escape and self.draw_mode():
            self.set_draw_mode(False)
            event.accept()
            return
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and modifiers == Qt.KeyboardModifier.NoModifier):
            self.accept()
            event.accept()
            return
        super().keyPressEvent(event)

    def _component_from_rectangle(self, scene_rect):
        """Create one component from a rectangle just drawn on the photo.

        The rectangle arrives in scene (crop) pixels and is converted through
        ``PhotoTransform``; a freshly drawn box is axis aligned, so its
        rotation is 0 and its local length/width are the board axes. Each side
        is held at MIN_HANDLE_SIZE_MM, the same floor the resize handles
        enforce, so a stroke made at deep zoom cannot leave a degenerate body
        that no handle could grab again.

        The floor grows a side after the rectangle has been clipped to the
        photo, which would push a stroke traced on the very edge of the board
        back outside it — the outline would be flagged "outside the PCB" the
        instant it appeared. The centre is therefore clamped afterwards, on
        each axis independently, exactly as the placeholder path does.
        """
        top_left = self.photo_transform.pixel_to_mm(scene_rect.left(), scene_rect.top())
        bottom_right = self.photo_transform.pixel_to_mm(scene_rect.right(), scene_rect.bottom())
        length = max(MIN_HANDLE_SIZE_MM, bottom_right.x() - top_left.x())
        width = max(MIN_HANDLE_SIZE_MM, bottom_right.y() - top_left.y())
        center = self._clamped_center((top_left.x() + bottom_right.x()) / 2,
                                      (top_left.y() + bottom_right.y()) / 2, length, width)
        self.add_component(geometry=(center.x(), center.y(), length, width))

    def _clamped_center(self, x_mm, y_mm, length, width):
        """Centre of an unrotated body kept so its whole extent stays on the board.

        @param x_mm Wanted centre along the board length, in millimeters.
        @param y_mm Wanted centre along the board width, in millimeters.
        @param length Extent along the board length axis, in millimeters.
        @param width Extent along the board width axis, in millimeters.
        @return Clamped centre as a QPointF, in board millimeters.
        @note Axis-aligned only: both callers create a body at rotation 0. A
              rotated body's envelope is not its length and width, so this must
              not be reused for one without recomputing the envelope.
        """
        half_length = self.photo_transform.body_length_mm / 2
        half_width = self.photo_transform.body_width_mm / 2
        return QPointF(
            max(-half_length + length / 2, min(half_length - length / 2, x_mm)),
            max(-half_width + width / 2, min(half_width - width / 2, y_mm)))

    def add_component(self, geometry=None):
        """@brief Add a generic package and select it, asking for what is missing.

        @param geometry Optional ``(x_mm, y_mm, length_mm, width_mm)`` measured
               from a rectangle the user has drawn. Without it the body gets
               placeholder geometry at the visible center, clamped inside the
               board and sized to a tenth of it (at most 2 x 1 mm); that is the
               programmatic/headless path, kept for callers that add a row and
               then set its values themselves.
        @note The body is flagged uncertain either way: a hand-drawn outline is
              a human estimate, not a detection. A drawn body already has its
              geometry, so the parameter modal opens on the type ("draw the
              rectangle, then give it its category"); a placeholder body has
              neither name nor geometry and opens on the name, as before.
        @note Adding disarms the drawing mode, on every route in: one body per
              gesture, and the parameter modal must never open over a canvas
              still waiting for a stroke — the next drag would create a second
              body instead of adjusting the one just added.
        """
        self.set_draw_mode(False)
        drawn = geometry is not None
        if drawn:
            x_mm, y_mm, length, width = (float(value) for value in geometry)
        else:
            center = self.view.mapToScene(self.view.viewport().rect().center())
            center = self.photo_transform.pixel_to_mm(
                min(self.photo_rect.width(), max(0, center.x())),
                min(self.photo_rect.height(), max(0, center.y())))
            length = min(2.0, self.photo_transform.body_length_mm / 10)
            width = min(1.0, self.photo_transform.body_width_mm / 10)
            center = self._clamped_center(center.x(), center.y(), length, width)
            x_mm, y_mm = center.x(), center.y()
        # "generic" is the project's generic-volume path, not an identity claim:
        # the type is what the modal asks for right below.
        component = dict(name=self._tr("Nuovo componente"), kind="generic", x_mm=x_mm, y_mm=y_mm,
                         length_mm=length, width_mm=width, height_mm=1.0, rotation_deg=0.0,
                         confidence="low", _uncertain=True)
        self._components.append(component)
        self._append_item(component)
        self._update_scene_rect()
        self._refresh_categories()
        self._select_row(len(self._items) - 1)
        # The parameter modal follows automatically so the user can type
        # straight away. A modal over an editor that is not on screen would be
        # orphaned, so a hidden editor (headless callers) only prepares the field.
        focus = "kind" if drawn else "name"
        if self.isVisible() and not self.parameters_dialog.isVisible():
            self.open_parameters(focus)
        else:
            self.text_fields[focus].setFocus()
            self.text_fields[focus].selectAll()

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
        # A category can disappear with its last component.
        self._refresh_categories()
        self._select_row(self._nearest_shown(index))
        self._update_scene_rect()

    def _nearest_shown(self, index):
        """Row of the outline nearest to `index` that the category filter shows; -1 if none.

        Forward first, as before. Before, the next row was selected even when
        the filter hid it, and `_select_row` made it visible: a second Delete
        removed a component the filter was hiding (reviewer 1, defect 1).
        """
        category = self.active_category()
        for distance in range(len(self._items)):
            for candidate in (index + distance, index - distance - 1):
                if 0 <= candidate < len(self._items) and (
                        category is None or self._matches_category(self._items[candidate], category)):
                    return candidate
        return -1

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
