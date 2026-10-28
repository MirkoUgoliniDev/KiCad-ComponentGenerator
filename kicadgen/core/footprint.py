"""@brief KiCad footprint (.kicad_mod) data model and writer.

Provides high-level classes (Pad, FpLine, FpRect, FpCircle, FpText) that
convert themselves into S-expressions through core.sexpr, and the
Footprint class that assembles them into the complete file format
accepted by KiCad (pcbnew) version 10.x.

Minimal example:

    from kicadgen.core.footprint import Footprint, Pad

    fp = Footprint(name="MY_CONN", description="Test connector")
    fp.add_pad(Pad(number="1", pad_type="thru_hole", shape="circle",
                    at=(0, 0), size=(1.7, 1.7), drill=1.0,
                    layers=["*.Cu", "*.Mask"]))
    fp.save("MY_CONN.pretty/MY_CONN.kicad_mod")
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

from .drawing_transform import bounds_centre, mirror_points_x, rotate_points_90
from .kicad_text import kicad_centre_from_corner, kicad_centre_to_corner
from .sexpr import YES, SExpr, Symbol, bare, sx
from ..services.messages import tx

KICAD_VERSION = 20260206
GENERATOR_VERSION = "10.0"

#: Language of the text the generators write INTO KiCad files (footprint
#: `descr`, fixed notes). Deliberately fixed and independent of the interface
#: language: a library is shared and versioned, so its content must not change
#: with the GUI language of whoever regenerated it, and the pinned footprint
#: digests of the regression tests stay reproducible. Italian because every
#: default description and note of the generators was written in it. These
#: strings never go through `tx()`; messages shown to the user do.
FOOTPRINT_TEXT_LANGUAGE = "it"

Point = Tuple[float, float]

PAD_TYPES = {"thru_hole", "smd", "np_thru_hole", "connect"}
# This writer has no custom-pad anchor/primitives representation. A
# trapezoid is accepted only together with its `rect_delta` (the two side
# lengths KiCad subtracts/adds to the rectangle); without it KiCad draws a
# plain rectangle, which is what a silent writer produced before (review
# 2026-10-27, G7), so `Pad` refuses the shape without the delta.
PAD_SHAPES = {"circle", "rect", "oval", "roundrect", "trapezoid"}


def _uuid() -> str:
    return str(uuid.uuid4())


def _at_node(at: Point, rotation: float = 0.0) -> SExpr:
    x, y = at
    if rotation:
        return sx("at", x, y, rotation)
    return sx("at", x, y)


def _stroke_node(width: float) -> SExpr:
    return sx("stroke", sx("width", width), sx("type", Symbol("solid")))


#: Text size (mm) of the Reference/Value properties the generator writes;
#: the footprint preview draws them at this size (capitals this tall).
PROPERTY_TEXT_SIZE = 1.0
#: Stroke thickness (mm) of the Reference/Value properties the generator
#: writes; with the size it fixes where KiCad centres them
#: (`property_corner_offset`).
PROPERTY_TEXT_THICKNESS = 0.15
#: Text of the Reference property of a library footprint, as KiCad shows it.
REFERENCE_TEXT = "REF**"


def property_corner_offset(text: str) -> Point:
    """@brief Offset, mm, from a centred Reference/Value property's anchor to the bottom-left anchor KiCad draws it at.

    @param text The text KiCad shows for the property («REF**», the
        footprint name for the Value).
    @return (dx, dy) mm (footprint axes, y down): minus half the width KiCad
        centres the text on, and 0.585 x size downwards, for the size and
        thickness `Footprint` writes (`PROPERTY_TEXT_SIZE`,
        `PROPERTY_TEXT_THICKNESS`).
    @note KiCad's stroke-font geometry measured with kicad-cli
        (`kicadgen.core.kicad_text`), not any desktop font: a text moved by
        this offset and written with ``(justify left bottom)`` stays where
        KiCad drew it centred, within 0.02 mm. The preview draws texts at
        KiCad's width (`kicadgen.gui.kicad_text_item`, 2026-10-16), so on
        screen it stays put too, apart from first/last glyph side bearings.
    """
    return kicad_centre_to_corner(text, PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS, y_down=True)


def _text_effects(size: float = 1.0, thickness: float = 0.15,
                  justify: Optional[Tuple[str, ...]] = None) -> SExpr:
    """Text ``effects`` node; ``justify`` adds ``(justify ...)`` after the font, as KiCad writes it."""
    node = sx("effects", sx("font", sx("size", size, size), sx("thickness", thickness)))
    if justify:
        node.add(sx("justify", *(Symbol(word) for word in justify)))
    return node


@dataclass
class Pad:
    """@brief Footprint pad: number, type, shape, position, size, layers and optional drill.

    - number: pin name/number (e.g. "1", "A2", "" for mechanical pads without a net)
    - pad_type: "thru_hole" | "smd" | "np_thru_hole"
    - shape: "circle" | "rect" | "oval" | "roundrect" | "trapezoid"
    - at: (x, y) position in mm relative to the footprint origin
    - size: (width, height) in mm
    - layers: list of KiCad layers, e.g. ["*.Cu", "*.Mask"] for THT or ["F.Cu", "F.Paste", "F.Mask"] for SMD
    - drill: hole diameter in mm (required for thru_hole/np_thru_hole)
    - roundrect_rratio: rounding ratio for shape="roundrect" (0..0.5)
    - rect_delta: (dx, dy) in mm, KiCad's `rect_delta` of a trapezoid pad:
      exactly one of the two is non-zero and its magnitude is below the
      corresponding pad size, as KiCad's pad editor requires. Required for
      shape="trapezoid" and refused for every other shape.
    """

    number: str
    pad_type: str
    shape: str
    at: Point
    size: Tuple[float, float]
    layers: Sequence[str]
    rotation: float = 0.0
    drill: Optional[float] = None
    roundrect_rratio: Optional[float] = None
    rect_delta: Optional[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        if self.pad_type not in PAD_TYPES:
            raise ValueError(tx("pad_type non valido: {pad_type!r} (attesi: {PAD_TYPES})",
                                pad_type=self.pad_type,
                                PAD_TYPES=PAD_TYPES))
        if self.shape not in PAD_SHAPES:
            raise ValueError(tx("shape non valida: {shape!r} (attese: {PAD_SHAPES})",
                                shape=self.shape,
                                PAD_SHAPES=PAD_SHAPES))
        if self.pad_type in ("thru_hole", "np_thru_hole") and self.drill is None:
            raise ValueError(tx("pad {number!r}: drill obbligatorio per pad_type={pad_type!r}",
                                number=self.number,
                                pad_type=self.pad_type))
        self._validate_rect_delta()

    def _validate_rect_delta(self) -> None:
        """Refuse a trapezoid without its delta and a delta on another shape.

        KiCad defines a trapezoid pad by `(rect_delta dx dy)`: one of the two
        is zero, the other shortens one side and lengthens the opposite one
        by half its value, so its magnitude must stay below the pad size on
        that axis or the sides would cross. A trapezoid written without the
        delta is loaded by KiCad as a plain rectangle, silently.
        """
        if self.shape == "trapezoid":
            if self.rect_delta is None:
                raise ValueError(tx("pad {number!r}: un pad trapezoidale richiede rect_delta",
                                    number=self.number))
            try:
                dx, dy = (float(v) for v in self.rect_delta)
            except (TypeError, ValueError) as exc:
                raise ValueError(tx("pad {number!r}: rect_delta non valido", number=self.number)) from exc
            width, height = (float(v) for v in self.size)
            if not (math.isfinite(dx) and math.isfinite(dy)) or (dx != 0.0) == (dy != 0.0)                     or abs(dx) >= width or abs(dy) >= height:
                raise ValueError(tx("pad {number!r}: rect_delta non valido", number=self.number))
            self.rect_delta = (dx, dy)
        elif self.rect_delta is not None:
            raise ValueError(tx("pad {number!r}: rect_delta ammesso solo per la forma trapezoid",
                                number=self.number))

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this pad to a KiCad `(pad ...)` S-expression.

        @return The pad node; `at` is the pad position (for a drilled pad, the
            hole position: KiCad places the copper at `at` plus the drill offset).
        """
        node = sx("pad", self.number, Symbol(self.pad_type), Symbol(self.shape))
        node.add(_at_node(self.at, self.rotation))
        node.add(sx("size", *self.size))
        if self.drill is not None:
            node.add(sx("drill", self.drill))
        node.add(sx("layers", *self.layers))
        if self.shape == "roundrect" and self.roundrect_rratio is not None:
            node.add(sx("roundrect_rratio", self.roundrect_rratio))
        if self.shape == "trapezoid":
            # Always present for a trapezoid (`_validate_rect_delta`): it is
            # the geometry of the shape, not an option.
            node.add(sx("rect_delta", *self.rect_delta))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpLine:
    """@brief Graphic line segment on a footprint layer.

    Used for the silkscreen, fab and courtyard outlines, among others.
    """

    start: Point
    end: Point
    layer: str
    width: float = 0.12

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this line graphic to a KiCad S-expression.

        @return The `fp_line` node with start, end, stroke, layer and a fresh uuid.
        """
        node = sx("fp_line", sx("start", *self.start), sx("end", *self.end))
        node.add(_stroke_node(self.width))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpRect:
    """@brief Graphic rectangle on a footprint layer, optionally filled.

    Graphic rectangle (e.g. a silkscreen outline).
    """

    start: Point
    end: Point
    layer: str
    width: float = 0.12
    fill: bool = False

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this rectangle graphic to a KiCad S-expression.

        @return The `fp_rect` node with corners, stroke, fill, layer and a fresh uuid.
        """
        node = sx("fp_rect", sx("start", *self.start), sx("end", *self.end))
        node.add(_stroke_node(self.width))
        node.add(sx("fill", bare(self.fill)))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpCircle:
    """@brief Graphic circle on a footprint layer, optionally filled.

    Graphic circle (e.g. the pin 1 marker).
    """

    center: Point
    radius: float
    layer: str
    width: float = 0.12
    fill: bool = False

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this circle graphic to a KiCad S-expression.

        @return The `fp_circle` node (centre plus a point on the circumference,
            as KiCad writes it) with stroke, fill, layer and a fresh uuid.
        """
        end = (self.center[0] + self.radius, self.center[1])
        node = sx("fp_circle", sx("center", *self.center), sx("end", *end))
        node.add(_stroke_node(self.width))
        node.add(sx("fill", bare(self.fill)))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpArc:
    """@brief Graphic circular arc on a footprint layer, through three points.

    Graphic arc (fp_arc): start, mid point and end, the way KiCad writes it.
    """

    start: Point
    mid: Point
    end: Point
    layer: str
    width: float = 0.12

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this arc graphic to a KiCad S-expression.

        @return The `fp_arc` node with start, mid, end, stroke, layer and a fresh uuid.
        """
        node = sx("fp_arc", sx("start", *self.start), sx("mid", *self.mid),
                  sx("end", *self.end))
        node.add(_stroke_node(self.width))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpText:
    """@brief Graphic text item on a footprint layer.

    Graphic text (used for extra references, not the Reference/Value properties).

    @param justify KiCad justification words written as
        ``(justify <words>)`` inside ``effects`` (for example
        ``("left", "bottom")``: `at` is then the bottom-left corner of the
        text box); None (default) writes no justify node, so KiCad centres
        the text on `at`, and the output stays byte-identical to the one
        written before this option existed.
    @param angle Text rotation, degrees counter-clockwise as KiCad counts
        them (90 reads bottom-to-top); written as the third value of
        ``(at x y angle)`` only when non-zero, so a text without rotation
        is written byte-identical to the output before this option existed.
    """

    kind: str  # "user" | "reference" | "value"
    text: str
    at: Point
    layer: str
    size: float = 1.0
    thickness: float = 0.15
    justify: Optional[Tuple[str, ...]] = None
    angle: float = 0

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this text graphic to a KiCad S-expression.

        @return The `fp_text` node with kind, text, position (with the angle
            when non-zero), layer, a fresh uuid and the font effects (size,
            thickness, optional justify).
        """
        node = sx("fp_text", Symbol(self.kind), self.text)
        node.add(_at_node(self.at, self.angle))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        node.add(_text_effects(self.size, self.thickness, self.justify))
        return node


#: Largest and smallest height of the ``${REFERENCE}`` text on F.Fab, mm.
#: 1.0 mm is the KLC default for fabrication text (and what the castellated
#: generator always wrote); 0.5 mm is the floor the text is reduced to so it
#: stays legible on parts whose body is too small for 1.0 mm.
FAB_REFERENCE_MAX_MM = 1.0
FAB_REFERENCE_MIN_MM = 0.5
#: Share of the fabrication outline's smaller side the text may fill.
FAB_REFERENCE_FILL = 0.8


def fab_reference_text(center: Point, outline_size: Optional[Point] = None) -> "FpText":
    """@brief Build the ``${REFERENCE}`` text that KLC F5.2 asks for on F.Fab.

    @param center Anchor (text centre) in footprint mm: the centre of the
        fabrication outline, or of the pad field when there is none.
    @param outline_size (width, height) mm of that outline; None keeps the
        full FAB_REFERENCE_MAX_MM height.
    @return User text on F.Fab with height min(MAX, max(MIN, FILL x smaller
        side)) and stroke 15 % of the height (KLC thickness ratio).
    @throws ValueError When `center` is not finite or lies beyond
        KICAD_COORD_LIMIT_MM (KiCad cannot represent it). The tighter
        TEXT_POSITION_LIMIT_MM bounds the texts the USER moves, not generated
        geometry: a package far from the origin must still build.
    @note The silkscreen Reference is hidden under a mounted part; the F.Fab
        copy stays readable in assembly drawings, so every generator emits it.
    """
    x, y = (float(center[0]), float(center[1]))
    if not (math.isfinite(x) and math.isfinite(y)) or max(abs(x), abs(y)) > KICAD_COORD_LIMIT_MM:
        raise ValueError(f"fabrication reference: invalid position {center!r}")
    size = FAB_REFERENCE_MAX_MM
    if outline_size is not None:
        side = min(abs(float(outline_size[0])), abs(float(outline_size[1])))
        if math.isfinite(side):
            size = min(FAB_REFERENCE_MAX_MM, max(FAB_REFERENCE_MIN_MM, round(side * FAB_REFERENCE_FILL, 2)))
    return FpText(kind="user", text="${REFERENCE}", at=(x + 0.0, y + 0.0), layer="F.Fab",
                  size=size, thickness=round(size * 0.15, 4))


#: Visible footprint texts whose position the user may choose (the KiCad
#: properties drawn on the board), in the order they are written.
TEXT_KEYS = ("Reference", "Value")
#: Fallback positions for a footprint with no geometry at all (no pad and no
#: outline, see default_text_positions): the fixed places every footprint
#: used before decision D1 (user, 2026-10-14). A footprint with geometry
#: gets its texts above and below its envelope instead.
DEFAULT_TEXT_POSITIONS = {"Reference": (0.0, -2.0), "Value": (0.0, 2.0)}
#: Grid the default text anchors are snapped to, mm: the 2D preview's
#: default drawing grid (1 mm main step / fine divisor 10), anchored like it
#: on the first numbered pad, so a default text sits on a node the user can
#: also drag it back to.
TEXT_GRID_MM = 0.1
#: Layers whose graphics make up the footprint envelope for the default text
#: positions, with the pads: courtyard (keep-out), fabrication outline (body)
#: and silkscreen (pin-1 marker, outline pieces), so the texts clear them all.
TEXT_ENVELOPE_LAYERS = ("F.CrtYd", "F.Fab", "F.SilkS")
#: Bound of a chosen coordinate, mm: nothing on a footprint is farther.
TEXT_POSITION_LIMIT_MM = 500.0


def validate_text_positions(raw) -> dict:
    """@brief Keep the well-formed entries of a saved {key: [x, y]} mapping of text positions.

    @param raw Whatever the project file holds under `footprint_texts`.
    @return {key: (x, y)} for the keys of `TEXT_KEYS` whose value is a pair
        of finite numbers within `TEXT_POSITION_LIMIT_MM`; anything else is
        left out, like a malformed pad override.
    """
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        if key not in TEXT_KEYS or not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   and abs(v) <= TEXT_POSITION_LIMIT_MM for v in value):
            continue
        result[key] = (round(float(value[0]), 4), round(float(value[1]), 4))
    return result


#: Layers a user drawing of the footprint preview may live on: printed
#: silkscreen, assembly documentation, free comments. Copper and courtyard
#: are deliberately excluded: they carry electrical and DRC meaning the
#: preview must not let a free sketch change.
USER_DRAW_LAYERS = ("F.SilkS", "F.Fab", "Cmts.User")
#: Drawing kinds a record may declare, with the exact point count each needs
#: (0 means "two or more", the open polyline of the line tool).
USER_DRAW_KINDS = {"line": 0, "rectangle": 2, "circle": 2, "arc": 3, "text": 1}
#: Stroke width, mm, of a user drawing (KiCad's default silkscreen line).
USER_DRAW_WIDTH_MM = 0.12
#: Default text height of a user text, mm (KiCad's footprint text default).
USER_DRAW_TEXT_MM = 1.0
#: Value of a text record's ``anchor`` field when its point is the
#: BOTTOM-LEFT corner of the text (user, 2026-10-14): left edge and baseline
#: (descenders below, as KiCad); the text grows right and up from it and the
#: .kicad_mod writes ``(justify left bottom)``. A
#: text record without the field is a record saved before that date, whose
#: point is the CENTRE of the text (KiCad's default justification).
USER_TEXT_ANCHOR = "left_bottom"
#: KiCad justification written for a text record anchored at `USER_TEXT_ANCHOR`.
USER_TEXT_JUSTIFY = ("left", "bottom")
#: Angles, degrees, a text record's optional ``angle`` field may hold: a
#: text is horizontal or turned a quarter turn to read bottom-to-top, as the
#: symbol editor's texts (rotation taken modulo 180; KiCad's "keep upright"
#: shows 180/270 as 0/90 anyway, plan decision C-M2). No field means 0.
USER_TEXT_ANGLES = (0, 90)
#: Field of the saved Reference/Value position mapping (``footprint_texts``
#: of the project, ``texts`` of the footprint recovery file) that marks its
#: points as BOTTOM-LEFT anchors (value `USER_TEXT_ANCHOR`, user,
#: 2026-10-16). A mapping without it was saved before that date and holds
#: the CENTRE of each text; it is converted once on load
#: (`migrate_centred_positions`). `validate_text_positions` ignores the
#: field, so a program older than the mark still reads the mapping.
TEXT_POSITIONS_ANCHOR_FIELD = "anchor"


#: Largest coordinate KiCad can hold, mm: its internal unit is the
#: nanometre stored in a signed 32-bit integer, so +-2147483647 nm. A point
#: (or an arc centre KiCad derives) beyond it cannot be represented at all.
KICAD_COORD_LIMIT_MM = 2147.483647


def _arc_centre(start: Point, mid: Point, end: Point) -> Optional[Point]:
    """@brief Centre, mm, of the circle through three points, or None when they are collinear.

    @param start First point of the arc, mm.
    @param mid Point the arc passes through, mm.
    @param end Last point of the arc, mm.
    @return (x, y) of the circumcentre in mm; None when the determinant of
        the three points is exactly zero (no circle passes through them).
    """
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    det = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if det == 0.0:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / det
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / det
    return ux, uy


def _arc_circumradius(start: Point, mid: Point, end: Point) -> Optional[float]:
    """@brief Radius, mm, of the circle through three points, or None when they are collinear.

    @param start First point of the arc, mm.
    @param mid Point the arc passes through, mm.
    @param end Last point of the arc, mm.
    @return The circumradius in mm; None when no circle passes through them.
    """
    centre = _arc_centre(start, mid, end)
    if centre is None:
        return None
    return math.hypot(start[0] - centre[0], start[1] - centre[1])


def arc_centre_representable(start: Point, mid: Point, end: Point) -> bool:
    """@brief Tell whether KiCad can hold the centre of the arc through three points.

    @param start First point of the arc, mm.
    @param mid Point the arc passes through, mm.
    @param end Last point of the arc, mm.
    @return True when the circumcentre exists (non-zero determinant) and both
        its coordinates are within `KICAD_COORD_LIMIT_MM`.
    @note This is the file format's own limit, not a plausibility bound: a
        very flat but real arc (0.1 mm of bow over a 20 mm chord, radius
        about 500 mm) is kept; collinear points, and nearly collinear ones
        whose centre lands outside KiCad's coordinate range, are refused.
    """
    centre = _arc_centre(start, mid, end)
    return centre is not None and all(
        math.isfinite(v) and abs(v) <= KICAD_COORD_LIMIT_MM for v in centre)


def validate_footprint_drawings(raw) -> list:
    """@brief Keep the well-formed records of saved footprint user drawings.

    @param raw Whatever the project file holds under `footprint_drawings`.
    @return List of {kind, points, layer, width[, text, size[, anchor][, angle]]}
        records whose kind and layer are known, whose points are pairs of
        finite numbers within `TEXT_POSITION_LIMIT_MM` in the count the kind
        requires, and whose text (text kind only) is one non-blank line of
        positive size; a text keeps ``anchor`` only when it is
        `USER_TEXT_ANCHOR` (point = bottom-left corner), no ``anchor`` means a
        legacy centred text, any other anchor value drops the record;
        a text's ``angle`` must be a finite number that is a whole multiple
        of 90; it is taken modulo 180 (180 -> 0, 270 and -90 -> 90, as the
        symbol editor's texts and KiCad's "keep upright") and stored as the
        int 90, or as no field for 0 (the default); any other angle value
        (45, a string, a bool, NaN) drops the record;
        ``angle`` on a shape is ignored;
        an arc must also have a circumcentre KiCad can represent
        (`arc_centre_representable`: collinear points, or nearly collinear
        ones whose centre falls outside `KICAD_COORD_LIMIT_MM`, are left
        out); anything else is left out, like a malformed pad override.
    @note Same contract style as `validate_text_positions`: loading never
        raises, it silently drops what it cannot trust.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    result = []
    for record in raw:
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        layer = record.get("layer")
        points = record.get("points")
        if kind not in USER_DRAW_KINDS or layer not in USER_DRAW_LAYERS:
            continue
        if not isinstance(points, (list, tuple)):
            continue
        count = USER_DRAW_KINDS[kind]
        if count and len(points) != count or not count and len(points) < 2:
            continue
        clean_points = []
        for point in points:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                               and math.isfinite(v) and abs(v) <= TEXT_POSITION_LIMIT_MM
                               for v in point)):
                break
            clean_points.append((round(float(point[0]), 4), round(float(point[1]), 4)))
        if len(clean_points) != len(points):
            continue
        width = record.get("width", USER_DRAW_WIDTH_MM)
        if (isinstance(width, bool) or not isinstance(width, (int, float))
                or not math.isfinite(width) or not 0 < width <= 10):
            continue
        clean = {"kind": kind, "layer": layer, "width": round(float(width), 4),
                 "points": [list(p) for p in clean_points]}
        if kind == "text":
            text = record.get("text")
            size = record.get("size", USER_DRAW_TEXT_MM)
            if (not isinstance(text, str) or not text.strip()
                    or "\n" in text or "\r" in text):
                continue
            if (isinstance(size, bool) or not isinstance(size, (int, float))
                    or not math.isfinite(size) or not 0 < size <= 50):
                continue
            anchor = record.get("anchor")
            if anchor is not None and anchor != USER_TEXT_ANCHOR:
                continue
            angle = record.get("angle")
            if angle is not None:
                if (isinstance(angle, bool) or not isinstance(angle, (int, float))
                        or not math.isfinite(angle) or angle % 90):
                    continue
                # A hand-edited 180/270 keeps the user's text instead of
                # dropping it: the same text, shown upright (plan C-M2).
                angle = int(angle) % 180
                if angle not in USER_TEXT_ANGLES:
                    continue
            clean["text"] = text.strip()
            clean["size"] = round(float(size), 4)
            if anchor is not None:
                clean["anchor"] = anchor
            if angle:
                # 0 is the default: not stored, so a text turned back and
                # forth is the same record as one never turned.
                clean["angle"] = int(angle)
        elif kind == "circle":
            # Second point on the rim: a zero radius draws nothing.
            if clean_points[0] == clean_points[1]:
                continue
        elif kind == "rectangle":
            if (clean_points[0][0] == clean_points[1][0]
                    or clean_points[0][1] == clean_points[1][1]):
                continue
        elif kind == "arc":
            if len(set(clean_points)) < 3:
                continue
            # KiCad derives the arc from its circumcentre: collinear points
            # have none, and a centre outside its 32-bit nanometre range
            # cannot be stored. A large radius alone is a legitimate flat arc.
            if not arc_centre_representable(*clean_points):
                continue
        result.append(clean)
    return result


