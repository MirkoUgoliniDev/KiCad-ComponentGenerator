"""@brief One placement whoever chose the layout, partial re-reads that settle nothing else, answers that never contradict in silence.

Review of 2026-11-10 (findings A4, A5, A11 of the extraction):
- A4: a pad layout chosen through the «pad_layout» question (a saved answer
  or a fresh one) must land exactly where the same layout lands when the
  drawn holes or an agreeing reading choose it;
- A5: a partial re-read confronts one datum; everything else stays as the
  analysis settled it (`merge_reread`), and alone it unblocks nothing;
- A11: two saved answers that cannot both be true never decide by
  themselves; a new answer drops the ones it supersedes.

Invented parts, stub witnesses, a fake `call=`: no network, no AI call. Every
positive has a negative twin that differs for a structural reason.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import apply_dedicated_pads, pads_from_chains

# Invented asymmetric five-pad part, page orientation (Y down), mm.
PART = {"1": (0.0, 0.0), "2": (10.0, 0.0), "3": (10.0, 6.0), "4": (2.5, 6.0), "5": (5.0, 3.0)}
# A main answer that misread it: another row spacing, pad 5 elsewhere.
MISREAD = {"1": (0.0, 0.0), "2": (10.0, 0.0), "3": (10.0, 4.0), "4": (2.5, 4.0), "5": (7.0, 2.0)}
BODY = (16.0, 12.0)
# The printed chains of PART: X starts 1 mm inside the body outline, Y has no outline quote.
CHAIN_X_ONE_EDGE = {"axis": "x", "kind": "consecutive", "stops": ["edge", "1", "4", "5", "2,3"],
                    "values": [1.0, 2.5, 2.5, 5.0]}
CHAIN_X = {"axis": "x", "kind": "consecutive", "stops": ["1", "4", "5", "2,3"],
           "values": [2.5, 2.5, 5.0]}
CHAIN_Y = {"axis": "y", "kind": "consecutive", "stops": ["1,2", "5", "3,4"], "values": [3.0, 3.0]}


def _verdict(status, reason):
    return SimpleNamespace(status=status, reason=reason, image="x", max_error_mm=0.0,
                           tolerance_mm=0.1, circles=5, scale_observable=True)


class _Holes:
    """Check stub: confirms the layouts with the signed offsets of `only`, refutes the others."""

    def __init__(self, only):
        self.only = only

    def __call__(self, pads, body):
        if ext._offset_disagreements(self.only, pads, sorted(self.only), 0.05):
            return _verdict("not_confirmed", "no_match")
        return _verdict("confirmed", "confirmed")


def _no_drawing(pads, body):
    """Check stub of a project without a drawing the holes can be found in."""
    return SimpleNamespace(status="unverifiable", reason="not_a_drawing", image=None,
                           max_error_mm=None, tolerance_mm=None, circles=0)


def _pin(number, x, y):
    return {"number": number, "x_mm": x, "y_mm": y, "pad_type": "thru_hole", "drill_mm": 1.0,
            "pad_length_mm": 1.8, "pad_width_mm": 1.8, "shape": "circle"}


def _answer(pads, origin="custom", view="top"):
    """A main answer before canonicalisation: complete explicit pads and a 16 x 12 body, mm."""
    return {"drawing_unit": "mm", "contacts_view": view, "confidence": "high",
            "body_length_mm": BODY[0], "body_width_mm": BODY[1],
            "value_sources": {"body_length_mm": "read", "body_width_mm": "read"},
            "package_geometry": {"family": "unknown", "origin": origin, "source_view": "top",
                                 "body_length_mm": BODY[0], "body_width_mm": BODY[1],
                                 "pins": [_pin(n, x, y) for n, (x, y) in pads.items()]}}


def _reading(*chains):
    return dict(pads_from_chains(list(chains)), view="top", notes="")


def _placement(data_or_params):
    """(origin, {number: (x, y)}) of an answer dict or of an ExtractedParams."""
    geometry = (data_or_params["package_geometry"] if isinstance(data_or_params, dict)
                else data_or_params.package_geometry)
    return geometry["origin"], {pin["number"]: (pin["x_mm"], pin["y_mm"]) for pin in geometry["pins"]}


def _confront(pads, chains, witness, answers=None, origin="custom"):
    data = _answer(pads, origin)
    if answers is not None:
        data["_answers"] = answers
    apply_dedicated_pads(data, _reading(*chains), check_layout=witness)
    return data


# ---------------------------------------------------------------------------
# A4: one writer for every path
# ---------------------------------------------------------------------------

def _dedicated_value(data):
    (key, payloads), = data["_candidates"].items()
    return key, next(value for value in payloads if value.startswith("dedicated"))


@pytest.mark.parametrize("chains", [(CHAIN_X_ONE_EDGE, CHAIN_Y), (CHAIN_X, CHAIN_Y)])
def test_a_layout_lands_the_same_whoever_chose_it(chains):
    """The holes, a saved answer and a fresh answer write one placement of the same reading.

    With the edge quote the body is placed by it along X (origin body
    centre); the twin without any outline quote keeps the reading's own
    frame (origin custom): the three paths must agree in both cases.
    """
    by_holes = _confront(MISREAD, chains, _Holes(PART))
    assert "_pin_map_conflict" not in by_holes and "fori disegnati" in by_holes["_numbering_note"]
    asked = _confront(MISREAD, chains, _no_drawing)
    key, value = _dedicated_value(asked)
    by_saved_answer = _confront(MISREAD, chains, _no_drawing, answers={key: value})
    fresh = ext.apply_answers(ext._params_from_tool_arguments(asked), {key: value})
    expected = _placement(by_holes)
    assert _placement(by_saved_answer) == expected
    assert _placement(fresh) == expected
    assert expected[0] == ("body_center" if chains[0] is CHAIN_X_ONE_EDGE else "custom")
    # The writer's own remark about the axis it had to estimate travels with the choice.
    estimated = "corpo centrato sui pad, STIMA" in by_holes.get("notes", "")
    assert ("corpo centrato sui pad, STIMA" in by_saved_answer.get("notes", "")) == estimated
    assert ("corpo centrato sui pad, STIMA" in fresh.notes) == estimated


def test_an_edge_quote_places_the_body_of_a_layout_the_user_chose():
    """The one-edge quote (1 mm from the outline to pad 1) is used, not dropped."""
    asked = _confront(MISREAD, (CHAIN_X_ONE_EDGE, CHAIN_Y), _no_drawing)
    key, value = _dedicated_value(asked)
    origin, pins = _placement(ext.apply_answers(ext._params_from_tool_arguments(asked), {key: value}))
    # Pad 1 sits 1 mm inside a 16 mm body: 1 - 8 = -7 from the body centre.
    assert origin == "body_center" and pins["1"][0] == pytest.approx(-7.0)


@pytest.mark.parametrize("origin", ["body_center", "custom"])
def test_choosing_the_main_coordinates_keeps_the_frame_the_answer_declared(origin):
    """A declared body-centre frame survives the choice; an undeclared one is not invented."""
    asked = _confront(MISREAD, (CHAIN_X, CHAIN_Y), _no_drawing, origin=origin)
    (key, payloads), = asked["_candidates"].items()
    value = next(v for v in payloads if v.startswith("main"))
    before = _placement(asked)
    chosen = ext.apply_answers(ext._params_from_tool_arguments(asked), {key: value})
    assert _placement(chosen) == before and before[0] == origin
    saved = _confront(MISREAD, (CHAIN_X, CHAIN_Y), _no_drawing, answers={key: value}, origin=origin)
    assert _placement(saved) == before


# ---------------------------------------------------------------------------
# A5: a partial re-read settles its own scope only
# ---------------------------------------------------------------------------

def _drawing(tmp_path):
    path = tmp_path / "plan.png"
    assert cv2.imwrite(str(path), np.full((64, 64, 3), 255, np.uint8))
    return path


def _height_call(**_kwargs):
    return {"drawing_unit": "mm", "body_height": 10.0,
            "printed_vertical_quotes": [{"value": 10.0, "measures": "body_height"}]}


def _settled():
    """What an analysis settled: the confirmed PART layout, a view question still open."""
    params = ext._params_from_tool_arguments(_answer(PART, origin="body_center"))
    params.body_height_mm = 7.0
    params.package_geometry["body_height_mm"] = 7.0
    params.questions = [{"key": "contacts_view:x", "field": "contacts_view", "text": "?",
                         "options": [{"label": "t", "value": "top"}]},
                        {"key": "body_height_mm:7.0|9.0", "field": "body_height_mm", "text": "?",
                         "options": [{"label": "7", "value": 7.0}]}]
    return params


def test_a_height_reread_alone_never_passes_the_stored_pads_as_settled(tmp_path):
    """The stored main answer's pads were confronted with nothing here."""
    stored = _answer(MISREAD)
    reread = ext.reread_body_height(stored, [_drawing(tmp_path)], model="m", call=_height_call)
    assert reread.body_height_mm == pytest.approx(10.0)
    # Contract of 2026-11-11 (review E5): the partial result also names the
    # notes of its own scope, which is what `merge_reread` imports.
    assert {key: reread.partial_reread[key] for key in ("field", "body_axes_measured")} == {
        "field": "body_height_mm", "body_axes_measured": False}
    assert reread.pin_map_conflict and "non riconfrontate" in reread.pin_map_conflict
    # The block about the unconfronted pads is a note of the result, never of the scope.
    assert "non riconfrontate" in reread.notes
    assert not any("non riconfrontate" in line for line in reread.partial_reread["notes"])
    # Negative twin: a stored answer without explicit pads has nothing to block.
    grid = {"drawing_unit": "mm", "rows": 1, "cols": 3, "pitch_mm": 2.54, "pad_diameter_mm": 1.6,
            "drill_mm": 0.8, "confidence": "high"}
    plain = ext.reread_body_height(grid, [_drawing(tmp_path)], model="m", call=_height_call)
    assert plain.pin_map_conflict is None and plain.body_height_mm == pytest.approx(10.0)


