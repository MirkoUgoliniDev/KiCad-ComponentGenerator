"""@brief Review fixes E1-E6 of the 3D generation (2026-10-13).

* E1 -- a single package with an orientation turns its 3D body (and the
  pin-number labels lying on it) like its pads and F.Fab outline, at any angle;
* E2 -- the ic_soic/ic_qfn fallback draws side-specific leads only from the
  sides moved into the builder's local frame, never from image sides;
* E3 -- a headers-only regeneration that reuses the model choices keeps the
  "estimated height reduced" notes and the model-resolution messages;
* E4 -- a failing progress callback never leaves the FreeCAD child running;
* E5 -- the model-measuring child gets the same no-window flags as the solid's;
* E6 -- the newest FreeCAD installation is chosen by numeric version.

No FreeCAD is needed: the solid builder is replaced by captures, and the
FreeCAD-side helpers are loaded from the script source without FreeCAD.
"""
import ast
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import kicad_models, process_log, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.lead_sides import local_lead_sides
from kicadgen.core.package_geometry import PackageGeometry, PinSpec, pad_outline
from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, ComponentBox)
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams
from tests.test_fixE_parts_orientation import parts, flip_y  # noqa: F401 - shared fixtures
from tests.test_review_1027_sweep import fake_freecad_writing_the_step

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"


def _script_functions(*names):
    """@brief Load top-level functions of freecad_build_body.py without FreeCAD."""
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name in names)
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "_FB_SWAP" for t in n.targets))]
    namespace = {"math": math}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<script>", "exec"), namespace)
    return namespace


def _capture_payload(monkeypatch):
    """@brief Replace freecadcmd with a fake that records the JSON payload.

    @note CONTRACT CHANGED 2026-10-27 (review sweep, S4): every `_run_freecad`
        call now goes through `process_log.run_logged`, also without progress
        and cancel, so the double moved from `subprocess.run` to the
        `subprocess.Popen` that run_logged starts the child with
        (`tests.test_review_1027_sweep.fake_freecad_writing_the_step`). What
        this helper records -- the payload the child receives -- is unchanged.
    """
    seen = []
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: "freecadcmd")
    fake_freecad_writing_the_step(monkeypatch, seen)
    return seen


def _package(angle, **changes):
    values = dict(name="ORI", family="custom", body_length_mm=8.0, body_width_mm=2.0,
                  body_height_mm=1.0, orientation_deg=angle,
                  pins=[PinSpec("1", -3.0, 0.0, 1.0, 0.6), PinSpec("2", 3.0, 0.0, 1.0, 0.6)])
    values.update(changes)
    return PackageGeometry(**values)


def _model_body_corners(payload):
    """@brief Body corners as FreeCAD places them, back in footprint axes (Y down).

    Production chain: `_flip_y` reflects the payload, the body box is built
    axis-aligned at the reflected offsets and turned by -rotation_deg about
    the origin (the same call the script applies to extra boxes).
    """
    data = _script_functions("_flip_y")["_flip_y"](deepcopy(payload))
    body = data["body"]
    turn = math.radians(-float(body.get("rotation_deg", 0.0)))
    x0, y0 = body["offset_x_mm"], body["offset_y_mm"]
    corners = []
    for x, y in ((x0, y0), (x0 + body["length_mm"], y0),
                 (x0 + body["length_mm"], y0 + body["width_mm"]), (x0, y0 + body["width_mm"])):
        mx = x * math.cos(turn) - y * math.sin(turn)
        my = x * math.sin(turn) + y * math.cos(turn)
        corners.append((round(mx, 6) + 0.0, round(-my, 6) + 0.0))
    return sorted(corners)


# -- E1: the body turns with the package --------------------------------------------

