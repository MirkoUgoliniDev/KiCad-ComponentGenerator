"""@brief Pulsanti della scheda Progetto: niente comandi accesi a vuoto, niente eco in modale.

Casi osservati dall'utente sulle schermate dell'applicazione:

- «Qui c'è troppa confusione con i pulsanti sotto. alcuni non hanno neppure
  senso. se il log non è presente il pulsante non si deve vedere».
- «Poi se questa fase è stata completata, dovrebbe essere possibile poterla
  sempre editare, inserire un pulsante in basso chiaro».
- «Non ripetere sotto il modello lo hai scritto sopra, ma spiega cosa stai
  facendo» e «evitare di mettere il log dei componenti, non servirebbe a nulla»:
  la riga di stato della modale deve dire la fase, non ripetere l'intestazione
  ne' sfogliare i componenti uno per uno.
- «Mi piacerebbe vedere indicata anche la fase di elaborazione YOLO locale»:
  il rilevatore locale deve poter annunciarsi come fa il modello a pagamento.

Ogni prova ha il suo caso negativo: cio' che deve restare visibile, restare nel
log, o arrivare comunque in modale.
"""
from pathlib import Path

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QLayout, QScrollArea

from kicadgen.gui import main_window as gui
from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


#: Riga di rendiconto per-elemento come la scrive l'abbinamento dei modelli:
#: e' l'esemplare portato dall'utente, ma cio' che si verifica e' la forma
#: «elemento: campo; campo; campo», non questa frase.
MATCHER_LINE = ("SOT-23-6 / SOT-363-like small outline, 6 leads: 3 dimensional "
                "candidates; review_required; identity not verified.")
PHASE_LINE = "Abbinamento dei modelli KiCad in corso…"


def _own_visible(button):
    """@brief Visibilita' propria del pulsante, non quella ereditata dal tab."""
    return button.isVisibleTo(button.parentWidget())


def _clear_log(window):  # noqa: F811
    """@brief Riporta la sessione allo stato «nessun log ancora scritto»."""
    window.ai_progress_text.clear()
    window._pending_log_lines = []
    log = window._log_file()
    if log.is_file():
        log.unlink()
    window._refresh_ai_log_button()


def _contains(layout, widget):
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is widget:
            return True
        if item.layout() is not None and _contains(item.layout(), widget):
            return True
    return False


# ── «Se il log non è presente il pulsante non si deve vedere» ─────────────

def test_ai_log_button_appears_only_once_a_log_line_exists(window):  # noqa: F811
    """@brief Negativo: senza log il pulsante non c'e'. Positivo: alla prima riga compare."""
    _clear_log(window)
    assert not _own_visible(window.btn_ai_log), "un log inesistente non si apre"
    assert not window.btn_ai_log.isEnabled()
    window._append_log("Tipologia componente: modulo castellato.")
    assert _own_visible(window.btn_ai_log), "il log c'e': il pulsante deve comparire"
    assert window.btn_ai_log.isEnabled()


def test_ai_log_button_follows_the_log_of_the_component_name(window):  # noqa: F811
    """@brief La condizione e' il log vero, non «e' stata avviata un'analisi».

    Positivo: il diario scritto su disco da una sessione precedente basta a far
    comparire il pulsante quando il nome torna a puntare a quella cartella.
    Negativo: un nome che punta a una cartella senza diario lo fa sparire.
    """
    _clear_log(window)
    folder = window._project_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / gui.MainWindow.LOG_FILENAME).write_text("[t] corsa precedente\n", encoding="utf-8")
    window.field_name.setText("")
    window.field_name.setText("Componente_di_prova")
    assert not window.ai_progress_text.toPlainText(), "nulla in memoria: conta il file"
    assert _own_visible(window.btn_ai_log)
    window.field_name.setText("AltroComponente")
    assert not _own_visible(window.btn_ai_log), "altra cartella, altro diario: non c'e'"


def test_an_empty_log_file_is_not_an_openable_log(window):  # noqa: F811
    """@brief Negativo: un file vuoto non e' un log; il pulsante resta invisibile."""
    _clear_log(window)
    folder = window._project_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / gui.MainWindow.LOG_FILENAME).write_text("", encoding="utf-8")
    window.field_name.setText("")
    window.field_name.setText("Componente_di_prova")
    assert not _own_visible(window.btn_ai_log)


# ── «Un pulsante in basso chiaro» per rieditare la fase completata ────────

def test_edit_review_command_sits_in_the_bottom_row_of_the_project_tab(window):  # noqa: F811
    """@brief Positivo: nella stessa riga di «Rivedi proposte salvate…», in fondo alla scheda."""
    scroll = window.tabs.widget(0)
    tab = scroll.widget() if isinstance(scroll, QScrollArea) else scroll
    outer = tab.layout()
    row = outer.itemAt(outer.count() - 1).layout()
    assert isinstance(row, QLayout)
    widgets = [row.itemAt(i).widget() for i in range(row.count())]
    assert window.btn_edit_component_review in widgets
    assert window.btn_reopen_review in widgets and window.btn_relaunch_inventory in widgets
    assert window.btn_edit_component_review.text().strip()


