"""@brief Geometric checks of a footprint in memory (`services.footprint_checks`, 2026-11-06).

One positive and one negative case per rule, built by hand on
`core.footprint.Footprint`; then the generators' own outputs, which must be
clean: a rule that flagged a generated footprint would be a wrong rule or a
wrong generator, both worth knowing.
"""
import pytest

from kicadgen.core.footprint import FpArc, FpCircle, FpLine, FpRect, Footprint, Pad
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.services import footprint_checks as fc

THT = ["*.Cu", "*.Mask"]
SMD = ["F.Cu", "F.Paste", "F.Mask"]


def _fp(*pads, graphics=()):
    fp = Footprint(name="T", attr="smd")
    for pad in pads:
        fp.add_pad(pad)
    for graphic in graphics:
        fp.add_graphic(graphic)
    return fp


def _smd(number, x, y, w=1.0, h=1.0, shape="rect", rotation=0.0):
    return Pad(number=number, pad_type="smd", shape=shape, at=(x, y), size=(w, h), layers=SMD, rotation=rotation)


def _codes(findings):
    return [f.code for f in findings]


# ── pads_overlap ───────────────────────────────────────────────────────────

def test_two_numbered_pads_sharing_copper_are_an_error_with_both_indices():
    fp = _fp(_smd("1", 0, 0), _smd("2", 0.5, 0), _smd("3", 5, 0))
    findings = fc.check_pads_overlap(fp)
    assert _codes(findings) == ["pads_overlap"] and findings[0].severity == "error"
    assert findings[0].pads == (0, 1) and "1" in findings[0].message and "2" in findings[0].message


def test_pads_with_the_same_number_or_apart_do_not_overlap():
    assert fc.check_pads_overlap(_fp(_smd("1", 0, 0), _smd("1", 0.5, 0))) == []
    assert fc.check_pads_overlap(_fp(_smd("1", 0, 0), _smd("2", 1.5, 0))) == []


# ── silk_clearance ─────────────────────────────────────────────────────────

def test_silk_ink_inside_the_clearance_is_a_warning_naming_pad_and_graphic():
    # Pad edge at x = 0.5; a 0.12 mm line at x = 0.6 has its ink at 0.54: 0.04 mm from the copper.
    line = FpLine(start=(0.6, -1), end=(0.6, 1), layer="F.SilkS", width=0.12)
    fp = _fp(_smd("1", 0, 0), graphics=[line])
    findings = fc.check_silk_clearance(fp)
    assert _codes(findings) == ["silk_clearance"] and findings[0].severity == "warning"
    assert findings[0].pads == (0,) and findings[0].graphics == (0,)
    assert "0.04" in findings[0].message


def test_silk_at_the_clearance_and_on_other_layers_passes():
    # Ink edge at 0.5 + 0.2 = 0.7 -> centre line at 0.76.
    ok = FpLine(start=(0.76, -1), end=(0.76, 1), layer="F.SilkS", width=0.12)
    fab = FpLine(start=(0.3, -1), end=(0.3, 1), layer="F.Fab", width=0.1)
    assert fc.check_silk_clearance(_fp(_smd("1", 0, 0), graphics=[ok, fab])) == []


def test_the_distance_is_measured_to_the_copper_not_to_the_bounding_box():
    # A 2 x 0.4 mm pad turned 45 degrees (KiCad: counter-clockwise on screen,
    # so its long axis runs from bottom-left to top-right, i.e. along (1, -1)
    # with Y down): its box reaches 0.85 mm along X, its copper does not at
    # (0.9, 0.6), which lies across the axis.
    pad = _smd("1", 0, 0, w=2.0, h=0.4, rotation=45.0)
    near_box_far_from_copper = FpLine(start=(0.9, 0.6), end=(1.5, 0.6), layer="F.SilkS", width=0.12)
    assert fc.check_silk_clearance(_fp(pad, graphics=[near_box_far_from_copper])) == []
    on_copper = FpLine(start=(0.5, -0.5), end=(0.8, -0.8), layer="F.SilkS", width=0.12)
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[on_copper]))) == ["silk_clearance"]


