"""@brief Parametric generator for modules with castellated (half-hole) edge contacts.

PCB modules (isolated RS485/TTL converters, WiFi/BLE modules such as the
ESP32, nRF...) carry their contacts as plated half-holes on the edge of the
small board and are soldered onto the mother board as SMD components:
under every half-hole goes a rectangular land.

Why the matrix generator (`pin_header`) is not enough: there, all the pads
share a single `pitch` and sit on a rows x columns grid. In real modules
every side is a story of its own -- for example an RS485/TTL converter
with 3 contacts at a 5.08 mm pitch on the left side and 5 contacts at a
2.54 mm pitch on the right side cannot be represented by any matrix. Here
instead every side has its own number of pads, its own pitch and its own
numbering.

Not every module has half-holes, though: every side declares its own
contact type (`CastellatedSide.contact_style`) -- half-hole only, closed
through-hole set back from the edge only (development boards with 2.54 mm
pitch strips), or both. The type is visible in the photograph and is asked
of the model; the default is the historical behaviour (both).

Geometric conventions:

* the footprint origin is the **centre of the body** of the module (the
  standard for SMD components in KiCad), not pin 1 as in the pin header;
* the X axis is the body length, the Y axis the body width, with Y growing
  downwards as in KiCad: the "top" side is therefore at negative Y;
* every pad is `pad_length` long in the direction perpendicular to the edge
  and `pad_width` wide along the edge, and protrudes by `pad_overhang`
  beyond the module edge, so that the solder fillet stays inspectable with
  the module mounted;
* a half_and_through contact with a declared through-hole does not have two
  lands but ONE continuous stadium-shaped land (`continuous_pad`): from the
  same outer overhang to beyond the hole ring, with the hole off-centre
  inside it. This is what the photo of a real board shows (user decision of
  2026-09-23) and it is the same shape the 3D draws;
* the hole sizes (half-hole, through-hole, ring) may be declared for each
  side; the undeclared ones are a reference (AssumedSize) which, when it
  does not fit the side's pitch or behind the through-holes placed by the
  row spacing, is reduced and declared as an estimate
  (CastellatedModuleParams.side_geometry / geometry_notes). A declared size
  that does not fit is refused, never reduced;
* an ESTIMATED hole also fits inside the copper surrounding it (user
  decision of 2026-10-02: «una regola generale, non per questo PCB», i.e. a
  general rule, not one for this PCB): the half-hole
  inside the width of the side's land, the through-hole inside a ring that
  fits the pitch (or inside the declared ring), with MIN_HOLE_WALL_MM of
  copper on each side. A declared hole is not reduced;
* the same holds for the land sizes (user decision of 2026-09-28, «Riduci
  e segnala», i.e. reduce and report): an ESTIMATED width or length (AssumedSize, per side or
  the module reference) that would bring the copper of two different
  contacts closer than MIN_COPPER_GAP_MM is reduced just enough and written
  in the notes (CastellatedModuleParams.pad_sizes / geometry_notes); a
  measure read from the documents or written by the user that does not fit
  is refused as before.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence

from ..core.footprint import Footprint, FpRect, FpText, Pad, fab_reference_text
from ..core.output_names import validate_output_name
from ..core.silk import keepout_box, pin_one, pin_one_marker, silk_rectangle
from ..core.sexpr import SExpr, sx
from ..services.messages import tx

#: Minimum copper wall between the half-hole on the edge and the through hole.
#: Two through cylinders subtracted from the same solid stay distinct only if
#: the distance between the axes exceeds the sum of the radii: below that
#: threshold you do not get two features but a single keyhole open on the
#: edge, which is exactly the defect found by measuring the generated solid
#: (44 walls of 164.7 degrees instead of 44 closed holes).
#: The same wall holds between the holes of two DIFFERENT contacts (nearby
#: half-holes on the same side, nearby holes at a corner): it is the minimum
#: "web" of material between two drilled holes. Justified by manufacturing
#: practice, not by the sample: 0.2 mm (8 mil) is the order of magnitude of
#: the minimum hole-to-hole in the capability tables of standard PCB
#: manufacturers (typically 0.2-0.5 mm depending on manufacturer and nets).
#: It is a LOWER BOUND: it guarantees two distinct, manufacturable holes, not
#: a recommended distance.
MIN_HOLE_WALL_MM = 0.2

#: Minimum distance between the copper of two DIFFERENT contacts that the
#: generator leaves when it REDUCES an estimated land size (pad_sizes). It is
#: the default clearance of a KiCad project (net class «Default», clearance
#: 0.2 mm) and sits above the copper-to-copper spacing minimum of standard
#: PCB manufacturers (typically 0.1-0.15 mm): an estimate reduced down to
#: here does not trip the DRC of the host board and stays manufacturable.
#: Same value and same justification as pin_header.MIN_PAD_GAP_MM. It is a
#: manufacturing LOWER BOUND, not calibrated on any module; it applies ONLY
#: to estimates: a declared size is only checked by validate() (copper that
#: touches = rejection), never reduced.
MIN_COPPER_GAP_MM = 0.2

#: Smallest hole to which the generator REDUCES an ESTIMATED hole so that it
#: fits in the copper around it. Declared HEURISTIC, from manufacturing
#: practice and not from a module: 0.3 mm is the minimum mechanical drill of
#: the standard specifications of PCB manufacturers (0.2 mm only in advanced
#: services). Below this value the estimate is not reduced: either the
#: measured land or the hole estimate is wrong, and the note says so instead
#: of drawing a hole that nobody would drill (adversarial check of
#: 2026-10-02: land 0.45 -> hole 0.05). A declared hole size would make it
#: unnecessary.
MIN_ESTIMATED_DRILL_MM = 0.3

#: Centre of the half-hole relative to the edge, in fractions of the radius,
#: when it is not declared (historical convention: a castellated hole is a
#: hole drilled near the edge and then cut, the centre lies INSIDE the board).
HALF_HOLE_INSET_RATIO = 0.76


class AssumedSize(float):
    """@brief A size (hole or pad), in mm, that was ASSUMED or estimated rather than declared.

    Behaves exactly like the float it wraps (arithmetic, comparisons,
    formatting, JSON, copy and pickle); only its type records the provenance.
    A plain float passed by a caller (document, photo measure, user field) is
    a DECLARED size.
    @note Why a type and not a flag: the three module-level sizes have always
        been plain floats read by several callers (3D generator, contact
        zones, GUI); keeping them floats keeps every caller working, while the
        type lets validate() tell "declared, must be respected" from "assumed,
        may be reduced to fit the pitch" (see CastellatedModuleParams.side_geometry).
    @note A JSON round trip turns it into a plain float, i.e. a declared size:
        callers that persist parameters must persist the provenance themselves
        (or pass None for a size that was not declared).
    """

    __slots__ = ()


#: Reference diameters (mm) of the historical RS485/TTL sample at 2.54 pitch:
#: NOT measured on the module being processed. Used only where nobody declares
#: a dimension, and only if they fit in the pitch of the side; otherwise the
#: largest that fits is estimated and written in the notes
#: (CastellatedModuleParams.geometry_notes).
REFERENCE_HALF_HOLE_MM = AssumedSize(1.7)
REFERENCE_THRU_DRILL_MM = AssumedSize(1.0)
REFERENCE_THRU_RING_MM = AssumedSize(1.7)
#: Reference edge land (length x width, mm): the same historical land pattern
#: of the RS485/TTL sample. It is the fallback when neither the side nor the
#: module declares a size: like the holes, if it does not leave the copper of
#: nearby contacts at MIN_COPPER_GAP_MM it is reduced and written in the notes.
REFERENCE_PAD_LENGTH_MM = AssumedSize(3.6)
REFERENCE_PAD_WIDTH_MM = AssumedSize(1.8)

#: Provenance of a land size resolved for a side (PadSize).
PAD_DECLARED = "declared"             # read from the documents or written by the user
PAD_ASSUMED = "assumed"               # estimate or reference, kept as it is
PAD_ESTIMATE_PITCH = "estimate_pitch"          # estimate reduced for the side's pitch
PAD_ESTIMATE_CLEARANCE = "estimate_clearance"  # estimate reduced for another side's copper

#: Provenance of a hole dimension resolved for a side (SideGeometry).
SIZE_SIDE = "side"                    # declared on the side
SIZE_MODULE = "module"                # declared for the whole module
SIZE_REFERENCE = "reference"          # reference, not declared, it fits
SIZE_ESTIMATE_PITCH = "estimate_pitch"  # estimated: the reference did not fit in the pitch
SIZE_ESTIMATE_ROWS = "estimate_rows"    # estimated: the reference did not fit behind the through holes
SIZE_ESTIMATE_DRILL = "estimate_drill"  # estimated: the assumed ring did not surround the hole
SIZE_ESTIMATE_PAD = "estimate_pad"      # estimated: the half-hole did not fit in the side's land
SIZE_ESTIMATE_RING = "estimate_ring"    # estimated: the hole left no room for a ring

#: Provenance of the edge distance of a side's through hole.
INSET_EXPLICIT = "explicit"           # thru_hole_offset declared
INSET_ROW_SPACING = "row_spacing"     # body and row spacing declared
INSET_HALF_HOLE_ROWS = "half_hole_rows"  # the row spacing is that of the half-holes
INSET_FALLBACK = "fallback"           # declared estimate (minimum compatible)


def is_declared_size(value) -> bool:
    """@brief Tell whether a hole size was declared by a caller rather than assumed.

    @param value A size in mm, an AssumedSize, or None.
    @return False for None and for AssumedSize instances, True otherwise.
    """
    return value is not None and not isinstance(value, AssumedSize)


class PadSize(NamedTuple):
    """@brief Edge-land size resolved for one side, in mm, with its provenance.

    @note `length` is perpendicular to the edge, `width` along it; the
        `*_source` fields are PAD_* values and `*_estimate` the assumed value
        before any reduction (equal to the resolved value when nothing was
        reduced). `clearance_edge` is the edge of the other side whose copper
        forced a length reduction, else None.
    """

    length: float
    length_source: str
    length_estimate: float
    width: float
    width_source: str
    width_estimate: float
    clearance_edge: Optional[str] = None


def _ceil_hundredth(value: float) -> float:
    """Smallest 0.01 mm step not below ``value``."""
    return math.ceil(value * 100 - 1e-9) / 100


def _floor_hundredth(value: float) -> float:
    """Round DOWN to 0.01 mm, so an estimate that must fit never grows past its bound."""
    return math.floor(value * 100 + 1e-9) / 100


def _finite_positive(value) -> bool:
    """True for a finite number > 0 (bools excluded)."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)

#: Module sides on which the contacts can sit.
EDGES = ("left", "right", "top", "bottom")

#: Contact type of a side: only the half-hole cut by the edge (pure
#: castellated module, reflow-soldered on the edge lands).
CONTACT_HALF_HOLE = "half_hole"
#: Only a WHOLE plated through hole, with its ring, set back from the edge:
#: board material remains between ring and edge and the edge is straight.
#: It is the case of development boards with 2.54-pitch strips.
CONTACT_THROUGH_HOLE = "through_hole"
#: Both features on the same contact: the half-hole on the edge and a through
#: hole slightly further in, joined by ONE continuous stepped copper land
#: (as on real boards: the copper starts at the edge, where the half-hole
#: opens, and continues without interruption past the hole's ring).
#: It is the generator's default behaviour.
CONTACT_HALF_AND_THROUGH = "half_and_through"
#: Allowed values for `CastellatedSide.contact_style`. Which one is right is
#: seen in the photograph (the edge cuts the hole or not) and must be asked of
#: the model, not deduced from a dimensional threshold: see datasheet_extractor.
CONTACT_STYLES = (CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)

#: Sentence appended to the description of a footprint declared SMD that
#: carries drilled pads. KiCad considers the type inconsistent (DRC check
#: `footprint_type_mismatch`, off by default in KiCad 10) and normally exports
#: only SMD footprints to the placement file. The type stays SMD by the user's
#: choice (2026-09-24): the module is placed and reflow-soldered, so it must
#: stay in that file; the sentence tells whoever opens the library.
SMD_WITH_DRILLED_PADS_NOTE = (
    "Tipo SMD di proposito anche con piazzole forate: il modulo si posa e si "
    "salda a rifusione, i fori non ospitano pin."
)

#: Readable names of the contact types, for error messages and logs.
CONTACT_STYLE_NAMES = {
    CONTACT_HALF_HOLE: "mezzo foro sul bordo",
    CONTACT_THROUGH_HOLE: "foro passante",
    CONTACT_HALF_AND_THROUGH: "mezzo foro + foro passante",
}


def style_has_half_hole(style: str) -> bool:
    """@brief Tell whether a contact style cuts a plated half-hole into the edge.

    @param style One of CONTACT_STYLES.
    @return True for CONTACT_HALF_HOLE and CONTACT_HALF_AND_THROUGH.
    """
    return style in (CONTACT_HALF_HOLE, CONTACT_HALF_AND_THROUGH)


def style_has_through_hole(style: str) -> bool:
    """@brief Tell whether a contact style carries a closed plated through-hole.

    @param style One of CONTACT_STYLES.
    @return True for CONTACT_THROUGH_HOLE and CONTACT_HALF_AND_THROUGH.
    """
    return style in (CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH)


#: The two pairs of opposite sides: the row spacing is measured across the
#: body, so along X for left/right and along Y for top/bottom.
OPPOSITE_EDGE_PAIRS = (("left", "right"), ("top", "bottom"))


def opposite_row_edges(edges) -> Optional[tuple]:
    """@brief The single pair of opposite edges a declared row spacing refers to.

    @param edges Edges that carry contacts (any iterable of EDGES values).
    @return ("left", "right") or ("top", "bottom") when exactly one opposite
        pair carries contacts on both of its edges, otherwise None.
    @note One declared row spacing is one number: it can describe the distance
        between two facing rows, not between rows on both axes at once. With
        contacts on all four edges it cannot be attributed, and with a single
        row there is nothing for it to measure; in both cases it is not used,
        rather than being applied to an axis it may not belong to.
    """
    present = set(edges)
    pairs = [pair for pair in OPPOSITE_EDGE_PAIRS if set(pair) <= present]
    return pairs[0] if len(pairs) == 1 else None


def through_hole_inset_from_rows(extent_mm, row_spacing_mm) -> Optional[float]:
    """@brief Distance from the board edge of the through-hole centres, in mm.

    @param extent_mm Board extent across the two rows (body length for
        left/right rows, body width for top/bottom rows).
    @param row_spacing_mm Declared centre-to-centre distance between the rows.
    @return (extent - row_spacing) / 2, or None when either quantity is missing
        or not a positive finite number, or when the spacing is not smaller
        than the extent (the rows would not fit inside the board).
    @note Assumes the two rows are symmetric about the body centre, which is
        the same assumption the footprint makes: its origin is the body centre
        and every side is centred along its edge unless an offset is declared.
        Example from a real run: 25.4 mm across, rows 22.86 mm apart, so the
        centres sit 1.27 mm in from each edge. Nothing here is tuned to it.
    """
    try:
        extent = float(extent_mm)
        spacing = float(row_spacing_mm)
    except (TypeError, ValueError):
        return None
    if isinstance(extent_mm, bool) or isinstance(row_spacing_mm, bool):
        return None
    if not (math.isfinite(extent) and math.isfinite(spacing)) or extent <= 0 or spacing <= 0:
        return None
    if spacing >= extent:
        return None
    return round((extent - spacing) / 2, 4)


