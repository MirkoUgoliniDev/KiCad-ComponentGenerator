"""@brief Round 4 of the 2026-11-15 review: the window's check states, decided on declared fields only.

Rules, each with a case that must trigger and one that must not:

- Item 1: no persistent-failure signature when another kicad-cli
  invocation of the same run SUCCEEDED (a DRC or ERC row that ran, a KiCad
  read or import OK/WARN): kicad-cli works, so failing reads are about the
  files. A row declaring a component-side failure (`cli_failure`
  «component») is never part of a signature either.
- Item 2: a run that ENDED without concluding a check an installation needs
  (a footprint held by another program) blocks with «file in uso» and
  «Controlla di nuovo», which runs again although no file changed.
- Item 3: the files' fingerprint records a folder in a file's place as
  absent, as the service's `is_file()` sees it.
- Item 4: a persistent «scratch» cause (the check's temporary folder
  unusable) has its own sentence.

No AI and no kicad-cli: `verify_component` is replaced by a scripted one.
Service interface (S4, written in the same round): the declared
`cli_failure` values «component» and «scratch» are plain strings on `Check`;
a component file held by another program is a SKIP declaring
`Check.file_busy`, which the service's table (`missing_required`) does not
count as concluding the footprint file nor pin-pad -- the real table is used
here. Only the cases no real row produces yet (an unconcluded check with no
row of its own, an unconcluded DRC) fake `missing_required`.
"""

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.services import component_check  # noqa: E402
from kicadgen.services.component_check import Check  # noqa: E402
from tests.test_kicad_guided_tab import (CLI_OK, DRC_OK, FILE_READ, IMPORT_3D, MANUAL,  # noqa: E402,F401
                                         NO_CLI, NO_CLI_FILE_READ, NO_CLI_READ, OK, READ, REQUIRED_ROWS,
                                         Script, enter_kicad_tab, guided, wait_idle)
from tests.test_main_window_r2_check_state import _blocked, _run_again  # noqa: E402
from tests.test_generated_files_alignment import NAME  # noqa: E402

#: A Windows status kicad-cli's process may end with on every read (a
#: stack buffer overrun), as the service declares it in `Check.cli_exit_code`.
STACK_OVERRUN = 0xC0000409
#: The three reads of a run without a STEP import row.
READS = ("kicad_footprint_read", "kicad_symbol_read", "kicad_symbol_file_read")
#: The footprint file's and pin-pad's own outcome rows, which every finished
#: run of the service emits (`component_check.BLOCKING_CHECKS`).
FILE_ROWS = REQUIRED_ROWS[:2]
ERC_OK = Check('OK', 'ERC', 'ok', code='erc')


def _ok(code):
    """A concluding OK row of `code`."""
    return Check('OK', 'x', 'x', code=code)


def _read_failed(code, failure="not_run", exit_code=STACK_OVERRUN):
    """The service's SKIP of a KiCad read that failed with a declared cause."""
    return Check('SKIP', 'Lettura in KiCad', 'kicad-cli è terminato con un codice', code=code,
                 cli_failure=failure, cli_exit_code=exit_code)


def _reads_crash_run(*others):
    """The reviewer's run: every read ends with the same crash code; `others` added before the end."""
    return [CLI_OK, _ok('footprint_file'), _ok('footprint_geometry'), _read_failed(READS[0]),
            _ok('pin_pad'), _read_failed(READS[1]), _read_failed(READS[2]),
            Check('WARN', 'STEP', 'Nessun modello STEP', code='step_missing'), *others, MANUAL]


def _twice(window, monkeypatch, first, second):
    """Two consecutive runs; returns the state after the first one."""
    Script(monkeypatch, first)
    enter_kicad_tab(window)
    wait_idle(window)
    state = window._kicad_check_state()
    _run_again(window, monkeypatch, second)
    return state


# ── item 1: kicad-cli worked for another command of the run ─────────────────

