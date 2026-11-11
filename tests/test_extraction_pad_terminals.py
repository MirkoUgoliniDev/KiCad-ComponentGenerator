"""@brief Per-pad terminal, printed hole and provenance of the explicit pads (contract A1-A3, 2026-11-10).

The model transcribes what the drawing prints (a hole callout, a land, the
terminal section); the program derives the rest with the one rule of
`core.tht_estimates` and marks every value it did not read. Every rule has a
positive and a negative case that differ for a structural reason (a hole
printed or not, a terminal read or estimated, a second reading that saw the
same callout or another one), never for a number tuned to a part. Invented
two-pad parts; no network, no AI call.
"""

from __future__ import annotations

import pytest

from kicadgen.core import tht_estimates
from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.openrouter import strict_tool


def _pad(number, x, **extra):
    """One through-hole pad of the single-part schema, 1.8 mm round unless `extra` says otherwise."""
    pad = {"number": number, "x_mm": x, "y_mm": 0.0, "pad_type": "thru_hole",
           "pad_length_mm": 1.8, "pad_width_mm": 1.8, "shape": "circle"}
    pad.update(extra)
    return pad


def _answer(*pads, unit="mm", value_sources=None):
    """A single-part answer with the given pads, origin on pad 1."""
    answer = {"drawing_unit": unit, "contacts_view": "top", "confidence": "high",
              "printed_vertical_quotes": [], "pads_origin": "pin1", "pads": list(pads)}
    if value_sources is not None:
        answer["value_sources"] = value_sources
    return answer


def _params(answer, dedicated_holes=None):
    """The validated result of a single-part answer; `dedicated_holes` as apply_dedicated_pads leaves them."""
    data = ext.single_answer_to_data(answer)
    if dedicated_holes is not None:
        data["_dedicated_holes"] = dedicated_holes
    return ext._params_from_tool_arguments(data)


def _pins(params):
    """{number: pin dict} of the result's explicit geometry."""
    return {pin["number"]: pin for pin in params.package_geometry["pins"]}


ROUND_06 = {"shape": "round", "diameter_mm": 0.6}


# ---------------------------------------------------------------------------
# Printed hole against terminal
# ---------------------------------------------------------------------------

def test_a_printed_hole_is_kept_as_read_and_its_land_follows_it():
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.3, hole_callout="2-Ø1.3",
                                  value_sources={"drill_mm": "read"}, lead=ROUND_06),
                             _pad("2", 5.0, drill_mm=1.3, value_sources={"drill_mm": "read"})))
    pin = _pins(params)["1"]
    assert pin["drill_mm"] == 1.3 and pin["sources"]["drill_mm"] == "read"
    # The land is not printed: the round pad of the rule, derived from a read hole.
    assert pin["pad_length_mm"] == pin["pad_width_mm"] == tht_estimates.estimated_pad_diameter(1.3)
    assert pin["sources"]["pad_size"] == "derived" and pin["shape"] == "circle"
    assert "hole_callout" not in pin and "value_sources" not in pin


def test_a_hole_not_printed_is_derived_from_the_read_terminal():
    """Negative twin: the same model value 1.3, NOT declared printed, gives way to the terminal."""
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.3, lead=ROUND_06,
                                  value_sources={"drill_mm": "estimated", "lead": "read"}),
                             _pad("2", 5.0, lead=ROUND_06, value_sources={"lead": "read"})))
    drill, pad = tht_estimates.estimated_round_pad(ROUND_06)
    for pin in _pins(params).values():
        assert (pin["drill_mm"], pin["pad_length_mm"], pin["pad_width_mm"]) == (drill, pad, pad)
        assert pin["sources"] == {"drill_mm": "derived", "pad_size": "derived", "shape": "derived",
                                  "lead": "read"}
        assert pin["lead"] == ROUND_06
    assert "pad senza numero" not in params.notes.lower()


def test_an_estimated_terminal_gives_estimated_values():
    """Same terminal, declared estimated (or undeclared): the derived values are estimates."""
    params = _params(_answer(_pad("1", 0.0, lead=ROUND_06, value_sources={"lead": "estimated"}),
                             _pad("2", 5.0, lead=ROUND_06)))
    for pin in _pins(params).values():
        assert pin["sources"]["drill_mm"] == "estimated" and pin["sources"]["lead"] == "estimated"


def test_a_printed_land_is_kept_around_a_derived_hole():
    flat = {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}
    params = _params(_answer(_pad("1", 0.0, pad_length_mm=2.4, pad_width_mm=1.6, shape="oval", lead=flat,
                                  value_sources={"pad_size": "read", "lead": "read"}),
                             _pad("2", 5.0, lead=flat, value_sources={"lead": "read"})))
    one, two = _pins(params)["1"], _pins(params)["2"]
    assert (one["pad_length_mm"], one["pad_width_mm"], one["shape"]) == (2.4, 1.6, "oval")
    assert one["sources"]["pad_size"] == "read" and one["sources"]["drill_mm"] == "derived"
    # Negative twin: pad 2 prints no land, so its land is the round pad of the rule.
    assert two["shape"] == "circle" and two["sources"]["pad_size"] == "derived"