@pytest.mark.parametrize("angle", [30.0, 45.0, 90.0, 210.0, -60.0])
def test_turned_package_body_matches_the_footprint_outline(monkeypatch, tmp_path, angle):
    """Positive: the 3D body corners are the F.Fab corners of the footprint, any angle."""
    seen = _capture_payload(monkeypatch)
    package = _package(angle)
    step_generator.generate_single_package_3d_model(package, Body3DParams(10, 10, 5), tmp_path)
    expected = sorted((round(x, 6) + 0.0, round(y, 6) + 0.0) for x, y in
                      pad_outline(0.0, 0.0, 8.0, 2.0, angle))
    assert _model_body_corners(seen[0]) == pytest.approx(expected, abs=1e-6)
    # The F.Fab outline the footprint draws has the same corners.
    footprint = build_package(package)
    fab_points = set()
    for graphic in footprint.graphics:
        # The ${REFERENCE} text on F.Fab (L-3, 2026-10-14) is not outline.
        if getattr(graphic, "layer", "") != "F.Fab" or type(graphic).__name__ == "FpText":
            continue
        (sx, sy), (ex, ey) = graphic.start, graphic.end
        # A rectangle (multiple of 90 degrees) stores two opposite corners.
        ends = ([(sx, sy), (ex, ey), (sx, ey), (ex, sy)] if type(graphic).__name__ == "FpRect"
                else [(sx, sy), (ex, ey)])
        fab_points |= {(round(x, 6) + 0.0, round(y, 6) + 0.0) for x, y in ends}
    fab_points = sorted(fab_points)
    assert fab_points == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("angle", [0.0, 360.0, -720.0])
def test_unturned_package_payload_is_unchanged(monkeypatch, tmp_path, angle):
    """Negative: a full turn writes no rotation and the body stays axis-aligned."""
    seen = _capture_payload(monkeypatch)
    step_generator.generate_single_package_3d_model(_package(angle), Body3DParams(10, 10, 5),
                                                    tmp_path)
    body = seen[0]["body"]
    assert "rotation_deg" not in body
    assert (body["offset_x_mm"], body["offset_y_mm"], body["length_mm"], body["width_mm"]) == (
        -4.0, -1.0, 8.0, 2.0)
    assert _model_body_corners(seen[0]) == [(-4.0, -1.0), (-4.0, 1.0), (4.0, -1.0), (4.0, 1.0)]


def _tht(angle):
    return _package(angle, body_length_mm=10.0, body_width_mm=3.0, pins=[
        PinSpec(str(i + 1), -3.81 + 2.54 * i, 0.0, 1.6, 1.6, pad_type="thru_hole", drill_mm=1.0)
        for i in range(4)])


@pytest.mark.parametrize("angle", [30.0, 90.0, 135.0])
def test_pin_labels_are_placed_in_the_body_frame(monkeypatch, tmp_path, angle):
    """Positive: labels are the unturned ones, so FreeCAD turns them with their body.

    Turned with the body, each label lands beside its OWN lead (nearest lead)
    and inside the turned body outline, whatever the angle.
    """
    captured = []

    def fake_run(name, pins, body, output_dir, **options):
        captured.append(dict(options, pins=pins))
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    body = Body3DParams(10, 10, 5, pin_numbers_3d=True)
    step_generator.generate_single_package_3d_model(_tht(0.0), body, tmp_path)
    step_generator.generate_single_package_3d_model(_tht(angle), body, tmp_path)
    flat, turned = captured
    assert turned["body_rotation_deg"] == angle
    assert [(t.text, t.x_mm, t.y_mm) for t in turned["face_texts"]] == pytest.approx(
        [(t.text, t.x_mm, t.y_mm) for t in flat["face_texts"]])
    a = math.radians(angle)
    leads = {str(n): (x, y) for n, x, y in turned["pins"]}
    for label in turned["face_texts"]:
        # KiCad turn of the label anchor (footprint frame, Y down).
        lx = label.x_mm * math.cos(a) + label.y_mm * math.sin(a)
        ly = -label.x_mm * math.sin(a) + label.y_mm * math.cos(a)
        nearest = min(leads, key=lambda n: math.hypot(leads[n][0] - lx, leads[n][1] - ly))
        assert nearest == label.text
        # Back in the body frame the anchor is inside the 10 x 3 mm body.
        assert abs(label.x_mm) <= 5.0 and abs(label.y_mm) <= 1.5


