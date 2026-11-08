"""@brief Dimension geometry as KiCad 10 computes it: text box, leader frames, shelves, label pose, arrowheads.

Review findings M9, G7, C3, C4 and C8 of 08/11. The text box of a stroke
text, the rectangle and circle frames of a leader, the shelf of a leader and
of a radial dimension, the value position of an aligned/orthogonal
dimension and the arrowhead wings are checked against numbers measured with
kicad-cli 10.0 and, when KiCad is installed, against kicad-cli itself
(`fp export svg` for the ink, `fp upgrade` for the text position KiCad
recomputes on load).
"""
import math
import re
import subprocess

import pytest

from kicadgen.core.footprint import (DIMENSION_ARROW_ANGLE_DEG, DIMENSION_ARROW_LENGTH_MM, DIMENSION_SHELF_MM,
                                     FpDimension, FpLine, FpRect, FpText, Footprint, default_text_positions,
                                     dimension_crossbar, dimension_ink_segments, dimension_label_pose,
                                     dimension_text_half_box, drawing_graphics, footprint_envelope,
                                     leader_dimension_ink, validate_footprint_drawings)
from kicadgen.services.system_dependencies import find_kicad_cli

#: Agreement asked of the program's geometry against KiCad's own, mm.
KICAD_TOLERANCE_MM = 0.03


# ── Text box (G7) ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, size, pen, frame_hw, frame_hh", [
    # Half width and half height of KiCad's rectangle frame around a leader
    # text, read from kicad-cli 10.0 SVG (2026-11-08); the frame is the text
    # box inflated by size / 2 across and 2 x pen up and down.
    ("ABC", 1.0, 0.1504, 2.0542, 1.1498),
    ("1", 1.0, 0.15, 1.1012, 1.1482),
    ("R 2.50 mm", 2.0, 0.2022, 9.6271, 1.9293),
    ("x", 2.0, 0.2031, 1.9142, 1.9326),
])
def test_the_stroke_text_box_matches_kicads_measured_box(text, size, pen, frame_hw, frame_hh):
    half_w, half_h = dimension_text_half_box(text, size, pen)
    assert half_w + size / 2 == pytest.approx(frame_hw, abs=1e-3)
    assert half_h + 2 * pen == pytest.approx(frame_hh, abs=1e-3)


def test_the_text_box_height_grows_with_the_pen_and_the_width_with_the_text():
    """The height follows size and pen only (one line), the width the glyphs; an empty text keeps the pens."""
    assert dimension_text_half_box("W", 1.0)[1] == dimension_text_half_box("....", 1.0)[1]
    assert dimension_text_half_box("W", 1.0, 0.2)[1] > dimension_text_half_box("W", 1.0, 0.1)[1]
    assert dimension_text_half_box("WW", 1.0)[0] > dimension_text_half_box("W", 1.0)[0]
    assert dimension_text_half_box("", 1.0, 0.15)[0] == pytest.approx(3 * 0.15 / 2), "no glyph, no trailing space"
    assert dimension_text_half_box("ABC", 1.0, 0.4) == dimension_text_half_box("ABC", 1.0, 0.25), \
        "KiCad clamps the pen to a quarter of the size"


def test_the_rectangle_frame_is_the_box_plus_half_a_size_across_and_two_pens_up_and_down():
    """M9: the old frame was 0.175 mm too low per side at size 1 (fudge applied to size + pen only)."""
    ink = leader_dimension_ink((0, 0), (3, -3), "ABC", 1.0, "rect")
    frame = ink[4:]
    ys = [p[1] for seg in frame for p in seg]
    xs = [p[0] for seg in frame for p in seg]
    # kicad-cli 10.0: half height 1.1482 for any text at size 1, pen 0.15.
    assert (max(ys) - min(ys)) / 2 == pytest.approx(1.1482, abs=0.001)
    assert (max(xs) - min(xs)) / 2 == pytest.approx(2.0536 - 0.0, abs=0.005)


# ── Label pose of aligned/orthogonal dimensions (C4) ───────────────────────

