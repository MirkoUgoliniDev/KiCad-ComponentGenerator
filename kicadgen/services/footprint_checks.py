"""@brief Geometric checks of a footprint built in memory: the «Controlli» list of the footprint tab.

The checks read a `core.footprint.Footprint` (pads and graphics) and nothing
else: no part name, no package family, no file on disk. Each rule yields a
`GeometryFinding` with the KLC rule it comes from, a severity, a message, a
remedy and the indices of the pads and graphics concerned, so the preview can
select them. Every threshold says where it comes from (a published value);
none is tuned to a part.

Rules:
- pads_overlap (error): two pads with different numbers whose copper
  overlaps or touches (KiCad refuses the net assignment, DRC flags it).
- silk_clearance (warning, KLC F5.1): silkscreen ink closer than
  `SILK_PAD_CLEARANCE_MM` to a pad's copper.
- courtyard_enclosure (warning, KLC F5.3): a pad or a fabrication-layer
  graphic that the courtyard does not enclose with the minimum clearance.
- annular_ring (warning): a plated hole whose copper ring is thinner than
  `ANNULAR_RING_MIN_MM` (the KiCad getting-started guide's advice, not a KLC
  rule: said so in the message).
- pin_one_marker (warning, KLC F5.1/F5.2): the footprint has a pin 1 but no
  marker of it on the silkscreen or fabrication layer.
- silk_stroke_width (warning, KLC F5.1): a silkscreen stroke thinner than
  `SILK_MIN_WIDTH_MM`, the low end of the KLC's allowed range. ONLY the
  lower bound is checked (see `SILK_MIN_WIDTH_MM`).
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from types import SimpleNamespace
from typing import Iterable, Optional, Sequence

from ..core.footprint import (FpArc, FpCircle, FpCurve, FpDimension, FpLine, FpPoly, FpRect, FpTextBox,
                              Footprint, Pad, bezier_points, pad_copper_centre)
from ..core.package_geometry import _rotate_kicad, overlapping_pad_pairs, pad_copper_shape
from ..core.silk import SILK_PAD_CLEARANCE_MM, pin_one
from .klc_checks import Finding
from .messages import tx

#: Minimum clearance, mm, between the courtyard and the component it
#: encloses: the generic value of KLC F5.3 (0.25 mm). Connectors take 0.5 mm
#: there, but the checker cannot tell a connector from its geometry, so it
#: holds every footprint to the generic minimum only.
COURTYARD_MIN_CLEARANCE_MM = 0.25

#: Minimum copper ring around a plated hole, mm. The KiCad getting-started
#: guide (docs/reference/kicad9-getting-started-notes.md) advises a ring of at
#: least 0.15 mm, preferably more; the KLC publishes no number for it, so the
#: finding is a warning that names the guide, never an error.
ANNULAR_RING_MIN_MM = 0.15

#: Largest gap, mm, allowed between a sampled chord and the true circle or
#: arc it stands for (the sagitta): the segment count follows from the
#: radius so a large silkscreen frame circle is measured as finely as a
#: small dot. Half the 0.01 mm grid of KiCad's courtyard, a resolution of
#: the measurement, not a geometric quantity.
_CURVE_SAGITTA_MM = 0.005

#: Bounds of the segment count of one full circle: never coarser than a
#: dodecagon, never finer than the sagitta needs on a 100 mm circle.
_CURVE_SEGMENTS_MIN = 12
_CURVE_SEGMENTS_MAX = 720


def _circle_segments(radius: float, sweep: float = 2 * math.pi) -> int:
    """Segments for an arc of `radius` and `sweep` (radians) within `_CURVE_SAGITTA_MM`."""
    radius = abs(float(radius))
    if radius <= _CURVE_SAGITTA_MM:
        return _CURVE_SEGMENTS_MIN
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - _CURVE_SAGITTA_MM / radius)))
    full = math.ceil(2 * math.pi / step) if step > 0 else _CURVE_SEGMENTS_MAX
    full = max(_CURVE_SEGMENTS_MIN, min(_CURVE_SEGMENTS_MAX, full))
    return max(2, math.ceil(full * abs(sweep) / (2 * math.pi)))

#: Thinnest silkscreen stroke, mm, the check accepts without a warning.
#: Source: KLC F5.1 «Silkscreen layer requirements» (KiCad Library
#: Conventions, content/footprint/F5/F5.1.adoc of gitlab.com/kicad/libraries/
#: klc, read 2026-11-09): «Silkscreen line width is between 0.10mm and
#: 0.15mm», nominally 0.12 mm, «0.1mm is allowed for high density designs».
#: So 0.10 mm is the KLC's own lower bound, not a fab figure: a stroke of
#: exactly 0.10 mm passes, a thinner one is flagged. 0.12 mm stays the default
#: width of a silkscreen drawing (`footprint_layers`). ONLY THIS LOWER BOUND
#: IS CHECKED: F5.1's upper end (0.15 mm) is not, a thicker stroke breaks the
#: KLC range but still prints, and the risk this warning guards against is a
#: stroke that does not print. A board house may ask for more than 0.10 mm
#: (several quote 0.15 mm): that is the user's fab choice.
SILK_MIN_WIDTH_MM = 0.10


def bezier_segments(control) -> int:
    """@brief Number of equal-parameter segments that follow a cubic Bezier within `_CURVE_SAGITTA_MM`.

    @param control The four control points, mm.
    @return N in [1, `_CURVE_SEGMENTS_MAX`]: the standard flattening bound
        for a polynomial curve, distance(curve, chord polyline) <=
        max|B''| / (8 N^2), with max|B''| <= 6 x the largest second
        difference of the control points (|p0 - 2 p1 + p2|, |p1 - 2 p2 + p3|),
        solved for N. The count follows from the curve's own bending, so a
        long gentle curve and a tight hook are measured equally finely; a
        straight curve (collinear, evenly spaced controls) needs one segment.
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = ((float(x), float(y)) for x, y in control)
    bend = max(math.hypot(x0 - 2 * x1 + x2, y0 - 2 * y1 + y2), math.hypot(x1 - 2 * x2 + x3, y1 - 2 * y2 + y3))
    if bend <= 0.0:
        return 1
    count = math.ceil(math.sqrt(6.0 * bend / (8.0 * _CURVE_SAGITTA_MM)))
    return max(1, min(_CURVE_SEGMENTS_MAX, count))

