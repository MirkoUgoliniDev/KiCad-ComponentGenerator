"""@brief Shared measuring tool of the footprint preview and the symbol editor, and the shared dimension line.

Plan `docs/canvas-measure-plan.md`. Both tabs measure the same way: the tool
is one more `canvas_clicktools.ToolSpec` (`MEASURE_SPEC`, open-ended and
persistent: two clicks for a true distance or a perpendicular, three for a
linear dimension and its placement) on each tab's existing
`PointCollector`; the host forwards its
`ClickToolHost` callbacks to a `MeasureTool` when `spec.key == MEASURE_KEY`
and only provides the targets (pad or pin centres with their hit shapes,
body corners and edges).

Layers of this module:

- Pure, Qt-free geometry (unit-testable without a scene): `MeasureAnchor`,
  `SnapTargets`, `pad_polygon`, `circle_polygon`, `point_in_polygon`,
  `segment_foot`, `line_foot`, `snap_point`, `Measurement`, `measure`,
  `measurement_label`.
- Qt drawing: `DimensionStyle` and `add_dimension_line` (also the drawing of
  the footprint preview's overall dimensions, factored out of
  `MainWindow._add_dim_h` / `_add_dim_v`), `screen_dot` (factored out of
  `main_window._screen_dot`), `anchor_marker`, `bounds_without_measures`.
- `MeasureOverlay`: owns the measure items of one scene (add, remove, clear,
  hover highlight, hit test, redraw after the host rebuilt its scene, drop
  when the geometry changed, the hover feedback and live preview).
- `MeasureTool`: the host glue both tabs share (snap, finish, Escape ladder,
  hover feedback, per-measure removal by menu or Delete).

@note Measures are an OVERLAY: never part of a draft, of the undo history,
    of a recovery or project file or of a generated KiCad file. Their items
    accept no mouse button, carry `MEASURE_ITEM_ROLE` and nothing else, so no
    hit test of either tab sees them as a pad, pin, text or drawing; the
    overlay has its own hit test (`MeasureOverlay.measurement_at`).
@note Scene units differ per tab (footprint 20 px per mm, Y down; symbol 10
    units per mm, Y flipped): every function takes positions in scene units
    and the host's scene-units-per-mm factor; labels are always in mm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem,
                             QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsSimpleTextItem)

from ..core.package_geometry import _rotate_kicad, pad_outline
from . import canvas_menus
from .canvas_clicktools import ToolSpec
from .theme import BASE

# ── Constants ──────────────────────────────────────────────────────────────

#: Key of the measuring tool in a `ToolSpec`; hosts dispatch on it.
MEASURE_KEY = "measure"

#: The measuring tool: open-ended (count 0) because a measure has two points
#: (true distance, perpendicular) or three (a linear dimension and its
#: placement): `MeasureTool.points_changed` finishes the collector at the
#: right count. Stays armed after each measure; a press-move-release takes
#: the release point too (click tools plan, decision C1).
MEASURE_SPEC = ToolSpec(MEASURE_KEY, count=0, persistent=True, drag_adds_release_point=True)

#: Snap reaches in SCREEN pixels, converted to scene units with the view's
#: current scale so they feel the same at any zoom. They are pointing
#: tolerances of the display (user feedback 2026-10-31: «il metro deve
#: intercettare il centro del pad», the ruler must catch the pad centre), not
#: geometric quantities of the part:
#: - a point inside a pad's copper, or within PAD_MARGIN_PX of its outline,
#:   takes the pad CENTRE (centres win over everything else);
#: - within CORNER_PX of a body corner it takes the corner;
#: - within EDGE_PX of a body edge it takes the perpendicular foot.
PAD_MARGIN_PX = 6.0
CORNER_PX = 12.0
EDGE_PX = 10.0

#: Screen distance (px) from a measure's line within which the pointer
#: hovers it (highlight, Delete, «Rimuovi misura»).
REMOVE_TOLERANCE_PX = 6.0

#: Colour of the measures: Catppuccin Mocha teal, distinct from the yellow
#: drawing markers, the blue pads and the green handles, and
#: readable on the dark base of both canvases.
MEASURE_COLOR = "#94e2d5"
#: Brighter variant of a hovered measure and of the hovered target outline.
MEASURE_HIGHLIGHT_COLOR = "#d4fbf3"

#: Item data role set on every overlay item (value True).
MEASURE_ITEM_ROLE = 0x4D4541

#: Z value of the committed measures and of the transient markers: above
#: pads (1-5), drawing markers (6-9) and texts.
MEASURE_Z = 60.0
MEASURE_TRANSIENT_Z = 61.0

#: Anchor kinds.
KIND_CENTRE = "centre"
KIND_CORNER = "corner"
KIND_EDGE = "edge"
KIND_FREE = "free"

#: Italian sources of the measure labels (translated by the host's catalogue).
LABEL_SOURCE = "{distance} mm · ΔX {dx} · ΔY {dy}"
LABEL_SOURCE_PERPENDICULAR = "⊥ {distance} mm · ΔX {dx} · ΔY {dy}"

#: Italian sources of the hover tags. The centre tag is the host's: the
#: footprint says «Pad {label} · centro», the symbol «Pin {label} · connessione».
TAG_PAD_CENTRE = "Pad {label} · centro"
TAG_PIN_END = "Pin {label} · connessione"
TAG_CENTRE = "Centro"
TAG_CORNER = "Spigolo"
TAG_EDGE = "Bordo"
TAG_FREE = "Punto libero"

#: Italian sources of the toolbar buttons and of the measure menu, identical in both tabs.
MEASURE_TOOLTIP_SOURCE = ("Misura: clicca un centro, uno spigolo o un bordo. Allineandoti a un altro "
                          "pad nasce una quota orizzontale o verticale: posizionala con un terzo clic. "
                          "Tasto destro o Invio confermano, Maiusc dà un punto libero, Esc annulla il "
                          "punto e poi cancella le misure.")
CLEAR_MEASURES_SOURCE = "Cancella misure"
REMOVE_MEASURE_SOURCE = "Rimuovi misura"
EDIT_MEASURE_SOURCE = "Modifica quota…"

#: Menu key of «Modifica quota…» (edit the measured value).
EDIT_MEASURE = "edit_measure"
#: Italian source of «Esporta come quota» (export the measure as a KiCad dimension of the footprint).
EXPORT_MEASURE_SOURCE = "Esporta come quota"
#: Menu key of «Esporta come quota»; offered only by a host that gives `on_export`.
EXPORT_MEASURE = "export_measure"

#: Label placements of `DimensionStyle.label`.
LABEL_ABOVE = "above"      # horizontal text above the line, scene-sized (preview dimensions)
LABEL_ALONG = "along"      # text rotated -90 degrees along a vertical line, scene-sized
LABEL_SCREEN = "screen"    # screen-constant boxed text centred beside the line (measures)

#: Below this squared length (scene units^2) a segment is a point.
_EPS2 = 1e-18


# ── Pure geometry ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MeasureAnchor:
    """@brief One end of a measure, in scene units.

    @note `kind` is one of KIND_CENTRE (a pad centre or a pin connection
        point; `label` = its number, `shape` = its hit polygon), KIND_CORNER,
        KIND_EDGE (`edge` = the segment (x1, y1, x2, y2) the point lies on)
        or KIND_FREE (no target in reach, or Shift); a second anchor may be
        KIND_ALIGNED (taken through alignment `guides`, each
        (orientation 'v'/'h', x, y, label, shape)) and carries `axis` 'x' / 'y'
        when it starts a linear dimension; KIND_PLACE is a linear
        dimension's placement point. Frozen and comparable:
        the click collector refuses a click equal to the previous one.
    """
    x: float
    y: float
    kind: str = KIND_FREE
    label: str = ""
    edge: Optional[tuple] = None
    shape: Optional[tuple] = None
    axis: str = ""
    guides: tuple = ()


@dataclass(frozen=True)
class SnapTargets:
    """@brief The geometry a tab offers to the measuring tool, in scene units.

    @note `centres` holds (x, y, label, shape) tuples, `shape` being the
        hit polygon ((x, y), ...) of the pad copper or pin end, or None for a
        bare point (hit within PAD_MARGIN_PX only); (x, y, label) triples are
        accepted as bare points. `corners` holds (x, y), `edges`
        (x1, y1, x2, y2). Equality compares the whole geometry: the overlay
        drops its measures when the targets of a redraw differ. The bounding
        boxes of the shapes are computed once, at construction, so a hover
        does not recompute them.
    """
    centres: tuple = ()
    corners: tuple = ()
    edges: tuple = ()

    def __post_init__(self):
        """Normalise the centres to 4-tuples and precompute the shape bounding boxes."""
        centres = tuple(c if len(c) == 4 else (c[0], c[1], c[2], None) for c in self.centres)
        object.__setattr__(self, "centres", centres)
        boxes = []
        for x, y, _label, shape in centres:
            if shape:
                xs = [p[0] for p in shape]
                ys = [p[1] for p in shape]
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
            else:
                boxes.append((x, y, x, y))
        object.__setattr__(self, "_boxes", tuple(boxes))

    @staticmethod
    def rectangle(x0: float, y0: float, x1: float, y1: float) -> tuple:
        """@brief Corners and edges of an axis-aligned rectangle.

        @param x0 One corner X (scene units).
        @param y0 One corner Y.
        @param x1 The opposite corner X.
        @param y1 The opposite corner Y.
        @return (corners, edges) tuples, corners in drawing order.
        """
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        edges = tuple((*corners[i], *corners[(i + 1) % 4]) for i in range(4))
        return corners, edges


def circle_polygon(x: float, y: float, radius: float, segments: int = 24) -> tuple:
    """@brief A circle as a polygon (hit area of a round pad or a pin end).

    @param x Centre X.
    @param y Centre Y.
    @param radius Radius, same units.
    @param segments Number of vertices.
    @return ((x, y), ...) counter-clockwise.
    """
    return tuple((x + radius * math.cos(2 * math.pi * i / segments),
                  y + radius * math.sin(2 * math.pi * i / segments)) for i in range(segments))


def pad_polygon(x: float, y: float, size_x: float, size_y: float, shape: str = "rect",
                rotation_deg: float = 0.0, segments: int = 12) -> tuple:
    """@brief The copper outline of a pad as a polygon, for the hit test.

    @param x Pad centre X (any unit, Y down as KiCad).
    @param y Pad centre Y.
    @param size_x Pad size along its local X.
    @param size_y Pad size along its local Y.
    @param shape KiCad pad shape: circle, oval, rect, roundrect, trapezoid, custom.
    @param rotation_deg KiCad pad angle (counter-clockwise on screen).
    @param segments Vertices per half circle of an oval or circle end.
    @return ((x, y), ...): the rotated rectangle of `core.package_geometry.pad_outline`
        for rect, roundrect, trapezoid and custom (a conservative envelope),
        a circle for circle, a stadium for oval.
    """
    if shape == "circle":
        return circle_polygon(x, y, max(size_x, size_y) / 2, 2 * segments)
    if shape != "oval" or size_x == size_y:
        if shape == "oval":
            return circle_polygon(x, y, size_x / 2, 2 * segments)
        return tuple(pad_outline(x, y, size_x, size_y, rotation_deg))
    # Stadium in the pad's own axes, then turned like KiCad turns a pad.
    radius = min(size_x, size_y) / 2
    half = (max(size_x, size_y) - min(size_x, size_y)) / 2
    along_x = size_x > size_y
    local = []
    for end, sign in ((half, 1.0), (-half, -1.0)):
        for i in range(segments + 1):
            angle = -math.pi / 2 + math.pi * i / segments
            u, v = end + sign * radius * math.cos(angle), sign * radius * math.sin(angle)
            local.append((u, v) if along_x else (v, u))
    a = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    return tuple((x + rx, y + ry) for rx, ry in (_rotate_kicad(u, v, cos_a, sin_a) for u, v in local))


def point_in_polygon(px: float, py: float, polygon: Sequence) -> bool:
    """@brief Even-odd test of a point against a polygon.

    @param px Point X.
    @param py Point Y.
    @param polygon ((x, y), ...) vertices.
    @return True when the point is inside.
    """
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            if px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def segment_foot(px: float, py: float, edge: Sequence[float]) -> tuple:
    """@brief The point of a segment nearest to a point (foot clamped to the segment).

    @param px Point X.
    @param py Point Y.
    @param edge Segment (x1, y1, x2, y2), same units.
    @return (x, y) on the segment; the first end for a zero-length segment.
    """
    x1, y1, x2, y2 = edge
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 <= _EPS2:
        return (x1, y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
    return (x1 + t * dx, y1 + t * dy)


def line_foot(px: float, py: float, edge: Sequence[float]) -> tuple:
    """@brief The perpendicular foot of a point on the infinite line through a segment.

    @param px Point X.
    @param py Point Y.
    @param edge Segment (x1, y1, x2, y2) giving the line.
    @return (x, y) on the line; the first end for a zero-length segment.
    """
    x1, y1, x2, y2 = edge
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 <= _EPS2:
        return (x1, y1)
    t = ((px - x1) * dx + (py - y1) * dy) / length2
    return (x1 + t * dx, y1 + t * dy)


def _polygon_distance(px: float, py: float, polygon: Sequence) -> float:
    """Distance from a point to the outline of a polygon."""
    n = len(polygon)
    best = math.inf
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        fx, fy = segment_foot(px, py, (x1, y1, x2, y2))
        best = min(best, math.hypot(fx - px, fy - py))
    return best


def snap_point(x: float, y: float, targets: SnapTargets, scale: float = 1.0, *,
               free: bool = False) -> MeasureAnchor:
    """@brief Snap a pointer position to the measurable target under it.

    @param x Pointer X, scene units.
    @param y Pointer Y, scene units.
    @param targets The tab's `SnapTargets`.
    @param scale View pixels per scene unit (the view's current zoom): the
        screen reaches PAD_MARGIN_PX, CORNER_PX and EDGE_PX are divided by it.
    @param free True (Shift) returns the raw pointer, whatever is near.
    @return The anchor. Priority: a pad or pin whose hit shape CONTAINS the
        point (its centre; the nearest centre when several contain it), else
        one whose outline is within PAD_MARGIN_PX (the nearest), else a corner within CORNER_PX,
        else the nearest edge foot within EDGE_PX, else the raw point
        (KIND_FREE). There is no grid fallback: a measure is taken on the
        geometry or exactly where the pointer is.
    @note A priority, not a nearest-of-all rule: anywhere on a pad's copper
        the pad centre is what is meant, even if an outline passes closer.
    """
    if free:
        return MeasureAnchor(float(x), float(y), KIND_FREE)
    s = max(abs(float(scale)), 1e-9)
    margin, corner_reach, edge_reach = PAD_MARGIN_PX / s, CORNER_PX / s, EDGE_PX / s

    best = None
    inside = None
    boxes = getattr(targets, "_boxes", ())
    for index, (cx, cy, label, shape) in enumerate(targets.centres):
        x0, y0, x1, y1 = boxes[index]
        if x < x0 - margin or x > x1 + margin or y < y0 - margin or y > y1 + margin:
            continue
        d = math.hypot(cx - x, cy - y)
        anchor = None
        if shape and point_in_polygon(x, y, shape):
            # On the copper itself: this pad, whatever margin of a neighbour
            # also reaches the point (an exposed pad beside small leads).
            if inside is None or d < inside[0]:
                inside = (d, MeasureAnchor(float(cx), float(cy), KIND_CENTRE, str(label), shape=shape))
            continue
        if shape:
            near = _polygon_distance(x, y, shape) <= margin
        else:
            near = d <= margin
        if near:
            anchor = MeasureAnchor(float(cx), float(cy), KIND_CENTRE, str(label), shape=shape)
            if best is None or d < best[0]:
                best = (d, anchor)
    if inside is not None:
        return inside[1]
    if best is not None:
        return best[1]
    for cx, cy in targets.corners:
        d = math.hypot(cx - x, cy - y)
        if d <= corner_reach and (best is None or d < best[0]):
            best = (d, MeasureAnchor(float(cx), float(cy), KIND_CORNER))
    if best is not None:
        return best[1]
    for edge in targets.edges:
        fx, fy = segment_foot(x, y, edge)
        d = math.hypot(fx - x, fy - y)
        if d <= edge_reach and (best is None or d < best[0]):
            best = (d, MeasureAnchor(float(fx), float(fy), KIND_EDGE, edge=tuple(map(float, edge))))
    if best is not None:
        return best[1]
    return MeasureAnchor(float(x), float(y), KIND_FREE)


@dataclass(frozen=True)
class Measurement:
    """@brief One measure: the two clicked anchors and what was measured between them.

    @note `start` / `end` are the scene points the dimension line joins
        (an edge end is replaced by the perpendicular foot); `distance_mm`,
        `dx_mm`, `dy_mm` are millimetres, ΔX and ΔY as absolute values (the
        symbol scene is Y-flipped: magnitudes read the same in both tabs).
        A linear dimension has `axis` 'x' (horizontal, |ΔX|) or 'y'
        (vertical, |ΔY|) and its two `extensions` (x1, y1, x2, y2).
    """
    first: MeasureAnchor
    second: MeasureAnchor
    start: tuple
    end: tuple
    distance_mm: float
    dx_mm: float
    dy_mm: float
    perpendicular: bool = False
    axis: str = ""
    extensions: tuple = ()


def _parallel(a: Sequence[float], b: Sequence[float]) -> bool:
    """True when two segments are parallel (cross product of the unit directions ~ 0)."""
    ax, ay = a[2] - a[0], a[3] - a[1]
    bx, by = b[2] - b[0], b[3] - b[1]
    la, lb = math.hypot(ax, ay), math.hypot(bx, by)
    if la == 0 or lb == 0:
        return False
    return abs(ax * by - ay * bx) / (la * lb) < 1e-9


def measure(first: MeasureAnchor, second: MeasureAnchor, units_per_mm: float) -> Measurement:
    """@brief Measure between two anchors.

    @param first The first clicked anchor.
    @param second The second clicked anchor.
    @param units_per_mm Scene units per millimetre of the host (> 0).
    @return The `Measurement`.
    @throws ValueError When `units_per_mm` is not a positive finite number.
    @note Rules (plan D-M2): the second anchor an edge -> the first point is
        projected perpendicularly on that edge's LINE and the foot is the end
        (perpendicular distance); only the first an edge -> the second point
        is projected on the first's line; both edges -> perpendicular when
        parallel (e.g. a body width), else straight between the two points.
    """
    k = float(units_per_mm)
    if not math.isfinite(k) or k <= 0:
        raise ValueError(f"units_per_mm must be positive, got {units_per_mm!r}")
    start = (first.x, first.y)
    end = (second.x, second.y)
    perpendicular = False
    first_edge = first.kind == KIND_EDGE and first.edge is not None
    second_edge = second.kind == KIND_EDGE and second.edge is not None
    if second_edge and (not first_edge or _parallel(first.edge, second.edge)):
        end = line_foot(first.x, first.y, second.edge)
        perpendicular = True
    elif first_edge and not second_edge:
        start = line_foot(second.x, second.y, first.edge)
        perpendicular = True
    dx = abs(end[0] - start[0]) / k
    dy = abs(end[1] - start[1]) / k
    return Measurement(first, second, start, end, math.hypot(dx, dy), dx, dy, perpendicular)


def measurement_label(m: Measurement, translate: Callable[[str], str] = str) -> str:

    """@brief The label of a measure in the interface language, two decimals.

    @param m The measure.
    @param translate The host's catalogue lookup for an Italian source.
    @return E.g. «2.54 mm · ΔX 2.54 · ΔY 0.00»; prefixed «⊥» for a
        perpendicular distance.
    """
    template = translate(LABEL_SOURCE_PERPENDICULAR if m.perpendicular else LABEL_SOURCE)
    return template.format(distance=f"{m.distance_mm:.2f}", dx=f"{m.dx_mm:.2f}", dy=f"{m.dy_mm:.2f}")



#: Screen reach (px) of an alignment guide: the pointer's X (Y) this close to
#: a target's X (Y) snaps to it. A pointing tolerance, like the other reaches.
GUIDE_PX = 8.0

#: Dominance ratio of a direct linear dimension: a second target whose ΔX is
#: at least this many times ΔY gives a horizontal dimension (and vice versa);
#: in between the measure is the true (diagonal) distance. A drawing
#: convention for «almost aligned» targets, not a property of the part.
LINEAR_DOMINANCE = 3.0

#: Extension lines of a linear dimension, in mm of the part: the gap left at
#: the anchor and the overshoot past the dimension line (datasheet practice).
EXTENSION_GAP_MM = 0.2
EXTENSION_OVERSHOOT_MM = 0.5

#: Anchor kind of a point taken through alignment guides (`MeasureAnchor.guides`).
KIND_ALIGNED = "aligned"
#: Anchor kind of the placement point of a linear dimension (third click).
KIND_PLACE = "place"

#: Italian sources of the linear-dimension label and of the aligned tag.
LABEL_SOURCE_LINEAR = "{distance} mm"
TAG_ALIGNED = "Allineato: {target}"


class GuideIndex:
    """@brief Sorted X and Y coordinates of the targets, for the alignment guides.

    @note Built once per target change (`MeasureTool.sync`); a hover looks
        up the nearest coordinate with `bisect`, so its cost does not grow
        with a scan of every target.
    """

    def __init__(self, targets: "SnapTargets"):
        """@brief Index the centres and corners of `targets`.

        @param targets The `SnapTargets` of the drawn geometry.
        """
        entries = [(x, y, str(label), shape) for x, y, label, shape in targets.centres]
        entries += [(x, y, "", None) for x, y in targets.corners]
        self.by_x = sorted(entries, key=lambda e: e[0])
        self.by_y = sorted(entries, key=lambda e: e[1])
        self._xs = [e[0] for e in self.by_x]
        self._ys = [e[1] for e in self.by_y]

    @staticmethod
    def _nearest(values, entries, value, reach, exclude):
        import bisect
        i = bisect.bisect_left(values, value)
        best = None
        # Walk outwards on each side, skipping the excluded coordinate (a run
        # of targets on the first anchor's own line), until out of reach.
        for step in (-1, 1):
            j = i - 1 if step < 0 else i
            while 0 <= j < len(values):
                d = abs(values[j] - value)
                if d > reach:
                    break
                if exclude is None or abs(values[j] - exclude) > 1e-9:
                    if best is None or d < best[0]:
                        best = (d, entries[j])
                    break
                j += step
        return None if best is None else best[1]

    def nearest_x(self, x: float, reach: float, exclude: Optional[float] = None):
        """@brief The target whose X is nearest to `x` within `reach`, or None.

        @param x Pointer X.
        @param reach Reach in scene units.
        @param exclude An X never returned (the first anchor's own, a zero-length dimension).
        @return (x, y, label, shape) or None.
        """
        return self._nearest(self._xs, self.by_x, x, reach, exclude)

    def nearest_y(self, y: float, reach: float, exclude: Optional[float] = None):
        """@brief The target whose Y is nearest to `y` within `reach`, or None.

        @param y Pointer Y.
        @param reach Reach in scene units.
        @param exclude A Y never returned.
        @return (x, y, label, shape) or None.
        """
        return self._nearest(self._ys, self.by_y, y, reach, exclude)


def _linear_axis(first: MeasureAnchor, x: float, y: float) -> str:
    """'x' (horizontal dimension) or 'y' (vertical) when one delta dominates, else ''."""
    dx, dy = abs(x - first.x), abs(y - first.y)
    if dx > 0 and dx >= LINEAR_DOMINANCE * dy:
        return "x"
    if dy > 0 and dy >= LINEAR_DOMINANCE * dx:
        return "y"
    return ""


def snap_second(x: float, y: float, targets: SnapTargets, index: GuideIndex, first: MeasureAnchor,
                scale: float = 1.0, *, free: bool = False) -> MeasureAnchor:
    """@brief Snap the SECOND point of a measure, with the alignment guides.

    @param x Pointer X, scene units.
    @param y Pointer Y, scene units.
    @param targets The tab's `SnapTargets`.
    @param index Their `GuideIndex`.
    @param first The first anchor.
    @param scale View pixels per scene unit.
    @param free Shift: the raw point, no guide, a diagonal measure.
    @return The anchor. Order: a pad/pin hit (its centre) or a corner within
        reach, with `axis` 'x' / 'y' when ΔX / ΔY dominates by
        LINEAR_DOMINANCE (a linear dimension), '' otherwise (diagonal);
        else an edge foot within EDGE_PX (the perpendicular case); else the
        alignment guides within GUIDE_PX: the pointer's X on a
        target's X gives a KIND_ALIGNED point on that vertical (axis 'x',
        a horizontal dimension measuring ΔX), its Y on a target's Y a point on
        that horizontal (axis 'y'); both at once take that crossing point
        (both guides kept, axis by dominance); else the raw point (diagonal).
    """
    if free:
        return MeasureAnchor(float(x), float(y), KIND_FREE)
    base = snap_point(x, y, targets, scale)
    if base.kind in (KIND_CENTRE, KIND_CORNER):
        axis = _linear_axis(first, base.x, base.y)
        return MeasureAnchor(base.x, base.y, base.kind, base.label, shape=base.shape, axis=axis)
    if base.kind == KIND_EDGE:
        # The perpendicular case wins over a guide: near the outline the
        # meant measure is centre-to-edge (plan D-M2).
        return base
    s = max(abs(float(scale)), 1e-9)
    reach = GUIDE_PX / s
    vertical = index.nearest_x(x, reach, exclude=first.x)
    horizontal = index.nearest_y(y, reach, exclude=first.y)
    if vertical is not None or horizontal is not None:
        gx = vertical[0] if vertical is not None else x
        gy = horizontal[1] if horizontal is not None else y
        guides = []
        if vertical is not None:
            guides.append(("v",) + tuple(vertical))
        if horizontal is not None:
            guides.append(("h",) + tuple(horizontal))
        if vertical is not None and horizontal is not None:
            axis = _linear_axis(first, gx, gy)
        else:
            axis = "x" if vertical is not None else "y"
        label = (vertical or horizontal)[2]
        return MeasureAnchor(float(gx), float(gy), KIND_ALIGNED, label, axis=axis, guides=tuple(guides))
    return MeasureAnchor(float(x), float(y), KIND_FREE)


def measure_linear(first: MeasureAnchor, second: MeasureAnchor, units_per_mm: float,
                   place: Optional[MeasureAnchor] = None) -> Measurement:
    """@brief A linear (horizontal or vertical) dimension between two anchors.

    @param first The first anchor.
    @param second The second anchor; its `axis` is 'x' (horizontal, ΔX) or 'y' (vertical, ΔY).
    @param units_per_mm Scene units per millimetre (> 0).
    @param place The placement point (third click), or None: the dimension
        line then passes through the first anchor.
    @return The `Measurement`: `start` / `end` on the dimension line, the
        value |ΔX| or |ΔY| in mm, `extensions` the two extension lines (a gap
        of EXTENSION_GAP_MM at each anchor, EXTENSION_OVERSHOOT_MM past the
        dimension line).
    @throws ValueError When `units_per_mm` is not positive or `second.axis` is not 'x' or 'y'.
    """
    k = float(units_per_mm)
    if not math.isfinite(k) or k <= 0:
        raise ValueError(f"units_per_mm must be positive, got {units_per_mm!r}")
    if second.axis not in ("x", "y"):
        raise ValueError(f"a linear dimension needs axis 'x' or 'y', got {second.axis!r}")
    gap, over = EXTENSION_GAP_MM * k, EXTENSION_OVERSHOOT_MM * k
    extensions = []
    if second.axis == "x":
        line = place.y if place is not None else first.y
        start, end = (first.x, line), (second.x, line)
        for ax, ay in ((first.x, first.y), (second.x, second.y)):
            sign = 1.0 if line >= ay else -1.0
            if abs(line - ay) > gap:
                extensions.append((ax, ay + sign * gap, ax, line + sign * over))
        value = abs(second.x - first.x) / k
        dx, dy = value, 0.0
    else:
        line = place.x if place is not None else first.x
        start, end = (line, first.y), (line, second.y)
        for ax, ay in ((first.x, first.y), (second.x, second.y)):
            sign = 1.0 if line >= ax else -1.0
            if abs(line - ax) > gap:
                extensions.append((ax + sign * gap, ay, line + sign * over, ay))
        value = abs(second.y - first.y) / k
        dx, dy = 0.0, value
    return Measurement(first, second, start, end, value, dx, dy, False, second.axis, tuple(extensions))


def measure_points(points: Sequence, units_per_mm: float) -> Measurement:
    """@brief The measure the collected anchors describe (two, or three with a placement).

    @param points The anchors: first, second and optionally the placement point.
    @param units_per_mm Scene units per millimetre.
    @return A linear dimension when the second anchor has an axis, else `measure`.
    """
    first, second = points[0], points[1]
    if second.axis in ("x", "y"):
        return measure_linear(first, second, units_per_mm, points[2] if len(points) > 2 else None)
    return measure(first, second, units_per_mm)


#: Anchor kinds an edited measure may move: a pad centre or a pin end. A
#: corner, an edge, an aligned or a free point is geometry the host does not
#: move by a measure (user, 2026-11-02: «se uno è un bordo deve modificare
#: solo posizione pad» (if one is an edge, move only the pad)).
MOVABLE_KINDS = (KIND_CENTRE,)


def measure_mover(m: Measurement) -> str:
    """@brief Which end an edit of the measure moves: 'second', 'first' or '' (not editable).

    @param m The measure.
    @return 'second' when the second anchor is a centre (the first is the
        reference: the user chooses which pad moves by the order of the
        clicks, decision of 2026-11-02, to confirm with the user), else
        'first' when only the first is a centre, else '' (no pad in the
        measure, e.g. a body width between two edges).
    """
    if m.second.kind in MOVABLE_KINDS:
        return "second"
    if m.first.kind in MOVABLE_KINDS:
        return "first"
    return ""


def edit_displacement(m: Measurement, value_mm: float, units_per_mm: float) -> Optional[tuple]:
    """@brief The move of one end that gives a measure a new value.

    @param m The measure.
    @param value_mm The wanted value, millimetres (>= 0).
    @param units_per_mm Scene units per millimetre of the host (> 0).
    @return (end, dx, dy): `end` 'first' or 'second' (`measure_mover`), the
        displacement of that anchor in scene units; None when the measure is
        not editable, the value is not a finite number >= 0, or the measured
        direction is undefined (both ends at the same point).
    @note The moved end slides along the measured direction, so only the
        measured quantity changes: a linear dimension changes only its ΔX
        (axis 'x') or ΔY (axis 'y'); a centre-to-edge measure moves the
        centre perpendicular to the edge (the edge never moves); a true
        distance keeps its direction. The direction points from the fixed
        end to the moved one, so a larger value moves them apart.
    """
    mover = measure_mover(m)
    k = float(units_per_mm)
    try:
        value = float(value_mm)
    except (TypeError, ValueError):
        return None
    if not mover or not math.isfinite(value) or value < 0 or not math.isfinite(k) or k <= 0:
        return None
    dx, dy = m.end[0] - m.start[0], m.end[1] - m.start[1]
    if m.axis == "x":
        dy = 0.0
    elif m.axis == "y":
        dx = 0.0
    if mover == "first":
        dx, dy = -dx, -dy
    length = math.hypot(dx, dy)
    if length * length < _EPS2:
        return None
    change = value * k - length
    return mover, dx / length * change, dy / length * change


def _index_of(points: Sequence, x: float, y: float) -> Optional[int]:
    """Index of the point (first two values of each entry) equal to (x, y) within 1e-6, or None."""
    for index, entry in enumerate(points):
        if abs(entry[0] - x) < 1e-6 and abs(entry[1] - y) < 1e-6:
            return index
    return None


def _reanchor(anchor: MeasureAnchor, old: SnapTargets, new: SnapTargets) -> Optional[MeasureAnchor]:
    """The anchor on the redrawn targets, or None when its target cannot be found again.

    A centre, a corner or an edge is found by its POSITION in the old
    targets and taken at the same index in the new ones, provided the lists
    have the same length (and, for a centre, the same label): the targets of
    a footprint or symbol are built in the same order at every redraw. An
    edge point keeps its fraction along the edge. Aligned and free points
    belong to no target and cannot follow.
    """
    if anchor.kind == KIND_CENTRE:
        index = _index_of(old.centres, anchor.x, anchor.y)
        if index is None or len(old.centres) != len(new.centres) or old.centres[index][2] != new.centres[index][2]:
            return None
        x, y, _label, shape = new.centres[index]
        return MeasureAnchor(x, y, anchor.kind, anchor.label, shape=shape, axis=anchor.axis)
    if anchor.kind == KIND_CORNER:
        index = _index_of(old.corners, anchor.x, anchor.y)
        if index is None or len(old.corners) != len(new.corners):
            return None
        x, y = new.corners[index]
        return MeasureAnchor(x, y, anchor.kind, anchor.label, axis=anchor.axis)
    if anchor.kind == KIND_EDGE and anchor.edge is not None:
        if anchor.edge not in old.edges or len(old.edges) != len(new.edges):
            return None
        edge = new.edges[old.edges.index(anchor.edge)]
        x1, y1, x2, y2 = anchor.edge
        length2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
        t = 0.0 if length2 < _EPS2 else ((anchor.x - x1) * (x2 - x1) + (anchor.y - y1) * (y2 - y1)) / length2
        nx1, ny1, nx2, ny2 = edge
        return MeasureAnchor(nx1 + t * (nx2 - nx1), ny1 + t * (ny2 - ny1), anchor.kind, anchor.label,
                             edge=edge, axis=anchor.axis)
    return None


def reanchor_measurement(m: Measurement, old: SnapTargets, new: SnapTargets,
                         units_per_mm: float) -> Optional[Measurement]:
    """@brief The same measure taken again on redrawn geometry, or None when it cannot follow.

    @param m The measure taken on `old`.
    @param old The targets it was taken on.
    @param new The targets of the redrawn geometry.
    @param units_per_mm Scene units per millimetre.
    @return The measure between the same targets where they are now, its
        value recomputed (never the old number on new geometry); None when
        an end is an aligned or free point or its target is gone.
    @note A linear dimension keeps its dimension line at the same offset
        from its first anchor.
    """
    first, second = _reanchor(m.first, old, new), _reanchor(m.second, old, new)
    if first is None or second is None:
        return None
    if m.axis in ("x", "y"):
        if m.axis == "x":
            place = MeasureAnchor(first.x, first.y + (m.start[1] - m.first.y), KIND_PLACE)
        else:
            place = MeasureAnchor(first.x + (m.start[0] - m.first.x), first.y, KIND_PLACE)
        return measure_linear(first, second, units_per_mm, place)
    return measure(first, second, units_per_mm)


# ── Qt drawing ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DimensionStyle:
    """@brief How `add_dimension_line` draws.

    @note `tick` is the half length of the end ticks in scene units (0 = no
        ticks); `arrow_px` the arrowhead length in screen px (0 = none);
        `label` one of LABEL_ABOVE, LABEL_ALONG, LABEL_SCREEN; `label_gap`
        the scene offset of an ABOVE label's top (or an ALONG label's left)
        from the line; `z` None leaves the default z value.
    """
    color: str
    width: float = 1.0
    cosmetic: bool = False
    dashed: bool = False
    tick: float = 5.0
    arrow_px: float = 0.0
    label: str = LABEL_ABOVE
    label_gap: float = 18.0
    z: Optional[float] = None


#: Colour of the preview's automatic dimensions: the measures' teal, so every
#: on-screen-only measurement shares one colour. They were yellow, almost the
#: KiCad silkscreen colour (#f2eda1), and read as printed silkscreen (user,
#: 2026-11-03: «non deve essere stampato sul pcb ... ad uso interno»). It must
#: never equal a KiCad layer colour: nothing drawn in it is written to the file.
DIMENSION_COLOR = MEASURE_COLOR

#: Styles of the footprint preview's overall dimensions («totale 22.50 mm»,
#: pitch, row spacing, margin chains), drawn through `MainWindow._add_dim_h` /
#: `_add_dim_v`: DIMENSION_COLOR, 1 px, 5 px ticks, label above (horizontal) or
#: along (vertical) the line, scene-sized text.
DIMENSION_STYLE_HORIZONTAL = DimensionStyle(DIMENSION_COLOR, width=1.0, tick=5.0, label=LABEL_ABOVE,
                                            label_gap=18.0)
DIMENSION_STYLE_VERTICAL = DimensionStyle(DIMENSION_COLOR, width=1.0, tick=5.0, label=LABEL_ALONG)

#: Item data role carried by every item of an EDITABLE preview dimension
#: (`add_dimension_line(edit_key=...)`): its value is the host's key of the
#: quantity the dimension shows (e.g. "body_x"). The view finds it under the
#: pointer for «Modifica quota…» and the double click.
DIMENSION_EDIT_ROLE = 0x44494D


def editable_dimension_at(view, pos, tolerance_px: float = REMOVE_TOLERANCE_PX):
    """@brief Key of the editable dimension under a viewport position of `view`, or None.

    @param view The QGraphicsView showing the dimensions.
    @param pos Viewport position (QPoint).
    @param tolerance_px Screen reach in pixels: the same as a measure's
        (REMOVE_TOLERANCE_PX), since both are teal lines of one scene unit
        that an exact hit would need pixel aim to find.
    @return The `DIMENSION_EDIT_ROLE` value of the first item within reach.
    """
    from PyQt6.QtCore import QRect
    reach = int(math.ceil(tolerance_px))
    area = QRect(pos.x() - reach, pos.y() - reach, 2 * reach + 1, 2 * reach + 1)
    for item in view.items(area):
        key = item.data(DIMENSION_EDIT_ROLE)
        if key:
            return str(key)
    return None


def set_dimension_highlight(scene, key, on: bool) -> None:
    """@brief Show the editable dimension `key` hovered (brighter) or at rest.

    @param scene The scene holding the dimension.
    @param key Its `DIMENSION_EDIT_ROLE` value; None or "" does nothing.
    @param on True paints it in MEASURE_HIGHLIGHT_COLOR, False in DIMENSION_COLOR.
    @note Same feedback as a hovered measure (plan D-M11): the user sees
        which teal numbers answer a double click (review 2026-11-03, C4).
    """
    if not key:
        return
    colour = QColor(MEASURE_HIGHLIGHT_COLOR if on else DIMENSION_COLOR)
    for item in scene.items():
        if item.data(DIMENSION_EDIT_ROLE) != key:
            continue
        if isinstance(item, QGraphicsSimpleTextItem):
            item.setBrush(QBrush(colour))
        elif isinstance(item, QGraphicsLineItem):
            pen = item.pen()
            pen.setColor(colour)
            item.setPen(pen)

#: Style of the measures (plan D-M6).
MEASURE_STYLE = DimensionStyle(MEASURE_COLOR, width=1.4, cosmetic=True, tick=0.0, arrow_px=9.0,
                               label=LABEL_SCREEN, z=MEASURE_Z)
#: Style of the hovered measure (highlight before «Rimuovi misura» / Delete).
MEASURE_HIGHLIGHT_STYLE = DimensionStyle(MEASURE_HIGHLIGHT_COLOR, width=2.4, cosmetic=True, tick=0.0,
                                         arrow_px=11.0, label=LABEL_SCREEN, z=MEASURE_Z)
#: Style of the live preview between the first point and the pointer.
MEASURE_PREVIEW_STYLE = DimensionStyle(MEASURE_COLOR, width=1.2, cosmetic=True, dashed=True, tick=0.0,
                                       arrow_px=7.0, label=LABEL_SCREEN, z=MEASURE_TRANSIENT_Z)


def _line_item(x1, y1, x2, y2, pen):
    """A Python-built line item (its wrapper reports deletion: kept-items rule)."""
    item = QGraphicsLineItem(x1, y1, x2, y2)
    item.setPen(pen)
    return item


def _arrow(scene, tip, direction, length, color, z):
    """A screen-constant filled arrowhead with its tip at `tip`, pointing along `direction` (unit)."""
    ux, uy = direction
    nx, ny = -uy, ux
    half = length * 0.35
    bx, by = -ux * length, -uy * length
    polygon = QPolygonF([QPointF(0, 0), QPointF(bx + nx * half, by + ny * half),
                         QPointF(bx - nx * half, by - ny * half)])
    item = QGraphicsPolygonItem(polygon)
    pen = QPen(QColor(color), 1.0)
    pen.setCosmetic(True)
    item.setPen(pen)
    item.setBrush(QBrush(QColor(color)))
    scene.addItem(item)
    item.setPos(QPointF(*tip))
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    if z is not None:
        item.setZValue(z)
    return item


def _screen_label(scene, text, mid, direction, color, z):
    """A screen-constant boxed label centred on `mid`, pushed to one side of the line."""
    box = QGraphicsRectItem()
    label = QGraphicsSimpleTextItem(text, box)
    label.setBrush(QBrush(QColor(color)))
    rect = label.boundingRect()
    w, h = rect.width(), rect.height()
    pad = 3.0
    # Side of the line: the normal pointing up (or right for a vertical line).
    if direction is None:
        nx, ny = 0.0, -1.0
    else:
        nx, ny = -direction[1], direction[0]
        if ny > 0 or (ny == 0 and nx < 0):
            nx, ny = -nx, -ny
    offset = abs(nx) * (w / 2 + pad) + abs(ny) * (h / 2 + pad) + 4.0
    cx, cy = nx * offset, ny * offset
    box.setRect(cx - w / 2 - pad, cy - h / 2 - pad, w + 2 * pad, h + 2 * pad)
    label.setPos(cx - w / 2, cy - h / 2)
    background = QColor(BASE)
    background.setAlpha(225)
    pen = QPen(QColor(color), 1.0)
    pen.setCosmetic(True)
    box.setPen(pen)
    box.setBrush(QBrush(background))
    scene.addItem(box)
    box.setPos(QPointF(*mid))
    box.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    if z is not None:
        box.setZValue(z)
    return box


def add_dimension_line(scene, p1, p2, text: str, style: DimensionStyle, *, normal=None,
                       edit_key: Optional[str] = None) -> list:
    """@brief Draw a dimension line between two scene points with its label.

    @param scene The QGraphicsScene.
    @param p1 First end (x, y), scene units.
    @param p2 Second end (x, y), scene units.
    @param text The label (already translated and formatted).
    @param style The `DimensionStyle`.
    @param normal Unit vector of the end ticks; None takes the perpendicular
        of p1 -> p2 (the preview's horizontal / vertical wrappers pass
        (0, 1) / (1, 0), so a zero-length dimension still has its ticks).
    @param edit_key The host's key of the quantity, set as DIMENSION_EDIT_ROLE
        on every item, for an editable dimension; None (computed, read-only
        dimensions) sets nothing.
    @return The top-level items added, in drawing order: end ticks (p1 then
        p2), the line, the arrowheads, the label.
    @note The footprint preview's overall dimensions («totale 22.50 mm»)
        use it through `MainWindow._add_dim_h` / `_add_dim_v` with the items
        those methods drew before the factoring (same types, pens and
        positions, same order: tests/test_footprint_measure_parity.py).
        Every item is Python-built, so a caller may keep its wrapper.
    """
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    pen = QPen(QColor(style.color))
    pen.setWidthF(style.width)
    if style.cosmetic:
        pen.setCosmetic(True)
    if style.dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    length = math.hypot(x2 - x1, y2 - y1)
    direction = None if length <= 0 else ((x2 - x1) / length, (y2 - y1) / length)
    items = []

    def add(item):
        scene.addItem(item)
        if style.z is not None:
            item.setZValue(style.z)
        items.append(item)

    if style.tick > 0:
        if normal is not None:
            nx, ny = normal
        elif direction is not None:
            nx, ny = -direction[1], direction[0]
        else:
            nx, ny = 0.0, 1.0
        t = style.tick
        for px, py in ((x1, y1), (x2, y2)):
            add(_line_item(px - nx * t, py - ny * t, px + nx * t, py + ny * t, pen))
    add(_line_item(x1, y1, x2, y2, pen))
    if style.arrow_px > 0 and direction is not None:
        ux, uy = direction
        items.append(_arrow(scene, (x1, y1), (-ux, -uy), style.arrow_px, style.color, style.z))
        items.append(_arrow(scene, (x2, y2), (ux, uy), style.arrow_px, style.color, style.z))
    if style.label == LABEL_SCREEN:
        items.append(_screen_label(scene, text, ((x1 + x2) / 2, (y1 + y2) / 2), direction,
                                   style.color, style.z))
        if edit_key is not None:
            for item in items:
                item.setData(DIMENSION_EDIT_ROLE, edit_key)
        return items
    label = QGraphicsSimpleTextItem(text)
    scene.addItem(label)
    label.setBrush(QBrush(QColor(style.color)))
    if style.label == LABEL_ALONG:
        label.setRotation(-90)
        # Rotating by -90 around the origin, the text extends upwards
        # (height -> width): it is repositioned to centre it on the dimension.
        rect = label.boundingRect()
        label.setPos(x1 + 6, (y1 + y2) / 2 + rect.width() / 2)
    else:
        label.setPos((x1 + x2) / 2 - label.boundingRect().width() / 2, y1 - style.label_gap)
    if style.z is not None:
        label.setZValue(style.z)
    items.append(label)
    if edit_key is not None:
        for item in items:
            item.setData(DIMENSION_EDIT_ROLE, edit_key)
    return items


def bounds_without_measures(scene):
    """@brief The bounding rectangle of a scene's items, measures left out.

    @param scene The QGraphicsScene.
    @return The union of the scene bounding rectangles of every item that
        does not carry `MEASURE_ITEM_ROLE` (as `itemsBoundingRect`, which
        unites the same rectangles); a null QRectF for an empty scene.
    @note Measures are an overlay and never frame a camera: their labels
        ignore transformations, so their scene rectangles are screen pixels.
    """
    rect = QRectF()
    for item in scene.items():
        if not item.data(MEASURE_ITEM_ROLE):
            rect = rect.united(item.sceneBoundingRect())
    return rect


def screen_dot(scene, x: float, y: float, size: float, outline: str, fill: str) -> QGraphicsEllipseItem:
    """@brief A round marker of constant screen size at a scene point.

    @param scene Scene to draw in.
    @param x Scene x of the centre.
    @param y Scene y of the centre.
    @param size Diameter in screen px.
    @param outline Outline colour (hex).
    @param fill Fill colour (hex); "transparent" for a ring.
    @return The ellipse item (Python-built), not yet tagged with a role or z.
    @note Factored out of `main_window._screen_dot`, which adds its role and z.
    """
    half = size / 2
    dot = QGraphicsEllipseItem(-half, -half, size, size)
    dot.setPen(QPen(QColor(outline), 1.6))
    dot.setBrush(QBrush(QColor(fill)))
    scene.addItem(dot)
    dot.setPos(x, y)
    dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    return dot


def _cosmetic_pen(color: str, width: float, dashed: bool = False) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCosmetic(True)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


def anchor_marker(scene, anchor: MeasureAnchor, color: str = MEASURE_COLOR):
    """@brief The screen-constant snap marker of an anchor, shaped by its kind.

    @param scene Scene to draw in.
    @param anchor The anchor.
    @param color Stroke colour.
    @return The marker item: a ring for a centre, a square for a corner, a
        diamond for an edge foot, a small cross for any other point.
    """
    if anchor.kind == KIND_CENTRE:
        return screen_dot(scene, anchor.x, anchor.y, 12, color, "transparent")
    path = QPainterPath()
    if anchor.kind == KIND_CORNER:
        path.addRect(-5, -5, 10, 10)
    elif anchor.kind == KIND_EDGE:
        path.moveTo(0, -6)
        path.lineTo(6, 0)
        path.lineTo(0, 6)
        path.lineTo(-6, 0)
        path.closeSubpath()
    else:
        path.moveTo(-6, 0)
        path.lineTo(6, 0)
        path.moveTo(0, -6)
        path.lineTo(0, 6)
    item = QGraphicsPathItem(path)
    item.setPen(_cosmetic_pen(color, 1.6))
    scene.addItem(item)
    item.setPos(anchor.x, anchor.y)
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    return item


def _shape_highlight(scene, shape) -> QGraphicsPolygonItem:
    """The hovered pad or pin hit shape, outlined in the highlight colour."""
    item = QGraphicsPolygonItem(QPolygonF([QPointF(px, py) for px, py in shape]))
    item.setPen(_cosmetic_pen(MEASURE_HIGHLIGHT_COLOR, 2.0))
    fill = QColor(MEASURE_COLOR)
    fill.setAlpha(55)
    item.setBrush(QBrush(fill))
    scene.addItem(item)
    return item


def _screen_tag(scene, text: str, x: float, y: float) -> QGraphicsRectItem:
    """A small screen-constant tag below-right of a scene point (the hover feedback)."""
    box = QGraphicsRectItem()
    label = QGraphicsSimpleTextItem(text, box)
    label.setBrush(QBrush(QColor(MEASURE_HIGHLIGHT_COLOR)))
    rect = label.boundingRect()
    pad = 2.0
    box.setRect(12, 12, rect.width() + 2 * pad, rect.height() + 2 * pad)
    label.setPos(12 + pad, 12 + pad)
    background = QColor(BASE)
    background.setAlpha(230)
    box.setPen(_cosmetic_pen(MEASURE_COLOR, 1.0))
    box.setBrush(QBrush(background))
    scene.addItem(box)
    box.setPos(x, y)
    box.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    return box


def _mark_overlay(item, z: float) -> None:
    """Make an item an inert overlay: no mouse button, no hover, the measure role, the z value."""
    item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    item.setAcceptHoverEvents(False)
    item.setData(MEASURE_ITEM_ROLE, True)
    item.setZValue(z)
    for child in item.childItems():
        _mark_overlay(child, z)


def _remove(item) -> None:
    """Remove an item from its scene unless Qt already deleted it."""
    if item is None or sip.isdeleted(item):
        return
    scene = item.scene()
    if scene is not None:
        scene.removeItem(item)


def _segment_distance(px, py, segment) -> float:
    fx, fy = segment_foot(px, py, segment)
    return math.hypot(fx - px, fy - py)


# ── Overlay ────────────────────────────────────────────────────────────────

class MeasureOverlay:
    """@brief The committed measures of one canvas and the items that show them.

    @note The host's redraw may delete every item of the scene (the
        footprint clears the scene, the symbol removes every unkept item):
        `forget_items` drops the references and `sync` redraws the measures
        on the new scene, or drops them when the measured geometry changed.
    """

    def __init__(self, units_per_mm: float, translate: Callable[[str], str] = str, *,
                 on_changed: Optional[Callable[[int], None]] = None):
        """@brief Create an empty overlay.

        @param units_per_mm Scene units per millimetre of the host.
        @param translate Catalogue lookup of the label's Italian source.
        @param on_changed Called with the new count whenever it changes (the
            host enables «Cancella misure», clear measures).
        """
        self.units_per_mm = float(units_per_mm)
        self._translate = translate
        self._on_changed = on_changed
        self._measurements: list = []
        self._items: list = []          # one list of items per measurement
        self._transient: list = []
        self._targets: Optional[SnapTargets] = None
        self._hovered: Optional[int] = None

    @property
    def count(self) -> int:
        """@brief Number of committed measures.

        @return The count.
        """
        return len(self._measurements)

    @property
    def measurements(self) -> tuple:
        """@brief The committed measures, oldest first.

        @return A tuple of `Measurement`.
        """
        return tuple(self._measurements)

    @property
    def targets(self) -> Optional[SnapTargets]:
        """@brief The targets of the last `sync` (the geometry the measures belong to).

        @return The `SnapTargets`, or None before the first sync.
        """
        return self._targets

    @property
    def hovered(self) -> Optional[int]:
        """@brief Index of the highlighted (hovered) measure, or None.

        @return The index into `measurements`, or None.
        """
        return self._hovered

    def _notify(self) -> None:
        if self._on_changed is not None:
            self._on_changed(len(self._measurements))

    def label(self, m: Measurement) -> str:
        """@brief The translated label of a measure.

        @param m The measure.
        @return «12.70 mm» for a linear dimension, else `measurement_label`.
        """
        if m.axis in ("x", "y"):
            return self._translate(LABEL_SOURCE_LINEAR).format(distance=f"{m.distance_mm:.2f}")
        return measurement_label(m, self._translate)

    def _draw_measure(self, scene, m: Measurement, style: DimensionStyle, z: float) -> list:
        items = []
        if m.extensions:
            pen = _cosmetic_pen(style.color, 1.0)
            for x1, y1, x2, y2 in m.extensions:
                line = _line_item(x1, y1, x2, y2, pen)
                scene.addItem(line)
                items.append(line)
        items.extend(add_dimension_line(scene, m.start, m.end, self.label(m), style))
        for item in items:
            _mark_overlay(item, z)
        return items

    def _draw_one(self, scene, index: int) -> list:
        style = MEASURE_HIGHLIGHT_STYLE if index == self._hovered else MEASURE_STYLE
        return self._draw_measure(scene, self._measurements[index], style, MEASURE_Z)

    def add(self, scene, m: Measurement) -> None:
        """@brief Commit a measure and draw it.

        @param scene The scene to draw in (None: stored, drawn at the next `redraw`).
        @param m The measure.
        """
        self._measurements.append(m)
        self._items.append(self._draw_one(scene, len(self._measurements) - 1) if scene is not None else [])
        self._notify()

    def remove(self, scene, index: int) -> bool:
        """@brief Remove one measure.

        @param scene The scene the measures are drawn in.
        @param index Its index in `measurements`.
        @return True when it existed.
        """
        if not 0 <= index < len(self._measurements):
            return False
        self._measurements.pop(index)
        self._hovered = None
        self.redraw(scene)
        self._notify()
        return True

    def clear(self) -> bool:
        """@brief Remove every committed measure (and the transient markers).

        @return True when there was something to clear.
        """
        had = bool(self._measurements)
        for group in self._items:
            for item in group:
                _remove(item)
        self._items = []
        self._measurements = []
        self._hovered = None
        self.clear_transient()
        if had:
            self._notify()
        return had

    def forget_items(self) -> None:
        """@brief Drop the references to items the host's scene clear deleted (measures kept)."""
        self._items = []
        self._transient = []

    def redraw(self, scene) -> None:
        """@brief Draw every committed measure again on `scene` (after the host rebuilt it).

        @param scene The scene to draw in; None only removes the old items.
        """
        for group in self._items:
            for item in group:
                _remove(item)
        self._items = []
        if scene is None:
            return
        self._items = [self._draw_one(scene, i) for i in range(len(self._measurements))]

    def sync(self, scene, targets: SnapTargets) -> None:
        """@brief After a host redraw: keep the measures of the same geometry, drop the others.

        @param scene The redrawn scene.
        @param targets The targets of the redrawn geometry.
        @note CONTRACT CHANGED (user, 2026-11-02: a measure whose value is
            edited must stay and show the new value): was «a measure of other
            targets is dropped» (plan D-M4). A measure of the old geometry
            must never show the old number on the new one: each is taken
            again between the same targets where they are now
            (`reanchor_measurement`), so a measure on a moved pad follows it
            and shows its new value; a measure whose target is gone, or
            whose end is an aligned or free point, is dropped. The first
            sync only records the targets.
        """
        previous, self._targets = self._targets, targets
        if previous is not None and targets != previous and self._measurements:
            kept = [reanchor_measurement(m, previous, targets, self.units_per_mm) for m in self._measurements]
            kept = [m for m in kept if m is not None]
            dropped = len(kept) != len(self._measurements)
            self._measurements = kept
            self._hovered = None
            if dropped:
                self._notify()
        self.redraw(scene)

    def measurement_at(self, scene_point, scale: float = 1.0, view=None,
                       tolerance_px: float = REMOVE_TOLERANCE_PX) -> Optional[int]:
        """@brief The measure under a scene point (the overlay's own hit test).

        @param scene_point QPointF or (x, y) in scene units.
        @param scale View pixels per scene unit.
        @param view The view, to hit the screen-constant label boxes; None tests the lines only.
        @param tolerance_px Screen distance from a line (dimension or extension) that hits it.
        @return The index of the topmost (newest) measure hit, or None.
        """
        px, py = (scene_point.x(), scene_point.y()) if hasattr(scene_point, "x") else scene_point
        reach = tolerance_px / max(abs(float(scale)), 1e-9)
        device = view.mapFromScene(QPointF(px, py)) if view is not None else None
        for index in range(len(self._measurements) - 1, -1, -1):
            m = self._measurements[index]
            segments = ((*m.start, *m.end),) + tuple(m.extensions)
            if any(_segment_distance(px, py, seg) <= reach for seg in segments):
                return index
            if device is not None and index < len(self._items):
                for item in self._items[index]:
                    if (isinstance(item, QGraphicsRectItem) and not sip.isdeleted(item)
                            and item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations):
                        box = item.deviceTransform(view.viewportTransform()).mapRect(item.boundingRect())
                        if box.contains(QPointF(device)):
                            return index
        return None

    def set_hovered(self, scene, index: Optional[int]) -> bool:
        """@brief Highlight one measure (None: none).

        @param scene The scene.
        @param index The hovered measure, or None.
        @return True when the highlight changed (the measures were redrawn).
        """
        if index == self._hovered:
            return False
        self._hovered = index
        self.redraw(scene)
        return True

    def show_transient(self, scene, pending: Sequence, hover: Optional[MeasureAnchor],
                       tag: str = "", scale: float = 1.0) -> None:
        """@brief The hover feedback: highlighted target, guides, tag, collected points and live preview.

        @param scene The scene.
        @param pending The anchors collected so far (0, 1 or 2).
        @param hover The anchor a click at the pointer would take, or None.
        @param tag The translated tag shown beside the pointer («Pad 11 · centro»).
        @param scale View pixels per scene unit (guide overshoot).
        """
        self.clear_transient()
        if scene is None:
            return
        items = []
        for point in pending:
            items.append(screen_dot(scene, point.x, point.y, 7, MEASURE_COLOR, MEASURE_COLOR))
        if hover is not None:
            if hover.kind == KIND_CENTRE and hover.shape:
                items.append(_shape_highlight(scene, hover.shape))
            if hover.kind == KIND_EDGE and hover.edge:
                items.append(_line_item(*hover.edge, _cosmetic_pen(MEASURE_HIGHLIGHT_COLOR, 3.0)))
                scene.addItem(items[-1])
            over = 15.0 / max(abs(float(scale)), 1e-9)
            for orientation, tx, ty, _label, shape in hover.guides:
                if shape:
                    items.append(_shape_highlight(scene, shape))
                if orientation == "v":
                    y0, y1 = sorted((ty, hover.y))
                    guide = _line_item(tx, y0 - over, tx, y1 + over, _cosmetic_pen(MEASURE_HIGHLIGHT_COLOR, 1.0, True))
                else:
                    x0, x1 = sorted((tx, hover.x))
                    guide = _line_item(x0 - over, ty, x1 + over, ty, _cosmetic_pen(MEASURE_HIGHLIGHT_COLOR, 1.0, True))
                scene.addItem(guide)
                items.append(guide)
            if hover.kind != KIND_PLACE:
                items.append(anchor_marker(scene, hover, MEASURE_HIGHLIGHT_COLOR))
            if tag:
                items.append(_screen_tag(scene, tag, hover.x, hover.y))
            preview = None
            if len(pending) == 1 and hover != pending[0]:
                preview = measure_points((pending[0], hover), self.units_per_mm)
            elif len(pending) == 2:
                preview = measure_points((pending[0], pending[1], hover), self.units_per_mm)
            if preview is not None:
                items.extend(self._draw_measure(scene, preview, MEASURE_PREVIEW_STYLE, MEASURE_TRANSIENT_Z))
        for item in items:
            _mark_overlay(item, MEASURE_TRANSIENT_Z)
        self._transient = items

    def clear_transient(self) -> None:
        """@brief Remove the hover feedback, the collected points and the preview."""
        for item in self._transient:
            _remove(item)
        self._transient = []

    def items(self) -> list:
        """@brief The live overlay items (committed and transient), for tests and audits.

        @return A list of the items Qt has not deleted.
        """
        flat = [i for group in self._items for i in group] + self._transient
        return [i for i in flat if not sip.isdeleted(i)]


