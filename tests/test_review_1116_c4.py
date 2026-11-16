"""@brief Review 2026-11-16, fixer C4: generators and core (findings R11-1 .. R11-9).

Each rule has a positive and a negative case that differ for a structural
reason: a size the program changed against one it kept or that was declared,
a pin 1 in the middle of a row against one at its end, a grid above the
declared bound against one at it, a collapsed drawing against a sound one.
"""
from __future__ import annotations

import math
import time

import pytest

from kicadgen.core import pad_limits
from kicadgen.core.footprint import (Footprint, FpCircle, Pad, drawing_graphics, drawing_is_degenerate,
                                     skipped_drawings_note)
from kicadgen.core.pad_limits import MAX_PAD_POSITIONS, validate_grid_counts
from kicadgen.core.silk import (PIN_ONE_NO_ROOM_NOTE, keepout_box, marker_points_at,
                                pin_one_marker_beside)
from kicadgen.generators import castellated_module as cm
from kicadgen.generators.castellated_module import (CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE,
                                                    CastellatedModuleParams, CastellatedSide,
                                                    build_castellated_module)
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.generators.symbol import SymbolError, SymbolPin, export_symbol
from kicadgen.generators.symbol_relay import contact_label
from kicadgen.services.footprint_checks import has_pin_one_marker


# ── R11-1: reduced and derived castellated sizes are recorded as estimates ──

def _module(pitch, **side):
    """A one-side half-hole module whose assumed pad width meets the pitch `pitch`."""
    return CastellatedModuleParams(
        name="M", body_length=20.0, body_width=30.0,
        sides=[CastellatedSide("left", 5, pitch, contact_style=CONTACT_HALF_HOLE, **side)])


@pytest.mark.parametrize("pitch", [1.0, 1.27, 1.5, 1.8])
def test_a_pad_width_reduced_to_the_pitch_is_named_with_the_written_value(pitch):
    """@brief Positive, for every pitch narrower than the assumed land: the description names the
    REDUCED width, the one written in the pads, and says it was reduced."""
    params = _module(pitch)
    fp = build_castellated_module(params)
    width = min(pad.size[1] for pad in fp.pads)
    assert width < float(cm.REFERENCE_PAD_WIDTH_MM)
    assert f"pad width {width:.2f}mm (reduced to fit the pitch)" in fp.description
    assert "assumed, not measured (estimate)" in fp.description


def test_a_declared_pad_width_is_not_named_as_an_estimate():
    """@brief Negative: the same pitch with a DECLARED width and half-hole that fit -> nothing
    reduced, and neither is in the description (a measure is not an estimate)."""
    params = _module(1.27, pad_width=0.8)
    params.hole_diameter = 0.5
    fp = build_castellated_module(params)
    assert "pad width" not in fp.description
    assert "reduced" not in fp.description


def test_the_fallback_through_hole_distance_is_named_and_an_explicit_one_is_not():
    """@brief Positive: a through-hole side with no row spacing nor offset -> the distance from the
    edge is the smallest that fits, named with its value; negative: the same side with a declared
    offset -> not named."""
    def through(offset):
        return CastellatedModuleParams(
            name="T", body_length=20.0, body_width=30.0, thru_hole_offset=offset,
            sides=[CastellatedSide("left", 3, 2.54, contact_style=CONTACT_THROUGH_HOLE)])
    estimated = through(0.0)
    inset = estimated.side_geometry(estimated.sides[0]).thru_inset
    description = build_castellated_module(estimated).description
    assert f"through-hole centre {inset:.2f}mm from the edge (smallest that fits)" in description
    assert "through-hole centre" not in build_castellated_module(through(2.0)).description


def test_estimated_sizes_list_only_changed_values():
    """@brief Positive: an assumed size reduced -> in `estimated_sizes`, not in `assumed_sizes`;
    negative: an assumed size that fits -> in `assumed_sizes`, not in `estimated_sizes`."""
    reduced = _module(1.27)
    kept = _module(5.0)
    # At 1.27 mm both the reference land (1.8 mm) and the reference half-hole
    # (1.7 mm) are wider than the pitch allows: both are reduced.
    changed = [kind for kind, _v, _s in reduced.estimated_sizes(reduced.sides[0])]
    assert changed == ["half_hole", "pad_width"]
    assert not set(changed) & {kind for kind, _v in reduced.assumed_sizes(reduced.sides[0])}
    assert kept.estimated_sizes(kept.sides[0]) == []
    assert {"half_hole", "pad_width"} <= {kind for kind, _v in kept.assumed_sizes(kept.sides[0])}


