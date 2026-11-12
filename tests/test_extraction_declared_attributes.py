"""@brief Declared attributes instead of numeric coincidences (review of 2026-11-04, blocks A-C).

Every rule that broke on one part (T7N, SRD, HLK) encoded a coincidence of
value or a drafting convention where the reader could DECLARE a fact: the
role of a printed profile quote, the kind of a chain, the anchor of a quote
at a pad's edge, a stop that is a datum or another outline, the printed text
of a quote, the qualifier of a body size, the side of a centreline's outline
stop. These tests pair a positive and a negative case that differ in the
declaration, never in a tuned number. The drawings are invented.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    _chain_contradictions, _height_candidate, apply_answers, apply_dedicated_body,
    apply_dedicated_pads, pads_from_chains, printed_quotes, quote_rounding, question_key)

from test_pad_dimension_chains import unresolved  # noqa: E402
from test_single_component_prompt import _CHAINS_ANSWER, _drawing, _single, offline  # noqa: F401


def _quotes(*pairs):
    return [{"value": value, "measures": role} for value, role in pairs]


def _main(**sizes):
    data = {"drawing_unit": "mm", "package_geometry": {"origin": "body_center", "pins": []},
            "value_sources": {"body_height_mm": "read"}}
    for key, value in sizes.items():
        data[key] = value
        if key != "pin_length_3d_mm":
            data["package_geometry"][key] = value
    return data


# ── A1: the role of a printed quote ──────────────────────────────────────

def test_printed_quotes_accept_objects_with_roles_and_bare_numbers():
    quotes = printed_quotes([{"value": 16.9, "measures": "body_height"}, 0.4,
                             {"value": 3.5, "measures": "nonsense"}, "x", -1], 1.0)
    assert quotes == [(16.9, "body_height"), (0.4, "other"), (3.5, "other")]


def test_a_quote_declared_body_height_supports_the_height_whatever_its_value():
    """H1: a square-section body (height equal to the plan width) is a declared fact."""
    quotes = [(10.0, "body_height"), (10.0, "plan"), (0.4, "standoff")]
    assert _height_candidate(10.0, quotes, others=[10.0], tolerance=0.1)


def test_a_quote_declared_a_plan_size_never_supports_the_height():
    """Negative twin: the same number declared «plan» only (rotated view) gives no support."""
    quotes = [(10.0, "plan"), (0.4, "standoff")]
    assert not _height_candidate(10.0, quotes, others=[], tolerance=0.1)


def test_without_any_role_the_former_value_rules_still_apply():
    """Declared fallback for cached answers: bare numbers equal to a plan size are excluded."""
    assert not _height_candidate(10.0, [10.0, 0.4], others=[10.0], tolerance=0.1)
    assert _height_candidate(10.0, [10.0, 0.4], others=[], tolerance=0.1)


def test_a_stepped_body_height_equal_to_two_other_quotes_is_supported_when_roles_are_declared():
    """H3: 12.5 = 9.5 + 3.0 is a printed height, not a sum, when the addends are plain quotes."""
    quotes = [(12.5, "body_height"), (9.5, "other"), (3.0, "other")]
    assert _height_candidate(12.5, quotes, others=[], tolerance=0.1)


def test_a_sum_with_a_declared_standoff_or_lead_is_still_a_computation():
    """Negative twin: 17.3 = 16.9 + 0.4 (standoff) is the overall figure, not the body."""
    quotes = [(17.3, "other"), (16.9, "body_height"), (0.4, "standoff")]
    assert not _height_candidate(17.3, quotes, others=[], tolerance=0.1)
    assert _height_candidate(16.9, quotes, others=[], tolerance=0.1)


def test_a_lead_length_is_looked_for_as_a_lead_not_as_a_height():
    quotes = [(3.5, "lead"), (16.9, "body_height")]
    assert _height_candidate(3.5, quotes, [], 0.1, field="pin_length_3d_mm")
    assert not _height_candidate(16.9, quotes, [], 0.1, field="pin_length_3d_mm")
    assert not _height_candidate(3.5, quotes, [], 0.1, field="body_height_mm")


def test_a_missing_main_height_equal_to_the_plan_width_is_not_adopted_without_support():
    """H1 adoption path (HLK: main mute, dedicated 23 = plan width, bare lists)."""
    data = _main(body_length_mm=38.0, body_width_mm=23.0)
    data["printed_vertical_quotes"] = _quotes((5.0, "standoff"), (18.0, "body_height"), (23.0, "plan"))
    logs = []
    apply_dedicated_body(data, {"values": {"body_height": 23.0}, "printed": _quotes((23.0, "plan")),
                                "notes": ""}, logs.append)
    assert data.get("body_height_mm") is None and "body_height_mm" not in data["package_geometry"]
    assert any("non e' sostenuta da nessuna quota stampata" in line for line in logs)
    # Positive twin: the same value declared the body height by the reader is adopted.
    data = _main(body_length_mm=38.0, body_width_mm=23.0)
    data["printed_vertical_quotes"] = _quotes((23.0, "body_height"))
    apply_dedicated_body(data, {"values": {"body_height": 23.0}, "printed": _quotes((23.0, "body_height")),
                                "notes": ""})
    assert data["body_height_mm"] == pytest.approx(23.0)


def test_a_declared_body_height_equal_to_the_width_is_confirmed_twice_without_a_question():
    """Square section declared: two lists print 10 as body_height -> confirmed, no question."""
    data = _main(body_length_mm=10.0, body_width_mm=10.0, body_height_mm=10.0)
    data["printed_vertical_quotes"] = _quotes((10.0, "body_height"))
    apply_dedicated_body(data, {"values": {"body_height": 10.0}, "printed": _quotes((10.0, "body_height")),
                                "notes": ""})
    assert data["body_height_mm"] == 10.0 and not data.get("_questions")


def test_a_declared_role_does_not_close_the_question_when_a_reader_saw_the_leads_sideways():
    """Negative (generality audit 2026-11-05, M6): the same declared body_height equal to a plan size,
    but one reader says the leads point LEFT (rotated side view): the role no longer settles it, the
    square-section-or-rotated-view question is asked."""
    data = _main(body_length_mm=38.0, body_width_mm=23.0, body_height_mm=23.0)
    data["printed_vertical_quotes"] = _quotes((23.0, "body_height"))
    apply_dedicated_body(data, {"values": {"body_height": 23.0}, "printed": _quotes((23.0, "body_height")),
                                "notes": "", "leads_point": "left"})
    question, = data["_questions"]
    assert question["field"] == "height_equals_plan" and "vista ruotata" in question["text"]


def test_a_quote_without_a_role_is_excluded_by_value_even_in_a_mixed_list():
    """Negative (generality audit 2026-11-05, M3): a 23 with no role next to a declared 18 is still a
    bare number equal to the width: excluded. Positive twin: the same 23 declared body_height passes."""
    mixed = [(23.0, "other"), (18.0, "body_height")]
    assert not _height_candidate(23.0, mixed, others=[23.0], tolerance=0.1)
    assert _height_candidate(18.0, mixed, others=[23.0], tolerance=0.1)
    assert _height_candidate(23.0, [(23.0, "body_height"), (18.0, "other")], others=[23.0], tolerance=0.1)


def test_two_body_lists_of_one_model_give_one_point_at_most():
    """A2 (generality audit 2026-11-05): the main list and the dedicated list both print 19.3 as the
    height. From two models that is «read twice» (written); from ONE model it is one witness: the
    disagreement with the dedicated value stays a conflict (dropped, question recorded)."""
    for same_model, expected in ((False, 19.3), (True, None)):
        data = _main(body_length_mm=22.5, body_width_mm=16.5, body_height_mm=19.3)
        data["value_sources"]["body_height_mm"] = "read"
        data["printed_vertical_quotes"] = _quotes((19.3, "body_height"))
        apply_dedicated_body(data, {"values": {"body_height": 16.9},
                                    "printed": _quotes((19.3, "body_height"), (16.9, "other")),
                                    "notes": ""}, same_model=same_model)
        assert data["body_height_mm"] == expected, same_model
    assert "stesso modello" in data["notes"]


# ── A5: height agreed on a plan size -> a question, not a silent confirmation ──

def test_two_readings_agreeing_on_a_plan_size_ask_square_section_or_rotated_view():
    data = _main(body_length_mm=38.0, body_width_mm=23.0, body_height_mm=23.0, leads_point="left")
    data["printed_vertical_quotes"] = [23.0, 5.0]
    apply_dedicated_body(data, {"values": {"body_height": 23.0}, "printed": [23.0, 5.0], "notes": "",
                                "leads_point": "left"})
    question, = data["_questions"]
    assert question["field"] == "height_equals_plan" and "vista ruotata" in question["text"]
    assert [o["value"] for o in question["options"]] == ["keep", "clear"]
    assert question["key"] == question_key("height_equals_plan", [23.0])
    assert data["body_height_mm"] == 23.0, "kept until the answer, said unconfirmed"
    # A saved answer «clear» removes the height at once.
    data = _main(body_length_mm=38.0, body_width_mm=23.0, body_height_mm=23.0)
    data["printed_vertical_quotes"] = [23.0, 5.0]
    data["_answers"] = {question["key"]: "clear"}
    apply_dedicated_body(data, {"values": {"body_height": 23.0}, "printed": [23.0, 5.0], "notes": ""})
    assert data["body_height_mm"] is None and not data.get("_questions")


def test_apply_answers_keeps_or_clears_the_height_equal_to_the_plan():
    params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 body_length_mm=38.0, body_width_mm=23.0, body_height_mm=23.0,
                                 package_geometry={"pins": [], "body_height_mm": 23.0},
                                 value_sources={"body_height_mm": "read"},
                                 questions=[{"key": "height_equals_plan:23.0", "field": "height_equals_plan",
                                             "text": "?", "options": [{"label": "k", "value": "keep"},
                                                                      {"label": "c", "value": "clear"}],
                                             "evidence": None}])
    kept = apply_answers(params, {"height_equals_plan:23.0": "keep"})
    assert kept.body_height_mm == 23.0 and kept.questions == []
    cleared = apply_answers(params, {"height_equals_plan:23.0": "clear"})
    assert cleared.body_height_mm is None and "body_height_mm" not in cleared.package_geometry
    assert params.body_height_mm == 23.0, "the input is never modified"


# ── M5/M13: no lists at all -> no paid third call, a question ────────────

def test_without_printed_lists_the_third_reading_is_not_paid_and_the_user_is_asked():
    data = _main(body_height_mm=18.0)
    third = Mock(return_value={"values": {"body_height": 23.0}, "printed": [23.0], "notes": ""})
    logs = []
    apply_dedicated_body(data, {"values": {"body_height": 23.0}, "printed": [], "notes": ""},
                         logs.append, tie_break=third)
    assert third.call_count == 0
    assert any("Spareggio non richiesto" in line for line in logs)
    assert data["body_height_mm"] is None and data["_questions"][0]["field"] == "body_height_mm"


# ── A2: chain kinds, anchors, named outlines and datums ──────────────────

def test_ordinate_chains_are_measured_from_the_first_stop():
    """H2: 0, 5, 10, 15 from one datum are positions, not consecutive quotes."""
    chains = [{"axis": "x", "kind": "from_first_stop", "stops": ["edge", "1", "2", "3"],
               "values": [5.0, 10.0, 15.0]},
              {"axis": "y", "stops": ["1,2,3", "edge"], "values": [2.0]}]
    pads = pads_from_chains(chains)["pads"]
    assert [pads[n][0] for n in ("1", "2", "3")] == pytest.approx([5.0, 10.0, 15.0])
    # Negative twin: the same numbers read as a consecutive chain add up.
    consecutive = [dict(chains[0], kind="consecutive"), chains[1]]
    pads = pads_from_chains(consecutive)["pads"]
    assert [pads[n][0] for n in ("1", "2", "3")] == pytest.approx([5.0, 15.0, 30.0])


def test_a_quote_to_a_pad_edge_is_converted_with_the_pad_size():
    """SMD land pattern: the quote reaches the near edge of pad 2 (1.0 mm wide along X)."""
    chains = [{"axis": "x", "stops": ["1", "2"], "values": [4.0], "at": ["centre", "near_edge"]},
              {"axis": "y", "stops": ["1,2", "edge"], "values": [2.0]}]
    sized = pads_from_chains(chains, 1.0, None, {"1": (1.0, 2.0), "2": (1.0, 2.0)})
    assert sized["pads"]["2"][0] - sized["pads"]["1"][0] == pytest.approx(4.5)
    far = [dict(chains[0], at=["centre", "far_edge"]), chains[1]]
    assert (pads_from_chains(far, 1.0, None, {"2": (1.0, 2.0)})["pads"]["2"][0]
            == pytest.approx(3.5))
    # Negative twin: without the pad size the chain is not read and says why.
    unsized = pads_from_chains(chains)
    assert "2" not in unsized["pads"] and "2" in unsized["unplaced"]
    assert any("dimensione del pad" in note for note in unsized["malformed"])


def test_an_anchor_list_of_the_wrong_length_or_on_the_outline_is_malformed():
    chains = [{"axis": "x", "stops": ["edge", "1"], "values": [4.0], "at": ["near_edge", "centre"]},
              {"axis": "x", "stops": ["1", "2"], "values": [4.0], "at": ["centre"]}]
    result = pads_from_chains(chains, 1.0, None, {"1": (1.0, 1.0), "2": (1.0, 1.0)})
    assert len(result["malformed"]) == 2 and not result["pads"]


def test_a_named_outline_stop_is_a_node_never_compared_with_the_body():
    """M3: a chain closed on the flange places the pads; the span is not the body's."""
    chains = [{"axis": "x", "stops": ["edge:flangia", "1", "2", "edge:flangia"], "values": [3.0, 10.0, 3.0]},
              {"axis": "y", "stops": ["edge", "1,2", "edge"], "values": [2.0, 2.0]}]
    result = pads_from_chains(chains)
    assert result["pads"]["2"][0] - result["pads"]["1"][0] == pytest.approx(10.0)
    assert result["spans"]["x"] is None and result["spans"]["y"] == pytest.approx(4.0)
    problems, _exchanged = _chain_contradictions({"body_length_mm": 12.0, "body_width_mm": 4.0},
                                                 result, 1.0)
    assert problems == []
    # Negative twin: the bare body outline closing the same 16 mm IS compared.
    body = [dict(chains[0], stops=["edge", "1", "2", "edge"]), chains[1]]
    problems, _exchanged = _chain_contradictions({"body_length_mm": 12.0, "body_width_mm": 4.0},
                                                 pads_from_chains(body), 1.0)
    assert len(problems) == 1 and "16.00 mm" in problems[0]


