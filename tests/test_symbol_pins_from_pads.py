"""@brief Symbol pins are exactly the footprint pads; the pinout table only names them."""
import ast
from pathlib import Path

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.generators.symbol_pins import name_tokens, pinout_on_pads
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS

ROOT = Path(__file__).resolve().parents[1]

#: Real case (relay T7N): pads read from the drawing, table numbered by the analysis.
T7N_PADS = ['11', '12', '14', 'A1', 'A2']
T7N_PINOUT = [
    {'pin': 1, 'name': 'Coil_A1'}, {'pin': 2, 'name': 'Coil_A2'},
    {'pin': 3, 'name': 'Common_11'}, {'pin': 4, 'name': 'NC_14'}, {'pin': 5, 'name': 'NO_12'},
]


def _names(result):
    return {row['pin']: row['name'] for row in result.rows}


def test_t7n_table_with_invented_numbers_gives_five_named_pins():
    result = pinout_on_pads(T7N_PINOUT, T7N_PADS)
    assert [row['pin'] for row in result.rows] == T7N_PADS
    assert _names(result) == {'11': 'Common_11', '12': 'NO_12', '14': 'NC_14',
                              'A1': 'Coil_A1', 'A2': 'Coil_A2'}
    assert result.unmatched == []
    # The heuristic is reported, never silent.
    assert len(result.by_name) == 5 and len(result.notes) == 1
    pins, body = symbol_inputs(result.rows)
    text = serialize_symbol('T7N', pins, footprint='KiCadGen:T7N', body=body)
    assert text.count('(pin ') == 5
    for number in ('"1"', '"2"', '"3"', '"4"', '"5"'):
        assert f'(number {number}' not in text


def test_exact_number_match_wins_and_keeps_the_electrical_type():
    pinout = [{'pin': 1, 'name': 'VCC', 'electrical_type': 'power_in'},
              {'pin': '2', 'name': 'GND', 'electrical_type': 'power_in'},
              {'pin': 3, 'name': 'OUT'}]
    result = pinout_on_pads(pinout, ['1', '2', '3'])
    assert _names(result) == {'1': 'VCC', '2': 'GND', '3': 'OUT'}
    assert result.rows[0]['electrical_type'] == 'power_in'
    assert result.by_name == [] and result.unmatched == [] and result.notes == []


def test_number_match_is_not_overridden_by_a_name_token():
    # Negative of the heuristic: the number is a pad, so the name is not used to move it.
    result = pinout_on_pads([{'pin': 1, 'name': 'OUT_2'}, {'pin': 2, 'name': 'IN'}], ['1', '2'])
    assert _names(result) == {'1': 'OUT_2', '2': 'IN'}
    assert len(result.notes) == 1 and 'pad 2' in str(result.notes[0])


def test_name_matching_two_pads_is_not_placed_and_is_reported():
    result = pinout_on_pads([{'pin': 9, 'name': 'Bridge_A1_A2'}], ['A1', 'A2'])
    assert _names(result) == {'A1': '', 'A2': ''}
    assert [entry['name'] for entry in result.unmatched] == ['Bridge_A1_A2']
    assert any('Bridge_A1_A2' in str(note) for note in result.notes)


def test_two_entries_claiming_the_same_pad_by_name_are_both_left_out():
    result = pinout_on_pads([{'pin': 7, 'name': 'Coil_A1'}, {'pin': 8, 'name': 'Drive_A1'}], ['A1', 'A2'])
    assert _names(result) == {'A1': '', 'A2': ''}
    assert len(result.unmatched) == 2


def test_token_must_be_whole_never_fuzzy():
    # «IN1» is one token: it does not name pad 1; «a1» is not «A1».
    result = pinout_on_pads([{'pin': 9, 'name': 'IN1'}, {'pin': 10, 'name': 'coil_a1'}], ['1', 'A1'])
    assert _names(result) == {'1': '', 'A1': ''}
    assert len(result.unmatched) == 2
    assert name_tokens('Common_11') == ('Common', '11')
    assert name_tokens('IN1') == ('IN1',)


def test_extra_pinout_entries_never_become_pins():
    pinout = [{'pin': 1, 'name': 'A'}, {'pin': 2, 'name': 'B'}, {'pin': 3, 'name': 'C'}]
    result = pinout_on_pads(pinout, ['1', '2'])
    assert [row['pin'] for row in result.rows] == ['1', '2']
    assert [entry['pin'] for entry in result.unmatched] == [3]
    assert '3 C' in str(result.notes[-1])


def test_pads_without_pinout_get_an_empty_name():
    result = pinout_on_pads([], ['1', '2', 'EP'])
    assert result.rows == [{'pin': '1', 'name': ''}, {'pin': '2', 'name': ''},
                           {'pin': 'EP', 'name': ''}]
    assert result.notes == []
    pins, _body = symbol_inputs(result.rows)
    assert '(name "~"' in serialize_symbol('X', pins)


def test_pads_sharing_a_number_give_one_pin():
    result = pinout_on_pads([{'pin': 'GND', 'name': 'GND'}, {'pin': 'GND', 'name': 'GND'}],
                            ['1', 'GND', 'GND', '2'])
    assert [row['pin'] for row in result.rows] == ['1', 'GND', '2']
    assert result.unmatched == []


def test_conflicting_duplicate_entry_is_reported():
    result = pinout_on_pads([{'pin': 1, 'name': 'A'}, {'pin': 1, 'name': 'B'}], ['1'])
    assert _names(result) == {'1': 'A'}
    assert [entry['name'] for entry in result.unmatched] == ['B']


def test_unknown_footprint_keeps_the_pinout_unchanged():
    result = pinout_on_pads(T7N_PINOUT, [])
    assert [row['pin'] for row in result.rows] == [1, 2, 3, 4, 5]
    assert result.notes == []


def test_every_note_template_has_four_languages():
    tree = ast.parse((ROOT / 'kicadgen' / 'generators' / 'symbol_pins.py').read_text(encoding='utf-8'))
    templates = [node.args[0].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == '_message']
    assert len(templates) == 3
    for template in templates:
        entry = DEFAULT_TRANSLATIONS['ui:' + template]
        assert set(entry) >= {'it', 'en', 'fr', 'de'} and all(entry[lang] for lang in ('it', 'en', 'fr', 'de'))
