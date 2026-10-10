"""@brief Palette of graphic elements in the symbol editor (user, 2026-10-10).

Placing, moving, turning, mirroring, deleting a glyph with undo; pins snap
to its terminals; the drawing survives apply, project save and export.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtWidgets import QGraphicsPathItem, QGraphicsSimpleTextItem

from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui.symbol_tab import SymbolTab, SCENE_UNITS_PER_MM


def _tab():
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)],
                 pad_ids=['1', '2', '3', '4', '5'])
    return tab


def _first_key(tab):
    return tab.glyph_combo.itemData(0)


def test_the_palette_lists_the_catalogue_and_a_click_places_the_chosen_glyph(qapp):
    tab = _tab()
    try:
        keys = [tab.glyph_combo.itemData(i) for i in range(tab.glyph_combo.count())]
        assert keys == [glyph.key for glyph in g.catalogue()]
        tab._start_placing()
        assert tab.view.placing is True and 'Clicca' in tab.status.text()
        tab.view.place_requested.emit(QPointF(2.6 * SCENE_UNITS_PER_MM, -5.0 * SCENE_UNITS_PER_MM))
        assert tab.view.placing is False
        graphics = tab.graphics()
        assert len(graphics) == 1 and graphics[0]['key'] == _first_key(tab)
        assert (graphics[0]['x'], graphics[0]['y']) == (2.54, 5.08), 'snapped to the grid, Y up'
        assert tab._selected_graphic == 0
        assert any(isinstance(item, QGraphicsPathItem) and item.data(0) == ('graphic', 0)
                   for item in tab.view.scene().items()), 'drawn and tagged'
        tab._start_placing()
        tab.view.place_requested.emit(None)
        assert tab.view.placing is False and len(tab.graphics()) == 1, 'cancel places nothing'
    finally:
        tab.deleteLater()


def test_edits_are_undoable_and_every_row_carries_the_same_list(qapp):
    tab = _tab()
    try:
        assert tab.place_glyph(_first_key(tab), 0, 0)
        assert tab._edit_graphic(0, 'move', (5.08, 2.54)) and tab.graphics()[0]['x'] == 5.08
        assert tab._edit_graphic(0, 'rotate', 90) and tab.graphics()[0]['rotation'] == 90
        assert tab._edit_graphic(0, 'mirror') and tab.graphics()[0]['mirror'] is True
        rows = tab.draft_rows()
        assert all(row['symbol_graphics'] == rows[0]['symbol_graphics'] for row in rows)
        tab.undo()
        assert tab.graphics()[0]['mirror'] is False
        tab.undo()
        assert tab.graphics()[0]['rotation'] == 0
        tab.redo()
        assert tab.graphics()[0]['rotation'] == 90
        assert tab._edit_graphic(0, 'delete') and tab.graphics() == []
        assert tab._selected_graphic is None
        tab.undo()
        assert len(tab.graphics()) == 1
        assert tab._edit_graphic(7, 'delete') is False, 'unknown index does nothing'
    finally:
        tab.deleteLater()


def test_a_pin_dropped_on_a_terminal_takes_its_position_and_direction(qapp):
    tab = _tab()
    try:
        assert tab.place_glyph(_first_key(tab), 0, 0)
        terminal = g.placed_terminals(tab.graphics()[0])[0]
        assert tab.place_pin('1', terminal.x + 0.5, terminal.y - 0.4)
        row = next(r for r in tab.draft_rows() if r['pin'] == '1')
        assert row['symbol_position'] == [round(terminal.x, 4), round(terminal.y, 4)]
        assert row['symbol_angle'] == terminal.angle
        # Far from every terminal the pin keeps the grid snap only.
        assert tab.place_pin('2', terminal.x + 12.0, terminal.y + 12.0)
        row = next(r for r in tab.draft_rows() if r['pin'] == '2')
        assert row['symbol_position'] != [round(terminal.x, 4), round(terminal.y, 4)]
    finally:
        tab.deleteLater()


def test_dragging_a_glyph_moves_it_on_the_grid_and_escape_restores(qapp):
    tab = _tab()
    try:
        assert tab.place_glyph(_first_key(tab), 0, 0)
        tab._begin_gesture(('graphic', 0), QPointF(0, 0))
        tab._move_gesture(('graphic', 0), QPointF(2.6 * SCENE_UNITS_PER_MM, 0))
        assert tab.graphics()[0]['x'] == 2.54
        tab._end_gesture(False)
        assert tab.graphics()[0]['x'] == 0.0, 'cancelled gesture goes back'
        tab._begin_gesture(('graphic', 0), QPointF(0, 0))
        tab._move_gesture(('graphic', 0), QPointF(0, -5.2 * SCENE_UNITS_PER_MM))
        tab._end_gesture(True)
        assert tab.graphics()[0]['y'] == 5.08
    finally:
        tab.deleteLater()


def test_the_body_can_follow_the_library_outline_when_the_glyph_had_one(qapp):
    tab = _tab()
    try:
        glyph = next((glyph for glyph in g.catalogue() if glyph.body_hint), None)
        if glyph is None:
            pytest.skip('no glyph with a body outline in this catalogue')
        assert tab.place_glyph(glyph.key, 0, 0)
        assert tab.fit_body_to_graphic(0)
        assert (tab._symbol.width, tab._symbol.height) == glyph.body_hint
        assert tab._edit_graphic(0, 'rotate', 90) and tab.fit_body_to_graphic(0)
        assert (tab._symbol.width, tab._symbol.height) == (glyph.body_hint[1], glyph.body_hint[0])
    finally:
        tab.deleteLater()


def test_applied_graphics_reach_the_project_and_the_exported_symbol(window, monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QFileDialog
    from tests.test_symbol_integration import setup_board
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    assert tab.place_glyph(_first_key(tab), 0, 0)
    tab.apply()
    assert window.pinout[0]['symbol_graphics'][0]['key'] == _first_key(tab)
    target = tmp_path / 'with_graphics.kicad_sym'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    window._export_electrical_symbol()
    text = target.read_text(encoding='utf-8')
    unit = text.split('_0_1"')[1].split('_1_1"')[0]
    assert '(polyline' in unit or '(rectangle' in unit or '(circle' in unit
    assert unit.count('(rectangle') >= 1, 'the body rectangle stays first'


def test_the_palette_is_a_vertical_panel_with_collapsible_groups(qapp):
    from kicadgen.generators.symbol_glyphs import grouped_catalogue
    tab = _tab()
    try:
        groups = grouped_catalogue()
        assert [key for key, _label, _glyphs in groups][:2] == ['relay', 'passive']
        assert tab.palette.width() <= 160, 'narrow panel on the left'
        first = groups[0][0]
        assert tab.palette.group_is_open(first)
        buttons_of_first = [action for action in tab.palette_actions()
                            if action.data() in {glyph.key for glyph in groups[0][2]}]
        assert buttons_of_first, 'the relay parts are in the first group'
        tab.palette.set_group_open(first, False)
        assert not tab.palette.group_is_open(first)
        tab.palette.set_group_open('nope', True)      # unknown key: ignored
        tab.retranslate()
        assert not tab.palette.group_is_open(first), 'a rebuild keeps the open/closed state'
        assert tab.palette.group_is_open(groups[1][0])
    finally:
        tab.deleteLater()


def test_the_palette_is_a_strip_of_icons_drawn_from_the_elements(qapp):
    from kicadgen.gui.symbol_tab import glyph_icon
    tab = _tab()
    try:
        keys = [action.data() for action in tab.palette_actions() if action.data()]
        assert keys == [glyph.key for glyph in g.catalogue()]
        assert tab.glyph_combo.isHidden(), 'the text selector is the hidden source of truth'
        icon = glyph_icon(g.catalogue()[0])
        image = icon.pixmap(36, 36).toImage()
        painted = sum(1 for x in range(36) for y in range(36) if image.pixelColor(x, y).alpha() > 0)
        assert painted > 20, 'the icon shows the element, it is not blank'
        second = tab.palette_actions()[1]
        second.trigger()
        assert tab.glyph_combo.currentData() == second.data() and tab.view.placing is True
        assert second.isChecked() and not tab.palette_actions()[0].isChecked()
        tab.view.place_requested.emit(QPointF(0, 0))
        assert tab.graphics()[0]['key'] == second.data()
        assert not second.isChecked(), 'placing done: the icon is released'
    finally:
        tab.deleteLater()


def test_a_free_terminal_shows_the_pin_place_as_a_dotted_guide(qapp):
    """The dot is the outer end of the pin to come; the guide fills the gap until then."""
    from PyQt6.QtWidgets import QGraphicsLineItem
    tab = _tab()
    try:
        assert tab.place_glyph('relay_coil', 0, 0)

        def guides():
            return [item for item in tab.view.scene().items()
                    if isinstance(item, QGraphicsLineItem) and item.pen().style() == Qt.PenStyle.DotLine]

        assert len(guides()) == 2, 'two free terminals, two guides'
        terminal = g.placed_terminals(tab.graphics()[0])[0]
        assert tab.place_pin('1', terminal.x, terminal.y)
        assert len(guides()) == 1, 'the attached pin replaces its guide'
    finally:
        tab.deleteLater()


def test_a_wire_joins_a_pin_end_to_an_element_terminal_and_is_exported(qapp):
    """User, 2026-10-10: «collegare con dei fili». Points snap to pin and terminal inner ends."""
    tab = _tab()
    try:
        assert tab.place_glyph('relay_coil', 0, 0)
        pin = next(p for p in tab._symbol.pins if p.pin.number == '1')
        vx = {0: 1, 180: -1}.get(pin.angle, 0)
        vy = {90: 1, 270: -1}.get(pin.angle, 0)
        pin_end = (round(pin.x + vx * tab._symbol.pin_length, 4), round(pin.y + vy * tab._symbol.pin_length, 4))
        terminal = g.placed_terminals(tab.graphics()[0])[0]
        tv = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[terminal.angle]
        terminal_end = (round(terminal.x + tv[0] * terminal.length, 4), round(terminal.y + tv[1] * terminal.length, 4))
        tab.start_wire()
        assert tab.view.wiring and tab.wire_button.defaultAction().isChecked()
        assert tab.add_wire_point(pin_end[0] + 0.4, pin_end[1] - 0.3), 'snaps to the pin end'
        assert tab.add_wire_point(pin_end[0], terminal_end[1])
        assert tab.add_wire_point(terminal_end[0] + 0.3, terminal_end[1] + 0.2), 'snaps to the terminal end'
        first, last = tab._wire_points[0], tab._wire_points[-1]
        assert (first['x'], first['y']) == pin_end and first['anchor'] == {'kind': 'pin', 'number': '1'}
        assert (last['x'], last['y']) == terminal_end and last['anchor']['kind'] == 'terminal'
        assert last['anchor']['element'] == tab.graphics()[0]['id'] and last['anchor']['index'] == 0
        assert tab.finish_wire(True)
        assert not tab.view.wiring and not tab.wire_button.defaultAction().isChecked()
        wire = tab.graphics()[-1]
        assert wire['key'] == 'wire' and wire['primitives'][0]['points'][0] == first, 'anchored point kept'
        assert wire['id'], 'a wire has a stable id like any element'
        from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
        pins, body = symbol_inputs(tab.draft_rows())
        text = serialize_symbol('W', pins, body=body, graphics=tab.graphics())
        assert '(polyline (pts (xy %s %s)' % (g._num(pin_end[0]), g._num(pin_end[1])) in text
        tab.undo()
        assert all(item['key'] != 'wire' for item in tab.graphics())
    finally:
        tab.deleteLater()


def test_a_wire_with_one_point_or_cancelled_adds_nothing(qapp):
    tab = _tab()
    try:
        tab.start_wire()
        assert tab.add_wire_point(0, 0)
        assert not tab.add_wire_point(0.2, -0.1), 'the same grid point twice is one point'
        assert tab.finish_wire(True) is False and tab.graphics() == []
        tab.start_wire()
        tab.add_wire_point(0, 0)
        tab.add_wire_point(2.54, 0)
        assert tab.finish_wire(False) is False and tab.graphics() == [] and tab._wire_points == []
    finally:
        tab.deleteLater()


def test_the_palette_sits_on_the_right_of_the_canvas(qapp):
    tab = _tab()
    tab.show()
    qapp.processEvents()
    try:
        assert tab.palette.geometry().left() > tab.view.geometry().left()
    finally:
        tab.deleteLater()


def test_the_wire_tool_lives_in_the_palette_too(qapp):
    tab = _tab()
    try:
        assert 'tools' in tab.palette._groups and tab.palette.group_is_open('tools')
        wire = tab.palette._tool_actions['wire']
        assert wire.data() == 'wire' and not wire.isChecked()
        wire.trigger()
        assert tab.view.wiring and wire.isChecked() and tab.wire_button.defaultAction().isChecked()
        tab.finish_wire(False)
        assert not wire.isChecked()
        assert all(action.data() != 'wire' for action in tab.palette_actions()), 'tools are not elements'
    finally:
        tab.deleteLater()


def test_kicad_colours_turn_the_body_yellow_and_the_drawings_dark(qapp):
    """User, 2026-10-10: the preview can show the schematic's colours; the file does not change."""
    from PyQt6.QtWidgets import QGraphicsRectItem
    from kicadgen.gui.symbol_tab import CANVAS_THEMES
    from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
    tab = _tab()
    try:
        assert tab.place_glyph('relay_coil', 0, 0)

        def body_fill():
            rects = [item for item in tab.view.scene().items()
                     if isinstance(item, QGraphicsRectItem) and item.data(0) is None
                     and item.rect().width() > 50]
            return max(rects, key=lambda r: r.rect().width()).brush().color().name()

        assert body_fill() == CANVAS_THEMES['dark']['body_fill']
        pins, body = symbol_inputs(tab.draft_rows())
        before = serialize_symbol('T', pins, body=body, graphics=tab.graphics())
        tab._select_graphic(None)                 # an unselected element shows the plain colour
        tab.theme_button.defaultAction().setChecked(True)
        assert tab.theme['body_fill'] == '#ffffc2' and body_fill() == '#ffffc2'
        assert tab.view.backgroundBrush().color().name() == '#ffffff'
        paths = [item for item in tab.view.scene().items() if item.data(0) == ('graphic', 0)]
        assert paths and all(item.pen().color().name() == '#840000' for item in paths), 'drawings dark on yellow'
        after = serialize_symbol('T', pins, body=body, graphics=tab.graphics())
        assert before == after, 'display only'
        tab.set_kicad_colors(False)
        assert body_fill() == CANVAS_THEMES['dark']['body_fill'] and not tab.theme_button.defaultAction().isChecked()
    finally:
        tab.deleteLater()


