"""@brief Shared drag, hover, key-repeat and checkpoint helpers of the symbol editor and the footprint preview.

Both graphics views (the symbol editor's `_SymbolView` and the footprint
preview's `FootprintView`) keep their classes, signals, hit-testing, snapping
and committing; what they share is handled here and only here (plan
`docs/canvas-gestures-plan.md`, section 2a):

- `GestureController`: the drag state machine (idle -> dragging -> idle), the
  live-vs-stepped policy, «the release always passes through a move», exactly
  one `gesture_end` per drag, state cleared BEFORE the host callbacks so a
  host may re-enter (`cancel()`, `press()`) from inside `gesture_end`.
- `CancelOnEscapeAndHide`: Escape while a drag runs, and the view hidden,
  cancel the drag.
- `HoverGroups`: the reveal rule of the hover handles (same screen margin at
  any zoom, ring groups, the dragged owner always revealed).
- `StepMemo` and `Identity`: the identical-step memo of a stepped drag.
- `KeyRepeatFilter`, `DeferredCheckpoint`, `bind_shortcuts`: the auto-repeat
  deferral of the recovery-file write.

The module is Qt-only: it imports no model, no tab and no translated string,
so the two views can adopt it independently and the unit tests drive it with
a bare `QGraphicsView` and a fake host.

@note Scene items referenced here are only ones built in Python and added
    with `addItem` (kept-items rule): the hosts call `forget_items()` /
    `clear()` before every `scene.clear()`, and every access to an item goes
    through `_alive`, so a wrapper Qt already destroyed is skipped, never
    raised on (an exception in a slot aborts the application).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsView

#: Screen pixels, at any zoom, around a hover group's base rect within which
#: its handles are revealed. Shared by both tabs: `symbol_tab.HANDLE_HOVER_MARGIN`
#: and `footprint_view.HOVER_KEEP_MARGIN_PX` are aliases of this value.
HOVER_MARGIN_PX = 12

#: Side, in screen pixels, of a drag handle square (vertex, shape point, body
#: ring, text anchor); drawn ignoring the view transform.
HANDLE_SIZE_PX = 6


def _alive(item) -> bool:
    """True when `item` is a wrapper whose C++ object still exists (never raises)."""
    try:
        return item is not None and not sip.isdeleted(item)
    except TypeError:       # not a sip wrapper at all: a plain Python object is alive
        return item is not None


class Identity:
    """@brief Token compared by identity, for `StepMemo` tokens.

    @note The symbol editor's step memo asks whether the draft is still the
        very object the previous step produced (`last[2] is self._draft`):
        two equal drafts built by different paths must not count as the
        same step. Wrapping the object in `Identity` lets the memo compare
        tokens with `==` while this component compares with `is`.
    """

    __slots__ = ('obj',)

    def __init__(self, obj):
        """@brief Wrap an object whose identity, not value, is the token.

        @param obj Any object; only `is` is ever applied to it.
        """
        self.obj = obj

    def __eq__(self, other) -> bool:
        """@brief Equal only to another `Identity` of the SAME object.

        @param other Any value.
        @return True when `other` is an `Identity` wrapping the same object.
        """
        return isinstance(other, Identity) and other.obj is self.obj

    def __ne__(self, other) -> bool:
        """@brief The negation of `__eq__`.

        @param other Any value.
        @return True unless `other` is an `Identity` of the same object.
        """
        return not self.__eq__(other)

    __hash__ = None

    def __repr__(self) -> str:
        """@brief Debug representation naming the wrapped object.

        @return `Identity(<repr of the object>)`.
        """
        return f'Identity({self.obj!r})'


class StepMemo:
    """@brief What the last step of the running drag committed, so an identical step is skipped.

    @note A drag sends one step per mouse move and most moves stay on the
        same grid node: before the memo, every one of them rebuilt the layout
        and redrew the scene to show the same picture. The memo is scoped to
        ONE drag: `start()` opens it (the controller calls it on a press),
        `stop()` closes it (on the finish); outside, `repeats` is False and
        `remember` stores nothing, so nothing is ever skipped outside a drag
        and the first step of a new drag always goes through the full path.
        The key is the data itself (kind, key, token), so any real change
        (another node, another target, another path, an edit of the draft
        between two steps) still goes through.
    """

    def __init__(self):
        """@brief A stopped memo: nothing remembered, `repeats` False."""
        self._running = False
        self._last = None

    @property
    def running(self) -> bool:
        """@brief Whether a drag is open (between `start()` and `stop()`).

        @return True between `start()` and `stop()`.
        """
        return self._running

    def start(self) -> None:
        """@brief Open the memo for a new drag, forgetting any previous step."""
        self._running = True
        self._last = None

    def stop(self) -> None:
        """@brief Close the memo: `repeats` is False and `remember` a no-op until the next `start()`."""
        self._running = False
        self._last = None

    def repeats(self, kind: str, key, token=()) -> bool:
        """@brief Tell whether a step would commit exactly what the previous step of THIS drag committed.

        @param kind Path the step takes (the symbol uses 'accept' or
            'restore'): the same key through another path is another step.
        @param key The data the step is about to commit (rows, a point...),
            compared with `==`.
        @param token Extra state the step depends on, compared with `==`;
            wrap in `Identity` what must be compared by identity (the draft
            object the previous step produced, the status text it left).
        @return True only while the memo is running, when `remember` was
            called with an equal (kind, key, token) for the previous step.
        """
        if not self._running or self._last is None:
            return False
        last_kind, last_key, last_token = self._last
        return last_kind == kind and last_token == token and last_key == key

    def remember(self, kind: str, key, token=()) -> None:
        """@brief Record what the step just committed; a no-op while stopped.

        @param kind Path the step took.
        @param key The data it committed (kept by reference: pass a private copy).
        @param token Extra state, see `repeats`.
        """
        self._last = (kind, key, token) if self._running else None


@dataclass
class DragPlan:
    """@brief How the host wants a drag handled, returned by `GestureHost.gesture_begin`.

    @note `policy` 'live' moves `live_items` with the pointer in the scene and
        commits only at the drop (`gesture_end` with the last point); 'stepped'
        gives every move, and the release point, to `gesture_step`, which snaps
        and commits (or shows a ghost) itself.
    """

    #: 'live' or 'stepped'.
    policy: str
    #: (item, position at the press) of every scene item that follows the
    #: pointer; 'live' only. Mutated by the controller on rebind/forget.
    live_items: list[tuple[QGraphicsItem, QPointF]] = field(default_factory=list)
    #: Cursor shown on the viewport while the drag runs; None keeps the current one.
    cursor: Qt.CursorShape | None = None

    def __post_init__(self):
        """@brief Validate the policy and copy the live positions.

        @throws ValueError When `policy` is neither 'live' nor 'stepped'.
        """
        if self.policy not in ('live', 'stepped'):
            raise ValueError(f"DragPlan.policy must be live or stepped, got {self.policy!r}")
        self.live_items = [(item, QPointF(origin)) for item, origin in self.live_items]


class GestureHost(Protocol):
    """@brief What a view's owner implements to receive the drags of a `GestureController`."""

    def gesture_begin(self, target: tuple, point: QPointF) -> DragPlan | None:
        """@brief Prepare a drag on `target` pressed at `point` (scene coordinates).

        @param target Tuple identifying what was pressed (('pin', n), ('text', key)...).
        @param point Scene position of the press.
        @return The `DragPlan`, or None to refuse the press (nothing is captured then).
        """

    def gesture_step(self, target: tuple, point: QPointF) -> None:
        """@brief One step of a 'stepped' drag: every move AND the release point.

        @param target The target of the running drag.
        @param point Scene position of the pointer.
        """

    def gesture_end(self, target: tuple, last: QPointF | None, commit: bool) -> None:
        """@brief The drag is over; called exactly once per drag, with the controller already idle.

        @param target The target of the drag that ended.
        @param last Scene position of the last move (the drop point on a
            commit), None when the pointer never moved.
        @param commit True on a drop, False on a cancel (Escape, hide, host).
        """


