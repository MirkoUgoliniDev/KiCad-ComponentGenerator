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
    """@brief Righe legacy senza `grouping_basis`: e' la classe a dire se e' un assieme.

    Il campo resta assente di proposito, perche' e' l'inferenza dalla classe
    che si verifica. Con il contratto nuovo l'omissione di un campo
    obbligatorio su ogni riga fa ripetere la chiamata una volta; la risposta
    finta e' identica, quindi si tiene la prima con l'errore d'inventario, e
    una risposta cosi' non finisce mai in cache. Una cache «vecchia» gia' su
    disco, invece, si rilegge senza alcuna chiamata.
    """
    items = [candidate(1), candidate(2)]
    items[1]['_box'] = [.6, .6, .7, .7]
    data = response()
    data['components'][0].update(**{'class': kind}, bbox_basis=basis,
                                 presence_confidence=presence)
    data['components'][0].pop('grouping_basis')
    before = deepcopy(items)
    result, calls, _ = run(data, items, cache=tmp_path)
    assert len(calls) == 2 and items == before
    assert all(omission_errors(result))
    assert not list(tmp_path.glob('*.json'))
    if accepted:
        assert len(result) == 1 and result[0]['component_id'] == 'assembly'
        seed_cache(tmp_path, calls[0], data)
        replayed, calls, _ = run(data, items, cache=tmp_path)
        assert not calls and len(replayed) == 1
        assert replayed[0]['_global_inventory_status'] == 'validated_hypotheses'
    else:
        assert {r['_id'] for r in result} == {1, 2}
        assert [r['_box'] for r in result] == [r['_box'] for r in items]
        assert all(r['_global_inventory_status'] == 'partial' for r in result)


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
    """@brief Riga di risposta completa rispetto allo schema `inventory_tool()`.

    Ogni campo `required` e' presente: una risposta che ne omette uno su TUTTE
    le righe non e' piu' accettata in silenzio ma fa ripetere la chiamata, e
    qui si contano le chiamate. I valori aggiunti replicano i ripieghi che il
    codice applica quando il campo manca (presenza e classe pari alla
    confidenza complessiva, `assembly` come un rf_module senza dichiarazione,
    conteggio dei terminali non osservato), cosi' il significato di ogni test
    scritto prima di questi campi resta identico. Chi vuole verificare la
    tolleranza a un campo assente lo toglie con `pop` e attende il retry.
    """
    row = dict(component_id=identity, grouping_basis='assembly',
               source_candidate_ids=[1, 2] if sources is None else sources,
               **{'class': 'rf_module'}, bbox=[.1, .1, .8, .8], bbox_basis='body',
               rotation_deg=0, package='', marking='?', terminal_count=None,
               terminal_layout='unresolved',
               lead_count=None, lead_count_complete=False, lead_count_confidence='low',
               height_mm=None, height_source='unknown',
               confidence='medium', evidence='Contiguous substrate with shield and attached socket',
               presence_confidence='medium', class_confidence='medium',
               asymmetry_landmark=None)
    row.update(changes)
    return row


def response():
    return dict(components=[body()], candidate_decisions=[
        dict(candidate_id=i, status='merge', component_id='assembly', reason='Part of retained assembly')
        for i in (1, 2)], uncertainties=['Height cannot be measured from top'])


def run(data, items=None, cache=None, **kwargs):
    """@brief Una sola risposta finta, ripetuta tale e quale a ogni chiamata.

    Se `data` omette un campo obbligatorio su tutte le righe il codice ritenta
    una volta e riceve la stessa risposta: tiene la prima e aggiunge l'errore
    d'inventario. E' il modo in cui i test sulla tolleranza ai campi assenti
    esercitano il contratto nuovo senza inventare una seconda risposta.
    """
    return run_replies([data], items, cache, **kwargs)


def run_replies(replies, items=None, cache=None, **kwargs):
    """@brief Risposte finte in sequenza; l'ultima si ripete se le chiamate sono di piu'."""
    calls, logs = [], []
    def fake(**request):
        calls.append(request)
        return deepcopy(replies[min(len(calls), len(replies)) - 1])
    result = review_yolo_components(np.zeros((60, 120, 3), np.uint8),
        [candidate(1), candidate(2)] if items is None else items,
        40, 20, 'unchanged/model', fake, logs.append, cache, strategy='global', **kwargs)
    return result, calls, logs


def run_failing_second(first, items, error, cache=None, **kwargs):
    """@brief Prima risposta finta, seconda chiamata che fallisce come l'API.

    @param first Risposta della prima chiamata (quella che omette i campi).
    @param items Proposte del rilevatore locale.
    @param error Testo dell'errore sollevato dalla seconda chiamata, cioe' cio'
        che arriva davvero da OpenRouter: non e' testo nostro e puo' contenere
        qualunque punteggiatura.
    @return (risultato, chiamate, log), come `run_replies`.
    """
    from kicadgen.services.openrouter import OpenRouterError
    calls, logs = [], []

    def fake(**request):
        calls.append(request)
        if len(calls) >= 2:
            raise OpenRouterError(error)
        return deepcopy(first)

    result = review_yolo_components(np.zeros((60, 120, 3), np.uint8), items,
                                    40, 20, 'unchanged/model', fake, logs.append,
                                    cache, strategy='global', **kwargs)
    return result, calls, logs


def seed_cache(cache, request, data):
    """@brief Scrive su disco la cache che una corsa precedente avrebbe lasciato.

    @param cache Directory della cache.
    @param request I kwargs ricevuti dalla chiamata finta: la chiave e' il loro hash.
    @param data La risposta da far rileggere.
    @note Serve a simulare una cache «vecchia», anteriore ai campi obbligatori
        di oggi: in replay non c'e' nessuna chiamata e quindi nessun retry.
    """
    import hashlib
    key = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()
    (Path(cache)/f'global-inventory-{key}.json').write_text(
        json.dumps(dict(key=key, response=data)), encoding='utf-8')


def omission_errors(result):
    """@brief Per ogni riga, se porta l'errore d'inventario dei campi omessi."""
    return [any('omitted required fields' in e for e in r['_global_inventory_errors'])
            for r in result]


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
    # Contract changed on purpose (2026-09-26): an omitted/null confidence is
    # recorded as explicitly undeclared, no longer as a guessed 'low'.
    assert result[0]['lead_count_confidence'] == inventory.LEAD_COUNT_CONFIDENCE_UNDECLARED
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
    # L'involucro sopravvive al dubbio sulla funzione: "non so che cosa faccia"
    # non e' "non so che forma abbia", ed e' la forma che serve a cercare un
    # modello. A cancellare il campo resta la fiducia complessiva bassa o un
    # contorno irrisolto, verificati dal test sulle confidenze indipendenti.
    assert result[0]['class_confidence'] == 'low' and result[0]['package'] == 'unverified'
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
    ('high', 'high', 'medium', 'SOT-223'),
    # Il caso che mancava, ed e' quello reale di un regolatore: l'involucro e'
    # descritto bene e il contorno e' risolto, ma non si sa se sia un
    # regolatore o un transistor di potenza. Il dubbio e' sulla funzione
    # elettrica e non puo' cancellare una dichiarazione meccanica: senza
    # package la ricerca perde la chiave e scandisce l'intera famiglia.
    ('high', 'low', 'medium', 'SOT-223')])
