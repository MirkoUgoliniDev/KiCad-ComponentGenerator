"""@brief Unit tests of `kicadgen.gui.canvas_camera` on a bare QGraphicsView (offscreen).

Every rule has a positive and a negative twin. The pan tests compare the camera
with FROZEN copies of the two legacy algorithms (the symbol editor's scroll-bar
pan and the footprint preview's `centerOn` pan).
"""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView

from kicadgen.gui.canvas_camera import Camera

SYMBOL_LIMITS = (0.15, 20.0)
FOOTPRINT_LIMITS = (0.02, 40.0)


def _make_view(scene_rect=QRectF(-500, -500, 1000, 1000)):
    scene = QGraphicsScene()
    scene.setSceneRect(scene_rect)
    view = QGraphicsView(scene)
    view.resize(400, 300)
    view.show()
    return view, scene


@pytest.fixture
def view(qapp):
    v, scene = _make_view()
    qapp.processEvents()
    yield v
    v.hide()
    scene.clear()
    v.deleteLater()


@pytest.fixture
def cam(view):
    return Camera(view, limits=SYMBOL_LIMITS)


def _wheel(view, delta, pos=QPoint(100, 80)):
    return QWheelEvent(QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), QPoint(0, 0), QPoint(0, delta),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


class Counter:
    def __init__(self, signal):
        self.n = 0
        signal.connect(self._hit)

    def _hit(self):
        self.n += 1


# ---------------------------------------------------------------- construction

def test_bad_limits_and_strategy_are_refused(view):
    with pytest.raises(ValueError):
        Camera(view, limits=(0.0, 5.0))
    with pytest.raises(ValueError):
        Camera(view, limits=(5.0, 1.0))
    with pytest.raises(TypeError):
        Camera(view, limits=(1.0, 5.0), strategy='center')     # the pan strategy parameter is gone (D17)


# ------------------------------------------------------------------------ zoom

def test_zoom_within_limits_scales_exactly_and_clears_auto_fit(cam, view):
    changes = Counter(cam.changed)
    assert cam.auto_fit is True
    assert cam.zoom(2.0) is True
    assert cam.scale() == pytest.approx(2.0)
    assert cam.auto_fit is False
    assert changes.n == 1


def test_zoom_clamps_at_the_upper_limit(cam):
    assert cam.zoom(1000.0) is True            # clamped, not refused
    assert cam.scale() == pytest.approx(SYMBOL_LIMITS[1])


def test_zoom_clamps_at_the_lower_limit(cam):
    assert cam.zoom(0.0001) is True
    assert cam.scale() == pytest.approx(SYMBOL_LIMITS[0])


def test_zoom_that_would_not_change_the_scale_does_nothing(cam):
    cam.zoom(1000.0)
    cam.auto_fit = True
    changes = Counter(cam.changed)
    key = cam.camera_key()
    assert cam.zoom(2.0) is False              # already at the limit
    assert cam.auto_fit is True                # untouched
    assert changes.n == 0
    assert cam.camera_key() == key


def test_zoom_by_one_is_a_no_op(cam):
    changes = Counter(cam.changed)
    assert cam.zoom(1.0) is False
    assert cam.auto_fit is True and changes.n == 0


@pytest.mark.parametrize('factor', [0.0, -2.0, float('nan'), float('inf')])
def test_zoom_ignores_non_positive_or_non_finite_factors(cam, factor):
    assert cam.zoom(factor) is False
    assert cam.scale() == pytest.approx(1.0)


@pytest.mark.parametrize('anchor', [QPoint(0, 0), QPoint(100, 80), QPoint(399, 299), QPoint(200, 150)])
@pytest.mark.parametrize('factor', [1.5, 0.5])
def test_zoom_keeps_the_anchor_scene_point_under_the_pointer(cam, view, anchor, factor):
    view.scale(2.0, 2.0)                       # not at scale 1, so the anchor maths is exercised
    view.centerOn(30, -20)
    before = view.mapToScene(anchor)
    assert cam.zoom(factor, anchor) is True
    after = view.mapToScene(anchor)
    pixel = 0.5 / cam.scale()                  # half a device pixel (the scroll values are integers)
    assert abs(after.x() - before.x()) <= pixel and abs(after.y() - before.y()) <= pixel


