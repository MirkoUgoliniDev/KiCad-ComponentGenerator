"""@brief Audit of the hole-or-terminal question and the night5 real-run defects (2026-11-14).

- H1: «is it the hole or the terminal?» only when both readings point to ONE
  printed value (equal within the transcription rounding); a hole smaller
  than a DIFFERENT terminal stays a contradiction; a declared press-fit pad
  is not judged at all.
- M2: after «it is the hole» the terminal read is marked «contradicted».
- M3: the part-level terminal takes the same question.
- L1: one question per (hole, terminal).
- D-a: the terminals follow the reading whose layout was adopted.
- D-b: a note describing a conflict an answer settled leaves the result.
Invented parts. No network.
"""
import pytest

from kicadgen.core.tht_estimates import normalised_sources
from kicadgen.services import datasheet_extractor as ext


def _pins(numbers=(1, 2), drill=0.8, lead=0.8, fit=None, own_lead=True):
    pins = []
    for n in numbers:
        pin = {"number": str(n), "pad_type": "thru_hole", "drill_mm": drill, "x_mm": 2.54 * n,
               "y_mm": 0.0, "pad_length_mm": 1.6, "pad_width_mm": 1.6, "shape": "circle",
               "sources": {"drill_mm": "read", "pad_size": "estimated"}}
        if own_lead:
            pin["lead"] = {"shape": "round", "diameter_mm": lead}
        if fit:
            pin["lead_fit"] = fit
        pins.append(pin)
    return pins


def _found():
    return {"lead_contradicted": [], "lead_lifted": [], "widened": [], "loose_blade": [],
            "part_narrow": [], "part_protected": [], "part_lifted": [], "part_widened": []}


def _judge(pins, part_lead=None, part_word=None, answers=None, unit_cap=0.05):
    data = {"notes": "", "_question_documents": ["d.png"]}
    if answers is not None:
        data["_answers"] = answers
    found, derived, notes = _found(), {}, []
    ext._judge_holes_against_leads(pins, part_lead, part_word, found, derived, unit_cap)
    ext._ask_hole_or_terminal(data, found, derived, notes)
    return data, found, notes


def _questions(data):
    return [q for q in data.get("_questions") or () if q["field"] == "pad_layout"]


# ── H1 and press-fit ────────────────────────────────────────────────────

def test_one_printed_value_read_as_hole_and_terminal_is_asked():
    data, found, _notes = _judge(_pins(drill=0.8, lead=0.8))
    assert len(_questions(data)) == 1 and not found["lead_contradicted"]


def test_a_hole_smaller_than_a_different_terminal_is_a_contradiction_not_a_question():
    data, found, _notes = _judge(_pins(drill=0.6, lead=0.8))
    assert not _questions(data) and found["lead_contradicted"] == ["1", "2"]


def test_a_declared_press_fit_pad_is_not_judged():
    pins = _pins(drill=0.8, lead=0.8, fit="press_fit")
    data, found, _notes = _judge(pins)
    assert not _questions(data) and not found["lead_contradicted"]
    assert pins[0]["sources"]["drill_mm"] == "read"


def test_press_fit_is_in_the_pad_schema():
    item = ext._PAD_TERMINAL_PROPERTIES["lead_fit"]
    assert item["enum"] == ["clearance", "press_fit"]


# ── M2: the terminal contradicted by the chosen hole ────────────────────

def test_choosing_the_hole_marks_the_terminal_contradicted():
    first, _found, _notes = _judge(_pins())
    (question,) = _questions(first)
    hole = next(o["value"] for o in question["options"] if o["value"].endswith("=hole"))
    pins = _pins()
    _judge(pins, answers={question["key"]: hole})
    assert pins[0]["sources"]["lead"] == "contradicted" and pins[0]["sources"]["drill_mm"] == "read"


def test_contradicted_is_a_provenance_word_of_the_lead_only():
    assert normalised_sources({"lead": "contradicted"}) == {"lead": "contradicted"}
    with pytest.raises(ValueError):
        normalised_sources({"drill_mm": "contradicted"})


# ── M3: the part-level terminal ─────────────────────────────────────────

def test_a_part_level_terminal_equal_to_the_read_hole_is_asked_not_lifted():
    pins = _pins(drill=0.8, own_lead=False)
    data, found, _notes = _judge(pins, part_lead=({"shape": "round", "diameter_mm": 0.8}, "read"),
                                 part_word="read")
    assert len(_questions(data)) == 1 and not found["part_lifted"]
    assert pins[0]["drill_mm"] == 0.8


def test_a_part_level_terminal_of_another_value_lifts_the_hole_as_before():
    """Negative of M3: 0.5 mm hole, 0.7 mm terminal read -> the existing lift."""
    pins = _pins(drill=0.5, own_lead=False)
    data, found, _notes = _judge(pins, part_lead=({"shape": "round", "diameter_mm": 0.7}, "read"),
                                 part_word="read")
    assert not _questions(data) and found["part_lifted"] == ["1", "2"]


