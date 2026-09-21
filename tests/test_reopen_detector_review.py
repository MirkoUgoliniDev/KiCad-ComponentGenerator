"""@brief Riapertura di «Corpi rilevati» sulle proposte salvate e rilancio dell'inventario.

Il percorso normale e' documenti -> AI -> YOLO -> filtro -> misura -> revisione
-> riconciliazione a pagamento. Qui si prova la scorciatoia che riparte dalla
revisione usando cio' che l'estrazione ha salvato su disco, e che deve
ricongiungersi al percorso normale subito dopo: stessa riconciliazione, stessi
parametri, stessa partenza dell'abbinamento modelli e del 3D.
"""
import json
import os
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services import photo_inventory as inv
from kicadgen.services.datasheet_extractor import ExtractedParams
from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa

import kicadgen.gui.yolo_review_dialog as review_module

LENGTH, WIDTH = 40.0, 20.0


def _proposal(identity, box=(.2, .2, .4, .4), **extra):
    """@brief Una proposta come `detector-*.json` la conserva: `_box` e geometria in mm."""
    x0, y0, x1, y1 = box
    row = dict(_id=identity, _box=list(box), name=f'YOLO candidate {identity}', kind='generic',
               x_mm=((x0 + x1) / 2 - .5) * LENGTH, y_mm=((y0 + y1) / 2 - .5) * WIDTH,
               length_mm=(x1 - x0) * LENGTH, width_mm=(y1 - y0) * WIDTH, height_mm=1.0,
               rotation_deg=0, color_hex='#303030', confidence='low', _uncertain=True,
               _source='YOLO local', _yolo_class='resistor', _yolo_confidence=.6,
               _yolo_review_only=False)
    row.update(extra)
    return row


def _response(candidates=({'id': 1, 'bbox': [.2, .2, .4, .4]},)):
    """@brief Risposta valida dell'inventario globale: un corpo e una decisione per
    OGNI proposta inviata (una proposta senza decisione rende la risposta
    parziale, e una risposta parziale non entra in cache).
    """
    return dict(components=[dict(
        component_id=f'R{c["id"]}', source_candidate_ids=[c['id']], **{'class': 'resistor'},
        bbox=list(c['bbox']), bbox_basis='body', rotation_deg=0, package='R_0603', marking='?',
        terminal_count=2, terminal_layout='two endcaps', height_mm=None,
        height_source='unknown', confidence='high', evidence='complete body',
        presence_confidence='high', class_confidence='high', lead_count=2,
        lead_count_complete=True, lead_count_confidence='high',
        grouping_basis='single_body', asymmetry_landmark=None) for c in candidates],
        candidate_decisions=[dict(candidate_id=c['id'], status='keep',
                                  component_id=f'R{c["id"]}', reason='body') for c in candidates],
        uncertainties=[])


def _answer_call(calls):
    """@brief Finta chiamata: registra la richiesta e risponde per i candidati ricevuti."""
    def call(**kwargs):
        calls.append(kwargs)
        return _response(_candidates(kwargs))
    return call


def _candidates(kwargs):
    """@brief I candidati inviati al modello, dal blocco JSON della richiesta."""
    for block in kwargs['content_blocks']:
        text = block.get('text', '')
        if text.startswith('{'):
            return json.loads(text)['candidates']
    raise AssertionError('nessun blocco JSON nella richiesta')


def _photo_png():
    import cv2
    photo = np.full((100, 200, 3), 90, np.uint8)
    photo[20:40, 40:80] = (20, 200, 30)
    return cv2.imencode('.png', photo)[1].tobytes()


