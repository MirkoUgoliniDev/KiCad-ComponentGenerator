"""@brief Shared test infrastructure: isolation from the user's data and a crash-proof Qt harness.

Besides keeping configuration and SQLite tests away from the user's live
preferences, this module turns the silent native aborts of HANDOFF 113 into
ordinary test failures: exceptions escaping Qt slots and worker threads, Qt
fatal messages, QThreads left running, widgets freed by the garbage collector
in a later test, and real network access.
"""
import faulthandler
import gc
import ipaddress
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import types
from pathlib import Path

import pytest

# `tests` e' la cartella di questi test. Ultralytics 8.4.x
# (requirements/yolo-pt.txt, modelli .pt) installa per errore un suo pacchetto `tests` di primo
# livello in site-packages; questa cartella non ha __init__.py, quindi per
# Python e' un pacchetto «namespace» e quello di Ultralytics vince sempre,
# e `from tests.test_photo_inventory_gui import ...` falliva in 17 file
# (2026-09-25). Il nome si lega qui, prima di ogni import dei test.
_TESTS_DIR = Path(__file__).resolve().parent
_bound = sys.modules.get("tests")
if _bound is None or str(_TESTS_DIR) not in [str(Path(p).resolve()) for p in getattr(_bound, "__path__", [])]:
    _package = types.ModuleType("tests")
    _package.__path__ = [str(_TESTS_DIR)]
    sys.modules["tests"] = _package

# Shared Qt fixtures (one QApplication for the session, the MainWindow
# harness): defined in tests/qt_fixtures.py, registered here for every module.
from qt_fixtures import app, qapp, window  # noqa: E402,F401 - pytest fixtures


@pytest.fixture(autouse=True)
def isolated_profile_paths(monkeypatch, tmp_path):
    # photo_yolo imposta YOLO_OFFLINE con setdefault (anche nell'app): nei test
    # il valore torna quello di partenza alla fine di ogni test, invece di
    # restare per il resto della sessione (revisione 131).
    monkeypatch.setenv("YOLO_OFFLINE", "1")
    monkeypatch.setenv('KICADGEN_PROFILE_DB', str(tmp_path / 'profiles.sqlite3'))
    monkeypatch.setenv('KICADGEN_CONFIG_PATH', str(tmp_path / 'config.json'))
    # L'archivio degli abbinamenti accettati cambia l'esito del matching: un
    # test non deve mai leggere (ne' scrivere) quello vero dell'utente.
    monkeypatch.setenv('KICADGEN_MATCH_DB', str(tmp_path / 'accepted_components.sqlite3'))
    # Everything resolved through `kicadgen.app_paths` (logs, crash reports,
    # caches, `data_file`, default projects folder) lands in the test's tmp
    # folder instead of the checkout. `data_file(seed=True)` still copies the
    # shipped database from `resource_path("data", ...)`. A test that needs
    # another folder sets the variable itself: its monkeypatch runs later.
    monkeypatch.setenv('KICADGEN_DATA_DIR', str(tmp_path / 'kicadgen-data'))


#: Funzioni che aprono una finestra e restano in attesa di un clic. In un test
#: nessuno clicca: la chiamata non torna piu' e la suite si ferma li'.
_BLOCKING_DIALOGS = (
    ('QMessageBox', ('information', 'warning', 'critical', 'question', 'about')),
    ('QFileDialog', ('getOpenFileName', 'getOpenFileNames', 'getSaveFileName',
                     'getExistingDirectory')),
    ('QInputDialog', ('getText', 'getItem', 'getInt', 'getDouble')),
)


@pytest.fixture(autouse=True)
def no_unattended_modal_dialog(monkeypatch):
    """@brief Fai fallire subito un test che apre una modale non prevista.

    Una modale aperta durante un test non e' mai voluta: nessuno la chiude, la
    chiamata non ritorna e la suite resta appesa a tempo indeterminato, senza
    dire dove ne' perche'. E' costato un'ora di diagnosi per scoprire che un
    ramo di validazione apriva un QMessageBox.warning.

    Un'eccezione e' diagnosticabile in un secondo; un blocco no. I test che una
    modale se la aspettano continuano a sostituirla per conto proprio: il loro
    monkeypatch viene applicato dopo questo e ha la precedenza.
    """
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        return  # Suite senza Qt: non c'e' nulla da proteggere.

    def refuse(name):
        def blocked(*args, **kwargs):
            raise AssertionError(
                f"{name} aperto durante un test e mai chiuso: la suite si "
                "fermerebbe qui. Sostituiscilo nel test (monkeypatch.setattr) "
                "se la modale e' prevista, oppure evita di aprirla.")
        return blocked

    for class_name, methods in _BLOCKING_DIALOGS:
        target = getattr(QtWidgets, class_name, None)
        if target is None:
            continue
        for method in methods:
            if hasattr(target, method):
                monkeypatch.setattr(target, method, refuse(f"{class_name}.{method}"),
                                    raising=False)

    # The unapplied-edits choice (2026-10-14, symbol and/or footprint) is a
    # built QMessageBox, not one of the static calls above: unattended it
    # answers «Resta», which is what leaving a project with pending symbol
    # edits did before the modal (the window fixtures close windows that
    # still hold a draft). Tests of the choice patch it themselves.
    # The footprint-draft question before a generation or analysis
    # (2026-10-14) answers «Applica»: before the draft existed every
    # footprint edit was saved at once, so a run started after an edit
    # always carried it; «Applica» keeps that behaviour for older tests.
    try:
        from kicadgen.gui import main_window
    except ImportError:
        return
    monkeypatch.setattr(main_window.MainWindow, "_ask_pending_edits_choice",
                        lambda self, editors, can_apply: main_window.MainWindow.PENDING_EDITS_STAY)
    monkeypatch.setattr(main_window.MainWindow, "_ask_footprint_draft_before_start",
                        lambda self: main_window.MainWindow.PENDING_EDITS_APPLY)
    # Pin names typed by the user vs a new analysis (audit 2026-10-15,
    # item 2): unattended, the safe answer «keep my names» (nothing lost).
    monkeypatch.setattr(main_window.MainWindow, "_ask_user_pin_names",
                        lambda self, conflicts, proposed, vanished=(): main_window.MainWindow.USER_PIN_NAMES_KEEP)
    # Structural doubts of a package geometry (audit 2026-10-15, item 3):
    # unattended, «leave as it is / confirm», i.e. the geometry is never
    # changed, which is what every older test expects.
    monkeypatch.setattr(main_window.MainWindow, "_ask_geometry_doubt",
                        lambda self, doubt: main_window.MainWindow.GEOMETRY_DOUBT_KEEP)


