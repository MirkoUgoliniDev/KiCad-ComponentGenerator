"""@brief The installed SYMBOL in the «Libreria KiCad» row, by round trip through a real install (review 2026-11-15).

@details M10: `library_checks(symbol_path=...)` had no test. Each case
installs the component with `kicad_library.install_component_library` into
`tmp_path` (no KiCad needed), then compares. M1: the check derives the
symbol with the installation's own rule (`kicad_library._component_symbol`),
so a component symbol the install would skip is never «OK» while the shared
library still holds an older one, and its remedy re-exports the symbol (a
reinstallation leaves the old one: `symbol_left_stale`). L1: an installed
library that is not UTF-8 is unreadable, never «simbolo mancante».
Positive and negative twins.
"""

from pathlib import Path

from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
from kicadgen.services import component_check as check
from kicadgen.services.kicad_library import install_component_library
from test_kicad_library import _artifacts

ROWS = [{'pin': '1', 'name': 'GPIO1'}]


def _symbol_text(name='Demo', rows=ROWS):
    """A component symbol library as the generator writes it."""
    pins, body = symbol_inputs(rows)
    return serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body)


def _installed(tmp_path, symbol_text=None):
    """A component with its own .kicad_sym, installed into a project library.

    @return (artifacts, symbol_path, installs, library .kicad_sym, install result).
    """
    art = _artifacts(tmp_path)
    symbol_path = art.component_dir / 'Demo.kicad_sym'
    symbol_path.write_text(symbol_text if symbol_text is not None else _symbol_text(),
                           encoding='utf-8')
    project = tmp_path / 'project'
    project.mkdir()
    result = install_component_library(art, project)
    installs = [{'scope': 'project', 'pretty_dir': str(project / 'KiCadGen.pretty')}]
    return art, symbol_path, installs, project / 'KiCadGen.kicad_sym', result


def _rows(art, installs, symbol_path):
    """The library rows of the installed component."""
    return check.library_checks('Demo', art.footprint_path.read_text(encoding='utf-8'), installs,
                                art.step_path, symbol_path=symbol_path)


# ── M10: the symbol comparison itself ───────────────────────────────────

def test_a_freshly_installed_symbol_is_ok(tmp_path):
    """@brief Positive: what the install wrote is what it would write now -> one OK row."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    assert library.is_file() and '"Demo"' in library.read_text(encoding='utf-8')
    rows = _rows(art, installs, symbol_path)
    assert [(r.status, r.code) for r in rows] == [('OK', 'kicad_library')]


def test_a_pin_name_edited_in_the_installed_library_reads_simbolo(tmp_path):
    """@brief Negative twin: a pin renamed in KiCad's editor -> WARN 2 «(simbolo)», reinstall remedy."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    text = library.read_text(encoding='utf-8')
    assert '"GPIO1"' in text
    library.write_text(text.replace('"GPIO1"', '"GPIO9"'), encoding='utf-8')
    (row,) = _rows(art, installs, symbol_path)
    assert row.status == 'WARN' and row.importance == check.IMPORTANCE_BEFORE_PRODUCTION
    assert '(simbolo)' in row.detail and row.action == check.ACTION_OPEN_KICAD_TAB


