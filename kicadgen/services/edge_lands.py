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
from typing import Optional

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None
    np = None

from ..generators.castellated_module import MIN_HOLE_WALL_MM
from .messages import tx
from .photo_refine import _lab, _otsu_level

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

#: Pixels of uncertainty on each measured boundary: the outline of a patch
#: on a photograph is one pixel wide on each side.
EDGE_UNCERTAINTY_PX = 1.0

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
        """@brief JSON-friendly copy for the extraction answer (`_measured_lands`)."""
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
