"""@brief Pin names typed by the user survive a new analysis unless the user says otherwise (audit 2026-10-15, items 2 and 11).

Item 2: a new analysis reset `pinout` before the overwrite question, saved
it and rewrote the .kicad_sym with «~», silently discarding names typed in
the symbol editor; the overwrite question never listed the .kicad_sym. Now
the names the USER applied are recorded (provenance); a new analysis that
would replace or drop one asks first («Mantieni i miei nomi» / «Usa i nomi
della nuova analisi»), nothing is reset before the answer, and the overwrite
question lists the .kicad_sym.

Item 11: pin names read beside the pads of a package (`pins[].name`, or the
single-component `pads[].name`) never reached the symbol; now they build the
pinout when the analysis gives no explicit table (the table wins).
"""
import json
import re

from kicadgen.gui import main_window as gui
from tests.ui_flow_helpers import (header_extraction, package_extraction, prepare,
                                   run_extraction, soic_geometry)

NAME = "HDR"
#: The real question, captured at import (conftest replaces it in every test).
_REAL_ASK = gui.MainWindow._ask_user_pin_names


def _symbol_names(folder, name=NAME):
    text = (folder / f"{name}.kicad_sym").read_text(encoding="utf-8")
    body = text.split(f'{name}_1_1"')[-1]
    return dict((number, pin) for pin, number in
                re.findall(r'\(name "([^"]*)"[\s\S]*?\(number "([^"]*)"', body))


def _type_names(window, names):
    """Type names in the symbol editor and press «Applica», as the user does."""
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    table = window.symbol_tab.table
    for row, name in enumerate(names):
        table.item(row, 1).setText(name)
    window.symbol_tab.apply_button.click()
    assert not window.symbol_tab.has_pending_edits()


def _record_question(monkeypatch, answer, seen):
    def ask(self, conflicts, proposed, vanished=()):
        # Nothing may have been reset or saved before the answer.
        folder = self._project_dir()
        saved = json.loads((folder / "project_config.json").read_text(encoding="utf-8"))
        seen.append({"conflicts": dict(conflicts), "proposed": dict(proposed),
                     "vanished": list(vanished),
                     "pinout": [(r.get("pin"), r.get("name")) for r in self.pinout][:2],
                     "saved": [(str(r.get("pin")), r.get("name"))
                               for r in saved["state"]["pinout"]][:2]})
        return answer
    monkeypatch.setattr(gui.MainWindow, "_ask_user_pin_names", ask)


def test_typed_names_are_asked_about_and_kept(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative of the old behaviour: the re-analysis asks, nothing was reset
    before the answer, «Mantieni» keeps VCC/GND in pinout and .kicad_sym."""
    asked = prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction())
    _type_names(window, ["VCC", "GND"])
    folder = window._project_dir()
    assert _symbol_names(folder)["1"] == "VCC"
    seen = []
    _record_question(monkeypatch, gui.MainWindow.USER_PIN_NAMES_KEEP, seen)
    run_extraction(window, header_extraction())
    assert len(seen) == 1
    assert seen[0]["conflicts"] == {"1": "VCC", "2": "GND"}
    assert seen[0]["pinout"] == [("1", "VCC"), ("2", "GND")]
    assert seen[0]["saved"] == [("1", "VCC"), ("2", "GND")]
    names = {str(r["pin"]): r["name"] for r in window.pinout}
    assert names["1"] == "VCC" and names["2"] == "GND"
    assert _symbol_names(folder)["1"] == "VCC" and _symbol_names(folder)["2"] == "GND"
    # The overwrite question names the symbol file it will rewrite.
    assert any(f"{NAME}.kicad_sym" in text for text in asked)


def test_replace_takes_the_new_analysis_and_forgets_the_provenance(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction())
    _type_names(window, ["VCC"])
    seen = []
    _record_question(monkeypatch, gui.MainWindow.USER_PIN_NAMES_REPLACE, seen)
    run_extraction(window, header_extraction(pinout=[{"pin": 1, "name": "IN"}]))
    assert len(seen) == 1 and seen[0]["proposed"]["1"] == "IN"
    assert {str(r["pin"]): r["name"] for r in window.pinout}["1"] == "IN"
    assert _symbol_names(window._project_dir())["1"] == "IN"
    assert window._user_pin_names == {}


def test_analysis_names_are_replaced_without_a_question(window, monkeypatch, tmp_path):  # noqa: F811
    """Positive control: names that came from a previous ANALYSIS are not the
    user's; a new analysis replaces them as before, with no question."""
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction(pinout=[{"pin": 1, "name": "VCC"}]))
    seen = []
    _record_question(monkeypatch, gui.MainWindow.USER_PIN_NAMES_KEEP, seen)
    run_extraction(window, header_extraction())
    assert seen == []
    assert window.pinout == []


