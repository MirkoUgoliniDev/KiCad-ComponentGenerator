"""@brief Contract of the progress messages and phase notifications shown in the modal.

Nessuna inferenza: le chiamate a pagamento sono finte e il rilevatore locale e'
sostituito da un doppio. Si verificano due cose che l'utente legge davvero:
quali documenti partono nella chiamata che si paga, e quale strumento sta
lavorando in un dato momento (rilevamento locale contro chiamata remota).
I casi negativi sono la meta' del contratto: una fase che non sta girando non
deve essere annunciata, e un documento non inviato non deve comparire.
"""
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services import photo_global_inventory, photo_inventory, photo_yolo
from kicadgen.services.datasheet_extractor import _files_sent_message, _LOG_NAME_CHARS


def _tiny_png() -> bytes:
    """@brief A real 4x4 PNG for image fixtures.

    CONTRACT CHANGED 2026-10-15: document images must be decodable, because
    an upload whose metadata cannot be removed is refused (fail closed); fake
    bytes named .png/.jpg no longer reach the fake provider.
    """
    from io import BytesIO
    from PIL import Image
    out = BytesIO()
    Image.new("RGB", (4, 4), "white").save(out, "PNG")
    return out.getvalue()


_TINY_PNG = _tiny_png()


# ── 1. Quali file sono stati inviati alla chiamata a pagamento ───────
#
# CONTRATTO CAMBIATO, di proposito: fra il nome del file e il suo ruolo c'era
# una virgola («pinout.png, Pinout / …»), ora c'e' " — ". L'utente ha chiesto
# i nomi in grassetto e staccati dal ruolo, e la scheda deve sapere DOVE finisce
# il nome senza indovinarlo dalla sua forma: la virgola compare anche dentro i
# nomi dei file, e riconoscere un nome da «stem.ext» avrebbe lasciato senza
# grassetto i nomi con spazi o senza estensione. Le asserzioni sotto sono le
# stesse di prima, con il nuovo separatore; nessuna e' stata allentata.


def test_one_document_is_named_with_the_role_it_is_sent_as():
    """@brief Positivo minimo: un solo documento, nome e ruolo, senza plurale sbagliato."""
    message = _files_sent_message([Path('/tmp/progetto/pinout.pdf')],
                                 {'pinout.pdf': 'pinout'})
    assert message == ('Invio a AI 1 documento per layout, quote e pinout: '
                       'pinout.pdf — Pinout / disposizione contatti.')
    assert '\n' not in message


def test_every_document_is_named_with_its_role_in_request_order():
    """@brief Positivo: tutti i file sono nominati, ciascuno col ruolo con cui parte.

    L'ordine e' quello della richiesta, cosi' la riga di log e i blocchi di
    contenuto inviati al modello si leggono uno accanto all'altro.
    """
    paths = [Path('a/pinout.png'), Path('a/top.png'), Path('a/hires.jpg')]
    roles = {'pinout.png': 'pinout', 'top.png': 'top', 'hires.jpg': 'hires'}
    message = _files_sent_message(paths, roles)
    assert message == ('Invio a AI 3 documenti per layout, quote e pinout: '
                       'pinout.png — Pinout / disposizione contatti · '
                       'top.png — Immagine con quote · '
                       'hires.jpg — Foto ad alta risoluzione.')


@pytest.mark.parametrize('roles', [None, {}, {'altro.png': 'top'}, {'foto.png': 'inventato'}])
def test_an_unknown_role_is_declared_unknown_and_never_guessed(roles):
    """@brief Negativo: senza ruolo dichiarato non si indovina dal nome del file.

    Il nome «top»/«pinout» dentro il file non e' una dichiarazione: sarebbe una
    regola basata sul nome, e sbaglierebbe sul primo utente che chiama i suoi
    file diversamente. Il ruolo mancante o non riconosciuto viene detto tale.
    """
    message = _files_sent_message([Path('x/foto.png')], roles)
    assert message == ('Invio a AI 1 documento per layout, quote e pinout: '
                       'foto.png — ruolo non dichiarato.')
    assert 'Immagine con quote' not in message


