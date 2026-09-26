"""@brief Offline checks: required fields follow the answer's structure; units and views are converted by code.

Audit findings 3, 4, 5 and 6 of 2026-09-26. Every answer here is an offline
tool-argument dictionary: no network, no paid call, no .env.
"""
import copy

import pytest

from kicadgen.generators.castellated_module import CastellatedSide
from kicadgen.generators.pin_header import PinHeaderParams, pin_positions
from kicadgen.services.contact_zones import contact_positions
from kicadgen.services.datasheet_extractor import (
    ExtractionError, _castellated_sides, _params_from_tool_arguments, _validate_main_response)


def _soic8(**geometry):
    """An SOIC-8 land pattern as explicit pads (1.27 mm pitch, rows 5.4 mm apart)."""
    pins = [dict(number=str(i + 1), x_mm=-2.7 if i < 4 else 2.7,
                 y_mm=-1.905 + 1.27 * (i if i < 4 else 7 - i),
                 pad_length_mm=1.55, pad_width_mm=0.6, pad_type="smd") for i in range(8)]
    data = dict(family="soic", source_view="top", pins=pins)
    data.update(geometry)
    return data


def _matrix(**extra):
    data = dict(layout="matrix", rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=7.62,
                pad_diameter_mm=1.7, drill_mm=1.0, confidence="high",
                drawing_unit="mm", contacts_view="top")
    data.update(extra)
    return data


def _pin_x(params):
    """{pin: x in mm} as the footprint draws the extracted grid."""
    mapping = ({e["pin"]: (e["row"], e["col"]) for e in params.pin_map}
               if params.pin_map else None)
    grid = PinHeaderParams(rows=params.rows, cols=params.cols, pitch=params.pitch_mm or 1.0,
                           row_spacing=params.row_spacing_mm, pin_map=mapping)
    return {pin: x for pin, x, _ in pin_positions(grid)}


# ── Finding 4: what is required depends on what the answer is ────────────────

@pytest.mark.parametrize("drill", [0, None])
def test_smd_part_with_explicit_pads_and_no_drill_is_accepted(drill):
    data = dict(layout="matrix", confidence="high", drawing_unit="mm", contacts_view="top",
                package_geometry=_soic8())
    if drill is not None:
        data["drill_mm"] = drill
    params = _params_from_tool_arguments(data)
    assert params.package_geometry["family"] == "soic" and len(params.package_geometry["pins"]) == 8
    assert params.drill_mm is None and params.rows is None
    # A drill of 0 is how SMD is written, not an invalid value to report.
    assert "drill_mm" not in params.notes


def test_the_same_smd_answer_without_explicit_pads_is_rejected():
    """Negative: a pad without hole and no pad shape cannot become a footprint.

    The pin-grid generator draws only drilled pads (PinHeaderParams.validate
    requires drill > 0): passing drill 0 on would give nothing or, through the
    GUI minimum, a drilled pad the part does not have.
    """
    data = _matrix(drill_mm=0)
    with pytest.raises(ExtractionError, match="package_geometry"):
        _params_from_tool_arguments(data)
    del data["drill_mm"]
    with pytest.raises(ExtractionError, match="drill_mm"):
        _params_from_tool_arguments(data)


def test_explicit_pads_with_informative_matrix_keep_the_valid_values():
    params = _params_from_tool_arguments(dict(
        layout="matrix", rows=2, cols=4, pitch_mm=1.27, row_spacing_mm="?", drill_mm=0,
        confidence="high", drawing_unit="mm", contacts_view="top", package_geometry=_soic8()))
    assert (params.rows, params.cols, params.pitch_mm, params.row_spacing_mm) == (2, 4, 1.27, None)
    assert "row_spacing_mm scartato" in params.notes


def test_invalid_explicit_pads_are_still_an_error():
    geometry = _soic8()
    geometry["pins"][1]["x_mm"] = geometry["pins"][0]["x_mm"]
    geometry["pins"][1]["y_mm"] = geometry["pins"][0]["y_mm"]
    with pytest.raises(ExtractionError, match="Geometria package"):
        _params_from_tool_arguments(dict(layout="matrix", confidence="high",
                                         package_geometry=geometry))


def test_one_row_needs_no_row_spacing_and_two_rows_do():
    one = _matrix(rows=1, cols=6)
    del one["row_spacing_mm"]
    params = _params_from_tool_arguments(one)
    assert params.rows == 1 and params.row_spacing_mm is None
    two = _matrix()
    del two["row_spacing_mm"]
    with pytest.raises(ExtractionError, match="row_spacing_mm"):
        _params_from_tool_arguments(two)


