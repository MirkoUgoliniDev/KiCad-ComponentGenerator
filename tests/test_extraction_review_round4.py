"""@brief Round 4 of the extraction review (2026-11-11): ONE invariant for the explicit pads.

After the pad rules have run (`_resolve_pad_terminals`), the explicit
geometry either validates as a footprint (`package_from_mapping`, overlaps
included) or is returned as UNREAD geometry with a `pin_map_conflict` that
names the pads and the remedy; a paid answer is never lost to an exception
raised by the program's own derivation, and nothing changes a pad after its
last validation. A malformed VALUE (text, boolean, non-finite, a list in a
scalar field) stays a readable `ExtractionError`.

Every rule has a positive and a negative case that differ for a structural
reason (a pad type with or without a hole, a land read or estimated, a
count or a size in a callout), never for a number tuned to a part. Invented
parts; no network, no AI call.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import json

import pytest

from kicadgen.core import tht_estimates
from kicadgen.core.package_geometry import (copper_separation, pad_copper_shape,
                                            package_from_mapping)
from kicadgen.generators.pin_header import MIN_PAD_GAP_MM
from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import ExtractionError
from kicadgen.services.openrouter import strict_tool

RING = tht_estimates.MIN_ANNULAR_RING_MM


def _pin(number, x, y=0.0, **extra):
    """One pad of the main answer's explicit geometry: through-hole, round 1.6 mm, hole 1.0 unless `extra` says otherwise."""
    pin = {"number": number, "x_mm": x, "y_mm": y, "pad_length_mm": 1.6, "pad_width_mm": 1.6,
           "pad_type": "thru_hole", "shape": "circle", "drill_mm": 1.0}
    pin.update(extra)
    return {key: value for key, value in pin.items() if value != "DEL"}


def _main(pins, **extra):
    """A main-shape answer whose footprint is the explicit pads `pins`."""
    data = {"layout": "matrix", "drawing_unit": "mm", "name": "X", "body_length_mm": 10,
            "body_width_mm": 5, "body_height_mm": 3,
            "package_geometry": {"name": "X", "body_length_mm": 10, "body_width_mm": 5, "pins": pins}}
    data.update(extra)
    return data


def _run(data):
    """The validated result of a main-shape answer."""
    return ext._params_from_tool_arguments(data)


def _by_number(params):
    """{number: pin dict} of the result's explicit geometry (the unread one when the pads were dropped)."""
    geometry = params.package_geometry or params.unread_pad_geometry
    return {pin["number"]: pin for pin in geometry["pins"]}


def _assert_invariant(params):
    """The invariant of round 4 on one result: a valid footprint, or unread pads with a block."""
    if params.package_geometry is not None:
        package_from_mapping(params.package_geometry)
        assert params.unread_pads is None
        return "footprint"
    assert params.unread_pads and params.pin_map_conflict
    assert params.unread_pad_geometry is not None
    return "unread"


def _smallest_gap(params):
    """The smallest copper-to-copper distance, mm, among the pads of a validated result."""
    shapes = [pad_copper_shape(pin) for pin in package_from_mapping(params.package_geometry).pins]
    return min(copper_separation(a, b) for a, b in itertools.combinations(shapes, 2))


def _ring(pin):
    """The copper ring per side, mm, a pad leaves around its hole (smaller axis)."""
    slot = pin.get("drill_length_mm") or pin["drill_mm"]
    return min(pin["pad_length_mm"] - pin["drill_mm"], pin["pad_width_mm"] - slot) / 2


# ---------------------------------------------------------------------------
# F7-3: a zero drill on a pad WITHOUT a hole is «no hole», anywhere else an error
# ---------------------------------------------------------------------------

def _smd(number, x, **extra):
    """One SMD pad 0.6 x 1.5 mm without any drill field unless `extra` gives one."""
    return _pin(number, x, pad_type="smd", shape="rect", pad_length_mm=0.6, pad_width_mm=1.5,
                **{"drill_mm": "DEL", **extra})


@pytest.mark.parametrize("zero", [0, 0.0])
def test_a_zero_drill_on_an_smd_pad_is_no_hole(zero):
    params = _run(_main([_smd("1", 0.0, drill_mm=zero, drill_length_mm=zero), _smd("2", 1.27, drill_mm=zero)]))
    assert _assert_invariant(params) == "footprint"
    assert all(pin.get("drill_mm") is None for pin in params.package_geometry["pins"])
    # «No hole» said with a number is not a doubt about the pad type.
    assert "dichiarati senza foro" not in params.notes


def test_a_real_drill_on_an_smd_pad_is_dropped_and_named():
    """Negative twin of the zero: a positive hole on a pad without one is a doubt, and is said."""
    params = _run(_main([_smd("1", 0.0, drill_mm=0.5), _smd("2", 1.27)]))
    assert _assert_invariant(params) == "footprint"
    assert "dichiarati senza foro" in params.notes and "1 (smd)" in params.notes