# ── R11-2: the pin-1 dot next to pin 1, never next to another pad, never a block ──

def _row_with_pin_one_at(index, count, pitch=1.27):
    """A left row of `count` half-holes whose pin 1 is the contact at `index` along the edge."""
    numbers = [str(n) for n in range(2, count + 1)]
    numbers.insert(index, "1")
    side = CastellatedSide("left", count, pitch, pin_numbers=numbers, contact_style=CONTACT_HALF_HOLE)
    return CastellatedModuleParams(name="R", body_length=20.0, body_width=count * pitch + 4.0, sides=[side])


@pytest.mark.parametrize("count", [41, 101, 7])
def test_a_pin_one_in_the_middle_of_a_row_gets_a_dot_beside_it(count):
    """@brief Positive: pin 1 in the middle of a tight row (no room between neighbours) -> the
    footprint is written and its dot points at pad 1 by the footprint check's own criterion."""
    params = _row_with_pin_one_at(count // 2, count)
    fp = build_castellated_module(params)
    dots = [g for g in fp.graphics if isinstance(g, FpCircle) and g.layer == "F.SilkS"]
    assert len(dots) == 1
    # Structural, not a distance a dot beside pad 2 would also meet: the dot's
    # centre is strictly nearer to pad 1's copper than to any other pad's,
    # with the generator's own copper (rounded boxes) and with the check's.
    placements = list(cm.pad_positions(params))
    owner = [cm._copper_outline(params, p) for p in placements if p.number == "1"]
    others = [cm._copper_outline(params, p) for p in placements if p.number != "1"]
    assert marker_points_at(tuple(dots[0].center), owner, others)
    assert has_pin_one_marker(fp)
    assert PIN_ONE_NO_ROOM_NOTE not in fp.description


def test_a_pin_one_at_the_end_of_a_row_keeps_its_historical_dot():
    """@brief Negative: pin 1 at the end of the row -> the dot is beside it along the edge, away
    from the row (the historical spot, unchanged), not moved outward."""
    fp = build_castellated_module(_row_with_pin_one_at(0, 7))
    dot = next(g for g in fp.graphics if isinstance(g, FpCircle) and g.layer == "F.SilkS")
    one = next(pad for pad in fp.pads if pad.number == "1")
    assert dot.center[0] == pytest.approx(one.at[0])
    assert dot.center[1] < one.at[1]
    assert has_pin_one_marker(fp)


def test_no_free_spot_leaves_the_dot_out_and_says_why(monkeypatch):
    """@brief When no spot next to pin 1 is free the footprint is still written, without a dot,
    and the description carries the note (wiring of the None outcome).

    Monkeypatched on purpose: with castellated edge lands the outward spot
    past pad 1's outer end lies outside the module, where no other pad's
    copper is, so a real module always offers it; the real-geometry «no
    room» case is the boxed-in pad of
    test_the_search_stops_where_the_dot_would_point_at_another_pad."""
    monkeypatch.setattr(cm, "pin_one_marker_beside", lambda *args, **kwargs: None)
    monkeypatch.setattr(cm, "pin_one_marker", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError()))
    fp = build_castellated_module(_row_with_pin_one_at(3, 7))
    assert not [g for g in fp.graphics if isinstance(g, FpCircle) and g.layer == "F.SilkS"]
    assert PIN_ONE_NO_ROOM_NOTE in fp.description


def test_the_search_stops_where_the_dot_would_point_at_another_pad():
    """@brief Positive: a pad boxed in by neighbours on every side -> None (no dot next to another
    pad); negative: the same pad with one free side -> a dot on that side, pointing at it."""
    owner = [(0.0, 0.0, 0.0, 0.0, 0.5)]
    around = [(dx, dy, dx, dy, 0.5) for dx, dy in ((1.3, 0), (-1.3, 0), (0, 1.3), (0, -1.3))]
    keepouts = [keepout_box(x0 - r, y0 - r, x1 + r, y1 + r, line_width=0.0)
                for x0, y0, x1, y1, r in owner + around]
    candidates = [((0.9, 0.0), (1.0, 0.0)), ((-0.9, 0.0), (-1.0, 0.0)),
                  ((0.0, 0.9), (0.0, 1.0)), ((0.0, -0.9), (0.0, -1.0))]
    assert pin_one_marker_beside(candidates, keepouts, owner, around) is None
    open_side = around[1:]
    open_keepouts = keepouts[:1] + keepouts[2:]
    dot = pin_one_marker_beside(candidates, open_keepouts, owner, open_side)
    assert dot is not None and dot.center[0] > 0
    assert marker_points_at(dot.center, owner, open_side)


