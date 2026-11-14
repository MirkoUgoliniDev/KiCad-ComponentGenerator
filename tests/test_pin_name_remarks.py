"""@brief Pin names that hold the model's remarks (fix set A, 2026-11-13).

The analysis of 2026-11-13 18:32 (castellated devkit, deepseek-flash) wrote
its own remarks into pin names («GND (labelled G, between TX and the right
hole)», «GPIO1 / IO1 (labelled "1")»). CONTRACT CHANGED 2026-11-13 (generality
audit M2): such names never cost a paid retry any more -- the heuristic has
known false positives («NC (not connected)»...), so the names are kept
untouched and flagged («Nomi dei pin», importance 2) by the component check,
until the schema separates names from remarks. More pinout entries than
contacts with CLEAN names never costs a paid retry (user decision,
2026-11-13): it gets the free side-numbering question. Offline: the main
call is faked, nothing reaches the network.
"""
import copy
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as de
from kicadgen.services import pinout_coherence as pc
from kicadgen.services.component_check import IMPORTANCE_BEFORE_PRODUCTION, document_checks
from minimal_pdf import MINIMAL_PDF


@pytest.fixture
def offline(monkeypatch):
    """No key, no credit check, no network."""
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kw: None)


# --- the heuristic (pure) -----------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "GND (labelled G, between TX and the right hole)",   # prose in a group
    "GND (top_left_field_label_G)",                      # words joined by underscores
    "GND (top left field label G)",
    'GPIO1 / IO1 (labelled "1")',                        # a quoted label
    "GND (labelled G)",                                  # one prose word, inside a group
    "VCC near the corner",                               # two prose words, no group
])
def test_a_remark_in_the_name_is_recognised(name):
    """Positive: quotes, prose in a group or two prose words make a remark."""
    assert pc.name_remarks(name)


@pytest.mark.parametrize("name", [
    "GPIO4 (ADC1_3, TOUCH4)",            # other functions, identifiers
    "GPIO4 (ADC1_3/RTC/TOUCH4)",
    "IO0/BOOT", "IO19 / USB_D-", "RST (EN)", "TX (U0TXD)", "5V (5V0)", "G (GND)",
    "nRESET", "3V3", "GND", "Coil", "Common", "Not connected", "GND (EP)", "", None, 7,
])
def test_a_signal_name_as_printed_is_clean(name):
    """Negative: upper-case identifiers, mnemonics and one capitalised word are no remark."""
    assert pc.name_remarks(name) == []


# --- the extraction: one paid retry -------------------------------------------------------------

def _side(edge, labels, first):
    return {"edge": edge, "count": len(labels), "pitch_mm": 2.54, "first_pin": first,
            "pin_names": list(labels), "contact_style": "through_hole",
            "through_hole_drill_mm": 1.0}


def _answer(names, labels=("G", "4", "5", "TX"), extra=()):
    """A castellated answer: one side of four contacts named by `names`, plus `extra` entries."""
    pinout = [{"pin": str(n), "name": name} for n, name in enumerate(names, start=1)]
    pinout += [dict(entry) for entry in extra]
    return {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 20.0,
            "body_width_mm": 10.0, "contacts_view": "top", "confidence": "medium", "notes": "",
            "pin_numbers_printed": True,
            "castellated_sides": [_side("left", labels, 1)], "pinout": pinout}


CLEAN = ["GND", "GPIO4 (ADC1_3, TOUCH4)", "GPIO5", "TX (U0TXD)"]
REMARKED = ["GND (labelled G, between TX and the right hole)", "GPIO4 (ADC1_3, TOUCH4)",
            'GPIO5 (labelled "5")', "TX (U0TXD)"]


def _run(tmp_path, monkeypatch, answers):
    source = tmp_path / "board.pdf"
    source.write_bytes(MINIMAL_PDF)
    call = Mock(side_effect=[copy.deepcopy(a) for a in answers])
    monkeypatch.setattr(de, "call_tool", call)
    lines = []
    params = de.extract_pin_header_params([source], progress=lines.append)
    return params, call, lines


def test_remark_names_cost_no_retry_and_are_flagged(offline, tmp_path, monkeypatch):
    """CONTRACT CHANGED 2026-11-13 (audit M2): one call, names kept, flagged for review."""
    params, call, lines = _run(tmp_path, monkeypatch, [_answer(REMARKED)])
    assert call.call_count == 1, "remark-like names never cost a paid retry"
    # Nothing stripped silently: the names stay as the model gave them.
    assert [entry["name"] for entry in params.pinout] == REMARKED
    assert "Pinout da rivedere" in params.notes
    # The component check shows them «DA RIVEDERE» (importance 2) on those pins.
    rows = [{"pin": str(e["pin"]), "name": e["name"]} for e in params.pinout]
    results = document_checks({"pinout": params.pinout}, rows, {"1", "2", "3", "4"})
    (flag,) = [r for r in results if r.code == "document_name_remark"]
    assert flag.status == "WARN" and flag.importance == IMPORTANCE_BEFORE_PRODUCTION
    assert flag.pins == ("1", "3")


