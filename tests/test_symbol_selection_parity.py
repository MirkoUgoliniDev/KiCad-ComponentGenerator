"""@brief Element selection of the symbol editor on the shared `canvas_selection` (plan `docs/canvas-selection-plan.md`, phase D).

Parity part, written against the view's own rubber band BEFORE it became a
`canvas_selection.RubberBand`: Shift+drag (or the selection mode) selects the
elements whose whole bounds lie in the band, Ctrl adds, pins in the band win
over elements, Escape drops the band without selecting.

New behaviour (confirmed by the user, 2026-10-18), each with its negative
twin: a click on the empty background clears the selection (elements and
pins) while a background drag pans and keeps it; Escape with nothing else to
cancel clears the selection, while an Escape that cancels a placement, a click
tool, a pan, a band or a drag cancels only that. `clear_clipboard` empties the
element clipboard.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab

NO_MOD = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL_SHIFT = Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
LEFT = Qt.MouseButton.LeftButton


@pytest.fixture
def tab(qapp):
    """Two rectangles left and right of the symbol body, far from every pin."""
    widget = SymbolTab()
    widget.resize(1200, 800)
    widget.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)],
                    pad_ids=['1', '2', '3', '4', '5'])
    for x in (-40.64, 30.48):
        widget.start_shape('rectangle')
        widget.add_shape_point(x, 20.32)
        widget.add_shape_point(x + 5.08, 25.4)
    widget.view.collector.cancel()
    widget._select_graphic(None)
    assert len(widget.graphics()) == 2
    yield widget
    widget.deleteLater()


def _vp(view, x_mm, y_mm):
    """Viewport position (px) of a symbol point (mm, Y up)."""
    return view.mapFromScene(QPointF(x_mm * SCENE_UNITS_PER_MM, -y_mm * SCENE_UNITS_PER_MM))


def _mouse(view, kind, pos, button=LEFT, modifiers=NO_MOD):
    buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(pos), view.viewport().mapToGlobal(QPointF(pos)), button, buttons, modifiers)
    {QEvent.Type.MouseButtonPress: view.mousePressEvent, QEvent.Type.MouseMove: view.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)


def _key(view, key=Qt.Key.Key_Escape):
    """Send a key through the view's event filters, as Qt does."""
    QApplication.sendEvent(view, QKeyEvent(QEvent.Type.KeyPress, key, NO_MOD))


def _band(view, start, end, modifiers=SHIFT):
    _mouse(view, QEvent.Type.MouseButtonPress, start, modifiers=modifiers)
    _mouse(view, QEvent.Type.MouseMove, end, button=Qt.MouseButton.NoButton, modifiers=modifiers)
    _mouse(view, QEvent.Type.MouseButtonRelease, end, modifiers=modifiers)


def _band_active(view):
    """Whether the view draws a rubber band (the shared `RubberBand`, or the former own band)."""
    band = getattr(view, 'band', None)
    return band.active if band is not None else view._rubber_start is not None


def _no_grab():
    return QWidget.mouseGrabber() is None


def _background(view):
    """A viewport point with no item under it."""
    pos = _vp(view, 0.0, -60.96)
    assert not view.items(pos)
    return pos


# ── Rubber band parity ─────────────────────────────────────────────────────

def test_shift_band_selects_elements_whose_whole_bounds_lie_inside(tab):
    view = tab.view
    _band(view, _vp(view, -43.18, 27.94), _vp(view, -33.02, 17.78))
    assert tab.selected_graphics() == [0] and tab._selected_graphic == 0
    assert _no_grab() and not _band_active(view)
    # A band cutting through the element selects nothing.
    _band(view, _vp(view, -43.18, 27.94), _vp(view, -38.1, 17.78))
    assert tab.selected_graphics() == []


