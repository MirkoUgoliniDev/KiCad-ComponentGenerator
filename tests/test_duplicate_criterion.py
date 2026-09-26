"""@brief Duplicates across views are recognised physically, not by a tuned area ratio.

HANDOFF point 82/85, medium 5a: CONTAINMENT_MAX_AREA_RATIO had been raised
from 2.5 to 3.5 until one observed duplicate merged. The rule now: two
detections are the same body when their centres coincide within the
measurement uncertainty -- one pixel per box edge of the calibrated photo,
i.e. 2 px / (px/mm) in millimetres -- and their sizes agree within it, or one
contains the other within it. Two mounted bodies cannot be stacked, so a
shared centre means one body; a small part next to or inside the generous box
of a larger one does not share its centre and is never absorbed.
"""

import pytest

from kicadgen.services import photo_inventory as inv

#: Calibrated photo of a 40 x 20 mm board at 20 px/mm.
IMAGE_PX = (800, 400)
BOARD_MM = (40.0, 20.0)


def _box_mm(cx, cy, length, width, source="A", kind="generic"):
    """Item with a normalized box built from a centre and a size in mm."""
    L, W = BOARD_MM
    return dict(_box=((cx - length / 2) / L + .5, (cy - width / 2) / W + .5,
                      (cx + length / 2) / L + .5, (cy + width / 2) / W + .5),
                _source=source, kind=kind, name=kind, confidence="high")


def _px(pixels, axis=0):
    """Normalized length of `pixels` of the photo along an axis."""
    return pixels / IMAGE_PX[axis]


def test_the_tolerance_comes_from_the_pixels_of_the_photo():
    """@brief 2 px per axis (one per box edge, two boxes), in normalized units."""
    tx, ty = inv.detection_tolerance(IMAGE_PX)
    assert tx == pytest.approx(2 / 800) and ty == pytest.approx(2 / 400)
    # In millimetres: 2 px at 20 px/mm = 0.1 mm on both axes.
    assert tx * BOARD_MM[0] == pytest.approx(0.1) and ty * BOARD_MM[1] == pytest.approx(0.1)
    for unknown in (None, (0, 400), (float("nan"), 3), "800x400"):
        assert max(inv.detection_tolerance(unknown)) < 1e-6


def test_the_same_part_seen_twice_with_slightly_different_boxes_merges():
    """@brief Positive: every edge of the two boxes within one pixel of each other."""
    first = _box_mm(3.0, 2.0, 3.2, 1.6)
    x0, y0, x1, y1 = first["_box"]
    second = dict(first, _source="B",
                  _box=(x0 + _px(.8), y0 - _px(.6, 1), x1 - _px(.4), y1 + _px(.9, 1)))
    assert len(inv.merge_components([[first], [second]], containment=True,
                                    image_px=IMAGE_PX)) == 1


def test_a_tight_and_a_loose_box_of_the_same_part_merge():
    """@brief Positive: body box and body-plus-terminals box, concentric within a pixel."""
    loose = _box_mm(-5.0, 1.0, 5.6, 3.6)
    tight = _box_mm(-5.0 + 0.02, 1.0 - 0.03, 3.4, 2.0, source="B")
    assert inv.same_body(loose, tight, inv.detection_tolerance(IMAGE_PX))
    assert len(inv.merge_components([[loose], [tight]], containment=True,
                                    image_px=IMAGE_PX)) == 1


@pytest.mark.parametrize("offset_mm", [(2.0, 1.5), (-2.4, 0.0), (0.0, 2.2)])
def test_a_1206_inside_the_generous_box_of_a_larger_part_is_kept(offset_mm):
    """@brief Negative: a 1206 inside a 7 x 7 mm box but off its centre is a second body.

    Area ratio ~9.6 and full containment, as for a real duplicate: what tells
    them apart is the centre, which a separate body does not share.
    """
    large = _box_mm(0.0, 0.0, 7.0, 7.0, kind="ic")
    passive = _box_mm(offset_mm[0], offset_mm[1], 3.2, 1.6, source="B", kind="resistor")
    assert not inv.same_body(large, passive, inv.detection_tolerance(IMAGE_PX))
    assert len(inv.merge_components([[large], [passive]], containment=True,
                                    image_px=IMAGE_PX)) == 2


def test_a_1206_next_to_a_larger_part_is_kept():
    """@brief Negative: adjacent bodies whose boxes touch are never one body."""
    large = _box_mm(0.0, 0.0, 7.0, 7.0, kind="ic")
    passive = _box_mm(3.5 + 1.6, 0.0, 3.2, 1.6, source="B", kind="resistor")
    assert len(inv.merge_components([[large], [passive]], containment=True,
                                    image_px=IMAGE_PX)) == 2


def test_the_decision_depends_on_the_resolution_not_on_a_ratio():
    """@brief Same pair of boxes, two photos: within one pixel on a coarse one, not on a fine one.

    A loose and a tight box (IoU 0.34, so the IoU association does not apply)
    whose centres differ by 0.15 mm: 3 px at 20 px/mm (two boxes the measure
    can tell apart), 0.75 px at 5 px/mm (indistinguishable).
    """
    first = _box_mm(0.0, 0.0, 5.6, 3.6)
    second = _box_mm(0.15, 0.0, 3.4, 2.0, source="B")
    assert inv.overlap(first, second)[0] < .6
    fine, coarse = IMAGE_PX, (200, 100)
    assert len(inv.merge_components([[first], [second]], containment=True, image_px=fine)) == 2
    assert len(inv.merge_components([[first], [second]], containment=True, image_px=coarse)) == 1


def test_without_the_photo_size_only_coinciding_boxes_merge():
    """@brief Unknown pixels = no measured uncertainty: only a shared centre to float precision."""
    outer = dict(_box=(.2, .2, .6, .6), _source="full", kind="generic")
    inner = dict(_box=(.25, .25, .55, .55), _source="tile", kind="generic")
    shifted = dict(_box=(.26, .25, .56, .55), _source="tile", kind="generic")
    assert len(inv.merge_components([[outer], [inner]], containment=True)) == 1
    assert len(inv.merge_components([[outer], [shifted]], containment=True)) == 2


def test_one_to_one_holds_with_the_physical_criterion():
    """@brief Two copies in the same view are two bodies: only one can match."""
    outer = _box_mm(0.0, 0.0, 5.0, 3.0)
    inner = _box_mm(0.0, 0.0, 3.0, 2.0, source="B")
    merged = inv.merge_components([[outer], [inner, dict(inner)]], containment=True,
                                  image_px=IMAGE_PX)
    assert len(merged) == 2


def test_containment_is_only_used_when_requested():
    """@brief Without containment only the IoU association applies, as before."""
    loose = _box_mm(-5.0, 1.0, 5.6, 3.6)
    tight = _box_mm(-5.0, 1.0, 3.4, 2.0, source="B")
    assert len(inv.merge_components([[loose], [tight]], image_px=IMAGE_PX)) == 2
