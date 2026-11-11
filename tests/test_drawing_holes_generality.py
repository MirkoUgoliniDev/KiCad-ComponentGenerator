"""Generality tests of the drawn-hole check (audit A7-A10, 2026-11-10).

What a confirmation covers (`body_check`, `scale_verified`), fits that may
be a coincidence ("unanchored"), which drawn rectangle is the body, and the
two separate terms of the error budget (per-hole pixels, proportional
ratios) with a skewed page told apart from a distorted one.

Every page is drawn with OpenCV in pytest's tmp_path from invented parts.
Each rule has a positive and a negative case that differ in the property the
rule is about (how many pads witness a fit, what the circles are drawn
like, the proportions of a rectangle, a ratio at two sizes), never in a
number brought near a threshold.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import drawing_holes
from kicadgen.services.drawing_holes import (
    LayoutCheck,
    MeasuredLayout,
    body_allowance_mm,
    body_axes_distinguishable,
    check_pad_layout,
    measured_layout,
    measured_outline,
)

# Invented asymmetric part, page orientation (Y down), mm.
PART = {"1": (0.0, 0.0), "2": (10.0, 0.0), "3": (10.0, 6.0), "4": (2.5, 6.0), "5": (5.0, 3.0)}
# Its body outline, 3 mm outside the holes: left, top, right, bottom mm (16 x 12 mm).
BOX = (-3.0, -3.0, 13.0, 9.0)
BODY = (16.0, 12.0)
BLACK = (0, 0, 0)


def _page(path: Path, part=PART, *, ppmm: float = 10.0, size=(760, 520), origin=(300, 220),
          hole_r_mm: float = 0.6, marks: str = "none", boxes=(BOX,), chamfer_mm: float = 0.0,
          frame: bool = False, cell=None, rotation_deg: float = 0.0, stretch_x: float = 1.0) -> Path:
    """Draw a pin view and return its path.

    @param part Pad -> (x, y) mm, page orientation; drawn as 1 px rings.
    @param marks What is drawn through every hole: "none" (a ring alone on
        paper), "centre" (horizontal and vertical centre lines) or
        "diagonal" (two diagonal strokes: the ring is then neither crossed
        by a centre line nor alone on paper, so it is not drawn like a hole).
    @param boxes Rectangles (left, top, right, bottom mm) drawn solid; the
        first is the body, cut at its corners by ``chamfer_mm``.
    @param frame A rectangle near the page border.
    @param cell A table cell around the view, (left, top, right, bottom) px.
    @param rotation_deg Rigid rotation of the whole view about ``origin``.
    @param stretch_x Scale of the page along X only (a scan stretch).
    """
    width, height = size
    img = np.full((height, width, 3), 255, np.uint8)
    cos_a, sin_a = math.cos(math.radians(rotation_deg)), math.sin(math.radians(rotation_deg))

    def px(x_mm: float, y_mm: float) -> tuple[float, float]:
        """Page pixel of a point of the part's mm frame."""
        x, y = x_mm * stretch_x, y_mm
        return origin[0] + (x * cos_a - y * sin_a) * ppmm, origin[1] + (x * sin_a + y * cos_a) * ppmm

    def ipx(x_mm: float, y_mm: float) -> tuple[int, int]:
        """The same, rounded to whole pixels."""
        x, y = px(x_mm, y_mm)
        return int(round(x)), int(round(y))

    radius = hole_r_mm * ppmm
    for x_mm, y_mm in part.values():
        cx, cy = px(x_mm, y_mm)
        cv2.circle(img, (int(round(cx * 16)), int(round(cy * 16))), int(round(radius * 16)), BLACK, 1,
                   cv2.LINE_AA, shift=4)
        reach = int(round(radius * 3.5))
        if marks == "centre":
            cv2.line(img, (int(cx) - reach, int(round(cy))), (int(cx) + reach, int(round(cy))), BLACK, 1)
            cv2.line(img, (int(round(cx)), int(cy) - reach), (int(round(cx)), int(cy) + reach), BLACK, 1)
        elif marks == "diagonal":
            reach = int(round(radius * 2.5))
            cv2.line(img, (int(cx) - reach, int(cy) - reach), (int(cx) + reach, int(cy) + reach), BLACK, 1,
                     cv2.LINE_AA)
            cv2.line(img, (int(cx) - reach, int(cy) + reach), (int(cx) + reach, int(cy) - reach), BLACK, 1,
                     cv2.LINE_AA)
    for index, (left, top, right, bottom) in enumerate(boxes):
        cut = chamfer_mm if index == 0 else 0.0
        if cut:
            corners = [ipx(left + cut, top), ipx(right - cut, top), ipx(right, top + cut),
                       ipx(right, bottom - cut), ipx(right - cut, bottom), ipx(left + cut, bottom),
                       ipx(left, bottom - cut), ipx(left, top + cut)]
        else:
            corners = [ipx(left, top), ipx(right, top), ipx(right, bottom), ipx(left, bottom)]
        for a, b in zip(corners, corners[1:] + corners[:1]):
            cv2.line(img, a, b, BLACK, 2, cv2.LINE_AA)
    if frame:
        cv2.rectangle(img, (20, 20), (width - 20, height - 20), BLACK, 2)
    if cell is not None:
        cv2.rectangle(img, (cell[0], cell[1]), (cell[2], cell[3]), BLACK, 1)
    assert cv2.imwrite(str(path), img)
    return path


def _scaled(part, factor: float) -> dict:
    """The reading with every value multiplied by ``factor``."""
    return {key: (x * factor, y * factor) for key, (x, y) in part.items()}


