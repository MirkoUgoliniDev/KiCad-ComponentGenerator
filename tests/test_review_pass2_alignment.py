"""@brief Adversarial review, pass 2, of the file-alignment and pin-name work (2026-10-15).

M1: a «Mantieni» on a smaller re-analysis kept the name of a pin the new
    footprint no longer has (8-pin symbol beside a 4-pad footprint, «pronti»),
    and asked about it at every later analysis; a symbol whose pins differ
    from the footprint on disk passed the gate.
M2: «Mantieni» copied the old layout of some pins only, two pins ended on
    the same spot and the symbol was not rewritten.
M3: every photographed board was exempt from the documented-body rule, even
    without a known board size.
L1: body-only changes (swap, height, colour) followed by «No» left «pronti»
    with the old F.Fab and STEP.
L2: the gate cost ~70 ms per metadata keystroke on a 900-pad package.
L3: the read-back cache could survive our own write on a coarse clock.
L4: the stale-footprint message blamed the parameters only.
L5: autosave could run inside the questions asked while applying an
    analysis; a resolved «DUBBIO» note stayed in the notes.
L6: the KiCad commands did not follow the review dialogs.
"""
import re
import time

from PyQt6.QtWidgets import QDialog

from kicadgen.gui import main_window as gui
from tests.test_photo_inventory_gui import fill_required_metadata
from tests.ui_flow_helpers import (header_extraction, package_extraction, prepare,
                                   run_extraction, soic_geometry)


def _type(window, names: dict):
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    table = window.symbol_tab.table
    for row, name in names.items():
        table.item(row, 1).setText(name)
    window.symbol_tab.apply_button.click()
    window.tabs.setCurrentIndex(0)


def _symbol_numbers(folder, name="HDR"):
    return re.findall(r'\(number "([^"]*)"', (folder / f"{name}.kicad_sym").read_text(encoding="utf-8"))


def _write_step(folder, name="HDR"):
    step = folder / f"{name}.3dshapes" / f"{name}.step"
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")


# ── M1 ─────────────────────────────────────────────────────────────────────

