"""@brief The NC/NO witness in the window and in «Verifica componente» (2026-11-14).

@details Night real runs: one relay drawing read twice gave NC and NO swapped
and nothing checked them. The extractor now compares the readings and, when
they disagree, asks «contact_nc» (which terminal is normally closed). In the
window that is a SYMBOL question: asked in the analysis' question pass, it
blocks no footprint and holds no generation back; left open, «Verifica
componente per KiCad» reports it and asks it again, and the answer recomposes
the symbol from the new pinout functions. The extraction's
`contact_function_witness` is saved with the project and shown by the check:
«two_models»/«user» OK, «same_model» a final review, «single»/«asked» to fix
before production. Offline: `apply_answers` is replaced by a fake with the extractor's
contract (the chosen option's role assignment is written into the pinout,
the witness becomes «user», the question closes), so the window's flow is
tested with OPAQUE option values: the window never parses them (the
extractor's options are whole role assignments). Positive and negative cases.
"""

from copy import deepcopy

from kicadgen.gui import main_window as gui
from kicadgen.services import component_check
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.component_check import (ACTION_ANSWER_CONTACT_NC, ACTION_EDIT_PINOUT,
                                               IMPORTANCE_BEFORE_PRODUCTION,
                                               IMPORTANCE_FINAL_REVIEW, contact_function_checks,
                                               verify_component)

NC_KEY = "contact_nc:K1|3=nc/no|4=no/nc"
NC_QUESTION = {"key": NC_KEY, "field": "contact_nc", "text": "Quale contatto e' normalmente chiuso (NC)?",
               "evidence": None,
               "options": [{"label": "Lettura principale", "value": "reading-a"},
                           {"label": "Lettura dedicata", "value": "reading-b"},
                           {"label": "Nessuna delle due", "value": "none"}]}
#: The role assignment each option stands for (what the fake extractor writes).
ASSIGNMENTS = {"reading-a": {"3": "nc", "4": "no"}, "reading-b": {"3": "no", "4": "nc"}}


def _params(witness="asked"):
    """A settled relay extraction holding the open «contact_nc» question and its candidates."""
    pinout = [{"pin": "1", "name": "COIL+"}, {"pin": "2", "name": "COIL-"},
              {"pin": "3", "name": "NC", "function": "nc"}, {"pin": "4", "name": "NO", "function": "no"},
              {"pin": "5", "name": "COM", "function": "common"}]
    return ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                               layout="package", pinout=pinout, questions=[deepcopy(NC_QUESTION)],
                               candidates={NC_KEY: {value: {} for value in ASSIGNMENTS}},
                               contact_function_witness=witness)


def _fake_apply(calls):
    """A stand-in for `apply_answers` with the extractor's «contact_nc» contract, recording each call."""
    def apply(params, answers):
        calls.append(dict(answers))
        result = deepcopy(params)
        value = answers.get(NC_KEY)
        listed = any(q["key"] == NC_KEY for q in result.questions or ())
        if listed and value in ASSIGNMENTS:
            for row in result.pinout:
                if row["pin"] in ASSIGNMENTS[value]:
                    row["function"] = ASSIGNMENTS[value][row["pin"]]
            result.contact_function_witness = "user"
            result.questions = [q for q in result.questions if q["key"] != NC_KEY] or None
        elif listed and value == "none":
            # «Nessuna delle due»: nothing applied, the question closed, the
            # roles left to the Pinout table.
            result.contact_function_witness = "manual"
            result.questions = [q for q in result.questions if q["key"] != NC_KEY] or None
        return result
    return apply


def _prepare(window, monkeypatch, params, answer):
    """The window with `params` settled and a scripted answer; returns (asked, logged, settled pinouts)."""
    asked, logged, settled = [], [], []
    calls = []
    monkeypatch.setattr(ext, "apply_answers", _fake_apply(calls))
    window._extraction_questions = window._valid_extraction_questions(params.questions)
    window._extraction_answers = {}
    window._last_extracted = params
    monkeypatch.setattr(window, "_settled_extraction", lambda: window._last_extracted)
    monkeypatch.setattr(window, "_ask_extraction_question",
                        lambda question: asked.append(question["field"]) or answer)
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    monkeypatch.setattr(gui.QMessageBox, "information",
                        staticmethod(lambda *a, **k: logged.append("INFORMATION")))
    monkeypatch.setattr(window, "_settle_extracted_pinout",
                        lambda candidate: settled.append(deepcopy(candidate)))
    return asked, logged, settled


