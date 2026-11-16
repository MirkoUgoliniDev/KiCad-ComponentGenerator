"""@brief Review 2026-11-16 (R5), canvas findings fixed through the shared components, in both tabs.

- R5-1: a drag whose button release is lost (a dialog opened mid-drag,
  Alt+Tab) is cancelled by the shared `GestureController.lost_release` (a
  move without the left button) and by `CancelOnEscapeAndHide` (focus out,
  window deactivated), never left following a pointer with no button.
- R5-2: «E» pressed mid-drag cancels the drag before the properties dialog
  opens (symbol editor; the footprint's E handler lives in main_window).
- R5-3: `paint_grid` skips the line level closer than `GRID_LINE_MIN_PX`
  on screen (shared by both tabs).
- R5-4: the right press that cancels a glyph placement opens no menu.
- R5-5: one snap per mouse move with a click tool armed (symbol editor).
- R5-6: the footprint hover's «is the hovered drawing still shown» asks
  Qt's spatial index, not the whole scene.

Every positive case has a negative twin that the rule must tell apart for a
structural reason (`.claude/rules/tests.md`).
"""
import os
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import (QColor, QContextMenuEvent, QFocusEvent, QImage,  # noqa: E402
                         QMouseEvent, QPainter, QTransform)
from PyQt6.QtTest import QTest                                       # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QGraphicsScene, QGraphicsView, QWidget  # noqa: E402

from kicadgen.gui.canvas_gestures import (                           # noqa: E402
    LOST_RELEASE_EVENTS, CancelOnEscapeAndHide, DragPlan, GestureController)
from kicadgen.gui.canvas_grid import GRID_LINE_MIN_PX, GridStyle, paint_grid, painter_scale  # noqa: E402

import test_symbol_gesture_parity as sym                             # noqa: E402
from test_symbol_gesture_parity import tab5, tab7                    # noqa: E402,F401 - fixtures
from test_canvas_gestures import RecordingHost, view                 # noqa: E402,F401 - fixture
from test_footprint_gesture_parity import fp                         # noqa: E402,F401 - fixture


def _buttonless_move(viewport, pos):
    """Deliver a move with NO button held at a viewport point (what follows a lost release)."""
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(viewport.mapToGlobal(pos)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, event)


# ── R5-1: shared component ───────────────────────────────────────────

def test_lost_release_cancels_a_drag_without_the_left_button_and_keeps_one_with_it(view):
    """@brief R5-1 unit: a move without the left button cancels (commit False, grab released); with it, nothing."""
    host = RecordingHost()
    ctrl = GestureController(view, host, grab_mouse=True)
    view.show()
    assert ctrl.press(('pin', '1'), QPointF(0, 0))
    assert QWidget.mouseGrabber() is view.viewport()
    # Negative twin: the left button still held (alone, or with another one) is a drag step.
    assert ctrl.lost_release(Qt.MouseButton.LeftButton) is False
    assert ctrl.lost_release(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton) is False
    assert ctrl.active and host.of('end') == []
    # Positive: only the right button, or none, means the left release was lost.
    assert ctrl.lost_release(Qt.MouseButton.RightButton) is True
    assert not ctrl.active
    assert host.of('end') == [('end', ('pin', '1'), None, False)], "cancelled, not committed"
    assert QWidget.mouseGrabber() is not view.viewport(), "the mouse grab is released"
    # Idle: nothing to cancel, no second end.
    assert ctrl.lost_release(Qt.MouseButton.NoButton) is False
    assert len(host.of('end')) == 1


