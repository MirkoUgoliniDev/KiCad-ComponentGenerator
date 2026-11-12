"""@brief Round 5 of the 2026-11-11 review in the window: findings V2-1 .. V2-4 of the independent verifier.

Each rule with a positive and a negative case that differ for a structural
reason, never for a number:

- V2-1: the height scope of a re-read (height, conflict, height questions) is
  replaced only by something that can stand in its place -- a height or a
  question of the scope. A re-read that brought ONLY a conflict removes
  neither the stored height questions nor a declared height: the user can
  still choose («Rigenera modello STEP» asks the stored question) or the
  declared height is still in force. The conflict is reported and, while the
  height is missing, words the STEP deferral.
- V2-2: the «unread pads» sentence of the log is evaluated on the result
  being applied, so the log, the box and the gate name the same remedy from
  the first analysis of a session on.
- V2-3: one question per key; the same key listed twice is one question,
  also after «Torna indietro» on it.
- V2-4: in the pad dialog the last free height comes back when the shape
  stops being a one-size one; a one-size shape writes the exact width.

Offline: no worker runs a model, every dialog is replaced.
"""
import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import main_window as gui
from tests.test_extraction_questions_gui import _single_part
from tests.test_review_round3_gui import (ESTIMATED, PARTIAL_HEIGHT, VIEW_A, VIEW_B, _dropped_for, _height_question,
                                          _pad_question, _question, _reopened, _single_tht_project, _tht_package,
                                          _view)
from tests.test_review_round4_gui import REMEDY, _dialog, _review, _with_pad
from tests.ui_flow_helpers import header_extraction, run_extraction

#: A height conflict as the extractor words one.
CONFLICT = "altezza: le due letture non coincidono."
#: Start of the «unread pads» sentence, in the log and in the box.
BLOCK_START = "Nessun footprint"


# ── V2-1: a conflict alone replaces neither a question nor a height ────────

def _reread(**values):
    """A partial height re-read result; `values` override the fields (no height by default)."""
    return header_extraction("RELAY", cols=2, partial_reread=dict(PARTIAL_HEIGHT),
                             **dict({"body_height_mm": None}, **values))


def _conflict_only():
    """Two quotes that disagree: no height, no question."""
    return _reread(body_conflict=CONFLICT)


def _other_height_question():
    """A height question the re-read itself asks, with other printed candidates."""
    return _question("body_height_mm:8|9", "body_height_mm", (8.0, 9.0))


def _session(window, monkeypatch, tmp_path, kind, declared):
    """A single part analysed in this session ("settled") or reopened; its height declared or not."""
    if kind == "settled":
        _single_part(window, monkeypatch, tmp_path)
        monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
        if not declared:
            window._last_extracted.body_height_mm = None
    else:
        _reopened(window, monkeypatch, tmp_path)
    window.group_3d.setChecked(True)
    window._body_height_declared = declared
    window._step_deferral = None
    assert ("body_height_mm" in window._missing_body_quotes()) is (not declared)


def _regeneration_asks(window, monkeypatch):
    """The questions «Rigenera modello STEP» asks now (each answered «Torna indietro»)."""
    asked = []
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, question: asked.append(question["key"]) or gui.QUESTION_BACK)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    window._regenerate_3d()
    return asked


@pytest.mark.parametrize("kind", ["settled", "reopened"])
def test_a_conflict_alone_keeps_the_stored_height_question_and_the_way_to_choose(window, monkeypatch, tmp_path,  # noqa: F811
                                                                                 kind):
    _session(window, monkeypatch, tmp_path, kind, declared=False)
    stored = [_pad_question(), _height_question()]
    window._extraction_questions = list(stored)
    window._on_reread_done(_conflict_only())
    assert window._extraction_questions == stored
    assert CONFLICT in window._log_plain_text()
    # The conflict words the deferral; it is saved with the project beside the question.
    assert window._step_deferral_reason() == ("conflict", CONFLICT)
    saved = window._project_state()
    assert saved["step_deferral"]["code"] == "conflict"
    assert [question["key"] for question in saved["extraction_questions"]] == [q["key"] for q in stored]
    # The free way to choose is still there: the stored candidates are asked.
    assert _regeneration_asks(window, monkeypatch) == [_height_question()["key"]]