def _verdict(result: LayoutCheck) -> tuple[str, str, str, bool]:
    """Status, reason, body check and scale flag of a result."""
    return result.status, result.reason, result.body_check, result.scale_verified


# ---------------------------------------------------------------------------
# A7 -- what a confirmation covers
# ---------------------------------------------------------------------------

#: One row of eight holes, 2.54 mm apart, and its 22.78 x 5 mm body.
ROW = {str(n + 1): (2.54 * n, 0.0) for n in range(8)}
ROW_BOX = (-2.5, -2.5, 20.28, 2.5)
ROW_BODY = (22.78, 5.0)


def test_new_fields_default_to_nothing_verified():
    """Callers and stubs that build the results positionally keep working."""
    check = LayoutCheck("confirmed", "confirmed", "a.png", 0.0, 0.15, 5)
    assert (check.body_check, check.scale_verified) == ("none", False)
    layout = MeasuredLayout({}, {}, None, None, 0.0, 0.15, "a.png")
    assert (layout.body_check, layout.scale_verified) == ("none", False)


@pytest.mark.parametrize("factor", [1.0, 2.0 / 2.54, 1 / 0.0254])
def test_without_a_body_any_multiple_of_the_reading_confirms_and_says_so(tmp_path, factor):
    """A point set fits at any size: right pitch, 2.0 read for 2.54, mils taken for mm.

    The check cannot tell them apart and must not pretend to: confirmed,
    with nothing said about the scale.
    """
    path = _page(tmp_path / "row.png", ROW, boxes=(ROW_BOX,), origin=(200, 250))
    assert _verdict(check_pad_layout([path], _scaled(ROW, factor))) == (
        "confirmed", "confirmed", "none", False)


@pytest.mark.parametrize("factor, expected", [
    (1.0, ("confirmed", "confirmed", "agrees", True)),
    (2.0 / 2.54, ("not_confirmed", "body_scale", "contradicted", False)),
    (1 / 0.0254, ("not_confirmed", "body_scale", "contradicted", False)),
])
def test_the_drawn_body_ties_the_pads_to_an_absolute_size(tmp_path, factor, expected):
    """The same three readings with the body given: only the right one keeps the body's scale."""
    path = _page(tmp_path / "row.png", ROW, boxes=(ROW_BOX,), origin=(200, 250))
    assert _verdict(check_pad_layout([path], _scaled(ROW, factor), ROW_BODY)) == expected


def test_limit_pads_and_body_multiplied_together_agree_with_each_other(tmp_path):
    """Documented limit: `scale_verified` ties the pads to the DECLARED body, it does not measure it."""
    path = _page(tmp_path / "part.png")
    result = check_pad_layout([path], _scaled(PART, 2.0), (BODY[0] * 2, BODY[1] * 2))
    assert _verdict(result) == ("confirmed", "confirmed", "agrees", True)


def test_a_body_the_check_never_reached_is_unverifiable_not_none(tmp_path):
    """"none" means no body was given; a body that no fit compared with an outline is "unverifiable"."""
    path = _page(tmp_path / "part.png")
    two = {key: PART[key] for key in ("1", "2")}
    assert check_pad_layout([path], two).body_check == "none"
    assert check_pad_layout([path], two, BODY).body_check == "unverifiable"
    wrong = {**PART, "5": (5.0, 4.5)}
    result = check_pad_layout([path], wrong, BODY)
    assert _verdict(result) == ("not_confirmed", "no_match", "unverifiable", False)


def test_a_later_image_that_shows_the_body_is_preferred(tmp_path):
    """Two confirming images: the one whose outline ties the scale decides, whatever their order."""
    bare = _page(tmp_path / "bare.png", boxes=())
    drawn = _page(tmp_path / "drawn.png")
    for images in ([bare, drawn], [drawn, bare]):
        result = check_pad_layout(images, PART, BODY)
        assert _verdict(result) == ("confirmed", "confirmed", "agrees", True)
        assert result.image == "drawn.png"
    # Negative twin: no image shows the body, the first confirmation stands.
    result = check_pad_layout([bare], PART, BODY)
    assert _verdict(result) == ("confirmed", "confirmed", "unverifiable", False)


def test_the_measured_layout_carries_the_body_check(tmp_path):
    """A measured layout says whether its positions are tied to the body, like the check."""
    path = _page(tmp_path / "part.png")
    assert measured_layout([path], PART).body_check == "none"
    tied = measured_layout([path], PART, BODY)
    assert (tied.body_check, tied.scale_verified) == ("agrees", True)
    bare = _page(tmp_path / "bare.png", boxes=())
    loose = measured_layout([bare], PART, BODY)
    assert (loose.body_check, loose.scale_verified) == ("unverifiable", False)


# ---------------------------------------------------------------------------
# A8 -- fits that may be a coincidence
# ---------------------------------------------------------------------------

TRIANGLE = {key: PART[key] for key in ("1", "2", "4")}

#: Rings of 10 px radius: large enough for the detector to keep a ring
#: crossed by two diagonal strokes (at 6 px the strokes hide it).
WIDE_PAGE = dict(ppmm=20.0, size=(800, 600), origin=(250, 200), hole_r_mm=0.5, boxes=())