def fallback_through_hole_inset(pad_diameter_mm: float) -> float:
    """@brief Heuristic distance of a through-hole centre from the edge, in mm.

    @param pad_diameter_mm Diameter of the annular pad around the hole.
    @return pad radius + MIN_HOLE_WALL_MM.
    @note HEURISTIC, used only when neither an explicit offset nor a row
        spacing that can be attributed to the side is available. It is the
        SMALLEST inset compatible with what the style declares -- a closed
        ring with board material between it and the edge -- using the same
        minimum wall this module already requires between two plated
        features. It was not fitted to any board and it is a lower bound: a
        real board usually sets its rows further in (the 2.54 mm grid dev kit
        that motivated the through-hole style has 1.27 mm, this gives 1.05 mm
        with the default 1.7 mm pad). Declaring the row spacing, or the offset
        explicitly, makes it unnecessary; the 3D generator reports its use.
    """
    return round(pad_diameter_mm / 2 + MIN_HOLE_WALL_MM, 4)

#: Human-readable edge labels inserted as values into GUI and error
#: messages: Italian source words, like the `tx` templates they fill.
EDGE_LABELS = {
    "left": "sinistro",
    "right": "destro",
    "top": "superiore",
    "bottom": "inferiore",
}


class PadPlacement(NamedTuple):
    """@brief One placed contact land: number, centre and oriented size, in mm."""

    number: str
    x: float
    y: float
    width: float
    height: float
    edge: str
    # Contact type of the side: it decides WHICH land this is. For
    # CONTACT_THROUGH_HOLE the centre is that of the through hole and the sizes
    # are the ring diameter, because it is the only land the part offers; for
    # the other two it is the usual rectangular edge land.
    style: str = CONTACT_HALF_AND_THROUGH


class SideGeometry(NamedTuple):
    """@brief Hole sizes and through-hole position resolved for one side, in mm.

    Every value is what the footprint, the 3D model and validate() use for the
    contacts of that side; each carries its provenance (SIZE_* / INSET_*), so
    an estimate can always be told from a declared quantity.
    """

    #: Diameter of the plated half-hole cut by the edge.
    half_hole: float
    half_hole_source: str
    #: Distance of the half-hole centre from the edge, measured inward.
    half_hole_inset: float
    #: Drill of the closed through-hole.
    drill: float
    drill_source: str
    #: Outer diameter of the annular ring around the through-hole.
    ring: float
    ring_source: str
    #: True when the contacts of this side carry a closed through-hole.
    has_thru_holes: bool
    #: Distance of the through-hole centre from the edge (None without one).
    thru_inset: Optional[float]
    thru_inset_source: Optional[str]
    #: Inset implied by a declared row spacing attributable to this side, or None.
    rows_inset: Optional[float]


#: Unit vector towards the inside of the body for each side, in footprint
#: coordinates (Y downwards): the "top" side is at negative Y, so it enters towards +Y.
INWARD_FOOTPRINT = {"left": (1.0, 0.0), "right": (-1.0, 0.0), "top": (0.0, 1.0), "bottom": (0.0, -1.0)}


def _clean(value: float) -> float:
    """Round float noise away and turn -0.0 into 0.0, so S-expressions stay stable."""
    return round(float(value), 6) + 0.0


class ContactPad(NamedTuple):
    """@brief The ONE continuous copper land of a half_and_through contact.

    A stadium (KiCad "oval") whose long axis is perpendicular to the board
    edge: it starts at the outer end of the edge land (pad_overhang beyond the
    edge, where the solder fillet of the castellation is inspected) and runs
    inward, past the ring of the through-hole, without interruption. All
    values are in mm, footprint coordinates (origin at the body centre, Y down).

    @note The drill is NOT at the stadium centre: it sits where
        CastellatedModuleParams.thru_hole_centre puts it. In KiCad a pad's
        `at` is the HOLE position and `(drill D (offset dx dy))` moves the
        COPPER away from it, so the footprint pad is written at the drill
        centre with offset = copper centre - drill centre (see `drill_offset`).
    """

    number: str
    edge: str
    #: Centre of the stadium copper (the KiCad pad `at` plus its drill offset).
    x: float
    y: float
    #: Oriented size along X and Y (the KiCad pad `size`, no rotation).
    width: float
    height: float
    #: Through-hole drill diameter and its absolute centre.
    drill: float
    drill_x: float
    drill_y: float
    #: Centre (inside the board) and diameter of the plated half-hole at the edge.
    half_hole_x: float
    half_hole_y: float
    half_hole_diameter: float
    #: Distance from the board edge of the inner (rounded) end, measured inward.
    inner_reach: float
    #: Distance of the outer end beyond the board edge (= pad_overhang).
    outer_reach: float
    #: Stadium width, measured along the edge (the diameter of its round ends).
    across: float

    @property
    def drill_offset(self) -> tuple:
        """@brief KiCad drill offset: copper centre relative to the hole, in mm.

        @return (dx, dy) = stadium centre - drill centre, in footprint
            coordinates; the pad is never rotated, so pad-local and footprint
            axes coincide.
        @note KiCad semantics, verified with pcbnew 10: for `(at X Y)` with
            `(drill D (offset dx dy))` the hole is at (X, Y) (GetPosition) and
            the copper shape is centred at (X + dx, Y + dy) (ShapePos). Until
            2026-09-25 this returned drill - centre and the writer put `at` on
            the copper centre, which swapped hole and copper in KiCad.
        """
        return (_clean(self.x - self.drill_x), _clean(self.y - self.drill_y))

    @property
    def length(self) -> float:
        """@brief Stadium length perpendicular to the edge, in mm (outer_reach + inner_reach).

        @return outer_reach + inner_reach, mm.
        """
        return self.outer_reach + self.inner_reach

    def axis(self) -> tuple:
        """@brief Segment joining the centres of the two round ends.

        @return ((x0, y0), (x1, y1)) in mm, outer end first. A point belongs to
            the stadium when its distance from this segment is <= across / 2.
        """
        nx, ny = INWARD_FOOTPRINT[self.edge]
        half = max(self.length / 2 - self.across / 2, 0.0)
        return ((self.x - nx * half, self.y - ny * half), (self.x + nx * half, self.y + ny * half))

    def contains(self, px: float, py: float, tolerance: float = 1e-9) -> bool:
        """@brief Tell whether a point lies on the copper of this stadium.

        @param px Point X, mm (footprint coordinates).
        @param py Point Y, mm.
        @param tolerance Numeric slack, mm.
        @return True when the distance from the stadium axis is <= across / 2.
        """
        (ax, ay), (bx, by) = self.axis()
        return _point_segment_distance(px, py, ax, ay, bx, by) <= self.across / 2 + tolerance


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    """Euclidean distance from point P to segment AB, in mm."""
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class OffsetDrillPad(Pad):
    """@brief A KiCad pad whose drill sits off the pad centre.

    KiCad writes it as `(drill D (offset dx dy))`, dx/dy in mm in pad-local
    axes; it is how a single plated pad can carry one hole away from the
    middle of its copper (KiCad 6 and later, unchanged in the 8/9/10 formats).
    The pad `at` is the HOLE position and the offset moves the COPPER shape:
    copper centre = at + offset (pcbnew GetPosition() is the hole, ShapePos()
    the copper centre).
    Kept here rather than in core.footprint so that every other generator's
    output stays byte-identical.
    """

    def __init__(self, *args, drill_offset=(0.0, 0.0), **kwargs):
        """@brief Build the pad.

        @param drill_offset (dx, dy) of the copper centre from the hole
            (the pad `at`), mm, as KiCad defines the drill offset.
        @param args Forwarded to Pad.
        @param kwargs Forwarded to Pad.
        """
        super().__init__(*args, **kwargs)
        self.drill_offset = (_clean(drill_offset[0]), _clean(drill_offset[1]))

    def to_sexpr(self) -> SExpr:
        """@brief Serialize like Pad, adding `(offset dx dy)` inside `(drill ...)` when nonzero.

        @return The `pad` node of Pad.to_sexpr, whose `drill` child carries the
            offset when it is not (0, 0).
        """
        node = super().to_sexpr()
        if self.drill_offset != (0.0, 0.0):
            for child in node.args:
                if isinstance(child, SExpr) and child.name == "drill":
                    child.add(sx("offset", *self.drill_offset))
        return node


@dataclass
class ComponentBox:
    """@brief Component mounted on the module, approximated as a box for the 3D model only.

    It serves the 3D model only: a real module is not a smooth slab, and
    recognising it at a glance (the transformer, the ICs, the LEDs) helps
    to understand which way it is mounted and how much room it really takes.

    Coordinates are those of the footprint: origin at the module centre,
    X to the right, Y downwards, Z from the PCB plane upwards.
    `length` x `width` is the component's envelope in ITS OWN system (long
    axis x short axis); `rotation` orients it on the module.
    """

    x: float
    y: float
    length: float
    width: float
    height: float
    name: str = "Componente"
    color_hex: str = "#303030"
    # Package type: it decides the shape in 3D (see cad3d/freecad_parts.py).
    kind: str = "generic"
    # Text printed on the component (e.g. "SP3485", "VPT"), if readable.
    marking: str = ""
    # Orientation in the photo: `length` is the LONG axis of the component and
    # `width` the short one, always; rotation says how it sits on the module
    # (0 = long axis parallel to X, 90 = vertical). Explicit, not deduced
    # from the proportions: with almost square estimates the deduction is wrong.
    rotation: float = 0.0
    # Optional count of the observed terminals of an IC; used by the 3D model.
    pin_count: Optional[int] = None
    pins_by_side: Optional[Dict[str, int]] = None
    detected_kind: Optional[str] = None
    terminal_geometry: Optional[dict] = None
    package: str = ""
    mating: str = ""
    bbox_basis: str = "unknown"
    package_evidence: str = ""
    # Confidence/source belong to the observed contact count, not package identity.
    pin_count_confidence: str = ""
    pin_count_source: str = ""
    # Reviewed native-model placement overrides, not inferred photo pin-1.
    model_rotation_deg: Optional[float] = None
    model_z_offset_mm: float = 0.0
    # Explicit local user choice; absence never establishes a verified identity.
    model_selection: Optional[dict] = None
    model_match: Optional[dict] = None
    # Photographic evidence for mechanical matching, never placement overrides.
    match_evidence: Optional[dict] = None
    # Explicit user choice on the solid: None lets the automation decide,
    # False removes a detector false positive from the 3D, True puts back a
    # part that the automation had omitted. The row stays in the table
    # anyway: the exclusion concerns the model, not the inventory.
    render_3d_override: Optional[bool] = None
    # Manual correction of the solid's orientation, in degrees, ADDED to the
    # angle that the builder deduces from the native model. A delta stays
    # valid even if the chosen model changes; an absolute angle does not.
    model_rotation_delta_deg: float = 0.0

    def is_usable(self) -> bool:
        """@brief Tell whether the box can become a 3D body.

        @return True when position, sizes and rotation are finite numbers and
            the three sizes are positive. NaN or infinity would otherwise be
            turned into invented coordinates downstream (audit 2026-09-25, E6).
        """
        values = (self.x, self.y, self.length, self.width, self.height, self.rotation)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in values):
            return False
        return self.length > 0 and self.width > 0 and self.height > 0


@dataclass
class CastellatedSide:
    """@brief Contacts on one module edge: count, pitch, offset and pin numbering.

    `offset` shifts the group of pads along the edge with respect to the
    centred position: it serves the modules whose contacts are not
    symmetric about the body centre.

    `first_pin` and `reverse` describe the numbering: the pads are numbered
    in sequence starting from `first_pin` following the edge (top to bottom
    for the vertical sides, left to right for the horizontal ones), or in
    the opposite direction with `reverse=True`. With `pin_numbers` the
    numbers are given one by one instead, when the sequence is not regular.

    `contact_style` (one of CONTACT_STYLES) says which features every
    contact of the side carries: it decides the footprint lands and the 3D
    holes, never the numbering.

    `hole_diameter`, `thru_hole_drill` and `thru_pad_diameter` (mm, optional)
    are the hole sizes OF THIS SIDE when they have been declared (measure,
    document, user): the sides of a module may have different pitches and
    different holes. None = the module size is used (see
    CastellatedModuleParams.side_geometry).
    @note A declared side-specific size is always respected: validate()
        refuses it when it does not fit the side's pitch; it is never reduced.
    """

    edge: str
    count: int
    pitch: float
    first_pin: int = 1
    reverse: bool = False
    offset: float = 0.0
    pad_length: Optional[float] = None
    pad_width: Optional[float] = None
    pin_numbers: Optional[Sequence[str]] = None
    # Which features each contact of this side carries (see CONTACT_STYLES).
    # It is seen in the photograph and must be asked of the model; the default
    # is the historical behaviour, so every project and choice already saved
    # keeps producing exactly what it produced.
    contact_style: str = CONTACT_HALF_AND_THROUGH
    # Hole dimensions declared for this side, in mm (None = module dimension).
    hole_diameter: Optional[float] = None       # half-hole on the edge
    thru_hole_drill: Optional[float] = None     # closed through hole
    thru_pad_diameter: Optional[float] = None   # ring of the through hole

    def numbers(self) -> List[str]:
        """@brief Return the pad numbers in the order they are met along the edge.

        @return `pin_numbers` as strings when given; otherwise `count`
            consecutive numbers from `first_pin`, reversed when `reverse`.
        """
        if self.pin_numbers is not None:
            return [str(n) for n in self.pin_numbers]
        numbers = [str(self.first_pin + i) for i in range(self.count)]
        return list(reversed(numbers)) if self.reverse else numbers


