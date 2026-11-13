"""@brief Pad-layout dead ends turned into a shape retry or ONE question (plan of 2026-11-13).

Two real cases (offline replays in the coordinator's scratchpad) showed three
dead ends of the single-part pad reading:
- P1: a declared grid that contradicts itself (a pin_map entry outside its own
  rows x cols, a printed populated pin no position carries) was numbered by
  convention and FILTERED by the printed numbers: one pad survived, silently.
  It is now a shape error with the one corrective retry, and a broken retry
  keeps every printed number without a position.
- P3: a main answer that invented a pad (a case-marking letter) or lost pads
  made the only complete, coherent reading ineligible, and the generation
  blocked. That reading is now offered in ONE question, never adopted alone.
- P4: a placed pad without a printed hole and without a terminal dropped the
  footprint with a remedy nobody could act on. It is now a question: the same
  printed hole as other pads (estimated), not a terminal (remove), or blocked.
Every positive test has a negative twin that differs for a structural reason.
No network: the model calls and the hole witness are stubbed.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import AnswerShapeError, single_answer_to_data


# ---------------------------------------------------------------------------
# P1 -- a self-contradicting grid is a shape error, never a 1-pad geometry
# ---------------------------------------------------------------------------

def _single(**extra):
    """A single-part answer with a grid and nothing else that decides the pads."""
    answer = dict(drawing_unit="mm", contacts_view="bottom", confidence="high",
                  body_length_mm=23.8, body_width_mm=13.7, lead_exit="bottom",
                  lead_diameter_mm=0.5, value_sources={"body_length_mm": "read"})
    answer.update(extra)
    return answer


#: A 2x2 grid whose map puts two printed pins at column 2: outside its own matrix.
CONTRADICTING_GRID = {
    "rows": 2, "cols": 2, "pitch_mm": 15.24, "row_spacing_mm": 10.16, "pad_diameter_mm": 1.5,
    "pin_map": [{"pin": "1", "row": 0, "col": 0}, {"pin": "7", "row": 0, "col": 1},
                {"pin": "8", "row": 0, "col": 2}, {"pin": "16", "row": 1, "col": 0},
                {"pin": "10", "row": 1, "col": 1}, {"pin": "9", "row": 1, "col": 2}],
    "populated_pins": ["1", "7", "8", "9", "10", "16"]}


def test_a_pin_map_entry_outside_the_grid_is_a_shape_error_with_every_printed_number_kept():
    with pytest.raises(AnswerShapeError, match="pad_chains") as caught:
        single_answer_to_data(_single(grid=dict(CONTRADICTING_GRID)))
    fallback = caught.value.fallback
    pins = fallback["package_geometry"]["pins"]
    # Never the 1-pad geometry of the convention filtered by the printed numbers.
    assert [p["number"] for p in pins] == ["1", "7", "8", "9", "10", "16"]
    assert all(p["x_mm"] is None and p["y_mm"] is None for p in pins), "no grid coordinate kept"
    assert "_numbers_by_convention" not in fallback


def test_a_printed_populated_pin_without_a_grid_position_is_a_shape_error():
    grid = {"rows": 2, "cols": 2, "pitch_mm": 2.54, "row_spacing_mm": 2.54,
            "populated_pins": ["1", "7", "8"]}
    with pytest.raises(AnswerShapeError, match="7, 8"):
        single_answer_to_data(_single(grid=grid))


def test_a_valid_grid_with_its_pin_map_keeps_its_pads():
    """Negative: every entry inside rows x cols, every populated pin placed."""
    grid = {"rows": 2, "cols": 3, "pitch_mm": 2.54, "row_spacing_mm": 7.62, "pad_diameter_mm": 1.5,
            "pin_map": [{"pin": "1", "row": 0, "col": 0}, {"pin": "2", "row": 0, "col": 1},
                        {"pin": "3", "row": 0, "col": 2}, {"pin": "6", "row": 1, "col": 0},
                        {"pin": "5", "row": 1, "col": 1}, {"pin": "4", "row": 1, "col": 2}],
            "populated_pins": ["1", "2", "3", "4", "5", "6"]}
    data = single_answer_to_data(_single(grid=grid))
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert sorted(pins) == ["1", "2", "3", "4", "5", "6"]
    assert pins["3"][0] - pins["1"][0] == pytest.approx(5.08)
    assert pins["6"][1] - pins["1"][1] == pytest.approx(7.62)
    assert "_numbers_by_convention" not in data


def test_a_grid_without_pin_map_whose_populated_pins_are_positions_keeps_the_convention():
    """Negative: populated_pins inside 1..rows*cols name occupied positions, as declared."""
    grid = {"rows": 2, "cols": 3, "pitch_mm": 2.54, "row_spacing_mm": 7.62, "pad_diameter_mm": 1.5,
            "populated_pins": [1, 2, 6]}
    data = single_answer_to_data(_single(grid=grid))
    assert sorted(p["number"] for p in data["package_geometry"]["pins"]) == ["1", "2", "6"]
    assert data["_numbers_by_convention"] is True


@pytest.fixture
def offline(monkeypatch):
    """No credit check, no key: the extraction never reaches a provider."""
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)


def _drawing(tmp_path):
    """A blank drawing file: the model calls are stubbed, the page is never read for holes."""
    from PIL import Image
    path = tmp_path / "quote.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


def test_a_grid_contradicting_itself_twice_keeps_the_paid_answer(offline, monkeypatch, tmp_path):
    """One corrective retry; when it is broken the same way, the printed numbers survive."""
    call = Mock(side_effect=[_single(grid=dict(CONTRADICTING_GRID)),
                             _single(grid=dict(CONTRADICTING_GRID))])
    monkeypatch.setattr(ext, "call_tool", call)
    lines = []
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]},
                                           progress=lines.append)
    assert call.call_count == 2
    assert call.call_args_list[1].kwargs["content_blocks"][-1] == {
        "type": "text", "text": ext._SHAPE_CORRECTION}
    assert "pad_chains" in ext._SHAPE_CORRECTION
    # Pads without positions: dropped as unread geometry, every printed number named.
    assert sorted(params.unread_pads, key=ext._pad_number_key) == ["1", "7", "8", "9", "10", "16"]
    assert params.pin_map_conflict


def test_a_well_formed_retry_replaces_the_contradicting_grid(offline, monkeypatch, tmp_path):
    """Negative: the retry answers in pads, and that answer is the one used."""
    pads = [{"number": n, "x_mm": x, "y_mm": 0.0, "pad_type": "thru_hole", "drill_mm": 0.8,
             "pad_length_mm": 1.5, "pad_width_mm": 1.5} for n, x in (("1", 0.0), ("2", 2.54))]
    call = Mock(side_effect=[_single(grid=dict(CONTRADICTING_GRID)), _single(pads=pads)])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]})
    assert call.call_count == 2
    assert [p["number"] for p in params.package_geometry["pins"]] == ["1", "2"]


# ---------------------------------------------------------------------------
# P3 -- the only complete, coherent reading is offered, never adopted alone
# ---------------------------------------------------------------------------

#: Five real terminals, page orientation (mm), an asymmetric relay-like layout.
TRUE = {"11": (0.0, 6.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0), "14": (14.2, 0.0), "12": (14.2, 12.0)}


def _verdict(status, reason, **extra):
    """A witness verdict double."""
    return SimpleNamespace(status=status, reason=reason, image="x", max_error_mm=0.05,
                           tolerance_mm=0.3, circles=5, **extra)


def _witness(confirmed=(), refused=None):
    """check_layout stub: confirms the given layouts, `refused` for the rest (default unverifiable)."""
    def check(pads, body):
        if any(pads == dict(layout) for layout in confirmed):
            return _verdict("confirmed", "confirmed")
        return refused or _verdict("unverifiable", "too_few_circles")
    return check


def _pin(number, x, y):
    return {"number": number, "x_mm": x, "y_mm": y, "pad_type": "thru_hole", "drill_mm": 1.3,
            "pad_length_mm": 2.2, "pad_width_mm": 2.2, "shape": "circle"}


def _data(pins, pinout=None):
    data = {"drawing_unit": "mm", "contacts_view": "bottom",
            "package_geometry": {"family": "unknown", "origin": "custom", "source_view": "top",
                                 "pins": pins, "body_length_mm": 22.5, "body_width_mm": 16.5}}
    if pinout is not None:
        data["pinout"] = pinout
    return data


def _reading(pads, **extra):
    reading = {"view": "unknown", "pads": dict(pads), "notes": "", "problems": [], "malformed": [],
               "span_quotes": {}, "unplaced": [], "spans": {"x": None, "y": None, "edges": {}},
               "holes": {}, "leads": {}, "references": {}}
    reading.update(extra)
    return reading


#: The main answer: the five pads misplaced, plus a sixth «C» nobody else names.
MAIN_WITH_C = [_pin("11", 12.2, 0.0), _pin("12", 12.2, 12.2), _pin("14", 12.2, 8.0),
               _pin("A1", 0.0, 0.0), _pin("A2", 0.0, 12.2), _pin("C", 2.05, 8.0)]
PINOUT = [{"pin": "A1", "name": "Coil+"}, {"pin": "A2", "name": "Coil-"}, {"pin": "11", "name": "NO"},
          {"pin": "12", "name": "NC"}, {"pin": "14", "name": "Common"}, {"pin": "C", "name": "Common"}]
NO_MATCH = _verdict("not_confirmed", "no_match", scale_observable=True)


def _pad_layout_questions(data):
    return [q for q in data.get("_questions") or () if q["field"] == "pad_layout"]


def test_an_unwitnessed_extra_pad_is_offered_for_removal_in_one_question():
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    log = []
    ext.apply_dedicated_pads(data, _reading(TRUE), log.append,
                             check_layout=_witness([TRUE], refused=NO_MATCH))
    questions = _pad_layout_questions(data)
    # CONTRACT CHANGED 2026-11-13 (coordinator round 3, (a)): a removal
    # question always also offers to keep the main layout.
    assert len(questions) == 1 and len(questions[0]["options"]) == 2
    assert questions[0]["options"][1]["value"].startswith("main:")
    assert "C" in questions[0]["text"] and "_pin_map_conflict" not in data
    # Never adopted alone: the main pads stay until the answer.
    assert sorted(p["number"] for p in data["package_geometry"]["pins"]) == sorted(
        ["11", "12", "14", "A1", "A2", "C"])
    payload = next(iter(data["_candidates"][questions[0]["key"]].values()))
    assert payload["remove"] == ["C"] and payload["add"] == []


def test_a_saved_answer_removes_the_pad_from_pins_and_pinout():
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE], refused=NO_MATCH))
    question = _pad_layout_questions(data)[0]
    answered = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    answered["_answers"] = {question["key"]: question["options"][0]["value"]}
    ext.apply_dedicated_pads(answered, _reading(TRUE),
                             check_layout=_witness([TRUE], refused=NO_MATCH))
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in answered["package_geometry"]["pins"]}
    assert sorted(pins) == ["11", "12", "14", "A1", "A2"]
    assert pins["14"][0] - pins["A1"][0] == pytest.approx(12.2)
    assert [e["pin"] for e in answered["pinout"]] == ["A1", "A2", "11", "12", "14"]
    assert "_pin_map_conflict" not in answered and not _pad_layout_questions(answered)


@pytest.mark.parametrize("witness_key, value", [
    ("unplaced", ["C"]),
    ("holes", {"C": {"drill_mm": 1.3}}),
    ("leads", {"C": {"lead": {"shape": "round", "diameter_mm": 0.8}}}),
])
def test_an_extra_pad_an_independent_witness_names_is_never_offered_for_removal(witness_key, value):
    """Negative (audit H1 spirit): a printed fact names the pad, so it is a terminal."""
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    reading = _reading(TRUE, **{witness_key: value})
    ext.apply_dedicated_pads(data, reading, check_layout=_witness([TRUE], refused=NO_MATCH))
    offered = [payload for options in (data.get("_candidates") or {}).values()
               for payload in options.values() if payload.get("remove")]
    assert not offered
    assert data.get("_pin_map_conflict")


def test_an_extra_pad_on_drawn_holes_is_never_offered_for_removal():
    """Negative: the drawn holes confirm the main layout that holds the extra pad."""
    main = {p["number"]: (p["x_mm"], p["y_mm"]) for p in MAIN_WITH_C}
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE, main]))
    offered = [payload for options in (data.get("_candidates") or {}).values()
               for payload in options.values() if payload.get("remove")]
    assert not offered


@pytest.mark.parametrize("refusal", [
    _verdict("not_confirmed", "distorted"),
    _verdict("not_confirmed", "body_scale"),
    _verdict("not_confirmed", "no_match", scale_observable=True),
])
def test_a_complete_reading_the_drawn_holes_refute_is_never_offered(refusal):
    """Negative: the only complete reading is contradicted by the holes -> the block stands."""
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness(refused=refusal))
    assert not _pad_layout_questions(data)
    assert data.get("_pin_map_conflict")


def test_an_unverifiable_witness_is_not_a_refutation():
    """Positive twin: «cannot tell» leaves the offer open (the question says why)."""
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    # A «cannot tell» reason the extractor has no special text for (e.g. a
    # plan view that cannot show the pads) is worded as not checkable.
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness(
        refused=_verdict("unverifiable", "pads_not_visible")))
    assert len(_pad_layout_questions(data)) == 1
    assert "ne' confermate ne' smentite" in _pad_layout_questions(data)[0]["text"]


def test_a_dedicated_reading_with_fewer_witnessed_pads_is_never_offered():
    """Negative (H1): the reading lacks a pad its own chains name -> it is incomplete, not the answer."""
    four = {n: xy for n, xy in TRUE.items() if n != "12"}
    data = _data([_pin(n, x, y) for n, (x, y) in TRUE.items()])
    ext.apply_dedicated_pads(data, _reading(four, unplaced=["12"]),
                             check_layout=_witness([four], refused=NO_MATCH))
    offered = [payload for options in (data.get("_candidates") or {}).values()
               for payload in options.values() if payload.get("remove")]
    assert not offered


def test_two_complete_readings_with_the_same_numbers_keep_the_choice_between_them():
    """Negative: same pad set on both sides -> today's rules (choice or block), no pad-set offer."""
    misread = {"11": (0.0, 5.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0), "14": (14.2, 0.0),
               "12": (14.2, 12.0)}
    data = _data([_pin(n, x, y) for n, (x, y) in misread.items()])
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness())
    for options in (data.get("_candidates") or {}).values():
        for payload in options.values():
            assert not payload.get("remove") and not payload.get("add")