def test_one_column_needs_no_pitch_and_two_columns_do():
    one = _matrix(rows=2, cols=1)
    del one["pitch_mm"]
    params = _params_from_tool_arguments(one)
    assert params.cols == 1 and params.pitch_mm is None
    two = _matrix(cols=2)
    del two["pitch_mm"]
    with pytest.raises(ExtractionError, match="pitch_mm"):
        _params_from_tool_arguments(two)


def test_the_row_spacing_is_never_borrowed_from_the_pitch():
    params = _params_from_tool_arguments(_matrix(rows=1, row_spacing_mm=None))
    assert params.row_spacing_mm is None and params.pitch_mm == 2.54


# ── Finding 5: the code mirrors a bottom view, for every contact geometry ────

def test_bottom_view_1x6_puts_pin_1_at_the_opposite_end():
    bottom = _params_from_tool_arguments(_matrix(rows=1, cols=6, row_spacing_mm=None,
                                                 contacts_view="bottom"))
    top = _params_from_tool_arguments(_matrix(rows=1, cols=6, row_spacing_mm=None))
    assert _pin_x(top)[1] == 0 and _pin_x(top)[6] == pytest.approx(12.7)
    assert _pin_x(bottom)[1] == pytest.approx(12.7) and _pin_x(bottom)[6] == 0
    assert top.pin_map is None and "DUBBIO vista" not in top.notes
    assert "vista dal basso" in bottom.notes


def test_bottom_view_mirrors_the_body_offset_without_a_pin_map():
    params = _params_from_tool_arguments(_matrix(
        rows=1, cols=6, row_spacing_mm=None, contacts_view="bottom",
        body_length_mm=16.0, body_offset_x_mm=-1.0, body_offset_y_mm=-2.0))
    # Drawn from below: 1.0 mm left margin, 16 - 12.7 - 1.0 = 2.3 mm right margin.
    assert params.body_offset_x_mm == pytest.approx(-2.3)
    assert params.body_offset_y_mm == -2.0


def test_a_single_column_body_offset_is_mirrored_too():
    params = _params_from_tool_arguments(_matrix(
        rows=3, cols=1, pitch_mm=None, contacts_view="bottom",
        body_length_mm=5.0, body_offset_x_mm=-1.0))
    assert params.body_offset_x_mm == pytest.approx(-4.0)


def test_an_unknown_view_is_a_declared_doubt_and_nothing_is_mirrored():
    for view in ("unknown", None, "sideways"):
        data = _matrix(rows=1, cols=6, row_spacing_mm=None, contacts_view=view)
        params = _params_from_tool_arguments(data)
        assert "DUBBIO vista" in params.notes and params.pin_map is None


def test_the_legacy_pin_map_view_still_mirrors():
    data = _matrix(rows=1, cols=6, row_spacing_mm=None, pin_map_view="bottom")
    del data["contacts_view"]
    assert _pin_x(_params_from_tool_arguments(data))[1] == pytest.approx(12.7)


def _castellated(view, **extra):
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view=view,
                body_length_mm=20.0, body_width_mm=10.0, castellated_sides=[
                    dict(edge="left", count=3, pitch_mm=2.54, first_pin=1, offset_mm=0.5),
                    dict(edge="top", count=4, pitch_mm=2.54, first_pin=4, offset_mm=1.0,
                         pin_names=["A", "B", "C", "D"])])
    data.update(extra)
    return data


def _named_numbers(side):
    """Pad number -> silkscreen name, pairing names with pads in edge order."""
    cs = CastellatedSide(edge=side["edge"], count=side["count"], pitch=side["pitch_mm"],
                         first_pin=side["first_pin"], reverse=side.get("reverse", False))
    return dict(zip(cs.numbers(), side.get("pin_names") or ()))


def test_castellated_sides_in_bottom_view_are_mirrored():
    top = _params_from_tool_arguments(_castellated("top")).castellated_sides
    bottom = _params_from_tool_arguments(_castellated("bottom")).castellated_sides
    assert [s["edge"] for s in top] == ["left", "top"]
    assert [s["edge"] for s in bottom] == ["right", "top"]
    # Vertical edge: order from top to bottom and offset unchanged.
    assert bottom[0].get("reverse", False) is False and bottom[0]["offset_mm"] == 0.5
    # Horizontal edge: order from left to right reversed, offset negated.
    assert top[1].get("reverse", False) is False and bottom[1]["reverse"] is True
    assert bottom[1]["offset_mm"] == -1.0
    # Names travel with their pads: pin 4 is «A» in both views.
    assert _named_numbers(top[1])["4"] == _named_numbers(bottom[1])["4"] == "A"
    # And pin 4 moved from the left end to the right end of the edge.
    assert CastellatedSide(edge="top", count=4, pitch=2.54, first_pin=4,
                           reverse=bottom[1]["reverse"]).numbers()[-1] == "4"