@dataclass
class CastellatedModuleParams:
    """@brief Parameters of a castellated module: body size plus contacts on each edge.

    The body is the module outline (length along X, width along Y); every
    CastellatedSide in `sides` adds its own contacts, pitch and numbering.
    """

    name: str = "CastellatedModule"
    description: str = ""
    tags: str = "castellated module SMD"
    #: Datasheet URL or path for the footprint's "Datasheet" property; "" when unknown.
    datasheet: str = ""
    body_length: float = 42.8
    body_width: float = 15.2
    sides: List[CastellatedSide] = field(default_factory=list)
    # Default pad sizes, overridable side by side.
    # Default land pattern: 3.6 x 1.8 mm with 1.4 mm outside the edge, i.e.
    # the pad centred 0.4 mm outside the module -- it is the pattern already
    # verified on the RS485/TTL converter, a good starting point for similar
    # modules. The default is an AssumedSize (reference, not a measure): it
    # holds only for the sides without a size of their own and, if it does not
    # leave the copper of nearby contacts at MIN_COPPER_GAP_MM, it is reduced
    # and noted (pad_sizes). A number passed by the caller is declared.
    # None = not declared.
    pad_length: float = REFERENCE_PAD_LENGTH_MM
    pad_width: float = REFERENCE_PAD_WIDTH_MM
    pad_overhang: float = 1.4
    # Diameter of the plated half-hole on the edge: not needed by the footprint
    # (the pads are rectangular) but by the 3D model, where the half-holes must
    # be cut into the solid so that the part resembles the real one.
    # The three hole dimensions hold for every side that does not declare its
    # own. The default is an AssumedSize (reference, not a measure): if it
    # does not fit in a side's pitch it is reduced for that side and the
    # estimate ends up in the notes. A number passed by the caller is declared
    # and is respected (or rejected). None = not declared.
    hole_diameter: float = REFERENCE_HALF_HOLE_MM
    # Distance of the half-hole centre from the edge (None = 0.76 * radius): a
    # castellated hole is a normal hole drilled near the edge and then cut.
    hole_inset: Optional[float] = None
    # CLOSED through hole: dimension shared by every side that carries it.
    # How many features a contact has (half-hole only, through hole only,
    # both) is not decided here: it is `CastellatedSide.contact_style`, read
    # from the photograph. Setting the diameter to zero remains the historical
    # way to say "half-holes only" for half_and_through sides.
    thru_hole_drill: float = REFERENCE_THRU_DRILL_MM
    thru_pad_diameter: float = REFERENCE_THRU_RING_MM
    # Distance of the closed hole centre from the edge. At zero the program
    # computes it: from the declared rows when the row spacing can be
    # attributed to the side, otherwise with a declared estimate (see
    # side_geometry). An explicit number is respected, but checked.
    thru_hole_offset: float = 0.0
    # Declared distance between the centres of two contact rows on opposite
    # sides (ExtractedParams.row_spacing_mm). Needed by the sides with a
    # through hole (through_hole and half_and_through): with the body envelope
    # it gives the edge distance of the holes without inventing it.
    # None = not declared.
    row_spacing: Optional[float] = None
    # The module is not a solid block: it is a thin PCB with the components
    # on top. The 3D model builds it as two solids, because a single box as
    # tall as the tallest component neither resembles the real part nor
    # helps to evaluate the envelopes.
    pcb_thickness: float = 1.0
    component_inset: float = 1.0
    component_color_hex: str = "#303030"
    # Look of the PCB in 3D: colour of the substrate visible at the cut, silk
    # labels next to the contacts (pad number -> text) and their colour.
    substrate_color_hex: str = "#2f3f1f"
    silk_labels: Dict[str, str] = field(default_factory=dict)
    silk_color_hex: str = "#e8e8e0"
    show_silk_labels_3d: bool = False
    # Components read from the module photo: if present, the 3D draws them
    # one by one instead of the single block derived from component_inset.
    components: List[ComponentBox] = field(default_factory=list)
    margin: float = 0.25

    def __post_init__(self) -> None:
        """@brief Turn an explicit None hole or pad size into the assumed reference size.

        @note None is how a caller says "not declared" (e.g. the extraction
            did not measure it); the reference value then applies, as an
            estimate that may be reduced per side (see side_geometry).
        """
        for name, reference in (("hole_diameter", REFERENCE_HALF_HOLE_MM),
                                ("thru_hole_drill", REFERENCE_THRU_DRILL_MM),
                                ("thru_pad_diameter", REFERENCE_THRU_RING_MM),
                                ("pad_length", REFERENCE_PAD_LENGTH_MM),
                                ("pad_width", REFERENCE_PAD_WIDTH_MM)):
            if getattr(self, name) is None:
                setattr(self, name, reference)

    @staticmethod
    def _sized(side_value, module_value, reference, bounds) -> tuple:
        """Resolve one hole size: (value mm, SIZE_* source).

        A declared size (on the side, or module-wide) is returned as is, even
        when it does not fit: validate() refuses it, it is never reduced. An
        assumed size is kept when it fits every bound, otherwise it becomes
        the largest 0.01 mm step that fits (an estimate, declared in the notes
        by geometry_notes). When nothing positive fits, the assumed value is
        kept and validate() reports why.
        Precedence: side declared, module declared, side assumed (e.g. an
        estimate from the photo, AssumedSize), module assumed, reference. A
        per-side estimate is closer to the part than the historical reference
        but never outranks a measure (2026-09-26: an estimated half-hole passed
        as declared made validate() refuse the row spacing read from the
        drawing).
        """
        if side_value is not None and is_declared_size(side_value):
            return float(side_value), SIZE_SIDE
        if is_declared_size(module_value):
            return float(module_value), SIZE_MODULE
        value = float(side_value if side_value is not None
                      else reference if module_value is None else module_value)
        limits = [b for b in bounds if b is not None]
        if not limits or value <= min(limits) + 1e-9:
            return value, SIZE_REFERENCE
        best = _floor_hundredth(min(limits))
        if best <= 0:
            return value, SIZE_REFERENCE
        return best, SIZE_ESTIMATE_PITCH

    def _half_hole_inset_for(self, diameter: float) -> float:
        """Half-hole centre distance from the edge for a given diameter, mm."""
        return (self.hole_inset if self.hole_inset is not None
                else round(diameter / 2 * HALF_HOLE_INSET_RATIO, 3))

    def _smallest_estimated_half_hole(self) -> float:
        """Smallest diameter an estimated half-hole may be reduced to, mm.

        The fabrication minimum MIN_ESTIMATED_DRILL_MM, and with a declared
        hole_inset just above twice it: a smaller half-hole would not reach
        the edge (_validate_half_hole_insets refuses it).
        """
        smallest = MIN_ESTIMATED_DRILL_MM
        if self.hole_inset is not None:
            smallest = max(smallest, _floor_hundredth(2 * float(self.hole_inset)) + 0.01)
        return smallest

    @staticmethod
    def _side_land_width(side) -> Optional[float]:
        """Width (along the edge) of the side's own measured or declared land, mm, or None."""
        value = getattr(side, "pad_width", None)
        if value is None or not is_declared_size(value) or not _finite_positive(value):
            return None
        return float(value)

    def _declared_hole(self, side, name: str) -> Optional[float]:
        """The declared value of hole size `name` for `side` (side first, then module), or None."""
        value = getattr(side, name, None)
        if value is not None and is_declared_size(value):
            return float(value)
        value = getattr(self, name, None)
        return float(value) if value is not None and is_declared_size(value) else None

    def _largest_half_hole_before(self, limit: float) -> float:
        """Largest half-hole (0.01 mm steps) whose far side stays within `limit` of the edge, mm."""
        if self.hole_inset is not None:
            diameter = 2 * (limit - self.hole_inset)
        else:
            diameter = limit / (HALF_HOLE_INSET_RATIO / 2 + 0.5)
        diameter = _floor_hundredth(diameter)
        # Rounding the centre to 0.001 mm can shift it by a hair:
        # step down until the dimension really lies within the limit.
        while diameter > 0 and self._half_hole_inset_for(diameter) + diameter / 2 > limit + 1e-9:
            diameter = round(diameter - 0.01, 2)
        return diameter

    def side_of(self, placement: "PadPlacement") -> Optional[CastellatedSide]:
        """@brief The side a placed contact belongs to.

        @param placement Contact from pad_positions().
        @return The side on the same edge whose numbers include the contact's,
            else the first side on that edge, else None.
        @note Matched by edge AND pin number, which validate() keeps unique,
            so two sides declared on the same edge are still told apart.
        """
        fallback = None
        for side in self.sides:
            if side.edge != placement.edge:
                continue
            if fallback is None:
                fallback = side
            if placement.number in side.numbers():
                return side
        return fallback

    def contact_geometry(self, placement: "PadPlacement") -> SideGeometry:
        """@brief Resolved hole geometry of the side of a placed contact.

        @param placement Contact from pad_positions().
        @return side_geometry() of its side; for a placement that matches no
            side, the geometry of a single contact of its edge and style.
        """
        side = self.side_of(placement)
        if side is None:
            side = CastellatedSide(edge=placement.edge, count=1, pitch=0,
                                   contact_style=placement.style)
        return self.side_geometry(side)

    def side_geometry(self, side: CastellatedSide) -> SideGeometry:
        """@brief Hole sizes and through-hole position of the contacts of one side.

        @param side One of self.sides (or an equivalent description).
        @return SideGeometry, all lengths in mm.
        @note Sizes: the side's declared value, else the module's declared
            value, else the assumed reference (AssumedSize). An ASSUMED size
            that does not fit the side is reduced to the largest that fits,
            with the provenance set to an estimate:
            * pitch (sides with more than one contact): adjacent holes of the
              side need MIN_HOLE_WALL_MM of board between them, so half-hole,
              ring and drill are each at most pitch - MIN_HOLE_WALL_MM (for
              the ring the same margin keeps two copper rings apart);
            * an estimated ring also bounds an assumed drill to ring -
              2 * MIN_HOLE_WALL_MM (the same web, as annular ring);
            * a declared row spacing that fixes the through-hole: an assumed
              half-hole must leave MIN_HOLE_WALL_MM before that hole;
            * an assumed half-hole fits inside the side's pad width (along the
              edge) with MIN_HOLE_WALL_MM of copper on each part
              (SIZE_ESTIMATE_PAD): a hole wider than the land around it is not
              a castellation (user, 2026-10-02: an AI-estimated 2.0 mm half-hole
              in a 1.53 mm pad measured on the photo);
            * half_and_through: an assumed ring is copper of the same
              continuous pad, so it is at most the side's pad width
              (SIZE_ESTIMATE_PAD);
            * an assumed drill leaves room for a ring: within a declared ring
              or one bounded by the pad,
              ring - 2 * MIN_HOLE_WALL_MM; otherwise within the pitch,
              pitch_bound - 2 * MIN_HOLE_WALL_MM, so the assumed ring can then
              grow around it (SIZE_ESTIMATE_RING).
            A declared size is never reduced: validate() refuses it instead.
        @note Through-hole position (sides that carry one): thru_hole_offset
            when declared; else the inset implied by a row spacing that can be
            attributed to the side's opposite pair (through_hole_inset_from_rows),
            for BOTH styles with a through-hole; else a declared estimate --
            fallback_through_hole_inset for through_hole, the minimum behind
            the half-hole for half_and_through. For half_and_through, a row
            spacing whose centres could not hold a closed ring (inset < ring
            radius) cannot be the through-hole rows: the only other row that
            side has is the castellations, so it is not used to place the
            through-holes (INSET_HALF_HOLE_ROWS, declared in the notes).
        @note HEURISTIC in the estimates: MIN_HOLE_WALL_MM as the web and the
            annular ring is a fabrication lower bound, not a measure; the
            reference sizes come from one historical sample. A measured size
            per side makes all of it unnecessary.
        """
        style = side.contact_style
        count = side.count if isinstance(side.count, int) else 0
        pitch_bound = (side.pitch - MIN_HOLE_WALL_MM
                       if count > 1 and side.pitch > 0 else None)
        through = style_has_through_hole(style)
        half_style = style_has_half_hole(style)
        # «Il foro sta nel suo rame» (the hole fits in its copper; user,
        # 2026-10-02): it holds for every NOT declared hole (AI estimate or
        # historical reference) of a side whose land is evidence of the copper
        # that is there -- measured on the photo or declared FOR THAT SIDE. The
        # criterion is the provenance of the two dimensions, not the fact that
        # the AI estimated something on that side. The module's land width does
        # not count: it is the module default (the interface resets it to the
        # reference at every extraction) and in the through-hole contact it is
        # not even drawn (the land is the ring); only the side's size describes
        # the copper of those contacts.
        pad_width = self._side_land_width(side)
        declared_drill = (self._declared_hole(side, "thru_hole_drill") if through else None)

        ring, ring_source = self._sized(getattr(side, "thru_pad_diameter", None), self.thru_pad_diameter,
                                        REFERENCE_THRU_RING_MM, [pitch_bound] if through else [])
        # «mezzo foro + foro passante» (half-hole + through hole) contact: the ring
        # is copper of the same continuous land, so an ESTIMATED ring is not wider
        # than the side's land. Never, however, below the minimum ring of a
        # DECLARED hole: the hole size wins over the ring estimate, and the note
        # reports the contradiction with the land.
        if (style == CONTACT_HALF_AND_THROUGH and ring > 0 and pad_width is not None
                and ring_source not in (SIZE_SIDE, SIZE_MODULE)
                and ring > pad_width + 1e-9):
            cap = _floor_hundredth(pad_width)
            if declared_drill is not None:
                cap = max(cap, _ceil_hundredth(declared_drill + 2 * MIN_HOLE_WALL_MM))
            if cap < ring - 1e-9:
                ring, ring_source = cap, SIZE_ESTIMATE_PAD
        drill_bounds = [pitch_bound] if through else []
        if through and ring_source == SIZE_ESTIMATE_PITCH:
            drill_bounds.append(ring - 2 * MIN_HOLE_WALL_MM)
        drill, drill_source = self._sized(getattr(side, "thru_hole_drill", None), self.thru_hole_drill,
                                          REFERENCE_THRU_DRILL_MM, drill_bounds)
        if (through and drill > 0 and pad_width is not None
                and drill_source not in (SIZE_SIDE, SIZE_MODULE)):
            # A NOT declared hole must leave room for its ring: inside the declared
            # one or the one limited by the land, or else inside the pitch (and the
            # continuous land), where the assumed ring can then grow around it
            # (below). Without this, a wide AI estimate (2.0 mm at 2.54 pitch)
            # stopped footprint and 3D (user, 2026-10-02). Never below the minimum
            # drill: then it stays, and the ring grows.
            if ring_source in (SIZE_SIDE, SIZE_MODULE, SIZE_ESTIMATE_PAD):
                room = ring
            else:
                caps = [cap for cap in (pitch_bound, pad_width if style == CONTACT_HALF_AND_THROUGH
                                        else None) if cap is not None]
                room = min(caps) if caps else None
            if room is not None:
                limit = _floor_hundredth(room - 2 * MIN_HOLE_WALL_MM)
                if MIN_ESTIMATED_DRILL_MM - 1e-9 <= limit < drill - 1e-9:
                    drill, drill_source = limit, SIZE_ESTIMATE_RING
        if (through and drill > 0
                and ring_source not in (SIZE_SIDE, SIZE_MODULE)
                and ring < drill + 2 * MIN_HOLE_WALL_MM - 1e-9):
            # The ASSUMED ring (historical reference 1.7, reduced to the pitch or to
            # the land) is not a measure: it cannot reject a declared or estimated
            # hole wider than itself (HANDOFF 159: an AI-estimated hole of 1.7 against
            # a reference ring of 1.7 stopped footprint and 3D). It grows around the
            # hole with the minimum annulus, if the pitch allows it; otherwise it
            # stays and validate() rejects, saying why. If it thereby exceeds the
            # measured land, geometry_notes reports it.
            wanted = _ceil_hundredth(drill + 2 * MIN_HOLE_WALL_MM)
            if pitch_bound is None or wanted <= pitch_bound + 1e-9:
                ring, ring_source = wanted, SIZE_ESTIMATE_DRILL
        half, half_source = self._sized(getattr(side, "hole_diameter", None), self.hole_diameter,
                                        REFERENCE_HALF_HOLE_MM, [pitch_bound] if half_style else [])
        if (half_style and half > 0 and pad_width is not None
                and half_source not in (SIZE_SIDE, SIZE_MODULE)):
            # A NOT declared half-hole lies inside the land that surrounds it, with
            # the minimum copper wall on each side: on the photo the land is measured,
            # the hole is only estimated by the AI. Not below the minimum drill, nor
            # so small that a DECLARED edge distance would leave it whole inside the
            # board: then the estimate stays and the note asks for the measure.
            limit = _floor_hundredth(pad_width - 2 * MIN_HOLE_WALL_MM)
            if limit < half - 1e-9 and limit >= self._smallest_estimated_half_hole() - 1e-9:
                half, half_source = limit, SIZE_ESTIMATE_PAD
        has_thru = through and drill > 0 and ring > 0

        rows_inset = None
        pair = self._row_spacing_edges()
        if pair is not None and side.edge in pair:
            rows_inset = through_hole_inset_from_rows(self.edge_extent(side.edge), self.row_spacing)

        inset, inset_source = None, None
        if style == CONTACT_THROUGH_HOLE:
            if self.thru_hole_offset > 0:
                inset, inset_source = self.thru_hole_offset, INSET_EXPLICIT
            elif rows_inset is not None:
                inset, inset_source = rows_inset, INSET_ROW_SPACING
            else:
                inset, inset_source = fallback_through_hole_inset(ring), INSET_FALLBACK
        elif has_thru:
            if self.thru_hole_offset > 0:
                inset, inset_source = self.thru_hole_offset, INSET_EXPLICIT
            elif rows_inset is not None and rows_inset >= ring / 2 - 1e-9:
                inset, inset_source = rows_inset, INSET_ROW_SPACING
                limit = rows_inset - drill / 2 - MIN_HOLE_WALL_MM
                # Only an ASSUMED half-hole is reduced; a declared one that does not
                # leave the wall is an error (validate).
                if (half_style and half_source in (SIZE_REFERENCE, SIZE_ESTIMATE_PITCH,
                                                   SIZE_ESTIMATE_PAD)
                        and self._half_hole_inset_for(half) + half / 2 > limit + 1e-9):
                    largest = self._largest_half_hole_before(limit)
                    if 0 < largest < half:
                        half, half_source = largest, SIZE_ESTIMATE_ROWS
            else:
                inset_source = (INSET_HALF_HOLE_ROWS if rows_inset is not None
                                else INSET_FALLBACK)
        half_inset = self._half_hole_inset_for(half)
        if has_thru and inset is None:
            # The minimum that keeps the hole separate from the half-hole with the
            # minimum wall: same expression as thru_hole_centre_offset.
            inset = round(half_inset + half / 2 + drill / 2 + MIN_HOLE_WALL_MM, 3)
        return SideGeometry(half, half_source, half_inset, drill, drill_source, ring, ring_source,
                            has_thru, inset, inset_source, rows_inset)

    def geometry_notes(self) -> List[str]:
        """@brief Human-readable notes on every estimated hole quantity (Italian, via tx).

        @return One line per estimate, one side at a time in declaration order:
            reduced assumed sizes first, then where the through-holes come
            from when that is not an explicit offset.
        @note Nothing estimated is left silent (project rule on estimated
            dimensions). Reference sizes that fit are not listed: they are the
            historical default, and listing them would change the pinned
            payload of the default cases.
        """
        notes = []
        for side in self.sides:
            if side.edge not in EDGES or side.contact_style not in CONTACT_STYLES:
                continue
            label = EDGE_LABELS[side.edge]
            geometry = self.side_geometry(side)
            style = side.contact_style
            if style_has_half_hole(style):
                if geometry.half_hole_source == SIZE_ESTIMATE_PITCH:
                    notes.append(tx(
                        "Lato {label}: diametro del semi-foro non dichiarato; il valore di "
                        "riferimento {reference}mm non sta nel passo {pitch}mm con la parete "
                        "minima di {wall}mm, quindi e' stimato {value:.2f}mm (il massimo che ci "
                        "sta). Indica il diametro misurato per averlo esatto.",
                        label=label, reference=float(self._reference("hole_diameter", side)),
                        pitch=side.pitch, wall=MIN_HOLE_WALL_MM, value=geometry.half_hole))
                elif geometry.half_hole_source == SIZE_ESTIMATE_PAD:
                    notes.append(tx(
                        "Lato {label}: diametro del semi-foro non dichiarato; la stima non stava "
                        "nella piazzola larga {pad:.2f}mm con {wall}mm di rame per parte, quindi "
                        "e' ridotto a {value:.2f}mm. Indica il diametro misurato per averlo esatto.",
                        label=label, pad=self._side_land_width(side) or 0.0,
                        wall=MIN_HOLE_WALL_MM, value=geometry.half_hole))
                elif geometry.half_hole_source == SIZE_ESTIMATE_ROWS:
                    notes.append(tx(
                        "Lato {label}: diametro del semi-foro non dichiarato; con i fori passanti "
                        "a {inset:.2f}mm dal bordo (distanza fra le file {row_spacing}mm) il "
                        "valore di riferimento {reference}mm non lascia la parete minima di "
                        "{wall}mm, quindi e' stimato {value:.2f}mm. Indica il diametro misurato "
                        "per averlo esatto.",
                        label=label, inset=geometry.thru_inset, row_spacing=self.row_spacing,
                        reference=float(self._reference("hole_diameter", side)),
                        wall=MIN_HOLE_WALL_MM, value=geometry.half_hole))
            if style == CONTACT_THROUGH_HOLE or geometry.has_thru_holes:
                if geometry.ring_source == SIZE_ESTIMATE_PITCH:
                    notes.append(tx(
                        "Lato {label}: corona del foro passante non dichiarata; il valore di "
                        "riferimento {reference}mm non sta nel passo {pitch}mm con la distanza "
                        "minima di {wall}mm, quindi e' stimata {value:.2f}mm (il massimo che ci "
                        "sta). Indica il diametro misurato per averlo esatto.",
                        label=label, reference=float(self._reference("thru_pad_diameter", side)),
                        pitch=side.pitch, wall=MIN_HOLE_WALL_MM, value=geometry.ring))
                if geometry.ring_source == SIZE_ESTIMATE_PAD:
                    notes.append(tx(
                        "Lato {label}: corona del foro passante non dichiarata; e' rame della "
                        "stessa piazzola, quindi non supera la sua larghezza: {value:.2f}mm.",
                        label=label, value=geometry.ring))
                if geometry.ring_source == SIZE_ESTIMATE_DRILL:
                    notes.append(tx(
                        "Lato {label}: corona del foro passante non dichiarata; il valore di "
                        "riferimento {reference}mm non avvolge il foro di {drill:.2f}mm con "
                        "l'anello minimo di {wall}mm, quindi e' stimata {value:.2f}mm. Indica "
                        "il diametro misurato per averlo esatto.",
                        label=label, reference=float(self._reference("thru_pad_diameter", side)),
                        drill=geometry.drill, wall=MIN_HOLE_WALL_MM, value=geometry.ring))
                if geometry.drill_source == SIZE_ESTIMATE_RING:
                    notes.append(tx(
                        "Lato {label}: diametro del foro passante non dichiarato; la stima non "
                        "lasciava posto a una corona con {wall}mm di rame per parte, quindi e' "
                        "ridotto a {value:.2f}mm (corona {ring:.2f}mm). Indica il diametro misurato "
                        "per averlo esatto.",
                        label=label, wall=MIN_HOLE_WALL_MM, value=geometry.drill,
                        ring=geometry.ring))
                if geometry.drill_source == SIZE_ESTIMATE_PITCH:
                    notes.append(tx(
                        "Lato {label}: diametro del foro passante non dichiarato; il valore di "
                        "riferimento {reference}mm non sta nel passo {pitch}mm e nella corona di "
                        "{ring:.2f}mm con la parete minima di {wall}mm, quindi e' stimato "
                        "{value:.2f}mm (il massimo che ci sta). Indica il diametro misurato per "
                        "averlo esatto.",
                        label=label, reference=float(self._reference("thru_hole_drill", side)),
                        pitch=side.pitch, ring=geometry.ring, wall=MIN_HOLE_WALL_MM,
                        value=geometry.drill))
            notes.extend(self._copper_fit_notes(side, geometry, label))
            notes.extend(self._inset_notes(side, geometry, label))
            notes.extend(self._pad_notes(side, label))
        return notes

    def _pad_notes(self, side: CastellatedSide, label: str) -> List[str]:
        """Notes on the estimated pad sizes of one side that were reduced (pad_sizes)."""
        size = self._pad_size_of(side)
        notes = []
        if size is None:
            return notes
        raw_length, declared = self._raw_pad(side, "pad_length")
        if (not declared and side.contact_style != CONTACT_THROUGH_HOLE
                and _finite_positive(raw_length) and raw_length <= self.pad_overhang
                and size.length > self.pad_overhang):
            notes.append(tx(
                "Lato {label}: lunghezza pad stimata {estimate}mm incompatibile con lo "
                "sbalzo {overhang}mm; usato il riferimento del modulo, con lunghezza "
                "finale {value:.2f}mm. Quota da verificare.",
                label=label, estimate=float(raw_length), overhang=self.pad_overhang,
                value=size.length))
        if size.width_source == PAD_ESTIMATE_PITCH:
            notes.append(tx(
                "Lato {label}: larghezza delle piazzole stimata {estimate}mm, non letta dai "
                "documenti; con il passo {pitch}mm il rame di due contatti vicini resterebbe a "
                "meno di {gap}mm, quindi e' ridotta a {value:.2f}mm. Indica la larghezza letta "
                "dal disegno per averla esatta.",
                label=label, estimate=float(size.width_estimate), pitch=side.pitch,
                gap=MIN_COPPER_GAP_MM, value=size.width))
        if size.length_source == PAD_ESTIMATE_CLEARANCE:
            notes.append(tx(
                "Lato {label}: lunghezza delle piazzole stimata {estimate}mm, non letta dai "
                "documenti; il rame arriverebbe a meno di {gap}mm da quello dei contatti del "
                "lato {other}, quindi e' ridotta a {value:.2f}mm. Indica la lunghezza letta dal "
                "disegno per averla esatta.",
                label=label, estimate=float(size.length_estimate), gap=MIN_COPPER_GAP_MM,
                other=EDGE_LABELS.get(size.clearance_edge, size.clearance_edge),
                value=size.length))
        return notes

    def _reference(self, name: str, side: Optional[CastellatedSide] = None) -> float:
        """The assumed size an estimate started from, before any reduction, mm.

        The side's own assumed value when it has one (same precedence as
        _sized), else the module's, else the historical reference.
        """
        own = getattr(side, name, None) if side is not None else None
        if own is not None and not is_declared_size(own):
            return float(own)
        value = getattr(self, name)
        if value is None:
            value = {"hole_diameter": REFERENCE_HALF_HOLE_MM,
                     "thru_hole_drill": REFERENCE_THRU_DRILL_MM,
                     "thru_pad_diameter": REFERENCE_THRU_RING_MM}[name]
        return value

    def _inset_notes(self, side: CastellatedSide, geometry: SideGeometry, label: str) -> List[str]:
        """Notes on where the through-holes of one side are, when not explicit."""
        source = geometry.thru_inset_source
        notes = []
        if side.contact_style == CONTACT_THROUGH_HOLE:
            # Same texts (and same values) that the 3D generator wrote before the
            # notes moved here: the default payload does not change.
            if source == INSET_FALLBACK:
                notes.append(
                    tx("Lato {label}: distanza dal bordo dei fori passanti non dichiarata, "
                       "stimata {value:.2f}mm (raggio della corona piu' la parete minima). E' "
                       "una stima per difetto: indica la distanza fra le file o la distanza "
                       "dal bordo per averla esatta.",
                       label=label,
                       value=geometry.thru_inset))
            elif source == INSET_ROW_SPACING:
                notes.append(
                    tx("Lato {label}: fori passanti a {value:.2f}mm dal bordo, ricavati da "
                       "corpo {edge_extent}mm e distanza fra le file {row_spacing}mm.",
                       label=label,
                       value=geometry.thru_inset,
                       edge_extent=self.edge_extent(side.edge),
                       row_spacing=self.row_spacing))
            return notes
        if not geometry.has_thru_holes:
            return notes
        if source == INSET_ROW_SPACING:
            notes.append(
                tx("Lato {label}: fori passanti a {value:.2f}mm dal bordo, ricavati da "
                   "corpo {edge_extent}mm e distanza fra le file {row_spacing}mm.",
                   label=label,
                   value=geometry.thru_inset,
                   edge_extent=self.edge_extent(side.edge),
                   row_spacing=self.row_spacing))
        elif source in (INSET_FALLBACK, INSET_HALF_HOLE_ROWS):
            if source == INSET_HALF_HOLE_ROWS:
                notes.append(tx(
                    "Lato {label}: con la distanza fra le file dichiarata ({row_spacing}mm) i "
                    "centri starebbero a {inset:.2f}mm dal bordo, meno del raggio della corona "
                    "({radius:.2f}mm): un foro passante chiuso non ci sta, quindi e' la distanza "
                    "fra le file di semi-fori e non viene usata per i fori passanti.",
                    label=label, row_spacing=self.row_spacing, inset=geometry.rows_inset,
                    radius=geometry.ring / 2))
            notes.append(tx(
                "Lato {label}: distanza dal bordo dei fori passanti non dichiarata, stimata "
                "{value:.2f}mm (la minima che li separa dal semi-foro con la parete minima). "
                "E' una stima per difetto: indica la distanza fra le file o la distanza dal "
                "bordo per averla esatta.",
                label=label, value=geometry.thru_inset))
        return notes

    @property
    def half_hole_inset(self) -> float:
        """@brief Distance from the edge of the centre of the MODULE's half-hole, in mm.

        @return The declared value, otherwise HALF_HOLE_INSET_RATIO radii as
            by convention, computed on the module diameter (hole_diameter).
        @note A castellation is a normal hole drilled near the edge and then
            cut: its centre lies INSIDE the board. The value of each side,
            which may have a half-hole of its own, is SideGeometry.half_hole_inset.
        """
        return self._half_hole_inset_for(self.hole_diameter)

    @property
    def thru_hole_centre_offset(self) -> float:
        """@brief Distance from the edge of the through-hole behind the half-hole, MODULE sizes, mm.

        @return The declared value, otherwise the minimum that keeps the two
            holes apart leaving MIN_HOLE_WALL_MM of wall between them.
        @note It is not a size chosen on one board: it is the distance below
            which the two cavities merge, plus one wall. It uses the module
            sizes only, without the sides: it is the module-level estimate
            (services/contact_zones now uses SideGeometry). The true position
            of each side -- which accounts for the side's holes and for the
            declared row spacing -- is SideGeometry.thru_inset.
        """
        if self.thru_hole_offset > 0:
            return self.thru_hole_offset
        return round(self.half_hole_inset + self.hole_diameter / 2
                     + self.thru_hole_drill / 2 + MIN_HOLE_WALL_MM, 3)

    @property
    def has_thru_holes(self) -> bool:
        """@brief True when the MODULE declares a closed through-hole (module sizes).

        @return True when both the module's hole and its land are > 0.
        @note Setting the diameter to zero is the way to say "this module has
            half-holes only": a hole of zero diameter does not exist, so zero
            cannot be mistaken for a size. For a side with sizes of its own
            SideGeometry.has_thru_holes applies.
        """
        return self.thru_hole_drill > 0 and self.thru_pad_diameter > 0

    def edge_extent(self, edge: str) -> float:
        """@brief Board extent across a contact row on `edge`, in mm.

        @param edge One of EDGES.
        @return body_length for left/right rows (measured along X), body_width
            for top/bottom rows (measured along Y).
        """
        return self.body_length if edge in ("left", "right") else self.body_width

    def _row_spacing_edges(self) -> Optional[tuple]:
        """The opposite pair the declared row spacing belongs to, if any."""
        if self.row_spacing is None:
            return None
        return opposite_row_edges(side.edge for side in self.sides)

    def _through_geometry(self, edge: str) -> SideGeometry:
        """Geometry of the first side on `edge`, described as a through_hole side if it has no through-hole."""
        side = next((s for s in self.sides if s.edge == edge), None)
        if side is None:
            side = CastellatedSide(edge=edge, count=1, pitch=0, contact_style=CONTACT_THROUGH_HOLE)
        geometry = self.side_geometry(side)
        if geometry.thru_inset is None:
            geometry = self.side_geometry(replace(side, contact_style=CONTACT_THROUGH_HOLE))
        return geometry

    def through_hole_inset_source(self, edge: str) -> str:
        """@brief Where the through-hole inset of the side on `edge` comes from.

        @param edge Edge of the side.
        @return "explicit" (thru_hole_offset), "row_spacing" (body extent and
            declared row spacing), "half_hole_rows" (half_and_through side
            whose declared rows are the castellations) or "fallback" (declared
            estimate). See side_geometry.
        """
        return self._through_geometry(edge).thru_inset_source

    def through_hole_inset(self, edge: str) -> float:
        """@brief Distance from the edge of the through-hole centres of the side on `edge`, mm.

        @param edge Edge of the side.
        @return The explicit thru_hole_offset when given; otherwise
            (extent - row_spacing) / 2 when a row spacing is declared for this
            edge's opposite pair (and, for half_and_through, can hold a closed
            ring); otherwise the declared estimate of the side's style.
        @note Same value the footprint and the 3D use (side_geometry).
        """
        return self._through_geometry(edge).thru_inset

    def needs_half_holes(self) -> bool:
        """@brief True when at least one side cuts half-holes into the edge.

        @return True when some side's contact_style has a half-hole
            (style_has_half_hole), False otherwise.
        """
        return any(style_has_half_hole(side.contact_style) for side in self.sides)

    def thru_hole_centre(self, placement: "PadPlacement") -> tuple:
        """@brief Centre of the closed hole of a contact, in footprint coordinates.

        @param placement Contact already positioned on its edge.
        @return (x, y) in mm: the hole lies on the same row as the contact,
            but shifted towards the inside of the body by its side's distance
            (SideGeometry.thru_inset). For a through_hole contact the land IS
            the hole ring, so its centre is already the one.
        """
        if placement.style == CONTACT_THROUGH_HOLE:
            return (placement.x, placement.y)
        offset = self.contact_geometry(placement).thru_inset
        if offset is None:
            offset = self.thru_hole_centre_offset
        if placement.edge == "left":
            return (-self.body_length / 2 + offset, placement.y)
        if placement.edge == "right":
            return (self.body_length / 2 - offset, placement.y)
        if placement.edge == "top":
            return (placement.x, -self.body_width / 2 + offset)
        return (placement.x, self.body_width / 2 - offset)

    def side_pad_length(self, side: CastellatedSide) -> float:
        """@brief Return the resolved pad length in mm for `side` (see pad_sizes).

        @param side One of self.sides; any other side object gets its own
            value, else the module default, unresolved.
        @return The pad length in mm, after the clearance reduction of
            pad_sizes when `side` belongs to this module.
        """
        size = self._pad_size_of(side)
        return size.length if size is not None else float(self._raw_pad(side, "pad_length")[0])

    def side_pad_width(self, side: CastellatedSide) -> float:
        """@brief Return the resolved pad width in mm for `side` (see pad_sizes).

        @param side One of self.sides; any other side object gets its own
            value, else the module default, unresolved.
        @return The pad width in mm, after the clearance reduction of
            pad_sizes when `side` belongs to this module.
        """
        size = self._pad_size_of(side)
        return size.width if size is not None else float(self._raw_pad(side, "pad_width")[0])

    def _raw_pad(self, side, name: str) -> tuple:
        """(value, declared) of a pad size before any reduction.

        Precedence: the side's own value (declared or estimated), else the
        module's. Unlike the hole sizes, a module-wide DECLARED pad size does
        not outrank a side's estimate: the module value is only the default
        of the sides without a land pattern of their own, and letting it win
        is exactly how one side's size used to reach every side (2026-09-28).
        """
        own = getattr(side, name, None)
        value = own if own is not None else getattr(self, name)
        if value is None:
            value = REFERENCE_PAD_LENGTH_MM if name == "pad_length" else REFERENCE_PAD_WIDTH_MM
        return value, is_declared_size(value)

    def _copper_fit_notes(self, side, geometry: "SideGeometry", label: str) -> List[str]:
        """Notes on holes that still do not fit the measured or declared pad of `side`.

        side_geometry reduces an undeclared hole to fit its land, but not
        below the fabrication minimum, and an undeclared ring grows around a
        hole wider than the land: in both cases the drawing contradicts the
        measured pad, and the note says so instead of leaving it silent.
        """
        pad_width = self._side_land_width(side)
        if pad_width is None:
            return []
        notes = []
        if (style_has_half_hole(side.contact_style)
                and geometry.half_hole_source not in (SIZE_SIDE, SIZE_MODULE)
                and geometry.half_hole > pad_width - 2 * MIN_HOLE_WALL_MM + 1e-9):
            notes.append(tx(
                "Lato {label}: il semi-foro stimato di {value:.2f}mm non sta nella piazzola "
                "larga {pad:.2f}mm con {wall}mm di rame per parte, e ridurlo lo porterebbe "
                "sotto {smallest:.2f}mm: la piazzola misurata o la stima del foro e' "
                "sbagliata. Indica il diametro misurato.",
                label=label, value=geometry.half_hole, pad=pad_width, wall=MIN_HOLE_WALL_MM,
                smallest=self._smallest_estimated_half_hole()))
        if (geometry.has_thru_holes and side.contact_style == CONTACT_HALF_AND_THROUGH
                and geometry.ring_source not in (SIZE_SIDE, SIZE_MODULE)
                and geometry.ring > pad_width + 1e-9):
            notes.append(tx(
                "Lato {label}: la corona del foro passante ({value:.2f}mm) e' piu' larga "
                "della piazzola ({pad:.2f}mm) di cui fa parte, perche' deve avvolgere il "
                "foro di {drill:.2f}mm: la piazzola o il foro va verificato.",
                label=label, value=geometry.ring, pad=pad_width, drill=geometry.drill))
        return notes

    def _pad_size_of(self, side) -> Optional[PadSize]:
        """The resolved PadSize of `side` when it is one of self.sides (identity), else None."""
        for index, candidate in enumerate(self.sides):
            if candidate is side:
                return self.pad_sizes()[index]
        return None

    def _pad_cache_key(self) -> str:
        """Snapshot of every input of pad_sizes, provenance (type) included."""
        def tagged(value):
            if isinstance(value, (list, tuple)):
                return tuple(tagged(v) for v in value)
            return (type(value).__name__, value)
        module = tuple(tagged(getattr(self, name, None)) for name in (
            "body_length", "body_width", "pad_length", "pad_width", "pad_overhang",
            "hole_diameter", "hole_inset", "thru_hole_drill", "thru_pad_diameter",
            "thru_hole_offset", "row_spacing"))
        sides = tuple(tuple(tagged(getattr(side, name, None)) for name in (
            "edge", "count", "pitch", "first_pin", "reverse", "offset", "pad_length",
            "pad_width", "pin_numbers", "contact_style", "hole_diameter",
            "thru_hole_drill", "thru_pad_diameter")) for side in self.sides)
        return repr((module, sides))

    def pad_sizes(self) -> List[PadSize]:
        """@brief Edge-land size of every side, estimates reduced to keep the copper apart.

        @return One PadSize per entry of self.sides, same order, mm.
        @note Rule (decision of the user, 2026-09-28, «Riduci e segnala», the
            same as for estimated holes since 2026-09-26): a DECLARED size (a
            plain float: read from the documents or typed by the user) is
            returned as is, and validate() refuses it when two contacts'
            copper touches. An ASSUMED size (AssumedSize: an AI estimate or
            the reference land pattern) that brings two different contacts'
            copper closer than MIN_COPPER_GAP_MM is reduced just enough:
            * width, along the edge: at most pitch - MIN_COPPER_GAP_MM on a
              side with more than one contact (floor to 0.01 mm);
            * length, across the edge: when the copper of two different sides
              (facing rows of a narrow body, contacts near a corner) is closer
              than the gap, the assumed lengths of the two sides are shortened
              together in 0.01 mm steps -- the outer end stays at the
              overhang, the inner end retreats -- by the smallest step that
              leaves the gap, never below pad_overhang + 0.01 mm (the land
              must stay under the module). When even that is not enough
              nothing is reduced, and validate() reports the conflict.
            Through-hole sides are left alone: their land is the ring
            (side_geometry), which has its own rule.
        @note Every reduction is reported by geometry_notes(). The result is
            cached on a snapshot of its inputs (types included, so a change
            of provenance alone invalidates it).
        """
        key = self._pad_cache_key()
        cached = self.__dict__.get("_pad_sizes_cache")
        if cached is not None and cached[0] == key:
            return list(cached[1])
        sizes = self._resolve_pad_sizes()
        self.__dict__["_pad_sizes_cache"] = (key, tuple(sizes))
        return list(sizes)

    def _resolve_pad_sizes(self) -> List[PadSize]:
        """Compute pad_sizes() (uncached)."""
        entries = []
        for side in self.sides:
            length, length_declared = self._raw_pad(side, "pad_length")
            width, width_declared = self._raw_pad(side, "pad_width")
            # A photo estimate shorter than the existing overhang cannot be
            # a usable land. Fall back to the module reference, still as an
            # estimate subject to clearance fitting. Never alter read sizes.
            if (not length_declared and side.contact_style != CONTACT_THROUGH_HOLE
                    and _finite_positive(length)
                    and isinstance(self.pad_overhang, (int, float))
                    and math.isfinite(self.pad_overhang)
                    and 0 <= length <= self.pad_overhang
                    and _finite_positive(self.pad_length)
                    and self.pad_length > self.pad_overhang):
                length = float(self.pad_length)
            entries.append(PadSize(
                length, PAD_DECLARED if length_declared else PAD_ASSUMED, length,
                width, PAD_DECLARED if width_declared else PAD_ASSUMED, width))
        if not all(_finite_positive(e.length) and _finite_positive(e.width) for e in entries):
            return entries  # validate() rejects these dimensions with its own message
        for index, side in enumerate(self.sides):
            entry = entries[index]
            count, pitch = getattr(side, "count", 0), getattr(side, "pitch", 0)
            if (entry.width_source != PAD_ASSUMED or side.contact_style == CONTACT_THROUGH_HOLE
                    or isinstance(count, bool) or not isinstance(count, int) or count < 2
                    or not _finite_positive(pitch)):
                continue
            bound = pitch - MIN_COPPER_GAP_MM
            if entry.width > bound + 1e-9:
                best = _floor_hundredth(bound)
                if best > 0:
                    entries[index] = entry._replace(width=best, width_source=PAD_ESTIMATE_PITCH)
        adjustable = [e.length_source == PAD_ASSUMED and side.contact_style != CONTACT_THROUGH_HOLE
                      for e, side in zip(entries, self.sides)]
        overhang = self.pad_overhang
        overhang_ok = (not isinstance(overhang, bool) and isinstance(overhang, (int, float))
                       and math.isfinite(overhang) and overhang >= 0)
        if any(adjustable) and len(self.sides) > 1 and overhang_ok:
            try:
                self._shorten_assumed_lengths(entries, adjustable)
            except (ArithmeticError, KeyError, TypeError, ValueError):
                # Parameters that validate() will reject anyway (unknown side or
                # style, invalid dimensions): no reduction, the right message is
                # its own.
                pass
        return entries

    def _probe(self, entries: List[PadSize]) -> "CastellatedModuleParams":
        """A copy whose sides carry `entries` as DECLARED sizes, so it reduces nothing."""
        sides = [replace(side, pad_length=float(e.length), pad_width=float(e.width))
                 for side, e in zip(self.sides, entries)]
        return replace(self, sides=sides, pad_length=float(self.pad_length),
                       pad_width=float(self.pad_width))

    def _side_gap(self, entries: List[PadSize], first: int, second: int) -> float:
        """Smallest copper gap between contacts of two sides with different numbers, mm."""
        probe = self._probe(entries)
        outlines = {first: [], second: []}
        for placement in pad_positions(probe):
            owner = probe.side_of(placement)
            for index in (first, second):
                if owner is probe.sides[index]:
                    outlines[index].append((placement.number, _copper_outline(probe, placement)))
        gap = math.inf
        for number_a, a in outlines[first]:
            for number_b, b in outlines[second]:
                if number_a != number_b:
                    gap = min(gap, _box_distance(a, b) - a[4] - b[4])
        return gap

    def _shorten_assumed_lengths(self, entries: List[PadSize], adjustable: List[bool]) -> None:
        """Shorten, in place, the assumed lengths of side pairs whose copper is closer than the gap."""
        floor = round(self.pad_overhang + 0.01, 2)
        count = len(self.sides)
        for first in range(count):
            for second in range(first + 1, count):
                movable = [i for i in (first, second) if adjustable[i]]
                if not movable or self._side_gap(entries, first, second) >= MIN_COPPER_GAP_MM - 1e-9:
                    continue
                starts = {i: _floor_hundredth(entries[i].length) for i in movable}
                steps = max(int(round((starts[i] - floor) * 100)) for i in movable)
                if steps <= 0:
                    continue
                base = list(entries)

                def shortened(step):
                    trial = list(base)
                    for i in movable:
                        trial[i] = trial[i]._replace(
                            length=max(floor, round(starts[i] - step / 100, 2)))
                    return trial

                def clear(step):
                    return self._side_gap(shortened(step), first, second) >= MIN_COPPER_GAP_MM - 1e-9

                if not clear(steps):
                    continue  # not even the minimum land is enough: validate() says so
                low, high = -1, steps  # high: the distance is enough; low: it is not
                while high - low > 1:
                    middle = (low + high) // 2
                    if clear(middle):
                        high = middle
                    else:
                        low = middle
                result = shortened(high)
                for i in movable:
                    if result[i].length < entries[i].length - 1e-9:
                        other = self.sides[second if i == first else first].edge
                        entries[i] = result[i]._replace(length_source=PAD_ESTIMATE_CLEARANCE,
                                                        clearance_edge=other)

    def edge_length(self, edge: str) -> float:
        """@brief Return the body edge length in mm along which the pads of `edge` are spread.

        @param edge One of EDGES.
        @return body_width for the left/right edges (they run along Y),
            body_length for top/bottom (they run along X). Not to be confused
            with edge_extent, the extent ACROSS a row.
        """
        return self.body_width if edge in ("left", "right") else self.body_length

    def validate(self) -> None:
        """@brief Check that the module can be drawn as a manufacturable footprint.

        @throws ValueError With a localized message on the first violation.
        @note Contract: every dimension is a finite number; every contact row
            fits inside its edge, offset included; the copper of two different
            pins never touches (strictly positive gap, every pair of contacts,
            any side, any contact style); a pad keeps part of its land under
            the module (0 <= pad_overhang < pad_length); every pin number is
            nonempty and unique; every half-hole is really cut by the edge;
            two drilled holes of different contacts (half-holes and closed
            through-holes, same side or not) keep at least MIN_HOLE_WALL_MM of
            board between them, and so do the two holes of one half_and_through
            contact. Sizes are the side-specific ones (side_geometry): a declared
            size that does not fit is refused, an assumed one was already
            reduced to fit and is reported by geometry_notes().
        """
        self._validate_finite_numbers()
        if not self.name.strip():
            raise ValueError(tx("Il nome del footprint non puo' essere vuoto."))
        # The name becomes the stem of every generated file and of the
        # `.3dshapes` folder: the shared rule refuses a path such as
        # "..\evil" here too, so a script caller is protected exactly like
        # the window (2026-10-27 review, H4).
        validate_output_name(self.name)
        if self.body_length <= 0 or self.body_width <= 0:
            raise ValueError(tx("Le dimensioni del corpo devono essere > 0."))
        if not self.sides:
            raise ValueError(tx("Serve almeno un lato con dei contatti."))
        # An unknown value is rejected, not remapped: guessing which of the
        # three was meant would be exactly the deduction that the field
        # exists to avoid.
        for side in self.sides:
            if side.contact_style not in CONTACT_STYLES:
                raise ValueError(
                    tx("Lato {get}: tipo di contatto {contact_style!r} non riconosciuto "
                       "(ammessi: {join}).",
                       get=EDGE_LABELS.get(side.edge, side.edge),
                       contact_style=side.contact_style,
                       join=', '.join(CONTACT_STYLES))
                )
        # The half-hole is needed only by the sides that really cut it.
        if any(style_has_half_hole(side.contact_style) and self.side_geometry(side).half_hole <= 0
               for side in self.sides):
            raise ValueError(tx("Il diametro del semi-foro deve essere > 0."))
        if self.pcb_thickness <= 0:
            raise ValueError(tx("Lo spessore del PCB deve essere > 0."))
        declared_through = [self.thru_hole_drill, self.thru_pad_diameter] + [
            getattr(side, name, None) for side in self.sides
            for name in ("thru_hole_drill", "thru_pad_diameter")]
        declared_through = [value for value in declared_through if value is not None]
        if any(value < 0 for value in declared_through):
            raise ValueError(tx("Le quote del foro passante non possono essere negative."))
        if self.pad_overhang < 0:
            raise ValueError(
                tx("Lo sbalzo dei pad oltre il bordo ({pad_overhang}mm) non puo' essere negativo.",
                   pad_overhang=self.pad_overhang)
            )
        self._validate_through_hole_sides()
        self._validate_double_contacts()

        seen_numbers: dict[str, str] = {}
        for side in self.sides:
            label = EDGE_LABELS.get(side.edge, side.edge)
            if side.edge not in EDGES:
                raise ValueError(
                    tx("Lato non valido: {edge!r} (attesi: {join}).",
                       edge=side.edge,
                       join=', '.join(EDGES))
                )
            if side.count < 1:
                raise ValueError(tx("Lato {label}: servono almeno 1 pad.", label=label))
            if side.pitch <= 0 and side.count > 1:
                raise ValueError(tx("Lato {label}: il passo deve essere > 0.", label=label))

            pad_length = self.side_pad_length(side)
            pad_width = self.side_pad_width(side)
            if pad_length <= 0 or pad_width <= 0:
                raise ValueError(tx("Lato {label}: le dimensioni dei pad devono essere > 0.", label=label))
            # A land entirely outside the edge does not lie under the module: there
            # is nothing to solder. It holds for the sides with an edge land.
            if side.contact_style != CONTACT_THROUGH_HOLE and self.pad_overhang >= pad_length:
                raise ValueError(
                    tx("Lato {label}: lo sbalzo dei pad oltre il bordo ({pad_overhang}mm) non e' "
                       "minore della loro lunghezza ({pad_length}mm): la piazzola starebbe tutta "
                       "fuori dal modulo.",
                       label=label,
                       pad_overhang=self.pad_overhang,
                       pad_length=pad_length)
                )
            # The land that is aligned along the edge is the one the side really
            # offers: the hole's ring for through_hole, the rectangular edge land
            # for the other two types.
            geometry = self.side_geometry(side)
            if side.contact_style == CONTACT_THROUGH_HOLE:
                land = geometry.ring
                if side.count > 1 and side.pitch < land:
                    raise ValueError(
                        tx("Lato {label}: passo {pitch}mm minore del diametro della corona "
                           "{land}mm -- le piazzole dei fori passanti si sovrapporrebbero.",
                           label=label,
                           pitch=side.pitch,
                           land=land)
                    )
            else:
                land = pad_width
                if side.count > 1 and side.pitch < pad_width:
                    raise ValueError(
                        tx("Lato {label}: passo {pitch}mm minore della larghezza pad "
                           "{pad_width}mm -- i pad si sovrapporrebbero.",
                           label=label,
                           pitch=side.pitch,
                           pad_width=pad_width)
                    )
                if has_continuous_pad(self, side.contact_style, side):
                    # The continuous land also contains the hole's ring: it is as wide
                    # as the larger of the two, and the pitch must keep them
                    # separate.
                    land = continuous_pad_across(self, pad_width, geometry.ring)
                    if side.count > 1 and side.pitch < land:
                        raise ValueError(
                            tx("Lato {label}: passo {pitch}mm minore della larghezza della piazzola "
                               "continua {land}mm (la maggiore fra pad {pad_width}mm e corona "
                               "{thru_pad_diameter}mm) -- i pad si sovrapporrebbero.",
                               label=label,
                               pitch=side.pitch,
                               land=land,
                               pad_width=pad_width,
                               thru_pad_diameter=geometry.ring)
                        )

            span = (side.count - 1) * side.pitch + land
            available = self.edge_length(side.edge)
            if span - 1e-6 > available:
                raise ValueError(
                    tx("Lato {label}: i {count} pad occupano {span:.2f}mm ma il bordo e' "
                       "lungo {available:.2f}mm.",
                       label=label,
                       count=side.count,
                       span=span,
                       available=available)
                )
            # The group is centred and then shifted by `offset`: even when shifted
            # it must stay within the edge, otherwise the pads end up past the
            # module's corner.
            if abs(side.offset) + span / 2 > available / 2 + 1e-6:
                raise ValueError(
                    tx("Lato {label}: spostati di {offset}mm, i pad escono dal bordo: "
                       "occupano fino a {reach:.2f}mm dal centro, il bordo arriva a "
                       "{half:.2f}mm.",
                       label=label,
                       offset=side.offset,
                       reach=abs(side.offset) + span / 2,
                       half=available / 2)
                )
            if side.pin_numbers is not None and any(
                    n is None or not str(n).strip() for n in side.pin_numbers):
                raise ValueError(tx("Lato {label}: un numero di pin e' vuoto.", label=label))

            numbers = side.numbers()
            if len(numbers) != side.count:
                raise ValueError(
                    tx("Lato {label}: {n_numbers} numeri di pin per {count} pad.",
                       label=label,
                       n_numbers=len(numbers),
                       count=side.count)
                )
            for number in numbers:
                if number in seen_numbers:
                    raise ValueError(
                        tx("Il pin {number} compare sia sul lato {seen_numbers} sia su quello "
                           "{label}.",
                           number=number,
                           seen_numbers=seen_numbers[number],
                           label=label)
                    )
                seen_numbers[number] = label

        self._validate_copper_clearance()
        self._validate_half_hole_insets()
        self._validate_hole_walls()

    def _validate_finite_numbers(self) -> None:
        """Refuse NaN, infinities, booleans and non-numbers before any comparison.

        A NaN compares False with everything, so every `<= 0` check below
        would silently accept it and the footprint writer would emit "nan".
        """
        def check(value, name, optional=False):
            if value is None and optional:
                return
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(tx("{name} deve essere un numero finito (mm).", name=name))

        for name in ("body_length", "body_width", "pad_length", "pad_width", "pad_overhang",
                     "hole_diameter", "thru_hole_drill", "thru_pad_diameter", "thru_hole_offset",
                     "pcb_thickness", "margin"):
            # None for the hole dimensions = not declared (the reference holds).
            check(getattr(self, name), name,
                  optional=name in ("hole_diameter", "thru_hole_drill", "thru_pad_diameter"))
        # row_spacing has its own check, with its own message, where it really
        # matters (sides with a through hole): elsewhere it does not enter the geometry.
        check(self.hole_inset, "hole_inset", optional=True)
        for side in self.sides:
            label = EDGE_LABELS.get(side.edge, side.edge)
            for name in ("pitch", "offset"):
                check(getattr(side, name), f"{name} ({label})")
            for name in ("pad_length", "pad_width", "hole_diameter", "thru_hole_drill",
                         "thru_pad_diameter"):
                # getattr with a default: a side described by an older object, without
                # the per-side hole dimensions, stays valid.
                check(getattr(side, name, None), f"{name} ({label})", optional=True)
            for name in ("count", "first_pin"):
                value = getattr(side, name)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(tx("Lato {label}: {name} deve essere un numero intero.",
                                        label=label, name=name))

    def _validate_copper_clearance(self) -> None:
        """Refuse any two contacts of different pins whose copper touches or overlaps.

        One rule for every pair (same side, adjacent sides near a corner,
        facing rows of a narrow body) and every contact style: the copper of
        each contact is the envelope from _copper_outline() and the gap
        between two envelopes must be strictly positive. Copper that merely
        touches is a short on the host board, so it is refused too (until
        2026-09-25 touching was accepted and only pairs involving a continuous
        pad were checked). Contacts with the same number may share copper.
        """
        placements = list(pad_positions(self))
        stadiums = {id(placement): continuous_pad(self, placement) for placement in placements}
        outlines = [(placement, _copper_outline(self, placement)) for placement in placements]
        for i, (first, a) in enumerate(outlines):
            for second, b in outlines[i + 1:]:
                if first.number == second.number:
                    continue
                if _box_distance(a, b) - a[4] - b[4] > 1e-6:
                    continue
                if stadiums[id(first)] is not None or stadiums[id(second)] is not None:
                    stadium = first if stadiums[id(first)] is not None else second
                    pad = stadiums[id(stadium)]
                    raise ValueError(
                        tx("La piazzola continua del contatto {number} (lato {get}), che dal "
                           "bordo arriva {inner_reach:.2f}mm dentro il corpo fino oltre il foro "
                           "passante, tocca il rame del contatto {value} (lato {get2}): due pin "
                           "diversi sarebbero in corto. Avvicina il foro passante al bordo, "
                           "accorcia la piazzola o riduci la corona.",
                           number=stadium.number,
                           get=EDGE_LABELS.get(stadium.edge, stadium.edge),
                           inner_reach=pad.inner_reach,
                           value=second.number if stadium is first else first.number,
                           get2=EDGE_LABELS.get((second if stadium is first else first).edge, ''))
                    )
                raise ValueError(
                    tx("Il rame del contatto {first} (lato {first_edge}) tocca quello del "
                       "contatto {second} (lato {second_edge}): due pin diversi sarebbero in "
                       "corto. Aumenta il passo, sposta i contatti o riduci le piazzole.",
                       first=first.number,
                       first_edge=EDGE_LABELS.get(first.edge, first.edge),
                       second=second.number,
                       second_edge=EDGE_LABELS.get(second.edge, second.edge))
                )

    def _validate_half_hole_insets(self) -> None:
        """Refuse a half-hole whose centre is so far in that the edge does not cut it.

        A declared hole_inset is module-wide while half-hole diameters can
        differ per side: an inset valid for a large half-hole can leave a
        small one entirely inside the board, where it would be drawn as a
        castellation that does not exist.
        """
        for side in self.sides:
            if not style_has_half_hole(side.contact_style):
                continue
            geometry = self.side_geometry(side)
            if geometry.half_hole_inset >= geometry.half_hole / 2 - 1e-9:
                raise ValueError(
                    tx("Lato {label}: il centro del semi-foro dista {inset}mm dal bordo, non "
                       "meno del suo raggio ({radius}mm): il bordo non lo taglierebbe e non "
                       "sarebbe un semi-foro. Riduci la distanza dal bordo del semi-foro.",
                       label=EDGE_LABELS.get(side.edge, side.edge),
                       inset=geometry.half_hole_inset,
                       radius=geometry.half_hole / 2)
                )

    def drilled_holes(self) -> List[tuple]:
        """@brief Every drilled cylinder of the module board, per contact.

        @return List of (placement, kind, x, y, radius) in footprint mm;
            kind is "half" (plated half-hole cut by the edge, full cylinder)
            or "through" (closed through-hole drill).
        @note The same cylinders the 3D generator subtracts from the board.
        """
        holes = []
        for placement in pad_positions(self):
            geometry = self.contact_geometry(placement)
            if style_has_half_hole(placement.style):
                nx, ny = INWARD_FOOTPRINT[placement.edge]
                edge_x, edge_y = _edge_point(self, placement)
                holes.append((placement, "half",
                              edge_x + nx * geometry.half_hole_inset,
                              edge_y + ny * geometry.half_hole_inset,
                              geometry.half_hole / 2))
            if placement.style == CONTACT_THROUGH_HOLE or geometry.has_thru_holes:
                if geometry.drill > 0:
                    x, y = self.thru_hole_centre(placement)
                    holes.append((placement, "through", x, y, geometry.drill / 2))
        return holes

    def _validate_hole_walls(self) -> None:
        """Refuse two drilled holes of different contacts closer than MIN_HOLE_WALL_MM.

        Same rule for every pair -- adjacent contacts on one side (the pitch
        case), contacts of two sides near a corner, a half-hole next to a
        neighbour's through-hole -- so it is not tied to the pitch alone.
        Below the wall the two cylinders merge into one slot in the 3D solid
        and the board could not be drilled as two holes. Holes of the same
        contact are checked by _validate_double_contacts.
        """
        holes = self.drilled_holes()
        for i, (first, kind_a, xa, ya, ra) in enumerate(holes):
            for second, kind_b, xb, yb, rb in holes[i + 1:]:
                if first.number == second.number:
                    continue
                wall = math.hypot(xa - xb, ya - yb) - ra - rb
                if wall >= MIN_HOLE_WALL_MM - 1e-6:
                    continue
                side_a, side_b = self.side_of(first), self.side_of(second)
                if kind_a == kind_b == "half" and side_a is side_b and side_a is not None:
                    raise ValueError(
                        tx("Lato {label}: semi-fori da {diameter}mm a passo {pitch}mm: fra due "
                           "semi-fori vicini restano {wall:.2f}mm di scheda, meno della parete "
                           "minima di {minimum}mm, e si fonderebbero in un'unica asola. Indica il "
                           "diametro reale del semi-foro di questo lato (al massimo {largest:.2f}mm).",
                           label=EDGE_LABELS.get(first.edge, first.edge),
                           diameter=2 * ra,
                           pitch=side_a.pitch,
                           wall=wall,
                           minimum=MIN_HOLE_WALL_MM,
                           largest=side_a.pitch - MIN_HOLE_WALL_MM)
                    )
                raise ValueError(
                    tx("Fra il foro del contatto {first} (lato {first_edge}) e quello del "
                       "contatto {second} (lato {second_edge}) restano {wall:.2f}mm di scheda, "
                       "meno della parete minima di {minimum}mm: i due fori si fonderebbero. "
                       "Riduci i fori o allontana i contatti.",
                       first=first.number,
                       first_edge=EDGE_LABELS.get(first.edge, first.edge),
                       second=second.number,
                       second_edge=EDGE_LABELS.get(second.edge, second.edge),
                       wall=wall,
                       minimum=MIN_HOLE_WALL_MM)
                )

    def _validate_row_spacing(self) -> None:
        """Refuse a declared row spacing that is not a positive finite number."""
        spacing = self.row_spacing
        if spacing is not None and (
                isinstance(spacing, bool) or not isinstance(spacing, (int, float))
                or not math.isfinite(spacing) or spacing <= 0):
            raise ValueError(
                tx("La distanza fra le file ({row_spacing!r}) deve essere un numero > 0.",
                   row_spacing=self.row_spacing)
            )

    def _validate_double_contacts(self) -> None:
        """Checks for half_and_through sides that carry a closed through-hole.

        The historical checks (ring wider than the drill, through-hole not
        merging with the half-hole, ring closed inside the board, facing rows
        fitting the body) now run per side, on the side's own sizes and
        position -- which may come from a declared row spacing.
        """
        sides = [side for side in self.sides
                 if side.contact_style == CONTACT_HALF_AND_THROUGH and side.edge in EDGES
                 and self.side_geometry(side).has_thru_holes]
        if not sides:
            return
        self._validate_row_spacing()
        for side in sides:
            geometry = self.side_geometry(side)
            label = EDGE_LABELS[side.edge]
            if geometry.ring <= geometry.drill:
                raise ValueError(
                    tx("La piazzola del foro passante ({thru_pad_diameter}mm) deve essere "
                       "piu' larga del foro ({thru_hole_drill}mm): senza corona non c'e' rame "
                       "su cui saldare.",
                       thru_pad_diameter=geometry.ring,
                       thru_hole_drill=geometry.drill)
                )
            # A hole whose edge leaves the body is not a closed hole: it is a
            # second half-hole, and then it might as well not be declared.
            inset = geometry.thru_inset
            gap = inset - geometry.half_hole_inset - geometry.half_hole / 2 - geometry.drill / 2
            minimum = (geometry.half_hole_inset + geometry.half_hole / 2
                       + geometry.drill / 2 + MIN_HOLE_WALL_MM)
            if geometry.thru_inset_source == INSET_ROW_SPACING and gap < MIN_HOLE_WALL_MM - 1e-6:
                raise ValueError(
                    tx("Lato {label}: le file dichiarate distano {row_spacing}mm su un corpo di "
                       "{edge_extent}mm, quindi i fori passanti starebbero a {inset}mm dal bordo: "
                       "fra ciascun foro e il semi-foro dello stesso contatto resterebbero "
                       "{wall:.2f}mm di scheda, meno della parete minima di {minimum}mm. "
                       "Controlla la distanza fra le file e il diametro dei fori.",
                       label=label,
                       row_spacing=self.row_spacing,
                       edge_extent=self.edge_extent(side.edge),
                       inset=inset,
                       wall=gap,
                       minimum=MIN_HOLE_WALL_MM)
                )
            if gap < 0:
                raise ValueError(
                    tx("Il foro passante e il semi-foro si toccano: i loro assi distano "
                       "{value:.2f}mm ma la somma dei raggi e' {value2:.2f}mm, quindi si "
                       "fonderebbero in un'unica cavita' aperta sul bordo invece di restare "
                       "due fori. Porta la distanza dal bordo ad almeno {minimum:.2f}mm, "
                       "oppure riduci il semi-foro.",
                       value=inset - geometry.half_hole_inset,
                       value2=geometry.half_hole / 2 + geometry.drill / 2,
                       minimum=minimum)
                )
            if gap < MIN_HOLE_WALL_MM - 1e-6:
                raise ValueError(
                    tx("Lato {label}: fra il foro passante e il semi-foro dello stesso contatto "
                       "restano {wall:.2f}mm di scheda, meno della parete minima di {wall_min}mm. "
                       "Porta la distanza dal bordo ad almeno {minimum:.2f}mm, oppure riduci il "
                       "semi-foro.",
                       label=label,
                       wall=gap,
                       wall_min=MIN_HOLE_WALL_MM,
                       minimum=minimum)
                )
            if inset < geometry.ring / 2:
                raise ValueError(
                    tx("Il centro del foro passante dista {thru_hole_centre_offset}mm dal "
                       "bordo, meno del raggio della sua piazzola ({value}mm): resterebbe "
                       "aperto sul bordo invece di essere chiuso.",
                       thru_hole_centre_offset=inset,
                       value=geometry.ring / 2)
                )
        # Only the pairs of opposite sides that REALLY carry half_and_through
        # through holes compete for the envelope: a 30x4 module with contacts
        # only on the left and right measures 30 mm between the two rows,
        # not 4. A side without an opposite side is already covered by the
        # checks above (the hole stays closed inside the body).
        carrying = {}
        for side in sides:
            carrying.setdefault(side.edge, self.side_geometry(side))
        for pair in OPPOSITE_EDGE_PAIRS:
            if not set(pair) <= set(carrying):
                continue
            a, b = carrying[pair[0]], carrying[pair[1]]
            shortest = self.edge_extent(pair[0])
            needed = a.thru_inset + b.thru_inset + (a.ring + b.ring) / 2
            if needed > shortest + 1e-6:
                raise ValueError(
                    tx("I fori passanti dei due lati opposti non stanno in {shortest}mm: "
                       "servono almeno {value:.2f}mm.",
                       shortest=shortest,
                       value=needed)
                )

    def _validate_through_hole_sides(self) -> None:
        """Checks for sides whose only feature is a full through-hole."""
        # A side with an invalid edge is rejected further on, with its own
        # message: here it has no geometry to check.
        through = [side for side in self.sides
                   if side.contact_style == CONTACT_THROUGH_HOLE and side.edge in EDGES]
        if not through:
            return
        geometries = [self.side_geometry(side) for side in through]
        without = [side for side, geometry in zip(through, geometries) if geometry.drill <= 0]
        if without:
            raise ValueError(
                tx("Lato {labels}: i contatti sono fori passanti ma il diametro del foro "
                   "e' zero. Indica il foro, oppure scegli il tipo «mezzo foro sul bordo».",
                   labels=", ".join(EDGE_LABELS[side.edge] for side in without))
            )
        thin = [(side, geometry) for side, geometry in zip(through, geometries)
                if geometry.ring <= geometry.drill]
        if thin:
            raise ValueError(
                tx("Lato {labels}: la piazzola del foro passante ({thru_pad_diameter}mm) "
                   "deve essere piu' larga del foro ({thru_hole_drill}mm): senza corona "
                   "non c'e' rame su cui saldare.",
                   labels=", ".join(EDGE_LABELS[side.edge] for side, _ in thin),
                   thru_pad_diameter=thin[0][1].ring,
                   thru_hole_drill=thin[0][1].drill)
            )
        self._validate_row_spacing()
        for side, geometry in zip(through, geometries):
            label = EDGE_LABELS.get(side.edge, side.edge)
            source = geometry.thru_inset_source
            inset = geometry.thru_inset
            radius = geometry.ring / 2
            # A through_hole contact has an intact edge by definition: a ring
            # leaving the body would be a half-hole.
            if inset < radius:
                if source == INSET_ROW_SPACING:
                    why = (tx("le file dichiarate distano {row_spacing}mm su un corpo di "
                              "{edge_extent}mm, quindi i fori starebbero a {inset}mm dal bordo",
                              row_spacing=self.row_spacing,
                              edge_extent=self.edge_extent(side.edge),
                              inset=inset))
                else:
                    why = tx("il centro del foro dista {inset}mm dal bordo", inset=inset)
                raise ValueError(
                    tx("Lato {label}: {why}, meno del raggio della corona ({radius}mm): il "
                       "foro resterebbe aperto sul bordo, cioe' sarebbe un semi-foro e non un "
                       "foro passante.",
                       label=label,
                       why=why,
                       radius=radius)
                )
            extent = self.edge_extent(side.edge)
            opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}[side.edge]
            facing = any(other.edge == opposite for other in self.sides)
            if facing and 2 * inset + geometry.ring > extent + 1e-6:
                raise ValueError(
                    tx("Lato {label}: le corone dei fori passanti di due file opposte non "
                       "stanno in {extent}mm: servono almeno {value:.2f}mm.",
                       label=label,
                       extent=extent,
                       value=2 * inset + geometry.ring)
                )
            if inset + radius > extent + 1e-6:
                raise ValueError(
                    tx("Lato {label}: il foro passante a {inset}mm dal bordo esce dal lato "
                       "opposto del corpo ({extent}mm).",
                       label=label,
                       inset=inset,
                       extent=extent)
                )


