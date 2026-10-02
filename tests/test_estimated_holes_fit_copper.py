"""@brief An ESTIMATED hole fits inside the copper around it (user, 2026-10-02).

The AI estimated a 2.0 mm half-hole for contacts whose pad, measured on the
photo, was 1.53 mm wide: the 3D drew holes wider than their lands, touching
the next component. The rule is general, not for one board: an estimated hole
of a side fits its MEASURED (or declared) pad with MIN_HOLE_WALL_MM of copper
on each part, an estimated drill leaves room for a ring. Declared holes are
never reduced, and nothing changes where the pad is only the reference guess.
"""
import pytest

from kicadgen.generators.castellated_module import (
    AssumedSize, CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CastellatedModuleParams,
    CastellatedSide, MIN_HOLE_WALL_MM, SIZE_ESTIMATE_PAD, SIZE_ESTIMATE_RING)


def _side(edge, count, pitch, *, half=None, drill=None, pad_width=None, declared=False,
          style=CONTACT_HALF_HOLE, first_pin=1):
    size = (lambda v: None if v is None else float(v)) if declared else (
        lambda v: None if v is None else AssumedSize(v))
    return CastellatedSide(edge=edge, count=count, pitch=pitch, first_pin=first_pin,
                           contact_style=style, hole_diameter=size(half),
                           thru_hole_drill=size(drill), pad_width=pad_width)


def _params(*sides, length=30.0, width=20.0):
    return CastellatedModuleParams(name="T", body_length=length, body_width=width,
                                   sides=list(sides))


@pytest.mark.parametrize("pitch, pad, estimate", [
    (2.54, 1.53, 2.0),     # the RS485 case
    (1.27, 0.9, 1.1),      # a fine-pitch module
    (5.08, 2.0, 2.2),      # a wide-pitch module
])
def test_an_estimated_half_hole_fits_its_measured_pad(pitch, pad, estimate):
    params = _params(_side("left", 4, pitch, half=estimate, pad_width=pad))
    geometry = params.side_geometry(params.sides[0])
    assert geometry.half_hole_source == SIZE_ESTIMATE_PAD
    assert geometry.half_hole <= pad - 2 * MIN_HOLE_WALL_MM + 1e-9
    params.validate()
    assert any("piazzola larga" in note for note in params.geometry_notes())


def test_an_estimate_that_already_fits_is_kept():
    """@brief Negative: nothing to reduce, the estimate stays as it is."""
    params = _params(_side("left", 4, 2.54, half=1.0, pad_width=1.6))
    geometry = params.side_geometry(params.sides[0])
    assert geometry.half_hole == pytest.approx(1.0)
    assert geometry.half_hole_source != SIZE_ESTIMATE_PAD


def test_a_declared_hole_is_never_reduced():
    """@brief Negative: a hole read from the documents keeps its value."""
    params = _params(_side("left", 4, 2.54, half=2.0, pad_width=1.53, declared=True))
    assert params.side_geometry(params.sides[0]).half_hole == pytest.approx(2.0)


def test_a_reference_pad_does_not_shrink_an_estimate():
    """@brief Negative: with no measured pad there is no copper to fit in, only a guess."""
    params = _params(_side("left", 4, 2.54, half=2.0))
    geometry = params.side_geometry(params.sides[0])
    assert geometry.half_hole_source != SIZE_ESTIMATE_PAD


def test_an_estimated_drill_leaves_room_for_its_ring_and_the_part_generates():
    """@brief Case of 15:43: estimated drill 2.0 at pitch 2.54 used to stop the generation."""
    params = _params(_side("right", 4, 2.54, half=2.0, drill=2.0, pad_width=1.53,
                           style=CONTACT_HALF_AND_THROUGH))
    geometry = params.side_geometry(params.sides[0])
    assert geometry.drill_source == SIZE_ESTIMATE_RING
    assert geometry.ring <= 1.53 + 1e-9
    assert geometry.ring >= geometry.drill + 2 * MIN_HOLE_WALL_MM - 1e-9
    params.validate()


def test_a_declared_drill_that_does_not_fit_is_still_refused():
    """@brief Negative: a measure is checked, never reduced."""
    params = _params(_side("right", 4, 2.54, half=1.0, drill=2.0, pad_width=1.53,
                           style=CONTACT_HALF_AND_THROUGH, declared=True))
    with pytest.raises(ValueError, match="piu' larga del foro"):
        params.validate()


def test_the_default_reference_pair_is_untouched():
    """@brief Negative: no estimate on the side, the historical land pattern stays."""
    plain = _params(_side("left", 4, 2.54))
    geometry = plain.side_geometry(plain.sides[0])
    assert geometry.half_hole == pytest.approx(1.7)
