"""@brief Round 4b of the 2026-11-11 review: unfit pads named truthfully, a height conflict kept, ASCII-only integers.

Each rule with a positive and a negative case that differ for a structural
reason, never for a number:

- Third wording: the extractor returns as unread geometry also the pads it
  read in full but whose values the pad rules cannot settle (a land that
  cannot hold its hole, overlapping copper). The window names them for that,
  from the dropped geometry itself (`unsettled_pads`, and «placed, with its
  hole, yet dropped»), never as «positions not read» or «holes not read»;
  pads that really lack a position or a hole keep their own wording.
- Height re-read: two quotes that disagree are something the re-read found.
  The conflict is reported and recorded beside what the project holds (it
  defers the STEP model only while the height is missing) and replaces
  neither a height nor a stored question (review V2-1: round 4b merged it
  and lost both); a re-read with no height, no question and no conflict
  still replaces nothing and reports nothing.
- F9-3 class: a number made of non-ASCII digits («²», «①») is text at every
  site that converts with `int()`; ASCII integers keep their numeric order.

Offline: the extraction results come from the real extractor validation on
synthetic answers, no worker runs a model, every dialog is replaced.
"""
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.core import package_geometry
from kicadgen.gui import main_window as gui
from kicadgen.gui.pinout_dialog import pin_sort_key
from kicadgen.services import datasheet_extractor
from kicadgen.services.datasheet_extractor import _params_from_tool_arguments, single_answer_to_data
from tests.test_extraction_questions_gui import _single_part
from tests.test_review_round3_gui import (PARTIAL_HEIGHT, _dropped_for, _height_question, _pad_question, _reopened,
                                          _single_tht_project)
from tests.test_unread_pad_positions import POSITIONS, _answer, _pads
from tests.ui_flow_helpers import header_extraction, run_extraction

#: Label of the pad re-read entry, exactly as the tools menu shows it.
REMEDY = "Rileggi solo le posizioni dei pad"
#: What only the third wording says.
UNFIT = "non possono essere tutti veri"
#: Start of the line that carries the extractor's own account.
DETAIL = "Dettaglio dell'analisi:"
#: A height conflict as the extractor words one.
CONFLICT = "altezza: le due letture non coincidono."


# ── Third wording: pads read in full that the pad rules cannot settle ──────

def _extracted(pads):
    """The real extractor's result for a single-part answer with `pads`; its pads must have been dropped."""
    params = _params_from_tool_arguments(single_answer_to_data(_answer(pads)))
    assert params.footprint_pads_dropped() and params.package_geometry is None
    # What every real analysis keeps: the main answer a re-read starts from.
    params.raw_main = {"name": "RLY", "_candidates": {}}
    return params


def _small_lands(unread=()):
    """Pads whose READ land (1.2 mm) cannot hold the hole their READ terminal (1.0 mm) needs."""
    pads = _pads(unread=unread)
    for pad in pads:
        del pad["drill_mm"]
        pad.update(pad_length_mm=1.2, pad_width_mm=1.2, lead={"shape": "round", "diameter_mm": 1.0},
                   value_sources={"lead": "read", "pad_size": "read"})
    return pads


def _overlapping():
    """Well-formed SMD lands (no hole to judge), each fit alone, with pad 5 drawn on top of pad 1."""
    pads = _pads()
    for pad in pads:
        del pad["drill_mm"]
        pad["pad_type"] = "smd"
        if pad["number"] == "5":
            pad.update(x_mm=0.5, y_mm=0.0)
    return pads


def _analysed(window, monkeypatch, tmp_path, params):
    """Apply `params` in a fresh single THT project; returns the warnings shown as (title, text)."""
    from kicadgen.gui import workers
    from tests.test_extraction_questions_gui import _FakeReread
    _single_tht_project(window, monkeypatch, tmp_path)
    monkeypatch.setattr(workers, "RereadWorker", _FakeReread, raising=False)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, text, *a, **k: warnings.append((title, text)))
    run_extraction(window, params)
    return warnings


def test_the_extractor_marks_small_lands_as_unsettled_and_overlaps_not():
    """The data the window reads: `unsettled_pads` for the first kind only, every pad placed in both."""
    small, overlap = _extracted(_small_lands()), _extracted(_overlapping())
    assert small.unread_pad_geometry["unsettled_pads"] == list(POSITIONS)
    assert "unsettled_pads" not in overlap.unread_pad_geometry
    for params in (small, overlap):
        assert all(pin["x_mm"] is not None and pin["y_mm"] is not None
                   for pin in params.unread_pad_geometry["pins"])


