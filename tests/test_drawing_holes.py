"""Tests of the local check of a pad-layout reading against drawn hole circles.

Every synthetic drawing is made with OpenCV at run time in pytest's tmp_path:
an invented part with an outline, hole circles at known mm positions, dashed
centre lines, dimension lines with arrows and quote text (with the round
glyphs 0, 6, 8, 9). Real drawings of the repository are used only when present.

Each threshold-based negative is paired with a positive that differs only in
the property under test.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import drawing_holes
from kicadgen.services.drawing_holes import LayoutCheck, check_pad_layout

ROOT = Path(__file__).resolve().parents[1]

# Invented asymmetric part: five staggered holes, page orientation (Y down).
STAGGERED = {
    1: (0.0, 0.0),
    2: (5.08, 0.0),
    3: (2.54, -5.08),
    4: (12.7, -7.62),
    5: (15.24, -2.54),
}

# Invented DIP-like part: 2 x 8 holes, pitch 2.54 mm, rows 7.62 mm apart.
DIP16 = {**{n: ((n - 1) * 2.54, 0.0) for n in range(1, 9)},
         **{n: ((16 - n) * 2.54, -7.62) for n in range(9, 17)}}


def _to_px(points_mm, ppmm, centre_px, rotation_deg, mid_mm):
    """Map mm (page orientation) to page pixels around ``centre_px``."""
    angle = math.radians(rotation_deg)
    rot = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    rel = (np.asarray(points_mm, float) - mid_mm) * ppmm
    return rel @ rot.T + np.asarray(centre_px, float)


def _circle(img, centre, radius, thickness=1):
    """Anti-aliased circle at a sub-pixel centre."""
    cv2.circle(img, (int(round(centre[0] * 16)), int(round(centre[1] * 16))),
               int(round(radius * 16)), (0, 0, 0), thickness, cv2.LINE_AA, shift=4)


def _dashed(img, p0, p1, on=7, off=4):
    """Dashed straight line from p0 to p1."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    length = float(np.linalg.norm(p1 - p0))
    if length == 0:
        return
    step = (p1 - p0) / length
    pos = 0.0
    while pos < length:
        a = p0 + step * pos
        b = p0 + step * min(pos + on, length)
        cv2.line(img, tuple(int(round(v)) for v in a), tuple(int(round(v)) for v in b), (0, 0, 0), 1)
        pos += on + off


def _text(img, text, org, scale=0.5):
    """Dimension-style text."""
    cv2.putText(img, text, (int(org[0]), int(org[1])), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1,
                cv2.LINE_AA)


