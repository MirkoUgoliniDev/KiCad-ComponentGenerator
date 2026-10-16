"""@brief Find the real component edges in the photo through edge detection.

The language model estimates well *which* components there are but often
gets wrong *where* they are and *how* they are oriented: two independent
estimates can end up overlapping, or an almost black component on an almost
black PCB (a common case: dark plastic case) is placed by eye without the
model really managing to read its boundaries.

A colour-threshold contour fails precisely in that case (black on black has
no colour step to segment). The luminance gradient, instead, is almost
always there, even between two slightly different blacks (glossy vs. matte,
plastic vs. solder mask): this is why **Canny** on the local contrast is
used here, not a global threshold.

Procedure, deterministic and independent of the module:

1. in the top-view photo the board is found by CONTRAST WITH THE BACKGROUND,
   whatever the colours: the background is estimated from the photo border,
   and a region is the board only if it does not touch the border on two or
   more sides, is surrounded by the background on every free side and has
   the declared proportions IN THE DECLARED DIRECTION (X = length). The
   px/mm scale is derived from its extents; an incompatible photo (side
   view, detail, board rotated by 90 degrees with respect to the
   dimensions) is rejected with an explicit reason, never cropped at random;
2. for each component estimated by the model a window is cut around the
   declared position, the edges are searched with Canny (thresholds from
   the window gradient and its noise, not from the luminance: valid for
   black, green, blue and white boards), dilated just enough to close the
   broken contours, and the closed contour most compatible by area and
   distance from the centre is searched;
3. the minimum rectangle containing it gives the orientation; position and
   extent are re-read on the Canny pixels enclosed by the contour, along
   that orientation, so the growth of the dilation does not inflate the
   measurement (error within one pixel at every scale). The angle is kept
   as measured: the CAD generator applies arbitrary rotations
   (`_rectangle_axes` has the branch for angles that are not cardinal). It
   is brought back to the cardinal only when the deviation is smaller than
   the uncertainty -- the residual tilt that the calibration admits on the
   board, or the angular resolution of the contour for that size -- because
   there an oblique part and a skewed board give the same measurement.

When the measurement is not reliable -- no useful contour, area too
different from the estimated one, excessive displacement from the estimate
-- the model estimate is kept: when it does NOT measure, the result is never
worse than not measuring.

Known limit, verified on real photos (not only on the synthetic cases of the
tests): when it measures, it is not infallible. On a dense board a small
plated hole (via) or an isolated solder spot can have edges sharp enough to
pass the filters and be taken for a component, moving the estimate to a
wrong position instead of leaving it unchanged. It is the same category of
error as the language model estimates (sometimes they are wrong), not a
defect fixable with another threshold: it must therefore be used as a second
opinion to review in the component table, not as an automatic correction to
trust blindly.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence
from .messages import tx

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - dipendenza opzionale (opencv-python)
    cv2 = None
    np = None

#: Maximum allowed gap between the length/width ratio of the board found and
#: that of the module: beyond it, the photo is not a top view.
MAX_BOARD_ASPECT_ERROR = 0.12
#: Maximum edge drift due to rasterisation/irregular contour.
#: The angular limit avoids accepting real rotations on small images.
#:
#: The drift grows with the contour length: an edge serrated by castellated
#: pads makes minAreaRect read a few tenths of a degree, which on a 770 px
#: long side is already 5.6 px. With an *absolute* threshold in pixels the
#: same board, perfectly aligned to the axes, passed at low resolution and was
#: rejected at high resolution: the criterion became the image size, not the
#: geometry. The allowance is therefore the larger of a minimum in pixels
#: (small images, where a few rasterisation pixels weigh a lot) and a
#: fraction of the long side. The angular limit stays and keeps dominating:
#: 1 deg is 1.75% drift, more than the ratio below.
MAX_BOARD_EDGE_DRIFT_PX = 5.0
MAX_BOARD_EDGE_DRIFT_RATIO = 0.01
MAX_BOARD_TILT_DEG = 1.0
#: Minimum resolution of the crop: below it, measuring packages makes no
#: sense. At 1.9 px/mm a 1 mm passive takes 2 px, and the inventory would
#: measure noise. It also unmasks false positives: a small dark rectangle
#: with the right aspect ratio passes every other filter, but not this one.
MIN_BOARD_SCALE_PX_PER_MM = 4.0
#: Minimum fraction of the image taken by the board. In a top view the board
#: is the subject: a candidate covering a negligible portion of it is an
#: inner detail (a connector, a label), not the module.
MIN_BOARD_IMAGE_FRACTION = 0.02
#: Minimum pixels on the short side of the smallest body for it to be
#: distinguishable. Exposed here (and reused by the inventory) so that the GUI
#: can judge a photo without importing the whole chain.
MIN_PACKAGE_PIXELS_HINT = 12
#: Short side, in mm, of the smallest body assumed mounted on the board when
#: judging whether a photo is fine enough. DECLARED ASSUMPTION, not a
#: measurement and not derived from a board: 0.5 mm is the short side of an
#: 0402 passive (1.0 x 0.5 mm), the smallest common package on mass-assembled
#: boards. A board with 0201 (0.3 mm) needs finer photos, one with only 0805
#: tolerates coarser ones: whoever knows the board can pass their own size
#: (photo_inventory._warn_if_too_coarse), and the warning always says against
#: which size it was given.
SMALLEST_EXPECTED_PART_MM = 0.5
#: CAP on how far one moves from the estimated position when looking for the
#: edges, beyond the component's half-footprint. It is not the window: that
#: stops halfway to the nearest body (see `search_margin_mm`). It stays as the
#: upper limit for isolated bodies, where no neighbour closes it.
SEARCH_MARGIN_MM = 2.0
#: Area of the contour found relative to the estimated one: outside this
#: range it is not the same component (too small, or too large because merged
#: with a neighbour by the morphological closing).
AREA_RATIO_RANGE = (0.4, 1.8)
#: Maximum accepted shift between estimated centre and measured centre.
MAX_SHIFT_MM = 2.0
#: Minimum fraction of the minimum rectangle (minAreaRect) actually filled by
#: the contour: a solid component fills almost all of it, a blob resulting
#: from the merging of several nearby pieces ("dumbbell" or "L" shape) fills
#: a much smaller fraction.
MIN_FILL_RATIO = 0.45
#: How far the elongation (long side / short side) of the contour may depart
#: from the estimated one: a merged blob is almost always much more or much
#: less elongated than the component it should represent.
MAX_ELONGATION_RATIO = 1.5
#: Position uncertainty of a contour point, in pixels. Declared fallback: it
#: is the quantisation of the contour (one pixel), not a measurement of this
#: camera's noise. It is used to derive the angular resolution of a body from
#: its size: a short body cannot declare a fine angle. It would become
#: unnecessary by estimating the edge noise from the photo itself.
CONTOUR_NOISE_PX = 1.0
#: How much `_edge_mask` inflates the contour relative to the body, per side:
#: the Canny ring after blurring lies about one pixel outside the edge and the
#: 3x3 dilation adds another. It must be removed from the measured short side
#: before judging how fine the angle can be, otherwise a small body looks
#: larger than it is and ends up credited with an angular precision it does
#: not have. Measured on synthetic rectangles from 10 to 84 px: the contour
#: comes out 4.4-4.8 px wider in total, so 2.0 per side is the value the
#: kernel predicts and also the one observed.
MORPH_GROWTH_PX = 2.0
#: Thickness, in pixels, of the band of edge pixels describing ONE side of a
#: body in `_edge_extent`. It is not a tuned threshold: the brightness step
#: falls between two adjacent pixels and Canny marks one of the two, so the
#: pixels of a side lie in a band one pixel wide from the outermost one.
#: The mean of that band lies within half a pixel of the true edge, per side:
#: the measured footprint stays within one pixel of the body's at every
#: scale, while the dilated contour inflated it by a constant amount in pixels
#: (+0.25 mm at 12 px/mm, +0.38 mm at 8 px/mm).
EDGE_SIDE_BAND_PX = 1.0
#: Thickness of the frame ring from which the background is estimated, as a
#: fraction of the short side of the image (minimum BACKGROUND_RING_MIN_PX).
#: In a top view the frame is background by construction: the board must lie
#: entirely within the shot. Declared fallback on the thickness: thin enough
#: not to enter the board with a normal margin, thick enough to give
#: thousands of samples even on a small photo.
BACKGROUND_RING_FRACTION = 0.02
BACKGROUND_RING_MIN_PX = 2
#: How many robust deviations (MAD scaled to sigma) beyond the frame median a
#: colour must be from the background not to be background. Declared
#: fallback, not tuned on a photo: 4 sigma would be the tail of a Gaussian
#: noise, but Delta E is a modulus with a right tail and on synthetic noise it
#: leaves ~0.5% of background pixels above threshold, isolated; they are
#: removed by the majority vote of `_contrast_mask`, not by this constant. On
#: a truly non-uniform background (vignetting, two surfaces) the frame
#: statistics are not enough: it is a declared limit.
BACKGROUND_SPREAD_SIGMAS = 4.0
#: Minimum colour distance (Delta E CIE76) below which two surfaces cannot be
#: told apart: the classic just-noticeable-difference (JND) threshold. It is
#: used only when the frame is perfectly uniform (drawings, renderings), where
#: the measured spread is zero and every antialias pixel would look "board".
MIN_BACKGROUND_DELTA_E = 2.3
#: Fraction of the band just OUTSIDE each free side of the board that must
#: resemble the background. The board is surrounded by the background; an
#: inner detail (an IC, a shield, a dense component area) is surrounded by
#: the board on at least one side. Simple majority: a cable, a protruding
#: connector or a dimension line crossing the edge covers a smaller part of
#: it, an inner side covers almost all of it.
MIN_OUTSIDE_BACKGROUND_SHARE = 0.5
#: Frame sides a candidate may touch. A region touching two or more of them is
#: the background itself (or a surface leaving the shot) and its true extent
#: is not measurable: it is never the board.
MAX_FRAME_SIDES_TOUCHED = 1


@dataclass
class BoardFrame:
    """@brief Board placement in the photo: centre in pixels and px/mm scale."""

    cx: float
    cy: float
    scale: float
    width_px: float
    height_px: float

    def to_px(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """@brief Convert board millimetres (origin at centre, Y down) to photo pixels.

        @param x_mm Board X coordinate, in mm.
        @param y_mm Board Y coordinate, in mm.
        @return (x_px, y_px) in the photo, in px.
        """
        # Footprint Y pointing down, like the image rows.
        return self.cx + x_mm * self.scale, self.cy + y_mm * self.scale

    def to_mm(self, x_px: float, y_px: float) -> tuple[float, float]:
        """@brief Convert photo pixels to board millimetres (origin at centre, Y down).

        @param x_px Photo X coordinate, in px.
        @param y_px Photo Y coordinate, in px.
        @return (x_mm, y_mm) in the board frame, in mm.
        """
        return (x_px - self.cx) / self.scale, (y_px - self.cy) / self.scale


#: Errors a single edge measurement is expected to raise on legitimate input:
#: OpenCV rejecting a degenerate window (cv2.error), and geometry that is
#: missing, non-numeric or non-finite (KeyError, TypeError, ValueError,
#: OverflowError from int() of an infinite span). They leave the estimate in
#: place like an unclear contour. Anything else is a defect of this module and
#: is counted separately in RefineReport.failed, never reported as «unclear».
def _expected_measure_errors():
    """Exception classes treated as an unmeasurable component, not as a bug."""
    errors = (KeyError, TypeError, ValueError, OverflowError)
    if cv2 is not None and isinstance(getattr(cv2, "error", None), type):
        errors += (cv2.error,)
    return errors


@dataclass
class RefineReport:
    """@brief Outcome of one edge-measurement pass over a list of components.

    @note ``skipped`` lists components whose contour was unclear or whose
        geometry could not be measured (the estimate is kept); ``failed``
        lists components where the measurement raised an UNEXPECTED error,
        as ``"<name>: <ExceptionType>: <message>"``. The estimate is kept in
        both cases, but only ``failed`` points at a defect to investigate.
    """
    measured: int = 0
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)


#: Maximum tilt one tries to straighten. Beyond it, the photo is not a
#: skewed top view but a perspective shot, which a rigid rotation cannot
#: correct.
MAX_DESKEW_DEG = 20.0
#: Verification band of the board contour, as a fraction of its short side in
#: pixels: `gap` skips the antialias transition and the castellated pads that
#: protrude by a few pixels, `band` is the thickness examined.
#: Declared fallback on the proportions (1% and 3%): they only serve to sample
#: a thin strip close to the edge, the verdict comes from the majority
#: (BOUNDARY_MAJORITY_SHARE), not from these values.
BOUNDARY_GAP_FRACTION = 0.01
BOUNDARY_GAP_MIN_PX = 2
BOUNDARY_BAND_FRACTION = 0.03
BOUNDARY_BAND_MIN_PX = 3
#: Majority required on each free side: outside the background must prevail,
#: inside what is not background must prevail.
BOUNDARY_MAJORITY_SHARE = 0.5

#: Codes of the reason why `locate_board` did not find the board.
BOARD_FOUND = "found"
BOARD_NOT_FOUND = "not_found"
BOARD_ROTATED_90 = "rotated_90"
#: No board with the declared proportions and MORE than one object separated
#: from the background with different proportions: which one is the board
#: cannot be decided.
BOARD_AMBIGUOUS = "ambiguous"
#: Quantisation pixels per side of the box (the same assumption as the scale
#: tolerance of the tests: one edge pixel per side): below 2 px of difference
#: between the two extents the orientation of an almost square contour is not
#: measured, it is edge noise.
ORIENTATION_QUANTIZATION_PX = 2.0


@dataclass(frozen=True)
class BackgroundModel:
    """@brief Colour of the photo background, estimated from the image frame.

    @note Colours are CIE L*a*b* with L in 0..100, so distances are Delta E
        CIE76 and mean the same for dark, light and saturated surfaces: a
        black board on a black mat and a green board on a white sheet are
        judged by the same rule.
    """

    #: Median background colour, (L, a, b).
    lab: tuple
    #: Median background colour in the photo's BGR, used to fill new canvas.
    bgr: tuple
    #: Delta E under which a pixel is indistinguishable from the background.
    tolerance: float


def _lab(image):
    """Immagine BGR a 8 bit in L*a*b* float (L 0..100): distanze in Delta E."""
    return cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)


def estimate_background(image) -> Optional[BackgroundModel]:
    """@brief Estimate the background colour and its spread from a ring along the image frame.

    @param image BGR photo (three channels).
    @return BackgroundModel, or None when OpenCV is missing, the image is not
        a three-channel picture or it is too small to hold a ring.
    @note A top view frames the whole board, so the frame is background by
        construction. Median and MAD are robust: dimension lines, a cable or
        one board side grazing the frame are a minority of the ring and do
        not move the estimate.
    @note Limit: one colour for the whole background. A strongly vignetted
        or two-surface background is only partly "background" under this
        model; the regions that differ touch the frame and are rejected as
        candidates, so the failure is a declared one, not a wrong crop.
    """
    if cv2 is None or image is None or getattr(image, "ndim", 0) != 3 or image.shape[2] != 3:
        return None
    height, width = image.shape[:2]
    ring = max(BACKGROUND_RING_MIN_PX,
               int(round(BACKGROUND_RING_FRACTION * min(height, width))))
    if min(height, width) <= 2 * ring:
        return None

    def frame_pixels(array):
        return np.concatenate([array[:ring].reshape(-1, 3), array[-ring:].reshape(-1, 3),
                               array[ring:-ring, :ring].reshape(-1, 3),
                               array[ring:-ring, -ring:].reshape(-1, 3)])

    samples = frame_pixels(_lab(image))
    centre = np.median(samples, axis=0)
    distance = np.linalg.norm(samples - centre, axis=1)
    median = float(np.median(distance))
    # MAD scaled to sigma (1.4826): same unit as a standard deviation.
    spread = 1.4826 * float(np.median(np.abs(distance - median)))
    tolerance = max(MIN_BACKGROUND_DELTA_E, median + BACKGROUND_SPREAD_SIGMAS * spread)
    bgr = tuple(int(v) for v in np.median(frame_pixels(image), axis=0))
    return BackgroundModel(lab=tuple(float(v) for v in centre), bgr=bgr,
                           tolerance=float(tolerance))


def _background_distance(image, background: BackgroundModel):
    """Delta E of every pixel from the background colour."""
    return np.linalg.norm(_lab(image) - np.array(background.lab, dtype=np.float32), axis=2)


def _clean_mask(mask, shape):
    """Closing (stitches back together) and opening (removes thin lines: dimensions, traces)."""
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    open_size = max(9, int(min(shape[:2]) * 0.02) | 1)
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (open_size, open_size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k)


def _board_mask(image):
    """Historical mask of the very dark pixels (grey < 70).

    It is no longer the criterion that decides what the board is: it is one
    of the segmentations tried, and each of its candidates must pass the same
    background-contrast checks (`_board_candidates`). It stays first for
    compatibility: on the dark-on-light photos where it was already correct
    it reproduces the earlier crop to the pixel, and with it the cached
    answers. The threshold 70 is inherited, not derived: on a dark background
    the mask takes the background, which touches the border and is discarded.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, dark = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY_INV)
    return _clean_mask(dark, image.shape)


