"""@brief Report failed confirmed STEP imports and retain stable source rows."""

import json
from pathlib import Path
import subprocess

import pytest

from test_observed_terminals import cad, component


@pytest.mark.parametrize("raises", [False, True])
def test_failed_import_marks_magenta_placeholder(cad, monkeypatch, raises):
    module, _ = cad
    def failed(*args):
        if raises:
            raise ValueError("broken\nSTEP\x00" + "x" * 1000)
        return None
    monkeypatch.setattr(module, "_library_model", failed)
    c = component(model_path="corrupt.step", model_selection={"status": "confirmed"})
    parts = module.build_component(c, 1.6)
    assert parts and all(color == "#FF00FF" for _, color in parts)
    assert c["_model_import_failed"] is True
    error = c["_model_import_error"]
    assert isinstance(error, str) and error and len(error) <= 500 and error.isprintable()
    json.dumps({"error": error}, allow_nan=False)


def test_successful_retry_clears_import_failure(cad, monkeypatch):
    module, _ = cad
    expected = [(object(), "#202020")]
    monkeypatch.setattr(module, "_library_model", lambda *a: expected)
    c = component(model_path="repaired.step", _model_import_failed=True, _model_import_error="previous failure")
    assert module.build_component(c, 1.6) == expected
    assert "_model_import_failed" not in c and "_model_import_error" not in c


def test_normal_synthetic_component_has_no_import_failure(cad):
    module, _ = cad
    c = component()
    assert module.build_component(c, 1.6)
    assert "_model_import_failed" not in c and "_model_import_error" not in c


def test_real_freecad_corrupt_step_exports_failure_metadata(tmp_path):
    """@brief Run actual OCC import failure through body generation and mesh export."""
    from kicadgen.cad3d.step_generator import find_freecadcmd
    try:
        executable = find_freecadcmd()
    except Exception as exc:
        pytest.skip(f"FreeCAD unavailable: {exc}")
    corrupt = tmp_path / "corrupt.step"
    corrupt.write_text("ISO-10303-21;\nTHIS IS NOT A VALID STEP FILE\n", encoding="ascii")
    mesh = tmp_path / "preview.json"
    payload = {
        "body": {"length_mm": 40, "width_mm": 30, "height_mm": 1.6,
                 "offset_x_mm": -20, "offset_y_mm": -15},
        "pins": [], "pin_diameter_mm": .5, "pin_length_mm": 2,
        "step_path": str(tmp_path / "result.step"), "mesh_path": str(mesh),
        "components": [component(name="Invalid", length_mm=0, source_row=2),
            component(name="Corrupt", kind="generic", x=0, source_row=7,
                model_path=str(corrupt), model_selection={"status": "confirmed", "path": str(corrupt)}),
            component(name="After", kind="generic", x=-12)],
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    root = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"
    script = tmp_path / "run_corrupt.py"
    script.write_text("import runpy, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"sys.argv = ['freecadcmd', 'build', {str(input_path)!r}]\n"
        f"runpy.run_path({str(root / 'freecad_build_body.py')!r})\n"
        "print('CORRUPT_STEP_FALLBACK_OK', flush=True)\n", encoding="utf-8")
    result = subprocess.run([executable, str(script)], capture_output=True, text=True,
        timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "CORRUPT_STEP_FALLBACK_OK" in output, output
    preview = json.loads(mesh.read_text(encoding="utf-8"))
    failed = [p for p in preview["parts"] if p.get("component_index") == 1]
    assert failed
    related = [p for p in preview['parts'] if p['name'].startswith('Cmp1_')]
    assert related and all(p['source_row'] == 7 for p in related)
    # Contratto della cornice dal 2026-09-20 (3ae0f9e): va solo dove il corpo
    # si confonderebbe col fondo. Il segnaposto magenta dell'import fallito
    # contrasta col PCB scuro, quindi non ne ha; il corpo scuro «After», sullo
    # stesso PCB e nella stessa corsa, si'. Prima il test chiedeva la cornice
    # su ogni componente (regola del 09ac162) ed era sempre saltato senza
    # FreeCAD, per questo il cambio non si era visto.
    assert not any('_silk' in p['name'] for p in related)
    after_silk = [p for p in preview['parts']
                  if p['name'].startswith('Cmp2_') and '_silk' in p['name']]
    assert len(after_silk) == 1 and after_silk[0]['source_row'] == 2
    for part in failed:
        assert part["source"] == "approximation" and part["model_path"] is None
        assert part["source_row"] == 7 and part["model_import_failed"] is True
        assert part["model_import_error"] and len(part["model_import_error"]) <= 500
        assert part["color"].upper() == "#FF00FF"
        assert part["vertices"] and part["triangles"]
    after = [p for p in preview["parts"] if p.get("component_index") == 2]
    assert after and all(p["source_row"] == 2 and not p.get("model_import_failed") for p in after)
    assert not any(p.get("component_index") == 0 for p in preview["parts"])
    assert Path(payload["step_path"]).stat().st_size > 0
