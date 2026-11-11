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
  `SILK_PAD_CLEARANCE_MM` to a pad's copper ON THE SAME SIDE: F.SilkS
  against the front copper, B.SilkS against the back copper
  (`pad_copper_sides`); the two print on opposite faces of the board.
- courtyard_enclosure (warning, KLC F5.3): a pad or an F.Fab graphic that
  the F.CrtYd courtyard does not enclose with the minimum clearance.
  Keepout zones are neither ink nor body: no silk or courtyard rule reads them.
  B.Fab graphics are NOT held to the F.CrtYd courtyard, a declared limit:
  KLC F5.3 asks a component that needs room on the back of the board for
  its own courtyard on B.CrtYd («If the component requires a courtyard on
  the back of the PCB, a corresponding courtyard must be provided on the
  B.CrtYd layer», content/footprint/F5/F5.3.adoc of gitlab.com/kicad/
  libraries/klc, read 2026-11-09), and this program draws no B.CrtYd (user
  decision): the front courtyard is the wrong outline to measure a back
  body against, and no check of the back body exists.
- annular_ring (warning): a plated hole whose copper ring is thinner than
  `MIN_ANNULAR_RING_MM` (the KiCad getting-started guide's advice, not a KLC
  rule: said so in the message). A pad whose ring differs on its two axes is
  reported with both values and the axis of each.
- hole_vs_lead (error): a pad whose hole does not let its DECLARED terminal
  pass (`core.tht_estimates.lead_passes`): a structural impossibility, not a
  threshold. Only pads carrying a lead (`Pad.lead`) are judged.
- estimated_pad (warning): the pads whose hole, size or shape is derived or
  estimated by the program (`Pad.sources`), with which of the three: to be
  verified on the drawing or the manufacturer's land pattern. A pad without
  provenance (a project saved before it existed) is never listed.
- pin_one_marker (warning, KLC F5.1/F5.2): the footprint has a pin 1 but no
  marker of it on the silkscreen or fabrication layer.
- silk_stroke_width (warning, KLC F5.1): a silkscreen stroke, on either
  side, thinner than `SILK_MIN_WIDTH_MM`, the low end of the KLC's allowed
  range. ONLY the lower bound is checked (see `SILK_MIN_WIDTH_MM`).
- silk_text_size (warning, not a KLC rule): a text, text box or dimension
  on F.SilkS or B.SilkS lower than `SILK_TEXT_MIN_HEIGHT_MM` or drawn with a
  pen thinner than `SILK_TEXT_MIN_THICKNESS_MM`: KiCad's DRC board defaults.
- pad_in_keepout (warning, not a KLC rule): a pad whose copper meets a
  keepout zone of the footprint that forbids pads on a copper layer the pad
  has, by at least KiCad's DRC epsilon (`KEEPOUT_DRC_EPSILON_MM`, a length).
  KiCad's DRC flags it on the board: its disallow constraint exempts
  only the footprint ITSELF from its own keepout, not the footprint's pads
  (pcbnew/drc/drc_engine.cpp, branch 10.0: «A footprint's own keepout never
  applies to that footprint», tested as item == parent footprint).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
from types import SimpleNamespace
from typing import Iterable, Optional, Sequence

from ..core.footprint import (DIMENSION_TEXT_THICKNESS_MM, FpArc, FpCircle, FpCurve, FpDimension, FpLine,
                              FpPoly, FpRect, FpText, FpTextBox, FpZone, Footprint, Pad, bezier_points,
                              pad_copper_centre)
from ..core.kicad_text import KICAD_PEN_CLAMP_RATIO
from ..core.package_geometry import _rotate_kicad, overlapping_pad_pairs, pad_copper_shape
from ..core.silk import SILK_PAD_CLEARANCE_MM, pin_one
from ..core.tht_estimates import MIN_ANNULAR_RING_MM, lead_passes, unverified_values
from .klc_checks import Finding
from .messages import tx

#: Minimum clearance, mm, between the courtyard and the component it
#: encloses: the generic value of KLC F5.3 (0.25 mm). Connectors take 0.5 mm
#: there, but the checker cannot tell a connector from its geometry, so it
#: holds every footprint to the generic minimum only.
COURTYARD_MIN_CLEARANCE_MM = 0.25

