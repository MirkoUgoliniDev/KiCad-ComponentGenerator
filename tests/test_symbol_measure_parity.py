"""@brief Symbol editor behaviour the measuring tool must not change (plan docs/canvas-measure-plan.md §6).

Written BEFORE the measuring tool against the code of 2026-10-31: the
existing toolbar actions keep their relative order, a shape tool still
commits through real events and stays armed, a mouse move with a tool armed
still redraws nothing, the wire tool still releases on Escape, and Escape
with nothing armed still clears the selection.
"""
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPointF, Qt                    # noqa: E402
from PyQt6.QtGui import QContextMenuEvent                # noqa: E402
from PyQt6.QtTest import QTest                          # noqa: E402
from PyQt6.QtWidgets import QApplication                # noqa: E402

from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab  # noqa: E402

ROWS5 = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)]
S = SCENE_UNITS_PER_MM


def _settle(qapp, times=10):
    for _ in range(times):
        qapp.processEvents()


@pytest.fixture
def tab(qapp, tmp_path):
    """@brief A shown five-pin symbol tab with a recovery file under `tmp_path`."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    tab.set_recovery_path(tmp_path / '.symbol-draft.json')
    tab.show()
    _settle(qapp)
    yield tab
    tab.deleteLater()


def _vp(tab, x_mm, y_mm):
    return tab.view.mapFromScene(QPointF(x_mm * S, -y_mm * S))


def _click(tab, qapp, x_mm, y_mm):
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     _vp(tab, x_mm, y_mm))
    qapp.processEvents()


def test_the_existing_toolbar_actions_keep_their_order(tab):
    """New actions may be inserted, the existing ones keep their relative order."""
    toolbar_buttons = [b for b, _text in tab._icon_buttons]
    expected = [tab.table_toggle, tab.grid_check, tab.undo_button, tab.redo_button, tab.cancel_button,
                tab.apply_button, tab.selection_button, tab.wire_button, tab.snap_button]
    positions = [toolbar_buttons.index(b) for b in expected]
    assert positions == sorted(positions)


def test_a_rectangle_commits_through_events_and_the_tool_stays_armed(tab, qapp):
    before = len(tab.draft_rows()) if hasattr(tab, 'draft_rows') else None
    assert tab.start_shape('rectangle')
    _click(tab, qapp, -2.54, -2.54)
    _click(tab, qapp, 2.54, 2.54)
    assert tab.shape_tool() == 'rectangle'
    graphics = [g for g in tab._graphics() if g.get('kind') == 'rectangle' or g.get('type') == 'rectangle'
                or 'rectangle' in str(g)]
    assert graphics, "the rectangle was committed"
    assert before is not None


def test_a_move_with_a_tool_armed_redraws_nothing(tab, qapp, monkeypatch):
    calls = []
    original = tab._draw
    monkeypatch.setattr(tab, '_draw', lambda *a, **k: (calls.append(1), original(*a, **k)))
    assert tab.start_shape('rectangle')
    _click(tab, qapp, -2.54, -2.54)
    calls.clear()
    for x in (0.0, 1.0, 2.0):
        QTest.mouseMove(tab.view.viewport(), _vp(tab, x, 1.0))
        qapp.processEvents()
    assert calls == []


def test_escape_releases_the_wire_and_escape_idle_clears_the_selection(tab, qapp):
    tab.start_wire()
    assert tab._collector.armed
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not tab._collector.armed
    cleared = []
    tab.view.clear_selection_requested.connect(lambda: cleared.append(1))
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert cleared == [1]


# ── The measuring tool in the symbol editor (plan §6) ───────────────────────

from PyQt6.QtCore import QPoint                                     # noqa: E402

from kicadgen.gui import canvas_measure as cm                       # noqa: E402
from kicadgen.gui import canvas_menus                               # noqa: E402


@pytest.fixture
def sym(qapp, tmp_path):
    """@brief A shown five-pin symbol tab, camera at 2 view px per scene unit (20 px per mm), auto-fit off."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    tab.set_recovery_path(tmp_path / '.symbol-draft.json')
    tab.show()
    _settle(qapp)
    tab.view.auto_fit = False
    tab.view.resetTransform()
    tab.view.scale(2.0, 2.0)
    tab.view.centerOn(0, 0)
    _settle(qapp)
    yield tab
    tab.deleteLater()