def test_a_datum_stop_is_reported_apart_and_never_becomes_a_pad():
    """M2: a mounting hole quoted in the chain is a node for the sums, not a terminal."""
    chains = [{"axis": "x", "stops": ["edge", "ref:M3", "1", "2", "edge"], "values": [2.0, 3.0, 10.0, 2.0]},
              {"axis": "y", "stops": ["edge", "ref:M3,1,2", "edge"], "values": [2.0, 2.0]}]
    result = pads_from_chains(chains)
    assert set(result["pads"]) == {"1", "2"} and result["unplaced"] == []
    assert result["references"]["M3"] == pytest.approx((2.0, 2.0))
    assert result["pads"]["1"] == pytest.approx((5.0, 2.0))
    # Negative twin: the same hole written as a pad number «M3» is a pad.
    as_pad = [dict(chains[0], stops=["edge", "M3", "1", "2", "edge"]),
              dict(chains[1], stops=["edge", "M3,1,2", "edge"])]
    assert "M3" in pads_from_chains(as_pad)["pads"]


def test_a_datum_in_the_dedicated_reading_does_not_make_an_extra_pad():
    """M2 end to end: the main answer has pads 1, 2; the reading's datum is not «a pad it lacks»."""
    data = {"drawing_unit": "mm", "contacts_view": "bottom", "package_geometry": {
        "origin": "custom", "body_length_mm": 17.0, "body_width_mm": 4.0,
        "pins": [{"number": "1", "x_mm": 5.0, "y_mm": 2.0}, {"number": "2", "x_mm": 15.0, "y_mm": 2.0}]}}
    chains = [{"axis": "x", "stops": ["edge", "ref:M3", "1", "2", "edge"], "values": [2.0, 3.0, 10.0, 2.0]},
              {"axis": "y", "stops": ["edge", "ref:M3,1,2", "edge"], "values": [2.0, 2.0]}]
    apply_dedicated_pads(data, dict(pads_from_chains(chains), view="bottom", notes=""))
    assert "_pin_map_conflict" not in data and "riferimenti e fori di fissaggio" in data["_numbering_note"]