class GestureController(QObject):
    """@brief The drag state machine of a graphics view: idle -> dragging -> idle, one end per drag.

    @note Hit-testing stays in the view (it decides WHAT was pressed and
        calls `press`), snapping and committing stay in the host (`gesture_step`
        / `gesture_end`). The controller owns the capture (mouse grab and
        cursor), the live move of the plan's items, the last/start points and
        the step memo. Its state is cleared BEFORE `gesture_end` is called, so
        the host may safely start another drag or cancel from inside it.
    """

    #: (target, point): a drag began (after the capture).
    started = pyqtSignal(object, object)
    #: (target, point): the pointer moved during the drag (after the step / the live move).
    moved = pyqtSignal(object, object)
    #: (commit): the drag ended, after `gesture_end` returned.
    finished = pyqtSignal(bool)

    def __init__(self, view: QGraphicsView, host: GestureHost, *, grab_mouse: bool):
        """@brief Bind the controller to a view and its host.

        @param view The graphics view whose viewport is captured; also the Qt parent.
        @param host Object implementing `GestureHost`.
        @param grab_mouse True to `grabMouse()` the viewport for the drag
            (the symbol editor); False to rely on Qt's implicit grab (the
            footprint preview). The release mirrors the choice.
        """
        super().__init__(view)
        self._view = view
        self._host = host
        self._grab_mouse = bool(grab_mouse)
        self._target = None
        self._plan = None
        self._start = None
        self._last = None
        #: Memo of the stepped drag in progress; started on the press, stopped on the finish.
        self.memo = StepMemo()

    @property
    def target(self) -> tuple | None:
        """@brief The target of the running drag, None when idle.

        @return The tuple given to `press`, or None.
        """
        return self._target

    @property
    def plan(self) -> DragPlan | None:
        """@brief The plan of the running drag, None when idle.

        @return The `DragPlan` the host returned, or None.
        """
        return self._plan

    @property
    def start_point(self) -> QPointF | None:
        """@brief Scene position of the press of the running drag, None when idle.

        @return A copy of the press point, or None.
        """
        return None if self._start is None else QPointF(self._start)

    @property
    def last_point(self) -> QPointF | None:
        """@brief Scene position of the last move of the running drag; None when idle or before any move.

        @return A copy of the last point, or None.
        """
        return None if self._last is None else QPointF(self._last)

    @property
    def live_items(self) -> list[tuple[QGraphicsItem, QPointF]]:
        """@brief The (item, origin) pairs following the pointer; empty when idle or 'stepped'.

        @return The plan's live list itself (mutated by rebind/forget), or an empty list.
        """
        return [] if self._plan is None else self._plan.live_items

    @property
    def active(self) -> bool:
        """@brief Whether a drag runs.

        @return True between a successful `press` and its finish.
        """
        return self._target is not None

    def press(self, target, point) -> bool:
        """@brief Begin a drag on `target` at `point` (scene coordinates).

        @param target Tuple identifying what was pressed; stored as a tuple.
        @param point Scene position of the press.
        @return True when the drag began; False, with nothing done, while a
            drag already runs or when the host refused (`gesture_begin`
            returned None).
        @note Order (contract rule 1): `memo.start()`, `host.gesture_begin`
            BEFORE the grab (a refusal leaves no capture and the memo
            stopped), grab the mouse (when `grab_mouse`), set the cursor
            (when the plan has one), then `started`.
        """
        if self._target is not None:
            return False
        self.memo.start()
        plan = self._host.gesture_begin(tuple(target), point)
        if plan is None or self._target is not None:
            # Refused, or the host began another drag from inside: no capture.
            if self._target is None:
                self.memo.stop()
            return False
        self._target = tuple(target)
        self._plan = plan
        self._start = QPointF(point)
        self._last = None
        viewport = self._view.viewport()
        if self._grab_mouse:
            viewport.grabMouse()
        if plan.cursor is not None:
            viewport.setCursor(plan.cursor)
        self.started.emit(self._target, QPointF(point))
        return True

    def move(self, point) -> bool:
        """@brief The pointer moved to `point` (scene coordinates) during the drag.

        @param point Scene position of the pointer.
        @return True when a drag was running; False (nothing done) when idle.
        @note 'live': every item whose wrapper still exists is placed at
            `origin + (point - start)`; a wrapper Qt destroyed is skipped,
            never raised on. 'stepped': `host.gesture_step(target, point)`.
            Both record `last_point` first and emit `moved` after.
        """
        if self._target is None:
            return False
        target, plan = self._target, self._plan
        point = QPointF(point)
        self._last = QPointF(point)
        if plan.policy == 'live':
            self._place_live_items(point - self._start)
        else:
            self._host.gesture_step(target, point)
        self.moved.emit(target, point)
        return True

    def release(self, point) -> bool:
        """@brief The button was released at `point`: a last move there, then the drop.

        @param point Scene position of the release (Windows coalesces moves:
            the actual drop point always counts).
        @return True when a drag was finished; False when idle.
        """
        if self._target is None:
            return False
        self.move(point)
        return self._finish(True)

    def cancel(self) -> bool:
        """@brief Abort the running drag without moving anything.

        @return True when a drag was cancelled; False when idle.
        @note The picture is the host's business: the symbol editor redraws
            the rows the drag started from, the footprint preview leaves the
            item where it is until the next redraw (D5).
        """
        if self._target is None:
            return False
        return self._finish(False)

    def rebind_live_items(self, items: Iterable[QGraphicsItem]) -> None:
        """@brief A redraw rebuilt the live items mid-drag: follow the new ones from where they were drawn.

        @param items The new scene items, drawn where the drag began; their
            current positions become the origins.
        @note The last delta (`last_point - start_point`) is re-applied at
            once when a move already happened, so the dragged text neither
            freezes nor jumps at the drop. No-op when idle or 'stepped'.
        """
        if self._target is None or self._plan.policy != 'live':
            return
        self._plan.live_items = [(item, QPointF(item.pos())) for item in items if _alive(item)]
        if self._last is not None:
            self._place_live_items(self._last - self._start)

    def forget_items(self) -> None:
        """@brief Drop every scene item reference, keeping the drag state (before a `scene.clear()`).

        @note The drag goes on: a following `rebind_live_items` resumes the
            live move, a release still ends it once through `gesture_end`.
        """
        if self._plan is not None:
            self._plan.live_items = []

    def _place_live_items(self, delta: QPointF) -> None:
        """Put every surviving live item at its origin plus `delta`, never raising."""
        for item, origin in self._plan.live_items:
            if not _alive(item):
                continue
            try:
                item.setPos(origin + delta)
            except RuntimeError:    # destroyed between the check and the call
                continue

    def _finish(self, commit: bool) -> bool:
        """End the drag: clear the state, release the capture, then tell the host once."""
        if self._target is None:
            return False
        target, last = self._target, self._last
        self._target = None
        self._plan = None
        self._start = None
        self._last = None
        self.memo.stop()
        viewport = self._view.viewport()
        if self._grab_mouse:
            viewport.releaseMouse()
        viewport.unsetCursor()
        self._host.gesture_end(target, last, commit)
        self.finished.emit(commit)
        return True


