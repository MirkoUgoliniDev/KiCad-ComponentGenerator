"""@brief Per-layer defaults of the footprint drawing layers: colour, stroke width, text size.

One Qt-free table (`LAYERS`) for every layer the footprint preview shows a
user drawing or a generated outline on. The writer, the generators, the
checks and the preview read their default stroke widths, text sizes and
colours from it, so a value is changed in one place only.

Sources:
- Stroke widths and text sizes of F.SilkS, F.Fab and F.CrtYd: KiCad Library
  Conventions (KLC) F5.1 (silkscreen 0.12 mm, text 1.0 mm / 0.15 mm), F5.2
  (fabrication 0.10 mm, text 1.0 mm / 0.15 mm), F5.3 (courtyard 0.05 mm).
  B.SilkS and B.Fab take the values of their front twins: the KLC states
  the silkscreen and fabrication rules per layer kind, not per side.
- Cmts.User and Dwgs.User: KiCad's «Other layers» defaults of Board Setup >
  Text & Graphics (line 0.10 mm, text 1.0 mm / 0.15 mm); the KLC sets no value.
- Colours: KiCad's built-in default colour theme
  (`common/settings/builtin_color_themes.h`, KiCad 10): F_SilkS (242, 237, 161),
  F_Fab (175, 175, 175), Cmts_User (89, 148, 220), Dwgs_User (194, 194, 194),
  F_CrtYd (255, 38, 226), B_SilkS (232, 178, 167), B_Fab (88, 93, 132),
  F_Cu (200, 52, 52), B_Cu (77, 127, 196) (`s_defaultTheme`, read from
  gitlab.com/kicad/code/kicad master on 2026-11-09).
- Order: each back layer follows its front twin, as KiCad's own layer list
  pairs them (F.SilkS, B.SilkS, ... F.Fab, B.Fab).
- A text on a back layer is written mirrored (`is_back_layer`): KiCad's
  drawing tool sets `m_Mirrored = IsBackLayer(layer)` on every new text, text
  box and dimension (pcbnew/tools/drawing_tool.cpp).

Labels are Italian source strings, translated by the interface through `tx`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class DrawLayer:
    """@brief Defaults of one footprint layer.

    @param name KiCad layer name as written in the file («F.SilkS»).
    @param label Italian source label shown in the interface (translated with `tx`).
    @param colour KiCad default theme colour, «#rrggbb».
    @param width_mm Default stroke width of a line drawn on the layer, mm.
    @param text_mm Default text height on the layer, mm.
    @param text_thickness_mm Default text stroke thickness on the layer, mm.
    @param drawable True when the user may draw on the layer from the palette;
        False for a layer only the generators write (the courtyard, user
        decision of 2026-11-06).
    @param side «front» or «back»: the board side the layer belongs to. A
        back layer is seen through the board from the top view: its texts are
        written and drawn mirrored (`is_back_layer`), and its silkscreen is
        checked against the copper on the back (`services.footprint_checks`).
    """

    name: str
    label: str
    colour: str
    width_mm: float
    text_mm: float
    text_thickness_mm: float
    drawable: bool
    side: str = "front"


#: The footprint layers, in the order the interface lists them.
LAYERS: Tuple[DrawLayer, ...] = (
    # KLC F5.1: silkscreen lines 0.12 mm, texts 1.0 mm with 0.15 mm stroke.
    DrawLayer("F.SilkS", "Serigrafia (stampata)", "#f2eda1", 0.12, 1.0, 0.15, True),
    # The back silkscreen: the same KLC F5.1 values, KiCad's B_SilkS colour.
    DrawLayer("B.SilkS", "Serigrafia lato inferiore (stampata)", "#e8b2a7", 0.12, 1.0, 0.15, True, "back"),
    # KLC F5.2: fabrication lines 0.10 mm, texts 1.0 mm with 0.15 mm stroke.
    DrawLayer("F.Fab", "Fabbricazione (documentazione)", "#afafaf", 0.10, 1.0, 0.15, True),
    # The back fabrication layer: the same KLC F5.2 values, KiCad's B_Fab colour.
    DrawLayer("B.Fab", "Fabbricazione lato inferiore (documentazione)", "#585d84", 0.10, 1.0, 0.15, True, "back"),
    # KiCad "Other layers" defaults (Board Setup > Text & Graphics).
    DrawLayer("Cmts.User", "Commenti (solo progetto)", "#5994dc", 0.10, 1.0, 0.15, True),
    # KiCad "User.Drawings" (file name Dwgs.User), "Other layers" defaults.
    DrawLayer("Dwgs.User", "Disegni utente (solo progetto)", "#c2c2c2", 0.10, 1.0, 0.15, True),
    # KLC F5.3: courtyard lines 0.05 mm; written by the generators only.
    DrawLayer("F.CrtYd", "Courtyard (ingombro)", "#ff26e2", 0.05, 1.0, 0.15, False),
)

_BY_NAME = {entry.name: entry for entry in LAYERS}

#: KiCad default theme colours of the two outer copper layers, «#rrggbb»
#: (`s_defaultTheme`: F_Cu (200, 52, 52), B_Cu (77, 127, 196)). Not drawable
#: layers: a keepout zone names them and the preview draws it in their colour.
COPPER_COLOURS = {"F.Cu": "#c83434", "B.Cu": "#4d7fc4"}


def layer(name: str) -> Optional[DrawLayer]:
    """@brief The defaults of a layer by its KiCad name.

    @param name KiCad layer name («F.Fab»).
    @return The `DrawLayer`, or None for a layer not in `LAYERS`.
    """
    return _BY_NAME.get(name)


def drawable_layers() -> Tuple[DrawLayer, ...]:
    """@brief The layers a user drawing may live on, in table order.

    @return Every `DrawLayer` with `drawable` True.
    """
    return tuple(entry for entry in LAYERS if entry.drawable)


def default_width(name: str) -> float:
    """@brief Default stroke width, mm, of a line drawn on a layer.

    @param name KiCad layer name.
    @return The layer's `width_mm`; the silkscreen width for an unknown layer
        (KiCad's own fallback line width for graphics is the silkscreen's).
    """
    entry = _BY_NAME.get(name) or _BY_NAME["F.SilkS"]
    return entry.width_mm


def default_text(name: str) -> Tuple[float, float]:
    """@brief Default text height and stroke thickness, mm, on a layer.

    @param name KiCad layer name.
    @return (text_mm, text_thickness_mm); the silkscreen values for an unknown layer.
    """
    entry = _BY_NAME.get(name) or _BY_NAME["F.SilkS"]
    return (entry.text_mm, entry.text_thickness_mm)


def is_back_layer(name: str) -> bool:
    """@brief Whether a KiCad layer lies on the back of the board.

    @param name KiCad layer name («B.SilkS»).
    @return The table's `side` == «back» for a layer of `LAYERS`; for any
        other layer KiCad's own naming, where every back layer is «B.*»
        (B.Cu, B.Mask, B.CrtYd...). A text on such a layer is mirrored.
    """
    entry = _BY_NAME.get(name)
    if entry is not None:
        return entry.side == "back"
    return isinstance(name, str) and name.startswith("B.")
