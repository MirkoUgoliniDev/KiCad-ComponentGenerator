"""@brief Le pause della pipeline e il rientro gratuito dopo che tutto e' finito.

Richiesta dell'utente, testuale: «alla partenza, dopo che YOLO ha terminato ci
deve essere una pausa per poter editare il suo risultato ok? non ho visto la
pausa. Poi dopo l'elaborazione AI stessa cosa... Poi alla fine comunque devo
poter tornare sui miei passi senza fare intervenire AI».

Qui si provano le tre fermate e la regola del denaro che le tiene insieme:

1. dopo il rilevatore locale: la revisione «Corpi rilevati» si apre PRIMA della
   riconciliazione a pagamento -- ma solo quando l'analisi passa davvero
   dall'inventario fotografico, che e' il motivo per cui l'utente non l'ha
   vista (tipologia «componente singolo»);
2. dopo l'inventario a pagamento: la finestra si ferma e propone la revisione
   dei componenti prima dell'abbinamento modelli e del 3D;
3. alla fine: si riapre la revisione, si correggono i corpi e si rigenera lo
   STEP senza che parta UNA sola chiamata al modello.

Piu' l'interruzione, che deve essere offerta in ogni fase che puo' durare
minuti, e il canale di rinuncia passato in modo difensivo all'abbinamento.
"""
from copy import deepcopy
import os
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QListWidgetItem, QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services import photo_inventory as inv
from kicadgen.services.datasheet_extractor import ExtractedParams

from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa
from tests.test_global_inventory_pipeline import pipeline  # noqa: F401 - fixture condivisa
from tests.test_reopen_detector_review import _photo_png, _proposal, LENGTH, WIDTH


# ── Materiale comune ──────────────────────────────────────────────────────

def _component(name="R1", **changes):
    """@brief Un corpo come l'inventario lo consegna alla tabella dei componenti."""
    row = dict(name=name, kind="generic", x_mm=1.0, y_mm=2.0, length_mm=4.0,
               width_mm=2.0, height_mm=1.0, rotation_deg=0, color_hex="#303030",
               marking="", package="R_0603")
    row.update(changes)
    return row


def _extracted(components=None, **changes):
    """@brief Estrazione completa di una scheda a fori passanti con corpi dalla foto."""
    values = dict(
        name="PausaModulo", description="", tags="", confidence="high", notes="note",
        layout="matrix", rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=7.62,
        pad_diameter_mm=1.7, drill_mm=1.0, body_length_mm=20, body_width_mm=10,
        body_height_mm=3, photo_png=_photo_png(), photo_source="top.png",
        components_3d=[_component()] if components is None else components)
    values.update(changes)
    return ExtractedParams(**values)


def _ready(window, monkeypatch):  # noqa: F811
    """@brief Finestra pronta per una corsa: niente rete, niente CAD, niente 3D mancante.

    @note `_freecad_available` e' un presupposto dichiarato, non una scorciatoia:
        senza FreeCAD il gruppo «Modello 3D» resta spento per scelta e la corsa
        sarebbe di solo footprint, cioe' un'altra cosa da quella in prova.
    """
    window._freecad_available = True
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData("ai"))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda self: None)
    monkeypatch.setattr(window, "_show_generated_mesh", lambda path: None)
    monkeypatch.setattr(window, "_draw_preview", lambda: None)
    monkeypatch.setattr(window, "_draw_preview_3d", lambda: None)


def _no_ai(monkeypatch):
    """@brief Sostituisce la chiamata a pagamento con una che fa fallire il test.

    @return La lista delle chiamate: deve restare vuota.
    @note Registra PRIMA di sollevare: un'eccezione dentro un worker viene
        catturata e trasformata in un segnale d'errore, quindi la sola
        `AssertionError` potrebbe non arrivare mai al test. La lista, invece,
        si legge dopo e non puo' essere zittita.
    """
    calls = []

    def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("percorso gratuito: nessuna chiamata AI deve partire")

    import kicadgen.services.openrouter as openrouter
    monkeypatch.setattr(openrouter, "call_tool", forbidden)
    return calls


