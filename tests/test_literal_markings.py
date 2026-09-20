"""@brief Keep AI marking sentinels out of CAD while preserving raw evidence."""
from copy import deepcopy
import json

import numpy as np
import pytest

from kicadgen.services.datasheet_extractor import _components_3d
from kicadgen.services.photo_global_inventory import _normalize, review_global_inventory


def _observation(marking):
    return dict(component_id='body', source_candidate_ids=[],
                **{'class': 'resistor'}, grouping_basis='single_body',
                bbox=[.1, .1, .2, .2], bbox_basis='body', rotation_deg=0,
                package='R_0603', marking=marking, terminal_count=2,
                terminal_layout='two visible endcaps', height_mm=None,
                height_source='unknown', confidence='medium',
                presence_confidence='high', class_confidence='medium',
                # Campi obbligatori dello schema: una risposta che ne omette
                # uno su ogni riga fa scattare un secondo tentativo a
                # pagamento, e questi test contano le chiamate.
                lead_count=None, lead_count_complete=False,
                lead_count_confidence='low', asymmetry_landmark=None,
                evidence='Mounted body with endcaps')


@pytest.mark.parametrize('marking', ['none', 'NONE', ' none ', '?', ' ?\t'])
def test_exact_ai_sentinels_are_empty_in_both_conversions(marking):
    raw = _observation(marking)
    before = deepcopy(raw)
    normalized = _normalize(raw, 1, 40, 20, [])
    assert raw == before
    assert normalized['marking'] == ''

    # Exercise the second conversion independently, not only cleaned input.
    entry = dict(normalized, marking=marking)
    source = deepcopy(entry)
    parsed = _components_3d([entry])[0]
    assert entry == source
    assert parsed['marking'] == ''
    assert parsed['terminal_count'] == normalized['terminal_count'] == 2


@pytest.mark.parametrize('marking', ['0', 'S?', 'None123', 'J3Y', '?4?4 A8C',
                                     'none visible', 'unknown', '??'])
def test_literal_readings_and_non_sentinel_text_are_preserved(marking):
    raw = _observation(marking)
    before = deepcopy(raw)
    normalized = _normalize(raw, 1, 40, 20, [])
    source = deepcopy(normalized)
    parsed = _components_3d([normalized])[0]
    assert normalized['marking'] == parsed['marking'] == marking
    assert raw == before and normalized == source


@pytest.mark.parametrize('marking', ['none', '?', '0', 'S?', 'None123'])
def test_saved_audit_and_response_keep_original_marking(tmp_path, marking):
    response = dict(components=[_observation(marking)],
                    candidate_decisions=[], uncertainties=[])
    before = deepcopy(response)
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return response

    result = review_global_inventory(
        np.zeros((40, 80, 3), dtype=np.uint8), [], 40, 20,
        'offline/test', fake_call, lambda message: None, tmp_path)
    assert len(calls) == 1 and response == before
    expected = '' if marking in ('none', '?') else marking
    assert result[0]['marking'] == expected
    audit_paths = list((tmp_path / 'yolo-audit').glob('global-inventory-*.json'))
    assert len(audit_paths) == 1
    audit = json.loads(audit_paths[0].read_text(encoding='utf-8'))
    assert audit['response'] == before
    assert audit['components'][0]['marking'] == expected
    cache_paths = list(tmp_path.glob('global-inventory-*.json'))
    assert len(cache_paths) == 1
    assert json.loads(cache_paths[0].read_text(encoding='utf-8'))['response'] == before
