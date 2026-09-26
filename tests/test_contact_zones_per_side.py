"""@brief The photo mask of the contacts uses each side's own hole sizes, like the footprint.

Before 26/09 contact_zones read module-wide hole values and placed the ring of
a half_and_through contact at the module offset, ignoring the declared row
spacing: on a module whose sides have different holes, or whose rows fix the
through-holes, the strip masked on the photo and the pads the footprint draws
disagreed. Now the strip is built from CastellatedModuleParams.side_geometry,
the same rule the footprint and the 3D use.
"""

import pytest

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CastellatedSide,
    pad_positions,
)
from kicadgen.services.contact_zones import (_LABEL_GAP_MM, contact_positions,
                                             declared_module)

BOARD = (30.0, 20.0)


def _side(edge, style, **extra):
    side = dict(edge=edge, count=5, pitch_mm=2.54, pad_length_mm=1.0, contact_style=style)
    side.update(extra)
    return side


def _depths(data):
    return {p["edge"]: p["depth_mm"] for p in contact_positions(data, *BOARD)}


def test_each_side_keeps_its_own_declared_half_hole():
    """@brief Positive: two sides of one module, two declared half-holes, two strips."""
    data = dict(castellated_sides=[
        _side("top", CONTACT_HALF_HOLE, half_hole_diameter_mm=1.0),
        _side("bottom", CONTACT_HALF_HOLE, half_hole_diameter_mm=2.0)])
    depths = _depths(data)
    assert depths["top"] == pytest.approx(1.0 + 0.5 + _LABEL_GAP_MM)
    assert depths["bottom"] == pytest.approx(1.0 + 1.0 + _LABEL_GAP_MM)


def test_a_side_drill_wins_over_the_module_drill():
    """@brief Positive: through_hole_drill_mm of the side, not drill_mm of the module, sizes the bore."""
    common = dict(drill_mm=1.2, pad_diameter_mm=1.8, row_spacing_mm=17.46)
    plain = _depths(dict(common, castellated_sides=[
        _side("top", CONTACT_THROUGH_HOLE), _side("bottom", CONTACT_THROUGH_HOLE)]))
    own = _depths(dict(common, castellated_sides=[
        _side("top", CONTACT_THROUGH_HOLE, through_hole_drill_mm=0.8),
        _side("bottom", CONTACT_THROUGH_HOLE)]))
    inset = (20.0 - 17.46) / 2
    assert plain["top"] == pytest.approx(inset + 0.9 + 0.6 + _LABEL_GAP_MM)
    assert own["top"] == pytest.approx(inset + 0.9 + 0.4 + _LABEL_GAP_MM)
    assert own["bottom"] == plain["bottom"]


def test_the_ring_of_a_double_contact_sits_where_the_footprint_puts_it():
    """@brief The strip reaches the ring the footprint draws, row spacing included.

    Positive and negative on one module: with the declared row spacing the
    through-holes move (side_geometry) and the strip follows them; without it
    they sit at the minimum behind the half-hole. In both cases the far side of
    the ring the strip covers is the drill position of the generated footprint.
    """
    for row_spacing in (None, 16.0):
        data = dict(drill_mm=1.0, pad_diameter_mm=1.7, castellated_sides=[
            _side("top", CONTACT_HALF_AND_THROUGH), _side("bottom", CONTACT_HALF_AND_THROUGH)])
        if row_spacing is not None:
            data["row_spacing_mm"] = row_spacing
        module, pairs = declared_module(data, *BOARD)
        geometry = module.side_geometry(pairs[0][1])
        placement = next(p for p in pad_positions(module) if p.edge == "top")
        drill_from_edge = module.thru_hole_centre(placement)[1] + BOARD[1] / 2
        expected = (drill_from_edge + geometry.ring / 2
                    + geometry.half_hole / 2 + _LABEL_GAP_MM)
        assert _depths(data)["top"] == pytest.approx(expected)
        if row_spacing is not None:
            assert drill_from_edge == pytest.approx((BOARD[1] - row_spacing) / 2)


def test_an_unobserved_style_uses_declared_quantities_only():
    """@brief Negative: no style, no assumed hole -- the strip is not widened on a guess."""
    unknown = _depths(dict(castellated_sides=[_side("top", None, pad_width_mm=0.9)]))
    assert unknown["top"] == pytest.approx(1.0 + 0.45 + _LABEL_GAP_MM)
    observed = _depths(dict(castellated_sides=[_side("top", CONTACT_HALF_HOLE,
                                                     pad_width_mm=0.9)]))
    # Osservato: il semi-foro del lato (qui il riferimento del generatore) conta.
    assert observed["top"] > unknown["top"]


def test_side_objects_are_read_like_dicts():
    """@brief Tolerance: CastellatedSide objects give the same strips as extraction dicts."""
    dicts = dict(castellated_sides=[
        _side("top", CONTACT_HALF_HOLE, half_hole_diameter_mm=1.2),
        _side("bottom", CONTACT_THROUGH_HOLE)], row_spacing_mm=17.46)
    objects = dict(castellated_sides=[
        CastellatedSide(edge="top", count=5, pitch=2.54, contact_style=CONTACT_HALF_HOLE,
                        hole_diameter=1.2, pad_length=1.0),
        CastellatedSide(edge="bottom", count=5, pitch=2.54,
                        contact_style=CONTACT_THROUGH_HOLE, pad_length=1.0)],
                   row_spacing_mm=17.46)
    assert _depths(objects) == pytest.approx(_depths(dicts))
    xs = [p["x_mm"] for p in contact_positions(objects, *BOARD) if p["edge"] == "top"]
    assert xs == pytest.approx([-5.08, -2.54, 0.0, 2.54, 5.08])