# ── R11-3 / R11-9: bounded whole-number grids and pin-map positions ──

def test_a_grid_above_the_bound_is_refused_at_once_and_one_at_it_is_built():
    """@brief Positive: 1000 x 1000 and 10000 x 10000 -> ValueError in well under a second;
    negative: 100 x 100 (exactly MAX_PAD_POSITIONS) validates."""
    for size in (1000, 10000):
        started = time.perf_counter()
        with pytest.raises(ValueError):
            PinHeaderParams(rows=size, cols=size).validate()
        assert time.perf_counter() - started < 1.0
    side = int(math.isqrt(MAX_PAD_POSITIONS))
    PinHeaderParams(rows=side, cols=side).validate()
    assert side * side == MAX_PAD_POSITIONS


def test_a_non_integer_count_is_a_clear_value_error_and_an_integral_type_is_accepted():
    """@brief Positive: 2.5, 2.0, "2" and True rows -> ValueError (never a TypeError); negative: an
    int and a numpy integer -> accepted and normalised to int."""
    for rows in (2.5, 2.0, "2", True, float("nan")):
        with pytest.raises(ValueError):
            PinHeaderParams(rows=rows, cols=4).validate()
    params = PinHeaderParams(rows=2, cols=4)
    params.validate()
    numpy = pytest.importorskip("numpy")
    params = PinHeaderParams(rows=numpy.int64(2), cols=numpy.int32(3))
    params.validate()
    assert type(params.rows) is int and type(params.cols) is int
    assert len(build_pin_header(params).pads) == 6


def test_validate_grid_counts_is_the_shared_rule():
    """@brief The exported rule other readers reuse: the product is bounded, not each axis."""
    assert validate_grid_counts(1, MAX_PAD_POSITIONS) == (1, MAX_PAD_POSITIONS)
    with pytest.raises(ValueError):
        validate_grid_counts(1, MAX_PAD_POSITIONS + 1)
    with pytest.raises(ValueError):
        validate_grid_counts(0, 3)
    assert pad_limits.is_integer_count(3) and not pad_limits.is_integer_count(3.0)


def test_a_pin_map_position_between_grid_lines_is_refused():
    """@brief Positive: a row of 0.5 -> ValueError; negative: whole positions given as a list
    (JSON) or a tuple -> accepted, stored as int tuples."""
    with pytest.raises(ValueError):
        PinHeaderParams(rows=2, cols=2, row_spacing=2.54, pin_map={"1": (0.5, 0), "2": (1, 1)}).validate()
    with pytest.raises(ValueError):
        PinHeaderParams(rows=2, cols=2, row_spacing=2.54, pin_map={"1": (0, 1.0), "2": (1, 1)}).validate()
    params = PinHeaderParams(rows=2, cols=2, row_spacing=2.54, pin_map={"1": [0, 0], "2": (1, 1)})
    params.validate()
    assert params.pin_map == {"1": (0, 0), "2": (1, 1)}


# ── R11-4: castellated bounds before any contact is enumerated ──

def test_castellated_bounds_are_checked_before_the_numbers_are_built(monkeypatch):
    """@brief Positive: too many contacts, or a body beyond KiCad's range -> refused WITHOUT
    enumerating a pin number; negative: a sound module enumerates them and validates."""
    calls = []
    real = CastellatedSide.numbers

    def counted(self):
        calls.append(self.count)
        return real(self)
    monkeypatch.setattr(CastellatedSide, "numbers", counted)
    many = CastellatedModuleParams(name="M", body_length=20.0, body_width=1500.0,
                                   sides=[CastellatedSide("left", MAX_PAD_POSITIONS + 1, 0.1,
                                                          pad_width=0.05, contact_style=CONTACT_HALF_HOLE)])
    with pytest.raises(ValueError):
        many.validate()
    huge = CastellatedModuleParams(name="M", body_length=20.0, body_width=1e7,
                                   sides=[CastellatedSide("left", 3, 2.54)])
    with pytest.raises(ValueError):
        huge.validate()
    assert calls == []
    CastellatedModuleParams(name="M", body_length=20.0, body_width=20.0,
                            sides=[CastellatedSide("left", 3, 2.54)]).validate()
    assert calls


# ── R11-6: relay contact labels that are not numbers ──

def test_a_nan_or_infinite_contact_label_is_no_label():
    """@brief Positive: NaN and infinities -> None (no crash); negative: 2.0, 2 and a huge int ->
    their text."""
    for value in (float("nan"), float("inf"), float("-inf")):
        assert contact_label({"contact": value}) is None
    assert contact_label({"contact": 2.0}) == "2"
    assert contact_label({"contact": 2}) == "2"
    assert contact_label({"contact": 10 ** 400}) == str(10 ** 400)
    assert contact_label({"contact": 2.5}) is None


