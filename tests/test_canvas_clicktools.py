"""@brief Unit tests of `kicadgen.gui.canvas_clicktools` on a bare QGraphicsView (offscreen).

A real `Camera` and a recording fake host drive the collector and the router.
Every rule has a positive and a negative twin.
"""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView

from kicadgen.gui.canvas_camera import Camera
from kicadgen.gui.canvas_clicktools import (REFUSED_COINCIDENT, ClickToolRouter, PointCollector,
                                            ToolSpec)

LEFT, RIGHT, MIDDLE = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton
NOMOD, SHIFT = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier

LINE = ToolSpec('line')
RECT = ToolSpec('rect', count=2)
RECT_P = ToolSpec('rect_p', count=2, persistent=True)
POLY = ToolSpec('poly', close_after=4)
WIRE = ToolSpec('wire', drag_adds_release_point=False)


class Host:
    """Recording host: snaps to whole scene units; `calls` lists every callback."""

    def __init__(self):
        self.calls = []
        self.stay = False
        self.snap_result = None          # fixed list overriding the snap
        self.on_finished = None          # callable(spec, points, commit) run inside tool_finished
        self.on_snap = None
        self.collector = None

    def tool_snap(self, spec, point, free, points):
        self.calls.append(('snap', spec.key, free, tuple(points)))
        if self.on_snap:
            self.on_snap()
        if self.snap_result is not None:
            return list(self.snap_result)
        return [(round(point.x()), round(point.y()))]

    def tool_points_changed(self, spec, points):
        self.calls.append(('points', spec.key, list(points)))

    def tool_preview_changed(self, spec, points, preview):
        self.calls.append(('preview', spec.key, list(points), list(preview)))

    def tool_refused(self, spec, reason):
        self.calls.append(('refused', spec.key, reason))

    def tool_finished(self, spec, points, commit):
        self.calls.append(('finished', spec.key, list(points), commit))
        if self.on_finished:
            self.on_finished(spec, points, commit)
        return self.stay

    def tool_armed(self, spec):
        self.calls.append(('armed', spec.key))

    def tool_disarmed(self, spec):
        self.calls.append(('disarmed', spec.key))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]

    def kinds(self):
        return [c[0] for c in self.calls if c[0] not in ('snap',)]


@pytest.fixture
def view(qapp):
    scene = QGraphicsScene()
    scene.setSceneRect(QRectF(-500, -500, 1000, 1000))
    v = QGraphicsView(scene)
    v.resize(400, 300)
    v.show()
    qapp.processEvents()
    yield v
    v.hide()
    scene.clear()
    v.deleteLater()


@pytest.fixture
def host():
    return Host()


@pytest.fixture
def col(host):
    return PointCollector(host)


@pytest.fixture
def cam(view):
    return Camera(view, limits=(0.15, 20.0))


@pytest.fixture
def router(view, col, cam):
    return ClickToolRouter(view, col, cam)


def sp(view, x, y):
    """Snapped scene point the fake host returns for a viewport position."""
    p = view.mapToScene(QPoint(x, y))
    return (round(p.x()), round(p.y()))


def mouse(kind, x, y, button=LEFT, mods=NOMOD):
    buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    if kind == QEvent.Type.MouseMove:
        button, buttons = Qt.MouseButton.NoButton, Qt.MouseButton.NoButton
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, mods)


def press(x, y, button=LEFT, mods=NOMOD):
    return mouse(QEvent.Type.MouseButtonPress, x, y, button, mods)


def release(x, y, button=LEFT, mods=NOMOD):
    return mouse(QEvent.Type.MouseButtonRelease, x, y, button, mods)


def dbl(x, y, button=LEFT, mods=NOMOD):
    return mouse(QEvent.Type.MouseButtonDblClick, x, y, button, mods)


def move(x, y, mods=NOMOD):
    return mouse(QEvent.Type.MouseMove, x, y, mods=mods)


def keyev(key):
    return QKeyEvent(QEvent.Type.KeyPress, key, NOMOD)


# ---------------------------------------------------------------------------
# PointCollector: state machine
# ---------------------------------------------------------------------------

def test_arm_reports_armed_then_empty_points(col, host):
    assert not col.armed and col.spec is None
    assert col.arm(LINE) is True
    assert col.armed and col.spec == LINE and col.points == []
    assert host.kinds() == ['armed', 'points']
    assert host.named('points')[0][2] == []


def test_arm_rejects_a_non_spec_without_raising(col, host):
    assert col.arm('line') is False
    assert col.arm(None) is False
    assert not col.armed and host.calls == []


def test_click_when_idle_does_nothing(col, host):
    assert col.click(QPointF(1, 1)) is False
    assert host.calls == []


def test_click_appends_and_reports_points(col, host):
    col.arm(LINE)
    host.calls.clear()
    assert col.click(QPointF(1.2, 2.4)) is True
    assert col.points == [(1, 2)]
    assert host.kinds() == ['points']
    assert host.named('points')[0][2] == [(1, 2)]


def test_points_property_is_a_copy(col):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.points.append('x')
    assert col.points == [(1, 1)]


def test_count_auto_finishes_with_the_points(col, host):
    col.arm(RECT)
    col.click(QPointF(1, 1))
    host.calls.clear()
    assert col.click(QPointF(5, 6)) is True
    assert host.named('finished') == [('finished', 'rect', [(1, 1), (5, 6)], True)]
    assert host.kinds() == ['finished', 'disarmed']
    assert not col.armed and col.points == []


def test_below_count_does_not_finish(col, host):
    col.arm(RECT)
    col.click(QPointF(1, 1))
    assert col.armed and host.named('finished') == []


def test_count_zero_never_auto_finishes(col, host):
    col.arm(LINE)
    for i in range(10):
        col.click(QPointF(i * 3, 0))
    assert col.armed and len(col.points) == 10 and host.named('finished') == []


