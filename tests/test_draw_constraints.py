"""@brief Tests of the free / 45 / 90 degree angle constraint of the drawing tools."""
import math

import pytest

from kicadgen.core.draw_constraints import (ANGLE_MODES, AngleMode, constrain, constrain_to_grid, next_mode,
                                            polygon_has_area)


def _polar(deg, length=10.0):
    """Point at `deg` degrees and `length` from the origin."""
    rad = math.radians(deg)
    return (length * math.cos(rad), length * math.sin(rad))


def _angle(p, anchor=(0.0, 0.0)):
    """Direction of anchor -> p in degrees."""
    return math.degrees(math.atan2(p[1] - anchor[1], p[0] - anchor[0]))


@pytest.mark.parametrize("deg, expected", [(40, 45), (20, 0), (-50, -45), (68, 90), (-170, 180), (130, 135)])
def test_deg45_snaps_to_nearest_multiple_of_45(deg, expected):
    """A direction goes to the nearest 45 degree ray."""
    out = constrain((0.0, 0.0), _polar(deg), AngleMode.DEG45)
    got = _angle(out)
    assert math.isclose(math.cos(math.radians(got - expected)), 1.0, abs_tol=1e-12)


def test_deg45_keeps_projected_length_and_moves_with_anchor():
    """The result is the pointer's projection on the ray, from any anchor."""
    anchor = (3.0, -7.0)
    p = _polar(40, 10.0)
    p = (p[0] + anchor[0], p[1] + anchor[1])
    out = constrain(anchor, p, "45")
    length = math.hypot(out[0] - anchor[0], out[1] - anchor[1])
    assert math.isclose(length, 10.0 * math.cos(math.radians(5)), rel_tol=1e-12)
    assert math.isclose(_angle(out, anchor), 45.0, abs_tol=1e-9)


def test_point_already_on_an_allowed_ray_is_unchanged():
    """Positive: on-ray points keep their full length; negative: off-ray ones shrink."""
    on_ray = _polar(45, 8.0)
    out = constrain((0.0, 0.0), on_ray, AngleMode.DEG45)
    assert math.isclose(out[0], on_ray[0], abs_tol=1e-12) and math.isclose(out[1], on_ray[1], abs_tol=1e-12)
    off = constrain((0.0, 0.0), _polar(30, 8.0), AngleMode.DEG45)
    assert math.hypot(*off) < 8.0


def test_deg90_projects_on_horizontal_or_vertical():
    """91 degrees is vertical, 20 degrees horizontal, 40 degrees in 90 mode stays horizontal."""
    v = constrain((1.0, 1.0), (1.0 + _polar(91)[0], 1.0 + _polar(91)[1]), AngleMode.DEG90)
    assert v[0] == 1.0 and v[1] > 1.0
    h = constrain((0.0, 0.0), _polar(20), AngleMode.DEG90)
    assert h[1] == 0.0 and math.isclose(h[0], 10.0 * math.cos(math.radians(20)))
    h40 = constrain((0.0, 0.0), _polar(40), AngleMode.DEG90)
    assert h40[1] == 0.0
    # The same 40 degrees is NOT constrained in free mode.
    free = constrain((0.0, 0.0), _polar(40), AngleMode.FREE)
    assert math.isclose(_angle(free), 40.0)


def test_exact_tie_goes_to_first_ray_in_order():
    """45 degrees exactly in 90 mode is a tie: horizontal (0 deg) is listed first."""
    assert constrain((0.0, 0.0), (5.0, 5.0), AngleMode.DEG90) == (5.0, 0.0)
    # The mirrored point ties between 0 and 270 degrees; 0 comes first again.
    assert constrain((0.0, 0.0), (5.0, -5.0), AngleMode.DEG90) == (5.0, 0.0)


def test_orientation_agnostic_under_y_flip():
    """Flipping Y on input flips Y on output (symbol Y up vs footprint Y down)."""
    for deg in (10, 40, 70, 100, 160, 200, 250, 300, 340):
        p = _polar(deg)
        a = constrain((0.0, 0.0), p, AngleMode.DEG45)
        b = constrain((0.0, 0.0), (p[0], -p[1]), AngleMode.DEG45)
        assert math.isclose(a[0], b[0], abs_tol=1e-12) and math.isclose(a[1], -b[1], abs_tol=1e-12)


def test_zero_length_returns_anchor():
    """A pointer on the anchor stays on the anchor in every mode."""
    for mode in ANGLE_MODES:
        assert constrain((2.0, 3.0), (2.0, 3.0), mode) == (2.0, 3.0)


def test_free_is_identity():
    """Free mode returns the pointer as floats."""
    assert constrain((0, 0), (3, 4.5), AngleMode.FREE) == (3.0, 4.5)


def test_cycle_order_and_unknown_mode():
    """Shift+Space cycles free -> 90 -> 45 -> free (KiCad order); an unknown mode is refused."""
    assert ANGLE_MODES == (AngleMode.FREE, AngleMode.DEG90, AngleMode.DEG45)
    assert next_mode(AngleMode.FREE) is AngleMode.DEG90
    assert next_mode("90") is AngleMode.DEG45
    assert next_mode(AngleMode.DEG45) is AngleMode.FREE
    with pytest.raises(ValueError):
        next_mode("30")
    with pytest.raises(ValueError):
        constrain((0, 0), (1, 1), "30")


# ── constrain_to_grid: the ONE "angle + grid" rule of both tabs (review M3) ──

def _on_ray(anchor, p, mode):
    """True when anchor -> p is exactly one of the allowed directions of `mode`."""
    dx, dy = p[0] - anchor[0], p[1] - anchor[1]
    if dx == 0.0 or dy == 0.0:
        return True
    return mode == AngleMode.DEG45 and math.isclose(abs(dx), abs(dy), rel_tol=0, abs_tol=1e-12)