@pytest.mark.parametrize("marks, expected", [
    ("centre", ("confirmed", "confirmed")),
    ("none", ("confirmed", "confirmed")),
    ("diagonal", ("unverifiable", "no_round_holes")),
])
def test_three_pads_confirm_only_on_circles_drawn_like_holes(tmp_path, marks, expected):
    """Three pads leave ONE witness: the fit counts only where every circle is drawn like a hole.

    The three pages differ only in what is drawn through the rings: centre
    lines or nothing (a hole), diagonal strokes (not the way a hole is
    drawn: so are the counters of slashed glyphs). With no circle drawn
    like a hole on the page, the page-level reason "no_round_holes" is the
    one given; "unanchored" when the page has holes but the pads sit on
    other circles (next test).
    """
    path = _page(tmp_path / "tri.png", TRIANGLE, marks=marks, **WIDE_PAGE)
    result = check_pad_layout([path], TRIANGLE)
    assert (result.status, result.reason) == expected, result


def test_three_pads_on_undeclared_circles_of_a_page_with_holes_are_unanchored(tmp_path):
    """The triangle sits on rings crossed diagonally; three isolated holes elsewhere make the page judgeable.

    The reason then names the fit itself: unanchored, not confirmed.
    """
    path = _page(tmp_path / "tri.png", TRIANGLE, marks="diagonal", **WIDE_PAGE)
    img = cv2.imread(str(path))
    for x in (600, 680, 760 - 20):
        cv2.circle(img, (x, 520), 10, BLACK, 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img)
    drawing_holes.clear_analysis_cache()
    result = check_pad_layout([path], TRIANGLE)
    assert (result.status, result.reason) == ("unverifiable", "unanchored"), result


def test_two_witness_pads_may_sit_on_any_circle(tmp_path):
    """Structural twin of the above: the same undeclared circles, four pads instead of three."""
    four = {key: PART[key] for key in ("1", "2", "3", "4")}
    path = _page(tmp_path / "four.png", four, marks="diagonal", **WIDE_PAGE)
    assert check_pad_layout([path], four).status == "confirmed"
    wrong = {**four, "3": (10.0, 4.5)}
    assert check_pad_layout([path], wrong).status != "confirmed"


def test_three_wrong_pads_on_drawn_holes_are_still_refused(tmp_path):
    """The witness rule weakens confirmations only: a wrong third pad is not excused by it."""
    path = _page(tmp_path / "tri.png", TRIANGLE, marks="centre", boxes=())
    wrong = {**TRIANGLE, "4": (2.5, 7.0)}
    result = check_pad_layout([path], wrong)
    assert (result.status, result.reason) == ("not_confirmed", "no_match_unobservable"), result


# ---------------------------------------------------------------------------
# A9 -- which drawn rectangle is the body
# ---------------------------------------------------------------------------

def test_a_page_frame_around_a_body_that_is_not_drawn_does_not_judge_the_reading(tmp_path):
    """The only rectangle around the holes is the page border: other proportions, not the body."""
    path = _page(tmp_path / "frame.png", boxes=(), frame=True)
    assert _verdict(check_pad_layout([path], PART, BODY)) == (
        "confirmed", "confirmed", "unverifiable", False)


@pytest.mark.parametrize("surround", ["frame", "cell"])
def test_the_frame_around_a_chamfered_body_is_not_the_body(tmp_path, surround):
    """A body with cut corners closes no rectangle; the frame or table cell around it is not it."""
    path = _page(tmp_path / "chamfer.png", chamfer_mm=2.5, frame=surround == "frame",
                 cell=(150, 100, 560, 400) if surround == "cell" else None)
    assert _verdict(check_pad_layout([path], PART, BODY)) == (
        "confirmed", "confirmed", "unverifiable", False)
    assert measured_outline([path], PART, BODY) is None


@pytest.mark.parametrize("frame", [False, True])
def test_the_body_drawn_at_another_scale_than_the_pads_still_contradicts(tmp_path, frame):
    """Negative twin: the body IS drawn (its proportions say so) and the reading is 25 % too large."""
    path = _page(tmp_path / "body.png", frame=frame)
    assert _verdict(check_pad_layout([path], PART, BODY)) == ("confirmed", "confirmed", "agrees", True)
    result = check_pad_layout([path], _scaled(PART, 1.25), BODY)
    assert _verdict(result) == ("not_confirmed", "body_scale", "contradicted", False)
    assert measured_outline([path], _scaled(PART, 1.25), BODY) is None


def test_one_misread_body_size_does_not_judge_the_pads(tmp_path):
    """19 mm for 16 mm along X: the rectangle no longer has the declared proportions.

    It is a mistake of the body reading; the pads fit and stay confirmed,
    without a verified scale. The uniformly wrong body (both sizes x 1.25:
    the drawn proportions) is the twin that contradicts.
    """
    path = _page(tmp_path / "body.png")
    assert _verdict(check_pad_layout([path], PART, (19.0, 12.0))) == (
        "confirmed", "confirmed", "unverifiable", False)
    assert _verdict(check_pad_layout([path], PART, (20.0, 15.0))) == (
        "not_confirmed", "body_scale", "contradicted", False)


def test_the_two_sizes_exchanged_are_other_proportions(tmp_path):
    """A body given the other way round is not contradicted: the caller tests the exchange itself."""
    path = _page(tmp_path / "body.png")
    assert _verdict(check_pad_layout([path], PART, (BODY[1], BODY[0]))) == (
        "confirmed", "confirmed", "unverifiable", False)


