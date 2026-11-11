"""@brief Extraction fixes of 2026-11-03: pin labels as text, readers that keep the main answer,
null pad numbers, undeclared drawing units.

* A: pin numbers are TEXT («1», «11», «A1», «EP»): the T7NS5D4-12 relay
  lost pads A1 and A2 because the schemas declared ``pin`` an integer.
* B (M5): a drawing that vanished before a dedicated reading must not lose
  the paid main answer.
* C (M6): a null pad number is the empty number, never the pad «None».
* D (L11): a drawing unit that is missing or unknown is never read as mm.

Every model call is a Mock: no network, no .env. Each rule has a positive and
a negative case told apart by a structural difference (a label vs a malformed
value, a declared vs an undeclared unit), not by a number tuned to one part.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    ExtractionError, apply_dedicated_numbering, apply_dedicated_pads, read_pad_positions,
    read_pin_numbering, single_answer_to_data)
from kicadgen.services.openrouter import strict_tool


def _drawing(tmp_path):
    """A blank drawing image the readers can encode."""
    from PIL import Image
    path = tmp_path / "quote.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


# -- A: pin labels -----------------------------------------------------------------

#: Relay-like bottom view: two coil pads A1/A2 and three numbered contacts on a 2x3 grid.
RELAY_MAP = [{"pin": "A1", "row": 0, "col": 0}, {"pin": "11", "row": 0, "col": 1},
             {"pin": "14", "row": 0, "col": 2}, {"pin": "A2", "row": 1, "col": 0},
             {"pin": "12", "row": 1, "col": 2}, {"pin": "EP", "row": 1, "col": 1}]


def test_every_pin_field_of_the_schemas_is_text():
    """The pin_map, pinout and dedicated-numbering pins are strings, as printed."""
    properties = ext._openai_tool()["function"]["parameters"]["properties"]
    assert properties["pin_map"]["items"]["properties"]["pin"]["type"] == "string"
    assert properties["pinout"]["items"]["properties"]["pin"]["type"] == "string"
    assert properties["populated_pins"]["items"]["type"] == "string"
    numbering = ext._pin_numbering_tool()["function"]["parameters"]
    assert numbering["properties"]["pins"]["items"]["properties"]["pin"]["type"] == "string"


def test_the_numbering_tool_still_converts_losslessly_to_strict_mode():
    """A string pin keeps the DeepSeek strict conversion lossless (every field nullable)."""
    strict = strict_tool(ext._pin_numbering_tool())
    assert strict is not None
    pin = strict["function"]["parameters"]["properties"]["pins"]["anyOf"][0]["items"][
        "properties"]["pin"]
    assert {"type": "string"} in [{k: v for k, v in option.items() if k == "type"}
                                  for option in pin["anyOf"]]


def test_an_alphanumeric_pin_map_is_kept_and_mirrored():
    """Positive: A1, A2, EP are labels; numeric text stays the number it was (11 -> 11)."""
    entries = ext._pin_map_seen_from_top(RELAY_MAP, 3, "bottom", 2)
    seen = {entry["pin"]: (entry["row"], entry["col"]) for entry in entries}
    assert seen == {"A1": (0, 2), 11: (0, 1), 14: (0, 0), "A2": (1, 2), 12: (1, 0),
                    "EP": (1, 1)}


def test_a_numeric_pin_map_works_as_before():
    """Numbers sent as integers or digits are the same pins: 1 and «1»."""
    entries = ext._pin_map_seen_from_top(
        [{"pin": 1, "row": 0, "col": 0}, {"pin": "2", "row": 1, "col": 0}], 1, "top", 2)
    assert [entry["pin"] for entry in entries] == [1, 2]


# CONTRACT CHANGED 2026-11-03 (review S7): pad 0 is a real pad number that
# _validated_pinout already accepted; _pin_label now accepts it too, as the
# text «0», so 0 and «0» left this list of malformed pins. Negative numbers stay.
@pytest.mark.parametrize("pin", [True, -1, "", "pin 1", 1.5, None, "A 1", "-3"])
def test_a_malformed_pin_is_still_refused(pin):
    """Negative: booleans, negative numbers, fractions, blanks and sentences are not labels."""
    with pytest.raises(ExtractionError):
        ext._pin_map_seen_from_top([{"pin": pin, "row": 0, "col": 0}], 2, "top", 1)


def test_a_label_repeated_as_number_and_text_is_one_pin_twice():
    """Negative: 1 and «1» name the same pin, so the map repeats it."""
    with pytest.raises(ExtractionError, match="piu' volte"):
        ext._pin_map_seen_from_top(
            [{"pin": 1, "row": 0, "col": 0}, {"pin": " 1 ", "row": 0, "col": 1}], 2, "top", 1)


def test_the_dedicated_numbering_keeps_alphanumeric_pins(tmp_path):
    """Positive: the reader returns A1/A2 instead of dropping them (T7NS5D4-12)."""
    call = Mock(return_value={"contacts_view": "bottom", "pins": RELAY_MAP, "notes": ""})
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=3, model="m", call=call)
    assert "discarded" not in reading and "failed" not in reading
    assert {entry["pin"] for entry in reading["pins"]} == {"A1", "A2", "EP", 11, 12, 14}


def test_too_many_distinct_labels_still_contradict_the_grid(tmp_path):
    """Negative: seven distinct printed labels cannot sit on a 2x3 grid, letters or not."""
    pins = RELAY_MAP + [{"pin": "B1", "row": 0, "col": 0}]
    call = Mock(return_value={"contacts_view": "bottom", "pins": pins, "notes": ""})
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=3, model="m", call=call)
    assert reading["contradicts_grid"] is True and reading["pins"] == []


def test_alphanumeric_readings_that_agree_confirm_the_numbering():
    """apply_dedicated_numbering compares labels as text: «A1» in both readings agrees."""
    data = {"rows": 2, "cols": 3, "contacts_view": "bottom", "pin_map": list(RELAY_MAP)}
    reading = {"view": "bottom", "notes": "",
               "pins": ext._pin_map_seen_from_top(RELAY_MAP, 3, "top", 2)}
    apply_dedicated_numbering(data, reading)
    assert "_pin_map_conflict" not in data
    assert "confermata" in data["_numbering_note"]


def test_alphanumeric_readings_that_disagree_block():
    """Negative: A1 and A2 swapped between the readings is a conflict, not a crash."""
    swapped = [dict(entry, pin={"A1": "A2", "A2": "A1"}.get(entry["pin"], entry["pin"]))
               for entry in RELAY_MAP]
    data = {"rows": 2, "cols": 3, "contacts_view": "bottom", "pin_map": list(RELAY_MAP)}
    reading = {"view": "bottom", "notes": "",
               "pins": ext._pin_map_seen_from_top(swapped, 3, "top", 2)}
    apply_dedicated_numbering(data, reading)
    assert "non coincidono" in data["_pin_map_conflict"]


def test_a_complete_alphanumeric_map_decides_the_pins_and_a_partial_one_blocks():
    """Positive: a map placing every position stands. Negative: no standard convention
    completes letters, so a partial alphanumeric map blocks instead of numbering by guess."""
    entries = ext._pin_map_seen_from_top(RELAY_MAP, 3, "top", 2)
    notes = []
    pin_map, populated, block = ext._resolve_pin_numbering(
        entries, None, 2, 3, 6, dedicated=True, notes=notes)
    assert block is None and {e["pin"] for e in pin_map} == {"A1", "A2", "EP", 11, 12, 14}
    partial = ext._pin_map_seen_from_top(RELAY_MAP[:2], 3, "top", 2)
    pin_map, populated, block = ext._resolve_pin_numbering(
        partial, None, 2, 3, 6, dedicated=True, notes=[])
    assert pin_map is None and block and "A1" in block


def test_populated_pins_accept_labels_placed_by_the_map():
    """Positive: «A1» is populated when the map places it. Negative: without a map a
    text label is outside the automatic 1..rows x cols numbering."""
    notes = []
    assert ext._validated_populated_pins(["A1", "11", 14], 6, notes,
                                         mapped=["A1", 11, 14]) == ["A1", 11, 14]
    notes = []
    assert ext._validated_populated_pins(["A1", "2"], 6, notes, None) == [2]
    assert notes and "A1" in notes[0]


def test_the_pin_field_follows_an_alphanumeric_map():
    """_pin_field_positions draws every labelled pin instead of dropping the map."""
    params = SimpleNamespace(pin_map=ext._pin_map_seen_from_top(RELAY_MAP, 3, "top", 2),
                             rows=2, cols=3, pitch_mm=2.54, row_spacing_mm=5.08,
                             populated_pins=None)
    assert len(ext._pin_field_positions(params)) == 6


# -- B: a vanished drawing keeps the main answer -----------------------------------

def test_the_numbering_reader_reports_a_vanished_drawing_as_failed(tmp_path):
    call = Mock()
    logs = []
    reading = read_pin_numbering([tmp_path / "gone.png"], rows=2, cols=3, model="m", call=call,
                                 progress=logs.append)
    assert reading["pins"] == [] and reading["failed"] and not call.called
    assert logs and "resta la numerazione" in logs[0]


def test_the_pad_reader_reports_a_vanished_drawing_as_failed(tmp_path):
    call = Mock()
    reading = read_pad_positions([tmp_path / "gone.png"], model="m", call=call)
    assert reading["pads"] == {} and reading["failed"] and not call.called


def test_a_cancellation_while_reading_still_propagates(tmp_path):
    """Negative: a cancellation is not a failure to swallow."""
    call = Mock(side_effect=ext.ExtractionCancelled("stop"))
    with pytest.raises(ext.ExtractionCancelled):
        read_pad_positions([_drawing(tmp_path)], model="m", call=call)
    with pytest.raises(ext.ExtractionCancelled):
        read_pin_numbering([_drawing(tmp_path)], rows=2, cols=3, model="m", call=call)


# -- C: a null pad number is not the pad «None» ------------------------------------

def _single(number):
    return {"drawing_unit": "mm", "contacts_view": "top", "confidence": "medium",
            "pads": [{"number": number, "x_mm": 0.0, "y_mm": 0.0, "pad_type": "thru_hole",
                      "pad_length_mm": 1.8, "pad_width_mm": 1.8},
                     {"number": "2", "x_mm": 2.54, "y_mm": 0.0, "pad_type": "thru_hole",
                      "pad_length_mm": 1.8, "pad_width_mm": 1.8}]}


def test_a_null_pad_number_is_a_broken_shape():
    """Contract changed 2026-11-10: the empty number it used to become is now refused by
    `package_from_mapping` (an unreadable error after the paid calls), so a pad without its
    printed number is a broken SHAPE that earns the one corrective retry, never the pad «None»."""
    with pytest.raises(ext.AnswerShapeError, match="elementi 1"):
        single_answer_to_data(_single(None))


def test_a_real_pad_number_is_kept():
    numbers = [pin["number"] for pin in single_answer_to_data(_single(" A1 "))["package_geometry"]["pins"]]
    assert numbers == ["A1", "2"]


def test_the_main_pads_skip_a_null_number():
    main, repeated = ext._main_pads([{"number": None, "x_mm": 0, "y_mm": 0},
                                     {"number": "1", "x_mm": 1, "y_mm": 0}], 1.0)
    assert main == {"1": (1.0, 0.0)} and "None" not in main and not repeated


# -- D: an undeclared unit is never millimetres ------------------------------------

CHAINS = [{"axis": "x", "stops": ["1", "2"], "values": [2.54]},
          {"axis": "y", "stops": ["1,2", "edge"], "values": [1.0]}]


@pytest.mark.parametrize("unit", [None, "cm", 25.4])
def test_a_pad_reading_without_a_declared_unit_is_discarded(tmp_path, unit):
    answer = {"contacts_view": "top", "chains": CHAINS, "notes": ""}
    if unit is not None:
        answer["drawing_unit"] = unit
    reading = read_pad_positions([_drawing(tmp_path)], model="m", call=Mock(return_value=answer))
    assert reading["pads"] == {} and reading["discarded"] == "unita' non dichiarata"


@pytest.mark.parametrize("unit", ["mm", "inch", "mil"])
def test_a_pad_reading_with_a_declared_unit_is_summed(tmp_path, unit):
    answer = {"contacts_view": "top", "chains": CHAINS, "notes": "", "drawing_unit": unit}
    reading = read_pad_positions([_drawing(tmp_path)], model="m", call=Mock(return_value=answer))
    assert "discarded" not in reading and set(reading["pads"]) == {"1", "2"}


def _main(unit):
    data = {"contacts_view": "top", "package_geometry": {"origin": "custom", "pins": [
        {"number": "1", "x_mm": 0.0, "y_mm": 0.0}, {"number": "2", "x_mm": 2.54, "y_mm": 0.0}]}}
    if unit is not None:
        data["drawing_unit"] = unit
    return data


def test_main_pads_without_a_declared_unit_block_with_the_reason():
    data = _main(None)
    apply_dedicated_pads(data, None)
    assert "unita' non dichiarata" in data["_pin_map_conflict"]


def test_main_pads_with_a_declared_unit_are_judged_on_the_readings():
    """Negative: with «mm» the block, if any, names the readings, not the unit."""
    data = _main("mm")
    apply_dedicated_pads(data, None)
    assert "unita' non dichiarata" not in data.get("_pin_map_conflict", "")


def test_model_chains_without_a_declared_unit_leave_the_model_coordinates():
    answer = dict(_single("1"), pad_chains=CHAINS)
    answer.pop("drawing_unit")
    data = single_answer_to_data(answer)
    assert "unita' non dichiarata" in data["notes"]
    assert [(p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]] == [
        (0.0, 0.0), (2.54, 0.0)]


# ── Review S7 (2026-11-03): pad 0, one rule for pin maps and pinouts ─────────

@pytest.mark.parametrize("pin", [0, "0", " 0 "])
def test_pad_zero_is_a_text_label_in_a_pin_map_and_a_pinout(pin):
    """Positive: pad 0 no longer invalidates a whole pin_map; it is the text «0», like the
    pinout keeps it."""
    entries = ext._pin_map_seen_from_top(
        [{"pin": pin, "row": 0, "col": 0}, {"pin": 1, "row": 0, "col": 1}], 2, "top", 1)
    assert [entry["pin"] for entry in entries] == ["0", 1]
    assert ext._validated_pinout([{"pin": pin, "name": "GND"}], [])[0]["pin"] in (0, "0")


def test_pad_zero_stays_outside_the_automatic_numbering():
    """Negative: «0» is a label, so without a map placing it it is not a populated pin of the
    automatic 1..rows x cols numbering, while «1» is."""
    notes = []
    assert ext._validated_populated_pins(["0", "1"], 4, notes, None) == [1]
    assert notes
    assert ext._validated_populated_pins(["0", "1"], 4, [], mapped=["0", 1]) == ["0", 1]