@pytest.mark.parametrize("pads", [
    # Contract changed on 2026-11-11 (review round 5, V1-6): a zero on a pad
    # that HAS a hole used to be an error here; it is «hole not printed» said
    # with a number, so the hole is derived from the terminal or the pad is
    # unread geometry with a block (tests/test_extraction_review_round5.py).
    # Negative, non-finite, boolean and text values are malformed on any pad type.
    [_pin("1", 0.0, drill_mm=-1), _pin("2", 2.54)],
    [_smd("1", 0.0, drill_mm=-1), _smd("2", 1.27)],
    [_smd("1", 0.0, drill_mm=float("nan")), _smd("2", 1.27)],
    [_smd("1", 0.0, drill_mm="0"), _smd("2", 1.27)],
    [_smd("1", 0.0, drill_mm=False), _smd("2", 1.27)],
])
def test_a_malformed_drill_is_still_an_error(pads):
    with pytest.raises(ExtractionError, match="drill_mm"):
        _run(_main(pads))


# ---------------------------------------------------------------------------
# F7-5, F6-8: a malformed container is a readable error, never AttributeError/TypeError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("geometry", [[1, 2], "abc", {"pins": 5}, {"pins": {"a": 1}},
                                      {"pins": ["x", "y"]}])
def test_a_geometry_of_the_wrong_shape_is_a_readable_error(geometry):
    with pytest.raises(ExtractionError, match="Geometria package non valida"):
        _run(_main([], package_geometry=geometry))


@pytest.mark.parametrize("empty", [None, {}, {"pins": []}, [], ""])
def test_an_empty_geometry_is_no_geometry_and_no_error(empty):
    """Negative twin: an empty optional field is absent; the matrix is then the footprint."""
    params = _run({"layout": "matrix", "drawing_unit": "mm", "rows": 1, "cols": 4, "pitch_mm": 2.54,
                   "pad_diameter_mm": 1.7, "drill_mm": 1.0, "package_geometry": empty})
    assert params.package_geometry is None and params.unread_pads is None and params.cols == 4


@pytest.mark.parametrize("field, value", [("shape", ["circle"]), ("pad_type", ["thru_hole"]),
                                          ("number", ["1"]), ("x_mm", [0.0]), ("drill_mm", {"mm": 1}),
                                          ("rotation_deg", [90])])
def test_a_list_in_a_scalar_pad_field_is_a_readable_error(field, value):
    with pytest.raises(ExtractionError, match="Geometria package non valida") as error:
        _run(_main([{**_pin("1", 0.0), field: value}, _pin("2", 2.54)]))
    assert field in str(error.value)


def test_an_object_field_of_the_wrong_type_is_ignored_not_an_error():
    """Negative twin: `lead` and `value_sources` ARE objects by contract; a wrong type there is dropped by their own rule."""
    params = _run(_main([_pin("1", 0.0, lead=["round", 0.6], value_sources=["read"]), _pin("2", 2.54)]))
    assert _assert_invariant(params) == "footprint"
    assert "terminale dichiarato non valido" in params.notes


def test_uniform_pad_fields_survive_a_list_in_a_scalar_field():
    """The replaced-pad path meets the broken answer before the validation: it must not raise TypeError."""
    uniform, differing = ext._uniform_pad_fields([{"number": "1", "shape": ["circle"]},
                                                  {"number": "2", "shape": ["circle"]}])
    assert differing == [] and uniform["shape"] == ["circle"]


def test_an_unknown_shape_is_an_error_even_where_the_land_would_be_derived():
    """The derived land replaces the shape: an unknown one must be refused before, not vanish."""
    pads = [_pin(str(i + 1), i * 2.54, shape="banana", drill_mm="DEL") for i in range(2)]
    with pytest.raises(ExtractionError, match="shape non valida"):
        _run(_main(pads, lead_diameter_mm=0.6, value_sources={"lead_diameter_mm": "read"}))
    # Negative twin: the legacy shape an older schema offered is still accepted.
    pads = [_pin(str(i + 1), i * 2.54, shape="trapezoid", drill_mm="DEL") for i in range(2)]
    assert _assert_invariant(_run(_main(pads, lead_diameter_mm=0.6,
                                        value_sources={"lead_diameter_mm": "read"}))) == "footprint"


# ---------------------------------------------------------------------------
# F6-1: a printed slot, and a read land that cannot hold the hole
# ---------------------------------------------------------------------------

def _slot_pads(**extra):
    """Two pads 5.08 mm apart with a printed slot 1.0 (X) x 3.0 (Y) and what `extra` says of the land."""
    base = dict(drill_mm=1.0, drill_length_mm=3.0, hole_callout="2-1.0x3.0")
    base.update(extra)
    return [_pin("1", 0.0, **copy.deepcopy(base)), _pin("2", 5.08, **copy.deepcopy(base))]


def test_a_printed_slot_beside_a_round_estimated_land_gets_a_land_derived_around_it():
    params = _run(_main(_slot_pads(pad_length_mm=1.8, pad_width_mm=1.8,
                                   value_sources={"drill_mm": "read", "pad_size": "estimated"})))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    # The slot as transcribed (a round land shows no orientation), the ring of the one rule on both axes.
    assert (pin["drill_mm"], pin["drill_length_mm"]) == (1.0, 3.0)
    annulus = tht_estimates.HOLE_TO_PAD_ANNULUS_MM
    assert (pin["pad_length_mm"], pin["pad_width_mm"], pin["shape"]) == (1.0 + annulus, 3.0 + annulus, "oval")
    assert pin["sources"] == {"drill_mm": "read", "pad_size": "derived", "shape": "derived"}