@pytest.mark.parametrize('kind', LOST_RELEASE_EVENTS)
def test_focus_out_and_window_deactivation_cancel_a_running_drag_only(view, kind):
    """@brief R5-1 unit: FocusOut / WindowDeactivate on the view cancel a drag; idle they do nothing; other events never cancel."""
    host = RecordingHost()
    ctrl = GestureController(view, host, grab_mouse=True)
    view.installEventFilter(CancelOnEscapeAndHide(ctrl))
    view.show()

    def deliver(event_kind):
        event = (QFocusEvent(event_kind, Qt.FocusReason.ActiveWindowFocusReason)
                 if event_kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut) else QEvent(event_kind))
        QApplication.sendEvent(view, event)

    # Negative twin: the opposite events (focus in, window activated) leave the drag running.
    ctrl.press(('text', 'Reference'), QPointF(0, 0))
    deliver(QEvent.Type.FocusIn)
    deliver(QEvent.Type.WindowActivate)
    assert ctrl.active
    # Positive.
    deliver(kind)
    assert not ctrl.active
    assert host.of('end') == [('end', ('text', 'Reference'), None, False)]
    # Idle: the event passes on and ends nothing a second time.
    deliver(kind)
    assert len(host.of('end')) == 1


# ── R5-1: symbol editor, real event path ─────────────────────────────

def test_symbol_a_buttonless_move_mid_drag_cancels_it_and_the_pin_goes_back(qapp, tab7):
    """@brief R5-1 symbol: the pin returns to the start, no grab is left; the twin with the button held steps the pin."""
    tab = tab7
    start = next(p for p in tab._symbol.pins if p.pin.number == '1')
    before = (start.x, start.y)
    pos = sym._pin_press_point(tab, '1')
    far = pos + (sym._vp(tab, 0, -3 * sym.GRID_MM) - sym._vp(tab, 0, 0))
    # Negative twin: a move WITH the button held steps the pin (the drag runs).
    sym._press(tab, qapp, pos)
    sym._move(tab, qapp, far)
    moved = next(p for p in tab._symbol.pins if p.pin.number == '1')
    assert tab.view.gesture is not None and (moved.x, moved.y) != before
    # Positive: the release is lost; the next move arrives with no button.
    _buttonless_move(tab.view.viewport(), far + QPoint(3, 3))
    qapp.processEvents()
    assert tab.view.gesture is None, "the orphan drag is over"
    back = next(p for p in tab._symbol.pins if p.pin.number == '1')
    assert (back.x, back.y) == before, "cancelled to where the drag began, as Escape"
    assert QWidget.mouseGrabber() is not tab.view.viewport()
    # A later click no longer drops anything: the pin stays where it is.
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     far + QPoint(9, 9))
    qapp.processEvents()
    after = next(p for p in tab._symbol.pins if p.pin.number == '1')
    assert (after.x, after.y) == before


def test_symbol_window_deactivation_mid_drag_cancels_it(qapp, tab7):
    """@brief R5-1 symbol: the window deactivated (Alt+Tab, a dialog) mid-drag cancels the drag at once."""
    tab = tab7
    start = next(p for p in tab._symbol.pins if p.pin.number == '1')
    before = (start.x, start.y)
    pos = sym._pin_press_point(tab, '1')
    sym._press(tab, qapp, pos)
    sym._move(tab, qapp, pos + (sym._vp(tab, 0, -3 * sym.GRID_MM) - sym._vp(tab, 0, 0)))
    assert tab.view.gesture is not None
    # Qt propagates WindowDeactivate from the window to every visible child.
    QApplication.sendEvent(tab, QEvent(QEvent.Type.WindowDeactivate))
    qapp.processEvents()
    assert tab.view.gesture is None
    back = next(p for p in tab._symbol.pins if p.pin.number == '1')
    assert (back.x, back.y) == before
    sym._release(tab, qapp, pos)


# ── R5-2: «E» mid-drag (symbol editor) ───────────────────────────────