#: Float noise absorbed by the comparisons, mm.
_EPS = 1e-6

#: Code -> Italian source of the remedy (translated through `tx`).
GEOMETRY_HINTS = {
    'pads_overlap': ('Nella scheda «Anteprima footprint» sposta uno dei due pad (trascinalo o scrivi la '
                     'posizione nel suo dialog) finché il rame non si tocca più.'),
    'silk_clearance': ('Sposta o accorcia il disegno sulla serigrafia (tasto destro sul disegno) oppure '
                       'portalo sul layer di fabbricazione: la serigrafia stampata sopra il rame non si legge '
                       'e la fabbrica la taglia.'),
    'courtyard_enclosure': ('Il courtyard viene ridisegnato attorno a pad e corpo da «Applica ai file»; se '
                            'un disegno di fabbricazione resta fuori, riportalo dentro il corpo o rendilo '
                            'un commento.'),
    'annular_ring': ('Nel dialog del pad allarga il pad o riduci il foro: con un anello sottile la '
                     'metallizzazione può staccarsi in saldatura.'),
    'pin_one_marker': ('Disegna un cerchio sulla serigrafia accanto al pad del pin 1 (strumento cerchio '
                       'della palette, più vicino a lui che a ogni altro pad) oppure dai al pin 1 una forma '
                       'di pad diversa dagli altri: chi monta la scheda deve riconoscerlo.'),
    'silk_stroke_width': ('Apri le proprietà del disegno (tasto destro sul disegno) e porta lo spessore ad '
                          'almeno 0.10 mm, meglio 0.12 mm: la KLC F5.1 ammette da 0.10 a 0.15 mm, 0.12 mm '
                          'è il valore nominale.'),
}


def has_copper(pad: Pad) -> bool:
    """@brief Whether a pad carries copper the copper rules apply to.

    @param pad A `core.footprint.Pad`.
    @return False for a non-plated mechanical hole (its "pad" is a mask
        opening, as the generators write it), True otherwise.
    """
    return pad.pad_type != 'np_thru_hole'