def _save_evidence(cache_dir, proposals, stamp='20260101-120000', photo_stamp='20260101-120005'):
    """@brief Scrive su disco cio' che extract_inventory lascia dopo un'estrazione."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f'detector-{stamp}.json').write_text(
        json.dumps(dict(components=proposals, count=len(proposals))), encoding='utf-8')
    if photo_stamp:
        (cache_dir / f'inventory-{photo_stamp}-photo.png').write_bytes(_photo_png())


def _params():
    return ExtractedParams(
        name='', description='', tags='', confidence='medium', notes='',
        layout='castellated', body_length_mm=LENGTH, body_width_mm=WIDTH,
        castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54, first_pin=1)],
        components_3d=[dict(name='old', kind='generic', x_mm=0, y_mm=0, length_mm=2,
                            width_mm=1, height_mm=1, rotation_deg=0)],
        photo_png=_photo_png(), photo_source='top.png')


@pytest.fixture
def prepared(window, monkeypatch, tmp_path):  # noqa: F811 - fixture importata
    """@brief Finestra con cache monkeypatchata, senza rete e senza modali bloccanti."""
    cache = tmp_path / 'photo-cache'
    monkeypatch.setattr(window, '_photo_cache_dir', lambda: cache)
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[2]))
    started = []
    monkeypatch.setattr(window, '_start_automatic_generation', lambda params: started.append(params))
    calls = []
    import kicadgen.services.openrouter as openrouter
    monkeypatch.setattr(openrouter, 'call_tool', _answer_call(calls))
    return window, cache, calls, started, warnings


def _answer(monkeypatch, outcome, kept):
    """@brief La vista risponde senza aprirsi: cio' che si prova sta fuori di essa."""
    monkeypatch.setattr(review_module.YoloReviewDialog, 'exec', lambda self: outcome)
    monkeypatch.setattr(review_module.YoloReviewDialog, 'kept', lambda self: list(kept))


def _run_worker(window):
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    worker = window._reconcile_worker
    assert worker is not None, 'nessun worker avviato'
    assert worker.wait(20000)
    for _ in range(20):
        app.processEvents()
    return worker


# ── Servizio: proposte salvate ────────────────────────────────────────────

def test_saved_proposals_pair_with_the_photo_of_the_same_run(tmp_path):
    cache = tmp_path / 'c'
    _save_evidence(cache, [_proposal(1)], stamp='20260101-120000', photo_stamp='20260101-120005')
    # Un run precedente, con la sua foto: non deve vincere sul piu' recente.
    _save_evidence(cache, [_proposal(9)], stamp='20250101-000000', photo_stamp='20250101-000005')
    saved = inv.load_saved_detector_proposals(cache)
    assert saved.stamp == '20260101-120000'
    assert [row['_id'] for row in saved.proposals] == [1]
    assert saved.photo_path.name == 'inventory-20260101-120005-photo.png'


def test_a_photo_older_than_the_proposals_is_not_paired_with_them(tmp_path):
    """@brief Caso negativo: una foto di un run precedente e' un altro ritaglio."""
    cache = tmp_path / 'c'
    _save_evidence(cache, [_proposal(1)], stamp='20260101-120000', photo_stamp='20250101-000005')
    saved = inv.load_saved_detector_proposals(cache)
    assert saved is not None and saved.photo_path is None
    assert inv.load_saved_detector_proposals(tmp_path / 'missing') is None


def test_reconcile_forwards_hand_drawn_boxes_untouched_and_pays_once(tmp_path):
    """@brief Le proposte tenute vanno alla riconciliazione come sono, una richiesta sola."""
    calls = []
    manual = _proposal(2, box=(.6, .5, .7, .8), _yolo_manual_box=True)
    report = inv.reconcile_saved_proposals(
        _photo_png(), [_proposal(1), manual], LENGTH, WIDTH, 'test/model',
        _answer_call(calls), lambda _: None, tmp_path)
    assert len(calls) == 1
    sent = _candidates(calls[0])
    assert [c['id'] for c in sent] == [1, 2]
    assert sent[1]['bbox'] == pytest.approx([.6, .5, .7, .8])
    assert sent[1]['detector_metadata']['_yolo_manual_box'] is True
    assert report.components and report.components[0]['package'] == 'R_0603'
    assert report.calibration['reconciled_from_saved'] and report.calibration['manual_proposals'] == 1
    assert report.calibration['replayed_from_cache'] is False
    assert list(tmp_path.glob('inventory-*-photo.png')) and list(tmp_path.glob('inventory-*.json'))
    # Stesse proposte, stessa foto: la seconda volta non si paga.
    seen = []
    again = inv.reconcile_saved_proposals(
        _photo_png(), [_proposal(1), manual], LENGTH, WIDTH, 'test/model',
        lambda **kwargs: pytest.fail('richiesta ripagata'), seen.append, tmp_path)
    assert again.calibration['replayed_from_cache'] is True
    assert any('validated cache hit' in m for m in seen) and any('nessun nuovo addebito' in m for m in seen)


