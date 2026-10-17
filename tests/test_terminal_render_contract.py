"""Independent projection/topology checks; no FreeCAD or network required.

The box-union mock proves coordinate and contact contracts, not OCC validity,
fillet quality, or correspondence to photograph pixels.

Attachment contract (MEDIO 16, 2026-10-16): every terminal the decoder accepts
as attached is drawn with a riser sharing a face with the body; the lead is
only enlarged toward the body, never moved at its outer end; a terminal beyond
the shared tolerance stays an island without an invented bridge.
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


# Body [.2, .2, .8, .8]; the first terminal is the one under test, the others
# are ordinary face contacts. Expected: the drawn foot after anchoring, with
# source() length 8 / width 3 (riser lateral footprint .12 mm = .04 of width).
ANCHORED = [
    # Gap of .01 to the left face: the inner edge is pulled onto the face.
    ([0, .3, .19, .4], [0, .3, .2, .4]),
    # Corner-only contact: anchored to the left face (X before Y on the tie),
    # the root lengthened along the face by the riser footprint.
    ([0, 0, .2, .2], [0, 0, .2, .24]),
    # Gap on both axes near a corner: both closures, outer edges untouched.
    ([0, .05, .17, .15], [0, .05, .2, .24]),
    # Overlap into the body is kept as observed (hidden under the body).
    ([0, .3, .3, .4], [0, .3, .3, .4]),
]


def decoded(terminals, rotation=0, **updates):
    body = [.2, .2, .8, .8]
    entry = dict(resolved=True, confidence="high", body_bbox=body,
                 terminals=[{"bbox": b} for b in terminals])
    geometry = decode_terminal_geometry(entry, 0)
    assert geometry is not None
    return source(body, terminals, terminal_geometry=geometry, rotation_deg=rotation, **updates)


@pytest.mark.parametrize("first_terminal,anchored", ANCHORED)
def test_decoded_attached_terminals_have_face_contact(renderer, first_terminal, anchored):
    module, _ = renderer
    terminals = [first_terminal, [.8, .3, 1, .4], [.4, .8, .5, 1]]
    parts = module.build_component(decoded(terminals), 0)
    body_box = parts[0][0].boxes[0]
    for lead, _ in parts[1:]:
        assert len(lead.boxes) == 2
        foot, riser = lead.boxes
        assert face_or_volume_contact(foot, riser)
        assert face_or_volume_contact(body_box, riser)
    foot = parts[1][0].boxes[0]
    expected = (3.7 + (anchored[0] - .5) * 8, -2.1 + (.5 - anchored[3]) * 3,
                3.7 + (anchored[2] - .5) * 8, -2.1 + (.5 - anchored[1]) * 3)
    assert (foot[0], foot[1], foot[0] + foot[3], foot[1] + foot[4]) == pytest.approx(expected)


@pytest.mark.parametrize("first_terminal,anchored", ANCHORED)
@pytest.mark.parametrize("rotation", [45, 33, -120])
def test_anchoring_survives_non_cardinal_rotation(renderer, first_terminal, anchored, rotation):
    """The caller's rotation is applied once to the anchored lead; the oracle is
    the projection used by test_actual_projected_bounds_equal_observed_boxes."""
    _, render = renderer
    terminals = [first_terminal, [.8, .3, 1, .4], [.4, .8, .5, 1]]
    output = render(decoded(terminals, rotation))
    assert len(output) == 4
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    for (shape, _), bbox in zip(output, [[.2, .2, .8, .8], anchored] + terminals[1:]):
        expected = []
        for u, v in product((bbox[0], bbox[2]), (bbox[1], bbox[3])):
            x, y = (u - .5) * 8, (v - .5) * 3
            expected.append((3.7 + c * x - s * y, -(-2.1 + s * x + c * y)))
        assert bounds(shape.projected_corners()) == pytest.approx(bounds(expected), abs=1e-10)
        assert len(shape.rotation_calls) == 1
    # Local (pre-rotation) contact is what the STEP solid carries.
    body_box = output[0][0].boxes[0]
    assert all(face_or_volume_contact(body_box, lead.boxes[1]) for lead, _ in output[1:])


def test_far_terminal_is_an_island_not_a_bridge(renderer):
    """Negative: a terminal clearly beyond the shared tolerance (here across the
    envelope from the body) is refused by the decoder and, if drawn anyway,
    gets no riser and touches nothing."""
    module, _ = renderer
    body = [.2, .2, .5, .8]
    far = [.9, .3, 1, .4]           # gap .4 along X: not a lead of this body
    terminals = [far, [.5, .3, .6, .4], [.3, .8, .4, 1]]
    entry = dict(resolved=True, confidence="high", body_bbox=body,
                 terminals=[{"bbox": b} for b in terminals])
    with pytest.raises(ValueError):
        decode_terminal_geometry(entry, 0)
    parts = module.build_component(source(body, terminals), 0)
    body_box = parts[0][0].boxes[0]
    assert len(parts[1][0].boxes) == 1
    assert not face_or_volume_contact(body_box, parts[1][0].boxes[0])
    assert all(face_or_volume_contact(body_box, lead.boxes[1]) for lead, _ in parts[2:])


@pytest.mark.parametrize("terminal", [[0, .3, .19, .4], [0, 0, .2, .2]])
@pytest.mark.parametrize("length,width", [(8, 3), (.3, .8), (2, 9)])
def test_anchoring_only_enlarges_toward_the_body(renderer, terminal, length, width):
    """The outer end measured on the photo is never moved; the foot grows only
    toward the body face, whatever the envelope proportions."""
    module, _ = renderer
    terminals = [terminal, [.8, .3, 1, .4], [.4, .8, .5, 1]]
    parts = module.build_component(decoded(terminals, length_mm=length, width_mm=width), 0)
    x, y, _, dx, dy, _ = parts[1][0].boxes[0]
    observed = (3.7 + (terminal[0] - .5) * length, -2.1 + (.5 - terminal[3]) * width,
                3.7 + (terminal[2] - .5) * length, -2.1 + (.5 - terminal[1]) * width)
    assert x == pytest.approx(observed[0])            # outer (left) edge kept
    assert y + dy == pytest.approx(observed[3])       # top edge (away from body) kept
    assert x + dx >= observed[2] - 1e-12 and y <= observed[1] + 1e-12
    # Growth toward the body is bounded by the gap plus the riser footprint
    # (.12 mm), whichever face the millimetre protrusion selects.
    body = parts[0][0].boxes[0]
    assert x + dx <= max(observed[2], body[0]) + .12 + 1e-12
    assert y >= min(observed[1], body[1] + body[4]) - .12 - 1e-12


# A gap of .25 - .19 on Y, written with two decimals like a model answer: in
# floating point it lands exactly on the tolerance, accepted by the decoder.
BOUNDARY_BODY = [.2, .25, .8, .75]
BOUNDARY_TERMINALS = [[.3, .09, .4, .19], [.8, .3, 1, .4], [.4, .75, .5, 1]]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_boundary_gap_accepted_by_the_decoder_is_drawn_attached(renderer, rotation):
    """The renderer uses the decoder's `attached` flag: a gap exactly at the
    tolerance is drawn attached at every cardinal rotation, never as an island."""
    module, _ = renderer
    entry = dict(resolved=True, confidence="high", body_bbox=BOUNDARY_BODY,
                 terminals=[{"bbox": b} for b in BOUNDARY_TERMINALS])
    geometry = decode_terminal_geometry(entry, rotation)
    parts = module.build_component(
        source(geometry["body_bbox"], [t["bbox"] for t in geometry["terminals"]],
               terminal_geometry=geometry), 0)
    body_box = parts[0][0].boxes[0]
    for lead, _ in parts[1:]:
        assert len(lead.boxes) == 2
        assert face_or_volume_contact(body_box, lead.boxes[1])


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_payload_without_flag_is_decided_once_with_rounding_slack(renderer, rotation):
    """Backward compatibility: a cached payload without `attached` flags gets
    the same decision at every rotation (the slack absorbs 1 - x rounding)."""
    module, _ = renderer
    entry = dict(resolved=True, confidence="high", body_bbox=BOUNDARY_BODY,
                 terminals=[{"bbox": b} for b in BOUNDARY_TERMINALS])
    geometry = decode_terminal_geometry(entry, rotation)
    for terminal in geometry["terminals"]:
        del terminal["attached"]
    parts = module.build_component(
        source(geometry["body_bbox"], [t["bbox"] for t in geometry["terminals"]],
               terminal_geometry=geometry), 0)
    assert [len(lead.boxes) for lead, _ in parts[1:]] == [2, 2, 2]


def test_explicit_detached_flag_is_never_bridged(renderer):
    """Negative: the flag is authoritative also when it says detached, even for
    a box that would pass the geometric tolerance."""
    module, _ = renderer
    terminals = [[0, .3, .2, .4], [.8, .3, 1, .4], [.4, .8, .5, 1]]
    geometry = dict(body_bbox=[.2, .2, .8, .8], confidence="high",
                    terminals=[{"bbox": b, "attached": i != 0} for i, b in enumerate(terminals)])
    parts = module.build_component(source([.2, .2, .8, .8], terminals,
                                          terminal_geometry=geometry), 0)
    assert len(parts[1][0].boxes) == 1
    assert [len(lead.boxes) for lead, _ in parts[2:]] == [2, 2]


def feet(parts):
    """Drawn foot rectangles (x0, y0, x1, y1) in mm, one per lead."""
    result = []
    for shape, _ in parts[1:]:
        x, y, _, dx, dy, _ = shape.boxes[0]
        result.append((x, y, x + dx, y + dy))
    return result


def overlap_area(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


@pytest.mark.parametrize("first,neighbour", [
    # Corner lead, fine-pitch neighbour just inside the face span.
    ([0, .1, .2, .2], [0, .22, .2, .3]),
    # Corner lead slightly detached (blurred edge), neighbour below it.
    ([0, .12, .19, .19], [0, .215, .19, .3]),
    # Neighbour exactly at the span edge: no lateral room at all.
    ([0, .1, .2, .2], [0, .2, .2, .3]),
])
@pytest.mark.parametrize("length,width", [(8, 3), (2, 9)])
def test_anchoring_never_makes_two_leads_overlap(renderer, capsys, first, neighbour, length, width):
    """Positive: lateral growth is clamped against neighbours, the outer end is
    kept and the riser stays a valid box inside the foot.

    CONTRACT CHANGED 2026-10-16 (R2T2): the riser of every lead drawn attached
    must also share a FACE with the body; the former version accepted the
    edge-only contact of the no-room case without checking the body at all.
    The only other admitted outcome is the documented fallback: the foot as
    observed, alone, with a warning naming the terminal."""
    module, _ = renderer
    terminals = [first, neighbour, [.8, .3, 1, .4]]
    parts = module.build_component(decoded(terminals, length_mm=length, width_mm=width), 0)
    body_box = parts[0][0].boxes[0]
    warned = capsys.readouterr().out
    for position, (lead, _) in enumerate(parts[1:], 1):
        if len(lead.boxes) == 2:
            assert face_or_volume_contact(body_box, lead.boxes[1])
        else:
            assert len(lead.boxes) == 1 and f'"terminal": {position}' in warned
    drawn = feet(parts)
    for i, a in enumerate(drawn):
        for b in drawn[i + 1:]:
            assert overlap_area(a, b) <= 1e-12
    x0 = 3.7 + (first[0] - .5) * length
    top = -2.1 + (.5 - first[1]) * width
    assert drawn[0][0] == pytest.approx(x0) and drawn[0][3] == pytest.approx(top)
    # In these layouts a face is always reachable (the top face when the left
    # one is blocked), so the fallback must not be taken.
    assert len(parts[1][0].boxes) == 2
    foot, riser = parts[1][0].boxes
    assert riser[0] >= foot[0] - 1e-12 and riser[0] + riser[3] <= foot[0] + foot[3] + 1e-12
    assert riser[1] >= foot[1] - 1e-12 and riser[1] + riser[4] <= foot[1] + foot[4] + 1e-12


def test_isolated_corner_lead_growth_is_unchanged_by_far_leads(renderer):
    """Negative: with no neighbour in its growth path the corner lead keeps the
    full riser-footprint lengthening (the ANCHORED oracle)."""
    module, _ = renderer
    terminals = [[0, 0, .2, .2], [.8, .3, 1, .4], [.4, .8, .5, 1]]
    parts = module.build_component(decoded(terminals), 0)
    expected = (3.7 + (0 - .5) * 8, -2.1 + (.5 - .24) * 3,
                3.7 + (.2 - .5) * 8, -2.1 + (.5 - 0) * 3)
    assert feet(parts)[0] == pytest.approx(expected)
