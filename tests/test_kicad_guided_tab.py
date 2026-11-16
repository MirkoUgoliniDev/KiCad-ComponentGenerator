"""@brief The KiCad tab as a guided path: the check runs by itself and decides the installation.

User, 2026-11-15: «prima il controllo e poi la proposta» (first the check, then
the proposal), «io preferirei qualcosa di guidato». Decided rules, each with a
case that must trigger and one that must not:

- entering the KiCad tab with files no check has read starts the check by
  itself, quietly; elsewhere, or while work runs, it does not;
- the installations wait for a check of the CURRENT files: off before it, off
  while it runs, off when it found an error or was stopped; on with warnings;
  on and highlighted when every automatic check passed;
- any write of the files an installation copies makes the outcome stale and
  the check starts again; a finished check of unchanged files never restarts;
- a menu entry or a direct call is refused like the button.

No AI and no kicad-cli: `verify_component` is replaced by a scripted one.
"""
import threading
import time

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from kicadgen.gui import component_check_dialog as ccd  # noqa: E402
from kicadgen.gui import main_window as gui  # noqa: E402
from kicadgen.services.component_check import (ACTION_OPEN_KICAD_TAB, IMPORTANCE_BEFORE_PRODUCTION,  # noqa: E402
                                               Check)
from tests.qt_fixtures import use_real_kicad_check  # noqa: E402
from tests.test_generated_files_alignment import _project  # noqa: E402

OK = Check('OK', 'Geometria del footprint', 'ok', code='footprint_geometry')
WARN = Check('WARN', 'Numerazione', 'Confronta il pin 1', importance=IMPORTANCE_BEFORE_PRODUCTION,
             remedy='Confronta il pin 1 con la scheda reale.', code='pin_one')
ERROR = Check('ERROR', 'Lettura footprint in KiCad', 'File illeggibile', code='kicad_footprint_read')
LIBRARY = Check('WARN', 'Libreria KiCad', 'Componente non ancora installato in una libreria KiCad.',
                category='final', action=ACTION_OPEN_KICAD_TAB, code='kicad_library')
#: Always listed by the service: the checks done by hand in KiCad, after the installation.
MANUAL = Check('WARN', 'Verifiche manuali', 'ERC del tuo schema e DRC della tua scheda.',
               category='manual', code='manual_checks')
#: A failure of the checking tool, as the service reports it since review H1.
TOOL_FAILED = Check('SKIP', 'Lettura footprint in KiCad', 'kicad-cli non ha risposto entro 60 secondi.',
                    remedy='Ripeti il controllo.', code='kicad_footprint_read')
#: KiCad read the footprint / the applied symbol: the rows of a real run with
#: kicad-cli. CONTRACT CHANGED 2026-11-15 (review M6): an outcome without any
#: KiCad read, kicad-cli being there, keeps the installation off («unread»),
#: so the scripted outcomes that must allow it carry their reads.
READ = Check('OK', 'Lettura footprint in KiCad', 'File letto da KiCad.', code='kicad_footprint_read')
SYMBOL_READ = Check('OK', 'Simbolo applicato: lettura in KiCad', 'File letto da KiCad.',
                    code='kicad_symbol_read')
#: KiCad read the saved symbol file the installation ships. CONTRACT CHANGED
#: 2026-11-15 (round 2, R4): each file an installation copies must have been
#: read -- the footprint AND, when <name>.kicad_sym exists (it does in the
#: generated project of these tests), the symbol file -- so the outcomes that
#: must allow the installation carry both reads.
FILE_READ = Check('OK', 'File del simbolo: lettura in KiCad', 'File letto da KiCad.',
                  code='kicad_symbol_file_read')
SYMBOL_TOOL_FAILED = Check('SKIP', 'Simbolo applicato: lettura in KiCad',
                           'kicad-cli non avviato: accesso negato', code='kicad_symbol_read')
#: kicad-cli absent: the service's rows, each declared `needs_cli`.
NO_CLI = Check('SKIP', 'KiCad', 'KiCad CLI non disponibile: apertura reale non verificata.',
               code='kicad_cli', needs_cli=True)
NO_CLI_READ = Check('SKIP', 'Lettura footprint in KiCad', 'Richiede KiCad CLI.',
                    code='kicad_footprint_read', needs_cli=True)
#: The service's first row when kicad-cli is there (no code, as `verify_component` emits it).
CLI_OK = Check('OK', 'KiCad CLI', 'C:/KiCad/bin/kicad-cli.exe')
#: The rows that conclude the non-KiCad checks an installation needs
#: (`component_check.REQUIRED_FOR_INSTALL`, round 2, R1): the footprint file
#: parsed, pin-pad, and the STEP's integrity (the test project has a STEP).
REQUIRED_ROWS = (Check('OK', 'File footprint', 'ok', code='footprint_file'),
                 Check('OK', 'Pin e pad', 'ok', code='pin_pad'),
                 Check('OK', 'Modello STEP', 'ok', code='step_integrity'))
