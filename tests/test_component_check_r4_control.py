"""@brief Every tool-side failure of a KiCad read is judged against the differential control (review 2026-11-15, round 4).

@details Item 1: a component whose OWN input makes kicad-cli crash (a name
«Relay_x%NOPE%y» crashes every footprint and symbol read of kicad-cli 10,
0xC0000409, while the DRC and ERC of the same run finish) was a SKIP «not
run» -- a tool failure, eventually «KiCad broken» and installable. Now a
crash, another exit code, exit 0 without output and a timeout are judged
like a repeated refusal: the program's own known-good file of the same kind
is read under the same conditions; read -> ERROR «component» with a remedy
(rename when the name holds a character KiCad mis-handles); failed too ->
the SKIP of before, with its cause. Item 4: the STEP control imports a
model with the component's own flags. Item 5: the controls carry the
generators' format versions. Item 6: a failure of the check's own scratch
folder is declared «scratch». The fakes run without KiCad; the real-cli
tests are skipped without it. Positive and negative twins.
"""

import re
import subprocess
from pathlib import Path

import pytest

from kicadgen.core.footprint import KICAD_VERSION
from kicadgen.generators.symbol import (SYMBOL_FORMAT_VERSION, serialize_symbol,
                                        symbol_inputs)
from kicadgen.services import component_check as check
from kicadgen.services.kicad_library import create_preview_project
from test_component_check_r3_control import _component, _fake_cli, _row
from test_kicad_library import FOOTPRINT, _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
#: 0xC0000409, the code kicad-cli 10 ends with on a name holding «%NOPE%».
FAST_FAIL = 3221226505
TIMEOUT = subprocess.TimeoutExpired('kicad-cli', 60)


def _named_component(tmp_path, name, *, saved_symbol=True):
    """A component folder for any name: footprint (and saved symbol), no STEP."""
    folder = tmp_path / 'component'
    (folder / f'{name}.pretty').mkdir(parents=True)
    (folder / f'{name}.pretty' / f'{name}.kicad_mod').write_text(
        FOOTPRINT.replace('"Demo"', f'"{name}"'), encoding='utf-8')
    if saved_symbol:
        pins, body = symbol_inputs(ROWS)
        (folder / f'{name}.kicad_sym').write_text(
            serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body), encoding='utf-8')
    return folder


# ── item 1: crash, no output, timeout against the control ───────────────

@pytest.mark.parametrize('failure, cause, exit_code', [
    (FAST_FAIL, 'not_run', FAST_FAIL), (4, 'not_run', 4)])
def test_a_crash_the_control_shares_stays_the_tools(tmp_path, monkeypatch, failure, cause,
                                                    exit_code):
    """@brief Negative: the footprint read fails twice and the fp control fails the same way ->
    SKIP with the cause and exit code of before round 4, plus a line saying the control failed."""
    _fake_cli(monkeypatch, component={'fp': [failure, failure]}, control={'fp': [failure]})
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'SKIP' and (row.cli_failure, row.cli_exit_code) == (cause, exit_code)
    assert row.remedy


@pytest.mark.parametrize('failure', [FAST_FAIL, 4, 1])
def test_a_crash_the_control_does_not_share_is_the_components(tmp_path, monkeypatch, failure):
    """@brief Positive twin: the same two failures, the fp control READ -> ERROR «component»,
    importance 1, naming the code, with a remedy; three fp calls (two reads, one control)."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [failure, failure]}, control={'fp': [0]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'ERROR' and row.cli_failure == 'component'
    assert row.importance == check.IMPORTANCE_BLOCKS_USE and row.remedy
    assert check._exit_code_text(failure) in row.detail
    assert [ctl for kind, ctl, _a, _t in seen if kind == 'fp'] == [False, False, True]


def test_a_crash_that_does_not_repeat_is_a_read(tmp_path, monkeypatch):
    """@brief Negative: a crash then a read on the fresh copy -> OK, and no control at all (a
    transient crash says nothing about the file)."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [FAST_FAIL, 0]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'OK'
    assert not any(ctl for kind, ctl, _a, _t in seen if kind == 'fp')


