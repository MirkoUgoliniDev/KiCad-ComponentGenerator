"""@brief Count the half-hole notches of a castellated module on its calibrated top photo.

The number of contacts per side comes from the AI reading the documents, and
an answer can be consistent with itself (as many names as contacts, a row that
fits its edge) and still be wrong: on the RS485 sample the model once counted
an LED label as a fifth contact (HANDOFF points 132-133). The photo can check
it: a half-hole is a plated hole cut by the board edge, so on a top view it is
a notch where the BACKGROUND shows through the board outline. Counting notches
is a structural measure of the outline, not a colour rule for gold or silver
plating, and holds for any module.

This module only measures and compares; it makes no AI call. What happens
with a disagreement is decided by the caller: a warning, and since HANDOFF
point 137 one paid recount of the disagreeing sides done by the extractor
(datasheet_extractor._recount_sides_from_photo), adopted only when the
recounted contacts sit where the photo shows the notches.

How the outline is measured (HANDOFF point 139, G1):
- the profile of an edge is the signed position of the board outline relative
  to the crop edge, per position along it (positive inside the crop);
- the reference is the outline itself between the notches -- the LOWER
  ENVELOPE of the profile, fitted as a line plus a local correction -- not the
  median of the profile: a residual tilt of the photo makes the outline a
  ramp, and dense castellations cover more than half of the edge;
- the detection threshold comes from the noise of that outline measured on
  the same photo, never below the edge uncertainty of the photo; a notch must
  stand clearly above it, and above half the declared half-hole radius when
  the side declares one;
- depths are measured with the scale ACROSS the edge, widths and positions
  with the scale ALONG it: photos are calibrated axis by axis.

A count is valid only when the photo proves where the row ENDS (HANDOFF
point 139, final review R-G1/R-M3): runs touching an end of the edge (corner
arc, background columns of a tilted crop) are set aside before neighbouring
runs are joined, and if the next notch of the row could hide in such a stretch
the count is only a lower bound; on a declared side, notches that sit on
consecutive declared contacts, fewer than declared, are a lower bound too (an
end notch covered on plain outline leaves no other trace).

Limits, declared: only open half-holes are countable (a full hole inside the
board does not touch the outline); a background indistinguishable from the
board, a photo too coarse to resolve a notch, a notch hardly deeper than the
outline noise, or a row with a missing notch (an arrow, dust or a shadow can
hide one) gives «not countable» or «at least N», never a guessed number. Pads
of full holes can bump the outline by a noise-level amount: on a side whose
contact style is not declared half_hole the photo only reports, it never asks
for a recount.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Union

try:
    from ..cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX
    from ..core.validation import finite_number, positive_number
    from .messages import tx
except ImportError:  # loaded as a stand-alone file
    from kicadgen.cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX
    from kicadgen.core.validation import finite_number, positive_number
    from kicadgen.services.messages import tx

#: Board edges, in the calibrated photo frame (X right, Y down, seen from above).
EDGES = ("left", "right", "top", "bottom")
#: Italian names of the edges, translated through the catalogue.
_EDGE_NAMES = {"left": "sinistro", "right": "destro", "top": "superiore", "bottom": "inferiore"}
#: How deep past the board outline the background is followed, in mm.
#: Castellated half-holes are at most a few millimetres wide, so their notch
#: is at most about half of that deep. The follow starts from the crop edge,
#: which may sit away from the outline (see prepare_photo): that slack is
#: added in pixels by the caller, so a crop wider than the board does not eat
#: into the depth that can be measured.
MAX_NOTCH_DEPTH_MM = 1.5
#: Narrowest notch counted, in mm: the smallest plated half-holes in use are
#: about 0.4-0.5 mm across. Also never below MIN_NOTCH_WIDTH_PX.
MIN_NOTCH_WIDTH_MM = 0.2
#: Narrowest notch counted, in pixels: fewer pixels are noise on any photo.
MIN_NOTCH_WIDTH_PX = 3
#: Smallest plated half-hole diameter assumed, in mm. Used ONLY to decide
#: whether a straight outline can be called «no notch»: the photo must be able
#: to show a notch this deep (half of it), otherwise the edge is «not
#: countable». A manufacturing lower bound (the smallest castellated holes
#: offered are about 0.4-0.5 mm), not a value measured on a sample board. A
#: declared diameter does NOT replace it: how deep a notch is depends on how
#: far the hole centre sits inside the board, not on the hole size (HANDOFF
#: point 140: the RS485 half-holes are cut less than halfway).
MIN_HALF_HOLE_DIAMETER_MM = 0.4
#: Width of the outline noise band, in standard deviations of the outline
#: residual. HEURISTIC, declared: the usual 3-sigma convention (a straight
#: outline stays inside it for 99.7% of the positions under Gaussian noise);
#: isolated outliers are further filtered by the minimum notch width. Never
#: below EDGE_UNCERTAINTY_PX. A per-photo noise model of the edge detector
#: would make it unnecessary.
NOISE_BAND_SIGMAS = 3.0
#: How far above the noise band a notch must reach to be counted: its depth
#: must be at least this multiple of the band. A run above the band but not
#: this deep is AMBIGUOUS and makes the edge «not countable» (never «no
#: notch»). HEURISTIC, declared: a factor-2 margin between signal and noise,
#: the smallest ratio at which «notch» and «outline wobble» are not a coin
#: toss; it is not tuned on a board (the sample notches are 4-5 bands deep).
NOTCH_SEPARATION = 2.0
#: A notch is the chord of its hole at the board edge, so it is never wider
#: than the hole: with a diameter READ from the documents, a notch wider than
#: it by more than this many times EDGE_UNCERTAINTY_PX (blur on both sides of
#: the chord) is not one of the declared half-holes. Structural, not tuned.
#: A diameter the model only ESTIMATED is never used (HANDOFF point 140).
HOLE_WIDTH_SLACK_PX = 2
#: Share of the profile used for the first, rough fit of the outline: the
#: lowest residuals. Structural: a row of half-holes whose diameter stays
#: below 0.75 x pitch (see MERGE_GAP_PITCH_FRACTION) leaves at least a quarter
#: of the edge as plain outline, so the lowest fifth is outline even on the
#: densest rows. The final fit uses every position outside the notches.
LOWER_ENVELOPE_FRACTION = 0.2
#: Gaps inside one notch (plating reflections, JPEG noise) shorter than this
#: fraction of the pitch are closed. Two neighbouring half-holes are separated
#: by pitch - diameter, which stays above it while the diameter is below
#: 0.75 x pitch, the usual range for castellated modules.
MERGE_GAP_PITCH_FRACTION = 0.25
#: Tolerance on the spacing of notch centres, as a fraction of the pitch: a
#: spacing is regular when it is an integer multiple of the pitch within it.
#: HEURISTIC, declared: it absorbs the measurement of two notch centres and
#: a small error of the declared pitch; halfway to the rounding boundary of
#: the missing-notch rule, so «regular», «missing notch» and «incompatible»
#: stay three separate bands.
PITCH_TOLERANCE = 0.25
#: A spacing of this many pitches or more between two neighbouring notches is
#: a missing notch inside the row: halfway between one pitch (neighbours) and
#: two (one skipped), the rounding boundary of the regularity rule. The count
#: is then only a lower bound.
MISSING_NOTCH_PITCHES = 1.5
#: Fewest notches that make a contact row on an edge the analysis did not
#: declare: two notches are just as well two mounting holes or a pair of
#: mouse bites; three evenly spaced notches of the same width are a row.
MIN_UNDECLARED_ROW = 3
#: Notches of one undeclared row must have similar widths: within this
#: fraction of the median width, and never tighter than two edge
#: uncertainties. HEURISTIC, declared: the plated half-holes of one row are
#: drilled with one tool, so their widths differ only by measurement; the
#: fraction matches PITCH_TOLERANCE.
WIDTH_TOLERANCE = 0.25


@dataclass
class EdgeCount:
    """@brief What the photo says about the contacts of one edge.

    @note `found` is None when the edge is not countable; `reason` then says why.
    @note `at_least` is set when the notches found are a lower bound (a notch
        of the row is missing, possibly hidden): `found` is then None.
    @note `tolerance_mm` is the uncertainty of each centre in `centres_mm`,
        from the photo scale along the edge (notch centre plus board end).
    @note `shift_bound_mm` is how far the whole row of centres may be shifted
        along the edge by the calibration alone (see row_shift_bound_mm); set
        by count_disagreements, None elsewhere.
    """

    edge: str
    found: Optional[int]
    declared: Optional[int] = None
    centres_mm: list = field(default_factory=list)
    reason: str = ""
    #: True when the outline shows no notch at all (a straight edge, on a
    #: photo able to show one), as opposed to notches that cannot be counted.
    no_notch: bool = False
    at_least: Optional[int] = None
    tolerance_mm: Optional[float] = None
    shift_bound_mm: Optional[float] = None


def _per_edge(value, edge, default):
    """Value for one edge from a scalar or an {edge: value} mapping."""
    if isinstance(value, Mapping):
        value = value.get(edge, default)
    return default if value is None else max(0, int(value))


def depth_profiles(background_mask, box, max_depth_px: Union[int, Mapping],
                   outside_px: Union[int, Mapping, None] = None) -> dict:
    """@brief Signed position of the board outline along each edge of a board box.

    @param background_mask 2-D boolean array: True where the pixel matches the
        photo background (photo_refine.estimate_background tolerance).
    @param box (x0, y0, x1, y1) of the board crop in that array, x1/y1 excluded.
    @param max_depth_px How far into the box the background is followed, in
        pixels: one number, or {edge: pixels}.
    @param outside_px How far OUT of the box the board is followed when it
        reaches the box edge, in pixels: one number, {edge: pixels}, or None
        (0: the outline is then taken at the box edge, as before point 139).
    @return {edge: list}: for left/right one value per row of the box, for
        top/bottom one per column. A value is the position of the first board
        pixel counted from the box edge: positive when background enters the
        box (a notch, or a box wider than the board), negative when the board
        runs past the box edge, capped at max_depth_px; None when the board
        reaches past `outside_px` (outline not seen).
    """
    import numpy as np
    mask = np.asarray(background_mask, dtype=bool)
    height, width = mask.shape[:2]
    x0, y0, x1, y1 = (int(value) for value in box)

    def depth_of(edge):
        return max(1, min(_per_edge(max_depth_px, edge, 1), x1 - x0, y1 - y0))

    def outside_of(edge):
        return _per_edge(outside_px, edge, 0)

    bands = {}
    d, o = depth_of("left"), outside_of("left")
    bands["left"] = (mask[y0:y1, x0:x0 + d], mask[y0:y1, max(0, x0 - o):x0][:, ::-1], d, o)
    d, o = depth_of("right"), outside_of("right")
    bands["right"] = (mask[y0:y1, x1 - d:x1][:, ::-1], mask[y0:y1, x1:min(width, x1 + o)], d, o)
    d, o = depth_of("top"), outside_of("top")
    bands["top"] = (mask[y0:y0 + d, x0:x1].T, mask[max(0, y0 - o):y0, x0:x1][::-1].T, d, o)
    d, o = depth_of("bottom"), outside_of("bottom")
    bands["bottom"] = (mask[y1 - d:y1, x0:x1][::-1].T, mask[y1:min(height, y1 + o), x0:x1].T,
                       d, o)

    profiles = {}
    for edge, (inward, outward, depth, outside) in bands.items():
        # Background pixels before the first board pixel, going inward.
        inside = np.where(inward.all(axis=1), inward.shape[1], inward.argmin(axis=1))
        values = []
        for row, count in enumerate(inside.tolist()):
            if count > 0 or outside == 0:
                values.append(int(count))
                continue
            # The board touches the box edge: follow it outward to the background.
            strip = outward[row] if outward.shape[1] else outward[row][:0]
            if strip.size and strip.any():
                values.append(-int(strip.argmax()))
            else:
                values.append(None)
        profiles[edge] = values
    return profiles


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


def _merge(runs, max_gap):
    """Join runs separated by fewer than `max_gap` positions."""
    merged = runs[:1]
    for start, stop in runs[1:]:
        if start - merged[-1][1] < max_gap:
            merged[-1] = (merged[-1][0], stop)
        else:
            merged.append((start, stop))
    return merged


def _away_from_the_ends(runs, known, min_width):
    """Runs that can be measured: not touching an end of the edge or an outline not seen.

    A run touching an end is the corner (its arc, background columns of a
    tilted crop, a notch cut by the corner). A run separated from such an end
    run by less outline than the narrowest notch counted (`min_width`) is not
    resolved from it -- the same reason a gap that short inside a notch is
    closed -- and belongs to the corner too. Nothing else is joined here: the
    first notch of a row stands on its own however close to the corner.
    """
    n = len(known)

    def touches(start, stop):
        return start == 0 or stop >= n or not known[start - 1] or not known[stop]

    runs = list(runs)
    keep = [not touches(start, stop) for start, stop in runs]
    for order in (range(len(runs)), range(len(runs) - 1, -1, -1)):
        previous = None
        for index in order:
            if not keep[index]:
                previous = index
            elif previous is not None and min(abs(runs[index][0] - runs[previous][1]),
                                              abs(runs[previous][0] - runs[index][1]))                     < min_width:
                keep[index] = False
                previous = index
            else:
                break
    return [run for run, kept in zip(runs, keep) if kept]


def _mad_sigma(values):
    """Robust standard deviation (1.4826 x median absolute deviation)."""
    import numpy as np
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(values - np.median(values))))


def _outline_baseline(values, known, window):
    """Outline position per index, fitted on the outline between the notches.

    Two passes. The first fits a line to the LOWER ENVELOPE of the profile (the
    lowest LOWER_ENVELOPE_FRACTION of the residuals, iterated): notches only
    ever push the profile up, so the envelope is outline whatever share of the
    edge they cover. The second excludes everything the first sees above the
    noise band and fits the outline itself: a line (a straight board edge is a
    straight line in any perspective view), plus the running median of the
    outline residual over `window` positions, which follows what a line does
    not (lens distortion, a slightly bowed crop).

    @return (baseline array, outline mask, noise sigma in px), or None when
        too little outline is visible.
    """
    import numpy as np
    n = len(values)
    x = np.arange(n, dtype=float)
    if known.sum() < 3:
        return None
    # Rumore del contorno dalle differenze fra posizioni vicine: robusto anche
    # con le tacche (pochi salti) e indipendente dall'inclinazione.
    pairs = known[1:] & known[:-1]
    diffs = np.diff(values)[pairs]
    sigma_diff = _mad_sigma(diffs) / math.sqrt(2.0) if diffs.size else 0.0
    keep = known.copy()
    coef = np.polyfit(x[keep], values[keep], 1)
    for _ in range(12):
        residual = values - np.polyval(coef, x)
        cut = np.quantile(residual[known], LOWER_ENVELOPE_FRACTION)
        new_keep = known & (residual <= cut)
        if new_keep.sum() < 2:
            break
        coef = np.polyfit(x[new_keep], values[new_keep], 1)
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    # La linea dell'inviluppo inferiore sta sotto il contorno di circa il
    # rumore: la si riporta sul contorno con la mediana dei residui bassi.
    residual = values - np.polyval(coef, x)
    band = max(EDGE_UNCERTAINTY_PX, NOISE_BAND_SIGMAS * sigma_diff)
    level = np.median(residual[known & (residual <= np.quantile(residual[known], 0.5))])
    above = known & (residual - level > band)
    grow = int(math.ceil(EDGE_UNCERTAINTY_PX))
    excluded = above.copy()
    for shift in range(1, grow + 1):
        excluded[shift:] |= above[:-shift]
        excluded[:-shift] |= above[shift:]
    outline = known & ~excluded
    if outline.sum() < max(3, LOWER_ENVELOPE_FRACTION * known.sum()):
        return None
    coef = np.polyfit(x[outline], values[outline], 1)
    line = np.polyval(coef, x)
    residual = values - line
    # Correzione locale: mediana mobile del residuo del SOLO contorno.
    half = max(2, int(window) // 2)
    step = max(1, half // 2)
    anchors, offsets = [], []
    for centre in range(0, n, step):
        lo, hi = max(0, centre - half), min(n, centre + half + 1)
        local = residual[lo:hi][outline[lo:hi]]
        if local.size >= 3:
            anchors.append(centre)
            offsets.append(float(np.median(local)))
    correction = (np.interp(x, anchors, offsets) if anchors else np.zeros(n))
    baseline = line + correction
    sigma = max(sigma_diff, _mad_sigma((values - baseline)[outline]))
    return baseline, outline, sigma


def count_notches(profile: Sequence, px_per_mm: float, pitch_mm: Optional[float] = None, *,
                  across_px_per_mm: Optional[float] = None,
                  hole_diameter_mm: Optional[float] = None,
                  declared_count: Optional[int] = None) -> EdgeCount:
    """@brief Count the notches of one edge profile.

    @param profile Signed outline position per position along the edge
        (depth_profiles); None where the outline was not seen.
    @param px_per_mm Scale of the photo ALONG this edge (widths, positions).
    @param pitch_mm Declared contact pitch of the edge, or None when the edge
        declares no contacts (then at least MIN_UNDECLARED_ROW notches of
        similar width, evenly spaced among themselves, are required).
    @param across_px_per_mm Scale ACROSS this edge (depths); None = the same
        as along (an isotropic photo).
    @param hole_diameter_mm Half-hole diameter of the side MEASURED in the
        documents, or None (an estimate must not be passed: see
        `_measured_diameter`). It only bounds the WIDTH of a notch (a chord of
        the hole cannot be wider than the hole); it sets no minimum depth,
        because the depth depends on where the hole centre sits (point 140).
    @param declared_count Number of contacts the side declares, or None. With
        a declared pitch, a single notch is a row only when this is 1.
    @return EdgeCount with `edge` left empty: `found` and the notch centres in
        mm from the start of the edge; or `found` None with the reason, and
        `no_notch` only when the outline is straight on a photo that could
        show a notch; `at_least` when the notches seen are only a lower bound:
        a notch of the row is missing, or the row may go on into a stretch of
        the edge that cannot be measured (see _row_may_continue).
    @note Thresholds, all from the data: the outline (lower envelope, see
        _outline_baseline) is the reference; the noise band is
        max(EDGE_UNCERTAINTY_PX, NOISE_BAND_SIGMAS x outline sigma); a notch
        rises above the band over at least max(MIN_NOTCH_WIDTH_PX,
        MIN_NOTCH_WIDTH_MM) and at most one pitch, does not touch either end
        of the edge or an outline not seen (a rounded board corner is a notch
        too: such runs are set aside BEFORE neighbouring runs are joined) and
        reaches NOTCH_SEPARATION bands (and half the declared radius) deep; a
        run above the band but not that deep makes the edge not countable.
    @note A declared side (pitch given) needs two notches or more, unless it
        declares one contact; an undeclared one MIN_UNDECLARED_ROW of similar
        width.
    """
    import numpy as np
    scale = positive_number(px_per_mm)
    across = positive_number(across_px_per_mm) or scale
    raw = list(profile) if profile is not None else []
    if scale is None or len(raw) < 3:
        return EdgeCount("", None, reason=tx("foto non calibrata"))
    values = np.array([np.nan if finite_number(v) is None else float(v) for v in raw])
    known = ~np.isnan(values)
    n = len(values)
    tolerance_mm = round(2 * EDGE_UNCERTAINTY_PX / scale, 3)
    pitch = positive_number(pitch_mm)
    diameter = positive_number(hole_diameter_mm)
    if known.sum() < n / 2:
        return EdgeCount("", None, reason=tx("contorno della scheda non visibile su questo bordo"),
                         tolerance_mm=tolerance_mm)
    filled = np.where(known, values, np.nanmedian(values))
    window = 2 * pitch * scale if pitch is not None else n / 4
    fitted = _outline_baseline(filled, known, window)
    if fitted is None:
        return EdgeCount("", None, reason=tx("contorno della scheda non visibile su questo bordo"),
                         tolerance_mm=tolerance_mm)
    baseline, _outline, sigma = fitted
    residual = np.where(known, filled - baseline, 0.0)
    band = max(EDGE_UNCERTAINTY_PX, NOISE_BAND_SIGMAS * sigma)
    required = NOTCH_SEPARATION * band
    # Nessuna profondita' minima ricavata dal diametro (punto 140): la tacca e'
    # profonda quanto il foro sporge dal bordo, che dipende da dove cade il suo
    # centro, non dalla sua misura. Il diametro letto limita solo la LARGHEZZA.
    widest = (diameter * scale + HOLE_WIDTH_SLACK_PX * EDGE_UNCERTAINTY_PX
              if diameter is not None else None)
    flags = (residual >= band) & known
    # Corse sopra la banda che toccano un'estremita' del bordo o un tratto
    # non visto (l'arco di un angolo arrotondato, le colonne di sfondo di un
    # ritaglio inclinato, una tacca tagliata dall'angolo) non si misurano. Si
    # mettono da parte PRIMA di unire le corse vicine: unite dopo, una o due
    # colonne di sfondo all'angolo si fondevano con la prima tacca e la
    # facevano sparire con loro (punto 139, R-G1). Restano segnate in `flags`:
    # _row_may_continue non le prende per contorno pulito.
    min_width = max(MIN_NOTCH_WIDTH_PX, MIN_NOTCH_WIDTH_MM * scale)
    inner = _away_from_the_ends(_runs(flags.tolist()), known, min_width)
    max_gap = max(MIN_NOTCH_WIDTH_PX, MERGE_GAP_PITCH_FRACTION * pitch * scale
                  if pitch is not None else 0)
    runs = _merge(inner, max_gap)
    # A notch is one half-hole: never wider than the pitch between two of them.
    max_width = pitch * scale if pitch is not None else None
    notches, ambiguous, too_wide = [], [], []
    for start, stop in runs:
        width = stop - start
        if max_width is not None and width > max_width:
            # Not one half-hole (a cut-out, a connector); still flagged, so it
            # never proves that a notch is absent either.
            continue
        if width < min_width:
            continue
        peak = float(residual[start:stop].max())
        if widest is not None and width > widest and peak >= required:
            # Troppo larga per essere il semiforo dichiarato: non si conta
            # come tale, e non prova nemmeno che non ci sia.
            too_wide.append((start, stop))
            continue
        (notches if peak >= required else ambiguous).append((start, stop))
    if ambiguous:
        return EdgeCount("", None, tolerance_mm=tolerance_mm, reason=tx(
            "tacche poco distinte dal rumore del contorno"))
    if too_wide:
        return EdgeCount("", None, tolerance_mm=tolerance_mm, reason=tx(
            "tacche piu' larghe del semiforo dichiarato nei documenti"))
    if not notches:
        smallest = MIN_HALF_HOLE_DIAMETER_MM / 2 * across
        if smallest < NOTCH_SEPARATION * band:
            # Un semiforo cosi' piccolo non si distinguerebbe dal rumore: il
            # bordo dritto non prova che le tacche non ci sono.
            return EdgeCount("", None, tolerance_mm=tolerance_mm, reason=tx(
                "foto troppo poco definita o contorno troppo irregolare per vedere i semi-fori"))
        return EdgeCount("", None, reason=tx("nessuna tacca visibile sul bordo"), no_notch=True,
                         tolerance_mm=tolerance_mm)
    centres_px = [(start + stop) / 2 for start, stop in notches]
    centres = [round(c / scale, 3) for c in centres_px]
    if pitch is not None and len(notches) < 2 and declared_count != 1:
        # Una sola tacca non e' una fila: la piazzola di un foro intero, un
        # segno di fresatura o un foro di fissaggio la fanno uguale. Vale solo
        # se il lato dichiara proprio un contatto (punto 139, R-M2).
        return EdgeCount("", None, centres_mm=centres, tolerance_mm=tolerance_mm,
                         reason=tx("una sola tacca: non basta per riconoscere una fila di "
                                   "contatti"))
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    unit = pitch if pitch is not None else (min(gaps) if gaps else None)
    missing = False
    multiples = []
    if unit is not None:
        for gap in gaps:
            multiple = round(gap / unit)
            if multiple < 1 or abs(gap - multiple * unit) > PITCH_TOLERANCE * unit:
                return EdgeCount("", None, centres_mm=centres, tolerance_mm=tolerance_mm,
                                 reason=tx("tacche a distanze non compatibili con il passo"))
            if gap >= MISSING_NOTCH_PITCHES * unit:
                missing = True
            multiples.append(multiple)
    if pitch is None:
        # Checked before the missing-notch and end rules, so that a lower
        # bound on an undeclared edge is always a row of similar notches (see
        # check_declared_contacts).
        widths = [stop - start for start, stop in notches]
        middle = float(np.median(widths))
        spread = max(2 * EDGE_UNCERTAINTY_PX, WIDTH_TOLERANCE * middle)
        if len(centres) < MIN_UNDECLARED_ROW:
            return EdgeCount("", None, centres_mm=centres, tolerance_mm=tolerance_mm,
                             reason=tx("troppo poche tacche per una fila di contatti"))
        if any(abs(width - middle) > spread for width in widths):
            return EdgeCount("", None, centres_mm=centres, tolerance_mm=tolerance_mm,
                             reason=tx("tacche di larghezze diverse"))
    if missing:
        # Un buco nella fila e' una tacca che manca o che non si vede (una
        # freccia di quota, polvere, un'ombra): il numero e' solo un minimo e
        # non puo' smentire ne' correggere il conteggio dichiarato.
        return EdgeCount("", None, centres_mm=centres, at_least=len(centres),
                         tolerance_mm=tolerance_mm, reason=tx(
                             "manca una tacca nella fila, forse nascosta: sulla foto se ne "
                             "contano almeno {found}", found=len(centres)))
    if unit is not None and _row_may_continue(notches, centres_px, multiples, unit * scale,
                                              known & ~flags):
        # Lo stesso vale in testa e in coda alla fila (punto 139, R-G1/R-M3).
        return EdgeCount("", None, centres_mm=centres, at_least=len(centres),
                         tolerance_mm=tolerance_mm, reason=tx(
                             "la fila puo' continuare in un tratto del bordo non misurabile "
                             "(un angolo, il contorno non visto): sulla foto se ne contano "
                             "almeno {found}", found=len(centres)))
    return EdgeCount("", len(centres), centres_mm=centres, tolerance_mm=tolerance_mm)


def _row_may_continue(notches, centres_px, multiples, unit_px, clean) -> bool:
    """True when a notch may sit, unseen, just before the first or after the last one counted.

    The count of a row is valid only when the photo PROVES that the row ends
    where the counting ends. The next notch of the row would be centred one
    spacing past the first (last) counted one, within a guard of two edge
    uncertainties (the extrapolated centre comes from measured centres). The
    notches of one row are cut by one tool, so that notch would cover at
    least its CORE: the narrowest notch counted, less the guard on each side.
    The slot is proven empty when
    - the core lies past the end of the edge (no half-hole of this edge can
      sit there), or
    - some position of the core is plain, measured outline: a notch there
      would be above the noise band over its whole core.
    Otherwise -- the core falls entirely on a run touching the end (the corner
    arc, background columns of a tilted crop), on an outline not seen, or on a
    cut-out wider than the pitch -- the row may continue there unseen.
    When the notches are too narrow to have a core (half-width within the
    guard), the whole slot (widest notch plus the guard) must be plain outline.

    The spacing is the one measured on the counted notches (their span over
    the pitches it holds) when there are two or more, else `unit_px`.
    @param clean Boolean array: outline seen and below the noise band.
    @note Structural, no tuned value: slot, core and guard come from the
        notches measured and from EDGE_UNCERTAINTY_PX. A notch fully covered
        by dust or an arrow on PLAIN outline is not seen by this rule: that
        case is not guessed (HANDOFF point 142): it becomes a disagreement
        with the declared count, i.e. one recount and at most a warning.
    """
    n = len(clean)
    if len(centres_px) >= 2 and sum(multiples) > 0:
        spacing = (centres_px[-1] - centres_px[0]) / sum(multiples)
    else:
        spacing = unit_px
    guard = 2 * EDGE_UNCERTAINTY_PX
    widths = [stop - start for start, stop in notches]
    core = min(widths) / 2 - guard
    for slot in (centres_px[0] - spacing, centres_px[-1] + spacing):
        if core > 0:
            lo = max(0, int(math.ceil(slot - core)))
            hi = min(n, int(math.floor(slot + core)) + 1)
            if lo >= hi:
                continue   # oltre la fine del bordo: li' non c'e' un semiforo di questo lato
            if not clean[lo:hi].any():
                return True
            continue
        if slot < 0 or slot >= n:
            continue
        half = max(widths) / 2 + guard
        lo, hi = max(0, int(math.floor(slot - half))), min(n, int(math.ceil(slot + half)))
        if not clean[lo:hi].all():
            return True
    return False


def _edge_name(edge: str) -> str:
    return tx(_EDGE_NAMES.get(edge, edge))


def _scales(crop_shape, length_mm, width_mm) -> dict:
    """(along, across) px/mm for each edge of the calibrated crop.

    Left and right run along Y, so their positions use the Y scale and their
    depths the X scale; top and bottom the other way round. The crop is
    calibrated axis by axis, so the two may differ.
    """
    height_px, width_px = crop_shape[:2]
    sx, sy = width_px / length_mm, height_px / width_mm
    return {"left": (sy, sx), "right": (sy, sx), "top": (sx, sy), "bottom": (sx, sy)}


def _integer(value) -> Optional[int]:
    """A positive whole number from an int, a whole float or a numeric string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    number = positive_number(value)
    return int(number) if number is not None and number == int(number) else None