def test_the_electrical_class_doubt_does_not_erase_the_mechanical_package(
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


# ── provenienza sbagliata vs presenza assente ────────────────────────

def test_a_wrong_link_does_not_delete_a_body_the_detector_still_backs():
    """Un id sbagliato e' un errore di provenienza, non di presenza.

    Finche' resta almeno una proposta dentro l'inviluppo il corpo e' comunque
    rilevato: si toglie il legame, non la riga. Caso reale: il tab del
    regolatore linkato al corpo ma lasciato fuori dal rettangolo.
    """
    data = response()
    data['components'][0].update(source_candidate_ids=[1, 2], bbox=[.1, .1, .5, .5],
        bbox_basis='body_and_leads', grouping_basis='single_body',
        presence_confidence='medium', confidence='low')
    items = [candidate(1), dict(candidate(2), _box=[.85, .85, .95, .95])]
    result, _, _ = run(data, items)
    assert [r.get('component_id', r.get('_id')) for r in result] == ['assembly', 2]
    assert result[0]['source_candidate_ids'] == [1]
    assert any('links removed' in e for e in result[0]['_global_inventory_errors'])


def test_without_any_backing_left_a_merely_probable_body_is_still_dropped():
    """Il negativo dello stesso contratto: se nessuna proposta resta dentro,
    la riga si regge solo sulla parola del modello e serve la dichiarazione
    esplicita di corpo visibile (presence_confidence alta)."""
    data = response()
    data['components'][0].update(source_candidate_ids=[1], bbox=[.6, .6, .8, .8],
        bbox_basis='body_and_leads', grouping_basis='single_body',
        presence_confidence='medium', confidence='low')
    data['candidate_decisions'] = data['candidate_decisions'][:1]
    result, _, _ = run(data, [candidate()])
    assert [r['_id'] for r in result] == [1]
    assert any('outside retained envelope' in e for e in result[0]['_global_inventory_errors'])


# ── corpo unico dichiarato: stessa asticella dell'assembly ───────────

def test_a_declared_single_body_binds_separate_leads_at_medium_presence():
    """Un assieme di pezzi distinti passa a 'medium'; un corpo unico, che e'
    un'affermazione piu' forte, non puo' chiedere piu' prove."""
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    data = response()
    data['components'][0].update(grouping_basis='single_body', bbox_basis='body_and_leads',
                                 presence_confidence='medium', confidence='low')
    assert [r['component_id'] for r in run(data, items)[0]] == ['assembly']


def test_a_guessed_single_body_cannot_bind_separate_leads():
    """Sotto 'medium' resta un'ipotesi: i frammenti staccati tornano separati."""
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    data = response()
    data['components'][0].update(grouping_basis='single_body', bbox_basis='body_and_leads',
                                 presence_confidence='low', confidence='low')
    assert [r['_id'] for r in run(data, items)[0]] == [1, 2]


@pytest.mark.parametrize('landmark,expected', [
    ({'feature': 'coaxial_jack', 'side': 'right', 'confidence': 'high'},
     {'landmark_feature': 'coaxial_jack', 'landmark_side': 'right',
      'landmark_confidence': 'high'}),
    ({'feature': 'chamfer', 'side': 'top', 'confidence': 'medium'},
     {'landmark_feature': 'chamfer', 'landmark_side': 'top',
      'landmark_confidence': 'medium'}),
    # Un corpo simmetrico non ha un capo distinguibile dall'altro: l'assenza e'
    # una risposta, e inventargliela ruoterebbe il pezzo di mezzo giro.
    (None, {}),
    ({}, {}),
    # Una confidenza bassa vale come assenza: qui un errore vale 180 gradi.
    ({'feature': 'coaxial_jack', 'side': 'right', 'confidence': 'low'}, {}),
    # Vocabolari chiusi: un lato o una feature fuori elenco non si interpretano.
    ({'feature': 'coaxial_jack', 'side': 'sopra', 'confidence': 'high'}, {}),
    ({'feature': 'antenna', 'side': 'right', 'confidence': 'high'}, {}),
    # Mezza dichiarazione non e' confrontabile con niente.
    ({'feature': 'coaxial_jack', 'confidence': 'high'}, {}),
    ('lower right', {}),
])
def test_the_asymmetry_landmark_is_read_only_when_it_is_complete(landmark, expected):
    """@brief E' cio' che rende verificabile un orientamento.

    Un ingombro da solo non dice quale capo sia quale. Prima il verso lo
    decideva una regex inglese su prosa libera, ancorata al nome di un file;
    ora e' un campo dichiarato, con vocabolari chiusi, che vale solo se
    completo.
    """
    data = response()
    data['components'][0]['asymmetry_landmark'] = landmark
    result, _, _ = run(data)
    assert len(result) == 1
    got = {k: v for k, v in result[0].items() if k.startswith('landmark_')}
    assert got == expected


def test_a_response_without_the_landmark_field_is_still_accepted(tmp_path):
    """@brief Le risposte in cache sono anteriori al campo: l'assenza non cancella la riga.

    Su una risposta fresca il campo omesso su ogni riga fa ripetere la
    chiamata una volta (contratto nuovo); la seconda finta e' identica, quindi
    resta la prima con l'errore d'inventario, e la riga sopravvive senza
    landmark. La cache «vecchia» si rilegge senza chiamate ne' errore.
    """
    data = response()
    data['components'][0].pop('asymmetry_landmark', None)
    result, calls, _ = run(data)
    assert len(result) == 1 and len(calls) == 2
    assert not any(k.startswith('landmark_') for k in result[0])
    assert omission_errors(result) == [True]
    seed_cache(tmp_path, calls[0], data)
    replayed, calls, _ = run(data, cache=tmp_path)
    assert not calls and len(replayed) == 1
    assert replayed[0]['_global_inventory_errors'] == []
    assert not any(k.startswith('landmark_') for k in replayed[0])


def _boxed(*boxes):
    return [dict(bbox=list(box)) for box in boxes]


def test_boxes_delivered_in_pixels_or_on_a_thousand_grid_are_converted():
    """@brief Un rettangolo giusto in un'altra unita' non e' una risposta sbagliata.

    Un modello di grounding risponde spesso in pixel o su griglia 0-1000 anche
    quando il contratto chiede frazioni. Scartare tutto significherebbe buttare
    una risposta corretta per un'unita' di misura.
    """
    messages = []
    rows = _boxed([250, 100, 500, 400])
    assert inventory._rescale_boxes(rows, 2000, 1000, messages.append) == 'normalized_1000'
    assert rows[0]['bbox'] == pytest.approx([.25, .1, .5, .4])
    assert 'normalized_1000' in messages[0]

    # Oltre mille non puo' essere la griglia: restano i pixel dell'immagine.
    rows = _boxed([500, 200, 1500, 800])
    assert inventory._rescale_boxes(rows, 2000, 1000, messages.append) == 'pixels'
    assert rows[0]['bbox'] == pytest.approx([.25, .2, .75, .8])


def test_fractions_are_left_alone_and_a_broken_answer_stays_broken():
    """@brief Caso negativo: la conversione non deve salvare cio' che e' rotto.

    @note Il messaggio di rinuncia e' stato tradotto in italiano insieme agli
        altri avanzamenti del modulo (l'interfaccia e' italiana): qui si
        asserisce la nuova frase, non una piu' debole -- resta l'assenza di
        qualunque conversione quando nessuna convenzione vale per TUTTI i
        rettangoli.
    """
    messages = []
    rows = _boxed([.25, .1, .5, .4])
    assert inventory._rescale_boxes(rows, 2000, 1000, messages.append) is None
    assert rows[0]['bbox'] == [.25, .1, .5, .4] and not messages

    # Nessuna convenzione rende validi TUTTI i rettangoli: non si tocca nulla,
    # perche' una conversione che ne salva una parte dice che il problema e'
    # un altro.
    rows = _boxed([250, 100, 500, 400], [100, 100, 90, 400])
    assert inventory._rescale_boxes(rows, 2000, 1000, messages.append) is None
    assert rows[0]['bbox'] == [250, 100, 500, 400]
    assert 'nessuna convenzione coerente' in messages[-1]

    # Un intero enorme o un non-numero descrive una risposta rotta, non
    # un'unita' diversa.
    for broken in ([0, 0, 10**400, 1], [0, 0, float('nan'), 1], ['x', 0, 1, 1]):
        rows = _boxed(broken)
        assert inventory._rescale_boxes(rows, 2000, 1000, messages.append) is None


def test_a_merged_box_that_throws_away_its_own_sources_is_reported():
    """@brief La fusione che esclude meta' di una sorgente e' una contraddizione.

    Sul modulo radio della scheda di prova la bbox fusa era il 47% dell'area
    dell'unione delle proposte che dichiarava di fondere, e passava senza un
    avviso perche' la validazione controlla solo la SOVRAPPOSIZIONE. Non si
    corregge il rettangolo -- allargarlo all'unione importerebbe gli errori dei
    frammenti -- ma non deve restare invisibile.
    """
    # La sorgente 2 sta quasi tutta fuori dalla bbox dichiarata.
    boxes = {1: [.10, .10, .30, .30], 2: [.30, .10, .80, .30]}
    row = dict(component_id='M1', source_candidate_ids=[1, 2], grouping_basis='assembly',
               bbox=[.10, .10, .35, .30], bbox_basis='body', rotation_deg=None,
               **{'class': 'rf_module'}, package='', marking='', terminal_layout='',
               terminal_count=None, lead_count=None, lead_count_complete=False,
               lead_count_confidence='low', height_mm=None, height_source='unknown',
               confidence='medium', evidence='one shielded assembly',
               presence_confidence='high', class_confidence='medium', asymmetry_landmark=None)
    data = dict(components=[row], uncertainties=[], candidate_decisions=[
        dict(candidate_id=i, status='merge', component_id='M1', reason='fragment',
             rejection_category=None) for i in (1, 2)])
    _, _, errors = inventory._validate(data, {1, 2}, boxes)
    assert any('excludes most of' in message and '[2]' in message for message in errors)

    # Caso negativo: la stessa fusione che contiene le sue sorgenti non segnala
    # nulla, e un pezzo che sporge un po' resta accettabile.
    data['components'][0]['bbox'] = [.10, .08, .82, .32]
    _, _, clean = inventory._validate(data, {1, 2}, boxes)
    assert not [message for message in clean if 'excludes most of' in message]


# ── un errore su una riga degrada quella riga, non l'inventario ────────

def _clean_row(identity, source, box):
    return body(identity, [source], **{'class': 'resistor'}, bbox=list(box),
                grouping_basis='single_body', presence_confidence='high',
                class_confidence='medium')


def test_a_geometric_inconsistency_degrades_only_the_row_it_belongs_to():
    """@brief Caso positivo: l'errore resta sulla riga incriminata.

    Prima l'elenco degli errori era unico e finiva su ogni componente, e lo
    status era unico per l'inventario: su una corsa reale la fusione
    incoerente di UN modulo ha marcato 55 componenti su 55 come parziali.
    Qui M1 dichiara di fondere la proposta 2 e poi la lascia quasi tutta
    fuori dal suo rettangolo; R1 non c'entra nulla e deve restare com'era.
    """
    items = [candidate(1), dict(candidate(2), _box=[.35, .1, .8, .3]),
             dict(candidate(3), _box=[.1, .6, .3, .7])]
    items[0]['_box'] = [.1, .1, .3, .3]
    data = dict(components=[
        body('M1', [1, 2], bbox=[.1, .1, .36, .3], grouping_basis='assembly',
             presence_confidence='high', class_confidence='medium'),
        _clean_row('R1', 3, [.1, .6, .3, .7])],
        candidate_decisions=[
            dict(candidate_id=i, status='merge', component_id='M1', reason='fragment',
                 rejection_category=None) for i in (1, 2)]
        + [dict(candidate_id=3, status='keep', component_id='R1', reason='chip body',
                rejection_category=None)],
        uncertainties=[])
    result, _, logs = run(data, items)
    rows = {r['component_id']: r for r in result}
    assert set(rows) == {'M1', 'R1'}
    # La riga sana conserva la presenza dichiarata dal modello e passa i
    # controlli per conto suo.
    assert rows['R1']['presence_confidence'] == 'high'
    assert rows['R1']['_global_inventory_status'] == 'validated_hypotheses'
    assert rows['R1']['_global_inventory_errors'] == []
    # La riga colpita e' degradata: parziale e con il suo errore. La presenza
    # resta quella dichiarata, perche' l'incoerenza e' di provenienza, non di
    # assenza: il corpo e' comunque coperto dalle altre proposte.
    assert rows['M1']['_global_inventory_status'] == 'partial'
    assert rows['M1']['_global_inventory_errors'] == [
        'component M1: merged box excludes most of sources [2]']
    assert rows['M1']['presence_confidence'] == 'high'
    # L'inventario nel suo insieme resta non certificato: log e cache non
    # cambiano contratto.
    assert any('FAILED/PARTIAL' in line for line in logs)


def test_a_dropped_row_hands_its_error_to_the_proposals_that_replace_it():
    """@brief Quando la riga cade, l'errore segue le proposte che tornano originali."""
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7]),
             dict(candidate(3), _box=[.1, .8, .2, .9])]
    data = dict(components=[
        body('G1', [1, 2], grouping_basis='unresolved', presence_confidence='high'),
        _clean_row('R1', 3, [.1, .8, .2, .9])],
        candidate_decisions=[
            dict(candidate_id=i, status='uncertain', component_id='G1', reason='doubtful',
                 rejection_category=None) for i in (1, 2)]
        + [dict(candidate_id=3, status='keep', component_id='R1', reason='chip body',
                rejection_category=None)],
        uncertainties=[])
    result, _, _ = run(data, items)
    originals = {r['_id']: r for r in result if 'component_id' not in r}
    assert set(originals) == {1, 2}
    for original in originals.values():
        assert original['_global_inventory_status'] == 'partial'
        assert any('unsupported group' in e for e in original['_global_inventory_errors'])
    clean = next(r for r in result if r.get('component_id') == 'R1')
    assert clean['_global_inventory_status'] == 'validated_hypotheses'
    assert clean['_global_inventory_errors'] == []


