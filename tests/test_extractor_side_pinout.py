"""Side numbering against the document pinout in the extraction (docs/pinout-coherence-plan.md, item B).

A castellated answer whose sides and pinout are not one numbering gets ONE
corrective retry with a structural text; a second contradictory answer is
kept and the doubt becomes the question «side_numbering». Offline: the main
call is faked, nothing reaches the network. Two naming conventions so the
behaviour is not tied to one board.
"""
import copy
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as de
from kicadgen.services import pinout_coherence as pc
from minimal_pdf import MINIMAL_PDF

from test_pinout_coherence import (CONVENTIONS, EP_LABELS, EP_PINOUT, EP_PINOUT_DECLARED,
                                   RECOMMENDED)


@pytest.fixture
def offline(monkeypatch):
    """No key, no credit check, no network."""
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kw: None)


def _answer(convention, top_first, bottom_first, top_reverse=None, bottom_reverse=None):
    """A complete castellated answer of `convention` with the given numbering."""
    pinout, top, bottom, expected = CONVENTIONS[convention]
    top_reverse = expected["top"][1] if top_reverse is None else top_reverse
    bottom_reverse = expected["bottom"][1] if bottom_reverse is None else bottom_reverse

    def side(edge, names, first, reverse):
        return {"edge": edge, "count": len(names), "pitch_mm": 2.54, "first_pin": first,
                "reverse": reverse, "pin_names": list(names), "contact_style": "through_hole",
                "through_hole_drill_mm": 1.0}
    return {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 30.0,
            "body_width_mm": 20.0, "confidence": "medium", "notes": "",
            "castellated_sides": [side("top", top, top_first, top_reverse),
                                  side("bottom", bottom, bottom_first, bottom_reverse)],
            "pinout": copy.deepcopy(pinout)}


def _correct(convention):
    expected = CONVENTIONS[convention][3]
    return _answer(convention, expected["top"][0], expected["bottom"][0])


def _shifted(convention):
    """Rule 1 broken: the top side numbered after the end of the pinout."""
    return _answer(convention, 12, 1, CONVENTIONS[convention][3]["bottom"][1],
                   CONVENTIONS[convention][3]["top"][1])


def _run(tmp_path, monkeypatch, answers_seq, answers=None):
    source = tmp_path / "board.pdf"
    source.write_bytes(MINIMAL_PDF)
    call = Mock(side_effect=[copy.deepcopy(a) if isinstance(a, dict) else a for a in answers_seq])
    monkeypatch.setattr(de, "call_tool", call)
    lines = []
    params = de.extract_pin_header_params([source], progress=lines.append, answers=answers)
    return params, call, lines


def _side_questions(params):
    return [q for q in params.questions or () if q["field"] == "side_numbering"]


def _numbering(params):
    return {side["edge"]: (side["first_pin"], bool(side.get("reverse")))
            for side in params.castellated_sides}


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_a_coherent_retry_replaces_the_contradictory_answer(offline, tmp_path, monkeypatch, convention):
    params, call, lines = _run(tmp_path, monkeypatch, [_shifted(convention), _correct(convention)])
    assert call.call_count == 2
    first, second = (c.kwargs for c in call.call_args_list)
    assert second["content_blocks"][:-1] == first["content_blocks"]
    # Round 5: the shifted labels also contradict the mapping strongly, so the
    # ONE retry carries both structural texts.
    assert second["content_blocks"][-1] == {"type": "text", "text": (
        de._SIDE_PINOUT_CORRECTION + "\n\n" + de._SIDE_LABEL_CORRECTION)}
    assert not _side_questions(params)
    assert _numbering(params) == CONVENTIONS[convention][3]


