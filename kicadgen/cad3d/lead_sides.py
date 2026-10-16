"""@brief Observed lead counts per side, moved from the photograph into the builder's local frame.

Pure geometry, with no AI service and no FreeCAD: the CAD package imports it
to build the FreeCAD payload offline (tests/test_regeneration_is_offline.py).
"""
from __future__ import annotations

import math
import numbers


def _quarter_turns(box, angle):
    """The rotation of `photo_terminals._local_box`, repeated here for the CAD side.

    It must stay identical to that function: tests/test_observed_lead_shape.py
    checks both on every side and quarter turn.
    """
    left, top, right, bottom = box
    corners = [(left, top), (left, bottom), (right, top), (right, bottom)]
    for _ in range(int(angle % 360) // 90):
        corners = [(y, 1 - x) for x, y in corners]
    return (min(x for x, _ in corners), min(y for _, y in corners),
            max(x for x, _ in corners), max(y for _, y in corners))


#: Probe box on each image side of a component box, used to carry a side name
#: through the quarter-turn rotation: the side conversion is the same
#: transformation that moves observed terminal boxes into the package-local frame
#: (photo_terminals._local_box).
_SIDE_PROBES = {'left': (0.0, 0.4, 0.1, 0.6), 'right': (0.9, 0.4, 1.0, 0.6),
                'top': (0.4, 0.0, 0.6, 0.1), 'bottom': (0.4, 0.9, 0.6, 1.0)}


def _real(value) -> bool:
    """True for any real number (Python, numpy...), False for bool: True is not an angle."""
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _integral(value) -> bool:
    """True for any integral number (int, numpy.int64...), False for bool and floats."""
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def local_lead_sides(sides, rotation):
    """@brief Observed lead counts for each image side of a component, moved to its local frame.

    @param sides {side: count} over left/right/top/bottom of the component box
        as seen in the photograph (X right, Y down), e.g. pins_by_side.
    @param rotation Component rotation in degrees, photo frame, as stored on
        the component (the angle photo_terminals._local_box undoes).
    @return {side: count} in the package-local frame the CAD builders use
        before the caller rotates the shapes, or None when the input is not a
        clean observation or the angle is exactly between two quarter turns.
    @note The builders draw every body with its long axis along local X and
        the caller rotates it afterwards. A body photographed vertical
        (rotation 90) has its leads on the image left/right, which are its
        local bottom/top: without this conversion the leads of every vertical
        SOT or SOIC were drawn on the wrong sides.
    @note A non-cardinal angle (an edge-measured body) is taken at the nearest
        quarter turn: the model counted leads on each side of the component box,
        and each box side stays nearest to one body side up to 45 degrees.
        Exactly 45 degrees has no nearest side and returns None.
    @note Numbers are recognised by their abstract type (numbers.Real,
        numbers.Integral), so numpy scalars coming from the photo pipeline are
        accepted; booleans are refused, and a float count (2.0) is not a
        count of leads.
    """
    if (not isinstance(sides, dict) or not sides or not _real(rotation)
            or not math.isfinite(rotation)):
        return None
    quarter = rotation / 90.0
    if abs(abs(quarter - math.floor(quarter)) - 0.5) < 1e-9:
        return None
    angle = (int(round(quarter)) % 4) * 90
    result = {}
    for side, count in sides.items():
        if side not in _SIDE_PROBES or not _integral(count) or count < 0:
            return None
        left, top, right, bottom = _quarter_turns(_SIDE_PROBES[side], angle)
        x, y = (left + right) / 2, (top + bottom) / 2
        local = ('left' if x < 0.25 else 'right' if x > 0.75 else
                 'top' if y < 0.25 else 'bottom')
        if count:
            result[local] = result.get(local, 0) + int(count)
    return result or None