@pytest.mark.parametrize("kind", ["settled", "reopened"])
def test_a_reread_that_brings_its_own_height_question_replaces_the_stored_one(window, monkeypatch, tmp_path,  # noqa: F811
                                                                              kind):
    # Negative of the test above: the same conflict WITH a question of the
    # scope has something to stand in place of the stored question.
    _session(window, monkeypatch, tmp_path, kind, declared=False)
    window._extraction_questions = [_pad_question(), _height_question()]
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", lambda self, question: gui.QUESTION_BACK)
    result = _conflict_only()
    result.questions = [_other_height_question()]
    window._on_reread_done(result)
    keys = [question["key"] for question in window._extraction_questions]
    assert _other_height_question()["key"] in keys and _height_question()["key"] not in keys


@pytest.mark.parametrize("kind", ["settled", "reopened"])
def test_a_conflict_alone_leaves_a_declared_height_in_force(window, monkeypatch, tmp_path, kind):  # noqa: F811
    _session(window, monkeypatch, tmp_path, kind, declared=True)
    before = window.field_body_height.value()
    applied = []
    monkeypatch.setattr(window, "_on_extracted", applied.append)
    window._on_reread_done(_conflict_only())
    assert applied == []
    assert window._body_height_declared is True and window.field_body_height.value() == before
    if kind == "settled":
        assert window._last_extracted.body_height_mm == before
    # A body can be built: nothing is deferred, the conflict is only reported.
    assert window._step_deferral is None and window._step_deferral_reason() == ("", "")
    assert CONFLICT in window._log_plain_text()


@pytest.mark.parametrize("kind", ["settled", "reopened"])
def test_a_reread_that_brings_a_height_replaces_the_declared_one(window, monkeypatch, tmp_path, kind):  # noqa: F811
    # Negative of the test above: a height can stand in place of a height.
    _session(window, monkeypatch, tmp_path, kind, declared=True)
    window._extraction_questions = [_height_question()]
    applied = []
    if kind == "settled":
        monkeypatch.setattr(window, "_on_extracted", applied.append)
    window._on_reread_done(_reread(body_height_mm=7.5))
    if kind == "settled":
        # Merged into the settled result and handed to the normal apply.
        assert [params.body_height_mm for params in applied] == [7.5]
        assert not [q for q in applied[0].questions or () if q.get("field") == "body_height_mm"]
    else:
        assert window.field_body_height.value() == 7.5 and window._extraction_questions == []


@pytest.mark.parametrize("kind", ["settled", "reopened"])
def test_without_a_height_and_without_a_question_the_conflict_is_the_deferral_reason(window, monkeypatch,  # noqa: F811
                                                                                     tmp_path, kind):
    _session(window, monkeypatch, tmp_path, kind, declared=False)
    window._extraction_questions = [_pad_question()]
    assert window._step_deferral_reason()[0] == "missing"
    window._on_reread_done(_conflict_only())
    # Nothing to choose from: the STEP tab says why, instead of «the quote is missing».
    assert window._step_deferral_reason() == ("conflict", CONFLICT)
    assert CONFLICT in window._missing_body_reason()
    assert window._extraction_questions == [_pad_question()]
    # Negative, same state: a re-read that read nothing says nothing about a conflict.
    window._step_deferral = None
    window._on_reread_done(_reread())
    assert window._step_deferral is None and window._step_deferral_reason()[0] == "missing"