@pytest.mark.parametrize('damage', ['uncertainties', 'api_error'])
def test_an_error_of_the_response_itself_still_reaches_every_row(damage):
    """@brief Caso negativo, per ragione strutturale.

    Un errore che non nomina nessuna riga ne' nessuna proposta -- la risposta
    e' malformata nel suo insieme, o la chiamata e' fallita -- non ha una riga
    da incolpare: vale per tutte, come prima. La distinzione non e' il tipo
    di messaggio ma se esiste una riga trattenuta che lo puo' reclamare.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    data = dict(components=[_clean_row('R1', 1, [.2, .2, .4, .4]),
                            _clean_row('R2', 2, [.6, .6, .7, .7])],
                candidate_decisions=[dict(candidate_id=i, status='keep', component_id=f'R{i}',
                                          reason='chip body', rejection_category=None)
                                     for i in (1, 2)],
                uncertainties=[])
    if damage == 'uncertainties':
        data['uncertainties'] = None
        result, _, _ = run(data, items)
        assert len(result) == 2
        assert all(r['_global_inventory_errors'] == ['uncertainties missing/invalid'] for r in result)
    else:
        def fail(**kwargs):
            raise ValueError('provider refused the tool call')
        result = inventory.review_global_inventory(np.zeros((60, 120, 3), np.uint8), items,
                                                   40, 20, 'model', fail, lambda _: None)
        assert len(result) == 2
        # Con la chiamata fallita mancano anche componenti, decisioni e
        # incertezze: tutti errori della risposta, tutti su ogni riga. La
        # decisione mancante, invece, e' di ciascuna proposta e resta sua.
        for r in result:
            assert {'provider refused the tool call', 'components missing',
                    'candidate_decisions missing', 'uncertainties missing/invalid'
                    }.issubset(r['_global_inventory_errors'])
            assert [e for e in r['_global_inventory_errors'] if e.startswith('candidate ')] == [
                f"candidate {r['_id']}: missing/duplicate decision"]
    assert all(r['_global_inventory_status'] == 'partial' for r in result)


def test_a_missing_presence_field_is_read_as_the_overall_confidence(tmp_path):
    """@brief L'assenza del campo non e' una dichiarazione di assenza.

    Lo schema lo chiede obbligatorio, ma il provider puo' ometterlo e le
    risposte in cache anteriori al campo non lo hanno. La confidenza
    complessiva e' per contratto l'incertezza globale, classe compresa:
    e' un limite inferiore della presenza, e lo stesso che i controlli
    geometrici usavano gia'. Prima la normalizzazione metteva 'low' fisso e
    una risposta valida intera perdeva ogni presenza.

    Contratto nuovo: l'omissione su ogni riga non e' accettata in silenzio.
    Su una risposta fresca la chiamata si ripete una volta; la seconda finta e'
    identica, quindi si tiene la prima e ogni riga porta l'errore
    d'inventario, ma il ripiego sulla confidenza resta quello. Una cache
    anteriore al campo si rilegge invece senza chiamate e senza errore.
    """
    for overall in ('high', 'medium', 'low'):
        data = response()
        data['components'][0].pop('presence_confidence', None)
        data['components'][0]['confidence'] = overall
        result, calls, _ = run(data)
        assert len(result) == 1 and len(calls) == 2
        assert result[0]['presence_confidence'] == overall
        assert omission_errors(result) == [True]
    # Replay: la cache «vecchia» non paga nessun retry e legge lo stesso ripiego.
    seed_cache(tmp_path, calls[0], data)
    replayed, calls, _ = run(data, cache=tmp_path)
    assert not calls and len(replayed) == 1
    assert replayed[0]['presence_confidence'] == 'low'
    assert replayed[0]['_global_inventory_errors'] == []
    # Il campo dichiarato vince sempre sul ripiego.
    data = response()
    data['components'][0].update(confidence='high', presence_confidence='low')
    assert run(data)[0][0]['presence_confidence'] == 'low'


def test_the_real_module_merge_leaves_the_rest_of_the_board_with_its_presence():
    """@brief Estratto fedele della risposta reale che ha fatto emergere il difetto.

    Righe, rettangoli e decisioni sono copiati dall'audit della corsa: il
    modello ha fuso nel modulo le proposte 44 e 45 lasciandone fuori piu' di
    meta'. Con il contratto vecchio 55 componenti su 55 uscivano 'low' e
    'partial' con lo stesso errore. Nella corsa reale mancavano anche
    presence_confidence, class_confidence e lead_count_confidence su ogni
    riga: oggi quell'omissione fa ripetere la chiamata (vedi i test sul retry),
    percio' qui le righe portano i campi con i valori che il ripiego
    assegnava, e resta sotto prova solo l'attribuzione per riga dell'errore.
    """
    boxes = {2: [0.6885578143483833, 0.007545103707988857, 0.8699513581136185, 0.4159765730308681],
             3: [0.861069036275362, 0.029376821840177928, 1.0, 0.448181108465133],
             12: [0.6145262002944946, 0.14233062495266594, 1.0, 0.854081887910899],
             28: [0.624565560821485, 0.3450787401574803, 0.7248815165876777, 0.7982283464566929],
             44: [0.6258293838862559, 0.5387795275590551, 0.7324644549763033, 0.8305118110236221],
             45: [0.8238862160457593, 0.5422589256127561, 1.0, 0.8696284674638299],
             11: [0.0, 0.13145354207173943, 0.07492483258247375, 0.27030856878443815]}
    module = dict(component_id='U1_MODULE', grouping_basis='assembly',
        source_candidate_ids=[2, 3, 12, 28, 44, 45], **{'class': 'rf_module'},
        bbox=[0.617, 0.108, 0.999, 0.68], bbox_basis='body_and_leads', rotation_deg=0.0,
        package='shielded SMD module with castellated/edge pads',
        marking='ESPRESSIF ESP32-S3-WROOM-1U MCN16R8',
        terminal_layout='castellated pads along left, top and bottom edges; coaxial antenna jack at lower-right',
        terminal_count=None, lead_count=None, lead_count_complete=False,
        lead_count_confidence='low', height_mm=3.2, height_source='nominal_package',
        confidence='high', presence_confidence='high', class_confidence='high',
        evidence='Large metal-shielded daughterboard module with castellated pads and coaxial jack.',
        asymmetry_landmark={'feature': 'coaxial_jack', 'side': 'right', 'confidence': 'high'})
    switch = dict(component_id='SW_BOOT', grouping_basis='single_body', source_candidate_ids=[11],
        **{'class': 'switch'}, bbox=[0.0, 0.128, 0.076, 0.272], bbox_basis='body_and_leads',
        rotation_deg=90.0, package='SMD tactile switch, ~4.5x3.5mm body', marking='?',
        terminal_layout='gull-wing leads on left and right sides of body; actuator cap on top face',
        terminal_count=2, lead_count=2, lead_count_complete=False, lead_count_confidence='low',
        height_mm=1.6, height_source='visual_estimate', confidence='medium',
        presence_confidence='medium', class_confidence='medium',
        evidence='Plastic switch housing with round metal dome actuator; solder fillets on visible right-side leads.',
        asymmetry_landmark={'feature': 'other', 'side': 'right', 'confidence': 'medium'})
    data = dict(components=[module, switch], uncertainties=[], candidate_decisions=[
        dict(candidate_id=i, status='merge', component_id='U1_MODULE', reason='Fragment of the shielded module assembly.',
             rejection_category=None) for i in (2, 3, 12, 28, 44, 45)]
        + [dict(candidate_id=11, status='keep', component_id='SW_BOOT',
                reason='Tactile pushbutton body with metal cap and visible side leads.', rejection_category=None)])
    items = [dict(candidate(i), _box=list(box)) for i, box in boxes.items()]
    result, _, logs = run(data, items)
    rows = {r['component_id']: r for r in result}
    assert set(rows) == {'U1_MODULE', 'SW_BOOT'}
    assert rows['U1_MODULE']['_global_inventory_status'] == 'partial'
    assert rows['U1_MODULE']['_global_inventory_errors'] == [
        'component U1_MODULE: merged box excludes most of sources [44, 45]']
    assert rows['U1_MODULE']['presence_confidence'] == 'high'
    assert rows['SW_BOOT']['_global_inventory_status'] == 'validated_hypotheses'
    assert rows['SW_BOOT']['_global_inventory_errors'] == []
    assert rows['SW_BOOT']['presence_confidence'] == 'medium'
    assert rows['SW_BOOT']['class_confidence'] == 'medium'
    assert any('FAILED/PARTIAL: component U1_MODULE' in line for line in logs)


# ── campi obbligatori omessi su ogni riga: un retry, poi si dichiara ──────

def test_omitted_required_fields_distinguishes_contract_from_row_omissions():
    """@brief Unita': omesso su TUTTE le righe, su alcune, su nessuna.

    Un campo assente su una riga e' un'omissione di quella riga; assente su
    tutte e' il modello che non ha onorato `required` dello schema. Solo il
    secondo caso e' una violazione di contratto, e l'ordine restituito e'
    quello dello schema, non quello di scoperta.
    """
    complete = dict(components=[_clean_row('R1', 1, [.2, .2, .4, .4]),
                                _clean_row('R2', 2, [.6, .6, .7, .7])],
                    candidate_decisions=[], uncertainties=[])
    assert inventory.omitted_required_fields(complete) == []

    everywhere = deepcopy(complete)
    for row in everywhere['components']:
        row.pop('class_confidence'); row.pop('presence_confidence')
    assert inventory.omitted_required_fields(everywhere) == ['presence_confidence', 'class_confidence']

    somewhere = deepcopy(complete)
    somewhere['components'][0].pop('presence_confidence')
    assert inventory.omitted_required_fields(somewhere) == []

    # Senza righe da giudicare non c'e' contratto da violare: una risposta
    # vuota o malformata e' un altro errore, con un altro nome.
    for empty in (None, {}, {'components': []}, {'components': 'malformed'},
                  {'components': ['not a row']}):
        assert inventory.omitted_required_fields(empty) == []


def _without(fields, *rows):
    """@brief Copie delle righe senza i campi indicati."""
    stripped = []
    for row in rows:
        row = deepcopy(row)
        for name in fields:
            row.pop(name, None)
        stripped.append(row)
    return stripped


def _two_row_response(rows):
    return dict(components=rows, uncertainties=[],
                candidate_decisions=[dict(candidate_id=i, status='keep', component_id=f'R{i}',
                                          reason='chip body', rejection_category=None)
                                     for i in (1, 2)])


def test_a_contract_omission_is_retried_once_and_the_complete_answer_wins(tmp_path):
    """@brief Positivo: la seconda risposta porta i campi ed e' quella usata.

    La prima risposta omette presence_confidence e class_confidence su ogni
    riga; la seconda le dichiara 'high' con confidenza complessiva 'medium',
    cosi' il valore letto a valle dice da sola quale risposta e' stata usata.
    L'unica differenza fra le due richieste e' la coda del prompt che nomina i
    campi omessi: e' l'informazione che il primo tentativo non aveva.

    @note Contratto dei log aggiornato: gli avanzamenti dell'inventario
        globale sono passati in italiano (l'interfaccia lo e') e non ripetono
        piu' l'identificativo del modello, che la modale mostra
        nell'intestazione. Le asserzioni seguono la frase nuova senza
        allentarsi: continuano a esigere che il secondo tentativo sia
        dichiarato a pagamento e che il suo esito sia detto esplicitamente.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    complete = [dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='medium'),
                dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='medium')]
    first = _two_row_response(_without(('presence_confidence', 'class_confidence'), *complete))
    second = _two_row_response(complete)
    result, calls, logs = run_replies([first, second], items, cache=tmp_path)
    assert len(calls) == 2
    assert 'omitted these REQUIRED fields' not in calls[0]['system_prompt']
    assert calls[1]['system_prompt'].startswith(calls[0]['system_prompt'])
    assert 'presence_confidence, class_confidence' in calls[1]['system_prompt']
    assert calls[1]['content_blocks'] == calls[0]['content_blocks']
    assert len(result) == 2
    assert [r['presence_confidence'] for r in result] == ['high', 'high']
    assert [r['class_confidence'] for r in result] == ['medium', 'medium']
    assert all(r['_global_inventory_errors'] == [] for r in result)
    assert all(r['_global_inventory_status'] == 'validated_hypotheses' for r in result)
    # Contratto aggiornato: gli avanzamenti dell'inventario globale sono in
    # italiano e non ripetono l'identificativo del modello (lo mostra gia'
    # l'intestazione della modale). Restano asserite le due cose che contano:
    # che il secondo tentativo sia dichiarato A PAGAMENTO e che venga usato.
    assert any('un solo nuovo tentativo a pagamento' in line for line in logs)
    assert any('il nuovo tentativo porta i campi obbligatori' in line for line in logs)


