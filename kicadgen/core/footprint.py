"""@brief KiCad footprint (.kicad_mod) data model and writer.

Modello dati e generatore di footprint KiCad (.kicad_mod).

Fornisce classi ad alto livello (Pad, FpLine, FpRect, FpCircle, FpText)
che si convertono in S-expression tramite core.sexpr, e la classe
Footprint che le assembla nel formato file completo accettato da KiCad
(pcbnew) versione 10.x.

Esempio minimo:

    from kicadgen.core.footprint import Footprint, Pad

    fp = Footprint(name="MY_CONN", description="Connettore di test")
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
# This writer has no custom-pad anchor/primitives representation.
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


def _text_effects(size: float = 1.0, thickness: float = 0.15) -> SExpr:
    return sx("effects", sx("font", sx("size", size, size), sx("thickness", thickness)))


@dataclass
class Pad:
    """@brief Footprint pad: number, type, shape, position, size, layers and optional drill.

    Piazzola (pad) di un footprint.

    - number: nome/numero pin (es. "1", "A2", "" per pad meccaniche senza rete)
    - pad_type: "thru_hole" | "smd" | "np_thru_hole"
    - shape: "circle" | "rect" | "oval" | "roundrect"
    - at: posizione (x, y) in mm relativa all'origine del footprint
    - size: (larghezza, altezza) in mm
    - layers: lista layer KiCad, es. ["*.Cu", "*.Mask"] per THT o ["F.Cu", "F.Paste", "F.Mask"] per SMD
    - drill: diametro foro in mm (obbligatorio per thru_hole/np_thru_hole)
    - roundrect_rratio: rapporto di arrotondamento per shape="roundrect" (0..0.5)
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
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpLine:
    """@brief Graphic line segment on a footprint layer.

    Segmento grafico (bordo silkscreen/fab/courtyard ecc.).
    """

    start: Point
    end: Point
    layer: str
    width: float = 0.12

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this line graphic to a KiCad S-expression."""
        node = sx("fp_line", sx("start", *self.start), sx("end", *self.end))
        node.add(_stroke_node(self.width))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpRect:
    """@brief Graphic rectangle on a footprint layer, optionally filled.

    Rettangolo grafico (es. contorno serigrafia).
    """

    start: Point
    end: Point
    layer: str
    width: float = 0.12
    fill: bool = False

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this rectangle graphic to a KiCad S-expression."""
        node = sx("fp_rect", sx("start", *self.start), sx("end", *self.end))
        node.add(_stroke_node(self.width))
        node.add(sx("fill", bare(self.fill)))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpCircle:
    """@brief Graphic circle on a footprint layer, optionally filled.

    Cerchio grafico (es. marcatore pin 1).
    """

    center: Point
    radius: float
    layer: str
    width: float = 0.12
    fill: bool = False

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this circle graphic to a KiCad S-expression."""
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

    Arco grafico (fp_arc): inizio, punto intermedio e fine, come lo scrive KiCad.
    """

    start: Point
    mid: Point
    end: Point
    layer: str
    width: float = 0.12

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this arc graphic to a KiCad S-expression."""
        node = sx("fp_arc", sx("start", *self.start), sx("mid", *self.mid),
                  sx("end", *self.end))
        node.add(_stroke_node(self.width))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        return node


@dataclass
class FpText:
    """@brief Graphic text item on a footprint layer.

    Testo grafico (usato per riferimenti extra, non Reference/Value).
    """

    kind: str  # "user" | "reference" | "value"
    text: str
    at: Point
    layer: str
    size: float = 1.0
    thickness: float = 0.15

    def to_sexpr(self) -> SExpr:
        """@brief Serialize this text graphic to a KiCad S-expression."""
        node = sx("fp_text", Symbol(self.kind), self.text)
        node.add(_at_node(self.at))
        node.add(sx("layer", self.layer))
        node.add(sx("uuid", _uuid()))
        node.add(_text_effects(self.size, self.thickness))
        return node


#: Visible footprint texts whose position the user may choose (the KiCad
#: properties drawn on the board), in the order they are written.
TEXT_KEYS = ("Reference", "Value")
#: Where they go unless moved: the positions every footprint had before they
#: became movable (user, 2026-10-13), so files without a choice do not change.
DEFAULT_TEXT_POSITIONS = {"Reference": (0.0, -2.0), "Value": (0.0, 2.0)}
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


def validate_footprint_drawings(raw) -> list:
    """@brief Keep the well-formed records of saved footprint user drawings.

    @param raw Whatever the project file holds under `footprint_drawings`.
    @return List of {kind, points, layer, width[, text, size]} records whose
        kind and layer are known, whose points are pairs of finite numbers
        within `TEXT_POSITION_LIMIT_MM` in the count the kind requires, and
        whose text (text kind only) is one non-blank line of positive size;
        anything else is left out, like a malformed pad override.
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
            clean["text"] = text.strip()
            clean["size"] = round(float(size), 4)
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
        result.append(clean)
    return result