def _functions(rows):
    """{pin: function} of pinout rows."""
    return {row["pin"]: row.get("function") for row in rows or () if row.get("function")}


# ── The window ────────────────────────────────────────────────────────────

def test_contact_nc_is_a_symbol_question_that_blocks_no_footprint(window, monkeypatch):
    """@brief Positive: asked in the pass; back leaves it open WITHOUT holding back; negative: no footprint block."""
    asked, logged, _settled = _prepare(window, monkeypatch, _params(), gui.QUESTION_BACK)
    assert "contact_nc" in window.EXTRACTION_QUESTION_FIELDS
    assert "contact_nc" not in window.FOOTPRINT_QUESTION_FIELDS
    params, held_back = window._review_extraction_questions(_params())
    assert asked == ["contact_nc"] and held_back is False
    assert [q["key"] for q in window._extraction_questions] == [NC_KEY]
    assert window._pad_layout_question_block() == "" and window._stored_pad_layout_question() is None
    assert any("Verifica componente per KiCad" in line for line in logged)
    text = window._held_back_questions_text()
    assert "Verifica componente per KiCad" in text and "ripetendo l'analisi" not in text


def test_an_answer_in_the_pass_settles_the_witness(window, monkeypatch):
    """@brief Positive: the dedicated reading chosen in the pass -> its roles, witness «user»."""
    _prepare(window, monkeypatch, _params(), "reading-b")
    params, held_back = window._review_extraction_questions(_params())
    assert held_back is False and not params.questions
    assert _functions(params.pinout) == {"3": "no", "4": "nc", "5": "common"}
    assert window._contact_function_witness == "user"
    assert window._extraction_answers == {NC_KEY: "reading-b"}


def test_the_open_question_is_asked_later_and_recomposes_the_symbol(window, monkeypatch):
    """@brief Positive: asked from the check's action -> applied, remembered, symbol settled from the new pinout."""
    asked, _logged, settled = _prepare(window, monkeypatch, _params(), "reading-b")
    window._contact_function_witness = "asked"
    assert window._answer_contact_nc_action()
    assert asked == ["contact_nc"] and window._extraction_questions == []
    assert window._extraction_answers == {NC_KEY: "reading-b"}
    assert window._contact_function_witness == "user"
    assert len(settled) == 1 and _functions(settled[0]) == {"3": "no", "4": "nc", "5": "common"}


def test_back_or_a_reopened_project_changes_nothing(window, monkeypatch):
    """@brief Negative: «Torna indietro» or no settled result -> no answer, no symbol, the action fails."""
    asked, logged, settled = _prepare(window, monkeypatch, _params(), gui.QUESTION_BACK)
    assert window._resolve_stored_symbol_question() is False
    assert asked == ["contact_nc"] and settled == [] and window._extraction_answers == {}
    assert [q["key"] for q in window._extraction_questions] == [NC_KEY]
    window._last_extracted = None
    try:
        window._answer_contact_nc_action()
    except RuntimeError:
        pass
    else:
        raise AssertionError("an unresolved question must fail the action")
    assert "INFORMATION" in logged and settled == []


def test_the_witness_is_saved_with_the_project_and_reaches_the_check(window):
    """@brief Positive: saved, restored and passed to the check; negative: an unknown value is dropped."""
    window._contact_function_witness = "single"
    state = window._project_state()
    assert state.get("contact_function_witness") == "single"
    window._contact_function_witness = None
    window._apply_project_state(deepcopy(state))
    assert window._contact_function_witness == "single"
    assert window._component_check_args()["contact_witness"] == "single"
    window._apply_project_state(dict(state, contact_function_witness="manual"))
    assert window._contact_function_witness == "manual"
    window._apply_project_state(dict(state, contact_function_witness="maybe"))
    assert window._contact_function_witness is None
    state.pop("contact_function_witness")
    window._apply_project_state(deepcopy(state))
    assert window._contact_function_witness is None