def test_a_printed_slot_is_not_turned_by_an_estimated_land():
    """The land 4 x 2 is only the model's guess: the printed slot keeps the axes it was transcribed on.

    Contract changed on 2026-11-11 (review round 5, V1-3): this slot used to
    be turned to the estimated land's long side and still called «read». The
    schema defines the slot's axes (drill_mm along X, drill_length_mm along
    Y); an estimate decides nothing about a printed value, so the land is
    derived around the slot as transcribed. Only a READ land turns a slot
    (the test after the next one).
    """
    params = _run(_main(_slot_pads(pad_length_mm=4.0, pad_width_mm=2.0, shape="oval",
                                   value_sources={"drill_mm": "read", "pad_size": "estimated"})))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    assert (pin["drill_mm"], pin["drill_length_mm"]) == (1.0, 3.0)
    assert pin["pad_length_mm"] < pin["pad_width_mm"]
    assert pin["sources"]["drill_mm"] == "read" and pin["sources"]["pad_size"] == "derived"
    assert "lato lungo sul lato lungo" not in params.notes


def test_a_printed_slot_inside_a_read_land_is_kept_as_transcribed():
    """Negative twin: a read land that holds the slot as transcribed changes nothing, across or not."""
    params = _run(_main(_slot_pads(pad_length_mm=4.5, pad_width_mm=4.0, shape="oval",
                                   value_sources={"drill_mm": "read", "pad_size": "read"})))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    assert (pin["drill_mm"], pin["drill_length_mm"], pin["pad_length_mm"], pin["pad_width_mm"]) == (
        1.0, 3.0, 4.5, 4.0)
    assert "lato lungo sul lato lungo" not in params.notes


def test_a_horizontal_slot_is_expressible_and_fits_a_read_land_only_when_turned():
    """A read land 4 x 2 with the slot sides exchanged: the two readings agree once the axes are exchanged."""
    params = _run(_main(_slot_pads(pad_length_mm=4.0, pad_width_mm=2.0, shape="oval",
                                   value_sources={"drill_mm": "read", "pad_size": "read"})))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    assert (pin["drill_mm"], pin["drill_length_mm"], pin["pad_length_mm"], pin["pad_width_mm"]) == (
        3.0, 1.0, 4.0, 2.0)
    # The two sides were read, their axes are the program's (V1-3): never «read», and said.
    assert pin["sources"]["drill_mm"] == "derived" and "lato lungo sul lato lungo" in params.notes


def test_a_read_land_that_no_orientation_of_the_slot_fits_is_a_conflict_naming_both():
    params = _run(_main(_slot_pads(pad_length_mm=2.0, pad_width_mm=2.0,
                                   value_sources={"drill_mm": "read", "pad_size": "read"})))
    assert _assert_invariant(params) == "unread"
    assert params.unread_pads == ["1", "2"]
    assert "2.00 mm" in params.pin_map_conflict and "1.00 x 3.00 mm" in params.pin_map_conflict


@pytest.mark.parametrize("lead", [{"shape": "round", "diameter_mm": 1.0},
                                  {"shape": "flat", "width_mm": 2.8, "thickness_mm": 0.5}])
def test_a_read_terminal_beside_a_read_land_too_small_is_a_conflict_never_an_exception(lead):
    """F6-1 B: the program derives a hole the read land cannot hold; both readings are named."""
    pads = [_pin(n, x, drill_mm="DEL", pad_length_mm=1.2, pad_width_mm=1.2, lead=dict(lead),
                 value_sources={"lead": "read", "pad_size": "read"}) for n, x in (("1", 0.0), ("2", 5.08))]
    params = _run(_main(pads))
    assert _assert_invariant(params) == "unread"
    hole = tht_estimates.estimated_hole(lead)
    assert "1.20 mm" in params.pin_map_conflict and f"{hole:.2f} mm" in params.pin_map_conflict
    # The read land is kept as it was read in the dropped geometry.
    assert _by_number(params)["1"]["pad_length_mm"] == 1.2
    # A chosen pad layout gives positions, never a land: the pads stay dropped.
    assert params.unread_pad_geometry["unsettled_pads"] == ["1", "2"]


def test_the_same_terminal_beside_an_estimated_land_gets_a_derived_land():
    """Negative twin: the land is the model's guess, so the program's rule replaces it."""
    lead = {"shape": "round", "diameter_mm": 1.0}
    pads = [_pin(n, x, drill_mm="DEL", pad_length_mm=1.2, pad_width_mm=1.2, lead=dict(lead),
                 value_sources={"lead": "read", "pad_size": "estimated"}) for n, x in (("1", 0.0), ("2", 5.08))]
    params = _run(_main(pads))
    assert _assert_invariant(params) == "footprint"
    estimate = tht_estimates.estimated_round_pad(lead)
    pin = _by_number(params)["1"]
    assert (pin["drill_mm"], pin["pad_length_mm"]) == (estimate.drill_mm, estimate.pad_diameter_mm)


def test_a_read_hole_as_large_as_its_read_land_is_a_conflict():
    pads = [_pin(n, x, drill_mm=1.3, pad_length_mm=1.3, pad_width_mm=1.3, hole_callout="Ø1.3",
                 value_sources={"drill_mm": "read", "pad_size": "read"}) for n, x in (("1", 0.0), ("2", 5.08))]
    params = _run(_main(pads))
    assert _assert_invariant(params) == "unread"
    assert "read" in params.pin_map_conflict


# ---------------------------------------------------------------------------
# F7-2: nothing changes a pad after its validation
# ---------------------------------------------------------------------------