#: Minimum copper ring around a plated hole, mm: the ONE constant
#: `core.tht_estimates.MIN_ANNULAR_RING_MM`, shared with the extraction; its
#: source (the KiCad getting-started guide's advice, not a KLC rule) is
#: stated there, once. The old name is kept for its callers.
ANNULAR_RING_MIN_MM = MIN_ANNULAR_RING_MM

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
    'hole_vs_lead': ('Nel dialog del pad allarga il foro (e il pad con lui) finché supera il terminale: per '
                     'un terminale piatto il foro tondo deve superare la sua diagonale, oppure usa '
                     "un'asola più grande del terminale su entrambi gli assi. Se è il terminale a essere "
                     'stato letto male, nessun dialog lo modifica: lo rilegge solo una nuova analisi dei '
                     'documenti (la rilettura delle posizioni dei pad riparte dalla stessa risposta e non '
                     'lo cambia).'),
    'estimated_pad': ('Confronta questi pad con il disegno quotato o con il land pattern del costruttore: nel '
                      'dialog del pad scrivi i valori letti se sono diversi, oppure spunta «Valori '
                      'verificati sul disegno» se sono giusti. In entrambi i casi il pad non è più '
                      'segnalato come stimato.'),
    'pin_one_marker': ('Disegna un cerchio sulla serigrafia accanto al pad del pin 1 (strumento cerchio '
                       'della palette, più vicino a lui che a ogni altro pad) oppure dai al pin 1 una forma '
                       'di pad diversa dagli altri: chi monta la scheda deve riconoscerlo.'),
    'silk_stroke_width': ('Apri le proprietà del disegno (tasto destro sul disegno) e porta lo spessore ad '
                          'almeno 0.10 mm, meglio 0.12 mm: la KLC F5.1 ammette da 0.10 a 0.15 mm, 0.12 mm '
                          'è il valore nominale.'),
    'pad_in_keepout': ('Sposta il pad fuori dalla zona di esclusione, ridisegna la zona attorno al pad oppure '
                       'togli il divieto «pad» dalle sue proprietà.'),
    'silk_text_size': ("Apri le proprietà del testo (tasto destro sul testo) e porta l'altezza ad almeno "
                       '0.80 mm, oppure spostalo sul layer di fabbricazione: il DRC di KiCad, con i valori '
                       'predefiniti della scheda, segnala i testi di serigrafia più bassi di 0.80 mm o con '
                       'tratto sotto 0.08 mm.'),
}

#: The silkscreen layers and the outer copper layer each one prints over.
SILK_COPPER_SIDE = {'F.SilkS': 'F.Cu', 'B.SilkS': 'B.Cu'}


def has_copper(pad: Pad) -> bool:
    """@brief Whether a pad carries copper the copper rules apply to.

    @param pad A `core.footprint.Pad`.
    @return False for a non-plated mechanical hole (its "pad" is a mask
        opening, as the generators write it), True otherwise.
    """
    return pad.pad_type != 'np_thru_hole'


