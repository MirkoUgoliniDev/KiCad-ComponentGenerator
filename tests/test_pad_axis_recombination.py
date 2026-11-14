"""@brief Two pad readings that fail on DIFFERENT axes never end in a dead end (task C, 2026-11-14).

Night real run of 2026-11-14 (a five-pad relay): the main answer had the X
positions wrong, refuted by the drawn holes; the dedicated chain reading had
X right but a Y quote attributed to the wrong stop, so its Y chain summed to
less than the body. Both were excluded and the analysis ended on «Nessuna
domanda puo' chiudere il caso», after asking the contacts side first.

The rules tested here, the same for every part:
- C1: the layout taking each axis from the reading whose chain on that axis
  passes is offered in the pad-layout question -- recommended when the drawn
  holes confirm it, without recommendation when they cannot check it, not
  offered when they refute it -- never adopted by itself;
- C2: with no recombination on offer, ONE targeted paid re-read of the
  failing axes (faked here); if that also fails, the question offers the
  readings with what is wrong in each, plus «keep blocked»;
- C3: no contacts-side question while a pad-layout question offers layouts;
  the side of the CHOSEN layout is asked after the answer.
Generality audit of 2026-11-14: no recombination across two declared views
that differ (M1); a refuted reading stays in the question only beside a
CONFIRMED recombination, and «keep blocked» is offered whenever nothing is
recommended (M2); the re-read is told no target value (M3); the side is
deferred only with a layout question and carried per candidate in the
drawing frame with the saved answers (M4); a recombination is recommended
only when the two readings order the numbers alike on both axes (M5);
layouts with coincident pads never reach the last resort (L1).
The drawing is invented (a 20 x 12 mm body, five holes), not the relay.
No network: the model calls and the hole witness are faked.
"""
import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    ExtractedParams, LAYOUT_BLOCK_KIND, RECOMBINED_SOURCE, apply_answers, apply_dedicated_pads,
    pads_from_chains)
from test_pad_positions_by_drawn_holes import CHAINS, TRUE, FakeHoles, _Verdict

#: The Y chain with one quote attributed to the wrong stop: pad 2 one
#: millimetre low and the edge-to-edge sum 10 mm against a 12 mm body.
WRONG_Y = {"axis": "y", "stops": ["edge", "1,3", "2", "4,5", "edge"], "values": [2.0, 4.0, 3.0, 1.0]}
#: The X chain with the same kind of fault: the sum 18 mm against 20 mm.
WRONG_X = {"axis": "x", "stops": ["edge", "1", "2,5", "3,4", "edge"], "values": [2.0, 5.0, 7.0, 4.0]}
#: The main answer: Y right, X of pads 2 and 5 two millimetres off (no chain).
MAIN_WRONG_X = dict(TRUE, **{"2": (9.0, 5.0), "5": (9.0, 9.0)})
#: A main answer wrong on BOTH axes (pad 2 elsewhere).
MAIN_WRONG_XY = dict(TRUE, **{"2": (10.0, 3.0)})


def _main(pads, view="bottom"):
    """A single-part main answer in mm with a 20 x 12 mm body."""
    return {"drawing_unit": "mm", "contacts_view": view, "notes": "", "package_geometry": {
        "origin": "custom", "body_length_mm": 20.0, "body_width_mm": 12.0,
        "pins": [{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole", "drill_mm": 0.8,
                  "pad_length_mm": 1.6, "pad_width_mm": 1.6} for n, (x, y) in pads.items()]}}


def _reading(chains, view="bottom"):
    """A dedicated reading as `read_pad_positions` returns it, raw chains included."""
    return dict(pads_from_chains(chains, 1.0), view=view, notes="", raw_chains=list(chains),
                raw_midpoints=[], raw_factor=1.0, pad_sizes=None)


