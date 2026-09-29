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
from kicadgen.services import photo_grounding, photo_inventory, photo_terminals, photo_yolo


def _proposal(identity):
    return dict(_id=identity, _box=[.2, .2, .4, .4], name='detector hypothesis',
                kind='generic', x_mm=-8, y_mm=-4, length_mm=8, width_mm=4,
                height_mm=1, rotation_deg=0, color_hex='#303030')


def _body(identity='whole-assembly', sources=None, **changes):
    """@brief Riga di risposta completa rispetto allo schema `inventory_tool()`.

    Ogni campo `required` dello schema e' presente: una risposta che ne omette
    uno su TUTTE le righe fa ripetere la chiamata a pagamento, e i test di
    questo file contano le richieste. I valori dei campi aggiunti replicano i
    ripieghi che il codice applicava quando mancavano (presenza e classe pari
    alla confidenza complessiva, gruppo `assembly` come un rf_module senza
    dichiarazione), cosi' le attese dei test non cambiano.
    """
    row = dict(component_id=identity, grouping_basis='assembly',
               source_candidate_ids=[1, 2] if sources is None else sources,
               **{'class': 'rf_module'}, bbox=[.1, .1, .8, .8], bbox_basis='body',
               rotation_deg=25, package='', marking='?', terminal_count=4,
               terminal_layout='three leads left, one tab right',
               lead_count=None, lead_count_complete=False, lead_count_confidence='low',
               height_mm=1.8, height_source='visual_estimate', confidence='medium',
               evidence='One substrate with attached shield; estimated height only',
               presence_confidence='medium', class_confidence='medium',
               asymmetry_landmark=None)
    row.update(changes)
    return row


def test_the_fixture_row_honours_every_required_field_of_the_schema():
    """@brief Guardia sul builder: se lo schema cresce, il test lo dice subito.

    Senza questa guardia un campo nuovo farebbe scattare il retry in tutti i
    test del file, e il sintomo sarebbe «tre richieste invece di due», lontano
    dalla causa.
    """
    assert global_inventory.omitted_required_fields(_inventory_response()) == []


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


def _uninformative_crop_reading():
    """An observation (tool observe_body, HANDOFF 148) that derives «unknown» on these rows.

    An opaque two-terminal body of no recognisable shape: with a row whose
    class is neither two-terminal nor leaded (rf_module, unknown) the
    derivation is 'unknown' and changes no field of the row.

    Two such readings agree but change no field of the row, so these tests
    keep checking the global inventory's own output; the crop reading itself
    is tested in tests/test_photo_crop_reading.py.
    """
    return {'bodies_in_frame': 1, 'metal_contacts_total': 2, 'two_end_terminals_only': True,
            'body_shape': 'other', 'top_translucent': False,
            'something_visible_inside': False, 'coloured_patch_at_end': False,
            'band_at_end': False, 'printed_text': 'none', 'marking': '', 'tab': False,
            'size_family': '', 'leads_by_side': {'left': None, 'right': None,
                                                 'top': None, 'bottom': None}}


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """@brief Keep IO boundaries local while exercising the application entrypoint."""
    def forbidden(*args, **kwargs):
        pytest.fail('Legacy review, terminal inference or network must not run')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    for owner, name in ((photo_grounding, 'grounded_components'),
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
                 crop_requests=[], crop_reading=_uninformative_crop_reading(),
                 logs=[], detection=detection, setup=setup, credit=credit, global_review=global_review)

    def fake_api(**kwargs):
        name = kwargs['tool']['function']['name']
        if name == 'observe_body':
            # Contratto aggiornato (HANDOFF 144, 148): dopo l'inventario globale ogni
            # corpo tenuto si rilegge due volte dal suo ritaglio. Quelle
            # chiamate si contano a parte: `requests` resta l'elenco delle
            # chiamate sulla scheda (estrazione + UN inventario globale).
            state['crop_requests'].append(deepcopy(kwargs))
            return deepcopy(state['crop_reading'])
        state['requests'].append(deepcopy(kwargs))
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


