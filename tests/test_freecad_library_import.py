"""@brief Verify native STEP colors, placement, and isolated component failures."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from test_observed_terminals import cad, component


@pytest.mark.parametrize("key", ["length_mm", "width_mm", "height_mm"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), None, "bad", True])
def test_invalid_dimensions_skip_before_occ(cad, capsys, key, value):
    module, made = cad
    assert module.build_component(component(**{key: value}), 1.6) == []
    assert made == []
    assert "skipped: invalid geometry" in capsys.readouterr().out


def test_failed_generic_fallback_does_not_rethrow(cad, capsys, monkeypatch):
    module, _ = cad

    def fail(*args):
        raise RuntimeError("OCC cannot make this box")

    monkeypatch.setattr(module, "_generic", fail)
    monkeypatch.setitem(module.BUILDERS, "generic", fail)
    assert module.build_component(component(kind="generic"), 1.6) == []
    assert "generic fallback failed" in capsys.readouterr().out


@pytest.mark.parametrize("size", [(4, 4, 2), (14, 14, 4), (8, 3, 2)])
@pytest.mark.parametrize("mating", [None, "none", "invalid"])
def test_unknown_mating_never_infers_connector_type(cad, capsys, size, mating):
    module, made = cad
    parts = module._connector(0, 0, 1.6, *size, "#303030", mating=mating)
    assert len(parts) == len(made) == 1
    assert "mating sconosciuto" in capsys.readouterr().out


@pytest.mark.parametrize("size", [(4, 4, 2), (14, 14, 4)])
def test_declared_mating_reaches_generic_mechanical_envelope(cad, monkeypatch, size):
    module, _ = cad
    monkeypatch.setattr(module, "_coaxial_connector", lambda *args: ["observed top"])
    assert module.build_component(component(kind="generic", mating="top",
        length_mm=size[0], width_mm=size[1], height_mm=size[2]), 1.6) == ["observed top"]


def test_real_freecad_kicad_step_roundtrip(tmp_path):
    """@brief Exercise actual FreeCAD import, body export, preview and reimport."""
    from kicadgen.cad3d.step_generator import find_freecadcmd

    try:
        executable = find_freecadcmd()
    except Exception as exc:
        pytest.skip(f"FreeCAD unavailable: {exc}")
    library = Path(os.environ.get("KICAD10_3DMODEL_DIR",
        "C:/Program Files/KiCad/10.0/share/kicad/3dmodels"))
    model = library / "Package_TO_SOT_SMD.3dshapes/SOT-223.step"
    if not model.is_file():
        pytest.skip("Real KiCad SOT-223 STEP unavailable")
    root = Path(__file__).resolve().parents[1]
    payload = {
        "body": {"length_mm": 40, "width_mm": 30, "height_mm": 1.6,
                 "offset_x_mm": -20, "offset_y_mm": -15},
        "pins": [], "pin_diameter_mm": 0.5, "pin_length_mm": 2,
        "notches": [{"x": 0, "y": 0, "radius": 0.5}],
        "edge_contacts": [{"number": "1", "x": 0, "y": 0, "radius": 0.5,
                           "edge": "through", "pad_width_mm": 2}],
        "step_path": str(tmp_path / "actual.step"),
        "mesh_path": str(tmp_path / "actual.preview.json"),
        "wrl_path": str(tmp_path / "actual.wrl"),
        "components": [
            component(name="Bad", length_mm=0),
            component(name="Real", kind="generic", x=10, y=5,
                      length_mm=6.5, width_mm=7, height_mm=2,
                      rotation_deg=30, model_rotation_deg=90, model_path=str(model)),
            component(name="After", kind="generic", x=-10, y=0),
            component(name="BadY", y="invalid"),
            component(name="Missing", kind="generic", model_path=str(tmp_path / "missing.step")),
        ],
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    script = tmp_path / "verify.py"
    script.write_text(
        "import json, runpy, sys, traceback\n"
        "import FreeCAD, Part, Import\n"
        "try:\n"
        f"    sys.path.insert(0, {str(root / 'kicadgen/cad3d')!r})\n"
        f"    sys.argv = ['freecadcmd', 'build', {str(input_path)!r}]\n"
        f"    runpy.run_path({str(root / 'kicadgen/cad3d/freecad_build_body.py')!r})\n"
        "    d = FreeCAD.newDocument('roundtrip')\n"
        f"    imported = Import.insert({payload['step_path']!r}, d.Name)\n"
        "    colors = {tuple(round(v, 3) for v in c[:3]) for o, cs in imported for c in cs}\n"
        "    assert {(0.148,0.145,0.145), (0.824,0.82,0.781), (0.691,0.664,0.598)} <= colors, colors\n"
        "    source = FreeCAD.getDocument('kicadgen_body').getObject('Cmp1_Real_0').Shape\n"
        f"    original = Part.read({str(model)!r})\n"
        "    assert abs(source.Volume - original.Volume) < 1e-6\n"
        "    assert len(source.Vertexes) > 8\n"
        "    box = original.BoundBox\n"
        "    original.translate(FreeCAD.Vector(-(box.XMin+box.XMax)/2, -(box.YMin+box.YMax)/2,0))\n"
        "    original.rotate(FreeCAD.Vector(), FreeCAD.Vector(0,0,1), 60)\n"
        "    original.translate(FreeCAD.Vector(10,-5,1.6))\n"
        "    assert abs(source.Solids[0].CenterOfMass.x-original.Solids[0].CenterOfMass.x) < 1e-6\n"
        "    assert abs(source.Solids[0].CenterOfMass.y-original.Solids[0].CenterOfMass.y) < 1e-6\n"
        "    assert abs(source.BoundBox.ZMin-original.BoundBox.ZMin) < 1e-6\n"
        "    assert abs(source.BoundBox.XLength-original.BoundBox.XLength) < 1e-6\n"
        f"    imported_shape = Part.read({payload['step_path']!r})\n"
        "    assert imported_shape.isValid() and len(imported_shape.Solids) >= 4\n"
        "    pad = FreeCAD.getDocument('kicadgen_body').getObject('Pin1').Shape.cleaned()\n"
        "    assert abs(pad.BoundBox.XLength-2) < 1e-6 and abs(pad.BoundBox.YLength-2) < 1e-6, str(pad.BoundBox)\n"
        "    assert not pad.isInside(FreeCAD.Vector(0,0,0.8), 1e-6, True)\n"
        "    print('REAL_FREECAD_ROUNDTRIP_OK', flush=True)\n"
        "except Exception:\n"
        "    traceback.print_exc()\n", encoding="utf-8")
    result = subprocess.run([executable, str(script)], capture_output=True, text=True,
                            timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "REAL_FREECAD_ROUNDTRIP_OK" in output, output
    assert "KICADGEN_IMPORT_OK:" in output
    assert "KICADGEN_STEP_COLORS_OK:" in output
    assert "KICADGEN_COMPONENT_SKIPPED:Cmp0_Bad" in output
    preview = json.loads(Path(payload["mesh_path"]).read_text())
    assert preview["step_colors_exported"] is True
    assert next(part for part in preview["parts"] if part["name"] == "Pin1")["label"][:2] == [0, 0]
    real = [part for part in preview["parts"] if part.get("source") == "kicad_library"]
    assert {part["color"] for part in real} == {"#262525", "#d2d1c7", "#b0a998"}
    assert all(part["model_path"] == str(model) and part["native_colors"] for part in real)
    assert all(part["vertices"] and part["triangles"] for part in real)
    assert any(part["name"].startswith("Cmp2_After") for part in preview["parts"])
    assert any(part.get("source") == "approximation" and part["name"].startswith("Cmp4_Missing")
               for part in preview["parts"])