def test_symbol_e_mid_drag_ends_the_drag_before_the_dialog_and_its_edit_applies(qapp, tab7, monkeypatch):
    """@brief R5-2: E with the button down cancels the drag first; the dialog then edits; the idle twin opens the same dialog."""
    tab = tab7
    seen = []

    def fake_exec(dialog):
        seen.append({'drag': tab.view.gesture, 'grab': QWidget.mouseGrabber() is tab.view.viewport(),
                     'edit': tab.edit_pin('1', hidden=True)})
        return 0

    monkeypatch.setattr(QDialog, 'exec', fake_exec)
    pos = sym._pin_press_point(tab, '1')
    sym._press(tab, qapp, pos)
    sym._move(tab, qapp, pos + (sym._vp(tab, 0, -2 * sym.GRID_MM) - sym._vp(tab, 0, 0)))
    assert tab.view.gesture is not None
    assert tab.edit_properties() is True
    assert seen and seen[-1] == {'drag': None, 'grab': False, 'edit': True}, seen
    assert tab.view.gesture is None, "no drag survives the dialog"
    sym._release(tab, qapp, pos)
    # Negative twin: idle, E opens the very same dialog and the edit applies too.
    tab.edit_pin('1', hidden=False)
    tab._selected_pin = '1'
    assert tab.edit_properties() is True
    assert len(seen) == 2 and seen[-1]['drag'] is None and seen[-1]['edit'] is True


def test_symbol_e_with_nothing_to_edit_leaves_the_drag_alone(qapp, tab7, monkeypatch):
    """@brief R5-2 negative: a property drag with no selection and no element under the pointer: E opens nothing and cancels nothing (C3)."""
    tab = tab7
    opened = []
    monkeypatch.setattr(QDialog, 'exec', lambda dialog: opened.append(dialog) or 0)
    tab._selected_pin = None
    start = sym._property_press_point(tab, 'Reference')
    sym._press(tab, qapp, start)
    sym._move(tab, qapp, start + QPoint(30, 12))
    assert tab.view.gesture == ('property', 'Reference')
    assert not tab.selected_graphics() and tab._selected_pin is None
    assert tab.edit_properties() is False
    assert opened == [] and tab.view.gesture == ('property', 'Reference'), "the drag runs on"
    sym._release(tab, qapp, start + QPoint(30, 12))


# ── R5-4: right press cancelling a glyph placement ───────────────────

def _right_click_with_context(tab, qapp, pos):
    """A right press, its release and the context-menu event Qt sends after them."""
    viewport = tab.view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                                       viewport.mapToGlobal(pos)))
    qapp.processEvents()


def test_a_right_click_cancelling_a_glyph_placement_opens_no_menu_and_the_next_one_does(qapp, tab7):
    """@brief R5-4: the right press that ends the placement owns its context event; the following right click opens the pin menu."""
    tab = tab7
    menus = []
    tab.view.pin_context_requested.disconnect()
    tab.view.pin_context_requested.connect(lambda number, _pos: menus.append(number))
    pos = sym._pin_press_point(tab, '1')
    tab.view.placing = True
    _right_click_with_context(tab, qapp, pos)
    assert tab.view.placing is False, "the right press cancelled the placement"
    assert menus == [], "and opened no menu under it"
    # Negative twin: nothing armed, the same right click opens the pin's menu.
    _right_click_with_context(tab, qapp, pos)
    assert menus == ['1']


# ── R5-5: one snap per move ──────────────────────────────────────────