@pytest.mark.parametrize('control_ok', [True, False])
def test_exit_zero_without_output(tmp_path, monkeypatch, control_ok):
    """@brief Positive (control writes its output) -> ERROR «component»; negative (the control
    exits 0 without output too) -> SKIP «not_run» with exit code 0."""
    _fake_cli(monkeypatch)
    real_produced = check._output_produced

    def produced(output, pattern):
        # The component's reads write nothing; the control does unless it fails too.
        if 'control-' in str(output):
            return control_ok and real_produced(output, pattern)
        return False
    monkeypatch.setattr(check, '_output_produced', produced)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    if control_ok:
        assert row.status == 'ERROR' and row.cli_failure == 'component'
    else:
        assert row.status == 'SKIP' and (row.cli_failure, row.cli_exit_code) == ('not_run', 0)


@pytest.mark.parametrize('control, status, cause', [
    ([0], 'ERROR', 'component'), ([TIMEOUT], 'SKIP', 'timeout'), ([FAST_FAIL], 'SKIP', 'timeout')])
def test_a_timeout_is_controlled_once_without_repeat(tmp_path, monkeypatch, control, status,
                                                     cause):
    """@brief A timeout of the footprint read is NOT repeated with the normal budget and goes to
    the fp control: read -> one EXTENDED read, timing out too -> ERROR «component» naming the
    extended seconds; control timed out or crashed -> SKIP «timeout» as before round 4, with no
    extended read.
    CONTRACT CHANGED 2026-11-15 (round 5): a control that reads no longer makes the first 60 s
    timeout the component's at once -- a large file on a slow PC would be blocked for good on one
    criterion -- so the ERROR now needs the extended read to time out as well (third fp call,
    budget `CLI_SLOW_READ_TIMEOUT_S`)."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [TIMEOUT, TIMEOUT]}, control={'fp': control},
              seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert (row.status, row.cli_failure) == (status, cause)
    assert row.cli_exit_code is None
    calls = [(ctl, timeout) for kind, ctl, _a, timeout in seen if kind == 'fp']
    if status == 'ERROR':
        assert calls == [(False, check.CLI_READ_TIMEOUT_S), (True, check.CLI_CONTROL_TIMEOUT_S),
                         (False, check.CLI_SLOW_READ_TIMEOUT_S)]
        assert str(check.CLI_SLOW_READ_TIMEOUT_S) in row.detail
    else:
        assert [ctl for ctl, _t in calls] == [False, True]


def test_a_timeout_late_in_the_run_gets_no_new_control(tmp_path, monkeypatch):
    """@brief Budget bound: past `CLI_TIMEOUT_CONTROL_LIMIT_S` a timeout gets no NEW control (SKIP
    «timeout», no fp control call); a control of the kind already made is reused (the saved
    symbol's timeout reuses the sym control made for the applied symbol's refusal -> ERROR).
    CONTRACT CHANGED 2026-11-15 (round 5): the reused control reads, so the saved symbol's
    timeout gets the run's one extended read, which times out too (the fourth sym outcome)
    before the ERROR; the extended read needs no new control, so it is allowed late."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [TIMEOUT], 'sym': [2, 2, TIMEOUT, TIMEOUT]},
              control={'sym': [0]}, seen=seen)
    late = check.CLI_TIMEOUT_CONTROL_LIMIT_S + 1
    monkeypatch.setattr(check, '_clock', lambda: late if seen else 0)
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    footprint = _row(results, 'kicad_footprint_read')
    assert (footprint.status, footprint.cli_failure) == ('SKIP', 'timeout')
    assert not any(ctl for kind, ctl, _a, _t in seen if kind == 'fp')
    saved = _row(results, 'kicad_symbol_file_read')
    assert (saved.status, saved.cli_failure) == ('ERROR', 'component')
    assert [ctl for kind, ctl, _a, _t in seen if kind == 'sym'].count(True) == 1