@pytest.mark.parametrize("start, end, height, axis, centre, angle", [
    # What kicad-cli 10.0 `fp upgrade` rewrites into the gr_text (2026-11-08).
    ((0, 0), (4, 0), 1, "", (2, -0.15), 0),
    ((0, 0), (4, 0), -1, "", (2, -2.15), 0),
    ((4, 0), (0, 0), 1, "", (2, -2.15), 0),
    ((0, 0), (0, 4), 1, "", (-2.15, 2), 90),
    ((0, 4), (0, 0), 1, "", (-0.15, 2), 90),
    ((0, 0), (3, 4), 2, "", (0.82, 2.51), 306.8699),
    ((0, 0), (-3, 4), 2, "", (-4.02, 0.11), 53.1301),
    ((0, 0), (3, -4), -2, "", (-1.02, -3.89), 53.1301),
    ((0, 0), (1, 4), 1, "y", (-0.15, 2), 90),
    ((0, 0), (4, 1), -1.5, "x", (2, -2.65), 0),
    ((1, 4), (0, 0), 2, "y", (1.85, 2), 90),
])
def test_the_value_sits_where_kicad_recomputes_it(start, end, height, axis, centre, angle):
    (x, y), got_angle = dimension_label_pose(start, end, height, 1.0, axis)
    assert (x, y) == pytest.approx(centre, abs=1e-6)
    assert got_angle == pytest.approx(angle, abs=1e-3)


def test_the_value_side_follows_the_crossbar_direction_not_the_height_sign():
    """Structural contrast: one rightward line, heights of both signs put the text on the same side (above).

    The old rule moved the text along the height side, so the two cases
    differed; KiCad puts it up the page for both.
    """
    (_x, above_pos), _ = dimension_label_pose((0, 0), (4, 0), 1.0, 1.0)
    crossbar_pos = dimension_crossbar((0, 0), (4, 0), 1.0)[0][1]
    (_x, above_neg), _ = dimension_label_pose((0, 0), (4, 0), -1.0, 1.0)
    crossbar_neg = dimension_crossbar((0, 0), (4, 0), -1.0)[0][1]
    assert above_pos < crossbar_pos and above_neg < crossbar_neg
    assert crossbar_pos - above_pos == pytest.approx(1.15) == crossbar_neg - above_neg


def test_the_written_file_carries_the_position_kicad_computes():
    fp = Footprint(name="T", attr="smd")
    fp.add_graphic(FpDimension(start=(0, 0), end=(0, 4), height=1.0, layer="F.Fab"))
    assert "(at -2.15 2 90)" in fp.render()


# ── Arrowheads (C3) ────────────────────────────────────────────────────────

def _kicad_wings(tip, angle_deg):
    """KiCad's `drawAnArrow(tip, angle)`: (L, 0) turned by -angle +- arrow angle with RotatePoint (Y down)."""
    ends = set()
    for turn in (-angle_deg + DIMENSION_ARROW_ANGLE_DEG, -angle_deg - DIMENSION_ARROW_ANGLE_DEG):
        r = math.radians(turn)
        # RotatePoint(x, y, a) = (x cos a + y sin a, y cos a - x sin a).
        ends.add((round(tip[0] + DIMENSION_ARROW_LENGTH_MM * math.cos(r), 6),
                  round(tip[1] - DIMENSION_ARROW_LENGTH_MM * math.sin(r), 6)))
    return ends


def _expected_arrow_ends(start, end, height):
    (c1, c2) = dimension_crossbar(start, end, height)
    theta = math.degrees(math.atan2(c2[1] - c1[1], c2[0] - c1[0]))
    return _kicad_wings(c1, theta) | _kicad_wings(c2, theta + 180.0)


def _old_reflection_arrow_ends(start, end, height):
    """The formula before 08/11 (a reflection, not a rotation), kept here to prove the test can tell them apart."""
    (c1, c2) = dimension_crossbar(start, end, height)
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    length = math.hypot(dx, dy)
    dx, dy = dx / length, dy / length
    a = math.radians(DIMENSION_ARROW_ANGLE_DEG)
    ends = set()
    for (px, py), (tx_, ty_) in ((c1, (-dx, -dy)), (c2, (dx, dy))):
        for s in (1.0, -1.0):
            ax = tx_ * math.cos(a) - s * ty_ * math.sin(a)
            ay = tx_ * math.sin(a) + s * ty_ * math.cos(a)
            ends.add((round(px - ax * DIMENSION_ARROW_LENGTH_MM, 6), round(py - ay * DIMENSION_ARROW_LENGTH_MM, 6)))
    return ends


@pytest.mark.parametrize("start, end, height", [((0, 0), (0, 10), 2.0), ((1, 1), (7, 5), -1.5),
                                                ((0, 0), (10, 0), 2.0)],
                         ids=["vertical", "slanted", "horizontal"])
def test_both_wings_of_both_arrowheads_point_as_kicad_draws_them(start, end, height):
    strokes = dimension_ink_segments(start, end, height)[3:]
    got = {(round(seg[1][0], 6), round(seg[1][1], 6)) for seg in strokes}
    expected = _expected_arrow_ends(start, end, height)
    assert len(expected) == 4
    assert got == expected
    assert _old_reflection_arrow_ends(start, end, height) != expected, \
        "the old reflection formula fails here, so this case guards against it"


