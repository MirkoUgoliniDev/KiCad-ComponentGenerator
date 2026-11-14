"""@brief NC/NO of a drawn switch contact: observed, read twice, asked as whole role assignments.

Night real runs of 2026-11-14: one relay drawing read in two analyses gave
the normally closed and normally open terminals swapped, and nothing checked
them (the drawn-hole witness sees geometry only). Now:
- the main answer may declare what the drawn contact shows for a terminal
  (`symbol_contact`) and the pivot it is drawn with (`contact_common`); the
  PROGRAM derives the role, and a declared function the observation
  contradicts is a contradiction inside that reading;
- the dedicated pad reading, in the call made anyway, transcribes the roles
  of a drawn circuit symbol (`terminal_functions`);
- per contact group, agreement keeps the roles with a note saying how strong
  it is; one reading says WHY there is no second; a disagreement, a
  contradiction or an impossible group asks «contact_nc» whose options are
  WHOLE role assignments, one per reading, plus «none» -- a choice writes
  only the roles it names (generality audit 2026-11-14, H1-H3, M4-M7, L8-L9).
Invented terminals, no part names. No network.
"""
from unittest.mock import Mock

from kicadgen.services import datasheet_extractor as ext


def _questions(data):
    return [q for q in data.get("_questions") or () if q["field"] == "contact_nc"]


def _data(pinout, dedicated=None, same_model=True, answers=None, reason=None):
    data = {"notes": "", "pinout": [dict(entry) for entry in pinout],
            "_question_documents": ["drawing.png"]}
    if dedicated is not None:
        data["_dedicated_functions"] = dict(dedicated)
        data["_functions_same_model"] = same_model
    if reason is not None:
        data["_dedicated_functions_reason"] = reason
    if answers is not None:
        data["_answers"] = answers
    return data


#: Coil on A and E, normally open on B, normally closed on C, common on D.
RIGHT = [{"pin": "A", "name": "Coil", "function": "coil"},
         {"pin": "B", "name": "NO", "function": "no"},
         {"pin": "C", "name": "NC", "function": "nc"},
         {"pin": "D", "name": "COM", "function": "common"},
         {"pin": "E", "name": "Coil", "function": "coil"}]
#: The same terminals with NC and NO swapped.
SWAPPED = [dict(entry, function={"no": "nc", "nc": "no"}.get(entry["function"], entry["function"]))
           for entry in RIGHT]
DEDICATED_RIGHT = {"B": ("no", "drawn_apart", None), "C": ("nc", "arm_touches", None),
                   "D": ("common", "arm_pivot", None)}


def _functions(entries):
    return {entry["pin"]: entry.get("function") for entry in entries}


def _params(data, pinout):
    return ext.ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                               pinout=[dict(entry) for entry in pinout],
                               questions=list(data["_questions"]), candidates=data["_candidates"],
                               contact_function_witness=data.get("_contact_function_witness"),
                               contact_function_groups=data.get("_contact_function_groups"))


# ── agreement and single readings ───────────────────────────────────────

def test_two_readings_of_one_model_that_agree_keep_the_roles_with_a_weak_note():
    data = _data(RIGHT, DEDICATED_RIGHT, same_model=True)
    ext._confront_contact_functions(data)
    assert not _questions(data)
    assert data["_contact_function_witness"] == "same_model" and "stesso modello" in data["notes"]


def test_two_models_that_agree_say_so():
    data = _data(RIGHT, DEDICATED_RIGHT, same_model=False)
    ext._confront_contact_functions(data)
    assert data["_contact_function_witness"] == "two_models" and "modelli diversi" in data["notes"]


def test_a_role_only_observed_is_derived_by_the_program():
    pinout = [{"pin": "B", "name": "x", "symbol_contact": "drawn_apart"},
              {"pin": "C", "name": "y", "symbol_contact": "arm_touches"},
              {"pin": "D", "name": "z", "symbol_contact": "arm_pivot"}]
    data = _data(pinout)
    ext._confront_contact_functions(data)
    assert _functions(data["pinout"]) == {"B": "no", "C": "nc", "D": "common"}
    assert "ricavata dal disegno" in data["notes"]


def test_a_part_without_switch_contacts_says_nothing():
    data = _data([{"pin": "1", "name": "A", "function": "other"},
                  {"pin": "2", "name": "K", "function": "other"}])
    ext._confront_contact_functions(data)
    assert "_contact_function_witness" not in data and not data["notes"]


# ── whole assignments (H1, H3) ──────────────────────────────────────────

def test_a_disagreement_offers_whole_assignments_and_none():
    data = _data(SWAPPED, DEDICATED_RIGHT)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    values = [o["value"] for o in question["options"]]
    assert values == ["B=nc,C=no,D=common", "B=no,C=nc,D=common", ext.CONTACT_NONE]
    assert not any(o.get("recommended") for o in question["options"])
    assert question["evidence"] == {"document": "drawing.png", "crop": None}
    assert data["_contact_function_witness"] == "asked"
    assert _functions(data["pinout"])["B"] == "nc", "until the answer the main roles stay"


