"""@brief Regression tests of the second 2026-11-03 adversarial review, generators and core.

Each fix has a positive case and a negative case that differ for a
structural reason (an emitted pin 1 or not, a collinear arc or a real one,
pads of one number or of two), never by a tuned number.
"""

import math
import random
import time

import pytest

from kicadgen.core.footprint import FpCircle, FpRect
from kicadgen.core.package_geometry import (PackageGeometry, PinSpec, copper_separation,
                                            package_from_mapping, pad_copper_shape)
from kicadgen.core.silk import PIN_ONE_UNMARKED_NOTE, marker_ink_box, pin_one
from kicadgen.generators import pin_header as pin_header_module
from kicadgen.generators.castellated_module import (CONTACT_HALF_HOLE, CastellatedModuleParams,
                                                    CastellatedSide, build_castellated_module)
from kicadgen.generators.host_mounting import HostLandGeometry, header_footprint_params
from kicadgen.generators.package import COURTYARD_MARGIN_MM, build_package
from kicadgen.generators.pin_header import (COURTYARD_MARGIN_MM as HEADER_MARGIN_MM, PinHeaderParams,
                                            build_pin_header, square_pad_pin)
from kicadgen.generators.symbol_glyphs import (parse_sexpr, primitives_of, shape_instance,
                                               symbol_graphics)


# -- helpers ------------------------------------------------------------------

def _courtyard(fp):
    """(x0, y0, x1, y1) of the F.CrtYd rectangle."""
    rect = next(g for g in fp.graphics if isinstance(g, FpRect) and g.layer == "F.CrtYd")
    return (min(rect.start[0], rect.end[0]), min(rect.start[1], rect.end[1]),
            max(rect.start[0], rect.end[0]), max(rect.start[1], rect.end[1]))


def _dots(fp):
    """Pin-1 dots: filled circles on the front silkscreen."""
    return [g for g in fp.graphics if isinstance(g, FpCircle) and g.layer == "F.SilkS"]


def _inside(box, outer):
    return (box[0] >= outer[0] - 1e-9 and box[1] >= outer[1] - 1e-9
            and box[2] <= outer[2] + 1e-9 and box[3] <= outer[3] + 1e-9)


def _pad_shape(fp, number):
    return next(pad.shape for pad in fp.pads if pad.number == number)


# -- N1: one legacy collinear arc never costs the other drawings --------------

def _shape_record(kind, points, ident):
    return {"id": ident, "key": f"shape:{kind}", "label": kind, "source": "shape",
            "x": 0.0, "y": 0.0, "rotation": 0, "mirror": False,
            "primitives": [{"kind": kind, "points": points, "radius": 0.0, "fill": "none",
                            "width": 0.254, "style": "default"}],
            "terminals": []}


def test_a_legacy_collinear_arc_is_converted_and_the_other_records_load():
    collinear = _shape_record("arc", [[0, 0], [1, 0], [2, 0]], "a")
    real_arc = _shape_record("arc", [[0, 0], [1, 1], [2, 0]], "b")
    line = _shape_record("polyline", [[0, 0], [3, 3]], "c")
    shared = [collinear, real_arc, line]
    rows = [{"number": "1", "symbol_graphics": shared}, {"number": "2", "symbol_graphics": shared}]
    notes = []
    loaded = symbol_graphics(rows, notes)
    assert [record["id"] for record in loaded] == ["a", "b", "c"]
    # The collinear arc becomes the polyline through the same three points.
    assert loaded[0]["primitives"][0]["kind"] == "polyline"
    assert loaded[0]["primitives"][0]["points"] == [[0, 0], [1, 0], [2, 0]]
    # A real arc is not touched (negative case: it has a centre).
    assert loaded[1] is real_arc and loaded[1]["primitives"][0]["kind"] == "arc"
    assert len(notes) == 1
    # The rows themselves are not rewritten by the load.
    assert shared[0]["primitives"][0]["kind"] == "arc"


def test_a_malformed_record_is_dropped_alone_and_reported():
    good = _shape_record("polyline", [[0, 0], [3, 3]], "good")
    bad = dict(_shape_record("polyline", [[0, 0], [1, 1]], "bad"), rotation=45)
    rows = [{"symbol_graphics": [bad, good]}]
    notes = []
    loaded = symbol_graphics(rows, notes)
    assert [record["id"] for record in loaded] == ["good"]
    assert len(notes) == 1