def test_what_a_conflict_only_reread_is(window):  # noqa: F811
    assert window._reread_height_conflict_only(_conflict_only()) is True
    # Each of the three things that make it something else.
    assert window._reread_height_conflict_only(_reread()) is False
    assert window._reread_height_conflict_only(_reread(body_conflict=CONFLICT, body_height_mm=7.5)) is False
    with_question = _conflict_only()
    with_question.questions = [_other_height_question()]
    assert window._reread_height_conflict_only(with_question) is False
    # A question outside the height scope does not stand in place of a height.
    with_pad_question = _conflict_only()
    with_pad_question.questions = [_pad_question()]
    assert window._reread_height_conflict_only(with_pad_question) is True
    # A conflict that is not a sentence is no conflict.
    for malformed in ("  ", True, 5, ["x"]):
        assert window._reread_height_conflict_only(_reread(body_conflict=malformed)) is False


# ── V2-2: the log names the remedy the box names ───────────────────────────

def _first_analysis(window, monkeypatch, tmp_path, params):
    """Apply `params` as the FIRST analysis of the session; returns (log lines of the block, box texts)."""
    from kicadgen.gui import workers
    from tests.test_extraction_questions_gui import _FakeReread
    _single_tht_project(window, monkeypatch, tmp_path)
    monkeypatch.setattr(workers, "RereadWorker", _FakeReread, raising=False)
    assert window._last_extracted is None
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, _title, text, *a, **k: boxes.append(text))
    run_extraction(window, params)
    assert window._unread_pads == ["5"]
    lines = [line for line in window._log_plain_text().splitlines() if BLOCK_START in line]
    return lines, [text for text in boxes if BLOCK_START in text]


def test_the_first_analysis_logs_the_pad_reread_the_box_names(window, monkeypatch, tmp_path):  # noqa: F811
    lines, boxes = _first_analysis(window, monkeypatch, tmp_path, _dropped_for("position"))
    block = window._unread_pads_block()
    assert REMEDY in block and window._reread_block_reason("pad_layout") == ""
    # One state, one sentence: every line of the log and every box carry the block as it is now.
    assert lines and boxes
    assert all(block in line for line in lines) and all(block in text for text in boxes)


def test_the_first_analysis_does_not_log_a_reread_that_is_not_available(window, monkeypatch, tmp_path):  # noqa: F811
    # Negative: the same unread pad in a result without a main answer to
    # re-read. The remedy is judged on the result applied, not assumed.
    params = _dropped_for("position")
    params.raw_main = None
    lines, boxes = _first_analysis(window, monkeypatch, tmp_path, params)
    block = window._unread_pads_block()
    assert REMEDY not in block and window._reread_block_reason("pad_layout") != ""
    assert lines and boxes
    assert all(block in line for line in lines) and all(block in text for text in boxes)
    assert not any(REMEDY in line for line in lines)


# ── V2-3: one question per key ─────────────────────────────────────────────

def test_the_same_key_listed_twice_is_asked_once_after_a_back(window, monkeypatch, tmp_path):  # noqa: F811
    held_back, asked, received = _review(
        window, monkeypatch, tmp_path, {}, [_view(VIEW_B), _view(VIEW_B), _pad_question()],
        {VIEW_B: gui.QUESTION_BACK, _pad_question()["key"]: "main"})
    assert asked == [VIEW_B, _pad_question()["key"]] and held_back is True
    assert window._extraction_questions == [_view(VIEW_B)]
    # Open and applied are disjoint: the key left open was not answered.
    assert received == [{_pad_question()["key"]: "main"}] and VIEW_B not in window._extraction_answers


def test_two_different_keys_are_two_questions_after_a_back(window, monkeypatch, tmp_path):  # noqa: F811
    # Negative: the same field and the same text pattern, another key.
    held_back, asked, received = _review(
        window, monkeypatch, tmp_path, {}, [_view(VIEW_A), _view(VIEW_B)],
        {VIEW_A: gui.QUESTION_BACK, VIEW_B: "top"})
    assert asked == [VIEW_A, VIEW_B] and held_back is True
    assert window._extraction_questions == [_view(VIEW_A)] and received == [{VIEW_B: "top"}]


