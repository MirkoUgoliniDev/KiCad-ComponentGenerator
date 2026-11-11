"""@brief Generic package and pad geometry, not limited to pin matrices.

The model does not assume that the pins form a matrix: the pin header
matrix is only a special case. Coordinates are local to the package and
seen from the top, i.e. in the same system KiCad uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Optional, Sequence
from .footprint import require_kicad_reach, same_kicad_length, validate_pad_number
from .output_names import validate_output_name
from .tht_estimates import normalised_lead, normalised_sources
from ..services.messages import tx


PACKAGE_FAMILIES = {
    "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp", "module",
    "castellated", "custom",
}
PAD_TYPES = {"smd", "thru_hole", "np_thru_hole", "connect"}
# Custom pads require an anchor and primitives, and trapezoid pads require
# KiCad's `rect_delta` (which side shrinks, by how much): a PinSpec carries
# only a centre, a size and a rotation, so neither can be described here.
# "trapezoid" used to be accepted and written WITHOUT its delta, which KiCad
# loads as a plain rectangle: refusing it is the honest option (review
# 2026-10-27, G7). `core.footprint.Pad` still writes a trapezoid when given
# its `rect_delta` explicitly.
PAD_SHAPES = {"circle", "rect", "oval", "roundrect"}

#: Corner ratio of a "roundrect" pad as `generators.package.build_package`
#: writes it (`roundrect_rratio`): the corner radius is this fraction of the
#: smaller pad side. One value for the file and for the overlap check.
ROUNDRECT_RRATIO = 0.25

#: Declared pad-coordinate origins. Only "body_center" says where the body is;
#: for the others the body centre is estimated (see PackageGeometry.body_frame_offset).
ORIGINS = {"body_center", "pin1", "custom"}


def _finite(value: float, name: str, positive: bool = False) -> float:
    """Return `value` as a float, refusing anything but a real int/float.

    A numeric string such as "90" is refused, not converted: the geometry
    keeps the value it was given and later arithmetic on it (orientation,
    rotation) would raise TypeError far from the cause (review 2026-11-03,
    N5). `core.validation.finite_number` is the shared type rule.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(tx("{name} deve essere numerico", name=name))
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(tx("{name} deve essere numerico", name=name)) from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(tx("{name} deve essere {value} valido",
                            name=name,
                            value='>' if positive else 'finito'))
    return value