def test_selection_mode_draws_a_band_without_shift(tab):
    tab.view.selection_mode = True
    _band(tab.view, _vp(tab.view, 27.94, 27.94), _vp(tab.view, 38.1, 17.78), modifiers=NO_MOD)
    assert tab.selected_graphics() == [1]


def test_ctrl_band_adds_to_the_selection(tab):
    view = tab.view
    _band(view, _vp(view, -43.18, 27.94), _vp(view, -33.02, 17.78))
    _band(view, _vp(view, 27.94, 27.94), _vp(view, 38.1, 17.78), modifiers=CTRL_SHIFT)
    assert tab.selected_graphics() == [0, 1] and tab._selected_graphic == 1
    _band(view, _vp(view, 27.94, 27.94), _vp(view, 38.1, 17.78))
    assert tab.selected_graphics() == [1], 'without Ctrl the band replaces'


def test_pins_in_the_band_win_over_elements(tab):
    view = tab.view
    _band(view, _vp(view, -60.0, 45.0), _vp(view, 60.0, -45.0))
    assert tab._selected_pins == {'1', '2', '3', '4', '5'}
    assert tab.selected_graphics() == []


def test_escape_drops_the_band_without_selecting(tab):
    view = tab.view
    tab._select_graphic(1)
    _mouse(view, QEvent.Type.MouseButtonPress, _vp(view, -43.18, 27.94), modifiers=SHIFT)
    _mouse(view, QEvent.Type.MouseMove, _vp(view, -33.02, 17.78), button=Qt.MouseButton.NoButton, modifiers=SHIFT)
    assert _band_active(view)
    _key(view)
    assert not _band_active(view) and _no_grab()
    assert tab.selected_graphics() == [1], 'the Escape cancelled only the band'
    _mouse(view, QEvent.Type.MouseButtonRelease, _vp(view, -33.02, 17.78), modifiers=SHIFT)
    assert tab.selected_graphics() == [1], 'the late release selects nothing'


def test_ctrl_click_toggles_an_element(tab):
    tab._select_graphic(0)
    tab._toggle_graphic(1)
    assert tab.selected_graphics() == [0, 1] and tab._selected_graphic == 1
    tab._toggle_graphic(0)
    assert tab.selected_graphics() == [1] and tab._selected_graphic == 1
    tab._toggle_graphic(1)
    assert tab.selected_graphics() == [] and tab._selected_graphic is None


def test_elements_and_pins_stay_exclusive(tab):
    tab._select_graphic(0)
    tab._select_pins({'1'})
    assert tab.selected_graphics() == [] and tab._selected_pins == {'1'}
    tab._select_graphic(1)
    assert tab._selected_pins == set() and tab._selected_pin is None


# ── Background click (new) ─────────────────────────────────────────────────

def test_a_background_click_clears_elements_and_pins(tab):
    view = tab.view
    pos = _background(view)
    tab._select_graphic(0)
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos)
    assert tab.selected_graphics() == [] and _no_grab()
    tab._select_pins({'2', '3'})
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos)
    assert tab._selected_pins == set() and tab._selected_pin is None


def test_a_background_drag_pans_and_keeps_the_selection(tab):
    view = tab.view
    pos = _background(view)
    tab._select_graphic(0)
    before = view.mapToScene(QPoint(0, 0))
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    _mouse(view, QEvent.Type.MouseMove, pos + QPoint(40, 30))
    _mouse(view, QEvent.Type.MouseButtonRelease, pos + QPoint(40, 30))
    assert view.mapToScene(QPoint(0, 0)) != before, 'the view panned'
    assert tab.selected_graphics() == [0]


def test_a_middle_click_on_the_background_keeps_the_selection(tab):
    view = tab.view
    pos = _background(view)
    tab._select_graphic(0)
    _mouse(view, QEvent.Type.MouseButtonPress, pos, button=Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos, button=Qt.MouseButton.MiddleButton)
    assert tab.selected_graphics() == [0]


# ── Escape (new) ───────────────────────────────────────────────────────────

