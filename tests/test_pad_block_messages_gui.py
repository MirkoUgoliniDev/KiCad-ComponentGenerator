"""@brief The unread-pad block names the cause and the «Piazzole» fields never show a stale hole.

Two real analyses of 2026-11-13 ended in «Generazione automatica non
avviata» dead ends:

- The dialog said only that the hole of one pad was not read, «nessun foro
  stampato», while the cause -- positions of every pad unconfirmed, the pad
  without a hole a phantom, a layout confirmed by the drawn holes -- was in
  the extractor's account (`pin_map_conflict`) and only in the log. Contract:
  the account is shown under the block for EVERY reason a pad is dropped
  (session text, not restored on reopen); «no printed hole» is never said
  when other pads of the same part have a hole; an open pad-layout question
  is named only while its answer can be applied.
- The «Pad Ø / Foro Ø» spin boxes kept a previous analysis' numbers while
  the footprint was made of explicit pads. Contract: with explicit pads as
  the footprint (validated, or dropped as unread) the two fields are
  disabled, show «—» and a line says why; a grid THT keeps them enabled and
  filled.

Each rule with a positive and a negative case that differ structurally.
Offline: extraction results come from the real extractor validation on
synthetic answers, no worker runs a model, every dialog is replaced.
"""
import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services.datasheet_extractor import _params_from_tool_arguments, single_answer_to_data
from tests.test_unread_pad_positions import _answer, _pads
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction

#: Start of the line that carries the extractor's own account.
DETAIL = "Dettaglio dell'analisi:"
#: What the hole wording claims about the drawing when no pad has a hole.
NONE_PRINTED = "nessun foro stampato"
#: What the hole wording says when other pads of the part do have a hole.
MAY_NOT_EXIST = "potrebbero non esistere"
#: The sentence naming the open pad-layout question.
QUESTION = "Una domanda sulle posizioni dei pad è rimasta aperta"
#: A cause as the extractor words one, which the window cannot rebuild from pad numbers.
CAUSE = ("Le due letture delle posizioni dei pad non coincidono. Controllo dei fori disegnati: "
         "lettura dedicata: i fori disegnati coincidono.")


@pytest.fixture(autouse=True)
def _questions_left_open(monkeypatch):
    """Every structured question of the analysis is answered «Torna indietro».

    The extractor may ask about a pad without a hole (a «pad_layout»
    question); left open, the pads stay dropped and the block is what is
    under test. Without this, the question's modal would wait for a click.
    """
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, question: gui.QUESTION_BACK)


def _single_tht(window, monkeypatch, tmp_path, name="RLY"):
    """A fresh single-THT project; returns the list the warnings append (title, text) to."""
    prepare(window, monkeypatch, tmp_path, name)
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, text, *a, **k: warnings.append((title, text)))
    return warnings


def _holeless(lacking, conflict=None):
    """A real extraction whose pads in `lacking` were read without a hole (and without a terminal)."""
    pads = _pads()
    for pad in pads:
        if pad["number"] in lacking:
            del pad["drill_mm"]
    params = _params_from_tool_arguments(single_answer_to_data(_answer(pads)))
    assert params.footprint_pads_dropped() and params.package_geometry is None
    params.raw_main = {"name": "RLY", "_candidates": {}}
    if conflict is not None:
        params.pin_map_conflict = conflict
    return params


def _blocked(warnings):
    """The texts of the automatic-generation dialogs shown so far."""
    return [text for title, text in warnings if title == "Generazione automatica non avviata"]


def _assert_dialog_reason(window, warnings, expected):
    """`expected` is in the reason the automatic generation's dialog shows.

    The reason is what `_validate_automatic_input` raises (the one input of
    `_automatic_generation_blocked`). When the analysis left a structured
    question open, the automatic run stops on that question instead and no
    block dialog is shown; otherwise the dialog itself is checked too.
    """
    with pytest.raises(ValueError) as raised:
        window._validate_automatic_input(window._last_extracted)
    assert expected in str(raised.value)
    if not window._extraction_questions:
        assert _blocked(warnings) and expected in _blocked(warnings)[-1]


# -- 1. the hole sentence: never «no printed hole» when other pads have one ----------

