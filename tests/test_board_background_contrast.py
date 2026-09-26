"""Board detection by contrast with the background, whatever the colours.

find_board used to look for "the largest dark rectangle" (grey < 70). A
green board on a black mat, or a black board on a dark-grey mat, then gave a
crop of the whole photo or of an interior patch, silently. The contract is
now structural:

* the background is estimated on the image frame;
* a region touching the frame on two or more sides is the background, never
  the board;
* a board is surrounded by background on every free side, and has board
  (not background) just inside each side;
* the declared orientation holds: X is the declared length, so a board
  declared with L < W must be vertical in the photo, and a board seen rotated
  by 90 degrees is a declared failure, not a silent axis swap.

Synthetic images only: every board is drawn with exact integer pixel extents
at SCALE px/mm, so the expected calibration is known in advance.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python non installato")

from kicadgen.services.photo_refine import (
    BOARD_FOUND, BOARD_NOT_FOUND, BOARD_ROTATED_90, MAX_BOARD_ASPECT_ERROR,
    _aspect_error, deskew_board, estimate_background, find_board, isotropic_crop_frame,
    locate_board,
)

SCALE = 10.0
LENGTH_MM, WIDTH_MM = 40.0, 16.0
def _scale_tolerance(length_mm, width_mm):
    """Tolerance on the calibrated scale from pixel quantisation alone.

    Each extent (w on X, h on Y) may be off by one pixel at the segmentation
    edge (sensor noise flipping a boundary pixel), and scale = (w/L + h/W) / 2,
    so |error| <= (1/L + 1/W) / 2 px/mm. Nothing here is fitted to a result.
    """
    return 0.5 * (1.0 / length_mm + 1.0 / width_mm)


def _noisy(image, seed=0, sigma=2.5):
    """Same sensor noise as the other synthetic photos of the suite."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, sigma, image.shape).astype("int16")
    return np.clip(image.astype("int16") + noise, 0, 255).astype("uint8")