@pytest.fixture(autouse=True)
def no_real_api_key_sources(monkeypatch, tmp_path):
    """@brief No test reads the developer's real OpenRouter key, from any source.

    `openrouter.api_key` looks in the environment, the Windows Credential
    Manager and the `.env` files. The repository holds a real `.env` and the
    developer may have saved a key with «Account AI...»: a test that expects
    «no key» would find it, and one that writes a key would overwrite it.
    Here the environment variable is removed, the Credential Manager becomes
    an empty in-memory vault (`fake_credman.FakeAdvapi32`) and the `.env`
    files are looked up in the test's own tmp folder. Tests that need a key
    set it themselves, as they already did with `monkeypatch.setenv`.
    """
    from fake_credman import FakeAdvapi32
    from kicadgen.services import credentials, openrouter, providers
    monkeypatch.delenv(openrouter.API_KEY_ENV, raising=False)
    monkeypatch.delenv(providers.DEEPSEEK_KEY_ENV, raising=False)
    monkeypatch.setattr(credentials, "_advapi32", FakeAdvapi32())
    monkeypatch.setattr(openrouter, "env_files", lambda: [tmp_path / "dotenv" / ".env"])
    monkeypatch.setattr(providers, "env_files", lambda: [tmp_path / "dotenv" / ".env"])
    openrouter.invalidate_credit_cache()
    yield
    openrouter.invalidate_credit_cache()


@pytest.fixture(autouse=True)
def no_real_uninstall_registry(monkeypatch):
    """@brief No test sees the programs registered in this machine's registry.

    The FreeCAD and KiCad finders also read the «Uninstall» registry entries
    (`installed_programs._uninstall_entries`). A test that fakes the program
    folders to mean «not installed», or «only these versions», would
    otherwise find the developer's real installation. Tests of the registry
    path pass their own entries or patch this function themselves.
    """
    from kicadgen.services import installed_programs
    monkeypatch.setattr(installed_programs, "_uninstall_entries", lambda: [])


@pytest.fixture(autouse=True)
def drain_qt_thread_pool(monkeypatch, hang_watchdog):
    """@brief Ogni lavoro del pool globale di Qt finisce dentro il test che lo ha avviato.

    Un QRunnable avviato da una finestra (per esempio il controllo del modello
    YOLO di `yolo_model_dialog._Job`) sopravvive al test e partiva durante il
    test successivo, mentre quel test aveva sostituito un modulo con una
    funzione finta che chiama `pytest.fail`: l'eccezione nasceva nel thread del
    pool e Qt terminava il processo senza traceback (uscita 127 o 0xC0000409,
    HANDOFF 113 e 131). Si aspetta il pool PRIMA di annullare i monkeypatch
    (la fixture dipende da `monkeypatch`, quindi la sua chiusura viene prima).
    I lavori possono essere a catena: il risultato di uno, consegnato come
    segnale in coda, ne avvia un altro (in «Scegli il modello YOLO» l'impronta
    del file avvia il controllo). Per questo si alternano consegna degli eventi
    e attesa finche' il pool resta vuoto dopo una consegna.
    Il pool si prende PRIMA del test: alcuni test sostituiscono
    `QThreadPool.globalInstance` con una funzione che fallisce, per provare
    che nulla parte in anticipo.
    Dipende da `hang_watchdog`: la sua chiusura viene quindi PRIMA di quella
    del cane da guardia, e un'attesa bloccata resta sorvegliata. Se dopo tutti
    i giri il pool non e' vuoto il test fallisce: annullare i monkeypatch sotto
    un lavoro ancora vivo e' proprio lo scenario del crash (revisione 131).
    """
    from PyQt6.QtCore import QThreadPool
    pool = QThreadPool.globalInstance()
    yield
    from PyQt6.QtWidgets import QApplication
    application = QApplication.instance()
    if application is None:
        return
    if pool is None:
        # No application existed when the test started: take the pool now,
        # unless the test replaced `globalInstance` with a failing fake.
        try:
            pool = QThreadPool.globalInstance()
        except BaseException:  # noqa: BLE001 - pytest.fail is a BaseException
            return
        if pool is None:
            return
    for _round in range(50):
        pool.waitForDone(10_000)
        application.processEvents()
        if pool.activeThreadCount() == 0:
            return
    pytest.fail(f"QThreadPool still has {pool.activeThreadCount()} running job(s) after "
                "the drain: a background job outlives its test", pytrace=False)


@pytest.fixture(autouse=True)
def no_application_font_in_the_shared_session(monkeypatch):
    """@brief Refuse `QFontDatabase.addApplicationFont` outside the desktop-font child process.

    A registered font is never really removed: it changed the default font of
    every later test and made a layout test fail depending on the order
    (HANDOFF, 2026-10-16). Font-dependent checks use `tests/desktop_fonts.py`,
    which reruns them in a child process.
    """
    from desktop_fonts import in_desktop_font_process
    if in_desktop_font_process():
        yield
        return
    from PyQt6.QtGui import QFontDatabase

    def refused(*args, **kwargs):
        raise AssertionError("addApplicationFont in the shared test session: use "
                             "tests/desktop_fonts.run_with_desktop_fonts (child process)")
    monkeypatch.setattr(QFontDatabase, "addApplicationFont", staticmethod(refused))
    monkeypatch.setattr(QFontDatabase, "addApplicationFontFromData", staticmethod(refused))
    yield


@pytest.fixture(autouse=True)
def no_leftover_message_translator():
    """@brief Ogni test parte e finisce senza traduttore dei messaggi registrato.

    Una finestra registra il proprio `I18n` in `messages.tx`; se un test la
    lascia in inglese, i test successivi dei servizi (che confrontano i
    messaggi italiani) vedrebbero l'inglese per un effetto di ordine.
    """
    from kicadgen.services.messages import set_translator
    set_translator(None)
    yield
    set_translator(None)


