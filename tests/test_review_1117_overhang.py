"""@brief Castellated edge-land overhang resolved per side (review of the final generality check, 2026-11-17).

Three medium defects of the assumed overhang (`resolved_pad_overhang`):
(1) a declared land shorter than the assumed half-hole's reach set the
overhang to 0 with the false reason «reduced so the declared pad covers the
half-hole»; (2) one module-wide value let one side's short land move the
DECLARED lands of every other side inward; (3) continuous pads
(half_and_through with a through-hole) were shortened for a half-hole their
copper already covers. Each rule has a positive and a negative case that
differ for a structural reason (provenance, side, contact style), checked on
several samples rather than on one module.
"""

import math

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.generators import castellated_module as cm
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, MIN_INNER_LAND_MM,
    AssumedSize, CastellatedModuleParams, CastellatedSide, build_castellated_module, pad_positions,
)


def _floor(value):
    """Round down to 0.01 mm, like the generator's estimates."""
    return math.floor(value * 100 + 1e-9) / 100


def _reach(params, side):
    """How far the side's half-hole reaches inside the module edge, mm."""
    geometry = params.side_geometry(side)
    return geometry.half_hole_inset + geometry.half_hole / 2


def _module(*sides, **module):
    """A 20 x 15 mm module with the given sides and module-wide sizes."""
    return CastellatedModuleParams(name="M", body_length=20.0, body_width=15.0, sides=list(sides), **module)


# -- (1) the undeclared half-hole fits under the declared land ---------------

@pytest.mark.parametrize("pitch, length, inset", [
    (2.54, 1.2, None), (2.54, 1.4, None), (2.0, 1.0, None), (2.54, 0.9, 0.2), (1.27, 0.6, None)])
def test_an_assumed_half_hole_is_reduced_to_fit_under_a_declared_land(pitch, length, inset):
    """@brief Positive: half-hole NOT declared, land DECLARED too short for it -> the half-hole is
    reduced (SIZE_ESTIMATE_PAD_LENGTH) until its far side lies under the land, the module validates,
    and the overhang is never the false «covers the half-hole» 0 nor the contradiction."""
    # No side width: the width rule (SIZE_ESTIMATE_PAD) stays out of the way.
    side = CastellatedSide("left", 3, pitch, pad_length=length, contact_style=CONTACT_HALF_HOLE)
    params = _module(side, hole_inset=inset)
    params.validate()
    geometry = params.side_geometry(side)
    assert geometry.half_hole_source == cm.SIZE_ESTIMATE_PAD_LENGTH
    assert _reach(params, side) <= length + 1e-9
    overhang, source = params.resolved_pad_overhang(side)
    assert source != cm.SIZE_ESTIMATE_SHORT_LAND
    assert length - overhang >= _reach(params, side) - 1e-9
    description = build_castellated_module(params).description
    assert "reduced to fit under the declared pad" in description
    assert any("sotto la piazzola dichiarata" in note for note in params.geometry_notes())


@pytest.mark.parametrize("pitch, length", [(2.54, 1.2), (2.54, 1.0), (2.0, 0.8)])
def test_a_declared_half_hole_longer_than_the_declared_land_is_reported_as_a_contradiction(pitch, length):
    """@brief Negative of the reduction: the SAME land with the half-hole DECLARED (1.7 mm) -> not
    reduced; the overhang becomes 0 with SIZE_ESTIMATE_SHORT_LAND, the description and the note name
    the contradiction (never «covers the half-hole», which is false), and the module is still built."""
    side = CastellatedSide("left", 3, pitch, pad_length=length,
                           contact_style=CONTACT_HALF_HOLE, hole_diameter=1.0 if pitch < 2.5 else 1.7)
    params = _module(side)
    params.validate()
    assert params.side_geometry(side).half_hole_source == cm.SIZE_SIDE
    assert params.resolved_pad_overhang(side) == (0.0, cm.SIZE_ESTIMATE_SHORT_LAND)
    description = build_castellated_module(params).description
    assert "set to 0, the declared pad is shorter than the half-hole" in description
    assert "covers the half-hole" not in description
    note = next(note for note in params.geometry_notes() if "si contraddicono" in note)
    assert "{}mm".format(length) in note