@pytest.mark.parametrize("pads", [_small_lands, _overlapping], ids=["land cannot hold its hole", "copper overlaps"])
def test_pads_the_rules_cannot_settle_are_named_for_that(window, monkeypatch, tmp_path, pads):  # noqa: F811
    params = _extracted(pads())
    warnings = _analysed(window, monkeypatch, tmp_path, params)
    named = ", ".join(params.unread_pads)
    assert window._unread_pads == params.unread_pads and window._unread_pad_unfit == params.unread_pads
    assert window._unread_pad_holes is None
    block = window._unread_pads_block()
    assert f"i valori letti per i pad {named} {UNFIT}" in block
    # Neither of the two other reasons, and no re-read of positions that were read.
    assert "non sono state lette" not in block and "non sono stati letti" not in block
    assert REMEDY not in block and window._reread_block_reason("pad_layout") == ""
    assert "land pattern PCB" in block
    # The extractor's own account, with the values in conflict, while the session holds it.
    assert f"{DETAIL} {params.pin_map_conflict.strip()}" in block
    # The same reason in the log, in the box of the automatic generation and in the manual gate.
    assert block in window._log_plain_text()
    blocked = [text for title, text in warnings if title == "Generazione automatica non avviata"]
    assert blocked and UNFIT in blocked[-1] and "non sono state lette" not in blocked[-1]
    with pytest.raises(ValueError, match=UNFIT):
        window._current_params()


def test_the_unfit_pads_are_saved_and_a_reopened_project_says_the_same_without_the_account(window, monkeypatch, tmp_path):  # noqa: F811
    params = _extracted(_small_lands())
    _analysed(window, monkeypatch, tmp_path, params)
    state = window._project_state()
    assert state["unread_pad_unfit"] == params.unread_pads and "unread_pad_holes" not in state
    window._switch_project("OTHER", window._general_project_state)
    assert window._unread_pad_unfit is None and window._unread_pads_block() == ""
    window._switch_project("RLY", state)
    assert window._unread_pad_unfit == params.unread_pads
    block = window._unread_pads_block()
    # The pads and the reason are data and come back; the extractor's prose is session text.
    assert f"i valori letti per i pad {', '.join(params.unread_pads)} {UNFIT}" in block
    assert DETAIL not in block and "non sono state lette" not in block
    # The re-read entry is disabled here, and was never named for these pads.
    assert window._reread_block_reason("pad_layout") != "" and REMEDY not in block


@pytest.mark.parametrize("kind, own, other", [
    ("position", "le posizioni dei pad 5 non sono state lette", "i fori dei pad"),
    ("hole", "i fori dei pad 5 non sono stati letti", "le posizioni dei pad")])
def test_pads_that_lack_a_position_or_a_hole_keep_their_own_wording(window, monkeypatch, tmp_path,  # noqa: F811
                                                                    kind, own, other):
    _analysed(window, monkeypatch, tmp_path, _dropped_for(kind))
    assert window._unread_pads == ["5"] and window._unread_pad_unfit is None
    block = window._unread_pads_block()
    assert own in block and other not in block
    assert UNFIT not in block and DETAIL not in block
    assert "unread_pad_unfit" not in window._project_state()


def test_an_unread_position_beside_unfit_pads_gets_each_its_own_sentence(window, monkeypatch, tmp_path):  # noqa: F811
    params = _extracted(_small_lands(unread=("5",)))
    _analysed(window, monkeypatch, tmp_path, params)
    # Pad 5 has no position: that is what any remedy must read first, whatever its land.
    assert window._unread_pad_unfit == ["1", "2", "3", "4"]
    block = window._unread_pads_block()
    assert "le posizioni dei pad 5 non sono state lette" in block
    assert f"i valori letti per i pad 1, 2, 3, 4 {UNFIT}" in block
    # The re-read is named once, in the sentence of the unread position.
    assert block.count(REMEDY) == 1 and block.index(REMEDY) < block.index(UNFIT)


def _pin(number, **values):
    """One dropped pad as the extractor keeps it: placed, holed and with its hole unless overridden."""
    return dict(dict(number=number, x_mm=0.0, y_mm=0.0, pad_type="thru_hole", drill_mm=1.0), **values)


