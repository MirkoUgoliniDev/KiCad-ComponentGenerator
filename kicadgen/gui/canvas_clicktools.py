"""@brief Shared click tools of the symbol editor and the footprint preview: collect points by clicking.

Both tabs have tools that COLLECT POINTS by clicking (symbol: wire and shape
tools; footprint: draw mode). What they share is handled here and only here
(plan `docs/canvas-clicktools-plan.md`, section 2):

- `ToolSpec`: the immutable description of one tool (key, point count, close
  rule, persistence, drag rule).
- `ClickToolHost`: what a tab implements to snap, draw and commit.
- `PointCollector`: the state machine (idle -> collecting -> idle), the point
  list, the hover preview and the finish/cancel/stay protocol.
- `ClickToolRouter`: maps the view's mouse and key events to the collector
  (press, release, double click, key, move, leave, hide) and owns the cursor.

The module is Qt-only: it imports no model, no tab and no translated string,
so the two views adopt it independently and the unit tests drive it with a
bare `QGraphicsView`, a real `Camera` and a recording fake host.

@note The collector and the router never raise on stale host state (an
    exception in a slot aborts the application): the state is cleared BEFORE
    a host callback is called, and every callback may re-enter the collector
    (see each `ClickToolHost` method for the calls it may make). Exceptions
    raised BY the host are never swallowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QObject, QPoint, QPointF, Qt
from PyQt6.QtWidgets import QApplication, QGraphicsView

#: Reason given to `ClickToolHost.tool_refused` when a click adds nothing
#: (the host's snap returned no point, or the point repeats the previous one).
REFUSED_COINCIDENT = "coincident"

#: Largest number of nested cancellations `PointCollector.arm` performs when a
#: host keeps arming tools from inside `tool_finished` (a runaway-host guard).
_MAX_ARM_CANCELS = 8


@dataclass(frozen=True)
class ToolSpec:
    """@brief The immutable description of one click tool.

    @note Compared by value: two specs with the same fields are the same tool.
    """

    #: Name of the tool ("wire", "rectangle", ...); opaque to this module.
    key: str
    #: Fixed number of points; reaching it finishes the tool. 0 = open-ended.
    count: int = 0
    #: > 0: with at least this many points, a click equal to the FIRST point
    #: finishes the tool (the symbol polygon closes at 4). 0 = no close rule.
    close_after: int = 0
    #: True: the host's `tool_finished` may return True to stay armed after a
    #: committed shape; this field documents the intent and is read by hosts.
    persistent: bool = False
    #: True: a press-move-release drag adds the release point (router rule).
    drag_adds_release_point: bool = True


class ClickToolHost(Protocol):
    """@brief What a tab implements to receive the events of a `PointCollector`.

    @note RE-ENTRANCY (contract rule 9). Every callback runs with the
        collector in a consistent state and the host MAY call `arm`,
        `disarm`, `cancel` and `finish` from inside any of them (they are
        safe and never raise). The host MUST NOT call `click` from inside
        `tool_snap` and SHOULD NOT call `hover`/`click` from inside
        `tool_finished`/`tool_disarmed` (the collector is idle there and
        both are ignored). The per-method notes list the exact rules.
    """

    def tool_snap(self, spec: ToolSpec, point: QPointF, free: bool, points: Sequence) -> list:
        """@brief Turn a raw scene point into the points a click would APPEND.

        @param spec The armed tool.
        @param point Raw scene position of the click or of the hover.
        @param free True when the user asked for an unconstrained point (Shift).
        @param points Snapshot (tuple) of the points collected so far.
        @return The points to append: 0 (refused: nothing is added, the click
            reports `tool_refused` unless it was `quiet`), 1, or 2 (the wire's
            orthogonal corner first). Used for clicks AND hovers, so the ghost
            and the click agree.
        @note Called for every click and every hover. It must be pure with
            respect to the collector: it may call `arm`/`disarm`/`cancel`
            (the click or hover is then dropped) but never `click`.
        """

    def tool_points_changed(self, spec: ToolSpec, points: list) -> None:
        """@brief The collected points changed: draw the dots.

        @param spec The armed tool.
        @param points A copy of the collected points.
        @note Called after an add, after a stay re-arm (empty list) and once
            right after `tool_armed` (empty list). The host may call
            `cancel`, `finish` and `arm` from inside; `click` is not
            expected.
        """

    def tool_preview_changed(self, spec: ToolSpec, points: list, preview: list) -> None:
        """@brief The hover preview changed: draw the ghost, or clear it when empty.

        @param spec The armed tool.
        @param points A copy of the collected points.
        @param preview The points that WOULD be appended; [] when none (the
            pointer left, or the snap refused).
        @note Emitted only when the preview differs (`!=`) from the previous
            one, and once when a leave clears it. The ghost left by a finish
            or a cancel is cleared by the host in `tool_finished` /
            `tool_disarmed`: the collector does not emit for those.
            The host may call `cancel`, `finish` and `arm` from inside.
        """

    def tool_refused(self, spec: ToolSpec, reason: str) -> None:
        """@brief A click added nothing.

        @param spec The armed tool.
        @param reason `REFUSED_COINCIDENT`.
        @note At most once per click; never for a `quiet` click and never for
            a hover. The host may call `cancel` and `arm` from inside.
        """

    def tool_finished(self, spec: ToolSpec, points: list, commit: bool) -> bool:
        """@brief The tool ended: commit or abandon the points.

        @param spec The tool that ended.
        @param points The collected points; ownership passes to the host (the
            collector keeps no reference).
        @param commit True: finished by count, close rule, right click, Enter
            or double click; False: cancelled (Escape, hide, disarm, another
            tool armed).
        @return True to stay armed with an empty list (a persistent tool after
            a shape), False to disarm. Ignored when `commit` is False
            through `arm`/`disarm`, and ignored when the host armed another
            tool (or the same one again) from inside this callback.
        @note The collector is already IDLE when this is called (`armed`
            False, no points): `click`, `hover` and `finish` are no-ops
            there; `arm` is allowed and wins over the return value; `cancel`
            and `disarm` are no-ops. If the host arms a tool inside,
            `tool_disarmed` is NOT called for the ended tool (the new
            `tool_armed` replaces it).
        """

    def tool_armed(self, spec: ToolSpec) -> None:
        """@brief A tool was armed (after any previous tool ended).

        @param spec The tool now armed.
        @note The host may call `cancel`/`disarm` from inside (the tool ends
            at once with `tool_finished(commit=False)`) or `arm` (replaces it).
        """

    def tool_disarmed(self, spec: ToolSpec) -> None:
        """@brief The tool ended and the collector is idle (no stay, no new tool).

        @param spec The tool that was disarmed.
        @note Called after `tool_finished`. The host may call `arm` from
            inside; the other calls are no-ops (idle).
        """


class PointCollector(QObject):
    """@brief The click-tool state machine: idle -> collecting -> idle, one `tool_finished` per tool run.

    @note State is cleared BEFORE the host is told, so every callback may
        re-enter. Points are any equality-comparable objects (`==` is used for
        the duplicate and close-on-first rules); they are not copied deeply.
        Not a signal emitter: the host protocol is the only output.
    """

    def __init__(self, host: ClickToolHost, parent: QObject | None = None):
        """@brief Create an idle collector.

        @param host Object implementing `ClickToolHost`.
        @param parent Qt parent.
        """
        super().__init__(parent)
        self._host = host
        self._spec: ToolSpec | None = None
        self._armed = False
        self._points: list = []
        self._preview: list = []
        #: Incremented at every arm, end and stay, so a callback that changed
        #: the state under a running click/hover is detected.
        self._epoch = 0

    @property
    def armed(self) -> bool:
        """@brief Whether a tool is armed.

        @return True between `arm` and the end of the tool (a stay keeps it True).
        """
        return self._armed

    @property
    def spec(self) -> ToolSpec | None:
        """@brief The armed tool.

        @return The `ToolSpec`, or None when idle.
        """
        return self._spec

    @property
    def points(self) -> list:
        """@brief The collected points.

        @return A copy of the list (the points themselves are not copied).
        """
        return list(self._points)

    @property
    def preview(self) -> list:
        """@brief The points the last hover would append.

        @return A copy of the list; empty when none or after a leave.
        """
        return list(self._preview)

    def arm(self, spec: ToolSpec) -> bool:
        """@brief Arm a tool, ending any armed tool first.

        @param spec The tool to arm.
        @return True when armed; False, with nothing done, when `spec` is not
            a `ToolSpec` or a runaway host kept arming tools from inside
            `tool_finished`. Never raises.
        @note Order: an armed tool (even the same one) ends with
            `tool_finished(commit=False)` and `tool_disarmed`; then
            `tool_armed(spec)`; then `tool_points_changed(spec, [])`.
        """
        if not isinstance(spec, ToolSpec):
            return False
        guard = 0
        while self._armed and guard < _MAX_ARM_CANCELS:
            self._end(False, allow_stay=False)
            guard += 1
        if self._armed:
            return False
        self._armed = True
        self._spec = spec
        self._points = []
        self._preview = []
        self._epoch += 1
        epoch = self._epoch
        self._host.tool_armed(spec)
        if self._armed and self._epoch == epoch:
            self._host.tool_points_changed(spec, [])
        return True

    def disarm(self) -> None:
        """@brief End the armed tool without committing (same as `cancel`)."""
        self.cancel()

    def click(self, point, *, free: bool = False, quiet: bool = False) -> bool:
        """@brief Feed one click (scene coordinates) to the armed tool.

        @param point Raw scene position of the click.
        @param free True for an unconstrained point (Shift); passed to `tool_snap`.
        @param quiet True suppresses `tool_refused` (the second click of a double click).
        @return True when at least one point was appended; False when idle or refused.
        @note Order (contract rule 1): `tool_snap`; an empty result or a first
            appended point equal to the last collected one is refused
            (`tool_refused` at most once, unless quiet); append; then
            count reached -> `finish(True)`, else `close_after` reached with
            the click equal to the first point -> `finish(True)`, else
            `tool_points_changed`. A host that ended or re-armed the tool
            inside a callback drops the rest of the click.
        """
        if not self._armed:
            return False
        spec, epoch = self._spec, self._epoch
        appended = list(self._host.tool_snap(spec, point, bool(free), tuple(self._points)))
        if not self._armed or self._epoch != epoch:
            return False
        if not appended or (self._points and appended[0] == self._points[-1]):
            if not quiet:
                self._host.tool_refused(spec, REFUSED_COINCIDENT)
            return False
        self._points.extend(appended)
        self._preview = []
        total = len(self._points)
        if spec.count and total >= spec.count:
            self.finish(True)
        elif (spec.close_after and total >= spec.close_after
                and appended[-1] == self._points[0]):
            self.finish(True)
        else:
            self._host.tool_points_changed(spec, list(self._points))
        return True

    def finish(self, commit: bool = True) -> bool:
        """@brief End the armed tool, handing the points to the host.

        @param commit True to commit, False to abandon (see `tool_finished`).
        @return True when a tool was armed (and has ended or stayed); False when idle.
        @note Order (contract rule 2): capture (spec, points) -> clear points
            and preview -> idle -> `tool_finished(spec, points, commit)` ->
            act on its result: True re-arms the same spec silently
            (`tool_points_changed(spec, [])`, no `tool_armed`), False calls
            `tool_disarmed`. If the host armed a tool inside the callback the
            result is ignored and `tool_disarmed` is skipped.
        """
        if not self._armed:
            return False
        return self._end(bool(commit), allow_stay=True)

    def cancel(self) -> bool:
        """@brief Abandon the armed tool: `finish(False)`.

        @return True when a tool was cancelled; False when idle.
        """
        return self.finish(False)

    def hover(self, point, *, free: bool = False) -> None:
        """@brief Update the preview for the pointer at `point`.

        @param point Raw scene position, or None to clear (the pointer left).
        @param free True for an unconstrained point (Shift).
        @note With a point, `tool_preview_changed` is emitted only when the
            new preview differs (`!=`) from the previous one. With None
            (leave) it is ALWAYS emitted, once per call, with an empty
            preview, whatever was stored: a click resets the stored preview
            silently, and the host may still show a ghost or snap dot that
            only this emission clears (hosts treat clearing as idempotent).
            No-op when idle.
        """
        if not self._armed:
            return
        spec, epoch = self._spec, self._epoch
        if point is None:
            self._preview = []
            self._host.tool_preview_changed(spec, list(self._points), [])
            return
        new = list(self._host.tool_snap(spec, point, bool(free), tuple(self._points)))
        if not self._armed or self._epoch != epoch:
            return
        if new != self._preview:
            self._preview = new
            self._host.tool_preview_changed(spec, list(self._points), list(new))

    def _end(self, commit: bool, *, allow_stay: bool) -> bool:
        """End the armed tool following the finish protocol (the tool must be armed)."""
        spec, points = self._spec, self._points
        self._points = []
        self._preview = []
        self._armed = False
        self._spec = None
        self._epoch += 1
        epoch = self._epoch
        stay = bool(self._host.tool_finished(spec, points, commit))
        if self._armed or self._epoch != epoch:
            return True                     # the host armed a tool from inside: it wins
        if stay and allow_stay:
            self._armed = True
            self._spec = spec
            self._epoch += 1
            self._host.tool_points_changed(spec, [])
        else:
            self._host.tool_disarmed(spec)
        return True


def _alive(obj) -> bool:
    """True when `obj` is a wrapper whose C++ object still exists (never raises)."""
    try:
        return obj is not None and not sip.isdeleted(obj)
    except TypeError:       # not a sip wrapper: a plain Python object is alive
        return obj is not None


class ClickToolRouter:
    """@brief Maps a view's mouse and key events to a `PointCollector`; owns the tool cursor.

    @note The router never emits Qt signals and never touches the scene
        (contract rule 7); it calls the camera only through `pan_active` and
        `pan_cancel()`. Each handler is meant to be called FIRST in the
        view's event handler and returns True when the event was consumed
        (the router has accepted it); on False the view continues as usual.
        Shift is read from the event's `modifiers()` for press, double click
        and move; `hover_at` (a hover with no event, e.g. after a key) reads
        `QApplication.keyboardModifiers()`, as the symbol editor does today.
    """

    def __init__(self, view: QGraphicsView, collector: PointCollector, camera,
                 *, on_arm: Callable[[], None] | None = None):
        """@brief Bind the router to a view, its collector and its camera.

        @param view The graphics view (maps event positions, owns the cursor and the focus).
        @param collector The collector fed by the events.
        @param camera The view's `Camera` (`pan_active`, `pan_cancel()`).
        @param on_arm Optional callable run by `arm` before the tool is armed
            (the host's `cancel_interaction` hook: cancel a drag, a rubber
            band, a placing glyph). Exceptions it raises propagate.
        """
        self._view = view
        self._collector = collector
        self._camera = camera
        self._on_arm = on_arm
        #: Viewport position of the left press that added a point (drag rule); None otherwise.
        self._press: QPoint | None = None
        self._last_hover: tuple | None = None
        #: True from a consumed left press / double click until its release.
        self._swallow_release = False
        #: True while the router believes the viewport shows the tool cursor.
        self._cursor_set = False
        #: True after the tool cursor was unset: `QGraphicsView` may still hold it as the
        #: "original" cursor of a hovered item and put it back on leaving that item.
        self._stale_cursor = False

    # -- helpers ---------------------------------------------------------

    def _usable(self) -> bool:
        """The view is a live object the router can map through."""
        return _alive(self._view)

    @staticmethod
    def _viewport_pos(event) -> QPoint:
        """Viewport position (px) of a mouse event, whatever the Qt flavour."""
        if hasattr(event, "position"):
            return event.position().toPoint()
        return event.pos()

    def _scene(self, pos: QPoint) -> QPointF:
        """Scene position of a viewport position."""
        return QPointF(self._view.mapToScene(pos))

    @staticmethod
    def _shift(event) -> bool:
        """Whether the event carries the Shift modifier."""
        return bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

    def _sync_cursor(self) -> None:
        """Keep the viewport cursor in step with `collector.armed` (set on arm, unset on end)."""
        if not self._usable():
            self._cursor_set = False
            return
        viewport = self._view.viewport()
        if self._collector.armed and not self._cursor_set:
            viewport.setCursor(Qt.CursorShape.CrossCursor)
            self._cursor_set = True
        elif not self._collector.armed and self._cursor_set:
            viewport.unsetCursor()
            self._cursor_set = False
            self._stale_cursor = True

    def settle_cursor(self, event) -> None:
        """@brief Drop a tool cursor that `QGraphicsView` restored after the tool had ended.

        @param event The mouse move, after the view's base class handled it.
        @note The view saves the viewport cursor when the pointer enters an item
            with its own cursor and restores it on leaving. A tool that ends
            over such an item leaves the CrossCursor saved, and it comes back
            on the next move off the item. Called after the base handling of
            each move: when nothing is armed and no item under the pointer
            has a cursor, a viewport still showing the CrossCursor is reset.
        """
        if not self._stale_cursor or not self._usable() or self._collector.armed:
            return
        if any(item.hasCursor() for item in self._view.items(self._viewport_pos(event))):
            return
        viewport = self._view.viewport()
        if viewport.cursor().shape() == Qt.CursorShape.CrossCursor:
            viewport.unsetCursor()
        self._stale_cursor = False

    # -- public ----------------------------------------------------------

    def arm(self, spec: ToolSpec) -> bool:
        """@brief Arm a tool: focus, cancel any pan, run the host hook, collector, cursor.

        @param spec The tool to arm.
        @return The collector's `arm` result (False for an invalid spec; then nothing else is done).
        @note Order: `camera.pan_cancel()` (it resets the cursor, so it runs
            before ours), the `on_arm` hook, `collector.arm(spec)` (which ends
            any other armed tool first), `setFocus`, then the CrossCursor on
            the VIEWPORT.
        """
        if not self._usable() or not isinstance(spec, ToolSpec):
            return False
        self._camera.pan_cancel()
        if self._on_arm is not None:
            self._on_arm()
        self._press = None
        self._last_hover = None
        self._swallow_release = False
        ok = self._collector.arm(spec)
        if ok:
            self._view.setFocus()
        self._cursor_set = False        # pan_cancel/on_arm may have reset it: set it again
        self._sync_cursor()
        return ok

    def disarm(self) -> None:
        """@brief Cancel the armed tool and unset the cursor."""
        self._press = None
        self._last_hover = None
        self._swallow_release = False
        self._collector.disarm()
        self._sync_cursor()

    def press(self, event) -> bool:
        """@brief A mouse press.

        @param event The `QMouseEvent`.
        @return False when no tool is armed or for the middle (or any other)
            button, so the camera can pan; True (accepted) otherwise.
        @note Left: records the press for the drag rule and calls
            `collector.click(point, free=Shift)`. Right: `collector.finish(True)`.
            While `camera.pan_active` a left or right press is swallowed with
            no point and no finish (a pan in progress must not draw).
        """
        if not self._usable():
            return False
        # A new press supersedes any stale record: a modal prompt opened by the finishing
        # click eats its own release, which would otherwise leave the swallow flag set.
        self._press = None
        self._last_hover = None
        self._swallow_release = False
        if not self._collector.armed:
            return False
        button = event.button()
        if button not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return False
        event.accept()
        if self._camera.pan_active:
            return True
        if button == Qt.MouseButton.RightButton:
            self._press = None
            self._last_hover = None
            self._collector.finish(True)
        else:
            pos = self._viewport_pos(event)
            self._swallow_release = True
            self._collector.click(self._scene(pos), free=self._shift(event))
            spec = self._collector.spec
            # Remember the press while a shape is IN PROGRESS after the click, whether the click
            # was accepted or refused as a duplicate of the last vertex (the drag then adds the
            # release point). A press that completed or cleared a shape (a persistent tool that
            # stayed armed with an empty list, a count-1 tool) must not start the next one by dragging.
            if spec is not None and spec.drag_adds_release_point and self._collector.points:
                self._press = QPoint(pos)
            else:
                self._press = None
                self._last_hover = None
        self._sync_cursor()
        return True

    def release(self, event) -> bool:
        """@brief A mouse release.

        @param event The `QMouseEvent`.
        @return True for a left release that follows a consumed press or
            double click, or arrives while a tool is armed; False otherwise.
        @note The press is remembered when a shape is in progress after it
            (points collected), even if the click was refused as a duplicate
            of the last vertex; not when it completed or cleared a shape.
            A release at least `QApplication.startDragDistance()` (manhattan,
            viewport px) from the press adds the release point, when the tool
            has `drag_adds_release_point`. A release at the press, or after a
            double click, adds nothing.
        """
        if not self._usable() or event.button() != Qt.MouseButton.LeftButton:
            return False
        press, swallow = self._press, self._swallow_release
        self._press = None
        self._last_hover = None
        self._swallow_release = False
        armed = self._collector.armed
        if not (armed or swallow):
            return False
        event.accept()
        if armed and press is not None:
            pos = self._viewport_pos(event)
            delta = pos - press
            spec = self._collector.spec
            if (spec is not None and spec.drag_adds_release_point
                    and abs(delta.x()) + abs(delta.y()) >= QApplication.startDragDistance()):
                self._collector.click(self._scene(pos), free=self._shift(event))
        self._sync_cursor()
        return True

    def double_click(self, event) -> bool:
        """@brief A mouse double click.

        @param event The `QMouseEvent`.
        @return False when no tool is armed or for the middle button; True otherwise.
        @note Left: `collector.click(point, quiet=True)` THEN `collector.finish(True)`,
            the finish only when the click left points collected (a fixed-count
            tool that the click already finished, even one that stayed armed
            with an empty list, finishes exactly once). The release that
            follows adds no point. Right and swallowed pan clicks do nothing
            more than being consumed.
        """
        if not self._usable() or not self._collector.armed:
            return False
        button = event.button()
        if button not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return False
        event.accept()
        if self._camera.pan_active or button == Qt.MouseButton.RightButton:
            return True
        self._press = None
        self._last_hover = None
        self._swallow_release = True
        self._collector.click(self._scene(self._viewport_pos(event)), free=self._shift(event), quiet=True)
        if self._collector.armed and self._collector.points:
            self._collector.finish(True)
        self._sync_cursor()
        return True

    def key(self, event) -> bool:
        """@brief A key press.

        @param event The `QKeyEvent`.
        @return False when no tool is armed or the key is not handled; True otherwise.
        @note Escape asks `camera.pan_cancel()` first: a cancelled pan takes
            the key and the tool survives (the CrossCursor is set again,
            the pan reset it); otherwise the tool is cancelled. Return and
            Enter (keypad too) finish with commit. Nothing else is handled.
        """
        if not self._usable() or not self._collector.armed:
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._camera.pan_cancel():
                self._cursor_set = False
                self._sync_cursor()
            else:
                self._press = None
                self._last_hover = None
                self._swallow_release = False
                self._collector.cancel()
                self._sync_cursor()
            event.accept()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._collector.finish(True)
            self._sync_cursor()
            event.accept()
            return True
        return False

    def move(self, event) -> bool:
        """@brief A mouse move: hover the preview.

        @param event The `QMouseEvent`.
        @return True when the preview was updated (armed, no pan); the router
            does NOT accept the event, the view decides (the symbol goes on to
            its readout, the footprint treats it as consumed).
        @note Shift comes from the event's `modifiers()`. The CrossCursor is
            re-applied here when something else (a finished pan) reset it.
        """
        if not self._usable() or not self._collector.armed or self._camera.pan_active:
            return False
        viewport = self._view.viewport()
        if self._cursor_set and viewport.cursor().shape() != Qt.CursorShape.CrossCursor:
            viewport.setCursor(Qt.CursorShape.CrossCursor)
        point, free = self._scene(self._viewport_pos(event)), self._shift(event)
        self._last_hover = (point, free)
        self._collector.hover(point, free=free)
        self._sync_cursor()
        return True

    def hover_at(self, scene_point: QPointF) -> None:
        """@brief Update the preview for a scene point with no event (a key changed the Shift state).

        @param scene_point Scene position of the pointer.
        @note Shift is `QApplication.keyboardModifiers()` here, as the symbol
            editor reads it today.
        """
        if self._usable() and self._collector.armed and not self._camera.pan_active:
            free = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
            if self._last_hover == (scene_point, free):
                return              # `move` just snapped this very point: do not snap twice
            self._last_hover = (scene_point, free)
            self._collector.hover(scene_point, free=free)

    def leave(self) -> None:
        """@brief The pointer left the view: clear the preview (emits once if it was not empty)."""
        self._last_hover = None
        self._collector.hover(None)

    def hide(self, spontaneous: bool = False) -> None:
        """@brief The view was hidden: cancel the tool and unset the cursor. Idempotent.

        @param spontaneous True for a window-system hide (minimise): the work in
            progress is kept, only a tab switch or programmatic hide cancels.
        """
        if spontaneous:
            return
        self._press = None
        self._last_hover = None
        self._swallow_release = False
        self._collector.cancel()
        self._sync_cursor()
