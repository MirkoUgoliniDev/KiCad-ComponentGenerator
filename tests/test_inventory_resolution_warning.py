"""@brief The resolution warning states the part size it assumes, and that size is a parameter.

HANDOFF point 85 (minor): the warning assumed the smallest part is an 0402
"on a development board", as on the sample. The size is now a declared
assumption (photo_refine.SMALLEST_EXPECTED_PART_MM), can be passed by whoever
knows the board, and the warning says which size it was judged against.
"""

import pytest

from kicadgen.services import photo_inventory as inv
from kicadgen.services.photo_refine import MIN_PACKAGE_PIXELS_HINT, SMALLEST_EXPECTED_PART_MM


def _warnings(crop_px, length, **kwargs):
    messages = []
    inv._warn_if_too_coarse(crop_px, length, messages.append, **kwargs)
    return messages


def test_the_warning_names_the_assumed_size():
    """@brief The default size is reported, so the reader knows what the verdict means."""
    messages = _warnings(400, 40.0)                          # 10 px/mm
    assert messages and f"{SMALLEST_EXPECTED_PART_MM:.2f} mm" in messages[0]


def test_borderline_resolution_uses_precise_pixels_and_conditional_wording():
    [message] = _warnings(987, 42.8)
    assert '11.53 px' in message
    assert '1028 px' in message
    assert 'potrebbero' in message
    assert 'non verranno rilevati' not in message
    assert '12 px, meno dei 12' not in message


def test_the_verdict_follows_the_declared_smallest_part():
    """@brief Same photo, two boards: fine enough for 0805 bodies, too coarse for 0201.

    20 px/mm: a 1.25 mm short side is 25 px (readable), a 0.3 mm one is 6 px
    (not). The difference is the declared size, not the photo.
    """
    crop_px, length = 800, 40.0
    assert _warnings(crop_px, length, smallest_part_mm=1.25) == []
    coarse = _warnings(crop_px, length, smallest_part_mm=0.3)
    assert coarse and "0.30 mm" in coarse[0]
    # The threshold is the pixel count, the same one the GUI uses.
    assert 20.0 * 0.3 < MIN_PACKAGE_PIXELS_HINT <= 20.0 * 1.25


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_an_unusable_declared_size_says_nothing_instead_of_raising(bad):
    assert _warnings(400, 40.0, smallest_part_mm=bad) == []