#: The rows that conclude the KiCad stages after the reads. CONTRACT CHANGED
#: 2026-11-15 (round 3): a crashed run allows the installation only when it
#: concluded EVERY blocking-capable check (`component_check.BLOCKING_CHECKS`),
#: the STEP import into KiCad and the preview's DRC included when kicad-cli
#: and a STEP are there; a script that must read «crashed» carries them.
IMPORT_3D = Check('OK', 'Importazione 3D in KiCad', 'ok', code='kicad_3d_import')
DRC_OK = Check('OK', 'DRC', 'ok', code='drc')
#: kicad-cli absent: the saved symbol file's read, skipped with `needs_cli`;
#: it closes the symbol stage (round 3, `component_check.BLOCKING_CHECKS`).
NO_CLI_FILE_READ = Check('SKIP', 'File del simbolo: lettura in KiCad', 'Richiede KiCad CLI.',
                         code='kicad_symbol_file_read', needs_cli=True)
#: CONTRACT CHANGED 2026-11-15 (round 4, item 2): a run that ENDED must have
#: concluded every check an installation needs (`component_check.missing_required`,
#: now judged on finished runs too), and the service's table requires the
#: footprint file's and pin-pad's OWN outcome rows (a SKIP «file in uso» does
#: not conclude them). A real `verify_component` run always emits both and
#: ends with the manual checks; the scripted runs that stand for a finished
#: run carry them through `finished`, their assertions unchanged.
FILE_ROWS = REQUIRED_ROWS[:2]


def finished(*rows):
    """A run that ended: the footprint file's and pin-pad's own rows, `rows`, then the manual checks."""
    return [*FILE_ROWS, *rows, *(() if MANUAL in rows else (MANUAL,))]


#: The panel's row of a check that stopped on an internal failure after some results (I3).
CRASHED = Check('SKIP', 'Controllo del componente', 'Interrotto dopo 3 controlli (RuntimeError).',
                remedy='Usa «Controlla di nuovo».', code='check_crashed')


class Script:
    """A scripted `verify_component`: emits `checks`, counts its runs, can be held open."""

    def __init__(self, monkeypatch, checks, hold=False):
        self.checks, self.runs = list(checks), 0
        self.gate = threading.Event()
        if not hold:
            self.gate.set()
        monkeypatch.setattr(ccd, "verify_component", self)

    def __call__(self, *, emit, **_):
        self.runs += 1
        self.gate.wait(5)
        for check in self.checks:
            emit(check)


def pump(seconds=0.0):
    """Process events for `seconds` (at least once)."""
    end = time.monotonic() + seconds
    while True:
        QApplication.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.01)


def wait_idle(window, limit=5.0):
    """Wait until the check of step 1 is not running."""
    end = time.monotonic() + limit
    while window.kicad_check_panel.is_running() and time.monotonic() < end:
        pump(0.01)
    assert not window.kicad_check_panel.is_running()
    pump()


def wait_until(condition, limit=5.0):
    """Pump events until `condition()` holds or `limit` seconds pass; return its last value.

    Review 2026-11-15 (margins): a fixed pump of the debounce plus 0.2 s was
    a race on a loaded machine; a positive waits for its event with a deadline.
    """
    end = time.monotonic() + limit
    while not condition() and time.monotonic() < end:
        pump(0.01)
    return condition()


def assert_not_started(window):
    """A negative of the automatic start: nothing runs and nothing is queued to run."""
    assert not window.kicad_check_panel.is_running()
    timer = getattr(window, "_kicad_check_timer", None)
    assert timer is None or not timer.isActive()


@pytest.fixture
def guided(window, monkeypatch, tmp_path):
    """A generated project, the real gate and automatic start, the KiCad tab not yet shown."""
    _project(window, monkeypatch, tmp_path)
    use_real_kicad_check(window)
    window.tabs.setCurrentIndex(gui.TAB_PROJECT)
    window._refresh_output_actions()
    return window


def enter_kicad_tab(window):
    """Show the KiCad tab and let the automatic start fire."""
    window.tabs.setCurrentIndex(gui.TAB_KICAD)
    pump(window.KICAD_CHECK_DELAY_MS / 1000 + 0.2)


# ── the gate ──────────────────────────────────────────────────────────────

def test_no_installation_before_a_check_of_these_files(guided):
    assert guided._kicad_check_state() == "none"
    assert not guided.btn_export.isEnabled() and not guided.action_install_global.isEnabled()
    assert "Prima controlla il componente" in guided.btn_export.toolTip()
    # The preview in KiCad does not wait for the check (negative twin).
    assert guided.btn_test_kicad.isEnabled()


def test_entering_the_tab_checks_and_a_clean_outcome_offers_the_recommended_installation(guided, monkeypatch):
    script = Script(monkeypatch, finished(OK, READ, FILE_READ), hold=True)
    enter_kicad_tab(guided)
    assert wait_until(lambda: script.runs == 1) and guided.kicad_check_panel.is_running()
    assert not guided.btn_export.isEnabled()
    assert "in corso" in guided.label_install_advice.text()
    script.gate.set()
    wait_idle(guided)
    assert guided._kicad_check_state() == "ok"
    assert guided.btn_export.isEnabled() and guided.btn_install_global.isEnabled()
    assert guided.btn_export.objectName() == "primary"
    assert guided.label_install_advice.text() == "Pronto: installa il componente nella libreria."


def test_warnings_allow_the_installation_without_highlighting_it(guided, monkeypatch):
    Script(monkeypatch, finished(OK, READ, FILE_READ, WARN))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "warnings"
    assert guided.btn_export.isEnabled() and guided.btn_export.objectName() != "primary"
    # CONTRACT CHANGED 2026-11-15 (review P3): an importance-2 warning reads
    # «DA RIVEDERE PRIMA DELL’USO» in step 1, so step 3 says to review before
    # USE, never «va bene per prove e prototipi» beside it.
    assert guided.kicad_check_panel.verdict_key == "DA RIVEDERE PRIMA DELL’USO"
    assert "prima di usarlo" in guided.label_install_advice.text()
    assert "prototipi" not in guided.label_install_advice.text()


