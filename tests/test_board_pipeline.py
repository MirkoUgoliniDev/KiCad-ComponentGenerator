"""Regression coverage across AI decoding, Qt state and actual CAD payloads."""
from pathlib import Path
import sys

import pytest
from test_photo_inventory_gui import window
from kicadgen.services import datasheet_extractor as ext
from kicadgen.cad3d import step_generator as cad, kicad_models
from kicadgen.cad3d.process_log import run_logged


@pytest.mark.parametrize("layout", ["matrix", "castellated"])
def test_package_survives_gui_and_reaches_library_resolution(window, monkeypatch, tmp_path, layout):
    raw = dict(layout=layout, name="Module", body_length_mm=40, body_width_mm=20,
        body_height_mm=5, pcb_thickness_mm=1.5,
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=15,
        pad_diameter_mm=1.7, drill_mm=1,
        castellated_sides=[dict(edge="left", count=4, pitch_mm=2.54)],
        components_3d=[dict(name="R", package="R_0603", kind="resistor", x_mm=2,
            y_mm=-3, length_mm=1.6, width_mm=.8, height_mm=.5,
            bbox_basis="body", package_evidence="two metal ends", mating="none")])
    params = ext._params_from_tool_arguments(raw)
    window._on_extracted(params)
    boxes = window._components_from_table()
    assert boxes[0].package == "R_0603"
    assert boxes[0].bbox_basis == "body"
    captured = {}
    def resolve(components, **kwargs):
        assert components[0]["package"] == "R_0603"
        components[0]["model_path"] = "resolved.step"
        kwargs["log"]("resolved-live")
    monkeypatch.setattr(kicad_models, "attach_models", resolve)
    def build(*args, **kwargs):
        captured.update(kwargs)
        return {"warnings": []}
    monkeypatch.setattr(cad, "_run_freecad", build)
    progress = []
    body = window._current_body_params()
    if layout == "castellated":
        p = window._current_castellated_params()
        cad.generate_castellated_3d_model(p, body, tmp_path, progress=progress.append)
    else:
        p = window._current_params()
        cad.generate_3d_model(p, body, tmp_path, progress=progress.append)
        assert body.pcb_thickness_mm == 1.5
        assert len(captured["notches"]) == 8
    assert captured["extra_payload"]["components"][0]["model_path"] == "resolved.step"
    assert progress == ["resolved-live"]
    assert not captured["extra_payload"].get("silk_texts")


def test_all_gui_logs_are_flushed_once(window):
    window._on_ai_progress("analysis-proof")
    window._append_log("generation-proof")
    text = window._log_file().read_text(encoding="utf-8")
    assert text.count("analysis-proof") == text.count("generation-proof") == 1


@pytest.mark.parametrize("layout", ["matrix", "castellated"])
def test_verified_photo_family_cannot_invent_terminals(window, monkeypatch, tmp_path, layout):
    params = ext._params_from_tool_arguments(dict(
        layout=layout, body_length_mm=40, body_width_mm=20, body_height_mm=5,
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=15,
        pad_diameter_mm=1.7, drill_mm=1,
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.54)],
        components_3d=[dict(name="U", kind="ic_qfn", _detected_kind="ic_qfn",
            package="QFN", x_mm=0, y_mm=0, length_mm=5, width_mm=5, height_mm=1)]))
    window._on_extracted(params)
    captured = {}
    monkeypatch.setattr(kicad_models, "attach_models", lambda components, **kw: None)
    def build(*args, **kwargs):
        captured.update(kwargs["extra_payload"])
        return {"warnings": []}
    monkeypatch.setattr(cad, "_run_freecad", build)
    if layout == "castellated":
        cad.generate_castellated_3d_model(window._current_castellated_params(),
                                         window._current_body_params(), tmp_path)
    else:
        cad.generate_3d_model(window._current_params(), window._current_body_params(), tmp_path)
    component = captured["components"][0]
    assert component["kind"] == "ic"
    assert component["detected_kind"] == "ic_qfn"
    assert component["package"] == "QFN"


def test_process_log_is_readable_before_process_exits(tmp_path):
    path = tmp_path / "freecad.log"
    marker = tmp_path / "callback-seen"
    def on_line(message):
        assert "first" in path.read_text(encoding="utf-8")
        marker.touch()
    script = ("import pathlib,time; print('first',flush=True); "
              f"p=pathlib.Path({str(marker)!r}); "
              "deadline=time.monotonic()+3; "
              "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(.01)'); "
              "assert p.exists(), 'callback was delayed until process exit'")
    result = run_logged([sys.executable, "-c", script], path, progress=on_line, timeout=6)
    assert result.returncode == 0
