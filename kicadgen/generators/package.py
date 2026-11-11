"""@brief KiCad footprint generator from explicit pad coordinates.

The geometry comes from core.package_geometry (already normalized to the top
view and to KiCad's rotation convention); this module only turns it into a
footprint with the same layer conventions as the other generators: pads,
silkscreen body outline kept off the copper, fabrication outline, courtyard
around body and pads, and a pin-1 marker next to the pad numbered "1" (or
"A1" of a grid numbering, `core.silk.pin_one`; none, with a note in the
description, when no pad is pin 1).
"""

from __future__ import annotations

import math

from ..core.footprint import (COURTYARD_WIDTH_MM, FAB_WIDTH_MM, SILK_OUTSIDE_FAB_MM, SILK_WIDTH_MM, Footprint, FpLine,
                              FpRect, Pad, courtyard_corners, fab_reference_text)
from ..core.package_geometry import ROUNDRECT_RRATIO, PackageGeometry, pad_outline
from ..core.silk import (PIN_ONE_UNMARKED_NOTE, clip_segment, keepout_box, marker_ink_box, pin_one,
                         pin_one_marker, silk_rectangle)
from ..core.tht_estimates import unverified_values
from ..services.messages import tx

#: Note recorded in the footprint description (fixed file language, see
#: `core.footprint.FOOTPRINT_TEXT_LANGUAGE`) when the hole, size or shape of
#: some pads is not a reading of the drawing but derived or estimated by the
#: program (`PinSpec.sources`): an estimate, never a measure. `{numbers}` is
#: the list of those pads as `estimated_pad_list` writes it.
ESTIMATED_PADS_NOTE = "foro o piazzola stimati sui pad {numbers}: da verificare sul disegno"

#: Most entries (single numbers or ranges) the note lists by name; beyond
#: them it gives a count (review 2026-11-11, K7: a large part with every pad
#: estimated wrote hundreds of numbers into the one-line `descr`). A
#: presentation bound, not a geometric threshold: the full list is in the
#: footprint preview's tooltips and in the checks.
ESTIMATED_PADS_LISTED = 8

#: Tail of the note when entries were left out (fixed file language).
ESTIMATED_PADS_MORE = "e altri {count}"

#: Count of estimated pads that have no number (mechanical holes): they
#: cannot be named, but the note must not hide them (fixed file language).
ESTIMATED_PADS_UNNAMED = "{count} senza numero"


def estimated_pad_list(numbers) -> str:
    """@brief The pad numbers of ESTIMATED_PADS_NOTE: unique, ranges compressed, bounded.

    @param numbers Pad numbers in footprint order (text; several pads may
        share one number).
    @return "" when no pad is given; otherwise the comma-separated
        entries: blank numbers are not listed as empty entries (a mechanical
        hole without a number has no name to cite) but counted at the end
        (ESTIMATED_PADS_UNNAMED), repeated numbers once, runs of three or more
        consecutive integers written «first-last» (pads 1, 2, 3, 4 -> «1-4»),
        textual numbers («A1», «EP») as they are. At most
        ESTIMATED_PADS_LISTED entries are written; the remaining pads are
        counted (ESTIMATED_PADS_MORE).
    @note Only integer numbers form ranges: «A1»-«A3» is not a well-defined
        run (grid numbering skips letters), so text stays listed.
    """
    seen, unique, unnamed = set(), [], 0
    for number in numbers:
        text = str(number).strip()
        if not text:
            unnamed += 1
        elif text not in seen:
            seen.add(text)
            unique.append(text)
    # Each entry is (text, number of pads it names).
    entries: list[tuple[str, int]] = []
    index = 0
    while index < len(unique):
        text = unique[index]
        if not text.isdigit():
            entries.append((text, 1))
            index += 1
            continue
        end = index
        while (end + 1 < len(unique) and unique[end + 1].isdigit()
               and int(unique[end + 1]) == int(unique[end]) + 1):
            end += 1
        run = end - index + 1
        if run >= 3:
            entries.append((f"{text}-{unique[end]}", run))
        else:
            entries.extend((unique[i], 1) for i in range(index, end + 1))
        index = end + 1
    listed = entries[:ESTIMATED_PADS_LISTED]
    words = ", ".join(text for text, _count in listed)
    left = sum(count for _text, count in entries[ESTIMATED_PADS_LISTED:])
    if left:
        words += " " + ESTIMATED_PADS_MORE.format(count=left)
    if unnamed:
        tail = ESTIMATED_PADS_UNNAMED.format(count=unnamed)
        words = f"{words}, {tail}" if words else tail
    return words

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


