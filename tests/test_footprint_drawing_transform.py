"""@brief Turn, mirror, resize and text angle of footprint drawing records (plan docs/canvas-menus-plan.md, §2).

Pure model tests: `core.footprint` turns and mirrors a record with the shared
`core.drawing_transform` geometry; a text's optional ``angle`` is validated
and written as ``(at x y 90)``, and a record without it is written exactly
as before.
"""
import re

import pytest

from kicadgen.core.footprint import (
    FpText, USER_TEXT_ANCHOR, arc_centre_representable, drawing_bounds, drawing_centre,
    drawing_graphics, mirror_drawing, resize_drawing, translate_drawing, turn_drawing,
    validate_footprint_drawings,
)


def record(kind="line", points=((0, 0), (5, 5)), layer="F.SilkS", **extra):
    """A validated drawing record."""
    base = {"kind": kind, "layer": layer, "width": 0.12, "points": [list(p) for p in points]}
    base.update(extra)
    clean = validate_footprint_drawings([base])
    assert clean, base
    return clean[0]


def _no_uuid(node):
    """The S-expression text without its random uuid."""
    return re.sub(r'\(uuid "[^"]*"\)', "(uuid)", str(node))


SHAPES = [record(points=((0, 0), (3, 1), (3, 4))),
          record("rectangle", ((1, 1), (4, 3))),
          record("circle", ((2, 2), (3, 2))),
          record("arc", ((0, 0), (1, 1), (2, 0)))]


# ── Turn ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("shape", SHAPES, ids=lambda r: r["kind"])
@pytest.mark.parametrize("ccw", [True, False])
def test_four_turns_give_the_record_back(shape, ccw):
    turned = shape
    for _ in range(4):
        turned = validate_footprint_drawings([turn_drawing(turned, ccw)])[0]
    assert turned == shape


@pytest.mark.parametrize("shape", SHAPES, ids=lambda r: r["kind"])
def test_a_turn_keeps_the_bounding_box_centre(shape):
    turned = turn_drawing(shape, True)
    assert drawing_centre(turned) == pytest.approx(drawing_centre(shape), abs=1e-4)


def test_counter_clockwise_on_screen_sends_a_point_right_of_centre_up():
    """Y grows down: «antiorario» moves (cx + d, cy) to (cx, cy - d); clockwise to (cx, cy + d)."""
    line = record(points=((0, 0), (4, 0)))
    assert turn_drawing(line, True)["points"] == [[2, 2], [2, -2]]
    assert turn_drawing(line, False)["points"] == [[2, -2], [2, 2]]


def test_a_turned_rectangle_stays_axis_aligned_and_swaps_its_sides():
    rect = record("rectangle", ((1, 1), (5, 3)))
    turned = validate_footprint_drawings([turn_drawing(rect, True)])
    assert turned, "still a valid (non-flat) rectangle"
    (x0, y0), (x1, y1) = turned[0]["points"]
    assert (abs(x1 - x0), abs(y1 - y0)) == pytest.approx((2, 4))


def test_a_circle_turns_about_its_own_centre():
    circle = record("circle", ((2, 2), (3, 2)))
    turned = turn_drawing(circle, True)
    assert turned["points"][0] == [2, 2], "the centre is the circle's box centre, not the midpoint of its two points"


@pytest.mark.parametrize("operation", [lambda r: turn_drawing(r, True), lambda r: turn_drawing(r, False),
                                       mirror_drawing], ids=["ccw", "cw", "mirror"])
def test_an_arc_stays_a_valid_arc_of_the_same_radius(operation):
    arc = record("arc", ((0, 0), (1, 1), (2, 0)))
    moved = validate_footprint_drawings([operation(arc)])
    assert moved and moved[0]["kind"] == "arc"
    assert arc_centre_representable(*moved[0]["points"])
    x0, y0, x1, y1 = drawing_bounds(moved[0])
    assert sorted((round(x1 - x0, 4), round(y1 - y0, 4))) == [1, 2], "a half circle of radius 1"


