"""@brief Round 7b (2026-11-11): the findings of the final verification V6, fixed.

- F1: a «no_match» WITHOUT a decisive fit (`max_error_mm` None) against two
  agreeing readings is the "pad_layout" question too (1266fb7 refused it,
  round 7 confirmed it with a warning); its text claims no fit: the holes
  do not coincide with the layout and confirm no other.
- F2: a «distorted» verdict with no named pad contradicts the agreement
  when the stretch of its fit (`LayoutCheck.stretch_ratio`) exceeds
  drawing_holes.HYPOTHESIS_TOL_SPAN; below the span the warning stays.
- F3: an open pad-layout question is asked by every command that writes
  the footprint; «Torna indietro» writes nothing; the unattended paths write
  nothing; the log names the command that asks each kind of question.
- F4: the one-option question does not repeat «Posizioni dei pad», asks for
  a confirmation, and says what «Torna indietro» does.
- F6: changing a type's instructions in the profile dialog keeps a block
  the user edited in the open project.

Every rule has a positive and a negative case that differ for a structural
reason (user's rule, .claude/CLAUDE.md «Correzioni»). No network, no AI call.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QLabel

from kicadgen.gui import main_window as gui
from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import drawing_holes as dh
from kicadgen.services.datasheet_extractor import apply_dedicated_pads

from test_extraction_review_round6 import _main, _reading  # noqa: E402 - shared doubles
from test_review_round6b import draw_page  # noqa: E402
from test_review_round7_gui import (  # noqa: E402 - the real-window helpers of round 7
    USER_NOTES, _new, _select, _set_instructions, _type, quiet_config)  # noqa: F401
from tests.ui_flow_helpers import package_extraction, prepare, run_extraction, soic_geometry

#: Words the question must never say: the witness cannot prove them (round 7).
FORBIDDEN = ("in scala,", "e' in scala", "smentisc", "non e' dove le letture")


def _verdict(reason, *, max_error_mm=0.9, observable=True, stretch=None, misplaced=None):
    """A refusal of the drawn-hole witness, with the fields the extractor reads."""
    return SimpleNamespace(status="not_confirmed", reason=reason, image="drawing.png",
                           max_error_mm=max_error_mm, tolerance_mm=0.15, circles=8,
                           scale_observable=observable, misplaced_pad=misplaced,
                           stretch_ratio=stretch, body_check=None, body_exchanged=False)


def _witness(**verdict):
    """A witness that answers every layout with the same verdict."""
    return lambda pads, body: _verdict(**verdict)


def _pad_question(data):
    question, = [q for q in data.get("_questions") or () if q["field"] == "pad_layout"]
    return question


def _texts(data):
    return [data.get("_numbering_note") or "", data.get("_pin_map_conflict") or ""] + [
        q["text"] for q in data.get("_questions") or ()]


# ---------------------------------------------------------------------------
# F1: a no_match without a fit is a question, said as such
# ---------------------------------------------------------------------------

def test_an_observable_no_match_without_a_fit_is_a_one_option_question():
    """Positive: the pads could show the page's scale, the witness found no assignment: asked."""
    data = _main()
    before = copy.deepcopy(data["package_geometry"]["pins"])
    apply_dedicated_pads(data, _reading(), check_layout=_witness(reason="no_match", max_error_mm=None))
    assert "_pin_map_conflict" not in data, "never a hard block"
    question = _pad_question(data)
    assert len(question["options"]) == 1
    assert ("i fori disegnati in drawing.png non coincidono con questa disposizione e non ne "
            "confermano nessun'altra") in question["text"]
    assert data["package_geometry"]["pins"] == before
    assert not any(word in text for text in _texts(data) for word in FORBIDDEN)


def test_a_no_match_the_pads_cannot_observe_still_confirms_with_the_warning():
    """Negative twin: the same refusal on pads that cannot show a page's scale (H5/M18).

    With pads that cannot tell a wrong reading from a page not to scale the
    «no_match» is no losing verdict (`_losing_verdict`): nothing changes.
    """
    data = _main()
    apply_dedicated_pads(data, _reading(),
                         check_layout=_witness(reason="no_match", max_error_mm=None, observable=False))
    assert not data.get("_questions") and "_pin_map_conflict" not in data
    assert "confermate dalla lettura dedicata" in data["_numbering_note"]