def draw_part(path, holes, *, ppmm=10.0, page=(1000, 700), hole_d_mm=1.0, margin_mm=3.0,
              rotation_deg=0.0, extra_circle=False, labels=True, centre_lines=True,
              label_beside=None):
    """Draw an invented part and return the path written.

    @param holes pad number -> (x, y) mm, page orientation (Y down).
    @param extra_circle add a mounting hole in a separate side view, away
        from the hole pattern (an extra circle that no pad uses).
    @param centre_lines draw dashed centre lines through the hole rows/columns.
    @param label_beside pad number whose hole gets a text label right beside
        it on the same line, like a word ("PIN 3"), which the glyph filter
        takes for a letter: the detector then misses that hole.
    """
    width, height = page
    img = np.full((height, width, 3), 255, np.uint8)
    pts = np.array(list(holes.values()), float)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    mid = (lo + hi) / 2
    centre = (width * 0.42, height * 0.42)
    to_px = lambda p: _to_px(p, ppmm, centre, rotation_deg, mid)  # noqa: E731
    r_px = hole_d_mm / 2 * ppmm

    # Outline of the body.
    corners = [(lo[0] - margin_mm, lo[1] - margin_mm), (hi[0] + margin_mm, lo[1] - margin_mm),
               (hi[0] + margin_mm, hi[1] + margin_mm), (lo[0] - margin_mm, hi[1] + margin_mm)]
    poly = np.round(to_px(corners)).astype(np.int32)
    cv2.polylines(img, [poly], True, (0, 0, 0), 2, cv2.LINE_AA)

    # Dashed centre lines through every row and column of holes, beyond the outline.
    for y in sorted({p[1] for p in pts}) if centre_lines else ():
        a, b = to_px([(lo[0] - margin_mm - 1.5, y), (hi[0] + margin_mm + 1.5, y)])
        _dashed(img, a, b)
    for x in sorted({p[0] for p in pts}) if centre_lines else ():
        a, b = to_px([(x, lo[1] - margin_mm - 1.5), (x, hi[1] + margin_mm + 1.5)])
        _dashed(img, a, b)

    # Holes and their numbers, offset diagonally like datasheet pin labels.
    for number, p in holes.items():
        c = to_px([p])[0]
        _circle(img, c, r_px)
        if labels:
            _text(img, str(number), (c[0] + r_px + 3, c[1] + r_px + 13), 0.4)
        if number == label_beside:
            # Glyphs as tall as the hole, a stroke width away on its left and right.
            cv2.putText(img, "PIN", (int(c[0] - r_px - 3 - 3 * 2 * r_px), int(c[1] + r_px)),
                        cv2.FONT_HERSHEY_SIMPLEX, 2 * r_px / 22.0, (0, 0, 0), 1, cv2.LINE_AA)
            cv2.putText(img, "3", (int(c[0] + r_px + 3), int(c[1] + r_px)),
                        cv2.FONT_HERSHEY_SIMPLEX, 2 * r_px / 22.0, (0, 0, 0), 1, cv2.LINE_AA)

    # Horizontal dimension chain under the outline, with extension lines and arrows.
    bottom = to_px([(lo[0], hi[1] + margin_mm)])[0][1]
    y_dim = bottom + 40
    xs = sorted({p[0] for p in pts})
    for x in xs:
        top_pt = to_px([(x, hi[1])])[0]
        cv2.line(img, (int(top_pt[0]), int(bottom + 6)), (int(top_pt[0]), int(y_dim + 8)), (0, 0, 0), 1)
    for xa, xb in zip(xs, xs[1:]):
        pa, pb = to_px([(xa, 0)])[0][0], to_px([(xb, 0)])[0][0]
        cv2.arrowedLine(img, (int((pa + pb) / 2), int(y_dim)), (int(pa), int(y_dim)), (0, 0, 0), 1,
                        cv2.LINE_AA, tipLength=0.15)
        cv2.arrowedLine(img, (int((pa + pb) / 2), int(y_dim)), (int(pb), int(y_dim)), (0, 0, 0), 1,
                        cv2.LINE_AA, tipLength=0.15)
        _text(img, f"{xb - xa:.2f}", ((pa + pb) / 2 - 16, y_dim - 6), 0.4)

    # Vertical dimension at the right with a quote written sideways-free.
    right = to_px([(hi[0] + margin_mm, 0)])[0][0]
    x_dim = right + 40
    ys = sorted({p[1] for p in pts})
    ya, yb = to_px([(0, ys[0])])[0][1], to_px([(0, ys[-1])])[0][1]
    cv2.arrowedLine(img, (int(x_dim), int((ya + yb) / 2)), (int(x_dim), int(ya)), (0, 0, 0), 1,
                    cv2.LINE_AA, tipLength=0.1)
    cv2.arrowedLine(img, (int(x_dim), int((ya + yb) / 2)), (int(x_dim), int(yb)), (0, 0, 0), 1,
                    cv2.LINE_AA, tipLength=0.1)
    _text(img, f"{ys[-1] - ys[0]:.2f}", (x_dim + 6, (ya + yb) / 2 + 5), 0.5)

    # Notes with round glyphs.
    _text(img, "Dimensions in mm  tol 0.08  69.06  8.0  0.6", (20, height - 40), 0.5)
    _text(img, "Unit: mm  2.54  7.62  10.16  0.89", (20, height - 15), 0.5)

    if extra_circle:
        c = (width - 90, 90)
        _circle(img, c, 1.5 * ppmm)
        cv2.line(img, (c[0] - 25, c[1]), (c[0] + 25, c[1]), (0, 0, 0), 1)
        cv2.line(img, (c[0], c[1] - 25), (c[0], c[1] + 25), (0, 0, 0), 1)

    assert cv2.imwrite(str(path), img)
    return path


def _check(path, pads, body=None) -> LayoutCheck:
    """Run the check on a single image."""
    return check_pad_layout([path], pads, body)


@pytest.fixture
def staggered(tmp_path):
    """Drawing of the invented staggered part."""
    return draw_part(tmp_path / "staggered.png", STAGGERED)


@pytest.fixture
def dip(tmp_path):
    """Drawing of the invented DIP-like part."""
    return draw_part(tmp_path / "dip.png", DIP16, ppmm=12.0, page=(1000, 600))