def test_without_hole_or_terminal_the_models_values_are_marked_estimated():
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.0)))
    pin = _pins(params)["1"]
    assert pin["drill_mm"] == 1.0 and pin["pad_length_mm"] == 1.8
    assert tht_estimates.unverified_values(pin["sources"]) == ("drill_mm", "pad_size", "shape")
    assert "ne' foro stampato ne' terminale dichiarato" in params.notes
    assert "Valori STIMATI o ricavati dal programma nei pad" in params.notes


def test_the_answer_level_drill_declaration_is_no_longer_dropped():
    """Point 5: a pad that declares nothing takes the answer-level `value_sources.drill_mm`."""
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.0),
                             value_sources={"drill_mm": "read"}))
    assert all(pin["sources"]["drill_mm"] == "read" for pin in _pins(params).values())


def test_a_slot_is_used_only_when_printed():
    # drill_length_mm runs along the pad WIDTH (PinSpec), so the pad is wide on that axis.
    printed = _params(_answer(_pad("1", 0.0, drill_mm=1.0, drill_length_mm=2.0, pad_width_mm=3.0,
                                   shape="oval", value_sources={"drill_mm": "read"}),
                              _pad("2", 5.0, drill_mm=1.0, value_sources={"drill_mm": "read"})))
    assert _pins(printed)["1"]["drill_length_mm"] == 2.0
    guessed = _params(_answer(_pad("1", 0.0, drill_mm=1.0, drill_length_mm=2.0),
                              _pad("2", 5.0, drill_mm=1.0)))
    assert _pins(guessed)["1"].get("drill_length_mm") is None
    assert "asola non stampata" in guessed.notes


def test_the_terminal_is_converted_with_the_drawing_unit():
    params = _params(_answer(_pad("1", 0.0, lead={"shape": "round", "diameter_mm": 0.02},
                                  value_sources={"lead": "read"}),
                             _pad("2", 0.2, lead={"shape": "round", "diameter_mm": 0.02},
                                  value_sources={"lead": "read"}), unit="inch"))
    assert _pins(params)["1"]["lead"]["diameter_mm"] == pytest.approx(0.508)


# ---------------------------------------------------------------------------
# The printed hole read twice
# ---------------------------------------------------------------------------

PRINTED = {"drill_mm": 1.3, "drill_length_mm": None, "text": "2-Ø1.3"}


def _printed_answer():
    return _answer(_pad("1", 0.0, drill_mm=1.3, value_sources={"drill_mm": "read"}),
                   _pad("2", 5.0, drill_mm=1.3, value_sources={"drill_mm": "read"}))


def test_the_same_printed_hole_in_both_readings_is_a_reading():
    params = _params(_printed_answer(), {"1": PRINTED, "2": PRINTED})
    assert all(pin["sources"]["drill_mm"] == "read" for pin in _pins(params).values())
    assert "diversamente dalle due letture" not in params.notes


def test_two_different_printed_holes_are_flagged_never_averaged():
    other = dict(PRINTED, drill_mm=1.2)
    params = _params(_printed_answer(), {"1": other, "2": PRINTED})
    one = _pins(params)["1"]
    assert one["drill_mm"] == 1.3 and one["sources"]["drill_mm"] == "estimated"
    assert "1.30 mm" in params.notes and "1.20 mm" in params.notes
    assert _pins(params)["2"]["sources"]["drill_mm"] == "read"


def test_a_hole_only_one_reading_printed_is_named_with_its_reader():
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.3, value_sources={"drill_mm": "read"}),
                             _pad("2", 5.0)), {"2": PRINTED})
    assert _pins(params)["2"]["drill_mm"] == 1.3 and _pins(params)["2"]["sources"]["drill_mm"] == "read"
    assert "letto solo dalla lettura dedicata" in params.notes
    assert "letto solo dalla risposta principale" in params.notes
    # Negative twin: without a dedicated reading nothing is said about it.
    alone = _params(_answer(_pad("1", 0.0, drill_mm=1.3, value_sources={"drill_mm": "read"}),
                            _pad("2", 5.0, drill_mm=1.3, value_sources={"drill_mm": "read"})))
    assert "letto solo dalla" not in alone.notes


