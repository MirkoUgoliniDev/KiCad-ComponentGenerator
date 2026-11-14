"""@brief VE12H in the window: the deferred terminal-view question and «Lascia bloccato» (2026-11-14).

@details The extractor no longer asks the side of the pads (top or bottom
view) while the pad layout is still open: it asks it in the result of the
pad-layout answer. The window asks that opened question right after the
layout answer, in «Rigenera footprint» and in the analysis pass alike; a
saved answer is reused. The side is a FOOTPRINT question: «Torna indietro»
(back) on it leaves it open and writes no footprint (GUI review H1).

«Lascia bloccato» (keep blocked) adopts no reading: the window then writes
no footprint from the provisional pads (a hard block naming «Piazzole del
componente THT») until the user enters the pad positions by hand and
confirms them all, forgets the answers, or repeats the analysis. The side of
the provisional pads is neither asked nor kept.

The project's saved answers that are no open question go to
`apply_answers` together with the layout answer, so a side already answered
for the chosen candidate is applied by the extractor and not asked again.

Offline: `apply_answers` is replaced by a fake that reproduces the
extractor's contract (an answered key leaves the questions; a chosen layout
applies a side answer given with it or opens the side question; «blocked»
puts the side question back), so the window's flow is tested independently
of the extractor's internals. Positive and negative cases.
"""

from copy import deepcopy

from PyQt6.QtWidgets import QApplication, QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services import datasheet_extractor as ext
from tests.test_footprint_draft import project  # noqa: F401 - pytest fixture
from tests.test_package_pad_dialog import _drive, _tht_index

LAYOUT_KEY = "pad_layout:ve12h-test"
BLOCKED = f"{ext.LAYOUT_BLOCK_KIND}:blocked"
LAYOUT_QUESTION = {"key": LAYOUT_KEY, "field": "pad_layout", "text": "Quale disposizione?",
                   "evidence": None,
                   "options": [{"label": "Lettura principale", "value": "main"},
                               {"label": "Lascia bloccato", "value": BLOCKED}]}
VIEW_KEY = "contacts_view:1,2,3|digest"
VIEW_QUESTION = {"key": VIEW_KEY, "field": "contacts_view", "text": "Da quale lato?",
                 "evidence": None,
                 "options": [{"label": "Dall'alto", "value": "top"},
                             {"label": "Dal basso", "value": "bottom"}]}


def _params(questions=(LAYOUT_QUESTION,)):
    """A settled single-part extraction holding `questions` (no package: nothing to rebuild).

    The candidates are what the window checks before asking a pad layout (the
    payloads its options write); the fake `apply_answers` does not read them.
    """
    return ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                               layout="package", questions=[deepcopy(q) for q in questions],
                               candidates={LAYOUT_KEY: {"main": {}, BLOCKED: {}}})


def _fake_apply(calls):
    """A stand-in for `apply_answers` with the extractor's contract, recording each call."""
    def apply(params, answers):
        calls.append(dict(answers))
        result = deepcopy(params)
        listed = {q["key"] for q in result.questions or ()}
        remaining = [q for q in result.questions or () if q["key"] not in answers]
        layout = answers.get(LAYOUT_KEY) if LAYOUT_KEY in listed else None
        if layout == "main" and VIEW_KEY in answers:
            # The candidate's side question answered in the same call.
            result.notes = f"view {answers[VIEW_KEY]}"
        elif layout is not None:
            # The chosen layout (or the provisional pads kept blocked)
            # declares no view: its side question is open, once.
            remaining.append(deepcopy(VIEW_QUESTION))
        if VIEW_KEY in answers and VIEW_KEY in listed:
            result.notes = f"view {answers[VIEW_KEY]}"
        result.questions = remaining or None
        return result
    return apply