@dataclass(frozen=True)
class GeometryFinding(Finding):
    """@brief A geometric finding with the elements it concerns.

    @param pads Indices into `Footprint.pads` of the pads concerned.
    @param graphics Indices into `Footprint.graphics` of the graphics concerned.
    """
    pads: tuple = ()
    graphics: tuple = ()


def geometry_hint(code: str) -> str:
    """@brief The translated remedy of a geometric rule, '' for an unknown code.

    @param code A key of `GEOMETRY_HINTS`.
    @return The hint in the interface language.
    """
    source = GEOMETRY_HINTS.get(code)
    return tx(source) if source else ''


def _finding(code: str, severity: str, message: str, pads=(), graphics=()) -> GeometryFinding:
    return GeometryFinding(code, severity, message, geometry_hint(code), tuple(pads), tuple(graphics))


# ── Pad copper ─────────────────────────────────────────────────────────────

def pad_copper(pad: Pad) -> tuple:
    """@brief The copper of a written pad as (core points, radius), mm, like `pad_copper_shape`.

    @param pad A `core.footprint.Pad`.
    @return (core, radius): the convex core and the distance the copper
        extends beyond it. A trapezoid's core is its four real corners
        (from `rect_delta`, KiCad's convention: one side shortened and the
        opposite one lengthened by half the delta), radius 0.
    @note The copper centre includes a castellated pad's drill offset
        (`pad_copper_centre`): KiCad places the copper there, not at `at`.
    """
    cx, cy = pad_copper_centre(pad)
    if pad.shape == "trapezoid" and pad.rect_delta is not None:
        # KiCad (PAD::TransformShapeToPolygon): with delta (dx, dy) the
        # corners of the half-size box move by dy/2 along X on the top and
        # bottom sides, by dx/2 along Y on the left and right sides.
        half_x, half_y = float(pad.size[0]) / 2, float(pad.size[1]) / 2
        dx, dy = (float(v) for v in pad.rect_delta)
        local = [(-half_x - dy / 2, half_y + dx / 2), (-half_x + dy / 2, -half_y - dx / 2),
                 (half_x - dy / 2, -half_y + dx / 2), (half_x + dy / 2, half_y - dx / 2)]
        angle = math.radians(float(pad.rotation or 0.0))
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        core = []
        for x, y in local:
            rx, ry = _rotate_kicad(x, y, cos_a, sin_a)
            core.append((cx + rx, cy + ry))
        return core, 0.0
    shape = pad.shape if pad.shape in ("circle", "oval", "roundrect", "rect") else "rect"
    probe = SimpleNamespace(x_mm=cx, y_mm=cy, pad_length_mm=float(pad.size[0]),
                            pad_width_mm=float(pad.size[1]), shape=shape,
                            rotation_deg=float(pad.rotation or 0.0))
    return pad_copper_shape(probe)


def copper_box(pad: Pad) -> tuple:
    """@brief Axis-aligned box (x0, y0, x1, y1), mm, enclosing a pad's copper.

    @param pad A `core.footprint.Pad`.
    @return The box; exact for the shapes `pad_copper` describes.
    """
    core, radius = pad_copper(pad)
    xs = [point[0] for point in core]
    ys = [point[1] for point in core]
    return (min(xs) - radius, min(ys) - radius, max(xs) + radius, max(ys) + radius)


# ── Distances ──────────────────────────────────────────────────────────────