def test_a_main_answer_without_positions_is_completed_by_the_offered_layout():
    """Positive: the TEL class -- printed numbers kept, no position: the reading places them."""
    data = _data([dict(_pin(n, None, None)) for n in TRUE])
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness())
    questions = _pad_layout_questions(data)
    assert len(questions) == 1 and "posizione" in questions[0]["text"]


# ---------------------------------------------------------------------------
# P3 + P4 through the result: apply_answers on the settled extraction
# ---------------------------------------------------------------------------

def _params(data):
    return ext._params_from_tool_arguments(data)


def test_apply_answers_writes_the_offered_layout_and_drops_the_extra_pad():
    data = _data([dict(p) for p in MAIN_WITH_C[:-1]] + [
        {"number": "C", "x_mm": 2.05, "y_mm": 8.0, "pad_type": "thru_hole",
         "pad_length_mm": 2.0, "pad_width_mm": 2.0, "shape": "circle"}], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE], refused=NO_MATCH))
    params = _params(data)
    # C has no hole: the footprint waits, and C is NOT asked about a second time.
    assert params.package_geometry is None
    assert len([q for q in params.questions if q["field"] == "pad_layout"]) == 1
    question = params.questions[0]
    applied = ext.apply_answers(params, {question["key"]: question["options"][0]["value"]})
    assert sorted(p["number"] for p in applied.package_geometry["pins"]) == [
        "11", "12", "14", "A1", "A2"]
    assert all(p["drill_mm"] == pytest.approx(1.3) for p in applied.package_geometry["pins"])
    assert "C" not in [e["pin"] for e in applied.pinout]
    assert not applied.questions and not applied.pin_map_conflict


