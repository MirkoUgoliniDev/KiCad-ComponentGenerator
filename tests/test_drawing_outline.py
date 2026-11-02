"""Tests of the body outline drawn around the holes of a pin view: solid, dashed or dotted.

Synthetic pages are drawn with OpenCV in pytest's tmp_path: an invented
five-hole part at 10 px/mm with a rectangle around it in the style under
test. Every negative is paired with a positive that differs only in the
property under test (dash gap, text instead of a line, enclosure). Real
drawings of the repository are used only when present.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import drawing_holes
from kicadgen.services.drawing_holes import MeasuredOutline, check_pad_layout, measured_outline

ROOT = Path(__file__).resolve().parents[1]

# Invented asymmetric part, page orientation (Y down), mm.
PART = {1: (0.0, 0.0), 2: (10.0, 0.0), 3: (10.0, 6.0), 4: (2.5, 6.0), 5: (5.0, 3.0)}
PPMM = 10.0
ORIGIN = np.array([300.0, 220.0])
# Outline 3 mm outside the hole pattern: left, top, right, bottom mm.
BOX = (-3.0, -3.0, 13.0, 9.0)


def _px(x_mm: float, y_mm: float) -> tuple[int, int]:
    """Page pixel of a point in the part's mm frame."""
    return int(round(ORIGIN[0] + x_mm * PPMM)), int(round(ORIGIN[1] + y_mm * PPMM))


def _broken_line(img, p0, p1, on: int, off: int, thickness: int = 2) -> None:
    """Straight line from p0 to p1 drawn as ``on`` px dashes separated by ``off`` px gaps."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    length = float(np.linalg.norm(p1 - p0))
    step = (p1 - p0) / length
    pos = 0.0
    while pos < length:
        a = p0 + step * pos
        b = p0 + step * min(pos + on, length)
        cv2.line(img, tuple(int(round(v)) for v in a), tuple(int(round(v)) for v in b), (0, 0, 0), thickness)
        pos += on + off


def _rectangle(img, box_mm, style: str) -> None:
    """Rectangle in the part's mm frame, drawn solid, dashed, dotted or sparsely dotted."""
    left, top, right, bottom = box_mm
    corners = [_px(left, top), _px(right, top), _px(right, bottom), _px(left, bottom)]
    pattern = {"dashed": (8, 3), "dotted": (2, 4), "sparse": (2, 16)}
    for a, b in zip(corners, corners[1:] + corners[:1]):
        if style == "solid":
            cv2.line(img, a, b, (0, 0, 0), 2)
        else:
            _broken_line(img, a, b, *pattern[style])


