"""Tests of small drawn holes crossed by lines and of the measured layout of drawing_holes.

Two subjects:

1. Detection of small holes (about 3 px of radius) crossed or touched by
   centre, extension and dimension lines: the paper-inside test leaves out
   the directions covered only by a crossing line, a candidate must be a
   localised ring (not a stretch of two parallel lines or a bar), and of two
   overlapping circles the one with the less complete ring goes.
2. :func:`drawing_holes.measured_layout`: pad positions deduced from the
   drawn holes when one pad of a reading is off, refused whenever the answer
   is not unique.

Synthetic drawings are made with OpenCV at run time; real pages of the
repository are used only when present (skipped otherwise), like in
tests/test_drawing_holes.py. Each negative differs from its positive by the
structural property under test.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import drawing_holes
from kicadgen.services.drawing_holes import MeasuredLayout, check_pad_layout, measured_layout

ROOT = Path(__file__).resolve().parents[1]

#: True layout of the T7NS relay, bottom view on solder pins, page orientation (mm).
T7N_TRUE = {"11": (0.0, 6.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0), "14": (14.2, 0.0), "12": (14.2, 12.0)}
#: The dedicated reading of the same page: pad 11 put 2.0 mm right of the
#: A1/A2 column instead of left of it; the other pads correct (origin 0.05 mm away).
T7N_READING = {"11": (4.05, 6.0), "A1": (2.05, 0.0), "A2": (2.05, 12.0), "14": (14.25, 0.0),
               "12": (14.25, 12.0)}

#: Relay bottom view of tests/test_drawing_holes.py.
RELAY = {4: (0.0, 0.0), 1: (2.54, -7.62), 5: (5.08, -2.54), 3: (17.78, 0.0), 2: (17.78, -7.62)}


def _real(*candidates):
    """First existing real page among ``candidates`` (relative paths), or skip the test."""
    for relative in candidates:
        path = ROOT / relative
        if path.is_file():
            return path
    pytest.skip(f"{candidates[0]} not available")


def _t7n():
    """The T7NS relay datasheet page (858 x 717 px), committed as a fixture (user, 2026-10-31)."""
    return _real("tests/data/drawings/t7n_datasheet_page.png")


def _page(width=420, height=300):
    """White page with a line of text, so that the page looks like a datasheet drawing."""
    img = np.full((height, width, 3), 255, np.uint8)
    cv2.putText(img, "Bottom view 1,3 2,0 6,0", (10, height - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (0, 0, 0), 1, cv2.LINE_AA)
    return img


def _crossed_hole(img, centre, radius, filled=False):
    """Hole outline of ``radius`` px crossed by a horizontal and a vertical 1 px line."""
    x, y = centre
    cv2.circle(img, (x, y), radius, (0, 0, 0), -1 if filled else 1, cv2.LINE_8)
    cv2.line(img, (x - 40, y), (x + 40, y), (0, 0, 0), 1)
    cv2.line(img, (x, y - 30), (x, y + 30), (0, 0, 0), 1)


def _circles(img):
    """Circles the detector keeps on an in-memory page."""
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask, _paper = drawing_holes._ink_mask(grey)
    return drawing_holes._find_circles(grey, mask)


def _near(circles, x, y, distance=2.0):
    """Circles whose centre is within ``distance`` px of (x, y)."""
    return [c for c in circles if math.hypot(c.x - x, c.y - y) <= distance]


# ---------------------------------------------------------------------------
# Small holes crossed by lines
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("radius", [3, 4])
def test_small_hole_crossed_by_centre_lines_is_found_and_a_solid_disc_is_not(radius):
    # Same size, same two crossing lines: only the interior differs (paper
    # in the quarters between the lines versus ink).
    img = _page()
    _crossed_hole(img, (100, 100), radius)
    _crossed_hole(img, (250, 100), radius, filled=True)
    circles = _circles(img)
    hole = _near(circles, 100, 100)
    assert hole and hole[0].evidence, circles
    assert not _near(circles, 250, 100, distance=radius), circles


def test_ring_is_localised_and_two_parallel_lines_are_not():
    # A ring moved by one radius loses the arc that crosses its paper
    # interior; two parallel lines look the same at every height.
    # Pattern of the doubled body wall of the VE12H relay page: two 2 px
    # lines one pixel apart, a ring of 3 px radius fitting across them.
    mask = np.zeros((80, 80), np.uint8)
    cv2.circle(mask, (20, 40), 3, 255, 1)
    mask[10:70, 58:60] = 255
    mask[10:70, 61:63] = 255
    to_fine = lambda v: (v + 0.5) * drawing_holes.UPSCALE - 0.5  # noqa: E731
    ru = 3 * drawing_holes.UPSCALE
    assert drawing_holes._ring_coverage(mask, to_fine(20), to_fine(40), ru) >= drawing_holes.COVERAGE_MIN
    assert drawing_holes._is_localised(mask, to_fine(20), to_fine(40), ru)
    assert drawing_holes._ring_coverage(mask, to_fine(60), to_fine(40), ru) >= drawing_holes.COVERAGE_MIN
    assert not drawing_holes._is_localised(mask, to_fine(60), to_fine(40), ru)


def test_real_t7n_top_left_hole_crossed_by_lines_is_found():
    # A1, top left: a horizontal dimension line and a vertical centre line
    # (rastered with a one-pixel jog inside the hole) cross it.
    analysis = drawing_holes._analyse(_t7n())
    for x, y in [(649, 98), (640, 124), (649, 150), (701, 150), (702, 98)]:
        found = _near(analysis.circles, x, y)
        assert found and found[0].evidence, (x, y)


def test_real_t7n_true_layout_is_confirmed_and_a_wrong_one_is_not():
    path = _t7n()
    assert check_pad_layout([path], T7N_TRUE).status == "confirmed"
    # Pad 11 on the wrong side of the A1/A2 column: 2 mm off at 4.3 px/mm.
    wrong = dict(T7N_TRUE, **{"11": (4.0, 6.0)})
    assert check_pad_layout([path], wrong).status == "not_confirmed"


def test_real_relay_holes_are_not_replaced_by_composites_or_parallel_lines():
    path = _real("tests/data/drawings/ve12hk_quote_lowres.png")
    circles = drawing_holes._analyse(path).circles
    # The lower-left hole touched by a leader line is kept at its own centre,
    # not as the bigger composite of ring and leader below it.
    assert _near(circles, 661.3, 130.1, distance=1.0)
    assert not _near(circles, 660.0, 135.0, distance=1.5)
    # No circle stacked along the doubled body wall or a lead bar (the
    # false circle at (188, 191), where the bar meets a dimension line, is
    # older than these rules and still there).
    assert not _near(circles, 80, 103, distance=4.0)
    for y in (195, 201, 215):
        assert not _near(circles, 187, y, distance=2.5), y
    assert check_pad_layout([path], RELAY).status == "confirmed"


# ---------------------------------------------------------------------------
# measured_layout
# ---------------------------------------------------------------------------

#: Invented part, not mirror-symmetric: four anchor holes on a trapezoid and
#: pad 5 at (3, 4) mm (page orientation).
TRAPEZOID = {1: (0.0, 0.0), 2: (12.0, 0.0), 3: (9.0, 8.0), 4: (0.0, 8.0), 5: (3.0, 4.0)}


def _draw_holes(path, holes_mm, ppmm=10.0, hole_d_mm=1.0, origin=(120, 80)):
    """Isolated hole circles (drawn like holes: nothing around them) and a line of text."""
    img = _page(420, 300)
    for x, y in holes_mm:
        centre = (origin[0] + x * ppmm, origin[1] + y * ppmm)
        cv2.circle(img, (int(round(centre[0] * 16)), int(round(centre[1] * 16))),
                   int(round(hole_d_mm / 2 * ppmm * 16)), (0, 0, 0), 1, cv2.LINE_AA, shift=4)
    assert cv2.imwrite(str(path), img)
    return path


def test_real_t7n_misplaced_pad_is_moved_onto_the_only_free_hole():
    result = measured_layout([_t7n()], T7N_READING)
    assert isinstance(result, MeasuredLayout), result
    assert result.moved == "11"
    assert result.moved_from_mm == (4.05, 6.0)
    x, y = result.positions_mm["11"]
    # 2.0 mm left of the A1/A2 column at 2.05 mm.
    assert abs(x - 0.05) <= result.tolerance_mm and abs(y - 6.0) <= result.tolerance_mm, result
    for key in ("A1", "A2", "14", "12"):
        assert result.positions_mm[key] == T7N_READING[key]
        assert math.dist(result.measured_mm[key], T7N_READING[key]) <= result.tolerance_mm
    assert result.image == _t7n().name


def test_real_t7n_correct_reading_comes_back_unchanged():
    result = measured_layout([_t7n()], T7N_TRUE)
    assert result is not None and result.moved is None
    assert result.positions_mm == T7N_TRUE


def test_real_t7n_two_pads_off_is_refused():
    two_off = dict(T7N_READING, **{"14": (16.25, 0.0)})
    assert measured_layout([_t7n()], two_off) is None


def test_one_free_hole_is_taken_and_two_free_holes_are_refused(tmp_path):
    reading = {**TRAPEZOID, 5: (5.0, 4.0)}  # pad 5 read 2 mm right of its hole
    one = _draw_holes(tmp_path / "one.png", TRAPEZOID.values())
    result = measured_layout([one], reading)
    assert result is not None and result.moved == 5, result
    assert math.dist(result.positions_mm[5], TRAPEZOID[5]) <= result.tolerance_mm
    assert all(result.positions_mm[k] == reading[k] for k in (1, 2, 3, 4))
    # A second free hole as close to the reading (2 mm on the other side):
    # both explain it equally, nothing is chosen.
    two = _draw_holes(tmp_path / "two.png", list(TRAPEZOID.values()) + [(7.0, 4.0)])
    assert measured_layout([two], reading) is None


def test_a_move_needs_an_anchoring_that_can_show_the_page_scale(tmp_path):
    # Three pads, one off: the two anchored pads fix the fit exactly, any
    # two circles in the right direction would anchor them.
    path = _draw_holes(tmp_path / "three.png", [TRAPEZOID[1], TRAPEZOID[2], TRAPEZOID[5]])
    assert measured_layout([path], {1: (0.0, 0.0), 2: (12.0, 0.0), 5: (5.0, 4.0)}) is None
    # Four pads, one off: three anchored pads fit a similarity, but an
    # affine map fits any three points exactly, so a page not to scale on
    # the axis of the move could not be told from a misread pad (review H5,
    # 2026-11-04): refused, with the reason. With four anchored pads not
    # in a line (TRAPEZOID) the move is made: see
    # test_one_free_hole_is_taken_and_two_free_holes_are_refused.
    four = _draw_holes(tmp_path / "four.png", [TRAPEZOID[k] for k in (1, 2, 3, 5)])
    reading = {1: (0.0, 0.0), 2: (12.0, 0.0), 3: (9.0, 8.0), 5: (5.0, 4.0)}
    assert measured_layout([four], reading) is None
    outcome = drawing_holes.measured_layout_outcome([four], reading)
    assert outcome.layout is None and outcome.refused == "scale_unobservable", outcome


def test_a_photo_or_no_image_gives_nothing(tmp_path):
    rng = np.random.default_rng(3)
    photo = tmp_path / "photo.png"
    cv2.imwrite(str(photo), rng.integers(0, 256, (300, 400, 3), dtype=np.uint8))
    assert measured_layout([photo], T7N_READING) is None
    assert measured_layout([], T7N_READING) is None
    with pytest.raises(ValueError):
        measured_layout([photo], {1: (0.0, float("nan")), 2: (1.0, 0.0), 3: (2.0, 1.0)})
