"""@brief Fixes of the 2026-11-16 «super controllo» review in `kicadgen/gui/main_window.py` (fixer C10).

Each finding gets a positive case (the defect no longer happens) and a
negative case that the rule must tell apart for a structural reason: the
findings R1-*, R2-* and R3-* are summarised in HANDOFF.md, section «IN CORSO
16/11/2026 — SUPER CONTROLLO».
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QMessageBox  # noqa: E402

from tests.test_format_painter_window import _rows  # noqa: E402
from tests.test_footprint_reference_image import _png_bytes  # noqa: E402
from tests.test_photo_inventory_gui import gui, raw_params  # noqa: E402,F401 - fixtures
from test_footprint_gesture_parity import fp  # noqa: E402,F401 - fixture


def _logged(window, monkeypatch):
    """@brief Capture the log lines of `window` into a list."""
    lines = []
    monkeypatch.setattr(window, "_append_log", lambda message, **_k: lines.append(str(message)))
    return lines


# ── R3-1: «Copia formato» does not survive a project switch ────────────────


def test_r3_1_project_switch_turns_the_format_painter_off_without_applying(window, monkeypatch):
    """@brief Positive: a switch ends the painter, forgets A's format and regenerates nothing."""
    window._set_photo_components(_rows())
    applied = []
    monkeypatch.setattr(window, "_apply_photo_review", lambda *args, **kw: applied.append(args))
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)
    window._on_format_painter_click(1)
    assert window._format_source is not None and window._format_targets
    window._switch_project("ALTRO", window._general_project_state)
    assert not window.btn_format_painter_3d.isChecked()
    assert window._format_source is None and window._format_targets == []
    assert window._format_source_row is None and window._format_target_rows == []
    assert window.preview_3d.format_painter_active is False
    assert applied == [], "the closing project's pastes are never applied to the new one"


def test_r3_1_without_a_switch_the_painter_keeps_its_source(window, monkeypatch):
    """@brief Negative: the same clicks without a switch keep the mode and the copied format."""
    window._set_photo_components(_rows())
    monkeypatch.setattr(window, "_apply_photo_review", lambda *args, **kw: None)
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)
    window._refresh_components_editor_button()
    assert window.btn_format_painter_3d.isChecked()
    assert window._format_source is not None


def test_r3_1_a_paste_writes_the_saved_choices_at_once(window, monkeypatch):
    """@brief Positive: each paste reaches the choices JSON; copying alone writes nothing."""
    from kicadgen.services import model_choices
    window._set_photo_components(_rows())
    window._photo_png = b"photo"
    saved = []
    monkeypatch.setattr(model_choices, "save_choices",
                        lambda folder, png, rows: saved.append([dict(r) for r in rows]) or folder)
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)                       # copy only
    assert saved == []
    window._on_format_painter_click(1)                       # paste on Q2
    assert len(saved) == 1
    assert saved[0][1]["length_mm"] == pytest.approx(2.9)    # Q2 carries Q1's format
    monkeypatch.setattr(window, "_apply_photo_review", lambda *args, **kw: None)
    window.btn_format_painter_3d.setChecked(False)


def test_r3_1_a_paste_without_a_photo_writes_no_choices(window, monkeypatch):
    """@brief Negative: no calibrated photo, no choices file (as `_apply_photo_review`)."""
    from kicadgen.services import model_choices
    window._set_photo_components(_rows())
    window._photo_png = None
    saved = []
    monkeypatch.setattr(model_choices, "save_choices", lambda *args: saved.append(args))
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)
    window._on_format_painter_click(1)
    assert saved == []
    monkeypatch.setattr(window, "_apply_photo_review", lambda *args, **kw: None)
    window.btn_format_painter_3d.setChecked(False)


# ── R2-1: two castellated runs on the same edge ──────────────────────────────


def _two_runs_on_left(window):
    """@brief Window sides: two runs on the left edge, one on the right."""
    window._sides[:] = [gui.normalised_side(side) for side in (
        {"edge": "left", "count": 4, "pitch_mm": 2.54, "first_pin": 1, "reverse": False,
         "contact_style": "half_hole"},
        {"edge": "left", "count": 4, "pitch_mm": 2.54, "offset_mm": 12.0, "first_pin": 5,
         "reverse": False, "contact_style": "half_hole"},
        {"edge": "right", "count": 4, "pitch_mm": 2.54, "first_pin": 9, "reverse": False,
         "contact_style": "half_hole"},
    )]


def test_r2_1_runs_sharing_an_edge_are_paired_by_their_order(window, monkeypatch):
    """@brief Positive: each run of the left edge takes ITS answer side, not the last one."""
    _two_runs_on_left(window)
    monkeypatch.setattr(window, "_on_sides_changed", lambda: None)
    updated = SimpleNamespace(castellated_sides=[
        {"edge": "left", "first_pin": 4, "reverse": True, "contact_style": "through_hole"},
        {"edge": "left", "first_pin": 8, "reverse": True, "contact_style": "half_hole"},
        {"edge": "right", "first_pin": 12, "reverse": True, "contact_style": "through_hole"}])
    pairs = window._sides_paired_with_answer(updated)
    assert [(a["first_pin"], b["first_pin"]) for a, b in pairs] == [(1, 4), (5, 8), (9, 12)]
    window._apply_answered_castellated_choice(updated, "contact_style")
    assert [s["contact_style"] for s in window._sides] == [
        "through_hole", "half_hole", "through_hole"]


def test_r2_1_a_different_number_of_runs_on_an_edge_is_not_guessed(window, monkeypatch):
    """@brief Negative: one answer side for two table runs on the edge -> no pairing, nothing changed."""
    _two_runs_on_left(window)
    monkeypatch.setattr(window, "_on_sides_changed", lambda: None)
    updated = SimpleNamespace(castellated_sides=[
        {"edge": "left", "first_pin": 4, "reverse": True, "contact_style": "through_hole"},
        {"edge": "right", "first_pin": 12, "reverse": True, "contact_style": "through_hole"}])
    assert window._sides_paired_with_answer(updated) is None
    assert window._unpaired_side_edges(updated) == [gui.edge_label("left")]
    before = [dict(s) for s in window._sides]
    window._apply_answered_castellated_choice(updated, "contact_style")
    assert window._sides == before


def test_r2_1_an_unpairable_side_answer_keeps_the_question_open(window, monkeypatch):
    """@brief Positive: the answer is neither remembered nor applied, the question stays open."""
    from kicadgen.services import datasheet_extractor
    _two_runs_on_left(window)
    lines = _logged(window, monkeypatch)
    question = {"key": "side_numbering:x", "field": "side_numbering", "text": "?",
                "options": [{"label": "Rinumera", "value": "renumber"}]}
    window._extraction_questions = [question]
    window._extraction_answers = {}
    extracted = SimpleNamespace(questions=[question])
    updated = SimpleNamespace(questions=[], castellated_sides=[
        {"edge": "left", "first_pin": 4, "reverse": True}])
    monkeypatch.setattr(window, "_settled_extraction", lambda: extracted)
    monkeypatch.setattr(window, "_extraction_holds_question", lambda *_a: True)
    monkeypatch.setattr(window, "_ask_extraction_question", lambda q: "renumber")
    monkeypatch.setattr(window, "_saved_answers_for_deferred", lambda p: {})
    monkeypatch.setattr(datasheet_extractor, "apply_answers", lambda p, answers: updated)
    assert window._resolve_one_footprint_question(question) is False
    assert window._extraction_answers == {}
    assert window._extraction_questions == [question]
    assert any("Risposta non applicata" in line for line in lines)


# ── R2-2: castellated module without a known board size ─────────────────────


def _castellated(window):
    """@brief A castellated module with its sides, the 3D group on."""
    window.group_castellated.setChecked(True)
    window.group_3d.setChecked(True)
    window.package_geometry = None


def test_r2_2_unknown_board_size_builds_no_footprint_and_no_body(window):
    """@brief Positive: the placeholder outline is never a module footprint or a STEP body."""
    _castellated(window)
    window._board_size_known = False
    with pytest.raises(ValueError, match="quote della scheda"):
        window._current_castellated_params()
    assert window._current_body_params() is None
    assert window._missing_body_quotes() == ["body_length_mm", "body_width_mm"]
    assert window._preview_params() is None


def test_r2_2_known_board_size_builds_both(window):
    """@brief Negative: the same module with a known size builds its footprint and body."""
    _castellated(window)
    window._board_size_known = True
    params = window._current_castellated_params()
    assert params.body_length == pytest.approx(window.field_cast_body_length.value())
    assert window._current_body_params() is not None
    assert window._missing_body_quotes() == []


def test_r2_2_a_pin_matrix_ignores_the_board_size(window):
    """@brief Negative: the block is about a module's edges, not about every component."""
    window.group_castellated.setChecked(False)
    window._board_size_known = False
    assert window._castellated_board_size_block() == ""


def test_r2_2_regenerate_footprint_asks_the_size_and_stops_on_cancel(window, monkeypatch):
    """@brief Positive: a user command asks the size; «Annulla» writes nothing and logs the block."""
    _castellated(window)
    window._board_size_known = False
    lines = _logged(window, monkeypatch)
    asked, started = [], []
    monkeypatch.setattr(window, "_resolve_stored_pad_layout_question", lambda: True)
    monkeypatch.setattr(window, "_ask_board_size", lambda note=None: asked.append(note) or None)
    monkeypatch.setattr(window, "_generate_applied", lambda **kw: started.append(kw) or True)
    assert window._generate() is False
    assert len(asked) == 1 and "bordi" in asked[0]
    assert started == []
    assert any("quote della scheda" in line for line in lines)


def test_r2_2_regenerate_footprint_proceeds_once_the_size_is_typed(window, monkeypatch):
    """@brief Negative of the cancel: a typed size lets the generation start."""
    _castellated(window)
    window._board_size_known = False
    started = []

    def typed(note=None):
        window._board_size_known = True
        return 30.0, 15.0
    monkeypatch.setattr(window, "_resolve_stored_pad_layout_question", lambda: True)
    monkeypatch.setattr(window, "_settle_footprint_draft_before_start", lambda: True)
    monkeypatch.setattr(window, "_ask_board_size", typed)
    monkeypatch.setattr(window, "_generate_applied", lambda **kw: started.append(kw) or True)
    assert window._generate() is True
    assert len(started) == 1


def test_r2_2_the_automatic_pipeline_never_asks(window, monkeypatch):
    """@brief Negative: an unattended generation does not open the size question."""
    _castellated(window)
    window._board_size_known = False
    asked = []
    monkeypatch.setattr(window, "_pad_layout_question_block", lambda: "")
    monkeypatch.setattr(window, "_ask_board_size", lambda note=None: asked.append(note))
    monkeypatch.setattr(window, "_generate_applied", lambda **kw: False)
    window._generate(automatic=True)
    assert asked == []


# ── R1-4: the board-size question never stores the untouched minimum ─────────


def test_r1_4_ok_is_disabled_until_both_sizes_are_typed(window, monkeypatch):
    """@brief Positive: «OK» starts disabled and is enabled only with both values above the minimum."""
    window._board_size_known = False
    seen = []

    def answer(dialog):
        ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        spins = dialog.findChildren(QDoubleSpinBox)
        seen.append(ok.isEnabled())
        spins[0].setValue(30.0)
        seen.append(ok.isEnabled())
        spins[1].setValue(15.0)
        seen.append(ok.isEnabled())
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", answer)
    assert window._ask_board_size() == (30.0, 15.0)
    assert seen == [False, False, True]
    assert window._board_size_known