# ── Anchored wires (user, 2026-10-10: «se sposto un elemento si porta dietro il filo») ──

def _pin1_to_coil(tab):
    """A wire from pin 1's inner end (-5.08, 2.54) to the coil's first terminal end (0, 5.08):
    not aligned, so the orthogonal tool inserts the corner (0, 2.54). Returns the wire index."""
    assert tab.place_glyph('relay_coil', 0, 0)
    tab.start_wire()
    assert tab.add_wire_point(-5.0, 2.6) and tab.add_wire_point(0.1, 5.0)
    assert tab.finish_wire(True)
    return len(tab.graphics()) - 1


def _resolved(tab, index):
    return g.placed_primitives(tab.graphics()[index], graphics=tab.graphics(),
                               pin_ends=g.pin_inner_ends(tab._symbol))[0].points


def _vertex_handles(tab):
    return sorted(item.data(0) for item in tab.view.scene().items()
                  if item.data(0) and item.data(0)[0] == 'vertex')


def test_an_anchored_end_follows_the_element_while_the_free_corner_stays(qapp):
    tab = _tab()
    try:
        wire = _pin1_to_coil(tab)
        points = tab.graphics()[wire]['primitives'][0]['points']
        assert points[0]['anchor'] == {'kind': 'pin', 'number': '1'}
        assert points[1] == [0.0, 2.54], 'the corner is a free grid point'
        assert points[2]['anchor'] == {'kind': 'terminal', 'element': tab.graphics()[0]['id'], 'index': 0,
                                       'end': 'inner'}
        assert _resolved(tab, wire) == ((-5.08, 2.54), (0.0, 2.54), (0.0, 5.08))
        assert tab._edit_graphic(0, 'move', (2.54, 0.0))
        assert _resolved(tab, wire) == ((-5.08, 2.54), (0.0, 2.54), (2.54, 5.08)), 'the end moved, the corner did not'
        assert tab._edit_graphic(0, 'rotate', 90)
        expected = g.terminal_inner_end(g.placed_terminals(tab.graphics()[0])[0])
        assert _resolved(tab, wire)[2] == expected and _resolved(tab, wire)[1] == (0.0, 2.54)
        # The drag gesture goes through the same resolution.
        tab._begin_gesture(('graphic', 0), QPointF(0, 0))
        tab._move_gesture(('graphic', 0), QPointF(2.54 * SCENE_UNITS_PER_MM, 0))
        tab._end_gesture(True)
        assert _resolved(tab, wire)[2] == g.terminal_inner_end(g.placed_terminals(tab.graphics()[0])[0])
        # The exported symbol carries the resolved coordinates.
        from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
        pins, body = symbol_inputs(tab.draft_rows())
        text = serialize_symbol('W', pins, body=body, graphics=tab.graphics())
        x, y = _resolved(tab, wire)[2]
        assert '(xy %s %s))' % (g._num(x), g._num(y)) in text
        assert '(xy 0 5.08)' not in text, 'the original attachment point is gone from the export'
    finally:
        tab.deleteLater()