class CancelOnEscapeAndHide(QObject):
    """@brief Event filter cancelling the controller's drag on Escape (while a drag runs) and on Hide.

    @note Install it on the VIEW (the focus widget receives the key, the
        widget receives Hide) AFTER the `KeyRepeatFilter`: Qt runs the
        filters of an object in reverse installation order, so the one
        installed last runs first, and the hide must cancel the drag before
        the repeat flush writes the draft (see `install_canvas_filters`).
        Escape is consumed only when it cancelled a drag; every other event
        goes on to the view.
    """

    def __init__(self, controller: GestureController, *, escape: bool = True, parent: QObject | None = None):
        """@brief Bind the filter to a controller.

        @param controller The controller whose `cancel()` is called.
        @param escape False keeps Escape off, so only Hide cancels a drag; a
            host whose Escape means something else while dragging passes
            False (both tabs pass True: Escape cancels a running drag).
        @param parent Qt parent; the controller's view when None.
        """
        super().__init__(parent if parent is not None else controller.parent())
        self._controller = controller
        self.escape = bool(escape)

    def eventFilter(self, watched, event) -> bool:
        """@brief Qt override: cancel on Escape during a drag (consumed) and on Hide (passed on).

        @param watched The object the event is for.
        @param event The event.
        @return True only for an Escape press that cancelled a drag.
        """
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if self.escape and event.key() == Qt.Key.Key_Escape and self._controller.cancel():
                event.accept()
                return True
            return False
        if kind == QEvent.Type.Hide:
            self._controller.cancel()
        return False