def test_r1_4_an_accept_on_the_untouched_fields_stores_nothing(window, monkeypatch):
    """@brief Negative: an accept that bypassed «OK» on the minimum values is a cancel."""
    window._board_size_known = False
    monkeypatch.setattr(QDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    assert window._ask_board_size() is None
    assert not window._board_size_known


# ── R2-3 / R2-4 / R2-5: partial re-read error path and apply guard ───────────


def _retry_calls(window, monkeypatch, qapp):
    """@brief Make «Riprova» answer yes and record what it restarts."""
    calls = []
    monkeypatch.setattr(window, "_ask_extraction_retry", lambda message: True)
    monkeypatch.setattr(window, "_start_reread", lambda field: calls.append(("reread", field)))
    monkeypatch.setattr(window, "_extract_with_ai", lambda: calls.append(("full", None)))
    return calls


def test_r2_3_retry_after_a_failed_reread_repeats_only_the_reread(window, monkeypatch, qapp):
    """@brief Positive: the retry of a re-read error starts that re-read, not the full analysis."""
    calls = _retry_calls(window, monkeypatch, qapp)
    worker = SimpleNamespace(_reread_field="pad_layout")
    monkeypatch.setattr(window, "sender", lambda: worker)
    window._on_reread_error("rete assente")
    qapp.processEvents()
    assert calls == [("reread", "pad_layout")]


def test_r2_3_retry_after_a_failed_analysis_repeats_the_analysis(window, monkeypatch, qapp):
    """@brief Negative: an analysis error keeps its full retry."""
    calls = _retry_calls(window, monkeypatch, qapp)
    window._on_extract_error("rete assente")
    qapp.processEvents()
    assert calls == [("full", None)]


def test_r2_4_a_finished_arriving_during_a_reread_apply_waits_for_it(window, monkeypatch):
    """@brief Positive: the queued `finished` delivered inside the apply runs once, after it."""
    ran = []

    def finished():
        # The real slot's protocol (`_on_extract_finished`): postpone while applying.
        if window._applying_extraction:
            window._extract_finish_pending = True
            return
        ran.append(True)
    monkeypatch.setattr(window, "_on_extract_finished", finished)

    def apply(params):
        window._on_extract_finished()                 # the queued signal, delivered in a modal
        assert ran == [], "the window must not be reset half way"
    monkeypatch.setattr(window, "_apply_reread_result", apply)
    window._on_reread_done(SimpleNamespace(partial_reread={"field": "body_height_mm"}))
    assert ran == [True]
    assert not window._applying_extraction and not window._extract_finish_pending


def test_r2_4_the_guard_is_reentrant_and_outside_it_finished_runs_at_once(window, monkeypatch):
    """@brief Negative: outside the guard `finished` runs at once; a nested guard waits for the outer one."""
    ran = []

    def finished():
        if window._applying_extraction:
            window._extract_finish_pending = True
            return
        ran.append(True)
    monkeypatch.setattr(window, "_on_extract_finished", finished)
    window._on_extract_finished()
    assert ran == [True]
    with window._extraction_apply_guard():
        with window._extraction_apply_guard():
            window._on_extract_finished()
        assert ran == [True], "the inner guard leaves the pending finish to the outer one"
    assert ran == [True, True]


def test_r2_5_an_exception_while_applying_a_reread_goes_to_the_error_path(window, monkeypatch):
    """@brief Positive: no exception escapes the slot; the error names the re-read for its retry."""
    errors = []
    monkeypatch.setattr(window, "_on_extract_error",
                        lambda message, reread_field=None: errors.append((message, reread_field)))

    def broken(params):
        raise RuntimeError("merge rotto")
    monkeypatch.setattr(window, "_apply_reread_result", broken)
    window._on_reread_done(SimpleNamespace(partial_reread={"field": "body_height_mm"}))
    assert len(errors) == 1 and "merge rotto" in errors[0][0]
    assert errors[0][1] == "body_height_mm"
    assert not window._applying_extraction


def test_r2_5_a_reread_applied_cleanly_reports_no_error(window, monkeypatch):
    """@brief Negative: a clean apply never reaches the error path."""
    errors = []
    monkeypatch.setattr(window, "_on_extract_error", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(window, "_apply_reread_result", lambda params: None)
    window._on_reread_done(SimpleNamespace(partial_reread={"field": "pad_layout"}))
    assert errors == []


# ── R2-6: the retry after correcting the holes keeps the body and its outcome ─


def test_r2_6_the_hole_correction_retry_forwards_the_body_and_the_outcome(window, monkeypatch):
    """@brief Positive: the retried generation gets the user's body and its result is returned."""
    body = object()
    calls = []
    monkeypatch.setattr(window, "_unread_pads_block", lambda: "pad non letti")

    def recover(params):
        monkeypatch.setattr(window, "_generate_applied",
                            lambda **kw: calls.append(kw) or True)
        return True
    monkeypatch.setattr(window, "_correct_invalid_through_holes", recover)
    assert window._generate_applied(automatic=False, body_override=body) is True
    assert calls == [{"automatic": False, "footprint_only": False, "body_override": body}]


def test_r2_6_no_correction_means_no_retry(window, monkeypatch):
    """@brief Negative: when nothing is corrected, the failed generation stays failed."""
    calls = []
    monkeypatch.setattr(window, "_unread_pads_block", lambda: "pad non letti")
    monkeypatch.setattr(window, "_correct_invalid_through_holes", lambda params: False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: calls.append(args[1]))
    assert window._generate_applied(automatic=False) is False
    assert calls == ["Footprint non generato"]


# ── R2-7: answers rolled back when the extractor cannot apply them ───────────


def _pass_with_one_question(window, monkeypatch):
    """@brief One pad-layout question answered «a» in a pass, with a saved unrelated answer."""
    question = {"key": "pad_layout:1", "field": "pad_layout", "text": "Quale schema?",
                "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]}
    params = SimpleNamespace(questions=[question])
    window._extraction_answers = {"other:1": "x"}
    monkeypatch.setattr(window, "_ask_extraction_question", lambda q: "a")
    monkeypatch.setattr(window, "_saved_answers_for_deferred", lambda p: {})
    monkeypatch.setattr(window, "_ask_opened_questions", lambda keys, p, ask=True: (p, False))
    return params, question


def test_r2_7_a_failed_apply_rolls_the_answers_back_and_holds_the_generation(window, monkeypatch):
    """@brief Positive: nothing of the pass is remembered, the question stays open, held back."""
    from kicadgen.services import datasheet_extractor
    params, question = _pass_with_one_question(window, monkeypatch)

    def refuse(p, answers):
        raise ValueError("schema non costruibile")
    monkeypatch.setattr(datasheet_extractor, "apply_answers", refuse)
    lines = _logged(window, monkeypatch)
    result, held_back = window._review_extraction_questions(params)
    assert held_back is True
    assert result is params
    assert window._extraction_answers == {"other:1": "x"}
    assert [q["key"] for q in window._extraction_questions] == ["pad_layout:1"]
    assert any("schema non costruibile" in line for line in lines)


def test_r2_7_an_applied_answer_is_remembered(window, monkeypatch):
    """@brief Negative: the same pass with an extractor that applies it commits the answer."""
    from kicadgen.services import datasheet_extractor
    params, question = _pass_with_one_question(window, monkeypatch)
    applied = SimpleNamespace(questions=None)
    monkeypatch.setattr(datasheet_extractor, "apply_answers", lambda p, answers: applied)
    result, held_back = window._review_extraction_questions(params)
    assert held_back is False
    assert result is applied
    assert window._extraction_answers.get("pad_layout:1") == "a"
    assert window._extraction_questions == []


# ── R2-8: provenance of the module-wide castellated sizes is a flag ──────────


def test_r2_8_a_declared_size_equal_to_the_reference_stays_a_measure(window):
    """@brief Positive: a measured 1.7 mm hole is declared although it equals the reference."""
    window.field_cast_hole.setValue(1.7)
    window._cast_declared_sizes = {"field_cast_hole"}
    assert window._declared_cast_size("field_cast_hole") == pytest.approx(1.7)


def test_r2_8_an_undeclared_value_is_an_estimate_whatever_its_number(window):
    """@brief Negative: a value no one declared reaches the generator as None (its estimate)."""
    window.field_cast_hole.setValue(2.0)
    window._cast_declared_sizes = set()
    assert window._declared_cast_size("field_cast_hole") is None


def test_r2_8_the_flag_survives_save_and_restore(window):
    """@brief Positive: the recorded provenance comes back with the project."""
    window.field_cast_thru_pad.setValue(1.7)
    window._cast_declared_sizes = {"field_cast_thru_pad"}
    state = window._project_state()
    assert state["cast_declared_sizes"] == ["field_cast_thru_pad"]
    window._cast_declared_sizes = set()
    window._apply_project_state(state)
    assert window._cast_declared_sizes == {"field_cast_thru_pad"}


def test_r2_8_a_project_saved_before_the_flag_uses_the_declared_heuristic(window):
    """@brief Negative: without the key, the old reading (value other than the reference) applies."""
    state = window._project_state()
    state.pop("cast_declared_sizes")
    state["widgets"]["field_cast_hole"] = 2.0
    state["widgets"]["field_cast_thru_drill"] = 1.0
    window._apply_project_state(state)
    assert "field_cast_hole" in window._cast_declared_sizes
    assert "field_cast_thru_drill" not in window._cast_declared_sizes


# ── R2-9: one isolated preview at a time while its image is rendered ────────


class _RenderWorker:
    """@brief Stand-in of PreviewRenderWorker with a settable running state."""

    def __init__(self, token, running=True):
        self.token = token
        self.running = running

    def isRunning(self):  # noqa: N802 - Qt name
        """@brief Whether the render is still running."""
        return self.running


def test_r2_9_a_preview_waits_for_the_render_of_the_same_project(window, monkeypatch):
    """@brief Positive: «Apri in KiCad» refuses while the same preview folder is being rendered."""
    created, shown = [], []
    monkeypatch.setattr(window, "_kicad_output_complete", lambda: True)
    monkeypatch.setattr(window, "_refuse_kicad_command", lambda: False)
    monkeypatch.setattr(gui, "create_preview_project", lambda *a, **k: created.append(a))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    window._preview_render_worker = _RenderWorker((1, window._preview_render_key(), "X"))
    try:
        window._test_in_kicad()
        assert created == []
        assert len(shown) == 1 and "ancora in creazione" in shown[0]
        with pytest.raises(RuntimeError, match="ancora in creazione"):
            _guided_preview(window, monkeypatch)
    finally:
        window._preview_render_worker = None


def _guided_preview(window, monkeypatch):
    """@brief Run the panel's «Mostra in KiCad» with its gate passed."""
    monkeypatch.setattr(window, "_kicad_block_now", lambda: None)
    return window._show_check_in_kicad()


def test_r2_9_a_render_of_another_project_or_a_finished_one_does_not_block(window):
    """@brief Negative: another project's render reads another folder; a finished one reads nothing."""
    window._preview_render_worker = _RenderWorker((1, ("altra cartella", "Y"), "Y"))
    try:
        assert window._preview_render_busy_text() == ""
        window._preview_render_worker = _RenderWorker((1, window._preview_render_key(), "X"),
                                                      running=False)
        assert window._preview_render_busy_text() == ""
    finally:
        window._preview_render_worker = None


# ── R1-1: the profile manager is refused during a run ───────────────────────


class _ProfilesDialog:
    """@brief Stand-in of ProfilesDialog that records its creation."""

    created = []

    def __init__(self, store, parent):
        _ProfilesDialog.created.append(store)
        self.changed = False

    def discard_working_copy(self):
        """@brief Nothing to discard."""

    def deleteLater(self):  # noqa: N802 - Qt name
        """@brief Nothing to delete."""


def _profiles_stub(window, monkeypatch):
    """@brief Replace the dialog and its modal run; return the creation list."""
    _ProfilesDialog.created = []
    monkeypatch.setattr(gui, "ProfilesDialog", _ProfilesDialog)
    monkeypatch.setattr(window.i18n, "retranslate_widgets", lambda widget: None)
    monkeypatch.setattr(window, "_run_modal", lambda dialog: QDialog.DialogCode.Rejected)
    return _ProfilesDialog.created


def test_r1_1_the_profile_manager_is_refused_while_work_runs(window, monkeypatch):
    """@brief Positive: during an analysis the manager does not open and says why."""
    created = _profiles_stub(window, monkeypatch)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._edit_profiles()
    assert created == []
    assert len(shown) == 1 and "attendi la fine" in shown[0]


def test_r1_1_the_profile_manager_opens_when_idle(window, monkeypatch):
    """@brief Negative: with nothing running the manager opens as before."""
    created = _profiles_stub(window, monkeypatch)
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    window._automatic_generation_active = False
    window._extraction_busy = False
    window._edit_profiles()
    assert len(created) == 1


# ── R1-2: a failed detector review is not a review ──────────────────────────


class _ReviewWorker:
    """@brief A worker waiting for the detector review."""

    photo_detector = "yolo"

    def __init__(self, revision):
        self._analysis_revision = revision
        self.delivered = "nothing"

    def deliver_review(self, kept, reviewed=True):
        """@brief Record what the window delivered, and whether as reviewed."""
        self.delivered = kept
        self.reviewed = reviewed


PROPOSALS = [{"name": "U1", "x_mm": 0.0}, {"name": "C1", "x_mm": 1.0, "_yolo_review_only": True}]


def _review_setup(window, monkeypatch):
    """@brief A pending review request: worker in the slot, bundle writes recorded."""
    worker = _ReviewWorker(window._analysis_revision)
    window._extract_worker = worker
    window._reviewed_this_extraction = False
    bundles = []
    monkeypatch.setattr(window, "_write_review_bundle",
                        lambda rows, *a, **k: bundles.append((rows, k.get("reviewed"))) or None)
    monkeypatch.setattr(window.i18n, "retranslate_widgets", lambda widget: None)
    return worker, bundles


def test_r1_2_a_review_view_that_fails_delivers_an_unreviewed_set(window, monkeypatch):
    """@brief Positive: every proposal is delivered as NOT reviewed, the bundle is unreviewed.

    @note CONTRACT CHANGED 2026-11-17 (R1-2, service side): the window no
        longer drops the weak proposals nor marks the worker
        `_review_unreviewed` for a later correction of the result. It
        delivers the whole set with `reviewed=False` and the service treats
        it as it does without a reviewer (`photo_inventory.review_outcome`;
        the weak ones are left out by `yolo_only_components`, or judged by
        the paid reconciliation). Service side: tests/test_review_1117_b.py.
    """
    from kicadgen.gui import photo_review_dialog
    worker, bundles = _review_setup(window, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("vista rotta")
    monkeypatch.setattr(photo_review_dialog, "PhotoReviewDialog", broken)
    try:
        window._on_review_requested(b"png", PROPOSALS, 10.0, 10.0)
        assert [p["name"] for p in worker.delivered] == ["U1", "C1"]
        assert worker.reviewed is False                # the service is told
        assert len(bundles) == 1
        rows, reviewed = bundles[0]
        assert reviewed is False and [p["name"] for p in rows] == ["U1", "C1"]
        assert window._reviewed_this_extraction is False
    finally:
        window._extract_worker = None


def test_r1_2_an_answered_review_delivers_the_users_choice(window, monkeypatch):
    """@brief Negative: a review the user answered keeps a weak proposal they kept."""
    from kicadgen.gui import photo_review_dialog
    worker, bundles = _review_setup(window, monkeypatch)

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def kept_components(self):
            return [dict(PROPOSALS[1])]
    monkeypatch.setattr(photo_review_dialog, "PhotoReviewDialog", Dialog)
    monkeypatch.setattr(window, "_run_modal", lambda dialog: QDialog.DialogCode.Accepted)
    try:
        window._on_review_requested(b"png", PROPOSALS, 10.0, 10.0)
        assert [p["name"] for p in worker.delivered] == ["C1"]
        assert bundles[0][1] is True
        assert window._reviewed_this_extraction is True
        assert worker.reviewed is True
    finally:
        window._extract_worker = None


# ── R1-3: weight files from a project file stay inside the models folder ────


def _models_folder():
    """@brief The program's YOLO models folder."""
    return gui.resource_path(*gui.YOLO_MODELS_DIR)


def test_r1_3_a_weight_file_outside_the_models_folder_is_not_restored(window, monkeypatch, tmp_path):
    """@brief Positive: a project naming an external .pt brings the default back, with a log line."""
    lines = _logged(window, monkeypatch)
    state = window._project_state()
    state["widgets"]["field_yolo_model"] = str(tmp_path / "evil.pt")
    window._apply_project_state(state)
    assert window.field_yolo_model.text() == ""
    assert any("evil.pt" in line for line in lines)
    escaping = str(Path("..") / ".." / "evil.pt")
    assert window._trusted_model_file(escaping, _models_folder()) is None


def test_r1_3_a_weight_file_inside_the_models_folder_is_restored_absolute(window):
    """@brief Negative: a file of the program's folder comes back, a relative one resolved there."""
    state = window._project_state()
    state["widgets"]["field_yolo_model"] = "custom.pt"
    window._apply_project_state(state)
    expected = (_models_folder() / "custom.pt").resolve(strict=False)
    assert Path(window.field_yolo_model.text()) == expected
    # User decision 2026-11-16: an .onnx (no code inside) is restored as named,
    # a relative one in the models folder, never against the current folder.
    state["widgets"]["field_yolo_model"] = "custom.onnx"
    window._apply_project_state(state)
    assert Path(window.field_yolo_model.text()) == _models_folder() / "custom.onnx"


def test_r1_3_an_onnx_outside_the_models_folder_is_restored(window, tmp_path):
    """@brief Positive (policy of 2026-11-16): only a .pt outside the folder is refused."""
    state = window._project_state()
    state["widgets"]["field_yolo_model"] = str(tmp_path / "board.onnx")
    window._apply_project_state(state)
    assert window.field_yolo_model.text() == str(tmp_path / "board.onnx")


def test_r1_3_a_relative_field_never_resolves_against_the_current_folder(window):
    """@brief Positive: the detector options read a relative name in the models folder."""
    window.field_yolo_model.setText("custom.onnx")
    index = window.combo_photo_detector.findData("yolo")
    if index < 0:
        pytest.skip("the YOLO detector is not offered on this machine")
    window.combo_photo_detector.setCurrentIndex(index)
    _detector, path = window._photo_detector_options()
    assert path == _models_folder() / "custom.onnx"
    assert path.is_absolute()


# ── R1-5: a damaged «step_deferral» does not stop the project from opening ──


def test_r1_5_a_damaged_step_deferral_is_cleaned_not_raised(window):
    """@brief Positive: wrong types are dropped key by key, the project opens."""
    state = window._project_state()
    state["step_deferral"] = {"code": "missing", "fields": 5, "conflict": 7}
    window._apply_project_state(state)
    assert window._step_deferral == {"code": "missing", "conflict": None, "fields": []}


def test_r1_5_a_well_formed_step_deferral_is_kept(window):
    """@brief Negative: a valid record comes back as saved; an unknown code is dropped."""
    state = window._project_state()
    state["step_deferral"] = {"code": "conflict", "fields": ["body_height_mm", "x"],
                              "conflict": "5 mm o 6 mm"}
    window._apply_project_state(state)
    assert window._step_deferral == {"code": "conflict", "conflict": "5 mm o 6 mm",
                                     "fields": ["body_height_mm"]}
    state["step_deferral"] = {"code": "boh", "fields": []}
    window._apply_project_state(state)
    assert window._step_deferral is None


# ── R1-7: a worker that started before its start failed is abandoned ───────


class _Event:
    """@brief Minimal threading.Event stand-in."""

    def __init__(self):
        self.flag = False

    def set(self):
        """@brief Raise the flag."""
        self.flag = True


class _Signal:
    """@brief A signal stand-in that accepts connections."""

    def connect(self, slot):
        """@brief Accept and ignore the connection."""


class _RereadWorker:
    """@brief Stand-in of the re-read worker: start() makes it run."""

    def __init__(self, *args, **kwargs):
        self.running = False
        self.cancel_event = _Event()
        for name in ("done", "error", "progress", "phase", "finished"):
            setattr(self, name, _Signal())

    def start(self):
        """@brief Start running."""
        self.running = True

    def isRunning(self):  # noqa: N802 - Qt name
        """@brief Whether the stand-in runs."""
        return self.running


def test_r1_7_a_failure_after_start_abandons_the_running_worker(window, monkeypatch):
    """@brief Positive: the running worker becomes an abandoned run, its paid calls cancelled."""
    worker = _RereadWorker()
    worker.start()
    revision = window._analysis_revision
    try:
        assert window._abandon_if_running(worker) is True
        assert window._analysis_revision == revision + 1
        assert any(w is worker for w in window._abandoned_workers)
        assert worker.cancel_event.flag
    finally:
        worker.running = False
        window._abandoned_workers = [w for w in window._abandoned_workers if w is not worker]


def test_r1_7_a_worker_that_never_started_is_not_abandoned(window):
    """@brief Negative: nothing ran, so nothing is abandoned and the revision stays."""
    worker = _RereadWorker()
    revision = window._analysis_revision
    assert window._abandon_if_running(worker) is False
    assert window._abandon_if_running(None) is False
    assert window._analysis_revision == revision
    assert not any(w is worker for w in window._abandoned_workers)


def test_r1_7_reread_start_failing_after_start_keeps_the_worker_counted(window, monkeypatch):
    """@brief Positive: the re-read slot keeps the running worker and the error names the re-read."""
    errors, finished = [], []
    monkeypatch.setattr(window, "_reread_block_reason", lambda field: "")
    monkeypatch.setattr(window, "_ensure_api_key", lambda *a, **k: True)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([], None))
    monkeypatch.setattr(window.profile_store, "snapshot", lambda key: {"model": "m"})
    monkeypatch.setattr(window, "_reread_worker_class", lambda: _RereadWorker)
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: {})
    monkeypatch.setattr(window, "_begin_ai_run", lambda label: None)

    def broken(worker):
        raise RuntimeError("tracciamento rotto")
    monkeypatch.setattr(window, "_track_background_work", broken)
    monkeypatch.setattr(window, "_on_extract_finished", lambda: finished.append(True))
    monkeypatch.setattr(window, "_on_extract_error",
                        lambda message, reread_field=None: errors.append(reread_field))
    try:
        window._start_reread("pad_layout")
        worker = window._reread_worker
        assert worker is not None and worker.isRunning()
        assert any(w is worker for w in window._abandoned_workers)
        assert errors == ["pad_layout"] and finished == [True]
    finally:
        worker = window._reread_worker
        if worker is not None:
            worker.running = False
        window._abandoned_workers = []
        window._reread_worker = None
        window.tabs.setEnabled(True)
        window.analysis_overlay.hide()


# ── R3-2: the STEP regeneration rechecks «busy» after the modal question ────


def _regen_setup(window, monkeypatch, start_work):
    """@brief A STEP regeneration whose height question is answered (and may start other work)."""
    reached = []
    monkeypatch.setattr(window, "_current_body_params", lambda *a, **k: None)
    monkeypatch.setattr(window, "_stored_body_height_question", lambda: {"key": "h"})

    def answer():
        if start_work:
            window._automatic_generation_active = True
        return True
    monkeypatch.setattr(window, "_resolve_stored_body_height_question", answer)
    monkeypatch.setattr(window, "_missing_body_reason", lambda: reached.append(True) or "manca")
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    window._regen_3d_automatic = False
    window.package_geometry = None
    return reached


def test_r3_2_work_started_during_the_height_question_stops_the_regeneration(window, monkeypatch):
    """@brief Positive: after the modal, a run that started meanwhile wins; nothing else happens."""
    lines = _logged(window, monkeypatch)
    reached = _regen_setup(window, monkeypatch, start_work=True)
    try:
        window._regenerate_3d_applied()
        assert reached == []
        assert any("Altezza registrata" in line for line in lines)
    finally:
        window._automatic_generation_active = False


def test_r3_2_without_other_work_the_regeneration_goes_on(window, monkeypatch):
    """@brief Negative: an idle window continues to the body check after the answer."""
    reached = _regen_setup(window, monkeypatch, start_work=False)
    window._regenerate_3d_applied()
    assert reached == [True]


# ── R3-3: the pin-number toggle prepares the mesh off the GUI thread ────────


def _mesh_file(window, tmp_path):
    """@brief A generated mesh file set as the current one."""
    path = tmp_path / "board.preview.json"
    path.write_text("{}", encoding="utf-8")
    window._last_mesh_path = path
    return path


def test_r3_3_the_toggle_prepares_the_mesh_before_rendering(window, monkeypatch, tmp_path):
    """@brief Positive: nothing is parsed on the toggle; the render follows the prepared mesh."""
    path = _mesh_file(window, tmp_path)
    rendered = []
    monkeypatch.setattr(window.preview_3d, "render_mesh", lambda p: rendered.append(Path(p)))
    monkeypatch.setattr(window.preview_3d, "prepare_mesh", lambda p: 41, raising=False)
    monkeypatch.setattr(window.preview_3d, "mesh_prepared", object(), raising=False)
    window._on_pin_numbers_toggled(True)
    assert rendered == []
    assert window._mesh_request is not None and window._mesh_request[0] == 41
    window._on_mesh_prepared(41)
    assert rendered == [path]


def test_r3_3_a_viewer_without_preparation_renders_at_once(window, monkeypatch, tmp_path):
    """@brief Negative: the stub viewer (no off-thread preparation) renders synchronously, as before."""
    path = _mesh_file(window, tmp_path)
    rendered = []
    monkeypatch.setattr(window.preview_3d, "render_mesh", lambda p: rendered.append(Path(p)))
    window._on_pin_numbers_toggled(False)
    assert rendered == [path]


# ── R3-4: the reference-photo cache holds the photo it was made from ───────


def test_r3_4_the_cache_keeps_the_photo_object_alive(window):
    """@brief Positive: the entry retains the bytes, so their id cannot be reused meanwhile."""
    window._photo_png = _png_bytes(20, 10)
    window._reference_pixmap_cache = None
    pixmap = window._reference_pixmap({"source": "photo"})
    assert pixmap is not None
    assert window._reference_pixmap_cache[2] is window._photo_png


def test_r3_4_an_entry_of_another_photo_object_is_a_miss(window):
    """@brief Negative: same id key, different object -> reloaded, never the cached picture."""
    window._photo_png = _png_bytes(20, 10)
    sentinel = object()
    key = ("photo", id(window._photo_png))
    window._reference_pixmap_cache = (key, sentinel, b"another photo")
    assert window._reference_pixmap({"source": "photo"}) is not sentinel
    window._reference_pixmap_cache = (key, sentinel, window._photo_png)
    assert window._reference_pixmap({"source": "photo"}) is sentinel


# ── Coordinator item (fixer C1, R12-1): unreadable .env files at start-up ───


def test_env_files_the_launcher_skipped_are_named_with_their_reason(window):
    """@brief Positive: one sentence per skipped file, the encoding one with its remedy."""
    window._env_file_problems = [(Path("C:/a/.env"), "encoding", "utf-16 without BOM"),
                                 (Path("C:/b/.env"), "io", "permission denied")]
    lines = window._env_file_problem_lines()
    assert len(lines) == 2
    assert "UTF-8" in lines[0] and str(Path("C:/a/.env")) in lines[0]
    assert "permission denied" in lines[1] and str(Path("C:/b/.env")) in lines[1]


def test_env_files_no_problem_no_line_and_a_failing_check_is_silent(window, monkeypatch):
    """@brief Negative: nothing skipped -> no line; a failing check never stops the start."""
    from kicadgen import app_paths
    window._env_file_problems = []
    assert window._env_file_problem_lines() == []

    def broken():
        raise OSError("disk gone")
    monkeypatch.setattr(app_paths, "unreadable_env_files", broken)
    assert window._unreadable_env_files() == []


# ── Coordinator item (fixer C9, R2-9): the abandoned render is stopped ─────


class _StoppableRender(_RenderWorker):
    """@brief A render stand-in with `stop`, recording the call."""

    def __init__(self, token, running=True):
        super().__init__(token, running)
        self.stopped = 0

    def stop(self):
        """@brief Record the stop."""
        self.stopped += 1


def test_a_project_switch_stops_the_render_in_flight(window):
    """@brief Positive: the closing project's kicad-cli render is killed, the slot freed."""
    worker = _StoppableRender((1, window._preview_render_key(), "X"))
    window._preview_render_worker = worker
    window._switch_project("ALTRO", window._general_project_state)
    assert worker.stopped == 1
    assert window._preview_render_worker is None


def test_a_finished_render_is_not_stopped(window):
    """@brief Negative: a render already over is only forgotten."""
    worker = _StoppableRender((1, window._preview_render_key(), "X"), running=False)
    window._preview_render_worker = worker
    window._stop_preview_render()
    assert worker.stopped == 0 and window._preview_render_worker is None


def test_a_new_render_stops_the_previous_one(window, monkeypatch):
    """@brief Positive: starting a render kills the one it replaces (no STEP: no new thread)."""
    worker = _StoppableRender((1, ("altro", "Y"), "Y"))
    window._preview_render_worker = worker
    window._start_preview_render(object(), None, SimpleNamespace(has_step=False, name="X"))
    assert worker.stopped == 1


# ── Coordinator item (fixer C4, R11-8): collapsed drawings are said ─────────


COLLAPSED = {"kind": "polygon", "layer": "F.Fab", "width": 0.1,
             "points": [[0, 0], [2, 0], [2, 0], [0, 0]]}
SQUARE = {"kind": "polygon", "layer": "F.Fab", "width": 0.1,
          "points": [[0, 0], [2, 0], [2, 2], [0, 2]]}


def test_a_collapsed_drawing_is_logged_once_when_the_footprint_is_built(window, monkeypatch):
    """@brief Positive: the drawing left out of the footprint is said in the log, once."""
    lines = _logged(window, monkeypatch)
    params = window._current_params()
    window._footprint_signature_of(params, drawings=(COLLAPSED, SQUARE))
    window._pad_records_cache = {}
    window._footprint_signature_of(params, drawings=(COLLAPSED, SQUARE))
    assert len([line for line in lines if "ridotti a un punto" in line]) == 1


def test_well_formed_drawings_log_nothing(window, monkeypatch):
    """@brief Negative: drawings that all draw something leave no note."""
    lines = _logged(window, monkeypatch)
    window._footprint_signature_of(window._current_params(), drawings=(SQUARE,))
    assert not any("ridotti a un punto" in line for line in lines)


# ── Coordinator item (fixer C6, R6-8): one usage scope per paid run ────────


def _cost_capture(window, monkeypatch):
    """@brief Record the totals `_show_ai_cost` composes, without the balance request."""
    seen = []
    monkeypatch.setattr(window, "_usage_cost_parts",
                        lambda totals: seen.append(totals) or ("costo", [], []))
    monkeypatch.setattr(window.ai_account, "refresh_balance", lambda *a, **k: None)
    return seen


def test_r6_8_a_run_gets_its_own_scope_and_its_cost_comes_from_it(window, monkeypatch):
    """@brief Positive: the run start creates a scope (no global reset) and the cost reads it."""
    from kicadgen.services import openrouter
    resets = []
    monkeypatch.setattr(openrouter, "reset_usage", lambda: resets.append(True))
    monkeypatch.setattr(openrouter, "usage_totals", lambda: "process-wide")
    window._begin_ai_run("Analisi AI")
    try:
        scope = window._usage_scope
        assert isinstance(scope, openrouter.UsageScope)
        assert resets == [], "another run's tally is never zeroed"
        worker = SimpleNamespace()
        window._attach_usage_scope(worker)
        assert worker.usage_scope is scope
        monkeypatch.setattr(scope, "totals", lambda: "this run")
        seen = _cost_capture(window, monkeypatch)
        window._show_ai_cost()
        assert seen == ["this run"]
    finally:
        window.ai_account.release_run_project()


def test_r6_8_without_a_run_scope_the_process_tally_is_read(window, monkeypatch):
    """@brief Negative: no run started in this window -> the process-wide tally, as before."""
    from kicadgen.services import openrouter
    window._usage_scope = None
    monkeypatch.setattr(openrouter, "usage_totals", lambda: "process-wide")
    seen = _cost_capture(window, monkeypatch)
    window._show_ai_cost()
    assert seen == ["process-wide"]


# ── Coordinator item (fixer C5, R5-2): E ends a drag before it edits ───────


def _e_key_setup(window, monkeypatch, drawings=(), pads=(), hovered=-1):
    """@brief Record the order of «end the drag» and «edit» for one E press."""
    order = []
    monkeypatch.setattr(window, "_fp_selected_indices", lambda: list(drawings))
    monkeypatch.setattr(window, "_fp_selected_pads", lambda: list(pads))
    monkeypatch.setattr(window, "_end_footprint_drawing_interaction", lambda: order.append("end"))
    monkeypatch.setattr(window, "_focus_fp_properties", lambda: order.append("panel") or True)
    monkeypatch.setattr(window, "_edit_package_pad", lambda pad: order.append(("pad", pad)))
    monkeypatch.setattr(window, "_edit_drawing_properties", lambda i: order.append(("drawing", i)))
    monkeypatch.setattr(window.preview_view, "drawing_under_pointer", lambda: hovered)
    return order


def _press_e(window):
    """@brief Trigger the footprint preview's real E shortcut (as bound by the window)."""
    from PyQt6.QtGui import QKeySequence
    from kicadgen.gui.canvas_tool_hints import PROPERTIES_SHORTCUT
    wanted = QKeySequence(PROPERTIES_SHORTCUT)
    shortcut = next(s for s in window._fp_shortcuts if s.key() == wanted)
    shortcut.activated.emit()


def test_r5_2_e_ends_the_drag_before_editing_a_selection(window, monkeypatch):
    """@brief Positive: selected drawings or pads -> the drag ends first, then the editor opens."""
    order = _e_key_setup(window, monkeypatch, drawings=[0])
    _press_e(window)
    assert order == ["end", "panel"]
    order = _e_key_setup(window, monkeypatch, pads=["3"])
    _press_e(window)
    assert order == ["end", ("pad", "3")]


def test_r5_2_e_ends_the_drag_before_editing_the_drawing_under_the_pointer(window, monkeypatch):
    """@brief Positive: the hovered drawing is edited only after the drag ended."""
    window.footprint_drawings.append(dict(SQUARE))
    order = _e_key_setup(window, monkeypatch, hovered=len(window.footprint_drawings) - 1)
    _press_e(window)
    assert order == ["end", ("drawing", len(window.footprint_drawings) - 1)]


def test_r5_2_e_with_nothing_to_edit_leaves_the_drag_alone(window, monkeypatch):
    """@brief Negative: nothing selected, nothing under the pointer -> the drag goes on."""
    order = _e_key_setup(window, monkeypatch)
    _press_e(window)
    assert order == []


# ── Coordinator items (fixer C2): earlier rollbacks, check stopped on switch ─


def _install_result(**fields):
    """@brief A LibraryInstallResult stand-in with the fields the report reads."""
    base = dict(identifier="KiCadGen:X", footprint_path=Path("C:/lib/X.kicad_mod"),
                table_path=Path("C:/p/fp-lib-table"), step_path=Path("C:/lib/X.step"),
                wrl_path=None, symbol_library_path=None, symbol_skipped=None,
                unchanged=(), replaced=(), removed=(), rollback_left=None, bom_removed=(),
                restored_tables=(), symbol_identical=False)
    base.update(fields)
    return SimpleNamespace(**base)


def test_an_earlier_unfinished_install_is_named_in_both_reports(window):
    """@brief Positive: one line per backup folder, in the message box and in step 3's report."""
    folder = Path("C:/lib/.kicadgen-rollback-1")
    result = _install_result(earlier_rollbacks=(folder,))
    lines = window._install_earlier_rollback_lines(result)
    assert len(lines) == 1 and str(folder) in lines[0]
    assert lines[0] in window._install_result_message(result)
    assert lines[0] in window._install_report_summary(result, True)


def test_an_install_without_earlier_rollbacks_adds_no_line(window):
    """@brief Negative: no folder (or an older result without the field) -> no line."""
    assert window._install_earlier_rollback_lines(_install_result(earlier_rollbacks=())) == []
    assert window._install_earlier_rollback_lines(_install_result()) == []


class _StoppedCheckWorker:
    """@brief A check worker whose kicad-cli command is killed by the stop (CliStopped)."""

    def __init__(self):
        self.stopped = 0
        self.aborted = False

    def stop(self):
        """@brief The stop kills the command: the run will end as aborted."""
        self.stopped += 1
        self.aborted = True


def test_a_project_switch_leaves_the_running_check_idle_when_it_ends(window):
    """@brief Positive: switched mid-check, the run is stopped and ends idle, not «in corso»."""
    panel = window.kicad_check_panel
    worker = _StoppedCheckWorker()
    panel.worker, panel._running = worker, True
    window._switch_project("ALTRO", window._general_project_state)
    assert worker.stopped == 1
    panel._finished()                           # the worker's `finished` after CliStopped
    assert not panel._running
    assert panel.verdict.text() == "Controllo non ancora eseguito"


def test_an_idle_check_is_not_stopped_by_a_switch(window):
    """@brief Negative: no check running -> nothing to stop, the panel just goes idle."""
    panel = window.kicad_check_panel
    worker = _StoppedCheckWorker()
    panel.worker, panel._running = worker, False
    window._switch_project("ALTRO", window._general_project_state)
    assert worker.stopped == 0
    assert panel.verdict.text() == "Controllo non ancora eseguito"


# ── Generality audit S1: an analysis contact conflict is never written silently ─


def _manual_generation(window, monkeypatch, conflict):
    """@brief A manual «Rigenera footprint» after an analysis that left `conflict`."""
    started = []
    window._last_extracted = SimpleNamespace(pin_map_conflict=conflict)
    window._unread_pads = None
    monkeypatch.setattr(window, "_resolve_stored_pad_layout_question", lambda: True)
    monkeypatch.setattr(window, "_settle_footprint_draft_before_start", lambda: True)
    monkeypatch.setattr(window, "_generate_applied", lambda **kw: started.append(kw) or True)
    return started


def test_s1_a_conflict_is_shown_and_no_writes_nothing(window, monkeypatch):
    """@brief Positive: the conflict text is shown; «No» writes nothing and logs why."""
    started = _manual_generation(window, monkeypatch, "lato sinistro: 8 contatti o 9?")
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.No)
    lines = _logged(window, monkeypatch)
    assert window._generate() is False
    assert started == []
    assert len(asked) == 1 and "8 contatti o 9?" in asked[0]
    assert any("non ancora verificato" in line for line in lines)


def test_s1_a_confirmed_conflict_is_written_and_not_asked_again(window, monkeypatch):
    """@brief Positive: «Sì» writes, logs the accepted conflict, and the same extraction never asks again."""
    started = _manual_generation(window, monkeypatch, "lato sinistro: 8 contatti o 9?")
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.Yes)
    lines = _logged(window, monkeypatch)
    assert window._generate() is True
    assert window._generate() is True
    assert len(asked) == 1 and len(started) == 2
    assert any("verificati dall'utente" in line for line in lines)
    # An answer that replaces the extraction object keeps the confirmation
    # (same text, same analysis: final review follow-up, item 6) ...
    window._last_extracted = SimpleNamespace(pin_map_conflict="lato sinistro: 8 contatti o 9?")
    window._generate()
    assert len(asked) == 1
    # ... a new analysis with the same text is asked again.
    window._analysis_serial = getattr(window, "_analysis_serial", 0) + 1
    window._generate()
    assert len(asked) == 2