def drawing_graphics(records) -> list:
    """@brief Convert validated user drawing records into footprint graphics.

    @param records Records in the shape `validate_footprint_drawings` returns
        (the caller validates; a malformed record here is a programming error).
    @return Graphic items (FpLine/FpRect/FpCircle/FpArc/FpText) ready for
        `Footprint.add_graphic`, in record order; a line record yields one
        FpLine per segment. Points are absolute footprint mm, drawn as-is.
    @note A text anchored at `USER_TEXT_ANCHOR` is written with
        ``(justify left bottom)`` at its point; a legacy text (no anchor)
        keeps KiCad's default centred justification, exactly as before.
    @note A text's optional ``angle`` (90) is written as ``(at x y 90)``;
        a text without it, or with 0, is written exactly as before.
    """
    items = []
    for record in records:
        kind, layer = record["kind"], record["layer"]
        width = record.get("width", USER_DRAW_WIDTH_MM)
        points = [tuple(p) for p in record["points"]]
        if kind == "line":
            items.extend(FpLine(start=a, end=b, layer=layer, width=width)
                         for a, b in zip(points, points[1:]))
        elif kind == "rectangle":
            items.append(FpRect(start=points[0], end=points[1], layer=layer, width=width))
        elif kind == "circle":
            radius = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
            items.append(FpCircle(center=points[0], radius=round(radius, 4),
                                  layer=layer, width=width))
        elif kind == "arc":
            start, mid, end = points
            items.append(FpArc(start=start, mid=mid, end=end, layer=layer, width=width))
        elif kind == "text":
            justify = USER_TEXT_JUSTIFY if record.get("anchor") == USER_TEXT_ANCHOR else None
            items.append(FpText(kind="user", text=record["text"], at=points[0],
                                layer=layer, size=record.get("size", USER_DRAW_TEXT_MM),
                                justify=justify, angle=record.get("angle", 0)))
    return items