def test_a_name_the_new_analysis_agrees_with_is_not_a_conflict(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction())
    _type_names(window, ["VCC"])
    seen = []
    _record_question(monkeypatch, gui.MainWindow.USER_PIN_NAMES_REPLACE, seen)
    run_extraction(window, header_extraction(pinout=[{"pin": "1", "name": "VCC"}]))
    assert seen == []


def test_the_provenance_survives_reopening_the_project(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction())
    _type_names(window, ["VCC"])
    folder = window._project_dir()
    assert window._save_project_config(force=True)
    window._switch_project("Other", window._general_project_state)
    assert window._user_pin_names == {}
    window._open_project_at(folder)
    assert window._user_pin_names == {"1": "VCC"}
    seen = []
    _record_question(monkeypatch, gui.MainWindow.USER_PIN_NAMES_KEEP, seen)
    run_extraction(window, header_extraction())
    assert len(seen) == 1


# ── Item 11: per-pad names of a package ─────────────────────────────────

NAMES = ("VIN", "GND", "EN", "NC", "BST", "SW", "FB", "VOUT")


def test_package_pad_names_reach_the_pinout_and_the_symbol(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative of the old behaviour (every pin «~»)."""
    prepare(window, monkeypatch, tmp_path, "BUCK")
    run_extraction(window, package_extraction(soic_geometry(names=NAMES), name="BUCK"))
    assert [(r["pin"], r["name"]) for r in window.pinout] == [
        (str(i + 1), name) for i, name in enumerate(NAMES)]
    assert _symbol_names(window._project_dir(), "BUCK")["8"] == "VOUT"


def test_an_explicit_pinout_table_wins_over_pad_names(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "BUCK")
    run_extraction(window, package_extraction(soic_geometry(names=NAMES), name="BUCK",
                                              pinout=[{"pin": "1", "name": "IN"}]))
    assert window.pinout == [{"pin": "1", "name": "IN"}]


def test_unnamed_pads_give_no_pinout(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "BUCK")
    run_extraction(window, package_extraction(soic_geometry(), name="BUCK"))
    assert window.pinout == []


def test_package_pinout_skips_holes_and_keeps_the_first_name_of_a_shared_number():
    from kicadgen.core.package_geometry import PinSpec, PackageGeometry
    geometry = PackageGeometry(name="P", family="custom", body_length_mm=5, body_width_mm=5, pins=[
        PinSpec(number="1", x_mm=0, y_mm=0, pad_length_mm=1, pad_width_mm=1, name="GND"),
        PinSpec(number="1", x_mm=2, y_mm=0, pad_length_mm=1, pad_width_mm=1, name="EP"),
        PinSpec(number="2", x_mm=4, y_mm=0, pad_length_mm=1, pad_width_mm=1, name="~"),
        PinSpec(number="", x_mm=0, y_mm=3, pad_length_mm=1, pad_width_mm=1, drill_mm=1,
                pad_type="np_thru_hole", shape="circle", name="MH")])
    assert gui._package_pinout(geometry) == [{"pin": "1", "name": "GND"}]


def test_the_real_question_keeps_by_default(window, monkeypatch):  # noqa: F811
    """The dialog lists each conflict (user name -> analysis name) and Esc
    keeps the user's names: the safe answer loses nothing."""
    from PyQt6.QtWidgets import QMessageBox
    seen = {}

    def fake_exec(box):
        seen["buttons"] = sorted(b.text() for b in box.buttons())
        seen["escape"] = box.escapeButton().text()
        seen["details"] = box.informativeText()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(gui.MainWindow, "_ask_user_pin_names", _REAL_ASK)
    tx = window.i18n.text
    answer = window._ask_user_pin_names({"1": "VCC", "10": "GND"}, {"1": "IN"})
    assert answer == gui.MainWindow.USER_PIN_NAMES_KEEP
    assert seen["escape"] == tx("Mantieni i miei nomi")
    assert seen["buttons"] == sorted([tx("Mantieni i miei nomi"), tx("Usa i nomi della nuova analisi")])
    assert seen["details"].splitlines() == ["1: «VCC» → «IN»", "10: «GND» → «~»"]
