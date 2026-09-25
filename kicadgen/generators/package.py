"""@brief KiCad footprint generator from explicit pad coordinates.

The geometry comes from core.package_geometry (already normalized to the top
view and to KiCad's rotation convention); this module only turns it into a
footprint with the same layer conventions as the other generators: pads,
silkscreen body outline kept off the copper, fabrication outline, courtyard
around body and pads, and a pin-1 marker next to the pad numbered "1".
"""

from __future__ import annotations

import math

from ..core.footprint import Footprint, FpRect, Pad
from ..core.package_geometry import PackageGeometry, pad_outline
from ..core.silk import keepout_box, pin_one, pin_one_marker, silk_rectangle

#: Courtyard clearance around body and pads, mm (KiCad Library Convention
#: F5.3 default for ordinary SMD/THT parts).
COURTYARD_MARGIN_MM = 0.25

#: KiCad layer sets per pad type. A non-plated hole carries no copper and no
#: paste; KiCad itself writes NPTH pads with "*.Cu" "*.Mask" (the copper
#: layers only say on which layers the hole exists). Edge connector pads
#: ("connect") carry no paste.
PAD_LAYERS = {
    "smd": ["F.Cu", "F.Paste", "F.Mask"],
    "thru_hole": ["*.Cu", "*.Mask"],
    "np_thru_hole": ["*.Cu", "*.Mask"],
    "connect": ["F.Cu", "F.Mask"],
}


def footprint_attr(pad_types) -> str | None:
    """@brief KiCad footprint attribute for a set of pad types.

    @param pad_types Iterable of Pad.pad_type values.
    @return "through_hole" when any pad is plated through-hole, otherwise
        "smd" when any pad is SMD, otherwise None (only mechanical holes or
        edge-connector fingers: neither mounting technology applies).
    @note Non-plated holes never make a footprint through-hole: they hold
        no pin and need no soldering.
    """
    types = set(pad_types)
    if "thru_hole" in types:
        return "through_hole"
    if "smd" in types:
        return "smd"
    return None


def build_package(geometry: PackageGeometry, name: str | None = None) -> Footprint:
    """@brief Build the footprint of a package described by explicit pad coordinates.

    @param geometry Validated package model; pins are normalized to the top
        view by pins_seen_from_top().
    @param name Footprint name; defaults to geometry.name.
    @return Footprint whose pads keep the source numbers and positions.
    @throws ValueError When the geometry does not validate.
    """
    geometry.validate()
    pins = geometry.pins_seen_from_top()
    footprint_name = name or geometry.name
    fp = Footprint(name=footprint_name, description=f"{geometry.family} package generico",
                   tags=geometry.family, attr=footprint_attr(p.pad_type for p in pins))

    outlines = {pin.number: pad_outline(pin.x_mm, pin.y_mm, pin.pad_length_mm,
                                        pin.pad_width_mm, pin.rotation_deg) for pin in pins}
    boxes = {number: (min(x for x, _ in corners), min(y for _, y in corners),
                      max(x for x, _ in corners), max(y for _, y in corners))
             for number, corners in outlines.items()}
    silk_keepouts = [keepout_box(*box, line_width=0.12) for box in boxes.values()]
    marker_keepouts = [keepout_box(*box, line_width=0.0) for box in boxes.values()]

    extent = [math.inf, math.inf, -math.inf, -math.inf]
    if geometry.body_length_mm and geometry.body_width_mm:
        x, y = geometry.body_length_mm / 2, geometry.body_width_mm / 2
        for graphic in silk_rectangle((-x, -y), (x, y), silk_keepouts):
            fp.add_graphic(graphic)
        fp.add_graphic(FpRect(start=(-x, -y), end=(x, y), layer="F.Fab", width=0.1))
        extent = [-x, -y, x, y]
    for x0, y0, x1, y1 in boxes.values():
        extent = [min(extent[0], x0), min(extent[1], y0), max(extent[2], x1), max(extent[3], y1)]

    # Un foro meccanico non e' un pin: non riceve il marcatore del pin 1.
    first = pin_one(pin.number for pin in pins if pin.pad_type != "np_thru_hole")
    if first is not None:
        pin = next(p for p in pins if p.number == first)
        # Fuori dal pad, dalla parte opposta al centro del package: e' il lato
        # libero del pin 1 (verso l'esterno per un pin di bordo).
        dx, dy = pin.x_mm, pin.y_mm
        if math.hypot(dx, dy) < 1e-9:
            dx, dy = -1.0, -1.0
        box = boxes[first]
        reach = math.hypot(box[2] - box[0], box[3] - box[1]) / 2 + 0.5
        norm = math.hypot(dx, dy)
        center = (pin.x_mm + dx / norm * reach, pin.y_mm + dy / norm * reach)
        marker = pin_one_marker(center, marker_keepouts, (dx, dy))
        fp.add_graphic(marker)

    if all(math.isfinite(v) for v in extent):
        m = COURTYARD_MARGIN_MM
        fp.add_graphic(FpRect(start=(round(extent[0] - m, 6), round(extent[1] - m, 6)),
                              end=(round(extent[2] + m, 6), round(extent[3] + m, 6)),
                              layer="F.CrtYd", width=0.05))

    for pin in pins:
        fp.add_pad(Pad(
            number=pin.number,
            pad_type=pin.pad_type, shape=pin.shape,
            at=(pin.x_mm, pin.y_mm), size=(pin.pad_length_mm, pin.pad_width_mm),
            layers=PAD_LAYERS[pin.pad_type], rotation=pin.rotation_deg, drill=pin.drill_mm,
            roundrect_rratio=0.25 if pin.shape == "roundrect" else None,
        ))
    return fp