def test_a_height_reread_changes_the_height_and_nothing_the_analysis_settled(tmp_path):
    settled = _settled()
    stored = _answer(MISREAD)
    stored["body_length_mm"] = stored["package_geometry"]["body_length_mm"] = 12.0
    stored["body_width_mm"] = stored["package_geometry"]["body_width_mm"] = 16.0
    reread = ext.reread_body_height(stored, [_drawing(tmp_path)], model="m", call=_height_call)
    merged = ext.merge_reread(settled, reread)
    assert merged.body_height_mm == pytest.approx(10.0)
    assert merged.package_geometry["body_height_mm"] == pytest.approx(10.0)
    # Pads, frame, plan sizes and the pad block are the settled ones, not the raw answer's.
    assert _placement(merged) == _placement(settled)
    assert (merged.body_length_mm, merged.body_width_mm) == BODY
    assert merged.pin_map_conflict is None and merged.partial_reread is None
    # The view question stays open; the settled height question is replaced by the re-read's (none).
    assert [question["field"] for question in merged.questions] == ["contacts_view"]
    # The settled result itself is not modified.
    assert settled.body_height_mm == 7.0


def test_a_pad_reread_changes_the_pads_and_keeps_the_settled_body(tmp_path):
    """Negative twin of the above: the other scope, the other half kept."""
    settled = _settled()
    settled.pin_map_conflict = "blocked before"
    stored = _answer(PART)
    stored["body_height_mm"] = stored["package_geometry"]["body_height_mm"] = 19.3

    def call(**_kwargs):
        return {"drawing_unit": "mm", "contacts_view": "top", "chains": [CHAIN_X, CHAIN_Y]}

    reread = ext.reread_pad_positions(stored, [_drawing(tmp_path)], model="reader", call=call)
    assert reread.partial_reread["field"] == "pad_layout"
    # Alone, the unconfronted height of the stored answer is held back.
    assert reread.body_height_mm == pytest.approx(19.3) and "non riconfrontate" in reread.body_conflict
    merged = ext.merge_reread(settled, reread)
    assert merged.body_height_mm == 7.0 and merged.package_geometry["body_height_mm"] == 7.0
    assert merged.body_conflict is None
    assert merged.pin_map_conflict == reread.pin_map_conflict
    assert _placement(merged)[1] == _placement(reread)[1]
    # The height question of the analysis stays; its view question is the re-read's to ask.
    assert [question["field"] for question in merged.questions] == ["body_height_mm"]