def test_a_complete_answer_is_never_retried(tmp_path):
    """@brief Negativo: con tutti i campi c'e' una sola chiamata, e la cache si scrive.

    @note Qui non c'e' nessun secondo tentativo, quindi non c'e' nessuna
        dichiarazione di spesa da esigere: cio' che si asserisce e' l'assenza
        della riga. La dichiarazione «a pagamento» del retry e' contratto dei
        test in cui il retry avviene davvero.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    data = _two_row_response([_clean_row('R1', 1, [.2, .2, .4, .4]),
                              _clean_row('R2', 2, [.6, .6, .7, .7])])
    result, calls, logs = run(data, items, cache=tmp_path)
    assert len(calls) == 1 and len(result) == 2
    assert 'omitted these REQUIRED fields' not in calls[0]['system_prompt']
    assert not any('nuovo tentativo' in line for line in logs)
    assert all(r['_global_inventory_errors'] == [] for r in result)
    assert len(list(tmp_path.glob('*.json'))) == 1
    assert not run(data, items, cache=tmp_path)[1]


def test_a_retry_that_still_omits_keeps_the_first_answer_and_declares_it(tmp_path):
    """@brief Il secondo tentativo omette ancora: si tiene il primo, si dichiara.

    L'errore e' della risposta nel suo insieme e raggiunge ogni riga; la
    presenza ripiega sulla confidenza complessiva (vedi `_presence`) e una
    risposta cosi' non finisce in cache come buona.

    @note Il secondo tentativo qui avviene davvero, quindi si esige che sia
        dichiarato A PAGAMENTO e che il suo esito sia detto esplicitamente:
        sono due frasi distinte nel log e il test le pretende entrambe.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    rows = _without(('presence_confidence', 'class_confidence'),
                    dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='high'),
                    dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='low'))
    result, calls, logs = run(_two_row_response(rows), items, cache=tmp_path)
    assert len(calls) == 2
    assert 'presence_confidence, class_confidence' in calls[1]['system_prompt']
    assert len(result) == 2 and omission_errors(result) == [True, True]
    assert all(r['_global_inventory_errors'] == [
        'model omitted required fields on every component: presence_confidence, class_confidence']
        for r in result)
    assert all(r['_global_inventory_status'] == 'partial' for r in result)
    assert [r['presence_confidence'] for r in result] == ['high', 'low']
    assert [r['class_confidence'] for r in result] == ['high', 'low']
    assert 'a pagamento' in _retry_charge_line(logs)
    outcome = _retry_outcome_line(logs)
    assert 'che omette ancora presence_confidence, class_confidence' in outcome
    assert 'si tiene la prima risposta' in outcome
    assert 'fallito' not in outcome, 'il secondo tentativo ha risposto, non e’ fallito'
    assert not list(tmp_path.glob('*.json'))


