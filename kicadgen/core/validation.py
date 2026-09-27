"""@brief Shared validation of untrusted numbers and normalized rectangles.

Numbers reaching the generator come from AI responses, JSON project files and
user state; before this module every package carried its own copy of "is this
a usable number" and "how much do two boxes overlap", and the copies drifted
apart (one crashed on integers too large for a float, another accepted them).

@note Accepted numeric types, for every function here: instances of Python
    ``int`` or ``float`` (subclasses included, so ``numpy.float64`` is
    accepted as a ``float`` subclass), never ``bool``. Strings, ``None``,
    ``numpy.float32`` and ``numpy.int64`` are rejected. This reproduces the
    callers migrated here exactly; helpers that deliberately parse text
    fields (for example numeric strings typed in a dialog) keep their own
    wrapper and must not be folded in.
@note An integer too large for a float (``10**400``) is rejected, never
    raised as ``OverflowError``.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def finite_number(value) -> Optional[float]:
    """@brief Convert a finite real number to float, rejecting anything else.

    @param value Candidate value of any type.
    @return ``float(value)`` when @p value is a non-boolean ``int``/``float``
        that is finite and representable as a float; otherwise None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def is_finite_number(value) -> bool:
    """@brief Tell whether a value is a finite real number (bool excluded).

    @param value Candidate value of any type.
    @return True exactly when finite_number(@p value) is not None.
    """
    return finite_number(value) is not None


def positive_number(value) -> Optional[float]:
    """@brief Convert a finite, strictly positive real number to float.

    @param value Candidate value of any type (typically a length in mm).
    @return ``float(value)`` when finite_number accepts it and it is > 0;
        otherwise None. Zero is rejected.
    """
    number = finite_number(value)
    return number if number is not None and number > 0 else None


def covered_fraction(inner: Sequence[float], outer: Sequence[float]) -> float:
    """@brief Fraction of the first rectangle's area that lies inside the second.

    @param inner Rectangle ``(x0, y0, x1, y1)`` whose coverage is measured.
    @param outer Rectangle ``(x0, y0, x1, y1)`` covering it; same units.
    @return Intersection area divided by the area of @p inner, in [0, 1] for
        well-formed rectangles; 0.0 when @p inner has no positive area.
    @note The rectangles are not validated: callers pass boxes already checked.
    """
    wide = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    high = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return (wide * high / area) if area > 0 else 0.0


def iou(first: Sequence[float], second: Sequence[float]) -> float:
    """@brief Intersection over union of two ``(x0, y0, x1, y1)`` rectangles.

    @param first First rectangle.
    @param second Second rectangle, in the same units as @p first.
    @return Intersection area over union area; 0.0 when the union is empty.
    @note The rectangles are not validated: callers pass boxes already checked.
    """
    wide = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    high = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    inter = wide * high
    union = ((first[2] - first[0]) * (first[3] - first[1])
             + (second[2] - second[0]) * (second[3] - second[1]) - inter)
    return inter / union if union > 0 else 0.0
