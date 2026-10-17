"""@brief Reference and Value of the symbol can be moved and keep their place in the file (user, 2026-10-13).

The name above the body is KiCad's «Value» property; «Reference» («U?») is
the designator. Both are drawn by the editor where the file puts them and
can be dragged on the pin grid; the chosen position travels on every row
(`symbol_properties`, like `symbol_body`) and ends in the property's
`(at x y 0)`. A symbol without a choice keeps the positions of before.

CONTRACT CHANGED 2026-10-16 (user): the position is the bottom-left anchor
of the text, `(justify left bottom)` in the file; centres (defaults and
positions stored before) are converted with KiCad's text geometry, see
tests/test_symbol_property_anchor.py.
"""
from copy import deepcopy

import pytest

from kicadgen.generators.symbol import (
    PROPERTIES_KEY, PROPERTY_ANCHOR_BOTTOM_LEFT, PROPERTY_ANCHOR_FIELD, TEXT_SIZE, SymbolPin,
    default_property_positions, layout_symbol, property_anchor_from_centre, property_anchors, property_texts,
    serialize_symbol, symbol_inputs, symbol_property_positions,
)

PINS = [SymbolPin('1', 'A', 'passive', 'left'), SymbolPin('2', 'B', 'passive', 'right')]
ROWS = [{'pin': '1', 'name': 'A'}, {'pin': '2', 'name': 'B'}]


def _property_line(text, key):
    return next(line for line in text.splitlines() if line.strip().startswith(f'(property "{key}"'))


def _num(value):
    return f'{value:.4f}'.rstrip('0').rstrip('.') or '0'


def _at(point):
    return f'(at {_num(point[0])} {_num(point[1])} 0)'


def _anchor(key, centre, name='Board'):
    """Bottom-left anchor of a property KiCad draws centred at `centre`."""
    return property_anchor_from_centre(property_texts('U', name)[key], TEXT_SIZE, *centre)


def _snap(point):
    return tuple(round(round(v / 2.54) * 2.54, 4) for v in point)


# ── Generator ────────────────────────────────────────────────────────

def test_without_a_choice_the_file_keeps_the_positions_of_before():
    layout = layout_symbol(PINS)
    h = layout.height / 2
    text = serialize_symbol('Board', PINS)
    # CONTRACT CHANGED 2026-10-17 (user, «Predefinito centrato»): an unmoved
    # property is a centred KiCad text at its default centre, no justify.
    assert _at((0, h + 2.54)) in _property_line(text, 'Value')
    assert _at((0, h + 5.08)) in _property_line(text, 'Reference')
    assert 'justify' not in _property_line(text, 'Value') + _property_line(text, 'Reference')
    assert default_property_positions(layout) == {'Reference': (0.0, round(h + 5.08, 4)),
                                                  'Value': (0.0, round(h + 2.54, 4))}


def test_a_chosen_position_is_written_and_the_other_property_keeps_its_default():
    layout = layout_symbol(PINS)
    text = serialize_symbol('Board', PINS, properties={'Value': (7.62, -10.16)})
    assert _at(_anchor('Value', (7.62, -10.16))) in _property_line(text, 'Value'), 'a plain mapping holds centres'
    assert '(justify left bottom)' in _property_line(text, 'Value'), 'the chosen one is anchored'
    assert _at(default_property_positions(layout)['Reference']) in _property_line(text, 'Reference')
    assert 'justify' not in _property_line(text, 'Reference'), 'the unmoved one stays centred'
    assert '(at 0 0 0)' in _property_line(text, 'Footprint'), 'hidden properties stay at the origin'


def test_rows_carry_the_positions_like_the_body():
    rows = [dict(row, **{PROPERTIES_KEY: {'Reference': [2.54, 12.7]}}) for row in ROWS]
    assert symbol_property_positions(rows) == {'Reference': (2.54, 12.7)}
    assert symbol_property_positions(ROWS) == {}, 'no key: no choice'
    assert symbol_property_positions([dict(ROWS[0], **{PROPERTIES_KEY: {}}), ROWS[1]]) == {}
    pins, body = symbol_inputs(rows)
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(rows))
    assert _at(_anchor('Reference', (2.54, 12.7))) in _property_line(text, 'Reference'), 'a legacy centre'
    marked = [dict(row, **{PROPERTIES_KEY: {'Reference': [2.54, 12.7], PROPERTY_ANCHOR_FIELD: PROPERTY_ANCHOR_BOTTOM_LEFT}})
              for row in ROWS]
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(marked))
    assert '(at 2.54 12.7 0)' in _property_line(text, 'Reference'), 'an anchor as stored'



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
    # CONTRACT CHANGED 2026-10-16 (user): drawn from the bottom-left anchor
    # the file writes, like the drawn texts, and (later the same day) as
    # KiCad draws from it: cells half the pen margin to the right, baseline
    # 0.125 x size above (gui/kicad_text_item.py).
    from kicadgen.core.kicad_text import kicad_text_cells
    from kicadgen.generators.symbol import TEXT_SIZE
    anchors = property_anchors(tab._symbol, property_texts('U', 'Board'))
    from kicadgen.gui.symbol_tab import SymbolTab
    for key, item in items.items():
        left, up = kicad_text_cells(item.text(), TEXT_SIZE)
        assert item.sceneBoundingRect().left() == pytest.approx(
            (anchors[key][0] + left) * SCENE_UNITS_PER_MM, abs=0.05)
        baseline = item.mapToScene(0, SymbolTab._text_baseline(item.text(), item.font())).y()
        assert baseline == pytest.approx(-(anchors[key][1] + up) * SCENE_UNITS_PER_MM, abs=0.05)