def test_a_turned_body_refuses_edge_contacts_and_a_non_finite_angle(tmp_path):
    """Negative: the turn is defined for a single body, not for an edge-plated board."""
    body = Body3DParams(10, 10, 1)
    with pytest.raises(ValueError):
        step_generator._run_freecad("X", [], body, tmp_path, freecadcmd_path="x",
                                    edge_contacts=[{"number": "1"}], body_rotation_deg=30.0)
    with pytest.raises(ValueError):
        step_generator._run_freecad("X", [], body, tmp_path, freecadcmd_path="x",
                                    body_rotation_deg=float("nan"))


@pytest.mark.parametrize("angle", [30.0, 45.0, 90.0, 300.0])
def test_holes_are_brought_into_the_turned_body_frame(flip_y, angle):
    """Positive: a hole turned back with the body ends exactly where it was given."""
    helpers = _script_functions("_body_local_point")
    data = flip_y({"body": {"offset_y_mm": -1.0, "width_mm": 2.0, "rotation_deg": angle},
                   "pins": []})
    turn = data["body"]["rotation_deg"]
    assert turn == -angle
    x, y = helpers["_body_local_point"](1.5, -0.7, turn)
    back = math.radians(-turn)
    assert (x * math.cos(back) - y * math.sin(back),
            x * math.sin(back) + y * math.cos(back)) == pytest.approx((1.5, -0.7))
    assert (x, y) != pytest.approx((1.5, -0.7))


def test_body_frame_point_inverts_the_package_rotation():
    """Positive and negative: inverse of pins_seen_from_top; 0 is the identity."""
    package = _package(37.0)
    for original, seen in zip(package.pins, package.pins_seen_from_top()):
        assert step_generator.body_frame_point(seen.x_mm, seen.y_mm, 37.0) == pytest.approx(
            (original.x_mm, original.y_mm))
    assert step_generator.body_frame_point(1.25, -2.5, 0.0) == (1.25, -2.5)


# -- E2: IC fallback from local-frame sides only ------------------------------------

def _qfn(rotation, **changes):
    raw = {"top": 4, "bottom": 4}
    component = {"name": "Q", "kind": "ic_qfn", "x": 0.0, "y": 0.0, "length_mm": 5.0,
                 "width_mm": 4.0, "height_mm": 1.0, "rotation_deg": rotation,
                 "pins_by_side": raw, "lead_sides_local": local_lead_sides(raw, rotation)}
    component.update(changes)
    return component


@pytest.mark.parametrize("kind", ["ic_qfn", "ic_soic"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 88.0])
def test_fallback_leads_end_on_the_photographed_sides(parts, flip_y, kind, rotation):
    """Positive: leads seen on the image top/bottom stay on the image top/bottom.

    For ic_soic the observed-lead branch handles it, for ic_qfn the fallback:
    both must agree with the photograph after the production `_flip_y` and the
    rotation the script applies.
    """
    data = flip_y({"body": {"offset_y_mm": 0.0, "width_mm": 10.0},
                   "components": [_qfn(rotation, kind=kind)]})
    component = data["components"][0]
    cad = math.radians(component["rotation_deg"])
    result = parts.build_component(component, 1.6)
    pads = [shape for shape, colour in result if colour == parts.METAL]
    assert len(pads) == 8
    for pad in pads:
        x0, y0, x1, y1 = pad.bounds()
        px, py = (x0 + x1) / 2, (y0 + y1) / 2
        image_x = px * math.cos(cad) - py * math.sin(cad)
        image_y = -(px * math.sin(cad) + py * math.cos(cad))
        assert abs(image_y) > abs(image_x), "a lead left the photographed top/bottom sides"


@pytest.mark.parametrize("changes,count", [({}, 8), ({"pin_count": 6}, 6),
                                           ({"lead_sides_local": None}, 8)])