def test_the_correction_names_the_rule_and_no_value():
    text = de._SIDE_PINOUT_CORRECTION
    assert "first_pin" in text and "reverse" in text and "pinout" in text
    assert not any(ch.isdigit() for ch in text)


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_two_contradictory_answers_keep_the_newer_and_ask(offline, tmp_path, monkeypatch, convention):
    newer = _shifted(convention)
    newer["notes"] = "second answer"
    params, call, lines = _run(tmp_path, monkeypatch, [_shifted(convention), newer])
    assert call.call_count == 2, "one corrective retry only"
    assert "second answer" in params.notes
    (question,) = _side_questions(params)
    assert question["key"].startswith("side_numbering:")
    values = [option["value"] for option in question["options"]]
    labels = {option["value"]: option["label"] for option in question["options"]}
    # CONTRACT CHANGED 2026-11-13 (generality audit H3/L1): the devkit's bare
    # numbers carry no evidence, so its renumbering is offered after
    # «keep_sides», «da verificare», with the competing windows named.
    # CONTRACT CHANGED 2026-11-13 (round 4, audit H1): «off_side_entries»
    # is always offered for undeclared entries without a pad.
    if RECOMMENDED[convention]:
        assert values == ["renumber", "off_side_entries", "keep_sides"]
        assert "consigliata" in labels["renumber"]
        # The support is shown by kind, never a threshold.
        assert "6 etichette su 6" in labels["renumber"]
    else:
        assert values == ["off_side_entries", "keep_sides", "renumber"]
        assert "da verificare" in labels["renumber"] and "consigliata" not in labels["renumber"]
        assert "altre 1 collocazioni" in labels["renumber"]
    applied = de.apply_answers(params, {question["key"]: "renumber"})
    assert _numbering(applied) == CONVENTIONS[convention][3]
    report = pc.side_pinout_report(applied.castellated_sides, applied.pinout)
    assert not report["dropped"] and not report["unnamed_pads"]
    assert len(applied.pinout) == 12, "no pinout entry dropped"
    assert not _side_questions(applied)


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_a_coherent_first_answer_costs_no_retry_and_asks_nothing(offline, tmp_path, monkeypatch,
                                                                 convention):
    params, call, _lines = _run(tmp_path, monkeypatch, [_correct(convention)])
    assert call.call_count == 1
    assert not _side_questions(params)


@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_labels_that_differ_are_noted_never_asked(offline, tmp_path, monkeypatch, convention):
    """Rule 1 holds (numbers cover the pinout), only the labels see the swap.

    CONTRACT CHANGED 2026-11-13 (round 3, item 1): a name difference alone
    never opens the blocking question; it is a «verifica» note and a finding.
    CONTRACT CHANGED 2026-11-13 (round 5): the swapped sides' labels agree on
    two or more unique names elsewhere -> ONE corrective retry; the retry
    answering the same, the names stay as the analysis gave them.
    """
    expected = CONVENTIONS[convention][3]
    swapped = _answer(convention, expected["bottom"][0], expected["top"][0])
    params, call, _lines = _run(tmp_path, monkeypatch, [swapped, swapped])
    assert call.call_count == 2
    assert call.call_args_list[1].kwargs["content_blocks"][-1]["text"].endswith(
        de._SIDE_LABEL_CORRECTION)
    assert _numbering(params) == {"top": (expected["bottom"][0], expected["top"][1]),
                                  "bottom": (expected["top"][0], expected["bottom"][1])}
    assert "da rivedere prima dell'uso" in params.notes
    assert not _side_questions(params)
    assert "Etichette dei lati diverse dal nome del pinout" in params.notes
    report = pc.side_pinout_report(params.castellated_sides, params.pinout)
    assert any(f["kind"] == pc.FINDING_LABEL_DIFFERS for f in pc.document_findings(report))


def test_a_failed_retry_keeps_the_first_answer(offline, tmp_path, monkeypatch):
    params, call, lines = _run(tmp_path, monkeypatch,
                               [_shifted("devkit"), de.OpenRouterError("network down")])
    assert call.call_count == 2
    assert _side_questions(params)
    assert any("si tiene la prima risposta" in line for line in lines)