def _point_segment_distance(p, a, b) -> float:
    """Distance from point `p` to segment `a`-`b`, mm."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _orientation(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d) -> bool:
    """True when segments a-b and c-d cross or touch."""
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0:
        return True
    # Collinear or touching ends: the point-to-segment distances decide.
    return min(_point_segment_distance(c, a, b), _point_segment_distance(d, a, b),
               _point_segment_distance(a, c, d), _point_segment_distance(b, c, d)) <= _EPS


def _segment_segment_distance(a, b, c, d) -> float:
    """Distance between segments a-b and c-d, mm (0 when they cross)."""
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(_point_segment_distance(c, a, b), _point_segment_distance(d, a, b),
               _point_segment_distance(a, c, d), _point_segment_distance(b, c, d))


def _inside_convex(p, polygon) -> bool:
    """True when `p` lies inside (or on) the convex polygon given in order."""
    signs = [_orientation(polygon[i], polygon[(i + 1) % len(polygon)], p) for i in range(len(polygon))]
    return all(s >= -_EPS for s in signs) or all(s <= _EPS for s in signs)


def segment_copper_distance(a, b, copper: tuple) -> float:
    """@brief Distance, mm, from segment a-b to a pad's copper (0 when they overlap).

    @param a Segment start (x, y), mm.
    @param b Segment end (x, y), mm.
    @param copper (core, radius) as `pad_copper` gives it.
    @return The distance between the segment's centre line and the copper
        edge; a stroke width is the caller's business.
    """
    core, radius = copper
    if len(core) == 1:
        gap = _point_segment_distance(core[0], a, b)
    elif len(core) == 2:
        gap = _segment_segment_distance(a, b, core[0], core[1])
    else:
        if _inside_convex(a, core) or _inside_convex(b, core):
            gap = 0.0
        else:
            gap = min(_segment_segment_distance(a, b, core[i], core[(i + 1) % len(core)])
                      for i in range(len(core)))
    return max(0.0, gap - radius)


def _arc_points(start, mid, end) -> list:
    """Points along the arc through three points (the chord when degenerate), sampled within the sagitta."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    radius = math.hypot(ax - ux, ay - uy)
    a0, a1, am = (math.atan2(ay - uy, ax - ux), math.atan2(cy - uy, cx - ux), math.atan2(by - uy, bx - ux))
    # Sweep from a0 to a1 through am.
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    segments = _circle_segments(radius, sweep)
    return [(ux + radius * math.cos(a0 + sweep * i / segments), uy + radius * math.sin(a0 + sweep * i / segments))
            for i in range(segments + 1)]


def graphic_segments(graphic) -> list:
    """@brief The centre-line segments of a stroked graphic, mm.

    @param graphic FpLine, FpRect, FpPoly, FpCircle, FpArc, FpCurve, FpTextBox
        or FpDimension; anything else gives [].
    @return List of ((x0, y0), (x1, y1)) segments; curves are sampled (a
        Bezier into `bezier_segments` pieces). A text box gives the four
        sides of its border, or [] when the border is off (its text, like
        every text, is not checked). A dimension gives the strokes KiCad
        draws for its type (`FpDimension.ink_segments`) plus its sampled
        text frame circle (a leader's), never its text.
    """
    if isinstance(graphic, FpLine):
        return [(tuple(graphic.start), tuple(graphic.end))]
    if isinstance(graphic, FpRect):
        (x0, y0), (x1, y1) = graphic.start, graphic.end
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    if isinstance(graphic, FpPoly):
        corners = [tuple(p) for p in graphic.points]
        return [(corners[i], corners[(i + 1) % len(corners)]) for i in range(len(corners))]
    if isinstance(graphic, FpTextBox):
        if not graphic.border:
            return []
        (x0, y0), (x1, y1) = graphic.start, graphic.end
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    if isinstance(graphic, FpCurve):
        points = bezier_points(graphic.points, bezier_segments(graphic.points))
        return list(zip(points, points[1:]))
    if isinstance(graphic, FpDimension):
        # The ink of a dimension as KiCad draws it from the written style,
        # one geometry function per type shared with the writer.
        segments = list(graphic.ink_segments())
        circle = graphic.frame_circle()
        if circle is not None:
            (cx, cy), r = circle
            count = _circle_segments(r)
            points = [(cx + r * math.cos(2 * math.pi * i / count),
                       cy + r * math.sin(2 * math.pi * i / count)) for i in range(count + 1)]
            segments.extend(zip(points, points[1:]))
        return segments
    if isinstance(graphic, FpCircle):
        cx, cy = graphic.center
        r = float(graphic.radius)
        count = _circle_segments(r)
        points = [(cx + r * math.cos(2 * math.pi * i / count),
                   cy + r * math.sin(2 * math.pi * i / count)) for i in range(count + 1)]
        return list(zip(points, points[1:]))
    if isinstance(graphic, FpArc):
        points = _arc_points(tuple(graphic.start), tuple(graphic.mid), tuple(graphic.end))
        return list(zip(points, points[1:]))
    return []