@pytest.fixture(autouse=True)
def no_leftover_usage_listener():
    """@brief Ogni test parte e finisce senza ascoltatore delle chiamate pagate.

    Una finestra registra la riga del costo come ascoltatore globale di
    `openrouter`; lasciata attiva, una chiamata finta di un test successivo
    scriverebbe `ai-costs.json` nella cartella del progetto di quella finestra.
    """
    from kicadgen.services.openrouter import reset_usage, set_usage_listener
    set_usage_listener(None)
    # Anche il consuntivo della corsa: un test che chiama `_record_usage` non
    # deve lasciare chiamate «pagate» al successivo (audit 25/09).
    reset_usage()
    yield
    set_usage_listener(None)
    reset_usage()



#: Tempo massimo di un singolo test, in secondi. Il piu' lento della suite
#: (FreeCAD reale) resta sotto il minuto: il margine e' ampio di proposito.
TEST_WATCHDOG_S = 300
#: Dove finisce lo stack di un test bloccato (vedi `hang_watchdog`). Un file
#: per processo: piu' suite in parallelo (agenti) non si sovrascrivono a
#: vicenda. A fine sessione senza blocchi il file viene cancellato, quindi un
#: file rimasto in %TEMP% e' sempre il segno di una suite bloccata.
HANG_REPORT = Path(tempfile.gettempdir()) / f"kicadgen-test-hang-{os.getpid()}.txt"


def pytest_sessionfinish(session, exitstatus):
    """@brief Free the frozen objects while Qt is alive, then remove the hang report.

    @param session pytest session.
    @param exitstatus Session exit status.
    @note What survives a test with Qt windows is frozen after it (gc.freeze,
        see `request_post_test_collection`). Left frozen, those Qt wrappers were
        destroyed only during interpreter shutdown, in arbitrary order and
        possibly after the QApplication: the process then died with an access
        violation after all tests had passed (exit 139, HANDOFF 131). Here
        they are unfrozen and collected, and the deferred deletions
        delivered, while the application still exists.
    @note Exceptions that escaped after the last test (its post-test
        collection, see `_collect_after_test`) have no next setup to fail.
        They fail the SESSION instead: the exit status becomes
        TESTS_FAILED and `pytest_terminal_summary` lists them. A mid-run
        escape of the same kind fails the next test, so the last test of a
        run (twice per suite, which runs in two halves) must not be the one
        place where it is silently dropped; a printed warning with exit 0
        would not be seen by an unattended run.
    """
    _SESSION_END_ESCAPES.extend(_take_escaped())
    if _SESSION_END_ESCAPES and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    gc.unfreeze()
    try:
        from PyQt6.QtCore import QCoreApplication, QEvent
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        gc.collect()
    else:
        application = QApplication.instance()
        if application is not None:
            import faulthandler
            from PyQt6.QtWidgets import QMessageBox
            # Session teardown is no test: the per-test watchdog is off, so a
            # blocking close would hang CI with no message. Arm a watchdog and
            # make message boxes return at once (a window whose worker leaked
            # asks «wait for the running work?» in closeEvent).
            faulthandler.dump_traceback_later(TEST_WATCHDOG_S, exit=True)
            for name in ("information", "warning", "critical", "question"):
                setattr(QMessageBox, name, staticmethod(
                    lambda *args, **kwargs: QMessageBox.StandardButton.No))
            # Built message boxes too (e.g. the unapplied-symbol choice):
            # exec() returns at once with no button clicked, i.e. «stay».
            QMessageBox.exec = lambda self, *args, **kwargs: 0
            kept = {id(item) for item in _GRAVEYARD}
            for widget in list(application.topLevelWidgets()):
                if id(widget) in kept:
                    continue       # kept alive on purpose: its thread still runs
                try:
                    if widget.close():
                        widget.deleteLater()
                except RuntimeError:
                    pass
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            application.processEvents()
        gc.collect()
        if application is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            application.processEvents()
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
    try:
        HANG_REPORT.unlink()
    except OSError:
        pass


@pytest.fixture(autouse=True)
def hang_watchdog():
    r"""@brief Un test bloccato stampa dove si e' fermato e chiude la suite.

    `no_unattended_modal_dialog` copre le modali statiche di Qt, non le
    finestre del programma aperte con `exec()`: il 26/09 una di queste (la
    nuova «Scegli il modello YOLO», aperta da un test scritto per il vecchio
    selettore di file) ha tenuto ferma la suite completa senza nessun
    messaggio. Con `faulthandler` (libreria standard, nessuna dipendenza) un
    blocco oltre TEST_WATCHDOG_S scrive lo stack di tutti i thread in
    HANG_REPORT (%TEMP%\kicadgen-test-hang-<pid>.txt) e termina il processo: la causa si legge subito invece di
    aspettare. Su file e non su stderr perche' pytest cattura stderr e, con
    l'uscita immediata, quel testo andrebbe perso.
    """
    HANG_REPORT.parent.mkdir(parents=True, exist_ok=True)
    # Riscritto a ogni test: lo stack segue questa riga SOLO se il test
    # si e' bloccato; altrimenti il file dice soltanto l'ultimo avviato.
    with open(HANG_REPORT, "w", encoding="utf-8") as report:
        report.write(f"Ultimo test avviato (limite {TEST_WATCHDOG_S} s): "
                     f"{os.environ.get('PYTEST_CURRENT_TEST', '?')}\n")
    # Append mode: the Qt message handler and the escaped-exception hook also
    # append to this file (`_append_to_hang_report`); a stack dump written at
    # a stale offset would overwrite their lines.
    with open(HANG_REPORT, "a", encoding="utf-8") as report:
        faulthandler.dump_traceback_later(TEST_WATCHDOG_S, exit=True, file=report)
        try:
            yield
        finally:
            faulthandler.cancel_dump_traceback_later()