def test_a_hole_missing_beside_read_holes_is_not_called_unprinted(window, monkeypatch, tmp_path):  # noqa: F811
    warnings = _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _holeless({"5"}))
    assert window._unread_pad_holes == ["5"] and window._unread_pad_holes_elsewhere is True
    block = window._unread_pads_block()
    assert "il foro dei pad 5 non è stato letto" in block and MAY_NOT_EXIST in block
    assert NONE_PRINTED not in block and "il disegno letto non li riporta" not in block
    assert "Verifica sul disegno se i pad 5 esistono davvero" in block
    _assert_dialog_reason(window, warnings, MAY_NOT_EXIST)
    # Saved as data: a reopened project words it the same way.
    state = window._project_state()
    assert state["unread_pad_holes_elsewhere"] is True
    window._switch_project("OTHER", window._general_project_state)
    assert window._unread_pad_holes_elsewhere is False
    window._switch_project("RLY", state)
    reopened = window._unread_pads_block()
    assert MAY_NOT_EXIST in reopened and NONE_PRINTED not in reopened


#: What only the wording of a pad that no witness names says.
UNQUOTED = "non compaiono in nessuna quota del disegno"
#: What only the wording of a quoted pad whose hole alone is not quoted says.
HOLE_NOT_QUOTED = "sono quotati nel disegno, ma il loro foro non è quotato"


def _with_unquoted(params, unquoted):
    """`params` as an extraction whose dedicated reading said which dropped pads no witness names."""
    params.unread_pad_geometry["unquoted_pads"] = list(unquoted)
    return params


def test_without_the_witness_list_both_possibilities_are_named(window, monkeypatch, tmp_path):  # noqa: F811
    """No dedicated reading on this path: the extraction cannot say whether pad 5 is quoted."""
    _single_tht(window, monkeypatch, tmp_path)
    params = _holeless({"5"})
    assert "unquoted_pads" not in params.unread_pad_geometry
    run_extraction(window, params)
    assert window._unread_pad_unquoted is None
    block = window._unread_pads_block()
    assert MAY_NOT_EXIST in block and "oppure il loro foro non è quotato" in block
    assert UNQUOTED not in block and HOLE_NOT_QUOTED not in block
    assert "unread_pad_unquoted" not in window._project_state()


def test_a_pad_no_witness_names_may_not_exist(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _with_unquoted(_holeless({"5"}), ["5"]))
    assert window._unread_pad_unquoted == ["5"]
    block = window._unread_pads_block()
    assert f"i pad 5 {UNQUOTED}" in block and "potrebbero non esistere" in block
    assert "Verifica sul disegno se i pad 5 esistono davvero" in block
    assert HOLE_NOT_QUOTED not in block and NONE_PRINTED not in block
    # Saved as data and restored.
    state = window._project_state()
    assert state["unread_pad_unquoted"] == ["5"]
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    assert window._unread_pad_unquoted == ["5"] and UNQUOTED in window._unread_pads_block()


