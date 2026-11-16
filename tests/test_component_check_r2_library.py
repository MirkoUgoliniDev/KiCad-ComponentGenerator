"""@brief «Libreria KiCad» row: a leading BOM is «not up to date», an unreadable library is never OK (review 2026-11-15, round 2).

@details R8: KiCad 10 refuses a .kicad_sym, a .kicad_mod or a library
table that starts with a UTF-8 BOM (kicad-cli 10.0 `sym export svg` exit 2,
ERC `lib_symbol_issues`), and a reinstallation rewrites them without it;
the comparisons drop the BOM, so the row said «aggiornata». Now a BOM in a
file the installation writes is named, with the reinstall remedy, and a
real reinstallation clears it. R9: with an unusable component symbol, an
installed library that cannot be read read as «nothing to say» -> OK; the
unreadable library is now checked first. Each case installs the component
for real into `tmp_path` (no KiCad needed). Positive and negative twins.
"""

from kicadgen.services import component_check as check
from kicadgen.services.kicad_library import UTF8_BOM, install_component_library
from test_component_check_c1_library_symbol import _installed, _rows
from test_kicad_library import _artifacts


def _add_bom(path):
    """Prefix a file with a UTF-8 BOM."""
    path.write_bytes(UTF8_BOM + path.read_bytes())


# ── R8: a BOM in what the installation writes ───────────────────────────

def test_each_installed_file_with_a_bom_is_not_up_to_date(tmp_path):
    """@brief Positive: a BOM in the installed .kicad_mod, .kicad_sym, fp-lib-table or
    sym-lib-table -> WARN 2 «non aggiornata» naming that file, reinstall action; never OK."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    project = library.parent
    for path in (project / 'KiCadGen.pretty' / 'Demo.kicad_mod', library,
                 project / 'fp-lib-table', project / 'sym-lib-table'):
        assert path.is_file(), path
        original = path.read_bytes()
        _add_bom(path)
        (row,) = _rows(art, installs, symbol_path)
        assert row.status == 'WARN' and row.importance == check.IMPORTANCE_BEFORE_PRODUCTION, path
        assert path.name in row.detail and 'BOM' in row.detail
        assert row.action == check.ACTION_OPEN_KICAD_TAB
        path.write_bytes(original)
    assert [r.status for r in _rows(art, installs, symbol_path)] == ['OK']


def test_a_reinstallation_clears_the_bom_it_named(tmp_path):
    """@brief Positive round trip: the remedy works -- after a real reinstallation the same files
    hold no BOM and the row is OK again."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    project = library.parent
    bommed = [project / 'KiCadGen.pretty' / 'Demo.kicad_mod', library,
              project / 'fp-lib-table', project / 'sym-lib-table']
    for path in bommed:
        _add_bom(path)
    assert _rows(art, installs, symbol_path)[0].status == 'WARN'
    install_component_library(art, project, overwrite=True)
    assert not any(path.read_bytes().startswith(UTF8_BOM) for path in bommed)
    assert [r.status for r in _rows(art, installs, symbol_path)] == ['OK']


def test_symbol_files_are_not_named_when_the_installation_ships_no_symbol(tmp_path):
    """@brief Negative: the component has no usable symbol (and the library none of its name) ->
    an installation leaves the .kicad_sym and sym-lib-table alone, so their BOM is not named
    (a reinstall would not remove it); the fp-lib-table, which every installation rewrites, is."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    project = library.parent
    symbol_path.unlink()
    library.write_text('(kicad_symbol_lib (version 20241209) (generator "x"))\n', encoding='utf-8')
    _add_bom(library)
    _add_bom(project / 'sym-lib-table')
    assert [r.status for r in _rows(art, installs, symbol_path)] == ['OK']
    _add_bom(project / 'fp-lib-table')
    (row,) = _rows(art, installs, symbol_path)
    assert row.status == 'WARN' and 'fp-lib-table' in row.detail and 'sym-lib-table' not in row.detail


def test_the_global_table_is_looked_at_only_where_it_is_known(tmp_path):
    """@brief Positive: a global installation and the path of its global table -> a BOM there is
    named; negative twin: the location unknown (None) -> not guessed, the row stays OK."""
    art = _artifacts(tmp_path)
    (art.component_dir / 'Demo.kicad_sym').unlink(missing_ok=True)
    config = tmp_path / 'config'
    config.mkdir()
    table = config / 'fp-lib-table'
    table.write_text('(fp_lib_table\n  (version 7)\n)\n', encoding='utf-8')
    libraries = tmp_path / 'libraries'
    install_component_library(art, libraries, project_library=False, table_path=table)
    installs = [{'scope': 'global', 'pretty_dir': str(libraries / 'KiCadGen.pretty')}]
    text = art.footprint_path.read_text(encoding='utf-8')

    def rows(global_table):
        return check.library_checks('Demo', text, installs, art.step_path,
                                    global_table_path=global_table)
    assert [r.status for r in rows(table)] == ['OK']
    _add_bom(table)
    (row,) = rows(table)
    assert row.status == 'WARN' and 'fp-lib-table' in row.detail
    assert [r.status for r in rows(None)] == ['OK']
    entry = dict(installs[0], table_path=str(table))
    (row,) = check.library_checks('Demo', text, [entry], art.step_path)
    assert row.status == 'WARN' and 'fp-lib-table' in row.detail


# ── R9: an unreadable library with an unusable component symbol ─────────

def test_an_unreadable_library_is_said_even_when_the_component_symbol_is_unusable(tmp_path):
    """@brief Positive: the component file lacks its root (version) (the install skips it) AND the
    installed library is not UTF-8 -> WARN 2 «non leggibile», never a silent OK."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    text = symbol_path.read_text(encoding='utf-8')
    symbol_path.write_text(text.replace('(version 20251024)', '', 1), encoding='utf-8')
    library.write_bytes(library.read_text(encoding='utf-8').replace(
        '"GPIO1"', '"GPIO1 è"').encode('cp1252'))
    rows = _rows(art, installs, symbol_path)
    assert not any(r.status == 'OK' for r in rows), rows
    assert any(r.status == 'WARN' and 'UTF-8' in r.detail for r in rows)


def test_a_readable_library_with_an_unusable_component_symbol_is_stale(tmp_path):
    """@brief Negative twin: the same unusable component symbol, the library readable and still
    holding the old symbol -> «vecchio» with the re-export action (unchanged rule)."""
    art, symbol_path, installs, library, _result = _installed(tmp_path)
    text = symbol_path.read_text(encoding='utf-8')
    symbol_path.write_text(text.replace('(version 20251024)', '', 1), encoding='utf-8')
    (row,) = _rows(art, installs, symbol_path)
    assert row.status == 'WARN' and row.action == check.ACTION_REEXPORT_SYMBOL
    assert 'UTF-8' not in row.detail