# ---------------------------------------------------------------------------
# Crash-proof Qt harness (HANDOFF 113 / 131 block B).
#
# The random 0xC0000409 / exit 127 of the full suite is Qt's qFatal: PyQt
# calls it when an exception escapes a slot, a QRunnable or a QThread.run
# while `sys.excepthook` is the default one, and Qt calls it when a QThread
# is destroyed while running. Closed windows are freed by the cyclic garbage
# collector at arbitrary moments, so the abort landed in unrelated tests with
# no traceback. The pieces below make each cause visible and attributable.
# ---------------------------------------------------------------------------

#: Bounded wait, in milliseconds, for a QThread a test started (see
#: `qt_worker_threads_joined`). Generous: a worker that needs longer is a hang.
THREAD_JOIN_TIMEOUT_MS = 15_000
#: When True a QThread still running after the bounded wait fails its test;
#: when False it is only reported (warning plus terminal summary).
ENFORCE_THREAD_JOIN = True

_ESCAPED_LOCK = threading.Lock()
#: Formatted tracebacks of exceptions that escaped a Qt slot or thread and
#: were not yet charged to a test.
_ESCAPED: list[str] = []
#: (test id, description) of QThreads found still running after the wait.
_THREAD_LEAKS: list[tuple[str, str]] = []
#: Leaked QThread wrappers, kept alive for the whole session: freeing a
#: running QThread is itself a qFatal. (Only the thread is kept; a parented
#: thread would still die with its parent.)
_GRAVEYARD: list = []
_HOOK_STATE: dict = {"previous_excepthook": None, "previous_qt_handler": None,
                     "qt_handler_installed": False}


def _current_test() -> str:
    """Return the id of the running test as pytest exports it, or '?'."""
    return os.environ.get("PYTEST_CURRENT_TEST", "?")


def _append_to_hang_report(text: str) -> None:
    """Append text to HANG_REPORT with an unbuffered os.write (survives an abort)."""
    try:
        descriptor = os.open(HANG_REPORT, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                             | getattr(os, "O_BINARY", 0))
    except OSError:
        return
    try:
        os.write(descriptor, text.encode("utf-8", "replace"))
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _qt_message_handler(mode, context, message) -> None:
    """@brief Mirror Qt critical/fatal messages into HANG_REPORT, then behave as before.

    @param mode Qt message type.
    @param context Source location supplied by Qt (may be empty in release builds).
    @param message Message text.
    @note A QtFatalMsg aborts the process as soon as this returns: the reason
        and the Python stacks of all threads are written first, so the file
        left in %TEMP% explains the abort. Every message is then forwarded to
        the previous handler or, like Qt's default one, to stderr.
    """
    try:
        from PyQt6.QtCore import QtMsgType
        if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
            label = "FATAL" if mode == QtMsgType.QtFatalMsg else "CRITICAL"
            where = ""
            if getattr(context, "file", None):
                where = f" [{context.file}:{context.line} {context.function}]"
            _append_to_hang_report(f"\nQt {label} during {_current_test()}: {message}{where}\n")
            if mode == QtMsgType.QtFatalMsg:
                descriptor = os.open(HANG_REPORT, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                try:
                    faulthandler.dump_traceback(file=descriptor, all_threads=True)
                finally:
                    os.close(descriptor)
        previous = _HOOK_STATE["previous_qt_handler"]
        if previous is not None:
            previous(mode, context, message)
        else:
            os.write(2, f"{message}\n".encode("utf-8", "replace"))
    except BaseException:  # noqa: BLE001 - an exception here would be a qFatal itself
        pass


def _record_escaped_exception(exc_type, exc_value, exc_tb) -> None:
    """@brief Session `sys.excepthook`: remember the exception instead of letting PyQt abort.

    @param exc_type Exception class.
    @param exc_value Exception instance.
    @param exc_tb Traceback.
    @note PyQt6 calls qFatal for an exception escaping a slot, a QRunnable or
        a QThread.run only while `sys.excepthook` is the default hook, and
        this holds for BaseException too (e.g. `pytest.fail` called by a
        fake inside a worker). The exception is charged to the current test
        by the `pytest_runtest_*` wrappers below.
    """
    if sys.excepthook is not _record_escaped_exception:
        # Reached by chaining from a hook a test installed on purpose (crash
        # reports, launcher): that hook owns the exception, and while it is
        # installed PyQt calls it instead of aborting. Print as the default.
        try:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
        except BaseException:  # noqa: BLE001 - never escalate from a hook
            pass
        return
    try:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        text = f"[thread {threading.current_thread().name}]\n{text}"
    except BaseException:  # noqa: BLE001 - formatting must never escalate
        text = f"{exc_type!r}: {exc_value!r}"
    with _ESCAPED_LOCK:
        _ESCAPED.append(text)
    _append_to_hang_report(f"\nException escaped a Qt slot/thread during {_current_test()}:\n{text}")


def _take_escaped() -> list[str]:
    """Return and forget the escaped exceptions recorded so far."""
    with _ESCAPED_LOCK:
        taken = list(_ESCAPED)
        _ESCAPED.clear()
    return taken


def _fail_on_escaped(when: str) -> None:
    """Fail the current phase when exceptions escaped a Qt slot or thread."""
    escaped = _take_escaped()
    if escaped:
        pytest.fail(
            f"{len(escaped)} exception(s) escaped a Qt slot, QRunnable or thread {when}; "
            "outside the tests PyQt would have aborted the process:\n\n"
            + "\n".join(escaped), pytrace=False)


def pytest_configure(config):
    """@brief Register markers and install the session-wide Qt/exception hooks.

    @param config pytest configuration.
    """
    config.addinivalue_line(
        "markers", "allow_network: the test may open non-loopback network connections")
    _HOOK_STATE["previous_excepthook"] = sys.excepthook
    sys.excepthook = _record_escaped_exception
    _HOOK_STATE["installed_hook"] = _record_escaped_exception
    try:
        from PyQt6.QtCore import qInstallMessageHandler
    except ImportError:
        return
    _HOOK_STATE["previous_qt_handler"] = qInstallMessageHandler(_qt_message_handler)
    _HOOK_STATE["qt_handler_installed"] = True


def pytest_unconfigure(config):
    """@brief Restore the hooks replaced in `pytest_configure`.

    @param config pytest configuration.
    """
    # A leaked QThread still running when the interpreter frees it would
    # abort the process at exit and hide the session's real exit status.
    join_qthreads([item for item in _GRAVEYARD if hasattr(item, "isRunning")], 60_000)
    _release_graveyard()
    if sys.excepthook is _record_escaped_exception:
        sys.excepthook = _HOOK_STATE["previous_excepthook"] or sys.__excepthook__
    if _HOOK_STATE["qt_handler_installed"]:
        from PyQt6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_HOOK_STATE["previous_qt_handler"])
        _HOOK_STATE["qt_handler_installed"] = False


