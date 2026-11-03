"""@brief Alphanumeric pins applied from an analysis (second review 2026-11-03, A1).

A pin matrix whose drawing numbers some pins with letters (a relay: 11, 12,
A1, A2) reaches the window with populated pins and a pin map mixing ints and
text. Applying it must not stop halfway (a plain sorted() raised TypeError
and the paid result was half applied), and the pin map read by the analysis
must be in place before the pinout is settled against the pads.
"""
import pytest

pytest.importorskip("PyQt6")

from ui_flow_helpers import header_extraction, prepare, run_extraction  # noqa: E402

MAP = [{"pin": 11, "row": 0, "col": 0}, {"pin": 12, "row": 0, "col": 1},
       {"pin": "A1", "row": 1, "col": 0}, {"pin": "A2", "row": 1, "col": 1}]


def test_mixed_populated_pins_and_map_are_applied_whole(window, monkeypatch, tmp_path):
    prepare(window, monkeypatch, tmp_path, "RELAY")
    errors = []
    monkeypatch.setattr(window, "_on_extract_error", lambda message: errors.append(message))
    run_extraction(window, header_extraction("RELAY", cols=2, pin_map=MAP,
                                             populated_pins=[11, 12, "A1", "A2"],
                                             pinout=[{"pin": "A1", "name": "Coil+"}]))
    assert errors == []
    assert window.pin_map == {11: (0, 0), 12: (0, 1), "A1": (1, 0), "A2": (1, 1)}
    assert window.field_populated_pins.text() == "11,12,A1,A2", "natural pin order, ints and text"
    names = {row["pin"]: row["name"] for row in window.pinout}
    assert names.get("A1") == "Coil+", "the pinout was settled against the map's pads"


def test_a_numeric_matrix_is_applied_as_before(window, monkeypatch, tmp_path):
    """Negative twin: digits only keep the old numeric order and ints."""
    prepare(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction("HDR", cols=2, populated_pins=[4, 1, 3]))
    assert window.field_populated_pins.text() == "1,3,4"
