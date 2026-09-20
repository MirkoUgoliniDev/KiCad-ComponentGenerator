"""@brief Exercise the real extractor/global-inventory/parser chain without inference.

Only external API, calibrated pixels and local ONNX detection are fixtures.
The inventory routing, global request construction, validation and parser run live.
"""
from copy import deepcopy
import json
import socket
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services import photo_global_inventory as global_inventory
from kicadgen.services import photo_grounding, photo_inventory, photo_terminals, photo_yolo, photo_yolo_review


def _proposal(identity):
    return dict(_id=identity, _box=[.2, .2, .4, .4], name='detector hypothesis',
                kind='generic', x_mm=-8, y_mm=-4, length_mm=8, width_mm=4,
                height_mm=1, rotation_deg=0, color_hex='#303030')


def _body(identity='whole-assembly', sources=None, **changes):
    row = dict(component_id=identity, source_candidate_ids=[1, 2] if sources is None else sources,
               **{'class': 'rf_module'}, bbox=[.1, .1, .8, .8], bbox_basis='body',
               rotation_deg=25, package='', marking='?', terminal_count=4,
               terminal_layout='three leads left, one tab right', height_mm=1.8,
               height_source='visual_estimate', confidence='medium',
               evidence='One substrate with attached shield; estimated height only')
    row.update(changes)
    return row


def _inventory_response():
    return dict(components=[_body()], candidate_decisions=[
        dict(candidate_id=i, status='merge', component_id='whole-assembly',
             reason='Fragment of retained substrate assembly') for i in (1, 2)],
        uncertainties=['Pin1 and actual height unresolved'])


def _main_response():
    return dict(name='Synthetic board', layout='castellated', body_length_mm=40,
                body_width_mm=20, body_height_mm=3, pcb_thickness_mm=1.6,
                top_photo_index=0, castellated_sides=[
                    dict(edge='left', count=2, pitch_mm=2.54, first_pin=1)])


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """@brief Keep IO boundaries local while exercising the application entrypoint."""
    def forbidden(*args, **kwargs):
        pytest.fail('Legacy review, terminal inference or network must not run')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    for owner, name in ((photo_yolo_review, 'recover_coverage'),
                        (photo_yolo_review, 'review_yolo_components'),
                        (photo_yolo_review, 'crop_context'),
                        (photo_grounding, 'grounded_components'),
                        (photo_terminals, 'observe_terminals')):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr(extractor, 'api_key', lambda: 'offline-fixture')
    credit = Mock()
    monkeypatch.setattr(extractor, 'check_credit', credit)
    setup = Mock()
    monkeypatch.setattr(photo_yolo, 'validate_yolo_setup', setup)
    pixels = np.zeros((100, 200, 3), np.uint8)
    photo = tmp_path/'top.png'
    assert cv2.imwrite(str(photo), pixels)
    monkeypatch.setattr(photo_inventory, 'prepare_photo', lambda *args: pixels.copy())
    detection = Mock(return_value=[_proposal(1), _proposal(2)])
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', detection)
    global_review = Mock(wraps=global_inventory.review_global_inventory)
    monkeypatch.setattr(global_inventory, 'review_global_inventory', global_review)
    state = dict(main=_main_response(), inventory=_inventory_response(), requests=[],
                 logs=[], detection=detection, setup=setup, credit=credit, global_review=global_review)

    def fake_api(**kwargs):
        state['requests'].append(deepcopy(kwargs))
        name = kwargs['tool']['function']['name']
        if name == extractor._openai_tool()['function']['name']:
            return deepcopy(state['main'])
        assert name == 'report_global_inventory', f'Unexpected extra semantic call: {name}'
        return deepcopy(state['inventory'])

    monkeypatch.setattr(extractor, 'call_tool', fake_api)

    def execute(**options):
        arguments = dict(model='offline/board', vision_model='offline/localizer',
                         verification_model='offline/global', photo_detector='yolo',
                         yolo_model_path=tmp_path/'fixture.onnx', photo_cache_dir=tmp_path/'cache',
                         progress=state['logs'].append,
                         verification_prompt=photo_grounding.VERIFICATION_PROMPT,
                         extra_instructions='Measured board; preserve uncertain bodies',
                         terminal_prompt='This legacy terminal prompt must never trigger a call')
        arguments.update(options)
        return extractor.extract_pin_header_params([photo], **arguments)

    state['execute'] = execute
    return state


