"""@brief Fixes of the full code review of 2026-11-11 in the window and the generators (F9-1 F9-3 F2-3 F2-4 F2-5 F3-3 F3-4 F3-6 F3-7).

Each rule with a positive and a negative case that differ for a structural
reason, never for a number:

- F9-1: a host land for a header pin is a new pad -- it carries neither the
  module pad's terminal nor its provenance; the module's own pads, and a
  mechanical hole copied unchanged, keep theirs.
- F9-3: a pad number or a contact label made of non-ASCII digits («²», «①»)
  is text; ASCII integers still form ranges and sort by value.
- F2-3: the «unread pads» block names the pad re-read only where that entry
  is enabled (not in a reopened project), and never offers a re-read for a
  hole the drawing does not print.
- F2-4: every question of a pass ends answered or open; a saved answer
  dropped within the pass has its question asked, and answers of the same
  pass that contradict each other leave the question open and hold back.
- F2-5: a height re-read that brought no height, no question and no conflict
  replaces nothing, with or without a settled result in the session (a
  conflict alone is something read and is recorded beside the stored
  questions, which stay: round 4b and V2-1, tests/test_review_round5_gui.py).
- F3-3: «Cerchio» has one free dimension, like «Quadrato».
- F3-4 / F3-6: a box of the pad dialog is untouched only while it shows what
  it was filled with; a typed number is taken, an untouched one is exact
  (sizes, hole, slot, rotation).
- F3-7: a non-plated pad keeps the «Stimato dal programma…» tooltip line.

Offline: no worker runs a model, every dialog is replaced.
"""
from dataclasses import replace

from PyQt6.QtWidgets import QComboBox, QDialog, QDoubleSpinBox

from kicadgen.core.package_geometry import package_from_mapping
from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.package import ESTIMATED_PADS_NOTE, build_package, estimated_pad_list
from kicadgen.generators.symbol_relay import relay_plan
from kicadgen.gui import main_window as gui
from kicadgen.services import datasheet_extractor
from kicadgen.services import footprint_checks as fc
from tests.test_extraction_questions_gui import _single_part
from tests.test_review_round3_gui import (ESTIMATED, PARTIAL_HEIGHT, VIEW_A, VIEW_B, _dialog_ok, _dropped_for,
                                          _height_question, _pad_question, _reopened, _single_tht_project,
                                          _tht_package, _view)
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction

#: Label of the pad re-read entry, exactly as the tools menu shows it.
REMEDY = "Rileggi solo le posizioni dei pad"
#: Start of the tooltip line that names what the program estimated.
ESTIMATE_LINE = "Stimato dal programma"


# ── F9-1: a host land is not the module's pad ──────────────────────────────

ROUND_LEAD = {"shape": "round", "diameter_mm": 1.2}


def _module(mechanical=False):
    """A two-pad THT module whose pads carry a terminal and estimates; optionally a mechanical hole."""
    pins = [dict(number=str(n), x_mm=x, y_mm=0.0, pad_length_mm=2.0, pad_width_mm=2.0, pad_type="thru_hole",
                 shape="circle", drill_mm=1.5, lead=dict(ROUND_LEAD), sources=dict(ESTIMATED))
            for n, x in ((1, -5.0), (2, 5.0))]
    if mechanical:
        pins.append(dict(number="", x_mm=0.0, y_mm=0.0, pad_length_mm=1.0, pad_width_mm=1.0,
                         pad_type="np_thru_hole", shape="circle", drill_mm=1.0,
                         sources={"drill_mm": "estimated"}))
    return package_from_mapping(dict(name="MOD", family="custom", body_length_mm=14.0, body_width_mm=6.0,
                                     pins=pins))


def test_a_host_land_carries_neither_the_module_terminal_nor_its_provenance():
    module = _module()
    # A header hole SMALLER than the module's terminal: the terminal never
    # enters the host board, so it must not be judged against this hole.
    host = header_footprint_params(module, pad_diameter=1.7, drill=1.0)
    assert [(pin.lead, pin.sources) for pin in host.pins] == [(None, None)] * 2
    footprint = build_package(host)
    assert fc.check_estimated_pads(footprint) == []
    assert ESTIMATED_PADS_NOTE.split("{")[0] not in footprint.description
    assert all(pad.lead is None for pad in footprint.pads)


