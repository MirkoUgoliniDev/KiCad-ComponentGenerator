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
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PyQt6.QtWidgets import QApplication, QGraphicsView

#: Reason given to `ClickToolHost.tool_refused` when a click adds nothing
#: (the host's snap returned no point, or the point repeats the previous one).
REFUSED_COINCIDENT = "coincident"

#: Largest number of nested cancellations `PointCollector.arm` performs when a
#: host keeps arming tools from inside `tool_finished` (a runaway-host guard).
_MAX_ARM_CANCELS = 8

#: Angle modes of the drawing tools (plan `docs/footprint-drawing-kicad-plan.md`,
#: A6). The collector only STORES the mode and hands it to the host's
#: `tool_snap_ex`; the geometry and the ONE cycling order live in
#: `kicadgen/core/draw_constraints.py` (pure Python, no Qt): the constants here
#: are its plain string values, so the two modules cannot drift apart.
from ..core.draw_constraints import ANGLE_MODES as _CORE_ANGLE_MODES, AngleMode as _AngleMode

ANGLE_FREE = _AngleMode.FREE.value
#: Segments constrained to horizontal, vertical and the two diagonals.
ANGLE_45 = _AngleMode.DEG45.value
#: Segments constrained to horizontal and vertical.
ANGLE_90 = _AngleMode.DEG90.value
#: Shift+Space cycling order (free -> 90 -> 45, KiCad's declared order), as plain strings.
ANGLE_MODES = tuple(mode.value for mode in _CORE_ANGLE_MODES)


def next_angle_mode(mode: str) -> str:
    """@brief The angle mode that follows `mode` in the Shift+Space cycle.

    @param mode One of `ANGLE_MODES` (an equal string, e.g. a `str` enum value, is accepted).
    @return The next mode, wrapping from the last back to `ANGLE_FREE`; an
        unknown mode returns `ANGLE_FREE` (never raises: called from a key slot).
    """
    try:
        index = ANGLE_MODES.index(str(getattr(mode, "value", mode)))
    except ValueError:
        return ANGLE_FREE
    return ANGLE_MODES[(index + 1) % len(ANGLE_MODES)]


def _angle_mode_value(mode) -> str | None:
    """The plain string of a known angle mode (accepts a `str` enum), None when unknown."""
    value = getattr(mode, "value", mode)
    return value if isinstance(value, str) and value in ANGLE_MODES else None


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
    #: The threshold counts the closing click: a polygon of at least three
    #: vertices (plan D1) uses close_after=4; 3 would close on A, B, A.
    close_after: int = 0
    #: True: the host's `tool_finished` may return True to stay armed after a
    #: committed shape; this field documents the intent and is read by hosts.
    persistent: bool = False
    #: True: a press-move-release drag adds the release point (router rule).
    drag_adds_release_point: bool = True
    #: Fewest points a committed shape needs (a polygon: 3). Informative only:
    #: the collector never refuses a finish because of it; the host reads it in
    #: `tool_finished` and applies its own outcome (toast, status line). 0 = no rule.
    min_points: int = 0