def _release_graveyard() -> None:
    """Destroy the objects kept alive for a leaked QThread, once every such thread has ended.

    `_GRAVEYARD` keeps a window whose worker did not stop in time, and the
    worker, for the whole session: freeing either while the thread runs is a
    qFatal. `pytest_sessionfinish` skips them on purpose. Left there, they
    were destroyed by the interpreter's own shutdown, after the QApplication
    could already be gone, and the process died with an access violation
    (0xC0000005) AFTER all its tests had passed: reproduced 3 times in 6 with
    the child run of test_window_whose_worker_does_not_stop_is_kept_alive_with_its_graph
    (2026-10-15), the same class as the exit-139 crash of HANDOFF 131.
    Here, after `pytest_unconfigure` joined the threads, the widgets are
    deleted while Qt still exists and the list is emptied. If a thread is
    STILL running nothing is touched: the old contract wins.
    @note `close()` is NOT called (final review, L2). The monkeypatches are
        undone at unconfigure, and the real `MainWindow.closeEvent` opens a
        modal QMessageBox when `_background_work_running()` is true or an
        edit is pending: nobody would click it, and a green run would end
        with the watchdog's exit code. `deleteLater` skips closeEvent, and
        destroying is what this function is for. Modal dialogs are refused
        meanwhile all the same, as in `_collect_after_test`: a dialog opened
        by a dying child would hang the same way.
    """
    if not _GRAVEYARD:
        return
    if any(_thread_is_running(item) for item in _GRAVEYARD if hasattr(item, "isRunning")):
        return
    try:
        from PyQt6 import sip
        from PyQt6.QtCore import QCoreApplication, QEvent
        from PyQt6.QtWidgets import QApplication, QWidget
    except ImportError:
        _GRAVEYARD.clear()
        return
    application = QApplication.instance()
    if application is None:
        return            # nothing safe to do without Qt: keep the old behaviour
    # Session teardown has no per-test watchdog: a blocking close must not
    # hang an unattended run without a stack dump.
    faulthandler.dump_traceback_later(TEST_WATCHDOG_S, exit=True)
    patcher = pytest.MonkeyPatch()
    try:
        _refuse_modal_dialogs_after_test(patcher)
        for item in list(_GRAVEYARD):
            if isinstance(item, QWidget) and not sip.isdeleted(item):
                try:
                    item.hide()         # no paint of a half-destroyed window
                    item.deleteLater()  # its threads have ended: nothing to refuse
                except RuntimeError:
                    pass
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        application.processEvents()
        _GRAVEYARD.clear()
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        application.processEvents()
    finally:
        patcher.undo()
        faulthandler.cancel_dump_traceback_later()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    """@brief Charge exceptions escaped between tests to the next test's setup.

    @param item Test about to run.
    @return The wrapped hook's result.
    """
    _fail_on_escaped("before this test started (leftover of an earlier test, "
                     "typically an object freed by the garbage collector)")
    if (_HOOK_STATE.get("installed_hook") is not None
            and sys.excepthook is not _HOOK_STATE["installed_hook"]):
        # A previous test replaced the hook without monkeypatch: from here on
        # a slot exception would abort the process again. Restore and say so.
        leaked, sys.excepthook = sys.excepthook, _HOOK_STATE["installed_hook"]
        pytest.fail(f"sys.excepthook was replaced and not restored by an earlier test "
                    f"({leaked!r}); use monkeypatch.setattr(sys, 'excepthook', ...)",
                    pytrace=False)
    try:
        return (yield)
    finally:
        _fail_on_escaped("during setup")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """@brief Fail the test whose body let an exception escape a Qt slot or thread.

    @param item Running test.
    @return The wrapped hook's result.
    """
    try:
        return (yield)
    finally:
        _fail_on_escaped("during the test")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """@brief Fail the teardown when fixture finalisers let an exception escape.

    @param item Test being torn down.
    @param nextitem Next test, or None.
    @return The wrapped hook's result.
    """
    try:
        return (yield)
    finally:
        _fail_on_escaped("during teardown (fixture finalisers, window destruction)")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """@brief List leaked QThreads, network attempts and exceptions escaped after the last test.

    @param terminalreporter pytest terminal reporter.
    @param exitstatus Session exit status.
    @param config pytest configuration.
    """
    if _THREAD_LEAKS:
        terminalreporter.section("QThreads still running at teardown")
        for test_id, description in _THREAD_LEAKS:
            terminalreporter.line(f"{test_id}: {description}")
    if _SESSION_END_ESCAPES:
        # Charged to the session, not to a test (see pytest_sessionfinish).
        terminalreporter.section("Exceptions escaped after the last test (session failed)")
        for text in _SESSION_END_ESCAPES:
            terminalreporter.line(text)
    if _NETWORK_ATTEMPTS:
        terminalreporter.section("Network access blocked (mock it or mark allow_network)")
        for test_id, attempt in _NETWORK_ATTEMPTS:
            terminalreporter.line(f"{test_id}: {attempt}")


def _describe_thread(thread) -> str:
    """Short human description of a QThread for the reports."""
    try:
        name = thread.objectName()
    except RuntimeError:
        name = ""
    return f"{type(thread).__module__}.{type(thread).__qualname__}" + (f" '{name}'" if name else "")


def _thread_is_running(thread) -> bool:
    """True when the QThread still runs; False when it ended or its C++ object is gone."""
    try:
        return bool(thread.isRunning())
    except RuntimeError:
        return False


