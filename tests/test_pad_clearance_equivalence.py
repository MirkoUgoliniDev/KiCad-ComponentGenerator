"""@brief The grid-bucketed pad clearance check equals the former pair scan (review pass 4, 2026-10-15).

`PinHeaderParams._validate_pad_clearance` judged every pair of pads, O(n²):
a 30 x 30 matrix cost ~300 ms per validation, paid again at every metadata
keystroke of the KiCad gate. The check now buckets the pads on a grid of
the largest pad plus the minimum gap and judges adjacent cells only. The
former scan is kept here, verbatim, as the oracle: on random matrices both
accept the same parameters and report the same first pair with the same
message.
"""
import random
from math import hypot

import pytest

from kicadgen.generators.pin_header import (MIN_PAD_GAP_MM, PinHeaderParams, pad_copper_gap,
                                            pin_positions)


def former_scan(params: PinHeaderParams) -> None:
    """The O(n²) check as it was before pass 4 (the oracle)."""
    pads = sorted(pin_positions(params), key=lambda item: item[1])
    for index, (pin_a, xa, ya) in enumerate(pads):
        for pin_b, xb, yb in pads[index + 1:]:
            size_a = float((params.pad_overrides or {}).get(str(pin_a), {}).get(
                "size", params.pad_diameter))
            size_b = float((params.pad_overrides or {}).get(str(pin_b), {}).get(
                "size", params.pad_diameter))
            radius_a, radius_b = size_a / 2, size_b / 2
            if xb - xa >= radius_a + radius_b + MIN_PAD_GAP_MM:
                continue
            shape_a = (params.pad_overrides or {}).get(str(pin_a), {}).get(
                "shape", "rect" if params.pad_shape == "rect" or pin_a == 1 else "circle")
            shape_b = (params.pad_overrides or {}).get(str(pin_b), {}).get(
                "shape", "rect" if params.pad_shape == "rect" or pin_b == 1 else "circle")
            gap = pad_copper_gap(xa, ya, shape_a, radius_a, xb, yb, shape_b, radius_b)
            if gap < MIN_PAD_GAP_MM - 1e-9:
                distance = hypot(xb - xa, yb - ya)
                raise ValueError(f"{pin_a}|{pin_b}|{distance!r}|{max(gap, 0.0)!r}")


def _outcome(check, params):
    try:
        check(params)
    except ValueError as exc:
        return str(exc)
    return None


def _random_params(rng: random.Random) -> PinHeaderParams:
    rows, cols = rng.randint(1, 7), rng.randint(1, 7)
    pitch = rng.choice([0.5, 0.8, 1.0, 1.27, 2.0, 2.54, rng.uniform(0.4, 3.0)])
    spacing = rng.choice([None, pitch, rng.uniform(0.4, 3.0)])
    pad = rng.uniform(0.3, 2.4)
    overrides = {}
    for pin in rng.sample(range(1, rows * cols + 1), k=min(rows * cols, rng.randint(0, 3))):
        entry = {}
        if rng.random() < 0.7:
            entry["size"] = round(rng.uniform(0.3, 2.6), 2)
        if rng.random() < 0.5:
            entry["shape"] = rng.choice(["rect", "circle"])
        if entry:
            overrides[str(pin)] = entry
    populated = None
    if rng.random() < 0.4 and rows * cols > 1:
        populated = set(rng.sample(range(1, rows * cols + 1), k=rng.randint(1, rows * cols)))
    return PinHeaderParams(name="R", rows=rows, cols=cols, pitch=pitch, row_spacing=spacing,
                           pad_diameter=pad, drill=min(pad * 0.5, 0.3),
                           pad_shape=rng.choice(["circle", "rect"]),
                           pad_overrides=overrides or None, populated_pins=populated)


@pytest.mark.parametrize("seed", range(40))
def test_random_matrices_give_the_same_verdict_and_first_pair(seed):
    rng = random.Random(seed)
    accepted = rejected = 0
    for _ in range(25):
        params = _random_params(rng)
        expected = _outcome(former_scan, params)
        actual = _outcome(lambda p: p._validate_pad_clearance(), params)
        if expected is None:
            assert actual is None, (params, actual)
            accepted += 1
        else:
            assert actual is not None, (params, expected)
            pin_a, pin_b, distance, gap = expected.split("|")
            assert f"Pad {pin_a} e {pin_b}:" in actual, (params, expected, actual)
            assert f"{float(distance):.2f} mm" in actual and f"{float(gap):.2f} mm di rame" in actual
            rejected += 1
    assert accepted and rejected, "both verdicts must be exercised"


def test_a_big_regular_matrix_validates_quickly():
    """Positive bound on the class the review measured: 900 pads, 1 mm pitch."""
    import time
    params = PinHeaderParams(name="BIG", rows=30, cols=30, pitch=1.0, row_spacing=1.0,
                             pad_diameter=0.7, drill=0.4)
    start = time.perf_counter()
    for _ in range(5):
        params._validate_pad_clearance()
    assert (time.perf_counter() - start) / 5 < 0.05
    assert _outcome(former_scan, params) is None


def test_one_huge_pad_still_stops_at_the_first_violation_quickly():
    """Adversarial (review pass 4): one override far larger than the pitch puts
    every pad in one cell. Verdict and first pair must equal the oracle's, and
    the check must stop at that pair instead of collecting n²/2 pairs first."""
    import time
    params = PinHeaderParams(name="BIG", rows=30, cols=30, pitch=1.0, row_spacing=1.0,
                             pad_diameter=0.7, drill=0.4, pad_overrides={"450": {"size": 60}})
    expected = _outcome(former_scan, params)
    assert expected is not None
    start = time.perf_counter()
    actual = _outcome(lambda p: p._validate_pad_clearance(), params)
    elapsed = time.perf_counter() - start
    pin_a, pin_b, distance, gap = expected.split("|")
    assert actual is not None and f"Pad {pin_a} e {pin_b}:" in actual
    assert f"{float(distance):.2f} mm" in actual and f"{float(gap):.2f} mm di rame" in actual
    assert elapsed < 0.05, elapsed
