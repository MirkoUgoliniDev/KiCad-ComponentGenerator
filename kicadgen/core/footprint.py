"""Modello dati e generatore di footprint KiCad (.kicad_mod).

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

Point = Tuple[float, float]

PAD_TYPES = {"thru_hole", "smd", "np_thru_hole", "connect"}
PAD_SHAPES = {"circle", "rect", "oval", "roundrect", "trapezoid", "custom"}


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
    """Piazzola (pad) di un footprint.

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
        """@brief Provide the to sexpr operation for this workflow."""
        """@brief Provide the to sexpr operation for this workflow."""
        """@brief Provide the to sexpr operation for this workflow."""
        """@brief Provide the to sexpr operation for this workflow."""
        """@brief Provide the to sexpr operation for this workflow."""
        """@brief Provide the to sexpr operation for this workflow."""
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
    """Segmento grafico (bordo silkscreen/fab/courtyard ecc.)."""

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
    """Rettangolo grafico (es. contorno serigrafia)."""

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
    """Cerchio grafico (es. marcatore pin 1)."""

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
    """Testo grafico (usato per riferimenti extra, non Reference/Value)."""

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
    """Footprint KiCad completo, esportabile come file .kicad_mod."""

    name: str
    description: str = ""
    tags: str = ""
    attr: Optional[str] = None  # "through_hole" | "smd" | None
    reference_at: Point = (0.0, -2.0)
    value_at: Point = (0.0, 2.0)

    pads: list = field(default_factory=list)
    graphics: list = field(default_factory=list)  # FpLine | FpRect | FpCircle | FpText

    def add_pad(self, pad: Pad) -> "Footprint":
        """@brief Provide the add pad operation for this workflow."""
        self.pads.append(pad)
        return self

    def add_graphic(self, item) -> "Footprint":
        """@brief Provide the add graphic operation for this workflow."""
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
        root.add(self._property_node("Datasheet", "", (0, 0), "F.Fab", hide=True))
        root.add(self._property_node("Description", "", (0, 0), "F.Fab", hide=True))

        if self.attr:
            root.add(sx("attr", Symbol(self.attr)))

        for item in self.graphics:
            root.add(item.to_sexpr())
        for pad in self.pads:
            root.add(pad.to_sexpr())

        return root

    def render(self) -> str:
        """@brief Provide the render operation for this workflow."""
        return self.to_sexpr().render(0) + "\n"

    def save(self, path: str) -> None:
        """Scrive il file .kicad_mod. Crea le cartelle mancanti (incluso il .pretty)."""
        import os

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.render())