def test_the_reference_text_follows_the_designator(tab):
    tab.set_reference('RV')
    assert _property_items(tab)['Reference'].text() == 'RV?'
    tab.set_reference('')
    assert _property_items(tab)['Reference'].text() == 'U?', 'blank falls back to U'


def test_dragging_the_value_snaps_to_the_grid_and_reaches_the_rows_and_the_file(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    defaults = tab._property_positions(tab.draft_rows(), tab._symbol)
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    # 7.9 mm right, 10.3 mm down (scene y grows downwards): the anchor snapped on the grid.
    tab._move_gesture(('property', 'Value'), QPointF(7.9 * SCENE_UNITS_PER_MM, 10.3 * SCENE_UNITS_PER_MM))
    tab._end_gesture(True)
    expected = _snap((defaults['Value'][0] + 7.9, defaults['Value'][1] - 10.3))
    assert all(row[PROPERTIES_KEY]['Value'] == list(expected) for row in tab.draft_rows())
    # CONTRACT CHANGED 2026-10-15 (review M3): the untouched property is no
    # longer written with its default. Persisting it froze it there, so the
    # default rule (which lifts the texts above top pins) never reached it
    # again; now only the moved key is stored and the other one follows the
    # default, which still draws it at the same place today.
    assert all(set(row[PROPERTIES_KEY]) == {'Value', PROPERTY_ANCHOR_FIELD} for row in tab.draft_rows()), \
        'only the moved property is written'
    assert tab._property_positions(tab.draft_rows(), tab._symbol)['Reference'] == defaults['Reference']
    assert tab.has_pending_edits()
    tab.apply()
    pins, body = symbol_inputs(tab.rows())
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(tab.rows()))
    assert _at(expected) in _property_line(text, 'Value')
    item = _property_items(tab)['Value']
    from kicadgen.core.kicad_text import kicad_text_cells
    from kicadgen.generators.symbol import TEXT_SIZE
    left = kicad_text_cells(item.text(), TEXT_SIZE)[0]
    assert item.sceneBoundingRect().left() == pytest.approx((expected[0] + left) * SCENE_UNITS_PER_MM, abs=0.05)