def test_a_max_body_size_bounds_the_span_from_above_only():
    """A housing quoted «22.5 MAX»: a 22.0 mm span is no contradiction; a 23.0 mm one is."""
    chains = [{"axis": "x", "stops": ["edge", "1", "2", "edge"], "values": [2.0, 18.0, 2.0]},
              {"axis": "y", "stops": ["edge", "1,2", "edge"], "values": [2.0, 2.0]}]
    reading = pads_from_chains(chains)
    geometry = {"body_length_mm": 22.5, "body_width_mm": 4.0, "body_quote_kind": "max"}
    assert _chain_contradictions(geometry, reading, 1.0) == ([], False)
    longer = pads_from_chains([dict(chains[0], values=[2.0, 19.0, 2.0]), chains[1]])
    problems, _exchanged = _chain_contradictions(geometry, longer, 1.0)
    assert len(problems) == 1 and "al massimo" in problems[0]
    # Negative twin: the same 22.0 span against a NOMINAL 22.5 is a contradiction.
    nominal = dict(geometry, body_quote_kind="nominal")
    assert len(_chain_contradictions(nominal, reading, 1.0)[0]) == 1


def test_the_body_qualifier_travels_from_the_single_answer_into_the_geometry():
    data = ext.single_answer_to_data(_single(body_quote_kind="max"))
    assert data["package_geometry"]["body_quote_kind"] == "max"
    assert "body_quote_kind" not in ext.single_answer_to_data(_single())["package_geometry"]