def _holeless_c_data(pinout=PINOUT):
    """The main answer with C placed but without hole or terminal (the T7N shape)."""
    return _data([dict(p) for p in MAIN_WITH_C[:-1]] + [
        {"number": "C", "x_mm": 2.05, "y_mm": 8.0, "pad_type": "thru_hole",
         "pad_length_mm": 2.0, "pad_width_mm": 2.0, "shape": "circle"}], pinout)


def test_a_dropped_geometry_names_the_pads_no_witness_quotes():
    """Coordinator request: «no hole printed» told apart from «nothing printed names the pad»."""
    data = _holeless_c_data()
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE], refused=NO_MATCH))
    assert _params(data).unread_pad_geometry["unquoted_pads"] == ["C"]


def test_a_pad_a_printed_callout_names_is_not_unquoted():
    """Negative: a stop of the dedicated reading's chains names C (an unplaced stop).

    CONTRACT CHANGED 2026-11-13 (audit D4): a datum stop is a label, not a
    pad name, so the witness here is an unplaced chain stop named C.

    The pad still has no hole, so the geometry is still dropped: only the
    witness differs from the positive case.
    """
    data = _holeless_c_data()
    reading = _reading(TRUE, unplaced=["C"])
    ext.apply_dedicated_pads(data, reading, check_layout=_witness([TRUE], refused=NO_MATCH))
    dropped = _params(data).unread_pad_geometry
    assert dropped is not None and dropped["unquoted_pads"] == []


def test_without_a_dedicated_reading_nothing_is_called_unquoted():
    """Negative: no reading ran, so no witness could have named any pad: the key is absent."""
    data = _holeless_c_data()
    ext.apply_dedicated_pads(data, None)
    assert "unquoted_pads" not in (_params(data).unread_pad_geometry or {})


# -- audit C2: an added pad never borrows the other pads' hole ----------------------

