"""@brief Geometry checks on the new drawing kinds and the silkscreen stroke rule (plan D6).

The silk clearance and the courtyard enclosure see the Bezier curve, the
text box border and the radial/centre/leader inks; a silkscreen stroke
thinner than KLC F5.1's lower bound (0.10 mm) is a warning with a remedy.
"""

from kicadgen.core.footprint import (FpCircle, FpCurve, FpDimension, FpLine, FpPoly, FpRect, FpText, FpTextBox,
                                     Footprint, Pad, bezier_point)
from kicadgen.services import footprint_checks as fc


def footprint_with_pad() -> Footprint:
    fp = Footprint(name="T", attr="smd")
    fp.add_pad(Pad(number="1", pad_type="smd", shape="rect", at=(0, 0), size=(1.0, 1.0), layers=["F.Cu"]))
    return fp


# ── Silk stroke width ──────────────────────────────────────────────────────

def test_a_silk_stroke_under_the_fab_minimum_is_flagged_and_the_klc_width_is_not():
    fp = Footprint(name="T")
    fp.add_graphic(FpLine(start=(0, 0), end=(5, 0), layer="F.SilkS", width=0.08))
    findings = fc.check_silk_stroke_width(fp)
    assert [f.code for f in findings] == ["silk_stroke_width"]
    assert findings[0].graphics == (0,) and findings[0].severity == "warning"
    assert findings[0].hint == fc.geometry_hint("silk_stroke_width") and findings[0].hint
    fp.graphics[0] = FpLine(start=(0, 0), end=(5, 0), layer="F.SilkS", width=0.12)
    assert fc.check_silk_stroke_width(fp) == []


def test_the_boundary_passes_a_stroke_of_exactly_the_minimum():
    assert fc.SILK_MIN_WIDTH_MM == 0.10
    fp = Footprint(name="T")
    fp.add_graphic(FpLine(start=(0, 0), end=(5, 0), layer="F.SilkS", width=0.10))
    assert fc.check_silk_stroke_width(fp) == [], "0.10 mm is printable: the rule is strictly below"
    fp.graphics[0] = FpLine(start=(0, 0), end=(5, 0), layer="F.SilkS", width=0.0999)
    assert len(fc.check_silk_stroke_width(fp)) == 1


def test_the_rule_reads_only_printed_silkscreen_strokes():
    fp = Footprint(name="T")
    # A thin line on F.Fab is documentation, never printed.
    fp.add_graphic(FpLine(start=(0, 0), end=(5, 0), layer="F.Fab", width=0.05))
    # A filled shape prints its area, a borderless text box prints no line, a text is a font.
    fp.add_graphic(FpPoly(points=[(0, 0), (1, 0), (0, 1)], layer="F.SilkS", width=0.05, fill=True))
    fp.add_graphic(FpTextBox(start=(0, 0), end=(4, 2), text="x", layer="F.SilkS", width=0.05, border=False))
    fp.add_graphic(FpText(kind="user", text="x", at=(0, 0), layer="F.SilkS", thickness=0.05))
    assert fc.check_silk_stroke_width(fp) == []
    # The same shapes as outlines are strokes and are flagged, all in one finding.
    fp.add_graphic(FpPoly(points=[(0, 0), (1, 0), (0, 1)], layer="F.SilkS", width=0.05))
    fp.add_graphic(FpTextBox(start=(0, 0), end=(4, 2), text="x", layer="F.SilkS", width=0.05))
    fp.add_graphic(FpCurve(points=[(0, 0), (1, 1), (2, 1), (3, 0)], layer="F.SilkS", width=0.06))
    fp.add_graphic(FpDimension(start=(0, 0), end=(2, 0), height=0, layer="F.SilkS", width=0.05, dtype="center"))
    findings = fc.check_silk_stroke_width(fp)
    assert len(findings) == 1 and findings[0].graphics == (4, 5, 6, 7)
    assert "0.05" in findings[0].message


def test_the_rule_is_part_of_the_full_check():
    fp = Footprint(name="T")
    fp.add_graphic(FpCircle(center=(5, 5), radius=1, layer="F.SilkS", width=0.05))
    assert "silk_stroke_width" in [f.code for f in fc.check_footprint_geometry(fp)]


# ── Ink of the new kinds ───────────────────────────────────────────────────