# ── A3: the rounding from the printed text ───────────────────────────────

@pytest.mark.parametrize("text, value, expected", [
    ("12", 12, 0.5), ("12.2", 12.2, 0.05), ("12.20", 12.2, 0.005), ("12,20", 12.2, 0.005),
    ("+0.100", 0.1, 0.0005), ("12.2", 12.9, None), ("abc", 12, None), (12, 12, None)])
def test_the_rounding_of_a_quote_follows_its_printed_decimals(text, value, expected):
    assert quote_rounding(text, value, 1.0) == (pytest.approx(expected) if expected else None)


def test_the_rounding_is_in_the_declared_unit():
    assert quote_rounding("0.10", 0.1, 25.4) == pytest.approx(0.005 * 25.4)


def test_a_printed_text_never_loosens_the_units_rounding():
    """CONTRACT CHANGED (generality audit 2026-11-05, M4): a drawing prints NOMINAL values under a
    general tolerance class, so «12» + «11» do not license a 0.8 mm slip against a body of 23: the
    text of a quote can only tighten the unit's rounding, never loosen it (M14 was never reproduced)."""
    chains = [{"axis": "x", "stops": ["edge", "1", "2", "edge"], "values": [2.0, 18.0, 2.0]},
              {"axis": "y", "stops": ["edge", "1", "edge"], "values": [12.4, 11.4],
               "values_text": ["12", "11"]},
              {"axis": "y", "stops": ["1", "2"], "values": [0.0001]}]
    reading = pads_from_chains(chains)
    geometry = {"body_length_mm": 22.0, "body_width_mm": 23.0}
    problems, _exchanged = _chain_contradictions(geometry, reading, 1.0)
    assert len(problems) == 1 and "23.80" in problems[0]
    # Positive twin: the same texts with the numbers they print close on the body.
    exact = [chains[0], dict(chains[1], values=[12.0, 11.0]), chains[2]]
    assert _chain_contradictions(geometry, pads_from_chains(exact), 1.0)[0] == []
    # The declared rounding is still recorded (the text was consistent), only capped when used.
    assert reading["span_rounding"]["x"] is None and reading["span_rounding"]["y"] == pytest.approx(1.0)


