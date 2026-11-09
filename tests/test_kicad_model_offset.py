"""@brief The STEP model offset written for a moved footprint anchor (plan F2, contract 7).

The footprint writer moves every pad and graphic by (-ax, -ay) (mm, Y down)
when the user moves the anchor to (ax, ay); the model, built in the
generator's coordinates, must follow through the ``(offset (xyz ...))`` of
its model node. The sign of that offset is MEASURED with KiCad here
(`test_kicad_keeps_the_model_on_the_pads_after_an_anchor_move`), not
reasoned: kicad-cli 10.0.8 places a model with ``(offset (xyz 2 3 0))``
2 mm right of and 3 mm ABOVE the footprint origin (board Y -3), so the
offset's Y axis points up.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
from pathlib import Path

import pytest

from kicadgen.services.kicad_library import (
    ComponentArtifacts,
    attach_3d_model,
    create_preview_project,
    discover_component_artifacts,
    install_component_library,
    model_offset_for_anchor,
)
from kicadgen.services.system_dependencies import find_kicad_cli


FOOTPRINT = """(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""

#: Smallest STEP that discover_component_artifacts accepts (ISO trailer).
STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"

#: The model node attach_3d_model wrote before the offset existed: the
#: default must still produce it byte for byte (plan contract 2).
LEGACY_MODEL_NODE = (
    '\t(model "x.step"\n'
    "\t\t(offset (xyz 0 0 0))\n"
    "\t\t(scale (xyz 1 1 1))\n"
    "\t\t(rotate (xyz 0 0 0))\n"
    "\t)"
)


def _component(tmp_path: Path, offset=(0.0, 0.0, 0.0)) -> ComponentArtifacts:
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    return discover_component_artifacts(component, model_offset=offset)


# ── Pure rule ─────────────────────────────────────────────────────────────

def test_anchor_offset_negates_x_and_keeps_y_because_kicad_model_y_points_up():
    """Footprint shifted by (-ax, -ay) with Y down: the model offset is (-ax, +ay, 0)."""
    assert model_offset_for_anchor(3.0, 1.5) == (-3.0, 1.5, 0.0)
    assert model_offset_for_anchor(-2.25, -0.4) == (2.25, -0.4, 0.0)
    # Negative: the naive same-sign shift is NOT the rule (it would drop the
    # body 2 * ay away from its pads, see the KiCad test below).
    assert model_offset_for_anchor(3.0, 1.5) != (-3.0, -1.5, 0.0)


def test_unmoved_anchor_gives_a_clean_zero_offset_without_negative_zero():
    offset = model_offset_for_anchor(0.0, 0.0)
    assert offset == (0.0, 0.0, 0.0)
    assert all(math.copysign(1.0, v) == 1.0 for v in offset)
    assert model_offset_for_anchor(1e-7, -1e-7) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("bad", [(math.nan, 0.0), (0.0, math.inf), (True, 0.0), ("1", 0.0), (None, 0.0)])
def test_anchor_offset_refuses_values_that_are_not_finite_numbers(bad):
    with pytest.raises(ValueError):
        model_offset_for_anchor(*bad)


# ── Writer ────────────────────────────────────────────────────────────────

def test_default_offset_writes_the_legacy_model_node_byte_for_byte():
    assert LEGACY_MODEL_NODE in attach_3d_model(FOOTPRINT, "x.step")
    assert attach_3d_model(FOOTPRINT, "x.step") == attach_3d_model(FOOTPRINT, "x.step", (0, 0, 0))


def test_offset_is_written_in_the_board_number_format():
    text = attach_3d_model(FOOTPRINT, "x.step", (-1.27, 0.5000001, -0.0))
    assert "(offset (xyz -1.27 0.5 0))" in text
    assert "-0)" not in text and "-0 " not in text
    # Re-attaching replaces the node: one model, the newest offset.
    again = attach_3d_model(text, "y.step", (2, 3, 0))
    assert again.count("(model ") == 1
    assert "(offset (xyz 2 3 0))" in again


@pytest.mark.parametrize("bad", [(1.0, 2.0), (1.0, 2.0, 3.0, 4.0), (math.nan, 0.0, 0.0),
                                 (False, 0.0, 0.0), "abc", 5.0])
def test_attach_refuses_an_offset_that_is_not_three_finite_numbers(bad):
    with pytest.raises(ValueError):
        attach_3d_model(FOOTPRINT, "x.step", bad)


def test_discover_validates_the_offset_before_any_delivery(tmp_path):
    with pytest.raises(ValueError):
        _component(tmp_path, offset=(math.nan, 0.0, 0.0))


# ── Deliveries carry the offset ───────────────────────────────────────────

def test_preview_board_carries_the_artifacts_offset(tmp_path):
    moved = _component(tmp_path / "a", offset=model_offset_for_anchor(1.27, 0.8))
    board = create_preview_project(moved).board_path.read_text(encoding="utf-8")
    assert "(offset (xyz -1.27 0.8 0))" in board
    plain = _component(tmp_path / "b")
    board = create_preview_project(plain).board_path.read_text(encoding="utf-8")
    assert "(offset (xyz 0 0 0))" in board


@pytest.mark.parametrize("project_library", [True, False])
def test_project_and_global_install_carry_the_artifacts_offset(tmp_path, project_library):
    moved = _component(tmp_path / "src", offset=(-2, 0.5, 0))
    table = tmp_path / "lib" / "fp-lib-table"
    if not project_library:
        table.parent.mkdir(parents=True)
        table.write_text("(fp_lib_table\n\t(version 7)\n)\n", encoding="utf-8")
    result = install_component_library(moved, tmp_path / "lib", project_library=project_library,
                                       table_path=table)
    installed = result.footprint_path.read_text(encoding="utf-8")
    assert "(offset (xyz -2 0.5 0))" in installed
    # The component's own file stays without a model node.
    assert "(model" not in moved.footprint_path.read_text(encoding="utf-8")


# ── KiCad itself: the sign, end to end ────────────────────────────────────

#: A box model, hand-written VRML 2.0, in KiCad's WRL unit (0.1 in = 2.54 mm):
#: 2.54 x 1.27 x 0.508 mm from its own origin, asymmetric in X and Y so a
#: flipped axis cannot go unnoticed.
BOX_WRL = """#VRML V2.0 utf8
Shape {
  appearance Appearance { material Material { diffuseColor 1 0 0 } }
  geometry IndexedFaceSet {
    coord Coordinate { point [ 0 0 0, 1 0 0, 1 0.5 0, 0 0.5 0, 0 0 0.2, 1 0 0.2, 1 0.5 0.2, 0 0.5 0.2 ] }
    coordIndex [ 0 1 2 3 -1, 4 5 6 7 -1, 0 1 5 4 -1, 1 2 6 5 -1, 2 3 7 6 -1, 3 0 4 7 -1 ]
  }
}
"""

#: Pads in the GENERATOR's coordinates (mm, Y down) and the anchor the user
#: moves to; neither pad sits on the anchor, so a wrong offset shows on both axes.
GENERATOR_PADS = ((0.0, 0.0), (2.54, 0.0))
ANCHOR = (1.27, 0.8)


def _library_footprint(anchor: tuple[float, float]) -> str:
    ax, ay = anchor
    pads = "".join(
        f'\t(pad "{i}" smd rect (at {x - ax:g} {y - ay:g}) (size 1 1) (layers "F.Cu" "F.Mask"))\n'
        for i, (x, y) in enumerate(GENERATOR_PADS, start=1))
    return f'(footprint "Demo"\n\t(version 20260206)\n\t(generator "kicadgen_tool")\n\t(layer "F.Cu")\n{pads})\n'


def _board(footprint: str) -> str:
    placed = footprint.replace('(footprint "Demo"', '(footprint "T:Demo"', 1).rstrip()
    placed = placed[:-1] + "\t(at 100 100)\n)"
    return ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
            "\t(general (thickness 1.6))\n\t(paper \"A4\")\n"
            '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user) (25 "Edge.Cuts" user))\n'
            f"{placed}\n"
            '\t(gr_rect (start 80 80) (end 120 120) (stroke (width 0.05) (type default)) (fill none)'
            ' (layer "Edge.Cuts"))\n)\n')


