"""@brief Defects of the night4 real runs (2026-11-14): pin names after a role answer, hole or terminal.

- G1: the answer to «which roles are the drawing's» changed the pins'
  functions and left the NAMES «NC»/«NO» of the main answer on the wrong
  pins: the symbol showed a pin named NC whose function was NO. A name that
  is only the word of the old role now follows the new role in the same
  style; any other name stays; a self-check names what still contradicts.
- G2: the dedicated reading filed a printed diameter as the HOLE while the
  main answer read the same value as the TERMINAL; the program only noted
  that the terminal does not pass the hole. Now the user is asked whether
  that printed diameter is the hole or the terminal.
Invented parts, no part names. No network.
"""
from kicadgen.services import datasheet_extractor as ext


# ── G1: names that are only a role word follow the chosen role ──────────

def test_role_word_names_follow_the_answer_in_their_own_style():
    entries = [{"pin": "2", "name": "NC", "function": "nc"},
               {"pin": "3", "name": "n.o.", "function": "no"},
               {"pin": "4", "name": "Common", "function": "common"}]
    ext._apply_contact_assignment(entries, {"2": "no", "3": "nc", "4": "common"})
    assert [(e["name"], e["function"]) for e in entries] == [
        ("NO", "no"), ("n.c.", "nc"), ("Common", "common")]
    assert not ext.pin_role_conflicts(entries)


def test_other_names_never_change():
    """Negative: a signal name, a labelled contact and an unchanged role keep their names."""
    entries = [{"pin": "2", "name": "NO1", "function": "nc"},
               {"pin": "3", "name": "OUT_A", "function": "no"},
               {"pin": "4", "name": "COM", "function": "common"}]
    ext._apply_contact_assignment(entries, {"2": "no", "3": "nc", "4": "common"})
    assert [e["name"] for e in entries] == ["NO1", "OUT_A", "COM"]


def test_a_name_that_does_not_spell_the_old_role_is_not_rewritten():
    """Negative: the name already says another role than the old function -> left, reported."""
    entries = [{"pin": "2", "name": "NO", "function": "nc"}]
    ext._apply_contact_assignment(entries, {"2": "common"})
    assert entries[0]["name"] == "NO" and entries[0]["function"] == "common"
    assert ext.pin_role_conflicts(entries) == [{"pin": "2", "name": "NO", "function": "common"}]


def test_the_answer_through_apply_answers_renames_and_checks():
    pinout = [{"pin": "1", "name": "COM", "function": "common"},
              {"pin": "2", "name": "NC", "function": "nc"},
              {"pin": "3", "name": "NO", "function": "no"}]
    data = {"notes": "", "pinout": [dict(e) for e in pinout], "_question_documents": ["d.png"],
            "_dedicated_functions": {"1": ("common", None, None), "2": ("no", None, None),
                                     "3": ("nc", None, None)},
            "_functions_same_model": True}
    ext._confront_contact_functions(data)
    (question,) = [q for q in data["_questions"] if q["field"] == "contact_nc"]
    params = ext.ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                                 pinout=[dict(e) for e in pinout],
                                 questions=list(data["_questions"]), candidates=data["_candidates"],
                                 contact_function_groups=data["_contact_function_groups"])
    answered = ext.apply_answers(params, {question["key"]: "1=common,2=no,3=nc"})
    assert [(e["pin"], e["name"], e["function"]) for e in answered.pinout] == [
        ("1", "COM", "common"), ("2", "NO", "no"), ("3", "NC", "nc")]
    assert answered.pin_role_conflicts is None


def test_a_main_answer_whose_names_contradict_its_functions_is_reported():
    """The self-check also runs on the analysis result."""
    data = {"notes": "", "pinout": [{"pin": "1", "name": "COM", "function": "common"},
                                    {"pin": "2", "name": "NC", "function": "no"}]}
    ext._confront_contact_functions(data)
    assert "ATTENZIONE pin 2" in data["notes"]


# ── G2: a printed diameter read as the hole that the terminal does not pass ──