def test_the_module_pads_and_a_copied_mechanical_hole_keep_theirs():
    module = _module(mechanical=True)
    host = header_footprint_params(module, pad_diameter=1.7, drill=1.0)
    # The module itself is not modified, and still says what was estimated.
    assert all(pin.sources == ESTIMATED and pin.lead == ROUND_LEAD for pin in module.pins[:2])
    assert ESTIMATED_PADS_NOTE.split("{")[0] in build_package(module).description
    # The mechanical hole is the SAME hole on the host board: unchanged, mark included.
    assert host.pins[2] == module.pins[2] and host.pins[2].sources == {"drill_mm": "estimated"}


# ── F9-3: digits `int()` does not read are text ────────────────────────────

def test_non_ascii_digit_pad_numbers_are_listed_as_text():
    assert estimated_pad_list(["1", "²"]) == "1, ²"
    assert estimated_pad_list(["①", "②", "③"]) == "①, ②, ③"
    # A non-ASCII digit between integers breaks the run instead of crashing.
    assert estimated_pad_list(["1", "2", "³", "4"]) == "1, 2, ³, 4"


def test_ascii_integers_still_form_ranges():
    assert estimated_pad_list(["1", "2", "3", "4"]) == "1-4"
    assert estimated_pad_list(["7", "8", "A1"]) == "7, 8, A1"


def _relay_rows(first, second):
    """A relay with two contacts labelled `first` and `second`, in that document order."""
    return [{"pin": "1", "function": "coil"}, {"pin": "2", "function": "coil"},
            {"pin": "3", "function": "common", "contact": first},
            {"pin": "4", "function": "no", "contact": first},
            {"pin": "5", "function": "common", "contact": second},
            {"pin": "6", "function": "no", "contact": second}]


def test_a_non_ascii_digit_contact_label_sorts_as_text():
    plan, reason = relay_plan(_relay_rows("²", "1"))
    assert reason == () and plan is not None
    # The integer label first, the superscript after it as text.
    assert [contact.common for contact in plan.contacts] == ["5", "3"]


def test_ascii_integer_contact_labels_sort_by_value():
    plan, _reason = relay_plan(_relay_rows("10", "9"))
    assert [contact.common for contact in plan.contacts] == ["5", "3"]   # 9 before 10, not «10» < «9»


# ── F2-3: the block names only an enabled remedy ───────────────────────────

def _unread(window, monkeypatch, tmp_path, kind):
    """A single THT project whose analysis of this session left pad 5 unread (`kind`: position or hole)."""
    from kicadgen.gui import workers
    from tests.test_extraction_questions_gui import _FakeReread
    _single_tht_project(window, monkeypatch, tmp_path)
    monkeypatch.setattr(workers, "RereadWorker", _FakeReread, raising=False)
    run_extraction(window, _dropped_for(kind))
    assert window._unread_pads == ["5"]


def test_the_pad_reread_is_named_while_its_entry_is_enabled(window, monkeypatch, tmp_path):  # noqa: F811
    _unread(window, monkeypatch, tmp_path, "position")
    assert window._reread_block_reason("pad_layout") == ""
    assert REMEDY in window._unread_pads_block()
    # Work in progress is a passing condition, not a state of the project:
    # the entry is back as soon as the window is idle, so it is still named.
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    assert window._reread_block_reason("pad_layout") != ""
    assert REMEDY in window._unread_pads_block()


def test_the_pad_reread_is_not_named_in_a_reopened_project(window, monkeypatch, tmp_path):  # noqa: F811
    _unread(window, monkeypatch, tmp_path, "position")
    state = window._project_state()
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    # The same pads, the same block; only the session's settled result is gone.
    assert window._unread_pads == ["5"] and window._reread_block_reason("pad_layout") != ""
    block = window._unread_pads_block()
    assert "le posizioni dei pad 5" in block and REMEDY not in block
    assert "Ripeti l'analisi con un disegno quotato dei pad" in block