class HoverGroups:
    """@brief The handle groups of a view and the rule revealing them near the pointer.

    @note A group is (items, base rect, ring, owner). The reveal area is the
        base rect grown by `margin_px` SCREEN pixels (divided by the view
        scale, so the margin is the same on screen at any zoom); with `ring`
        the rect shrunk by the same margin does not count (the symbol body
        covers the whole symbol: its handles show only near its outline).
        While a drag runs on the owner of a group, its handles stay revealed
        wherever the pointer is (a handle hidden under the pointer that
        drags it would be absurd); the other groups follow the pointer rule.
        Only Python-built items added with `addItem` belong here, and the
        host calls `clear()` before every `scene.clear()`.
    """

    def __init__(self, view: QGraphicsView, margin_px: float = HOVER_MARGIN_PX):
        """@brief Bind the groups to the view whose scale sets the margin.

        @param view The graphics view (its `transform().m11()` is the scale).
        @param margin_px Reveal margin in screen pixels.
        """
        self._view = view
        self.margin_px = float(margin_px)
        #: Registered groups: (items, base rect, ring, owner).
        self.groups: list[tuple[list[QGraphicsItem], QRectF, bool, tuple]] = []

    def clear(self) -> None:
        """@brief Forget every group (before the scene is cleared)."""
        self.groups = []

    def add(self, items: Iterable[QGraphicsItem], base: QRectF, ring: bool, owner: tuple) -> None:
        """@brief Register a group of handles.

        @param items The handle items shown or hidden together.
        @param base Scene rect the reveal area is built from (copied).
        @param ring True when only the band around `base` reveals the group.
        @param owner Tuple naming what the handles belong to, matched
            against the `dragged_owner` given to `revealed` and `apply`
            (the handles of the dragged owner stay revealed).
        """
        self.groups.append((list(items), QRectF(base), bool(ring), tuple(owner)))

    def scene_margin(self) -> float:
        """@brief The reveal margin in scene units at the view's current zoom.

        @return `margin_px / abs(m11)`, with a scale of 1.0 when m11 is 0.
        """
        return self.margin_px / abs(self._view.transform().m11() or 1.0)

    def revealed(self, point: QPointF | None, dragged_owner: tuple | None) -> set[tuple]:
        """@brief The owners whose handles are visible for a pointer position.

        @param point Scene position of the pointer; None (pointer outside)
            hides every group but the dragged owner's.
        @param dragged_owner Owner of the running drag, always revealed; None for no drag.
        @return The set of revealed owners (contract rule 8).
        """
        m = self.scene_margin()
        shown = set()
        for _items, base, ring, owner in self.groups:
            if dragged_owner is not None and owner == dragged_owner:
                shown.add(owner)
            elif point is None:
                continue
            elif base.adjusted(-m, -m, m, m).contains(point) and not (
                    ring and base.adjusted(m, m, -m, -m).contains(point)):
                shown.add(owner)
        return shown

    def apply(self, point: QPointF | None, dragged_owner: tuple | None, *,
              setter: Callable[[QGraphicsItem, bool], None] | None = None) -> set[tuple]:
        """@brief Show or hide every group's items according to `revealed`.

        @param point Scene position of the pointer, or None.
        @param dragged_owner Owner of the running drag, or None.
        @param setter Called as `setter(item, visible)` for every item;
            `item.setVisible(visible)` when None. Items Qt destroyed are skipped.
        @return The set of revealed owners.
        """
        shown = self.revealed(point, dragged_owner)
        for items, _base, _ring, owner in self.groups:
            visible = owner in shown
            for item in items:
                if not _alive(item):
                    continue
                try:
                    if setter is None:
                        item.setVisible(visible)
                    else:
                        setter(item, visible)
                except RuntimeError:    # destroyed between the check and the call
                    continue
        return shown