#: Three contacts printed Ø1.5 in the main answer; the chains add two coil pads.
CONTACTS = {"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (10.0, 0.0)}
WITH_COIL = dict(CONTACTS, **{"4": (0.0, 8.0), "5": (10.0, 8.0)})


def _contacts_data():
    pins = [{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole", "drill_mm": 1.5,
             "pad_length_mm": 2.5, "pad_width_mm": 2.5, "shape": "circle",
             "value_sources": {"drill_mm": "read", "pad_size": "estimated"}}
            for n, (x, y) in CONTACTS.items()]
    return _data(pins)


def _coil_callout(*pads):
    """The dedicated reading's printed callout of the coil hole, naming `pads`."""
    return {pad: {"drill_mm": 1.3, "drill_length_mm": None, "text": "Ø1.3", "rounding_mm": 0.05}
            for pad in pads}


def _added_pad_result(callout_pads, saved=False):
    """Offer the 5-pad layout; answer it (saved during the extraction, or fresh afterwards)."""
    reading = _reading(WITH_COIL, holes=_coil_callout(*callout_pads))
    data = _contacts_data()
    ext.apply_dedicated_pads(data, reading, check_layout=_witness())
    question = _pad_layout_questions(data)[0]
    answer = {question["key"]: question["options"][0]["value"]}
    if saved:
        again = _contacts_data()
        again["_answers"] = answer
        ext.apply_dedicated_pads(again, reading, check_layout=_witness())
        return _params(again)
    return ext.apply_answers(_params(data), answer)


@pytest.mark.parametrize("saved", [False, True])
def test_an_added_pad_whose_printed_callout_names_it_takes_that_hole_as_read(saved):
    result = _added_pad_result(("4", "5"), saved=saved)
    pins = {p["number"]: p for p in result.package_geometry["pins"]}
    assert sorted(pins) == ["1", "2", "3", "4", "5"]
    for number in ("4", "5"):
        assert pins[number]["drill_mm"] == pytest.approx(1.3)
        assert pins[number]["sources"]["drill_mm"] == "read"
    assert pins["1"]["drill_mm"] == pytest.approx(1.5)


@pytest.mark.parametrize("saved", [False, True])
def test_an_added_pad_no_callout_names_never_borrows_a_hole(saved):
    """Negative: the callout names pad 4 only; pad 5 gets no hole, least of all Ø1.5 «read»."""
    result = _added_pad_result(("4",), saved=saved)
    assert result.package_geometry is None and result.unread_pads == ["5"]
    pins = {p["number"]: p for p in result.unread_pad_geometry["pins"]}
    assert pins["5"].get("drill_mm") is None
    assert "read" not in (pins["5"].get("sources") or {}).values()
    question = next(q for q in result.questions if q["field"] == "pad_layout")
    # The printed holes are offered as the user's choice (estimated), never applied.
    labels = [o["label"] for o in question["options"]]
    assert any("1.50" in label for label in labels) and any("1.30" in label for label in labels)


# -- audit C3: convention numbering never makes terminals vanish unseen --------------

#: A 2x4 grid numbered 1..8 by convention.
GRID_2X4 = [_pin(str(col * 2 + row + 1), col * 5.0, row * 8.0) for col in range(4) for row in range(2)]
#: The dedicated chains place six printed pads (two pads quoted only in another view are missing).
SIX = {"A": (0.0, 0.0), "B": (0.0, 8.0), "C": (5.0, 0.0), "D": (5.0, 8.0), "E": (10.0, 0.0),
       "F": (10.0, 8.0)}


def _convention_data():
    data = _data([dict(p) for p in GRID_2X4])
    data["_numbers_by_convention"] = True
    return data


def test_a_shorter_reading_under_convention_is_not_offered_without_the_holes():
    """Negative (H1 under convention): 8 positions, 6 read, witness cannot tell -> no offer."""
    data = _convention_data()
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_witness())
    assert not _pad_layout_questions(data)
    assert data.get("_pin_map_conflict")


def test_a_shorter_reading_the_holes_confirm_is_offered_naming_the_lost_positions():
    """Positive twin: the drawn holes confirm the 6-pad layout; the question says 2 positions go."""
    data = _convention_data()
    # CONTRACT CHANGED 2026-11-13 (audit D6): the confirmation must say it
    # left no drawn hole unmatched and did not rest on marks only.
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_counting_witness(SIX, 0, 0))
    questions = _pad_layout_questions(data)
    assert len(questions) == 1
    assert "2 delle 8 posizioni" in questions[0]["text"]


def _counting_witness(confirmed, unassigned, marks):
    """check_layout stub confirming `confirmed` and saying what the confirmation rested on (D6)."""
    def check(pads, body):
        if pads == dict(confirmed):
            return _verdict("confirmed", "confirmed", unassigned_refuting=unassigned,
                            marks_used=marks)
        return _verdict("unverifiable", "too_few_circles")
    return check


@pytest.mark.parametrize("unassigned, marks, label", [
    (2, 0, "an unmatched drawn hole may be a lost terminal"),
    (0, 6, "every matched target was a centre-line mark"),
])
def test_a_shorter_reading_confirmed_on_weak_evidence_is_not_offered(unassigned, marks, label):
    """Negative (D6): the confirmation leaves holes unexplained, or rests on marks only."""
    data = _convention_data()
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_counting_witness(SIX, unassigned,
                                                                                marks))
    assert not _pad_layout_questions(data), label
    assert "non proposta" in data["_pin_map_conflict"]


def test_a_witness_that_does_not_say_what_it_matched_cannot_justify_lost_positions():
    """Negative (D6, conservative default): no `unassigned_refuting`/`marks_used` -> refused."""
    data = _convention_data()
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_witness([SIX]))
    assert not _pad_layout_questions(data)


# -- D1: under convention each pad keeps what its POSITION printed --------------------

def _printed_grid(n_positions, drills=None):
    """A 2 x n/2 grid numbered by convention, every hole printed (Ø1.3, or `drills` per position)."""
    pins = []
    for col in range(n_positions // 2):
        for row in range(2):
            number = str(col * 2 + row + 1)
            pin = _pin(number, col * 5.0, row * 8.0)
            pin["drill_mm"] = (drills or {}).get(number, 1.3)
            pin["value_sources"] = {"drill_mm": "read"}
            pins.append(pin)
    data = _data(pins)
    data["_numbers_by_convention"] = True
    return data


@pytest.mark.parametrize("saved", [False, True])
def test_under_convention_each_read_pad_keeps_its_positions_printed_hole(saved):
    """D1: 8 printed positions, 6 read and confirmed -> 6 pads Ø1.3 «read», no dead end."""
    data = _printed_grid(8)
    witness = _counting_witness(SIX, 0, 0)
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=witness)
    question = _pad_layout_questions(data)[0]
    answer = {question["key"]: question["options"][0]["value"]}
    if saved:
        again = _printed_grid(8)
        again["_answers"] = answer
        ext.apply_dedicated_pads(again, _reading(SIX), check_layout=witness)
        result = _params(again)
    else:
        result = ext.apply_answers(_params(data), answer)
    pins = {p["number"]: p for p in result.package_geometry["pins"]}
    assert sorted(pins) == ["A", "B", "C", "D", "E", "F"]
    assert all(p["drill_mm"] == pytest.approx(1.3) and p["sources"]["drill_mm"] == "read"
               for p in pins.values())


def test_under_convention_a_position_carries_its_own_hole_not_a_neighbours():
    """Positive and structural: the two rows print DIFFERENT holes; each read pad keeps its row's.

    Six pads fit the eight positions two ways (shifted by one column), and
    both ways put every pad on a position of the same row: what it inherits
    does not depend on which way is the real one.
    """
    drills = {str(n): (1.0 if n % 2 else 1.6) for n in range(1, 9)}     # row 0 odd, row 1 even
    data = _printed_grid(8, drills)
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_counting_witness(SIX, 0, 0))
    question = _pad_layout_questions(data)[0]
    result = ext.apply_answers(_params(data), {question["key"]: question["options"][0]["value"]})
    pins = {p["number"]: p["drill_mm"] for p in result.package_geometry["pins"]}
    # SIX puts A, C, E on row 0 and B, D, F on row 1.
    assert pins == pytest.approx({"A": 1.0, "B": 1.6, "C": 1.0, "D": 1.6, "E": 1.0, "F": 1.6})