def test_a_land_long_enough_leaves_the_assumed_half_hole_and_overhang_alone():
    """@brief Negative: a declared 3.6 mm land -> the 1.7 mm reference half-hole and the 1.4 mm
    reference overhang both fit and stay references."""
    side = CastellatedSide("left", 3, 2.54, pad_length=3.6, contact_style=CONTACT_HALF_HOLE)
    params = _module(side)
    assert params.side_geometry(side).half_hole_source == cm.SIZE_REFERENCE
    assert params.resolved_pad_overhang(side) == (1.4, cm.SIZE_REFERENCE)


def test_a_declared_overhang_bounds_the_assumed_half_hole_with_the_land_left_under_the_module():
    """@brief Positive: declared land 1.8 mm and DECLARED overhang 0.5 mm -> the assumed half-hole
    fits within the 1.3 mm under the module and nothing is said about the overhang; with the overhang
    ASSUMED the half-hole gives way first (it is not written in the footprint, the overhang moves the
    copper): it fits within the 0.4 mm the reference 1.4 mm overhang leaves, and the overhang stays
    the reference."""
    def build(overhang):
        side = CastellatedSide("left", 3, 2.54, pad_length=1.8, contact_style=CONTACT_HALF_HOLE)
        kwargs = {} if overhang is None else {"pad_overhang": overhang}
        return side, _module(side, **kwargs)
    side, declared = build(0.5)
    assert declared.side_geometry(side).half_hole_source == cm.SIZE_ESTIMATE_PAD_LENGTH
    assert _reach(declared, side) <= 1.3 + 1e-9
    side, assumed = build(None)
    assert assumed.side_geometry(side).half_hole_source == cm.SIZE_ESTIMATE_PAD_LENGTH
    assert _reach(assumed, side) <= 0.4 + 1e-9
    assert assumed.resolved_pad_overhang(side) == (1.4, cm.SIZE_REFERENCE)


#: Setups of the length sweep: (pitch, declared hole_inset, module land width, side half-hole
#: estimate). Structural variety, not tuned values: fine and coarse pitch, a declared centre that
#: raises the smallest half-hole, a declared module width that bounds it first, and an estimate of
#: this part (which gives way like the reference, user decision 2026-11-17).
SWEEP_SETUPS = [(2.54, None, None, None), (1.27, None, None, None), (2.54, 0.3, None, None),
                (2.54, None, 1.0, None), (2.54, None, None, 1.0), (2.0, 0.2, 0.9, 0.8)]


@pytest.mark.parametrize("setup", SWEEP_SETUPS)
def test_the_assumptions_give_way_continuously_over_the_declared_length(setup):
    """@brief Positive, swept over declared lands 0.20-3.15 mm on several setups: the land always
    covers the half-hole unless the overhang is 0 with SIZE_ESTIMATE_SHORT_LAND; neither the
    half-hole nor the overhang ever grows when the land gets shorter (no jump back to a reference);
    the half-hole never drops below the smallest estimated one; and the half-hole, reference or
    estimate of this part alike (user decision 2026-11-17), reaches that floor before the reference
    overhang moves."""
    pitch, inset, width, estimate = setup

    def resolved(value):
        side = CastellatedSide("left", 3, pitch, pad_length=value, contact_style=CONTACT_HALF_HOLE,
                               hole_diameter=None if estimate is None else AssumedSize(estimate))
        params = _module(side, hole_inset=inset, **({} if width is None else {"pad_width": width}))
        return params, params.side_geometry(side).half_hole, params.resolved_pad_overhang(side)

    previous = None
    for step in range(60, -1, -1):
        length = round(0.2 + 0.05 * step, 2)
        params, half, (overhang, source) = resolved(length)
        side = params.sides[0]
        smallest = params._smallest_estimated_half_hole()
        assert half >= smallest - 1e-9
        if source == cm.SIZE_ESTIMATE_SHORT_LAND:
            assert overhang == 0 and length < _reach(params, side)
        else:
            assert length - overhang >= _reach(params, side) - 1e-9
        if overhang < 1.4 - 1e-9:
            # The half-hole, reference or estimate alike (user decision 2026-11-17), gave way
            # first: at its floor, or already narrower for the land width.
            assert half == pytest.approx(smallest) or params.side_geometry(side).half_hole_source in (
                cm.SIZE_ESTIMATE_PAD, cm.SIZE_ESTIMATE_MIN)
        if previous is not None:
            assert half <= previous[0] + 1e-9 and overhang <= previous[1] + 1e-9
        previous = (half, overhang)


