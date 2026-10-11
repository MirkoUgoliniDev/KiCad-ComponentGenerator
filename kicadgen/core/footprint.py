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