def test_clean_names_with_parentheses_cost_no_retry(offline, tmp_path, monkeypatch):
    """Negative: «GPIO4 (ADC1_3, TOUCH4)» lists functions, it is no remark -> one call."""
    params, call, _lines = _run(tmp_path, monkeypatch, [_answer(CLEAN)])
    assert call.call_count == 1
    assert [entry["name"] for entry in params.pinout] == CLEAN


def test_more_clean_entries_than_contacts_cost_no_retry_and_ask_for_free(offline, tmp_path,
                                                                        monkeypatch):
    """Negative of the names rule: 45 clean entries for 44 contacts (two sides of 22, one
    undeclared entry without a pad) -> one call, and the free side-numbering question."""
    labels = [f"IO{n}" for n in range(44)]
    names = [f"GPIO{n}" for n in range(44)] + ["GND"]
    answer = {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": 63.3,
              "body_width_mm": 25.4, "contacts_view": "top", "confidence": "medium", "notes": "",
              "pin_numbers_printed": True,
              "castellated_sides": [_side("top", labels[:22], 1), _side("bottom", labels[22:], 23)],
              "pinout": [{"pin": str(n), "name": name} for n, name in enumerate(names, start=1)]}
    params, call, _lines = _run(tmp_path, monkeypatch, [answer])
    assert call.call_count == 1, "no paid retry for an entry without a pad"
    (question,) = [q for q in params.questions or () if q["field"] == "side_numbering"]
    assert "off_side_entries" in [option["value"] for option in question["options"]]
    assert "Pinout da rivedere" not in (params.notes or "")


def test_an_extra_entry_declared_off_the_sides_costs_no_retry(offline, tmp_path, monkeypatch):
    """Structural negative: the same fifth entry DECLARED off the sides (an exposed pad)."""
    declared = _answer(CLEAN, extra=[{"pin": "5", "name": "GND (EP)", "off_side": True}])
    _params, call, _lines = _run(tmp_path, monkeypatch, [declared])
    assert call.call_count == 1


def test_the_schema_asks_for_one_written_form_of_the_pin_name():
    """CONTRACT CHANGED (task D, 2026-11-14): «only the signal name as printed» left the form free.

    Real runs wrote one pin as «USB_D-/GPIO19», «GPIO19 / USB_D-», «USB_D-
    GPIO19» or «GPIO19» alone. The schema now asks PRIMARY or PRIMARY (ALT1,
    ALT2, ...), the form `symbol.split_pin_name` reads (primary name and
    alternates). A label printed with '/' stays one PRIMARY (a combined
    signal); DIFFERENT names are never joined. Remarks still go to notes.
    """
    pinout = de._EXTRACTION_TOOL["input_schema"]["properties"]["pinout"]
    name = pinout["items"]["properties"]["name"]["description"]
    assert "PRIMARY (ALT1, ALT2, ...)" in name
    assert "the same category for every pin" in name
    assert "keep it as printed as PRIMARY" in name
    assert "Do not join DIFFERENT names" in name
    assert "Never put remarks inside the parentheses" in name and "go to notes" in name
    # The parent no longer says «the alternative in brackets»: variants of
    # the same part go into the same parenthesised list, another part to notes.
    assert "in brackets only if different" not in pinout["description"]
    assert "inside the parentheses of name" in pinout["description"]


def test_the_written_form_is_the_one_the_symbol_reads():
    """The form the schema asks splits into the primary name and its alternates."""
    from kicadgen.generators.symbol import split_pin_name
    primary, alternates = split_pin_name("GPIO19 (USB_D-, ADC2_8)")[:2]
    assert primary == "GPIO19" and list(alternates) == ["USB_D-", "ADC2_8"]
    # Negative: a label printed with '/' outside parentheses stays one name.
    primary, alternates = split_pin_name("DE/RE")[:2]
    assert primary == "DE/RE" and not list(alternates)


def test_the_component_check_leaves_clean_names_alone():
    """Negative of the check: identifiers in a group, a slash name -> no name finding."""
    pinout = [{"pin": "1", "name": "GPIO4 (ADC1_3/RTC/TOUCH4)"}, {"pin": "2", "name": "IO0/BOOT"}]
    results = document_checks({"pinout": pinout}, pinout, {"1", "2"})
    assert not [r for r in results if r.code == "document_name_remark"]