def test_a_field_missing_on_some_rows_only_is_a_row_omission_not_a_retry(tmp_path):
    """@brief Un campo assente su ALCUNE righe non e' una violazione di contratto.

    La riga senza il campo ripiega sulla confidenza complessiva, l'altra tiene
    il valore dichiarato, e nessuno paga una seconda chiamata.

    @note Nessun secondo tentativo: cio' che si esige e' che il log non ne
        parli affatto. Pretendere qui la dichiarazione di spesa del retry
        sarebbe una promessa vuota, perche' la riga non esiste.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    declared = _clean_row('R1', 1, [.2, .2, .4, .4])
    (bare,) = _without(('presence_confidence',),
                       dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='low'))
    result, calls, logs = run(_two_row_response([declared, bare]), items, cache=tmp_path)
    assert len(calls) == 1 and len(result) == 2
    assert not any('nuovo tentativo' in line for line in logs)
    assert omission_errors(result) == [False, False]
    assert [r['presence_confidence'] for r in result] == ['high', 'low']
    assert all(r['_global_inventory_status'] == 'validated_hypotheses' for r in result)


def test_the_accepted_retry_is_cached_under_the_original_request(tmp_path):
    """@brief La corsa successiva con gli stessi input rigioca la cache: zero chiamate.

    La cache e' la risposta accettata PER QUELLA richiesta. Se il retry fosse
    messo in cache sotto la sua chiave (prompt con la coda che nomina i campi),
    la lettura -- che usa la chiave della richiesta originale -- non la
    troverebbe mai, e ogni esecuzione pagherebbe due chiamate per sempre.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    complete = [dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='medium'),
                dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='medium')]
    first = _two_row_response(_without(('presence_confidence', 'class_confidence'), *complete))
    second = _two_row_response(complete)
    _, calls, _ = run_replies([first, second], items, cache=tmp_path)
    assert len(calls) == 2
    replayed, calls_again, logs = run_replies([first, second], items, cache=tmp_path)
    assert calls_again == [], 'la risposta accettata deve rigiocarsi dalla cache'
    assert any('validated cache hit' in line for line in logs)
    assert [r['presence_confidence'] for r in replayed] == ['high', 'high']