def test_under_convention_an_ambiguous_position_inherits_nothing():
    """Negative: the columns print different holes and the six pads fit two column shifts.

    Which positions are the real ones is unknown, so no hole is inherited:
    the pads are asked about, and the question offers every printed hole of
    the part -- those of the positions removed included (D1).
    """
    drills = {str(c * 2 + r + 1): (1.0, 1.2, 1.4, 1.6)[c] for c in range(4) for r in range(2)}
    data = _printed_grid(8, drills)
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_counting_witness(SIX, 0, 0))
    question = _pad_layout_questions(data)[0]
    result = ext.apply_answers(_params(data), {question["key"]: question["options"][0]["value"]})
    assert result.package_geometry is None
    assert sorted(result.unread_pads) == ["A", "B", "C", "D", "E", "F"]
    asked = next(q for q in result.questions if q["field"] == "pad_layout")
    labels = " ".join(o["label"] for o in asked["options"])
    assert all(f"{d:.2f}" in labels for d in (1.0, 1.2, 1.4, 1.6))


# -- D2: a body exchange never lets a holeless pad through ----------------------------

def test_an_added_holeless_pad_with_an_exchanged_body_is_held_and_asked():
    reading = _reading(WITH_COIL, holes=_coil_callout("4"))
    data = _contacts_data()
    ext.apply_dedicated_pads(data, reading, check_layout=_witness())
    question = _pad_layout_questions(data)[0]
    params = _params(data)
    value = question["options"][0]["value"]
    params.candidates[question["key"]][value]["exchange_body"] = {"measured_mm": [16.5, 22.5],
                                                                  "evidence": "probe"}
    length, width = params.body_length_mm, params.body_width_mm
    result = ext.apply_answers(params, {question["key"]: value})
    assert result.package_geometry is None and result.unread_pads == ["5"]
    assert result.pin_map_conflict
    assert [q for q in result.questions if q["field"] == "pad_layout"]
    assert (result.body_length_mm, result.body_width_mm) == (width, length)


# -- D3: saved and fresh answers give the added pad the same hole ----------------------

def _contacts_with_part_lead():
    data = _contacts_data()
    data["lead_diameter_mm"] = 0.8
    data["value_sources"] = {"lead_diameter_mm": "read"}
    return data


def _both_paths(make):
    reading = _reading(WITH_COIL, holes=_coil_callout("4"))
    data = make()
    ext.apply_dedicated_pads(data, reading, check_layout=_witness())
    question = _pad_layout_questions(data)[0]
    answer = {question["key"]: question["options"][0]["value"]}
    fresh = ext.apply_answers(_params(data), answer)
    again = make()
    again["_answers"] = answer
    ext.apply_dedicated_pads(again, reading, check_layout=_witness())
    return fresh, _params(again)


def _pad5(result):
    geometry = result.package_geometry or result.unread_pad_geometry
    return next(p for p in geometry["pins"] if p["number"] == "5")


def test_saved_and_fresh_answers_derive_the_added_pad_from_the_part_lead_alike():
    """Holed pads share one drill: the part-level terminal stands for pad 5 on both paths."""
    fresh, saved = _both_paths(_contacts_with_part_lead)
    assert fresh.package_geometry is not None and saved.package_geometry is not None
    assert _pad5(fresh)["drill_mm"] == pytest.approx(_pad5(saved)["drill_mm"])
    assert _pad5(fresh)["sources"]["drill_mm"] == _pad5(saved)["sources"]["drill_mm"] != "read"


def test_with_different_drills_the_part_lead_stands_for_no_added_pad_on_either_path():
    """Negative: pad 1 Ø1.2, pads 2-3 Ø1.5: the one terminal cannot say whose it is."""
    def make():
        data = _contacts_with_part_lead()
        data["package_geometry"]["pins"][0]["drill_mm"] = 1.2
        return data
    fresh, saved = _both_paths(make)
    for result in (fresh, saved):
        assert result.package_geometry is None and _pad5(result).get("drill_mm") is None


# -- D4: a reference stop witnesses a pad by position, never by its label -------------

#: The five pads where the dedicated reading puts them, plus C at (6, 3).
MAIN_AGREEING_WITH_C = [_pin(n, x, y) for n, (x, y) in TRUE.items()] + [_pin("C", 6.0, 3.0)]


@pytest.mark.parametrize("label", ["C", "M1"])
def test_a_reference_stop_on_the_extra_pads_position_witnesses_it(label):
    """The datum label does not matter; its position on pad C does (C is not offered for removal)."""
    data = _data([dict(p) for p in MAIN_AGREEING_WITH_C], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE, references={label: (6.0, 3.0)}),
                             check_layout=_witness())
    offered = [payload for options in (data.get("_candidates") or {}).values()
               for payload in options.values() if payload.get("remove")]
    assert not offered


def test_a_reference_stop_named_like_the_pad_elsewhere_witnesses_nothing():
    """Negative (D4): datum «C» far from pad C -> C is still offered for removal."""
    data = _data([dict(p) for p in MAIN_AGREEING_WITH_C], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE, references={"C": (30.0, 30.0)}),
                             check_layout=_witness())
    offered = [payload for options in (data.get("_candidates") or {}).values()
               for payload in options.values() if payload.get("remove") == ["C"]]
    assert offered


# -- (a) keep option, (c) reasons in the block, self-quoted pads ------------------------

def test_keeping_the_main_layout_asks_about_its_holeless_pad_instead_of_removing_it():
    data = _holeless_c_data()
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE], refused=NO_MATCH))
    params = _params(data)
    question = params.questions[0]
    keep = question["options"][1]["value"]
    result = ext.apply_answers(params, {question["key"]: keep})
    assert result.package_geometry is None
    assert "C" in [e["pin"] for e in result.pinout]
    asked = [q for q in result.questions if q["field"] == "pad_layout"]
    assert len(asked) == 1 and asked[0]["key"].startswith("pad_layout:pad_hole:C=")