@pytest.mark.parametrize("geometry, unread, expected", [
    # Placed and with its hole, yet dropped: the third reason, with or without the extractor's list.
    ({"pins": [_pin("1"), _pin("2")]}, ["1", "2"], ["1", "2"]),
    ({"pins": [_pin("1"), _pin("2")], "unsettled_pads": ["2"]}, ["1", "2"], ["1", "2"]),
    # An SMD pad needs no hole: without `drill_mm` it is still complete.
    ({"pins": [_pin("1", pad_type="smd", drill_mm=None)]}, ["1"], ["1"]),
    # A missing position or hole ranks first, even for a pad the extractor lists as unsettled.
    ({"pins": [_pin("1", x_mm=None), _pin("2", drill_mm=None)], "unsettled_pads": ["1", "2"]}, ["1", "2"], None),
    # Only pads the extraction dropped; a duplicate entry lacking a value decides.
    ({"pins": [_pin("1"), _pin("9")]}, ["1"], ["1"]),
    ({"pins": [_pin("1"), _pin("1", y_mm=None)]}, ["1"], None),
    # Malformed or absent geometry names nothing.
    ({"pins": "1, 2", "unsettled_pads": ["1"]}, ["1"], None),
    (None, ["1"], None),
    ({"pins": [_pin("1")]}, None, None),
])
def test_the_unfit_pads_are_read_from_the_dropped_geometry(geometry, unread, expected):
    params = SimpleNamespace(unread_pad_geometry=geometry, pin_map_conflict="testo che non viene letto")
    assert gui.MainWindow._pads_with_unfit_values(params, unread) == expected


def test_a_hand_edited_project_cannot_name_other_pads_nor_one_pad_for_two_reasons(window, monkeypatch, tmp_path):  # noqa: F811
    _analysed(window, monkeypatch, tmp_path, _extracted(_small_lands()))
    state = window._project_state()
    state["unread_pads"] = ["1", "2"]
    state["unread_pad_holes"] = ["2"]
    state["unread_pad_unfit"] = ["1", "2", "7", 3]
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    assert window._unread_pad_holes == ["2"] and window._unread_pad_unfit == ["1"]
    block = window._unread_pads_block()
    assert f"i valori letti per i pad 1 {UNFIT}" in block and "i fori dei pad 2 non sono stati letti" in block


# ── Height re-read: a conflict alone is something read ─────────────────────

def _conflict_only(**overrides):
    """A height re-read result with two quotes that disagree: no height, no question."""
    return header_extraction("RELAY", cols=2, body_height_mm=None, partial_reread=dict(PARTIAL_HEIGHT),
                             **dict({"body_conflict": CONFLICT}, **overrides))


def _nothing_read():
    """A height re-read result with no height, no question and no conflict."""
    return header_extraction("RELAY", cols=2, body_height_mm=None, partial_reread=dict(PARTIAL_HEIGHT))


def test_what_counts_as_nothing_read(window):  # noqa: F811
    assert window._reread_height_read_nothing(_nothing_read()) is True
    assert window._reread_height_read_nothing(_conflict_only()) is False
    # An empty conflict text is no conflict.
    assert window._reread_height_read_nothing(_conflict_only(body_conflict="  ")) is True


