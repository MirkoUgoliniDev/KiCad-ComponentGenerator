"""@brief Window fixes of the third review round (2026-11-11, K2 K3 K4 K6 K9 K10).

Each rule with a positive and a negative case that differ for a structural
reason:

- K2: a pad re-read needs the result the analysis settled in this session;
  without one (project reopened) it is refused BEFORE its paid call, and a
  late result is never applied raw. The height re-read needs no base.
- K3/K4: a height re-read in a reopened project reviews every question of
  its scope (`REREAD_SCOPES["body_height_mm"]["questions"]`) through the
  normal question review, keeps the project's other open questions, and
  applies the height only when no scope question is left open; the saved
  answers reach the worker and, from it, an extractor function that takes them.
- K9: that path counts as applied and clears only a height-related deferral.
- K10: a saved answer taken into a pass and then contradicted by a fresh one
  leaves the pass.
- K6: the pad dialog's 3-decimal boxes never turn an untouched estimated size
  into a «read» one.
- K5 (user decision): «Valori verificati sul disegno» in the pad dialog turns
  that pad's estimates into readings; without it a move or a rotation keeps
  every mark; a pad with nothing to verify has no checkbox.
- Unread holes: the footprint block names a pad whose HOLE was not read
  apart from a pad whose POSITION was not read.

Offline: no worker runs a model, every dialog is replaced.
"""
from PyQt6.QtWidgets import QCheckBox, QDialog, QDoubleSpinBox

from kicadgen.gui import main_window as gui
from kicadgen.gui import workers
from kicadgen.services import datasheet_extractor
from tests.test_extraction_questions_gui import _FakeReread, _single_part
from tests.ui_flow_helpers import header_extraction, package_extraction, prepare, run_extraction

HEIGHT_SCOPE = datasheet_extractor.REREAD_SCOPES["body_height_mm"]["questions"]
PARTIAL_HEIGHT = {"field": "body_height_mm", "body_axes_measured": False}
PARTIAL_PADS = {"field": "pad_layout", "body_axes_measured": False}


def _question(key, field, values):
    """A well-formed question of `field` with one option per value."""
    return {"key": key, "field": field, "text": f"Domanda {key}?",
            "options": [{"label": str(value), "value": value} for value in values], "evidence": None}


def _height_question():
    return _question("body_height_mm:7.5|9", "body_height_mm", (7.5, 9.0))


def _plan_question():
    return _question("height_equals_plan:7.5", "height_equals_plan", ("keep", "clear"))


def _pad_question():
    return _question("pad_layout:1|2|abc", "pad_layout", ("main", "dedicated"))


def _reopened(window, monkeypatch, tmp_path):
    """A single part analysed, then «reopened»: no settled result in the session."""
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    window._last_extracted = None
    # What a reopened project restores: the main answer saved with it.
    window._stored_main_answer = {"name": "RELAY", "_candidates": {}}
    before = window.field_body_height.value()
    return before


def _answer(monkeypatch, choices, seen):
    """Questions answered from `choices` {field: value or QUESTION_BACK}; their keys recorded."""
    def ask(self, question):
        seen.append(question["key"])
        return choices[question["field"]]
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", ask)


# ── K2: pad re-read without a settled result ───────────────────────────────