def test_escape_with_nothing_to_cancel_clears_elements_and_pins(tab):
    tab._select_graphic(0)
    _key(tab.view)
    assert tab.selected_graphics() == []
    tab._select_pins({'4'})
    _key(tab.view)
    assert tab._selected_pins == set()


def test_another_key_keeps_the_selection(tab):
    tab._select_graphic(0)
    _key(tab.view, Qt.Key.Key_A)
    assert tab.selected_graphics() == [0]


def test_escape_cancelling_a_placement_keeps_the_selection(tab):
    tab._select_graphic(0)
    tab.view.placing = True
    _key(tab.view)
    assert tab.view.placing is False and tab.selected_graphics() == [0]


def test_escape_cancelling_a_click_tool_keeps_the_selection(tab):
    tab._select_graphic(0)
    assert tab.start_shape('rectangle')
    kept = tab.selected_graphics()
    _key(tab.view)
    assert not tab.view.wiring and tab.selected_graphics() == kept


def test_escape_cancelling_a_pan_keeps_the_selection(tab):
    view = tab.view
    tab._select_graphic(0)
    _mouse(view, QEvent.Type.MouseButtonPress, _background(view))
    assert view.camera.pan_active
    _key(view)
    assert not view.camera.pan_active and tab.selected_graphics() == [0] and _no_grab()


def test_escape_cancelling_a_drag_keeps_the_selection(tab):
    view = tab.view
    tab._select_graphic(1)
    assert view.controller.press(('graphic', 0), QPointF(-38.1 * SCENE_UNITS_PER_MM, -22.86 * SCENE_UNITS_PER_MM))
    kept = tab.selected_graphics()
    _key(view)
    assert view.gesture is None and tab.selected_graphics() == kept and kept


# ── Clipboard (new) ────────────────────────────────────────────────────────

def test_clear_clipboard_empties_it(tab):
    tab._select_graphic(0)
    assert tab.copy_selected() == 1
    tab.clear_clipboard()
    assert tab.paste() is False and len(tab.graphics()) == 2


def test_without_clear_the_clipboard_survives(tab):
    tab._select_graphic(0)
    assert tab.copy_selected() == 1
    assert tab.paste() and len(tab.graphics()) == 3


# ── Review findings of phase D (2026-10-18) ───────────────────────────────

@pytest.fixture
def flat(qapp):
    """A horizontal line, a vertical line and a text, all below the body, far from every pin."""
    widget = SymbolTab()
    widget.resize(1200, 800)
    widget.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)],
                    pad_ids=['1', '2', '3', '4', '5'])
    for a, b in (((-40.64, -30.48), (-30.48, -30.48)), ((30.48, -35.56), (30.48, -25.4))):
        assert widget.start_shape('line')
        widget.add_shape_point(*a)
        widget.add_shape_point(*b)
        assert widget.finish_shape(True)
    widget._text_prompt = lambda *args: ('IN+', 1.27)
    assert widget.start_shape('text')
    widget.add_shape_point(-2.54, -40.64)
    widget._select_graphic(None)
    assert len(widget.graphics()) == 3
    yield widget
    widget.deleteLater()


def test_m1_a_band_selects_a_horizontal_line(flat):
    _band(flat.view, _vp(flat.view, -43.18, -27.94), _vp(flat.view, -27.94, -33.02))
    assert flat.selected_graphics() == [0]


def test_m1_a_band_selects_a_vertical_line(flat):
    _band(flat.view, _vp(flat.view, 27.94, -22.86), _vp(flat.view, 33.02, -38.1))
    assert flat.selected_graphics() == [1]


def test_m1_a_band_selects_a_text(flat):
    _band(flat.view, _vp(flat.view, -5.08, -35.56), _vp(flat.view, 12.7, -45.72))
    assert flat.selected_graphics() == [2]