def test_merging_needs_a_partial_result_and_a_settled_one(tmp_path):
    reread = ext.reread_body_height(_answer(MISREAD), [_drawing(tmp_path)], model="m",
                                    call=_height_call)
    assert ext.merge_reread(None, reread) is reread
    full = dataclasses.replace(reread, partial_reread=None)
    assert ext.merge_reread(_settled(), full) is full


# ---------------------------------------------------------------------------
# A11: saved answers that contradict each other
# ---------------------------------------------------------------------------

def _view_data(pads, answers=None):
    data = _answer(pads, view="unknown")
    if answers is not None:
        data["_answers"] = answers
    return data


def _view_key(pads):
    data = _view_data(pads)
    ext._require_declared_view(data)
    (question,) = data["_questions"]
    return question["key"]


def test_a_saved_view_is_applied_only_when_no_other_saved_answer_contradicts_it():
    key, other_layout = _view_key(PART), _view_key(MISREAD)
    assert key != other_layout  # same pad numbers, another layout: another key
    alone = _view_data(PART, {key: "bottom"})
    ext._require_declared_view(alone)
    assert alone["contacts_view"] == "bottom" and "_questions" not in alone
    # The same pads answered the other way for another layout: asked again, nothing applied.
    contradicted = _view_data(PART, {key: "bottom", other_layout: "top"})
    ext._require_declared_view(contradicted)
    assert contradicted["contacts_view"] == "unknown"
    (question,) = contradicted["_questions"]
    assert question["key"] == key and "si contraddicono" in question["text"]
    # Negative twin: the other layout answered the SAME way contradicts nothing.
    agreeing = _view_data(PART, {key: "bottom", other_layout: "bottom"})
    ext._require_declared_view(agreeing)
    assert agreeing["contacts_view"] == "bottom"
    # Negative twin: the opposite side saved for OTHER pad numbers is another question.
    renamed = {"A" + number: xy for number, xy in MISREAD.items()}
    unrelated = _view_data(PART, {key: "bottom", _view_key(renamed): "top"})
    ext._require_declared_view(unrelated)
    assert unrelated["contacts_view"] == "bottom"


