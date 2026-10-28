"""@brief Fixes of the canvas review of 2026-10-27 (findings D1-D5), each with its negative twin.

D1 symbol: an armed wire tool and its points are abandoned by undo/redo,
«Reimposta disposizione» and a project switch (`set_data`), as the
footprint preview abandons its draw tool; a wire anchored to a pin of the
old draft is never committed into the new one. Negative: an idle editor is
untouched, and the tool can be armed and used afterwards.

D2 footprint: auto-fit goes off before a drag begins (symbol rule adopted,
plan docs/canvas-gestures-plan.md §1c D13), so the drop never refits the
camera under the pointer. Negative: a refused press leaves auto-fit on.

D3 symbol: the Reference/Value texts have the footprint's «Posizione
predefinita» context menu (plan docs/canvas-menus-plan.md C-M6); the entry
is greyed out at the default, choosing it is one undo step, the menu is
released. Negative: a right click on the empty canvas opens nothing.

D4 symbol: an armed glyph placement is cancelled by Hide and by
`cancel_interaction` (plan docs/canvas-clicktools-plan.md C14); the next
click places nothing. Negative: without the hide the click places.

D5 symbol: an empty, non-additive rubber band clears the selected pins
(plan docs/canvas-selection-plan.md §2). Negative: an additive band keeps
them.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt            # noqa: E402
from PyQt6.QtGui import QContextMenuEvent, QMouseEvent                   # noqa: E402
from PyQt6.QtTest import QTest                                           # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu                          # noqa: E402

from kicadgen.gui import canvas_menus                                    # noqa: E402
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab        # noqa: E402

NO_MOD = Qt.KeyboardModifier.NoModifier
LEFT = Qt.MouseButton.LeftButton

#: Scene scale of the footprint preview (px per mm).
FP_PX = 20.0


# ── Symbol fixtures and helpers ───────────────────────────────────────

@pytest.fixture
def tab(qapp):
    """A symbol editor with five pins and one history entry to undo."""
    widget = SymbolTab()
    widget.resize(1200, 800)
    widget.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)],
                    pad_ids=['1', '2', '3', '4', '5'])
    yield widget
    widget.deleteLater()


def _scene(x_mm, y_mm):
    """Scene point of a symbol point (mm, Y up)."""
    return QPointF(x_mm * SCENE_UNITS_PER_MM, -y_mm * SCENE_UNITS_PER_MM)


def _first_pin_end(tab):
    """Scene point of the inner end of the first pin (a wire anchor)."""
    from kicadgen.generators.symbol_glyphs import pin_inner_ends
    ends = pin_inner_ends(tab._symbol)
    x, y = next(iter(ends.values()))
    return _scene(x, y)


def _arm_wire_with_one_point(tab):
    """Arm the wire tool and click the first pin's end: one anchored point."""
    tab.start_wire()
    assert tab.view.wiring
    assert tab._collector.click(_first_pin_end(tab))
    assert len(tab._collector.points) == 1


def _make_history(tab):
    """One draft change, so that `undo` has a snapshot to restore."""
    pin = tab._symbol.pins[0]
    before = tab._history_index
    assert tab.place_pin(pin.pin.number, pin.x, pin.y + 2.54)
    assert tab._history_index == before + 1


def _assert_tool_idle_and_harmless(tab):
    """The tool is disarmed, holds no point, and a later click commits nothing."""
    assert not tab.view.wiring
    assert tab._collector.points == []
    graphics_before = len(tab.graphics())
    # An idle collector refuses the click; `finish_wire` then has nothing to commit.
    assert not tab._collector.click(QPointF(100.0, 100.0))
    tab.finish_wire()
    assert len(tab.graphics()) == graphics_before


def _press(view, pos, button=LEFT, modifiers=NO_MOD):
    """A left press delivered straight to the view (no QTest: nothing to show)."""
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos),
                        view.viewport().mapToGlobal(QPointF(pos)), button, button, modifiers)
    view.mousePressEvent(event)


def _context_click(view, pos):
    """A mouse context-menu event at a viewport position; returns whether the view accepted it."""
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, view.viewport().mapToGlobal(pos))
    view.contextMenuEvent(event)
    return event.isAccepted()