@dataclass(frozen=True)
class PinSpec:
    """@brief Describe one package pin and its physical pad geometry."""
    number: str
    x_mm: float
    y_mm: float
    pad_length_mm: float
    pad_width_mm: float
    side: Optional[str] = None
    name: str = ""
    pad_type: str = "smd"
    shape: str = "roundrect"
    rotation_deg: float = 0.0
    drill_mm: Optional[float] = None
    #: Second side of the hole, mm, along the pad's width axis (`pad_width_mm`),
    #: for a slot (oval hole): `drill_mm` is then the side along the pad's
    #: length axis. None, or equal to `drill_mm`, is a round hole. The hole
    #: turns with the pad (`rotation_deg`), as KiCad's `(drill oval w h)`.
    drill_length_mm: Optional[float] = None
    #: Declared section of the terminal that goes through the hole, mm:
    #: {"shape": "round", "diameter_mm": d} or {"shape": "flat", "width_mm": w,
    #: "thickness_mm": t} (`core.tht_estimates`). None when the drawing does
    #: not quote it, and for every project saved before 2026-11-10. Each pin
    #: has its own: one part may mix round pins and flat blades.
    lead: Optional[dict] = None
    #: Provenance of this pad's values: any of the keys "drill_mm",
    #: "pad_size", "shape", "lead", each "read", "derived" or "estimated"
    #: (`core.tht_estimates.SOURCE_WORDS`). None states nothing: no mark and
    #: no finding, as before the provenance existed. Never written to the
    #: .kicad_mod; the footprint checks read it from the `Pad`.
    sources: Optional[dict] = None

    def __post_init__(self) -> None:
        """@brief Normalise the shape and take private copies of `lead` and `sources`.

        @note `lead` and `sources` are replaced by validated copies
            (`core.tht_estimates.normalised_lead`, `normalised_sources`; an
            empty `sources` becomes None), so the frozen pin does not share a
            dict with its caller. Values out of contract are left as given
            for `validate`, which refuses them: construction never raises.
        """
        for name, normalise in (("lead", normalised_lead), ("sources", normalised_sources)):
            try:
                object.__setattr__(self, name, normalise(getattr(self, name), str(self.number)))
            except ValueError:
                pass
        self._circle_with_two_sides_is_an_oval()

    def __hash__(self) -> int:
        """@brief Hash of the pad's identity and geometry, so a pin stays usable in sets and as a key.

        @return A hash of the scalar fields. `lead` and `sources` are dicts
            and stay out of it: pins equal by `==` still hash alike, which is
            all a hash must guarantee.
        """
        return hash((self.number, self.x_mm, self.y_mm, self.pad_length_mm, self.pad_width_mm, self.side,
                     self.name, self.pad_type, self.shape, self.rotation_deg, self.drill_mm,
                     self.drill_length_mm))

    def _circle_with_two_sides_is_an_oval(self) -> None:
        """@brief Turn a "circle" with two different sides into the "oval" KiCad would need.

        @note KiCad draws a circle pad of diameter equal to its FIRST size
            and ignores the second one, so `(size 2 1)` on a circle is a
            2 mm disc, not the 2 x 1 mm land that was measured. The extractor
            defaults a through-hole pad without a declared shape to "circle"
            whatever its two measured sides, so refusing the pair would lose
            measured data: the land with two different sides is an oval (a
            stadium of those sides), which keeps both measures and is what
            KiCad then draws. Sides that are not finite numbers are left to
            `validate`, which refuses them.
        @note «Different» means written as two different numbers at KiCad's
            1 nm resolution (`core.footprint.same_kicad_length`): 1.7 and
            1.7000000000000002, the float noise of a unit conversion, are
            one diameter and the pad stays the circle that was declared.
        """
        if self.shape != "circle":
            return
        try:
            length, width = float(self.pad_length_mm), float(self.pad_width_mm)
        except (TypeError, ValueError, OverflowError):
            return
        if math.isfinite(length) and math.isfinite(width) and not same_kicad_length(length, width):
            object.__setattr__(self, "shape", "oval")

    def validate(self) -> None:
        """@brief Validate the object against its supported contract.

        @throws ValueError When a value is out of contract, or the pad's copper
            reaches beyond KICAD_COORD_LIMIT_MM (`core.footprint.require_kicad_reach`).
        @note The bound applies in the pin's own frame too (a source view
            or a pin-1 origin): a drawing whose pads lie more than two metres
            from its own origin describes no component, and the final frame
            is checked again by `PackageGeometry.pins_seen_from_top`.
        @note An empty number is accepted only for a non-plated hole: KiCad
            writes mechanical holes with number "" (no net), while every
            electrical pad needs a number to be connected.
        @note A number holding whitespace or a control character is refused
            (`core.footprint.validate_pad_number`), the rule the symbol
            applies to its pin numbers: a pad « 1» and a symbol pin «1»
            would no longer match.
        @note A missing number (None) is refused: turned into text it would
            name the pad «None». A hole size on a pad type that has no hole
            ("smd", "connect") is refused like `drill_length_mm`: the pad
            would be written as SMD and the hole lost in silence; whoever
            builds the pin states the type or drops the hole.
        @note `lead` and `sources` must follow the contract of
            `core.tht_estimates` (known shape and words, finite positive
            sizes, no unknown key).
        """
        if self.number is None:
            raise ValueError(tx("il numero del pin non puo' essere vuoto"))
        if not str(self.number).strip() and self.pad_type != "np_thru_hole":
            raise ValueError(tx("il numero del pin non puo' essere vuoto"))
        validate_pad_number(self.number)
        x = _finite(self.x_mm, "x_mm")
        y = _finite(self.y_mm, "y_mm")
        length = _finite(self.pad_length_mm, "pad_length_mm", True)
        width = _finite(self.pad_width_mm, "pad_width_mm", True)
        if self.pad_type not in PAD_TYPES:
            raise ValueError(tx("pad_type non valido: {pad_type}", pad_type=self.pad_type))
        if self.shape not in PAD_SHAPES:
            raise ValueError(tx("shape non valida: {shape}", shape=self.shape))
        if self.side is not None and self.side not in {"left", "right", "top", "bottom", "center"}:
            raise ValueError(tx("lato non valido: {side}", side=self.side))
        rotation = _finite(self.rotation_deg, "rotation_deg")
        # The copper, not only the centre, must stay within KiCad's range
        # (review 2026-10-29, G3): checked on the rotated outline, so a pad
        # turned by any angle is bounded by its real corners.
        require_kicad_reach(pad_reach(x, y, length, width, rotation), str(self.number))
        if self.pad_type in {"thru_hole", "np_thru_hole"}:
            if self.drill_mm is None:
                raise ValueError(tx("il pin {number} richiede drill_mm", number=self.number))
            drill = _finite(self.drill_mm, "drill_mm", True)
            slot = None if self.drill_length_mm is None else _finite(self.drill_length_mm, "drill_length_mm", True)
            if self.pad_type == "thru_hole":
                if slot is None or slot == drill:
                    # A round hole must leave copper on every side.
                    if drill >= min(length, width):
                        raise ValueError(tx("Il foro del pin {number} deve essere minore di entrambe le "
                                            "dimensioni del pad.", number=self.number))
                elif drill >= length or slot >= width:
                    # A slot leaves copper on each axis separately: its first
                    # side runs along the pad length, the second along the width.
                    raise ValueError(tx("Ogni lato dell'asola del pin {number} deve essere minore del lato "
                                        "del pad sullo stesso asse.", number=self.number))
        elif self.drill_mm is not None:
            # An SMD or edge-connector pad carrying a hole: writing it would
            # drop the hole without a word. The type is never guessed from it.
            raise ValueError(tx("il pin {number} non ha foro: drill_mm non ammesso", number=self.number))
        elif self.drill_length_mm is not None:
            raise ValueError(tx("il pin {number} non ha foro: drill_length_mm non ammesso", number=self.number))
        # Raise on a lead or a provenance out of contract (`__post_init__`
        # left such a value as it was given).
        normalised_lead(self.lead, str(self.number))
        normalised_sources(self.sources, str(self.number))

    def hole_sizes(self) -> Optional[tuple]:
        """@brief The hole as (size along the pad length, size along the pad width), mm, or None.

        @return (drill_mm, drill_mm) for a round hole, (drill_mm,
            drill_length_mm) for a slot, None for a pad without a hole (an
            SMD pad, or a holed type whose drill is not given yet).
        """
        if self.pad_type not in {"thru_hole", "np_thru_hole"} or self.drill_mm is None:
            return None
        second = self.drill_mm if self.drill_length_mm is None else self.drill_length_mm
        return (float(self.drill_mm), float(second))

    def is_slot(self) -> bool:
        """@brief Whether the hole is an oval slot.

        @return True when the hole has two different sides; False for a round
            hole or a pad without a hole.
        """
        sizes = self.hole_sizes()
        return sizes is not None and sizes[0] != sizes[1]


