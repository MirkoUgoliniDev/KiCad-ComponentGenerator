"""@brief Unit tests of `kicadgen.gui.canvas_gestures` on a bare QGraphicsView and a recording fake host.

Every positive case has a negative twin: the rule under test must distinguish
the two for a structural reason (plan `docs/canvas-gestures-plan.md`, §4).
"""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QApplication, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QWidget

from kicadgen.gui.canvas_gestures import (
    HANDLE_SIZE_PX, HOVER_MARGIN_PX, CancelOnEscapeAndHide, DeferredCheckpoint, DragPlan,
    GestureController, HoverGroups, Identity, KeyRepeatFilter, StepMemo, bind_shortcuts,
    install_canvas_filters)


class RecordingHost:
    """@brief Fake `GestureHost` recording every callback, with a programmable plan and re-entrant hooks."""

    def __init__(self, plan_for=None):
        self.calls = []
        self.plan_for = plan_for or (lambda target, point: DragPlan('stepped'))
        self.on_end = None

    def gesture_begin(self, target, point):
        self.calls.append(('begin', target, QPointF(point)))
        return self.plan_for(target, point)

    def gesture_step(self, target, point):
        self.calls.append(('step', target, QPointF(point)))

    def gesture_end(self, target, last, commit):
        self.calls.append(('end', target, None if last is None else QPointF(last), commit))
        if self.on_end is not None:
            self.on_end()

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def view(qapp):
    scene = QGraphicsScene()
    scene.setSceneRect(-500, -500, 1000, 1000)
    v = QGraphicsView(scene)
    v.resize(400, 300)
    yield v
    scene.clear()
    v.deleteLater()
    qapp.processEvents()


def _controller(view, host=None, *, grab_mouse=True):
    host = host or RecordingHost()
    return GestureController(view, host, grab_mouse=grab_mouse), host


def _rect_item(scene, x, y):
    item = QGraphicsRectItem(0, 0, 10, 10)
    item.setPos(x, y)
    scene.addItem(item)
    return item


# --------------------------------------------------------------------------- state machine


def test_press_move_release_runs_one_drag_with_one_end(view):
    ctrl, host = _controller(view)
    signals = []
    ctrl.started.connect(lambda t, p: signals.append(('started', t, QPointF(p))))
    ctrl.moved.connect(lambda t, p: signals.append(('moved', t, QPointF(p))))
    ctrl.finished.connect(lambda c: signals.append(('finished', c)))

    assert ctrl.target is None and ctrl.plan is None and ctrl.last_point is None and ctrl.start_point is None
    assert ctrl.press(['pin', '3'], QPointF(1, 2)) is True
    assert ctrl.target == ('pin', '3')          # stored as a tuple
    assert ctrl.start_point == QPointF(1, 2) and ctrl.last_point is None
    assert ctrl.memo.running
    assert ctrl.move(QPointF(5, 6)) is True
    assert ctrl.last_point == QPointF(5, 6)
    assert ctrl.release(QPointF(9, 9)) is True
    assert ctrl.target is None and ctrl.plan is None and ctrl.last_point is None and ctrl.start_point is None
    assert not ctrl.memo.running

    assert host.of('begin') == [('begin', ('pin', '3'), QPointF(1, 2))]
    # The release passed through a move: two steps, the second at the drop point.
    assert host.of('step') == [('step', ('pin', '3'), QPointF(5, 6)), ('step', ('pin', '3'), QPointF(9, 9))]
    assert host.of('end') == [('end', ('pin', '3'), QPointF(9, 9), True)]
    assert signals == [('started', ('pin', '3'), QPointF(1, 2)), ('moved', ('pin', '3'), QPointF(5, 6)),
                       ('moved', ('pin', '3'), QPointF(9, 9)), ('finished', True)]


def test_move_release_and_cancel_while_idle_do_nothing(view):
    ctrl, host = _controller(view)
    fired = []
    ctrl.moved.connect(lambda *a: fired.append(a))
    ctrl.finished.connect(lambda *a: fired.append(a))
    assert ctrl.move(QPointF(1, 1)) is False
    assert ctrl.release(QPointF(1, 1)) is False
    assert ctrl.cancel() is False
    assert host.calls == [] and fired == []