def _inside_polygon(point, corners) -> bool:
    """Ray-casting test: True when `point` lies inside the (possibly concave) polygon."""
    x, y = point
    inside = False
    count = len(corners)
    for i in range(count):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % count]
        if (y0 > y) != (y1 > y):
            cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < cross:
                inside = not inside
    return inside


def filled_area_contains(graphic, point) -> bool:
    """@brief Whether a FILLED graphic's area covers `point`.

    @param graphic FpPoly, FpRect or FpCircle; anything else, or an outline
        (``fill`` False), gives False.
    @param point (x, y), mm.
    @return True when the point lies inside the filled area.
    """
    if not getattr(graphic, 'fill', False):
        return False
    if isinstance(graphic, FpPoly):
        return _inside_polygon(point, [tuple(p) for p in graphic.points])
    if isinstance(graphic, FpRect):
        (x0, y0), (x1, y1) = graphic.start, graphic.end
        return min(x0, x1) <= point[0] <= max(x0, x1) and min(y0, y1) <= point[1] <= max(y0, y1)
    if isinstance(graphic, FpCircle):
        return math.hypot(point[0] - graphic.center[0], point[1] - graphic.center[1]) <= float(graphic.radius)
    return False


def graphic_box(graphic) -> Optional[tuple]:
    """@brief Axis-aligned box of a stroked graphic's centre line, mm, or None for a text.

    @param graphic FpLine, FpRect, FpCircle or FpArc.
    @return (x0, y0, x1, y1), or None when the graphic has no segments.
    """
    segments = graphic_segments(graphic)
    if not segments:
        return None
    xs = [p[0] for seg in segments for p in seg]
    ys = [p[1] for seg in segments for p in seg]
    return (min(xs), min(ys), max(xs), max(ys))


# ── Rules ──────────────────────────────────────────────────────────────────

def check_pads_overlap(footprint: Footprint) -> list:
    """@brief Pads with different numbers whose copper overlaps or touches.

    @param footprint The footprint checked.
    @return One error per overlapping pair, in validation order.
    @note Pads sharing one non-empty number are one electrical pin and may
        overlap (a split thermal pad), as `PackageGeometry.validate` allows.
    """
    copper = [(index, pad) for index, pad in enumerate(footprint.pads) if has_copper(pad)]
    outlines = [(SimpleNamespace(number=str(pad.number)), pad_copper(pad)) for _index, pad in copper]
    findings = []
    for j, i in overlapping_pad_pairs(outlines):
        (ia, a), (ib, b) = copper[i], copper[j]
        if str(a.number) and str(a.number) == str(b.number):
            # One electrical pin in several pieces (a split thermal pad).
            continue
        findings.append(_finding('pads_overlap', 'error', tx(
            'Pad {a} e pad {b}: il rame si sovrappone.', a=a.number or '«»', b=b.number or '«»'),
            pads=(ia, ib)))
    return findings


def check_silk_clearance(footprint: Footprint, clearance: float = SILK_PAD_CLEARANCE_MM) -> list:
    """@brief Silkscreen ink closer than `clearance` to a pad's copper (KLC F5.1).

    @param footprint The footprint checked.
    @param clearance Minimum ink-to-copper distance, mm.
    @return One warning per (graphic, pad) pair below the clearance.
    @note The ink reaches half the stroke width beyond the centre line, so
        the distance measured is centre line to copper minus that half width.
        Texts are not checked: the Reference's place is the user's.
    """
    # A mechanical hole has no copper for the silk to clash with.
    coppers = [pad_copper(pad) if has_copper(pad) else None for pad in footprint.pads]
    boxes = [copper_box(pad) for pad in footprint.pads]
    findings = []
    for index, graphic in enumerate(footprint.graphics):
        if getattr(graphic, 'layer', None) != 'F.SilkS':
            continue
        segments = graphic_segments(graphic)
        if not segments:
            continue
        half_stroke = float(getattr(graphic, 'width', 0.0) or 0.0) / 2
        gx0, gy0, gx1, gy1 = graphic_box(graphic)
        reach = clearance + half_stroke + _EPS
        for pad_index, copper in enumerate(coppers):
            if copper is None:
                continue
            # Boxes farther apart than the reach cannot violate it: the exact
            # distance is measured only for the few graphics near each pad.
            px0, py0, px1, py1 = boxes[pad_index]
            if gx0 > px1 + reach or px0 > gx1 + reach or gy0 > py1 + reach or py0 > gy1 + reach:
                continue
            core, _radius = copper
            if any(filled_area_contains(graphic, point) for point in core):
                # A filled shape covers the copper whatever its outline keeps
                # clear (parity review 2026-11-06): the ink is the whole area.
                number = footprint.pads[pad_index].number or '«»'
                findings.append(_finding('silk_clearance', 'warning', tx(
                    'Serigrafia piena sopra il rame del pad {number} (KLC F5.1).', number=number),
                    pads=(pad_index,), graphics=(index,)))
                continue
            gap = min(segment_copper_distance(a, b, copper) for a, b in segments) - half_stroke
            if gap < clearance - _EPS:
                number = footprint.pads[pad_index].number or '«»'
                findings.append(_finding('silk_clearance', 'warning', tx(
                    'Serigrafia a {gap:.2f} mm dal rame del pad {number} (KLC F5.1: almeno {minimum:.2f} mm).',
                    gap=max(gap, 0.0), number=number, minimum=clearance),
                    pads=(pad_index,), graphics=(index,)))
    return findings


