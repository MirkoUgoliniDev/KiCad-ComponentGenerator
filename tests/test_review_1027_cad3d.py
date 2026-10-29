"""@brief Fixes of the 2026-10-27 adversarial review of kicadgen/cad3d (H1, H2, H4-H12).

Each finding has a positive and a negative case that the rule must tell
apart for a structural reason, never by a coincidence of numbers:

- H1  a process older than its claimed parent is a pid-reuse orphan, not a
      descendant: it is never terminated; an unverifiable one is skipped.
- H2  a model whose measurement stalled is remembered with its timeout and
      skipped; a rewritten file or a larger timeout measures it again.
- H4  the component name is a plain file name, through one shared rule.
- H5  a script caller with a cancel channel but no progress listener is
      still interruptible (the logged path is used).
- H6  the envelope index is written to the user cache, never beside the
      library, while an old index beside it is still read.
- H7  an assumed header pitch is declared in the notes.
- H8  the FreeCAD source is staged and published like the shipped exports.
- H9  reaping a killed measuring child is bounded.
- H10 the library is listed once per attach_models call.
- H11 the measuring script carries English comments only.
- H12 dead code removed, index_key delegates to file_identity.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import model_size_cache, process_log, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.output_names import is_valid_output_name, validate_output_name
from kicadgen.cad3d.process_log import select_descendants
from kicadgen.cad3d.step_generator import Cad3DError, staging_path
from kicadgen.generators.castellated_module import (
    CONTACT_THROUGH_HOLE, CastellatedModuleParams, CastellatedSide,
)

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"


# ── H1: descendants verified by creation time ─────────────────────────────

def _tree(children, born):
    return select_descendants(1, children, lambda pid: born.get(pid))


def test_h1_a_genuine_child_and_grandchild_are_selected_deepest_first():
    """@brief Positive: created after their parents, both are ours; the grandchild dies first."""
    assert _tree({1: [2], 2: [4]}, {1: 100, 2: 110, 4: 120}) == [4, 2]


def test_h1_a_candidate_older_than_its_claimed_parent_is_excluded():
    """@brief Negative: pid 3 existed before pid 1 was born, so pid 1 cannot have started it."""
    found = _tree({1: [2, 3], 3: [5]}, {1: 100, 2: 110, 3: 50, 5: 200})
    assert 3 not in found and 5 not in found, "an orphan's own children are not explored either"
    assert found == [2]


def test_h1_a_process_that_cannot_be_opened_is_skipped_never_killed():
    """@brief Negative: no creation stamp means no proof of parentage; the subtree stays alive."""
    assert _tree({1: [2], 2: [4]}, {1: 100, 4: 120}) == []
    assert _tree({1: [2]}, {2: 120}) == [], "an unverifiable root selects nothing"


def test_h1_kill_process_tree_never_terminates_a_pid_reuse_orphan(monkeypatch):
    """@brief End to end: the Windows path terminates only verified descendants."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        born = {process.pid: 1000, 777: 500, 888: 1100}
        terminated = []
        monkeypatch.setattr(process_log, "_windows_children_map",
                            lambda: {process.pid: [777, 888]})
        monkeypatch.setattr(process_log, "_windows_creation_time", lambda pid: born.get(pid))
        monkeypatch.setattr(process_log, "_windows_terminate", terminated.append)
        process_log.kill_process_tree(process, platform="win32")
        process.wait(timeout=20)
        assert terminated == [888], "the older pid 777 is a foreign process and is left alone"
    finally:
        if process.poll() is None:
            process.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API only")