def test_an_answer_saved_for_another_layout_is_never_applied_to_this_one():
    """What the layout digest in the key protects: no exact key, no automatic answer."""
    data = _view_data(PART, {_view_key(MISREAD): "bottom"})
    ext._require_declared_view(data)
    assert data["contacts_view"] == "unknown" and len(data["_questions"]) == 1


def test_remembering_an_answer_drops_only_the_ones_it_supersedes():
    key, other_layout = _view_key(PART), _view_key(MISREAD)
    saved = {other_layout: "top", "body_height_mm:16.9|19.3": 16.9}
    kept = ext.remember_answer(saved, key, "bottom")
    assert kept == {key: "bottom", "body_height_mm:16.9|19.3": 16.9}
    assert other_layout in saved  # the input is not modified
    # Negative twin: the same side for the other layout is kept.
    assert ext.remember_answer(saved, key, "top") == dict(saved, **{key: "top"})
    assert ext.contradicting_answers(kept, key) == []
    assert ext.contradicting_answers(dict(saved, **{key: "bottom"}), key) == [other_layout]
    assert ext.remember_answer(None, key, "top") == {key: "top"}


def test_candidate_answers_contradict_when_one_question_refused_what_the_other_chose():
    first, second = "pad_layout:dedicated:aaaa1111|main:bbbb2222", "pad_layout:dedicated:aaaa1111|main:cccc3333"
    # The layout aaaa1111 was refused in one question and chosen in the other.
    assert ext.answers_contradict(first, "main:bbbb2222", second, "dedicated:aaaa1111")
    # Chosen in both, whichever reading brought it: no contradiction.
    assert not ext.answers_contradict(first, "dedicated:aaaa1111", second, "chains+dedicated:aaaa1111")
    # Two choices that were never on offer together say nothing about each other.
    assert not ext.answers_contradict(first, "main:bbbb2222", second, "main:cccc3333")
    heights = ("body_height_mm:16.9|19.3", 16.9, "body_height_mm:16.9|18.9", 18.9)
    assert ext.answers_contradict(*heights)
    assert not ext.answers_contradict("body_height_mm:16.9|19.3", 16.9, "body_height_mm:16.9|18.9", 16.9)
    # Different fields, and one same key, never contradict.
    assert not ext.answers_contradict(first, "main:bbbb2222", "contacts_view:1|2|aaaa1111", "top")
    assert not ext.answers_contradict(first, "main:bbbb2222", first, "dedicated:aaaa1111")