def test_a_saved_answer_is_applied_during_the_extraction(offline, tmp_path, monkeypatch):
    params, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("nano"), _shifted("nano")])
    key = _side_questions(params)[0]["key"]
    again, call, _lines = _run(tmp_path, monkeypatch, [_shifted("nano"), _shifted("nano")],
                               answers={key: "renumber"})
    assert not _side_questions(again)
    assert _numbering(again) == CONVENTIONS["nano"][3]
    assert "Contatti rinumerati come il pinout" in again.notes


def test_keep_sides_makes_the_pinout_the_side_labels(offline, tmp_path, monkeypatch):
    params, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("devkit"), _shifted("devkit")])
    key = _side_questions(params)[0]["key"]
    applied = de.apply_answers(params, {key: "keep_sides"})
    assert _numbering(applied) == _numbering(params), "the sides' numbering stays"
    labels = {row["pin"]: row["name"] for row in applied.pinout}
    for side in applied.castellated_sides:
        for row in pc.side_pad_labels(side):
            assert labels[str(row["pin"])] == row["name"]
    assert "Numerazione dei lati tenuta" in applied.notes


def test_a_changed_reading_leaves_the_question_open(offline, tmp_path, monkeypatch):
    """The candidate's pad-label pairs no longer match: nothing is written."""
    params, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("devkit"), _shifted("devkit")])
    key = _side_questions(params)[0]["key"]
    params.castellated_sides[0]["pin_names"] = list(reversed(params.castellated_sides[0]["pin_names"]))
    applied = de.apply_answers(params, {key: "keep_sides"})
    assert _side_questions(applied)
    assert "non applicabile" in applied.notes


def test_an_ambiguous_side_offers_only_keep_sides(offline, tmp_path, monkeypatch):
    """No unique placement: renumbering is not defined, never offered."""
    answer = _shifted("devkit")
    answer["castellated_sides"][0]["pin_names"] = ["Q"] * 6
    params, _call, _lines = _run(tmp_path, monkeypatch, [answer, copy.deepcopy(answer)])
    (question,) = _side_questions(params)
    assert [option["value"] for option in question["options"]] == ["off_side_entries",
                                                                   "keep_sides"]


def test_side_numbering_is_a_question_field():
    assert "side_numbering" in de.QUESTION_FIELDS


def test_a_saved_answer_applied_by_the_extractor_is_reported(offline, tmp_path, monkeypatch):
    """Positive: a saved «keep_sides» re-applied silently is listed in `answered_questions`
    with its exact key and value (the window needs it, 2026-11-13). Negative: a fresh
    analysis without saved answers reports nothing and keeps the question open."""
    fresh, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("devkit"), _shifted("devkit")])
    (question,) = _side_questions(fresh)
    assert fresh.answered_questions is None
    again, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("devkit"), _shifted("devkit")],
                                answers={question["key"]: "keep_sides", "side_numbering:other": "renumber"})
    assert not _side_questions(again)
    assert again.answered_questions == {question["key"]: "keep_sides"}


def _ep_answer(first_pin, pinout=EP_PINOUT_DECLARED):
    """A castellated module with four edge contacts and a numbered centre pad in its pinout."""
    return {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 20.0,
            "body_width_mm": 20.0, "confidence": "medium", "notes": "",
            "castellated_sides": [{"edge": "left", "count": 4, "pitch_mm": 2.54,
                                   "first_pin": first_pin, "pin_names": list(EP_LABELS),
                                   "contact_style": "half_hole"}],
            "pinout": copy.deepcopy(pinout)}


def test_a_centre_pad_in_the_pinout_costs_no_retry_and_is_kept(offline, tmp_path, monkeypatch):
    """H2 positive: sides right, pinout entry 5 DECLARED off the sides -> one call, no question."""
    params, call, _lines = _run(tmp_path, monkeypatch, [_ep_answer(1)])
    assert call.call_count == 1
    assert not _side_questions(params)
    entry = next(e for e in params.pinout if str(e["pin"]) == "5")
    assert entry.get("off_side") is True