def test_a_pad_reread_in_a_reopened_project_is_refused_before_the_call(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    reason = window._reread_block_reason("pad_layout")
    assert "riapertura del progetto" in reason
    window._refresh_reread_actions()
    assert not window.action_reread_pads.isEnabled()
    assert window.action_reread_pads.toolTip() == reason
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    window._start_reread("pad_layout")
    assert _FakeReread.created == [] and shown == [reason]
    # The height re-read needs no settled base: its entry stays on.
    assert window._reread_block_reason("body_height_mm") == ""
    assert window.action_reread_height.isEnabled()


def test_a_pad_reread_with_a_settled_result_starts_and_is_merged(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    assert window._reread_block_reason("pad_layout") == ""
    window._start_reread("pad_layout")
    assert len(_FakeReread.created) == 1
    applied = []
    monkeypatch.setattr(window, "_on_extracted", applied.append)
    window._on_reread_done(header_extraction("RELAY", cols=2, pitch_mm=5.08, body_height_mm=9.0,
                                             partial_reread=dict(PARTIAL_PADS)))
    # Merged: the settled body quotes and pitch, not the raw answer's.
    assert len(applied) == 1 and applied[0].pitch_mm == 2.54 and applied[0].body_height_mm == 3


def test_a_late_pad_reread_without_a_settled_result_is_never_applied(window, monkeypatch, tmp_path):  # noqa: F811
    before = _reopened(window, monkeypatch, tmp_path)
    applied = []
    monkeypatch.setattr(window, "_on_extracted", applied.append)
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=9.0,
                                             partial_reread=dict(PARTIAL_PADS)))
    assert applied == [] and window.field_body_height.value() == before
    assert "Rilettura dei pad scartata" in window._log_plain_text()


# ── K3 / K4: the height scope's questions in a reopened project ─────────────