# -- (2) the overhang is resolved per side ------------------------------------

def _two_sides(left_length, right_length):
    """Left and right half-hole sides with declared holes and the given declared land lengths."""
    left = CastellatedSide("left", 3, 2.54, pad_length=left_length, pad_width=1.0,
                           contact_style=CONTACT_HALF_HOLE)
    right = CastellatedSide("right", 3, 2.54, first_pin=4, pad_length=right_length, pad_width=1.0,
                            contact_style=CONTACT_HALF_HOLE)
    return _module(left, right, hole_diameter=0.6, hole_inset=0.2)


def test_a_short_land_on_one_side_does_not_move_the_declared_lands_of_another():
    """@brief Positive: left land 1.2 mm (overhang reduced to 0.70), right land 3.0 mm -> the right
    side keeps the 1.40 mm reference and its pads sit exactly where a module with the right side
    alone puts them; the description names each side's overhang."""
    params = _two_sides(1.2, 3.0)
    left, right = params.sides
    assert params.resolved_pad_overhang(left) == (0.7, cm.SIZE_ESTIMATE_LENGTH)
    assert params.resolved_pad_overhang(right) == (1.4, cm.SIZE_REFERENCE)
    alone = _module(CastellatedSide("right", 3, 2.54, first_pin=4, pad_length=3.0, pad_width=1.0,
                                    contact_style=CONTACT_HALF_HOLE), hole_diameter=0.6, hole_inset=0.2)
    assert ([(p.x, p.y) for p in pad_positions(params) if p.edge == "right"]
            == [(p.x, p.y) for p in pad_positions(alone)])
    # Copper of the left pads: outer end 0.70 mm beyond the edge.
    left_pad = next(p for p in pad_positions(params) if p.edge == "left")
    assert left_pad.x - left_pad.width / 2 == pytest.approx(-10.0 - 0.7)
    description = build_castellated_module(params).description
    assert "left side: pad overhang 0.70mm beyond the edge (reduced" in description
    assert "right side: pad overhang 1.40mm beyond the edge assumed" in description
    assert "every side" not in description
    assert params.effective_pad_overhang == pytest.approx(1.4)  # the envelope: the farthest copper


def test_sides_that_share_the_same_overhang_are_described_once():
    """@brief Negative: both lands 1.2 mm -> the same reduced overhang on both sides, written once
    («every side») in the description and in one note."""
    params = _two_sides(1.2, 1.2)
    description = build_castellated_module(params).description
    assert description.count("pad overhang") == 1
    assert "every side: pad overhang 0.70mm beyond the edge" in description
    notes = [note for note in params.geometry_notes() if note.startswith("Sbalzo delle piazzole")]
    assert len(notes) == 1 and "tutti i lati" in notes[0]


def test_the_3d_lands_use_their_own_sides_overhang():
    """@brief Positive: the STEP payload's golden land inside the board is the footprint land minus
    ITS side's overhang (left 1.2 - 0.70, right 3.0 - 1.40)."""
    params = _two_sides(1.2, 3.0)
    contacts = step_generator.castellated_contact_payload(params, "#d4af37")[1]
    inner = {c["edge"]: c["pad_length_mm"] for c in contacts}
    assert inner["left"] == pytest.approx(1.2 - 0.7)
    assert inner["right"] == pytest.approx(3.0 - 1.4)


def test_a_declared_side_overhang_outranks_the_module_and_is_validated():
    """@brief Positive: a side's own declared overhang is used as is (SIZE_SIDE); negative: a
    negative one is refused like the module's."""
    params = _two_sides(3.0, 3.0)
    params.sides[0].pad_overhang = 0.3
    assert params.resolved_pad_overhang(params.sides[0]) == (0.3, cm.SIZE_SIDE)
    assert params.resolved_pad_overhang(params.sides[1]) == (1.4, cm.SIZE_REFERENCE)
    params.validate()
    params.sides[0].pad_overhang = -0.1
    with pytest.raises(ValueError, match="sbalzo"):
        params.validate()


# -- (3) continuous pads are not shortened for their half-hole ---------------