def test_a_symbol_absent_from_the_library_reads_simbolo_mancante(tmp_path):
    """@brief Negative: the library holds only another symbol -> «simbolo mancante»."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    text = library.read_text(encoding='utf-8')
    library.write_text(text.replace('(symbol "Demo"', '(symbol "Unrelated"', 1), encoding='utf-8')
    (row,) = _rows(art, installs, symbol_path)
    assert row.status == 'WARN' and 'simbolo mancante' in row.detail


def test_a_symbol_renamed_by_hand_in_the_component_file_is_ok(tmp_path):
    """@brief Positive: the component file holds ONE symbol of another name; the install names it
    «Demo» (`_library_symbol`), and the check, deriving it the same way, says OK."""
    art, symbol_path, installs, library, _result = _installed(tmp_path, _symbol_text('Renamed'))
    assert '(symbol "Demo"' in library.read_text(encoding='utf-8')
    assert [r.status for r in _rows(art, installs, symbol_path)] == ['OK']


def test_no_symbol_path_compares_no_symbol(tmp_path):
    """@brief Negative: symbol_path None -> the symbol is not compared (old callers unchanged)."""
    art, _symbol_path, installs, library, _result = _installed(tmp_path)
    library.unlink()
    assert [r.status for r in _rows(art, installs, None)] == ['OK']


# ── M1: a component symbol the install would skip ───────────────────────

def test_a_removed_component_symbol_with_an_old_one_installed_is_stale_never_ok(tmp_path):
    """@brief Positive: the component file is gone, the library keeps the earlier symbol ->
    WARN 2 «vecchio» with the re-export action, never OK; a real reinstall leaves it
    (`symbol_left_stale`), so «reinstall» alone could not be the remedy."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    symbol_path.unlink()
    rows = _rows(art, installs, symbol_path)
    assert not any(r.status == 'OK' for r in rows), rows
    (row,) = rows
    assert row.status == 'WARN' and row.importance == check.IMPORTANCE_BEFORE_PRODUCTION
    assert row.code == 'kicad_library' and row.action == check.ACTION_REEXPORT_SYMBOL
    assert 'vecchio' in row.detail and 'Riesporta il simbolo' in row.remedy
    again = install_component_library(art, tmp_path / 'project', overwrite=True)
    assert again.symbol_left_stale
    assert [r.action for r in _rows(art, installs, symbol_path)] == [check.ACTION_REEXPORT_SYMBOL]


def test_a_missing_component_symbol_never_installed_is_not_reported(tmp_path):
    """@brief Negative twin: no component symbol and no symbol of that name in the library ->
    the library is what an install writes now (no symbol): OK."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    symbol_path.unlink()
    library.unlink()
    assert [r.status for r in _rows(art, installs, symbol_path)] == ['OK']


def test_a_component_symbol_without_root_version_is_not_compared_but_stale(tmp_path):
    """@brief Positive (M1): a component file without a root ``(version)`` is skipped by the
    install («symbol_unreadable»); the check no longer compares it and asks to reinstall
    (which could not work) -- it says the installed one is old, with the re-export action."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    text = symbol_path.read_text(encoding='utf-8')
    without = text.replace('(version 20251024)', '', 1)
    assert without != text
    symbol_path.write_text(without, encoding='utf-8')
    (row,) = _rows(art, installs, symbol_path)
    assert row.status == 'WARN' and row.action == check.ACTION_REEXPORT_SYMBOL
    assert '(simbolo)' not in row.detail


# ── L1: an installed library that cannot be read ────────────────────────

def test_a_library_that_is_not_utf8_is_unreadable_not_missing(tmp_path):
    """@brief Positive: the library re-saved in cp1252 -> WARN 2 «non leggibile (non è testo
    UTF-8)», never «simbolo mancante», and never OK."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    text = library.read_text(encoding='utf-8')
    library.write_bytes(text.replace('"GPIO1"', '"GPIO1 è"').encode('cp1252'))
    rows = _rows(art, installs, symbol_path)
    assert not any(r.status == 'OK' for r in rows)
    assert not any('simbolo mancante' in r.detail for r in rows)
    (row,) = rows
    assert row.status == 'WARN' and 'UTF-8' in row.detail and row.importance == 2


def test_a_library_file_that_is_gone_is_simbolo_mancante(tmp_path):
    """@brief Negative twin: the library file does not exist -> «simbolo mancante» (reinstall fixes it)."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    library.unlink()
    (row,) = _rows(art, installs, symbol_path)
    assert row.status == 'WARN' and 'simbolo mancante' in row.detail
    assert row.action == check.ACTION_OPEN_KICAD_TAB
    assert Path(symbol_path).is_file()