def _edge_point(params: CastellatedModuleParams, placement: PadPlacement) -> tuple:
    """Point of the board edge on the axis of a contact, footprint mm."""
    if placement.edge == "left":
        return (-params.body_length / 2, placement.y)
    if placement.edge == "right":
        return (params.body_length / 2, placement.y)
    if placement.edge == "top":
        return (placement.x, -params.body_width / 2)
    return (placement.x, params.body_width / 2)


def pad_positions(params: CastellatedModuleParams) -> Iterator[PadPlacement]:
    """@brief Yield the land of every contact, already placed and oriented.

    @param params Module description.
    @return Iterator of PadPlacement in footprint coordinates (mm, Y down).
    @note Single source of truth for the geometry: the 2D footprint, the 3D
        payload and the GUI preview all read it, so there are no two
        computations to keep aligned. The along-edge coordinate and the pin
        numbering depend only on the side, never on its contact style; the
        style decides WHICH land is yielded -- the rectangular edge pad for
        half_hole and half_and_through, the annular ring of the through-hole
        (centre at through_hole_inset, size = ring diameter) for through_hole,
        because that ring is the only land such a part offers.
    @note For half_and_through with a declared through-hole the edge pad
        yielded here is the REFERENCE the continuous copper is built from
        (same outer end, same along-edge position): the copper actually drawn
        is continuous_pad(params, placement), which replaces it.
    """
    half_length = params.body_length / 2
    half_width = params.body_width / 2

    for side in params.sides:
        pad_length = params.side_pad_length(side)
        pad_width = params.side_pad_width(side)
        style = side.contact_style

        # Coordinate along the edge: group centred, then shifted by offset.
        start = -(side.count - 1) * side.pitch / 2 + side.offset
        outer = (half_length if side.edge in ("left", "right") else half_width)
        if style == CONTACT_THROUGH_HOLE:
            # The ring is set back from the edge: between it and the edge there is
            # intact board, so no part protrudes. Distance and ring are those of
            # the side (side_geometry).
            geometry = params.side_geometry(side)
            center_offset = outer - geometry.thru_inset
            pad_length = pad_width = geometry.ring
        else:
            # The pad protrudes by pad_overhang past the edge, the rest of its
            # length goes under the module.
            center_offset = outer + params.pad_overhang - pad_length / 2

        for index, number in enumerate(side.numbers()):
            along = start + index * side.pitch
            if side.edge == "left":
                yield PadPlacement(number, -center_offset, along, pad_length, pad_width, side.edge, style)
            elif side.edge == "right":
                yield PadPlacement(number, center_offset, along, pad_length, pad_width, side.edge, style)
            elif side.edge == "top":
                yield PadPlacement(number, along, -center_offset, pad_width, pad_length, side.edge, style)
            else:  # bottom
                yield PadPlacement(number, along, center_offset, pad_width, pad_length, side.edge, style)