def _offsets(pads):
    ox, oy = pads["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in pads.items()}


def _pins(data):
    return {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}


def _layout_question(data):
    questions = [q for q in data.get("_questions") or () if q["field"] == "pad_layout"]
    assert len(questions) == 1, data.get("_pin_map_conflict")
    return questions[0]


# ── C1: axis-wise recombination ─────────────────────────────────────────

def test_a_confirmed_recombination_is_the_recommended_option_never_adopted():
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]), check_layout=FakeHoles(TRUE))
    question = _layout_question(data)
    assert "_pin_map_conflict" not in data
    recommended = [o for o in question["options"] if o.get("recommended")]
    assert len(recommended) == 1 and recommended[0]["value"].startswith(RECOMBINED_SOURCE)
    assert "ricombinazione" in recommended[0]["label"]
    # Never adopted: the provisional pads are still the main answer's.
    assert _offsets(_pins(data)) == _offsets(MAIN_WRONG_X)
    # The payload of the recommended option writes the true layout.
    payload = data["_candidates"][question["key"]][recommended[0]["value"]]
    chosen = {p["number"]: (p["x_mm"], p["y_mm"]) for p in payload["pins"]}
    assert _offsets(chosen) == _offsets(TRUE)


def test_a_recombination_the_holes_cannot_check_is_offered_without_recommendation():
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]),
                         check_layout=FakeHoles({}, "unverifiable", "not_a_drawing"))
    question = _layout_question(data)
    assert not any(o.get("recommended") for o in question["options"])
    assert any(o["value"].startswith(RECOMBINED_SOURCE) for o in question["options"])
    # Nothing recommended: «keep blocked» is one of the answers (M2).
    assert any(o["value"].startswith(LAYOUT_BLOCK_KIND) for o in question["options"])


def test_a_recombination_the_holes_refute_is_not_offered_and_nothing_is_recommended():
    """Negative of the first test: the drawn holes are another layout altogether."""
    elsewhere = dict(TRUE, **{"2": (12.0, 7.0)})
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]), check_layout=FakeHoles(elsewhere))
    question = _layout_question(data)
    assert not any(o["value"].startswith(RECOMBINED_SOURCE) for o in question["options"])
    assert not any(o.get("recommended") for o in question["options"])
    # The last resort: the readings with their faults, plus «keep blocked».
    assert any(o["value"].startswith(LAYOUT_BLOCK_KIND) for o in question["options"])
    assert "Nessuna domanda" not in str(data.get("_pin_map_conflict"))


def test_readings_failing_on_the_same_axis_give_no_recombination():
    """Negative: the main coordinates are wrong on X too, the dedicated chain fails on X."""
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([WRONG_X, CHAINS[1]]), check_layout=FakeHoles(TRUE))
    question = _layout_question(data)
    assert not any(o["value"].startswith(RECOMBINED_SOURCE) for o in question["options"])


# ── C2: targeted re-read, then the last-resort question ─────────────────

def test_both_axes_failing_in_one_reading_take_the_reread_path():
    calls = []

    def reread(axis, problem):
        calls.append((axis, problem))
        return {"chains": [dict(chain) for chain in CHAINS], "midpoints": [], "factor": 1.0}

    data = _main(MAIN_WRONG_XY)
    apply_dedicated_pads(data, _reading([WRONG_X, WRONG_Y]), check_layout=FakeHoles(TRUE),
                         axis_reread=reread)
    assert len(calls) == 1 and calls[0][0] == "xy"
    # CONTRACT CHANGED (audit M3): the contradiction is named per axis in
    # English WITHOUT numbers -- the body size is the main answer's reading,
    # and a target value would make the re-read copy it.
    assert "along X" in calls[0][1] and "along Y" in calls[0][1]
    assert "body outline" in calls[0][1] and "mm" not in calls[0][1]
    assert not any(ch.isdigit() for ch in calls[0][1])
    # The re-read chains pass and the drawn holes confirm them alone.
    assert "_pin_map_conflict" not in data
    assert not any(q["field"] == "pad_layout" for q in data.get("_questions") or ())
    assert _offsets(_pins(data)) == _offsets(TRUE)