def pad_copper_sides(pad: Pad) -> frozenset:
    """@brief The outer copper layers («F.Cu», «B.Cu») a pad has copper on.

    @param pad A `core.footprint.Pad`.
    @return A plated through-hole pad («thru_hole») always gives both: its
        barrel ends in copper on both faces. Any other pad (SMD, connector,
        mechanical hole) gives the outer copper layers its KiCad layer list
        names: «*.Cu» (or the legacy «F&B.Cu») both, «F.Cu» / «B.Cu» their
        own side; a pad naming no outer copper layer (a paste or mask
        aperture, a mechanical hole written with only «*.Mask», a pad on
        inner layers only) gives none: an aperture is not copper.
    @note Checked against KiCad's own DRC (`kicad-cli pcb drc`, KiCad
        10.0.8, 2026-11-09, test in tests/test_footprint_checks_back_zone.py):
        a «thru_hole» pad written with «F.Cu» only IS reported in a B.Cu
        keepout (KiCad gives a PTH every copper layer), while an
        «np_thru_hole» follows its layer list exactly like an SMD pad: with
        «*.Cu» it is reported, with «F.Cu» only it is not reported in a B.Cu
        keepout, with «*.Mask» only it is reported in none; a mask-only or
        paste+mask SMD aperture is reported in none.
    """
    layers = {str(name) for name in pad.layers}
    if pad.pad_type == 'thru_hole' or layers & {'*.Cu', 'F&B.Cu'}:
        return frozenset(('F.Cu', 'B.Cu'))
    return frozenset(layers & {'F.Cu', 'B.Cu'})


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
    """@brief Silkscreen ink closer than `clearance` to a pad's copper on the same side (KLC F5.1).

    @param footprint The footprint checked.
    @param clearance Minimum ink-to-copper distance, mm.
    @return One warning per (graphic, pad) pair below the clearance.
    @note The ink reaches half the stroke width beyond the centre line, so
        the distance measured is centre line to copper minus that half width.
        Texts are not checked: the Reference's place is the user's.
    @note F.SilkS ink is measured against the pads with front copper and
        B.SilkS ink against the pads with back copper (`SILK_COPPER_SIDE`,
        `pad_copper_sides`): ink printed on one face cannot cover copper on
        the other, so a back-only pad under front silk is not a finding.
    """
    # A mechanical hole has no copper for the silk to clash with.
    coppers = [pad_copper(pad) if has_copper(pad) else None for pad in footprint.pads]
    sides = [pad_copper_sides(pad) for pad in footprint.pads]
    boxes = [copper_box(pad) for pad in footprint.pads]
    findings = []
    for index, graphic in enumerate(footprint.graphics):
        copper_side = SILK_COPPER_SIDE.get(getattr(graphic, 'layer', None))
        if copper_side is None:
            continue
        segments = graphic_segments(graphic)
        if not segments:
            continue
        half_stroke = float(getattr(graphic, 'width', 0.0) or 0.0) / 2
        gx0, gy0, gx1, gy1 = graphic_box(graphic)
        reach = clearance + half_stroke + _EPS
        for pad_index, copper in enumerate(coppers):
            if copper is None or copper_side not in sides[pad_index]:
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
    @note B.Fab graphics are not measured: KLC F5.3 wants a back body
        enclosed by a B.CrtYd courtyard, which this program does not draw
        (see the module docstring); holding it to F.CrtYd would test the
        wrong outline.
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
    @return The smaller of the two values of `annular_rings`.
    """
    rings = annular_rings(pad)
    return None if rings is None else min(rings)


def annular_rings(pad: Pad) -> Optional[tuple]:
    """@brief The copper ring around a plated pad's hole on each pad axis, mm; None for an unplated or holeless pad.

    @param pad A `core.footprint.Pad`.
    @return (ring along the pad's length, ring along its width): the pad's
        own axes, `size[0]` and `size[1]`, which turn with its rotation. Per
        axis, half the pad side minus half the hole side minus the hole's
        offset from the copper centre along that axis (a castellated pad's
        `drill_offset`, pad-local like the hole). A round hole has equal
        hole sides, so the two rings differ exactly when the pad's sides do
        (an oval 2.1 x 1.7 mm pad with a 1.3 mm hole: 0.4 and 0.2 mm).
    @note The ring is measured along the axes, where a rectangle, an oval
        and a circle are thinnest; a rounded corner is never nearer the hole.
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
    return (size_x / 2 - ox - hole_x / 2, size_y / 2 - oy - hole_y / 2)


def check_annular_ring(footprint: Footprint, minimum: float = ANNULAR_RING_MIN_MM) -> list:
    """@brief Plated holes whose copper ring is thinner than `minimum`.

    @param footprint The footprint checked.
    @param minimum Thinnest acceptable ring, mm (the KiCad guide's advice,
        `core.tht_estimates.MIN_ANNULAR_RING_MM`).
    @return One warning per pad below the minimum. When the ring is the same
        on both pad axes (a round pad and hole) the message gives that one
        value; otherwise it gives BOTH rings, each with the pad axis it lies
        along («lunghezza» is `size[0]`, «larghezza» is `size[1]`) and that
        pad side, thinnest first: the user sees which side of the pad to widen.
    """
    findings = []
    for index, pad in enumerate(footprint.pads):
        rings = annular_rings(pad)
        if rings is None or min(rings) >= minimum - _EPS:
            continue
        along_length, along_width = (max(ring, 0.0) for ring in rings)
        length, width = (float(v) for v in pad.size)
        number = pad.number or '«»'
        if abs(rings[0] - rings[1]) <= _EPS:
            message = tx(
                'Pad {number}: anello di rame di {ring:.2f} mm attorno al foro (la guida KiCad consiglia '
                'almeno {minimum:.2f} mm).', number=number, ring=along_length, minimum=minimum)
        elif rings[1] < rings[0]:
            message = tx(
                'Pad {number}: anello di rame di {ring:.2f} mm lungo la larghezza del pad ({side:.2f} mm) e di '
                '{other:.2f} mm lungo la lunghezza ({other_side:.2f} mm) (la guida KiCad consiglia almeno '
                '{minimum:.2f} mm).', number=number, ring=along_width, side=width, other=along_length,
                other_side=length, minimum=minimum)
        else:
            message = tx(
                'Pad {number}: anello di rame di {ring:.2f} mm lungo la lunghezza del pad ({side:.2f} mm) e di '
                '{other:.2f} mm lungo la larghezza ({other_side:.2f} mm) (la guida KiCad consiglia almeno '
                '{minimum:.2f} mm).', number=number, ring=along_length, side=length, other=along_width,
                other_side=width, minimum=minimum)
        findings.append(_finding('annular_ring', 'warning', message, pads=(index,)))
    return findings


def _hole_sides(pad: Pad) -> Optional[tuple]:
    """The hole of a pad as (side along the pad length, side along the pad width), mm; None without a hole."""
    if pad.drill_slot is not None:
        return tuple(float(v) for v in pad.drill_slot)
    if pad.drill is not None:
        return (float(pad.drill), float(pad.drill))
    return None


def _lead_text(lead: dict) -> str:
    """A declared lead section as the user reads it on a drawing: «Ø0.6 mm» or «1 x 0.4 mm»."""
    if lead.get('shape') == 'round':
        return 'Ø{:g} mm'.format(lead['diameter_mm'])
    return '{:g} x {:g} mm'.format(lead['width_mm'], lead['thickness_mm'])


def check_hole_vs_lead(footprint: Footprint) -> list:
    """@brief Pads whose hole does not let their declared terminal pass.

    @param footprint The footprint checked.
    @return One error per holed pad (plated or not) that carries a lead
        (`Pad.lead`) and whose hole fails `core.tht_estimates.lead_passes`:
        a round hole not larger than the lead's circumscribed diameter (the
        diagonal of a flat blade), or a slot the lead does not fit in. A pad
        without a lead, or without a hole, is never judged: nothing says
        what should pass through it.
    @note An error, not a warning: no threshold is involved, the part
        cannot be inserted. The lead itself may be the wrong reading; the
        remedy says so. A lead out of contract (a hand-built `Pad`) is
        skipped: validation belongs to `PinSpec.validate`.
    """
    findings = []
    for index, pad in enumerate(footprint.pads):
        lead = getattr(pad, 'lead', None)
        hole = _hole_sides(pad)
        if not lead or hole is None:
            continue
        try:
            if lead_passes(lead, hole[0], hole[1]):
                continue
        except ValueError:
            continue
        if hole[0] == hole[1]:
            hole_text = 'Ø{:g} mm'.format(hole[0])
        else:
            hole_text = '{:g} x {:g} mm'.format(*hole)
        findings.append(_finding('hole_vs_lead', 'error', tx(
            'Pad {number}: il terminale dichiarato ({lead}) non passa nel foro ({hole}).',
            number=pad.number or '«»', lead=_lead_text(lead), hole=hole_text), pads=(index,)))
    return findings


def check_estimated_pads(footprint: Footprint) -> list:
    """@brief The pads whose hole, size or shape is derived or estimated, not read.

    @param footprint The footprint checked.
    @return At most one warning selecting every pad whose `Pad.sources`
        marks "drill_mm", "pad_size" or "shape" as "derived" or "estimated"
        (`core.tht_estimates.unverified_values`), naming for each pad which
        of the three. Nothing when no pad carries such a mark: a pad
        without provenance is not an estimate the program knows of.
    @note A warning: the values may well be right, but nobody read them;
        they are checked against the drawing or the manufacturer's land
        pattern before the footprint is trusted.
    """
    # Visible names of the three values, in the order of `unverified_values`.
    names = {'drill_mm': tx('foro'), 'pad_size': tx('dimensione'), 'shape': tx('forma')}
    marked = []
    for index, pad in enumerate(footprint.pads):
        try:
            values = unverified_values(getattr(pad, 'sources', None))
        except AttributeError:
            # Not a mapping (a hand-built pad out of contract): states nothing.
            values = ()
        if values:
            marked.append((index, '{} ({})'.format(pad.number or '«»', ', '.join(names[key] for key in values))))
    if not marked:
        return []
    return [_finding('estimated_pad', 'warning', tx(
        'Pad con valori stimati o derivati dal programma, non letti dal disegno: {pads}. Da verificare sul '
        'disegno quotato o sul land pattern del costruttore.', pads='; '.join(text for _index, text in marked)),
        pads=[index for index, _text in marked])]


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
    @return At most one warning naming every F.SilkS or B.SilkS graphic
        whose stroke is below `minimum` (a stroke equal to it passes) and the
        thinnest width found; nothing when all are wide enough. The KLC
        range is stated for the silkscreen, whatever its side.
    @note Lines, rectangles, circles, arcs, polygons, curves, text box
        borders and dimensions are strokes; texts and filled shapes are not
        (see `_stroke_width`).
    """
    thin = []
    for index, graphic in enumerate(footprint.graphics):
        if getattr(graphic, 'layer', None) not in SILK_COPPER_SIDE:
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


#: Smallest silkscreen text height and stroke, mm, KiCad's DRC accepts on a
#: new board: BOARD_DESIGN_SETTINGS sets m_MinSilkTextHeight to
#: DEFAULT_SILK_TEXT_SIZE x 0.8 = 1.0 x 0.8 and m_MinSilkTextThickness to
#: DEFAULT_SILK_TEXT_WIDTH x 0.8 = 0.1 x 0.8 (pcbnew/board_design_settings.cpp
#: and include/board_design_settings.h, branch 10.0, read 2026-11-09); the
#: DRC applies them to texts on F.Silkscreen and B.Silkscreen
#: («board setup constraints silk text height / thickness»,
#: pcbnew/drc/drc_engine.cpp). These are KiCad's board DEFAULTS, not a KLC
#: rule: a board may set others in its «Constraints» page. Checked with
#: kicad-cli 10.0.8 `pcb drc` on 2026-11-09 (test in
#: tests/test_footprint_checks_back_zone.py).
SILK_TEXT_MIN_HEIGHT_MM = 0.8
SILK_TEXT_MIN_THICKNESS_MM = 0.08


def _silk_text(graphic) -> Optional[tuple]:
    """(height, pen) in mm of a text KiCad's text-size DRC measures, or None for anything else.

    Texts, text boxes and dimensions carry a text (pcbnew/drc/
    drc_test_provider_text_dims.cpp tests PCB_TEXT_T, PCB_TEXTBOX_T and
    PCB_DIMENSION_T); a centre mark writes none. The pen is the one KiCad
    draws (EDA_TEXT::GetEffectiveTextPenWidth): the written thickness clamped
    to `KICAD_PEN_CLAMP_RATIO` x the size (ClampTextPenSize, common/gr_text.cpp).
    """
    if isinstance(graphic, FpDimension):
        if graphic.dtype == 'center':
            return None
        size, thickness = graphic.text_size, DIMENSION_TEXT_THICKNESS_MM
    elif isinstance(graphic, (FpText, FpTextBox)):
        size, thickness = graphic.size, graphic.thickness
    else:
        return None
    size, thickness = float(size), float(thickness)
    return size, min(thickness, KICAD_PEN_CLAMP_RATIO * size)


def check_silk_text_size(footprint: Footprint, min_height: float = SILK_TEXT_MIN_HEIGHT_MM,
                         min_thickness: float = SILK_TEXT_MIN_THICKNESS_MM) -> list:
    """@brief Silkscreen texts lower or thinner than KiCad's DRC board defaults (not a KLC rule).

    @param footprint The footprint checked.
    @param min_height Lowest text height, mm (`SILK_TEXT_MIN_HEIGHT_MM`).
    @param min_thickness Thinnest drawn text stroke, mm (`SILK_TEXT_MIN_THICKNESS_MM`).
    @return At most one warning naming every F.SilkS or B.SilkS text, text
        box or dimension whose height is below `min_height` or whose drawn
        pen is below `min_thickness` (a value equal to the minimum passes,
        as in KiCad); nothing when all pass.
    @note The Reference and Value properties are not graphics and keep
        KiCad's default size (`PROPERTY_TEXT_SIZE`, `PROPERTY_TEXT_THICKNESS`),
        above both minimums. A text on the fabrication or user layers is not
        printed and not checked, as in KiCad.
    """
    small = []
    for index, graphic in enumerate(footprint.graphics):
        if getattr(graphic, 'layer', None) not in SILK_COPPER_SIDE:
            continue
        measured = _silk_text(graphic)
        if measured is None:
            continue
        height, pen = measured
        if height < min_height - _EPS or pen < min_thickness - _EPS:
            small.append((index, height, pen))
    if not small:
        return []
    return [_finding('silk_text_size', 'warning', tx(
        'Serigrafia: {count} testi sotto i minimi predefiniti del DRC di KiCad (altezza {height:.2f} mm, '
        'tratto {thickness:.2f} mm; il testo più basso è alto {smallest:.2f} mm, il tratto più sottile è di '
        '{pen:.3f} mm): '
        'KiCad li segnala sulla scheda (non è una regola KLC).', count=len(small), height=min_height,
        thickness=min_thickness, smallest=min(height for _i, height, _p in small),
        pen=min(pen for _i, _h, pen in small)), graphics=[index for index, _h, _p in small])]


#: KiCad's DRC epsilon, mm: ADVANCED_CFG::m_DRCEpsilon = 0.0005 («0.5um is
#: small enough not to materially violate any constraints»,
#: common/advanced_config.cpp, branch 10.0, read 2026-11-09), returned by
#: BOARD_DESIGN_SETTINGS::GetDRCEpsilon(). A LENGTH: before testing a keepout
#: KiCad deflates the rule area by it (pcbnew/pcbexpr_functions.cpp,
#: getDeflatedZoneOutline: «Collisions include touching, so we need to
#: deflate outline by enough to exclude it») and then reports any pad whose
#: shape meets the deflated outline, touching included (collidesWithArea).
#: Measured with kicad-cli 10.0.8 `pcb drc` (2026-11-09): rectangular,
#: round and oval pads that cross the border by 0.1-0.4 um are not reported,
#: by 0.5 um or more they are (exactly 0.5 um sits on KiCad's 1 nm rounding
#: and went either way for different shapes).
KEEPOUT_DRC_EPSILON_MM = 0.0005

#: KiCad's internal unit, mm (1 nm, coordinates are 32-bit nanometres): the
#: membership tests of `copper_meets_keepout` accept half of it, the finest
#: difference a written file can express.
_KICAD_UNIT_MM = 1e-6


def _convex_hull(points) -> list:
    """Convex hull of `points`, counter-clockwise in (x, y) maths axes (Andrew's monotone chain)."""
    unique = sorted(set((float(x), float(y)) for x, y in points))
    if len(unique) < 3:
        return unique
    lower, upper = [], []
    for point in unique:
        while len(lower) >= 2 and _orientation(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(unique):
        while len(upper) >= 2 and _orientation(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _point_hull_distance(point, hull) -> float:
    """Distance, mm, from `point` to a convex hull given as 1, 2 or 3+ ordered points (0 inside)."""
    if len(hull) == 1:
        return math.hypot(point[0] - hull[0][0], point[1] - hull[0][1])
    if len(hull) == 2:
        return _point_segment_distance(point, hull[0], hull[1])
    if _inside_convex(point, hull):
        return 0.0
    return min(_point_segment_distance(point, hull[i], hull[(i + 1) % len(hull)]) for i in range(len(hull)))


def _offset_lines(a, b, distance: float) -> list:
    """The two lines parallel to a-b at `distance` on either side, as (point, direction); [] for a point."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return []
    nx, ny = -dy / length * distance, dx / length * distance
    return [((a[0] + nx, a[1] + ny), (dx, dy)), ((a[0] - nx, a[1] - ny), (dx, dy))]


def _line_line(first, second) -> list:
    """Crossing point of two lines (point, direction); [] when parallel."""
    (px, py), (dx, dy) = first
    (qx, qy), (ex, ey) = second
    det = dx * ey - dy * ex
    if det == 0.0:
        return []
    t = ((qx - px) * ey - (qy - py) * ex) / det
    return [(px + t * dx, py + t * dy)]


def _line_circle(line, circle) -> list:
    """Points where a line (point, direction) meets a circle (centre, radius)."""
    (px, py), (dx, dy) = line
    (cx, cy), radius = circle
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return []
    t0 = ((cx - px) * dx + (cy - py) * dy) / length2
    fx, fy = px + t0 * dx, py + t0 * dy
    gap2 = (fx - cx) ** 2 + (fy - cy) ** 2
    if gap2 > radius * radius:
        return []
    half = math.sqrt(max(0.0, radius * radius - gap2) / length2)
    return [(fx + half * dx, fy + half * dy), (fx - half * dx, fy - half * dy)]


def _circle_circle(first, second) -> list:
    """Points where two circles (centre, radius) meet; [] for concentric or separate circles."""
    (ax, ay), ra = first
    (bx, by), rb = second
    dx, dy = bx - ax, by - ay
    gap = math.hypot(dx, dy)
    if gap <= 0.0 or gap > ra + rb or gap < abs(ra - rb):
        return []
    along = (ra * ra - rb * rb + gap * gap) / (2 * gap)
    height = math.sqrt(max(0.0, ra * ra - along * along))
    mx, my = ax + along * dx / gap, ay + along * dy / gap
    return [(mx - height * dy / gap, my + height * dx / gap), (mx + height * dy / gap, my - height * dx / gap)]


def _meet(first, second) -> list:
    """Crossing points of two boundary carriers, each ('line', (point, direction)) or ('circle', (centre, r))."""
    (kind_a, a), (kind_b, b) = first, second
    if kind_a == 'line' and kind_b == 'line':
        return _line_line(a, b)
    if kind_a == 'line':
        return _line_circle(a, b)
    if kind_b == 'line':
        return _line_circle(b, a)
    return _circle_circle(a, b)


def _zone_carriers(corners, epsilon: float) -> list:
    """The lines and circles the boundary of the zone deflated by `epsilon` lies on (a superset)."""
    count = len(corners)
    carriers = []
    for i in range(count):
        carriers.extend(('line', line) for line in _offset_lines(corners[i], corners[(i + 1) % count], epsilon))
        carriers.append(('circle', (corners[i], epsilon)))
    return carriers


def _copper_carriers(hull, radius: float) -> list:
    """The lines and circles the boundary of a pad's copper (convex `hull` grown by `radius`) lies on."""
    count = len(hull)
    sides = [(hull[i], hull[(i + 1) % count]) for i in range(count if count > 2 else count - 1)]
    carriers = []
    for a, b in sides:
        if radius > 0.0:
            carriers.extend(('line', line) for line in _offset_lines(a, b, radius))
        else:
            carriers.append(('line', (a, (b[0] - a[0], b[1] - a[1]))))
    if radius > 0.0:
        carriers.extend(('circle', (point, radius)) for point in hull)
    return carriers


class KeepoutOutline:
    """@brief A keepout zone's outline prepared ONCE for every pad `copper_meets_keepout` tests against it.

    Everything that depends on the zone alone is computed here and shared:
    the corners, sides and bounding box at once; the boundary carriers of
    the deflated zone and the crossings of those carriers with each other
    lazily, on the first pad that needs them, sorted by X so each pad reads
    only the crossings inside its own copper box (`crossings_in`). The
    carrier crossings are quadratic in the number of corners, so computing
    them per pad made the rule quadratic per pad (0.31 s for two pads on a
    256-corner zone, verification review 2026-11-09); the depth test of a
    crossing is left to the pads, since one is needed only near a pad.
    @param corners The zone's corners in order, mm (closed implicitly).
    @param epsilon How far, mm, the zone is deflated (`KEEPOUT_DRC_EPSILON_MM`).
    @note The answers are those of the unprepared computation: each
        candidate point is tested by the same predicates, only the tests
        that do not depend on the pad are done once.
    """

    def __init__(self, corners: Sequence, epsilon: float = KEEPOUT_DRC_EPSILON_MM):
        """@brief Store the corners, sides and bounding box; the carriers wait for the first pad.

        @param corners The zone's corners in order, mm.
        @param epsilon Deflation, mm.
        """
        self.zone = [(float(x), float(y)) for x, y in corners]
        self.epsilon = float(epsilon)
        self.sides = [(self.zone[i], self.zone[(i + 1) % len(self.zone)]) for i in range(len(self.zone))]
        self.box = ((min(p[0] for p in self.zone), min(p[1] for p in self.zone),
                     max(p[0] for p in self.zone), max(p[1] for p in self.zone)) if self.zone else None)
        self._carriers = None
        self._crossings = None
        self._crossing_xs = None

    def deep(self, point) -> bool:
        """@brief Whether `point` lies inside the zone at least epsilon from every side (half a KiCad unit tolerance).

        @param point (x, y) mm.
        @return True for a point of the deflated zone.
        """
        if not _inside_polygon(point, self.zone):
            return False
        tolerance = _KICAD_UNIT_MM / 2
        return min(_point_segment_distance(point, a, b) for a, b in self.sides) >= self.epsilon - tolerance

    @property
    def carriers(self) -> list:
        """@brief The lines and circles the deflated boundary lies on (`_zone_carriers`), computed once.

        @return The carrier list.
        """
        if self._carriers is None:
            self._carriers = _zone_carriers(self.zone, self.epsilon)
        return self._carriers

    def crossings_in(self, box) -> list:
        """@brief Crossings of the zone's carriers with each other inside a box (built once, then sliced).

        @param box (x0, y0, x1, y1) mm, borders included.
        @return The crossing points (x, y) mm in the box; their depth in the
            zone is NOT tested here (the caller tests it after the cheaper
            copper test).
        """
        if self._crossings is None:
            carriers = self.carriers
            points = []
            for i, first in enumerate(carriers):
                for second in carriers[i + 1:]:
                    points.extend(_meet(first, second))
            points.sort()
            self._crossings = points
            self._crossing_xs = [point[0] for point in points]
        x0, y0, x1, y1 = box
        low, high = bisect_left(self._crossing_xs, x0), bisect_right(self._crossing_xs, x1)
        return [point for point in self._crossings[low:high] if y0 <= point[1] <= y1]


def copper_meets_keepout(copper: tuple, corners, epsilon: float = KEEPOUT_DRC_EPSILON_MM) -> bool:
    """@brief Whether a pad's copper meets a keepout polygon deflated by `epsilon`, as KiCad's DRC tests it.

    @param copper (core, radius) as `pad_copper` gives it: the convex hull of
        the core grown by the radius, EXACTLY (no sampled outline, so an
        overlap thinner than any sampling step is not lost).
    @param corners The zone's corners in order, mm (closed implicitly), or a
        `KeepoutOutline` prepared once for many pads (used as it is when its
        epsilon is `epsilon`, re-prepared otherwise); the interior is the
        even-odd one of the outline.
    @param epsilon How far, mm, the zone is deflated first
        (`KEEPOUT_DRC_EPSILON_MM`): copper that only touches the border, or
        crosses it by less than epsilon, does not meet it.
    @return True when some point of the copper lies inside the zone at least
        `epsilon` from every side (KiCad: the deflated outline collides with
        the pad's effective shape, touching included), within half a KiCad
        unit (`_KICAD_UNIT_MM`).
    @note Exact by construction: if the set S of such points is not empty,
        its rightmost point is a corner of S (two boundary carriers crossing:
        sides of the copper, sides of the zone moved in by epsilon, circles
        of radius epsilon round the zone's corners) or the rightmost point
        of one of the copper's convex arcs; every candidate of those kinds
        is tested, so no S is missed. The deflation is the round one; KiCad
        joins the deflated sides with mitres (CORNER_STRATEGY::
        ALLOW_ACUTE_CORNERS, mitre limit 10), which excludes slightly more
        near a CONCAVE corner of the zone: there, copper crossing by less
        than about 10 x epsilon (5 um) may be reported here and not by
        KiCad. Declared residual divergence.
    """
    if isinstance(corners, KeepoutOutline):
        outline = corners if corners.epsilon == float(epsilon) else KeepoutOutline(corners.zone, epsilon)
    else:
        outline = KeepoutOutline(corners, epsilon)
    core, radius = copper
    radius = float(radius)
    zone = outline.zone
    hull = _convex_hull(core)
    if len(zone) < 3 or not hull:
        return False
    tolerance = _KICAD_UNIT_MM / 2

    def in_copper(point) -> bool:
        return _point_hull_distance(point, hull) <= radius + tolerance

    # Fast answers first: the copper's first core point deep in the zone,
    # or copper and zone entirely apart (no side within reach and neither
    # one holding a point of the other).
    if outline.deep(hull[0]) and in_copper(hull[0]):
        return True
    copper_box = (min(p[0] for p in hull) - radius, min(p[1] for p in hull) - radius,
                  max(p[0] for p in hull) + radius, max(p[1] for p in hull) + radius)
    zone_box = outline.box
    if (copper_box[0] > zone_box[2] or zone_box[0] > copper_box[2]
            or copper_box[1] > zone_box[3] or zone_box[1] > copper_box[3]):
        return False
    probe = (hull, radius)
    if (all(segment_copper_distance(a, b, probe) > tolerance for a, b in outline.sides)
            and not _inside_polygon(hull[0], zone) and not in_copper(zone[0])):
        return False
    # Zone-only candidates (carrier x carrier), built once per zone: only
    # those in the copper box widened by the tolerance can be in the copper.
    reach_box = (copper_box[0] - tolerance, copper_box[1] - tolerance,
                 copper_box[2] + tolerance, copper_box[3] + tolerance)
    candidates = outline.crossings_in(reach_box)
    copper_carriers = _copper_carriers(hull, radius)
    candidates.extend((x + radius, y) for x, y in hull)
    for first in outline.carriers:
        for second in copper_carriers:
            candidates.extend(_meet(first, second))
    return any(in_copper(point) and outline.deep(point) for point in candidates)


def check_pad_in_keepout(footprint: Footprint) -> list:
    """@brief Pads whose copper meets a keepout zone that forbids pads on their side.

    @param footprint The footprint checked.
    @return One warning per (zone, pad) pair: the zone forbids pads
        (`FpZone.forbids`), the pad has a copper side among the zone's
        layers (`pad_copper_sides`: a mechanical hole counts on the copper
        layers it names, as KiCad tests every pad object on its layer set)
        and its shape meets the zone deflated by KiCad's DRC epsilon
        (`copper_meets_keepout`, `KEEPOUT_DRC_EPSILON_MM`: touching, or
        crossing the border by less than 0.5 um, is not a finding, as in
        KiCad).
    @note Not a KLC rule: KiCad's DRC reports it on the board, said so in
        the message. A zone that allows pads is never a finding.
    """
    findings = []
    zones = [(index, graphic) for index, graphic in enumerate(footprint.graphics)
             if isinstance(graphic, FpZone) and graphic.forbids('pads')]
    for zone_index, zone in zones:
        corners = [tuple(point) for point in zone.points]
        if len(corners) < 3:
            continue
        # Zone-only geometry once per zone, shared by every pad.
        outline = KeepoutOutline(corners)
        for pad_index, pad in enumerate(footprint.pads):
            shared = pad_copper_sides(pad) & set(zone.layers)
            if not shared or not copper_meets_keepout(pad_copper(pad), outline):
                continue
            findings.append(_finding('pad_in_keepout', 'warning', tx(
                'Pad {number} dentro una zona di esclusione che vieta i pad su {layers}: il DRC di KiCad lo '
                'segnala sulla scheda (non è una regola KLC).', number=pad.number or '«»',
                layers=', '.join(name for name in ('F.Cu', 'B.Cu') if name in shared)),
                pads=(pad_index,), graphics=(zone_index,)))
    return findings


def check_footprint_geometry(footprint: Footprint) -> list:
    """@brief Run every geometric rule on a footprint: errors first, then warnings.

    @param footprint The footprint checked (generated graphics and the user's
        drawings already added).
    @return `GeometryFinding` list, errors before warnings, rule order kept
        within each severity.
    """
    findings = []
    for check in (check_pads_overlap, check_hole_vs_lead, check_silk_clearance, check_courtyard_enclosure,
                  check_annular_ring, check_estimated_pads, check_pin_one_marker, check_silk_stroke_width,
                  check_silk_text_size, check_pad_in_keepout):
        findings.extend(check(footprint))
    return sorted(findings, key=lambda f: 0 if f.severity == 'error' else 1)