def migrate_centred_text(record: dict, corner_offset: Point) -> dict:
    """@brief Move a legacy centred text record's point to the bottom-left corner of its drawn box.

    @param record A validated drawing record (any kind).
    @param corner_offset (dx, dy) mm from the record's centred point to the
        left edge and baseline of the text the preview draws for it (dx
        usually negative, dy positive: Y grows downwards). The caller
        measures it with the preview's own text metrics.
    @return A new record: for a text without ``anchor``, its point moved by
        `corner_offset` (rounded to 0.1 um) and ``anchor`` set to
        `USER_TEXT_ANCHOR`; the record unchanged (a copy) for any other kind,
        for a text already anchored (idempotent), or when the moved point
        would leave `TEXT_POSITION_LIMIT_MM` (it then stays a valid legacy
        centred text instead of being dropped).
    @note The conversion keeps the text where the user SEES it in the
        preview. KiCad's stroke font is not the preview's font: for a
        MIGRATED text the box KiCad draws may differ from the old centred
        placement by a fraction of a character. New texts are exact: a
        bottom-left anchor does not depend on the text width.
    """
    clean = dict(record)
    clean["points"] = [list(p) for p in record.get("points", ())]
    if record.get("kind") != "text" or record.get("anchor") is not None:
        return clean
    (x, y), (dx, dy) = record["points"][0], corner_offset
    moved = (round(float(x) + float(dx), 4), round(float(y) + float(dy), 4))
    if not all(math.isfinite(v) and abs(v) <= TEXT_POSITION_LIMIT_MM for v in moved):
        return clean
    clean["points"] = [list(moved)]
    clean["anchor"] = USER_TEXT_ANCHOR
    return clean