def test_a_height_question_is_reviewed_and_the_other_open_questions_are_kept(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    pad = _pad_question()
    old_height = _question("body_height_mm:1|2", "body_height_mm", (1.0, 2.0))
    window._extraction_questions = [pad, old_height]
    seen = []
    _answer(monkeypatch, {"body_height_mm": 9.0}, seen)
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=None,
                                             body_conflict="altezza: 7.5 o 9?",
                                             questions=[_height_question()],
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert seen == [_height_question()["key"]]
    assert window.field_body_height.value() == 9.0
    # The pad question of the project stays; the scope's old question is gone.
    assert window._extraction_questions == [pad]
    assert window._extraction_answers[_height_question()["key"]] == 9.0


def test_a_square_section_question_is_asked_and_clear_keeps_the_height(window, monkeypatch, tmp_path):  # noqa: F811
    before = _reopened(window, monkeypatch, tmp_path)
    seen = []
    _answer(monkeypatch, {"height_equals_plan": "clear"}, seen)
    # The agreed number equals a plan size: never applied before the answer.
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=10.0,
                                             questions=[_plan_question()],
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert seen == [_plan_question()["key"]]
    assert window.field_body_height.value() == before
    assert "misura in pianta" in window._log_plain_text()


def test_a_square_section_answered_keep_applies_the_height(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    seen = []
    _answer(monkeypatch, {"height_equals_plan": "keep"}, seen)
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=10.0,
                                             questions=[_plan_question()],
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert seen == [_plan_question()["key"]] and window.field_body_height.value() == 10.0


def test_a_scope_question_left_open_keeps_the_height_and_stays_open(window, monkeypatch, tmp_path):  # noqa: F811
    before = _reopened(window, monkeypatch, tmp_path)
    pad = _pad_question()
    window._extraction_questions = [pad]
    _answer(monkeypatch, {"height_equals_plan": gui.QUESTION_BACK}, [])
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=10.0,
                                             questions=[_plan_question()],
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert window.field_body_height.value() == before
    assert [q["key"] for q in window._extraction_questions] == [pad["key"], _plan_question()["key"]]
    assert "resta senza risposta" in window._log_plain_text()


def test_a_saved_scope_answer_is_applied_without_asking(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    seen = []
    _answer(monkeypatch, {}, seen)
    window._extraction_answers = {_plan_question()["key"]: "keep"}
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=10.0,
                                             questions=[_plan_question()],
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert seen == [] and window.field_body_height.value() == 10.0


class _AnsweringReread(_FakeReread):
    """A re-read worker that declares `answers=`, as workers.RereadWorker does."""

    def __init__(self, *args, answers=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.answers = answers


def test_the_saved_answers_reach_a_worker_that_takes_them(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(workers, "RereadWorker", _AnsweringReread, raising=False)
    window._extraction_answers = {"height_equals_plan:7.5": "keep"}
    window._start_reread("body_height_mm")
    assert _FakeReread.created[-1].answers == {"height_equals_plan:7.5": "keep"}
    assert _FakeReread.created[-1].answers is not window._extraction_answers


def test_a_worker_without_the_keyword_still_starts(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative twin: the window and the workers module change separately."""
    _single_part(window, monkeypatch, tmp_path)
    window._extraction_answers = {"height_equals_plan:7.5": "keep"}
    window._start_reread("body_height_mm")
    assert len(_FakeReread.created) == 1 and not hasattr(_FakeReread.created[0], "answers")


def _run_worker(monkeypatch, reread, answers):
    """Run a RereadWorker synchronously on `reread`; return its errors and results."""
    monkeypatch.setattr(datasheet_extractor, "reread_body_height", reread, raising=False)
    monkeypatch.setattr(datasheet_extractor, "_pad_reading_model",
                        lambda pad_model, main, family, progress, check: main)
    worker = workers.RereadWorker({"name": "X"}, ["a.png"], None, field="body_height_mm",
                                  model="openai/gpt-test", answers=answers)
    errors, done = [], []
    worker.error.connect(errors.append)
    worker.done.connect(done.append)
    worker.run()
    return errors, done


def test_the_worker_forwards_the_answers_to_an_extractor_that_takes_them(qapp, monkeypatch):
    seen = {}

    def reread(stored, documents, *, model, call, progress=None, answers=None, same_model=False):
        seen["answers"] = answers
        return "result"

    errors, done = _run_worker(monkeypatch, reread, {"k": "keep"})
    assert not errors and done == ["result"] and seen["answers"] == {"k": "keep"}


def test_the_worker_gives_no_answers_to_an_extractor_without_the_keyword(qapp, monkeypatch):
    def reread(stored, documents, *, model, call, progress=None, same_model=False):
        return "result"

    errors, done = _run_worker(monkeypatch, reread, {"k": "keep"})
    assert not errors and done == ["result"]


# ── K9: applied revision, autosave, deferral ───────────────────────────────

def test_the_height_alone_path_counts_as_applied_and_suspends_the_autosave(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    window._extraction_applied_revision = None
    suspended = []
    real = window._autosave_suspended

    def spy():
        suspended.append(True)
        return real()
    monkeypatch.setattr(window, "_autosave_suspended", spy)
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=7.5,
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert window._extraction_applied_revision == window._analysis_revision
    assert suspended == [True]


def test_a_height_clears_only_what_it_resolves_in_the_step_deferral(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    reread = dict(body_height_mm=7.5, partial_reread=dict(PARTIAL_HEIGHT))
    # Positive: a missing height alone, or a height conflict, is resolved.
    for deferral in ({"code": "missing", "conflict": None, "fields": ["body_height_mm"]},
                     {"code": "conflict", "conflict": "altezza: 7 o 8?", "fields": ["body_height_mm"]}):
        window._step_deferral = dict(deferral)
        window._on_reread_done(header_extraction("RELAY", cols=2, **reread))
        assert window._step_deferral is None
    # Negative: a missing length is not resolved by a height.
    window._step_deferral = {"code": "missing", "conflict": None,
                             "fields": ["body_length_mm", "body_height_mm"]}
    window._on_reread_done(header_extraction("RELAY", cols=2, **reread))
    assert window._step_deferral == {"code": "missing", "conflict": None, "fields": ["body_length_mm"]}


# ── K10: an answer dropped within the pass leaves the pass ─────────────────

VIEW_A = "contacts_view:1|2|46b50b1a"
VIEW_B = "contacts_view:1|2|61060db3"


def _view(key):
    return _question(key, "contacts_view", ("top", "bottom"))


def test_a_saved_answer_contradicted_later_in_the_pass_is_not_applied(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    received = []
    monkeypatch.setattr(datasheet_extractor, "apply_answers",
                        lambda params, answers: received.append(dict(answers)) or params, raising=False)
    asked = []
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, q: asked.append(q["key"]) or "bottom")
    # VIEW_A is saved alone (applied silently); VIEW_B, asked now, contradicts it.
    window._extraction_answers = {VIEW_A: "top"}
    params = header_extraction()
    params.questions = [_view(VIEW_A), _view(VIEW_B)]
    run_extraction(window, params)
    # CONTRACT CHANGED 2026-11-11 (review F2-4). Before: `received ==
    # [{VIEW_B: "bottom"}]` and only VIEW_B saved -- the dropped VIEW_A was
    # neither answered nor open, and nothing held the generation back. Now
    # the question whose saved answer was dropped is asked in the same pass:
    # the saved «top» is still never applied (the point of K10), and VIEW_A
    # ends answered by the user («bottom», which agrees with VIEW_B).
    assert asked == [VIEW_B, VIEW_A]
    assert received == [{VIEW_B: "bottom", VIEW_A: "bottom"}]
    assert window._extraction_answers == {VIEW_B: "bottom", VIEW_A: "bottom"}
    assert window._extraction_questions == []


def test_saved_answers_not_contradicted_in_the_pass_are_all_applied(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "HDR")
    received = []
    monkeypatch.setattr(datasheet_extractor, "apply_answers",
                        lambda params, answers: received.append(dict(answers)) or params, raising=False)
    # The fresh answer agrees with the saved one: nothing is dropped.
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", lambda self, q: "top")
    window._extraction_answers = {VIEW_A: "top"}
    params = header_extraction()
    params.questions = [_view(VIEW_A), _view(VIEW_B)]
    run_extraction(window, params)
    assert received == [{VIEW_A: "top", VIEW_B: "top"}]


# ── K6: the pad dialog's decimals ──────────────────────────────────────────

ESTIMATED = {"drill_mm": "estimated", "pad_size": "estimated", "shape": "estimated"}


def _tht_package(window, monkeypatch, tmp_path, sources=ESTIMATED, lead=None):
    """A two-pad THT package whose first pad has sizes finer than the dialog shows.

    `sources` None gives pads without provenance (a project saved before it existed).
    """
    prepare(window, monkeypatch, tmp_path, "THT")
    pins = [{"number": str(n), "name": "", "x_mm": x, "y_mm": 0.0, "pad_length_mm": 1.5875,
             "pad_width_mm": 1.5875, "pad_type": "thru_hole", "shape": "circle", "drill_mm": 0.8375,
             **({"sources": dict(sources)} if sources is not None else {}),
             **({"lead": dict(lead)} if lead is not None else {})} for n, x in ((1, -5.0), (2, 5.0))]
    geometry = {"name": "THT", "family": "custom", "body_length_mm": 14.0, "body_width_mm": 6.0,
                "body_height_mm": 5.0, "origin": "body_center", "source_view": "top",
                "orientation_deg": 0, "mirrored": False, "confidence": "high", "needs_review": False,
                "pins": pins}
    run_extraction(window, package_extraction(geometry, name="THT"))
    assert window.package_geometry is not None
    return window.package_geometry.pins[0]


def _dialog_ok(window, monkeypatch, edit=None, seen=None):
    """Open the pad dialog of pad 0 and press OK, after `edit(boxes)` when given.

    `seen` (a list) receives the dialog's «verified» checkbox, or None when absent.
    """
    def run(dialog):
        boxes = dialog.findChildren(QDoubleSpinBox)
        if seen is not None:
            seen.append(dialog.findChild(QCheckBox, "pad_values_verified"))
        if edit is not None:
            edit(boxes)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(window, "_run_modal", run)
    window._edit_package_pad(0)


def test_ok_on_an_untouched_dialog_keeps_the_exact_sizes_and_the_estimate(window, monkeypatch, tmp_path):  # noqa: F811
    before = _tht_package(window, monkeypatch, tmp_path)
    _dialog_ok(window, monkeypatch)
    after = window.package_geometry.pins[0]
    assert (after.pad_length_mm, after.pad_width_mm, after.drill_mm) == (1.5875, 1.5875, 0.8375)
    assert after.sources == before.sources == ESTIMATED


def test_a_move_alone_keeps_the_sizes_and_the_estimate(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)

    def move(boxes):
        # Boxes in form order: width, height, rotation, hole, slot, X, Y.
        boxes[5].setValue(boxes[5].value() + 1.0)
    _dialog_ok(window, monkeypatch, move)
    after = window.package_geometry.pins[0]
    assert (after.pad_length_mm, after.drill_mm) == (1.5875, 0.8375) and after.sources == ESTIMATED


def test_a_size_the_user_types_becomes_read(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)

    def resize(boxes):
        boxes[0].setValue(1.8)
        boxes[1].setValue(1.8)
        boxes[3].setValue(0.9)
    _dialog_ok(window, monkeypatch, resize)
    after = window.package_geometry.pins[0]
    assert (after.pad_length_mm, after.pad_width_mm, after.drill_mm) == (1.8, 1.8, 0.9)
    assert after.sources["pad_size"] == "read" and after.sources["drill_mm"] == "read"
    assert after.sources["shape"] == "estimated"


def test_the_kept_value_rule_compares_with_what_the_box_showed():
    """CONTRACT CHANGED 2026-11-11 (review F3-4); was `..._compares_at_the_boxes_decimals`.

    Before, the rule took (original, value) and kept the original for ANY
    value within half a unit of the box's last decimal: `keep(1.5875, 1.588)
    == keep(1.5875, 1.587) == 1.5875`. That asserted the defect: the box shows
    1.587 (Qt) and a user who types 1.588 was ignored. The rule now takes
    what the box showed when it was filled, the one fact that tells an edit
    from a rounding: equal to it is untouched, anything else is typed.
    """
    keep = gui.MainWindow._pad_dialog_value
    # Untouched, whichever way the box rounded the tie.
    assert keep(1.5875, 1.587, 1.587) == 1.5875 and keep(1.5875, 1.588, 1.588) == 1.5875
    # Typed: the neighbouring number the box can show, however close to the original.
    assert keep(1.5875, 1.587, 1.588) == 1.588 and keep(1.5875, 1.588, 1.587) == 1.587
    assert keep(1.5875, 1.587, 1.9) == 1.9
    # A box filled with «no value» (0) and then typed, or emptied.
    assert keep(None, 0.0, 0.9) == 0.9 and keep(None, 0.0, 0.0) is None
    assert keep(0.8, 0.8, None) is None


# ── K5: «Valori verificati sul disegno» ───────────────────────────────────

ROUND_LEAD = {"shape": "round", "diameter_mm": 0.6}


def _estimates(window):
    """The estimated_pad findings and the description of the footprint the window would write."""
    from kicadgen.generators.package import build_package
    from kicadgen.services import footprint_checks as fc
    footprint = build_package(window.package_geometry)
    return fc.check_estimated_pads(footprint), footprint.description


def test_values_declared_verified_stop_being_estimates(window, monkeypatch, tmp_path):  # noqa: F811
    from kicadgen.generators.package import ESTIMATED_PADS_NOTE
    _tht_package(window, monkeypatch, tmp_path, sources={**ESTIMATED, "lead": "estimated"}, lead=ROUND_LEAD)
    findings, description = _estimates(window)
    assert findings and ESTIMATED_PADS_NOTE.split("{")[0] in description
    seen = []
    _dialog_ok(window, monkeypatch, lambda boxes: seen[0].setChecked(True), seen)
    assert seen[0] is not None          # offered: the pad carries estimates
    after = window.package_geometry.pins[0]
    assert after.sources == {"drill_mm": "read", "pad_size": "read", "shape": "read", "lead": "estimated"}
    # The sizes are the original ones (nothing typed), and only pad 0 was declared.
    assert (after.pad_length_mm, after.drill_mm) == (1.5875, 0.8375)
    assert window.package_geometry.pins[1].sources == {**ESTIMATED, "lead": "estimated"}
    findings, description = _estimates(window)
    assert [f.pads for f in findings] == [(1,)]
    assert ESTIMATED_PADS_NOTE.format(numbers="2") in description


def test_unchecked_a_move_and_a_rotation_keep_every_mark(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    seen = []

    def move_and_turn(boxes):
        # Boxes in form order: width, height, rotation, hole, slot, X, Y.
        boxes[2].setValue(90.0)
        boxes[5].setValue(boxes[5].value() + 1.0)
    _dialog_ok(window, monkeypatch, move_and_turn, seen)
    assert seen[0] is not None and not seen[0].isChecked()
    after = window.package_geometry.pins[0]
    assert after.sources == ESTIMATED and after.rotation_deg != 0.0


def test_a_pad_without_unverified_values_has_no_checkbox(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path, sources=None)
    seen = []
    _dialog_ok(window, monkeypatch, None, seen)
    assert seen == [None]


def test_a_pad_with_read_values_alone_has_no_checkbox(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path, sources={"drill_mm": "read", "pad_size": "read"})
    seen = []
    _dialog_ok(window, monkeypatch, None, seen)
    assert seen == [None]


# ── Unread holes: the block names what is missing ─────────────────────────

def _dropped_for(unread_kind):
    """An extraction whose pad 5 was dropped; `unread_kind` "hole" gives it a position and no hole."""
    from tests.test_unread_pads_gui import _dropped
    params = _dropped(("5",))
    if unread_kind == "hole":
        for pin in params.unread_pad_geometry["pins"]:
            if str(pin.get("number")) == "5":
                pin.update(x_mm=5.0, y_mm=0.0, pad_type="thru_hole", drill_mm=None)
    return params


def _single_tht_project(window, monkeypatch, tmp_path):
    prepare(window, monkeypatch, tmp_path, "RLY")
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))


def test_a_pad_whose_hole_was_not_read_is_named_for_its_hole(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht_project(window, monkeypatch, tmp_path)
    run_extraction(window, _dropped_for("hole"))
    assert window._unread_pads == ["5"] and window._unread_pad_holes == ["5"]
    block = window._unread_pads_block()
    assert "i fori dei pad 5 non sono stati letti" in block
    assert "le posizioni dei pad 5" not in block and "Per i fori" in block
    # Saved and restored with the project: a reopen says the same.
    state = window._project_state()
    assert state["unread_pad_holes"] == ["5"]
    window._switch_project("OTHER", window._general_project_state)
    assert window._unread_pad_holes is None
    window._switch_project("RLY", state)
    assert window._unread_pads_block() == block


def test_a_pad_whose_position_was_not_read_keeps_the_position_message(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht_project(window, monkeypatch, tmp_path)
    run_extraction(window, _dropped_for("position"))
    assert window._unread_pads == ["5"] and window._unread_pad_holes is None
    block = window._unread_pads_block()
    assert "le posizioni dei pad 5 non sono state lette" in block and "fori" not in block
    assert "unread_pad_holes" not in window._project_state()


def test_a_saved_hole_list_naming_other_pads_is_ignored(window, monkeypatch, tmp_path):  # noqa: F811
    """A hand-edited project cannot name as unread holes pads that are not unread."""
    _single_tht_project(window, monkeypatch, tmp_path)
    run_extraction(window, _dropped_for("hole"))
    state = window._project_state()
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", dict(state, unread_pad_holes=["5", "9"]))
    assert window._unread_pad_holes == ["5"]
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", dict(state, unread_pad_holes=["9"]))
    assert window._unread_pad_holes is None
    assert "le posizioni dei pad 5" in window._unread_pads_block()
