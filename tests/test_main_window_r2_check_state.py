"""@brief Round 2 of the 2026-11-15 review: when a stopped or partial component check allows an installation.

Rules, each with a case that must trigger and one that must not:

- R1: a check that stopped on an internal error allows the installation
  only when it concluded every check an installation needs
  (`component_check.REQUIRED_FOR_INSTALL`); the service always emits the
  «KiCad CLI» row first, so the number of rows proves nothing.
- R4: «unread» asks KiCad's read of EACH file the installation copies -- the
  footprint, and the symbol file when it exists -- not of any one of them.
- R5: a persistent failure of kicad-cli (the same declared failure in two
  consecutive runs) is judged like kicad-cli absent: allowed, with the cause
  named. A single occurrence, or a failure no row declares, keeps
  «Controlla di nuovo». CONTRACT CHANGED 2026-11-15 (round 3): every declared
  tool-side cause may persist (tests/test_main_window_r3_cli_persistence.py),
  and the same internal crash twice is NEVER «cli_unusable».

No AI and no kicad-cli: `verify_component` is replaced by a scripted one.
"""

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import component_check_dialog as ccd  # noqa: E402
from kicadgen.gui import main_window as gui  # noqa: E402
from kicadgen.services import component_check  # noqa: E402
from kicadgen.services.component_check import Check  # noqa: E402
from tests.test_kicad_guided_tab import (CLI_OK, FILE_READ, MANUAL, NO_CLI, NO_CLI_FILE_READ,  # noqa: E402,F401
                                         NO_CLI_READ, OK, READ, REQUIRED_ROWS, TOOL_FAILED, Script, enter_kicad_tab, guided,
                                         wait_idle)
from tests.test_generated_files_alignment import NAME  # noqa: E402
from tests.test_kicad_guided_tab import FILE_ROWS  # noqa: E402

# CONTRACT CHANGED 2026-11-15 (round 4, item 2): a run that ENDED must have
# concluded every check an installation needs (`component_check.missing_required`
# now judges finished runs too, «incomplete» / «file_busy»), and the service's
# table requires the footprint file's and pin-pad's OWN outcome rows. A real
# run always emits both; the finished runs scripted here carry them
# (`FILE_ROWS`), their assertions unchanged.


def _start_failed(code):
    """The service's SKIP row of a KiCad read whose kicad-cli could not be launched (OSError).

    Uses the service's own declared field `Check.cli_failure` (round 2, R5;
    the service sets it in tests/test_component_check_r2_cli_failure.py).
    """
    return Check('SKIP', 'Lettura in KiCad', 'kicad-cli non avviato: accesso negato', code=code,
                 cli_failure=gui.MainWindow.CLI_FAILURE_START)


def _crash(error="RuntimeError: boom", count=4):
    """The panel's «check_crashed» row exactly as the worker builds it (its recipe carries the error)."""
    return ccd._self_check(ccd.CODE_CHECK_CRASHED, component_check.tx(
        ccd.CHECK_CRASHED_DETAIL, error=error, count=count))


def _run_again(window, monkeypatch, checks):
    """A second run of the check, by «Controlla di nuovo», with scripted rows."""
    Script(monkeypatch, checks)
    window._verify_component()
    wait_idle(window)


def _blocked(window):
    """Neither installation is offered."""
    return not window.btn_export.isEnabled() and not window.btn_install_global.isEnabled()


# ── R1: a stopped check and the required checks ─────────────────────────────

def test_a_crash_right_after_the_cli_row_keeps_the_installation_off(guided, monkeypatch):
    """R1 positive: the real service's first row is «KiCad CLI»; a crash after it verified nothing."""
    Script(monkeypatch, [CLI_OK, _crash(count=1)])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crash_incomplete"
    assert _blocked(guided)
    assert guided.label_install_advice.text() == guided.i18n.text(
        guided.KICAD_CHECK_WAIT_REASONS["crash_incomplete"])
    assert "errore interno" in guided.label_install_advice.text()


def test_a_crash_before_the_required_checks_keeps_the_installation_off(guided, monkeypatch):
    """R1 positive, reproduced without kicad-cli: crash before pin-pad -> blocked, «Controlla di nuovo»."""
    Script(monkeypatch, [NO_CLI, REQUIRED_ROWS[0], _crash(count=2)])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crash_incomplete"
    assert _blocked(guided) and "Controlla di nuovo" in guided.label_install_advice.text()