def test_reconcile_refuses_an_unreadable_photo_before_any_call(tmp_path):
    with pytest.raises(ValueError, match='Foto calibrata'):
        inv.reconcile_saved_proposals(b'not a png', [_proposal(1)], LENGTH, WIDTH, 'm',
                                      lambda **_: pytest.fail('rete'), lambda _: None, tmp_path)


# ── Servizio: pacchetto nella cartella del componente ─────────────────────

def test_review_bundle_round_trips_proposals_extents_and_photo(tmp_path):
    """@brief Positivo: cio' che si salva si rilegge uguale, foto compresa."""
    rows = [_proposal(1), _proposal(2, box=(.5, .5, .7, .7), _yolo_manual_box=True)]
    path = inv.save_review_bundle(tmp_path, rows, _photo_png(), LENGTH, WIDTH,
                                  detector='yolo', source_json='detector-x.json')
    assert path == tmp_path / 'photo-review' / 'proposals.json'
    bundle = inv.load_review_bundle(tmp_path)
    assert bundle is not None
    assert bundle.proposals == rows
    assert (bundle.body_length_mm, bundle.body_width_mm) == (LENGTH, WIDTH)
    assert bundle.photo_path is not None and bundle.photo_path.read_bytes() == _photo_png()
    assert bundle.source_json == 'detector-x.json' and bundle.detector == 'yolo'
    assert bundle.proposals[1]['_yolo_manual_box'] is True


@pytest.mark.parametrize('spoil', ['missing', 'corrupt', 'zero_extent', 'no_proposals', 'bad_row'])
def test_review_bundle_is_absent_when_unusable(tmp_path, spoil):
    """@brief Negativo: file assente, corrotto, senza scala o senza proposte = niente da riaprire."""
    if spoil != 'missing':
        inv.save_review_bundle(tmp_path, [_proposal(1)], _photo_png(), LENGTH, WIDTH)
        path = tmp_path / 'photo-review' / 'proposals.json'
        if spoil == 'corrupt':
            path.write_text('{not json', encoding='utf-8')
        else:
            document = json.loads(path.read_text(encoding='utf-8'))
            if spoil == 'zero_extent':
                document['body_length_mm'] = 0
            elif spoil == 'no_proposals':
                document['proposals'] = []
            else:
                document['proposals'] = [dict(name='senza id ne box')]
            path.write_text(json.dumps(document), encoding='utf-8')
    assert inv.load_review_bundle(tmp_path) is None


# ── Interfaccia ───────────────────────────────────────────────────────────

def _bundle_dir(window):
    return window._project_dir() / 'photo-review'


def _retype_name(window, name='Componente_di_prova'):
    """@brief Come chi riavvia e scrive il nome: il cambio del campo ricalcola i pulsanti."""
    window.field_name.setText('')
    window.field_name.setText(name)


def test_buttons_follow_the_bundle_in_the_component_folder(prepared):
    """@brief Con il solo nome digitato e il pacchetto nella cartella, si riapre; senza nome no.

    Decisione dell'utente: «se il file json esiste nella cartella deve essere
    possibile aprire la revisione bypassando l'interrogazione AI», anche dopo
    un riavvio, quindi senza estrazione in questa sessione.
    """
    window, _cache, _calls, _started, _warnings = prepared
    assert window._last_extracted is None
    assert not window.btn_reopen_review.isEnabled()
    assert not window.btn_relaunch_inventory.isEnabled()
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    window.field_name.setText('AltroNome')
    assert not window.btn_reopen_review.isEnabled(), 'altra cartella, nessun pacchetto'
    window.field_name.setText('Componente_di_prova')
    assert window.btn_reopen_review.isEnabled()
    assert window.btn_relaunch_inventory.isEnabled()
    window.field_name.setText('')
    assert not window.btn_reopen_review.isEnabled(), 'senza nome non c\'e\' cartella'
    assert not window.btn_relaunch_inventory.isEnabled()


