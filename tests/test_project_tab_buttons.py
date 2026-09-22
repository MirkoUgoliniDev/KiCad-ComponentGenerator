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

#: Righe VERE prese dai diari di due corse registrate
#: (`generated/*/analisi-ai.log`), che il filtro per-elemento cancellava dalla
#: modale pur non essendo rendiconti di un elemento. Sono tenute integre --
#: accorciarle toglierebbe proprio cio' che le faceva classificare male --
#: tranne dove indicato.
REAL_LINES_THAT_MUST_REACH_THE_MODAL = [
    # Unica riga che dice che la fase AI e' finita; emessa mentre la modale e'
    # ancora aperta. La coda e' troncata: conta la testa con la fine di frase.
    ("Estrazione completata. Confidenza: low. CONTATTI CASTELLATED: il modulo "
     "(dev-kit ESP32-S3-DEV-KIT-NXRX) ha due file di piazzole a mezzo foro sui "
     "bordi lunghi; drill_mm=0, pad_diameter_mm usato come LARGHEZZA piazzola "
     "(~1.6mm stimata), lunghezza piazzola ~2.0mm stimata, semi-foro ~1.0-1.1mm: "
     "NESSUNA di queste quote e' presente nei documenti forniti."),
    # Avviso di spesa.
    ("Credito OpenRouter: 14.05 USD residui (saldo dell'account); "
     "account 60.95/75.00 USD usati; chiave 15.86 USD."),
    # Fallimento parziale dell'inventario globale, nelle due lingue in cui esce.
    ("Inventario globale parziale: candidate 91: invalid status/retained parent/"
     "source link; component J2: source proposals [70] outside retained envelope; "
     "links removed, visible body and offending original kept."),
    ("Global inventory FAILED/PARTIAL: candidate 91: invalid status/retained "
     "parent/source link; component SW2: source proposals [89] outside retained "
     "envelope; links removed."),
    # Riepilogo del rilevatore locale, che l'utente ha chiesto di poter vedere.
    ("YOLO local: 64 operating-threshold candidates; returned 80 generic "
     "hypotheses; heights are placeholders."),
    ("YOLO local: 30 views; confidence 0.25; audit floor 0.05; "
     "class-agnostic NMS IoU 0.5; CPU inference."),
]


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
        # Si CONTANO i collegamenti PRIMA di toccarli: `disconnect()` senza
        # argomenti li toglie tutti, quindi contare dopo non direbbe nulla e la
        # versione precedente di questa prova non poteva accorgersi di un
        # comando collegato due volte -- il difetto tipico di un pulsante
        # ricostruito senza togliere il precedente.
        assert button.receivers(button.clicked) == 1, "un comando, un collegamento"
        # Il collegamento vero punta al metodo originale (Qt tiene il bound
        # method creato al momento della connessione, che monkeypatch non
        # raggiunge): per osservare il clic senza aprire la revisione vera si
        # sostituisce il collegamento con il sensore.
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
    # Campo macchina scritto come coppia: stessa natura del token di stato.
    ("FreeCAD: KICADGEN_COMPONENT_OK:Cmp9_R1__resistor_; parts=1", True),
    ("R_0603: no_candidate", False),  # senza elenco di campi non e' un rendiconto
    ("Generazione del footprint KiCad…", False),
    ("Profilo AI: Modulo castellato.", False),
    ("Proposte salvate in photo-review/x.json: 41 corpi; si riapre senza AI.", False),
])
def test_per_item_detail_recognises_the_shape_not_the_sentence(line, per_item):
    """@brief La regola e' strutturale: elenco di campi dopo i due punti, non una frase nota."""
    assert gui.per_item_detail(line) is per_item


@pytest.mark.parametrize("line", REAL_LINES_THAT_MUST_REACH_THE_MODAL)
def test_real_lines_with_three_clauses_are_not_per_item_bookkeeping(line):
    """@brief Negativo, su dati reali: tre punti e virgola non fanno un rendiconto.

    Il criterio precedente era il solo conteggio delle clausole, e queste sei
    righe -- la conferma di fine estrazione, l'avviso di credito, i due
    inventari globali parziali e i due riepiloghi del rilevatore locale --
    finivano tutte cancellate dalla modale. Sono i testi originali dei diari di
    due corse registrate, non esemplari costruiti.
    """
    assert gui.per_item_detail(line) is False


