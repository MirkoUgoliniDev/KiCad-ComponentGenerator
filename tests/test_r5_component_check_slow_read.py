"""@brief A kicad-cli read that times out gets one extended read before it is blamed on the component (review 2026-11-15, round 5, item 1).

@details Round 4 made a TIMEOUT whose differential control of the same kind
reads an ERROR «component»: one 60 s criterion then blocked for good a sound
but large file (a STEP model of tens of MB imported on a slow PC). Now such a
timeout gets ONE extended read (`CLI_SLOW_READ_TIMEOUT_S`, at most
`CLI_SLOW_READS_PER_RUN` per run): read -> the read's outcome with a «slow
read» note; timed out again -> ERROR «component»; a control that does not
read -> SKIP «timeout» with no extended read, as before; a run whose extended
read is spent -> SKIP «timeout», never an ERROR on the 60 s criterion alone.
kicad-cli and the clock are faked. Positive and negative twins.
"""

import subprocess

import pytest

from kicadgen.services import component_check as check
from test_component_check_r3_control import _component, _fake_cli, _row

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
TIMEOUT = subprocess.TimeoutExpired('kicad-cli', 60)
#: 0xC0000005, an access violation as kicad-cli reports a crash.
CRASH = 3221225477
#: (kind of read, code of its row) of a footprint read and of the 3D import.
READS = [('fp', 'kicad_footprint_read'), ('step', 'kicad_3d_import')]


@pytest.fixture(autouse=True)
def _still_clock(monkeypatch):
    """A clock that never advances: every budget is the declared one, no run is «late»."""
    monkeypatch.setattr(check, '_clock', lambda: 0)


def _calls(seen, kind):
    """(is_control, timeout) of every kicad-cli call of one kind, in order."""
    return [(ctl, timeout) for k, ctl, _args, timeout in seen if k == kind]


def test_the_extended_budget_is_a_declared_multiple_of_the_read_budget():
    """@brief The extended read is longer than the normal one by the declared factor, and one per run."""
    assert check.CLI_SLOW_READ_TIMEOUT_S == check.CLI_SLOW_READ_FACTOR * check.CLI_READ_TIMEOUT_S
    assert check.CLI_SLOW_READ_TIMEOUT_S > check.CLI_READ_TIMEOUT_S
    assert check.CLI_SLOW_READS_PER_RUN == 1


@pytest.mark.parametrize('kind, code', READS)
def test_a_slow_read_the_extended_read_completes_is_ok(tmp_path, monkeypatch, kind, code):
    """@brief Positive: timeout, control read, extended read OK -> OK (never ERROR), with the
    slow-read note naming both budgets; the third call carries `CLI_SLOW_READ_TIMEOUT_S`."""
    seen = []
    _fake_cli(monkeypatch, component={kind: [TIMEOUT, 0]}, control={kind: [0]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS), code)
    assert row.status == 'OK' and row.cli_failure == ''
    assert str(check.CLI_SLOW_READ_TIMEOUT_S) in row.detail
    assert str(check.CLI_READ_TIMEOUT_S) in row.detail
    assert _calls(seen, kind) == [(False, check.CLI_READ_TIMEOUT_S),
                                  (True, check.CLI_CONTROL_TIMEOUT_S),
                                  (False, check.CLI_SLOW_READ_TIMEOUT_S)]


@pytest.mark.parametrize('kind, code', READS)
def test_a_read_that_times_out_on_the_extended_read_too_is_the_components(tmp_path, monkeypatch,
                                                                          kind, code):
    """@brief Negative twin: the same calls, the extended read times out too -> ERROR «component»
    naming the extended seconds, with a remedy."""
    seen = []
    _fake_cli(monkeypatch, component={kind: [TIMEOUT, TIMEOUT]}, control={kind: [0]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS), code)
    assert (row.status, row.cli_failure) == ('ERROR', 'component') and row.remedy
    assert str(check.CLI_SLOW_READ_TIMEOUT_S) in row.detail
    assert [timeout for ctl, timeout in _calls(seen, kind) if not ctl] == [
        check.CLI_READ_TIMEOUT_S, check.CLI_SLOW_READ_TIMEOUT_S]


@pytest.mark.parametrize('control', [TIMEOUT, CRASH])
def test_a_timeout_whose_control_fails_gets_no_extended_read(tmp_path, monkeypatch, control):
    """@brief Negative: the control times out or crashes -> SKIP «timeout» as before round 4 and
    NO extended read (the tool, not the file, does not answer; waiting longer proves nothing)."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [TIMEOUT, 0]}, control={'fp': [control]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert (row.status, row.cli_failure) == ('SKIP', 'timeout')
    assert [ctl for ctl, _t in _calls(seen, 'fp')] == [False, True]


def test_an_extended_read_that_crashes_is_judged_against_the_control(tmp_path, monkeypatch):
    """@brief A timeout then a crash on the extended read: two failures on two fresh copies while
    the control reads -> ERROR «component» naming the crash code (the failure repeated)."""
    _fake_cli(monkeypatch, component={'fp': [TIMEOUT, CRASH]}, control={'fp': [0]})
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert (row.status, row.cli_failure) == ('ERROR', 'component')
    assert check._exit_code_text(CRASH) in row.detail


@pytest.mark.parametrize('per_run, status, cause', [(1, 'SKIP', 'timeout'), (2, 'ERROR', 'component')])
def test_the_extended_reads_of_a_run_are_capped(tmp_path, monkeypatch, per_run, status, cause):
    """@brief Run cap: the footprint read takes the run's one extended read (and completes); the
    3D import then times out with its control read and gets NONE -> SKIP «timeout» with the
    remedy to repeat, never an ERROR on the 60 s criterion alone. Twin: with two extended reads
    allowed the 3D import gets its own, which times out too -> ERROR «component»."""
    monkeypatch.setattr(check, 'CLI_SLOW_READS_PER_RUN', per_run)
    seen = []
    _fake_cli(monkeypatch, component={'fp': [TIMEOUT, 0], 'step': [TIMEOUT, TIMEOUT]},
              control={'fp': [0], 'step': [0]}, seen=seen)
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_footprint_read').status == 'OK'
    step = _row(results, 'kicad_3d_import')
    assert (step.status, step.cli_failure) == (status, cause) and step.remedy
    step_reads = [timeout for ctl, timeout in _calls(seen, 'step') if not ctl]
    if per_run == 1:
        assert step_reads == [check.CLI_READ_TIMEOUT_S]
    else:
        assert step_reads == [check.CLI_READ_TIMEOUT_S, check.CLI_SLOW_READ_TIMEOUT_S]