# ── Host glue ──────────────────────────────────────────────────────────────

class MeasureTool:
    """@brief What both hosts do with the measuring tool, written once.

    Interaction (plan docs/canvas-measure-plan.md §4): click a first target
    (pad or pin centre anywhere on its copper, corner, edge or a free
    point); move: the target under the pointer is highlighted with a tag,
    and the alignment guides catch the vertical / horizontal of other
    targets; the second click on an alignment (or a target whose ΔX or ΔY
    dominates) starts a LINEAR dimension, placed by a third click (live
    preview); any other second click finishes a true-distance or
    perpendicular measure. The tool spec is open-ended (count 0): the host
    callback finishes the collector at two points or three.

    A host forwards its `ClickToolHost` callbacks here when
    `spec.key == MEASURE_KEY`; its view calls `escape`, `context_menu`,
    `hover_measure` and its Delete shortcut calls `remove_hovered`; its
    redraw calls `forget_items` (before a scene clear) and `sync` (after).
    """

    def __init__(self, view, collector, *, units_per_mm: float, translate: Callable[[str], str] = str,
                 centre_tag: str = TAG_PAD_CENTRE, on_changed: Optional[Callable[[int], None]] = None,
                 on_edit: Optional[Callable[[Measurement], bool]] = None,
                 on_export: Optional[Callable[[Measurement], bool]] = None):
        """@brief Bind the tool to a view and its click collector.

        @param view The canvas QGraphicsView (its scale converts the screen reaches).
        @param collector The tab's `PointCollector`.
        @param units_per_mm Scene units per millimetre (footprint 20, symbol 10).
        @param translate Catalogue lookup for the Italian sources.
        @param centre_tag Italian source of a centre's tag (TAG_PAD_CENTRE or TAG_PIN_END).
        @param on_changed Called with the measure count when it changes.
        @param on_edit The host's «Modifica quota…» (edit the measured value):
            called with an editable measure (`measure_mover`), it asks the
            value and moves the geometry, returning True when it did. None:
            the tab's measures are read-only (the symbol editor today).
        @param on_export The host's «Esporta come quota» (export as a
            dimension): called with the measure, it adds a dimension drawing
            of the host's own and returns True when it did. None: the entry
            is not offered (the symbol has no dimensions, 2026-11-06).
        """
        self._view = view
        self._collector = collector
        self._translate = translate
        self._centre_tag = centre_tag
        self._on_edit = on_edit
        self._on_export = on_export
        self._targets = SnapTargets()
        self._index = GuideIndex(self._targets)
        self._hover: Optional[MeasureAnchor] = None
        self._escaping = False
        #: The tab's `ClickToolRouter`, set by the host: a cancel goes through
        #: `router.cancel_tool()` so a press recorded before it adds no point
        #: at its release. None falls back to the collector (tests).
        self.router = None
        self.overlay = MeasureOverlay(units_per_mm, translate, on_changed=on_changed)

    @property
    def armed(self) -> bool:
        """@brief True while the collector holds the measuring tool.

        @return Whether the measuring tool is armed.
        """
        spec = self._collector.spec if self._collector is not None else None
        return bool(self._collector is not None and self._collector.armed
                    and spec is not None and spec.key == MEASURE_KEY)

    @property
    def targets(self) -> SnapTargets:
        """@brief The targets snapping uses now.

        @return The `SnapTargets` of the last `sync`.
        """
        return self._targets

    def _scene(self):
        return None if self._view is None or sip.isdeleted(self._view) else self._view.scene()

    def scale(self) -> float:
        """@brief View pixels per scene unit at the current zoom.

        @return The view's horizontal scale (1 without a view).
        """
        return abs(self._view.transform().m11()) if self._view is not None else 1.0

    def tag(self, anchor: Optional[MeasureAnchor]) -> str:
        """@brief The translated hover tag of an anchor.

        @param anchor The anchor, or None.
        @return «Pad 11 · centro», «Spigolo», «Bordo», «Allineato: Pad 2»,
            «Punto libero»; "" for None or a placement point.
        """
        if anchor is None or anchor.kind == KIND_PLACE:
            return ""
        tx = self._translate
        if anchor.kind == KIND_CENTRE:
            return tx(self._centre_tag).format(label=anchor.label) if anchor.label else tx(TAG_CENTRE)
        if anchor.kind == KIND_CORNER:
            return tx(TAG_CORNER)
        if anchor.kind == KIND_EDGE:
            return tx(TAG_EDGE)
        if anchor.kind == KIND_ALIGNED:
            target = (tx(self._centre_tag).format(label=anchor.label) if anchor.label else tx(TAG_CORNER))
            return tx(TAG_ALIGNED).format(target=target)
        return tx(TAG_FREE)

    def snap(self, point, free: bool, points: Sequence = ()) -> list:
        """@brief `tool_snap` of the measuring tool.

        @param point Raw scene position (QPointF).
        @param free Shift: the raw point.
        @param points The anchors collected so far.
        @return One anchor: the first point by `snap_point`, the second by
            `snap_second` (alignment guides), the third (a linear
            dimension's placement) the raw pointer as KIND_PLACE.
        """
        x, y = point.x(), point.y()
        if len(points) >= 2:
            return [MeasureAnchor(float(x), float(y), KIND_PLACE)]
        if len(points) == 1:
            return [snap_second(x, y, self._targets, self._index, points[0], self.scale(), free=free)]
        return [snap_point(x, y, self._targets, self.scale(), free=free)]

    def _show(self, points) -> None:
        self.overlay.show_transient(self._scene(), list(points), self._hover, self.tag(self._hover), self.scale())

    def points_changed(self, points: list) -> None:
        """@brief `tool_points_changed`: show the collected points; finish a measure that is complete.

        @param points The collected anchors.
        @note Two anchors finish a true-distance or perpendicular measure;
            a linear dimension (second anchor with an axis) waits for its
            placement click, the third point.
        """
        if self.armed and (len(points) >= 3 or (len(points) == 2 and points[1].axis not in ("x", "y"))):
            self._collector.finish(True)
            return
        self._show(points)

    def preview_changed(self, points: list, preview: list) -> None:
        """@brief `tool_preview_changed`: move the highlight, guides, tag and live preview.

        @param points The collected anchors.
        @param preview The anchor a click would add, or [] when none.
        """
        self._hover = preview[0] if preview else None
        self._show(points)

    def finished(self, points: list, commit: bool) -> bool:
        """@brief `tool_finished` of the measuring tool.

        @param points The collected anchors.
        @param commit False for a cancel (Escape, hide, another tool, a project switch).
        @return True to stay armed: after a committed measure, and after an
            Escape that only dropped the pending points (`escape`); False
            otherwise (right click or Enter with fewer than two points, hide,
            a disarm by the host). A linear dimension finished by right
            click or Enter before its placement is placed through the first anchor.
        """
        if commit and len(points) >= 2:
            self.overlay.add(self._scene(), measure_points(points, self.overlay.units_per_mm))
            self._hover = None
            self._show([])
            return True
        stay = not commit and self._escaping
        if not stay:
            self._hover = None
        self._show([])
        return stay

    def disarmed(self) -> None:
        """@brief `tool_disarmed`: remove the transient markers (the measures stay)."""
        self._hover = None
        self.overlay.clear_transient()

    def escape(self, event, camera=None) -> bool:
        """@brief The Escape ladder, called by the view BEFORE the router's key handling.

        @param event The QKeyEvent.
        @param camera The view's `Camera`; a pan in progress leaves the key to the router.
        @return True when the key was consumed here: the pending points
            dropped (also a linear dimension waiting for its placement; the
            tool stays armed), or the measures cleared with no point pending
            (the tool stays armed). False otherwise: the router then disarms
            an idle tool, as it does for every click tool.
        """
        if event.key() != Qt.Key.Key_Escape or not self.armed:
            return False
        if camera is not None and camera.pan_active:
            return False
        if self._collector.points:
            self._drop_pending()
            event.accept()
            return True
        if self.overlay.count:
            self.overlay.clear()
            event.accept()
            return True
        return False

    def _drop_pending(self) -> None:
        """Drop the pending points and stay armed, through the router when there is one."""
        self._escaping = True
        try:
            if self.router is not None:
                self.router.cancel_tool()
            else:
                self._collector.cancel()
        finally:
            self._escaping = False

    def measurement_at_viewport(self, pos) -> Optional[int]:
        """@brief The measure under a viewport position, or None.

        @param pos Viewport position (QPoint).
        @return The index of the measure.
        """
        if self._view is None or not self.overlay.count:
            return None
        return self.overlay.measurement_at(self._view.mapToScene(pos), self.scale(), self._view)

    def hover_measure(self, pos) -> bool:
        """@brief The pointer moved (no button): highlight the measure under it.

        @param pos Viewport position, or None when the pointer left.
        @return True when a measure is hovered.
        """
        index = None if pos is None else self.measurement_at_viewport(pos)
        self.overlay.set_hovered(self._scene(), index)
        return index is not None

    def remove_hovered(self) -> bool:
        """@brief Delete key: remove the highlighted measure.

        @return True when a measure was removed (the key is then consumed).
        @note The measure is looked up under the cursor at the key press,
            not taken from the last hover: a wheel zoom or a pan may have
            moved the measures away from the pointer since.
        """
        index = self._index_under_cursor()
        self.overlay.set_hovered(self._scene(), index)
        return index is not None and self.overlay.remove(self._scene(), index)

    def _index_under_cursor(self) -> Optional[int]:
        """The measure under the mouse cursor now (after a zoom or pan the last hover may be stale).

        A view with the shared `canvas_hit.PointerTracker` (`view.pointer`,
        both tabs) is asked for the pointer, so one key press sees ONE
        pointer with E and the keyboard fallback (VER5 D); a bare view (unit
        tests of this module) reads the platform cursor.
        """
        if self._view is None or sip.isdeleted(self._view) or not self.overlay.count:
            return None
        tracker = getattr(self._view, "pointer", None)
        if tracker is not None and hasattr(tracker, "viewport_point"):
            pos = tracker.viewport_point()
            return None if pos is None else self.measurement_at_viewport(pos)
        from PyQt6.QtGui import QCursor
        viewport = self._view.viewport()
        pos = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(pos):
            return None
        return self.measurement_at_viewport(pos)

    def context_menu(self, pos, global_pos, parent=None) -> bool:
        """@brief Right press on a measure: the shared element menu with «Modifica quota…» (edit the value, when the host can) and «Rimuovi misura» (remove measure).

        @param pos Viewport position of the press.
        @param global_pos Screen position of the menu.
        @param parent Parent widget of the menu (the view when None).
        @return True when a measure was under the pointer (the press is
            consumed, chosen or not); False elsewhere (the tab's own menus),
            and always False while a click tool (this one or another) has
            collected points: the right press then belongs to the router,
            which finishes that tool (review 2026-10-31, finding 1).
        """
        if self._collector is not None and self._collector.armed and self._collector.points:
            return False
        index = self.measurement_at_viewport(pos)
        if index is None:
            return False
        self.overlay.set_hovered(self._scene(), index)
        entries = [canvas_menus.delete_entry(self._translate, REMOVE_MEASURE_SOURCE)]
        if self._on_edit is not None:
            # «Modifica quota…» is offered greyed out on a measure without a
            # pad: the user sees why nothing can move (a body width).
            entries.insert(0, canvas_menus.MenuEntry(
                EDIT_MEASURE, self._translate(EDIT_MEASURE_SOURCE),
                enabled=bool(measure_mover(self.overlay.measurements[index]))))
        if self._on_export is not None:
            entries.append(canvas_menus.MenuEntry(EXPORT_MEASURE, self._translate(EXPORT_MEASURE_SOURCE),
                                                  separator_before=True))
        menu, keys = canvas_menus.build_element_menu(
            parent if parent is not None else self._view, "", entries)
        chosen = canvas_menus.exec_element_menu(menu, keys, global_pos)
        if chosen == canvas_menus.DELETE:
            self.overlay.remove(self._scene(), index)
        elif chosen == EDIT_MEASURE:
            self.edit(index)
        elif chosen == EXPORT_MEASURE and self._on_export is not None:
            self._on_export(self.overlay.measurements[index])
        return True

    def edit(self, index: int) -> bool:
        """@brief «Modifica quota…»: let the host ask a new value for measure `index` and move a pad.

        @param index Index into the overlay's measures.
        @return True when the host moved the geometry; False without a host
            callback, for an unknown index or a measure without a pad.
        @note The measure follows the moved pad through the host's redraw
            (`MeasureOverlay.sync`), showing the new value.
        """
        measures = self.overlay.measurements
        if self._on_edit is None or not 0 <= index < len(measures) or not measure_mover(measures[index]):
            return False
        return bool(self._on_edit(measures[index]))

    def edit_at(self, pos) -> bool:
        """@brief Double click: edit the measure under a viewport position, if it is editable.

        @param pos Viewport position (QPoint).
        @return True when a measure was under the pointer (the double click
            is consumed, edited or not); False elsewhere and while a click
            tool has collected points (the double click finishes that tool).
        """
        if self._collector is not None and self._collector.armed and self._collector.points:
            return False
        index = self.measurement_at_viewport(pos)
        if index is None:
            return False
        self.edit(index)
        return True

    def clear(self) -> bool:
        """@brief «Cancella misure» (clear measures): remove every measure.

        @return True when there was something to clear.
        @note A measure in progress loses its collected points too, so the
            dots and the collector agree; the tool stays armed.
        """
        if self.armed and self._collector.points:
            self._drop_pending()
        return self.overlay.clear()

    def forget_items(self) -> None:
        """@brief The host is about to clear (or has cleared) its scene: drop item references."""
        self.overlay.forget_items()

    def sync(self, targets: Optional[SnapTargets]) -> None:
        """@brief After a host redraw: new targets, measures kept or dropped, items drawn again.

        @param targets The geometry just drawn; None when the draft is
            transiently INVALID (nothing could be built): the targets, the
            measures and a pending point are all kept, as the wire and shape
            tools keep their points, and come back with the next valid
            geometry if it has the same targets (review 2026-10-31, decision a).
        @note When the targets changed, the pending points are dropped too
            (the tool stays armed): they were snapped on the old geometry.
            The guide index is rebuilt only then.
        """
        if targets is None:
            scene = self._scene()
            self.overlay.redraw(scene)
            if self.armed:
                self._show(self._collector.points)
            return
        previous = self.overlay.targets
        if targets != self._targets:
            self._index = GuideIndex(targets)
        self._targets = targets
        scene = self._scene()
        self.overlay.sync(scene, targets)
        if previous is not None and targets != previous and self.armed and self._collector.points:
            self._drop_pending()
        if self.armed:
            self._show(self._collector.points)
        else:
            self.overlay.clear_transient()