def _reference_pos(tab):
    """Viewport position over the Reference text."""
    item, _square = tab._property_items['Reference']
    return tab.view.mapFromScene(item.sceneBoundingRect().center())


# ── D1: a click tool does not survive undo, reset or a project switch ──

def test_undo_disarms_the_wire_tool_and_drops_its_points(tab):
    _make_history(tab)
    _arm_wire_with_one_point(tab)
    tab.undo()
    _assert_tool_idle_and_harmless(tab)


def test_redo_disarms_the_wire_tool(tab):
    _make_history(tab)
    tab.undo()
    _arm_wire_with_one_point(tab)
    tab.redo()
    _assert_tool_idle_and_harmless(tab)


def test_undo_with_nothing_to_undo_still_disarms_the_tool_like_the_footprint(tab):
    # The footprint's `_step_footprint_history` cancels the draw tool before
    # looking at the stack: Ctrl+Z always leaves the tab idle.
    assert tab._history_index == 0
    _arm_wire_with_one_point(tab)
    tab.undo()
    _assert_tool_idle_and_harmless(tab)


def test_reset_layout_disarms_the_wire_tool(tab):
    _arm_wire_with_one_point(tab)
    tab.reset_layout()
    _assert_tool_idle_and_harmless(tab)


def test_set_data_of_another_project_never_commits_the_old_wire(tab):
    _arm_wire_with_one_point(tab)
    # Another project: the pin the point was anchored to no longer exists.
    tab.set_data('Other', [{'pin': '1', 'name': 'A'}], pad_ids=['1'])
    assert len(tab._symbol.pins) == 1
    assert not tab.view.wiring
    assert tab._collector.points == []
    # The scenario of the review: a click and a finish after the switch
    # must not insert a wire anchored to the old project's pin.
    assert not tab._collector.click(QPointF(100.0, 100.0))
    tab.finish_wire()
    assert [g for g in tab.graphics() if g.get('key') == 'wire'] == []


def test_undo_on_an_idle_editor_does_not_tell_the_host_a_tool_ended(tab, monkeypatch):
    """Negative twin: with no tool armed the disarm is a no-op for the host."""
    _make_history(tab)
    calls = []
    monkeypatch.setattr(tab, 'tool_disarmed', lambda spec: calls.append(spec))
    monkeypatch.setattr(tab, 'tool_finished', lambda spec, points, commit: calls.append((spec, commit)))
    tab.undo()
    assert calls == []
    assert not tab.view.wiring


def test_the_wire_tool_still_works_after_an_undo(tab):
    """Negative twin: disarming on undo does not break a tool armed afterwards."""
    _make_history(tab)
    tab.undo()
    before = len(tab.graphics())
    tab.start_wire()
    assert tab._collector.click(_first_pin_end(tab))
    assert tab._collector.click(_first_pin_end(tab) + QPointF(0.0, -5 * 2.54 * SCENE_UNITS_PER_MM))
    tab.finish_wire()
    assert len(tab.graphics()) == before + 1


# ── D3: «Posizione predefinita» on the symbol's property texts ─────────

@pytest.fixture
def menu_spy(monkeypatch):
    """Replace the modal menu with a recorder that returns the chosen key."""
    shown = []
    answer = {'key': None}

    def fake_exec(menu, keys, global_pos):
        shown.append({'entries': [(keys[action], action.text(), action.isEnabled())
                                  for action in menu.actions() if action in keys]})
        menu.deleteLater()
        return answer['key']

    monkeypatch.setattr(canvas_menus, 'exec_element_menu', fake_exec)
    return shown, answer


def test_right_click_on_a_moved_reference_offers_and_applies_the_default_position(tab, menu_spy):
    shown, answer = menu_spy
    assert tab.move_property('Reference', 10.16, 20.32)
    assert 'Reference' in tab._chosen_properties(tab.draft_rows())
    history_before = tab._history_index
    answer['key'] = 'default_position'
    assert _context_click(tab.view, _reference_pos(tab))
    assert len(shown) == 1
    assert shown[0]['entries'] == [('default_position', tab.tx('Posizione predefinita'), True)]
    # Reset: the key is gone, the property follows the default again.
    assert 'Reference' not in tab._chosen_properties(tab.draft_rows())
    # One undo step, and undo brings the moved position back.
    assert tab._history_index == history_before + 1
    tab.undo()
    assert tab._chosen_properties(tab.draft_rows())['Reference'] == (10.16, 20.32)


