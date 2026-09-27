"""@brief Silkscreen helpers shared by the footprint generators.

Two rules every generator must respect, whatever the part:

* silkscreen never lies on exposed copper: KiCad's DRC reports silk over a
  pad, and the fabricator clips it anyway, so outlines are split around every
  pad with a clearance (SILK_PAD_CLEARANCE_MM from the copper, measured to the
  edge of the stroke);
* the pin-1 marker identifies the pad numbered "1" (or the lowest number when
  there is no "1"), never "the first pad produced", which depends on side
  order and numbering direction.

All coordinates are footprint millimetres, KiCad axes (X right, Y down).
"""

from __future__ import annotations

import math
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from .footprint import FpCircle, FpLine, FpRect
from ..services.messages import tx

#: Minimum distance between silkscreen and exposed copper, mm. KiCad
#: Library Convention F5.1 asks for 0.2 mm between silk and pads; the
#: stroke half-width is added on top, so the INK keeps that clearance.
SILK_PAD_CLEARANCE_MM = 0.2

#: Pieces of an outline shorter than this are dropped: they would be specks
#: of ink between two pads, not a readable outline.
MIN_SILK_SEGMENT_MM = 0.2

Box = Tuple[float, float, float, float]


def keepout_box(x0: float, y0: float, x1: float, y1: float, line_width: float,
                clearance: float = SILK_PAD_CLEARANCE_MM) -> Box:
    """@brief Area a silk stroke centre line must avoid around one copper box.

    @param x0 Copper bounding box min X, mm.
    @param y0 Copper bounding box min Y, mm.
    @param x1 Copper bounding box max X, mm.
    @param y1 Copper bounding box max Y, mm.
    @param line_width Silk stroke width, mm.
    @param clearance Required copper-to-ink distance, mm.
    @return (x0, y0, x1, y1) grown by clearance + line_width / 2.
    @note A bounding box is a conservative envelope of round and rounded
        pads: the silk can only stay further away than strictly required.
    """
    grow = clearance + line_width / 2
    return (min(x0, x1) - grow, min(y0, y1) - grow, max(x0, x1) + grow, max(y0, y1) + grow)


def _inside_interval(start, end, box: Box) -> Optional[Tuple[float, float]]:
    """Parameter interval (t0, t1) of segment start->end strictly inside `box`, or None."""
    (sx, sy), (ex, ey) = start, end
    t0, t1 = 0.0, 1.0
    for p0, d, lo, hi in ((sx, ex - sx, box[0], box[2]), (sy, ey - sy, box[1], box[3])):
        if abs(d) < 1e-12:
            if not (lo < p0 < hi):
                return None
            continue
        a, b = (lo - p0) / d, (hi - p0) / d
        if a > b:
            a, b = b, a
        t0, t1 = max(t0, a), min(t1, b)
        if t0 >= t1:
            return None
    return (t0, t1)


def clip_segment(start, end, keepouts: Iterable[Box]) -> List[Tuple[tuple, tuple]]:
    """@brief Split a segment into the pieces that lie outside every keepout box.

    @param start (x, y) of the segment start, mm.
    @param end (x, y) of the segment end, mm.
    @param keepouts Boxes (x0, y0, x1, y1) the segment must not enter.
    @return List of (start, end) pieces, in order along the segment; pieces
        shorter than MIN_SILK_SEGMENT_MM are dropped.
    """
    cuts = sorted(i for i in (_inside_interval(start, end, box) for box in keepouts) if i)
    length = math.dist(start, end)
    pieces, cursor = [], 0.0
    for t0, t1 in cuts + [(1.0, 1.0)]:
        if t0 > cursor and (t0 - cursor) * length >= MIN_SILK_SEGMENT_MM:
            pieces.append((cursor, t0))
        cursor = max(cursor, t1)
    point = lambda t: (round(start[0] + (end[0] - start[0]) * t, 6) + 0.0,
                       round(start[1] + (end[1] - start[1]) * t, 6) + 0.0)
    return [(point(a), point(b)) for a, b in pieces]