def test_per_item_detail_needs_a_machine_field_not_three_clauses():
    """@brief Positivo e negativo sulla stessa forma: decide il campo, non il numero.

    Due righe con identica struttura «etichetta: clausola; clausola; clausola»:
    quella che porta un campo macchina e' un rendiconto per-elemento, quella
    fatta di sola prosa non lo e'. Il conteggio delle clausole non le
    distinguerebbe, ed e' questa la ragione per cui e' stato tolto.
    """
    field = "QFN-24: 2 dimensional candidates; review_required; identity not verified."
    prose = "QFN-24: 2 candidati dimensionali; da controllare; identita' non verificata."
    assert gui.per_item_detail(field) is True
    assert gui.per_item_detail(prose) is False


def test_the_label_does_not_change_the_verdict_of_the_same_report():
    """@brief La stessa riga con e senza etichetta deve avere lo stesso verdetto.

    L'abbinamento emette anche la forma senza etichetta, quando il package non
    e' noto. Prima l'assenza di testa faceva uscire subito con False: la riga
    etichettata spariva dalla modale e la sua gemella senza etichetta no.
    """
    labelled = "QFN-24: 4 dimensional candidates; review_required; identity not verified."
    unlabelled = ": 4 dimensional candidates; review_required; identity not verified."
    assert gui.per_item_detail(labelled) is gui.per_item_detail(unlabelled) is True


def test_a_sentence_that_happens_to_contain_a_colon_is_not_a_report():
    """@brief Negativo: la testa si legge dall'ULTIMO «: », e una frase non e' un'etichetta.

    Positivo per contrasto: la stessa coda, preceduta da un'etichetta vera,
    resta un rendiconto.
    """
    tail = "3 dimensional candidates; review_required; identity not verified."
    assert gui.per_item_detail(f"Estrazione completata. Confidenza: low. QFN-24: {tail}") is False
    assert gui.per_item_detail(f"QFN-24: {tail}") is True


def test_modal_does_not_echo_the_model_already_in_the_header(window):  # noqa: F811
    """@brief Negativo: una riga che E' soltanto il dettaglio dell'intestazione non sale.

    Positivo: una riga che dice anche cos'altro sta succedendo sale, anche se
    finisce col nome del modello.

    Contratto corretto rispetto a prima. La versione precedente scartava
    qualunque «testa: <dettaglio>», cioe' buttava via la testa, che e' la
    meta' informativa: «Fallita la chiamata a: anthropic/claude-opus-5.»
    spariva dall'unico posto in cui la si sarebbe letta. Distinguere
    un'etichetta da un evento richiederebbe di sapere se la testa contiene un
    verbo, cosa che nessuna regola ortografica fa senza tararsi sull'esemplare
    osservato; si scarta quindi solo l'eco esatta. Sulla riga vera che aveva
    motivato la regola («Modello AI unico: <modello>.») non si perde nulla:
    viene scritta PRIMA di `analysis_overlay.start()`, che azzera
    l'intestazione, quindi il filtro non la vedeva comunque mai scattare.
    """
    window._on_phase("llm", "anthropic/claude-opus-5", 0, 0)
    window._on_ai_progress(PHASE_LINE)
    window._on_ai_progress("anthropic/claude-opus-5.")
    assert window.analysis_overlay.status.text() == PHASE_LINE, "l'intestazione lo dice gia'"
    assert "anthropic/claude-opus-5." in window.ai_progress_text.toPlainText(), "nel log resta"
    for informative in ("Fallita la chiamata a: anthropic/claude-opus-5.",
                        "Invio a anthropic/claude-opus-5 di 3 file per layout, quote e pinout."):
        window._on_ai_progress(informative)
        assert window.analysis_overlay.status.text() == informative


def test_a_header_detail_ending_in_a_full_stop_still_silences_its_echo(window):  # noqa: F811
    """@brief Negativo: il punto finale si toglie da entrambi i lati, non solo dalla riga.

    Prima il punto veniva tolto solo al messaggio: un dettaglio che finisce con
    un punto rendeva il confronto sempre falso, cioe' spegneva il filtro in
    silenzio invece di dirlo.
    """
    window._on_phase("llm", "rilevatore locale ONNX.", 0, 0)
    window._on_ai_progress(PHASE_LINE)
    window._on_ai_progress("rilevatore locale ONNX")
    assert window.analysis_overlay.status.text() == PHASE_LINE


class _ElidingLabel:
    """@brief Etichetta che restituisce il testo accorciato, come farebbe un elide."""

    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class _OverlayWithStoredDetail:
    """@brief Modale che espone il dettaglio memorizzato accanto all'etichetta dipinta."""

    def __init__(self, painted, detail):
        self.tool = _ElidingLabel(painted)
        self.tool_detail = detail


