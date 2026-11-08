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
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

from .draw_constraints import polygon_has_area
from .drawing_transform import bounds_centre, mirror_points_x, rotate_points_90
from .footprint_layers import default_width, drawable_layers, layer as draw_layer
from .kicad_text import (KICAD_PEN_CLAMP_RATIO, kicad_advance_units, kicad_centre_from_corner,
                         kicad_centre_to_corner, kicad_text_width)
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

#: Footprint attribute flags KiCad writes after the mounting type in
#: `(attr ...)`, in KiCad's own order (pcb_io_kicad_sexpr): «Not in
#: schematic», «Exclude from position files», «Exclude from BOM», «Exempt
#: from courtyard requirement», «Do not populate». `Footprint.attributes`
#: holds a subset of them; an unknown flag is refused, not written.
FOOTPRINT_ATTRIBUTE_FLAGS = ("board_only", "exclude_from_pos_files", "exclude_from_bom",
                             "allow_missing_courtyard", "dnp")

#: Silkscreen stroke width, mm (KLC F5.1 recommends 0.12 mm).
SILK_WIDTH_MM = 0.12

#: Courtyard stroke width, mm (KLC F5.3: 0.05 mm), read from the layer
#: table `footprint_layers.LAYERS` so the generators and the table agree.
COURTYARD_WIDTH_MM = draw_layer("F.CrtYd").width_mm

#: Stroke width, mm, of a written dimension (KiCad's default dimension line).
USER_DIMENSION_WIDTH_MM = 0.1
#: Text height, mm, of a written dimension's value.
USER_DIMENSION_TEXT_MM = 1.0
#: Stroke thickness, mm, of a written dimension's value text.
DIMENSION_TEXT_THICKNESS_MM = 0.15
#: How far, mm, each extension line of a dimension runs BEYOND the dimension
#: line (KiCad's default `extension_height`, pcb_dimension.cpp). Written in
#: the style node and used by the ink of the checks and the preview, so the
#: three agree by construction.
DIMENSION_EXTENSION_HEIGHT_MM = 0.58642
#: Length, mm, of the arrowheads at the dimension line's ends (KiCad's
#: default `arrow_length`), drawn as two strokes at `DIMENSION_ARROW_ANGLE_DEG`.
DIMENSION_ARROW_LENGTH_MM = 1.27
#: Half opening angle, degrees, of a KiCad arrowhead (pcb_dimension.cpp `s_arrowAngle`).
DIMENSION_ARROW_ANGLE_DEG = 27.5

#: Fabrication-layer stroke width, mm (KLC F5.2 recommends 0.10 mm).
FAB_WIDTH_MM = 0.10

#: Distance, mm, of a silk body outline's centre line outside the F.Fab
#: body outline: half of each stroke (0.05 + 0.06), so the two lines just
#: meet and the silk is drawn outside the body, never over the assembly
#: outline. Shared by every generator that draws both.
SILK_OUTSIDE_FAB_MM = FAB_WIDTH_MM / 2 + SILK_WIDTH_MM / 2

#: Grid, mm, the courtyard corners are rounded OUTWARDS to (KLC F5.3: the
#: courtyard lies on a 0.01 mm grid). Rounding outwards keeps the clearance
#: at least the declared margin.
COURTYARD_GRID_MM = 0.01


def courtyard_round_out(low: float, high: float) -> tuple:
    """@brief Round a courtyard interval outwards to `COURTYARD_GRID_MM`.

    @param low Smaller coordinate of the interval, mm.
    @param high Larger coordinate of the interval, mm.
    @return (low', high') with low' <= low and high' >= high, both whole
        multiples of 0.01 mm; a value already on the grid (within float
        noise of 1e-6 mm) is kept.
    """
    steps = 1.0 / COURTYARD_GRID_MM
    return (round(math.floor(low * steps + 1e-6) / steps, 2) + 0.0,
            round(math.ceil(high * steps - 1e-6) / steps, 2) + 0.0)


def courtyard_corners(start: Point, end: Point) -> tuple:
    """@brief The corners of a courtyard rectangle rounded outwards to the 0.01 mm grid.

    @param start (x, y) of one corner, mm.
    @param end (x, y) of the opposite corner, mm.
    @return (start', end') with start' the lower-left and end' the
        upper-right corner, each coordinate rounded outwards
        (`courtyard_round_out`).
    """
    x0, x1 = courtyard_round_out(min(start[0], end[0]), max(start[0], end[0]))
    y0, y1 = courtyard_round_out(min(start[1], end[1]), max(start[1], end[1]))
    return (x0, y0), (x1, y1)


def normalised_attribute_flags(flags) -> Tuple[str, ...]:
    """@brief The footprint attribute flags in KiCad's order, each once.

    @param flags Iterable of flag names (see `FOOTPRINT_ATTRIBUTE_FLAGS`);
        None or an empty iterable gives ().
    @return The known flags present, ordered as KiCad writes them.
    @throws ValueError When a flag is not one KiCad knows: writing it would
        make the file unreadable, silently dropping it would lose a choice.
    """
    wanted = {str(flag) for flag in (flags or ())}
    unknown = sorted(wanted - set(FOOTPRINT_ATTRIBUTE_FLAGS))
    if unknown:
        raise ValueError(tx("Attributo del footprint sconosciuto: {flags}", flags=", ".join(unknown)))
    return tuple(flag for flag in FOOTPRINT_ATTRIBUTE_FLAGS if flag in wanted)