def test_a_crash_after_every_required_check_without_kicad_cli_allows_the_installation(guided, monkeypatch):
    """R1 negative: without kicad-cli the reads are not required; every other one concluded -> «crashed».

    CONTRACT CHANGED 2026-11-15 (round 3): the symbol stage must be closed
    too (`component_check.BLOCKING_CHECKS`, «symbol_error»): the skipped read
    of the symbol file is the row that closes it without kicad-cli.
    """
    Script(monkeypatch, [NO_CLI, *REQUIRED_ROWS, NO_CLI_READ, NO_CLI_FILE_READ, _crash(count=5)])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crashed"
    assert guided.btn_export.isEnabled()
    assert "errore interno" in guided.label_install_advice.text()
    assert "puoi installare" in guided.label_install_advice.text()


def test_a_real_worker_crash_after_its_first_row_is_never_allowed(guided, monkeypatch):
    """R1 with the panel's own worker: the verification emits the CLI row, then raises."""
    def crashing(*, emit, **_):
        emit(CLI_OK)
        raise RuntimeError("internal failure")
    monkeypatch.setattr(ccd, "verify_component", crashing)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert any(result.code == "check_crashed" for result in guided.kicad_check_panel.results)
    assert guided._kicad_check_state() == "crash_incomplete" and _blocked(guided)


# ── R4: each file the installation copies is read by KiCad ──────────────────

