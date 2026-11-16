"""@brief A kicad-cli refusal is the component's only when it repeats on fresh copies (review 2026-11-15, round 2, R2).

@details kicad-cli 10 gives the codes of a refused input (2, 3) also for
failures of its own, measured on valid inputs: `pcb export step` onto a
read-only output or a folder in the output's place (2), `fp/sym export svg`
with the input held exclusively by another process (2), `sym export svg -o
<folder that cannot be created>` (2), `pcb export step` on a locked board
(3). So a 2 or 3 is repeated ONCE on a fresh copy of the input into a fresh
output folder: refused again -> ERROR; read -> the read's normal outcome;
anything else (another code, a crash, a timeout) -> the differential
control decides since round 4 (read -> ERROR «component», failed too ->
SKIP «not run»). A
failure to prepare an attempt (output folder not writable, input copy not
openable) is a SKIP before kicad-cli runs. The fakes run without KiCad; the
last test uses the real kicad-cli and is skipped without it. Positive and
negative twins.
"""

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from kicadgen.services import component_check as check
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
#: Exit code of a Windows access violation, as kicad-cli reports a crash.
CRASH = 3221225477


def _kind(args):
    """Which command of the check a kicad-cli argument list is."""
    if args[:3] == ['fp', 'export', 'svg']:
        return 'fp'
    if args[:3] == ['sym', 'export', 'svg']:
        return 'sym'
    if args[:3] == ['pcb', 'export', 'step']:
        return 'step'
    return args[1] if len(args) > 1 and args[0] in ('pcb', 'sch') else '?'


def _fake_cli(monkeypatch, sequences, seen):
    """kicad-cli faked with one outcome per CALL of each command kind.

    @param sequences {kind: [outcome, ...]}: an int exit code (0 writes the
        output) or an exception instance raised instead; a kind whose list
        is used up, or absent, answers 0.
    @param seen Receives (kind, args) of every call.
    """
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())
    queues = {kind: list(outcomes) for kind, outcomes in sequences.items()}

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        kind = _kind(args)
        seen.append((kind, args))
        outcome = queues[kind].pop(0) if queues.get(kind) else 0
        if isinstance(outcome, BaseException):
            raise outcome
        output = Path(args[args.index('-o') + 1])
        if kind == 'drc':
            output.write_text('{"violations": []}', encoding='utf-8')
        elif kind == 'erc':
            output.write_text('{"sheets": []}', encoding='utf-8')
        elif outcome == 0 and kind == 'step':
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        elif outcome == 0:
            (output / 'out.svg').write_text('<svg/>', encoding='utf-8')
        return subprocess.CompletedProcess(command, outcome, '', '')
    monkeypatch.setattr(check, '_run_cli', run)


def _component(tmp_path):
    """The test component with an exported symbol file, so every read runs."""
    art = _artifacts(tmp_path)
    from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
    pins, body = symbol_inputs(ROWS)
    (art.component_dir / 'Demo.kicad_sym').write_text(
        serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body), encoding='utf-8')
    return art


def _row(results, code):
    """The one row of a code."""
    (row,) = [r for r in results if r.code == code]
    return row


# ── the rule, with a faked kicad-cli ────────────────────────────────────

def test_a_refusal_that_does_not_repeat_is_a_read(tmp_path, monkeypatch):
    """@brief Positive: footprint read 2 then 0 -> OK, two calls on two DIFFERENT fresh inputs and
    outputs, both copies holding the component's bytes."""
    art = _component(tmp_path)
    seen = []
    _fake_cli(monkeypatch, {'fp': [2, 0]}, seen)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_footprint_read').status == 'OK'
    calls = [args for kind, args in seen if kind == 'fp']
    assert len(calls) == 2
    inputs = [Path(args[-1]) for args in calls]
    outputs = [Path(args[args.index('-o') + 1]) for args in calls]
    assert inputs[0] != inputs[1] and outputs[0] != outputs[1]
    assert inputs[0].parent != inputs[1].parent