def test_a_name_kicad_mishandles_gets_the_rename_remedy(tmp_path, monkeypatch):
    """@brief Positive: a crash the control does not share, on a component named with «%» ->
    the remedy names «%» and says to rename; negative twin: the same crash on «Demo» -> a
    remedy without «%» (regenerate)."""
    name = 'Relay_x%NOPE%y'
    _fake_cli(monkeypatch, component={'fp': [FAST_FAIL, FAST_FAIL]}, control={'fp': [0]})
    row = _row(check.verify_component(_named_component(tmp_path / 'pct', name), name, ROWS),
               'kicad_footprint_read')
    assert row.status == 'ERROR' and '%' in row.remedy and 'Rinomina progetto' in row.remedy
    _fake_cli(monkeypatch, component={'fp': [FAST_FAIL, FAST_FAIL]}, control={'fp': [0]})
    row = _row(check.verify_component(_component(tmp_path / 'demo').component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.status == 'ERROR' and '%' not in row.remedy and row.remedy


def test_a_start_failure_makes_no_control(tmp_path, monkeypatch):
    """@brief Negative: kicad-cli that cannot start -> SKIP «start», no control is attempted."""
    seen = []
    _fake_cli(monkeypatch, component={'fp': [OSError('blocked')]}, seen=seen)
    row = _row(check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS),
               'kicad_footprint_read')
    assert row.cli_failure == 'start'
    assert not any(ctl for kind, ctl, _a, _t in seen if kind == 'fp')


# ── item 6: the check's own scratch side ────────────────────────────────

def test_a_scratch_folder_that_refuses_files_is_declared_scratch(tmp_path, monkeypatch):
    """@brief Positive: the attempt's output folder refuses the probe -> every read SKIP
    «scratch» (not «not_run»: the window names the temporary folder, not KiCad)."""
    _fake_cli(monkeypatch)
    real_write = Path.write_bytes

    def refuse_probe(self, data):
        if self.name == '.kicadgen-probe':
            raise PermissionError(13, 'Access is denied', str(self))
        return real_write(self, data)
    monkeypatch.setattr(Path, 'write_bytes', refuse_probe)
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    for code in ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read'):
        row = _row(results, code)
        assert (row.status, row.cli_failure) == ('SKIP', 'scratch') and row.remedy


def test_a_kicad_failure_is_not_declared_scratch(tmp_path, monkeypatch):
    """@brief Negative twin: a crash of kicad-cli shared by the control -> «not_run», never
    «scratch»; a read -> no cause at all."""
    _fake_cli(monkeypatch, component={'fp': [FAST_FAIL, FAST_FAIL]}, control={'fp': [FAST_FAIL]})
    results = check.verify_component(_component(tmp_path).component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_footprint_read').cli_failure == 'not_run'
    assert _row(results, 'kicad_symbol_read').cli_failure == ''


# ── item 5: the controls' format versions ───────────────────────────────

def _version(text):
    """The root ``(version N)`` of a KiCad file."""
    return int(re.search(r'\(version\s+(\d+)\)', text).group(1))


def test_the_controls_carry_the_generators_versions(tmp_path):
    """@brief Positive: the fp and board controls carry the footprint/board version the program
    writes (a generated footprint and the preview board), the sym control the symbol's."""
    pins, body = symbol_inputs(ROWS)
    symbol = serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body)
    board = create_preview_project(_artifacts(tmp_path)).board_path.read_text(encoding='utf-8')
    assert _version(check.CONTROL_READS['fp'][1]) == KICAD_VERSION == _version(board)
    assert _version(check.CONTROL_READS['step'][1]) == _version(board)
    assert _version(check.CONTROL_READS['sym'][1]) == SYMBOL_FORMAT_VERSION == _version(symbol)


def test_the_control_versions_are_not_frozen_literals():
    """@brief Negative: the controls' versions are written from the generators' constants, never
    as number literals, so raising a constant raises the controls (read on the source of the
    `CONTROL_READS` table)."""
    source = Path(check.__file__).read_text(encoding='utf-8')
    table = source[source.index('CONTROL_READS = {'):source.index('def _box_step_text')]
    assert not re.search(r'\(version\s+\d', table)
    assert table.count('(version {KICAD_VERSION})') == 2
    assert table.count('(version {SYMBOL_FORMAT_VERSION})') == 1


# ── item 4: the STEP control imports a model ────────────────────────────

def test_the_control_step_model_is_one_closed_box():
    """@brief The control model is a complete ISO 10303-21 file with one manifold solid of six
    faces, twelve edges and eight vertices (a malformed model would make a sound environment
    look broken)."""
    text = check.CONTROL_STEP_TEXT
    assert text.startswith('ISO-10303-21;') and text.rstrip().endswith('END-ISO-10303-21;')
    assert text.count('MANIFOLD_SOLID_BREP(') == 1 and text.count('ADVANCED_FACE(') == 6
    assert text.count('EDGE_CURVE(') == 12 and text.count('VERTEX_POINT(') == 8
    # Each edge is used by exactly two faces, once in each direction (a closed, oriented shell).
    uses = re.findall(r"ORIENTED_EDGE\('',\*,\*,(#\d+),\.(T|F)\.\)", text)
    by_edge = {}
    for edge, sense in uses:
        by_edge.setdefault(edge, []).append(sense)
    assert len(by_edge) == 12 and all(sorted(s) == ['F', 'T'] for s in by_edge.values())


# ── the real kicad-cli ──────────────────────────────────────────────────

def _real_cli():
    """kicad-cli of this machine, or None (the GitHub runner has none)."""
    from kicadgen.services.system_dependencies import find_kicad_cli
    return find_kicad_cli()


def _run_step_control(cli, folder, model_text):
    """The STEP control, run by hand with a given model text: (exit code, output text or '')."""
    relative, text, make_args, _pattern, output_name = check.CONTROL_READS['step']
    source = folder / 'in' / relative
    source.parent.mkdir(parents=True)
    source.write_text(text, encoding='utf-8')
    for extra in check.CONTROL_EXTRA_FILES['step']:
        (folder / 'in' / extra).write_text(model_text, encoding='utf-8')
    output = folder / 'out' / output_name
    output.parent.mkdir()
    proc = check._run_cli([cli, *map(str, make_args(source, output))], timeout=120,
                          cwd=str(check.neutral_working_dir(cli)))
    return proc.returncode, output.read_text(errors='replace') if output.is_file() else ''


def test_real_kicad_cli_the_step_control_imports_its_model(tmp_path):
    """@brief With the real kicad-cli: the control's board exports its model's solid with
    ``--no-board-body`` (positive); the same board with an empty model is refused (exit 2,
    negative) -- so an environment that cannot load models fails the control too."""
    cli = _real_cli()
    if not cli:
        pytest.skip('kicad-cli not installed')
    code, output = _run_step_control(cli, tmp_path / 'sound', check.CONTROL_STEP_TEXT)
    assert code == 0 and 'MANIFOLD_SOLID_BREP' in output
    empty = 'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n'
    code, _output = _run_step_control(cli, tmp_path / 'empty', empty)
    assert code in check.CLI_INPUT_REFUSED


def test_real_kicad_cli_a_percent_name_is_the_components(tmp_path):
    """@brief With the real kicad-cli 10: a component named «Relay_x%NOPE%y» crashes the footprint
    and symbol reads on every run while the controls read -> ERROR «component» with the rename
    remedy; the twin named «Relay_x_NOPE_y» reads (the name, not the content, is the cause)."""
    if not _real_cli():
        pytest.skip('kicad-cli not installed')
    name = 'Relay_x%NOPE%y'
    results = check.verify_component(_named_component(tmp_path / 'pct', name), name, ROWS)
    for code in ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read'):
        row = _row(results, code)
        assert (row.status, row.cli_failure) == ('ERROR', 'component'), row
        assert '%' in row.remedy
    safe = 'Relay_x_NOPE_y'
    results = check.verify_component(_named_component(tmp_path / 'safe', safe), safe, ROWS)
    for code in ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read'):
        assert _row(results, code).status == 'OK'


def test_real_kicad_cli_a_crashing_library_content_is_the_components(tmp_path):
    """@brief With the real kicad-cli 10: the saved symbol library holds, beside the component's
    sound symbol, a symbol named «x%n» (kicad-cli exports every symbol and crashes) -> ERROR
    «component» on the saved file's read, with the regenerate remedy (the component's NAME is
    safe); the applied symbol, without it, reads."""
    if not _real_cli():
        pytest.skip('kicad-cli not installed')
    folder = _named_component(tmp_path, 'Demo')
    saved = folder / 'Demo.kicad_sym'
    text = saved.read_text(encoding='utf-8')
    pins, body = symbol_inputs(ROWS)
    other = serialize_symbol('x%n', pins, footprint='KiCadGen:x', body=body)
    block = other[other.index('  (symbol "x%n"'):other.rstrip().rindex(')')]
    saved.write_text(text.rstrip()[:-1] + block + ')\n', encoding='utf-8')
    results = check.verify_component(folder, 'Demo', ROWS)
    row = _row(results, 'kicad_symbol_file_read')
    assert (row.status, row.cli_failure) == ('ERROR', 'component'), row
    assert '%' not in row.remedy
    assert _row(results, 'kicad_symbol_read').status == 'OK'
