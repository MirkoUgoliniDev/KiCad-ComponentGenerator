"""@brief Round 3 of the 2026-11-15 review: an installation declares whether it wrote a table row.

`LibraryInstallResult.table_changed` / `symbol_table_changed` say that the
install added or rewrote the library's row in ``fp-lib-table`` /
``sym-lib-table``, even when every component file was already identical
(the window then must not say «niente da scrivere», "nothing to write").
Each rule has a twin it must tell apart for a structural reason:

- identical reinstall -> no table written; the footprint row removed by the
  user -> the footprint table written, the symbol table not;
- the symbol row removed -> the symbol table written, the footprint one not;
- a row pointing elsewhere, repointed on confirmation -> written;
- a BOM dropped alone is NOT a row change (it is in `bom_removed`).
"""

from kicadgen.services.kicad_library import install_component_library
from tests.test_kicad_library_c2_review_1115 import BOM, _artifacts, _component

EMPTY_FP_TABLE = "(fp_lib_table\n  (version 7)\n)\n"
EMPTY_SYM_TABLE = "(sym_lib_table\n  (version 7)\n)\n"


def _installed(tmp_path):
    """A project with the component installed once; returns (project, component, first result)."""
    project = tmp_path / "project"
    project.mkdir()
    component = _component(tmp_path)
    first = install_component_library(_artifacts(component), project)
    assert first.table_changed is True and first.symbol_table_changed is True   # first install: rows added
    return project, component, first


def test_identical_reinstall_declares_no_table_change(tmp_path):
    """Negative: same files and both rows in place -> neither table written."""
    project, component, first = _installed(tmp_path)
    again = install_component_library(_artifacts(component), project)
    assert set(again.unchanged) == {first.footprint_path, first.step_path}
    assert again.symbol_identical is True
    assert again.table_changed is False and again.symbol_table_changed is False


def test_a_removed_footprint_row_is_declared_restored(tmp_path):
    """Positive: the user removed the footprint row; the reinstall re-adds it and says so."""
    project, component, first = _installed(tmp_path)
    (project / "fp-lib-table").write_text(EMPTY_FP_TABLE, encoding="utf-8")
    again = install_component_library(_artifacts(component), project)
    assert set(again.unchanged) == {first.footprint_path, first.step_path}   # files identical
    assert again.table_changed is True and again.symbol_table_changed is False
    assert '(name "KiCadGen")' in (project / "fp-lib-table").read_text(encoding="utf-8")


def test_a_removed_symbol_row_is_declared_restored(tmp_path):
    """Positive twin on the other table: only the symbol table is written."""
    project, component, _first = _installed(tmp_path)
    (project / "sym-lib-table").write_text(EMPTY_SYM_TABLE, encoding="utf-8")
    again = install_component_library(_artifacts(component), project)
    assert again.symbol_identical is True
    assert again.symbol_table_changed is True and again.table_changed is False


def test_a_row_pointing_elsewhere_repointed_is_declared(tmp_path):
    """Positive: a KiCadGen row to another folder, repointed on confirmation -> written."""
    project, component, _first = _installed(tmp_path)
    table = project / "fp-lib-table"
    text = table.read_text(encoding="utf-8")
    table.write_text(text.replace("${KIPRJMOD}/KiCadGen.pretty", "C:/elsewhere/KiCadGen.pretty"),
                     encoding="utf-8")
    assert "C:/elsewhere" in table.read_text(encoding="utf-8")
    again = install_component_library(_artifacts(component), project, replace_library_entry=True)
    assert again.table_changed is True
    assert "C:/elsewhere" not in table.read_text(encoding="utf-8")


def test_a_bom_dropped_alone_is_not_a_row_change(tmp_path):
    """Negative: a BOM in front of a table that already holds the row -> `bom_removed`, no row change."""
    project, component, _first = _installed(tmp_path)
    table = project / "fp-lib-table"
    table.write_bytes(BOM + table.read_bytes())
    again = install_component_library(_artifacts(component), project)
    assert table in again.bom_removed
    assert again.table_changed is False
    assert not table.read_bytes().startswith(BOM)