def test_s1_without_a_conflict_nothing_is_asked(window, monkeypatch):
    """@brief Negative: an analysis without conflict writes as before, no question."""
    started = _manual_generation(window, monkeypatch, None)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("no question expected"))
    assert window._generate() is True
    assert len(started) == 1


def test_s1_the_unattended_rewrite_holds_an_unverified_conflict(window, monkeypatch):
    """@brief Positive: the deferred rewrite never asks and never writes the conflicting contacts."""
    started = _manual_generation(window, monkeypatch, "numerazione in disaccordo")
    monkeypatch.setattr(window, "_pad_layout_question_block", lambda: "")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("no question expected"))
    window._fp_rewrite_flushing = True
    try:
        assert window._generate() is False
        assert started == []
        window._pin_map_conflict_record["confirmed"] = True       # as «Sì» records it
        assert window._generate() is True, "a verified conflict no longer holds the rewrite"
    finally:
        window._fp_rewrite_flushing = False


def test_s1_unread_pads_keep_their_own_block(window, monkeypatch):
    """@brief Negative: with unread pads the conflict is that block's detail, not a second question."""
    window._last_extracted = SimpleNamespace(pin_map_conflict="pad 3 non letto")
    monkeypatch.setattr(window, "_unread_pads_block", lambda: "pad 3 non letti")
    assert window._unverified_pin_map_conflict() == ""


