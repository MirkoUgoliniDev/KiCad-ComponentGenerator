"""@brief kicad-cli outcomes classified by their DECLARED exit code, on scratch copies (review 2026-11-15).

@details A2/A4: an ERROR forbids the installation in the guided KiCad tab,
so only KiCad declaring its input refused (`CLI_INPUT_REFUSED`, observed
with kicad-cli 10.0: 3 for a file that does not open, 2 for one that does
not load) is an ERROR; a crash code, another code, exit 0 without an
output, or a kicad-cli that does not start are «not run» (SKIP with a
retry remedy) -- since round 4 only when the program's own control file of
the same kind fails too, else they are the component's (ERROR
«component»); words in KiCad's log never make an ERROR. M4: kicad-cli
reads scratch copies, never the project's files in place. L2: `_run_cli`
releases its Job Object and kills the tree on any exception. Everything
runs without KiCad (`_run_cli` faked) except the real-process test, which
starts a Python child. Positive and negative twins.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kicadgen.cad3d import process_log
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
READ_CODES = ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read', 'kicad_3d_import')


def _kind(args):
    """Which command of the check a kicad-cli argument list is."""
    if args[:3] == ['fp', 'export', 'svg']:
        return 'fp'
    if args[:3] == ['sym', 'export', 'svg']:
        return 'sym'
    if args[:3] == ['pcb', 'export', 'step']:
        return 'step'
    if args[:2] == ['pcb', 'drc']:
        return 'drc'
    if args[:2] == ['sch', 'erc']:
        return 'erc'
    return '?'


def _is_control(args):
    """True for a kicad-cli call of the differential control (its input lies under «control-<kind>»)."""
    return any(part.startswith('control-') for arg in args for part in Path(arg).parts)


def _fake_cli(monkeypatch, codes=None, produce=True, log='', seen=None, raises=None,
              control_fails=False):
    """kicad-cli faked: per command kind an exit code, an output written or not, a log.

    @param codes {kind: exit code}; 0 by default.
    @param produce False: the reads write no output (DRC/ERC still write their report).
    @param raises {kind: exception instance} raised instead of running.
    @param control_fails True: the differential controls fail exactly like the
        component's reads (same code, same output or none) -- the tool's or the
        computer's failure; False: they read (a sound environment).
    """
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        kind = _kind(args)
        if seen is not None:
            seen.append((kind, args))
        if raises and kind in raises:
            raise raises[kind]
        output = Path(args[args.index('-o') + 1])
        # CONTRACT CHANGED 2026-11-15 (round 3): a refusal that repeats is now
        # tested against the program's own control file of the same kind; a
        # code faked for EVERY call of a kind would be an environment that
        # refuses every fresh file (SKIP «environment»). These tests fake a
        # sound environment: the control (input under «control-<kind>»)
        # reads, so the component's refusal stays its own.
        control = _is_control(args) and not control_fails
        if kind == 'drc':
            output.write_text('{"violations": []}', encoding='utf-8')
        elif kind == 'erc':
            output.write_text('{"sheets": []}', encoding='utf-8')
        elif (produce or control) and kind == 'step':
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        elif produce or control:
            output.mkdir(parents=True, exist_ok=True)
            (output / 'out.svg').write_text('<svg/>', encoding='utf-8')
        code = 0 if control else (codes or {}).get(kind, 0)
        return subprocess.CompletedProcess(command, code, log, '')
    monkeypatch.setattr(check, '_run_cli', run)


def _component(tmp_path, with_symbol=True):
    """The test component, with an exported symbol file so all four reads run."""
    art = _artifacts(tmp_path)
    if with_symbol:
        from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
        pins, body = symbol_inputs(ROWS)
        (art.component_dir / 'Demo.kicad_sym').write_text(
            serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body), encoding='utf-8')
    return art


def _reads(results):
    """{code: Check} of the kicad-cli reads."""
    return {r.code: r for r in results if r.code in READ_CODES}


# ── the verdict rule ────────────────────────────────────────────────────

@pytest.mark.parametrize('code, produced, verdict', [
    (check.CLI_ERR_INVALID_INPUT_FILE, False, 'unreadable'),
    (check.CLI_ERR_UNKNOWN, True, 'unreadable'),
    (0, True, 'read'),
    (0, False, 'not_run'),
    (check.CLI_ERR_ARGS, False, 'not_run'),
    (check.CLI_ERR_INVALID_OUTPUT_CONFLICT, False, 'not_run'),
    (3221225477, False, 'not_run'),
    (-1073741819, True, 'not_run'),
])
def test_the_verdict_follows_the_declared_code_only(code, produced, verdict):
    """@brief Input refused (2, 3) vs read (0 + output) vs not run (anything else)."""
    assert check.cli_read_verdict(code, produced) == verdict


def test_a_crash_code_is_shown_in_hex():
    """@brief 0xC0000005 is how the user can look a Windows crash up; small codes stay decimal."""
    assert check._exit_code_text(3221225477) == '0xC0000005'
    assert check._exit_code_text(-1073741819) == '0xC0000005'
    assert check._exit_code_text(4) == '4'


# ── verify_component with a faked kicad-cli ─────────────────────────────

def test_kicad_declaring_the_footprint_invalid_is_an_error(tmp_path, monkeypatch):
    """@brief Positive: exit 3 on the footprint read -> ERROR on kicad_footprint_read only (the
    declared code decides, whatever was written); the other reads, exit 0, stay OK."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, codes={'fp': 3}, log='The footprint library does not exist')
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert reads['kicad_footprint_read'].status == 'ERROR'
    assert 'does not exist' in reads['kicad_footprint_read'].detail
    assert reads['kicad_symbol_read'].status == 'OK'