def test_circles_and_arcs_are_sampled_rectangles_use_their_edges():
    pad = _smd("1", 0, 0)
    dot_too_close = FpCircle(center=(0.9, 0), radius=0.25, layer="F.SilkS", width=0.12, fill=True)
    dot_clear = FpCircle(center=(1.2, 0), radius=0.25, layer="F.SilkS", width=0.12, fill=True)
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[dot_too_close]))) == ["silk_clearance"]
    assert fc.check_silk_clearance(_fp(pad, graphics=[dot_clear])) == []
    arc_near = FpArc(start=(0.6, -0.5), mid=(0.55, 0.0), end=(0.6, 0.5), layer="F.SilkS", width=0.12)
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[arc_near]))) == ["silk_clearance"]
    frame = FpRect(start=(-2, -2), end=(2, 2), layer="F.SilkS", width=0.12)
    assert fc.check_silk_clearance(_fp(pad, graphics=[frame])) == []
    hugging = FpRect(start=(-0.6, -0.6), end=(0.6, 0.6), layer="F.SilkS", width=0.12)
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[hugging]))) == ["silk_clearance"]


# ── courtyard_enclosure ────────────────────────────────────────────────────

def test_a_pad_or_fab_graphic_outside_the_courtyard_margin_is_reported():
    yard = FpRect(start=(-1, -1), end=(1, 1), layer="F.CrtYd", width=0.05)
    inside = _smd("1", 0, 0, w=1.0, h=1.0)          # edge at 0.5, margin 0.5 >= 0.25
    outside = _smd("2", 0.6, 0, w=0.6, h=0.6)        # edge at 0.9, margin 0.1 < 0.25
    body = FpLine(start=(-1.5, 0), end=(1.5, 0), layer="F.Fab", width=0.1)
    findings = fc.check_courtyard_enclosure(_fp(inside, outside, graphics=[yard, body]))
    assert _codes(findings) == ["courtyard_enclosure", "courtyard_enclosure"]
    assert findings[0].pads == (1,) and "2" in findings[0].message
    assert findings[1].graphics == (1,)


def test_everything_inside_the_courtyard_margin_or_no_courtyard_passes():
    yard = FpRect(start=(-1, -1), end=(1, 1), layer="F.CrtYd", width=0.05)
    body = FpRect(start=(-0.7, -0.7), end=(0.7, 0.7), layer="F.Fab", width=0.1)
    assert fc.check_courtyard_enclosure(_fp(_smd("1", 0, 0), graphics=[yard, body])) == []
    assert fc.check_courtyard_enclosure(_fp(_smd("1", 0, 0, w=5, h=5))) == []   # no courtyard: not this rule


# ── annular_ring ───────────────────────────────────────────────────────────

def test_a_thin_ring_is_a_warning_and_a_slot_is_measured_per_axis():
    thin = Pad(number="1", pad_type="thru_hole", shape="circle", at=(0, 0), size=(1.0, 1.0), layers=THT, drill=0.8)
    fine = Pad(number="2", pad_type="thru_hole", shape="circle", at=(3, 0), size=(1.7, 1.7), layers=THT, drill=1.0)
    slot = Pad(number="3", pad_type="thru_hole", shape="oval", at=(6, 0), size=(1.6, 3.0), layers=THT,
               drill_slot=(1.0, 2.8))                     # ring 0.3 along X, 0.1 along Y
    mechanical = Pad(number="", pad_type="np_thru_hole", shape="circle", at=(9, 0), size=(1.0, 1.0),
                     layers=THT, drill=1.0)                 # unplated: no ring expected
    findings = fc.check_annular_ring(_fp(thin, fine, slot, mechanical))
    assert [(f.code, f.pads) for f in findings] == [("annular_ring", (0,)), ("annular_ring", (2,))]
    assert fc.annular_ring(slot) == pytest.approx(0.1) and fc.annular_ring(mechanical) is None


# ── pin_one_marker ─────────────────────────────────────────────────────────

def test_a_pin_one_without_any_marker_is_a_warning_on_that_pad():
    fp = _fp(_smd("2", -1, 0, shape="roundrect"), _smd("1", 1, 0, shape="roundrect"))
    findings = fc.check_pin_one_marker(fp)
    assert _codes(findings) == ["pin_one_marker"] and findings[0].pads == (1,)