def drawing_bounds(record: dict) -> Tuple[float, float, float, float]:
    """@brief Axis-aligned bounding box, mm, of what a shape record draws.

    @param record A validated shape record (line, rectangle, circle or arc).
    @return (x_min, y_min, x_max, y_max) mm, Y down: a line's and a
        rectangle's points; a circle's centre +- radius; an arc's start and
        end plus every quarter point (0, 90, 180, 270 degrees on its circle)
        the arc passes through, so the box is the drawn arc's, not its three
        stored points'.
    @note A text has no drawn box here: its geometry depends on the font
        (its anchor is its stored point).
    """
    points = [(float(x), float(y)) for x, y in record["points"]]
    kind = record["kind"]
    if kind == "circle":
        (cx, cy), (rx, ry) = points
        radius = math.hypot(rx - cx, ry - cy)
        points = [(cx - radius, cy - radius), (cx + radius, cy + radius)]
    elif kind == "arc":
        start, mid, end = points
        centre = _arc_centre(start, mid, end)
        if centre is not None:
            cx, cy = centre
            radius = math.hypot(start[0] - cx, start[1] - cy)
            turn = 2.0 * math.pi
            a_start, a_mid, a_end = (math.atan2(y - cy, x - cx) % turn for x, y in (start, mid, end))
            # The arc runs from start to end the way that passes through mid.
            if (a_mid - a_start) % turn <= (a_end - a_start) % turn:
                low, sweep = a_start, (a_end - a_start) % turn
            else:
                low, sweep = a_end, (a_start - a_end) % turn
            points = [start, end]
            for quarter in range(4):
                angle = quarter * math.pi / 2.0
                if (angle - low) % turn <= sweep:
                    points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def drawing_centre(record: dict) -> Point:
    """@brief Centre, mm, a drawing record turns and mirrors about.

    @param record A validated drawing record.
    @return A shape's `drawing_bounds` centre (computed by the shared
        `drawing_transform.bounds_centre`); a text's anchor point.
    """
    if record["kind"] == "text":
        x, y = record["points"][0]
        return (float(x), float(y))
    x0, y0, x1, y1 = drawing_bounds(record)
    return bounds_centre([(x0, y0), (x1, y1)])


