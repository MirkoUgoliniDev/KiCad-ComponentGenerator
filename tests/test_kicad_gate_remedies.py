"""@brief Every block of the KiCad gate names a remedy that is enabled and clears it (review pass 3, 2026-10-15).

PRINCIPLE enforced here: a block message must name an action that clears it,
and that action must be enabled in that state. The table below drives, for
EVERY code `MainWindow._kicad_block` can produce, a setup that produces the
code, the remedy the message names (checked enabled), and asserts the gate
clears afterwards. A code without a row fails the coverage test.

H1: a (re)generation with fewer pins left symbol rows of vanished pads with
    no way to remove them; the pinout is now reconciled with the written
    pads, and the editor has «Rimuovi i pin assenti dal footprint».
H2: a STEP built with a body, then no body buildable: «Rigenera» was the
    remedy and was disabled; now «Rimuovi il modello 3D obsoleto».
M1: the symbol gate compared pin numbers only; now the applied symbol text.
"""
import re

import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import main_window as gui
from tests.test_generated_files_alignment import FakeCad3D, _enable_3d, dialogs  # noqa: F401
from tests.test_photo_inventory_gui import fill_required_metadata
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction

NAME = "HDR"
KICAD_COMMANDS = gui.MainWindow.KICAD_COMMAND_ATTRS


def _pinout(n):
    return [{"pin": str(i), "name": f"P{i}"} for i in range(1, n + 1)]


def _write_step(folder, name=NAME):
    step = folder / f"{name}.3dshapes" / f"{name}.step"
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")


def _symbol_numbers(folder, name=NAME):
    return re.findall(r'\(number "([^"]*)"', (folder / f"{name}.kicad_sym").read_text(encoding="utf-8"))


def _ready(window, monkeypatch, tmp_path, cols=4, with_body=True):
    """A generated, aligned, installable project (gate clear)."""
    prepare(window, monkeypatch, tmp_path, NAME)
    monkeypatch.setattr(gui, "Cad3DWorker", FakeCad3D)
    monkeypatch.setattr(window, "_show_generated_mesh", lambda *_: None)
    FakeCad3D.created = []
    body = {} if with_body else dict(body_length_mm=None, body_width_mm=None, body_height_mm=None)
    run_extraction(window, header_extraction(cols=cols, pinout=_pinout(2 * cols), **body))
    fill_required_metadata(window)
    window._apply_metadata_to_files()
    folder = window._project_dir()
    if with_body:
        window.group_3d.setChecked(True)
        window._regenerate_3d()                      # a STEP with its records
    else:
        _write_step(folder)
    window._refresh_output_actions()
    assert window._kicad_block_code() == "", window._kicad_command_block_reason()
    return folder


# ── setups producing each code, and the remedy each message names ──────────

def _setup_busy(window, monkeypatch, folder):
    monkeypatch.setattr(window, "_terminal_worker", type("W", (), {"isRunning": lambda self: True})())


def _remedy_busy(window, monkeypatch, folder):
    monkeypatch.setattr(window, "_terminal_worker", None)      # the work ends


def _setup_footprint_invalid(window, monkeypatch, folder):
    monkeypatch.setattr(window, "_current_params", lambda: (_ for _ in ()).throw(ValueError("campo")))


def _remedy_footprint_invalid(window, monkeypatch, folder):
    monkeypatch.undo()                                          # the user corrects the field
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)


def _setup_footprint_pads(window, monkeypatch, folder):
    window.field_cols.setValue(2)


def _setup_footprint_outline(window, monkeypatch, folder):
    window.field_body_length.setValue(window.field_body_length.value() + 3.0)


def _remedy_apply_files(window, monkeypatch, folder):
    window.btn_apply_metadata.click()
    if window._kicad_block_code().startswith("step_"):
        window.btn_regen_3d.click()                             # the body changed too


def _setup_symbol_pads(window, monkeypatch, folder):
    symbol = folder / f"{NAME}.kicad_sym"
    symbol.write_text(symbol.read_text(encoding="utf-8").replace('(number "8"', '(number "9"'),
                      encoding="utf-8")


def _setup_symbol_text(window, monkeypatch, folder):
    symbol = folder / f"{NAME}.kicad_sym"
    symbol.write_text(symbol.read_text(encoding="utf-8").replace('(name "P1"', '(name "OTHER"'),
                      encoding="utf-8")


def _remedy_update_symbol(window, monkeypatch, folder):
    window.btn_update_symbol_file.click()


