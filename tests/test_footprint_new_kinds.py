"""@brief New footprint drawing kinds: Bezier curve, text box, radial/centre/leader dimensions (plan D2-D4).

Records round-trip through `validate_footprint_drawings`, unknown keys are
dropped, every kind turns and mirrors, the S-expression has KiCad 10's shape
(taken from KiCad's own writer `pcb_io_kicad_sexpr.cpp`) and, when kicad-cli
is installed, KiCad loads the file and draws its ink.
"""
import math
import re
import subprocess

import pytest

from kicadgen.core.drawing_transform import rotate_points_90
from kicadgen.core.footprint import (DIMENSION_ARROW_LENGTH_MM, DIMENSION_RADIAL_LEADER_MM, DIMENSION_SHELF_MM,
                                     FpCurve, FpDimension, Footprint, bezier_bounds, bezier_point,
                                     center_dimension_ink, dimension_label, drawing_bounds, drawing_centre,
                                     drawing_graphics, leader_dimension_frame_circle, leader_dimension_ink,
                                     leader_dimension_label_pose, mirror_drawing, radial_dimension_ink,
                                     radial_dimension_knee, radial_dimension_label_pose, radial_dimension_text,
                                     record_dimension, resize_drawing, textbox_margin, translate_drawing,
                                     turn_drawing, validate_footprint_drawings)
from kicadgen.services.system_dependencies import find_kicad_cli

CURVE = {"kind": "curve", "layer": "F.SilkS", "width": 0.12,
         "points": [[0.0, 0.0], [1.0, 2.0], [3.0, 2.0], [4.0, 0.0]]}
TEXTBOX = {"kind": "textbox", "layer": "Dwgs.User", "width": 0.1, "points": [[0.0, 5.0], [10.0, 9.0]],
           "text": "Hello", "size": 1.0}
RADIAL = {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[0.0, -10.0], [2.0, -12.0]],
          "dtype": "radial", "leader_length": 3.81}
CENTER = {"kind": "dimension", "layer": "Cmts.User", "width": 0.1, "points": [[-10.0, 0.0], [-9.0, 0.0]],
          "dtype": "center"}
LEADER = {"kind": "dimension", "layer": "Cmts.User", "width": 0.1, "points": [[-10.0, 10.0], [-7.0, 7.0]],
          "dtype": "leader", "text": "Pin 1", "frame": "circle"}
NEW = (CURVE, TEXTBOX, RADIAL, CENTER, LEADER)
OLD_DIMENSION = {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[-30.0, -30.0], [-20.0, -30.0]],
                 "offset": 2.0}


def render(records) -> str:
    fp = Footprint(name="T")
    for item in drawing_graphics(validate_footprint_drawings(records)):
        fp.add_graphic(item)
    return fp.render()


def strip_uuids(text: str) -> str:
    return re.sub(r'\(uuid "[^"]+"\)', "(uuid)", text)


# ── Records ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("record", NEW, ids=lambda r: r.get("dtype", r["kind"]))
def test_every_new_kind_round_trips_unchanged_and_drops_unknown_keys(record):
    assert validate_footprint_drawings([record]) == [record]
    assert validate_footprint_drawings(validate_footprint_drawings([record])) == [record], "idempotent"
    assert validate_footprint_drawings([{**record, "colour": "red", "junk": [1]}]) == [record]


def test_a_curve_needs_four_points_not_all_equal():
    assert validate_footprint_drawings([{**CURVE, "points": CURVE["points"][:3]}]) == []
    assert validate_footprint_drawings([{**CURVE, "points": [[1, 1]] * 4}]) == []
    assert validate_footprint_drawings([{**CURVE, "fill": True}]) == [CURVE], "a curve is never filled"


