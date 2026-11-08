"""@brief Object snap for drawing tools: typed hot points, edges and their priority.

Pure geometry, no Qt (plan `docs/footprint-drawing-kicad-plan.md`, B1). The
footprint preview (mm, Y down) and the symbol editor (mm, Y up) build their
snap candidates from generic primitives — points, segments, polylines,
rectangles, circles, arcs — with the builders of this module, then ask a
`SnapIndex` for the hot point under the pointer. Nothing here depends on the
orientation of Y: every rule is a distance or a projection.

Priority (highest first, `PRIORITY`): pad/pin hot point > endpoint = corner >
intersection > midpoint > centre > quadrant > edge. Endpoint and corner share
one tier, as in the plan: both are vertices of a drawn element and differ only
in whether the vertex closes a shape, which is no reason to prefer one when the
pointer is nearer the other. Within a tier the nearest candidate wins; exact
ties go to the candidate inserted first.

The reach passed to `SnapIndex.nearest` and `snap_point` is a UI reach: the
caller converts `SNAP_REACH_PX` screen pixels into model units with the current
zoom. It is not a geometric threshold and carries no physical meaning.

`snap_point` is the ONE classifier both tabs use while a tool is drawing: it
merges the drawing's index with the points the tool collected so far, minus
the last one (see its notes for the tie rule).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

Point = Sequence[float]

PAD = "pad"
ENDPOINT = "endpoint"
CORNER = "corner"
INTERSECTION = "intersection"
MIDPOINT = "midpoint"
CENTRE = "centre"
QUADRANT = "quadrant"
EDGE = "edge"

#: Rank of each snap kind; a lower number wins. Endpoint and corner share a tier.
PRIORITY = {
    PAD: 0,
    ENDPOINT: 1,
    CORNER: 1,
    INTERSECTION: 2,
    MIDPOINT: 3,
    CENTRE: 4,
    QUADRANT: 5,
    EDGE: 6,
}

#: Kinds in priority order, highest first (stable order for ties of rank).
SNAP_KINDS: Tuple[str, ...] = (PAD, ENDPOINT, CORNER, INTERSECTION, MIDPOINT, CENTRE, QUADRANT, EDGE)

#: Magnet radius of the object snap, in SCREEN pixels: the single UI constant of
#: both tabs (the symbol editor and the footprint preview divide it by their
#: current pixels per mm). 10 px is KiCad's magnet feel; it is a UI reach chosen
#: for the hand and the eye, not a geometric tolerance, and is independent of
#: any drawing or component.
SNAP_REACH_PX = 10.0

#: Relative tolerance under which two directions count as parallel, or three
#: arc points as collinear. It is relative to the product of the lengths
#: involved, so it does not depend on the drawing unit or scale.
_PARALLEL_REL_EPS = 1e-12


@dataclass(frozen=True)
class SnapCandidate:
    """@brief One hot point the pointer can snap to.

    @param x Abscissa, model units (mm).
    @param y Ordinate, model units (mm), in the caller's orientation.
    @param kind One of `SNAP_KINDS` except `EDGE` (edges are computed on demand).
    @param owner Opaque tag returned with the result, e.g. ``("pad", 3)``.
    """

    x: float
    y: float
    kind: str
    owner: Any = None


@dataclass(frozen=True)
class SnapSegment:
    """@brief One straight edge the pointer can snap onto (foot of the perpendicular).

    @param a (x, y) first end, model units.
    @param b (x, y) second end, model units.
    @param owner Opaque tag returned with an edge result.
    """

    a: Tuple[float, float]
    b: Tuple[float, float]
    owner: Any = None


@dataclass(frozen=True)
class SnapResult:
    """@brief The point a snap query chose.

    @param x Snapped abscissa, model units.
    @param y Snapped ordinate, model units.
    @param kind Kind of the hot point, one of `SNAP_KINDS`.
    @param owner The owner tag of the candidate or segment that produced it.
    """

    x: float
    y: float
    kind: str
    owner: Any = None


def _pt(p: Point) -> Tuple[float, float]:
    """Convert a point-like pair to a float tuple."""
    return (float(p[0]), float(p[1]))


def _finite(x: float, y: float) -> bool:
    """Tell whether both coordinates are finite numbers."""
    return math.isfinite(x) and math.isfinite(y)


def points(pts: Iterable[Point], kind: str, owner: Any = None) -> List[SnapCandidate]:
    """@brief Build candidates of one kind from bare points (pads, pin ends, origins).

    @param pts (x, y) pairs, model units.
    @param kind Snap kind, one of `SNAP_KINDS` except `EDGE`.
    @param owner Tag carried by every candidate.
    @return One candidate per point, in input order.
    @throws ValueError When `kind` is unknown or is `EDGE`.
    """
    if kind not in PRIORITY or kind == EDGE:
        raise ValueError(f"invalid snap kind: {kind!r}")
    return [SnapCandidate(x, y, kind, owner) for x, y in map(_pt, pts)]


def _mid(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
    """Midpoint of two points."""
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def segment(a: Point, b: Point, owner: Any = None) -> List[SnapCandidate]:
    """@brief Build the hot points of a straight segment.

    @param a (x, y) first end, model units.
    @param b (x, y) second end, model units.
    @param owner Tag carried by every candidate.
    @return Two endpoints followed by the midpoint.
    """
    a, b = _pt(a), _pt(b)
    m = _mid(a, b)
    return [
        SnapCandidate(a[0], a[1], ENDPOINT, owner),
        SnapCandidate(b[0], b[1], ENDPOINT, owner),
        SnapCandidate(m[0], m[1], MIDPOINT, owner),
    ]


def polyline(pts: Sequence[Point], closed: bool, owner: Any = None) -> List[SnapCandidate]:
    """@brief Build the hot points of a polyline or polygon.

    @param pts Vertices in drawing order, model units.
    @param closed True for a polygon (the last vertex joins the first).
    @param owner Tag carried by every candidate.
    @return Vertices first — kind "endpoint" for the two free ends of an open
            polyline, "corner" for interior vertices and for every vertex of a
            closed one — then the midpoint of each side (the closing side
            included when `closed`).
    @note A single vertex yields one endpoint and no midpoint.
    """
    vs = [_pt(p) for p in pts]
    out: List[SnapCandidate] = []
    n = len(vs)
    for i, (x, y) in enumerate(vs):
        free_end = not closed and (i == 0 or i == n - 1)
        out.append(SnapCandidate(x, y, ENDPOINT if free_end else CORNER, owner))
    for side in polyline_segments(vs, closed):
        m = _mid(side.a, side.b)
        out.append(SnapCandidate(m[0], m[1], MIDPOINT, owner))
    return out


def polyline_segments(pts: Sequence[Point], closed: bool, owner: Any = None) -> List[SnapSegment]:
    """@brief Build the edges of a polyline or polygon, for edge snapping and intersections.

    @param pts Vertices in drawing order, model units.
    @param closed True to add the side from the last vertex back to the first.
    @param owner Tag carried by every segment.
    @return One segment per side; none for fewer than two vertices.
    """
    vs = [_pt(p) for p in pts]
    if len(vs) < 2:
        return []
    pairs = list(zip(vs, vs[1:]))
    if closed and len(vs) > 2:
        pairs.append((vs[-1], vs[0]))
    return [SnapSegment(a, b, owner) for a, b in pairs]


def _rect_corners(p1: Point, p2: Point) -> List[Tuple[float, float]]:
    """Corners of the axis-aligned rectangle with opposite corners p1, p2, in loop order."""
    (x1, y1), (x2, y2) = _pt(p1), _pt(p2)
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def rectangle(p1: Point, p2: Point, owner: Any = None) -> List[SnapCandidate]:
    """@brief Build the hot points of an axis-aligned rectangle.

    @param p1 (x, y) one corner, model units.
    @param p2 (x, y) the opposite corner, model units.
    @param owner Tag carried by every candidate.
    @return Four corners, four side midpoints, then the centre.
    """
    corners = _rect_corners(p1, p2)
    out = [SnapCandidate(x, y, CORNER, owner) for x, y in corners]
    for i in range(4):
        m = _mid(corners[i], corners[(i + 1) % 4])
        out.append(SnapCandidate(m[0], m[1], MIDPOINT, owner))
    c = _mid(corners[0], corners[2])
    out.append(SnapCandidate(c[0], c[1], CENTRE, owner))
    return out


def rectangle_segments(p1: Point, p2: Point, owner: Any = None) -> List[SnapSegment]:
    """@brief Build the four sides of an axis-aligned rectangle.

    @param p1 (x, y) one corner, model units.
    @param p2 (x, y) the opposite corner, model units.
    @param owner Tag carried by every segment.
    @return Four segments in loop order.
    """
    return polyline_segments(_rect_corners(p1, p2), True, owner)


def circle(c: Point, r: float, owner: Any = None) -> List[SnapCandidate]:
    """@brief Build the hot points of a circle.

    @param c (x, y) centre, model units.
    @param r Radius, model units.
    @param owner Tag carried by every candidate.
    @return The centre followed by the four quadrant points on the axes
            (+X, +Y, -X, -Y); only the centre when `r` is not a positive finite number.
    """
    cx, cy = _pt(c)
    out = [SnapCandidate(cx, cy, CENTRE, owner)]
    r = float(r)
    if math.isfinite(r) and r > 0.0:
        for dx, dy in ((r, 0.0), (0.0, r), (-r, 0.0), (0.0, -r)):
            out.append(SnapCandidate(cx + dx, cy + dy, QUADRANT, owner))
    return out


def circumcentre(a: Point, b: Point, c: Point) -> Optional[Tuple[float, float]]:
    """@brief Centre of the circle through three points.

    @param a (x, y) first point.
    @param b (x, y) second point.
    @param c (x, y) third point.
    @return (x, y) centre, or None when the points are collinear or coincide
            (relative tolerance, independent of the unit).
    """
    (ax, ay), (bx, by), (cx, cy) = _pt(a), _pt(b), _pt(c)
    bx, by, cx, cy = bx - ax, by - ay, cx - ax, cy - ay
    cross = bx * cy - by * cx
    scale = math.hypot(bx, by) * math.hypot(cx, cy)
    if scale == 0.0 or abs(cross) <= _PARALLEL_REL_EPS * scale:
        return None
    d = 2.0 * cross
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (cy * b2 - by * c2) / d
    uy = (bx * c2 - cx * b2) / d
    return (ax + ux, ay + uy)


def arc(start: Point, mid: Point, end: Point, owner: Any = None) -> List[SnapCandidate]:
    """@brief Build the hot points of a three-point arc (KiCad's start/mid/end form).

    @param start (x, y) arc start, model units.
    @param mid (x, y) a point on the arc between the ends.
    @param end (x, y) arc end.
    @param owner Tag carried by every candidate.
    @return Two endpoints, then the centre computed from the three points;
            no centre when they are collinear (a degenerate arc is a line).
    """
    s, e = _pt(start), _pt(end)
    out = [SnapCandidate(s[0], s[1], ENDPOINT, owner), SnapCandidate(e[0], e[1], ENDPOINT, owner)]
    centre = circumcentre(start, mid, end)
    if centre is not None:
        out.append(SnapCandidate(centre[0], centre[1], CENTRE, owner))
    return out


def _as_segment(item: Any) -> SnapSegment:
    """Accept a SnapSegment or a plain (a, b) / (a, b, owner) tuple."""
    if isinstance(item, SnapSegment):
        return SnapSegment(_pt(item.a), _pt(item.b), item.owner)
    if len(item) == 3:
        return SnapSegment(_pt(item[0]), _pt(item[1]), item[2])
    return SnapSegment(_pt(item[0]), _pt(item[1]), None)


def _cell_size(segs: Sequence[SnapSegment]) -> float:
    """Cell side of the uniform grid of `intersections`: the larger of the median
    segment length and the extent divided by sqrt(n), so the grid has at most
    about n cells and a typical segment covers a handful of them."""
    lengths = sorted(math.hypot(s.b[0] - s.a[0], s.b[1] - s.a[1]) for s in segs)
    median = lengths[len(lengths) // 2]
    xs = [v for s in segs for v in (s.a[0], s.b[0])]
    ys = [v for s in segs for v in (s.a[1], s.b[1])]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    return max(median, extent / math.sqrt(len(segs)))


def _candidate_pairs(segs: Sequence[SnapSegment]) -> List[Tuple[int, int]]:
    """Pairs (i < j) of segments whose bounding boxes share a cell of a uniform grid.

    A broad phase only: every truly crossing pair shares at least the cell of
    its crossing point, so no crossing is lost; the narrow test decides.
    """
    n = len(segs)
    if n < 2:
        return []
    cell = _cell_size(segs)
    if not (cell > 0.0 and math.isfinite(cell)):
        return [(i, j) for i in range(n) for j in range(i + 1, n)]
    x0 = min(min(s.a[0], s.b[0]) for s in segs)
    y0 = min(min(s.a[1], s.b[1]) for s in segs)
    buckets: dict = {}
    for i, s in enumerate(segs):
        c0 = int((min(s.a[0], s.b[0]) - x0) // cell)
        c1 = int((max(s.a[0], s.b[0]) - x0) // cell)
        r0 = int((min(s.a[1], s.b[1]) - y0) // cell)
        r1 = int((max(s.a[1], s.b[1]) - y0) // cell)
        for c in range(c0, c1 + 1):
            for r in range(r0, r1 + 1):
                buckets.setdefault((c, r), []).append(i)
    pairs = set()
    for members in buckets.values():
        for a in range(len(members)):
            i = members[a]
            for b in range(a + 1, len(members)):
                pairs.add((i, members[b]))
    return sorted(pairs)


def intersections(segments: Sequence[Any], owner: Any = None,
                  limit: Optional[int] = None) -> List[SnapCandidate]:
    """@brief Intersection points among segments, every pair examined (no silent cap).

    @param segments `SnapSegment` items or (a, b[, owner]) tuples, model units.
    @param owner Tag carried by every candidate (the pair is not recorded).
    @param limit None (default) examines every segment. An explicit number
           pairs only the first `limit` segments: a choice the CALLER makes
           and owns, never applied by this module on its own.
    @return One "intersection" candidate per crossing pair, ends included
            (two segments touching at a vertex give that vertex), in pair
            order (i < j, sorted), exactly as a full pairwise scan.
    @note A uniform-grid broad phase (`_candidate_pairs`) pairs only the
          segments whose bounding boxes share a cell, so a drawing of
          thousands of short strokes stays fast; segments crowded into one
          cell degrade to the honest O(n^2), never to a truncated answer.
          Parallel and collinear pairs are skipped: an overlap has no single
          crossing point and its ends are already endpoint candidates.
          Zero-length and non-finite segments are skipped.
    """
    items = list(segments)
    if limit is not None:
        items = items[:max(0, int(limit))]
    segs = []
    for item in items:
        seg = _as_segment(item)
        if _finite(*seg.a) and _finite(*seg.b) and seg.a != seg.b:
            segs.append(seg)
    out: List[SnapCandidate] = []
    for i, j in _candidate_pairs(segs):
        (ax, ay), (bx, by) = segs[i].a, segs[i].b
        rx, ry = bx - ax, by - ay
        (cx, cy), (dx, dy) = segs[j].a, segs[j].b
        sx, sy = dx - cx, dy - cy
        denom = rx * sy - ry * sx
        if abs(denom) <= _PARALLEL_REL_EPS * math.hypot(rx, ry) * math.hypot(sx, sy):
            continue
        qx, qy = cx - ax, cy - ay
        t = (qx * sy - qy * sx) / denom
        u = (qx * ry - qy * rx) / denom
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            out.append(SnapCandidate(ax + t * rx, ay + t * ry, INTERSECTION, owner))
    return out


class SnapIndex:
    """@brief Answer "which hot point is under the pointer" for a fixed set of candidates.

    Candidates are kept sorted by x; a query bisects the x strip
    [x - reach, x + reach] and scans only it, rejecting on |dy| before the
    distance. Building is O(n log n); a query is O(log n + k) where k is the
    number of candidates in the strip (O(n) in the worst case of all candidates
    sharing one column). Edge snapping scans the segments linearly with a
    bounding-box reject, O(m), and runs only when no candidate is in reach.

    @note Rebuild the index when the drawing changes; it is immutable.
    """

    def __init__(self, candidates: Iterable[SnapCandidate] = (), segments: Iterable[Any] = ()):
        """@brief Index the candidates and the edge segments.

        @param candidates Hot points; non-finite ones and unknown kinds are dropped.
        @param segments `SnapSegment` items or (a, b[, owner]) tuples for edge snapping.
        """
        rows = []
        for order, cand in enumerate(candidates):
            x, y = float(cand.x), float(cand.y)
            if not _finite(x, y) or cand.kind not in PRIORITY or cand.kind == EDGE:
                continue
            rows.append((x, order, y, cand))
        rows.sort(key=lambda row: (row[0], row[1]))
        self._xs = [row[0] for row in rows]
        self._rows = rows
        segs = []
        for item in segments:
            seg = _as_segment(item)
            if _finite(*seg.a) and _finite(*seg.b):
                segs.append(seg)
        self._segments = segs

    def __len__(self) -> int:
        """@brief Number of indexed candidates.

        @return Count of candidates kept after validation.
        """
        return len(self._rows)

    def nearest(self, x: float, y: float, reach: float) -> Optional[SnapResult]:
        """@brief Return the hot point the pointer snaps to, or None.

        @param x Pointer abscissa, model units.
        @param y Pointer ordinate, model units.
        @param reach UI reach in model units (the caller's pixel radius divided
               by the current scale). A candidate exactly at distance `reach`
               is in reach (inclusive boundary).
        @return Among the candidates in reach, the highest priority; within a
                tier the nearest; exact ties go to the earlier inserted
                candidate. With no candidate in reach, the nearest foot of the
                perpendicular on a segment within reach (kind "edge"); else None.
        @note A negative or non-finite reach, or a non-finite pointer, gives None.
        """
        x, y, reach = float(x), float(y), float(reach)
        if not (_finite(x, y) and math.isfinite(reach)) or reach < 0.0:
            return None
        r2 = reach * reach
        lo = bisect.bisect_left(self._xs, x - reach)
        hi = bisect.bisect_right(self._xs, x + reach)
        best_key = None
        best = None
        for cx, order, cy, cand in self._rows[lo:hi]:
            dy = cy - y
            if dy > reach or -dy > reach:
                continue
            dx = cx - x
            d2 = dx * dx + dy * dy
            if d2 > r2:
                continue
            key = (PRIORITY[cand.kind], d2, order)
            if best_key is None or key < best_key:
                best_key, best = key, cand
        if best is not None:
            return SnapResult(float(best.x), float(best.y), best.kind, best.owner)
        return self._nearest_edge(x, y, reach, r2)

    def _nearest_edge(self, x: float, y: float, reach: float, r2: float) -> Optional[SnapResult]:
        """Nearest perpendicular foot on a segment within reach (clamped to the segment)."""
        best_d2 = None
        best = None
        for seg in self._segments:
            (ax, ay), (bx, by) = seg.a, seg.b
            if (x < min(ax, bx) - reach or x > max(ax, bx) + reach
                    or y < min(ay, by) - reach or y > max(ay, by) + reach):
                continue
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            if length2 == 0.0:
                t = 0.0
            else:
                t = ((x - ax) * dx + (y - ay) * dy) / length2
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            fx, fy = ax + t * dx, ay + t * dy
            d2 = (fx - x) ** 2 + (fy - y) ** 2
            # Strict comparison: an exact tie keeps the earlier segment.
            if d2 <= r2 and (best_d2 is None or d2 < best_d2):
                best_d2, best = d2, SnapResult(fx, fy, EDGE, seg.owner)
        return best


def snap_point(index: Optional["SnapIndex"], collected: Sequence[Point], x: float, y: float,
               reach: float, *, own_kind: str = ENDPOINT) -> Optional[SnapResult]:
    """@brief The ONE object-snap classifier of the drawing tools: drawing hot points plus the tool's own points.

    @param index The drawing's `SnapIndex` (pads, shapes, intersections), or None.
    @param collected The points the armed tool collected so far, model units,
           in click order. Every one EXCEPT THE LAST is a hot point: the first
           closes a polygon, the others let a line come back to its own
           vertices; the last is the anchor of the segment being drawn, and
           snapping onto it would make a zero-length segment that the click
           tool refuses as coincident.
    @param x Pointer abscissa, model units.
    @param y Pointer ordinate, model units.
    @param reach UI reach in model units (`SNAP_REACH_PX` divided by the
           current pixels per unit); inclusive, as `SnapIndex.nearest`.
    @param own_kind Kind given to the collected points (default "endpoint").
    @return The chosen `SnapResult`, or None when nothing is within reach (or
            the reach is not a positive finite number). A collected point
            comes back with owner ("collected", i), i its position in
            `collected`.
    @throws ValueError When `own_kind` is not a snap kind or is "edge".
    @note Tie rule between the index's answer and the best collected point:
          a strictly higher priority (lower `PRIORITY` rank) wins; at equal
          priority the nearer wins; an exact tie of rank and distance goes to
          the index (the existing drawing), never to the stroke in progress.
    """
    if own_kind not in PRIORITY or own_kind == EDGE:
        raise ValueError(f"invalid snap kind: {own_kind!r}")
    x, y, reach = float(x), float(y), float(reach)
    if not (_finite(x, y) and math.isfinite(reach)) or reach <= 0.0:
        return None
    best = index.nearest(x, y, reach) if index is not None else None
    own_points = list(collected)[:-1]
    if not own_points:
        return best
    own_candidates = [SnapCandidate(px, py, own_kind, ("collected", i))
                      for i, (px, py) in enumerate(map(_pt, own_points))]
    # Edge snapping does not apply to bare points: no segments in this index.
    own = SnapIndex(own_candidates).nearest(x, y, reach)
    if own is None:
        return best
    if best is None:
        return own

    def rank(hit: SnapResult) -> Tuple[int, float]:
        """Priority tier, then squared distance to the pointer."""
        return (PRIORITY[hit.kind], (hit.x - x) ** 2 + (hit.y - y) ** 2)

    # Strict: an exact tie keeps the index's answer.
    return own if rank(own) < rank(best) else best
