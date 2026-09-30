"""Real FreeCAD check of the angle carried from rendering to consolidation."""
import json
from pathlib import Path
import subprocess

import pytest


def test_automatic_axis_plus_manual_delta_reaches_archive(tmp_path):
    from kicadgen.cad3d.step_generator import find_freecadcmd, _reconcile_model_imports
    from kicadgen.services.accepted_models import _explicit_rotation
    try:
        executable = find_freecadcmd()
    except Exception as exc:
        pytest.skip(f'FreeCAD unavailable: {exc}')
    root = Path(__file__).resolve().parents[1] / 'kicadgen' / 'cad3d'
    model = tmp_path / 'test.step'
    mesh = tmp_path / 'preview.json'
    component = dict(name='Q1', kind='ic', source_row=0, x=0, y=0,
                     length_mm=4, width_mm=2, height_mm=1, rotation_deg=90,
                     model_path=str(model), model_rotation_delta_deg=90,
                     model_match={'status': 'auto_matched'})
    payload = dict(body=dict(length_mm=20, width_mm=20, height_mm=1.6,
                            offset_x_mm=-10, offset_y_mm=-10),
                   pins=[], pin_diameter_mm=.5, pin_length_mm=2,
                   step_path=str(tmp_path / 'result.step'), mesh_path=str(mesh),
                   components=[component])
    input_path = tmp_path / 'input.json'
    input_path.write_text(json.dumps(payload), encoding='utf-8')
    script = tmp_path / 'run.py'
    script.write_text(
        "import Part, runpy, sys\n"
        f"Part.makeBox(2, 4, 1).exportStep({str(model)!r})\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"sys.argv = ['freecadcmd', 'build', {str(input_path)!r}]\n"
        f"runpy.run_path({str(root / 'freecad_build_body.py')!r})\n",
        encoding='utf-8')
    result = subprocess.run([executable, str(script)], capture_output=True, text=True,
                            timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
    preview = json.loads(mesh.read_text(encoding='utf-8'))
    parts = [p for p in preview['parts'] if p.get('source') == 'kicad_library']
    assert parts and all(p['model_local_rotation_deg'] == 180 for p in parts)
    _reconcile_model_imports({'mesh_path': mesh}, [component], tmp_path)
    assert component['model_match']['rendered_local_rotation_deg'] == 180
    # Local automatic base 90 + menu correction 90; board rotation is separate.
    assert _explicit_rotation(component) == 90
    assert _explicit_rotation(component) + component['model_rotation_delta_deg'] == 180
