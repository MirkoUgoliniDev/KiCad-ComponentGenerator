"""Exact native-pixel targets remove ambiguous contact-sheet coordinate frames.

These are intentionally general synthetic-image contracts, not PCB fixtures.
Rotation belongs to decode_terminal_geometry, never to this image crop helper.
"""
from copy import deepcopy

import numpy as np
import pytest

from kicadgen.services import photo_terminals


def _crop(photo, box):
    # Resolve at test time so a missing implementation produces focused failures.
    return photo_terminals.target_crop(photo, box)


def _photo(height=80, width=120):
    y, x = np.indices((height, width))
    return np.stack((x % 256, y % 256, (x + y) % 256), axis=-1).astype(np.uint8)


@pytest.mark.parametrize("height,width,box,pixels", [
    (80, 120, [.125, .25, .375, .75], (15, 20, 45, 60)),
    (80, 120, [.5, .125, .75, .625], (60, 10, 90, 50)),
    (160, 240, [.125, .25, .375, .75], (30, 40, 90, 120)),
    (80, 120, [.101, .201, .309, .619], (12, 16, 38, 50)),
    (80, 120, [0, 0, 1, 1], (0, 0, 120, 80)),
    (80, 120, [.501, .501, .502, .502], (60, 40, 61, 41)),
])
def test_exact_target_native_pixels_and_actual_full_photo_extents(height, width, box, pixels):
    photo = _photo(height, width)
    before = photo.copy()
    original_box = deepcopy(box)
    crop, actual = _crop(photo, box)
    left, top, right, bottom = pixels
    assert crop.shape == (bottom - top, right - left, 3)
    assert crop.dtype == photo.dtype
    np.testing.assert_array_equal(crop, photo[top:bottom, left:right])
    assert actual == pytest.approx([left / width, top / height, right / width, bottom / height])
    np.testing.assert_array_equal(photo, before)
    assert box == original_box


@pytest.mark.parametrize("box,pixels", [
    ([-.125, .25, .375, .75], (0, 20, 45, 60)),
    ([.5, -.25, .75, .625], (60, 0, 90, 50)),
    ([.5, .25, 1.25, .75], (60, 20, 120, 60)),
    ([.5, .25, .75, 1.25], (60, 20, 90, 80)),
    ([-2, -3, 4, 5], (0, 0, 120, 80)),
])
def test_partial_outside_target_is_clipped_not_padded(box, pixels):
    photo = _photo()
    crop, actual = _crop(photo, box)
    left, top, right, bottom = pixels
    np.testing.assert_array_equal(crop, photo[top:bottom, left:right])
    assert actual == pytest.approx([left / 120, top / 80, right / 120, bottom / 80])


@pytest.mark.parametrize("box", [
    None, [], [0, 0, 1], [0, 0, 1, 1, 1], "0,0,1,1",
    [True, 0, 1, 1], [0, "0", 1, 1],
    [0, 0, float("nan"), 1], [float("inf"), 0, 1, 1],
    [0, float("-inf"), 1, 1],
    [.5, 0, .5, 1], [0, .5, 1, .5], [.75, 0, .25, 1], [0, .75, 1, .25],
    [-2, .25, -.1, .75], [1, .25, 2, .75],
    [.25, -2, .75, 0], [.25, 1, .75, 2],
])
def test_invalid_nonfinite_empty_or_fully_outside_boxes_rejected(box):
    with pytest.raises(ValueError):
        _crop(_photo(), box)


@pytest.mark.parametrize("photo", [
    np.empty((0, 10, 3), dtype=np.uint8),
    np.empty((10, 0, 3), dtype=np.uint8),
])
def test_empty_photograph_rejected(photo):
    with pytest.raises(ValueError):
        _crop(photo, [0, 0, 1, 1])


def test_asymmetric_target_keeps_photo_orientation_and_all_edge_pixels():
    photo = _photo()
    crop, actual = _crop(photo, [.125, .25, .375, .75])
    assert crop.shape == (40, 30, 3)
    np.testing.assert_array_equal(crop[0, 0], photo[20, 15])
    np.testing.assert_array_equal(crop[0, -1], photo[20, 44])
    np.testing.assert_array_equal(crop[-1, 0], photo[59, 15])
    np.testing.assert_array_equal(crop[-1, -1], photo[59, 44])
    assert actual == pytest.approx([.125, .25, .375, .75])