def test_ids_reach_every_row_at_the_original_position_after_a_drop():
    bad = dict(_shape_record("polyline", [[0, 0], [1, 1]], "bad"), rotation=45)
    legacy = _shape_record("polyline", [[0, 0], [3, 3]], "")
    first, second = [dict(bad), dict(legacy)], [dict(bad), dict(legacy)]
    loaded = symbol_graphics([{"symbol_graphics": first}, {"symbol_graphics": second}])
    assert len(loaded) == 1
    assert second[1]["id"] == loaded[0]["id"] and second[0]["id"] == "bad"


def test_new_placements_still_refuse_a_collinear_arc():
    assert shape_instance("arc", [(0, 0), (2, 0), (1, 0)]) is None
    assert shape_instance("arc", [(0, 0), (2, 0), (1, 1)]) is not None


# -- N10: library primitives a placement would refuse are skipped at load -----

def test_library_primitives_that_placement_refuses_are_skipped():
    unit = parse_sexpr('''(symbol "X_0_1"
      (arc (start 0 0) (mid 1 0) (end 2 0) (stroke (width 0.254) (type default)) (fill (type none)))
      (arc (start 0 0) (mid 1 1) (end 2 0) (stroke (width 0.254) (type default)) (fill (type none)))
      (polyline (pts (xy 0 0) (xy 1 1)) (stroke (width -0.1) (type default)) (fill (type none)))
      (polyline (pts (xy 0 0) (xy nan 1)) (stroke (width 0.2) (type default)) (fill (type none)))
      (polyline (pts (xy 0 0) (xy 2 2)) (stroke (width 0.2) (type default)) (fill (type none))))''')[0]
    kept = primitives_of(unit)
    assert [(p.kind, p.points) for p in kept] == [("arc", ((0.0, 0.0), (1.0, 1.0), (2.0, 0.0))),
                                                  ("polyline", ((0.0, 0.0), (2.0, 2.0)))]


# -- N2: overlap pre-filter gives the brute-force verdicts, fast --------------

def _brute_force(pins):
    """The former pairwise check (oracle): first overlapping (later, earlier) pair, or None."""
    shapes = []
    for pin in pins:
        shape = pad_copper_shape(pin)
        for other, other_shape in shapes:
            same = str(pin.number).strip() and pin.number == other.number
            if not same and copper_separation(shape, other_shape) <= 1e-6:
                return (other.number, pin.number)
        shapes.append((pin, shape))
    return None


@pytest.mark.parametrize("seed", range(40))
def test_overlap_verdicts_match_the_pairwise_scan(seed):
    rng = random.Random(seed)
    pins = []
    for index in range(rng.randint(2, 30)):
        shape = rng.choice(["circle", "rect", "oval", "roundrect"])
        length, width = rng.uniform(0.2, 2.0), rng.uniform(0.2, 2.0)
        pins.append(PinSpec(number=str(rng.randint(1, 40)) if rng.random() < 0.3 else str(index + 1),
                            x_mm=rng.uniform(-6, 6), y_mm=rng.uniform(-6, 6),
                            pad_length_mm=length, pad_width_mm=width, shape=shape,
                            rotation_deg=rng.choice([0, 30, 45, 90, rng.uniform(0, 360)])))
    expected = _brute_force(pins)
    geometry = PackageGeometry(name="Rand", family="custom", pins=pins)
    if expected is None:
        geometry.validate()
    else:
        with pytest.raises(ValueError) as caught:
            geometry.validate()
        assert f"{expected[0]} e {expected[1]}" in str(caught.value)


def test_a_400_pad_grid_validates_quickly():
    """Generous bound (1 s) for 400 pads; the pairwise scan took about 1-11 s."""
    pins = [PinSpec(number=f"{chr(65 + r)}{c + 1}", x_mm=c * 1.0, y_mm=r * 1.0,
                    pad_length_mm=0.5, pad_width_mm=0.5, shape="circle")
            for r in range(20) for c in range(20)]
    geometry = PackageGeometry(name="LGA400", family="custom", pins=pins)
    start = time.perf_counter()
    geometry.validate()
    assert time.perf_counter() - start < 1.0


# -- N9: pads sharing one number may overlap ----------------------------------

