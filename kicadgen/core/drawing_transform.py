"""@brief Quarter turns and mirrors of point lists (mm, Y down), shared by the drawing editors.

Pure geometry, no Qt: the footprint preview turns and mirrors its user
drawings with these functions (plan `docs/canvas-menus-plan.md`, section
3.2). Coordinates follow the footprint and the screen: X right, Y DOWN, so a
turn "counter-clockwise on screen" is (x, y) -> (cx + (y - cy), cy - (x - cx)).
Results are rounded to 0.1 um, the precision of the saved drawings.
"""
from __future__ import annotations

from typing import Iterable, Sequence

Point = Sequence[float]


def _r(value: float) -> float:
    """Round to 0.1 um and turn -0.0 into 0.0."""
    return round(float(value), 4) + 0.0


def bounds_centre(points: Iterable[Point]) -> tuple:
    """@brief Centre of the axis-aligned bounding box of the points.

    @param points (x, y) pairs, mm.
    @return (cx, cy) mm.
    @throws ValueError When `points` is empty.
    """
    points = [(float(x), float(y)) for x, y in points]
    if not points:
        raise ValueError("no points")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def rotate_points_90(points: Iterable[Point], centre: Point, counter_clockwise_on_screen: bool) -> list:
    """@brief Turn points by a quarter turn about `centre`, as seen on a Y-down screen.

    @param points (x, y) pairs, mm, Y down.
    @param centre (cx, cy) mm.
    @param counter_clockwise_on_screen True for «antiorario» as the user sees it.
    @return [[x, y], ...] rounded to 0.1 um, same order as the input.
    @note Four turns in the same sense give the input back (to rounding).
    """
    cx, cy = float(centre[0]), float(centre[1])
    result = []
    for x, y in points:
        dx, dy = float(x) - cx, float(y) - cy
        if counter_clockwise_on_screen:
            nx, ny = cx + dy, cy - dx
        else:
            nx, ny = cx - dy, cy + dx
        result.append([_r(nx), _r(ny)])
    return result


def mirror_points_x(points: Iterable[Point], centre_x: float) -> list:
    """@brief Mirror points left-right about the vertical line x = `centre_x`.

    @param points (x, y) pairs, mm.
    @param centre_x X of the mirror axis, mm.
    @return [[x, y], ...] rounded to 0.1 um, same order as the input.
    """
    cx = float(centre_x)
    return [[_r(2.0 * cx - float(x)), _r(float(y))] for x, y in points]