class _OverlayWithoutStoredDetail:
    """@brief Modale della versione precedente: si puo' solo leggere l'etichetta."""

    def __init__(self, painted):
        self.tool = _ElidingLabel(painted)


def test_the_header_detail_is_read_from_the_overlay_state_not_from_the_label(window):  # noqa: F811
    """@brief Il confronto usa il valore memorizzato dalla modale, non il testo dipinto.

    Positivo: con un dettaglio memorizzato che NON compare per intero
    nell'etichetta, l'eco viene comunque riconosciuta -- l'etichetta puo'
    essere mandata a capo o accorciata con i puntini senza che il filtro
    cominci a mentire.
    Negativo: una modale che non espone l'attributo fa tornare all'analisi
    dell'etichetta, cosi' le due modifiche possono atterrare in qualunque
    ordine.
    """
    real_overlay = window.analysis_overlay
    try:
        elided = "Modello AI (remoto, a consumo) · anthropic/cla…"
        window.analysis_overlay = _OverlayWithStoredDetail(elided, "anthropic/claude-opus-5")
        assert window._overlay_header_detail() == "anthropic/claude-opus-5"
        assert window._restates_overlay_header("anthropic/claude-opus-5.") is True

        window.analysis_overlay = _OverlayWithoutStoredDetail(
            "Modello AI (remoto, a consumo) · anthropic/claude-opus-5")
        assert window._overlay_header_detail() == "anthropic/claude-opus-5", "ripiego sull'etichetta"
        assert window._restates_overlay_header("anthropic/claude-opus-5.") is True
    finally:
        # La modale vera deve tornare al suo posto: la chiusura della finestra
        # la usa, e un doppio senza widget farebbe fallire lo smontaggio.
        window.analysis_overlay = real_overlay


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


def test_an_opaque_wrapper_signature_does_not_get_phase():
    """@brief Negativo: una firma di soli `*args, **kwargs` non dimostra nulla, quindi niente `phase`.

    Un decoratore scritto senza `functools.wraps` espone la firma del wrapper:
    `**kwargs` c'e', ma dietro c'e' una funzione che `phase` non lo accetta, ed
    e' esattamente il TypeError che questo aiutante esiste per evitare. Qui si
    riproduce quel decoratore e si verifica che la chiamata riesca.

    Positivo per contrasto: lo stesso decoratore con `functools.wraps` rende di
    nuovo visibile la firma vera, e l'annuncio della fase torna a passare
    quando la funzione lo dichiara davvero.
    """
    import functools

    def service(paths, progress=None):
        return "ok"

    def with_phase(paths, *, phase=None, progress=None):
        return "ok"

    def sloppy(function):
        def wrapper(*a, **k):
            return function(*a, **k)
        return wrapper

    def careful(function):
        @functools.wraps(function)
        def wrapper(*a, **k):
            return function(*a, **k)
        return wrapper

    callback = lambda *args: None  # noqa: E731 - solo identita'
    opaque = sloppy(service)
    assert gui.phase_kwargs(opaque, callback) == {}
    # La prova che il rischio era reale: passare `phase=` qui esploderebbe.
    assert opaque([], **gui.phase_kwargs(opaque, callback)) == "ok"
    with pytest.raises(TypeError):
        opaque([], phase=callback)

    assert gui.phase_kwargs(careful(with_phase), callback) == {"phase": callback}
    assert gui.phase_kwargs(careful(service), callback) == {}


# ── L'ordine delle fasi annunciate durante la generazione ─────────────────

def _run_worker_phases(monkeypatch, worker):
    """@brief Esegue run() con il CAD sostituito e restituisce le fasi annunciate.

    @param monkeypatch Fixture pytest, per sostituire `_generate_3d`.
    @param worker Worker gia' costruito, con `params`/`body` pronti.
    @return Lista di (strumento, dettaglio) nell'ordine in cui sono usciti.
    """
    def fake_generate_3d(params, body, project_dir, fillets, face_texts,
                         progress=None, model_progress=None):
        # Ordine reale del servizio: prima l'abbinamento dei modelli di KiCad,
        # poi la costruzione del solido.
        if model_progress is not None:
            model_progress(1, 41)
        if progress is not None:
            progress("FreeCAD: KICADGEN_COMPONENT_START:1/41")
        return {"step_path": "out.step", "warnings": [], "notes": []}

    monkeypatch.setattr(gui, "_generate_3d", fake_generate_3d)
    phases = []
    worker.phase.connect(lambda tool, detail, done, total: phases.append((tool, detail)))
    worker.progress.connect(lambda message: None)
    worker.run()
    return phases