def test_zoom_without_an_anchor_uses_the_viewport_centre(cam, view):
    view.centerOn(40, 40)
    centre = view.viewport().rect().center()
    before = view.mapToScene(centre)
    cam.zoom(2.0)
    after = view.mapToScene(centre)
    assert abs(after.x() - before.x()) <= 0.5 / cam.scale() and abs(after.y() - before.y()) <= 0.5 / cam.scale()


def test_zoom_clamped_step_keeps_the_anchor_too(cam, view):
    anchor = QPoint(50, 60)
    before = view.mapToScene(anchor)
    cam.zoom(10000.0, anchor)
    after = view.mapToScene(anchor)
    assert abs(after.x() - before.x()) <= 0.5 / cam.scale() and abs(after.y() - before.y()) <= 0.5 / cam.scale()


def test_limits_are_per_camera(view):
    wide = Camera(view, limits=FOOTPRINT_LIMITS)
    wide.zoom(1000.0)
    assert wide.scale() == pytest.approx(FOOTPRINT_LIMITS[1])


# ----------------------------------------------------------------------- wheel

def test_wheel_zooms_by_1_2_per_notch_about_the_pointer(cam, view):
    event = _wheel(view, 120)
    cam.wheel(event)
    assert cam.scale() == pytest.approx(1.2)
    assert event.isAccepted()


def test_wheel_down_zooms_out(cam, view):
    cam.wheel(_wheel(view, -120))
    assert cam.scale() == pytest.approx(1 / 1.2)


def test_wheel_is_clamped_to_four_notches(cam, view):
    cam.wheel(_wheel(view, 120 * 40))
    assert cam.scale() == pytest.approx(1.2 ** 4)


def test_wheel_below_the_clamp_is_not_altered(cam, view):
    cam.wheel(_wheel(view, 120 * 3))
    assert cam.scale() == pytest.approx(1.2 ** 3)


def test_wheel_clamp_follows_max_notches(view):
    cam = Camera(view, limits=SYMBOL_LIMITS, max_notches=2.0)
    cam.wheel(_wheel(view, 120 * 9))
    assert cam.scale() == pytest.approx(1.2 ** 2)


def test_blocked_wheel_changes_nothing_but_accepts(cam, view):
    changes = Counter(cam.changed)
    event = _wheel(view, 120)
    event.ignore()
    cam.wheel(event, blocked=True)
    assert event.isAccepted()
    assert cam.scale() == pytest.approx(1.0) and cam.auto_fit is True and changes.n == 0


def test_wheel_with_a_null_delta_accepts_and_does_not_zoom(cam, view):
    changes = Counter(cam.changed)
    event = _wheel(view, 0)
    event.ignore()
    cam.wheel(event)
    assert event.isAccepted() and changes.n == 0 and cam.auto_fit is True


def test_wheel_at_the_limit_accepts_without_a_signal(cam, view):
    cam.zoom(1000.0)
    changes = Counter(cam.changed)
    event = _wheel(view, 120)
    event.ignore()
    cam.wheel(event)
    assert event.isAccepted() and changes.n == 0


# ------------------------------------------------------------------------- pan

def test_pan_state_machine_and_cursor(cam, view):
    viewport = view.viewport()
    assert cam.pan_active is False and cam.pan_button is None
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(10, 10), grab_mouse=False)
    assert cam.pan_active is True and cam.pan_button == Qt.MouseButton.MiddleButton
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert cam.pan_end(QPoint(10, 10)) is True
    assert cam.pan_active is False and cam.pan_button is None
    assert not viewport.testAttribute(Qt.WidgetAttribute.WA_SetCursor)