class KeyRepeatFilter(QObject):
    """@brief Event filter telling whether the shortcut being handled came from an auto-repeated key, and when the repeat ends.

    @note Qt sends the focus widget a ShortcutOverride event, carrying the
        key's auto-repeat flag, right before it activates a matching
        shortcut; `QShortcut.activated` itself does not say whether the key
        repeats. The flag is read once by `take()` in the shortcut's handler.
        `repeat_ended` fires on a real key release (not one of the auto-repeat
        pairs), on the focus leaving the widget (the release goes elsewhere)
        and on the widget hidden. Install it on the view, FIRST (see
        `CancelOnEscapeAndHide`).
    """

    #: The held key was released, the focus left or the widget was hidden.
    repeat_ended = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        """@brief A filter with no repeat recorded.

        @param parent Qt parent (the view, usually).
        """
        super().__init__(parent)
        self._repeat = False

    def eventFilter(self, watched, event) -> bool:
        """@brief Qt override: record the auto-repeat flag and report the end of a repeat; never consumes.

        @param watched The object the event is for.
        @param event The event.
        @return Always False.
        """
        kind = event.type()
        if kind == QEvent.Type.ShortcutOverride:
            self._repeat = bool(event.isAutoRepeat())
        elif kind == QEvent.Type.KeyRelease:
            if not event.isAutoRepeat():
                self.repeat_ended.emit()
        elif kind in (QEvent.Type.FocusOut, QEvent.Type.Hide):
            self.repeat_ended.emit()
        return False

    def take(self) -> bool:
        """@brief Tell, once, whether the shortcut being handled was triggered by an auto-repeated key.

        @return True when the ShortcutOverride that preceded this activation
            was an automatic repeat; the flag is cleared, so a later
            activation without its own key event never inherits it.
        """
        repeat, self._repeat = self._repeat, False
        return repeat


