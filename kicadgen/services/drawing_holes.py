"""@brief Local, deterministic check of a pad-layout reading against the hole circles of a drawing.

@details The vision model reads pad positions from a dimensioned drawing by
transcribing dimension chains; the program sums them. Those readings are often
wrong (a quote taken from the wrong chain, two quotes exchanged, a row read on
the wrong side). This module checks such a reading against the hole circles
actually drawn in the image, without any AI and without any network access:

1. the image is read (unreadable files are skipped) and reduced to an ink mask
   relative to its own paper and ink levels;
2. photographs are refused by a paper/ink gate: they are not line drawings and
   their round features are not drawn holes;
3. small drawn circles are found by a ring matched filter over a scale
   pyramid, refined on the ink and filtered against text glyphs;
4. the reading (pad centres in mm, page orientation) is matched to the
   circles by similarity hypotheses built on its longest baselines;
5. "confirmed" means a displacement test: mapped by a similarity (uniform
   scale, rotation, translation, never a mirror) fitted on the other pads,
   every pad lies within the tolerance of its circle, and the stretch or
   shear an affine fit would add, expressed as a displacement over the
   reading's shorter extent, stays within the same tolerance. A reading
   that only fits after a stretch is "distorted";
6. optionally the outline rectangle drawn around the matched holes is
   compared with the body size of the reading, also as a displacement.

Every image analysis (photo gate, ink mask, circles) is cached per file, so
checking several competing readings against one page detects once.

Asymmetry of the verdict: any circle may help CONFIRM a reading, but a
reading is reported NOT CONFIRMED only when the image holds at least as many
circles drawn like holes (centre or extension line through them, or standing
alone on paper) as the reading has pads. Otherwise the image cannot tell a
wrong reading from a page whose circles are letters, and the answer is
"unverifiable" with reason "too_few_circles".

@note This is a CHECK, never a measurement: no millimetre value is produced
    from pixels to replace the quotes. It answers only whether the given set
    of pad centres fits the drawn circles.
@note What it cannot see: pad NUMBERS (two pads with exchanged numbers form
    the same point set), a reading that omits pads (extra circles are
    allowed), errors smaller than the tolerance, a whole axis rescaled
    consistently when no body size is given, a mirrored reading of a
    mirror-symmetric layout (same point set), anisotropy and shear of a
    single-row reading, an error that a coincidental set of other circles
    on the page happens to fit (glyph counters are filtered, not excluded),
    a drawing not to scale (the drawn proportions confirm, the quoted ones
    are "distorted"), and on a page with both a bottom and a mirrored top
    view, which view the reading was taken from (both a reading and its
    mirror can confirm).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import math
from pathlib import Path
import threading
from typing import Mapping, Sequence

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Declared limits and constants
# ---------------------------------------------------------------------------

#: Image suffixes read by the check; any other file is skipped.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

#: Images whose long side exceeds this are downscaled before the circle
#: search. 2000 px keeps a 1 mm hole drawn at the usual 8-20 px/mm of a
#: datasheet page at several pixels of radius, while bounding the cost of the
#: matched filter (linear in the pixel count).
MAX_LONG_SIDE_PX = 2000

#: Readings with fewer pads are not checked (unverifiable "too_few_pads"):
#: two pads fix scale, rotation and position of the fit, so any two circles
#: in the right direction would "confirm" them; a third pad is the first
#: one that can disagree.
MIN_PADS = 3

#: A reading whose second singular value is below this fraction of the first
#: is treated as one row of pads.
COLLINEAR_RATIO = 1e-3

#: An affine fit (used to call a failed reading "distorted" and to bound the
#: extra displacement of a confirmed one) needs at least this many pads:
#: three pads determine an affine map exactly, so it would explain anything.
AFFINE_MIN_PADS = 4

#: ... and a spread across the main direction of at least this fraction
#: (second / first singular value). Below it the cross-row scale of an
#: affine fit is set by sub-pixel offsets: a single row whose pads zigzag by
#: +-0.127 mm has 0.02, a 2x20 header with 2.54 mm rows 0.09. Declared
#: heuristic; it only decides "distorted" versus "no_match", never a
#: confirmation.
AFFINE_MIN_SPREAD = 0.05

#: Declared bound: the tolerance must not exceed this fraction of the
#: reading's smallest centre-to-centre distance, otherwise the answer is
#: unverifiable ("low_resolution"). The typical wrong reading puts a pad
#: half a spacing away (between two holes); with the tolerance at most a
#: quarter of the spacing, that error is at least twice the tolerance.
LOW_RESOLUTION_FRACTION = 0.25

#: Readings with more pads are not checked (unverifiable "too_many_pads").
#: 100 covers 2x40 headers and DIP/SIP parts with margin; at 100 pads one
#: check of a 1900x1200 page took about 6 s on a laptop (40 pads: about 3 s).
MAX_PADS = 100

#: At most this many circles per image take part in the matching, kept by
#: decreasing ring coverage. A drawing page of one part shows far fewer holes;
#: the cap only bounds the cost on pages crowded with circle-like text.
MAX_CIRCLES = 300

#: Number of the longest pad-to-pad baselines of the reading used to build
#: similarity hypotheses. For a correct reading any one baseline whose two
#: pads sit on drawn circles suffices; three bound the O(B * C^2) search.
BASELINES = 3

#: Ink threshold as a fraction of the page's paper-to-ink contrast: a pixel is
#: ink when darker than paper - 0.35 * (paper - ink). It keeps the faint,
#: anti-aliased thin lines of low-resolution scans that Otsu (about 0.5) drops,
#: while JPEG ringing on white paper stays far below 35 % of the contrast.
INK_LEVEL = 0.35

#: Grey levels below the paper level (90th percentile) still counted as paper
#: by the photo gate.
PAPER_BAND = 20

#: Declared heuristic (photo gate): a line drawing is mostly paper. Measured on
#: the sample pages of the repository: drawings 0.92-0.95 of paper and
#: 0.03-0.07 of ink; product photos with dimension arrows drawn over them
#: 0.66-0.72 of paper and 0.19-0.28 of ink. A colour/texture classifier would
#: make these two numbers unnecessary.
PAPER_FRACTION_MIN = 0.85

#: Declared heuristic (photo gate), see :data:`PAPER_FRACTION_MIN`.
INK_FRACTION_MAX = 0.12

#: Smallest circle radius searched, px: below that a circle cannot be told
#: from a dot or a letter counter.
R_MIN_PX = 2.5

#: Largest circle radius searched, as a fraction of the page short side:
#: holes, not round bodies or frames.
R_MAX_FRACTION = 0.04

#: Radii searched per pyramid level (level pixels). Larger circles are
#: searched on the page halved once more, so the matched-filter kernels stay
#: small and the cost does not grow with the radius.
LEVEL_R_HIGH = 12.0

#: Minimum fraction of a ring that must be inked (+-1 px): a drawn circle is
#: closed; 15 % tolerates JPEG dropout and arrows touching it.
COVERAGE_MIN = 0.85

#: Maximum spread of the per-direction ink radius divided by the radius:
#: elongated glyph bowls exceed 0.15, drawn circles stay well below.
ROUND_TOLERANCE = 0.15

#: Declared heuristic: minimum fraction of directions with paper just inside
#: AND just outside the ring. Two full centre lines (four arms) spoil up to
#: about 40 % of the directions on a 4 px hole (measured 0.35 on the relay
#: drawing); solid fills and photo texture spoil more than 80 % (measured on
#: the ESP32 and RS485 product photos before the photo gate existed). A
#: vector source (PDF drawing with explicit circles) would make it
#: unnecessary.
PAPER_SIDES_MIN = 0.5

#: Work resolution factor of the sub-pixel ring tests: drawn holes are often
#: only 8-12 px wide on a page scan.
UPSCALE = 4

#: Hypothesis gate, px: a projected pad must fall within this distance of a
#: circle (about 1 px of centre noise plus drafting inaccuracy) ...
HYPOTHESIS_TOL_PX = 3.0

#: ... or within this fraction of the pad's distance from the nearer pad of
#: the baseline (whose two pads sit exactly on circles). It absorbs the
#: anisotropy of a scanned page (0.2-1.6 % measured on the sample drawings)
#: with margin. It only proposes assignments: the verdict is the fit's.
HYPOTHESIS_TOL_SPAN = 0.06

#: Two circles are "of about the same size" when their radii differ by less
#: than this factor; the hypothesis search prefers such circles. Declared
#: heuristic: pin-label glyph counters next to holes were half the hole radius
#: on the synthetic drawings and 0.6-1.1 of it on the sample pages, and pins
#: of one footprint are usually drawn with one or two close diameters.
#: Mounting holes three times larger are still reached as a fallback. An
#: explicit hole diameter in the reading would make it unnecessary.
SIMILAR_RADIUS = 1.6

#: Gate fraction of the second pass, run only when nothing confirmed: wide
#: enough for a drawing not to scale by about a third (a DIP whose 7.62 mm
#: rows are drawn at 10.16 mm, the audit case it was chosen on) to reach the
#: affine fit and be reported "distorted" instead of "no_match". Declared
#: heuristic: an affine hypothesis search from three correspondences would
#: make it unnecessary, at a much higher cost.
DISTORTION_GATE = 0.35

#: Final tolerance floor, mm, and its pixel term: tolerance =
#: max(0.15 mm, 3 px / scale). 3 px = about 1 px of centre noise plus the
#: drafting inaccuracy measured on the sample drawings (residuals
#: 0.02-0.13 mm = 0.3-1 px).
MIN_TOLERANCE_MM = 0.15
TOLERANCE_PX = 3.0

#: Maximum rotation, degrees: a page skewed more than that should be
#: deskewed first, not absorbed by the fit.
MAX_ROTATION_DEG = 3.0

#: Body check: the outline drawn around the matched holes and the body size
#: of the reading, both in px at the fitted scale, may differ by two
#: tolerances (two edges) plus this drafting allowance, mm. Declared
#: heuristic: datasheets quote bodies to +-0.2-0.5 mm and the sample
#: outlines differed from their quotes by up to about 0.4 mm. A body quote
#: with its own tolerance would make it unnecessary.
BODY_ALLOWANCE_MM = 0.5

#: Minimum coverage of the four sides of an outline rectangle: an outline is a
#: continuous solid line; 10 % tolerates JPEG dropout and gaps where text or
#: arrows overlap. Dotted and dashed centre lines (about 50 %) fail.
OUTLINE_COVER_MIN = 0.90

#: At most this many distinct circle assignments are refined per image.
MAX_ASSIGNMENTS = 50

#: Budget of the missed-hole search, in ring probes (one radius over one
#: search window, one filter on a small crop). When it runs out the image
#: is "unverifiable" ("search_limit") instead of searched further: about one
#: second of work on a laptop.
MAX_RING_PROBES = 3000

#: Images whose analysis (photo gate, ink mask, circles) is kept in memory,
#: least recently used dropped first. The extractor checks two to four
#: readings against the same few images.
ANALYSIS_CACHE_SIZE = 8

#: Status values of :class:`LayoutCheck`.
CONFIRMED = "confirmed"
NOT_CONFIRMED = "not_confirmed"
UNVERIFIABLE = "unverifiable"

# Order of the not-confirmed stages: a later stage means the reading got
# further through the checks, so its reason is the more informative one.
_STAGES = ("no_match", "distorted", "body_scale", "low_resolution", "confirmed")

# Stages that contradict a reading (the others confirm it or cannot tell).
_CONTRADICTING = ("no_match", "distorted", "body_scale")


@dataclass(frozen=True)
class LayoutCheck:
    """@brief Outcome of :func:`check_pad_layout`.

    @note ``status`` is "confirmed", "not_confirmed" or "unverifiable";
        ``reason`` is a machine code ("confirmed", "too_few_pads",
        "too_many_pads", "no_image", "not_a_drawing", "too_few_circles",
        "low_resolution", "search_limit", "no_match", "distorted",
        "body_scale"); ``image`` is the file name of
        the image that decided, None when none did; ``max_error_mm`` and
        ``tolerance_mm`` come from the best fit, None when no fit was made;
        ``circles`` is the number of circles found in the deciding image (all
        of them, glyph-like ones included; for "too_few_circles" it may exceed
        the pad count when too few of them are drawn like holes).
    """

    status: str
    reason: str
    image: str | None
    max_error_mm: float | None
    tolerance_mm: float | None
    circles: int


@dataclass(frozen=True)
class _Circle:
    """A drawn circle in working-image pixels."""

    x: float
    y: float
    r: float
    coverage: float
    #: Drawn like a hole (centre line through it, or isolated on paper), see
    #: :func:`_hole_evidence`; only such circles can contradict a reading.
    evidence: bool = False


@dataclass(frozen=True)
class _Fit:
    """Result of one refined assignment: its stage and errors."""

    stage: str
    max_error_mm: float
    tolerance_mm: float


@dataclass(frozen=True)
class _ImageResult:
    """Per-image outcome used to choose the overall answer."""

    name: str
    drawing: bool
    circles: int
    fit: _Fit | None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_pad_layout(images: Sequence[Path], pads_mm: Mapping[object, Sequence[float]],
                     body_mm: Sequence[float] | None = None) -> LayoutCheck:
    """@brief Tell whether a reading of pad centres fits the hole circles drawn in an image.

    @param images Candidate drawing images; files with other suffixes than
        png/jpg/jpeg/webp/bmp and unreadable files are skipped.
    @param pads_mm Pad number -> (x, y) in mm AS SEEN ON THE PAGE: X to the
        right, Y down, arbitrary origin. The reading is never mirrored: a
        mirrored fit does not confirm.
    @param body_mm Optional (length along page X, width along page Y) in mm.
        When given and an outline rectangle enclosing the matched holes is
        found, its width and height at the fitted scale must agree with
        the body within two tolerances plus BODY_ALLOWANCE_MM.
    @return A :class:`LayoutCheck`: "confirmed" if any image confirms; else
        "not_confirmed" if at least one line drawing had at least as many
        circles drawn like holes as the reading has pads; else
        "unverifiable" with the most informative reason ("too_few_pads" below
        MIN_PADS pads, "too_many_pads" above MAX_PADS, then
        "search_limit" > "low_resolution" > "too_few_circles" >
        "not_a_drawing" > "no_image").
    @throws ValueError When a pad coordinate or a body size is not a finite
        number (a positive one for the body).
    @note Deterministic and local: no AI, no network, no millimetres derived
        from pixels.
    """
    points = _validated_points(pads_mm)
    body = _validated_body(body_mm)
    if len(points) < MIN_PADS:
        return LayoutCheck(UNVERIFIABLE, "too_few_pads", None, None, None, 0)
    if len(points) > MAX_PADS:
        return LayoutCheck(UNVERIFIABLE, "too_many_pads", None, None, None, 0)

    results: list[_ImageResult] = []
    for path in images:
        path = Path(path)
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        analysis = _analyse(path)
        if analysis is None:
            continue
        result = _check_image(path.name, analysis, points, body)
        if result.fit is not None and result.fit.stage == "confirmed":
            return LayoutCheck(CONFIRMED, "confirmed", result.name, round(result.fit.max_error_mm, 4),
                               round(result.fit.tolerance_mm, 4), result.circles)
        results.append(result)
    return _summarise(results)


# ---------------------------------------------------------------------------
# Input validation and image reading
# ---------------------------------------------------------------------------

def _validated_points(pads_mm: Mapping[object, Sequence[float]]) -> np.ndarray:
    """Return the reading as an (n, 2) float array; reject non-finite values."""
    rows = []
    for key, value in pads_mm.items():
        x, y = (float(v) for v in value)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"pad {key!r}: non-finite coordinate {value!r}")
        rows.append((x, y))
    return np.array(rows, float).reshape(-1, 2)


def _validated_body(body_mm: Sequence[float] | None) -> tuple[float, float] | None:
    """Return the body size as a pair of positive floats, or None."""
    if body_mm is None:
        return None
    length, width = (float(v) for v in body_mm)
    if not (math.isfinite(length) and math.isfinite(width) and length > 0 and width > 0):
        raise ValueError(f"body size must be two positive finite numbers, got {body_mm!r}")
    return length, width


def _read_image(path: Path) -> np.ndarray | None:
    """Read an image as BGR, transparent pixels composited on white; None if unreadable.

    The bytes are decoded from memory so that non-ASCII Windows paths work.
    """
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    except (OSError, ValueError, cv2.error):
        return None
    if image is None or image.size == 0:
        return None
    if image.dtype != np.uint8:
        # 16-bit PNG and similar: bring to 8 bit by the full range of the type.
        image = (image.astype(np.float64) * 255.0 / np.iinfo(image.dtype).max).astype(np.uint8) \
            if np.issubdtype(image.dtype, np.integer) else None
        if image is None:
            return None
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        # A drawing exported with a transparent background is black on
        # transparent: composite on white paper, not on black.
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        rgb = image[:, :, :3].astype(np.float32)
        return (rgb * alpha + 255.0 * (1.0 - alpha)).round().astype(np.uint8)
    return image[:, :, :3]


def _working_grey(image: np.ndarray) -> np.ndarray:
    """Grey image, downscaled so that its long side is at most MAX_LONG_SIDE_PX."""
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    long_side = max(grey.shape)
    if long_side > MAX_LONG_SIDE_PX:
        factor = MAX_LONG_SIDE_PX / long_side
        grey = cv2.resize(grey, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    return grey


# ---------------------------------------------------------------------------
# Ink mask and photo gate
# ---------------------------------------------------------------------------

def _ink_mask(grey: np.ndarray) -> tuple[np.ndarray, float]:
    """Ink mask relative to the page's own paper and ink levels, and the paper level.

    Paper = 90th percentile of grey (most of a drawing page is paper), ink =
    0.5th percentile. See :data:`INK_LEVEL`.
    """
    paper = float(np.percentile(grey, 90))
    ink = float(np.percentile(grey, 0.5))
    threshold = paper - INK_LEVEL * max(paper - ink, 1.0)
    return np.where(grey < threshold, 255, 0).astype(np.uint8), paper


def _is_line_drawing(grey: np.ndarray, mask: np.ndarray, paper: float) -> bool:
    """Photo gate: True when the page is mostly paper with little ink (declared heuristic)."""
    paper_fraction = float((grey > paper - PAPER_BAND).mean())
    ink_fraction = float((mask > 0).mean())
    return paper_fraction >= PAPER_FRACTION_MIN and ink_fraction <= INK_FRACTION_MAX


# ---------------------------------------------------------------------------
# Circle detection
# ---------------------------------------------------------------------------

def _stroke_width(mask: np.ndarray) -> float:
    """Typical line width in px: twice the median distance-transform value on stroke ridges."""
    dt = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 3)
    ridge = (dt > 0) & (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8)))
    values = dt[ridge]
    return float(max(1.0, 2 * np.median(values))) if values.size else 1.0


def _ring_kernel(radius: float, filled: bool = False, inner: float = 0.0, ss: int = 8) -> np.ndarray:
    """Normalised ring (1 px thick) or annulus kernel, supersampled and area-averaged."""
    half = int(math.ceil(radius)) + 2
    size = 2 * half + 1
    big = np.zeros((size * ss, size * ss), np.uint8)
    centre = (size * ss) // 2
    if filled:
        cv2.circle(big, (centre, centre), max(1, int(round(radius * ss))), 255, -1, lineType=cv2.LINE_AA)
        if inner > 0:
            cv2.circle(big, (centre, centre), int(round(inner * ss)), 0, -1, lineType=cv2.LINE_AA)
    else:
        cv2.circle(big, (centre, centre), int(round(radius * ss)), 255, ss, lineType=cv2.LINE_AA)
    kernel = cv2.resize(big, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    return kernel / max(float(kernel.sum()), 1e-6)


def _remove_hv_lines(mask: np.ndarray, length: int) -> np.ndarray:
    """Mask without horizontal / vertical straight runs of at least ``length`` px.

    Centre lines and extension lines through a hole would otherwise pull the
    centre fit; the removal is widened by 1 px across the line only.
    """
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    horizontal = cv2.dilate(horizontal, np.ones((3, 1), np.uint8))
    vertical = cv2.dilate(vertical, np.ones((1, 3), np.uint8))
    return cv2.bitwise_and(mask, cv2.bitwise_not(horizontal | vertical))


def _fit_circle(x: np.ndarray, y: np.ndarray, weights: np.ndarray) -> tuple[float, float, float]:
    """Weighted algebraic (Kasa) least-squares circle fit."""
    a = np.column_stack([x, y, np.ones_like(x)]) * weights[:, None]
    b = (x * x + y * y) * weights
    sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    return float(cx), float(cy), math.sqrt(max(float(sol[2] + cx * cx + cy * cy), 0.0))


def _weighted_fit(free: np.ndarray, dark: np.ndarray, cx: float, cy: float, r: float,
                  band: float) -> tuple[float, float, float]:
    """Re-fit a circle on the line-free ink of its ring, weighted by darkness.

    Keeps the filter estimate when the fit runs away (too few or one-sided points).
    """
    height, width = free.shape
    half = int(r + band) + 2
    x0, y0 = max(0, int(cx) - half), max(0, int(cy) - half)
    x1, y1 = min(width, int(cx) + half + 1), min(height, int(cy) + half + 1)
    ys, xs = np.nonzero(free[y0:y1, x0:x1])
    xs = xs + x0
    ys = ys + y0
    sel = np.abs(np.hypot(xs - cx, ys - cy) - r) <= band
    if sel.sum() < 8:
        return cx, cy, r
    ncx, ncy, nr = _fit_circle(xs[sel].astype(float), ys[sel].astype(float),
                               dark[ys[sel], xs[sel]].astype(float))
    if math.hypot(ncx - cx, ncy - cy) > 0.5 * r or abs(nr - r) > 0.5 * r:
        return cx, cy, r
    return ncx, ncy, nr


def _fine(mask: np.ndarray, xs_u: np.ndarray, ys_u: np.ndarray) -> np.ndarray:
    """Sample the mask at UPSCALE-times coordinates (nearest neighbour), out of image = paper.

    Equivalent to indexing a nearest-neighbour upscaled copy of the mask
    without allocating it.
    """
    height, width = mask.shape
    xi = np.floor(xs_u / UPSCALE).astype(int)
    yi = np.floor(ys_u / UPSCALE).astype(int)
    inside = (xi >= 0) & (xi < width) & (yi >= 0) & (yi < height)
    out = np.zeros(xi.shape, bool)
    out[inside] = mask[yi[inside], xi[inside]] > 0
    return out


def _fine_dilated(mask: np.ndarray, xs_u: np.ndarray, ys_u: np.ndarray) -> np.ndarray:
    """As :func:`_fine` on the upscaled mask dilated by +-UPSCALE/2 fine pixels (+-0.5 px)."""
    half = UPSCALE // 2
    out = np.zeros(np.shape(xs_u), bool)
    for dx in (-half, half):
        for dy in (-half, half):
            out |= _fine(mask, xs_u + dx, ys_u + dy)
    return out


def _ring_samples(cx: float, cy: float, radii: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    """Rounded sample coordinates on ``count`` directions x ``radii`` around a centre."""
    angles = np.linspace(0, 2 * math.pi, count, endpoint=False)
    xs = np.round(cx + np.outer(np.cos(angles), radii))
    ys = np.round(cy + np.outer(np.sin(angles), radii))
    return xs, ys


def _ring_coverage(mask: np.ndarray, cu: float, cv: float, ru: float) -> float:
    """Fraction of the ring (fine coordinates) inked on the slightly dilated mask."""
    xs, ys = _ring_samples(cu, cv, np.array([ru]), 96)
    return float(_fine_dilated(mask, xs, ys).mean())


def _radial_profile(mask: np.ndarray, cu: float, cv: float, ru: float) -> np.ndarray:
    """Per direction, the ink radius closest to ``ru`` within [0.5, 1.5] ru (nan = none)."""
    radii = np.arange(0.5 * ru, 1.5 * ru, 0.5)
    xs, ys = _ring_samples(cu, cv, radii, 72)
    hit = _fine(mask, xs, ys)
    distance = np.where(hit, np.abs(radii[None, :] - ru), np.inf)
    best = distance.argmin(axis=1)
    profile = radii[best].astype(float)
    profile[~hit.any(axis=1)] = np.nan
    return profile


def _paper_sides(mask: np.ndarray, cu: float, cv: float, ru: float, margin: float) -> tuple[float, float]:
    """Fractions of directions with paper inside [0.3r, r-m] and outside [r+m, r+m+0.5r]."""
    fractions = []
    for lo, hi in ((0.3 * ru, ru - margin), (ru + margin, ru + margin + 0.5 * ru)):
        if hi <= lo:
            hi = lo + 1
        xs, ys = _ring_samples(cu, cv, np.linspace(lo, hi, 6), 72)
        fractions.append(float((~_fine(mask, xs, ys)).any(axis=1).mean()))
    return fractions[0], fractions[1]


def _is_glyph(circle: _Circle, ink: np.ndarray, others: Sequence[_Circle] = ()) -> bool:
    """True when the circle is (part of) a character of text. Declared heuristics.

    A. Own component: the ink component carrying the ring has the height of
       a text line (<= 3 d), is not much wider than three of its own heights
       (a glyph or a few touching glyphs, not a long line), and sticks out of
       the circle by > 0.3 d on only one or two sides, massively (>= half the
       ring's own ink): a bowl of B, P, R, 5, 6, 8, 9, a, e... A hole is
       either isolated, crossed by centre lines (sticks out thinly on 3-4
       sides) or attached to a long line (> 3 d).
    B. Neighbours: another small component of similar height (0.5-1.6 x),
       vertically overlapping >= 50 %, within 0.6 d to the left or right: a
       letter in a word. A hole keeps a clearance of about its diameter from
       its labels, which are usually offset diagonally. A neighbour that
       carries another candidate circle of about the same size (see
       SIMILAR_RADIUS) is the next hole of a row, not a letter.

    Tuned on: the relay and TEL-8 pages of the repository and synthetic
    pages drawn with OpenCV's Hershey font (digits 0, 4, 5, 6, 8, 9 at
    12 px). The 0.3 d protrusion, the half-ring "massive" test, the 3 d
    size bound and the 0.6 d / 0.5-1.6 x neighbour bands separate those
    glyph bowls from those holes; the 0.6 d gap had only a few percent of
    margin on Hershey zeros. Known misses: a slashed zero, the counter of a
    4, a "0" between hyphens. What would make them unnecessary: text
    recognition (OCR boxes) of the page, removing every glyph before the
    circle search.
    """
    height, width = ink.shape
    d = 2 * circle.r
    # Crop wide enough to hold a few touching glyphs of the circle's size.
    pad = int(math.ceil(d * 4))
    x0, y0 = max(0, int(circle.x - pad)), max(0, int(circle.y - pad))
    x1, y1 = min(width, int(circle.x + pad) + 1), min(height, int(circle.y + pad) + 1)
    sub = ink[y0:y1, x0:x1].astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
    cx0, cx1 = circle.x - circle.r, circle.x + circle.r
    cy0, cy1 = circle.y - circle.r, circle.y + circle.r
    angles = np.linspace(0, 2 * math.pi, 48, endpoint=False)
    rx = np.clip(np.round(circle.x + circle.r * np.cos(angles)).astype(int) - x0, 0, sub.shape[1] - 1)
    ry = np.clip(np.round(circle.y + circle.r * np.sin(angles)).astype(int) - y0, 0, sub.shape[0] - 1)
    ring_labels = labels[ry, rx]
    ring_labels = ring_labels[ring_labels > 0]
    own = int(np.bincount(ring_labels).argmax()) if ring_labels.size else 0
    glyph_height = d
    if own:
        bx, by, bw, bh, _ = stats[own]
        bx += x0
        by += y0
        touches_crop = bx <= x0 or by <= y0 or bx + bw >= x1 or by + bh >= y1
        if not touches_crop and bh <= 3 * d and bw <= 3 * max(d, bh):
            extents = [cx0 - bx, (bx + bw) - cx1, cy0 - by, (by + bh) - cy1]
            sides = sum(e > 0.3 * d for e in extents)
            yy, xx = np.nonzero(labels == own)
            rr = np.hypot(xx + x0 - circle.x, yy + y0 - circle.y)
            outside = int((rr > circle.r + 1.5).sum())
            ring_ink = int((np.abs(rr - circle.r) <= 1.5).sum())
            if sides in (1, 2) and outside >= 0.5 * max(ring_ink, 1):
                return True
            glyph_height = max(d, bh)
    for index in range(1, count):
        if index == own:
            continue
        bx, by, bw, bh, area = stats[index]
        bx += x0
        by += y0
        if area < 3 or not (0.5 * glyph_height <= bh <= 1.6 * glyph_height) or bw > 3 * glyph_height:
            continue
        # A neighbour carrying another circle of about the same size is the
        # next hole of a row (its box widened by centre-line dashes), not a
        # letter: a row of holes on a centre line would otherwise read as a
        # word.
        if any(o is not circle and bx <= o.x <= bx + bw and by <= o.y <= by + bh
               and 1 / SIMILAR_RADIUS <= o.r / circle.r <= SIMILAR_RADIUS for o in others):
            continue
        overlap = min(by + bh, cy1) - max(by, cy0)
        if overlap < 0.5 * min(bh, d):
            continue
        gap = max(bx - cx1, cx0 - (bx + bw))
        if -1.5 <= gap <= 0.6 * d:
            return True
    return False


def _level_candidates(mask: np.ndarray, factor: int, r_low: float, r_high: float
                      ) -> list[tuple[float, float, float, float]]:
    """Ring matched filter on one pyramid level; candidates in working pixels.

    Score per radius r = ring coverage on the dilated mask minus the ink on
    two probe rings just inside and just outside it (a straight line crosses
    a ring in two points only, a concentric stroke or a glyph covers much of
    it). Returns (score, x, y, r) after a plateau suppression.
    """
    height, width = mask.shape
    sw = _stroke_width(mask)
    margin = sw / 2 + 1.0
    dilated = cv2.dilate(mask, np.ones((3, 3), np.uint8)).astype(np.float32) / 255.0
    plain = (mask > 0).astype(np.float32)
    best = np.full((height, width), -9, np.float32)
    best_r = np.zeros((height, width), np.float32)
    cache: dict[tuple[str, float], np.ndarray] = {}

    def ring_response(radius: float, source: np.ndarray, key: str) -> np.ndarray:
        """Ring-kernel response of ``source`` at a quarter-pixel quantised radius."""
        quantised = (key, round(radius * 4) / 4)
        if quantised not in cache:
            cache[quantised] = cv2.filter2D(source, -1, _ring_kernel(quantised[1]),
                                            borderType=cv2.BORDER_CONSTANT)
        return cache[quantised]

    for r in np.arange(r_low, r_high, 0.5):
        ring = ring_response(r, dilated, "d")
        r_out = r + margin + 0.5
        outer = ring_response(r_out, plain, "p")
        r_in = r - margin - 0.5
        inner = ring_response(r_in, plain, "p") if r_in >= 2.5 else np.zeros_like(ring)
        # A line crossing a probe ring costs about 2 w / (2 pi r) per line;
        # the budget leaves room for four such crossings.
        limit_out = min(0.6, 0.25 + 4 * sw / (2 * math.pi * r_out))
        limit_in = min(0.6, 0.25 + 4 * sw / (2 * math.pi * max(r_in, 1)))
        ok = (ring >= COVERAGE_MIN) & (inner <= limit_in) & (outer <= limit_out)
        score = np.where(ok, ring - 0.5 * (inner + outer), -9)
        better = score > best
        best[better] = score[better]
        best_r[better] = r
    ys, xs = np.nonzero(best > -9)
    order = np.argsort(-best[ys, xs], kind="stable")
    taken = np.zeros((height, width), np.uint8)
    out = []
    for index in order:
        y, x = int(ys[index]), int(xs[index])
        if taken[y, x]:
            continue
        r = float(best_r[y, x])
        # Suppress only the plateau around the candidate (0.35 r): the cells
        # between a hole and its crosshair are small round candidates
        # themselves and would otherwise hide the hole's own centre.
        cv2.circle(taken, (x, y), max(1, int(round(0.35 * r))), 1, -1)
        out.append((float(best[y, x]), (x + 0.5) * factor - 0.5, (y + 0.5) * factor - 0.5, r * factor))
    return out


def _find_circles(grey: np.ndarray, mask: np.ndarray) -> list[_Circle]:
    """Small drawn circles (holes, pins seen end-on), verified and glyph-filtered.

    Candidates come from a ring matched filter on a pyramid (radii 2.5-12 px
    on the page, then 6-12 px on the page halved, quartered...), so the
    kernels stay small for every radius. Each candidate is refined on the
    line-free ink of the working image and verified there at sub-pixel
    resolution: ring coverage, roundness, paper on both sides, not a glyph.
    """
    height, width = mask.shape
    r_max = max(R_MIN_PX + 1, R_MAX_FRACTION * min(height, width))
    candidates: list[tuple[float, float, float, float]] = []
    level, factor = 0, 1
    while True:
        r_low = R_MIN_PX if level == 0 else LEVEL_R_HIGH / 2
        if r_low * factor > r_max:
            break
        r_high = min(LEVEL_R_HIGH, r_max / factor + 0.5)
        if level == 0:
            level_mask = mask
        else:
            # Max-pool the ink so thin lines survive the reduction.
            level_mask = (cv2.resize(mask, (max(1, width // factor), max(1, height // factor)),
                                     interpolation=cv2.INTER_AREA) > 0).astype(np.uint8) * 255
        if min(level_mask.shape) < 2 * r_low + 4:
            break
        candidates += _level_candidates(level_mask, factor, r_low, r_high)
        level += 1
        factor *= 2
    candidates.sort(key=lambda item: -item[0])

    dark = (255 - grey.astype(np.int16)).clip(1, 255)
    sw = _stroke_width(mask)
    margin = sw / 2 + 1.0
    ink = mask > 0
    free_cache: dict[int, np.ndarray] = {}

    def free_for(radius: float) -> np.ndarray:
        """Line-free ink for a radius; the run length is quantised in 15 % steps."""
        length = max(6, int(math.ceil(2.2 * radius)))  # longer than any near-straight chord
        quantised = 6
        while quantised < length:
            quantised = int(math.ceil(quantised * 1.15))
        if quantised not in free_cache:
            free_cache[quantised] = _remove_hv_lines(mask, quantised) > 0
        return free_cache[quantised]

    found: list[_Circle] = []
    for _score, x, y, r in candidates:
        free = free_for(r)
        cx, cy, rr = x, y, r
        for _ in range(2):
            cx, cy, rr = _weighted_fit(free, dark, cx, cy, rr, band=max(1.5, sw))
        cu, cv, ru = (cx + 0.5) * UPSCALE - 0.5, (cy + 0.5) * UPSCALE - 0.5, rr * UPSCALE
        coverage = _ring_coverage(mask, cu, cv, ru)
        if coverage < COVERAGE_MIN:
            continue
        profile = _radial_profile(mask, cu, cv, ru)
        near = np.abs(profile - ru) <= 0.2 * ru
        if np.mean(near) < COVERAGE_MIN:
            continue
        if float(np.nanstd(profile[near]) / ru) > ROUND_TOLERANCE:
            continue
        inside, outside = _paper_sides(mask, cu, cv, ru, margin * UPSCALE)
        if inside < PAPER_SIDES_MIN or outside < PAPER_SIDES_MIN:
            continue
        found.append(_Circle(cx, cy, rr, coverage))

    found.sort(key=lambda c: (-c.coverage, -c.r))
    kept: list[_Circle] = []
    for circle in found:
        duplicate = any(math.hypot(circle.x - k.x, circle.y - k.y) < 0.5 * max(circle.r, k.r)
                        and abs(circle.r - k.r) < 0.3 * max(circle.r, k.r) for k in kept)
        if not duplicate:
            kept.append(circle)
    # A small circle whose centre lies inside a bigger one, not concentric with
    # it, is a cell between a hole outline and its centre lines (or a glyph
    # counter): two drawn holes never overlap.
    kept = [c for c in kept
            if not any(k.r > 1.4 * c.r and 0.2 * k.r < math.hypot(c.x - k.x, c.y - k.y) < k.r
                       for k in kept)]
    kept = [c for c in kept if not _is_glyph(c, ink, kept)][:MAX_CIRCLES]
    return [replace(c, evidence=_hole_evidence(c, ink, sw)) for c in kept]


def _bridge(run: np.ndarray, gap: int) -> np.ndarray:
    """1-D closing: fill the paper gaps of at most ``gap`` samples between two inked samples."""
    out = run.copy()
    inked = np.nonzero(run)[0]
    for a, b in zip(inked, inked[1:]):
        if 1 < b - a <= gap + 1:
            out[a:b] = True
    return out


def _hole_evidence(circle: _Circle, ink: np.ndarray, sw: float) -> bool:
    """True when the circle is drawn the way a hole is drawn, not only shaped like one.

    Declared heuristic, from drafting convention: a hole is either marked by
    a horizontal or vertical line through its centre that continues outside
    it for at least two diameters on one side (centre line or extension line
    running to a dimension; dashed and dotted lines are bridged over gaps up
    to 2 stroke widths + 1 px; no stroke of a glyph runs that far straight),
    or it stands alone on paper (no
    ink within one diameter outside its ring). Glyph counters fail both: a
    slashed zero is crossed diagonally, a bar of e or theta stops at the
    glyph, and letters stand next to each other. A hole drawn without centre
    lines but with a label inside that clearance is not counted (a
    conservative miss: the image then cannot contradict a reading).

    Tuned on: the relay and TEL-8 pages (every drawn hole counted) and two
    synthetic text pages (all 58 glyph circles of a digits page refused, 4
    of 13 on a mixed paragraph accepted, through "-0-" hyphen rows). The
    numbers: line beyond two diameters, at least 50 % inked after bridging,
    paper on both sides of at least 70 % of its samples (a row through text
    crosses strokes that extend across it), isolation ring free of ink up
    to 2 % of its samples. What would make it unnecessary: text recognition
    of the page, or vector drawings whose holes are explicit circles.
    """
    height, width = ink.shape
    r = circle.r
    step = 0.5
    gap = int(round((2 * sw + 1) / step))

    def inked(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Ink at rounded sample positions; outside the image counts as paper."""
        xi, yi = np.round(xs).astype(int), np.round(ys).astype(int)
        ok = (xi >= 0) & (xi < width) & (yi >= 0) & (yi < height)
        out = np.zeros(xi.shape, bool)
        out[ok] = ink[yi[ok], xi[ok]]
        return out

    inner = np.arange(-0.6 * r, 0.6 * r + 1e-9, step)
    outer = np.arange(r + sw + 1, 5 * r + 1e-9, step)
    for dx, dy in ((1.0, 0.0), (0.0, 1.0)):
        for offset in (-1.0, 0.0, 1.0):
            px, py = circle.x + dy * offset, circle.y + dx * offset
            centre_ok = _bridge(inked(px + dx * inner, py + dy * inner), gap).mean() >= 0.5
            if not centre_ok:
                continue
            for sign in (-1.0, 1.0):
                xs, ys = px + sign * dx * outer, py + sign * dy * outer
                run = inked(xs, ys)
                if not run.size or _bridge(run, gap).mean() < 0.5:
                    continue
                # A line is thin: beside its inked samples, on both sides
                # across it, there is paper. A row through a line of text
                # crosses strokes that extend across it, so bridging the
                # gaps between letters does not make it a line.
                side = sw + 1.5
                clear = ~inked(xs[run] + dy * side, ys[run] + dx * side)                     & ~inked(xs[run] - dy * side, ys[run] - dx * side)
                if clear.mean() >= 0.7:
                    return True
    angles = np.linspace(0, 2 * math.pi, 72, endpoint=False)
    radii = np.arange(r + sw + 1.5, 3 * r + 1e-9, 1.0)
    if radii.size == 0:
        return False
    xs = circle.x + np.outer(np.cos(angles), radii)
    ys = circle.y + np.outer(np.sin(angles), radii)
    # 2 % of the samples tolerate isolated JPEG specks.
    return float(inked(xs, ys).mean()) <= 0.02


