"""@brief Add and remove vertices of a drawn line (polyline) or polygon.

Pure geometry, no Qt (plan `docs/footprint-back-anchor-vertices-zones-plan.md`,
F3, contracts 5 and 8). The symbol editor (mm, Y up) and the footprint preview
(mm, Y down) both use these helpers, so a right click picks, inserts and
removes a vertex by the same rules in both tabs. Nothing here depends on the
orientation of Y: every rule is a distance or a projection.

Vertex lists are always the DISTINCT vertices in drawing order: a closed
polygon's closing side (last vertex back to the first) is implied by
``closed=True`` and the first vertex is NOT repeated at the end. A caller whose
storage repeats it (the symbol's polygon polyline) strips it before and
appends it again after (`open_vertices`).

The drag handles of a stored polyline follow the same definition of closed
(`is_closed_outline`): `handle_points` gives the closing point ONE handle and
`move_handle_point` moves the first and the repeated last point together
(user decision H2, 2026-11-09), in both tabs.

Refusals raise `VertexEditRefused` (a `ValueError`) carrying a `reason` code,
so the caller can say why in its own words; the input list is never mutated.

Two points are "the same" at the STORED resolution (`same_point`): every
drawing is saved with 4 decimals, so points that round to one stored value
are one vertex once written, whatever their float difference now.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .draw_constraints import polygon_has_area

Point = Sequence[float]

#: Decimals of a stored drawing point (every record is saved with
#: `round(value, 4)`). A storage fact, not a tuned threshold.
STORED_POINT_DECIMALS = 4

#: Coordinate resolution, mm, of a stored drawing point (4 decimals). Shared
#: by `kicadgen.core.footprint` (imported there) and equal to the default
#: `resolution` of `draw_constraints.polygon_has_area`; a test pins them together.
STORED_POINT_RESOLUTION_MM = 10.0 ** -STORED_POINT_DECIMALS

#: Fewest vertices of an open line and of a closed polygon.
MIN_LINE_POINTS = 2
MIN_POLYGON_POINTS = 3

#: Refusal reasons carried by `VertexEditRefused.reason`.
TOO_FEW_POINTS = "too_few_points"
NO_AREA = "no_area"
NO_LENGTH = "no_length"
COINCIDENT = "coincident"
BAD_INDEX = "bad_index"
#: A removal that would make two consecutive vertices the same point.
DUPLICATE = "duplicate"

#: The pick of a right click (`pick`).
VERTEX = "vertex"
SEGMENT = "segment"


class VertexEditRefused(ValueError):
    """@brief A vertex insertion or removal that would leave an invalid drawing.

    @param reason One of TOO_FEW_POINTS, NO_AREA, NO_LENGTH, COINCIDENT,
        DUPLICATE, BAD_INDEX.
    @param message Developer-facing text (the GUI shows its own translated words).
    """

    def __init__(self, reason: str, message: str = ""):
        """@brief Store the reason code.

        @param reason Refusal reason code.
        @param message Optional explanation; defaults to the reason.
        """
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class SegmentHit:
    """@brief The segment of a vertex list nearest to a point.

    @param index Segment index: segment i joins vertex i to vertex i+1; with
        ``closed`` the last segment (index n-1) joins the last vertex to the first.
    @param point (x, y) foot of the perpendicular from the point onto the
        segment, clamped to the segment's ends.
    @param distance Distance from the point to `point`, same unit.
    """

    index: int
    point: Tuple[float, float]
    distance: float


@dataclass(frozen=True)
class Pick:
    """@brief What a right click at a point hits on a line or polygon.

    @param kind VERTEX or SEGMENT.
    @param index Vertex index (VERTEX) or segment index (SEGMENT).
    @param point The vertex itself (VERTEX) or the projection on the segment (SEGMENT).
    @param distance Distance from the click to `point`.
    """

    kind: str
    index: int
    point: Tuple[float, float]
    distance: float


def min_points(closed: bool) -> int:
    """@brief Fewest vertices of a valid drawing: 3 for a polygon, 2 for a line.

    @param closed True for a polygon.
    @return MIN_POLYGON_POINTS or MIN_LINE_POINTS.
    """
    return MIN_POLYGON_POINTS if closed else MIN_LINE_POINTS


def stored_key(point: Point) -> Tuple[int, int]:
    """@brief A point as the two integers of its STORED coordinates: the one "same point" rule.

    @param point (x, y) mm.
    @return (round(x, 4), round(y, 4)) expressed in units of
        `STORED_POINT_RESOLUTION_MM`, as exact integers.
    @note Built on `round(value, STORED_POINT_DECIMALS)`, the very rounding
        every record is saved with, NOT on `round(value / 1e-4)`: the two
        disagree at half steps (the float 2.21535 lies just below the half
        step, so round(2.21535, 4) stores 2.2153, while 2.21535 / 1e-4
        rounds to 22154, the key of 2.2154), so only this key says whether
        two points ARE one point once written. `same_point`, `kicadgen.core.footprint.collapse_repeated_points`
        and the exact crossing test `polygon_is_simple` all use it.
    """
    scale = 10 ** STORED_POINT_DECIMALS
    return (int(round(round(float(point[0]), STORED_POINT_DECIMALS) * scale)),
            int(round(round(float(point[1]), STORED_POINT_DECIMALS) * scale)))


def same_point(a: Point, b: Point) -> bool:
    """@brief Whether two points are one vertex once stored (`stored_key` equal).

    @param a (x, y) mm.
    @param b (x, y) mm.
    @return True when x and y of both points round to the same stored value.
    """
    return stored_key(a) == stored_key(b)


def is_closed_outline(points: Sequence[Point]) -> bool:
    """@brief Whether stored points describe a closed outline: the last repeats the first, 3+ vertices before it.

    @param points Stored points of a polyline.
    @return True when the last point is the first one again (`same_point`) and
        at least MIN_POLYGON_POINTS distinct vertices precede the repeat;
        False otherwise (an open line, or an A-B-A back-and-forth that encloses
        nothing and so stays a line).
    @note User decision A1 (2026-11-09, «Come il footprint»: like the
        footprint): a symbol line that ends on its first point is edited as a
        closed polygon.
    """
    return len(points) > MIN_POLYGON_POINTS and same_point(points[0], points[-1])


def open_vertices(points: Sequence[Point]) -> List[Tuple[float, float]]:
    """@brief The distinct vertices of a stored polygon that repeats its first point at the end.

    @param points Stored points; the last equal to the first (`same_point`)
        marks the closing repeat.
    @return The points as (x, y) floats, the final repeat removed (a list
        without the repeat is returned unchanged).
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) > 1 and same_point(pts[-1], pts[0]):
        pts.pop()
    return pts