# ---------------------------------------------------------------------------
# Positives
# ---------------------------------------------------------------------------

def test_correct_staggered_reading_is_confirmed(staggered):
    result = _check(staggered, STAGGERED)
    assert result.status == "confirmed", result
    assert result.reason == "confirmed"
    assert result.image == "staggered.png"
    assert result.circles >= len(STAGGERED)
    assert result.max_error_mm <= result.tolerance_mm


def test_correct_dip_reading_is_confirmed(dip):
    result = _check(dip, DIP16)
    assert result.status == "confirmed", result


def test_origin_of_the_reading_is_arbitrary(staggered):
    shifted = {k: (x + 31.7, y - 4.2) for k, (x, y) in STAGGERED.items()}
    assert _check(staggered, shifted).status == "confirmed"


# ---------------------------------------------------------------------------
# Negatives, each paired with the positive above or a dedicated one
# ---------------------------------------------------------------------------

def test_two_quotes_swapped_in_a_chain_are_not_confirmed(staggered):
    # X chain 0 / 2.54 / 5.08 / 12.7 / 15.24: offsets 2.54 2.54 7.62 2.54.
    # Exchanging the last two offsets keeps the sum (15.24) but moves pad 4.
    swapped = dict(STAGGERED)
    swapped[4] = (5.08 + 2.54, -7.62)
    result = _check(staggered, swapped)
    assert result.status == "not_confirmed", result


def test_mirrored_reading_is_not_confirmed(staggered):
    mirrored = {k: (-x, y) for k, (x, y) in STAGGERED.items()}
    result = _check(staggered, mirrored)
    assert result.status == "not_confirmed", result


@pytest.mark.parametrize("shift_mm, expected", [(0.0, "confirmed"), (1.27, "not_confirmed")])
def test_one_pad_shifted_by_half_a_pitch(staggered, shift_mm, expected):
    reading = dict(STAGGERED)
    reading[3] = (STAGGERED[3][0] + shift_mm, STAGGERED[3][1])
    assert _check(staggered, reading).status == expected


def test_reading_with_an_extra_pad_without_circle_is_not_confirmed(tmp_path):
    # The page has six circles (one mounting hole in a side view), so the
    # check runs; the sixth pad of the reading has no drawn hole.
    path = draw_part(tmp_path / "extra.png", STAGGERED, extra_circle=True)
    assert _check(path, STAGGERED).status == "confirmed"
    reading = {**STAGGERED, 6: (10.16, -10.16)}
    result = _check(path, reading)
    assert result.status == "not_confirmed", result
    assert result.reason == "no_match"


# 2 x 20 header, 2.54 mm pitch, rows 7.62 mm apart.
HEADER40 = {**{n: ((n - 1) * 2.54, 0.0) for n in range(1, 21)},
            **{n: ((40 - n) * 2.54, -7.62) for n in range(21, 41)}}


@pytest.mark.parametrize("pitch, expected", [(2.54, "confirmed"), (2.45, "not_confirmed")])
def test_whole_x_axis_read_with_a_wrong_pitch(tmp_path, pitch, expected):
    # Every X quote 3.5 % short: the far pad is 1.7 mm off. A least-squares
    # similarity would absorb most of it as a uniform scale; the stretch it
    # needs, expressed as a displacement over the 7.62 mm rows (0.27 mm),
    # exceeds the 0.15 mm tolerance, so it must not be confirmed.
    path = draw_part(tmp_path / "hdr.png", HEADER40, ppmm=20.0, page=(1300, 700))
    reading = {k: (x * pitch / 2.54, y) for k, (x, y) in HEADER40.items()}
    result = _check(path, reading)
    assert result.status == expected, result
    if expected == "not_confirmed":
        assert result.reason == "distorted"


@pytest.mark.parametrize("factor, expected", [(1.0, "confirmed"), (1.10, "not_confirmed")])
def test_x_quotes_scaled_against_y_quotes(staggered, factor, expected):
    # Every X quote 10 % long: 0.76 mm over the 7.62 mm Y extent, far beyond
    # the 0.3 mm tolerance at 10 px/mm.
    reading = {k: (x * factor, y) for k, (x, y) in STAGGERED.items()}
    result = _check(staggered, reading)
    assert result.status == expected, result
    if expected == "not_confirmed":
        assert result.reason in ("distorted", "no_match")