def test_pan_end_and_cancel_when_idle_return_false(cam):
    assert cam.pan_end(QPoint(1, 1)) is False
    assert cam.pan_cancel() is False


def test_pan_cancel_cleans_up_without_moving(cam, view):
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(10, 10), grab_mouse=False)
    cam.pan_move(QPoint(40, 40))
    key = cam.camera_key()
    assert cam.pan_cancel() is True
    assert cam.pan_active is False and cam.camera_key() == key
    assert not view.viewport().testAttribute(Qt.WidgetAttribute.WA_SetCursor)
    cam.pan_move(QPoint(90, 90))               # idle: ignored
    assert cam.camera_key() == key


def test_grab_only_when_asked(cam, view, qapp):
    from PyQt6.QtWidgets import QWidget
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(1, 1), grab_mouse=False)
    assert QWidget.mouseGrabber() is not view.viewport()
    cam.pan_cancel()
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(1, 1), grab_mouse=True)
    assert QWidget.mouseGrabber() is view.viewport()
    cam.pan_end(QPoint(1, 1))
    assert QWidget.mouseGrabber() is not view.viewport()


def test_release_without_grab_does_not_release_a_foreign_grab(cam, view):
    from PyQt6.QtWidgets import QWidget
    other = QWidget()
    other.show()
    other.grabMouse()
    try:
        cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(1, 1), grab_mouse=False)
        cam.pan_end(QPoint(1, 1))
        assert QWidget.mouseGrabber() is other
    finally:
        other.releaseMouse()
        other.deleteLater()


def test_pan_begin_clears_auto_fit_by_default_and_can_keep_it(cam):
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(5, 5), grab_mouse=False)
    assert cam.auto_fit is False
    cam.pan_cancel()
    cam.auto_fit = True
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(5, 5), grab_mouse=False, keep_auto_fit=True)
    assert cam.auto_fit is True


def test_null_delta_keeps_auto_fit_and_emits_nothing(cam):
    changes = Counter(cam.changed)
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(5, 5), grab_mouse=False, keep_auto_fit=True)
    cam.pan_move(QPoint(5, 5))
    cam.pan_end(QPoint(5, 5))
    assert cam.auto_fit is True and changes.n == 0


def test_a_real_move_clears_auto_fit_and_emits_no_signal(view):
    view.scale(2.0, 2.0)
    cam = Camera(view, limits=SYMBOL_LIMITS)
    changes = Counter(cam.changed)
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(5, 5), grab_mouse=False, keep_auto_fit=True)
    cam.pan_move(QPoint(25, 15))
    cam.pan_end(QPoint(30, 20))
    assert cam.auto_fit is False and changes.n == 0


def test_pan_end_makes_a_last_move_at_the_release_point(cam, view):
    view.scale(2.0, 2.0)
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(100, 100), grab_mouse=False)
    h = view.horizontalScrollBar().value()
    cam.pan_end(QPoint(130, 100))
    assert view.horizontalScrollBar().value() == h - 30


def _legacy_center_pan(view, points):
    """FROZEN old footprint pan: re-centres with centerOn from QRect.center(), which is half a pixel short."""
    last = points[0]
    for p in points[1:]:
        delta = view.mapToScene(last) - view.mapToScene(p)
        view.centerOn(view.mapToScene(view.viewport().rect().center()) + delta)
        last = p


SCENES = [QRectF(-1e6, -1e6, 2e6, 2e6), QRectF(-300, -200, 600, 400)]   # huge, and a tight content rect


def _prepared(qapp, rect, zoom, size=(400, 300)):
    view, scene = _make_view(rect)
    view.resize(*size)
    qapp.processEvents()
    view.scale(zoom, zoom)
    view.centerOn(17, -9)
    qapp.processEvents()
    return view, scene


def _drop(view, scene):
    view.hide()
    scene.clear()
    view.deleteLater()


def _scrolls(view):
    return view.horizontalScrollBar().value(), view.verticalScrollBar().value()