def has_closing_repeat(points: Sequence[Point], closed: Optional[bool] = None) -> bool:
    """@brief Whether stored points end on a closing repeat of the first point that is ONE vertex with it.

    @param points Stored points of a polyline.
    @param closed Whether the caller's record is a closed outline (a symbol
        polygon is closed by its key, whatever its points); None decides it
        on the points alone (`is_closed_outline`).
    @return True for a closed outline whose last point is the first one
        again (`same_point`): its handle and its drag treat them as one.
    @note The one rule behind `handle_points` and `move_handle_point`, and
        the same closed-ness the vertex edits use (`open_vertices` strips the
        same repeat), so handles, drags and vertex add/remove agree.
    """
    pts = list(points)
    if closed is None:
        closed = is_closed_outline(pts)
    return bool(closed) and len(pts) > 1 and same_point(pts[0], pts[-1])


def handle_points(points: Sequence[Point], closed: Optional[bool] = None) -> List[Point]:
    """@brief The points of a stored polyline that get a drag handle: the closing repeat has none of its own.

    @param points Stored points of a polyline (a symbol line or polygon, a
        footprint line).
    @param closed The record's closed-ness when the caller knows it (a
        symbol polygon), None to decide it on the points (`has_closing_repeat`).
    @return The points unchanged for an open line; for a closed outline that
        repeats its first point all but the last, whose position is the first
        one's: ONE handle at the closing point (user decision H2,
        2026-11-09, «Sì, una maniglia sola»: yes, a single handle). Handle
        index i is stored point i, so a caller keeps its point indices.
    @note An open line whose ends lie apart, or an A-B-A back-and-forth that
        is not declared closed, keeps a handle per stored point.
    """
    pts = list(points)
    return pts[:-1] if has_closing_repeat(pts, closed) else pts


