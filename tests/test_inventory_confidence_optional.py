"""@brief An omitted lead_count_confidence is «not declared», never a second paid call.

User decision of 2026-09-26: a real run paid a full second global-inventory
request (about 170 s and 0.63 USD) only because the model omitted
lead_count_confidence on every component. That field only QUALIFIES the lead
count; the data fields (lead_count, lead_count_complete, geometry, IDs) keep
their blocking validation and their single paid retry.
"""
from copy import deepcopy

import pytest

from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services.datasheet_extractor import _components_3d
from tests.test_photo_global_inventory import (
    _clean_row, candidate, omission_errors, run, run_replies, seed_cache)

UNDECLARED = inventory.LEAD_COUNT_CONFIDENCE_UNDECLARED


def _items():
    return [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]


def _rows(**changes):
    return [dict(_clean_row('R1', 1, [.2, .2, .4, .4]), **changes),
            dict(_clean_row('R2', 2, [.6, .6, .7, .7]), **changes)]


def _response(rows):
    return dict(components=rows, uncertainties=[],
                candidate_decisions=[dict(candidate_id=i, status='keep', component_id=f'R{i}',
                                          reason='chip body', rejection_category=None)
                                     for i in (1, 2)])


def _strip(rows, *names):
    rows = deepcopy(rows)
    for row in rows:
        for name in names:
            row.pop(name, None)
    return rows


def _undeclared_lines(logs):
    return [line for line in logs if 'senza confidenza dichiarata' in line]


def test_qualifier_is_structurally_separated_from_data_fields():
    """@brief The non-blocking set holds a qualifier only, and it names what it qualifies."""
    required = set(inventory.inventory_tool()['function']['parameters']['properties']
                   ['components']['items']['required'])
    assert set(inventory.NON_BLOCKING_QUALIFIERS) == {'lead_count_confidence'}
    # The schema still asks for it: the request (and so the cache key) is unchanged.
    assert 'lead_count_confidence' in required
    for data_fields in inventory.NON_BLOCKING_QUALIFIERS.values():
        assert set(data_fields) <= required
        assert not set(data_fields) & set(inventory.NON_BLOCKING_QUALIFIERS)
    assert UNDECLARED not in ('high', 'medium', 'low')


def test_omitted_required_fields_ignores_the_qualifier_but_not_data():
    rows = _rows()
    assert inventory.omitted_required_fields(
        _response(_strip(rows, 'lead_count_confidence'))) == []
    assert inventory.omitted_required_fields(
        _response(_strip(rows, 'lead_count_confidence', 'lead_count_complete'))) == ['lead_count_complete']
    assert inventory.omitted_required_fields(
        _response(_strip(rows, 'lead_count_confidence', 'presence_confidence'))) == ['presence_confidence']


@pytest.mark.parametrize('how', ['absent', 'null'])
def test_qualifier_omitted_everywhere_costs_one_call_and_is_cached(how, tmp_path):
    """@brief Positive: one paid call, rows kept, value undeclared, cache written and replayed."""
    rows = _rows(lead_count=2, lead_count_complete=True, lead_count_confidence='high')
    rows = (_strip(rows, 'lead_count_confidence') if how == 'absent'
            else [dict(row, lead_count_confidence=None) for row in rows])
    data = _response(rows)
    result, calls, logs = run(data, _items(), cache=tmp_path)
    assert len(calls) == 1
    assert 'omitted these REQUIRED fields' not in calls[0]['system_prompt']
    assert not any('nuovo tentativo' in line for line in logs)
    assert [r['component_id'] for r in result] == ['R1', 'R2']
    assert all(r['_global_inventory_status'] == 'validated_hypotheses' for r in result)
    assert omission_errors(result) == [False, False]
    assert all(r['lead_count'] == 2 and r['lead_count_complete'] is True for r in result)
    assert all(r['lead_count_confidence'] == UNDECLARED for r in result)
    # An undeclared confidence is the weakest level: no exact pin count.
    assert all('pin_count' not in r for r in result)
    lines = _undeclared_lines(logs)
    assert len(lines) == 1 and '2 componenti su 2' in lines[0]
    assert 'Nessuna nuova richiesta a pagamento' in lines[0]
    assert len(list(tmp_path.glob('global-inventory-*.json'))) == 1
    replayed, calls, logs = run(data, _items(), cache=tmp_path)
    assert not calls and [r['lead_count_confidence'] for r in replayed] == [UNDECLARED] * 2
    assert len(_undeclared_lines(logs)) == 1