def test_a_moved_or_turned_pin_drags_its_wire_end(qapp):
    tab = _tab()
    try:
        wire = _pin1_to_coil(tab)
        assert tab.place_pin('1', -7.62, 0.0), 'swaps with pin 2'
        assert _resolved(tab, wire)[0] == (-5.08, 0.0)
        assert _resolved(tab, wire)[1:] == ((0.0, 2.54), (0.0, 5.08)), 'only the anchored end moved'
        assert tab.rotate_pin('1', 90)
        assert _resolved(tab, wire)[0] == g.pin_inner_ends(tab._symbol)['1']
        assert tab.set_pin_geometry('1', -7.62, -5.08, 0)
        assert _resolved(tab, wire)[0] == (-5.08, -5.08)
        tab.undo()
        assert _resolved(tab, wire)[0] == g.pin_inner_ends(tab._symbol)['1'] != (-5.08, -5.08)
    finally:
        tab.deleteLater()


def test_deleting_the_element_freezes_the_wire_end_where_it_was(qapp):
    tab = _tab()
    try:
        _pin1_to_coil(tab)
        assert tab._edit_graphic(0, 'move', (2.54, 0.0))
        assert tab._edit_graphic(0, 'delete')
        points = tab.graphics()[0]['primitives'][0]['points']
        assert points[2] == [2.54, 5.08], 'fixed where the terminal was at the deletion'
        assert points[0]['anchor'] == {'kind': 'pin', 'number': '1'}, 'the other anchor is untouched'
        assert _resolved(tab, 0) == ((-5.08, 2.54), (0.0, 2.54), (2.54, 5.08))
        tab.undo()
        assert tab.graphics()[1]['primitives'][0]['points'][2]['anchor']['kind'] == 'terminal'
    finally:
        tab.deleteLater()