def _prepare(window, monkeypatch, params, script):
    """The window holding the open questions of `params`; answers scripted by field.

    The settled extraction is the one the window last committed, `params`
    before any answer (as `_settled_extraction` reads `_last_extracted`).
    @return (asked fields in order, apply_answers calls, logged lines).
    """
    asked, calls, logged = [], [], []
    window._extraction_questions = window._valid_extraction_questions(params.questions)
    window._extraction_answers = {}
    window._pads_kept_blocked = False
    window._last_extracted = params
    monkeypatch.setattr(window, "_settled_extraction", lambda: window._last_extracted)
    monkeypatch.setattr(window, "_ask_extraction_question",
                        lambda question: asked.append(question["field"]) or script[question["field"]])
    monkeypatch.setattr(ext, "apply_answers", _fake_apply(calls))
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: logged.append("WARNING")))
    monkeypatch.setattr(gui.QMessageBox, "information",
                        staticmethod(lambda *a, **k: logged.append("INFORMATION")))
    for name in ("_dropped_footprint_pads", "_pads_with_unread_holes", "_pads_with_unfit_values",
                 "_holes_read_elsewhere", "_unquoted_unread_pads"):
        monkeypatch.setattr(window, name, lambda *a, **k: [])
    monkeypatch.setattr(window, "_unread_pads_account", lambda *a, **k: None)
    return asked, calls, logged


def _keys(window):
    """The keys of the window's open questions, in order."""
    return [q["key"] for q in window._extraction_questions]


# ── 1. The deferred side question ───────────────────────────────────────

def test_the_side_question_opened_by_the_layout_answer_is_asked_next(window, monkeypatch):
    """@brief Positive: layout answered -> the side question it opened is asked and applied."""
    asked, calls, _logged = _prepare(window, monkeypatch, _params(),
                                     {"pad_layout": "main", "contacts_view": "bottom"})
    assert window._resolve_stored_pad_layout_question() is True
    assert asked == ["pad_layout", "contacts_view"]
    assert calls == [{LAYOUT_KEY: "main"}, {VIEW_KEY: "bottom"}]
    assert window._last_extracted.notes == "view bottom"
    assert window._extraction_answers == {LAYOUT_KEY: "main", VIEW_KEY: "bottom"}
    assert window._extraction_questions == []
    assert window._pads_kept_blocked is False


def test_back_on_the_opened_side_question_writes_no_footprint(window, monkeypatch):
    """@brief Negative: «Torna indietro» on the side -> nothing written, the side blocks and is asked next.

    CONTRACT CHANGED 2026-11-14 (GUI review H1): the window used to report
    the layout answer as resolved (True) and let the footprint be written
    with the view unknown -- a bottom view writes it mirrored. The side is a
    footprint question now: open, it blocks every write and is the next
    question «Rigenera footprint» asks.
    """
    asked, calls, logged = _prepare(window, monkeypatch, _params(),
                                    {"pad_layout": "main", "contacts_view": gui.QUESTION_BACK})
    assert window._resolve_stored_pad_layout_question() is False
    assert asked == ["pad_layout", "contacts_view"]
    assert calls == [{LAYOUT_KEY: "main"}]
    assert _keys(window) == [VIEW_KEY] and VIEW_KEY not in window._extraction_answers
    assert window._extraction_answers == {LAYOUT_KEY: "main"}, "the layout answer is kept"
    assert "lato da cui sono disegnati i pad" in window._pad_layout_question_block()
    monkeypatch.setattr(window, "_generate_applied", lambda **k: logged.append("WRITTEN") or True)
    assert window._generate(automatic=True) is False and "WRITTEN" not in logged
    # The next «Rigenera footprint» asks the side, and only then writes.
    window._ask_extraction_question = (lambda question: asked.append(question["field"]) or "top")
    assert window._resolve_stored_pad_layout_question() is True
    assert asked[-1] == "contacts_view" and window._last_extracted.notes == "view top"
    assert window._extraction_questions == []


def test_a_saved_side_answer_is_reused_without_asking(window, monkeypatch):
    """@brief Positive: the project already answered the side -> passed with the layout answer.

    CONTRACT CHANGED 2026-11-14: the saved answers that are no open question
    now reach `apply_answers` in the SAME call as the layout answer (the
    extractor applies the candidate's side there), not in a second call.
    """
    asked, calls, _logged = _prepare(window, monkeypatch, _params(),
                                     {"pad_layout": "main", "contacts_view": "top"})
    window._extraction_answers = {VIEW_KEY: "bottom"}
    assert window._resolve_stored_pad_layout_question() is True
    assert asked == ["pad_layout"]
    assert calls == [{VIEW_KEY: "bottom", LAYOUT_KEY: "main"}]
    assert window._last_extracted.notes == "view bottom"
    assert window._extraction_questions == []