def test_an_undeclared_centre_pad_asks_without_a_paid_retry(offline, tmp_path, monkeypatch):
    """Item 2: the same answer without the declaration -> a free question, never silence."""
    params, call, _lines = _run(tmp_path, monkeypatch, [_ep_answer(1, EP_PINOUT)])
    assert call.call_count == 1, "no paid retry for an unexplained entry"
    (question,) = _side_questions(params)
    values = [option["value"] for option in question["options"]]
    assert "off_side_entries" in values and "keep_sides" not in values
    applied = de.apply_answers(params, {question["key"]: "off_side_entries"})
    assert next(e for e in applied.pinout if str(e["pin"]) == "5").get("off_side") is True
    assert not _side_questions(applied)


def test_the_shifted_centre_pad_module_retries_and_asks(offline, tmp_path, monkeypatch):
    """H2 negative: the same module numbered from 6 -> one retry, then the question."""
    params, call, _lines = _run(tmp_path, monkeypatch, [_ep_answer(6), _ep_answer(6)])
    assert call.call_count == 2
    (question,) = _side_questions(params)
    assert "renumber" in [option["value"] for option in question["options"]]
    applied = de.apply_answers(params, {question["key"]: "keep_sides"})
    # The user's choice on a shifted reading: the shifted entries left out are
    # named; the declared centre pad 5 stays (round 3, item 2).
    assert "1-4" in applied.notes
    assert next(e for e in applied.pinout if str(e["pin"]) == "5").get("off_side") is True


def test_the_correction_never_orders_removing_a_pinout_entry():
    text = de._SIDE_PINOUT_CORRECTION
    assert "never remove, renumber or move a pinout entry" in text
    assert "every pinout entry is exactly one contact" not in text


# --- declared «recommended» flag (the window highlights only this, never the wording) ----------

@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_only_a_recommended_renumbering_carries_the_flag(offline, tmp_path, monkeypatch, convention):
    params, _call, _lines = _run(tmp_path, monkeypatch, [_shifted(convention), _shifted(convention)])
    (question,) = _side_questions(params)
    flagged = [option["value"] for option in question["options"] if option.get("recommended")]
    assert flagged == (["renumber"] if RECOMMENDED[convention] else [])
    # The flag does not change the key: a saved answer still applies.
    assert question["key"] == de.question_key("side_numbering", [question["key"].split(":", 1)[1]])


def test_the_sole_hole_confirmed_layout_is_flagged():
    """CONTRACT CHANGED 2026-11-13 (audit L4): the flag needs the confirming verdict itself;
    a rank without a verdict is not enough."""
    options = [{"label": "a", "value": "a"}, {"label": "b", "value": "b"}]
    de._flag_sole_confirmed(options, {"a": 0, "b": 1}, {"a": (_Verdict(), {"1": (0.0, 0.0)})})
    assert options[0].get("recommended") is True and "recommended" not in options[1]
    bare = [{"label": "a", "value": "a"}, {"label": "b", "value": "b"}]
    de._flag_sole_confirmed(bare, {"a": 0, "b": 1})
    assert not any("recommended" in option for option in bare)


@pytest.mark.parametrize("ranks,count", [
    ({"a": 0, "b": 0}, 2),     # two confirmed: the holes single out none
    ({"a": 1, "b": 2}, 2),     # none confirmed
    ({"a": 0}, 1),             # one option: a confirmation, not a choice
])
def test_no_flag_without_a_single_confirmed_choice(ranks, count):
    options = [{"label": key, "value": key} for key in list(ranks)[:count]]
    de._flag_sole_confirmed(options, ranks)
    assert not any("recommended" in option for option in options)



# --- round 3 ------------------------------------------------------------------------------------