def test_a_pin_dropped_by_a_new_pinout_leaves_its_wire_end_fixed(qapp):
    tab = _tab()
    try:
        _pin1_to_coil(tab)
        tab.apply()
        rows = [row for row in tab.rows() if row['pin'] != '1']
        tab.set_data('Board', rows, pad_ids=['2', '3', '4', '5'])
        points = tab.graphics()[1]['primitives'][0]['points']
        assert points[0] == [-5.08, 2.54], 'pin 1 is gone: its end stays where pin 1 was'
        assert points[2]['anchor']['kind'] == 'terminal', 'the terminal anchor survives'
        assert all(row['symbol_graphics'] == tab.draft_rows()[0]['symbol_graphics'] for row in tab.draft_rows())
    finally:
        tab.deleteLater()


def test_legacy_graphics_get_a_stable_id_in_the_first_history_entry(qapp):
    tab = SymbolTab()
    try:
        legacy = g.instance_from_glyph(g.catalogue()[0], 0, 0)
        del legacy['id']
        rows = [{'pin': str(i), 'name': f'P{i}', 'symbol_graphics': [dict(legacy)]} for i in range(1, 4)]
        tab.set_data('Legacy', rows, pad_ids=['1', '2', '3'])
        ident = tab.draft_rows()[0]['symbol_graphics'][0]['id']
        assert ident and all(row['symbol_graphics'][0]['id'] == ident for row in tab.draft_rows())
        assert len(tab._history) == 1 and tab._history[0][0]['symbol_graphics'][0]['id'] == ident
        tab.start_wire()
        assert tab.add_wire_point(*g.terminal_inner_end(g.placed_terminals(tab.graphics()[0])[0]))
        assert tab._wire_points[0]['anchor']['element'] == ident
        tab.finish_wire(False)
    finally:
        tab.deleteLater()


