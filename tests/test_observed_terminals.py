"""Observed terminal geometry, exercised without a FreeCAD installation."""

import ast
import copy
import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


class Shape:
    def __init__(self, boxes):
        self.boxes = boxes
        self.Edges = []
        self.BoundBox = SimpleNamespace(ZMax=max(z + dz for _, _, z, _, _, dz in boxes))

    def fuse(self, other):
        return Shape(self.boxes + other.boxes)

    def cut(self, other):
        return self

    def rotate(self, *args):
        raise AssertionError("Rotation belongs to the external caller")


@pytest.fixture
def cad(monkeypatch):
    made = []

    def make_box(length, width, height, position):
        assert all(math.isfinite(v) and v > 0 for v in (length, width, height))
        box = (*position, length, width, height)
        made.append(box)
        return Shape([box])

    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(Vector=lambda *v: v))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        makeBox=make_box,
        makeCylinder=lambda *args: Shape([(0, 0, 0, 1, 1, 1)]),
    ))
    path = Path(__file__).resolve().parents[1] / "kicadgen/cad3d/freecad_parts.py"
    spec = importlib.util.spec_from_file_location("observed_terminal_test_parts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, made


def component(**updates):
    payload = dict(name="U1", kind="ic", x=10.0, y=-7.0,
                   length_mm=10.0, width_mm=4.0, height_mm=2.0,
                   color_hex="#202020", rotation_deg=0)
    payload.update(updates)
    return payload


def observation(**updates):
    geometry = dict(
        body_bbox=[0.2, 0.25, 0.8, 0.75], confidence="high",
        terminals=[
            {"bbox": [0.0, 0.3, 0.2, 0.4]},
            {"bbox": [0.8, 0.6, 1.0, 0.7]},
            {"bbox": [0.3, 0.0, 0.4, 0.25]},
            {"bbox": [0.6, 0.75, 0.7, 1.0]},
        ],
    )
    geometry.update(updates)
    return geometry


@pytest.mark.parametrize("confidence", ["high", "medium"])
def test_observed_body_and_four_asymmetric_terminals_use_local_y_down(cad, confidence, capsys):
    module, _ = cad
    payload = component(terminal_geometry=observation(confidence=confidence),
                        pin_count=64, pins_by_side={"left": 32, "right": 32})
    before = copy.deepcopy(payload)
    parts = module.build_component(payload, 1.6)
    assert len(parts) == 5  # Exactly the observations, regardless of count hints.
    assert parts[0][1] == "#202020"
    assert parts[0][0].boxes[0] == pytest.approx((7, -8, 1.72, 6, 2, 1.88))
    expected_feet = [
        (5, -6.6, 1.6, 2, .4, .08),
        (13, -7.8, 1.6, 2, .4, .08),
        (8, -6, 1.6, 1, 1, .08),
        (11, -9, 1.6, 1, 1, .08),
    ]
    for (shape, color), expected in zip(parts[1:], expected_feet):
        assert color == module.METAL
        assert shape.boxes[0] == pytest.approx(expected)
        assert len(shape.boxes) == 2  # Foot plus a conservative body-side riser.
    assert payload == before
    assert not capsys.readouterr().out


@pytest.mark.parametrize("length,width", [(10, 4), (3, 9), (.2, .1)])
@pytest.mark.parametrize("height", [.03, .8, 2])
def test_every_solid_stays_inside_envelope_and_risers_inside_observed_feet(cad, length, width, height):
    module, _ = cad
    parts = module.build_component(component(
        length_mm=length, width_mm=width, height_mm=height,
        terminal_geometry=observation()), 1.6)
    for shape, _ in parts:
        for x, y, z, dx, dy, dz in shape.boxes:
            assert x >= 10 - length / 2 - 1e-12
            assert x + dx <= 10 + length / 2 + 1e-12
            assert y >= -7 - width / 2 - 1e-12
            assert y + dy <= -7 + width / 2 + 1e-12
            assert z >= 1.6
            assert z + dz <= 1.6 + height + 1e-12
    body_base = parts[0][0].boxes[0][2]
    assert body_base > 1.6
    for shape, _ in parts[1:]:
        x, y, z, dx, dy, dz = shape.boxes[0]
        assert z + dz < body_base
        for rx, ry, rz, rdx, rdy, rdz in shape.boxes[1:]:
            assert x - 1e-12 <= rx <= rx + rdx <= x + dx + 1e-12
            assert y - 1e-12 <= ry <= ry + rdy <= y + dy + 1e-12
            assert body_base < rz + rdz <= 1.6 + height * .3 + 1e-12


def test_disconnected_terminal_does_not_get_an_invented_bridge(cad):
    module, _ = cad
    parts = module.build_component(component(terminal_geometry=observation(
        terminals=[{"bbox": [0, 0, .1, .1]}])), 0)
    assert len(parts) == 2
    assert len(parts[1][0].boxes) == 1


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 33])
def test_rotation_is_left_to_caller_without_axis_swapping(cad, rotation):
    module, _ = cad
    payload = component(length_mm=3, width_mm=9,
                        terminal_geometry=observation(body_bbox=[.1, .2, .6, .8]))
    reference = module.build_component(payload, 0)
    rotated = module.build_component(dict(payload, rotation_deg=rotation), 0)
    assert [s.boxes for s, _ in rotated] == [s.boxes for s, _ in reference]
    assert reference[0][0].boxes[0][:2] == pytest.approx((8.8, -9.7))