def test_saved_answers_ride_along_only_when_free_and_uncontradicted(window, monkeypatch):
    """@brief Negative: a contradicted saved side answer, or one to a listed open question, stays out."""
    asked, calls, _logged = _prepare(window, monkeypatch, _params(),
                                     {"pad_layout": "main", "contacts_view": "top"})
    window._extraction_answers = {VIEW_KEY: "bottom"}
    monkeypatch.setattr(ext, "contradicting_answers",
                        lambda answers, key, value=None: ["other"] if key == VIEW_KEY else [])
    assert window._resolve_stored_pad_layout_question() is True
    assert calls[0] == {LAYOUT_KEY: "main"}, "a contradicted saved answer is not applied silently"
    assert asked == ["pad_layout", "contacts_view"], "it is asked instead"
    assert window._last_extracted.notes == "view top"
    # A saved answer whose question is listed open is never sent along.
    other = dict(VIEW_QUESTION, key="contacts_view:other", text="Altro lato?")
    _prepare(window, monkeypatch, _params((LAYOUT_QUESTION, other)), {"pad_layout": "main",
                                                                      "contacts_view": "top"})
    monkeypatch.setattr(ext, "contradicting_answers", lambda answers, key, value=None: [])
    window._extraction_answers = {"contacts_view:other": "bottom"}
    assert window._saved_answers_for_deferred(_params((LAYOUT_QUESTION, other))) == {}


def test_the_analysis_pass_asks_the_opened_side_question(window, monkeypatch):
    """@brief Positive: in the analysis pass too; negative: back on it holds the generation."""
    asked, _calls, _logged = _prepare(window, monkeypatch, _params(),
                                      {"pad_layout": "main", "contacts_view": "top"})
    params, held_back = window._review_extraction_questions(_params())
    assert asked == ["pad_layout", "contacts_view"] and held_back is False
    assert not params.questions and params.notes == "view top"
    assert window._extraction_questions == []
    asked, _calls, _logged = _prepare(window, monkeypatch, _params(),
                                      {"pad_layout": "main", "contacts_view": gui.QUESTION_BACK})
    params, held_back = window._review_extraction_questions(_params())
    assert asked == ["pad_layout", "contacts_view"] and held_back is True
    assert _keys(window) == [VIEW_KEY]
    # A side saved by the project goes along with the layout answer: not asked.
    asked, calls, _logged = _prepare(window, monkeypatch, _params(),
                                     {"pad_layout": "main", "contacts_view": "top"})
    window._extraction_answers = {VIEW_KEY: "bottom"}
    params, held_back = window._review_extraction_questions(_params())
    assert asked == ["pad_layout"] and held_back is False
    assert calls == [{VIEW_KEY: "bottom", LAYOUT_KEY: "main"}] and params.notes == "view bottom"


def test_opened_answers_are_rolled_back_when_the_layout_no_longer_builds(window, monkeypatch):
    """@brief L9: the layout builds, the side answer then breaks it -> nothing committed, all rolled back."""
    asked, _calls, _logged = _prepare(window, monkeypatch, _params(),
                                      {"pad_layout": "main", "contacts_view": "bottom"})
    builds = iter([(True, None), (False, None)])
    monkeypatch.setattr(window, "_package_from_answered", lambda updated: next(builds))
    questions_before = list(window._extraction_questions)
    assert window._resolve_stored_pad_layout_question() is False
    assert asked == ["pad_layout", "contacts_view"]
    assert window._extraction_answers == {} and window._extraction_questions == questions_before
    # Negative: a layout that does not build asks nothing more.
    asked, _calls, _logged = _prepare(window, monkeypatch, _params(),
                                      {"pad_layout": "main", "contacts_view": "bottom"})
    monkeypatch.setattr(window, "_package_from_answered", lambda updated: (False, None))
    assert window._resolve_stored_pad_layout_question() is False
    assert asked == ["pad_layout"] and window._extraction_answers == {}


# ── 2. «Lascia bloccato»: a hard block ─────────────────────────────────