def test_one_snap_per_move_with_a_click_tool_and_hover_at_still_snaps_a_new_point(qapp, tab5, monkeypatch):
    """@brief R5-5: ten moves to ten points snap ten times; a hover at ANOTHER point (no move) still snaps."""
    tab = tab5
    assert tab.start_shape('line')
    calls = []
    original = tab.tool_snap_ex

    def counting(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(tab, 'tool_snap_ex', counting)
    base = sym._vp(tab, 0, 0)
    for i in range(10):
        sym._move(tab, qapp, base + QPoint(7 * i + 3, 5 * i + 1))
    assert len(calls) == 10, f"one snap per move, got {len(calls)}"
    last = tab.view.mapToScene(base + QPoint(7 * 9 + 3, 5 * 9 + 1))
    assert calls[-1] == last and tab._collector.hover_point == last, "the preview follows the last move"
    # Negative twin: the readout's hover at a point the move did NOT snap
    # (a camera change under a still pointer) is not skipped.
    other = tab.view.mapToScene(base + QPoint(200, 120))
    tab._router.hover_at(other)
    assert len(calls) == 11 and calls[-1] == other


# ── R5-3: grid line level below the legibility threshold ─────────────

def _grid_pixels(scale, step=20.0, size=(200, 120)):
    """Non-background pixels after painting a line-only grid at `scale` device px per scene unit."""
    image = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
    image.fill(QColor('#000000'))
    painter = QPainter(image)
    painter.setWorldTransform(QTransform.fromScale(scale, scale))
    rect = QRectF(0, 0, size[0] / scale, size[1] / scale)
    paint_grid(painter, rect, exposed=None, step=step, style=GridStyle(QColor('#ffffff')))
    painter.end()
    return sum(1 for y in range(size[1]) for x in range(size[0]) if image.pixel(x, y) != 0xff000000)


def test_grid_lines_closer_than_the_threshold_are_skipped_and_wider_ones_drawn():
    """@brief R5-3: lines 1.9 px apart are not laid out; 2 px and wider are, at any absolute scale."""
    assert GRID_LINE_MIN_PX == 2.0
    step = 20.0
    below = (GRID_LINE_MIN_PX * 0.95) / step
    at = GRID_LINE_MIN_PX / step
    assert _grid_pixels(below, step) == 0, "a sub-threshold grid is a tint: not painted"
    assert _grid_pixels(at, step) > 0
    assert _grid_pixels(1.0, step) > 0
    # The same spacing reached with another step and scale gives the same answer:
    # the rule is the on-screen spacing, not the zoom.
    assert _grid_pixels(below * 4, step / 4) == 0
    assert _grid_pixels(at * 4, step / 4) > 0


def test_painter_scale_is_the_denser_axis_and_survives_a_rotation():
    """@brief R5-3: the device scale read from the painter: min of the axes, rotation-invariant."""
    image = QImage(10, 10, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    painter.setWorldTransform(QTransform.fromScale(3.0, 0.5))
    assert painter_scale(painter) == pytest.approx(0.5)
    rotated = QTransform().rotate(30).scale(2.0, 2.0)
    painter.setWorldTransform(rotated)
    assert painter_scale(painter) == pytest.approx(2.0)
    painter.end()


def test_footprint_view_grid_at_minimum_zoom_is_cheap_and_still_drawn_at_a_working_zoom(qapp):
    """@brief R5-3 footprint view: no line at 0.02 px/unit (sub-pixel grid), lines at 1 px/unit; the first paint is fast."""
    from kicadgen.gui.footprint_view import FootprintView
    view = FootprintView(QGraphicsScene())
    view.resize(800, 500)
    view.show()
    qapp.processEvents()
    view.set_grid(20.0, (0.0, 0.0), True)

    def render(scale):
        view.resetTransform()
        view.scale(scale, scale)
        qapp.processEvents()
        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
        started = time.perf_counter()
        view.viewport().render(image)
        return image, time.perf_counter() - started

    tiny_on, elapsed = render(0.02)
    view.set_grid(20.0, (0.0, 0.0), False)
    tiny_off, _ = render(0.02)
    # Generous bound (the sub-pixel grid cost hundreds of ms before): the
    # assertion is the absence of lines, the time only guards a regression.
    assert tiny_on == tiny_off and elapsed < 0.2, "a sub-pixel grid paints nothing"
    working_off, _ = render(1.0)
    view.set_grid(20.0, (0.0, 0.0), True)
    working_on, _ = render(1.0)
    assert working_on != working_off, "the grid is still drawn where its lines are legible"
    view.deleteLater()


# ── R5-6: footprint hover hysteresis without a whole-scene walk ──────

def test_footprint_hover_near_a_drawing_asks_the_spatial_index_not_the_whole_scene(fp, monkeypatch):
    """@brief R5-6: moves inside the hysteresis margin walk no whole scene; stale bounds still answer by the full walk."""
    import test_footprint_gesture_parity as fpp
    window = fp
    view = window.preview_view
    fpp._with_drawing(window, 'rectangle', ((0, 8), (4, 10)))
    PX = fpp.PX
    fpp._move(window, fpp._vp(window, 4 * PX, 9 * PX))
    assert view.hovered_drawing() == 0
    scene = window.preview_scene
    walks = []
    original = scene.items

    def counting(*args, **kwargs):
        if not args and not kwargs:
            walks.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(scene, 'items', counting)
    # Inside the drawing's bounds, off its stroke: the hysteresis keeps it.
    for dx in (2, 4, 6):
        fpp._move(window, fpp._vp(window, 2 * PX, 9 * PX, (dx, 0)))
    assert view.hovered_drawing() == 0, "the handles stay while the pointer is near"
    assert walks == [], "no whole-scene walk per move"
    # Negative twin: bounds that no longer meet the drawing (stale) never
    # give a wrong answer: the full walk still finds it shown.
    view._hover_bounds = QRectF(-5000, -5000, 1, 1)
    assert view.drawing_shown(0) is True
    assert walks == [1]


# ── R5-1: footprint preview, real event path ─────────────────────────

def test_footprint_a_buttonless_move_mid_text_drag_cancels_it_and_the_text_goes_back(fp):
    """@brief R5-1 footprint: the text returns, nothing is committed; the twin with the button held follows the pointer."""
    import test_footprint_gesture_parity as fpp
    window = fp
    view = window.preview_view
    group = fpp._group(window, 'Reference')
    origin = QPointF(group.pos())
    start = fpp._glyph_box(window, group).center()
    fpp._press(window, start)
    fpp._move(window, start + QPoint(40, 20))
    # Negative twin: the button held, the text follows live.
    assert view.gestures.active and (group.pos() - origin).manhattanLength() > 40
    _buttonless_move(view.viewport(), start + QPoint(44, 22))
    assert not view.gestures.active, "the orphan drag is over"
    assert group.pos() == origin, "the text is back where it was, as after Escape"
    assert window.footprint_text_positions == {} and len(window._fp_drawings_undo) == 0
    fpp._release(window, start + QPoint(44, 22))
    assert window.footprint_text_positions == {}, "a later release drops nothing"


def test_footprint_window_deactivation_mid_text_drag_cancels_it(fp, qapp):
    """@brief R5-1 footprint: WindowDeactivate reaching the view (propagated by Qt) cancels the text drag."""
    import test_footprint_gesture_parity as fpp
    window = fp
    view = window.preview_view
    group = fpp._group(window, 'Reference')
    origin = QPointF(group.pos())
    start = fpp._glyph_box(window, group).center()
    fpp._press(window, start)
    fpp._move(window, start + QPoint(40, 20))
    assert view.gestures.active
    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
    qapp.processEvents()
    assert not view.gestures.active and group.pos() == origin
    fpp._release(window, start + QPoint(40, 20))
    assert window.footprint_text_positions == {}


# ── R5-1 generalised (canvas-parity follow-up): rubber band and pan ──

def test_band_and_pan_lost_release_units(view):
    """@brief Shared components: a band drops without its left button; a pan drops without the button that STARTED it."""
    from kicadgen.gui.canvas_camera import Camera
    from kicadgen.gui.canvas_selection import RubberBand
    view.show()
    band = RubberBand(view)
    band.begin(QPoint(10, 10), False)
    assert band.lost_release(Qt.MouseButton.LeftButton) is False and band.active
    assert band.lost_release(Qt.MouseButton.NoButton) is True
    assert not band.active and QWidget.mouseGrabber() is not view.viewport()
    assert band.lost_release(Qt.MouseButton.NoButton) is False, "idle: nothing to cancel"
    camera = Camera(view, limits=(0.1, 10.0))
    # A middle-button pan: the middle button held keeps it, the LEFT one alone does not.
    camera.pan_begin(Qt.MouseButton.MiddleButton, QPoint(5, 5), grab_mouse=True)
    assert camera.pan_lost_release(Qt.MouseButton.MiddleButton) is False and camera.pan_active
    assert camera.pan_lost_release(Qt.MouseButton.LeftButton) is True
    assert not camera.pan_active and QWidget.mouseGrabber() is not view.viewport()
    # A left-button pan: the left button keeps it, none drops it.
    camera.pan_begin(Qt.MouseButton.LeftButton, QPoint(5, 5), grab_mouse=False)
    assert camera.pan_lost_release(Qt.MouseButton.LeftButton) is False
    assert camera.pan_lost_release(Qt.MouseButton.NoButton) is True and not camera.pan_active
    assert camera.pan_lost_release(Qt.MouseButton.NoButton) is False


def test_the_filter_cancels_band_and_pan_on_a_lost_release_only_when_given_them(view):
    """@brief FocusOut cancels the drag, the band and the pan passed to the filter; a filter without them leaves them alone."""
    from kicadgen.gui.canvas_camera import Camera
    from kicadgen.gui.canvas_selection import RubberBand
    view.show()
    ctrl = GestureController(view, RecordingHost(), grab_mouse=True)
    band, camera = RubberBand(view), Camera(view, limits=(0.1, 10.0))
    bare = CancelOnEscapeAndHide(ctrl)
    full = CancelOnEscapeAndHide(ctrl, band=band, camera=camera)
    focus_out = QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason)
    # Negative twin: the bare filter knows only the controller.
    band.begin(QPoint(10, 10), False)
    bare.eventFilter(view, focus_out)
    assert band.active
    band.cancel()
    # Positive: the filter given the band and the camera drops both.
    band.begin(QPoint(10, 10), False)
    full.eventFilter(view, focus_out)
    assert not band.active
    camera.pan_begin(Qt.MouseButton.MiddleButton, QPoint(5, 5), grab_mouse=True)
    full.eventFilter(view, QEvent(QEvent.Type.WindowDeactivate))
    assert not camera.pan_active


def test_a_focus_out_to_the_menu_bar_keeps_the_drag_and_another_reason_cancels_it(view):
    """@brief L1: Alt alone (MenuBarFocusReason) keeps the drag, band and pan; ActiveWindowFocusReason cancels them."""
    from kicadgen.gui.canvas_camera import Camera
    from kicadgen.gui.canvas_selection import RubberBand
    view.show()
    host = RecordingHost()
    ctrl = GestureController(view, host, grab_mouse=True)
    band, camera = RubberBand(view), Camera(view, limits=(0.1, 10.0))
    view.installEventFilter(CancelOnEscapeAndHide(ctrl, band=band, camera=camera))
    ctrl.press(('pin', '1'), QPointF(0, 0))
    band.begin(QPoint(10, 10), False)
    camera.pan_begin(Qt.MouseButton.MiddleButton, QPoint(5, 5), grab_mouse=False)
    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MenuBarFocusReason))
    assert ctrl.active and band.active and camera.pan_active, "Alt alone ends nothing"
    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    assert not ctrl.active and not band.active and not camera.pan_active
    assert host.of('end') == [('end', ('pin', '1'), None, False)]