# ── Envelope with the ink of a dimension (C8) ──────────────────────────────

def test_the_envelope_counts_a_radial_knee_and_its_value_not_only_the_two_points():
    fp = Footprint(name="T", attr="smd")
    fp.add_graphic(FpRect(start=(-1, -1), end=(1, 1), layer="F.Fab", width=0.1))
    plain = footprint_envelope(fp)
    fp.add_graphic(FpDimension(start=(0, 0), end=(1, 0), height=0, layer="F.Fab", dtype="radial"))
    x0, y0, x1, y1 = footprint_envelope(fp)
    # Knee 3.81 mm beyond the rim, shelf 1.27 mm, then the «R 1.00 mm» text.
    assert x1 > 1 + 3.81 + DIMENSION_SHELF_MM, "the shelf and the value are inside the envelope"
    assert y0 < plain[1] or y0 == pytest.approx(-DIMENSION_ARROW_LENGTH_MM), "the centre cross counts"


def test_the_envelope_counts_the_value_of_an_aligned_dimension_above_its_crossbar():
    fp = Footprint(name="T", attr="smd")
    fp.add_graphic(FpDimension(start=(0, 5), end=(4, 5), height=1.0, layer="F.Fab"))
    _x0, y0, _x1, _y1 = footprint_envelope(fp)
    # KiCad puts the value above the rightward crossbar (y = 6): centre
    # 4.85, box top about 4.0; the two points alone would stop at y = 5.
    assert y0 < 4.5, "the value box reaches above the measured points"
    other = Footprint(name="T", attr="smd")
    other.add_graphic(FpDimension(start=(0, 5), end=(4, 5), height=1.0, layer="Cmts.User"))
    assert footprint_envelope(other) is None, "a dimension off the envelope layers does not count"


def test_reference_and_value_clear_a_leader_shelf_on_the_fabrication_layer():
    fp = Footprint(name="T", attr="smd")
    fp.add_graphic(FpRect(start=(-1, -1), end=(1, 1), layer="F.Fab", width=0.1))
    before = default_text_positions(fp)
    fp.add_graphic(FpDimension(start=(0, 0), end=(0, -3), height=0, layer="F.Fab", dtype="leader",
                               text="NOTE", frame="rect"))
    after = default_text_positions(fp)
    assert after["Reference"][1] < before["Reference"][1], "the Reference moves above the leader's frame"


# ── KiCad itself ───────────────────────────────────────────────────────────

def _cli():
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