def test_a_textbox_needs_an_area_a_text_and_valid_options():
    assert validate_footprint_drawings([{**TEXTBOX, "points": [[0, 5], [0, 9]]}]) == [], "no width"
    assert validate_footprint_drawings([{**TEXTBOX, "text": "  "}]) == []
    assert validate_footprint_drawings([{**TEXTBOX, "text": "a\rb"}]) == []
    assert validate_footprint_drawings([{**TEXTBOX, "text": "two\nlines"}])[0]["text"] == "two\nlines"
    assert validate_footprint_drawings([{**TEXTBOX, "border": "no"}]) == []
    assert validate_footprint_drawings([{**TEXTBOX, "border": True}]) == [TEXTBOX], "default not stored"
    assert validate_footprint_drawings([{**TEXTBOX, "border": False}])[0]["border"] is False
    assert validate_footprint_drawings([{**TEXTBOX, "angle": 270}])[0]["angle"] == 90
    assert validate_footprint_drawings([{**TEXTBOX, "angle": 180}]) == [TEXTBOX], "180 is upright 0"
    assert validate_footprint_drawings([{**TEXTBOX, "angle": 45}]) == []


def test_dimension_types_are_checked_and_the_linear_ones_stay_the_old_record():
    assert validate_footprint_drawings([OLD_DIMENSION]) == [OLD_DIMENSION]
    assert validate_footprint_drawings([{**OLD_DIMENSION, "dtype": "aligned"}]) == [OLD_DIMENSION], \
        "an explicit aligned type is the old record, not stored"
    ortho = {**OLD_DIMENSION, "points": [[0.0, 0.0], [10.0, 3.0]], "axis": "x"}
    assert validate_footprint_drawings([{**ortho, "dtype": "orthogonal"}]) == [ortho]
    assert validate_footprint_drawings([{**OLD_DIMENSION, "dtype": "orthogonal"}]) == [], "orthogonal needs an axis"
    assert validate_footprint_drawings([{**ortho, "dtype": "aligned"}]) == [], "aligned with an axis contradicts"
    assert validate_footprint_drawings([{**OLD_DIMENSION, "dtype": "spiral"}]) == []
    # Fields of the other types are dropped.
    assert validate_footprint_drawings([{**RADIAL, "offset": 3.0, "axis": "x"}]) == [RADIAL]
    assert validate_footprint_drawings([{**CENTER, "leader_length": 2.0, "text": "x"}]) == [CENTER]


def test_radial_and_leader_fields():
    no_length = {k: v for k, v in RADIAL.items() if k != "leader_length"}
    assert validate_footprint_drawings([no_length])[0]["leader_length"] == DIMENSION_RADIAL_LEADER_MM
    assert validate_footprint_drawings([{**RADIAL, "leader_length": 0}]) == []
    assert validate_footprint_drawings([{**RADIAL, "leader_length": True}]) == []
    plain = {k: v for k, v in LEADER.items() if k not in ("text", "frame")}
    assert validate_footprint_drawings([plain])[0] == {**plain, "text": ""}, "empty text, no frame"
    assert validate_footprint_drawings([{**LEADER, "frame": "none"}]) == [{**plain, "text": "Pin 1"}]
    assert validate_footprint_drawings([{**LEADER, "frame": "hexagon"}]) == []
    assert validate_footprint_drawings([{**LEADER, "text": "a\nb"}]) == []


# ── Transforms ─────────────────────────────────────────────────────────────

def _close(a, b):
    return all(x == pytest.approx(y, abs=2e-4) for pa, pb in zip(a, b) for x, y in zip(pa, pb))


@pytest.mark.parametrize("record", NEW, ids=lambda r: r.get("dtype", r["kind"]))
def test_turning_twice_is_a_half_turn_and_mirroring_twice_is_the_identity(record):
    twice = turn_drawing(turn_drawing(record, True), True)
    half = rotate_points_90(rotate_points_90(record["points"], drawing_centre(record), True),
                            drawing_centre(record), True)
    assert _close(twice["points"], half)
    assert validate_footprint_drawings([twice]), "the turned record stays valid"
    assert _close(mirror_drawing(mirror_drawing(record))["points"], record["points"])
    assert mirror_drawing(mirror_drawing(record)) == record
    moved = translate_drawing(record, 1.5, -2.0)
    assert _close(moved["points"], [[x + 1.5, y - 2.0] for x, y in record["points"]])
    assert {k: v for k, v in moved.items() if k != "points"} == {k: v for k, v in record.items() if k != "points"}