def test_m1_a_band_cutting_a_flat_line_selects_nothing(flat):
    _band(flat.view, _vp(flat.view, -43.18, -27.94), _vp(flat.view, -35.56, -33.02))
    assert flat.selected_graphics() == []


def _body_point(view):
    """A viewport point over the untagged body outline item only."""
    pos = _vp(view, 0.0, 0.0)
    items = view.items(pos)
    assert items and all(item.data(0) is None for item in items)
    return pos


def test_m2_a_click_inside_the_body_clears_the_selection(tab):
    view = tab.view
    pos = _body_point(view)
    tab._select_graphic(0)
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    assert not view.camera.pan_active, 'a press on the body does not pan (as before)'
    _mouse(view, QEvent.Type.MouseButtonRelease, pos)
    assert tab.selected_graphics() == []


def test_m2_a_drag_inside_the_body_keeps_the_selection(tab):
    view = tab.view
    pos = _body_point(view)
    tab._select_graphic(0)
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    _mouse(view, QEvent.Type.MouseMove, pos + QPoint(40, 30))
    _mouse(view, QEvent.Type.MouseButtonRelease, pos + QPoint(40, 30))
    assert tab.selected_graphics() == [0]


def test_m2_a_click_on_an_element_does_not_clear(tab):
    view = tab.view
    tab._select_graphic(1)
    pos = _vp(view, -40.64, 22.86)
    assert any(item.data(0) == ('graphic', 0) for item in view.items(pos))
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos)
    assert tab.selected_graphics() == [0], 'the click selected the element, it did not clear'


def test_m3_a_ctrl_click_on_the_background_keeps_the_selection(tab):
    view = tab.view
    pos = _background(view)
    tab._select_graphic(0)
    ctrl = Qt.KeyboardModifier.ControlModifier
    _mouse(view, QEvent.Type.MouseButtonPress, pos, modifiers=ctrl)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos, modifiers=ctrl)
    assert tab.selected_graphics() == [0]
    # Twin: the same click without Ctrl clears.
    _mouse(view, QEvent.Type.MouseButtonPress, pos)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos)
    assert tab.selected_graphics() == []


def test_l2_a_shift_click_without_drag_keeps_the_selection(tab):
    view = tab.view
    pos = _background(view)
    tab._select_graphic(0)
    emitted = []
    view.region_selected.connect(lambda *args: emitted.append(args))
    _mouse(view, QEvent.Type.MouseButtonPress, pos, modifiers=SHIFT)
    _mouse(view, QEvent.Type.MouseButtonRelease, pos, modifiers=SHIFT)
    assert emitted == [] and tab.selected_graphics() == [0]
    assert not _band_active(view) and _no_grab()
    # Twin: a real Shift+drag over nothing replaces the selection with nothing.
    _band(view, pos, pos + QPoint(30, 30))
    assert len(emitted) == 1 and tab.selected_graphics() == []


def test_l3_right_and_middle_presses_are_swallowed_while_the_band_grows(tab, monkeypatch):
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu
    # A menu opened by the twin below must not block the test.
    monkeypatch.setattr(QMenu, 'exec', lambda *args: None)
    view = tab.view
    asked = []
    view.graphic_context_requested.connect(lambda *args: asked.append(args))
    view.pin_context_requested.connect(lambda *args: asked.append(args))
    tab._select_graphic(1)
    start, end = _vp(view, -43.18, 27.94), _vp(view, -33.02, 17.78)
    _mouse(view, QEvent.Type.MouseButtonPress, start, modifiers=SHIFT)
    _mouse(view, QEvent.Type.MouseMove, end, button=Qt.MouseButton.NoButton, modifiers=SHIFT)
    on_element = _vp(view, -40.64, 22.86)
    assert any(item.data(0) == ('graphic', 0) for item in view.items(on_element))
    _mouse(view, QEvent.Type.MouseButtonPress, on_element, button=Qt.MouseButton.RightButton)
    view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, on_element,
                                            view.viewport().mapToGlobal(on_element)))
    _mouse(view, QEvent.Type.MouseButtonRelease, on_element, button=Qt.MouseButton.RightButton)
    _mouse(view, QEvent.Type.MouseButtonPress, on_element, button=Qt.MouseButton.MiddleButton)
    assert asked == [] and not view.camera.pan_active
    assert _band_active(view) and tab.selected_graphics() == [1], 'nothing changed mid-band'
    _mouse(view, QEvent.Type.MouseButtonRelease, end, modifiers=SHIFT)
    assert tab.selected_graphics() == [0], 'the band still selects on its left release'
    # Twin: without a band the context menu of the element is asked (the
    # redraw after the selection may have moved the camera: map again).
    on_element = _vp(view, -40.64, 22.86)
    assert any(item.data(0) == ("graphic", 0) for item in view.items(on_element))
    view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, on_element,
                                            view.viewport().mapToGlobal(on_element)))
    assert asked and asked[0][0] == 0