def test_the_reread_replaces_only_the_failing_axis():
    seen = []

    def reread(axis, problem):
        seen.append(axis)
        return {"chains": [dict(CHAINS[1])], "midpoints": [], "factor": 1.0}

    data = _main(MAIN_WRONG_XY)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]), check_layout=FakeHoles(TRUE),
                         axis_reread=reread)
    assert seen == ["y"]
    assert _offsets(_pins(data)) == _offsets(TRUE)


def test_a_failed_reread_ends_in_a_question_with_the_readings_never_a_dead_end():
    calls = []

    def reread(axis, problem):
        calls.append(axis)
        return None

    data = _main(MAIN_WRONG_XY)
    apply_dedicated_pads(data, _reading([WRONG_X, WRONG_Y]), check_layout=FakeHoles(TRUE),
                         axis_reread=reread)
    assert calls == ["xy"]
    question = _layout_question(data)
    assert "_pin_map_conflict" not in data
    values = [o["value"] for o in question["options"]]
    assert any(v.startswith("main") for v in values)
    assert any(v.startswith("dedicated") for v in values)
    assert any(v.startswith(LAYOUT_BLOCK_KIND) for v in values)
    assert not any(o.get("recommended") for o in question["options"])
    dedicated = next(o for o in question["options"] if o["value"].startswith("dedicated"))
    assert "sommano" in dedicated["label"], "the option says what is wrong in it"
    # At most once per analysis: a second pass does not pay again.
    apply_dedicated_pads(data, _reading([WRONG_X, WRONG_Y]), check_layout=FakeHoles(TRUE),
                         axis_reread=reread)
    assert calls == ["xy"]


def test_keep_blocked_keeps_the_block_and_closes_the_question():
    data = _main(MAIN_WRONG_XY)
    apply_dedicated_pads(data, _reading([WRONG_X, WRONG_Y]), check_layout=FakeHoles(TRUE))
    question = _layout_question(data)
    params = ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                             package_geometry=data["package_geometry"],
                             questions=list(data["_questions"]), candidates=data["_candidates"])
    block = next(o["value"] for o in question["options"] if o["value"].startswith(LAYOUT_BLOCK_KIND))
    kept = apply_answers(params, {question["key"]: block})
    assert not any(q["field"] == "pad_layout" for q in kept.questions)
    assert "bloccate" in kept.pin_map_conflict
    # A saved «keep blocked» applies to the same question at once.
    again = _main(MAIN_WRONG_XY)
    again["_answers"] = {question["key"]: block}
    apply_dedicated_pads(again, _reading([WRONG_X, WRONG_Y]), check_layout=FakeHoles(TRUE))
    assert "bloccate" in again["_pin_map_conflict"]


# ── C3: the side waits for the layout ───────────────────────────────────

def test_the_side_is_asked_after_the_pad_layout_answer_on_the_chosen_layout():
    data = _main(MAIN_WRONG_X, view="unknown")
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y], view="unknown"),
                         check_layout=FakeHoles(TRUE))
    ext._require_declared_view(data)
    assert not any(q["field"] == "contacts_view" for q in data["_questions"])
    question = _layout_question(data)
    chosen = next(o["value"] for o in question["options"] if o.get("recommended"))
    params = ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                             package_geometry=data["package_geometry"],
                             questions=list(data["_questions"]), candidates=data["_candidates"])
    answered = apply_answers(params, {question["key"]: chosen})
    side = [q for q in answered.questions if q["field"] == "contacts_view"]
    assert len(side) == 1
    assert ext._layout_digest(_pins({"package_geometry": answered.package_geometry})) in side[0]["key"]


