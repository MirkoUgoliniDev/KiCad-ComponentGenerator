"""@brief Through-hole lead sections, the hole and pad ESTIMATED from them, and their provenance words.

A drawing of a through-hole part usually quotes its terminals (a round pin
of a given diameter, a flat blade of a given width and thickness), not the
holes of the board. When no hole is printed, the program derives hole and pad
from the terminal with the ONE rule of this module, so the same drawing gives
the same pads at every run, and every value obtained this way is marked per
pad (`sources`) instead of passing for a measure.

Data contract of one pin mapping (`package_geometry["pins"][i]`), all
lengths in mm:

    "lead":    {"shape": "round", "diameter_mm": d}
            or {"shape": "flat", "width_mm": w, "thickness_mm": t}
    "sources": {"drill_mm": s, "pad_size": s, "shape": s, "lead": s}
               with s in "read" | "derived" | "estimated"; any key may be missing.

Both are optional: a mapping without them (a project saved before
2026-11-10) behaves exactly as before, with no mark and no finding.

Everything here is pure geometry on declared sections: no part name, no
package family, no threshold tuned to a component.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional

from ..services.messages import tx

#: Clearance, in mm, between a lead and the hole it goes through when the
#: hole has to be estimated from the lead: the usual 0.2-0.3 mm of the
#: project rules for through-hole parts (middle of the range, declared).
#: The KiCad getting-started guide teaches «largest pin dimension + 0.2 mm»
#: (docs/reference/kicad9-getting-started-notes.md), the low end of that range.
LEAD_TO_HOLE_CLEARANCE_MM = 0.25
#: Copper, in mm, added to an estimated hole DIAMETER for an estimated pad:
#: a 0.4 mm ring per side. DECLARED HEURISTIC (review G8, 2026-11-03): no
#: standard is cited for it -- it is a common through-hole design-rule
#: choice, comfortably above the minimum ring of MIN_ANNULAR_RING_MM
#: and the usual fabrication allowance, chosen so an estimated pad stays
#: solderable. It is only ever applied to values already marked ESTIMATED
#: (a lifted drill, a pad that no longer clears it); a pad diameter read from
#: the drawing or the manufacturer's land pattern makes it superfluous.
HOLE_TO_PAD_ANNULUS_MM = 0.8
#: Smallest copper ring per side, in mm, around a plated hole: the ONE value
#: used by the extraction (a pad that must be lifted with its hole) and by
#: the footprint checks (`footprint_checks.check_annular_ring`).
#: Source, the only one verified: the KiCad «Getting Started» guide (KiCad
#: 9.0, chapter «Creating New Footprints», paraphrased in
#: docs/reference/kicad9-getting-started-notes.md) advises a ring of at least
#: 0.15 mm, preferably larger (0.3 mm in the guide). It is a teaching value,
#: NOT a KLC rule and NOT a fabrication limit: the KLC publishes no number
#: for the ring, and no IPC clause was checked for it (an earlier comment
#: named IPC-7251 without a verified clause: withdrawn). A board house's own
#: minimum ring would make it superfluous; until then a finding built on it
#: is a warning that names the guide, never an error.
MIN_ANNULAR_RING_MM = 0.15

#: Step, in mm, an estimated hole is rounded UP to. DECLARED HEURISTIC: metric
#: drill sets are commonly stocked in 0.05 mm increments (no standard is
#: cited for it), so an estimate such as 1.327 mm (a 1 x 0.4 mm blade) names
#: no tool a fabricator has; rounding UP never removes clearance and adds
#: less than one step (0.05 mm) to it. It applies only to holes this module
#: estimates, never to a hole read from the drawing. A drill table of the
#: chosen board house would make it superfluous.
ESTIMATED_DRILL_STEP_MM = 0.05

#: Lead section shapes of the data contract.
LEAD_SHAPES = ("round", "flat")
#: Dimension keys each lead shape requires, in mm; no other key is accepted.
LEAD_FIELDS = {"round": ("diameter_mm",), "flat": ("width_mm", "thickness_mm")}

#: Per-pad values whose provenance `sources` may state.
SOURCE_KEYS = ("drill_mm", "pad_size", "shape", "lead")
#: Provenance words: «read» is printed in the document (or typed by the
#: user), «derived» is computed by the program from read values (a hole from
#: a read lead), «estimated» is a guess with no printed basis.
SOURCE_WORDS = ("read", "derived", "estimated")
#: The words that are not a reading: the values carrying them must be
#: verified on the drawing or the manufacturer's land pattern.
UNVERIFIED_SOURCE_WORDS = ("derived", "estimated")

#: KiCad's internal unit, mm (1 nm): two lengths closer than half of it are
#: written as the same number, so a comparison absorbs that much float noise.
_KICAD_UNIT_MM = 1e-6


def _positive(value, name: str, number: str) -> float:
    """Return `value` as a finite float > 0, refusing booleans, strings and non-finite numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(tx("pin {number}: {name} deve essere un numero maggiore di zero",
                            number=number, name=name))
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(tx("pin {number}: {name} deve essere un numero maggiore di zero",
                            number=number, name=name)) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(tx("pin {number}: {name} deve essere un numero maggiore di zero",
                            number=number, name=name))
    return value