def _courtyard_box(footprint: Footprint) -> Optional[tuple]:
    """Union box of the F.CrtYd rectangles and lines, or None without a courtyard."""
    boxes = [graphic_box(g) for g in footprint.graphics if getattr(g, 'layer', None) == 'F.CrtYd']
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _box_inside(inner: tuple, outer: tuple, margin: float) -> bool:
    return (inner[0] - margin >= outer[0] - _EPS and inner[1] - margin >= outer[1] - _EPS
            and inner[2] + margin <= outer[2] + _EPS and inner[3] + margin <= outer[3] + _EPS)


def check_courtyard_enclosure(footprint: Footprint, margin: float = COURTYARD_MIN_CLEARANCE_MM) -> list:
    """@brief Pads and fabrication graphics the courtyard does not enclose with `margin` (KLC F5.3).

    @param footprint The footprint checked.
    @param margin Minimum distance, mm, between an element and the courtyard.
    @return At most two warnings: one naming the pads outside, one the
        F.Fab graphics outside (dimensions and text boxes excluded: they
        document the part). Nothing without a courtyard (the file-level
        check reports its absence).
    """
    courtyard = _courtyard_box(footprint)
    if courtyard is None:
        return []
    pads_out = [i for i, pad in enumerate(footprint.pads) if not _box_inside(copper_box(pad), courtyard, margin)]
    graphics_out = []
    for index, graphic in enumerate(footprint.graphics):
        if getattr(graphic, 'layer', None) != 'F.Fab' or isinstance(graphic, (FpDimension, FpTextBox)):
            # A dimension or a text box documents the part, it is not the
            # part: the courtyard need not enclose it. A text box is judged
            # as text whatever its border flag: the border frames a note, it
            # does not outline the body (the body outline is a line,
            # rectangle, polygon, circle or arc).
            continue
        box = graphic_box(graphic)
        if box is not None and not _box_inside(box, courtyard, margin):
            graphics_out.append(index)
    findings = []
    if pads_out:
        numbers = ', '.join(footprint.pads[i].number or '«»' for i in pads_out)
        findings.append(_finding('courtyard_enclosure', 'warning', tx(
            'Courtyard: non racchiude i pad {numbers} con {margin:.2f} mm di margine (KLC F5.3).',
            numbers=numbers, margin=margin), pads=pads_out))
    if graphics_out:
        findings.append(_finding('courtyard_enclosure', 'warning', tx(
            'Courtyard: {count} disegni di fabbricazione sporgono oltre il margine di {margin:.2f} mm (KLC F5.3).',
            count=len(graphics_out), margin=margin), graphics=graphics_out))
    return findings