# ── L1: one question per (hole, terminal) ───────────────────────────────

def test_questions_are_grouped_by_hole_and_terminal():
    pins = _pins((1, 2), drill=0.8, lead=0.8) + _pins((3, 4), drill=0.8, lead=0.82)
    data, _found, _notes = _judge(pins)
    keys = sorted(q["key"] for q in _questions(data))
    assert len(keys) == 2 and "@0.8/0.82=" in keys[0] + keys[1]


# ── D-b: the settled conflict note leaves the result ────────────────────

def test_the_conflict_note_is_said_while_asked_and_removed_by_the_answer():
    pins = _pins()
    data, _found, notes = _judge(pins)
    conflict = next(line for line in notes if "non passa nel foro" in line)
    (question,) = _questions(data)
    terminal = next(o["value"] for o in question["options"] if o["value"].endswith("=terminal"))
    params = ext.ExtractedParams(name="X", description="", tags=[], confidence="low",
                                 notes="prima\n" + conflict + "\ndopo",
                                 package_geometry={"origin": "custom", "pins": pins},
                                 questions=list(data["_questions"]), candidates=data["_candidates"])
    answered = ext.apply_answers(params, {question["key"]: terminal})
    assert conflict not in answered.notes and "prima" in answered.notes
    # A refused answer settles nothing (negative).
    unanswered = ext.apply_answers(params, {"pad_layout:other": "x"})
    assert conflict in unanswered.notes


# ── D-a: terminals follow the adopted layout's reading ──────────────────

def _srd_like(source):
    """Two pads whose terminals the two readings assign the other way round."""
    pins = [{"number": "1", "pad_type": "thru_hole", "x_mm": 0.0, "y_mm": 0.0,
             "pad_length_mm": 2.0, "pad_width_mm": 2.0, "shape": "circle",
             "lead": {"shape": "round", "diameter_mm": 0.6}, "value_sources": {"lead": "read"}},
            {"number": "2", "pad_type": "thru_hole", "x_mm": 6.0, "y_mm": 0.0,
             "pad_length_mm": 2.0, "pad_width_mm": 2.0, "shape": "circle",
             "lead": {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4},
             "value_sources": {"lead": "read"}}]
    data = {"drawing_unit": "mm", "_layout_source": source,
            "_reading_leads": {"1": {"lead": {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}},
                               "2": {"lead": {"shape": "round", "diameter_mm": 0.6}}}}
    return {"origin": "custom", "pins": pins}, data


def test_the_adopted_reading_gives_its_terminals_with_a_note():
    geometry, data = _srd_like("dedicated")
    notes = []
    resolved, _problems = ext._resolve_pad_terminals(geometry, data, notes)
    leads = {pin["number"]: pin["lead"]["shape"] for pin in resolved["pins"]}
    assert leads == {"1": "flat", "2": "round"}
    assert any("lettura dedicata, di cui e' stata adottata la disposizione" in n for n in notes)
    assert all(pin["sources"]["lead"] == "estimated" for pin in resolved["pins"])


def test_with_the_main_layout_the_main_terminals_stay():
    """Negative: the main answer's layout was adopted -> its terminals, no swap note."""
    geometry, data = _srd_like("main")
    notes = []
    resolved, _problems = ext._resolve_pad_terminals(geometry, data, notes)
    leads = {pin["number"]: pin["lead"]["shape"] for pin in resolved["pins"]}
    assert leads == {"1": "round", "2": "flat"}
    assert not any("di cui e' stata adottata la disposizione" in n for n in notes)


def test_a_layout_chosen_later_brings_its_reading_s_terminals():
    geometry = {"pins": [{"number": "1", "pad_type": "thru_hole", "drill_mm": 0.85,
                          "pad_length_mm": 1.6, "pad_width_mm": 1.6, "shape": "circle",
                          "lead": {"shape": "round", "diameter_mm": 0.6},
                          "sources": {"drill_mm": "estimated", "lead": "estimated"}}]}
    notes = []
    ext._apply_reading_leads(geometry, {"1": {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}},
                             notes)
    pin = geometry["pins"][0]
    assert pin["lead"]["shape"] == "flat" and pin["drill_mm"] > 1.0 and notes


def test_an_answer_saved_under_the_first_key_still_applies():
    """L1 changed the key (hole AND terminal); an answer of the hole-only key is the same fact."""
    legacy = ("pad_layout:hole_or_terminal:1,2@0.8=hole|hole_or_terminal:1,2@0.8=terminal")
    pins = _pins()
    data, found, _notes = _judge(pins, answers={legacy: "hole_or_terminal:1,2@0.8=terminal"})
    assert not _questions(data) and found["lead_lifted"] == ["1", "2"]
    assert pins[0]["drill_mm"] > 0.8


# ── final audit M1: one printed value read twice, by the roundings ──────

def _with_roundings(pins, hole_rounding, lead_rounding):
    for pin in pins:
        pin["_callout_roundings"] = (hole_rounding, lead_rounding)
    return pins


