"""Conservative, model-free geometry on a BGR PCB crop.

Boxes are normalized ``(left, top, right, bottom)`` in *this crop*, as in
photo_inventory._box; pixel boxes use exclusive right/bottom boundaries.
``analyze_geometry(image, detections)`` returns aligned decisions and independent
region proposals. Refinement is opt-in (``refine=True``), and even an accepted
fit requires visual review. It never mutates detections or metric dimensions.
Callers must recompute mm geometry if they accept a decision.

Silkscreen frames are useful search regions, NOT component extents: frames,
empty footprints and printed text are deliberately review-only proposals.
Experimental refinement requires four locally supported, non-silkscreen edges,
bounded movement, unambiguous evidence and no increase in neighbor overlap.
No contour dilation/closing joins adjacent parts. Rotated/perspective photos,
occluded boundaries, leads disconnected from the body, low resolution and
grossly misplaced model boxes normally require abstention. Pixel evidence
cannot establish component identity, presence, class, polarity or height.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import math
from typing import Mapping, Sequence

import cv2
import numpy as np

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class GeometryConfig:
    """Scale-relative limits; thresholds are heuristics, not probabilities."""

    search_fraction: float = 0.18
    min_side_px: int = 10
    min_side_support: float = 0.62
    min_support_gain: float = 0.12
    min_scale: float = 0.72
    max_scale: float = 1.25
    ambiguity_margin: float = 0.035
    max_proposals: int = 200

    def __post_init__(self):
        for name in ("search_fraction", "min_side_support", "min_support_gain",
                     "min_scale", "max_scale", "ambiguity_margin"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not (0 < self.search_fraction <= 0.3 and 0 < self.min_side_support <= 1
                and 0 <= self.min_support_gain <= 1 and 0 < self.min_scale <= 1
                and 1 <= self.max_scale <= 1.5 and 0 <= self.ambiguity_margin <= 1):
            raise ValueError("unsafe geometry thresholds")
        if (type(self.min_side_px) is not int or self.min_side_px < 6
                or type(self.max_proposals) is not int or self.max_proposals < 1):
            raise ValueError("invalid size/proposal limit")


@dataclass(frozen=True)
class GeometryCandidate:
    bbox: Box
    source: str  # silkscreen_frame or edge_rectangle; always requires review
    side_support: tuple[float, float, float, float]
    score: float
    requires_review: bool = True


@dataclass(frozen=True)
class GeometryDecision:
    original_box: Box
    bbox: Box
    accepted: bool
    reason: str
    before_support: tuple[float, float, float, float]
    after_support: tuple[float, float, float, float]
    requires_review: bool = True


@dataclass(frozen=True)
class GeometryAnalysis:
    decisions: tuple[GeometryDecision, ...]
    candidates: tuple[GeometryCandidate, ...]


def _box(value) -> Box:
    try:
        box = tuple(float(v) for v in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("expected a finite normalized box") from exc
    if (len(box) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in box)
            or box[0] >= box[2] or box[1] >= box[3]):
        raise ValueError("expected a positive normalized box within [0, 1]")
    return box


def _intersection(a, b):
    return max(0., min(a[2], b[2])-max(a[0], b[0])) * max(
        0., min(a[3], b[3])-max(a[1], b[1]))


def _area(a):
    return (a[2]-a[0]) * (a[3]-a[1])


def _iou(a, b):
    intersection = _intersection(a, b)
    return intersection / max(1e-12, _area(a)+_area(b)-intersection)


class ComponentGeometry:
    """Cache image evidence once; use refine_box/propose_candidates repeatedly."""

    def __init__(self, image: np.ndarray, config: GeometryConfig | None = None):
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                or image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 8):
            raise ValueError("image must be a uint8 BGR crop at least 8x8 pixels")
        self.config = config or GeometryConfig()
        self.h, self.w = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        smooth = cv2.GaussianBlur(gray, (3, 3), 0)
        gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
        # Modest fixed noise floor, adaptive to the actual contrast distribution.
        magnitude = cv2.magnitude(gx, gy)
        high = max(24., min(120., float(np.percentile(magnitude, 80))))
        self.edges = cv2.Canny(smooth, high*.4, high, L2gradient=True)
        self.vertical = (self.edges > 0) & (np.abs(gx) >= np.abs(gy)*1.4)
        self.horizontal = (self.edges > 0) & (np.abs(gy) >= np.abs(gx)*1.4)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        self.white = ((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 160)).astype(np.uint8)*255
        _, _, stats, _ = cv2.connectedComponentsWithStats(self.white)
        self._bright_blobs = stats[1:]

    def _pixels(self, box):
        return tuple(round(v*s) for v, s in zip(box, (self.w, self.h, self.w, self.h)))

    def _normalized(self, box):
        return tuple(float(v/s) for v, s in zip(box, (self.w, self.h, self.w, self.h)))

    def _side(self, coordinate, start, stop, vertical, white=False):
        # Ignore corners; nearby pixels are evidence only within one pixel.
        trim = max(1, round((stop-start)*.08))
        lo, hi = start+trim, stop-trim
        arr = self.white > 0 if white else (self.vertical if vertical else self.horizontal)
        if not vertical:
            arr = arr.T
        lo, hi = max(0, lo), min(arr.shape[0], hi)
        a, b = max(0, coordinate-1), min(arr.shape[1], coordinate+2)
        if hi <= lo or b <= a:
            return 0.
        hits = arr[lo:hi, a:b].any(axis=1)
        # Both halves must contribute: a small bright solder joint is not a side.
        halves = np.array_split(hits, 2)
        return min(float(h.mean()) for h in halves if len(h))

    def _support(self, box, white=False):
        x0, y0, x1, y1 = box
        return (self._side(x0, y0, y1, True, white),
                self._side(y0, x0, x1, False, white),
                self._side(x1-1, y0, y1, True, white),
                self._side(y1-1, x0, x1, False, white))

    def edge_support(self, bbox: Sequence[float]):
        """Directional support fractions in left, top, right, bottom order."""
        return self._support(self._pixels(_box(bbox)))

    def _cuts_possible_terminal(self, original, candidate):
        """Do not tighten onto a body while clipping compact bright terminals.

        A bright blob may also be a pad/marking; uncertainty means abstention.
        Long thin silk segments and enclosing frames are not compact terminals.
        """
        cw, ch = candidate[2]-candidate[0], candidate[3]-candidate[1]
        for x, y, w, h, area in self._bright_blobs:
            if area < 8 or area/(w*h) < .35 or w > cw*.7 or h > ch*.7:
                continue
            blob = (int(x), int(y), int(x+w), int(y+h))
            old = _intersection(original, blob)
            new = _intersection(candidate, blob)
            # A near-boundary bright island can be a disconnected pin as well.
            near = (candidate[0]-3, candidate[1]-3, candidate[2]+3, candidate[3]+3)
            if (old >= _area(blob)*.6 and old-new >= max(6, _area(blob)*.25)
                    and _intersection(near, blob) > 0):
                return True
        return False

    def propose_candidates(self) -> tuple[GeometryCandidate, ...]:
        """Return unclassified image regions. Never add these as mounted parts."""
        found = []
        for mask, source in ((self.white, "silkscreen_frame"), (self.edges, "edge_rectangle")):
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if min(w, h) < self.config.min_side_px or w*h > self.w*self.h*.20:
                    continue
                if x <= 1 or y <= 1 or x+w >= self.w-1 or y+h >= self.h-1:
                    continue
                # Closed, near-axis-aligned rectangles; circles/text fail fill/vertices.
                perimeter = cv2.arcLength(contour, True)
                polygon = cv2.approxPolyDP(contour, .025*perimeter, True)
                if len(polygon) != 4 or not cv2.isContourConvex(polygon):
                    continue
                if abs(cv2.contourArea(contour))/max(1, (w-1)*(h-1)) < .86:
                    continue
                pixels = (x, y, x+w, y+h)
                support = self._support(pixels, white=source == "silkscreen_frame")
                if min(support) < self.config.min_side_support:
                    continue
                found.append(GeometryCandidate(self._normalized(pixels), source,
                                               support, float(np.mean(support))))
        # Deduplicate the inner/outer edges of the same frame. Prefer silk label.
        found.sort(key=lambda c: (c.source != "silkscreen_frame", -c.score, -_area(c.bbox), c.bbox))
        kept = []
        for candidate in found:
            if all(_iou(candidate.bbox, other.bbox) < .75 for other in kept):
                kept.append(candidate)
        return tuple(kept[:self.config.max_proposals])

    def refine_box(self, bbox: Sequence[float], neighbors: Sequence[Sequence[float]] = ()) -> GeometryDecision:
        """Attempt a small correction; abstention returns the exact original box.

        ``accepted`` means a local fit passed these heuristics, not that its
        extent/identity is correct. This experimental operation requires review.

        Neighbors are the other supplied boxes, including duplicate detections.
        Their overlap may decrease, but cannot grow by more than pixel rounding.
        With no neighbor boxes this guard is unavailable; movement remains local.
        """
        original = _box(bbox)
        other_boxes = tuple(_box(n) for n in neighbors)
        pixels = self._pixels(original)
        before = self._support(pixels)

        def reject(reason):
            return GeometryDecision(original, original, False, reason, before, before)

        x0, y0, x1, y1 = pixels
        width, height = x1-x0, y1-y0
        cfg = self.config
        if min(width, height) < cfg.min_side_px:
            return reject("too_small")
        if x0 <= 1 or y0 <= 1 or x1 >= self.w-1 or y1 >= self.h-1:
            return reject("crop_boundary")
        if min(before) >= .9:
            return reject("already_supported")

        options = []
        for coordinate, start, stop, vertical, span, upper in (
                (x0, y0, y1, True, width, self.w-2),
                (y0, x0, x1, False, height, self.h-2),
                (x1-1, y0, y1, True, width, self.w-2),
                (y1-1, x0, x1, False, height, self.h-2)):
            radius = max(1, math.floor(span*cfg.search_fraction))
            scored = [(self._side(p, start, stop, vertical), p)
                      for p in range(max(2, coordinate-radius), min(upper, coordinate+radius)+1)]
            scored.sort(key=lambda v: (-v[0], abs(v[1]-coordinate), v[1]))
            peaks = []
            for support, p in scored:
                if support < cfg.min_side_support*.65:
                    break
                if all(abs(p-q) > 2 for q in peaks):
                    peaks.append(p)
                if len(peaks) == 3:
                    break
            if not peaks:
                return reject("insufficient_edges")
            options.append(peaks)

        matches = []
        blocked_neighbor = False
        blocked_silk = False
        blocked_terminal = False
        for left, top, right, bottom in product(*options):
            candidate = (left, top, right+1, bottom+1)
            cw, ch = right+1-left, bottom+1-top
            if not (cfg.min_scale <= cw/width <= cfg.max_scale
                    and cfg.min_scale <= ch/height <= cfg.max_scale):
                continue
            support = self._support(candidate)
            if min(support) < cfg.min_side_support:
                continue
            gain = float(np.mean(support)-np.mean(before))
            if gain < cfg.min_support_gain or min(support) < min(before):
                continue
            # White on three sides is a likely silk frame, not a package extent.
            if sum(s > .55 for s in self._support(candidate, white=True)) >= 3:
                blocked_silk = True
                continue
            if self._cuts_possible_terminal(pixels, candidate):
                blocked_terminal = True
                continue
            normalized = self._normalized(candidate)
            tolerance = 1./(self.w*self.h)
            if any(_intersection(normalized, n) > _intersection(original, n)+tolerance
                   for n in other_boxes):
                blocked_neighbor = True
                continue
            # Prior is only a tie breaker. Edge support must improve first.
            movement = sum(abs(a-b)/s for a, b, s in zip(
                candidate, pixels, (width, height, width, height)))/4
            score = float(np.mean(support))-.15*movement
            matches.append((score, candidate, support))
        if not matches:
            return reject("neighbor_overlap" if blocked_neighbor else
                          "possible_terminals" if blocked_terminal else
                          "silkscreen_only" if blocked_silk else "no_supported_improvement")
        matches.sort(key=lambda m: (-m[0], m[1]))
        best = matches[0]
        for alternative in matches[1:]:
            if max(abs(a-b) for a, b in zip(best[1], alternative[1])) <= 3:
                continue  # the same edge has pixel-width uncertainty
            if best[0]-alternative[0] < cfg.ambiguity_margin:
                return reject("ambiguous_edges")
            break
        return GeometryDecision(original, self._normalized(best[1]), True,
                                "supported_local_edges", before, best[2])


def analyze_geometry(image: np.ndarray, detections: Sequence[Mapping], *,
                     config: GeometryConfig | None = None, refine: bool = False) -> GeometryAnalysis:
    """Analyze ``_box`` detections without changing metadata or metric geometry.

    Validation is atomic: a malformed detection raises ValueError before any
    decisions are returned. Batch refinements are checked against both original
    and final neighbor boxes, avoiding order-dependent simultaneous expansion.
    By default this only proposes regions and reports existing edge support.
    ``refine=True`` enables experimental, review-only local fits: on dense real
    boards, even four supported sides can belong to silk instead of a package.
    """
    boxes = []
    for detection in detections:
        if not isinstance(detection, Mapping) or "_box" not in detection:
            raise ValueError("each detection must have a normalized _box")
        boxes.append(_box(detection["_box"]))
    geometry = ComponentGeometry(image, config)
    if not refine:
        return GeometryAnalysis(tuple(GeometryDecision(
            b, b, False, "refinement_disabled", geometry.edge_support(b),
            geometry.edge_support(b)) for b in boxes), geometry.propose_candidates())
    decisions = [geometry.refine_box(box, boxes[:i]+boxes[i+1:]) for i, box in enumerate(boxes)]
    conflicting = set()
    for i, first in enumerate(decisions):
        for j in range(i+1, len(decisions)):
            second = decisions[j]
            if _intersection(first.bbox, second.bbox) > _intersection(boxes[i], boxes[j])+1/(geometry.w*geometry.h):
                conflicting.update((i, j))
    for i in conflicting:
        d = decisions[i]
        if d.accepted:
            decisions[i] = GeometryDecision(d.original_box, d.original_box, False,
                                            "batch_neighbor_overlap", d.before_support, d.before_support)
    return GeometryAnalysis(tuple(decisions), geometry.propose_candidates())