def test_an_unread_hole_is_never_sent_to_a_reread(window, monkeypatch, tmp_path):  # noqa: F811
    _unread(window, monkeypatch, tmp_path, "hole")
    # The re-read IS enabled here: it is left out because it cannot read a
    # hole the drawing does not print, not because the entry is greyed out.
    assert window._reread_block_reason("pad_layout") == ""
    block = window._unread_pads_block()
    assert "i fori dei pad 5" in block and REMEDY not in block
    assert "un documento che quoti i fori o i terminali" in block


# ── F2-4: a question is answered or open, never neither ────────────────────

def _review(window, monkeypatch, tmp_path, saved, questions, replies):
    """Run the question review; returns (held_back, keys asked, answers handed to the extractor)."""
    prepare(window, monkeypatch, tmp_path, "HDR")
    received, asked = [], []
    monkeypatch.setattr(datasheet_extractor, "apply_answers",
                        lambda params, answers: received.append(dict(answers)) or params, raising=False)

    def ask(self, question):
        asked.append(question["key"])
        return replies[question["key"]]
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", ask)
    window._extraction_answers = dict(saved)
    params = header_extraction()
    params.questions = list(questions)
    _params, held_back = window._review_extraction_questions(params)
    return held_back, asked, received


def test_answers_of_one_pass_that_contradict_leave_the_question_open(window, monkeypatch, tmp_path):  # noqa: F811
    # VIEW_A saved «top»; VIEW_B answered «bottom» drops it; VIEW_A, asked in
    # turn, is answered «top» again, which drops the fresh VIEW_B.
    held_back, asked, received = _review(
        window, monkeypatch, tmp_path, {VIEW_A: "top"}, [_view(VIEW_A), _view(VIEW_B)],
        {VIEW_A: "top", VIEW_B: "bottom"})
    assert asked == [VIEW_B, VIEW_A]                 # each asked once, never in a loop
    assert held_back is True
    assert window._extraction_questions == [_view(VIEW_B)]
    assert received == [{VIEW_A: "top"}] and window._extraction_answers == {VIEW_A: "top"}
    # Every question of the pass is accounted for.
    settled = set(received[0]) | {q["key"] for q in window._extraction_questions}
    assert settled == {VIEW_A, VIEW_B}


def test_a_dropped_saved_answer_is_asked_and_agreeing_answers_close_the_pass(window, monkeypatch, tmp_path):  # noqa: F811
    held_back, asked, received = _review(
        window, monkeypatch, tmp_path, {VIEW_A: "top"}, [_view(VIEW_A), _view(VIEW_B)],
        {VIEW_A: "bottom", VIEW_B: "bottom"})
    assert asked == [VIEW_B, VIEW_A] and held_back is False
    assert window._extraction_questions == []
    assert received == [{VIEW_A: "bottom", VIEW_B: "bottom"}]


def test_the_same_question_listed_twice_is_asked_once_and_stays_answered(window, monkeypatch, tmp_path):  # noqa: F811
    # Asked in this pass and still saved: the «asked once» rule must not
    # take the second entry for a contradiction.
    held_back, asked, received = _review(
        window, monkeypatch, tmp_path, {}, [_view(VIEW_B), _view(VIEW_B)], {VIEW_B: "top"})
    assert asked == [VIEW_B] and held_back is False and window._extraction_questions == []
    assert received == [{VIEW_B: "top"}]


# ── F2-5: a height re-read that read nothing replaces nothing ──────────────

def _nothing_read(**overrides):
    """A height re-read result with no height and no question."""
    return header_extraction("RELAY", cols=2, body_height_mm=None,
                             partial_reread=dict(PARTIAL_HEIGHT), **overrides)