def join_qthreads(threads, timeout_ms: int | None = None) -> list:
    """@brief Wait, within one shared deadline, for QThreads to finish.

    @param threads QThread objects (deleted C++ objects are skipped).
    @param timeout_ms Total budget in milliseconds for all of them; None
        means THREAD_JOIN_TIMEOUT_MS as it is at call time.
    @return The threads still running when the budget is exhausted.
    @note Cancellation is requested first (`requestInterruption` and, for the
        application workers, their `cancel_event`), so a cooperative worker
        stops early; nothing is terminated forcibly.
    """
    if timeout_ms is None:
        timeout_ms = THREAD_JOIN_TIMEOUT_MS
    running = [thread for thread in threads if _thread_is_running(thread)]
    for thread in running:
        cancel_event = getattr(thread, "cancel_event", None)
        if cancel_event is not None and hasattr(cancel_event, "set"):
            cancel_event.set()
        try:
            thread.requestInterruption()
        except RuntimeError:
            pass
    deadline = time.monotonic() + timeout_ms / 1000.0
    for thread in running:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            thread.wait(remaining)
        except RuntimeError:
            pass
    return [thread for thread in running if _thread_is_running(thread)]


def _collect_new_top_level_widgets(application, before: set) -> None:
    """Free, inside the owning test, top-level widgets it left for the garbage collector.

    The survivors are NOT frozen here: at this point pytest still references
    the test's fixture values (`item.funcargs`, the fixture caches), so a
    freeze would pin them, and every object they reach, for the whole session
    (2026-10-15, see `request_post_test_collection`). The freeze that keeps
    later collections fast happens after the test, in `pytest_runtest_protocol`.
    """
    try:
        current = {id(widget) for widget in application.topLevelWidgets()}
    except RuntimeError:
        return
    if not (current - before):
        return
    from PyQt6.QtCore import QCoreApplication, QEvent
    application.processEvents()
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    application.processEvents()
    request_post_test_collection()


#: Set by `request_post_test_collection`, consumed by `pytest_runtest_protocol`.
_POST_TEST_COLLECTION = {"requested": False}
#: Bounded wait, in milliseconds, for the global Qt pool after the post-test
#: collection: a job started by an object freed there must not run into the
#: next test, but nothing may block between tests without a watchdog.
POST_TEST_POOL_WAIT_MS = 10_000


def request_post_test_collection() -> None:
    """@brief Ask for one garbage collection and freeze once pytest has released the test.

    @note Why the collection cannot happen in the teardown itself
        (2026-10-15). Until the end of `runtestprotocol` pytest holds the
        test's fixture values in `item.funcargs` and in the fixture caches,
        so a destroyed MainWindow is still reachable while its fixture tears
        down. The old teardown called gc.freeze() at that point: the window's
        Python graph moved to the permanent generation and was never
        collected (about 730 objects per window test, measured), and the
        sip wrappers it kept stayed registered for the session, which let a
        new C++ object allocated at a reused address come back as a stale
        wrapper of another type (HANDOFF «TEST INSTABILE DRILLS»).
    @note Why a freeze is still done afterwards. It was introduced for speed
        (HANDOFF 131 block B: the teardown collections rescanned the whole
        heap, second half of the suite from about 3 to 7 minutes) and so
        that what survives a test is never destroyed at a random moment of a
        later test (HANDOFF 113). Both reasons hold for what is still alive
        once pytest has dropped the test: that is long-lived by definition.
    """
    _POST_TEST_COLLECTION["requested"] = True


#: Watchdog of the post-test collection, in seconds. It runs between two
#: tests, after `hang_watchdog` has disarmed its own timer, so it needs one
#: of its own: a destructor or slot that blocks there would otherwise hang
#: the suite with no report. Module global, read at call time (tests lower it).
POST_TEST_WATCHDOG_S = TEST_WATCHDOG_S
#: Exceptions of the post-test collection after the LAST test: no next setup
#: will charge them, so `pytest_sessionfinish` fails the session with them
#: and `pytest_terminal_summary` lists them.
_SESSION_END_ESCAPES: list[str] = []


def _refuse_modal_dialogs_after_test(patcher) -> None:
    """Make every blocking dialog raise during the post-test collection.

    The per-test guard `no_unattended_modal_dialog` is already undone at that
    point. Here the static Qt dialogs of `_BLOCKING_DIALOGS` AND the `exec`
    of built dialogs are refused: no test is running, so no dialog is ever
    expected, and a dialog opened by a dying object would wait for a click
    that never comes. The raised AssertionError is recorded (excepthook for
    slots, the unraisable hook installed by `_collect_after_test` for
    destructors) and reported like any other escaped exception.
    """
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        return

    def refuse(name):
        def blocked(*args, **kwargs):
            raise AssertionError(f"{name} opened by an object freed after its test "
                                 "(post-test garbage collection): nobody can close it")
        return blocked

    # In PyQt6 6.11 the standard dialogs inherit QDialog.exec, but a binding
    # that redefines it on a subclass would bypass a QDialog-only patch; the
    # standard ones are listed too (monkeypatch removes the added class
    # attribute on undo, so the inherited method comes back unchanged).
    built = tuple((name, ("exec",)) for name in (
        "QDialog", "QMessageBox", "QFileDialog", "QInputDialog", "QColorDialog",
        "QFontDialog", "QProgressDialog", "QErrorMessage", "QWizard"))
    for class_name, methods in _BLOCKING_DIALOGS + built:
        target = getattr(QtWidgets, class_name, None)
        if target is None:
            continue
        for method in methods:
            if hasattr(target, method):
                patcher.setattr(target, method, refuse(f"{class_name}.{method}"), raising=False)


def _record_unraisable(unraisable) -> None:
    """Record an exception raised in a destructor (`__del__`, weakref callback) as escaped.

    Python only prints those («Exception ignored in ...»); outside a test
    pytest's own unraisable plugin is not listening, so without this hook an
    AssertionError from a refused dialog in a destructor would vanish.
    """
    exc = unraisable.exc_value
    text = ("".join(traceback.format_exception(unraisable.exc_type, exc, unraisable.exc_traceback))
            if exc is not None else repr(unraisable))
    with _ESCAPED_LOCK:
        _ESCAPED.append(f"Exception in a destructor ({unraisable.object!r}):\n{text}")


