"""@brief Fix E: orchestration contracts around the FreeCAD children.

No FreeCAD, no network. Covers E4 (per-model measurement watchdog, partial
results cached), E5 (heartbeat of long silent steps), E6 (non-finite values
rejected), E7 (clamping and height capping announced), E8 (timeouts converted
to Cad3DError with the tail, UTF-8 decoding), E9 (explicit text frame), E11
(detector-only rows recognised by structure, overlap check after the
exclusion, three-leads-plus-tab by counts), C10 (PYTHONDONTWRITEBYTECODE in
every freecadcmd child) and C12 (user cache folders honour KICADGEN_DATA_DIR).
"""
import ast
import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import model_size_cache
from kicadgen.cad3d import process_log
from kicadgen.cad3d import step_generator as cad
from kicadgen.cad3d.annotations import EdgeFilletAnnotation, FaceTextAnnotation
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import (CastellatedModuleParams, CastellatedSide,
                                                    ComponentBox)

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"
NAN, INF = float("nan"), float("inf")


def _script_namespace(names, classes=(), constants=()):
    """@brief Execute selected top-level definitions of freecad_build_body."""
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name in names)
              or (isinstance(n, ast.ClassDef) and n.name in classes)
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") in constants for t in n.targets))]
    namespace = {"math": math, "sys": sys, "threading": threading, "time": time}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<build_body>", "exec"), namespace)
    return namespace


# ── E4: per-model silence watchdog, partial results survive ─────────────

_HANGING_MEASURER = """
import json, os, time
folder = os.environ["KICADGEN_MEASURE_DIR"]
with open(os.path.join(folder, "in.json"), encoding="utf-8") as handle:
    paths = json.load(handle)
sizes = {{}}
with open(os.path.join(folder, "progress.jsonl"), "w", encoding="utf-8") as done:
    for path in paths:
        if path.endswith("HANG.step"):
            time.sleep(60)
        time.sleep({interval})
        sizes[path] = {{"total": [1.6, 0.8, 0.4], "body": [1.6, 0.8, 0.4]}}
        done.write(json.dumps({{"path": path, "size": sizes[path]}}) + chr(10))
        done.flush()
with open(os.path.join(folder, "out.json"), "w", encoding="utf-8") as handle:
    json.dump(sizes, handle)
"""


def _models(tmp_path, names):
    folder = tmp_path / "lib" / "Resistor_SMD.3dshapes"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = folder / f"{name}.step"
        path.write_text("STEP finto", encoding="utf-8")
        paths.append(path)
    return paths


def _arm(monkeypatch, interval):
    monkeypatch.setattr(km, "_MEASURE_SCRIPT", _HANGING_MEASURER.format(interval=interval))
    monkeypatch.setattr(cad, "find_freecadcmd", lambda: sys.executable)


def test_a_stuck_model_does_not_throw_away_the_others(monkeypatch, tmp_path):
    """@brief Positive: the models before AND after the stuck one are measured.

    The old single 300 s budget returned {} on expiry: nothing cached, and
    every later run stalled again on the same file.
    """
    _arm(monkeypatch, interval=0.01)
    models = _models(tmp_path, ["R_1", "R_2_HANG", "R_3"])
    started = time.monotonic()
    result = km.measure_models(models, timeout=2)
    assert time.monotonic() - started < 30
    assert result[str(models[0])]["total"] == [1.6, 0.8, 0.4]
    assert result[str(models[1])] is None
    assert result[str(models[2])]["total"] == [1.6, 0.8, 0.4]


def test_a_slow_but_progressing_batch_is_not_killed(monkeypatch, tmp_path):
    """@brief Negative: the batch lasts longer than the timeout, each model does not.

    Structural difference with the positive case: here every model completes
    inside the per-model window, so nothing is declared stuck.
    """
    _arm(monkeypatch, interval=0.6)
    models = _models(tmp_path, ["R_a", "R_b", "R_c", "R_d"])
    result = km.measure_models(models, timeout=1.5)
    assert all(result[str(path)]["total"] == [1.6, 0.8, 0.4] for path in models)


def test_model_sizes_caches_what_was_measured_around_a_stuck_model(monkeypatch, tmp_path):
    """@brief The partial result reaches the size cache; the stuck one stays retryable."""
    _arm(monkeypatch, interval=0.01)
    real = km.measure_models
    monkeypatch.setattr(km, "measure_models", lambda paths, **kw: real(paths, timeout=2, **kw))
    models = _models(tmp_path, ["R_x", "R_y_HANG", "R_z"])
    library = tmp_path / "lib"
    km.model_sizes(models, library)
    index = km.load_index(library)
    assert index[km.index_key(models[0])]["total"] == [1.6, 0.8, 0.4]
    assert index[km.index_key(models[2])]["total"] == [1.6, 0.8, 0.4]
    assert km.index_key(models[1]) not in index


