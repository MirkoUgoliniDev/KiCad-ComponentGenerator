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
    assert "UN SOLO numero STAMPATO" in kwargs["system_prompt"]


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
    # CONTRACT CHANGED (review 2026-11-04, B3): a height conflict is no dead
    # end; the two values are offered as a structured question.
    assert any("18.90 mm" in line and "16.90 mm" in line and "non coincidono" in line for line in logs)
    assert "18.90 mm" in data["_body_conflict"]
    question, = data["_questions"]
    assert question["field"] == "body_height_mm"
    assert [option["value"] for option in question["options"]] == [18.9, 16.9]


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


# ── Two printed lists vote (user, 2026-11-03, «sgraviamo»; generality audit F1-F3) ──

def _printed(values, printed, notes="altezza dalla vista frontale"):
    return {"values": values, "printed": printed, "notes": notes}


def test_a_main_height_listed_by_neither_reading_loses_to_the_quote_both_list():
    """The relay: main 19.3 «read» (printed nowhere), both lists [16.9, 0.4, 3.5], dedicated 16.9."""
    data = _main(body_length_mm=22.5, body_width_mm=16.5, body_height_mm=19.3)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [16.9, 0.4, 3.5]
    logs = []
    apply_dedicated_body(data, _printed({"body_height": 16.9, "lead_length": 3.5, "standoff": 0.4},
                                        [16.9, 0.4, 3.5]), logs.append)
    assert data["body_height_mm"] == pytest.approx(16.9)
    assert data["package_geometry"]["body_height_mm"] == pytest.approx(16.9)
    assert data["pin_length_3d_mm"] == pytest.approx(3.5)
    assert "_body_conflict" not in data and "printed_vertical_quotes" not in data
    assert any("19.30 mm) non e' una quota stampata letta da entrambe" in line for line in logs)
    assert any("Lettura dedicata: altezza dalla vista frontale" in line for line in logs)
    assert any("Distanziale stampato" in line and "0.40 mm" in line for line in logs)


def test_a_right_main_height_is_never_discarded_on_one_list_alone():
    """Audit F2 (negative twin): main 16.9 listed by itself, dedicated 16.5 listed by itself only ->
    one point each, nobody wins, no value, conflict named; never «16.5 read»."""
    data = _main(body_height_mm=16.9)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [16.9, 0.4, 3.5]
    logs = []
    apply_dedicated_body(data, _printed({"body_height": 16.5}, [16.5, 0.4, 3.5]), logs.append)
    assert data["body_height_mm"] is None
    assert "16.90 mm" in data["_body_conflict"] and "16.50 mm" in data["_body_conflict"]


def test_a_main_height_both_lists_print_beats_a_dedicated_one_listed_once():
    """Mirror of the relay case: the dedicated reading slipped, the main value is printed twice."""
    data = _main(body_height_mm=16.9)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [16.9, 0.4, 3.5]
    logs = []
    apply_dedicated_body(data, _printed({"body_height": 17.3}, [16.9, 0.4, 3.5, 17.3]), logs.append)
    assert data["body_height_mm"] == 16.9
    assert any("17.30 mm) non e' una quota stampata letta da entrambe" in line for line in logs)


def test_a_listed_value_equal_to_a_plan_size_or_the_lead_never_supports_a_height():
    """Audit F3: 16.5 is the plan width (listed by a model that wrote the view's width too)."""
    data = _main(body_length_mm=22.5, body_width_mm=16.5, body_height_mm=16.9)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [16.9, 16.5, 3.5]
    apply_dedicated_body(data, _printed({"body_height": 16.5, "lead_length": 3.5}, [16.5, 16.9, 3.5]))
    assert data["body_height_mm"] == 16.9, "16.5 is listed twice but it is the width: no support"


def test_a_height_that_is_the_sum_of_two_listed_quotes_is_a_computation():
    """A model that summed 16.9 + 0.4 and then listed 17.3 gets no support from its own list."""
    data = _main(body_height_mm=16.9)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [16.9, 0.4, 3.5]
    apply_dedicated_body(data, _printed({"body_height": 17.3}, [17.3, 16.9, 0.4, 3.5]))
    assert data["body_height_mm"] == 16.9


def test_two_printed_candidates_in_both_lists_block_with_both_values():
    """Declared fragility: two vertical quotes both printed twice, not one the sum of others
    (16.9 and 17.5, e.g. a stepped housing) -> the lists do not decide, no value."""
    data = _main(body_length_mm=22.5, body_height_mm=17.5)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [17.5, 16.9, 0.4]
    logs = []
    apply_dedicated_body(data, _printed({"body_height": 16.9}, [17.5, 16.9, 0.4]), logs.append)
    assert data["body_height_mm"] is None and "body_height_mm" not in data["package_geometry"]
    assert "17.50 mm" in data["_body_conflict"] and "16.90 mm" in data["_body_conflict"]
    assert any("le quote stampate non decidono" in line for line in logs)


def test_a_printed_overall_quote_that_is_the_sum_of_body_and_standoff_yields_to_the_body():
    """Consequence of the sum rule, declared: a sheet printing 16.9, 0.4 and their sum 17.3 gives the
    body-only 16.9 (the STEP then sits on the PCB without the standoff, said in the notes)."""
    data = _main(body_length_mm=22.5, body_height_mm=17.3)
    data["value_sources"]["body_height_mm"] = "read"
    data["printed_vertical_quotes"] = [17.3, 16.9, 0.4]
    apply_dedicated_body(data, _printed({"body_height": 16.9, "standoff": 0.4}, [17.3, 16.9, 0.4]))
    assert data["body_height_mm"] == pytest.approx(16.9) and "_body_conflict" not in data


def test_a_missing_main_height_is_adopted_only_when_both_lists_print_it():
    data = _main(body_length_mm=22.5)
    data["printed_vertical_quotes"] = [16.9, 0.4, 3.5]
    apply_dedicated_body(data, _printed({"body_height": 16.9}, [16.9, 0.4, 3.5]))
    assert data["body_height_mm"] == pytest.approx(16.9)
    # One list only: adopted as an unconfirmed reading (the earlier rule), said so.
    data = _main(body_length_mm=22.5)
    logs = []
    apply_dedicated_body(data, _printed({"body_height": 16.9}, [16.9, 0.4, 3.5]), logs.append)
    assert data["body_height_mm"] == pytest.approx(16.9)
    assert any("Non confermate da una seconda lettura" in line for line in logs)


def test_without_any_printed_list_the_earlier_rules_apply():
    data = _main(body_height_mm=16.9)
    data["value_sources"]["body_height_mm"] = "read"
    apply_dedicated_body(data, _printed({"body_height": 16.5}, []))
    assert data["body_height_mm"] is None
