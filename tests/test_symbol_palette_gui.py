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
        assert tab._wire_points[0] == list(pin_end) and tab._wire_points[-1] == list(terminal_end)
        assert tab.finish_wire(True)
        assert not tab.view.wiring and not tab.wire_button.defaultAction().isChecked()
        wire = tab.graphics()[-1]
        assert wire['key'] == 'wire' and wire['primitives'][0]['points'][0] == list(pin_end)
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