def test_the_measuring_child_does_not_write_bytecode(monkeypatch, tmp_path):
    """@brief C10: PYTHONDONTWRITEBYTECODE reaches the measuring child."""
    captured = {}
    script = ("import os, json\n"
              "folder = os.environ['KICADGEN_MEASURE_DIR']\n"
              "open(os.path.join(folder, 'out.json'), 'w').write(json.dumps("
              "{'flag': os.environ.get('PYTHONDONTWRITEBYTECODE')}))\n")
    monkeypatch.setattr(km, "_MEASURE_SCRIPT", script)
    monkeypatch.setattr(cad, "find_freecadcmd", lambda: sys.executable)
    captured.update(km.measure_models(_models(tmp_path, ["R_env"])))
    assert captured["flag"] == "1"


# ── E5: heartbeat of the long silent steps ───────────────────────────────

def test_a_long_silent_step_beats(capsys):
    """@brief Positive: a step longer than the interval prints heartbeat lines."""
    ns = _script_namespace(set(), classes={"_StepHeartbeat"},
                           constants={"_HEARTBEAT_INTERVAL_S"})
    with ns["_StepHeartbeat"]("step_export", interval=0.05):
        time.sleep(0.4)
    beats = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith("KICADGEN_HEARTBEAT:step_export")]
    assert len(beats) >= 2


def test_a_short_step_is_silent_and_nothing_beats_after_it(capsys):
    """@brief Negative: no beat for a quick step, none after the step ends."""
    ns = _script_namespace(set(), classes={"_StepHeartbeat"},
                           constants={"_HEARTBEAT_INTERVAL_S"})
    with ns["_StepHeartbeat"]("recompute", interval=0.2):
        pass
    time.sleep(0.5)
    assert "KICADGEN_HEARTBEAT" not in capsys.readouterr().out