def test_a_common_mislabelled_by_one_reading_is_never_rewritten_after_a_right_answer():
    """H1: the dedicated reading calls the common «no»; the user picks the main assignment."""
    dedicated = dict(DEDICATED_RIGHT, D=("no", None, None))
    data = _data(RIGHT, dedicated)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    main_value = "B=no,C=nc,D=common"
    assert main_value in [o["value"] for o in question["options"]]
    answered = ext.apply_answers(_params(data, RIGHT), {question["key"]: main_value})
    functions = _functions(answered.pinout)
    assert functions == {"A": "coil", "B": "no", "C": "nc", "D": "common", "E": "coil"}


def test_the_other_assignment_writes_only_what_it_names():
    """Negative twin of H1: choosing the dedicated claim writes its roles, the coil pins untouched."""
    dedicated = dict(DEDICATED_RIGHT, D=("no", None, None))
    data = _data(RIGHT, dedicated)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    answered = ext.apply_answers(_params(data, RIGHT), {question["key"]: "B=no,C=nc,D=no"})
    functions = _functions(answered.pinout)
    assert functions["D"] == "no" and functions["A"] == "coil" and functions["E"] == "coil"


def test_a_normally_open_only_contact_has_a_right_answer():
    """H3: SPST-NO -- the main answer says NC, the dedicated reading NO: «NO» can be chosen."""
    pinout = [{"pin": "1", "name": "COM", "function": "common"},
              {"pin": "2", "name": "S", "function": "nc"}]
    data = _data(pinout, {"1": ("common", None, None), "2": ("no", None, None)})
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    answered = ext.apply_answers(_params(data, pinout), {question["key"]: "1=common,2=no"})
    assert _functions(answered.pinout) == {"1": "common", "2": "no"}


def test_none_applies_nothing_and_leaves_the_roles_to_correct_by_hand():
    data = _data(SWAPPED, DEDICATED_RIGHT)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    answered = ext.apply_answers(_params(data, SWAPPED), {question["key"]: ext.CONTACT_NONE})
    assert _functions(answered.pinout) == _functions(SWAPPED)
    assert answered.contact_function_witness == "manual" and not answered.questions


# ── groups (H2, M5) ─────────────────────────────────────────────────────

DPDT = [{"pin": "1", "name": "C1", "function": "common", "contact_common": "1"},
        {"pin": "2", "name": "a", "function": "nc", "contact_common": "1"},
        {"pin": "3", "name": "b", "function": "no", "contact_common": "1"},
        {"pin": "4", "name": "C2", "function": "common", "contact_common": "4"},
        {"pin": "5", "name": "c", "function": "no", "contact_common": "4"},
        {"pin": "6", "name": "d", "function": "nc", "contact_common": "4"}]
DPDT_SECOND = {"1": ("common", None, "1"), "2": ("nc", None, "1"), "3": ("no", None, "1"),
               "4": ("common", None, "4"), "5": ("nc", None, "4"), "6": ("no", None, "4")}


def test_an_unlabelled_double_contact_is_grouped_by_its_drawn_pivot():
    """H2: only the second contact disagrees; the question names its three terminals only."""
    data = _data(DPDT, DPDT_SECOND)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    assert [o["value"] for o in question["options"][:2]] == ["4=common,5=no,6=nc",
                                                            "4=common,5=nc,6=no"]
    assert data["_contact_function_groups"] == {"pivot=1": "same_model", "pivot=4": "asked"}


def test_without_a_pivot_the_double_contact_is_one_coarser_group():
    """Negative of H2: no label, no pivot -> one group; the options still are whole assignments."""
    pinout = [{k: v for k, v in entry.items() if k != "contact_common"} for entry in DPDT]
    second = {pin: (function, observed, None) for pin, (function, observed, _c) in DPDT_SECOND.items()}
    data = _data(pinout, second)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    assert question["options"][0]["value"].count("=") == 6


def test_an_impossible_labelled_contact_is_asked_without_a_second_reading():
    """M5: one printed contact with two NC terminals cannot be."""
    pinout = [{"pin": "1", "name": "C", "function": "common", "contact": "K"},
              {"pin": "2", "name": "x", "function": "nc", "contact": "K"},
              {"pin": "3", "name": "y", "function": "nc", "contact": "K"}]
    data = _data(pinout)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    assert "impossibili" in question["text"]


def test_an_unknown_group_with_two_contacts_is_not_impossible():
    """Negative of M5: unlabelled, no pivot -> two commons may be two contacts."""
    pinout = [{k: v for k, v in entry.items() if k != "contact_common"} for entry in DPDT]
    data = _data(pinout)
    ext._confront_contact_functions(data)
    assert not _questions(data) and data["_contact_function_witness"] == "single"


def test_a_reading_that_contradicts_itself_is_asked_with_both_of_its_claims():
    pinout = [dict(entry) for entry in RIGHT]
    pinout[2]["symbol_contact"] = "drawn_apart"      # C declared nc, drawn apart
    pinout[1]["symbol_contact"] = "arm_touches"      # B declared no, arm touches
    data = _data(pinout)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    values = [o["value"] for o in question["options"]]
    assert "B=nc,C=no,D=common" in values and "B=no,C=nc,D=common" in values