def test_press_is_refused_while_a_drag_runs(view):
    ctrl, host = _controller(view)
    assert ctrl.press(('pin', '1'), QPointF(0, 0))
    assert ctrl.press(('pin', '2'), QPointF(3, 3)) is False
    assert ctrl.target == ('pin', '1')
    assert len(host.of('begin')) == 1
    ctrl.release(QPointF(4, 4))
    # Idle again: the next press is accepted.
    assert ctrl.press(('pin', '2'), QPointF(3, 3)) is True
    assert ctrl.target == ('pin', '2')


def test_refused_begin_leaves_no_capture_and_no_signals(view):
    host = RecordingHost(plan_for=lambda t, p: None)
    ctrl, _ = _controller(view, host)
    started = []
    ctrl.started.connect(lambda *a: started.append(a))
    view.show()
    assert ctrl.press(('pin', '1'), QPointF(0, 0)) is False
    assert ctrl.target is None and started == []
    assert QWidget.mouseGrabber() is None     # grab happens only after an accepted begin
    assert not ctrl.memo.running
    assert ctrl.release(QPointF(1, 1)) is False and host.of('end') == []
    # Twin: the same view, an accepting host: the grab follows the begin.
    host.plan_for = lambda t, p: DragPlan('stepped', cursor=Qt.CursorShape.SizeAllCursor)
    assert ctrl.press(('pin', '1'), QPointF(0, 0)) is True
    assert QWidget.mouseGrabber() is view.viewport()
    assert len(started) == 1
    ctrl.cancel()
    assert QWidget.mouseGrabber() is None


def test_grab_mouse_false_never_grabs(view):
    ctrl, _ = _controller(view, grab_mouse=False)
    view.show()
    assert ctrl.press(('text', 'Reference'), QPointF(0, 0))
    assert QWidget.mouseGrabber() is None
    ctrl.release(QPointF(1, 1))


def test_cancel_ends_once_with_commit_false_and_never_moves_items(view):
    scene = view.scene()
    item = _rect_item(scene, 10, 10)
    host = RecordingHost(plan_for=lambda t, p: DragPlan('live', [(item, item.pos())]))
    ctrl, _ = _controller(view, host)
    finished = []
    ctrl.finished.connect(finished.append)
    ctrl.press(('text', 'Reference'), QPointF(0, 0))
    ctrl.move(QPointF(7, 3))
    assert item.pos() == QPointF(17, 13)
    assert ctrl.cancel() is True
    assert host.of('end') == [('end', ('text', 'Reference'), QPointF(7, 3), False)]
    assert finished == [False]
    assert item.pos() == QPointF(17, 13)        # the picture is the host's business (D5)
    assert ctrl.cancel() is False and len(host.of('end')) == 1   # exactly one end per drag


def test_end_reports_none_as_last_point_when_the_pointer_never_moved(view):
    ctrl, host = _controller(view)
    ctrl.press(('pin', '1'), QPointF(2, 2))
    ctrl.cancel()
    assert host.of('end') == [('end', ('pin', '1'), None, False)]
    # Twin: a release always carries the drop point, even without a prior move.
    ctrl.press(('pin', '1'), QPointF(2, 2))
    ctrl.release(QPointF(2, 2))
    assert host.of('end')[-1] == ('end', ('pin', '1'), QPointF(2, 2), True)
    assert host.of('step') == [('step', ('pin', '1'), QPointF(2, 2))]


def test_finish_clears_the_state_before_calling_the_host(view):
    ctrl, host = _controller(view)
    seen = {}

    def on_end():
        seen['target'] = ctrl.target
        seen['plan'] = ctrl.plan
        seen['last'] = ctrl.last_point
        seen['start'] = ctrl.start_point
        seen['memo'] = ctrl.memo.running
        seen['grabber'] = QWidget.mouseGrabber()
    host.on_end = on_end
    view.show()
    ctrl.press(('pin', '1'), QPointF(0, 0))
    ctrl.move(QPointF(1, 1))
    ctrl.release(QPointF(2, 2))
    assert seen == {'target': None, 'plan': None, 'last': None, 'start': None, 'memo': False, 'grabber': None}