def silk_rectangle(start, end, keepouts: Sequence[Box], layer: str = "F.SilkS",
                   width: float = 0.12) -> list:
    """@brief Silkscreen rectangle outline that stays off the copper.

    @param start (x, y) of one corner, mm.
    @param end (x, y) of the opposite corner, mm.
    @param keepouts Boxes from keepout_box(), one per pad.
    @param layer Silk layer name.
    @param width Stroke width, mm.
    @return [FpRect] when no pad touches the outline (the file is then the
        same as before this rule existed), otherwise the FpLine pieces of the
        four sides that lie outside every keepout.
    """
    (x0, y0), (x1, y1) = start, end
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    sides = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    if not any(_inside_interval(a, b, box) for a, b in sides for box in keepouts):
        return [FpRect(start=start, end=end, layer=layer, width=width)]
    return [FpLine(start=piece_start, end=piece_end, layer=layer, width=width)
            for side_start, side_end in sides
            for piece_start, piece_end in clip_segment(side_start, side_end, keepouts)]


def _natural_key(number) -> tuple:
    """Sort key that orders "2" before "10" and never compares int with str."""
    parts = re.split(r"(\d+)", str(number))
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p != "")


def pin_one(numbers: Iterable) -> Optional[str]:
    """@brief The pad number the pin-1 marker must point at.

    @param numbers Pad numbers of the footprint (any order, str or int).
    @return "1" when present, otherwise the lowest number in natural order
        ("A1" before "A2" before "A10"); None when there are no numbered pads.
    """
    values = [str(n) for n in numbers if str(n).strip()]
    if not values:
        return None
    if "1" in values:
        return "1"
    return min(values, key=_natural_key)


def marker_clear_of(center, radius: float, keepouts: Sequence[Box]) -> bool:
    """@brief Tell whether a round marker stays outside every keepout box.

    @param center Marker centre (x, y), mm.
    @param radius Marker radius, mm.
    @param keepouts Boxes from keepout_box().
    @return True when the disc does not enter any box.
    """
    cx, cy = center
    for x0, y0, x1, y1 in keepouts:
        dx = max(x0 - cx, 0.0, cx - x1)
        dy = max(y0 - cy, 0.0, cy - y1)
        if math.hypot(dx, dy) < radius - 1e-9:
            return False
    return True


def push_marker_clear(center, direction, radius: float, keepouts: Sequence[Box],
                      step: float = 0.1, limit: float = 50.0) -> tuple:
    """@brief Move a marker along `direction` until it clears every keepout.

    @param center Preferred marker centre (x, y), mm.
    @param direction Unit-less (dx, dy) to move along when it is on copper.
    @param radius Radius of the marker INK (stroke included), mm.
    @param keepouts Boxes from keepout_box().
    @param step Search step, mm.
    @param limit Maximum displacement, mm.
    @return The first clear centre, rounded to 1 um; the preferred one when
        it is already clear (so existing footprints keep their marker).
    @throws ValueError When no clear position exists within `limit`: ink
        on exposed copper is never returned silently.
    """
    norm = math.hypot(*direction) or 1.0
    ux, uy = direction[0] / norm, direction[1] / norm
    moved = 0.0
    x, y = center
    while not marker_clear_of((x, y), radius, keepouts):
        if moved >= limit:
            raise ValueError(tx(
                "Segno del pin 1: nessuna posizione libera dal rame entro {limit:.0f} mm "
                "dal punto previsto.", limit=limit))
        moved += step
        x, y = center[0] + ux * moved, center[1] + uy * moved
    return (round(x, 6) + 0.0, round(y, 6) + 0.0)


def pin_one_marker(center, keepouts: Sequence[Box], direction,
                   radius: float = 0.25, layer: str = "F.SilkS",
                   width: float = 0.12) -> FpCircle:
    """@brief Filled silk dot for pin 1, pushed off the copper if needed.

    @param center Preferred centre, mm.
    @param keepouts Boxes from keepout_box(..., line_width=0) for every pad:
        copper grown by the silk clearance only.
    @param direction Direction to move along when the preferred centre is on copper.
    @param radius Dot radius (geometry of the circle), mm.
    @param layer Silk layer.
    @param width Stroke width of the circle outline, mm.
    @return The FpCircle to add to the footprint.
    @throws ValueError When the dot cannot be placed off the copper.
    @note The ink of a stroked circle reaches radius + width / 2: that is the
        radius kept clear, so the ink stays SILK_PAD_CLEARANCE_MM from the
        copper as promised (counting only `radius` left 0.14 mm).
    """
    ink_radius = radius + width / 2
    return FpCircle(center=push_marker_clear(center, direction, ink_radius, keepouts),
                    radius=radius, layer=layer, width=width, fill=True)