def _header(land_read, pitch=1.27, **pin_extra):
    """A 1 x 4 header: read hole 0.5, land 1.0 read or not, READ part lead 0.6 (the hole must be lifted)."""
    sources = {"drill_mm": "read", "lead_diameter_mm": "read"}
    if land_read:
        sources["pad_diameter_mm"] = "read"
    pins = [_pin(str(i + 1), i * pitch, pad_length_mm=1.0, pad_width_mm=1.0, drill_mm=0.5, **pin_extra)
            for i in range(4)]
    return _main(pins, lead_diameter_mm=0.6, value_sources=sources)


def test_a_lifted_hole_inside_a_read_land_validates_and_the_land_is_not_widened():
    params = _run(_header(land_read=True))
    assert _assert_invariant(params) == "footprint"
    for pin in params.package_geometry["pins"]:
        assert pin["drill_mm"] == tht_estimates.estimated_hole({"shape": "round", "diameter_mm": 0.6})
        assert pin["pad_length_mm"] == 1.0 and pin["sources"]["pad_size"] == "read"
        assert pin["sources"]["drill_mm"] == "estimated"
    # The thin ring a read land leaves around a derived hole is said, not hidden.
    assert "meno di 0.15 mm di corona" in params.notes


def test_a_lifted_hole_whose_derived_land_has_no_room_at_the_pitch_is_a_conflict():
    """The land is not read: the program would need 0.85 + 2 x 0.15 mm of pad and 0.2 mm of gap in 1.27 mm."""
    params = _run(_header(land_read=False))
    assert _assert_invariant(params) == "unread"
    assert "non c'e' posto" in params.pin_map_conflict and "0.85 mm" in params.pin_map_conflict


def test_the_same_lifted_hole_at_a_wider_pitch_gets_its_derived_land():
    """Negative twin of the conflict: only the pitch differs, and now the derived land fits."""
    params = _run(_header(land_read=False, pitch=2.54))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    assert pin["pad_length_mm"] == tht_estimates.estimated_pad_diameter(pin["drill_mm"])
    assert _smallest_gap(params) >= MIN_PAD_GAP_MM - 1e-6


def test_a_per_pad_terminal_that_lifts_a_disagreeing_hole_never_leaves_overlapping_copper():
    data = _header(land_read=False, lead={"shape": "round", "diameter_mm": 0.6},
                   value_sources={"drill_mm": "read", "pad_size": "estimated", "lead": "read"})
    data["_dedicated_holes"] = {str(i + 1): {"drill_mm": 0.7, "drill_length_mm": None, "rounding_mm": 0.05}
                                for i in range(4)}
    params = _run(data)
    # Either outcome of the invariant is acceptable; overlapping copper in a «footprint» is not.
    assert _assert_invariant(params) == "unread"


# ---------------------------------------------------------------------------
# F7-4, F7-6: a reduced land keeps its ring; no room is a conflict; the room is shared symmetrically
# ---------------------------------------------------------------------------

def _leaded(pitch, lead, model_land, count=3):
    """`count` pads at `pitch` with no hole, a READ part lead and the model's own land."""
    pins = [_pin(str(i + 1), i * pitch, pad_length_mm=model_land, pad_width_mm=model_land, drill_mm="DEL")
            for i in range(count)]
    return _main(pins, lead_diameter_mm=lead, value_sources={"lead_diameter_mm": "read"})


def test_a_reduced_land_keeps_the_minimum_ring_and_the_gap():
    """Lead 0.4 -> hole 0.65, derived land 1.45 at 1.27 mm; the model's 1.3 overlaps too: reduced."""
    params = _run(_leaded(1.27, 0.4, 1.3, count=4))
    assert _assert_invariant(params) == "footprint"
    for pin in params.package_geometry["pins"]:
        assert _ring(pin) >= RING - 1e-9
        assert pin["sources"]["pad_size"] == "estimated"
    assert _smallest_gap(params) >= MIN_PAD_GAP_MM - 1e-6
    assert "ridotta per lasciare" in params.notes


@pytest.mark.parametrize("model_land", [0.9, 0.8, 0.7])
def test_no_room_for_the_ring_at_the_pitch_is_a_conflict_never_a_thinner_ring(model_land):
    """Lead 0.5 -> hole 0.75 at 1.0 mm: 0.75 + 0.3 of ring + 0.2 of gap do not fit, whatever the model's land.

    The model's land smaller than the derived hole (0.7) used to come back
    and raise; the larger ones used to pass with a 0.025 mm ring.
    """
    params = _run(_leaded(1.0, 0.5, model_land))
    assert _assert_invariant(params) == "unread"
    assert "non c'e' posto" in params.pin_map_conflict and "0.75 mm" in params.pin_map_conflict
    assert params.unread_pads == ["1", "2", "3"]


def test_the_models_land_comes_back_only_when_it_keeps_the_ring():
    """Negative twin: at 1.27 mm the model's 1.0 mm land holds the 0.65 hole with its ring and clears."""
    params = _run(_leaded(1.27, 0.4, 1.0, count=4))
    assert _assert_invariant(params) == "footprint"
    assert all(pin["pad_length_mm"] == 1.0 for pin in params.package_geometry["pins"])
    assert "tenuta quella della risposta" in params.notes