def test_the_no_fit_text_is_used_only_without_a_fit():
    """The decisive fit keeps the round-7 text; only the absent fit says «nessun'altra»."""
    decisive = _main()
    apply_dedicated_pads(decisive, _reading(), check_layout=_witness(reason="no_match"))
    text = _pad_question(decisive)["text"]
    assert "non confermano questa disposizione" in text and "nessun'altra" not in text
    contradiction = {"verdict": _verdict("no_match", max_error_mm=None), "fitted": False,
                     "misplaced": None, "stretch": None, "singled": []}
    # A hole-backed alternative on offer makes «they confirm no other» false.
    assert "nessun'altra" in ext._contradiction_text(contradiction)
    assert "nessun'altra" not in ext._contradiction_text(contradiction, alternatives=True)


# ---------------------------------------------------------------------------
# F2: «distorted» beyond the page-stretch span is a question
# ---------------------------------------------------------------------------

def test_a_distorted_fit_beyond_the_span_is_a_question_with_the_measured_stretch():
    data = _main()
    stretch = dh.HYPOTHESIS_TOL_SPAN + 0.01
    apply_dedicated_pads(data, _reading(), check_layout=_witness(reason="distorted", stretch=stretch))
    assert "_pin_map_conflict" not in data
    text = _pad_question(data)["text"]
    assert f"{100 * stretch:.1f}%" in text and f"{100 * dh.HYPOTHESIS_TOL_SPAN:.0f}%" in text
    assert "il pad" not in text, "no pad is named: the witness named none"


@pytest.mark.parametrize("stretch", [None, dh.HYPOTHESIS_TOL_SPAN - 0.01, dh.HYPOTHESIS_TOL_SPAN],
                         ids=["no stretch shown", "within the span", "at the span"])
def test_a_distorted_fit_within_the_span_keeps_the_warning(stretch):
    """Negative twin: the legitimate case of 65aee39, a page not drawn to scale, stays confirmed."""
    data = _main()
    apply_dedicated_pads(data, _reading(), check_layout=_witness(reason="distorted", stretch=stretch))
    assert not data.get("_questions") and "_pin_map_conflict" not in data
    assert "confermate dalla lettura dedicata" in data["_numbering_note"]
    assert "Attenzione" in data["_numbering_note"]


def _header(pitch=2.54):
    return {str(r * 4 + c + 1): (c * pitch, r * 2.54) for r in range(2) for c in range(4)}


def test_the_witness_reports_the_stretch_of_a_wrong_pitch(tmp_path):
    """Real witness: a 2x4 header read with a pitch 7 % long on a page to scale (V6, wp6.py)."""
    image = draw_page(tmp_path / "page.png", list(_header().values()))
    wrong = _header(2.54 * 1.07)

    def holes(pads):
        return dh.check_pad_layout([image], pads)
    verdict = holes(wrong)
    assert verdict.reason == "distorted" and verdict.stretch_ratio > dh.HYPOTHESIS_TOL_SPAN
    assert ext._agreement_contradicted(wrong, holes)["stretch"] == verdict.stretch_ratio


def test_a_correct_header_on_a_page_stretched_within_the_span_is_no_contradiction(tmp_path):
    """Negative twin, real witness: the right reading on a page 4 % long (a scan) -- no question."""
    image = draw_page(tmp_path / "page.png", list(_header().values()), sx=1.04)

    def holes(pads):
        return dh.check_pad_layout([image], pads)
    verdict = holes(_header())
    assert verdict.stretch_ratio is None or verdict.stretch_ratio <= dh.HYPOTHESIS_TOL_SPAN
    assert ext._agreement_contradicted(_header(), holes) is None


def test_a_confirmation_carries_no_stretch_ratio(tmp_path):
    """The field belongs to «distorted» only: a confirmed or no_match verdict leaves it None."""
    image = draw_page(tmp_path / "page.png", list(_header().values()))
    assert dh.check_pad_layout([image], _header()).stretch_ratio is None


# ---------------------------------------------------------------------------
# F4: wording of the one-option question
# ---------------------------------------------------------------------------

def test_the_contradiction_question_does_not_repeat_its_opening_words():
    data = _main()
    apply_dedicated_pads(data, _reading(), check_layout=_witness(reason="no_match"))
    text = _pad_question(data)["text"]
    assert text.startswith("Posizioni dei pad NON confermate")
    assert "Posizioni dei pad: Posizioni dei pad" not in text
    assert text.endswith("Confermare la disposizione letta dopo averla controllata sul disegno?")


def _dialog_text(window, monkeypatch, question):
    """The informative text of the real question dialog, closed with «Torna indietro»."""
    seen = {}

    def run_modal(box):
        seen["text"] = box.informativeText()
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    assert window._ask_extraction_question(question) is gui.QUESTION_BACK
    return seen["text"]


