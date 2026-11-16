"""@brief What concludes a blocking check: code families and real outcomes (review 2026-11-15, round 4).

@details Item 3: the DRC was concluded by the exact codes `drc`, `erc`,
`kicad_library`, but the ERC's violation rows are `erc_<kind>`: a run with
ERC violations that stopped before its last row left the DRC unconcluded
(«crash_incomplete» instead of «crashed»). The table now declares families
(``erc_*``). Item 7: a footprint held by another program gave a SKIP that
concluded `footprint_file`, and the run's last row concluded the rest, so
pads, terminals, model links, KiCad's read and pin-pad counted as concluded
although never made. Own rows now conclude only as an outcome, a «file in
uso» row never, and the footprint stage needs the footprint read. Positive
and negative twins, on real runs with a faked kicad-cli and on row lists.
"""

from pathlib import Path

import pytest

from kicadgen.services import component_check as check
from test_component_check_r3_control import _component, _fake_cli, _row
from test_component_check_r3_files import _hold

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
FULL = dict(cli_available=True, has_step=True, has_symbol_file=True)
NO_CLI = dict(cli_available=False, has_step=True, has_symbol_file=True)
FOOTPRINT_STAGE = {'footprint_file', 'footprint_pads_overlap', 'model_link', 'pin_pad'}


def _no_cli(monkeypatch):
    """kicad-cli absent."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)


# ── item 7: a held footprint concludes nothing of its stage ─────────────

def test_a_held_footprint_without_cli_leaves_its_checks_unconcluded(tmp_path, monkeypatch):
    """@brief Positive: footprint held, no kicad-cli -> footprint_file, pin_pad and the footprint
    stage's checks are missing although the run ended; the held row declares `file_busy`."""
    art = _component(tmp_path)
    _no_cli(monkeypatch)
    _hold(monkeypatch, art.footprint_path)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert results[-1].code == 'manual_checks'
    held = _row(results, 'footprint_file')
    assert held.status == 'SKIP' and held.file_busy
    assert _row(results, 'pin_pad').status == 'SKIP'
    assert FOOTPRINT_STAGE <= check.missing_required(results, **NO_CLI)


def test_a_normal_run_without_cli_misses_nothing(tmp_path, monkeypatch):
    """@brief Negative twin: the same component readable, no kicad-cli -> nothing missing, and no
    row declares `file_busy`."""
    art = _component(tmp_path)
    _no_cli(monkeypatch)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert check.missing_required(results, **NO_CLI) == frozenset()
    assert not any(row.file_busy for row in results)


def test_a_held_footprint_with_cli_leaves_the_reads_that_need_it_unconcluded(tmp_path, monkeypatch):
    """@brief Positive: footprint held, kicad-cli there -> KiCad's footprint read, the 3D import
    and the DRC (which need the footprint read) are missing too; the symbol reads are not."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    _hold(monkeypatch, art.footprint_path)
    missing = check.missing_required(check.verify_component(art.component_dir, 'Demo', ROWS), **FULL)
    assert FOOTPRINT_STAGE | {'kicad_footprint_read', 'kicad_3d_import', 'drc'} <= missing
    assert not missing & {'kicad_symbol_read', 'kicad_symbol_file_read', 'step_integrity'}


def test_a_normal_run_with_cli_misses_nothing(tmp_path, monkeypatch):
    """@brief Negative twin: everything readable, kicad-cli there -> nothing missing."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert check.missing_required(results, **FULL) == frozenset()


def test_a_held_saved_symbol_leaves_its_read_unconcluded(tmp_path, monkeypatch):
    """@brief Positive: the saved .kicad_sym held -> its read is a `file_busy` SKIP and stays
    missing; the applied symbol's read and the footprint are concluded."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    _hold(monkeypatch, art.component_dir / 'Demo.kicad_sym')
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_symbol_file_read').file_busy
    assert check.missing_required(results, **FULL) == {'kicad_symbol_file_read'}


@pytest.mark.parametrize('cause', ['not_run', 'timeout', 'scratch', 'start', 'environment'])
def test_a_tool_failure_skip_of_a_read_still_concludes_it(cause):
    """@brief Negative: a SKIP of KiCad's read for a tool failure IS the read's outcome (the window
    judges it by its read rules), unlike a «file in uso» SKIP of the same code."""
    rows = [check.Check('OK', 'f', 'x', code='footprint_file'),
            check.Check('SKIP', 'r', 'x', code='kicad_footprint_read', cli_failure=cause)]
    assert 'kicad_footprint_read' in check.concluded_required(rows, **FULL)
    busy = [rows[0], check.Check('SKIP', 'r', 'x', code='kicad_symbol_file_read', file_busy=True)]
    assert 'kicad_symbol_file_read' not in check.concluded_required(busy, **FULL)


def test_own_rows_conclude_only_as_an_outcome():
    """@brief Positive: footprint_file OK / ERROR and pin_pad OK / ERROR conclude themselves;
    negative: their SKIP does not, nor does the run's last row stand in for them."""
    end = check.Check('WARN', 'm', 'x', code='manual_checks')
    for status in ('OK', 'ERROR'):
        rows = [check.Check(status, 'f', 'x', code='footprint_file'),
                check.Check(status, 'p', 'x', code='pin_pad'), end]
        assert {'footprint_file', 'pin_pad'} <= check.concluded_required(rows, **NO_CLI)
    rows = [check.Check('SKIP', 'f', 'x', code='footprint_file'),
            check.Check('SKIP', 'p', 'x', code='pin_pad'), end]
    concluded = check.concluded_required(rows, **NO_CLI)
    assert not concluded & {'footprint_file', 'pin_pad', 'footprint_pads_overlap', 'model_link'}


