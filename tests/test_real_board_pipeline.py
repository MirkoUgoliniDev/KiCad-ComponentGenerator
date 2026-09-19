"""@brief Offline GUI-to-FreeCAD integration using a SYNTHETIC extracted fixture.

This proves transport, library resolution and CAD generation, not photographic
detection quality. No photograph, AI inference, network API or pre-resolved
model_path is used. A human-selection fixture confirms a measured candidate.
Only the existing window fixture's VTK display is stubbed;
the actual Qt generation worker, resolver and FreeCAD subprocesses run.
"""

import json
from pathlib import Path

import pytest

from test_photo_inventory_gui import window, gui, QApplication
from PyQt6.QtTest import QTest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d.step_generator import Cad3DError, find_freecadcmd
from kicadgen.services.datasheet_extractor import ExtractedParams


@pytest.fixture
def real_local_installation(monkeypatch, tmp_path):
    """Require local assets, isolating only the writable measurement-cache path."""
    library = km.find_library()
    if library is None:
        pytest.skip("Local KiCad 3D library is not installed")
    model = library / "Resistor_SMD.3dshapes" / "R_0603_1608Metric.step"
    footprint = (library.parent / "footprints" / "Resistor_SMD.pretty"
                 / "R_0603_1608Metric.kicad_mod")
    if not model.is_file() or not footprint.is_file():
        pytest.skip("Local KiCad R_0603 STEP and matching footprint are required")
    try:
        executable = find_freecadcmd()
    except Cad3DError:
        pytest.skip("Local FreeCAD command-line installation is unavailable")
    if not Path(executable).is_file():
        pytest.skip("Configured FreeCAD executable does not exist")
    # Force a fresh real measurement, without changing user/system caches.
    monkeypatch.setattr(km, "_index_path", lambda library: tmp_path / "sizes.json")
    monkeypatch.setenv(km.LIBRARY_ENV, str(library))
    monkeypatch.setenv("FREECAD_CMD_PATH", executable)
    return model.resolve()


def test_synthetic_r0603_through_gui_resolver_and_real_freecad(
        real_local_installation, window, monkeypatch, tmp_path):
    """@brief Verify real imported geometry/colors from synthetic extraction data."""
    expected_model = real_local_installation

    def forbidden_inference(*args, **kwargs):
        pytest.fail("This offline integration must never call an extraction API")

    monkeypatch.setattr(gui, "extract_pin_header_params", forbidden_inference)
    window.i18n.set_language("it")
    window._auto_after_extraction = False
    fixture = ExtractedParams(
        name="SYNTHETIC_R0603_PIPELINE", description="Synthetic integration fixture",
        tags="synthetic offline integration", confidence="low",
        notes="Manually constructed fixture; NOT photographic detection evidence.",
        layout="castellated", body_length_mm=20, body_width_mm=12,
        body_height_mm=2, pcb_thickness_mm=1,
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.54, first_pin=1)],
        components_3d=[dict(
            name="Synthetic_R1", kind="generic", package="R_0603",
            x_mm=3, y_mm=2, length_mm=2.62, width_mm=1.25,
            height_mm=.45, rotation_deg=0, color_hex="#ff00ff",
            bbox_basis="pads",
        )],
    )
    assert not fixture.photo_source and not fixture.photo_png
    assert "model_path" not in fixture.components_3d[0]
    proposal = km.model_candidates(fixture.components_3d[0])
    assert proposal['status'] == 'review_required'
    assert any(Path(c['path']).resolve() == expected_model for c in proposal['candidates'])
    # Simulate explicit acceptance, not an automatic dimensional match.
    fixture.components_3d[0]['model_selection'] = dict(
        status='confirmed', path=str(expected_model), rotation_deg=0)
    window._on_extracted(fixture)
    window.group_3d.setChecked(True)
    assert window._acquisition_completed
    assert window._is_castellated()
    params = window._current_castellated_params()
    params.validate()
    assert params.components[0].package == "R_0603"
    assert params.components[0].length == pytest.approx(2.62)
    assert params.components[0].width == pytest.approx(1.25)
    assert params.pcb_thickness == pytest.approx(1)
    assert window._current_body_params() is not None

    # Exercise the real button handler, worker thread and completion signals.
    # FreeCAD subprocesses have their own bounded timeouts in production.
    window._generate()
    worker = window._worker
    assert worker is not None, window.log_text.toPlainText()
    while worker.isRunning():
        QTest.qWait(25)
    worker.wait()
    QApplication.processEvents()

    project = window._project_dir()
    assert project.is_relative_to(tmp_path)
    stem = fixture.name
    step = project / f"{stem}.3dshapes" / f"{stem}.step"
    preview_path = step.with_suffix(".preview.json")
    assert step.is_file() and step.stat().st_size > 0, window.log_text.toPlainText()
    assert (project / f"{stem}.pretty" / f"{stem}.kicad_mod").is_file()
    assert window._last_mesh_path == preview_path, window.log_text.toPlainText()
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    mounted = [part for part in preview["parts"] if part.get("component_index") == 0]
    assert mounted, "Generated preview must contain the actual mounted component"
    assert all(part["source"] == "kicad_library" for part in mounted)
    assert all(Path(part["model_path"]).resolve() == expected_model for part in mounted)
    assert all(part["native_colors"] is True for part in mounted)
    assert preview["step_colors_exported"] is True
    assert len({part["color"] for part in mounted}) >= 2
    assert "#ff00ff" not in {part["color"].lower() for part in mounted}
    assert all(part["vertices"] and part["triangles"] for part in mounted)

    vertices = [vertex for part in mounted for vertex in part["vertices"]]
    lower = [min(v[axis] for v in vertices) for axis in range(3)]
    upper = [max(v[axis] for v in vertices) for axis in range(3)]
    # Native 0603 solid, NOT a 2.62 x 1.25 photographic-envelope fallback box.
    assert [hi-lo for lo, hi in zip(lower, upper)] == pytest.approx([1.6, .8, .45], abs=.002)
    assert [(lower[i]+upper[i])/2 for i in (0, 1)] == pytest.approx([3, -2], abs=.002)
    assert lower[2] == pytest.approx(1, abs=.002)

    freecad_log = (project / "freecad.log").read_text(encoding="utf-8")
    assert f"KICADGEN_IMPORT_OK:{expected_model}" in freecad_log
    assert "KICADGEN_STEP_COLORS_OK:" in freecad_log
    assert "colori nativi" not in freecad_log  # This text marks color-import fallback.
    gui_log = window.log_text.toPlainText()
    assert "R_0603_1608Metric.step" in gui_log
    assert "Explicitly confirmed KiCad models: 1" in gui_log
    assert "KICADGEN_IMPORT_OK:" in gui_log