def test_a_one_option_pad_question_asks_a_confirmation_and_says_what_back_does(window, monkeypatch):
    question = {"key": "pad_layout:x", "field": "pad_layout", "text": "Posizioni dei pad NON confermate.",
                "options": [{"label": "lettura", "value": "main:1"}], "evidence": None}
    text = _dialog_text(window, monkeypatch, question)
    assert "Conferma l'opzione solo dopo averla controllata sul documento" in text
    assert "Scegli il valore che vedi" not in text
    assert "«Torna indietro» lascia la domanda aperta" in text


def test_a_two_option_height_question_keeps_the_choice_wording(window, monkeypatch):
    """Negative twin: a real choice of value, and a field no footprint command asks."""
    question = {"key": "h:1", "field": "body_height_mm", "text": "Altezza?",
                "options": [{"label": "18 mm", "value": 18.0}, {"label": "23 mm", "value": 23.0}],
                "evidence": None}
    text = _dialog_text(window, monkeypatch, question)
    assert "Scegli il valore che vedi nel documento" in text
    assert "Conferma l'opzione" not in text and "lascia la domanda aperta" not in text


# ---------------------------------------------------------------------------
# F3: every command that writes the footprint asks the open pad question
# ---------------------------------------------------------------------------

KEY = "pad_layout:main+dedicated:abcd1234"
VALUE = "main+dedicated:abcd1234"


def _pad_extraction():
    """An SOIC-8 result with an open one-option pad question whose layout moves pad 1 by -0.2 mm."""
    geometry = soic_geometry()
    params = package_extraction(geometry)
    chosen = [{"number": p["number"], "x_mm": p["x_mm"] - (0.2 if p["number"] == "1" else 0.0),
               "y_mm": p["y_mm"]} for p in geometry["pins"]]
    params.questions = [{"key": KEY, "field": "pad_layout", "text": "Posizioni dei pad NON confermate.",
                         "options": [{"label": "lettura", "value": VALUE}], "evidence": None}]
    params.candidates = {KEY: {VALUE: {"source": "main", "pins": chosen, "origin": "body_center",
                                       "view": None, "notes": []}}}
    return params


def _footprint(window, name="SOIC"):
    return window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"


def _answer(monkeypatch, value, asked):
    def ask(self, question):
        asked.append(question["key"])
        return value
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", ask)


def _left_open(window, monkeypatch, tmp_path):
    """Run the analysis and go back on the pad question: nothing generated, question open."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    asked = []
    _answer(monkeypatch, gui.QUESTION_BACK, asked)
    run_extraction(window, _pad_extraction())
    assert asked == [KEY] and not _footprint(window).exists()
    assert [q["key"] for q in window._extraction_questions] == [KEY]
    return asked


def test_back_then_rigenera_asks_again_and_back_writes_nothing(window, monkeypatch, tmp_path):
    asked = _left_open(window, monkeypatch, tmp_path)
    log = window._log_plain_text()
    assert "«Rigenera footprint» o «Applica ai file»" in log
    assert "rispondi da «Rigenera modello STEP»" not in log
    assert window._generate(footprint_only=True) is False
    assert asked == [KEY, KEY] and not _footprint(window).exists()
    assert [q["key"] for q in window._extraction_questions] == [KEY]
    assert window._extraction_answers == {}
    assert "resta aperta e verrà riproposta prima di scrivere il footprint" in window._log_plain_text()


def test_back_then_rigenera_and_an_answer_writes_the_chosen_layout(window, monkeypatch, tmp_path):
    asked = _left_open(window, monkeypatch, tmp_path)
    _answer(monkeypatch, VALUE, asked)
    assert window._generate(footprint_only=True) is True
    assert asked == [KEY, KEY] and _footprint(window).exists()
    pad1 = next(p for p in window.package_geometry.pins if str(p.number) == "1")
    assert pad1.x_mm == pytest.approx(-2.9)
    assert window._extraction_answers == {KEY: VALUE} and window._extraction_questions == []
    assert window._project_state()["extraction_answers"] == {KEY: VALUE}
    # Answered: the next command does not ask again.
    assert window._generate(footprint_only=True) is True and asked == [KEY, KEY]


def test_a_full_generation_asks_the_open_question_too(window, monkeypatch, tmp_path):
    """«Genera» without footprint_only: the same question, before anything is written."""
    asked = _left_open(window, monkeypatch, tmp_path)
    assert window._generate() is False and asked == [KEY, KEY] and not _footprint(window).exists()


def test_without_an_open_question_nothing_is_asked(window, monkeypatch, tmp_path):
    """Negative twin: no open pad question, the command writes as it always did."""
    prepare(window, monkeypatch, tmp_path, "SOIC")
    run_extraction(window, package_extraction(soic_geometry()))
    assert _footprint(window).exists()
    _footprint(window).unlink()

    def never(self, question):
        raise AssertionError("no question may be asked")
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", never)
    assert window._generate(footprint_only=True) is True and _footprint(window).exists()


def test_the_unattended_paths_write_nothing_while_the_question_is_open(window, monkeypatch, tmp_path):
    """The automatic pipeline and the deferred rewrite cannot ask: they write nothing and say why."""
    asked = _left_open(window, monkeypatch, tmp_path)
    assert window._generate(automatic=True) is False
    window._fp_rewrite_flushing = True
    try:
        assert window._generate(footprint_only=True) is False
    finally:
        window._fp_rewrite_flushing = False
    assert asked == [KEY] and not _footprint(window).exists()
    assert "la domanda sulle posizioni dei pad è ancora aperta" in window._log_plain_text()


def test_a_reopened_project_cannot_apply_the_answer_and_writes_nothing(window, monkeypatch, tmp_path):
    """Without the session's extraction the candidates are gone: no question, no write, a remedy."""
    asked = _left_open(window, monkeypatch, tmp_path)
    window._last_extracted = None
    assert window._generate(footprint_only=True) is False
    assert asked == [KEY] and not _footprint(window).exists()
    assert "Ripeti l'analisi dei documenti e rispondi alla domanda" in window._log_plain_text()