@pytest.mark.parametrize("length", [1.5, 1.45, 2.0])
def test_a_continuous_pad_keeps_the_reference_overhang_while_an_edge_land_is_shortened(length):
    """@brief Positive: half_and_through WITH a through-hole (one continuous pad) and a declared
    land just longer than the overhang -> the 1.4 mm reference is kept, the land only has to stay
    partly under the module; negative: the same land on a half_hole side (edge land only) ->
    shortened so the land covers its half-hole."""
    def build(style):
        side = CastellatedSide("left", 3, 2.54, pad_length=length, pad_width=1.8, contact_style=style)
        return side, _module(side, hole_diameter=1.0, hole_inset=0.3)
    side, continuous = build(CONTACT_HALF_AND_THROUGH)
    assert continuous.side_geometry(side).has_thru_holes
    assert continuous.resolved_pad_overhang(side) == (1.4, cm.SIZE_REFERENCE)
    continuous.validate()
    side, edge_only = build(CONTACT_HALF_HOLE)
    value, source = edge_only.resolved_pad_overhang(side)
    assert source == cm.SIZE_ESTIMATE_LENGTH and value == pytest.approx(_floor(length - 0.8))


def test_a_continuous_pad_with_a_land_shorter_than_the_reference_overhang_is_built_unchanged():
    """@brief Positive: a continuous pad with a declared 1.2 mm edge land -> the 1.4 mm reference is
    kept and the module validates: its copper runs on from the edge to the ring, so it lies under
    the module whatever the edge land's length (validate's «all outside» rule is for edge lands);
    negative: the same land as a half_hole edge land -> the overhang gives way."""
    side = CastellatedSide("left", 3, 2.54, pad_length=1.2, pad_width=1.8,
                           contact_style=CONTACT_HALF_AND_THROUGH)
    params = _module(side, hole_diameter=1.0, hole_inset=0.3)
    assert params.resolved_pad_overhang(side) == (1.4, cm.SIZE_REFERENCE)
    params.validate()
    copper = next(cm.continuous_pads(params))
    assert copper.outer_reach == pytest.approx(1.4) and copper.inner_reach > 1.4
    edge = CastellatedSide("left", 3, 2.54, pad_length=1.2, pad_width=1.8, contact_style=CONTACT_HALF_HOLE)
    edge_only = _module(edge, hole_diameter=1.0, hole_inset=0.3)
    assert edge_only.resolved_pad_overhang(edge)[1] == cm.SIZE_ESTIMATE_LENGTH


def test_a_half_and_through_side_without_a_through_hole_is_still_an_edge_land():
    """@brief Negative of (3): half_and_through with the drill at 0 (half-holes only) has an edge
    land, not a continuous pad -> shortened to cover its half-hole like half_hole."""
    side = CastellatedSide("left", 3, 2.54, pad_length=1.5, pad_width=1.8,
                           contact_style=CONTACT_HALF_AND_THROUGH)
    params = _module(side, hole_diameter=1.0, hole_inset=0.3, thru_hole_drill=0)
    assert not params.side_geometry(side).has_thru_holes
    assert params.resolved_pad_overhang(side) == (_floor(1.5 - 0.8), cm.SIZE_ESTIMATE_LENGTH)


# -- low findings ---------------------------------------------------------------

@pytest.mark.parametrize("body, pitch", [(5.0, 3.6), (4.0, 2.6), (4.4, 3.0)])
def test_a_land_shortened_for_clearance_says_when_it_no_longer_covers_its_half_hole(body, pitch):
    """@brief Positive: assumed lengths shortened to clear another side -> a note exactly when the
    shortened land no longer reaches past its half-hole under the module; negative: the side whose
    land was not shortened gets no such note."""
    sides = [CastellatedSide(edge="left", count=2, pitch=pitch, pad_length=1.0, pad_width=0.9,
                             contact_style=CONTACT_HALF_HOLE),
             CastellatedSide(edge="bottom", count=2, pitch=pitch, first_pin=3, pad_width=0.9,
                             contact_style=CONTACT_HALF_HOLE)]
    params = CastellatedModuleParams(name="P", body_length=body, body_width=body, sides=sides)
    params.validate()
    notes = params.geometry_notes()
    for side, size in zip(params.sides, params.pad_sizes()):
        uncovered = (size.length_source == cm.PAD_ESTIMATE_CLEARANCE
                     and size.length - params.side_pad_overhang(side) < _reach(params, side) - 1e-9)
        label = cm.edge_label(side.edge)
        flagged = any(note.startswith("Lato {}: accorciata".format(label)) for note in notes)
        assert flagged == uncovered
    assert params.pad_sizes()[1].length_source == cm.PAD_ESTIMATE_CLEARANCE