# ---------------------------------------------------------------------------
# Outline rectangle (body scale check)
# ---------------------------------------------------------------------------

def _line_bands(mask: np.ndarray, horizontal: bool, min_length: int) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Straight ink runs of at least ``min_length`` px and the row/column bands holding them."""
    shape = (min_length, 1) if horizontal else (1, min_length)
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, shape))
    profile = (opened > 0).sum(axis=1 if horizontal else 0)
    indices = np.nonzero(profile)[0]
    bands: list[tuple[int, int]] = []
    for i in indices:
        i = int(i)
        if bands and i - bands[-1][1] <= 1:
            bands[-1] = (bands[-1][0], i)
        else:
            bands.append((i, i))
    return opened, bands


def _outline_around(mask: np.ndarray, circles: Sequence[_Circle]) -> tuple[float, float] | None:
    """Width and height, px, of the smallest solid rectangle enclosing all the circles.

    A side counts when it is inked for at least OUTLINE_COVER_MIN of its
    length; the circles must lie fully inside. None when no such rectangle exists.
    """
    height, width = mask.shape
    min_length = max(8, int(0.04 * min(height, width)))
    h_lines, rows = _line_bands(mask, True, min_length)
    v_lines, cols = _line_bands(mask, False, min_length)
    h_ok = np.cumsum(np.pad(cv2.dilate(h_lines, np.ones((3, 3), np.uint8)) > 0, ((0, 0), (1, 0))), axis=1)
    v_ok = np.cumsum(np.pad(cv2.dilate(v_lines, np.ones((3, 3), np.uint8)) > 0, ((1, 0), (0, 0))), axis=0)
    left_edge = min(c.x - c.r for c in circles)
    right_edge = max(c.x + c.r for c in circles)
    top_edge = min(c.y - c.r for c in circles)
    bottom_edge = max(c.y + c.r for c in circles)

    def centre(band: tuple[int, int]) -> float:
        """Centre of a band of rows or columns."""
        return (band[0] + band[1]) / 2

    tops = [b for b in rows if centre(b) < top_edge]
    bottoms = [b for b in rows if centre(b) > bottom_edge]
    lefts = [b for b in cols if centre(b) < left_edge]
    rights = [b for b in cols if centre(b) > right_edge]

    def h_cover(band: tuple[int, int], xa: int, xb: int) -> float:
        """Best inked fraction of a horizontal side over the rows of its band."""
        return max((h_ok[r, xb] - h_ok[r, xa]) / max(1, xb - xa) for r in range(band[0], band[1] + 1))

    def v_cover(band: tuple[int, int], ya: int, yb: int) -> float:
        """Best inked fraction of a vertical side over the columns of its band."""
        return max((v_ok[yb, c] - v_ok[ya, c]) / max(1, yb - ya) for c in range(band[0], band[1] + 1))

    combos = [(t, b, l, r) for t in tops for b in bottoms for l in lefts for r in rights]
    combos.sort(key=lambda q: (centre(q[3]) - centre(q[2])) * (centre(q[1]) - centre(q[0])))
    for t, b, l, r in combos:
        xa, xb = int(centre(l)), int(centre(r))
        ya, yb = int(centre(t)), int(centre(b))
        if min(h_cover(t, xa, xb), h_cover(b, xa, xb), v_cover(l, ya, yb), v_cover(r, ya, yb)) \
                >= OUTLINE_COVER_MIN:
            return centre(r) - centre(l), centre(b) - centre(t)
    return None


# ---------------------------------------------------------------------------
# Matching a reading to the circles
# ---------------------------------------------------------------------------

def _hypothesis_assignments(points: np.ndarray, circles: Sequence[_Circle],
                            max_misses: int = 0,
                            gate_fraction: float = HYPOTHESIS_TOL_SPAN) -> list[np.ndarray]:
    """Circle assignments (one distinct circle per pad, -1 = none) proposed by similarity hypotheses.

    Each of the BASELINES longest pad pairs is mapped onto every ordered pair
    of circles whose direction differs by at most MAX_ROTATION_DEG + 2 degrees
    (no mirror: the reading is in page orientation). A hypothesis survives
    only while at most ``max_misses`` pads project farther than the gate
    distance from every circle; pads are tested farthest-from-the-baseline
    first, so most hypotheses are dropped after a few pads. Cost:
    O(BASELINES * C^2 + surviving * n * C). Assignments with fewer misses
    come first. ``gate_fraction`` scales the gate with the pad's distance
    from the nearer baseline pad.
    """
    n = len(points)
    q = points[:, 0] + 1j * points[:, 1]
    centres = np.array([c.x + 1j * c.y for c in circles])
    radii = np.array([c.r for c in circles])
    pair_i, pair_j = np.triu_indices(n, 1)
    lengths = np.abs(q[pair_j] - q[pair_i])
    min_spacing = float(lengths.min())
    baselines = np.argsort(-lengths, kind="stable")[:BASELINES]
    a_idx, b_idx = np.nonzero(~np.eye(len(circles), dtype=bool))
    found: dict[tuple[int, ...], np.ndarray] = {}
    for k in baselines:
        i, j = int(pair_i[k]), int(pair_j[k])
        dq = q[j] - q[i]
        z = (centres[b_idx] - centres[a_idx]) / dq  # scale * exp(i rotation)
        keep = np.abs(np.degrees(np.angle(z))) <= MAX_ROTATION_DEG + 2
        # Two pads closer than two tolerances could share one circle's
        # neighbourhood: such a scale cannot tell pads apart.
        keep &= np.abs(z) * min_spacing >= 2 * TOLERANCE_PX
        za, origin = z[keep], centres[a_idx[keep]]
        if za.size == 0:
            continue
        base_r = (radii[a_idx[keep]] + radii[b_idx[keep]]) / 2
        assign = np.full((za.size, n), -1, int)
        misses = np.zeros(za.size, int)
        # The two baseline pads sit exactly on their circles, so a small
        # anisotropy or shear displaces a pad in proportion to its distance
        # from the nearer of them: the gate grows with that distance.
        anchor = np.minimum(np.abs(q - q[i]), np.abs(q - q[j]))
        order = np.argsort(-anchor, kind="stable")
        alive = np.arange(za.size)
        for p in order:
            projected = origin[alive] + za[alive] * (q[p] - q[i])
            distance = np.abs(projected[:, None] - centres[None, :])
            gate = np.maximum(HYPOTHESIS_TOL_PX, gate_fraction * np.abs(za[alive]) * anchor[p])
            rows = np.arange(alive.size)
            # Prefer a circle of about the baseline holes' size (declared
            # heuristic, see SIMILAR_RADIUS): with a wide gate the counter of
            # a pin label next to a hole can be nearer than the hole itself.
            ratio = radii[None, :] / base_r[alive][:, None]
            similar = (ratio >= 1 / SIMILAR_RADIUS) & (ratio <= SIMILAR_RADIUS)
            preferred = np.where(similar, distance, np.inf).argmin(axis=1)
            nearest = distance.argmin(axis=1)
            use_preferred = distance[rows, preferred] <= gate
            nearest = np.where(use_preferred, preferred, nearest)
            good = distance[rows, nearest] <= gate
            assign[alive[good], p] = nearest[good]
            misses[alive[~good]] += 1
            alive = alive[misses[alive] <= max_misses]
            if alive.size == 0:
                break
        for row in assign[alive]:
            key = tuple(int(v) for v in row)
            matched = [v for v in key if v >= 0]
            if len(set(matched)) == len(matched) and key not in found:
                found[key] = row
    return sorted(found.values(), key=lambda row: int((row < 0).sum()))


def _is_collinear(points: np.ndarray, ratio: float = COLLINEAR_RATIO) -> bool:
    """True when the points lie on one straight line within ``ratio``.

    ``ratio`` bounds the second singular value of the centred points as a
    fraction of the first.
    """
    centred = points - points.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    return bool(singular[1] <= ratio * singular[0])


def _min_spacing_mm(points: np.ndarray) -> float:
    """Smallest centre-to-centre distance of the reading, mm."""
    q = points[:, 0] + 1j * points[:, 1]
    pair_i, pair_j = np.triu_indices(len(points), 1)
    return float(np.abs(q[pair_j] - q[pair_i]).min())


@dataclass(frozen=True)
class _Similarity:
    """Uniform scale + rotation + translation from reading mm to image px (no mirror)."""

    gain: complex
    shift: complex

    @property
    def scale(self) -> float:
        """px per mm."""
        return max(abs(self.gain), 1e-9)

    @property
    def rotation_deg(self) -> float:
        """Rotation of the page relative to the reading, degrees."""
        return math.degrees(np.angle(self.gain))

    def project(self, points: np.ndarray) -> np.ndarray:
        """Image px of reading points (n, 2)."""
        z = self.gain * (points[:, 0] + 1j * points[:, 1]) + self.shift
        return np.column_stack([z.real, z.imag])


def _fit_similarity(points: np.ndarray, targets: np.ndarray) -> _Similarity:
    """Least-squares similarity (complex linear fit) of reading points onto image targets."""
    q = points[:, 0] + 1j * points[:, 1]
    c = targets[:, 0] + 1j * targets[:, 1]
    sol, *_ = np.linalg.lstsq(np.column_stack([q, np.ones_like(q)]), c, rcond=None)
    return _Similarity(complex(sol[0]), complex(sol[1]))


def _fit_affine(points: np.ndarray, targets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares affine map; returns the projected points (px) and the 2x2 linear part."""
    design = np.column_stack([points, np.ones(len(points))])
    sol, *_ = np.linalg.lstsq(design, targets, rcond=None)  # 3x2: [A^T; t]
    return design @ sol, sol[:2].T


