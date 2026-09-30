"""@brief A castellated row spacing places the through-holes only when the model READ it (HANDOFF 167).

User's report of 2026-09-30 («ma stai scherzando???»): the model returned the
pitch of one side as `row_spacing_mm`, saying itself it was derived and not a
distance between rows; the generator took it as declared and put the
through-holes 18.86 mm from the edge, with copper lands across half the
board. The rule is on the declared provenance, not on a number: "read"
keeps the value, anything else drops it with a note.
"""
import pytest

from kicadgen.services.datasheet_extractor import _params_from_tool_arguments


def _answer(spacing=5.08, source="derived"):
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=42.8, body_width_mm=15.2, row_spacing_mm=spacing,
                castellated_sides=[
                    dict(edge="left", count=3, pitch_mm=5.08, first_pin=1,
                         contact_style="half_and_through"),
                    dict(edge="right", count=4, pitch_mm=2.54, first_pin=4,
                         contact_style="half_and_through")])
    if source is not None:
        data["value_sources"] = {"row_spacing_mm": source}
    return data


def test_a_read_row_spacing_is_kept():
    """@brief Positive: a measurement read in the drawing places the holes."""
    params = _params_from_tool_arguments(_answer(source="read"))
    assert params.row_spacing_mm == 5.08
    assert params.value_sources["row_spacing_mm"] == "read"
    assert "non usata per posizionare" not in params.notes


@pytest.mark.parametrize("source", ["derived", "estimated", None])
def test_a_row_spacing_the_model_did_not_read_never_places_the_holes(source):
    """@brief Negative: the same 5.08, derived / estimated / undeclared, is dropped and said."""
    params = _params_from_tool_arguments(_answer(source=source))
    assert params.row_spacing_mm is None
    assert "row_spacing_mm" not in (params.value_sources or {})
    [note] = [line for line in params.notes.splitlines() if "non usata per posizionare" in line]
    assert "5.08" in note and "18.86" in note        # (42.8 - 5.08) / 2: where it would have gone
    assert ("derivata" if source == "derived" else "stimata" if source == "estimated"
            else "senza provenienza") in note


def test_a_matrix_keeps_its_row_spacing_whatever_the_provenance():
    """@brief Negative: the rule is about castellated through-holes; a pin matrix needs its spacing."""
    params = _params_from_tool_arguments(dict(
        layout="matrix", confidence="high", drawing_unit="mm", rows=2, cols=4, pitch_mm=2.54,
        row_spacing_mm=7.62, pad_diameter_mm=1.7, drill_mm=1.0,
        value_sources={"row_spacing_mm": "estimated"}))
    assert params.row_spacing_mm == 7.62
