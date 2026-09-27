"""@brief Estimate the body heights a top photograph cannot measure, and say they are estimates.

@note A photograph taken from above gives the plan of a body, never its height.
Every value produced here is therefore an ESTIMATE: callers must keep
`_height_estimated` True and a `_height_source` ending in `_not_measured`, so
the log and the review can show the height as «stimata» and a person can
correct it.
@note Two kinds of rule, both declared heuristics (typical values from common
package families, not measurements):
- a family prior, for families whose thickness does not grow with the plan
  (chip passives, molded ICs, connectors...);
- a plan-scaled rule, for bodies whose height is of the order of their plan
  size: a cylindrical can seen end-on (electrolytic capacitor, buzzer) or a
  roughly cubic housing (relay). Using the fixed prior of the family there
  turned a 10 mm electrolytic can into a 0.8 mm disc.
What would make them unnecessary: a side view with dimensions, or a height
declared by the user or by a datasheet.
"""
from __future__ import annotations

import math

#: Typical height, in mm, of the most common package of each family whose
#: thickness does not follow its plan. The first fourteen are the priors used
#: by the photographic pipeline so far (photo_grounding.mechanical_envelopes);
#: the others cover the kinds added to datasheet_extractor.COMPONENT_KINDS on
#: 2026-09-26. Each is the height of the family's most common member, so it
#: over- or under-estimates the others; it is never a measurement:
#: ic_sot SOT-23/SC-70 (0.9-1.1, SOT-89 1.5); ic_qfp LQFP (1.4-1.6);
#: ic_bga 0.8-1.7; shield_can RF shield lids 1.5-3; pin_header / socket_header
#: the common 2.54 mm family (8.5 above the board; finer pitches are lower,
#: so this over-estimates them -- visible and correctable in the review);
#: crystal_can HC-49/S (3.5; the tall HC-49/U reaches 13.5); display a glass
#: panel on its carrier (~3); ic an unrecognised molded package; fuse a chip
#: fuse (0.6-1.0).
FAMILY_HEIGHT_MM = {
    "ic_soic": 1.75, "ic_qfn": 1.0, "module": 4.0, "transformer": 4.0,
    "led": 1.0, "capacitor_tantalum": 1.5, "capacitor": .8, "resistor": .6,
    "inductor": 1.5, "diode": 1.2, "diode_melf": 1.5, "connector": 3.0,
    "switch": 1.7, "crystal": 1.5,
    "ic": 1.5, "ic_sot": 1.1, "ic_qfp": 1.6, "ic_bga": 1.2, "shield_can": 2.0,
    "pin_header": 8.5, "socket_header": 8.5, "crystal_can": 3.5, "display": 3.0,
    "fuse": .8,
}

#: Families whose height grows with the plan, as the ratio height / plan
#: short side (typical values, declared estimates):
#: capacitor_electrolytic a can seen end-on, 0.85-2 diameters tall (SMD
#: 6.3x5.4 ... 10x10.2, radial 5x11): 1.0 is a LOW estimate; relay a closed
#: block roughly as tall as wide; buzzer 9-12 mm transducers 0.45-0.8 of the
#: diameter; inductor_power shielded power inductors (4x4x2 ... 12x12x6),
#: "height comparable to the side"; power_tab DPAK 6.1 -> 2.3, D2PAK 9 -> 4.4;
#: potentiometer trimmers (3 mm -> 1.6, 6 mm -> 4.5); heatsink board-mounted
#: finned or pin heatsinks (stick-on 14x14x6 ... 20x20x10, clip-on TO-220
#: types taller than their short side): 0.5 is a LOW estimate of a family
#: whose height varies widely, added 2026-09-27 so a heatsink no longer falls
#: to the 1 mm placeholder. The names cover the detector classes (photo_yolo)
#: too; a name no producer emits is harmless.
PLAN_SCALED_RATIO = {
    "capacitor_electrolytic": 1.0, "electrolytic_capacitor": 1.0,
    "relay": 1.0, "buzzer": .75, "inductor_power": .5, "power_tab": .4,
    "potentiometer": .6, "heatsink": .5,
}