def _pan(view, path):
    cam = Camera(view, limits=FOOTPRINT_LIMITS)
    cam.pan_begin(Qt.MouseButton.LeftButton, path[0], grab_mouse=False)
    for p in path[1:-1]:
        cam.pan_move(p)
    cam.pan_end(path[-1])


@pytest.mark.parametrize('rect', SCENES)
@pytest.mark.parametrize('zoom', [0.5, 3.0])
def test_the_scene_point_under_the_pointer_at_the_press_stays_under_it(qapp, rect, zoom):
    view, scene = _prepared(qapp, rect, zoom)
    path = [QPoint(200, 150), QPoint(180, 140), QPoint(150, 120), QPoint(160, 170), QPoint(190, 150)]
    grabbed = view.mapToScene(path[0])
    _pan(view, path)
    held = view.mapToScene(path[-1])
    # Where the scroll range clamps (tight scene) the picture cannot follow the pointer, so the
    # check only applies when the move was fully available.
    hbar, vbar = view.horizontalScrollBar(), view.verticalScrollBar()
    free = all(b.minimum() < b.value() < b.maximum() for b in (hbar, vbar))
    if free:
        step = 1.01 / zoom                      # one scroll step of rounding
        assert abs(held.x() - grabbed.x()) <= step and abs(held.y() - grabbed.y()) <= step
    _drop(view, scene)


@pytest.mark.parametrize('rect', SCENES)
def test_a_null_move_and_the_release_do_not_move_the_picture(qapp, rect):
    view, scene = _prepared(qapp, rect, 3.0)
    before = _scrolls(view)
    cam = Camera(view, limits=FOOTPRINT_LIMITS)
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(120, 80), grab_mouse=False)
    cam.pan_move(QPoint(120, 80))
    cam.pan_end(QPoint(120, 80))
    assert _scrolls(view) == before
    _drop(view, scene)


@pytest.mark.parametrize('rect', SCENES)
@pytest.mark.parametrize('size', [(400, 300), (401, 301)])
def test_many_one_pixel_steps_end_where_one_big_move_ends(qapp, rect, size):
    steps = 200
    one = [QPoint(300 - i, 150) for i in range(steps + 1)]
    big = [QPoint(300, 150), QPoint(300 - steps, 150)]
    view, scene = _prepared(qapp, rect, 3.0, size)
    _pan(view, one)
    many = _scrolls(view)
    _drop(view, scene)
    view, scene = _prepared(qapp, rect, 3.0, size)
    _pan(view, big)
    single = _scrolls(view)
    _drop(view, scene)
    assert many == single


@pytest.mark.parametrize('size', [(400, 300), (401, 301)])
def test_the_old_center_pan_drifts_and_the_new_one_does_not(qapp, size):
    # Negative twin: the frozen centerOn algorithm loses a scroll step per step on these sizes.
    steps = 60
    path = [QPoint(300 - i, 150) for i in range(steps + 1)]
    rect = SCENES[0]
    view, scene = _prepared(qapp, rect, 3.0, size)
    start = _scrolls(view)
    _legacy_center_pan(view, path)
    old = _scrolls(view)
    _drop(view, scene)
    view, scene = _prepared(qapp, rect, 3.0, size)
    _pan(view, path)
    new = _scrolls(view)
    _drop(view, scene)
    assert new == (start[0] + steps, start[1])      # exact: the content followed the pointer
    assert abs(old[0] - new[0]) + abs(old[1] - new[1]) >= steps // 2   # the old one drifted by about a step each time


# ------------------------------------------------------------------------- fit

def test_fit_frames_the_rectangle_sets_auto_fit_and_emits(cam, view):
    cam.auto_fit = True
    changes = Counter(cam.changed)
    assert cam.fit(QRectF(-100, -100, 200, 200)) is True
    assert cam.auto_fit is True and changes.n == 1
    assert cam.scale() == pytest.approx(min(view.viewport().width(), view.viewport().height()) / 200, rel=0.05)