def test_keep_blocked_blocks_every_footprint_write(window, monkeypatch):
    """@brief Positive: «Lascia bloccato» -> nothing written, the block names the remedy."""
    asked, _calls, logged = _prepare(window, monkeypatch, _params(), {"pad_layout": BLOCKED})
    assert window._resolve_stored_pad_layout_question() is False
    assert asked == ["pad_layout"], "the side of pads never written is not asked"
    # The answered question stays listed; the side of the provisional pads is not kept.
    assert _keys(window) == [LAYOUT_KEY]
    assert window._extraction_answers == {LAYOUT_KEY: BLOCKED}
    assert window._pads_kept_blocked is True
    block = window._pad_layout_question_block()
    assert "Piazzole del componente THT" in block and "bloccate per scelta" in block
    assert window._footprint_missing_reason() == block
    code, text = window._kicad_block_now()
    assert code == "pads_kept_blocked" and "Dimentica le risposte" in text
    # Every write stays refused, unattended or not.
    monkeypatch.setattr(window, "_generate_applied", lambda **k: logged.append("WRITTEN") or True)
    assert window._generate(automatic=True) is False and "WRITTEN" not in logged
    logged.clear()
    assert window._resolve_stored_pad_layout_question() is False
    assert "WARNING" in logged and asked == ["pad_layout"], "asked once, then a plain block"


def test_a_chosen_layout_does_not_keep_the_pads_blocked(window, monkeypatch):
    """@brief Negative: a reading chosen -> no block anywhere."""
    _prepare(window, monkeypatch, _params(), {"pad_layout": "main", "contacts_view": "top"})
    assert window._resolve_stored_pad_layout_question() is True
    assert window._pads_kept_blocked is False and window._pad_layout_question_block() == ""
    assert window._footprint_missing_reason() is None
    assert (window._kicad_block_now() or ("",))[0] != "pads_kept_blocked"


def test_forgetting_the_answers_asks_the_layout_again(window, monkeypatch):
    """@brief L7: «Dimentica le risposte» lifts the block and the layout question is asked again."""
    asked, _calls, _logged = _prepare(window, monkeypatch, _params(), {"pad_layout": BLOCKED})
    assert window._resolve_stored_pad_layout_question() is False
    window._forget_extraction_answers()
    assert window._pads_kept_blocked is False and window._extraction_answers == {}
    window._ask_extraction_question = (
        lambda question: asked.append(question["field"]) or {"pad_layout": "main",
                                                              "contacts_view": "top"}[question["field"]])
    assert window._resolve_stored_pad_layout_question() is True
    assert asked == ["pad_layout", "pad_layout", "contacts_view"]


def test_forgetting_waits_for_running_work(window, monkeypatch):
    """@brief M4: while an analysis runs the answers stay and the entry is disabled; idle it works."""
    _prepare(window, monkeypatch, _params(), {"pad_layout": BLOCKED})
    window._extraction_answers = {LAYOUT_KEY: BLOCKED}
    window._pads_kept_blocked = True
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._forget_extraction_answers()
    assert window._extraction_answers == {LAYOUT_KEY: BLOCKED} and window._pads_kept_blocked is True
    window._refresh_reread_actions()
    assert not window.action_forget_answers.isEnabled()
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    window._refresh_reread_actions()
    assert window.action_forget_answers.isEnabled()
    window._forget_extraction_answers()
    assert window._extraction_answers == {} and window._pads_kept_blocked is False


def test_keep_blocked_from_the_analysis_pass_and_a_new_analysis(window, monkeypatch):
    """@brief Positive: chosen in the pass -> blocked; negative: a new pass without it clears it."""
    asked, _calls, _logged = _prepare(window, monkeypatch, _params(), {"pad_layout": BLOCKED})
    _params_out, held_back = window._review_extraction_questions(_params())
    assert window._pads_kept_blocked is True
    assert asked == ["pad_layout"] and held_back is False, "the provisional side is not asked"
    assert _keys(window) == [LAYOUT_KEY]
    _prepare(window, monkeypatch, _params(), {"pad_layout": "main", "contacts_view": "top"})
    window._pads_kept_blocked = True
    window._review_extraction_questions(_params())
    assert window._pads_kept_blocked is False


def test_a_height_reread_keeps_the_pad_state(window, monkeypatch):
    """@brief H2: a body-height re-read leaves «keep blocked» and the side-numbering key as they were.

    Negative: the same result applied as a whole analysis recomputes them.
    """
    _prepare(window, monkeypatch, _params(), {})
    window._pads_kept_blocked = True
    window._side_numbering_key = "side_numbering:kept"
    window._pin_numbers_printed = False
    reread = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 layout="package", body_height_mm=4.2,
                                 partial_reread={"field": "body_height_mm"})
    window._apply_reread_height_alone(reread)
    assert window._pads_kept_blocked is True
    assert window._side_numbering_key == "side_numbering:kept"
    assert window._pin_numbers_printed is False
    assert window.field_body_height.value() == 4.2
    window._review_extraction_questions(deepcopy(reread))
    assert window._pads_kept_blocked is False and window._side_numbering_key is None