class DeferredCheckpoint:
    """@brief Hold back the recovery-file write during an auto-repeated key; write once when the repeat ends.

    @note GUARANTEE: holding an arrow (or any key of the view) used to write
        and fsync the recovery file on every repeat. Only the steps of the
        repeat in progress are held back: the first, real press writes as
        before, every repeat updates the editor without writing, and the
        draft is written once when the repeat ends (`KeyRepeatFilter.repeat_ended`:
        real release, focus out, hide) or as soon as any other edit writes it
        (that write holds the whole draft). The deferral is scoped to the
        handler run through `run`, so no other edit is ever deferred.
    """

    def __init__(self, keys: KeyRepeatFilter, write: Callable[[], None]):
        """@brief Bind the checkpoint to the key filter and the host's write.

        @param keys The filter whose `take()` says whether the key repeats and
            whose `repeat_ended` triggers `flush`.
        @param write The host's checkpoint function; it must call `request()`
            and write only when that returns True, which clears `pending`.
        """
        self._keys = keys
        self._write = write
        #: True while a handler run by `run` for an auto-repeated key executes.
        self.deferring = False
        #: True when a write was asked during a deferral and not yet done.
        self.pending = False
        keys.repeat_ended.connect(self.flush)

    def run(self, action: Callable[[], None]) -> None:
        """@brief Run a shortcut handler; a write it requests is deferred when the key auto-repeats.

        @param action The handler (nudge, rotate, undo, paste...).
        """
        self.deferring = self._keys.take()
        try:
            action()
        finally:
            self.deferring = False

    def request(self) -> bool:
        """@brief Ask whether the write may happen now.

        @return True: write now (and `pending` is cleared, the file is about
            to hold the whole draft); False: deferred, `pending` is set.
        """
        if self.deferring:
            self.pending = True
            return False
        self.pending = False
        return True

    def flush(self) -> None:
        """@brief Write now if a deferred write is pending (the repeat ended)."""
        if self.pending:
            self._write()


def install_canvas_filters(view: QObject, keys: KeyRepeatFilter, cancel: CancelOnEscapeAndHide) -> None:
    """@brief Install the two filters on the view in the order the contract requires.

    @param view The graphics view (focus widget, receives Hide).
    @param keys The `KeyRepeatFilter`, installed FIRST.
    @param cancel The `CancelOnEscapeAndHide`, installed SECOND.
    @note Qt runs the event filters of an object in reverse installation
        order: the cancel filter, installed last, sees Hide first and cancels
        the drag; the key filter then emits `repeat_ended`, whose flush writes
        the draft with the drag already rolled back (contract rule 12).
    """
    view.installEventFilter(keys)
    view.installEventFilter(cancel)


def bind_shortcuts(view, bindings: list[tuple[str, Callable[[], None]]], runner, context) -> list[QShortcut]:
    """@brief Create one `QShortcut` per binding on the view, routed through `runner`.

    @param view Parent widget of the shortcuts.
    @param bindings (key sequence text, handler) pairs, e.g. `('Ctrl+Z', self.undo)`.
    @param runner Called as `runner(handler)` on activation (typically
        `DeferredCheckpoint.run`); None calls the handler directly.
    @param context `Qt.ShortcutContext` of every shortcut.
    @return The shortcuts, in the order of `bindings`, parented to the view.
    """
    shortcuts = []
    for sequence, handler in bindings:
        shortcut = QShortcut(QKeySequence(sequence), view)
        shortcut.setContext(context)
        if runner is None:
            shortcut.activated.connect(lambda h=handler: h())
        else:
            shortcut.activated.connect(lambda h=handler: runner(h))
        shortcuts.append(shortcut)
    return shortcuts