def _tiles(request):
    """Detail tiles announced in the request (each is followed by its image).

    Contratto aggiornato (26/09): i dettagli non sono piu' tre strisce fisse
    ma la griglia in mm di photo_grounding.detail_grid, provata a parte.
    """
    return sum(1 for b in request['content_blocks']
               if b['type'] == 'text' and b['text'].startswith('Context tile '))


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
    # ...then the retained body re-read twice from its own crop (HANDOFF 144).
    assert len(pipeline['crop_requests']) == 2
    assert {r['model'] for r in pipeline['crop_requests']} == {'offline/global'}
    request = pipeline['requests'][1]
    assert request['tool']['function']['name'] == 'report_global_inventory'
    assert request['model'] == 'offline/global'
    # Foto piena, sovrapposizione numerata e i dettagli della griglia in mm.
    assert sum(b['type'] == 'image_url' for b in request['content_blocks']) == 2 + _tiles(request)
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
    # The crop reading's outcome reaches the delivered row (HANDOFF 144).
    assert row['_crop_reading'] == 'agreed' and len(row['_crop_readings']) == 2
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


def test_review_prunes_before_the_paid_call_and_a_cancel_never_pays(pipeline):
    """@brief La revisione sta prima della riconciliazione, e vale.

    Coppia positivo/negativo sulla stessa catena reale: tolta una proposta,
    quella non compare fra i candidati inviati e la chiamata parte comunque;
    annullata la revisione, la chiamata non parte affatto. Se la potatura
    avvenisse dopo l'invio -- o se l'annullo fosse degradato a nota come gli
    altri guasti dell'inventario -- uno dei due casi fallirebbe.
    """
    seen = []
    # La risposta deve restare coerente con cio' che viene inviato: se la
    # revisione toglie il candidato 2, la riconciliazione non puo' parlarne.
    pipeline['inventory'] = dict(components=[_body(sources=[1])], uncertainties=[],
                                 candidate_decisions=[dict(candidate_id=1, status='merge',
                                                           component_id='whole-assembly',
                                                           reason='Retained substrate assembly')])

    def keep_the_first(photo_png, proposals, length_mm, width_mm):
        seen.append((photo_png, [dict(p) for p in proposals], length_mm, width_mm))
        return proposals[:1]

    params = pipeline['execute'](review_proposals=keep_the_first)
    assert len(seen) == 1
    photo_png, offered, length_mm, width_mm = seen[0]
    # Chi rivede riceve la foto gia' codificata e la calibrazione con cui i
    # riquadri sono stati misurati: un corpo aggiunto a mano deve cadere sulla
    # stessa scala di quelli del rilevatore.
    assert photo_png[:8] == b'\x89PNG\r\n\x1a\n'
    assert [row['_id'] for row in offered] == [1, 2]
    assert (length_mm, width_mm) == (40, 20)
    inputs = json.loads(pipeline['requests'][1]['content_blocks'][4]['text'])
    assert [row['id'] for row in inputs['candidates']] == [1]
    assert inputs['board_size_mm'] == [40, 20]
    assert len(params.components_3d) == 1

    before = len(pipeline['requests'])
    with pytest.raises(extractor.ExtractionError, match='annullata'):
        pipeline['execute'](review_proposals=lambda *args: None)
    # L'estrazione della scheda e' gia' avvenuta; quella che non deve partire
    # e' la riconciliazione globale, cioe' la seconda richiesta.
    assert len(pipeline['requests']) == before + 1
    assert pipeline['requests'][-1]['tool']['function']['name'] != 'report_global_inventory'


def test_without_a_local_detector_the_vision_model_enumerates_the_bodies(pipeline):
    """@brief Terzo rilevatore: nessuna proposta locale, stessa richiesta e schema.

    Il percorso e' quello di produzione con un pezzo in meno: niente ONNX,
    niente revisione delle proposte (non ce ne sono), una sola chiamata. Il
    prompt cambia perche' non c'e' nulla da riconciliare, e cade la
    sovrapposizione numerata, che sarebbe una copia vuota della fotografia.
    """
    pipeline['inventory'] = dict(components=[_body(sources=[])], candidate_decisions=[],
                                 uncertainties=[])
    params = pipeline['execute'](photo_detector='vlm')
    pipeline['detection'].assert_not_called()
    pipeline['setup'].assert_not_called()
    assert len(pipeline['requests']) == 2
    request = pipeline['requests'][1]
    assert request['tool']['function']['name'] == 'report_global_inventory'
    # Una immagine in meno dell'altro percorso: niente sovrapposizione numerata,
    # solo la foto piena e i dettagli della griglia in mm.
    assert sum(b['type'] == 'image_url' for b in request['content_blocks']) == 1 + _tiles(request)
    inputs = json.loads(request['content_blocks'][2]['text'])
    assert inputs['candidates'] == [] and inputs['board_size_mm'] == [40, 20]
    assert inputs['prompt_version'] == global_inventory.ENUMERATION_PROMPT_VERSION
    system = request['system_prompt']
    assert 'NO local detector ran' in system and 'candidate_decisions MUST be an empty list' in system
    # La coda che parla di corpi, misure e orientamento e' la stessa dell'altro
    # percorso: le regole fisiche non cambiano con chi propone i rettangoli.
    assert global_inventory.PROMPT[global_inventory.PROMPT.index(
        global_inventory._COMMON_FROM):] in system
    assert len(params.components_3d) == 1
    row = params.components_3d[0]
    assert row['component_id'] == 'whole-assembly' and row['length_mm'] == pytest.approx(28)
    assert row['_global_inventory_prompt_version'] == global_inventory.ENUMERATION_PROMPT_VERSION


