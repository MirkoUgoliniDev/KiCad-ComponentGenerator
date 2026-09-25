"""@brief La via d'uscita dalla modale: che cosa «Interrompi» fa davvero.

La modale copre la finestra e spegne le schede: finche' non c'e' stato un
pulsante per fermarsi, l'unico modo di uscire da una generazione lunga era
chiudere il programma. Qui si verifica che il pulsante ci sia, che chieda
conferma e che mantenga esattamente cio' che promette, che e' diverso nei due
casi:

* CAD -- il processo figlio (freecadcmd) viene ucciso davvero, e la finestra
  rientra dalla via d'errore che esisteva gia';
* AI -- la richiesta HTTP gia' partita NON si puo' annullare (urllib
  bloccante): la corsa si abbandona, la finestra torna subito e il risultato
  tardivo viene scartato.

Nessun test pretende FreeCAD installato: al suo posto gira un figlio Python
che stampa righe come farebbe freecadcmd.
"""

import subprocess
import sys
import textwrap
import time

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QListWidgetItem, QMessageBox

from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.process_log import ProcessCancelled, run_logged
from kicadgen.services.datasheet_extractor import ExtractedParams


# ── Aiutanti: un finto freecadcmd e un'attesa che fa girare la GUI ────────

#: Figlio chiacchierone: stampa una riga ogni `interval` secondi e SOLO alla
#: fine scrive il file di completamento. E' quel file a dimostrare l'uccisione:
#: se dopo l'interruzione non esiste, il figlio non e' arrivato in fondo.
_CHATTY_CHILD = """
import sys, time
from pathlib import Path
for step in range({steps}):
    print("KICADGEN_COMPONENT_START:%d/{steps}" % (step + 1), flush=True)
    time.sleep({interval})
Path(r"{flag}").write_text("completato", encoding="utf-8")
print("KICADGEN_STEP_OK", flush=True)
"""


def _child_script(tmp_path, flag, steps=3000, interval=0.02):
    """@brief Scrive il finto freecadcmd e restituisce (script, file di fine)."""
    script = tmp_path / "fake_freecad_child.py"
    script.write_text(_CHATTY_CHILD.format(steps=steps, interval=interval, flag=flag),
                      encoding="utf-8")
    return script


def _arm_fake_freecad(monkeypatch, tmp_path, *, steps=3000, interval=0.02):
    """@brief Mette al posto di freecadcmd un figlio Python che stampa e dura.

    @note Di norma dura un minuto (3000 x 0.02 s): ben oltre qualunque attesa
        dei test, cosi' "non e' arrivato in fondo" dipende dall'uccisione e
        non dalla velocita' della macchina. Ogni test lo termina comunque.
    @return Il percorso del file che il figlio scrive SOLO se arriva in fondo.
    """
    flag = tmp_path / "child-completed.flag"
    script = _child_script(tmp_path, flag, steps=steps, interval=interval)
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: sys.executable)
    monkeypatch.setattr(step_generator, "_SCRIPT_PATH", script)
    return flag


def _pump(predicate, timeout=20.0):
    """@brief Fa girare la coda Qt finche' la condizione non e' vera.

    @return True se la condizione si e' avverata entro il tempo concesso.
    @note I worker sono QThread veri: senza far girare la coda i segnali non
        arrivano mai e il test misurerebbe soltanto il proprio sonno.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return predicate()


def _prepare_3d(window):  # noqa: F811
    """@brief Campi minimi perche' «Rigenera modello 3D» abbia un solido da fare."""
    window.group_3d.setChecked(True)
    window.field_body_length.setValue(10.0)
    window.field_body_width.setValue(8.0)
    window.field_body_height.setValue(2.0)


def _say_yes(monkeypatch, answers):
    """@brief Conferma ogni domanda, contando quante volte e' stata posta."""
    def question(*args, **kwargs):
        answers.append(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)