def test_orthogonal_wires_project_free_points_and_shift_or_the_toggle_keep_the_diagonal(qapp):
    tab = _tab()
    try:
        assert tab.orthogonal_button.defaultAction().isChecked() and tab._wire_orthogonal
        tab.start_wire()
        # Points above the body, away from every pin end (y <= 2.54).
        assert tab.add_wire_point(0, 10.16)
        assert tab.add_wire_point(5.08, 12.7), 'longer delta wins: horizontal'
        assert tab._wire_points[-1] == [5.08, 10.16]
        assert tab.add_wire_point(7.62, 17.78), 'vertical this time'
        assert tab._wire_points[-1] == [5.08, 17.78]
        assert tab.add_wire_point(10.16, 20.32, free=True), 'Shift: diagonal allowed'
        assert tab._wire_points[-1] == [10.16, 20.32]
        tab.finish_wire(False)
        tab.orthogonal_button.defaultAction().setChecked(False)
        assert not tab._wire_orthogonal
        tab.start_wire()
        assert tab.add_wire_point(0, 10.16) and tab.add_wire_point(5.08, 12.7)
        assert tab._wire_points[-1] == [5.08, 12.7], 'toggle off: the diagonal stays'
        tab.finish_wire(False)
        tab.set_wire_orthogonal(True)
        assert tab.orthogonal_button.defaultAction().isChecked()
        # An anchored target off the row/column gets a corner; an aligned one does not.
        assert tab.place_glyph('relay_coil', 0, 0)
        tab.start_wire()
        assert tab.add_wire_point(-5.08, 2.54) and tab.add_wire_point(0.0, 5.08)
        assert [tab._xy(p) for p in tab._wire_points] == [(-5.08, 2.54), (0.0, 2.54), (0.0, 5.08)]
        assert isinstance(tab._wire_points[1], list) and isinstance(tab._wire_points[2], dict)
        tab.finish_wire(False)
        tab.start_wire()
        assert tab.add_wire_point(-5.08, 2.54) and tab.add_wire_point(5.08, 2.54), 'pin 1 to pin 4, same row'
        assert len(tab._wire_points) == 2 and tab._wire_points[1]['anchor'] == {'kind': 'pin', 'number': '4'}
        tab.finish_wire(False)
    finally:
        tab.deleteLater()