def test_a_missing_footprint_stands_in_for_its_stage():
    """@brief Positive: `footprint_missing` (an ERROR that blocks on its own) concludes the
    footprint file and pin-pad, which could not be compared."""
    rows = [check.Check('ERROR', 'f', 'x', code='footprint_missing'),
            check.Check('SKIP', 'p', 'x', code='pin_pad'),
            check.Check('WARN', 'm', 'x', code='manual_checks')]
    assert {'footprint_file', 'pin_pad'} <= check.concluded_required(rows, **NO_CLI)


# ── item 3: the DRC concluded by the ERC's violation rows ───────────────

def _drc_rows(*erc_codes):
    """Rows of a run whose footprint was read, with DRC violations, then the given ERC rows."""
    return [check.Check('OK', 'f', 'x', code='footprint_file'),
            check.Check('WARN', 'd', 'x', code='drc_clearance'),
            check.Check('ERROR', 'd', 'x', code='drc_shorting_items')] + [
        check.Check('WARN', 'e', 'x', code=code) for code in erc_codes]


def test_an_erc_violation_row_concludes_the_drc():
    """@brief Positive: the ERC published a violation row `erc_<kind>` (and the run stopped
    there) -> the DRC is concluded, as by the ERC's single `erc` row."""
    assert 'drc' in check.concluded_required(_drc_rows('erc_pin_to_pin'), **FULL)
    assert 'drc' in check.concluded_required(_drc_rows('erc'), **FULL)


def test_drc_violation_rows_alone_do_not_conclude_the_drc():
    """@brief Negative twin: only DRC violation rows (statuses interleaved, more may follow) ->
    the DRC is not concluded; a code that merely starts with «erc» without the family's
    separator does not conclude it either."""
    assert 'drc' not in check.concluded_required(_drc_rows(), **FULL)
    assert 'drc' not in check.concluded_required(_drc_rows('ercx'), **FULL)


def test_a_run_with_erc_violations_interrupted_there_concludes_the_drc(tmp_path, monkeypatch):
    """@brief Positive, on a real run: DRC and ERC with violations, interrupted right after the
    first ERC violation row -> the DRC (with its contact ERROR) is concluded; interrupted on the
    last DRC row it is not."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    real_run = check._run_cli

    def reports(command, **kwargs):
        args = [str(a) for a in command[1:]]
        proc = real_run(command, **kwargs)
        output = Path(args[args.index('-o') + 1])
        if args[:2] == ['pcb', 'drc']:
            output.write_text('{"violations": [{"type": "silk_overlap", "severity": "warning", '
                              '"description": "x", "items": [{"uuid": "u1"}]}]}', encoding='utf-8')
        elif args[:2] == ['sch', 'erc']:
            output.write_text('{"sheets": [{"violations": [{"type": "pin_to_pin", "severity": '
                              '"error", "description": "y", "items": [{"uuid": "a"}, {"uuid": "b"}]'
                              '}]}]}', encoding='utf-8')
        return proc
    monkeypatch.setattr(check, '_run_cli', reports)
    full = check.verify_component(art.component_dir, 'Demo', ROWS)
    codes = [row.code for row in full]
    first_erc = next(i for i, code in enumerate(codes) if code.startswith('erc_'))
    last_drc = max(i for i, code in enumerate(codes) if code.startswith('drc_'))
    assert last_drc < first_erc
    assert 'drc' in check.concluded_required(full[:first_erc + 1], **FULL)
    assert 'drc' not in check.concluded_required(full[:last_drc + 1], **FULL)