def _say_no(monkeypatch, answers):
    """@brief Rifiuta ogni domanda, contando quante volte e' stata posta."""
    def question(*args, **kwargs):
        answers.append(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", question)


def _finish_cad_worker(window):  # noqa: F811
    """@brief Non lascia figli vivi dietro a un test: ordina la fine e aspetta."""
    worker = window._cad3d_worker or window._worker
    if worker is not None and worker.isRunning():
        worker.request_abort()
        worker.wait(20000)
    QApplication.processEvents()


# ── run_logged: la rinuncia e il silenzio sono due cose diverse ───────────

def _python_child(script):
    return [sys.executable, "-c", textwrap.dedent(script)]


def test_run_logged_kills_the_child_on_request_and_says_so(tmp_path):
    """@brief Positivo: chi chiede la fine la ottiene, ed e' scritto come tale."""
    import threading

    cancel = threading.Event()
    log = tmp_path / "freecad.log"
    lines = []

    def progress(line):
        lines.append(line)
        # Si rinuncia appena il figlio da' segno di vita: cosi' l'uccisione
        # avviene a lavoro avviato, non prima di partire.
        cancel.set()

    with pytest.raises(ProcessCancelled):
        run_logged(_python_child("""
            import time
            for i in range(500):
                print('vivo', i, flush=True)
                time.sleep(0.02)
            print('FINITO', flush=True)
        """), log, progress=progress, timeout=30, cancel=cancel)

    text = log.read_text(encoding="utf-8")
    assert "interrotto su richiesta dell'utente" in text
    assert "cancelled=True" in text
    # Il verso negativo che conta: una richiesta dell'utente non deve MAI
    # essere raccontata come un blocco diagnosticato dal watchdog.
    assert "(nessun output per" not in text
    assert "FINITO" not in text, "il figlio non doveva arrivare in fondo"


def test_run_logged_without_cancellation_still_runs_to_completion(tmp_path):
    """@brief Negativo: senza richiesta nessuno tocca il figlio."""
    import threading

    cancel = threading.Event()
    log = tmp_path / "freecad.log"
    result = run_logged(_python_child("""
        print('prima', flush=True)
        print('FINITO', flush=True)
    """), log, progress=lambda line: None, timeout=30, cancel=cancel)

    assert result.returncode == 0
    assert "FINITO" in result.stdout
    text = log.read_text(encoding="utf-8")
    assert "cancelled=False" in text
    assert "interrotto su richiesta" not in text
    assert "(nessun output per" not in text


def test_run_logged_cancelled_before_the_first_line_never_lets_it_work(tmp_path):
    """@brief Positivo: rinuncia gia' chiesta = figlio ucciso al primo respiro."""
    import threading

    cancel = threading.Event()
    cancel.set()
    flag = tmp_path / "done.flag"
    log = tmp_path / "freecad.log"
    with pytest.raises(ProcessCancelled):
        run_logged(_python_child(f"""
            import time
            from pathlib import Path
            time.sleep(0.5)
            Path(r'{flag}').write_text('x', encoding='utf-8')
        """), log, progress=lambda line: None, timeout=30, cancel=cancel)
    # Si guarda DOPO il momento in cui un figlio sopravvissuto avrebbe scritto
    # il file (0.5 s), con margine: controllare subito, con un figlio da 3 s,
    # passava anche se l'uccisione non fosse avvenuta.
    time.sleep(1.5)
    assert not flag.exists(), "il figlio ha continuato a lavorare dopo la rinuncia"


def test_the_silence_watchdog_keeps_its_own_distinct_report(tmp_path):
    """@brief Negativo: un blocco vero resta un blocco, non un'interruzione.

    E' il verso opposto del test positivo: le due uccisioni passano per la
    stessa macchina, quindi vanno distinte da cio' che scrivono e da cio' che
    sollevano, o la diagnosi del watchdog sparirebbe dentro l'interruzione.
    """
    import threading

    log = tmp_path / "freecad.log"
    with pytest.raises(subprocess.TimeoutExpired):
        run_logged(_python_child("import time; time.sleep(30)"), log,
                   progress=lambda line: None, timeout=0.5,
                   cancel=threading.Event())
    text = log.read_text(encoding="utf-8")
    assert "(nessun output per 0.5 s)" in text
    assert "cancelled=False" in text
    assert "interrotto su richiesta" not in text


# ── step_generator: la rinuncia arrivata prima non avvia nemmeno il figlio ─

def test_a_cancellation_asked_during_the_matching_skips_the_freecad_launch(tmp_path):
    """@brief Positivo: chiesta la fine, il solido non si comincia nemmeno."""
    import threading

    from kicadgen.cad3d.body_params import Body3DParams

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(step_generator.Cad3DError) as error:
        step_generator._run_freecad(
            "Test", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
            freecadcmd_path="/percorso/inesistente/freecadcmd",
            progress=lambda _: None, cancel=cancel)
    assert step_generator.CAD_ABORT_MESSAGE in str(error.value)


def test_without_a_cancellation_the_same_call_does_try_to_launch(tmp_path):
    """@brief Negativo che rende utile il precedente: senza rinuncia si parte.

    Con lo stesso eseguibile inesistente l'errore e' quello del lancio, non
    quello dell'interruzione: e' la prova che a fermare la corsa e' stato il
    canale e non il percorso sbagliato.
    """
    from kicadgen.cad3d.body_params import Body3DParams

    with pytest.raises((OSError, step_generator.Cad3DError)) as error:
        step_generator._run_freecad(
            "Test", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
            freecadcmd_path="/percorso/inesistente/freecadcmd",
            progress=lambda _: None)
    assert step_generator.CAD_ABORT_MESSAGE not in str(error.value)


# ── Il pulsante: c'e' solo quando c'e' qualcosa da fermare ────────────────

def test_the_abort_button_is_absent_while_nothing_is_running(window):  # noqa: F811
    """@brief Negativo: a riposo il comando non si vede e non si puo' premere."""
    overlay = window.analysis_overlay
    assert not overlay.is_abortable()
    assert not overlay.abort_button.isEnabled()
    assert not overlay.abort_button.isVisibleTo(overlay.button_row)
    # Anche dopo un'operazione: la fine toglie il pulsante insieme alla modale.
    overlay.start("Generazione modello 3D", "Avvio di FreeCAD…", abortable=True)
    assert overlay.abort_button.isEnabled()
    assert overlay.abort_button.isVisibleTo(overlay.button_row)
    overlay.stop()
    assert not overlay.is_abortable()
    assert not overlay.abort_button.isEnabled()


def test_an_overlay_started_without_declaring_it_offers_no_abort(window):  # noqa: F811
    """@brief Negativo: chi non sa fermare il lavoro non promette di fermarlo."""
    overlay = window.analysis_overlay
    overlay.start("Analisi AI in corso", "Preparazione dei documenti…")
    try:
        assert not overlay.is_abortable()
        assert not overlay.abort_button.isEnabled()
    finally:
        overlay.stop()


def test_the_abort_button_sits_next_to_the_ai_log_button(window):  # noqa: F811
    """@brief Dove l'utente l'ha chiesto: subito a destra di «Apri log AI»."""
    overlay = window.analysis_overlay
    row = overlay.button_row.layout()
    positions = {row.itemAt(i).widget(): i for i in range(row.count())
                 if row.itemAt(i).widget() is not None}
    assert positions[overlay.abort_button] == positions[overlay.log_button] + 1
    assert overlay.abort_button.objectName() == "analysisAbortButton"


# ── Interruzione della generazione 3D: il figlio muore davvero ────────────

def test_aborting_a_generation_kills_the_child_and_gives_the_window_back(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: il figlio non arriva in fondo e la finestra torna usabile."""
    flag = _arm_fake_freecad(monkeypatch, tmp_path)
    answers = []
    _say_yes(monkeypatch, answers)
    _prepare_3d(window)
    window._regenerate_3d()
    assert window._cad3d_worker is not None
    assert _pump(lambda: "KICADGEN_COMPONENT_START" in window.log_text.toPlainText()), (
        "il finto freecadcmd non ha nemmeno cominciato a stampare")
    assert window.analysis_overlay.abort_button.isEnabled()

    window.analysis_overlay.abort_button.click()

    assert len(answers) == 1, "una domanda sola, non una a ogni passaggio"
    assert _pump(lambda: window._cad3d_worker is not None
                 and not window._cad3d_worker.isRunning())
    QApplication.processEvents()

    assert not flag.exists(), "il processo figlio e' arrivato in fondo: non e' stato ucciso"
    assert window.analysis_overlay.isHidden()
    assert window.tabs.isEnabled()
    assert not window.analysis_overlay.abort_button.isEnabled()
    assert window.btn_regen_3d.isEnabledTo(window.btn_regen_3d.parentWidget())
    # Il comando e' un'icona senza testo (scelta dell'utente, 2026-09-23): il
    # ritorno a riposo si legge nel nome accessibile, non piu' in `text()`.
    assert "Rigenera" in window.btn_regen_3d.accessibleName()
    assert not window.btn_regen_3d.property("workflow_busy")
    log = window.log_text.toPlainText()
    assert "INTERRUZIONE chiesta dall'utente durante" in log
    assert "Rigenerazione del modello 3D" in log, "il log deve dire a che punto si era"
    assert "INTERROTTO 3D" in log
    assert "ERRORE 3D" not in log, "una scelta dell'utente non e' un guasto"
    freecad_log = (window._project_dir() / "freecad.log").read_text(encoding="utf-8")
    assert "interrotto su richiesta dell'utente" in freecad_log
    assert "(nessun output per" not in freecad_log


def test_declining_the_confirmation_leaves_everything_running(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negativo: chi risponde di no non deve perdere niente."""
    _arm_fake_freecad(monkeypatch, tmp_path)
    answers = []
    _say_no(monkeypatch, answers)
    _prepare_3d(window)
    window._regenerate_3d()
    assert _pump(lambda: "KICADGEN_COMPONENT_START" in window.log_text.toPlainText())

    window.analysis_overlay.abort_button.click()
    QApplication.processEvents()

    assert len(answers) == 1
    assert window._cad3d_worker.isRunning(), "il lavoro doveva continuare"
    assert not window._cad3d_worker.cancel.is_set(), "nessun ordine doveva partire"
    assert not window._abort_pending
    assert not window.analysis_overlay.isHidden()
    assert not window.tabs.isEnabled()
    assert window.analysis_overlay.abort_button.isEnabled(), (
        "il comando deve restare per ripensarci")
    assert "INTERRUZIONE" not in window.log_text.toPlainText()
    _finish_cad_worker(window)


def test_pressing_abort_twice_asks_once_and_sends_one_order(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negativo: due clic non fanno due interruzioni ne' due domande."""
    _arm_fake_freecad(monkeypatch, tmp_path)
    answers = []
    _say_yes(monkeypatch, answers)
    aborts = []
    original = gui.Cad3DWorker.request_abort

    def counting_abort(self):
        aborts.append(self)
        original(self)

    monkeypatch.setattr(gui.Cad3DWorker, "request_abort", counting_abort)
    _prepare_3d(window)
    window._regenerate_3d()
    assert _pump(lambda: "KICADGEN_COMPONENT_START" in window.log_text.toPlainText())

    window.analysis_overlay.abort_button.click()
    window.analysis_overlay.abort_button.click()
    window.analysis_overlay.abort_requested.emit()
    QApplication.processEvents()

    assert len(answers) == 1
    assert len(aborts) == 1
    assert _pump(lambda: not window._cad3d_worker.isRunning())
    QApplication.processEvents()
    assert window.analysis_overlay.isHidden() and window.tabs.isEnabled()
    assert not window._abort_pending


def test_abort_racing_with_a_normal_completion_does_nothing_and_does_not_crash(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negativo: premere quando il lavoro e' appena finito non rompe nulla."""
    _arm_fake_freecad(monkeypatch, tmp_path, steps=1, interval=0.0)
    answers = []
    _say_yes(monkeypatch, answers)
    _prepare_3d(window)
    window._regenerate_3d()
    assert _pump(lambda: window._cad3d_worker is not None
                 and not window._cad3d_worker.isRunning())
    QApplication.processEvents()
    assert window.analysis_overlay.isHidden()

    window.analysis_overlay.abort_requested.emit()
    QApplication.processEvents()

    assert answers == [], "niente da fermare: non si chiede nemmeno conferma"
    assert not window.analysis_overlay.is_abortable()
    assert window.tabs.isEnabled()
    assert not window._abort_pending


def test_a_run_that_ends_while_the_question_is_open_is_not_ordered_around(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negativo: la domanda gira un event loop, e li' il mondo cambia.

    La conferma viene data DOPO che il lavoro e' finito da solo: l'ordine non
    deve partire (non c'e' piu' un destinatario) e la finestra non deve essere
    toccata una seconda volta.
    """
    _arm_fake_freecad(monkeypatch, tmp_path, steps=6, interval=0.05)
    answers = []

    def slow_question(*args, **kwargs):
        answers.append(args[2] if len(args) > 2 else "")
        _pump(lambda: window._cad3d_worker is not None
              and not window._cad3d_worker.isRunning())
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", slow_question)
    _prepare_3d(window)
    window._regenerate_3d()
    assert _pump(lambda: "KICADGEN_COMPONENT_START" in window.log_text.toPlainText())

    window.analysis_overlay.abort_button.click()
    QApplication.processEvents()

    assert len(answers) == 1
    assert not window._cad3d_worker.cancel.is_set()
    assert not window._abort_pending
    assert window.tabs.isEnabled()
    log = window.log_text.toPlainText()
    assert "era già terminata" in log
    assert "INTERRUZIONE chiesta dall'utente durante" not in log


# ── Interruzione dell'analisi AI: si abbandona, non si annulla ────────────

def _start_blocking_extraction(window, monkeypatch, released, result):  # noqa: F811
    """@brief Avvia un'estrazione vera bloccata su una finta chiamata a pagamento.

    @param released Evento che libera la chiamata, come farebbe la risposta HTTP.
    @param result Parametri che la chiamata restituira' una volta liberata.
    """
    def blocking_extract(*args, **kwargs):
        progress = kwargs.get("progress")
        if progress is not None:
            progress("Richiesta inviata al modello (a pagamento).")
        released.wait(20)
        return result

    monkeypatch.setattr(gui, "extract_pin_header_params", blocking_extract)
    # Modalita' avanzata: qui si prova l'interruzione, non la generazione
    # automatica che in modalita' standard parte da sola a estrazione finita e
    # aprirebbe le proprie finestre su parametri finti.
    window._config["advanced_user"] = True
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData("ai"))
    row = QListWidgetItem("photo.png")
    row.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(row)
    window._extract_with_ai()
    assert window._extract_worker is not None


def test_aborting_an_ai_run_abandons_it_honestly_and_returns_the_window(
        window, monkeypatch):  # noqa: F811
    """@brief Positivo: la finestra torna subito; la spesa viene dichiarata.

    Non si finge di aver annullato la chiamata: il thread resta vivo (la
    guardia di chiusura continua a vederlo) e quello che cambia e' la validita'
    del suo risultato.
    """
    import threading

    released = threading.Event()
    answers = []
    _say_yes(monkeypatch, answers)
    try:
        _start_blocking_extraction(window, monkeypatch, released,
                                   ExtractedParams(name="Tardivo", description="dopo",
                                                   tags="", confidence="low", notes=""))
        assert _pump(lambda: window._extract_worker.isRunning())
        assert window.analysis_overlay.abort_button.isEnabled()
        window.field_description.setText("prima")

        window.analysis_overlay.abort_button.click()
        QApplication.processEvents()

        assert len(answers) == 1
        # La domanda deve dire che il denaro e' gia' speso: e' il punto per cui
        # l'interruzione dell'AI non e' la stessa cosa di quella del CAD.
        assert "rimbors" in answers[0]
        assert window.analysis_overlay.isHidden()
        assert window.tabs.isEnabled()
        assert not window.analysis_overlay.is_abortable()
        assert window._analysis_revision == 1
        log = window.log_text.toPlainText()
        assert "INTERRUZIONE chiesta dall'utente durante" in log
        assert "Analisi AI dei documenti" in log
        assert "non si può annullare" in log
        # La chiamata e' ancora in volo: la guardia di chiusura deve continuare
        # a vederla, altrimenti chiudere la finestra abbatterebbe il processo
        # con un QThread ancora vivo. `close()` restituisce False quando
        # l'evento viene rifiutato.
        assert window._background_work_running()
        assert window.close() is False, "la finestra si e' chiusa con un worker vivo"
    finally:
        released.set()
        if window._extract_worker is not None:
            window._extract_worker.wait(20000)
        QApplication.processEvents()


def test_the_late_result_of_an_abandoned_run_is_discarded(window, monkeypatch):  # noqa: F811
    """@brief Positivo: cio' che arriva dopo l'abbandono non ripopola nulla."""
    import threading

    released = threading.Event()
    _say_yes(monkeypatch, [])
    late = ExtractedParams(name="Tardivo", description="dopo", tags="",
                           confidence="low", notes="")
    _start_blocking_extraction(window, monkeypatch, released, late)
    assert _pump(lambda: window._extract_worker.isRunning())
    window.field_description.setText("prima")
    window.analysis_overlay.abort_button.click()
    QApplication.processEvents()

    released.set()
    assert _pump(lambda: window._extract_worker is None)
    QApplication.processEvents()

    assert window.field_description.text() == "prima", "i campi sono stati ripopolati"
    assert window._last_extracted is None
    assert window._abandoned_workers == []
    log = window.log_text.toPlainText()
    assert "scartato" in log
    # Chiusa la corsa fantasma, i comandi tornano disponibili da soli.
    assert not window._background_work_running()
    assert window.btn_extract_ai.isEnabled()
    assert "Estrai parametri" in window.btn_extract_ai.text()


def test_a_result_of_the_current_run_is_still_applied(window, monkeypatch):  # noqa: F811
    """@brief Negativo del precedente: senza interruzione il risultato entra.

    Distingue «scartato perche' interrotto» da «scartato per un difetto della
    guardia»: e' lo stesso percorso, con l'unica differenza che conta.
    """
    import threading

    released = threading.Event()
    released.set()
    _start_blocking_extraction(
        window, monkeypatch, released,
        ExtractedParams(name="Componente_di_prova", description="applicata", tags="",
                        confidence="low", notes="", rows=2, cols=4, pitch_mm=2.54,
                        row_spacing_mm=5.08, pad_diameter_mm=1.7, drill_mm=1.0))
    assert _pump(lambda: window._extract_worker is not None
                 and not window._extract_worker.isRunning())
    QApplication.processEvents()
    assert window.field_description.text() == "applicata"
    assert window._analysis_revision == 0


def test_aborting_the_automatic_generation_restores_the_window_without_alarms(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo sull'altra via CAD: la corsa automatica completa.

    Qui l'interruzione tocca anche la barra dei menu e il consuntivo di fine
    corsa. Il footprint 2D e' gia' stato scritto quando il solido comincia:
    quello resta, e va detto. E soprattutto NON deve aprirsi l'avviso
    «Generazione incompleta»: l'incompletezza e' la decisione dell'utente, non
    un guasto da riportargli (se si aprisse, la guardia della suite sui modali
    non chiusi farebbe fallire questo test).
    """
    flag = _arm_fake_freecad(monkeypatch, tmp_path)
    answers = []
    _say_yes(monkeypatch, answers)
    _prepare_3d(window)
    window._generate(automatic=True)
    assert window._worker is not None
    assert _pump(lambda: "KICADGEN_COMPONENT_START" in window.log_text.toPlainText())
    assert window.analysis_overlay.abort_button.isEnabled()
    assert not window.menuBar().isEnabled()

    window.analysis_overlay.abort_button.click()

    assert _pump(lambda: window._worker is not None and not window._worker.isRunning())
    QApplication.processEvents()

    assert not flag.exists(), "il processo figlio non e' stato ucciso"
    name = window._component_name()
    assert (window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod").is_file()
    assert not (window._project_dir() / f"{name}.3dshapes" / f"{name}.step").is_file()
    assert window.analysis_overlay.isHidden()
    assert window.tabs.isEnabled()
    assert window.menuBar().isEnabled()
    assert window.btn_generate.isEnabled()
    assert not window._abort_pending
    log = window.log_text.toPlainText()
    assert "INTERRUZIONE chiesta dall'utente durante" in log
    assert "Generazione di footprint e modello 3D" in log
    assert "Generazione interrotta dall’utente" in log
    assert "ERRORE 3D" not in log