def test_the_long_steps_are_wrapped():
    """@brief Every step named in the audit is inside a heartbeat."""
    source = (CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8")
    for step in ("fillets", "holes", "recompute", "step_export", "fcstd_save",
                 "tessellation", "mesh_json", "vrml_export"):
        assert f'_StepHeartbeat("{step}")' in source, step
    assert '_set_heartbeat_interval(data.get("heartbeat_interval_s"))' in source


# ── E6: non-finite values ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    dict(x=NAN), dict(y=INF), dict(length=INF), dict(rotation=NAN), dict(body_length=NAN)])
def test_clamping_rejects_non_finite_values(bad):
    """@brief Positive: NaN/inf are refused instead of becoming coordinates."""
    values = dict(x=1.0, y=1.0, length=2.0, width=1.0, rotation=0.0,
                  body_length=20.0, body_width=10.0)
    values.update(bad)
    with pytest.raises(ValueError):
        cad.clamp_component_position(**values)


def test_clamping_finite_values_is_unchanged():
    """@brief Negative: ordinary values still clamp exactly as before."""
    assert cad.clamp_component_position(15.0, 0.0, 2.0, 1.0, 0.0, 20.0, 10.0) == (
        pytest.approx(8.8), 0.0)


def test_overlap_check_ignores_non_finite_rows_and_still_finds_real_pairs():
    """@brief NaN made every comparison false: the pair silently vanished."""
    good = [{"name": "A", "x": 0.0, "y": 0.0, "length_mm": 2.0, "width_mm": 2.0},
            {"name": "B", "x": 0.5, "y": 0.0, "length_mm": 2.0, "width_mm": 2.0}]
    broken = {"name": "N", "x": NAN, "y": 0.0, "length_mm": 2.0, "width_mm": 2.0}
    warnings = cad.find_overlapping_components(good + [broken])
    assert len(warnings) == 1 and "'A'" in warnings[0] and "'B'" in warnings[0]


@pytest.mark.parametrize("field", ["length_mm", "height_mm", "offset_x_mm", "pin_length_mm"])
@pytest.mark.parametrize("value", [NAN, INF])
def test_body_params_reject_non_finite(field, value):
    params = Body3DParams(10.0, 5.0, 2.0)
    params.validate()
    setattr(params, field, value)
    with pytest.raises(ValueError):
        params.validate()


@pytest.mark.parametrize("annotation", [
    EdgeFilletAnnotation("top-front", NAN), EdgeFilletAnnotation("top-front", INF),
    FaceTextAnnotation("top", "U1", size_mm=NAN), FaceTextAnnotation("top", "U1", thickness_mm=INF)])
def test_annotations_reject_non_finite(annotation):
    with pytest.raises(ValueError):
        annotation.validate()


def test_finite_annotations_still_validate():
    EdgeFilletAnnotation("top-front", 0.5).validate()
    FaceTextAnnotation("top", "U1", size_mm=1.0).validate()


# ── E6/E7/E11 through the castellated orchestrator ──────────────────────

def _castellated(monkeypatch, tmp_path, components, attach=None, height=3.0):
    captured = {}

    def fake_run(name, pins, body, output_dir, **kw):
        captured["components"] = kw["extra_payload"]["components"]
        return {"step_path": output_dir / "x.step", "warnings": [], "notes": [],
                "mesh_path": None}
    monkeypatch.setattr(cad, "_run_freecad", fake_run)
    monkeypatch.setattr(km, "attach_models", attach or (lambda components, **kw: None))
    params = CastellatedModuleParams(name="FixE", body_length=20, body_width=10,
                                     sides=[CastellatedSide(edge="left", count=2, pitch=2.54)],
                                     components=components)
    body = Body3DParams(20, 10, height)
    result = cad.generate_castellated_3d_model(params, body, tmp_path)
    return result, captured["components"]


def test_a_nan_component_is_excluded_with_a_note(monkeypatch, tmp_path):
    """@brief E6: excluded and announced, never moved to an invented place."""
    result, built = _castellated(monkeypatch, tmp_path, [
        ComponentBox(NAN, 0, 2, 1, 1, name="Rotto"), ComponentBox(0, 0, 2, 1, 1, name="Buono"),
        ComponentBox(0, 2, 2, 1, 1, name="Ruotato", rotation=NAN)])
    assert [c["name"] for c in built] == ["Buono"]
    notes = "\n".join(result["notes"])
    assert "Rotto" in notes and "Ruotato" in notes and "Buono" not in notes


def test_clamping_and_height_capping_are_announced(monkeypatch, tmp_path):
    """@brief E7 positive: every changed estimate leaves a note.

    CONTRACT CHANGED 2026-09-26 (punto 84 medio 4): un corpo viene riportato
    dentro solo se sporge meno dell'incertezza di misura. Il vecchio caso
    (x=9.8, 0.8 mm oltre il bordo) ora resta dov'e' con una nota: lo copre
    tests/test_overhang_uncertainty.py. Qui x=9.1 sporge di 0.1 mm, meno
    dell'incertezza di ripiego (0.17 mm), e viene ancora spostato.
    """
    result, built = _castellated(monkeypatch, tmp_path, [
        ComponentBox(9.1, 0, 2, 1, 5.0, name="Sporgente")], height=3.0)
    (component,) = built
    assert component["x"] < 9.1
    thickness = CastellatedModuleParams(name="x").pcb_thickness
    assert component["height_mm"] == pytest.approx(3.0 - thickness)
    notes = [n for n in result["notes"] if "Sporgente" in n]
    assert len(notes) == 2


def test_an_unchanged_component_leaves_no_placement_note(monkeypatch, tmp_path):
    """@brief E7 negative: nothing changed, nothing to announce."""
    result, built = _castellated(monkeypatch, tmp_path, [
        ComponentBox(0, 0, 2, 1, 0.8, name="Dentro")], height=3.0)
    assert built[0]["x"] == 0 and built[0]["height_mm"] == 0.8
    assert not [n for n in result["notes"] if "Dentro" in n]


def test_hidden_rows_do_not_produce_overlap_warnings(monkeypatch, tmp_path):
    """@brief E11: the overlap check runs after the detector-only exclusion."""
    def hide_second(components, **kw):
        components[1]["render_3d"] = False
    rows = [ComponentBox(0, 0, 3, 2, 1, name="Reale"),
            ComponentBox(0.5, 0, 3, 2, 1, name="Ipotesi")]
    result, _ = _castellated(monkeypatch, tmp_path, rows, attach=hide_second)
    assert not [w for w in result["warnings"] if "sovrapposti" in w or "overlap" in w.lower()]
    result, _ = _castellated(monkeypatch, tmp_path, rows)
    assert len([w for w in result["warnings"] if "Ipotesi" in w]) == 1


# ── E11: detector-only rows by structure ─────────────────────────────────

@pytest.mark.parametrize("row", [
    {"name": "Resistor 3", "_yolo_proposal_id": "tile-1:7"},
    {"name": "qualunque", "_yolo_proposal_id": "overview:3",
     "_yolo_origin": "operating_threshold"},
    {"name": "YOLO candidate 7", "_yolo_origin": "low_confidence_review"},
])
def test_an_unjudged_detector_row_is_excluded_whatever_its_name(row):
    assert km._is_yolo_only_hypothesis(row) is True


@pytest.mark.parametrize("row", [
    # Detector-only inventory chosen by the user (pretest), reviewed or not.
    {"name": "resistor 1", "_yolo_proposal_id": "tile-1:7", "_presence_reviewed_by_user": True},
    {"name": "resistor 1", "_yolo_origin": "operating_threshold",
     "_presence_reviewed_by_user": False},
    # Judged by the reconciliation.
    {"name": "U1", "_yolo_proposal_id": "overview:2", "presence_confidence": "high"},
    {"name": "YOLO candidate 4", "_yolo_origin": "operating_threshold",
     "_global_inventory_status": "validated_hypotheses"},
    # Explicitly confirmed model.
    {"name": "YOLO candidate 9", "_yolo_origin": "operating_threshold",
     "model_selection": {"status": "confirmed"}},
    # Ordinary row.
    {"name": "C3"},
    # Origin without proposal id: what the GUI forwarded before fix E for the
    # detector-only inventory; not enough to hide a renamed row.
    {"name": "resistor 2", "_yolo_origin": "operating_threshold"},
])
def test_judged_or_ordinary_rows_are_kept(row):
    assert km._is_yolo_only_hypothesis(row) is False


def test_a_legacy_row_with_only_the_detector_label_keeps_its_old_verdict():
    """@brief Declared compatibility: no structural field at all."""
    assert km._is_yolo_only_hypothesis({"name": "YOLO candidate 12"}) is True


def test_the_structural_fields_reach_the_3d_payload():
    """@brief The evidence copied into the payload carries the new fields."""
    box = ComponentBox(0, 0, 1, 1, 1, match_evidence={
        "_yolo_proposal_id": "t:1", "_presence_reviewed_by_user": True,
        "lead_count": 3, "lead_count_complete": True, "unrelated": 1})
    evidence = cad._match_evidence(box)
    assert evidence == {"_yolo_proposal_id": "t:1", "_presence_reviewed_by_user": True,
                        "lead_count": 3, "lead_count_complete": True}


@pytest.mark.parametrize("row,expected", [
    ({"lead_count": 3, "terminal_count": 4, "lead_count_complete": True}, True),
    # Counts contradict the prose: counts win.
    ({"lead_count": 5, "terminal_count": 6, "lead_count_complete": True,
      "terminal_layout": "three leads and one tab"}, False),
    ({"lead_count": 3, "terminal_count": 4, "lead_count_complete": False}, False),
    # Declared heuristic fallback when the counts are absent.
    ({"terminal_layout": "three gull-wing leads bottom, one large tab top"}, True),
    ({"terminal_layout": "three leads, maybe one tab"}, False),
])
def test_three_leads_plus_tab(row, expected):
    assert km._three_leads_one_tab(row) is expected


# ── E8 + C10: timeouts and decoding of the FreeCAD child ────────────────

def _run(tmp_path, **kw):
    return cad._run_freecad("T", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
                            freecadcmd_path="freecadcmd", **kw)


def test_a_silent_child_becomes_a_cad_error_with_its_tail(monkeypatch, tmp_path):
    """@brief Non-interactive path: TimeoutExpired -> Cad3DError with the output tail."""
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(command, 300, output=b"riga 1\nultima riga\n")
    monkeypatch.setattr(cad.subprocess, "run", fake_run)
    with pytest.raises(cad.Cad3DError, match="ultima riga"):
        _run(tmp_path)
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_the_logged_path_converts_the_watchdog_timeout_too(monkeypatch, tmp_path):
    """@brief Interactive path: the watchdog's TimeoutExpired is not 'unexpected'."""
    def fake_logged(command, log_path, progress=None, timeout=None, creationflags=0, **kw):
        raise subprocess.TimeoutExpired(command, timeout, output="import...\nfermo qui\n")
    monkeypatch.setattr(process_log, "run_logged", fake_logged)
    with pytest.raises(cad.Cad3DError, match="fermo qui"):
        _run(tmp_path, progress=lambda _: None)


def test_a_cancellation_is_still_the_abort_message(monkeypatch, tmp_path):
    """@brief Negative: the conversion does not swallow the user's stop."""
    def fake_logged(command, log_path, progress=None, timeout=None, creationflags=0, **kw):
        raise process_log.ProcessCancelled(command)
    monkeypatch.setattr(process_log, "run_logged", fake_logged)
    with pytest.raises(cad.Cad3DError) as stopped:
        _run(tmp_path, progress=lambda _: None)
    assert str(stopped.value) == cad.CAD_ABORT_MESSAGE


def test_run_logged_children_do_not_write_bytecode(tmp_path):
    """@brief C10: the default child environment carries the flag."""
    code = "import os; print('FLAG=' + os.environ.get('PYTHONDONTWRITEBYTECODE', ''))"
    result = process_log.run_logged([sys.executable, "-c", code], tmp_path / "log.txt",
                                    timeout=30)
    assert "FLAG=1" in result.stdout


def test_the_params_file_carries_the_cache_folders(monkeypatch, tmp_path):
    """@brief C12: the FreeCAD child receives the resolved cache folders."""
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path / "dati"))
    captured = {}

    def fake_logged(command, log_path, progress=None, timeout=None, creationflags=0, **kw):
        captured.update(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
        raise RuntimeError("stop")
    monkeypatch.setattr(process_log, "run_logged", fake_logged)
    with pytest.raises(RuntimeError):
        _run(tmp_path, progress=lambda _: None)
    assert Path(captured["cache_dirs"][0]) == tmp_path / "dati" / "cache"


# ── C12: user cache folders ──────────────────────────────────────────────

def test_the_data_dir_override_moves_the_caches(monkeypatch, tmp_path):
    """@brief Positive: KICADGEN_DATA_DIR first, legacy kept readable second."""
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path / "dati"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    folders = model_size_cache.user_cache_dirs()
    assert folders == [tmp_path / "dati" / "cache", tmp_path / "local" / "kicadgen"]
    library = tmp_path / "readonly-lib"
    library.mkdir()
    paths = km._index_paths(library)
    assert tmp_path / "dati" / "cache" / km.INDEX_FILENAME in paths
    assert tmp_path / "local" / "kicadgen" / km.INDEX_FILENAME in paths


def test_from_the_sources_the_legacy_folder_is_unchanged(monkeypatch, tmp_path):
    """@brief Negative: no override, not frozen -> exactly the old location."""
    monkeypatch.delenv("KICADGEN_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert model_size_cache.user_cache_dirs() == [tmp_path / "local" / "kicadgen"]


def test_an_old_index_is_still_read_and_migrates(monkeypatch, tmp_path):
    """@brief Existing caches stay readable and move on the next save."""
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path / "dati"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    legacy = tmp_path / "local" / "kicadgen" / "index.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"k": {"total": [1, 2, 3]}}), encoding="utf-8")
    paths = [folder / "index.json" for folder in model_size_cache.user_cache_dirs()]
    assert model_size_cache.load(paths) == {"k": {"total": [1, 2, 3]}}
    model_size_cache.save({"n": {"total": [4, 5, 6]}}, paths)
    moved = json.loads((tmp_path / "dati" / "cache" / "index.json").read_text(encoding="utf-8"))
    assert set(moved) == {"k", "n"}


# ── E9: explicit text frame ──────────────────────────────────────────────

def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


@pytest.mark.parametrize("normal,reading", [
    ((0, -1, 0), (1, 0, 0)),   # front: seen from -Y, reads towards +X
    ((0, 1, 0), (-1, 0, 0)),   # back: seen from +Y, reads towards -X
    ((1, 0, 0), (0, 1, 0)),    # right: seen from +X, reads towards +Y
    ((-1, 0, 0), (0, -1, 0)),  # left: seen from -X, reads towards -Y
])
def test_side_text_is_upright_and_reads_left_to_right(normal, reading):
    """@brief Positive: up is +Z on every side face (the back was upside down)."""
    frame = _script_namespace({"_text_frame"})["_text_frame"]
    x, y, z = frame(normal)
    assert tuple(round(c, 9) for c in y) == (0, 0, 1)
    assert tuple(round(c, 9) for c in x) == reading
    assert tuple(round(c, 9) for c in _cross(x, y)) == tuple(float(c) for c in normal)


def test_top_text_keeps_the_plan_orientation():
    """@brief Negative: the top face is unchanged (reads along +X, up +Y)."""
    frame = _script_namespace({"_text_frame"})["_text_frame"]
    x, y, z = frame((0, 0, 1))
    assert tuple(round(c, 9) for c in x) == (1, 0, 0)
    assert tuple(round(c, 9) for c in y) == (0, 1, 0)