def test_the_arc_bounds_are_the_drawn_arcs_not_its_three_points():
    # Start, mid, end of a three-quarter arc: the drawn arc reaches x = -1 and
    # y = -1 although none of the stored points does.
    arc = record("arc", ((1, 0), (0, 1), (0, -1)))
    assert drawing_bounds(arc) == pytest.approx((-1, -1, 1, 1))
    # Negative twin: the same circle's short arc through (1, 0)...(0, 1) stays in its quadrant.
    short = record("arc", ((1, 0), (0.7071, 0.7071), (0, 1)))
    assert drawing_bounds(short) == pytest.approx((0, 0, 1, 1), abs=1e-3)


# ── Mirror ─────────────────────────────────────────────────────────────

def test_mirror_is_left_right_about_the_box_centre_and_twice_is_identity():
    line = record(points=((0, 0), (4, 2)))
    mirrored = mirror_drawing(line)
    assert mirrored["points"] == [[4, 0], [0, 2]]
    assert mirror_drawing(mirrored) == line


def test_mirror_leaves_a_text_unchanged():
    text = record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)
    assert mirror_drawing(text) == text


# ── Text angle ─────────────────────────────────────────────────────────

def test_turning_a_text_toggles_its_angle_and_keeps_its_anchor():
    text = record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)
    for ccw in (True, False):
        turned = turn_drawing(text, ccw)
        assert turned["angle"] == 90 and turned["points"] == [[1, 1]]
        assert turn_drawing(turned, ccw) == text, "a second turn is horizontal again: no field"


@pytest.mark.parametrize("angle", [90, 90.0])
def test_validation_keeps_a_quarter_turned_text(angle):
    clean = record("text", ((1, 1),), text="HI", size=1.0, angle=angle)
    assert clean["angle"] == 90 and isinstance(clean["angle"], int)


def test_validation_stores_angle_zero_as_no_field():
    assert "angle" not in record("text", ((1, 1),), text="HI", size=1.0, angle=0)
    assert "angle" not in record("text", ((1, 1),), text="HI", size=1.0), "absent stays absent"


@pytest.mark.parametrize("bad", [45, 180, 270, -90, True, "90", float("nan"), [90]])
def test_validation_drops_a_text_with_any_other_angle(bad):
    raw = {"kind": "text", "layer": "F.SilkS", "width": 0.12, "points": [[1, 1]],
           "text": "HI", "size": 1.0, "angle": bad}
    assert validate_footprint_drawings([raw]) == []


def test_angle_on_a_shape_is_ignored():
    assert "angle" not in record(angle=90)


def test_a_turned_text_is_written_at_x_y_90():
    text = record("text", ((1, 2),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR, angle=90)
    (item,) = drawing_graphics([text])
    assert item.angle == 90
    assert "(at 1 2 90)" in str(item.to_sexpr())


def test_a_text_without_angle_is_written_byte_identical_to_before():
    """Output before the field existed: `(at x y)`, nothing else changed."""
    text = record("text", ((1, 2),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)
    (item,) = drawing_graphics([text])
    before = FpText(kind="user", text="HI", at=(1, 2), layer="F.SilkS", size=1.0,
                    justify=("left", "bottom"))
    assert _no_uuid(item.to_sexpr()) == _no_uuid(before.to_sexpr())
    assert "(at 1 2)" in str(item.to_sexpr()) and "(at 1 2 " not in str(item.to_sexpr())


# ── Translate and resize ──────────────────────────────────────────────

def test_translate_moves_every_point_and_returns_a_copy():
    line = record(points=((0, 0), (1, 1)))
    moved = translate_drawing(line, 0.5, -0.25)
    assert moved["points"] == [[0.5, -0.25], [1.5, 0.75]] and line["points"] == [[0, 0], [1, 1]]


def test_resize_a_rectangle_about_its_centre_keeping_the_first_corner_side():
    rect = record("rectangle", ((4, 3), (0, 1)))
    resized = resize_drawing(rect, width=2.0)
    assert resized["points"] == [[3, 3], [1, 1]], "first corner stays the right-bottom one"
    assert resize_drawing(rect)["points"] == rect["points"], "no size given: unchanged"


def test_resize_ignores_a_size_that_does_not_apply_to_the_kind():
    line = record()
    assert resize_drawing(line, width=9.0, radius=3.0) == line
