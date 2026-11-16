"""@brief A repeated kicad-cli refusal is judged against a differential control (review 2026-11-15, round 3).

@details Item 1: «refused twice on fresh copies -> ERROR» could not tell an
environment that refuses EVERY fresh file (an antivirus holding new files in
%TEMP%, a broken KiCad user configuration) from a defective file. When the
component's read is refused twice, the same kind of command runs on the
program's own known-good file (`CONTROL_READS`) under the same scratch
conditions: read -> ERROR (the component); refused too -> SKIP with
`cli_failure` «environment»; no verdict -> SKIP «not run». One control per
kind and run. Item 4: the repeat gets only the time the first attempt left
(at least `CLI_RETRY_MIN_TIMEOUT_S`), the control its own budget. Item 5:
a SKIP from a non-refusal exit code declares it in `Check.cli_exit_code`.
The fakes run without KiCad; the last tests use the real kicad-cli and are
skipped without it. Positive and negative twins.
"""

import subprocess
from pathlib import Path

import pytest

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
CRASH = 3221225477
READS = ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read', 'kicad_3d_import')


def _kind(args):
    """Which command of the check a kicad-cli argument list is."""
    if args[:3] == ['fp', 'export', 'svg']:
        return 'fp'
    if args[:3] == ['sym', 'export', 'svg']:
        return 'sym'
    if args[:3] == ['pcb', 'export', 'step']:
        return 'step'
    return args[1] if len(args) > 1 and args[0] in ('pcb', 'sch') else '?'


def _is_control(args):
    """True for a call of the differential control (its input lies under «control-<kind>»)."""
    return any(part.startswith('control-') for arg in args for part in Path(arg).parts)


def _fake_cli(monkeypatch, component=None, control=None, seen=None):
    """kicad-cli faked with one outcome per call, the component's reads and the controls apart.

    @param component {kind: [outcome, ...]} for the component's reads.
    @param control {kind: [outcome, ...]} for the differential controls.
    @param seen Receives (kind, is_control, args, timeout) of every call.
    @note An outcome is an exit code (0 writes the output) or an exception
        raised instead; a used-up or absent list answers 0.
    """
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())
    queues = {('c', k): list(v) for k, v in (component or {}).items()}
    queues.update({('k', k): list(v) for k, v in (control or {}).items()})

    def run(command, timeout=None, **_kwargs):
        args = [str(a) for a in command[1:]]
        kind, is_control = _kind(args), _is_control(args)
        if seen is not None:
            seen.append((kind, is_control, args, timeout))
        queue = queues.get(('k' if is_control else 'c', kind))
        outcome = queue.pop(0) if queue else 0
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
    pins, body = symbol_inputs(ROWS)
    (art.component_dir / 'Demo.kicad_sym').write_text(
        serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body), encoding='utf-8')
    return art


def _row(results, code):
    """The one row of a code."""
    (row,) = [r for r in results if r.code == code]
    return row


# ── item 1: the differential control ────────────────────────────────────

def test_a_refusal_the_control_does_not_share_is_the_components(tmp_path, monkeypatch):
    """@brief Positive: component 2, 2 and control 0 -> ERROR importance 1, no declared failure."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [2, 2]}, control={'fp': [0]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'ERROR' and row.importance == check.IMPORTANCE_BLOCKS_USE
    assert row.cli_failure == '' and row.cli_exit_code is None
    assert [(kind, ctl) for kind, ctl, _a, _t in seen if kind == 'fp'] == [
        ('fp', False), ('fp', False), ('fp', True)]


def test_a_refusal_the_control_shares_is_the_environment(tmp_path, monkeypatch):
    """@brief Negative twin: component 2, 2 and control 2 -> SKIP «environment» with a remedy,
    never an ERROR: the computer refuses even the program's own sound file."""
    _fake_cli(monkeypatch, component={'fp': [2, 2]}, control={'fp': [2]})
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'SKIP' and row.cli_failure == 'environment' and row.remedy
    assert row.cli_exit_code is None


@pytest.mark.parametrize('outcome, failure, exit_code', [
    (subprocess.TimeoutExpired('kicad-cli', 30), 'timeout', None),
    (OSError('blocked'), 'start', None),
    (CRASH, 'not_run', CRASH),
    (4, 'not_run', 4),
])
def test_a_control_without_verdict_attributes_nothing(tmp_path, monkeypatch, outcome, failure,
                                                      exit_code):
    """@brief Positive: the control times out / does not start / ends with another code -> SKIP
    «not run» declaring the control's failure; the refusal is not called the component's."""
    _fake_cli(monkeypatch, component={'sym': [3, 3]}, control={'sym': [outcome]})
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_symbol_read')
    assert row.status == 'SKIP' and row.remedy
    assert (row.cli_failure, row.cli_exit_code) == (failure, exit_code)