@pytest.mark.parametrize("row_mm, expected", [(7.62, "confirmed"), (7.95, "not_confirmed")])
def test_row_spacing_off_by_a_third_of_a_millimetre(tmp_path, row_mm, expected):
    path = draw_part(tmp_path / "dip20.png", DIP16, ppmm=20.0, page=(1300, 800))
    reading = {k: (x, -row_mm if y else 0.0) for k, (x, y) in DIP16.items()}
    assert _check(path, reading).status == expected


@pytest.mark.parametrize("y3, expected", [(-5.08, "confirmed"), (-5.30, "not_confirmed")])
def test_three_pads_are_checked_by_the_similarity(tmp_path, y3, expected):
    # An affine map fits any three points; the similarity fitted on the
    # other two pads still sees a 0.22 mm error on the third.
    tri = {1: (0.0, 0.0), 2: (5.08, 0.0), 3: (2.54, -5.08)}
    path = draw_part(tmp_path / "tri.png", tri, ppmm=20.0, page=(900, 700))
    reading = {**tri, 3: (2.54, y3)}
    assert _check(path, reading).status == expected


def test_drawing_not_to_scale_confirms_the_drawn_proportions(tmp_path):
    # DIP rows quoted 7.62 but drawn at 10.16: the right reading needs a
    # stretch ("distorted"), the reading that copies the drawn proportions
    # fits. The extractor must therefore never let the latter win while the
    # right one is "distorted" (only "no_match" lets a competitor win).
    drawn = {k: (x, -10.16 if y else 0.0) for k, (x, y) in DIP16.items()}
    path = draw_part(tmp_path / "nts.png", drawn, ppmm=12.0, page=(1000, 600))
    right = _check(path, DIP16)
    assert (right.status, right.reason) == ("not_confirmed", "distorted"), right
    assert _check(path, drawn).status == "confirmed"


def test_page_with_bottom_view_and_mirrored_top_view(tmp_path):
    # Datasheets often print both views. The correct (bottom-view) reading
    # confirms; its mirror confirms too, on the top view drawn at another
    # scale. Documented: the extractor rejects a reading whose mirror also
    # confirms on the same image.
    mirrored = {k: (-x, y) for k, (x, y) in STAGGERED.items()}
    bottom = cv2.imread(str(draw_part(tmp_path / "bottom.png", STAGGERED, page=(700, 700))))
    top = cv2.imread(str(draw_part(tmp_path / "top.png", mirrored, page=(700, 700), ppmm=8.0)))
    path = tmp_path / "two_views.png"
    cv2.imwrite(str(path), np.hstack([bottom, top]))
    assert _check(path, STAGGERED).status == "confirmed"
    assert _check(path, mirrored).status == "confirmed"


@pytest.mark.parametrize("ppmm", [12.0, 20.0])
def test_near_collinear_zigzag_row(tmp_path, ppmm):
    # Locking header: pads alternately offset by +-0.127 mm. The straight
    # reading is wrong. At 20 px/mm the offset (2.5 px) is resolved: the
    # zigzag confirms and the straight row does not. At 12 px/mm the offset
    # (1.5 px) is below the 3 px tolerance and both confirm (the extractor
    # then adopts neither). Never the wrong one alone.
    zigzag = {n: ((n - 1) * 2.54, 0.127 if n % 2 else -0.127) for n in range(1, 9)}
    straight = {n: ((n - 1) * 2.54, 0.0) for n in range(1, 9)}
    path = draw_part(tmp_path / "zig.png", zigzag, ppmm=ppmm, page=(1000, 600), centre_lines=False)
    right, wrong = _check(path, zigzag), _check(path, straight)
    assert not (wrong.status == "confirmed" and right.status != "confirmed")
    assert right.status == "confirmed"
    if ppmm >= 20.0:
        assert wrong.status == "not_confirmed"


@pytest.mark.parametrize("ppmm, expected", [(4.0, "low_resolution"), (12.0, "confirmed")])
def test_low_resolution_drawing_is_unverifiable(tmp_path, ppmm, expected):
    # At 4 px/mm the tolerance (3 px = 0.75 mm) exceeds a quarter of the
    # 2.54 mm pitch: a half-pitch error would not be twice the tolerance.
    # Plain circles: at 4 px/mm dashed centre lines between 10 px holes leave
    # too few detected holes to reach a fit at all ("too_few_circles").
    path = draw_part(tmp_path / "low.png", DIP16, ppmm=ppmm, page=(900, 600), hole_d_mm=1.5,
                     labels=False, centre_lines=False)
    result = _check(path, DIP16)
    assert (result.reason if expected == "low_resolution" else result.status) == expected, result
    if expected == "low_resolution":
        assert result.status == "unverifiable"