def _answer_pause(monkeypatch, answer):
    """@brief Risponde alla domanda della pausa senza aprire nulla.

    @param answer Bottone standard da restituire, o None per far fallire il
        test se la domanda viene posta (caso «nessuna pausa offerta»).
    @return La lista dei testi delle domande poste.
    """
    asked = []

    def question(parent, title, text, *args, **kwargs):
        asked.append(text)
        if answer is None:
            raise AssertionError(f"nessuna domanda attesa qui: {title}")
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def _install_review_dialog(monkeypatch, correction=None):
    """@brief Sostituisce la revisione dei componenti con una che risponde da sola.

    @param correction Callable(componenti) -> componenti applicati; None
        significa «Annulla», cioe' nessuna applicazione.
    @return La lista delle aperture: ogni voce sono i componenti ricevuti.
    """
    opened = []

    class FakeReview(QDialog):
        """@brief Finta `PhotoReviewDialog`: stessa firma, stesso segnale."""

        components_applied = pyqtSignal(object)

        def __init__(self, photo_png, components, length, width, parent=None):
            super().__init__(parent)
            self._items = []
            self.received = deepcopy(components)
            opened.append(self.received)

        def exec(self):
            """@brief Applica la correzione decisa dal test, o annulla."""
            if correction is None:
                return QDialog.DialogCode.Rejected
            self.components_applied.emit(correction(deepcopy(self.received)))
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui, "PhotoReviewDialog", FakeReview)
    return opened


def _widen(components):
    """@brief Correzione del revisore: il primo corpo si sposta e si allarga."""
    corrected = deepcopy(components)
    corrected[0].update(x_mm=5.5, length_mm=7.5, width_mm=3.5)
    return corrected


def _run_extraction(window, monkeypatch):  # noqa: F811
    """@brief Porta la finestra nello stato «analisi appena conclusa», senza rete.

    @note Passa da `_extract_with_ai` e non solo da `_on_extracted`: e' la' che
        si decide la generazione automatica (`_auto_after_extraction`), cioe'
        proprio cio' che la pausa deve precedere.
    """
    row = QListWidgetItem("photo.png")
    row.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(row)
    window._extract_with_ai()


# ── 1. Pausa dopo l'inventario a pagamento ────────────────────────────────

def test_the_finished_inventory_stops_and_carries_the_corrections_into_the_build(
        window, monkeypatch):  # noqa: F811
    """@brief Positivo: la pausa si apre e cio' che si corregge finisce nel 3D.

    E' la richiesta dell'utente: «dopo l'elaborazione AI... ci deve essere una
    pausa per poter editare il suo risultato». La prova non si ferma alla
    finestra aperta: il corpo corretto deve arrivare fino ai parametri con cui
    viene costruito il solido, altrimenti la pausa sarebbe decorativa.
    """
    _ready(window, monkeypatch)
    asked = _answer_pause(monkeypatch, QMessageBox.StandardButton.Yes)
    opened = _install_review_dialog(monkeypatch, _widen)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted())
    assert window._worker is None, "il 3D non deve partire prima della pausa"

    window._on_extract_finished()

    assert len(asked) == 1 and "gratuita" in asked[0]
    assert len(opened) == 1, "la revisione dei componenti deve essersi aperta"
    assert opened[0][0]["length_mm"] == 4.0, "la revisione vede il risultato dell'AI"
    corrected = window._components_from_table()
    assert (corrected[0].x, corrected[0].length) == (5.5, 7.5)
    worker = window._worker
    assert worker is not None, "dopo la pausa la generazione riparte"
    assert [(c.x, c.length, c.width) for c in worker.body.components] == [(5.5, 7.5, 3.5)]


