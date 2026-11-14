"""@brief Three program defects the model benchmark of 2026-11-14 exposed, after its generality audit.

1. A reasoning model spent the completion budget before the tool call and
   the answer was lost: the logged wrapper (every caller's) now asks ONCE
   more with a larger budget (the model's declared limit, else a declared
   cap), announced and paid; a second cut, or any other failure of that
   attempt, is reported together with the truncation. Reasoning effort is
   never changed.
2. Two readings with the same pad geometry that DECLARE different views
   ended in a block without a question: now the side is asked, saying that
   the two readings disagreed on it.
3. A pad number given to two sides that the one correction did not fix
   escaped as an exception: now a structured question (moved sides, the
   counter-clockwise convention, keep blocked; none recommended) with the
   footprint blocked until the answer -- or, when the correction call itself
   failed, a block with the error and a retry remedy. Never a silent guess.
No network: the provider answers are faked.
"""
import copy
import json
from unittest.mock import Mock

import pytest

from kicadgen.services import ai_diagnostics
from kicadgen.services import datasheet_extractor as de
from kicadgen.services import openrouter
from minimal_pdf import MINIMAL_PDF
from test_pad_positions_by_drawn_holes import TRUE, _main, _reading


# ── 1: the completion budget ────────────────────────────────────────────

_TOOL = {"type": "function", "function": {"name": "report", "description": "x",
                                          "parameters": {"type": "object",
                                                         "properties": {"a": {"type": "number"}}}}}