def has_continuous_pad(params: CastellatedModuleParams, style: str,
                       side: Optional[CastellatedSide] = None) -> bool:
    """@brief Tell whether contacts of `style` are drawn as one continuous stadium pad.

    @param params Module description.
    @param style One of CONTACT_STYLES.
    @param side The side the contacts belong to; None = module-level sizes only.
    @return True for CONTACT_HALF_AND_THROUGH when a closed through-hole is
        declared -- for the side (SideGeometry.has_thru_holes, which honours
        per-side sizes) or, without a side, for the module
        (params.has_thru_holes). With a zero drill such a side is
        "half-holes only" and keeps its historical rectangular edge land.
    """
    if style != CONTACT_HALF_AND_THROUGH:
        return False
    if side is None:
        return params.has_thru_holes
    return params.side_geometry(side).has_thru_holes


def continuous_pad_across(params: CastellatedModuleParams, land_width: float,
                          ring: Optional[float] = None) -> float:
    """@brief Width (along the edge) of the continuous pad of a half_and_through contact, mm.

    @param params Module description (for the through-hole ring diameter).
    @param land_width Declared width of the edge land (side pad_width), mm.
    @param ring Ring diameter of the contact's side (SideGeometry.ring), mm;
        None = the module's thru_pad_diameter.
    @return max(land_width, ring).
    @note Not a tuned value: the copper must contain BOTH declared lands, the
        edge land (land_width wide) and the full annular ring of the
        through-hole (thru_pad_diameter), so the narrowest stadium doing so is
        as wide as the larger of the two. This keeps the declared annular ring
        (thru_pad_diameter - drill) / 2 all around the drill.
    """
    return max(land_width, float(params.thru_pad_diameter) if ring is None else ring)