def test_without_local_sides_only_the_count_is_used(parts, monkeypatch, changes, count):
    """Negative: image sides are never passed to the local builder; a warning says so.

    Contract change (review G4): the third case used to be a payload without
    `lead_sides_local` at 0 degrees, which also fell back to the count. At a
    multiple of 90 degrees the image sides map exactly to the local ones, so
    such a payload now gets them recomputed (see
    test_missing_local_sides_are_recomputed_from_the_image_sides); only an
    angle exactly between two quarter turns (45 degrees here) still has no
    local sides and falls back to the count.
    """
    calls, warnings = [], []
    monkeypatch.setattr(parts, "_ic_observed_pins",
                        lambda *a, **k: calls.append(k) or [("body", "#000")])
    monkeypatch.setattr(parts, "_warn", lambda template, **v: warnings.append(template))
    parts.build_component(_qfn(45.0, **changes), 1.6)
    assert calls == [{"pin_count": count, "pins_by_side": None, "marker": None}]
    assert any("non riportabili" in template for template in warnings)


@pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0, -90.0, 30.0])
def test_missing_local_sides_are_recomputed_from_the_image_sides(parts, flip_y, monkeypatch,
                                                                rotation):
    """Review G4, positive: an old payload without `lead_sides_local` gets the writer's sides.

    Asymmetric sides (three on the image left, one on the image right) make a
    wrong sign of the angle or a count-only spread visible; the payload goes
    through the production `_flip_y`, which negates the angle, as in FreeCAD.
    """
    raw = {"left": 3, "right": 1, "top": 0, "bottom": 0}
    calls, warnings = [], []
    monkeypatch.setattr(parts, "_ic_observed_pins",
                        lambda *a, **k: calls.append(k) or [("body", "#000")])
    monkeypatch.setattr(parts, "_warn", lambda template, **v: warnings.append(template))
    legacy = _qfn(rotation, pins_by_side=raw)
    del legacy["lead_sides_local"]
    data = flip_y({"body": {"offset_y_mm": 0.0, "width_mm": 10.0}, "components": [legacy]})
    parts.build_component(data["components"][0], 1.6)
    expected = local_lead_sides(raw, rotation)
    assert expected and calls[0]["pins_by_side"] == expected
    assert not any("non riportabili" in template for template in warnings)


def test_unmappable_missing_local_sides_still_fall_back_to_the_count(parts, flip_y, monkeypatch):
    """Review G4, negative: at 45 degrees no side is nearest, so only the count is used."""
    raw = {"left": 3, "right": 1}
    calls, warnings = [], []
    monkeypatch.setattr(parts, "_ic_observed_pins",
                        lambda *a, **k: calls.append(k) or [("body", "#000")])
    monkeypatch.setattr(parts, "_warn", lambda template, **v: warnings.append(template))
    legacy = _qfn(45.0, pins_by_side=raw)
    del legacy["lead_sides_local"]
    data = flip_y({"body": {"offset_y_mm": 0.0, "width_mm": 10.0}, "components": [legacy]})
    parts.build_component(data["components"][0], 1.6)
    assert calls == [{"pin_count": 4, "pins_by_side": None, "marker": None}]
    assert any("non riportabili" in template for template in warnings)


def test_raw_side_total_counts_numpy_integers_but_not_bools_or_floats(parts):
    """Review G4: a count is integral by abstract type, as in lead_sides._integral."""
    import numpy as np
    assert parts._raw_side_total({"left": np.int64(3), "right": np.int32(2)}) == 5
    assert parts._raw_side_total({"left": True, "right": 2.0, "top": -1}) is None


def test_local_sides_are_passed_unchanged(parts, monkeypatch):
    """Positive: with local sides the builder receives them, not the image ones."""
    calls, warnings = [], []
    monkeypatch.setattr(parts, "_ic_observed_pins",
                        lambda *a, **k: calls.append(k) or [("body", "#000")])
    monkeypatch.setattr(parts, "_warn", lambda template, **v: warnings.append(template))
    parts.build_component(_qfn(90), 1.6)
    assert calls[0]["pins_by_side"] == {"left": 4, "right": 4}
    assert not any("non riportabili" in template for template in warnings)


# -- E3: notes survive the header-only reuse -----------------------------------------