def test_a_marker_counts_only_where_pin_one_is():
    """generality audit 2026-11-06: the mark must be a measured relation to pin 1, not any dot or shape."""
    pads = (_smd("1", -1, 0, shape="roundrect"), _smd("2", 1, 0, shape="roundrect"),
            _smd("3", 3, 0, shape="roundrect"))
    near_one = FpCircle(center=(-2, 0), radius=0.25, layer="F.SilkS", width=0.12, fill=False)
    assert fc.check_pin_one_marker(_fp(*pads, graphics=[near_one])) == [], "a hollow user circle beside pin 1"
    near_two = FpCircle(center=(2, 0), radius=0.25, layer="F.SilkS", width=0.12, fill=True)
    assert _codes(fc.check_pin_one_marker(_fp(*pads, graphics=[near_two]))) == ["pin_one_marker"]
    # The differing shape must be ON pin 1.
    assert fc.check_pin_one_marker(_fp(_smd("1", -1, 0, shape="rect"), _smd("2", 1, 0, shape="roundrect"),
                                       _smd("3", 3, 0, shape="roundrect"))) == []
    assert _codes(fc.check_pin_one_marker(_fp(_smd("1", -1, 0, shape="roundrect"), _smd("2", 1, 0, shape="roundrect"),
                                              _smd("3", 3, 0, shape="oval", w=1.0, h=1.4)))) == ["pin_one_marker"]
    # A short bevel at pin 1 marks it; the long slanted outline edge of a turned package does not.
    bevel = FpLine(start=(-2, -1.5), end=(-1.5, -2), layer="F.Fab", width=0.1)
    assert fc.check_pin_one_marker(_fp(*pads, graphics=[bevel])) == []
    outline = FpLine(start=(-2, -2), end=(2, 2), layer="F.Fab", width=0.1)
    assert _codes(fc.check_pin_one_marker(_fp(*pads, graphics=[outline]))) == ["pin_one_marker"]
    assert fc.check_pin_one_marker(_fp(_smd("K", -1, 0), _smd("A", 1, 0))) == [], "no pin 1: nothing to mark"


def test_a_large_silk_circle_is_measured_as_finely_as_a_small_one():
    """generality audit 2026-11-06: fixed 36-segment sampling let a 50 mm frame circle pass."""
    pad = _smd("1", 0, 0)
    for radius in (5.0, 20.0, 50.0):
        # Ink edge 0.15 mm from the copper edge (x = 0.5): centre line 0.06 mm further out.
        circle = FpCircle(center=(radius + 0.5 + 0.15 + 0.06, 0), radius=radius, layer="F.SilkS", width=0.12)
        findings = fc.check_silk_clearance(_fp(pad, graphics=[circle]))
        assert _codes(findings) == ["silk_clearance"], radius
        assert "0.15" in findings[0].message, radius
        clear = FpCircle(center=(radius + 0.5 + 0.21 + 0.06, 0), radius=radius, layer="F.SilkS", width=0.12)
        assert fc.check_silk_clearance(_fp(pad, graphics=[clear])) == [], radius


def test_a_trapezoid_is_measured_on_its_real_corners_not_its_box():
    trapezoid = Pad(number="1", pad_type="smd", shape="trapezoid", at=(0, 0), size=(2.0, 2.0), layers=SMD,
                    rect_delta=(0.0, 1.8))     # narrow at the top (y = -1), wide at the bottom (y = +1)
    beside_narrow_end = FpLine(start=(0.95, -1.5), end=(0.95, -0.6), layer="F.SilkS", width=0.12)
    assert fc.check_silk_clearance(_fp(trapezoid, graphics=[beside_narrow_end])) == []
    beside_wide_end = FpLine(start=(1.95, 0.5), end=(1.95, 1.5), layer="F.SilkS", width=0.12)
    assert _codes(fc.check_silk_clearance(_fp(trapezoid, graphics=[beside_wide_end]))) == ["silk_clearance"]