def test_the_held_back_log_names_the_command_of_each_question(window):
    """The height still goes to «Rigenera modello STEP»; the pads to the footprint commands."""
    height = {"key": "h:1", "field": "body_height_mm", "text": "t", "options": [], "evidence": None}
    pads = {"key": KEY, "field": "pad_layout", "text": "t", "options": [], "evidence": None}
    view = {"key": "v:1", "field": "contacts_view", "text": "t", "options": [], "evidence": None}
    window._extraction_questions = [height]
    text = window._held_back_questions_text()
    assert "«Rigenera modello STEP»" in text and "«Rigenera footprint»" not in text
    window._extraction_questions = [pads]
    text = window._held_back_questions_text()
    assert "«Rigenera footprint»" in text and "«Rigenera modello STEP»" not in text
    # CONTRACT CHANGED 2026-11-14 (GUI review H1): the terminal view is a
    # footprint question now (a bottom view mirrors the footprint, so none is
    # written with the view unknown): its held-back line names the footprint
    # commands. The «other questions» line is checked on another field.
    window._extraction_questions = [view]
    text = window._held_back_questions_text()
    assert "«Rigenera footprint»" in text and "ripetendo l'analisi" not in text
    other = {"key": "e:1", "field": "height_equals_plan", "text": "t", "options": [], "evidence": None}
    window._extraction_questions = [other]
    assert "ripetendo l'analisi" in window._held_back_questions_text()


# ---------------------------------------------------------------------------
# F6: the profile dialog keeps a block the user edited
# ---------------------------------------------------------------------------

_EDITED_LOG = "conserva le istruzioni della tipologia modificate"


def test_a_profile_change_keeps_the_user_s_edited_block(window, monkeypatch, quiet_config):
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v1")
    _new(window, monkeypatch, "ProfiloModificato")
    _select(window, "single_tht")
    edited = component_types.apply_block(
        USER_NOTES, _type("ISTRUZIONI DEL PROFILO v1\n- I miei fori sono da 1.3."))
    window.field_component_prompt.setPlainText(edited)
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v2")
    window._reload_profile_types(update_prompt=True,
                                 previous_instructions="ISTRUZIONI DEL PROFILO v1")
    assert window.field_component_prompt.toPlainText() == edited
    assert window._log_plain_text().count(_EDITED_LOG) == 1


def test_a_profile_change_refreshes_the_program_s_block(window, monkeypatch, quiet_config):
    """Negative twin: the block holds the program's text, so it takes the new instructions."""
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v1")
    _new(window, monkeypatch, "ProfiloProgramma")
    _select(window, "single_tht")
    window.field_component_prompt.setPlainText(
        window.field_component_prompt.toPlainText() + "\n\n" + USER_NOTES)
    _set_instructions(window, "single_tht", "ISTRUZIONI DEL PROFILO v2")
    window._reload_profile_types(update_prompt=True,
                                 previous_instructions="ISTRUZIONI DEL PROFILO v1")
    notes = window.field_component_prompt.toPlainText()
    assert component_types.block_text(notes) == "ISTRUZIONI DEL PROFILO v2"
    assert notes.endswith(USER_NOTES)
    assert _EDITED_LOG not in window._log_plain_text()