def test_the_entry_is_greyed_out_while_the_property_sits_at_its_default(tab, menu_spy):
    shown, _answer = menu_spy
    assert 'Reference' not in tab._chosen_properties(tab.draft_rows())
    assert _context_click(tab.view, _reference_pos(tab))
    assert shown[0]['entries'] == [('default_position', tab.tx('Posizione predefinita'), False)]


def test_resetting_the_reference_keeps_the_value_choice(tab):
    assert tab.move_property('Reference', 10.16, 20.32)
    assert tab.move_property('Value', -10.16, -20.32)
    assert tab.reset_property('Reference')
    chosen = tab._chosen_properties(tab.draft_rows())
    assert 'Reference' not in chosen
    assert chosen['Value'] == (-10.16, -20.32)
    # Already at the default: nothing to do, no history entry.
    index = tab._history_index
    assert not tab.reset_property('Reference')
    assert tab._history_index == index


def test_right_click_on_the_empty_canvas_opens_no_menu(tab, menu_spy):
    """Negative twin: nothing under the pointer, no property menu."""
    shown, _answer = menu_spy
    far = tab.view.mapFromScene(QPointF(-100000.0, -100000.0))
    assert not _context_click(tab.view, far)
    assert shown == []


def test_the_property_menu_is_released_after_it_closes(tab, qapp, monkeypatch):
    monkeypatch.setattr(QMenu, 'exec', lambda self, *args: None)
    before = len(tab.findChildren(QMenu))
    for _ in range(3):
        tab._property_context_menu('Reference', QPoint(0, 0))
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(tab.findChildren(QMenu)) == before


# ── D4: a glyph placement does not survive Hide ────────────────────────

def _start_placing(tab, qapp):
    tab.show()
    qapp.processEvents()
    assert tab.glyph_combo.count() > 0
    tab.glyph_combo.setCurrentIndex(0)
    tab._start_placing()
    assert tab.view.placing


def test_hiding_the_view_cancels_the_placement_and_the_next_click_places_nothing(tab, qapp):
    _start_placing(tab, qapp)
    before = len(tab.graphics())
    tab.view.hide()
    qapp.processEvents()
    assert not tab.view.placing
    assert tab.status.text() == ''
    tab.view.show()
    qapp.processEvents()
    _press(tab.view, tab.view.mapFromScene(_scene(0.0, 0.0)))
    assert len(tab.graphics()) == before


def test_cancel_interaction_cancels_the_placement(tab, qapp):
    _start_placing(tab, qapp)
    tab.view.cancel_interaction()
    assert not tab.view.placing
    assert tab.status.text() == ''


def test_arming_the_wire_tool_cancels_the_placement(tab, qapp):
    """One armed tool at a time (plan C11), now also when the glyph was armed first."""
    _start_placing(tab, qapp)
    tab.start_wire()
    assert tab.view.wiring
    assert not tab.view.placing


def test_without_a_hide_the_click_places_the_glyph(tab, qapp):
    """Negative twin: the placement is still armed and the click inserts the glyph."""
    _start_placing(tab, qapp)
    before = len(tab.graphics())
    _press(tab.view, tab.view.mapFromScene(_scene(0.0, 0.0)))
    assert not tab.view.placing
    assert len(tab.graphics()) == before + 1


# ── D5: an empty band clears the selected pins ─────────────────────────

def _selected_table_rows(tab):
    return {index.row() for index in tab.table.selectionModel().selectedRows()}


def test_an_empty_plain_band_clears_the_selected_pins(tab):
    tab._select_pins({'1', '2'})
    assert tab._selected_pins == {'1', '2'}
    assert _selected_table_rows(tab) == {0, 1}
    tab._select_region(QRectF(-2000.0, -2000.0, 10.0, 10.0), False)
    assert tab._selected_pins == set()
    assert tab._selected_pin is None
    assert _selected_table_rows(tab) == set()
    assert tab.selected_graphics() == []