def turn_drawing(record: dict, counter_clockwise_on_screen: bool) -> dict:
    """@brief A copy of a drawing record turned a quarter turn, as the user sees it.

    @param record A validated drawing record.
    @param counter_clockwise_on_screen True for «antiorario» on the Y-down screen.
    @return A new record. A shape: every point turned about the centre of its
        `drawing_bounds` (`drawing_transform.rotate_points_90`), so a
        rectangle stays axis aligned, a circle keeps its centre and an arc
        keeps its start/mid/end order. A text: its anchor stays and its
        ``angle`` toggles between 0 (no field) and 90, because a text is
        horizontal or vertical (`USER_TEXT_ANGLES`, taken modulo 180 as the
        symbol editor's texts); both senses give the same toggle.
    @note The caller validates the result (`validate_footprint_drawings`): a
        turned shape may leave `TEXT_POSITION_LIMIT_MM`.
    """
    clean = dict(record)
    clean["points"] = [list(p) for p in record["points"]]
    if record["kind"] == "text":
        if record.get("angle", 0):
            clean.pop("angle", None)
        else:
            clean["angle"] = 90
        return clean
    clean["points"] = rotate_points_90(record["points"], drawing_centre(record), counter_clockwise_on_screen)
    return clean


def mirror_drawing(record: dict) -> dict:
    """@brief A copy of a shape record mirrored left-right about its bounding-box centre.

    @param record A validated drawing record.
    @return A new record: a shape's points mirrored about the vertical line
        through the centre of its `drawing_bounds`
        (`drawing_transform.mirror_points_x`; an arc keeps its start/mid/end
        order, the mirrored three points define the mirrored arc); a text is
        returned unchanged (a copy): KiCad never mirrors text glyphs and its
        anchor is the mirror's own axis point (plan decision C-M1).
    """
    clean = dict(record)
    clean["points"] = [list(p) for p in record["points"]]
    if record["kind"] == "text":
        return clean
    clean["points"] = mirror_points_x(record["points"], drawing_centre(record)[0])
    return clean


def translate_drawing(record: dict, dx: float, dy: float) -> dict:
    """@brief A copy of a drawing record moved by (dx, dy) mm.

    @param record A drawing record (validated or not; only ``points`` is read).
    @param dx Shift along X, mm.
    @param dy Shift along Y (down), mm.
    @return A new record whose every point is moved and rounded to 0.1 um.
    """
    clean = dict(record)
    clean["points"] = [[round(float(x) + dx, 4) + 0.0, round(float(y) + dy, 4) + 0.0]
                       for x, y in record["points"]]
    return clean


def resize_drawing(record: dict, *, width: Optional[float] = None, height: Optional[float] = None,
                   radius: Optional[float] = None) -> dict:
    """@brief A copy of a rectangle or circle record with a new size, its centre kept.

    @param record A validated drawing record.
    @param width New rectangle width, mm (None keeps it).
    @param height New rectangle height, mm (None keeps it).
    @param radius New circle radius, mm (None keeps it).
    @return A new record. A rectangle keeps its centre and which corner is
        stored first; a circle keeps its centre and the direction of its rim
        point. Any other kind, or a size that does not apply to the kind, is
        returned unchanged (a copy). A non-positive size gives a degenerate
        record the caller's validation refuses.
    """
    clean = dict(record)
    points = [[float(x), float(y)] for x, y in record["points"]]
    clean["points"] = [list(p) for p in record["points"]]
    if record["kind"] == "rectangle" and (width is not None or height is not None):
        (x0, y0), (x1, y1) = points
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        half_w = abs(x1 - x0) / 2.0 if width is None else float(width) / 2.0
        half_h = abs(y1 - y0) / 2.0 if height is None else float(height) / 2.0
        sign_x = 1.0 if x0 >= x1 else -1.0
        sign_y = 1.0 if y0 >= y1 else -1.0
        clean["points"] = [[round(cx + sign_x * half_w, 4) + 0.0, round(cy + sign_y * half_h, 4) + 0.0],
                           [round(cx - sign_x * half_w, 4) + 0.0, round(cy - sign_y * half_h, 4) + 0.0]]
    elif record["kind"] == "circle" and radius is not None:
        (cx, cy), (rx, ry) = points
        old = math.hypot(rx - cx, ry - cy)
        ux, uy = ((rx - cx) / old, (ry - cy) / old) if old > 0 else (1.0, 0.0)
        r = float(radius)
        clean["points"] = [list(record["points"][0]),
                           [round(cx + ux * r, 4) + 0.0, round(cy + uy * r, 4) + 0.0]]
    return clean


