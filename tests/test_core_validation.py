"""@brief Contract of kicadgen.core.validation and equivalence with the helpers it replaced.

Each migrated private helper is copied here verbatim as it was before the
migration, and compared with its replacement on the same adversarial inputs.
The single deliberate difference is documented and asserted explicitly: the
old copies that called ``float()`` or ``math.isfinite()`` without guarding
``OverflowError`` crashed on an integer too large for a float (``10**400``);
the shared functions reject it like any other non-finite value.
"""
import math

import numpy as np
import pytest

from kicadgen.core.validation import (
    covered_fraction, finite_number, iou, is_finite_number, positive_number)


HUGE = 10 ** 400

#: Inputs every numeric helper must classify: bools, None, strings, NaN,
#: infinities, numpy scalars, zero/negative boundaries and a huge integer.
INPUTS = [
    True, False, None, "1.5", "nan", b"1", [1.0], {}, object(),
    math.nan, math.inf, -math.inf, np.float64("nan"), np.float64("inf"),
    np.float32(1.5), np.int64(3), np.float64(2.5), np.bool_(True),
    0, 0.0, -0.0, -1, -1e-300, 1e-300, 5e-324, 1, 7, 2.5, 1e308, -1e308,
    HUGE, -HUGE,
]


# ---------------------------------------------------------------- contract

@pytest.mark.parametrize("value, expected", [
    (1, 1.0), (2.5, 2.5), (-3, -3.0), (0, 0.0), (np.float64(2.5), 2.5),
    (1e308, 1e308),
])
def test_finite_number_accepts_finite_reals(value, expected):
    """@brief Finite non-boolean int/float values come back as float."""
    result = finite_number(value)
    assert type(result) is float and result == expected
    assert is_finite_number(value)


@pytest.mark.parametrize("value", [
    True, False, None, "1.5", "3", math.nan, math.inf, -math.inf,
    np.float32(1.5), np.int64(3), np.bool_(True), HUGE, -HUGE, [1.0],
])
def test_finite_number_rejects_everything_else(value):
    """@brief Bools, text, non-finite, huge ints and non-float numpy scalars are rejected."""
    assert finite_number(value) is None
    assert not is_finite_number(value)


def test_positive_number_boundaries():
    """@brief Zero and negatives are rejected; the smallest positive float is kept."""
    assert positive_number(0) is None and positive_number(-0.0) is None
    assert positive_number(-1e-300) is None
    assert positive_number(5e-324) == 5e-324
    assert positive_number(7) == 7.0
    assert positive_number(True) is None and positive_number(HUGE) is None
    assert positive_number(math.inf) is None and positive_number(math.nan) is None


def test_covered_fraction_and_iou():
    """@brief Overlap measures on nested, disjoint, identical and degenerate boxes."""
    outer, inner = (0, 0, 4, 4), (1, 1, 3, 3)
    assert covered_fraction(inner, outer) == 1.0
    assert covered_fraction(outer, inner) == 0.25
    assert covered_fraction((0, 0, 1, 1), (2, 2, 3, 3)) == 0.0
    assert covered_fraction((1, 1, 1, 3), outer) == 0.0  # no area
    assert iou(outer, outer) == 1.0
    assert iou(outer, inner) == 0.25
    assert iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0.0
    assert iou((1, 1, 1, 1), (1, 1, 1, 1)) == 0.0  # empty union
    assert iou((0, 0, 2, 1), (1, 0, 3, 1)) == pytest.approx(1 / 3)


# ------------------------------------------------- old copies, verbatim

def old_project_costs_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def old_accepted_models_finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def old_accepted_models_finite_positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def old_contact_zones_number(value, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > minimum else None


def old_step_generator_is_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def old_body_params_positive(value):
    return old_step_generator_is_finite_number(value) and value > 0


def old_annotations_positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def old_measurement_uncertainty_positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def old_photo_inventory_positive_extent(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def old_covered(inner, outer):
    wide = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    high = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return (wide * high / area) if area > 0 else 0.0


def old_iou(first, second):
    wide = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    high = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    inter = wide * high
    union = ((first[2]-first[0])*(first[3]-first[1])
             + (second[2]-second[0])*(second[3]-second[1]) - inter)
    return inter / union if union > 0 else 0.0


# --------------------------------------------------------- equivalence

def _same(old, new, value, *, overflow_was_crash):
    """Compare one old/new pair; an old OverflowError must map to a rejection."""
    try:
        expected = old(value)
    except OverflowError:
        assert overflow_was_crash, f"unexpected crash of the old helper on {value!r}"
        assert new(value) in (None, False)
        return
    result = new(value)
    if isinstance(expected, float) and math.isnan(expected):
        pytest.fail("no helper may return NaN")
    if isinstance(expected, (bool, np.bool_)):
        # Old predicates returned numpy.bool_ for numpy.float64 inputs
        # (``value > 0``); every caller only tests truthiness.
        assert type(result) is bool and result == bool(expected), (value, expected, result)
        return
    assert result == expected and type(result) is type(expected), (value, expected, result)


EQUIVALENCES = [
    # (old, new, old crashed on huge ints)
    (old_project_costs_number, finite_number, True),
    (old_accepted_models_finite, finite_number, True),
    (old_accepted_models_finite_positive, positive_number, True),
    (old_contact_zones_number, positive_number, True),
    (lambda v: old_contact_zones_number(v, minimum=-math.inf), finite_number, True),
    (old_step_generator_is_finite_number, is_finite_number, False),
    (old_body_params_positive, lambda v: positive_number(v) is not None, False),
    (old_annotations_positive, lambda v: positive_number(v) is not None, False),
    (old_measurement_uncertainty_positive, positive_number, False),
    (old_photo_inventory_positive_extent, lambda v: positive_number(v) is not None, True),
]


@pytest.mark.parametrize("old, new, crashed", EQUIVALENCES)
def test_migrated_number_helpers_keep_their_semantics(old, new, crashed):
    """@brief Each replacement answers exactly like the helper it replaced."""
    for value in INPUTS:
        _same(old, new, value, overflow_was_crash=crashed)


def test_the_huge_integer_difference_is_real():
    """@brief Negative control: the old unguarded copies really raised on 10**400."""
    with pytest.raises(OverflowError):
        old_project_costs_number(HUGE)
    with pytest.raises(OverflowError):
        old_photo_inventory_positive_extent(HUGE)
    assert finite_number(HUGE) is None


def test_migrated_callers_use_the_shared_helpers():
    """@brief The modules now call the shared functions instead of private copies."""
    from kicadgen.cad3d import annotations, body_params
    from kicadgen.services import photo_yolo
    assert photo_yolo._cover is covered_fraction
    assert body_params._positive(2.0) and not body_params._positive(HUGE)
    assert annotations._positive(1) and not annotations._positive(True)


BOXES = [
    (0, 0, 1, 1), (0.2, 0.2, 0.6, 0.9), (0.5, 0.5, 0.5, 0.8), (1, 1, 0, 0),
    (-1, -1, 2, 2), (0.1, 0.3, 0.4, 0.35), (0, 0, 1e-12, 1e-12), (3, 3, 4, 4),
]


def test_box_overlap_helpers_are_bit_identical():
    """@brief covered_fraction and iou equal the removed copies on every pair."""
    for first in BOXES:
        for second in BOXES:
            assert covered_fraction(first, second) == old_covered(first, second)
            assert iou(first, second) == old_iou(first, second)
