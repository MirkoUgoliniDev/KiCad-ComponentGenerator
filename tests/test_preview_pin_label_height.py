"""@brief Pin labels of the STEP preview sized by the room between anchors (user, 2026-11-04).

On a relay with five pins 6 mm apart the fixed 0.65 mm labels were
unreadable; on a 2.54 mm header they must stay that small or touch. The
height follows the nearest anchor distance, between a floor and a cap.
"""
import numpy as np
import pytest

from kicadgen.gui.preview_3d_vtk import (
    _LABEL_HEIGHT_MAX_MM, _LABEL_HEIGHT_MIN_MM, _LABEL_HEIGHT_OF_GAP, _pin_label_height)


def test_a_sparse_part_gets_taller_labels():
    """Positive: relay-like anchors (nearest pair 6.3 mm) -> above the floor."""
    relay = np.array([[0, 0, 0], [12.2, 0, 0], [12.2, 12, 0], [-2, 6, 0], [0, 12, 0]], float)
    height = _pin_label_height(relay)
    assert _LABEL_HEIGHT_MIN_MM < height < _LABEL_HEIGHT_MAX_MM
    assert height == pytest.approx(_LABEL_HEIGHT_OF_GAP * np.hypot(2, 6))


def test_a_header_keeps_the_former_height():
    """Negative: 2.54 mm pins -> the floor, as before."""
    header = np.array([[i * 2.54, 0, 0] for i in range(6)], float)
    assert _pin_label_height(header) == pytest.approx(_LABEL_HEIGHT_MIN_MM)


def test_a_fine_grid_never_goes_below_the_floor_and_a_huge_part_is_capped():
    grid = np.array([[i * 0.5, j * 0.5, 0] for i in range(3) for j in range(3)], float)
    assert _pin_label_height(grid) == _LABEL_HEIGHT_MIN_MM
    wide = np.array([[0, 0, 0], [40, 0, 0]], float)
    assert _pin_label_height(wide) == _LABEL_HEIGHT_MAX_MM


def test_a_single_or_coincident_anchor_uses_the_floor():
    assert _pin_label_height(np.array([[1, 1, 0]], float)) == _LABEL_HEIGHT_MIN_MM
    assert _pin_label_height(np.array([[1, 1, 0], [1, 1, 0]], float)) == _LABEL_HEIGHT_MIN_MM