def _probe4_answer(**extra):
    """Six side contacts + entry 7 the exposed pad; the model numbered the sides 2..7."""
    pinout = [{"pin": str(n), "name": name} for n, name in
              enumerate(["GND", "3V3", "EN", "IO4", "IO5", "IO6", "GND"], start=1)]
    side = {"pitch_mm": 2.54, "contact_style": "half_hole"}
    return {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 20.0,
            "body_width_mm": 20.0, "confidence": "medium", "notes": "",
            "castellated_sides": [dict(side, edge="left", count=3, first_pin=2,
                                       pin_names=["1", "2", "3"]),
                                  dict(side, edge="right", count=3, first_pin=5, reverse=True,
                                       pin_names=["4", "5", "6"])],
            "pinout": pinout, **extra}


def test_a_shift_behind_an_exposed_pad_is_asked_with_the_shift(offline, tmp_path, monkeypatch):
    params, call, _lines = _run(tmp_path, monkeypatch, [_probe4_answer()])
    assert call.call_count == 1
    (question,) = _side_questions(params)
    labels = {option["value"]: option["label"] for option in question["options"]}
    assert set(labels) == {"shift:-1", "off_side_entries"}
    assert "1=GND" in labels["shift:-1"]
    applied = de.apply_answers(params, {question["key"]: "shift:-1"})
    pads = sorted(n for side in applied.castellated_sides for n in pc.side_pad_numbers(side))
    assert pads == [1, 2, 3, 4, 5, 6]
    assert next(e for e in applied.pinout if str(e["pin"]) == "7").get("off_side") is True


def test_name_differences_on_an_aligned_numbering_open_no_question():
    """Probe6 negative: GND1/GND2 against two «GND» labels, numbering right."""
    data = {"layout": "castellated",
            "castellated_sides": [{"edge": "left", "count": 4, "first_pin": 1, "pitch_mm": 2.54,
                                   "pin_names": ["GND", "IO1", "IO2", "GND"]}],
            "pinout": [{"pin": str(i), "name": n} for i, n in
                       enumerate(["GND1", "GPIO1 (ADC1, TOUCH1)", "GPIO2", "GND2"], 1)]}
    de._ask_side_numbering(data)
    assert not data.get("_questions")


def test_printed_numbers_keep_the_side_numbering_question(offline, tmp_path, monkeypatch):
    """Item 3 negative: pin_numbers_printed True -> retry and «side_numbering», no convention."""
    answer = _shifted("nano")
    answer["pin_numbers_printed"] = True
    params, call, _lines = _run(tmp_path, monkeypatch, [answer, answer])
    assert call.call_count == 2
    assert _side_questions(params)
    assert params.pin_numbers_printed is True
    assert "numerazione convenzionale" not in params.notes


def test_the_schema_declares_numbers_printed_and_off_side_entries():
    properties = de._openai_tool()["function"]["parameters"]["properties"]
    assert properties["pin_numbers_printed"]["type"] == "boolean"
    # L1: required, so the model must say it; strict mode keeps it nullable.
    assert "pin_numbers_printed" in de._openai_tool()["function"]["parameters"]["required"]
    from kicadgen.services.openrouter import strict_tool
    assert strict_tool(de._openai_tool()) is not None
    assert properties["pinout"]["items"]["properties"]["off_side"]["type"] == "boolean"
    assert "off_side" in de._SIDE_PINOUT_CORRECTION and "pin_numbers_printed" in de._SIDE_PINOUT_CORRECTION


class _Verdict:
    """A drawn-hole verdict stand-in: status, source and centre-line marks used."""

    def __init__(self, source="drawing", marks_used=0):
        self.status, self.source, self.marks_used = "confirmed", source, marks_used