def test_a_precise_inch_text_tightens_the_tolerance_down_to_the_floor():
    """Positive (M4): an inch quote printed to 0.001" rounds by 0.013 mm, under the unit's 0.127 mm
    fallback: the printed text makes the check stricter, but never below PAD_READING_TOLERANCE_MM."""
    from kicadgen.services.datasheet_extractor import PAD_READING_TOLERANCE_MM, _Rounding, _sum_tolerance
    precise = _Rounding(quote_rounding("0.100", 0.1, 25.4) * 2, True, 2)
    assert precise.tolerance(25.4) == pytest.approx(PAD_READING_TOLERANCE_MM)
    coarse = _Rounding(quote_rounding("1", 1.0, 25.4) * 2, True, 2)
    assert coarse.tolerance(25.4) == pytest.approx(_sum_tolerance(2, 25.4))


def test_two_chains_closing_a_loop_keep_the_units_tolerance_whatever_the_texts_say():
    """CONTRACT CHANGED (M4): «10» against 5.0 + 5.0 read as 10.4 is a 0.4 mm contradiction in a loop
    of three millimetre quotes, with or without printed texts; the loop closes when the numbers do."""
    chains = [{"axis": "x", "stops": ["1", "2"], "values": [10.4], "values_text": ["10"]},
              {"axis": "x", "stops": ["1", "3"], "values": [5.0], "values_text": ["5.0"]},
              {"axis": "x", "stops": ["3", "2"], "values": [5.0], "values_text": ["5.0"]}]
    column = [{"axis": "y", "stops": ["1,2,3", "edge"], "values": [1.0]}]
    assert len(pads_from_chains(chains + column)["problems"]) == 1
    closed = [dict(chains[0], values=[10.0])] + chains[1:]
    assert pads_from_chains(closed + column)["problems"] == []


# ── A4: SMD part end to end, case-insensitive numbers ────────────────────

def test_a_single_smd_part_goes_through_the_same_path_with_edge_quotes(offline, monkeypatch, tmp_path):
    """Gull-wing SMD: pads without coordinates placed by chains to the pad edges, a height read
    perpendicular to the seating plane, the dedicated reading respelled case-insensitively."""
    logs = []
    pads = [{"number": "A1", "pad_type": "smd", "pad_length_mm": 1.0, "pad_width_mm": 2.0},
            {"number": "A2", "pad_type": "smd", "pad_length_mm": 1.0, "pad_width_mm": 2.0}]
    chains = [{"axis": "x", "stops": ["A1", "A2"], "values": [5.0], "at": ["far_edge", "near_edge"]},
              {"axis": "y", "stops": ["A1,A2", "edge"], "values": [2.0]}]
    main = _single(pads=pads, pad_chains=chains, body_length_mm=8.0, body_width_mm=4.0,
                   body_height_mm=2.5, lead_exit="side", leads_point="down",
                   printed_vertical_quotes=_quotes((2.5, "body_height"), (0.1, "standoff")))
    second = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["a1", "a2"], "values": [6.0]},
        {"axis": "y", "stops": ["a1,a2", "edge"], "values": [2.0]}]}
    body = {"drawing_unit": "mm", "body_length": 8.0, "body_width": 4.0, "body_height": 2.5,
            "leads_point": "down", "printed_vertical_quotes": _quotes((2.5, "body_height"), (0.1, "standoff"))}
    call = Mock(side_effect=[main, second, body])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], model="vendor/main", component_family="single_smd",
        pad_model="vendor/reader", document_roles={"quote.png": ["top"]}, progress=logs.append)
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in params.package_geometry["pins"]}
    assert set(pins) == {"A1", "A2"}, "the reading's a1/a2 were respelled to the answer's A1/A2"
    assert pins["A2"][0] - pins["A1"][0] == pytest.approx(6.0), "far edge + 5 + near edge = 6 mm"
    assert params.pin_map_conflict is None and "confermate" in params.notes
    assert params.body_height_mm == pytest.approx(2.5)
    assert call.call_args_list[1].kwargs["tool"]["function"]["name"] == "report_pad_positions"


