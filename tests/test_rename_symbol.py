"""@brief «Rinomina progetto» carries the electrical symbol and its editor along (audit 2026-10-15, item 9).

Before: with the Symbol tab showing, the editor stayed bound to OLD (footprint
link «KiCadGen:OLD», recovery file under OLD\\, which the next edit
recreated), `OLD.kicad_sym` was left behind with «Footprint KiCadGen:OLD»,
and the isolated-preview files kept the old name. Now the rename moves and
rewrites the .kicad_sym (rolled back with the rest on failure), rebinds the
editor keeping a pending draft, and removes the stale preview files.
"""
import re

from PyQt6.QtWidgets import QDialog

from kicadgen.gui import main_window as gui
from kicadgen.gui import project_dialogs
from kicadgen.services.kicad_library import PREVIEW_DIR_NAME, kicad_lock_file
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction


def _project(window, monkeypatch, tmp_path):
    prepare(window, monkeypatch, tmp_path, "OLD")
    run_extraction(window, header_extraction(name="OLD"))
    folder = window._project_dir()
    assert (folder / "OLD.kicad_sym").is_file()
    return folder


def _rename(window, monkeypatch, new_name="NEW"):
    monkeypatch.setattr(project_dialogs.ProjectNameDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(project_dialogs.ProjectNameDialog, "project_name", lambda self: new_name)
    window.action_rename_project.trigger()


def _symbol_identity(path):
    text = path.read_text(encoding="utf-8")
    return (re.findall(r'\(symbol "([^"]*)"', text),
            re.findall(r'\(property "Footprint" "([^"]*)"', text),
            re.findall(r'\(property "Value" "([^"]*)"', text))


def test_the_symbol_file_follows_the_name(window, monkeypatch, tmp_path):  # noqa: F811
    _project(window, monkeypatch, tmp_path)
    _rename(window, monkeypatch)
    folder = tmp_path / "NEW"
    assert not (folder / "OLD.kicad_sym").exists()
    symbols, footprint, value = _symbol_identity(folder / "NEW.kicad_sym")
    assert symbols == ["NEW", "NEW_0_1", "NEW_1_1"]
    assert footprint == ["KiCadGen:NEW"] and value == ["NEW"]


def test_the_editor_is_rebound_and_keeps_its_draft(window, monkeypatch, tmp_path):  # noqa: F811
    """With the Symbol tab showing and a pending edit: after the rename the
    editor points at NEW, the draft is still a draft, and applying it writes
    NEW.kicad_sym without recreating the OLD folder."""
    _project(window, monkeypatch, tmp_path)
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    tab = window.symbol_tab
    tab.table.item(0, 1).setText("VCC")
    assert tab.has_pending_edits()
    _rename(window, monkeypatch)
    assert tab.footprint() == "KiCadGen:NEW"
    assert str(tab._recovery_path).startswith(str(tmp_path / "NEW"))
    assert tab.has_pending_edits() and tab.draft_rows()[0]["name"] == "VCC"
    # Still a draft, not merely an offer: its undo history is there.
    assert tab.undo_button.defaultAction().isEnabled()
    tab.apply_button.click()
    assert not (tmp_path / "OLD").exists()
    text = (tmp_path / "NEW" / "NEW.kicad_sym").read_text(encoding="utf-8")
    assert '(name "VCC"' in text and '"KiCadGen:NEW"' in text


def test_a_failed_rename_restores_the_symbol(window, monkeypatch, tmp_path):  # noqa: F811
    """A later step fails (a model already named NEW): everything is rolled
    back, the symbol file with its original bytes and name included."""
    folder = _project(window, monkeypatch, tmp_path)
    original = (folder / "OLD.kicad_sym").read_bytes()
    shapes = folder / "OLD.3dshapes"
    shapes.mkdir(exist_ok=True)
    (shapes / "OLD.step").write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    (shapes / "NEW.step").write_bytes(b"conflict")
    _rename(window, monkeypatch)
    assert window._component_name() == "OLD"
    assert (folder / "OLD.kicad_sym").read_bytes() == original
    assert not (folder / "NEW.kicad_sym").exists() and not (tmp_path / "NEW").exists()


def test_stale_preview_files_are_removed_unless_kicad_holds_them(window, monkeypatch, tmp_path):  # noqa: F811
    folder = _project(window, monkeypatch, tmp_path)
    preview = folder / PREVIEW_DIR_NAME
    preview.mkdir()
    names = ("OLD-preview.kicad_pcb", "OLD-preview.kicad_pro", "OLD.step")
    for name in names:
        (preview / name).write_text("x", encoding="utf-8")
    (preview / "notes.txt").write_text("user", encoding="utf-8")
    _rename(window, monkeypatch)
    moved = tmp_path / "NEW" / PREVIEW_DIR_NAME
    assert not any((moved / name).exists() for name in names)
    assert (moved / "notes.txt").exists(), "only the program's own preview files go"


def test_a_preview_open_in_kicad_is_left_alone(window, monkeypatch, tmp_path):  # noqa: F811
    folder = _project(window, monkeypatch, tmp_path)
    preview = folder / PREVIEW_DIR_NAME
    preview.mkdir()
    board = preview / "OLD-preview.kicad_pcb"
    board.write_text("x", encoding="utf-8")
    kicad_lock_file(board).write_text("lock", encoding="utf-8")
    _rename(window, monkeypatch)
    assert (tmp_path / "NEW" / PREVIEW_DIR_NAME / "OLD-preview.kicad_pcb").exists()


def test_renamed_symbol_text_touches_only_the_identity():
    from kicadgen.generators.symbol import serialize_symbol, symbol_inputs
    pins, body = symbol_inputs([{"pin": "1", "name": "OLD"}, {"pin": "2", "name": "OLD_1_1"}])
    text = serialize_symbol("OLD", pins, footprint="Lib:OLD", body=body)
    renamed = gui.renamed_symbol_text(text, "OLD", "NEW")
    assert '(name "OLD"' in renamed and '(name "OLD_1_1"' in renamed
    assert '"Lib:NEW"' in renamed and '(symbol "NEW_1_1"' in renamed
    other = serialize_symbol("OLDER", pins, footprint="Lib:OLDER", body=body)
    assert gui.renamed_symbol_text(other, "OLD", "NEW") == other
