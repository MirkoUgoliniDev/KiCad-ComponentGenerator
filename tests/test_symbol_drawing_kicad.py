"""@brief KiCad-grade drawing in the symbol editor (plan docs/footprint-drawing-kicad-plan.md, wave 2).

Pins, through real QTest events wherever the offscreen platform delivers
them, the symbol side of the shared drawing tools:

- D1: the polygon tool closes from three vertices, never on two, and is
  stored as a closed polyline (first point repeated, click-tools plan C12);
- A1: the «Disegno» tool grid, Select first, follows arm/disarm silently;
- A4: the status line shows the next step after each click and Backspace;
- A6/A8: Shift+Space cycles the angle mode, the toolbar control follows and
  the configuration keeps it; a 40 degree segment becomes 45 degrees in
  mode 45, Shift frees it;
- B3/B4: the object snap catches a vertex within the 10 px magnet and not at
  12 px, catches a pin end, and shows/removes its typed marker;
- A7: Ctrl+Shift+L arms the line, E edits the properties;
- C3: the «Proprietà» panel commits one undo step per field, a refresh
  commits nothing, a mixed width is not written unless edited.

Every rule has a positive and a negative case (CLAUDE.md «Correzioni»).
"""

import math
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui.canvas_tool_hints import step_hint
from kicadgen.gui.symbol_tab import (CONFIG_ANGLE_MODE, CONFIG_OBJECT_SNAP, OBJECT_SNAP_REACH_PX,
                                     SCENE_UNITS_PER_MM, SymbolTab)

S = SCENE_UNITS_PER_MM
LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ROWS5 = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)]


def _settle(qapp, times=10):
    """@brief Process pending events (layout, fit, deferred redraws)."""
    for _ in range(times):
        qapp.processEvents()