def _positive(value) -> Optional[float]:
    """A positive finite number from a number or a numeric string."""
    return positive_number(_numeric(value))


def _numeric(value):
    """A number from a number or a numeric string; anything else unchanged."""
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return value


def _measured_diameter(entry) -> Optional[float]:
    """Half-hole diameter of a side only when it was measured, never an estimate.

    The model marks each length in the side's `value_sources`; the window marks
    a side value with `half_hole_diameter_declared`. An ESTIMATED diameter (the
    model guessed 1.7-2.4 mm on the RS485, whose notches are 0.3-0.4 mm deep)
    made every real notch «not countable» (HANDOFF point 140): it is ignored.
    """
    value = _positive(entry.get("half_hole_diameter_mm")
                      if entry.get("half_hole_diameter_mm") is not None
                      else entry.get("hole_diameter_mm"))
    if value is None:
        return None
    sources = entry.get("value_sources")
    source = sources.get("half_hole_diameter_mm") if isinstance(sources, dict) else None
    if source in ("read", "derived") or entry.get("half_hole_diameter_declared") is True:
        return value
    return None


def normalized_sides(sides) -> dict:
    """@brief Declared castellated sides, keyed by edge, in comparable form.

    @param sides Raw castellated sides as the model wrote them (after unit and
        view canonicalization): count and pitch may be numeric strings, the
        edge may be capitalised or padded.
    @return {edge: {"count", "pitch_mm", "offset_mm", "contact_style",
        "hole_diameter_mm"}}; an invalid count or pitch becomes None, a
        missing or invalid offset 0 (the answer contract: omitted means a
        centred row). An edge listed twice is left out: nothing says which
        description to trust (the extraction refuses such an answer,
        datasheet_extractor._normalized_castellated_sides).
    @note Same reading as the extractor's normalization, kept local so that
        this module does not import the extractor.
    """
    result, repeated = {}, set()
    for entry in sides if isinstance(sides, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        edge = str(entry.get("edge") or "").strip().lower()
        if edge not in EDGES:
            continue
        if edge in result:
            repeated.add(edge)
            continue
        style = entry.get("contact_style")
        offset = entry.get("offset_mm") if entry.get("offset_mm") is not None \
            else entry.get("offset")
        result[edge] = {
            "count": _integer(entry.get("count")),
            "pitch_mm": _positive(entry.get("pitch_mm")),
            "offset_mm": finite_number(_numeric(offset)) or 0.0,
            "contact_style": str(style).strip().lower() if isinstance(style, str) else None,
            "hole_diameter_mm": _measured_diameter(entry),
        }
    for edge in repeated:
        result.pop(edge, None)
    return result


def row_shift_bound_mm(crop_shape, along_px_per_mm: float) -> Optional[float]:
    """@brief How far the calibration alone may shift a whole contact row along an edge, in mm.

    Notch centres are measured from the edge of the crop box, and the box is
    the bounding box of a board that find_board accepts with a residual tilt:
    a board corner may then sit inside the box edge by up to the drift that
    find_board allows (photo_refine: max(MAX_BOARD_EDGE_DRIFT_PX,
    MAX_BOARD_EDGE_DRIFT_RATIO x the long side of the box)), and the box edge
    itself is located within EDGE_UNCERTAINTY_PX. Their sum, in mm along the
    edge, is the common shift between the true and the measured positions of
    every notch of the row.

    @param crop_shape Shape (height, width, ...) of the calibrated crop, px.
    @param along_px_per_mm Scale of the photo along the edge.
    @return The bound in mm, or None when the scale is not valid.
    @note Worst case of the calibration contract, not a measure of this photo:
        a photo shot square gets the same bound as one at the tilt limit. The
        declared offset of the row adds nothing: the schema carries no
        tolerance for it, an omitted offset means a centred row by the answer
        contract, and a dimension read from a drawing is far more precise than
        this bound. A measured tilt of the crop would make the bound tighter.
    """
    scale = positive_number(along_px_per_mm)
    if scale is None:
        return None
    try:
        from .photo_refine import MAX_BOARD_EDGE_DRIFT_PX, MAX_BOARD_EDGE_DRIFT_RATIO
    except ImportError:  # loaded as a stand-alone file
        from kicadgen.services.photo_refine import (MAX_BOARD_EDGE_DRIFT_PX,
                                                    MAX_BOARD_EDGE_DRIFT_RATIO)
    long_px = max(int(v) for v in crop_shape[:2])
    drift = max(MAX_BOARD_EDGE_DRIFT_PX, MAX_BOARD_EDGE_DRIFT_RATIO * long_px)
    return round((drift + EDGE_UNCERTAINTY_PX) / scale, 3)


def row_offset_from(declared_mm: Sequence[float], measured_mm: Sequence[float],
                    tolerance_mm: float, shift_bound_mm: float) -> Optional[float]:
    """@brief Common shift that puts declared contact centres on measured notch centres.

    @param declared_mm Declared centres along the edge, mm, in order.
    @param measured_mm Measured notch centres, same edge and origin, in order.
    @param tolerance_mm How far each centre may stay off once the common shift
        is removed (the spacing must match).
    @param shift_bound_mm Largest common shift admitted (row_shift_bound_mm).
    @return The shift (measured - declared) in mm, or None when the numbers of
        centres differ, the spacing does not match within the tolerance, or
        the shift exceeds the bound.
    @note The shift minimizes the largest residual (midrange of the
        differences), so a single centre off by more than the tolerance is
        never hidden by the others.
    """
    if len(declared_mm) != len(measured_mm) or not declared_mm:
        return None
    diffs = [float(m) - float(d) for d, m in zip(declared_mm, measured_mm)]
    shift = (max(diffs) + min(diffs)) / 2
    if (max(diffs) - min(diffs)) / 2 > tolerance_mm or abs(shift) > shift_bound_mm:
        return None
    return shift


def _count_side(profile, scales, side, edge_length_mm=None, shift_bound_mm=None) -> EdgeCount:
    """Count one declared side, a lower bound when the declared row explains a shortfall."""
    along, across = scales
    counted = count_notches(profile, along, side.get("pitch_mm"), across_px_per_mm=across,
                            hole_diameter_mm=side.get("hole_diameter_mm"),
                            declared_count=side.get("count"))
    # Nessuna ipotesi di semifori nascosti su contorno misurato liscio (punto
    # 142): count_notches da' un numero valido solo dopo aver visto liscio il
    # posto successivo al primo e all'ultimo semiforo, e presumerli coperti
    # impediva di smentire un modello che contava troppo (6 dichiarati per 4
    # semifori veri). Un semiforo davvero coperto diventa un disaccordo: un
    # ricontrollo e, se il modello conferma, un avviso; mai un dato sbagliato.
    return counted


def _edge_lengths(length_mm, width_mm) -> dict:
    """Length of each edge in mm: top/bottom run along X, left/right along Y."""
    return {"left": width_mm, "right": width_mm, "top": length_mm, "bottom": length_mm}


def count_disagreements(profiles, crop_shape, length_mm: float, width_mm: float,
                        sides) -> list[EdgeCount]:
    """@brief Declared sides whose half-holes the photo counts differently.

    @param profiles depth_profiles of the calibrated crop, or None.
    @param crop_shape Shape of the calibrated crop, in px.
    @param length_mm Board length along X, in mm.
    @param width_mm Board width along Y, in mm.
    @param sides Declared castellated sides, seen from above, in mm (raw:
        normalized here, see normalized_sides).
    @return One EdgeCount per disagreeing side (`found`, `declared`, notch
        centres in mm from the start of the edge: left to right for top and
        bottom, top to bottom for left and right, their tolerance, and the
        common shift the calibration allows, `shift_bound_mm`). Only sides
        declared with half-holes (contact_style "half_hole") are compared:
        full holes do not cut the outline, and a side whose style is not
        declared may be full holes whose pads make small bumps on it (the
        ESP32 sample), so it is only reported by check_declared_contacts,
        never sent to a paid recount. Sides not countable on the photo
        (including a row that is only a lower bound) never disagree.
    @note Used to ask the model to recount those sides (user decision
        2026-09-28, HANDOFF point 137); the count is the same as the warning
        of `check_declared_contacts`.
    """
    if not profiles or not isinstance(sides, (list, tuple)):
        return []
    scales = _scales(crop_shape, length_mm, width_mm)
    lengths = _edge_lengths(length_mm, width_mm)
    result = []
    for edge, side in normalized_sides(sides).items():
        if side["contact_style"] != "half_hole" or edge not in profiles:
            continue
        if side["count"] is None:
            continue
        bound = row_shift_bound_mm(crop_shape, scales[edge][0])
        counted = _count_side(profiles[edge], scales[edge], side, lengths[edge], bound)
        if counted.found is not None and counted.found != side["count"]:
            result.append(EdgeCount(edge, counted.found, side["count"], counted.centres_mm,
                                    tolerance_mm=counted.tolerance_mm, shift_bound_mm=bound))
    return result


def check_declared_contacts(profiles, crop_shape, length_mm: float, width_mm: float,
                            sides) -> list[tuple[str, str]]:
    """@brief Compare the declared contacts per edge with the notches on the photo.

    @param profiles depth_profiles of the calibrated crop, or None.
    @param crop_shape Shape (height, width, ...) of the calibrated crop, in px.
    @param length_mm Board length along X, in mm.
    @param width_mm Board width along Y, in mm.
    @param sides Declared castellated sides (dicts with edge, count, pitch_mm,
        contact_style, optional half_hole_diameter_mm), in mm and seen from
        above as the photo is; normalized here (see normalized_sides).
    @return (kind, message) lines, kind "warning" for a disagreement the user
        must check (another count, a side the analysis left out, declared
        half-holes on an edge with no notch at all), "log" for a confirmation
        or a side that cannot be counted. Empty when there is nothing to
        compare.
    @note Only reports: the declared contacts are never changed here.
    """
    if not profiles or not isinstance(sides, (list, tuple)):
        return []
    scales = _scales(crop_shape, length_mm, width_mm)
    declared = normalized_sides(sides)
    listed = {str(entry.get("edge") or "").strip().lower()
              for entry in sides if isinstance(entry, dict)}
    lines = []
    for edge in EDGES:
        side = declared.get(edge)
        profile = profiles.get(edge)
        if profile is None:
            continue
        if side is None:
            if edge in listed:
                continue      # lato elencato due volte: nessun confronto possibile
            along, across = scales[edge]
            result = count_notches(profile, along, None, across_px_per_mm=across)
            if result.found is not None:
                lines.append(("warning", tx(
                    "Lato {edge}: sulla foto si contano {found} semi-fori, ma l'analisi non "
                    "dichiara contatti su questo lato: da verificare.",
                    edge=_edge_name(edge), found=result.found)))
            elif result.at_least is not None:
                # A lower bound on an undeclared edge is still a checked row
                # (count_notches validates it before the lower-bound rules).
                lines.append(("warning", tx(
                    "Lato {edge}: sulla foto si contano almeno {found} semi-fori, ma l'analisi "
                    "non dichiara contatti su questo lato: da verificare.",
                    edge=_edge_name(edge), found=result.at_least)))
            continue
        count = side["count"]
        if side["contact_style"] == "through_hole":
            lines.append(("log", tx(
                "Lato {edge}: contatti a foro intero, non contabili dalla sagoma della foto.",
                edge=_edge_name(edge))))
            continue
        result = _count_side(profile, scales[edge], side, _edge_lengths(length_mm, width_mm)[edge],
                             row_shift_bound_mm(crop_shape, scales[edge][0]))
        if result.no_notch and side["contact_style"] == "half_hole":
            # Declared half-holes cut the outline; a straight edge, on a photo
            # able to show them, contradicts the side (user decision
            # 2026-09-28: a warning, nothing changed).
            lines.append(("warning", tx(
                "Lato {edge}: l'analisi dichiara {declared} contatti a semi-foro, ma sulla foto "
                "il bordo non ha nessuna tacca: lato da verificare.",
                edge=_edge_name(edge), declared=count)))
        elif result.found is None or count is None:
            lines.append(("log", tx("Lato {edge}: contatti non contabili sulla foto ({reason}).",
                                    edge=_edge_name(edge),
                                    reason=result.reason or tx("numero dichiarato non valido"))))
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