def test_a_selected_wire_shows_draggable_vertices_that_anchor_or_free(qapp):
    tab = _tab()
    try:
        wire = _pin1_to_coil(tab)
        tab._select_graphic(None)
        assert _vertex_handles(tab) == [], 'no handles while the wire is not selected'
        tab._select_graphic(wire)
        assert _vertex_handles(tab) == [('vertex', wire, 0), ('vertex', wire, 1), ('vertex', wire, 2)]
        # The anchored end dropped on pin 3's end re-anchors.
        tab._begin_gesture(('vertex', wire, 0), QPointF(0, 0))
        tab._move_gesture(('vertex', wire, 0), QPointF(-5.0 * SCENE_UNITS_PER_MM, 2.5 * SCENE_UNITS_PER_MM))
        assert tab.graphics()[wire]['primitives'][0]['points'][0]['anchor'] == {'kind': 'pin', 'number': '3'}
        tab._end_gesture(True)
        assert _resolved(tab, wire)[0] == (-5.08, -2.54)
        tab.undo()
        assert tab.graphics()[wire]['primitives'][0]['points'][0]['anchor'] == {'kind': 'pin', 'number': '1'}
        # Dragged far from every attachment the vertex becomes a free grid point.
        tab._begin_gesture(('vertex', wire, 2), QPointF(0, 0))
        tab._move_gesture(('vertex', wire, 2), QPointF(2.6 * SCENE_UNITS_PER_MM, -2.5 * SCENE_UNITS_PER_MM))
        tab._end_gesture(True)
        assert tab.graphics()[wire]['primitives'][0]['points'][2] == [2.54, 2.54]
        assert tab._edit_graphic(0, 'move', (2.54, 0.0))
        assert _resolved(tab, wire)[2] == (2.54, 2.54), 'freed: the element no longer drags it'
        # The free corner moves on the grid, and Escape rolls the drag back.
        tab._begin_gesture(('vertex', wire, 1), QPointF(0, 0))
        tab._move_gesture(('vertex', wire, 1), QPointF(-2.6 * SCENE_UNITS_PER_MM, 0.1 * SCENE_UNITS_PER_MM))
        assert tab.graphics()[wire]['primitives'][0]['points'][1] == [-2.54, 0.0]
        tab._end_gesture(False)
        assert tab.graphics()[wire]['primitives'][0]['points'][1] == [0.0, 2.54]
        assert tab.move_wire_vertex(0, 0, 0, 0) is False, 'not a wire'
        assert tab.move_wire_vertex(wire, 9, 0, 0) is False, 'no such vertex'
    finally:
        tab.deleteLater()


# ── Adversarial review of the anchored wires (2026-10-10) ───────────────────

def _legacy_rows(count=3):
    legacy = g.instance_from_glyph(g.catalogue()[0], 0, 0)
    del legacy['id']
    return [{'pin': str(i), 'name': f'P{i}', 'symbol_graphics': [dict(legacy)]} for i in range(1, count + 1)]


def test_a_legacy_project_loads_clean_with_the_same_ids_in_baseline_and_draft(qapp):
    tab = SymbolTab()
    try:
        tab.set_data('Legacy', _legacy_rows(), pad_ids=['1', '2', '3'])
        assert not tab.has_pending_edits(), 'an untouched project is not dirty'
        assert tab.export_button.defaultAction().isEnabled() and not tab.apply_button.defaultAction().isEnabled()
        ident = tab.draft_rows()[0]['symbol_graphics'][0]['id']
        assert ident and all(row['symbol_graphics'][0]['id'] == ident for row in tab._baseline)
        tab.cancel_button.defaultAction().trigger()
        assert tab.draft_rows()[0]['symbol_graphics'][0]['id'] == ident, 'Cancel keeps the ids'
        # Negative: a real edit still makes the project dirty.
        assert tab._edit_graphic(0, 'move', (2.54, 0.0)) and tab.has_pending_edits()
    finally:
        tab.deleteLater()


def test_a_reload_without_the_pin_puts_the_wire_end_where_the_pin_last_was(qapp):
    tab = _tab()
    try:
        wire = _pin1_to_coil(tab)
        assert tab.place_pin('1', -7.62, 0.0), 'pin 1 moves to the row of pin 2 (swapped)'
        assert _resolved(tab, wire)[0] == (-5.08, 0.0)
        tab.apply()
        rows = tab.rows()
        start = rows[0]['symbol_graphics'][wire]['primitives'][0]['points'][0]
        assert (start['x'], start['y']) == (-5.08, 0.0), 'the applied rows carry the last resolved position'
        # Positive: a fresh editor without pin 1 (no previous layout) uses that position.
        other = SymbolTab()
        try:
            other.set_data('Board', [row for row in rows if row['pin'] != '1'], pad_ids=['2', '3', '4', '5'])
            assert _resolved(other, wire)[0] == (-5.08, 0.0), 'not where the wire was first drawn (-5.08, 2.54)'
            assert not other.has_pending_edits()
        finally:
            other.deleteLater()
        # Negative: with pin 1 present the end is still anchored and follows the pin.
        again = SymbolTab()
        try:
            again.set_data('Board', rows, pad_ids=['1', '2', '3', '4', '5'])
            point = again.graphics()[wire]['primitives'][0]['points'][0]
            assert point['anchor'] == {'kind': 'pin', 'number': '1'}
            assert again.place_pin('1', -7.62, -5.08)
            assert _resolved(again, wire)[0] == (-5.08, -5.08)
        finally:
            again.deleteLater()
    finally:
        tab.deleteLater()


