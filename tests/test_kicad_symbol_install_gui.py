"""@brief GUI of the installation that brings the symbol (user decision 2026-11-15, «opzione 1»).

The window asks the symbol table's own conflict with the symbol wording,
never repoints it on the footprint row's answer, and reports the installed
symbol library, or why no symbol was installed.
"""
from kicadgen.gui import main_window as gui

from test_photo_inventory_gui import _write_complete_kicad_output


def _symbol_file(window):
    """Write the component's .kicad_sym as «Applica» does; return its path."""
    path = window._project_dir() / f"{window._component_name()}.kicad_sym"
    window._save_applied_electrical_symbol(path)
    window._refresh_output_actions()
    return path


def _project(tmp_path, *, symbol_row=None):
    """A KiCad project folder, optionally with a KiCadGen row in its sym-lib-table."""
    project = tmp_path / "DestinationProject"
    project.mkdir()
    (project / "DestinationProject.kicad_pro").write_text("{}", encoding="utf-8")
    if symbol_row:
        (project / "sym-lib-table").write_text(
            '(sym_lib_table\n  (version 7)\n'
            f'  (lib (name "KiCadGen")(type "KiCad")(uri "{symbol_row}")'
            '(options "")(descr "altra"))\n)\n', encoding="utf-8")
    return project


def _answer(monkeypatch, project, answer):
    """Fake the folder dialog and every question; collect questions and reports."""
    seen = {"asked": [], "reports": []}
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(project))
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda _p, _t, text, *a: seen["asked"].append(text) or answer)
    monkeypatch.setattr(gui.QMessageBox, "information",
                        lambda _p, _t, text, *a: seen["reports"].append(text))
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        lambda _p, _t, text, *a: seen["reports"].append(text))
    return seen


def test_install_reports_the_symbol_library(window, tmp_path, monkeypatch):  # noqa: F811
    """Positive: with a symbol file the project gets KiCadGen.kicad_sym and the report names it."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    project = _project(tmp_path)
    seen = _answer(monkeypatch, project, gui.QMessageBox.StandardButton.Yes)
    window._install_project_library()
    assert (project / "KiCadGen.kicad_sym").is_file()
    assert "KIPRJMOD" in (project / "sym-lib-table").read_text(encoding="utf-8")
    report = seen["reports"][-1]
    assert "KiCadGen.kicad_sym" in report
    # The symbol lines sit with the footprint's, before the advice paragraph.
    assert report.index("KiCadGen.kicad_sym") < report.index("\n\n")


def test_install_without_symbol_file_says_why(window, tmp_path, monkeypatch):  # noqa: F811
    """Negative twin: no symbol file -> footprint installed, report says how to add the symbol."""
    _write_complete_kicad_output(window)
    symbol = window._project_dir() / f"{window._component_name()}.kicad_sym"
    if symbol.exists():
        symbol.unlink()
    project = _project(tmp_path)
    seen = _answer(monkeypatch, project, gui.QMessageBox.StandardButton.Yes)
    window._install_project_library()
    assert not (project / "KiCadGen.kicad_sym").exists()
    assert seen["reports"] and "Simbolo non installato" in seen["reports"][-1]


def test_symbol_row_conflict_is_its_own_question(window, tmp_path, monkeypatch):  # noqa: F811
    """The symbol row elsewhere is asked with the SYMBOL wording and repointed after yes."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    project = _project(tmp_path, symbol_row="C:/Altro/KiCadGen.kicad_sym")
    seen = _answer(monkeypatch, project, gui.QMessageBox.StandardButton.Yes)
    window._install_project_library()
    conflict = [text for text in seen["asked"] if "C:/Altro/KiCadGen.kicad_sym" in text]
    assert len(conflict) == 1 and "simboli" in conflict[0] and "footprint" not in conflict[0]
    table = (project / "sym-lib-table").read_text(encoding="utf-8")
    assert "C:/Altro" not in table and "KIPRJMOD" in table


def test_answering_no_to_the_symbol_row_writes_nothing(window, tmp_path, monkeypatch):  # noqa: F811
    """Negative twin: «No» leaves both tables and the libraries untouched."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    project = _project(tmp_path, symbol_row="C:/Altro/KiCadGen.kicad_sym")
    before = (project / "sym-lib-table").read_text(encoding="utf-8")
    _answer(monkeypatch, project, gui.QMessageBox.StandardButton.No)
    window._install_project_library()
    assert (project / "sym-lib-table").read_text(encoding="utf-8") == before
    assert not (project / "KiCadGen.pretty").exists()
    assert not (project / "KiCadGen.kicad_sym").exists()
    assert not (project / "fp-lib-table").exists()


def _global_tables(tmp_path, *, symbols):
    """Fake KiCad's global settings folder (never the real one); return the fp-lib-table."""
    config = tmp_path / "kicad" / "10.0"
    config.mkdir(parents=True)
    (config / "fp-lib-table").write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    if symbols:
        (config / "sym-lib-table").write_text("(sym_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    return config / "fp-lib-table"


def test_global_install_names_the_global_symbol_table_before_writing(window, tmp_path, monkeypatch):  # noqa: F811
    """Positive (review 2026-11-15, M2): the global question names the sym-lib-table it changes."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    table = _global_tables(tmp_path, symbols=True)
    seen = _answer(monkeypatch, tmp_path / "libs", gui.QMessageBox.StandardButton.Yes)
    window._install_kicad_library(tmp_path / "libs", project_library=False, table_path=table,
                                  table_version="10.0", artifacts=window._current_kicad_artifacts())
    assert seen["asked"] and str(table.with_name("sym-lib-table")) in seen["asked"][0]
    assert "KiCadGen" in table.with_name("sym-lib-table").read_text(encoding="utf-8")


def test_global_install_without_symbol_table_is_refused_before_asking(window, tmp_path, monkeypatch):  # noqa: F811
    """Negative twin: no global sym-lib-table -> refused BEFORE the question, nothing written."""
    _write_complete_kicad_output(window)
    _symbol_file(window)
    table = _global_tables(tmp_path, symbols=False)
    seen = _answer(monkeypatch, tmp_path / "libs", gui.QMessageBox.StandardButton.Yes)
    window._install_kicad_library(tmp_path / "libs", project_library=False, table_path=table,
                                  table_version="10.0", artifacts=window._current_kicad_artifacts())
    assert seen["asked"] == []
    assert seen["reports"] and str(table.with_name("sym-lib-table")) in seen["reports"][-1]
    assert not (tmp_path / "libs").exists()
    assert table.read_text(encoding="utf-8") == "(fp_lib_table\n  (version 7)\n)\n"


def test_symbol_table_errors_use_the_symbol_wording(window):  # noqa: F811
    """A table error of kind «symbol» names the symbol table; the footprint one does not."""
    from kicadgen.services.kicad_library import KiCadLibraryError
    symbol = KiCadLibraryError("x", code="global_table_missing", path="T", kind="symbol")
    footprint = KiCadLibraryError("x", code="global_table_missing", path="T", kind="footprint")
    assert "simboli" in window._kicad_error_text(symbol)
    assert "footprint" in window._kicad_error_text(footprint)
