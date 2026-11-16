"""@brief Files held by another program, the check's own scratch output, and the recorded global tables (review 2026-11-15, round 3).

@details Item 3: an OSError while scanning kicad-cli's output (an antivirus
quarantining the SVG) escaped into the footprint section and became «Nessun
footprint nella cartella del componente» (ERROR); a component file another
program holds (PermissionError) read as missing or as a defect. Now the
scan is protected (SKIP «not run») and a held file is a SKIP with a «file
in uso» cause and remedy -- never an ERROR -- while a folder in a file's
place and a file that really is missing keep their ERROR; a held STEP
keeps its ERROR on purpose (the window waits for no read of the STEP). Item 6: the «Libreria KiCad» row no longer resolves «the current»
global table for a global installation recorded without one (it may be
another KiCad version's); it reads the `table_path` and `symbol_table_path`
the record names. Positive and negative twins.
"""

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import UTF8_BOM, install_component_library
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]


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


def _codes(results, status):
    """The codes of the rows of one status."""
    return {r.code for r in results if r.status == status}


def _row(results, code):
    """The one row of a code."""
    (row,) = [r for r in results if r.code == code]
    return row


# ── item 3: the scan of kicad-cli's output ──────────────────────────────

def test_an_unreadable_output_is_not_run_never_a_missing_footprint(tmp_path, monkeypatch):
    """@brief Positive: the output scan raises (an antivirus took the SVG) -> the read is SKIP
    «not run»; the footprint stays read (footprint_file OK), no «footprint_missing» ERROR.
    CONTRACT CHANGED 2026-11-15 (round 4, item 6): the row declares «scratch» (the check's own
    temporary folder), no longer the generic «not_run» the window words as a KiCad problem."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)

    def quarantined(output, pattern):
        raise PermissionError(13, 'Access is denied', str(output))
    monkeypatch.setattr(check, '_output_produced', quarantined)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    row = _row(results, 'kicad_footprint_read')
    assert row.status == 'SKIP' and row.cli_failure == 'scratch' and row.remedy
    assert _row(results, 'footprint_file').status == 'OK'
    assert not _codes(results, 'ERROR')


def test_a_footprint_that_really_is_missing_is_still_an_error(tmp_path, monkeypatch):
    """@brief Negative twin: no footprint file at all -> «footprint_missing» ERROR (unchanged)."""
    art = _component(tmp_path)
    art.footprint_path.unlink()
    _fake_cli(monkeypatch)
    assert 'footprint_missing' in _codes(check.verify_component(art.component_dir, 'Demo', ROWS),
                                         'ERROR')


def test_an_unreadable_3d_output_leaves_the_solids_unconfirmed(tmp_path, monkeypatch):
    """@brief Positive: the assembly STEP the check wrote cannot be read back -> WARN «solids
    unconfirmed», never a «Modello STEP» ERROR (the scratch output is the check's own)."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    real_read_text = Path.read_text

    def refuse(self, *args, **kwargs):
        if self.name == 'assembly.step':
            raise PermissionError(13, 'Access is denied', str(self))
        return real_read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', refuse)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert _row(results, 'kicad_3d_import').status == 'OK'
    assert _row(results, 'solids_unconfirmed').status == 'WARN'
    assert not _codes(results, 'ERROR')


# ── item 3: component files held by another program ─────────────────────

def _hold(monkeypatch, target):
    """Make reading `target` raise the PermissionError of a file another program holds."""
    real_read_bytes = Path.read_bytes

    def held(self):
        if Path(self) == target:
            raise PermissionError(13, 'The process cannot access the file', str(self))
        return real_read_bytes(self)
    monkeypatch.setattr(Path, 'read_bytes', held)


def test_a_held_footprint_is_a_skip_with_its_cause(tmp_path, monkeypatch):
    """@brief Positive: the .kicad_mod held by another program -> SKIP footprint_file with a
    remedy naming the file; neither «footprint_missing» nor any ERROR."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    _hold(monkeypatch, art.footprint_path)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    row = _row(results, 'footprint_file')
    assert row.status == 'SKIP' and row.remedy and str(art.footprint_path) in row.detail
    assert not _codes(results, 'ERROR')


def test_a_folder_in_the_footprints_place_stays_an_error(tmp_path, monkeypatch):
    """@brief Negative twin: a FOLDER named like the footprint (Windows refuses it with the same
    PermissionError) is not a held file -> footprint_file ERROR, as before."""
    art = _component(tmp_path)
    art.footprint_path.unlink()
    art.footprint_path.mkdir()
    _fake_cli(monkeypatch)
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    assert _row(results, 'footprint_file').status == 'ERROR'


def test_a_held_symbol_file_is_a_skip_of_its_read(tmp_path, monkeypatch):
    """@brief Positive: the saved .kicad_sym held -> SKIP kicad_symbol_file_read with a remedy,
    no «symbol_error» ERROR; negative twin in the same run: the applied symbol is still read."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    _hold(monkeypatch, art.component_dir / 'Demo.kicad_sym')
    results = check.verify_component(art.component_dir, 'Demo', ROWS)
    row = _row(results, 'kicad_symbol_file_read')
    assert row.status == 'SKIP' and row.remedy
    assert _row(results, 'kicad_symbol_read').status == 'OK'
    assert not _codes(results, 'ERROR')


def test_a_held_step_stays_an_error_and_a_sound_one_is_ok(tmp_path, monkeypatch):
    """@brief Negative (scope of item 3): the STEP held by another program keeps its «step_model»
    ERROR -- as a SKIP the window would allow the installation with the STEP unchecked; its
    positive twin, the same STEP readable, is OK."""
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    assert _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'step_integrity').status == 'OK'

    def held(path):
        raise PermissionError(13, 'The process cannot access the file', str(path))
    monkeypatch.setattr(check, 'step_file_is_complete', held)
    assert 'step_model' in _codes(check.verify_component(art.component_dir, 'Demo', ROWS), 'ERROR')