def _setup_step_pads(window, monkeypatch, folder):
    window._step_source_pads = window._step_source_pads[:-1]    # a STEP built over other pads


def _setup_step_body(window, monkeypatch, folder):
    window.field_body_height.setValue(window.field_body_height.value() + 1.0)


def _remedy_regen_3d(window, monkeypatch, folder):
    window.btn_regen_3d.click()


def _setup_step_unbuildable(window, monkeypatch, folder):
    run_extraction(window, header_extraction(cols=4, pinout=_pinout(8), body_length_mm=None,
                                             body_width_mm=None, body_height_mm=None))
    fill_required_metadata(window)


def _remedy_remove_model(window, monkeypatch, folder):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.btn_remove_obsolete_model.click()


#: code -> (setup, remedy): the remedy is what the block message names.
CASES = {
    "busy": (_setup_busy, _remedy_busy),
    "footprint_invalid": (_setup_footprint_invalid, _remedy_footprint_invalid),
    "footprint_pads": (_setup_footprint_pads, _remedy_apply_files),
    "footprint_outline": (_setup_footprint_outline, _remedy_apply_files),
    "symbol_pads": (_setup_symbol_pads, _remedy_update_symbol),
    "symbol_text": (_setup_symbol_text, _remedy_update_symbol),
    "step_pads": (_setup_step_pads, _remedy_regen_3d),
    "step_body": (_setup_step_body, _remedy_regen_3d),
    "step_unbuildable": (_setup_step_unbuildable, _remedy_remove_model),
}


def test_every_gate_code_has_a_remedy_row():
    assert set(CASES) == set(gui.MainWindow.KICAD_BLOCK_REMEDIES)


@pytest.mark.parametrize("code", sorted(CASES))
def test_the_named_remedy_is_enabled_and_clears_the_block(window, monkeypatch, tmp_path, dialogs, code):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path)
    setup, remedy = CASES[code]
    setup(window, monkeypatch, folder)
    window._refresh_output_actions()
    assert window._kicad_block_code() == code, window._kicad_command_block_reason()
    assert not any(getattr(window, attr).isEnabled() for attr in KICAD_COMMANDS)
    widget = gui.MainWindow.KICAD_BLOCK_REMEDIES[code]
    if widget is not None:
        assert getattr(window, widget).isEnabled(), getattr(window, widget).toolTip()
    remedy(window, monkeypatch, folder)
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == "" and not window._kicad_work_busy(), \
        window._kicad_command_block_reason()


# ── H1 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("via", ["applica_ai_file", "generate"])
def test_fewer_pins_reconcile_the_pinout_and_the_symbol(window, monkeypatch, tmp_path, dialogs, via):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path, with_body=False)
    window.field_cols.setValue(2)
    if via == "applica_ai_file":
        window._apply_metadata_to_files()
    else:
        assert window._generate()
    window._refresh_output_actions()
    assert [str(r["pin"]) for r in window.pinout] == ["1", "2", "3", "4"]
    assert {r["pin"]: r["name"] for r in window.pinout}["1"] == "P1"   # names of survivors kept
    assert sorted(_symbol_numbers(folder)) == ["1", "2", "3", "4"]
    assert window._kicad_block_code() == ""
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    assert not window.symbol_tab.absent_pins() and window.symbol_tab.status.text() != ""


def test_a_pinout_with_absent_pins_can_drop_them_in_the_editor_and_apply(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """The editor itself: an applied pinout holding rows of pads the
    footprint does not have (a project saved by an older version, a
    hand-edited file) leaves the state with «Rimuovi i pin assenti dal
    footprint» then «Applica»; before, «Applica» stayed disabled for good."""
    folder = _ready(window, monkeypatch, tmp_path, cols=2, with_body=False)
    window.pinout = _pinout(8)                                 # four pins the footprint lacks
    window.pinout[0]["name"] = "VCC"
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    window._refresh_symbol_tab(force=True)
    tab = window.symbol_tab
    assert sorted(tab.absent_pins()) == ["5", "6", "7", "8"]
    assert tab.absent_pins_button.defaultAction().isEnabled()
    assert not tab.apply_button.defaultAction().isEnabled()    # negative: not before the removal
    tab.remove_absent_pins()
    assert tab.absent_pins() == [] and tab.apply_button.defaultAction().isEnabled()
    assert tab.undo_button.defaultAction().isEnabled()
    tab.apply()
    assert [str(r["pin"]) for r in window.pinout] == ["1", "2", "3", "4"]
    assert {r["pin"]: r["name"] for r in window.pinout}["1"] == "VCC"
    assert sorted(_symbol_numbers(folder)) == ["1", "2", "3", "4"]
    window._refresh_output_actions()
    assert window._kicad_block_code() == ""


def test_the_overwrite_question_lists_the_symbol_only_when_it_is_rewritten(window, monkeypatch, tmp_path):  # noqa: F811
    asked = prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction(cols=4))
    run_extraction(window, header_extraction(cols=4))
    assert any(f"{NAME}.kicad_sym" in text for text in asked)       # positive
    asked.clear()
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    window.symbol_tab.table.item(0, 1).setText("DRAFT")              # unapplied: not rewritten
    window.tabs.setCurrentIndex(0)
    run_extraction(window, header_extraction(cols=4))
    assert asked and not any(f"{NAME}.kicad_sym" in text for text in asked)