def text_positions_anchored(raw) -> bool:
    """@brief Tell whether a saved Reference/Value position mapping holds bottom-left anchors.

    @param raw Whatever the project or recovery file holds for the positions.
    @return True when it is a mapping marked with `TEXT_POSITIONS_ANCHOR_FIELD`
        = `USER_TEXT_ANCHOR`; False for an unmarked (legacy, centred) or
        malformed value.
    """
    return isinstance(raw, dict) and raw.get(TEXT_POSITIONS_ANCHOR_FIELD) == USER_TEXT_ANCHOR


def text_positions_json(positions) -> dict:
    """@brief JSON form of validated Reference/Value anchors, marked as bottom-left anchors.

    @param positions {key: (x, y)} mm, keys of `TEXT_KEYS` (bottom-left anchors).
    @return {key: [x, y]} plus `TEXT_POSITIONS_ANCHOR_FIELD` when at least
        one position is chosen; {} when none is (nothing to convert, and the
        file stays as it was before the mark existed).
    """
    data = {key: [float(value[0]), float(value[1])] for key, value in positions.items()}
    if data:
        data[TEXT_POSITIONS_ANCHOR_FIELD] = USER_TEXT_ANCHOR
    return data


def migrate_centred_positions(positions, corner_offsets) -> dict:
    """@brief Move legacy centred Reference/Value positions to the bottom-left corner of their text.

    @param positions {key: (x, y)} mm as `validate_text_positions` returns
        them from an UNMARKED mapping (each point the centre of its text).
    @param corner_offsets {key: (dx, dy)} mm from the centre to the
        bottom-left anchor of that key's text, `property_corner_offset` of
        the text KiCad shows; a key without an offset is left out of the
        result (it falls back to its default instead of being misread).
    @return {key: (x, y)} anchors rounded to 0.1 um; a coordinate pushed past
        `TEXT_POSITION_LIMIT_MM` is clamped to it (the text then moves by at
        most half its width instead of being lost).
    @note CONTRACT CHANGED 2026-10-16 (user: KiCad stays still in both
        tabs): with `property_corner_offset` the conversion keeps the text
        where KICAD drew it; the preview may show it shifted once by a few
        tenths of a mm (its font is not KiCad's). Before, the offsets came
        from the preview's metrics and KiCad moved the text instead.
    """
    limit = TEXT_POSITION_LIMIT_MM
    result = {}
    for key, (x, y) in positions.items():
        offset = corner_offsets.get(key)
        if offset is None:
            continue
        moved = [round(float(v) + float(d), 4) for v, d in zip((x, y), offset)]
        if not all(math.isfinite(v) for v in moved):
            continue
        result[key] = tuple(max(-limit, min(limit, v)) + 0.0 for v in moved)
    return result


#: Extension of a KiCad footprint file inside a ``.pretty`` library folder.
FOOTPRINT_FILE_SUFFIX = ".kicad_mod"


def footprint_file_name(name: str) -> str:
    """@brief Return the only file name KiCad accepts for footprint `name`.

    @param name Footprint name as written in ``(footprint "<name>" ...)``.
    @return ``<name>.kicad_mod``: KiCad resolves ``library:<name>`` through
        the file name, so the two must be identical, case included.
    """
    return f"{name}{FOOTPRINT_FILE_SUFFIX}"