def test_a_footprint_that_does_not_load_is_an_error_too(tmp_path, monkeypatch):
    """@brief Positive: exit 2 («Unable to load library», kicad-cli 10.0) -> ERROR; an empty log
    still says which code KiCad gave."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, codes={'fp': 2}, produce=False)
    row = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))['kicad_footprint_read']
    assert row.status == 'ERROR' and '2' in row.detail


@pytest.mark.parametrize('code', [3221225477, -1073741819, 1, 4, 99])
def test_a_crash_or_another_code_is_not_run_with_a_remedy(tmp_path, monkeypatch, code):
    """@brief Negative twin: a crash or a code that does not declare the input refused, the
    differential control failing the same way -> SKIP naming the code, with a retry remedy;
    never an ERROR.
    CONTRACT CHANGED 2026-11-15 (round 4, item 1): such a failure is the tool's only when the
    program's own control file of the same kind fails too (`control_fails`); with a sound
    control it is the component's (next test)."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, codes={'fp': code, 'sym': code, 'step': code}, produce=False,
              control_fails=True)
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert set(reads) == set(READ_CODES)
    for row in reads.values():
        assert row.status == 'SKIP' and row.remedy, row
        assert check._exit_code_text(code) in row.detail
    assert not any(r.status == 'ERROR' for r in reads.values())


@pytest.mark.parametrize('code', [3221225477, -1073741819, 1, 4, 99])
def test_a_crash_the_control_does_not_share_is_the_components(tmp_path, monkeypatch, code):
    """@brief Positive (round 4, item 1): the same failure on every read, the controls READ ->
    ERROR on every read, declared «component», naming the code, with a remedy."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, codes={'fp': code, 'sym': code, 'step': code}, produce=False)
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert set(reads) == set(READ_CODES)
    for row in reads.values():
        assert row.status == 'ERROR' and row.cli_failure == 'component' and row.remedy, row
        assert check._exit_code_text(code) in row.detail


def test_exit_zero_without_output_is_not_run(tmp_path, monkeypatch):
    """@brief Negative: kicad-cli 10.0 exits 0 when it cannot write its output, and so does the
    control -> SKIP, not ERROR. CONTRACT CHANGED 2026-11-15 (round 4, item 1): with a control
    that reads, the same exit 0 without output is the component's (next test)."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, produce=False, log='Errore nel creare il file svg', control_fails=True)
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert {code: row.status for code, row in reads.items()} == dict.fromkeys(READ_CODES, 'SKIP')
    assert all(row.remedy and 'Errore nel creare il file svg' in row.detail for row in reads.values())


def test_exit_zero_without_output_the_control_does_not_share_is_the_components(tmp_path, monkeypatch):
    """@brief Positive twin (round 4, item 1): exit 0 without output on the component's reads, the
    controls write theirs -> ERROR «component» with KiCad's log kept."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, produce=False, log='Errore nel creare il file svg')
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert {code: row.status for code, row in reads.items()} == dict.fromkeys(READ_CODES, 'ERROR')
    assert all(row.cli_failure == 'component' and 'Errore nel creare il file svg' in row.detail
               for row in reads.values())


def test_exit_zero_with_output_is_a_read(tmp_path, monkeypatch):
    """@brief Positive twin: exit 0 with an output and a quiet log -> OK on every read."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert {code: row.status for code, row in reads.items()} == dict.fromkeys(READ_CODES, 'OK')


@pytest.mark.parametrize('log', ['Errore: impossibile caricare il modello', 'Fehler beim Laden',
                                 'Erreur de lecture', 'Error: failed to load model'])