def _tall_board(castellated):
    tall = ComponentBox(x=0, y=0, length=3, width=2, height=9.0, name="TALL")
    if castellated:
        params = CastellatedModuleParams(name="R", body_length=20, body_width=12,
                                         sides=[CastellatedSide(edge="left", count=3, pitch=2.54)],
                                         components=[tall])
        return params, Body3DParams(20, 12, 3, -10, -6, show_pins_3d=False), \
            step_generator.generate_castellated_3d_model
    params = PinHeaderParams(name="R")
    return params, Body3DParams(20, 12, 3, -10, -6, populated_board=True, components=[tall],
                                show_pins_3d=False), step_generator.generate_3d_model


def _two_runs(monkeypatch, tmp_path, castellated, model_path):
    searches = []

    def attach(components, log=None, **kwargs):
        searches.append(True)
        log("library message: TALL resolved")
        for component in components:
            if model_path:
                component["model_path"] = str(model_path)

    def build(name, pins, body, output_dir, **kwargs):
        step = output_dir / f"{name}.step"
        step.write_text(f"headers={body.show_pins_3d}", encoding="utf-8")
        return {"step_path": step, "warnings": [], "notes": []}

    monkeypatch.setattr(kicad_models, "attach_models", attach)
    monkeypatch.setattr(step_generator, "_run_freecad", build)
    import kicadgen.services.model_choices as model_choices
    monkeypatch.setattr(model_choices, "save_candidate_report", lambda *a: None)
    params, body, generate = _tall_board(castellated)
    first = generate(params, body, tmp_path)
    second = generate(params, replace(body, show_pins_3d=True), tmp_path)
    assert len(searches) == 1, "the second run must be the reuse path"
    return first, second


@pytest.mark.parametrize("castellated", [True, False])
def test_reuse_keeps_the_reduced_height_and_model_notes(monkeypatch, tmp_path, castellated):
    """Positive: no library model -> the same height note and messages on both runs."""
    first, second = _two_runs(monkeypatch, tmp_path, castellated, model_path=None)
    tall = [n for n in first["notes"] if "TALL" in n and "library message" not in n]
    assert len(tall) == 1 and "9.00" in tall[0], "first run must report the reduction"
    assert [n for n in second["notes"] if "TALL" in n and "library message" not in n] == tall
    assert "library message: TALL resolved" in second["notes"]


@pytest.mark.parametrize("castellated", [True, False])
def test_reuse_drops_the_height_note_of_a_library_model(monkeypatch, tmp_path, castellated):
    """Negative: a body imported from the library has no reduced-height note, on both runs."""
    model = tmp_path / "model.step"
    model.write_text("model", encoding="utf-8")
    first, second = _two_runs(monkeypatch, tmp_path, castellated, model_path=model)
    height = [n for run in (first, second) for n in run["notes"]
              if "TALL" in n and "library message" not in n]
    assert height == []
    assert "library message: TALL resolved" in second["notes"]


def test_reused_height_notes_pair_by_source_row():
    """Positive and negative on the pairing itself."""
    fresh = [({"source_row": 0}, "note A"), ({"source_row": 1}, "note B"),
             ({"source_row": 5}, "note C")]
    reused = [{"source_row": 0, "model_path": "a.step"}, {"source_row": 1}]
    assert step_generator.reused_height_notes(fresh, reused) == ["note B", "note C"]


# -- E4: a failing progress callback kills the child ---------------------------------

def _child(tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import time\nprint('line', flush=True)\ntime.sleep(60)\n"
                      "print('late', flush=True)\n", encoding="utf-8")
    return [sys.executable, str(script)]


def test_failing_progress_kills_the_child_and_re_raises(tmp_path):
    """Positive: the exception reaches the caller in seconds, not after the child's 60 s."""
    def progress(line):
        raise RuntimeError("window deleted")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="window deleted"):
        process_log.run_logged(_child(tmp_path), tmp_path / "log.txt", progress=progress,
                               timeout=120)
    assert time.monotonic() - started < 20


def test_ordinary_progress_still_reads_to_the_end(tmp_path):
    """Negative: without an error the child runs to completion and nothing is killed."""
    script = tmp_path / "quick.py"
    script.write_text("print('one', flush=True)\nprint('two', flush=True)\n", encoding="utf-8")
    lines = []
    result = process_log.run_logged([sys.executable, str(script)], tmp_path / "log.txt",
                                    progress=lines.append, timeout=60)
    assert result.returncode == 0 and len(lines) == 2