def validate_pad_number(number) -> None:
    """@brief Refuse a pad number KiCad and the symbol would not read as the same text.

    @param number Pad number as it will be written (str; "" for a hole without a net).
    @throws ValueError When the number holds whitespace anywhere (leading,
        trailing or inner) or a control character.
    @note The same rule as the symbol's pin numbers (`generators.symbol`
        refuses control characters and inner blanks and strips the ends):
        a pad « 1», or «B3» followed by a newline, would be compared with
        a symbol pin «1» or «B3» and the pin-pad match would fail without
        a visible cause.
    """
    text = str(number)
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cs") for ch in text):
        raise ValueError(tx("Numero del pad «{number}»: spazi e caratteri di controllo non sono ammessi.",
                            number=text.strip()))


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
    - shape: "circle" | "rect" | "oval" | "roundrect" | "trapezoid"; a
      "circle" with two different sizes is written as "oval" (KiCad would
      draw a disc of the first size only)
    - at: (x, y) position in mm relative to the footprint origin
    - size: (width, height) in mm
    - layers: list of KiCad layers, e.g. ["*.Cu", "*.Mask"] for THT or ["F.Cu", "F.Paste", "F.Mask"] for SMD
    - drill: round hole diameter in mm (a thru_hole/np_thru_hole pad needs
      `drill` or `drill_slot`, never both)
    - drill_slot: (width, height) in mm of an oval hole (a slot), written as
      KiCad's `(drill oval w h)`; the sides are along the pad's own axes and
      turn with its rotation. Equal sides are a round hole and are stored
      as `drill`. For a plated pad each side must stay below the pad side on
      its axis, as KiCad's DRC requires copper around the hole.
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
    drill_slot: Optional[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        if self.pad_type not in PAD_TYPES:
            raise ValueError(tx("pad_type non valido: {pad_type!r} (attesi: {PAD_TYPES})",
                                pad_type=self.pad_type,
                                PAD_TYPES=PAD_TYPES))
        if self.shape not in PAD_SHAPES:
            raise ValueError(tx("shape non valida: {shape!r} (attese: {PAD_SHAPES})",
                                shape=self.shape,
                                PAD_SHAPES=PAD_SHAPES))
        self._validate_drill_slot()
        if self.pad_type in ("thru_hole", "np_thru_hole") and self.drill is None and self.drill_slot is None:
            raise ValueError(tx("pad {number!r}: drill obbligatorio per pad_type={pad_type!r}",
                                number=self.number,
                                pad_type=self.pad_type))
        validate_pad_number(self.number)
        # KiCad draws a circle pad with the diameter of its FIRST size and
        # ignores the second: two different sides describe an oval, which
        # is written so the file holds the copper that was measured.
        if self.shape == "circle":
            try:
                first, second = (float(v) for v in self.size)
            except (TypeError, ValueError):
                first = second = None
            if first is not None and first != second:
                self.shape = "oval"
        self._validate_rect_delta()

    def _validate_drill_slot(self) -> None:
        """Normalise and check the slot: one hole only, finite positive sides, copper around them.

        A slot and a round drill together would be two holes for one pad;
        equal sides describe a round hole and are stored as `drill`, so a
        file never holds `(drill oval d d)` where `(drill d)` is meant. A
        plated pad (thru_hole) keeps copper on both axes: each side of the
        slot must be below the pad side on that axis (KiCad's DRC refuses a
        hole that leaves no annular ring); a mechanical hole (np_thru_hole)
        may be as large as it needs, its "pad" is only the mask opening.
        """
        if self.drill_slot is None:
            return
        if self.drill is not None:
            raise ValueError(tx("pad {number!r}: foro tondo e asola insieme non sono ammessi",
                                number=self.number))
        try:
            width, height = (float(v) for v in self.drill_slot)
        except (TypeError, ValueError) as exc:
            raise ValueError(tx("pad {number!r}: asola non valida", number=self.number)) from exc
        if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
            raise ValueError(tx("pad {number!r}: asola non valida", number=self.number))
        if self.pad_type not in ("thru_hole", "np_thru_hole"):
            raise ValueError(tx("pad {number!r}: asola ammessa solo per un pad forato",
                                number=self.number))
        if self.pad_type == "thru_hole":
            try:
                size_x, size_y = (float(v) for v in self.size)
            except (TypeError, ValueError) as exc:
                raise ValueError(tx("pad {number!r}: asola non valida", number=self.number)) from exc
            if width >= size_x or height >= size_y:
                raise ValueError(tx("pad {number!r}: ogni lato dell'asola deve essere minore del lato "
                                    "del pad sullo stesso asse", number=self.number))
        if width == height:
            self.drill = width
            self.drill_slot = None
        else:
            self.drill_slot = (width, height)

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
        elif self.drill_slot is not None:
            node.add(sx("drill", Symbol("oval"), *self.drill_slot))
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
class FpPoly:
    """@brief Graphic polygon on a footprint layer (fp_poly): closed, optionally filled.

    @param points The corners in order, mm; the last side closes back to
        the first corner, which is not repeated (as KiCad writes `pts`).
    """

    points: Sequence[Point]
    layer: str
    width: float = 0.12
    fill: bool = False

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this polygon to a KiCad S-expression.

        @return The `fp_poly` node with its `pts`, stroke, fill, layer and a fresh uuid.
        @throws ValueError With fewer than three corners (no area to draw).
        """
        if len(self.points) < 3:
            raise ValueError(tx("Un poligono richiede almeno tre punti."))
        node = sx("fp_poly", sx("pts", *(sx("xy", x, y) for x, y in self.points)))
        node.add(_stroke_node(self.width))
        node.add(sx("fill", bare(self.fill)))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


def dimension_normal(start: Point, end: Point) -> Point:
    """@brief Unit vector from the measured line towards a positive `height`, KiCad's convention.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @return (-dy, dx) / length: KiCad (`PCB_DIM_ALIGNED::updateGeometry`)
        puts the crossbar of an aligned dimension at the points plus this
        vector times `height`; a negative height goes the other way.
        (0, 0) for coincident points.
    """
    dx, dy = float(end[0]) - float(start[0]), float(end[1]) - float(start[1])
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return (0.0, 0.0)
    return (-dy / length, dx / length)


def dimension_offset_direction(start: Point, end: Point, axis: str = "") -> Point:
    """@brief Unit vector along which `height` moves the dimension line.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param axis "" for an aligned dimension (`dimension_normal`), "x" for an
        orthogonal one measuring |dX| (its line is horizontal, moved along
        Y), "y" for one measuring |dY| (vertical line, moved along X).
    @return The unit vector; (0, 0) for coincident aligned points.
    """
    if axis == "x":
        return (0.0, 1.0)
    if axis == "y":
        return (1.0, 0.0)
    return dimension_normal(start, end)


def dimension_crossbar(start: Point, end: Point, height: float, axis: str = "") -> Tuple[Point, Point]:
    """@brief The two ends of a dimension line, mm, as KiCad draws them.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param height Signed distance of the crossbar from the measured points, mm.
    @param axis "" aligned; "x" / "y" orthogonal (see `dimension_offset_direction`).
    @return (crossbar start, crossbar end). An orthogonal crossbar lies at
        the first point's coordinate plus `height` along the perpendicular
        axis and spans the two points along the measured axis
        (PCB_DIM_ORTHOGONAL::updateGeometry).
    """
    h = float(height)
    sx_, sy_ = float(start[0]), float(start[1])
    ex_, ey_ = float(end[0]), float(end[1])
    if axis == "x":
        return ((sx_, sy_ + h), (ex_, sy_ + h))
    if axis == "y":
        return ((sx_ + h, sy_), (sx_ + h, ey_))
    nx, ny = dimension_normal(start, end)
    return ((sx_ + nx * h, sy_ + ny * h), (ex_ + nx * h, ey_ + ny * h))


def dimension_offset_from_crossbar(start: Point, end: Point, crossbar_start: Point, axis: str = "") -> float:
    """@brief The signed `height` that puts a dimension line through `crossbar_start`.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param crossbar_start Where the dimension line passes at the first point, mm.
    @param axis "" aligned; "x" / "y" orthogonal.
    @return The height, mm: the component of (crossbar_start - start) along
        `dimension_offset_direction`. The inverse of `dimension_crossbar`,
        used to carry a dimension through a turn or a mirror.
    """
    ux, uy = dimension_offset_direction(start, end, axis)
    return (float(crossbar_start[0]) - float(start[0])) * ux + (float(crossbar_start[1]) - float(start[1])) * uy


def dimension_value_mm(start: Point, end: Point, axis: str = "") -> float:
    """@brief What a dimension measures, mm: the distance, or |dX| / |dY| for an orthogonal one.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param axis "" aligned; "x" / "y" orthogonal.
    @return The measured value, mm.
    """
    dx, dy = float(end[0]) - float(start[0]), float(end[1]) - float(start[1])
    if axis == "x":
        return abs(dx)
    if axis == "y":
        return abs(dy)
    return math.hypot(dx, dy)


def dimension_text(start: Point, end: Point, axis: str = "") -> str:
    """@brief The value a dimension shows: millimetres with two decimals, as the preview labels.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param axis "" aligned; "x" / "y" orthogonal.
    @return «12.70 mm» style text (KiCad rewrites it from the points on
        load; the file still carries a sane value).
    """
    return f"{dimension_value_mm(start, end, axis):.2f} mm"


def dimension_ink_segments(start: Point, end: Point, height: float, axis: str = "") -> list:
    """@brief Every stroke KiCad draws for a dimension, mm: extension lines, crossbar, arrowheads.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param height Signed distance of the crossbar from the points, mm.
    @param axis "" aligned; "x" / "y" orthogonal.
    @return List of ((x0, y0), (x1, y1)) segments: each extension line from
        its measured point to `DIMENSION_EXTENSION_HEIGHT_MM` beyond the
        crossbar, the crossbar, and the two strokes of each arrowhead
        (`DIMENSION_ARROW_LENGTH_MM` long, `DIMENSION_ARROW_ANGLE_DEG` from
        the crossbar, pointing outward as KiCad's default `arrow_direction`).
        The silk-clearance check and the preview share it with the writer's
        style constants, so what is checked and shown is what is written.
    """
    (c1, c2) = dimension_crossbar(start, end, height, axis)
    ux, uy = dimension_offset_direction(start, end, axis)
    sign = 1.0 if float(height) >= 0 else -1.0
    over = DIMENSION_EXTENSION_HEIGHT_MM
    segments = [(tuple(map(float, start)), (c1[0] + ux * over * sign, c1[1] + uy * over * sign)),
                (tuple(map(float, end)), (c2[0] + ux * over * sign, c2[1] + uy * over * sign)),
                (c1, c2)]
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    length = math.hypot(dx, dy)
    if length > 0:
        dx, dy = dx / length, dy / length
        angle = math.radians(DIMENSION_ARROW_ANGLE_DEG)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        for (px, py), (tx_, ty_) in ((c1, (-dx, -dy)), (c2, (dx, dy))):
            # The two strokes of an outward arrowhead at this end: the
            # tangent turned by +angle and by -angle (s flips the sine).
            for s in (1.0, -1.0):
                ax = tx_ * cos_a - ty_ * s * sin_a
                ay = tx_ * s * sin_a + ty_ * cos_a
                segments.append(((px, py), (px - ax * DIMENSION_ARROW_LENGTH_MM, py - ay * DIMENSION_ARROW_LENGTH_MM)))
    return segments


def dimension_label_pose(start: Point, end: Point, height: float, text_size: float, axis: str = "",
                         thickness: float = DIMENSION_TEXT_THICKNESS_MM) -> tuple:
    """@brief Where KiCad puts an aligned/orthogonal dimension's value: beside the crossbar middle, upright.

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param height Signed distance of the crossbar from the points, mm.
    @param text_size Text height, mm.
    @param axis "" aligned; "x" / "y" orthogonal.
    @param thickness Written text stroke thickness, mm (its clamped pen is
        part of the offset).
    @return ((x, y), angle_deg): the text centre and the KiCad angle of the
        text (counter-clockwise, kept upright: read left to right or bottom
        to top), as KiCad recomputes them on load for `text_position_mode 0`
        (outside) with `keep_text_aligned` (`PCB_DIM_ALIGNED::updateText`,
        which the orthogonal type also ends in): the crossbar middle moved
        by text size + pen perpendicular to the crossbar, towards -y (up the
        page) for any non-vertical crossbar and towards -x for a vertical
        one, WHATEVER the sign of `height`: a positive height on a rightward
        line puts the text between the crossbar and the measured points.
    @note Verified with kicad-cli 10.0 `fp upgrade`, which rewrites the
        gr_text position (2026-11-08): (0,0)->(4,0) height 1 -> (2, -0.15);
        (0,0)->(0,4) height 1 -> (-2.15, 2); (0,0)->(3,4) height 2 ->
        (0.82, 2.51); orthogonal (1,4)->(0,0) along Y height 2 -> (1.85, 2).
    """
    (c1, c2) = dimension_crossbar(start, end, height, axis)
    hx, hy = (c2[0] - c1[0]) / 2.0, (c2[1] - c1[1]) / 2.0
    # KiCad turns the half crossbar a quarter turn (RotatePoint, Y down:
    # +90 maps (x, y) to (y, -x), -90 to (-y, x)): +90 when it runs right,
    # -90 when it runs left, and for a vertical one by the sign of -y.
    if hx == 0.0:
        quarter = 1.0 if hy < 0 else -1.0
    else:
        quarter = 1.0 if hx > 0 else -1.0
    px, py = (hy, -hx) if quarter > 0 else (-hy, hx)
    distance = float(text_size) + dimension_text_pen(text_size, thickness)
    norm = math.hypot(px, py)
    if norm > 0.0:
        px, py = px / norm * distance, py / norm * distance
    centre = (c1[0] + hx + px, c1[1] + hy + py)
    angle = math.degrees(math.atan2(-(c2[1] - c1[1]), c2[0] - c1[0])) % 360.0
    if 90.0 < angle <= 270.0:
        angle = (angle + 180.0) % 360.0
    return (centre, angle)


#: Dimension types a drawing record's ``dtype`` may name, as KiCad's
#: `(type ...)` tokens. A record without ``dtype`` is the type written before
#: the field existed: aligned, or orthogonal when it carries ``axis``.
DIMENSION_TYPES = ("aligned", "orthogonal", "radial", "center", "leader")
#: Default distance, mm, from the rim point of a radial dimension to its
#: knee: KiCad's own default (`PCB_DIM_RADIAL` constructor: three arrow lengths).
DIMENSION_RADIAL_LEADER_MM = 3 * DIMENSION_ARROW_LENGTH_MM
#: Length, mm, of the horizontal shelf a radial or leader dimension draws
#: from its knee to the edge of its text box. A placement default of the
#: drawing (KiCad takes the text position from a click; this program
#: derives it), chosen as one arrowhead length so the shelf reads as part of
#: the arrow; not a measurement.
DIMENSION_SHELF_MM = DIMENSION_ARROW_LENGTH_MM
#: Text frame of a leader, record value -> KiCad's `text_frame` number
#: (`DIM_TEXT_BORDER`: NONE 0, RECTANGLE 1, CIRCLE 2).
LEADER_FRAMES = {"none": 0, "rect": 1, "circle": 2}
#: Height factor of KiCad's stroke-font text box: `EDA_TEXT::GetTextBox`
#: (KiCad 10 eda_text.cpp) adds ``KiROUND(extents.y * 0.17)`` («fudgeFactor»)
#: to the height of a stroke-font text.
_STROKE_TEXT_BOX_HEIGHT_FACTOR = 1.17
#: Pen widths, in effective text pens, that KiCad's stroke font adds to both
#: extents of a one-line text box (`FONT::StringBoundaryLimits`). Measured
#: with kicad-cli 10.0 `fp export svg` on leader frames (2026-11-08): the box
#: is exactly (size + 3 x pen) x 1.17 high for sizes 0.5-2.0 mm and pens
#: 0.05-0.25 x size, any text, and its width carries the same 3 x pen.
_STROKE_TEXT_BOX_PENS = 3.0
#: Width, in text sizes, that a non-empty stroke text box lacks against the
#: summed glyph advances (the spacing after the last glyph is not ink).
#: Same kicad-cli 10.0 measurement, 32 texts from «.» to «R 2.50 mm» at
#: sizes 1 and 2 mm, residue below 0.0001 mm; an empty text has no glyph and
#: no such spacing (its box is 3 x pen wide).
_STROKE_TEXT_BOX_TRAIL = 0.2


def _unit(dx: float, dy: float) -> Optional[Point]:
    """Unit vector of (dx, dy), None for a null vector."""
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return None
    return (dx / length, dy / length)


def _arrow_strokes(tip: Point, direction: Point) -> list:
    """The two strokes of a KiCad arrowhead at `tip`, running from it along `direction`.

    Each stroke is `DIMENSION_ARROW_LENGTH_MM` long and turned by
    +-`DIMENSION_ARROW_ANGLE_DEG` from `direction` (pcb_dimension.cpp
    `drawAnArrow`).
    """
    unit = _unit(float(direction[0]), float(direction[1]))
    if unit is None:
        return []
    ux, uy = unit
    angle = math.radians(DIMENSION_ARROW_ANGLE_DEG)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    x, y = float(tip[0]), float(tip[1])
    strokes = []
    for s in (1.0, -1.0):
        # `direction` turned by +angle and by -angle (s flips the sine).
        ax = ux * cos_a - uy * s * sin_a
        ay = ux * s * sin_a + uy * cos_a
        strokes.append(((x, y), (x + ax * DIMENSION_ARROW_LENGTH_MM, y + ay * DIMENSION_ARROW_LENGTH_MM)))
    return strokes


def dimension_text_half_box(text: str, text_size: float,
                            thickness: float = DIMENSION_TEXT_THICKNESS_MM) -> Point:
    """@brief Half width and half height, mm, of the box KiCad gives a one-line dimension text.

    @param text The text as KiCad shows it.
    @param text_size Text height, mm.
    @param thickness Text stroke thickness, mm.
    @return (half width, half height) of `EDA_TEXT::GetTextBox` for a stroke
        font: width = size x advances / 21 - `_STROKE_TEXT_BOX_TRAIL` x size
        (that term only for a non-empty text) + `_STROKE_TEXT_BOX_PENS` x pen;
        height = (size + `_STROKE_TEXT_BOX_PENS` x pen) x
        `_STROKE_TEXT_BOX_HEIGHT_FACTOR`; pen = `thickness` clamped to
        `KICAD_PEN_CLAMP_RATIO` x size, as KiCad draws it.
    @note Matches kicad-cli 10.0's own frames within 0.0001 mm on the
        measured range (see the constants); the preview, the checks and the
        writer share it.
    """
    size = float(text_size)
    pen = dimension_text_pen(size, thickness)
    width = _STROKE_TEXT_BOX_PENS * pen
    if text:
        width += size * kicad_advance_units(text) / 21.0 - _STROKE_TEXT_BOX_TRAIL * size
    height = (size + _STROKE_TEXT_BOX_PENS * pen) * _STROKE_TEXT_BOX_HEIGHT_FACTOR
    return (max(width, 0.0) / 2.0, height / 2.0)


def dimension_text_pen(text_size: float, thickness: float = DIMENSION_TEXT_THICKNESS_MM) -> float:
    """@brief The pen KiCad draws a dimension text with, mm (`GetEffectiveTextPenWidth`).

    @param text_size Text height, mm.
    @param thickness Written stroke thickness, mm.
    @return `thickness` clamped to `KICAD_PEN_CLAMP_RATIO` x `text_size`.
    """
    return min(float(thickness), KICAD_PEN_CLAMP_RATIO * float(text_size))


def _shelf_sign(a: Point, b: Point) -> float:
    """+1 when the shelf from `b` runs right (b right of or level with a), -1 when it runs left."""
    return -1.0 if float(b[0]) - float(a[0]) < 0 else 1.0


def radial_dimension_text(centre: Point, rim: Point) -> str:
    """@brief The value a radial dimension shows: «R» and the radius in millimetres with two decimals.

    @param centre Circle centre, mm.
    @param rim A point on the circle, mm.
    @return «R 1.50 mm» style text (prefix «R » as KiCad's default radial
        dimension, units mm with suffix, precision 2).
    """
    return f"R {math.hypot(float(rim[0]) - float(centre[0]), float(rim[1]) - float(centre[1])):.2f} mm"


def radial_dimension_knee(centre: Point, rim: Point, leader_length: float) -> Point:
    """@brief The knee of a radial dimension: the rim point moved outwards by the leader length.

    @param centre Circle centre, mm.
    @param rim A point on the circle, mm.
    @param leader_length Distance from the rim point to the knee, mm.
    @return (x, y) mm (`PCB_DIM_RADIAL::GetKnee`); the rim point itself
        when centre and rim coincide.
    """
    unit = _unit(float(rim[0]) - float(centre[0]), float(rim[1]) - float(centre[1])) or (0.0, 0.0)
    return (float(rim[0]) + unit[0] * float(leader_length), float(rim[1]) + unit[1] * float(leader_length))


def radial_dimension_label_pose(centre: Point, rim: Point, leader_length: float, text_size: float) -> tuple:
    """@brief Where a radial dimension's value is written: beyond a horizontal shelf from the knee.

    @param centre Circle centre, mm.
    @param rim A point on the circle, mm.
    @param leader_length Distance from the rim point to the knee, mm.
    @param text_size Text height, mm.
    @return ((x, y), 0.0): the text centre, on the knee's horizontal line,
        `DIMENSION_SHELF_MM` plus the text box reach away from the knee on
        the side the radius points to (right when it is vertical); the text
        is horizontal (KiCad keeps a radial text aligned with the shelf).
    @note KiCad clips the shelf at the text box inflated by half a text
        height on each side (`PCB_DIM_RADIAL::updateGeometry`), so the shelf
        it draws is `DIMENSION_SHELF_MM` long.
    """
    kx, ky = radial_dimension_knee(centre, rim, leader_length)
    half_w, _half_h = dimension_text_half_box(radial_dimension_text(centre, rim), text_size)
    reach = half_w + float(text_size) / 2.0
    sign = _shelf_sign(centre, rim)
    return ((kx + sign * (DIMENSION_SHELF_MM + reach), ky), 0.0)


def radial_dimension_ink(centre: Point, rim: Point, leader_length: float, text_size: float) -> list:
    """@brief Every stroke KiCad draws for a radial dimension, mm.

    @param centre Circle centre, mm.
    @param rim A point on the circle, mm.
    @param leader_length Distance from the rim point to the knee, mm.
    @param text_size Text height, mm (fixes nothing here but is kept in the
        signature of every dimension ink for symmetry with the label pose).
    @return ((x0, y0), (x1, y1)) segments: the centre cross (two axis-aligned
        strokes of +-`DIMENSION_ARROW_LENGTH_MM`), the leader from the rim
        point to the knee, the shelf to the text box, and the arrowhead at
        the rim point pointing at the circle (`PCB_DIM_RADIAL::updateGeometry`).
    """
    cx, cy = float(centre[0]), float(centre[1])
    arm = DIMENSION_ARROW_LENGTH_MM
    knee = radial_dimension_knee(centre, rim, leader_length)
    rim = (float(rim[0]), float(rim[1]))
    sign = _shelf_sign(centre, rim)
    segments = [((cx, cy - arm), (cx, cy + arm)), ((cx - arm, cy), (cx + arm, cy))]
    if knee != rim:
        segments.append((rim, knee))
    segments.append((knee, (knee[0] + sign * DIMENSION_SHELF_MM, knee[1])))
    segments.extend(_arrow_strokes(rim, (rim[0] - cx, rim[1] - cy)))
    return segments


def center_dimension_ink(centre: Point, end: Point) -> list:
    """@brief The two strokes of a centre mark, mm.

    @param centre Marked centre, mm.
    @param end End of one arm, mm: the arm length and its direction.
    @return Two segments through the centre: centre -+ arm and centre -+ the
        arm turned a quarter turn (`PCB_DIM_CENTER::updateGeometry`). A
        centre mark has no text.
    """
    cx, cy = float(centre[0]), float(centre[1])
    ax, ay = float(end[0]) - cx, float(end[1]) - cy
    return [((cx - ax, cy - ay), (cx + ax, cy + ay)), ((cx - ay, cy + ax), (cx + ay, cy - ax))]


def _leader_reach(text: str, text_size: float, frame: str) -> float:
    """Horizontal distance, mm, from a leader text's centre to where KiCad clips its shelf."""
    half_w, _half_h = dimension_text_half_box(text, text_size)
    # KiCad inflates the text box by half a text size across
    # (`PCB_DIM_LEADER::updateGeometry`); a circle frame's radius is that
    # half width minus half the text pen.
    frame_half_w = half_w + float(text_size) / 2.0
    if frame == "circle":
        return frame_half_w - dimension_text_pen(text_size) / 2.0
    return frame_half_w


def leader_dimension_label_pose(tip: Point, elbow: Point, text: str, text_size: float,
                                frame: str = "none") -> tuple:
    """@brief Where a leader's text is written: beyond a horizontal shelf from the elbow.

    @param tip Arrow tip, mm.
    @param elbow Elbow of the leader, mm.
    @param text The free text of the leader ("" shows nothing).
    @param text_size Text height, mm.
    @param frame "none", "rect" or "circle" (`LEADER_FRAMES`).
    @return ((x, y), 0.0): the text centre on the elbow's horizontal line,
        `DIMENSION_SHELF_MM` plus the frame reach away from the elbow, on the
        side away from the tip (right when the leader is vertical); the text
        is horizontal.
    """
    sign = _shelf_sign(tip, elbow)
    reach = _leader_reach(text, text_size, frame)
    return ((float(elbow[0]) + sign * (DIMENSION_SHELF_MM + reach), float(elbow[1])), 0.0)


def leader_dimension_frame_circle(tip: Point, elbow: Point, text: str, text_size: float,
                                  frame: str = "none") -> Optional[tuple]:
    """@brief The circle KiCad draws around a leader's text, or None.

    @param tip Arrow tip, mm.
    @param elbow Elbow of the leader, mm.
    @param text The free text of the leader.
    @param text_size Text height, mm.
    @param frame "none", "rect" or "circle".
    @return ((cx, cy), radius) mm for a circle frame around a non-empty text
        (radius: half the inflated text box width minus half the text stroke,
        `PCB_DIM_LEADER::updateGeometry`); None otherwise.
    """
    if frame != "circle" or not text:
        return None
    centre, _angle = leader_dimension_label_pose(tip, elbow, text, text_size, frame)
    return (centre, _leader_reach(text, text_size, frame))


def leader_dimension_ink(tip: Point, elbow: Point, text: str, text_size: float, frame: str = "none") -> list:
    """@brief Every straight stroke KiCad draws for a leader, mm.

    @param tip Arrow tip, mm.
    @param elbow Elbow of the leader, mm.
    @param text The free text of the leader.
    @param text_size Text height, mm.
    @param frame "none", "rect" or "circle".
    @return Segments: tip to elbow, the arrowhead at the tip pointing at it,
        the shelf from the elbow to the text frame, and the four sides of a
        rectangle frame (the text box inflated by half a text size across
        and by two text pens up and down, `PCB_DIM_LEADER::updateGeometry`)
        around a non-empty text. A
        circle frame is not a segment: `leader_dimension_frame_circle` gives it.
    """
    tip = (float(tip[0]), float(tip[1]))
    elbow = (float(elbow[0]), float(elbow[1]))
    sign = _shelf_sign(tip, elbow)
    segments = [(tip, elbow)]
    segments.extend(_arrow_strokes(tip, (elbow[0] - tip[0], elbow[1] - tip[1])))
    segments.append((elbow, (elbow[0] + sign * DIMENSION_SHELF_MM, elbow[1])))
    if frame == "rect" and text:
        (x, y), _angle = leader_dimension_label_pose(tip, elbow, text, text_size, frame)
        half_w, half_h = dimension_text_half_box(text, text_size)
        hw, hh = half_w + float(text_size) / 2.0, half_h + 2.0 * dimension_text_pen(text_size)
        corners = [(x - hw, y - hh), (x + hw, y - hh), (x + hw, y + hh), (x - hw, y + hh)]
        segments.extend((corners[i], corners[(i + 1) % 4]) for i in range(4))
    return segments


@dataclass
class FpDimension:
    """@brief Aligned dimension of a footprint (KiCad `dimension (type aligned)`, allowed in footprints since 7).

    @param start First measured point, mm.
    @param end Second measured point, mm.
    @param height Signed distance of the dimension line from the measured
        points, mm (`dimension_offset_direction` gives the side; verified
        against KiCad 10's own render with kicad-cli, 2026-11-06).
    @param layer KiCad layer of the dimension.
    @param width Line thickness, mm.
    @param text_size Height of the value text, mm.
    @param axis "" for an aligned dimension; "x" or "y" for an orthogonal
        one (`type orthogonal`, `orientation` 0 or 1) that measures |dX| or
        |dY| between the points, as a linear measure of the ruler does.
    @param dtype "" (default) for the aligned/orthogonal dimension chosen by
        `axis`, written exactly as before the field existed; "radial"
        (`start` = circle centre, `end` = a rim point), "center" (`start` =
        centre, `end` = end of one arm) or "leader" (`start` = arrow tip,
        `end` = elbow). "aligned" and "orthogonal" are accepted as synonyms
        of "" with the matching `axis`. `height` and `axis` apply only to
        aligned/orthogonal.
    @param leader_length Radial only: distance, mm, from the rim point to the knee.
    @param text Leader only: the free text shown ("" shows nothing).
    @param frame Leader only: "none", "rect" or "circle" (`LEADER_FRAMES`).
    """

    start: Point
    end: Point
    height: float
    layer: str
    width: float = USER_DIMENSION_WIDTH_MM
    text_size: float = USER_DIMENSION_TEXT_MM
    axis: str = ""
    dtype: str = ""
    leader_length: float = DIMENSION_RADIAL_LEADER_MM
    text: str = ""
    frame: str = "none"

    def kind(self) -> str:
        """@brief The KiCad dimension type this item writes.

        @return One of `DIMENSION_TYPES`: "orthogonal" for a legacy item
            with an axis, "aligned" for one without, else `dtype`.
        """
        if self.dtype in ("", "aligned", "orthogonal"):
            return "orthogonal" if self.axis else "aligned"
        return self.dtype

    def ink_segments(self) -> list:
        """@brief Every straight stroke KiCad draws for this dimension, mm.

        @return The segments of the type's one geometry function
            (`dimension_ink_segments`, `radial_dimension_ink`,
            `center_dimension_ink`, `leader_dimension_ink`); a leader's
            circle frame comes from `frame_circle`.
        """
        kind = self.kind()
        start, end = tuple(self.start), tuple(self.end)
        if kind == "radial":
            return radial_dimension_ink(start, end, self.leader_length, self.text_size)
        if kind == "center":
            return center_dimension_ink(start, end)
        if kind == "leader":
            return leader_dimension_ink(start, end, self.text, self.text_size, self.frame)
        return dimension_ink_segments(start, end, self.height, self.axis)

    def frame_circle(self) -> Optional[tuple]:
        """@brief The circle around a leader's text, ((cx, cy), radius) mm, or None.

        @return `leader_dimension_frame_circle` for a leader; None for every other type.
        """
        if self.kind() != "leader":
            return None
        return leader_dimension_frame_circle(tuple(self.start), tuple(self.end), self.text,
                                             self.text_size, self.frame)

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this dimension to a KiCad S-expression.

        @return The `dimension` node: type (aligned, or orthogonal with its
            orientation), layer, uuid, pts, height, the value as gr_text
            placed as `dimension_label_pose` (KiCad recomputes it on load
            anyway), a millimetre format with two decimals and KiCad's
            default style (text outside the line, arrows and extension
            overshoot from the shared constants). Radial, centre and leader
            dimensions follow KiCad 10's writer
            (`PCB_IO_KICAD_SEXPR::format(PCB_DIMENSION_BASE*)`): pts,
            leader_length (radial), format (none for a centre mark), style,
            then the gr_text (none for a centre mark).
        @throws ValueError For coincident points (no direction to draw), an
            unknown axis, type or leader frame.
        """
        if tuple(self.start) == tuple(self.end):
            raise ValueError(tx("Una quota richiede due punti distinti."))
        if self.dtype not in ("",) + DIMENSION_TYPES:
            raise ValueError(tx("Tipo di quota non valido: {dtype}", dtype=self.dtype))
        if self.dtype in ("radial", "center", "leader"):
            return self._typed_sexpr()
        if self.axis not in ("", "x", "y") or (self.dtype == "aligned" and self.axis) \
                or (self.dtype == "orthogonal" and not self.axis):
            raise ValueError(tx("Asse della quota non valido: {axis}", axis=self.axis))
        (tx_, ty_), angle = dimension_label_pose(self.start, self.end, self.height, self.text_size, self.axis)
        node = sx("dimension", sx("type", Symbol("orthogonal" if self.axis else "aligned")),
                  sx("layer", self.layer), sx("uuid", _uuid()))
        node.add(sx("pts", sx("xy", *self.start), sx("xy", *self.end)))
        node.add(sx("height", self.height))
        if self.axis:
            # KiCad: orientation 0 measures along X (horizontal line), 1 along Y.
            node.add(sx("orientation", 0 if self.axis == "x" else 1))
        text = sx("gr_text", dimension_text(self.start, self.end, self.axis))
        text.add(_at_node((round(tx_, 4), round(ty_, 4)), round(angle, 2)))
        text.add(sx("layer", self.layer))
        text.add(sx("uuid", _uuid()))
        text.add(_text_effects(self.text_size, DIMENSION_TEXT_THICKNESS_MM))
        node.add(text)
        node.add(sx("format", sx("units", 2), sx("units_format", 1), sx("precision", 2)))
        node.add(sx("style", sx("thickness", self.width), sx("arrow_length", DIMENSION_ARROW_LENGTH_MM),
                    sx("text_position_mode", 0), sx("extension_height", DIMENSION_EXTENSION_HEIGHT_MM),
                    sx("extension_offset", 0), Symbol("keep_text_aligned")))
        return node

    def _typed_sexpr(self) -> SExpr:
        """The `dimension` node of a radial, centre or leader dimension, in KiCad 10's own order."""
        kind = self.dtype
        if kind == "radial" and not (math.isfinite(float(self.leader_length)) and float(self.leader_length) > 0):
            raise ValueError(tx("Lunghezza del richiamo della quota non valida."))
        if kind == "leader" and self.frame not in LEADER_FRAMES:
            raise ValueError(tx("Cornice del richiamo non valida: {frame}", frame=self.frame))
        start, end = tuple(self.start), tuple(self.end)
        node = sx("dimension", sx("type", Symbol(kind)), sx("layer", self.layer), sx("uuid", _uuid()))
        node.add(sx("pts", sx("xy", *start), sx("xy", *end)))
        if kind == "radial":
            node.add(sx("leader_length", self.leader_length))
        if kind == "radial":
            # Prefix «R », millimetres with the unit suffix: KiCad shows «R 1.50 mm».
            node.add(sx("format", sx("prefix", "R "), sx("suffix", ""), sx("units", 2),
                        sx("units_format", 1), sx("precision", 2)))
        elif kind == "leader":
            # The leader's text is KiCad's override value, shown without units.
            node.add(sx("format", sx("prefix", ""), sx("suffix", ""), sx("units", 2),
                        sx("units_format", 0), sx("precision", 2), sx("override_value", self.text)))
        style = sx("style", sx("thickness", self.width), sx("arrow_length", DIMENSION_ARROW_LENGTH_MM),
                   sx("text_position_mode", 0))
        if kind == "leader":
            style.add(sx("text_frame", LEADER_FRAMES[self.frame]))
        style.add(sx("extension_offset", 0))
        if kind == "radial":
            style.add(sx("keep_text_aligned", YES))
        node.add(style)
        if kind == "center":
            # A centre mark has no text (KiCad writes no format and no gr_text).
            return node
        if kind == "radial":
            (tx_, ty_), angle = radial_dimension_label_pose(start, end, self.leader_length, self.text_size)
            shown = radial_dimension_text(start, end)
        else:
            (tx_, ty_), angle = leader_dimension_label_pose(start, end, self.text, self.text_size, self.frame)
            shown = self.text
        text = sx("gr_text", shown)
        text.add(_at_node((round(tx_, 4), round(ty_, 4)), round(angle, 2)))
        text.add(sx("layer", self.layer))
        text.add(sx("uuid", _uuid()))
        text.add(_text_effects(self.text_size, DIMENSION_TEXT_THICKNESS_MM))
        node.add(text)
        return node


def bezier_point(control: Sequence[Point], t: float) -> Point:
    """@brief A point of a cubic Bezier curve.

    @param control The four control points (start, c1, c2, end), mm.
    @param t Curve parameter, 0 at the start, 1 at the end.
    @return (x, y) mm.
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = ((float(x), float(y)) for x, y in control)
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3)


def bezier_points(control: Sequence[Point], segments: int) -> list:
    """@brief Points along a cubic Bezier curve at evenly spaced parameters.

    @param control The four control points, mm.
    @param segments Number of segments (at least 1).
    @return segments + 1 points from the start to the end.
    """
    count = max(1, int(segments))
    return [bezier_point(control, i / count) for i in range(count + 1)]


def bezier_bounds(control: Sequence[Point]) -> Tuple[float, float, float, float]:
    """@brief Exact axis-aligned bounding box, mm, of a cubic Bezier curve.

    @param control The four control points, mm.
    @return (x_min, y_min, x_max, y_max) of the drawn curve: its ends plus
        the points where dx/dt or dy/dt vanishes inside (0, 1), not the
        control polygon (which only bounds it from outside).
    """
    pts = [(float(x), float(y)) for x, y in control]
    params = [0.0, 1.0]
    for axis in (0, 1):
        p0, p1, p2, p3 = (p[axis] for p in pts)
        # dB/dt / 3 = A t^2 + B t + C with a = p1 - p0, b = p2 - p1, c = p3 - p2.
        a, b, c = p1 - p0, p2 - p1, p3 - p2
        qa, qb, qc = a - 2 * b + c, 2 * (b - a), a
        if abs(qa) < 1e-12:
            if abs(qb) > 1e-12:
                params.append(-qc / qb)
        else:
            disc = qb * qb - 4 * qa * qc
            if disc >= 0:
                root = math.sqrt(disc)
                params.extend(((-qb + root) / (2 * qa), (-qb - root) / (2 * qa)))
    points = [bezier_point(pts, t) for t in params if 0.0 <= t <= 1.0]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class FpCurve:
    """@brief Cubic Bezier curve on a footprint layer (fp_curve).

    @param points The four control points in order: start, first control,
        second control, end, mm (KiCad's `pts` of a BEZIER shape).
    """

    points: Sequence[Point]
    layer: str
    width: float = 0.12

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this curve to a KiCad S-expression.

        @return The `fp_curve` node with its four `pts`, stroke, layer and a
            fresh uuid, as KiCad 10 writes a footprint BEZIER shape (no fill).
        @throws ValueError Without exactly four control points.
        """
        if len(self.points) != 4:
            raise ValueError(tx("Una curva di Bézier richiede quattro punti."))
        node = sx("fp_curve", sx("pts", *(sx("xy", x, y) for x, y in self.points)))
        node.add(_stroke_node(self.width))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


def textbox_margin(width: float, size: float) -> float:
    """@brief KiCad's default inner margin of a text box, mm.

    @param width Border stroke width, mm.
    @param size Text height, mm.
    @return Half the border stroke plus 0.75 text heights, rounded to 0.1 um
        (`PCB_TEXTBOX::GetLegacyTextMargin`, the default of a new text box).
    """
    return round(float(width) / 2.0 + float(size) * 0.75, 4) + 0.0


@dataclass
class FpTextBox:
    """@brief Text box on a footprint layer (fp_text_box): a rectangle with a text inside.

    @param start One corner of the box, mm.
    @param end The opposite corner, mm.
    @param text The text (may hold newlines: a KiCad text box is multi-line).
    @param layer KiCad layer.
    @param size Text height, mm.
    @param thickness Text stroke thickness, mm.
    @param width Border stroke width, mm.
    @param border True draws the rectangle's border, False only the text.
    @param angle 0 or 90, degrees counter-clockwise: the text's angle;
        written as ``(angle 90)`` only when non-zero, as KiCad does.
    """

    start: Point
    end: Point
    text: str
    layer: str
    size: float = 1.0
    thickness: float = 0.15
    width: float = 0.1
    border: bool = True
    angle: float = 0

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this text box to a KiCad S-expression.

        @return The `fp_text_box` node in KiCad 10's order
            (`PCB_IO_KICAD_SEXPR::format(PCB_TEXTBOX*)`): text, start, end,
            margins (`textbox_margin` on the four sides), optional angle,
            layer, uuid, effects (text justified left and top inside the
            box), border, stroke, knockout no.
        """
        margin = textbox_margin(self.width, self.size)
        node = sx("fp_text_box", self.text, sx("start", *self.start), sx("end", *self.end))
        node.add(sx("margins", margin, margin, margin, margin))
        if self.angle:
            node.add(sx("angle", self.angle))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        node.add(_text_effects(self.size, self.thickness, ("left", "top")))
        node.add(sx("border", bare(bool(self.border))))
        node.add(_stroke_node(self.width))
        node.add(sx("knockout", bare(False)))
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
#: silkscreen, assembly documentation, free comments and user drawings, the
#: drawable layers of `footprint_layers.LAYERS` in table order. Copper and
#: courtyard are deliberately excluded: they carry electrical and DRC
#: meaning the preview must not let a free sketch change.
USER_DRAW_LAYERS = tuple(entry.name for entry in drawable_layers())
#: Drawing kinds a record may declare, with the exact point count each needs
#: (0 means "two or more", the open polyline of the line tool). A "curve" is
#: a cubic Bezier (start, c1, c2, end); a "textbox" is two opposite corners.
USER_DRAW_KINDS = {"line": 0, "polygon": 0, "rectangle": 2, "circle": 2, "arc": 3, "text": 1,
                   "dimension": 2, "curve": 4, "textbox": 2}
#: Default distance, mm, of the dimension line from the measured points when
#: a measure is exported as a dimension (the tool itself takes it from the
#: third click): a placement default of the drawing, not a measurement.
USER_DIMENSION_OFFSET_MM = 1.0
#: Fewest points of the open-ended kinds: a line is two points, a closed
#: polygon three (its last side returns to the first point, not stored).
USER_DRAW_MIN_POINTS = {"line": 2, "polygon": 3}
#: Kinds whose record may carry ``fill: true`` (written as KiCad's
#: ``(fill yes)``); absent means an outline, as every record saved before
#: 2026-11-06, so those files stay byte-identical.
USER_DRAW_FILLABLE = ("polygon", "rectangle", "circle")
#: Stroke width, mm, of a user drawing record saved without ``width`` (KiCad's
#: default silkscreen line). Kept under this name for the records and callers
#: written before the per-layer table; a NEW drawing takes its layer's
#: default from `user_draw_width`.
USER_DRAW_WIDTH_MM = 0.12


def user_draw_width(layer_name: str) -> float:
    """@brief Default stroke width, mm, of a new user drawing on a layer.

    @param layer_name KiCad layer name («F.Fab»).
    @return The layer's default from `footprint_layers.LAYERS`: 0.12 mm on
        F.SilkS (KLC F5.1), 0.10 mm on F.Fab (KLC F5.2) and on the user
        layers (KiCad's «Other layers» default); the silkscreen width for a
        layer not in the table.
    """
    return default_width(layer_name)
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


def require_kicad_reach(reach_mm: float, number: Optional[str] = None) -> None:
    """@brief Refuse geometry that reaches beyond the coordinates KiCad can represent.

    @param reach_mm Largest |x| or |y|, mm, the geometry reaches: a pad's
        centre plus its half extent, or a whole footprint's envelope
        (courtyard included).
    @param number Pad number when `reach_mm` is one pad's reach; None when it
        is the footprint's envelope.
    @throws ValueError When `reach_mm` is not finite or exceeds
        KICAD_COORD_LIMIT_MM: KiCad would not refuse the file, it would
        silently saturate the coordinate and move the copper (review
        2026-10-29, G3).
    @note Shared by the explicit-pad model (`PinSpec.validate`) and the
        parametric generators (pin header, castellated module), so every
        path refuses with the same bound and the same wording.
    """
    reach = float(reach_mm)
    if math.isfinite(reach) and reach <= KICAD_COORD_LIMIT_MM:
        return
    if number is not None:
        raise ValueError(tx("Il pad {number} arriva a {reach:.1f} mm dall'origine, oltre i ±{limit:.0f} mm "
                            "che KiCad sa rappresentare.",
                            number=number, reach=reach, limit=KICAD_COORD_LIMIT_MM))
    raise ValueError(tx("Il footprint arriva a {reach:.1f} mm dall'origine, oltre i ±{limit:.0f} mm "
                        "che KiCad sa rappresentare.", reach=reach, limit=KICAD_COORD_LIMIT_MM))


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


#: Coordinate resolution, mm, of a stored drawing point (4 decimals).
_STORED_POINT_RESOLUTION_MM = 1e-4


def _polygon_has_area(points) -> bool:
    """Whether polygon vertices enclose an area: the ONE shared rule, `draw_constraints.polygon_has_area`.

    Some vertex must lie farther than one stored rounding unit from the line
    through the two vertices farthest apart. A bow tie has area by this rule
    (KiCad accepts self-intersecting outlines). The symbol editor applies the
    same function, so both tabs refuse exactly the same polygons.
    """
    return polygon_has_area([(float(p[0]), float(p[1])) for p in points],
                            resolution=_STORED_POINT_RESOLUTION_MM)


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
        A ``curve`` needs four points not all equal. A ``textbox`` needs two
        corners differing on both axes, a non-blank ``text`` (newlines
        allowed), ``size``, an optional bool ``border`` (stored only when
        False) and ``angle`` as a text's. A ``dimension`` may carry
        ``dtype`` (`DIMENSION_TYPES`): absent, "aligned" or "orthogonal"
        give the record saved before the field (dtype not stored, it must
        agree with ``axis``); "radial" stores ``leader_length``, "center"
        nothing more, "leader" ``text`` (may be empty) and ``frame`` when
        not "none". A ``polygon`` must enclose an area (`_polygon_has_area`:
        all-collinear or repeated vertices drop it; a self-intersecting one
        is kept). A field of the wrong type (a list, a dict, a number where
        a name is expected) drops the record. Keys a kind does not use are
        dropped.
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
        # A string check first: an unhashable value (a list, a dict) in a
        # dict membership test would raise instead of dropping the record.
        if not isinstance(kind, str) or not isinstance(layer, str):
            continue
        if kind not in USER_DRAW_KINDS or layer not in USER_DRAW_LAYERS:
            continue
        if not isinstance(points, (list, tuple)):
            continue
        count = USER_DRAW_KINDS[kind]
        if count and len(points) != count or not count and len(points) < USER_DRAW_MIN_POINTS[kind]:
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
        fill = record.get("fill", False)
        if not isinstance(fill, bool):
            continue
        if fill and kind in USER_DRAW_FILLABLE:
            clean["fill"] = True
        if kind == "polygon" and not _polygon_has_area(clean_points):
            # Repeated or collinear vertices enclose no area: KiCad would
            # write a polygon that draws a line, or nothing.
            continue
        if kind == "dimension":
            if tuple(clean_points[0]) == tuple(clean_points[1]):
                continue
            dtype = record.get("dtype")
            if dtype is not None and (not isinstance(dtype, str) or dtype not in DIMENSION_TYPES):
                continue
            if dtype in ("radial", "center", "leader"):
                typed = _clean_typed_dimension(record, dtype)
                if typed is None:
                    continue
                clean.update(typed)
                result.append(clean)
                continue
            # Aligned/orthogonal: two distinct points and the signed distance
            # of the dimension line from them (`offset`, KiCad's `height`): 0
            # lies on the points. An explicit dtype must agree with `axis`
            # and is not stored, so the record stays the one saved before
            # the field existed.
            offset = record.get("offset", 0.0)
            if (isinstance(offset, bool) or not isinstance(offset, (int, float))
                    or not math.isfinite(offset) or abs(offset) > TEXT_POSITION_LIMIT_MM):
                continue
            clean["offset"] = round(float(offset), 4) + 0.0
            axis = record.get("axis", "")
            if not isinstance(axis, str) or axis not in ("", "x", "y"):
                continue
            if (dtype == "aligned" and axis) or (dtype == "orthogonal" and not axis):
                continue
            if axis:
                # Orthogonal: the measured coordinates must differ along the axis.
                if clean_points[0][0 if axis == "x" else 1] == clean_points[1][0 if axis == "x" else 1]:
                    continue
                clean["axis"] = axis
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
            if anchor is not None and (not isinstance(anchor, str) or anchor != USER_TEXT_ANCHOR):
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
        elif kind == "curve":
            # Four control points that all coincide draw nothing.
            if len(set(clean_points)) < 2:
                continue
        elif kind == "textbox":
            box = _clean_textbox(record, clean_points)
            if box is None:
                continue
            clean.update(box)
        result.append(clean)
    return result


def _clean_angle(raw) -> Optional[int]:
    """The 0/90 text angle of a record value, or None when it must drop the record (see `validate_footprint_drawings`)."""
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw % 90:
        return None
    angle = int(raw) % 180
    return angle if angle in USER_TEXT_ANGLES else None


def _clean_textbox(record: dict, points: list) -> Optional[dict]:
    """The text fields of a text box record (text, size, border, angle), or None to drop it.

    The corners must differ on both axes (a box with an area); the text is
    non-blank (judged without surrounding white space), may hold newlines
    but no carriage return, and is stored with only its trailing newlines
    removed, so an indented first line keeps its indentation; the size is in
    (0, 50] mm; ``border`` is a bool stored only when False (default True);
    ``angle`` follows the text record's rule (0/90, stored only when 90).
    """
    if points[0][0] == points[1][0] or points[0][1] == points[1][1]:
        return None
    text = record.get("text")
    if not isinstance(text, str) or not text.strip() or "\r" in text:
        return None
    size = record.get("size", USER_DRAW_TEXT_MM)
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or not 0 < size <= 50:
        return None
    border = record.get("border", True)
    if not isinstance(border, bool):
        return None
    angle = _clean_angle(record.get("angle"))
    if angle is None:
        return None
    clean = {"text": text.rstrip("\n"), "size": round(float(size), 4)}
    if not border:
        clean["border"] = False
    if angle:
        clean["angle"] = angle
    return clean


def _clean_typed_dimension(record: dict, dtype: str) -> Optional[dict]:
    """The fields of a radial, centre or leader dimension record, or None to drop it.

    radial: ``leader_length`` finite in (0, `TEXT_POSITION_LIMIT_MM`] mm,
    default `DIMENSION_RADIAL_LEADER_MM`, always stored; centre: no field;
    leader: ``text`` a string without line breaks (stripped, "" allowed:
    KiCad then shows nothing), always stored, and ``frame`` one of
    `LEADER_FRAMES`, stored only when not "none". ``offset`` and ``axis``
    belong to aligned/orthogonal dimensions and are dropped.
    """
    clean = {"dtype": dtype}
    if dtype == "radial":
        length = record.get("leader_length", DIMENSION_RADIAL_LEADER_MM)
        if (isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(length)
                or not 0 < length <= TEXT_POSITION_LIMIT_MM):
            return None
        clean["leader_length"] = round(float(length), 4) + 0.0
    elif dtype == "leader":
        text = record.get("text", "")
        if not isinstance(text, str) or "\n" in text or "\r" in text:
            return None
        frame = record.get("frame", "none")
        if not isinstance(frame, str) or frame not in LEADER_FRAMES:
            return None
        clean["text"] = text.strip()
        if frame != "none":
            clean["frame"] = frame
    return clean


def drawing_graphics(records) -> list:
    """@brief Convert validated user drawing records into footprint graphics.

    @param records Records in the shape `validate_footprint_drawings` returns
        (the caller validates; a malformed record here is a programming error).
    @return Graphic items (FpLine/FpPoly/FpRect/FpCircle/FpArc/FpText/
        FpDimension/FpCurve/FpTextBox) ready for
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
        fill = bool(record.get("fill", False))
        if kind == "line":
            items.extend(FpLine(start=a, end=b, layer=layer, width=width)
                         for a, b in zip(points, points[1:]))
        elif kind == "polygon":
            items.append(FpPoly(points=points, layer=layer, width=width, fill=fill))
        elif kind == "dimension":
            items.append(FpDimension(start=points[0], end=points[1], height=record.get("offset", 0.0),
                                     layer=layer, width=width, axis=record.get("axis", ""),
                                     dtype=record.get("dtype", ""),
                                     leader_length=record.get("leader_length", DIMENSION_RADIAL_LEADER_MM),
                                     text=record.get("text", ""), frame=record.get("frame", "none")))
        elif kind == "curve":
            items.append(FpCurve(points=points, layer=layer, width=width))
        elif kind == "textbox":
            size = record.get("size", USER_DRAW_TEXT_MM)
            # Text stroke 15 % of the height, the KLC ratio of every default text.
            items.append(FpTextBox(start=points[0], end=points[1], text=record["text"], layer=layer,
                                   size=size, thickness=round(float(size) * 0.15, 4), width=width,
                                   border=record.get("border", True), angle=record.get("angle", 0)))
        elif kind == "rectangle":
            items.append(FpRect(start=points[0], end=points[1], layer=layer, width=width, fill=fill))
        elif kind == "circle":
            radius = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
            items.append(FpCircle(center=points[0], radius=round(radius, 4),
                                  layer=layer, width=width, fill=fill))
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


def record_dimension(record: dict) -> "FpDimension":
    """@brief The `FpDimension` a validated dimension record writes, for its geometry.

    @param record A validated ``dimension`` record.
    @return The item `drawing_graphics` builds for it; its `ink_segments`,
        `frame_circle` and the type's label pose are what the writer, the
        checks and the preview share.
    """
    points = [tuple(p) for p in record["points"]]
    return FpDimension(start=points[0], end=points[1], height=record.get("offset", 0.0),
                       layer=record["layer"], width=record.get("width", USER_DIMENSION_WIDTH_MM),
                       axis=record.get("axis", ""), dtype=record.get("dtype", ""),
                       leader_length=record.get("leader_length", DIMENSION_RADIAL_LEADER_MM),
                       text=record.get("text", ""), frame=record.get("frame", "none"))


def dimension_label(dimension: "FpDimension") -> Optional[tuple]:
    """@brief Text and pose of a dimension's value as written, for every type.

    @param dimension An `FpDimension`.
    @return (text, (x, y), angle_deg) with (x, y) the text centre; None for
        a centre mark (no text). The pose functions are the writer's own
        (`dimension_label_pose`, `radial_dimension_label_pose`,
        `leader_dimension_label_pose`).
    """
    kind = dimension.kind()
    start, end = tuple(dimension.start), tuple(dimension.end)
    if kind == "center":
        return None
    if kind == "radial":
        centre, angle = radial_dimension_label_pose(start, end, dimension.leader_length, dimension.text_size)
        return (radial_dimension_text(start, end), centre, angle)
    if kind == "leader":
        centre, angle = leader_dimension_label_pose(start, end, dimension.text, dimension.text_size,
                                                    dimension.frame)
        return (dimension.text, centre, angle)
    centre, angle = dimension_label_pose(start, end, dimension.height, dimension.text_size, dimension.axis)
    return (dimension_text(start, end, dimension.axis), centre, angle)


def drawing_bounds(record: dict) -> Tuple[float, float, float, float]:
    """@brief Axis-aligned bounding box, mm, of what a shape record draws.

    @param record A validated shape record (line, rectangle, circle, arc,
        polygon, curve, text box or dimension).
    @return (x_min, y_min, x_max, y_max) mm, Y down: a line's, a
        rectangle's and a text box's points; a circle's centre +- radius; an arc's start and
        end plus every quarter point (0, 90, 180, 270 degrees on its circle)
        the arc passes through, so the box is the drawn arc's, not its three
        stored points'; a curve's exact box (`bezier_bounds`); an
        aligned/orthogonal dimension's points and crossbar; a radial, centre
        or leader dimension's points and every stroke of its ink (frame
        included, text excluded).
    @note A text has no drawn box here: its geometry depends on the font
        (its anchor is its stored point).
    """
    points = [(float(x), float(y)) for x, y in record["points"]]
    kind = record["kind"]
    if kind == "circle":
        (cx, cy), (rx, ry) = points
        radius = math.hypot(rx - cx, ry - cy)
        points = [(cx - radius, cy - radius), (cx + radius, cy + radius)]
    elif kind == "dimension" and record.get("dtype") in ("radial", "center", "leader"):
        # Everything KiCad draws but the text: crosses, leaders, shelves, frames.
        dimension = record_dimension(record)
        points = points + [p for segment in dimension.ink_segments() for p in segment]
        circle = dimension.frame_circle()
        if circle is not None:
            (cx, cy), radius = circle
            points += [(cx - radius, cy - radius), (cx + radius, cy + radius)]
    elif kind == "dimension":
        # The measured points and the dimension line beyond them.
        points = points + list(dimension_crossbar(points[0], points[1], record.get("offset", 0.0),
                                                  record.get("axis", "")))
    elif kind == "curve":
        x0, y0, x1, y1 = bezier_bounds(points)
        points = [(x0, y0), (x1, y1)]
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
        `drawing_transform.bounds_centre`); a text's anchor point; the
        circle centre of a radial dimension and the marked centre of a
        centre mark (the first point: they turn about what they mark); the
        centre of a leader's two stored points (its text side is derived
        from them, so its ink box is not a fixed frame to turn about).
    """
    if record["kind"] == "text" or (record["kind"] == "dimension"
                                    and record.get("dtype") in ("radial", "center")):
        x, y = record["points"][0]
        return (float(x), float(y))
    if record["kind"] == "dimension" and record.get("dtype") == "leader":
        return bounds_centre(record["points"])
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
        symbol editor's texts); both senses give the same toggle. A text
        box: its corners turn about its centre (it stays axis aligned) and
        its ``angle`` toggles like a text's. A radial, centre or leader
        dimension: its two points turn about `drawing_centre`; its derived
        parts (knee, shelf, text) follow from them.
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
    if record["kind"] == "textbox":
        # The box turns about its centre and its text with it (0 <-> 90).
        if record.get("angle", 0):
            clean.pop("angle", None)
        else:
            clean["angle"] = 90
    if _is_linear_dimension(record):
        _carry_dimension_offset(record, clean, lambda pts: rotate_points_90(
            pts, drawing_centre(record), counter_clockwise_on_screen),
            swap_axis=True)
    return clean


def _is_linear_dimension(record: dict) -> bool:
    """True for an aligned/orthogonal dimension record, the ones carrying an ``offset``."""
    return record["kind"] == "dimension" and record.get("dtype") in (None, "", "aligned", "orthogonal")


def _carry_dimension_offset(record: dict, clean: dict, transform, swap_axis: bool = False) -> None:
    """Give `clean` (a transformed copy of the dimension `record`) the offset that keeps its line in place.

    The dimension line is carried as a point: the old crossbar start is
    transformed with the measured points and the new offset is read back
    from it (`dimension_offset_from_crossbar`), so a turn keeps the line
    where the user sees it and a mirror lands it on the mirrored side,
    whatever the kind of dimension. An orthogonal dimension swaps its axis
    under a quarter turn.
    """
    axis = record.get("axis", "")
    old_start, old_end = (tuple(p) for p in record["points"][:2])
    crossbar_start = dimension_crossbar(old_start, old_end, record.get("offset", 0.0), axis)[0]
    moved = transform([list(old_start), list(crossbar_start)])
    new_axis = {"x": "y", "y": "x"}.get(axis, axis) if swap_axis else axis
    new_start, new_end = (tuple(p) for p in clean["points"][:2])
    clean["offset"] = round(dimension_offset_from_crossbar(new_start, new_end, tuple(moved[1]), new_axis), 4) + 0.0
    if new_axis:
        clean["axis"] = new_axis


def mirror_drawing(record: dict) -> dict:
    """@brief A copy of a shape record mirrored left-right about its bounding-box centre.

    @param record A validated drawing record.
    @return A new record: a shape's points mirrored about the vertical line
        through the centre of its `drawing_bounds`
        (`drawing_transform.mirror_points_x`; an arc keeps its start/mid/end
        order, the mirrored three points define the mirrored arc); a text is
        returned unchanged (a copy): KiCad never mirrors text glyphs and its
        anchor is the mirror's own axis point (plan decision C-M1). A text
        box keeps its box (mirrored about its own centre) and its text
        unmirrored; a curve's control points are mirrored; a leader's shelf
        and text move to the side away from the mirrored tip.
    """
    clean = dict(record)
    clean["points"] = [list(p) for p in record["points"]]
    if record["kind"] == "text":
        return clean
    clean["points"] = mirror_points_x(record["points"], drawing_centre(record)[0])
    if _is_linear_dimension(record):
        # A reflection flips the handedness of `dimension_normal`: the line
        # is carried as a point, so it lands on the mirrored side.
        axis_x = drawing_centre(record)[0]
        _carry_dimension_offset(record, clean, lambda pts: mirror_points_x(pts, axis_x))
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
    @return A new record. A rectangle or a text box keeps its centre and which corner is
        stored first; a circle keeps its centre and the direction of its rim
        point, and so do a radial dimension (radius = centre to rim) and a
        centre mark (radius = arm length). Any other kind, or a size that does not apply to the kind, is
        returned unchanged (a copy). A non-positive size gives a degenerate
        record the caller's validation refuses.
    """
    clean = dict(record)
    points = [[float(x), float(y)] for x, y in record["points"]]
    clean["points"] = [list(p) for p in record["points"]]
    radial = record["kind"] == "circle" or (record["kind"] == "dimension"
                                            and record.get("dtype") in ("radial", "center"))
    if record["kind"] in ("rectangle", "textbox") and (width is not None or height is not None):
        (x0, y0), (x1, y1) = points
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        half_w = abs(x1 - x0) / 2.0 if width is None else float(width) / 2.0
        half_h = abs(y1 - y0) / 2.0 if height is None else float(height) / 2.0
        sign_x = 1.0 if x0 >= x1 else -1.0
        sign_y = 1.0 if y0 >= y1 else -1.0
        clean["points"] = [[round(cx + sign_x * half_w, 4) + 0.0, round(cy + sign_y * half_h, 4) + 0.0],
                           [round(cx - sign_x * half_w, 4) + 0.0, round(cy - sign_y * half_h, 4) + 0.0]]
    elif radial and radius is not None:
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
    #: Attribute flags written after the mounting type in `(attr ...)`, a
    #: subset of `FOOTPRINT_ATTRIBUTE_FLAGS` in any order (written in KiCad's
    #: order, once each). Empty for an ordinary component. Validated by
    #: `to_sexpr`, so a flag typed wrong is refused before a file is written.
    attributes: Sequence[str] = ()
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

        flags = normalised_attribute_flags(self.attributes)
        if self.attr or flags:
            # KiCad accepts `(attr exclude_from_bom)` without a mounting type
            # (a logo, a mounting hole): the type is written first when known.
            root.add(sx("attr", *([Symbol(self.attr)] if self.attr else []),
                        *(Symbol(flag) for flag in flags)))

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
    """Points bounding a footprint graphic (circles by their radius); [] for texts.

    A dimension is bounded by what KiCad draws for it, not by its two
    defining points: every ink stroke end (`FpDimension.ink_segments`), a
    leader's frame circle and the corners of its value's text box (turned by
    the text angle), so a Reference/Value placed from the envelope clears a
    radial knee, a leader's shelf and a label beside the crossbar.
    """
    if isinstance(item, FpText):
        return []
    if isinstance(item, FpDimension):
        points = [p for segment in item.ink_segments() for p in segment]
        circle = item.frame_circle()
        if circle is not None:
            (cx, cy), r = circle
            points += [(cx - r, cy - r), (cx + r, cy + r)]
        label = dimension_label(item)
        if label is not None and label[0]:
            text, (lx, ly), angle = label
            half_w, half_h = dimension_text_half_box(text, item.text_size)
            # KiCad angles turn counter-clockwise on a Y-down page.
            c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            for ex, ey in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)):
                points.append((lx + ex * c + ey * s, ly - ex * s + ey * c))
        return points
    if isinstance(item, FpCurve):
        x0, y0, x1, y1 = bezier_bounds(item.points)
        return [(x0, y0), (x1, y1)]
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
        excluded; a dimension counts with its whole ink, frame and value
        text box); None when there is neither, or a value is not finite.
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