def test_an_error_keeps_the_installation_off_with_its_reason(guided, monkeypatch):
    Script(monkeypatch, [OK, ERROR])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "blocking"
    assert not guided.btn_export.isEnabled() and not guided.action_export.isEnabled()
    assert "errori" in guided.label_install_advice.text()
    assert guided.kicad_check_panel.verdict_key == "NON USARE"


def test_a_stopped_check_never_unlocks_the_installation(guided, monkeypatch):
    script = Script(monkeypatch, [OK, OK], hold=True)
    enter_kicad_tab(guided)
    guided.kicad_check_panel.stop_button.click()
    script.gate.set()
    wait_idle(guided)
    assert guided._kicad_check_state() == "aborted"
    assert not guided.btn_export.isEnabled() and "interrotto" in guided.label_install_advice.text()


def test_a_failing_checking_tool_never_blocks_the_installation(guided, monkeypatch):
    """Review H1: a timeout or a crash of the CHECK is «inconclusive», never «do not use».

    Negative twin: KiCad reporting the file unreadable stays an error
    (`test_an_error_keeps_the_installation_off_with_its_reason`).
    """
    # CONTRACT CHANGED 2026-11-15 (review M6): the tool failed on ONE read,
    # another read ended; when every read fails see
    # `test_no_kicad_read_with_kicad_cli_present_asks_for_a_new_check`.
    # CONTRACT CHANGED 2026-11-15 (round 2, R4): the read that failed is the
    # APPLIED symbol's, which no installation copies; the footprint and the
    # symbol file were read. A failed read of a copied file is «unread»
    # (`test_each_file_the_installation_copies_must_be_read_by_kicad`).
    Script(monkeypatch, finished(OK, READ, FILE_READ, SYMBOL_TOOL_FAILED, MANUAL))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "inconclusive"
    assert guided.btn_export.isEnabled() and guided.btn_export.objectName() != "primary"
    assert "non sono stati eseguiti" in guided.label_install_advice.text()


def test_a_check_that_could_not_run_asks_for_a_new_one(guided, monkeypatch):
    """A crash of the check is not an error of the component, but nothing was verified.

    Final verification 2026-11-15 (1): «inconclusive» allows the installation
    when some KiCad reads are missing, never when no check ran at all.
    Negative twin: `test_a_failing_checking_tool_never_blocks_the_installation`.
    """
    def crash(**_):
        raise RuntimeError("internal failure")
    monkeypatch.setattr(ccd, "verify_component", crash)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided.kicad_check_panel.verdict_key == "ESITO NON CONCLUSIVO"
    assert not guided.kicad_check_panel.blocking
    assert guided._kicad_check_state() == "failed" and not guided.btn_export.isEnabled()
    assert "Controlla di nuovo" in guided.label_install_advice.text()


def test_never_installed_and_manual_checks_still_reach_ready(guided, monkeypatch):
    """Review M1: the row «not installed yet» and the manual checks do not keep a sound part from «ok»."""
    Script(monkeypatch, finished(OK, READ, FILE_READ, MANUAL, LIBRARY))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "ok"
    assert guided.label_install_advice.text() == "Pronto: installa il componente nella libreria."
    assert guided.btn_export.objectName() == "primary"
    # Step 1 reads like step 3: the row is listed, the verdict leaves it out.
    assert guided.kicad_check_panel.verdict_key == "PRONTO PER LE PROVE"
    assert any("non ancora installato" in item.text for item in guided.kicad_check_panel.items)
    # Negative twin: a real point to review keeps the warning state.
    Script(monkeypatch, finished(OK, READ, FILE_READ, MANUAL, LIBRARY, WARN))
    guided._verify_component()
    wait_idle(guided)
    assert guided._kicad_check_state() == "warnings" and guided.btn_export.objectName() != "primary"


# ── stale outcome and automatic restart ──────────────────────────────────