def annular_ring(pad: Pad) -> Optional[float]:
    """@brief The thinnest copper ring around a plated pad's hole, mm; None for an unplated or holeless pad.

    @param pad A `core.footprint.Pad`.
    @return Per axis of the pad, half the pad side minus half the hole side
        minus the hole's offset from the copper centre along that axis (a
        castellated pad's `drill_offset`, pad-local like the hole); the
        smaller of the two. A round hole has equal hole sides.
    """
    if pad.pad_type != 'thru_hole':
        return None
    size_x, size_y = (float(v) for v in pad.size)
    if pad.drill_slot is not None:
        hole_x, hole_y = (float(v) for v in pad.drill_slot)
    elif pad.drill is not None:
        hole_x = hole_y = float(pad.drill)
    else:
        return None
    # `drill_offset` moves the COPPER away from the hole at `at`: the ring on
    # the far side grows, the one on the near side shrinks by the same amount.
    ox, oy = (abs(float(v)) for v in (getattr(pad, 'drill_offset', None) or (0.0, 0.0)))
    return min(size_x / 2 - ox - hole_x / 2, size_y / 2 - oy - hole_y / 2)


def check_annular_ring(footprint: Footprint, minimum: float = ANNULAR_RING_MIN_MM) -> list:
    """@brief Plated holes whose copper ring is thinner than `minimum`.

    @param footprint The footprint checked.
    @param minimum Thinnest acceptable ring, mm (the KiCad guide's advice).
    @return One warning per pad below the minimum.
    """
    findings = []
    for index, pad in enumerate(footprint.pads):
        ring = annular_ring(pad)
        if ring is not None and ring < minimum - _EPS:
            findings.append(_finding('annular_ring', 'warning', tx(
                'Pad {number}: anello di rame di {ring:.2f} mm attorno al foro (la guida KiCad consiglia '
                'almeno {minimum:.2f} mm).', number=pad.number or '«»', ring=max(ring, 0.0), minimum=minimum),
                pads=(index,)))
    return findings


def _point_copper_distance(point, copper: tuple) -> float:
    """Distance, mm, from a point to a pad's copper (0 inside)."""
    return segment_copper_distance(point, point, copper)


def _nearest_pad(point, coppers: Sequence) -> Optional[int]:
    """Index of the pad whose copper is nearest to `point`, None without pads."""
    if not coppers:
        return None
    return min(range(len(coppers)), key=lambda index: _point_copper_distance(point, coppers[index]))


def has_pin_one_marker(footprint: Footprint, first: Optional[str] = None) -> bool:
    """@brief Whether something marks pin 1 BY ITS POSITION: a dot beside it, its own pad shape or a bevel at it.

    @param footprint The footprint checked.
    @param first The pin-1 number (`core.silk.pin_one`); computed when None.
    @return True when (a) a circle on F.SilkS or F.Fab (filled or not: the
        user's drawn circles carry no fill) has its centre nearer to the
        pin-1 copper than to any other pad's copper, (b) the pin-1 pad has
        a shape no other electrical pad has (the square pad of a pin
        header), (c) a diagonal F.Fab line (an IC bevel) has its midpoint
        nearest to pin 1 and is shorter than half the span of the pads, so
        the slanted outline of a turned package does not count, or (d) a
        polygon (a drawn triangle or chamfer) on F.SilkS or F.Fab has its
        centroid nearest to pin 1 and spans less than half the pad span. A
        mark beside another pad marks nothing.
    @note (c) and (d) are HEURISTICS, declared: a bevel or a triangle is
        small next to the body and an outline is not; a larger mark would
        be missed, which is reported as a missing marker, never the reverse.
    """
    electrical = [(i, pad) for i, pad in enumerate(footprint.pads) if has_copper(pad)]
    if first is None:
        first = pin_one(pad.number for _i, pad in electrical)
    if first is None:
        return False
    coppers = [pad_copper(pad) for _i, pad in electrical]
    ones = {position for position, (_i, pad) in enumerate(electrical) if pad.number == first}
    # (b) its own shape
    own = {pad.shape for _i, pad in electrical if pad.number == first}
    others = {pad.shape for _i, pad in electrical if pad.number != first}
    if own and others and not (own & others):
        return True
    xs = [point[0] for core, _r in coppers for point in core]
    ys = [point[1] for core, _r in coppers for point in core]
    span = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0
    for graphic in footprint.graphics:
        layer = getattr(graphic, 'layer', None)
        if layer not in ('F.SilkS', 'F.Fab'):
            continue
        if isinstance(graphic, FpCircle):
            if _nearest_pad(tuple(graphic.center), coppers) in ones:
                return True
        elif isinstance(graphic, FpPoly):
            box = graphic_box(graphic)
            if box is None or math.hypot(box[2] - box[0], box[3] - box[1]) > span / 2:
                continue
            corners = [tuple(p) for p in graphic.points]
            centroid = (sum(p[0] for p in corners) / len(corners), sum(p[1] for p in corners) / len(corners))
            if _nearest_pad(centroid, coppers) in ones:
                return True
        elif isinstance(graphic, FpLine) and layer == 'F.Fab':
            dx, dy = graphic.end[0] - graphic.start[0], graphic.end[1] - graphic.start[1]
            if abs(dx) <= _EPS or abs(dy) <= _EPS or math.hypot(dx, dy) > span / 2:
                continue
            mid = ((graphic.start[0] + graphic.end[0]) / 2, (graphic.start[1] + graphic.end[1]) / 2)
            if _nearest_pad(mid, coppers) in ones:
                return True
    return False