def test_a_quoted_pad_whose_hole_alone_is_missing_is_never_called_non_existent(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    # The witnesses name every dropped pad: the list is there, and empty.
    run_extraction(window, _with_unquoted(_holeless({"5"}), []))
    assert window._unread_pad_unquoted == []
    block = window._unread_pads_block()
    assert f"i pad 5 {HOLE_NOT_QUOTED}" in block
    assert "Per il foro dei pad 5 ripeti l'analisi" in block
    assert "potrebbero non esistere" not in block and "esistono davvero" not in block
    # An empty list is information and survives the reopen as such.
    state = window._project_state()
    assert state["unread_pad_unquoted"] == []
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    assert window._unread_pad_unquoted == [] and HOLE_NOT_QUOTED in window._unread_pads_block()


def test_quoted_and_unquoted_pads_get_each_their_own_sentence(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _with_unquoted(_holeless({"4", "5"}), ["5"]))
    block = window._unread_pads_block()
    assert f"i pad 5 {UNQUOTED}" in block and f"i pad 4 {HOLE_NOT_QUOTED}" in block


#: What only the wording of a pad named only by the main answer's own chains says.
OWN_CHAINS = "sono nominati solo dalle catene di quote della risposta principale"


def _with_self_quoted(params, unquoted, self_quoted):
    """`params` whose dedicated reading also said which pads only the main answer's chains name."""
    _with_unquoted(params, unquoted)
    params.unread_pad_geometry["self_quoted_pads"] = list(self_quoted)
    return params


def test_a_pad_named_only_by_the_main_answers_chains_is_not_called_quoted(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _with_self_quoted(_holeless({"5"}), [], ["5"]))
    assert window._unread_pad_self_quoted == ["5"]
    block = window._unread_pads_block()
    assert f"i pad 5 {OWN_CHAINS}" in block
    assert HOLE_NOT_QUOTED not in block and "potrebbero non esistere" not in block
    # Round 4: own chains are no witness, so the remedy asks to check that
    # the pad exists (as for an unnamed pad), then for the hole; the
    # statement keeps its own wording.
    assert "Verifica sul disegno se i pad 5 esistono davvero" in block
    assert "Per il foro dei pad 5 ripeti l'analisi" not in block
    # Saved as data and restored.
    state = window._project_state()
    assert state["unread_pad_self_quoted"] == ["5"]
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    assert window._unread_pad_self_quoted == ["5"] and OWN_CHAINS in window._unread_pads_block()


def test_a_pad_an_independent_witness_names_stays_quoted(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    # The list is there and does not name pad 5: an independent witness does.
    run_extraction(window, _with_self_quoted(_holeless({"5"}), [], []))
    assert window._unread_pad_self_quoted == []
    block = window._unread_pads_block()
    assert f"i pad 5 {HOLE_NOT_QUOTED}" in block and OWN_CHAINS not in block
    # Negative of round 4: a pad an independent witness names is not asked
    # to be checked for existence; only its hole is missing.
    assert "esistono davvero" not in block and "Per il foro dei pad 5 ripeti l'analisi" in block
    assert window._project_state()["unread_pad_self_quoted"] == []


def test_without_the_own_chain_list_the_quoted_wording_stands(window, monkeypatch, tmp_path):  # noqa: F811
    """Absent key (an extractor that does not say): today's split, nothing saved."""
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _with_unquoted(_holeless({"5"}), []))
    assert window._unread_pad_self_quoted is None
    block = window._unread_pads_block()
    assert HOLE_NOT_QUOTED in block and OWN_CHAINS not in block
    assert "unread_pad_self_quoted" not in window._project_state()


def test_an_unnamed_pad_wins_over_the_own_chain_list(window, monkeypatch, tmp_path):  # noqa: F811
    """A pad in both lists (a malformed extraction) is worded as the stronger doubt."""
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _with_self_quoted(_holeless({"4", "5"}), ["5"], ["4", "5"]))
    block = window._unread_pads_block()
    assert f"i pad 5 {UNQUOTED}" in block and f"i pad 4 {OWN_CHAINS}" in block
    # Pad 5 is worded once, as unnamed, never also among the own-chain pads.
    assert f"i pad 4, 5 {OWN_CHAINS}" not in block and f"i pad 4, 5 {UNQUOTED}" not in block


def test_a_malformed_saved_witness_list_says_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _with_unquoted(_holeless({"5"}), ["5"]))
    state = dict(window._project_state(), unread_pad_unquoted="5")
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    # Unknown again: the neutral wording, never a guess.
    assert window._unread_pad_unquoted is None
    block = window._unread_pads_block()
    assert UNQUOTED not in block and HOLE_NOT_QUOTED not in block and MAY_NOT_EXIST in block


def test_no_hole_on_any_pad_keeps_the_unprinted_wording(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _holeless({"1", "2", "3", "4", "5"}))
    assert window._unread_pad_holes == ["1", "2", "3", "4", "5"]
    assert window._unread_pad_holes_elsewhere is False
    block = window._unread_pads_block()
    assert NONE_PRINTED in block and "il disegno letto non li riporta" in block
    assert MAY_NOT_EXIST not in block
    assert "unread_pad_holes_elsewhere" not in window._project_state()


def test_a_hand_edited_flag_without_unread_holes_decides_nothing(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _holeless({"1", "2", "3", "4", "5"}))
    state = dict(window._project_state(), unread_pad_holes_elsewhere="yes")
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    # Not a strict True: the wording stays the one of «no hole at all».
    assert window._unread_pad_holes_elsewhere is False
    assert NONE_PRINTED in window._unread_pads_block()


# -- 1. the extractor's account: shown for every reason, session only ----------------

def test_the_cause_is_shown_beside_an_unread_hole(window, monkeypatch, tmp_path):  # noqa: F811
    warnings = _single_tht(window, monkeypatch, tmp_path)
    params = _holeless({"5"}, conflict=CAUSE)
    run_extraction(window, params)
    block = window._unread_pads_block()
    assert f"{DETAIL} {CAUSE}" in block
    # In the dialog the user reads, not only in the log.
    _assert_dialog_reason(window, warnings, CAUSE)
    # Last: the window's own sentences come first.
    assert block.index(DETAIL) > block.index("il foro dei pad 5")