def test_close_after_finishes_on_a_click_equal_to_the_first_point(col, host):
    col.arm(POLY)
    for p in [(0, 0), (10, 0), (10, 10), (0, 10)]:
        col.click(QPointF(*p))
    assert col.armed                         # 4 points, none equal to the first yet
    assert col.click(QPointF(0, 0)) is True
    assert host.named('finished')[0][2] == [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    assert not col.armed


def test_close_after_is_not_triggered_below_its_threshold(col, host):
    col.arm(POLY)
    for p in [(0, 0), (10, 0), (10, 10)]:
        col.click(QPointF(*p))
    col.click(QPointF(0, 0))                 # 4 points but the click is the first: total = 4 >= 4
    assert not col.armed                     # reaches the threshold exactly: closes
    col2 = PointCollector(Host())
    col2.arm(POLY)
    for p in [(0, 0), (10, 0)]:
        col2.click(QPointF(*p))
    col2.click(QPointF(0, 0))                # only 3 points: 3 < 4, stays open
    assert col2.armed and len(col2.points) == 3


def test_close_after_requires_equality_with_the_first_point(col, host):
    col.arm(POLY)
    for p in [(0, 0), (10, 0), (10, 10), (0, 10), (5, 5)]:
        col.click(QPointF(*p))
    assert col.armed and len(col.points) == 5


def test_no_close_rule_means_a_click_on_the_first_point_just_adds(col, host):
    col.arm(LINE)
    for p in [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]:
        col.click(QPointF(*p))
    assert col.armed and len(col.points) == 5


def test_finish_commit_hands_the_points_over(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.click(QPointF(2, 2))
    host.calls.clear()
    assert col.finish() is True
    assert host.kinds() == ['finished', 'disarmed']
    assert host.named('finished')[0] == ('finished', 'line', [(1, 1), (2, 2)], True)
    assert not col.armed


def test_finish_when_idle_is_false_and_silent(col, host):
    assert col.finish() is False and col.cancel() is False
    assert host.calls == []


def test_cancel_reports_commit_false(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.calls.clear()
    assert col.cancel() is True
    assert host.named('finished') == [('finished', 'line', [(1, 1)], False)]
    assert host.named('disarmed') == [('disarmed', 'line')]


def test_disarm_is_a_cancel_without_a_commit(col, host):
    col.arm(LINE)
    col.disarm()
    assert host.named('finished')[0][3] is False and not col.armed


def test_stay_rearms_the_same_spec_silently(col, host):
    host.stay = True
    col.arm(RECT_P)
    col.click(QPointF(1, 1))
    host.calls.clear()
    col.click(QPointF(4, 4))
    assert col.armed and col.spec == RECT_P and col.points == []
    assert host.kinds() == ['finished', 'points']
    assert host.named('points')[0][2] == []
    assert host.named('armed') == [] and host.named('disarmed') == []
    # and it collects the next shape
    col.click(QPointF(7, 7))
    assert col.points == [(7, 7)]


def test_not_stay_disarms(col, host):
    host.stay = False
    col.arm(RECT)
    col.click(QPointF(1, 1))
    col.click(QPointF(4, 4))
    assert not col.armed and host.named('disarmed')


def test_stay_is_ignored_when_the_tool_is_cancelled_through_arm(col, host):
    host.stay = True
    col.arm(LINE)
    col.arm(RECT)
    assert col.spec == RECT
    assert host.named('disarmed') == [('disarmed', 'line')]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

def test_host_returning_nothing_appends_nothing_and_refuses_once(col, host):
    col.arm(LINE)
    host.snap_result = []
    host.calls.clear()
    assert col.click(QPointF(3, 3)) is False
    assert col.points == []
    assert host.named('refused') == [('refused', 'line', REFUSED_COINCIDENT)]
    assert host.named('points') == []


def test_a_point_equal_to_the_last_is_refused(col, host):
    col.arm(LINE)
    col.click(QPointF(3, 3))
    host.calls.clear()
    assert col.click(QPointF(3.1, 2.9)) is False
    assert col.points == [(3, 3)]
    assert host.named('refused') == [('refused', 'line', REFUSED_COINCIDENT)]


def test_a_point_equal_to_an_earlier_one_is_accepted(col, host):
    col.arm(LINE)
    col.click(QPointF(3, 3))
    col.click(QPointF(9, 9))
    assert col.click(QPointF(3, 3)) is True
    assert len(col.points) == 3


def test_quiet_click_suppresses_the_refusal_but_still_refuses(col, host):
    col.arm(LINE)
    col.click(QPointF(3, 3))
    host.calls.clear()
    assert col.click(QPointF(3, 3), quiet=True) is False
    assert host.named('refused') == [] and col.points == [(3, 3)]


def test_duplicate_does_not_finish_a_fixed_count_tool(col, host):
    col.arm(RECT)
    col.click(QPointF(3, 3))
    col.click(QPointF(3, 3))
    assert col.armed and col.points == [(3, 3)]


def test_two_point_append_checks_only_the_first_against_the_last(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.snap_result = [(5, 1), (5, 5)]
    assert col.click(QPointF(5, 5)) is True
    assert col.points == [(1, 1), (5, 1), (5, 5)]
    host.snap_result = [(5, 5), (7, 7)]
    assert col.click(QPointF(7, 7)) is False         # first appended equals the last point
    assert len(col.points) == 3


def test_free_flag_and_points_snapshot_reach_the_snap(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.click(QPointF(4, 4), free=True)
    snaps = host.named('snap')
    assert snaps[0][2:] == (False, ())
    assert snaps[1][2:] == (True, ((1, 1),))


def test_tool_snap_runs_before_any_refusal_or_append(col, host):
    col.arm(LINE)
    host.calls.clear()
    col.click(QPointF(1, 1))
    assert host.calls[0][0] == 'snap' and host.calls[1][0] == 'points'


# ---------------------------------------------------------------------------
# re-entrancy
# ---------------------------------------------------------------------------

def test_collector_is_idle_inside_tool_finished(col, host):
    seen = []
    host.on_finished = lambda s, p, c: seen.append((col.armed, col.points, col.click(QPointF(1, 1)),
                                                    col.finish(), col.cancel()))
    col.arm(LINE)
    col.click(QPointF(2, 2))
    col.finish()
    assert seen == [(False, [], False, False, False)]


def test_rearming_the_same_tool_inside_tool_finished_wins_over_stay(col, host):
    host.stay = True

    def rearm(spec, points, commit):
        host.on_finished = None
        col.arm(spec)

    host.on_finished = rearm
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.calls.clear()
    col.finish()
    assert col.armed and col.spec == LINE and col.points == []
    # armed once more, no stay re-arm (no extra points event after tool_armed's), no disarmed
    assert host.kinds() == ['finished', 'armed', 'points']


def test_arming_another_tool_inside_tool_finished_wins_over_stay_and_disarm(col, host):
    host.stay = True

    def other(spec, points, commit):
        host.on_finished = None
        col.arm(RECT)

    host.on_finished = other
    col.arm(LINE)
    col.finish()
    assert col.armed and col.spec == RECT
    assert host.named('disarmed') == []
    assert host.named('armed') == [('armed', 'line'), ('armed', 'rect')]


def test_arming_another_tool_inside_tool_finished_does_not_leak_old_points(col, host):
    def other(spec, points, commit):
        host.on_finished = None
        col.arm(RECT)

    host.on_finished = other
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.finish()
    assert col.points == []


def test_stay_true_with_nothing_armed_inside_is_honoured(col, host):
    host.stay = True
    col.arm(LINE)
    col.finish()
    assert col.armed and host.named('disarmed') == []


def test_arm_ends_the_first_tool_with_commit_false_then_arms(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.calls.clear()
    assert col.arm(RECT) is True
    assert host.kinds() == ['finished', 'disarmed', 'armed', 'points']
    assert host.named('finished')[0] == ('finished', 'line', [(1, 1)], False)
    assert col.spec == RECT and col.points == []


def test_rearming_the_same_spec_restarts_it(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.arm(LINE)
    assert col.points == [] and host.named('finished')[-1][3] is False


def test_cancel_from_inside_tool_armed_ends_the_tool(col, host):
    orig = host.tool_armed

    def armed_then_cancel(spec):
        orig(spec)
        col.cancel()

    host.tool_armed = armed_then_cancel
    col.arm(LINE)
    assert not col.armed
    assert host.named('finished') == [('finished', 'line', [], False)]
    assert host.named('disarmed') == [('disarmed', 'line')]


def test_cancel_from_inside_tool_points_changed_drops_the_rest(col, host):
    col.arm(LINE)
    orig = host.tool_points_changed

    def points_then_cancel(spec, points):
        orig(spec, points)
        col.cancel()

    host.tool_points_changed = points_then_cancel
    assert col.click(QPointF(1, 1)) is True
    assert not col.armed and host.named('finished')[0][3] is False


def test_arm_from_inside_tool_snap_drops_the_click(col, host):
    col.arm(LINE)

    def swap():
        host.on_snap = None
        col.arm(RECT)

    host.on_snap = swap
    assert col.click(QPointF(1, 1)) is False
    assert col.spec == RECT and col.points == []


def test_cancel_from_inside_tool_snap_during_hover_emits_nothing(col, host):
    col.arm(LINE)
    host.on_snap = col.cancel
    host.calls.clear()
    col.hover(QPointF(1, 1))
    assert host.named('preview') == [] and not col.armed


def test_runaway_host_arming_forever_does_not_hang_or_raise(col, host):
    def always(spec, points, commit):
        col.arm(LINE)

    col.arm(LINE)
    host.on_finished = always
    col.arm(RECT)            # returns (True or False) without raising or looping forever


def test_finish_from_inside_tool_refused_after_a_refusal(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.tool_refused = lambda spec, reason: col.finish(False)
    col.click(QPointF(1, 1))
    assert not col.armed


# ---------------------------------------------------------------------------
# hover / preview
# ---------------------------------------------------------------------------

def test_hover_emits_the_preview(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.calls.clear()
    col.hover(QPointF(5, 5))
    assert host.named('preview') == [('preview', 'line', [(1, 1)], [(5, 5)])]
    assert col.preview == [(5, 5)]


def test_hover_emits_only_on_change(col, host):
    col.arm(LINE)
    col.hover(QPointF(5, 5))
    host.calls.clear()
    col.hover(QPointF(5.2, 5.1))                 # same snapped node
    assert host.named('preview') == []
    col.hover(QPointF(8, 8))
    assert len(host.named('preview')) == 1


def test_hover_none_always_emits_one_clearing_preview_per_call(col, host):
    # Reason: a click resets the stored preview silently, so a leave must still tell the host
    # to clear its ghost / snap dot; clearing is idempotent for hosts.
    col.arm(LINE)
    col.hover(QPointF(5, 5))
    host.calls.clear()
    col.hover(None)
    assert host.named('preview') == [('preview', 'line', [], [])]
    col.hover(None)
    assert len(host.named('preview')) == 2
    assert col.preview == []


def test_hover_none_after_a_click_still_emits(col, host):
    col.arm(LINE)
    col.hover(QPointF(5, 5))
    col.click(QPointF(5, 5))
    host.calls.clear()
    col.hover(None)
    assert host.named('preview') == [('preview', 'line', [(5, 5)], [])]


def test_hover_none_when_empty_still_emits_while_armed(col, host):
    col.arm(LINE)
    host.calls.clear()
    col.hover(None)
    assert len(host.named('preview')) == 1


def test_hover_when_idle_does_nothing(col, host):
    col.hover(QPointF(1, 1))
    col.hover(None)
    assert host.calls == []


def test_hover_passes_free_flag(col, host):
    col.arm(LINE)
    col.hover(QPointF(1, 1), free=True)
    assert host.named('snap')[-1][2] is True


def test_hover_with_an_empty_snap_clears_the_preview(col, host):
    col.arm(LINE)
    col.hover(QPointF(1, 1))
    host.snap_result = []
    host.calls.clear()
    col.hover(QPointF(2, 2))
    assert host.named('preview') == [('preview', 'line', [], [])]
    assert host.named('refused') == []


def test_hover_compares_with_equality_not_identity(col, host):
    col.arm(LINE)
    host.snap_result = [(1, 1)]
    col.hover(QPointF(0, 0))
    host.snap_result = [(1, 1)]                  # a new but equal list
    host.calls.clear()
    col.hover(QPointF(0, 0))
    assert host.named('preview') == []


def test_a_click_resets_the_preview_so_the_next_hover_reports_again(col, host):
    col.arm(LINE)
    col.hover(QPointF(5, 5))
    col.click(QPointF(5, 5))
    assert col.preview == []
    host.calls.clear()
    col.hover(QPointF(5, 5))
    assert len(host.named('preview')) == 1


def test_finish_clears_the_preview_without_emitting(col, host):
    col.arm(LINE)
    col.hover(QPointF(5, 5))
    host.calls.clear()
    col.finish(False)
    assert host.named('preview') == [] and col.preview == []


# ---------------------------------------------------------------------------
# router: precedence
# ---------------------------------------------------------------------------

def test_press_not_armed_is_not_consumed(router, host):
    ev = press(50, 50)
    ev.ignore()
    assert router.press(ev) is False
    assert not ev.isAccepted() and host.calls == []


def test_left_press_armed_is_consumed_and_adds_a_point(router, col, view, host):
    router.arm(LINE)
    ev = press(50, 60)
    ev.ignore()
    assert router.press(ev) is True and ev.isAccepted()
    assert col.points == [sp(view, 50, 60)]


def test_shift_on_the_press_event_frees_the_point(router, col, host):
    router.arm(LINE)
    router.press(press(50, 60, mods=SHIFT))
    router.press(press(80, 90))
    snaps = host.named('snap')
    assert snaps[0][2] is True and snaps[1][2] is False


def test_right_press_finishes_with_commit(router, col, host, view):
    router.arm(LINE)
    router.press(press(50, 60))
    host.calls.clear()
    assert router.press(press(70, 70, button=RIGHT)) is True
    assert host.named('finished') == [('finished', 'line', [sp(view, 50, 60)], True)]
    assert host.named('snap') == []                   # no point added by the right press


def test_right_press_does_not_add_a_point(router, col, host):
    router.arm(LINE)
    router.press(press(70, 70, button=RIGHT))
    assert host.named('finished')[0][2] == []


def test_middle_press_falls_through(router, col, host):
    router.arm(LINE)
    host.calls.clear()
    ev = press(50, 50, button=MIDDLE)
    ev.ignore()
    assert router.press(ev) is False and not ev.isAccepted()
    assert host.calls == [] and col.armed


def test_pan_active_swallows_a_left_press_without_a_point(router, col, cam, host):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    host.calls.clear()
    ev = press(50, 50)
    ev.ignore()
    assert router.press(ev) is True and ev.isAccepted()
    assert col.points == [] and host.calls == []
    cam.pan_cancel()


def test_pan_active_does_not_swallow_a_middle_press(router, cam):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    assert router.press(press(50, 50, button=MIDDLE)) is False
    cam.pan_cancel()


def test_pan_active_swallows_a_right_press_without_finishing(router, col, cam, host):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    assert router.press(press(50, 50, button=RIGHT)) is True
    assert col.armed
    cam.pan_cancel()


def test_after_the_pan_a_left_press_adds_again(router, col, cam):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    router.press(press(50, 50))
    cam.pan_cancel()
    router.press(press(50, 50))
    assert len(col.points) == 1


# ---------------------------------------------------------------------------
# router: release through drag
# ---------------------------------------------------------------------------

def test_release_far_from_the_press_adds_the_release_point(router, col, view):
    router.arm(RECT)
    router.press(press(50, 50))
    far = 50 + QApplication.startDragDistance() + 5
    assert router.release(release(far, 50)) is True
    assert col.armed is False                      # the second point finished the rectangle
    # (committed through the host)


def test_release_far_adds_exactly_the_release_point(router, col, view, host):
    router.arm(LINE)
    router.press(press(50, 50))
    far = 50 + QApplication.startDragDistance() + 5
    router.release(release(far, 50))
    assert col.points == [sp(view, 50, 50), sp(view, far, 50)]


def test_release_at_the_press_adds_nothing(router, col, view):
    router.arm(LINE)
    router.press(press(50, 50))
    assert router.release(release(50, 50)) is True
    assert col.points == [sp(view, 50, 50)]


def test_release_just_below_the_drag_distance_adds_nothing(router, col, view):
    router.arm(LINE)
    router.press(press(50, 50))
    router.release(release(50 + QApplication.startDragDistance() - 1, 50))
    assert len(col.points) == 1


def test_release_at_exactly_the_drag_distance_adds(router, col, view):
    router.arm(LINE)
    router.press(press(50, 50))
    router.release(release(50 + QApplication.startDragDistance(), 50))
    assert len(col.points) == 2


def test_drag_distance_is_manhattan(router, col, view):
    d = QApplication.startDragDistance()
    router.arm(LINE)
    router.press(press(50, 50))
    router.release(release(50 + d // 2 + 1, 50 + d - d // 2))       # |dx|+|dy| = d + 1
    assert len(col.points) == 2


def test_flag_off_release_adds_nothing(router, col):
    router.arm(WIRE)
    router.press(press(50, 50))
    router.release(release(300, 200))
    assert len(col.points) == 1


def test_release_after_a_press_that_finished_a_persistent_tool_adds_nothing(router, col, host):
    host.stay = True
    router.arm(RECT_P)
    router.press(press(50, 50))
    router.press(press(80, 80))                    # second point: finishes, stays armed, empty
    assert col.armed and col.points == []
    router.release(release(300, 200))
    assert col.points == []


def test_release_is_not_consumed_when_idle_and_nothing_was_pressed(router):
    ev = release(50, 50)
    ev.ignore()
    assert router.release(ev) is False and not ev.isAccepted()


def test_release_of_other_buttons_is_not_consumed(router):
    router.arm(LINE)
    assert router.release(release(50, 50, button=MIDDLE)) is False


def test_release_after_the_press_that_auto_finished_is_consumed(router, col, view):
    router.arm(ToolSpec('one', count=1))
    router.press(press(50, 50))
    assert not col.armed
    assert router.release(release(300, 200)) is True
    assert router.release(release(300, 200)) is False      # only once


# ---------------------------------------------------------------------------
# router: double click
# ---------------------------------------------------------------------------

def test_double_click_adds_a_quiet_point_then_finishes(router, col, host, view):
    router.arm(LINE)
    router.press(press(50, 50))
    host.calls.clear()
    assert router.double_click(dbl(90, 90)) is True
    assert host.kinds() == ['points', 'finished', 'disarmed']
    assert host.named('finished')[0][2] == [sp(view, 50, 50), sp(view, 90, 90)]
    assert host.named('finished')[0][3] is True


def test_double_click_on_the_last_vertex_adds_it_once_without_a_toast(router, col, host, view):
    router.arm(LINE)
    router.press(press(50, 50))
    router.press(press(90, 90))
    host.calls.clear()
    router.double_click(dbl(90, 90))
    assert host.named('refused') == []
    assert host.named('finished')[0][2] == [sp(view, 50, 50), sp(view, 90, 90)]


def test_the_release_after_a_double_click_adds_no_point(router, col, host):
    host.stay = True
    router.arm(LINE)
    router.press(press(50, 50))
    router.double_click(dbl(90, 90))
    assert col.armed and col.points == []              # stayed, empty
    assert router.release(release(300, 200)) is True
    assert col.points == []


def test_a_fixed_count_tool_finished_by_a_fast_double_click_finishes_once(router, col, host, view):
    host.stay = True                                     # worst case: stays armed with an empty list
    router.arm(RECT_P)
    router.press(press(50, 50))
    host.calls.clear()
    router.double_click(dbl(90, 90))
    assert len(host.named('finished')) == 1
    assert host.named('finished')[0][2] == [sp(view, 50, 50), sp(view, 90, 90)]


def test_a_fixed_count_tool_not_finished_by_its_double_click_is_finished_by_it(router, col, host):
    router.arm(ToolSpec('three', count=3))
    router.press(press(50, 50))
    router.double_click(dbl(90, 90))
    assert len(host.named('finished')) == 1 and host.named('finished')[0][3] is True


def test_double_click_not_armed_is_not_consumed(router):
    assert router.double_click(dbl(50, 50)) is False


def test_double_click_middle_is_not_consumed(router, col):
    router.arm(LINE)
    assert router.double_click(dbl(50, 50, button=MIDDLE)) is False and col.armed


def test_double_click_during_a_pan_adds_nothing(router, col, cam, host):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    host.calls.clear()
    assert router.double_click(dbl(50, 50)) is True
    assert host.calls == [] and col.armed
    cam.pan_cancel()


def test_double_click_does_not_finish_an_empty_tool_that_refused_its_point(router, col, host):
    router.arm(LINE)
    host.snap_result = []
    host.calls.clear()
    router.double_click(dbl(50, 50))
    assert host.named('finished') == [] and col.armed


# ---------------------------------------------------------------------------
# router: keys
# ---------------------------------------------------------------------------

def test_escape_cancels_the_tool(router, col, host):
    router.arm(LINE)
    router.press(press(50, 50))
    ev = keyev(Qt.Key.Key_Escape)
    ev.ignore()
    assert router.key(ev) is True and ev.isAccepted()
    assert not col.armed and host.named('finished')[-1][3] is False


def test_escape_asks_pan_cancel_first_and_the_tool_survives(router, col, cam, host):
    router.arm(LINE)
    router.press(press(50, 50))
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    host.calls.clear()
    assert router.key(keyev(Qt.Key.Key_Escape)) is True
    assert not cam.pan_active
    assert col.armed and len(col.points) == 1 and host.named('finished') == []
    # the next Escape cancels the tool
    router.key(keyev(Qt.Key.Key_Escape))
    assert not col.armed


def test_escape_restores_the_cross_cursor_after_cancelling_a_pan(router, view, cam):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    router.key(keyev(Qt.Key.Key_Escape))
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_enter_and_return_finish_with_commit(router, col, host, view, key):
    router.arm(LINE)
    router.press(press(50, 50))
    host.calls.clear()
    assert router.key(keyev(key)) is True
    assert host.named('finished') == [('finished', 'line', [sp(view, 50, 50)], True)]


def test_other_keys_are_not_consumed(router, col):
    router.arm(LINE)
    ev = keyev(Qt.Key.Key_A)
    ev.ignore()
    assert router.key(ev) is False and not ev.isAccepted() and col.armed


def test_keys_are_not_consumed_when_idle(router, cam):
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    assert router.key(keyev(Qt.Key.Key_Escape)) is False
    assert cam.pan_active                      # the router leaves the pan to the view
    cam.pan_cancel()
    assert router.key(keyev(Qt.Key.Key_Return)) is False


# ---------------------------------------------------------------------------
# router: move, leave, hide
# ---------------------------------------------------------------------------

def test_move_hovers_without_accepting(router, col, host, view):
    router.arm(LINE)
    host.calls.clear()
    ev = move(60, 70)
    ev.ignore()
    assert router.move(ev) is True and not ev.isAccepted()
    assert host.named('preview')[0][3] == [sp(view, 60, 70)]


def test_move_shift_comes_from_the_event(router, host):
    router.arm(LINE)
    router.move(move(60, 70, mods=SHIFT))
    assert host.named('snap')[-1][2] is True
    router.move(move(90, 70))
    assert host.named('snap')[-1][2] is False


def test_move_not_armed_does_nothing(router, host):
    assert router.move(move(60, 70)) is False and host.calls == []


def test_move_during_a_pan_does_not_hover(router, cam, host):
    router.arm(LINE)
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    host.calls.clear()
    assert router.move(move(60, 70)) is False and host.calls == []
    cam.pan_cancel()


def test_hover_at_reads_the_keyboard_modifiers(router, host, monkeypatch):
    router.arm(LINE)
    monkeypatch.setattr(QApplication, 'keyboardModifiers', staticmethod(lambda: SHIFT))
    router.hover_at(QPointF(3, 3))
    assert host.named('snap')[-1][2] is True


def test_leave_emits_one_clearing_preview_per_call(router, col, host):
    router.arm(LINE)
    router.move(move(60, 70))
    host.calls.clear()
    router.leave()
    assert host.named('preview') == [('preview', 'line', [], [])]
    router.leave()
    assert len(host.named('preview')) == 2


def test_leave_keeps_the_tool_and_points(router, col):
    router.arm(LINE)
    router.press(press(50, 50))
    router.leave()
    assert col.armed and len(col.points) == 1


def test_hide_cancels_the_tool_and_is_idempotent(router, col, host):
    router.arm(LINE)
    router.press(press(50, 50))
    host.calls.clear()
    router.hide()
    router.hide()
    assert not col.armed
    assert host.named('finished')[0][3] is False
    assert len(host.named('finished')) == 1 and len(host.named('disarmed')) == 1


def test_hide_when_idle_does_nothing(router, host):
    router.hide()
    assert host.calls == []


# ---------------------------------------------------------------------------
# router: arm, disarm, cursor
# ---------------------------------------------------------------------------

def test_arm_sets_focus_and_the_cross_cursor_on_the_viewport(router, view, col):
    view.clearFocus()
    assert router.arm(LINE) is True
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    assert col.armed


def test_disarm_unsets_the_cursor(router, view, col):
    router.arm(LINE)
    router.disarm()
    assert view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor
    assert not col.armed


def test_cursor_is_not_touched_when_never_armed(router, view):
    view.viewport().setCursor(Qt.CursorShape.WaitCursor)
    router.hide()
    router.disarm()
    assert view.viewport().cursor().shape() == Qt.CursorShape.WaitCursor


def test_hide_unsets_the_cursor(router, view):
    router.arm(LINE)
    router.hide()
    assert view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor


def test_the_cursor_is_unset_when_the_tool_finishes_by_itself(router, view, col):
    router.arm(RECT)
    router.press(press(50, 50))
    router.press(press(90, 90))
    assert not col.armed
    assert view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor


def test_the_cursor_stays_when_the_tool_stays_armed(router, view, host):
    host.stay = True
    router.arm(RECT_P)
    router.press(press(50, 50))
    router.press(press(90, 90))
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor


def test_arm_cancels_a_pan_in_progress(router, cam):
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    router.arm(LINE)
    assert not cam.pan_active


def test_arm_keeps_the_cross_cursor_after_the_pan_reset_it(router, view, cam):
    cam.pan_begin(MIDDLE, QPoint(10, 10), grab_mouse=False)
    router.arm(LINE)
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor


def test_arm_runs_the_on_arm_hook_before_the_tool_is_armed(view, col, cam):
    order = []
    r = ClickToolRouter(view, col, cam, on_arm=lambda: order.append(('hook', col.armed)))
    r.arm(LINE)
    assert order == [('hook', False)] and col.armed


def test_arm_with_an_invalid_spec_does_nothing(view, col, cam):
    called = []
    r = ClickToolRouter(view, col, cam, on_arm=lambda: called.append(1))
    assert r.arm('wire') is False and called == [] and not col.armed


def test_arming_a_second_tool_cancels_the_first(router, col, host):
    router.arm(LINE)
    router.press(press(50, 50))
    host.calls.clear()
    router.arm(RECT)
    assert host.kinds()[:2] == ['finished', 'disarmed']
    assert host.named('finished')[0][3] is False
    assert col.spec == RECT


def test_a_press_after_arm_does_not_see_a_stale_release_from_before(router, col):
    router.arm(LINE)
    router.press(press(50, 50))
    router.arm(LINE)
    router.release(release(300, 200))
    assert col.points == []


def test_router_never_raises_on_a_deleted_view(qapp, col, cam):
    scene = QGraphicsScene()
    v = QGraphicsView(scene)
    r = ClickToolRouter(v, col, cam)
    r.arm(LINE)
    from PyQt6 import sip
    sip.delete(v)
    assert r.press(press(1, 1)) is False
    assert r.release(release(1, 1)) is False
    assert r.double_click(dbl(1, 1)) is False
    assert r.key(keyev(Qt.Key.Key_Escape)) is False
    assert r.move(move(1, 1)) is False
    assert r.arm(LINE) is False
    r.leave()
    r.hide()
    r.disarm()


def test_router_never_emits_signals_nor_touches_the_scene(router, view, col):
    scene = view.scene()
    before = len(scene.items())
    router.arm(RECT)
    router.press(press(50, 50))
    router.move(move(60, 60))
    router.press(press(90, 90))
    router.hide()
    assert len(scene.items()) == before


def test_leave_after_a_click_clears_the_host_ghost(router, col, host):
    router.arm(LINE)
    router.move(move(60, 70))
    router.press(press(60, 70))
    host.calls.clear()
    router.leave()
    assert len(host.named('preview')) == 1 and host.named('preview')[0][3] == []


def test_leave_when_idle_emits_nothing(router, host):
    router.leave()
    assert host.calls == []


def test_refused_duplicate_press_then_drag_adds_the_release_point_and_finishes(router, col, host, view):
    router.arm(RECT)
    router.press(press(50, 50))
    host.calls.clear()
    router.press(press(50, 50))                       # duplicate of the last vertex: refused
    assert host.named('refused') and col.points == [sp(view, 50, 50)]
    router.release(release(50 + QApplication.startDragDistance() + 5, 50))
    assert not col.armed
    assert host.named('finished')[0][2] == [sp(view, 50, 50), sp(view, 50 + QApplication.startDragDistance() + 5, 50)]


def test_refused_duplicate_press_then_release_at_the_press_adds_nothing(router, col, view):
    router.arm(LINE)
    router.press(press(50, 50))
    router.press(press(50, 50))
    router.release(release(50, 50))
    assert col.points == [sp(view, 50, 50)]


def test_refused_press_with_no_points_does_not_remember_the_press(router, col, host):
    router.arm(LINE)
    host.snap_result = []
    router.press(press(50, 50))
    host.snap_result = None
    router.release(release(300, 200))
    assert col.points == []


def test_refused_duplicate_press_with_the_flag_off_does_not_drag_add(router, col, view):
    router.arm(WIRE)
    router.press(press(50, 50))
    router.press(press(50, 50))
    router.release(release(300, 200))
    assert col.points == [sp(view, 50, 50)]


def test_press_that_completed_a_count_one_tool_then_drag_adds_nothing(router, col, host):
    host.stay = True
    router.arm(ToolSpec('one', count=1, persistent=True))
    router.press(press(50, 50))
    assert col.armed and col.points == []
    router.release(release(300, 200))
    assert col.points == []


def test_a_lost_release_does_not_swallow_the_next_unrelated_release(router, col, host):
    """@brief A tool that ends on a press whose release goes elsewhere (a modal prompt) leaves no stale swallow.

    Positive: after the lost release, a fresh press/release with no tool armed is NOT consumed by the router.
    Negative control: with the tool armed the same sequence is consumed (nothing else changed).
    """
    router.arm(ToolSpec(key="t", count=1))
    assert router.press(press(50, 50)) is True      # finishes the count-1 tool; its release never arrives
    assert col.armed is False
    assert router.press(press(60, 60)) is False     # idle press resets the stale record
    assert router.release(release(60, 60)) is False


def test_settle_cursor_does_nothing_while_a_tool_is_armed_or_never_ended(router, col, view):
    """@brief `settle_cursor` only resets a stale cross: armed, or with no tool ever ended, it leaves the cursor alone."""
    ev = move(20, 20)
    router.settle_cursor(ev)                                  # nothing ended yet
    router.arm(LINE)
    router.settle_cursor(ev)                                  # armed
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    router.disarm()
    view.viewport().setCursor(Qt.CursorShape.CrossCursor)     # what Qt's restore leaves behind
    router.settle_cursor(ev)
    assert view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor


def test_a_drag_release_snapping_onto_the_last_point_is_dropped_silently(router, col, host):
    """@brief A trembling click (moved past the drag distance, snapped back) shows no «Punto coincidente».

    Positive: the release point equal to the pressed one is refused WITHOUT `tool_refused`.
    Negative twin: a release on a different snapped point is added (a real drag closes the rectangle).
    """
    d = QApplication.startDragDistance()
    router.arm(RECT_P)
    host.snap_result = [(0, 0)]                                 # everything snaps to one node
    router.press(press(50, 50))
    host.calls.clear()
    router.release(release(50 + d, 50))
    assert host.named('refused') == [] and col.points == [(0, 0)]
    router.disarm()
    host.snap_result = None
    router.arm(RECT_P)
    host.calls.clear()
    router.press(press(60, 60))
    router.release(release(60 + 3 * d, 60))
    assert host.named('finished'), "the real drag closed the rectangle"


# ---------------------------------------------------------------------------
# Backspace (plan A5, contract 6), angle mode (A6), polygon semantics (D1)
# ---------------------------------------------------------------------------

from kicadgen.gui.canvas_clicktools import (ANGLE_45, ANGLE_90, ANGLE_FREE, ANGLE_MODES,
                                            next_angle_mode)


def keyev_mod(key, mods):
    return QKeyEvent(QEvent.Type.KeyPress, key, mods)


class HostEx(Host):
    """Host with the optional callbacks: snaps like `Host` and records the angle mode."""

    def __init__(self):
        super().__init__()
        self.modes = []

    def tool_snap_ex(self, spec, point, free, points, angle_mode):
        self.calls.append(('snap_ex', spec.key, free, tuple(points), angle_mode))
        return [(round(point.x()), round(point.y()))]

    def tool_angle_mode_changed(self, mode):
        self.modes.append(mode)


def test_undo_last_removes_exactly_the_last_point_and_reports_it(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.click(QPointF(2, 2))
    host.calls.clear()
    assert col.undo_last() is True
    assert col.points == [(1, 1)] and col.armed
    assert host.named('points') == [('points', 'line', [(1, 1)])]


def test_undo_last_with_no_point_is_a_no_op_that_keeps_the_tool(col, host):
    col.arm(LINE)
    host.calls.clear()
    assert col.undo_last() is False
    assert col.armed and host.calls == []


def test_undo_last_when_idle_is_a_no_op(col, host):
    assert col.undo_last() is False and host.calls == []


def test_undo_last_removes_one_point_of_a_two_point_append(col, host):
    col.arm(WIRE)
    host.snap_result = [(5, 0), (5, 5)]          # the wire's corner, then the point
    col.click(QPointF(5, 5))
    assert col.undo_last() is True
    assert col.points == [(5, 0)]


def test_undo_refreshes_the_ghost_from_the_last_hover_without_a_move(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.click(QPointF(2, 2))
    col.hover(QPointF(7, 7))
    host.calls.clear()
    col.undo_last()
    snaps = host.named('snap')
    assert snaps == [('snap', 'line', False, ((1, 1),))], "re-snapped against the remaining points"
    assert host.named('preview') == [('preview', 'line', [(1, 1)], [(7, 7)])]
    assert col.preview == [(7, 7)]


def test_undo_without_a_remembered_hover_clears_the_ghost(col, host):
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.hover(None)                                # the pointer left: nothing to re-snap
    host.calls.clear()
    col.undo_last()
    assert host.named('snap') == []
    assert host.named('preview') == [('preview', 'line', [], [])]


def test_undo_emits_the_preview_even_when_it_is_unchanged(col, host):
    # The ghost was drawn FROM the removed point: the same appended point still
    # needs a redraw, so the emission is unconditional.
    col.arm(LINE)
    col.click(QPointF(1, 1))
    col.click(QPointF(2, 2))
    col.hover(QPointF(7, 7))
    col.click(QPointF(7, 7))
    col.hover(QPointF(7, 7))                       # preview equals [(7, 7)] once more
    host.calls.clear()
    col.undo_last()
    assert len(host.named('preview')) == 1


def test_a_new_arm_forgets_the_last_hover(col, host):
    col.arm(LINE)
    col.hover(QPointF(3, 3))
    col.arm(LINE)
    col.click(QPointF(1, 1))
    host.calls.clear()
    col.undo_last()
    assert host.named('snap') == [] and host.named('preview') == [('preview', 'line', [], [])]


def test_backspace_through_the_router_removes_the_last_point(router, col, view, host):
    router.arm(LINE)
    router.press(press(50, 50))
    router.release(release(50, 50))
    router.press(press(80, 50))
    router.release(release(80, 50))
    ev = keyev(Qt.Key.Key_Backspace)
    assert router.key(ev) is True and ev.isAccepted()
    assert col.points == [sp(view, 50, 50)] and col.armed


def test_backspace_with_no_point_is_consumed_and_keeps_the_tool(router, col, host):
    router.arm(LINE)
    host.calls.clear()
    assert router.key(keyev(Qt.Key.Key_Backspace)) is True
    assert col.armed and host.named('finished') == []


def test_backspace_is_not_consumed_when_idle(router, col):
    ev = keyev(Qt.Key.Key_Backspace)
    ev.ignore()
    assert router.key(ev) is False and not ev.isAccepted()


def test_angle_mode_constants_match_the_geometry_module_values():
    assert ANGLE_MODES == (ANGLE_FREE, ANGLE_90, ANGLE_45)
    assert (ANGLE_FREE, ANGLE_45, ANGLE_90) == ("free", "45", "90")


def test_next_angle_mode_cycles_and_falls_back_to_free():
    assert [next_angle_mode(m) for m in ANGLE_MODES] == [ANGLE_90, ANGLE_45, ANGLE_FREE]
    assert next_angle_mode("bogus") == ANGLE_FREE


def test_shift_space_cycles_the_angle_mode_and_calls_the_host():
    h = HostEx()
    c = PointCollector(h)
    c.arm(LINE)
    assert c.cycle_angle_mode() == ANGLE_90
    assert c.cycle_angle_mode() == ANGLE_45
    assert c.cycle_angle_mode() == ANGLE_FREE
    assert h.modes == [ANGLE_90, ANGLE_45, ANGLE_FREE]


def test_shift_space_through_the_router_while_armed(view, cam):
    h = HostEx()
    c = PointCollector(h)
    r = ClickToolRouter(view, c, cam)
    r.arm(LINE)
    ev = keyev_mod(Qt.Key.Key_Space, SHIFT)
    assert r.key(ev) is True and ev.isAccepted()
    assert r.angle_mode == ANGLE_90 and h.modes == [ANGLE_90]


def test_plain_space_and_idle_shift_space_do_not_cycle(view, cam):
    h = HostEx()
    c = PointCollector(h)
    r = ClickToolRouter(view, c, cam)
    assert r.key(keyev_mod(Qt.Key.Key_Space, SHIFT)) is False      # idle: the view decides
    r.arm(LINE)
    ev = keyev(Qt.Key.Key_Space)
    ev.ignore()
    assert r.key(ev) is False and not ev.isAccepted()              # Space alone is KiCad's local origin
    assert r.angle_mode == ANGLE_FREE and h.modes == []


def test_angle_mode_without_the_optional_callback_is_silent(router, col, host):
    router.arm(LINE)
    assert router.key(keyev_mod(Qt.Key.Key_Space, SHIFT)) is True
    assert col.angle_mode == ANGLE_90                               # the plain host has no callback: no error


def test_set_angle_mode_emits_once_and_only_on_a_change():
    h = HostEx()
    c = PointCollector(h)
    assert c.set_angle_mode(ANGLE_45) is True                       # idle: the panel control works too
    assert c.set_angle_mode(ANGLE_45) is False
    assert c.set_angle_mode("bogus") is False
    assert h.modes == [ANGLE_45] and c.angle_mode == ANGLE_45


def test_set_angle_mode_accepts_a_str_enum_value():
    import enum

    class Mode(str, enum.Enum):
        DEG90 = "90"

    c = PointCollector(HostEx())
    assert c.set_angle_mode(Mode.DEG90) is True and c.angle_mode == ANGLE_90
    assert type(c.angle_mode) is str


def test_an_angle_mode_change_refreshes_the_ghost_with_the_new_mode():
    h = HostEx()
    c = PointCollector(h)
    c.arm(LINE)
    c.click(QPointF(0, 0))
    c.hover(QPointF(4, 3))
    h.calls.clear()
    c.set_angle_mode(ANGLE_45)
    assert h.named('snap_ex') == [('snap_ex', 'line', False, ((0, 0),), ANGLE_45)]


def test_tool_snap_ex_is_preferred_when_the_host_defines_it():
    h = HostEx()
    c = PointCollector(h)
    c.set_angle_mode(ANGLE_90)
    c.arm(LINE)
    c.click(QPointF(1, 2), free=True)
    c.hover(QPointF(3, 3))
    assert h.named('snap') == []
    assert h.named('snap_ex') == [('snap_ex', 'line', True, (), ANGLE_90),
                                  ('snap_ex', 'line', False, ((1, 2),), ANGLE_90)]


def test_tool_snap_is_used_when_the_host_has_no_extended_snap(col, host):
    col.set_angle_mode(ANGLE_90)
    col.arm(LINE)
    col.click(QPointF(1, 2))
    col.hover(QPointF(3, 3))
    assert [c[0] for c in host.calls if c[0].startswith('snap')] == ['snap', 'snap']


def test_tool_spec_min_points_defaults_to_zero_and_is_part_of_the_value():
    assert ToolSpec('line').min_points == 0
    assert ToolSpec('polygon', min_points=3) != ToolSpec('polygon')


#: An explicit polygon (plan D1). `close_after` counts the closing click too, so
#: a polygon of at least three vertices closes from the FOURTH click (the one on
#: the first vertex): close_after=4, as the line's polygon rule of both tabs.
POLYGON = ToolSpec('polygon', count=0, close_after=4, min_points=3)


def test_explicit_polygon_closes_on_the_first_point_with_three_points(col, host):
    col.arm(POLYGON)
    for p in ((0, 0), (5, 0), (5, 5)):
        col.click(QPointF(*p))
    assert not host.named('finished')
    col.click(QPointF(0, 0))
    assert host.named('finished') == [('finished', 'polygon', [(0, 0), (5, 0), (5, 5), (0, 0)], True)]


def test_explicit_polygon_does_not_close_with_two_points(col, host):
    col.arm(POLYGON)
    col.click(QPointF(0, 0))
    col.click(QPointF(5, 0))
    col.click(QPointF(0, 0))                       # two vertices + the first again: no polygon
    assert host.named('finished') == [] and col.armed
    assert col.points == [(0, 0), (5, 0), (0, 0)]


def test_close_after_three_would_close_a_two_vertex_shape():
    # Pins why the polygon uses close_after=4: the threshold counts the closing click.
    h = Host()
    c = PointCollector(h)
    c.arm(ToolSpec('polygon', close_after=3))
    c.click(QPointF(0, 0))
    c.click(QPointF(5, 0))
    c.click(QPointF(0, 0))
    assert h.named('finished') == [('finished', 'polygon', [(0, 0), (5, 0), (0, 0)], True)]


# ── Snap kind in the preview comparison (review M5) ──

class KindHost(Host):
    """Host whose snap returns a fixed point while the hot point's KIND can change under it."""

    def __init__(self):
        super().__init__()
        self.kind = None
        self.snap_result = [(3, 3)]

    def tool_snap_kind(self):
        return self.kind


def test_preview_re_emitted_when_only_the_snap_kind_changes(col):
    h = KindHost()
    c = PointCollector(h)
    c.arm(LINE)
    c.hover(QPointF(3, 3))
    assert len(h.named('preview')) == 1
    h.kind = 'pad'                                   # same point, now a pad centre
    c.hover(QPointF(3.2, 3.1))
    assert len(h.named('preview')) == 2, "the marker must change even though the point did not"
    c.hover(QPointF(3.1, 3.0))
    assert len(h.named('preview')) == 2, "same point AND same kind: no emission"
    h.kind = None                                    # back to a plain grid node
    c.hover(QPointF(3.0, 3.0))
    assert len(h.named('preview')) == 3


def test_preview_kind_ignored_for_a_host_without_the_callback(col, host):
    host.snap_result = [(3, 3)]
    col.arm(LINE)
    col.hover(QPointF(3, 3))
    col.hover(QPointF(3.2, 3.2))
    assert len(host.named('preview')) == 1


def test_undo_last_records_the_kind_so_the_next_equal_hover_is_quiet():
    h = KindHost()
    h.snap_result = None                             # clicks snap to whole units
    c = PointCollector(h)
    c.arm(LINE)
    c.click(QPointF(0, 0))
    c.click(QPointF(1, 1))
    h.snap_result = [(3, 3)]
    h.kind = 'endpoint'
    c.hover(QPointF(3, 3))
    h.calls.clear()
    c.undo_last()
    assert len(h.named('preview')) == 1              # undo always emits once
    c.hover(QPointF(3, 3))
    assert len(h.named('preview')) == 1, "kind recorded by undo: the same hover stays quiet"
    h.kind = 'pad'
    c.hover(QPointF(3, 3))
    assert len(h.named('preview')) == 2


# ── Shift pressed / released without a mouse move (review M2) ──

def keyrel(key, mods=NOMOD, auto=False):
    return QKeyEvent(QEvent.Type.KeyRelease, key, mods, "", auto)


def test_shift_press_and_release_rehover_with_the_new_free_state(router, col, host, view):
    router.arm(LINE)
    router.move(move(50, 50))
    assert host.named('snap')[-1][2] is False
    host.calls.clear()
    ev = keyev_mod(Qt.Key.Key_Shift, SHIFT)
    assert router.key(ev) is False, "Shift is not consumed: the view keeps its own handling"
    assert [c[2] for c in host.named('snap')] == [True], "the ghost is recomputed as free"
    host.calls.clear()
    assert router.key_release(keyrel(Qt.Key.Key_Shift)) is False
    assert [c[2] for c in host.named('snap')] == [False], "the constraint is back on"


def test_shift_key_auto_repeat_and_idle_do_not_resnap(router, col, host):
    assert router.key(keyev_mod(Qt.Key.Key_Shift, SHIFT)) is False       # idle
    assert router.key_release(keyrel(Qt.Key.Key_Shift)) is False
    assert host.calls == []
    router.arm(LINE)
    router.move(move(50, 50))
    router.key(keyev_mod(Qt.Key.Key_Shift, SHIFT))
    host.calls.clear()
    router.key(keyev_mod(Qt.Key.Key_Shift, SHIFT))                       # auto-repeat press
    router.key_release(keyrel(Qt.Key.Key_Shift, SHIFT, auto=True))       # auto-repeat release
    assert host.named('snap') == []


def test_shift_without_a_remembered_hover_does_nothing(router, col, host):
    router.arm(LINE)
    host.calls.clear()
    assert router.modifiers_changed(True) is False
    assert host.named('snap') == []


def test_shift_after_a_click_updates_the_next_segment_ghost(router, col, host, view):
    """A click keeps the collector's hover: Shift right after it re-snaps the same pointer as free."""
    router.arm(LINE)
    router.move(move(50, 50))
    router.press(press(50, 50))
    router.release(release(50, 50))
    host.calls.clear()
    assert router.modifiers_changed(SHIFT) is True
    assert host.named('snap')[-1][2] is True and host.named('snap')[-1][3] == (sp(view, 50, 50),)


def test_modifiers_changed_accepts_bools_flags_and_events(router, col, host):
    router.arm(LINE)
    router.move(move(50, 50))
    assert router.modifiers_changed(Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)
    assert router.modifiers_changed(NOMOD)
    assert router.modifiers_changed(keyev_mod(Qt.Key.Key_Shift, NOMOD)), "a Shift PRESS means held"
    assert router.modifiers_changed(keyrel(Qt.Key.Key_Shift, SHIFT)), "a Shift RELEASE means up"
    assert router.modifiers_changed(keyev_mod(Qt.Key.Key_A, SHIFT))
    assert router.modifiers_changed(True) is False, "unchanged state: no snap"


def test_shift_space_then_shift_release_shows_the_new_constraint(view, cam):
    """Shift held: the cycle refreshes as free; releasing Shift re-hovers with the new mode."""
    h = HostEx()
    c = PointCollector(h)
    r = ClickToolRouter(view, c, cam)
    r.arm(LINE)
    r.move(move(50, 50))
    r.key(keyev_mod(Qt.Key.Key_Shift, SHIFT))
    h.calls.clear()
    assert r.key(keyev_mod(Qt.Key.Key_Space, SHIFT)) is True
    assert h.named('snap_ex')[-1][2] is True and h.named('snap_ex')[-1][4] == ANGLE_90
    h.calls.clear()
    r.key_release(keyrel(Qt.Key.Key_Shift))
    assert [(c[2], c[4]) for c in h.named('snap_ex')] == [(False, ANGLE_90)]


def test_an_armed_tool_keeps_its_cross_over_any_cursor_but_the_pan_hand(router, view, cam, qapp):
    """`QGraphicsView` restores a cursor saved before the arming (the arrow) when the pointer
    leaves an item, also on its own replay of the last mouse event after a redraw (H1 review,
    2026-11-09): while a tool is armed the cross comes back.
    CONTRACT CHANGED (2026-11-09, review of H1, plan C10): an element's own move cursor is
    replaced by the cross too. The symbol editor runs the base mouse move with a tool armed, so
    the item cursor used to win there while the footprint preview (whose router consumes the
    move) kept the cross; the cross in both is the plan's rule. Negative twins: the closed hand
    of a pan stays while the pan runs (also against an item cursor the view puts back when a pan
    step replays the last mouse event), and with no tool armed every cursor stays."""
    cross, arrow, move = Qt.CursorShape.CrossCursor, Qt.CursorShape.ArrowCursor, Qt.CursorShape.SizeAllCursor
    router.arm(LINE)
    viewport = view.viewport()
    assert viewport.cursor().shape() == cross
    viewport.setCursor(arrow)        # what the view's restore of a stored original cursor does
    qapp.processEvents()
    assert viewport.cursor().shape() == cross
    viewport.setCursor(move)         # an element's own cursor under the pointer
    qapp.processEvents()
    assert viewport.cursor().shape() == cross, "an armed tool shows the cross over an element too"
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(10, 10), grab_mouse=False)
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor, "negative: the pan keeps its hand"
    viewport.setCursor(move)         # a pan step's replayed mouse event over an element
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor, "the pan keeps its hand over items"
    cam.pan_end(QPoint(12, 12))
    qapp.processEvents()
    assert viewport.cursor().shape() == cross, "the pan over, the cross is back"
    router.disarm()
    viewport.setCursor(arrow)
    qapp.processEvents()
    assert viewport.cursor().shape() == arrow, "negative: no tool armed, the arrow stays"
    viewport.setCursor(move)
    qapp.processEvents()
    assert viewport.cursor().shape() == move, "negative: no tool armed, the item cursor stays"
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(10, 10), grab_mouse=False)
    viewport.setCursor(move)
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor, "a pan keeps its hand, tool or no tool"
    cam.pan_cancel()