def _contrast_mask(distance, background: BackgroundModel, shape):
    """Mask of what stands out from the background, whatever its colour.

    The Delta E distance is a magnitude, with a right tail: even beyond the
    tolerance some isolated noise pixels remain (measured ~0.5% on synthetic
    Gaussian noise). The 15x15 closing would weld them to the board as
    excrescences that widen its box. A 5x5 majority vote (median filter) is
    taken first: the board is a region, an isolated pixel is not. A straight
    edge stays where it is, a corner is rounded by one pixel without changing
    the box.
    """
    mask = np.where(distance > background.tolerance, 255, 0).astype("uint8")
    return _clean_mask(cv2.medianBlur(mask, 5), shape)


def _texture_mask(image):
    """Board mask by texture, not by colour.

    In a photograph the board is dense with components, silkscreen and
    traces, while a uniform background is smooth. The gradient energy
    separates the two even when the colours overlap: a light metal shield
    almost as bright as the supporting surface is full of edges, the surface
    is not.

    The morphological closing with a wide kernel stitches the inside of the
    board back together, shield included: what matters is the outer shape.
    Limit: a background that is itself textured (wood, cloth) does not
    separate this way; its blobs touch the border and are discarded.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    gradient = cv2.magnitude(cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3),
                             cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3))
    window = max(9, int(min(image.shape[:2]) * 0.012) | 1)
    energy = cv2.boxFilter(gradient, cv2.CV_32F, (window, window))
    energy = cv2.normalize(energy, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
    _, mask = cv2.threshold(energy, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    close = cv2.getStructuringElement(cv2.MORPH_RECT, (window * 3 | 1, window * 3 | 1))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close)
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (window, window))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_k)


def _segmentations(image, background: BackgroundModel, distance):
    """Masks in trial order, each with the indication whether the box must
    be brought to the middle of the contrast step (`_mid_step_box`).

    1. very dark pixels (historical, see `_board_mask`): box unchanged, to
       reproduce the earlier crops to the pixel;
    2. contrast with the background estimated from the border: the general
       criterion, valid for a green board on black, white on dark, red on grey;
    3. texture: fallback for the parts of the board having the same colour as
       the background (metal shields), where no colour mask sees the edge.
    """
    # Generator: a mask is computed only if the previous ones are not enough.
    yield _board_mask(image), False
    yield _contrast_mask(distance, background, image.shape), True
    yield _texture_mask(image), False


def _mid_step_box(distance, background: BackgroundModel, bx, by, bw, bh):
    """Box of the candidate with the edge at the middle of the background -> board step.

    The contrast mask uses the noise tolerance, that is the foot of the step:
    on a blurred edge (optics, resampling) it takes the whole gradient and
    widens the box by the blur radius on each side, a systematic scale error.
    The point of a blurred step that does not move with the blur is the one
    at half amplitude: here the candidate window is thresholded again at half
    the median contrast of the board (never below the noise tolerance) and
    its box is taken.
    """
    height, width = distance.shape[:2]
    _, gap = _side_bands(bx, by, bw, bh, width, height)
    interior = distance[by + gap:by + bh - gap, bx + gap:bx + bw - gap]
    if interior.size == 0:
        return bx, by, bw, bh
    level = max(background.tolerance, 0.5 * float(np.median(interior)))
    x0, y0 = max(0, bx - gap), max(0, by - gap)
    x1, y1 = min(width, bx + bw + gap), min(height, by + bh + gap)
    window = np.where(distance[y0:y1, x0:x1] > level, 255, 0).astype("uint8")
    # Frame of zeros wider than the closing kernel: without it, the closing
    # (erosion with an "infinite" border) does not erode from the window side
    # and the region grows up to the edge of the window itself.
    pad = 16
    window = cv2.copyMakeBorder(cv2.medianBlur(window, 5), pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=0)
    window = _clean_mask(window, distance.shape)
    contours, _ = cv2.findContours(window, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return bx, by, bw, bh
    rx, ry, rw, rh = cv2.boundingRect(max(contours, key=cv2.contourArea))
    return x0 + rx - pad, y0 + ry - pad, rw, rh


def _frame_sides_touched(bx, by, bw, bh, width, height) -> int:
    """How many sides of the photo border the box reaches."""
    return sum((bx <= 0, by <= 0, bx + bw >= width, by + bh >= height))


def _side_bands(bx, by, bw, bh, width, height):
    """Pairs (outer band, inner band) for every side not on the border."""
    short = min(bw, bh)
    gap = max(BOUNDARY_GAP_MIN_PX, int(round(BOUNDARY_GAP_FRACTION * short)))
    band = max(BOUNDARY_BAND_MIN_PX, int(round(BOUNDARY_BAND_FRACTION * short)))
    x1, y1 = bx + bw, by + bh
    rows, cols = slice(by, y1), slice(bx, x1)
    sides = []
    left = (max(0, bx - gap - band), max(0, bx - gap))
    if left[1] > left[0]:
        sides.append(((rows, slice(*left)), (rows, slice(bx + gap, min(x1, bx + gap + band)))))
    right = (min(width, x1 + gap), min(width, x1 + gap + band))
    if right[1] > right[0]:
        sides.append(((rows, slice(*right)), (rows, slice(max(bx, x1 - gap - band), x1 - gap))))
    top = (max(0, by - gap - band), max(0, by - gap))
    if top[1] > top[0]:
        sides.append(((slice(*top), cols), (slice(by + gap, min(y1, by + gap + band)), cols)))
    bottom = (min(height, y1 + gap), min(height, y1 + gap + band))
    if bottom[1] > bottom[0]:
        sides.append(((slice(*bottom), cols), (slice(max(by, y1 - gap - band), y1 - gap), cols)))
    return sides, gap


def _surrounded_by_background(distance, background: BackgroundModel, bx, by, bw, bh) -> bool:
    """The box separates, one side at a time, what is not background from the background.

    Outside: a pixel counts as background if it is closer to the background
    than to the board (half the contrast step, the step-edge criterion), or
    anyway indistinguishable from the background. So a light halo, a faint
    shadow or a pad protruding by one pixel stay "outside", while the board
    around an inner detail does not.

    Inside: the band must be mostly distinguishable from the background. A
    mask that puts a side where there is background inside has glued
    something external to the board (the text of a dimension, a protruding
    connector): the box is wrong. It holds for every segmentation: on the
    dimensioned reference photo the texture glued the dimensions to the board
    and gave a box with a 7% aspect deviation, within tolerance, which passed
    silently without this check. Declared limit: a board whose side is
    occupied for more than half by a surface indistinguishable from the
    background (metal shield up to the edge on a surface of the same colour)
    is rejected, not measured.

    A side lying on the border cannot be checked and is skipped; if no
    checkable side remains the board is not demonstrably surrounded by the
    background and the candidate is discarded.
    """
    height, width = distance.shape[:2]
    sides, gap = _side_bands(bx, by, bw, bh, width, height)
    if not sides:
        return False
    interior = distance[by + gap:by + bh - gap, bx + gap:bx + bw - gap]
    if interior.size == 0:
        interior = distance[by:by + bh, bx:bx + bw]
    outside_tolerance = max(background.tolerance, 0.5 * float(np.median(interior)))
    for outer, inner in sides:
        outside = distance[outer]
        if outside.size and np.mean(outside <= outside_tolerance) < BOUNDARY_MAJORITY_SHARE:
            return False
        inside = distance[inner]
        if inside.size and np.mean(inside <= background.tolerance) >= BOUNDARY_MAJORITY_SHARE:
            return False
    return True


def _aspect_error(width_px, height_px, body_length, body_width) -> float:
    """Relative deviation of the measured X/Y ratio from the declared one, in the declared direction.

    The comparison is made on the long side / short side ratio OF THE
    DIMENSIONS: with L >= W the board must be horizontal (w/h against L/W, as
    always), with L < W vertical (h/w against W/L). A board with the wrong
    direction gives a ratio < 1 against one >= 1 and is discarded, without
    swapping the axes.
    """
    if body_length >= body_width:
        found, expected = width_px / height_px, body_length / body_width
    else:
        found, expected = height_px / width_px, body_width / body_length
    return abs(found - expected) / expected


def deskew_board(image, body_length: float, body_width: float):
    """@brief Rotate a tilted board photo upright, or return None when not needed or not possible.

    `find_board` does not rectify the rotation, so as not to distort scale
    and coordinates: it therefore tolerates less than one degree. On a photo
    taken by hand two or three degrees are enough to get the image rejected,
    and this is why a real high-resolution photo was discarded in favour of a
    low-resolution drawing.

    Here the tilt of the largest region compatible with the module
    proportions (in either direction: the direction is judged later by
    `find_board`) and not touching the border on two or more sides is
    measured, and the whole image is rotated. It is a rigid transformation:
    lengths and proportions remain the true ones, so the scale derived after
    straightening is just as valid. The canvas is enlarged, so the rotation
    does not cut the corners, and filled with the estimated background
    colour: a fixed white fill would create, on a dark background, an edge
    that does not exist.

    @param image BGR photo.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return The straightened image, or None when there is nothing to
        straighten or the tilt is too large for a top view.
    """
    if cv2 is None or not all(math.isfinite(v) and v > 0 for v in (body_length, body_width)):
        return None
    background = estimate_background(image)
    if background is None:
        return None
    expected = max(body_length, body_width) / min(body_length, body_width)
    height, width = image.shape[:2]
    distance = _background_distance(image, background)
    contours = []
    for mask, _ in _segmentations(image, background, distance):
        found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours.extend(found)
    best = None
    for contour in contours:
        (cx, cy), (rw, rh), angle = cv2.minAreaRect(contour)
        if min(rw, rh) < 20:
            continue
        if _frame_sides_touched(*cv2.boundingRect(contour), width, height) > MAX_FRAME_SIDES_TOUCHED:
            continue
        # On the rotated rectangle the ratio is the board's true one: the
        # axis-aligned bounding box of a skewed rectangle is always stubbier.
        found = max(rw, rh) / min(rw, rh)
        if abs(found - expected) / expected > MAX_BOARD_ASPECT_ERROR:
            continue
        area = rw * rh
        if best is None or area > best[0]:
            best = (area, cx, cy, rw, rh, angle)
    if best is None:
        return None
    _, cx, cy, rw, rh, angle = best
    # minAreaRect reports the angle of the side, not of the board: the
    # correction is brought into the quadrant closest to zero.
    tilt = angle % 90
    if tilt > 45:
        tilt -= 90
    if abs(tilt) <= MAX_BOARD_TILT_DEG or abs(tilt) > MAX_DESKEW_DEG:
        return None
    matrix = cv2.getRotationMatrix2D((cx, cy), tilt, 1.0)
    radians = math.radians(abs(tilt))
    new_w = int(width * math.cos(radians) + height * math.sin(radians)) + 2
    new_h = int(width * math.sin(radians) + height * math.cos(radians)) + 2
    matrix[0, 2] += new_w / 2 - cx
    matrix[1, 2] += new_h / 2 - cy
    return cv2.warpAffine(image, matrix, (new_w, new_h), flags=cv2.INTER_CUBIC,
                          borderValue=background.bgr)


@dataclass(frozen=True)
class BoardSearch:
    """@brief Outcome of the board search: the frame, or why there is none."""

    #: Board reference in the photo, None when the board was not found.
    frame: Optional[BoardFrame]
    #: BOARD_FOUND, BOARD_NOT_FOUND or BOARD_ROTATED_90.
    reason: str

    def message(self, body_length: float, body_width: float) -> str:
        """@brief Translated explanation for the user; empty when the board was found.

        @param body_length Declared board extent along X, in mm.
        @param body_width Declared board extent along Y, in mm.
        @return Log line in the active language.
        """
        if self.reason == BOARD_FOUND:
            return ""
        if self.reason == BOARD_AMBIGUOUS:
            return tx("Scheda non riconosciuta nella foto: nessun oggetto ha le proporzioni "
                      "{length:.2f} x {width:.2f} mm e ce n'e' piu' di uno separato dallo "
                      "sfondo. Ritagliare la foto lasciando solo la scheda.",
                      length=body_length, width=body_width)
        if self.reason == BOARD_ROTATED_90:
            return tx("La scheda nella foto e' ruotata di 90 gradi rispetto alle quote "
                      "dichiarate: la lunghezza ({length:.2f} mm) corre lungo X, cioe' in "
                      "orizzontale nella foto, la larghezza ({width:.2f} mm) in verticale. "
                      "Ruotare la foto di 90 gradi, oppure verificare che lunghezza e "
                      "larghezza non siano state inserite al contrario.",
                      length=body_length, width=body_width)
        return tx("Scheda non riconosciuta nella foto: nessuna regione distinta dallo "
                  "sfondo, interamente inquadrata e con le proporzioni "
                  "{length:.2f} x {width:.2f} mm.",
                  length=body_length, width=body_width)


def _search(image, body_length, body_width, background, distance) -> Optional[BoardFrame]:
    """First segmentation that yields valid candidates; the largest wins."""
    for mask, mid_step in _segmentations(image, background, distance):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = _board_candidates(contours, image, body_length, body_width,
                                       distance, background, mid_step)
        if candidates:
            return _frame_from(candidates, body_length, body_width)
    return None


def _orientation_mismatch(width_px, height_px, body_length, body_width) -> bool:
    """The measured long side lies on the axis opposite to the declared one.

    Only if both directions are determined: equal dimensions do not declare
    one, and a contour whose extents differ by no more than
    ORIENTATION_QUANTIZATION_PX does not measure one.
    """
    if body_length == body_width or abs(width_px - height_px) <= ORIENTATION_QUANTIZATION_PX:
        return False
    return (width_px > height_px) != (body_length > body_width)


def _search_any_aspect(image, body_length, body_width, background, distance):
    """Second pass: the board with proportions different from the declared ones.

    A top view resized out of proportion (or photographed with pixels that
    are not square) is still a top view: the scale is measured along each
    axis, X on the length and Y on the width, and the consumers already read
    the coordinates axis by axis (`isotropic_board` resamples where a single
    scale is needed). Here the aspect ratio stops identifying the board; the
    structural criteria identify it, all still applied by `_board_candidates`
    (surrounded by the background, at most one border side, not tilted,
    minimum scale and area), plus uniqueness: without the ratio nothing
    distinguishes two objects on the table, so if there is more than one the
    answer is BOARD_AMBIGUOUS, not the largest.

    @param image BGR photo.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @param background Background model estimated from the photo border.
    @param distance Delta E distance of every pixel from the background.
    @return (reason, frame or None) with reason one of the BOARD_* codes.
    @note The deformation is not bounded: any threshold would be calibrated
        on the photos seen so far. It is declared in the log
        (`aspect_deformation`), because either the photo is deformed or the
        declared dimensions are wrong, and from the photo alone the two
        causes cannot be told apart.
    """
    for mask, mid_step in _segmentations(image, background, distance):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = _board_candidates(contours, image, body_length, body_width,
                                       distance, background, mid_step, check_aspect=False)
        if not candidates:
            continue
        if len(candidates) > 1:
            return BOARD_AMBIGUOUS, None
        _, _, _, _, w, h = candidates[0]
        if _orientation_mismatch(w, h, body_length, body_width):
            return BOARD_ROTATED_90, None
        return BOARD_FOUND, _frame_from(candidates, body_length, body_width)
    return BOARD_NOT_FOUND, None


def aspect_deformation(frame: BoardFrame, body_length: float, body_width: float) -> float:
    """@brief Relative gap between the X and Y scales of a board frame.

    @param frame Board found by `locate_board`.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return (px/mm on X) / (px/mm on Y) - 1: 0 for an undistorted view,
        negative when the photo is squeezed along X.
    """
    return (frame.width_px / body_length) / (frame.height_px / body_width) - 1.0


def locate_board(image, body_length: float, body_width: float) -> BoardSearch:
    """@brief Locate the board by contrast with the background and say why it failed, if it did.

    @param image BGR photo.
    @param body_length Declared board extent along X (horizontal in the photo), in mm.
    @param body_width Declared board extent along Y (vertical in the photo), in mm.
    @return BoardSearch with the frame, or with the reason it is missing.
    @note A board is a region that differs from the background estimated on
        the image frame, touches at most one side of the frame, is surrounded
        by background on every free side, is axis-aligned within
        MAX_BOARD_TILT_DEG, has the declared X/Y proportions IN THE DECLARED
        ORIENTATION within MAX_BOARD_ASPECT_ERROR, at least
        MIN_BOARD_SCALE_PX_PER_MM and MIN_BOARD_IMAGE_FRACTION of the photo.
        When nothing qualifies there is no frame: a wrong rectangle is never
        returned in its place.
    @note No image is rotated to guess the orientation. When the board only
        fits with the axes swapped the reason is BOARD_ROTATED_90, so the user
        can rotate the photo or correct the dimensions.
    """
    if cv2 is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                              and math.isfinite(v) and v > 0
                              for v in (body_length, body_width)):
        return BoardSearch(None, BOARD_NOT_FOUND)
    background = estimate_background(image)
    if background is None:
        return BoardSearch(None, BOARD_NOT_FOUND)
    distance = _background_distance(image, background)
    frame = _search(image, body_length, body_width, background, distance)
    if frame is not None:
        return BoardSearch(frame, BOARD_FOUND)
    if body_length != body_width and _search(image, body_width, body_length,
                                             background, distance) is not None:
        return BoardSearch(None, BOARD_ROTATED_90)
    reason, frame = _search_any_aspect(image, body_length, body_width, background, distance)
    return BoardSearch(frame, reason)


def find_board(image, body_length: float, body_width: float) -> Optional[BoardFrame]:
    """@brief Locate the board rectangle in the photo; None when it is not a usable top view.

    The reason for the rejection is given by `locate_board`.

    @param image BGR photo.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return BoardFrame whose width_px/height_px are the X/Y extents, or None.
    """
    return locate_board(image, body_length, body_width).frame


def _board_candidates(contours, image, body_length, body_width, distance, background,
                      mid_step=False, check_aspect=True):
    """Rectangles compatible with the module, filtered as the contract requires.

    With `check_aspect` False the aspect ratio does not filter (second pass,
    `_search_any_aspect`): every other criterion remains.
    """
    # Module photos often come with dimension annotations: lines and arrows
    # can form the largest contour, but with a ratio very different from the
    # board's real one. So we choose the largest candidate among the
    # geometrically compatible ones, instead of always using max(contourArea).
    candidates = []
    height, width = image.shape[:2]
    for contour in contours:
        (_, _), (rw, rh), angle = cv2.minAreaRect(contour)
        if min(rw, rh) < 20:
            continue
        tilt = min(abs(angle) % 90, 90 - abs(angle) % 90)
        # BoardFrame does not correct the rotation: tolerate only a marginal
        # drift on the long side, not tilts that distort scale and XY.
        # The allowance scales with the contour, otherwise the filter would
        # measure the image resolution instead of the board tilt.
        edge_drift = max(rw, rh) * math.sin(math.radians(tilt))
        drift_allowance = max(MAX_BOARD_EDGE_DRIFT_PX,
                              MAX_BOARD_EDGE_DRIFT_RATIO * max(rw, rh))
        if tilt > MAX_BOARD_TILT_DEG or edge_drift > drift_allowance:
            continue
        bx, by, bw, bh = cv2.boundingRect(contour)
        if mid_step:
            bx, by, bw, bh = _mid_step_box(distance, background, bx, by, bw, bh)
        w, h = float(bw), float(bh)
        # Consumers use X = declared length. The orientation is compared with
        # that of the dimensions: no implicit axis swap.
        error = _aspect_error(w, h, body_length, body_width)
        if check_aspect and error > MAX_BOARD_ASPECT_ERROR:
            continue
        # The aspect ratio alone does not identify the board: on a real photo
        # a connector or a dark label can have the same ratio. Without these
        # filters the wrong candidate was silently accepted and calibrated the
        # whole inventory on a wrong crop -- a silent error, worse than a
        # rejection.
        if w / body_length < MIN_BOARD_SCALE_PX_PER_MM:
            continue
        if (w * h) < MIN_BOARD_IMAGE_FRACTION * height * width:
            continue
        # A region reaching two or more sides of the frame is the background
        # (or leaves the shot): its extent is not measurable.
        if _frame_sides_touched(bx, by, bw, bh, width, height) > MAX_FRAME_SIDES_TOUCHED:
            continue
        # The board is surrounded by the background; an inner detail with the
        # same proportions is not.
        if not _surrounded_by_background(distance, background, bx, by, bw, bh):
            continue
        candidates.append((cv2.contourArea(contour), error, bx, by, w, h))
    return candidates


def _frame_from(candidates, body_length, body_width):
    """The largest candidate becomes the board reference.

    width_px and height_px are the extents along the image X and Y, in the
    declared direction (X = length): for a board declared vertical width_px
    is the short side.
    """
    _, _, bx, by, w, h = max(candidates, key=lambda item: item[0])
    cx, cy = bx + w / 2, by + h / 2
    scale = (w / body_length + h / body_width) / 2
    return BoardFrame(cx=cx, cy=cy, scale=scale, width_px=w, height_px=h)


#: How many noise standard deviations the gradient must exceed for Canny to
#: take it even as a weak edge (low threshold). Declared fallback, not tuned
#: on a photo: for a gradient of pure Gaussian noise the modulus exceeds 3
#: sigma with probability exp(-4.5) ~ 1% per pixel, and non-maximum
#: suppression leaves a fraction of it; on synthetic noise (sigma 1-6, grey
#: 22-225) no false measurement. A model of the camera noise would make it
#: unnecessary.
EDGE_NOISE_SIGMAS = 3.0


@functools.lru_cache(maxsize=1)
def _gradient_gain() -> float:
    """Standard deviation of the Sobel gradient (ksize 3) of unit white noise
    after the 3x3 Gaussian blur used by `_canny_edges`.

    It is measured by applying the same two filters to an impulse: the L2
    norm of the response is the exact gain of the chain, no copied number.
    """
    impulse = np.zeros((11, 11), np.float32)
    impulse[5, 5] = 1.0
    response = cv2.Sobel(cv2.GaussianBlur(impulse, (3, 3), 0), cv2.CV_32F, 1, 0, ksize=3)
    return float(np.sqrt(np.sum(response ** 2)))


def _noise_sigma(gray) -> float:
    """Grey-level noise of the window (Immerkaer estimator, 1996).

    The kernel [[1,-2,1],[-2,4,-2],[1,-2,1]] is the product of two second
    derivatives: on a straight edge it responds zero, so the edge of a body
    does not pass for noise. The median (instead of the mean of the original
    estimator) also removes the weight of corners and lettering. The minimum
    is the 8-bit quantization noise (1/sqrt(12) levels): a synthetic image
    without noise does not get a zero threshold.
    """
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    response = cv2.filter2D(gray.astype(np.float32), -1, kernel,
                            borderType=cv2.BORDER_REFLECT)[1:-1, 1:-1]
    # 0.6745 = median of |N(0,1)|; 6 = L2 norm of the kernel.
    sigma = float(np.median(np.abs(response))) / (0.6745 * 6.0) if response.size else 0.0
    return max(sigma, 1.0 / math.sqrt(12.0))


def _otsu_level(values) -> float:
    """Otsu threshold on float values (256 bins between minimum and maximum)."""
    if values.size == 0:
        return 0.0
    hist, bounds = np.histogram(values, bins=256)
    centres = (bounds[:-1] + bounds[1:]) / 2
    below = np.cumsum(hist).astype(np.float64)
    moment = np.cumsum(hist * centres)
    total, mean_total = below[-1], moment[-1]
    above = total - below
    valid = (below > 0) & (above > 0)
    if not np.any(valid):
        return float(centres[-1])
    between = np.zeros_like(centres, dtype=np.float64)
    between[valid] = ((mean_total * below[valid] - moment[valid] * total) ** 2
                      / (below[valid] * above[valid]))
    return float(centres[int(np.argmax(between))])


def canny_thresholds(gray, blurred=None) -> tuple:
    """@brief Canny hysteresis thresholds from the gradient statistics of a window.

    @param gray Grey window (8 bit), as it comes from the photo: its noise is
        measured here, before any smoothing.
    @param blurred The same window smoothed as Canny will see it (3x3
        Gaussian); computed from `gray` when omitted.
    @return (low, high) in the units of Canny's L1 Sobel magnitude.
    @note Canny compares GRADIENT magnitudes, so its thresholds must come from
        gradients, not from brightness. The former rule (low/high from the
        median intensity) set high = 255 on a white board: a body had to differ
        by ~85 grey levels to count as an edge there, while on a black board 20
        were enough -- fine measurement switched off on light boards.
    @note high = Otsu split of the window's gradient magnitudes (flat surfaces
        versus edges: the two populations are there whatever the colours), low =
        high / 2 (Canny's own 2:1 hysteresis ratio). Both are kept above the
        gradient that pure noise produces, EDGE_NOISE_SIGMAS standard deviations
        of the measured noise (low) and twice that (high): a window with nothing
        in it gives no edges instead of an Otsu split of its noise.
    """
    if blurred is None:
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.abs(gx) + np.abs(gy)          # same L1 norm as cv2.Canny
    # L1 modulus of two components with deviation s: it stays below sqrt(2)
    # times the Euclidean modulus, whose tail beyond k*s is exp(-k^2/2)
    # (Rayleigh). The gain already includes the blur: the noise is measured
    # before.
    floor = EDGE_NOISE_SIGMAS * math.sqrt(2.0) * _noise_sigma(gray) * _gradient_gain()
    split = _otsu_level(magnitude.ravel())
    return max(split / 2.0, floor), max(split, 2.0 * floor)


def _canny_edges(window) -> "np.ndarray":
    """Canny edges of the window, with thresholds from the gradient (see canny_thresholds)."""
    gray = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    low, high = canny_thresholds(gray, blurred)
    return cv2.Canny(blurred, low, high)


def _closed_edges(edges) -> "np.ndarray":
    """Minimal closing of the edges: dilation and closing with a 3x3 kernel.

    One pass only, minimal kernel: on a dense board (close components, each
    with its own silkscreen frame) a more aggressive closing welds the
    contours of ADJACENT parts into a single blob, which would then replace
    the estimate with the wrong shape (the group, not the part).
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    edges = cv2.dilate(edges, kernel, iterations=1)
    return cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)


