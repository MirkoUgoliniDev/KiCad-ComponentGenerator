"""Independent projection/topology checks; no FreeCAD or network required.

The box-union mock proves coordinate and contact contracts, not OCC validity,
fillet quality, or correspondence to photograph pixels. Known topology gaps
are strict xfails so a future resolution requires updating these contracts.
"""
import ast
from copy import deepcopy
import importlib.util
from itertools import product
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from kicadgen.services.photo_terminals import decode_terminal_geometry


CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"


class BoxUnion:
    def __init__(self, boxes):
        self.boxes = boxes
        self.rotation_calls = []

    def fuse(self, other):
        return BoxUnion(self.boxes + other.boxes)

    def rotate(self, centre, axis, angle):
        assert axis == (0, 0, 1)
        self.rotation_calls.append((centre, angle))

    def projected_corners(self):
        vertices = []
        for x, y, z, dx, dy, dz in self.boxes:
            for px, py in product((x, x + dx), (y, y + dy)):
                for (cx, cy, _), angle in self.rotation_calls:
                    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
                    px, py = cx + c * (px-cx) - s * (py-cy), cy + s * (px-cx) + c * (py-cy)
                vertices.append((px, py))
        return vertices


@pytest.fixture
def renderer(monkeypatch):
    def make_box(dx, dy, dz, position):
        assert min(dx, dy, dz) > 0
        return BoxUnion([(*position, dx, dy, dz)])

    freecad = SimpleNamespace(Vector=lambda *args: args)
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(makeBox=make_box))
    spec = importlib.util.spec_from_file_location("independent_terminal_parts", CAD_DIR / "freecad_parts.py")
    parts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parts)

    # Execute the production payload reflection and actual component rotation
    # loop, not a test reimplementation of the caller's sign/pivot/order.
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    reflection = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name in {"_flip_y", "_swap_front_back", "_safe"}]
    reflection += [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_FB_SWAP" for t in n.targets)]
    ns = {}
    exec(compile(ast.Module(body=reflection, type_ignores=[]), "<reflection>", "exec"), ns)
    # Select the caller through its build operation, not the iterable's syntax.
    # Keep the production build, empty-result guard, rotation/pivot assignments,
    # and entire shape loop. Exclude only subsequent silk/marking decoration.
    build_blocks = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
                    and any(isinstance(statement, ast.Assign)
                            and isinstance(statement.value, ast.Call)
                            and isinstance(statement.value.func, ast.Attribute)
                            and statement.value.func.attr == "build_component"
                            for statement in n.body)]
    assert len(build_blocks) == 1, "Expected one production component build block"
    build_block = build_blocks[0]
    shape_loops = [n for n in build_block.body if isinstance(n, ast.For)
                   and any(isinstance(call, ast.Call)
                           and isinstance(call.func, ast.Attribute)
                           and call.func.attr == "rotate" for call in ast.walk(n))]
    assert len(shape_loops) == 1, "Expected one component shape rotation loop"
    callers = [n for n in ast.walk(tree) if isinstance(n, ast.For)
               and build_block in n.body]
    assert len(callers) == 1
    caller = deepcopy(callers[0])
    # Leave exceptions uncaught in the fixture so caller errors fail the test;
    # retain the outer loop so the actual empty-result `continue` stays valid.
    caller.body = (deepcopy(callers[0].body[:callers[0].body.index(build_block)])
                   + deepcopy(build_block.body[:build_block.body.index(shape_loops[0]) + 1]))
    compiled = compile(ast.Module(body=[caller], type_ignores=[]), "<component-loop>", "exec")

    def render(source, z0=1.6):
        data = {"body": {"offset_y_mm": 0, "width_mm": 20}, "components": [deepcopy(source)]}
        reflected = ns["_flip_y"](data)
        output = []

        def add(name, shape, color):
            output.append((shape, color))
            return SimpleNamespace(Name=name)

        exec(compiled, {"FreeCAD": freecad, "freecad_parts": parts,
                       "components": reflected["components"], "z1": z0,
                       "_safe": ns["_safe"], "metadata": {}, "add": add})
        return output

    return parts, render


LAYOUTS = [
    ([.23, .17, .79, .83], [[.02, .21, .23, .29], [.79, .61, .96, .74], [.4, .83, .48, .99]]),
    ([.14, .3, .88, .73], [[.2, 0, .28, .3], [.65, .73, .77, 1], [0, .5, .14, .58]]),
    # Unusual observations must not be silently replaced by package ratios.
    ([.47, .06, .54, .97], [[0, .1, .47, .2], [.54, .4, 1, .48], [.54, .7, 1, .76]]),
]


def source(body, terminals, **updates):
    result = dict(name="unknown", _id=77, kind="ic", x=3.7, y=-2.1,
                  length_mm=8, width_mm=3, height_mm=1.4, rotation_deg=0,
                  terminal_geometry=dict(body_bbox=body, confidence="high",
                                         terminals=[{"bbox": b} for b in terminals]))
    result.update(updates)
    return result