def test_the_reading_is_respelled_only_when_one_number_matches_ignoring_case():
    reading = {"pads": {"a1": (0.0, 0.0), "B1": (1.0, 0.0)}, "unplaced": ["c1"]}
    respelled = ext._respell_reading(reading, ["A1", "B1", "C1"])
    assert set(respelled["pads"]) == {"A1", "B1"} and respelled["unplaced"] == ["C1"]
    ambiguous = ext._respell_reading({"pads": {"a1": (0.0, 0.0)}, "unplaced": []}, ["A1", "a1"])
    assert set(ambiguous["pads"]) == {"a1"}, "two answer numbers match: nothing is renamed"


def test_the_single_schema_requires_the_printed_quotes_and_not_the_coordinates():
    pad = ext._single_component_tool()["function"]["parameters"]["properties"]["pads"]["items"]
    assert "x_mm" not in pad["required"] and "y_mm" not in pad["required"]
    required = ext._single_component_tool()["function"]["parameters"]["required"]
    assert "printed_vertical_quotes" in required
    assert "printed_vertical_quotes" in ext._BODY_DIMENSIONS_TOOL["input_schema"]["required"]
    assert "leads_point" in ext._single_component_tool()["function"]["parameters"]["properties"]
    assert "larghezze delle viste frontale" not in ext.DEFAULT_SINGLE_SYSTEM_PROMPT
    assert "larghezze delle viste frontale" not in ext.DEFAULT_BODY_DIMENSIONS_PROMPT
    assert ext._LEAD_RULE in ext.DEFAULT_SINGLE_SYSTEM_PROMPT and ext._LEAD_RULE in ext.DEFAULT_BODY_DIMENSIONS_PROMPT
    assert "PERPENDICOLARMENTE AL PIANO DI APPOGGIO" in ext._HEIGHT_RULE
    assert "DIREZIONE DEI TERMINALI" not in ext._HEIGHT_RULE


# ── B: questions and answers ─────────────────────────────────────────────

def test_a_pad_layout_question_carries_its_candidates_and_a_saved_answer_applies_them(offline, monkeypatch,
                                                                                      tmp_path):
    from test_single_component_prompt import PADS
    wrong = [dict(p, x_mm=x, y_mm=y) for p, (x, y) in zip(PADS, [(0.0, 0.0), (2.54, 0.0), (5.08, 0.0)])]
    second = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["1", "5", "2"], "values": [2.54, 12.7]},
        {"axis": "y", "stops": ["1,2", "5"], "values": [5.08]}]}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[_single(pads=wrong), second, {"drawing_unit": "mm"}]))
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           pad_model="vendor/reader", document_roles={"quote.png": ["top"]})
    question, = [q for q in params.questions if q["field"] == "pad_layout"]
    dedicated = next(o["value"] for o in question["options"] if o["value"].startswith("dedicated"))
    answered = apply_answers(params, {question["key"]: dedicated})
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in answered.package_geometry["pins"]}
    assert pins["5"][1] - pins["1"][1] == pytest.approx(5.08) and pins["2"][0] - pins["1"][0] == pytest.approx(15.24)
    assert answered.package_geometry["needs_review"] and answered.pin_map_conflict is None
    assert all(q["field"] != "pad_layout" for q in answered.questions)
    # The same analysis given the saved answer asks nothing and writes the choice.
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[_single(pads=wrong), second, {"drawing_unit": "mm"}]))
    again = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                          pad_model="vendor/reader", document_roles={"quote.png": ["top"]},
                                          answers={question["key"]: dedicated})
    assert not any(q["field"] == "pad_layout" for q in again.questions)
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in again.package_geometry["pins"]}
    assert pins["5"][1] - pins["1"][1] == pytest.approx(5.08) and "scelte dall'utente" in again.notes


def test_an_answer_outside_the_options_or_for_an_unknown_key_changes_nothing():
    params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 package_geometry={"pins": [{"number": "1", "x_mm": 0.0, "y_mm": 0.0},
                                                            {"number": "2", "x_mm": 1.0, "y_mm": 2.0}],
                                                   "source_view": "top"},
                                 questions=[{"key": "contacts_view:1|2", "field": "contacts_view", "text": "?",
                                             "options": [{"label": "t", "value": "top"},
                                                         {"label": "b", "value": "bottom"}], "evidence": None}])
    same = apply_answers(params, {"contacts_view:1|2": "sideways", "other": "bottom"})
    assert len(same.questions) == 1 and same.package_geometry["source_view"] == "top"
    flipped = apply_answers(params, {"contacts_view:1|2": "bottom"})
    assert flipped.questions == [] and flipped.package_geometry["source_view"] == "bottom"