def _two_leads(order):
    """Two pads 2.0 mm apart with READ leads Ø1.5 and Ø0.4, listed in `order`."""
    pads = {"big": _pin("1", 0.0, drill_mm="DEL", lead={"shape": "round", "diameter_mm": 1.5},
                        value_sources={"lead": "read"}),
            "small": _pin("2", 2.0, drill_mm="DEL", lead={"shape": "round", "diameter_mm": 0.4},
                          value_sources={"lead": "read"})}
    return _run(_main([pads[name] for name in order]))


def test_two_derived_lands_share_the_room_by_the_same_ring_whichever_comes_first():
    first, second = _two_leads(("big", "small")), _two_leads(("small", "big"))
    assert _assert_invariant(first) == "footprint" and _assert_invariant(second) == "footprint"
    rings = [_ring(pin) for pin in first.package_geometry["pins"]]
    # The same copper around each hole, not all the room to one pad.
    assert rings[0] == pytest.approx(rings[1], abs=2e-6) and rings[0] >= RING - 1e-9
    assert _smallest_gap(first) >= MIN_PAD_GAP_MM - 1e-6
    for number in ("1", "2"):
        assert _by_number(first)[number]["pad_length_mm"] == _by_number(second)[number]["pad_length_mm"]


def test_a_land_is_named_as_reduced_only_when_it_was():
    """Every pad of the «ridotta» note is smaller than the land the rule derives, by more than KiCad's 1 nm."""
    params = _two_leads(("big", "small"))
    for pin in params.package_geometry["pins"]:
        derived = tht_estimates.estimated_pad_diameter(pin["drill_mm"])
        named = f"{pin['number']} (" in next(line for line in params.notes.splitlines() if "ridotta" in line)
        assert named == (derived - pin["pad_length_mm"] > 1e-6)
    # Negative twin: far apart, nothing is reduced and nothing is named.
    pads = [_pin("1", 0.0, drill_mm="DEL", lead={"shape": "round", "diameter_mm": 1.5},
                 value_sources={"lead": "read"}),
            _pin("2", 6.0, drill_mm="DEL", lead={"shape": "round", "diameter_mm": 0.4},
                 value_sources={"lead": "read"})]
    assert "ridotta" not in _run(_main(pads)).notes


# ---------------------------------------------------------------------------
# F7-7: a read hole the unread lead cannot pass is no longer «read», on explicit pads too
# ---------------------------------------------------------------------------

def test_a_read_hole_an_unread_part_lead_cannot_pass_keeps_its_value_and_loses_read():
    pads = [_pin("1", 0.0, drill_mm=0.9), _pin("2", 2.54, drill_mm=0.9)]
    params = _run(_main(pads, value_sources={"drill_mm": "read"}, lead_diameter_mm=1.0))
    assert _assert_invariant(params) == "footprint"
    for pin in params.package_geometry["pins"]:
        assert pin["drill_mm"] == 0.9 and pin["sources"]["drill_mm"] == "estimated"
    assert "fori non modificati" in params.notes


def test_a_read_hole_an_unread_pad_lead_cannot_pass_keeps_its_value_and_loses_read():
    pads = [_pin("1", 0.0, drill_mm=0.9, lead={"shape": "round", "diameter_mm": 1.0},
                 value_sources={"drill_mm": "read"}),
            _pin("2", 2.54, drill_mm=0.9, value_sources={"drill_mm": "read"})]
    params = _run(_main(pads))
    pins = _by_number(params)
    assert pins["1"]["drill_mm"] == 0.9 and pins["1"]["sources"]["drill_mm"] == "estimated"
    # Negative twin in the same answer: the pad without that terminal keeps its read hole.
    assert pins["2"]["sources"]["drill_mm"] == "read"


def test_a_read_hole_the_lead_passes_stays_read():
    pads = [_pin("1", 0.0, drill_mm=0.9), _pin("2", 2.54, drill_mm=0.9)]
    params = _run(_main(pads, value_sources={"drill_mm": "read"}, lead_diameter_mm=0.6))
    assert all(pin["sources"]["drill_mm"] == "read" for pin in params.package_geometry["pins"])


# ---------------------------------------------------------------------------
# F6-3, F7-8, F6-6: what a printed callout states
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, value, factor, expected", [
    # A count of holes is not a size: the value is taken as transcribed, with its own digits.
    ("2-Ø1.6", 1.5, 1.0, (1.5, 0.05)),
    ("4-1.0", 3.5, 1.0, (3.5, 0.05)),
    ("2-0.8", 1.3, 1.0, (1.3, 0.05)),
    ("Ø1.3 (5x)", 5, 1.0, (5.0, 0.05)),
    ("2x Ø0.8", 2, 1.0, (2.0, 0.05)),
    # ... while the size beside the count is read as printed.
    ("2-0.8x2.0", 2.0, 1.0, (2.0, 0.05)),
    ("5-Ø1,3", 1.3, 1.0, (1.3, 0.05)),
    # An integer callout is a drill size, not a quote rounded to the millimetre.
    ("Ø1", 1, 1.0, (1.0, 0.05)),
    ("Ø1", 1.4, 1.0, (1.4, 0.05)),
    # A range between two sizes names no nominal; a tolerance does, on its own side only.
    ("1.0-1.2", 1.1, 1.0, (1.1, 0.05)),
    ("Ø1.3+0.1", 1.4, 1.0, (1.3, 0.05)),
    ("Ø1.3+0.1", 1.2, 1.0, (1.2, 0.05)),
    ("Ø1.3-0.1", 1.2, 1.0, (1.3, 0.05)),
    ("Ø1.3±0.1", 1.2, 1.0, (1.3, 0.05)),
    # A decimal without its leading zero, the inch habit.
    (".040", 0.04, 25.4, (1.016, 0.0127)),
    (".040 ±.003", 0.043, 25.4, (1.016, 0.0127)),
])
def test_what_a_printed_hole_callout_states(text, value, factor, expected):
    got = ext._printed_hole_value(text, value, factor)
    assert got == pytest.approx(expected, abs=1e-9)