# ── Coordinator item (fixer C1): the crash log watches the application ────


def test_main_hands_the_application_to_the_crash_report_watch(monkeypatch):
    """@brief Positive: the helper forwards the application to `watch_application`."""
    from kicadgen import crash_report
    seen = []
    monkeypatch.setattr(crash_report, "watch_application", lambda app: seen.append(app) or True)
    app = object()
    assert gui._watch_application_for_crash_log(app) is True
    assert seen == [app]


def test_a_missing_or_failing_watch_never_blocks_the_start(monkeypatch):
    """@brief Negative: no function, or one that raises, is a quiet False."""
    from kicadgen import crash_report

    def broken(app):
        raise RuntimeError("watch rotto")
    monkeypatch.setattr(crash_report, "watch_application", broken)
    assert gui._watch_application_for_crash_log(object()) is False
    monkeypatch.delattr(crash_report, "watch_application")
    assert gui._watch_application_for_crash_log(object()) is False


# ── Final round: count_unusable, terminal-pass stop, unreviewed presence ────


def _document_setup(window, monkeypatch, count):
    """@brief A castellated project whose document sides declare `count` contacts on one side."""
    window.group_castellated.setChecked(True)
    window._pin_numbers_printed = None
    monkeypatch.setattr(window, "_main_answer_for_reread",
                        lambda: {"pinout": [{"pin": "1", "name": "VCC"}]})
    window._document_sides = [{"edge": "left", "count": count, "pitch_mm": 2.54, "first_pin": 1}]


