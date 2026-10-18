"""@brief Quarter turns and mirrors of drawing points (`kicadgen.core.drawing_transform`)."""
import pytest

from kicadgen.core.drawing_transform import bounds_centre, mirror_points_x, rotate_points_90


def test_bounds_centre_is_the_middle_of_the_box():
    assert bounds_centre([(0, 0), (4, 2), (1, 6)]) == (2.0, 3.0)


def test_bounds_centre_refuses_no_points():
    with pytest.raises(ValueError):
        bounds_centre([])


def test_counter_clockwise_on_a_y_down_screen():
    """@brief A point to the RIGHT of the centre goes UP on screen (smaller Y) when turned counter-clockwise.

    Negative twin: clockwise sends it DOWN (larger Y).
    """
    assert rotate_points_90([(1, 0)], (0, 0), True) == [[0.0, -1.0]]
    assert rotate_points_90([(1, 0)], (0, 0), False) == [[0.0, 1.0]]


def test_four_turns_give_the_points_back_and_opposite_turns_cancel():
    points = [(1.27, -3.5), (7.0, 2.25), (-4.1, 0.3)]
    centre = bounds_centre(points)
    turned = points
    for _ in range(4):
        turned = rotate_points_90(turned, centre, True)
    assert turned == [[round(x, 4), round(y, 4)] for x, y in points]
    back = rotate_points_90(rotate_points_90(points, centre, True), centre, False)
    assert back == [[round(x, 4), round(y, 4)] for x, y in points]


def test_a_turn_about_the_box_centre_keeps_the_centre():
    points = [(0, 0), (6, 2)]
    centre = bounds_centre(points)
    assert bounds_centre(rotate_points_90(points, centre, True)) == centre


def test_mirror_flips_x_about_the_axis_and_keeps_y():
    assert mirror_points_x([(1, 5), (3, -2)], 2.0) == [[3.0, 5.0], [1.0, -2.0]]
    assert mirror_points_x(mirror_points_x([(1.5, 2)], 0.7), 0.7) == [[1.5, 2.0]]


def test_no_negative_zero():
    assert str(mirror_points_x([(0, 0)], 0)[0][0]) == "0.0"