def test_set_data_of_another_project_never_freezes_with_the_old_layout(qapp):
    tab = _tab()
    try:
        wire = _pin1_to_coil(tab)
        tab.apply()
        rows = [row for row in tab.rows() if row['pin'] != '1']
        tab.set_data('Other', rows, pad_ids=['2', '3', '4', '5'])
        point = tab.graphics()[wire]['primitives'][0]['points'][0]
        assert isinstance(point, dict) and point['anchor'] == {'kind': 'pin', 'number': '1'}, 'not frozen'
        assert _resolved(tab, wire)[0] == (-5.08, 2.54), 'the refreshed fallback stands in'
        assert not tab.has_pending_edits()
        # Negative: the same project does freeze the dropped pin (the existing contract).
        same = _tab()
        try:
            wire = _pin1_to_coil(same)
            same.apply()
            same.set_data('Board', [row for row in same.rows() if row['pin'] != '1'], pad_ids=['2', '3', '4', '5'])
            assert same.graphics()[wire]['primitives'][0]['points'][0] == [-5.08, 2.54]
        finally:
            same.deleteLater()
    finally:
        tab.deleteLater()


def test_rotate_and_mirror_are_refused_on_a_wire_and_kept_for_glyphs(qapp):
    tab = _tab()
    try:
        wire = _pin1_to_coil(tab)
        before = tab.draft_rows()
        assert tab._edit_graphic(wire, 'rotate', 90) is False
        assert tab._edit_graphic(wire, 'mirror') is False
        assert tab.draft_rows() == before, 'the wire is unchanged'
        assert tab._edit_graphic(0, 'rotate', 90) and tab.graphics()[0]['rotation'] == 90
        assert tab._edit_graphic(0, 'mirror') and tab.graphics()[0]['mirror'] is True
        # The context menu offers a wire only deletion.
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtCore import QPoint, QTimer
        seen = {}

        def capture():
            for menu in qapp.topLevelWidgets():
                if isinstance(menu, QMenu) and menu.isVisible():
                    seen['texts'] = [a.text() for a in menu.actions() if a.isEnabled() and not a.isSeparator()]
                    menu.close()
        QTimer.singleShot(0, capture)
        tab._graphic_context_menu(wire, tab.mapToGlobal(QPoint(10, 10)))
        assert seen['texts'] == ['Elimina elemento']
        QTimer.singleShot(0, capture)
        tab._graphic_context_menu(0, tab.mapToGlobal(QPoint(10, 10)))
        assert 'Ruota di 90 gradi (antiorario)' in seen['texts'] and 'Specchia' in seen['texts']
    finally:
        tab.deleteLater()


def test_a_projection_or_corner_landing_on_an_attachment_is_anchored(qapp):
    tab = _tab()
    try:
        tab.start_wire()
        # From a free point above the body: the click is 2.08 mm right of pin
        # 1's end, outside the snap, but its vertical projection lands on it.
        assert tab.add_wire_point(-5.08, 10.16)
        assert tab.add_wire_point(-3.0, 2.54)
        assert tab._wire_points[-1] == {'x': -5.08, 'y': 2.54, 'anchor': {'kind': 'pin', 'number': '1'}}
        tab.finish_wire(False)
        # Negative: a projection one grid step (2.54 mm) from the pin end
        # stays a free point: only exact coincidence anchors.
        tab.start_wire()
        assert tab.add_wire_point(-2.54, 10.16) and tab.add_wire_point(-1.0, 2.54)
        assert tab._wire_points[-1] == [-2.54, 2.54]
        tab.finish_wire(False)
        # The corner computed for an anchored target is anchored too when it
        # hits a pin end: from above pin 1 into pin 4 (right side, horizontal)
        # the corner (-5.08, 0) is pin 2's end.
        tab.start_wire()
        assert tab.add_wire_point(-5.08, 10.16) and tab.add_wire_point(5.08, 0.0)
        assert tab._wire_points[1] == {'x': -5.08, 'y': 0.0, 'anchor': {'kind': 'pin', 'number': '2'}}
        tab.finish_wire(False)
    finally:
        tab.deleteLater()


def test_dragging_a_whole_wire_moves_it_or_stretches_it_between_its_anchors(qapp):
    tab = _tab()
    try:
        # MOVE: no anchor, every vertex translates.
        tab.start_wire()
        assert tab.add_wire_point(0, 10.16) and tab.add_wire_point(5.08, 12.7)
        assert tab.finish_wire(True)
        loose = len(tab.graphics()) - 1
        tab._begin_gesture(('graphic', loose), QPointF(0, 0))
        tab._move_gesture(('graphic', loose), QPointF(2.54 * SCENE_UNITS_PER_MM, 2.54 * SCENE_UNITS_PER_MM))
        tab._end_gesture(True)
        assert _resolved(tab, loose) == ((2.54, 7.62), (7.62, 7.62))
        # STRETCH: anchored ends stay, the free corner follows the drag.
        wire = _pin1_to_coil(tab)
        tab._begin_gesture(('graphic', wire), QPointF(0, 0))
        tab._move_gesture(('graphic', wire), QPointF(2.54 * SCENE_UNITS_PER_MM, 0))
        tab._end_gesture(True)
        assert _resolved(tab, wire) == ((-5.08, 2.54), (2.54, 2.54), (0.0, 5.08))
        hints = {item.toolTip() for item in tab.view.scene().items() if item.data(0) == ('graphic', wire)}
        assert hints == {'Filo: trascinalo per spostarlo; se ha estremi agganciati si spostano solo i vertici liberi.'}
    finally:
        tab.deleteLater()