def test_an_inner_frame_does_not_hide_the_body_outline(tmp_path):
    """The smallest rectangle around the holes is a 14 x 8 mm frame; the body is the next one out."""
    inner = (-2.0, -1.0, 12.0, 7.0)  # 14 x 8 mm: other proportions than the 16 x 12 body
    # Centre lines: the inner frame passes within a diameter of the holes,
    # so they are drawn like holes by their lines, not by isolation.
    path = _page(tmp_path / "nested.png", boxes=(BOX, inner), marks="centre")
    assert _verdict(check_pad_layout([path], PART, BODY)) == ("confirmed", "confirmed", "agrees", True)
    outline = measured_outline([path], PART, BODY)
    assert outline is not None
    assert abs(outline.width_mm - BODY[0]) <= outline.tolerance_mm
    assert abs(outline.height_mm - BODY[1]) <= outline.tolerance_mm
    # Without a body nothing identifies it: the smallest rectangle comes back.
    assert abs(measured_outline([path], PART).width_mm - 14.0) <= outline.tolerance_mm
    # Negative twin: on the same page a reading 25 % too large is caught by the body outline.
    result = check_pad_layout([path], _scaled(PART, 1.25), BODY)
    assert _verdict(result) == ("not_confirmed", "body_scale", "contradicted", False)


# ---------------------------------------------------------------------------
# The decision table of the body check (module docstring of drawing_holes,
# review finding V1-1, 2026-11-11). One block per row; the twins of a row
# differ in WHAT IS DRAWN or in which reading is given, never in a number
# brought near a threshold.
# ---------------------------------------------------------------------------

#: Outline of a nearly square 15 x 16 mm body around PART: left, top, right, bottom mm.
NEAR_SQUARE_BOX = (-2.5, -5.0, 12.5, 11.0)
NEAR_SQUARE_BODY = (15.0, 16.0)

#: The 16 x 12 mm body drawn loosely, 11.6 mm along Y (3 % out of proportion,
#: inside the drafting allowance): what the sample pages do with their bodies.
LOOSE_BOX = (-3.0, -2.8, 13.0, 8.8)

#: A reading of PART with every value 20 % too small, and a frame 1.75 mm
#: outside the 16 x 12 mm outline: 19.5 x 15.5 mm, which that reading takes
#: for 15.6 x 12.4 mm -- inside the allowance of the body, out of proportion.
SMALL = 0.8
FRAME = (-4.75, -4.75, 14.75, 10.75)

AGREES = ("confirmed", "confirmed", "agrees", True)
UNVERIFIED = ("confirmed", "confirmed", "unverifiable", False)
REFUSED = ("not_confirmed", "body_scale", "contradicted", False)


def _inset(box, margin_mm: float):
    """A rectangle (left, top, right, bottom mm) moved inwards by one margin on its four sides."""
    return box[0] + margin_mm, box[1] + margin_mm, box[2] - margin_mm, box[3] - margin_mm


@pytest.mark.parametrize("outline_px, scale, expected", [
    ((160.0, 120.0), 10.0, "identified_here"),         # the body as quoted
    ((160.0, 120.0), 12.5, "identified_elsewhere"),    # the same rectangle, the pads read 20 % too small
    ((160.0, 116.0), 10.0, "loose_here"),              # drawn 3 % short along Y
    ((195.0, 155.0), 12.5, "loose_here"),              # a frame 1.75 mm outside, at the wrong scale (V1-1)
    ((160.0, 116.0), 12.5, "loose_elsewhere"),         # the loose body, at the wrong scale
    ((195.0, 155.0), 10.0, "loose_elsewhere"),         # the frame, at the right scale
    ((140.0, 80.0), 10.0, None),                       # other proportions altogether
    ((120.0, 160.0), 10.0, None),                      # the two sizes exchanged
])
def test_what_one_rectangle_says_about_the_body(outline_px, scale, expected):
    """The two questions asked of a rectangle, on a 16 x 12 mm body and a 3 px tolerance."""
    assert drawing_holes._judge_outline(outline_px, BODY, scale, 3.0) == expected


def test_the_proportion_test_knows_nothing_of_the_pads_scale():
    """Scale-free: a rectangle keeps or lacks the body's proportions at any size, the allowance does not."""
    for factor in (0.3, 1.0, 4.0):
        exact = (160.0 * factor, 120.0 * factor)
        assert drawing_holes._has_body_proportions(exact, BODY)
        assert not drawing_holes._has_body_proportions((exact[0], exact[1] * 0.9), BODY)
        assert drawing_holes._has_body_proportions((exact[0], exact[1] * 0.95), BODY, drawing_holes.BODY_SCALE_RATIO)
        assert not drawing_holes._has_body_proportions((exact[0], exact[1] * 0.8), BODY,
                                                       drawing_holes.BODY_SCALE_RATIO)
        assert drawing_holes._within_allowance(exact, BODY, 10.0, 3.0) is (factor == 1.0)


# Row 1 -- a candidate at the pads' scale and none elsewhere: agrees.
# (The plain case, the outline alone, is asserted all over this file.)

def test_row_1_a_body_drawn_loosely_and_alone_agrees(tmp_path):
    """A body drawn 3 % out of proportion, the only rectangle around the holes, is still the body.

    Negative twin, same page: a reading 20 % too small finds no rectangle
    at its scale and none with the EXACT proportions elsewhere (row 4):
    confirmed by its shape, scale not verified, and NOT refused -- only
    exact proportions identify a body at another scale.
    """
    path = _page(tmp_path / "loose.png", boxes=(LOOSE_BOX,))
    assert _verdict(check_pad_layout([path], PART, BODY)) == AGREES
    outline = measured_outline([path], PART, BODY)
    assert outline is not None and abs(outline.height_mm - 11.6) <= outline.tolerance_mm
    assert _verdict(check_pad_layout([path], _scaled(PART, SMALL), BODY)) == UNVERIFIED