@pytest.mark.parametrize("verdict,flagged", [
    (_Verdict(), True),                              # drawn holes
    (_Verdict(marks_used=2), False),                 # centre-line marks only
    (_Verdict(source="library"), False),             # KiCad library footprint only
    (_Verdict(marks_used=1), True),                  # some marks, some holes
    (None, False),                                   # L4: no verdict
    (de._HoleFailure("OSError"), False),             # L4: a hole check that could not run
])
def test_a_confirmation_on_marks_or_library_only_is_not_flagged(verdict, flagged):
    options = [{"label": "a", "value": "a"}, {"label": "b", "value": "b"}]
    pads = {"1": (0.0, 0.0), "2": (2.54, 0.0)}
    de._flag_sole_confirmed(options, {"a": 0, "b": 1}, {"a": (verdict, pads)})
    assert bool(options[0].get("recommended")) is flagged



# --- round 4: no printed numbers -> the fixed convention, no question (user, 2026-11-13) ----------

def test_no_printed_numbers_applies_the_convention_without_a_question(offline, tmp_path, monkeypatch):
    """A shifted-looking numbering of a document without numbers: no retry, no question."""
    answer = _shifted("devkit")
    answer["pin_numbers_printed"] = False
    before = {}
    for side in answer["castellated_sides"]:
        for index, number in enumerate(pc.side_pad_numbers(side)):
            before[(side["edge"], index)] = number
    names = {str(entry["pin"]): entry["name"] for entry in answer["pinout"]}
    params, call, _lines = _run(tmp_path, monkeypatch, [answer, answer])
    # No shift retry (nothing printed to correct against); round 5: the labels
    # contradict the mapping strongly, which earns the one corrective retry.
    assert call.call_count == 2
    assert not [q for q in params.questions if q["field"] in ("side_numbering",
                                                              "numbering_convention")]
    assert ("numerazione convenzionale (primo contatto incontrato girando in senso antiorario "
            "dall'angolo in alto a sinistra)") in params.notes
    # Two horizontal rows: pin 1 is the bottom-left contact.
    assert _numbering(params) == {"bottom": (1, False), "top": (7, True)}
    after = {str(row["pin"]): row["name"] for row in params.pinout}
    for side in params.castellated_sides:
        for index, number in enumerate(pc.side_pad_numbers(side)):
            old = before[(side["edge"], index)]
            if str(old) in names:
                # The name the ANALYSIS gave this physical contact, under its new number.
                assert after[str(number)] == names[str(old)]


def test_an_old_saved_convention_answer_is_ignored_with_a_note(offline, tmp_path, monkeypatch):
    params, _call, _lines = _run(tmp_path, monkeypatch, [_correct("nano")])
    params.questions = [{"key": "numbering_convention:abc", "field": "numbering_convention",
                         "text": "?", "options": [{"label": "x", "value": "same:top:b"}]}]
    applied = de.apply_answers(params, {"numbering_convention:abc": "same:top:b"})
    assert not applied.questions
    assert _numbering(applied) == _numbering(params)
    assert "ignorata" in applied.notes


def test_no_printed_numbers_never_asks_for_a_shift(offline, tmp_path, monkeypatch):
    """Negative of probe4: the same answer declared without printed numbers -> convention."""
    params, call, _lines = _run(tmp_path, monkeypatch, [_probe4_answer(pin_numbers_printed=False)])
    assert call.call_count == 1 and not _side_questions(params)
    assert "Voci del pinout senza contatto nell'analisi" in params.notes



# --- round 5: strong label contradiction -> one retry ---------------------------------------------

#: The sides and pinout of the analysis of 2026-11-13 12:27 (ESP32-S3 devkit,
#: deepseek-flash): the model put pinout 1..22 on the bottom contacts, whose
#: silkscreen reads G, G, 19, 20..., and numbered the top side 44..65.
ESP32_1227_SIDES = [
    {"edge": "top", "count": 22, "first_pin": 44, "reverse": True, "pitch_mm": 2.54,
     "contact_style": "through_hole", "through_hole_drill_mm": 1.0,
     "pin_names": ["G", "5V", "14", "13", "12", "11", "10", "9", "46", "3", "8", "18", "17", "16",
                   "15", "7", "6", "5", "4", "RST", "3V3", "3V3"]},
    {"edge": "bottom", "count": 22, "first_pin": 1, "reverse": False, "pitch_mm": 2.54,
     "contact_style": "through_hole", "through_hole_drill_mm": 1.0,
     "pin_names": ["G", "G", "19", "20", "21", "47", "48", "45", "0", "35", "36", "37", "38", "39",
                   "40", "41", "42", "2", "1", "RX", "TX", "G"]}]