def test_host_may_cancel_or_press_again_from_inside_gesture_end(view):
    ctrl, host = _controller(view)
    outcomes = []

    def on_end():
        host.on_end = None                     # the nested drag ends normally
        outcomes.append(ctrl.cancel())         # idle: nothing to cancel
        outcomes.append(ctrl.press(('pin', '2'), QPointF(5, 5)))
    host.on_end = on_end
    finished = []
    ctrl.finished.connect(finished.append)
    ctrl.press(('pin', '1'), QPointF(0, 0))
    ctrl.release(QPointF(1, 1))
    assert outcomes == [False, True]
    assert ctrl.target == ('pin', '2')          # the nested drag survives the outer finish
    assert finished == [True]
    ctrl.release(QPointF(6, 6))
    ends = host.of('end')
    assert [e[1] for e in ends] == [('pin', '1'), ('pin', '2')]
    assert finished == [True, True]


def test_host_cancelling_from_inside_a_step_ends_the_drag_once(view):
    ctrl, host = _controller(view)
    original_step = host.gesture_step

    def cancelling_step(target, point):
        original_step(target, point)
        ctrl.cancel()                           # the footprint's drop may redraw and cancel
    host.gesture_step = cancelling_step
    ctrl.press(('handle', 0, 1), QPointF(0, 0))
    assert ctrl.release(QPointF(3, 3)) is False  # the drag was already over when the drop came
    assert host.of('end') == [('end', ('handle', 0, 1), QPointF(3, 3), False)]
    assert ctrl.target is None


# --------------------------------------------------------------------------- live policy


def test_live_plan_moves_every_item_by_the_pointer_delta(view):
    scene = view.scene()
    text = _rect_item(scene, 10, 20)
    square = _rect_item(scene, 30, 40)
    host = RecordingHost(plan_for=lambda t, p: DragPlan('live', [(text, text.pos()), (square, square.pos())],
                                                        cursor=Qt.CursorShape.SizeAllCursor))
    ctrl, _ = _controller(view, host)
    ctrl.press(('property', 'Reference'), QPointF(100, 100))
    assert ctrl.plan.policy == 'live' and ctrl.plan.cursor == Qt.CursorShape.SizeAllCursor
    assert len(ctrl.live_items) == 2
    ctrl.move(QPointF(103, 95))
    assert text.pos() == QPointF(13, 15) and square.pos() == QPointF(33, 35)
    ctrl.move(QPointF(90, 110))                 # from the origins, not cumulative
    assert text.pos() == QPointF(0, 30) and square.pos() == QPointF(20, 50)
    assert host.of('step') == []                # live: the host sees no steps
    ctrl.release(QPointF(92, 112))
    assert text.pos() == QPointF(2, 32)
    assert host.of('end') == [('end', ('property', 'Reference'), QPointF(92, 112), True)]
    assert ctrl.live_items == []


def test_stepped_plan_leaves_items_where_they_are(view):
    scene = view.scene()
    item = _rect_item(scene, 10, 20)
    ctrl, host = _controller(view)              # stepped
    ctrl.press(('pin', '1'), QPointF(100, 100))
    ctrl.move(QPointF(103, 95))
    assert item.pos() == QPointF(10, 20)
    assert host.of('step') == [('step', ('pin', '1'), QPointF(103, 95))]
    ctrl.release(QPointF(103, 95))


def test_drag_plan_rejects_an_unknown_policy():
    with pytest.raises(ValueError):
        DragPlan('smooth')
    assert DragPlan('live').live_items == [] and DragPlan('stepped').cursor is None


def test_rebind_re_applies_the_last_delta_to_the_new_items(view):
    scene = view.scene()
    old = _rect_item(scene, 10, 10)
    host = RecordingHost(plan_for=lambda t, p: DragPlan('live', [(old, old.pos())]))
    ctrl, _ = _controller(view, host)
    ctrl.press(('property', 'Value'), QPointF(0, 0))
    ctrl.move(QPointF(4, 6))
    assert old.pos() == QPointF(14, 16)
    new = _rect_item(scene, 10, 10)             # redrawn where the drag began
    ctrl.rebind_live_items([new])
    assert new.pos() == QPointF(14, 16)         # resumed at the last pointer position
    assert [item for item, _o in ctrl.live_items] == [new]
    ctrl.move(QPointF(5, 5))
    assert new.pos() == QPointF(15, 15) and old.pos() == QPointF(14, 16)   # the old item is no longer driven


