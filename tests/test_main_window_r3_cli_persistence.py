"""@brief Round 3 of the 2026-11-15 review: a persistent kicad-cli failure, decided on declared fields only.

Rules, each with a case that must trigger and one that must not:

- Item 1: a run's failure signature is the set of (code, cli_failure,
  cli_exit_code) of its KiCad read rows when EVERY KiCad read failed on the
  tool side (`Check.cli_failure` in start / timeout / not_run /
  environment) and every read an installation needs has a row. Two
  consecutive runs with the same signature -> «cli_unusable» (allowed), the
  declared cause named in step 3. Different signatures, or one read that
  succeeded, stay «unread».
- Item 2: an internal crash of the checker («check_crashed») is never a
  signature and never «cli_unusable»: «crashed» when every required check
  concluded, otherwise «crash_incomplete», said as an internal error.
- Item 5: the reads required are the service's table
  (`component_check.required_reads`), a STEP read included as soon as the
  service's table holds it; the checks a crashed run must have concluded
  are the service's (`missing_required`), the STEP import among them; the
  window passes every fact it knows that the service's signature declares.

No AI and no kicad-cli: `verify_component` is replaced by a scripted one,
whose rows carry the service's declared fields (`Check.cli_failure`,
`Check.cli_exit_code`: 0 for an exit 0 that wrote nothing, the Windows
status as an unsigned integer for a crash).
"""

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import main_window as gui  # noqa: E402
from kicadgen.services import component_check  # noqa: E402
from kicadgen.services.component_check import Check  # noqa: E402
from tests.test_kicad_guided_tab import (CLI_OK, DRC_OK, FILE_READ, IMPORT_3D, MANUAL,  # noqa: E402,F401
                                         NO_CLI, NO_CLI_FILE_READ, NO_CLI_READ, OK, READ, REQUIRED_ROWS,
                                         Script, enter_kicad_tab, guided, wait_idle)
from tests.test_main_window_r2_check_state import _blocked, _crash, _run_again  # noqa: E402
from tests.test_kicad_guided_tab import FILE_ROWS  # noqa: E402

# CONTRACT CHANGED 2026-11-15 (round 4, item 2): a run that ENDED must have
# concluded every check an installation needs (`component_check.missing_required`
# now judges finished runs too, «incomplete» / «file_busy»), and the service's
# table requires the footprint file's and pin-pad's OWN outcome rows. A real
# run always emits both; the finished runs scripted here carry them
# (`FILE_ROWS`), their assertions unchanged.

#: Every KiCad read of a run with kicad-cli and a STEP (`MainWindow.KICAD_READ_CODES`).
READ_CODES = ("kicad_footprint_read", "kicad_symbol_read", "kicad_symbol_file_read", "kicad_3d_import")
#: Windows status codes as kicad-cli's process returns them: a missing
#: runtime DLL and an access violation.
DLL_NOT_FOUND = 0xC0000135
ACCESS_VIOLATION = 0xC0000005


def _tool_failed(code, failure, exit_code=None):
    """The service's SKIP row of a KiCad read that failed on the tool side, with its declared fields."""
    return Check('SKIP', 'Lettura in KiCad', 'lettura non verificata', code=code, cli_failure=failure,
                 cli_exit_code=exit_code)


def _failing_run(failure, exit_code=None, codes=READ_CODES):
    """kicad-cli there, every KiCad read failed the same declared way."""
    return [CLI_OK, *FILE_ROWS, OK, *(_tool_failed(code, failure, exit_code) for code in codes), MANUAL]


def _twice(window, monkeypatch, first, second):
    """Two consecutive runs; returns the state after the first one."""
    Script(monkeypatch, first)
    enter_kicad_tab(window)
    wait_idle(window)
    state = window._kicad_check_state()
    _run_again(window, monkeypatch, second)
    return state


# ── item 1: every declared tool-side cause may be persistent ────────────────

@pytest.mark.parametrize("failure, exit_code, words", [
    ("start", None, "kicad-cli non si avvia"),
    ("timeout", None, "non risponde mai in tempo"),
    ("not_run", DLL_NOT_FOUND, "codice 0xC0000135"),
    ("not_run", ACCESS_VIOLATION, "codice 0xC0000005"),
    ("not_run", 0, "senza produrre alcun risultato"),
    ("environment", None, "file di prova valido"),
])
def test_the_same_declared_failure_twice_is_judged_like_no_kicad_cli(guided, monkeypatch, failure,
                                                                      exit_code, words):
    """Positive: first run «unread» (may be transient), the same signature again -> allowed, cause named."""
    run = _failing_run(failure, exit_code)
    assert _twice(guided, monkeypatch, run, run) == "unread"
    assert guided._kicad_check_state() == "cli_unusable"
    assert guided.btn_export.isEnabled() and guided.btn_install_global.isEnabled()
    advice = guided.label_install_advice.text()
    assert words in advice and advice.startswith("Puoi installare")
    # Not permanent: a run where KiCad reads every required file lifts it.
    _run_again(guided, monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, FILE_READ, IMPORT_3D, MANUAL])
    assert guided._kicad_check_state() == "ok"