def normalised_lead(raw, number: str = "") -> Optional[dict]:
    """@brief Validate a declared lead section and return a clean copy.

    @param raw None, or a mapping {"shape": "round", "diameter_mm": d} or
        {"shape": "flat", "width_mm": w, "thickness_mm": t}, lengths in mm.
    @param number Pin number named in the error message.
    @return None for None (no lead declared); otherwise a new dict with the
        shape and exactly the dimension keys of that shape, as floats.
    @throws ValueError When `raw` is not a mapping, the shape is unknown, a
        required dimension is missing or is not a finite number > 0, or a key
        outside the contract is present (a typo such as "diametre_mm" must
        not be dropped in silence: the lead would lose its size).
    @note A flat lead keeps width and thickness as declared: no order between
        them is imposed, and `lead_passes` tries both orientations.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(tx("pin {number}: lead deve essere un oggetto", number=number))
    shape = raw.get("shape")
    if not isinstance(shape, str) or shape not in LEAD_SHAPES:
        raise ValueError(tx("pin {number}: forma del terminale non valida: {shape!r}",
                            number=number, shape=shape))
    fields = LEAD_FIELDS[shape]
    unknown = sorted(str(key) for key in raw if key != "shape" and key not in fields)
    if unknown:
        raise ValueError(tx("pin {number}: chiave non ammessa in {name}: {key}",
                            number=number, name="lead", key=", ".join(unknown)))
    clean = {"shape": shape}
    for key in fields:
        clean[key] = _positive(raw.get(key), f"lead.{key}", number)
    return clean


def normalised_sources(raw, number: str = "") -> Optional[dict]:
    """@brief Validate the per-pad provenance words and return a clean copy.

    @param raw None, or a mapping with any of the keys SOURCE_KEYS, each one
        of SOURCE_WORDS.
    @param number Pin number named in the error message.
    @return None for None or an empty mapping (nothing is stated: the same as
        a project saved before the provenance existed); otherwise a new dict
        holding the given keys in SOURCE_KEYS order.
    @throws ValueError When `raw` is not a mapping, a key is not in
        SOURCE_KEYS or a word is not in SOURCE_WORDS: an unknown word would
        otherwise read as «not estimated» and hide an estimate.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(tx("pin {number}: sources deve essere un oggetto", number=number))
    unknown = sorted(str(key) for key in raw if key not in SOURCE_KEYS)
    if unknown:
        raise ValueError(tx("pin {number}: chiave non ammessa in {name}: {key}",
                            number=number, name="sources", key=", ".join(unknown)))
    clean = {}
    for key in SOURCE_KEYS:
        if key not in raw:
            continue
        word = raw[key]
        if not isinstance(word, str) or word not in SOURCE_WORDS:
            raise ValueError(tx("pin {number}: provenienza non valida per {key}: {word!r}",
                                number=number, key=key, word=word))
        clean[key] = word
    return clean or None