def test_one_control_per_kind_and_run(tmp_path, monkeypatch):
    """@brief Positive: both symbol reads refused twice -> ONE sym control, reused (both ERROR);
    negative: a read that is not refused twice runs no control at all."""
    seen = []
    _fake_cli(monkeypatch, component={'sym': [2, 2, 2, 2]}, control={'sym': [0]}, seen=seen)
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_symbol_read').status == 'ERROR'
    assert _row(results, 'kicad_symbol_file_read').status == 'ERROR'
    assert [ctl for kind, ctl, _a, _t in seen if kind == 'sym'].count(True) == 1
    assert not any(ctl for kind, ctl, _a, _t in seen if kind != 'sym')


def test_the_step_import_has_its_own_control(tmp_path, monkeypatch):
    """@brief Positive: the 3D import refused twice, its control (a model import) read ->
    ERROR; refused -> SKIP «environment». The control's input is the program's board, exported
    with the SAME flags as the component's import, with the control's STEP model beside it.
    CONTRACT CHANGED 2026-11-15 (round 4, item 4): the control used to export the board BODY of a
    board without a model (no model import proved); it now runs with ``--no-board-body`` on a
    footprint whose model is the program's known-good STEP."""
    seen = []
    _fake_cli(monkeypatch, component={'step': [3, 3]}, control={'step': [0]}, seen=seen)
    faked, inputs = check._run_cli, {}

    def snapshot(command, **kwargs):
        # The scratch folder is gone after the run: the control's input is read at call time.
        board = Path(str(command[-1]))
        if _is_control([str(a) for a in command[1:]]) and board.suffix == '.kicad_pcb':
            inputs['board'] = board.read_text(encoding='utf-8')
            inputs['model'] = (board.parent / f'{check.CONTROL_NAME}.step').read_text(encoding='utf-8')
        return faked(command, **kwargs)
    monkeypatch.setattr(check, '_run_cli', snapshot)
    art = _component(tmp_path)
    assert _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_3d_import').status == 'ERROR'
    (args,) = [a for kind, ctl, a, _t in seen if kind == 'step' and ctl]
    component_args = [a for kind, ctl, a, _t in seen if kind == 'step' and not ctl][0]
    flags = [a for a in args if a.startswith('--')]
    assert flags == [a for a in component_args if a.startswith('--')] == ['--no-board-body']
    assert args[-1].endswith(f'{check.CONTROL_NAME}.kicad_pcb')
    assert inputs['model'] == check.CONTROL_STEP_TEXT
    assert f'${{KIPRJMOD}}/{check.CONTROL_NAME}.step' in inputs['board']
    _fake_cli(monkeypatch, component={'step': [3, 3]}, control={'step': [3]})
    row = _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'kicad_3d_import')
    assert row.status == 'SKIP' and row.cli_failure == 'environment'


def test_the_control_runs_under_the_same_scratch_conditions(tmp_path, monkeypatch):
    """@brief Positive: the control's input and output are fresh folders of the SAME scratch
    folder as the component's reads, never the component folder."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [2, 2]}, seen=seen)
    art = _component(tmp_path)
    check.verify_component(art.component_dir, 'Demo', ROWS)
    fp = [(ctl, Path(a[-1])) for kind, ctl, a, _t in seen if kind == 'fp']
    scratch = {path.parents[2] for _ctl, path in fp}
    assert len(scratch) == 1 and art.component_dir not in next(iter(scratch)).parents
    assert sum(ctl for ctl, _p in fp) == 1


# ── item 4: the budgets ─────────────────────────────────────────────────

def _fake_clock(monkeypatch, step):
    """A clock that advances `step` seconds at every reading."""
    ticks = iter(range(0, 10_000_000, step))
    monkeypatch.setattr(check, '_clock', lambda: next(ticks))


def test_the_repeat_gets_only_what_the_first_attempt_left(tmp_path, monkeypatch):
    """@brief Positive: the first attempt used 50 s -> the repeat gets 15 s (the floor, not 10);
    the control gets its own budget. Negative twin: 20 s used -> the repeat gets 40 s."""
    for used, expected in ((50, check.CLI_RETRY_MIN_TIMEOUT_S), (20, check.CLI_READ_TIMEOUT_S - 20)):
        seen = []
        _fake_cli(monkeypatch, component={'fp': [2, 2]}, seen=seen)
        _fake_clock(monkeypatch, used)
        check.verify_component(_component(tmp_path / str(used)).component_dir, 'Demo', ROWS)
        timeouts = [(ctl, timeout) for kind, ctl, _a, timeout in seen if kind == 'fp']
        assert timeouts == [(False, check.CLI_READ_TIMEOUT_S), (False, expected),
                            (True, check.CLI_CONTROL_TIMEOUT_S)], timeouts


def test_a_repeat_that_times_out_says_its_own_budget(tmp_path, monkeypatch):
    """@brief Positive: 2 then a timeout of the shortened repeat -> SKIP «timeout» naming the
    seconds the repeat had, not 60."""
    _fake_cli(monkeypatch, component={'fp': [2, subprocess.TimeoutExpired('kicad-cli', 15)]})
    _fake_clock(monkeypatch, 50)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'SKIP' and row.cli_failure == 'timeout'
    assert str(check.CLI_RETRY_MIN_TIMEOUT_S) in row.detail and '60' not in row.detail


# ── item 5: the declared exit code ──────────────────────────────────────

@pytest.mark.parametrize('code', [CRASH, 4, 1])
def test_a_non_refusal_exit_code_is_declared(tmp_path, monkeypatch, code):
    """@brief Positive: a read that ends with a crash or another code, its repeat and the
    differential control too -> SKIP whose `cli_exit_code` is that code (the window's
    persistent-failure signature).
    CONTRACT CHANGED 2026-11-15 (round 4, item 1): such a failure is repeated and controlled; it
    is the tool's (SKIP) only when the control fails too, so the controls fail here."""
    _fake_cli(monkeypatch, component={'fp': [code] * 2, 'sym': [code] * 4, 'step': [code] * 2},
              control={'fp': [code], 'sym': [code], 'step': [code]})
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    for read in READS:
        row = _row(results, read)
        assert row.status == 'SKIP' and row.cli_failure == 'not_run' and row.cli_exit_code == code


