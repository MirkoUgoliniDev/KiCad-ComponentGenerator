"""@brief The checks an installation needs, as a declared contract (review 2026-11-15, round 2, R1).

@details The window told «crashed» (installation allowed) from «failed»
(blocked) by how many rows a run emitted; since the «KiCad CLI» row always
comes first, every crash was «crashed» even when nothing was verified.
`REQUIRED_FOR_INSTALL`, `required_for_install`, `concluded_required` and
`missing_required` declare which conclusions an installation needs, by
`Check.code`. A complete run concludes them all; a run interrupted after
its first row concludes none. Positive and negative twins.
"""

import subprocess
from pathlib import Path

import pytest

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]
FULL = dict(cli_available=True, has_step=True, has_symbol_file=True)
#: CONTRACT CHANGED 2026-11-15 (round 3, item 2): `REQUIRED_FOR_INSTALL` is
#: every blocking-capable check (`BLOCKING_CHECKS`); the document, contact
#: and terminal checks apply only when the run had them, declared by three
#: keyword-only flags that default to False.
EVERYTHING = dict(FULL, has_document=True, has_contacts=True, has_pad_leads=True)


class _Crash(Exception):
    """An internal failure in the middle of a run."""


def _fake_cli(monkeypatch):
    """kicad-cli faked: every command succeeds and writes its output."""
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        output = Path(args[args.index('-o') + 1])
        if args[:2] == ['pcb', 'drc']:
            output.write_text('{"violations": []}', encoding='utf-8')
        elif args[:2] == ['sch', 'erc']:
            output.write_text('{"sheets": []}', encoding='utf-8')
        elif args[:3] == ['pcb', 'export', 'step']:
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        else:
            (output / 'out.svg').write_text('<svg/>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(check, '_run_cli', run)


def _component(tmp_path):
    """The test component with its saved symbol file."""
    art = _artifacts(tmp_path)
    pins, body = symbol_inputs(ROWS)
    (art.component_dir / 'Demo.kicad_sym').write_text(
        serialize_symbol('Demo', pins, footprint='KiCadGen:Demo', body=body), encoding='utf-8')
    return art


def test_the_requirements_follow_what_the_component_has():
    """@brief Positive: everything present -> every blocking-capable code; negatives: without
    kicad-cli no KiCad read, without a STEP no integrity, without a saved symbol no read of it."""
    assert check.required_for_install(**EVERYTHING) == check.REQUIRED_FOR_INSTALL
    # CONTRACT CHANGED 2026-11-15 (round 3): without kicad-cli, a STEP or a
    # saved symbol, the checks made on the footprint and the applied symbol
    # alone remain -- they too can block.
    assert check.required_for_install(cli_available=False, has_step=False, has_symbol_file=False) == {
        'footprint_file', 'footprint_pads_overlap', 'model_link', 'pin_pad', 'symbol_error'}
    assert 'kicad_symbol_file_read' not in check.required_for_install(
        cli_available=True, has_step=True, has_symbol_file=False)
    assert 'step_integrity' not in check.required_for_install(
        cli_available=True, has_step=False, has_symbol_file=True)


def test_a_complete_run_concludes_every_requirement(tmp_path, monkeypatch):
    """@brief Positive: a run that ended normally -> nothing missing."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert check.concluded_required(results, **FULL) == set(check.required_for_install(**FULL))
    assert check.missing_required(results, **FULL) == frozenset()


@pytest.mark.parametrize('rows_before_crash', [1, 3])
def test_a_run_stopped_early_misses_what_it_never_concluded(tmp_path, monkeypatch, rows_before_crash):
    """@brief Negative twin: the run raises after its first rows («KiCad CLI», the footprint) ->
    the required conclusions not reached are missing, so the window cannot call it «crashed»."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    emitted = []

    def emit(result):
        emitted.append(result)
        if len(emitted) >= rows_before_crash:
            raise _Crash()
    with pytest.raises(_Crash):
        check.verify_component(art.component_dir, 'Demo', ROWS, emit=emit)
    missing = check.missing_required(emitted, **FULL)
    assert 'kicad_footprint_read' in missing and 'pin_pad' in missing
    if rows_before_crash == 1:
        assert missing == check.required_for_install(**FULL)


def test_a_row_that_replaces_a_check_concludes_it(tmp_path, monkeypatch):
    """@brief Positive: no footprint at all -> «footprint_missing» concludes the footprint
    requirement (and blocks on its own); without kicad-cli nothing of KiCad is required."""
    art = _component(tmp_path)
    art.footprint_path.unlink()
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    flags = dict(cli_available=False, has_step=True, has_symbol_file=True)
    assert 'footprint_file' in check.concluded_required(results, **flags)
    assert check.missing_required(results, **flags) == frozenset()


def test_the_codes_are_read_never_the_titles():
    """@brief Negative: rows with the right titles but no code conclude nothing."""
    rows = [check.Check('OK', 'Footprint su disco', 'x'), check.Check('OK', 'Corrispondenza pin-pad', 'y')]
    assert check.concluded_required(rows, **FULL) == set()