def test_pads_of_one_number_may_overlap_but_different_numbers_may_not():
    ep = PinSpec(number="EP", x_mm=0, y_mm=0, pad_length_mm=3, pad_width_mm=3, shape="rect")
    via = PinSpec(number="EP", x_mm=0.5, y_mm=0.5, pad_length_mm=0.6, pad_width_mm=0.6,
                  pad_type="thru_hole", shape="circle", drill_mm=0.3)
    PackageGeometry(name="QFN", family="qfn", pins=[ep, via]).validate()
    other = PinSpec(number="1", x_mm=0.5, y_mm=0.5, pad_length_mm=0.6, pad_width_mm=0.6,
                    pad_type="thru_hole", shape="circle", drill_mm=0.3)
    with pytest.raises(ValueError):
        PackageGeometry(name="QFN", family="qfn", pins=[ep, other]).validate()
    # Two copies of ONE pad (same size, each centre inside the other: a
    # double reading) are an error, not a pin (GA9) ...
    copy = PinSpec(number="3", x_mm=0, y_mm=0, pad_length_mm=1.2, pad_width_mm=0.8, shape="rect")
    with pytest.raises(ValueError):
        PackageGeometry(name="Q", family="custom",
                        pins=[copy, PinSpec(**{**copy.__dict__, "x_mm": 0.05})]).validate()
    # ... while two lands of one split exposed pad, touching side by side, are one pin.
    land = PinSpec(number="EP", x_mm=-0.5, y_mm=0, pad_length_mm=1.0, pad_width_mm=1.0, shape="rect")
    PackageGeometry(name="Q", family="custom",
                    pins=[land, PinSpec(**{**land.__dict__, "x_mm": 0.45})]).validate()
    # Unnumbered holes are not one pin: overlapping ones are still refused.
    hole = PinSpec(number="", x_mm=0, y_mm=0, pad_length_mm=1, pad_width_mm=1,
                   pad_type="np_thru_hole", shape="circle", drill_mm=0.8)
    with pytest.raises(ValueError):
        PackageGeometry(name="H", family="custom",
                        pins=[hole, PinSpec(**{**hole.__dict__, "x_mm": 0.2})]).validate()


# -- N5: numeric strings are refused, not carried to a later TypeError ---------

def _mapping(**changes):
    data = {"name": "P", "family": "custom", "orientation_deg": 0,
            "pins": [{"number": "1", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1, "pad_width_mm": 1}]}
    data.update(changes)
    return data


def test_numeric_strings_are_refused_in_package_geometry():
    package_from_mapping(_mapping(orientation_deg=90)).pins_seen_from_top()
    with pytest.raises(ValueError):
        package_from_mapping(_mapping(orientation_deg="90"))
    with pytest.raises(ValueError):
        package_from_mapping(_mapping(pins=[{"number": "1", "x_mm": "1.5", "y_mm": 0,
                                             "pad_length_mm": 1, "pad_width_mm": 1}]))


# -- G4: the pin-1 marker is never guessed ------------------------------------

@pytest.mark.parametrize("numbers, expected", [
    (["3", "1", "2"], "1"),
    (["A1", "A2", "B1", "B2"], "A1"),
    (["K", "A"], None),
    (["11", "12", "14", "A1", "A2"], None),
    # CONTRACT CHANGED (generality audit 2026-11-03, GA8): a grid needs two
    # row letters and two columns; one lettered row is not an area array.
    (["A1", "A2", "A10"], None),
    (["A1", "A2"], None),                  # relay coil / bidirectional TVS
    (["A1", "B1"], None),                  # two rows, one column: no grid
    (["A1", "A2", "B1", "B2", "C1", "C2"], "A1"),
    (["A1", "B"], None),
    (["01", "02"], None),
    (["A2", "B1", "B2"], None),
])
def test_pin_one_marks_only_a_declared_first_pin(numbers, expected):
    assert pin_one(numbers) == expected


def _two_pin_package(numbers):
    return PackageGeometry(name="D", family="custom", body_length_mm=4.0, body_width_mm=2.0, pins=[
        PinSpec(number=numbers[0], x_mm=-2.5, y_mm=0, pad_length_mm=1.2, pad_width_mm=1.2),
        PinSpec(number=numbers[1], x_mm=2.5, y_mm=0, pad_length_mm=1.2, pad_width_mm=1.2)])


def test_a_package_without_pin_one_gets_no_dot_and_says_why():
    unmarked = build_package(_two_pin_package(("K", "A")))
    assert not _dots(unmarked) and PIN_ONE_UNMARKED_NOTE in unmarked.description
    marked = build_package(_two_pin_package(("1", "2")))
    assert len(_dots(marked)) == 1 and PIN_ONE_UNMARKED_NOTE not in marked.description