def test_declining_the_pause_keeps_the_ai_result_and_the_run_goes_on(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: «No» non tocca niente e non ferma la pipeline.

    Rifiutare non e' annullare: i corpi restano quelli proposti dall'AI e la
    generazione parte comunque. Il contrario -- una corsa che si interrompe
    perche' non si e' voluta la revisione -- costringerebbe a ripagare.
    """
    _ready(window, monkeypatch)
    asked = _answer_pause(monkeypatch, QMessageBox.StandardButton.No)
    opened = _install_review_dialog(monkeypatch, _widen)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted())

    window._on_extract_finished()

    assert len(asked) == 1
    assert opened == [], "rifiutando, la revisione non si apre affatto"
    kept = window._components_from_table()
    assert [(c.x, c.length, c.width) for c in kept] == [(1.0, 4.0, 2.0)]
    worker = window._worker
    assert worker is not None
    assert [(c.x, c.length) for c in worker.body.components] == [(1.0, 4.0)]
    assert "revisione rifiutata" in window.ai_progress_text.toPlainText()


def test_with_the_setting_off_nothing_is_asked_and_the_run_is_as_before(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: chi ha spento la pausa non viene fermato, ne' interrogato.

    La spunta scrive in config.json come «utente avanzato»: la scelta vale per
    le corse successive, non solo per questa. Qui la domanda stessa fa fallire
    il test, perche' «non chiedere piu'» deve voler dire esattamente questo.
    """
    _ready(window, monkeypatch)
    saved = []
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(dict(config)))
    window.chk_pause_after_inventory.setChecked(False)
    assert saved and saved[-1]["pause_after_inventory"] is False
    assert not window._inventory_pause_enabled()
    _answer_pause(monkeypatch, None)
    opened = _install_review_dialog(monkeypatch, _widen)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted())

    window._on_extract_finished()

    assert opened == []
    worker = window._worker
    assert worker is not None, "senza pausa la corsa prosegue esattamente come prima"
    assert [(c.x, c.length) for c in worker.body.components] == [(1.0, 4.0)]
    assert "disattivata nelle impostazioni" in window.ai_progress_text.toPlainText()


def test_the_pause_is_offered_once_even_if_the_run_finishes_twice(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: una seconda `finished` non riapre la stessa pausa.

    `_on_extract_finished` puo' arrivare due volte (vedi
    `_extract_finish_pending`, che la rimanda a quando i campi sono pieni):
    una revisione riaperta da sola su dati gia' applicati sarebbe un ciclo.
    """
    _ready(window, monkeypatch)
    asked = _answer_pause(monkeypatch, QMessageBox.StandardButton.Yes)
    opened = _install_review_dialog(monkeypatch, _widen)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted())

    window._on_extract_finished()
    window._on_extract_finished()

    assert len(asked) == 1 and len(opened) == 1


def test_an_extraction_without_bodies_has_nothing_to_pause_for(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: senza corpi dalla foto non c'e' niente da rivedere.

    Un componente singolo letto dal solo datasheet non produce inventario: una
    pausa li' fermerebbe la corsa per mostrare una finestra vuota.
    """
    _ready(window, monkeypatch)
    _answer_pause(monkeypatch, None)
    opened = _install_review_dialog(monkeypatch, _widen)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted(components=[]))

    window._on_extract_finished()

    assert opened == []
    assert window._worker is not None


def test_the_pause_hides_the_modal_and_puts_it_back_as_it_was(
        window, monkeypatch):  # noqa: F811
    """@brief La modale non copre la revisione, e torna com'era dopo.

    Stessa disciplina della revisione dei corpi rilevati
    (`_on_review_requested`): nascosta mentre si guarda la fotografia, rimessa
    dopo. L'orologio della modale non conta il tempo passato nascosta, quindi
    misura il lavoro e non il revisore.
    """
    _ready(window, monkeypatch)
    _answer_pause(monkeypatch, QMessageBox.StandardButton.Yes)
    seen = []

    def correction(components):
        seen.append(window.analysis_overlay.isVisible())
        return _widen(components)

    _install_review_dialog(monkeypatch, correction)
    _run_extraction(window, monkeypatch)
    window._on_extracted(_extracted())
    window.analysis_overlay.start("Analisi AI in corso", "…", abortable=True)
    window.show()
    try:
        assert window.analysis_overlay.isVisible()
        window._offer_inventory_review()
        assert seen == [False], "la modale resta nascosta mentre si rivede"
        assert window.analysis_overlay.isVisible(), "e torna visibile dopo"
        assert window.analysis_overlay.timer.isActive()
    finally:
        window.analysis_overlay.stop()
        window.hide()


