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
    # CONTRACT CHANGED 2026-10-15 (texts at KiCad's size, user): vertically
    # the anchor is the middle of the CAPITALS, as KiCad centres a text, not
    # the middle of Qt's line box (which also holds the descender space and
    # sits ~0.12 mm lower at 1.27 mm).
    from PyQt6.QtGui import QPainterPath
    from kicadgen.gui.symbol_tab import SymbolTab
    for key, item in items.items():
        centre = item.sceneBoundingRect().center()
        assert centre.x() == pytest.approx(defaults[key][0] * SCENE_UNITS_PER_MM, abs=1)
        outline = QPainterPath()
        outline.addText(0.0, 0.0, item.font(), 'H')
        cap = outline.boundingRect().height() * item.scale()
        baseline = item.mapToScene(0, SymbolTab._text_baseline(item.text(), item.font())).y()
        assert baseline - cap / 2 == pytest.approx(-defaults[key][1] * SCENE_UNITS_PER_MM, abs=0.05)


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
    # CONTRACT CHANGED 2026-10-15 (review M3): the untouched property is no
    # longer written with its default. Persisting it froze it there, so the
    # default rule (which lifts the texts above top pins) never reached it
    # again; now only the moved key is stored and the other one follows the
    # default, which still draws it at the same place today.
    assert all(set(row[PROPERTIES_KEY]) == {'Value'} for row in tab.draft_rows()), \
        'only the moved property is written'
    assert tab._property_positions(tab.draft_rows(), tab._symbol)['Reference'] == defaults['Reference']
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
    # CONTRACT CHANGED 2026-10-15 (review M3): Value is not persisted with its
    # default any more; it is absent and keeps following the default rule.
    assert 'Value' not in chosen
    assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == defaults['Value']


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
    assert positions['Value'] == lifted['Value'], 'the unmoved Value follows the lifted default'
    assert positions['Reference'] == moved, 'the moved Reference keeps the user choice'
    pins, body = symbol_inputs(tab.draft_rows())
    text = serialize_symbol('Board', pins, body=body, properties=symbol_property_positions(tab.draft_rows()))
    assert f'(at 0 {lifted["Value"][1]:g} 0)' in _property_line(text, 'Value')


def test_a_property_moved_by_the_user_does_not_follow_the_default_rule(tab):
    # Negative: the property the user DID move is a choice and stays put
    # when a top pin would lift the default.
    _drag(tab, 'Value', 7.62, 0)
    chosen = symbol_property_positions(tab.draft_rows())['Value']
    assert tab.move_pin('2', 'top', 0)
    assert tab._property_positions(tab.draft_rows(), tab._symbol)['Value'] == chosen
    assert chosen != default_property_positions(tab._symbol)['Value']


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
        assert chosen['Value'] == (0.0, 7.62)
        assert chosen['Reference'] == (-5.08, 10.16)
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