def test_a_changed_file_makes_the_outcome_stale_and_the_check_starts_again(guided, monkeypatch):
    import os
    script = Script(monkeypatch, finished(OK, READ, FILE_READ))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided.btn_export.isEnabled() and script.runs == 1
    footprint = guided._project_dir() / f"{guided._component_name()}.pretty" / f"{guided._component_name()}.kicad_mod"
    stat = footprint.stat()
    os.utime(footprint, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    guided._refresh_output_actions()
    assert not guided.btn_export.isEnabled()
    # Review P4: on the KiCad tab step 3 says the check restarts by itself,
    # as step 1 does, not «Prima controlla il componente».
    assert guided.label_install_advice.text() == guided.KICAD_CHECK_RESTARTING_REASON
    assert "riparte" in guided.kicad_check_panel.verdict.text()
    assert wait_until(lambda: script.runs == 2)
    wait_idle(guided)
    assert script.runs == 2 and guided.btn_export.isEnabled()


def test_a_finished_check_of_unchanged_files_never_restarts(guided, monkeypatch):
    """Negative twin: refreshes and tab switches do not loop the check."""
    script = Script(monkeypatch, [OK, READ, FILE_READ])
    enter_kicad_tab(guided)
    wait_idle(guided)
    for _ in range(3):
        guided._refresh_output_actions()
        guided.tabs.setCurrentIndex(gui.TAB_PROJECT)
        enter_kicad_tab(guided)
    assert script.runs == 1
    assert_not_started(guided)


def test_an_installation_redoes_only_the_library_rows_and_keeps_the_other_installation(guided, monkeypatch):
    """Review M2: an installation changes no file, so no full run and no false «files changed».

    The library rows are redone alone; the other installation stays available.
    """
    from kicadgen.services import component_check
    script = Script(monkeypatch, finished(OK, READ, FILE_READ, MANUAL, LIBRARY))
    enter_kicad_tab(guided)
    wait_idle(guided)
    before = guided._kicad_files_fingerprint()
    installed = [Check('OK', 'Libreria KiCad', 'Installato e aggiornato.', code='kicad_library')]
    monkeypatch.setattr(component_check, "library_checks", lambda *a, **k: installed)
    guided._record_kicad_install("project", guided._project_dir() / "KiCadGen.pretty" / "x.kicad_mod")
    pump(guided.KICAD_CHECK_DELAY_MS / 1000 + 0.2)
    assert guided._kicad_files_fingerprint() == before and script.runs == 1
    assert_not_started(guided)
    assert guided._kicad_check_state() == "ok" and guided.btn_install_global.isEnabled()
    assert guided.label_install_advice.text() == "Installato e aggiornato. Reinstalla solo dopo una modifica."
    # Installed and current: the recommended installation no longer stands out.
    assert guided.btn_export.objectName() != "primary"


def test_no_automatic_start_outside_the_kicad_tab_or_while_work_runs(guided, monkeypatch):
    script = Script(monkeypatch, [OK, READ, FILE_READ])
    guided._refresh_output_actions()
    pump(guided.KICAD_CHECK_DELAY_MS / 1000 + 0.2)
    assert script.runs == 0                                    # not on the KiCad tab
    assert_not_started(guided)
    monkeypatch.setattr(guided, "_background_work_running", lambda: True)
    enter_kicad_tab(guided)
    assert script.runs == 0                                    # work in progress
    assert not guided.kicad_check_panel.is_running()
    assert "attesa" in guided.kicad_check_panel.verdict.text()
    monkeypatch.setattr(guided, "_background_work_running", lambda: False)
    guided._refresh_output_actions()                           # the work's end refreshes
    assert wait_until(lambda: script.runs == 1)
    wait_idle(guided)
    assert script.runs == 1


# ── every entry point is refused like the button ─────────────────────────

def test_a_direct_installation_is_refused_and_leads_to_the_check(guided, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[1:3]))
    Script(monkeypatch, [OK], hold=True)
    guided._install_project_library()
    assert warned and warned[-1][0] == "Controllo del componente"
    assert guided.tabs.currentIndex() == gui.TAB_KICAD