def _pins(tab):
    return {p.pin.number: (p.x, p.y) for p in tab._symbol.pins}


def _sym_click(tab, qapp, x_mm, y_mm, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, modifier, _vp(tab, x_mm, y_mm))
    qapp.processEvents()


def _free_diagonal(tab, qapp):
    """A two-click true-distance measure between two free points (Shift), from (0.37, 0.53) to (1.37, 1.13) mm."""
    _sym_click(tab, qapp, 0.37, 0.53, Qt.KeyboardModifier.ShiftModifier)
    _sym_click(tab, qapp, 1.37, 1.13, Qt.KeyboardModifier.ShiftModifier)


def _sym_move(tab, qapp, x_mm, y_mm):
    pos = _vp(tab, x_mm, y_mm)
    QTest.mouseMove(tab.view.viewport(), pos + QPoint(1, 1))
    QTest.mouseMove(tab.view.viewport(), pos)
    qapp.processEvents()


def _two_columns(tab):
    """A left pin and a right pin on different rows (no dominant delta), and two pins of one column."""
    pins = _pins(tab)
    left = sorted((n for n in pins if pins[n][0] < 0), key=lambda n: -pins[n][1])
    right = sorted((n for n in pins if pins[n][0] > 0), key=lambda n: -pins[n][1])
    return pins, left, right


def test_symbol_a_click_on_the_pin_end_area_takes_the_connection_point(sym, qapp):
    pins, left, _right = _two_columns(sym)
    x, y = pins[left[0]]
    assert sym.toggle_measure() and sym.measure_button.defaultAction().isChecked()
    _sym_move(sym, qapp, x + 0.4, y - 0.3)            # inside the 0.635 mm hit area
    hover = sym._measure._hover
    assert (hover.kind, hover.label) == (cm.KIND_CENTRE, left[0])
    assert sym._measure.tag(hover) == f"Pin {left[0]} · connessione"
    assert sym._shape_tool is None and sym._shape_points == [] and sym._wire_points == []
    # Negative: away from every target the point is free, never a grid node.
    _sym_move(sym, qapp, 0.37, 0.53)
    assert sym._measure._hover.kind == cm.KIND_FREE


def test_symbol_vertical_dimension_between_two_pins_of_a_column(sym, qapp):
    pins, left, _right = _two_columns(sym)
    (x1, y1), (x2, y2) = pins[left[0]], pins[left[1]]
    sym.toggle_measure()
    _sym_click(sym, qapp, x1, y1)
    _sym_click(sym, qapp, x2, y2)
    assert len(sym._collector.points) == 2, "a linear dimension waits for its placement"
    _sym_move(sym, qapp, x1 - 3.0, (y1 + y2) / 2)
    _sym_click(sym, qapp, x1 - 3.0, (y1 + y2) / 2)
    (m,) = sym._measure.overlay.measurements
    assert (m.axis, m.distance_mm) == ("y", pytest.approx(abs(y1 - y2)))
    assert len(m.extensions) == 2 and sym._measure.armed
    assert sym.clear_measures_button.defaultAction().isEnabled()


def test_symbol_horizontal_dimension_through_the_vertical_of_a_pin(sym, qapp):
    """First pin on the left, move right across the vertical of a right pin: guide, click, place."""
    pins, left, right = _two_columns(sym)
    (x1, y1), (xr, yr) = pins[left[0]], pins[right[0]]
    sym.toggle_measure()
    _sym_click(sym, qapp, x1, y1)
    target_y = yr - 1.27 if abs(yr - 1.27 - y1) > 0.5 else yr + 1.27
    _sym_move(sym, qapp, xr + 0.2, target_y)
    hover = sym._measure._hover
    assert (hover.kind, hover.axis) == (cm.KIND_ALIGNED, "x")
    assert hover.x == pytest.approx(xr * SCENE_UNITS_PER_MM)
    _sym_click(sym, qapp, xr + 0.2, target_y)
    _sym_click(sym, qapp, 0.0, y1 + 5.0)
    (m,) = sym._measure.overlay.measurements
    assert (m.axis, m.distance_mm) == ("x", pytest.approx(abs(xr - x1)))