def test_exit_zero_without_output_declares_zero(tmp_path, monkeypatch):
    """@brief Positive: exit 0 with no output -> `cli_exit_code` 0, distinct from None."""
    _fake_cli(monkeypatch)
    monkeypatch.setattr(check, '_output_produced', lambda output, pattern: False)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'SKIP' and row.cli_exit_code == 0


@pytest.mark.parametrize('outcome', [subprocess.TimeoutExpired('kicad-cli', 60), OSError('x')])
def test_no_exit_code_without_a_completed_process(tmp_path, monkeypatch, outcome):
    """@brief Negative: a timeout or a start failure declares no exit code (None), and neither
    does a read, an ERROR or a row that is not a read."""
    _fake_cli(monkeypatch, component={'fp': [outcome]})
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_footprint_read').cli_exit_code is None
    assert all(r.cli_exit_code is None for r in results)


# ── the real kicad-cli ──────────────────────────────────────────────────

def _real_cli():
    """kicad-cli of this machine, or None (the GitHub runner has none)."""
    from kicadgen.services.system_dependencies import find_kicad_cli
    return find_kicad_cli()


@pytest.mark.parametrize('kind', sorted(check.CONTROL_READS))
def test_real_kicad_cli_reads_every_control(tmp_path, kind):
    """@brief With the real kicad-cli: each control input is read with exit 0 and an output."""
    cli = _real_cli()
    if not cli:
        pytest.skip('kicad-cli not installed')
    relative, text, make_args, pattern, output_name = check.CONTROL_READS[kind]
    source = tmp_path / 'in' / relative
    source.parent.mkdir(parents=True)
    source.write_text(text, encoding='utf-8')
    for extra, extra_text in check.CONTROL_EXTRA_FILES.get(kind, {}).items():
        (tmp_path / 'in' / extra).write_text(extra_text, encoding='utf-8')
    out = tmp_path / 'out'
    out.mkdir()
    output = out / output_name if output_name else out
    proc = check._run_cli([cli, *map(str, make_args(source, output))], timeout=120,
                          cwd=str(check.neutral_working_dir(cli)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert check._output_produced(output, pattern)


def test_real_kicad_cli_still_blames_a_corrupt_file_and_reads_a_sound_one(tmp_path):
    """@brief With the real kicad-cli: a footprint with an unknown token -> refused twice, the
    control read -> ERROR; the sound footprint -> OK (no control run)."""
    if not _real_cli():
        pytest.skip('kicad-cli not installed')
    sound = _component(tmp_path / 'sound')
    assert _row(check.verify_component(sound.component_dir, 'Demo', ROWS),
                'kicad_footprint_read').status == 'OK'
    corrupt = _component(tmp_path / 'corrupt')
    text = corrupt.footprint_path.read_text(encoding='utf-8')
    corrupt.footprint_path.write_text(text.replace('(pad ', '(bogus_token 1) (pad ', 1), encoding='utf-8')
    row = _row(check.verify_component(corrupt.component_dir, 'Demo', ROWS), 'kicad_footprint_read')
    assert row.status == 'ERROR', row


def test_real_kicad_cli_environment_refusal_is_not_the_components(tmp_path, monkeypatch):
    """@brief With the real kicad-cli wrapped: every FRESH footprint input is replaced by garbage
    before kicad-cli opens it (an environment that spoils new files) -> the component and the
    control are both refused -> SKIP «environment», never ERROR."""
    cli = _real_cli()
    if not cli:
        pytest.skip('kicad-cli not installed')
    real_run = check._run_cli

    def spoil(command, **kwargs):
        args = [str(a) for a in command[1:]]
        if _kind(args) == 'fp':
            for library in Path(args[-1]).glob('*.kicad_mod'):
                library.write_text('(footprint "x" (bogus_token 1))\n', encoding='utf-8')
        return real_run(command, **kwargs)
    monkeypatch.setattr(check, '_run_cli', spoil)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'SKIP' and row.cli_failure == 'environment', row