def test_the_last_recheck_before_copying_includes_the_check(guided, monkeypatch):
    """Review M1: a file rewritten during the install questions makes the outcome stale: nothing is copied."""
    import os
    Script(monkeypatch, finished(OK, READ, FILE_READ))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "ok"
    copied, warned = [], []
    monkeypatch.setattr(gui, "install_component_library", lambda *a, **k: copied.append(a))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[1:3]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    footprint = guided._project_dir() / f"{guided._component_name()}.pretty" / f"{guided._component_name()}.kicad_mod"
    real_refuse = guided._refuse_kicad_command
    calls = []

    def refuse_and_rewrite():
        calls.append(1)
        if len(calls) == 2:         # the loop's re-check, after the questions
            stat = footprint.stat()
            os.utime(footprint, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        return real_refuse()
    monkeypatch.setattr(guided, "_refuse_kicad_command", refuse_and_rewrite)
    monkeypatch.setattr(guided, "_confirm_install_plan_warnings", lambda *a, **k: True)
    guided._install_kicad_library(guided._project_dir() / "dest", project_library=True)
    assert copied == [] and warned and warned[-1][0] == "Controllo del componente"


def test_row_actions_wait_while_work_rewrites_the_files(guided, monkeypatch):
    """Review M2: the panel is not modal; its fixes and views wait for the work in progress."""
    from kicadgen.services.component_check import ACTION_REEXPORT_SYMBOL
    fix = Check('WARN', 'Simbolo esportato', 'Il file del simbolo è diverso.', importance=2,
                action=ACTION_REEXPORT_SYMBOL, code='symbol_file')
    Script(monkeypatch, [OK, READ, FILE_READ, fix])
    enter_kicad_tab(guided)
    wait_idle(guided)
    done = []
    guided.kicad_check_panel.actions[ACTION_REEXPORT_SYMBOL] = lambda: done.append(1) or "fatto"
    monkeypatch.setattr(guided, "_kicad_work_busy", lambda: True)
    guided.kicad_check_panel.action_buttons[ACTION_REEXPORT_SYMBOL].click()
    assert done == [] and "Attendi" in guided.kicad_check_panel.action_status.text()
    # Negative twin: no work, the fix runs and the check runs again through the window.
    monkeypatch.setattr(guided, "_kicad_work_busy", lambda: False)
    guided.kicad_check_panel.action_buttons[ACTION_REEXPORT_SYMBOL].click()
    assert done == [1]
    wait_idle(guided)
    assert "Verifica ripetuta automaticamente." in guided.kicad_check_panel.action_status.text()


def test_a_project_switch_throws_the_running_check_away(guided, monkeypatch):
    """Review M3/L2: no rows of the closing project, no «interrupted» state, kicad-cli killed."""
    killed = []
    monkeypatch.setattr(ccd, "stop_cli_commands", lambda: killed.append(1) or 0)
    script = Script(monkeypatch, [OK, ERROR], hold=True)
    enter_kicad_tab(guided)
    assert wait_until(lambda: script.runs == 1) and guided.kicad_check_panel.is_running()
    # Off the KiCad tab first: the forgotten run must not be restarted by
    # the automatic start, so the state after it is exactly «none».
    guided.tabs.setCurrentIndex(gui.TAB_PROJECT)
    guided.kicad_check_panel.forget()
    script.gate.set()
    wait_idle(guided)
    assert killed and guided.kicad_check_panel.results == []
    assert guided.kicad_check_panel.verdict_key is None and not guided.kicad_check_panel.aborted
    # Review 2026-11-15, M11: the two former assertions could not fail
    # (a state in («none», «running») and a module attribute not None).
    assert guided._kicad_check_state() == "none" and script.runs == 1
    assert_not_started(guided)


def test_the_metadata_come_before_the_check(guided, monkeypatch):
    """A metadata block: step 3 names the metadata, not the check (filling them comes first)."""
    reason = "Installazione bloccata: compila la descrizione."
    monkeypatch.setattr(guided, "_install_block_reason", lambda findings=None: reason)
    guided._refresh_output_actions()
    assert not guided.btn_export.isEnabled() and reason in guided.btn_export.toolTip()
    assert guided.label_install_advice.text() == reason
    # Negative twin: without the metadata block the check's own reason shows.
    monkeypatch.setattr(guided, "_install_block_reason", lambda findings=None: "")
    guided._refresh_output_actions()
    assert guided._kicad_check_state() == "none"
    assert guided.label_install_advice.text() == guided.i18n.text(guided.KICAD_CHECK_WAIT_REASONS["none"])


def test_a_direct_call_with_both_blocks_is_refused_for_the_metadata_first(guided, monkeypatch):
    """Order on a direct call (review 2026-11-15): the metadata refusal, then -- once filled -- the check's."""
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[1]))
    script = Script(monkeypatch, [OK, READ, FILE_READ], hold=True)
    monkeypatch.setattr(guided, "_install_block_reason", lambda findings=None: "metadati da compilare")
    assert guided._kicad_check_state() == "none"                 # the check blocks too
    assert guided._refuse_install_for_metadata()
    assert warned == ["Metadati del componente"]
    assert guided.tabs.currentIndex() == gui.TAB_PROJECT         # the check is not led to yet
    monkeypatch.setattr(guided, "_install_block_reason", lambda findings=None: "")
    assert guided._refuse_install_for_metadata()
    assert warned == ["Metadati del componente", "Controllo del componente"]
    assert guided.tabs.currentIndex() == gui.TAB_KICAD
    script.gate.set()
    wait_idle(guided)


# ── review 2026-11-15: KiCad reads (M6), a crash after some rows (M2) ────

def test_no_kicad_read_with_kicad_cli_present_asks_for_a_new_check(guided, monkeypatch):
    """M6 positive: kicad-cli was there (no `needs_cli` row) and every read skipped -> blocked, repeatable."""
    Script(monkeypatch, finished(OK, TOOL_FAILED, SYMBOL_TOOL_FAILED, MANUAL))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "unread"
    assert not guided.btn_export.isEnabled() and not guided.btn_install_global.isEnabled()
    assert guided.label_install_advice.text() == guided.i18n.text(guided.KICAD_CHECK_WAIT_REASONS["unread"])
    # Not permanent: a run in which KiCad reads each copied file lifts it
    # (round 2, R4: the footprint AND the symbol file).
    Script(monkeypatch, finished(OK, READ, FILE_READ, SYMBOL_TOOL_FAILED, MANUAL))
    guided._verify_component()
    wait_idle(guided)
    assert guided._kicad_check_state() == "inconclusive" and guided.btn_export.isEnabled()


def test_without_kicad_cli_the_skipped_reads_still_allow_the_installation(guided, monkeypatch):
    """M6 negative: no kicad-cli at all (`needs_cli`) -> nothing to repeat, allowed as before."""
    Script(monkeypatch, finished(OK, NO_CLI, NO_CLI_READ, MANUAL))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "inconclusive"
    assert guided.btn_export.isEnabled()
    assert "non sono stati eseguiti" in guided.label_install_advice.text()


def test_a_check_that_crashed_after_some_rows_allows_the_installation_and_says_so(guided, monkeypatch):
    """M2: «check_crashed» is a failure of the tool after results -- allowed, incomplete, steps 1 and 3 agree.

    Negative twin: nothing verified («check_failed») stays refused,
    `test_a_check_that_could_not_run_asks_for_a_new_one`; a crash before the
    required checks stays refused too,
    `test_a_crash_before_the_required_checks_keeps_the_installation_off`.
    """
    # CONTRACT CHANGED 2026-11-15 (round 2, R1): «crashed» allows the
    # installation only after EVERY required check concluded
    # (`component_check.REQUIRED_FOR_INSTALL`); the script carries them --
    # round 3: the STEP import and the DRC too (`IMPORT_3D`, `DRC_OK`).
    Script(monkeypatch, [CLI_OK, *REQUIRED_ROWS, READ, FILE_READ, IMPORT_3D, DRC_OK, CRASHED])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crashed"
    assert guided.btn_export.isEnabled() and guided.btn_export.objectName() != "primary"
    assert guided.kicad_check_panel.verdict_key == "ESITO NON CONCLUSIVO"
    assert "errore interno" in guided.label_install_advice.text()
    assert "puoi installare" in guided.label_install_advice.text()