def test_a_mixed_set_keeps_the_known_roles_and_marks_only_the_unknown_one():
    """@brief Un ruolo ignoto non cancella quelli dichiarati sugli altri file."""
    message = _files_sent_message([Path('q/top.png'), Path('q/extra.pdf')],
                                 {'top.png': 'top'})
    assert 'top.png — Immagine con quote' in message
    assert 'extra.pdf — ruolo non dichiarato' in message
    assert message.startswith('Invio a AI 2 documenti')


def test_no_document_is_not_reported_as_a_call_with_zero_files():
    """@brief Negativo: zero file non e' un invio, e non deve sembrarlo.

    «Invio a AI 0 file» farebbe credere a una richiesta partita a vuoto, quindi
    pagata, mentre nessuna richiesta lascia il programma.
    """
    message = _files_sent_message([], {})
    assert message == ('Nessun documento da inviare a AI: '
                       'layout, quote e pinout non sono estraibili.')
    assert 'Invio a AI' not in message


def test_the_line_names_files_not_paths():
    """@brief Le cartelle non entrano: dicono dove sta il file, non che cosa sta
    partendo, e su una riga della scheda prenderebbero il posto del nome e del
    ruolo -- che sono l'informazione richiesta. Vale anche ora che ogni
    documento ha una riga propria: la riga e' larga quanto la scheda."""
    path = Path('/home/utente/Documenti/progetti/scheda-radio/immagini/top.png')
    message = _files_sent_message([path], {'top.png': 'top'})
    assert 'top.png' in message and str(path.parent) not in message
    assert 'immagini' not in message


def test_a_very_long_name_is_shortened_but_stays_recognizable():
    """@brief Il nome lunghissimo non deve spingere fuori i file successivi.

    E' un ripiego di presentazione dichiarato: si accorcia solo cio' che si
    mostra, e testa e coda (quindi l'estensione) restano leggibili. Il nome
    inviato al modello non viene toccato -- lo verifica
    `test_the_extraction_log_names_the_documents_and_their_roles`.
    """
    name = 'scheda-radio-vista-dall-alto-alta-risoluzione-rev-C-2026.png'
    assert len(name) > _LOG_NAME_CHARS
    message = _files_sent_message([Path('d')/name], {name: 'hires'})
    shown = message.split(': ', 1)[1].split(' — ')[0]
    assert len(shown) <= _LOG_NAME_CHARS
    assert shown.startswith('scheda-radio-vista') and shown.endswith('.png')
    assert '…' in shown


def test_a_short_name_is_never_touched():
    """@brief Negativo del precedente: sotto il limite non si accorcia nulla."""
    message = _files_sent_message([Path('d/top.png')], {'top.png': 'top'})
    assert '…' not in message


def test_the_extraction_log_names_the_documents_and_their_roles(monkeypatch, tmp_path):
    """@brief Integrazione: la riga scritta durante l'estrazione nomina i file reali.

    Verifica anche che l'accorciamento sia solo di presentazione: il nome
    completo raggiunge il modello nei blocchi di contenuto.
    """
    long_name = 'foto-dall-alto-ad-altissima-risoluzione-rev-C.png'
    paths = [tmp_path / 'pinout.pdf', tmp_path / long_name]
    paths[0].write_bytes(b'%PDF-1.4 offline')
    paths[1].write_bytes(_TINY_PNG)
    roles = {'pinout.pdf': 'pinout', long_name: 'hires'}
    call = Mock(return_value=dict(
        layout='castellated', castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54)]))
    monkeypatch.setattr(extractor, 'api_key', lambda: 'offline')
    monkeypatch.setattr(extractor, 'check_credit', lambda **kwargs: None)
    monkeypatch.setattr(extractor, 'call_tool', call)
    logs = []
    extractor.extract_pin_header_params(paths, document_roles=roles, progress=logs.append,
                                       photo_cache_dir=tmp_path / 'cache')
    (line,) = [l for l in logs if l.startswith('Invio a AI')]
    assert '2 documenti' in line
    assert 'pinout.pdf — Pinout / disposizione contatti' in line
    assert 'Foto ad alta risoluzione' in line and '…' in line
    assert str(tmp_path) not in line
    sent = ' '.join(b['text'] for b in call.call_args.kwargs['content_blocks']
                    if b['type'] == 'text')
    assert long_name in sent, 'il nome accorciato e’ solo per il log'