def test_symbol_alt_alone_mid_drag_keeps_the_drag_and_the_drop_commits(qapp, tab7):
    """@brief L1 symbol: a FocusOut to the menu bar mid-drag keeps the drag, whose release then commits."""
    tab = tab7
    pos = sym._pin_press_point(tab, '1')
    before = (sym._pin(tab, '1').x, sym._pin(tab, '1').y)
    far = pos + (sym._vp(tab, 0, -3 * sym.GRID_MM) - sym._vp(tab, 0, 0))
    sym._press(tab, qapp, pos)
    sym._move(tab, qapp, far)
    QApplication.sendEvent(tab.view, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MenuBarFocusReason))
    assert tab.view.gesture is not None
    sym._release(tab, qapp, far)
    assert tab.view.gesture is None
    assert (sym._pin(tab, '1').x, sym._pin(tab, '1').y) != before, "the drop committed"


def test_footprint_alt_alone_mid_text_drag_keeps_it_and_the_drop_commits(fp):
    """@brief L1 footprint: a FocusOut to the menu bar mid-drag keeps the text drag; the drop commits."""
    import test_footprint_gesture_parity as fpp
    window = fp
    view = window.preview_view
    group = fpp._group(window, 'Reference')
    start = fpp._glyph_box(window, group).center()
    fpp._press(window, start)
    fpp._move(window, start + QPoint(40, 20))
    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MenuBarFocusReason))
    assert view.gestures.active
    fpp._release(window, start + QPoint(40, 20))
    assert 'Reference' in window.footprint_text_positions