def test_with_a_settled_result_a_conflict_alone_is_reported_and_replaces_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    _single_part(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    window._extraction_questions = [_height_question()]
    applied = []
    monkeypatch.setattr(window, "_on_extracted", applied.append)
    # Nothing read: not merged, nothing said about a conflict.
    window._on_reread_done(_nothing_read())
    assert applied == [] and window._last_extracted.body_height_mm == 3
    assert "non coincidono" not in window._log_plain_text()
    # CONTRACT CHANGED 2026-11-11 (review V2-1). Round 4b merged the
    # conflict-only result (`merged.body_height_mm is None` was asserted
    # here): the declared height 3 became «no height», the STEP model was
    # deferred and the open height question was dropped, while the field
    # still showed 3.0. A conflict alone has nothing to stand in place of
    # the settled height or of its question: it is reported (that is what
    # tells it from «nothing read») and the settled result stays whole.
    window._on_reread_done(_conflict_only(pitch_mm=5.08))
    assert applied == []
    assert window._last_extracted.body_height_mm == 3 and window._last_extracted.pitch_mm == 2.54
    assert window._extraction_questions == [_height_question()]
    assert CONFLICT in window._log_plain_text()
    # Negative: a re-read that brings a height IS merged, scope only.
    window._on_reread_done(header_extraction("RELAY", cols=2, body_height_mm=7.5, pitch_mm=5.08,
                                             partial_reread=dict(PARTIAL_HEIGHT)))
    assert len(applied) == 1
    merged = applied[0]
    assert merged.body_height_mm == 7.5 and merged.pitch_mm == 2.54 and merged.partial_reread is None


def _reopened_without_height(window, monkeypatch, tmp_path):
    """A reopened single part whose height no document declared, with the STEP model wanted."""
    _reopened(window, monkeypatch, tmp_path)
    window.group_3d.setChecked(True)
    window._body_height_declared = False
    window._step_deferral = None
    assert "body_height_mm" in window._missing_body_quotes()
    assert window._step_deferral_reason()[0] == "missing"


def test_in_a_reopened_project_a_conflict_alone_defers_the_step_model_as_a_conflict(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened_without_height(window, monkeypatch, tmp_path)
    window._extraction_questions = [_pad_question(), _height_question()]
    before = window.field_body_height.value()
    window._on_reread_done(_conflict_only())
    assert window._step_deferral["code"] == "conflict" and window._step_deferral["conflict"] == CONFLICT
    assert window._step_deferral_reason() == ("conflict", CONFLICT)
    assert CONFLICT in window._missing_body_reason()
    assert window.field_body_height.value() == before
    assert "non coincidono" in window._log_plain_text()
    # CONTRACT CHANGED 2026-11-11 (review V2-1). Before: `== [_pad_question()]`,
    # the scope's stored question replaced by nothing -- the printed
    # candidates were lost and «Rigenera modello STEP» no longer asked. A
    # conflict alone replaces nothing: every stored question stays, the
    # deferral only words the reason (tests/test_review_round5_gui.py).
    assert window._extraction_questions == [_pad_question(), _height_question()]
    # Saved with the project: a second reopen still says «conflict».
    assert window._project_state()["step_deferral"]["code"] == "conflict"


def test_in_a_reopened_project_nothing_read_leaves_the_deferral_and_the_questions(window, monkeypatch, tmp_path):  # noqa: F811
    _reopened_without_height(window, monkeypatch, tmp_path)
    stored = [_pad_question(), _height_question()]
    window._extraction_questions = list(stored)
    window._on_reread_done(_nothing_read())
    assert window._step_deferral is None and window._step_deferral_reason()[0] == "missing"
    assert window._extraction_questions == stored
    assert "nessuna quota letta" in window._log_plain_text()


def test_a_conflict_beside_a_declared_height_defers_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    before = _reopened(window, monkeypatch, tmp_path)
    window.group_3d.setChecked(True)
    window._step_deferral = None
    assert "body_height_mm" not in window._missing_body_quotes()
    window._on_reread_done(_conflict_only())
    # The declared height stays and a body can be built: there is nothing to defer.
    assert window._step_deferral is None and window._step_deferral_reason() == ("", "")
    assert window.field_body_height.value() == before


# ── F9-3 class: only ASCII digits are integers ─────────────────────────────

def _turning_sense(numbers):
    """`_numbering_area` of three leads at fixed corners carrying `numbers`; None when undefined."""
    corners = ((0.0, 0.0), (4.0, 0.0), (4.0, 3.0))
    leads = [(SimpleNamespace(number=number, x_mm=x, y_mm=y), None) for number, (x, y) in zip(numbers, corners)]
    return package_geometry._numbering_area(leads)


def _extractor_order(numbers):
    return sorted(numbers, key=datasheet_extractor._pad_number_key)


def _pinout_order(numbers):
    return [entry["pin"] for entry in sorted(({"pin": number} for number in numbers), key=pin_sort_key)]


@pytest.mark.parametrize("digit", ["²", "①", "²³"])
@pytest.mark.parametrize("site", ["package_geometry", "datasheet_extractor", "pinout_dialog"])
def test_a_number_of_non_ascii_digits_is_text_at_every_site_that_converts(site, digit):
    assert digit.isdigit() and not digit.isascii()      # the trap: `int(digit)` raises
    if site == "package_geometry":
        # Text among the numbers: no turning sense, as for «A1».
        assert _turning_sense(["1", digit, "3"]) is None
    elif site == "datasheet_extractor":
        assert _extractor_order(["10", digit, "2"]) == ["2", "10", digit]
    else:
        assert _pinout_order(["10", digit, "2"]) == ["2", "10", digit]


@pytest.mark.parametrize("site", ["package_geometry", "datasheet_extractor", "pinout_dialog"])
def test_ascii_integers_keep_their_numeric_meaning_at_every_site(site):
    if site == "package_geometry":
        # The same corners numbered the other way round turn the other way.
        forward, backward = _turning_sense(["1", "2", "3"]), _turning_sense(["3", "2", "1"])
        assert forward == 6.0 and backward == -6.0
    elif site == "datasheet_extractor":
        assert _extractor_order(["10", "9", "A2", "A10"]) == ["9", "10", "A2", "A10"]
    else:
        assert _pinout_order(["10", "9", "A2", "A10"]) == ["9", "10", "A2", "A10"]