def test_l6_clear_selection_with_nothing_selected_does_not_redraw(tab, monkeypatch):
    draws = []
    original = tab._draw
    monkeypatch.setattr(tab, '_draw', lambda *args, **kwargs: (draws.append(1), original(*args, **kwargs)))
    tab.clear_selection()
    assert draws == []
    # Twin: with a selection the clear redraws.
    tab._element_selection.select(0)
    tab.clear_selection()
    assert draws and tab.selected_graphics() == []


# ── Stale element indices (phase D, 2026-10-18) ───────────────────────────

def _new_rectangle(tab, x):
    tab.start_shape('rectangle')
    tab.add_shape_point(x, -20.32)
    tab.add_shape_point(x + 5.08, -15.24)
    tab.view.collector.cancel()


def test_a_single_delete_clears_the_whole_element_selection(tab):
    tab._select_graphic(0)
    tab._select_graphic(1, additive=True)
    assert tab._edit_graphic(0, 'delete')
    assert tab.selected_graphics() == [] and not tab._element_selection
    # A new element created at the freed index is not selected silently.
    _new_rectangle(tab, 0.0)
    tab._select_graphic(None)
    _new_rectangle(tab, 12.7)
    tab._select_graphic(None)
    assert len(tab.graphics()) == 3 and tab.selected_graphics() == []


def test_a_refused_delete_keeps_the_selection(tab, monkeypatch):
    tab._select_graphic(0)
    tab._select_graphic(1, additive=True)
    monkeypatch.setattr(tab, '_accept_geometry', lambda rows: False)
    assert tab._edit_graphic(0, 'delete') is False
    assert tab.selected_graphics() == [0, 1]


def test_undo_and_redo_clear_the_element_selection(tab):
    tab._select_graphic(1)
    tab.undo()
    assert len(tab.graphics()) == 1 and not tab._element_selection
    tab.redo()
    assert len(tab.graphics()) == 2 and tab.selected_graphics() == [], 'redo does not reselect index 1'
    tab._select_graphic(0)
    assert tab._edit_graphic(0, 'delete')
    tab.undo()
    assert len(tab.graphics()) == 2 and tab.selected_graphics() == [], 'the restored element is not selected'


def test_an_edit_that_keeps_the_elements_keeps_the_selection(tab):
    tab._select_graphic(0)
    tab._select_graphic(1, additive=True)
    assert tab._edit_graphic(1, 'rotate', 90)
    assert tab.selected_graphics() == [0, 1]
    assert tab._edit_selected_graphics('mirror')
    assert tab.selected_graphics() == [0, 1]


def test_undo_keeps_the_pin_selection(tab):
    """@brief Pins are selected by number, not by index: an undo cannot make the selection name another pin."""
    tab._select_pins({'2'}, '2')
    tab.undo()
    assert tab._selected_pins == {'2'} and tab._selected_pin == '2'