def test_a_textbox_turns_its_text_with_it_and_a_mirror_keeps_the_text():
    turned = turn_drawing(TEXTBOX, False)
    assert turned["angle"] == 90 and "angle" not in turn_drawing(turned, False)
    x0, y0, x1, y1 = drawing_bounds(turned)
    assert (x1 - x0, y1 - y0) == pytest.approx((4.0, 10.0)), "the box is turned, still axis aligned"
    assert mirror_drawing(TEXTBOX).get("angle") is None and mirror_drawing(TEXTBOX)["text"] == "Hello"


def test_radial_and_centre_turn_about_what_they_mark_and_resize_as_a_radius():
    assert drawing_centre(RADIAL) == (0.0, -10.0) and drawing_centre(CENTER) == (-10.0, 0.0)
    assert turn_drawing(RADIAL, True)["points"][0] == [0.0, -10.0]
    bigger = resize_drawing(RADIAL, radius=5.0)
    assert math.dist(*bigger["points"]) == pytest.approx(5.0, abs=1e-3) and bigger["points"][0] == RADIAL["points"][0]
    assert math.dist(*resize_drawing(CENTER, radius=2.0)["points"]) == pytest.approx(2.0, abs=1e-3)
    assert resize_drawing(LEADER, radius=5.0) == LEADER, "a leader has no radius"
    box = resize_drawing(TEXTBOX, width=6.0)
    assert drawing_bounds(box) == pytest.approx((2.0, 5.0, 8.0, 9.0)), "centre kept"


def test_the_curve_box_is_the_drawn_curve_not_the_control_polygon():
    x0, y0, x1, y1 = drawing_bounds(CURVE)
    assert (x0, x1) == (0.0, 4.0)
    assert y1 == pytest.approx(1.5), "peak of the symmetric curve: 0.75 x control height"
    assert y1 < 2.0, "the control points at y=2 are outside the curve"
    assert bezier_bounds([(0, 0), (1, 0), (2, 0), (3, 0)]) == (0.0, 0.0, 3.0, 0.0)
    assert bezier_point(CURVE["points"], 0.5) == pytest.approx((2.0, 1.5))


# ── Geometry shared by writer, checks and preview ──────────────────────────

def test_radial_geometry_knee_shelf_and_arrow():
    knee = radial_dimension_knee((0, 0), (3, 4), 5.0)
    assert knee == pytest.approx((6.0, 8.0))
    ink = radial_dimension_ink((0, 0), (3, 4), 5.0, 1.0)
    assert ((0.0, -DIMENSION_ARROW_LENGTH_MM), (0.0, DIMENSION_ARROW_LENGTH_MM)) in ink, "centre cross"
    assert ((3.0, 4.0), knee) in ink
    assert (knee, (knee[0] + DIMENSION_SHELF_MM, knee[1])) in ink, "shelf to the right"
    assert len(ink) == 6
    left = radial_dimension_ink((0, 0), (-3, 4), 5.0, 1.0)
    assert left[3][1][0] < left[3][0][0], "a radius pointing left puts the shelf on the left"
    (x, y), angle = radial_dimension_label_pose((0, 0), (3, 4), 5.0, 1.0)
    assert y == knee[1] and x > knee[0] + DIMENSION_SHELF_MM and angle == 0.0
    assert radial_dimension_text((0, 0), (3, 4)) == "R 5.00 mm"


def test_centre_mark_is_two_crossing_arms():
    ink = center_dimension_ink((0, 0), (2, 0))
    assert {frozenset(seg) for seg in ink} == {frozenset({(-2.0, 0.0), (2.0, 0.0)}), frozenset({(0.0, -2.0), (0.0, 2.0)})}