def test_the_resolved_overhangs_are_cached_and_follow_an_edit(monkeypatch):
    """@brief Positive: building a footprint resolves each side's overhang once; negative: editing a
    side's declared land in place resolves it again, with the new value."""
    calls = []
    real = CastellatedModuleParams._resolve_side_overhang

    def counted(self, side):
        calls.append(side.edge)
        return real(self, side)
    monkeypatch.setattr(CastellatedModuleParams, "_resolve_side_overhang", counted)
    params = _two_sides(1.2, 3.0)
    build_castellated_module(params)
    assert sorted(calls) == ["left", "right"]
    params.sides[0].pad_length = 3.0
    assert params.side_pad_overhang(params.sides[0]) == pytest.approx(1.4)


def test_a_through_hole_side_has_no_overhang_entry():
    """@brief Negative: a through_hole side has no edge land -> no overhang in its description, and
    an assumed land length on another side keeps the reference."""
    params = _module(CastellatedSide("left", 3, 2.54, contact_style=CONTACT_THROUGH_HOLE),
                     CastellatedSide("right", 3, 2.54, first_pin=4, pad_length=AssumedSize(3.0),
                                     contact_style=CONTACT_HALF_HOLE))
    description = build_castellated_module(params).description
    assert description.count("pad overhang") == 1
    assert "every side: pad overhang 1.40mm" in description


# -- review findings on the first version (2026-11-17) -------------------------

def test_a_short_land_names_the_declared_value_it_contradicts():
    """@brief Positive, three causes of SIZE_ESTIMATE_SHORT_LAND, each note naming what to check:
    a declared half-hole (its diameter), a declared half-hole centre with an undeclared half-hole
    clamped at the smallest estimated one (the centre distance, not the diameter), nothing declared
    but a land shorter than the smallest half-hole (the land only)."""
    def notes(length, **module):
        side = CastellatedSide("left", 3, 2.54, pad_length=length, contact_style=CONTACT_HALF_HOLE)
        params = _module(side, **module)
        params.validate()
        assert params.resolved_pad_overhang(side) == (0.0, cm.SIZE_ESTIMATE_SHORT_LAND)
        return params, [note for note in params.geometry_notes() if note.startswith("Piazzole dichiarate")]
    _params, declared_hole = notes(1.0, hole_diameter=1.7)
    assert "diametro del semi-foro" in declared_hole[0] and "distanza" not in declared_hole[0]
    params, declared_inset = notes(1.0, hole_inset=0.5)
    # Clamped at the smallest estimated half-hole, never put back to the 1.7 mm reference.
    assert params.side_geometry(params.sides[0]).half_hole == pytest.approx(params._smallest_estimated_half_hole())
    assert "distanza del semi-foro dal bordo" in declared_inset[0] and "0.5mm" in declared_inset[0]
    params, nothing = notes(0.2)
    assert params.side_geometry(params.sides[0]).half_hole == pytest.approx(cm.MIN_ESTIMATED_DRILL_MM)
    assert "Controlla la lunghezza della piazzola." in nothing[0] and "diametro" not in nothing[0]


def test_a_declared_overhang_that_leaves_the_half_hole_uncovered_is_reported():
    """@brief Positive: declared land 0.6 mm and DECLARED overhang 0.5 mm with a declared 1.0 mm
    half-hole -> the overhang is kept (declared) and a note names the contradiction; negative: a
    declared land long enough -> no such note."""
    def build(length):
        side = CastellatedSide("left", 3, 2.54, pad_length=length, contact_style=CONTACT_HALF_HOLE)
        return _module(side, pad_overhang=0.5, hole_diameter=1.0)
    short = build(0.6)
    short.validate()
    assert short.side_pad_overhang(short.sides[0]) == pytest.approx(0.5)
    assert any("con lo sbalzo dichiarato di 0.5mm" in note for note in short.geometry_notes())
    assert not any("sbalzo dichiarato" in note for note in build(2.0).geometry_notes())