def test_a_declared_view_asks_no_side_after_the_pad_layout_answer():
    """Negative: the readings declare the view, nothing is deferred."""
    data = _main(MAIN_WRONG_X, view="bottom")
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y], view="bottom"),
                         check_layout=FakeHoles(TRUE))
    ext._require_declared_view(data)
    question = _layout_question(data)
    chosen = next(o["value"] for o in question["options"] if o.get("recommended"))
    params = ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                             package_geometry=data["package_geometry"],
                             questions=list(data["_questions"]), candidates=data["_candidates"])
    answered = apply_answers(params, {question["key"]: chosen})
    assert not any(q["field"] == "contacts_view" for q in answered.questions)


def test_a_settled_layout_still_asks_the_side_at_once():
    """Negative of the deferral: no block, no open layout question -> the side is asked now."""
    data = _main(TRUE, view="unknown")
    ext._require_declared_view(data)
    assert any(q["field"] == "contacts_view" for q in data["_questions"])


# ── Generality audit of 2026-11-14 ─────────────────────────────────────

#: A mirror-symmetric four-pad set (rectangle corners): the holes cannot
#: tell a reading from its mirror image.
RECT = {"1": (2.0, 2.0), "2": (18.0, 2.0), "3": (18.0, 10.0), "4": (2.0, 10.0)}
RECT_X = {"axis": "x", "stops": ["edge", "1,4", "2,3", "edge"], "values": [2.0, 16.0, 2.0]}
#: Y misread: pads 3 and 4 one millimetre high, the sum 10 mm against 12 mm.
RECT_WRONG_Y = {"axis": "y", "stops": ["edge", "1,2", "3,4", "edge"], "values": [2.0, 7.0, 1.0]}
#: Main coordinates with X wrong (pads 2 and 3 two millimetres in).
RECT_MAIN = dict(RECT, **{"2": (16.0, 2.0), "3": (16.0, 10.0)})


@pytest.mark.parametrize("dedicated_view, offered", [("top", True), ("bottom", False)])
def test_no_recombination_across_two_declared_views_that_differ(dedicated_view, offered):
    """M1: the same readings, the dedicated one declaring the other face -> no recombination."""
    data = _main(RECT_MAIN, view="top")
    apply_dedicated_pads(data, _reading([RECT_X, RECT_WRONG_Y], view=dedicated_view),
                         check_layout=FakeHoles(RECT))
    question = _layout_question(data)
    assert any(o["value"].startswith(RECOMBINED_SOURCE) for o in question["options"]) is offered


class _RefuteMainOnly:
    """The holes refute the main coordinates and cannot check anything else."""

    def __init__(self, main):
        self.main = _offsets(main)

    def __call__(self, pads, body):
        if _offsets(pads) == self.main:
            return _Verdict("not_confirmed", "no_match")
        return _Verdict("unverifiable", "not_a_drawing")


def test_an_unconfirmed_recombination_does_not_bring_back_a_refuted_reading():
    """M2: beside an UNconfirmed recombination the refuted main is filtered, «keep blocked» offered."""
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]),
                         check_layout=_RefuteMainOnly(MAIN_WRONG_X))
    question = _layout_question(data)
    values = [o["value"] for o in question["options"]]
    assert not any(v.startswith("main") for v in values), "the refuted reading is not offered"
    assert any(v.startswith(RECOMBINED_SOURCE) for v in values)
    assert any(v.startswith(LAYOUT_BLOCK_KIND) for v in values)
    assert not any(o.get("recommended") for o in question["options"])


def test_a_confirmed_recombination_keeps_the_refuted_reading_beside_it():
    """Negative of M2: a CONFIRMED recombination keeps the refuted main as the alternative."""
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]), check_layout=FakeHoles(TRUE))
    values = [o["value"] for o in _layout_question(data)["options"]]
    assert any(v.startswith("main") for v in values)
    assert not any(v.startswith(LAYOUT_BLOCK_KIND) for v in values), "one option is recommended"