def test_extraction_writes_the_bundle_into_the_component_folder(prepared):
    """@brief L'estrazione copia le proposte fresche della cache nella cartella del componente."""
    window, cache, *_ = prepared
    window._on_extracted(_params())
    assert not (_bundle_dir(window) / 'proposals.json').exists(), 'nulla in cache: nulla da salvare'
    assert not window.btn_reopen_review.isEnabled()
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    bundle = inv.load_review_bundle(window._project_dir())
    assert bundle is not None and [r['_id'] for r in bundle.proposals] == [1, 2]
    assert (bundle.body_length_mm, bundle.body_width_mm) == (LENGTH, WIDTH)
    assert bundle.photo_path is not None
    assert window.btn_reopen_review.isEnabled()
    assert 'senza chiamate AI' in window.ai_progress_text.toPlainText()


def test_proposals_older_than_the_current_extraction_are_ignored(prepared):
    """@brief La cache e' comune ai componenti: un file di un'altra estrazione non vale."""
    window, cache, *_ = prepared
    _save_evidence(cache, [_proposal(1)], stamp='20200101-000000', photo_stamp='20200101-000001')
    window._extraction_started_stamp = '20260101-000000'
    window._on_extracted(_params())
    assert not window.btn_reopen_review.isEnabled()
    assert not (_bundle_dir(window) / 'proposals.json').exists()


def test_accepting_saves_offline_and_pays_nothing(prepared, monkeypatch):
    """@brief Decisione dell'utente: l'OK salva e basta; la spesa e' un altro pulsante.

    Sostituisce il contratto precedente (OK = riconciliazione a pagamento).
    """
    window, _cache, calls, started, warnings = prepared
    inv.save_review_bundle(window._project_dir(),
                           [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))],
                           _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    kept = _proposal(1, box=(.21, .2, .41, .4), _yolo_manual_box=True)
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [kept])
    window.btn_reopen_review.click()
    assert window._reconcile_worker is None, 'nessun worker: la revisione e\' gratuita'
    assert calls == [] and started == [] and warnings == []
    bundle = inv.load_review_bundle(window._project_dir())
    assert bundle.proposals == [kept]
    assert (bundle.body_length_mm, bundle.body_width_mm) == (LENGTH, WIDTH)
    log = window.ai_progress_text.toPlainText()
    assert 'nessuna richiesta AI' in log and 'gratuito' in log
    assert window.btn_reopen_review.isEnabled() and window.btn_relaunch_inventory.isEnabled()


def test_cancelling_starts_nothing_and_leaves_the_file(prepared, monkeypatch):
    """@brief Negativo: Annulla non avvia nulla e non tocca il pacchetto."""
    window, _cache, calls, started, _warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    before = (_bundle_dir(window) / 'proposals.json').read_bytes()
    _answer(monkeypatch, QDialog.DialogCode.Rejected, [])
    window.btn_reopen_review.click()
    assert window._reconcile_worker is None
    assert calls == [] and started == []
    assert 'annullata' in window.ai_progress_text.toPlainText()
    assert (_bundle_dir(window) / 'proposals.json').read_bytes() == before
    assert window.btn_reopen_review.isEnabled()