def continuous_pad(params: CastellatedModuleParams, placement: PadPlacement) -> Optional[ContactPad]:
    """@brief The single continuous copper land of a half_and_through contact.

    @param params Module description.
    @param placement The contact's edge land, from pad_positions().
    @return ContactPad, or None when the contact is not drawn as one
        continuous pad (see has_continuous_pad): half_hole and through_hole
        contacts, and half_and_through ones without a declared through-hole,
        keep exactly their historical lands.
    @note Rule (user decision 2026-09-23, from the photo of a real board:
        each contact is ONE elongated gold pad, no bare board between the
        castellation and the through-hole). Every dimension comes from declared
        parameters:
        * outer end: pad_overhang beyond the board edge, like the edge land;
        * inner end: the farthest of the edge land's inner end
          (pad_length - pad_overhang) and the far side of the through-hole
          ring (thru_hole_centre_offset + thru_pad_diameter / 2);
        * width: continuous_pad_across();
        * geometric floor: a stadium is never shorter than it is wide (else
          its round ends would turn along the edge), and its inner end is at
          least half a width inside the edge (else the 3D copper, which starts
          at the edge, would have no room for its round end). Both only
          lengthen the pad inward, never move the outer end.
        However far the through-hole sits, the pad stays continuous: that is
        what the style declares physically. The limits are physical and are
        enforced by validate(): the pad must not reach another contact's copper.
    """
    if placement.style != CONTACT_HALF_AND_THROUGH:
        return None
    geometry = params.contact_geometry(placement)
    if not geometry.has_thru_holes:
        return None
    vertical_edge = placement.edge in ("left", "right")
    land_length = placement.width if vertical_edge else placement.height
    land_width = placement.height if vertical_edge else placement.width
    across = continuous_pad_across(params, land_width, geometry.ring)
    overhang = params.pad_overhang
    inner = max(land_length - overhang,
                geometry.thru_inset + geometry.ring / 2,
                across - overhang,
                across / 2)
    length = overhang + inner
    nx, ny = INWARD_FOOTPRINT[placement.edge]
    # Point of the edge on the contact axis: everything is measured from there.
    edge_x, edge_y = _edge_point(params, placement)
    centre_depth = (inner - overhang) / 2
    drill_x, drill_y = params.thru_hole_centre(placement)
    size = (length, across) if vertical_edge else (across, length)
    return ContactPad(
        number=placement.number, edge=placement.edge,
        x=_clean(edge_x + nx * centre_depth), y=_clean(edge_y + ny * centre_depth),
        width=_clean(size[0]), height=_clean(size[1]),
        drill=geometry.drill, drill_x=_clean(drill_x), drill_y=_clean(drill_y),
        half_hole_x=_clean(edge_x + nx * geometry.half_hole_inset),
        half_hole_y=_clean(edge_y + ny * geometry.half_hole_inset),
        half_hole_diameter=geometry.half_hole,
        inner_reach=_clean(inner), outer_reach=_clean(overhang), across=_clean(across),
    )