def test_unusable_side_counts_reach_the_component_check(window, monkeypatch):
    """@brief Positive: a total beyond the generators' bound is handed to the check."""
    from kicadgen.core.pad_limits import MAX_PAD_POSITIONS
    _document_setup(window, monkeypatch, MAX_PAD_POSITIONS + 1)
    unusable = window._component_check_document()["count_unusable"]
    assert unusable == {"total": MAX_PAD_POSITIONS + 1, "limit": MAX_PAD_POSITIONS}


def test_usable_side_counts_hand_no_unusable_count(window, monkeypatch):
    """@brief Negative: ordinary counts -> None, no blocking row."""
    _document_setup(window, monkeypatch, 4)
    assert window._component_check_document()["count_unusable"] is None


class _TerminalWorker:
    """@brief A running terminal pass with the worker's stop/cancelled contract."""

    def __init__(self):
        self.running = True
        self.stops = 0
        self.cancelled = False

    def isRunning(self):  # noqa: N802 - Qt name
        """@brief Whether the pass runs."""
        return self.running

    def stop(self):
        """@brief Record the stop."""
        self.stops += 1

    def can_cancel(self):
        """@brief The terminal pass can stop."""
        return True


def _close_event(window):
    """@brief Deliver one accepted close event; return whether it survived."""
    from PyQt6.QtGui import QCloseEvent
    event = QCloseEvent()
    event.accept()
    window.closeEvent(event)
    return event.isAccepted()


def test_r1_6_the_close_box_stops_the_terminal_pass_on_request(window, monkeypatch):
    """@brief Positive: «Interrompi la lettura» stops the pass; the window stays until it ends."""
    worker = _TerminalWorker()
    window._terminal_worker = worker
    monkeypatch.setattr(window, "_ask_stop_terminal_pass", lambda: True)
    try:
        assert _close_event(window) is False
        assert worker.stops == 1
        assert "Interruzione" in window.label_3d_status.text()
    finally:
        worker.running = False
        window._terminal_worker = None


def test_r1_6_waiting_leaves_the_terminal_pass_running(window, monkeypatch):
    """@brief Negative: «Attendi» stops nothing."""
    worker = _TerminalWorker()
    window._terminal_worker = worker
    monkeypatch.setattr(window, "_ask_stop_terminal_pass", lambda: False)
    try:
        assert _close_event(window) is False
        assert worker.stops == 0
    finally:
        worker.running = False
        window._terminal_worker = None


def test_r1_6_the_terminal_worker_turns_a_cancel_into_cancelled(monkeypatch, qapp):
    """@brief Positive: the service's CallCancelled is the user's stop, never «fallito»."""
    from kicadgen.gui.workers import TerminalDetailWorker
    from kicadgen.services import photo_terminals
    from kicadgen.services.ai_diagnostics import CallCancelled
    seen = {}

    def observe(photo, components, model, call, log, cache_dir=None, system_prompt=None,
                step_models=None, cancel=None):
        seen["cancel"] = cancel
        raise CallCancelled("stop")
    monkeypatch.setattr(photo_terminals, "observe_terminals", observe)
    worker = TerminalDetailWorker(b"png", [], "m")
    errors, done = [], []
    worker.error.connect(errors.append)
    worker.done.connect(done.append)
    worker.stop()
    worker.run()
    assert seen["cancel"] is worker.cancel and worker.cancel.is_set()
    assert worker.cancelled is True and errors == [] and done == []
    worker.deleteLater()


def test_r1_6_a_real_failure_stays_a_failure(monkeypatch, qapp):
    """@brief Negative: an error without a stop is still «Dettaglio terminali fallito»."""
    from kicadgen.gui.workers import TerminalDetailWorker
    from kicadgen.services import photo_terminals

    def observe(*args, **kwargs):
        raise ValueError("foto illeggibile")
    monkeypatch.setattr(photo_terminals, "observe_terminals", observe)
    worker = TerminalDetailWorker(b"png", [], "m")
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert worker.cancelled is False and len(errors) == 1 and "foto illeggibile" in errors[0]
    worker.deleteLater()


def test_r1_2_the_window_no_longer_corrects_the_result_of_a_failed_review(window):
    """@brief The window-side correction of «reviewed by the user» is gone: the service never stamps it.

    @note CONTRACT CHANGED 2026-11-17 (R1-2, service side): the two tests of
        `_unmark_user_reviewed_presence` (a failed-review run loses the
        stamp, a reviewed run keeps it) tested a patch on the result that
        no longer exists. The same pair is now tested where the stamp is
        written, in tests/test_review_1117_b.py
        (`test_r1_2_a_review_that_raises_marks_nothing_reviewed`,
        `test_r1_2_an_answered_review_marks_the_kept_bodies_reviewed`).
    """
    assert not hasattr(window, "_unmark_user_reviewed_presence")


# ── Final review follow-up ───────────────────────────────────────────────────


def test_followup_1_an_unverified_conflict_survives_a_reopen(window, monkeypatch):
    """@brief Positive: conflict -> save -> reopen -> «Genera» still asks."""
    window._last_extracted = SimpleNamespace(pin_map_conflict="lato destro: 6 contatti o 7?")
    window._unread_pads = None
    state = window._project_state()
    assert state["pin_map_conflict"] == {"text": "lato destro: 6 contatti o 7?", "confirmed": False}
    window._switch_project("ALTRO", window._general_project_state)
    assert window._unverified_pin_map_conflict() == ""
    window._switch_project("SCHEDA", state)
    assert window._last_extracted is None
    asked = []
    monkeypatch.setattr(window, "_resolve_stored_pad_layout_question", lambda: True)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.No)
    assert window._generate() is False
    assert len(asked) == 1 and "6 contatti o 7?" in asked[0]


def test_followup_1_a_confirmed_conflict_is_not_asked_after_a_reopen(window, monkeypatch):
    """@brief Negative: the user's confirmation is saved with the conflict."""
    state = window._project_state()
    state["pin_map_conflict"] = {"text": "lato destro: 6 contatti o 7?", "confirmed": True}
    window._switch_project("SCHEDA", state)
    assert window._unverified_pin_map_conflict() == ""
    state["pin_map_conflict"] = "damaged"
    window._switch_project("SCHEDA", state)
    assert window._pin_map_conflict_record is None


def test_followup_2_a_result_arriving_after_a_stop_is_dropped(window, monkeypatch):
    """@brief Positive: a lead pass (no cancel) that ends after the stop applies nothing."""
    import threading
    applied = []
    monkeypatch.setattr(window, "_set_photo_components", lambda rows: applied.append(rows))
    cancel = threading.Event()
    cancel.set()
    window._terminal_worker = SimpleNamespace(cancel=cancel, _model_revision=window._model_revision,
                                              _photo_snapshot=window._photo_png, components=[],
                                              only_leads=True)
    try:
        window._on_terminal_detail_done([{"name": "U1"}])
        assert applied == []
    finally:
        window._terminal_worker = None


def test_followup_2_only_a_pass_that_can_stop_is_offered_the_stop(monkeypatch, qapp):
    """@brief Positive/negative: the terminal pass can stop, the lead refinement cannot."""
    from kicadgen.gui.workers import TerminalDetailWorker
    terminals = TerminalDetailWorker(b"png", [], "m")
    leads = TerminalDetailWorker(b"png", [], "m", only_leads=True)
    assert terminals.can_cancel() is True
    assert leads.can_cancel() is False
    terminals.deleteLater()
    leads.deleteLater()


def test_followup_2_a_pass_that_cannot_stop_gets_the_plain_box(window, monkeypatch):
    """@brief Negative: no «Interrompi la lettura» for a pass that would not stop."""
    worker = _TerminalWorker()
    worker.can_cancel = lambda: False
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[1]))
    monkeypatch.setattr(window, "_ask_stop_terminal_pass", lambda: pytest.fail("no stop offer"))
    window._terminal_worker = worker
    try:
        assert _close_event(window) is False
        assert shown == ["Operazione in corso"] and worker.stops == 0
    finally:
        worker.running = False
        window._terminal_worker = None


def test_followup_4_pastes_over_a_solid_mark_the_step_stale_across_a_reopen(window, monkeypatch):
    """@brief Positive: pastes with a STEP on screen are recorded and said when the project reopens."""
    window._set_photo_components(_rows())
    window._last_mesh_path = Path("model.preview.json")
    monkeypatch.setattr(window, "_apply_photo_review", lambda *args, **kw: None)
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)
    window._on_format_painter_click(1)
    state = window._project_state()
    assert state["step_regen_pending"] is True
    window._switch_project("ALTRO", window._general_project_state)
    window._switch_project("SCHEDA", state)
    assert "Copia formato" in window.label_3d_status.text()
    window._show_generated_mesh(Path("C:/p/X.3dshapes/X.step"))
    assert window._step_regen_pending_saved is False