def test_fit_memo_skips_an_identical_second_call(cam):
    rect = QRectF(-100, -100, 200, 200)
    assert cam.fit(rect) is True
    changes = Counter(cam.changed)
    key = cam.camera_key()
    assert cam.fit(rect) is False
    assert changes.n == 0 and cam.camera_key() == key


def test_fit_refits_when_the_viewport_changes(cam, view, qapp):
    rect = QRectF(-100, -100, 200, 200)
    cam.fit(rect)
    view.resize(500, 400)
    qapp.processEvents()
    assert cam.fit(rect) is True


def test_fit_refits_when_the_scene_rect_changes(cam, view):
    rect = QRectF(-100, -100, 200, 200)
    cam.fit(rect)
    view.scene().setSceneRect(-600, -600, 1200, 1200)
    assert cam.fit(rect) is True


def test_fit_refits_when_the_framing_rectangle_changes(cam):
    cam.fit(QRectF(-100, -100, 200, 200))
    assert cam.fit(QRectF(-50, -50, 100, 100)) is True


def test_fit_refits_after_the_user_moved_the_camera(cam, view):
    rect = QRectF(-100, -100, 200, 200)
    cam.fit(rect)
    cam.zoom(2.0)
    assert cam.auto_fit is False
    assert cam.fit(rect) is False              # auto-fit off: skipped
    cam.auto_fit = True                        # the host's explicit «fit» turns it back on
    assert cam.fit(rect) is True               # the camera key changed since the last fit


def test_fit_is_skipped_while_auto_fit_is_off_unless_forced(cam):
    rect = QRectF(-100, -100, 200, 200)
    cam.auto_fit = False
    assert cam.fit(rect) is False
    assert cam.scale() == pytest.approx(1.0)
    assert cam.fit(rect, force=True) is True
    assert cam.auto_fit is True


def test_force_refits_even_when_the_camera_is_unchanged(cam):
    rect = QRectF(-100, -100, 200, 200)
    cam.fit(rect)
    assert cam.fit(rect) is False
    assert cam.fit(rect, force=True) is True


def test_empty_rectangle_fits_nothing_but_turns_auto_fit_on(cam):
    changes = Counter(cam.changed)
    cam.auto_fit = True
    assert cam.fit(QRectF()) is False
    cam.auto_fit = False
    assert cam.fit(QRectF(0, 0, 0, 50), force=True) is False
    assert cam.auto_fit is True                # legacy symbol fit: set before the rect is looked at
    assert changes.n == 0 and cam.scale() == pytest.approx(1.0)


def test_empty_rectangle_with_auto_fit_off_and_no_force_leaves_it_off(cam):
    cam.auto_fit = False
    assert cam.fit(QRectF()) is False
    assert cam.auto_fit is False


def test_auto_fit_is_a_plain_read_write_attribute(cam):
    cam.auto_fit = False
    assert cam.auto_fit is False
    cam.auto_fit = True
    assert cam.auto_fit is True


def test_pan_cursor_and_grab_are_on_the_viewport(cam, view):
    from PyQt6.QtWidgets import QWidget
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(3, 3), grab_mouse=True)
    assert view.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert QWidget.mouseGrabber() is view.viewport()
    cam.pan_cancel()
    assert QWidget.mouseGrabber() is not view.viewport()
    assert not view.viewport().testAttribute(Qt.WidgetAttribute.WA_SetCursor)


def test_reset_starts_from_the_identity_transform(cam, view):
    view.rotate(0)
    view.scale(7.0, 7.0)
    rect = QRectF(-100, -100, 200, 200)
    cam.fit(rect, force=True, reset=True)
    fitted = cam.scale()
    view.scale(3.0, 3.0)
    cam.fit(rect, force=True, reset=True)
    assert cam.scale() == pytest.approx(fitted)