def test_views_that_differ_are_named_when_the_offer_is_withheld():
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    reading = _reading(TRUE)
    reading["view"] = "top"            # the main answer declares bottom
    ext.apply_dedicated_pads(data, reading, check_layout=_witness())
    assert "viste diverse" in (data.get("_pin_map_conflict") or data.get("_numbering_note"))


def test_pads_named_only_by_the_main_answers_own_chains_are_reported_apart():
    """C named only by the main answer's chain sums: self-quoted, not unquoted; negative: none."""
    data = _holeless_c_data()
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE], refused=NO_MATCH))
    dropped = _params(data).unread_pad_geometry
    assert dropped["self_quoted_pads"] == [] and dropped["unquoted_pads"] == ["C"]
    independent, own = ext._pad_witnesses(_reading(TRUE), {"pads": {"C": (0.0, 0.0)}},
                                          {"C": (2.05, 8.0)}, 0.1)
    assert "c" in own and "c" not in independent


def _hole_data(holed_like=True):
    """Three placed pads: 1 and 2 with a printed hole (or none at all), 3 without hole or terminal."""
    pins = []
    for number, x in (("1", 0.0), ("2", 5.08), ("3", 10.16)):
        pin = {"number": number, "x_mm": x, "y_mm": 0.0, "pad_type": "thru_hole",
               "pad_length_mm": 2.0, "pad_width_mm": 2.0, "shape": "circle"}
        if holed_like and number != "3":
            pin["drill_mm"] = 1.0
            pin["value_sources"] = {"drill_mm": "read", "pad_size": "estimated"}
        pins.append(pin)
    return {"drawing_unit": "mm", "contacts_view": "top",
            "pinout": [{"pin": "1", "name": "A"}, {"pin": "2", "name": "B"}, {"pin": "3", "name": "C"}],
            "package_geometry": {"family": "unknown", "origin": "custom", "source_view": "top",
                                 "pins": pins}}


def test_a_placed_pad_without_hole_asks_the_printed_hole_of_its_part():
    params = _params(_hole_data())
    assert params.package_geometry is None
    question = next(q for q in params.questions if q["field"] == "pad_layout")
    labels = [o["label"] for o in question["options"]]
    assert any("1, 2" in label and "1.00" in label for label in labels)
    assert len(labels) == 3            # same hole, remove, leave blocked
    same = question["options"][0]["value"]
    applied = ext.apply_answers(params, {question["key"]: same})
    pins = {p["number"]: p for p in applied.package_geometry["pins"]}
    assert pins["3"]["drill_mm"] == pytest.approx(1.0)
    assert pins["3"]["sources"]["drill_mm"] == "estimated"
    # The same answer saved with the project applies during a new extraction.
    again = _hole_data()
    again["_answers"] = {question["key"]: same}
    params_again = _params(again)
    assert params_again.package_geometry is not None
    assert not [q for q in params_again.questions if q["field"] == "pad_layout"]


def test_the_remove_option_drops_the_pad_and_its_pinout_entry():
    params = _params(_hole_data())
    question = next(q for q in params.questions if q["field"] == "pad_layout")
    remove = next(o["value"] for o in question["options"] if o["value"].endswith("=remove"))
    applied = ext.apply_answers(params, {question["key"]: remove})
    assert [p["number"] for p in applied.package_geometry["pins"]] == ["1", "2"]
    assert [e["pin"] for e in applied.pinout] == ["1", "2"]


def test_without_any_printed_hole_no_value_is_offered():
    """Negative: nothing printed in the part -> never a number.

    CONTRACT CHANGED 2026-11-13 (fourth re-audit, M3): here every pad of the
    part lacks a hole, so «remove» would leave no footprint and is not
    offered either: only «leave blocked» remains.
    """
    params = _params(_hole_data(holed_like=False))
    question = next(q for q in params.questions if q["field"] == "pad_layout")
    values = [o["value"] for o in question["options"]]
    assert len(values) == 1 and values[0].endswith("=block")
    applied = ext.apply_answers(params, {question["key"]: values[0]})
    assert applied.package_geometry is None and applied.pin_map_conflict


# ---------------------------------------------------------------------------
# P5 -- messages name both pad sets in natural order
# ---------------------------------------------------------------------------

def test_pad_summaries_use_the_natural_pad_order():
    text = ext._pad_summary({"10": (0.0, 0.0), "9": (1.0, 0.0), "A1": (2.0, 0.0), "1": (3.0, 0.0)})
    assert [entry.split("@")[0] for entry in text.split(", ")] == ["1", "9", "10", "A1"]


def test_the_few_common_pads_message_names_both_sets():
    data = _data([_pin("1", 0.0, 0.0)])
    ext.apply_dedicated_pads(data, _reading({"1": (0, 0), "10": (5, 0), "9": (10, 0)}),
                             check_layout=_witness(refused=NO_MATCH))
    text = data["_pin_map_conflict"]
    assert "(1, 9, 10)" in text and "principale (1)" in text


# -- Hole verdict on every option, options ordered by it (user, 2026-11-13) ----------

#: The main answer reads the five terminals at other positions (same numbers).
MISREAD_FIVE = {"11": (0.0, 5.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0), "14": (14.2, 0.0),
                "12": (14.2, 12.0)}


def _two_layout_question(witness):
    """Main (MISREAD_FIVE) against dedicated (TRUE), same pads: the plain choice of `_block_or_ask`."""
    data = _data([_pin(n, x, y) for n, (x, y) in MISREAD_FIVE.items()])
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=witness)
    return data, _pad_layout_questions(data)


def test_a_confirmed_layout_comes_first_and_says_so():
    """Positive: dedicated confirmed, main not checkable -> dedicated first, labelled confirmed."""
    def witness(pads, body):
        if pads == dict(TRUE):
            return _verdict("confirmed", "confirmed", unassigned_refuting=0, marks_used=0)
        return _verdict("unverifiable", "pads_not_visible")
    data, questions = _two_layout_question(witness)
    # A main reading the holes cannot check does not lose: no adoption, a question.
    assert len(questions) == 1
    options = questions[0]["options"]
    assert options[0]["value"].startswith("dedicated:")
    assert "coincidono" in options[0]["label"]
    assert "ne' confermate ne' smentite" in options[1]["label"]
    # The provisional pads stay the main answer's, and the note says which
    # option the holes confirm.
    assert "confermano invece: lettura dedicata" in data["_numbering_note"]


def test_two_layouts_the_holes_cannot_check_keep_todays_order_and_both_say_so():
    """Negative: both not checkable -> the readings' order (main first), both labelled."""
    data, questions = _two_layout_question(_witness(refused=_verdict("unverifiable",
                                                                     "pads_not_visible")))
    options = questions[0]["options"]
    assert options[0]["value"].startswith("main:") and options[1]["value"].startswith("dedicated:")
    assert all("ne' confermate ne' smentite" in option["label"] for option in options)


