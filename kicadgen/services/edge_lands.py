"""@brief Measure the copper lands of castellated contacts on the calibrated photo.

The land pattern of a castellated module is often not in the documents, and
the model's estimate of it changes from run to run (HANDOFF point 169: the
same prompt gave a 2.54 mm pad width on a 2.54 mm pitch, then another value).
The calibrated top photo, however, shows every land: a patch of copper of its
own colour around the half-hole the outline count already located
(edge_notches). This module measures that patch, contact by contact, and
gives per side the land width ALONG the edge and its extent INWARD from the
edge, in millimetres with the uncertainty of the photo scale.

Every decision is taken on quantities measured on the photo itself:

* the half-hole is the notch of the outline profile, excluded by geometry;
* the solder mask colour is sampled LOCALLY, in the gap between the contact
  and its neighbours (the columns half a pitch away, just inside the edge):
  by construction no land lies there. A global «board colour» fails on a
  crowded board, where dark bodies can outnumber the visible mask;
* «not mask» is the Otsu split of the window's colour distances from that
  colour: the separation the window itself shows, no fixed Delta E;
* silkscreen strokes and traces are thinner than a land: a morphological
  opening of one annular ring (MIN_HOLE_WALL_MM, the project's declared
  fabrication minimum) removes them, and a closing of the same size heals
  the shading a metal surface shows under a lamp;
* rows in which «not mask» runs past half a pitch from the contact centre
  hold no land (no land reaches its neighbour's): they are the bright line
  of the board's cut edge, or copper that really is continuous;
* the land is the patch connected to the ring around the hole; its width is
  the upper quartile of the extents of its rows that are at least half its
  widest row (LAND_ROW_RATIO), and its inward extent the last such row.

What cannot be measured is said (`LandMeasure.reason`) and never invented:
copper running into the neighbouring contact, a side without a confirmed
row of notches, no copper around the hole. Declared limit: a body that
touches a land inward is not mask either and lengthens the inward extent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Optional, Sequence

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None
    np = None

from ..cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX
from ..generators.castellated_module import MIN_HOLE_WALL_MM
from .messages import tx
from ..core.validation import finite_number
from .photo_refine import (MAX_BOARD_EDGE_DRIFT_PX, MAX_BOARD_EDGE_DRIFT_RATIO, _lab,
                           _otsu_level)

#: Rows of the land patch narrower than this fraction of its widest row are
#: not the land (a trace wider than the opening, the tail of a shadow): the
#: land is measured on the others, and ends at the last of them. Declared
#: structural ratio (a track is a fraction of a pad), not a size.
LAND_ROW_RATIO = 0.5

#: Quantile of the row extents of the land that gives its width: the upper
#: quartile, i.e. the widest rows short of the single widest one, so a row
#: that lost pixels to the hole's shadow does not shrink the land and one
#: stray protrusion does not widen it.
LAND_WIDTH_QUANTILE = 75

# Pixels of uncertainty on each measured boundary of a land: the SAME
# quantity as the outline uncertainty of the notches, the inventory boxes and
# the refined contours, so it is imported from its single source
# (cad3d.measurement_uncertainty.EDGE_UNCERTAINTY_PX, review 2026-10-27 F4)
# instead of a second value of its own. Every tolerance below is expressed
# in multiples of it, never as a literal pixel count.

#: Half width of the search window of one contact, in pitches: wider than
#: half a pitch so a land that nearly fills the pitch is seen whole, with
#: the neighbours' lands as separate patches around it. Copper that reaches
#: past half a pitch from the contact centre belongs to two contacts.
WINDOW_HALF_PITCHES = 0.75

#: Depth of the mask sample in the gap, in annular rings from the edge: from
#: one ring in (past the bright line of the cut edge) to four.
GAP_SAMPLE_RINGS = (1, 4)

EDGES = ("left", "right", "top", "bottom")


@dataclass
class LandMeasure:
    """@brief What the photo says about the lands of one edge.

    @note `width_mm` is the land extent ALONG the edge (the footprint's
        `pad_width_mm` on every edge, see castellated_module.pad_positions);
        `inward_mm` its extent from the edge INTO the board (the part of the
        footprint's `pad_length_mm` that lies on the module, without the
        overhang). Either is None when not measurable; `reason` says why.
    @note `contacts` keeps the per-contact values (width_mm, inward_mm or
        None) that entered the side value, `rejected` the reasons of the
        contacts left out.
    """

    edge: str
    width_mm: Optional[float]
    inward_mm: Optional[float]
    tolerance_mm: float
    contacts: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        """@brief JSON-friendly copy for the extraction answer (`_measured_lands`).

        @return A plain dict with the edge, the land width and inward extent in
            mm, the tolerance in mm, the contact pairs as lists, the rejected
            entries and the reason text.
        """
        return dict(edge=self.edge, width_mm=self.width_mm, inward_mm=self.inward_mm,
                    tolerance_mm=self.tolerance_mm,
                    contacts=[list(pair) for pair in self.contacts], rejected=list(self.rejected),
                    reason=self.reason)


def _mad_sigma(values) -> float:
    """MAD scaled to a standard deviation (1.4826), 0 for fewer than two values."""
    values = np.asarray(values, dtype=np.float64)
    if values.size < 2:
        return 0.0
    return 1.4826 * float(np.median(np.abs(values - np.median(values))))


def _edge_window(crop, edge, a0, a1, depth):
    """Sub-image of `crop` with axis 0 = depth from `edge` (row 0 on the edge), axis 1 = along."""
    height, width = crop.shape[:2]
    if edge == "top":
        return crop[0:depth, a0:a1]
    if edge == "bottom":
        return crop[height - depth:height, a0:a1][::-1]
    if edge == "left":
        return np.transpose(crop[a0:a1, 0:depth], (1, 0, 2))
    return np.transpose(crop[a0:a1, width - depth:width], (1, 0, 2))[::-1]


def _hole_mask(profile, a0, a1, depth):
    """True where the outline profile says background enters the window (the notch)."""
    hole = np.zeros((depth, a1 - a0), dtype=bool)
    if profile is None:
        return hole
    for column in range(a0, a1):
        value = profile[column] if column < len(profile) else None
        if value is None:
            continue
        rows = int(min(depth, max(0, math.ceil(float(value)))))
        if rows > 0:
            hole[:rows, column - a0] = True
    return hole


def _component_containing(candidates, seed):
    """Boolean mask of the 8-connected component of `candidates` overlapping `seed` the most."""
    count, labels = cv2.connectedComponents(candidates.astype(np.uint8), connectivity=8)
    if count <= 1:
        return None
    overlap = np.bincount(labels[seed & candidates].ravel(), minlength=count)
    overlap[0] = 0
    best = int(np.argmax(overlap))
    if overlap[best] == 0:
        return None
    return labels == best


def _ring_band(hole, centre_col, half_cols, band_px):
    """The annular ring around the hole: `band_px` around the notch, hole itself excluded.

    Without a visible notch (profile not seen) the band is the first rows on
    the edge around the contact centre: the copper of the land touches the
    edge there even when the hole could not be traced.
    """
    if hole.any():
        kernel = np.ones((2 * band_px + 1, 2 * band_px + 1), np.uint8)
        dilated = cv2.dilate(hole.astype(np.uint8), kernel).astype(bool)
        return dilated & ~hole
    ring = np.zeros_like(hole)
    ring[:band_px, max(0, centre_col - half_cols):centre_col + half_cols + 1] = True
    return ring


def _run_reach(row_mask, centre_col):
    """(first, last) columns of the run of True cells of `row_mask` holding `centre_col`, or None."""
    if centre_col < 0 or centre_col >= len(row_mask) or not row_mask[centre_col]:
        return None
    first = centre_col
    while first > 0 and row_mask[first - 1]:
        first -= 1
    last = centre_col
    while last < len(row_mask) - 1 and row_mask[last + 1]:
        last += 1
    return first, last


def _land_candidates(lab, hole, centre_col, half_pitch, band_px, width_cols):
    """«Not mask» pixels of the window, cleaned: (candidates, mask colour) or (None, reason)."""
    gap = np.zeros(lab.shape[:2], dtype=bool)
    r0, r1 = GAP_SAMPLE_RINGS[0] * band_px, GAP_SAMPLE_RINGS[1] * band_px
    for g in (centre_col - int(round(half_pitch)), centre_col + int(round(half_pitch))):
        gap[r0:r1, max(0, g - band_px):min(width_cols, g + band_px + 1)] = True
    gap &= ~hole
    if not gap.any():
        return None, tx("finestra troppo stretta")
    mask_lab = np.median(lab[gap], axis=0)
    distances = np.linalg.norm(lab - mask_lab, axis=2)
    threshold = _otsu_level(distances.ravel())
    not_mask = ((distances > threshold) & ~hole).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * band_px + 1, 2 * band_px + 1))
    cleaned = cv2.morphologyEx(not_mask, cv2.MORPH_OPEN, kernel)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel).astype(bool) & ~hole
    return cleaned, mask_lab


def _row_reach(row_mask, hole_row, centre_col):
    """(first, last) columns of the copper run around `centre_col`, looking beside the hole."""
    run = _run_reach(row_mask, centre_col)
    if run is not None:
        return run
    beside = np.where(row_mask & ~hole_row)[0]
    if beside.size == 0:
        return None
    left = beside[beside < centre_col]
    right = beside[beside > centre_col]
    first = ((_run_reach(row_mask, int(left.max())) or (centre_col, centre_col))[0]
             if left.size else centre_col)
    last = ((_run_reach(row_mask, int(right.min())) or (centre_col, centre_col))[1]
            if right.size else centre_col)
    return first, last


def _measure_contact(crop, profile, edge, centre_mm, pitch_mm, along_scale, across_scale):
    """One contact: ((width_mm, inward_mm or None), "") or (None, reason)."""
    height, width = crop.shape[:2]
    along_extent = width if edge in ("top", "bottom") else height
    across_extent = height if edge in ("top", "bottom") else width
    half_pitch = (pitch_mm / 2) * along_scale if pitch_mm else along_extent / 2
    half = 2 * WINDOW_HALF_PITCHES * half_pitch
    centre_px = centre_mm * along_scale
    a0 = int(max(0, math.floor(centre_px - half)))
    a1 = int(min(along_extent, math.ceil(centre_px + half)))
    depth = int(max(2, across_extent // 2))
    if a1 - a0 < 3:
        return None, tx("finestra troppo stretta")
    window = _edge_window(crop, edge, a0, a1, depth)
    lab = _lab(np.ascontiguousarray(window))
    hole = _hole_mask(profile, a0, a1, depth)
    band_px = max(2, int(round(MIN_HOLE_WALL_MM * across_scale)))
    centre_col = int(round(centre_px - a0))
    ring = _ring_band(hole, centre_col, max(1, int(round(half_pitch / 2))), band_px)
    ring &= ~hole
    if not ring.any():
        return None, tx("nessun rame accanto al semi-foro")
    cleaned, reason = _land_candidates(lab, hole, centre_col, half_pitch, band_px, a1 - a0)
    if cleaned is None:
        return None, reason
    notch_rows = int(hole.any(axis=1).sum()) if hole.any() else band_px
    reach = half_pitch - EDGE_UNCERTAINTY_PX
    candidates = cleaned.copy()
    continuous_rows = 0
    for r in range(depth):
        run = _row_reach(cleaned[r], hole[r], centre_col)
        if run is None:
            continue
        if run[0] <= centre_col - reach or run[1] >= centre_col + reach:
            candidates[r] = False
            if r <= notch_rows:
                continuous_rows += 1
    blob = _component_containing(candidates, ring)
    if blob is None:
        if continuous_rows >= max(1, notch_rows // 2):
            return None, tx("rame continuo fino al contatto vicino")
        return None, tx("nessun rame accanto al semi-foro")
    widths = np.zeros(depth, dtype=np.float64)
    for r in np.where(blob.any(axis=1))[0]:
        cols = np.where(blob[r])[0]
        widths[r] = cols.max() - cols.min() + 1
    main = widths >= LAND_ROW_RATIO * widths.max()
    main_rows = np.where(main)[0]
    width_px = float(np.percentile(widths[main], LAND_WIDTH_QUANTILE))
    last = int(main_rows.max()) + 1
    inward_mm = None if last >= depth else last / across_scale
    return (width_px / along_scale, inward_mm), ""


def measure_lands(crop, length_mm: float, width_mm: float, rows: dict, profiles) -> dict:
    """@brief Measure the lands of every confirmed row of contacts on the calibrated crop.

    @param crop Calibrated BGR crop, the board filling the frame.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @param rows {edge: dict(centres_mm=[...], pitch_mm=float or None)}: the
        notch centres the outline count CONFIRMED against the declared side
        (edge_notches.confirmed_rows), in mm from the start of the edge (left
        to right for top/bottom, top to bottom for left/right).
    @param profiles Outline profiles of the crop (edge_notches.depth_profiles).
    @return {edge: LandMeasure}. A side value is the median over the
        contacts measured; its tolerance the larger of the pixel uncertainty
        of two boundaries and the spread (MAD) across contacts. A side with no
        measurable contact carries None and the reason of its first contact.
    """
    if cv2 is None or crop is None or getattr(crop, "ndim", 0) != 3 or not rows:
        return {}
    height, width = crop.shape[:2]
    scale_x, scale_y = width / float(length_mm), height / float(width_mm)
    result = {}
    for edge, row in rows.items():
        if edge not in EDGES:
            continue
        along_scale, across_scale = (scale_x, scale_y) if edge in ("top", "bottom") else (scale_y, scale_x)
        profile = (profiles or {}).get(edge)
        measured, rejected = [], []
        for centre in row.get("centres_mm") or ():
            value, reason = _measure_contact(crop, profile, edge, float(centre), row.get("pitch_mm"),
                                             along_scale, across_scale)
            if value is None:
                rejected.append(reason)
            else:
                measured.append(value)
        pixel_tolerance = 2 * EDGE_UNCERTAINTY_PX / along_scale
        if not measured:
            result[edge] = LandMeasure(edge, None, None, round(pixel_tolerance, 3), [], rejected,
                                       rejected[0] if rejected else tx("nessun contatto confermato"))
            continue
        widths = [pair[0] for pair in measured]
        inwards = [pair[1] for pair in measured if pair[1] is not None]
        tolerance = max(pixel_tolerance, _mad_sigma(widths))
        result[edge] = LandMeasure(
            edge, round(float(np.median(widths)), 3),
            round(float(np.median(inwards)), 3) if inwards else None,
            round(tolerance, 3), measured, rejected)
    return result


@dataclass
class CopperRow:
    """@brief Periodic row of elements found along one edge whose outline is straight.

    @note `found` is the number of consecutive elements of the chain (None
        when the strip could not be read or the row is too short to confirm
        or exclude -- see count_edge_copper -- 0 when no row was found);
        `centres_mm` their centres from the start of the edge (left to right
        for top/bottom, top to bottom for left/right); `reason` why nothing
        could be counted. The elements are the LANDS or the GAPS between
        them, whichever class forms the longer chain: the two alternate with
        the same period, and the photo does not say which colour is copper.
        A presence signal, not a count of contacts.
    """

    edge: str
    found: Optional[int]
    centres_mm: list = field(default_factory=list)
    reason: str = ""


#: Rows of the edge strip read for lands, in annular rings (MIN_HOLE_WALL_MM)
#: from the edge: from one ring in, past the bright line of the cut edge, to
#: four rings, where every land of an edge row still shows -- an edge contact
#: touches the edge by definition (half-hole or pad reaching it), while
#: silkscreen labels, traces and mounted bodies sit further in.
COPPER_STRIP_RINGS = (1, 4)
#: Fewest consecutive elements on the lattice that make a row. HEURISTIC,
#: declared as such and FRAGILE: with the alignment window set by the
#: measurement bounds and a straight-line fit of the chain, chance chains on
#: the RS485 long edges (no contacts) stayed below this at 2.54 mm, but
#: reached 5-6 at 1.27 mm (second review 2026-11-03), while a real row gave
#: 18-22 (ESP32 devkit). So at a fine pitch a blind chain of this length is
#: NOT proof of a row: a call with declared centres is decided at those
#: positions (`_declared_position_verdict`), and only calls without them
#: still rely on this number. What would replace it: a threshold derived
#: from the expected chance-chain length at the pitch and the edge's own
#: density of features, measured outside the row. Declared limit: a real row of fewer than this many edge contacts is not confirmable
#: from copper -- and, for the same reason, its ABSENCE is not provable either:
#: a chain shorter than this, or a declared row shorter than this, is reported
#: as unverifiable (`found` None), never as «no copper» (count_edge_copper).
MIN_ROW_LANDS = 5
#: Fewest elements that still form a chain (two points fix a pitch). Only used
#: to tell «a short row may be there» (unverifiable) from «nothing periodic at
#: all» (absence); it never confirms a row.
MIN_CHAIN_LANDS = 2


def _colour_classes(colours, k: int = 2, iterations: int = 20):
    """Class index of each colour row of `colours` (N x 3) and the k centres (Lloyd).

    Seeds: the darkest colour, then repeatedly the colour farthest from every
    seed so far (farthest-point init): no colour is assumed, the k dominant
    colours of the data become the classes.
    """
    seeds = [int(np.argmin(colours[:, 0]))]
    while len(seeds) < k:
        distance = np.min(np.stack([np.linalg.norm(colours - colours[i], axis=1) for i in seeds]), axis=0)
        seeds.append(int(np.argmax(distance)))
    centres = np.stack([colours[i] for i in seeds]).astype(float)
    labels = np.zeros(len(colours), dtype=int)
    for step in range(iterations):
        distances = np.stack([np.linalg.norm(colours - centre, axis=1) for centre in centres], axis=1)
        new_labels = distances.argmin(axis=1)
        if step > 0 and np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for index in range(k):
            member = labels == index
            if member.any():
                centres[index] = colours[member].mean(axis=0)
    return labels, centres


def _two_colour_classes(colours, iterations: int = 20):
    """Boolean class of each colour row of `colours` (N x 3) by two-centre clustering."""
    labels, _centres = _colour_classes(colours, 2, iterations)
    return labels == 1


def count_edge_copper(crop, edge: str, pitch_mm, length_mm: float, width_mm: float,
                      declared_count: Optional[int] = None,
                      declared_centres_mm: Optional[Sequence[float]] = None,
                      centres_assumed: bool = False) -> CopperRow:
    """@brief Count the copper lands lying ON an edge, at the declared pitch, on the calibrated crop.

    @param crop Calibrated BGR crop, the board filling the frame.
    @param edge "top", "bottom", "left" or "right".
    @param pitch_mm Declared pitch of the row, in mm (None: nothing to count).
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @param declared_count Contacts declared on this side, when known (None:
        unknown). Only used to tell whether an absence could be proved.
    @param declared_centres_mm Centres of the DECLARED contacts along the
        edge, mm from its start (`edge_notches._declared_centres_mm`), when
        known. With them the copper is read AT the declared positions
        (`_declared_position_verdict`), which settles presence and absence
        whatever the row length (second review 2026-11-03: a blind chain
        search kept the phantom RS485 sides the user had removed, point 164,
        and found chance chains of 5-6 at 1.27 mm on a contact-free edge).
    @param centres_assumed True when the centres come from the answer
        contract, not from a declared quantity (an omitted offset reads as a
        centred row): copper missing at those positions then says only that
        the row is not THERE, never that it is absent from the edge, so it
        gives `found` None (generality audit 2026-11-03, GA3).
    @return CopperRow with the lands found on the lattice (`found` at least
        MIN_ROW_LANDS), `found` 0 when the strip shows no periodic row and a
        row of the declared length would have been confirmable, or `found`
        None and a reason when nothing can be said.
    @note Contract (2026-11-03, review): a row SHORTER than MIN_ROW_LANDS is
        not confirmable, and so its absence is not provable. Hence a regular
        chain of MIN_CHAIN_LANDS..MIN_ROW_LANDS-1 elements gives `found` None
        (a short row may be there; the caller keeps the declared side as
        unverifiable) when the declared length is unknown; when
        `declared_count` is below MIN_ROW_LANDS even an empty strip gives
        None: such a row could never have reached the confirmable length, so
        not seeing one says nothing. Whatever the declared length, a short
        regular chain at the pitch is None too (generality audit 2026-11-03:
        a declared row of 6 with two lands hidden by a label shows a chain
        of 4); only an edge without any periodic chain at the declared pitch
        is a measured absence, `found` 0. This note covers calls WITHOUT
        declared centres: with them a short row is decided at its positions,
        and every one of them read bare is a measured absence (0) whatever
        the row length (second review 2026-11-03).
    @note A straight outline says the contacts are NOT half-holes; it says
        nothing about whether a row of contacts is there (ESP32 devkit,
        2026-10-08: through-holes with the pad reaching a straight edge were
        declared half-holes, the edge was «clean» and 22 contacts were
        dropped). Presence is a copper fact with three structural marks: an
        edge contact TOUCHES the edge, so only the first rings of the strip
        are read (COPPER_STRIP_RINGS); the lands of one row are IDENTICAL,
        so only runs of the median width count; and a row is PERIODIC at the
        declared pitch, so only the longest chain of CONSECUTIVE lattice
        points occupied (best phase, pitch re-measured on the chain) counts,
        and only when it is at least MIN_ROW_LANDS long.
        The strip's columns are split in two classes by a two-centre
        clustering of their mean Lab colour, whichever colour the mask is
        (a third colour, a label, joins the nearer class); runs of either
        class at least two hole walls wide (MIN_HOLE_WALL_MM,
        the declared fabrication minimum) are the candidate elements, and
        the class with the longer regular chain is the row (lands or gaps,
        see CopperRow). The alignment window is the measurement bound of a
        run's extent (one hole wall plus two boundary pixels), the chain must
        fit a straight line within it and its pitch must agree with the
        declared one within the calibration scale error (the board-edge
        drift find_board accepts at both ends of the edge) plus the window.
    @note Declared limit: a copper feature that touches the edge on a lattice
        point (a mounting-hole ring at the corner) adds one; the caller treats
        a count different from the declared one as «copper present, number to
        verify», never as proof of absence.
    """
    if cv2 is None or crop is None or getattr(crop, "ndim", 0) != 3:
        return CopperRow(edge, None, reason=tx("foto non leggibile"))
    if edge not in EDGES or not pitch_mm or not math.isfinite(float(pitch_mm)) or pitch_mm <= 0:
        return CopperRow(edge, None, reason=tx("passo dei contatti non dichiarato"))
    height, width = crop.shape[:2]
    scale_x, scale_y = width / float(length_mm), height / float(width_mm)
    along_scale, across_scale = (scale_x, scale_y) if edge in ("top", "bottom") else (scale_y, scale_x)
    along_extent = width if edge in ("top", "bottom") else height
    across_extent = height if edge in ("top", "bottom") else width
    band_px = max(2, int(round(MIN_HOLE_WALL_MM * across_scale)))
    r0, r1 = COPPER_STRIP_RINGS[0] * band_px, COPPER_STRIP_RINGS[1] * band_px
    depth = min(r1, across_extent // 2)
    if depth - r0 < 2 or along_extent < 3:
        return CopperRow(edge, None, reason=tx("finestra troppo stretta"))
    strip = _edge_window(crop, edge, 0, along_extent, depth)[r0:depth]
    lab = _lab(np.ascontiguousarray(strip))
    # Each column of the strip has a mean colour; the columns fall in two
    # classes by a two-centre clustering of those colours (Lloyd iterations
    # from the two most distant columns). No colour is assumed: whichever
    # two colours dominate the strip (mask and copper, usually) become the
    # classes, and a third one (a white label) joins the nearer of them.
    try:
        short_declared = declared_count is not None and 0 < int(declared_count) < MIN_ROW_LANDS
    except (TypeError, ValueError):
        short_declared, declared_count = False, None

    def _short_declared():
        # A declared row too short to be confirmed: its absence is not provable.
        return CopperRow(edge, None, reason=tx(
            "fila dichiarata di {n} contatti: troppo corta per essere verificata sul rame",
            n=int(declared_count)))

    columns = lab.mean(axis=0)
    if float(np.linalg.norm(columns.max(axis=0) - columns.min(axis=0))) <= 0:
        if short_declared and not declared_centres_mm:
            return _short_declared()
        return CopperRow(edge, 0, reason=tx("nessuna alternanza di colore sul bordo"))
    column_class = _two_colour_classes(columns)
    min_run = max(2, int(round(2 * MIN_HOLE_WALL_MM * along_scale)))
    pitch_px = float(pitch_mm) * along_scale
    verdict = _declared_position_verdict(
        edge, column_class, declared_centres_mm, along_scale, pitch_px,
        MIN_HOLE_WALL_MM * along_scale + 2 * EDGE_UNCERTAINTY_PX)
    if verdict is not None and verdict.found == 0 and centres_assumed:
        verdict = CopperRow(edge, None, reason=tx(
            "nessuna piazzola alle posizioni di una fila centrata, ma l'offset della fila non e' "
            "dichiarato: la fila puo' stare altrove sul bordo"))
    # How far a measured centre may sit from the straight line of its row:
    # the uncertainty of a run's extent, i.e. one hole wall (the opening and
    # closing of the morphology) plus the pixel uncertainty of two
    # boundaries -- the same tolerance that tells alike widths apart. A
    # measurement bound, never a fraction of the pitch (a quarter pitch made
    # half of every period «on the lattice» and chains formed by chance). The
    # common shift of the calibration is absorbed by the fitted intercept.
    window = MIN_HOLE_WALL_MM * along_scale + 2 * EDGE_UNCERTAINTY_PX

    def runs(mask):
        found, start = [], None
        for index, value in enumerate(mask):
            if value and start is None:
                start = index
            elif not value and start is not None:
                found.append((start, index))
                start = None
        if start is not None:
            found.append((start, len(mask)))
        return [(a, b) for a, b in found if b - a >= min_run]

    def lattice_chain(centres, pitch):
        """Longest consecutive chain of centres on one lattice of `pitch`, as [(k, c)]."""
        best = []
        # Candidate phases are the centres' own residues: every chain the
        # window admits contains a centre, so its phase is within the window
        # of that centre's residue. Independent of the pitch in pixels.
        for phase in sorted({c % pitch for c in centres}):
            indices = {}
            for c in centres:
                k = round((c - phase) / pitch)
                if abs((c - phase) - k * pitch) <= window:
                    indices.setdefault(int(k), c)
            chain, current = [], []
            for k in sorted(indices):
                if current and k == current[-1][0] + 1:
                    current.append((k, indices[k]))
                else:
                    current = [(k, indices[k])]
                if len(current) > len(chain):
                    chain = list(current)
            if len(chain) > len(best):
                best = chain
        return best

    def regular(chain, pitch, minimum=MIN_ROW_LANDS):
        """The chain's centres, if a straight line of the declared pitch fits them.

        Least squares of position against lattice index: every residual
        within the window, and the drift between the fitted and the declared
        pitch over the whole chain within the window too -- the calibration
        scale error accumulated along the row, not a percentage.
        """
        while len(chain) >= minimum:
            ks = np.array([k for k, _c in chain], dtype=float)
            cs = np.array([c for _k, c in chain], dtype=float)
            slope, intercept = np.polyfit(ks, cs, 1)
            residuals = np.abs(cs - (slope * ks + intercept))
            if residuals.max() <= window:
                # The fitted pitch may differ from the declared one by the
                # scale error of the calibration -- the crop box ends are
                # each within the board-edge drift find_board accepts, so
                # the scale is known to 2 x drift over the edge length --
                # plus the window spread over the chain. No percentage.
                drift = max(MAX_BOARD_EDGE_DRIFT_PX, MAX_BOARD_EDGE_DRIFT_RATIO * max(crop.shape[:2]))
                allowed = pitch * (2 * drift / along_extent) + window / (len(chain) - 1)
                return list(cs) if abs(slope - pitch) <= allowed else []
            # The element farthest from the line is not of the row (a corner
            # ring, a label on a lattice point): drop it and keep the longer
            # of the two consecutive pieces it leaves.
            worst = int(residuals.argmax())
            left, right = chain[:worst], chain[worst + 1:]
            chain = left if len(left) >= len(right) else right
        return []

    def row_of(spans, minimum=MIN_ROW_LANDS):
        # Lands of one row are identical by design: runs whose width differs
        # from the median by more than one hole wall (plus the pixel
        # uncertainty of two boundaries) are not its lands -- a label, a
        # mounting-hole ring, a trace end.
        if not spans:
            return []
        widths = np.array([b - a for a, b in spans], dtype=float)
        # Two families of widths may share the strip (lands, and labels or
        # rings of another width): each family is tried on its own, so the
        # row is not lost when the other family outnumbers it. The families
        # are the two sides of the Otsu split of the widths; alike means
        # within the window of the family's median.
        families = [widths]
        if len(widths) >= 2 and widths.max() > widths.min():
            level = _otsu_level(widths)
            families = [widths[widths <= level], widths[widths > level]]
        rows = []
        for family in families:
            if family.size == 0:
                continue
            alike = [(a, b) for (a, b), w in zip(spans, widths) if abs(w - np.median(family)) <= window]
            rows.append(_row_from([(a + b) / 2 for a, b in alike], minimum))
        return max(rows, key=len) if rows else []

    def _row_from(centres, minimum=MIN_ROW_LANDS):
        # The declared pitch is in millimetres and the photo scale carries
        # its own error (0.3 % on the sample board): the chain found measures
        # the pitch in pixels and a second pass on that pitch collects the
        # ends -- accepted only if `regular` still holds against the DECLARED
        # pitch, so a chain of some other period never passes.
        chain = lattice_chain(centres, pitch_px)
        row = regular(chain, pitch_px, minimum)
        if row:
            measured = (chain[-1][1] - chain[0][1]) / (chain[-1][0] - chain[0][0])
            refined = regular(lattice_chain(centres, measured), pitch_px, minimum)
            if len(refined) > len(row):
                row = refined
        return row

    best = max((row_of(runs(column_class)), row_of(runs(~column_class))), key=len)
    if verdict is not None:
        if len(best) >= MIN_ROW_LANDS:
            # A long periodic row confirms the contacts unless the declared
            # positions deny it: then it is a chance alignment of other
            # features (a crowded edge at a fine pitch), unverifiable.
            if verdict.found == 0:
                return CopperRow(edge, None, [round(c / along_scale, 3) for c in best], reason=tx(
                    "una fila di {n} elementi al passo dichiarato non coincide con le posizioni "
                    "dichiarate dei contatti", n=len(best)))
            return CopperRow(edge, len(best), [round(c / along_scale, 3) for c in best])
        # No long row: the declared positions decide (present, measured
        # absence, or unverifiable).
        return verdict
    if len(best) < MIN_ROW_LANDS:
        # Not confirmable is not absent (contract above). A declared row
        # shorter than the confirmable length could never have shown as one;
        # a declared row long enough would have, and chance chains on crowded
        # edges reach MIN_ROW_LANDS - 1 (see MIN_ROW_LANDS), so a short chain
        # does not save it. With no declared length, a short regular chain
        # may be a short row: unverifiable.
        if short_declared:
            return _short_declared()
        # A short regular chain at the declared pitch is unverifiable whatever
        # the declared length (generality audit 2026-11-03): a declared row of
        # 6 with two lands under a label shows a chain of 4, and a chance chain
        # on a crowded edge cannot be told from it. Only an edge with NO
        # periodic chain at the pitch is a measured absence (0).
        short = max((row_of(runs(column_class), MIN_CHAIN_LANDS),
                     row_of(runs(~column_class), MIN_CHAIN_LANDS)), key=len)
        if len(short) >= MIN_CHAIN_LANDS:
            return CopperRow(edge, None, [round(c / along_scale, 3) for c in short],
                             reason=tx("fila di {n} elementi al passo dichiarato: troppo corta "
                                       "per confermare o escludere i contatti", n=len(short)))
        return CopperRow(edge, 0, reason=tx("nessuna fila di piazzole al passo dichiarato sul bordo"))
    return CopperRow(edge, len(best), [round(c / along_scale, 3) for c in best])


#: Significance level of the chance test of `_declared_position_verdict`:
#: the conventional 5 %, not a value tuned on a photo. A full match at the
#: declared positions confirms the row only when its chance on the edge's own
#: background is below it. The test never proves an absence: not rejecting
#: chance says nothing about a row that may be there.
CHANCE_SIGNIFICANCE = 0.05


def _binomial_tail(n: int, k: int, p: float) -> float:
    """Probability of k or more successes out of n with success probability p."""
    return float(sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def _background_rate(column_class, span_px, pitch_px: float, half_px: float) -> float:
    """Rate at which the position test fires on the edge OUTSIDE the declared row.

    Samples half a pitch apart (and at least one window), so two samples do
    not read the same feature twice, skipping the declared row's span (a real
    row there would contaminate the background). The rate is the Laplace
    estimate (fired + 1) / (samples + 2): with few samples it stays near one
    half, so a short edge never makes a chance match look rare -- no minimum
    sample count is needed (GA6).
    """
    lo, hi = span_px
    step = max(1.0, 2 * half_px, pitch_px / 2)
    fired = total = 0
    position = half_px
    while position < len(column_class) - half_px:
        if not (lo <= position <= hi):
            at = _window_class(column_class, position, half_px)
            gaps = [g for g in (_window_class(column_class, position - pitch_px / 2, half_px),
                                _window_class(column_class, position + pitch_px / 2, half_px))
                    if g is not None]
            if at is not None and gaps:
                total += 1
                fired += all(gap != at for gap in gaps)
        position += step
    return (fired + 1) / (total + 2)


#: Minimum fraction of a sampling window that must share one colour class
#: for that window to have a class at all: a STRUCTURAL majority (more than
#: half), not a tuned value; a window split by a boundary says nothing.
_WINDOW_MAJORITY = 0.5


def _window_class(column_class, centre_px: float, half_px: float):
    """The colour class (True/False) most columns of [centre - half, centre + half] share, or None (outside or split)."""
    a0 = int(math.floor(centre_px - half_px))
    a1 = int(math.ceil(centre_px + half_px)) + 1
    if a0 < 0 or a1 > len(column_class) or a1 - a0 < 1:
        return None
    window = column_class[a0:a1]
    share = float(np.mean(window))
    if share > _WINDOW_MAJORITY:
        return True
    if 1.0 - share > _WINDOW_MAJORITY:
        return False
    return None


def _declared_position_verdict(edge, column_class, centres_mm, along_scale: float, pitch_px: float,
                               window_px: float) -> Optional[CopperRow]:
    """@brief Presence of the declared contacts read AT their declared positions on the edge strip.

    @param edge Edge name.
    @param column_class Boolean colour class of each strip column (two-centre clustering).
    @param centres_mm Declared contact centres along the edge (mm from its start), or None.
    @param along_scale Pixels per mm along the edge.
    @param pitch_px Declared pitch in pixels.
    @param window_px Measurement window of one feature (one hole wall plus two
        boundary pixels): each sample reads +/- half of it.
    @return None without declared centres; else a CopperRow: `found` = the
        declared count when EVERY declared position differs in colour class
        from the gaps beside it AND all positions share one class (identical
        lands of one row); `found` 0 when NO position differs from its gaps
        (a measured absence: the edge shows the same colour at the contacts
        and between them); `found` None with the count of differing positions
        otherwise (some lands hidden or a crowded edge: unverifiable).
    @note Structural, no tuned number: a land is copper touching the edge
        and the gaps of a row are mask, so at a real row the two alternate at
        the declared pitch; the gaps are sampled half a pitch from each
        centre (the middle between two lands), the windows are the existing
        measurement bound. The first and last contact use the gap that lies
        on the board only.
    """
    if not centres_mm or pitch_px <= 0:
        return None
    half = max(1.0, window_px / 2)
    if pitch_px / 2 < 2 * half:
        # The gap windows half a pitch away would overlap the land window:
        # land and gap cannot be read apart at this resolution (GA7).
        return CopperRow(edge, None, reason=tx(
            "passo dichiarato troppo fine per la risoluzione della foto: posizioni non leggibili"))
    readable, differing, classes = 0, [], set()
    for centre_mm in centres_mm:
        centre = float(centre_mm) * along_scale
        at = _window_class(column_class, centre, half)
        gaps = [g for g in (_window_class(column_class, centre - pitch_px / 2, half),
                            _window_class(column_class, centre + pitch_px / 2, half)) if g is not None]
        if at is None or not gaps:
            continue
        readable += 1
        if all(gap != at for gap in gaps):
            differing.append(round(float(centre_mm), 3))
            classes.add(at)
    if readable == 0:
        return CopperRow(edge, None, reason=tx("posizioni dichiarate non leggibili sul bordo"))
    total = len(centres_mm)
    if not differing:
        # A measured absence needs EVERY declared position read: an unread
        # position may be the land (generality audit 2026-11-03, GA2).
        if readable == total:
            return CopperRow(edge, 0, reason=tx(
                "nessuna piazzola alle {n} posizioni dichiarate sul bordo", n=total))
        return CopperRow(edge, None, reason=tx(
            "nessuna piazzola alle {k} posizioni lette su {n} dichiarate: le altre non sono "
            "leggibili", k=readable, n=total))
    if len(differing) == total and len(classes) == 1:
        # A full match confirms the row only when chance could not produce
        # it on this edge (GA4): a busy edge whose features happen to fall on
        # both positions of a phantom 2-contact side is no row.
        centres_px = [float(c) * along_scale for c in centres_mm]
        span = (min(centres_px) - pitch_px, max(centres_px) + pitch_px)
        rate = _background_rate(column_class, span, pitch_px, half)
        if _binomial_tail(total, total, rate) < CHANCE_SIGNIFICANCE:
            return CopperRow(edge, total, differing)
        return CopperRow(edge, None, differing, reason=tx(
            "rame a tutte le {n} posizioni dichiarate, ma frequente anche sul resto del bordo "
            "({rate:.0%}): potrebbe essere un caso", n=total, rate=rate))
    # Some positions differ, some do not: a row with lands hidden (a label,
    # a mounted connector) or chance on a busy edge. Failing to tell copper
    # from the background is NOT evidence of absence (generality audit
    # 2026-11-03, GA1: the chance test used to return 0 here and excluded the
    # side, a real 4-contact row read at a wrong offset among them):
    # unverifiable, the side stays flagged for review.
    return CopperRow(edge, None, differing, reason=tx(
        "rame a {k} delle {n} posizioni dichiarate", k=len(differing), n=total))


@dataclass
class HoleStyle:
    """@brief What the photo says about the holes of the contacts of one edge.

    @note `style` is "half_hole" (the hole centre sits on the edge: the
        opening on the edge is as wide as the hole), "half_and_through" (an
        opening on the edge AND a closed hole behind it, or a full plated
        hole inside the board that the edge only nicks: the opening is a
        chord narrower than the hole, whose widest row lies inside) or
        "through_hole" (a closed circle and NO opening on the edge, whether
        its land reaches the edge or not), or None when the lands or the
        holes could not be read; `contacts` says on how many lands the
        verdict rests, `agreeing` how many agree with it; `drill_mm` the
        median hole width, `chord_mm` the median width of the opening on the
        edge rows (0.0 for through_hole), both over the agreeing lands only;
        `openings` how many agreeing lands showed an opening segment,
        `closed` how many a CLOSED hole behind the edge (a through hole the
        contact has, whatever the edge shows);
        `reason` why there is no verdict;
        `row_lands` the length of the row when its lands were located by an
        independent witness (the outline notches, `confirmed_centres_mm` of
        `measure_edge_holes`), None when the reader found the row itself by
        its blind chain search (see `hole_agreement_floor`).
    """

    edge: str
    style: Optional[str]
    contacts: int = 0
    agreeing: int = 0
    drill_mm: Optional[float] = None
    chord_mm: Optional[float] = None
    reason: str = ""
    openings: int = 0
    closed: int = 0
    row_lands: Optional[int] = None

    def as_dict(self) -> dict:
        """@brief JSON-friendly copy for the extraction answer (`_measured_holes`).

        @return A plain dict with the edge, the hole style, the contact,
            agreeing and opening counts, the drill and chord diameters in mm
            (None when not measured), the reason text and the length of a
            row located by the outline (`row_lands`, None otherwise).
        """
        return dict(edge=self.edge, style=self.style, contacts=self.contacts,
                    agreeing=self.agreeing, drill_mm=self.drill_mm, chord_mm=self.chord_mm,
                    reason=self.reason, openings=self.openings, closed=self.closed,
                    row_lands=self.row_lands)


def hole_agreement_floor(row_lands=None) -> int:
    """@brief Fewest lands that must agree before a hole verdict (or a count of closed holes) stands.

    @param row_lands Length of a row whose lands an independent witness
        located (`HoleStyle.row_lands`), or None for a row the reader found
        by its own blind chain search.
    @return MIN_ROW_LANDS for a blind row; for a located row, its length
        capped at MIN_ROW_LANDS -- every land of a row shorter than
        MIN_ROW_LANDS must agree -- and never fewer than two: one land alone
        has no other land of the row to corroborate its reading, so a
        one-contact side gets no verdict (structural, not tuned).
    @note Why the two cases differ (night1 RS485 run, 2026-11-14: rows of 3
        and 4 castellations the outline confirmed, and the reader never gave
        a verdict because it asked for 5 agreeing lands). MIN_ROW_LANDS
        guards against CHANCE: a blind chain of fewer lands may be labels or
        marks aligned at the pitch. When the outline has counted exactly the
        declared notches at the declared contacts
        (`edge_notches.confirmed_rows`), the lands are known and no chance
        chain is involved; what remains is the colour reading of each land,
        which a unanimity of the whole short row guards instead. No number
        is tuned on a board: the floor is the row's own length.
    """
    try:
        located = int(row_lands) if row_lands is not None else 0
    except (TypeError, ValueError):
        located = 0
    if located <= 0:
        return MIN_ROW_LANDS
    return max(2, min(MIN_ROW_LANDS, located))


def choose_land_class(verdicts, classes: int, mask_class: int, floor: int, spans=None,
                      enclosure_px: float = 0.0):
    """@brief Which colour class holds the lands, its verdict, and whether that verdict stands.

    @param verdicts [(class index, verdict)] of every land read, one entry per
        land and colour class (`measure_edge_holes`).
    @param classes Number of colour classes.
    @param mask_class The class of the board's own colour (never the lands).
    @param floor Fewest agreeing lands (`hole_agreement_floor`).
    @param spans Optional {class: typical width along the edge, px} of the
        runs each class read as lands.
    @param enclosure_px How much wider than what it encloses a land must be:
        the copper ring on both sides of a hole, 2 x MIN_HOLE_WALL_MM in px.
    @return None when no class but the mask produced a reading; otherwise
        (land class, its majority verdict, lands agreeing on it, lands read
        in that class, decided). `decided` is False when the majority is
        below the floor or below half of the lands read, or when another
        non-mask class that passes the same tests reaches a DIFFERENT
        verdict and the enclosure rule cannot tell which class is the land.
    @note Generality audit 2026-11-14, M2. Several colour classes may read
        "lands": the copper, and the colour that shows through the holes,
        whose runs enclose specks of the ring. A land SURROUNDS its hole, so
        along the edge its run is wider than the hole's by the copper ring
        on both sides, at least 2 x MIN_HOLE_WALL_MM by fabrication. Among
        the passing classes with conflicting verdicts, the one wider than
        every rival by that much encloses them and is the land; when no
        class is that much wider, the photo does not say which colour is
        copper and the reading abstains -- the class order of the clustering
        or a one-land lead never picks the style. A rival below the floor is
        noise (labels, specks). Structural: the margin is the fabrication
        minimum, no number tuned on a board.
    """
    def majority(cls):
        kept = [v for k, v in verdicts if k == cls]
        if cls == mask_class or not kept:
            return None
        best = max(sorted(set(kept)), key=kept.count)
        return best, kept.count(best), len(kept)

    def passes(entry):
        return entry is not None and entry[1] >= floor and entry[1] * 2 >= entry[2]

    readings = {cls: majority(cls) for cls in range(classes)}
    readings = {cls: entry for cls, entry in readings.items() if entry is not None}
    if not readings:
        return None
    land_class = max(readings, key=lambda cls: (readings[cls][1], readings[cls][2], -cls))
    passing = [cls for cls, entry in readings.items() if passes(entry)]
    if land_class in passing and any(readings[cls][0] != readings[land_class][0] for cls in passing):
        # Conflicting passing classes: the enclosing one is the land, if any.
        widths = spans or {}

        def encloses(cls):
            return cls in widths and all(
                other in widths and widths[cls] - widths[other] >= enclosure_px
                for other in passing if readings[other][0] != readings[cls][0])
        enclosing = [cls for cls in passing if encloses(cls)]
        if len(enclosing) != 1:
            best, agreeing, read = readings[land_class]
            return land_class, best, agreeing, read, False
        land_class = enclosing[0]
    best, agreeing, read = readings[land_class]
    return land_class, best, agreeing, read, passes(readings[land_class])


def measure_edge_holes(crop, edge: str, pitch_mm, length_mm: float, width_mm: float,
                       confirmed_centres_mm: Optional[Sequence[float]] = None,
                       land_inward_mm: Optional[float] = None) -> HoleStyle:
    """@brief Read, land by land, how the holes of an edge row meet the board edge.

    @param crop Calibrated BGR crop, the board filling the frame.
    @param edge "top", "bottom", "left" or "right".
    @param pitch_mm Declared pitch of the row, in mm.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @param confirmed_centres_mm Contact centres along the edge (mm from its
        start) that an INDEPENDENT witness located -- the outline notches of
        `edge_notches.confirmed_rows`, whose count equals the declared one --
        or None. Given, the lands are read at those centres instead of being
        searched by the blind copper chain (`count_edge_copper`), and the
        verdict needs the agreement of `hole_agreement_floor(len(centres))`
        lands: a row shorter than MIN_ROW_LANDS is then readable, with every
        land agreeing.
    @param land_inward_mm Inward extent of the lands MEASURED on the photo
        (`measure_lands`, `LandMeasure.inward_mm`), or None. Used only with
        `confirmed_centres_mm`: the holes are then read over the whole land
        plus one hole wall, not only one pitch from the edge, so «no closed
        hole» covers every row of the land. On a located row a land still
        filling the inner row of the window gives no «half_hole» reading: a
        closed hole may lie past it (evidence of absence only over what was
        read).
    @return HoleStyle; `style` None when there is no verdict.
    @note Geometry, not a declaration (user, 2026-10-08: the model declared
        «half_hole» or «half_and_through» for the same photo from one run to
        the next, and the header pins depend on it). In the strip of one
        pitch from the edge the columns fall in three colour classes
        (`_colour_classes`: mask, copper, what the hole shows; only the periodic row counts); the lands are the runs of the class whose
        runs enclose a hole (a run of the other colour bounded by the land
        on both sides); for each land an OPENING is a hole segment that
        starts within one hole wall (MIN_HOLE_WALL_MM) of the edge -- no
        copper wall separates it from the edge, so on the colours it reads as
        a cut, which only the outline can confirm -- and a CLOSED hole one
        that starts farther in; the opening on the edge rows (chord
        c, the width on the edge rows -- the first hole wall's worth of rows
        past the bright cut line, where a cut barrel is as wide as the cut)
        and the widest hole row (D, at
        depth r*) decide: D within the measurement window of c and r* on
        the edge rows -> the centre is on the edge, "half_hole"; D wider
        than c by more than the window with r* inside -> a full hole the
        edge nicks, "half_and_through"; an opening AND a closed hole behind
        it -> "half_and_through"; a closed hole and NO opening segment ->
        "through_hole", whether its land reaches the edge or is set back
        (user, 2026-11-13: an edge pad around a closed hole was called
        «half_and_through», and the generator then drilled a half-hole the
        board does not have). Drill and chord medians are taken over the
        lands of the winning verdict only, so a closed-only land never
        lowers the opening of the lands that have one. The side's verdict is the majority, taken only when
        at least `hole_agreement_floor` lands agree (MIN_ROW_LANDS for a
        blind row, the whole row for a short row the outline located) and
        they are at least half of the lands read. No board-specific number: the window is the
        uncertainty of a run's extent (one hole wall plus two boundary
        pixels), as in count_edge_copper.
    """
    if cv2 is None or crop is None or getattr(crop, "ndim", 0) != 3:
        return HoleStyle(edge, None, reason=tx("foto non leggibile"))
    if edge not in EDGES or not pitch_mm or not math.isfinite(float(pitch_mm)) or pitch_mm <= 0:
        return HoleStyle(edge, None, reason=tx("passo dei contatti non dichiarato"))
    height, width = crop.shape[:2]
    scale_x, scale_y = width / float(length_mm), height / float(width_mm)
    along_scale, across_scale = (scale_x, scale_y) if edge in ("top", "bottom") else (scale_y, scale_x)
    along_extent = width if edge in ("top", "bottom") else height
    across_extent = height if edge in ("top", "bottom") else width
    band_px = max(2, int(round(MIN_HOLE_WALL_MM * across_scale)))
    depth = int(min(across_extent // 2, max(4 * band_px, round(float(pitch_mm) * across_scale))))
    if depth < 2 * band_px or along_extent < 3:
        return HoleStyle(edge, None, reason=tx("finestra troppo stretta"))
    located = [float(c) for c in confirmed_centres_mm or ()
               if finite_number(c) is not None
               and 0.0 <= float(c) * along_scale <= along_extent]
    if confirmed_centres_mm and len(located) != len(confirmed_centres_mm):
        # A located centre off the edge (or not a number) means the outline's
        # count and this frame disagree: the lands can no longer be matched
        # one to one with the declared contacts, and a floor taken from the
        # surviving centres would ask fewer lands than the row has (audit
        # 2026-11-14, L2). No verdict rather than a verdict on part of a row.
        return HoleStyle(edge, None, reason=tx("posizioni dichiarate non leggibili sul bordo"),
                         row_lands=len(confirmed_centres_mm))
    # Rows the holes are read on: one pitch from the edge (`depth`), or, on
    # a located row with a measured land, the whole land plus one hole wall
    # -- the inner through-hole of a half_and_through contact lies inside
    # its land, which may run deeper than one pitch. Never past the middle
    # of the board. The land runs and the mask colour keep `depth`.
    read_depth = depth
    inward = finite_number(land_inward_mm)
    if located and inward is not None and inward > 0:
        read_depth = int(min(across_extent // 2,
                             max(depth, math.ceil((inward + MIN_HOLE_WALL_MM) * across_scale))))
    strip = _edge_window(crop, edge, 0, along_extent, read_depth)
    lab = _lab(np.ascontiguousarray(strip))
    # Column classes on the rings the lands always fill (as count_edge_copper),
    # then every pixel of the strip joins the nearer of the two column colours.
    r0, r1 = COPPER_STRIP_RINGS[0] * band_px, min(depth, COPPER_STRIP_RINGS[1] * band_px)
    columns = lab[r0:r1].mean(axis=0)
    if float(np.linalg.norm(columns.max(axis=0) - columns.min(axis=0))) <= 0:
        return HoleStyle(edge, None, reason=tx("nessuna alternanza di colore sul bordo"))
    # Three colours live on such a strip: the mask, the copper and what shows
    # through the hole (the plated barrel, the background); with two classes
    # the barrel joined the copper and the opening on the edge vanished.
    _labels, centres = _colour_classes(columns, 3)
    pixels = lab.reshape(-1, 3)
    pixel_class = np.stack([np.linalg.norm(pixels - c, axis=1) for c in centres], axis=1)
    pixel_class = pixel_class.argmin(axis=1).reshape(lab.shape[:2])
    min_run = max(2, int(round(2 * MIN_HOLE_WALL_MM * along_scale)))
    window = MIN_HOLE_WALL_MM * along_scale + 2 * EDGE_UNCERTAINTY_PX
    # Only lands of the periodic row are read: a body or a label on an edge
    # without contacts also encloses a patch of another colour.
    pitch_px = float(pitch_mm) * along_scale
    if located:
        # The outline located each contact (one notch per declared contact):
        # those centres ARE the lands, so only they are probed -- not the
        # half-pitch points, which would be the gaps.
        lattice = [c * along_scale for c in located]
        points = list(lattice)
        row_lands = len(located)
    else:
        row = count_edge_copper(crop, edge, pitch_mm, length_mm, width_mm)
        if not row.found:
            return HoleStyle(edge, None, reason=row.reason or tx("nessuna fila di piazzole sul bordo"))
        lattice = [c * along_scale for c in row.centres_mm]
        # The chain may be the lands or the gaps: a land holds a chain point
        # or a point half a pitch from one (a land whose tongue to the edge
        # is not centred still holds the point).
        points = lattice + [k + pitch_px / 2 for k in lattice] + [k - pitch_px / 2 for k in lattice]
        row_lands = None
    floor = hole_agreement_floor(row_lands)

    def on_row(a, b):
        # A land run holds one of the probe points, within the window.
        return any(a - window <= k <= b + window for k in points)

    def runs(mask):
        found, start = [], None
        for index, value in enumerate(mask):
            if value and start is None:
                start = index
            elif not value and start is not None:
                found.append((start, index))
                start = None
        if start is not None:
            found.append((start, len(mask)))
        return [(a, b) for a, b in found if b - a >= min_run]

    def hole_profile(land, a, b):
        """Per row, the width of the largest non-land run enclosed by land pixels in [a, b)."""
        widths = np.zeros(read_depth, dtype=float)
        for r in range(read_depth):
            row = land[r, a:b]
            edges = np.where(row)[0]
            if edges.size < 2:
                continue
            inner = row[edges[0]:edges[-1] + 1]
            best, start = 0, None
            for index, value in enumerate(inner):
                if not value and start is None:
                    start = index
                elif value and start is not None:
                    best = max(best, index - start)
                    start = None
            widths[r] = best
        return widths

    verdicts, drills, chords, has_opening, has_closed = [], [], [], [], []
    # Typical width along the edge of the runs each class read as lands,
    # for the enclosure rule of `choose_land_class`.
    class_spans = {}
    for land_class in range(len(centres)):
        land = pixel_class == land_class
        # A land is the run of columns that hold its colour somewhere in the
        # strip (at least one ring's worth of rows): a U-shaped land, whose
        # opening on the edge runs deeper than the first rings, is then one
        # run and not its two legs.
        spans = [(a, b) for a, b in runs(land[:depth].sum(axis=0) >= band_px) if on_row(a, b)]
        if len(spans) < floor:
            continue
        # The lands of a row are alike: runs narrower than the row's by more
        # than the window are marks and tongues, not lands.
        typical = float(np.median([b - a for a, b in spans]))
        class_spans[land_class] = typical
        for a, b in spans:
            if abs((b - a) - typical) > window:
                continue
            widths = hole_profile(land, a, b)
            if widths.max() < min_run / 2:
                continue                      # nothing as wide as a hole wall: a plain pad
            # Segments of rows with an enclosed non-land run: the one that
            # touches the edge is the opening (past the bright cut line), any
            # other, separated by rows of solid copper, is a closed hole.
            segments, start = [], None
            for r in range(read_depth):
                if widths[r] >= min_run / 2 and start is None:
                    start = r
                elif widths[r] < min_run / 2 and start is not None:
                    segments.append((start, r))
                    start = None
            if start is not None:
                segments.append((start, read_depth))
            # A CLOSED hole has a hole wall of solid land between it and the
            # edge (band_px rows, MIN_HOLE_WALL_MM). A hole that starts closer
            # has no such wall and is read as an OPENING of the edge: on the
            # land colours it cannot be told from a cut whose first rows the
            # cut line or the blur hid (a 0.6 mm castellation at 12 px/mm
            # starts at row 2, audit 2026-11-13 H1). Whether such an edge
            # feature IS a cut is the outline's to say, not the colours'
            # (photo_inventory.settle_edge_holes: a copper-free notch of a
            # U-shaped pad looks the same, 18:32 devkit run).
            opening = next((seg for seg in segments if seg[0] <= band_px), None)
            closed = [seg for seg in segments if seg[0] > band_px and seg is not opening]
            chord = 0.0
            if opening:
                # The width ON the edge: one hole wall's worth of rows from the
                # opening's first row past the cut line (row 0). The median over
                # the whole opening read a semicircle at ~0.7-0.9 of its chord
                # and made real castellations look narrower (audit 2026-11-13, H1).
                first = max(opening[0], 1)
                edge_rows = widths[first:min(opening[1], first + band_px)]
                chord = float(np.median(edge_rows)) if edge_rows.size else float(widths[opening[0]])
            if opening and closed:
                # A half-hole on the edge and a full hole behind it.
                verdict, diameter = "half_and_through", float(max(widths[s0:s1].max() for s0, s1 in closed))
            elif opening:
                widest = int(widths[opening[0]:opening[1]].argmax()) + opening[0]
                diameter = float(widths[opening[0]:opening[1]].max())
                # Widest on the edge rows: the hole centre sits on the edge.
                # Widest inside by more than the window: a full hole the edge
                # only nicks, which is a hole as well.
                verdict = ("half_hole" if widest <= 2 * band_px and diameter - chord <= window
                           else "half_and_through" if diameter - chord > window else None)
                if verdict is None:
                    continue
                if (verdict == "half_hole" and row_lands is not None
                        and float(land[read_depth - 1, a:b].mean()) > _WINDOW_MAJORITY):
                    # «No closed hole» is evidence of absence only over the
                    # land the strip holds: a land that still fills the
                    # strip's inner row runs past the window, and a closed
                    # hole may lie beyond it. That land says nothing. Only
                    # on located rows, which read the measured land whole;
                    # the blind reading keeps its one-pitch contract.
                    continue
            elif closed:
                # A closed hole and no opening on the edge: a through-hole
                # contact, whether its land reaches the edge or is set back.
                # «half_and_through» means a half-hole CUT by the edge, which
                # the generator drills; with no opening the board has none
                # (user, 2026-11-13: the 18:32 devkit run, plain header holes).
                verdict = "through_hole"
                diameter = float(max(widths[s0:s1].max() for s0, s1 in closed))
            else:
                continue
            verdicts.append((land_class, verdict))
            drills.append(diameter / along_scale)
            chords.append(chord / along_scale)
            has_opening.append(opening is not None)
            has_closed.append(bool(closed))
    if not verdicts:
        return HoleStyle(edge, None, reason=tx("nessun foro leggibile sulle piazzole"),
                         row_lands=row_lands)
    # The land class is the one whose runs enclose holes that AGREE: a row
    # of contacts has one kind of hole, while the gaps between them enclose
    # labels and marks of every shape. The class with the largest agreeing
    # majority wins, not the one with the most readings.
    # The solder mask is the board's own colour, the same far from the edge
    # as between the contacts: the class nearest to the colour of the board
    # interior (two to three pitches in) is the mask, and it encloses labels,
    # not holes. The lands are chosen among the other classes.
    inner_depth = min(across_extent, 3 * depth)
    interior = _lab(np.ascontiguousarray(_edge_window(crop, edge, 0, along_extent, inner_depth)[2 * depth:]))
    board_colour = np.median(interior.reshape(-1, 3), axis=0) if interior.size else centres[0]
    mask_class = int(np.argmin(np.linalg.norm(centres - board_colour, axis=1)))

    choice = choose_land_class(verdicts, len(centres), mask_class, floor, class_spans,
                               2 * MIN_HOLE_WALL_MM * along_scale)
    if choice is None:
        # Every reading came from the mask class: labels, not holes.
        return HoleStyle(edge, None, reason=tx("nessun foro leggibile sulle piazzole"),
                         row_lands=row_lands)
    land_class, best, agreeing, kept, decided = choice
    if not decided:
        return HoleStyle(edge, None, kept, agreeing,
                         reason=tx("letture dei fori discordi o troppo poche"),
                         row_lands=row_lands)
    # Medians over the lands of the winning verdict only (M3): every verdict
    # with an edge opening has one on each of its lands, a through_hole land
    # has none, so no statistic mixes the two.
    chosen = [i for i, (k, v) in enumerate(verdicts) if k == land_class and v == best]
    return HoleStyle(edge, best, kept, agreeing,
                     round(float(np.median([drills[i] for i in chosen])), 3),
                     round(float(np.median([chords[i] for i in chosen])), 3),
                     openings=sum(1 for i in chosen if has_opening[i]),
                     closed=sum(1 for i in chosen if has_closed[i]),
                     row_lands=row_lands)


def land_lines(lands: dict) -> list:
    """@brief Log lines of the measurement, one per edge: (kind, message).

    @param lands Result of measure_lands.
    @return "log" lines for measured sides, "warning" for sides that could
        not be measured (nothing is used for them).
    """
    from .edge_notches import _edge_name
    lines = []
    for edge, land in lands.items():
        name = _edge_name(edge)
        if land.width_mm is None:
            lines.append(("warning", tx(
                "Lato {edge}: piazzole non misurabili sulla foto ({reason}): restano le "
                "quote dei documenti o le stime.", edge=name, reason=land.reason)))
            continue
        inward = (tx(", estensione verso l'interno {inward:.2f} mm", inward=land.inward_mm)
                  if land.inward_mm is not None else "")
        rejected = (tx("; {count} contatti non misurabili ({reason})",
                       count=len(land.rejected), reason=land.rejected[0])
                    if land.rejected else "")
        lines.append(("log", tx(
            "Lato {edge}: piazzole misurate sulla foto su {count} contatti: larghezza lungo il "
            "bordo {width:.2f} mm (±{tolerance:.2f}){inward}{rejected}.",
            edge=name, count=len(land.contacts), width=land.width_mm,
            tolerance=land.tolerance_mm, inward=inward, rejected=rejected)))
    return lines