def test_a_castellated_module_without_pin_one_gets_no_dot():
    side = CastellatedSide(edge="left", count=2, pitch=2.54, contact_style=CONTACT_HALF_HOLE)
    numbered = build_castellated_module(CastellatedModuleParams(body_length=10, body_width=8,
                                                                sides=[side]))
    assert len(_dots(numbered)) == 1
    lettered = build_castellated_module(CastellatedModuleParams(
        body_length=10, body_width=8, sides=[CastellatedSide(
            edge="left", count=2, pitch=2.54, contact_style=CONTACT_HALF_HOLE,
            pin_numbers=["K", "A"])]))
    assert not _dots(lettered) and PIN_ONE_UNMARKED_NOTE in lettered.description


# -- N3: an unpopulated pin 1 marks its own grid position, never pin 2 --------

def test_an_unpopulated_pin_one_squares_no_pad_and_marks_its_grid_corner():
    params = PinHeaderParams(rows=2, cols=4, pitch=2.54, row_spacing=10.16,
                             populated_pins={2, 3, 4, 5, 6, 7, 8})
    fp = build_pin_header(params)
    assert all(pad.shape == "circle" for pad in fp.pads)
    dot = _dots(fp)[0]
    # Pin 1's grid position is (0, 0): the dot is at its top-left corner,
    # not near pin 2 (0, 10.16).
    assert dot.center[0] < 0 and dot.center[1] < 0
    full = build_pin_header(PinHeaderParams(rows=2, cols=4, pitch=2.54, row_spacing=10.16))
    assert _pad_shape(full, "1") == "rect"
    assert square_pad_pin(["2", "3"], params) is None
    assert square_pad_pin(["1", "2"], PinHeaderParams(rows=1, cols=2)) == "1"


def test_a_lettered_map_without_a_first_pin_has_no_mark():
    params = PinHeaderParams(rows=1, cols=2, pitch=2.54, pin_map={"K": (0, 0), "A": (0, 1)})
    fp = build_pin_header(params)
    assert not _dots(fp) and all(pad.shape == "circle" for pad in fp.pads)
    assert PIN_ONE_UNMARKED_NOTE in fp.description


# -- N4: the pin-1 dot lies inside the courtyard ------------------------------

@pytest.mark.parametrize("body", [None, (10.16, 4.0, -1.27, -1.27)])
def test_the_header_dot_ink_is_inside_the_courtyard(body):
    extra = {} if body is None else dict(body_length=body[0], body_width=body[1],
                                         body_offset_x=body[2], body_offset_y=body[3])
    fp = build_pin_header(PinHeaderParams(rows=1, cols=4, pitch=2.54, **extra))
    assert _inside(marker_ink_box(_dots(fp)[0]), _courtyard(fp))


@pytest.mark.parametrize("angle", [0, 30, 45, 90, 137])
def test_the_package_dot_ink_is_inside_the_courtyard(angle):
    pins = [PinSpec(number=str(i + 1), x_mm=-2.7 if i < 4 else 2.7,
                    y_mm=(i - 1.5) * 1.27 if i < 4 else (6 - i - 0.5) * 1.27,
                    pad_length_mm=1.5, pad_width_mm=0.6) for i in range(8)]
    geometry = PackageGeometry(name="SOIC", family="soic", body_length_mm=3.9, body_width_mm=4.9,
                               orientation_deg=angle, pins=pins)
    fp = build_package(geometry)
    assert _inside(marker_ink_box(_dots(fp)[0]), _courtyard(fp))


def test_a_dot_inside_the_envelope_does_not_grow_the_courtyard():
    """Negative case: the castellated dot sits between the edge lands, already enclosed."""
    side = CastellatedSide(edge="left", count=3, pitch=2.54, contact_style=CONTACT_HALF_HOLE)
    params = CastellatedModuleParams(body_length=10, body_width=12, sides=[side])
    fp = build_castellated_module(params)
    dot = marker_ink_box(_dots(fp)[0])
    x0, y0, x1, y1 = _courtyard(fp)
    assert _inside(dot, (x0, y0, x1, y1))
    # Symmetric about the body centre: the dot added nothing.
    assert x0 == pytest.approx(-x1) and y0 == pytest.approx(-y1)


# -- N6: "01" is not pin 1 ----------------------------------------------------

def test_a_zero_padded_populated_pin_needs_a_map():
    with pytest.raises(ValueError, match="01"):
        PinHeaderParams(rows=1, cols=4, populated_pins={"01", "2"}).validate()
    PinHeaderParams(rows=1, cols=4, populated_pins={"1", "2"}).validate()
    mapped = PinHeaderParams(rows=1, cols=2, pin_map={"01": (0, 0), "02": (0, 1)},
                             populated_pins={"01"})
    assert [pad.number for pad in build_pin_header(mapped).pads] == ["01"]


# -- N7: blanks in numbers are refused by validate, not at build --------------

