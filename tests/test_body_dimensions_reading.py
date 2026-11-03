"""@brief The body quotes of a single part are read twice (user, 2026-11-03, T7N relay).

The main model skipped the side view's height in two analyses out of four
and invented it (18.9 and 19.3 mm for a quoted 16.9) in the other two: the
STEP model was either deferred or built on a false height. A dedicated
reading transcribes the printed outline quotes, each from the view that
prints it; the program compares them with the main answer and never
averages: a missing main quote is adopted (said unconfirmed), an agreeing
one is confirmed, a disagreeing vertical quote is dropped (no witness), a
disagreeing plan quote keeps the main value (the drawn holes bound it).
Positive and negative cases differ in structure, not in a tuned number.
"""
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import apply_dedicated_body, read_body_dimensions


def _drawing(tmp_path):
    path = tmp_path / "quote.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


def _main(unit="mm", **sizes):
    data = {"drawing_unit": unit, "package_geometry": {"origin": "body_center", "pins": []},
            "value_sources": {}}
    for key, value in sizes.items():
        data[key] = value
        if key != "pin_length_3d_mm":
            data["package_geometry"][key] = value
    return data


# ── read_body_dimensions ────────────────────────────────────────────────

def test_the_reading_converts_the_declared_unit_and_keeps_only_valid_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "_file_to_content_block", lambda path, model: {"type": "text", "text": "img"})
    answer = {"drawing_unit": "inch", "body_height": 0.665, "body_length": "wide", "standoff": -1}
    reading = read_body_dimensions([_drawing(tmp_path)], model="m", call=Mock(return_value=answer))
    assert reading["values"] == {"body_height": pytest.approx(16.891)}
    assert "failed" not in reading and "discarded" not in reading


def test_a_failed_or_unitless_reading_is_best_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "_file_to_content_block", lambda path, model: {"type": "text", "text": "img"})
    failed = read_body_dimensions([_drawing(tmp_path)], model="m",
                                  call=Mock(side_effect=ext.ExtractionError("down")))
    assert failed["failed"] == "down" and failed["values"] == {}
    unitless = read_body_dimensions([_drawing(tmp_path)], model="m",
                                    call=Mock(return_value={"body_height": 16.9}))
    assert "discarded" in unitless and unitless["values"] == {}
    assert read_body_dimensions([], model="m", call=Mock()) is None


def test_the_reading_uses_the_body_tool_and_the_given_model(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "_file_to_content_block", lambda path, model: {"type": "text", "text": "img"})
    call = Mock(return_value={"drawing_unit": "mm", "body_height": 16.9})
    read_body_dimensions([_drawing(tmp_path)], model="vendor/reader", call=call)
    kwargs = call.call_args.kwargs
    assert kwargs["model"] == "vendor/reader"
    assert kwargs["tool"]["function"]["name"] == "report_body_dimensions"
    assert "quota VERTICALE" in kwargs["system_prompt"]


# ── apply_dedicated_body ────────────────────────────────────────────────

def test_a_height_the_main_answer_lacks_is_adopted_and_said_unconfirmed():
    """The relay case: no height in the main answer, 16.9 mm read from the side view."""
    data = _main(body_length_mm=22.5, body_width_mm=16.5)
    logs = []
    apply_dedicated_body(data, {"values": {"body_length": 22.5, "body_width": 16.5, "body_height": 16.9,
                                           "lead_length": 3.5}, "notes": ""}, logs.append)
    assert data["body_height_mm"] == pytest.approx(16.9)
    assert data["package_geometry"]["body_height_mm"] == pytest.approx(16.9)
    assert data["pin_length_3d_mm"] == pytest.approx(3.5)
    assert "pin_length_3d_mm" not in data["package_geometry"]
    assert data["value_sources"]["body_height_mm"] == "read"
    assert any("confermate" in line and "lunghezza" in line for line in logs)
    assert any("assenti nella risposta principale" in line and "altezza 16.90 mm" in line for line in logs)


def test_an_adopted_value_is_written_in_the_answer_unit():
    """The answer is still in its declared unit: the mm value is written back in inches."""
    data = _main(unit="inch", body_length_mm=0.886, body_width_mm=0.65)
    apply_dedicated_body(data, {"values": {"body_height": 16.891}, "notes": ""})
    assert data["body_height_mm"] == pytest.approx(0.665, abs=1e-4)


