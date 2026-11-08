"""@brief Per-layer defaults of the footprint drawing layers: colour, stroke width, text size.

One Qt-free table (`LAYERS`) for every layer the footprint preview shows a
user drawing or a generated outline on. The writer, the generators, the
checks and the preview read their default stroke widths, text sizes and
colours from it, so a value is changed in one place only.

Sources:
- Stroke widths and text sizes of F.SilkS, F.Fab and F.CrtYd: KiCad Library
  Conventions (KLC) F5.1 (silkscreen 0.12 mm, text 1.0 mm / 0.15 mm), F5.2
  (fabrication 0.10 mm, text 1.0 mm / 0.15 mm), F5.3 (courtyard 0.05 mm).
- Cmts.User and Dwgs.User: KiCad's «Other layers» defaults of Board Setup >
  Text & Graphics (line 0.10 mm, text 1.0 mm / 0.15 mm); the KLC sets no value.
- Colours: KiCad's built-in default colour theme
  (`common/settings/builtin_color_themes.h`, KiCad 10): F_SilkS (242, 237, 161),
  F_Fab (175, 175, 175), Cmts_User (89, 148, 220), Dwgs_User (194, 194, 194),
  F_CrtYd (255, 38, 226).

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
    @param side «front»: every layer of this table is a front layer (back
        layers are out of scope, plan section 1).
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
    # KLC F5.2: fabrication lines 0.10 mm, texts 1.0 mm with 0.15 mm stroke.
    DrawLayer("F.Fab", "Fabbricazione (documentazione)", "#afafaf", 0.10, 1.0, 0.15, True),
    # KiCad "Other layers" defaults (Board Setup > Text & Graphics).
    DrawLayer("Cmts.User", "Commenti (solo progetto)", "#5994dc", 0.10, 1.0, 0.15, True),
    # KiCad "User.Drawings" (file name Dwgs.User), "Other layers" defaults.
    DrawLayer("Dwgs.User", "Disegni utente (solo progetto)", "#c2c2c2", 0.10, 1.0, 0.15, True),
    # KLC F5.3: courtyard lines 0.05 mm; written by the generators only.
    DrawLayer("F.CrtYd", "Courtyard (ingombro)", "#ff26e2", 0.05, 1.0, 0.15, False),
)

_BY_NAME = {entry.name: entry for entry in LAYERS}


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