def test_an_integer_callout_does_not_make_two_different_holes_agree():
    hole = {"drill_mm": 1.4, "drill_length_mm": None, "rounding_mm": 0.05}
    _value, rounding = ext._printed_hole_value("Ø1", 1, 1.0)
    assert not ext._same_printed_hole(1.0, None, hole, rounding)
    # A margin that did not come from a printed text is capped all the same.
    assert not ext._same_printed_hole(1.0, None, hole, 0.5)
    # Negative twin: the same hole spelt in inches and in millimetres still agrees.
    _inch, inch_rounding = ext._printed_hole_value(".040", 0.04, 25.4)
    assert ext._same_printed_hole(1.016, None, {"drill_mm": 1.02, "rounding_mm": 0.005}, inch_rounding)


def _two_readings(main_drill, callout, dedicated_drill, dedicated_text):
    """Two pads whose printed hole the main answer and the dedicated reading both transcribed."""
    pads = [_pin(n, x, drill_mm=main_drill, pad_length_mm=3.0, pad_width_mm=3.0, hole_callout=callout,
                 value_sources={"drill_mm": "read"}) for n, x in (("1", 0.0), ("2", 5.08))]
    data = _main(pads)
    data["_dedicated_holes"] = ext.printed_holes(
        [{"pads": "1,2", "diameter": dedicated_drill, "text": dedicated_text}], 1.0)
    return _run(data)


def test_a_count_prefix_does_not_make_two_disagreeing_readings_read():
    """Main 1.5 beside «2-Ø1.6», dedicated 1.9: the count 2 used to «state» 1.5 within half a millimetre."""
    params = _two_readings(1.5, "2-Ø1.6", 1.9, "2-Ø1.9")
    assert all(pin["sources"]["drill_mm"] == "estimated" for pin in params.package_geometry["pins"])
    assert "letto diversamente dalle due letture" in params.notes


def test_the_same_callout_read_twice_with_its_count_is_read():
    params = _two_readings(1.6, "2-Ø1.6", 1.6, "2-Ø1.6")
    assert all(pin["sources"]["drill_mm"] == "read" for pin in params.package_geometry["pins"])


# ---------------------------------------------------------------------------
# F6-4: prompt and schema of the single-part answer say one thing
# ---------------------------------------------------------------------------

def _single(**extra):
    """A single-part answer without pads or grid; `extra` adds them."""
    answer = {"drawing_unit": "mm", "contacts_view": "top", "confidence": "high",
              "printed_vertical_quotes": [], "body_length_mm": 10.0, "body_width_mm": 5.0}
    answer.update(extra)
    return answer


def test_a_grid_may_omit_an_unprinted_hole_and_the_part_lead_gives_it():
    grid = ext._single_component_tool()["function"]["parameters"]["properties"]["grid"]
    assert "drill_mm" not in grid["required"] and "drill_mm" in grid["properties"]
    answer = _single(grid={"rows": 1, "cols": 4, "pitch_mm": 2.54, "pad_diameter_mm": 1.7},
                     lead_diameter_mm=0.64, value_sources={"lead_diameter_mm": "read"})
    params = ext._params_from_tool_arguments(ext.single_answer_to_data(answer))
    assert _assert_invariant(params) == "footprint"
    estimate = tht_estimates.estimated_round_pad({"shape": "round", "diameter_mm": 0.64})
    assert {pin["drill_mm"] for pin in params.package_geometry["pins"]} == {estimate.drill_mm}
    assert all(pin["sources"]["drill_mm"] == "derived" for pin in params.package_geometry["pins"])


def test_a_grid_without_hole_and_without_lead_is_unread_never_invented():
    """Negative twin: nothing to derive the hole from."""
    answer = _single(grid={"rows": 1, "cols": 4, "pitch_mm": 2.54, "pad_diameter_mm": 1.7})
    params = ext._params_from_tool_arguments(ext.single_answer_to_data(answer))
    assert _assert_invariant(params) == "unread"
    assert "non stampato e terminale non dichiarato" in params.pin_map_conflict


def test_the_single_prompt_no_longer_asks_and_forbids_the_hole():
    prompt = ext.DEFAULT_SINGLE_SYSTEM_PROMPT
    # The contradiction of 2026-11-10: «thru_hole con drill_mm» and, later, «ometti drill_mm».
    assert "thru_hole con drill_mm" not in prompt and "ometti drill_mm" in prompt
    # The land is said once: always given, printed or an estimate the program replaces.
    assert prompt.count("PIAZZOLA:") == 1 and "piazzola consigliata" not in prompt
    # The slot has axes, and the grid follows the pads' rule.
    assert "asola orizzontale" in prompt and "grid.drill_mm solo se" in prompt
    assert ("a8ec8c4c0e846a73fa8f74f93eb2ac111d0eebb093c5d5d91484013d19a9f5ef"
            in ext.SUPERSEDED_SINGLE_PROMPT_DIGESTS)
    assert component_types.text_digest(prompt) not in ext.SUPERSEDED_SINGLE_PROMPT_DIGESTS