def test_leader_text_goes_away_from_the_tip_and_its_frame_follows_the_type():
    ink = leader_dimension_ink((0, 0), (3, -3), "A", 1.0)
    assert ink[0] == ((0.0, 0.0), (3.0, -3.0)) and len(ink) == 4, "leader, two arrow strokes, shelf"
    framed = leader_dimension_ink((0, 0), (3, -3), "A", 1.0, "rect")
    assert len(framed) == 8, "plus four frame sides"
    assert len(leader_dimension_ink((0, 0), (3, -3), "", 1.0, "rect")) == 4, "no frame around an empty text"
    assert leader_dimension_frame_circle((0, 0), (3, -3), "A", 1.0, "circle") is not None
    assert leader_dimension_frame_circle((0, 0), (3, -3), "A", 1.0, "rect") is None
    (right, _y), _ = leader_dimension_label_pose((0, 0), (3, -3), "A", 1.0)
    (left, _y), _ = leader_dimension_label_pose((6, 0), (3, -3), "A", 1.0)
    assert right > 3.0 > left


def test_record_dimension_and_dimension_label_cover_every_type():
    assert dimension_label(record_dimension(CENTER)) is None
    assert dimension_label(record_dimension(LEADER))[0] == "Pin 1"
    assert dimension_label(record_dimension(RADIAL))[0] == "R 2.83 mm"
    assert dimension_label(record_dimension(OLD_DIMENSION))[0] == "10.00 mm"


def test_arrowheads_have_two_distinct_strokes_on_a_horizontal_dimension():
    """Regression: the old formula drew the same stroke twice when the crossbar was horizontal.

    The direction of each wing, for vertical and slanted dimensions too, is
    checked against KiCad's `drawAnArrow` in
    `test_footprint_dimension_kicad_geometry.test_both_wings_of_both_arrowheads_point_as_kicad_draws_them`.
    """
    from kicadgen.core.footprint import dimension_ink_segments
    strokes = dimension_ink_segments((0, 0), (10, 0), 2.0)[3:]
    ends = {tuple(round(v, 6) for v in seg[1]) for seg in strokes}
    assert len(ends) == 4


# ── Writer ─────────────────────────────────────────────────────────────────

def test_the_curve_and_textbox_s_expressions_have_kicad_10_shape():
    text = render([CURVE, TEXTBOX])
    assert re.search(r"\(fp_curve\s+\(pts\s+\(xy 0 0\)\s+\(xy 1 2\)\s+\(xy 3 2\)\s+\(xy 4 0\)\s*\)\s+"
                     r"\(stroke\s+\(width 0\.12\)\s+\(type solid\)\s*\)\s+\(layer \"F\.SilkS\"\)", text)
    margin = textbox_margin(0.1, 1.0)
    assert margin == 0.8
    assert re.search(r'\(fp_text_box "Hello"\s+\(start 0 5\)\s+\(end 10 9\)\s+\(margins 0\.8 0\.8 0\.8 0\.8\)\s+'
                     r'\(layer "Dwgs\.User"\)', text)
    assert "(border yes)" in text and "(knockout no)" in text and "(angle" not in text
    turned = render([{**TEXTBOX, "angle": 90, "border": False}])
    assert "(angle 90)" in turned and "(border no)" in turned


def test_the_typed_dimension_s_expressions_follow_kicads_writer():
    radial = render([RADIAL])
    assert "(type radial)" in radial and "(leader_length 3.81)" in radial and '(prefix "R ")' in radial
    assert "(keep_text_aligned yes)" in radial and '(gr_text "R 2.83 mm"' in radial and "(height" not in radial
    center = render([CENTER])
    assert "(type center)" in center and "(format" not in center and "gr_text" not in center
    leader = render([LEADER])
    assert "(type leader)" in leader and '(override_value "Pin 1")' in leader and "(text_frame 2)" in leader
    assert leader.index("(style") < leader.index("(gr_text"), "text after the style, as KiCad writes it"
    with pytest.raises(ValueError):
        FpDimension(start=(0, 0), end=(1, 1), height=0, layer="F.Fab", dtype="leader", frame="star").to_sexpr()
    with pytest.raises(ValueError):
        FpDimension(start=(0, 0), end=(1, 1), height=0, layer="F.Fab", dtype="bogus").to_sexpr()
    with pytest.raises(ValueError):
        FpCurve(points=[(0, 0), (1, 1)], layer="F.SilkS").to_sexpr()