def test_a_one_decimal_hole_and_a_two_decimal_terminal_within_its_rounding_are_asked():
    """«0.8» (rounding 0.05) and «0.82» (0.005): the coarser rounding holds both."""
    data, found, _notes = _judge(_with_roundings(_pins(drill=0.8, lead=0.82), 0.05, 0.005))
    assert len(_questions(data)) == 1 and not found["lead_contradicted"]


def test_two_two_decimal_values_apart_are_a_contradiction_not_a_question():
    """«0.80» and «0.85», both to the hundredth: two printed values."""
    data, found, _notes = _judge(_with_roundings(_pins(drill=0.80, lead=0.85), 0.005, 0.005))
    assert not _questions(data) and found["lead_contradicted"] == ["1", "2"]


def test_an_unknown_rounding_takes_the_drawing_unit_s_precision():
    """0.80 and 0.90 with no known rounding: one value in an inch drawing, two in mm."""
    data, _found, _notes = _judge(_with_roundings(_pins(drill=0.80, lead=0.90), 0.0, 0.0),
                                  unit_cap=0.127)
    assert len(_questions(data)) == 1
    data, found, _notes = _judge(_with_roundings(_pins(drill=0.80, lead=0.90), 0.0, 0.0))
    assert not _questions(data) and found["lead_contradicted"]


# ── final audit M2: the adopted reading's printed hole is never re-estimated ──

def test_a_disputed_hole_takes_the_adopted_reading_s_printed_value_later():
    """Main Ø0.8 and dedicated Ø1.0 disputed; the dedicated layout chosen later -> Ø1.0."""
    geometry = {"pins": [{"number": "1", "pad_type": "thru_hole", "drill_mm": 0.8,
                          "pad_length_mm": 1.8, "pad_width_mm": 1.8, "shape": "circle",
                          "lead": {"shape": "round", "diameter_mm": 0.6},
                          "sources": {"drill_mm": "estimated", "lead": "estimated"}}]}
    notes = []
    ext._apply_reading_leads(geometry, {"1": {"lead": {"shape": "flat", "width_mm": 0.6,
                                                       "thickness_mm": 0.3}}}, notes,
                             {"1": {"drill_mm": 1.0, "drill_length_mm": None}})
    pin = geometry["pins"][0]
    assert pin["drill_mm"] == 1.0 and pin["lead"]["shape"] == "flat"


def test_the_analysis_takes_the_adopted_reading_s_printed_hole_when_disputed():
    pins = [{"number": "1", "pad_type": "thru_hole", "x_mm": 0.0, "y_mm": 0.0, "drill_mm": 0.8,
             "pad_length_mm": 1.8, "pad_width_mm": 1.8, "shape": "circle",
             "value_sources": {"drill_mm": "read"}}]
    data = {"drawing_unit": "mm", "_layout_source": "dedicated", "_reading_leads": {},
            "_dedicated_holes": {"1": {"drill_mm": 1.0, "drill_length_mm": None, "rounding_mm": 0.05}}}
    notes = []
    resolved, _problems = ext._resolve_pad_terminals({"origin": "custom", "pins": pins}, data, notes)
    assert resolved["pins"][0]["drill_mm"] == 1.0
    assert resolved["pins"][0]["sources"]["drill_mm"] == "estimated"


def test_with_the_main_layout_the_disputed_hole_stays_the_main_one():
    """Negative of M2: no adopted dedicated layout -> the main value stays, estimated."""
    pins = [{"number": "1", "pad_type": "thru_hole", "x_mm": 0.0, "y_mm": 0.0, "drill_mm": 0.8,
             "pad_length_mm": 1.8, "pad_width_mm": 1.8, "shape": "circle",
             "value_sources": {"drill_mm": "read"}}]
    data = {"drawing_unit": "mm", "_layout_source": "main",
            "_dedicated_holes": {"1": {"drill_mm": 1.0, "drill_length_mm": None, "rounding_mm": 0.05}}}
    resolved, _problems = ext._resolve_pad_terminals({"origin": "custom", "pins": pins}, data, [])
    assert resolved["pins"][0]["drill_mm"] == 0.8


# ── final audit L1: a gross press-fit mismatch is named ─────────────────

def test_a_gross_press_fit_mismatch_is_named_and_a_normal_interference_is_not():
    data = {"notes": ""}
    for drill, lead, named in ((0.3, 0.8, True), (0.85, 1.08, False)):
        pins = [{"number": "1", "pad_type": "thru_hole", "x_mm": 0.0, "y_mm": 0.0, "drill_mm": drill,
                 "pad_length_mm": 2.0, "pad_width_mm": 2.0, "shape": "circle", "lead_fit": "press_fit",
                 "lead": {"shape": "round", "diameter_mm": lead},
                 "value_sources": {"drill_mm": "read", "lead": "read"}}]
        notes = []
        ext._resolve_pad_terminals({"origin": "custom", "pins": pins}, dict(data), notes)
        assert any("dichiarati a pressione" in n for n in notes) is named