def test_complete_declared_response_logs_nothing_about_the_qualifier():
    """@brief Negative for the log line: nothing omitted, nothing reported."""
    result, calls, logs = run(_response(_rows(lead_count=2, lead_count_complete=True,
                                              lead_count_confidence='medium')), _items())
    assert len(calls) == 1 and not _undeclared_lines(logs)
    assert all(r['lead_count_confidence'] == 'medium' and r['pin_count'] == 2 for r in result)


def test_old_cache_missing_only_the_qualifier_is_replayed_without_a_call(tmp_path):
    """@brief A cache written by a run that lacked the qualifier is accepted, no re-payment."""
    complete = _response(_rows())
    _, calls, _ = run(complete, _items())
    seed_cache(tmp_path, calls[0], _response(_strip(_rows(), 'lead_count_confidence')))
    result, calls, _ = run(complete, _items(), cache=tmp_path)
    assert not calls
    assert all(r['lead_count_confidence'] == UNDECLARED for r in result)
    assert all(r['_global_inventory_status'] == 'validated_hypotheses' for r in result)


def test_omitted_data_field_is_still_retried_and_never_cached(tmp_path):
    """@brief Negative: a DATA field omitted everywhere keeps the paid retry and blocks the cache."""
    data = _response(_strip(_rows(lead_count=2), 'lead_count_complete'))
    result, calls, logs = run(data, _items(), cache=tmp_path)
    assert len(calls) == 2
    assert 'lead_count_complete' in calls[1]['system_prompt'].split('REQUIRED fields')[-1]
    assert all(omission_errors(result))
    assert not list(tmp_path.glob('global-inventory-*.json'))
    assert any('un solo nuovo tentativo a pagamento' in line for line in logs)


def test_retry_for_another_field_does_not_name_or_require_the_qualifier(tmp_path):
    """@brief The retry path stays identical for blocking fields and ignores the qualifier."""
    rows = _rows()
    first = _response(_strip(rows, 'presence_confidence', 'lead_count_confidence'))
    second = _response(_strip(rows, 'lead_count_confidence'))
    result, calls, logs = run_replies([first, second], _items(), cache=tmp_path)
    assert len(calls) == 2
    reminder = calls[1]['system_prompt'][len(calls[0]['system_prompt']):]
    assert 'presence_confidence' in reminder and 'lead_count_confidence' not in reminder
    assert any('il nuovo tentativo porta i campi obbligatori' in line for line in logs)
    assert omission_errors(result) == [False, False]
    assert all(r['lead_count_confidence'] == UNDECLARED for r in result)
    assert len(list(tmp_path.glob('global-inventory-*.json'))) == 1


@pytest.mark.parametrize('confidence,promoted', [
    (None, False), ('low', False), ('medium', True), ('high', True)])
def test_undeclared_is_never_promoted_to_an_exact_pin_count(confidence, promoted):
    row = dict(_clean_row('U1', 1, [.2, .2, .4, .4]), terminal_count=4, lead_count=3,
               lead_count_complete=True, lead_count_confidence=confidence)
    assert inventory._component_valid(row, {1})
    parsed = _components_3d([inventory._normalize(row, 5, 40, 20, [])], trusted=True)[0]
    assert parsed['lead_count_confidence'] == (confidence or UNDECLARED)
    assert ('pin_count' in parsed) is promoted


@pytest.mark.parametrize('changes', [
    {'lead_count_confidence': 'certain'},
    # A declared qualifier with nothing to qualify is incoherent.
    {'lead_count': None, 'lead_count_complete': None, 'lead_count_confidence': 'high'},
])
def test_declared_but_invalid_qualifier_is_still_rejected(changes):
    row = dict(_clean_row('U1', 1, [.2, .2, .4, .4]), lead_count=3, lead_count_complete=False)
    row.update(changes)
    if changes.get('lead_count_complete', False) is None:
        row.pop('lead_count'); row.pop('lead_count_complete')
    assert not inventory._component_valid(row, {1})
