"""@brief Saved answers that contradict each other and partial re-reads, in the window (review 2026-11-10, A5 / A11).

Two rules the window must keep, whatever the component:

- a saved answer is applied by itself only when no other saved answer
  contradicts it; the answer given now drops the ones it supersedes
  (`datasheet_extractor.contradicting_answers` / `remember_answer`);
- a partial re-read brings ONE datum: the rest of what it carries is the raw
  main answer and never replaces what the analysis settled
  (`datasheet_extractor.merge_reread`), also in a session that holds no
  settled result (project reopened).

Offline: the paid worker never starts, every dialog is replaced.
"""
from kicadgen.gui import main_window as gui
from kicadgen.services import datasheet_extractor
from tests.test_extraction_questions_gui import _single_part
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction

#: Two keys of the view question about the same pads, for two readings of the layout.
VIEW_A = "contacts_view:1|2|46b50b1a"
VIEW_B = "contacts_view:1|2|61060db3"
#: The same question about OTHER pads: never a contradiction of the two above.
VIEW_OTHER_PADS = "contacts_view:1|3|61060db3"


def _view_question(key):
    """The question «from which side is the drawing seen», with its two options."""
    return {"key": key, "field": "contacts_view", "text": "Da quale lato è visto il disegno?",
            "options": [{"label": "top", "value": "top"}, {"label": "bottom", "value": "bottom"}],
            "evidence": None}


def _ask_and_record(monkeypatch, seen):
    """Every question is answered with its first option («top»); the questions are recorded."""
    def ask(self, question):
        seen.append(question["key"])
        return question["options"][0]["value"]
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", ask)
    monkeypatch.setattr(datasheet_extractor, "apply_answers", lambda params, answers: params, raising=False)


def _extraction_with(question):
    params = header_extraction()
    params.questions = [question]
    return params


def test_a_saved_answer_contradicted_by_another_one_is_asked_again(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    seen = []
    _ask_and_record(monkeypatch, seen)
    window._extraction_answers = {VIEW_A: "top", VIEW_B: "bottom"}
    run_extraction(window, _extraction_with(_view_question(VIEW_A)))
    # Neither saved answer is the newer one for the program: the user is asked.
    assert seen == [VIEW_A]
    # The answer given now stays, the one it contradicts is dropped.
    assert window._extraction_answers == {VIEW_A: "top"}


def test_saved_answers_that_agree_or_concern_other_pads_are_still_applied_silently(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    seen = []
    _ask_and_record(monkeypatch, seen)
    # Same pads, same side: nothing to choose between.
    window._extraction_answers = {VIEW_A: "top", VIEW_B: "top"}
    run_extraction(window, _extraction_with(_view_question(VIEW_A)))
    assert seen == [] and window._extraction_answers == {VIEW_A: "top", VIEW_B: "top"}
    # The other side, but for other pads: another part of the drawing.
    window._extraction_answers = {VIEW_A: "top", VIEW_OTHER_PADS: "bottom"}
    run_extraction(window, _extraction_with(_view_question(VIEW_A)))
    assert seen == [] and window._extraction_answers == {VIEW_A: "top", VIEW_OTHER_PADS: "bottom"}


def _height_reread(**overrides):
    """A re-read result whose pitch differs from the settled one, as a raw main answer may."""
    return header_extraction("RELAY", cols=2, body_height_mm=7.5, pitch_mm=5.08, **overrides)


def test_a_partial_height_reread_changes_the_height_and_nothing_else(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    assert window._last_extracted.pitch_mm == 2.54
    window._on_reread_done(_height_reread(partial_reread={"field": "body_height_mm",
                                                         "body_axes_measured": False}))
    assert window._last_extracted.body_height_mm == 7.5
    assert window.field_body_height.value() == 7.5
    # Outside the scope of the re-read: the settled value, not the raw answer's.
    assert window._last_extracted.pitch_mm == 2.54
    assert window._last_extracted.partial_reread is None


def test_a_full_result_on_the_reread_path_is_applied_whole_as_before(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    # Not marked partial: the caller's contract is the one of a whole extraction.
    window._on_reread_done(_height_reread())
    assert window._last_extracted.body_height_mm == 7.5
    assert window._last_extracted.pitch_mm == 5.08


def test_a_height_reread_in_a_reopened_project_applies_the_height_alone(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    # A reopened project holds no settled extraction in the session.
    window._last_extracted = None
    applied = []
    monkeypatch.setattr(window, "_on_extracted", lambda params: applied.append(params))
    window._on_reread_done(_height_reread(partial_reread={"field": "body_height_mm",
                                                         "body_axes_measured": False}))
    assert applied == []          # the raw answer's pads never reach the fields
    assert window.field_body_height.value() == 7.5
    assert "applicata al solo corpo" in window._log_plain_text()


def test_a_reopened_project_keeps_its_height_when_the_reread_left_it_in_doubt(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    window._last_extracted = None
    before = window.field_body_height.value()
    monkeypatch.setattr(window, "_on_extracted", lambda params: None)
    window._on_reread_done(_height_reread(body_conflict="altezza: le due letture non coincidono.",
                                          partial_reread={"field": "body_height_mm",
                                                          "body_axes_measured": False}))
    assert window.field_body_height.value() == before
    assert "l'altezza resta com'è" in window._log_plain_text()