def check_pin_one_marker(footprint: Footprint) -> list:
    """@brief A pin 1 without a marker beside it on the silkscreen or fabrication layer (KLC F5.1, F5.2).

    @param footprint The footprint checked.
    @return One warning naming the pin-1 pad, or nothing when there is no
        pin 1 (`core.silk.pin_one`) or a marker at it exists.
    """
    electrical = [(i, pad) for i, pad in enumerate(footprint.pads) if has_copper(pad)]
    first = pin_one(pad.number for _i, pad in electrical)
    if first is None or has_pin_one_marker(footprint, first):
        return []
    index = next(i for i, pad in electrical if pad.number == first)
    return [_finding('pin_one_marker', 'warning', tx(
        'Pin 1 (pad {number}) senza marcatore su serigrafia o fabbricazione (KLC F5.1, F5.2).',
        number=first), pads=(index,))]


def _stroke_width(graphic) -> Optional[float]:
    """The printed stroke width of a stroked graphic, mm, or None when it prints no line.

    Texts are not strokes here (their thickness is a font property); a text
    box without border prints no line; a FILLED shape prints its area, so
    its stroke width does not make a line too thin to print.
    """
    if not graphic_segments(graphic):
        return None
    if getattr(graphic, 'fill', False):
        return None
    width = getattr(graphic, 'width', None)
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        return None
    return float(width)


def check_silk_stroke_width(footprint: Footprint, minimum: float = SILK_MIN_WIDTH_MM) -> list:
    """@brief Silkscreen strokes thinner than `minimum` (KLC F5.1's lower bound, 0.10 mm).

    @param footprint The footprint checked.
    @param minimum Thinnest printable stroke, mm (`SILK_MIN_WIDTH_MM`).
    @return At most one warning naming every F.SilkS graphic whose stroke is
        below `minimum` (a stroke equal to it passes) and the thinnest width
        found; nothing when all are wide enough.
    @note Lines, rectangles, circles, arcs, polygons, curves, text box
        borders and dimensions are strokes; texts and filled shapes are not
        (see `_stroke_width`).
    """
    thin = []
    for index, graphic in enumerate(footprint.graphics):
        if getattr(graphic, 'layer', None) != 'F.SilkS':
            continue
        width = _stroke_width(graphic)
        if width is not None and width < minimum - _EPS:
            thin.append((index, width))
    if not thin:
        return []
    return [_finding('silk_stroke_width', 'warning', tx(
        'Serigrafia: {count} tratti più sottili di {minimum:.2f} mm (il più sottile è di {width:.2f} mm): '
        'sotto il minimo della KLC F5.1, rischiano di non essere stampati.', count=len(thin), minimum=minimum,
        width=min(width for _index, width in thin)), graphics=[index for index, _width in thin])]


def check_footprint_geometry(footprint: Footprint) -> list:
    """@brief Run every geometric rule on a footprint: errors first, then warnings.

    @param footprint The footprint checked (generated graphics and the user's
        drawings already added).
    @return `GeometryFinding` list, errors before warnings, rule order kept
        within each severity.
    """
    findings = []
    for check in (check_pads_overlap, check_silk_clearance, check_courtyard_enclosure,
                  check_annular_ring, check_pin_one_marker, check_silk_stroke_width):
        findings.extend(check(footprint))
    return sorted(findings, key=lambda f: 0 if f.severity == 'error' else 1)