@pytest.mark.parametrize("others", [
    (DRC_OK, ERC_OK),
    (Check('WARN', 'DRC', 'x', code='drc_silk_overlap'),),
    (Check('SKIP', 'DRC', 'non eseguito', code='drc'), Check('WARN', 'ERC', 'x', code='erc_pin_to_pin')),
    (IMPORT_3D,),
], ids=["drc_and_erc_ok", "drc_violation", "erc_violation", "step_import_ok"])
def test_reads_failing_alike_twice_while_another_kicad_cli_command_worked_stay_unread(guided, monkeypatch,
                                                                                       others):
    """Positive of item 1 (the reviewer's case): the same crash code on every read, twice, but a DRC,
    an ERC or the STEP import of the same runs worked -> no signature, «unread», blocked."""
    run = _reads_crash_run(*others)
    assert _twice(guided, monkeypatch, run, run) == "unread"
    assert guided._kicad_check_state() == "unread" and _blocked(guided)
    assert guided._kicad_failure_signature(run) is None and guided._kicad_failure_persistent is None


def test_the_same_reads_with_drc_and_erc_failing_too_are_a_kicad_cli_that_cannot_work(guided, monkeypatch):
    """Negative twin: the DRC and the ERC did not run either (SKIP) -> the same signature twice,
    «cli_unusable», allowed with the exit code named."""
    run = _reads_crash_run(Check('SKIP', 'DRC', 'non eseguito', code='drc', cli_failure='not_run',
                                 cli_exit_code=STACK_OVERRUN),
                           Check('SKIP', 'ERC', 'non eseguito', code='erc'))
    assert _twice(guided, monkeypatch, run, run) == "unread"
    assert guided._kicad_check_state() == "cli_unusable" and guided.btn_export.isEnabled()
    assert "0xC0000409" in guided.label_install_advice.text()


def test_a_component_side_failure_is_never_part_of_a_signature(guided):
    """Item 1: a read whose failure the differential control proved to be the component's
    (`cli_failure` «component») makes no signature; the same row declared tool-side does."""
    guided._kicad_check_files = guided._kicad_files_fingerprint()
    tool = [CLI_OK, *FILE_ROWS, OK, *(_read_failed(code) for code in READS), MANUAL]
    assert guided._kicad_failure_signature(tool) is not None
    component = [CLI_OK, *FILE_ROWS, OK, _read_failed(READS[0], failure="component", exit_code=None),
                 *(_read_failed(code) for code in READS[1:]), MANUAL]
    assert guided._kicad_failure_signature(component) is None


def test_a_component_side_failure_twice_blocks_on_its_error(guided, monkeypatch):
    """Item 1 in the window: the component's failure comes with an ERROR -> «blocking» run after run."""
    run = [CLI_OK, *FILE_ROWS, OK, _read_failed(READS[0], failure="component", exit_code=None),
           Check('ERROR', 'Lettura footprint in KiCad', 'KiCad rifiuta il file', code='kicad_footprint_read'),
           *(_read_failed(code) for code in READS[1:]), MANUAL]
    _twice(guided, monkeypatch, run, run)
    assert guided._kicad_check_state() == "blocking" and _blocked(guided)
    assert guided._kicad_failure_persistent is None


# ── item 2: a component file held by another program ────────────────────────

#: The service's rows of a footprint held by another program (round 3, item 3),
#: declared `Check.file_busy` (round 4).
HELD = Check('SKIP', 'Footprint su disco', 'Il file è in uso da un altro programma', code='footprint_file',
             remedy='Chiudi il programma che tiene aperto il file, poi ripeti il controllo.', file_busy=True)
PIN_PAD_UNREAD = Check('SKIP', 'Pin e pad', 'Footprint non leggibile.', code='pin_pad')


def test_a_held_footprint_without_kicad_cli_blocks_until_the_check_is_repeated(guided, monkeypatch):
    """Positive of item 2 (the reviewer's case): no kicad-cli, the footprint held -> «file_busy»,
    blocked, «file in uso» said; the lock released changes no file, and «Controlla di nuovo»
    still runs the check again, which then allows the installation."""
    held = [NO_CLI, HELD, PIN_PAD_UNREAD, NO_CLI_READ, NO_CLI_FILE_READ, _ok('step_integrity'),
            Check('SKIP', 'DRC', 'Richiede KiCad CLI.', code='drc', needs_cli=True), MANUAL]
    Script(monkeypatch, held)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "file_busy" and _blocked(guided)
    advice = guided.label_install_advice.text()
    assert "in uso da un altro programma" in advice and "Controlla di nuovo" in advice
    fingerprint = guided._kicad_files_fingerprint()
    released = [NO_CLI, _ok('footprint_file'), _ok('pin_pad'), NO_CLI_READ, NO_CLI_FILE_READ,
                _ok('step_integrity'), MANUAL]
    script = Script(monkeypatch, released)
    guided.kicad_check_panel.rerun_button.click()
    wait_idle(guided)
    assert script.runs == 1 and guided._kicad_files_fingerprint() == fingerprint
    assert guided._kicad_check_state() == "inconclusive" and guided.btn_export.isEnabled()


