"""@brief Fix E: component orientation contracts of the approximate CAD builders.

Covers, without FreeCAD:

* E1 -- the verse correction compares the landmark side (global photo frame,
  Y down) with the native model direction in the component's LOCAL frame,
  i.e. after undoing the Y flip AND the component rotation applied later;
* E2 -- `_ic_observed_pins` puts image-"top" pads on local +Y and keeps every
  pad inside the photographed envelope;
* E3 -- `_outward_sign` measures "outward" from the board centre, not from the
  origin;
* E10 -- polarity / pin-1 features only from an observed landmark, chip
  terminations along the long side, tiny chips without raising;
* C12 -- the landmark index follows the cache folders chosen by the parent.
"""
import ast
from copy import deepcopy
import importlib.util
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"


class _Shape:
    """Axis-aligned box union: enough to read where a builder put things."""

    def __init__(self, boxes, kind="box"):
        self.boxes = boxes
        self.kind = kind
        self.cuts = []

    def fuse(self, other):
        return _Shape(self.boxes + other.boxes)

    def cut(self, other):
        result = _Shape(list(self.boxes), self.kind)
        result.cuts = self.cuts + [other]
        return result

    def makeFillet(self, radius, edges):  # noqa: N802 - FreeCAD API name
        return self

    @property
    def Edges(self):  # noqa: N802 - FreeCAD API name
        return []

    @property
    def BoundBox(self):  # noqa: N802 - FreeCAD API name
        zs = [v for _, _, z, _, _, dz in self.boxes for v in (z, z + dz)]
        return SimpleNamespace(ZMax=max(zs), ZMin=min(zs))

    def bounds(self):
        xs = [v for x, _, _, dx, _, _ in self.boxes for v in (x, x + dx)]
        ys = [v for _, y, _, _, dy, _ in self.boxes for v in (y, y + dy)]
        return min(xs), min(ys), max(xs), max(ys)


def _make_box(dx, dy, dz, position=(0, 0, 0)):
    assert min(dx, dy, dz) > 0, "a builder produced a degenerate box"
    return _Shape([(*position, dx, dy, dz)])


def _make_cylinder(radius, height, base=(0, 0, 0), axis=(0, 0, 1)):
    shape = _Shape([(base[0] - radius, base[1] - radius, base[2],
                     2 * radius, 2 * radius, height)], kind="cylinder")
    shape.centre = (base[0], base[1])
    return shape


@pytest.fixture
def parts(monkeypatch):
    """@brief freecad_parts loaded with a box-recording Part stub."""
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(Vector=lambda *a: a))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        makeBox=_make_box, makeCylinder=_make_cylinder))
    spec = importlib.util.spec_from_file_location("fixE_parts", CAD_DIR / "freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._warn = lambda *a, **k: None
    return module


@pytest.fixture(scope="module")
def flip_y():
    """@brief The production `_flip_y` of freecad_build_body, without FreeCAD."""
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name == "_flip_y")
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "_FB_SWAP" for t in n.targets))]
    namespace = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<flip>", "exec"), namespace)
    return namespace["_flip_y"]


# Photo frame: X right, Y down.
_IMAGE = {"right": (1, 0), "left": (-1, 0), "bottom": (0, 1), "top": (0, -1)}


def _rotate(direction, degrees):
    """Exact counter-clockwise rotation (Y up) of a cardinal direction."""
    c, s = round(math.cos(math.radians(degrees))), round(math.sin(math.radians(degrees)))
    return (c * direction[0] - s * direction[1], s * direction[0] + c * direction[1])


def _reflected_rotation(flip_y, image_rotation):
    """Rotation the builder's caller applies, obtained through the real `_flip_y`."""
    data = {"body": {"offset_y_mm": 0.0, "width_mm": 10.0},
            "components": [{"x": 1.0, "y": 2.0, "rotation_deg": image_rotation}]}
    return flip_y(deepcopy(data))["components"][0]["rotation_deg"]


# ── E1: verse correction at every cardinal component rotation ────────────

