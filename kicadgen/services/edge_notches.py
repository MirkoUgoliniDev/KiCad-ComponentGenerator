"""@brief Count the half-hole notches of a castellated module on its calibrated top photo.

The number of contacts per side comes from the AI reading the documents, and
an answer can be consistent with itself (as many names as contacts, a row that
fits its edge) and still be wrong: on the RS485 sample the model once counted
an LED label as a fifth contact (HANDOFF points 132-133). The photo can check
it without any paid call: a half-hole is a plated hole cut by the board edge,
so on a top view it is a notch where the BACKGROUND shows through the board
outline. Counting notches is a structural measure of the outline, not a
colour rule for gold or silver plating, and holds for any module.

User decision of 2026-09-28: a disagreement is only reported (log line and
analysis warning); nothing is changed and no AI call is made.

Limits, declared: only open half-holes are countable (a full hole inside the
board does not touch the outline); a background indistinguishable from the
board, or a photo too coarse to resolve a notch, gives «not countable», never
a guessed number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

try:
    from ..cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX
    from ..core.validation import positive_number
    from .messages import tx
except ImportError:  # loaded as a stand-alone file
    from kicadgen.cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX
    from kicadgen.core.validation import positive_number
    from kicadgen.services.messages import tx

#: Board edges, in the calibrated photo frame (X right, Y down, seen from above).
EDGES = ("left", "right", "top", "bottom")
#: Italian names of the edges, translated through the catalogue.
_EDGE_NAMES = {"left": "sinistro", "right": "destro", "top": "superiore", "bottom": "inferiore"}
#: How deep into the board the background is followed, in mm. Castellated
#: half-holes are at most a few millimetres wide, so their notch is at most
#: about half of that deep; deeper background is not a notch.
MAX_NOTCH_DEPTH_MM = 1.5
#: Narrowest notch counted, in mm: the smallest plated half-holes in use are
#: about 0.4-0.5 mm across. Also never below MIN_NOTCH_WIDTH_PX.
MIN_NOTCH_WIDTH_MM = 0.2
#: Narrowest notch counted, in pixels: fewer pixels are noise on any photo.
MIN_NOTCH_WIDTH_PX = 3
#: Shallowest notch counted, in mm. HEURISTIC, declared: a half-hole is a
#: plated hole cut by the edge, so its notch is about as deep as the hole
#: radius (less when the hole centre sits inside the board); the smallest
#: half-holes in use are about 0.4-0.5 mm across. Checked on two sample boards
#: (HANDOFF point 133): real notches of the RS485 module 0.30-0.43 mm deep,
#: false ones on the ESP32 dev kit -- full holes whose pads reach the edge, a
#: straight outline -- at most 0.15 mm. A margin of about 2x on two boards
#: only: a board with shallower half-holes would read «not countable».
MIN_NOTCH_DEPTH_MM = 0.2
#: Gaps inside one notch (plating reflections, JPEG noise) shorter than this
#: fraction of the pitch are closed. Two neighbouring half-holes are separated
#: by pitch - diameter, which stays above it while the diameter is below
#: 0.75 x pitch, the usual range for castellated modules.
MERGE_GAP_PITCH_FRACTION = 0.25
#: Tolerance on the spacing of notch centres, as a fraction of the pitch: a
#: spacing is regular when it is an integer multiple of the pitch within it.
PITCH_TOLERANCE = 0.25


@dataclass
class EdgeCount:
    """@brief What the photo says about the contacts of one edge.

    @note `found` is None when the edge is not countable; `reason` then says why.
    """

    edge: str
    found: Optional[int]
    declared: Optional[int] = None
    centres_mm: list = field(default_factory=list)
    reason: str = ""
    #: True when the outline shows no notch at all (a straight edge), as
    #: opposed to notches that cannot be counted.
    no_notch: bool = False


def depth_profiles(background_mask, box, max_depth_px: int) -> dict:
    """@brief Depth of background contiguous to each edge of a board box, per position.

    @param background_mask 2-D boolean array: True where the pixel matches the
        photo background (photo_refine.estimate_background tolerance).
    @param box (x0, y0, x1, y1) of the board crop in that array, x1/y1 excluded.
    @param max_depth_px Deepest background followed from the edge, in pixels.
    @return {edge: list of int}: for left/right one value per row of the box,
        for top/bottom one per column; each value is how many pixels of
        background touch that edge before the board starts, capped.
    """
    import numpy as np
    x0, y0, x1, y1 = (int(value) for value in box)
    depth = max(1, min(int(max_depth_px), x1 - x0, y1 - y0))

    def first_board(band):
        # Pixels of background before the first board pixel along axis 1.
        return np.where(band.all(axis=1), band.shape[1], band.argmin(axis=1)).astype(int).tolist()

    mask = np.asarray(background_mask, dtype=bool)
    return {
        "left": first_board(mask[y0:y1, x0:x0 + depth]),
        "right": first_board(mask[y0:y1, x1 - depth:x1][:, ::-1]),
        "top": first_board(mask[y0:y0 + depth, x0:x1].T),
        "bottom": first_board(mask[y1 - depth:y1, x0:x1][::-1].T),
    }


def _runs(flags):
    """Half-open [start, stop) index ranges where `flags` is true."""
    runs, start = [], None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


def count_notches(profile: Sequence[int], px_per_mm: float,
                  pitch_mm: Optional[float] = None) -> EdgeCount:
    """@brief Count the notches of one edge profile.

    @param profile Background depth per position along the edge (depth_profiles).
    @param px_per_mm Scale of the photo along this edge.
    @param pitch_mm Declared contact pitch of the edge, or None when the edge
        declares no contacts (then the notches must be evenly spaced among
        themselves).
    @return EdgeCount with `edge` left empty: `found` and the notch centres in
        mm from the start of the edge, or `found` None and the reason.
    @note Rules (see the module constants): depth above the edge's baseline
        (median) by at least EDGE_UNCERTAINTY_PX, and somewhere by at least
        MIN_NOTCH_DEPTH_MM; no wider than the pitch; gaps shorter than
        MERGE_GAP_PITCH_FRACTION x pitch closed; runs touching either end of
        the edge dropped, because a rounded board corner is a notch too; runs
        narrower than max(MIN_NOTCH_WIDTH_PX, MIN_NOTCH_WIDTH_MM) dropped.
    """
    scale = positive_number(px_per_mm)
    values = [int(value) for value in profile]
    if scale is None or len(values) < 3:
        return EdgeCount("", None, reason=tx("foto non calibrata"))
    ordered = sorted(values)
    baseline = ordered[len(ordered) // 2]
    flags = [value - baseline >= EDGE_UNCERTAINTY_PX for value in values]
    pitch = positive_number(pitch_mm)
    if pitch is not None:
        max_gap = MERGE_GAP_PITCH_FRACTION * pitch * scale
        runs = _runs(flags)
        merged = runs[:1]
        for start, stop in runs[1:]:
            if start - merged[-1][1] < max_gap:
                merged[-1] = (merged[-1][0], stop)
            else:
                merged.append((start, stop))
        runs = merged
    else:
        runs = _runs(flags)
    min_width = max(MIN_NOTCH_WIDTH_PX, MIN_NOTCH_WIDTH_MM * scale)
    # A notch is one half-hole: never wider than the pitch between two of
    # them (a longer run is the outline drifting from the crop edge).
    max_width = pitch * scale if pitch is not None else None
    min_depth = MIN_NOTCH_DEPTH_MM * scale
    notches = [(start, stop) for start, stop in runs
               if start > 0 and stop < len(values) and stop - start >= min_width
               and (max_width is None or stop - start <= max_width)
               and max(values[start:stop]) - baseline >= min_depth]
    centres = [round((start + stop) / 2 / scale, 3) for start, stop in notches]
    if not centres:
        return EdgeCount("", None, reason=tx("nessuna tacca visibile sul bordo"), no_notch=True)
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    unit = pitch if pitch is not None else (sorted(gaps)[0] if gaps else None)
    if unit is not None:
        for gap in gaps:
            multiple = round(gap / unit)
            if multiple < 1 or abs(gap - multiple * unit) > PITCH_TOLERANCE * unit:
                return EdgeCount("", None, centres_mm=centres,
                                 reason=tx("tacche a distanze non compatibili con il passo"))
    return EdgeCount("", len(centres), centres_mm=centres)


def _edge_name(edge: str) -> str:
    return tx(_EDGE_NAMES.get(edge, edge))


def _scales(crop_shape, length_mm, width_mm) -> dict:
    """px/mm along each edge of the calibrated crop (left/right run along Y)."""
    height_px, width_px = crop_shape[:2]
    return {"left": height_px / width_mm, "right": height_px / width_mm,
            "top": width_px / length_mm, "bottom": width_px / length_mm}


def count_disagreements(profiles, crop_shape, length_mm: float, width_mm: float,
                        sides) -> list[EdgeCount]:
    """@brief Declared sides whose half-holes the photo counts differently.

    @param profiles depth_profiles of the calibrated crop, or None.
    @param crop_shape Shape of the calibrated crop, in px.
    @param length_mm Board length along X, in mm.
    @param width_mm Board width along Y, in mm.
    @param sides Declared castellated sides, seen from above, in mm.
    @return One EdgeCount per disagreeing side (`found`, `declared`, notch
        centres in mm from the start of the edge: left to right for top and
        bottom, top to bottom for left and right). Sides declared with full
        holes, or not countable on the photo, never disagree.
    @note Used to ask the model to recount those sides (user decision
        2026-09-28, HANDOFF point 137); the comparison is the same as the
        warning of `check_declared_contacts`.
    """
    if not profiles or not isinstance(sides, (list, tuple)):
        return []
    scales = _scales(crop_shape, length_mm, width_mm)
    result = []
    for side in sides:
        if not isinstance(side, dict) or side.get("edge") not in EDGES:
            continue
        if side.get("contact_style") == "through_hole" or side["edge"] not in profiles:
            continue
        counted = count_notches(profiles[side["edge"]], scales[side["edge"]], side.get("pitch_mm"))
        if counted.found is not None and counted.found != side.get("count"):
            result.append(EdgeCount(side["edge"], counted.found, side.get("count"),
                                    counted.centres_mm))
    return result


def check_declared_contacts(profiles, crop_shape, length_mm: float, width_mm: float,
                            sides) -> list[tuple[str, str]]:
    """@brief Compare the declared contacts per edge with the notches on the photo.

    @param profiles depth_profiles of the calibrated crop, or None.
    @param crop_shape Shape (height, width, ...) of the calibrated crop, in px.
    @param length_mm Board length along X, in mm.
    @param width_mm Board width along Y, in mm.
    @param sides Declared castellated sides (dicts with edge, count, pitch_mm,
        contact_style), in mm and seen from above as the photo is.
    @return (kind, message) lines, kind "warning" for a disagreement the user
        must check (another count, a side the analysis left out, declared
        half-holes on an edge with no notch at all), "log" for a confirmation or a side that cannot be counted.
        Empty when there is nothing to compare.
    @note Only reports (user decision 2026-09-28): the declared contacts are
        never changed here.
    """
    if not profiles or not isinstance(sides, (list, tuple)):
        return []
    scales = _scales(crop_shape, length_mm, width_mm)
    declared = {}
    for side in sides:
        if isinstance(side, dict) and side.get("edge") in EDGES:
            declared[side["edge"]] = side
    lines = []
    for edge in EDGES:
        side = declared.get(edge)
        profile = profiles.get(edge)
        if profile is None:
            continue
        if side is None:
            result = count_notches(profile, scales[edge], None)
            if result.found is not None and result.found >= 2:
                lines.append(("warning", tx(
                    "Lato {edge}: sulla foto si contano {found} semi-fori, ma l'analisi non "
                    "dichiara contatti su questo lato: da verificare.",
                    edge=_edge_name(edge), found=result.found)))
            continue
        count = side.get("count")
        if side.get("contact_style") == "through_hole":
            lines.append(("log", tx(
                "Lato {edge}: contatti a foro intero, non contabili dalla sagoma della foto.",
                edge=_edge_name(edge))))
            continue
        result = count_notches(profile, scales[edge], side.get("pitch_mm"))
        if result.no_notch and side.get("contact_style") == "half_hole":
            # Declared half-holes cut the outline; a straight edge contradicts
            # the side (user decision 2026-09-28: a warning, nothing changed).
            # Limit: half-holes shallower than MIN_NOTCH_DEPTH_MM read as none.
            lines.append(("warning", tx(
                "Lato {edge}: l'analisi dichiara {declared} contatti a semi-foro, ma sulla foto "
                "il bordo non ha nessuna tacca: lato da verificare.",
                edge=_edge_name(edge), declared=count)))
        elif result.found is None:
            lines.append(("log", tx("Lato {edge}: contatti non contabili sulla foto ({reason}).",
                                    edge=_edge_name(edge), reason=result.reason)))
        elif result.found == count:
            lines.append(("log", tx(
                "Lato {edge}: sulla foto si contano {found} semi-fori, come i contatti "
                "dichiarati.", edge=_edge_name(edge), found=result.found)))
        else:
            lines.append(("warning", tx(
                "Lato {edge}: sulla foto si contano {found} semi-fori, l'analisi dichiara "
                "{declared} contatti: numero di contatti da verificare.",
                edge=_edge_name(edge), found=result.found, declared=count)))
    return lines