def test_row_1_a_rectangle_that_could_not_be_the_body_is_no_rival(tmp_path):
    """A 28 x 24 mm frame around the 16 x 12 mm outline: no scale makes it the body, the outline agrees.

    Like the 14 x 8 mm inner frame of the test above. Negative twin, what
    is drawn: the frame 1.75 mm outside the outline COULD be the body
    within the drafting allowance and takes the agreement away (row 2,
    test_row_2_a_frame_inside_the_allowance...).
    """
    path = _page(tmp_path / "frame.png", boxes=(BOX, _inset(BOX, -6.0)))
    assert _verdict(check_pad_layout([path], PART, BODY)) == AGREES
    outline = measured_outline([path], PART, BODY)
    assert outline is not None and abs(outline.width_mm - BODY[0]) <= outline.tolerance_mm


def test_limit_a_frame_alone_inside_the_allowance_agrees_with_a_wrong_scale(tmp_path):
    """Declared limit of row 1, pinned so that a change of it is noticed.

    The page draws NO body outline, only the frame 1.75 mm outside it, and
    the reading is 20 % too small: the frame is the single candidate,
    within the allowance exactly like a loosely drawn body, and it agrees.
    Drawing the real outline as well (row 2, below) removes the agreement;
    nothing on this page can.
    """
    path = _page(tmp_path / "frame.png", boxes=(FRAME,))
    assert _verdict(check_pad_layout([path], _scaled(PART, SMALL), BODY)) == AGREES
    # The right reading on the same page: the frame is a loose candidate elsewhere, alone (row 4).
    assert _verdict(check_pad_layout([path], PART, BODY)) == UNVERIFIED


# Row 2 -- a candidate at the pads' scale AND one elsewhere: unverifiable.

def test_row_2_a_frame_inside_the_allowance_does_not_certify_a_wrong_scale(tmp_path):
    """Review finding V1-1: a clearly non-square body, its outline, and a frame 1.75 mm outside it.

    A reading 20 % too small takes the frame for the body within the
    allowance, while the real outline has the body's exact proportions at
    another scale: certified until this fix, now confirmed with the scale
    not verified. The right reading meets the same two rectangles the
    other way round and is not verified either -- the price of not
    certifying the wrong one. The twins differ in what is DRAWN: with the
    outline alone the right reading agrees and the wrong one is refused.
    """
    path = _page(tmp_path / "frame.png", boxes=(BOX, FRAME))
    assert _verdict(check_pad_layout([path], _scaled(PART, SMALL), BODY)) == UNVERIFIED
    assert measured_outline([path], _scaled(PART, SMALL), BODY) is None
    assert _verdict(check_pad_layout([path], PART, BODY)) == UNVERIFIED
    assert measured_outline([path], PART, BODY) is None
    alone = _page(tmp_path / "alone.png")
    assert _verdict(check_pad_layout([alone], PART, BODY)) == AGREES
    assert _verdict(check_pad_layout([alone], _scaled(PART, SMALL), BODY)) == REFUSED


def test_row_2_a_step_inside_a_nearly_square_body_leaves_the_scale_unverified(tmp_path):
    """F7-1 and V1-1 together: a step 1.5 mm inside a nearly square body keeps the body's proportions.

    The correct reading is CONFIRMED (never refused, F7-1) and its scale is
    NOT verified: this page is, rectangle for rectangle, the page of a
    reading 12/15 too small whose body is the inner rectangle and whose
    outer one is a frame (see the next test). The twins differ in what is
    DRAWN: the outline alone agrees, the step alone is the body at another
    scale.
    """
    step = _inset(NEAR_SQUARE_BOX, 1.5)
    both = _page(tmp_path / "step.png", boxes=(NEAR_SQUARE_BOX, step), marks="centre")
    assert _verdict(check_pad_layout([both], PART, NEAR_SQUARE_BODY)) == UNVERIFIED
    assert measured_outline([both], PART, NEAR_SQUARE_BODY) is None
    outline = _page(tmp_path / "outline.png", boxes=(NEAR_SQUARE_BOX,), marks="centre")
    assert _verdict(check_pad_layout([outline], PART, NEAR_SQUARE_BODY)) == AGREES
    alone = _page(tmp_path / "alone.png", boxes=(step,), marks="centre")
    assert _verdict(check_pad_layout([alone], PART, NEAR_SQUARE_BODY)) == REFUSED
    assert measured_outline([alone], PART, NEAR_SQUARE_BODY) is None


def test_row_2_a_frame_outside_a_nearly_square_body_does_not_certify_a_wrong_scale(tmp_path):
    """The mirror page: a 17 x 18 mm frame 1 mm outside the 15 x 16 mm body, a reading 15/17 too small.

    The frame has the declared sizes at that wrong scale and the real
    outline the body's proportions at another: until 2026-11-11 this was a
    declared limit that "agreed"; it is the ambiguity of the test above
    seen from the other side, and is now answered the same way.
    """
    path = _page(tmp_path / "frame.png", boxes=(NEAR_SQUARE_BOX, _inset(NEAR_SQUARE_BOX, -1.0)))
    assert _verdict(check_pad_layout([path], _scaled(PART, 15 / 17), NEAR_SQUARE_BODY)) == UNVERIFIED
    # The right reading on the same page: the same two rectangles, the other way round.
    assert _verdict(check_pad_layout([path], PART, NEAR_SQUARE_BODY)) == UNVERIFIED