def test_files_not_sent_do_not_appear_in_the_line():
    """@brief Negativo: la riga descrive l'invio, non la cartella del progetto."""
    message = _files_sent_message([Path('p/top.png')], {'top.png': 'top'})
    assert 'pinout' not in message.lower().split(': ', 1)[1]


# ── 1b. La stessa riga per due lettori: il log e la scheda della modale ──
#
# La riga resta UNA per il log -- un evento, una marca temporale, come ogni
# altra riga -- e la scheda la rimpagina a un documento per riga
# (`analysis_overlay.enumeration_lines`). Qui si verifica il patto fra le due
# parti sul testo; che la scheda poi la disegni senza tagliarla e' provato in
# tests/test_analysis_overlay.py, dove c'e' un widget vero da misurare.


def _card_lines(message):
    """Le righe che la scheda ricava dalla riga di log, senza aprire una finestra."""
    overlay = pytest.importorskip('kicadgen.gui.analysis_overlay')
    return overlay.enumeration_lines(message)


@pytest.mark.parametrize('count', (2, 3, 8))
def test_the_event_stays_one_log_line_however_many_documents(count):
    """@brief Il log non si spezza: un evento resta una riga, con una marca sola.

    E' il motivo per cui la divisione fra i due lettori NON e' fatta mettendo
    un a capo nel messaggio: una riga su piu' righe rompe la forma con cui il
    resto del log si rilegge a ritroso (`analisi-ai.log`), e la scheda non
    saprebbe comunque quanto alta farsi. La riga viene rimpaginata a valle.
    """
    names = [f'documento-{index}.png' for index in range(count)]
    roles = {name: ('top', 'pinout', 'hires')[index % 3]
             for index, name in enumerate(names)}
    message = _files_sent_message([Path('d') / name for name in names], roles)
    assert '\n' not in message and '\r' not in message
    assert message.count(' · ') == count - 1
    assert message.startswith(f'Invio a AI {count} documenti')


def test_the_card_puts_one_document_per_line_with_its_role():
    """@brief Positivo: dalla riga sola la scheda ricava intestazione piu' una
    riga per documento, e su ogni riga restano il nome e il ruolo.

    E' la richiesta dell'utente: «i nomi dei file uno sotto l'altro, più
    chiaro». Il patto e' tutto nella punteggiatura della riga -- due punti che
    chiudono l'annuncio, punto mediano fra i documenti -- quindi si verifica
    qui, accanto a chi la scrive.
    """
    paths = [Path('a/pinout.png'), Path('a/top.png'), Path('a/hires.jpg')]
    roles = {'pinout.png': 'pinout', 'top.png': 'top', 'hires.jpg': 'hires'}
    lines = _card_lines(_files_sent_message(paths, roles))
    assert lines == ['Invio a AI 3 documenti per layout, quote e pinout:',
                     'pinout.png — Pinout / disposizione contatti',
                     'top.png — Immagine con quote',
                     'hires.jpg — Foto ad alta risoluzione.']


@pytest.mark.parametrize('paths,roles', [
    ([Path('a/top.png')], {'top.png': 'top'}),
    ([], {}),
])
def test_one_document_and_no_document_are_not_lists(paths, roles):
    """@brief Negativo: con un documento solo, o nessuno, non c'e' nulla da
    incolonnare e la riga resta una riga.

    Un elenco di una voce sarebbe due righe per dire cio' che ne occupa una, e
    la riga dello zero non elenca affatto: non deve poter diventare un elenco
    vuoto, che sembrerebbe un invio a vuoto.
    """
    assert _card_lines(_files_sent_message(paths, roles)) is None


@pytest.mark.parametrize('name', ('MOD_REV_B.pdf', 'MOD_REV_B'))
def test_a_name_shaped_like_a_service_marker_still_reaches_the_card(name):
    """@brief Negativo, contro i filtri VERI della modale.

    UPPER_SNAKE e' la forma con cui questo programma marca le proprie righe di
    servizio, ed e' anche un nome di file legittimo (una revisione, un
    disegno). Il marcatore si cerca solo in TESTA alla riga, e questa riga
    comincia con l'annuncio: il documento dell'utente non puo' quindi far
    sparire dalla scheda l'unica riga che dice che cosa e' partito. Si prova
    anche il nome senza estensione, che e' il caso peggiore.
    """
    overlay = pytest.importorskip('kicadgen.gui.analysis_overlay')
    window = pytest.importorskip('kicadgen.gui.main_window')
    message = _files_sent_message([Path('d') / name, Path('d/top.png')],
                                  {name: 'pinout', 'top.png': 'top'})
    assert not overlay.is_machine_line(message)
    assert not window.per_item_detail(message)
    lines = overlay.enumeration_lines(message)
    assert lines[1] == f'{name} — Pinout / disposizione contatti'
    assert overlay.elide_paths(message) == message, (
        'un nome di file non e’ un percorso e non va accorciato come tale')