def test_the_yolo_path_keeps_its_own_prompt_and_its_numbered_overlay(pipeline):
    """@brief Caso negativo del precedente: con le proposte nulla cambia."""
    pipeline['execute']()
    request = pipeline['requests'][1]
    assert sum(b['type'] == 'image_url' for b in request['content_blocks']) == 2 + _tiles(request)
    assert json.loads(request['content_blocks'][4]['text'])['prompt_version'] \
        == global_inventory.PROMPT_VERSION
    assert 'NO local detector ran' not in request['system_prompt']


def test_an_empty_enumeration_is_a_failure_and_not_a_result(pipeline):
    """@brief Qwen ha risposto in 9 secondi con gli array vuoti: non e' un esito.

    Senza candidati nulla obbliga il modello a pronunciarsi -- `components` puo'
    legittimamente essere vuoto -- e registrare quell'elenco come «0 ipotesi,
    completezza non certificata» lo farebbe passare per un risultato, mentre la
    lavorazione prosegue in silenzio con cio' che c'era prima. Deve restare
    scritto che non ha trovato nulla.
    """
    pipeline['inventory'] = dict(components=[], candidate_decisions=[], uncertainties=[])
    params = pipeline['execute'](photo_detector='vlm')
    assert 'no bodies at all' in (params.notes or '')
    assert any('no bodies at all' in message for message in pipeline['logs']) or params.notes
    # Con i candidati invece un elenco vuoto resta gestibile: le proposte del
    # rilevatore sopravvivono come ipotesi irrisolte, e non e' un fallimento.
    pipeline['inventory'] = dict(components=[], candidate_decisions=[], uncertainties=[])
    recovered = pipeline['execute']()
    assert len(recovered.components_3d) == 2
    assert all(row['_uncertain'] for row in recovered.components_3d)


# ── fix A5: zero proposals in the paid 'yolo' path ──────────────────────────

def test_a_detector_that_found_nothing_is_not_described_as_absent(pipeline):
    """@brief Il rilevatore HA girato e non ha trovato nulla: lo si dice com'e'.

    Prima il percorso 'yolo' con zero proposte ricadeva sull'enumerazione con
    un prompt «NO local detector ran» e un log «Nessun rilevatore locale»:
    falso, e chi legge il log non poteva distinguere i due casi.
    """
    pipeline['detection'].return_value = []
    pipeline['inventory'] = dict(components=[_body(sources=[])], candidate_decisions=[],
                                 uncertainties=[])
    params = pipeline['execute']()
    pipeline['detection'].assert_called_once()
    assert len(pipeline['requests']) == 2
    request = pipeline['requests'][1]
    system = request['system_prompt']
    assert 'NO local detector ran' not in system
    assert 'A local detector ran on this photograph and left NO candidate body' in system
    inputs = json.loads(request['content_blocks'][2]['text'])
    assert inputs['candidates'] == []
    assert inputs['prompt_version'] == \
        global_inventory.ENUMERATION_AFTER_EMPTY_DETECTOR_PROMPT_VERSION
    logs = ' '.join(pipeline['logs'])
    assert 'non ha trovato corpi' in logs
    assert 'nessun rilevatore locale' not in logs.lower()
    assert len(params.components_3d) == 1


def test_a_review_that_removed_every_proposal_stops_without_paying(pipeline):
    """@brief Chi rivede ha tolto tutto di proposito: nessuna enumerazione a pagamento.

    Negativo strutturale del test precedente: li' il rilevatore non aveva
    trovato nulla e l'enumerazione e' legittima; qui le proposte c'erano e
    l'utente le ha rifiutate tutte, quindi rifarle pagando contraddice la sua
    scelta. La richiesta dell'inventario globale non deve partire.
    """
    before = len(pipeline['requests'])
    params = pipeline['execute'](review_proposals=lambda *args: [])
    assert len(pipeline['requests']) == before + 1  # solo l'estrazione della scheda
    assert all(request['tool']['function']['name'] != 'report_global_inventory'
               for request in pipeline['requests'])
    pipeline['global_review'].assert_not_called()
    assert any('tutte le 2 proposte sono state tolte' in message for message in pipeline['logs'])
    assert not params.components_3d


