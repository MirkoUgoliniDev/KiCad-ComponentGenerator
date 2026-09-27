"""@brief Contratto del pacchetto di revisione e del percorso di `_yolo_manual_box` fino al componente.

Due cose si verificano qui, entrambe offline. La prima: cio' che i caricatori
del pacchetto (`load_review_bundle`, `load_saved_detector_proposals`)
accettano deve essere esattamente cio' che la riconciliazione accetta, riga
per riga -- altrimenti una riga sbagliata su disco faceva saltare l'intero
rilancio. La seconda: la mano del revisore (`_yolo_manual_box`) deve arrivare
dal candidato inviato al componente finale, attraverso l'inventario globale e
l'estrattore, e il modello deve essere avvertito di cosa significa.
"""
import json
from copy import deepcopy

import numpy as np
import pytest

from kicadgen.services import photo_inventory as inv
from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services.datasheet_extractor import _components_3d
from kicadgen.services.photo_global_inventory import review_global_inventory
from tests.test_reopen_detector_review import (_answer_call as _fake_call, _photo_png, _proposal,
                                               _save_evidence)

LENGTH, WIDTH = 40.0, 20.0


# ── Caricatori: stesse regole del consumatore ─────────────────────────────

def _bundle_with_rows(tmp_path, rows):
    """@brief Scrive un pacchetto valido e poi ne sostituisce le righe, come farebbe un editor."""
    inv.save_review_bundle(tmp_path, [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    path = tmp_path / 'photo-review' / 'proposals.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    document['proposals'] = rows
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


@pytest.mark.parametrize('spoil', ['inverted_x', 'inverted_y', 'string_id', 'bool_id',
                                   'outside_unit', 'nan'])
def test_a_row_the_reconciliation_would_refuse_is_dropped_by_the_loader(tmp_path, spoil):
    """@brief Negativo: la riga malformata cade, la riga buona resta e il rilancio non salta.

    Ogni caso e' uno che review_global_inventory rifiuta in blocco (ID non
    intero, rettangolo rovesciato o fuori da [0, 1]): prima il caricatore lo
    accettava e reconcile_saved_proposals sollevava per l'intero insieme.
    """
    bad = _proposal(2, box=(.5, .5, .7, .7))
    if spoil == 'inverted_x':
        bad['_box'] = [.7, .5, .5, .7]
    elif spoil == 'inverted_y':
        bad['_box'] = [.5, .7, .7, .5]
    elif spoil == 'string_id':
        bad['_id'] = '2'
    elif spoil == 'bool_id':
        bad['_id'] = True
    elif spoil == 'outside_unit':
        bad['_box'] = [.5, .5, 1.2, .7]
    else:
        bad['_box'] = [.5, .5, float('nan'), .7]
    _bundle_with_rows(tmp_path, [_proposal(1), bad])
    bundle = inv.load_review_bundle(tmp_path)
    assert bundle is not None
    assert [row['_id'] for row in bundle.proposals] == [1]
    # Cio' che il caricatore restituisce, il consumatore lo accetta senza sollevare.
    calls = []
    report = inv.reconcile_saved_proposals(
        _photo_png(), bundle.proposals, LENGTH, WIDTH, 'test/model',
        _fake_call(calls), lambda _: None, tmp_path / 'cache')
    assert len(calls) == 1 and report.components


def test_duplicate_ids_keep_the_first_row_only(tmp_path):
    """@brief Negativo: due righe con lo stesso `_id` sono una sola per il consumatore."""
    first = _proposal(1)
    second = _proposal(1, box=(.5, .5, .7, .7))
    _bundle_with_rows(tmp_path, [first, second, _proposal(2, box=(.6, .6, .8, .8))])
    bundle = inv.load_review_bundle(tmp_path)
    assert [row['_id'] for row in bundle.proposals] == [1, 2]
    assert bundle.proposals[0]['_box'] == first['_box']
    # Lo stesso filtro vale in scrittura: il file non contiene mai il doppione.
    inv.save_review_bundle(tmp_path, [first, second], _photo_png(), LENGTH, WIDTH)
    document = json.loads((tmp_path / 'photo-review' / 'proposals.json').read_text(encoding='utf-8'))
    assert document['count'] == 1 and [r['_id'] for r in document['proposals']] == [1]


def test_valid_rows_at_the_unit_boundary_are_kept(tmp_path):
    """@brief Positivo: un riquadro che tocca 0 e 1 e' valido, e un `_id` int lo e' anche se grande."""
    rows = [_proposal(7, box=(0.0, 0.0, 1.0, 1.0)), _proposal(10 ** 6, box=(.1, .1, .2, .2))]
    _bundle_with_rows(tmp_path, rows)
    assert [row['_id'] for row in inv.load_review_bundle(tmp_path).proposals] == [7, 10 ** 6]


def test_a_bundle_with_a_byte_order_mark_still_loads(tmp_path):
    """@brief Windows: un BOM lasciato da Notepad non e' corruzione."""
    inv.save_review_bundle(tmp_path, [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    path = tmp_path / 'photo-review' / 'proposals.json'
    path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
    bundle = inv.load_review_bundle(tmp_path)
    assert bundle is not None and [row['_id'] for row in bundle.proposals] == [1]
    # Stesso trattamento per le proposte salvate nella cache dell'estrazione.
    cache = tmp_path / 'cache'
    _save_evidence(cache, [_proposal(3)])
    detector = next(cache.glob('detector-*.json'))
    detector.write_bytes(b'\xef\xbb\xbf' + detector.read_bytes())
    saved = inv.load_saved_detector_proposals(cache)
    assert saved is not None and [row['_id'] for row in saved.proposals] == [3]


def test_saving_without_a_photo_keeps_the_existing_one(tmp_path):
    """@brief `photo_png=None` aggiorna le proposte e lascia la foto che c'era."""
    inv.save_review_bundle(tmp_path, [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    photo = tmp_path / 'photo-review' / 'photo.png'
    before = photo.read_bytes()
    inv.save_review_bundle(tmp_path, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))],
                           None, LENGTH, WIDTH)
    bundle = inv.load_review_bundle(tmp_path)
    assert [row['_id'] for row in bundle.proposals] == [1, 2]
    assert bundle.photo_path == photo and photo.read_bytes() == before
    document = json.loads(bundle.json_path.read_text(encoding='utf-8'))
    assert document['photo'] == 'photo.png'


def test_saving_without_a_photo_into_an_empty_folder_declares_no_photo(tmp_path):
    """@brief Negativo del precedente: senza foto ne' prima ne' ora, il pacchetto lo dice."""
    inv.save_review_bundle(tmp_path, [_proposal(1)], None, LENGTH, WIDTH)
    bundle = inv.load_review_bundle(tmp_path)
    assert bundle is not None and bundle.photo_path is None
    assert json.loads(bundle.json_path.read_text(encoding='utf-8'))['photo'] is None


def test_the_nearest_later_photo_is_paired_not_the_newest(tmp_path):
    """@brief Con due foto piu' recenti delle proposte vince la piu' vicina, cioe' quella dello stesso run."""
    cache = tmp_path / 'c'
    _save_evidence(cache, [_proposal(1)], stamp='20260101-120000', photo_stamp='20260101-120005')
    # Due run successivi che si sono fermati prima di scrivere proposte: solo
    # le foto, che sono altri ritagli.
    for later in ('20260301-090000', '20260401-090000'):
        (cache / f'inventory-{later}-photo.png').write_bytes(_photo_png())
    saved = inv.load_saved_detector_proposals(cache)
    assert saved.stamp == '20260101-120000'
    assert saved.photo_path.name == 'inventory-20260101-120005-photo.png'


# ── `_yolo_manual_box`: dal candidato al componente ──────────────────────

def test_manual_box_reaches_the_reconciled_component_and_the_prompt(tmp_path):
    """@brief Positivo: il componente assorbito da un candidato ritoccato porta `_yolo_manual_box`.

    Negativo: il componente nato da un candidato della macchina non lo
    riceve, e nessuno dei due si inventa la chiave quando nessun candidato
    la porta.
    """
    calls = []
    manual = _proposal(2, box=(.6, .5, .7, .8), _yolo_manual_box=True)
    report = inv.reconcile_saved_proposals(
        _photo_png(), [_proposal(1), manual], LENGTH, WIDTH, 'test/model',
        _fake_call(calls), lambda _: None, tmp_path)
    by_source = {tuple(c['source_candidate_ids']): c for c in report.components}
    assert by_source[(2,)]['_yolo_manual_box'] is True
    assert '_yolo_manual_box' not in by_source[(1,)]
    prompt = calls[0]['system_prompt']
    assert '_yolo_manual_box' in prompt
    assert 'placed or adjusted by a person' in prompt and 'treat presence as confirmed' in prompt
    # Il prompt di enumerazione (nessun candidato) non parla di riquadri manuali.
    assert '_yolo_manual_box' not in inventory.PROMPT_ENUMERATE


def test_a_merge_of_a_manual_and_a_machine_candidate_is_manual():
    """@brief Un assieme che assorbe anche un solo riquadro ritoccato e' stato guardato."""
    from tests.test_photo_global_inventory import candidate, response
    items = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7], _yolo_manual_box=True)]
    calls = []
    def fake(**request):
        calls.append(request)
        return deepcopy(response())
    result = review_global_inventory(np.zeros((60, 120, 3), np.uint8), items, 40, 20,
                                    'unchanged/model', fake, lambda _: None, None)
    assert len(result) == 1 and result[0]['source_candidate_ids'] == [1, 2]
    assert result[0]['_yolo_manual_box'] is True
    # Senza candidati ritoccati la chiave non compare.
    plain = review_global_inventory(np.zeros((60, 120, 3), np.uint8), [candidate(1), candidate(2)],
                                   40, 20, 'unchanged/model', fake, lambda _: None, None)
    assert '_yolo_manual_box' not in plain[0]


@pytest.mark.parametrize('manual', [True, False])
def test_the_extractor_forwards_the_manual_flag_and_does_not_invent_it(manual):
    """@brief La lista bianca dell'estrattore lascia passare `_yolo_manual_box` solo se c'e'."""
    source = dict(x_mm=0, y_mm=0, length_mm=1.6, width_mm=.8, height_mm=.4,
                  _global_inventory_status='validated_hypotheses')
    if manual:
        source['_yolo_manual_box'] = True
    # The manual flag is set by the review dialog: trusted (program) path.
    component = _components_3d([source], trusted=True)[0]
    if manual:
        assert component['_yolo_manual_box'] is True
    else:
        assert '_yolo_manual_box' not in component