@pytest.fixture
def flip_y():
    # Load the production functions, excluding the script's unconditional main()
    # and CAD imports. This tests the real payload boundary without running STEP.
    path = Path(__file__).resolve().parents[1] / "kicadgen/cad3d/freecad_build_body.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in {"_flip_y", "_swap_front_back"}
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_FB_SWAP" for target in node.targets)
    )]
    namespace = {}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace["_flip_y"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("length,width", [(10, 4), (3, 9)])
def test_flip_y_builder_and_external_cardinal_rotation_compose(cad, flip_y, rotation, length, width):
    module, _ = cad
    geometry = observation(body_bbox=[.1, .2, .6, .8])
    source = component(x=13, y=7, length_mm=length, width_mm=width,
                       rotation_deg=rotation, terminal_geometry=geometry)
    data = dict(body=dict(offset_y_mm=-10, width_mm=20), components=[copy.deepcopy(source)])
    converted = flip_y(data)["components"][0]
    assert converted["y"] == -source["y"]
    assert converted["rotation_deg"] == -rotation
    assert converted["terminal_geometry"] == geometry  # Local rectangles are untouched.
    parts = module.build_component(converted, 1.6)
    assert len(parts) == 1 + len(geometry["terminals"])

    # Oracle: rotate source-local corners in footprint coordinates, then reflect
    # global Y. Compare with the builder's reflected local boxes followed by the
    # caller's CAD rotation. The equality is F R(theta) = R(-theta) F.
    cardinal = {
        0: lambda x, y: (x, y),
        90: lambda x, y: (-y, x),
        180: lambda x, y: (-x, -y),
        270: lambda x, y: (y, -x),
    }[rotation]
    angle = math.radians(converted["rotation_deg"])
    cosine, sine = math.cos(angle), math.sin(angle)
    bboxes = [geometry["body_bbox"]] + [t["bbox"] for t in geometry["terminals"]]
    for (shape, _), bbox in zip(parts, bboxes):
        expected = []
        for u in (bbox[0], bbox[2]):
            for v in (bbox[1], bbox[3]):
                dx, dy = cardinal((u - .5) * length, (v - .5) * width)
                expected.append((source["x"] + dx, -(source["y"] + dy)))
        x, y, _, dx, dy, _ = shape.boxes[0]
        actual = []
        for px in (x, x + dx):
            for py in (y, y + dy):
                local_x, local_y = px - converted["x"], py - converted["y"]
                actual.append((converted["x"] + cosine * local_x - sine * local_y,
                               converted["y"] + sine * local_x + cosine * local_y))
        # Round only for stable vertex ordering under cardinal floating-point trig.
        ordered = lambda points: sorted((round(x, 10), round(y, 10)) for x, y in points)
        for actual_point, expected_point in zip(ordered(actual), ordered(expected)):
            assert actual_point == pytest.approx(expected_point, abs=1e-9)


INVALID_BOXES = [
    None, "0,0,1,1", {}, [0, 0, 1], [0, 0, 1, 1, 1],
    [0, 0, 0, 1], [0, .5, 1, .5], [.8, 0, .2, 1], [0, .8, 1, .2],
    [-.01, 0, 1, 1], [0, 0, 1.01, 1], [False, 0, 1, 1], ["0", 0, 1, 1],
    [None, 0, 1, 1], [float("nan"), 0, 1, 1],
    [0, float("inf"), 1, 1], [0, 0, 1, float("-inf")],
]


@pytest.mark.parametrize("bbox", INVALID_BOXES)
@pytest.mark.parametrize("target", ["body", "terminal"])
def test_invalid_boxes_fall_back_atomically_with_warning(cad, capsys, bbox, target):
    module, made = cad
    geometry = observation()
    if target == "body":
        geometry["body_bbox"] = bbox
    else:
        geometry["terminals"].append({"bbox": bbox})
    parts = module.build_component(component(kind="ic_soic", terminal_geometry=geometry), 1.6)
    assert len(parts) == len(made) == 1
    assert parts[0][0].boxes[0] == (5, -9, 1.6, 10, 4, 2)
    warning = capsys.readouterr().out
    assert "KICADGEN_WARN:" in warning and "terminal_geometry" in warning


@pytest.mark.parametrize("geometry", [
    False, 0, [], {}, observation(confidence="low"), observation(confidence=None),
    observation(confidence=["high"]), observation(terminals=None),
    observation(terminals={}), observation(terminals=[None]),
    observation(terminals=[{}]), observation(terminals=[[0, 0, 1, 1]]),
    observation(terminals=[{"bbox": [0, 0, .1, .1]}] * 129),
])
def test_invalid_observation_does_not_trigger_legacy_guesses(cad, capsys, geometry):
    module, made = cad
    parts = module.build_component(component(kind="ic_qfn", pin_count=16,
                                            terminal_geometry=geometry), 0)
    assert len(parts) == len(made) == 1
    assert "KICADGEN_WARN:" in capsys.readouterr().out


@pytest.mark.parametrize("updates", [
    {"kind": "generic"}, {"kind": "ic"}, {"kind": "ic", "pin_count": 8},
    {"kind": "ic_soic"}, {"kind": "ic_soic", "pin_count": 8},
    {"kind": "ic_qfn"}, {"kind": "ic_qfn", "pin_count": 8}, {"kind": "resistor"},
])
def test_none_observation_is_identical_to_absent_in_geometry_and_warnings(cad, capsys, updates):
    module, _ = cad
    absent = module.build_component(component(**updates), 0)
    absent_warning = capsys.readouterr().out
    explicit_none = module.build_component(component(**updates, terminal_geometry=None), 0)
    none_warning = capsys.readouterr().out
    assert [(s.boxes, color) for s, color in explicit_none] == [(s.boxes, color) for s, color in absent]
    assert none_warning == absent_warning
    assert "semplificato" not in none_warning


@pytest.mark.parametrize("count", [0, 128])
def test_terminal_list_limits(cad, count):
    module, _ = cad
    # Distinct narrow boxes along the top, including the maximum supported count.
    terminals = [{"bbox": [i / 128, 0, (i + .5) / 128, .1]} for i in range(count)]
    parts = module.build_component(component(terminal_geometry=observation(terminals=terminals)), 0)
    assert len(parts) == count + 1


@pytest.mark.parametrize("hints", [{}, {"pin_count": 8}, {"pins_by_side": {"left": 4}}])
def test_unknown_ic_never_guesses_soic_even_with_count_hints(cad, capsys, hints):
    module, _ = cad
    parts = module.build_component(component(**hints), 0)
    assert len(parts) == 1
    assert parts[0][0].boxes == [(5, -9, 0, 10, 4, 2)]
    assert "KICADGEN_WARN:" in capsys.readouterr().out


def test_qfn_without_observations_does_not_fabricate_sixteen_pins(cad, capsys):
    module, _ = cad
    parts = module.build_component(component(kind="ic_qfn"), 0)
    assert len(parts) == 1
    assert "approssimata" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["ic_soic", "ic_qfn", "resistor", "generic"])