#: Batteries: an elongated plan is a cylinder lying on the board (as tall as
#: it is wide); a round or square plan is a coin cell in its holder, about a
#: quarter to a third of its diameter tall. The split is the plan's shape,
#: not its size: a CR2032 holder (~22 mm) and an 18650 holder (~77 x 21 mm)
#: differ in aspect, not in short side.
BATTERY_CYLINDER_MIN_ASPECT = 2.0
BATTERY_COIN_RATIO = .3

#: Detector classes that name the same family under another word. A detector
#: "transistor" is taken as the smallest common transistor package (SOT); a
#: TO-220 would need the vision model's kind (power_tab).
_ALIASES = {"clock": "crystal", "button": "switch", "transistor": "ic_sot"}

#: Short side, in mm, above which a square-ish "capacitor" is read as a can
#: seen end-on: the smallest common aluminium electrolytic can is 4 mm across.
#: DECLARED FRAGILE HEURISTIC: the family "capacitor" covers both chips and
#: cans, and the plan is the only thing a top photograph offers. Chip
#: capacitors are rectangular and, up to case 1812 (4.5 x 3.2 mm), narrower
#: than 4 mm; the large 2220/2225 chips (5.7 x 5.0 / 5.7 x 6.3 mm) and big
#: film boxes WOULD be misread as cans. Superseded as soon as the vision model
#: gives the specific kind (an electrolytic kind in PLAN_SCALED_RATIO) or a
#: height of its own.
CAN_MIN_DIAMETER_MM = 4.0
#: Minimum short/long ratio of a can's plan: a can is round (1.0) and its SMD
#: base plate square; a chip is at least 1.25 : 1 up to case 1210.
CAN_MIN_ASPECT = 0.8

#: Height sources written by estimate_body_height, all ending in _not_measured.
FAMILY_SOURCE = "family_prior_not_measured"
PLAN_SOURCE = "plan_shape_estimate_not_measured"


def _positive(value):
    """Finite positive float, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def estimate_body_height(kind, length_mm=None, width_mm=None):
    """@brief Estimate the height of a body from its family and its plan.

    @param kind Component kind or detector class (e.g. "capacitor", "relay").
    @param length_mm One plan dimension in mm (either side), or None.
    @param width_mm The other plan dimension in mm, or None.
    @return (height_mm, source) where source is PLAN_SOURCE or FAMILY_SOURCE,
        or None when the family is unknown: the caller then keeps its own
        declared placeholder.
    @note Never a measurement: see the module notes. The plan-scaled rule
        needs both plan dimensions; without them a plan-scaled family falls
        back to None, not to a guess.
    """
    name = _ALIASES.get(str(kind or "").strip().casefold(), str(kind or "").strip().casefold())
    sides = [side for side in (_positive(length_mm), _positive(width_mm)) if side is not None]
    short = min(sides) if len(sides) == 2 else None
    ratio = PLAN_SCALED_RATIO.get(name)
    if name == "battery":
        if short is None:
            return None
        cylinder = max(sides) / short >= BATTERY_CYLINDER_MIN_ASPECT
        return round(short * (1.0 if cylinder else BATTERY_COIN_RATIO), 2), PLAN_SOURCE
    if ratio is not None:
        return (round(short * ratio, 2), PLAN_SOURCE) if short is not None else None
    if (name == "capacitor" and short is not None and short >= CAN_MIN_DIAMETER_MM
            and short / max(sides) >= CAN_MIN_ASPECT):
        return round(short, 2), PLAN_SOURCE
    if name in FAMILY_HEIGHT_MM:
        return FAMILY_HEIGHT_MM[name], FAMILY_SOURCE
    return None