def test_a_height_reread_that_read_nothing_keeps_the_stored_height_question(window, monkeypatch, tmp_path):  # noqa: F811
    before = _reopened(window, monkeypatch, tmp_path)
    stored = [_pad_question(), _height_question()]
    window._extraction_questions = list(stored)
    window._on_reread_done(_nothing_read())
    assert window._extraction_questions == stored
    assert window._stored_body_height_question() == _height_question()
    assert window.field_body_height.value() == before
    assert "nessuna quota letta" in window._log_plain_text()
    # CONTRACT CHANGED 2026-11-11 (review V2-1), for the second time today.
    # Round 4b made a conflict alone «something read», and with that REPLACED
    # the scope's stored question (`== [_pad_question()]` was asserted
    # here): the printed candidates 7.5 / 9 were gone and «Rigenera modello
    # STEP» asked nothing, so a paid re-read destroyed the user's only free
    # way to choose a height. The scope is replaced only by what can stand
    # in its place (a height, a scope question): a conflict alone is
    # recorded BESIDE the stored questions, which all stay
    # (tests/test_review_round5_gui.py holds the full contract).
    window._on_reread_done(_nothing_read(body_conflict="altezza: le due letture non coincidono."))
    assert window._extraction_questions == stored
    assert window._stored_body_height_question() == _height_question()
    assert window.field_body_height.value() == before
    assert "non coincidono" in window._log_plain_text()


