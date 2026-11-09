"""@brief Footprint preview: what a camera change refreshes, and never through a hidden layer (VER5, 2026-11-09).

Verification VER5 of the PointerTracker round:
(A) the hover refresh ran inside `MainWindow._draw_preview`'s auto-fit, BEFORE
`_after_preview_drawn` hid the items of the hidden layers, so it revealed the
handles of a drawing on a hidden layer and Delete with nothing selected
removed an invisible drawing; hiding a layer while a drawing's handles were
revealed left them on screen. (B) a programmatic scroll
(`_scroll_preview_mm`) emitted no camera change. (C) one rule with the symbol
editor: a camera change refreshes only an idle view. (D) the body hover read
`QCursor.pos()`, not the shared tracker. (E) the reach cursor was not
recomputed after a camera change. The symbol editor's twins are in
tests/test_symbol_camera_refresh.py.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QCursor  # noqa: E402

from test_footprint_outline_hit import (KEYS, SHAPES, _key_effect, _pos, _send_move,  # noqa: E402
                                        _shown, _zoom_away, pointer_on_drawing)

HANDLE_ROLE = 0x48474F


def _visible_handles(window):
    """The drawing handles (and ghosts) shown on the preview scene."""
    return [item for item in window.preview_scene.items() if item.data(HANDLE_ROLE) is not None and item.isVisible()]


def _set_layer(window, monkeypatch, key, shown):
    """Switch a «Layer» panel entry as its checkbox does, without writing the configuration file."""
    monkeypatch.setattr(window, "_save_config_safely", lambda: True)
    window._on_preview_layer_toggled(key, shown)


def _refit_redraw(window, qapp, pos):
    """Zoom about the pointer (the camera leaves the fit), then redraw with the auto-fit on: the
    fit runs INSIDE the redraw and brings the drawing back under the still pointer."""
    view = window.preview_view
    assert view.camera.zoom(1.05, pos)
    view.auto_fit = True
    window._draw_preview()
    qapp.processEvents()


# ── A: the camera reaction waits for the complete scene ────────────────

@pytest.mark.parametrize("hidden", [True, False])
def test_a_refit_redraw_never_reveals_the_handles_of_a_drawing_on_a_hidden_layer(window, qapp, monkeypatch, hidden):
    """Reproduction of VER5 A. Positive (layer shown): after the refit redraw the drawing is back under
    the pointer, its handles show and Delete removes it. Negative (layer hidden): no handle, no target,
    and Delete leaves the invisible drawing alone. Before the fix the hidden case revealed the handles
    and Delete removed the drawing."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    view.auto_fit = True
    window._draw_preview()
    qapp.processEvents()
    pos = pointer_on_drawing(window, qapp, 0)
    _send_move(view, QPoint(2, 2))
    if hidden:
        _set_layer(window, monkeypatch, "F.SilkS", False)
    _send_move(view, pos)
    qapp.processEvents()
    _refit_redraw(window, qapp, pos)
    if hidden:
        assert view.hovered_drawing() == -1 and window._fp_hovered_drawing == -1
        assert _visible_handles(window) == []
        assert view.drawing_under_pointer() == -1 and window._fp_targets() == []
        drawings, _undo, _clip = _key_effect(window, KEYS["delete"])
        assert len(drawings) == 1, "Delete with nothing selected leaves the invisible drawing"
    else:
        assert view.hovered_drawing() == 0 and _visible_handles(window), "the shown drawing reveals its handles"
        drawings, _undo, _clip = _key_effect(window, KEYS["delete"])
        assert drawings == [], "positive: the visible drawing under the pointer is deleted"