def _text_column(img, x_px: int, y_top: int, y_bottom: int) -> None:
    """Left-aligned lines of text starting at ``x_px``: their capital stems stack into a column."""
    words = ["Rated 250 V", "Pollution 2", "Endurance", "Mounting PCB", "Rated 240 V", "Bounce 5 ms",
             "Dielectric", "Holding 1 W", "Release", "Pick-up 9 V", "Nominal", "Fused"]
    y = y_top + 11
    k = 0
    while y <= y_bottom:
        cv2.putText(img, words[k % len(words)], (x_px, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1,
                    cv2.LINE_AA)
        y += 14
        k += 1


def _page(path: Path, *, box: str | None = None, box_mm=BOX, holes=PART, text_rows: bool = False,
          dimensions: bool = False, text_columns: bool = False, rules: bool = False) -> Path:
    """Draw a pin view of PART and return its path.

    @param box Style of the outline rectangle ("solid", "dashed", "dotted",
        "sparse") or None for no rectangle.
    @param text_rows Lines of text above and below the holes, as wide as the outline.
    @param dimensions Dimension lines with arrows and extension lines around the holes.
    @param text_columns Left-aligned text columns left and right of the holes.
    @param rules Solid horizontal rules above and below the holes, as in a table.
    """
    img = np.full((520, 760, 3), 255, np.uint8)
    for x, y in holes.values():
        centre = (int(round((ORIGIN[0] + x * PPMM) * 16)), int(round((ORIGIN[1] + y * PPMM) * 16)))
        cv2.circle(img, centre, int(0.6 * PPMM * 16), (0, 0, 0), 1, cv2.LINE_AA, shift=4)
    if box is not None:
        _rectangle(img, box_mm, box)
    if text_rows:
        for y_mm in (-4.5, -6.0, 10.6, 12.1):
            x0, y0 = _px(-3.0, y_mm)
            cv2.putText(img, "Bottom view on solder pins 0.6 x 9", (x0, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                        (0, 0, 0), 1, cv2.LINE_AA)
    if dimensions:
        # Horizontal chain under the holes with extension lines, vertical one at the left.
        y_dim = _px(0, 11.0)[1]
        for x_mm in (0.0, 2.5, 10.0):
            cv2.line(img, (_px(x_mm, 0)[0], _px(0, 6.8)[1]), (_px(x_mm, 0)[0], y_dim + 6), (0, 0, 0), 1)
        for xa, xb in ((0.0, 2.5), (2.5, 10.0)):
            cv2.arrowedLine(img, ((_px(xa, 0)[0] + _px(xb, 0)[0]) // 2, y_dim), (_px(xa, 0)[0], y_dim),
                            (0, 0, 0), 1, cv2.LINE_AA, tipLength=0.2)
            cv2.arrowedLine(img, ((_px(xa, 0)[0] + _px(xb, 0)[0]) // 2, y_dim), (_px(xb, 0)[0], y_dim),
                            (0, 0, 0), 1, cv2.LINE_AA, tipLength=0.2)
        x_dim = _px(-5.0, 0)[0]
        for y_mm in (0.0, 6.0):
            cv2.line(img, (_px(-0.8, 0)[0], _px(0, y_mm)[1]), (x_dim - 6, _px(0, y_mm)[1]), (0, 0, 0), 1)
        cv2.arrowedLine(img, (x_dim, _px(0, 3.0)[1]), (x_dim, _px(0, 0.0)[1]), (0, 0, 0), 1, cv2.LINE_AA,
                        tipLength=0.2)
        cv2.arrowedLine(img, (x_dim, _px(0, 3.0)[1]), (x_dim, _px(0, 6.0)[1]), (0, 0, 0), 1, cv2.LINE_AA,
                        tipLength=0.2)
        cv2.putText(img, "6.0", (x_dim - 30, _px(0, 3.0)[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1,
                    cv2.LINE_AA)
        cv2.putText(img, "7.50", (_px(4.0, 0)[0], y_dim - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1,
                    cv2.LINE_AA)
    if rules:
        for y_mm in BOX[1], BOX[3]:
            cv2.line(img, _px(-16.0, y_mm), _px(30.0, y_mm), (0, 0, 0), 2)
    if text_columns:
        top, bottom = _px(0, BOX[1])[1] + 2, _px(0, BOX[3])[1] - 2
        # The left column starts 12 mm left of the holes and ends before them.
        _text_column(img, _px(-12.0, 0)[0], top, bottom)
        _text_column(img, _px(13.0, 0)[0], top, bottom)
    assert cv2.imwrite(str(path), img)
    return path


def _edges(outline: MeasuredOutline) -> tuple[float, float, float, float]:
    """Edges of a measured outline as (left, top, right, bottom) mm."""
    return outline.left_mm, outline.top_mm, outline.right_mm, outline.bottom_mm


def _assert_box(outline: MeasuredOutline | None, box_mm=BOX) -> None:
    """The measured outline lies on ``box_mm`` within the fit tolerance."""
    assert outline is not None
    for got, want in zip(_edges(outline), box_mm):
        assert abs(got - want) <= outline.tolerance_mm, (_edges(outline), box_mm)


# ---------------------------------------------------------------------------
# Line styles: solid, dashed and dotted sides are one outline; sparse dots are not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style", ["solid", "dashed", "dotted"])
def test_outline_is_measured_in_the_reading_frame_for_every_line_style(tmp_path, style):
    path = _page(tmp_path / f"{style}.png", box=style)
    _assert_box(measured_outline([path], PART))


def test_outline_follows_the_reading_origin(tmp_path):
    # The same drawing read with another origin: the outline moves with it.
    path = _page(tmp_path / "dashed.png", box="dashed")
    shifted = {k: (x + 4.0, y - 1.0) for k, (x, y) in PART.items()}
    left, top, right, bottom = BOX
    _assert_box(measured_outline([path], shifted), (left + 4.0, top - 1.0, right + 4.0, bottom - 1.0))


def test_dots_farther_apart_than_three_line_widths_are_not_a_side(tmp_path):
    # Same rectangle as the dotted positive, with gaps of 8 line widths:
    # separate marks, not one drawn line.
    path = _page(tmp_path / "sparse.png", box="sparse")
    assert measured_outline([path], PART) is None


def test_dashed_outline_now_takes_part_in_the_body_check(tmp_path):
    path = _page(tmp_path / "dashed.png", box="dashed")
    assert check_pad_layout([path], PART, (16.0, 12.0)).status == "confirmed"
    result = check_pad_layout([path], PART, (19.0, 12.0))
    assert (result.status, result.reason) == ("not_confirmed", "body_scale")


def test_body_disagreeing_with_the_outline_gives_no_outline(tmp_path):
    path = _page(tmp_path / "dashed.png", box="dashed")
    _assert_box(measured_outline([path], PART, (16.0, 12.0)))
    assert measured_outline([path], PART, (19.0, 12.0)) is None
    assert measured_outline([path], PART, (16.0, 14.0)) is None


# ---------------------------------------------------------------------------
# Negatives: no rectangle, text, a rectangle that does not enclose every hole
# ---------------------------------------------------------------------------

def test_text_rows_and_dimension_lines_are_not_an_outline(tmp_path):
    bare = _page(tmp_path / "bare.png", text_rows=True, dimensions=True)
    assert check_pad_layout([bare], PART).status == "confirmed"
    assert measured_outline([bare], PART) is None
    # Positive twin: the same page with a dashed outline added.
    boxed = _page(tmp_path / "boxed.png", box="dashed", text_rows=True, dimensions=True)
    _assert_box(measured_outline([boxed], PART))


def test_stacked_text_stems_between_table_rules_are_not_a_side(tmp_path, monkeypatch):
    # Solid rules above and below, left-aligned text columns at the place
    # of the vertical sides: the capital stems stack into dashed-looking
    # vertical runs, but each carries the rest of its letter beside it.
    path = _page(tmp_path / "table.png", rules=True, text_columns=True)
    assert check_pad_layout([path], PART).status == "confirmed"
    assert measured_outline([path], PART) is None
    # The negative exercises the clutter guard, not some other filter:
    # without it the text columns would close the rectangle.
    drawing_holes.clear_analysis_cache()
    monkeypatch.setattr(drawing_holes, "OUTLINE_NEIGHBOUR_MAX", 1.0)
    assert measured_outline([path], PART) is not None


def test_rectangle_not_enclosing_every_hole_is_not_the_outline(tmp_path):
    # Same dashed rectangle, its right side moved inside hole column x = 10.
    path = _page(tmp_path / "short.png", box="dashed", box_mm=(-3.0, -3.0, 7.0, 9.0))
    assert measured_outline([path], PART) is None
    path = _page(tmp_path / "solid_short.png", box="solid", box_mm=(-3.0, -3.0, 7.0, 9.0))
    assert measured_outline([path], PART) is None


def test_unconfirmed_reading_gives_no_outline(tmp_path):
    path = _page(tmp_path / "dashed.png", box="dashed")
    wrong = dict(PART)
    wrong[5] = (6.5, 3.0)
    assert check_pad_layout([path], wrong).status == "not_confirmed"
    assert measured_outline([path], wrong) is None


def test_measured_outline_validates_its_input(tmp_path):
    path = _page(tmp_path / "dashed.png", box="dashed")
    assert measured_outline([path], {1: (0.0, 0.0), 2: (1.0, 0.0)}) is None
    with pytest.raises(ValueError):
        measured_outline([path], {**PART, 6: (float("nan"), 0.0)})
    with pytest.raises(ValueError):
        measured_outline([path], PART, (16.0, -1.0))


# ---------------------------------------------------------------------------
# Real drawings of the repository (skipped when absent)
# ---------------------------------------------------------------------------

def _real(relative: str) -> Path:
    """Path of a real drawing, or skip the test when it is missing."""
    path = ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} not available")
    return path


# T7NS relay, bottom view on solder pins, dashed body outline 22.5 x 16.5 mm.
# The crop reading has its origin on the outline's left edge (pin 11 is
# quoted 2.05 mm from it), the page reading on pin 11.
T7N_CROP = {'11': (2.05, 6.0), 'A1': (4.05, 0.0), 'A2': (4.05, 12.0), '14': (16.25, 0.0), '12': (16.25, 12.0)}
T7N_PAGE = {'11': (0.0, 6.0), 'A1': (2.0, 0.0), 'A2': (2.0, 12.0), '14': (14.2, 0.0), '12': (14.2, 12.0)}
# Body edges in the crop reading's frame: the pins are about vertically
# centred in the 16.5 mm body ((16.5 - 12.0) / 2 = 2.25 mm margins).
T7N_BOX = (0.0, -2.25, 22.5, 14.25)
T7N_BODY = (22.5, 16.5)


def test_real_t7n_dashed_outline_on_the_crop():
    path = _real("tests/data/drawings/t7n_drawing_crop.png")
    outline = measured_outline([path], T7N_CROP, T7N_BODY)
    _assert_box(outline, T7N_BOX)
    assert check_pad_layout([path], T7N_CROP, T7N_BODY).status == "confirmed"
    result = check_pad_layout([path], T7N_CROP, (25.0, 16.5))
    assert (result.status, result.reason) == ("not_confirmed", "body_scale")


def test_real_t7n_dashed_outline_on_the_full_page():
    path = _real("tests/data/drawings/t7n_datasheet_page.png")
    outline = measured_outline([path], T7N_PAGE, T7N_BODY)
    left, top, right, bottom = T7N_BOX
    _assert_box(outline, (left - 2.05, top, right - 2.05, bottom))
    # The two copies of the drawing agree with each other once the origins match.
    crop = measured_outline([_real("tests/data/drawings/t7n_drawing_crop.png")], T7N_CROP)
    pairs = ((crop.left_mm, outline.left_mm + 2.05), (crop.top_mm, outline.top_mm),
             (crop.right_mm, outline.right_mm + 2.05), (crop.bottom_mm, outline.bottom_mm))
    limit = max(crop.tolerance_mm, outline.tolerance_mm)
    assert all(abs(a - b) <= limit for a, b in pairs), pairs


def test_real_relay_solid_outline_is_unchanged():
    # Solid outline of the VE12H relay bottom view: 20.5 x 10.5 mm body.
    path = _real("tests/data/drawings/ve12hk_quote_pinout.jpeg")
    relay = {4: (0.0, 0.0), 1: (2.54, -7.62), 5: (5.08, -2.54), 3: (17.78, 0.0), 2: (17.78, -7.62)}
    outline = measured_outline([path], relay, (20.5, 10.5))
    assert outline is not None
    allowed = 2 * outline.tolerance_mm + drawing_holes.BODY_ALLOWANCE_MM
    assert abs(outline.width_mm - 20.5) <= allowed
    assert abs(outline.height_mm - 10.5) <= allowed


# -- one hole pattern drawn twice on the page (generality audit M3, 2026-10-31) ----------

#: Mirror-symmetric pattern: both views confirm the same reading.
SYMMETRIC = {1: (0.0, 0.0), 2: (10.0, 0.0), 3: (0.0, 6.0), 4: (10.0, 6.0), 5: (5.0, 6.0)}


def _views_page(path: Path, views) -> Path:
    """A page drawing SYMMETRIC once per (origin px, dashed box mm) in `views`."""
    global ORIGIN
    img = np.full((520, 900, 3), 255, np.uint8)
    saved = ORIGIN
    try:
        for origin, box in views:
            ORIGIN = np.array(origin, float)
            for x, y in SYMMETRIC.values():
                centre = (int(round((origin[0] + x * PPMM) * 16)),
                          int(round((origin[1] + y * PPMM) * 16)))
                cv2.circle(img, centre, int(0.6 * PPMM * 16), (0, 0, 0), 1, cv2.LINE_AA, shift=4)
            _rectangle(img, box, "dashed")
    finally:
        ORIGIN = saved
    cv2.imwrite(str(path), img)
    drawing_holes.clear_analysis_cache()
    return path


def test_two_views_with_outlines_in_different_places_give_no_outline(tmp_path):
    """The body would move 4 mm depending on which view comes first: refused."""
    path = _views_page(tmp_path / "two.png", [((120, 200), (-2, -3, 16, 9)),
                                              ((560, 200), (-6, -3, 12, 9))])
    assert drawing_holes.measured_outline([path], SYMMETRIC, (18.0, 12.0)) is None


def test_one_view_of_the_same_pattern_gives_its_outline(tmp_path):
    """Contrast: the same pattern and box drawn once is measured."""
    path = _views_page(tmp_path / "one.png", [((120, 200), (-2, -3, 16, 9))])
    outline = drawing_holes.measured_outline([path], SYMMETRIC, (18.0, 12.0))
    assert outline is not None
    assert abs(outline.left_mm + 2.0) <= outline.tolerance_mm
    assert abs(outline.right_mm - 16.0) <= outline.tolerance_mm