def test_a_mechanical_hole_has_no_copper_for_the_copper_rules():
    hole = Pad(number="", pad_type="np_thru_hole", shape="circle", at=(0, 0), size=(1.0, 1.0), layers=THT, drill=1.0)
    assert fc.check_pads_overlap(_fp(_smd("1", 0, 0), hole)) == []
    silk = FpLine(start=(0.6, -1), end=(0.6, 1), layer="F.SilkS", width=0.12)
    assert fc.check_silk_clearance(_fp(hole, graphics=[silk])) == []


def test_the_ring_of_an_offset_hole_is_measured_on_its_thin_side():
    """generality audit 2026-11-06: castellated lands put the hole off the copper centre."""
    from kicadgen.generators.castellated_module import (CastellatedModuleParams, CastellatedSide,
                                                        OffsetDrillPad, build_castellated_module)
    shifted = OffsetDrillPad(number="1", pad_type="thru_hole", shape="oval", at=(0, 0), size=(3.0, 1.8),
                             layers=THT, drill=1.0, drill_offset=(0.8, 0.0))
    assert fc.annular_ring(shifted) == pytest.approx(1.5 - 0.8 - 0.5)       # 0.2 along X, 0.4 along Y
    thin = CastellatedModuleParams(sides=[CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1,
                                                          contact_style="half_and_through")],
                                   row_spacing=12.0, thru_pad_diameter=1.2, thru_hole_drill=1.0)
    rings = [f for f in fc.check_footprint_geometry(build_castellated_module(thin)) if f.code == "annular_ring"]
    assert rings and all("0.10" in f.message for f in rings)


# ── check_footprint_geometry and the generators ────────────────────────────

def test_errors_come_before_warnings_and_every_finding_carries_its_hint():
    fp = _fp(_smd("2", -1, 0, shape="roundrect"), _smd("1", -0.5, 0, shape="roundrect"))
    findings = fc.check_footprint_geometry(fp)
    assert [f.severity for f in findings][:1] == ["error"]
    assert all(f.hint for f in findings)
    assert fc.geometry_hint("nope") == ""


def test_the_generators_own_footprints_are_clean():
    header = build_pin_header(PinHeaderParams(rows=2, cols=4))
    assert fc.check_footprint_geometry(header) == []
    package = PackageGeometry(name="P", family="soic", body_length_mm=5.0, body_width_mm=4.0, pins=[
        PinSpec(number=str(n), x_mm=-2.7 if n <= 4 else 2.7, y_mm=(-1.905 + 1.27 * ((n - 1) % 4)) * (1 if n <= 4 else -1),
                pad_length_mm=1.5, pad_width_mm=0.6, side="left" if n <= 4 else "right")
        for n in range(1, 9)])
    assert fc.check_footprint_geometry(build_package(package)) == []
    tht = PackageGeometry(name="R", family="custom", body_length_mm=6.0, body_width_mm=3.0, pins=[
        PinSpec(number="1", x_mm=-2.54, y_mm=0, pad_length_mm=1.7, pad_width_mm=1.7, pad_type="thru_hole",
                shape="circle", drill_mm=1.0),
        PinSpec(number="2", x_mm=2.54, y_mm=0, pad_length_mm=1.7, pad_width_mm=1.7, pad_type="thru_hole",
                shape="circle", drill_mm=1.0)])
    assert fc.check_footprint_geometry(build_package(tht)) == []
    # Turned packages (slanted outlines, rotated pads), castellated modules
    # (offset holes) and the header host lands are clean too.
    from dataclasses import replace
    assert fc.check_footprint_geometry(build_package(replace(package, orientation_deg=30))) == []
    from kicadgen.generators.castellated_module import (CastellatedModuleParams, CastellatedSide,
                                                        build_castellated_module)
    module = CastellatedModuleParams(sides=[CastellatedSide(edge="left", count=4, pitch=2.54),
                                            CastellatedSide(edge="right", count=4, pitch=2.54, first_pin=5)])
    assert fc.check_footprint_geometry(build_castellated_module(module)) == []
    from kicadgen.generators.host_mounting import header_footprint_params
    assert fc.check_footprint_geometry(build_package(header_footprint_params(tht, 1.7, 1.0))) == []
