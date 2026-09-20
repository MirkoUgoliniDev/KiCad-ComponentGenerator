"""@brief Offline regressions for global inventory accounting and compatibility."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services.photo_yolo_review import review_yolo_components


@pytest.mark.parametrize('kind,basis,presence,accepted', [
    ('unknown', 'unknown', 'low', False),
    ('resistor', 'body', 'high', False),
    ('module', 'body', 'high', True),
    ('rf_module', 'unknown', 'low', False),
])
def test_separated_groups_require_physical_assembly(kind, basis, presence, accepted, tmp_path):
    items = [candidate(1), candidate(2)]
    items[1]['_box'] = [.6, .6, .7, .7]
    data = response()
    data['components'][0].update(**{'class': kind}, bbox_basis=basis,
                                 presence_confidence=presence)
    before = deepcopy(items)
    result, calls, _ = run(data, items, cache=tmp_path)
    assert len(calls) == 1 and items == before
    if accepted:
        assert len(result) == 1 and result[0]['component_id'] == 'assembly'
        assert not run(data, items, cache=tmp_path)[1]
    else:
        assert {r['_id'] for r in result} == {1, 2}
        assert [r['_box'] for r in result] == [r['_box'] for r in items]
        assert all(r['_global_inventory_status'] == 'partial' for r in result)
        assert not list(tmp_path.glob('*.json'))


def test_explicit_assembly_is_not_limited_to_module_class():
    items = [candidate(1), candidate(2)]
    items[1]['_box'] = [.6, .6, .7, .7]
    data = response()
    data['components'][0].update(**{'class': 'unknown'}, grouping_basis='assembly',
                                 presence_confidence='high')
    assert len(run(data, items)[0]) == 1
    data['components'][0]['grouping_basis'] = 'unresolved'
    assert len(run(data, items)[0]) == 2


@pytest.mark.parametrize('kind', ['module', 'resistor', 'unknown'])
def test_outside_source_invalidates_parent_even_for_one_source(kind):
    data = response()
    data['components'] = [body('assembly', [1], **{'class': kind}, bbox=[.6, .6, .8, .8])]
    data['candidate_decisions'] = data['candidate_decisions'][:1]
    result, _, _ = run(data, [candidate()])
    assert len(result) == 1 and result[0]['_id'] == 1
    assert result[0]['_box'] == candidate()['_box']
    assert any('outside retained envelope' in e for e in result[0]['_global_inventory_errors'])


@pytest.mark.parametrize('kind', ['unknown', 'resistor', 'module'])
def test_visible_body_survives_all_sources_unlinked(kind, tmp_path):
    data = response()
    data['components'] = [body('visible', [1], **{'class': kind},
        bbox=[.6, .6, .8, .8], presence_confidence='high', class_confidence='low')]
    data['candidate_decisions'] = [dict(candidate_id=1, status='keep',
        component_id='visible', reason='Incorrect association')]
    before = deepcopy(data)
    result, _, _ = run(data, [candidate()], cache=tmp_path)
    assert data == before
    assert len(result) == 2
    assert result[0]['component_id'] == 'visible' and result[0]['source_candidate_ids'] == []
    assert result[0]['_box'] == [.6, .6, .8, .8]
    assert result[1]['_id'] == 1 and result[1]['_box'] == candidate()['_box']
    assert all(r['_global_inventory_status'] == 'partial' for r in result)
    assert any('links removed' in e for e in result[0]['_global_inventory_errors'])
    assert not list(tmp_path.glob('*.json'))


def test_visible_body_with_separate_leads_detaches_only_outside_source():
    data = response()
    data['components'][0].update(**{'class': 'unknown'}, source_candidate_ids=[1, 2, 3],
        bbox_basis='body_and_leads', presence_confidence='high', class_confidence='low')
    data['candidate_decisions'].append(dict(candidate_id=3, status='merge',
        component_id='assembly', reason='Incorrect fragment association'))
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7]),
             dict(candidate(3), _box=[.9, .9, 1, 1])]
    result, _, _ = run(data, items)
    assert len(result) == 2
    assert result[0]['source_candidate_ids'] == [1, 2]
    assert result[0]['_box'] == data['components'][0]['bbox']
    assert result[1]['_id'] == 3 and result[1]['_box'] == items[2]['_box']


def test_high_presence_does_not_override_explicit_unresolved_group():
    data = response()
    data['components'][0].update(grouping_basis='unresolved', presence_confidence='high',
                                bbox_basis='body_and_leads')
    assert [r['_id'] for r in run(data)[0]] == [1, 2]


@pytest.mark.parametrize('sources', [[], [1]])
def test_explicit_unresolved_zero_or_one_source_never_replaces_original(sources, tmp_path):
    data = dict(components=[body('unresolved', sources, grouping_basis='unresolved',
                                presence_confidence='high')],
        candidate_decisions=[dict(candidate_id=1, status='uncertain',
            component_id='unresolved' if sources else None,
            reason='Presence unresolved', rejection_category=None)], uncertainties=[])
    original = candidate()
    before = deepcopy(data)
    result, calls, _ = run(data, [original], cache=tmp_path)
    assert data == before and len(calls) == 1
    assert len(result) == 1 and result[0]['_id'] == original['_id']
    assert result[0]['_box'] == original['_box']
    assert 'component_id' not in result[0]
    assert result[0]['_global_inventory_status'] == 'partial'
    assert any('unsupported group' in e for e in result[0]['_global_inventory_errors'])
    assert not list(tmp_path.glob('*.json'))


def test_connected_fragments_unknown_class_remain_one_body():
    items = [candidate(1), candidate(2)]
    items[1]['_box'] = [.3, .3, .5, .5]
    data = response()
    data['components'][0].update(**{'class': 'unknown'}, presence_confidence='high',
                                 class_confidence='low', grouping_basis='single_body')
    assert len(run(data, items)[0]) == 1


def test_overlap_alone_cannot_promote_an_unresolved_group():
    data = response()
    data['components'][0].update(bbox_basis='unknown', presence_confidence='low')
    for decision in data['candidate_decisions']:
        decision['status'] = 'uncertain'
    result, _, _ = run(data)
    assert [r['_id'] for r in result] == [1, 2]


def test_malformed_metadata_does_not_erase_explicit_unresolved_group():
    data = response()
    data['components'][0].update(grouping_basis='unresolved', terminal_count=True)
    assert [r['_id'] for r in run(data)[0]] == [1, 2]


@pytest.mark.parametrize('old', [False, True])
@pytest.mark.parametrize('field', ['verification_prompt', 'extra_instructions'])
def test_known_prompt_versions_and_generated_category_removed_preserving_notes(old, field):
    from kicadgen.services.photo_grounding import VERIFICATION_PROMPT
    from kicadgen.services.component_types import BLOCK_START, BLOCK_END
    template = VERIFICATION_PROMPT
    if old:
        template = template.replace('Do NOT invent exact electrical function, part numbers, markings or values.',
            'Do NOT identify electrical type, part number, markings, values or circuit function.')
    template = template.replace(' ', '\r\n  ')
    context = f'Keep measured dimensions.\n{template}\nCustom note A.\n{BLOCK_START}\nBoard-only datasheet instructions\n{BLOCK_END}\nCustom note B.'
    _, calls, _ = run(response(), **{field: context})
    prompt = calls[0]['system_prompt']
    assert 'mechanical-envelope inventory' not in prompt
    assert 'Do not add IDs' not in prompt and 'Board-only' not in prompt
    assert all(note in prompt for note in ('Keep measured dimensions.', 'Custom note A.', 'Custom note B.'))


def test_unknown_custom_verification_text_is_not_discarded():
    custom = 'This is a mechanical-envelope inventory with custom user requirements. Inspect the socket.'
    assert inventory._inventory_context(custom) == custom


def test_geometry_is_revalidated_on_cache_read(tmp_path):
    run(response(), cache=tmp_path)
    path = next(tmp_path.glob('*.json'))
    record = json.loads(path.read_text())
    record['response']['components'][0]['bbox'] = [.6, .6, .8, .8]
    path.write_text(json.dumps(record))
    result, calls, _ = run(response(), cache=tmp_path)
    assert len(calls) == 1 and len(result) == 1


def test_saved_audit_replay_without_network(monkeypatch):
    """@brief Replay local evidence through the real request/validation path offline."""
    import socket
    path = Path(__file__).resolve().parents[1]/'generated/photo-cache/yolo-audit/global-inventory-9192fbb92e084110ae701802800b896a.json'
    if not path.exists():
        pytest.skip('Private saved audit is not shipped with the repository')
    def forbidden(*args, **kwargs):
        pytest.fail('Saved audit replay must never access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    audit = json.loads(path.read_text(encoding='utf-8'))
    items = [dict(candidate(c['id']), _box=c['bbox'], **c.get('detector_metadata', {}))
             for c in audit['inputs']['candidates']]
    calls = []
    def fake(**kwargs):
        calls.append(kwargs)
        return deepcopy(audit['response'])
    w, h = audit['inputs']['image_size_px']
    result = inventory.review_global_inventory(np.zeros((h, w, 3), np.uint8), items,
        *audit['inputs']['board_size_mm'], 'offline/saved', fake, lambda text: None,
        verification_prompt=audit['prompt'].split('never impose a target count.\n', 1)[1])
    assert len(calls) == 1
    assert 'Do not add IDs or alter rectangles' not in calls[0]['system_prompt']
    assert 'TIPOLOGIA:' not in calls[0]['system_prompt']
    assert not any(r.get('component_id') == 'R13' for r in result)
    rejected = next(r for r in audit['response']['components'] if r['component_id'] == 'R13')
    originals = {r['_id']: r for r in result if 'component_id' not in r}
    for identity in rejected['source_candidate_ids']:
        assert originals[identity]['_box'] == next(c['_box'] for c in items if c['_id'] == identity)
    module = next(r for r in result if r.get('component_id') == 'MOD1')
    assert module['_box'] == next(r['bbox'] for r in audit['response']['components'] if r['component_id'] == 'MOD1')
    assert {61, 63}.issubset(originals)
    assert 89 in originals
    for identity, detached in (('SW2', 89), ('U4', 63)):
        saved = next(r for r in audit['response']['components'] if r['component_id'] == identity)
        kept = next(r for r in result if r.get('component_id') == identity)
        assert kept['_box'] == saved['bbox']
        assert kept['source_candidate_ids'] == [i for i in saved['source_candidate_ids'] if i != detached]
        assert not any(i in originals for i in kept['source_candidate_ids'])
    assert not any(r.get('component_id') == 'C15' for r in result)
    assert module['length_mm'] == pytest.approx(.3855 * 63.3)
    assert module['width_mm'] == pytest.approx(.815 * 25.4)


def candidate(identity=1):
    return dict(_id=identity, _box=[.2, .2, .4, .4], kind='generic',
                name='candidate', x_mm=-8, y_mm=-4, length_mm=8, width_mm=4,
                height_mm=1, rotation_deg=0, color_hex='#303030')


def body(identity='assembly', sources=None, **changes):
    row = dict(component_id=identity, source_candidate_ids=[1, 2] if sources is None else sources,
               **{'class': 'rf_module'}, bbox=[.1, .1, .8, .8], bbox_basis='body',
               rotation_deg=0, package='', marking='?', terminal_count=None,
               terminal_layout='unresolved', height_mm=None, height_source='unknown',
               confidence='medium', evidence='Contiguous substrate with shield and attached socket')
    row.update(changes)
    return row


def response():
    return dict(components=[body()], candidate_decisions=[
        dict(candidate_id=i, status='merge', component_id='assembly', reason='Part of retained assembly')
        for i in (1, 2)], uncertainties=['Height cannot be measured from top'])


def run(data, items=None, cache=None, **kwargs):
    calls, logs = [], []
    def fake(**request):
        calls.append(request)
        return deepcopy(data)
    result = review_yolo_components(np.zeros((60, 120, 3), np.uint8),
        [candidate(1), candidate(2)] if items is None else items,
        40, 20, 'unchanged/model', fake, logs.append, cache, strategy='global', **kwargs)
    return result, calls, logs


def test_single_call_five_images_all_candidates_and_retained_parent():
    items = [candidate(1), candidate(2)]; before = deepcopy(items)
    result, calls, logs = run(response(), items)
    assert items == before and len(calls) == 1
    assert len([b for b in calls[0]['content_blocks'] if b['type'] == 'image_url']) == 5
    metadata = json.loads(calls[0]['content_blocks'][4]['text'])
    assert [c['id'] for c in metadata['candidates']] == [1, 2]
    assert calls[0]['model'] == 'unchanged/model'
    assert len(result) == 1 and result[0]['kind'] == 'module'
    assert result[0]['source_candidate_ids'] == [1, 2]
    assert result[0]['length_mm'] == pytest.approx(28)
    assert result[0]['width_mm'] == pytest.approx(14)
    assert result[0]['_uncertain'] and not result[0]['_package_verified']
    assert any('sha256=' in line for line in logs)


def test_nullable_incomplete_lead_observation_keeps_semantic_component():
    """A provider null confidence must not erase package/class/marking data."""
    data = response()
    data['components'][0].update(
        component_id='M1', **{'class': 'rf_module'},
        package='ESP32-S3-WROOM-1U',
        marking='ESPRESSIF ESP32-S3-WROOM-1U',
        confidence='high', presence_confidence='high', class_confidence='high',
        lead_count=None, lead_count_complete=False, lead_count_confidence=None)
    for decision in data['candidate_decisions']:
        decision['component_id'] = 'M1'
    result, calls, logs = run(data)
    assert len(calls) == 1 and len(result) == 1
    assert result[0]['component_id'] == 'M1'
    assert result[0]['kind'] == 'module'
    assert result[0]['package'] == 'ESP32-S3-WROOM-1U'
    assert result[0]['marking'] == 'ESPRESSIF ESP32-S3-WROOM-1U'
    assert result[0]['lead_count'] is None
    assert result[0]['lead_count_confidence'] == 'low'
    assert not any('invalid or duplicate component' in line for line in logs)


@pytest.mark.parametrize('bad', [None, [], {}, {'components': []}])
def test_malformed_response_retains_all_candidates_visibly(bad, tmp_path):
    result, calls, logs = run(bad, cache=tmp_path)
    assert len(result) == 2 and len(calls) == 1
    assert all(r['_global_inventory_status'] == 'partial' for r in result)
    assert any('FAILED/PARTIAL' in s for s in logs)
    assert not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('box', [[0, 0, 1.1, 1], [False, 0, .9, 1],
    [0, 0, float('nan'), 1], [0, 0, float('inf'), 1], [.8, .1, .2, .9], [0, 0, 10**400, 1]])
def test_invalid_parent_box_never_loses_module(box, tmp_path):
    data = response(); data['components'][0]['bbox'] = box
    result, _, _ = run(data, cache=tmp_path)
    assert [r['_id'] for r in result] == [1, 2]
    assert not list(tmp_path.glob('*.json'))
    for path in (tmp_path/'yolo-audit').glob('*.json'):
        json.loads(path.read_text(), parse_constant=lambda s: pytest.fail(s))


@pytest.mark.parametrize('change', ['duplicate_decision', 'missing_decision', 'unknown_id',
                                  'dangling_parent', 'duplicate_parent', 'source_mismatch', 'boolean_id'])
def test_accounting_errors_are_uncached_and_preserve_uncertain_input(change, tmp_path):
    data = response()
    if change == 'duplicate_decision': data['candidate_decisions'].append(deepcopy(data['candidate_decisions'][0]))
    if change == 'missing_decision': data['candidate_decisions'].pop(0)
    if change == 'unknown_id': data['candidate_decisions'][0]['candidate_id'] = 99
    if change == 'dangling_parent': data['candidate_decisions'][0]['component_id'] = 'absent'
    if change == 'duplicate_parent': data['components'].append(deepcopy(data['components'][0]))
    if change == 'source_mismatch': data['components'][0]['source_candidate_ids'] = [2]
    if change == 'boolean_id': data['candidate_decisions'][0]['candidate_id'] = True
    result, _, _ = run(data, cache=tmp_path)
    assert any(r['_id'] == 1 for r in result)
    assert all(r['_global_inventory_status'] == 'partial' for r in result)
    assert len({r['_id'] for r in result}) == len(result)
    assert not list(tmp_path.glob('*.json'))


def test_valid_missing_body_survives_partial_response():
    data = response(); data['components'].append(body('missed', [], **{'class': 'connector'}))
    data['candidate_decisions'] = []
    result, _, _ = run(data)
    assert any(r.get('component_id') == 'missed' for r in result)
    assert {1, 2}.issubset({r['_id'] for r in result})


def test_malformed_metadata_preserves_whole_assembly_geometry(tmp_path):
    data = response(); data['components'][0]['terminal_count'] = True
    result, _, logs = run(data, cache=tmp_path)
    assert len(result) == 1 and result[0]['_box'] == [.1, .1, .8, .8]
    assert result[0]['kind'] == 'generic' and result[0]['confidence'] == 'low'
    assert result[0]['package'] == '' and result[0]['_global_inventory_status'] == 'partial'
    assert any('FAILED/PARTIAL' in s for s in logs)
    assert not list(tmp_path.glob('*.json'))


def test_empty_detector_still_requests_global_inventory_and_failure_raises():
    data = dict(components=[body('missed', [])], candidate_decisions=[], uncertainties=[])
    result, calls, _ = run(data, [])
    assert len(result) == len(calls) == 1
    with pytest.raises(ValueError, match='no recoverable bodies'):
        run({}, [])


def test_cache_revalidates_and_versions_prompt_and_schema(tmp_path, monkeypatch):
    run(response(), cache=tmp_path)
    assert not run(response(), cache=tmp_path)[1]
    monkeypatch.setattr(inventory, 'PROMPT_VERSION', 'next-prompt')
    assert len(run(response(), cache=tmp_path)[1]) == 1
    monkeypatch.setattr(inventory, 'SCHEMA_VERSION', 'next-schema')
    assert len(run(response(), cache=tmp_path)[1]) == 1
    for path in tmp_path.glob('*.json'):
        record = json.loads(path.read_text()); record['response']['candidate_decisions'] = []
        path.write_text(json.dumps(record))
    assert len(run(response(), cache=tmp_path)[1]) == 1


def test_custom_context_excludes_legacy_default_and_changes_cache(tmp_path):
    from kicadgen.services.photo_grounding import VERIFICATION_PROMPT
    _, calls, _ = run(response(), cache=tmp_path, verification_prompt=VERIFICATION_PROMPT,
                      extra_instructions='User measured board')
    prompt = calls[0]['system_prompt']
    assert VERIFICATION_PROMPT.strip() not in prompt and 'User measured board' in prompt
    _, calls, _ = run(response(), cache=tmp_path,
                      verification_prompt=VERIFICATION_PROMPT+'\nCustom detail')
    assert 'Custom detail' in calls[0]['system_prompt']


def test_terminal_tab_and_arbitrary_orientation_remain_evidence():
    data = response(); data['components'][0].update(
        **{'class': 'semiconductor_unknown'}, terminal_count=4,
        terminal_layout='three leads left and one tab right', rotation_deg=37,
        bbox_basis='body_and_leads', package='SOT-223', height_mm=1.8, height_source='nominal_package')
    row = run(data)[0][0]
    assert row['kind'] == 'ic' and row['class'] == 'semiconductor_unknown'
    assert row['terminal_count'] == 4 and 'pin_count' not in row and row['bbox_basis'] == 'leads'
    assert row['_reported_rotation_deg'] == 37 and row['rotation_deg'] == 0
    assert not row['_orientation_verified'] and row['_height_estimated']


def test_explicit_rejection_and_uncertain_parent():
    data = response(); data['components'][0]['source_candidate_ids'] = [2]
    data['candidate_decisions'][0].update(status='reject', component_id=None, reason='Empty pads',
                                        rejection_category='bare_pad_or_contact')
    data['candidate_decisions'][1]['status'] = 'uncertain'
    result, _, _ = run(data)
    assert len(result) == 1 and result[0]['source_candidate_ids'] == [2]
    data['uncertainties'] = None
    result = run(data)[0]
    assert len(result) == 1 and result[0]['source_candidate_ids'] == [2]
    assert result[0]['_global_inventory_status'] == 'partial'


@pytest.mark.parametrize('damage', ['metadata', 'bbox', 'duplicate_parent', 'missing_parent_id'])
def test_malformed_component_cannot_authorize_rejection_of_its_sources(damage, tmp_path):
    data = response()
    if damage == 'metadata': data['components'][0]['terminal_count'] = True
    if damage == 'bbox': data['components'][0]['bbox'] = [0, 0, 0, 1]
    if damage == 'duplicate_parent': data['components'].append(deepcopy(data['components'][0]))
    if damage == 'missing_parent_id': del data['components'][0]['component_id']
    data['candidate_decisions'][0].update(status='reject', component_id=None, reason='Non-component',
                                        rejection_category='background')
    data['candidate_decisions'].append(dict(candidate_id=3, status='reject',
                                           component_id=None, reason='Empty solder lands',
                                           rejection_category='bare_pad_or_contact'))
    result, _, logs = run(data, [candidate(1), candidate(2), candidate(3)], cache=tmp_path)
    assert any(r['_id'] == 1 for r in result)
    assert not any(r['_id'] == 3 for r in result)
    assert all(r['_global_inventory_status'] == 'partial' for r in result)
    assert any('candidate 1:' in s for s in logs)
    assert not list(tmp_path.glob('*.json'))


def test_one_conflicting_source_does_not_restore_unrelated_rejections(tmp_path):
    data = response()
    data['components'][0]['source_candidate_ids'] = [1]
    data['candidate_decisions'].extend(dict(candidate_id=i, status='reject',
        component_id=None, reason='Clear non-component', rejection_category='background') for i in (3, 4))
    result, _, _ = run(data, [candidate(i) for i in range(1, 5)], cache=tmp_path)
    assert len(result) == 2
    assert {r.get('component_id') for r in result} == {'assembly', None}
    assert [r['_id'] for r in result if '_source' not in r] == [2]
    assert all(r['_global_inventory_errors'] == [
        'candidate 2: invalid status/retained parent/source link'] for r in result)
    assert not list(tmp_path.glob('*.json'))


def test_invalid_competing_parent_keeps_conflicted_candidate_original():
    data = response()
    data['components'].append(body('bad-parent', [1], bbox=[0, 0, 0, 1]))
    result, _, _ = run(data)
    assert any(r['_id'] == 1 for r in result)
    assert any(r.get('component_id') == 'assembly' for r in result)


def test_api_exception_preserves_input_and_invalid_input_never_calls():
    photo = np.zeros((20, 20, 3), np.uint8)
    def fail(**kwargs):
        raise ValueError('malformed provider tool response')
    result = inventory.review_global_inventory(photo, [candidate()], 40, 20, 'model', fail, lambda _: None)
    assert result[0]['_id'] == 1 and result[0]['_global_inventory_status'] == 'partial'
    with pytest.raises(ValueError, match='unique integer'):
        inventory.review_global_inventory(photo, [candidate(), candidate()], 40, 20,
                                         'model', lambda **_: pytest.fail('network'), lambda _: None)


@pytest.mark.parametrize('category', [None, 'uncertain', 'package_mismatch', '', [], False])
def test_unstructured_or_uncertain_rejection_preserves_original(category, tmp_path):
    original = candidate()
    decision = dict(candidate_id=1, status='reject', component_id=None,
                    reason='Package/type cannot be established')
    if category is not None:
        decision['rejection_category'] = category
    data = dict(components=[], candidate_decisions=[decision], uncertainties=[])
    result, _, _ = run(data, [original], cache=tmp_path)
    assert len(result) == 1 and result[0]['_id'] == original['_id']
    for key, value in original.items():
        assert result[0][key] == value
    assert result[0]['_global_inventory_status'] == 'partial'
    assert not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('category', inventory.PHYSICAL_REJECTIONS)
def test_structured_physical_rejection_removes_only_clear_noncomponent(category):
    data = dict(components=[], candidate_decisions=[dict(candidate_id=1, status='reject',
        component_id=None, reason='Clearly visible noncomponent surface', rejection_category=category)],
        uncertainties=[])
    assert run(data, [candidate()])[0] == []


def test_uncertain_without_replacement_retains_exact_geometry_and_replays(tmp_path):
    data = dict(components=[], candidate_decisions=[dict(candidate_id=1, status='uncertain',
        component_id=None, reason='Presence unresolved', rejection_category=None)], uncertainties=[])
    first, _, _ = run(data, [candidate()], cache=tmp_path)
    second, calls, _ = run(data, [candidate()], cache=tmp_path)
    assert first == second and not calls and len(first) == 1
    assert first[0]['_box'] == candidate()['_box'] and first[0]['_id'] == 1
    assert first[0]['_global_inventory_status'] == 'validated_hypotheses'


def test_visible_unknown_has_independent_confidences_without_duplicate_original():
    data = response()
    data['components'][0].update(**{'class': 'unknown'}, package='unverified',
        presence_confidence='high', class_confidence='low')
    result, calls, _ = run(data)
    assert len(result) == 1 and result[0]['kind'] == 'generic'
    assert result[0]['presence_confidence'] == 'high'
    assert result[0]['class_confidence'] == 'low' and result[0]['package'] == ''
    assert 'Scan systematically' in calls[0]['system_prompt']
    schema = calls[0]['tool']['function']['parameters']['properties']
    assert 'rejection_category' in schema['candidate_decisions']['items']['required']
    assert 'presence_confidence' in schema['components']['items']['required']


def test_legacy_keep_merge_compatible_but_legacy_reject_cache_revalidated(tmp_path):
    _, calls, _ = run(response(), cache=tmp_path)
    assert calls and not run(response(), cache=tmp_path)[1]
    path = next(tmp_path.glob('*.json'))
    record = json.loads(path.read_text())
    record['response'] = dict(components=[], candidate_decisions=[dict(candidate_id=i,
        status='reject', component_id=None, reason='Unknown package') for i in (1, 2)], uncertainties=[])
    path.write_text(json.dumps(record))
    result, calls, _ = run(response(), cache=tmp_path)
    assert len(calls) == 1 and len(result) == 1


@pytest.mark.parametrize('damage', ['missing_evidence', 'retained_parent', 'duplicate_decision'])
def test_physical_category_alone_cannot_bypass_accounting(damage):
    decision = dict(candidate_id=1, status='reject', component_id=None,
                    reason='Bare pad', rejection_category='bare_pad_or_contact')
    data = dict(components=[], candidate_decisions=[decision], uncertainties=[])
    if damage == 'missing_evidence':
        decision['reason'] = ''
    elif damage == 'retained_parent':
        decision['component_id'] = 'missing'
    else:
        data['candidate_decisions'].append(deepcopy(decision))
    result, _, _ = run(data, [candidate()])
    assert len(result) == 1 and result[0]['_id'] == 1
    assert result[0]['_box'] == candidate()['_box']
    assert result[0]['_global_inventory_status'] == 'partial'


def test_review_candidate_metadata_reaches_verifier_and_survives_uncertainty(tmp_path):
    original = candidate()
    original.update(_yolo_confidence=.08, _yolo_class='capacitor',
        _yolo_proposal_id='tile-1:7', _yolo_review_only=True,
        _yolo_origin='low_confidence_review',
        _yolo_class_ambiguous=True, _source='YOLO local review queue',
        _uncertain=True, confidence='low')
    before = deepcopy(original)
    data = dict(components=[], candidate_decisions=[dict(candidate_id=1, status='uncertain',
        component_id=None, reason='Small body; physical presence unresolved', rejection_category=None)],
        uncertainties=[])
    result, calls, _ = run(data, [original], cache=tmp_path)
    evidence = json.loads(calls[0]['content_blocks'][4]['text'])['candidates'][0]
    assert evidence['yolo_confidence'] == .08
    assert evidence['confidence'] == 'low' and evidence['uncertain'] is True
    assert evidence['source'] == original['_source']
    assert evidence['detector_metadata'] == {k: v for k, v in original.items()
                                             if k.startswith('_yolo_')}
    assert 'Verify their physical presence independently' in calls[0]['system_prompt']
    assert original == before and len(result) == 1
    assert result[0]['_id'] == 1 and result[0]['_box'] == original['_box']
    assert result[0]['_yolo_review_only'] is True
    assert not run(data, [original], cache=tmp_path)[1]
    original['_yolo_review_only'] = False
    assert len(run(data, [original], cache=tmp_path)[1]) == 1


@pytest.mark.parametrize('presence,classification,overall,package', [
    ('high', 'low', 'low', ''), ('low', 'high', 'low', ''),
    ('high', 'high', 'medium', 'SOT-223')])
def test_independent_confidences_do_not_promote_legacy_low_confidence_package(
        presence, classification, overall, package):
    data = response()
    data['components'][0].update(presence_confidence=presence,
        class_confidence=classification, confidence=overall, package='SOT-223')
    result, _, _ = run(data)
    assert len(result) == 1
    assert result[0]['presence_confidence'] == presence
    assert result[0]['class_confidence'] == classification
    assert result[0]['confidence'] == overall and result[0]['package'] == package
    assert result[0]['_box'] == data['components'][0]['bbox']


@pytest.mark.parametrize('field', ['presence_confidence', 'class_confidence'])
@pytest.mark.parametrize('invalid', [None, True, .9, [], 'certain'])
def test_invalid_independent_confidence_keeps_generic_geometry(field, invalid, tmp_path):
    data = response()
    data['components'][0][field] = invalid
    result, _, _ = run(data, cache=tmp_path)
    assert len(result) == 1 and result[0]['kind'] == 'generic'
    assert result[0]['_box'] == data['components'][0]['bbox']
    assert result[0]['presence_confidence'] == result[0]['class_confidence'] == 'low'
    assert result[0]['_global_inventory_status'] == 'partial'
    assert not list(tmp_path.glob('*.json'))