def test_the_changed_tools_still_convert_to_strict_mode():
    assert strict_tool(ext._single_component_tool()) is not None
    assert strict_tool(ext._openai_tool()) is not None


def test_the_slot_axes_are_stated_in_the_shared_pad_schema():
    text = ext._PAD_TERMINAL_PROPERTIES["drill_length_mm"]["description"]
    assert "lungo Y" in text and "lungo X" in text


# ---------------------------------------------------------------------------
# F6-7: an empty lead is absent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("empty", [{}, {"shape": None, "diameter_mm": None, "width_mm": None,
                                        "thickness_mm": None}])
def test_an_empty_or_all_null_lead_is_absent_not_invalid(empty):
    pads = [_pin("1", 0.0, lead=dict(empty)), _pin("2", 2.54, lead=dict(empty))]
    params = _run(_main(pads))
    assert _assert_invariant(params) == "footprint"
    assert "terminale dichiarato non valido" not in params.notes


def test_a_lead_with_a_shape_and_no_size_is_invalid_and_said():
    """Negative twin: something was declared, and it is not a lead."""
    pads = [_pin("1", 0.0, lead={"shape": "round", "diameter_mm": None}), _pin("2", 2.54)]
    assert "terminale dichiarato non valido" in _run(_main(pads)).notes


# ---------------------------------------------------------------------------
# A dropped geometry and the answer to the pad-layout question
# ---------------------------------------------------------------------------

def _with_layout_question(params, positions):
    """`params` carrying one open pad-layout question whose only option places the pads at `positions`."""
    params = copy.deepcopy(params)
    params.questions = [{"key": "pad_layout:x", "field": "pad_layout", "options": [{"value": "main"}]}]
    params.candidates = {"pad_layout:x": {"main": {
        "source": "main", "origin": "pin1",
        "pins": [{"number": number, "x_mm": x, "y_mm": 0.0} for number, x in positions.items()]}}}
    return ext.apply_answers(params, {"pad_layout:x": "main"})


def test_overlapping_pads_become_a_footprint_again_once_a_chosen_layout_separates_them():
    params = _run(_main([_pin("1", 0.0), _pin("2", 0.5)]))
    assert _assert_invariant(params) == "unread" and "pad sovrapposti" in params.pin_map_conflict
    answered = _with_layout_question(params, {"1": 0.0, "2": 2.54})
    assert _assert_invariant(answered) == "footprint" and answered.pin_map_conflict is None


def test_a_land_that_cannot_hold_its_hole_is_not_settled_by_a_chosen_layout():
    """Negative twin: positions do not change a read land or a read terminal."""
    pads = [_pin(n, x, drill_mm="DEL", pad_length_mm=1.2, pad_width_mm=1.2,
                 lead={"shape": "round", "diameter_mm": 1.0},
                 value_sources={"lead": "read", "pad_size": "read"}) for n, x in (("1", 0.0), ("2", 5.08))]
    answered = _with_layout_question(_run(_main(pads)), {"1": 0.0, "2": 7.62})
    assert answered.package_geometry is None and answered.unread_pads == ["1", "2"]
    assert answered.questions


# ---------------------------------------------------------------------------
# The invariant, as a property over pitch x lead x land x provenance
# ---------------------------------------------------------------------------

_PITCHES = (0.9, 1.0, 1.27, 2.0, 2.54, 5.08)
_LEADS = (None, {"shape": "round", "diameter_mm": 0.4}, {"shape": "round", "diameter_mm": 0.64},
          {"shape": "round", "diameter_mm": 1.5}, {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4},
          {"shape": "flat", "width_mm": 4.8, "thickness_mm": 0.8})
#: The last land has NO shape field (review round 5, V1-7).
_LANDS = ((0.7, 0.7, "circle"), (1.0, 1.0, "circle"), (1.6, 1.6, "circle"), (1.6, 2.4, "oval"),
          (2.4, 1.6, "rect"), (1.6, 1.6, None))
#: Round holes, a slot on each axis (V1-3), a zero hole and a zero slot side (V1-6).
_HOLES = (None, 0, 0.5, 0.9, (0.6, 1.4), (1.4, 0.6), (0.9, 0))
_WORDS = ("read", "estimated")
#: Fields the pad rules never read, in shapes outside any contract (V1-2,
#: V1-10): carried by the middle pad of every combination.
_UNREAD_FIELDS = {"layers": ["F.Cu", "B.Cu"], "net": {"name": "GND"}, "roundrect_ratio": float("nan"),
                  "name": ["GND"], "hole_callout": ["3-Ø1.0"], "comment": "plain text"}


def _property_cases():
    """Every combination of the matrix as (pitch, lead, land, hole, hole word, land word, lead word, lead level)."""
    for pitch, lead, land, hole in itertools.product(_PITCHES, _LEADS, _LANDS, _HOLES):
        for hole_word, land_word, lead_word in itertools.product(_WORDS, repeat=3):
            for level in ("pad", "part"):
                if level == "part" and (lead is None or lead["shape"] != "round"):
                    continue
                if lead is None and (lead_word != "read" or level != "pad"):
                    continue
                if (hole is None or hole == 0) and hole_word != "read":
                    continue
                yield pitch, lead, land, hole, hole_word, land_word, lead_word, level