def _edge_mask(window) -> "np.ndarray":
    """Closed, usable contours in the window: Canny + morphological closing.

    Canny works on the local gradient, not on an absolute threshold: it
    separates a black component from a black PCB if there is even a minimal
    reflectance difference between the two surfaces, something a colour
    threshold cannot do. Its thresholds come from the window gradient
    (`canny_thresholds`), not from the luminance.
    """
    return _closed_edges(_canny_edges(window))


def _edge_extent(edges, contour, rotation_deg):
    """Centre and extent of the TRUE (undilated) edges enclosed by the chosen contour.

    The contour comes from the dilated and closed mask, so it is wider than
    the body by as much as the morphology inflated it (about MORPH_GROWTH_PX
    on each side, but it depends on the kernel and the pose). Instead of
    subtracting a constant, the body is re-read on the Canny pixels enclosed
    by the contour, projected on the declared long axis (`rotation_deg`, the
    same the measurement reports) and on its perpendicular: the growth
    disappears along each axis, whatever the kernel, and the edge
    quantization error remains. Projecting on the REPORTED axes, not on the
    raw ones of minAreaRect, keeps extent and orientation consistent: on a
    small body the raw rectangle can be skewed by a few degrees that the
    measurement then brings back to the cardinal (see _axis_rotation).

    Returns (cx, cy, length, width) in window pixels, or None if the contour
    does not enclose enough edge pixels.
    """
    filled = np.zeros(edges.shape[:2], dtype="uint8")
    cv2.drawContours(filled, [contour], -1, 255, thickness=-1)
    ys, xs = np.nonzero((edges > 0) & (filled > 0))
    if xs.size < 4:
        return None
    points = np.stack([xs, ys], axis=1).astype(np.float64)
    # Image rows pointing down: it is the same orientation as the minAreaRect
    # angles that rotation_deg comes from.
    angle = math.radians(float(rotation_deg))
    along = np.array([math.cos(angle), math.sin(angle)])
    across = np.array([-math.sin(angle), math.cos(angle)])
    projections = [points @ along, points @ across]
    spans, middle = [], np.zeros(2)
    for index, axis in enumerate((along, across)):
        projection, other = projections[index], projections[1 - index]
        # The brightness step falls BETWEEN two pixels and Canny marks one,
        # sometimes the inner one and sometimes the outer one: the edge of a
        # side is the mean of the pixels within EDGE_SIDE_BAND_PX of its
        # extreme, not the extreme itself (which on a long side always takes
        # the outermost pixel, on a short one not: the error would depend on
        # the size). Corners are left out: the pixels of the two perpendicular
        # sides falling there are already inside the body and on the short
        # side of a small passive they would shift its mean inwards.
        core = ((other > other.min() + EDGE_SIDE_BAND_PX + 1e-9)
                & (other < other.max() - EDGE_SIDE_BAND_PX - 1e-9))
        extreme_low, extreme_high = float(projection.min()), float(projection.max())
        sides = []
        for band in (projection <= extreme_low + EDGE_SIDE_BAND_PX + 1e-9,
                     projection >= extreme_high - EDGE_SIDE_BAND_PX - 1e-9):
            chosen = band & core if np.any(band & core) else band
            sides.append(float(projection[chosen].mean()))
        low, high = sides
        spans.append(max(high - low, 1.0))
        middle += axis * (low + high) / 2
    return float(middle[0]), float(middle[1]), spans[0], spans[1]