def continuous_pads(params: CastellatedModuleParams) -> Iterator[ContactPad]:
    """@brief Yield the continuous pad of every contact that has one, in pad_positions() order.

    @param params Module description.
    @return Iterator of ContactPad (half_and_through contacts with a through-hole only).
    @note The GUI preview can draw each one as a rounded rectangle of size
        (width, height) centred at (x, y) with corner radius across / 2, plus
        the drill circle at (drill_x, drill_y); the edge land of the same
        number from pad_positions() is then entirely replaced by it.
    """
    for placement in pad_positions(params):
        pad = continuous_pad(params, placement)
        if pad is not None:
            yield pad


def _copper_outline(params: CastellatedModuleParams, placement: PadPlacement) -> tuple:
    """Copper of one contact as (core box x0, y0, x1, y1, radius), footprint mm.

    The copper is the set of points within `radius` of the (possibly
    degenerate) axis-aligned core box: a stadium is its axis segment with
    radius across/2, a ring is its centre with radius ring/2, a rectangular
    edge land is its own box with radius 0 (a conservative bound for a
    roundrect).
    """
    pad = continuous_pad(params, placement)
    if pad is not None:
        (ax, ay), (bx, by) = pad.axis()
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), pad.across / 2)
    if placement.style == CONTACT_THROUGH_HOLE:
        ring = params.contact_geometry(placement).ring
        return (placement.x, placement.y, placement.x, placement.y, ring / 2)
    return (placement.x - placement.width / 2, placement.y - placement.height / 2,
            placement.x + placement.width / 2, placement.y + placement.height / 2, 0.0)


