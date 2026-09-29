"""@brief HANDOFF 159: an assumed annular ring grows around the hole instead of refusing it.

Real run (generated/TEST, 2026-09-29 21:27): «La piazzola del foro passante
(1.7mm) deve essere piu' larga del foro (1.7mm)» stopped the footprint and
the 3D. The ring was the historical reference 1.7 (nothing declared), the
hole the AI's estimate 1.7: two non-measures compared as if measured. The
rule is symmetric to the existing one (an assumed ring bounds an assumed
drill): an assumed ring now follows a wider hole with the minimum ring, as
long as the pitch leaves room; a DECLARED ring smaller than the hole is still
refused, and so is a ring that cannot fit the pitch.
"""

import pytest

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_THROUGH_HOLE, MIN_HOLE_WALL_MM, REFERENCE_THRU_RING_MM,
    SIZE_ESTIMATE_DRILL, AssumedSize, build_castellated_module,
)
from test_hole_sizes_per_side import fine_pitch, two_rows


@pytest.mark.parametrize("style", [CONTACT_HALF_AND_THROUGH, CONTACT_THROUGH_HOLE])
@pytest.mark.parametrize("drill", [AssumedSize(1.7), 1.7])
def test_the_reference_ring_grows_around_an_estimated_or_declared_hole(style, drill):
    """@brief Positive: the log's case (ring 1.7 assumed, hole 1.7) now builds."""
    params = two_rows(style, thru_hole_drill=drill)
    params.validate()
    for side in params.sides:
        geometry = params.side_geometry(side)
        assert geometry.drill == pytest.approx(1.7)
        assert geometry.ring == pytest.approx(1.7 + 2 * MIN_HOLE_WALL_MM)
        assert geometry.ring_source == SIZE_ESTIMATE_DRILL
    pads = [p for p in build_castellated_module(params).pads if p.drill]
    assert pads and all(p.drill == pytest.approx(1.7) for p in pads)
    assert all(max(p.size) >= 1.7 + 2 * MIN_HOLE_WALL_MM - 1e-9 for p in pads)
    notes = params.geometry_notes()
    assert any("corona del foro passante non dichiarata" in n and "non avvolge il foro di 1.70mm" in n
               and "stimata 2.10mm" in n for n in notes)


def test_a_ring_that_already_encloses_the_hole_is_left_alone():
    """@brief Negative: no growth, no note, when the reference ring fits the hole."""
    params = two_rows(CONTACT_THROUGH_HOLE, thru_hole_drill=AssumedSize(1.0))
    params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert geometry.ring == pytest.approx(float(REFERENCE_THRU_RING_MM))
    assert geometry.ring_source == "reference"
    assert not any("non avvolge" in n for n in params.geometry_notes())


def test_a_declared_ring_smaller_than_the_hole_is_still_refused():
    """@brief Negative: a measure is never grown; the contradiction is reported."""
    params = two_rows(CONTACT_HALF_AND_THROUGH, thru_hole_drill=1.7, thru_pad_diameter=1.7)
    with pytest.raises(ValueError, match="piu' larga del foro"):
        params.validate()


def test_a_ring_that_cannot_fit_the_pitch_is_refused_not_grown():
    """@brief Negative: on a 1.27 pitch a 1.2 mm hole needs a 1.6 mm ring: no room."""
    params = fine_pitch(CONTACT_THROUGH_HOLE, body_length=24, body_width=24, thru_hole_drill=1.2)
    with pytest.raises(ValueError):
        params.validate()
    geometry = params.side_geometry(params.sides[0])
    assert geometry.ring_source != SIZE_ESTIMATE_DRILL
