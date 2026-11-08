"""@brief Tests of the pure object snap: builders, priority, reach and edges."""
import math
import random
import time

import pytest

from kicadgen.core import object_snap as osn
from kicadgen.core.object_snap import SnapCandidate, SnapIndex, SnapSegment


def test_endpoint_caught_within_reach_not_beyond():
    """Positive at the reach, negative just beyond it."""
    idx = SnapIndex(osn.segment((0, 0), (10, 0), owner=("drawing", 1)))
    hit = idx.nearest(0.0, 0.9, 1.0)
    assert hit is not None and hit.kind == osn.ENDPOINT and (hit.x, hit.y) == (0.0, 0.0)
    assert idx.nearest(-1.01, 0.0, 1.0) is None


def test_reach_boundary_is_inclusive():
    """A candidate exactly at distance `reach` is caught (3-4-5 triangle, exact)."""
    idx = SnapIndex(osn.points([(0, 0)], osn.PAD))
    assert idx.nearest(3.0, 4.0, 5.0) is not None
    assert idx.nearest(3.0, 4.0, 4.999999) is None


def test_endpoint_beats_nearer_midpoint():
    """Priority wins over distance when both are in reach; nearest wins otherwise."""
    cands = [SnapCandidate(0.0, 0.0, osn.MIDPOINT), SnapCandidate(1.0, 0.0, osn.ENDPOINT)]
    idx = SnapIndex(cands)
    assert idx.nearest(0.1, 0.0, 1.0).kind == osn.ENDPOINT
    # Negative: the endpoint out of reach lets the midpoint win.
    assert idx.nearest(-0.5, 0.0, 1.0).kind == osn.MIDPOINT


def test_pad_beats_endpoint_and_corner_shares_endpoint_tier():
    """Pad outranks endpoint; endpoint and corner compete by distance only."""
    idx = SnapIndex([SnapCandidate(0.0, 0.0, osn.ENDPOINT), SnapCandidate(0.8, 0.0, osn.PAD)])
    assert idx.nearest(0.0, 0.0, 1.0).kind == osn.PAD
    idx = SnapIndex([SnapCandidate(0.0, 0.0, osn.ENDPOINT), SnapCandidate(1.0, 0.0, osn.CORNER)])
    assert idx.nearest(0.9, 0.0, 2.0).kind == osn.CORNER
    assert idx.nearest(0.1, 0.0, 2.0).kind == osn.ENDPOINT


def test_priority_table_order():
    """The declared ranks follow the plan's order, highest first."""
    ranks = [osn.PRIORITY[k] for k in osn.SNAP_KINDS]
    assert ranks == sorted(ranks)
    assert osn.PRIORITY[osn.INTERSECTION] < osn.PRIORITY[osn.MIDPOINT] < osn.PRIORITY[osn.CENTRE]
    assert osn.PRIORITY[osn.CENTRE] < osn.PRIORITY[osn.QUADRANT] < osn.PRIORITY[osn.EDGE]


def test_ties_go_to_insertion_order():
    """Same kind, same distance: the first inserted candidate wins."""
    idx = SnapIndex([SnapCandidate(1.0, 0.0, osn.CENTRE, "first"), SnapCandidate(-1.0, 0.0, osn.CENTRE, "second")])
    assert idx.nearest(0.0, 0.0, 2.0).owner == "first"
    idx = SnapIndex([SnapCandidate(-1.0, 0.0, osn.CENTRE, "first"), SnapCandidate(1.0, 0.0, osn.CENTRE, "second")])
    assert idx.nearest(0.0, 0.0, 2.0).owner == "first"


def test_edge_only_when_no_candidate():
    """The perpendicular foot is returned only with no hot point in reach."""
    seg = SnapSegment((0.0, 0.0), (10.0, 0.0), ("drawing", 7))
    idx = SnapIndex(osn.segment((0, 0), (10, 0), ("drawing", 7)), [seg])
    edge = idx.nearest(3.0, 0.4, 0.5)
    assert edge.kind == osn.EDGE and math.isclose(edge.x, 3.0) and edge.y == 0.0
    assert edge.owner == ("drawing", 7)
    # Negative: near the midpoint the candidate wins over the edge.
    assert idx.nearest(5.1, 0.4, 0.5).kind == osn.MIDPOINT
    # Negative: the edge beyond reach gives nothing.
    assert idx.nearest(3.0, 0.6, 0.5) is None