@pytest.fixture
def tab(qapp):
    """@brief A shown five-pin symbol tab, no recovery file."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    tab.show()
    _settle(qapp)
    yield tab
    tab.hide()
    tab.deleteLater()
    _settle(qapp, 2)


def _px_per_mm(tab):
    """@brief Viewport pixels per symbol millimetre at the current zoom."""
    return tab.view.transform().m11() * S


def _vp(tab, x_mm, y_mm):
    """@brief Viewport point of a symbol point (mm, Y up)."""
    return tab.view.mapFromScene(QPointF(x_mm * S, -y_mm * S))


def _click_at(tab, qapp, pos, modifier=NO_MOD):
    """@brief One real left click at a viewport position."""
    QTest.mouseClick(tab.view.viewport(), LEFT, modifier, pos)
    qapp.processEvents()


def _click(tab, qapp, x_mm, y_mm, modifier=NO_MOD):
    """@brief One real left click at a symbol point."""
    _click_at(tab, qapp, _vp(tab, x_mm, y_mm), modifier)


def _move_at(tab, qapp, pos):
    """@brief One real mouse move (no button) to a viewport position."""
    QTest.mouseMove(tab.view.viewport(), pos)
    qapp.processEvents()


def _key(tab, qapp, key, modifier=NO_MOD):
    """@brief One key click on the view."""
    QTest.keyClick(tab.view, key, modifier)
    qapp.processEvents()


def _far_rectangle(tab):
    """@brief Draw a rectangle shape far right of the body through the API; return its corners (mm)."""
    corners = ((15.24, 7.62), (20.32, 12.7))
    tab.set_object_snap(False)
    tab.start_shape('rectangle')
    for x, y in corners:
        tab.add_shape_point(x, y)
    tab.select_tool()
    tab.set_object_snap(True)
    tab.clear_selection()
    tab.view.fit()
    return corners


# ── D1: polygon tool ─────────────────────────────────────────────────────────

def test_polygon_tool_is_declared_with_a_closed_glyph():
    """@brief The polygon is a SHAPE_TOOLS entry (open-ended polyline) whose palette glyph is a closed outline."""
    assert g.shape_tool('polygon') == ('polygon', 'Poligono', 'polyline', 0)
    glyph = next(x for x in g.shape_tool_glyphs() if x.key == 'polygon')
    points = glyph.primitives[0].points
    assert points[0] == points[-1] and len(set(points)) >= 5


def test_polygon_record_is_a_closed_polyline_and_degenerate_polygons_are_refused():
    """@brief Positive: three vertices close (first point repeated); negative: two vertices or a collinear set."""
    record = g.shape_instance('polygon', [(0, 0), (2.54, 0), (2.54, 2.54)])
    points = [tuple(p) for p in record['primitives'][0]['points']]
    assert record['primitives'][0]['kind'] == 'polyline' and len(points) == 4 and points[0] == points[-1]
    closed_by_click = g.shape_instance('polygon', [(0, 0), (2.54, 0), (2.54, 2.54), (0, 0)])
    assert len(closed_by_click['primitives'][0]['points']) == 4, 'the closing click is not a fourth vertex'
    assert g.shape_instance('polygon', [(0, 0), (2.54, 0), (0, 0)]) is None, 'two vertices are no polygon'
    assert g.shape_instance('polygon', [(0, 0), (1.27, 0), (2.54, 0)]) is None, 'collinear vertices enclose nothing'
    assert g.shape_instance('line', [(0, 0), (1.27, 0), (2.54, 0)]) is not None, 'the line keeps its rule'


def test_polygon_closes_at_three_vertices_not_two(tab, qapp):
    """@brief A click back on the first point closes from three vertices; after two it is just another point."""
    tab.set_object_snap(False)
    tab.start_shape('polygon')
    a, b, c = (-2.54, -2.54), (2.54, -2.54), (2.54, 2.54)
    _click(tab, qapp, *a)
    _click(tab, qapp, *b)
    _click(tab, qapp, *a)
    assert tab.graphics() == [] and tab._shape_points == [a, b, a], 'A, B, A does not close'
    _key(tab, qapp, Qt.Key.Key_Escape)
    tab.start_shape('polygon')
    for point in (a, b, c, a):
        _click(tab, qapp, *point)
    assert len(tab.graphics()) == 1 and tab.shape_tool() == 'polygon', 'closed, and the tool persists'
    stored = [tuple(p) for p in g.placed_primitives(tab.graphics()[0])[0].points]
    assert stored == [a, b, c, a]


def test_polygon_finished_by_enter_is_closed_and_with_two_points_is_refused(tab, qapp):
    """@brief Enter with three vertices stores them closed; Enter with two says the shape is invalid."""
    tab.set_object_snap(False)
    tab.start_shape('polygon')
    for point in ((-2.54, -2.54), (2.54, -2.54)):
        _click(tab, qapp, *point)
    _key(tab, qapp, Qt.Key.Key_Return)
    assert tab.graphics() == [] and tab.status.text().startswith('Forma non valida')
    for point in ((-2.54, -2.54), (2.54, -2.54), (0.0, 2.54)):
        _click(tab, qapp, *point)
    _key(tab, qapp, Qt.Key.Key_Return)
    stored = [tuple(p) for p in g.placed_primitives(tab.graphics()[0])[0].points]
    assert stored[0] == stored[-1] and len(stored) == 4


# ── A1: tool grid ────────────────────────────────────────────────────────────

def test_select_and_a_click_on_the_active_tool_disarm(tab, qapp):
    """@brief Select disarms; clicking the active tool again disarms; another tool switches."""
    grid = tab.palette.tool_grid
    assert grid.keys()[0] == '' and 'polygon' in grid.keys()
    grid.button('rectangle').click()
    assert tab.shape_tool() == 'rectangle'
    grid.button('').click()
    assert tab.shape_tool() is None and not tab.view.wiring and grid.active_key() is None
    grid.button('circle').click()
    grid.button('circle').click()
    assert tab.shape_tool() is None and grid.active_key() is None
    grid.button('circle').click()
    grid.button('arc').click()
    assert tab.shape_tool() == 'arc' and grid.active_key() == 'arc'


def test_the_active_button_follows_arm_and_disarm_from_anywhere(tab, qapp):
    """@brief Arming through the API, ending with Escape and arming the wire are mirrored silently."""
    grid = tab.palette.tool_grid
    emitted = []
    grid.tool_triggered.connect(emitted.append)
    tab.start_shape('circle')
    assert grid.active_key() == 'circle' and grid.button('circle').isChecked()
    _key(tab, qapp, Qt.Key.Key_Escape)
    assert grid.active_key() is None and grid.button('').isChecked()
    tab.start_shape('bezier')
    tab.start_wire()
    # CONTRACT CHANGED 2026-11-08 (review S4): the wire lives outside the grid, so
    # the grid shows NO button checked, not even Select (it was "Select shown").
    assert grid.active_key() is None, 'the wire is not a grid tool'
    assert grid.outside_active() and not grid.button('').isChecked(), 'Select is not claimed while wiring'
    tab.finish_wire(False)
    assert not grid.outside_active() and grid.button('').isChecked(), 'the wire ended: back to Select'
    assert emitted == [], 'set_active never emits'
    tab.start_shape('text')
    tab.retranslate()
    assert tab.palette.tool_grid.active_key() == 'text', 'a language change keeps the shown tool'


def test_grid_tooltips_carry_the_shortcut(tab):
    """@brief Each shape button's tooltip ends with its KiCad-style shortcut; Select names Esc."""
    grid = tab.palette.tool_grid
    assert grid.button('line').toolTip().endswith('(Ctrl+Shift+L)')
    assert grid.button('polygon').toolTip().endswith('(Ctrl+Shift+P)')
    assert grid.button('').toolTip().endswith('(Esc)')