def test_question_keys_are_stable_and_name_their_candidates():
    assert question_key("body_height_mm", [23.0, 18.0]) == "body_height_mm:18.0|23.0"
    assert question_key("contacts_view", ["top", "bottom"]) == "contacts_view:bottom|top"


# ── C6: partial re-runs on the stored main answer ────────────────────────

def test_the_result_carries_the_raw_main_answer_for_partial_reruns(offline, monkeypatch, tmp_path):
    main = _single(body_height_mm=None)
    body = {"drawing_unit": "mm", "body_height": 16.9, "printed_vertical_quotes": _quotes((16.9, "body_height"))}
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[main, _CHAINS_ANSWER, body]))
    params = ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                           component_family="single_tht", pad_model="vendor/reader",
                                           document_roles={"quote.png": ["top"]})
    assert params.raw_main["drawing_unit"] == "mm" and params.raw_main["package_geometry"]["pins"]
    assert "_body_conflict" not in params.raw_main and "body_height_mm" not in params.raw_main["package_geometry"]
    # Re-reading the height alone: one paid call, the stored answer gets the height.
    call = Mock(return_value={"drawing_unit": "mm", "body_height": 16.9,
                              "printed_vertical_quotes": _quotes((16.9, "body_height"))})
    reread = ext.reread_body_height(params.raw_main, [_drawing(tmp_path)], model="vendor/reader", call=call)
    assert call.call_count == 1 and reread.body_height_mm == pytest.approx(16.9)
    assert reread.body_length_mm == pytest.approx(20.5) and reread.raw_main is not None


def test_reread_pad_positions_confronts_the_stored_pads_with_a_new_reading(offline, monkeypatch, tmp_path):
    from test_single_component_prompt import PADS
    wrong = [dict(p, x_mm=x, y_mm=y) for p, (x, y) in zip(PADS, [(0.0, 0.0), (2.54, 0.0), (5.08, 0.0)])]
    stored = ext.single_answer_to_data(_single(pads=wrong))
    agreeing = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["1", "2", "5"], "values": [2.54, 2.54]},
        {"axis": "y", "stops": ["1,2,5", "edge"], "values": [3.0]}]}
    call = Mock(return_value=agreeing)
    reread = ext.reread_pad_positions(stored, [_drawing(tmp_path)], model="vendor/reader", call=call)
    assert call.call_count == 1 and reread.pin_map_conflict is None and "confermate" in reread.notes
    # Negative twin: the same reading by the same model is one reading -> no confirmation.
    reread = ext.reread_pad_positions(stored, [_drawing(tmp_path)], model="vendor/main",
                                      call=Mock(return_value=agreeing), same_model=True)
    assert reread.pin_map_conflict and "stesso modello" in reread.pin_map_conflict
    with pytest.raises(ext.ExtractionError):
        ext.reread_pad_positions({"drawing_unit": "mm"}, [_drawing(tmp_path)], model="m", call=Mock())
    with pytest.raises(ext.ExtractionError):
        ext.reread_body_height(stored, [Path(tmp_path, "doc.pdf")], model="m", call=Mock())


# ── C2: measured candidates compared by offsets ──────────────────────────

def test_measured_candidates_with_different_origins_count_as_one():
    """M10: the main (origin pin 1) and the dedicated (origin outline) measure to one layout."""
    from types import SimpleNamespace
    from test_pad_positions_by_drawn_holes import CHAINS, FakeHoles, SHIFTED, TRUE

    class Measure:
        def __call__(self, pads, body):
            ox, oy = pads["1"]
            positions = {n: (TRUE[n][0] - TRUE["1"][0] + ox, TRUE[n][1] - TRUE["1"][1] + oy) for n in pads}
            return SimpleNamespace(positions_mm=positions, measured_mm=positions, moved="2",
                                   moved_from_mm=pads["2"], max_error_mm=0.05, tolerance_mm=0.2,
                                   image="drawing.png")
    shifted_main = {n: (x + 10.0, y + 10.0) for n, (x, y) in SHIFTED.items()}
    data = {"drawing_unit": "mm", "contacts_view": "bottom", "package_geometry": {
        "origin": "custom", "body_length_mm": 20.0, "body_width_mm": 12.0,
        "pins": [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in shifted_main.items()]}}
    reading = dict(pads_from_chains([CHAINS[0], {"axis": "y", "stops": ["edge", "1,2,3", "4,5", "edge"],
                                                 "values": [2.0, 7.0, 3.0]}]), view="bottom", notes="")
    apply_dedicated_pads(data, reading, check_layout=FakeHoles(TRUE), measure_layout=Measure())
    # CONTRACT CHANGED (user, 2026-11-11, round 6 (a)): the measured layout of two agreeing
    # readings the holes refute is OFFERED, not written. The point of M10 stands: the two
    # readings, measured in their own origins, are ONE measured layout -- one option of the
    # question beside the readings' layout, not two and not none.
    assert "_pin_map_conflict" not in data
    question, = data["_questions"]
    measured = [option for option in question["options"] if option["value"].startswith("holes:")]
    assert len(question["options"]) == 2 and len(measured) == 1
    assert "misurate sui fori" in measured[0]["label"]