@pytest.mark.parametrize('degenerate', [None, {}, {'components': []},
                                        {'components': [], 'candidate_decisions': [],
                                         'uncertainties': []}])
def test_a_degenerate_retry_never_replaces_the_first_answer(tmp_path, degenerate):
    """@brief Un secondo tentativo senza righe da giudicare non «omette nulla»: non e' una risposta.

    Senza questa guardia una risposta vuota o malformata passava per completa
    e sostituiva la prima, che aveva due righe valide: si sarebbero perse le
    righe per far posto al nulla.

    @note Il secondo tentativo avviene: si esige che sia dichiarato A
        PAGAMENTO e che il suo esito sia detto per quello che e'. Per tutti e
        quattro i parametri il codice prende lo stesso ramo, quello delle
        righe mancanti: accettare in alternativa «fallito» non tollererebbe
        una frase diversa, tollererebbe un'AFFERMAZIONE diversa sui soldi
        dell'utente -- «la chiamata e' fallita» invece di «ha risposto senza
        righe» -- e la lascerebbe passare per sempre.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    complete = [dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='medium'),
                dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='medium')]
    first = _two_row_response(_without(('presence_confidence', 'class_confidence'), *complete))
    result, calls, logs = run_replies([first, degenerate], items, cache=tmp_path)
    assert len(calls) == 2
    kept = [r for r in result if r.get('component_id') in ('R1', 'R2')]
    assert len(kept) == 2, 'le righe della prima risposta devono restare'
    assert all(r['presence_confidence'] == 'medium' for r in kept), \
        'la presenza ripiega sulla confidenza complessiva della prima risposta'
    assert 'a pagamento' in _retry_charge_line(logs)
    outcome = _retry_outcome_line(logs)
    assert 'senza righe da giudicare' in outcome
    assert 'si tiene la prima risposta' in outcome
    assert 'fallito' not in outcome, \
        'la chiamata ha risposto: dirla fallita direbbe un’altra cosa sulla spesa'


def _retry_charge_line(logs):
    """@brief La riga che ANNUNCIA la spesa del secondo tentativo."""
    lines = [line for line in logs if 'un solo nuovo tentativo' in line]
    assert len(lines) == 1, f'una sola riga deve annunciare il secondo tentativo: {lines}'
    return lines[0]


def _retry_outcome_line(logs):
    """@brief La riga che dichiara l'ESITO del secondo tentativo a pagamento."""
    lines = [line for line in logs
             if line.startswith('Inventario globale: nuovo tentativo ')]
    assert len(lines) == 1, f'un solo esito del secondo tentativo: {lines}'
    return lines[0]