# ── A4: step hints ───────────────────────────────────────────────────────────

def test_the_hint_follows_each_click_and_backspace(tab, qapp):
    """@brief Arm, each click and Backspace each print the next step; a result keeps its own text."""
    tab.set_object_snap(False)
    tab.start_shape('line')
    assert tab.status.text() == step_hint('line', 0)
    _click(tab, qapp, -2.54, 0.0)
    assert tab.status.text() == step_hint('line', 1)
    _click(tab, qapp, 2.54, 0.0)
    _click(tab, qapp, 2.54, 2.54)
    assert tab.status.text() == step_hint('line', 3, closable=True), 'the close rule is offered'
    _key(tab, qapp, Qt.Key.Key_Backspace)
    assert tab._shape_points == [(-2.54, 0.0), (2.54, 0.0)] and tab.status.text() == step_hint('line', 2)
    _key(tab, qapp, Qt.Key.Key_Backspace)
    _key(tab, qapp, Qt.Key.Key_Backspace)
    assert tab._shape_points == [] and tab.status.text() == step_hint('line', 0)
    _key(tab, qapp, Qt.Key.Key_Backspace)
    assert tab.shape_tool() == 'line', 'Backspace with no point keeps the tool'
    tab.start_shape('rectangle')
    _click(tab, qapp, -2.54, -2.54)
    assert tab.status.text() == step_hint('rectangle', 1)
    _click(tab, qapp, 2.54, 2.54)
    assert tab.status.text().startswith('Forma inserita.'), 'the result is not overwritten by the next hint'


def test_the_wire_shows_its_step_hints_too(tab, qapp):
    """@brief The wire prints its first and next step (key "wire")."""
    tab.start_wire()
    assert tab.status.text() == step_hint('wire', 0)
    _click(tab, qapp, -7.62, 5.08)
    assert tab.status.text() == step_hint('wire', 1)


# ── A6/A8: angle mode ────────────────────────────────────────────────────────

def test_shift_space_cycles_the_angle_mode_and_the_toolbar_follows(tab, qapp):
    """@brief Shift+Space while armed: free -> 90 -> 45 -> free, the combo mirrors it; idle it does nothing."""
    assert tab.angle_mode() == 'free' and tab.angle_combo.currentData() == 'free'
    _key(tab, qapp, Qt.Key.Key_Space, SHIFT)
    assert tab.angle_mode() == 'free', 'no tool armed: the router does not take the key'
    tab.start_shape('line')
    seen = []
    for _ in range(3):
        _key(tab, qapp, Qt.Key.Key_Space, SHIFT)
        seen.append((tab.angle_mode(), tab.angle_combo.currentData()))
    assert seen == [('90', '90'), ('45', '45'), ('free', 'free')]
    tab.angle_combo.setCurrentIndex(tab.angle_combo.findData('45'))
    assert tab.angle_mode() == '45', 'the control drives the router, armed or not'