def test_a_confirmation_on_marks_only_is_labelled_as_such():
    """A confirmation whose every matched target is a centre-line mark says it rests on marks."""
    verdict = _verdict("confirmed", "confirmed", unassigned_refuting=0, marks_used=len(TRUE))
    assert "solo sui segni sugli assi" in ext._hole_text(verdict, dict(TRUE))
    # Negative: rings matched (no mark), the label does not say it.
    rings = _verdict("confirmed", "confirmed", unassigned_refuting=0, marks_used=0)
    assert "segni sugli assi" not in ext._hole_text(rings, dict(TRUE))


def test_a_saved_answer_still_applies_after_the_reorder():
    """The question key is built from the sorted values: reordering never orphans an answer."""
    unverifiable = _witness(refused=_verdict("unverifiable", "pads_not_visible"))
    data, questions = _two_layout_question(unverifiable)
    question = questions[0]
    dedicated = next(o["value"] for o in question["options"] if o["value"].startswith("dedicated:"))
    assert question["key"] == ext.question_key("pad_layout", [o["value"] for o in
                                                              reversed(question["options"])])
    again = _data([_pin(n, x, y) for n, (x, y) in MISREAD_FIVE.items()])
    again["_answers"] = {question["key"]: dedicated}
    ext.apply_dedicated_pads(again, _reading(TRUE), check_layout=unverifiable)
    assert not _pad_layout_questions(again)
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in again["package_geometry"]["pins"]}
    assert pins["11"][1] - pins["A1"][1] == pytest.approx(6.0)


# -- Third re-audit (2026-11-13): positions, never same-numbered pins ---------------

#: Columns of the 2x4 grid drilled differently (printed «read»).
COLUMN_DRILLS = {str(c * 2 + r + 1): (1.0, 1.2, 1.4, 1.6)[c] for c in range(4) for r in range(2)}


def _grid_answered(dedicated, saved):
    """Offer `dedicated` against the column-drilled 2x4 convention grid; answer the first option."""
    witness = _counting_witness(dedicated, 0, 0)
    data = _printed_grid(8, COLUMN_DRILLS)
    ext.apply_dedicated_pads(data, _reading(dedicated), check_layout=witness)
    question = _pad_layout_questions(data)[0]
    answer = {question["key"]: question["options"][0]["value"]}
    if saved:
        again = _printed_grid(8, COLUMN_DRILLS)
        again["_answers"] = answer
        ext.apply_dedicated_pads(again, _reading(dedicated), check_layout=witness)
        return question, _params(again)
    return question, ext.apply_answers(_params(data), answer)