def test_a_refusal_that_repeats_is_an_error(tmp_path, monkeypatch):
    """@brief Negative twin: 2 then 2 -> ERROR, importance 1 (the component's file)."""
    art = _component(tmp_path)
    seen = []
    _fake_cli(monkeypatch, {'fp': [2, 2]}, seen)
    row = _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_footprint_read')
    assert row.status == 'ERROR' and row.importance == check.IMPORTANCE_BLOCKS_USE
    # CONTRACT CHANGED 2026-11-15 (round 3): a repeated refusal is followed
    # by ONE differential control of the same kind on the program's own file
    # (here it reads: the queue is used up, exit 0), so three fp calls.
    calls = [args for kind, args in seen if kind == 'fp']
    assert len(calls) == 3
    assert 'control-fp' in Path(calls[2][-1]).parts and all(
        'control-fp' not in Path(args[-1]).parts for args in calls[:2])


def test_an_invalid_input_code_that_repeats_is_an_error_too(tmp_path, monkeypatch):
    """@brief Positive: 3 then 3 on the STEP import -> ERROR; its twin 3 then 0 -> OK."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, {'step': [3, 3]}, [])
    assert _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_3d_import').status == 'ERROR'
    _fake_cli(monkeypatch, {'step': [3, 0]}, [])
    assert _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_3d_import').status == 'OK'


@pytest.mark.parametrize('second', [CRASH, 4, 1, subprocess.TimeoutExpired('kicad-cli', 60)])
def test_a_refusal_then_a_tool_failure_the_control_shares_is_not_run(tmp_path, monkeypatch, second):
    """@brief Positive: 2 then a crash / another code / a timeout, and the differential control
    (the third sym call) failing the same way -> SKIP «not run» with a remedy, never an ERROR.
    CONTRACT CHANGED 2026-11-15 (round 4, item 1): the failure of the repeat is no longer «not
    run» by itself -- the control of the same kind decides (see the twin below)."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch, {'sym': [2, second, second]}, [])
    row = _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_symbol_read')
    assert row.status == 'SKIP' and row.remedy and row.cli_failure in ('not_run', 'timeout')


@pytest.mark.parametrize('second', [CRASH, 4, 1, subprocess.TimeoutExpired('kicad-cli', 60)])
def test_a_refusal_then_a_tool_failure_the_control_does_not_share_is_the_components(
        tmp_path, monkeypatch, second):
    """@brief Negative twin (round 4, item 1): the same two failures, the control READ (the queue
    is used up, exit 0) -> ERROR declared «component», with a remedy.
    CONTRACT CHANGED 2026-11-15 (round 5): a timeout whose control reads is blamed on the
    component only after ONE extended read timed out too (a large file on a slow PC is not a
    defect), so the timeout case queues the control's read (0) and a second timeout for the
    extended read; the other failures are unchanged."""
    art = _component(tmp_path)
    timed_out = isinstance(second, subprocess.TimeoutExpired)
    _fake_cli(monkeypatch, {'sym': [2, second] + ([0, second] if timed_out else [])}, [])
    row = _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_symbol_read')
    assert row.status == 'ERROR' and row.remedy and row.cli_failure == 'component'


def test_a_refusal_then_a_start_failure_is_not_run_without_control(tmp_path, monkeypatch):
    """@brief Positive: 2 then a kicad-cli that cannot start -> SKIP «start»; no control is made
    (it could not start either)."""
    art = _component(tmp_path)
    seen = []
    _fake_cli(monkeypatch, {'sym': [2, OSError('access denied')]}, seen)
    row = _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_symbol_read')
    assert row.status == 'SKIP' and row.cli_failure == 'start'
    assert not any('control-sym' in Path(args[-1]).parts for kind, args in seen if kind == 'sym')


@pytest.mark.parametrize('first, calls', [(0, 1), (4, 3), (CRASH, 3),
                                          (subprocess.TimeoutExpired('kicad-cli', 60), 2)])
def test_which_failures_are_repeated(tmp_path, monkeypatch, first, calls):
    """@brief A read that ends with 0 runs ONCE and makes no control (negative); another code or
    a crash is repeated once, then controlled (positive: 2 reads + 1 control); a timeout is never
    repeated, only controlled (1 read + 1 control).
    CONTRACT CHANGED 2026-11-15 (round 4, item 1): only a refusal used to be repeated; every
    failure that may be the component's is now repeated (a crash can be transient) and then
    tested against the differential control, except a timeout, which used its whole budget
    (a repeat would get only the floor `CLI_RETRY_MIN_TIMEOUT_S`)."""
    art = _component(tmp_path)
    seen = []
    _fake_cli(monkeypatch, {'fp': [first, first]}, seen)
    check.verify_component(art.component_dir, 'Demo', ROWS)
    assert [kind for kind, _args in seen].count('fp') == calls