def test_the_relaunched_inventory_stops_for_the_review_too(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo sull'altra porta della stessa spesa: anche il rilancio si ferma.

    L'inventario a pagamento si raggiunge da due strade -- l'estrazione
    completa e «Rilancia inventario AI» -- e rientrano entrambe da
    `_on_extracted`. La pausa appartiene alla SPESA, non alla strada: se
    valesse solo per l'estrazione, chi ripaga il rilancio si ritroverebbe di
    nuovo con l'abbinamento e il 3D partiti su corpi mai guardati.
    """
    from tests.test_reopen_detector_review import _answer_call

    calls = []
    import kicadgen.services.openrouter as openrouter
    monkeypatch.setattr(openrouter, "call_tool", _answer_call(calls))
    monkeypatch.setattr(window, "_photo_cache_dir", lambda: tmp_path / "photo-cache")
    monkeypatch.setattr(window, "_start_automatic_generation", lambda params: None)
    window._on_extracted(_extracted())
    # Il pacchetto vive nella cartella del componente: il nome resta quello
    # scelto da chi lavora (l'estrazione lo propone soltanto), quindi si salva
    # dove la finestra andra' davvero a cercarlo.
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    window._refresh_reopen_review_button()
    assert window.btn_relaunch_inventory.isEnabled()
    asked = _answer_pause(monkeypatch, QMessageBox.StandardButton.Yes)
    opened = _install_review_dialog(monkeypatch, _widen)

    window._relaunch_inventory()
    worker = window._reconcile_worker
    assert worker is not None and worker.wait(20000)
    for _ in range(20):
        QApplication.instance().processEvents()

    assert len(calls) == 1, "il rilancio paga una volta sola"
    assert len(asked) == 1 and len(opened) == 1
    assert [(c.x, c.length) for c in window._components_from_table()] == [(5.5, 7.5)]


# ── 2. Il percorso tardivo e' gratuito, dalla revisione allo STEP ─────────

def test_the_late_free_path_reaches_freecad_with_the_corrections_and_no_ai_call(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: revisione -> correzione -> Applica -> STEP, senza pagare.

    E' la richiesta centrale dell'utente: «devo poter modificare la
    disposizione delle annotazioni per poi ripassare per la generazione
    tramite FreeCAD/KiCad». La chiamata a pagamento e' sostituita da una che
    fa fallire il test se qualcuno la usa, e la geometria corretta si verifica
    dove conta: nei parametri con cui il generatore 3D viene invocato.
    """
    _ready(window, monkeypatch)
    calls = _no_ai(monkeypatch)
    opened = _install_review_dialog(monkeypatch, _widen)
    window._on_extracted(_extracted())
    assert window._worker is None, "nessuna generazione automatica in questo percorso"

    built = []

    def fake_generate_3d(params, body, project_dir, fillets, face_texts, **options):
        """@brief Finto generatore: registra il corpo ricevuto e scrive nulla."""
        built.append(deepcopy(body))
        step = Path(project_dir) / f"{params.name}.3dshapes" / f"{params.name}.step"
        step.parent.mkdir(parents=True, exist_ok=True)
        step.write_text("ISO-10303-21;", encoding="utf-8")
        return {"step_path": step, "warnings": [], "notes": []}

    monkeypatch.setattr(gui, "_generate_3d", fake_generate_3d)

    assert window.btn_edit_component_review.isEnabled()
    window.btn_edit_component_review.click()
    assert len(opened) == 1
    assert [(c.x, c.length, c.width) for c in window._components_from_table()] == [(5.5, 7.5, 3.5)]

    window._regenerate_3d()
    worker = window._cad3d_worker
    assert worker is not None and worker.wait(20000)
    for _ in range(20):
        QApplication.instance().processEvents()

    assert calls == [], "il percorso tardivo non deve fare nessuna chiamata AI"
    assert len(built) == 1, "lo STEP e' stato generato una volta"
    assert [(c.x, c.length, c.width) for c in built[0].components] == [(5.5, 7.5, 3.5)]
    log = window.ai_progress_text.toPlainText()
    assert "Rigenerazione 3D" in window.log_text.toPlainText() or "Rigenerazione 3D" in log


def test_cancelling_the_late_review_changes_nothing_and_still_costs_nothing(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: Annulla non applica nulla e non paga nulla."""
    _ready(window, monkeypatch)
    calls = _no_ai(monkeypatch)
    opened = _install_review_dialog(monkeypatch, None)
    window._on_extracted(_extracted())

    window.btn_edit_component_review.click()

    assert len(opened) == 1
    assert [(c.x, c.length) for c in window._components_from_table()] == [(1.0, 4.0)]
    assert calls == []


# ── 3. Interruzione offerta in ogni fase lunga ────────────────────────────

def _record_overlay_start(window, monkeypatch):  # noqa: F811
    """@brief Registra ogni apertura della modale con l'interruzione dichiarata."""
    started = []
    original = window.analysis_overlay.start

    def start(title="Analisi AI in corso", status="…", abortable=False):
        started.append((title, bool(abortable)))
        return original(title, status, abortable)

    monkeypatch.setattr(window.analysis_overlay, "start", start)
    return started


def test_every_long_phase_offers_the_abort_button(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: le quattro fasi lunghe aprono la modale con «Interrompi».

    Richiesta dell'utente: «la possibilità di stoppare deve essere presente in
    ogni step». Le fasi che possono durare minuti sono l'analisi AI, il
    rilancio a pagamento dell'inventario, la generazione (abbinamento modelli
    KiCad piu' solido FreeCAD) e la sola rigenerazione 3D.
    """
    _ready(window, monkeypatch)
    monkeypatch.setattr(gui.Cad3DWorker, "start", lambda self: None)
    monkeypatch.setattr(gui.ReconcileWorker, "start", lambda self: None)
    monkeypatch.setattr(window, "_photo_cache_dir", lambda: tmp_path / "photo-cache")
    started = _record_overlay_start(window, monkeypatch)

    _run_extraction(window, monkeypatch)
    assert started and started[-1][1] is True, "analisi AI"
    window._extract_worker = None
    window.analysis_overlay.stop()

    window._on_extracted(_extracted())
    window._generate(automatic=False)
    assert window._worker is not None and window._worker.body is not None
    assert started[-1][1] is True, "generazione manuale con modello 3D"
    assert window.analysis_overlay.is_abortable()
    window._worker.finished.emit()
    assert window.analysis_overlay.isHidden(), "chi apre la modale la chiude"
    window._worker = None

    window._generate(automatic=True)
    assert started[-1][1] is True, "generazione automatica"
    window._automatic_generation_active = False
    window._worker = None
    window.analysis_overlay.stop()

    window._regenerate_3d()
    assert started[-1][1] is True, "rigenerazione 3D"
    window._cad3d_worker = None
    window.analysis_overlay.stop()

    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    window._relaunch_inventory()
    assert window._reconcile_worker is not None
    assert started[-1][1] is True, "rilancio dell'inventario a pagamento"
    window._reconcile_worker = None
    window.analysis_overlay.stop()


def test_a_footprint_only_run_does_not_open_a_modal_it_would_close_at_once(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: senza modello 3D non c'e' una fase lunga da interrompere.

    Il criterio e' il lavoro, non il comando: il solo footprint 2D si scrive in
    un istante, e una modale con «Interrompi» sarebbe un lampo che promette di
    fermare qualcosa di gia' finito.
    """
    _ready(window, monkeypatch)
    started = _record_overlay_start(window, monkeypatch)
    window._on_extracted(_extracted())
    window.group_3d.setChecked(False)

    window._generate(automatic=False)

    assert window._worker is not None and window._worker.body is None
    assert started == []
    assert not window.analysis_overlay.is_abortable()


def test_a_generator_without_the_channel_withdraws_the_promise_to_stop(
        window, monkeypatch):  # noqa: F811
    """@brief Negativo: «Interrompi» sparisce quando non fermerebbe nulla.

    Un pulsante che non interrompe e' peggio della sua assenza: chi lo preme
    smette di guardare. Qui il generatore sostituito non dichiara il canale, e
    la finestra deve togliere il comando e dirlo.
    """
    _ready(window, monkeypatch)
    window.analysis_overlay.start("Generazione modello 3D", "…", abortable=True)
    try:
        assert window.analysis_overlay.is_abortable()
        window._on_cad_cancellable(False)
        assert not window.analysis_overlay.is_abortable()
        assert "non è disponibile" in window.ai_progress_text.toPlainText()
    finally:
        window.analysis_overlay.stop()


def test_the_wrapper_does_not_vouch_for_a_generator_that_cannot_stop(monkeypatch):
    """@brief Positivo e negativo sulla domanda «questa corsa e' fermabile?».

    `_generate_3d` accetta `cancel` per costruzione: interrogare lui direbbe
    sempre «si'». La risposta deve venire dal generatore reale, che per un
    modulo castellated e per una matrice e' una funzione diversa.
    """
    from kicadgen.generators.pin_header import PinHeaderParams

    params = PinHeaderParams(name="X", rows=1, cols=2, pitch=2.54, row_spacing=2.54,
                             pad_diameter=1.7, drill=1.0)
    assert gui.generation_accepts_cancel(gui._generate_3d, params) is True

    def deaf(pin_params, body, output_dir, fillets=None, face_texts=None,
             progress=None, model_progress=None):
        """@brief Generatore che non conosce il canale di interruzione."""
        return {"step_path": Path("x.step")}

    monkeypatch.setattr(gui, "generate_3d_model", deaf)
    assert gui.generation_accepts_cancel(gui._generate_3d, params) is False


def test_the_worker_declares_whether_this_run_can_be_stopped(monkeypatch, tmp_path):
    """@brief Positivo e negativo sul messaggio con cui il worker apre il 3D.

    Il worker e' l'unico che sa quale generatore sta per chiamare: e' lui a
    dover dire alla finestra se «Interrompi» avra' un oggetto. Prima taceva e
    il comando restava acceso comunque.
    """
    from PyQt6.QtTest import QSignalSpy
    from kicadgen.cad3d.body_params import Body3DParams
    from kicadgen.generators.pin_header import PinHeaderParams

    params = PinHeaderParams(name="X", rows=1, cols=2, pitch=2.54, row_spacing=2.54,
                             pad_diameter=1.7, drill=1.0)
    body = Body3DParams(length_mm=10, width_mm=5, height_mm=2)
    worker = gui.GenerateWorker(params, tmp_path, body)
    spy = QSignalSpy(worker.cancellable)
    assert worker._cancel_kwargs(gui._generate_3d) == {"cancel": worker.cancel}
    assert [row[0] for row in spy] == [True]

    def deaf(pin_params, body, output_dir, fillets=None, face_texts=None,
             progress=None, model_progress=None):
        """@brief Generatore che non conosce il canale di interruzione."""
        return {"step_path": Path("x.step")}

    monkeypatch.setattr(gui, "generate_3d_model", deaf)
    warned = QSignalSpy(worker.progress)
    assert worker._cancel_kwargs(gui._generate_3d) == {}
    assert [row[0] for row in spy] == [True, False]
    assert any("non potrà essere fermata" in row[0] for row in warned)


def test_the_cancellation_channel_is_passed_only_where_it_is_declared(monkeypatch):
    """@brief Il canale arriva all'abbinamento appena la firma lo dichiara.

    Le due modifiche -- questa finestra e l'interrompibilita' dell'abbinamento
    dei modelli in `cad3d/` -- possono atterrare in qualunque ordine: si guarda
    la firma installata invece di scommettere sull'ordine di arrivo. Positivo e
    negativo insieme, piu' il canale dedicato all'abbinamento.
    """
    from kicadgen.cad3d.body_params import Body3DParams
    from kicadgen.generators.pin_header import PinHeaderParams
    import threading

    params = PinHeaderParams(name="X", rows=1, cols=2, pitch=2.54, row_spacing=2.54,
                             pad_diameter=1.7, drill=1.0)
    body = Body3DParams(length_mm=10, width_mm=5, height_mm=2)
    cancel = threading.Event()
    seen = []

    def accepting(pin_params, body, output_dir, fillets=None, face_texts=None,
                  progress=None, model_progress=None, cancel=None):
        seen.append(dict(cancel=cancel))
        return {"step_path": Path("x.step")}

    monkeypatch.setattr(gui, "generate_3d_model", accepting)
    gui._generate_3d(params, body, Path("."), [], [], cancel=cancel)
    assert seen[-1] == {"cancel": cancel}

    def deaf(pin_params, body, output_dir, fillets=None, face_texts=None,
             progress=None, model_progress=None):
        seen.append({})
        return {"step_path": Path("x.step")}

    monkeypatch.setattr(gui, "generate_3d_model", deaf)
    gui._generate_3d(params, body, Path("."), [], [], cancel=cancel)
    assert seen[-1] == {}, "una firma senza canale non deve ricevere il canale"

    def matching(pin_params, body, output_dir, fillets=None, face_texts=None,
                 progress=None, model_progress=None, cancel=None, model_cancel=None):
        seen.append(dict(cancel=cancel, model_cancel=model_cancel))
        return {"step_path": Path("x.step")}

    monkeypatch.setattr(gui, "generate_3d_model", matching)
    gui._generate_3d(params, body, Path("."), [], [], cancel=cancel)
    assert seen[-1] == {"cancel": cancel, "model_cancel": cancel}


# ── 4. La pausa dopo il rilevatore locale: quando c'e' e quando no ────────

def test_the_detector_review_runs_for_a_populated_board(pipeline):  # noqa: F811
    """@brief Positivo: con una scheda popolata il rilevatore gira e la revisione si apre.

    E' la pausa che l'utente si aspettava: fra il rilevatore locale e la
    riconciliazione a pagamento, cosi' una proposta tolta adesso non viene ne'
    inviata ne' pagata.
    """
    reviewed = []

    def review(photo_png, proposals, length_mm, width_mm):
        reviewed.append([dict(row) for row in proposals])
        return list(proposals)

    pipeline['execute'](review_proposals=review)

    pipeline['detection'].assert_called_once()
    assert len(reviewed) == 1 and [row['_id'] for row in reviewed[0]] == [1, 2]


def test_a_single_component_never_reaches_the_detector_or_its_review(pipeline):  # noqa: F811
    """@brief Negativo: il caso dell'utente. Tipologia «componente singolo», niente pausa.

    Causa reale del «non ho visto la pausa»: l'inventario fotografico parte
    solo per una scheda POPOLATA (`datasheet_extractor`, `populated_board =
    layout == 'castellated' or component_family == 'aggregate_tht'`). Con
    «Componente singolo a fori passanti (THT)» non parte affatto: nessun
    rilevatore locale, quindi nessuna proposta, quindi nessuna revisione --
    per quanto il menu «Rilevamento foto» dica «YOLO locale + verifica AI».
    Non e' la revisione a essere rotta: non viene mai raggiunta.
    """
    pipeline['main'].update(layout='matrix', rows=2, cols=2, pitch_mm=2.54,
                            row_spacing_mm=10, pad_diameter_mm=1.7, drill_mm=1)

    def review(*args):
        raise AssertionError('la revisione non puo' + "'" + ' essere raggiunta qui')

    params = pipeline['execute'](component_family='single_tht', review_proposals=review)

    pipeline['detection'].assert_not_called()
    assert len(pipeline['requests']) == 1, 'una sola chiamata: nessun inventario fotografico'
    assert not params.components_3d


def test_the_log_says_the_detector_will_not_run_instead_of_promising_a_photo_call(
        window, monkeypatch):  # noqa: F811
    """@brief Positivo/negativo sull'annuncio: si dice cio' che accadra' davvero.

    Prima, con una tipologia da componente singolo, il log annunciava lo stesso
    «chiamata sulla fotografia affidata a …»: una fase promessa che non sarebbe
    mai avvenuta, e chi aspettava la pausa la aspettava per sempre. Ora la
    previsione ha tre esiti e solo quello CERTO viene annunciato: con la
    tipologia «(non specificata)» la disposizione la decide il modello leggendo
    i documenti, quindi non si nega nulla e la dichiarazione della spesa resta.
    """
    from tests.test_photo_detector_ui import _fake_yolo

    _fake_yolo(monkeypatch, lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("board.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData("yolo"))

    lines = []
    monkeypatch.setattr(window, "_on_ai_progress", lines.append)
    window.combo_component_type.setCurrentIndex(
        window.combo_component_type.findData("single_tht"))
    assert window._photo_inventory_family() is False
    window._extract_with_ai()
    single = "\n".join(lines)
    assert "NON passa dall'inventario fotografico" in single
    assert "Corpi rilevati" in single
    assert "affidata a" not in single, "non si annuncia una chiamata che non avverrà"

    lines.clear()
    window._extract_worker = None
    window.combo_component_type.setCurrentIndex(
        window.combo_component_type.findData("aggregate_tht"))
    assert window._photo_inventory_family() is True
    window._extract_with_ai()
    board = "\n".join(lines)
    assert "affidata a" in board, "per una scheda popolata la spesa va dichiarata"
    assert "NON passa dall'inventario fotografico" not in board

    lines.clear()
    window._extract_worker = None
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData(""))
    assert window._photo_inventory_family() is None
    window._extract_with_ai()
    unknown = "\n".join(lines)
    assert "affidata a" in unknown, "previsione incerta: la spesa resta dichiarata"
    assert "NON passa dall'inventario fotografico" not in unknown


# ── 5. La riga dei rientri si legge come una pipeline ─────────────────────

def test_the_bottom_row_reads_as_one_pipeline_with_the_price_of_each_step(window):  # noqa: F811
    """@brief L'ordine e' quello della pipeline e ogni comando dichiara il suo costo.

    Richiesta dell'utente: poter vedere dove si rientra. I tre comandi
    esistevano gia' ma stavano in fila senza dire di essere passi della stessa
    catena: qui si fissa l'ordine (rilevatore, inventario a pagamento,
    revisione dei componenti) e la regola del denaro -- chi spende lo dice, chi
    non spende si riconosce.
    """
    from tests.test_photo_inventory_gui import tab_outer_layout

    outer = tab_outer_layout(window)
    row = outer.itemAt(outer.count() - 1).layout()
    order = [row.itemAt(i).widget() for i in range(row.count())
             if row.itemAt(i).widget() is not None]
    assert order == [window.btn_reopen_review, window.btn_relaunch_inventory,
                     window.btn_edit_component_review]
    assert "gratuito" in window.btn_reopen_review.toolTip()
    assert "a pagamento" not in window.btn_reopen_review.toolTip().lower()
    assert "A PAGAMENTO" in window.btn_relaunch_inventory.toolTip()
    assert "gratuito" in window.btn_edit_component_review.toolTip()
    assert "Non avvia la generazione" in window.btn_edit_component_review.toolTip()
    assert "gratuito" in window.btn_regen_3d.toolTip()
    assert "A PAGAMENTO" in window.btn_extract_ai.toolTip()
    for step, button in enumerate((window.btn_reopen_review, window.btn_relaunch_inventory,
                                   window.btn_edit_component_review), start=1):
        assert f"Passo {step}" in button.toolTip()


def test_the_pipeline_caption_appears_only_when_a_step_is_available(window):  # noqa: F811
    """@brief Negativo: nessun rientro disponibile, nessuna riga che li annuncia.

    Stessa disciplina dei pulsanti (`_set_action_available`): cio' che non puo'
    agire non si vede. Una didascalia che elenca tre passi sopra una fila vuota
    descriverebbe qualcosa che non c'e'.
    """
    caption = window.lbl_pipeline_steps
    assert not caption.isVisibleTo(caption.parentWidget())
    window._photo_png = b"finta foto"
    window._refresh_edit_review_buttons()
    assert window.btn_edit_component_review.isVisibleTo(
        window.btn_edit_component_review.parentWidget())
    assert caption.isVisibleTo(caption.parentWidget())
    assert "gratuito" in caption.text() and "a pagamento" in caption.text()
    window._photo_png = None
    window._refresh_edit_review_buttons()
    assert not caption.isVisibleTo(caption.parentWidget())