def _glb_box(path: Path) -> tuple[list[float], list[float]]:
    """World bounding box, mm, of every mesh of a GLB (glTF axes, whatever their mapping)."""
    data = path.read_bytes()
    gltf = json.loads(data[20:20 + struct.unpack_from("<I", data, 12)[0]])
    lo, hi = [math.inf] * 3, [-math.inf] * 3

    def walk(index, offset):
        node = gltf["nodes"][index]
        # KiCad writes translations only (no rotation for an unrotated model).
        assert "matrix" not in node and node.get("rotation", [0, 0, 0, 1]) == [0, 0, 0, 1], node
        assert node.get("scale", [1, 1, 1]) == [1, 1, 1], node
        here = [a + b for a, b in zip(offset, node.get("translation", [0, 0, 0]))]
        if "mesh" in node:
            for primitive in gltf["meshes"][node["mesh"]]["primitives"]:
                accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
                for i in range(3):
                    lo[i] = min(lo[i], (accessor["min"][i] + here[i]) * 1000.0)
                    hi[i] = max(hi[i], (accessor["max"][i] + here[i]) * 1000.0)
        for child in node.get("children", []):
            walk(child, here)

    for scene in gltf["scenes"]:
        for root in scene["nodes"]:
            walk(root, [0.0, 0.0, 0.0])
    assert all(math.isfinite(v) for v in lo + hi), "no mesh exported"
    return lo, hi