def test_h1_the_real_creation_time_is_readable_for_our_own_child():
    """@brief Positive on the real API: a child we own has a stamp not earlier than ours."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ours = process_log._windows_creation_time(os.getpid())
        child = process_log._windows_creation_time(process.pid)
        assert ours is not None and child is not None and child >= ours
    finally:
        process.kill()
        process.wait(timeout=20)


# ── H2: stalled measurements are remembered ───────────────────────────────

@pytest.fixture
def user_cache(monkeypatch, tmp_path):
    """@brief Point every user cache of this module at a private folder."""
    folder = tmp_path / "cache"
    monkeypatch.setattr(model_size_cache, "user_cache_dirs", lambda: [folder])
    return folder


def _model(tmp_path, name="M.step", text="ISO-10303-21;"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_h2_a_stall_is_remembered_with_its_timeout(user_cache, tmp_path):
    """@brief Positive: the same file with a budget no larger is skipped."""
    model = _model(tmp_path)
    km.record_stalls({str(model): 300})
    stalls = km.load_stalls()
    assert km.stall_remembered(stalls, model, 300)
    assert km.stall_remembered(stalls, model, 120), "a smaller budget would stall sooner"
    assert (user_cache / km.STALL_FILENAME).is_file()
    assert not (user_cache / km.INDEX_FILENAME).exists(), "a stall is not a measurement"


def test_h2_a_larger_timeout_or_a_rewritten_file_measures_again(user_cache, tmp_path):
    """@brief Negative: more patience, or a different file under the same name, is a new question."""
    model = _model(tmp_path)
    km.record_stalls({str(model): 300})
    stalls = km.load_stalls()
    assert not km.stall_remembered(stalls, model, 301)
    time.sleep(0.02)
    model.write_text("ISO-10303-21; rewritten", encoding="utf-8")
    os.utime(model, None)
    assert not km.stall_remembered(stalls, model, 300)
    assert not km.stall_remembered(stalls, tmp_path / "missing.step", 300)


def test_h2_model_sizes_skips_a_remembered_stall_until_the_file_changes(user_cache, tmp_path, monkeypatch):
    """@brief Integration: one paid stall, then no measuring call for that file; a rewrite re-measures."""
    model = _model(tmp_path)
    library = tmp_path / "lib"
    library.mkdir()
    calls = []

    def stalling(paths, **kwargs):
        calls.append([str(p) for p in paths])
        return km._Measurements({str(p): None for p in paths},
                                stalled={str(p): km.MEASURE_SILENCE_TIMEOUT_S for p in paths})

    monkeypatch.setattr(km, "measure_models", stalling)
    assert km.model_sizes([model], library) == {str(model): None}
    assert km.model_sizes([model], library) == {str(model): None}
    assert calls == [[str(model)]], "the second generation did not pay the silence again"
    time.sleep(0.02)
    model.write_text("ISO-10303-21; v2", encoding="utf-8")
    km.model_sizes([model], library)
    assert len(calls) == 2, "a rewritten file is measured again"


def test_h2_a_plain_failure_is_not_remembered_as_a_stall(user_cache, tmp_path, monkeypatch):
    """@brief Negative: an unreadable file (None without a stall) is never a stall.

    CONTRACT CHANGED 2026-10-29: review H1 remembers an unreadable answer as
    its own verdict ("unreadable"), bound to the FreeCAD build that gave it,
    so the same FreeCAD does not re-read it at every generation. What this
    test still guards: it is not recorded as a STALL, and without a FreeCAD
    identity (no installation found) it stays retryable every time.
    """
    model = _model(tmp_path)
    calls = []
    monkeypatch.setattr(km, "measure_models",
                        lambda paths, **kw: calls.append(1) or {str(p): None for p in paths})

    def no_freecad():
        raise step_generator.Cad3DError("freecadcmd not found")
    monkeypatch.setattr(step_generator, "find_freecadcmd", no_freecad)
    km.model_sizes([model], tmp_path / "lib")
    km.model_sizes([model], tmp_path / "lib")
    assert len(calls) == 2
    assert not (user_cache / km.STALL_FILENAME).exists()

    reader = tmp_path / "freecadcmd.exe"
    reader.write_text("reader", encoding="utf-8")
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: str(reader))
    km.model_sizes([model], tmp_path / "lib")
    km.model_sizes([model], tmp_path / "lib")
    assert len(calls) == 3, "the same FreeCAD does not re-read an unreadable file"
    stalls = km.load_stalls()
    assert not km.stall_remembered(stalls, model, km.MEASURE_SILENCE_TIMEOUT_S)
    assert stalls[km.index_key(model)]["status"] == "unreadable"


# ── H4: the output name is a plain file name ──────────────────────────────

@pytest.mark.parametrize("name", ["..\\evil", "../evil", "a/b", "a\\b", "name.", "name ",
                                  "", ".", "..", "a:b", "a\x00b", "a|b"])
def test_h4_a_name_that_is_not_a_plain_file_name_is_refused(name):
    """@brief Negative: separators, dot names, trailing dots/spaces and control characters."""
    assert not is_valid_output_name(name)
    with pytest.raises(ValueError):
        validate_output_name(name)


@pytest.mark.parametrize("name", ["ESP32-S3", "ESP32.module", "RS485 TTL", "Cmp_1"])
def test_h4_a_plain_name_is_accepted(name):
    """@brief Positive: dots inside the name and spaces are ordinary file-name characters."""
    assert is_valid_output_name(name)
    validate_output_name(name)


def test_h4_run_freecad_refuses_a_name_escaping_the_output_folder(tmp_path):
    """@brief The generator applies the same rule before touching the disk."""
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ValueError):
        step_generator._run_freecad("..\\evil", [], Body3DParams(10.0, 4.0, 2.0), out,
                                    freecadcmd_path="freecadcmd")
    assert list(tmp_path.iterdir()) == [out] and list(out.iterdir()) == []


def test_h4_the_gui_rule_is_the_shared_rule():
    """@brief The interface's own check must be the same criterion (it should import the helper)."""
    source = (CAD_DIR.parent / "gui" / "main_window.py").read_text(encoding="utf-8")
    body = source[source.index("def _validate_output_name"):][:600]
    if "validate_output_name(" in body.replace("_validate_output_name", ""):
        return  # already delegating to the shared helper
    gui_rule = re.search(r"re\.search\(r'(\[[^']+\])'", body).group(1)
    assert gui_rule == r'[<>:"/\\|?*\x00-\x1f]'
    assert "rstrip(' .')" in body and "('.', '..')" in body