@pytest.mark.parametrize("factor", [1.0, 1 / 1.5])
def test_row_2_the_body_drawn_at_two_scales_is_unverifiable(tmp_path, factor):
    """A 16 x 12 mm outline and a 24 x 18 mm one: the same proportions at two scales.

    Whichever of the two the reading agrees with (factor 1: the inner;
    1/1.5: the outer), the other contradicts it and the answer is
    "unverifiable", never a refusal and never a verified scale.
    """
    outer = (-7.0, -6.0, 17.0, 12.0)
    path = _page(tmp_path / "two.png", boxes=(BOX, outer))
    reading = _scaled(PART, factor)
    assert _verdict(check_pad_layout([path], reading, BODY)) == UNVERIFIED
    assert measured_outline([path], reading, BODY) is None


def test_row_2_a_loose_body_and_a_frame_are_two_candidates(tmp_path):
    """The loosely drawn body with the frame 1.75 mm outside it: neither rectangle has the exact proportions.

    The reading 20 % too small takes the frame for the body, the right one
    takes the outline; each has the other as a candidate elsewhere, so
    neither scale is verified and neither reading is refused. Twin:
    without the frame the right reading agrees (row 1, above).
    """
    path = _page(tmp_path / "pair.png", boxes=(LOOSE_BOX, FRAME))
    assert _verdict(check_pad_layout([path], _scaled(PART, SMALL), BODY)) == UNVERIFIED
    assert _verdict(check_pad_layout([path], PART, BODY)) == UNVERIFIED
    assert measured_outline([path], PART, BODY) is None


# Row 3 -- nothing at the pads' scale, the exact proportions elsewhere: contradicted.
# (test_the_body_drawn_at_another_scale_than_the_pads_still_contradicts is
# its plain case, with the right reading on the same page as the twin.)

def test_row_3_a_wrong_reading_is_contradicted_when_the_body_has_a_step(tmp_path):
    """Two rectangles with the body's proportions, neither at the pads' scale: both contradict."""
    path = _page(tmp_path / "step.png", boxes=(NEAR_SQUARE_BOX, _inset(NEAR_SQUARE_BOX, 1.5)), marks="centre")
    result = check_pad_layout([path], _scaled(PART, 1.4), NEAR_SQUARE_BODY)
    assert _verdict(result) == REFUSED
    assert measured_outline([path], _scaled(PART, 1.4), NEAR_SQUARE_BODY) is None


def test_row_3_a_loose_rectangle_elsewhere_does_not_soften_the_contradiction(tmp_path):
    """The outline says "another scale"; a frame that is not at the reading's scale either changes nothing."""
    path = _page(tmp_path / "far.png", boxes=(BOX, FRAME))
    assert _verdict(check_pad_layout([path], _scaled(PART, 1.25), BODY)) == REFUSED


def test_limit_the_only_rectangle_of_a_nearly_square_body_is_taken_for_the_body(tmp_path):
    """Declared limit of row 3, pinned: a step drawn alone inside a nearly square body refuses a right reading.

    The body outline is not drawn; its step keeps the body's proportions
    and is "the body at another scale". Unchanged since audit A9.
    """
    alone = _page(tmp_path / "alone.png", boxes=(_inset(NEAR_SQUARE_BOX, 1.5),), marks="centre")
    assert _verdict(check_pad_layout([alone], PART, NEAR_SQUARE_BODY)) == REFUSED


# Row 4 -- nothing at the pads' scale, no exact proportions elsewhere: unverifiable.
# (test_one_misread_body_size_does_not_judge_the_pads, the page-frame and
# chamfer tests above, and the second halves of the two row 1 tests: a
# loose candidate elsewhere, alone, never refuses.)


def _outline_mask(rects_px, size=(700, 1300)) -> np.ndarray:
    """An ink mask holding only rectangles (left, top, right, bottom px), 2 px lines."""
    mask = np.zeros(size, np.uint8)
    for left, top, right, bottom in rects_px:
        cv2.rectangle(mask, (int(round(left)), int(round(top))), (int(round(right)), int(round(bottom))), 255, 2)
    return mask