@dataclass
class PackageGeometry:
    """@brief Describe a package body and its ordered pins."""
    name: str = "Package"
    family: str = "unknown"
    body_length_mm: Optional[float] = None
    body_width_mm: Optional[float] = None
    body_height_mm: Optional[float] = None
    origin: str = "body_center"
    source_view: str = "top"
    orientation_deg: float = 0.0
    mirrored: bool = False
    confidence: str = "low"
    needs_review: bool = True
    pins: list[PinSpec] = field(default_factory=list)
    #: Lead shape seen in a side view, one of LEAD_FORMS: "j_lead" tells the
    #: structural checks that the lands lie under the body edge by design
    #: (package_doubts); "unknown" when the drawing does not show it.
    lead_form: str = "unknown"

    def validate(self) -> None:
        """@brief Validate the object against its supported contract.

        @throws ValueError When the name is not a plain file name
            (`core.output_names`), a field is out of contract, two pads
            overlap, or pads sharing a number declare different pin names.
        @note Several pads may share one number, as KiCad allows (a thermal
            pad split into several lands, several GND pads): they are the same
            electrical pin. What is refused is a shared number with
            CONFLICTING non-empty names, the structural sign of a misread
            number rather than of a multi-pad pin.
        @note Pads sharing one non-empty number may also overlap (an exposed
            pad with thermal-via lands numbered like it); pads of different
            numbers, or unnumbered holes, may not.
        @note The overlap test is exact on the copper KiCad draws but only
            measures pairs whose enclosing boxes come within tolerance
            (`overlapping_pad_pairs`): a 400-pad grid validates in
            milliseconds, with the verdicts and the reported pair of a
            check of every pair.
        """
        # The package name is the default footprint name (`build_package`)
        # and so a file stem: the shared rule refuses an empty name or a path
        # such as "..\evil" (2026-10-27 review, H4).
        validate_output_name(self.name)
        if self.family not in PACKAGE_FAMILIES:
            raise ValueError(tx("famiglia package non supportata: {family}", family=self.family))
        if self.source_view not in {"top", "bottom"}:
            raise ValueError(tx("source_view deve essere top o bottom"))
        if self.origin not in ORIGINS:
            raise ValueError(tx("origin non supportata"))
        # bool("false") is True: only a real boolean may decide a mirror.
        if not isinstance(self.mirrored, bool):
            raise ValueError(tx("mirrored deve essere true o false"))
        _finite(self.orientation_deg, "orientation_deg")
        if self.lead_form not in LEAD_FORMS:
            raise ValueError(tx("lead_form non valido: {lead_form}", lead_form=self.lead_form))
        if self.confidence not in {"high", "medium", "low"}:
            raise ValueError(tx("confidence non valida"))
        for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
            value = getattr(self, key)
            if value is not None:
                _finite(value, key, True)
        # The model exists to describe explicit pads: without pads there is no
        # footprint to write, and an empty .kicad_mod would pass as valid.
        if not self.pins:
            raise ValueError(tx("la geometria package non ha pad"))
        names: dict = {}
        outlines = []
        for pin in self.pins:
            try:
                pin.validate()
                name = str(pin.name or "").strip()
                known = names.get(pin.number)
                if known and name and known.casefold() != name.casefold():
                    raise ValueError(tx("pin duplicato: {number}", number=pin.number))
            except ValueError:
                # The pads validated so far may already overlap: that error
                # comes first in pin order, as it always did.
                _raise_first_overlap(outlines)
                raise
            names[pin.number] = known or name
            # The copper KiCad draws (`pad_copper_shape`): a rotated core
            # grown by the corner radius, so a circle is a disc, an oval a
            # stadium and a roundrect keeps its rounded corners. Circular
            # host-header lands sitting diagonally at a board corner, or a
            # disc next to a rectangle's corner, are judged on the real
            # copper, not on bounding squares that would overlap; rotated
            # rectangles (QFN) keep their rotated outline.
            outlines.append((pin, pad_copper_shape(pin)))
        _raise_first_overlap(outlines)

    def body_frame_offset(self) -> tuple:
        """@brief Point of the pad coordinates taken as the body centre, mm.

        @return (x, y) in the source-view pad coordinates; (0, 0) when the
            origin is declared "body_center".
        @note DECLARED HEURISTIC. With origin "pin1" or "custom" the pads are
            given relative to a point that is not the body centre, and nothing
            in the geometry says where the body is relative to that point. The
            body is then assumed to be centred on the bounding box of the pad
            centres, which holds for the symmetric families (DIP, SOIC, TSSOP,
            QFN, QFP) and is only an estimate for asymmetric parts (connectors,
            parts with an offset tab). What would make it unnecessary: a
            declared body offset from the pad origin in the extraction schema.
            `frame_notes` states the estimate so it is never silent.
        """
        if self.origin == "body_center" or not self.pins:
            return (0.0, 0.0)
        # Mounting holes are not part of the pin field: an asymmetric hole
        # would drag the estimated body centre (all pads count only when the
        # geometry has nothing but holes).
        field = [pin for pin in self.pins if pin.pad_type != "np_thru_hole"] or self.pins
        xs = [float(pin.x_mm) for pin in field]
        ys = [float(pin.y_mm) for pin in field]
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def frame_notes(self) -> list:
        """@brief Notes about estimated placement of the body relative to the pads.

        @return Localized notes; empty when the origin is the body centre.
        """
        if self.origin == "body_center":
            return []
        return [tx("Geometria package con origine «{origin}»: il centro del corpo e' "
                   "stimato al centro del campo dei pad (stima, da verificare sul "
                   "disegno quotato).", origin=self.origin)]

    def pins_seen_from_top(self) -> list[PinSpec]:
        """@brief Return the pins normalized from the source view to KiCad's top view.

        @return New PinSpec objects in footprint coordinates (mm, Y down) whose
            `rotation_deg` is the KiCad pad angle.
        @note Every angle follows KiCad's convention: degrees counter-clockwise
            as seen on screen, with Y pointing down, which is the direction
            KiCad rotates a pad written as `(at x y angle)` (verified with
            pcbnew 10: angle 30 turns the local +X axis to (cos30, -sin30)).
            Positions and pad angles are rotated by the SAME convention, so a
            pad keeps its alignment to its own position at any angle, not only
            at multiples of 90 degrees.
        @note A bottom view (and an explicit mirror) is a reflection across
            the Y axis: it negates X and, with it, every pad angle; the package
            orientation is applied after the reflection.
        @note The footprint origin is always the (possibly estimated) body
            centre: pads declared from pin 1 or a custom origin are first
            moved by `body_frame_offset`, so body outline, courtyard and 3D
            model, all drawn around (0, 0), line up with the pads.
        """
        self.validate()
        result = []
        angle = math.radians(self.orientation_deg % 360)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        flips = int(self.source_view == "bottom") + int(self.mirrored)
        shift_x, shift_y = self.body_frame_offset()
        for pin in self.pins:
            x, y = pin.x_mm, pin.y_mm
            if self.origin != "body_center":
                x, y = round(x - shift_x, 9), round(y - shift_y, 9)
            pad_angle = pin.rotation_deg
            if flips % 2:
                x, pad_angle = -x, -pad_angle
            rx, ry = _rotate_kicad(x, y, cos_a, sin_a)
            # The package orientation and the frame shift move the pads:
            # the coordinates KiCad receives are bounded here, where they are
            # final (review 2026-10-29, G3).
            require_kicad_reach(pad_reach(rx, ry, float(pin.pad_length_mm), float(pin.pad_width_mm),
                                          float(pad_angle) + float(self.orientation_deg)), str(pin.number))
            result.append(PinSpec(
                number=pin.number, x_mm=rx + 0.0, y_mm=ry + 0.0,
                pad_length_mm=pin.pad_length_mm, pad_width_mm=pin.pad_width_mm,
                side=pin.side, name=pin.name, pad_type=pin.pad_type,
                shape=pin.shape, rotation_deg=_normal_angle(pad_angle + self.orientation_deg),
                drill_mm=pin.drill_mm, drill_length_mm=pin.drill_length_mm,
                # The terminal and the provenance belong to the pad, whatever
                # the view it is seen from.
                lead=pin.lead, sources=pin.sources,
            ))
        return result


#: Copper separation, mm, at or below which two pads count as overlapping.
#: It absorbs float noise only: pads that merely touch are refused too.
_OVERLAP_EPS = 1e-6


def _copper_box(shape: tuple) -> tuple:
    """Axis-aligned box (x0, y0, x1, y1), mm, enclosing a `pad_copper_shape` copper."""
    core, radius = shape
    xs = [point[0] for point in core]
    ys = [point[1] for point in core]
    return (min(xs) - radius, min(ys) - radius, max(xs) + radius, max(ys) + radius)