def unverified_values(sources) -> tuple:
    """@brief The pad values a provenance mapping marks as derived or estimated.

    @param sources A validated `sources` mapping, or None.
    @return The keys among "drill_mm", "pad_size" and "shape" (in that
        order) whose word is in UNVERIFIED_SOURCE_WORDS; empty for None. The
        "lead" key is not a pad value: an estimated lead is not listed here.
    """
    if not sources:
        return ()
    return tuple(key for key in ("drill_mm", "pad_size", "shape")
                 if sources.get(key) in UNVERIFIED_SOURCE_WORDS)


def lead_circumscribed_diameter(lead: dict) -> float:
    """@brief Diameter, mm, of the smallest circle a lead section fits in.

    @param lead A lead mapping of the data contract (validated here).
    @return The diameter of a round lead; the diagonal sqrt(w^2 + t^2) of a
        flat one: a rectangular section turns freely in a round hole only
        when the hole clears its diagonal, not its width.
    @throws ValueError When the lead is None or out of contract.
    """
    clean = normalised_lead(lead)
    if clean is None:
        raise ValueError(tx("pin {number}: lead deve essere un oggetto", number=""))
    if clean["shape"] == "round":
        return clean["diameter_mm"]
    return math.hypot(clean["width_mm"], clean["thickness_mm"])


def estimated_hole(lead: dict) -> float:
    """@brief Round hole diameter, mm, ESTIMATED for a lead whose hole the drawing does not print.

    @param lead A lead mapping of the data contract.
    @return The circumscribed diameter plus LEAD_TO_HOLE_CLEARANCE_MM,
        rounded UP to ESTIMATED_DRILL_STEP_MM (a value already on the step
        within one KiCad unit is kept, not lifted by float noise).
    @throws ValueError When the lead is out of contract.
    @note An estimate: the caller marks the pad's "drill_mm" source as
        «derived» (the lead was read) or «estimated» (the lead was not).
    """
    raw = lead_circumscribed_diameter(lead) + LEAD_TO_HOLE_CLEARANCE_MM
    steps = math.ceil((raw - _KICAD_UNIT_MM / 2) / ESTIMATED_DRILL_STEP_MM)
    return round(steps * ESTIMATED_DRILL_STEP_MM, 6)


def estimated_pad_diameter(hole_mm: float) -> float:
    """@brief Round pad diameter, mm, ESTIMATED for a hole when the drawing prints no land.

    @param hole_mm Hole diameter, mm (finite, > 0).
    @return `hole_mm` plus HOLE_TO_PAD_ANNULUS_MM (a declared heuristic: a
        0.4 mm ring per side), rounded to KiCad's 1 nm resolution.
    @throws ValueError When the hole is not a finite number > 0.
    """
    return round(_positive(hole_mm, "drill_mm", "") + HOLE_TO_PAD_ANNULUS_MM, 6)


class EstimatedPad(NamedTuple):
    """@brief The round hole and round pad estimated for one lead, mm."""
    #: Round hole diameter, mm (`estimated_hole`).
    drill_mm: float
    #: Round pad diameter, mm (`estimated_pad_diameter`).
    pad_diameter_mm: float