def test_the_angle_mode_and_the_object_snap_are_saved_and_restored(tab, qapp):
    """@brief Bound settings: changes are written and saved once; a bound value is applied without a save."""
    saves = []
    config = {CONFIG_ANGLE_MODE: '90', CONFIG_OBJECT_SNAP: False}
    tab.bind_settings(config, lambda: saves.append(dict(config)))
    assert tab.angle_mode() == '90' and tab.angle_combo.currentData() == '90' and not tab.object_snap()
    assert not tab.object_snap_button.defaultAction().isChecked() and saves == [], 'restoring does not save'
    tab.angle_combo.setCurrentIndex(tab.angle_combo.findData('45'))
    tab.object_snap_button.defaultAction().trigger()
    assert config[CONFIG_ANGLE_MODE] == '45' and config[CONFIG_OBJECT_SNAP] is True and len(saves) == 2
    other = SymbolTab()
    try:
        other.bind_settings({CONFIG_ANGLE_MODE: 'bogus', CONFIG_OBJECT_SNAP: 'yes'})
        assert other.angle_mode() == 'free' and other.object_snap(), 'invalid values keep the defaults'
    finally:
        other.deleteLater()


def test_a_40_degree_segment_snaps_to_45_in_mode_45_and_not_with_shift(tab, qapp):
    """@brief Mode 45: the click lands exactly on the diagonal, on the grid; Shift keeps the grid point of the pointer."""
    tab.set_object_snap(False)
    tab.set_angle_mode('45')
    anchor = (-5.08, -2.54)
    length = 5.0
    raw = (anchor[0] + length * math.cos(math.radians(40)), anchor[1] + length * math.sin(math.radians(40)))
    tab.start_shape('line')
    _click(tab, qapp, *anchor)
    _click(tab, qapp, *raw)
    (ax, ay), (bx, by) = tab._shape_points
    assert math.isclose(bx - ax, by - ay, abs_tol=1e-6) and bx > ax, 'on the 45 degree ray'
    step = tab.drawing_grid()
    assert math.isclose(round((bx - ax) / step) * step, bx - ax, abs_tol=1e-6), 'a whole number of grid steps'
    tab.finish_shape(False)
    tab.start_shape('line')
    _click(tab, qapp, *anchor)
    pointer = tab.view.mapToScene(_vp(tab, *raw))
    expected = tab._grid_point(pointer.x() / S, -pointer.y() / S)
    _click(tab, qapp, *raw, modifier=SHIFT)
    assert tab._shape_points[1] == expected
    assert not math.isclose(expected[0] - anchor[0], expected[1] - anchor[1], abs_tol=1e-6), 'Shift: still 40 degrees'


def test_the_angle_mode_constrains_lines_and_polygons_only(tab, qapp):
    """@brief Mode 90 turns a sloped line segment horizontal; a rectangle corner is not constrained."""
    tab.set_object_snap(False)
    tab.set_angle_mode('90')
    tab.start_shape('line')
    _click(tab, qapp, -5.08, -2.54)
    _click(tab, qapp, 0.0, -1.27)
    assert tab._shape_points[1] == (0.0, -2.54)
    tab.finish_shape(False)
    tab.start_shape('rectangle')
    _click(tab, qapp, -5.08, -2.54)
    _click(tab, qapp, 0.0, -1.27)
    corners = g.shape_handles(tab.graphics()[-1])
    assert (0.0, -1.27) in [tuple(c) for c in corners], 'the rectangle keeps its free corner'


# ── B3/B4: object snap and marker ────────────────────────────────────────────

def test_the_object_snap_catches_a_vertex_within_10_px_and_not_at_12(tab, qapp):
    """@brief A rectangle corner is caught 9 px away and not 12 px away (the 10 px UI reach)."""
    corners = _far_rectangle(tab)
    _settle(qapp)
    corner = corners[1]
    assert OBJECT_SNAP_REACH_PX == 10.0
    tab.start_shape('line')
    _click_at(tab, qapp, _vp(tab, *corner) + QPoint(9, 0))
    assert tab._shape_points == [corner]
    tab.finish_shape(False)
    tab.start_shape('line')
    pos = _vp(tab, *corner) + QPoint(12, 0)
    # Mapped before the click: the click redraws, and the auto-fit may then move the view.
    scene = tab.view.mapToScene(pos)
    _click_at(tab, qapp, pos)
    assert tab._shape_points == [tab._grid_point(scene.x() / S, -scene.y() / S)] != [corner]