def test_relaunch_pays_once_for_the_saved_proposals_and_restarts_matching(prepared, monkeypatch):
    """@brief Il pulsante a pagamento ripaga una sola volta, sulle proposte del pacchetto."""
    window, _cache, calls, started, warnings = prepared
    _save_evidence(_cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [_proposal(1)])
    window.btn_reopen_review.click()
    assert calls == [], 'la revisione non paga'
    window.btn_relaunch_inventory.click()
    log_before = window.ai_progress_text.toPlainText()
    assert 'Rilancio dell\'inventario su 1 proposte' in log_before
    assert 'una richiesta a pagamento al modello' in log_before
    assert 'una richiesta a pagamento' in window.analysis_overlay.status.text()
    _run_worker(window)
    assert warnings == []
    assert len(calls) == 1
    assert [c['id'] for c in _candidates(calls[0])] == [1]
    assert calls[0]['model'] == window._photo_detector_model()
    assert [c.name for c in window.castellated_components] == ['R1 (resistor)']
    assert window._last_extracted.components_3d[0]['package'] == 'R_0603'
    assert 'Inventario rilanciato: 1 ingombri' in window._last_extracted.notes
    assert len(started) == 1 and started[0].components_3d[0]['package'] == 'R_0603'
    assert window.btn_extract_ai.isEnabled()
    assert window.btn_reopen_review.isEnabled()
    # Il pacchetto conserva le proposte tenute, non quelle originali della cache.
    assert [r['_id'] for r in inv.load_review_bundle(window._project_dir()).proposals] == [1]


def test_relaunch_after_restart_updates_components_without_auto_generation(prepared, monkeypatch):
    """@brief Senza estrazione in sessione il rilancio aggiorna i componenti e non avvia footprint/3D."""
    window, _cache, calls, started, warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    assert window._last_extracted is None
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert warnings == [] and len(calls) == 1
    assert [c.name for c in window.castellated_components] == ['R1 (resistor)']
    assert started == [], 'base sintetizzata dai campi: niente generazione automatica'
    assert 'senza avviare la generazione automatica' in window.ai_progress_text.toPlainText()


def test_identical_proposals_replay_the_cache_without_a_new_call(prepared, monkeypatch):
    window, cache, calls, started, warnings = prepared
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [_proposal(1)])
    window.btn_reopen_review.click()
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert len(calls) == 1
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert warnings == []
    assert len(calls) == 1, 'proposte identiche: la richiesta non va ripagata'
    log = window.ai_progress_text.toPlainText()
    assert 'validated cache hit' in log and 'nessun nuovo addebito' in log
    assert len(started) == 2


def _fake_extract_worker(delivered, detector='yolo'):
    """@brief Il worker fermo in attesa della revisione: registra la risposta consegnata."""
    from types import SimpleNamespace
    return SimpleNamespace(deliver_review=delivered.append, photo_detector=detector,
                           isRunning=lambda: False)


def test_in_extraction_review_writes_the_kept_proposals_not_the_cache_rows(prepared, monkeypatch):
    """@brief Positivo: il pacchetto contiene cio' che la revisione ha tenuto, box corretto compreso.

    Il `detector-*.json` in cache nasce PRIMA della revisione: copiarlo dopo
    l'estrazione riporterebbe il falso positivo cancellato e perderebbe il
    riquadro corretto. Ne' `_on_extracted` puo' poi sovrascriverlo.
    """
    window, cache, calls, _started, _warnings = prepared
    window._extraction_started_stamp = '20260101-000000'
    window._reviewed_this_extraction = False
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    edited = _proposal(1, box=(.21, .2, .41, .4))
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [edited])
    delivered = []
    monkeypatch.setattr(window, '_extract_worker', _fake_extract_worker(delivered, 'vlm'))
    window._on_review_requested(_photo_png(), [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))],
                                LENGTH, WIDTH)
    assert delivered == [[edited]], 'il worker riceve le proposte tenute'
    assert window._reviewed_this_extraction is True
    bundle = inv.load_review_bundle(window._project_dir())
    assert bundle is not None and bundle.proposals == [edited]
    assert bundle.detector == 'vlm'
    assert (bundle.body_length_mm, bundle.body_width_mm) == (LENGTH, WIDTH)
    assert bundle.photo_path is not None and bundle.photo_path.read_bytes() == _photo_png()
    assert 'tenute dalla revisione salvate' in window.ai_progress_text.toPlainText()
    # La fine dell'estrazione non deve riportare le righe grezze della cache.
    window._on_extracted(_params())
    assert inv.load_review_bundle(window._project_dir()).proposals == [edited]
    assert calls == []
    assert window.btn_reopen_review.isEnabled()