@pytest.mark.parametrize("saved", [False, True])
def test_a_read_pad_never_takes_the_hole_of_a_same_numbered_convention_pin(saved):
    """Negative (item 1): six pads numbered 1..6 row by row over three columns.

    The numbers 1..6 also exist in the convention grid (column by column):
    pad «2» read at column 1 must not take grid pin «2»'s hole (column 0),
    and the six pads fit two column shifts with different holes, so no hole
    is inherited at all: the pads are asked about, never written «read».
    """
    six = {str(i + 1): (5.0 * (i % 3), 8.0 * (i // 3)) for i in range(6)}
    question, result = _grid_answered(six, saved)
    assert len(question["options"]) == 2        # «keep the main layout» too (item 3)
    geometry = result.package_geometry or result.unread_pad_geometry
    assert result.package_geometry is None
    assert all(p.get("drill_mm") is None for p in geometry["pins"])


def test_a_full_renumbering_under_convention_takes_each_positions_hole():
    """Positive (item 2): eight pads renumbered row by row, one translation -> each its column's hole."""
    eight = {str(i + 1): (5.0 * (i % 4), 8.0 * (i // 4)) for i in range(8)}
    data = _printed_grid(8, COLUMN_DRILLS)
    ext.apply_dedicated_pads(data, _reading(eight), check_layout=_counting_witness(eight, 0, 0))
    assert "_pin_map_conflict" not in data
    result = _params(data)
    pins = {p["number"]: p for p in result.package_geometry["pins"]}
    for number, (x, _y) in eight.items():
        assert pins[number]["drill_mm"] == pytest.approx((1.0, 1.2, 1.4, 1.6)[int(x // 5)])
        assert pins[number]["sources"]["drill_mm"] == "read"


def test_an_ambiguous_renumbering_with_different_holes_still_blocks():
    """Negative (item 2): six pads on two rows fit the grid two ways with different column holes.

    No replacement happens by itself (no answer is guessed): the main pads
    stay until the user answers the whole-layout question.
    """
    six = {str(i + 1): (5.0 * (i % 3), 8.0 * (i // 3)) for i in range(6)}
    data = _printed_grid(8, COLUMN_DRILLS)
    ext.apply_dedicated_pads(data, _reading(six), check_layout=_counting_witness(six, 0, 0))
    pins = data["package_geometry"]["pins"]
    assert len(pins) == 8, "nothing is replaced without the user's answer"


def test_an_unobservable_no_match_ranks_with_the_unverifiable():
    """Lower item: «no_match» on a layout that cannot show the scale is not a refutation."""
    collinear = {"1": (0.0, 0.0), "2": (2.54, 0.0), "3": (5.08, 0.0)}
    unobservable = _verdict("not_confirmed", "no_match", scale_observable=False)
    assert ext._verdict_rank(unobservable, collinear) == 1
    observable = _verdict("not_confirmed", "no_match", scale_observable=True)
    assert ext._verdict_rank(observable, dict(TRUE)) == 2


@pytest.mark.parametrize("reason", ["pads_not_visible", "low_resolution", "search_limit"])
def test_no_machine_code_reaches_the_users_text(reason):
    """Lower item: a «cannot tell» verdict is worded, never shown as its code."""
    text = ext._hole_text(_verdict("unverifiable", reason))
    assert reason not in text and "ne' confermate ne' smentite" in text


def test_a_withheld_offer_says_why_when_the_holes_refute_it():
    """Lower item: the refusal by the drawn holes is named in the block."""
    data = _data([dict(p) for p in MAIN_WITH_C], PINOUT)
    ext.apply_dedicated_pads(data, _reading(TRUE),
                             check_layout=_witness(refused=_verdict("not_confirmed", "distorted")))
    assert "smentiscono la lettura dedicata" in data["_pin_map_conflict"]


def test_a_witness_that_did_not_compute_its_counts_cannot_justify_lost_positions():
    """Lower item: `unassigned_refuting`/`marks_used` None means «not computed»: refused."""
    data = _convention_data()
    def witness(pads, body):
        if pads == dict(SIX):
            return _verdict("confirmed", "confirmed", unassigned_refuting=None, marks_used=None)
        return _verdict("unverifiable", "too_few_circles")
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=witness)
    assert not _pad_layout_questions(data)


# -- Fourth re-audit (2026-11-13): no silent block, no silent part terminal ---------

def _relay_missing_a2():
    """Numbers read on both sides; the main answer lacks A2; coil Ø0.8 and contacts Ø1.3 printed."""
    main = [_pin("11", 0.0, 6.0), _pin("14", 14.2, 0.0), _pin("12", 14.2, 12.0), _pin("A1", 2.0, 0.0)]
    main[3]["drill_mm"] = 0.8
    for pin in main:
        pin["value_sources"] = {"drill_mm": "read"}
    return _data(main)


def test_a_confirmed_complete_reading_beside_mixed_holes_keeps_each_pads_own_hole():
    """H1 case A: never a block without a question; each read pad keeps its own printed hole.

    The added pad A2 borrows nothing: it is asked about, with every printed
    hole of the part offered (Ø0.80 of the coil pad, Ø1.30 of the contacts).
    """
    data = _relay_missing_a2()
    ext.apply_dedicated_pads(data, _reading(TRUE), check_layout=_witness([TRUE], refused=NO_MATCH))
    assert "_pin_map_conflict" not in data
    result = _params(data)
    pins = {p["number"]: p for p in result.unread_pad_geometry["pins"]}
    assert pins["A1"]["drill_mm"] == pytest.approx(0.8)
    assert pins["11"]["drill_mm"] == pytest.approx(1.3)
    assert pins["A2"].get("drill_mm") is None
    asked = next(q for q in result.questions if q["field"] == "pad_layout")
    labels = " ".join(o["label"] for o in asked["options"])
    assert "0.80" in labels and "1.30" in labels


def test_a_confirmed_complete_reading_beside_uniform_holes_is_written_at_once():
    """Negative twin: one printed hole for every pad -> A2 still borrows nothing, but no other change."""
    data = _relay_missing_a2()
    data["package_geometry"]["pins"][3]["drill_mm"] = 1.3
    ext.apply_dedicated_pads(data, _reading(TRUE, holes={"A2": {"drill_mm": 1.3,
                                                                "drill_length_mm": None,
                                                                "text": "1.3", "rounding_mm": 0.05}}),
                             check_layout=_witness([TRUE], refused=NO_MATCH))
    result = _params(data)
    pins = {p["number"]: p for p in result.package_geometry["pins"]}
    assert sorted(pins) == ["11", "12", "14", "A1", "A2"]
    assert pins["A2"]["drill_mm"] == pytest.approx(1.3) and pins["A2"]["sources"]["drill_mm"] == "read"


def test_a_convention_grid_that_matches_no_position_is_asked_not_blocked():
    """H1 case B: wrong pitch, per-column holes, no matching -> the whole-layout question."""
    pins = []
    for col in range(3):
        for row in range(2):
            pin = _pin(str(col * 2 + row + 1), col * 4.0, row * 8.0)
            pin["drill_mm"] = (1.0, 1.2, 1.4)[col]
            pin["value_sources"] = {"drill_mm": "read"}
            pins.append(pin)
    data = _data(pins)
    data["_numbers_by_convention"] = True
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_witness([SIX], refused=NO_MATCH))
    assert "_pin_map_conflict" not in data
    assert len(_pad_layout_questions(data)) == 1


@pytest.mark.parametrize("saved", [False, True])
def test_the_part_terminal_never_replaces_printed_holes_silently(saved):
    """H2: printed holes the read pads cannot be matched to -> asked, never Ø from the part lead."""
    def make():
        data = _printed_grid(8, COLUMN_DRILLS)
        data["lead_diameter_mm"] = 0.8
        data["value_sources"] = {"lead_diameter_mm": "read"}
        return data
    witness = _counting_witness(SIX, 0, 0)
    data = make()
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=witness)
    question = _pad_layout_questions(data)[0]
    answer = {question["key"]: question["options"][0]["value"]}
    if saved:
        again = make()
        again["_answers"] = answer
        ext.apply_dedicated_pads(again, _reading(SIX), check_layout=witness)
        result = _params(again)
    else:
        result = ext.apply_answers(_params(data), answer)
    assert result.package_geometry is None
    assert all(p.get("drill_mm") is None for p in result.unread_pad_geometry["pins"])
    asked = next(q for q in result.questions if q["field"] == "pad_layout")
    assert sum("stampato" in o["label"] for o in asked["options"]) == 4


def test_renumbered_convention_positions_are_not_called_removed_terminals():
    """M2: a full renumbering names the convention numbers as replaced, not as removed pads."""
    data = _printed_grid(8, COLUMN_DRILLS)
    data["pinout"] = [{"pin": str(n), "name": f"P{n}"} for n in range(1, 9)]
    six = dict(SIX)
    ext.apply_dedicated_pads(data, _reading(six), check_layout=_counting_witness(six, 0, 0))
    question = _pad_layout_questions(data)[0]
    result = ext.apply_answers(_params(data), {question["key"]: question["options"][0]["value"]})
    assert "sostituiti dai numeri letti" in result.notes
    assert "tolti per scelta dell'utente" not in result.notes


def test_the_hole_question_never_offers_to_remove_every_pad():
    """M3: every pad of the part without a hole -> no «remove» option (an empty footprint)."""
    every = _params(_hole_data(holed_like=False))          # pads 1, 2, 3 all without hole
    question = next(q for q in every.questions if q["field"] == "pad_layout")
    assert not any(o["value"].endswith("=remove") for o in question["options"])
    # Negative twin: only pad 3 lacks a hole -> removing it is offered.
    some = _params(_hole_data())
    question = next(q for q in some.questions if q["field"] == "pad_layout")
    assert any(o["value"].endswith("=remove") for o in question["options"])


def test_the_loss_question_says_only_what_the_witness_checked():
    """H3: the question says «no unmatched hole near the layout», never that every hole was checked."""
    data = _convention_data()
    ext.apply_dedicated_pads(data, _reading(SIX), check_layout=_counting_witness(SIX, 0, 0))
    text = _pad_layout_questions(data)[0]["text"]
    assert "vicino alla disposizione letta" in text and "non sono controllate" in text