# ── saved answers (M4) ──────────────────────────────────────────────────

def _saved_key():
    first = _data(SWAPPED, DEDICATED_RIGHT)
    ext._confront_contact_functions(first)
    return _questions(first)[0]["key"]


def test_a_saved_assignment_applies_while_a_reading_supports_it():
    key = _saved_key()
    data = _data(SWAPPED, DEDICATED_RIGHT, answers={key: "B=no,C=nc,D=common"})
    ext._confront_contact_functions(data)
    assert not _questions(data)
    assert _functions(data["pinout"])["C"] == "nc" and data["_contact_function_witness"] == "user"


def test_later_agreeing_readings_against_the_saved_assignment_ask_again():
    """Both readings now say C is NO: the saved fact is shown again beside them."""
    key = _saved_key()
    data = _data(SWAPPED, {"B": ("nc", None, None), "C": ("no", None, None),
                           "D": ("common", None, None)}, answers={key: "B=no,C=nc,D=common"})
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    labels = [o["label"] for o in question["options"]]
    assert any("scelta salvata" in label for label in labels)


def test_two_saved_assignments_that_disagree_contradict():
    assert ext.answers_contradict("contact_nc:all:B,C", "B=no,C=nc", "contact_nc:all:B,C,D",
                                  "B=nc,C=no,D=common")
    assert not ext.answers_contradict("contact_nc:all:B,C", "B=no,C=nc", "contact_nc:all:B,C,D",
                                      "B=no,C=nc,D=common")


# ── why one reading only (M7), coverage (L8), witness after an answer (L9) ──

def test_the_missing_second_reading_says_why():
    cases = {"failed": "non e' riuscita", "old_prompt": "non chiede le funzioni",
             "no_symbol": "non ha visto un simbolo", None: "non e' stata fatta"}
    for reason, words in cases.items():
        data = _data(RIGHT, {} if reason else None, reason=reason)
        ext._confront_contact_functions(data)
        assert data["_contact_function_witness"] == "single" and words in data["notes"], reason


def test_a_main_observation_without_a_symbol_in_the_second_reading_is_said_not_asked():
    pinout = [dict(entry) for entry in RIGHT]
    pinout[2]["symbol_contact"] = "arm_touches"
    data = _data(pinout, {}, reason="no_symbol")
    ext._confront_contact_functions(data)
    assert not _questions(data) and "descrive il contatto disegnato" in data["notes"]


def test_a_partly_covering_second_reading_is_single():
    data = _data(RIGHT, {"C": ("nc", None, None)})
    ext._confront_contact_functions(data)
    assert data["_contact_function_witness"] == "single" and "copre solo" in data["notes"]


def test_after_an_answer_the_witness_is_the_weakest_group():
    data = _data(DPDT, DPDT_SECOND)
    ext._confront_contact_functions(data)
    (question,) = _questions(data)
    answered = ext.apply_answers(_params(data, DPDT), {question["key"]: question["options"][1]["value"]})
    assert answered.contact_function_witness == "same_model"


def test_a_unit_discarded_pad_reading_keeps_its_roles(tmp_path):
    """M7: the roles of a drawn symbol do not depend on the unit."""
    from PIL import Image
    image = tmp_path / "d.png"
    Image.new("RGB", (40, 40), "white").save(image)
    call = Mock(return_value={"contacts_view": "unknown", "chains": [],
                              "terminal_functions": [{"pad": "2", "function": "nc"}]})
    reading = ext.read_pad_positions([image], model="m", call=call)
    assert reading.get("discarded") and reading["functions"] == {"2": ("nc", None, None)}


def test_the_pad_reading_s_roles_are_read_and_bad_values_ignored():
    found = ext.printed_functions([{"pad": "2", "function": "nc", "symbol_contact": "arm_touches",
                                    "contact_common": "4"},
                                   {"pad": "3", "function": "maybe"},
                                   {"pad": "", "function": "no"}, "junk"])
    assert found == {"2": ("nc", "arm_touches", "4")}
    assert ext.printed_functions(None) == {}


def test_the_schemas_carry_the_observation_and_convert_strictly():
    from kicadgen.services.openrouter import strict_tool
    item = ext._EXTRACTION_TOOL["input_schema"]["properties"]["pinout"]["items"]["properties"]
    assert item["symbol_contact"]["enum"] == list(ext.SYMBOL_CONTACT_ROLES)
    assert "set or energised state" in item["symbol_contact"]["description"]
    assert "contact_common" in item
    pad = ext._PAD_POSITIONS_TOOL["input_schema"]["properties"]
    assert "contact_common" in pad["terminal_functions"]["items"]["properties"]
    assert "terminal_functions" not in ext._PAD_POSITIONS_TOOL["input_schema"]["required"]
    assert strict_tool(ext._pad_positions_tool()) is not None
    assert "terminal_functions" in ext.DEFAULT_PAD_POSITIONS_PROMPT
    assert "symbol_contact" in ext.DEFAULT_SINGLE_SYSTEM_PROMPT