def _copper_outline_bbox(params: CastellatedModuleParams, placement: PadPlacement) -> tuple:
    """Axis-aligned bounding box (x0, y0, x1, y1) of one contact's copper, mm."""
    x0, y0, x1, y1, radius = _copper_outline(params, placement)
    return (x0 - radius, y0 - radius, x1 + radius, y1 + radius)


def _box_distance(a: tuple, b: tuple) -> float:
    """Distance between two axis-aligned boxes (x0, y0, x1, y1, ...), mm; 0 when they meet."""
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


def preview_grid_origin(params: CastellatedModuleParams) -> tuple:
    """@brief Origin of the 2D preview grid of a castellated module, mm.

    @param params Module description.
    @return Centre of the first contact pad_positions yields (the point the
        preview grid has always passed through, user decision 2026-10-09),
        or (0, 0) without contacts. Used by the generator for the default
        text anchors and by the preview drawer for its grid: one definition.
    """
    first = next(iter(pad_positions(params)), None)
    return (0.0, 0.0) if first is None else (float(first.x), float(first.y))


def build_castellated_module(params: CastellatedModuleParams) -> Footprint:
    """@brief Build the KiCad footprint of the module described by `params`.

    @param params Module description; validated first.
    @return Footprint with, for each contact, exactly the lands the part offers:
        an SMD edge pad for half_hole; for half_and_through ONE plated pad
        whose copper is a stadium ("oval") running from the outer end of the
        edge land to beyond the through-hole ring, with the drill offset inside
        it (see continuous_pad), or the SMD edge pad alone when no
        through-hole is declared; a THT pad only for through_hole.
    @throws ValueError When `params` does not validate.
    @note A through_hole side emits no edge pad: a board without
        castellations cannot be reflowed onto edge pads, so such a footprint
        could not be soldered to the part. The footprint attribute stays
        "smd" whenever some side has castellations (the module is reflowed),
        "through_hole" when every contact is a through-hole. An "smd"
        footprint that carries drilled pads (continuous pads, or a side of
        through-holes next to castellated sides) gets
        SMD_WITH_DRILLED_PADS_NOTE appended to its description: KiCad flags
        that combination and leaves footprints that are not SMD out of the default
        position file, so the reason for keeping "smd" is written where the
        library user reads it (user decision 2026-09-24; confirmed the same
        day for modules mixing half_hole and through_hole sides, which are
        reflowed too, with no pins in the holes).
    @note The continuous pad carries F.Paste so the castellations can still be
        reflowed (the old SMD edge pad carried it). KiCad applies the paste to
        the whole copper, drill mouth included: pin-in-paste, which also fills
        the module's through-hole above. Kept on purpose (user decision
        2026-09-24), knowing it is more paste than the old edge land and can
        bridge contacts on a tight pitch; no paste-only pad is added. It
        carries no `pad_prop_castellated`: this footprint is the land on the HOST board, which is not cut by any
        board edge; the castellation belongs to the module and is in the 3D.
    @note half_hole and through_hole contacts are emitted exactly as before
        (byte-identical output, apart from the Reference/Value anchors that
        core.footprint.default_text_positions derives since decision D1).
    """
    params.validate()

    total_pads = sum(side.count for side in params.sides)
    has_edge_pads = any(side.contact_style != CONTACT_THROUGH_HOLE for side in params.sides)
    kind = "castellated" if params.needs_half_holes() else "a fori passanti"
    fp = Footprint(
        name=params.name,
        description=params.description or (
            f"Modulo {kind} {params.body_length}x{params.body_width}mm, "
            f"{total_pads} contatti su {len(params.sides)} lati, "
            "generato con kicadgen_tool"
        ),
        tags=params.tags,
        datasheet=params.datasheet,
        attr="smd" if has_edge_pads else "through_hole",
    )

    half_length = params.body_length / 2
    half_width = params.body_width / 2

    placements = list(pad_positions(params))
    # Continuous land (half_and_through with a hole): it replaces both the
    # edge land and the separate ring of the same contact.
    stadiums = [continuous_pad(params, placement) for placement in placements]
    # Exposed copper of each contact, enlarged by the silk-to-pad clearance:
    # the silkscreen must never cover a land (the edge pads protrude past
    # the body outline and cross it).
    keepouts = [keepout_box(*_copper_outline_bbox(params, placement), line_width=0.12)
                for placement in placements]

    # Body outline: it is the module proper, so it goes both on the
    # silkscreen (interrupted where the pads pass) and on F.Fab, whole (it
    # serves the assembly drawing and does not go into production).
    for graphic in silk_rectangle((-half_length, -half_width), (half_length, half_width), keepouts):
        fp.add_graphic(graphic)
    fp.add_graphic(FpRect(
        start=(-half_length, -half_width),
        end=(half_length, half_width),
        layer="F.Fab",
    ))

    # Courtyard: real envelope, body plus the pads protruding from the edge.
    lands = placements + [pad for pad in stadiums if pad is not None]
    extent_x = max([half_length] + [abs(p.x) + p.width / 2 for p in lands])
    extent_y = max([half_width] + [abs(p.y) + p.height / 2 for p in lands])
    fp.add_graphic(FpRect(
        start=(-extent_x - params.margin, -extent_y - params.margin),
        end=(extent_x + params.margin, extent_y + params.margin),
        layer="F.CrtYd",
        width=0.05,
    ))

    # Reference at the body centre on F.Fab (KLC F5.2): the silkscreen
    # ends up covered once the module is mounted, F.Fab stays readable in
    # the assembly drawings. Same helper as every other generator.
    fp.add_graphic(fab_reference_text((0, 0), (params.body_length, params.body_width)))

    # The marker indicates the pad numbered "1" (or the lowest number), not
    # the first pad produced: with reverse=True or with the sides listed
    # starting from the right the first one produced is another pin.
    first_number = pin_one(placement.number for placement in placements)
    for placement, stadium in zip(placements, stadiums):
        if stadium is not None:
            # ONE single pad per contact, as on the real board: continuous copper
            # from the edge past the ring, off-centre through hole inside.
            # KiCad: `at` is the HOLE, the offset moves the COPPER (verified with
            # pcbnew 10). Writing the copper centre in `at` swapped hole and
            # copper on the board.
            fp.add_pad(OffsetDrillPad(
                number=placement.number,
                pad_type="thru_hole",
                shape="oval",
                at=(stadium.drill_x, stadium.drill_y),
                size=(stadium.width, stadium.height),
                layers=["*.Cu", "*.Mask", "F.Paste"],
                drill=stadium.drill,
                drill_offset=stadium.drill_offset,
            ))
        elif placement.style != CONTACT_THROUGH_HOLE:
            fp.add_pad(Pad(
                number=placement.number,
                pad_type="smd",
                shape="roundrect",
                at=(placement.x, placement.y),
                size=(placement.width, placement.height),
                layers=["F.Cu", "F.Paste", "F.Mask"],
                roundrect_rratio=0.25,
            ))
        if placement.style == CONTACT_THROUGH_HOLE:
            geometry = params.contact_geometry(placement)
            fp.add_pad(Pad(
                number=placement.number,
                pad_type="thru_hole",
                shape="circle",
                at=params.thru_hole_centre(placement),
                size=(geometry.ring, geometry.ring),
                layers=["*.Cu", "*.Mask"],
                drill=geometry.drill,
            ))
        if placement.number == first_number:
            first_number = None   # a single marker even if the number repeats
            # Pin 1 marker, beside its pad along the edge (not outwards, where it
            # would end up under the pad itself).
            # Distance measured from the real copper: the continuous land can be
            # wider than the edge land (it contains the ring).
            # The chosen side is the one opposite to the other pads of the same
            # row, so the marker does not end up between pin 1 and its neighbour
            # (reverse numbering); with a single pad it stays towards the start.
            half_along = (stadium.across / 2 if stadium is not None
                          else (placement.height if placement.edge in ("left", "right")
                                else placement.width) / 2)
            vertical = placement.edge in ("left", "right")
            along = placement.y if vertical else placement.x
            row = [(p.y if vertical else p.x) for p in placements
                   if p.edge == placement.edge and p.number != placement.number]
            sign = 1.0 if row and sum(row) / len(row) < along else -1.0
            offset = sign * (half_along + 0.5)
            if vertical:
                center, direction = (placement.x, placement.y + offset), (0.0, sign)
            else:
                center, direction = (placement.x + offset, placement.y), (sign, 0.0)
            # Keepouts of the copper plus the silk clearance only: the dot's
            # own ink radius (radius + half stroke) is added by pin_one_marker.
            marker_keepouts = [keepout_box(*_copper_outline_bbox(params, p), line_width=0.0)
                               for p in placements]
            fp.add_graphic(pin_one_marker(center, marker_keepouts, direction))

        # Contact label (PE, B-, A+, GND...) on F.Fab, past the inner edge of
        # the pad: readable in the assembly drawing without having to consult
        # the pinout table separately. Only if the extraction (or the user)
        # provided it for this pin number.
        label = params.silk_labels.get(placement.number, "").strip()
        if label:
            gap = 0.6
            if stadium is not None:
                # Past the inner end of the continuous copper, not on top of it.
                nx, ny = INWARD_FOOTPRINT[placement.edge]
                reach = (stadium.length / 2 + gap)
                label_at = (_clean(stadium.x + nx * reach), _clean(stadium.y + ny * reach))
            elif placement.edge == "left":
                label_at = (placement.x + placement.width / 2 + gap, placement.y)
            elif placement.edge == "right":
                label_at = (placement.x - placement.width / 2 - gap, placement.y)
            elif placement.edge == "top":
                label_at = (placement.x, placement.y + placement.height / 2 + gap)
            else:
                label_at = (placement.x, placement.y - placement.height / 2 - gap)
            fp.add_graphic(FpText(
                kind="user", text=label, at=label_at, layer="F.Fab",
                size=0.8, thickness=0.12,
            ))

    if fp.attr == "smd" and any(pad.pad_type == "thru_hole" for pad in fp.pads):
        # The criterion is the combination that KiCad flags (SMD type with
        # drilled pads), not the contact type: it holds for the stepped lands
        # and for a module that mixes half_hole and through_hole sides. The
        # extension to mixed modules is confirmed by the user (2026-09-24,
        # choice «A»): those too are placed and reflow-soldered, without pins
        # in the holes.
        fp.description = f"{fp.description} {SMD_WITH_DRILLED_PADS_NOTE}".strip()
    # Decision D1 (user, 2026-10-14): Reference above and Value below the
    # envelope; a user-chosen position is applied afterwards (place_texts).
    return fp.place_default_texts(preview_grid_origin(params))