def test_edge_foot_clamped_and_plain_tuples_accepted():
    """Beyond a segment's end the foot is the end; tuples work as segments."""
    idx = SnapIndex([], [((0, 0), (0, 10), "wall")])
    hit = idx.nearest(0.3, 10.2, 0.5)
    assert hit.kind == osn.EDGE and (hit.x, hit.y) == (0.0, 10.0) and hit.owner == "wall"


def test_intersection_of_an_x():
    """Two crossing diagonals give their crossing; parallel and disjoint ones give none."""
    segs = [SnapSegment((0, 0), (4, 4)), SnapSegment((0, 4), (4, 0))]
    out = osn.intersections(segs, owner="x")
    assert len(out) == 1
    assert math.isclose(out[0].x, 2.0) and math.isclose(out[0].y, 2.0)
    assert out[0].kind == osn.INTERSECTION and out[0].owner == "x"
    assert osn.intersections([((0, 0), (4, 0)), ((0, 1), (4, 1))]) == []
    assert osn.intersections([((0, 0), (4, 0)), ((1, 0), (6, 0))]) == []
    assert osn.intersections([((0, 0), (1, 1)), ((3, 0), (4, -1))]) == []


def test_arc_centre_and_collinear_arc():
    """A quarter arc gives its centre; three collinear points give no centre."""
    out = osn.arc((1, 0), (math.sqrt(0.5), math.sqrt(0.5)), (0, 1), owner="a")
    kinds = [c.kind for c in out]
    assert kinds == [osn.ENDPOINT, osn.ENDPOINT, osn.CENTRE]
    assert math.isclose(out[2].x, 0.0, abs_tol=1e-12) and math.isclose(out[2].y, 0.0, abs_tol=1e-12)
    flat = osn.arc((0, 0), (1, 1), (2, 2))
    assert [c.kind for c in flat] == [osn.ENDPOINT, osn.ENDPOINT]
    # Scale independence: the same arc at micrometre size keeps its centre.
    tiny = osn.arc((1e-3, 0), (0, 1e-3), (-1e-3, 0))
    assert len(tiny) == 3


def test_builders_kinds_and_counts():
    """Rectangle, circle, segment and polylines produce the declared hot points."""
    rect = osn.rectangle((0, 0), (4, 2), "r")
    assert [c.kind for c in rect].count(osn.CORNER) == 4
    assert [c.kind for c in rect].count(osn.MIDPOINT) == 4
    assert rect[-1].kind == osn.CENTRE and (rect[-1].x, rect[-1].y) == (2.0, 1.0)
    circ = osn.circle((1, 1), 2)
    assert [c.kind for c in circ] == [osn.CENTRE] + [osn.QUADRANT] * 4
    assert (circ[1].x, circ[1].y) == (3.0, 1.0)
    assert len(osn.circle((0, 0), 0)) == 1
    open_pl = osn.polyline([(0, 0), (1, 0), (1, 1)], closed=False)
    assert [c.kind for c in open_pl] == [osn.ENDPOINT, osn.CORNER, osn.ENDPOINT, osn.MIDPOINT, osn.MIDPOINT]
    closed_pl = osn.polyline([(0, 0), (1, 0), (1, 1)], closed=True)
    assert [c.kind for c in closed_pl] == [osn.CORNER] * 3 + [osn.MIDPOINT] * 3
    assert len(osn.rectangle_segments((0, 0), (1, 1))) == 4
    with pytest.raises(ValueError):
        osn.points([(0, 0)], osn.EDGE)


def test_owner_carried_back():
    """The opaque owner tag comes back with the result."""
    idx = SnapIndex(osn.points([(5, 5)], osn.PAD, owner=("pad", 3)))
    assert idx.nearest(5.2, 5.0, 0.5).owner == ("pad", 3)