def test_rebind_before_any_move_only_replaces_the_items(view):
    scene = view.scene()
    old = _rect_item(scene, 10, 10)
    host = RecordingHost(plan_for=lambda t, p: DragPlan('live', [(old, old.pos())]))
    ctrl, _ = _controller(view, host)
    ctrl.press(('property', 'Value'), QPointF(0, 0))
    new = _rect_item(scene, 10, 10)
    ctrl.rebind_live_items([new])
    assert new.pos() == QPointF(10, 10)
    ctrl.cancel()


def test_rebind_while_idle_or_stepped_is_a_no_op(view):
    scene = view.scene()
    item = _rect_item(scene, 10, 10)
    ctrl, host = _controller(view)
    ctrl.rebind_live_items([item])
    assert ctrl.live_items == []
    ctrl.press(('pin', '1'), QPointF(0, 0))
    ctrl.move(QPointF(3, 3))
    ctrl.rebind_live_items([item])
    assert ctrl.live_items == [] and item.pos() == QPointF(10, 10)
    ctrl.release(QPointF(3, 3))


def test_deleted_live_item_is_skipped_without_raising(view):
    scene = view.scene()
    gone = _rect_item(scene, 0, 0)
    kept = _rect_item(scene, 50, 50)
    host = RecordingHost(plan_for=lambda t, p: DragPlan('live', [(gone, gone.pos()), (kept, kept.pos())]))
    ctrl, _ = _controller(view, host)
    ctrl.press(('property', 'Value'), QPointF(0, 0))
    scene.removeItem(gone)
    sip.delete(gone)
    assert sip.isdeleted(gone)
    assert ctrl.move(QPointF(2, 3)) is True     # no RuntimeError
    assert kept.pos() == QPointF(52, 53)
    ctrl.rebind_live_items([gone, kept])        # a dead wrapper is not re-bound either
    assert [item for item, _o in ctrl.live_items] == [kept]
    assert ctrl.release(QPointF(2, 3)) is True
    assert host.of('end')[-1][3] is True


def test_forget_items_keeps_the_drag_running(view):
    scene = view.scene()
    item = _rect_item(scene, 10, 10)
    host = RecordingHost(plan_for=lambda t, p: DragPlan('live', [(item, item.pos())]))
    ctrl, _ = _controller(view, host)
    ctrl.press(('property', 'Value'), QPointF(0, 0))
    ctrl.move(QPointF(1, 1))
    ctrl.forget_items()
    assert ctrl.live_items == [] and ctrl.plan.live_items == []
    assert ctrl.target == ('property', 'Value') and ctrl.last_point == QPointF(1, 1)
    scene.clear()                               # as the host does right after
    assert ctrl.move(QPointF(2, 2)) is True     # nothing to move, nothing raised
    assert ctrl.release(QPointF(3, 3)) is True
    assert host.of('end') == [('end', ('property', 'Value'), QPointF(3, 3), True)]
    # Twin: cancel also still reaches the host after a forget.
    again = _rect_item(scene, 10, 10)
    host.plan_for = lambda t, p: DragPlan('live', [(again, again.pos())])
    ctrl.press(('property', 'Value'), QPointF(0, 0))
    ctrl.forget_items()
    assert ctrl.cancel() is True and host.of('end')[-1][3] is False


def test_forget_items_while_idle_is_harmless(view):
    ctrl, host = _controller(view)
    ctrl.forget_items()
    assert ctrl.target is None and host.calls == []


# --------------------------------------------------------------------------- cursor and capture


def test_plan_cursor_is_set_on_press_and_unset_on_finish(view):
    host = RecordingHost(plan_for=lambda t, p: DragPlan('stepped', cursor=Qt.CursorShape.SizeAllCursor))
    ctrl, _ = _controller(view, host)
    ctrl.press(('handle', 0, 0), QPointF(0, 0))
    assert view.viewport().cursor().shape() == Qt.CursorShape.SizeAllCursor
    ctrl.release(QPointF(1, 1))
    assert view.viewport().cursor().shape() != Qt.CursorShape.SizeAllCursor
    # Twin: a plan without a cursor leaves the viewport's cursor alone.
    view.viewport().setCursor(Qt.CursorShape.CrossCursor)
    host.plan_for = lambda t, p: DragPlan('stepped')
    ctrl.press(('handle', 0, 0), QPointF(0, 0))
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    ctrl.cancel()