def test_each_file_the_installation_copies_must_be_read_by_kicad(guided, monkeypatch):
    """R4 positive: the symbol file read, the footprint read skipped -> «unread»; then the reverse."""
    Script(monkeypatch, [*FILE_ROWS, OK, TOOL_FAILED, FILE_READ, MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    skipped_symbol_file = Check('SKIP', 'File del simbolo: lettura in KiCad',
                                'kicad-cli non ha risposto entro 60 secondi.', code='kicad_symbol_file_read')
    _run_again(guided, monkeypatch, [*FILE_ROWS, OK, READ, skipped_symbol_file, MANUAL])
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    # Negative: both copied files read -> allowed.
    _run_again(guided, monkeypatch, [*FILE_ROWS, OK, READ, FILE_READ, MANUAL])
    assert guided._kicad_check_state() == "ok" and guided.btn_export.isEnabled()


def test_the_symbol_file_read_is_required_only_when_the_file_exists():
    """R4: the same rows, the symbol file present or not (one rule, `component_check.required_for_install`)."""
    rows = [OK, READ, MANUAL]
    assert gui.MainWindow._kicad_never_read_the_files(rows, has_symbol_file=True)
    assert not gui.MainWindow._kicad_never_read_the_files(rows, has_symbol_file=False)
    # Without kicad-cli nothing is read and nothing can be repeated: never «unread».
    assert not gui.MainWindow._kicad_never_read_the_files([OK, NO_CLI, NO_CLI_READ], has_symbol_file=True)


def test_the_required_flags_follow_the_files_of_the_run(guided):
    """has_step / has_symbol_file come from the files' fingerprint, cli_available from the rows."""
    files = guided._kicad_files_fingerprint()
    flags = guided._kicad_required_flags([OK], files)
    assert flags == {"cli_available": True, "has_step": True, "has_symbol_file": True}
    (guided._project_dir() / f"{NAME}.kicad_sym").unlink()
    flags = guided._kicad_required_flags([NO_CLI], guided._kicad_files_fingerprint())
    assert flags == {"cli_available": False, "has_step": True, "has_symbol_file": False}


# ── R5: persistent failures of kicad-cli ────────────────────────────────────

def _start_failure_run():
    """kicad-cli there, every KiCad read failed to start (an antivirus, a missing runtime)."""
    return [CLI_OK, *FILE_ROWS, OK, _start_failed('kicad_footprint_read'), _start_failed('kicad_symbol_read'),
            _start_failed('kicad_symbol_file_read'), MANUAL]


def test_a_kicad_cli_that_never_starts_is_judged_like_no_kicad_cli_on_the_second_run(guided, monkeypatch):
    """R5 positive: the same start failure in two runs -> allowed, the cause and its remedy named."""
    Script(monkeypatch, _start_failure_run())
    enter_kicad_tab(guided)
    wait_idle(guided)
    # A single occurrence may be transient: repeat it.
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    _run_again(guided, monkeypatch, _start_failure_run())
    assert guided._kicad_check_state() == "cli_unusable"
    assert guided.btn_export.isEnabled() and guided.btn_export.objectName() != "primary"
    advice = guided.label_install_advice.text()
    assert "kicad-cli non si avvia" in advice and "antivirus" in advice and "puoi installare" in advice.lower()
    # Not permanent: a run where KiCad reads every file lifts it.
    _run_again(guided, monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, FILE_READ, MANUAL])
    assert guided._kicad_check_state() == "ok"


def test_an_undeclared_tool_failure_is_never_a_persistent_cause(guided, monkeypatch):
    """R5 negative: a skipped read that declares no `cli_failure` stays «unread» run after run.

    CONTRACT CHANGED 2026-11-15 (round 3): this test said timeouts are never
    persistent. A timeout on EVERY read in two runs is now a declared,
    persistent cause (tests/test_main_window_r3_cli_persistence.py); what
    stays «unread» is a skipped read whose cause the service did not declare.
    """
    run = [CLI_OK, *FILE_ROWS, OK, TOOL_FAILED, MANUAL]
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "unread"
    _run_again(guided, monkeypatch, run)
    assert guided._kicad_check_state() == "unread" and _blocked(guided)


def test_a_start_failure_followed_by_a_timeout_is_not_persistent(guided, monkeypatch):
    """R5 negative: two different failures are not «the same cause»."""
    Script(monkeypatch, _start_failure_run())
    enter_kicad_tab(guided)
    wait_idle(guided)
    _run_again(guided, monkeypatch, [CLI_OK, *FILE_ROWS, OK, TOOL_FAILED, MANUAL])
    assert guided._kicad_check_state() == "unread"


def test_the_same_crash_twice_before_the_reads_stays_blocked_as_an_internal_error(guided, monkeypatch):
    """R1 negative of R5: the same internal crash in two runs is never «cli_unusable».

    CONTRACT CHANGED 2026-11-15 (round 3, generality audit): this test
    asserted the opposite -- two identical crashes before KiCad's reads
    allowed the installation as «cli_unusable». A crash of the program's own
    checker is not a fact about kicad-cli: it stays «crash_incomplete»,
    blocked, and step 3 says it is an internal error, never that KiCad did
    not read the files.
    """
    run = [CLI_OK, *REQUIRED_ROWS, _crash(error="TypeError: bad", count=4)]
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crash_incomplete" and _blocked(guided)
    _run_again(guided, monkeypatch, run)
    assert guided._kicad_check_state() == "crash_incomplete" and _blocked(guided)
    assert guided._kicad_failure_persistent is None
    advice = guided.label_install_advice.text()
    assert "errore interno" in advice and "Controlla di nuovo" in advice
    assert "KiCad non ha letto" not in advice and "letture saltate" not in advice


def test_the_same_crash_before_pin_pad_stays_blocked(guided, monkeypatch):
    """R5 negative: a persistent crash cannot lift a missing NON-KiCad check (pin-pad never ran)."""
    run = [CLI_OK, REQUIRED_ROWS[0], _crash(count=2)]
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    _run_again(guided, monkeypatch, run)
    assert guided._kicad_check_state() == "crash_incomplete" and _blocked(guided)


def test_a_different_crash_on_the_second_run_is_not_persistent(guided, monkeypatch):
    """R5 negative: another error (another declared value) is a new failure, not the same one."""
    Script(monkeypatch, [CLI_OK, *REQUIRED_ROWS, _crash(error="TypeError: bad")])
    enter_kicad_tab(guided)
    wait_idle(guided)
    _run_again(guided, monkeypatch, [CLI_OK, *REQUIRED_ROWS, _crash(error="KeyError: 'x'")])
    assert guided._kicad_check_state() == "crash_incomplete"


def test_a_project_switch_forgets_the_failure_history(guided, monkeypatch):
    """R5: the history is per project; one failure in each project is never «twice»."""
    Script(monkeypatch, _start_failure_run())
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_failure_last is not None
    guided._switch_project("Altro", guided._general_project_state)
    assert guided._kicad_failure_last is None and guided._kicad_failure_persistent is None