# ── Forma delle righe che parlano di spesa: non devono poter sparire ──
#
# Il filtro della modale (`per_item_detail`) riconosce il rendiconto
# per-elemento dalla forma «intestazione: campo; campo; campo». Una riga di
# fase che elenchi i suoi motivi con il punto e virgola cambia forma al
# crescere dei motivi, e finiva nascosta proprio nei casi peggiori: due errori
# di contabilita', o un errore dell'API che contiene punti e virgola. Il
# contratto verificato qui e' quello del SERVIZIO -- la riga non produce
# clausole -- e resta valido qualunque sia la taratura del filtro nella GUI.


def _semicolon_clauses(line):
    """@brief Le clausole che il filtro della modale conterebbe in questa riga."""
    return line.partition(': ')[2].split(';')


def _two_accounting_errors(items):
    """@brief Risposta con DUE errori di contabilita', uno per riga.

    Ogni riga dichiara di assorbire la propria proposta e poi la lascia fuori
    dal proprio rettangolo: due errori distinti, ognuno attribuito alla sua
    riga.
    """
    return dict(
        components=[body('M1', [1], bbox=[.05, .05, .1, .1], grouping_basis='single_body',
                         presence_confidence='high', class_confidence='medium'),
                    body('M2', [2], bbox=[.9, .9, .95, .95], grouping_basis='single_body',
                         presence_confidence='high', class_confidence='medium')],
        candidate_decisions=[dict(candidate_id=1, status='merge', component_id='M1',
                                  reason='fuori dal rettangolo'),
                             dict(candidate_id=2, status='merge', component_id='M2',
                                  reason='fuori dal rettangolo')],
        uncertainties=[])


def _failed_partial_line(logs):
    """@brief L'unica riga che dichiara l'inventario parziale o fallito."""
    lines = [line for line in logs if 'FAILED/PARTIAL' in line]
    assert len(lines) == 1, f'un solo avviso FAILED/PARTIAL: {lines}'
    return lines[0]


def test_the_partial_failure_warning_keeps_its_shape_with_two_errors():
    """@brief Positivo: due errori restano una riga sola, senza clausole.

    Due errori di contabilita' sono il caso in cui l'avviso serve di piu', ed
    erano il caso in cui spariva: la riga vecchia («err; err; proposte
    irrisolte conservate.») raggiungeva tre clausole e veniva nascosta, mentre
    la riga «una sola richiesta a pagamento…» restava visibile. L'utente
    vedeva una chiamata pagata dall'aria riuscita.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    _, _, logs = run(_two_accounting_errors(items), items)
    line = _failed_partial_line(logs)
    assert 'component M1' in line and 'component M2' in line
    assert '2 problemi' in line and 'proposte irrisolte conservate' in line
    assert len(_semicolon_clauses(line)) == 1, line
    assert line.startswith('Inventario globale FAILED/PARTIAL: component M1'), \
        'gli errori restano subito dopo il marcatore, dove i parser li cercano'


def test_the_partial_failure_warning_has_the_same_shape_with_one_error():
    """@brief Negativo/controprova: con un solo errore la forma non cambia.

    E' la coppia del test precedente: la visibilita' dell'avviso non deve
    dipendere da quanti errori ci sono, e il numero va detto al singolare
    quando e' uno.
    """
    data = response()
    data['components'] = [body('visible', [1], bbox=[.6, .6, .8, .8],
                               presence_confidence='high', class_confidence='low')]
    data['candidate_decisions'] = [dict(candidate_id=1, status='keep',
                                        component_id='visible', reason='associazione errata')]
    _, _, logs = run(data, [candidate()])
    line = _failed_partial_line(logs)
    assert '1 problema' in line and '1 problemi' not in line
    assert len(_semicolon_clauses(line)) == 1, line


def test_the_retry_failure_line_survives_an_api_error_full_of_semicolons(tmp_path):
    """@brief Positivo: l'errore dell'API non decide se la riga si vede.

    E' la riga che dice che una SECONDA chiamata a pagamento e' stata spesa e
    non ha prodotto nulla, mentre quella che annuncia la spesa resta. Gli
    errori upstream contengono di routine punti e virgola («HTTP 502; upstream
    timeout; retry later»): interpolati tali e quali portavano la riga a
    quattro clausole e la facevano scomparire. Il motivo resta leggibile per
    intero: cambia la punteggiatura, non il contenuto.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    complete = [dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='medium'),
                dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='medium')]
    first = _two_row_response(_without(('presence_confidence', 'class_confidence'), *complete))
    result, calls, logs = run_failing_second(first, items, 'HTTP 502; upstream timeout; retry later')
    assert len(calls) == 2, 'la seconda chiamata e’ stata tentata e pagata'
    assert 'a pagamento' in _retry_charge_line(logs)
    line = _retry_outcome_line(logs)
    assert 'fallito' in line and 'si tiene la prima risposta' in line
    for fragment in ('HTTP 502', 'upstream timeout', 'retry later'):
        assert fragment in line, line
    assert ';' not in line, line
    assert len(_semicolon_clauses(line)) == 1, line
    assert [r['presence_confidence'] for r in result
            if r.get('component_id') in ('R1', 'R2')] == ['medium', 'medium']


def test_the_retry_failure_line_is_not_a_per_item_report_for_the_modal(tmp_path):
    """@brief Le due righe riformate non sono rendiconti per-elemento.

    Controllo di integrazione contro il filtro vero della GUI, cosi' il
    contratto non resta un'asserzione sulla punteggiatura: entrambe le righe
    devono arrivare in modale con un errore dell'API pieno di punti e virgola
    e con due errori di contabilita'.
    """
    per_item_detail = pytest.importorskip('kicadgen.gui.main_window').per_item_detail
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    complete = [dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='medium'),
                dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='medium')]
    first = _two_row_response(_without(('presence_confidence', 'class_confidence'), *complete))
    _, _, logs = run_failing_second(first, items, 'HTTP 502; upstream timeout; retry later')
    assert not per_item_detail(_retry_outcome_line(logs))
    assert not per_item_detail(_failed_partial_line(logs))
    _, _, logs = run(_two_accounting_errors(items), items)
    assert not per_item_detail(_failed_partial_line(logs))