def test_the_cause_is_shown_beside_unread_positions(window, monkeypatch, tmp_path):  # noqa: F811
    warnings = _single_tht(window, monkeypatch, tmp_path)
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(unread=("5",)))))
    params.raw_main = {"name": "RLY", "_candidates": {}}
    account = params.pin_map_conflict.strip()
    run_extraction(window, params)
    assert window._unread_pad_holes is None and window._unread_pad_unfit is None
    assert f"{DETAIL} {account}" in window._unread_pads_block()
    _assert_dialog_reason(window, warnings, account)


def test_the_cause_is_session_text_and_a_reopened_project_shows_none(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _holeless({"5"}, conflict=CAUSE))
    state = window._project_state()
    assert CAUSE not in str(state)
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    block = window._unread_pads_block()
    assert block and DETAIL not in block


def test_a_conflict_without_dropped_pads_is_no_unread_pad_block(window, monkeypatch, tmp_path):  # noqa: F811
    """A grid numbering in disagreement is corrected in the fields: no block, no account here."""
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, header_extraction("RLY", pin_map_conflict=CAUSE))
    assert window._unread_pads is None and window._unread_pad_detail is None
    assert window._unread_pads_block() == ""


# -- 1. the open pad-layout question: named only while it can be answered -----------

def _with_open_question(window, params):
    """Record an open pad-layout question that the settled extraction can apply."""
    question = {"key": "pad_layout:1|2|test", "field": "pad_layout", "text": "Domanda?",
                "options": [{"label": "Dedicata", "value": "dedicated"}], "evidence": None}
    params.questions = [question]
    params.candidates = {question["key"]: {"dedicated": {}}}
    window._extraction_questions = [question]
    return question


def test_an_open_question_the_session_can_apply_is_named(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    params = _holeless({"5"}, conflict=CAUSE)
    run_extraction(window, params)
    _with_open_question(window, window._last_extracted)
    block = window._unread_pads_block()
    assert QUESTION in block and block.index(QUESTION) < block.index(DETAIL)


def test_an_open_question_without_a_settled_extraction_is_not_named(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _holeless({"5"}, conflict=CAUSE))
    _with_open_question(window, window._last_extracted)
    # A reopened project: the question is saved, the extraction that applies it is not.
    window._last_extracted = None
    assert window._stored_pad_layout_question() is not None
    assert QUESTION not in window._unread_pads_block()


# -- 2. «Pad Ø / Foro Ø»: not shown as current when explicit pads are the footprint ----

def _assert_unused(window):
    for spin in (window.spin_single_tht_pad, window.spin_single_tht_drill):
        assert not spin.isEnabled() and spin.text() == "—"
    assert not window.label_single_tht_pads_unused.isHidden()


def test_dropped_explicit_pads_disable_the_matrix_pad_fields(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    # A previous analysis' value, which the explicit-pad extraction does not overwrite.
    window.field_drill.setValue(1.55)
    window._sync_single_tht_pad_controls()
    assert window.spin_single_tht_drill.text() != "—"
    run_extraction(window, _holeless({"5"}))
    _assert_unused(window)
    # The data is not rewritten, only not shown as this component's hole.
    assert abs(window.field_drill.value() - 1.55) < 1e-9
    # Reopened: the same view.
    state = window._project_state()
    window._switch_project("OTHER", window._general_project_state)
    window._switch_project("RLY", state)
    _assert_unused(window)


def test_validated_explicit_pads_disable_the_matrix_pad_fields(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads())))
    assert params.package_geometry is not None and not params.footprint_pads_dropped()
    run_extraction(window, params)
    assert window.package_geometry is not None
    _assert_unused(window)


def test_a_grid_tht_keeps_the_matrix_pad_fields_enabled_and_filled(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path)
    run_extraction(window, _holeless({"5"}))
    _assert_unused(window)
    # A later grid analysis: the matrix pad and drill ARE the footprint again.
    run_extraction(window, header_extraction("RLY", pad_diameter_mm=1.8, drill_mm=1.1))
    assert window.package_geometry is None and window._unread_pads is None
    for spin, value in ((window.spin_single_tht_pad, 1.8), (window.spin_single_tht_drill, 1.1)):
        assert spin.isEnabled() and abs(spin.value() - value) < 1e-9 and spin.text() != "—"
    assert window.label_single_tht_pads_unused.isHidden()