def _forbidden_mask(image, frame: BoardFrame, exclude_rects_mm) -> "np.ndarray":
    """Pixels to ignore: outside the board outline and over the contact
    pads (shiny metal edges, easy to confuse with those of a component)."""
    forbidden = np.ones(image.shape[:2], dtype="uint8")
    margin = 0.15 * frame.scale
    x0 = int(frame.cx - frame.width_px / 2 + margin)
    x1 = int(frame.cx + frame.width_px / 2 - margin)
    y0 = int(frame.cy - frame.height_px / 2 + margin)
    y1 = int(frame.cy + frame.height_px / 2 - margin)
    forbidden[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
    for rx0, ry0, rx1, ry1 in exclude_rects_mm or ():
        px0, py0 = frame.to_px(rx0, ry0)
        px1, py1 = frame.to_px(rx1, ry1)
        forbidden[int(max(0, py0)):int(max(0, py1)), int(max(0, px0)):int(max(0, px1))] = 1
    return forbidden


def _body_rect_mm(component) -> tuple:
    """Axis-aligned box of the estimated body, in millimetres."""
    length, width = float(component["length_mm"]), float(component["width_mm"])
    rotation = math.radians(float(component.get("rotation_deg") or 0.0))
    span_x = length * abs(math.cos(rotation)) + width * abs(math.sin(rotation))
    span_y = length * abs(math.sin(rotation)) + width * abs(math.cos(rotation))
    x, y = float(component["x_mm"]), float(component["y_mm"])
    return (x - span_x / 2, y - span_y / 2, x + span_x / 2, y + span_y / 2)


def _rect_gap_mm(first, second) -> float:
    """Distance between two boxes, in mm; zero when they touch or overlap."""
    dx = max(second[0] - first[2], first[0] - second[2], 0.0)
    dy = max(second[1] - first[3], first[1] - second[3], 0.0)
    return math.hypot(dx, dy)


def search_margin_mm(gap_mm: float, scale_px_per_mm: float) -> float:
    """@brief How far to look beyond the estimated body when searching for its edges.

    @param gap_mm Distance from the nearest estimated body, in mm; zero if they touch.
    @param scale_px_per_mm Scale of the calibrated photograph, in px/mm.
    @return Millimetres beyond the half extent, for the window side.
    @note The zone of competence of a body ends where that of another
        begins: one looks at most halfway to the neighbour. It is not a
        hand-picked threshold -- the density of the board decides it, so on a
        dense module the window is tiny and on a sparse board it opens up to
        the SEARCH_MARGIN_MM ceiling.
    @note The minimum is not arbitrary: it is how much `_edge_mask` inflates
        the contour with respect to the body. With a narrower window one
        would cut one's own contour and measure nothing any more.
    @note Why it is needed: with a fixed 2 mm window, on a 55 px/mm photo one
        looks at 110 pixels of neighbourhood on each side. Measured on the
        123 proposals of the test module, where the median distance between
        proposals is zero: the morphological closing merged the body with
        its surroundings (median contour 9 times the proposal area) and of
        the 12 measurements that passed the filters the median displacement
        was 1.44 mm, that is the detection latched onto the neighbour instead
        of refining the body. With this rule the measurements become 71 and
        the median displacement 0.08 mm.
    """
    floor = MORPH_GROWTH_PX / max(float(scale_px_per_mm), 1e-6)
    return max(floor, min(SEARCH_MARGIN_MM, float(gap_mm) / 2))


def angular_resolution_deg(short_side_px: float) -> float:
    """@brief Finest angle a contour of this size can declare.

    @param short_side_px Short side of the minimum rectangle, in pixels,
        already measured on the contour inflated by the morphological closing.
    @return Degrees: below this value the tilt cannot be told apart from the
        position uncertainty of the contour points.
    @note A 0402 chip 10 px tall cannot say it is rotated by two degrees: it
        is enough that one edge point is off by one pixel. An 800 px long
        module can. The value comes from the geometry of the contour, not
        from a threshold chosen so that one case falls inside it.
    """
    body_px = float(short_side_px) - 2 * MORPH_GROWTH_PX
    return math.degrees(math.atan2(CONTOUR_NOISE_PX, max(body_px, 1.0)))


def _axis_rotation(angle: float, rw: float, rh: float, short_side_px: float) -> float:
    """@brief Orientation of the long side, in degrees, from a minAreaRect.

    @param angle Angle returned by cv2.minAreaRect, in degrees, referred to
        any side of the rectangle.
    @param rw Width of the minimum rectangle, in pixels.
    @param rh Height of the minimum rectangle, in pixels.
    @param short_side_px Short side, in pixels, to know how fine the angle can be.
    @return Rotation in [0, 180) degrees, already referred to the long side.
    @note minAreaRect is ambiguous by 90 degrees because it does not say
        which side it took as reference: the ambiguity is removed by always
        taking the long side. The POLARITY remains undetermined (which end
        is pin 1, which way the mating faces): that cannot be derived from
        the extent and must be asked of the model, not deduced here.
    @note A tilt smaller than the uncertainty -- the residual that the
        calibration itself admits on the board, or the angular resolution of
        the contour, whichever is larger -- is brought back to the cardinal.
        It is not a rounding of convenience: below that threshold a truly
        oblique body and a slightly skewed board give the same measurement,
        and declaring an angle would be inventing precision.
    """
    axis = (angle if rw >= rh else angle + 90) % 180
    cardinal = 0.0 if (axis < 45 or axis > 135) else 90.0
    reference = 0.0 if axis < 45 else 180.0 if axis > 135 else 90.0
    deviation = axis - reference
    if abs(deviation) <= max(MAX_BOARD_TILT_DEG, angular_resolution_deg(short_side_px)):
        return cardinal
    return round(axis, 1)


def refine_component_edges(image, frame: BoardFrame, component: dict, forbidden=None,
                          margin_mm: Optional[float] = None) -> Optional[dict]:
    """@brief Measure the real edges of one component in the photo, or return None when unclear.

    @param image BGR photo.
    @param frame Board reference in the photo.
    @param component Estimate with x_mm, y_mm, length_mm, width_mm, rotation_deg.
    @param forbidden Optional mask (1 = ignore) of the whole photo.
    @param margin_mm Search margin beyond the half extent, in mm; None = SEARCH_MARGIN_MM.
    @return The measured geometry {x_mm, y_mm, length_mm, width_mm,
        rotation_deg}, or None when the window holds no contour clear enough
        and compatible with the starting estimate.
    @note The contour is chosen on the closed (dilated) edge mask, and the
        orientation read on its minimum rectangle; centre and size are then
        read on the undilated Canny pixels it encloses, along that orientation
        (_edge_extent). Measured sizes are therefore not inflated by the
        morphological growth, which is constant in pixels and so worth more
        millimetres the coarser the photo.
    """
    length = float(component["length_mm"])
    width = float(component["width_mm"])
    rotation = float(component.get("rotation_deg") or 0.0)
    # Footprint in the image frame (X/Y), not in the part frame.
    # It holds for any rotation; on the cardinal ones it reduces exactly to
    # swapping length and width.
    cosine = abs(math.cos(math.radians(rotation)))
    sine = abs(math.sin(math.radians(rotation)))
    span_x = length * cosine + width * sine
    span_y = length * sine + width * cosine

    # Search window: the cap applies only to an isolated body, otherwise one
    # stops earlier, where the neighbour's area of competence begins.
    margin = SEARCH_MARGIN_MM if margin_mm is None else max(float(margin_mm), 0.0)
    px, py = frame.to_px(float(component["x_mm"]), float(component["y_mm"]))
    half_x = (span_x / 2 + margin) * frame.scale
    half_y = (span_y / 2 + margin) * frame.scale
    x0, y0 = int(max(0, px - half_x)), int(max(0, py - half_y))
    x1 = int(min(image.shape[1], px + half_x))
    y1 = int(min(image.shape[0], py + half_y))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None

    window = image[y0:y1, x0:x1]
    raw_edges = _canny_edges(window)
    edges = _closed_edges(raw_edges)
    if forbidden is not None:
        blocked = forbidden[y0:y1, x0:x1] == 1
        edges[blocked] = 0
        raw_edges[blocked] = 0
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Area and elongation are properties of the part, not of its pose: they
    # are derived from the declared footprint and not from the rectangle that
    # encloses it in the image, which for an oblique pose is larger than the
    # body.
    expected_area = length * width * frame.scale ** 2
    expected_elongation = max(length, width) / max(1e-6, min(length, width))
    wcx, wcy = px - x0, py - y0
    best, best_score = None, None
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 10:
            continue
        ratio = area / expected_area
        if not (AREA_RATIO_RANGE[0] <= ratio <= AREA_RATIO_RANGE[1]):
            continue
        (rcx, rcy), (rw, rh), _ = cv2.minAreaRect(contour)
        rect_area = rw * rh
        if rect_area <= 0 or area / rect_area < MIN_FILL_RATIO:
            # It fills only a small fraction of its own minimum rectangle: it
            # is the "dumbbell"/"L" shape of several nearby pieces welded
            # together, not a solid component.
            continue
        elongation = max(rw, rh) / max(1e-6, min(rw, rh))
        if elongation / expected_elongation > MAX_ELONGATION_RATIO or \
           expected_elongation / elongation > MAX_ELONGATION_RATIO:
            continue
        distance = math.hypot(rcx - wcx, rcy - wcy) / (margin * frame.scale + 1e-6)
        score = distance + abs(math.log(ratio)) * 0.6
        if best_score is None or score < best_score:
            best, best_score = contour, score
    if best is None:
        return None

    (rcx, rcy), (rw, rh), angle = cv2.minAreaRect(best)
    if rw <= 0 or rh <= 0:
        return None
    # The orientation is read on the closed contour (angular_resolution_deg
    # knows its growth); centre and footprint on the true edges it encloses,
    # along the reported axis, so the morphological growth does not enter the
    # measurement (see _edge_extent). Without useful edge pixels the contour
    # rectangle remains, minus the declared growth per side.
    new_rotation = _axis_rotation(angle, rw, rh, min(rw, rh))
    extent = _edge_extent(raw_edges, best, new_rotation)
    if extent is not None:
        rcx, rcy, long_span, short_span = extent
    else:
        long_span = max(max(rw, rh) - 2 * MORPH_GROWTH_PX, 1.0)
        short_span = max(min(rw, rh) - 2 * MORPH_GROWTH_PX, 1.0)
    x_mm, y_mm = frame.to_mm(x0 + rcx, y0 + rcy)
    if math.hypot(x_mm - float(component["x_mm"]), y_mm - float(component["y_mm"])) > MAX_SHIFT_MM:
        return None
    measured_length = long_span / frame.scale
    measured_width = short_span / frame.scale

    return {
        "x_mm": round(x_mm, 2),
        "y_mm": round(y_mm, 2),
        "length_mm": round(measured_length, 2),
        "width_mm": round(measured_width, 2),
        "rotation_deg": new_rotation,
    }


#: Side of the colour classes (Delta E CIE76) of the histogram with which
#: `sample_board_color` looks for the most extensive colour of the board.
#: Declared fallback: about two perceptibility thresholds
#: (MIN_BACKGROUND_DELTA_E), narrow enough to separate the soldermask from
#: bodies and pads, wide enough to keep a surface's noise together. It does
#: not decide alone: the count is made on the class and its neighbours
#: (3x3x3), so the result does not depend on where the class boundaries fall.
BOARD_COLOR_BIN_DELTA_E = 5.0
#: Long side, in pixels, to which the crop is reduced before counting the
#: colours: speed only. Area-averaging reduction averages out the fine
#: details (silkscreen, traces) but does not shift the colour of an extended
#: surface.
BOARD_COLOR_MAX_SIDE_PX = 512


def sample_board_color(crop) -> Optional[str]:
    """@brief Solder-mask colour of a calibrated board crop, sampled from the photo.

    @param crop BGR image in which the board fills the whole frame (the
        calibrated crop that prepare_photo returns).
    @return "#rrggbb" (lower case), or None when OpenCV is missing or the
        crop is not a nonempty three-channel image.
    @note The mask is the surface that covers the largest area of the board:
        components, pads, silkscreen and holes are many small patches of other
        colours. The crop is binned in CIE L*a*b* (BOARD_COLOR_BIN_DELTA_E
        for each class), each class counted together with its 26 neighbours,
        and the result is the channel-wise median of the pixels of the winning
        neighbourhood -- so noise and gentle shading of the mask stay inside
        it, and the median ignores the rest.
    @note Declared limit: when one other colour covers more of the board than
        the visible mask (a metal shield or a module over most of it), that
        colour is returned. The caller should present it as sampled, not
        measured, and let the user correct it.
    """
    if cv2 is None or crop is None or getattr(crop, "ndim", 0) != 3 or crop.shape[2] < 3:
        return None
    height, width = crop.shape[:2]
    if height < 1 or width < 1:
        return None
    image = np.ascontiguousarray(crop[:, :, :3])
    longest = max(height, width)
    if longest > BOARD_COLOR_MAX_SIDE_PX:
        factor = BOARD_COLOR_MAX_SIDE_PX / longest
        image = cv2.resize(image, (max(1, round(width * factor)), max(1, round(height * factor))),
                           interpolation=cv2.INTER_AREA)
    pixels = image.reshape(-1, 3)
    lab = _lab(image).reshape(-1, 3)
    # Classes: L in [0, 100], a and b in [-128, 128]; non-negative indices.
    step = BOARD_COLOR_BIN_DELTA_E
    offsets = np.array([0.0, 128.0, 128.0], dtype=np.float32)
    index = np.floor((lab + offsets) / step).astype(np.int64)
    shape = tuple(int(v) for v in np.ceil(np.array([100.0, 256.0, 256.0]) / step) + 1)
    index = np.clip(index, 0, np.array(shape) - 1)
    counts = np.zeros(shape, dtype=np.int64)
    np.add.at(counts, (index[:, 0], index[:, 1], index[:, 2]), 1)
    # Sum over the class and its neighbours: a colour straddling two classes
    # counts in full, like one falling in the middle of one.
    padded = np.pad(counts, 1)
    smoothed = np.zeros_like(counts)
    for dl in range(3):
        for da in range(3):
            for db in range(3):
                smoothed += padded[dl:dl + shape[0], da:da + shape[1], db:db + shape[2]]
    mode = np.array(np.unravel_index(int(np.argmax(smoothed)), shape))
    member = np.all(np.abs(index - mode) <= 1, axis=1)
    if not np.any(member):
        member = np.ones(len(pixels), dtype=bool)
    b, g, r = (int(v) for v in np.median(pixels[member], axis=0))
    return f"#{r:02x}{g:02x}{b:02x}"


def crop_frame(image, body_length: float, body_width: float) -> Optional[BoardFrame]:
    """@brief Reference of a photo ALREADY calibrated and cropped to the board.

    @param image Crop in which the board occupies exactly the image.
    @param body_length Declared board length, in mm.
    @param body_width Declared board width, in mm.
    @return BoardFrame with origin at the centre, or None if the dimensions do not hold.
    @note `prepare_photo` crops the board on its outline: searching for it
        again with `find_board` inside its own crop is both useless and
        risky, because there the largest dark rectangle can be a component.
        Here the reference is built from the declared dimensions and the crop
        pixels, without any search.
    @note The scale is taken from the long side: it is the side on which the
        relative cropping error weighs less. It applies to BOTH axes, so it
        is correct only if the crop has the same ratio as the declared
        dimensions. `find_board` accepts a different one up to
        MAX_BOARD_ASPECT_ERROR: whoever measures on an arbitrary crop goes
        first through `isotropic_crop_frame`, which resamples the image.
    """
    if image is None or body_length <= 0 or body_width <= 0:
        return None
    height_px, width_px = image.shape[:2]
    if width_px < 2 or height_px < 2:
        return None
    return BoardFrame(cx=width_px / 2, cy=height_px / 2,
                      scale=width_px / body_length,
                      width_px=float(width_px), height_px=float(height_px))


def isotropic_board(image, frame: BoardFrame, body_length: float, body_width: float):
    """@brief Resample a photo so the board has the same px/mm on both axes.

    @param image BGR photo containing the board.
    @param frame Board reference in that photo (width_px along X, height_px
        along Y, both measured on the photo).
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return (image, frame): the inputs themselves when the two axis scales
        already agree, otherwise a resampled copy of the image and a
        BoardFrame whose single `scale` holds on X and on Y.
    @note The detected board rectangle may differ from the declared aspect
        ratio (find_board accepts up to MAX_BOARD_ASPECT_ERROR). Every other
        consumer maps pixels to mm per axis (x / length, y / width); the edge
        measurement works in pixels with ONE scale, so without this step a
        crop 10% taller than declared would shift and stretch every measured
        Y. Only the lower-resolution axis is upsampled: no pixel of the photo
        is thrown away.
    """
    if cv2 is None or image is None or frame is None or body_length <= 0 or body_width <= 0:
        return image, frame
    extents = (getattr(frame, "width_px", None), getattr(frame, "height_px", None))
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               for value in extents):
        # Reference without numeric extents: there is nothing from which to
        # derive the two scales, so no resampling.
        return image, frame
    scale_x = extents[0] / body_length
    scale_y = extents[1] / body_width
    if not (math.isfinite(scale_x) and math.isfinite(scale_y)) or scale_x <= 0 or scale_y <= 0:
        return image, frame
    if abs(scale_x - scale_y) <= 1e-9 * max(scale_x, scale_y):
        return image, frame
    target = max(scale_x, scale_y)
    height, width = image.shape[:2]
    new_width = max(1, round(width * target / scale_x))
    new_height = max(1, round(height * target / scale_y))
    # Effective factors, after rounding to whole pixels.
    factor_x, factor_y = new_width / width, new_height / height
    resized = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    board_width, board_height = frame.width_px * factor_x, frame.height_px * factor_y
    return resized, BoardFrame(cx=frame.cx * factor_x, cy=frame.cy * factor_y,
                               scale=(board_width / body_length + board_height / body_width) / 2,
                               width_px=board_width, height_px=board_height)