def test_an_empty_additive_band_keeps_the_selected_pins(tab):
    """Negative twin: Ctrl held means add, so nothing is dropped."""
    tab._select_pins({'1', '2'})
    tab._select_region(QRectF(-2000.0, -2000.0, 10.0, 10.0), True)
    assert tab._selected_pins == {'1', '2'}
    assert _selected_table_rows(tab) == {0, 1}


def test_a_band_around_pins_still_selects_them(tab):
    """A band that does enclose pins selects them, as before."""
    pin = tab._symbol.pins[0]
    centre = _scene(pin.x, pin.y)
    tab._select_region(QRectF(centre.x() - 5, centre.y() - 5, 10, 10), False)
    assert tab._selected_pins == {pin.pin.number}


# ── D2: the footprint drag switches auto-fit off at the press ──────────

@pytest.fixture
def shown(window, qapp, monkeypatch):
    """The preview tab shown with three drawings, auto-fit still on."""
    pytest.importorskip("pyvistaqt")
    from kicadgen.core.footprint import validate_footprint_drawings
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    window.chk_grid.setChecked(False)

    def record(kind, points):
        return {"kind": kind, "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in points]}

    window.footprint_drawings = validate_footprint_drawings([
        record("line", ((0, -10), (5, -10))),
        record("rectangle", ((8, -11), (10, -9))),
        record("circle", ((15, -10), (16, -10)))])
    window._draw_preview()
    assert window.preview_view.auto_fit is True
    return window


def _fp_vp(window, x_mm, y_mm):
    return window.preview_view.mapFromScene(QPointF(x_mm * FP_PX, y_mm * FP_PX))


def _fp_drag(window, start, end):
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, LEFT, NO_MOD, start)
    QTest.mouseMove(viewport, (start + end) / 2)
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, LEFT, NO_MOD, end)


def test_footprint_drag_with_auto_fit_on_keeps_the_scale_at_the_drop(shown):
    view = shown.preview_view
    scale_before = view.transform().m11()
    corner_before = view.mapToScene(QPoint(0, 0))
    # The line is dragged far beyond the pads: with auto-fit still on the
    # drop's redraw would refit the camera to the new bounds.
    _fp_drag(shown, _fp_vp(shown, 2.5, -10), _fp_vp(shown, 60, 40))
    assert shown.footprint_drawings[0]["points"] != [[0, -10], [5, -10]], "the drag moved the line"
    assert view.transform().m11() == pytest.approx(scale_before)
    assert view.mapToScene(QPoint(0, 0)) == corner_before
    assert view.auto_fit is False


def test_footprint_press_on_a_drawing_switches_auto_fit_off_before_the_host_begins(shown, monkeypatch):
    seen = []
    original = shown.gesture_begin

    def spying(target, point):
        seen.append(shown.preview_view.auto_fit)
        return original(target, point)

    monkeypatch.setattr(shown, "gesture_begin", spying)
    viewport = shown.preview_view.viewport()
    QTest.mousePress(viewport, LEFT, NO_MOD, _fp_vp(shown, 2.5, -10))
    assert seen == [False], "auto-fit was already off when the host began the drag"
    assert shown.preview_view.gestures.active
    QTest.mouseRelease(viewport, LEFT, NO_MOD, _fp_vp(shown, 2.5, -10))


def test_footprint_refused_press_leaves_auto_fit_on(shown, monkeypatch):
    """Negative twin: a press the host refuses changes nothing."""
    monkeypatch.setattr(shown, "gesture_begin", lambda target, point: None)
    viewport = shown.preview_view.viewport()
    QTest.mousePress(viewport, LEFT, NO_MOD, _fp_vp(shown, 2.5, -10))
    assert shown.preview_view.auto_fit is True
    assert not shown.preview_view.gestures.active
    QTest.mouseRelease(viewport, LEFT, NO_MOD, _fp_vp(shown, 2.5, -10))
    assert shown.preview_view.auto_fit is True