@pytest.mark.skipif(sys.platform != 'win32', reason='exclusive file locks through the Windows API')
def test_a_footprint_locked_by_a_real_process_is_held(tmp_path, monkeypatch):
    """@brief Positive, with a real Windows lock (share mode 0) on the .kicad_mod -> SKIP
    footprint_file «in uso»; after the lock is released the same run is OK."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    art = _component(tmp_path)
    _fake_cli(monkeypatch)
    handle = kernel.CreateFileW(str(art.footprint_path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        results = check.verify_component(art.component_dir, 'Demo', ROWS)
    finally:
        kernel.CloseHandle(handle)
    assert _row(results, 'footprint_file').status == 'SKIP'
    assert not _codes(results, 'ERROR')
    assert _row(check.verify_component(art.component_dir, 'Demo', ROWS), 'footprint_file').status == 'OK'


# ── item 6: the global tables a record names ────────────────────────────

def _global_install(tmp_path):
    """The component (with its symbol) installed globally with explicit tables in a config folder."""
    art = _component(tmp_path)
    config = tmp_path / 'config'
    config.mkdir()
    table = config / 'fp-lib-table'
    table.write_text('(fp_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    sym_table = config / 'sym-lib-table'
    sym_table.write_text('(sym_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    libraries = tmp_path / 'libraries'
    install_component_library(art, libraries, project_library=False, table_path=table,
                              symbol_table_path=sym_table)
    record = {'scope': 'global', 'pretty_dir': str(libraries / 'KiCadGen.pretty')}
    return art, record, table, sym_table


def _library_rows(art, record):
    """The «Libreria KiCad» rows of the component against one install record."""
    return check.library_checks('Demo', art.footprint_path.read_text(encoding='utf-8'), [record],
                                art.step_path, symbol_path=art.component_dir / 'Demo.kicad_sym')


def test_recorded_global_tables_are_checked_both(tmp_path):
    """@brief Positive: a record naming both tables -> a BOM in either is named (fp and sym)."""
    art, record, table, sym_table = _global_install(tmp_path)
    record = dict(record, table_path=str(table), symbol_table_path=str(sym_table))
    assert [r.status for r in _library_rows(art, record)] == ['OK']
    for path in (table, sym_table):
        original = path.read_bytes()
        path.write_bytes(UTF8_BOM + original)
        (row,) = _library_rows(art, record)
        assert row.status == 'WARN' and path.name in row.detail, path
        path.write_bytes(original)


def test_the_recorded_symbol_table_wins_over_the_one_beside(tmp_path):
    """@brief Negative twin: the record names a sym-lib-table elsewhere -> a BOM in the file
    beside the fp-lib-table is NOT named; a BOM in the recorded one is."""
    art, record, table, sym_table = _global_install(tmp_path)
    elsewhere = tmp_path / 'other' / 'sym-lib-table'
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(sym_table.read_bytes())
    record = dict(record, table_path=str(table), symbol_table_path=str(elsewhere))
    sym_table.write_bytes(UTF8_BOM + sym_table.read_bytes())
    assert [r.status for r in _library_rows(art, record)] == ['OK']
    elsewhere.write_bytes(UTF8_BOM + elsewhere.read_bytes())
    (row,) = _library_rows(art, record)
    assert row.status == 'WARN' and 'sym-lib-table' in row.detail


def test_a_record_without_tables_is_not_resolved_to_the_current_one(tmp_path, monkeypatch):
    """@brief Negative: a global record WITHOUT table paths -> verify_component never resolves
    «the current» global table (it may be another KiCad version's); a BOM there is not named."""
    art, record, table, _sym = _global_install(tmp_path)
    table.write_bytes(UTF8_BOM + table.read_bytes())

    def forbidden(*_args, **_kwargs):
        raise AssertionError('the current global table must not be resolved')
    monkeypatch.setattr(kicad_library, 'resolve_global_fp_lib_table', forbidden)
    _fake_cli(monkeypatch)
    results = check.verify_component(art.component_dir, 'Demo', ROWS, installs=[record])
    assert _row(results, 'kicad_library').status == 'OK'
    # Positive twin: the same record WITH its table names the BOM.
    results = check.verify_component(art.component_dir, 'Demo', ROWS,
                                     installs=[dict(record, table_path=str(table))])
    row = _row(results, 'kicad_library')
    assert row.status == 'WARN' and 'fp-lib-table' in row.detail