def test_constrain_to_grid_review_table_off_grid_anchor():
    """The review's counter-examples: steps are counted FROM THE ANCHOR, not on the absolute grid."""
    # 90: horizontal ray, run 2.3 rounds to 2 steps from x = 0.3 (the absolute-grid rule gave 3.0).
    assert constrain_to_grid((0.3, 0.0), (2.6, 0.2), AngleMode.DEG90, 1.0) == pytest.approx((2.3, 0.0))
    # 45: the pointer is nearer the horizontal ray (1.7 vs 1.48 projected); 1.7 rounds to 2 steps.
    assert constrain_to_grid((0.5, 0.5), (2.2, 0.1), AngleMode.DEG45, 1.0) == pytest.approx((2.5, 0.5))


@pytest.mark.parametrize("mode", [AngleMode.DEG90, AngleMode.DEG45])
def test_constrain_to_grid_on_grid_anchor_gives_on_grid_points_on_the_ray(mode):
    """Positive: an on-grid anchor stays on grid and exactly on the ray for every pointer direction."""
    step = 0.5
    anchor = (1.5, -2.0)
    for deg in range(0, 360, 7):
        for length in (0.3, 1.1, 2.76, 7.9):
            p = _polar(deg, length)
            out = constrain_to_grid(anchor, (anchor[0] + p[0], anchor[1] + p[1]), mode, step)
            for value in out:
                assert math.isclose(value / step, round(value / step), abs_tol=1e-9), (deg, length, out)
            assert _on_ray(anchor, out, mode), (deg, length, out)


def test_constrain_to_grid_diagonal_steps_are_step_times_sqrt2_along_the_ray():
    """A diagonal advances one step on EACH axis: along the ray step * sqrt(2)."""
    out = constrain_to_grid((0.0, 0.0), _polar(45, 1.5 * math.sqrt(2) - 0.01), AngleMode.DEG45, 1.0)
    assert out == pytest.approx((1.0, 1.0))
    out = constrain_to_grid((0.0, 0.0), _polar(45, 1.5 * math.sqrt(2) + 0.01), AngleMode.DEG45, 1.0)
    assert out == pytest.approx((2.0, 2.0))
    # Negative: a horizontal ray advances by the bare step, not step * sqrt(2).
    assert constrain_to_grid((0.0, 0.0), (1.6, 0.1), AngleMode.DEG45, 1.0) == pytest.approx((2.0, 0.0))


def test_constrain_to_grid_without_a_step_is_the_plain_constraint():
    """step None / 0 / negative: no rounding, the projection on the ray."""
    for step in (None, 0.0, -1.0, float("nan")):
        assert constrain_to_grid((0.3, 0.0), (2.6, 0.2), "90", step) == constrain((0.3, 0.0), (2.6, 0.2), "90")
        assert constrain_to_grid((0.3, 0.0), (2.6, 0.2), "free", step) == (2.6, 0.2)


def test_constrain_to_grid_free_rounds_to_the_absolute_grid_with_its_origin():
    """FREE ignores the anchor and rounds each coordinate to grid_origin + k * step."""
    assert constrain_to_grid((0.3, 0.0), (2.6, 0.2), AngleMode.FREE, 1.0) == (3.0, 0.0)
    out = constrain_to_grid((0.3, 0.0), (2.6, 0.2), AngleMode.FREE, 1.0, grid_origin=(0.25, 0.5))
    assert out == pytest.approx((2.25, 0.5))


def test_constrain_to_grid_short_pointer_returns_the_anchor_and_ndigits_rounds():
    """Less than half a step from the anchor gives the anchor; ndigits rounds the stored decimals."""
    assert constrain_to_grid((0.3, 0.0), (0.7, 0.05), "90", 1.0) == (0.3, 0.0)
    out = constrain_to_grid((0.1, 0.2), (0.4, 0.5), "45", 0.1, ndigits=4)
    assert out == (0.4, 0.5)
    with pytest.raises(ValueError):
        constrain_to_grid((0, 0), (1, 1), "30", 1.0)


# ── polygon_has_area (review findings P5/C6/S3) ──

def test_polygon_collinear_after_rounding_has_no_area():
    """Negative: (0,0), (1, 1/3), (3, 1) are collinear; stored rounding pushes the middle off by < 1e-4."""
    assert not polygon_has_area([(0, 0), (1, round(1 / 3, 4)), (3, 1)])
    assert not polygon_has_area([(0, 0), (1, 1), (2, 2), (5, 5)])


def test_polygon_symmetric_bow_tie_has_area():
    """Positive: a self-intersecting bow tie (signed area 0) is accepted, as KiCad accepts it."""
    assert polygon_has_area([(0, 0), (2, 2), (2, 0), (0, 2)])


def test_polygon_thin_real_triangle_has_area():
    """Positive: a vertex 0.001 mm off the chord (ten storage units) is a real triangle."""
    assert polygon_has_area([(0, 0), (5, 0.001), (10, 0)])
    # Negative twin: half a storage unit off the chord is rounding, not area.
    assert not polygon_has_area([(0, 0), (5, 0.00005), (10, 0)])


def test_polygon_with_duplicates_only_or_too_few_points_has_no_area():
    """Coincident vertices and fewer than three vertices give no area."""
    assert not polygon_has_area([(1, 1), (1, 1), (1, 1)])
    assert not polygon_has_area([(1, 1), (2, 2), (1, 1)])
    assert not polygon_has_area([(0, 0), (1, 0)])
    assert not polygon_has_area([])