def test_symbol_free_diagonal_and_pin_to_body_edge(sym, qapp):
    pins, left, right = _two_columns(sym)
    x1, y1 = pins[left[0]]
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    (m,) = sym._measure.overlay.measurements
    assert m.axis == "" and m.first.kind == cm.KIND_FREE
    symbol = sym._symbol
    _sym_click(sym, qapp, x1, y1)
    _sym_click(sym, qapp, -symbol.width / 2 + 0.1, 0.6)
    edge = sym._measure.overlay.measurements[-1]
    assert edge.perpendicular and edge.distance_mm == pytest.approx(abs(x1 + symbol.width / 2))


def test_symbol_measures_are_not_in_the_history_the_draft_or_the_kicad_text(sym, qapp):
    from kicadgen.generators.symbol import serialize_symbol, symbol_inputs, symbol_property_positions
    from kicadgen.generators.symbol_glyphs import symbol_graphics

    def text():
        rows = sym.draft_rows()
        pins, body = symbol_inputs(rows)
        return serialize_symbol('Board', pins, body=body, graphics=symbol_graphics(rows),
                                properties=symbol_property_positions(rows))

    rows, history, before = deepcopy(sym.draft_rows()), len(sym._history), text()
    pins, left, _right = _two_columns(sym)
    sym.toggle_measure()
    _sym_click(sym, qapp, *pins[left[0]])
    _sym_click(sym, qapp, *pins[left[1]])
    _sym_click(sym, qapp, pins[left[0]][0] - 3.0, 0.0)
    assert sym._measure.overlay.count == 1
    assert sym.draft_rows() == rows and len(sym._history) == history
    after = text()
    assert after == before and '⊥' not in after and 'ΔX' not in after


def test_symbol_escape_ladder_and_set_data_keeps_the_same_geometry(sym, qapp):
    pins, left, _right = _two_columns(sym)
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    _sym_click(sym, qapp, *pins[left[0]])
    QTest.keyClick(sym.view, Qt.Key.Key_Escape)
    assert sym._measure.armed and sym._collector.points == [] and sym._measure.overlay.count == 1
    QTest.keyClick(sym.view, Qt.Key.Key_Escape)
    assert sym._measure.armed and sym._measure.overlay.count == 0
    QTest.keyClick(sym.view, Qt.Key.Key_Escape)
    assert not sym._measure.armed and not sym.measure_button.defaultAction().isChecked()
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    # A1: a reload of the same geometry keeps the measures (the tool is disarmed, C13) ...
    sym.set_data('Other', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    qapp.processEvents()
    assert sym._measure.overlay.count == 1 and not sym._measure.armed
    # ... another geometry drops them.
    rows8 = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 9)]
    sym.set_data('Other', rows8, pad_ids=[r['pin'] for r in rows8])
    qapp.processEvents()
    assert sym._measure.overlay.count == 0


def test_symbol_escape_at_the_placement_stage_cancels_that_dimension(sym, qapp):
    pins, left, _right = _two_columns(sym)
    sym.toggle_measure()
    _sym_click(sym, qapp, *pins[left[0]])
    _sym_click(sym, qapp, *pins[left[1]])
    assert len(sym._collector.points) == 2
    QTest.keyClick(sym.view, Qt.Key.Key_Escape)
    assert sym._measure.armed and sym._collector.points == [] and sym._measure.overlay.count == 0


def test_symbol_escape_during_a_press_drag_adds_no_point_at_the_release(sym, qapp):
    """D1: Esc between the press and the release drops the point; the release adds none."""
    pins, left, right = _two_columns(sym)
    sym.toggle_measure()
    start, end = _vp(sym, 0.37, 0.53), _vp(sym, 1.37, 1.13)
    QTest.mousePress(sym.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, start)
    QTest.mouseMove(sym.view.viewport(), end)
    assert len(sym._collector.points) == 1
    QTest.keyClick(sym.view, Qt.Key.Key_Escape)
    QTest.mouseRelease(sym.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, end)
    qapp.processEvents()
    assert sym._measure.armed and sym._collector.points == [] and sym._measure.overlay.count == 0
    QTest.mousePress(sym.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, start)
    QTest.mouseMove(sym.view.viewport(), end)
    QTest.mouseRelease(sym.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, end)
    qapp.processEvents()
    assert sym._measure.overlay.count == 1