def preview_grid_origin(geometry: PackageGeometry) -> tuple:
    """@brief Origin of the 2D preview grid of a package, mm.

    @param geometry Package model.
    @return Position of the first numbered pin as seen from the top
        (pins_seen_from_top, the coordinates the pads are written at), or
        (0, 0) without one. Used by the generator for the default text
        anchors and by the preview drawer for its grid: one definition.
    """
    return _first_pin_origin(geometry.pins_seen_from_top())


def _first_pin_origin(pins) -> tuple:
    """Position of the first numbered pin of `pins` (already seen from the top), or (0, 0).

    Shared by `preview_grid_origin` and `build_package`, which already holds
    the pins and must not validate and transform the geometry again."""
    first = next((pin for pin in pins if str(pin.number).strip()), None)
    return (0.0, 0.0) if first is None else (float(first.x_mm), float(first.y_mm))


def build_package(geometry: PackageGeometry, name: str | None = None, *,
                  datasheet: str = "", description: str = "", tags: str = "",
                  courtyard_margin: float | None = None) -> Footprint:
    """@brief Build the footprint of a package described by explicit pad coordinates.

    @param geometry Validated package model; pins are normalized to the top
        view by pins_seen_from_top().
    @param name Footprint name; defaults to geometry.name.
    @param datasheet Datasheet URL or path for the "Datasheet" property; ""
        when unknown.
    @param description The project's own description for `descr` and the
        "Description" property; "" keeps the generic family text. The note
        about an estimated body centre is appended in both cases, and so is
        ESTIMATED_PADS_NOTE naming the pads whose hole, size or shape is
        derived or estimated (`PinSpec.sources`).
    @param tags The project's keywords for the footprint `tags`; "" (or
        blanks) keeps the package family, as before the keywords were passed
        (review 2026-10-15, R2-M2: the project keywords never reached the
        footprint on the package and host-mounted paths).
    @param courtyard_margin Courtyard clearance around body, pads and pin-1
        dot, mm. None (default) takes the geometry's own
        `courtyard_margin_mm` when it declares one, else COURTYARD_MARGIN_MM
        (0.25, KLC general). Host lands for pin headers
        (`host_mounting.HostLandGeometry`) declare the connector clearance,
        `pin_header.COURTYARD_MARGIN_MM` (0.5 mm, user decision 2026-11-03),
        so a module on headers gets the same courtyard whether it is built as
        a PinHeaderParams or as a package.
    @return Footprint whose pads keep the source numbers and relative
        positions; the footprint origin is the (possibly estimated) body
        centre, see PackageGeometry.body_frame_offset. A ``${REFERENCE}``
        text on F.Fab marks the body centre (core.footprint.fab_reference_text);
        Reference/Value anchored bottom-left at the KiCad corner of
        core.footprint.default_text_positions (place_default_texts). Each
        pad carries its pin's `lead` and `sources` as metadata that the
        .kicad_mod does not hold.
    @throws ValueError When the geometry does not validate, the courtyard
        margin is not a finite number >= 0, or the pin-1 marker cannot be
        placed off the copper.
    @note The courtyard encloses the pin-1 dot's ink too (`marker_ink_box`):
        the dot sits outside the pad, and at some package angles it used to
        lie beyond the courtyard (review 2026-11-03, N4).
    """
    if courtyard_margin is None:
        declared = getattr(geometry, "courtyard_margin_mm", None)
        courtyard_margin = COURTYARD_MARGIN_MM if declared is None else declared
    if (isinstance(courtyard_margin, bool) or not isinstance(courtyard_margin, (int, float))
            or not math.isfinite(courtyard_margin) or courtyard_margin < 0):
        raise ValueError(tx("Il margine del courtyard deve essere >= 0."))
    # pins_seen_from_top validates the geometry first: one validation per
    # build, where there used to be three (an explicit validate, this call
    # and preview_grid_origin), each with the O(n^2) pad-overlap scan
    # (review 2026-10-29, G4).
    pins = geometry.pins_seen_from_top()
    footprint_name = name or geometry.name
    description = description.strip() or f"{geometry.family} package generico"
    if geometry.origin != "body_center":
        # Fixed file language (see FOOTPRINT_TEXT_LANGUAGE): the estimate is
        # recorded in the library itself, not only in the session log.
        description += "; centro del corpo stimato dal campo dei pad"
    # A mechanical hole is not a pin: it does not get the pin 1 marker.
    electrical = [pin for pin in pins if pin.pad_type != "np_thru_hole"]
    first = pin_one(pin.number for pin in electrical)
    if first is None and any(str(pin.number).strip() for pin in electrical):
        # No pad is pin 1: no marker is guessed, and the file says why.
        description += "; " + PIN_ONE_UNMARKED_NOTE
    # Blank numbers dropped, repeats once, ranges and a bound (K7).
    unverified = estimated_pad_list(pin.number for pin in pins if unverified_values(pin.sources))
    if unverified:
        # Recorded in the library itself like the estimated body centre
        # (fixed file language): a hole, pad size or pad shape the program
        # derived or estimated is never passed for a measure. Absent for a
        # geometry without provenance, whose file stays byte-identical.
        description += "; " + ESTIMATED_PADS_NOTE.format(numbers=unverified)
    # The project's keywords when given, like the pin-header and castellated
    # generators; otherwise the family, so a project without keywords keeps a
    # byte-identical file.
    fp = Footprint(name=footprint_name, description=description, tags=tags.strip() or geometry.family,
                   datasheet=datasheet, attr=footprint_attr(p.pad_type for p in pins))

    # Lists, not dicts keyed by number: several pads may share one number
    # (split thermal pad, several GND lands) and each needs its own keepout.
    boxes = []
    for pin in pins:
        corners = pad_outline(pin.x_mm, pin.y_mm, pin.pad_length_mm,
                              pin.pad_width_mm, pin.rotation_deg)
        boxes.append((min(x for x, _ in corners), min(y for _, y in corners),
                      max(x for x, _ in corners), max(y for _, y in corners)))
    silk_keepouts = [keepout_box(*box, line_width=SILK_WIDTH_MM) for box in boxes]
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
        # The silk outline is the body grown by SILK_OUTSIDE_FAB_MM, turned
        # like the body: it lies just outside the F.Fab line, never on it,
        # and is still cut where it would reach a pad.
        g = SILK_OUTSIDE_FAB_MM
        silk_corners = [(round(cx, 6) + 0.0, round(cy, 6) + 0.0) for cx, cy in pad_outline(
            0.0, 0.0, geometry.body_length_mm + 2 * g, geometry.body_width_mm + 2 * g,
            geometry.orientation_deg)]
        if abs(((geometry.orientation_deg + 45.0) % 90.0) - 45.0) < 1e-9:
            # Multiple of 90 degrees: still an axis-aligned rectangle.
            start, end = (extent[0], extent[1]), (extent[2], extent[3])
            silk_xs, silk_ys = [cx for cx, _ in silk_corners], [cy for _, cy in silk_corners]
            for graphic in silk_rectangle((min(silk_xs), min(silk_ys)), (max(silk_xs), max(silk_ys)),
                                          silk_keepouts, width=SILK_WIDTH_MM):
                fp.add_graphic(graphic)
            fp.add_graphic(FpRect(start=start, end=end, layer="F.Fab", width=FAB_WIDTH_MM))
        else:
            for i in range(4):
                for piece_start, piece_end in clip_segment(silk_corners[i], silk_corners[(i + 1) % 4],
                                                           silk_keepouts):
                    fp.add_graphic(FpLine(start=piece_start, end=piece_end,
                                          layer="F.SilkS", width=SILK_WIDTH_MM))
                fp.add_graphic(FpLine(start=corners[i], end=corners[(i + 1) % 4], layer="F.Fab",
                                      width=FAB_WIDTH_MM))
    for x0, y0, x1, y1 in boxes:
        extent = [min(extent[0], x0), min(extent[1], y0), max(extent[2], x1), max(extent[3], y1)]

    if first is not None:
        # With several pads numbered like pin 1 the first one listed is used.
        index = next(i for i, p in enumerate(pins)
                     if p.number == first and p.pad_type != "np_thru_hole")
        pin = pins[index]
        # Outside the pad, on the side opposite to the package centre: it is the
        # free side of pin 1 (outwards for an edge pin).
        dx, dy = pin.x_mm, pin.y_mm
        if math.hypot(dx, dy) < 1e-9:
            dx, dy = -1.0, -1.0
        box = boxes[index]
        reach = math.hypot(box[2] - box[0], box[3] - box[1]) / 2 + 0.5
        norm = math.hypot(dx, dy)
        center = (pin.x_mm + dx / norm * reach, pin.y_mm + dy / norm * reach)
        marker = pin_one_marker(center, marker_keepouts, (dx, dy))
        fp.add_graphic(marker)
        x0, y0, x1, y1 = marker_ink_box(marker)
        extent = [min(extent[0], x0), min(extent[1], y0), max(extent[2], x1), max(extent[3], y1)]

    if all(math.isfinite(v) for v in extent):
        m = float(courtyard_margin)
        # KLC F5.3: courtyard corners on the 0.01 mm grid, rounded outwards
        # so the clearance is never below the margin.
        courtyard_start, courtyard_end = courtyard_corners((extent[0] - m, extent[1] - m),
                                                           (extent[2] + m, extent[3] + m))
        fp.add_graphic(FpRect(start=courtyard_start, end=courtyard_end, layer="F.CrtYd", width=COURTYARD_WIDTH_MM))
        # KLC F5.2 reference on F.Fab: centred on the body (the footprint
        # origin) when its size is known, otherwise on the pad field.
        if geometry.body_length_mm and geometry.body_width_mm:
            anchor = (0.0, 0.0)
            size = (geometry.body_length_mm, geometry.body_width_mm)
        else:
            anchor = (round((extent[0] + extent[2]) / 2, 6), round((extent[1] + extent[3]) / 2, 6))
            size = (extent[2] - extent[0], extent[3] - extent[1])
        fp.add_graphic(fab_reference_text(anchor, size))

    for pin in pins:
        # A slot (two different hole sides) is written as `(drill oval w h)`;
        # a round hole, or a slot whose sides are equal, as `(drill d)`.
        slot = pin.hole_sizes() if pin.is_slot() else None
        fp.add_pad(Pad(
            # KiCad's mechanical holes carry no net: written without a
            # number. The model keeps the number, so a pad made NPTH in the
            # preview can be made plated again and keeps its name.
            number="" if pin.pad_type == "np_thru_hole" else pin.number,
            pad_type=pin.pad_type, shape=pin.shape,
            at=(pin.x_mm, pin.y_mm), size=(pin.pad_length_mm, pin.pad_width_mm),
            layers=PAD_LAYERS[pin.pad_type], rotation=pin.rotation_deg,
            drill=None if slot is not None else pin.drill_mm, drill_slot=slot,
            roundrect_rratio=ROUNDRECT_RRATIO if pin.shape == "roundrect" else None,
            # Metadata for the checks and the preview, not written to the
            # file: the declared terminal and the provenance of the values.
            lead=pin.lead, sources=pin.sources,
        ))
    # Decision D1 (user, 2026-10-14): Reference above and Value below the
    # envelope; a user-chosen position is applied afterwards (place_texts).
    return fp.place_default_texts(_first_pin_origin(pins))