def test_a_height_reread_that_read_a_height_replaces_the_stored_height_question(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened(window, monkeypatch, tmp_path)
    window._extraction_questions = [_pad_question(), _height_question()]
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=7.5,
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    # The height is now read: its old question is settled, the pad one stays.
    assert window._extraction_questions == [_pad_question()]
    assert window.field_body_height.value() == 7.5


def test_with_a_settled_result_a_reread_that_read_nothing_is_not_merged(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    window._extraction_questions = [_height_question()]
    applied = []
    monkeypatch.setattr(window, "_on_extracted", lambda params: applied.append(params))
    window._on_reread_done(_nothing_read())
    assert applied == []
    assert window._last_extracted.body_height_mm == 3
    assert window._extraction_questions == [_height_question()]
    # Negative: a re-read that read a height IS merged and applied.
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=7.5,
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert [params.body_height_mm for params in applied] == [7.5]


# ── F3-3: a circle has one free dimension ──────────────────────────────────

def _dialog(window, monkeypatch, act):
    """Open the pad dialog of pad 0, run `act(dialog, boxes, shape_combo)` and press OK."""
    def run(dialog):
        # Combos in form order: pad type, shape. Boxes: width, height, rotation, hole, slot, X, Y.
        act(dialog, dialog.findChildren(QDoubleSpinBox), dialog.findChildren(QComboBox)[1])
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(window, "_run_modal", run)
    window._edit_package_pad(0)


def test_a_circle_edited_on_its_width_stays_a_circle(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    seen = {}

    def widen(_dialog_, boxes, _shape):
        seen["enabled_before"] = boxes[1].isEnabled()
        boxes[0].setValue(2.0)
        seen["height"] = boxes[1].value()
    _dialog(window, monkeypatch, widen)
    after = window.package_geometry.pins[0]
    assert seen == {"enabled_before": False, "height": 2.0}
    assert (after.shape, after.pad_length_mm, after.pad_width_mm) == ("circle", 2.0, 2.0)
    # The size was typed; the shape was not chosen again, so its mark stays.
    assert after.sources["pad_size"] == "read" and after.sources["shape"] == "estimated"


def test_an_oval_chosen_by_the_user_has_two_free_dimensions(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    seen = {}

    def make_oval(_dialog_, boxes, shape):
        shape.setCurrentIndex(shape.findData("oval"))
        seen["enabled"] = boxes[1].isEnabled()
        boxes[0].setValue(2.4)
        boxes[1].setValue(1.6)
    _dialog(window, monkeypatch, make_oval)
    after = window.package_geometry.pins[0]
    assert seen == {"enabled": True}
    assert (after.shape, after.pad_length_mm, after.pad_width_mm) == ("oval", 2.4, 1.6)
    assert after.sources["shape"] == "read"          # chosen by the user this time


# ── F3-4: a typed number is taken, an untouched box is exact ───────────────

def test_a_number_typed_next_to_the_shown_one_is_taken(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    typed = {}

    def retype(boxes):
        # 1.5875 is shown 1.587 or 1.588: the user types the other one.
        typed["size"] = 1.588 if boxes[0].value() < 1.5875 else 1.587
        boxes[0].setValue(typed["size"])
        # 0.8375 is shown 0.837 or 0.838: likewise for the hole.
        typed["hole"] = 0.838 if boxes[3].value() < 0.8375 else 0.837
        boxes[3].setValue(typed["hole"])
    _dialog_ok(window, monkeypatch, retype)
    after = window.package_geometry.pins[0]
    assert (after.pad_length_mm, after.pad_width_mm, after.drill_mm) == (typed["size"],) * 2 + (typed["hole"],)
    assert after.sources["pad_size"] == "read" and after.sources["drill_mm"] == "read"


def test_a_box_set_back_to_what_it_showed_keeps_the_exact_value(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)

    def touch_and_restore(boxes):
        for index in (0, 3):
            shown = boxes[index].value()
            boxes[index].setValue(shown + 0.5)
            boxes[index].setValue(shown)
    _dialog_ok(window, monkeypatch, touch_and_restore)
    after = window.package_geometry.pins[0]
    assert (after.pad_length_mm, after.pad_width_mm, after.drill_mm) == (1.5875, 1.5875, 0.8375)
    assert after.sources == ESTIMATED


# ── F3-6: rotation and slot follow the same rule ───────────────────────────

def _with_pad(window, **changes):
    """Replace fields of pad 0 through the draft and apply it; returns the pad."""
    pins = list(window.package_geometry.pins)
    pins[0] = replace(pins[0], **changes)
    window._edit_footprint_draft({"package_pins": pins})
    window.apply_footprint_draft()
    assert not window.has_pending_footprint_edits()
    return window.package_geometry.pins[0]


def test_an_untouched_fractional_rotation_and_fine_slot_are_kept_exactly(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    # An angle finer than the box's one decimal and a slot finer than its three.
    before = _with_pad(window, shape="oval", pad_length_mm=3.0, pad_width_mm=2.5, rotation_deg=37.25,
                       drill_mm=1.0004, drill_length_mm=1.0)
    _dialog_ok(window, monkeypatch)
    assert window.package_geometry.pins[0] == before
    assert not window.has_pending_footprint_edits()


def test_a_typed_rotation_and_a_slot_typed_equal_to_the_hole_are_taken(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    _with_pad(window, shape="oval", pad_length_mm=3.0, pad_width_mm=2.5, rotation_deg=37.25,
              drill_mm=1.0004, drill_length_mm=1.0)

    def edit(boxes):
        boxes[2].setValue(90.0)
        # Both hole sides typed to the same number: a round hole.
        boxes[3].setValue(1.1)
        boxes[4].setValue(1.1)
    _dialog_ok(window, monkeypatch, edit)
    after = window.package_geometry.pins[0]
    assert after.rotation_deg == 90.0
    assert after.drill_length_mm is None and after.drill_mm == 1.1


# ── F3-7: a non-plated pad says what was estimated ─────────────────────────

def _npth_tooltips(window, monkeypatch):
    """Turn pad 0 into a non-plated hole through the dialog; the tooltips of the preview that name it."""
    def make_npth(dialog, _boxes, _shape):
        pad_type = dialog.findChildren(QComboBox)[0]
        pad_type.setCurrentIndex(pad_type.findData("np_thru_hole"))
    _dialog(window, monkeypatch, make_npth)
    assert window.package_geometry.pins[0].pad_type == "np_thru_hole"
    return {item.toolTip() for item in window.preview_scene.items()
            if "Foro non metallizzato" in item.toolTip()}


def test_a_non_plated_pad_with_estimates_keeps_the_estimate_line(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path)
    tips = _npth_tooltips(window, monkeypatch)
    assert tips and all(ESTIMATE_LINE in tip and "foro" in tip for tip in tips)


def test_a_non_plated_pad_without_estimates_has_no_estimate_line(window, monkeypatch, tmp_path):  # noqa: F811
    _tht_package(window, monkeypatch, tmp_path, sources=None)
    tips = _npth_tooltips(window, monkeypatch)
    assert tips and not any(ESTIMATE_LINE in tip for tip in tips)