def test_followup_4_pastes_without_a_solid_mark_nothing(window, monkeypatch):
    """@brief Negative: no STEP on screen, nothing to call stale."""
    window._set_photo_components(_rows())
    window._last_mesh_path = None
    monkeypatch.setattr(window, "_apply_photo_review", lambda *args, **kw: None)
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(0)
    window._on_format_painter_click(1)
    assert window._project_state()["step_regen_pending"] is False
    window.btn_format_painter_3d.setChecked(False)


def test_followup_5_the_terminal_pass_runs_in_its_own_usage_scope(monkeypatch, qapp):
    """@brief Positive: the calls of the pass are made inside the scope the window gave it."""
    from kicadgen.gui.workers import TerminalDetailWorker
    from kicadgen.services import openrouter, photo_terminals
    seen = []
    monkeypatch.setattr(photo_terminals, "observe_terminals",
                        lambda *a, **k: seen.append(openrouter._active_scope.get()) or [])
    worker = TerminalDetailWorker(b"png", [], "m")
    worker.usage_scope = openrouter.UsageScope()
    worker.run()
    assert seen == [worker.usage_scope]
    worker.deleteLater()


def test_followup_5_without_a_scope_the_pass_runs_as_before(monkeypatch, qapp):
    """@brief Negative: no scope given -> no scope active, the process tally only."""
    from kicadgen.gui.workers import TerminalDetailWorker
    from kicadgen.services import openrouter, photo_terminals
    seen = []
    monkeypatch.setattr(photo_terminals, "observe_terminals",
                        lambda *a, **k: seen.append(openrouter._active_scope.get()) or [])
    worker = TerminalDetailWorker(b"png", [], "m")
    worker.run()
    assert seen == [None]
    worker.deleteLater()


# ── User-approved items of 2026-11-16 ────────────────────────────────────────


def test_a_lock_of_a_live_window_elsewhere_is_reported(tmp_path, monkeypatch):
    """@brief Positive: another live process of this host holds the project."""
    import json
    from kicadgen.services import project_lock
    monkeypatch.setattr(project_lock, "_process_started", lambda pid: 111)
    (tmp_path / project_lock.LOCK_FILE_NAME).write_text(json.dumps(
        {"pid": 424242, "host": project_lock._host(), "started": 111, "since": 0}), encoding="utf-8")
    assert project_lock.held_elsewhere(tmp_path)["pid"] == 424242


def test_a_stale_locks_are_not_reported(tmp_path, monkeypatch):
    """@brief Negative: dead process, reused pid, old foreign lock, damaged file, own lock."""
    import json
    import time
    from kicadgen.services import project_lock
    lock = tmp_path / project_lock.LOCK_FILE_NAME

    def write(**info):
        lock.write_text(json.dumps(info), encoding="utf-8")
    real_started = project_lock._process_started
    monkeypatch.setattr(project_lock, "_process_started", lambda pid: None)
    write(pid=424242, host=project_lock._host(), started=111, since=0)
    assert project_lock.held_elsewhere(tmp_path) is None            # process gone
    monkeypatch.setattr(project_lock, "_process_started", lambda pid: 222)
    assert project_lock.held_elsewhere(tmp_path) is None            # pid reused
    now = time.time()
    write(pid=1, host="altro-pc", started=5, since=now - project_lock.FOREIGN_LOCK_MAX_AGE_S - 60)
    assert project_lock.held_elsewhere(tmp_path) is None            # old foreign lock
    write(pid=1, host="altro-pc", started=5, since=now)
    assert project_lock.held_elsewhere(tmp_path)["host"] == "altro-pc"   # recent foreign: live
    lock.write_text("not json", encoding="utf-8")
    assert project_lock.held_elsewhere(tmp_path) is None            # damaged
    monkeypatch.setattr(project_lock, "_process_started", real_started)
    held = project_lock.acquire(tmp_path)
    assert held is not None and project_lock.held_elsewhere(tmp_path) is None   # ours
    assert held.release() and not lock.exists()


def test_a_opening_a_project_locked_elsewhere_asks(window, monkeypatch):
    """@brief Positive: «Annulla» refuses the open; negative: no lock opens without a question."""
    from kicadgen.services import project_lock
    folder = gui.DEFAULT_LIBRARY_ROOT / "BLOCCATO"
    monkeypatch.setattr(project_lock, "held_elsewhere",
                        lambda f: {"pid": 9, "host": "altro-pc"} if Path(f).name == "BLOCCATO" else None)
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: asked.append(box.windowTitle()) or 0)
    assert window._confirm_open_locked(folder) is False
    assert asked == ["Progetto già aperto in un'altra finestra di KiCadGen"]
    assert window._confirm_open_locked(gui.DEFAULT_LIBRARY_ROOT / "LIBERO") is True
    assert len(asked) == 1


def test_a_the_window_locks_the_project_it_opens_and_frees_it_on_switch(window):
    """@brief Positive: a switch to a named project writes the lock, the next switch removes it."""
    from kicadgen.services import project_lock
    folder = window._library_root() / "CON_LOCK"
    (folder / "docs").mkdir(parents=True)
    window._switch_project("CON_LOCK", window._general_project_state)
    assert project_lock.is_own(project_lock.read_lock(folder))
    window._switch_project("", window._general_project_state)
    assert not project_lock.lock_path(folder).exists()


def test_a_config_saves_only_the_keys_this_window_changed(window, monkeypatch):
    """@brief Positive: another window's key on disk survives; this window's change wins."""
    disk = {"language": "it", "show_grid": True, "grid_unit": "mm"}
    written = []
    monkeypatch.setattr(gui, "load_config", lambda report=None, require_file=False: dict(disk))
    monkeypatch.setattr(gui, "save_config", lambda config: written.append(dict(config)) or True)
    window._config_baseline = {"language": "it", "show_grid": True, "grid_unit": "mm"}
    window._config = dict(window._config_baseline)
    disk["grid_unit"] = "inch"                       # changed by another window
    window._config["show_grid"] = False              # changed by this window
    assert window._save_config_changes() is True
    assert written[-1]["grid_unit"] == "inch" and written[-1]["show_grid"] is False
    assert window._config["grid_unit"] == "inch", "the other window's value is adopted"


def test_a_without_changes_the_disk_is_written_back_unchanged(window, monkeypatch):
    """@brief Negative: a window that changed nothing never overwrites another's value."""
    disk = {"language": "en", "show_grid": False}
    written = []
    monkeypatch.setattr(gui, "load_config", lambda report=None, require_file=False: dict(disk))
    monkeypatch.setattr(gui, "save_config", lambda config: written.append(dict(config)) or True)
    window._config_baseline = {"language": "it", "show_grid": True}
    window._config = dict(window._config_baseline)
    window._save_config_changes()
    assert written[-1] == disk


def test_b_an_earlier_rollback_offers_a_button_that_marks_it(window, monkeypatch, tmp_path):
    """@brief Positive: the button asks, marks the folder, logs and disappears."""
    from kicadgen.services import kicad_library
    folder = tmp_path / ".kicadgen-rollback-x"
    marked = []
    monkeypatch.setattr(kicad_library, "acknowledge_rollback_dir",
                        lambda path: marked.append(Path(path)) or True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    lines = _logged(window, monkeypatch)
    window._last_install_report = (_install_result(earlier_rollbacks=(folder,)), True)
    window._refresh_install_report()
    assert not window.install_rollback_box.isHidden()
    assert window._acknowledge_rollback(folder) is True
    assert marked == [folder] and any("controllata" in line for line in lines)
    assert window.install_rollback_box.isHidden()


def test_b_a_refused_or_failed_mark_warns_and_keeps_the_button(window, monkeypatch, tmp_path):
    """@brief Negative: «No» does nothing; a refused folder is a warning, never a crash."""
    from kicadgen.services import kicad_library
    folder = tmp_path / ".kicadgen-rollback-x"
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    assert window._acknowledge_rollback(folder) is False
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a[2]))
    monkeypatch.setattr(kicad_library, "acknowledge_rollback_dir", lambda path: False)
    assert window._acknowledge_rollback(folder) is False
    assert len(warned) == 1


def test_c_a_module_analysis_without_board_size_says_the_size_is_inherited(window, monkeypatch,
                                                                         raw_params):  # noqa: F811
    """@brief Positive: a known size kept from before is said once in the log."""
    params = raw_params
    lines = _logged(window, monkeypatch)
    window._board_size_known = True
    params.body_length_mm = None
    params.body_width_mm = None
    window._on_extracted(params)
    assert any("non ha letto le quote della scheda" in line for line in lines)


def test_c_a_module_analysis_with_its_own_size_says_nothing(window, monkeypatch,
                                                           raw_params):  # noqa: F811
    """@brief Negative: the size read by this analysis needs no notice."""
    params = raw_params
    lines = _logged(window, monkeypatch)
    window._board_size_known = True
    window._on_extracted(params)
    assert not any("non ha letto le quote della scheda" in line for line in lines)


def test_e_retry_unmeasurable_models_forgets_the_verdicts(window, monkeypatch):
    """@brief Positive: the menu entry calls the service and says how many verdicts went."""
    from kicadgen.cad3d import kicad_models
    calls = []
    monkeypatch.setattr(kicad_models, "retry_unmeasurable_models", lambda: calls.append(1) or 3)
    lines = _logged(window, monkeypatch)
    window._refresh_retry_unmeasurable_action()
    assert window.action_retry_unmeasurable.isEnabled()
    window.action_retry_unmeasurable.trigger()
    assert calls == [1] and any("rimisurati" in line for line in lines)


def test_e_retry_is_off_while_a_generation_runs(window, monkeypatch):
    """@brief Negative: during a generation the entry is disabled and the call refused."""
    from kicadgen.cad3d import kicad_models
    calls = []
    monkeypatch.setattr(kicad_models, "retry_unmeasurable_models", lambda: calls.append(1) or 0)
    window._automatic_generation_active = True
    try:
        window._refresh_retry_unmeasurable_action()
        assert not window.action_retry_unmeasurable.isEnabled()
        window._retry_unmeasurable_models()
        assert calls == []
    finally:
        window._automatic_generation_active = False


def test_f_an_unseen_crash_report_is_shown_once(window, monkeypatch, tmp_path):
    """@brief Positive: a crash report not yet shown opens the notice and is marked seen."""
    from kicadgen import crash_report
    report = tmp_path / "crash-20261116.txt"
    seen = []
    monkeypatch.setattr(crash_report, "sessions_ended_without_exit", lambda: 0)
    monkeypatch.setattr(crash_report, "unseen_crash_report", lambda: report)
    monkeypatch.setattr(crash_report, "mark_crash_report_seen", lambda p: seen.append(p) or True)
    assert window._offer_crash_notice() is True
    assert seen == [report]
    assert not window._crash_notice.isModal()
    window._crash_notice.close()


def test_f_a_killed_session_or_no_report_shows_nothing(window, monkeypatch):
    """@brief Negative: a session ended by a kill is not an error; no report, no notice; failures are quiet."""
    from kicadgen import crash_report
    monkeypatch.setattr(crash_report, "sessions_ended_without_exit", lambda: 1)
    monkeypatch.setattr(crash_report, "unseen_crash_report", lambda: pytest.fail("not asked"))
    assert window._offer_crash_notice() is False
    monkeypatch.setattr(crash_report, "sessions_ended_without_exit", lambda: 0)
    monkeypatch.setattr(crash_report, "unseen_crash_report", lambda: None)
    assert window._offer_crash_notice() is False

    def broken():
        raise OSError("log folder gone")
    monkeypatch.setattr(crash_report, "unseen_crash_report", broken)
    assert window._offer_crash_notice() is False


# ── Item G (R5-7): the real window binds the footprint panel's two-step Escape ─


def test_g_the_window_binds_the_footprint_panel_escape(qapp, fp):  # noqa: F811
    """@brief Positive: with the window's own binding, Escape restores a field, the next one disarms the tool."""
    from tests.test_review_1116_c5 import _footprint_with_selected_drawing, _two_step_escape
    window = fp
    _footprint_with_selected_drawing(window)
    window._arm_footprint_tool('line')
    qapp.processEvents()
    assert window.preview_view.tool_armed()
    _two_step_escape(qapp, window.fp_properties_panel, window.preview_view.tool_armed,
                     window._fp_selected_indices)