def test_a_disagreeing_height_is_dropped_because_no_witness_sees_it():
    """Negative twin of the adoption: an invented 18.9 against a printed 16.9 -> neither is used."""
    data = _main(body_length_mm=22.5, body_width_mm=16.5, body_height_mm=18.9)
    data["value_sources"]["body_height_mm"] = "read"
    logs = []
    apply_dedicated_body(data, {"values": {"body_height": 16.9}, "notes": ""}, logs.append)
    assert data["body_height_mm"] is None
    assert "body_height_mm" not in data["package_geometry"]
    assert any("18.90 mm contro 16.90 mm" in line and "quota verticale" in line for line in logs)


def test_a_disagreeing_plan_quote_keeps_the_main_value_with_a_warning():
    """Two READ plan quotes that disagree: the main value stays and the warning claims no check."""
    data = _main(body_length_mm=22.5, body_width_mm=16.5)
    data["value_sources"]["body_length_mm"] = "read"
    logs = []
    apply_dedicated_body(data, {"values": {"body_length": 23.5}, "notes": ""}, logs.append)
    assert data["body_length_mm"] == 22.5 and data["package_geometry"]["body_length_mm"] == 22.5
    assert any("22.50 mm contro 23.50 mm" in line and "resta il valore" in line for line in logs)
    assert not any("controllato" in line for line in logs)


def test_a_declared_estimate_is_replaced_by_the_printed_quote_on_every_axis():
    """Provenance ranks before value (generality audit 2026-11-03): a main estimate, declared or
    undeclared, loses to the transcribed printed quote; the positive twin of the two-readings block."""
    for source in ("estimated", None):
        data = _main(body_length_mm=22.0, body_width_mm=16.5, body_height_mm=19.0)
        if source:
            data["value_sources"].update(body_length_mm=source, body_height_mm=source)
        logs = []
        apply_dedicated_body(data, {"values": {"body_length": 22.5, "body_height": 16.9}, "notes": ""},
                             logs.append)
        assert data["body_height_mm"] == pytest.approx(16.9), source
        assert data["body_length_mm"] == pytest.approx(22.5), source
        assert data["package_geometry"]["body_height_mm"] == pytest.approx(16.9)
        assert data["value_sources"]["body_height_mm"] == "read"
        assert any("sostituisce la stima" in line and "19.00 mm" in line for line in logs)


def test_an_estimate_the_printed_quote_agrees_with_becomes_a_reading():
    data = _main(body_height_mm=16.9)
    data["value_sources"]["body_height_mm"] = "estimated"
    apply_dedicated_body(data, {"values": {"body_height": 16.9}, "notes": ""})
    assert data["value_sources"]["body_height_mm"] == "read" and data["body_height_mm"] == 16.9


def test_a_main_height_the_reading_did_not_see_is_said_unverified():
    """No side view among the images: the main height stays, but never in silence."""
    data = _main(body_length_mm=22.5, body_height_mm=18.9)
    data["value_sources"]["body_height_mm"] = "read"
    logs = []
    apply_dedicated_body(data, {"values": {"body_length": 22.5}, "notes": ""}, logs.append)
    assert data["body_height_mm"] == 18.9
    assert any("NON verificata" in line and "18.90 mm" in line for line in logs)
    # Negative twin: a main answer without a height gets no such warning (the STEP deferral says it).
    data = _main(body_length_mm=22.5)
    logs = []
    apply_dedicated_body(data, {"values": {"body_length": 22.5}, "notes": ""}, logs.append)
    assert not any("NON verificata" in line for line in logs)


def test_agreement_is_judged_at_the_rounding_of_the_answer_unit():
    """0.665 in = 16.891 mm against 16.9 mm: within the inch rounding (0.127 mm), confirmed."""
    data = _main(unit="inch", body_height_mm=0.665)
    data["value_sources"]["body_height_mm"] = "read"
    logs = []
    apply_dedicated_body(data, {"values": {"body_height": 16.9}, "notes": ""}, logs.append)
    assert data["body_height_mm"] == 0.665
    assert any("confermate" in line and "altezza" in line for line in logs)


def test_an_undeclared_main_unit_compares_nothing():
    data = {"drawing_unit": None, "package_geometry": {"pins": []}}
    logs = []
    apply_dedicated_body(data, {"values": {"body_height": 16.9}, "notes": ""}, logs.append)
    assert "body_height_mm" not in data
    assert any("non dichiara l'unita'" in line for line in logs)


def test_failed_discarded_or_empty_readings_change_nothing():
    for reading in (None, {"values": {}, "failed": "x"}, {"values": {}, "discarded": "y"},
                    {"values": {}, "notes": ""}):
        data = _main(body_height_mm=18.9)
        apply_dedicated_body(data, reading)
        assert data["body_height_mm"] == 18.9