def test_of_two_entries_with_one_key_the_first_well_formed_one_is_kept():
    malformed = dict(_view(VIEW_A), options=[])
    first = dict(_view(VIEW_A), text="Prima?")
    second = dict(_view(VIEW_A), text="Seconda?")
    kept = gui.MainWindow._valid_extraction_questions([malformed, first, second, _view(VIEW_B)])
    assert [(question["key"], question["text"]) for question in kept] == [
        (VIEW_A, "Prima?"), (VIEW_B, _view(VIEW_B)["text"])]


# ── V2-4: the free height comes back, a one-size shape writes the exact width ──

def _oval(window, monkeypatch, tmp_path):
    """Pad 0 as an oval whose two sides are finer than the dialog's three decimals."""
    _tht_package(window, monkeypatch, tmp_path)
    return _with_pad(window, shape="oval", pad_length_mm=1.5875, pad_width_mm=2.5125)


def _choose(shape, *keys):
    for key in keys:
        shape.setCurrentIndex(shape.findData(key))


@pytest.mark.parametrize("one_size", ["circle", "square"])
def test_a_one_size_shape_looked_at_and_left_gives_back_the_exact_height(window, monkeypatch, tmp_path, one_size):  # noqa: F811
    before = _oval(window, monkeypatch, tmp_path)
    seen = {}

    def look(_dialog_, boxes, shape):
        _choose(shape, one_size)
        seen["linked"] = (boxes[1].isEnabled(), boxes[1].value() == boxes[0].value())
        _choose(shape, "oval")
        seen["free"] = boxes[1].isEnabled()
    _dialog(window, monkeypatch, look)
    assert seen == {"linked": (False, True), "free": True}
    # Nothing was typed: the pad is the pad it was, estimate marks included.
    assert window.package_geometry.pins[0] == before
    assert not window.has_pending_footprint_edits()


def test_a_typed_free_height_comes_back_and_is_taken(window, monkeypatch, tmp_path):  # noqa: F811
    # Negative of the test above: the height that returns is the LAST free
    # one, typed by the user this time, so it is written as typed.
    _oval(window, monkeypatch, tmp_path)

    def retype(_dialog_, boxes, shape):
        boxes[1].setValue(3.0)
        _choose(shape, "circle", "rect")
    _dialog(window, monkeypatch, retype)
    after = window.package_geometry.pins[0]
    assert (after.shape, after.pad_length_mm, after.pad_width_mm) == ("rect", 1.5875, 3.0)


def test_a_one_size_shape_left_chosen_writes_the_exact_width_on_both_sides(window, monkeypatch, tmp_path):  # noqa: F811
    _oval(window, monkeypatch, tmp_path)
    _dialog(window, monkeypatch, lambda _dialog_, _boxes, shape: _choose(shape, "circle"))
    after = window.package_geometry.pins[0]
    # The exact 1.5875 of the untouched width, not the 1.587 / 1.588 the boxes show.
    assert (after.shape, after.pad_length_mm, after.pad_width_mm) == ("circle", 1.5875, 1.5875)


def test_a_pad_opened_as_a_circle_and_made_an_oval_keeps_its_exact_side(window, monkeypatch, tmp_path):  # noqa: F811
    # The dialog opens already linked: the free height is the pad's own.
    _tht_package(window, monkeypatch, tmp_path)
    _dialog(window, monkeypatch, lambda _dialog_, _boxes, shape: _choose(shape, "oval", "circle", "oval"))
    after = window.package_geometry.pins[0]
    assert (after.shape, after.pad_length_mm, after.pad_width_mm) == ("oval", 1.5875, 1.5875)
    # Only the shape was chosen: the size keeps its estimate mark.
    assert after.sources["shape"] == "read" and after.sources["pad_size"] == ESTIMATED["pad_size"]
