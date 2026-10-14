"""@brief Regression tests for the generator defects fixed on 2026-09-25 (G1-G11).

Every rule is checked with a positive case and a structurally different
negative one; the KiCad-backed checks live in test_fixG_kicad_roundtrip.py.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os

import pytest

from kicadgen.core.footprint import FpCircle, FpLine, FpRect, Pad
from kicadgen.core.package_geometry import (
    PackageGeometry, PinSpec, pad_outline, polygon_separation,
)
from kicadgen.core.sexpr import SExpr, _format_number, sx
from kicadgen.core.silk import SILK_PAD_CLEARANCE_MM, clip_segment, pin_one
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE,
    CastellatedModuleParams, CastellatedSide, OffsetDrillPad, _copper_outline,
    build_castellated_module, continuous_pad, pad_positions,
)
from kicadgen.generators.package import build_package, footprint_attr
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from test_contact_pad_shape import _with_legacy_silk
from test_contact_style import (
    PINNED_FOOTPRINT_2026_09_24, _uuidless, dev_kit, four_sided, rs485, use_legacy_marker,
)


def _module(sides, **changes):
    params = CastellatedModuleParams(name="M", body_length=20, body_width=10, sides=sides)
    for key, value in changes.items():
        setattr(params, key, value)
    return params


# -- G1: the only changes to the pinned default output ------------------------

@pytest.mark.parametrize("name,builder", [("rs485", rs485), ("four", four_sided)])
def test_undoing_the_offset_fix_and_the_silk_clipping_gives_the_pinned_output(name, builder,
                                                                              monkeypatch):
    """@brief The half_and_through output changed ONLY in the drill model and the silk.

    Rewriting each drilled pad the old way (copper centre in `at`, offset =
    drill - copper) and folding the clipped silk back into one rectangle must
    reproduce the 2026-09-24 pinned footprint byte for byte. The new pinned
    hashes of test_contact_style.py therefore encode exactly these two fixes.
    The pin-1 dot is placed the pre-C3 way (see legacy_pin_one_marker).
    """
    use_legacy_marker(monkeypatch)
    fp = build_castellated_module(builder())
    assert any(isinstance(p, OffsetDrillPad) for p in fp.pads)
    legacy = copy.deepcopy(_with_legacy_silk(fp))
    for pad in legacy.pads:
        if isinstance(pad, OffsetDrillPad):
            dx, dy = pad.drill_offset
            pad.at = (pad.at[0] + dx, pad.at[1] + dy)
            pad.drill_offset = (-dx + 0.0, -dy + 0.0)
    rendered = _uuidless(legacy)
    assert hashlib.sha256(rendered.encode()).hexdigest() == PINNED_FOOTPRINT_2026_09_24[name]


def test_the_written_pad_position_is_the_drill_and_the_offset_reaches_the_copper():
    params = four_sided()
    fp = build_castellated_module(params)
    for placement in pad_positions(params):
        land = continuous_pad(params, placement)
        pad = next(p for p in fp.pads if p.number == placement.number)
        assert pad.at == pytest.approx((land.drill_x, land.drill_y))
        assert (pad.at[0] + pad.drill_offset[0], pad.at[1] + pad.drill_offset[1]) == \
            pytest.approx((land.x, land.y))


# -- G2: copper clearance between every pair, offsets inside the edge ----------

def test_through_hole_rings_of_two_sides_meeting_at_a_corner_are_refused():
    """@brief Negative: two rings of different pins overlap near a corner (was accepted)."""
    sides = [CastellatedSide(edge="left", count=1, pitch=0, offset=-3.6,
                             contact_style=CONTACT_THROUGH_HOLE),
             CastellatedSide(edge="top", count=1, pitch=0, offset=-3.6, first_pin=2,
                             contact_style=CONTACT_THROUGH_HOLE)]
    params = _module(sides, body_length=10, body_width=10)
    with pytest.raises(ValueError, match="in corto"):
        params.validate()
    for side in params.sides:          # positive: same rings, away from the corner
        side.offset = -1.0
    params.validate()


def test_edge_pads_of_two_sides_overlapping_at_a_corner_are_refused():
    """@brief Negative: DECLARED pads of two sides overlap at a corner; positive: moved away.

    @note Since 2026-09-28 only a declared size is refused: the same overlap
        with the assumed reference pads is reduced and noted instead
        (test_castellated_estimated_pads.py).
    """
    sides = [CastellatedSide(edge="left", count=1, pitch=0, offset=-3.5,
                             contact_style=CONTACT_HALF_HOLE),
             CastellatedSide(edge="top", count=1, pitch=0, offset=-3.5, first_pin=2,
                             contact_style=CONTACT_HALF_HOLE)]
    params = _module(sides, body_length=10, body_width=10, pad_length=3.6, pad_width=1.8)
    with pytest.raises(ValueError, match="in corto"):
        params.validate()
    for side in params.sides:
        side.offset = -1.0
    params.validate()


def test_a_side_offset_that_pushes_the_pads_past_the_edge_is_refused():
    """@brief Negative: offset 8 on a 5 mm half edge (was accepted); positive: offset 4."""
    params = _module([CastellatedSide(edge="left", count=1, pitch=0, offset=8.0,
                                      contact_style=CONTACT_HALF_HOLE)])
    with pytest.raises(ValueError, match="escono dal bordo"):
        params.validate()
    params.sides[0].offset = -8.0
    with pytest.raises(ValueError, match="escono dal bordo"):
        params.validate()
    params.sides[0].offset = 4.0          # 4 + 0.9 <= 5
    params.validate()


def test_a_pitch_equal_to_the_pad_width_is_a_short():
    """@brief Negative: touching DECLARED copper is a short (was accepted); positive: 0.1 mm gap.

    @note The pad size is declared (plain float): an assumed one would be
        reduced instead since 2026-09-28 (test_castellated_estimated_pads.py).
    """
    params = _module([CastellatedSide(edge="left", count=2, pitch=1.8,
                                      contact_style=CONTACT_HALF_HOLE)],
                     pad_length=3.6, pad_width=1.8)
    with pytest.raises(ValueError, match="in corto"):
        params.validate()
    params.sides[0].pitch = 1.9
    params.validate()


def test_the_same_number_on_two_pads_may_share_copper_but_is_refused_as_duplicate():
    params = _module([CastellatedSide(edge="left", count=2, pitch=1.8, pin_numbers=["1", "1"],
                                      contact_style=CONTACT_HALF_HOLE)])
    with pytest.raises(ValueError, match="compare sia"):
        params.validate()


# -- G5: non-finite numbers ------------------------------------------------------

@pytest.mark.parametrize("field", ["body_length", "body_width", "pad_length", "pad_overhang",
                                   "hole_diameter", "thru_hole_drill", "margin"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "3"])
def test_non_finite_module_dimensions_are_refused(field, value):
    params = rs485()
    setattr(params, field, value)
    with pytest.raises(ValueError, match=field):
        params.validate()


@pytest.mark.parametrize("field,value", [("pitch", float("nan")), ("offset", float("inf")),
                                         ("pad_width", float("nan"))])
def test_non_finite_side_dimensions_are_refused(field, value):
    params = rs485()
    setattr(params.sides[0], field, value)
    with pytest.raises(ValueError, match=field):
        params.validate()


@pytest.mark.parametrize("field,value", [("count", 3.0), ("count", True), ("first_pin", 1.5)])
def test_non_integer_counts_are_refused(field, value):
    params = rs485()
    setattr(params.sides[0], field, value)
    with pytest.raises(ValueError, match="intero"):
        params.validate()


def test_finite_values_still_validate():
    rs485().validate()
    four_sided().validate()


def test_number_formatting_rejects_nan_and_normalizes_negative_zero():
    for bad in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            _format_number(bad)
    assert _format_number(-1e-7) == "0"
    assert _format_number(-0.0) == "0"
    assert _format_number(-0.0000004) == "0"
    assert _format_number(-0.5) == "-0.5"
    assert _format_number(1.2699999999) == "1.27"
    assert _format_number(3) == "3"


def test_control_characters_in_strings_are_escaped_like_kicad():
    text = str(sx("descr", 'a\nb\rc\td "q" \\ e'))
    assert "\n" not in text and "\r" not in text and "\t" not in text
    assert text == '(descr "a\\nb\\rc\\td \\"q\\" \\\\ e")'


def test_render_keeps_atoms_after_children_in_their_place():
    """@brief G11: an atom after a child node is not moved in front of it."""
    node = SExpr("node", ["head", sx("child", sx("deep", 1)), "tail"])
    lines = node.render().splitlines()
    assert lines[0] == '(node "head"'
    assert lines[-2] == '\t"tail"'
    assert lines.index('\t"tail"') > next(i for i, l in enumerate(lines) if "child" in l)
    # Nodes without trailing atoms render exactly as before.
    plain = SExpr("n", ["a", sx("c", sx("d", 1))])
    assert plain.render() == '(n "a"\n\t(c\n\t\t(d 1)\n\t)\n)'


# -- G6: pin-1 marker by number ---------------------------------------------------

def _marker(fp):
    return next(g for g in fp.graphics if isinstance(g, FpCircle) and g.fill and g.layer == "F.SilkS")


def _nearest_pad(fp, point):
    def centre(p):
        off = getattr(p, "drill_offset", (0.0, 0.0))
        return (p.at[0] + off[0], p.at[1] + off[1])
    return min(fp.pads, key=lambda p: math.dist(centre(p), point)).number


@pytest.mark.parametrize("style", [CONTACT_HALF_HOLE, CONTACT_HALF_AND_THROUGH])
def test_marker_follows_pin_one_with_reverse_numbering(style):
    params = _module([CastellatedSide(edge="left", count=3, pitch=2.54, reverse=True,
                                      contact_style=style)])
    fp = build_castellated_module(params)
    marker = _marker(fp)
    one = next(p for p in pad_positions(params) if p.number == "1")
    two = next(p for p in pad_positions(params) if p.number == "2")
    assert _nearest_pad(fp, marker.center) == "1"
    # Beside pin 1 on the side AWAY from pin 2, not squeezed between them.
    assert (marker.center[1] - one.y) * (two.y - one.y) < 0


def test_marker_follows_pin_one_when_sides_are_listed_right_first():
    params = _module([CastellatedSide(edge="right", count=3, pitch=2.54, first_pin=4,
                                      contact_style=CONTACT_HALF_HOLE),
                      CastellatedSide(edge="left", count=3, pitch=2.54,
                                      contact_style=CONTACT_HALF_HOLE)])
    fp = build_castellated_module(params)
    assert _nearest_pad(fp, _marker(fp).center) == "1"
    assert sum(1 for g in fp.graphics if isinstance(g, FpCircle)) == 1


def test_pin_one_falls_back_to_the_lowest_natural_number():
    assert pin_one(["3", "1", "2"]) == "1"
    assert pin_one(["A10", "A2", "B1"]) == "A2"
    assert pin_one([10, 2, 3]) == "2"
    assert pin_one(["", "   "]) is None


def test_pin_header_marker_follows_a_mapped_pin_one():
    """@brief TEL8WI bottom view: pin 1 is at the top RIGHT, next to where pin 8 used to be."""
    pin_map = {1: (0, 7), 7: (0, 1), 8: (0, 0), 9: (1, 0), 10: (1, 1), 16: (1, 7)}
    fp = build_pin_header(PinHeaderParams(rows=2, cols=8, pitch=2.54, row_spacing=10.16,
                                          populated_pins=set(pin_map), pin_map=pin_map))
    marker = _marker(fp)
    assert _nearest_pad(fp, marker.center) == "1"
    assert marker.center[0] > 7 * 2.54 and marker.center[1] < 0
    plain = build_pin_header(PinHeaderParams(rows=2, cols=8, pitch=2.54, row_spacing=10.16))
    assert _marker(plain).center == (-1.5, -1.5)        # default grid: unchanged corner


# -- G7: populated pins without a position -----------------------------------------

def test_populated_pins_missing_from_the_pin_map_are_refused():
    params = PinHeaderParams(rows=1, cols=3, pin_map={1: (0, 0), 2: (0, 1)},
                             populated_pins={1, 2, 3})
    with pytest.raises(ValueError, match="3"):
        params.validate()
    params.populated_pins = {1, 2}
    params.validate()
    assert [p.number for p in build_pin_header(params).pads] == ["1", "2"]


# -- 2026-10-10: pin numbers of a map are not bounded by rows x cols ----------------

def test_mapped_pin_numbers_beyond_rows_times_cols_are_valid():
    """TEL8WI on its populated 2x3 grid: pin 16 exists where the map puts it."""
    pin_map = {1: (0, 0), 7: (0, 1), 8: (0, 2), 9: (1, 2), 10: (1, 1), 16: (1, 0)}
    params = PinHeaderParams(rows=2, cols=3, pitch=2.54, row_spacing=10.16,
                             pin_map=pin_map, populated_pins=set(pin_map))
    params.validate()
    assert sorted(int(p.number) for p in build_pin_header(params).pads) == [1, 7, 8, 9, 10, 16]
    unmapped = PinHeaderParams(rows=2, cols=3, populated_pins={1, 16})
    with pytest.raises(ValueError, match="16"):
        unmapped.validate()                      # automatic numbering: 1..6 only


# -- G8: opposite through-holes measured on the axis they face -------------------

def test_left_right_holes_are_measured_across_the_length_not_the_width():
    sides = [CastellatedSide(edge="left", count=1, pitch=0),
             CastellatedSide(edge="right", count=1, pitch=0, first_pin=2)]
    wide = _module(sides, body_length=30, body_width=4)
    wide.validate()                                    # was refused: min(30, 4) = 4
    narrow = _module(copy.deepcopy(sides), body_length=4, body_width=30)
    with pytest.raises(ValueError, match="non stanno in 4"):
        narrow.validate()                              # same body turned: rows 4 mm apart


def test_a_single_row_of_holes_is_not_compared_with_an_empty_opposite_edge():
    params = _module([CastellatedSide(edge="top", count=2, pitch=2.54)],
                     body_length=10, body_width=5)
    params.validate()


# -- G9: overhang, empty numbers, silk off copper ----------------------------------

@pytest.mark.parametrize("overhang", [3.6, 5.0, -0.1])
def test_pad_overhang_outside_the_land_or_negative_is_refused(overhang):
    params = rs485()
    params.pad_overhang = overhang
    with pytest.raises(ValueError, match="sbalzo"):
        params.validate()


def test_a_pad_still_partly_under_the_module_is_accepted():
    params = rs485(CONTACT_HALF_HOLE)
    shortest = min(params.side_pad_length(side) for side in params.sides)
    params.pad_overhang = shortest - 0.1           # 0.1 mm of land still under the module
    params.validate()
    params.pad_overhang = 0.0
    params.validate()


@pytest.mark.parametrize("numbers", [["1", "", "3"], [None, "2", "3"], ["1", "  ", "3"]])
def test_empty_pin_numbers_are_refused(numbers):
    params = rs485()
    params.sides[0].pin_numbers = numbers
    with pytest.raises(ValueError, match="vuoto"):
        params.validate()


def _silk_pieces(fp):
    pieces = []
    for g in fp.graphics:
        if g.layer != "F.SilkS":
            continue
        if isinstance(g, FpLine):
            pieces.append((g.start, g.end, g.width))
        elif isinstance(g, FpRect):
            (x0, y0), (x1, y1) = g.start, g.end
            c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            pieces += [(c[i], c[(i + 1) % 4], g.width) for i in range(4)]
    return pieces


def _segment_box_distance(a, b, box, samples=200):
    x0, y0, x1, y1 = box
    best = math.inf
    for i in range(samples + 1):
        t = i / samples
        px, py = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        best = min(best, math.hypot(max(x0 - px, 0, px - x1), max(y0 - py, 0, py - y1)))
    return best


@pytest.mark.parametrize("builder", [rs485, four_sided, lambda: rs485(CONTACT_HALF_HOLE)])
def test_castellated_silk_is_clipped_clear_of_the_overhanging_pads(builder):
    params = builder()
    fp = build_castellated_module(params)
    assert not any(isinstance(g, FpRect) and g.layer == "F.SilkS" for g in fp.graphics)
    pieces = _silk_pieces(fp)
    assert pieces, "the body outline must still be drawn where it can"
    for a, b, width in pieces:
        for placement in pad_positions(params):
            core = _copper_outline(params, placement)
            gap = _segment_box_distance(a, b, core[:4]) - core[4] - width / 2
            assert gap >= SILK_PAD_CLEARANCE_MM - 1e-3
    fab = [g for g in fp.graphics if isinstance(g, FpRect) and g.layer == "F.Fab"]
    assert fab and fab[0].start == (-params.body_length / 2, -params.body_width / 2)


def test_silk_that_does_not_meet_any_pad_stays_one_rectangle():
    """@brief Negative: rings set back from the edge leave the outline whole (byte-stable)."""
    fp = build_castellated_module(dev_kit())
    silk = [g for g in fp.graphics if g.layer == "F.SilkS" and not isinstance(g, FpCircle)]
    assert len(silk) == 1 and isinstance(silk[0], FpRect)


def test_clip_segment_removes_only_the_covered_part():
    pieces = clip_segment((0, 0), (10, 0), [(4, -1, 6, 1)])
    assert pieces == [((0.0, 0.0), (4.0, 0.0)), ((6.0, 0.0), (10.0, 0.0))]
    assert clip_segment((0, 0), (10, 0), [(4, 1, 6, 2)]) == [((0.0, 0.0), (10.0, 0.0))]


def test_pin_header_silk_stays_off_large_pads():
    fp = build_pin_header(PinHeaderParams(rows=2, cols=3, pitch=2.54, row_spacing=2.54,
                                          pad_diameter=2.0, margin=0.3))
    for a, b, width in _silk_pieces(fp):
        for pad in fp.pads:
            box = (pad.at[0] - 1, pad.at[1] - 1, pad.at[0] + 1, pad.at[1] + 1)
            assert _segment_box_distance(a, b, box) - width / 2 >= SILK_PAD_CLEARANCE_MM - 1e-3


# -- G3 / G4: package geometry and footprint ----------------------------------------

def test_rotated_pads_that_do_not_touch_are_accepted():
    """@brief Positive: two vertical QFN pads 0.2 mm apart (axis-aligned check refused them)."""
    PackageGeometry(pins=[PinSpec("1", 0, 0, 2, 0.4, rotation_deg=90),
                          PinSpec("2", 0.6, 0, 2, 0.4, rotation_deg=90)]).validate()


def test_rotated_pads_that_overlap_are_refused():
    """@brief Negative: a rotated pad crossing its neighbour (axis-aligned check accepted it)."""
    geometry = PackageGeometry(pins=[PinSpec("1", 0, 0, 2, 0.4),
                                     PinSpec("2", 0, 0.9, 2, 0.4, rotation_deg=90)])
    with pytest.raises(ValueError, match="pad sovrapposti"):
        geometry.validate()


def test_separation_is_positive_apart_zero_touching_negative_overlapping():
    a = pad_outline(0, 0, 2, 2)
    assert polygon_separation(a, pad_outline(3, 0, 2, 2)) == pytest.approx(1.0)
    assert polygon_separation(a, pad_outline(2, 0, 2, 2)) == pytest.approx(0.0, abs=1e-9)
    assert polygon_separation(a, pad_outline(1, 0, 2, 2)) < 0
    diamond = pad_outline(2.6, 0, 2, 2, 45)          # corner 1.414 from its centre
    assert polygon_separation(a, diamond) == pytest.approx(2.6 - math.sqrt(2) - 1)
    assert polygon_separation(a, pad_outline(2.3, 0, 2, 2, 45)) < 0


def test_positions_and_pad_angles_rotate_the_same_way():
    geometry = PackageGeometry(orientation_deg=90, pins=[PinSpec("1", 2, 0, 1, 0.5)])
    pin = geometry.pins_seen_from_top()[0]
    # KiCad: +90 turns +X towards -Y (up on screen), pads and positions alike.
    assert (pin.x_mm, pin.y_mm) == pytest.approx((0, -2))
    assert pin.rotation_deg == 90


def test_bottom_view_negates_the_pad_angle_together_with_x():
    geometry = PackageGeometry(source_view="bottom", pins=[PinSpec("1", 2, 1, 1, 0.5,
                                                                   rotation_deg=30)])
    pin = geometry.pins_seen_from_top()[0]
    assert (pin.x_mm, pin.y_mm) == pytest.approx((-2, 1))
    assert pin.rotation_deg == pytest.approx(330)


def _package(*pins, **kw):
    return PackageGeometry(name="P", family="custom", pins=list(pins), **kw)


def test_npth_pads_get_hole_layers_and_do_not_make_the_part_through_hole():
    geometry = _package(PinSpec("1", -2, 0, 1, 0.6),
                        PinSpec("MH", 2, 0, 1.2, 1.2, pad_type="np_thru_hole", shape="circle",
                                drill_mm=1.2))
    fp = build_package(geometry)
    npth = next(p for p in fp.pads if p.pad_type == "np_thru_hole")
    assert list(npth.layers) == ["*.Cu", "*.Mask"]
    assert fp.attr == "smd"
    assert footprint_attr(["np_thru_hole"]) is None
    assert footprint_attr(["smd", "thru_hole"]) == "through_hole"
    assert footprint_attr(["connect"]) is None


def test_package_has_courtyard_fab_and_pin_one_marker_off_the_copper():
    geometry = _package(*(PinSpec(str(i + 1), -2.5, -1.27 + 1.27 * i, 1.2, 0.6, side="left")
                          for i in range(3)),
                        *(PinSpec(str(6 - i), 2.5, -1.27 + 1.27 * i, 1.2, 0.6, side="right")
                          for i in range(3)),
                        body_length_mm=4, body_width_mm=4.5)
    fp = build_package(geometry)
    court = next(g for g in fp.graphics if isinstance(g, FpRect) and g.layer == "F.CrtYd")
    fab = next(g for g in fp.graphics if isinstance(g, FpRect) and g.layer == "F.Fab")
    assert fab.start == (-2, -2.25) and fab.end == (2, 2.25)
    assert court.start == pytest.approx((-3.35, -2.5)) and court.end == pytest.approx((3.35, 2.5))
    marker = _marker(fp)
    assert _nearest_pad(fp, marker.center) == "1"
    for pad in fp.pads:
        box = (pad.at[0] - 0.6, pad.at[1] - 0.3, pad.at[0] + 0.6, pad.at[1] + 0.3)
        assert _segment_box_distance(marker.center, marker.center, box) >= marker.radius
    for a, b, width in _silk_pieces(fp):
        for pad in fp.pads:
            box = (pad.at[0] - 0.6, pad.at[1] - 0.3, pad.at[0] + 0.6, pad.at[1] + 0.3)
            assert _segment_box_distance(a, b, box) - width / 2 >= SILK_PAD_CLEARANCE_MM - 1e-3


def test_package_without_body_still_gets_a_courtyard_around_the_pads():
    fp = build_package(_package(PinSpec("1", 0, 0, 1, 1), PinSpec("2", 3, 0, 1, 1)))
    court = next(g for g in fp.graphics if isinstance(g, FpRect) and g.layer == "F.CrtYd")
    assert court.start == pytest.approx((-0.75, -0.75)) and court.end == pytest.approx((3.75, 0.75))
    # CONTRACT CHANGED 2026-10-14 (L-3): the KLC ${REFERENCE} text sits on
    # F.Fab of every footprint; without a body there is still no F.Fab outline.
    assert not any(g.layer == "F.Fab" for g in fp.graphics if type(g).__name__ != "FpText")


# -- G10: pinout dialog order ----------------------------------------------------------

def test_pinout_sort_is_natural_and_accepts_mixed_types():
    from kicadgen.gui.pinout_dialog import pin_sort_key
    rows = [{"pin": 10}, {"pin": "2"}, {"pin": 1}, {"pin": "A10"}, {"pin": "A2"},
            {"name": "no pin"}, {"pin": "3"}]
    ordered = [r.get("pin") for r in sorted(rows, key=pin_sort_key)]
    assert ordered == [1, "2", "3", 10, "A2", "A10", None]


def test_pinout_dialog_builds_with_mixed_pin_types(qapp):
    pytest.importorskip("PyQt6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from kicadgen.gui.pinout_dialog import PinoutDialog
    app = qapp
    dialog = PinoutDialog([{"pin": 10, "name": "VOUT"}, {"pin": "2", "name": "GND"},
                           {"pin": 1, "name": "VIN"}], "X")
    column = [dialog.table.item(i, 0).text() for i in range(dialog.table.rowCount())]
    assert column == ["1", "2", "10"]
    dialog.deleteLater()
    app.processEvents()
