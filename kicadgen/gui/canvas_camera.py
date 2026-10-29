"""@brief Shared zoom, wheel, pan and fit of the symbol editor and the footprint preview.

Both graphics views keep their classes, signals and the decision of WHEN a
gesture starts; the camera arithmetic they used to implement twice lives here
and only here (plan `docs/canvas-camera-grid-plan.md`, section 2):

- `Camera.zoom` / `Camera.wheel`: clamped zoom about a viewport anchor.
- `Camera.pan_begin` / `pan_move` / `pan_end` / `pan_cancel`: the pointer-delta pan.
- `Camera.fit`: `fitInView` with a memo, so an unchanged camera is not refitted.

The module is Qt-only: it imports no model, no tab and no translated string,
so the unit tests drive it with a bare `QGraphicsView`.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtWidgets import QGraphicsView


class Camera(QObject):
    """@brief The camera of a `QGraphicsView`: clamped zoom, wheel, pointer pan and memoised fit.

    @note The view's owner keeps the routing (which button starts a pan, when
        a wheel is refused, the Escape hook) and the framing rectangle; the
        camera never inspects the scene's items. `auto_fit` is stored here;
        the views expose it as a property delegating to this attribute.
    """

    #: The scale or framing changed: after a zoom or a fit that ran (a pan emits nothing).
    changed = pyqtSignal()

    def __init__(self, view: QGraphicsView, *, limits: tuple[float, float], max_notches: float = 4.0):
        """@brief Bind the camera to a view.

        @param view The graphics view; also the Qt parent.
        @param limits (minimum, maximum) of the view scale (viewport px per scene unit);
            a zoom is CLAMPED to this range.
        @param max_notches Largest wheel magnitude, in 120-unit notches, one wheel event may zoom.
        @throws ValueError When the limits are not 0 < low <= high.
        """
        super().__init__(view)
        low, high = float(limits[0]), float(limits[1])
        if not (0.0 < low <= high):
            raise ValueError(f"Camera limits must satisfy 0 < low <= high, got {limits!r}")
        self._view = view
        self.limits = (low, high)
        self.max_notches = float(max_notches)
        #: True while the picture follows the framing rectangle (cleared by a zoom or a real pan).
        self.auto_fit = True
        self._pan_last: QPoint | None = None
        self._pan_button = None
        self._pan_grab = False
        self._fitted = None

    def scale(self) -> float:
        """@brief The current view scale.

        @return `abs(transform().m11())`, viewport pixels per scene unit.
        """
        return abs(self._view.transform().m11())

    def camera_key(self) -> tuple:
        """@brief Everything the picture of a fit depends on.

        @return (scene rect, viewport size, transform, horizontal scroll, vertical scroll).
        """
        view = self._view
        scene = view.scene()
        return (scene.sceneRect() if scene is not None else QRectF(), view.viewport().size(),
                view.transform(), view.horizontalScrollBar().value(), view.verticalScrollBar().value())

    def zoom(self, factor: float, anchor: QPoint | None = None) -> bool:
        """@brief Scale the view by `factor`, clamped to the limits, keeping the anchor fixed on screen.

        @param factor Multiplier of the current scale (>1 zooms in); non-finite or non-positive values are ignored.
        @param anchor Viewport point (px) that stays under the pointer; the viewport centre when None.
        @return True when the scale changed; False, with nothing touched (no
            `auto_fit` change, no signal), when the clamped scale equals the current one.
        """
        current = self.scale()
        if not (math.isfinite(factor) and factor > 0.0) or current <= 0.0:
            return False
        low, high = self.limits
        wanted = current * factor
        target = max(low, min(high, wanted))
        if target == current:
            return False
        # The exact factor when nothing was clamped keeps the arithmetic of the legacy views.
        ratio = factor if wanted == target else target / current
        view = self._view
        anchor = anchor if anchor is not None else view.viewport().rect().center()
        self.auto_fit = False
        before = view.mapToScene(anchor)
        view.scale(ratio, ratio)
        after = view.mapToScene(anchor)
        # The EXACT middle (w/2, h/2): QRect.center() of an even size is half a pixel short,
        # which re-centred the picture one scroll step off at every zoom.
        viewport = view.viewport()
        middle = QPointF(viewport.width() / 2.0, viewport.height() / 2.0)
        inverse, _ok = view.viewportTransform().inverted()      # mapToScene has no QPointF overload
        view.centerOn(inverse.map(middle) + before - after)
        self.changed.emit()
        return True

    def wheel(self, event, *, blocked: bool = False) -> None:
        """@brief Zoom about the pointer for a wheel event; the event is always accepted.

        @param event The `QWheelEvent`; its vertical angle delta (120 per notch) and position are used.
        @param blocked True while the owner refuses the zoom (a drag or a pan runs): the event is
            only accepted.
        @note One event zooms by `1.2 ** clamp(notches, -max_notches, max_notches)`, so a
            free-spinning wheel cannot jump by an unbounded power.
        """
        delta = event.angleDelta().y()
        if delta and not blocked:
            notches = max(-self.max_notches, min(self.max_notches, delta / 120))
            self.zoom(1.2 ** notches, event.position().toPoint())
        event.accept()

    @property
    def pan_active(self) -> bool:
        """@brief Whether a pan runs.

        @return True between `pan_begin` and `pan_end` / `pan_cancel`.
        """
        return self._pan_last is not None

    @property
    def pan_button(self):
        """@brief The mouse button that started the running pan.

        @return A `Qt.MouseButton`, or None when idle.
        """
        return self._pan_button if self._pan_last is not None else None

    def pan_begin(self, button, pos: QPoint, *, grab_mouse: bool, keep_auto_fit: bool = False) -> None:
        """@brief Start a pan at the viewport position `pos`.

        @param button The `Qt.MouseButton` that started it, returned by `pan_button`.
        @param pos Viewport position of the press (px).
        @param grab_mouse True to `grabMouse()` the viewport until the pan ends (the symbol
            editor); False to rely on Qt's implicit grab (the footprint preview).
        @note A pan already running is finished first (same cleanup as `pan_cancel`).
        @param keep_auto_fit False clears `auto_fit` at once; True leaves it until a step
            really moves the view. Both views pass True (review 2026-10-29, C4: a click
            without a move must not stop the auto-fit, in the symbol editor too).
        """
        view = self._view
        if self._pan_last is not None:
            self._finish_pan()                  # release what the running pan grabbed, then start anew
        self._pan_last = QPoint(pos)
        self._pan_button = button
        self._pan_grab = bool(grab_mouse)
        if not keep_auto_fit:
            self.auto_fit = False
        view.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        if self._pan_grab:
            view.viewport().grabMouse()

    def pan_move(self, pos: QPoint) -> None:
        """@brief Move the picture by the pointer delta since the previous position.

        @param pos Current viewport position (px); a no-op when no pan runs.
        @note `auto_fit` is cleared only when the delta is not null. No signal is emitted:
            a pan never did (the symbol tab's readout listens to `changed` for zoom and fit only).
        """
        if self._pan_last is None:
            return
        view = self._view
        last, self._pan_last = self._pan_last, QPoint(pos)
        delta_px = pos - last
        if delta_px.isNull():
            return
        self.auto_fit = False
        hbar, vbar = view.horizontalScrollBar(), view.verticalScrollBar()
        # Scroll-bar pan: exact in device pixels, so many small steps never accumulate the
        # rounding drift that re-centring with centerOn adds at every step.
        hbar.setValue(hbar.value() - delta_px.x())
        vbar.setValue(vbar.value() - delta_px.y())

    def pan_end(self, pos: QPoint) -> bool:
        """@brief Finish the pan: a last move at the release point, then release the capture.

        @param pos Viewport position of the release (px); Windows coalesces moves, so it counts.
        @return True when a pan was finished; False, with nothing done, when idle.
        """
        if self._pan_last is None:
            return False
        self.pan_move(pos)
        return self._finish_pan()

    def pan_cancel(self) -> bool:
        """@brief Abort the pan without moving: the cleanup of Escape or of a hidden view.

        @return True when a pan was cancelled; False when idle.
        """
        if self._pan_last is None:
            return False
        return self._finish_pan()

    def _finish_pan(self) -> bool:
        """Clear the pan state, release the mouse (only if grabbed) and restore the cursor."""
        grabbed = self._pan_grab
        self._pan_last = None
        self._pan_button = None
        self._pan_grab = False
        viewport = self._view.viewport()
        if grabbed:
            viewport.releaseMouse()
        viewport.unsetCursor()
        return True

    def fit(self, rect: QRectF, *, force: bool = False, reset: bool = False) -> bool:
        """@brief Frame `rect` in the view with `fitInView`, unless the picture would not change.

        @param rect Scene rectangle to frame; an empty one is ignored.
        @param force True fits even when `auto_fit` is off or the camera is unchanged.
        @param reset True calls `resetTransform()` first (the footprint preview re-frames from scale 1).
        @return True when the view was refitted (`changed` emitted); False when skipped:
            `auto_fit` off without `force` (it stays off), an empty `rect`, or the same
            rectangle with the same camera key as after the previous fit without `force`.
        @note Like the legacy symbol fit, `auto_fit` is set True BEFORE the rectangle is
            examined, so an empty rectangle still turns it on (nothing is fitted or emitted).
        @note `fitInView` resets and rescales the transform and repaints the whole viewport,
            hence the memo: a redraw that leaves the camera alone costs nothing.
        """
        if not force and not self.auto_fit:
            return False
        self.auto_fit = True
        rect = QRectF(rect)
        if rect.isEmpty():
            return False
        if not force and self._fitted == (rect, self.camera_key()):
            return False
        view = self._view
        if reset:
            view.resetTransform()
        view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._fitted = (rect, self.camera_key())
        self.changed.emit()
        return True