# --------------------------------------------------------------------------- memo


def test_memo_repeats_only_within_a_running_drag():
    memo = StepMemo()
    memo.remember('accept', [1, 2])             # stopped: stores nothing
    memo.start()
    assert memo.repeats('accept', [1, 2]) is False
    memo.remember('accept', [1, 2])
    assert memo.repeats('accept', [1, 2]) is True
    assert memo.repeats('restore', [1, 2]) is False     # another path
    assert memo.repeats('accept', [1, 3]) is False      # another key
    memo.stop()
    assert memo.repeats('accept', [1, 2]) is False
    memo.start()                                # a new drag forgets the previous step
    assert memo.repeats('accept', [1, 2]) is False


def test_memo_token_compares_by_value_and_identity_by_object():
    memo = StepMemo()
    memo.start()
    draft = {'rows': [1]}
    memo.remember('accept', [1], token=(Identity(draft), 'status'))
    assert memo.repeats('accept', [1], token=(Identity(draft), 'status')) is True
    assert memo.repeats('accept', [1], token=(Identity(dict(draft)), 'status')) is False  # equal, not the same
    assert memo.repeats('accept', [1], token=(Identity(draft), 'other')) is False
    assert memo.repeats('accept', [1]) is False  # token () differs
    assert Identity(draft) == Identity(draft) and Identity(draft) != Identity({'rows': [1]})
    assert Identity(draft) != draft


def test_controller_starts_and_stops_its_memo_with_the_drag(view):
    ctrl, host = _controller(view)
    assert ctrl.memo.repeats('accept', []) is False
    ctrl.press(('pin', '1'), QPointF(0, 0))
    ctrl.memo.remember('accept', [('1', 0.0)])
    assert ctrl.memo.repeats('accept', [('1', 0.0)]) is True
    ctrl.release(QPointF(0, 0))
    assert ctrl.memo.repeats('accept', [('1', 0.0)]) is False
    # Twin: a refused press leaves the memo stopped too.
    host.plan_for = lambda t, p: None
    ctrl.press(('pin', '1'), QPointF(0, 0))
    assert not ctrl.memo.running


# --------------------------------------------------------------------------- hover


def _hover(view, scale=1.0):
    view.resetTransform()
    view.scale(scale, scale)
    hover = HoverGroups(view)
    a = QGraphicsRectItem(0, 0, HANDLE_SIZE_PX, HANDLE_SIZE_PX)
    b = QGraphicsRectItem(0, 0, HANDLE_SIZE_PX, HANDLE_SIZE_PX)
    view.scene().addItem(a)
    view.scene().addItem(b)
    hover.add([a], QRectF(0, 0, 100, 100), False, ('graphic', 0))
    hover.add([b], QRectF(200, 0, 100, 100), False, ('property', 'Reference'))
    return hover, a, b


@pytest.mark.parametrize('scale', [1.0, 4.0])
def test_hover_reveals_inside_the_screen_margin_at_any_zoom(view, scale):
    hover, a, b = _hover(view, scale)
    m = HOVER_MARGIN_PX / scale                 # scene units of 12 screen pixels
    assert hover.scene_margin() == m
    assert hover.revealed(QPointF(-m + 0.01, 50), None) == {('graphic', 0)}
    assert hover.revealed(QPointF(-m - 0.01, 50), None) == set()
    assert hover.revealed(QPointF(100 + m - 0.01, 50), None) == {('graphic', 0)}
    assert hover.revealed(QPointF(100 + m + 0.01, 50), None) == set()
    assert hover.revealed(QPointF(250, 50), None) == {('property', 'Reference')}
    assert hover.revealed(QPointF(150, 50), None) == set()
    assert hover.revealed(None, None) == set()