def _collect_after_test(test_id: str = "?") -> None:
    """Collect the released test objects, drain Qt, then freeze the survivors.

    Runs between two tests, after pytest dropped the finished test's fixture
    values and before the next test's setup. Most of the window graph was
    already freed inside the test (see `qt_fixtures.shutdown_main_window`);
    what is freed here is what only pytest or monkeypatch still referenced:
    fixture values (e.g. a widget a fixture yielded and the test never
    destroyed) and the attributes monkeypatch wrote back onto a cleared
    window. A job or deferred deletion triggered by them is delivered now.
    @note Safety boundary: no per-test guard is active here (watchdog,
        modal-dialog guard, network guard and the test's monkeypatches are
        all undone). So this phase arms its own faulthandler watchdog
        (POST_TEST_WATCHDOG_S, stacks into HANG_REPORT, then the process
        exits), refuses every modal dialog, and records exceptions from
        slots (session excepthook) and destructors (unraisable hook). They
        are charged to the next test's setup, or to the session when this
        was the last test (`pytest_sessionfinish`).
    """
    _POST_TEST_COLLECTION["requested"] = False
    _append_to_hang_report(f"\nPost-test garbage collection after {test_id} "
                           f"(limit {POST_TEST_WATCHDOG_S} s)\n")
    patcher = pytest.MonkeyPatch()
    previous_unraisable = sys.unraisablehook
    report = None
    # Every step, the opening of the report included, is inside the guarded
    # region: this runs inside the `pytest_runtest_protocol` hook, where an
    # exception would become an INTERNALERROR. A failure is recorded as an
    # escape (charged like the others) and the finally always disarms.
    try:
        try:
            report = open(HANG_REPORT, "a", encoding="utf-8")
        except OSError as error:
            # Without the report the watchdog still runs (a hang must end the
            # process) and dumps to the original stderr. Caveat: pytest's fd
            # capture also redirects fd 2, so that dump may be lost when the
            # process exits; the escape below says why there is no report.
            with _ESCAPED_LOCK:
                _ESCAPED.append(f"Hang report {HANG_REPORT} not writable for the post-test "
                                f"collection ({error!r}); its watchdog dumps to stderr")
        target = report if report is not None else sys.__stderr__
        if target is not None:
            faulthandler.dump_traceback_later(POST_TEST_WATCHDOG_S, exit=True, file=target)
        _refuse_modal_dialogs_after_test(patcher)
        sys.unraisablehook = _record_unraisable
        application = None
        try:
            from PyQt6.QtCore import QCoreApplication, QEvent, QThreadPool
            from PyQt6.QtWidgets import QApplication
            application = QApplication.instance()
        except ImportError:
            pass
        gc.collect()
        if application is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            application.processEvents()
            try:
                pool = QThreadPool.globalInstance()
            except BaseException:  # noqa: BLE001 - a leftover fake must not abort the session
                pool = None
            if pool is not None and not pool.waitForDone(POST_TEST_POOL_WAIT_MS):
                with _ESCAPED_LOCK:
                    _ESCAPED.append(
                        f"QThreadPool still has {pool.activeThreadCount()} running job(s) "
                        f"{POST_TEST_POOL_WAIT_MS} ms after the post-test garbage collection")
            application.processEvents()
        gc.freeze()
    finally:
        sys.unraisablehook = previous_unraisable
        patcher.undo()
        faulthandler.cancel_dump_traceback_later()
        if report is not None:
            report.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """@brief Run the post-test collection requested during the test, once pytest released it.

    @param item Test whose protocol runs.
    @param nextitem Next test, or None.
    @return The wrapped hook's result.
    @note `runtestprotocol` clears `item.funcargs` after the teardown, so
        after the yield the finished test is no longer referenced by pytest
        (except by `sys.last_traceback` when it failed).
    @note Not a `finally`: when the protocol raises (KeyboardInterrupt,
        pytest.exit) the collection is skipped so it can never mask that
        exception; an ordinary exception of the collection itself is
        recorded as an escape, never propagated as an INTERNALERROR.
    """
    try:
        result = yield
    except BaseException:
        # The protocol itself raised: KeyboardInterrupt, pytest.exit, an
        # internal error. The run is ending; collecting here could only delay
        # it or, worse, replace that exception with one of ours. The frozen
        # and pending objects are freed by `pytest_sessionfinish` instead.
        _POST_TEST_COLLECTION["requested"] = False
        raise
    if _POST_TEST_COLLECTION["requested"]:
        try:
            _collect_after_test(item.nodeid)
        except Exception as error:  # noqa: BLE001 - must never become an INTERNALERROR
            # Recorded like an escape: it fails the next setup or the session.
            with _ESCAPED_LOCK:
                _ESCAPED.append(f"Post-test garbage collection after {item.nodeid} failed: "
                                f"{error!r}")
        if nextitem is None:
            # Last test of the run: no next setup will charge what the
            # collection let escape, so keep it for the session end.
            _SESSION_END_ESCAPES.extend(_take_escaped())
    return result


@pytest.fixture(autouse=True)
def qt_worker_threads_joined(monkeypatch):
    """@brief Every QThread a test starts has finished before its monkeypatches are undone.

    `QThread.start` is wrapped for the duration of the test; at teardown the
    started threads are joined within THREAD_JOIN_TIMEOUT_MS. A thread still
    running is kept alive for the session (freeing it would be a qFatal),
    listed in the terminal summary and, with ENFORCE_THREAD_JOIN, fails the
    test. Then the top-level widgets the test created are freed now, by an
    explicit garbage collection, instead of in a random later test.
    @note The name sorts after `drain_qt_thread_pool`, so this teardown runs
        before the pool drain: pool jobs triggered by the destruction of a
        widget are still drained inside this test.
    """
    try:
        from PyQt6.QtCore import QThread
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        yield
        return
    started: list = []
    original_start = QThread.start

    def recording_start(self, *args, **kwargs):
        started.append(self)
        return original_start(self, *args, **kwargs)

    monkeypatch.setattr(QThread, "start", recording_start)
    application = QApplication.instance()
    try:
        before = {id(widget) for widget in application.topLevelWidgets()} if application else set()
    except RuntimeError:
        before = set()
    yield
    leaked = join_qthreads(started)
    started.clear()
    application = QApplication.instance()
    if application is not None:
        application.processEvents()
        if not leaked:
            _collect_new_top_level_widgets(application, before)
    if leaked:
        _GRAVEYARD.extend(leaked)
        test_id = _current_test()
        descriptions = ", ".join(_describe_thread(thread) for thread in leaked)
        _THREAD_LEAKS.append((test_id, descriptions))
        _append_to_hang_report(f"\nQThread still running after teardown of {test_id}: {descriptions}\n")
        message = (f"QThread(s) still running {THREAD_JOIN_TIMEOUT_MS} ms after the test: "
                   f"{descriptions}. Stop or wait for them in the test.")
        if ENFORCE_THREAD_JOIN:
            pytest.fail(message, pytrace=False)
        import warnings
        warnings.warn(pytest.PytestWarning(message))


