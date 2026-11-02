"""@brief Unit tests of the shared drawing origin (`kicadgen/gui/canvas_origin.py`)."""
import math

import pytest

from kicadgen.gui.canvas_origin import from_drawing_mm, nearest_point, readout_text, to_drawing_mm


def test_drawing_coordinates_read_y_upwards_and_invert_exactly():
    """@brief Positive: a point above and right of the origin reads (+x, +y); negative: below reads -y."""
    origin = (100.0, 200.0)
    assert to_drawing_mm((140.0, 160.0), origin, 20.0) == (2.0, 2.0)
    assert to_drawing_mm((140.0, 240.0), origin, 20.0) == (2.0, -2.0)
    for point in ((0.0, 0.0), (-37.5, 912.25), (100.0, 200.0)):
        back = from_drawing_mm(to_drawing_mm(point, origin, 20.0), origin, 20.0)
        assert back == pytest.approx(point)


@pytest.mark.parametrize("scale", [0.0, -1.0, math.inf, math.nan])
def test_a_scale_that_is_not_positive_and_finite_is_refused(scale):
    """@brief Negative: no silent division by zero or sign flip."""
    with pytest.raises(ValueError):
        to_drawing_mm((1.0, 1.0), (0.0, 0.0), scale)
    with pytest.raises(ValueError):
        from_drawing_mm((1.0, 1.0), (0.0, 0.0), scale)


def test_nearest_point_takes_the_closest_and_keeps_the_first_on_a_tie():
    """@brief Positive: the closest key wins; a tie keeps the mapping's first key; empty gives None."""
    candidates = {"origin": (0.0, 0.0), "corner:3": (-5.0, 4.0), "pad:1": (5.0, -4.0)}
    assert nearest_point(candidates, (-4.0, 3.5)) == "corner:3"
    assert nearest_point(candidates, (0.0, 0.0)) == "origin"
    assert nearest_point({"a": (1.0, 0.0), "b": (-1.0, 0.0)}, (0.0, 0.0)) == "a"
    assert nearest_point({}, (0.0, 0.0)) is None


def test_the_pointer_line_is_the_symbol_editors_format():
    """@brief Positive: «X … Y … mm  |  zoom%» as the symbol editor wrote it; negative: no point, only the zoom."""
    assert readout_text((1.0, -2.5), 1.5) == "X 1.00  Y -2.50 mm  |  150%"
    assert readout_text(None, 0.25) == "25%"