def test_two_different_exit_codes_are_not_the_same_cause(guided, monkeypatch):
    """Negative: a missing DLL, then an access violation -> two signatures, still «unread»."""
    _twice(guided, monkeypatch, _failing_run("not_run", DLL_NOT_FOUND),
           _failing_run("not_run", ACCESS_VIOLATION))
    assert guided._kicad_check_state() == "unread" and _blocked(guided)


def test_two_different_causes_are_not_persistent(guided, monkeypatch):
    """Negative: «environment» then «timeout» -> still «unread»."""
    _twice(guided, monkeypatch, _failing_run("environment"), _failing_run("timeout"))
    assert guided._kicad_check_state() == "unread" and _blocked(guided)


def test_one_read_that_succeeded_means_kicad_cli_works(guided, monkeypatch):
    """Negative: the footprint read OK, the symbol file read failed to start, twice -> «unread»."""
    run = [CLI_OK, *FILE_ROWS, OK, READ, *(_tool_failed(code, "start") for code in READ_CODES[1:]), MANUAL]
    _twice(guided, monkeypatch, run, run)
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    assert guided._kicad_failure_signature(run) is None


def test_a_mix_of_declared_causes_names_no_single_cause(guided, monkeypatch):
    """Positive with a mixed signature: allowed on the second run, the generic sentence, no invented cause."""
    run = [CLI_OK, *FILE_ROWS, OK, _tool_failed(READ_CODES[0], "start"), _tool_failed(READ_CODES[1], "timeout"),
           _tool_failed(READ_CODES[2], "start"), _tool_failed(READ_CODES[3], "start"), MANUAL]
    _twice(guided, monkeypatch, run, run)
    assert guided._kicad_check_state() == "cli_unusable"
    advice = guided.label_install_advice.text()
    assert advice == guided.i18n.text(guided.KICAD_PERSISTENT_CAUSE_TEXTS["tool"])


def test_a_required_read_without_a_row_is_no_signature(guided):
    """Negative: the symbol-file read was never reached -> no declared failure of every read."""
    run = [CLI_OK, *FILE_ROWS, OK, _tool_failed("kicad_footprint_read", "start"), MANUAL]
    guided._kicad_check_files = guided._kicad_files_fingerprint()
    assert guided._kicad_failure_signature(run) is None
    complete = _failing_run("start")
    assert guided._kicad_failure_signature(complete) == frozenset(
        (code, "start", None) for code in READ_CODES)


# ── item 2: an internal crash is never «cli_unusable» ───────────────────────

def test_a_crash_is_never_part_of_a_signature(guided):
    """Negative: every read failed to start, then the checker crashed -> no signature."""
    guided._kicad_check_files = guided._kicad_files_fingerprint()
    assert guided._kicad_failure_signature([*_failing_run("start"), _crash()]) is None


def test_the_same_crash_after_failed_reads_twice_is_not_cli_unusable(guided, monkeypatch):
    """Negative: reads failed to start AND the checker crashed, twice alike -> blocked, never allowed.

    Every blocking-capable check is concluded (the DRC skipped after the
    failed reads), so the crash rule passes on to the usual «unread».
    """
    drc_skipped = Check('SKIP', 'DRC', 'non eseguito', code='drc', cli_failure='start')
    run = [CLI_OK, *REQUIRED_ROWS, *(_tool_failed(code, "start") for code in READ_CODES), drc_skipped,
           _crash(error="TypeError: bad", count=9)]
    _twice(guided, monkeypatch, run, run)
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    assert guided._kicad_failure_persistent is None


def test_a_crash_after_every_required_check_and_read_allows_with_internal_error(guided, monkeypatch):
    """Positive twin: every required check concluded and every read made -> «crashed», allowed."""
    run = [CLI_OK, *REQUIRED_ROWS, READ, FILE_READ, IMPORT_3D, DRC_OK, _crash(count=9)]
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crashed" and guided.btn_export.isEnabled()
    advice = guided.label_install_advice.text()
    assert "errore interno" in advice and "KiCad non ha letto" not in advice


def test_a_crash_before_the_step_import_concluded_keeps_the_installation_off(guided, monkeypatch):
    """Negative twin (item 5): STEP and kicad-cli there, the crash came before the STEP import -> blocked."""
    run = [CLI_OK, *REQUIRED_ROWS, READ, FILE_READ, DRC_OK, _crash(count=8)]
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crash_incomplete" and _blocked(guided)
    assert "errore interno" in guided.label_install_advice.text()