def _band_and_pan_drop_on_a_buttonless_move(qapp, view, background):
    """Shared script of both tabs: band and middle pan go on with their button and stop without it."""
    viewport = view.viewport()
    # Band: the held left button stretches it (negative twin), a button-less move drops it.
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, background)
    QTest.mouseMove(viewport, background + QPoint(60, 40))
    qapp.processEvents()
    assert view.band.active and view.band.visible
    _buttonless_move(viewport, background + QPoint(70, 50))
    assert not view.band.active and QWidget.mouseGrabber() is not viewport
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       background + QPoint(70, 50))
    qapp.processEvents()
    # Middle pan: held, it pans (negative twin); a move without it stops the pan.
    QTest.mousePress(viewport, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, background)
    hbar = view.horizontalScrollBar().value()
    QTest.mouseMove(viewport, background + QPoint(30, 0))
    qapp.processEvents()
    assert view.camera.pan_active and view.horizontalScrollBar().value() != hbar
    _buttonless_move(viewport, background + QPoint(60, 0))
    assert not view.camera.pan_active and QWidget.mouseGrabber() is not viewport
    QTest.mouseRelease(viewport, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier,
                       background + QPoint(60, 0))
    qapp.processEvents()


def test_symbol_band_and_pan_drop_on_a_buttonless_move_and_follow_a_held_one(qapp, tab7):
    """@brief R5-1 symbol: the band and a middle pan stop on a move without their button; with it they go on."""
    _band_and_pan_drop_on_a_buttonless_move(qapp, tab7.view, sym._empty_point(tab7))