def test_the_inventory_traceback_in_the_ai_log_is_scrubbed(pipeline, monkeypatch):
    """@brief S8: the traceback line of a failed inventory hides the home folder.

    Positive: the line is still written (the fault stays diagnosable).
    Negative: the home folder of the user does not appear in it.
    """
    from pathlib import Path
    home = str(Path.home())

    def broken(*args, **kwargs):
        raise RuntimeError(f"cannot open {Path(home, 'Pictures', 'board.png')}")
    monkeypatch.setattr(photo_inventory, 'extract_inventory', broken)
    pipeline['execute']()
    lines = [line for line in pipeline['logs'] if line.startswith('AI_PHOTO_INVENTORY_TRACEBACK')]
    assert len(lines) == 1
    assert 'RuntimeError' in lines[0] and 'board.png' in lines[0]
    assert home.lower() not in lines[0].lower()


@pytest.mark.parametrize('detector', ['yolo', 'vlm'])
@pytest.mark.parametrize('given,expected', [
    ('Profile crop instructions', 'Profile crop instructions'),
    (None, 'factory'),
    ('   ', 'factory'),
])
def test_extractor_sends_the_profile_crop_prompt_to_every_crop_reading(pipeline, given, expected,
                                                                       detector):
    """@brief The profile field crop_prompt reaches read_bodies; none or blank sends the factory text.

    Both detectors that go through the global inventory ('yolo' and 'vlm')
    run the crop reading. The delivered row records the digest of the text
    actually sent (HANDOFF 146 V5): the whitelist of _components_3d keeps it.
    """
    from kicadgen.services.component_types import text_digest
    from kicadgen.services.photo_crop_reading import SYSTEM_PROMPT
    params = pipeline['execute'](crop_prompt=given, photo_detector=detector)
    sent = {request['system_prompt'] for request in pipeline['crop_requests']}
    assert len(pipeline['crop_requests']) == 2
    effective = SYSTEM_PROMPT if expected == 'factory' else expected
    assert sent == {effective}
    [row] = params.components_3d
    assert row['_crop_reading_prompt_digest'] == text_digest(effective)
    if detector == 'vlm':
        pipeline['detection'].assert_not_called()   # really the vision-only route


@pytest.mark.parametrize('detector', ['yolo', 'vlm'])
@pytest.mark.parametrize('given,expected', [
    ('offline/crop-reader', 'offline/crop-reader'),
    ('  offline/crop-reader  ', 'offline/crop-reader'),
    (None, 'offline/global'),
    ('   ', 'offline/global'),
])
def test_extractor_reads_the_crops_with_the_profile_crop_model_or_the_photo_model(
        pipeline, monkeypatch, given, expected, detector):
    """@brief The profile field crop_model drives the crop readings only (HANDOFF 150).

    Set, it is the model of every 'observe_body' request and is announced in
    the log; None or blank, the crops go to the model of the paid call on the
    photograph. The global inventory request never uses it.
    """
    from kicadgen.services import providers
    monkeypatch_ready = []
    real_ready = providers.ensure_providers_ready
    monkeypatch.setattr(providers, 'ensure_providers_ready',
                        lambda models, *a, **k: (monkeypatch_ready.append(list(models)),
                                                 real_ready(models, *a, **k)))
    pipeline['execute'](crop_model=given, photo_detector=detector)
    assert len(pipeline['crop_requests']) == 2
    assert {r['model'] for r in pipeline['crop_requests']} == {expected}
    assert [r['model'] for r in pipeline['requests']] == ['offline/board', 'offline/global']
    named = [line for line in pipeline['logs']
             if line.startswith('Lettura dei singoli componenti affidata al modello')]
    if expected == 'offline/global':
        assert named == []
    else:
        assert len(named) == 1 and 'offline/crop-reader' in named[0] and 'offline/global' in named[0]
    # The provider of the crop model is checked before the run, like the others.
    [checked] = monkeypatch_ready
    assert [m for m in checked if m] == ['offline/board', 'offline/localizer', 'offline/global'] + (
        ['offline/crop-reader'] if expected != 'offline/global' else [])
