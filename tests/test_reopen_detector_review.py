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


# ── Interfaccia ───────────────────────────────────────────────────────────

def test_button_enabled_only_with_saved_proposals_and_extracted_extents(prepared):
    window, cache, _calls, _started, _warnings = prepared
    assert not window.btn_reopen_review.isEnabled()
    window._on_extracted(_params())
    # Estrazione fatta, ma nulla in cache: spento.
    assert not window.btn_reopen_review.isEnabled()
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    assert window.btn_reopen_review.isEnabled()
    # Quote della scheda assenti: le proposte non hanno scala, spento.
    window._on_extracted(ExtractedParams(name='', description='', tags='', confidence='low',
                                         notes='', layout='castellated', castellated_sides=[]))
    assert not window.btn_reopen_review.isEnabled()


def test_proposals_older_than_the_current_extraction_are_ignored(prepared):
    """@brief La cache e' comune ai componenti: un file di un'altra estrazione non vale."""
    window, cache, *_ = prepared
    _save_evidence(cache, [_proposal(1)], stamp='20200101-000000', photo_stamp='20200101-000001')
    window._extraction_started_stamp = '20260101-000000'
    window._on_extracted(_params())
    assert not window.btn_reopen_review.isEnabled()


def test_accepting_pays_once_for_the_kept_proposals_and_restarts_matching(prepared, monkeypatch):
    window, cache, calls, started, warnings = prepared
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [_proposal(1)])
    window.btn_reopen_review.click()
    log_before = window.ai_progress_text.toPlainText()
    assert 'Rilancio dell\'inventario su 1 proposte: una richiesta a pagamento al modello' in log_before
    assert 'una richiesta a pagamento' in window.analysis_overlay.status.text()
    _run_worker(window)
    assert warnings == []
    assert len(calls) == 1
    sent = _candidates(calls[0])
    assert [c['id'] for c in sent] == [1]
    assert calls[0]['model'] == window._photo_detector_model()
    # I componenti riconciliati hanno preso il posto di quelli vecchi...
    assert [c.name for c in window.castellated_components] == ['R1 (resistor)']
    assert window._last_extracted.components_3d[0]['package'] == 'R_0603'
    assert 'Inventario rilanciato: 1 ingombri' in window._last_extracted.notes
    # ...e l'abbinamento modelli + 3D e' ripartito per la via dell'estrazione.
    assert len(started) == 1 and started[0].components_3d[0]['package'] == 'R_0603'
    assert window.btn_extract_ai.isEnabled()
    assert window.btn_reopen_review.isEnabled()


def test_cancelling_starts_nothing(prepared, monkeypatch):
    """@brief Caso negativo: Annulla non avvia ne' worker ne' richiesta."""
    window, cache, calls, started, _warnings = prepared
    _save_evidence(cache, [_proposal(1)])
    window._on_extracted(_params())
    _answer(monkeypatch, QDialog.DialogCode.Rejected, [])
    window.btn_reopen_review.click()
    assert window._reconcile_worker is None
    assert calls == [] and started == []
    assert 'annullata' in window.ai_progress_text.toPlainText()
    assert window.btn_reopen_review.isEnabled()


def test_identical_proposals_replay_the_cache_without_a_new_call(prepared, monkeypatch):
    window, cache, calls, started, warnings = prepared
    _save_evidence(cache, [_proposal(1), _proposal(2, box=(.5, .5, .7, .7))])
    window._on_extracted(_params())
    _answer(monkeypatch, QDialog.DialogCode.Accepted, [_proposal(1)])
    window.btn_reopen_review.click()
    _run_worker(window)
    assert len(calls) == 1
    window.btn_reopen_review.click()
    _run_worker(window)
    assert warnings == []
    assert len(calls) == 1, 'proposte identiche: la richiesta non va ripagata'
    log = window.ai_progress_text.toPlainText()
    assert 'validated cache hit' in log and 'nessun nuovo addebito' in log
    assert len(started) == 2


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
