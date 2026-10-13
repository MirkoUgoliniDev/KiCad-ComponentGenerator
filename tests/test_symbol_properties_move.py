"""@brief Reference and Value of the symbol can be moved and keep their place in the file (user, 2026-10-13).

The name above the body is KiCad's «Value» property; «Reference» («U?») is
the designator. Both are drawn by the editor where the file puts them and
can be dragged on the pin grid; the chosen position travels on every row
(`symbol_properties`, like `symbol_body`) and ends in the property's
`(at x y 0)`. A symbol without a choice keeps the positions of before.
"""
from copy import deepcopy

import pytest

from kicadgen.generators.symbol import (
    PROPERTIES_KEY, SymbolPin, default_property_positions, layout_symbol, serialize_symbol,
    symbol_inputs, symbol_property_positions,
)

PINS = [SymbolPin('1', 'A', 'passive', 'left'), SymbolPin('2', 'B', 'passive', 'right')]
ROWS = [{'pin': '1', 'name': 'A'}, {'pin': '2', 'name': 'B'}]


def _property_line(text, key):
    return next(line for line in text.splitlines() if line.strip().startswith(f'(property "{key}"'))


# ── Generator ────────────────────────────────────────────────────────

def test_without_a_choice_the_file_keeps_the_positions_of_before():
    layout = layout_symbol(PINS)
    h = layout.height / 2
    text = serialize_symbol('Board', PINS)
    assert f'(at 0 {h + 2.54:.4f}'.rstrip('0') in _property_line(text, 'Value')
    assert f'(at 0 {h + 5.08:.4f}'.rstrip('0') in _property_line(text, 'Reference')
    assert default_property_positions(layout) == {'Reference': (0.0, round(h + 5.08, 4)),
                                                  'Value': (0.0, round(h + 2.54, 4))}


def test_a_chosen_position_is_written_and_the_other_property_keeps_its_default():
    layout = layout_symbol(PINS)
    text = serialize_symbol('Board', PINS, properties={'Value': (7.62, -10.16)})
    assert '(at 7.62 -10.16 0)' in _property_line(text, 'Value')
    assert f'(at 0 {default_property_positions(layout)["Reference"][1]:g} 0)' in _property_line(text, 'Reference')
    assert '(at 0 0 0)' in _property_line(text, 'Footprint'), 'hidden properties stay at the origin'


def test_rows_carry_the_positions_like_the_body():
    rows = [dict(row, **{PROPERTIES_KEY: {'Reference': [2.54, 12.7]}}) for row in ROWS]
    assert symbol_property_positions(rows) == {'Reference': (2.54, 12.7)}
    assert symbol_property_positions(ROWS) == {}, 'no key: no choice'
    assert symbol_property_positions([dict(ROWS[0], **{PROPERTIES_KEY: {}}), ROWS[1]]) == {}
    pins, body = symbol_inputs(rows)
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(rows))
    assert '(at 2.54 12.7 0)' in _property_line(text, 'Reference')


@pytest.mark.parametrize('bad', [
    [{'Value': [0, 1]}, {'Value': [0, 2]}],          # rows disagree
    [{'Footprint': [0, 1]}] * 2,                      # not a visible property
    [{'Value': [0]}] * 2,                             # not a pair
    [{'Value': [0, float('nan')]}] * 2,               # not finite
    [{'Value': [0, True]}] * 2,                       # a boolean is not a number
    [['Value', 0, 1]] * 2,                            # not a mapping
])
def test_malformed_or_inconsistent_positions_are_refused(bad):
    rows = [dict(row, **{PROPERTIES_KEY: value}) for row, value in zip(ROWS, bad)]
    with pytest.raises(ValueError):
        symbol_property_positions(rows)


def test_serialize_refuses_an_unknown_property_key():
    with pytest.raises(ValueError):
        serialize_symbol('Board', PINS, properties={'Datasheet': (0, 0)})


# ── Editor ───────────────────────────────────────────────────────────

@pytest.fixture
def tab(qapp):
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    tab.set_data('Board', deepcopy(ROWS), 'KiCadGen:Board', ['1', '2'])
    yield tab
    tab.deleteLater()


def _property_items(tab):
    return {item.data(0)[1]: item for item in tab.view.scene().items()
            if item.data(0) and item.data(0)[0] == 'property'}


