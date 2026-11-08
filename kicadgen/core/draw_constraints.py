"""@brief Angle constraint for drawing tools: free, 45 degree or 90 degree segments.

Pure geometry, no Qt (plan `docs/footprint-drawing-kicad-plan.md`, A6). The
footprint preview (mm, Y down) and the symbol editor (mm, Y up) both feed it
plain (x, y) tuples: the functions never assume an orientation, because the
set of allowed directions (multiples of 45 or 90 degrees) is symmetric under a
Y flip. A constrained point is the projection of the pointer onto the allowed
ray closest in angle to the anchor-to-pointer direction, as KiCad does while
drawing a line in 45 degree mode.
"""
from __future__ import annotations

import math
from enum import Enum
from typing import Optional, Sequence, Tuple

Point = Sequence[float]

#: sqrt(1/2), the exact component of a unit vector on a diagonal ray.
_S = math.sqrt(0.5)


class AngleMode(str, Enum):
    """@brief Angle constraint of the segment from the anchor to the pointer.

    @note The values are short strings so the mode can be saved as is in the
          per-tab configuration and read back with ``AngleMode(value)``.
    """

    FREE = "free"
    DEG45 = "45"
    DEG90 = "90"


#: Cycling order of Shift+Space and of the panel control: free -> 90 -> 45 -> free.
#: It follows the order in which KiCad 9 declares its line-mode actions and shows
#: their toolbar buttons (`lineModeFree`, `lineMode90`, `lineMode45` in
#: pcbnew/tools/pcb_actions.cpp). The single source of the cycle: the click-tool
#: router (`kicadgen/gui/canvas_clicktools.py`) imports it.
ANGLE_MODES: Tuple[AngleMode, ...] = (AngleMode.FREE, AngleMode.DEG90, AngleMode.DEG45)

#: Allowed unit directions per mode, in a fixed order that also breaks ties:
#: the first direction in this tuple wins an exact tie (see `constrain`).
_DIRECTIONS = {
    AngleMode.DEG90: ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)),
    AngleMode.DEG45: (
        (1.0, 0.0), (_S, _S), (0.0, 1.0), (-_S, _S),
        (-1.0, 0.0), (-_S, -_S), (0.0, -1.0), (_S, -_S),
    ),
}


def next_mode(mode) -> AngleMode:
    """@brief Return the mode that follows `mode` in the Shift+Space cycle.

    @param mode An `AngleMode` or its string value.
    @return The next mode of `ANGLE_MODES`, wrapping from 45 back to free.
    @throws ValueError When `mode` is not a known angle mode.
    """
    current = AngleMode(mode)
    index = ANGLE_MODES.index(current)
    return ANGLE_MODES[(index + 1) % len(ANGLE_MODES)]


def constrain(anchor: Point, point: Point, mode) -> Tuple[float, float]:
    """@brief Constrain the segment anchor -> point to the directions allowed by `mode`.

    FREE returns the point unchanged. DEG90 and DEG45 pick the allowed ray from
    the anchor with the largest projection of the pointer vector (equivalently
    the smallest angle from it) and return the foot of the perpendicular from
    the pointer onto that ray, so the result's length is the projected length
    ``|v| * cos(angle difference)``; a pointer already on an allowed ray is
    returned unchanged.

    @param anchor (x, y) start of the segment, in any length unit.
    @param point (x, y) pointer position, same unit as `anchor`.
    @param mode An `AngleMode` or its string value.
    @return The constrained (x, y) as floats.
    @throws ValueError When `mode` is not a known angle mode.
    @note Ties (a pointer exactly halfway between two rays, e.g. 22.5 degrees in
          DEG45 or 45 degrees in DEG90) go to the ray listed first in the fixed
          order 0, 45, 90, ... 315 degrees measured from +X towards +Y. The
          order is in the coordinate system the caller passes, so it does not
          depend on whether Y points up or down on screen. A zero-length
          segment returns the anchor.
    """
    mode = AngleMode(mode)
    ax, ay = float(anchor[0]), float(anchor[1])
    px, py = float(point[0]), float(point[1])
    if mode is AngleMode.FREE:
        return (px, py)
    best = _best_direction(px - ax, py - ay, mode)
    if best is None:
        return (ax, ay)
    (ux, uy), proj = best
    return (ax + proj * ux, ay + proj * uy)


def _best_direction(vx: float, vy: float, mode: AngleMode):
    """The allowed unit direction with the largest projection of (vx, vy) and that projection.

    None for a zero vector. Ties keep the direction listed first in `_DIRECTIONS`.
    """
    if vx == 0.0 and vy == 0.0:
        return None
    best_dir = None
    best_proj = -math.inf
    for ux, uy in _DIRECTIONS[mode]:
        proj = vx * ux + vy * uy
        # Strict comparison: an exact tie keeps the earlier direction.
        if proj > best_proj:
            best_proj, best_dir = proj, (ux, uy)
    return best_dir, best_proj


def _grid_round(value: float, step: float, origin: float) -> float:
    """`value` rounded to the nearest node origin + k * step (Python's round, half to even)."""
    return origin + round((value - origin) / step) * step