def test_edit_review_command_is_mirrored_in_the_3d_bottom_row(window):  # noqa: F811
    """@brief La copia nel tab Modello 3D e' lo stesso comando con lo stesso testo."""
    row = window.btn_regen_3d.parentWidget().layout()
    assert _contains(row, window.btn_edit_component_review_3d)
    assert window.btn_edit_component_review_3d.text() == window.btn_edit_component_review.text()


def test_both_edit_review_copies_open_the_component_review(window, monkeypatch):  # noqa: F811
    """@brief Positivo: entrambe le copie del comando chiamano _show_photo_comparison.

    Dopo la rimozione del doppione fra gli strumenti dell'analisi queste sono
    le uniche due copie: riga in fondo alla scheda Progetto e riga in fondo
    alla scheda Modello 3D.
    """
    calls = []
    monkeypatch.setattr(window, "_show_photo_comparison", lambda *a, **k: calls.append(True))
    window._photo_png = b"photo"
    window._set_review_buttons_enabled(True)
    # Il tab Modello 3D e' spento finche' non c'e' un modello: si riaccende
    # come dopo una generazione, altrimenti il clic non parte per quel motivo.
    page = window.btn_edit_component_review_3d
    while page.parentWidget() is not None and page.parentWidget() is not window.tabs:
        page = page.parentWidget()
        page.setEnabled(True)
    for button in (window.btn_edit_component_review, window.btn_edit_component_review_3d):
        # I segnali sono collegati al metodo originale: si verifica il
        # collegamento attraverso l'attributo, come fa Qt col bound method.
        button.clicked.disconnect()
        button.clicked.connect(window._show_photo_comparison)
        button.click()
    assert calls == [True, True]


def test_edit_review_is_hidden_without_the_calibrated_photo(window):  # noqa: F811
    """@brief Negativo: senza foto in memoria il comando non puo' aprire nulla, quindi non si vede.

    Positivo: con la foto compare in entrambe le copie, che restano allineate.
    """
    window._photo_png = None
    window._set_review_buttons_enabled(True)
    assert not _own_visible(window.btn_edit_component_review)
    assert not _own_visible(window.btn_edit_component_review_3d)
    window._photo_png = b"photo"
    window._set_review_buttons_enabled(True)
    assert _own_visible(window.btn_edit_component_review)
    assert _own_visible(window.btn_edit_component_review_3d)
    assert window.btn_edit_component_review.isEnabled()


def test_saved_proposal_buttons_are_hidden_when_nothing_can_be_reopened(window):  # noqa: F811
    """@brief Negativo: revisione e rilancio spenti sono anche invisibili, in entrambe le schede."""
    window._set_review_buttons_enabled(False)
    for name in ("btn_reopen_review", "btn_relaunch_inventory",
                 "btn_reopen_review_3d", "btn_relaunch_inventory_3d"):
        button = getattr(window, name)
        assert not _own_visible(button) and not button.isEnabled()
    window._set_review_buttons_enabled(True)
    for name in ("btn_reopen_review", "btn_relaunch_inventory",
                 "btn_reopen_review_3d", "btn_relaunch_inventory_3d"):
        assert _own_visible(getattr(window, name))


# ── La riga di stato della modale ─────────────────────────────────────────

def test_per_item_matcher_detail_stays_in_the_log_only(window):  # noqa: F811
    """@brief Negativo per la modale, positivo per il log: il dettaglio per-elemento non sale.

    Il contatore «n/N componenti» riassume gia' quella riga; il log invece la
    conserva per intero, perche' serve a ricostruire l'abbinamento.
    """
    window._on_ai_progress(PHASE_LINE)
    window._on_ai_progress(MATCHER_LINE)
    assert window.analysis_overlay.status.text() == PHASE_LINE
    assert MATCHER_LINE in window.ai_progress_text.toPlainText()
    assert MATCHER_LINE in window.log_text.toPlainText()


def test_a_phase_sentence_still_reaches_the_modal(window):  # noqa: F811
    """@brief Positivo: una frase di fase, anche con un punto e virgola, arriva in modale."""
    window._on_ai_progress("Foto calibrata su 25.4 x 17.8 mm; ritaglio pronto.")
    assert "ritaglio pronto" in window.analysis_overlay.status.text()


@pytest.mark.parametrize("line, per_item", [
    (MATCHER_LINE, True),
    ("QFN-24: 2 dimensional candidates; auto_matched; identity not verified.", True),
    ("R_0603: no_candidate", False),  # senza elenco di campi non e' un rendiconto
    ("Generazione del footprint KiCad…", False),
    ("Profilo AI: Modulo castellato.", False),
    ("Proposte salvate in photo-review/x.json: 41 corpi; si riapre senza AI.", False),
])
def test_per_item_detail_recognises_the_shape_not_the_sentence(line, per_item):
    """@brief La regola e' strutturale: elenco di campi dopo i due punti, non una frase nota."""
    assert gui.per_item_detail(line) is per_item