def test_both_properties_are_drawn_where_the_file_puts_them(tab):
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    items = _property_items(tab)
    assert set(items) == {'Reference', 'Value'}
    assert items['Value'].text() == 'Board' and items['Reference'].text() == 'U?'
    defaults = default_property_positions(tab._symbol)
    for key, item in items.items():
        centre = item.sceneBoundingRect().center()
        assert centre.x() == pytest.approx(defaults[key][0] * SCENE_UNITS_PER_MM, abs=1)
        assert centre.y() == pytest.approx(-defaults[key][1] * SCENE_UNITS_PER_MM, abs=1)


def test_the_reference_text_follows_the_designator(tab):
    tab.set_reference('RV')
    assert _property_items(tab)['Reference'].text() == 'RV?'
    tab.set_reference('')
    assert _property_items(tab)['Reference'].text() == 'U?', 'blank falls back to U'


def test_dragging_the_value_snaps_to_the_grid_and_reaches_the_rows_and_the_file(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    defaults = default_property_positions(tab._symbol)
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    # 7.9 mm right, 10.3 mm down (scene y grows downwards): snapped to 7.62, -10.16 + default.
    tab._move_gesture(('property', 'Value'), QPointF(7.9 * SCENE_UNITS_PER_MM, 10.3 * SCENE_UNITS_PER_MM))
    tab._end_gesture(True)
    expected = (7.62, round(round((defaults['Value'][1] - 10.3) / 2.54) * 2.54, 4))
    assert all(row[PROPERTIES_KEY]['Value'] == list(expected) for row in tab.draft_rows())
    assert all(tuple(row[PROPERTIES_KEY]['Reference']) == defaults['Reference'] for row in tab.draft_rows()), \
        'the untouched property keeps its default, written explicitly'
    assert tab.has_pending_edits()
    tab.apply()
    pins, body = symbol_inputs(tab.rows())
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(tab.rows()))
    assert f'(at {expected[0]:g} {expected[1]:g} 0)' in _property_line(text, 'Value')
    item = _property_items(tab)['Value']
    assert item.sceneBoundingRect().center().x() == pytest.approx(expected[0] * SCENE_UNITS_PER_MM, abs=1)


def test_dragging_the_reference_moves_only_the_reference(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    defaults = default_property_positions(tab._symbol)
    tab._begin_gesture(('property', 'Reference'), QPointF(0, 0))
    tab._move_gesture(('property', 'Reference'), QPointF(-5.08 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(True)
    chosen = symbol_property_positions(tab.draft_rows())
    assert chosen['Reference'] == (-5.08, defaults['Reference'][1])
    assert chosen['Value'] == defaults['Value']


def test_undo_restores_the_position_and_reset_layout_forgets_it(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(5.08 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(True)
    assert symbol_property_positions(tab.draft_rows())['Value'][0] == 5.08
    tab.undo()
    assert symbol_property_positions(tab.draft_rows()) == {}
    tab.redo()
    assert symbol_property_positions(tab.draft_rows())['Value'][0] == 5.08
    tab.reset_layout()
    assert all(PROPERTIES_KEY not in row for row in tab.draft_rows())


def test_a_cancelled_drag_leaves_the_rows_untouched(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    before = tab.draft_rows()
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(5.08 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(False)
    assert tab.draft_rows() == before


def test_move_property_refuses_an_unknown_key(tab):
    assert not tab.move_property('Footprint', 0, 0)
    assert symbol_property_positions(tab.draft_rows()) == {}


def test_property_texts_do_not_enter_the_pin_name_overlap_check(tab):
    assert all(number in ('1', '2') for number, _rect in tab._text_bounds)


# ── Main window ──────────────────────────────────────────────────────

def test_the_exported_symbol_carries_the_dragged_position(window, monkeypatch, tmp_path):
    """End to end: drag in the tab, apply, export from the window, read the file."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtWidgets import QFileDialog
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    from test_symbol_integration import setup_board
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(7.62 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(True)
    tab.apply()
    chosen = symbol_property_positions(tab.rows())['Value']
    assert chosen[0] == 7.62
    target = tmp_path / 'moved.kicad_sym'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    window._export_electrical_symbol()
    assert f'(at {chosen[0]:g} {chosen[1]:g} 0)' in _property_line(target.read_text(encoding='utf-8'), 'Value')