def test_extraction_without_review_dialog_still_copies_the_cache_rows(prepared):
    """@brief Negativo: senza revisione in questa estrazione il ripiego dalla cache resta attivo."""
    window, cache, *_ = prepared
    window._extraction_started_stamp = '20260101-000000'
    window._reviewed_this_extraction = False
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    assert [r['_id'] for r in inv.load_review_bundle(window._project_dir()).proposals] == [1, 2]


def test_review_that_keeps_nothing_marks_the_extraction_reviewed_and_copies_nothing(prepared, monkeypatch):
    """@brief Tenere zero proposte non scrive il pacchetto e non lascia copiare la cache."""
    window, cache, *_ = prepared
    window._extraction_started_stamp = '20260101-000000'
    window._reviewed_this_extraction = False
    _save_evidence(cache, [_proposal(1)])
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [])
    delivered = []
    monkeypatch.setattr(window, '_extract_worker', _fake_extract_worker(delivered))
    window._on_review_requested(_photo_png(), [_proposal(1)], LENGTH, WIDTH)
    assert delivered == [[]]
    assert window._reviewed_this_extraction is True
    assert not (_bundle_dir(window) / 'proposals.json').exists()
    assert 'nessuna proposta tenuta' in window.ai_progress_text.toPlainText()
    window._on_extracted(_params())
    assert not (_bundle_dir(window) / 'proposals.json').exists(), 'la cache grezza non va copiata'


def test_cancelled_in_extraction_review_writes_nothing(prepared, monkeypatch):
    """@brief Negativo: Annulla consegna None al worker e non tocca la cartella del componente."""
    window, cache, *_ = prepared
    window._reviewed_this_extraction = False
    _answer(monkeypatch, QDialog.DialogCode.Rejected, [])
    delivered = []
    monkeypatch.setattr(window, '_extract_worker', _fake_extract_worker(delivered))
    window._on_review_requested(_photo_png(), [_proposal(1)], LENGTH, WIDTH)
    assert delivered == [None]
    assert window._reviewed_this_extraction is False
    assert not (_bundle_dir(window) / 'proposals.json').exists()


def test_new_extraction_resets_the_reviewed_and_synthesized_flags(prepared, monkeypatch):
    """@brief I flag valgono per una sola analisi: la successiva riparte pulita."""
    window, *_ = prepared
    window._reviewed_this_extraction = True
    window._relaunch_base_synthesized = True

    class _NeverStarts:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('worker di prova')

    monkeypatch.setattr(gui, 'ExtractWorker', _NeverStarts)
    profile = dict(model='m', grounding_prompt='', verification_prompt='',
                   terminal_prompt='', system_prompt='')
    with pytest.raises(RuntimeError, match='worker di prova'):
        window._start_extract_worker([], None, profile, 'vlm', None)
    assert window._reviewed_this_extraction is False
    assert window._relaunch_base_synthesized is False
    assert window._extraction_started_stamp is not None


def test_relaunch_after_restart_keeps_a_matrix_component_in_matrix_layout(prepared):
    """@brief Positivo: la base sintetizzata segue la disposizione dei campi, non un castellated fisso.

    Negativo implicito: con il gruppo castellated spento, `_apply_extracted_result`
    riempie righe/colonne dagli spin box e il gruppo resta spento.
    """
    window, _cache, calls, started, warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    window.group_castellated.setChecked(False)
    window.field_rows.setValue(3)
    window.field_cols.setValue(5)
    window.field_pitch.setValue(1.27)
    bundle = inv.load_review_bundle(window._project_dir())
    base = window._relaunch_base_params(bundle)
    assert base.layout == 'matrix' and (base.rows, base.cols, base.pitch_mm) == (3, 5, 1.27)
    assert base.row_spacing_mm == window.field_row_spacing.value()
    assert base.pad_diameter_mm == window.field_pad.value()
    assert base.drill_mm == window.field_drill.value()
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert warnings == [] and len(calls) == 1
    assert not window.group_castellated.isChecked()
    assert (window.field_rows.value(), window.field_cols.value()) == (3, 5)
    assert [c.name for c in window.castellated_components] == ['R1 (resistor)']
    assert started == []
    # Positivo speculare: con il gruppo acceso la base resta castellated.
    window.group_castellated.setChecked(True)
    window._last_extracted = None
    assert window._relaunch_base_params(bundle).layout == 'castellated'