def test_modal_does_not_echo_the_model_already_in_the_header(window):  # noqa: F811
    """@brief Negativo: una riga il cui unico contenuto e' il dettaglio dell'intestazione non sale.

    Positivo: la stessa intestazione non zittisce una riga che dice anche cosa
    sta accadendo, anche se nomina lo stesso modello.
    """
    window._on_phase("llm", "anthropic/claude-opus-5", 0, 0)
    window._on_ai_progress(PHASE_LINE)
    window._on_ai_progress("Modello AI unico: anthropic/claude-opus-5.")
    assert window.analysis_overlay.status.text() == PHASE_LINE, "l'intestazione lo dice gia'"
    assert "Modello AI unico: anthropic/claude-opus-5." in window.ai_progress_text.toPlainText()
    informative = "Invio a anthropic/claude-opus-5 di 3 file per layout, quote e pinout."
    window._on_ai_progress(informative)
    assert window.analysis_overlay.status.text() == informative


def test_without_a_header_detail_nothing_is_suppressed(window):  # noqa: F811
    """@brief Negativo del filtro: senza dettaglio in intestazione ogni frase passa."""
    window._on_phase("kicad", "", 0, 0)
    window._on_ai_progress("Modello AI unico: anthropic/claude-opus-5.")
    assert "anthropic/claude-opus-5" in window.analysis_overlay.status.text()


# ── Fasi: lo strumento mostrato e' quello che sta davvero lavorando ───────

def test_model_matching_is_announced_as_kicad_and_the_solid_as_freecad():
    """@brief L'abbinamento interroga la libreria KiCad; FreeCAD parte dopo.

    Mostrare il solido di FreeCAD durante l'abbinamento diceva una cosa falsa
    su quale programma stesse lavorando.
    """
    for worker in (gui.GenerateWorker.__new__(gui.GenerateWorker),
                   gui.Cad3DWorker.__new__(gui.Cad3DWorker)):
        gui.QThread.__init__(worker)
        spy = QSignalSpy(worker.phase)
        worker._model_progress(7, 41)
        assert [spy[0][0], spy[0][2], spy[0][3]] == ["kicad", 7, 41]
        worker.progress.connect(lambda message: None)
        worker._cad_progress("FreeCAD: KICADGEN_COMPONENT_START:1/41")
        assert spy[1][0] == "freecad", "la costruzione del solido e' FreeCAD"


def test_extract_worker_forwards_a_phase_callable_to_the_overlay(window, monkeypatch):  # noqa: F811
    """@brief Positivo: la callable passata al servizio porta la fase in modale.

    Contratto del servizio: phase(tool, detail). Il servizio e' sostituito,
    cosi' la prova non dipende dalla firma installata: si chiama la callable
    consegnata e si guarda l'intestazione della modale, che e' cio' che
    l'utente non vedeva mai per il rilevatore locale.
    """
    captured = {}

    def extract(paths, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(gui, "extract_pin_header_params", extract)
    worker = gui.ExtractWorker([Path("board.png")], model="vendor/datasheet")
    worker.phase.connect(window._on_phase)
    worker.run()
    assert callable(captured.get("phase"))
    captured["phase"]("yolo", "rilevatore locale ONNX")
    assert "YOLO" in window.analysis_overlay.tool.text()
    assert "rilevatore locale ONNX" in window.analysis_overlay.tool.text()
    captured["phase"]("llm", "vendor/datasheet")
    assert "Modello AI" in window.analysis_overlay.tool.text()


def test_phase_is_not_forced_on_a_service_that_does_not_accept_it(monkeypatch):
    """@brief Negativo: con una firma senza `phase` non si passa nulla, invece di un TypeError.

    Serve perche' interfaccia e servizi cambiano separatamente: la fase in piu'
    non deve poter far fallire un'estrazione.
    """
    captured = {}

    def extract(paths, model=None, vision_model=None, grounding_prompt=None,
                verification_prompt=None, verification_model=None, terminal_prompt=None,
                document_roles=None, component_family=None, photo_detector="ai",
                yolo_model_path=None, system_prompt=None, extra_instructions=None,
                include_nc_pins=True, progress=None, review_proposals=None):
        captured.update(dict(model=model, photo_detector=photo_detector))
        return object()

    monkeypatch.setattr(gui, "extract_pin_header_params", extract)
    worker = gui.ExtractWorker([Path("board.png")], model="vendor/datasheet")
    errors = QSignalSpy(worker.error)
    done = QSignalSpy(worker.done)
    worker.run()
    assert len(errors) == 0 and len(done) == 1
    assert captured["model"] == "vendor/datasheet"
    assert gui.phase_kwargs(extract, lambda *a: None) == {}


def test_phase_is_passed_when_the_signature_declares_it():
    """@brief Positivo del rilevamento: dichiarato come parametro o via **kwargs, si passa."""
    def declared(paths, *, phase=None):
        return None

    def variadic(paths, **kwargs):
        return None

    def absent(paths, progress=None):
        return None

    callback = lambda *args: None  # noqa: E731 - solo identita', non viene chiamata
    assert gui.phase_kwargs(declared, callback) == {"phase": callback}
    assert gui.phase_kwargs(variadic, callback) == {"phase": callback}
    assert gui.phase_kwargs(absent, callback) == {}