def test_dip_row_spacing_read_from_the_wrong_quote_is_not_confirmed(dip):
    wrong = {k: (x, -10.16 if y else 0.0) for k, (x, y) in DIP16.items()}
    assert _check(dip, wrong).status == "not_confirmed"


@pytest.mark.parametrize("rotation_deg, confirmed", [(1.5, True), (8.0, False)])
def test_page_rotation_limit(tmp_path, rotation_deg, confirmed):
    # Beyond MAX_ROTATION_DEG the page must be deskewed first. At 8 degrees
    # the centre lines are no longer horizontal/vertical, so the circles do
    # not count as hole evidence either and the answer is "unverifiable".
    path = draw_part(tmp_path / "rot.png", STAGGERED, rotation_deg=rotation_deg)
    assert (_check(path, STAGGERED).status == "confirmed") is confirmed


@pytest.mark.parametrize("shift_mm, expected", [(0.0, "confirmed"), (1.27, "not_confirmed")])
def test_single_row_header(tmp_path, shift_mm, expected):
    # One row of pads: fitted with a similarity (the scale across the row is
    # not determined by the reading).
    row = {n: ((n - 1) * 2.54, 0.0) for n in range(1, 9)}
    path = draw_part(tmp_path / "row.png", row, ppmm=12.0)
    reading = dict(row)
    reading[3] = (row[3][0] + shift_mm, 0.0)
    assert _check(path, reading).status == expected


@pytest.mark.parametrize("shift_mm, expected", [(0.0, "confirmed"), (1.27, "not_confirmed")])
def test_plain_holes_without_centre_lines_still_count_as_holes(tmp_path, shift_mm, expected):
    # Circles standing alone on paper are hole evidence even without centre
    # lines, so a wrong reading is contradicted, not left unverifiable.
    path = draw_part(tmp_path / "plain.png", STAGGERED, centre_lines=False, labels=False)
    reading = dict(STAGGERED)
    reading[3] = (STAGGERED[3][0] + shift_mm, STAGGERED[3][1])
    assert _check(path, reading).status == expected


@pytest.mark.parametrize("shift_mm, expected", [(0.0, "unverifiable"), (1.27, "not_confirmed")])
def test_hole_missed_by_the_detector_does_not_contradict(tmp_path, shift_mm, expected):
    # Pad 3's hole is taken for a letter (a word written right beside it):
    # the correct reading puts pad 3 on a closed ring the detector dropped,
    # so the image cannot contradict it; a wrong pad 3 lands on paper.
    path = draw_part(tmp_path / "missed.png", STAGGERED, label_beside=3)
    reading = dict(STAGGERED)
    reading[3] = (STAGGERED[3][0] + shift_mm, STAGGERED[3][1] - shift_mm)
    result = _check(path, reading)
    assert result.status == expected, result


# ---------------------------------------------------------------------------
# Body scale
# ---------------------------------------------------------------------------

def test_body_size_agreeing_with_the_outline_is_confirmed(dip):
    # Outline = hole span + 2 x 3 mm margin.
    body = (7 * 2.54 + 6.0, 7.62 + 6.0)
    assert _check(dip, DIP16, body).status == "confirmed"


def test_uniformly_rescaled_reading_needs_the_body_to_be_caught(dip):
    # Every quote x1.25 (e.g. a 2.54 grid read as 3.175): the point set is
    # similar, so without the body the check cannot see it (documented limit).
    scaled = {k: (x * 1.25, y * 1.25) for k, (x, y) in DIP16.items()}
    assert _check(dip, scaled).status == "confirmed"
    body = (7 * 2.54 + 6.0, 7.62 + 6.0)
    result = _check(dip, scaled, body)
    assert result.status == "not_confirmed", result
    assert result.reason == "body_scale"


# ---------------------------------------------------------------------------
# Documented limits
# ---------------------------------------------------------------------------

def test_limit_swapped_pad_numbers_are_confirmed_because_only_geometry_is_checked(staggered):
    swapped = dict(STAGGERED)
    swapped[1], swapped[2] = STAGGERED[2], STAGGERED[1]
    assert _check(staggered, swapped).status == "confirmed"