def test_an_undeclared_half_hole_under_a_declared_overhang_is_clamped_not_put_back():
    """@brief Positive (generality audit, item 3): declared land 0.6 mm and overhang 0.5 mm, half-hole
    NOT declared -> the half-hole is the smallest estimated one, never the 1.7 mm reference, and the
    contradiction note is written; a longer land (0.8 mm) gives a half-hole not smaller."""
    def half(length):
        side = CastellatedSide("left", 3, 2.54, pad_length=length, contact_style=CONTACT_HALF_HOLE)
        params = _module(side, pad_overhang=0.5)
        return params, params.side_geometry(side).half_hole
    params, small = half(0.6)
    assert small == pytest.approx(cm.MIN_ESTIMATED_DRILL_MM)
    assert any("con lo sbalzo dichiarato" in note for note in params.geometry_notes())
    assert half(0.8)[1] >= small


def test_equal_sides_share_one_entry_and_sides_on_one_edge_are_told_apart():
    """@brief Positive: top and bottom with assumed lands (reference overhang) and left with a short
    declared land -> two description entries, «top/bottom» once; two sides on the same edge -> each
    entry carries its pad range."""
    params = _module(CastellatedSide("top", 3, 2.54, contact_style=CONTACT_HALF_HOLE),
                     CastellatedSide("bottom", 3, 2.54, first_pin=4, contact_style=CONTACT_HALF_HOLE),
                     CastellatedSide("left", 3, 2.54, first_pin=7, pad_length=1.2,
                                     contact_style=CONTACT_HALF_HOLE),
                     hole_diameter=0.6, hole_inset=0.2)
    description = build_castellated_module(params).description
    assert description.count("pad overhang") == 2
    assert "top/bottom side: pad overhang 1.40mm" in description
    same_edge = _module(CastellatedSide("left", 2, 2.54, offset=-3.0, pad_length=1.2,
                                        contact_style=CONTACT_HALF_HOLE),
                        CastellatedSide("left", 2, 2.54, first_pin=3, offset=3.0,
                                        contact_style=CONTACT_HALF_HOLE),
                        hole_diameter=0.6, hole_inset=0.2)
    description = build_castellated_module(same_edge).description
    assert "left 1-2 side:" in description and "left 3-4 side:" in description


def test_a_module_wide_declared_land_bounds_the_assumed_holes_like_a_sides_own():
    """@brief Positive (generality audit, item 2: one lookup on both axes): the module's DECLARED
    1.2 mm land width bounds the assumed half-hole like a side's own 1.2 mm width; negative: the same
    1.2 mm module width left assumed (AssumedSize, the window's untouched field) bounds nothing."""
    def half(width, on_side):
        side = CastellatedSide("left", 3, 2.54, contact_style=CONTACT_HALF_HOLE,
                               **({"pad_width": width} if on_side else {}))
        params = _module(side, **({} if on_side else {"pad_width": width}))
        return params.side_geometry(side)
    module_wide, own = half(1.2, False), half(1.2, True)
    assert module_wide.half_hole == own.half_hole and module_wide.half_hole_source == cm.SIZE_ESTIMATE_PAD
    assert half(AssumedSize(1.2), False).half_hole_source == cm.SIZE_REFERENCE


# -- second generality audit (2026-11-17) --------------------------------------

def test_a_module_land_width_does_not_touch_the_drill_of_a_through_hole_side():
    """@brief Negative: a through_hole side draws its ring, not the edge land, so a module-wide
    declared width is no evidence of its copper: with a declared 1.0 mm ring and an assumed 1.0 mm
    drill the module is refused whether the module width is declared or assumed (the width used to
    shrink the drill to 0.6 mm and hide the contradiction)."""
    for width in (1.8, 0.9, AssumedSize(1.8)):
        params = _module(CastellatedSide("left", 3, 2.54, contact_style=CONTACT_THROUGH_HOLE),
                         CastellatedSide("right", 3, 2.54, first_pin=4, contact_style=CONTACT_HALF_HOLE),
                         thru_pad_diameter=1.0, pad_width=width)
        geometry = params.side_geometry(params.sides[0])
        assert geometry.drill == pytest.approx(1.0)
        with pytest.raises(ValueError, match="piu' larga del foro"):
            params.validate()


