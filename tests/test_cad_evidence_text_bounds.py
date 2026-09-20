"""@brief Verify matching evidence boundaries and real glyph envelope fitting."""
from pathlib import Path
import subprocess

import pytest

from kicadgen.cad3d import step_generator as cad, kicad_models
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide, ComponentBox
from kicadgen.generators.pin_header import PinHeaderParams


@pytest.mark.parametrize('layout', ['castellated', 'matrix'])
def test_matching_evidence_reaches_matcher_without_overriding_geometry(monkeypatch, tmp_path, layout):
    c = ComponentBox(0, 0, 4, 2, 1)
    evidence = dict(presence_confidence='low', class_confidence='medium', confidence='low',
                    _yolo_review_only=True, _yolo_origin='low_confidence_review',
                    terminal_count=3, terminal_layout='two leads and tab',
                    terminal_count_basis='visible_including_tabs',
                    _global_inventory_status='partial')
    c.match_evidence = dict(evidence, x=999, length_mm=999, model_path='injected.step',
                            model_rotation_deg=999, source_row=999)
    seen = []
    monkeypatch.setattr(kicad_models, 'attach_models', lambda components, **kw: seen.extend(components))
    monkeypatch.setattr(cad, '_run_freecad', lambda *a, **kw: {'warnings': []})
    body = Body3DParams(40, 20, 5, populated_board=True, components=[c])
    if layout == 'castellated':
        params = CastellatedModuleParams(name='Evidence', body_length=40, body_width=20,
            sides=[CastellatedSide(edge='left', count=2, pitch=2.54)], components=[c])
        cad.generate_castellated_3d_model(params, body, tmp_path)
    else:
        cad.generate_3d_model(PinHeaderParams(), body, tmp_path)
    assert len(seen) == 1
    result = seen[0]
    assert {key: result[key] for key in evidence} == evidence
    assert result['length_mm'] == 4 and result['source_row'] == 0 and result['x'] != 999
    assert 'model_path' not in result and 'model_rotation_deg' not in result


@pytest.mark.parametrize('value', [None, [], 'invalid'])
def test_missing_evidence_is_not_invented(value):
    c = ComponentBox(0, 0, 4, 2, 1)
    c.match_evidence = value
    assert cad._match_evidence(c) == {}


def test_real_freecad_text_fits_rotated_component_without_truncation(tmp_path):
    try:
        executable = cad.find_freecadcmd()
    except Exception as exc:
        pytest.skip(str(exc))
    if not Path(cad.DEFAULT_FONT_FILE).is_file():
        pytest.skip('Arial unavailable')
    script = tmp_path / 'check_text.py'
    module_dir = str(Path(cad.__file__).parent)
    script.write_text(
        'import sys\n'
        f'sys.path.insert(0, {module_dir!r})\n'
        'import FreeCAD\n'
        'import ast, pathlib, Part\n'
        f'tree=ast.parse(pathlib.Path({module_dir!r}, "freecad_build_body.py").read_text(encoding="utf-8"))\n'
        'tree.body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ("_text_solid","_place_text")]\n'
        'exec(compile(tree,"text_helpers","exec"))\n'
        f'font = {cad.DEFAULT_FONT_FILE!r}\n'
        'for text, length, width in [("LONG OCR " * 3, 2., .6), ("OK", 20., 10.)]:\n'
        ' for local in (0, 90, 37):\n'
        '  original = _place_text(text,.35,0,0,0,0,font,local_angle=local)\n'
        '  placed = _place_text(text,.35,5,-7,3,29,font,bounds=(length,width),local_angle=local)\n'
        '  assert len(placed.Solids) == len(original.Solids), "lost glyph solids"\n'
        '  placed.translate(FreeCAD.Vector(-5,7,-3))\n'
        '  placed.rotate(FreeCAD.Vector(0,0,0),FreeCAD.Vector(0,0,1),-29)\n'
        '  bb=placed.BoundBox\n'
        '  assert bb.XMin >= -length/2-1e-6 and bb.XMax <= length/2+1e-6, (local,"X",bb)\n'
        '  assert bb.YMin >= -width/2-1e-6 and bb.YMax <= width/2+1e-6, (local,"Y",bb)\n'
        '  assert abs(bb.ZLength-.012)<1e-6, ("Z",bb)\n'
        '  if text == "OK": assert abs(bb.XLength-original.BoundBox.XLength)<1e-6\n'
        'print("TEXT_BOUNDS_OK")\n', encoding='utf-8')
    result = subprocess.run([executable, str(script)], capture_output=True, text=True,
                            timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert 'TEXT_BOUNDS_OK' in result.stdout, result.stdout + result.stderr