def isotropic_crop_frame(image, body_length: float, body_width: float):
    """@brief Reference of a calibrated board crop, resampled to square pixels if needed.

    @param image Crop in which the board fills the whole image.
    @param body_length Declared board extent along X, in mm.
    @param body_width Declared board extent along Y, in mm.
    @return (image, frame), where frame.scale holds on both axes of the
        returned image, or (image, None) when the dimensions do not hold.
    @note Use this, not crop_frame alone, whenever mm are read back from
        pixels: the calibrated crop keeps the aspect of the detected board,
        which may differ from the declared one (see isotropic_board).
    """
    frame = crop_frame(image, body_length, body_width)
    if frame is None:
        return image, None
    return isotropic_board(image, frame, body_length, body_width)


def refine_on_frame(image, frame: BoardFrame, components: Sequence[dict],
                    exclude_rects_mm: Sequence[tuple] = ()) -> tuple[list[dict], RefineReport]:
    """@brief Measure the edges of the given components, on a known photo and reference.

    @param image Photo in which to search for the edges.
    @param frame Board reference in the photo.
    @param components Hypotheses with x_mm, y_mm, length_mm, width_mm, rotation_deg.
    @param exclude_rects_mm Rectangles (x0, y0, x1, y1) in mm where contours
        must not be looked at: typically the strips of the declared contacts,
        whose shiny metal edges are confused with those of a component.
    @return A copy of the list with the measured geometry where it was
        possible, and the report of how many were measured, how many left
        estimated (contour not clear or geometry not measurable) and how many
        not measured because of an unexpected error (RefineReport.failed).
    @note It does not touch name, kind, colour, marking: only the geometry,
        and only when the measurement is reliable enough (see
        `refine_component_edges`). When it does not measure it keeps the
        estimate, so it cannot do worse than not measuring at all.
    """
    report = RefineReport()
    refined = [dict(c) for c in components]
    if cv2 is None or frame is None or not refined:
        return refined, report
    forbidden = _forbidden_mask(image, frame, exclude_rects_mm)
    # The search window of each one stops halfway to the nearest body: it is
    # computed once on the starting estimates, before the measurements change
    # them, otherwise the order of the list would decide the areas.
    rects = [_body_rect_mm(c) for c in refined]
    margins = [search_margin_mm(
        min((_rect_gap_mm(rect, other) for index, other in enumerate(rects) if index != position),
            default=SEARCH_MARGIN_MM), frame.scale)
        for position, rect in enumerate(rects)]
    expected_errors = _expected_measure_errors()
    for component, margin in zip(refined, margins):
        try:
            measured = refine_component_edges(image, frame, component, forbidden, margin)
        except expected_errors:
            measured = None
        except Exception as exc:  # noqa: BLE001 - a defect on one part does not stop the others
            # Not an unclear contour: a defect. It is kept apart so the log
            # does not present a bug as «contorno non chiaro».
            report.failed.append(f"{component.get('name', '?')}: {type(exc).__name__}: {exc}")
            continue
        if measured is None:
            report.skipped.append(component.get("name", "?"))
            continue
        report.measured += 1
        # Edge snapping does not establish whether the new contour follows body,
        # attached terminals or solder pads; old box evidence no longer applies.
        if any(component.get(key) != value for key, value in measured.items()):
            if "bbox_basis" in component:
                component["bbox_basis"] = "unknown"
            component.pop("package_evidence", None)
        component.update(measured)
    return refined, report