def _scene(background, board, size_mm=(LENGTH_MM, WIDTH_MM), margin_px=(80, 60),
           components=True, seed=0):
    """Photo with one board of exact size at SCALE px/mm, and its pixel box.

    Components are small rectangles well inside the board, light and dark, so
    the board is textured like a real one and not a flat colour.
    """
    bw, bh = int(round(size_mm[0] * SCALE)), int(round(size_mm[1] * SCALE))
    mx, my = margin_px
    image = np.zeros((bh + 2 * my, bw + 2 * mx, 3), dtype="uint8")
    image[:] = background
    image[my:my + bh, mx:mx + bw] = board
    if components:
        inset = int(0.2 * min(bw, bh))
        step = max(12, min(bw, bh) // 5)
        for index, y in enumerate(range(my + inset, my + bh - inset - 6, step)):
            for jndex, x in enumerate(range(mx + inset, mx + bw - inset - 10, step)):
                colour = (200, 200, 200) if (index + jndex) % 2 else (60, 45, 30)
                image[y:y + 6, x:x + 10] = colour
    return _noisy(image, seed), (mx, my, bw, bh)


def _assert_frame_matches(frame, box, length_mm=LENGTH_MM, width_mm=WIDTH_MM):
    mx, my, bw, bh = box
    assert frame is not None
    assert frame.scale == pytest.approx(SCALE, abs=_scale_tolerance(length_mm, width_mm))
    assert frame.width_px == pytest.approx(bw, abs=1)
    assert frame.height_px == pytest.approx(bh, abs=1)
    assert frame.cx == pytest.approx(mx + bw / 2, abs=1)
    assert frame.cy == pytest.approx(my + bh / 2, abs=1)


# -- positives: any board colour against any background colour ---------------

@pytest.mark.parametrize("background,board", [
    ((235, 235, 235), (22, 22, 22)),     # dark board on light table (historical case)
    ((15, 15, 15), (40, 110, 30)),       # green board on a black mat
    ((35, 35, 35), (225, 225, 225)),     # white board on a dark background
    ((128, 128, 128), (30, 30, 170)),    # red board on mid grey
    ((128, 128, 128), (150, 60, 20)),    # blue board on mid grey
    ((45, 45, 45), (22, 22, 22)),        # black board on a dark-grey mat
], ids=["dark-on-light", "green-on-black", "white-on-dark", "red-on-grey",
        "blue-on-grey", "black-on-darkgrey"])
def test_board_is_found_by_contrast_with_the_background(background, board):
    image, box = _scene(background, board)
    search = locate_board(image, LENGTH_MM, WIDTH_MM)
    assert search.reason == BOARD_FOUND and search.message(LENGTH_MM, WIDTH_MM) == ""
    _assert_frame_matches(search.frame, box)


def test_green_board_on_black_mat_is_not_confused_with_the_whole_photo():
    """The auditor's case: the photo itself has the declared proportions.

    The black mat is darker than 70, so the old "largest dark rectangle" was the
    mat, whose outline is the photo frame; with a 2.5 aspect photo it passed the
    aspect check and the whole photo became the board at 12.5 px/mm (25% off),
    silently. The mat touches all four frame sides: it is not a candidate.
    """
    image, box = _scene((15, 15, 15), (40, 110, 30), margin_px=(50, 20))
    height, width = image.shape[:2]
    # Precondition: the frame itself WOULD pass the aspect check, so only the
    # structural rule can reject it.
    assert _aspect_error(width, height, LENGTH_MM, WIDTH_MM) <= MAX_BOARD_ASPECT_ERROR
    _assert_frame_matches(find_board(image, LENGTH_MM, WIDTH_MM), box)


def test_a_blurred_edge_is_measured_at_half_contrast_not_at_its_foot():
    """Optical blur spreads the step over several pixels on each side.

    The contrast mask sees every pixel above the noise floor, i.e. the whole
    blur tail; the box is taken where the step reaches half its height, which
    blur does not move. Measured before the half-step refinement: +2-3 px per
    side, a 2% scale error on this board.
    """
    image, box = _scene((15, 15, 15), (40, 110, 30))
    blurred = cv2.GaussianBlur(image, (0, 0), 2.0)
    _assert_frame_matches(find_board(blurred, LENGTH_MM, WIDTH_MM), box)


# -- the frame is background ---------------------------------------------------

def _corner_region(offset_px):
    """Light photo with one dark region of the declared aspect near a corner."""
    image = np.full((500, 800, 3), 235, dtype="uint8")
    bw, bh = int(LENGTH_MM * SCALE), int(WIDTH_MM * SCALE)
    y1 = image.shape[0] - offset_px
    image[y1 - bh:y1, offset_px:offset_px + bw] = 22
    return _noisy(image), (offset_px, y1 - bh, bw, bh)


def test_a_region_touching_two_frame_sides_is_never_the_board():
    """Negative: same region, flush with the left and bottom edges of the photo.

    Aspect, resolution and image fraction all fit: only the structural rule
    (it continues beyond the frame, so its extent is unknown) rejects it.
    """
    image, (bx, by, bw, bh) = _corner_region(0)
    assert _aspect_error(bw, bh, LENGTH_MM, WIDTH_MM) == pytest.approx(0.0)
    search = locate_board(image, LENGTH_MM, WIDTH_MM)
    assert search.frame is None and search.reason == BOARD_NOT_FOUND
    assert "40.00 x 16.00" in search.message(LENGTH_MM, WIDTH_MM)


def test_the_same_region_inside_the_frame_is_the_board():
    """Positive counterpart: moved 30 px away from both edges, it is found."""
    image, box = _corner_region(30)
    _assert_frame_matches(find_board(image, LENGTH_MM, WIDTH_MM), box)


def test_touching_a_single_frame_side_is_still_accepted():
    """One side on the frame: the other three prove the board is surrounded."""
    image = np.full((400, 700, 3), 235, dtype="uint8")
    bw, bh = int(LENGTH_MM * SCALE), int(WIDTH_MM * SCALE)
    image[0:bh, 150:150 + bw] = 22
    _assert_frame_matches(find_board(_noisy(image), LENGTH_MM, WIDTH_MM), (150, 0, bw, bh))


# -- a board is surrounded by background, an interior patch is not -----------

def test_an_interior_patch_with_the_declared_aspect_is_not_the_board():
    """Negative: a square green board carrying a black part of aspect 2.5.

    The black part is darker than 70 and has the declared proportions, 6.25
    px/mm and enough area, so the old detector calibrated on it. It is
    surrounded by green board, not by the table: rejected. The square board
    does not have the declared proportions either, so there is no frame at all.
    """
    image = np.full((500, 600, 3), 235, dtype="uint8")
    image[100:400, 150:450] = (40, 110, 30)
    image[200:300, 175:425] = 22            # 250 x 100 px: aspect 2.5
    search = locate_board(_noisy(image), LENGTH_MM, WIDTH_MM)
    assert search.frame is None and search.reason == BOARD_NOT_FOUND


def test_a_smaller_textured_patch_on_the_background_does_not_replace_the_board():
    """Black board on a dark-grey mat, plus a textured object on the mat.

    The object has the declared aspect but is smaller than the board; the old
    detector, whose dark mask swallowed the whole mat, fell back on texture
    and could pick such a patch. The board is found and calibrated instead.
    """
    image, box = _scene((45, 45, 45), (22, 22, 22), margin_px=(80, 150))
    rng = np.random.default_rng(3)
    patch = rng.integers(0, 255, (60, 150, 3), dtype="uint8")   # aspect 2.5, 60% size
    image[10:70, 20:170] = patch
    _assert_frame_matches(find_board(image, LENGTH_MM, WIDTH_MM), box)


# -- orientation: X is the declared length -------------------------------------

def test_a_board_declared_narrower_than_tall_is_found_vertical():
    """Positive: 16 x 40 mm declared (contacts on the short edges), photo portrait.

    Before, `w < h` was always rejected and such a board could never be
    calibrated. X extent (width_px) is the declared length, 16 mm.
    """
    image, box = _scene((235, 235, 235), (22, 22, 22), size_mm=(WIDTH_MM, LENGTH_MM),
                        margin_px=(60, 80))
    frame = find_board(image, WIDTH_MM, LENGTH_MM)
    _assert_frame_matches(frame, box, WIDTH_MM, LENGTH_MM)
    assert frame.width_px < frame.height_px


def test_a_vertical_board_declared_wider_than_tall_is_a_declared_failure():
    """Negative: same portrait photo, declared 40 x 16 (X = 40 mm).

    The axes are never swapped behind the user's back: no frame, and the
    reason says the photo is rotated by 90 degrees relative to the dimensions.
    """
    image, _ = _scene((235, 235, 235), (22, 22, 22), size_mm=(WIDTH_MM, LENGTH_MM),
                      margin_px=(60, 80))
    search = locate_board(image, LENGTH_MM, WIDTH_MM)
    assert search.frame is None
    assert search.reason == BOARD_ROTATED_90
    assert "90 gradi" in search.message(LENGTH_MM, WIDTH_MM)


def test_a_landscape_board_declared_portrait_is_a_declared_failure_too():
    """The rule is symmetric: a landscape photo does not fit L < W either."""
    image, _ = _scene((235, 235, 235), (22, 22, 22))
    assert locate_board(image, WIDTH_MM, LENGTH_MM).reason == BOARD_ROTATED_90


def test_prepare_photo_crops_a_portrait_board_with_the_declared_scale(tmp_path):
    """Downstream: the crop keeps X = length and the crop frame scale holds."""
    from kicadgen.services.photo_inventory import prepare_photo
    image, (_, _, bw, bh) = _scene((15, 15, 15), (40, 110, 30),
                                   size_mm=(WIDTH_MM, LENGTH_MM), margin_px=(60, 80))
    path = tmp_path / "portrait.png"
    assert cv2.imwrite(str(path), image)
    crop = prepare_photo(path, WIDTH_MM, LENGTH_MM, {})
    assert crop is not None
    assert crop.shape[1] == pytest.approx(bw, abs=1) and crop.shape[0] == pytest.approx(bh, abs=1)
    _, frame = isotropic_crop_frame(crop, WIDTH_MM, LENGTH_MM)
    assert frame.scale == pytest.approx(SCALE, abs=_scale_tolerance(WIDTH_MM, LENGTH_MM))


# -- deskew on a dark background -----------------------------------------------

def test_deskew_fills_the_new_canvas_with_the_background_not_white():
    """A white fill on a dark mat would draw a light frame that is not there."""
    image, _ = _scene((20, 20, 20), (40, 110, 30), margin_px=(120, 120))
    centre = (image.shape[1] / 2, image.shape[0] / 2)
    tilted = cv2.warpAffine(image, cv2.getRotationMatrix2D(centre, 4.0, 1.0),
                            (image.shape[1], image.shape[0]), flags=cv2.INTER_LINEAR,
                            borderValue=(20, 20, 20))
    assert find_board(tilted, LENGTH_MM, WIDTH_MM) is None
    straight = deskew_board(tilted, LENGTH_MM, WIDTH_MM)
    assert straight is not None
    assert int(straight[0, 0].max()) < 40
    frame = find_board(straight, LENGTH_MM, WIDTH_MM)
    assert frame is not None
    # Two resamplings blur the edges; the extent is taken at half the contrast
    # step, where blur does not move a step edge, so the one-pixel
    # quantisation bound still holds (at the noise floor the blur tails
    # widened the box by 2-3 px per side, a 2% scale error).
    assert frame.scale == pytest.approx(SCALE, abs=_scale_tolerance(LENGTH_MM, WIDTH_MM))


def test_background_is_estimated_from_the_frame_whatever_its_colour():
    image, _ = _scene((15, 15, 15), (40, 110, 30))
    dark = estimate_background(image)
    image, _ = _scene((235, 235, 235), (22, 22, 22))
    light = estimate_background(image)
    assert dark.lab[0] < 10 < 90 < light.lab[0]
    assert dark.bgr == pytest.approx((15, 15, 15), abs=2)
    assert light.bgr == pytest.approx((235, 235, 235), abs=2)