# ── H2 ─────────────────────────────────────────────────────────────────────

def test_a_step_that_cannot_be_rebuilt_names_enabled_ways_out(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path)
    _setup_step_unbuildable(window, monkeypatch, folder)
    window._refresh_output_actions()
    reason = window._kicad_command_block_reason()
    assert "non ricostruibile" in reason and "Rimuovi il modello 3D obsoleto" in reason
    assert not window.btn_regen_3d.isEnabled()                 # the old, disabled remedy
    assert window.btn_remove_obsolete_model.isEnabled()
    assert window.btn_extract_ai.isEnabled()                   # the other way out
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    window.btn_remove_obsolete_model.click()
    assert (folder / f"{NAME}.3dshapes" / f"{NAME}.step").exists()   # «No» keeps it
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.btn_remove_obsolete_model.click()
    assert not (folder / f"{NAME}.3dshapes" / f"{NAME}.step").exists()
    assert window._step_source_pads is None and window._step_source_body is None
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    assert "Genera prima" in window.btn_export.toolTip()


def test_the_3d_group_off_after_a_step_is_also_unbuildable(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """Each step names an enabled remedy: the group off removes the body
    outline (footprint remedy «Applica ai file»), then the STEP cannot be
    rebuilt («Rimuovi il modello 3D obsoleto»)."""
    _ready(window, monkeypatch, tmp_path)
    window.group_3d.setChecked(False)
    window._refresh_output_actions()
    assert window._kicad_block_code() == "footprint_outline"
    window.btn_apply_metadata.click()
    window._refresh_output_actions()
    assert window._kicad_block_code() == "step_unbuildable"
    assert "spento" in window._kicad_command_block_reason()
    assert window.btn_remove_obsolete_model.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.btn_remove_obsolete_model.click()
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""


# ── M1 ─────────────────────────────────────────────────────────────────────

def test_replaced_names_without_overwrite_are_caught_by_the_symbol_gate(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    folder = _ready(window, monkeypatch, tmp_path, cols=1, with_body=False)
    monkeypatch.setattr(gui.MainWindow, "_ask_user_pin_names",
                        lambda self, *a, **k: self.USER_PIN_NAMES_REPLACE)
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    window.symbol_tab.table.item(0, 1).setText("VCC")
    window.symbol_tab.apply()
    window.tabs.setCurrentIndex(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    run_extraction(window, header_extraction(cols=1, pinout=[{"pin": "1", "name": "GND"},
                                                             {"pin": "2", "name": "P2"}],
                                             body_length_mm=None, body_width_mm=None,
                                             body_height_mm=None))
    fill_required_metadata(window)
    window._refresh_output_actions()
    assert '(name "VCC"' in (folder / f"{NAME}.kicad_sym").read_text(encoding="utf-8")
    assert window._kicad_block_code() == "symbol_text"
    assert window.btn_update_symbol_file.isEnabled()
    window.btn_update_symbol_file.click()
    assert '(name "GND"' in (folder / f"{NAME}.kicad_sym").read_text(encoding="utf-8")
    assert window._kicad_block_code() == ""


def test_metadata_only_differences_do_not_block_the_preview(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """The symbol's metadata properties belong to the metadata gate (install
    only): a description keystroke leaves the preview enabled."""
    _ready(window, monkeypatch, tmp_path, with_body=False)
    window.field_description.setText("another description")
    window._refresh_output_actions()
    assert window._kicad_block_code() == ""
    assert window.btn_test_kicad.isEnabled() and not window.btn_export.isEnabled()
