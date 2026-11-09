"""@brief Qt-free vertex editing of lines and polygons (`kicadgen.core.vertex_edit`).

Plan docs/footprint-back-anchor-vertices-zones-plan.md, F3 and contracts 5
and 8: every rule has a case it accepts and a case it refuses for a
structural reason (vertex count, area, segment ends), not a tuned number.
"""
import math

import pytest

from kicadgen.core import vertex_edit as ve
from kicadgen.core.draw_constraints import polygon_has_area

SQUARE = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
LINE = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0)]


# ── nearest_segment / pick ──────────────────────────────────────────────────

def test_nearest_segment_projects_the_point_on_the_segment():
    hit = ve.nearest_segment(LINE, (2.0, 0.5), closed=False)
    assert (hit.index, hit.point) == (0, (2.0, 0.0))
    assert math.isclose(hit.distance, 0.5)


def test_the_closing_side_is_a_segment_of_a_polygon_only():
    """@brief Positive: (0, 2) lies on the polygon's closing side; negative: an open line has no such side."""
    hit = ve.nearest_segment(SQUARE, (0.1, 2.0), closed=True)
    assert (hit.index, hit.point) == (3, (0.0, 2.0)), 'segment 3 = last vertex -> first'
    open_hit = ve.nearest_segment(SQUARE, (0.1, 2.0), closed=False)
    assert open_hit.index != 3 and open_hit.distance > 1.0


def test_projection_is_clamped_to_the_segment_ends():
    hit = ve.nearest_segment([(0, 0), (2, 0)], (5.0, 1.0), closed=False)
    assert hit.point == (2.0, 0.0)


def test_nearest_segment_needs_two_vertices():
    assert ve.nearest_segment([(1, 1)], (0, 0), closed=False) is None
    assert ve.nearest_segment([], (0, 0), closed=True) is None


def test_pick_prefers_a_vertex_within_reach_over_its_segments():
    picked = ve.pick(SQUARE, (4.05, 0.05), closed=True, reach=0.2)
    assert (picked.kind, picked.index, picked.point) == (ve.VERTEX, 1, (4.0, 0.0))


def test_pick_on_a_segment_away_from_vertices_gives_the_projection():
    picked = ve.pick(SQUARE, (2.0, 0.1), closed=True, reach=0.2)
    assert (picked.kind, picked.index, picked.point) == (ve.SEGMENT, 0, (2.0, 0.0))


def test_pick_off_the_outline_is_none():
    """@brief The middle of a polygon is neither a vertex nor a segment (the menu entries stay grey)."""
    assert ve.pick(SQUARE, (2.0, 2.0), closed=True, reach=0.2) is None
    assert ve.pick(SQUARE, (2.0, 0.1), closed=True, reach=-1) is None
    assert ve.pick(SQUARE, (2.0, 0.1), closed=True, reach=float('nan')) is None


# ── insert_vertex ───────────────────────────────────────────────────────────

def test_insert_on_a_middle_segment_keeps_the_order():
    result = ve.insert_vertex(LINE, 1, (4.0, 2.0), closed=False)
    assert result == [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (4.0, 4.0)]
    assert LINE == [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0)], 'the input is not mutated'


def test_insert_on_the_closing_side_appends_the_vertex():
    result = ve.insert_vertex(SQUARE, 3, (0.0, 2.0), closed=True)
    assert result == SQUARE + [(0.0, 2.0)]


def test_insert_on_the_closing_index_of_an_open_line_is_refused():
    """@brief Negative twin: an open line of 3 vertices has segments 0 and 1 only."""
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.insert_vertex(LINE, 2, (2.0, 2.0), closed=False)
    assert refused.value.reason == ve.BAD_INDEX


def test_insert_on_an_end_of_the_segment_is_refused():
    """@brief A grid snap that lands on an existing end would only duplicate it."""
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.insert_vertex(LINE, 0, (4.0, 0.0), closed=False)
    assert refused.value.reason == ve.COINCIDENT
    # Positive twin: a snapped point a little off the segment is still inserted (as a vertex drag allows).
    assert ve.insert_vertex(LINE, 0, (2.0, 0.1), closed=False)[1] == (2.0, 0.1)


@pytest.mark.parametrize('bad', [-1, 9, True, 1.0])
def test_insert_refuses_an_unknown_segment(bad):
    with pytest.raises(ve.VertexEditRefused):
        ve.insert_vertex(LINE, bad, (1.0, 1.0), closed=False)