def test_invalid_inputs_are_ignored():
    """Non-finite candidates are dropped; a negative or NaN reach answers None."""
    idx = SnapIndex([SnapCandidate(float("nan"), 0.0, osn.PAD), SnapCandidate(0.0, 0.0, osn.CENTRE)])
    assert len(idx) == 1
    assert idx.nearest(0.0, 0.0, -1.0) is None
    assert idx.nearest(0.0, 0.0, float("nan")) is None


def test_index_matches_brute_force():
    """The x-strip index chooses exactly what a full linear scan chooses."""
    rng = random.Random(1234)
    kinds = [k for k in osn.SNAP_KINDS if k != osn.EDGE]
    cands = [SnapCandidate(rng.uniform(0, 20), rng.uniform(0, 20), rng.choice(kinds), i) for i in range(400)]
    idx = SnapIndex(cands)
    for _ in range(300):
        x, y, reach = rng.uniform(0, 20), rng.uniform(0, 20), rng.uniform(0.1, 2.0)
        inside = [(osn.PRIORITY[c.kind], (c.x - x) ** 2 + (c.y - y) ** 2, i, c)
                  for i, c in enumerate(cands) if (c.x - x) ** 2 + (c.y - y) ** 2 <= reach * reach]
        hit = idx.nearest(x, y, reach)
        if not inside:
            assert hit is None
        else:
            assert hit.owner == min(inside, key=lambda r: r[:3])[3].owner


def test_micro_benchmark_5000_candidates_1000_queries():
    """A regression guard with a generous bound, not a performance claim."""
    rng = random.Random(7)
    cands = [SnapCandidate(rng.uniform(0, 100), rng.uniform(0, 100), osn.ENDPOINT) for _ in range(5000)]
    segs = [SnapSegment((rng.uniform(0, 100), rng.uniform(0, 100)), (rng.uniform(0, 100), rng.uniform(0, 100)))
            for _ in range(200)]
    idx = SnapIndex(cands, segs)
    queries = [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(1000)]
    start = time.perf_counter()
    for x, y in queries:
        idx.nearest(x, y, 0.5)
    assert time.perf_counter() - start < 1.0


# ── snap_point: the ONE classifier of both tabs (review M4) ──

def test_snap_reach_is_one_ui_constant():
    """Both tabs read the same magnet radius in screen pixels."""
    assert osn.SNAP_REACH_PX == 10.0


def test_snap_point_never_catches_the_last_collected_point():
    """Negative: the last point is the segment's anchor; positive: an earlier one is caught."""
    collected = [(0.0, 0.0), (5.0, 0.0)]
    assert osn.snap_point(None, collected, 5.1, 0.0, 0.5) is None
    hit = osn.snap_point(None, collected, 0.2, 0.1, 0.5)
    assert hit is not None and (hit.x, hit.y) == (0.0, 0.0)
    assert hit.kind == osn.ENDPOINT and hit.owner == ("collected", 0)
    # A single collected point is only the anchor: nothing of the tool's own is caught.
    assert osn.snap_point(None, [(0.0, 0.0)], 0.0, 0.0, 0.5) is None


def test_snap_point_catches_the_first_point_to_close_a_polygon():
    """The first vertex of a polygon in progress is a hot point once two or more are collected."""
    collected = [(0.0, 0.0), (4.0, 0.0), (4.0, 3.0)]
    hit = osn.snap_point(SnapIndex([]), collected, 0.3, -0.2, 0.5)
    assert (hit.x, hit.y) == (0.0, 0.0) and hit.owner == ("collected", 0)