def test_generate_worker_never_shows_freecad_during_the_kicad_matching(monkeypatch, tmp_path):
    """@brief Negativo: la modale non deve accendere FreeCAD prima che il solido cominci.

    Positivo: appena `_generate_3d` annuncia un componente si passa a FreeCAD.

    La prima cosa che fa `_generate_3d` e' l'abbinamento dei modelli di KiCad:
    annunciare `freecad` prima di chiamarlo mostrava il cubo del CAD proprio
    durante la fase che la correzione vuole attribuita a KiCad, per poi tornare
    indietro. La prova guarda la SEQUENZA, non il singolo annuncio.
    """
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.cad3d.body_params import Body3DParams

    class _Footprint:
        def save(self, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text("(module)", encoding="utf-8")

    monkeypatch.setattr(gui, "build_pin_header", lambda params: _Footprint())
    worker = gui.GenerateWorker(
        PinHeaderParams(name="Probe", rows=1, cols=2, pitch=2.54, row_spacing=2.54,
                        pad_diameter=1.7, drill=1.0),
        tmp_path,
        body=Body3DParams(length_mm=5, width_mm=5, height_mm=2,
                          offset_x_mm=0, offset_y_mm=0,
                          pin_diameter_mm=1, pin_length_mm=3),
    )
    phases = _run_worker_phases(monkeypatch, worker)
    tools = [tool for tool, _ in phases]
    assert tools[0] == "kicad", "si comincia dal footprint 2D, che e' KiCad"
    first_freecad = tools.index("freecad")
    assert all(tool == "kicad" for tool in tools[:first_freecad]), (
        "nessun annuncio di FreeCAD prima che il solido cominci: " + repr(phases))
    assert phases[first_freecad] == ("freecad", "modello 3D")
    assert ("kicad", "abbinamento modelli KiCad") in phases[:first_freecad]


def test_cad3d_worker_announces_the_matching_before_the_solid(monkeypatch, tmp_path):
    """@brief Stessa regola per la rigenerazione del solo modello 3D.

    Negativo: il primo annuncio non e' FreeCAD. Positivo: FreeCAD arriva quando
    il servizio dichiara un componente in costruzione.
    """
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.cad3d.body_params import Body3DParams

    worker = gui.Cad3DWorker(
        PinHeaderParams(name="Probe", rows=1, cols=2, pitch=2.54, row_spacing=2.54,
                        pad_diameter=1.7, drill=1.0),
        Body3DParams(length_mm=5, width_mm=5, height_mm=2, offset_x_mm=0, offset_y_mm=0,
                     pin_diameter_mm=1, pin_length_mm=3),
        tmp_path, [], [])
    phases = _run_worker_phases(monkeypatch, worker)
    assert phases[0] == ("kicad", "abbinamento modelli KiCad")
    assert ("freecad", "modello 3D") in phases
    assert phases.index(("freecad", "modello 3D")) > 0


# ── La radice della libreria decide gli stessi file dei pulsanti ──────────

def test_changing_the_library_root_refreshes_the_ai_log_button(window, tmp_path):  # noqa: F811
    """@brief Positivo: spostando la radice su una cartella col diario, «Log AI» compare.

    Negativo: spostandola su una cartella senza diario sparisce. La radice
    entra in `_project_dir()` esattamente come il nome del componente, quindi
    deve ricalcolare gli stessi pulsanti; prima solo il nome lo faceva.
    """
    _clear_log(window)
    with_log = tmp_path / "con-diario"
    (with_log / window._component_name()).mkdir(parents=True)
    (with_log / window._component_name() / gui.MainWindow.LOG_FILENAME).write_text(
        "[t] corsa precedente\n", encoding="utf-8")
    without_log = tmp_path / "senza-diario"
    without_log.mkdir()

    window.field_library_root.setText(str(with_log))
    assert _own_visible(window.btn_ai_log), "il diario c'e' in questa radice"
    window.field_library_root.setText(str(without_log))
    assert not _own_visible(window.btn_ai_log), "altra radice, nessun diario"


def test_changing_the_library_root_refreshes_the_reopen_review_button(window, tmp_path):  # noqa: F811
    """@brief Positivo/negativo sulle proposte salvate: la radice decide dove si cercano.

    Il pacchetto `photo-review/` sta sotto la cartella del componente, che
    dipende dalla radice: cambiandola il comando deve accendersi o spegnersi
    subito, non al primo clic.
    """
    window._photo_png = b"photo"
    with_bundle = tmp_path / "con-pacchetto"
    empty = tmp_path / "senza-pacchetto"
    empty.mkdir()
    window.field_library_root.setText(str(with_bundle))
    written = window._write_review_bundle(
        [{"_id": 1, "_box": [0.1, 0.1, 0.4, 0.4]}], b"photo", 10.0, 5.0)
    assert written is not None, "presupposto: il pacchetto e' stato scritto in questa radice"
    window.field_library_root.setText(str(empty))
    assert not _own_visible(window.btn_reopen_review), "in questa radice non c'e' nulla da riaprire"
    window.field_library_root.setText(str(with_bundle))
    assert _own_visible(window.btn_reopen_review), "il pacchetto e' qui: il comando si riaccende"


# ── «Log AI» guarda tutto il log, non solo meta' ──────────────────────────

def test_ai_log_button_counts_the_elements_list_too(window, tmp_path):  # noqa: F811
    """@brief Positivo: righe solo nell'elenco degli elementi bastano a far comparire «Log AI».

    `_append_log` manda le righe che cominciano con `#<numero>` SOLTANTO in
    `ai_elements_list`. Con una radice che non e' una cartella il diario non si
    puo' scrivere, e quelle righe restavano l'unico contenuto della finestra
    del log mentre il pulsante si nascondeva.

    Negativo: senza nessuna delle due il pulsante resta invisibile.
    """
    regular_file = tmp_path / "non-e-una-cartella"
    regular_file.write_text("x", encoding="utf-8")
    window.field_library_root.setText(str(regular_file))
    _clear_log(window)
    window.ai_elements_list.clear()
    window._refresh_ai_log_button()
    assert not _own_visible(window.btn_ai_log)
    assert window._ai_log_available() is False

    window._on_ai_progress("#12 QFN-24: corpo riconosciuto")
    assert not window.ai_progress_text.toPlainText().strip(), "questa riga va solo nell'elenco"
    assert window.ai_elements_list.count() == 1
    assert window._ai_log_available() is True
    assert _own_visible(window.btn_ai_log)


# ── Le tre copie nella scheda Modello 3D nascono uguali ───────────────────

def test_the_three_3d_mirrors_are_built_from_the_same_helper(window):  # noqa: F811
    """@brief Positivo: testo, suggerimento, stato e visibilita' copiati per tutte e tre.

    Prima due copie prendevano solo `setEnabled(...)` e la terza anche
    `isVisibleTo(...)`: lo stesso comando nasceva in tre stati diversi.
    """
    pairs = [(window.btn_reopen_review_3d, window.btn_reopen_review),
             (window.btn_relaunch_inventory_3d, window.btn_relaunch_inventory),
             (window.btn_edit_component_review_3d, window.btn_edit_component_review)]
    assert [pair for pair in window._project_action_mirrors] == pairs
    window._photo_png = b"photo"
    for enabled in (True, False):
        window._set_review_buttons_enabled(enabled)
        for mirror, source in pairs:
            assert mirror.text() == source.text()
            assert mirror.toolTip() == source.toolTip()
            assert _own_visible(mirror) is _own_visible(source), (
                f"{mirror.text()!r}: visibilita' propria allineata all'originale")


def test_a_mirror_is_enabled_state_is_also_gated_by_its_tab(window):  # noqa: F811
    """@brief Il `@note` di `_set_action_available` vale per cio' che il metodo imposta.

    Negativo: finche' la scheda Modello 3D e' spenta, `isEnabled()` di una copia
    e' falso anche se il comando e' disponibile -- e non e' un difetto, e' l'AND
    che Qt fa con lo stato del genitore. Positivo: la visibilita' propria resta
    quella dell'originale, ed e' quella che i controlli devono leggere.
    """
    window._photo_png = b"photo"
    window._set_review_buttons_enabled(True)
    # Si riaccende tutta la catena, come dopo una generazione, e poi si spegne
    # SOLO la scheda: e' quello il gate di cui parla il `@note`.
    ancestors = []
    widget = window.btn_reopen_review_3d
    while widget.parentWidget() is not None:
        widget = widget.parentWidget()
        widget.setEnabled(True)
        ancestors.append(widget)
    page = next(w for w in ancestors if window.tabs.indexOf(w) >= 0)
    page.setEnabled(False)
    for mirror in (window.btn_reopen_review_3d, window.btn_relaunch_inventory_3d,
                   window.btn_edit_component_review_3d):
        assert _own_visible(mirror), "visibile: il comando e' disponibile"
        assert not mirror.isEnabled(), "spento perche' la scheda lo e'"
    page.setEnabled(True)
    for mirror in (window.btn_reopen_review_3d, window.btn_relaunch_inventory_3d,
                   window.btn_edit_component_review_3d):
        assert mirror.isEnabled()