def test_hover_margin_is_twelve_pixels_at_four_times_zoom_not_twelve_units(view):
    hover, _a, _b = _hover(view, 4.0)
    assert hover.revealed(QPointF(-6, 50), None) == set()       # 24 screen px away: hidden
    assert hover.revealed(QPointF(-2, 50), None) == {('graphic', 0)}   # 8 screen px away: shown


def test_ring_group_hides_inside_the_inner_rect(view):
    hover = HoverGroups(view)
    hover.add([], QRectF(0, 0, 100, 100), True, ('body',))
    hover.add([], QRectF(0, 0, 100, 100), False, ('graphic', 1))
    m = HOVER_MARGIN_PX
    assert hover.revealed(QPointF(50, 50), None) == {('graphic', 1)}             # deep inside: ring hidden
    assert hover.revealed(QPointF(m - 0.01, 50), None) == {('body',), ('graphic', 1)}   # on the band
    assert hover.revealed(QPointF(m + 0.01, 50), None) == {('graphic', 1)}
    assert hover.revealed(QPointF(-m + 0.01, 50), None) == {('body',), ('graphic', 1)}  # outer band
    assert hover.revealed(QPointF(-m - 0.01, 50), None) == set()


def test_dragged_owner_is_revealed_wherever_the_pointer_is(view):
    hover, a, b = _hover(view)
    assert hover.revealed(QPointF(1000, 1000), ('graphic', 0)) == {('graphic', 0)}
    assert hover.revealed(None, ('graphic', 0)) == {('graphic', 0)}
    # The other groups still follow the pointer rule.
    assert hover.revealed(QPointF(250, 50), ('graphic', 0)) == {('graphic', 0), ('property', 'Reference')}
    # Twin: an owner nobody registered reveals nothing extra; an unrelated drag hides the rest.
    assert hover.revealed(None, ('vertex', 9)) == set()
    assert hover.revealed(QPointF(1000, 1000), ('property', 'Reference')) == {('property', 'Reference')}


def test_apply_sets_visibility_and_honours_a_custom_setter(view):
    hover, a, b = _hover(view)
    assert hover.apply(QPointF(50, 50), None) == {('graphic', 0)}
    assert a.isVisible() and not b.isVisible()
    assert hover.apply(None, None) == set()
    assert not a.isVisible() and not b.isVisible()
    calls = []
    assert hover.apply(QPointF(250, 50), None, setter=lambda item, visible: calls.append((item, visible))) == {
        ('property', 'Reference')}
    assert calls == [(a, False), (b, True)]
    assert not a.isVisible() and not b.isVisible()      # the setter replaced setVisible entirely


def test_apply_skips_deleted_items_and_clear_forgets_them(view):
    hover, a, b = _hover(view)
    view.scene().removeItem(a)
    sip.delete(a)
    assert hover.apply(QPointF(50, 50), None) == {('graphic', 0)}   # no RuntimeError
    assert len(hover.groups) == 2
    hover.clear()
    assert hover.groups == []
    assert hover.apply(QPointF(50, 50), None) == set()


# --------------------------------------------------------------------------- key repeat filter


def _key(view, kind, key=Qt.Key.Key_Left, repeat=False):
    QApplication.sendEvent(view, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier, '', repeat))


def test_key_filter_takes_the_auto_repeat_flag_once(view):
    keys = KeyRepeatFilter(view)
    view.installEventFilter(keys)
    assert keys.take() is False
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    assert keys.take() is True
    assert keys.take() is False                 # cleared: a later activation never inherits it
    _key(view, QEvent.Type.ShortcutOverride, repeat=False)
    assert keys.take() is False
    # Qt itself sends ShortcutOverride ahead of every KeyPress it delivers,
    # so a repeated press is recorded; a release is not an override.
    _key(view, QEvent.Type.KeyPress, repeat=True)
    assert keys.take() is True
    _key(view, QEvent.Type.KeyRelease, repeat=True)
    assert keys.take() is False


def test_key_filter_reports_the_end_of_a_repeat(view):
    keys = KeyRepeatFilter(view)
    view.installEventFilter(keys)
    ended = []
    keys.repeat_ended.connect(lambda: ended.append(1))
    _key(view, QEvent.Type.KeyRelease, repeat=True)      # one of the auto-repeat pairs: not an end
    assert ended == []
    _key(view, QEvent.Type.KeyRelease, repeat=False)
    assert len(ended) == 1
    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
    assert len(ended) == 2
    view.show()
    view.hide()
    assert len(ended) == 3
    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusIn))
    _key(view, QEvent.Type.KeyPress)
    assert len(ended) == 3                      # neither a focus in nor a press ends a repeat