# ── H5 and H8: the FreeCAD child under a script caller ────────────────────

_CHILD = """
import json, os, sys, time
data = json.load(open(sys.argv[-1], encoding="utf-8"))
staging = data["staging_paths"]
def write(key, final, text):
    with open(staging[key], "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(staging[key], final)
write("step", data["step_path"], "ISO-10303-21;\\nHEADER;\\nENDSEC;\\nDATA;\\nENDSEC;\\nEND-ISO-10303-21;\\n")
print("KICADGEN_STEP_OK:" + data["step_path"], flush=True)
if {fcstd!r} == "staged_only":
    open(staging["fcstd"], "w").write("partial")
elif {fcstd!r} == "published":
    write("fcstd", data["fcstd_path"], "FCStd")
    print("KICADGEN_FCSTD_OK:" + data["fcstd_path"], flush=True)
if {sleep}:
    print("working", flush=True)
    time.sleep({sleep})
"""


def _arm(monkeypatch, tmp_path, *, fcstd="", sleep=0):
    script = tmp_path / "fake_freecad.py"
    script.write_text(_CHILD.format(fcstd=fcstd, sleep=sleep), encoding="utf-8")
    monkeypatch.setattr(step_generator, "_SCRIPT_PATH", script)


def _run(out, **kwargs):
    return step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), out,
                                       freecadcmd_path=sys.executable, **kwargs)


def _shapes(out):
    return out / "Demo.3dshapes"