def _pins(drill_source="read", lead=0.8, drill=0.8):
    return [{"number": str(n), "pad_type": "thru_hole", "drill_mm": drill, "x_mm": 2.54 * n,
             "y_mm": 0.0, "pad_length_mm": 1.6, "pad_width_mm": 1.6, "shape": "circle",
             "sources": {"drill_mm": drill_source, "pad_size": "estimated"},
             "lead": {"shape": "round", "diameter_mm": lead}} for n in (1, 2)]


def _judge(pins, answers=None):
    data = {"notes": "", "_question_documents": ["d.png"]}
    if answers is not None:
        data["_answers"] = answers
    found = {"lead_contradicted": [], "lead_lifted": [], "widened": [], "loose_blade": [],
             "part_narrow": [], "part_protected": [], "part_lifted": [], "part_widened": []}
    derived, notes = {}, []
    ext._judge_holes_against_leads(pins, None, None, found, derived)
    ext._ask_hole_or_terminal(data, found, derived, notes)
    return data, found, notes


def test_a_hole_equal_to_its_terminal_asks_hole_or_terminal():
    pins = _pins()
    data, found, _notes = _judge(pins)
    (question,) = [q for q in data["_questions"] if q["field"] == "pad_layout"]
    assert "0.80" in question["text"] and "foro o il terminale" in question["text"]
    assert not any(o.get("recommended") for o in question["options"])
    assert [o["value"].rsplit("=", 1)[1] for o in question["options"]] == ["hole", "terminal"]
    # Until the answer the hole stays, marked estimated (the question blocks writes).
    assert pins[0]["drill_mm"] == 0.8 and pins[0]["sources"]["drill_mm"] == "estimated"


def test_a_hole_wider_than_its_terminal_asks_nothing():
    """Negative: 1.0 mm hole, 0.6 mm terminal -> it passes, no question."""
    data, found, _notes = _judge(_pins(lead=0.6, drill=1.0))
    assert not data.get("_questions") and not found["lead_contradicted"]


def test_saved_terminal_derives_the_hole_and_saved_hole_keeps_it_read():
    first, _found, _notes = _judge(_pins())
    (question,) = first["_questions"]
    terminal = next(o["value"] for o in question["options"] if o["value"].endswith("=terminal"))
    hole = next(o["value"] for o in question["options"] if o["value"].endswith("=hole"))
    pins = _pins()
    data, found, _notes = _judge(pins, answers={question["key"]: terminal})
    assert not data.get("_questions") and pins[0]["drill_mm"] > 0.8
    assert pins[0]["sources"]["drill_mm"] == "estimated" and found["lead_lifted"]
    pins = _pins()
    data, found, notes = _judge(pins, answers={question["key"]: hole})
    assert pins[0]["drill_mm"] == 0.8 and pins[0]["sources"]["drill_mm"] == "read"
    assert not found["lead_contradicted"] and "scelto dall'utente" in notes[0]


def test_apply_answers_writes_the_terminal_choice_on_the_result():
    pins = _pins()
    data, _found, _notes = _judge(pins)
    (question,) = data["_questions"]
    terminal = next(o["value"] for o in question["options"] if o["value"].endswith("=terminal"))
    geometry = {"origin": "custom", "pins": [dict(p, lead=None) for p in pins]}
    params = ext.ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                                 package_geometry=geometry, questions=list(data["_questions"]),
                                 candidates=data["_candidates"])
    answered = ext.apply_answers(params, {question["key"]: terminal})
    assert answered.package_geometry["pins"][0]["drill_mm"] > 0.8
    assert not answered.questions and "terminale, scelto dall'utente" in answered.notes


def test_the_circuit_symbol_sentence_speaks_of_pin_roles_only():
    """The pad prompt's symbol sentence must not invite re-filing hole or terminal callouts."""
    sentence = ext.DEFAULT_PAD_POSITIONS_PROMPT.split("CIRCUIT SYMBOL")[1].split("Write any note")[0]
    assert "pin roles only" in sentence and "changes nothing above about holes" in sentence