def _run(args):
    return subprocess.run(args, capture_output=True, timeout=120, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _svg_groups(svg: str) -> dict:
    """Stroke width -> (segments, circles) of every SVG group, page coordinates."""
    groups = {}
    for match in re.finditer(r"stroke-width:([\d.]+);[^>]*>(.*?)</g>", svg, re.S):
        segments, circles = groups.setdefault(round(float(match.group(1)), 4), ([], []))
        for path in re.finditer(r'<path d="([^"]*)"', match.group(2)):
            points = [(float(a), float(b)) for a, b in re.findall(r"[ML]\s*([-\d.]+)[ ,]([-\d.]+)", path.group(1))]
            segments.extend(zip(points, points[1:]))
        circles.extend((float(a), float(b), float(r)) for a, b, r in
                       re.findall(r'<circle cx="([-\d.]+)" cy="([-\d.]+)" r="([-\d.]+)"', match.group(2)))
    return groups


def _same_segment(a, b) -> bool:
    return any(math.dist(a[0], p) <= KICAD_TOLERANCE_MM and math.dist(a[1], q) <= KICAD_TOLERANCE_MM
               for p, q in ((b[0], b[1]), (b[1], b[0])))


def test_kicad_draws_the_leader_frames_and_shelves_the_program_computes(tmp_path):
    """Rectangle and circle frames for several texts, an empty text and a radial: every stroke within 0.03 mm."""
    cli = _cli()
    records = []
    for i, (text, frame) in enumerate([("ABC", "rect"), ("x", "rect"), ("R 2.50 mm", "rect"), ("Hg", "circle"),
                                       ("Pin 1", "circle"), ("", "rect"), ("", "none")]):
        y = 8.0 * i
        records.append({"kind": "dimension", "layer": "Cmts.User", "width": round(0.1 + 0.002 * i, 4),
                        "points": [[0, y], [2, y - 1]], "dtype": "leader", "text": text, "frame": frame})
    records.append({"kind": "dimension", "layer": "Cmts.User", "width": 0.2, "points": [[30, 0], [30, -3]],
                    "dtype": "radial", "leader_length": 1.5})
    clean = validate_footprint_drawings(records)
    assert len(clean) == len(records)
    fp = Footprint(name="G")
    # Origin marker: a stroke of a width no dimension uses.
    fp.add_graphic(FpLine(start=(0, 0), end=(0.5, 0), layer="Cmts.User", width=0.077))
    for item in drawing_graphics(clean):
        fp.add_graphic(item)
    library = tmp_path / "g.pretty"
    library.mkdir()
    (library / "G.kicad_mod").write_text(fp.render(), encoding="utf-8")
    out = tmp_path / "svg"
    out.mkdir()
    done = _run([cli, "fp", "export", "svg", "--layers", "Cmts.User", "-o", str(out), str(library)])
    svgs = list(out.glob("*.svg"))
    # kicad-cli may return 0 without writing: the file is the evidence.
    assert svgs, done.stderr
    groups = _svg_groups(svgs[0].read_text(encoding="utf-8"))
    (ox, oy), _ = groups[0.077][0][0]

    for record, item in zip(clean, fp.graphics[1:]):
        segments, circles = groups[round(record["width"], 4)]
        kicad = [((a[0] - ox, a[1] - oy), (b[0] - ox, b[1] - oy)) for a, b in segments]
        ours = item.ink_segments()
        assert len(kicad) == len(ours), (record, kicad)
        for segment in ours:
            assert any(_same_segment(segment, k) for k in kicad), (record, segment, kicad)
        circle = item.frame_circle()
        if circle is None:
            assert not circles
        else:
            (cx, cy, r), = circles
            assert math.dist((cx - ox, cy - oy), circle[0]) <= KICAD_TOLERANCE_MM
            assert r == pytest.approx(circle[1], abs=KICAD_TOLERANCE_MM)
        if record.get("dtype") == "leader":
            shelf = ours[3]
            assert math.dist(*shelf) == pytest.approx(DIMENSION_SHELF_MM), "the shelf is the declared length"


def test_kicad_recomputes_the_aligned_and_orthogonal_value_where_the_file_puts_it(tmp_path):
    cli = _cli()
    cases = [((0, 0), (4, 0), 1.0, ""), ((0, 10), (4, 10), -1.0, ""), ((0, 20), (0, 24), 1.0, ""),
             ((10, 0), (13, 4), 2.0, ""), ((10, 10), (7, 14), -2.0, ""), ((20, 0), (24, 1), -1.5, "x"),
             ((21, 14), (20, 10), 2.0, "y")]
    fp = Footprint(name="U")
    for start, end, height, axis in cases:
        fp.add_graphic(FpDimension(start=start, end=end, height=height, layer="Cmts.User", axis=axis))
    library = tmp_path / "u.pretty"
    library.mkdir()
    written = fp.render()
    (library / "U.kicad_mod").write_text(written, encoding="utf-8")
    out = tmp_path / "up.pretty"
    done = _run([cli, "fp", "upgrade", "--force", "-o", str(out), str(library)])
    upgraded = list(out.glob("*.kicad_mod"))
    assert upgraded, done.stderr
    pattern = r'\(gr_text "[^"]*"\s*\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)'
    ours = re.findall(pattern, written)
    theirs = re.findall(pattern, upgraded[0].read_text(encoding="utf-8"))
    assert len(ours) == len(theirs) == len(cases)
    # KiCad may reorder the items: pair each written text with KiCad's nearest.
    theirs = [(float(x), float(y), float(a or 0)) for x, y, a in theirs]
    for x, y, a in ours:
        kx, ky, ka = min(theirs, key=lambda t: math.dist((t[0], t[1]), (float(x), float(y))))
        assert math.dist((kx, ky), (float(x), float(y))) <= 1e-3, ((x, y), theirs)
        assert float(a or 0) == pytest.approx(ka, abs=1e-2)


def test_a_text_on_the_page_is_not_a_dimension_for_the_envelope():
    """Negative twin of the envelope tests: a plain text item still adds nothing."""
    fp = Footprint(name="T", attr="smd")
    fp.add_graphic(FpRect(start=(-1, -1), end=(1, 1), layer="F.Fab", width=0.1))
    plain = footprint_envelope(fp)
    fp.add_graphic(FpText(kind="user", text="LONG NOTE", at=(20, 0), layer="F.Fab"))
    assert footprint_envelope(fp) == plain