# --------------------------------------------------------------------------- deferred checkpoint


class _Writer:
    """A host checkpoint: writes only when `request()` allows it."""

    def __init__(self, checkpoint_holder):
        self.holder = checkpoint_holder
        self.writes = 0

    def __call__(self):
        if not self.holder['cp'].request():
            return
        self.writes += 1


def _checkpoint(view):
    keys = KeyRepeatFilter(view)
    view.installEventFilter(keys)
    holder = {}
    writer = _Writer(holder)
    cp = DeferredCheckpoint(keys, writer)
    holder['cp'] = cp
    return keys, cp, writer


def test_checkpoint_writes_now_outside_a_repeat_and_defers_during_one(view):
    keys, cp, writer = _checkpoint(view)
    assert cp.request() is True and cp.pending is False
    writer()
    assert writer.writes == 1
    # A real press: the handler's write happens at once.
    _key(view, QEvent.Type.ShortcutOverride, repeat=False)
    cp.run(writer)
    assert writer.writes == 2 and cp.pending is False and cp.deferring is False
    # A repeat: the handler's write is held back.
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    cp.run(writer)
    assert writer.writes == 2 and cp.pending is True and cp.deferring is False
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    cp.run(writer)
    assert writer.writes == 2 and cp.pending is True
    # The repeat ends: one write for the whole held run.
    _key(view, QEvent.Type.KeyRelease, repeat=False)
    assert writer.writes == 3 and cp.pending is False
    cp.flush()
    assert writer.writes == 3                   # nothing pending: nothing written


def test_run_restores_deferring_even_when_the_handler_raises(view):
    keys, cp, writer = _checkpoint(view)
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)

    def failing():
        assert cp.deferring is True
        raise RuntimeError('handler')
    with pytest.raises(RuntimeError):
        cp.run(failing)
    assert cp.deferring is False
    assert cp.request() is True


def test_another_write_during_a_repeat_clears_the_pending_flag(view):
    keys, cp, writer = _checkpoint(view)
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    cp.run(writer)
    assert cp.pending is True
    writer()                                    # any other edit writes the whole draft
    assert writer.writes == 1 and cp.pending is False
    _key(view, QEvent.Type.KeyRelease, repeat=False)
    assert writer.writes == 1                   # nothing left to flush


def test_a_write_that_never_requests_keeps_the_flush_pending(view):
    keys, cp, _writer = _checkpoint(view)
    guarded = []
    cp._write = lambda: guarded.append(1)       # a host guard returned before `request()`
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    cp.run(lambda: cp.request())
    cp.flush()
    assert guarded == [1] and cp.pending is True   # still pending: the next flush tries again


# --------------------------------------------------------------------------- cancel filter and order


def test_escape_cancels_a_running_drag_and_is_consumed(view):
    ctrl, host = _controller(view)
    cancel = CancelOnEscapeAndHide(ctrl)
    view.installEventFilter(cancel)
    view.show()
    ctrl.press(('pin', '1'), QPointF(0, 0))
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view, event)
    assert ctrl.target is None
    assert host.of('end') == [('end', ('pin', '1'), None, False)]
    assert event.isAccepted()
    # Twin: Escape while idle is not consumed and ends nothing; another key never cancels.
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    assert cancel.eventFilter(view, event) is False
    ctrl.press(('pin', '1'), QPointF(0, 0))
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Return)
    assert ctrl.target == ('pin', '1')
    ctrl.cancel()


def test_escape_can_be_kept_off_until_decision_d1(view):
    ctrl, host = _controller(view)
    cancel = CancelOnEscapeAndHide(ctrl, escape=False)
    view.installEventFilter(cancel)
    view.show()
    ctrl.press(('text', 'Reference'), QPointF(0, 0))
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Escape)
    assert ctrl.target == ('text', 'Reference')
    view.hide()                                 # Hide still cancels
    assert ctrl.target is None and host.of('end')[-1][3] is False
    cancel.escape = True                        # D1 applied: Escape works from now on
    view.show()
    ctrl.press(('text', 'Reference'), QPointF(0, 0))
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Escape)
    assert ctrl.target is None