def drawing_graphics(records) -> list:
    """@brief Convert validated user drawing records into footprint graphics.

    @param records Records in the shape `validate_footprint_drawings` returns
        (the caller validates; a malformed record here is a programming error).
    @return Graphic items (FpLine/FpRect/FpCircle/FpArc/FpText) ready for
        `Footprint.add_graphic`, in record order; a line record yields one
        FpLine per segment. Points are absolute footprint mm, drawn as-is.
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
            items.append(FpText(kind="user", text=record["text"], at=points[0],
                                layer=layer, size=record.get("size", USER_DRAW_TEXT_MM)))
    return items


@dataclass
class Footprint:
    """@brief Complete KiCad footprint that renders and saves as a .kicad_mod file.

    Footprint KiCad completo, esportabile come file .kicad_mod.

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

    pads: list = field(default_factory=list)
    graphics: list = field(default_factory=list)  # FpLine | FpRect | FpCircle | FpText

    def place_texts(self, positions) -> "Footprint":
        """@brief Put the Reference and Value texts where the user chose; the others keep their default.

        @param positions {key: (x, y)} mm in footprint coordinates (y down),
            keys of `TEXT_KEYS`; None or {} changes nothing.
        @return self, for chaining.
        @throws ValueError On an unknown key or a position that does not validate.
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
        return self

    def add_pad(self, pad: Pad) -> "Footprint":
        """@brief Append a pad and return self for chaining."""
        self.pads.append(pad)
        return self

    def add_graphic(self, item) -> "Footprint":
        """@brief Append a graphic item (line, rectangle, circle or text) and return self for chaining."""
        self.graphics.append(item)
        return self

    def _property_node(self, key: str, value: str, at: Point, layer: str, hide: bool = False) -> SExpr:
        node = sx("property", key, value)
        node.add(_at_node(at))
        node.add(sx("unlocked", YES))
        node.add(sx("layer", layer))
        if hide:
            node.add(sx("hide", YES))
        node.add(sx("uuid", _uuid()))
        node.add(_text_effects())
        return node

    def to_sexpr(self) -> SExpr:
        """@brief Serialize the complete footprint to a KiCad S-expression."""
        root = sx("footprint", self.name)
        root.add(sx("version", KICAD_VERSION))
        root.add(sx("generator", "kicadgen_tool"))
        root.add(sx("generator_version", GENERATOR_VERSION))
        root.add(sx("layer", "F.Cu"))
        if self.description:
            root.add(sx("descr", self.description))
        if self.tags:
            root.add(sx("tags", self.tags))

        root.add(self._property_node("Reference", "REF**", self.reference_at, "F.SilkS"))
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
        """@brief Return the complete .kicad_mod file text, newline-terminated."""
        return self.to_sexpr().render(0) + "\n"

    def save(self, path: str) -> None:
        """@brief Write the .kicad_mod file to `path` atomically, creating missing folders.

        @param path Destination .kicad_mod path.
        @throws OSError When the folder cannot be created or the file cannot
            be written or replaced; the previous file, if any, is left intact.
        @note The text is written to a temporary sibling and moved onto
            `path` with fsutil.replace_file: an interrupted write (full disk,
            killed process) never leaves a truncated footprint that KiCad or
            the library installer would pick up.
        """
        import os
        import tempfile

        from ..fsutil import replace_file

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