def _property_answer(pitch, lead, land, hole, hole_word, land_word, lead_word, level, unplaced=()):
    """Three pads in a row built from one combination of the matrix; the pads in `unplaced` have no position."""
    sources = {"pad_size": land_word}
    extra = {"pad_length_mm": land[0], "pad_width_mm": land[1],
             "shape": "DEL" if land[2] is None else land[2], "drill_mm": "DEL"}
    if land[2] is None:
        # A word about a shape nobody gave: it must never come back as «read».
        sources["shape"] = "read"
    if hole is not None:
        sources["drill_mm"] = hole_word
        if isinstance(hole, tuple):
            extra["drill_mm"], extra["drill_length_mm"] = hole
        else:
            extra["drill_mm"] = hole
    top = {}
    if lead is not None and level == "pad":
        extra["lead"] = dict(lead)
        sources["lead"] = lead_word
    elif lead is not None:
        top = {"lead_diameter_mm": lead["diameter_mm"], "value_sources": {"lead_diameter_mm": lead_word}}
    pins = [_pin(str(i + 1), i * pitch, value_sources=dict(sources), **copy.deepcopy(extra)) for i in range(3)]
    pins[1].update(copy.deepcopy(_UNREAD_FIELDS))
    for pin in pins:
        if pin["number"] in unplaced:
            pin["x_mm"] = pin["y_mm"] = None
    return _main(pins, **top)


def _assert_written_values(case, params):
    """What must hold of every result, footprint or not: storable values and truthful provenance."""
    _pitch, _lead, land, hole, _hole_word, _land_word, _lead_word, _level = case
    # Nothing a project file cannot store (V1-10), whatever the unread fields held (V1-2).
    json.dumps(dataclasses.asdict(params), allow_nan=False)
    given = hole if isinstance(hole, tuple) else (hole, None)
    given = tuple(None if value == 0 else value for value in given)
    for pin in _by_number(params).values():
        sources = pin.get("sources") or {}
        if land[2] is None:
            # A shape nobody gave is the program's choice, written and never «read» (V1-7).
            assert pin.get("shape") and sources.get("shape") != "read", (case, pin)
        if (pin.get("drill_mm"), pin.get("drill_length_mm")) != given:
            # A hole the program derived, lifted or turned is never «read» (V1-3, V1-6).
            assert sources.get("drill_mm") != "read", (case, pin)


def _assert_program_lands(case, params):
    """A footprint's lands the program wrote keep the minimum ring and the gap."""
    _pitch, lead, _land, hole, hole_word, land_word, _lead_word, _level = case
    if land_word == "read" or (lead is None and hole_word == "estimated"):
        # A read land is never changed, and a hole and a land that are
        # both the model's guesses stay as given: no ring is promised.
        return
    # Every land here was written by the program (derived, reduced or
    # the model's taken back): it keeps the minimum ring and the gap.
    for pin in params.package_geometry["pins"]:
        assert _ring(pin) >= RING - 1e-6, (case, pin)
    assert _smallest_gap(params) >= MIN_PAD_GAP_MM - 1e-5, case


def test_every_combination_ends_as_a_valid_footprint_or_as_unread_pads_with_a_block():
    """No combination raises; a footprint validates; a land the program wrote keeps its ring and the gap.

    Extended on 2026-11-11 (review round 5): fields the rules do not read in
    non-contract shapes, zero holes and zero slot sides, slots on both axes
    beside read and estimated lands, an absent shape -- and the same
    invariant after `apply_answers` restores a geometry dropped for one
    unread position (V1-4).
    """
    outcomes = {"footprint": 0, "unread": 0}
    restored = {"footprint": 0, "unread": 0}
    for case in _property_cases():
        try:
            params = _run(_property_answer(*case))
            dropped = _run(_property_answer(*case, unplaced=("3",)))
        except ExtractionError as error:  # pragma: no cover - the failure this test exists to catch
            pytest.fail(f"{case}: {error}")
        outcome = _assert_invariant(params)
        outcomes[outcome] += 1
        _assert_written_values(case, params)
        if outcome == "footprint":
            _assert_program_lands(case, params)
        # The same pads with pad 3 unread, then placed by a chosen layout:
        # the restored result obeys the same invariant, and a layout that
        # cannot be applied leaves the question open with the reason.
        assert _assert_invariant(dropped) == "unread" and "3" in dropped.unread_pads, case
        pitch = case[0]
        answered = _with_layout_question(dropped, {"1": 0.0, "2": pitch, "3": 2 * pitch})
        again = _assert_invariant(answered)
        restored[again] += 1
        _assert_written_values(case, answered)
        if again == "footprint":
            assert not answered.questions and answered.pin_map_conflict is None, case
            assert ext._DERIVED_LANDS_KEY not in answered.package_geometry, case
            _assert_program_lands(case, answered)
        else:
            assert answered.questions and answered.unread_pad_geometry == dropped.unread_pad_geometry, case
        # What the answer is does not depend on when pad 3 got its position.
        assert again == outcome, case
    assert restored["footprint"] > 100 and restored["unread"] > 100
    # The matrix really exercises both outcomes.
    assert outcomes["footprint"] > 100 and outcomes["unread"] > 100