def test_keep_blocked_is_saved_with_the_project(window):
    """@brief Positive: saved and restored; negative: a state without it is not blocked."""
    window._pads_kept_blocked = True
    state = window._project_state()
    assert state.get("pads_kept_blocked") is True
    window._pads_kept_blocked = False
    window._apply_project_state(deepcopy(state))
    assert window._pads_kept_blocked is True
    state.pop("pads_kept_blocked")
    window._apply_project_state(deepcopy(state))
    assert window._pads_kept_blocked is False


def test_pad_positions_lift_the_block_only_when_all_are_confirmed(project, monkeypatch):  # noqa: F811
    """@brief M5: moved pads + «Sì» lift the block; «No», or a drill edit alone, keep it."""
    window, _generations = project
    confirmations = []
    answer = {"value": False}
    monkeypatch.setattr(window, "_confirm_pads_verified",
                        lambda: confirmations.append(1) or answer["value"])
    window._extraction_answers = {LAYOUT_KEY: BLOCKED}
    window._extraction_questions = window._valid_extraction_questions([LAYOUT_QUESTION, VIEW_QUESTION])
    window._pads_kept_blocked = True
    _drive(monkeypatch, drill=0.5)
    window._edit_package_pad(_tht_index(window))
    assert window.apply_footprint_draft()
    assert window._pads_kept_blocked is True and confirmations == [], "a drill edit asks nothing"
    assert window._move_package_pads([_tht_index(window)], 0.5, 0.0)
    assert window.apply_footprint_draft()
    assert window._pads_kept_blocked is True and confirmations == [1], "«No» keeps the block"
    answer["value"] = True
    assert window._move_package_pads([_tht_index(window)], 0.5, 0.0)
    assert window.apply_footprint_draft()
    QApplication.processEvents()
    assert window._pads_kept_blocked is False and confirmations == [1, 1]
    assert window._extraction_questions == [], "the blocked layout and the provisional side are settled"


def test_a_pending_draft_is_settled_before_the_block_refuses(project, monkeypatch):  # noqa: F811
    """@brief L6: a command with hand-moved pads in the draft applies them first, then writes."""
    window, _generations = project
    written = []
    monkeypatch.setattr(window, "_generate_applied", lambda **k: written.append(k) or True)
    monkeypatch.setattr(window, "_confirm_pads_verified", lambda: True)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    window._pads_kept_blocked = True
    assert window._move_package_pads([_tht_index(window)], 0.5, 0.0)
    assert gui.MainWindow._generate(window) is True and written
    assert window._pads_kept_blocked is False
    # Negative: without a draft the block refuses at once.
    window._pads_kept_blocked = True
    written.clear()
    assert gui.MainWindow._generate(window) is False and written == []


# ── 3. Stored footprint questions without candidates (M3) ─────────────

def test_questions_applied_from_their_value_need_no_candidates(window):
    """@brief Positive: side, row spacing, contact type listed -> answerable; negative: a pad layout is not."""
    for field in ("contacts_view", "row_spacing", "contact_style"):
        question = dict(VIEW_QUESTION, key=f"{field}:k", field=field)
        params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                     questions=[question], candidates=None)
        assert window._extraction_holds_question(params, f"{field}:k"), field
    params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 questions=[dict(LAYOUT_QUESTION)], candidates=None)
    assert not window._extraction_holds_question(params, LAYOUT_KEY)
    assert not window._extraction_holds_question(params, "row_spacing:not-listed")


# ── 4. Preview labels of a duplicated pin number (L8) ─────────────────

def test_the_first_row_names_a_duplicated_pin_in_the_preview(window):
    """@brief Positive: the first row wins, as in the pad tooltip; negative: distinct numbers each keep theirs."""
    window.pinout = [{"pin": "1", "name": "FIRST"}, {"pin": "1", "name": "SECOND"},
                     {"pin": "2", "name": "OTHER"}]
    labels = window._preview_pin_labels()
    assert labels["1"] == "FIRST" and labels["2"] == "OTHER"