def test_second_relaunch_after_a_failed_one_still_does_not_auto_generate(prepared, monkeypatch):
    """@brief La base sintetizzata resta tale: un secondo rilancio non genera da solo.

    Dopo il primo rilancio `_last_extracted` non e' piu' None (porta la base
    sintetizzata), quindi il criterio non puo' essere quello.
    """
    window, _cache, calls, started, warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    import kicadgen.services.openrouter as openrouter

    def failing(**kwargs):
        raise RuntimeError('rete assente')

    monkeypatch.setattr(openrouter, 'call_tool', failing)
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert len(warnings) == 1 and 'rete assente' in warnings[0]
    assert window._last_extracted is not None and window._relaunch_base_synthesized is True
    assert started == []
    monkeypatch.setattr(openrouter, 'call_tool', _answer_call(calls))
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert len(calls) == 1
    assert started == [], 'seconda base sintetizzata: ancora nessuna generazione automatica'
    assert 'senza avviare la generazione automatica' in window.ai_progress_text.toPlainText()


def test_relaunch_after_a_real_extraction_auto_generates(prepared, monkeypatch):
    """@brief Negativo del precedente: con un'estrazione vera la generazione automatica parte."""
    window, cache, calls, started, warnings = prepared
    _save_evidence(cache, [_proposal(1)])
    window._relaunch_base_synthesized = False
    window._on_extracted(_params())
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    assert warnings == [] and len(calls) == 1
    assert len(started) == 1


def test_synthesized_relaunch_closes_with_an_inventory_message_not_an_extraction_one(prepared, monkeypatch):
    """@brief La modale finale dice cosa e' successo: un rilancio, non un'estrazione «a confidenza low»."""
    window, _cache, _calls, _started, _warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: shown.append((args[1], args[2])))
    window._auto_after_extraction = False
    monkeypatch.setattr(window, '_advanced_user', lambda: True)
    window.btn_relaunch_inventory.click()
    _run_worker(window)
    titles = [title for title, _text in shown]
    assert 'Inventario rilanciato' in titles
    assert 'Estrazione completata' not in titles
    assert not any('Confidenza' in text for _title, text in shown)


def test_extraction_setup_failure_restores_the_idle_ui(prepared, monkeypatch):
    """@brief Un'eccezione fra _begin_ai_run e worker.start() non lascia i tab spenti."""
    window, _cache, _calls, _started, warnings = prepared
    monkeypatch.setattr(window, '_selected_documents', lambda: ([], None))
    monkeypatch.setattr(window, '_photo_detector_options', lambda: ('vlm', None))

    class _Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('costruttore rotto')

    monkeypatch.setattr(gui, 'ExtractWorker', _Broken)
    window._extract_with_ai()
    assert window.tabs.isEnabled()
    assert window.btn_extract_ai.isEnabled()
    assert len(warnings) == 1 and 'costruttore rotto' in warnings[0]


def test_relaunch_setup_failure_restores_the_idle_ui(prepared, monkeypatch):
    """@brief Stessa garanzia per il rilancio: il worker che non nasce non blocca la finestra."""
    window, _cache, _calls, _started, warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)

    class _Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('costruttore rotto')

    monkeypatch.setattr(gui, 'ReconcileWorker', _Broken)
    window.btn_relaunch_inventory.click()
    assert window.tabs.isEnabled()
    assert window.btn_extract_ai.isEnabled()
    assert window.btn_relaunch_inventory.isEnabled(), 'il pacchetto e\' ancora valido: si puo\' riprovare'
    assert len(warnings) == 1 and 'costruttore rotto' in warnings[0]