def bounds(vertices):
    return (min(p[0] for p in vertices), min(p[1] for p in vertices),
            max(p[0] for p in vertices), max(p[1] for p in vertices))


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("length,width", [(9, 2), (2, 9), (.3, .8)])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 33, -90])
def test_actual_projected_bounds_equal_observed_boxes(renderer, layout, length, width, rotation):
    _, render = renderer
    body, terminals = layout
    payload = source(body, terminals, length_mm=length, width_mm=width, rotation_deg=rotation)
    before = deepcopy(payload)
    output = render(payload)
    assert payload == before
    assert len(output) == 1 + len(terminals)
    angle = math.radians(rotation)
    c, s = math.cos(angle), math.sin(angle)
    for (shape, _), bbox in zip(output, [body] + terminals):
        expected = []
        for u, v in product((bbox[0], bbox[2]), (bbox[1], bbox[3])):
            # Photo-local -> photo-world -> CAD-world, independent oracle.
            x, y = (u-.5)*length, (v-.5)*width
            expected.append((payload["x"] + c*x-s*y, -(payload["y"] + s*x+c*y)))
        assert bounds(shape.projected_corners()) == pytest.approx(bounds(expected), abs=1e-10)
        assert len(shape.rotation_calls) == (1 if rotation else 0)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("length,width", [(8, 3), (2, 7)])
def test_photo_decoder_to_actual_caller_has_no_double_axis_swap(renderer, rotation, length, width):
    _, render = renderer
    body, terminals = LAYOUTS[0]
    entry = dict(resolved=True, confidence="high", body_bbox=body,
                 terminals=[{"bbox": b} for b in terminals])
    geometry = decode_terminal_geometry(entry, rotation)
    output = render(source(body, terminals, terminal_geometry=geometry,
                           length_mm=length, width_mm=width, rotation_deg=rotation))
    photo_width, photo_height = (width, length) if rotation % 180 else (length, width)
    for (shape, _), b in zip(output, [body] + terminals):
        expected = (3.7+(b[0]-.5)*photo_width, 2.1+(.5-b[3])*photo_height,
                    3.7+(b[2]-.5)*photo_width, 2.1+(.5-b[1])*photo_height)
        assert bounds(shape.projected_corners()) == pytest.approx(expected)


@pytest.mark.parametrize("name,identifier", [("U1", 1), ("IC superiore destro", 5), ("other board", 923)])
def test_names_and_ids_cannot_select_different_geometry(renderer, name, identifier):
    _, render = renderer
    body, terminals = LAYOUTS[1]
    a = render(source(body, terminals, rotation_deg=90))
    b = render(source(body, terminals, rotation_deg=90, name=name, _id=identifier))
    assert [(s.boxes, s.rotation_calls, c) for s, c in a] == [(s.boxes, s.rotation_calls, c) for s, c in b]


def face_or_volume_contact(a, b):
    overlaps = [min(a[i]+a[i+3], b[i]+b[i+3])-max(a[i], b[i]) for i in range(3)]
    return min(overlaps) >= -1e-10 and sum(d > 1e-10 for d in overlaps) >= 2


@pytest.mark.parametrize("layout", LAYOUTS)
def test_attached_feet_have_connected_risers_touching_body(renderer, layout):
    module, _ = renderer
    body, terminals = layout
    parts = module.build_component(source(body, terminals), 0)
    body_box = parts[0][0].boxes[0]
    for lead, _ in parts[1:]:
        assert len(lead.boxes) == 2
        foot, riser = lead.boxes
        assert face_or_volume_contact(foot, riser)
        assert face_or_volume_contact(body_box, riser)


@pytest.mark.parametrize("first_terminal", [
    [0, .3, .19, .4],  # Gap of .01: decoder tolerates it, renderer leaves an island.
    [0, 0, .2, .2],    # Corner-only contact: vertical line, not a connected lead face.
])
@pytest.mark.xfail(strict=True, reason="Decoder accepts gap/corner attachment without a face-connected CAD lead")
def test_decoded_attached_terminals_should_have_face_contact(renderer, first_terminal):
    module, _ = renderer
    body = [.2, .2, .8, .8]
    terminals = [first_terminal, [.8, .3, 1, .4], [.4, .8, .5, 1]]
    entry = dict(resolved=True, confidence="high", body_bbox=body,
                 terminals=[{"bbox": b} for b in terminals])
    geometry = decode_terminal_geometry(entry, 0)
    assert geometry is not None
    parts = module.build_component(source(body, terminals, terminal_geometry=geometry), 0)
    body_box = parts[0][0].boxes[0]
    assert any(face_or_volume_contact(body_box, box) for box in parts[1][0].boxes)