def test_snap_point_priority_then_distance_then_the_index_wins_exact_ties():
    """Higher priority wins; equal priority -> nearer; an exact tie goes to the drawing's index."""
    collected = [(1.0, 0.0), (9.0, 9.0)]
    # A pad of the index outranks a nearer collected endpoint.
    idx = SnapIndex(osn.points([(-0.4, 0.0)], osn.PAD, "pad"))
    assert osn.snap_point(idx, collected, 0.9, 0.0, 2.0).owner == "pad"
    # A collected endpoint outranks a nearer midpoint of the index.
    idx = SnapIndex([SnapCandidate(0.8, 0.0, osn.MIDPOINT, "mid")])
    assert osn.snap_point(idx, collected, 0.8, 0.0, 2.0).owner == ("collected", 0)
    # Same tier (corner = endpoint): the nearer wins either way.
    idx = SnapIndex([SnapCandidate(0.0, 0.0, osn.CORNER, "corner")])
    assert osn.snap_point(idx, collected, 0.8, 0.0, 2.0).owner == ("collected", 0)
    assert osn.snap_point(idx, collected, 0.2, 0.0, 2.0).owner == "corner"
    # Exact tie of tier and distance: the index wins.
    idx = SnapIndex([SnapCandidate(0.0, 0.0, osn.ENDPOINT, "drawing")])
    assert osn.snap_point(idx, collected, 0.5, 0.0, 2.0).owner == "drawing"
    idx = SnapIndex([SnapCandidate(1.0, 0.0, osn.ENDPOINT, "drawing")])
    assert osn.snap_point(idx, collected, 1.0, 0.0, 2.0).owner == "drawing"


def test_snap_point_refuses_a_bad_reach_and_a_bad_kind():
    """Zero, negative or NaN reach gives None; an edge kind for the collected points is refused."""
    idx = SnapIndex(osn.points([(0, 0)], osn.PAD))
    for reach in (0.0, -1.0, float("nan")):
        assert osn.snap_point(idx, [(0, 0), (1, 1)], 0.0, 0.0, reach) is None
    with pytest.raises(ValueError):
        osn.snap_point(idx, [], 0.0, 0.0, 1.0, own_kind=osn.EDGE)
    corner = osn.snap_point(None, [(0, 0), (1, 1)], 0.0, 0.0, 1.0, own_kind=osn.CORNER)
    assert corner.kind == osn.CORNER


# ── intersections without silent truncation (review G4) ──

def _brute_intersections(segs):
    """Reference: the plain pairwise scan in pair order."""
    out = []
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            out += osn.intersections([segs[i], segs[j]])
    return [(c.x, c.y) for c in out]


def test_intersections_no_cap_review_counter_example():
    """401 horizontals and one vertical added LAST: all 401 crossings are found (the old cap lost them all)."""
    segs = [((0.0, float(i)), (10.0, float(i))) for i in range(401)]
    segs.append(((5.0, -1.0), (5.0, 401.0)))
    out = osn.intersections(segs)
    assert len(out) == 401
    assert sorted(c.y for c in out) == [float(i) for i in range(401)]
    assert all(c.x == 5.0 for c in out)


def test_intersections_broad_phase_matches_the_full_scan():
    """The uniform grid finds exactly what the pairwise scan finds, in the same order."""
    rng = random.Random(99)
    segs = []
    for _ in range(300):
        x, y = rng.uniform(0, 50), rng.uniform(0, 50)
        length = rng.choice((0.5, 3.0, 40.0))
        a = rng.uniform(0, math.tau)
        segs.append(((x, y), (x + length * math.cos(a), y + length * math.sin(a))))
    segs.append(((10.0, 10.0), (10.0, 10.0)))           # zero length: skipped
    got = [(c.x, c.y) for c in osn.intersections(segs)]
    assert got == _brute_intersections(segs)


def test_intersections_explicit_limit_is_the_callers_choice():
    """Only an explicit `limit` pairs fewer segments; the default examines all."""
    segs = [((0, 0), (4, 4)), ((0, 4), (4, 0))]
    assert osn.intersections(segs, limit=1) == []
    assert len(osn.intersections(segs, limit=2)) == 1
    assert len(osn.intersections(segs)) == 1


def test_intersections_benchmark_2000_short_segments():
    """A regression guard with a generous bound: 2000 short strokes, no cap, well under a second or two."""
    rng = random.Random(5)
    segs = []
    for _ in range(2000):
        x, y = rng.uniform(0, 100), rng.uniform(0, 100)
        a = rng.uniform(0, math.tau)
        segs.append(((x, y), (x + 2.0 * math.cos(a), y + 2.0 * math.sin(a))))
    start = time.perf_counter()
    out = osn.intersections(segs)
    assert time.perf_counter() - start < 3.0
    assert out, "random short strokes on a 100 mm square do cross"