def test_readings_ordering_the_numbers_differently_give_no_recommendation():
    """M5: the main answer puts pads 2 and 5 LEFT of pad 1; the dedicated X chain does not."""
    main = dict(TRUE, **{"2": (1.0, 5.0), "5": (1.0, 9.0)})
    data = _main(main)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]), check_layout=FakeHoles(TRUE))
    question = _layout_question(data)
    recombined = next(o for o in question["options"] if o["value"].startswith(RECOMBINED_SOURCE))
    assert not recombined.get("recommended")
    assert "non i numeri dei pad" in recombined["label"]
    assert any(o["value"].startswith(LAYOUT_BLOCK_KIND) for o in question["options"])


def test_the_recommended_recombination_says_the_numbers_are_not_checked():
    """Positive twin of M5: same order on both axes -> recommended, with the numbering clause."""
    data = _main(MAIN_WRONG_X)
    apply_dedicated_pads(data, _reading([CHAINS[0], WRONG_Y]), check_layout=FakeHoles(TRUE))
    recommended = next(o for o in _layout_question(data)["options"] if o.get("recommended"))
    assert "non i numeri dei pad" in recommended["label"]


def test_the_last_resort_never_offers_a_layout_with_coincident_pads():
    """L1: the dedicated reading puts pads 2 and 5 on one point; the main answer is refuted."""
    coincident_y = {"axis": "y", "stops": ["edge", "1,3", "2,5", "4", "edge"],
                    "values": [2.0, 3.0, 4.0, 1.0]}
    data = _main(MAIN_WRONG_XY)
    apply_dedicated_pads(data, _reading([CHAINS[0], coincident_y]), check_layout=FakeHoles(TRUE))
    values = [o["value"] for o in _layout_question(data)["options"]]
    assert not any(v.startswith("dedicated") for v in values)
    assert any(v.startswith("main") for v in values) and any(
        v.startswith(LAYOUT_BLOCK_KIND) for v in values)


def _params(data):
    return ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                           package_geometry=data["package_geometry"],
                           questions=list(data["_questions"]), candidates=data["_candidates"])


def test_keep_blocked_brings_back_the_side_of_the_provisional_pads():
    """M4: the side deferred with the layout becomes pending again after «keep blocked»."""
    data = _main(MAIN_WRONG_XY, view="unknown")
    apply_dedicated_pads(data, _reading([WRONG_X, WRONG_Y], view="unknown"),
                         check_layout=FakeHoles(TRUE))
    ext._require_declared_view(data)
    assert not any(q["field"] == "contacts_view" for q in data["_questions"])
    question = _layout_question(data)
    block = next(o["value"] for o in question["options"] if o["value"].startswith(LAYOUT_BLOCK_KIND))
    kept = apply_answers(_params(data), {question["key"]: block})
    assert kept.pin_map_conflict
    assert any(q["field"] == "contacts_view" for q in kept.questions)


def test_a_saved_side_answer_matches_the_chosen_candidate():
    """M4: the side key of a candidate is built in the drawing frame, so a saved answer applies."""
    first = _main(MAIN_WRONG_X, view="unknown")
    apply_dedicated_pads(first, _reading([CHAINS[0], WRONG_Y], view="unknown"),
                         check_layout=FakeHoles(TRUE))
    ext._require_declared_view(first)
    question = _layout_question(first)
    chosen = next(o["value"] for o in question["options"] if o.get("recommended"))
    side_key = first["_candidates"][question["key"]][chosen]["view_question"]["key"]
    again = _main(MAIN_WRONG_X, view="unknown")
    again["_answers"] = {side_key: "bottom"}
    apply_dedicated_pads(again, _reading([CHAINS[0], WRONG_Y], view="unknown"),
                         check_layout=FakeHoles(TRUE))
    ext._require_declared_view(again)
    answered = apply_answers(_params(again), {question["key"]: chosen})
    assert answered.package_geometry["source_view"] == "bottom"
    assert not any(q["field"] == "contacts_view" for q in answered.questions)