@pytest.mark.parametrize('family', ['castellated', 'aggregate_tht'])
def test_extractor_yolo_calls_global_once_without_legacy_or_terminals(pipeline, family):
    """@brief Check both supported populated-board entry routes through real services."""
    options = {}
    if family == 'aggregate_tht':
        pipeline['main'].update(layout='matrix', rows=2, cols=2, pitch_mm=2.54,
                                row_spacing_mm=10, pad_diameter_mm=1.7, drill_mm=1)
        options['component_family'] = family
    params = pipeline['execute'](**options)
    assert len(pipeline['requests']) == 2  # Board extraction plus ONE global inventory.
    request = pipeline['requests'][1]
    assert request['tool']['function']['name'] == 'report_global_inventory'
    assert request['model'] == 'offline/global'
    assert sum(b['type'] == 'image_url' for b in request['content_blocks']) == 5
    inputs = json.loads(request['content_blocks'][4]['text'])
    assert [row['id'] for row in inputs['candidates']] == [1, 2]
    assert inputs['board_size_mm'] == [40, 20]
    pipeline['global_review'].assert_called_once()
    pipeline['setup'].assert_called_once()
    pipeline['detection'].assert_called_once()
    pipeline['credit'].assert_called_once()
    assert 'Measured board; preserve uncertain bodies' in request['system_prompt']
    assert photo_grounding.VERIFICATION_PROMPT not in request['system_prompt']
    assert len(params.components_3d) == 1
    row = params.components_3d[0]
    assert row['component_id'] == 'whole-assembly' and row['source_candidate_ids'] == [1, 2]
    assert row['class'] == 'rf_module' and row['kind'] == 'module'
    assert row['_box'] == [.1, .1, .8, .8]
    assert row['length_mm'] == pytest.approx(28)
    assert row['width_mm'] == pytest.approx(14)
    assert row['terminal_count'] == 4 and 'pin_count' not in row
    assert row['terminal_layout'] == 'three leads left, one tab right'
    assert row['_reported_rotation_deg'] == 25 and row['rotation_deg'] == 0
    assert row['_height_estimated'] and not row['_orientation_verified']
    assert row['_global_inventory_status'] == 'validated_hypotheses'
    assert row['_global_inventory_errors'] == []
    assert row['_global_inventory_uncertainties'] == ['Pin1 and actual height unresolved']
    assert row['_global_inventory_prompt_version'] == global_inventory.PROMPT_VERSION
    assert len(row['_global_inventory_prompt_hash']) == 64
    assert params.photo_png and params.photo_overlay_png and params.photo_source == 'top.png'


def test_extractor_empty_yolo_still_discovers_missing_body(pipeline):
    pipeline['detection'].return_value = []
    pipeline['inventory']['components'][0]['source_candidate_ids'] = []
    pipeline['inventory']['candidate_decisions'] = []
    params = pipeline['execute']()
    assert len(pipeline['requests']) == 2
    assert len(params.components_3d) == 1
    assert params.components_3d[0]['source_candidate_ids'] == []


def test_independent_class_uncertainty_preserves_generic_body_through_parser(pipeline):
    pipeline['inventory']['components'][0].update(**{'class': 'unknown'},
        presence_confidence='high', class_confidence='low', confidence='low', package='')
    params = pipeline['execute']()
    assert len(params.components_3d) == 1
    row = params.components_3d[0]
    assert row['kind'] == 'generic' and row['class'] == 'unknown'
    assert row['confidence'] == 'low' and row['_uncertain']
    assert row['_box'] == [.1, .1, .8, .8]
    assert row['source_candidate_ids'] == [1, 2]
    assert not row.get('package')