@dataclass
class Footprint:
    """@brief Complete KiCad footprint that renders and saves as a .kicad_mod file.

    @param datasheet Datasheet URL or path; "" when unknown. It fills the
        hidden "Datasheet" property, as `description` fills "Description".
    """

    name: str
    description: str = ""
    tags: str = ""
    #: Datasheet URL or path, written to the "Datasheet" property; "" when unknown.
    datasheet: str = ""
    attr: Optional[str] = None  # "through_hole" | "smd" | None
    reference_at: Point = (0.0, -2.0)
    value_at: Point = (0.0, 2.0)
    #: {key: justify words} of the Reference/Value texts placed by
    #: `place_texts` or `place_default_texts` (bottom-left anchors,
    #: ``("left", "bottom")``); a key without an entry keeps KiCad's centred
    #: text (a Footprint built directly, as before 2026-10-16).
    text_justify: dict = field(default_factory=dict)

    pads: list = field(default_factory=list)
    graphics: list = field(default_factory=list)  # FpLine | FpRect | FpCircle | FpText

    def place_default_texts(self, grid_origin: Optional[Point] = None) -> "Footprint":
        """@brief Move Reference and Value to their geometry-derived defaults.

        @param grid_origin Origin of the grid the 2D preview draws for this
            footprint (each generator passes its own, the one its drawer
            uses); None uses text_grid_origin.
        @return self, for chaining.
        @note Generators call this once their pads and outlines are complete
            and before any user choice (place_texts) or user drawing is
            added, so neither moves the default (decision D1, 2026-10-14).
        @note CONTRACT CHANGED 2026-10-17 (user: «Predefinito centrato»):
            the D1 centres (`default_text_positions`) are written as KiCad
            centred texts, with no ``justify``. A centred default stays
            centred under the pads whatever text KiCad shows in it: the
            board shows the schematic Value, not the footprint name, and a
            rename changes the name's width. From 2026-10-16 to this change
            the defaults were converted to ``(justify left bottom)``
            anchors, which kept the left edge and so lost the centring.
            Only a text the user places (`place_texts`) is anchored at its
            bottom-left corner.
        """
        centres = default_text_positions(self, grid_origin)
        self.reference_at = centres["Reference"]
        self.value_at = centres["Value"]
        self.text_justify = {}
        return self

    def property_texts(self) -> dict:
        """@brief The texts KiCad shows for the Reference and Value properties of this footprint.

        @return {"Reference": `REFERENCE_TEXT`, "Value": the footprint name},
            the texts `to_sexpr` writes, as KiCad shows them: unescaped
            (`kicad_text.kicad_unescape`, «{slash}» shown as «/»).
        """
        from .kicad_text import kicad_unescape
        return {"Reference": kicad_unescape(REFERENCE_TEXT), "Value": kicad_unescape(self.name)}

    def _text_points(self) -> dict:
        """{key: (point, bottom-left?)} of the Reference/Value properties as written."""
        return {"Reference": (self.reference_at, "Reference" in self.text_justify),
                "Value": (self.value_at, "Value" in self.text_justify)}

    def text_anchors(self) -> dict:
        """@brief Bottom-left anchors, mm, at which KiCad draws the Reference and Value texts.

        @return {key: (x, y)} for every key of `TEXT_KEYS`: the written point
            of a bottom-left text, the KiCad conversion of a centred one
            (`property_corner_offset`, rounded to 0.1 um and clamped like
            `migrate_centred_positions`).
        """
        texts = self.property_texts()
        result = {}
        for key, (point, corner) in self._text_points().items():
            if corner:
                result[key] = (float(point[0]) + 0.0, float(point[1]) + 0.0)
            else:
                result[key] = migrate_centred_positions(
                    {key: point}, {key: property_corner_offset(texts[key])})[key]
        return result

    def text_centres(self) -> dict:
        """@brief Points, mm, on which KiCad centres the Reference and Value texts as written.

        @return {key: (x, y)} for every key of `TEXT_KEYS`: the written point
            of a centred text, the inverse KiCad conversion of a bottom-left
            one (unrounded; within 0.1 um of the centre it was converted
            from, the rounding of the anchor).
        """
        texts = self.property_texts()
        result = {}
        for key, (point, corner) in self._text_points().items():
            if corner:
                result[key] = kicad_centre_from_corner(
                    texts[key], PROPERTY_TEXT_SIZE, point[0], point[1],
                    thickness=PROPERTY_TEXT_THICKNESS, y_down=True)
            else:
                result[key] = (float(point[0]), float(point[1]))
        return result

    def place_texts(self, positions) -> "Footprint":
        """@brief Put the Reference and Value texts where the user chose; the others keep their default.

        @param positions {key: (x, y)} mm in footprint coordinates (y down),
            keys of `TEXT_KEYS`; each point is the BOTTOM-LEFT corner of the
            text (left edge, baseline); None or {} changes nothing.
        @return self, for chaining.
        @throws ValueError On an unknown key or a position that does not validate.
        @note CONTRACT CHANGED 2026-10-16 (user: REF**/Value anchored like
            the drawn texts): a placed text is written with
            ``(justify left bottom)`` at its point, so it grows right and up
            from it; before, the point was the text centre. A text not placed
            here keeps its centred default (`place_default_texts`).
        """
        for key, value in (positions or {}).items():
            if key not in TEXT_KEYS:
                raise ValueError(f"footprint text: unknown key {key!r}")
            checked = validate_text_positions({key: value})
            if key not in checked:
                raise ValueError(f"footprint text {key}: needs [x, y] in mm")
            if key == "Reference":
                self.reference_at = checked[key]
            else:
                self.value_at = checked[key]
            self.text_justify[key] = USER_TEXT_JUSTIFY
        return self

    def add_pad(self, pad: Pad) -> "Footprint":
        """@brief Append a pad and return self for chaining.

        @param pad The Pad to add; not validated here.
        @return This footprint.
        """
        self.pads.append(pad)
        return self

    def add_graphic(self, item) -> "Footprint":
        """@brief Append a graphic item (line, rectangle, circle or text) and return self for chaining.

        @param item Any object with a `to_sexpr()` method (FpLine, FpRect,
            FpCircle, FpArc, FpText).
        @return This footprint.
        """
        self.graphics.append(item)
        return self

    def _property_node(self, key: str, value: str, at: Point, layer: str, hide: bool = False) -> SExpr:
        """Property node; a key in `text_justify` gets its ``(justify ...)`` (bottom-left anchor)."""
        node = sx("property", key, value)
        node.add(_at_node(at))
        node.add(sx("unlocked", YES))
        node.add(sx("layer", layer))
        if hide:
            node.add(sx("hide", YES))
        node.add(sx("uuid", _uuid()))
        node.add(_text_effects(PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS,
                               justify=self.text_justify.get(key)))
        return node

    def to_sexpr(self) -> SExpr:
        """@brief Serialize the complete footprint to a KiCad S-expression.

        @return The root `footprint` node: header, properties (Reference,
            Value, hidden Datasheet and Description), attribute, graphics
            and pads, in the order KiCad writes them.
        """
        root = sx("footprint", self.name)
        root.add(sx("version", KICAD_VERSION))
        root.add(sx("generator", "kicadgen_tool"))
        root.add(sx("generator_version", GENERATOR_VERSION))
        root.add(sx("layer", "F.Cu"))
        if self.description:
            root.add(sx("descr", self.description))
        if self.tags:
            root.add(sx("tags", self.tags))

        root.add(self._property_node("Reference", REFERENCE_TEXT, self.reference_at, "F.SilkS"))
        root.add(self._property_node("Value", self.name, self.value_at, "F.Fab"))
        # The hidden properties repeat descr/datasheet: KiCad's footprint
        # editor edits the properties, so an empty one looks like missing data.
        root.add(self._property_node("Datasheet", self.datasheet, (0, 0), "F.Fab", hide=True))
        root.add(self._property_node("Description", self.description, (0, 0), "F.Fab", hide=True))

        if self.attr:
            root.add(sx("attr", Symbol(self.attr)))

        for item in self.graphics:
            root.add(item.to_sexpr())
        for pad in self.pads:
            root.add(pad.to_sexpr())

        return root

    def render(self) -> str:
        """@brief Return the complete .kicad_mod file text, newline-terminated.

        @return The rendered S-expression followed by a single newline.
        """
        return self.to_sexpr().render(0) + "\n"

    def save(self, path: str) -> None:
        """@brief Write the .kicad_mod file to `path` atomically, creating missing folders.

        @param path Destination .kicad_mod path; its file name must be
            ``<name>.kicad_mod``.
        @throws ValueError When the file name is not ``<name>.kicad_mod``
            (nothing is written).
        @throws OSError When the folder cannot be created or the file cannot
            be written or replaced; the previous file, if any, is left intact.
        @note The text is written to a temporary sibling and moved onto
            `path` with fsutil.replace_file: an interrupted write (full disk,
            killed process) never leaves a truncated footprint that KiCad or
            the library installer would pick up.
        @note KiCad identifies a library footprint by its file name
            (``library:<file stem>``) and reports a footprint whose internal
            name differs from it; every generator writes through this method,
            so the check here keeps the two identical on every path.
        """
        import os
        import tempfile

        from ..fsutil import replace_file

        expected = footprint_file_name(self.name)
        if os.path.basename(path) != expected:
            raise ValueError(
                f"footprint file name {os.path.basename(path)!r} does not match "
                f"the footprint name {self.name!r} (expected {expected!r})")
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        data = self.render().encode("utf-8")
        handle, temporary = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            replace_file(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def pad_copper_centre(pad: Pad) -> Point:
    """@brief Centre of a pad's copper shape in footprint mm.

    @param pad Any pad; one carrying ``drill_offset`` (dx, dy) in pad-local
        axes, as the castellated OffsetDrillPad does, has its copper there.
    @return ``at`` plus the offset turned by the pad rotation, as KiCad's
        PAD::ShapePos does (RotatePoint: x' = x cos + y sin, y' = -x sin +
        y cos, counter-clockwise on the y-down board); ``at`` when there is no
        offset. In KiCad ``at`` is the HOLE and the offset moves the copper.
    """
    dx, dy = getattr(pad, "drill_offset", None) or (0.0, 0.0)
    if not (dx or dy):
        return (float(pad.at[0]), float(pad.at[1]))
    angle = math.radians(float(pad.rotation or 0.0))
    ca, sa = math.cos(angle), math.sin(angle)
    return (float(pad.at[0]) + dx * ca + dy * sa, float(pad.at[1]) - dx * sa + dy * ca)


def _pad_box(pad: Pad) -> Tuple[float, float, float, float]:
    """Axis-aligned box of a pad's copper (drill offset and rotation included), mm."""
    half_w, half_h = float(pad.size[0]) / 2, float(pad.size[1]) / 2
    angle = math.radians(float(pad.rotation or 0.0))
    ca, sa = abs(math.cos(angle)), abs(math.sin(angle))
    dx, dy = half_w * ca + half_h * sa, half_w * sa + half_h * ca
    cx, cy = pad_copper_centre(pad)
    return (cx - dx, cy - dy, cx + dx, cy + dy)


def text_grid_origin(footprint: "Footprint") -> Point:
    """@brief Generic origin of the text grid: the first numbered pad, mm.

    @param footprint Footprint whose pads are read.
    @return Position (``at``) of the first numbered pad, or (0, 0) without
        one. It is the grid origin of the pin-header preview and the fallback
        of default_text_positions; a generator whose preview anchors its grid
        elsewhere passes that origin explicitly (castellated: its first
        contact, see castellated_module.preview_grid_origin; package: its
        first numbered pin, see package.preview_grid_origin).
    @note The grid passes through a pad, not through the footprint origin
        (user choice: pads sit on grid lines). A part symmetric about its
        origin therefore gets mirror-image texts only when that pad lies a
        whole number of half grid steps from the centre; otherwise the two
        anchors differ from a mirror image by less than one TEXT_GRID_MM.
    """
    pad = next((pad for pad in footprint.pads if str(pad.number).strip()), None)
    return (0.0, 0.0) if pad is None else (float(pad.at[0]) + 0.0, float(pad.at[1]) + 0.0)


def _graphic_points(item) -> list:
    """Points bounding a footprint graphic (circles by their radius); [] for texts."""
    if isinstance(item, FpText):
        return []
    if isinstance(item, FpCircle):
        (cx, cy), r = item.center, abs(float(item.radius))
        return [(cx - r, cy - r), (cx + r, cy + r)]
    return [p for p in (getattr(item, "start", None), getattr(item, "mid", None),
                        getattr(item, "end", None)) if p is not None]


def footprint_envelope(footprint: "Footprint") -> Optional[Tuple[float, float, float, float]]:
    """@brief Bounding box of a footprint's pads and outline graphics, mm.

    @param footprint Footprint whose pads and graphics are read.
    @return (min x, min y, max x, max y) of the pad copper (rotation and
        drill offset included) and of the graphics on TEXT_ENVELOPE_LAYERS (texts
        excluded); None when there is neither, or a value is not finite.
    @note Arcs are bounded by their three points, a slight underestimate
        that the text margin covers.
    """
    xs, ys = [], []
    for pad in footprint.pads:
        x0, y0, x1, y1 = _pad_box(pad)
        xs += [x0, x1]
        ys += [y0, y1]
    for item in footprint.graphics:
        if getattr(item, "layer", "") in TEXT_ENVELOPE_LAYERS:
            for x, y in _graphic_points(item):
                xs.append(float(x))
                ys.append(float(y))
    if not xs or not all(math.isfinite(v) for v in (*xs, *ys)):
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def default_text_positions(footprint: "Footprint", grid_origin: Optional[Point] = None) -> dict:
    """@brief Default Reference/Value anchors of a footprint, from its geometry.

    @param footprint Generated footprint (pads and outlines complete).
    @param grid_origin Origin of the preview grid of this footprint, mm;
        None uses text_grid_origin.
    @return {"Reference": (x, y), "Value": (x, y)} mm, the CENTRE of each
        text (KiCad's default justification): Reference centred
        above the envelope (footprint_envelope), Value centred below it,
        each clear of it by half the text box (size + stroke, 1.0 + 0.15 mm,
        the property text written by Footprint) plus SILK_PAD_CLEARANCE_MM,
        the silk-to-copper clearance of the silkscreen outlines. Anchors are
        snapped to TEXT_GRID_MM from `grid_origin` (the preview grid),
        outwards for y (never closer than the margin) and to the nearest node
        for x. Each coordinate beyond TEXT_POSITION_LIMIT_MM is clamped to
        the last grid node inside it, text by text (the other text keeps its
        place). DEFAULT_TEXT_POSITIONS only when the footprint has no geometry.
    @note Decision D1 (user, 2026-10-14): the old fixed (0, -2) put «REF**»
        on the pads of SOIC packages and on the pin-1 marker of pin headers
        (KiCad DRC silk_over_copper). The envelope is the larger of pads and
        body/courtyard, so a rotated or off-centre package uses its real extent.
    @note The default is a centre (the D1 contract); its bottom-left
        anchor depends on the text width, which `Footprint.place_default_texts`
        takes from KiCad's stroke-font geometry (`property_corner_offset`,
        2026-10-16), so every footprint, with or without the window, is
        written in the same ``(justify left bottom)`` form.
    """
    from .silk import SILK_PAD_CLEARANCE_MM

    envelope = footprint_envelope(footprint)
    if envelope is None:
        return dict(DEFAULT_TEXT_POSITIONS)
    x0, y0, x1, y1 = envelope
    margin = (1.0 + 0.15) / 2 + SILK_PAD_CLEARANCE_MM
    ox, oy = text_grid_origin(footprint) if grid_origin is None else (
        float(grid_origin[0]), float(grid_origin[1]))
    grid = TEXT_GRID_MM
    limit = TEXT_POSITION_LIMIT_MM

    def snap(value, origin, rounding):
        # Integer steps from the origin; the tiny tolerance keeps a value
        # already on a node there despite float noise. A value past the
        # limit is first brought to it, then to the last node inside it.
        if abs(value) > limit:
            value = math.copysign(limit, value)
            rounding = math.ceil if value < 0 else math.floor
        steps = (value - origin) / grid
        if rounding is math.floor:
            steps = math.floor(steps + 1e-9)
        elif rounding is math.ceil:
            steps = math.ceil(steps - 1e-9)
        else:
            steps = math.floor(steps + 0.5)
        return round(origin + steps * grid, 6) + 0.0

    x = snap((x0 + x1) / 2, ox, round)
    return {"Reference": (x, snap(y0 - margin, oy, math.floor)),
            "Value": (x, snap(y1 + margin, oy, math.ceil))}