def test_fit_matches_a_plain_fit_in_view(cam, view, qapp):
    other, scene = _make_view()
    qapp.processEvents()
    rect = QRectF(-123, -77, 240, 130)
    other.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
    cam.fit(rect)
    assert view.transform() == other.transform()
    assert view.horizontalScrollBar().value() == other.horizontalScrollBar().value()
    other.hide()
    scene.clear()
    other.deleteLater()


def test_camera_key_changes_with_each_ingredient(cam, view):
    key = cam.camera_key()
    view.scale(2.0, 2.0)
    assert cam.camera_key() != key
    key = cam.camera_key()
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().value() + 5)
    assert cam.camera_key() != key
    key = cam.camera_key()
    view.scene().setSceneRect(-10, -10, 20, 20)
    assert cam.camera_key() != key


def test_signal_count_over_a_session(cam, view):
    changes = Counter(cam.changed)
    cam.fit(QRectF(-100, -100, 200, 200))      # 1
    cam.fit(QRectF(-100, -100, 200, 200))      # skipped
    cam.zoom(1.5)                              # 2
    cam.zoom(1.0)                              # no-op
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(50, 50), grab_mouse=False)
    cam.pan_move(QPoint(60, 55))               # a pan emits nothing
    cam.pan_end(QPoint(60, 55))
    assert changes.n == 2


@pytest.mark.parametrize('size', [(400, 300), (401, 301), (333, 250), (640, 481)])
@pytest.mark.parametrize('factor', [1.2, 0.7, 2.5, 1.0000001])
@pytest.mark.parametrize('anchor', [QPoint(7, 9), QPoint(120, 80), QPoint(250, 190)])
def test_zoom_anchor_holds_within_half_a_pixel_for_odd_and_even_viewports(qapp, size, factor, anchor):
    view, scene = _prepared(qapp, SCENES[0], 1.7, size)
    cam = Camera(view, limits=FOOTPRINT_LIMITS)
    before = view.mapToScene(anchor)
    cam.zoom(factor, anchor)
    after = view.mapToScene(anchor)
    tol = 0.5 / cam.scale() + 1e-9
    assert abs(after.x() - before.x()) <= tol and abs(after.y() - before.y()) <= tol
    _drop(view, scene)


def test_repeated_zooms_about_a_point_do_not_walk_the_picture(qapp):
    view, scene = _prepared(qapp, SCENES[0], 1.0, (400, 300))
    cam = Camera(view, limits=FOOTPRINT_LIMITS)
    anchor = QPoint(200, 150)
    held = view.mapToScene(anchor)
    for _ in range(10):
        cam.zoom(1.1, anchor)
        cam.zoom(1 / 1.1, anchor)
    after = view.mapToScene(anchor)
    assert abs(after.x() - held.x()) <= 1.0 and abs(after.y() - held.y()) <= 1.0
    _drop(view, scene)


def test_begin_while_a_pan_runs_releases_the_earlier_grab(cam, view):
    from PyQt6.QtWidgets import QWidget
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(5, 5), grab_mouse=True)
    assert QWidget.mouseGrabber() is view.viewport()
    cam.pan_begin(Qt.MouseButton.LeftButton, QPoint(9, 9), grab_mouse=False)
    assert QWidget.mouseGrabber() is not view.viewport()
    assert cam.pan_active and cam.pan_button == Qt.MouseButton.LeftButton
    cam.pan_end(QPoint(9, 9))
    assert not cam.pan_active and not view.viewport().testAttribute(Qt.WidgetAttribute.WA_SetCursor)


def test_a_normal_begin_end_pair_is_unchanged(cam, view):
    from PyQt6.QtWidgets import QWidget
    cam.pan_begin(Qt.MouseButton.MiddleButton, QPoint(5, 5), grab_mouse=True)
    assert QWidget.mouseGrabber() is view.viewport()
    assert cam.pan_end(QPoint(5, 5)) is True
    assert QWidget.mouseGrabber() is not view.viewport() and cam.pan_active is False