@pytest.mark.parametrize("aspect", [1.0, 1.05, 1.23, 1.6, 2.0, 3.0])
def test_sweep_no_wrong_scale_is_certified_and_no_right_reading_refused_beside_the_outline(aspect):
    """Property of the table, swept: body 20 mm wide, lengths 20-60 mm, at 10 px/mm, a 3 px tolerance.

    The body outline is drawn exactly, with one frame 0.6-10 mm outside or
    a step 0.6-3.5 mm inside it. For every reading factor beyond the
    allowance the verdict is never "agrees" (rows 2 and 3). For the right
    reading it is never "contradicted" (rows 1 and 2); the outline alone
    agrees; so it does beside a frame 10 mm outside a clearly non-square
    body (20 mm more on both sides: no scale makes that the body); and
    beside any second square the square body is unverifiable (one margin
    keeps a square's proportions) unless that square is itself inside the
    allowance (a doubled outline: row 1).
    """
    ppmm, tolerance_px = 10.0, 3.0
    body = (20.0 * aspect, 20.0)
    centre = (650.0, 350.0)
    holes = [drawing_holes._Circle(centre[0] + dx, centre[1] + dy, 6.0, 1.0)
             for dx, dy in ((-50, -30), (50, -30), (50, 30), (-25, 30), (0, 0))]

    def rect(margin_mm: float):
        """The outline moved out (in when negative) by one margin, px."""
        half = (body[0] / 2 + margin_mm) * ppmm, (body[1] / 2 + margin_mm) * ppmm
        return centre[0] - half[0], centre[1] - half[1], centre[0] + half[0], centre[1] + half[1]

    for margin in (None, 0.6, 1.0, 2.0, 2.5, 5.0, 8.5, 10.0, -0.6, -1.0, -2.0, -3.5):
        mask = _outline_mask([rect(0.0)] + ([] if margin is None else [rect(margin)]))
        assert len(list(drawing_holes._outline_rects(mask, holes))) >= (1 if margin is None else 2), (aspect, margin)
        for factor in (0.5, 0.7, 2.0 / 2.54, 1 / 1.1, 1.1, 1.27, 2.0):
            scale = ppmm / factor
            # Beyond the check's resolution: the real outline itself is outside the allowance.
            assert not drawing_holes._within_allowance((body[0] * ppmm, body[1] * ppmm), body, scale, tolerance_px)
            verdict, _ = drawing_holes._body_check(mask, holes, body, scale, tolerance_px)
            assert verdict != "agrees", (aspect, margin, factor)
        verdict, chosen = drawing_holes._body_check(mask, holes, body, ppmm, tolerance_px)
        assert verdict != "contradicted", (aspect, margin)
        if margin is None or (margin == 10.0 and aspect >= 1.6):
            assert verdict == "agrees", (aspect, margin)
            # The rectangle handed back is the outline, not the frame.
            assert abs((chosen[2] - chosen[0]) - body[0] * ppmm) <= 2, (aspect, margin)
            assert abs((chosen[3] - chosen[1]) - body[1] * ppmm) <= 2, (aspect, margin)
        elif aspect == 1.0:
            moved = rect(margin)
            doubled = drawing_holes._within_allowance(
                (moved[2] - moved[0], moved[3] - moved[1]), body, ppmm, tolerance_px)
            assert verdict == ("agrees" if doubled else "unverifiable"), (aspect, margin)
            assert doubled or chosen is None, (aspect, margin)


# ---------------------------------------------------------------------------
# A10 -- proportional errors as ratios, per-hole noise in pixels
# ---------------------------------------------------------------------------

#: The same part four times larger (40 x 24 mm between holes), drawn at 20 px/mm.
BIG = _scaled(PART, 4.0)
BIG_PAGE = dict(ppmm=20.0, size=(1500, 1000), origin=(300, 200), hole_r_mm=0.5, boxes=())


@pytest.mark.parametrize("part, page", [(PART, dict(boxes=())), (BIG, BIG_PAGE)])
@pytest.mark.parametrize("stretch_x, expected", [
    (1.008, ("confirmed", "confirmed")),
    (1.016, ("confirmed", "confirmed")),
    (1.12, ("not_confirmed", "distorted")),
])
def test_a_scan_stretch_is_bounded_as_a_ratio_at_any_size(tmp_path, part, page, stretch_x, expected):
    """A page stretched along X by 0.8 % or 1.6 % confirms a small AND a large part; by 12 % neither.

    The large part at 20 px/mm is the case a pixel tolerance refused: the
    same ratio moves its far holes by more pixels. The allowance is the
    page's PAGE_STRETCH_RATIO plus what the pads can resolve (tolerance
    over their spread: 5 % for the small part at 10 px/mm, under 1 % for
    the large one), see the next test.
    """
    path = _page(tmp_path / "stretched.png", part, stretch_x=stretch_x, **page)
    result = check_pad_layout([path], part)
    assert (result.status, result.reason) == expected, result


def test_a_stretch_the_large_part_resolves_is_distorted_there_only(tmp_path):
    """6 % along X: beyond what the large part's pads allow, within what the small part's can resolve.

    The second term of the allowance is derived from the fit: a stretch
    that moves the small part's holes by about one tolerance cannot be
    told from the noise of its circle centres.
    """
    large = _page(tmp_path / "large.png", BIG, stretch_x=1.06, **BIG_PAGE)
    assert (check_pad_layout([large], BIG).reason) == "distorted"
    small = _page(tmp_path / "small.png", PART, stretch_x=1.06, boxes=())
    assert check_pad_layout([small], PART).status == "confirmed"


def test_a_misread_pad_on_a_stretched_page_is_not_absorbed_by_the_stretch(tmp_path):
    """The stretch moves every pad by one linear map; one pad moved alone still fails."""
    path = _page(tmp_path / "stretched.png", BIG, stretch_x=1.012, **BIG_PAGE)
    assert check_pad_layout([path], BIG).status == "confirmed"
    wrong = {**BIG, "5": (BIG["5"][0] + 0.6, BIG["5"][1])}
    assert check_pad_layout([path], wrong).status == "not_confirmed"


def test_a_stretched_fit_is_believed_only_on_circles_drawn_like_holes(tmp_path):
    """The same stretched page with rings that are not drawn like holes: unverifiable, not confirmed.

    The stretch allowance is the freest fit of the check; on glyph counters
    it would let a regular reading settle on a lattice of text.
    """
    holes = _page(tmp_path / "holes.png", BIG, stretch_x=1.012, **BIG_PAGE)
    assert check_pad_layout([holes], BIG).status == "confirmed"
    strokes = _page(tmp_path / "strokes.png", BIG, stretch_x=1.012, marks="diagonal", **BIG_PAGE)
    result = check_pad_layout([strokes], BIG)
    assert (result.status, result.reason) == ("unverifiable", "no_round_holes"), result
    # With an unstretched page the same undeclared circles do confirm (five pads, three witnesses).
    upright = _page(tmp_path / "upright.png", BIG, marks="diagonal", **BIG_PAGE)
    assert check_pad_layout([upright], BIG).status == "confirmed"