def _payload(finish, arguments=None):
    message = {}
    if arguments is not None:
        message["tool_calls"] = [{"function": {"name": "report",
                                               "arguments": json.dumps(arguments)}}]
    return {"choices": [{"finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 16384, "cost": 0.01}}


@pytest.fixture
def chat(monkeypatch, tmp_path):
    """The OpenRouter request faked: answers (or errors) in order, every body kept, usage counted."""
    sent, recorded = [], []
    path = tmp_path / "models.json"
    path.write_text(json.dumps([{"id": "vendor/thinker", "top_provider": {
        "max_completion_tokens": 40000}, "supported_parameters": ["tools", "reasoning"]}]),
        encoding="utf-8")
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", path)
    monkeypatch.setattr(openrouter, "api_key", lambda: "offline")
    monkeypatch.setattr(openrouter, "_record_usage", lambda payload, **k: recorded.append(payload))
    monkeypatch.setattr(ai_diagnostics, "_sleep", lambda seconds: None)
    answers = []

    def post(request, _blocks):
        sent.append(json.loads(request.data.decode("utf-8")))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(openrouter, "_post_chat", post)
    return answers, sent, recorded


def _logged(lines):
    return ai_diagnostics.make_logged_call(openrouter.call_tool, lines.append)


def _call(call):
    return call(model="vendor/thinker", system_prompt="sys",
                content_blocks=[{"type": "text", "text": "x"}], tool=_TOOL, max_tokens=16384)


def test_a_truncated_answer_is_asked_once_more_with_a_larger_budget(chat):
    answers, sent, recorded = chat
    answers += [_payload("length"), _payload("stop", {"a": 1})]
    lines = []
    assert _call(_logged(lines)) == {"a": 1}
    assert [body["max_tokens"] for body in sent] == [16384, 40000], "bounded by the declared limit"
    assert all("reasoning" not in body for body in sent), "the reasoning effort is never changed"
    assert len(recorded) == 2, "both attempts are paid and counted"
    assert any("reason=completion_budget max_tokens=40000" in line for line in lines)


def test_a_second_truncation_says_both_never_a_loop(chat):
    answers, sent, _recorded = chat
    answers += [_payload("length"), _payload("length")]
    with pytest.raises(openrouter.TruncatedAnswerError, match="troncata di nuovo"):
        _call(_logged([]))
    assert len(sent) == 2


def test_a_refusal_of_the_larger_request_is_reported_with_the_truncation(chat):
    answers, sent, _recorded = chat
    answers += [_payload("length"), openrouter.OpenRouterError("HTTP 400 max_tokens too large",
                                                               http_status=400)]
    lines = []
    with pytest.raises(openrouter.OpenRouterError) as caught:
        _call(_logged(lines))
    text = str(caught.value)
    assert "Risposta troncata" in text and "Secondo tentativo" in text and "HTTP 400" in text
    # Third review, L8: the second failure itself, with its attributes; the
    # truncation chained; the budget attempt counted in the log.
    assert caught.value.http_status == 400
    assert not isinstance(caught.value, openrouter.TruncatedAnswerError)
    assert isinstance(caught.value.__cause__, openrouter.TruncatedAnswerError)
    assert any("AI_CALL_FAILURE" in line and "attempts=2" in line for line in lines)
    assert len(sent) == 2


def test_a_transient_failure_after_the_budget_retry_is_still_retried(chat):
    answers, sent, _recorded = chat
    answers += [_payload("length"), openrouter.OpenRouterError("HTTP 503", retryable=True),
                _payload("stop", {"a": 3})]
    assert _call(_logged([])) == {"a": 3}
    assert [body["max_tokens"] for body in sent] == [16384, 40000, 40000]


def test_a_direct_call_makes_one_request_and_carries_the_larger_budget(chat):
    """Negative: without the logged wrapper nothing is retried; the error says what one could ask."""
    answers, sent, _recorded = chat
    answers += [_payload("length")]
    with pytest.raises(openrouter.TruncatedAnswerError) as caught:
        _call(openrouter.call_tool)
    assert len(sent) == 1 and caught.value.budget_retry == 40000


def test_the_retry_budget_is_bounded_and_none_when_nothing_larger_exists(monkeypatch, tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps([{"id": "vendor/small", "top_provider": {"max_completion_tokens": 8000}}]),
                    encoding="utf-8")
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", path)
    assert openrouter.completion_budget_retry("vendor/small", 16384) is None
    assert openrouter.completion_budget_retry("vendor/unknown", 16384) == min(
        16384 * openrouter.TRUNCATION_RETRY_FACTOR, openrouter.TRUNCATION_RETRY_CAP_TOKENS)


def test_a_complete_answer_costs_one_call(chat):
    """Negative: no truncation, no retry."""
    answers, sent, _recorded = chat
    answers += [_payload("stop", {"a": 2})]
    assert _call(_logged([])) == {"a": 2}
    assert len(sent) == 1


def test_the_retry_line_reaches_the_analysis_log(monkeypatch, tmp_path):
    """The extractor's own log (its `progress`) gets the AI_CALL_RETRY line of the budget retry."""
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kwargs: None)
    answer = _castellated([_side("left", 1), _side("right", 5)])
    budgets = []

    def fake(model, system_prompt, content_blocks, tool, max_tokens=2048, has_pdf=False,
             pdf_engine="native", compatibility_retry=0):
        budgets.append(max_tokens)
        if len(budgets) == 1:
            error = openrouter.TruncatedAnswerError("Risposta troncata")
            error.budget_retry, error.max_tokens = max_tokens * 2, max_tokens
            raise error
        if tool["function"]["name"] == "observe_contact_styles":
            return {"sides": []}
        return copy.deepcopy(answer)

    monkeypatch.setattr(de, "call_tool", fake)
    source = tmp_path / "board.pdf"
    source.write_bytes(MINIMAL_PDF)
    lines = []
    de.extract_pin_header_params([source], progress=lines.append)
    assert budgets[1] == 2 * budgets[0]
    assert any("AI_CALL_RETRY" in line and "reason=completion_budget" in line for line in lines)


# ── 2: same geometry, different declared views ──────────────────────────

def test_agreeing_geometry_with_different_views_asks_the_side_saying_why():
    data = _main(TRUE, view="top")
    de.apply_dedicated_pads(data, _reading(view="bottom"))
    assert "_pin_map_conflict" not in data, "never a block without a question"
    assert data.get("contacts_view") not in ("top", "bottom")
    de._require_declared_view(data)
    (question,) = [q for q in data["_questions"] if q["field"] == "contacts_view"]
    assert not any(option.get("recommended") for option in question["options"])
    assert "Le due letture dichiarano viste diverse" in question["text"]


def test_an_undeclared_view_keeps_its_own_wording():
    """Negative of L3: nobody declared a side -> «il documento non dice»."""
    data = _main(TRUE, view="unknown")
    de._require_declared_view(data)
    (question,) = [q for q in data["_questions"] if q["field"] == "contacts_view"]
    assert "Il documento non dice" in question["text"]


def test_different_geometry_with_different_views_is_not_settled_this_way():
    """Negative: the readings also disagree on the pads -> the views are not withdrawn."""
    moved = dict(TRUE, **{"2": (7.0, 2.0)})
    data = _main(moved, view="top")
    de.apply_dedicated_pads(data, _reading(view="bottom"))
    assert data.get("contacts_view") == "top"


# ── 3: a pad number on two sides that the correction did not fix ────────

def _side(edge, first, count=4):
    return {"edge": edge, "count": count, "pitch_mm": 2.54, "first_pin": first,
            "contact_style": "half_hole"}


def _castellated(sides):
    return {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 20.0,
            "body_width_mm": 12.0, "contacts_view": "top", "confidence": "medium", "notes": "",
            "pin_numbers_printed": True, "castellated_sides": sides,
            "pinout": [{"pin": str(n), "name": f"P{n}"} for n in range(1, 9)]}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kwargs: None)


def _run(tmp_path, monkeypatch, answers, saved=None):
    source = tmp_path / "board.pdf"
    source.write_bytes(MINIMAL_PDF)
    call = Mock(side_effect=[copy.deepcopy(a) if isinstance(a, dict) else a for a in answers])
    monkeypatch.setattr(de, "call_tool", call)
    params = de.extract_pin_header_params([source], progress=lambda _line: None,
                                          **({"answers": saved} if saved else {}))
    return params, call


BROKEN = _castellated([_side("left", 1), _side("right", 3)])     # pads 3 and 4 on both


def _conflict_question(params):
    return [q for q in params.questions or () if q["field"] == "side_numbering"]


def test_a_conflict_the_correction_repeats_is_asked_and_blocks(offline, tmp_path, monkeypatch):
    params, call = _run(tmp_path, monkeypatch, [BROKEN, BROKEN, {"sides": []}])
    assert call.call_count <= 3
    (question,) = _conflict_question(params)
    assert [o["value"] for o in question["options"]] == [
        de.SIDE_CONFLICT_MOVE, de.SIDE_CONFLICT_CONVENTION, de.SIDE_CONFLICT_BLOCK]
    assert not any(o.get("recommended") for o in question["options"])
    assert params.pin_map_conflict and "chiesta all'utente" in params.pin_map_conflict
    # The side that did not conflict keeps its printed numbers.
    left = next(s for s in params.castellated_sides if s["edge"] == "left")
    assert left["first_pin"] == 1


@pytest.mark.parametrize("choice, blocked", [("conflict_move", False),
                                             ("conflict_convention", False),
                                             ("conflict_block", True)])
def test_the_answers_to_the_conflict(offline, tmp_path, monkeypatch, choice, blocked):
    first, _call_mock = _run(tmp_path, monkeypatch, [BROKEN, BROKEN, {"sides": []}])
    (question,) = _conflict_question(first)
    answered = de.apply_answers(first, {question["key"]: choice})
    assert bool(answered.pin_map_conflict) is blocked
    firsts = sorted(side["first_pin"] for side in answered.castellated_sides)
    assert len(set(firsts)) == 2 and not _conflict_question(answered)


def test_a_failed_correction_call_blocks_with_the_error_and_no_question(offline, tmp_path,
                                                                         monkeypatch):
    params, _call_mock = _run(tmp_path, monkeypatch, [
        BROKEN, de.OpenRouterError("HTTP 402 credito esaurito"), {"sides": []}])
    assert not _conflict_question(params)
    assert "credito esaurito" in params.pin_map_conflict and "Ripeti l'analisi" in params.pin_map_conflict


def test_a_correction_that_fixes_the_numbering_is_used(offline, tmp_path, monkeypatch):
    """Negative: the correction gives disjoint numbers -> applied, no question, no block."""
    fixed = _castellated([_side("left", 1), _side("right", 5)])
    params, call = _run(tmp_path, monkeypatch, [BROKEN, fixed, {"sides": []}])
    assert not _conflict_question(params) and not params.pin_map_conflict
    assert sorted(side["first_pin"] for side in params.castellated_sides) == [1, 5]


# ── third review: one convention path, the conflict marker, coherence after «move» ──

def _relay_sides(pins=8):
    """Left 1-3, right 4-6, bottom declared 6-7 (pad 6 on two sides), pinout A.. on pins 1..pins."""
    answer = _castellated([_side("left", 1, 3), _side("right", 4, 3), _side("bottom", 6, 2)])
    answer["pinout"] = [{"pin": str(n), "name": chr(64 + n)} for n in range(1, pins + 1)]
    return answer


def _conflict_key(params):
    (question,) = [q for q in params.questions if q["key"].startswith("side_numbering:conflict:")]
    return question["key"]


def test_the_live_and_the_saved_convention_answers_give_the_same_pinout(offline, tmp_path,
                                                                         monkeypatch):
    broken = _relay_sides()
    live, _call_mock = _run(tmp_path, monkeypatch, [broken, broken, {"sides": []}])
    key = _conflict_key(live)
    answered = de.apply_answers(live, {key: de.SIDE_CONFLICT_CONVENTION})
    saved, _call_mock = _run(tmp_path, monkeypatch, [broken, broken, {"sides": []}],
                             saved={key: de.SIDE_CONFLICT_CONVENTION})
    def pinout(params):
        return sorted((str(e["pin"]), e["name"]) for e in params.pinout)
    assert pinout(answered) == pinout(saved)
    assert answered.label_conflicts == saved.label_conflicts
    assert sorted((s["edge"], s["first_pin"]) for s in answered.castellated_sides) == sorted(
        (s["edge"], s["first_pin"]) for s in saved.castellated_sides)


def test_a_moved_numbering_is_judged_against_the_pinout_after_a_live_answer(offline, tmp_path,
                                                                             monkeypatch):
    """M4: an entry 9 no pad carries -> the coherence question follows the «move» answer."""
    broken = _relay_sides(pins=9)
    live, _call_mock = _run(tmp_path, monkeypatch, [broken, broken, {"sides": []}])
    answered = de.apply_answers(live, {_conflict_key(live): de.SIDE_CONFLICT_MOVE})
    assert any(q["field"] == "side_numbering" and not q["key"].startswith("side_numbering:conflict:")
               for q in answered.questions)


def test_a_coherent_moved_numbering_asks_nothing_more(offline, tmp_path, monkeypatch):
    """Negative of M4: every entry on a pad -> no further question."""
    broken = _relay_sides(pins=8)
    live, _call_mock = _run(tmp_path, monkeypatch, [broken, broken, {"sides": []}])
    answered = de.apply_answers(live, {_conflict_key(live): de.SIDE_CONFLICT_MOVE})
    assert not any(q["field"] == "side_numbering" for q in answered.questions)


def test_an_unsettled_conflict_holds_the_coherence_question(offline, tmp_path, monkeypatch):
    """M3: a failed correction (no question) still marks the conflict: no coherence question."""
    broken = _relay_sides(pins=9)
    params, _call_mock = _run(tmp_path, monkeypatch, [
        broken, de.OpenRouterError("HTTP 402 credito esaurito"), {"sides": []}])
    assert not any(q["field"] == "side_numbering" for q in params.questions)
    assert params.pin_map_conflict


def test_without_printed_numbers_the_convention_settles_the_conflict(offline, tmp_path,
                                                                     monkeypatch):
    """M3: pin_numbers_printed False -> no conflict question, the convention numbers the sides."""
    broken = _relay_sides()
    broken["pin_numbers_printed"] = False
    params, _call_mock = _run(tmp_path, monkeypatch, [broken, broken, {"sides": []}])
    assert not any(q["field"] == "side_numbering" for q in params.questions)
    assert not params.pin_map_conflict
    assert "numerazione convenzionale" in params.notes