#: What commit bd83c9a (before the new kinds) wrote for an aligned record,
#: uuids masked: captured from that commit's writer, so the legacy path is
#: pinned byte for byte. ONE deliberate change (review C4, 08/11): the
#: gr_text position is now the one KiCad recomputes on load (`fp upgrade`
#: rewrites it to (-25, -29.15): crossbar middle moved by size + pen up the
#: page), where bd83c9a wrote (-25, -27), a position KiCad never kept.
OLD_ALIGNED_TEXT = (
    '\t(dimension\n\t\t(type aligned)\n\t\t(layer "F.Fab")\n\t\t(uuid)\n\t\t(pts\n\t\t\t(xy -30 -30)\n'
    '\t\t\t(xy -20 -30)\n\t\t)\n\t\t(height 2)\n\t\t(gr_text "10.00 mm"\n\t\t\t(at -25 -29.15)\n'
    '\t\t\t(layer "F.Fab")\n\t\t\t(uuid)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n'
    '\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t\t(format\n\t\t\t(units 2)\n'
    '\t\t\t(units_format 1)\n\t\t\t(precision 2)\n\t\t)\n\t\t(style\n\t\t\t(thickness 0.1)\n'
    '\t\t\t(arrow_length 1.27)\n\t\t\t(text_position_mode 0)\n\t\t\t(extension_height 0.58642)\n'
    '\t\t\t(extension_offset 0)\n\t\t\tkeep_text_aligned\n\t\t)\n\t)\n)\n')


def test_old_records_are_written_byte_for_byte_as_before():
    text = strip_uuids(render([OLD_DIMENSION]))
    assert text[text.index("\t(dimension"):] == OLD_ALIGNED_TEXT
    explicit = strip_uuids(render([{**OLD_DIMENSION, "dtype": "aligned"}]))
    assert explicit == text, "an explicit aligned type writes the very same file"
    line = {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 0], [1, 0]]}
    assert validate_footprint_drawings([line]) == [{**line, "points": [[0.0, 0.0], [1.0, 0.0]]}]


# ── KiCad itself ───────────────────────────────────────────────────────────