def test_footprint_band_and_pan_drop_on_a_buttonless_move_and_follow_a_held_one(fp, qapp):
    """@brief R5-1 footprint: the band and a middle pan stop on a move without their button; with it they go on."""
    view = fp.preview_view
    background = QPoint(8, 8)
    assert not view.items(background), 'precondition: nothing drawn there'
    _band_and_pan_drop_on_a_buttonless_move(qapp, view, background)


def test_window_deactivation_drops_a_band_in_both_tabs(qapp, tab7, fp):
    """@brief R5-1: WindowDeactivate reaching the view drops a running band in the symbol editor and in the footprint preview."""
    for owner, view, background in ((tab7, tab7.view, sym._empty_point(tab7)),
                                    (fp, fp.preview_view, QPoint(8, 8))):
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, background)
        qapp.processEvents()
        assert view.band.active
        QApplication.sendEvent(owner, QEvent(QEvent.Type.WindowDeactivate))
        qapp.processEvents()
        assert not view.band.active
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, background)
        qapp.processEvents()


# ── R5-7: two-step Escape in the «Proprietà» panel (user decision 2026-11-16) ──

def _x_editor(panel):
    """The inner line edit of the panel's X spin box (where the keys land)."""
    box = panel.field_widget('x')
    assert box is not None, panel.field_keys()
    return box.lineEdit()


def _two_step_escape(qapp, panel, tool_armed, selected):
    """Shared script of both tabs: typed edit, Escape restores (tool kept), Escape again ends the tool (selection kept)."""
    qapp.processEvents()                 # refreshes queued by the arming land first
    editor = _x_editor(panel)
    shown = editor.text()
    editor.setFocus()
    editor.selectAll()                   # the typed digit replaces the value
    QTest.keyClicks(editor, '7')
    qapp.processEvents()
    assert panel.has_unconfirmed_edit('x') and editor.text() != shown
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert editor.text() == shown and not panel.has_unconfirmed_edit('x'), "first Escape: the field is restored"
    assert tool_armed(), "first Escape leaves the tool armed"
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not tool_armed(), "second Escape: the tool ends, as Escape on the canvas"
    assert selected(), "the selection (and the panel) stay"