def overlapping_pad_pairs(outlines: Sequence) -> list:
    """@brief Pairs of pads whose copper overlaps or touches, in validation order.

    @param outlines (pin, `pad_copper_shape`) tuples in pin order.
    @return (j, i) index pairs with i < j, sorted as a pairwise loop over j
        then i would meet them; pads sharing the same non-empty number are
        never paired (they are one electrical pin, see PackageGeometry.validate).
    @note Same verdicts as testing every pair with `copper_separation`, at
        sweep cost: two coppers whose enclosing boxes are apart by more than
        `_OVERLAP_EPS` along X or Y are at least that far apart, so only pairs
        whose boxes come within the tolerance are measured exactly. The boxes
        are swept along the axis where the pad field is wider, so a column of
        pads is not compared all against all.
    """
    if len(outlines) < 2:
        return []
    boxes = [_copper_box(shape) for _pin, shape in outlines]
    spread_x = max(box[2] for box in boxes) - min(box[0] for box in boxes)
    spread_y = max(box[3] for box in boxes) - min(box[1] for box in boxes)
    lo, hi, cross_lo, cross_hi = (0, 2, 1, 3) if spread_x >= spread_y else (1, 3, 0, 2)
    order = sorted(range(len(boxes)), key=lambda index: boxes[index][lo])
    candidates = []
    for position, first in enumerate(order):
        box_a = boxes[first]
        for second in order[position + 1:]:
            box_b = boxes[second]
            if box_b[lo] > box_a[hi] + _OVERLAP_EPS:
                break
            if (box_b[cross_lo] > box_a[cross_hi] + _OVERLAP_EPS
                    or box_a[cross_lo] > box_b[cross_hi] + _OVERLAP_EPS):
                continue
            candidates.append((max(first, second), min(first, second)))
    pairs = []
    for later, earlier in sorted(candidates):
        number_a = str(outlines[later][0].number)
        number_b = str(outlines[earlier][0].number)
        # Pads repeating one number are the same pin (an exposed pad split
        # into lands, thermal vias numbered like their pad): their copper
        # may overlap by design (review 2026-11-03, N9) -- unless they are
        # COPIES of one pad (a double reading: same size, each centre inside
        # the other's box), which is an error, not a pin (generality audit
        # 2026-11-03, GA9).
        if number_a.strip() and number_a == number_b and not _copies(boxes[later], boxes[earlier]):
            continue
        if copper_separation(outlines[later][1], outlines[earlier][1]) <= _OVERLAP_EPS:
            pairs.append((later, earlier))
    return pairs


def _copies(box_a, box_b) -> bool:
    """True when two copper boxes are the same pad read twice: same size within the overlap tolerance and each centre inside the other box."""
    size_a = (box_a[2] - box_a[0], box_a[3] - box_a[1])
    size_b = (box_b[2] - box_b[0], box_b[3] - box_b[1])
    if any(abs(a - b) > _OVERLAP_EPS for a, b in zip(size_a, size_b)):
        return False

    def inside(box, other):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return other[0] <= cx <= other[2] and other[1] <= cy <= other[3]
    return inside(box_a, box_b) and inside(box_b, box_a)


def _raise_first_overlap(outlines: Sequence) -> None:
    """Raise the overlap error of the first overlapping pair in pin order, if any."""
    pairs = overlapping_pad_pairs(outlines)
    if pairs:
        later, earlier = pairs[0]
        raise ValueError(
            tx("pad sovrapposti tra i pin {other_number} e {number}",
               other_number=outlines[earlier][0].number,
               number=outlines[later][0].number)
        )


def _rotate_kicad(x: float, y: float, cos_a: float, sin_a: float) -> tuple:
    """Rotate (x, y) counter-clockwise on screen (Y down), as KiCad rotates a pad."""
    return (x * cos_a + y * sin_a, -x * sin_a + y * cos_a)


def _normal_angle(angle: float) -> float:
    """Map an angle in degrees to [0, 360) with no float noise and no -0."""
    value = round(float(angle) % 360.0, 9)
    return 0.0 if value in (0.0, 360.0) else value + 0.0


def pad_outline(x: float, y: float, size_x: float, size_y: float,
                rotation_deg: float = 0.0) -> list:
    """@brief Corners of a rotated rectangular pad, in mm.

    @param x Pad centre X, mm.
    @param y Pad centre Y, mm (KiCad axes, Y down).
    @param size_x Pad size along its local X, mm.
    @param size_y Pad size along its local Y, mm.
    @param rotation_deg KiCad pad angle (counter-clockwise on screen), degrees.
    @return Four (x, y) corners in order. For round or rounded shapes this is
        the bounding rectangle, a conservative envelope of the copper.
    """
    angle = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = []
    for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        rx, ry = _rotate_kicad(dx * size_x / 2, dy * size_y / 2, cos_a, sin_a)
        corners.append((x + rx, y + ry))
    return corners


def pad_copper_shape(pin: "PinSpec") -> tuple:
    """@brief The copper of a pad as KiCad draws it: a convex core grown by a radius.

    @param pin Pad whose shape, sizes, rotation and centre are used (mm, KiCad axes).
    @return (core, radius): `core` is a list of 1 (circle centre), 2 (the
        axis of an oval) or 4 (rectangle corners, in order) points and
        `radius` the distance, mm, the copper extends beyond the core.
    @note "circle" is a disc of diameter `pad_length_mm` (KiCad ignores the
        second size; `PinSpec.__post_init__` turns an unequal circle into an
        oval first); "oval" a stadium of the two sides; "roundrect" the
        rectangle with corners of radius `ROUNDRECT_RRATIO` times the smaller
        side, as `build_package` writes it; "rect" the plain rectangle.
    """
    x, y = float(pin.x_mm), float(pin.y_mm)
    length, width = float(pin.pad_length_mm), float(pin.pad_width_mm)
    if pin.shape == "circle":
        return [(x, y)], length / 2
    if pin.shape == "oval":
        radius = min(length, width) / 2
    elif pin.shape == "roundrect":
        radius = min(length, width) * ROUNDRECT_RRATIO
    else:
        radius = 0.0
    core = pad_outline(x, y, length - 2 * radius, width - 2 * radius, float(pin.rotation_deg))
    # A degenerate core (the axis of an oval, the centre of a square oval)
    # keeps only its distinct points, so every edge used below has a length
    # or is a single point.
    unique = []
    for point in core:
        if all(math.hypot(point[0] - q[0], point[1] - q[1]) > 1e-12 for q in unique):
            unique.append(point)
    return unique, radius


def _point_segment_distance(p, s, e) -> float:
    """Distance, mm, from point p to the segment s-e (a point when s == e)."""
    dx, dy = e[0] - s[0], e[1] - s[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / length2))
    return math.hypot(p[0] - s[0] - t * dx, p[1] - s[1] - t * dy)