def test_h5_a_cancel_without_a_progress_listener_kills_the_child(monkeypatch, tmp_path):
    """@brief Positive: no listener, a cancel channel: the request ends the run through the logged path."""
    _arm(monkeypatch, tmp_path, sleep=60)
    cancel = threading.Event()
    threading.Timer(0.8, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(Cad3DError) as error:
        _run(tmp_path / "out", cancel=cancel)
    assert step_generator.CAD_ABORT_MESSAGE in str(error.value)
    assert time.monotonic() - started < 30
    assert not _shapes(tmp_path / "out").joinpath("Demo.step").exists(), "a cancelled run ships nothing"
    assert (tmp_path / "out" / "freecad.log").is_file(), "the logged path was used"


def test_h5_an_unset_channel_lets_the_same_call_complete(monkeypatch, tmp_path):
    """@brief Negative: the channel alone changes nothing; the child runs to the end."""
    _arm(monkeypatch, tmp_path)
    result = _run(tmp_path / "out", cancel=threading.Event())
    assert result["step_path"].is_file()


def test_h8_a_published_fcstd_is_returned_and_no_staging_file_remains(monkeypatch, tmp_path):
    """@brief Positive: the source reaches its final name only by the rename."""
    _arm(monkeypatch, tmp_path, fcstd="published")
    result = _run(tmp_path / "out")
    fcstd = _shapes(tmp_path / "out") / "Demo.FCStd"
    assert result["fcstd_path"] == fcstd and fcstd.read_text() == "FCStd"
    assert not staging_path(fcstd).exists()


def test_h8_a_failed_save_leaves_no_stale_or_partial_fcstd(monkeypatch, tmp_path):
    """@brief Negative: the previous run's source and a half-written one are both gone."""
    out = tmp_path / "out"
    fcstd = _shapes(out) / "Demo.FCStd"
    fcstd.parent.mkdir(parents=True)
    fcstd.write_text("OLD")
    _arm(monkeypatch, tmp_path, fcstd="staged_only")
    result = _run(out)
    assert result["fcstd_path"] is None
    assert not fcstd.exists(), "the stale source of the previous run was deleted before the run"
    assert not staging_path(fcstd).exists(), "a source without its marker is not a result"
    assert result["step_path"].is_file()


def _script_namespace(*names, **stubs):
    """@brief Load top-level functions of freecad_build_body.py without FreeCAD."""
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    import shutil
    import tempfile
    namespace = {"os": os, "time": time, "tempfile": tempfile, "shutil": shutil, **stubs}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<script>", "exec"), namespace)
    return namespace


class _Heartbeat:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_h8_the_child_saves_under_the_staging_name_then_renames(tmp_path, capsys):
    """@brief Inside the script: saveAs sees the staging path, the final appears by rename."""
    warnings = []
    ns = _script_namespace("_export_fcstd", "_save_fcstd", "_publish", "_discard", "_long_path",
                           _StepHeartbeat=_Heartbeat, _warn=lambda t, **v: warnings.append(t))
    seen = []

    def save_as(path):
        seen.append(path)
        Path(path.replace("\\\\?\\", "")).write_text("doc")

    final, staged = tmp_path / "X.FCStd", tmp_path / "X.kicadgen-partial.FCStd"
    ns["_export_fcstd"](SimpleNamespace(saveAs=save_as), str(final), str(staged))
    assert seen == [str(staged)] and final.read_text() == "doc" and not staged.exists()
    assert "KICADGEN_FCSTD_OK" in capsys.readouterr().out and warnings == []


def test_h8_the_child_discards_the_staging_file_when_the_save_fails(tmp_path, capsys):
    """@brief Negative: a failing save leaves no file and warns; the job goes on."""
    warnings = []
    ns = _script_namespace("_export_fcstd", "_save_fcstd", "_publish", "_discard", "_long_path",
                           _StepHeartbeat=_Heartbeat, _warn=lambda t, **v: warnings.append(t))

    def save_as(path):
        Path(path.replace("\\\\?\\", "")).write_text("half")
        raise RuntimeError("disk full")

    final, staged = tmp_path / "X.FCStd", tmp_path / "X.kicadgen-partial.FCStd"
    ns["_export_fcstd"](SimpleNamespace(saveAs=save_as), str(final), str(staged))
    assert not final.exists() and not staged.exists()
    assert "KICADGEN_FCSTD_FAILED" in capsys.readouterr().out and len(warnings) == 1


# ── H6: the index never lands in the library folder ───────────────────────

def test_h6_a_writable_library_folder_stays_untouched(user_cache, tmp_path):
    """@brief Positive: the index lands in the user cache even though the library is writable."""
    library = tmp_path / "lib"
    library.mkdir()
    assert os.access(library, os.W_OK)
    km.save_index({"k": {"total": [1, 2, 3]}}, library)
    assert sorted(p.name for p in library.iterdir()) == []
    assert (user_cache / km.INDEX_FILENAME).is_file()
    assert km._index_path(library) == user_cache / km.INDEX_FILENAME


def test_h6_an_old_index_beside_the_library_is_still_read(user_cache, tmp_path):
    """@brief Negative that keeps migration alive: beside-library is a read source, not a sink."""
    library = tmp_path / "lib"
    library.mkdir()
    (library / km.INDEX_FILENAME).write_text(json.dumps({"old": {"total": [4, 5, 6]}}), encoding="utf-8")
    assert km.load_index(library)["old"]["total"] == [4, 5, 6]
    km.save_index({"new": {"total": [1, 2, 3]}}, library)
    merged = json.loads((user_cache / km.INDEX_FILENAME).read_text(encoding="utf-8"))
    assert {"old", "new"} <= set(merged), "the old entries migrate into the user cache"
    assert json.loads((library / km.INDEX_FILENAME).read_text(encoding="utf-8")) == {"old": {"total": [4, 5, 6]}}


# ── H7: an assumed header pitch is a declared estimate ────────────────────

def _one_contact(pitch):
    params = CastellatedModuleParams(
        name="One", body_length=10.0, body_width=10.0,
        sides=[CastellatedSide(edge="left", count=1, pitch=pitch,
                               contact_style=CONTACT_THROUGH_HOLE)])
    params.validate()
    return params


def _generate(params, monkeypatch, tmp_path):
    monkeypatch.setattr(step_generator, "_run_freecad",
                        lambda *a, **k: {"step_path": tmp_path / "x.step", "warnings": [], "notes": []})
    body = Body3DParams(length_mm=params.body_length, width_mm=params.body_width, height_mm=3.0)
    return step_generator.generate_castellated_3d_model(params, body, tmp_path)


def test_h7_a_single_contact_side_without_pitch_is_declared_in_the_notes(monkeypatch, tmp_path):
    """@brief Positive: the reference pitch is an estimate, so a note names the side and the value."""
    notes = [n for n in _generate(_one_contact(0.0), monkeypatch, tmp_path)["notes"] if "2.54" in n]
    assert len(notes) == 1 and "left" in notes[0]
    assert step_generator.header_side_pitch(_one_contact(0.0).sides[0]) == (2.54, True)


def test_h7_a_declared_pitch_adds_no_note(monkeypatch, tmp_path):
    """@brief Negative: a declared pitch is a datum, not an estimate."""
    result = _generate(_one_contact(2.0), monkeypatch, tmp_path)
    assert not [n for n in result["notes"] if "passo di riferimento" in n]
    assert step_generator.header_side_pitch(_one_contact(2.0).sides[0]) == (2.0, False)


# ── H9: bounded reaping of a killed child ─────────────────────────────────

class _Stuck:
    """@brief A process whose wait would block forever without a timeout."""
    pid = 4242

    def wait(self, timeout=None):
        assert timeout is not None, "an unbounded wait would hang the matching"
        raise subprocess.TimeoutExpired("freecadcmd", timeout)


def test_h9_a_child_that_refuses_to_die_does_not_hang_and_is_logged(caplog):
    """@brief Negative: the reaping gives up after the grace and says so."""
    with caplog.at_level(logging.WARNING, logger="kicadgen.cad3d.kicad_models"):
        assert km._reap_killed_child(_Stuck(), grace_s=0.01) is False
    assert "4242" in caplog.text


def test_h9_a_child_that_exits_is_reaped():
    """@brief Positive: an ordinary exit returns at once."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert km._reap_killed_child(process, grace_s=20) is True


# ── H10: one library listing per attach_models call ───────────────────────

def _library(tmp_path):
    root = tmp_path / "3dmodels"
    for name in ("Package_TO_SOT_SMD.3dshapes/SOT-223.step", "Package_TO_SOT_SMD.3dshapes/SOT-223-8.step",
                 "Resistor_SMD.3dshapes/R_0402_1005Metric.step", "Resistor_SMD.3dshapes/R_0402_1005Metric.wrl"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    return root


def test_h10_attach_models_lists_the_library_once(monkeypatch, tmp_path):
    """@brief Positive: several components, one walk of the root."""
    root = _library(tmp_path)
    walks = []
    real = km._walk_model_files
    monkeypatch.setattr(km, "_walk_model_files", lambda r: walks.append(r) or real(r))
    monkeypatch.setattr(km, "find_library", lambda: root)
    monkeypatch.setattr(km, "measure_models", lambda paths, **kw: {})
    components = [{"name": f"U{i}", "kind": "ic", "package": "SOT-223", "length_mm": 6.5,
                   "width_mm": 3.5, "confidence": "high"} for i in range(3)]
    km.attach_models(components, library=root)
    assert len(walks) == 1


def test_h10_outside_a_call_every_lookup_walks_and_the_listing_is_identical(monkeypatch, tmp_path):
    """@brief Negative and equivalence: no scope, no cache; the files are those rglob found."""
    root = _library(tmp_path)
    walks = []
    real = km._walk_model_files
    monkeypatch.setattr(km, "_walk_model_files", lambda r: walks.append(r) or real(r))
    first, second = km.candidates("SOT-223", root), km.candidates("SOT-223", root)
    assert len(walks) == 2 and first == second
    assert [p.name for p in first] == ["SOT-223.step", "SOT-223-8.step"]
    expected = sorted(p for p in root.rglob("*") if p.suffix.lower() in km.MODEL_SUFFIXES)
    assert km._model_files(root) == expected
    assert km._model_files(tmp_path / "missing") == []


# ── H11 and H12: comments and dead code ───────────────────────────────────

def test_h11_the_measuring_script_has_english_comments_only():
    """@brief The FreeCAD-side script is checked like every other comment."""
    comments = [line.strip() for line in km._MEASURE_SCRIPT.splitlines() if line.strip().startswith("#")]
    assert comments, "the script still explains itself"
    italian = re.compile(r"(?<!\w)(?:della|viene|perche'|oppure|ogni|cio'|solo corpo|misura)(?!\w)")
    assert not [c for c in comments if italian.search(c)]


def test_h12_dead_code_is_gone_and_index_key_delegates():
    """@brief _safe_name no longer exists; index_key is file_identity with the historic fallback."""
    assert not hasattr(step_generator, "_safe_name")
    source = (CAD_DIR / "kicad_models.py").read_text(encoding="utf-8")
    assert "st_mtime_ns" not in source.split("def index_key")[1].split("\ndef ")[0], "no second recipe"


def test_h12_index_key_equals_file_identity_and_keeps_the_path_fallback(tmp_path):
    """@brief Positive: same string for a real file; negative: a missing file keeps a string key."""
    model = _model(tmp_path)
    assert km.index_key(model) == model_size_cache.file_identity(model)
    missing = tmp_path / "missing.step"
    assert km.index_key(missing) == str(missing.absolute())