def test_g_without_a_tool_the_panel_escape_sends_nothing(qapp, fp):  # noqa: F811
    """@brief Negative: no tool armed, the window's binding keeps the selection."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from tests.test_review_1116_c5 import _footprint_with_selected_drawing, _x_editor
    window = fp
    _footprint_with_selected_drawing(window)
    assert not window.preview_view.tool_armed()
    editor = _x_editor(window.fp_properties_panel)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window._fp_selected_indices() == [0]


# ── Review F of the third round ──────────────────────────────────────────────


def _shared_config_file(monkeypatch, disk):
    """@brief A config.json shared by windows: reads copy `disk`, writes replace it."""
    def read(report=None, require_file=False):
        return dict(disk)

    def write(config):
        disk.clear()
        disk.update(config)
        return True
    monkeypatch.setattr(gui, "load_config", read)
    monkeypatch.setattr(gui, "save_config", write)


def test_f1_a_failed_read_writes_this_windows_config_and_adopts_no_defaults(window, monkeypatch):
    """@brief Positive: a locked/corrupt file (no read) -> this window's dict written as is."""
    written = []
    monkeypatch.setattr(gui, "load_config",
                        lambda report=None, require_file=False: None if require_file else {})
    monkeypatch.setattr(gui, "save_config", lambda config: written.append(dict(config)) or True)
    window._config_baseline = {"language": "en", "custom_key": 1}
    window._config.clear()
    window._config.update({"language": "en", "custom_key": 1, "show_grid": False})
    window._save_config_changes()
    assert written[-1] == {"language": "en", "custom_key": 1, "show_grid": False}
    assert window._config["language"] == "en" and window._config["custom_key"] == 1


def test_f1_f8_a_read_file_is_merged_and_vanished_keys_are_dropped(window, monkeypatch):
    """@brief Negative of F1 / positive of F8: a real read merges; a non-default key gone from disk goes."""
    disk = {"language": "de"}
    _shared_config_file(monkeypatch, disk)
    window._config_baseline = {"language": "it", "old_private_key": 3}
    window._config.clear()
    window._config.update({"language": "it", "old_private_key": 3, "show_grid": False})
    window._save_config_changes()
    assert disk == {"language": "de", "show_grid": False}
    assert window._config["language"] == "de" and "old_private_key" not in window._config


def test_f2_opening_a_project_keeps_the_language_another_window_chose(window, monkeypatch, tmp_path):
    """@brief Positive: window B changes the language, window A records a recent project, B's language stays."""
    disk = dict(window._config)
    _shared_config_file(monkeypatch, disk)
    window._config_baseline = dict(window._config)
    disk["language"] = "fr"                                  # window B, meanwhile
    folder = tmp_path / "P1"
    (folder / "docs").mkdir(parents=True)
    opened = window._open_project_folder
    window._open_project_folder = folder
    try:
        window._remember_recent_project(None)
    finally:
        window._open_project_folder = opened
    assert disk["language"] == "fr"
    assert str(folder) in disk.get("recent_projects")


def test_f2_the_documents_panel_writes_through_the_window(window, monkeypatch, tmp_path):
    """@brief Positive: the browse folder is saved per key; negative: another key on disk survives."""
    disk = dict(window._config)
    _shared_config_file(monkeypatch, disk)
    window._config_baseline = dict(window._config)
    disk["language"] = "de"
    picked = tmp_path / "foto.png"
    picked.write_bytes(b"x")
    from kicadgen.gui import app_config
    app_config.remember_browse_dir(window.guided_docs.config, str(picked),
                                   save=window.guided_docs.save_config)
    assert disk["last_document_dir"] == str(tmp_path) and disk["language"] == "de"


def test_f3_a_recent_project_locked_elsewhere_changes_nothing_on_cancel(window, monkeypatch, tmp_path):
    """@brief Positive: «Annulla» before anything moves: same working folder, same project."""
    from kicadgen.gui import main_window
    from kicadgen.services import project_lock
    other_root = tmp_path / "altra_cartella"
    folder = other_root / "LOCKED"
    (folder / "docs").mkdir(parents=True)
    monkeypatch.setattr(main_window.recent_projects, "is_project", lambda path: True)
    monkeypatch.setattr(project_lock, "held_elsewhere", lambda f: {"pid": 9, "host": "altro"})
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)        # «Annulla»
    prepared = []
    monkeypatch.setattr(window, "_prepare_project_open", lambda f: prepared.append(f))
    root_before = window._library_root()
    window._open_recent_project(str(folder))
    assert window._library_root() == root_before and prepared == []


def test_f3_a_free_recent_project_goes_on_without_the_lock_question(window, monkeypatch, tmp_path):
    """@brief Negative: no lock elsewhere -> the open proceeds to its next question."""
    from kicadgen.gui import main_window
    from kicadgen.services import project_lock
    folder = tmp_path / "altra" / "FREE"
    (folder / "docs").mkdir(parents=True)
    monkeypatch.setattr(main_window.recent_projects, "is_project", lambda path: True)
    monkeypatch.setattr(project_lock, "held_elsewhere", lambda f: None)
    prepared = []
    monkeypatch.setattr(window, "_prepare_project_open", lambda f: prepared.append(f))
    window._open_recent_project(str(folder))
    assert prepared == [folder]


def test_f4_a_project_save_refreshes_the_lock(window, monkeypatch):
    """@brief Positive: the ordinary save is the heartbeat; negative: no lock, nothing to refresh."""
    refreshed = []
    window._project_lock = SimpleNamespace(refresh=lambda: refreshed.append(1) or True,
                                           release=lambda: True, folder=Path("."))
    window._save_project_config(force=True)
    assert refreshed == [1]
    window._project_lock = None
    window._refresh_project_lock()
    assert refreshed == [1]


def test_f5_a_renamed_project_keeps_its_lock_under_the_new_folder(window, tmp_path):
    """@brief Positive: the lock follows the rename; the old path holds none."""
    import os
    from kicadgen.services import project_lock
    old, new = tmp_path / "VECCHIO", tmp_path / "NUOVO"
    old.mkdir()
    window._project_lock = project_lock.acquire(old)
    os.rename(old, new)                        # the folder moves with its lock file
    window._rebind_project_lock(old, new)
    assert Path(window._project_lock.folder) == new
    assert project_lock.is_own(project_lock.read_lock(new))
    window._release_project_lock()
    assert not project_lock.lock_path(new).exists()


def test_f6_a_foreign_lock_from_the_future_is_stale_within_skew_live(monkeypatch):
    """@brief Positive: far in the future = stale; negative: a small skew stays live."""
    import time
    from kicadgen.services import project_lock
    now = time.time()
    far = {"pid": 1, "host": "altro-pc", "since": now + project_lock.FOREIGN_CLOCK_SKEW_S + 60}
    near = {"pid": 1, "host": "altro-pc", "since": now + 30}
    assert project_lock.lock_is_live(far, now) is False
    assert project_lock.lock_is_live(near, now) is True


def test_f7_a_process_of_another_account_is_alive(monkeypatch):
    """@brief Positive: access denied = alive; negative: no such process = stale."""
    from kicadgen.services import project_lock
    info = {"pid": 4, "host": project_lock._host(), "started": 123, "since": 0}
    monkeypatch.setattr(project_lock, "_process_started", lambda pid: project_lock.STARTED_UNKNOWN)
    assert project_lock.lock_is_live(info) is True
    monkeypatch.setattr(project_lock, "_process_started", lambda pid: None)
    assert project_lock.lock_is_live(info) is False


def test_f7_this_process_is_seen_alive_with_its_own_start_time():
    """@brief Positive on the real system call: our own pid reads alive with a stamp."""
    import os
    from kicadgen.services import project_lock
    started = project_lock._process_started(os.getpid())
    assert started is not None and started != project_lock.STARTED_UNKNOWN


def test_f9_an_acknowledged_folder_is_no_longer_named_in_the_report(window, tmp_path):
    """@brief Positive: after «controllata» its line goes; negative: another folder keeps its line."""
    first, second = tmp_path / ".kicadgen-rollback-a", tmp_path / ".kicadgen-rollback-b"
    result = _install_result(earlier_rollbacks=(first, second))
    window._acknowledged_rollbacks = {str(first)}
    lines = window._install_earlier_rollback_lines(result)
    assert len(lines) == 1 and str(second) in lines[0]


def test_f10_a_second_window_never_removes_the_first_windows_lock(tmp_path, monkeypatch):
    """@brief Positive: after «Apri comunque» the lock is re-taken by the window still open."""
    import json
    from kicadgen.services import project_lock
    mine = project_lock.acquire(tmp_path)
    other = {"pid": 999999, "host": project_lock._host(), "started": 7, "since": 0}
    project_lock.lock_path(tmp_path).write_text(json.dumps(other), encoding="utf-8")
    # The other window is alive (since review H, 2 a dead one is re-taken).
    real = project_lock._process_started
    monkeypatch.setattr(project_lock, "_process_started",
                        lambda pid: 7 if pid == 999999 else real(pid))
    assert mine.refresh() is False                       # the other window holds it now
    assert mine.release() is True and project_lock.lock_path(tmp_path).exists()   # left alone
    project_lock.lock_path(tmp_path).unlink()            # the other window closed
    assert mine.refresh() is True
    assert project_lock.is_own(project_lock.read_lock(tmp_path))
    assert mine.release() and not project_lock.lock_path(tmp_path).exists()


# ── Review H ────────────────────────────────────────────────────────────────


def test_h1_two_windows_never_lose_each_others_recent_projects(window, tmp_path):
    """@brief Positive: A adds X, B (stale list) adds Y -> both on disk, most recent first."""
    base = {"recent_projects": [str(tmp_path / "OLD")]}
    a_mine = {"recent_projects": [str(tmp_path / "X"), str(tmp_path / "OLD")]}
    disk_after_a = window._merge_recent_lists(base, a_mine, base)
    assert disk_after_a == [str(tmp_path / "X"), str(tmp_path / "OLD")]
    b_mine = {"recent_projects": [str(tmp_path / "Y"), str(tmp_path / "OLD")]}
    disk = {"recent_projects": disk_after_a}
    merged = window._merge_recent_lists(base, b_mine, disk)
    assert merged == [str(tmp_path / "Y"), str(tmp_path / "X"), str(tmp_path / "OLD")]


def test_h1_a_forgotten_project_leaves_the_disk_list_and_nothing_else_moves(window, tmp_path):
    """@brief Negative: a removal is applied; another window's addition and the order stay."""
    paths = [str(tmp_path / name) for name in ("A", "B", "C")]
    base = {"recent_projects": list(paths)}
    mine = {"recent_projects": [paths[0], paths[2]]}               # B forgotten here
    disk = {"recent_projects": [str(tmp_path / "Z")] + paths}      # Z added elsewhere
    assert window._merge_recent_lists(base, mine, disk) == [str(tmp_path / "Z"), paths[0], paths[2]]


def test_h1_the_save_path_merges_the_recent_list(window, monkeypatch, tmp_path):
    """@brief Positive through `_save_config_changes`: B's save keeps A's project."""
    disk = dict(window._config)
    _shared_config_file(monkeypatch, disk)
    window._config_baseline = dict(window._config)
    disk["recent_projects"] = [str(tmp_path / "FROM_A")]          # window A saved meanwhile
    window._config["recent_projects"] = [str(tmp_path / "FROM_B")] + list(
        window._config_baseline.get("recent_projects") or [])
    window._save_config_changes()
    assert disk["recent_projects"][:2] == [str(tmp_path / "FROM_B"), str(tmp_path / "FROM_A")]
    assert window._config["recent_projects"] == disk["recent_projects"]


def test_h2_a_dead_foreign_lock_is_retaken_a_live_one_is_not(tmp_path, monkeypatch):
    """@brief Positive: the window that overrode crashed -> re-taken; negative: still live -> left."""
    import json
    from kicadgen.services import project_lock
    mine = project_lock.acquire(tmp_path)
    other = {"pid": 999999, "host": project_lock._host(), "started": 7, "since": 0}
    project_lock.lock_path(tmp_path).write_text(json.dumps(other), encoding="utf-8")
    real = project_lock._process_started
    monkeypatch.setattr(project_lock, "_process_started",
                        lambda pid: 7 if pid == 999999 else real(pid))
    assert mine.refresh() is False                       # live: left alone
    monkeypatch.setattr(project_lock, "_process_started",
                        lambda pid: None if pid == 999999 else real(pid))
    assert mine.refresh() is True                        # dead: re-taken
    assert project_lock.is_own(project_lock.read_lock(tmp_path))
    mine.release()


def test_h3_the_colours_choice_never_overwrites_an_unreadable_file(monkeypatch, tmp_path):
    """@brief Positive: a locked/corrupt file -> nothing written; negative: a missing file is created."""
    from kicadgen.gui import app_config, model_candidates_dialog
    written = []
    monkeypatch.setattr(app_config, "save_config", lambda config: written.append(dict(config)) or True)
    target = tmp_path / "config.json"
    monkeypatch.setattr(app_config, "config_path", lambda: target)
    target.write_text("{not json", encoding="utf-8")
    model_candidates_dialog._remember_preview_colors(False)
    assert written == []
    target.unlink()
    model_candidates_dialog._remember_preview_colors(False)
    assert written and written[-1][model_candidates_dialog.PREVIEW_COLORS_KEY] is False