def test_the_role_label_with_a_slash_is_not_taken_for_a_path():
    """@brief Negativo dedicato: «Pinout / disposizione contatti» contiene una
    barra, e la scheda accorcia i percorsi assoluti. Se l'etichetta finisse
    sotto quella regola, la riga perderebbe proprio il ruolo che deve dire."""
    overlay = pytest.importorskip('kicadgen.gui.analysis_overlay')
    message = _files_sent_message([Path('/tmp/p/pinout.pdf')], {'pinout.pdf': 'pinout'})
    assert overlay.elide_paths(message) == message
    assert 'Pinout / disposizione contatti' in message


# ── 2. Quale strumento sta lavorando: rilevamento locale o chiamata ──


def _proposal(identity=1):
    return dict(_id=identity, _box=[.2, .2, .4, .4], name='detector hypothesis',
                kind='generic', x_mm=-8, y_mm=-4, length_mm=8, width_mm=4,
                height_mm=1, rotation_deg=0, color_hex='#303030')


@pytest.fixture
def inventory(monkeypatch, tmp_path):
    """@brief Run extract_inventory with local detection and the paid call doubled.

    @return Callable(**options) -> (phase calls, log lines), so each test reads
        the exact sequence of announcements the modal would receive.
    """
    pixels = np.zeros((100, 200, 3), np.uint8)
    monkeypatch.setattr(photo_inventory, 'prepare_photo', lambda *args: pixels.copy())
    detect = Mock(return_value=[_proposal(1), _proposal(2)])
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', detect)
    reconcile = Mock(return_value=[])
    monkeypatch.setattr(photo_global_inventory, 'review_global_inventory', reconcile)
    state = dict(detect=detect, reconcile=reconcile)

    def execute(**options):
        calls, logs = [], []
        arguments = dict(detector='yolo', classifier_model='paid/global-model',
                         progress=logs.append, phase=lambda tool, detail: calls.append((tool, detail)),
                         yolo_model_path=tmp_path / 'fixture.onnx', cache_dir=None)
        arguments.update(options)
        report = photo_inventory.extract_inventory(
            [tmp_path / 'top.png'], 'vision/model', 40, 20, ('resistor',), Mock(), **arguments)
        state['report'] = report
        return calls, logs

    state['execute'] = execute
    return state


def test_local_detection_is_announced_before_it_starts_then_the_paid_call(inventory):
    """@brief Positivo: la sequenza esatta e' rilevamento locale, poi chiamata pagata.

    L'ordine e' il contratto: la modale deve mostrare la fase locale mentre
    ONNX gira, e tornare al modello remoto quando la richiesta parte. Il
    dettaglio della fase 'llm_inventory' e' il modello effettivamente usato per la
    riconciliazione, non quello della vista.
    """
    calls, _ = inventory['execute']()
    # Contratto aggiornato (utente, 2026-09-25): il modello vero, file e formato.
    # 'llm_inventory' (26/09): la chiamata pagata spedisce la foto della scheda.
    # 27/09: i passi locali di OpenCV (calibrazione, misura dei bordi) sono fasi
    # annunciate come le altre, nel loro ordine reale.
    assert calls == [('opencv', 'ricerca e calibrazione della scheda'), ('yolo', 'fixture.onnx (ONNX)'),
                     ('opencv', 'misura dei bordi dei corpi proposti'), ('llm_inventory', 'paid/global-model')]  # il modello del test
    assert inventory['detect'].called and inventory['reconcile'].called