def _leave_one_out_px(points: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Displacement of each pad from where the similarity fitted on the OTHER pads puts it, px.

    A least-squares fit spreads one wrong pad's error over all pads (with
    three pads a 0.22 mm error shows as 0.07 mm); fitting on the others
    measures each pad against the rest of the reading, which is what "the
    reading's own quotes land on the drawn holes" means. Closed form of the
    complex linear fit with one point removed.
    """
    q = points[:, 0] + 1j * points[:, 1]
    c = targets[:, 0] + 1j * targets[:, 1]
    m = len(q) - 1
    q_mean = (q.sum() - q) / m
    c_mean = (c.sum() - c) / m
    qq = (np.abs(q) ** 2).sum() - np.abs(q) ** 2 - m * np.abs(q_mean) ** 2
    cq = (c * np.conj(q)).sum() - c * np.conj(q) - m * c_mean * np.conj(q_mean)
    gain = cq / np.maximum(qq, 1e-12)
    return np.abs(c - (c_mean + gain * (q - q_mean)))


def _stretch_displacement_mm(points: np.ndarray, linear: np.ndarray) -> float:
    """Displacement, mm, that the affine part beyond a similarity implies on the reading.

    The fit cannot tell which axis is drawn to scale, so the anisotropy
    (|sx / sy - 1|) and the shear (radians) are expressed as the
    displacement they cause over the reading's SHORTER extent along the page
    axes: the smaller of the two possible errors, i.e. the one the reading
    must at least have. A whole X axis read 2.45 instead of 2.54 on a
    2 x 20 header (3.5 %) is 0.27 mm over its 7.62 mm rows, while the
    least-squares similarity would split it into +-0.14 mm.
    """
    sx, sy = float(np.linalg.norm(linear[:, 0])), float(np.linalg.norm(linear[:, 1]))
    cos_angle = float(linear[:, 0] @ linear[:, 1] / max(sx * sy, 1e-12))
    shear = abs(math.pi / 2 - math.acos(max(-1.0, min(1.0, abs(cos_angle)))))
    anisotropy = abs(sx / max(sy, 1e-12) - 1)
    extent = min(float(np.ptp(points[:, 0])), float(np.ptp(points[:, 1])))
    return (anisotropy + shear) * extent


def _affine_observable(points: np.ndarray) -> bool:
    """True when an affine fit of the reading is over-determined in both directions.

    It needs at least AFFINE_MIN_PADS pads that are not nearly collinear
    (see AFFINE_MIN_SPREAD); otherwise its cross-row scale is set by a few
    sub-pixel offsets and it would "explain" anything.
    """
    return len(points) >= AFFINE_MIN_PADS and not _is_collinear(points, AFFINE_MIN_SPREAD)


def _tolerance_mm(scale: float) -> float:
    """Residual tolerance: max(MIN_TOLERANCE_MM, TOLERANCE_PX / scale)."""
    return max(MIN_TOLERANCE_MM, TOLERANCE_PX / max(scale, 1e-9))


def _fit_assignment(points: np.ndarray, circles: Sequence[_Circle], assign: np.ndarray,
                    mask: np.ndarray, body: tuple[float, float] | None) -> _Fit:
    """Fit one complete assignment and classify it.

    The verdict is a displacement test: the reading, mapped onto the page by
    a similarity (uniform scale, rotation, translation, no mirror) fitted on
    the other pads, must put every pad within the tolerance of its circle
    (see :func:`_leave_one_out_px`; max_error_mm reports it). The affine fit
    is used only (a) to bound the stretch and shear the drawing would need,
    expressed as a displacement (see :func:`_stretch_displacement_mm`) and
    held to the same tolerance, and (b) to call a failed reading "distorted"
    when a stretch or shear explains it (a drawing not to scale, or a whole
    axis read wrong) rather than "no_match". Three pads are checked by the
    similarity alone: an affine map fits any three points exactly.
    """
    targets = np.array([(circles[k].x, circles[k].y) for k in assign], float)
    sim = _fit_similarity(points, targets)
    residual_px = _leave_one_out_px(points, targets)
    tolerance = _tolerance_mm(sim.scale)
    tolerance_px = tolerance * sim.scale
    max_error = float(residual_px.max() / sim.scale)
    affine = _affine_observable(points)
    if residual_px.max() > tolerance_px:
        if affine:
            affine_projected, linear = _fit_affine(points, targets)
            if (np.linalg.det(linear) > 0
                    and np.linalg.norm(targets - affine_projected, axis=1).max() <= tolerance_px):
                return _Fit("distorted", max_error, tolerance)
        return _Fit("no_match", max_error, tolerance)
    if affine:
        _, linear = _fit_affine(points, targets)
        if np.linalg.det(linear) <= 0 or _stretch_displacement_mm(points, linear) > tolerance:
            return _Fit("distorted", max_error, tolerance)
    if abs(sim.rotation_deg) > MAX_ROTATION_DEG:
        return _Fit("distorted", max_error, tolerance)
    if tolerance > LOW_RESOLUTION_FRACTION * _min_spacing_mm(points):
        return _Fit("low_resolution", max_error, tolerance)
    if body is not None:
        outline = _outline_around(mask, [circles[k] for k in assign])
        if outline is not None:
            # The outline width is the distance between two drawn edges, each
            # placed within the tolerance: their difference may reach two
            # tolerances, plus the drafting allowance of a body outline.
            allowed = 2 * tolerance_px + BODY_ALLOWANCE_MM * sim.scale
            if (abs(outline[0] - body[0] * sim.scale) > allowed
                    or abs(outline[1] - body[1] * sim.scale) > allowed):
                return _Fit("body_scale", max_error, tolerance)
    return _Fit("confirmed", max_error, tolerance)


class _BudgetExceeded(Exception):
    """Raised when the bounded search for missed holes runs out of probes."""


class _Budget:
    """Remaining ring probes (one radius tested over one search window)."""

    def __init__(self, probes: int) -> None:
        """@brief Start with ``probes`` probes.

        @param probes Number of ring probes allowed.
        """
        self.left = probes

    def spend(self, count: int) -> None:
        """Consume ``count`` probes; raise _BudgetExceeded when none are left."""
        self.left -= count
        if self.left < 0:
            raise _BudgetExceeded()


def _ring_at(mask: np.ndarray, x: float, y: float, r: float, search: float, budget: _Budget) -> bool:
    """True when a closed ring of about radius ``r`` is inked within ``search`` px of (x, y).

    Same closed-ring test as the detector's verification (COVERAGE_MIN of
    96 ring samples at UPSCALE sub-pixel resolution, on the mask dilated by
    half a pixel), for radii 0.7-1.3 r in half-pixel steps and centres on a
    half-pixel grid over the search window, without the detector's later
    filters. All centres of one radius are tested at once; each radius
    costs one probe of ``budget``.
    """
    offsets = np.arange(-search, search + 1e-9, 0.5)
    gx, gy = np.meshgrid(x + offsets, y + offsets)
    cu = (gx.ravel() + 0.5) * UPSCALE - 0.5
    cv = (gy.ravel() + 0.5) * UPSCALE - 0.5
    angles = np.linspace(0, 2 * math.pi, 96, endpoint=False)
    cos_a, sin_a = np.cos(angles), np.sin(angles)
    radii = np.arange(0.7 * r, 1.3 * r + 1e-9, 0.5)
    budget.spend(len(radii))
    for rr in radii:
        ru = rr * UPSCALE
        xs = np.round(cu[:, None] + ru * cos_a[None, :])
        ys = np.round(cv[:, None] + ru * sin_a[None, :])
        if _fine_dilated(mask, xs, ys).mean(axis=1).max() >= COVERAGE_MIN:
            return True
    return False


def _missed_hole_explains(points: np.ndarray, circles: Sequence[_Circle], assign: np.ndarray,
                          mask: np.ndarray, budget: _Budget) -> bool:
    """True when the pads that do not fit sit on rings the detector did not keep.

    Detection fails on small, low-resolution or crowded holes (a hole missed,
    or its centre pulled by a crossing leader line). Before a reading is
    reported as contradicted, a robust similarity is fitted: one from each of
    the longest assigned pad pairs, the one with the smallest median
    residual kept, its inliers refitted twice. When the inliers are a strict
    majority (and at least MIN_PADS), the rotation is within the limit and
    EVERY other pad lands on a closed ring of the matched holes' size, the
    image cannot contradict the reading. A wrong pad lands on paper or on a
    centre line, which is not a closed ring, or on a circle that WAS
    detected (another pad's hole), which is not a missed hole.
    """
    n = len(points)
    assigned = [p for p in range(n) if assign[p] >= 0]
    if len(assigned) < MIN_PADS:
        return False
    sub = points[assigned]
    targets = np.array([(circles[assign[p]].x, circles[assign[p]].y) for p in assigned], float)
    q = sub[:, 0] + 1j * sub[:, 1]
    c = targets[:, 0] + 1j * targets[:, 1]
    pair_i, pair_j = np.triu_indices(len(assigned), 1)
    longest = np.argsort(-np.abs(q[pair_j] - q[pair_i]), kind="stable")[:3 * BASELINES]
    best_median, model = math.inf, None
    for k in longest:
        i, j = pair_i[k], pair_j[k]
        gain = (c[j] - c[i]) / (q[j] - q[i])
        median = float(np.median(np.abs(c - (c[i] + gain * (q - q[i])))))
        if median < best_median:
            best_median, model = median, _Similarity(complex(gain), complex(c[i] - gain * q[i]))
    if model is None:
        return False
    keep = np.ones(len(assigned), bool)
    for _ in range(3):
        residual = np.linalg.norm(targets - model.project(sub), axis=1)
        keep = residual <= _tolerance_mm(model.scale) * model.scale
        if keep.sum() < MIN_PADS:
            return False
        model = _fit_similarity(sub[keep], targets[keep])
    residual = np.linalg.norm(targets - model.project(sub), axis=1)
    search = _tolerance_mm(model.scale) * model.scale
    keep = residual <= search
    inliers = [assigned[k] for k in range(len(assigned)) if keep[k]]
    if len(inliers) < MIN_PADS or 2 * len(inliers) <= n or abs(model.rotation_deg) > MAX_ROTATION_DEG:
        return False
    outliers = [p for p in range(n) if p not in inliers]
    if not outliers:
        return False
    projected = model.project(points[outliers])
    centres = np.array([(c.x, c.y) for c in circles], float)
    # A pad landing on a circle the detector DID keep (typically the hole of
    # another pad) is a wrong pad, not a missed hole.
    if np.any(np.linalg.norm(projected[:, None, :] - centres[None, :, :], axis=2) <= search):
        return False
    radius = float(np.median([circles[assign[p]].r for p in inliers]))
    return all(_ring_at(mask, x, y, radius, search, budget) for x, y in projected)


# ---------------------------------------------------------------------------
# Per-image analysis, cached
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Analysis:
    """What the check needs from one image: the photo gate, the ink mask and the circles."""

    drawing: bool
    mask: np.ndarray | None
    circles: tuple[_Circle, ...]


_ANALYSES: OrderedDict[tuple[str, int, int], _Analysis | None] = OrderedDict()
_ANALYSES_LOCK = threading.Lock()


def _analyse(path: Path) -> _Analysis | None:
    """Gate and circles of one image file, cached by (resolved path, mtime, size).

    The extractor checks two to four competing readings against the same
    images; detection is the costly part and does not depend on the reading.
    None when the file cannot be read.
    """
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    with _ANALYSES_LOCK:
        if key in _ANALYSES:
            _ANALYSES.move_to_end(key)
            return _ANALYSES[key]
    image = _read_image(path)
    if image is None:
        analysis = None
    else:
        grey = _working_grey(image)
        mask, paper = _ink_mask(grey)
        if _is_line_drawing(grey, mask, paper):
            analysis = _Analysis(True, mask, tuple(_find_circles(grey, mask)))
        else:
            analysis = _Analysis(False, None, ())
    with _ANALYSES_LOCK:
        _ANALYSES[key] = analysis
        _ANALYSES.move_to_end(key)
        while len(_ANALYSES) > ANALYSIS_CACHE_SIZE:
            _ANALYSES.popitem(last=False)
    return analysis


def clear_analysis_cache() -> None:
    """@brief Forget the cached per-image analyses (gate, ink mask, circles).

    @note The cache is keyed by resolved path, modification time and size,
        so a changed file is analysed again anyway; this is for tests and
        for releasing memory.
    """
    with _ANALYSES_LOCK:
        _ANALYSES.clear()


def _better(fit: _Fit, best: _Fit | None) -> bool:
    """True when ``fit`` got further through the checks than ``best``."""
    return best is None or (_STAGES.index(fit.stage), -fit.max_error_mm) > \
        (_STAGES.index(best.stage), -best.max_error_mm)


def _check_image(name: str, analysis: _Analysis, points: np.ndarray,
                 body: tuple[float, float] | None) -> _ImageResult:
    """Run the matching of one reading on one analysed image."""
    if not analysis.drawing:
        return _ImageResult(name, False, 0, None)
    circles, mask = list(analysis.circles), analysis.mask
    if len(circles) < len(points):
        return _ImageResult(name, True, len(circles), None)
    best: _Fit | None = None
    for assign in _hypothesis_assignments(points, circles)[:MAX_ASSIGNMENTS]:
        fit = _fit_assignment(points, circles, assign, mask, body)
        if _better(fit, best):
            best = fit
        if best.stage == "confirmed":
            return _ImageResult(name, True, len(circles), best)
    if best is None or best.stage == "no_match":
        # Second pass with a wide gate: a reading that fits only after a
        # stretch (drawing not to scale, a whole axis read wrong) is
        # reported "distorted", which is a stronger statement than
        # "no_match" and must not be lost to the narrow confirmation gate.
        for assign in _hypothesis_assignments(points, circles,
                                              gate_fraction=DISTORTION_GATE)[:MAX_ASSIGNMENTS]:
            fit = _fit_assignment(points, circles, assign, mask, body)
            if fit.stage != "confirmed" and _better(fit, best):
                best = fit
    if best is not None and best.stage == "low_resolution":
        return _ImageResult(name, True, len(circles), best)
    # Every circle may confirm (a hole drawn with a label close by is still a
    # hole), but only circles drawn like holes may contradict: a page whose
    # circles are glyph counters says nothing against the reading.
    if sum(c.evidence for c in circles) < len(points):
        return _ImageResult(name, True, len(circles), None)
    if best is None or best.stage == "no_match":
        # Nothing fits: before calling the reading wrong, check whether a hole
        # the detector missed explains it. When every pad did meet a circle
        # (distorted, body_scale) a missed hole is not the explanation.
        budget = _Budget(MAX_RING_PROBES)
        try:
            for assign in _hypothesis_assignments(points, circles, max_misses=1)[:MAX_ASSIGNMENTS]:
                if _missed_hole_explains(points, circles, assign, mask, budget):
                    return _ImageResult(name, True, len(circles), None)
        except _BudgetExceeded:
            return _ImageResult(name, True, len(circles), _Fit("search_limit", math.nan, math.nan))
    if best is None:
        best = _Fit("no_match", math.inf, math.nan)
    return _ImageResult(name, True, len(circles), best)


def _summarise(results: Sequence[_ImageResult]) -> LayoutCheck:
    """Choose the overall non-confirmed answer from the per-image results.

    "not_confirmed" needs an image whose reading reached a contradicting
    stage (no_match, distorted, body_scale); otherwise the most informative
    unverifiable reason wins: search_limit > low_resolution >
    too_few_circles > not_a_drawing > no_image.
    """
    def rounded(value: float) -> float | None:
        """Finite values rounded to 0.1 um, others None."""
        return round(value, 4) if math.isfinite(value) else None

    contradicting = [r for r in results if r.fit is not None and r.fit.stage in _CONTRADICTING]
    if contradicting:
        decisive = max(contradicting, key=lambda r: (_STAGES.index(r.fit.stage), -r.fit.max_error_mm))
        fit = decisive.fit
        return LayoutCheck(NOT_CONFIRMED, fit.stage, decisive.name, rounded(fit.max_error_mm),
                           rounded(fit.tolerance_mm), decisive.circles)
    for reason in ("search_limit", "low_resolution"):
        flagged = [r for r in results if r.fit is not None and r.fit.stage == reason]
        if flagged:
            decisive = flagged[0]
            return LayoutCheck(UNVERIFIABLE, reason, decisive.name, rounded(decisive.fit.max_error_mm),
                               rounded(decisive.fit.tolerance_mm), decisive.circles)
    drawings = [r for r in results if r.drawing]
    if drawings:
        decisive = max(drawings, key=lambda r: r.circles)
        return LayoutCheck(UNVERIFIABLE, "too_few_circles", decisive.name, None, None, decisive.circles)
    if results:
        return LayoutCheck(UNVERIFIABLE, "not_a_drawing", None, None, None, 0)
    return LayoutCheck(UNVERIFIABLE, "no_image", None, None, None, 0)