ESP32_1227_NAMES = [
    "3V3", "3V3", "RST", "GPIO4 (ADC1_3)", "GPIO5", "GPIO6", "GPIO7", "GPIO15", "GPIO16", "GPIO17",
    "GPIO18", "GPIO8", "GPIO3", "GPIO46", "GPIO9", "GPIO10", "GPIO11", "GPIO12", "GPIO13", "GPIO14",
    "5V0", "GND", "GND", "TX (GPIO43)", "RX (GPIO44)", "GPIO1", "GPIO2", "GPIO42", "GPIO41",
    "GPIO40", "GPIO39", "GPIO38", "GPIO37", "GPIO36", "GPIO35", "GPIO0 (BOOT)", "GPIO45", "GPIO48",
    "GPIO47", "GPIO21", "USB_D+ (GPIO20)", "USB_D- (GPIO19)", "GND", "GND"]


def _esp32_1227(top=(44, True), bottom=(1, False), **extra):
    sides = copy.deepcopy(ESP32_1227_SIDES)
    sides[0]["first_pin"], sides[0]["reverse"] = top
    sides[1]["first_pin"], sides[1]["reverse"] = bottom
    return {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 63.3,
            "body_width_mm": 25.4, "contacts_view": "top", "confidence": "medium", "notes": "",
            "castellated_sides": sides,
            "pinout": [{"pin": str(n), "name": name}
                       for n, name in enumerate(ESP32_1227_NAMES, start=1)], **extra}


@pytest.mark.parametrize("printed", [None, False])
def test_the_1227_answer_earns_one_retry_and_a_right_retry_is_used(offline, tmp_path, monkeypatch,
                                                                   printed):
    """The real case: strong label conflicts on both sides -> one retry, in both modes; the
    retry with the labels' mapping (top 1..22, bottom 23..44, both right to left) is used."""
    extra = {} if printed is None else {"pin_numbers_printed": printed}
    right = _esp32_1227(top=(1, True), bottom=(23, True), **extra)
    params, call, _lines = _run(tmp_path, monkeypatch, [_esp32_1227(**extra), right])
    assert call.call_count == 2
    assert de._SIDE_LABEL_CORRECTION in call.call_args_list[1].kwargs["content_blocks"][-1]["text"]
    report = pc.side_pinout_report(params.castellated_sides, params.pinout)
    assert not report["label_conflicts"]
    names = {str(row["pin"]): row["name"] for row in params.pinout}
    # Each silkscreen label sits on its own name, wherever the numbering puts it.
    for side in params.castellated_sides:
        for row in pc.side_pad_labels(side):
            assert pc.label_relation(row["name"], names[str(row["pin"])]) != pc.DIFFERENT


def test_a_strong_conflict_that_persists_is_a_finding_never_a_move(offline, tmp_path, monkeypatch):
    """The retry answers the same: the analysis mapping stays, the notes and the finding say it."""
    answer = _esp32_1227(pin_numbers_printed=False)
    params, call, _lines = _run(tmp_path, monkeypatch, [answer, answer])
    assert call.call_count == 2, "one retry only"
    assert "da rivedere prima dell'uso" in params.notes
    report = pc.side_pinout_report(params.castellated_sides, params.pinout)
    # The top side's labels still place its names elsewhere. The bottom side's
    # window is no longer complete after the convention numbered the entries
    # the analysis attached to no contact after the contacts: its wrong names
    # stay visible as label differences (G against 3V3).
    assert "top" in {item["edge"] for item in report["label_conflicts"]}
    assert any(item["edge"] == "bottom" for item in report["contradictions"])
    assert any(f["kind"] == pc.FINDING_LABEL_PLACEMENT for f in pc.document_findings(report))