def constrain_to_grid(anchor: Point, point: Point, mode, step: Optional[float], *,
                      grid_origin: Point = (0.0, 0.0),
                      ndigits: Optional[int] = None) -> Tuple[float, float]:
    """@brief The ONE rule of both tabs for "angle constraint + grid": the point a click adds.

    DEG90 and DEG45: the pointer is first constrained to the allowed ray from
    the anchor (`constrain`), then the run along that ray is rounded so that
    the run on EACH axis is a whole number k of `step` measured FROM THE
    ANCHOR: the result is ``anchor + k * step * g`` where g is the ray's
    direction scaled so its largest component is 1, i.e. (+-1, 0) or (0, +-1)
    on an axis ray and (+-1, +-1) on a diagonal. Along the ray this is a
    length multiple of `step` on an axis ray and of ``step * sqrt(2)`` on a
    diagonal; k is the nearest integer to ``projected length / (|g| * step)``.
    So the result is always exactly on the allowed ray (both axis runs are the
    same k * step on a diagonal), and it is on the grid whenever the anchor
    is: an on-grid anchor plus whole steps on each axis is an on-grid node. An
    off-grid anchor gives an off-grid result that keeps the direction: the
    angle is the user's explicit request, the grid is the convenience.

    FREE: the anchor plays no role; the point is rounded to the absolute grid,
    each coordinate to the nearest ``grid_origin + k * step``, as a click with
    no angle mode.

    @param anchor (x, y) start of the segment (the last collected point), mm.
    @param point (x, y) raw pointer position, same unit.
    @param mode An `AngleMode` or its string value.
    @param step Grid step, same unit; None, zero, negative or non-finite means
           no rounding (DEG90/DEG45 then return `constrain`, FREE the point).
    @param grid_origin (x, y) node the absolute grid passes through (FREE
           only; the footprint's grid follows its drawing origin).
    @param ndigits When given, both coordinates are finally rounded to this
           many decimals (4 = the stored 0.0001 mm). Rounding both run
           components by the same rule keeps a diagonal on its ray up to that
           rounding.
    @return The (x, y) point as floats.
    @throws ValueError When `mode` is not a known angle mode.
    @note Rounding uses Python's `round` (half to even), like the tabs' grid
          rounding. A pointer that rounds to k = 0 returns the anchor itself;
          the click tool then refuses it as coincident, the right outcome for
          a segment shorter than half a grid step.
    """
    mode = AngleMode(mode)
    ax, ay = float(anchor[0]), float(anchor[1])
    px, py = float(point[0]), float(point[1])
    step = None if step is None else float(step)
    rounding = step is not None and math.isfinite(step) and step > 0.0
    if mode is AngleMode.FREE:
        if rounding:
            ox, oy = float(grid_origin[0]), float(grid_origin[1])
            x, y = _grid_round(px, step, ox), _grid_round(py, step, oy)
        else:
            x, y = px, py
    elif not rounding:
        x, y = constrain((ax, ay), (px, py), mode)
    else:
        best = _best_direction(px - ax, py - ay, mode)
        if best is None:
            x, y = ax, ay
        else:
            (ux, uy), proj = best
            peak = max(abs(ux), abs(uy))
            # Direction scaled to a largest component of exactly 1: components in {-1, 0, 1}.
            gx, gy = round(ux / peak), round(uy / peak)
            k = round(proj / (math.hypot(gx, gy) * step))
            x, y = ax + k * step * gx, ay + k * step * gy
    if ndigits is not None:
        x, y = round(x, ndigits) + 0.0, round(y, ndigits) + 0.0
    return (x, y)


def polygon_has_area(points: Sequence[Point], *, resolution: float = 1e-4) -> bool:
    """@brief Whether a polygon's vertices enclose an area (are not all on one line).

    The two vertices farthest apart define the polygon's longest chord; the
    polygon has an area when some vertex lies farther than `resolution` from
    the line through them. Self-intersecting outlines (a bow tie) have area by
    this rule and are accepted, as KiCad accepts them.

    @param points Vertices (x, y) in drawing order, mm.
    @param resolution Coordinate rounding of the STORED points (0.0001 mm,
           the 4 decimals every drawing is saved with): a vertex within one
           rounding unit of the chord may be a collinear point that rounding
           pushed off the line, so it does not prove an area. It is the
           storage resolution, not a threshold tuned on any drawing.
    @return False for fewer than three vertices, coincident vertices, or all
            vertices within `resolution` of one line; True otherwise.
    @note O(n^2) for the longest chord, fine for hand-drawn polygons (tens of
          vertices).
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 3:
        return False
    best = -1.0
    a = b = pts[0]
    for i in range(len(pts)):
        xi, yi = pts[i]
        for j in range(i + 1, len(pts)):
            d2 = (pts[j][0] - xi) ** 2 + (pts[j][1] - yi) ** 2
            if d2 > best:
                best, a, b = d2, pts[i], pts[j]
    length = math.sqrt(best)
    if length <= resolution:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    widest = max(abs((x - a[0]) * dy - (y - a[1]) * dx) / length for x, y in pts)
    return widest > resolution