def test_castellated_sides_in_top_view_are_unchanged():
    raw = _castellated("top")
    sides = _params_from_tool_arguments(raw).castellated_sides
    assert sides[1]["offset_mm"] == 1.0 and sides[1]["pin_names"] == ["A", "B", "C", "D"]


def test_the_input_answer_is_never_modified():
    raw = _castellated("bottom", drawing_unit="inch")
    before = copy.deepcopy(raw)
    _params_from_tool_arguments(raw)
    assert raw == before


def test_validation_canonicalizes_once_for_the_photo_stage():
    """The photo stage reads the answer again: it must already be mm, seen from above."""
    data = _matrix(rows=1, cols=6, pitch_mm=0.1, row_spacing_mm=None, pad_diameter_mm=0.07,
                   drill_mm=0.04, drawing_unit="inch", contacts_view="bottom",
                   body_length_mm=0.7, body_width_mm=0.2, body_height_mm=0.3,
                   body_offset_x_mm=-0.05, body_offset_y_mm=-0.1)

    def no_call(**kwargs):
        raise AssertionError("no paid call is expected for a matrix answer")

    messages = []
    _validate_main_response(data, [], no_call, None, None, False, messages.append)
    assert data["drawing_unit"] == "mm" and data["contacts_view"] == "top"
    assert data["body_length_mm"] == pytest.approx(17.78) and data["pitch_mm"] == pytest.approx(2.54)
    assert any("convertite in millimetri" in m for m in messages)
    first = _params_from_tool_arguments(data)
    second = _params_from_tool_arguments(data)
    assert first.pitch_mm == second.pitch_mm == pytest.approx(2.54)
    assert first.notes == second.notes and first.notes.count("convertite") == 1
    # The contact mask of the photo stage agrees with the footprint, board-centred.
    xs = sorted(p["x_mm"] for p in contact_positions(data, 17.78, 5.08))
    expected = sorted(-17.78 / 2 - first.body_offset_x_mm + x for x in _pin_x(first).values())
    assert xs == pytest.approx(expected)
    # Read from below with pin 1 at the left end, it is at the right end seen from above.
    assert -17.78 / 2 - first.body_offset_x_mm + _pin_x(first)[1] == pytest.approx(7.62)


# ── Finding 6: units are declared by the model and converted by the code ─────

def test_an_inch_drawing_is_converted_everywhere():
    params = _params_from_tool_arguments(_matrix(
        drawing_unit="inch", pitch_mm=0.1, row_spacing_mm=0.6, pad_diameter_mm=0.067,
        drill_mm=0.04, body_length_mm=0.8, body_width_mm=0.7, body_height_mm=0.3,
        body_offset_x_mm=-0.05, pin_length_3d_mm=0.12,
        package_geometry=None))
    assert params.pitch_mm == pytest.approx(2.54)
    assert params.row_spacing_mm == pytest.approx(15.24)
    assert params.drill_mm == pytest.approx(1.016) and params.pad_diameter_mm == pytest.approx(1.7018)
    assert params.body_length_mm == pytest.approx(20.32) and params.body_offset_x_mm == pytest.approx(-1.27)
    assert "convertite in millimetri" in params.notes and "DUBBIO unita'" not in params.notes


def test_an_mm_drawing_is_unchanged():
    params = _params_from_tool_arguments(_matrix())
    assert (params.pitch_mm, params.row_spacing_mm, params.drill_mm) == (2.54, 7.62, 1.0)
    assert "convertite" not in params.notes and "DUBBIO" not in params.notes


def test_inch_sides_holes_and_explicit_pads_are_converted():
    castellated = _params_from_tool_arguments(_castellated(
        "top", drawing_unit="inch", body_length_mm=1.0, body_width_mm=0.5,
        castellated_sides=[dict(edge="left", count=3, pitch_mm=0.1, half_hole_diameter_mm=0.04)]))
    side = castellated.castellated_sides[0]
    assert side["pitch_mm"] == pytest.approx(2.54) and side["half_hole_diameter_mm"] == pytest.approx(1.016)
    smd = _params_from_tool_arguments(dict(layout="matrix", confidence="high", drawing_unit="mil",
                                           contacts_view="top",
                                           package_geometry=_soic8(pins=[dict(
                                               number="1", x_mm=100, y_mm=-50, pad_length_mm=60,
                                               pad_width_mm=24)])))
    pin = smd.package_geometry["pins"][0]
    assert (pin["x_mm"], pin["y_mm"], pin["pad_length_mm"]) == pytest.approx((2.54, -1.27, 1.524))