def test_the_camera_reaction_of_a_redraw_runs_once_on_the_complete_scene(window, qapp, monkeypatch):
    """The deferral itself, independent of the handle guard: every hover the camera change triggers
    during a redraw runs after the layer visibility of that redraw was applied (never mid-draw), and
    it does run (positive: the refit changed the camera)."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    applied = []
    apply = window._apply_preview_layer_visibility
    monkeypatch.setattr(window, "_apply_preview_layer_visibility", lambda: (applied.append(True), apply()))
    calls = []
    original = view._hover_at
    monkeypatch.setattr(view, "_hover_at", lambda p: (calls.append(bool(applied)), original(p)))
    readouts = []
    view.camera_changed.connect(lambda: readouts.append(bool(applied)))
    assert view.camera.zoom(1.05, pos)
    applied.clear()
    calls.clear()
    readouts.clear()
    view.auto_fit = True
    window._draw_preview()
    qapp.processEvents()
    assert calls and all(calls), "the hover ran, and only once the scene was complete"
    assert readouts and all(readouts), "so did the public camera_changed (the readout)"
    calls.clear()
    view.begin_scene_update()
    view.auto_fit = False
    assert view.camera.zoom(1.1, QPoint(0, 0))
    assert calls == [], "negative: inside the bracket nothing runs"
    view.end_scene_update()
    assert len(calls) == 1, "the waiting reaction runs once at the end"
    assert not view.scene_updating()


def test_hiding_a_layer_hides_the_revealed_handles_of_its_drawing(window, qapp, monkeypatch):
    """The preexisting class of VER5 A: handles are not tagged with a layer, so hiding the hovered
    drawing's layer left them on screen, where a press, E or Delete reached the invisible drawing.
    Positive: hiding F.SilkS takes the handles and the target away. Negative: hiding another layer
    (F.Fab) keeps both."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pointer_on_drawing(window, qapp, 0)
    assert window._fp_hovered_drawing == 0 and _visible_handles(window)
    handle = _visible_handles(window)[0]
    at_handle = view.mapFromScene(handle.sceneBoundingRect().center())
    _set_layer(window, monkeypatch, "F.Fab", False)
    assert window._fp_hovered_drawing == 0 and _visible_handles(window), "negative: another layer keeps them"
    assert view.drawing_under_pointer() == 0
    _set_layer(window, monkeypatch, "F.SilkS", False)
    assert window._fp_hovered_drawing == -1 and view.hovered_drawing() == -1
    assert _visible_handles(window) == []
    assert view.press_target(at_handle) is None, "no handle left to press"
    assert view.drawing_under_pointer() == -1 and window._fp_targets() == []
    drawings, _undo, _clip = _key_effect(window, KEYS["delete"])
    assert len(drawings) == 1


def test_the_pointer_fallback_never_takes_a_drawing_whose_items_are_hidden(window, qapp, monkeypatch):
    """Even with a handle still on the scene (hidden by a path that did not go through the window),
    the keyboard's target is never an invisible drawing. Negative: the same handle with the drawing
    shown is the target."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pointer_on_drawing(window, qapp, 0)
    handle = _visible_handles(window)[0]
    at_handle = view.mapFromScene(handle.sceneBoundingRect().center())
    _send_move(view, at_handle)
    qapp.processEvents()
    assert view.press_target(at_handle)[0] == "handle"
    assert view.drawing_under_pointer() == 0, "negative: shown, the handle's drawing is the target"
    for item in window.preview_scene.items():
        if item.data(DRAWING_KEY_ROLE) == 0:
            item.setVisible(False)
    assert view.press_target(at_handle)[0] == "handle", "the handle is still there"
    assert view.drawing_under_pointer() == -1 and window._fp_targets() == []


# ── B: a programmatic scroll is a camera change ─────────────────────────

def test_a_programmatic_scroll_moves_the_readout_and_the_hover(window, qapp, monkeypatch):
    """VER5 B: `_scroll_preview_mm` set the scroll bars directly, so the coordinate line and the
    revealed handles stayed on the old scene point. Positive: a 40 mm scroll moves the readout to
    the point under the cursor now and drops the hover. Negative: a null scroll emits nothing."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    assert view.hovered_drawing() == 0
    view.auto_fit = False
    emitted = []
    view.camera_changed.connect(lambda: emitted.append(True))
    window._scroll_preview_mm(0.0, 0.0)
    assert emitted == [], "negative: nothing moved, no camera change"
    window._scroll_preview_mm(-40.0, 0.0)
    qapp.processEvents()
    assert emitted == [True]
    now = view.mapToScene(pos)
    assert (window._fp_cursor_position.x(), window._fp_cursor_position.y()) == pytest.approx((now.x(), now.y()))
    assert view.hovered_drawing() == -1 and window._fp_hovered_drawing == -1 and _visible_handles(window) == []


