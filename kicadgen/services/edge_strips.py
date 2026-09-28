"""@brief Enlarged strips of a board's four edges, for the model to count the contacts itself.

User decision of 2026-09-28 (HANDOFF point 142): the number of contacts of a
castellated module must be recognised by the AI in its FIRST call, not fixed
afterwards. The first call already receives the whole photo, where each
half-hole of a small module spans a few dozen pixels among labels, LEDs and
parts; a strip along each edge, enlarged, shows the holes cut by the outline
from close up.

The strips are cut WITHOUT the board dimensions (they arrive with the answer
of that very call): the board is found as the largest region that differs
from the background colour sampled on the image frame, the same contrast
model the calibration uses (photo_refine.estimate_background). When that
fails the strips are simply not sent: they are help for the model, never a
measure, and nothing downstream depends on them.
"""
from __future__ import annotations

from typing import Optional

try:
    from .photo_refine import _background_distance, _contrast_mask, estimate_background
except ImportError:  # loaded as a stand-alone file
    from kicadgen.services.photo_refine import (_background_distance, _contrast_mask,
                                                estimate_background)

#: Depth of a strip, as a fraction of the board's SHORT side, taken inside the
#: outline. A half-hole sits at the edge; the fraction keeps the silkscreen
#: labels beside the holes in view (they are what the model must NOT count,
#: and it must see them to tell them apart). Structural choice, not tuned: it
#: only bounds how much of the board each strip shows.
STRIP_DEPTH_FRACTION = 0.18
#: Background kept outside the outline, as a fraction of the strip depth, so
#: that the cut of each hole against the background is visible.
STRIP_OUTSIDE_FRACTION = 0.25
#: Long side of an enlarged strip, in pixels. Enlarging adds no detail, but
#: models that tile images at a fixed size would otherwise see the strip tiny.
STRIP_LONG_SIDE_PX = 1600
#: A region smaller than this share of the image is not the board of a top
#: view (the board fills most of such a photo).
MIN_BOARD_AREA_FRACTION = 0.2

#: Board edges, in the photo frame (X right, Y down).
EDGES = ("top", "bottom", "left", "right")


def board_box(image) -> Optional[tuple]:
    """@brief Bounding box of the board in a top-view photo, without its dimensions.

    @param image BGR photo.
    @return (x0, y0, x1, y1) in pixels (x1/y1 excluded), or None when no region
        distinct from the background covers at least MIN_BOARD_AREA_FRACTION
        of the image.
    """
    import cv2
    import numpy as np
    background = estimate_background(image)
    if background is None:
        return None
    mask = _contrast_mask(_background_distance(image, background), background, image.shape)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return None
    index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = (int(v) for v in stats[index])
    height, width = image.shape[:2]
    if area < MIN_BOARD_AREA_FRACTION * height * width:
        return None
    return x, y, x + w, y + h


def edge_strips(image) -> list[tuple[str, bytes]]:
    """@brief PNG strips of the four edges of the board in a top-view photo, enlarged.

    @param image BGR photo (already upright).
    @return [(edge, png bytes)] in EDGES order; top/bottom strips are
        horizontal, left/right strips vertical, as in the photo. Empty when
        the board cannot be found or the photo cannot be encoded.
    """
    import cv2
    box = board_box(image)
    if box is None:
        return []
    x0, y0, x1, y1 = box
    height, width = image.shape[:2]
    depth = max(4, round(STRIP_DEPTH_FRACTION * min(x1 - x0, y1 - y0)))
    outside = max(2, round(STRIP_OUTSIDE_FRACTION * depth))
    cuts = {
        "top": (x0, max(0, y0 - outside), x1, min(height, y0 + depth)),
        "bottom": (x0, max(0, y1 - depth), x1, min(height, y1 + outside)),
        "left": (max(0, x0 - outside), y0, min(width, x0 + depth), y1),
        "right": (max(0, x1 - depth), y0, min(width, x1 + outside), y1),
    }
    strips = []
    for edge in EDGES:
        cx0, cy0, cx1, cy1 = cuts[edge]
        crop = image[cy0:cy1, cx0:cx1]
        if crop.size == 0:
            continue
        long_side = max(crop.shape[:2])
        if long_side < STRIP_LONG_SIDE_PX:
            factor = STRIP_LONG_SIDE_PX / long_side
            crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        ok, encoded = cv2.imencode(".png", crop)
        if ok:
            strips.append((edge, encoded.tobytes()))
    return strips