def test_hide_cancels_the_drag_before_the_repeat_flush(view):
    order = []
    ctrl, host = _controller(view)
    host.on_end = lambda: order.append('cancel')
    keys = KeyRepeatFilter(view)
    holder = {}
    cp = DeferredCheckpoint(keys, lambda: order.append('write') or holder['cp'].request())
    holder['cp'] = cp
    install_canvas_filters(view, keys, CancelOnEscapeAndHide(ctrl))
    view.show()
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    cp.run(lambda: cp.request())
    ctrl.press(('pin', '1'), QPointF(0, 0))
    view.hide()
    assert order == ['cancel', 'write']
    assert ctrl.target is None and cp.pending is False


def test_filters_in_the_opposite_order_would_write_the_draft_with_the_drag_still_running(view):
    # Pins the reason for rule 12: Qt runs the filters in reverse installation order.
    order = []
    ctrl, host = _controller(view)
    host.on_end = lambda: order.append('cancel')
    keys = KeyRepeatFilter(view)
    holder = {}
    cp = DeferredCheckpoint(keys, lambda: order.append(('write', ctrl.target)) or holder['cp'].request())
    holder['cp'] = cp
    view.installEventFilter(CancelOnEscapeAndHide(ctrl))   # wrong: cancel first...
    view.installEventFilter(keys)                           # ...keys second
    view.show()
    _key(view, QEvent.Type.ShortcutOverride, repeat=True)
    cp.run(lambda: cp.request())
    ctrl.press(('pin', '1'), QPointF(0, 0))
    view.hide()
    assert order == [('write', ('pin', '1')), 'cancel']


def test_cancel_filter_lets_hide_reach_the_view(view):
    ctrl, _ = _controller(view)
    view.installEventFilter(CancelOnEscapeAndHide(ctrl))
    view.show()
    assert view.isVisible()
    view.hide()
    assert not view.isVisible()


# --------------------------------------------------------------------------- shortcuts


def test_bind_shortcuts_routes_activations_through_the_runner(qapp, view):
    ran = []
    runner_calls = []

    def runner(handler):
        runner_calls.append(handler)
        handler()
    shortcuts = bind_shortcuts(view, [('Left', lambda: ran.append('left')), ('Ctrl+Z', lambda: ran.append('undo'))],
                               runner, Qt.ShortcutContext.WidgetShortcut)
    assert len(shortcuts) == 2 and all(s.parent() is view for s in shortcuts)
    assert [s.context() for s in shortcuts] == [Qt.ShortcutContext.WidgetShortcut] * 2
    view.show()
    view.activateWindow()
    view.setFocus()
    qapp.processEvents()
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Left)
    QApplication.sendEvent(view, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert ran == ['left', 'undo']
    assert len(runner_calls) == 2
    # Twin: an unbound key activates nothing.
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Right)
    assert ran == ['left', 'undo']


def test_bind_shortcuts_without_a_runner_calls_the_handler_directly(qapp, view):
    ran = []
    bind_shortcuts(view, [('Delete', lambda: ran.append('delete'))], None, Qt.ShortcutContext.WidgetShortcut)
    view.show()
    view.activateWindow()
    view.setFocus()
    qapp.processEvents()
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Delete)
    assert ran == ['delete']


def test_shortcut_through_the_checkpoint_runner_sees_the_repeat_flag(qapp, view):
    keys, cp, writer = _checkpoint(view)
    bind_shortcuts(view, [('Left', writer)], cp.run, Qt.ShortcutContext.WidgetShortcut)
    view.show()
    view.activateWindow()
    view.setFocus()
    qapp.processEvents()
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Left, repeat=False)   # Qt sends ShortcutOverride first
    assert writer.writes == 1
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Left, repeat=True)
    _key(view, QEvent.Type.KeyPress, key=Qt.Key.Key_Left, repeat=True)
    assert writer.writes == 1 and cp.pending
    _key(view, QEvent.Type.KeyRelease, key=Qt.Key.Key_Left, repeat=False)
    assert writer.writes == 2 and not cp.pending