def test_name_change_during_a_run_keeps_the_analysis_buttons_disabled(prepared, monkeypatch):
    """@brief Cambiare nome mentre un worker gira non riaccende estrazione, revisione e rilancio."""
    from types import SimpleNamespace
    window, *_ = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    _retype_name(window)
    assert window.btn_extract_ai.isEnabled() and window.btn_reopen_review.isEnabled()
    window.btn_extract_ai.setEnabled(False)
    window.btn_reopen_review.setEnabled(False)
    window.btn_relaunch_inventory.setEnabled(False)
    window._extract_worker = SimpleNamespace(isRunning=lambda: True)
    try:
        window.field_name.setText('Componente_di_prova_2')
        window.field_name.setText('Componente_di_prova')
        assert not window.btn_extract_ai.isEnabled()
        assert not window.btn_reopen_review.isEnabled()
        assert not window.btn_relaunch_inventory.isEnabled()
    finally:
        window._extract_worker = None
    # Negativo: a worker fermo il cambio nome riabilita come prima.
    window.field_name.setText('Componente_di_prova_2')
    window.field_name.setText('Componente_di_prova')
    assert window.btn_extract_ai.isEnabled() and window.btn_reopen_review.isEnabled()


def test_bundle_without_photo_disables_both_buttons_and_warns_neutrally(prepared):
    """@brief Senza foto calibrata (ne' su disco ne' in memoria) i pulsanti restano spenti."""
    window, _cache, _calls, _started, warnings = prepared
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    (_bundle_dir(window) / 'photo.png').unlink()
    assert not list(_bundle_dir(window).glob('*.png'))
    window._photo_png = None
    _retype_name(window)
    assert not window.btn_reopen_review.isEnabled()
    assert not window.btn_relaunch_inventory.isEnabled()
    # L'avviso, se raggiunto direttamente, non parla di «rilancio».
    bundle = inv.load_review_bundle(window._project_dir())
    assert bundle.photo_path is None
    assert window._review_photo(bundle) is None
    assert len(warnings) == 1 and 'rilanci' not in warnings[0].lower()
    # Positivo: con la foto in memoria i pulsanti si riaccendono.
    window._photo_png = _photo_png()
    _retype_name(window)
    assert window.btn_reopen_review.isEnabled() and window.btn_relaunch_inventory.isEnabled()


def test_reopen_button_sits_at_the_bottom_of_the_project_tab(window):  # noqa: F811
    """@brief «Un pulsante in basso nella scheda principale»: ultimo nella scheda, non fra gli strumenti.

    Positivo: e' l'ultimo elemento del layout della scheda Progetto, sotto il
    gruppo di analisi. Negativo: non compare piu' nella riga degli strumenti
    accanto a «Rivedi foto…», dove stava prima.
    """
    from PyQt6.QtWidgets import QLayout, QScrollArea
    scroll = window.tabs.widget(0)
    tab = scroll.widget() if isinstance(scroll, QScrollArea) else scroll
    outer = tab.layout()
    last = outer.itemAt(outer.count() - 1)
    row = last.layout()
    assert isinstance(row, QLayout), "l'ultimo elemento della scheda deve essere la riga del pulsante"
    widgets = [row.itemAt(i).widget() for i in range(row.count())]
    assert window.btn_reopen_review in widgets
    assert outer.indexOf(window.group_component_analysis) < outer.count() - 1
    # Negativo: non sta piu' nella riga degli strumenti dell'analisi.
    tools_row = window.btn_photo_comparison.parentWidget().layout()

    def _contains(layout, widget):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is widget:
                return True
            if item.layout() is not None and _contains(item.layout(), widget):
                return True
        return False

    assert _contains(tools_row, window.btn_photo_comparison)
    assert not _contains(tools_row, window.btn_reopen_review)