@pytest.mark.parametrize("pitch", [0.1, 0.05, 0.0197])
def test_a_pitch_below_any_package_read_as_mm_is_a_declared_doubt(pitch):
    params = _params_from_tool_arguments(_matrix(pitch_mm=pitch, row_spacing_mm=0.3,
                                                 pad_diameter_mm=0.06, drill_mm=0.03))
    assert "DUBBIO unita' di misura" in params.notes
    # Declared, never applied: the numbers stay as the model sent them.
    assert params.pitch_mm == pitch


@pytest.mark.parametrize("pitch", [0.3, 0.4, 0.5, 1.27])
def test_real_fine_pitches_in_mm_raise_no_doubt(pitch):
    params = _params_from_tool_arguments(_matrix(pitch_mm=pitch, pad_diameter_mm=0.25,
                                                 drill_mm=0.15))
    assert "DUBBIO unita'" not in params.notes


def test_explicit_pads_closer_than_any_package_are_a_doubt():
    tight = _soic8(pins=[dict(number=str(i + 1), x_mm=0.05 * i, y_mm=0, pad_length_mm=0.02,
                              pad_width_mm=0.02) for i in range(4)])
    params = _params_from_tool_arguments(dict(layout="matrix", confidence="high",
                                              drawing_unit="mm", package_geometry=tight))
    assert "DUBBIO unita'" in params.notes
    normal = _params_from_tool_arguments(dict(layout="matrix", confidence="high",
                                              drawing_unit="mm", package_geometry=_soic8()))
    assert "DUBBIO unita'" not in normal.notes


def test_an_unknown_unit_is_a_declared_doubt():
    params = _params_from_tool_arguments(_matrix(drawing_unit="cm"))
    assert "DUBBIO unita' di misura 'cm'" in params.notes and params.pitch_mm == 2.54


# ── Finding 3: castellated hole sizes are extracted per side ─────────────────

def test_hole_sizes_are_kept_per_side():
    sides = _castellated_sides([
        dict(edge="left", count=4, pitch_mm=2.54, contact_style="half_and_through",
             half_hole_diameter_mm=1.0, through_hole_drill_mm=0.8),
        dict(edge="right", count=4, pitch_mm=2.54)])
    assert (sides[0]["half_hole_diameter_mm"], sides[0]["through_hole_drill_mm"]) == (1.0, 0.8)
    assert "half_hole_diameter_mm" not in sides[1] and "through_hole_drill_mm" not in sides[1]


@pytest.mark.parametrize("bad", [0, -1.0, True, "x", float("nan"), float("inf")])
def test_invalid_hole_sizes_are_dropped_with_a_note(bad):
    notes = []
    sides = _castellated_sides([dict(edge="left", count=4, pitch_mm=2.54,
                                     half_hole_diameter_mm=bad)], notes=notes)
    assert "half_hole_diameter_mm" not in sides[0]
    assert any("half_hole_diameter_mm scartato" in n for n in notes)


def test_a_hole_not_smaller_than_the_pitch_is_dropped():
    """Physical bound: adjacent holes that large would merge into one slot."""
    notes = []
    sides = _castellated_sides([dict(edge="top", count=4, pitch_mm=1.27,
                                     through_hole_drill_mm=1.27, half_hole_diameter_mm=1.0)],
                               notes=notes)
    assert "through_hole_drill_mm" not in sides[0] and sides[0]["half_hole_diameter_mm"] == 1.0
    assert any("fori adiacenti" in n for n in notes)
    # A single contact has no neighbour: the same diameter is legal there.
    single = _castellated_sides([dict(edge="top", count=1, pitch_mm=0,
                                      through_hole_drill_mm=1.27)])
    assert single[0]["through_hole_drill_mm"] == 1.27


def test_a_hole_the_observed_style_does_not_have_is_kept_with_a_doubt():
    notes = []
    sides = _castellated_sides([dict(edge="left", count=2, pitch_mm=2.54,
                                     contact_style="half_hole", through_hole_drill_mm=0.8)],
                               notes=notes)
    assert sides[0]["through_hole_drill_mm"] == 0.8
    assert any("non ha quel foro" in n for n in notes)


def test_hole_sizes_reach_the_extracted_params():
    params = _params_from_tool_arguments(_castellated("top", castellated_sides=[
        dict(edge="left", count=3, pitch_mm=2.54, half_hole_diameter_mm=1.0,
             through_hole_drill_mm=0.7, contact_style="half_and_through")]))
    side = params.castellated_sides[0]
    assert side["half_hole_diameter_mm"] == 1.0 and side["through_hole_drill_mm"] == 0.7