def test_printed_holes_of_the_dedicated_reading_are_per_pad_and_self_consistent():
    holes = ext.printed_holes([{"pads": "1, 2", "diameter": 0.05, "text": "2-Ø.05"},
                               {"pads": "3", "diameter": 1.0, "slot_length": 2.0},
                               {"pads": "4", "diameter": 1.0}, {"pads": "4", "diameter": 1.1},
                               {"pads": "5", "diameter": None}], 25.4)
    assert holes["1"]["drill_mm"] == pytest.approx(1.27) and holes["2"]["text"] == "2-Ø.05"
    assert holes["3"]["drill_length_mm"] == pytest.approx(50.8)
    assert holes["4"] == {"conflict": True} and "5" not in holes


# ---------------------------------------------------------------------------
# Malformed pads and the lead rule
# ---------------------------------------------------------------------------

def test_a_drill_on_a_pad_without_hole_is_a_note_not_a_failed_analysis():
    params = _params(_answer(_pad("1", 0.0, pad_type="smd", drill_mm=0.8, shape="rect"),
                             _pad("2", 5.0, pad_type="smd", shape="rect")))
    assert "drill_mm" not in _pins(params)["1"]
    assert "dichiarati senza foro ma con un foro" in params.notes


def test_a_read_hole_is_never_changed_by_a_terminal_that_does_not_pass():
    big = {"shape": "round", "diameter_mm": 1.5}
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.3, lead=big,
                                  value_sources={"drill_mm": "read", "lead": "read"}),
                             _pad("2", 5.0, drill_mm=1.3, value_sources={"drill_mm": "read"})))
    assert _pins(params)["1"]["drill_mm"] == 1.3
    assert "non passa nel foro letto" in params.notes


def test_an_estimated_hole_that_the_terminal_does_not_pass_is_lifted_keeping_the_pad_orientation():
    """The two printed readings disagree (hole estimated) and the pad's terminal does not pass it."""
    big = {"shape": "round", "diameter_mm": 1.5}
    pads = _answer(_pad("1", 0.0, drill_mm=1.3, lead=big, pad_length_mm=2.4, pad_width_mm=1.6,
                        shape="oval", value_sources={"drill_mm": "read", "pad_size": "read",
                                                     "lead": "read"}),
                   _pad("2", 5.0, drill_mm=1.3, value_sources={"drill_mm": "read"}))
    params = _params(pads, {"1": dict(PRINTED, drill_mm=1.2), "2": PRINTED})
    pin = _pins(params)["1"]
    assert pin["drill_mm"] == tht_estimates.estimated_hole(big)
    # Widened on both axes by the same amount: the oval keeps its long axis.
    assert pin["pad_length_mm"] - pin["pad_width_mm"] == pytest.approx(0.8)
    assert pin["sources"]["drill_mm"] == "estimated" and pin["sources"]["pad_size"] == "estimated"
    assert "allargate attorno al foro ricavato" in params.notes


@pytest.mark.parametrize("lead_source, lifted", [("estimated", False), ("read", True)])
def test_the_part_level_lead_overrules_a_read_hole_only_when_read(lead_source, lifted):
    params = _params(_answer(_pad("1", 0.0, drill_mm=0.5), _pad("2", 5.0, drill_mm=0.5),
                             value_sources={"drill_mm": "read", "lead_diameter_mm": lead_source})
                     | {"lead_diameter_mm": 0.6})
    drills = {pin["drill_mm"] for pin in _pins(params).values()}
    assert drills == ({round(0.6 + tht_estimates.LEAD_TO_HOLE_CLEARANCE_MM, 3)} if lifted else {0.5})


# ---------------------------------------------------------------------------
# Schemas, prompt, constants
# ---------------------------------------------------------------------------

def test_the_new_schema_fields_keep_the_strict_conversion_lossless():
    for tool in (ext._single_component_tool(), ext._pad_positions_tool(), ext._openai_tool()):
        assert strict_tool(tool) is not None
    pad = ext._single_component_tool()["function"]["parameters"]["properties"]["pads"]["items"]
    assert {"lead", "hole_callout", "drill_length_mm", "value_sources"} <= set(pad["properties"])


def test_the_former_single_prompt_that_estimated_holes_is_superseded():
    assert ("ed685f00b13bfc7e7e6702815de750fbc54117b2202bb28fc560202175a062e3"
            in ext.SUPERSEDED_SINGLE_PROMPT_DIGESTS)
    assert component_types.text_digest(ext.DEFAULT_SINGLE_SYSTEM_PROMPT) not in ext.SUPERSEDED_SINGLE_PROMPT_DIGESTS
    assert "stima il foro" not in ext.DEFAULT_SINGLE_SYSTEM_PROMPT


def test_the_three_constants_have_one_definition():
    assert ext.LEAD_TO_HOLE_CLEARANCE_MM is tht_estimates.LEAD_TO_HOLE_CLEARANCE_MM
    assert ext.HOLE_TO_PAD_ANNULUS_MM is tht_estimates.HOLE_TO_PAD_ANNULUS_MM
    assert ext.MIN_ANNULAR_RING_MM is tht_estimates.MIN_ANNULAR_RING_MM