def test_blank_pin_numbers_are_refused_by_validate():
    with pytest.raises(ValueError):
        PinHeaderParams(rows=1, cols=2, pin_map={"1 2": (0, 0), "3": (0, 1)}).validate()
    PinHeaderParams(rows=1, cols=2, pin_map={"12": (0, 0), "3": (0, 1)}).validate()
    side = dict(edge="left", count=2, pitch=2.54, contact_style=CONTACT_HALF_HOLE)
    with pytest.raises(ValueError):
        CastellatedModuleParams(body_length=10, body_width=8, sides=[
            CastellatedSide(**side, pin_numbers=["1 2", "3"])]).validate()
    CastellatedModuleParams(body_length=10, body_width=8, sides=[
        CastellatedSide(**side, pin_numbers=["12", "3"])]).validate()


# -- N8: both header paths give the connector courtyard -----------------------

def _clearance(fp):
    """Smallest distance between the courtyard and the pads' bounding box, mm."""
    x0, y0, x1, y1 = _courtyard(fp)
    px0 = min(pad.at[0] - pad.size[0] / 2 for pad in fp.pads)
    py0 = min(pad.at[1] - pad.size[1] / 2 for pad in fp.pads)
    px1 = max(pad.at[0] + pad.size[0] / 2 for pad in fp.pads)
    py1 = max(pad.at[1] + pad.size[1] / 2 for pad in fp.pads)
    return min(px0 - x0, py0 - y0, x1 - px1, y1 - py1)


def test_a_module_on_headers_gets_the_connector_courtyard_on_both_paths():
    pins = [PinSpec(number=str(i + 1), x_mm=i * 2.54 - 3.81, y_mm=0, pad_length_mm=1.7,
                    pad_width_mm=1.7, pad_type="thru_hole", shape="circle", drill_mm=1.0)
            for i in range(4)]
    physical = PackageGeometry(name="M", family="module", pins=pins)
    host = header_footprint_params(physical, 1.7, 1.0)
    assert isinstance(host, HostLandGeometry) and host.courtyard_margin_mm == HEADER_MARGIN_MM
    header = header_footprint_params(PinHeaderParams(rows=1, cols=4), 1.7, 1.0)
    # The same connector margin on both paths: the package path measures it
    # from the lands, the header path from its silk frame around them.
    assert header.margin == HEADER_MARGIN_MM
    assert _clearance(build_package(host)) == pytest.approx(HEADER_MARGIN_MM, abs=0.011)
    # A plain package keeps the general 0.25 mm.
    assert _clearance(build_package(physical)) < HEADER_MARGIN_MM
    assert _clearance(build_package(physical)) >= COURTYARD_MARGIN_MM - 1e-9


# -- G1/G5: assumed sizes are never silent ------------------------------------

def test_assumed_castellated_sizes_are_noted_and_written_in_the_description():
    side = CastellatedSide(edge="left", count=3, pitch=2.54, contact_style=CONTACT_HALF_HOLE)
    assumed = CastellatedModuleParams(body_length=10, body_width=12, sides=[side])
    notes = assumed.geometry_notes()
    assert any("1.70mm" in note and "0.76" in note for note in notes)
    description = build_castellated_module(assumed).description
    assert "assunti, non misurati (stima)" in description and "semi-foro 1.70mm" in description
    declared = CastellatedModuleParams(body_length=10, body_width=12, sides=[side],
                                       hole_diameter=1.0, hole_inset=0.3, pad_length=2.5,
                                       pad_width=1.2)
    assert declared.assumed_sizes(declared.sides[0]) == []
    assert "assunti" not in build_castellated_module(declared).description


def test_the_inset_ratio_is_reported_only_when_the_inset_is_not_declared():
    side = CastellatedSide(edge="left", count=3, pitch=2.54, contact_style=CONTACT_HALF_HOLE)
    kinds = [kind for kind, _ in CastellatedModuleParams(
        body_length=10, body_width=12, sides=[side], hole_diameter=1.0).assumed_sizes(side)]
    assert "half_hole_inset" in kinds and "half_hole" not in kinds
    declared = CastellatedModuleParams(body_length=10, body_width=12, sides=[side],
                                       hole_diameter=1.0, hole_inset=0.3)
    assert "half_hole_inset" not in [kind for kind, _ in declared.assumed_sizes(side)]


# -- N11 ------------------------------------------------------------------------

def test_the_dead_is_pin_one_helper_is_gone():
    assert not hasattr(pin_header_module, "is_pin_one")
    assert math.isfinite(HEADER_MARGIN_MM)