# ── item 5: the service decides which reads and checks are required ─────────

def test_the_required_reads_are_the_services_table(guided):
    """The window's reads are `component_check.required_reads`: the copied files' reads."""
    flags = guided._kicad_required_flags([OK], guided._kicad_files_fingerprint())
    assert guided._kicad_required_reads(flags) == component_check.required_reads(
        cli_available=True, has_symbol_file=True)
    (guided._project_dir() / "HDR.kicad_sym").unlink()
    flags = guided._kicad_required_flags([OK], guided._kicad_files_fingerprint())
    assert guided._kicad_required_reads(flags) == frozenset({"kicad_footprint_read"})


def _with_step_read_required(monkeypatch):
    """A service whose table adds the STEP import to the reads of the copied files (one rule, there)."""
    def required_reads(*, cli_available, has_symbol_file, has_step):
        codes = {"kicad_footprint_read"} | ({"kicad_symbol_file_read"} if has_symbol_file else set())
        if has_step:
            codes.add("kicad_3d_import")
        return frozenset(codes) if cli_available else frozenset()
    monkeypatch.setattr(component_check, "required_reads", required_reads)


def test_a_step_read_the_service_requires_is_required_by_the_window(guided, monkeypatch):
    """Positive: the service's table holds the STEP import -> skipped means «unread»; read means allowed."""
    _with_step_read_required(monkeypatch)
    skipped = Check('SKIP', 'Importazione 3D in KiCad', 'non letto', code='kicad_3d_import')
    Script(monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, FILE_READ, skipped, MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    _run_again(guided, monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, FILE_READ, IMPORT_3D, MANUAL])
    assert guided._kicad_check_state() == "ok"


def test_the_step_read_is_not_required_without_a_step(guided, monkeypatch):
    """Negative: the same service rule, no STEP on disk -> the footprint and symbol reads suffice."""
    _with_step_read_required(monkeypatch)
    flags = guided._kicad_required_flags([OK], guided._kicad_files_fingerprint())
    assert "kicad_3d_import" in guided._kicad_required_reads(flags)
    for path in (guided._project_dir() / "HDR.3dshapes").glob("*"):
        path.unlink()
    flags = guided._kicad_required_flags([OK], guided._kicad_files_fingerprint())
    assert flags["has_step"] is False
    assert "kicad_3d_import" not in guided._kicad_required_reads(flags)


def test_only_the_flags_the_service_declares_are_passed():
    """The window passes what it knows; a keyword the rule does not declare is never sent."""
    seen = {}

    def narrow(*, cli_available, has_step):
        seen["narrow"] = (cli_available, has_step)
        return frozenset()

    def wide(results, **flags):
        seen["wide"] = (results, flags)
        return frozenset()

    flags = {"cli_available": True, "has_step": False, "has_symbol_file": True, "has_document": True}
    gui.MainWindow._call_with_flags(narrow, None, flags)
    gui.MainWindow._call_with_flags(wide, ["row"], flags)
    assert seen["narrow"] == (True, False)
    assert seen["wide"] == (["row"], flags)


def test_the_run_records_what_it_was_given(guided, monkeypatch):
    """The facts of the run's arguments are recorded with the service's own rules."""
    Script(monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, FILE_READ, MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_inputs == {"has_document": False, "has_contacts": False,
                                          "has_pad_leads": False}
    monkeypatch.setattr(component_check, "document_check_applies", lambda document: True)
    monkeypatch.setattr(component_check, "declares_contacts", lambda rows: True)
    _run_again(guided, monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, FILE_READ, MANUAL])
    assert guided._kicad_check_inputs == {"has_document": True, "has_contacts": True,
                                          "has_pad_leads": False}
    flags = guided._kicad_required_flags([OK], guided._kicad_files_fingerprint())
    assert flags["has_document"] is True and flags["has_contacts"] is True


def test_declared_contacts_decide_whether_a_crash_concluded(guided, monkeypatch):
    """Pair: no kicad-cli, no STEP, the crash before the contact roles' check.

    Without declared contacts every required check is concluded -> «crashed»;
    with them the NC/NO roles' check never concluded -> «crash_incomplete».
    The difference is the fact the window passes (`has_contacts`), nothing else.
    """
    for path in (guided._project_dir() / "HDR.3dshapes").glob("*"):
        path.unlink()
    run = [NO_CLI, REQUIRED_ROWS[0], REQUIRED_ROWS[1], NO_CLI_READ, NO_CLI_FILE_READ, _crash(count=5)]
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "crashed"
    monkeypatch.setattr(component_check, "declares_contacts", lambda rows: True)
    _run_again(guided, monkeypatch, run)
    assert guided._kicad_check_state() == "crash_incomplete" and _blocked(guided)
