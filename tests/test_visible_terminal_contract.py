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
               # Il riferimento asimmetrico e' obbligatorio nello schema: una
               # risposta che lo omette su ogni riga fa scattare un secondo
               # tentativo a pagamento, e qui si conta una chiamata sola. I
               # campi dei terminali restano a carico di ogni test.
               asymmetry_landmark=None,
               evidence='Mounted body with ordinary leads and a thermal tab')
    row.update(changes)
    return row


def test_versioned_schema_and_prompt_distinguish_complete_leads_and_visible_tabs():
    # Il prompt ora chiede anche il riferimento asimmetrico, quindi la versione
    # sale: un prompt diverso sotto la stessa versione renderebbe impossibile
    # dire, rileggendo una cache, che cosa era stato davvero chiesto.
    # 27/09: versione 8/5 per leads_by_side e package_confidence (forma separata
    # dalla funzione), vedi i test in fondo al file.
    assert inventory.PROMPT_VERSION == 'global-yolo-inventory-8'
    assert inventory.SCHEMA_VERSION == 'global-inventory-5'
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
    {'lead_count': 5}])
def test_malformed_or_incoherent_observation_is_not_valid(changes):
    # Contract changed on purpose (2026-09-26): a null lead_count_confidence is
    # «not declared», no longer malformed; see tests/test_inventory_confidence_optional.py.
    # A declared but unknown level ('certain') is still malformed.
    raw = _body(lead_count=3, lead_count_complete=True, lead_count_confidence='high')
    raw.update(changes)
    before = deepcopy(raw)
    assert not inventory._component_valid(raw, set())
    assert raw == before


@pytest.mark.parametrize('missing', ['lead_count', 'lead_count_complete'])
def test_partial_new_observation_is_not_mistaken_for_legacy_absence(missing):
    # Only DATA fields are listed: the qualifier lead_count_confidence may be
    # omitted since 2026-09-26 (NON_BLOCKING_QUALIFIERS).
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


# ── Forma separata dalla funzione (utente, 27/09: i SOT-23 uscivano blocchi) ──

def test_the_prompt_separates_package_shape_from_function():
    for prompt in (inventory.PROMPT, inventory.PROMPT_ENUMERATE):
        assert 'NEVER empties package' in prompt
        assert 'leads_by_side counts the observed ordinary leads' in prompt
        assert 'package_confidence rates the package alone' in prompt
        assert 'four orientations' in prompt
    item = inventory.inventory_tool()['function']['parameters']['properties']['components']['items']
    assert {'package_confidence', 'leads_by_side'} <= set(item['required'])
    sides = item['properties']['leads_by_side']
    assert set(sides['required']) == {'left', 'right', 'top', 'bottom'}
    assert sides['properties']['left']['type'] == ['integer', 'null']


def _sot(**changes):
    return _body(terminal_count=3, lead_count=3, lead_count_complete=True,
                 lead_count_confidence='high', package='SOT-23',
                 terminal_layout='two leads on one long side, one on the opposite',
                 **changes)


def test_an_unknown_function_keeps_a_package_the_shape_supports():
    """Positive: class unresolved, overall confidence low, package shape certain."""
    row = _sot(package_confidence='high', leads_by_side=dict(left=2, right=1, top=0, bottom=0))
    assert inventory._component_valid(row, set())
    result = inventory._normalize(row, 1, 40, 20, [])
    assert result['class'] == 'semiconductor_unknown' and result['confidence'] == 'low'
    assert result['package'] == 'SOT-23' and result['package_confidence'] == 'high'
    assert result['pins_by_side'] == {'left': 2, 'right': 1}


def test_an_ambiguous_shape_still_empties_the_package():
    """Negative: the package goes when the SHAPE is doubtful, whatever the function."""
    row = _sot(package_confidence='low', confidence='high')
    assert inventory._normalize(row, 1, 40, 20, [])['package'] == ''


def test_a_row_of_the_previous_schema_keeps_the_previous_rule():
    legacy = _sot()                                    # no package_confidence, confidence low
    assert inventory._normalize(legacy, 1, 40, 20, [])['package'] == ''
    assert inventory._normalize(_sot(confidence='medium'), 1, 40, 20, [])['package'] == 'SOT-23'


@pytest.mark.parametrize('sides,why', [
    (dict(left=2, right=None, top=0, bottom=0), 'a side not observed may carry leads'),
    (dict(left=2, right=2, top=0, bottom=0), 'does not add up to the complete lead_count'),
    (dict(left=0, right=0, top=0, bottom=0), 'no lead at all'),
    (dict(left=2, right=1, top=0), 'a side missing'),
    (dict(left=2, right=1, top=0, bottom=0, centre=1), 'unknown side'),
    (dict(left=2.0, right=1, top=0, bottom=0), 'not an integer'),
    ('2+1', 'not an object'),
])
def test_an_incomplete_or_incoherent_side_count_is_not_drawn(sides, why):
    row = _sot(package_confidence='high', leads_by_side=sides)
    assert inventory._component_valid(row, set()), why
    result = inventory._normalize(row, 1, 40, 20, [])
    assert 'pins_by_side' not in result, why
    assert result['package'] == 'SOT-23'               # the rest of the row survives


def test_without_the_new_fields_nothing_blocks_or_asks_again():
    """Omitting the refinements never makes a second paid request necessary."""
    rows = [_sot(), _body(lead_count=None, lead_count_complete=False)]
    assert inventory.omitted_required_fields({'components': rows}) == []
    assert all(inventory._component_valid(row, set()) for row in rows)
