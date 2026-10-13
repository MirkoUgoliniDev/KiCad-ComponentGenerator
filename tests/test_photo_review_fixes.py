"""@brief Regression tests for review D: provider numbers, optional extractor fields, crop contract.

Every test runs offline: the paid calls are fakes that build the answer the
provider would send, and no network or credential is touched.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import openrouter
from kicadgen.services import photo_crop_reading as crop
from kicadgen.services.analysis_profiles import ProfileStore, bare_backup_name
from kicadgen.services.photo_global_inventory import _rescale_boxes, review_global_inventory

from test_photo_crop_reading import Script, _obs, _read, _row
from test_photo_global_inventory import _clean_row, _two_row_response, candidate


# ---------------------------------------------------------------- D1

def _provider_payload(arguments_text: str, name: str) -> dict:
    """An OpenAI-style answer whose tool call carries @p arguments_text verbatim."""
    return {'choices': [{'finish_reason': 'stop', 'message': {'tool_calls': [
        {'type': 'function', 'function': {'name': name, 'arguments': arguments_text}}]}}]}


class _ProviderFake:
    """Paid-call fake that decodes the arguments exactly as the provider path does."""

    def __init__(self, answer: dict):
        self.answer = answer
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        # json.dumps writes NaN/Infinity literally, as a misbehaving model does.
        text = json.dumps(self.answer)
        return openrouter.tool_arguments(_provider_payload(text, kwargs['tool']['function']['name']),
                                         kwargs['tool']['function']['name'])


def _inventory(call, cache_dir, logs):
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    return review_global_inventory(np.zeros((60, 120, 3), np.uint8), items, 40, 20,
                                   'offline/model', call, logs.append, cache_dir)


def _good_answer() -> dict:
    return _two_row_response([_clean_row('R1', 1, [.2, .2, .4, .4]),
                              _clean_row('R2', 2, [.6, .6, .7, .7])])


def test_decoded_arguments_turn_non_finite_constants_into_null():
    text = '{"a": NaN, "b": [Infinity, -Infinity, 1.5], "c": {"d": 2}}'
    assert openrouter._decoded_arguments(text, '') == {'a': None, 'b': [None, None, 1.5],
                                                       'c': {'d': 2}}
    # Arguments already delivered as an object (envelope decoded with NaN allowed).
    assert openrouter._decoded_arguments({'a': [float('nan'), 3]}, '') == {'a': [None, 3]}
    # Negative: finite values and the original object structure are untouched.
    assert openrouter._decoded_arguments('{"a": 1e300, "b": -0.0}', '') == {'a': 1e300, 'b': -0.0}


def test_nan_in_an_ignored_field_keeps_the_rows_and_caches_the_answer(tmp_path):
    answer = _good_answer()
    answer['debug_score'] = float('nan')
    answer['components'][0]['aux'] = float('inf')
    call, logs = _ProviderFake(answer), []
    rows = _inventory(call, tmp_path, logs)
    assert call.calls == 1 and {r.get('component_id') for r in rows} >= {'R1', 'R2'}
    assert list(tmp_path.glob('global-inventory-*.json'))  # the cache, not the audit
    # The second run replays the cache: no new paid call.
    again = _inventory(call, tmp_path, [])
    assert call.calls == 1 and len(again) == len(rows)


def test_nan_inside_a_bbox_invalidates_that_row_and_is_not_cached_as_good(tmp_path):
    answer = _good_answer()
    answer['components'][0]['bbox'][2] = float('nan')
    call, logs = _ProviderFake(answer), []
    rows = _inventory(call, tmp_path, logs)
    assert any('FAILED/PARTIAL' in line for line in logs)
    assert not list(tmp_path.glob('global-inventory-*.json'))
    # The valid part of the partial answer is kept.
    assert any(r.get('component_id') == 'R2' for r in rows)


def test_a_direct_caller_returning_nan_never_crashes_the_cache_write(tmp_path):
    """Without the provider decoder the NaN reaches the cache write: logged, rows kept."""
    answer = _good_answer()
    answer['debug_score'] = float('nan')
    logs = []
    rows = _inventory(lambda **_kw: json.loads(json.dumps(answer)), tmp_path, logs)
    assert any(r.get('component_id') == 'R1' for r in rows)
    assert not list(tmp_path.glob('global-inventory-*.json'))
    assert any('cache non disponibile' in line for line in logs)


# ---------------------------------------------------------------- D2

def _castellated(**extra) -> dict:
    data = dict(layout='castellated', name='Module', body_length_mm=40, body_width_mm=20,
                pcb_thickness_mm=1.5, castellated_sides=[dict(edge='left', count=4, pitch_mm=2.54)])
    data.update(extra)
    return data


def test_malformed_edge_fillets_are_dropped_with_a_note_and_valid_ones_kept():
    raw = ['top', {'edge': 'top'}, {'edge': 'top-front', 'radius_mm': 'abc'},
           {'edge': 'nowhere', 'radius_mm': 1.0}, {'edge': 'top-left', 'radius_mm': -1},
           {'edge': 'top-right', 'radius_mm': True}, {'edge': 'top-back', 'radius_mm': '1.0'},
           {'edge': 'top-front', 'radius_mm': 1.2, 'extra': 'x'}]
    params = ext._params_from_tool_arguments(_castellated(edge_fillets=raw))
    assert params.edge_fillets == [{'edge': 'top-front', 'radius_mm': 1.2}]
    assert params.notes.count('edge_fillets[') == len(raw) - 1
    # Negative: no valid fillet at all is None, never an empty or malformed list.
    assert ext._params_from_tool_arguments(_castellated(edge_fillets=['top'])).edge_fillets is None
    assert ext._params_from_tool_arguments(_castellated()).edge_fillets is None


def test_malformed_pinout_entries_are_dropped_and_the_valid_shapes_kept():
    """Entries without a usable pin id are dropped; a pin without a valid name is kept.

    Contract change (review G5): pins with a missing or null name, and pin 0,
    used to be dropped, so pins of the source pinout vanished from the
    symbol. The symbol must keep every pin: such a pin now stays with an
    empty name and a note on its `name`; 0 is a valid pad number like any
    other non-negative integer.
    """
    raw = ['VCC', {'pin': 4}, {'pin': True, 'name': 'X'}, {'pin': 1.5, 'name': 'Y'},
           {'pin': 0, 'name': 'Z'}, {'pin': ' ', 'name': 'W'}, {'pin': 3, 'name': None},
           {'pin': -1, 'name': 'N'}, {'name': 'NOPIN'}, {'pin': 5, 'name': 7},
           {'pin': 1, 'name': 'VCC'}, {'pin': 'A2', 'name': 'GND'}, {'pin': 2.0, 'name': 'OUT'}]
    params = ext._params_from_tool_arguments(_castellated(pinout=raw))
    assert params.pinout == [{'pin': 4, 'name': ''}, {'pin': 0, 'name': 'Z'},
                             {'pin': 3, 'name': ''}, {'pin': 5, 'name': ''},
                             {'pin': 1, 'name': 'VCC'}, {'pin': 'A2', 'name': 'GND'},
                             {'pin': 2, 'name': 'OUT'}]
    # Six entries without a usable pin id (text, bool, 1.5, blank, -1, absent)
    # and three names discarded on kept pins.
    assert params.notes.count('pinout[') == 9
    assert params.notes.count('.name') == 3
    # What the GUI does with it can no longer raise.
    assert [dict(entry) for entry in params.pinout]
    assert ext._params_from_tool_arguments(_castellated(pinout='VCC')).pinout is None


# ---------------------------------------------------------------- D3

def test_a_fresh_crop_answer_missing_any_required_field_is_no_observation():
    required = crop.observation_tool()['function']['parameters']['required']
    assert crop.parse_observation(_obs()) is not None
    for field in required:
        answer = _obs()
        del answer[field]
        assert crop.parse_observation(answer) is None, field


def test_a_crop_answer_without_contacts_complete_is_never_cached(tmp_path):
    incomplete = _obs()
    del incomplete['contacts_complete']
    component = _row()
    summary, _ = _read([component], Script(*[dict(incomplete) for _ in range(8)]), tmp_path)
    assert summary.failed == 1 and not list((tmp_path / 'crop-reading').glob('*.json'))
    # Positive twin: the complete answer is cached for both readings.
    other = _row()
    summary, _ = _read([other], Script(_obs(), _obs()), tmp_path / 'ok')
    assert summary.failed == 0 and len(list((tmp_path / 'ok' / 'crop-reading').glob('*.json'))) == 2


# ---------------------------------------------------------------- D4

def test_numeric_strings_are_not_numbers_for_the_extractor():
    assert ext._optional_number('1.5') is None and ext._optional_number('3', integer=True) is None
    assert ext._optional_number(10 ** 400) is None and ext._optional_number(True) is None
    # Positive twins: real numbers, numpy scalars included, are numbers.
    assert ext._optional_number(1.5) == 1.5 and ext._optional_number(-2) == -2.0
    assert ext._optional_number(np.float32(1.5)) == 1.5
    assert ext._optional_number(np.int64(4), positive=True, integer=True) == 4


def test_a_3d_component_with_a_numeric_string_coordinate_is_dropped():
    good = dict(name='U1', x_mm=1.0, y_mm=2, length_mm=3, width_mm=2, height_mm=1)
    bad = dict(good, name='U2', x_mm='1.0')
    kept = ext._components_3d([good, bad])
    assert [c['name'] for c in kept] == ['U1']
    assert ext._components_3d([bad]) is None


def test_numeric_strings_in_a_bbox_are_no_unit_to_rescale():
    # A box only the pixel convention makes valid on a 2000 x 1500 photo.
    rows = [{'bbox': ['1600', '100', '1900', '400']}]
    assert _rescale_boxes(rows, 2000, 1500, lambda _m: None) is None
    assert rows[0]['bbox'] == ['1600', '100', '1900', '400']
    # Positive twin: the same values as numbers are converted to fractions.
    numeric = [{'bbox': [1600, 100, 1900, 400]}]
    assert _rescale_boxes(numeric, 2000, 1500, lambda _m: None) is not None
    assert numeric[0]['bbox'] == pytest.approx([0.8, 100 / 1500, 0.95, 400 / 1500])


# ---------------------------------------------------------------- D5

@pytest.fixture
def fresh_usage(monkeypatch):
    """Isolated run totals and listener for one test."""
    monkeypatch.setattr(openrouter, '_usage_totals', openrouter.UsageTotals())
    records = []
    monkeypatch.setattr(openrouter, '_usage_listener', records.append)
    return records


def test_overflowing_usage_numbers_never_leave_totals_half_updated(fresh_usage):
    openrouter._record_usage({'usage': {'cost': 10 ** 400, 'prompt_tokens': 5,
                                        'completion_tokens': 10 ** 400}})
    totals = openrouter.usage_totals()
    assert (totals.calls, totals.calls_without_cost, totals.prompt_tokens,
            totals.completion_tokens) == (1, 1, 5, 0)
    assert len(fresh_usage) == 1 and fresh_usage[0]['cost_usd'] is None
    assert fresh_usage[0]['prompt_tokens'] == 5 and fresh_usage[0]['completion_tokens'] is None
    assert openrouter._credit_number(10 ** 400) is None


def test_ordinary_usage_numbers_are_all_counted(fresh_usage):
    openrouter._record_usage({'usage': {'cost': 0.01, 'prompt_tokens': 7, 'completion_tokens': 10,
                                        'completion_tokens_details': {'reasoning_tokens': 3}}})
    totals = openrouter.usage_totals()
    assert (totals.calls, totals.calls_without_cost, totals.prompt_tokens,
            totals.completion_tokens, totals.reasoning_tokens) == (1, 0, 7, 10, 3)
    assert totals.cost_usd == pytest.approx(0.01)
    assert fresh_usage[0]['completion_tokens'] == 10


# ---------------------------------------------------------------- D6

@pytest.mark.parametrize('value,expected', [
    (r'C:\Users\someone\data\config-before-sqlite-ab.json', 'config-before-sqlite-ab.json'),
    (r'\\server\share\config-before-sqlite-ab.json', 'config-before-sqlite-ab.json'),
    ('/home/someone/data/config-before-sqlite-ab.json', 'config-before-sqlite-ab.json'),
    ('config-before-sqlite-ab.json', 'config-before-sqlite-ab.json'),
    ('', ''),
    ('data/config-before-sqlite-ab.json', 'data/config-before-sqlite-ab.json'),
])
def test_bare_backup_name(value, expected):
    assert bare_backup_name(value) == expected


def _backup_value(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT value FROM metadata WHERE name='legacy_backup'").fetchone()[0]


def _set_backup_value(path, value):
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("UPDATE metadata SET value=? WHERE name='legacy_backup'", (value,))
    connection.close()


def test_opening_the_store_rewrites_an_absolute_legacy_backup_to_its_name(tmp_path):
    path = tmp_path / 'profiles.sqlite3'
    ProfileStore(path)
    _set_backup_value(path, r'C:\Users\someone\data\config-before-sqlite-ab.json')
    ProfileStore(path)
    assert _backup_value(path) == 'config-before-sqlite-ab.json'
    ProfileStore(path)  # idempotent
    assert _backup_value(path) == 'config-before-sqlite-ab.json'


@pytest.mark.parametrize('value', ['', 'config-before-sqlite-ab.json'])
def test_opening_the_store_leaves_a_bare_or_empty_legacy_backup_alone(tmp_path, value):
    path = tmp_path / 'profiles.sqlite3'
    ProfileStore(path)
    _set_backup_value(path, value)
    ProfileStore(path)
    assert _backup_value(path) == value


def test_finite_arguments_object_is_returned_as_the_same_object():
    arguments = {'a': [1, 2.5], 'b': {'c': None}}
    assert openrouter._decoded_arguments(arguments, '') is arguments
    damaged = {'a': [1, float('nan')]}
    assert openrouter._decoded_arguments(damaged, '') == {'a': [1, None]}
    assert damaged['a'][1] != damaged['a'][1]  # the caller's object is never mutated


def test_overflowing_literals_and_numpy_non_finite_scalars_become_null():
    """Review G2: every non-finite number, however it arrives, decodes as None.

    ``1e999`` is a valid JSON number that overflows to infinity (parse_constant
    never sees it), and a NumPy float32 NaN is not a Python float.
    """
    import numpy as np
    assert openrouter._decoded_arguments('{"a": 1e999, "b": [-1e999, 2.0]}', '') == {
        'a': None, 'b': [None, 2.0]}
    damaged = {'a': [np.float32('nan'), np.float64('inf'), 1]}
    assert openrouter._decoded_arguments(damaged, '') == {'a': [None, None, 1]}
    # Negatives: finite NumPy scalars, integers and booleans keep the identity
    # of the supplied object; a large finite literal stays a number.
    kept = {'a': [np.float32(1.5), np.int64(3), True, False], 'b': 7}
    assert openrouter._decoded_arguments(kept, '') is kept
    decoded = openrouter._decoded_arguments('{"a": 1.7e308, "flag": true, "n": 4}', '')
    assert decoded == {'a': 1.7e308, 'flag': True, 'n': 4} and decoded['flag'] is True