def test_neither_reading_closes_the_question_and_leaves_the_pinout(window, monkeypatch):
    """@brief «Nessuna delle due» -> question closed, pinout unchanged, witness «manual», said so."""
    asked, logged, settled = _prepare(window, monkeypatch, _params(), "none")
    outcome = window._answer_contact_nc_action()
    assert asked == ["contact_nc"] and window._extraction_questions == []
    assert window._extraction_answers == {NC_KEY: "none"}
    assert window._contact_function_witness == "manual"
    assert _functions(settled[0]) == {"3": "nc", "4": "no", "5": "common"}, "nothing applied"
    assert "tabella Pinout" in outcome
    assert not any("non applicabile" in line for line in logged)


def test_the_pinout_action_turns_a_hand_correction_into_the_user_s_choice(window, monkeypatch):
    """@brief Positive: the table edited -> «manual» becomes «user»; negative: closed unchanged -> fails, stays."""
    window.pinout = [{"pin": "3", "name": "NC"}, {"pin": "4", "name": "NO"}]
    window._contact_function_witness = "manual"
    monkeypatch.setattr(window, "_show_pinout_dialog", lambda: None)
    try:
        window._edit_pinout_action()
    except RuntimeError:
        pass
    else:
        raise AssertionError("a table closed without a change is not a fix")
    assert window._contact_function_witness == "manual"
    monkeypatch.setattr(window, "_show_pinout_dialog", lambda: setattr(
        window, "pinout", [{"pin": "3", "name": "NO"}, {"pin": "4", "name": "NC"}]))
    assert window._edit_pinout_action()
    assert window._contact_function_witness == "user"


# ── The check ────────────────────────────────────────────────────────────

def test_each_witness_gives_its_verdict():
    """@brief Positive: two models/user OK; same model final review; single/asked before production."""
    def only(witness):
        (result,) = contact_function_checks(witness)
        return result
    assert only("two_models").status == "OK" and only("user").status == "OK"
    same = only("same_model")
    assert same.status == "WARN" and same.importance == IMPORTANCE_FINAL_REVIEW and same.remedy
    single = only("single")
    assert single.status == "WARN" and single.importance == IMPORTANCE_BEFORE_PRODUCTION and single.remedy
    asked = only("asked")
    assert asked.status == "WARN" and asked.importance == IMPORTANCE_BEFORE_PRODUCTION
    # CONTRACT CHANGED 2026-11-14 (practical test 6): «single» opens the Pinout table.
    assert asked.action == ACTION_ANSWER_CONTACT_NC and single.action == ACTION_EDIT_PINOUT
    manual = only("manual")
    assert manual.status == "WARN" and manual.importance == IMPORTANCE_BEFORE_PRODUCTION
    assert manual.action == ACTION_EDIT_PINOUT and "Pinout" in manual.remedy


def test_no_switch_contact_no_check():
    """@brief Negative: None or an unknown witness -> no row at all."""
    assert contact_function_checks(None) == [] and contact_function_checks("maybe") == []


def test_verify_component_reports_the_witness(tmp_path, monkeypatch):
    """@brief The check on disk carries the NC/NO row; without a witness it carries none."""
    monkeypatch.setattr(component_check, "find_kicad_cli", lambda: None)
    rows = [{"pin": "1", "name": "A"}]
    with_witness = verify_component(tmp_path, "X", rows, contact_witness="single")
    assert [r.status for r in with_witness if r.code == "contact_functions"] == ["WARN"]
    without = verify_component(tmp_path, "X", rows)
    assert not [r for r in without if r.code == "contact_functions"]


def test_nc_no_typed_by_hand_are_the_users_choice(window):
    """@brief Nit of the third review: a Pinout edit of a switch pin makes the witness «user» (OK
    «scelte dall'utente», never «lette una sola volta»); negative: an edit of a coil pin does not."""
    before = [{"pin": "1", "name": "COIL", "function": "coil"}, {"pin": "2", "name": "NC", "function": "nc"},
              {"pin": "3", "name": "NO", "function": "no"}]
    swapped = [dict(before[0]), dict(before[1], function="no", name="NO"), dict(before[2], function="nc", name="NC")]
    assert window._switch_pins_changed(before, swapped)
    assert not window._switch_pins_changed(before, [dict(before[0], name="COIL+")] + before[1:])
    (row,) = contact_function_checks("user")
    assert row.status == "OK" and row.detail == "Funzioni NC/NO scelte dall'utente."