def test_limit_reading_missing_a_pad_is_still_confirmed(staggered):
    partial = {k: v for k, v in STAGGERED.items() if k != 5}
    assert _check(staggered, partial).status == "confirmed"


def test_limit_mirror_of_a_symmetric_layout_is_the_same_point_set(dip):
    mirrored = {k: (-x, y) for k, (x, y) in DIP16.items()}
    assert _check(dip, mirrored).status == "confirmed"


# ---------------------------------------------------------------------------
# Unverifiable cases and input handling
# ---------------------------------------------------------------------------

def test_photo_like_image_is_not_a_drawing(tmp_path):
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 256, (600, 800), dtype=np.uint8)
    gradient = np.tile(np.linspace(40, 220, 800, dtype=np.float32), (600, 1))
    img = ((noise.astype(np.float32) * 0.5 + gradient * 0.5)).astype(np.uint8)
    path = tmp_path / "photo.jpg"
    cv2.imwrite(str(path), cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    result = _check(path, STAGGERED)
    assert (result.status, result.reason) == ("unverifiable", "not_a_drawing")
    assert result.image is None and result.circles == 0


def test_text_only_page_has_too_few_circles(tmp_path):
    img = np.full((700, 1000, 3), 255, np.uint8)
    for row in range(12):
        _text(img, f"{row}.80  6.09  0.68  9.60  R0.8  Q86  B9  e6  2.54  {row * 1.27:.2f}",
              (30, 50 + row * 50), 0.6)
    path = tmp_path / "text.png"
    cv2.imwrite(str(path), img)
    result = _check(path, STAGGERED)
    assert (result.status, result.reason) == ("unverifiable", "too_few_circles"), result
    assert result.image == "text.png"


def test_unreadable_and_foreign_files_are_skipped(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image at all")
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    pdf = tmp_path / "datasheet.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    missing = tmp_path / "missing.png"
    result = check_pad_layout([broken, empty, pdf, missing], STAGGERED)
    assert (result.status, result.reason) == ("unverifiable", "no_image")


def test_any_confirming_image_confirms(tmp_path, staggered):
    text_page = tmp_path / "text.png"
    cv2.imwrite(str(text_page), np.full((300, 400, 3), 255, np.uint8))
    result = check_pad_layout([text_page, staggered], STAGGERED)
    assert result.status == "confirmed"
    assert result.image == "staggered.png"


def test_not_confirmed_wins_over_unverifiable(tmp_path, staggered):
    blank = tmp_path / "blank.png"
    cv2.imwrite(str(blank), np.full((300, 400, 3), 255, np.uint8))
    mirrored = {k: (-x, y) for k, (x, y) in STAGGERED.items()}
    result = check_pad_layout([blank, staggered], mirrored)
    assert result.status == "not_confirmed"
    assert result.image == "staggered.png"


def test_transparent_png_is_composited_on_white(tmp_path, staggered):
    bgr = cv2.imread(str(staggered))
    alpha = np.where(bgr.min(axis=2) < 250, 255, 0).astype(np.uint8)
    bgra = np.dstack([np.zeros_like(bgr), alpha])  # black ink on transparent
    path = tmp_path / "transparent.png"
    cv2.imwrite(str(path), bgra)
    assert _check(path, STAGGERED).status == "confirmed"


def test_too_few_and_too_many_pads(staggered):
    # Two pads fix scale, rotation and position: any two circles in the
    # right direction would fit them, so nothing is left to check.
    two = _check(staggered, {1: STAGGERED[1], 4: STAGGERED[4]})
    assert (two.status, two.reason) == ("unverifiable", "too_few_pads")
    three = _check(staggered, {k: STAGGERED[k] for k in (1, 3, 4)})
    assert three.status == "confirmed"
    many = {n: (n * 2.54, 0.0) for n in range(drawing_holes.MAX_PADS + 1)}
    result = _check(staggered, many)
    assert (result.status, result.reason) == ("unverifiable", "too_many_pads")


def test_non_finite_values_are_programming_errors(staggered):
    with pytest.raises(ValueError):
        _check(staggered, {1: (0.0, 0.0), 2: (float("nan"), 0.0)})
    with pytest.raises(ValueError):
        _check(staggered, STAGGERED, (0.0, 5.0))


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

def test_large_page_with_forty_pads_is_checked_in_bounded_time(tmp_path):
    header = {**{n: ((n - 1) * 2.54, 0.0) for n in range(1, 21)},
              **{n: ((n - 21) * 2.54, -2.54) for n in range(21, 41)}}
    path = draw_part(tmp_path / "header.png", header, ppmm=20.0, page=(1600, 1200))
    start = time.perf_counter()
    result = _check(path, header)
    elapsed = time.perf_counter() - start
    assert result.status == "confirmed", result
    # Generous bound: a few seconds on a laptop, slack for a loaded CI runner.
    assert elapsed < 30.0, elapsed


def test_missed_hole_search_on_a_large_scale_drawing_is_bounded(tmp_path):
    # 100 px/mm: holes of 40 px radius; a wrong reading goes through the
    # missed-hole search, whose cost grows with the radius.
    path = draw_part(tmp_path / "big.png", STAGGERED, ppmm=100.0, page=(1990, 1990),
                     hole_d_mm=0.8, labels=False)
    wrong = dict(STAGGERED)
    wrong[3] = (STAGGERED[3][0] + 1.27, STAGGERED[3][1] - 1.27)
    start = time.perf_counter()
    result = _check(path, wrong)
    elapsed = time.perf_counter() - start
    assert result.status != "confirmed", result
    assert elapsed < 30.0, elapsed


def test_analysis_is_cached_per_file(tmp_path, staggered):
    drawing_holes.clear_analysis_cache()
    first = time.perf_counter()
    _check(staggered, STAGGERED)
    first = time.perf_counter() - first
    second = time.perf_counter()
    result = _check(staggered, {k: (x + 1.0, y) for k, (x, y) in STAGGERED.items()})
    second = time.perf_counter() - second
    assert result.status == "confirmed"
    assert second < first / 2, (first, second)


# ---------------------------------------------------------------------------
# Real drawings of the repository (skipped when absent)
# ---------------------------------------------------------------------------

def _real(relative):
    """Path of a real drawing, or skip the test when it is missing."""
    path = ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} not available")
    return path


# Relay bottom view, page orientation, origin at pad 4, Y down.
RELAY = {4: (0.0, 0.0), 1: (2.54, -7.62), 5: (5.08, -2.54), 3: (17.78, 0.0), 2: (17.78, -7.62)}


def test_real_relay_bottom_view():
    path = _real("generated/VE12H-K/docs/quote-pinout.jpeg")
    assert _check(path, RELAY).status == "confirmed"
    assert _check(path, RELAY, (20.5, 10.5)).status == "confirmed"
    wrong = dict(RELAY)
    wrong[1], wrong[5] = (RELAY[1][0], RELAY[5][1]), (RELAY[5][0], RELAY[1][1])
    assert _check(path, wrong).status == "not_confirmed"


def test_real_relay_low_resolution_is_not_contradicted():
    # Earlier, smaller copy of the same page: the middle hole is not detected
    # and the lower-left one is pulled by a leader line. The correct reading
    # must not be reported as wrong; a wrong one still is.
    path = _real("generated/VE12H-K/docs/.history/a2cd1dd9e18e46b19efef7ce05e395f6/quote.png")
    assert _check(path, RELAY).status != "not_confirmed"
    wrong = dict(RELAY)
    wrong[1], wrong[5] = (RELAY[1][0], RELAY[5][1]), (RELAY[5][0], RELAY[1][1])
    assert _check(path, wrong).status == "not_confirmed"


def test_real_tel8_bottom_view():
    path = _real("docs/MFG_TEL-8-2411WI/quote.png")
    reading = {1: (0.0, 0.0), 7: (15.24, 0.0), 8: (17.78, 0.0),
               16: (0.0, 10.16), 10: (15.24, 10.16), 9: (17.78, 10.16)}
    assert _check(path, reading, (23.8, 13.7)).status == "confirmed"
    # The 15.24 chain read as 12.7: pads 7, 8, 10 and 9 all move 2.54 mm left.
    wrong = {k: (x - 2.54 if x else x, y) for k, (x, y) in reading.items()}
    assert _check(path, wrong).status == "not_confirmed"


@pytest.mark.parametrize("relative", ["docs/ESP32/Dimensioni.png", "docs/RS485/Quote.jpg"])
def test_real_product_photos_with_quotes_are_not_drawings(relative):
    path = _real(relative)
    result = _check(path, RELAY)
    assert (result.status, result.reason) == ("unverifiable", "not_a_drawing")