def test_symbol_framing_ignores_the_measures(sym, qapp):
    """D2: the scene rect the fit frames is the same with and without measures."""
    sym._settle_after_gesture()
    without = sym.view.scene().sceneRect()
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    assert [i for i in sym.view.scene().items() if i.data(cm.MEASURE_ITEM_ROLE)]
    sym._settle_after_gesture()
    assert sym.view.scene().sceneRect() == without
    sym._draw(sym._symbol)
    assert sym.view.scene().sceneRect() == without


def test_symbol_a_pending_point_goes_when_the_geometry_changes(sym, qapp):
    sym.toggle_measure()
    _sym_click(sym, qapp, 0.37, 0.53)
    sym._draw(sym._symbol)
    assert len(sym._collector.points) == 1
    sym._measure.sync(cm.SnapTargets(centres=((0.0, 0.0, '1'),)))
    assert sym._measure.armed and sym._collector.points == []


def test_symbol_right_click_on_a_measure_offers_remove_and_not_the_pin_menu(sym, qapp, monkeypatch):
    pins, left, _right = _two_columns(sym)
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    sym.toggle_measure()
    offered, menus = [], []
    monkeypatch.setattr(canvas_menus, "exec_element_menu",
                        lambda menu, keys, pos: (offered.append(list(keys.values())), canvas_menus.DELETE)[1])
    sym.view.pin_context_requested.disconnect()
    sym.view.pin_context_requested.connect(lambda *a: menus.append(a))
    pos = _vp(sym, 0.87, 0.83)
    QTest.mousePress(sym.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QApplication.sendEvent(sym.view.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                                                  sym.view.viewport().mapToGlobal(pos)))
    QTest.mouseRelease(sym.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    qapp.processEvents()
    assert offered == [[canvas_menus.DELETE]] and sym._measure.overlay.count == 0 and menus == []
    # Negative: a right click on a pin away from any measure still asks for the pin menu.
    pin_pos = _vp(sym, *pins[left[0]])
    QTest.mousePress(sym.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pin_pos)
    QApplication.sendEvent(sym.view.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pin_pos,
                                                                  sym.view.viewport().mapToGlobal(pin_pos)))
    QTest.mouseRelease(sym.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pin_pos)
    qapp.processEvents()
    assert len(offered) == 1
    assert menus and menus[0][0] == left[0], "the pin menu was requested for that pin"


def test_symbol_hovering_a_measure_highlights_it_and_delete_removes_it(sym, qapp):
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    sym.toggle_measure()
    rows = deepcopy(sym.draft_rows())
    _sym_move(sym, qapp, 0.87, 0.83)
    assert sym._measure.overlay.hovered == 0
    sym.view.setFocus()
    QTest.keyClick(sym.view, Qt.Key.Key_Delete)
    qapp.processEvents()
    assert sym._measure.overlay.count == 0 and sym.draft_rows() == rows
    _sym_move(sym, qapp, -8.0, 8.0)
    assert sym._measure.overlay.hovered is None


def test_symbol_wire_still_arms_over_the_measure(sym, qapp):
    sym.toggle_measure()
    sym.start_wire()
    assert not sym._measure.armed and sym._collector.spec.key == 'wire'
    assert not sym.measure_button.defaultAction().isChecked()


def _record_menu(monkeypatch):
    offered = []
    monkeypatch.setattr(canvas_menus, "exec_element_menu",
                        lambda menu, keys, pos: (offered.append(list(keys.values())), None)[1])
    return offered