def move_handle_point(points: Sequence[Point], position: int, point: Point,
                      closed: Optional[bool] = None) -> List[Point]:
    """@brief A copy of stored polyline points with the point under handle `position` moved.

    @param points Stored points of a polyline.
    @param position Handle index (`handle_points`) or stored index.
    @param point The new (x, y) position.
    @param closed As in `handle_points`.
    @return A new list. On a closed outline with its closing repeat
        (`has_closing_repeat`, decided on the points BEFORE the move) the
        closing point is one vertex: moving index 0 or the last index moves
        both the first and the last point, so the outline stays closed (user
        decision H2, 2026-11-09). Any other index, and every index of an open
        line, moves that point alone.
    @throws IndexError When `position` is outside the points.
    """
    pts = list(points)
    if not -len(pts) <= position < len(pts):
        raise IndexError(position)
    moved = type(pts[position])(point) if isinstance(pts[position], (list, tuple)) else point
    if has_closing_repeat(pts, closed) and position in (0, len(pts) - 1, -1, -len(pts)):
        pts[0] = moved
        pts[-1] = type(moved)(moved) if isinstance(moved, list) else moved
    else:
        pts[position] = moved
    return pts


def segment_count(count: int, closed: bool) -> int:
    """@brief Number of segments of `count` vertices.

    @param count Number of vertices.
    @param closed True when a closing side joins the last vertex to the first.
    @return count - 1 for a line, count for a polygon (0 below two vertices).
    """
    if count < 2:
        return 0
    return count if closed else count - 1


def _project(a: Tuple[float, float], b: Tuple[float, float], p: Tuple[float, float]):
    """The foot of p on the segment a-b (clamped) and its distance from p."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    foot = (a[0] + t * dx, a[1] + t * dy)
    return foot, math.hypot(p[0] - foot[0], p[1] - foot[1])


def nearest_segment(points: Sequence[Point], point: Point, closed: bool) -> Optional[SegmentHit]:
    """@brief The segment nearest to `point` and the projection of the point on it.

    @param points Distinct vertices in drawing order (no closing repeat).
    @param point (x, y) query point, same unit.
    @param closed True to include the closing side (last vertex -> first).
    @return A `SegmentHit`, or None with fewer than two vertices.
    @note An exact tie goes to the lower segment index (deterministic, and it
        does not depend on the orientation of Y).
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    q = (float(point[0]), float(point[1]))
    best: Optional[SegmentHit] = None
    for index in range(segment_count(len(pts), closed)):
        foot, distance = _project(pts[index], pts[(index + 1) % len(pts)], q)
        if best is None or distance < best.distance:
            best = SegmentHit(index, foot, distance)
    return best


def nearest_vertex(points: Sequence[Point], point: Point) -> Optional[Tuple[int, float]]:
    """@brief The vertex nearest to `point`.

    @param points Vertices (x, y).
    @param point (x, y) query point.
    @return (index, distance), or None for an empty list; ties go to the lower index.
    """
    best = None
    for index, p in enumerate(points):
        distance = math.hypot(float(point[0]) - float(p[0]), float(point[1]) - float(p[1]))
        if best is None or distance < best[1]:
            best = (index, distance)
    return best


def pick(points: Sequence[Point], point: Point, closed: bool, reach: float) -> Optional[Pick]:
    """@brief Classify a right click on a line or polygon: on a vertex, on a segment, or off it.

    @param points Distinct vertices in drawing order (no closing repeat).
    @param point (x, y) click position, same unit as the points.
    @param closed True for a polygon (its closing side is a segment too).
    @param reach Pick distance, same unit: a UI reach the caller converts from
        screen pixels at the current zoom (`canvas_menus.pick_reach_mm`), not
        a geometric threshold.
    @return A VERTEX pick, a SEGMENT pick, or None when neither the nearest
        vertex nor the nearest segment lies within `reach`.
    @note When BOTH lie within reach, the click goes to whichever is nearer:
        the vertex, or the MIDPOINT of the nearest segment, which stands for
        the segment's interior (a tie goes to the vertex). KiCad's point
        editor represents a side by a handle at its midpoint (EDIT_LINE in
        common/tool/edit_points.cpp, tested after the corner points in
        `EDIT_POINTS::FindPoint`). With this rule a segment shorter than twice
        the reach keeps the middle half of its length for «Aggiungi vertice»
        (add vertex) instead of being covered by its two vertex reaches,
        while on a segment longer than four reaches the vertex keeps its
        whole reach, as a handle drawn on top of its outline does.
    """
    if reach is None or not math.isfinite(float(reach)) or reach < 0:
        return None
    vertex = nearest_vertex(points, point)
    segment = nearest_segment(points, point, closed)
    vertex_ok = vertex is not None and vertex[1] <= reach
    segment_ok = segment is not None and segment.distance <= reach
    if vertex_ok and segment_ok:
        a = points[segment.index]
        b = points[(segment.index + 1) % len(points)]
        middle = ((float(a[0]) + float(b[0])) / 2.0, (float(a[1]) + float(b[1])) / 2.0)
        if math.hypot(float(point[0]) - middle[0], float(point[1]) - middle[1]) < vertex[1]:
            vertex_ok = False
    if vertex_ok:
        index = vertex[0]
        return Pick(VERTEX, index, (float(points[index][0]), float(points[index][1])), vertex[1])
    if segment_ok:
        return Pick(SEGMENT, segment.index, segment.point, segment.distance)
    return None