def test_extractor_malformed_inventory_preserves_proposals_without_more_calls(pipeline):
    pipeline['inventory'] = {'components': 'malformed'}
    params = pipeline['execute']()
    assert len(pipeline['requests']) == 2
    assert {r['_id'] for r in params.components_3d} == {1, 2}
    assert all(r['_uncertain'] and r['_global_inventory_status'] == 'partial'
               and r['_global_inventory_errors'] for r in params.components_3d)
    assert any('FAILED/PARTIAL' in line for line in pipeline['logs'])


def test_extractor_partial_accounting_keeps_only_unresolved_original(pipeline):
    pipeline['detection'].return_value.append(_proposal(3))
    pipeline['inventory']['components'][0]['source_candidate_ids'] = [1]
    pipeline['inventory']['candidate_decisions'].append(dict(
        candidate_id=3, status='reject', component_id=None, reason='Unrelated empty pads',
        rejection_category='bare_pad_or_contact'))
    params = pipeline['execute']()
    assert len(pipeline['requests']) == 2 and len(params.components_3d) == 2
    assert {r.get('component_id') for r in params.components_3d} == {'whole-assembly', None}
    assert [r['_id'] for r in params.components_3d if 'component_id' not in r] == [2]
    assert all(r['_global_inventory_status'] == 'partial' for r in params.components_3d)
    assert any('FAILED/PARTIAL: candidate 2:' in line for line in pipeline['logs'])


def test_extractor_replays_inventory_cache_without_additional_semantic_request(pipeline):
    first = pipeline['execute']()
    second = pipeline['execute']()
    assert len(pipeline['requests']) == 3  # Two board calls, one cached inventory.
    assert pipeline['global_review'].call_count == 2
    assert first.components_3d == second.components_3d
    assert any('validated cache hit' in line for line in pipeline['logs'])


def test_global_response_cannot_inject_confirmed_model_path(pipeline, tmp_path):
    path = tmp_path/'injected.step'
    path.touch()
    pipeline['inventory']['components'][0].update(model_path=str(path),
        model_selection=dict(status='confirmed', path=str(path), rotation_deg=90))
    params = pipeline['execute']()
    assert len(params.components_3d) == 1
    assert 'model_path' not in params.components_3d[0]
    assert 'model_selection' not in params.components_3d[0]


@pytest.mark.parametrize('parser', ['components', 'main_tool'])
def test_ai_parser_drops_injected_selection_even_for_existing_step(tmp_path, parser):
    """@brief AI payloads cannot impersonate a user's confirmed model choice."""
    path = tmp_path/'exists.step'
    path.touch()
    component = dict(name='visible body', kind='resistor', package='R_0603',
                     x_mm=0, y_mm=0, length_mm=1.6, width_mm=.8, height_mm=.5,
                     model_path=str(path), model_rotation_deg=90,
                     model_selection=dict(status='confirmed', path=str(path), rotation_deg=90))
    original = deepcopy(component)
    if parser == 'components':
        result = extractor._components_3d([component])
    else:
        result = extractor._params_from_tool_arguments(
            dict(_main_response(), components_3d=[component])).components_3d
    assert component == original
    assert len(result) == 1 and result[0]['package'] == 'R_0603'
    assert not {'model_path', 'model_selection', 'model_rotation_deg'} & result[0].keys()


def test_main_response_injection_stays_dropped_when_photo_inventory_fails(pipeline, tmp_path):
    path = tmp_path/'injected.step'
    path.touch()
    pipeline['main']['components_3d'] = [dict(name='fallback body', kind='ic',
        x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=1, model_path=str(path),
        model_selection=dict(status='confirmed', path=str(path), rotation_deg=0))]
    pipeline['detection'].return_value = []
    pipeline['inventory'] = None
    params = pipeline['execute']()
    assert len(pipeline['requests']) == 2 and len(params.components_3d) == 1
    assert params.components_3d[0]['_uncertain']
    assert not {'model_path', 'model_selection'} & params.components_3d[0].keys()
    assert 'Inventario fotografico fallito' in params.notes