def _segment_distance(a, b, c, d) -> float:
    """Distance, mm, between segments a-b and c-d (0 when they properly cross)."""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return 0.0
    return min(_point_segment_distance(a, c, d), _point_segment_distance(b, c, d),
               _point_segment_distance(c, a, b), _point_segment_distance(d, a, b))


def _edges(points: Sequence) -> list:
    """Edges of a convex core: one degenerate edge for a point, one for a segment."""
    if len(points) <= 2:
        return [(points[0], points[-1])]
    return [(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]


def _inside_convex(point, polygon: Sequence) -> bool:
    """True when `point` lies inside or on the convex polygon (3+ corners, any winding)."""
    signs = set()
    count = len(polygon)
    for i in range(count):
        (ax, ay), (bx, by) = polygon[i], polygon[(i + 1) % count]
        cross = (bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax)
        if abs(cross) > 1e-12:
            signs.add(cross > 0)
    return len(signs) <= 1


def copper_separation(first: tuple, second: tuple) -> float:
    """@brief Free copper between two pads given by `pad_copper_shape`, mm.

    @param first (core, radius) of one pad.
    @param second (core, radius) of the other pad.
    @return The distance between the two copper areas: positive when they
        are apart, zero when they touch, negative when they overlap (its
        magnitude is then only a lower bound of the penetration).
    @note Exact for every shape `pad_copper_shape` describes: two disjoint
        convex cores are closest along a pair of their edges, so the copper
        gap is that edge distance minus both radii; overlapping cores are
        found by the separating-axis test (two rectangles) or by a core
        point lying inside the other rectangle.
    """
    (core_a, radius_a), (core_b, radius_b) = first, second
    if len(core_a) >= 3 and len(core_b) >= 3:
        gap = polygon_separation(core_a, core_b)
        if gap <= 0:
            return gap - radius_a - radius_b
    for inner, outer in ((core_a, core_b), (core_b, core_a)):
        if len(outer) >= 3 and _inside_convex(inner[0], outer):
            return -radius_a - radius_b
    distance = min(_segment_distance(a, b, c, d)
                   for a, b in _edges(core_a) for c, d in _edges(core_b))
    return distance - radius_a - radius_b


def pad_reach(x: float, y: float, size_x: float, size_y: float, rotation_deg: float = 0.0) -> float:
    """@brief Largest |x| or |y| reached by the corners of a rotated pad, mm.

    @param x Pad centre X, mm.
    @param y Pad centre Y, mm.
    @param size_x Pad size along its local X, mm.
    @param size_y Pad size along its local Y, mm.
    @param rotation_deg KiCad pad angle, degrees.
    @return max over the `pad_outline` corners of max(|x|, |y|).
    """
    return max(max(abs(cx), abs(cy)) for cx, cy in pad_outline(x, y, size_x, size_y, rotation_deg))


def polygon_separation(first: Sequence, second: Sequence) -> float:
    """@brief Largest gap between two convex polygons along their edge normals, mm.

    @param first Corners of a convex polygon, in order.
    @param second Corners of a convex polygon, in order.
    @return A positive value when a separating axis exists (the polygons are
        apart by at least that much along it), zero when they touch, negative
        when they overlap (separating axis theorem).
    """
    best = -math.inf
    for polygon in (first, second):
        count = len(polygon)
        for i in range(count):
            (ax, ay), (bx, by) = polygon[i], polygon[(i + 1) % count]
            nx, ny = by - ay, ax - bx
            norm = math.hypot(nx, ny)
            if norm == 0:
                continue
            nx, ny = nx / norm, ny / norm
            proj_a = [px * nx + py * ny for px, py in first]
            proj_b = [px * nx + py * ny for px, py in second]
            gap = max(min(proj_b) - max(proj_a), min(proj_a) - max(proj_b))
            best = max(best, gap)
    return best


def mark_user_edited(before: PinSpec, after: PinSpec) -> PinSpec:
    """@brief Provenance of a pad after the user edited it by hand: what was typed is no longer an estimate.

    @param before The pad before the edit.
    @param after The pad the editor built from it (e.g. `dataclasses.replace`).
    @return `after` with its `sources` updated: "drill_mm" becomes "read"
        when either hole side changed, "pad_size" when either pad side
        changed, "shape" when the shape changed; the "drill_mm" entry is
        dropped when the pad no longer has a hole. `after` itself when it
        carries no `sources` (a project without provenance gains none) or
        nothing of the three changed.
    @note «read» is the contract's word for a value that is not the
        program's guess: a value the user typed is the user's own reading.
        A move or a rotation changes no provenance. Sizes are compared as
        KiCad writes them (1 nm), so a value re-typed unchanged stays marked.
    """
    if not after.sources:
        return after

    def changed(*names: str) -> bool:
        """Whether one of the named sizes differs between the two pads."""
        for name in names:
            old, new = getattr(before, name), getattr(after, name)
            if (old is None) != (new is None):
                return True
            if old is not None and not same_kicad_length(old, new):
                return True
        return False

    sources = dict(after.sources)
    if after.hole_sizes() is None:
        sources.pop("drill_mm", None)
    elif "drill_mm" in sources and changed("drill_mm", "drill_length_mm"):
        sources["drill_mm"] = "read"
    if "pad_size" in sources and changed("pad_length_mm", "pad_width_mm"):
        sources["pad_size"] = "read"
    if "shape" in sources and before.shape != after.shape:
        sources["shape"] = "read"
    if sources == after.sources:
        return after
    return replace(after, sources=sources or None)


def mark_user_verified(pin: PinSpec) -> PinSpec:
    """@brief Provenance of a pad whose values the user checked on the drawing and declared right.

    @param pin The pad as it stands (after any edit of the same dialog).
    @return `pin` with every pad value its `sources` marks as derived or
        estimated ("drill_mm", "pad_size", "shape":
        `tht_estimates.unverified_values`) turned into "read"; "lead" and
        the values already read are kept. `pin` itself when nothing is
        marked unverified (a pad without provenance gains none).
    @note The explicit counterpart of `mark_user_edited` (review 2026-11-11,
        K5, user decision): a value the user verified and found right is
        not re-typed, so no edit can tell it apart from an untouched one;
        the pad dialog's «Valori verificati sul disegno» states it instead.
        A "drill_mm" entry of a pad that has no hole is not a value and is
        dropped, as `mark_user_edited` does.
    """
    from .tht_estimates import unverified_values
    pending = unverified_values(pin.sources)
    if not pending:
        return pin
    sources = dict(pin.sources)
    for key in pending:
        sources[key] = "read"
    if pin.hole_sizes() is None:
        sources.pop("drill_mm", None)
    return replace(pin, sources=sources or None)


def package_from_mapping(data: dict) -> PackageGeometry:
    """@brief Build and validate a PackageGeometry from the AI tool JSON; raise ValueError when invalid.

    @param data The decoded JSON object returned by the AI tool: package
        fields plus a `pins` list of pad dicts (number, x_mm, y_mm,
        pad_length_mm, pad_width_mm and the optional side, name, pad_type,
        shape, rotation_deg, drill_mm, drill_length_mm, lead, sources). A
        null `mirrored` means «not mirrored», a null `lead_form` means «not
        observed»; a missing or null `lead` or `sources` means «not
        declared» (every project saved before 2026-11-10).
    @return The validated PackageGeometry.
    @throws ValueError When `data` is not an object, a pin's `number` is
        null, or the resulting geometry does not validate (see
        `PackageGeometry.validate`).
    @note The inverse is `dataclasses.asdict(geometry)`: it writes `lead`
        and `sources` back (null when absent), so a geometry survives a
        save and a reload with its terminals and its provenance.
    @throws KeyError When a pin dict lacks one of its required fields.
    """
    if not isinstance(data, dict):
        raise ValueError(tx("geometria package non strutturata"))
    raw_pins = data.get("pins") or []
    for p in raw_pins:
        # str(None) is "None": a null number would become a pad of that name.
        if isinstance(p, dict) and "number" in p and p["number"] is None:
            raise ValueError(tx("il numero del pin non puo' essere vuoto"))
    pins = [PinSpec(
        # Surrounding blanks are dropped at the boundary, as the symbol drops
        # them from a pin number; inner blanks stay and `validate` refuses them.
        number=str(p["number"]).strip(), x_mm=p["x_mm"], y_mm=p["y_mm"],
        pad_length_mm=p["pad_length_mm"], pad_width_mm=p["pad_width_mm"],
        side=p.get("side"), name=str(p.get("name") or ""),
        pad_type=p.get("pad_type", "smd"), shape=p.get("shape", "roundrect"),
        rotation_deg=p.get("rotation_deg", 0), drill_mm=p.get("drill_mm"),
        # Optional (2026-11-06): absent or null is a round hole, as before.
        drill_length_mm=p.get("drill_length_mm"),
        # Optional (2026-11-10): the terminal's section and the provenance
        # of the pad's values; absent or null states nothing.
        lead=p.get("lead"), sources=p.get("sources"),
    ) for p in raw_pins]
    geometry = PackageGeometry(
        name=str(data.get("name") or "Package"),
        family=data.get("family", "unknown"), body_length_mm=data.get("body_length_mm"),
        body_width_mm=data.get("body_width_mm"), body_height_mm=data.get("body_height_mm"),
        origin=data.get("origin", "body_center"), source_view=data.get("source_view", "top"),
        orientation_deg=data.get("orientation_deg", 0),
        # Optional in the schema: an explicit null means «not mirrored», as
        # before; only a non-boolean value is refused (validate).
        mirrored=False if data.get("mirrored") is None else data.get("mirrored"),
        confidence=data.get("confidence", "low"), needs_review=data.get("needs_review", True), pins=pins,
        # Optional, added 2026-10-15: absent or null means «not observed».
        lead_form="unknown" if data.get("lead_form") is None else data.get("lead_form"),
    )
    geometry.validate()
    return geometry


#: Families whose leads leave the body through its SIDES and bend down to a
#: land at or beyond the body edge (gull-wing or J-lead SOIC/TSSOP/QFP,
#: through-hole DIP). It is a DECLARED attribute (`PackageGeometry.family`),
#: never inferred from a part name. Excluded on purpose: SIP (the leads leave
#: the bottom edge of a standing body, under its footprint), QFN and modules
#: (bottom-terminated: their pads lie under the body by design) and
#: unknown/custom families, where the data cannot tell side leads from
#: bottom terminations.
SIDE_LEAD_FAMILIES = frozenset({"dip", "soic", "tssop", "qfp"})

#: Doubt codes of `package_doubts`, stable identifiers for the GUI.
#: The body is too short along a lead row (the outer leads would leave
#: beyond its end): the signature of length and width written transposed.
DOUBT_BODY_PADS = "body_pad_mismatch"
#: The body across the rows reaches over the lead lands: J-leads (then it is
#: right) or the overall width E (lead tip to lead tip) read instead of the
#: body width E1. Swapping length and width is never the answer to this one.
DOUBT_BODY_OVER_LANDS = "body_over_lead_lands"
DOUBT_PIN_ORDER = "pin_order_clockwise"
DOUBT_VIEW_MIRROR = "bottom_view_mirrored"

#: Numerical tolerance of the structural checks, mm. It absorbs float noise
#: only: it is not a calibrated margin.
_DOUBT_EPS = 1e-6

#: DECLARED HEURISTIC: smallest lead width as a fraction of the SMD land width
#: along the row. A lead leaves the body through its face, so the outer lead
#: of a row must lie within the body end, lead width included; the drawing
#: gives the land, not the lead. IPC-7351 lands are the maximum lead width
#: plus a few hundredths of side fillet, and the narrowest JEDEC ratio of
#: minimum lead width to a usual land is about 0.42 (TSSOP: b 0.19 mm on a
#: 0.45 mm land), so 0.4 under-estimates every lead and keeps legitimate
#: parts clear. Margins, with the KiCad/IPC lands and the MINIMUM body length
#: of the datasheets: TSSOP-16 (D 4.9 min) clears by 0.085 mm, QSOP-16
#: (D 4.8 min) by 0.098 mm, every SOIC-8/14/16, TSSOP-8/14/20, SSOP, LQFP
#: and PLCC by more; the audit's transposed SOIC-8 (body 3.9 along a 3.81 mm
#: row of 0.6 mm lands) is caught by 0.075 mm. Both margins are below
#: 0.1 mm: the separation is real but narrow, which is why the result is a
#: doubt and never a correction. A declared lead width in the schema would
#: make the fraction unnecessary.
_LEAD_WIDTH_FRACTION_OF_LAND = 0.4

#: Lead shapes `PackageGeometry.lead_form` may declare (seen in a side view).
LEAD_FORMS = frozenset({"unknown", "gull_wing", "j_lead"})


@dataclass(frozen=True)
class GeometryDoubt:
    """@brief A structural inconsistency of a package geometry, to be confirmed by the user.

    A doubt never changes the geometry: it states what the data contradicts
    and, when one exists, which alternative reading the data supports, so the
    GUI can ask instead of guessing.
    """
    #: One of DOUBT_BODY_PADS, DOUBT_BODY_OVER_LANDS, DOUBT_PIN_ORDER, DOUBT_VIEW_MIRROR.
    code: str
    #: Localized explanation, starting with «DUBBIO» like the other doubts
    #: written in the extraction notes.
    message: str
    #: Pin numbers involved, in geometry order; empty when the doubt is global.
    pins: tuple = ()
    #: For DOUBT_BODY_PADS: True when swapping body length and width gives a
    #: body consistent with every lead (the two values are probably
    #: transposed and the swap may be proposed), False otherwise; None for
    #: the other codes.
    swap_clears: Optional[bool] = None

    def as_dict(self) -> dict:
        """@brief Return the doubt as a plain JSON-compatible dict.

        @return {"code", "message", "pins" (list), "swap_clears"}.
        """
        return {"code": self.code, "message": self.message, "pins": list(self.pins),
                "swap_clears": self.swap_clears}


def _extent(pin: PinSpec) -> tuple:
    """Half extents (x, y) of a pad's copper, rotation included, mm."""
    corners = pad_outline(0.0, 0.0, pin.pad_length_mm, pin.pad_width_mm, pin.rotation_deg)
    return (max(abs(c[0]) for c in corners), max(abs(c[1]) for c in corners))


def _lead_pads(pins: Sequence[PinSpec]) -> list:
    """Electrical pads lying on an outer row of the pad field, with the row direction.

    A pad belongs to the left/right row when its centre is at the minimum or
    maximum X of the field, to the top/bottom row when at the minimum or
    maximum Y, within half its smaller copper extent. A corner pad near both
    goes to the more populated row (a dual-row SOIC: the column of four beats
    the pair of end pads). On a tie (two pins per side: DIP-4, SOIC-4) the
    land shape decides, because a side lead's SMD land is elongated across
    its row; a round or square land leaves the direction unknown (None), and
    the pad still counts as a lead for the checks that need no direction.
    Pads on no outer row (an exposed thermal pad) are not leads.

    @return A list of (pin, axis) where axis is "y" for a left/right row
        (the row runs along Y), "x" for a top/bottom row, None when unknown.
    """
    pads = [pin for pin in pins if pin.pad_type != "np_thru_hole" and str(pin.number).strip()]
    if len(pads) < 2:
        return []
    xs = [float(pin.x_mm) for pin in pads]
    ys = [float(pin.y_mm) for pin in pads]
    tolerances = [min(_extent(pin)) for pin in pads]
    groups = {}
    for key, values, extreme, axis in (("left", xs, min(xs), "y"), ("right", xs, max(xs), "y"),
                                       ("top", ys, min(ys), "x"), ("bottom", ys, max(ys), "x")):
        groups[key] = (axis, {index for index in range(len(pads))
                              if abs(values[index] - extreme) <= tolerances[index]})
    # A field one column wide (or one row high) has no distinct left and right
    # rows: the same pads would be counted twice.
    if groups["left"][1] == groups["right"][1]:
        groups.pop("right")
    if groups["top"][1] == groups["bottom"][1]:
        groups.pop("bottom")
    result = []
    for index, pin in enumerate(pads):
        owners = sorted(((len(members), axis) for axis, members in groups.values()
                         if index in members), reverse=True)
        if not owners:
            continue
        axis = owners[0][1]
        if len(owners) > 1 and owners[0][0] == owners[1][0] and owners[0][1] != owners[1][1]:
            half_x, half_y = _extent(pin)
            axis = ("y" if half_x > half_y + _DOUBT_EPS else
                    "x" if half_y > half_x + _DOUBT_EPS else None)
        result.append((pin, axis))
    return result


def _body_pad_violations(geometry: "PackageGeometry", leads: list,
                         length: float, width: float) -> tuple:
    """Lead pads inconsistent with a body of `length` x `width`, in the source frame.

    Two independent physical rules for leads leaving the body through its sides:
    - FACE (along the row): the outer lead lies within the body end, its
      width included (estimated from the land, `_LEAD_WIDTH_FRACTION_OF_LAND`;
      through-hole: the lead centre, since the annular ring is far wider
      than the lead). Violated when the body is too short along the row.
    - OVER (across the rows): a through-hole lead must enter the board
      outside the body (its hole centre not under the body); an SMD land
      must reach beyond the body edge (a side lead always lands at least
      partly outside), and a gull-wing land centre is outside the body. A
      J-lead (`lead_form` "j_lead") lands under the body edge by design, so
      its covered centre is expected.

    @return (face, over): pin numbers violating each rule, geometry order.
    """
    centre_x, centre_y = geometry.body_frame_offset()
    half = {"x": float(length) / 2, "y": float(width) / 2}
    face, over = [], []
    for pin, axis in leads:
        offset = {"x": float(pin.x_mm) - centre_x, "y": float(pin.y_mm) - centre_y}
        extent = dict(zip(("x", "y"), _extent(pin)))
        centre_covered = all(abs(offset[a]) < half[a] - _DOUBT_EPS for a in ("x", "y"))
        if pin.pad_type == "smd":
            land_inside = all(abs(offset[a]) + extent[a] <= half[a] + _DOUBT_EPS
                              for a in ("x", "y"))
            if land_inside or (centre_covered and geometry.lead_form != "j_lead"):
                over.append(pin.number)
        elif centre_covered:
            over.append(pin.number)
        if axis is not None:
            reach = abs(offset[axis])
            if pin.pad_type == "smd":
                reach += _LEAD_WIDTH_FRACTION_OF_LAND * extent[axis]
            if reach > half[axis] + _DOUBT_EPS:
                face.append(pin.number)
    return face, over


def _numbering_area(leads: list) -> Optional[float]:
    """Signed shoelace area of the lead centres taken in pin-number order, source frame.

    @return The area (negative = counter-clockwise on screen, Y down), or
        None when a lead has a non-integer number, there are fewer than three
        leads, or they are collinear: the turning sense is then undefined.
    """
    numbered = []
    for pin, _axis in leads:
        text = str(pin.number).strip()
        # `isascii()` first: `str.isdigit()` is also true for «²» or «①»,
        # which `int()` refuses (review 2026-11-11, F9-3); such a number is
        # text, so the turning sense is undefined.
        if not (text.isascii() and text.isdigit()):
            return None
        numbered.append((int(text), float(pin.x_mm), float(pin.y_mm)))
    if len(numbered) < 3:
        return None
    numbered.sort()
    area = 0.0
    for index, (_n, x0, y0) in enumerate(numbered):
        _m, x1, y1 = numbered[(index + 1) % len(numbered)]
        area += x0 * y1 - x1 * y0
    return area / 2 if abs(area) > _DOUBT_EPS else None


def package_doubts(geometry: "PackageGeometry") -> list:
    """@brief Structural doubts of a package geometry, never corrected silently.

    Checks founded on declared attributes and measured geometry, never on a
    part name:
    - DOUBT_VIEW_MIRROR: `source_view` "bottom" together with `mirrored`
      true. The two reflections cancel (see `pins_seen_from_top`), so the
      footprint is drawn exactly as the drawing; a model that set mirrored
      only BECAUSE the drawing is a bottom view produces a mirrored
      footprint. The data alone cannot tell the two readings apart, so this
      is always a doubt, never a normalisation.
    - For SIDE_LEAD_FAMILIES with both body sizes known (rules in
      `_body_pad_violations`):
      DOUBT_BODY_PADS when the body is too short along a lead row.
      `swap_clears` is True only when the swapped body breaks NO rule: a
      transposition. The swap is only proposed.
      DOUBT_BODY_OVER_LANDS when the body across the rows reaches over the
      lead lands and the swap does not resolve everything: the overall width
      E read instead of E1, or J-leads not declared as such. Never answered
      by a swap: a swapped E would give a wrong body (review 2026-10-15, M4).
    - DOUBT_PIN_ORDER, for SIDE_LEAD_FAMILIES: seen from the top, the lead
      numbering of these families runs counter-clockwise (JEDEC convention).
      A clockwise numbering is the signature of a drawing read with Y up or
      reflected twice; reverse-pinout parts exist, so it is a doubt. Works
      with two pins on each side too (DIP-4, SOIC-4).

    @param geometry The package geometry, validated here.
    @return GeometryDoubt objects in the order above; empty when nothing is
        contradicted or the checks do not apply.
    @throws ValueError When the geometry is invalid (see `PackageGeometry.validate`).
    @note Limits, stated so they are not mistaken for guarantees: a body
        transposed so that NEITHER orientation breaks a rule (a TSSOP-8 whose
        length along the rows is shorter than its width) is not detected;
        with two round through-hole pins on each side (DIP-4) the row direction
        is unknown and the FACE rule is skipped; families outside
        SIDE_LEAD_FAMILIES are not checked for body/pad or numbering, because
        their terminals may lie under the body by design and their numbering
        has no single convention. The FACE rule rests on the declared
        heuristic `_LEAD_WIDTH_FRACTION_OF_LAND`, whose margins are below 0.1 mm.
    """
    geometry.validate()
    doubts = []
    if geometry.source_view == "bottom" and geometry.mirrored:
        doubts.append(GeometryDoubt(DOUBT_VIEW_MIRROR, tx(
            "DUBBIO vista: il disegno e' dichiarato vista dal basso e anche speculare "
            "(mirrored): le due specchiature si annullano e il footprint e' disegnato come "
            "il disegno. Se mirrored e' stato indicato solo perche' e' una vista dal basso, "
            "il footprint risulta specchiato. Da confermare sul disegno.")))
    if geometry.family not in SIDE_LEAD_FAMILIES:
        return doubts
    leads = _lead_pads(geometry.pins)
    if geometry.body_length_mm and geometry.body_width_mm and leads:
        length, width = float(geometry.body_length_mm), float(geometry.body_width_mm)
        face, over = _body_pad_violations(geometry, leads, length, width)
        swapped_face, swapped_over = _body_pad_violations(geometry, leads, width, length)
        transposed = bool(face) and not swapped_face and not swapped_over
        if face:
            pins = ", ".join(str(number) for number in face)
            if transposed:
                message = tx(
                    "DUBBIO corpo/pad ({family}): con body_length_mm={length:g} lungo X e "
                    "body_width_mm={width:g} lungo Y il corpo e' troppo corto lungo la fila "
                    "dei pin {pins}, che uscirebbero oltre la sua estremita'; scambiando le "
                    "due quote il corpo e' coerente con tutti i pin: lunghezza e larghezza "
                    "sembrano invertite. Da confermare sul disegno quotato.",
                    family=geometry.family, length=length, width=width, pins=pins)
            else:
                message = tx(
                    "DUBBIO corpo/pad ({family}): con body_length_mm={length:g} lungo X e "
                    "body_width_mm={width:g} lungo Y il corpo e' troppo corto lungo la fila "
                    "dei pin {pins}, che uscirebbero oltre la sua estremita', e scambiare le "
                    "due quote non basta. Da verificare sul disegno quotato.",
                    family=geometry.family, length=length, width=width, pins=pins)
            doubts.append(GeometryDoubt(DOUBT_BODY_PADS, message, tuple(face),
                                        swap_clears=transposed))
        if over and not transposed:
            pins = ", ".join(str(number) for number in over)
            if geometry.lead_form == "unknown" and all(
                    pin.pad_type == "smd" for pin, _axis in leads if pin.number in over):
                message = tx(
                    "DUBBIO corpo/piazzole ({family}): il corpo ({length:g} x {width:g} mm) "
                    "arriva sopra le piazzole dei pin {pins}. Se i terminali sono a J "
                    "(piegati sotto il bordo del corpo) e' corretto; altrimenti e' stata "
                    "probabilmente letta la larghezza totale E (da punta a punta dei "
                    "terminali) invece della larghezza del corpo E1. Da confermare sul "
                    "disegno quotato.",
                    family=geometry.family, length=length, width=width, pins=pins)
            else:
                message = tx(
                    "DUBBIO corpo/piazzole ({family}): il corpo ({length:g} x {width:g} mm) "
                    "copre le piazzole o i fori dei pin {pins}, da cui i terminali laterali "
                    "dovrebbero restare fuori: probabilmente e' stata letta la larghezza "
                    "totale E (da punta a punta dei terminali) invece della larghezza del "
                    "corpo E1. Da verificare sul disegno quotato.",
                    family=geometry.family, length=length, width=width, pins=pins)
            doubts.append(GeometryDoubt(DOUBT_BODY_OVER_LANDS, message, tuple(over)))
    area = _numbering_area(leads)
    if area is not None:
        # The check runs in the source frame: a rotation keeps the turning
        # sense, each reflection (bottom view, explicit mirror) inverts it.
        flips = int(geometry.source_view == "bottom") + int(geometry.mirrored)
        seen_from_top = -area if flips % 2 else area
        if seen_from_top > 0:  # positive shoelace with Y down = clockwise on screen
            doubts.append(GeometryDoubt(DOUBT_PIN_ORDER, tx(
                "DUBBIO ordine dei pin ({family}): visti dall'alto i pin sono numerati in "
                "senso orario, mentre in questa famiglia la numerazione standard e' "
                "antioraria. Il disegno potrebbe essere stato letto con l'asse Y verso "
                "l'alto o specchiato due volte; esistono pero' package a piedinatura "
                "inversa. Da confermare sul disegno.", family=geometry.family),
                tuple(pin.number for pin, _axis in leads)))
    return doubts
