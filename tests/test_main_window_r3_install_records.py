"""@brief Round 3 of the 2026-11-15 review: the install report and records follow what the install declares.

Rules, each with a case that must trigger and one that must not:

- «Già installato e identico: niente da scrivere» (nothing to write) only
  when the installation declares it wrote no table row either
  (`LibraryInstallResult.table_changed` / `symbol_table_changed`); identical
  files with a row re-added say the row was restored, naming the table.
- A global installation records the ``fp-lib-table`` and ``sym-lib-table``
  it wrote, so the component check's BOM test reads those tables; a project
  installation records none (its tables sit beside its ``.pretty``), and a
  remote path is never recorded.

No kicad-cli and no real KiCad settings: the global tables are fakes under
the test's own folder.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

from tests.test_main_window_c5_review import NAME, _result  # noqa: E402
from tests.test_kicad_symbol_install_gui import (_answer, _global_tables, _project,  # noqa: E402
                                                 _symbol_file)
from tests.test_photo_inventory_gui import _write_complete_kicad_output  # noqa: E402


# ── item 3: «niente da scrivere» only when no table was written ─────────────

def test_identical_files_with_a_restored_row_never_say_nothing_was_written(window, tmp_path):
    """Positive: files identical, the footprint row re-added -> «ripristinata», the table named."""
    files = _result(tmp_path)
    restored = _result(tmp_path, unchanged=(files.footprint_path, files.step_path), symbol_identical=True,
                       table_changed=True)
    assert not window._install_wrote_nothing(restored)
    assert window._install_restored_tables(restored) == [files.table_path]
    summary = window._install_report_summary(restored, True)
    assert summary == window.INSTALL_TABLE_RESTORED_TEXT
    assert "niente da scrivere" not in summary
    message = window._install_result_message(restored)
    assert message.startswith(window.INSTALL_TABLE_RESTORED_TEXT)
    assert f"Riga della libreria ripristinata in {files.table_path}" in message
    assert str(files.symbol_table_path) not in message          # the symbol table was not written
    # The symbol table alone: named, the footprint table not.
    symbol_only = _result(tmp_path, unchanged=restored.unchanged, symbol_identical=True,
                          symbol_table_changed=True)
    assert window._install_restored_tables(symbol_only) == [files.symbol_table_path]
    assert f"ripristinata in {files.symbol_table_path}" in window._install_result_message(symbol_only)
    # Negative twin: both flags declared False -> «niente da scrivere».
    identical = _result(tmp_path, unchanged=restored.unchanged, symbol_identical=True)
    assert window._install_wrote_nothing(identical) and window._install_restored_tables(identical) == []
    # A written file is the usual «Installato …» report, never «ripristinata».
    written = _result(tmp_path, table_changed=True)
    assert window._install_restored_tables(written) == []
    assert window._install_report_summary(written, True).startswith("Installato")


def test_an_undeclared_table_flag_is_never_read_as_unchanged(window, tmp_path):
    """Negative: a result without the round-3 flags is not proven to have left its tables alone."""
    files = _result(tmp_path)
    undeclared = _result(tmp_path, unchanged=(files.footprint_path, files.step_path), symbol_identical=True)
    del undeclared.table_changed, undeclared.symbol_table_changed
    assert not window._install_wrote_nothing(undeclared)


def test_reinstall_after_removing_the_row_says_the_row_was_restored(window, tmp_path, monkeypatch):
    """End to end with the real installer: row removed by the user -> «ripristinata»; then identical."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    project = _project(tmp_path)
    _answer(monkeypatch, project, QMessageBox.StandardButton.Yes)
    window._install_project_library()
    assert window.label_install_report.text().startswith("Installato KiCadGen:")
    table = project / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    window._install_project_library()
    assert window.label_install_report.text() == window.INSTALL_TABLE_RESTORED_TEXT
    assert '(name "KiCadGen")' in table.read_text(encoding="utf-8")
    # Negative twin: the same reinstall again finds the row there -> nothing written.
    window._install_project_library()
    assert window.label_install_report.text() == window.INSTALL_IDENTICAL_TEXT


# ── item 4: the tables a global installation wrote are recorded ─────────────

def test_a_global_installation_records_the_tables_it_wrote(window, tmp_path, monkeypatch):
    """Positive: the fake global fp-lib-table and sym-lib-table are in the global record."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    table = _global_tables(tmp_path, symbols=True)
    _answer(monkeypatch, tmp_path / "libs", QMessageBox.StandardButton.Yes)
    window._install_kicad_library(tmp_path / "libs", project_library=False, table_path=table,
                                  table_version="10.0", artifacts=window._current_kicad_artifacts())
    entry = next(entry for entry in window._kicad_installs if entry["scope"] == "global")
    assert entry["table_path"] == str(table)
    assert entry["symbol_table_path"] == str(table.with_name("sym-lib-table"))
    resolved = next(entry for entry in window._resolved_kicad_installs() if entry["scope"] == "global")
    assert resolved["table_path"] == str(table)
    assert resolved["symbol_table_path"] == str(table.with_name("sym-lib-table"))
    # Saved and read back with the project.
    assert window._valid_kicad_installs(window._kicad_installs) == window._kicad_installs


def test_a_project_installation_records_no_table(window, tmp_path):
    """Negative: a project's tables sit beside its .pretty; nothing to record."""
    pretty = window._project_dir() / "lib" / "KiCadGen.pretty"
    window._record_kicad_install("project", pretty / f"{NAME}.kicad_mod",
                                 table_path=pretty.parent / "fp-lib-table",
                                 symbol_table_path=pretty.parent / "sym-lib-table")
    entry = next(entry for entry in window._kicad_installs if entry["scope"] == "project")
    assert "table_path" not in entry and "symbol_table_path" not in entry


def test_a_remote_table_path_is_never_recorded(window, tmp_path):
    """Negative: a UNC table is dropped alone, the installation's folder stays recorded."""
    window._record_kicad_install("global", tmp_path / "g" / "KiCadGen.pretty" / f"{NAME}.kicad_mod",
                                 table_path="\\\\server\\share\\fp-lib-table", symbol_table_path=None)
    entry = next(entry for entry in window._kicad_installs if entry["scope"] == "global")
    assert entry["pretty_dir"] == str(tmp_path / "g" / "KiCadGen.pretty")
    assert "table_path" not in entry and "symbol_table_path" not in entry
    saved = [{"scope": "global", "pretty_dir": str(tmp_path / "g"), "table_path": "//server/x",
              "symbol_table_path": str(tmp_path / "sym-lib-table")}]
    assert window._valid_kicad_installs(saved) == [
        {"scope": "global", "pretty_dir": str(tmp_path / "g"),
         "symbol_table_path": str(tmp_path / "sym-lib-table")}]
