"""@brief KiCad footprint generator from explicit pad coordinates.

The geometry comes from core.package_geometry (already normalized to the top
view and to KiCad's rotation convention); this module only turns it into a
footprint with the same layer conventions as the other generators: pads,
silkscreen body outline kept off the copper, fabrication outline, courtyard
around body and pads, and a pin-1 marker next to the pad numbered "1".
"""

from __future__ import annotations

import math

from ..core.footprint import Footprint, FpLine, FpRect, Pad
from ..core.package_geometry import PackageGeometry, pad_outline
from ..core.silk import clip_segment, keepout_box, pin_one, pin_one_marker, silk_rectangle

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
    @return Footprint whose pads keep the source numbers and relative
        positions; the footprint origin is the (possibly estimated) body
        centre, see PackageGeometry.body_frame_offset.
    @throws ValueError When the geometry does not validate or the pin-1
        marker cannot be placed off the copper.
    """
    geometry.validate()
    pins = geometry.pins_seen_from_top()
    footprint_name = name or geometry.name
    description = f"{geometry.family} package generico"
    if geometry.origin != "body_center":
        # Fixed file language (see FOOTPRINT_TEXT_LANGUAGE): the estimate is
        # recorded in the library itself, not only in the session log.
        description += "; centro del corpo stimato dal campo dei pad"
    fp = Footprint(name=footprint_name, description=description,
                   tags=geometry.family, attr=footprint_attr(p.pad_type for p in pins))

    # Lists, not dicts keyed by number: several pads may share one number
    # (split thermal pad, several GND lands) and each needs its own keepout.
    boxes = []
    for pin in pins:
        corners = pad_outline(pin.x_mm, pin.y_mm, pin.pad_length_mm,
                              pin.pad_width_mm, pin.rotation_deg)
        boxes.append((min(x for x, _ in corners), min(y for _, y in corners),
                      max(x for x, _ in corners), max(y for _, y in corners)))
    silk_keepouts = [keepout_box(*box, line_width=0.12) for box in boxes]
    marker_keepouts = [keepout_box(*box, line_width=0.0) for box in boxes]

    extent = [math.inf, math.inf, -math.inf, -math.inf]
    if geometry.body_length_mm and geometry.body_width_mm:
        # The body turns with the package, exactly as its pads do
        # (pins_seen_from_top applies orientation_deg to them): before this,
        # a rotated package got rotated pads inside an unrotated outline.
        corners = [(round(cx, 6) + 0.0, round(cy, 6) + 0.0) for cx, cy in pad_outline(
            0.0, 0.0, geometry.body_length_mm, geometry.body_width_mm,
            geometry.orientation_deg)]
        xs, ys = [cx for cx, _ in corners], [cy for _, cy in corners]
        extent = [min(xs), min(ys), max(xs), max(ys)]
        if abs(((geometry.orientation_deg + 45.0) % 90.0) - 45.0) < 1e-9:
            # Multiple of 90 degrees: still an axis-aligned rectangle (at 0
            # degrees the output is byte-identical to before).
            start, end = (extent[0], extent[1]), (extent[2], extent[3])
            for graphic in silk_rectangle(start, end, silk_keepouts):
                fp.add_graphic(graphic)
            fp.add_graphic(FpRect(start=start, end=end, layer="F.Fab", width=0.1))
        else:
            sides = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
            for side_start, side_end in sides:
                for piece_start, piece_end in clip_segment(side_start, side_end, silk_keepouts):
                    fp.add_graphic(FpLine(start=piece_start, end=piece_end,
                                          layer="F.SilkS", width=0.12))
                fp.add_graphic(FpLine(start=side_start, end=side_end, layer="F.Fab", width=0.1))
    for x0, y0, x1, y1 in boxes:
        extent = [min(extent[0], x0), min(extent[1], y0), max(extent[2], x1), max(extent[3], y1)]

    # Un foro meccanico non e' un pin: non riceve il marcatore del pin 1.
    first = pin_one(pin.number for pin in pins if pin.pad_type != "np_thru_hole")
    if first is not None:
        # With several pads numbered like pin 1 the first one listed is used.
        index = next(i for i, p in enumerate(pins)
                     if p.number == first and p.pad_type != "np_thru_hole")
        pin = pins[index]
        # Fuori dal pad, dalla parte opposta al centro del package: e' il lato
        # libero del pin 1 (verso l'esterno per un pin di bordo).
        dx, dy = pin.x_mm, pin.y_mm
        if math.hypot(dx, dy) < 1e-9:
            dx, dy = -1.0, -1.0
        box = boxes[index]
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