def estimated_round_pad(lead: dict) -> EstimatedPad:
    """@brief The round hole and the round pad ESTIMATED for a lead, by the one rule of every run.

    @param lead A lead mapping of the data contract.
    @return EstimatedPad(drill_mm, pad_diameter_mm): `estimated_hole(lead)`
        and `estimated_pad_diameter` of that hole. The pad is a circle of
        that diameter (`pad_length_mm` = `pad_width_mm` = pad_diameter_mm).
    @throws ValueError When the lead is out of contract.
    @note The pad is round also for a flat blade: a round hole on its
        diagonal is the one hole every fabricator drills, while a slot is a
        choice (cost, minimum slot width) the data cannot make. A slot or a
        land printed in the drawing replaces the estimate.
    """
    hole = estimated_hole(lead)
    return EstimatedPad(hole, estimated_pad_diameter(hole))


def _rectangle_fits_stadium(along: float, across: float, length: float, width: float) -> bool:
    """Whether a centred `along` x `across` rectangle lies strictly inside a stadium `length` x `width`.

    The stadium (a slot) has straight sides `width` apart and round ends of
    radius width / 2; `along` is the rectangle side on the slot's long axis.
    """
    tolerance = _KICAD_UNIT_MM / 2
    if across >= width - tolerance or along >= length - tolerance:
        return False
    # The rectangle's corner must be inside the round end: within the
    # straight part it only needs to be between the two sides.
    reach = along / 2 - (length - width) / 2
    if reach <= 0:
        return True
    return math.hypot(reach, across / 2) < width / 2 - tolerance / 2


def lead_passes(lead: dict, drill_mm: float, drill_length_mm: Optional[float] = None) -> bool:
    """@brief Whether a lead section can pass through a hole: a structural fact, not a threshold.

    @param lead A lead mapping of the data contract.
    @param drill_mm Hole diameter, mm; for a slot, its side along the pad's
        LENGTH axis (`PinSpec.pad_length_mm`, `Pad.size[0]`, `Pad.drill_slot[0]`).
    @param drill_length_mm Second side of a slot, mm, along the pad's WIDTH
        axis (`PinSpec.pad_width_mm`, `Pad.size[1]`, `Pad.drill_slot[1]`):
        despite its name it does NOT run along the pad length, the
        convention of `PinSpec.drill_length_mm`. None, or equal to
        `drill_mm`, is a round hole.
    @return Round hole: True when the hole is larger than the lead's
        circumscribed diameter (a hole equal to the lead does not let it
        through). Slot (a stadium: two straight sides and two round ends): a
        round lead passes when its diameter is below the slot's SHORTER
        side; a flat lead when its rectangle lies inside the stadium with
        its width along either slot axis -- so it is smaller than the slot
        on each of the slot's own axes, and its corners clear the round ends.
        Equality counts as not passing, within half a KiCad unit (0.5 nm).
    @throws ValueError When the lead is out of contract or a hole side is
        not a finite number > 0.
    @note The contract does not say how a flat lead is turned relative to
        the pad, so both orientations are tried and the hole is refused only
        when NEITHER fits: a blade drawn across its slot is not detected. A
        declared lead angle in the schema would close this.
    @note No clearance is asked here: «passes» means «is not impossible».
        Whether the play is comfortable is LEAD_TO_HOLE_CLEARANCE_MM's
        business, applied only to estimates.
    """
    clean = normalised_lead(lead)
    if clean is None:
        raise ValueError(tx("pin {number}: lead deve essere un oggetto", number=""))
    first = _positive(drill_mm, "drill_mm", "")
    second = first if drill_length_mm is None else _positive(drill_length_mm, "drill_length_mm", "")
    tolerance = _KICAD_UNIT_MM / 2
    if clean["shape"] == "round":
        return clean["diameter_mm"] < min(first, second) - tolerance
    width, thickness = clean["width_mm"], clean["thickness_mm"]
    # The stadium's long axis is its larger side, whichever pad axis it is on.
    long_side, short_side = max(first, second), min(first, second)
    return (_rectangle_fits_stadium(width, thickness, long_side, short_side)
            or _rectangle_fits_stadium(thickness, width, long_side, short_side))