def test_the_half_hole_gives_way_whatever_its_provenance():
    """@brief User decision 2026-11-17 («cede sempre il semi-foro»). Positive: declared land 1.8 mm,
    the side's half-hole estimated 1.7 mm or 1.0 mm (AssumedSize, as the window passes a photo
    estimate) -> the estimate is reduced like the reference and the land stays at the 1.4 mm
    reference overhang: the soldered copper does not move on the provenance of a value the
    footprint does not write. Negative: a DECLARED half-hole is never reduced, so the overhang
    gives way instead."""
    def build(hole):
        side = CastellatedSide("left", 3, 2.54, pad_length=1.8, contact_style=CONTACT_HALF_HOLE,
                               hole_diameter=hole)
        return side, _module(side)
    positions = set()
    for hole in (None, AssumedSize(1.7), AssumedSize(1.0)):
        side, params = build(hole)
        assert params.side_geometry(side).half_hole_source == cm.SIZE_ESTIMATE_PAD_LENGTH
        assert params.resolved_pad_overhang(side) == (1.4, cm.SIZE_REFERENCE)
        positions.add(round(next(pad_positions(params)).x, 6))
    assert len(positions) == 1
    side, declared = build(1.0)
    assert declared.side_geometry(side).half_hole == pytest.approx(1.0)
    assert declared.resolved_pad_overhang(side) == (_floor(1.8 - _reach(declared, side)),
                                                    cm.SIZE_ESTIMATE_LENGTH)


def test_the_reduced_half_hole_note_quotes_what_the_footprint_draws():
    """@brief Positive: declared land 1.2 mm, nothing else -> the note names the land, the overhang
    the side really uses and the land left under the module, the numbers of the written pad (never
    a negative room computed with an overhang that was not used)."""
    side = CastellatedSide("left", 3, 2.54, pad_length=1.2, contact_style=CONTACT_HALF_HOLE)
    params = _module(side)
    overhang = params.side_pad_overhang(side)
    note = next(note for note in params.geometry_notes() if "ridotto a" in note and "dentro il modulo" in note)
    assert "{:.2f}mm".format(overhang) in note and "{:.2f}mm dentro".format(1.2 - overhang) in note
    assert "-0." not in note
    pad = next(p for p in pad_positions(params))
    assert pad.x - pad.width / 2 == pytest.approx(-10.0 - overhang)


def test_the_declared_centre_is_blamed_only_when_it_sets_the_smallest_half_hole():
    """@brief Positive: hole_inset 0.5 mm -> the smallest half-hole is 1.01 mm because of it, the
    note names the centre; negative: hole_inset 0.1 mm -> the fabrication minimum sets it, the note
    asks only for the land length."""
    def note(inset, length):
        side = CastellatedSide("left", 3, 2.54, pad_length=length, contact_style=CONTACT_HALF_HOLE)
        params = _module(side, hole_inset=inset)
        assert params.resolved_pad_overhang(side)[1] == cm.SIZE_ESTIMATE_SHORT_LAND
        return next(n for n in params.geometry_notes() if n.startswith("Piazzole dichiarate"))
    assert "distanza del semi-foro dal bordo" in note(0.5, 1.0)
    assert "Controlla la lunghezza della piazzola." in note(0.1, 0.2)


# -- second adversarial review (2026-11-17) ------------------------------------

