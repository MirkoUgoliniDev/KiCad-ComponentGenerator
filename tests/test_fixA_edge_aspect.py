"""Edge measurement on a calibrated crop whose aspect differs from the declared one.

find_board accepts a board rectangle whose aspect ratio differs from the
declared one by up to MAX_BOARD_ASPECT_ERROR, and prepare_photo crops exactly
that rectangle. The detector maps pixels to mm per axis; the edge measurement
must do the same, otherwise a body is moved and stretched along Y.

Synthetic images only: the expected geometry is known in advance.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python non installato")

from kicadgen.services.photo_inventory import _normalized_box, measure_proposal_edges
from kicadgen.services.photo_refine import (BoardFrame, crop_frame, isotropic_board,
                                            isotropic_crop_frame)

LENGTH_MM, WIDTH_MM = 40.0, 20.0


def _crop(width_px, height_px, body_px):
    """Uniform dark board with one lighter body given in crop pixels."""
    image = np.full((height_px, width_px, 3), 22, dtype="uint8")
    x0, y0, x1, y1 = body_px
    image[y0:y1, x0:x1] = 70
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 2.5, image.shape).astype("int16")
    return np.clip(image.astype("int16") + noise, 0, 255).astype("uint8")


def _proposal_from_px(body_px, width_px, height_px, shift_mm=(0.0, 0.0)):
    """Detector proposal, converted to mm PER AXIS like photo_yolo does."""
    x0, y0, x1, y1 = body_px
    sx, sy = width_px / LENGTH_MM, height_px / WIDTH_MM
    size = ((x1 - x0) / sx, (y1 - y0) / sy)
    centre = ((x0 + x1) / 2 / sx - LENGTH_MM / 2 + shift_mm[0],
              (y0 + y1) / 2 / sy - WIDTH_MM / 2 + shift_mm[1])
    geometry = {"x_mm": centre[0], "y_mm": centre[1], "length_mm": size[0],
                "width_mm": size[1], "rotation_deg": 0.0}
    return dict(geometry, name="YOLO candidate 1", kind="generic",
                _box=_normalized_box(geometry, LENGTH_MM, WIDTH_MM))


def _measured_box_px(row, width_px, height_px):
    x0, y0, x1, y1 = row["_box"]
    return x0 * width_px, y0 * height_px, x1 * width_px, y1 * height_px


def test_a_taller_crop_is_measured_per_axis_not_with_the_x_scale():
    """Positive case: 800 x 440 px for 40 x 20 mm (10% taller than declared).

    Before the fix the single X scale (20 px/mm) was used for Y as well and
    the body at rows 352..382 came back as rows ~364..398.
    """
    width_px, height_px, body = 800, 440, (300, 352, 380, 382)
    image = _crop(width_px, height_px, body)
    proposal = _proposal_from_px(body, width_px, height_px, shift_mm=(0.5, 0.4))
    measured = measure_proposal_edges(image, [proposal], [], LENGTH_MM, WIDTH_MM,
                                      lambda _: None)[0]
    assert measured.get("_edges_measured") is True
    x0, y0, x1, y1 = _measured_box_px(measured, width_px, height_px)
    assert (x0, x1) == pytest.approx((300, 380), abs=3)
    assert (y0, y1) == pytest.approx((352, 382), abs=3)
    # In mm: 30 px of height at 22 px/mm, not at 20 px/mm.
    assert measured["width_mm"] == pytest.approx(30 / 22, abs=0.15)


def test_a_crop_with_the_declared_aspect_is_not_resampled():
    """Negative case: square pixels need no resampling; same objects come back."""
    image = _crop(800, 400, (300, 352, 380, 382))
    frame = crop_frame(image, LENGTH_MM, WIDTH_MM)
    same_image, same_frame = isotropic_board(image, frame, LENGTH_MM, WIDTH_MM)
    assert same_image is image and same_frame is frame
    resized, iso = isotropic_crop_frame(image, LENGTH_MM, WIDTH_MM)
    assert resized is image and iso.scale == pytest.approx(20.0)


def test_isotropic_board_upsamples_only_the_coarser_axis():
    """The finer axis keeps its pixels; the frame scale then holds on both axes."""
    image = np.zeros((440, 800, 3), dtype="uint8")
    frame = BoardFrame(cx=400, cy=220, scale=20.0, width_px=800.0, height_px=440.0)
    resized, iso = isotropic_board(image, frame, LENGTH_MM, WIDTH_MM)
    assert resized.shape[:2] == (440, 880)
    assert iso.width_px / LENGTH_MM == pytest.approx(22.0)
    assert iso.height_px / WIDTH_MM == pytest.approx(22.0)
    assert iso.scale == pytest.approx(22.0)
    assert iso.to_px(0.0, 0.0) == pytest.approx((440.0, 220.0))