def test_a_held_footprint_with_kicad_cli_is_said_as_file_in_use_not_unread(guided, monkeypatch):
    """Item 2 with kicad-cli: the footprint read never happened, but the cause is the lock."""
    Script(monkeypatch, [CLI_OK, HELD, PIN_PAD_UNREAD, FILE_READ, IMPORT_3D, DRC_OK, MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "file_busy" and _blocked(guided)


def test_a_held_symbol_file_with_kicad_cli_is_said_as_file_in_use(guided, monkeypatch):
    """Item 2, the saved symbol held: its KiCad read was not made because of the lock -> «file_busy».
    Negative twin: the same read skipped WITHOUT the declared lock -> the usual «unread»."""
    held_symbol = Check('SKIP', 'Simbolo su disco: lettura in KiCad', 'in uso', code='kicad_symbol_file_read',
                        file_busy=True)
    Script(monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, held_symbol, IMPORT_3D, DRC_OK, MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "file_busy" and _blocked(guided)
    skipped = Check('SKIP', 'Simbolo su disco: lettura in KiCad', 'non letto', code='kicad_symbol_file_read')
    _run_again(guided, monkeypatch, [CLI_OK, *FILE_ROWS, OK, READ, skipped, IMPORT_3D, DRC_OK, MANUAL])
    assert guided._kicad_check_state() == "unread" and _blocked(guided)


def test_an_unconcluded_check_without_its_own_skip_is_incomplete_not_file_in_use(guided, monkeypatch):
    """Negative of the «file in uso» sentence: a required check unconcluded with no SKIP of its own
    -> «incomplete», blocked, the generic sentence (never a cause no row declares)."""
    real = component_check.missing_required
    monkeypatch.setattr(component_check, "missing_required",
                        lambda results, **flags: frozenset(real(results, **flags)) | {'pin_role_contradiction'})
    Script(monkeypatch, [CLI_OK, *REQUIRED_ROWS, READ, FILE_READ, IMPORT_3D, MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "incomplete" and _blocked(guided)
    advice = guided.label_install_advice.text()
    assert "in uso" not in advice and "senza concludere" in advice


def test_a_skipped_kicad_cli_check_is_never_a_file_in_use(guided, monkeypatch):
    """Negative of the heuristic: an unconcluded DRC with its own SKIP is a tool outcome -> «incomplete»."""
    real = component_check.missing_required

    def missing_required(results, **flags):
        drc_skipped = flags.get("cli_available") and any(r.code == 'drc' and r.status == 'SKIP' for r in results)
        return frozenset(real(results, **flags)) | ({'drc'} if drc_skipped else set())
    monkeypatch.setattr(component_check, "missing_required", missing_required)
    Script(monkeypatch, [CLI_OK, *REQUIRED_ROWS, READ, FILE_READ, IMPORT_3D,
                         Check('SKIP', 'DRC', 'DRC non eseguito', code='drc'), MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "incomplete"


def test_a_finished_run_that_concluded_everything_is_not_blocked_by_the_rule(guided, monkeypatch):
    """Negative of item 2: the same run without kicad-cli, footprint read normally -> «inconclusive», allowed."""
    Script(monkeypatch, [NO_CLI, _ok('footprint_file'), _ok('pin_pad'), NO_CLI_READ, NO_CLI_FILE_READ,
                         _ok('step_integrity'), MANUAL])
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "inconclusive" and guided.btn_export.isEnabled()


def test_a_persistent_kicad_cli_failure_does_not_require_the_checks_only_kicad_cli_makes(guided, monkeypatch):
    """Item 2 meets round 2's R5: a DRC left unconcluded by a kicad-cli that never starts blocks the
    first run; the same failure again is judged like kicad-cli absent, the DRC not required -> allowed."""
    real = component_check.missing_required

    def missing_required(results, **flags):
        drc_skipped = flags.get("cli_available") and any(r.code == 'drc' and r.status == 'SKIP' for r in results)
        return frozenset(real(results, **flags)) | ({'drc'} if drc_skipped else set())
    monkeypatch.setattr(component_check, "missing_required", missing_required)
    run = [CLI_OK, *REQUIRED_ROWS, *(_read_failed(code, "start", None) for code in READS),
           _read_failed('kicad_3d_import', "start", None),
           Check('SKIP', 'DRC', 'non eseguito', code='drc', cli_failure='start'), MANUAL]
    assert _twice(guided, monkeypatch, run, run) == "incomplete"
    assert guided._kicad_check_state() == "cli_unusable" and guided.btn_export.isEnabled()


# ── item 3: a folder in a file's place is no file ───────────────────────────

def test_a_folder_named_like_the_symbol_file_is_recorded_as_absent(guided, monkeypatch):
    """Positive: <name>.kicad_sym is a folder -> absent, no symbol-file read required -> «ok».
    Negative twin: the regular file -> present, its read required -> «unread» without it."""
    run = [CLI_OK, *FILE_ROWS, OK, READ, MANUAL]
    assert guided._kicad_files_fingerprint()[2][1] is not None
    Script(monkeypatch, run)
    enter_kicad_tab(guided)
    wait_idle(guided)
    assert guided._kicad_check_state() == "unread"
    symbol = guided._project_dir() / f"{NAME}.kicad_sym"
    symbol.unlink()
    symbol.mkdir()
    fingerprint = guided._kicad_files_fingerprint()
    assert fingerprint[2][1] is None
    assert guided._kicad_required_flags([OK], fingerprint)["has_symbol_file"] is False
    _run_again(guided, monkeypatch, run)
    assert guided._kicad_check_state() == "ok"


def test_a_folder_named_like_the_step_is_recorded_as_absent(guided):
    """Positive: <name>.step is a folder -> `has_step` False; negative twin: the regular file -> True."""
    step = guided._project_dir() / f"{NAME}.3dshapes" / f"{NAME}.step"
    assert step.is_file()
    assert guided._kicad_required_flags([OK], guided._kicad_files_fingerprint())["has_step"] is True
    step.unlink()
    step.mkdir()
    fingerprint = guided._kicad_files_fingerprint()
    assert fingerprint[2][2] is None
    assert guided._kicad_required_flags([OK], fingerprint)["has_step"] is False


# ── item 4: the check's temporary folder unusable ───────────────────────────

def _scratch_run(failure="scratch"):
    """kicad-cli there, every read failed for the declared cause `failure`."""
    return [CLI_OK, *FILE_ROWS, OK, *(_read_failed(code, failure, None) for code in (*READS, 'kicad_3d_import')),
            MANUAL]


def test_a_persistent_scratch_failure_names_the_temporary_folder(guided, monkeypatch):
    """Positive: «scratch» twice -> «cli_unusable» with its own sentence, not «Verifica l'installazione»."""
    assert _twice(guided, monkeypatch, _scratch_run(), _scratch_run()) == "unread"
    assert guided._kicad_check_state() == "cli_unusable"
    advice = guided.label_install_advice.text()
    assert advice == guided.i18n.text(guided.KICAD_PERSISTENT_CAUSE_TEXTS["scratch"])
    assert "cartella temporanea" in advice and "Verifica l'installazione di KiCad" not in advice


def test_scratch_then_another_cause_is_not_persistent(guided, monkeypatch):
    """Negative: «scratch», then a timeout -> two signatures, still «unread»."""
    _twice(guided, monkeypatch, _scratch_run(), _scratch_run("timeout"))
    assert guided._kicad_check_state() == "unread" and _blocked(guided)


def test_scratch_mixed_with_another_cause_names_no_single_cause(guided):
    """Negative: «scratch» on some reads, «start» on others -> the generic «tool» key."""
    guided._kicad_check_files = guided._kicad_files_fingerprint()
    run = [CLI_OK, OK, _read_failed(READS[0], "scratch", None),
           *(_read_failed(code, "start", None) for code in (*READS[1:], 'kicad_3d_import')), MANUAL]
    assert guided._kicad_persistent_cause_key(run) == ("tool", None)
    assert guided._kicad_persistent_cause_key(_scratch_run()) == ("scratch", None)