def test_observation_takes_precedence_over_legacy_kind(cad, kind, capsys):
    module, _ = cad
    parts = module.build_component(component(kind=kind, terminal_geometry=observation()), 0)
    assert len(parts) == 5
    assert not capsys.readouterr().out


# CONTRACT CHANGED 2026-09-26 (punto 82, SOIC senza terminali osservati): la
# stima dei reofori per lato e' ora lunghezza / passo (SOIC-8 4.9 mm -> 4,
# SOIC-16 9.9 mm -> 8), non (lunghezza - 1) / passo, che contava sempre un
# reoforo in meno (SOIC-8 -> 3). Il corpo di prova da 10 mm ha quindi 8
# reofori per lato: 16 + il corpo = 17 forme (prima 15).
@pytest.mark.parametrize("kind,count", [("ic_soic", 17), ("resistor", 3)])
def test_explicit_legacy_builders_are_preserved_with_approximation_warning(cad, kind, count, capsys):
    module, _ = cad
    parts = module.build_component(component(kind=kind), 0)
    assert len(parts) == count
    assert "approssimata" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["ic_soic", "ic_qfn"])
@pytest.mark.parametrize("hints,count", [({"pin_count": 8}, 9),
                                        ({"pins_by_side": {"left": 3, "bottom": 2}}, 6)])
def test_legacy_explicit_counts_preserved_but_warned(cad, kind, hints, count, capsys):
    module, _ = cad
    parts = module.build_component(component(kind=kind, **hints), 0)
    assert len(parts) == count
    assert "approssimata" in capsys.readouterr().out


def test_cad_failure_gracefully_returns_generic(cad, monkeypatch, capsys):
    module, _ = cad
    original = module.Part.makeBox

    def fail_observed_body_once(*args):
        monkeypatch.setattr(module.Part, "makeBox", original)
        raise RuntimeError("CAD construction failed")

    monkeypatch.setattr(module.Part, "makeBox", fail_observed_body_once)
    parts = module.build_component(component(terminal_geometry=observation()), 0)
    assert len(parts) == 1
    assert parts[0][0].boxes == [(5, -9, 0, 10, 4, 2)]
    assert "CAD construction failed" in capsys.readouterr().out
