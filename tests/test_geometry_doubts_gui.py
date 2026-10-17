"""@brief Structural doubts of a package geometry are asked, never fixed silently (audit 2026-10-15, item 3, GUI part).

The extraction now returns `ExtractedParams.geometry_doubts`
(`core.package_geometry.package_doubts`): a body that covers the pads with
the swap that would clear it, a clockwise pin numbering, a bottom view also
declared mirrored. The window shows each one while applying the analysis:
- body/pad mismatch cleared by the swap: «Scambia lunghezza e larghezza» or
  «Lascia così»;
- the others: «Confermo i dati» or «Torna indietro» (nothing is generated);
and records the answer per doubt and per analysed data, so a replay of the
same analysis is not asked again.
"""
import pytest

from kicadgen.gui import main_window as gui
from tests.ui_flow_helpers import package_extraction, prepare, run_extraction, soic_geometry


#: The real question, captured at import (conftest replaces it in every test).
_REAL_ASK = gui.MainWindow._ask_geometry_doubt


def _answer(monkeypatch, answer, seen):
    def ask(self, doubt):
        seen.append(dict(doubt))
        return answer
    monkeypatch.setattr(gui.MainWindow, "_ask_geometry_doubt", ask)


def _sizes(window):
    package = window.package_geometry
    return package.body_length_mm, package.body_width_mm


def test_a_transposed_body_is_swapped_only_when_the_user_chooses(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_SWAP, seen)
    run_extraction(window, package_extraction(soic_geometry(4.9, 3.9)))
    assert [d["code"] for d in seen] == ["body_pad_mismatch"] and seen[0]["swap_clears"] is True
    assert _sizes(window) == (3.9, 4.9)
    # The saved project carries the swapped body.
    assert window._project_state()["package_geometry"]["body_length_mm"] == 3.9


def test_lascia_cosi_keeps_the_declared_sizes(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_KEEP, seen)
    run_extraction(window, package_extraction(soic_geometry(4.9, 3.9)))
    assert len(seen) == 1 and _sizes(window) == (4.9, 3.9)


def test_consistent_data_ask_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_SWAP, seen)
    run_extraction(window, package_extraction(soic_geometry(3.9, 4.9)))
    assert seen == [] and _sizes(window) == (3.9, 4.9)


def test_the_answer_is_not_asked_again_for_the_same_data(window, monkeypatch, tmp_path):  # noqa: F811
    """A replay of the same analysis reuses the answer (and re-applies the
    swap); different data is a new question."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_SWAP, seen)
    run_extraction(window, package_extraction(soic_geometry(4.9, 3.9)))
    run_extraction(window, package_extraction(soic_geometry(4.9, 3.9)))
    assert len(seen) == 1 and _sizes(window) == (3.9, 4.9)
    run_extraction(window, package_extraction(soic_geometry(5.0, 3.9)))
    assert len(seen) == 2


@pytest.mark.parametrize("geometry, code", [
    (dict(clockwise=True), "pin_order_clockwise"),
    (dict(view="bottom", mirrored=True), "bottom_view_mirrored")])
def test_torna_indietro_generates_nothing(window, monkeypatch, tmp_path, geometry, code):  # noqa: F811
    """Negative: going back leaves the files alone (no footprint written)."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_BACK, seen)
    run_extraction(window, package_extraction(soic_geometry(**geometry)))
    assert [d["code"] for d in seen] == [code]
    assert not (window._project_dir() / "SOIC.pretty" / "SOIC.kicad_mod").exists()
    # «Back» is not a decision on the data: asked again next time.
    run_extraction(window, package_extraction(soic_geometry(**geometry)))
    assert len(seen) == 2


@pytest.mark.parametrize("geometry", [dict(clockwise=True), dict(view="bottom", mirrored=True)])
def test_confermo_generates_with_the_data_unchanged(window, monkeypatch, tmp_path, geometry):  # noqa: F811
    """Positive: confirmed, the footprint is generated with the geometry as read."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_KEEP, seen)
    data = soic_geometry(**geometry)
    run_extraction(window, package_extraction(data))
    assert len(seen) == 1
    assert (window._project_dir() / "SOIC.pretty" / "SOIC.kicad_mod").exists()
    assert [p.y_mm for p in window.package_geometry.pins] == [p["y_mm"] for p in data["pins"]]


def test_the_real_question_offers_the_documented_buttons(window, monkeypatch):  # noqa: F811
    """The dialog itself: swap/leave for a clearable mismatch, confirm/back
    otherwise; Esc is the safe answer (leave / back)."""
    from PyQt6.QtWidgets import QMessageBox
    seen = {}

    def fake_exec(box):
        seen["buttons"] = sorted(b.text() for b in box.buttons())
        seen["escape"] = box.escapeButton().text()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    tx = window.i18n.text
    monkeypatch.setattr(gui.MainWindow, "_ask_geometry_doubt", _REAL_ASK)
    answer = window._ask_geometry_doubt({"code": "body_pad_mismatch", "message": "DUBBIO",
                                         "swap_clears": True})
    assert answer == gui.MainWindow.GEOMETRY_DOUBT_KEEP
    assert seen["buttons"] == sorted([tx("Scambia lunghezza e larghezza"), tx("Lascia così")])
    assert seen["escape"] == tx("Lascia così")
    answer = window._ask_geometry_doubt({"code": "pin_order_clockwise", "message": "DUBBIO"})
    assert answer == gui.MainWindow.GEOMETRY_DOUBT_BACK
    assert seen["buttons"] == sorted([tx("Confermo i dati"), tx("Torna indietro")])


# ── Body over the lead lands (E read instead of E1, or undeclared J leads) ──

def test_body_over_the_lands_offers_j_leads_and_never_a_swap(window, monkeypatch, tmp_path):  # noqa: F811
    """«Piedini a J» declares the lead form (the body over the lands is then
    right); the sizes are never swapped for this doubt."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_J_LEAD, seen)
    run_extraction(window, package_extraction(soic_geometry(6.0, 4.9)))
    assert [d["code"] for d in seen] == ["body_over_lead_lands"]
    assert window.package_geometry.lead_form == "j_lead"
    assert _sizes(window) == (6.0, 4.9)


def test_a_swap_answer_never_touches_a_body_over_the_lands(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "SOIC")
    seen = []
    _answer(monkeypatch, gui.MainWindow.GEOMETRY_DOUBT_SWAP, seen)
    run_extraction(window, package_extraction(soic_geometry(6.0, 4.9)))
    assert _sizes(window) == (6.0, 4.9) and window.package_geometry.lead_form == "unknown"


def test_the_j_lead_button_is_offered_only_when_the_lead_form_is_unknown(window, monkeypatch, tmp_path):  # noqa: F811
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.core.package_geometry import package_from_mapping
    seen = {}

    def fake_exec(box):
        seen["buttons"] = sorted(b.text() for b in box.buttons())
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(gui.MainWindow, "_ask_geometry_doubt", _REAL_ASK)
    tx = window.i18n.text
    doubt = {"code": "body_over_lead_lands", "message": "DUBBIO", "pins": ["1", "8"]}
    window.package_geometry = package_from_mapping(soic_geometry(6.0, 4.9))
    window._ask_geometry_doubt(doubt)
    assert seen["buttons"] == sorted([tx("Confermo i dati"), tx("Piedini a J"), tx("Torna indietro")])
    window.package_geometry = package_from_mapping(soic_geometry(6.0, 4.9, lead_form="gull_wing"))
    window._ask_geometry_doubt(doubt)
    assert seen["buttons"] == sorted([tx("Confermo i dati"), tx("Torna indietro")])
