"""@brief A per-side size as wide as the pitch is not a measurement (HANDOFF point 168).

User's report of 2026-09-30: the model declared a 2.54 mm pad width «derived»
on a side whose pitch is 2.54 mm (its notes: «derivata dal foro stimato»),
and the generator, honouring a declared measure, refused the module: «il rame
del contatto 4 tocca quello del contatto 5». The rule is structural: two
adjacent contacts cannot share copper or drilling, so such a value is kept
as an estimate, which the generator reduces and notes. A value that fits the
pitch keeps its provenance, refusal included.
"""
import pytest

from test_photo_inventory_gui import gui  # noqa: F401 - GUI harness
from kicadgen.generators.castellated_module import MIN_COPPER_GAP_MM, is_declared_size
from kicadgen.services.datasheet_extractor import _params_from_tool_arguments


def _answer(pad_width, source, key="pad_width_mm"):
    side = dict(edge="right", count=4, pitch_mm=2.54, first_pin=4, contact_style="half_hole",
                value_sources={"pitch_mm": "read", key: source})
    side[key] = pad_width
    return dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=42.8, body_width_mm=15.2,
                castellated_sides=[
                    dict(edge="left", count=3, pitch_mm=5.08, first_pin=1,
                         contact_style="half_hole", value_sources={"pitch_mm": "read"}),
                    side])


@pytest.mark.parametrize("source", ["read", "derived"])
def test_a_pad_width_as_wide_as_the_pitch_becomes_an_estimate(source):
    """@brief Positive: 2.54 on a 2.54 pitch is impossible as a measure, whatever the model says."""
    params = _params_from_tool_arguments(_answer(2.54, source))
    assert params.value_sources["castellated_sides.right.pad_width_mm"] == "estimated"
    [note] = [line for line in params.notes.splitlines() if "non e' una misura" in line]
    assert "pad_width_mm" in note and "2.54" in note and "destro" in note


@pytest.mark.parametrize("key", ["half_hole_diameter_mm", "through_hole_drill_mm"])
def test_a_hole_as_wide_as_the_pitch_is_dropped_as_before(key):
    """@brief The holes have the same bound and were already dropped (with a note) by the
    side normalisation: the rule above adds nothing for them."""
    params = _params_from_tool_arguments(_answer(2.54, "read", key))
    assert key not in params.castellated_sides[1]
    assert f"castellated_sides.right.{key}" not in params.value_sources
    assert any("i fori adiacenti si unirebbero" in line for line in params.notes.splitlines())


@pytest.mark.parametrize("width", [2.0, 2.4])
def test_a_size_that_fits_the_pitch_keeps_its_provenance(width):
    """@brief Negative: 2.0 fits; 2.4 leaves less than the fabrication gap but is still a
    possible measure -- its provenance stays «read» and the generator, not the extraction,
    decides (a refusal to show, never a silent change)."""
    params = _params_from_tool_arguments(_answer(width, "read"))
    assert params.value_sources["castellated_sides.right.pad_width_mm"] == "read"
    assert "non e' una misura" not in params.notes


def test_a_single_contact_has_no_pitch_to_contradict():
    """@brief Negative: with one contact there is no adjacent copper; nothing is downgraded."""
    data = _answer(2.54, "read")
    data["castellated_sides"][1].update(count=1, pin_names=None)
    params = _params_from_tool_arguments(data)
    assert params.value_sources.get("castellated_sides.right.pad_width_mm") == "read"


def test_the_module_is_generated_with_the_width_reduced_and_noted(window):
    """@brief End to end: the downgraded width reaches the generator as an estimate, which fits it."""
    params = _params_from_tool_arguments(_answer(2.54, "derived"))
    window.group_castellated.setChecked(True)
    window._apply_extracted_castellated(params)
    window.field_cast_body_length.setValue(42.8)
    window.field_cast_body_width.setValue(15.2)
    module = window._current_castellated_params()
    right = next(side for side in module.sides if side.edge == "right")
    assert not is_declared_size(right.pad_width)
    module.validate()                                       # before: ValueError (copper touching)
    assert module.side_pad_width(right) == pytest.approx(2.54 - MIN_COPPER_GAP_MM)
    assert any("ridotta" in note for note in module.geometry_notes())