#: Repository root, to point the network report at the project's own frames.
_REPO_DIR = _TESTS_DIR.parent
#: When True a test whose code tried to reach the network fails at teardown,
#: even if the code swallowed the refusal; when False the attempt is still
#: blocked (the code sees an OSError) and only reported, as a warning and in
#: the terminal summary. True since the two known offenders were fixed (131).
ENFORCE_NO_NETWORK = True
#: (test id, first attempt) of the tests whose network access was blocked.
_NETWORK_ATTEMPTS: list[tuple[str, str]] = []


class NetworkAccessBlocked(ConnectionRefusedError):
    """@brief Raised by the test network guard for a non-loopback connection or lookup.

    @note It is an OSError on purpose: the code under test takes its ordinary
        offline path, and the guard still fails the test at teardown.
    """


def _is_loopback_host(host) -> bool:
    """True for hosts that never leave the machine (loopback names and addresses)."""
    if host in (None, "", b"", "localhost", b"localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_ip_literal(host) -> bool:
    """True when resolving host needs no DNS query."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        ipaddress.ip_address(str(host).split("%", 1)[0])
        return True
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """@brief No test talks to the network unless marked `allow_network`.

    Connections (`socket.connect`, `connect_ex`, hence `create_connection`,
    urllib and requests) to non-loopback addresses and DNS lookups of
    non-loopback names raise NetworkAccessBlocked, and the test fails at
    teardown with the address and the calling stack, even when the code under
    test swallowed the OSError. Loopback stays allowed: Windows builds
    `socket.socketpair` (asyncio, multiprocessing) on 127.0.0.1.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    attempts: list[str] = []
    # Exposed so a test of the guard itself can inspect or clear its attempts.
    request.node.network_attempts = attempts

    def refuse(what: str):
        frames = [frame for frame in traceback.extract_stack()[:-2]
                  if not any(part in frame.filename for part in ("pluggy", "_pytest"))]
        # The project's own frames say who called; the last few say how.
        ours = [frame for frame in frames[:-4] if frame.filename.startswith(str(_REPO_DIR))]
        stack = "".join(traceback.format_list(ours[-8:] + frames[-4:]))
        attempts.append(f"{what} (thread {threading.current_thread().name})\n{stack}")
        raise NetworkAccessBlocked(
            f"Network access blocked in tests: {what}. Mock it, or mark the test "
            "with @pytest.mark.allow_network if it really needs the network.")

    def guarded(original):
        def method(self, address, *args, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) \
                    and not _is_loopback_host(address[0]):
                refuse(f"connect to {address!r}")
            return original(self, address, *args, **kwargs)
        return method

    original_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not (_is_loopback_host(host) or _is_ip_literal(host)):
            refuse(f"DNS lookup of {host!r}")
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_resolver(original, what):
        def resolve(host, *args, **kwargs):
            if not (_is_loopback_host(host) or _is_ip_literal(host)):
                refuse(f"{what} of {host!r}")
            return original(host, *args, **kwargs)
        return resolve

    original_sendto = socket.socket.sendto

    def guarded_sendto(self, data, *args):
        address = args[-1] if args else None
        if (self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)
                and not _is_loopback_host(address[0])):
            refuse(f"datagram to {address!r}")
        return original_sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, guarded_resolver(getattr(socket, name), name))
    yield
    if attempts:
        message = (f"{len(attempts)} network access attempt(s) blocked during the test:\n\n"
                   + "\n".join(attempts))
        _NETWORK_ATTEMPTS.append((_current_test(), attempts[0].splitlines()[0]))
        _append_to_hang_report(f"\nNetwork access blocked during {_current_test()}\n")
        if ENFORCE_NO_NETWORK:
            pytest.fail(message, pytrace=False)
        import warnings
        warnings.warn(pytest.PytestWarning(message))


def pytest_collection_finish(session):
    """@brief Freeze the objects that exist after collection out of the garbage collector.

    @param session pytest session.
    @note The per-test teardown runs full garbage collections so that closed
        windows are destroyed inside their own test (HANDOFF 113/131). Each
        full collection scanned every module, Qt wrapper and array imported
        so far, and the second half of the suite went from about 3 to 7
        minutes. After collection those objects live for the whole session:
        gc.freeze() moves them to the permanent generation, so the teardown
        collections scan only what the tests themselves created.
    """
    gc.collect()
    gc.freeze()


@pytest.fixture(autouse=True)
def project_kind_dialog_accepts(monkeypatch):
    """@brief The project-kind wizard never waits for a click in a test.

    `ProjectKindDialog` (2026-10-09) opens with `exec()` from `_new_project`
    and from `_open_project_at` when a project has no kind: tests written
    before it patch only the name dialog and hung for TEST_WATCHDOG_S on the
    wizard (full suite of 2026-10-10 stopped at 45%). Here it accepts with
    the type it was given, or the window's current type, or the historical
    default: a test about the wizard itself patches `exec`/`selected_type`
    and its patch, applied later, wins.
    """
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui import project_dialogs

    original_init = project_dialogs.ProjectKindDialog.__init__

    def init(self, tr=None, selected="", parent=None):
        original_init(self, tr=tr, selected=selected, parent=parent)
        self._test_parent = parent

    def selected_type(self):
        if getattr(self, "_selected_key", ""):
            return self._selected_key
        parent = getattr(self, "_test_parent", None)
        key = parent._profile_key() if parent is not None and hasattr(parent, "_profile_key") else ""
        return key or "aggregate_castellated"

    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "__init__", init)
    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "selected_type", selected_type)