def _model_relative_to_pads(cli: str, folder: Path, footprint: str) -> list[float]:
    """Model box minus pad box (min corners then max corners), mm, as KiCad exports them."""
    folder.mkdir()
    (folder / "box.wrl").write_text(BOX_WRL, encoding="utf-8")
    board = folder / "b.kicad_pcb"
    board.write_text(_board(footprint), encoding="utf-8")
    boxes = []
    for name, flags in (("pads.glb", ["--no-components", "--include-pads", "--no-board-body"]),
                        ("model.glb", ["--no-board-body"])):
        out = folder / name
        done = subprocess.run([cli, "pcb", "export", "glb", *flags, "-f", "-o", str(out), str(board)],
                              capture_output=True, text=True, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # kicad-cli may return 0 without writing: the file is the evidence.
        assert out.is_file(), (done.returncode, done.stdout, done.stderr)
        boxes.append(_glb_box(out))
    (pads_lo, pads_hi), (model_lo, model_hi) = boxes
    return [m - p for m, p in zip(model_lo + model_hi, pads_lo + pads_hi)]


def test_kicad_keeps_the_model_on_the_pads_after_an_anchor_move(tmp_path):
    """Measured with kicad-cli (GLB export): the model sits on the pads as before the move.

    Reference: footprint written with the anchor at the generator origin,
    model offset (0, 0, 0). Moved: pads shifted by -ANCHOR, model offset
    from `model_offset_for_anchor`. The model box relative to the pad box,
    read from KiCad's own export, must match within 0.001 mm. Negative cases:
    no offset, and the same-sign offset (-ax, -ay, 0) a Y-down reasoning
    would give, both move the body off the pads by the predicted amount.
    """
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    model = "${KIPRJMOD}/box.wrl"
    reference = _model_relative_to_pads(
        cli, tmp_path / "ref", attach_3d_model(_library_footprint((0.0, 0.0)), model))
    moved_text = _library_footprint(ANCHOR)
    moved = _model_relative_to_pads(
        cli, tmp_path / "moved", attach_3d_model(moved_text, model, model_offset_for_anchor(*ANCHOR)))
    assert moved == pytest.approx(reference, abs=1e-3)

    ax, ay = ANCHOR
    wrong = {
        "no offset": (0.0, 0.0, 0.0),
        "Y down sign": (-ax, -ay, 0.0),
    }
    for label, offset in wrong.items():
        relative = _model_relative_to_pads(
            cli, tmp_path / label.replace(" ", "_"), attach_3d_model(moved_text, model, offset))
        error = max(abs(a - b) for a, b in zip(relative, reference))
        # Smallest predicted displacement: ax = 1.27 (no offset) or 2 * ay = 1.6.
        assert error > 1.0, (label, relative, reference)


# ── Huge integers and precision (review 2026-11-09) ───────────────────────

#: A JSON integer far beyond a float: math.isfinite raises OverflowError on it.
_HUGE_INT = 10 ** 400


@pytest.mark.parametrize("bad", [(_HUGE_INT, 0.0), (0.0, -_HUGE_INT)])
def test_anchor_offset_refuses_an_int_too_large_for_a_float_with_value_error(bad):
    with pytest.raises(ValueError):
        model_offset_for_anchor(*bad)


@pytest.mark.parametrize("bad", [(_HUGE_INT, 0, 0), (0, 0, -_HUGE_INT)])
def test_writer_refuses_an_int_too_large_for_a_float_with_value_error(bad):
    with pytest.raises(ValueError):
        attach_3d_model("(footprint \"X\"\n)", "m.step", bad)


def test_a_large_but_representable_int_is_still_accepted():
    """Positive: the bound is float conversion, not an arbitrary magnitude."""
    assert model_offset_for_anchor(10 ** 3, 0) == (-1000.0, 0.0, 0.0)


def test_model_offset_keeps_the_precision_of_the_pad_coordinates():
    """The model moves by the same written amount as the pads (format_number, 6 decimals)."""
    from kicadgen.core.sexpr import format_number
    anchor = (0.33333, 1.111111)
    offset = model_offset_for_anchor(*anchor)
    assert offset == (-0.33333, 1.111111, 0.0)
    # A pad at the generator origin is written at (-ax, -ay) after the anchor move.
    pad_x, pad_y = format_number(0.0 - anchor[0]), format_number(0.0 - anchor[1])
    text = attach_3d_model("(footprint \"X\"\n)", "m.step", offset)
    assert f"(offset (xyz {pad_x} {format_number(-float(pad_y))} 0))" in text
    assert "(offset (xyz -0.33333 1.111111 0))" in text
