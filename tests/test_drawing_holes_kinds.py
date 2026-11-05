"""Tests of hole kinds, slots, closed lands and scale observability in drawing_holes.

Review findings of 2026-11-04 (H4, H5, M11, M18 and the mirror-helper low):

1. a slot (obround: two semicircles joined by two parallel segments) is a
   drawn hole with a centre, carried beside the rings with ``kind``
   "obround"; a pad read on it is matched, never «missing»;
2. a free hole of another kind than the pad's declared kind, or than the
   kind of the anchored pads' holes, is never chosen for a move; declared
   reference holes are excluded from the free candidates;
3. a pad whose reading lands inside a closed non-circular contour (a slot
   or a rectangle land) is unverifiable, never a miss that contradicts;
4. the scale is observable only from AFFINE_MIN_PADS non-collinear anchored
   pads: a failed fit of fewer pads is "no_match_unobservable", not
   "no_match", and a pad is never moved from an anchoring that cannot show
   the page's anisotropy, nor from one that shows it;
5. a page without any circle drawn like a hole is "no_round_holes";
6. the mirror layouts tested by the measure are those of
   :func:`drawing_holes.mirror_variants`.

Every page is drawn with OpenCV at run time. Each negative differs from its
positive by the structural property under test (a kind, a closed contour,
the number or the collinearity of the anchored pads, the page's scale),
never by a tuned number.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from kicadgen.services import drawing_holes
from kicadgen.services.drawing_holes import (
    LayoutCheck, MeasureOutcome, MeasuredLayout, check_pad_layout, measured_layout,
    measured_layout_outcome, mirror_variants)

#: Four terminal holes on a rectangle (page frame, mm): an affine-observable anchoring.
RECT4 = {1: (0.0, 0.0), 2: (10.0, 0.0), 3: (10.0, 8.0), 4: (0.0, 8.0)}
#: Pad 5 on a slot below them, pad "M" a mounting hole of about the same size.
SLOT5 = (5.0, 14.0)
MOUNT = (5.0, 17.0)


def _draw(path, *, rings=(), slots=(), rects=(), ppmm=10.0, origin=(150.0, 110.0),
          size=(620, 440), scale_y=1.0, centre_lines=(), text=True):
    """Draw a page with ring holes, obround slots and outlined rectangles; return ``path``.

    @param rings [(x, y, diameter)] mm; @param slots [(x, y, length, width)]
        mm, axis along page X; @param rects [(x, y, length, width)] mm.
    @param scale_y Vertical scale of the page relative to the quotes (a
        page not drawn to scale along Y when not 1).
    @param centre_lines Points (x, y) mm through which a horizontal and a
        vertical dashed centre line are drawn, beyond the hole.
    @param text Draw a caption line (its round glyphs are circles the
        detector finds and the glyph filter is expected to refuse).
    """
    width, height = size
    img = np.full((height, width, 3), 255, np.uint8)
    if text:
        cv2.putText(img, "Bottom view  dimensions in mm", (12, height - 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (0, 0, 0), 1, cv2.LINE_AA)

    def px(x, y):
        """Page px of a point in mm."""
        return origin[0] + x * ppmm, origin[1] + y * ppmm * scale_y

    def shifted(p):
        """Sub-pixel integer coordinates for shift=4 drawing."""
        return int(round(p[0] * 16)), int(round(p[1] * 16))

    for x, y, diameter in rings:
        cv2.circle(img, shifted(px(x, y)), int(round(diameter / 2 * ppmm * 16)), (0, 0, 0), 1,
                   cv2.LINE_AA, shift=4)
    for x, y, length, slot_width in slots:
        a = (length - slot_width) / 2 * ppmm
        r = slot_width / 2 * ppmm
        cx, cy = px(x, y)
        for colour, radius in (((0, 0, 0), r + 0.5), ((255, 255, 255), r - 0.5)):
            cv2.circle(img, shifted((cx - a, cy)), int(round(radius * 16)), colour, -1, cv2.LINE_AA, shift=4)
            cv2.circle(img, shifted((cx + a, cy)), int(round(radius * 16)), colour, -1, cv2.LINE_AA, shift=4)
            cv2.rectangle(img, shifted((cx - a, cy - radius)), shifted((cx + a, cy + radius)), colour, -1,
                          cv2.LINE_AA, shift=4)
    for x, y, length, rect_width in rects:
        cx, cy = px(x, y)
        cv2.rectangle(img, shifted((cx - length / 2 * ppmm, cy - rect_width / 2 * ppmm)),
                      shifted((cx + length / 2 * ppmm, cy + rect_width / 2 * ppmm)), (0, 0, 0), 1,
                      cv2.LINE_AA, shift=4)
    for x, y in centre_lines:
        cx, cy = px(x, y)
        for p0, p1 in (((cx - 40, cy), (cx + 40, cy)), ((cx, cy - 40), (cx, cy + 40))):
            pos = 0.0
            p0, p1 = np.array(p0, float), np.array(p1, float)
            step = (p1 - p0) / np.linalg.norm(p1 - p0)
            while pos < 80:
                a_pt, b_pt = p0 + step * pos, p0 + step * min(pos + 6, 80)
                cv2.line(img, tuple(int(round(v)) for v in a_pt), tuple(int(round(v)) for v in b_pt),
                         (0, 0, 0), 1)
                pos += 9
    assert cv2.imwrite(str(path), img)
    return path


def _rings(points, diameter=1.2):
    """Ring tuples of a pad dict."""
    return [(x, y, diameter) for x, y in points.values()]


@pytest.fixture
def slot_page(tmp_path):
    """Four terminal rings, one slot for pad 5 and one mounting hole of about the same size."""
    return _draw(tmp_path / "slot.png", rings=_rings(RECT4) + [(*MOUNT, 1.6)],
                 slots=[(*SLOT5, 3.0, 1.2)])


# ---------------------------------------------------------------------------
# Slots are holes with a centre
# ---------------------------------------------------------------------------

def test_slot_is_found_as_an_obround_with_its_centre(slot_page):
    analysis = drawing_holes._analyse(slot_page)
    obrounds = [c for c in analysis.circles if c.kind == "obround"]
    assert len(obrounds) == 1, analysis.circles
    assert math.hypot(obrounds[0].x - (150 + 50), obrounds[0].y - (110 + 140)) <= 1.5
    assert obrounds[0].evidence
    # Five rings drawn like holes; the counters of the text line are not.
    assert sum(c.kind == "ring" and c.evidence for c in analysis.circles) == 5


@pytest.mark.parametrize("with_centre_lines", [False, True])
def test_slot_crossed_by_centre_lines_is_still_one_obround(tmp_path, with_centre_lines):
    # The same slot with and without dashed centre lines through it: the
    # lines split its interior into cells, which must merge back into one.
    path = _draw(tmp_path / "lines.png", rings=_rings(RECT4), slots=[(*SLOT5, 3.0, 1.2)],
                 centre_lines=[SLOT5] if with_centre_lines else ())
    analysis = drawing_holes._analyse(path)
    obrounds = [c for c in analysis.circles if c.kind == "obround"]
    assert len(obrounds) == 1, analysis.circles
    assert math.hypot(obrounds[0].x - 200, obrounds[0].y - 250) <= 1.5


def test_reading_with_a_pad_on_the_slot_is_confirmed_and_nothing_is_moved(slot_page):
    reading = {**RECT4, 5: SLOT5}
    result = check_pad_layout([slot_page], reading)
    assert result.status == "confirmed", result
    assert result.scale_observable
    measured = measured_layout([slot_page], reading)
    assert isinstance(measured, MeasuredLayout) and measured.moved is None


def test_pad_off_its_slot_is_not_moved_onto_the_mounting_hole(slot_page):
    # H4: pad 5 read 2 mm below its slot, a mounting hole of about the same
    # size 1 mm further. Without the slot the mounting hole would be «the
    # only free hole»; with it, two candidates: nothing is chosen.
    reading = {**RECT4, 5: (5.0, 16.0)}
    outcome = measured_layout_outcome([slot_page], reading)
    assert isinstance(outcome, MeasureOutcome)
    assert outcome.layout is None and outcome.refused == "ambiguous", outcome
    assert measured_layout([slot_page], reading) is None


def test_declared_slot_kind_chooses_the_obround(slot_page):
    reading = {**RECT4, 5: (5.0, 16.0)}
    outcome = measured_layout_outcome([slot_page], reading, hole_kinds={5: "slot"})
    assert outcome.layout is not None, outcome
    assert outcome.layout.moved == 5
    assert math.dist(outcome.layout.positions_mm[5], SLOT5) <= outcome.layout.tolerance_mm
    assert outcome.layout.scale_observable


def test_declared_terminal_kind_refuses_the_obround(slot_page):
    # The mounting hole is declared as a reference (excluded); the only
    # free hole left is the slot, whose kind is not the pad's: refused.
    reading = {**RECT4, 5: (5.0, 16.0)}
    outcome = measured_layout_outcome([slot_page], reading, hole_kinds={5: "terminal"},
                                      reference_holes_mm=[MOUNT])
    assert outcome.layout is None and outcome.refused == "hole_kind", outcome


def test_anchored_kinds_bound_the_free_hole_when_nothing_is_declared(slot_page):
    # Same page, no declared kinds: the anchored pads sit on rings, the only
    # free hole (mounting excluded as a reference) is an obround: refused.
    reading = {**RECT4, 5: (5.0, 16.0)}
    outcome = measured_layout_outcome([slot_page], reading, reference_holes_mm=[MOUNT])
    assert outcome.layout is None and outcome.refused == "hole_kind", outcome


def test_reference_hole_alone_leaves_a_ring_of_the_pad_kind(tmp_path):
    # Positive of the reference exclusion: two free rings near the misread
    # pad, one of them declared as a reference, the other is taken.
    path = _draw(tmp_path / "ref.png", rings=_rings(RECT4) + [(5.0, 14.0, 1.2), (*MOUNT, 1.6)])
    reading = {**RECT4, 5: (5.0, 16.0)}
    assert measured_layout_outcome([path], reading).refused == "ambiguous"
    outcome = measured_layout_outcome([path], reading, reference_holes_mm=[MOUNT])
    assert outcome.layout is not None and outcome.layout.moved == 5, outcome
    assert math.dist(outcome.layout.positions_mm[5], (5.0, 14.0)) <= outcome.layout.tolerance_mm


def test_declared_kind_also_binds_the_check(slot_page):
    # A pad declared a slot read on a ring: the ring is not its kind of
    # hole, so the layout is not confirmed; declared a terminal it is.
    reading = {**RECT4, 5: MOUNT}
    assert check_pad_layout([slot_page], reading, hole_kinds={5: "terminal"}).status == "confirmed"
    result = check_pad_layout([slot_page], reading, hole_kinds={5: "slot"})
    assert result.status != "confirmed", result


# ---------------------------------------------------------------------------
# Closed non-circular contours make a pad unverifiable, not wrong
# ---------------------------------------------------------------------------

@pytest.fixture
def land_page(tmp_path):
    """Four rings, a far mounting ring (so that five hole-like circles exist) and a rectangle land."""
    return _draw(tmp_path / "land.png", rings=_rings(RECT4) + [(24.0, 4.0, 1.6)],
                 rects=[(5.0, 14.0, 2.2, 1.4)])


def test_pad_read_inside_a_rectangle_land_is_unverifiable(land_page):
    analysis = drawing_holes._analyse(land_page)
    assert any(r.kind == "rectangle" for r in analysis.regions), analysis.regions
    reading = {**RECT4, 5: (5.0, 14.0)}
    result = check_pad_layout([land_page], reading)
    assert result.status == "unverifiable", result


def test_pad_read_on_blank_paper_is_still_a_no_match(land_page):
    reading = {**RECT4, 5: (5.0, 21.0)}
    result = check_pad_layout([land_page], reading)
    assert (result.status, result.reason) == ("not_confirmed", "no_match"), result
    assert result.scale_observable


# ---------------------------------------------------------------------------
# Scale observability
# ---------------------------------------------------------------------------

DIAGONAL3 = {1: (0.0, 0.0), 2: (5.0, 5.0), 3: (10.0, 10.0)}


@pytest.mark.parametrize("scale_y, expected", [(1.0, "confirmed"), (0.8, "no_match_unobservable")])
def test_three_collinear_pads_cannot_show_the_page_scale(tmp_path, scale_y, expected):
    path = _draw(tmp_path / "diag.png", rings=_rings(DIAGONAL3), scale_y=scale_y)
    result = check_pad_layout([path], DIAGONAL3)
    assert isinstance(result, LayoutCheck)
    assert result.reason == expected, result
    assert result.scale_observable is False
    if expected != "confirmed":
        assert result.status == "not_confirmed"


@pytest.mark.parametrize("scale_y, expected", [(1.0, "confirmed"), (0.8, "distorted")])
def test_four_observable_pads_show_the_page_scale(tmp_path, scale_y, expected):
    path = _draw(tmp_path / "rect.png", rings=_rings(RECT4), scale_y=scale_y)
    result = check_pad_layout([path], RECT4)
    assert result.reason == expected, result
    assert result.scale_observable is True


ROW3 = {1: (0.0, 0.0), 2: (5.0, 0.0), 3: (10.0, 0.0)}


@pytest.mark.parametrize("scale_y", [1.0, 0.8])
def test_a_move_from_a_collinear_anchoring_is_refused(tmp_path, scale_y):
    # H5: three pads in a row anchor the fit; pad 4 below them is read 2 mm
    # away from its drawn hole. Whether the page is to scale or not, the
    # anchoring cannot tell, so the quote is never overwritten.
    path = _draw(tmp_path / "row.png", rings=_rings(ROW3) + [(5.0, 10.0, 1.2)], scale_y=scale_y)
    reading = {**ROW3, 4: (5.0, 12.0)}
    outcome = measured_layout_outcome([path], reading)
    assert outcome.layout is None and outcome.refused == "scale_unobservable", outcome


@pytest.mark.parametrize("scale_y, refused", [(1.0, None), (0.8, "distorted")])
def test_a_move_from_an_observable_anchoring_needs_the_page_to_scale(tmp_path, scale_y, refused):
    path = _draw(tmp_path / "obs.png", rings=_rings(RECT4) + [(5.0, 14.0, 1.2)], scale_y=scale_y)
    reading = {**RECT4, 5: (5.0, 16.0)}
    outcome = measured_layout_outcome([path], reading)
    assert outcome.refused == refused, outcome
    if refused is None:
        assert outcome.layout.moved == 5 and outcome.layout.scale_observable
        assert math.dist(outcome.layout.positions_mm[5], (5.0, 14.0)) <= outcome.layout.tolerance_mm


# ---------------------------------------------------------------------------
# No round holes versus too few circles
# ---------------------------------------------------------------------------

def test_page_of_rectangle_lands_has_no_round_holes(tmp_path):
    # No caption: a glyph counter the hole-evidence heuristic lets through
    # (a documented miss) would be «one round hole», which is not the
    # subject here. The lands are closed contours, not circles.
    path = _draw(tmp_path / "smd.png", rects=[(x, y, 2.0, 1.2) for x, y in RECT4.values()], text=False)
    result = check_pad_layout([path], RECT4)
    assert (result.status, result.reason) == ("unverifiable", "no_round_holes"), result


def test_page_with_fewer_holes_than_pads_has_too_few_circles(tmp_path):
    path = _draw(tmp_path / "two.png", rings=_rings({1: RECT4[1], 2: RECT4[2]}), text=False)
    result = check_pad_layout([path], RECT4)
    assert (result.status, result.reason) == ("unverifiable", "too_few_circles"), result


# ---------------------------------------------------------------------------
# One mirror helper
# ---------------------------------------------------------------------------

def test_mirror_variants_are_the_x_flip_then_the_y_flip():
    pads = {"A1": (1.0, 2.0), "A2": (-3.0, 0.5)}
    assert mirror_variants(pads) == [{"A1": (-1.0, 2.0), "A2": (3.0, 0.5)},
                                     {"A1": (1.0, -2.0), "A2": (-3.0, -0.5)}]
    assert drawing_holes.MIRROR_FLIPS == ((-1.0, 1.0), (1.0, -1.0))