@pytest.mark.parametrize("rotation_deg, expected", [
    (2.0, ("confirmed", "confirmed")),
    (3.5, ("unverifiable", "page_skewed")),
    (5.0, ("unverifiable", "page_skewed")),
    (9.0, ("unverifiable", "page_skewed")),
    (25.0, ("unverifiable", "page_skewed")),
])
def test_a_rigidly_turned_page_is_skewed_never_distorted(tmp_path, rotation_deg, expected):
    """A rotation is no error of the reading: beyond the limit the page is to be deskewed."""
    path = _page(tmp_path / "turned.png", rotation_deg=rotation_deg, boxes=())
    result = check_pad_layout([path], PART)
    assert (result.status, result.reason) == expected, result


def test_a_tilted_chance_fit_does_not_hide_an_upright_contradiction(tmp_path):
    """A body drawn at 85 % around holes with centre lines: the cells between ring and lines

    offer fits at 3-5 degrees; the upright fit of every pad that the body
    contradicts must still decide (stage order), not a "page_skewed".
    """
    centre = ((BOX[0] + BOX[2]) / 2, (BOX[1] + BOX[3]) / 2)
    box = (centre[0] - 0.85 * BODY[0] / 2, centre[1] - 0.85 * BODY[1] / 2,
           centre[0] + 0.85 * BODY[0] / 2, centre[1] + 0.85 * BODY[1] / 2)
    path = _page(tmp_path / "body.png", size=(1000, 700), origin=(200, 150), boxes=(box,), marks="centre")
    result = check_pad_layout([path], PART, BODY)
    assert (result.status, result.reason) == ("not_confirmed", "body_scale"), result
    # Positive twin: the same page with the body as drawn agrees.
    drawn = check_pad_layout([path], PART, (0.85 * BODY[0], 0.85 * BODY[1]))
    assert (drawn.status, drawn.body_check) == ("confirmed", "agrees"), drawn


def test_a_wrong_reading_on_a_skewed_page_is_not_excused_by_the_skew(tmp_path):
    """Negative twin: on the page turned by 9 degrees a misplaced pad fits at no rotation."""
    path = _page(tmp_path / "turned.png", rotation_deg=9.0, boxes=())
    wrong = {**PART, "5": (5.0, 4.5)}
    result = check_pad_layout([path], wrong)
    assert (result.status, result.reason) == ("not_confirmed", "no_match"), result


@pytest.mark.parametrize("factor", [1.0, 4.0])
@pytest.mark.parametrize("drawn, expected", [
    (0.97, ("confirmed", "agrees")),
    (0.85, ("not_confirmed", "contradicted")),
])
def test_the_body_allowance_is_a_ratio_of_the_body(tmp_path, factor, drawn, expected):
    """A 16 x 12 mm and a 64 x 48 mm body, each drawn at 97 % and at 85 % of its quote.

    The same ratio gives the same answer at both sizes; a fixed allowance
    in millimetres accepted the small body and refused the large one.
    """
    part = _scaled(PART, factor)
    centre = ((BOX[0] + BOX[2]) / 2 * factor, (BOX[1] + BOX[3]) / 2 * factor)
    half = (BODY[0] * factor * drawn / 2, BODY[1] * factor * drawn / 2)
    box = (centre[0] - half[0], centre[1] - half[1], centre[0] + half[0], centre[1] + half[1])
    # Centre lines: a body drawn at 85 % passes within a diameter of the
    # holes, which are then drawn like holes by their lines.
    path = _page(tmp_path / "body.png", part, size=(1000, 700), origin=(200, 150), boxes=(box,),
                 marks="centre")
    result = check_pad_layout([path], part, (BODY[0] * factor, BODY[1] * factor))
    assert (result.status, result.body_check) == expected, result


def test_body_axes_are_distinguishable_exactly_when_the_allowances_do_not_overlap():
    """The separation is the sum of the two sizes' own allowances, as the body check applies them."""
    tolerance, scale = 0.1, 10.0
    for small in (12.0, 48.0):
        # Gap g at which g = allowance(small) + allowance(small + g).
        gap = 2 * body_allowance_mm(small, tolerance) / (1 - drawing_holes.BODY_SCALE_RATIO)
        near, far = (small, small + 0.9 * gap), (small, small + 1.2 * gap)
        assert not body_axes_distinguishable(near, tolerance)
        assert body_axes_distinguishable(far, tolerance)
        assert body_axes_distinguishable(far[::-1], tolerance)
        # Coherence with the check itself: a square outline whose side lies
        # between the two sizes (in proportion to their allowances) is inside
        # the allowance of the body in BOTH orders exactly below the separation.
        for body, both in ((near, True), (far, False)):
            first, second = (body_allowance_mm(size, tolerance) for size in body)
            side = (body[0] + (body[1] - body[0]) * first / (first + second)) * scale
            # The allowance alone (necessary for an agreement, no longer
            # sufficient since V1-1: a square is not this body's shape).
            orders = [drawing_holes._within_allowance((side, side), sizes, scale, tolerance * scale)
                      for sizes in (body, body[::-1])]
            assert (orders == [True, True]) is both, (body, orders)
    assert not body_axes_distinguishable((12.0, 40.0), None)  # no fit, no measure


def test_the_separation_of_body_axes_grows_with_the_body():
    """A ratio, not a fixed margin: 2 mm apart tells 12 from 14 mm, not 48 from 50 mm."""
    assert body_axes_distinguishable((12.0, 14.0), 0.1)
    assert not body_axes_distinguishable((48.0, 50.0), 0.1)