def test_keep_drops_names_of_pins_the_new_footprint_lacks(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    _type(window, {0: "VCC", 7: "LAST"})            # pins 1 and 8
    seen = []

    def ask(self, conflicts, proposed, vanished=()):
        seen.append((dict(conflicts), list(vanished)))
        return gui.MainWindow.USER_PIN_NAMES_KEEP
    monkeypatch.setattr(gui.MainWindow, "_ask_user_pin_names", ask)
    run_extraction(window, header_extraction(cols=2))  # 2 x 2: pin 8 is gone
    assert seen == [({"1": "VCC"}, ["8"])]
    assert {str(r["pin"]) for r in window.pinout} <= {"1", "2", "3", "4"}
    assert window._user_pin_names == {"1": "VCC"}
    folder = window._project_dir()
    assert sorted(_symbol_numbers(folder)) == ["1", "2", "3", "4"]
    fill_required_metadata(window)
    _write_step(folder)
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    # Nothing to ask the next time: the vanished name is forgotten.
    seen.clear()
    run_extraction(window, header_extraction(cols=2, pinout=[{"pin": "1", "name": "VCC"}]))
    assert seen == []


def test_only_vanished_names_are_dropped_with_a_log_line_and_no_question(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    _type(window, {7: "LAST"})
    asked = []
    monkeypatch.setattr(gui.MainWindow, "_ask_user_pin_names",
                        lambda self, *a, **k: asked.append(a) or self.USER_PIN_NAMES_KEEP)
    run_extraction(window, header_extraction(cols=2))
    assert asked == [] and window._user_pin_names == {}
    assert "tolti: 8" in window.ai_progress_text.toPlainText()


def test_a_symbol_file_that_disagrees_with_the_footprint_is_not_ready(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative: a .kicad_sym with other pin numbers than the footprint on
    disk blocks with its own reason; positive: the matching one does not."""
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    folder = window._project_dir()
    fill_required_metadata(window)
    _write_step(folder)
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    symbol = folder / "HDR.kicad_sym"
    symbol.write_text(symbol.read_text(encoding="utf-8").replace('(number "8"', '(number "9"'),
                      encoding="utf-8")
    window._refresh_output_actions()
    assert "Simbolo non aggiornato" in window.label_kicad_status.text()
    assert not window.btn_export.isEnabled()


# ── M2 ─────────────────────────────────────────────────────────────────────

def _move_pin(window, pin, x, y):
    tab = window.symbol_tab
    row = next(r for r in tab.draft_rows() if str(r["pin"]) == pin)
    assert tab.set_pin_geometry(pin, x, y, row.get("symbol_angle") or 0)


def test_keep_keeps_a_valid_custom_layout_for_the_same_pins(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    _move_pin(window, "2", 20.32, 20.32)
    _type(window, {0: "VCC"})
    moved = next(r for r in window.pinout if str(r["pin"]) == "2")["symbol_position"]
    run_extraction(window, header_extraction(cols=4, pinout=[{"pin": "1", "name": "IN"},
                                                             {"pin": "2", "name": "GND"}]))
    rows = {str(r["pin"]): r for r in window.pinout}
    assert rows["1"]["name"] == "VCC" and rows["2"]["name"] == "GND"
    assert rows["2"]["symbol_position"] == moved
    assert sorted(_symbol_numbers(window._project_dir())) == sorted(str(i) for i in range(1, 9))


def test_keep_falls_back_to_the_automatic_layout_when_the_pins_change(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative of the old mix: no row keeps a position, the symbol is
    rewritten (no «stessa posizione» error) and the log says why."""
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    _move_pin(window, "2", 20.32, 20.32)
    _type(window, {0: "VCC"})
    run_extraction(window, header_extraction(cols=3))
    assert not any("symbol_position" in r for r in window.pinout)
    assert {str(r["pin"]): r["name"] for r in window.pinout}["1"] == "VCC"
    assert sorted(_symbol_numbers(window._project_dir())) == sorted(str(i) for i in range(1, 7))
    assert "torna quella automatica" in window.ai_progress_text.toPlainText()


# ── M3 ─────────────────────────────────────────────────────────────────────

def test_a_board_needs_a_known_board_size_for_its_body(window):  # noqa: F811
    monkeypatch_family = window.combo_component_type.findData("aggregate_tht")
    window.combo_component_type.setCurrentIndex(monkeypatch_family)
    window.group_3d.setChecked(True)
    window._board_size_known = False
    assert window._current_body_params() is None
    window._board_size_known = True
    assert window._current_body_params() is not None


# ── L1 ─────────────────────────────────────────────────────────────────────

def test_a_body_swap_without_rewrite_is_not_ready(window, monkeypatch, tmp_path):  # noqa: F811
    """Same pads, other body outline (F.Fab, courtyard): blocked; the
    rewrite realigns."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    run_extraction(window, package_extraction(soic_geometry(3.9, 4.9)))
    folder = window._project_dir()
    fill_required_metadata(window)
    _write_step(folder, "SOIC")
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    package = window.package_geometry
    window.package_geometry = gui.replace(package, body_length_mm=3.0)
    window._refresh_output_actions()
    assert "contorno del corpo" in window.label_kicad_status.text()
    assert window._generate(footprint_only=True)
    window._refresh_output_actions()
    assert window._footprint_alignment_reason() == ""


def test_a_body_change_after_the_step_is_not_ready(window, monkeypatch, tmp_path):  # noqa: F811
    from tests.test_generated_files_alignment import FakeCad3D, _enable_3d, _project
    _project(window, monkeypatch, tmp_path)
    _enable_3d(window)
    window._regenerate_3d()
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    window.field_body_height.setValue(4.0)          # STEP-only quantity
    window._refresh_output_actions()
    assert "corpo, colori o annotazioni" in window.btn_export.toolTip()
    window._regenerate_3d()
    window._refresh_output_actions()
    assert window._kicad_files_alignment_reason() == ""
    window.field_body_color.setText("#123456")
    assert "corpo, colori o annotazioni" in window._kicad_files_alignment_reason()
    assert window._project_state()["step_source_body"]["body"]["height_mm"] == 4.0


# ── L2 / L3 ────────────────────────────────────────────────────────────────

def test_a_metadata_keystroke_is_cheap_on_a_big_package(window, monkeypatch, tmp_path):  # noqa: F811
    """No footprint is rebuilt and no file reparsed for a keystroke; generous
    bound on the time (the target is a few ms)."""
    pins = [{"number": str(i + 1), "name": "", "x_mm": (i % 30) * 0.8, "y_mm": (i // 30) * 0.8,
             "pad_length_mm": 0.4, "pad_width_mm": 0.4, "shape": "circle", "pad_type": "smd"}
            for i in range(900)]
    geometry = {"name": "BGA", "family": "custom", "body_length_mm": 25, "body_width_mm": 25,
                "body_height_mm": 1.2, "origin": "body_center", "source_view": "top",
                "orientation_deg": 0, "mirrored": False, "confidence": "high",
                "needs_review": False, "pins": pins}
    prepare(window, monkeypatch, tmp_path, "BGA")
    run_extraction(window, package_extraction(geometry, name="BGA"))
    fill_required_metadata(window)
    _write_step(window._project_dir(), "BGA")
    window._refresh_output_actions()
    builds, parses = [], []
    real_build = gui.MainWindow._build_footprint
    monkeypatch.setattr(gui.MainWindow, "_build_footprint",
                        staticmethod(lambda p: builds.append(1) or real_build(p)))
    real_read = gui.read_footprint_signature
    monkeypatch.setattr(gui, "read_footprint_signature", lambda p: parses.append(1) or real_read(p))
    start = time.perf_counter()
    for index in range(10):
        window.field_description.setText(f"descr {index}")
    elapsed = (time.perf_counter() - start) / 10
    assert builds == [] and parses == []
    assert elapsed < 0.05, elapsed


def test_our_own_write_drops_the_read_back_cache(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    assert window._written_footprint_pads() is not None
    reads = []
    real_read = gui.read_footprint_signature
    monkeypatch.setattr(gui, "read_footprint_signature", lambda p: reads.append(1) or real_read(p))
    window._written_footprint_pads()
    assert reads == []                               # cached
    window._on_generated(str(window._footprint_file()))
    window._written_footprint_pads()
    assert reads == [1]                              # re-read after our write


# ── L4 ─────────────────────────────────────────────────────────────────────

def test_the_stale_message_names_an_older_version_too(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction(cols=4))
    window.field_cols.setValue(6)
    reason = window._footprint_alignment_reason()
    assert "altri parametri" in reason and "versione precedente" in reason


# ── L5 ─────────────────────────────────────────────────────────────────────

def test_no_autosave_while_a_question_is_open(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    states = []

    def ask(self, doubt):
        states.append(self._project_autosave.isActive())
        return gui.MainWindow.GEOMETRY_DOUBT_SWAP
    monkeypatch.setattr(gui.MainWindow, "_ask_geometry_doubt", ask)
    assert window._project_autosave.isActive()
    run_extraction(window, package_extraction(soic_geometry(4.9, 3.9)))
    assert states == [False]
    assert window._project_autosave.isActive()
    # The swapped doubt's «DUBBIO» note is gone from the applied notes.
    assert "DUBBIO corpo/pad" not in (window._last_extracted.notes or "")


def test_an_unresolved_doubt_keeps_its_note(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    monkeypatch.setattr(gui.MainWindow, "_ask_geometry_doubt",
                        lambda self, doubt: gui.MainWindow.GEOMETRY_DOUBT_KEEP)
    params = package_extraction(soic_geometry(4.9, 3.9))
    params.notes = "\n".join(d["message"] for d in params.geometry_doubts)
    run_extraction(window, params)
    assert "DUBBIO corpo/pad" in window._last_extracted.notes


# ── L6 ─────────────────────────────────────────────────────────────────────

def test_the_review_dialog_switches_the_commands_off_and_back_on(window, monkeypatch, tmp_path):  # noqa: F811
    from tests.test_generated_files_alignment import _project
    from kicadgen.gui import component_check_dialog
    _project(window, monkeypatch, tmp_path)
    assert window.action_export.isEnabled()
    during = []

    class FakeCheck(QDialog):
        """Stands for ComponentCheckDialog, which starts its own checking thread."""

        def __init__(self, args, parent=None, tx=None):
            super().__init__(parent)

        def exec(self):  # noqa: D401 - QDialog override in the fake
            during.append(window.action_export.isEnabled())
            return QDialog.DialogCode.Rejected
    monkeypatch.setattr(component_check_dialog, "ComponentCheckDialog", FakeCheck)
    window._verify_component()
    assert during == [False]
    assert window.action_export.isEnabled()