def test_kicad_loads_every_new_kind_and_draws_its_ink(tmp_path):
    """kicad-cli read-only export of a footprint holding one of each new item."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    library = tmp_path / "kinds.pretty"
    library.mkdir()
    (library / "T.kicad_mod").write_text(render(list(NEW)), encoding="utf-8")
    out = tmp_path / "svg"
    out.mkdir()
    done = subprocess.run([cli, "fp", "export", "svg", "--fp", "T", "--layers",
                           "F.SilkS,F.Fab,Cmts.User,Dwgs.User", "-o", str(out), str(library)],
                          capture_output=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    svgs = list(out.glob("*.svg"))
    assert svgs, "kicad-cli exported the footprint"
    svg = svgs[0].read_text(encoding="utf-8")
    assert re.search(r'd="M[\d.]+,[\d.]+ C', svg), "the Bezier is drawn as a cubic path"
    assert "<desc>Hello</desc>" in svg, "the text box text"
    assert "<desc>Pin 1</desc>" in svg, "the leader text"
    assert re.search(r"<desc>R 2[.,]83 mm</desc>", svg), "the radial value"
    assert "<circle" in svg, "the leader's circle frame"
    assert "stroke:#C2C2C2" in svg, "ink on Dwgs.User (the text box and its border)"


# ── Review fixes F1 (08/11): malformed fields, text box text, polygon area, margins ──

#: Values of the wrong type a corrupted project or draft may hold.
MALFORMED = ([], {}, ["rect"], 3, None)


@pytest.mark.parametrize("value", MALFORMED, ids=repr)
@pytest.mark.parametrize("field, record", [
    ("kind", CURVE), ("layer", CURVE), ("dtype", RADIAL), ("frame", LEADER), ("axis", OLD_DIMENSION),
    ("anchor", {"kind": "text", "layer": "F.SilkS", "width": 0.12, "points": [[0.0, 0.0]], "text": "A",
                "size": 1.0, "anchor": "left_bottom"}),
    ("text", TEXTBOX), ("border", TEXTBOX), ("angle", TEXTBOX), ("leader_length", RADIAL),
    ("points", CURVE), ("width", CURVE), ("fill", CURVE),
], ids=lambda v: v if isinstance(v, str) else None)
def test_a_field_of_the_wrong_type_drops_the_record_without_raising(field, record, value):
    """M8: loading never raises; a None means «absent» only where the field is optional."""
    kept = validate_footprint_drawings([{**record, field: value}, record])
    assert kept[-1] == record, "the well-formed record after it survives"
    assert len(kept) in (1, 2)
    if len(kept) == 2:
        # Only an optional field set to None may read as absent, and the
        # number 3 is a valid width or leader length (3 mm), not a malformed one.
        assert ((value is None and field in ("dtype", "anchor", "angle"))
                or (value == 3 and field in ("width", "leader_length")))


@pytest.mark.parametrize("raw", [[[]], [{}], [["rect"]], [3], [None], 3, None, "x"], ids=repr)
def test_a_record_that_is_not_a_dict_is_skipped(raw):
    assert validate_footprint_drawings(raw) == []
    if isinstance(raw, list):
        assert validate_footprint_drawings(raw + [CURVE]) == [CURVE]


def test_a_textbox_keeps_its_leading_indentation_and_drops_only_trailing_newlines():
    """C5: blank is judged on the stripped text, the stored text loses only its trailing newlines."""
    kept = validate_footprint_drawings([{**TEXTBOX, "text": "  indented\n  second\n\n"}])
    assert kept[0]["text"] == "  indented\n  second"
    assert validate_footprint_drawings([{**TEXTBOX, "text": " \n \n"}]) == [], "only white space is blank"
    assert validate_footprint_drawings([{**TEXTBOX, "text": "Hello"}]) == [TEXTBOX]


POLYGON = {"kind": "polygon", "layer": "F.SilkS", "width": 0.12}


def test_a_polygon_without_area_is_dropped_but_a_thin_or_crossed_one_is_kept():
    """P5: collinear vertices enclose nothing; a bow tie and a thin triangle do enclose an area."""
    collinear = {**POLYGON, "points": [[0.0, 0.0], [5.0, 0.0], [10.0, 0.0], [2.0, 0.0]]}
    slanted_collinear = {**POLYGON, "points": [[0.0, 0.0], [1.0, 1.0], [3.0, 3.0]]}
    bow_tie = {**POLYGON, "points": [[0.0, 0.0], [2.0, 2.0], [2.0, 0.0], [0.0, 2.0]]}
    thin = {**POLYGON, "points": [[0.0, 0.0], [10.0, 0.0], [5.0, 0.001]]}
    assert validate_footprint_drawings([collinear]) == []
    assert validate_footprint_drawings([slanted_collinear]) == []
    assert validate_footprint_drawings([bow_tie]) == [bow_tie]
    assert validate_footprint_drawings([thin]) == [thin]


def test_a_vertex_within_the_stored_rounding_of_the_line_proves_no_area():
    """The resolution is the 4-decimal storage: 0.0001 mm off the line is rounding, 0.0002 mm is an area."""
    off_by_rounding = {**POLYGON, "points": [[0.0, 0.0], [10.0, 0.0], [5.0, 0.0001]]}
    off_by_two = {**POLYGON, "points": [[0.0, 0.0], [10.0, 0.0], [5.0, 0.0002]]}
    assert validate_footprint_drawings([off_by_rounding]) == []
    assert validate_footprint_drawings([off_by_two]) == [off_by_two]


@pytest.mark.parametrize("width, size, margin", [(0.1, 1.0, 0.8), (0.2, 2.0, 1.6), (0.15, 0.8, 0.675)])
def test_the_textbox_margin_is_half_the_border_plus_three_quarters_of_the_size(width, size, margin):
    """G12: a second and third (width, size) pair, so the formula is not one coincident number."""
    assert textbox_margin(width, size) == pytest.approx(margin, abs=1e-9)
    assert textbox_margin(width, size) != pytest.approx(width + size * 0.75), "half the border, not all of it"