def test_the_object_snap_catches_a_pin_end_and_shows_the_pad_marker(tab, qapp):
    """@brief Hovering near a pin end shows the filled-ring marker; far away, a leave or a disarm removes it."""
    pin = tab._symbol.pins[0]
    end = (round(pin.x, 4), round(pin.y, 4))
    tab.start_shape('line')
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(4, 3))
    assert tab.snap_marker() == ('pad', end[0], end[1])
    assert tab._collector.preview == [end], 'the ghost and the click agree (contract 2)'
    far = _vp(tab, 0.5, 0.8)
    _move_at(tab, qapp, far)
    assert tab.snap_marker() is None, 'a grid point shows no marker'
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(3, 0))
    assert tab.snap_marker() is not None
    QApplication.sendEvent(tab.view, QEvent(QEvent.Type.Leave))
    qapp.processEvents()
    assert tab.snap_marker() is None, 'leaving the view removes it'
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(2, 0))
    assert tab.snap_marker() is not None
    _key(tab, qapp, Qt.Key.Key_Escape)
    assert tab.snap_marker() is None, 'disarming removes it'


def test_the_polygon_closes_on_its_first_vertex_through_the_object_snap(tab, qapp):
    """@brief A click a few pixels off the first vertex snaps onto it and closes the polygon."""
    tab.set_object_snap(True)
    tab.start_shape('polygon')
    a = (12.7, -5.08)
    for point in (a, (17.78, -5.08), (17.78, 0.0)):
        _click(tab, qapp, *point)
    _click_at(tab, qapp, _vp(tab, *a) + QPoint(5, -4))
    assert len(tab.graphics()) == 1, 'closed on the first vertex'
    stored = [tuple(p) for p in g.placed_primitives(tab.graphics()[0])[0].points]
    assert stored[0] == stored[-1] == a


def test_a_shape_intersection_and_a_midpoint_are_candidates(tab):
    """@brief Two crossing lines give an intersection candidate; a segment gives its midpoint."""
    tab.set_object_snap(False)
    for points in (((10.16, 0.0), (15.24, 5.08)), ((10.16, 5.08), (15.24, 0.0))):
        tab.start_shape('line')
        for p in points:
            tab.add_shape_point(*p)
        tab.finish_shape(True)
    index = tab._snap_index()
    hit = index.nearest(12.7, 2.6, 0.2)
    assert hit is not None and hit.kind == 'intersection' and math.isclose(hit.x, 12.7, abs_tol=1e-6)
    assert index is tab._snap_index(), 'built once per drawing revision'
    tab.undo()
    assert tab._snap_index() is not index, 'a new revision rebuilds it'


# ── A7: shortcuts ────────────────────────────────────────────────────────────

def _shortcut(tab, sequence):
    """@brief The tab view's QShortcut for a key sequence."""
    wanted = QKeySequence(sequence)
    return next(s for s in tab.view.findChildren(QShortcut) if s.key() == wanted)


def test_ctrl_shift_l_arms_the_line_and_the_symbol_binds_only_its_tools(tab, qapp):
    """@brief The line, polygon... shortcuts exist on the view; the footprint-only dimension and text box do not."""
    _shortcut(tab, 'Ctrl+Shift+L').activated.emit()
    assert tab.shape_tool() == 'line'
    _shortcut(tab, 'Ctrl+Shift+P').activated.emit()
    assert tab.shape_tool() == 'polygon' and tab.palette.tool_grid.active_key() == 'polygon'
    keys = {s.key().toString() for s in tab.view.findChildren(QShortcut)}
    assert 'Ctrl+Shift+H' not in keys and 'Ctrl+Shift+X' not in keys
    assert {'R', 'Shift+R', 'Ctrl+Shift+R', 'E'} <= keys, 'no clash: rotation keys and the rectangle coexist'


