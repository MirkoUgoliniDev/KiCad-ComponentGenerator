"""@brief SYNTHETIC tentative-passive GUI-to-FreeCAD regression, with no AI calls.

This verifies application transport, automatic mechanical matching and real CAD,
not photographic recognition. No manual selection or prebound model path is used.
Only the existing window fixture replaces the VTK display and isolates GUI state.
"""
import json
from pathlib import Path

import pytest
from PyQt6.QtTest import QTest

from test_photo_inventory_gui import gui, QApplication
from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d.step_generator import Cad3DError, find_freecadcmd
from kicadgen.services.datasheet_extractor import ExtractedParams


def test_synthetic_tentative_c0603_auto_matches_through_real_gui_and_freecad(
        window, monkeypatch, tmp_path):
    """@brief Prove an unbound tentative passive imports native C0603 geometry/colors."""
    library = km.find_library()
    if library is None:
        pytest.skip('Local KiCad 3D library is required')
    expected_model = (library / 'Capacitor_SMD.3dshapes' / 'C_0603_1608Metric.step').resolve()
    if not expected_model.is_file():
        pytest.skip('Local KiCad C_0603 STEP is required')
    try:
        executable = find_freecadcmd()
    except Cad3DError:
        pytest.skip('Local FreeCAD command-line installation is required')
    if not Path(executable).is_file():
        pytest.skip('Configured FreeCAD executable is unavailable')

    def forbidden_inference(*args, **kwargs):
        pytest.fail('Synthetic regression must never call an extraction API')

    monkeypatch.setattr(gui, 'extract_pin_header_params', forbidden_inference)
    window.i18n.set_language('it')
    window._auto_after_extraction = False
    fixture = ExtractedParams(
        name='SYNTHETIC_TENTATIVE_C0603', description='Synthetic integration fixture',
        tags='synthetic offline regression', confidence='medium',
        notes='Synthetic fixture, NOT a photographic recognition guarantee.',
        layout='castellated', body_length_mm=20, body_width_mm=12,
        body_height_mm=2, pcb_thickness_mm=1,
        castellated_sides=[dict(edge='left', count=2, pitch_mm=2.54, first_pin=1)],
        components_3d=[dict(
            name='Synthetic_C1', kind='capacitor', **{'class': 'capacitor'},
            package='0603 (approx)', x_mm=3, y_mm=2, length_mm=1.69,
            width_mm=.87, height_mm=.9, rotation_deg=0, color_hex='#ff00ff',
            bbox_basis='body', confidence='medium', presence_confidence='high',
            class_confidence='medium', terminal_count=2,
            terminal_count_basis='visible_including_tabs',
            terminal_layout='two metal endcaps', lead_count=2,
            lead_count_complete=True, lead_count_confidence='high',
            pin_count=2, pin_count_confidence='high',
            pin_count_source='complete_lead_observation',
        )],
    )
    assert not fixture.photo_source and not fixture.photo_png
    assert 'model_selection' not in fixture.components_3d[0]
    assert 'model_path' not in fixture.components_3d[0]
    window._on_extracted(fixture)
    window.group_3d.setChecked(True)
    params = window._current_castellated_params()
    params.validate()
    component = params.components[0]
    assert component.package == '0603 (approx)'
    assert component.length == pytest.approx(1.69)
    assert component.width == pytest.approx(.87)
    assert component.model_selection is None
    assert component.pin_count == 2
    assert component.pin_count_source == 'complete_lead_observation'
    assert component.match_evidence['terminal_count_basis'] == 'visible_including_tabs'
    assert component.match_evidence['class'] == 'capacitor'

    window._generate()
    worker = window._worker
    assert worker is not None, window._log_plain_text()
    while worker.isRunning():
        QTest.qWait(25)
    worker.wait()
    QApplication.processEvents()

    project = window._project_dir()
    assert project.is_relative_to(tmp_path)
    step = project / f'{fixture.name}.3dshapes' / f'{fixture.name}.step'
    preview_path = step.with_suffix('.preview.json')
    assert step.is_file() and step.stat().st_size > 0, window._log_plain_text()
    assert window._last_mesh_path == preview_path, window._log_plain_text()
    report = json.loads((project / 'kicad-model-candidates.json').read_text(encoding='utf-8'))
    assert len(report['components']) == 1
    item = report['components'][0]
    assert item.get('model_selection') is None
    assert item['model_match']['status'] == 'auto_matched'
    assert Path(item['model_match']['selected_path']).resolve() == expected_model

    preview = json.loads(preview_path.read_text(encoding='utf-8'))
    mounted = [p for p in preview['parts'] if p.get('component_index') == 0]
    assert mounted
    assert all(p['source'] == 'kicad_library' for p in mounted)
    assert all(Path(p['model_path']).resolve() == expected_model for p in mounted)
    assert all(p['native_colors'] is True for p in mounted)
    assert preview['step_colors_exported'] is True
    colors = {p['color'].lower() for p in mounted}
    assert len(colors) >= 2 and '#ff00ff' not in colors
    assert all(p['vertices'] and p['triangles'] for p in mounted)
    vertices = [v for p in mounted for v in p['vertices']]
    lower = [min(v[axis] for v in vertices) for axis in range(3)]
    upper = [max(v[axis] for v in vertices) for axis in range(3)]
    # Installed native C0603, not the supplied 1.69 x .87 x .9 envelope.
    assert [upper[i] - lower[i] for i in range(3)] == pytest.approx([1.6, .8, .8], abs=.002)
    assert [(lower[i] + upper[i]) / 2 for i in (0, 1)] == pytest.approx([3, -2], abs=.002)
    assert lower[2] == pytest.approx(1, abs=.002)
    freecad_log = (project / 'freecad.log').read_text(encoding='utf-8')
    assert f'KICADGEN_IMPORT_OK:{expected_model}' in freecad_log
    assert 'KICADGEN_STEP_COLORS_OK:' in freecad_log