def test_h4_a_failing_read_writes_the_window_config_never_defaults(window, monkeypatch):
    """@brief Positive: an exception while reading is no read; negative: a real read still merges."""
    written = []

    def broken(report=None, require_file=False):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(gui, "load_config", broken)
    monkeypatch.setattr(gui, "save_config", lambda config: written.append(dict(config)) or True)
    window._config_baseline = dict(window._config)
    window._config["language"] = "de"
    window._save_config_changes()
    assert written[-1] == window._config


# ── Real run of 2026-11-16 ───────────────────────────────────────────────────


def _module_with_override(window, monkeypatch, pin):
    """@brief A castellated module with a host pad override on `pin`, log captured."""
    lines = _logged(window, monkeypatch)
    window.group_castellated.setChecked(True)
    window._board_size_known = True
    window.combo_mounting.setCurrentIndex(window.combo_mounting.findData("headers"))
    monkeypatch.setattr(window, "_photo_inventory_family", lambda: True)
    window.header_pad_overrides = {pin: {"shape": "circle", "size": 2.4}}
    window._stale_pad_overrides_reported = ()
    return lines


def _sides_extraction(counts):
    """@brief An extraction with one castellated side per (edge, count, first pin)."""
    from kicadgen.services.datasheet_extractor import ExtractedParams
    sides = [dict(edge=edge, count=count, pitch_mm=2.54, first_pin=first, contact_style="half_hole")
             for edge, count, first in counts]
    return ExtractedParams(name="", description="", tags="", confidence="medium", notes="",
                           layout="castellated", castellated_sides=sides,
                           body_length_mm=30.0, body_width_mm=15.2, photo_source="",
                           components_3d=[])


def test_run1_a_rebuild_never_reports_an_override_whose_pin_exists_at_the_end(window, monkeypatch):
    """@brief Positive: pin 1 lives on the SECOND side: nothing reported while the sides are rebuilt."""
    lines = _module_with_override(window, monkeypatch, "1")
    window._apply_extracted_castellated(_sides_extraction([("left", 4, 5), ("right", 4, 1)]))
    assert not any("non applicate" in line for line in lines)


def test_run1_an_override_on_a_pin_that_is_gone_is_reported_once(window, monkeypatch):
    """@brief Negative: a pin that truly disappeared is still reported, once."""
    lines = _module_with_override(window, monkeypatch, "9")
    window._apply_extracted_castellated(_sides_extraction([("left", 4, 1), ("right", 4, 5)]))
    window._draw_preview()
    assert len([line for line in lines if "non applicate" in line]) == 1


def test_run1_the_batch_refreshes_once_at_the_end(window, monkeypatch):
    """@brief Positive: a batch postpones every side change to one refresh; negative: outside it each change refreshes."""
    drawn = []
    monkeypatch.setattr(window, "_draw_preview", lambda: drawn.append(len(window._sides)))
    with window._sides_batch():
        window._sides.clear()
        window._add_side_row({"edge": "left", "count": 2, "pitch_mm": 2.54, "first_pin": 1})
        window._add_side_row({"edge": "right", "count": 2, "pitch_mm": 2.54, "first_pin": 3})
        assert drawn == []
    assert drawn == [2]
    window._add_side_row({"edge": "top", "count": 2, "pitch_mm": 2.54, "first_pin": 5})
    assert drawn == [2, 3]


def _annotation_warning_lines(window, monkeypatch, footprint_only):
    """@brief Run one generation up to the worker with 3D annotations and no STEP body."""
    lines = _logged(window, monkeypatch)
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    window.group_castellated.setChecked(False)
    window.group_3d.setChecked(False)
    window.package_geometry = None
    window._acquisition_completed = True
    window.fillet_annotations = [object()]
    try:
        window._generate_applied(footprint_only=footprint_only)
    finally:
        window.fillet_annotations = []
        window._worker = None
    return [line for line in lines if "annotazioni 3D presenti" in line]


def test_run2_a_footprint_only_rewrite_does_not_warn_about_3d_annotations(window, monkeypatch):
    """@brief Positive: «Applica ai file» / «Rigenera footprint» build no STEP: no warning."""
    assert _annotation_warning_lines(window, monkeypatch, footprint_only=True) == []


def test_run2_a_full_generation_without_a_step_still_warns(window, monkeypatch):
    """@brief Negative: a generation that would build the STEP but cannot still says so."""
    assert len(_annotation_warning_lines(window, monkeypatch, footprint_only=False)) == 1


# ── The castellated pad overhang is an estimate unless declared (C4, 2026-11-16) ─


def _declared_module(window):
    """@brief A castellated module whose only possible estimate is the overhang."""
    window.group_castellated.setChecked(True)
    window._board_size_known = True
    window._sides[:] = [gui.normalised_side(dict(
        edge="left", count=3, pitch_mm=2.54, first_pin=1, contact_style="half_hole",
        pad_length_mm=2.0, pad_width_mm=1.5, half_hole_diameter_mm=1.0,
        pad_length_declared=True, pad_width_declared=True, half_hole_diameter_declared=True))]


def test_overhang_untouched_default_is_an_estimate(window):
    """@brief Positive: the untouched 1.4 mm reaches the generator as an estimate.

    @note CONTRACT CHANGED 2026-11-16 (user decision): the assumed overhang is
        reported only in the footprint description, never as a «Pad stimati»
        mark on the pads (no editor for it: a permanent row with no remedy).
    """
    from kicadgen.generators.castellated_module import is_declared_size
    _declared_module(window)
    window._cast_declared_sizes = set()
    params = window._current_castellated_params()
    assert not is_declared_size(params.pad_overhang)
    assert params.pad_overhang == pytest.approx(1.4)
    assert "pad_size" not in (params.contact_sources(params.sides[0]) or {})
    # Listed as an estimate; its value is the generator's (C4 reduces an
    # assumed overhang so the declared pad covers the half-hole, here 1.12 mm).
    description = params.assumed_sizes_description()
    assert "pad overhang" in description and "estimate" in description


def test_overhang_declared_value_is_a_measure_and_survives_reopen_and_analysis(window, raw_params):
    """@brief Negative: a declared overhang is a measure, kept by a reopen and by a new analysis."""
    from kicadgen.generators.castellated_module import is_declared_size
    _declared_module(window)
    window.field_cast_overhang.setValue(0.8)
    window._cast_declared_sizes = {"field_cast_overhang"}
    params = window._current_castellated_params()
    assert is_declared_size(params.pad_overhang) and params.pad_overhang == pytest.approx(0.8)
    assert params.contact_sources(params.sides[0]) is None
    state = window._project_state()
    window._cast_declared_sizes = set()
    window._apply_project_state(state)
    assert "field_cast_overhang" in window._cast_declared_sizes
    window._on_extracted(raw_params)                      # a new analysis reads no overhang
    assert "field_cast_overhang" in window._cast_declared_sizes
    assert window.field_cast_overhang.value() == pytest.approx(0.8)


def test_overhang_legacy_project_reads_the_old_rule(window):
    """@brief A project saved before the flag: 1.4 is the estimate, another value was declared."""
    state = window._project_state()
    state.pop("cast_declared_sizes")
    state["widgets"]["field_cast_overhang"] = 1.4
    window._apply_project_state(state)
    assert "field_cast_overhang" not in window._cast_declared_sizes
    state["widgets"]["field_cast_overhang"] = 0.9
    window._apply_project_state(state)
    assert "field_cast_overhang" in window._cast_declared_sizes


# ── Review I: a project switch never judges the previous project's overrides ─


def _two_projects(window, monkeypatch, b_override=None):
    """@brief Project B (8 pins) saved, project A (40 pins, override on pin 30) in memory."""
    window.group_castellated.setChecked(True)
    window._board_size_known = True
    monkeypatch.setattr(window, "_photo_inventory_family", lambda: True)
    window.combo_mounting.setCurrentIndex(window.combo_mounting.findData("headers"))
    window._apply_extracted_castellated(_sides_extraction([("left", 4, 1), ("right", 4, 5)]))
    window.header_pad_overrides = dict(b_override or {})
    state_b = window._project_state()
    if b_override:
        state_b["host_mounting"]["pad_overrides"] = dict(b_override)
    window._apply_extracted_castellated(_sides_extraction([("left", 20, 1), ("right", 20, 21)]))
    window.header_pad_overrides = {"30": {"shape": "circle", "size": 2.4}}
    window._stale_pad_overrides_reported = ()
    window._draw_preview()
    return state_b


def test_i1_switching_to_another_project_never_reports_the_previous_overrides(window, monkeypatch):
    """@brief Positive: A's override on pin 30 is not reported while project B (8 pins) opens."""
    state_b = _two_projects(window, monkeypatch)
    lines = _logged(window, monkeypatch)
    window._switch_project("PROGETTO_B", state_b)
    assert not any("non applicate" in line for line in lines)
    assert window.header_pad_overrides == {}


def test_i1_a_stale_override_of_the_opened_project_is_still_reported(window, monkeypatch):
    """@brief Negative: B's own override on a pin B does not have is reported once."""
    state_b = _two_projects(window, monkeypatch, b_override={"12": {"shape": "circle", "size": 2.4}})
    lines = _logged(window, monkeypatch)
    window._switch_project("PROGETTO_B", state_b)
    window._draw_preview()
    assert len([line for line in lines if "non applicate" in line and "12" in line]) == 1


def test_i2_the_overhang_reference_is_the_generators(window):
    """@brief The field default and the legacy reference are the generator's REFERENCE_PAD_OVERHANG_MM."""
    from kicadgen.generators.castellated_module import REFERENCE_PAD_OVERHANG_MM
    assert window.CAST_MODULE_SIZE_REFERENCES["field_cast_overhang"] == float(REFERENCE_PAD_OVERHANG_MM)
    assert gui.BoundedValue(gui._reference_pad("pad_overhang"), 0.0, 5.0).value() == float(
        REFERENCE_PAD_OVERHANG_MM)


# ── Final review: the castellated switch and a failing batch ────────────────


def _matrix_with_override(window, monkeypatch, pin):
    """@brief A pin-matrix window with no sides and a host pad override on `pin`."""
    lines = _logged(window, monkeypatch)
    window.group_castellated.setChecked(False)
    window._sides.clear()
    window._board_size_known = True
    window.combo_mounting.setCurrentIndex(window.combo_mounting.findData("headers"))
    monkeypatch.setattr(window, "_photo_inventory_family", lambda: True)
    window.header_pad_overrides = {pin: {"shape": "circle", "size": 2.4}}
    window._stale_pad_overrides_reported = ()
    return lines


def test_final3_an_analysis_switching_castellated_on_never_judges_the_placeholders(window, monkeypatch):
    """@brief Positive: pin 9 exists in the analysed sides, not in the 6 placeholder pins: nothing logged."""
    lines = _matrix_with_override(window, monkeypatch, "9")
    window._apply_extracted_castellated(_sides_extraction([("left", 4, 1), ("right", 6, 5)]))
    assert not any("non applicate" in line for line in lines)


def test_final3_switching_castellated_on_by_hand_still_judges_its_placeholders(window, monkeypatch):
    """@brief Negative: outside a batch the switch refreshes at once on its placeholder pins."""
    lines = _matrix_with_override(window, monkeypatch, "9")
    window.group_castellated.setChecked(True)
    assert len([line for line in lines if "non applicate" in line]) == 1


def test_final6_a_failing_batch_skips_the_deferred_refresh(window, monkeypatch):
    """@brief Positive: the original error propagates and no deferred redraw runs after it."""
    drawn = []
    monkeypatch.setattr(window, "_draw_preview", lambda: drawn.append(1))
    with pytest.raises(RuntimeError, match="rebuild broken"):
        with window._sides_batch():
            window._on_sides_changed()
            raise RuntimeError("rebuild broken")
    assert drawn == [] and window._sides_batch_depth == 0
    window._on_sides_changed()                       # the window is not left batched
    assert drawn == [1]


def test_final6_a_clean_batch_refreshes_once(window, monkeypatch):
    """@brief Negative: without an error the deferred refresh runs once at the end."""
    drawn = []
    monkeypatch.setattr(window, "_draw_preview", lambda: drawn.append(1))
    with window._sides_batch():
        window._on_sides_changed()
        window._on_sides_changed()
    assert drawn == [1]