def test_symbol_right_click_or_enter_commit_a_dimension_at_its_placement_stage(sym, qapp, monkeypatch):
    """Finding 1: at the placement stage a right click over an existing measure commits, no menu."""
    pins, left, _right = _two_columns(sym)
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    offered = _record_menu(monkeypatch)
    _sym_click(sym, qapp, *pins[left[0]])
    _sym_click(sym, qapp, *pins[left[1]])
    QTest.mouseClick(sym.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _vp(sym, 0.87, 0.83))
    qapp.processEvents()
    assert offered == [] and sym._measure.overlay.count == 2
    assert sym._measure.overlay.measurements[-1].axis == 'y' and sym._measure.armed
    _sym_click(sym, qapp, *pins[left[0]])
    _sym_click(sym, qapp, *pins[left[1]])
    sym.view.setFocus()
    QTest.keyClick(sym.view, Qt.Key.Key_Return)
    qapp.processEvents()
    assert sym._measure.overlay.count == 3


def test_symbol_right_click_over_a_measure_finishes_the_wire(sym, qapp, monkeypatch):
    """Finding 1: the wire keeps its right click over a measure (it is committed, no menu)."""
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    graphics = len(sym._graphics())
    sym.start_wire()
    _sym_click(sym, qapp, -8.0, 8.0)
    _sym_click(sym, qapp, -8.0, 4.0)
    offered = _record_menu(monkeypatch)
    QTest.mouseClick(sym.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _vp(sym, 0.87, 0.83))
    qapp.processEvents()
    assert offered == [] and not sym._collector.armed
    assert len(sym._graphics()) == graphics + 1


def test_symbol_right_press_during_a_rubber_band_opens_no_measure_menu(sym, qapp, monkeypatch):
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    sym.toggle_measure()
    offered = _record_menu(monkeypatch)
    viewport = sym.view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, _vp(sym, -8, 8))
    QTest.mouseMove(viewport, _vp(sym, 3, -3))
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, _vp(sym, 0.87, 0.83))
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, _vp(sym, 0.87, 0.83))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, _vp(sym, 3, -3))
    assert offered == [] and sym._measure.overlay.count == 1


def test_symbol_delete_with_no_measure_under_the_pointer_reaches_the_element(sym, qapp):
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    sym.toggle_measure()
    assert sym.start_shape('rectangle')
    _sym_click(sym, qapp, -6.0, -6.0)
    _sym_click(sym, qapp, -4.0, -4.0)
    sym._router.disarm()
    count = len(sym._graphics())
    _sym_click(sym, qapp, -6.0, -5.0)
    assert sym._measure.overlay.hovered is None
    sym.view.setFocus()
    QTest.keyClick(sym.view, Qt.Key.Key_Delete)
    qapp.processEvents()
    assert len(sym._graphics()) == count - 1 and sym._measure.overlay.count == 1


def test_symbol_hide_disarms_and_keeps_the_measures(sym, qapp):
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    _sym_click(sym, qapp, 0.0, 0.0)
    sym.hide()
    qapp.processEvents()
    assert not sym._measure.armed and not sym.measure_button.defaultAction().isChecked()
    assert sym._measure.overlay.count == 1


def test_symbol_a_pending_point_goes_when_the_body_is_resized(sym, qapp):
    """A3 with a real geometry change: the body corners and edges move."""
    sym.toggle_measure()
    _sym_click(sym, qapp, 0.37, 0.53, Qt.KeyboardModifier.ShiftModifier)
    assert len(sym._collector.points) == 1
    sym.resize_body(sym._symbol.width + 5.08, sym._symbol.height)
    qapp.processEvents()
    assert sym._measure.armed and sym._collector.points == []


def test_symbol_a_transiently_invalid_draft_keeps_the_measures(sym, qapp):
    """Decision a: an invalid draft draws nothing for a moment; the measures stay."""
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    sym._draw(None)
    assert sym._measure.overlay.count == 1
    sym._draw(sym._symbol)
    assert sym._measure.overlay.count == 1
    assert [i for i in sym.view.scene().items() if i.data(cm.MEASURE_ITEM_ROLE)]


def test_symbol_clear_measures_also_drops_the_pending_point(sym, qapp):
    sym.toggle_measure()
    _free_diagonal(sym, qapp)
    _sym_click(sym, qapp, 0.37, 0.53, Qt.KeyboardModifier.ShiftModifier)
    sym.clear_measures()
    assert sym._measure.overlay.count == 0 and sym._measure.armed and sym._collector.points == []