def test_a_land_too_narrow_for_the_smallest_drill_is_reported_never_hidden():
    """@brief Swept over module land widths 1.20-0.40 mm on half_and_through sides (third review
    2026-11-17, which made the drill NOT clamp at the minimum: a clamped drill left annular rings
    under MIN_HOLE_WALL_MM accepted silently). Positive: while the land leaves room for the
    smallest drill and its ring, drill and ring fit the land and never grow as it narrows.
    Negative: below that (2 x MIN_HOLE_WALL_MM + MIN_ESTIMATED_DRILL_MM) the drill keeps its
    estimate and the note says the ring is wider than the land. Every accepted drill keeps an
    annular ring of at least MIN_HOLE_WALL_MM."""
    floor = 2 * cm.MIN_HOLE_WALL_MM + cm.MIN_ESTIMATED_DRILL_MM
    previous = None
    for step in range(120, 39, -1):
        width = step / 100
        params = _module(CastellatedSide("left", 3, 2.54, contact_style=CONTACT_HALF_AND_THROUGH),
                         CastellatedSide("right", 3, 2.54, first_pin=4, contact_style=CONTACT_HALF_AND_THROUGH),
                         pad_width=width, pad_length=1.8)
        geometry = params.side_geometry(params.sides[0])
        assert (geometry.ring - geometry.drill) / 2 >= cm.MIN_HOLE_WALL_MM - 1e-9
        wider = any("piu' larga" in note and "piazzola" in note for note in params.geometry_notes())
        if width >= floor - 1e-9:
            assert geometry.ring <= width + 1e-9 and not wider
            if previous is not None:
                assert geometry.drill <= previous[0] + 1e-9 and geometry.ring <= previous[1] + 1e-9
            previous = (geometry.drill, geometry.ring)
        else:
            assert geometry.ring > width and wider


def test_a_clamped_half_hole_is_not_described_as_fitting():
    """@brief Positive: declared land 0.2 mm -> the half-hole stops at the smallest estimable and
    still does not fit: SIZE_ESTIMATE_MIN, described as «still too large», never «reduced to fit»;
    negative: a 1.2 mm land the reduced half-hole really fits -> «reduced to fit under the declared
    pad»."""
    def built(length):
        side = CastellatedSide("left", 3, 2.54, pad_length=length, contact_style=CONTACT_HALF_HOLE)
        params = _module(side)
        return params, build_castellated_module(params).description
    params, description = built(0.2)
    assert params.side_geometry(params.sides[0]).half_hole_source == cm.SIZE_ESTIMATE_MIN
    assert "reduced to the smallest estimable, still too large for its copper" in description
    assert "reduced to fit under the declared pad" not in description
    params, description = built(1.2)
    assert params.side_geometry(params.sides[0]).half_hole_source == cm.SIZE_ESTIMATE_PAD_LENGTH
    assert "reduced to fit under the declared pad" in description


def test_a_declared_overhang_over_an_assumed_land_is_checked_too():
    """@brief Positive: module overhang DECLARED 3.0 mm over the assumed 3.6 mm reference land ->
    0.6 mm left under the module against the 1.7 mm reference half-hole: the contradiction note is
    written; negative: a declared 1.4 mm overhang leaves 2.2 mm -> no note."""
    def notes(overhang):
        params = _module(CastellatedSide("left", 3, 2.54, contact_style=CONTACT_HALF_HOLE),
                         pad_overhang=overhang)
        params.validate()
        return [note for note in params.geometry_notes() if "sbalzo dichiarato" in note]
    assert len(notes(3.0)) == 1 and "3.60mm" in notes(3.0)[0]
    assert notes(1.4) == []


def test_a_land_shortened_for_clearance_is_not_reported_twice_under_a_declared_overhang():
    """@brief Negative (third review 2026-11-17): a declared overhang over assumed lands shortened
    for another side's copper -> only the shortening note says the land no longer covers the
    half-hole, not the declared-overhang note too; positive: the same declared overhang over an
    unshortened assumed land too short for its half-hole -> the declared-overhang note."""
    for body, pitch in ((5.0, 3.6), (4.0, 2.6), (4.4, 3.0)):
        sides = [CastellatedSide("left", 2, pitch, pad_width=0.9, contact_style=CONTACT_HALF_HOLE),
                 CastellatedSide("bottom", 2, pitch, first_pin=3, pad_width=0.9,
                                 contact_style=CONTACT_HALF_HOLE)]
        params = CastellatedModuleParams(name="D", body_length=body, body_width=body, sides=sides,
                                         pad_overhang=1.4)
        params.validate()
        shortened = params.geometry_notes()
        assert all(size.length_source == cm.PAD_ESTIMATE_CLEARANCE for size in params.pad_sizes())
        assert sum("accorciata" in note for note in shortened) == 2
        assert not any("sbalzo dichiarato" in note for note in shortened)
    params = _module(CastellatedSide("left", 3, 2.54, pad_length=AssumedSize(2.0),
                                     contact_style=CONTACT_HALF_HOLE), pad_overhang=1.4)
    params.validate()
    assert any("sbalzo dichiarato" in note for note in params.geometry_notes())