@pytest.mark.parametrize("image_rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
@pytest.mark.parametrize("native", [(1, 0), (-1, 0), (0, 1), (0, -1)])
@pytest.mark.parametrize("inferred", [0, 90])
def test_the_landmark_ends_on_the_observed_side_after_every_rotation(
        parts, flip_y, image_rotation, side, native, inferred):
    """@brief Positive: whenever a correction is returned, the final model
    puts the native landmark exactly on the side seen in the photograph.

    The whole chain is the production one: `_flip_y` negates the rotation,
    the builder rotates by inferred + correction, the caller by the component
    rotation. The final direction is read back in the photo frame (Y down).
    """
    world_rotation = _reflected_rotation(flip_y, image_rotation)
    extra = parts._verse_correction(native, inferred, side, world_rotation)
    local = _rotate(native, inferred)
    axis_matches = abs(local[0]) == abs(_rotate(
        (_IMAGE[side][0], -_IMAGE[side][1]), -world_rotation)[0])
    if not axis_matches:
        # Negative, structural: the axis decided by the envelope points the
        # landmark across the observed side; 0/180 cannot fix it.
        assert extra is None
        return
    assert extra in (0, 180)
    final = _rotate(_rotate(native, inferred + extra), world_rotation)
    assert (final[0], -final[1]) == _IMAGE[side]


@pytest.mark.parametrize("image_rotation", [90, 270])
def test_ignoring_the_component_rotation_is_the_old_defect(parts, flip_y, image_rotation):
    """@brief Negative: comparing in the global frame gives no or a wrong answer.

    A quarter-turned component whose native landmark sits on +X: seen in the
    photo on the top/bottom side. The old comparison (component rotation
    ignored) cannot match a horizontal model vector with a vertical side.
    """
    world_rotation = _reflected_rotation(flip_y, image_rotation)
    final_side = _rotate((1, 0), world_rotation)
    observed = next(name for name, vector in _IMAGE.items()
                    if vector == (final_side[0], -final_side[1]))
    assert parts._verse_correction((1, 0), 0, observed, 0) is None
    assert parts._verse_correction((1, 0), 0, observed, world_rotation) == 0


@pytest.mark.parametrize("rotation", [45, 30.5, float("nan"), float("inf"), "x"])
def test_a_non_cardinal_rotation_has_no_cardinal_side(parts, rotation):
    """@brief Negative: a side of a tilted box is not a local direction."""
    assert parts._local_side_direction("top", rotation) is None
    assert parts._verse_correction((1, 0), 0, "right", rotation) is None


def test_the_library_import_passes_the_component_rotation(parts):
    """@brief The call site forwards the rotation the caller will apply."""
    source = (CAD_DIR / "freecad_parts.py").read_text(encoding="utf-8")
    call = source[source.index("verse = _verse_correction("):]
    call = call[:call.index("if verse:")]
    assert 'component.get("rotation_deg")' in call


# ── E2: pads counted from the photo ──────────────────────────────────────

def _pads(result):
    return [shape for shape, colour in result if colour == "#c8c8c8"]


@pytest.mark.parametrize("side,sign", [("top", 1), ("bottom", -1)])
def test_image_top_pads_are_on_local_plus_y(parts, side, sign):
    """@brief Positive/negative pair: the image top is +Y in the Y-up frame."""
    result = parts._ic_observed_pins(0.0, 0.0, 0.0, 6.0, 4.0, 1.0, "#000", "ic_soic",
                                     pins_by_side={side: 3})
    pads = _pads(result)
    assert len(pads) == 3
    for pad in pads:
        x0, y0, x1, y1 = pad.bounds()
        assert (y0 + y1) / 2 * sign > 1.0


@pytest.mark.parametrize("sides", [
    {"left": 4, "right": 4}, {"top": 2, "bottom": 3},
    {"left": 1, "right": 2, "top": 3, "bottom": 4}])
def test_every_pad_stays_inside_the_photographed_envelope(parts, sides):
    """@brief The envelope read on the photo already includes the leads."""
    cx, cy, length, width = 3.0, -2.0, 7.0, 5.0
    result = parts._ic_observed_pins(cx, cy, 0.0, length, width, 1.2, "#000", "ic_qfn",
                                     pins_by_side=sides)
    assert len(_pads(result)) == sum(sides.values())
    for shape, _ in result:
        x0, y0, x1, y1 = shape.bounds()
        assert x0 >= cx - length / 2 - 1e-9 and x1 <= cx + length / 2 + 1e-9
        assert y0 >= cy - width / 2 - 1e-9 and y1 <= cy + width / 2 + 1e-9
    body = result[0][0].bounds()
    # The body shrinks only on the sides that carry leads.
    assert (body[0] > cx - length / 2) == bool(sides.get("left"))
    assert (body[3] < cy + width / 2) == bool(sides.get("top"))


def test_pads_survive_the_payload_reflection(parts, flip_y):
    """@brief Combined: image top -> `_flip_y` -> builder -> caller rotation.

    A component at image (0, -3) (above the centre in the photo) with pads on
    its image top at rotation 0 must end with the pads ABOVE the body in the
    model (larger Y), exactly as `_observed_terminals` does.
    """
    data = {"body": {"offset_y_mm": -10.0, "width_mm": 20.0},
            "components": [{"x": 0.0, "y": -3.0, "rotation_deg": 0.0}]}
    component = flip_y(data)["components"][0]
    result = parts._ic_observed_pins(component["x"], component["y"], 0.0, 6.0, 4.0, 1.0,
                                     "#000", "ic_soic", pins_by_side={"top": 2})
    body_top = result[0][0].bounds()[3]
    assert component["y"] == 3.0
    assert all(pad.bounds()[1] >= body_top - 1e-9 for pad in _pads(result))


# ── E3: outward direction from the board centre ──────────────────────────

def test_outward_is_measured_from_the_board_centre(parts):
    """@brief Positive: a connector left of an off-centre board faces left.

    Matrix boards have the origin on pin 1; the board centre can be at +10.
    A connector at x=1 is on the LEFT edge of that board even though its
    coordinate is positive.
    """
    assert parts._outward_sign(1.0, 0.0, 0, 0, (10.0, 0.0)) == -1.0
    assert parts._outward_sign(19.0, 0.0, 0, 0, (10.0, 0.0)) == 1.0
    assert parts._outward_sign(1.0, 0.0, 0, 180, (10.0, 0.0)) == 1.0


def test_the_origin_default_is_the_old_defect_on_an_off_centre_board(parts):
    """@brief Negative: measured from the origin the same connector faces inwards."""
    assert parts._outward_sign(1.0, 0.0, 0, 0) == 1.0


def test_build_component_forwards_the_board_centre(parts, monkeypatch):
    """@brief The payload field reaches the connector builder."""
    seen = {}

    def fake_connector(*args, **kwargs):
        seen.update(kwargs)
        return [("shape", "#000")]
    monkeypatch.setattr(parts, "_connector", fake_connector)
    component = {"name": "J1", "kind": "connector", "mating": "edge", "x": 1.0, "y": 0.0,
                 "length_mm": 4.0, "width_mm": 6.0, "height_mm": 3.0,
                 "board_centre_xy": [10.0, -2.0]}
    parts.build_component(component, 1.6)
    assert seen["board_centre"] == (10.0, -2.0)
    component["board_centre_xy"] = [float("nan"), 0.0]
    parts.build_component(component, 1.6)
    assert seen["board_centre"] == (0.0, 0.0)


def test_build_body_puts_the_board_centre_in_every_component():
    """@brief The FreeCAD script computes the centre from the reflected body box."""
    source = (CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8")
    assert "board_centre = [(x0 + x1) / 2, (y0 + y1) / 2]" in source
    assert 'component.setdefault("board_centre_xy", board_centre)' in source


# ── E10: no invented polarity, caps along the long side ─────────────────

def _extras(result, colour):
    return [shape for shape, value in result if value == colour]


@pytest.mark.parametrize("builder,colour", [("_tantalum", "#f0e6c8"), ("_diode", "#e8e8e8")])
def test_no_polarity_mark_without_an_observed_landmark(parts, builder, colour):
    """@brief Negative: nothing observed, nothing drawn."""
    result = getattr(parts, builder)(0.0, 0.0, 0.0, 3.5, 1.6, 1.2, "#202020")
    assert not _extras(result, colour)


@pytest.mark.parametrize("builder,colour", [("_tantalum", "#f0e6c8"), ("_diode", "#e8e8e8")])
@pytest.mark.parametrize("marker,sign", [((1, 0), 1), ((-1, 0), -1)])
def test_the_polarity_mark_sits_on_the_observed_end(parts, builder, colour, marker, sign):
    """@brief Positive: the stripe/band goes to the end the photo marked."""
    result = getattr(parts, builder)(0.0, 0.0, 0.0, 3.5, 1.6, 1.2, "#202020", marker=marker)
    (mark,) = _extras(result, colour)
    x0, _, x1, _ = mark.bounds()
    assert (x0 + x1) / 2 * sign > 0.5


def test_a_mark_on_a_long_side_is_not_an_end(parts):
    """@brief Negative: a polarity band cannot run along the long side."""
    result = parts._tantalum(0.0, 0.0, 0.0, 3.5, 1.6, 1.2, "#202020", marker=(0, 1))
    assert not _extras(result, "#f0e6c8")


def test_soic_and_qfn_have_no_pin1_dimple_without_evidence(parts):
    """@brief Negative/positive: the dimple is cut only with a marker."""
    plain = parts._ic_qfn(0.0, 0.0, 0.0, 5.0, 5.0, 0.9, "#111")[0][0]
    marked = parts._ic_qfn(0.0, 0.0, 0.0, 5.0, 5.0, 0.9, "#111", marker=(0, 1))[0][0]
    assert plain.cuts == []
    assert len(marked.cuts) == 1 and marked.cuts[0].centre[1] > 0
    soic = parts._ic_soic(0.0, 0.0, 0.0, 5.0, 6.0, 1.5, "#111")[0][0]
    assert soic.cuts == []


def test_the_marker_needs_a_polarity_feature_and_follows_the_rotation(parts):
    """@brief Only polarity/pin-1 landmarks count, converted to the local frame."""
    base = {"landmark_side": "right", "landmark_confidence": "high", "rotation_deg": 0.0}
    assert parts._observed_marker(dict(base, landmark_feature="polarity_marker")) == (1, 0)
    assert parts._observed_marker(dict(base, landmark_feature="connector_opening")) is None
    assert parts._observed_marker(dict(base, landmark_feature="pin1_marker",
                                       landmark_confidence="low")) is None
    # Model-frame rotation -90 (image 90): the image right is local +Y.
    assert parts._observed_marker(dict(base, landmark_feature="pin1_marker",
                                       rotation_deg=-90.0)) == (0, 1)


@pytest.mark.parametrize("length,width", [(3.2, 1.6), (1.6, 3.2)])
def test_chip_terminations_follow_the_long_side(parts, length, width):
    """@brief Positive for both local axes: caps on the two SHORT ends."""
    result = parts._chip(0.0, 0.0, 0.0, length, width, 0.6, "#333")
    caps = _extras(result, "#c8c8c8")
    assert len(caps) == 2
    along_x = length >= width
    for cap in caps:
        x0, y0, x1, y1 = cap.bounds()
        span_along = (x1 - x0) if along_x else (y1 - y0)
        span_across = (y1 - y0) if along_x else (x1 - x0)
        assert span_across == pytest.approx(min(length, width))
        assert span_along < max(length, width) / 2


@pytest.mark.parametrize("length", [0.25, 0.2, 0.1])
def test_a_tiny_chip_is_built_without_raising(parts, length):
    """@brief A sub-0.3 mm chip used to make makeBox fail; now it fits."""
    result = parts._chip(0.0, 0.0, 0.0, length, length / 2, 0.1, "#333")
    for shape, _ in result:
        x0, y0, x1, y1 = shape.bounds()
        assert x0 >= -length / 2 - 1e-9 and x1 <= length / 2 + 1e-9


# ── C12: landmark index location ─────────────────────────────────────────

def test_the_landmark_index_follows_the_parent_cache_folders(parts, tmp_path, monkeypatch):
    """@brief Positive: the folder chosen by the parent comes first, legacy stays."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "legacy"))
    parts.set_cache_dirs([str(tmp_path / "data" / "cache")])
    paths = parts._landmark_cache_paths()
    assert Path(paths[0]).parent == tmp_path / "data" / "cache"
    assert Path(paths[-1]).parent == tmp_path / "legacy" / "kicadgen"


def test_without_parent_folders_the_legacy_location_is_kept(parts, tmp_path, monkeypatch):
    """@brief Negative: an old params file keeps the historic behaviour."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "legacy"))
    parts.set_cache_dirs(None)
    assert parts._landmark_cache_paths() == [
        os.path.join(str(tmp_path / "legacy"), "kicadgen", parts.LANDMARK_INDEX_FILENAME)]