@pytest.mark.parametrize("printed", [None, False])
def test_one_agreeing_label_never_costs_a_retry(offline, tmp_path, monkeypatch, printed):
    """P5 negative: one unique-name label elsewhere is never evidence enough."""
    pinout = [{"pin": str(i), "name": n} for i, n in
              enumerate(["RST", "EN", "IO3", "IO4", "IO5", "TX"], 1)]
    side = {"pitch_mm": 2.54, "contact_style": "half_hole"}
    answer = {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 20.0,
              "body_width_mm": 20.0, "confidence": "medium", "notes": "", "pinout": pinout,
              "castellated_sides": [dict(side, edge="left", count=3, first_pin=1,
                                         pin_names=["", "", ""]),
                                    dict(side, edge="right", count=3, first_pin=4, reverse=True,
                                         pin_names=["", "", "IO5"])]}
    if printed is not None:
        answer["pin_numbers_printed"] = printed
    params, call, _lines = _run(tmp_path, monkeypatch, [answer])
    assert call.call_count == 1
    assert "da rivedere prima dell'uso" not in params.notes



# --- round 6: final audit -------------------------------------------------------------------------

def test_a_persisting_conflict_covers_every_side_in_the_settled_numbering(offline, tmp_path,
                                                                         monkeypatch):
    """M3 (p4): computed on the analysis' mapping before the convention, translated contact by
    contact; exposed in `ExtractedParams.label_conflicts` for the window."""
    answer = _esp32_1227(pin_numbers_printed=False)
    params, call, _lines = _run(tmp_path, monkeypatch, [answer, answer])
    assert call.call_count == 2
    by_edge = {item["edge"]: item["pins"] for item in params.label_conflicts}
    assert set(by_edge) == {"top", "bottom"}
    settled = {side["edge"]: {str(n) for n in pc.side_pad_numbers(side)}
               for side in params.castellated_sides}
    assert set(by_edge["top"]) <= settled["top"] and set(by_edge["bottom"]) <= settled["bottom"]


def test_no_conflict_leaves_the_field_empty(offline, tmp_path, monkeypatch):
    params, call, _lines = _run(tmp_path, monkeypatch, [_correct("nano")])
    assert call.call_count == 1 and not params.label_conflicts


def test_two_uarts_printed_alike_cost_no_paid_retry(offline, tmp_path, monkeypatch):
    """M4 negative (p3) end to end."""
    side = {"pitch_mm": 2.54, "contact_style": "half_hole"}
    answer = {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 20.0,
              "body_width_mm": 20.0, "confidence": "medium", "notes": "",
              "pinout": [{"pin": str(n), "name": name} for n, name in
                         enumerate(["TX", "RX", "EN", "TXD1", "RXD1", "SCL"], start=1)],
              "castellated_sides": [dict(side, edge="left", count=3, first_pin=1,
                                         pin_names=["TX", "RX", "EN"]),
                                    dict(side, edge="right", count=3, first_pin=4,
                                         pin_names=["TX", "RX", "SCL"])]}
    params, call, _lines = _run(tmp_path, monkeypatch, [answer])
    assert call.call_count == 1 and not params.label_conflicts


def test_a_model_cannot_hide_a_conflict_with_the_label_mark(offline, tmp_path, monkeypatch):
    """L7: `label_name` on the model's own pinout entries is stripped; the retry still runs."""
    answer = _esp32_1227()
    for entry in answer["pinout"]:
        entry[pc.LABEL_NAME_KEY] = True
    params, call, _lines = _run(tmp_path, monkeypatch, [answer, _esp32_1227(top=(1, True),
                                                                            bottom=(23, True))])
    assert call.call_count == 2
    assert not any(entry.get(pc.LABEL_NAME_KEY) for entry in params.pinout)
