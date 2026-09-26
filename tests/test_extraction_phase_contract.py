"""@brief Contratto delle fasi annunciate dall'estrazione: mai una fase che non gira.

La modale mostra QUALE strumento sta lavorando, e la distinzione pesa: il
rilevatore locale non costa nulla, la chiamata al modello si paga. Il contratto
e' documentato su `photo_inventory.extract_inventory` (@param phase): la fase
'yolo' viene annunciata immediatamente prima del rilevatore locale, mai prima
della calibrazione, e non esiste per i rilevatori 'ai' e 'vlm'.

Qui si verifica il lato dell'estrattore generale, che e' il punto in cui la
segnalazione era sbagliata: annunciava il rilevatore locale prima di sapere se
ci fosse una foto calibrabile, e non chiudeva la fase quando l'inventario
falliva e l'errore veniva inghiottito nelle note.

Nessuna inferenza reale: le chiamate a pagamento sono finte, il rilevatore
locale e' un doppio e nessun file ONNX viene caricato.
"""
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services import photo_inventory, photo_yolo


#: Risposta minima dell'analisi principale che porta al passaggio fotografico:
#: un modulo castellated con le quote del corpo dichiarate.
_PAYLOAD = dict(layout='castellated', body_length_mm=40, body_width_mm=20,
                top_photo_index=0,
                castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54)])


@pytest.fixture
def extraction(monkeypatch, tmp_path):
    """@brief Esegue `extract_pin_header_params` offline raccogliendo le fasi.

    @return Callable(**options) -> (fasi annunciate, righe di log, parametri),
        cosi' ogni test legge la sequenza esatta che la modale riceverebbe.
    """
    photo = tmp_path / 'top.png'
    photo.write_bytes(b'offline fixture')
    monkeypatch.setattr(extractor, 'api_key', lambda: 'offline')
    monkeypatch.setattr(extractor, 'check_credit', lambda **kwargs: None)
    monkeypatch.setattr(extractor, 'call_tool', Mock(return_value=dict(_PAYLOAD)))
    monkeypatch.setattr(photo_yolo, 'validate_yolo_setup', Mock())

    def execute(**options):
        phases, logs = [], []
        arguments = dict(photo_detector='yolo', yolo_model_path=tmp_path / 'fixture.onnx',
                         photo_cache_dir=tmp_path / 'cache', progress=logs.append,
                         phase=lambda tool, detail: phases.append((tool, detail)))
        arguments.update(options)
        params = extractor.extract_pin_header_params([photo], **arguments)
        return phases, logs, params

    return execute


def test_no_local_phase_is_claimed_when_no_view_can_be_calibrated(extraction, monkeypatch):
    """@brief Riproduzione (a): senza vista calibrabile il rilevatore non parte mai.

    `extract_inventory` esce prima di caricare l'ONNX. La fase locale annunciata
    dall'estrattore prima della calibrazione restava sullo schermo per tutto il
    resto della corsa, descrivendo un rilevatore che non e' mai stato caricato.
    """
    detect = Mock()
    monkeypatch.setattr(photo_inventory, 'prepare_photo', lambda *args: None)
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', detect)

    phases, logs, params = extraction()

    assert not detect.called, 'nessuna foto calibrabile: il rilevatore non gira'
    assert not any(tool == 'yolo' for tool, _ in phases), phases
    assert phases == [('llm', extractor.DEFAULT_MODEL)]
    assert any("Nessuna vista dall'alto calibrabile" in line for line in logs)


def test_the_local_phase_is_closed_when_the_detector_dies_after_validation(
        extraction, monkeypatch):
    """@brief Riproduzione (b): il file ONNX sparisce fra validazione e rilevamento.

    L'errore viene inghiottito nelle note e l'elaborazione prosegue senza
    inventario: senza la chiusura della fase la modale resterebbe sul
    rilevatore locale fino alla fine della corsa. La fase annunciata prima del
    rilevatore e' corretta (li' il rilevatore sta davvero partendo); cio' che
    serve e' che qualcuno la chiuda.
    """
    monkeypatch.setattr(photo_inventory, 'prepare_photo',
                        lambda *args: np.zeros((100, 200, 3), np.uint8))
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components',
                        Mock(side_effect=ValueError('file ONNX non piu’ disponibile')))

    phases, _, params = extraction()

    # Contratto aggiornato (utente, 2026-09-25): il dettaglio nomina il modello
    # vero, file e formato, non piu' un «ONNX» fisso.
    assert ('yolo', 'fixture.onnx (ONNX)') in phases  # il modello passato dal test
    assert phases[-1] == ('', ''), 'la fase va chiusa: nessuno strumento sta lavorando'
    assert phases[-1][0] != 'yolo'
    assert 'Inventario fotografico fallito' in (params.notes or '')
    assert 'file ONNX' in (params.notes or '')


def test_a_successful_inventory_is_not_followed_by_a_closing_phase(extraction, monkeypatch):
    """@brief Negativo del precedente: la chiusura appartiene al fallimento.

    Se la fase venisse chiusa sempre, la chiusura non direbbe nulla: quando
    l'inventario riesce l'ultima fase annunciata resta quella dell'inventario.
    """
    report = photo_inventory.InventoryResult()
    report.components = []
    monkeypatch.setattr(photo_inventory, 'extract_inventory', Mock(return_value=report))

    phases, _, params = extraction()

    assert ('', '') not in phases, phases
    assert 'Inventario fotografico fallito' not in (params.notes or '')


@pytest.mark.parametrize('detector', ['ai', 'vlm'])
def test_the_paid_vision_call_is_still_announced_for_the_detectors_that_make_it(
        extraction, monkeypatch, detector):
    """@brief Positivo: per 'ai' e 'vlm' il passo successivo e' davvero una spesa.

    Senza rilevatore locale non c'e' nessuna fase locale da annunciare, ma la
    chiamata al modello di visione parte subito dopo: quella si paga, e va
    detta prima che parta.
    """
    inventory_call = Mock(return_value=photo_inventory.InventoryResult())
    monkeypatch.setattr(photo_inventory, 'extract_inventory', inventory_call)

    phases, _, _ = extraction(photo_detector=detector, vision_model='vision/paid-model')

    assert inventory_call.called
    # La chiamata spedisce la foto della scheda, non i documenti: fase
    # 'llm_inventory' (stessa chiamata remota, sorgente diversa nella modale).
    assert ('llm_inventory', 'vision/paid-model') in phases
    assert not any(tool == 'yolo' for tool, _ in phases), phases