def insert_vertex(points: Sequence[Point], segment_index: int, point: Point, closed: bool) -> List[Tuple[float, float]]:
    """@brief A new vertex list with `point` inserted on segment `segment_index`.

    @param points Distinct vertices in drawing order (no closing repeat).
    @param segment_index Segment receiving the vertex (see `SegmentHit.index`);
        the closing side of a polygon is index n-1 and appends the vertex.
    @param point (x, y) of the new vertex, already projected and snapped by
        the caller (a grid snap may move it a little off the segment, as a
        vertex drag does).
    @param closed True for a polygon.
    @return A new list, one vertex longer; the input is not changed.
    @throws VertexEditRefused BAD_INDEX for a segment that does not exist;
        COINCIDENT when the point is the same stored point (`same_point`) as
        either end of the segment (a duplicate vertex draws nothing and cannot
        be told apart).
    @note Only the two pairs the insertion creates are checked: duplicates
        already in the input are left for a removal to clean up.
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    count = segment_count(len(pts), closed)
    if isinstance(segment_index, bool) or not isinstance(segment_index, int) or not 0 <= segment_index < count:
        raise VertexEditRefused(BAD_INDEX, f"no segment {segment_index!r}")
    new = (float(point[0]), float(point[1]))
    if same_point(new, pts[segment_index]) or same_point(new, pts[(segment_index + 1) % len(pts)]):
        raise VertexEditRefused(COINCIDENT, "the new vertex coincides with an end of its segment")
    return pts[:segment_index + 1] + [new] + pts[segment_index + 1:]


def remove_vertex(points: Sequence[Point], index: int, closed: bool,
                  min_points_required: Optional[int] = None) -> List[Tuple[float, float]]:
    """@brief A new vertex list without vertex `index`, refused when it would be invalid.

    @param points Distinct vertices in drawing order (no closing repeat).
    @param index Vertex to remove.
    @param closed True for a polygon.
    @param min_points_required Fewest vertices the caller's drawing kind
        allows; None (or a lower value) means `min_points(closed)`, which is
        never undercut.
    @return A new list, one vertex shorter; the input is not changed.
    @throws VertexEditRefused BAD_INDEX for an unknown vertex; TOO_FEW_POINTS
        when fewer than the minimum would remain; NO_AREA when a polygon's
        remaining vertices enclose no area (`polygon_has_area`, the ONE rule
        of both tabs); NO_LENGTH when a line's remaining vertices are all the
        same point; DUPLICATE when the two vertices the removal makes
        neighbours are the same stored point (removing B from A, B, A, C).
    @note Only the pair the removal creates is checked, so removing one of
        two duplicates already in the input is allowed (it cleans them up).
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(pts):
        raise VertexEditRefused(BAD_INDEX, f"no vertex {index!r}")
    floor = min_points(closed)
    if min_points_required is not None:
        floor = max(floor, int(min_points_required))
    if len(pts) - 1 < floor:
        raise VertexEditRefused(TOO_FEW_POINTS, f"at least {floor} vertices are needed")
    result = pts[:index] + pts[index + 1:]
    if not closed and all(same_point(p, result[0]) for p in result):
        raise VertexEditRefused(NO_LENGTH, "the line would have no length")
    # The one new neighbour pair: the vertices on both sides of the removed one
    # (an end of an open line has a single neighbour and creates no pair).
    before, after = index - 1, index + 1
    if closed:
        before, after = before % len(pts), after % len(pts)
    if before >= 0 and after < len(pts) and before != after and same_point(pts[before], pts[after]):
        raise VertexEditRefused(DUPLICATE, "two consecutive vertices would be the same point")
    if closed and not polygon_has_area(result, resolution=STORED_POINT_RESOLUTION_MM):
        raise VertexEditRefused(NO_AREA, "the polygon would enclose no area")
    return result