# ── generality audit 2026-11-05: M7 (view key on the layout), M5 (references reach the witness) ──

def test_the_side_question_is_keyed_on_the_layout_not_on_the_pad_numbers_alone():
    """Negative (M7): the same pin numbers laid out differently are a different question, so a side
    answered for one drawing is not reused for another drawing of the same project."""
    from kicadgen.services.datasheet_extractor import _require_declared_view

    def asked(layout):
        data = {"drawing_unit": "mm", "contacts_view": "unknown", "package_geometry": {"pins": [
            {"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in layout.items()]}}
        _require_declared_view(data)
        question, = data["_questions"]
        return question["key"]

    first = asked({"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (5.0, 3.0), "4": (0.0, 7.0)})
    second = asked({"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (5.0, 7.0), "4": (0.0, 3.0)})
    assert first != second and first.startswith("contacts_view:")
    # Positive: the same layout asked again has the same key (a saved answer applies).
    assert asked({"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (5.0, 3.0), "4": (0.0, 7.0)}) == first


def test_declared_reference_holes_reach_the_witness_with_the_layout_they_belong_to():
    """Positive (M5): the `ref:` stops the chain reading placed are forwarded to the hole check and
    the measure as `references`, in the frame of the dedicated layout, and to no other layout."""
    from test_pad_positions_by_drawn_holes import CHAINS, TRUE, SHIFTED, _main as _answer, _reading
    seen = []

    def check(pads, body, references=None):
        seen.append((sorted(pads.items()), references))
        return None

    data = _answer(SHIFTED)
    chains = CHAINS + [{"axis": "x", "stops": ["1", "ref:M3"], "values": [30.0]},
                       {"axis": "y", "stops": ["1", "ref:M3"], "values": [1.0]}]
    apply_dedicated_pads(data, _reading(chains), check_layout=check)
    with_refs = [refs for pads, refs in seen if refs]
    assert with_refs and all(len(refs) == 1 for refs in with_refs)
    dedicated = next(pads for pads, refs in seen if refs)
    (x1, y1) = dict(dedicated)["1"]
    assert with_refs[0][0] == pytest.approx((x1 + 30.0, y1 + 1.0))
    # The main layout is checked without references: they are not in its frame.
    assert any(refs is None for pads, refs in seen)
    # A double that takes (pads, body) only is still called, without a TypeError.
    plain = []
    data = _answer(SHIFTED)
    apply_dedicated_pads(data, _reading(chains), check_layout=lambda pads, body: plain.append(1))
    assert plain


# ── relay symbol (user, 2026-11-05): the pinout declares a closed pin function ──

def test_a_pinout_entry_keeps_a_declared_function_and_drops_any_other_text():
    """Positive: «coil» (any case, blanks) is kept normalised; negative: a free word such as «bobina»
    is NOT mapped to a role but dropped with a note naming the field; the pin itself stays."""
    notes = []
    kept = ext._validated_pinout([{"pin": "1", "name": "Bobina", "function": " Coil "},
                                  {"pin": "2", "name": "Bobina", "function": "bobina"},
                                  {"pin": "3", "name": "NC", "function": "common", "contact": " A "},
                                  {"pin": "4", "name": "NC", "function": "nc", "contact": ""}], notes)
    assert kept == [{"pin": "1", "name": "Bobina", "function": "coil"},
                    {"pin": "2", "name": "Bobina"},
                    {"pin": "3", "name": "NC", "function": "common", "contact": "A"},
                    {"pin": "4", "name": "NC", "function": "nc"}]
    assert len(notes) == 2 and "pinout[1].function" in notes[0] and "pinout[3].contact" in notes[1]


def test_the_single_tool_with_the_pin_function_stays_strict_lossless():
    from kicadgen.services.openrouter import strict_tool
    tool = ext._single_component_tool()
    item = tool["function"]["parameters"]["properties"]["pinout"]["items"]["properties"]
    assert item["function"]["enum"] == list(ext.PINOUT_FUNCTIONS)
    assert "function" not in tool["function"]["parameters"]["properties"]["pinout"]["items"]["required"]
    assert strict_tool(tool) is not None and strict_tool(ext._openai_tool()) is not None
    assert "pinout[].function" in ext.DEFAULT_SINGLE_SYSTEM_PROMPT and "pinout[].contact" in ext.DEFAULT_SINGLE_SYSTEM_PROMPT
    assert "contact" in item and "contact" not in tool["function"]["parameters"]["properties"]["pinout"]["items"]["required"]