# ── review 2026-11-15, P3/P4: step 3 says what step 1 says ───────────────

def test_installed_and_current_is_said_with_warnings_too(guided, monkeypatch):
    """P3: the library rows say installed and up to date -> step 3 says so beside what to review."""
    current = Check('OK', 'Libreria KiCad', 'Installato e aggiornato.', code='kicad_library')
    Script(monkeypatch, finished(OK, READ, FILE_READ, WARN, current))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "warnings"
    text = guided.label_install_advice.text()
    assert text.startswith("Installato e aggiornato.") and "prima di usarlo" in text.lower()
    # Negative twin: not installed yet -> no «Installato» claim.
    Script(monkeypatch, finished(OK, READ, FILE_READ, WARN, LIBRARY))
    guided._verify_component()
    wait_idle(guided)
    assert "Installato" not in guided.label_install_advice.text()


def test_off_the_kicad_tab_the_check_waits_for_the_tab_and_says_so(guided, monkeypatch):
    """P4: «riparte…» only where it restarts; elsewhere step 1 says it waits for the tab, step 3 asks for it."""
    import os
    script = Script(monkeypatch, [OK, READ, FILE_READ])
    enter_kicad_tab(guided)
    wait_idle(guided)
    guided.tabs.setCurrentIndex(gui.TAB_PROJECT)
    footprint = guided._project_dir() / f"{guided._component_name()}.pretty" / f"{guided._component_name()}.kicad_mod"
    stat = footprint.stat()
    os.utime(footprint, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    guided._refresh_output_actions()
    assert "ripartirà quando apri questa scheda" in guided.kicad_check_panel.verdict.text()
    assert guided.label_install_advice.text() == guided.i18n.text(guided.KICAD_CHECK_WAIT_REASONS["none"])
    pump(guided.KICAD_CHECK_DELAY_MS / 1000 + 0.2)
    assert script.runs == 1
    assert_not_started(guided)
    # Positive twin: the tab shown -> it restarts, and both steps say so.
    guided.tabs.setCurrentIndex(gui.TAB_KICAD)
    assert guided.label_install_advice.text() == guided.KICAD_CHECK_RESTARTING_REASON
    assert wait_until(lambda: script.runs == 2)
    wait_idle(guided)


# ── review 2026-11-15, M11: the check is not work in progress ────────────

def test_a_project_switch_through_the_window_kills_and_forgets_the_running_check(guided, monkeypatch):
    killed = []
    monkeypatch.setattr(ccd, "stop_cli_commands", lambda: killed.append(1) or 0)
    script = Script(monkeypatch, [OK, READ, FILE_READ, ERROR], hold=True)
    enter_kicad_tab(guided)
    assert wait_until(lambda: script.runs == 1) and guided.kicad_check_panel.is_running()
    # The contract: a running check never holds the project (it is thrown away).
    assert not guided._background_work_running() and guided._can_leave_project()
    guided._switch_project("Altro", guided._general_project_state)
    script.gate.set()
    wait_idle(guided)
    panel = guided.kicad_check_panel
    assert killed and panel.results == [] and panel.verdict_key is None and not panel.aborted
    assert guided._kicad_check_state() == "none"


def test_closing_the_window_kills_the_running_check(guided, monkeypatch):
    killed = []
    monkeypatch.setattr(ccd, "stop_cli_commands", lambda: killed.append(1) or 0)
    script = Script(monkeypatch, [OK, READ, FILE_READ, ERROR], hold=True)
    enter_kicad_tab(guided)
    assert wait_until(lambda: script.runs == 1) and guided.kicad_check_panel.is_running()
    assert guided.close()                                   # not refused by the running check
    assert killed
    script.gate.set()
    wait_idle(guided)
    panel = guided.kicad_check_panel
    assert panel.results == [] and panel.verdict_key is None and not panel.aborted


# ── the panel in the tab ─────────────────────────────────────────────────

def test_the_tab_does_not_offer_to_go_to_itself(guided, monkeypatch):
    Script(monkeypatch, [READ, FILE_READ, LIBRARY])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert ACTION_OPEN_KICAD_TAB not in guided.kicad_check_panel.action_buttons
    assert any("non ancora installato" in item.text for item in guided.kicad_check_panel.items)


def test_the_panel_says_check_now_then_check_again_and_hides_it_while_running(qapp, monkeypatch):
    script = Script(monkeypatch, [OK], hold=True)
    panel = ccd.ComponentCheckPanel()
    try:
        assert panel.rerun_button.text() == "Controlla ora"
        panel.run({})
        assert not panel.rerun_button.isVisibleTo(panel) and panel.stop_button.isVisibleTo(panel)
        script.gate.set()
        end = time.monotonic() + 5
        while panel.is_running() and time.monotonic() < end:
            pump(0.01)
        assert panel.rerun_button.isVisibleTo(panel) and panel.rerun_button.text() == "Controlla di nuovo"
        panel.show_idle("I file sono cambiati")
        assert panel.rerun_button.text() == "Controlla ora" and panel.verdict_key is None
    finally:
        panel.deleteLater()
        pump()


def test_the_rows_area_takes_the_rows_height_without_an_empty_band(qapp, monkeypatch):
    """The area is measured at the rows' real width: no band below few rows, a scroll past the bound."""
    Script(monkeypatch, [OK, WARN])
    panel = ccd.ComponentCheckPanel()
    panel.resize(900, 600)
    panel.show()
    try:
        panel.run({})
        end = time.monotonic() + 5
        while panel.is_running() and time.monotonic() < end:
            pump(0.01)
        pump(0.05)
        rows = [panel.items_layout.itemAt(i).widget() for i in range(panel.items_layout.count())]
        used = rows[-1].geometry().bottom() + 1
        assert abs(panel.items_scroll.height() - used) <= 4
        many = [Check('WARN', f'Controllo {i}', f'Dettaglio {i}', code=f'c{i}') for i in range(60)]
        Script(monkeypatch, many)
        panel.run({})
        end = time.monotonic() + 5
        while panel.is_running() and time.monotonic() < end:
            pump(0.01)
        assert panel.items_scroll.height() == panel.ITEMS_MAX_HEIGHT
    finally:
        panel.close()
        panel.deleteLater()
        pump()


def test_the_outcome_follows_a_language_switch(guided, monkeypatch):
    Script(monkeypatch, finished(OK, READ, FILE_READ, WARN))
    enter_kicad_tab(guided)
    wait_idle(guided)
    try:
        guided.language_combo.setCurrentIndex(guided.language_combo.findData("en"))
        assert guided.kicad_check_panel.rerun_button.text() == "Check again"
        assert guided.label_install_advice.text().startswith("You can install")
    finally:
        guided.language_combo.setCurrentIndex(guided.language_combo.findData("it"))


def test_end_to_end_with_the_real_check_without_kicad_cli(guided, monkeypatch):
    """The real `verify_component`, kicad-cli missing (as on the GitHub runner), then an installation.

    Final verification 2026-11-15 (10): the scripted checks of the other tests
    cannot see a real flow that never leaves «none» or «stale» (a file the
    check itself rewrote would loop it). Here the real check runs once, ends
    inconclusive (KiCad reads not run), allows the installation, and does not
    start again by itself.
    """
    from kicadgen.services import component_check
    monkeypatch.setattr(component_check, "find_kicad_cli", lambda: None)
    runs = []
    real = ccd.verify_component

    def counted(**kwargs):
        runs.append(1)
        return real(**kwargs)
    monkeypatch.setattr(ccd, "verify_component", counted)
    enter_kicad_tab(guided)
    wait_idle(guided, limit=60)
    assert runs == [1] and guided._kicad_check_state() == "inconclusive"
    assert any(item.rank == ccd.RANK_NOT_RUN for item in guided.kicad_check_panel.items)
    pump(guided.KICAD_CHECK_DELAY_MS / 1000 + 0.3)
    assert runs == [1]                         # the check did not invalidate itself
    class Copied(Exception):
        """Stops the installation at the copy: what follows is not this test's contract."""

    def copy(*args, **kwargs):
        raise Copied()
    monkeypatch.setattr(gui, "install_component_library", copy)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(guided, "_confirm_install_plan_warnings", lambda *a, **k: True)
    with pytest.raises(Copied):
        guided._install_kicad_library(guided._project_dir() / "dest", project_library=True)


def test_apply_to_files_says_its_name_and_whether_something_is_left_to_apply(guided, monkeypatch):
    """User 2026-11-15: the icon-only ✓ «non è chiaro»; the button says its name and the state is beside it."""
    button = guided.btn_apply_metadata
    assert button.text() == "Applica ai file" and button.accessibleName() == "Applica ai file"
    monkeypatch.setattr(guided, "_metadata_not_applied", lambda: True)
    guided._refresh_output_actions()
    assert guided.label_metadata_apply_state.text() == "Metadati modificati: non sono ancora nei file."
    assert button.objectName() == "primary" and button.isEnabled()
    # Negative twin: nothing to apply -> plain button, the state says so, still usable.
    monkeypatch.setattr(guided, "_metadata_not_applied", lambda: False)
    guided._refresh_output_actions()
    assert guided.label_metadata_apply_state.text() == "I metadati sono già nei file."
    assert button.objectName() != "primary" and button.isEnabled()


def test_each_step_command_has_its_own_help_window(guided, monkeypatch):
    """User 2026-11-15: an ⓘ beside each option explains it in a window; the sentence under step 3 is gone."""
    from PyQt6.QtWidgets import QDialog, QLabel
    shown = []

    def fake_exec(dialog):
        shown.append((dialog.windowTitle(), " ".join(label.text() for label in dialog.findChildren(QLabel))))
        return 0
    monkeypatch.setattr(QDialog, "exec", fake_exec)
    expected = {"preview": ("Apri in KiCad", ".kicad-preview"),
                "project": ("Installa nel progetto KiCad (consigliato)", "${KIPRJMOD}"),
                "global": ("Installa come libreria globale", ".kicadgen-backup")}
    for key, (title, fact) in expected.items():
        button = guided._kicad_step_help_buttons[key]
        assert button.accessibleName() == f"Aiuto: {title}"
        button.click()
        assert shown[-1][0] == title and fact in shown[-1][1]
    # Negative twin: each window explains its own command, never another's.
    assert "${KIPRJMOD}" not in shown[2][1] and ".kicadgen-backup" not in shown[1][1]
    tab = guided.tabs.widget(gui.TAB_KICAD)
    assert not any("La libreria di progetto viaggia" in label.text() for label in tab.findChildren(QLabel))


def _ai_window(monkeypatch, proposals):
    """A stand-in of the AI pin-type window that is confirmed with `proposals` selected."""
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui import pin_type_dialog

    class Window(QObject):
        analysis_started = pyqtSignal()
        analysis_finished = pyqtSignal()

        def __init__(self, args, parent, tx, actions=None):
            super().__init__()

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected(self):
            return proposals

        def deleteLater(self):
            pass
    monkeypatch.setattr(pin_type_dialog, "PinTypeDialog", Window)


def test_the_ai_pin_types_are_saved_at_once_on_confirm(guided, monkeypatch, tmp_path):
    """User decision «A» 2026-11-15: the ✓ of the AI window saves the types in the symbol, no «Applica» needed."""
    guided._refresh_symbol_tab()
    pin = guided.symbol_tab.rows()[0]["pin"]
    monkeypatch.setattr(guided, "_selected_documents",
                        lambda: ([tmp_path / "sheet.pdf"], {"sheet.pdf": ["datasheet"]}))
    monkeypatch.setattr(guided, "_ensure_api_key", lambda *a, **k: True)
    assert not guided.symbol_tab.has_pending_edits()
    _ai_window(monkeypatch, [{"pin": pin, "electrical_type": "power_in", "source": "sheet.pdf",
                              "reason": "Table 1: supply input."}])
    guided._suggest_pin_types()
    saved = {row["pin"]: row for row in guided.symbol_tab.rows()}
    assert saved[pin]["electrical_type"] == "power_in" and saved[pin]["electrical_type_reviewed"]
    assert not guided.symbol_tab.has_pending_edits()
    assert guided.symbol_tab.status.text() == "Tipi elettrici salvati nel simbolo."


def test_unsaved_edits_made_by_hand_are_never_applied_behind_the_users_back(guided, monkeypatch, tmp_path):
    """Negative twin: other unapplied edits were waiting -> the types join the draft, the Symbol tab says «Applica»."""
    guided._refresh_symbol_tab()
    rows = guided.symbol_tab.rows()
    guided.symbol_tab.table.item(1, 1).setText("HAND")          # an edit made by hand, not applied
    assert guided.symbol_tab.has_pending_edits()
    monkeypatch.setattr(guided, "_selected_documents",
                        lambda: ([tmp_path / "sheet.pdf"], {"sheet.pdf": ["datasheet"]}))
    monkeypatch.setattr(guided, "_ensure_api_key", lambda *a, **k: True)
    _ai_window(monkeypatch, [{"pin": rows[0]["pin"], "electrical_type": "power_in", "source": "sheet.pdf",
                              "reason": "Table 1: supply input."}])
    guided._suggest_pin_types()
    assert guided.symbol_tab.has_pending_edits()
    assert guided.symbol_tab.rows()[1]["name"] != "HAND"        # nothing applied
    assert guided.tabs.currentIndex() == gui.TAB_SYMBOL
    assert "Applica" in guided.symbol_tab.status.text()


def test_remedy_icons_appear_only_when_there_is_something_to_fix(guided, monkeypatch):
    """User 2026-11-15: «Rimuovi il modello STEP obsoleto» stood there with «nessun modello obsoleto»."""
    guided._refresh_output_actions()
    assert not (not guided.btn_remove_obsolete_model.isHidden())
    assert not (not guided.btn_update_symbol_file.isHidden())
    # Positive twin: a stale STEP shows its remedy, a stale symbol file its own.
    monkeypatch.setattr(guided, "_kicad_block_code", lambda: "step_stale")
    guided._refresh_kicad_remedies()
    assert (not guided.btn_remove_obsolete_model.isHidden()) and guided.btn_remove_obsolete_model.isEnabled()
    monkeypatch.setattr(guided, "_kicad_block_code", lambda: "symbol_text")
    guided._refresh_kicad_remedies()
    assert (not guided.btn_update_symbol_file.isHidden())
    assert not (not guided.btn_remove_obsolete_model.isHidden())


def test_a_stale_installed_symbol_asks_for_the_reexport_before_the_reinstall(guided, monkeypatch):
    """A library row whose remedy is «Riesporta il simbolo»: step 3 names it, not a bare «reinstallalo».

    Negative twin: an installed copy that is merely older (no re-export
    action) keeps the plain reinstall advice.
    """
    from kicadgen.services.component_check import ACTION_REEXPORT_SYMBOL
    stale_symbol = Check('WARN', 'Libreria KiCad', 'Il simbolo installato non corrisponde.', importance=2,
                         category='final', action=ACTION_REEXPORT_SYMBOL, code='kicad_library')
    Script(monkeypatch, finished(OK, READ, FILE_READ, stale_symbol))
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "ok"
    assert guided.label_install_advice.text() == guided.i18n.text(guided.INSTALL_SYMBOL_STALE_TEXT)
    older = Check('WARN', 'Libreria KiCad', 'La copia installata è diversa.', importance=2,
                  category='final', code='kicad_library')
    Script(monkeypatch, finished(OK, READ, FILE_READ, older))
    guided._verify_component()
    wait_idle(guided)
    assert "Riesporta" not in guided.label_install_advice.text()
    assert "reinstallalo" in guided.label_install_advice.text()
