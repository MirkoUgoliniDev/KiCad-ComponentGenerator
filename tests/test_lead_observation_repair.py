"""@brief HANDOFF 155: an incoherent lead group no longer discards the whole row.

Real run RS485 2026-09-29 18:17: the model answered ``lead_count: null`` with
``lead_count_complete: true`` on 36 chip passives, following the prompt that
says to use null where there are no ordinary leads, and every one of those
rows -- class, package, marking included -- was replaced by a generic
hypothesis «malformed metadata». The lead group is a refinement of the row,
not its licence: it is discarded alone, with the reason on the row, and the
rest of the row keeps what the model read. Rows with a bad box, class or ID
are still replaced by the generic hypothesis, as before.
"""

from copy import deepcopy

from kicadgen.services import photo_global_inventory as inventory
from test_photo_global_inventory import body, candidate, response, run


def _passive(**changes):
    """A resistor row on ONE proposal, as the real run wrote it (shortened)."""
    row = body('R1', sources=[1], **{'class': 'resistor'}, grouping_basis='single_body',
               bbox=[.2, .2, .4, .4], bbox_basis='body_and_leads', package='0603',
               marking='103', terminal_count=2, terminal_layout='metal endcaps',
               lead_count=None, lead_count_complete=True, lead_count_confidence='high',
               confidence='medium', class_confidence='high', package_confidence='high',
               evidence='Chip resistor with printed code')
    row.update(changes)
    return row


def _single(row):
    return dict(components=[row], candidate_decisions=[
        dict(candidate_id=1, status='keep', component_id=row.get('component_id', 'R1'),
             reason='Visible body')], uncertainties=[])


def test_null_count_declared_complete_keeps_the_row_without_its_lead_data(tmp_path):
    """@brief Positive: the class survives, the lead group is «not observed», cached."""
    data = _single(_passive())
    before = deepcopy(data)
    result, calls, logs = run(data, [candidate(1)], cache=tmp_path)
    assert len(calls) == 1 and data == before  # the raw response is never edited
    assert len(result) == 1
    row = result[0]
    assert row['component_id'] == 'R1' and row['kind'] == 'resistor'
    assert row['package'] == '0603' and row['marking'] == '103'
    assert row['lead_count'] is None and row['lead_count_complete'] is False
    assert row['lead_count_confidence'] == inventory.LEAD_COUNT_CONFIDENCE_UNDECLARED
    assert row[inventory.LEAD_OBSERVATION_DISCARDED] == 'lead_count null but declared complete'
    assert row['_review_reason'].startswith(
        'Lead observation of the response discarded (lead_count null but declared complete)')
    assert row['_review_reason'].endswith('Chip resistor with printed code')
    # Not an inventory error: status validated, no FAILED/PARTIAL, response cached.
    assert row['_global_inventory_errors'] == []
    assert row['_global_inventory_status'] == 'validated_hypotheses'
    assert not any('invalid or duplicate component' in line or 'FAILED/PARTIAL' in line
                   for line in logs)
    assert any('osservazione dei piedini incoerente' in line
               and 'lead_count null but declared complete x1' in line for line in logs)
    assert len(list(tmp_path.glob('global-inventory-*.json'))) == 1


def test_other_incoherent_lead_groups_are_discarded_the_same_way():
    """@brief Positive: every lead-group fault is repaired, each with its own reason."""
    cases = [
        (dict(lead_count=5, lead_count_complete=True, terminal_count=3),
         'lead_count exceeds terminal_count'),
        (dict(lead_count='3', lead_count_complete=True), 'lead_count is not an integer in 0..512'),
        (dict(lead_count=3, lead_count_complete='true'), 'lead_count_complete is not a boolean'),
        (dict(lead_count=3, lead_count_complete=True, lead_count_confidence='certain'),
         'lead_count_confidence is not high/medium/low'),
    ]
    for changes, reason in cases:
        result, _, _ = run(_single(_passive(**changes)), [candidate(1)])
        assert len(result) == 1 and result[0]['kind'] == 'resistor', changes
        assert result[0][inventory.LEAD_OBSERVATION_DISCARDED] == reason, changes
        assert result[0]['lead_count'] is None and result[0]['lead_count_complete'] is False


def test_a_row_with_another_fault_is_still_a_generic_hypothesis(tmp_path):
    """@brief Negative: the repair covers the lead group only, never a bad class."""
    data = _single(_passive(**{'class': 'thingamajig'}))
    result, _, logs = run(data, [candidate(1)], cache=tmp_path)
    assert len(result) == 1
    assert result[0]['kind'] == 'generic' and result[0]['package'] == ''
    assert inventory.LEAD_OBSERVATION_DISCARDED not in result[0]
    assert result[0]['_global_inventory_status'] == 'partial'
    assert any('invalid or duplicate component' in line for line in logs)
    assert not list(tmp_path.glob('global-inventory-*.json'))


def test_a_coherent_row_is_untouched():
    """@brief Negative: nothing is repaired, nothing is annotated, when the group is coherent."""
    coherent = _passive(lead_count=None, lead_count_complete=False)
    assert inventory._without_lead_observation(coherent, {1}) is None
    assert inventory._without_lead_observation('not a row', {1}) is None
    result, _, logs = run(_single(coherent), [candidate(1)])
    assert inventory.LEAD_OBSERVATION_DISCARDED not in result[0]
    assert result[0]['_review_reason'] == 'Chip resistor with printed code'
    assert not any('osservazione dei piedini' in line for line in logs)


def test_the_validity_contract_of_the_group_is_unchanged():
    """@brief The old predicate still says «invalid»: the repair is downstream of it."""
    raw = _passive()
    assert not inventory._component_valid(raw, {1})
    assert not inventory._lead_observation_valid(raw)
    assert inventory._lead_observation_issue(raw) == 'lead_count null but declared complete'
    assert inventory._lead_observation_issue(_passive(lead_count=2, lead_count_complete=True)) is None
    assert inventory._lead_observation_issue(dict(component_id='x')) is None  # legacy absence
    assert inventory._lead_observation_issue(dict(lead_count_confidence='high')) \
        == 'lead_count_confidence declared without lead data'


def test_the_repair_does_not_change_the_whole_response_verdict(tmp_path):
    """@brief A repaired row beside a plain valid one: both kept, one call, cached."""
    data = response()
    # The resistor sits on its own proposal, outside the assembly's envelope:
    # a body inside a retained assembly would be counted as part of it.
    data['components'].append(_passive(source_candidate_ids=[3], bbox=[.85, .85, .95, .95]))
    data['candidate_decisions'].append(dict(candidate_id=3, status='keep', component_id='R1',
                                            reason='Visible body'))
    third = candidate(3); third['_box'] = [.85, .85, .95, .95]
    result, calls, _ = run(data, [candidate(1), candidate(2), third], cache=tmp_path)
    assert len(calls) == 1
    assert {r['component_id'] for r in result} == {'assembly', 'R1'}
    assert all(r['_global_inventory_status'] == 'validated_hypotheses' for r in result)
    assert len(list(tmp_path.glob('global-inventory-*.json'))) == 1
