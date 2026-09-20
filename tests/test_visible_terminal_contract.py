"""@brief Separate visible terminal surfaces from complete ordinary-lead counts."""
from copy import deepcopy
import json

import numpy as np
import pytest

from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services.datasheet_extractor import _components_3d


def _body(**changes):
    row = dict(component_id='power-body', source_candidate_ids=[],
               **{'class': 'semiconductor_unknown'}, grouping_basis='single_body',
               bbox=[.1, .1, .3, .3], bbox_basis='body_and_leads', rotation_deg=0,
               package='', marking='S?', terminal_count=4,
               terminal_layout='three ordinary leads and one thermal tab',
               height_mm=None, height_source='unknown', confidence='low',
               presence_confidence='high', class_confidence='low',
               evidence='Mounted body with ordinary leads and a thermal tab')
    row.update(changes)
    return row


def test_versioned_schema_and_prompt_distinguish_complete_leads_and_visible_tabs():
    # Il prompt ora chiede anche il riferimento asimmetrico, quindi la versione
    # sale: un prompt diverso sotto la stessa versione renderebbe impossibile
    # dire, rileggendo una cache, che cosa era stato davvero chiesto.
    assert inventory.PROMPT_VERSION == 'global-yolo-inventory-6'
    assert inventory.SCHEMA_VERSION == 'global-inventory-4'
    item = inventory.inventory_tool()['function']['parameters']['properties']['components']['items']
    fields = item['properties']
    assert fields['lead_count']['type'] == ['integer', 'null']
    assert fields['lead_count']['minimum'] == 0 and fields['lead_count']['maximum'] == 512
    assert fields['lead_count_complete']['type'] == 'boolean'
    assert fields['lead_count_confidence']['enum'] == ['high', 'medium', 'low']
    assert set(('lead_count', 'lead_count_complete', 'lead_count_confidence')) <= set(item['required'])
    assert 'INCLUDING visible tab' in inventory.PROMPT
    assert 'ALL ordinary leads are visible' in inventory.PROMPT
    assert 'hidden leads from a package name' in inventory.PROMPT
    assert 'terminal_count=4 and lead_count=3' in inventory.PROMPT


@pytest.mark.parametrize('complete,confidence,promoted', [
    (True, 'high', True), (True, 'medium', True), (True, 'low', False),
    (False, 'high', False), (False, 'medium', False), (False, 'low', False)])
def test_only_complete_confident_ordinary_leads_become_exact_pin_count(complete, confidence, promoted):
    raw = _body(lead_count=3, lead_count_complete=complete, lead_count_confidence=confidence)
    before = deepcopy(raw)
    assert inventory._component_valid(raw, set())
    normalized = inventory._normalize(raw, 1, 40, 20, [])
    parsed = _components_3d([normalized])[0]
    assert raw == before
    for result in (normalized, parsed):
        assert result['terminal_count'] == 4
        assert result['terminal_count_basis'] == 'visible_including_tabs'
        assert result['lead_count'] == 3
        assert result['lead_count_complete'] is complete
        assert result['lead_count_confidence'] == confidence
        if promoted:
            assert result['pin_count'] == 3
            assert result['pin_count_confidence'] == confidence
            assert result['pin_count_source'] == 'complete_lead_observation'
        else:
            assert 'pin_count' not in result and 'pin_count_source' not in result


def test_legacy_visible_count_is_preserved_but_never_promoted():
    raw = _body(confidence='high')
    assert inventory._component_valid(raw, set())
    result = _components_3d([inventory._normalize(raw, 1, 40, 20, [])])[0]
    assert result['terminal_count'] == 4 and result['terminal_count_basis'] == 'visible_including_tabs'
    assert result['lead_count'] is None and result['lead_count_complete'] is False
    assert 'pin_count' not in result


@pytest.mark.parametrize('count', [None, 0, 512])
def test_nullable_and_boundary_observations_remain_incomplete(count):
    raw = _body(terminal_count=None, lead_count=count, lead_count_complete=False,
                lead_count_confidence='low')
    assert inventory._component_valid(raw, set())
    result = _components_3d([inventory._normalize(raw, 1, 40, 20, [])])[0]
    assert result['lead_count'] == count and 'pin_count' not in result


@pytest.mark.parametrize('changes', [
    {'lead_count': True}, {'lead_count': '3'}, {'lead_count': 3.0},
    {'lead_count': -1}, {'lead_count': 513}, {'lead_count': None},
    {'lead_count_complete': 1}, {'lead_count_complete': 'true'},
    {'lead_count_complete': None}, {'lead_count_confidence': 'certain'},
    {'lead_count_confidence': None}, {'lead_count': 5}])
def test_malformed_or_incoherent_observation_is_not_valid(changes):
    raw = _body(lead_count=3, lead_count_complete=True, lead_count_confidence='high')
    raw.update(changes)
    before = deepcopy(raw)
    assert not inventory._component_valid(raw, set())
    assert raw == before


@pytest.mark.parametrize('missing', ['lead_count', 'lead_count_complete', 'lead_count_confidence'])
def test_partial_new_observation_is_not_mistaken_for_legacy_absence(missing):
    raw = _body(lead_count=3, lead_count_complete=True, lead_count_confidence='high')
    del raw[missing]
    assert not inventory._component_valid(raw, set())


def test_complete_leads_conversion_preserves_raw_response_and_audit(tmp_path):
    response = dict(components=[_body(lead_count=3, lead_count_complete=True,
                                      lead_count_confidence='medium')],
                    candidate_decisions=[], uncertainties=[])
    before = deepcopy(response)
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return response

    result = inventory.review_global_inventory(
        np.zeros((40, 80, 3), dtype=np.uint8), [], 40, 20,
        'offline/test', fake_call, lambda message: None, tmp_path)
    assert len(calls) == 1 and response == before
    assert result[0]['pin_count'] == 3 and result[0]['terminal_count'] == 4
    paths = list((tmp_path / 'yolo-audit').glob('global-inventory-*.json'))
    assert len(paths) == 1
    audit = json.loads(paths[0].read_text(encoding='utf-8'))
    assert audit['response'] == before
    assert audit['components'][0]['pin_count_source'] == 'complete_lead_observation'