# ── Riga di avanzamento della richiesta a pagamento ──────────────────


@pytest.mark.parametrize('images,proposals,singular_image,singular_proposal', [
    (1, 1, True, True),
    (5, 2, False, False),
    (1, 3, True, False),
    (4, 1, False, True),
])
def test_the_paid_request_line_agrees_in_number(images, proposals,
                                                singular_image, singular_proposal):
    """@brief Positivo e negativo: singolare e plurale concordano su entrambi i conteggi.

    Il docstring della riga promette l'accordo «anche con una sola immagine o
    una sola proposta», e i rami singolari non erano coperti da nessun test:
    quello dell'immagine non e' nemmeno raggiungibile dalla richiesta di oggi,
    che parte sempre con la foto piena e tre strisce di contesto. Si verifica
    qui la funzione che compone la riga, cosi' l'accordo e' provato e non
    soltanto dichiarato.
    """
    line = inventory.paid_request_message(images, proposals, enumerating=False)
    assert ('1 immagine' in line) is singular_image
    assert (f'{images} immagini' in line) is not singular_image
    assert ('riconcilia l’unica proposta' in line) is singular_proposal
    assert (f'riconcilia le {proposals} proposte' in line) is not singular_proposal
    assert 'una sola richiesta a pagamento' in line


def test_the_paid_request_line_names_no_proposals_when_none_were_detected():
    """@brief Negativo: senza rilevatore locale il numero di proposte non si nomina.

    Nel percorso di sola visione non esistono proposte, quindi nessuna forma
    singolare o plurale e' corretta: la riga dice che i corpi li enumera il
    modello.
    """
    line = inventory.paid_request_message(4, 0, enumerating=True)
    # Testo rifatto a frasi intere il 2026-09-24 (utente): «Nessun rilevatore
    # locale: è il modello di visione a elencare i corpi.» Il contratto e' lo
    # stesso -- dichiarare che il rilevatore non c'e' e chi elenca i corpi --
    # cambiano solo maiuscola e verbo.
    assert 'nessun rilevatore locale' in line.lower() and 'elencare i corpi' in line
    assert 'proposte' not in line and 'riconcilia' not in line
    assert 'una sola richiesta a pagamento' in line


def test_a_single_proposal_is_reconciled_in_the_singular_end_to_end():
    """@brief Il ramo singolare vero: una sola proposta, dall'inventario completo.

    Non basta provare la funzione: la riga deve leggersi al singolare anche
    nella corsa reale, dove il numero di proposte lo conta il codice.
    """
    data = dict(components=[body('R1', [1], **{'class': 'resistor'},
                                 grouping_basis='single_body', bbox=[.2, .2, .4, .4],
                                 presence_confidence='high', class_confidence='medium')],
                candidate_decisions=[dict(candidate_id=1, status='merge', component_id='R1',
                                          reason='corpo unico')],
                uncertainties=[])
    result, calls, logs = run(data, [candidate(1)])
    assert len(calls) == 1 and len(result) == 1
    line = _paid_request_line(logs)
    assert 'riconcilia l’unica proposta' in line
    assert '1 proposte' not in line and 'le 1 proposte' not in line


def _paid_request_line(logs):
    """@brief La riga di avanzamento che annuncia la richiesta a pagamento."""
    lines = [line for line in logs if 'una sola richiesta a pagamento' in line]
    assert len(lines) == 1, f'una sola riga deve annunciare la chiamata: {lines}'
    return lines[0]


def test_the_paid_request_line_says_what_is_done_and_never_repeats_the_model():
    """@brief Positivo: la riga dice COSA si fa e che si paga, senza l'id del modello.

    Contratto nuovo. Prima la riga era in inglese in un'interfaccia italiana e
    ripeteva l'identificativo del modello, che la modale mostra gia' nella sua
    intestazione: la ripetizione era rumore e non diceva nulla sul lavoro in
    corso. Resta asserito -- e non allentato -- il punto che conta davvero nel
    log: che quella riga dichiari una chiamata A PAGAMENTO, oltre a quante
    proposte riconcilia e su quante immagini.
    """
    _, calls, logs = run(response(), [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])])
    line = _paid_request_line(logs)
    assert 'unchanged/model' not in line, 'il modello e’ gia’ nell’intestazione della modale'
    assert 'riconcilia le 2 proposte' in line and 'rilevatore locale' in line
    images = len([b for b in calls[0]['content_blocks'] if b['type'] == 'image_url'])
    assert f'{images} immagini' in line
    assert 'nessun rilevatore locale' not in line, 'il rilevatore locale ha girato'


def test_the_enumeration_line_declares_that_no_local_detector_ran():
    """@brief Negativo del precedente: senza proposte non si nominano proposte.

    Nel percorso di sola visione non c'e' nessun rilevamento locale, quindi la
    riga non deve parlare di proposte da riconciliare: dichiara che i corpi li
    enumera il modello. Resta una sola chiamata, e resta detto che si paga.
    """
    data = dict(components=[body('B1', [])], candidate_decisions=[],
                uncertainties=[])
    result, calls, logs = run(data, [])
    assert len(calls) == 1 and len(result) == 1
    line = _paid_request_line(logs)
    assert 'unchanged/model' not in line
    # Frase intera dal 2026-09-24 (vedi il test della funzione): stesso
    # contratto, maiuscola e verbo diversi.
    assert 'nessun rilevatore locale' in line.lower() and 'elencare i corpi' in line
    assert 'proposte' not in line and 'riconcilia' not in line


def test_the_cache_hit_line_states_that_nothing_is_charged(tmp_path):
    """@brief Il replay non e' una chiamata: la riga lo dice, in italiano.

    La riga conserva il termine `validated cache hit`, che altri test usano
    come contratto sul fatto che la risposta ripresa era stata validata; la
    parte leggibile dice all'utente cio' che gli serve, cioe' che non si paga.
    """
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
    data = _two_row_response([_clean_row('R1', 1, [.2, .2, .4, .4]),
                              _clean_row('R2', 2, [.6, .6, .7, .7])])
    assert run(data, items, cache=tmp_path)[1], 'la prima corsa deve pagare la chiamata'
    _, calls, logs = run(data, items, cache=tmp_path)
    assert calls == []
    (line,) = [l for l in logs if 'validated cache hit' in l]
    assert 'nessuna nuova chiamata a pagamento' in line
    assert not any('una sola richiesta a pagamento' in l for l in logs)