def test_an_error_word_on_exit_zero_is_never_an_error(tmp_path, monkeypatch, log):
    """@brief Negative: a localized error word with exit 0 and an output -> WARN showing the log;
    the text is in KiCad's language and never decides an ERROR."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, log=log)
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert all(row.status == 'WARN' and log in row.detail for row in reads.values()), reads


def test_a_kicad_cli_that_does_not_start_is_not_run_with_a_remedy(tmp_path, monkeypatch):
    """@brief Negative: OSError at launch -> SKIP «kicad-cli non avviato» with a remedy."""
    art = _component(tmp_path)
    error = OSError(13, 'Access denied')
    _fake_cli(monkeypatch, raises=dict.fromkeys(('fp', 'sym', 'step'), error))
    reads = _reads(check.verify_component(art.component_dir, 'Demo', ROWS))
    assert set(reads) == set(READ_CODES)
    for row in reads.values():
        assert row.status == 'SKIP' and row.detail.startswith('kicad-cli non avviato') and row.remedy


# ── M4: kicad-cli reads scratch copies only ─────────────────────────────

def test_every_kicad_cli_input_lies_in_the_scratch_folder(tmp_path, monkeypatch):
    """@brief Positive: no path given to kicad-cli is inside the component folder; the copies hold
    exactly the bytes of the component's files."""
    art = _component(tmp_path)
    seen = []
    copies = {}
    original = art.footprint_path.read_bytes()

    def remember(command, **kwargs):
        args = [str(a) for a in command[1:]]
        if _kind(args) == 'fp':
            copies['fp'] = (Path(args[-1]) / 'Demo.kicad_mod').read_bytes()
        if _kind(args) == 'sym':
            # CONTRACT CHANGED 2026-11-15 (round 2): every read gets a fresh
            # attempt folder of its own (R2), so the two symbol reads are no
            # longer told apart by a shared «input» folder; the saved file's
            # bytes must be among the copies handed to kicad-cli.
            copies.setdefault('sym', []).append(Path(args[-1]).read_bytes())
        return inner(command, **kwargs)
    _fake_cli(monkeypatch, seen=seen)
    inner = check._run_cli
    monkeypatch.setattr(check, '_run_cli', remember)
    check.verify_component(art.component_dir, 'Demo', ROWS)
    component = art.component_dir.resolve()
    paths = [Path(arg) for _kind_, args in seen for arg in args
             if ('/' in arg or '\\' in arg)]
    assert {kind for kind, _args in seen} >= {'fp', 'sym', 'step', 'drc', 'erc'}
    assert paths
    scratch_roots = {next(p for p in [path, *path.parents] if p.name.startswith('kicadgen-check-'))
                     for path in paths}
    assert len(scratch_roots) == 1, paths
    for path in paths:
        assert component not in path.resolve().parents and path.resolve() != component, path
    assert copies['fp'] == original
    assert (art.component_dir / 'Demo.kicad_sym').read_bytes() in copies['sym']


# ── L2: the runner releases its job and kills on any exception ──────────

def test_the_job_is_released_after_every_command(monkeypatch):
    """@brief Positive: a finished command and a timed-out one both release their job;
    negative twin: nothing is left in the running set."""
    released = []
    real_release = process_log.release_process
    monkeypatch.setattr(process_log, 'release_process',
                        lambda process: (released.append(process), real_release(process)))
    done = check._run_cli([sys.executable, '-c', 'print("ok")'], timeout=30)
    assert done.returncode == 0 and len(released) == 1
    with pytest.raises(subprocess.TimeoutExpired):
        check._run_cli([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=1)
    assert len(released) == 2 and released[1].poll() is not None
    assert not check._CLI_PROCESSES


def test_an_interrupt_during_the_wait_kills_the_child_and_propagates(monkeypatch):
    """@brief Positive: a KeyboardInterrupt raised inside the wait kills and reaps the child,
    releases the job and re-raises; the child does not run on."""
    real_communicate = subprocess.Popen.communicate
    captured = {}

    def interrupted(self, *args, **kwargs):
        captured['process'] = self
        if kwargs.get('timeout') == 45:
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)
    monkeypatch.setattr(subprocess.Popen, 'communicate', interrupted)
    released = []
    real_release = process_log.release_process
    monkeypatch.setattr(process_log, 'release_process',
                        lambda process: (released.append(process), real_release(process)))
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        check._run_cli([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=45)
    process = captured['process']
    assert process.poll() is not None, 'the child must not outlive the interrupted wait'
    assert released == [process] and time.monotonic() - started < 15
    assert not check._CLI_PROCESSES


def test_a_stopped_command_is_still_released(monkeypatch):
    """@brief Negative twin of the interrupt: `stop_cli_commands` ends the command, which raises
    CliStopped -- and its job is released too."""
    released = []
    real_release = process_log.release_process
    monkeypatch.setattr(process_log, 'release_process',
                        lambda process: (released.append(process), real_release(process)))
    outcome = {}

    def slow():
        try:
            check._run_cli([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=60)
            outcome['result'] = 'finished'
        except check.CliStopped:
            outcome['result'] = 'stopped'
    thread = threading.Thread(target=slow)
    thread.start()
    deadline = time.monotonic() + 10
    while not check._CLI_PROCESSES and time.monotonic() < deadline:
        time.sleep(0.02)
    assert check.stop_cli_commands() == 1
    thread.join(15)
    assert outcome == {'result': 'stopped'} and len(released) == 1