class ClickToolHost(Protocol):
    """@brief What a tab implements to receive the events of a `PointCollector`.

    @note RE-ENTRANCY (contract rule 9). Every callback runs with the
        collector in a consistent state and the host MAY call `arm`,
        `disarm`, `cancel` and `finish` from inside any of them (they are
        safe and never raise). The host MUST NOT call `click` from inside
        `tool_snap` and SHOULD NOT call `hover`/`click` from inside
        `tool_finished`/`tool_disarmed` (the collector is idle there and
        both are ignored). The per-method notes list the exact rules.
    @note OPTIONAL callbacks (looked up with `getattr`, absent = ignored, so a
        host written before them keeps working unchanged): `tool_snap_ex`
        replaces `tool_snap` when defined and also receives the angle mode;
        `tool_angle_mode_changed` is told when the angle mode changes. Both
        are documented on `ClickToolHostEx`.
    @note SHIFT = FREE (plan contract 3): `free` True means no object snap and
        no angle constraint; the grid rounding still applies when the host's
        grid snap is on. The router computes `free`, the host applies it.
    @note A third OPTIONAL callback, `tool_snap_kind`, lets the preview be
        re-emitted when only the kind of the snapped hot point changed (see
        `ClickToolHostEx.tool_snap_kind`).
    """

    def tool_snap(self, spec: ToolSpec, point: QPointF, free: bool, points: Sequence) -> list:
        """@brief Turn a raw scene point into the points a click would APPEND.

        @param spec The armed tool.
        @param point Raw scene position of the click or of the hover.
        @param free True when the user asked for an unconstrained point (Shift):
            no object snap and no angle constraint, grid rounding kept.
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


class ClickToolHostEx(ClickToolHost, Protocol):
    """@brief The OPTIONAL callbacks a host may add to `ClickToolHost` (angle mode).

    @note The collector finds them with `getattr`: a host may implement either,
        both or none. Same re-entrancy rules as `tool_snap`.
    """

    def tool_snap_ex(self, spec: ToolSpec, point: QPointF, free: bool, points: Sequence,
                     angle_mode: str) -> list:
        """@brief `tool_snap` with the angle mode: called INSTEAD of `tool_snap` when defined.

        @param spec The armed tool.
        @param point Raw scene position of the click or of the hover.
        @param free True for Shift: no object snap and no angle constraint
            (the angle mode must then be ignored), grid rounding kept.
        @param points Snapshot (tuple) of the points collected so far.
        @param angle_mode One of `ANGLE_MODES`: the constraint the host applies
            to the segment from the last collected point to `point` (geometry in
            `kicadgen/core/draw_constraints.py`); the collector applies none.
        @return As `tool_snap`.
        """

    def tool_angle_mode_changed(self, mode: str) -> None:
        """@brief The angle mode changed (Shift+Space or `set_angle_mode`).

        @param mode The new mode, one of `ANGLE_MODES`.
        @note Called only on an actual change, before the preview is
            recomputed with the new mode; also called while no tool is armed
            (a panel control). The host may update its control and save it.
        """

    def tool_snap_kind(self):
        """@brief The kind of hot point the LAST `tool_snap`/`tool_snap_ex` call snapped to.

        @return Any hashable value the host uses to draw its typed snap marker
            (e.g. "pad", "endpoint"), or None for a plain grid point.
        @note Read by the collector right after each hover snap and compared,
            together with the preview points, with the previous hover: a
            pointer whose snapped point stays the same while its kind changes
            (a grid node that coincides with a pad centre, the object snap
            switched on or off) still gets `tool_preview_changed`, so the
            marker is never missing or stale. Must be cheap and pure.
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
        #: Angle mode handed to `tool_snap_ex`; kept across tools (one per tab).
        self._angle_mode = ANGLE_FREE
        #: (point, free) of the last hover with a point, so Backspace and an
        #: angle-mode change can refresh the ghost without a mouse move; None
        #: after a leave, an arm or an end.
        self._last_hover: tuple | None = None
        #: The host's `tool_snap_kind()` after the snap that produced `_preview`
        #: (None without the optional callback); compared with the points.
        self._preview_kind = None

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

    @property
    def angle_mode(self) -> str:
        """@brief The angle mode handed to the host's `tool_snap_ex`.

        @return One of `ANGLE_MODES`; `ANGLE_FREE` for a new collector.
        """
        return self._angle_mode

    def set_angle_mode(self, mode) -> bool:
        """@brief Set the angle mode (panel control, saved configuration, Shift+Space).

        @param mode One of `ANGLE_MODES` (a `str` enum with such a value is accepted).
        @return True when the mode changed; False when it was already set or is
            unknown (nothing done, never raises).
        @note On a change: store, then the optional host callback
            `tool_angle_mode_changed(mode)`, then, while armed and with a
            remembered hover, the preview is recomputed for that hover (emitted
            only when it differs). Works while idle too (no preview then).
        """
        value = _angle_mode_value(mode)
        if value is None or value == self._angle_mode:
            return False
        self._angle_mode = value
        callback = getattr(self._host, "tool_angle_mode_changed", None)
        if callable(callback):
            callback(value)
        if self._armed and self._last_hover is not None:
            point, free = self._last_hover
            self.hover(point, free=free)
        return True

    def cycle_angle_mode(self) -> str:
        """@brief Move to the next angle mode of `ANGLE_MODES` (Shift+Space).

        @return The new mode (see `set_angle_mode` for the callbacks).
        """
        self.set_angle_mode(next_angle_mode(self._angle_mode))
        return self._angle_mode

    @property
    def hover_point(self):
        """@brief The scene point of the last remembered hover, or None.

        @return The point given to the last `hover` with a point; None after a
            leave, an arm or the end of a tool (a click keeps it: the pointer
            is still there).
        """
        return None if self._last_hover is None else self._last_hover[0]

    @property
    def hover_free(self) -> bool | None:
        """@brief The Shift state (`free`) of the last remembered hover, or None without one.

        @return True / False, or None when no hover is remembered.
        """
        return None if self._last_hover is None else self._last_hover[1]

    def rehover(self, free: bool) -> bool:
        """@brief Re-run the last hover with another Shift state (Shift pressed or released, no mouse move).

        @param free The new Shift state.
        @return True when the hover was re-run; False when idle, with no
            remembered hover, or when `free` equals the remembered state
            (a held Shift's key auto-repeat costs nothing).
        @note The preview follows the usual emission rule of `hover`, so the
            ghost and the snap marker show what a click WITH the new Shift
            state would add.
        """
        if not self._armed or self._last_hover is None or self._last_hover[1] == bool(free):
            return False
        self.hover(self._last_hover[0], free=bool(free))
        return True

    def _snap_kind(self):
        """The host's optional `tool_snap_kind()` (None when the host has none)."""
        callback = getattr(self._host, "tool_snap_kind", None)
        return callback() if callable(callback) else None

    def _snap(self, spec: ToolSpec, point, free: bool) -> list:
        """The host's snap: `tool_snap_ex` with the angle mode when defined, else `tool_snap`."""
        snap_ex = getattr(self._host, "tool_snap_ex", None)
        if callable(snap_ex):
            return list(snap_ex(spec, point, bool(free), tuple(self._points), self._angle_mode))
        return list(self._host.tool_snap(spec, point, bool(free), tuple(self._points)))

    def undo_last(self) -> bool:
        """@brief Remove the last collected point (Backspace, KiCad behaviour).

        @return True when a point was removed; False, with nothing done and the
            tool still armed, when idle or with no point collected.
        @note Exactly ONE point is removed, even when the click that added it
            appended two (the wire's corner). Order: pop ->
            `tool_points_changed` -> the preview is recomputed from the last
            remembered hover and `tool_preview_changed` is emitted ONCE
            whatever its value (the old ghost was drawn from the removed
            point), with [] when no hover is remembered. A host that ended or
            re-armed the tool inside `tool_points_changed` stops the rest.
        """
        if not self._armed or not self._points:
            return False
        spec, epoch = self._spec, self._epoch
        self._points.pop()
        self._preview = []
        self._preview_kind = None
        self._host.tool_points_changed(spec, list(self._points))
        if not self._armed or self._epoch != epoch:
            return True
        new: list = []
        kind = None
        if self._last_hover is not None:
            point, free = self._last_hover
            new = self._snap(spec, point, free)
            if not self._armed or self._epoch != epoch:
                return True
            kind = self._snap_kind()
        self._preview = new
        self._preview_kind = kind
        self._host.tool_preview_changed(spec, list(self._points), list(new))
        return True

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
        self._preview_kind = None
        self._last_hover = None
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
        @param quiet True suppresses `tool_refused` (the second click of a double click, the release point of a drag).
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
        appended = self._snap(spec, point, free)
        if not self._armed or self._epoch != epoch:
            return False
        if not appended or (self._points and appended[0] == self._points[-1]):
            if not quiet:
                self._host.tool_refused(spec, REFUSED_COINCIDENT)
            return False
        self._points.extend(appended)
        self._preview = []
        self._preview_kind = None
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
            new preview differs (`!=`) from the previous one, or when the
            host's optional `tool_snap_kind()` differs from the previous
            hover's (same point, other marker). With None
            (leave) it is ALWAYS emitted, once per call, with an empty
            preview, whatever was stored: a click resets the stored preview
            silently, and the host may still show a ghost or snap dot that
            only this emission clears (hosts treat clearing as idempotent).
            No-op when idle. A point is remembered with its `free` flag (and
            None forgets it) so `undo_last` and `set_angle_mode` can refresh
            the ghost without a mouse move.
        """
        if not self._armed:
            return
        spec, epoch = self._spec, self._epoch
        if point is None:
            self._preview = []
            self._preview_kind = None
            self._last_hover = None
            self._host.tool_preview_changed(spec, list(self._points), [])
            return
        self._last_hover = (point, bool(free))
        new = self._snap(spec, point, free)
        if not self._armed or self._epoch != epoch:
            return
        kind = self._snap_kind()
        if new != self._preview or kind != self._preview_kind:
            self._preview = new
            self._preview_kind = kind
            self._host.tool_preview_changed(spec, list(self._points), list(new))

    def _end(self, commit: bool, *, allow_stay: bool) -> bool:
        """End the armed tool following the finish protocol (the tool must be armed)."""
        spec, points = self._spec, self._points
        self._points = []
        self._preview = []
        self._preview_kind = None
        self._last_hover = None
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
        Pressing or releasing Shift WITHOUT moving the mouse re-runs the last
        hover with the new state (`modifiers_changed`, called by `key` for
        the press and by `key_release` for the release), so the ghost and
        the next click always agree.
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
            has `drag_adds_release_point`; it is a quiet click, so a release that
            snaps back onto the last point is dropped silently. A release at the press, or after a
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
                # Quiet: a hand that trembled past the drag distance but snapped back
                # onto the pressed point is a click, not a "coincident point" mistake.
                self._collector.click(self._scene(pos), free=self._shift(event), quiet=True)
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
            Enter (keypad too) finish with commit. Backspace removes the last
            collected point (`collector.undo_last`) and is consumed even with
            no point left, so the view never turns it into a deletion of the
            selection while drawing; the tool stays armed. Shift+Space cycles
            the angle mode (`collector.cycle_angle_mode`); the ghost is then
            recomputed with the REAL modifiers, i.e. free while Shift is held,
            and shows the new constraint once Shift is released
            (`key_release`). Shift alone re-runs the last hover as free
            (`modifiers_changed`) and is NOT consumed (returns False), so the
            view's own Shift handling keeps working. Nothing else is handled.
        """
        if not self._usable() or not self._collector.armed:
            return False
        key = event.key()
        if key == Qt.Key.Key_Shift:
            self.modifiers_changed(True)
            return False
        if key == Qt.Key.Key_Escape:
            if self._camera.pan_cancel():
                self._cursor_set = False
                self._sync_cursor()
            else:
                self.cancel_tool()
            event.accept()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._collector.finish(True)
            self._sync_cursor()
            event.accept()
            return True
        if key == Qt.Key.Key_Backspace:
            self._collector.undo_last()
            self._sync_cursor()
            event.accept()
            return True
        if key == Qt.Key.Key_Space and self._shift(event):
            self._collector.cycle_angle_mode()
            self._sync_cursor()
            event.accept()
            return True
        return False

    def key_release(self, event) -> bool:
        """@brief A key release; views call it from `keyReleaseEvent`.

        @param event The `QKeyEvent`.
        @return Always False: nothing is consumed, the view handles the
            release as before.
        @note A Shift release re-runs the last hover with the constraint back
            on (`modifiers_changed(False)`): after Shift+Space the ghost then
            shows the NEW angle mode without a mouse move. Auto-repeat
            releases are ignored.
        """
        if not self._usable() or not self._collector.armed:
            return False
        if event.key() == Qt.Key.Key_Shift and not event.isAutoRepeat():
            self.modifiers_changed(False)
        return False

    def modifiers_changed(self, event_or_modifiers) -> bool:
        """@brief The Shift state changed with no mouse move: re-run the last hover with it.

        @param event_or_modifiers The new state as a bool (free), a
            `Qt.KeyboardModifier` combination, a `QKeyEvent` of the Shift key
            itself (press = Shift held, release = Shift up, whatever the
            platform reports in `modifiers()` for that event), or any other
            event with `modifiers()`.
        @return True when the preview was recomputed; False when idle, during
            a pan, with no remembered hover, or when the state did not change.
        @note The remembered hover is the collector's (a click keeps it: the
            pointer is still there), so a click followed by Shift updates the
            ghost of the next segment. The router's own hover memory is
            updated too, so a following `hover_at` at the same point does not
            snap twice.
        """
        if not self._usable() or not self._collector.armed or self._camera.pan_active:
            return False
        free = self._free_from(event_or_modifiers)
        point = self._collector.hover_point
        if point is None or not self._collector.rehover(free):
            return False
        self._last_hover = (point, free)
        self._sync_cursor()
        return True

    @staticmethod
    def _free_from(value) -> bool:
        """The Shift state carried by a bool, a modifier combination or an event."""
        if isinstance(value, bool):
            return value
        key = getattr(value, "key", None)
        if callable(key) and key() == Qt.Key.Key_Shift:
            return value.type() == QEvent.Type.KeyPress
        modifiers = getattr(value, "modifiers", None)
        if callable(modifiers):
            value = modifiers()
        try:
            return bool(value & Qt.KeyboardModifier.ShiftModifier)
        except TypeError:
            return bool(value)

    @property
    def angle_mode(self) -> str:
        """@brief The collector's angle mode (one of `ANGLE_MODES`).

        @return The mode handed to the host's `tool_snap_ex`.
        """
        return self._collector.angle_mode

    def set_angle_mode(self, mode) -> bool:
        """@brief Set the angle mode from the panel control or the saved configuration, armed or not.

        @param mode One of `ANGLE_MODES`.
        @return True when it changed (the host's optional `tool_angle_mode_changed`
            was called once); False when unchanged or unknown.
        """
        return self._collector.set_angle_mode(mode)

    def cancel_tool(self) -> bool:
        """@brief Cancel the armed tool as Escape does: forget the press record first, then `collector.cancel()`.

        @return True when a tool was cancelled (or re-armed itself by its host); False when idle.
        @note A caller that cancels on its own key (the measuring tool's
            Escape ladder) must come here, not to the collector: a press
            recorded before the cancel would otherwise turn the following
            release into a new (quiet) point.
        """
        self._press = None
        self._last_hover = None
        self._swallow_release = False
        cancelled = self._collector.cancel()
        self._sync_cursor()
        return cancelled

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