def test_dragging_the_reference_moves_only_the_reference(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    defaults = tab._property_positions(tab.draft_rows(), tab._symbol)
    tab._begin_gesture(('property', 'Reference'), QPointF(0, 0))
    tab._move_gesture(('property', 'Reference'), QPointF(-5.08 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(True)
    chosen = symbol_property_positions(tab.draft_rows())
    assert chosen['Reference'] == _snap((defaults['Reference'][0] - 5.08, defaults['Reference'][1]))
    # CONTRACT CHANGED 2026-10-15 (review M3): Value is not persisted with its
    # default any more; it is absent and keeps following the default rule.
    assert 'Value' not in chosen
    assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == defaults['Value']


def test_undo_restores_the_position_and_reset_layout_forgets_it(tab):
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    x = tab._property_positions(tab.draft_rows(), tab._symbol)['Value'][0]
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(5.08 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(True)
    moved = _snap((x + 5.08, 0))[0]
    assert symbol_property_positions(tab.draft_rows())['Value'][0] == moved
    tab.undo()
    assert symbol_property_positions(tab.draft_rows()) == {}
    tab.redo()
    assert symbol_property_positions(tab.draft_rows())['Value'][0] == moved
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


# ── Review M3 (2026-10-15): only the moved key is persisted ──────────

def _drag(tab, key, dx_mm, dy_mm):
    """Drag a property by (dx, dy) mm in symbol coordinates (y up) and commit."""
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    tab._begin_gesture(('property', key), QPointF(0, 0))
    # Scene y grows downwards: a positive symbol dy is a negative scene dy.
    tab._move_gesture(('property', key), QPointF(dx_mm * SCENE_UNITS_PER_MM, -dy_mm * SCENE_UNITS_PER_MM))
    tab._end_gesture(True)


def test_after_moving_the_reference_the_value_still_follows_the_default_rule(tab):
    # Positive: the user drags Reference, then adds a top pin. Value was not
    # moved, so it must clear the new pin; Reference stays where it was put.
    _drag(tab, 'Reference', -5.08, 0)
    moved = symbol_property_positions(tab.draft_rows())['Reference']
    assert tab.move_pin('2', 'top', 0)
    lifted = default_property_positions(tab._symbol)
    tip = max(p.y for p in tab._symbol.pins if p.angle == 270)
    assert lifted['Value'][1] >= tip + 2.54 - 1e-7, 'precondition: the rule lifts Value above the top pin'
    positions = tab._property_positions(tab.draft_rows(), tab._symbol)
    assert positions['Value'] == _anchor('Value', lifted['Value']), 'the unmoved Value follows the lifted default'
    assert positions['Reference'] == moved, 'the moved Reference keeps the user choice'
    pins, body = symbol_inputs(tab.draft_rows())
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(tab.draft_rows()))
    assert _at(lifted['Value']) in _property_line(text, 'Value'), 'written centred at the lifted default'


def test_a_property_moved_by_the_user_does_not_follow_the_default_rule(tab):
    # Negative: the property the user DID move is a choice and stays put
    # when a top pin would lift the default.
    _drag(tab, 'Value', 7.62, 0)
    chosen = symbol_property_positions(tab.draft_rows())['Value']
    assert tab.move_pin('2', 'top', 0)
    assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == chosen
    assert chosen != _anchor('Value', default_property_positions(tab._symbol)['Value'])


def test_a_second_drag_keeps_the_first_choice_and_adds_only_its_own_key(tab):
    _drag(tab, 'Reference', -5.08, 0)
    first = symbol_property_positions(tab.draft_rows())
    _drag(tab, 'Value', 5.08, 0)
    both = symbol_property_positions(tab.draft_rows())
    assert set(both) == {'Reference', 'Value'}
    assert both['Reference'] == first['Reference']
    tab.undo()
    assert symbol_property_positions(tab.draft_rows()) == first, 'undo returns to the single-key choice'
    tab.redo()
    assert symbol_property_positions(tab.draft_rows()) == both


def test_dragging_a_property_back_onto_its_default_writes_nothing(tab):
    # A drag that ends where it started (or on the default) must not pin
    # the text: the rows keep no `symbol_properties`.
    before = tab.draft_rows()
    _drag(tab, 'Value', 0.3, -0.2)   # snaps back onto the default
    assert tab.draft_rows() == before
    assert all(PROPERTIES_KEY not in row for row in tab.draft_rows())


def test_a_project_holding_both_keys_is_left_as_it_is(qapp):
    # Projects saved by the old code carry both keys although one was only
    # the default; they cannot be told apart from an explicit choice, so a
    # new drag of one key keeps the other one untouched.
    from kicadgen.gui.symbol_tab import SymbolTab
    legacy = {'Reference': [0.0, 10.16], 'Value': [0.0, 7.62]}
    rows = [dict(row, **{PROPERTIES_KEY: deepcopy(legacy)}) for row in ROWS]
    tab = SymbolTab()
    try:
        tab.set_data('Board', rows, 'KiCadGen:Board', ['1', '2'])
        _drag(tab, 'Reference', -5.08, 0)
        chosen = symbol_property_positions(tab.draft_rows())
        # CONTRACT CHANGED 2026-10-16: kept where KiCad drew it, now as the
        # anchor of that centre (converted once, in the same write).
        assert chosen['Value'] == _anchor('Value', (0.0, 7.62))
        reference = _anchor('Reference', (0.0, 10.16))
        assert chosen['Reference'] == _snap((reference[0] - 5.08, reference[1]))
    finally:
        tab.deleteLater()


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
    x = tab._property_positions(tab.draft_rows(), tab._symbol)['Value'][0]
    tab._begin_gesture(('property', 'Value'), QPointF(0, 0))
    tab._move_gesture(('property', 'Value'), QPointF(7.62 * SCENE_UNITS_PER_MM, 0))
    tab._end_gesture(True)
    tab.apply()
    chosen = symbol_property_positions(tab.rows())['Value']
    assert chosen[0] == _snap((x + 7.62, 0))[0]
    target = tmp_path / 'moved.kicad_sym'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    window._export_electrical_symbol()
    assert _at(chosen) in _property_line(target.read_text(encoding='utf-8'), 'Value')