def test_an_output_folder_that_refuses_files_is_not_run(tmp_path, monkeypatch):
    """@brief Positive: the probe file cannot be written in the attempt's output folder -> SKIP
    before kicad-cli runs (an output-side failure is never the component's)."""
    art = _component(tmp_path)
    seen = []
    _fake_cli(monkeypatch, {}, seen)
    real_write = Path.write_bytes

    def refuse_probe(self, data):
        if self.name == '.kicadgen-probe':
            raise PermissionError(13, 'Access is denied', str(self))
        return real_write(self, data)
    monkeypatch.setattr(Path, 'write_bytes', refuse_probe)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    for code in ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read'):
        row = _row(results, code)
        assert row.status == 'SKIP' and row.remedy, row
    assert not any(kind in ('fp', 'sym') for kind, _args in seen)


def test_an_input_copy_that_cannot_be_opened_is_not_run(tmp_path, monkeypatch):
    """@brief Positive: the footprint copy cannot be opened (held by another process) -> SKIP
    before kicad-cli; negative twin in the same run: the symbol reads, whose copies open, run."""
    art = _component(tmp_path)
    seen = []
    _fake_cli(monkeypatch, {}, seen)
    real_open = open

    def held(path, *args, **kwargs):
        if str(path).endswith('.kicad_mod'):
            raise PermissionError(13, 'The process cannot access the file', str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(check, 'open', held, raising=False)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_footprint_read').status == 'SKIP'
    assert _row(results, 'kicad_symbol_read').status == 'OK'
    assert not any(kind == 'fp' for kind, _args in seen)


# ── the real kicad-cli ──────────────────────────────────────────────────

def _real_cli():
    """kicad-cli of this machine, or None (the GitHub runner has none)."""
    from kicadgen.services.system_dependencies import find_kicad_cli
    return find_kicad_cli()


@pytest.mark.skipif(sys.platform != 'win32', reason='exclusive file locks through the Windows API')
def test_real_kicad_cli_tells_a_locked_input_from_a_corrupt_file(tmp_path, monkeypatch):
    """@brief With the real kicad-cli 10: the footprint copy held exclusively during the FIRST run
    (kicad-cli answers 2, as for a corrupt file) -> the repeat reads it -> OK; a footprint with
    an unknown token -> refused twice -> ERROR."""
    cli = _real_cli()
    if not cli:
        pytest.skip('kicad-cli not installed')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    real_run = check._run_cli
    calls = []

    def lock_first(command, **kwargs):
        args = [str(a) for a in command[1:]]
        calls.append(_kind(args))
        if _kind(args) == 'fp' and calls.count('fp') == 1:
            # Share mode 0: no other process can open the copy meanwhile.
            handle = kernel.CreateFileW(str(Path(args[-1]) / 'Demo.kicad_mod'), 0x80000000, 0,
                                        None, 3, 0x80, None)
            assert handle != wintypes.HANDLE(-1).value
            try:
                return real_run(command, **kwargs)
            finally:
                kernel.CloseHandle(handle)
        return real_run(command, **kwargs)
    monkeypatch.setattr(check, '_run_cli', lock_first)
    sound = _component(tmp_path / 'sound')
    row = _row(check.verify_component(sound.component_dir, 'Demo', ROWS), 'kicad_footprint_read')
    assert calls.count('fp') == 2 and row.status == 'OK', row
    corrupt = _component(tmp_path / 'corrupt')
    text = corrupt.footprint_path.read_text(encoding='utf-8')
    corrupt.footprint_path.write_text(text.replace('(pad ', '(bogus_token 1) (pad ', 1),
                                      encoding='utf-8')
    monkeypatch.setattr(check, '_run_cli', real_run)
    row = _row(check.verify_component(corrupt.component_dir, 'Demo', ROWS), 'kicad_footprint_read')
    assert row.status == 'ERROR', row