def test_symbol_panel_escape_restores_first_then_disarms_the_tool(qapp, tab5):
    """@brief R5-7 symbol: Escape restores a typed field, the next Escape disarms the armed tool."""
    tab = tab5
    sym._board_with_elements(tab)
    tab._select_graphic(0)
    assert tab.start_shape('line') and tab.view.tool_armed()
    _two_step_escape(qapp, tab.properties_panel, tab.view.tool_armed, tab.selected_graphics)


def test_symbol_panel_escape_with_no_tool_armed_changes_nothing(qapp, tab5):
    """@brief R5-7 symbol negative: no tool armed, the idle Escape sends nothing: the selection and the panel stay."""
    tab = tab5
    sym._board_with_elements(tab)
    tab._select_graphic(0)
    assert not tab.view.tool_armed()
    editor = _x_editor(tab.properties_panel)
    cleared = []
    tab.view.clear_selection_requested.connect(lambda: cleared.append(1))
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert cleared == [] and tab.selected_graphics() == [0]
    assert 'x' in tab.properties_panel.field_keys()


def test_panel_escape_binding_replaces_the_previous_one(qapp, tab5):
    """@brief R5-7: binding a panel twice keeps ONE forward (two would end the tool and then clear the selection)."""
    from kicadgen.gui.canvas_properties import bind_panel_escape
    tab = tab5
    sym._board_with_elements(tab)
    tab._select_graphic(0)
    bind_panel_escape(tab.properties_panel, tab.view, tab.view.tool_armed)   # a second binding
    sent = []
    counter = _KeyCounter(sent)          # kept referenced: a collected filter is removed
    tab.view.installEventFilter(counter)
    assert tab.start_shape('line')
    QTest.keyClick(_x_editor(tab.properties_panel), Qt.Key.Key_Escape)
    qapp.processEvents()
    assert sent == [Qt.Key.Key_Escape] and not tab.view.tool_armed()
    assert tab.selected_graphics() == [0]


class _KeyCounter(QObject):
    """Event filter recording the keys pressed on the object it watches (never consumes)."""

    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            self._sink.append(event.key())
        return False


def _footprint_with_selected_drawing(window):
    """A line drawing selected in the preview (the «Proprietà» panel shows its X field)."""
    import test_footprint_gesture_parity as fpp
    fpp._with_drawing(window, 'line', ((0, 8), (4, 8)))
    window._select_footprint_drawing(0)
    window._refresh_fp_properties()


def test_footprint_panel_escape_restores_first_then_disarms_the_tool(qapp, fp):
    """@brief R5-7 footprint, through the shared binder: Escape restores a typed field, the next one disarms the tool.

    The window builds the footprint panel (main_window.py, another fixer's
    file): the test binds it as main_window must (`bind_panel_escape(panel,
    preview_view, preview_view.tool_armed)`); the binder replaces any earlier
    binding, so the test stays valid once main_window binds it too.
    """
    from kicadgen.gui.canvas_properties import bind_panel_escape
    window = fp
    view = window.preview_view
    panel = window.fp_properties_panel
    bind_panel_escape(panel, view, view.tool_armed)
    _footprint_with_selected_drawing(window)
    window._arm_footprint_tool('line')
    qapp.processEvents()
    assert view.tool_armed()
    _two_step_escape(qapp, panel, view.tool_armed, window._fp_selected_indices)


def test_footprint_panel_escape_with_no_tool_armed_keeps_the_selection(qapp, fp):
    """@brief R5-7 footprint negative: no tool armed, two Escapes in the panel keep the selection and the fields."""
    from kicadgen.gui.canvas_properties import bind_panel_escape
    window = fp
    view = window.preview_view
    panel = window.fp_properties_panel
    bind_panel_escape(panel, view, view.tool_armed)
    _footprint_with_selected_drawing(window)
    assert not view.tool_armed()
    editor = _x_editor(panel)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window._fp_selected_indices() == [0] and 'x' in panel.field_keys()