# -- E5: no console window for the measuring child -----------------------------------

def test_measure_batch_uses_the_shared_no_window_flags(monkeypatch):
    """Positive: the measuring Popen receives the same flags as the solid's child."""
    seen = {}

    def fake_popen(command, **kwargs):
        seen.update(kwargs)
        raise OSError("not started")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    outcome, _, _ = kicad_models._measure_batch("freecadcmd", [], 5, None)
    assert outcome == "failed"
    assert seen["creationflags"] == process_log.NO_WINDOW_FLAGS == step_generator._CFLAGS


def test_no_window_flags_only_on_windows():
    """Negative: other platforms get no Windows-only flag."""
    assert process_log.no_window_flags("linux") == 0
    assert process_log.no_window_flags("darwin") == 0
    assert process_log.no_window_flags("win32") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


# -- E6: numeric version order of FreeCAD installations -------------------------------

def _installed(monkeypatch, paths):
    monkeypatch.delenv("FREECAD_CMD_PATH", raising=False)
    monkeypatch.setattr(step_generator.glob, "glob",
                        lambda pattern: list(paths) if "Program Files\\FreeCAD" in pattern
                        and "(x86)" not in pattern else [])


def test_the_highest_numeric_version_wins(monkeypatch):
    """Positive: 1.10 > 1.9 > 0.21, which a text sort got wrong."""
    _installed(monkeypatch, [r"C:\Program Files\FreeCAD 1.9\bin\freecadcmd.exe",
                             r"C:\Program Files\FreeCAD 1.10\bin\freecadcmd.exe",
                             r"C:\Program Files\FreeCAD 0.21\bin\freecadcmd.exe"])
    assert step_generator.find_freecadcmd().endswith(r"FreeCAD 1.10\bin\freecadcmd.exe")


def test_an_unversioned_installation_is_never_preferred(monkeypatch):
    """Negative: a folder without a version comes after every versioned one."""
    _installed(monkeypatch, [r"C:\Program Files\FreeCAD\bin\freecadcmd.exe",
                             r"C:\Program Files\FreeCAD 0.19\bin\freecadcmd.exe"])
    assert step_generator.find_freecadcmd().endswith(r"FreeCAD 0.19\bin\freecadcmd.exe")
    assert step_generator.freecad_folder_version("/usr/bin/freecadcmd") == ""


@pytest.mark.parametrize("folder,version", [("FreeCAD 1.1-dev", "1.1"), ("FreeCAD 1.1 RC", "1.1"),
                                            ("FreeCAD_1.0.2-conda-Windows-x86_64", "1.0.2"),
                                            ("FreeCAD 0.21", "0.21"), ("freecad-0.19", "0.19"),
                                            ("FreeCAD", ""), ("FreeCAD dev", ""), ("usr", "")])
def test_the_folder_version_ignores_a_suffix_after_the_number(folder, version):
    """Review G6: a pre-release suffix does not make an installation unversioned."""
    path = rf"C:\Program Files\{folder}\bin\freecadcmd.exe"
    assert step_generator.freecad_folder_version(path) == version


def test_a_suffixed_installation_ranks_by_its_number(monkeypatch):
    """Review G6, positive and negative: 1.1-dev outranks 0.19 and is outranked by 1.2."""
    _installed(monkeypatch, [r"C:\Program Files\FreeCAD 0.19\bin\freecadcmd.exe",
                             r"C:\Program Files\FreeCAD 1.1-dev\bin\freecadcmd.exe"])
    assert step_generator.find_freecadcmd().endswith(r"FreeCAD 1.1-dev\bin\freecadcmd.exe")
    _installed(monkeypatch, [r"C:\Program Files\FreeCAD 1.2\bin\freecadcmd.exe",
                             r"C:\Program Files\FreeCAD 1.1 RC\bin\freecadcmd.exe"])
    assert step_generator.find_freecadcmd().endswith(r"FreeCAD 1.2\bin\freecadcmd.exe")