def test_e_opens_the_hovered_element_and_does_nothing_on_empty_space(tab, qapp, monkeypatch):
    """@brief E with no selection: the element under the pointer opens its dialog; on empty canvas nothing."""
    corners = _far_rectangle(tab)
    _settle(qapp)
    opened = []
    monkeypatch.setattr(tab, '_edit_element_properties', opened.append)
    _move_at(tab, qapp, _vp(tab, *corners[0]))
    assert tab.edit_properties() and opened == [0]
    _move_at(tab, qapp, _vp(tab, -20.0, -20.0))
    assert not tab.edit_properties() and opened == [0]


# ── C3: properties panel ─────────────────────────────────────────────────────

def _two_rectangles(tab):
    """@brief Two rectangle shapes with different stroke widths, both selected."""
    tab.set_object_snap(False)
    tab.start_shape('rectangle')
    for p in ((10.16, 0.0), (12.7, 2.54), (15.24, 0.0), (17.78, 2.54)):
        tab.add_shape_point(*p)
    tab.select_tool()
    tab._edit_graphic(1, 'width', 0.5)
    tab._select_graphic(0)
    tab._select_graphic(1, additive=True)


def test_one_committed_field_is_one_undo_step(tab, qapp):
    """@brief Typing a stroke width and pressing Enter commits once; undo restores the old width."""
    tab.set_object_snap(False)
    tab.start_shape('rectangle')
    tab.add_shape_point(10.16, 0.0)
    tab.add_shape_point(12.7, 2.54)
    tab.select_tool()
    tab._select_graphic(0)
    panel = tab.properties_panel
    assert panel.field_keys()[:2] == ['x', 'y'] and 'size_w' in panel.field_keys()
    history = len(tab._history)
    box = panel.field_widget('width')
    box.setFocus()
    box.lineEdit().selectAll()
    QTest.keyClicks(box.lineEdit(), '0' + box.locale().decimalPoint() + '5')
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)
    qapp.processEvents()
    assert tab.graphics()[0]['primitives'][0]['width'] == 0.5 and len(tab._history) == history + 1
    assert panel.shown_value('width') == 0.5, 'the panel shows the committed value'
    tab.undo()
    assert tab.graphics()[0]['primitives'][0]['width'] == g.SHAPE_WIDTH_MM


def test_a_programmatic_refresh_commits_nothing(tab, qapp):
    """@brief Redraws, selection changes and undo refresh the panel without any commit."""
    _two_rectangles(tab)
    commits = []
    tab.properties_panel.committed.connect(lambda key, value: commits.append((key, value)))
    history = len(tab._history)
    tab._draw(tab._symbol)
    tab._select_graphic(0)
    tab.clear_selection()
    tab._select_graphic(1)
    assert commits == [] and len(tab._history) == history
    assert tab.properties_panel.shown_value('width') == 0.5


def test_a_mixed_width_is_not_written_unless_edited(tab, qapp):
    """@brief Two widths show «(vari)»; committing the fill keeps both widths; editing the width writes it."""
    _two_rectangles(tab)
    panel = tab.properties_panel
    box = panel.field_widget('width')
    assert box.is_mixed() and panel.shown_value('width') is None
    box.editingFinished.emit()
    assert [r['primitives'][0]['width'] for r in tab.graphics()] == [g.SHAPE_WIDTH_MM, 0.5], 'untouched: no write'
    combo = panel.field_widget('fill')
    history = len(tab._history)
    combo.setCurrentIndex(combo.findData('outline'))
    combo.activated.emit(combo.currentIndex())
    qapp.processEvents()
    records = tab.graphics()
    assert [r['primitives'][0]['fill'] for r in records] == ['outline', 'outline']
    assert [r['primitives'][0]['width'] for r in records] == [g.SHAPE_WIDTH_MM, 0.5]
    assert len(tab._history) == history + 1, 'one undo step for the whole selection'


def test_selected_pins_are_shown_read_only(tab, qapp):
    """@brief A selected pin shows its number and name, with no editable field."""
    tab._select_pins({'2'})
    panel = tab.properties_panel
    assert panel.field_keys() == ['pin', 'name'] and panel.shown_value('pin') == '2'
    assert panel.shown_value('name') == 'P2'
    tab.clear_selection()
    assert panel.field_keys() == ['pin', 'name'] and not panel.field_widget('pin').isVisible(), 'empty placeholder'