# ── remove_vertex ───────────────────────────────────────────────────────────

def test_a_line_keeps_at_least_two_vertices():
    assert ve.remove_vertex(LINE, 1, closed=False) == [(0.0, 0.0), (4.0, 4.0)]
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex([(0, 0), (1, 0)], 0, closed=False)
    assert refused.value.reason == ve.TOO_FEW_POINTS


def test_a_polygon_keeps_at_least_three_vertices():
    assert ve.remove_vertex(SQUARE, 0, closed=True) == [(4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex([(0, 0), (4, 0), (4, 4)], 2, closed=True)
    assert refused.value.reason == ve.TOO_FEW_POINTS


def test_a_polygon_keeps_its_area():
    """@brief Removing the only vertex off a line refuses; removing a collinear one is accepted."""
    flat = [(0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (2.0, 3.0)]
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex(flat, 3, closed=True)
    assert refused.value.reason == ve.NO_AREA
    assert not polygon_has_area(flat[:3]), 'the shared rule is the one that refuses'
    assert ve.remove_vertex(flat, 1, closed=True) == [(0.0, 0.0), (4.0, 0.0), (2.0, 3.0)]


def test_a_line_keeps_a_length():
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex([(1.0, 1.0), (3.0, 1.0), (1.0, 1.0)], 1, closed=False)
    assert refused.value.reason == ve.NO_LENGTH
    assert ve.remove_vertex([(1.0, 1.0), (3.0, 1.0), (1.0, 1.0)], 2, closed=False) == [(1.0, 1.0), (3.0, 1.0)]


def test_a_caller_minimum_is_never_lower_than_the_kind_minimum():
    with pytest.raises(ve.VertexEditRefused):
        ve.remove_vertex(SQUARE, 0, closed=True, min_points_required=4)
    with pytest.raises(ve.VertexEditRefused):
        ve.remove_vertex([(0, 0), (4, 0), (4, 4)], 0, closed=True, min_points_required=1)


def test_remove_refuses_an_unknown_vertex_and_leaves_the_input():
    points = list(SQUARE)
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex(points, 4, closed=True)
    assert refused.value.reason == ve.BAD_INDEX
    assert points == SQUARE
    assert isinstance(refused.value, ValueError)


def test_open_vertices_strips_only_the_closing_repeat():
    assert ve.open_vertices(SQUARE + [SQUARE[0]]) == SQUARE
    assert ve.open_vertices(SQUARE) == SQUARE
    assert ve.min_points(True) == 3 and ve.min_points(False) == 2
    assert ve.segment_count(4, True) == 4 and ve.segment_count(4, False) == 3 and ve.segment_count(1, True) == 0


# ── stored resolution and duplicates (review 2026-11-09) ────────────────────

def test_the_stored_resolution_is_the_one_of_the_footprint_and_of_the_area_rule():
    """@brief One storage resolution: vertex_edit, footprint drawings and `polygon_has_area` agree."""
    import inspect
    from kicadgen.core import draw_constraints, footprint
    assert ve.STORED_POINT_RESOLUTION_MM == footprint._STORED_POINT_RESOLUTION_MM
    default = inspect.signature(draw_constraints.polygon_has_area).parameters['resolution'].default
    assert ve.STORED_POINT_RESOLUTION_MM == default


def test_same_point_compares_the_stored_values():
    """@brief Positive: points rounding to one stored value are one vertex; negative: one stored step apart are two."""
    assert ve.same_point((1.0, 2.0), (1.00002, 1.99998))
    assert not ve.same_point((1.0, 2.0), (1.0001, 2.0))
    assert not ve.same_point((1.0, 2.0), (1.0, 2.0001))


@pytest.mark.parametrize('value', [2.21535, 2.21545, 0.00015, -1.23455, 7.77775])
def test_one_same_point_rule_for_vertex_edit_and_the_footprint_collapse(value):
    """@brief A half step rounds alike in `same_point`, `collapse_repeated_points` and the stored record.

    The float `value` sits on a 4-decimal half step; `round(v / 1e-4)` and
    `round(v, 4)` (what a record stores) may disagree there. Positive: the
    point and its stored form are one vertex for both helpers. Negative: the
    neighbouring stored value on the other side of the half step is another
    vertex for both.
    """
    from kicadgen.core.footprint import collapse_repeated_points
    stored = round(value, 4)
    other = round(stored + (1e-4 if stored < value else -1e-4), 4)
    assert ve.same_point((value, 0.0), (stored, 0.0))
    assert len(collapse_repeated_points([(value, 0.0), (stored, 0.0)], closed=False)) == 1
    assert ve.stored_key((value, 0.0)) == ve.stored_key((stored, 0.0))
    assert not ve.same_point((value, 0.0), (other, 0.0))
    assert len(collapse_repeated_points([(value, 0.0), (other, 0.0)], closed=False)) == 2


def test_insert_refuses_a_point_on_an_end_at_the_stored_resolution():
    """@brief A float a hair away from an end is still the same stored vertex: refused; one step away is accepted."""
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.insert_vertex(LINE, 0, (4.00003, 0.0), closed=False)
    assert refused.value.reason == ve.COINCIDENT
    assert ve.insert_vertex(LINE, 0, (3.9999, 0.0), closed=False)[1] == (3.9999, 0.0)


def test_a_removal_that_makes_two_consecutive_vertices_coincide_is_refused():
    """@brief Removing B from A, B, A, C would leave A, A, C; removing C (no new pair of equal points) is fine."""
    a, b, c = (0.0, 0.0), (2.0, 0.0), (2.0, 2.0)
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex([a, b, a, c], 1, closed=False)
    assert refused.value.reason == ve.DUPLICATE
    assert ve.remove_vertex([a, b, a, c], 3, closed=False) == [a, b, a]


def test_a_removal_closing_a_polygon_onto_a_duplicate_is_refused():
    """@brief Closed: the new pair can be the closing side (last and first); an open line has no such pair."""
    a, b, c, d = (0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)
    polygon = [a, b, c, d, a, (2.0, 6.0)]
    with pytest.raises(ve.VertexEditRefused) as refused:
        ve.remove_vertex(polygon, 5, closed=True)
    assert refused.value.reason == ve.DUPLICATE
    # Negative twin: on an open line the last vertex has no neighbour after it.
    assert ve.remove_vertex(polygon, 5, closed=False) == [a, b, c, d, a]


def test_removing_one_of_two_existing_duplicates_cleans_them_up():
    """@brief Only the pair a removal CREATES is refused: an existing duplicate can be removed."""
    a, b, c = (0.0, 0.0), (2.0, 0.0), (2.0, 2.0)
    assert ve.remove_vertex([a, b, b, c], 2, closed=False) == [a, b, c]


def test_a_closed_outline_needs_a_repeat_and_three_vertices():
    """@brief A1: A, B, C, A is closed; A, B, C is open; the A, B, A back-and-forth stays a line."""
    a, b, c = (0.0, 0.0), (2.0, 0.0), (2.0, 2.0)
    assert ve.is_closed_outline([a, b, c, a])
    assert ve.is_closed_outline([a, b, c, (0.00003, 0.0)]), 'the repeat at the stored resolution'
    assert not ve.is_closed_outline([a, b, c])
    assert not ve.is_closed_outline([a, b, a])
    assert not ve.is_closed_outline([])


# ── pick on a short segment (generality finding, 2026-11-09) ────────────────

def test_a_segment_shorter_than_two_reaches_keeps_its_middle_for_add():
    """@brief Both vertex reaches cover a 1.0 segment with reach 0.8: its middle still picks the SEGMENT.

    The rule is structural (the nearer of the vertex and the segment's
    midpoint), so the split lies at a quarter of the length whatever the reach.
    """
    short = [(0.0, 0.0), (1.0, 0.0), (1.0, 5.0)]
    reach = 0.8
    middle = ve.pick(short, (0.5, 0.05), closed=False, reach=reach)
    assert (middle.kind, middle.index, middle.point) == (ve.SEGMENT, 0, (0.5, 0.0))
    assert ve.pick(short, (0.3, 0.0), closed=False, reach=reach).kind == ve.SEGMENT
    near_end = ve.pick(short, (0.2, 0.0), closed=False, reach=reach)
    assert (near_end.kind, near_end.index) == (ve.VERTEX, 0)
    assert ve.pick(short, (0.0, 0.0), closed=False, reach=reach).kind == ve.VERTEX


def test_on_a_long_segment_the_vertex_keeps_its_whole_reach():
    """@brief Negative twin: a segment longer than four reaches never takes a click within a vertex reach."""
    reach = 0.2
    picked = ve.pick(SQUARE, (0.19, 0.0), closed=True, reach=reach)
    assert (picked.kind, picked.index) == (ve.VERTEX, 0)
    assert ve.pick(SQUARE, (0.21, 0.0), closed=True, reach=reach).kind == ve.SEGMENT