def test_the_corner_enters_the_attachment_along_the_pin_axis(qapp):
    tab = _tab()
    try:
        tab.start_wire()
        # Into pin 1 (left side, angle 0, horizontal): the last segment is horizontal.
        assert tab.add_wire_point(0, 10.16) and tab.add_wire_point(-5.08, 2.54)
        assert [tab._xy(p) for p in tab._wire_points] == [(0.0, 10.16), (0.0, 2.54), (-5.08, 2.54)]
        tab.finish_wire(False)
        # Into the coil's top terminal (angle 270, vertical): the last segment is vertical.
        assert tab.place_glyph('relay_coil', 2.54, 0)
        tab.start_wire()
        assert tab.add_wire_point(-2.54, 10.16) and tab.add_wire_point(2.54, 5.08)
        assert [tab._xy(p) for p in tab._wire_points] == [(-2.54, 10.16), (2.54, 10.16), (2.54, 5.08)]
        tab.finish_wire(False)
        # Turned by 90 degrees the same terminal is horizontal: the corner flips.
        assert tab._edit_graphic(0, 'rotate', 90)
        end = g.terminal_inner_end(g.placed_terminals(tab.graphics()[0])[0])
        tab.start_wire()
        assert tab.add_wire_point(-7.62, 10.16) and tab.add_wire_point(*end)
        assert [tab._xy(p) for p in tab._wire_points] == [(-7.62, 10.16), (-7.62, end[1]), end]
        tab.finish_wire(False)
    finally:
        tab.deleteLater()


def test_a_wire_clicked_on_the_coil_dot_follows_the_coil(qapp):
    """User, 2026-10-10 (VE12H-K): the wire end at the coil's dot (-5.08, 10.16) was stored free."""
    tab = _tab()
    try:
        assert tab.place_glyph('relay_coil', -5.08, 2.54)
        coil = tab.graphics()[0]
        dot = g.placed_terminals(coil)[0]
        assert (dot.x, dot.y) == (-5.08, 10.16)
        tab.start_wire()
        assert tab.add_wire_point(-15.24, 10.16)
        assert tab.add_wire_point(-5.1, 10.2), 'near the dot, within the snap'
        assert tab.finish_wire()
        wire = len(tab.graphics()) - 1
        end = tab.graphics()[wire]['primitives'][0]['points'][-1]
        assert end['anchor'] == {'kind': 'terminal', 'element': coil['id'], 'index': 0, 'end': 'outer'}
        assert _resolved(tab, wire)[-1] == (-5.08, 10.16)
        assert tab._edit_graphic(0, 'move', (2.54, 0.0))
        assert _resolved(tab, wire)[-1] == (2.54, 7.62), 'the wire end followed the coil (placed at 2.54, 0)'
    finally:
        tab.deleteLater()


def test_a_project_with_free_points_on_attachments_opens_anchored_and_clean(qapp):
    """Wires saved before anchors existed are adopted at load without dirtying the project."""
    tab = _tab()
    try:
        assert tab.place_glyph('relay_coil', -5.08, 2.54)
        coil = tab.graphics()[0]
        legacy = {'id': 'w1', 'key': 'wire', 'label': 'Filo', 'source': 'wire', 'x': 0.0, 'y': 0.0,
                  'rotation': 0, 'mirror': False, 'terminals': [],
                  'primitives': [{'kind': 'polyline', 'points': [[-12.7, 10.16], [-5.08, 10.16]],
                                  'radius': 0.0, 'fill': 'none', 'width': 0.254}]}
        rows = tab._with_graphics(tab.draft_rows(), [coil, legacy])
        tab.set_data('Board', rows, pad_ids=['1', '2', '3', '4', '5'])
        assert not tab.has_pending_edits(), 'adopted before the baseline'
        points = tab.graphics()[1]['primitives'][0]['points']
        assert points[0] == [-12.7, 10.16], 'free point away from any attachment'
        assert points[1]['anchor'] == {'kind': 'terminal', 'element': coil['id'], 'index': 0, 'end': 'outer'}
        assert tab._edit_graphic(0, 'move', (2.54, 0.0))
        assert _resolved(tab, 1)[-1] == (2.54, 7.62)
    finally:
        tab.deleteLater()
