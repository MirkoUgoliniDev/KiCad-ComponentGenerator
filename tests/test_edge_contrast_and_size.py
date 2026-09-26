"""@brief Edge measurement on boards of any colour, with sizes free of morphological growth.

Two defects of the fine edge measurement (photo_refine), found by the
adversarial audit of 26/09 (HANDOFF point 82, medium 6a and 6b):

* the Canny thresholds came from the median BRIGHTNESS of the window, while
  Canny compares GRADIENT magnitudes: on a white or green board a body had to
  differ by tens of grey levels more than on a black one, and the measure
  switched itself off exactly on the boards the sample never showed;
* the size came from the rectangle of the DILATED contour, so every body was
  measured larger by a constant number of pixels -- a fraction of a millimetre
  that grows as the photo gets coarser.

Synthetic images only: the expected result is known in advance.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python non installato")

from kicadgen.services import photo_refine as pr

LENGTH_MM, WIDTH_MM = 40.0, 16.0
#: Board colours (BGR) the sample never showed, plus the sample's dark board.
BOARDS = {
    "black": (22, 22, 22),
    "green": (40, 110, 30),
    "blue": (140, 60, 20),
    "white": (225, 225, 225),
}


def _scene(board, body, scale=20.0, size_mm=(3.0, 1.5), centre_mm=(5.0, -2.0), seed=0,
           noise=2.5):
    """Calibrated crop with one axis-aligned body; returns (image, true pixel box)."""
    image = np.full((int(WIDTH_MM * scale), int(LENGTH_MM * scale), 3), board, np.float32)
    cx = (centre_mm[0] / LENGTH_MM + 0.5) * image.shape[1]
    cy = (centre_mm[1] / WIDTH_MM + 0.5) * image.shape[0]
    x0, x1 = int(round(cx - size_mm[0] * scale / 2)), int(round(cx + size_mm[0] * scale / 2))
    y0, y1 = int(round(cy - size_mm[1] * scale / 2)), int(round(cy + size_mm[1] * scale / 2))
    image[y0:y1, x0:x1] = body
    image += np.random.default_rng(seed).normal(0, noise, image.shape)
    return np.clip(image, 0, 255).astype(np.uint8), (x0, y0, x1, y1)


def _shifted(colour, delta):
    return tuple(int(np.clip(v + delta, 0, 255)) for v in colour)


def _guess(frame, box, offset_mm=(0.3, 0.2)):
    x0, y0, x1, y1 = box
    x_mm, y_mm = frame.to_mm((x0 + x1 - 1) / 2, (y0 + y1 - 1) / 2)
    return {"x_mm": x_mm + offset_mm[0], "y_mm": y_mm + offset_mm[1],
            "length_mm": (x1 - x0) / frame.scale, "width_mm": (y1 - y0) / frame.scale,
            "rotation_deg": 0.0}


# ── 6a: thresholds from the gradient, not from the brightness ─────────

@pytest.mark.parametrize("board", sorted(BOARDS))
@pytest.mark.parametrize("delta", [-35, -20, -12, 12, 20, 35])
def test_a_body_is_measured_on_any_board_colour(board, delta):
    """@brief Positive case on every board: a body a few grey levels away is measured.

    The old rule needed ~85 grey levels of contrast on the white board and ~20
    on the black one; here the same modest contrast suffices everywhere.
    """
    colour = BOARDS[board]
    body = _shifted(colour, delta)
    if max(abs(a - b) for a, b in zip(body, colour)) < abs(delta):
        pytest.skip("contrast clipped at the ends of the 8-bit range")
    image, box = _scene(colour, body)
    frame = pr.crop_frame(image, LENGTH_MM, WIDTH_MM)
    guess = _guess(frame, box)
    measured = pr.refine_component_edges(image, frame, guess)
    assert measured is not None, f"no measure on the {board} board with contrast {delta}"
    x_mm, y_mm = frame.to_mm((box[0] + box[2] - 1) / 2, (box[1] + box[3] - 1) / 2)
    assert measured["x_mm"] == pytest.approx(x_mm, abs=0.1)
    assert measured["y_mm"] == pytest.approx(y_mm, abs=0.1)


def test_the_sample_like_dark_case_keeps_measuring():
    """@brief The case the old rule handled (dark body on dark mask) must not regress."""
    image, box = _scene((22, 22, 22), (45, 45, 45))
    frame = pr.crop_frame(image, LENGTH_MM, WIDTH_MM)
    measured = pr.refine_component_edges(image, frame, _guess(frame, box))
    assert measured is not None
    assert measured["length_mm"] == pytest.approx(3.0, abs=1 / frame.scale + 0.006)
    assert measured["width_mm"] == pytest.approx(1.5, abs=1 / frame.scale + 0.006)


@pytest.mark.parametrize("board", sorted(BOARDS))
def test_nothing_is_invented_where_there_is_no_edge(board):
    """@brief Negative case on the same boards: a body of the board's own colour gives nothing.

    Structural: the thresholds stay above the gradient of the measured noise,
    so a window with no step has no edge, whatever its brightness.
    """
    colour = BOARDS[board]
    for seed in range(3):
        image, box = _scene(colour, colour, seed=seed)
        frame = pr.crop_frame(image, LENGTH_MM, WIDTH_MM)
        assert pr.refine_component_edges(image, frame, _guess(frame, box)) is None


@pytest.mark.parametrize("sigma", [1.0, 2.5, 6.0])
@pytest.mark.parametrize("grey", [22, 120, 225])
def test_pure_noise_never_becomes_a_body(sigma, grey):
    """@brief Negative case: flat noise at any level and brightness gives no measure."""
    rng = np.random.default_rng(int(sigma * 10) + grey)
    image = np.clip(grey + rng.normal(0, sigma, (int(WIDTH_MM * 20), int(LENGTH_MM * 20), 3)),
                    0, 255).astype(np.uint8)
    frame = pr.crop_frame(image, LENGTH_MM, WIDTH_MM)
    for size in ((2.0, 2.0), (1.6, 0.8), (3.2, 1.6)):
        guess = {"x_mm": 0.0, "y_mm": 0.0, "length_mm": size[0], "width_mm": size[1],
                 "rotation_deg": 0.0}
        assert pr.refine_component_edges(image, frame, guess) is None


def test_thresholds_follow_the_step_not_the_brightness():
    """@brief The same step on a dark and on a light surface gets the same thresholds.

    And the step itself is a strong edge on both: the high threshold stays
    below its Sobel gradient (3 x the step after the 3x3 blur), which the old
    brightness rule did not do on a light board (high = 1.5 x 220 + 30 -> 255).
    """
    step = 20
    results = []
    for base in (22, 200):
        window = np.full((60, 60), base, np.float32)
        window[15:45, 20:40] = base + step
        window += np.random.default_rng(3).normal(0, 1.5, window.shape)
        results.append(pr.canny_thresholds(np.clip(window, 0, 255).astype(np.uint8)))
    (low_dark, high_dark), (low_light, high_light) = results
    assert high_light == pytest.approx(high_dark, rel=0.1)
    assert low_light == pytest.approx(low_dark, rel=0.1)
    assert high_light < 3 * step


# ── 6b: sizes read on the true edges, not on the dilated contour ───────

@pytest.mark.parametrize("scale", [8.0, 12.0, 30.0, 60.0])
def test_measured_size_error_stays_within_one_pixel_at_every_scale(scale):
    """@brief Positive case: length and width are right to within pixel quantisation.

    Before, the dilated contour made every body larger by a constant number of
    pixels: +0.25 mm at 12 px/mm, +0.38 mm at 8 px/mm. Now the error is bounded
    by one pixel of the photo (plus the 0.01 mm rounding of the result),
    whatever the scale, the polarity or the size of the body.
    """
    bound = 1.0 / scale + 0.006
    for board, body in (((22,) * 3, (45,) * 3), ((225,) * 3, (60,) * 3), ((40,) * 3, (200,) * 3)):
        for size in ((1.6, 0.8), (3.2, 1.6), (6.0, 2.0)):
            for seed in range(2):
                image, box = _scene(board, body, scale=scale, size_mm=size, centre_mm=(3.0, 0.3),
                                    seed=seed)
                frame = pr.crop_frame(image, LENGTH_MM, WIDTH_MM)
                measured = pr.refine_component_edges(image, frame, _guess(frame, box, (0.2, -0.1)))
                assert measured is not None, (scale, board, size, seed)
                true_length = (box[2] - box[0]) / scale
                true_width = (box[3] - box[1]) / scale
                assert abs(measured["length_mm"] - true_length) <= bound, (scale, board, size)
                assert abs(measured["width_mm"] - true_width) <= bound, (scale, board, size)


@pytest.mark.parametrize("scale", [8.0, 12.0, 30.0, 60.0])
def test_the_dilated_contour_would_fail_the_same_bound(scale):
    """@brief Negative case: the former measure (dilated contour) is outside that bound.

    Proves the size test discriminates: the rectangle of the closed contour
    exceeds the body by more than one pixel at every scale, so measuring on
    it could not pass test_measured_size_error_stays_within_one_pixel_at_every_scale.
    """
    image, box = _scene((22,) * 3, (45,) * 3, scale=scale, size_mm=(3.2, 1.6))
    x0, y0, x1, y1 = box
    margin = int(scale)
    window = image[y0 - margin:y1 + margin, x0 - margin:x1 + margin]
    contours, _ = cv2.findContours(pr._edge_mask(window), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    (_, _), (rw, rh), _ = cv2.minAreaRect(max(contours, key=cv2.contourArea))
    assert max(rw, rh) - (x1 - x0) > 1.0
    assert min(rw, rh) - (y1 - y0) > 1.0
