"""@brief Symbol editor: what a camera change refreshes (VER5 C and E, 2026-11-09), twin of the footprint's.

(C) coordinator decision: ONE rule for both tabs — on a camera change the
readout's point, the hover handles, the tool preview and the reach cursor
follow the pointer only in an IDLE view; with a tool armed (or a placement, a
pan, a drag, a band) the next real move does it. The symbol used to refresh
them in every state. (E) the reach cursor shown 3 px off a stroke was not
recomputed after a camera change. The footprint's twins are in
tests/test_footprint_camera_refresh.py.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402

from test_symbol_outline_hit import (OUTLINE, RECT, _deselect, _pointer_on_graphic, _revealed,  # noqa: E402
                                     _send_move, _settle, _shape, _vp, tab)  # noqa: F401

__all__ = ["tab"]


def test_with_a_tool_armed_a_camera_change_refreshes_nothing_until_the_next_move(tab, qapp, monkeypatch):
    """VER5 C. Armed (a line tool): a zoom away from the pointer leaves the readout's point, the
    handles and the tool preview as they were (the router is not asked to follow). Negative: idle,
    the same zoom moves the point and hides the handles of the element the pointer left."""
    index = _shape(tab, 'rectangle', RECT)
    tab._select_graphic(index)
    pos = _pointer_on_graphic(tab, qapp, index)
    assert _revealed(tab, index)
    assert tab.start_shape('line') is not False
    _send_move(tab.view, pos)
    _settle(qapp, 2)
    assert not tab.view.is_idle()
    kept = QPointF(tab._cursor_position)
    shown = len(_revealed(tab, index))
    follows = []
    original = tab._router.hover_at
    monkeypatch.setattr(tab._router, 'hover_at', lambda point: (follows.append(point), original(point)))
    reach = []
    monkeypatch.setattr(tab.view, 'refresh_reach', lambda: reach.append(True))
    assert tab.view.camera.zoom(4.0, QPoint(0, 0))
    _settle(qapp, 2)
    assert tab._cursor_position == kept, 'armed: the point waits for the next move'
    assert follows == [] and reach == [], 'armed: neither the tool preview nor the reach cursor follow'
    assert len(_revealed(tab, index)) == shown, 'armed: the handles are left as they were'
    tab._router.disarm()
    assert tab.view.is_idle()
    assert tab.view.camera.zoom(0.25, QPoint(0, 0))
    assert tab.view.camera.zoom(4.0, QPoint(0, 0))
    _settle(qapp, 2)
    now = tab.view.mapToScene(pos)
    assert (tab._cursor_position.x(), tab._cursor_position.y()) == pytest.approx((now.x(), now.y()))
    assert reach, 'negative: idle, the reach cursor is recomputed'
    assert tab.view.press_target(pos) != ('graphic', index)
    assert _revealed(tab, index) == [], 'negative: idle, the handles follow the pointer'


def test_the_reach_cursor_is_recomputed_after_a_camera_change(tab, qapp):
    """VER5 E, the footprint's twin: the move cursor shown within the press margin of a stroke
    stayed after a zoom carried the stroke away. Positive: a zoom away brings the arrow back.
    Negative: a zoom anchored on the pointer keeps the stroke there, and the move cursor."""
    from kicadgen.gui import canvas_menus
    index = _shape(tab, 'rectangle', RECT)
    _deselect(tab, qapp)
    viewport = tab.view.viewport()
    near = _vp(tab, *OUTLINE) - QPoint(canvas_menus.CONTEXT_HIT_MARGIN_PX - 1, 0)
    for point in (near - QPoint(0, 30), near):
        _send_move(tab.view, point)
    _settle(qapp, 2)
    assert tab.view.press_target(near) == ('graphic', index)
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, 'precondition: the reach cursor'
    assert tab.view.camera.zoom(1.2, near)
    _settle(qapp, 2)
    assert tab.view.press_target(near) == ('graphic', index)
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, 'negative: zoomed about the pointer, kept'
    assert tab.view.camera.zoom(4.0, QPoint(0, 0))
    _settle(qapp, 2)
    assert tab.view.press_target(near) is None, 'the stroke left the pointer'
    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor, 'the arrow is back'