def test_the_curve_is_sampled_within_the_sagitta():
    control = [(0, 0), (1, 4), (3, 4), (4, 0)]
    segments = fc.graphic_segments(FpCurve(points=control, layer="F.SilkS"))
    assert len(segments) == fc.bezier_segments(control) > 10
    # Midpoints of the chords lie within the sagitta of the true curve (checked at the chord ends' mid-parameter).
    count = len(segments)
    for i, (a, b) in enumerate(segments):
        mid_curve = bezier_point(control, (i + 0.5) / count)
        mid_chord = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert abs(mid_curve[0] - mid_chord[0]) + abs(mid_curve[1] - mid_chord[1]) <= 2 * 0.005
    assert fc.bezier_segments([(0, 0), (1, 0), (2, 0), (3, 0)]) == 1, "a straight curve is one chord"


def test_silk_clearance_sees_a_curve_bulging_onto_the_pad_and_not_one_bulging_away():
    fp = footprint_with_pad()
    # Ends 2 mm left and right of the pad at y = -2; the bulge goes down onto the copper.
    fp.add_graphic(FpCurve(points=[(-2, -2), (-1, 1.0), (1, 1.0), (2, -2)], layer="F.SilkS"))
    assert [f.code for f in fc.check_silk_clearance(fp)] == ["silk_clearance"]
    # Same ends, bulging upwards (away): the control polygon alone would not tell them apart.
    fp.graphics[0] = FpCurve(points=[(-2, -2), (-1, -5), (1, -5), (2, -2)], layer="F.SilkS")
    assert fc.check_silk_clearance(fp) == []


def test_silk_clearance_sees_a_textbox_border_only_when_it_is_drawn():
    fp = footprint_with_pad()
    fp.add_graphic(FpTextBox(start=(-0.6, -3), end=(3, 3), text="x", layer="F.SilkS", width=0.12))
    assert [f.code for f in fc.check_silk_clearance(fp)] == ["silk_clearance"], "left side 0.1 mm from copper"
    fp.graphics[0] = FpTextBox(start=(-0.6, -3), end=(3, 3), text="x", layer="F.SilkS", width=0.12, border=False)
    assert fc.check_silk_clearance(fp) == []


def test_silk_clearance_sees_the_radial_centre_cross_and_the_leader_frame():
    fp = footprint_with_pad()
    # Radial centred 1.5 mm right of the pad: its centre cross (1.27 mm arms) reaches the copper.
    fp.add_graphic(FpDimension(start=(1.5, 0), end=(4, 0), height=0, layer="F.SilkS", width=0.12, dtype="radial"))
    assert [f.code for f in fc.check_silk_clearance(fp)] == ["silk_clearance"]
    fp.graphics[0] = FpDimension(start=(6, 0), end=(8, 0), height=0, layer="F.SilkS", width=0.12, dtype="radial")
    assert fc.check_silk_clearance(fp) == []
    # Leader whose circle frame around the text sits over the pad.
    dim = FpDimension(start=(-6, 3), end=(-3, 0), height=0, layer="F.SilkS", width=0.12, dtype="leader",
                      text="A", frame="circle")
    (cx, cy), r = dim.frame_circle()
    assert -0.5 < cx + r < 0.5, "the ring crosses the pad"
    fp.graphics[0] = dim
    assert [f.code for f in fc.check_silk_clearance(fp)] == ["silk_clearance"]
    fp.graphics[0] = FpDimension(start=(-6, 3), end=(-3, 0), height=0, layer="F.SilkS", width=0.12,
                                 dtype="leader", text="A", frame="none")
    assert fc.check_silk_clearance(fp) == [], "without the frame nothing reaches the pad"


def test_courtyard_enclosure_exempts_every_dimension_type_and_a_fab_textbox_but_not_a_fab_outline():
    """C7 (08/11): a text box documents the part like a dimension, whatever its border flag.

    Contract changed on purpose: before, a bordered F.Fab text box outside
    the courtyard was flagged as if its border outlined the body. The
    negative twin is a rectangle of the same corners on F.Fab: an outline,
    which the courtyard must enclose.
    """
    fp = footprint_with_pad()
    fp.add_graphic(FpRect(start=(-1, -1), end=(1, 1), layer="F.CrtYd", width=0.05))
    for dtype in ("", "radial", "center", "leader"):
        fp.graphics[1:] = [FpDimension(start=(5, 5), end=(8, 2), height=1.0, layer="F.Fab", dtype=dtype,
                                       text="A")]
        assert fc.check_courtyard_enclosure(fp) == [], dtype
    for border in (True, False):
        fp.graphics[1:] = [FpTextBox(start=(5, 5), end=(8, 2), text="x", layer="F.Fab", border=border)]
        assert fc.check_courtyard_enclosure(fp) == [], border
    fp.graphics[1:] = [FpRect(start=(5, 5), end=(8, 2), layer="F.Fab", width=0.1)]
    assert [f.code for f in fc.check_courtyard_enclosure(fp)] == ["courtyard_enclosure"]