# ── C: one rule — only an idle view follows a camera change ─────────────

def test_with_a_tool_armed_a_camera_change_refreshes_nothing_until_the_next_move(window, qapp, monkeypatch):
    """Coordinator decision VER5 C, the symbol's twin: with a draw tool armed the readout's point and
    the hover stay until the tool's next real move; negative: disarmed, the same zoom moves them."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    window._arm_footprint_tool("line")
    _send_move(view, pos)
    qapp.processEvents()
    assert view.click_collector.armed
    kept = QPointF(window._fp_cursor_position)
    calls = []
    original = view._hover_at
    monkeypatch.setattr(view, "_hover_at", lambda p: (calls.append(p), original(p)))
    view.auto_fit = False
    assert view.camera.zoom(4.0, QPoint(0, 0))
    qapp.processEvents()
    assert window._fp_cursor_position == kept and calls == [], "armed: nothing follows the camera"
    view.click_router.disarm()
    assert view.camera.zoom(0.5, QPoint(0, 0))
    qapp.processEvents()
    now = view.mapToScene(pos)
    assert calls, "negative: idle, the hover follows"
    assert (window._fp_cursor_position.x(), window._fp_cursor_position.y()) == pytest.approx((now.x(), now.y()))


# ── D: the body hover reads the shared pointer ──────────────────────────

def test_the_body_hover_reads_the_shared_pointer_not_the_platform_cursor(window, qapp, monkeypatch):
    """VER5 D: `refresh_body_hover` read `QCursor.pos()`, which a synthetic move (and a key press
    handled before the platform cursor caught up) does not set, so one key press could see two
    pointers. With the platform cursor parked far away, the body handles revealed by a move on the
    outline stay revealed after the refresh; negative: once the pointer left, they hide."""
    from kicadgen.gui.footprint_view import BODY_PART_ROLE
    _shown(window, qapp, monkeypatch, [])
    view = window.preview_view
    far = view.viewport().mapToGlobal(QPoint(-500, -500))
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(far)))
    on_outline = _pos(window, -7.5, 0.0)
    for point in (on_outline + QPoint(30, 0), on_outline):
        _send_move(view, point)
    qapp.processEvents()

    def shown():
        return [item for item in window.preview_scene.items()
                if item.data(BODY_PART_ROLE) not in (None, "", "move") and item.isVisible()]
    assert shown(), "precondition: the move on the outline reveals the body handles"
    view.refresh_body_hover()
    assert shown(), "the refresh reads the tracked pointer, still on the outline"
    view.leaveEvent(QEvent(QEvent.Type.Leave))
    view.refresh_body_hover()
    assert shown() == [], "negative: the pointer left, the handles hide"


# ── E: the reach cursor follows a camera change ─────────────────────────

def _near_stroke(window, qapp):
    """Move to a point within the press margin of the rectangle's left side, not exactly on it."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, HIT_MARGIN_PX
    view = window.preview_view
    edge = _pos(window, -5.0, 0.5)
    half = 0
    while [item for item in view.items(edge - QPoint(half + 1, 0)) if item.data(DRAWING_KEY_ROLE) == 0]:
        half += 1
    near = edge - QPoint(half + HIT_MARGIN_PX - 1, 0)
    for point in (near - QPoint(0, 30), near):
        _send_move(view, point)
    qapp.processEvents()
    return near


def test_the_reach_cursor_is_recomputed_after_a_camera_change(window, qapp, monkeypatch):
    """VER5 E: the move cursor shown 3 px off a stroke stayed after a zoom carried the stroke away.
    Positive: a zoom away from the pointer brings the arrow back. Negative: a wheel-like zoom anchored
    on the pointer keeps the stroke there, and the move cursor."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    viewport = view.viewport()
    near = _near_stroke(window, qapp)
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, "precondition: the reach cursor"
    view.auto_fit = False
    assert view.camera.zoom(1.2, near)
    qapp.processEvents()
    assert view.press_target(near) == ("drawing", 0)
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, "negative: zoomed about the pointer, kept"
    _zoom_away(view, qapp, near)
    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor, "the stroke left: the arrow is back"