def test_the_vision_only_detector_never_claims_a_local_phase(inventory):
    """@brief Negativo: senza rilevatore locale la fase locale non esiste.

    E' il caso del run dell'utente, che aveva il rilevatore locale spento:
    annunciare 'yolo' descriverebbe un lavoro che non sta avvenendo.
    """
    calls, _ = inventory['execute'](detector='vlm')
    # La calibrazione con OpenCV avviene anche senza rilevatore; i bordi no,
    # perche' non ci sono proposte da misurare.
    assert calls == [('opencv', 'ricerca e calibrazione della scheda'), ('llm_inventory', 'paid/global-model')]
    assert not inventory['detect'].called
    assert not any(tool == 'yolo' for tool, _ in calls)


def test_the_local_phase_is_announced_before_the_detector_runs(inventory, monkeypatch):
    """@brief La segnalazione precede il rilevamento, non lo segue.

    Se arrivasse dopo, la modale mostrerebbe la fase locale quando e' gia'
    finita, cioe' mai per l'utente che guarda.
    """
    order = []
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components',
                        Mock(side_effect=lambda *a, **k: order.append('detect') or [_proposal(1)]))
    monkeypatch.setattr(photo_global_inventory, 'review_global_inventory',
                        Mock(side_effect=lambda *a, **k: order.append('paid call') or []))
    calls, _ = inventory['execute'](
        phase=lambda tool, detail: order.append(f'phase:{tool}'))
    assert order == ['phase:opencv', 'phase:yolo', 'detect', 'phase:opencv',
                     'phase:llm_inventory', 'paid call']
    assert calls == []


def test_no_phase_callback_behaves_exactly_as_before(inventory):
    """@brief Negativo: senza callback l'estrazione procede identica."""
    calls, logs = inventory['execute'](phase=None)
    assert calls == []
    assert inventory['detect'].called and inventory['reconcile'].called
    assert not any('fase' in line for line in logs)


def test_a_failing_phase_callback_never_breaks_the_extraction(inventory):
    """@brief Una modale rotta non deve far perdere un'elaborazione che si paga.

    La segnalazione e' informativa: l'errore viene registrato nel log e il
    lavoro continua, esattamente come per `progress`.
    """
    def broken(tool, detail):
        raise RuntimeError('modale chiusa')

    calls, logs = inventory['execute'](phase=broken)
    assert calls == []
    assert inventory['detect'].called and inventory['reconcile'].called
    # Quattro fasi annunciate (27/09: calibrazione e bordi con OpenCV, YOLO, AI).
    assert sum('Segnalazione della fase non riuscita' in line for line in logs) == 4
    assert any('modale chiusa' in line for line in logs)


def test_the_extractor_forwards_the_phase_callback_to_the_photo_inventory(monkeypatch, tmp_path):
    """@brief Senza l'inoltro la modale non vedrebbe mai la fase locale.

    L'unico `phase` che la GUI passa e' quello dell'estrazione: se non
    raggiunge l'inventario fotografico, il rilevamento locale resta invisibile
    -- che e' il difetto segnalato dall'utente.
    """
    photo = tmp_path / 'top.png'
    photo.write_bytes(_TINY_PNG)
    payload = dict(layout='castellated', body_length_mm=40, body_width_mm=20,
                   top_photo_index=0,
                   castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54)])
    inventory_call = Mock(return_value=photo_inventory.InventoryResult())
    monkeypatch.setattr(extractor, 'api_key', lambda: 'offline')
    monkeypatch.setattr(extractor, 'check_credit', lambda **kwargs: None)
    monkeypatch.setattr(extractor, 'call_tool', Mock(return_value=payload))
    monkeypatch.setattr(photo_yolo, 'validate_yolo_setup', Mock())
    monkeypatch.setattr(photo_inventory, 'extract_inventory', inventory_call)
    calls = []
    extractor.extract_pin_header_params(
        [photo], progress=lambda message: None, photo_detector='yolo',
        yolo_model_path=tmp_path / 'fixture.onnx', photo_cache_dir=tmp_path / 'cache',
        phase=lambda tool, detail: calls.append((tool, detail)))
    forwarded = inventory_call.call_args.kwargs['phase']
    forwarded('yolo', 'rilevatore locale ONNX')
    assert calls[-1] == ('yolo', 'rilevatore locale ONNX')
    assert ('llm', extractor.DEFAULT_MODEL) in calls