# ── R11-7: «%» refused by the file writers ──

def test_a_symbol_named_with_percent_is_never_written(tmp_path):
    """@brief Positive: «x%n» -> SymbolError, no file; negative: «x_n» -> written."""
    pins = [SymbolPin("1", "A", "passive", "left"), SymbolPin("2", "B", "passive", "right")]
    target = tmp_path / "x.kicad_sym"
    with pytest.raises(SymbolError):
        export_symbol(target, "x%n", pins)
    assert not target.exists()
    assert export_symbol(target, "x_n", pins).exists()


def test_a_footprint_named_with_percent_is_never_saved(tmp_path):
    """@brief Positive: a hand-built footprint «x%n» -> ValueError, no file; negative: «x_n» saved."""
    pad = Pad(number="1", pad_type="smd", shape="rect", at=(0, 0), size=(1, 1),
              layers=["F.Cu", "F.Paste", "F.Mask"])
    bad = Footprint(name="x%n").add_pad(pad)
    with pytest.raises(ValueError):
        bad.save(str(tmp_path / "x%n.kicad_mod"))
    assert not (tmp_path / "x%n.kicad_mod").exists()
    good = Footprint(name="x_n").add_pad(pad)
    good.save(str(tmp_path / "x_n.kicad_mod"))
    assert (tmp_path / "x_n.kicad_mod").exists()


# ── R11-8: collapsed drawings are left out, never a blocked write ──

SQUARE = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]


def test_a_collapsed_drawing_is_skipped_and_reported_while_a_sound_one_is_written():
    """@brief Positive: a polygon edited down to two distinct vertices and a dimension whose ends
    met -> left out, indices reported, the footprint renders; negative: the same polygon with its
    four corners and a dimension with distinct ends -> written."""
    collapsed_polygon = {"kind": "polygon", "layer": "F.SilkS", "width": 0.12,
                         "points": [[0.0, 0.0], [2.0, 0.0], [2.0, 0.0], [0.0, 0.0]]}
    collapsed_dimension = {"kind": "dimension", "layer": "F.Fab", "width": 0.1,
                           "points": [[1.0, 1.0], [1.0, 1.0]]}
    sound_polygon = {"kind": "polygon", "layer": "F.SilkS", "width": 0.12, "points": SQUARE}
    sound_dimension = {"kind": "dimension", "layer": "F.Fab", "width": 0.1,
                       "points": [[0.0, 0.0], [3.0, 0.0]]}
    records = [collapsed_polygon, sound_polygon, collapsed_dimension, sound_dimension]
    assert [drawing_is_degenerate(r) for r in records] == [True, False, True, False]
    skipped = []
    items = drawing_graphics(records, skipped=skipped)
    assert skipped == [0, 2]
    fp = build_pin_header(PinHeaderParams(rows=1, cols=2))
    for item in items:
        fp.add_graphic(item)
    text = fp.render()
    assert text.count("(fp_poly") == 1 and text.count("(dimension") == 1
    # Without the list the call is the old one: same items, nothing raised.
    assert len(drawing_graphics(records)) == len(items)


def test_a_collapsed_line_rectangle_and_circle_are_degenerate_and_sound_ones_are_not():
    """@brief Positive and negative twins for the other kinds the validator also refuses."""
    base = {"layer": "F.SilkS", "width": 0.12}
    assert drawing_is_degenerate({**base, "kind": "line", "points": [[1, 1], [1, 1]]})
    assert not drawing_is_degenerate({**base, "kind": "line", "points": [[1, 1], [2, 1]]})
    assert drawing_is_degenerate({**base, "kind": "rectangle", "points": [[0, 0], [0, 3]]})
    assert not drawing_is_degenerate({**base, "kind": "rectangle", "points": [[0, 0], [2, 3]]})
    assert drawing_is_degenerate({**base, "kind": "circle", "points": [[0, 0], [0, 0]]})
    assert not drawing_is_degenerate({**base, "kind": "circle", "points": [[0, 0], [1, 0]]})
    assert not drawing_is_degenerate({**base, "kind": "text", "points": [[0, 0]], "text": "A"})


def test_the_skipped_drawings_note():
    """@brief Nothing skipped -> no note; some skipped -> a sentence with the count."""
    assert skipped_drawings_note(0) == ""
    assert "2" in skipped_drawings_note(2)
