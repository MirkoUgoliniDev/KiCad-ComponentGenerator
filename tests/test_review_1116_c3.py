"""@brief Regression tests of the 2026-11-16 review, CAD scope (kicadgen/cad3d, findings R10-1..R10-8).

R10-1 a measuring child that never reaches its measuring loop blames no
model and stops the batch; stalls are bound to the FreeCAD build and can be
forgotten. R10-2 the watchdogs use a clock that does not count a suspended
PC, a file the child cannot open is not a verdict, the candidates skipped by
a remembered verdict are reported. R10-3 a WRL or preview lost after a good
STEP is a warning, and the child's tessellation is best-effort. R10-4 a
library model that kills the builder's import is remembered and the run is
repeated once without it. R10-5 the KiCad 3D library is found through
KiCad's variables, the declared program roots, the registry and the
redirected Documents folder. R10-6 the removal of an old output waits for a
transient refusal. R10-7 a failed model attachment leaves no half-attached
payloads. R10-8 a transient read error is not cached.

Each rule has a positive and a negative case that it must tell apart for a
structural reason. No test launches FreeCAD: the measuring child is this
Python running the REAL measuring script against fake `FreeCAD`/`Part`
modules, and the builder's child is replaced at `process_log.run_logged`.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import model_size_cache, process_log, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.step_generator import Cad3DError


ROOT = Path(__file__).resolve().parents[1]
CAD_DIR = ROOT / "kicadgen" / "cad3d"

#: A STEP with its ISO 10303-21 trailer, accepted as a complete export.
COMPLETE_STEP = ("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
                 "#1 = MANIFOLD_SOLID_BREP('Body',#2);\nENDSEC;\nEND-ISO-10303-21;\n")


@pytest.fixture(autouse=True)
def _fresh_measuring_session():
    """@brief Every test starts and ends without a «measuring did not start» flag of this session."""
    km.reset_unstarted_measuring()
    yield
    km.reset_unstarted_measuring()


@pytest.fixture
def user_cache(monkeypatch, tmp_path):
    """@brief Point every user cache of the CAD modules at a private folder."""
    folder = tmp_path / "cache"
    monkeypatch.setattr(model_size_cache, "user_cache_dirs", lambda: [folder])
    return folder


# ── the fake measuring child ──────────────────────────────────────────────

_FAKE_FREECAD = "class Vector:\n    def __init__(self, *values):\n        self.values = values\n"

_FAKE_PART = '''
import os
import time
with open(os.environ["FAKE_LAUNCHES"], "a", encoding="utf-8") as handle:
    handle.write("launch\\n")
if os.environ.get("FAKE_BLOCK_START"):
    # FreeCAD's start blocked by the system: silent and idle, before the
    # measuring loop opens progress.jsonl.
    time.sleep(60)


class _Box:
    XMin = YMin = ZMin = 0.0
    XLength, YLength, ZLength = 1.6, 0.8, 0.45


class _Empty:
    Volume = 0


class _Shape:
    BoundBox = _Box()

    def common(self, other):
        return _Empty()


def makeBox(*args):
    return _Shape()


def read(path):
    name = os.path.basename(path)
    if name in os.environ.get("FAKE_HANG", "").split(","):
        # Stuck inside the reader of one model, HOLDING the GIL as Part.read
        # does: the heartbeat thread stays silent (a PyDLL call keeps the GIL).
        import ctypes
        ctypes.PyDLL("kernel32").Sleep(60000)
    if name in os.environ.get("FAKE_UNREADABLE", "").split(","):
        raise RuntimeError("unreadable STEP")
    return _Shape()
'''


@pytest.fixture
def fake_reader(monkeypatch, tmp_path, user_cache):
    """@brief A measuring child: this Python with fake FreeCAD/Part modules.

    @return Callable returning the number of children launched so far.
    """
    modules = tmp_path / "fake_modules"
    modules.mkdir()
    (modules / "FreeCAD.py").write_text(_FAKE_FREECAD, encoding="utf-8")
    (modules / "Part.py").write_text(_FAKE_PART, encoding="utf-8")
    launches = tmp_path / "launches.txt"
    monkeypatch.setenv("PYTHONPATH", str(modules))
    monkeypatch.setenv("FAKE_LAUNCHES", str(launches))
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: sys.executable)

    def count():
        return len(launches.read_text(encoding="utf-8").splitlines()) if launches.exists() else 0
    return count


def _models(tmp_path, *names):
    library = tmp_path / "3dmodels"
    (library / "X.3dshapes").mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = library / "X.3dshapes" / f"{name}.step"
        path.write_text(f"ISO-10303-21; {name}", encoding="utf-8")
        paths.append(path)
    return library, paths


# ── R10-1: a child that never starts blames nobody ────────────────────────

def test_r10_1_a_child_blocked_before_measuring_stops_the_batch_and_blames_no_model(
        fake_reader, monkeypatch, tmp_path):
    """@brief Positive: start blocked -> ONE child, no model None, nothing remembered."""
    monkeypatch.setenv("FAKE_BLOCK_START", "1")
    _, paths = _models(tmp_path, "a", "b", "c")
    started = time.monotonic()
    measured = km.measure_models(paths, timeout=1)
    assert time.monotonic() - started < 20
    assert fake_reader() == 1, "no new child per model after an environment failure"
    assert dict(measured) == {}, "absent = never asked, so retryable"
    assert measured.stalled == {} and measured.failures == {}


@pytest.mark.skipif(sys.platform != "win32", reason="the GIL-holding hang uses kernel32")
def test_r10_1_a_child_stuck_inside_one_model_still_blames_that_model(
        fake_reader, monkeypatch, tmp_path):
    """@brief Negative: once the loop has started, the model in progress is the culprit."""
    monkeypatch.setenv("FAKE_HANG", "b.step")
    _, (a, b, c) = _models(tmp_path, "a", "b", "c")
    measured = km.measure_models([a, b, c], timeout=1)
    assert fake_reader() == 2
    assert measured[str(a)] is not None and measured[str(c)] is not None
    assert measured[str(b)] is None and str(b) in measured.stalled


def test_r10_1_model_sizes_remembers_nothing_after_a_blocked_start(fake_reader, monkeypatch,
                                                                  tmp_path):
    """@brief Integration: after a restart (the session flag reset) every model is measured again."""
    monkeypatch.setenv("FAKE_BLOCK_START", "1")
    # The timeout default of measure_models is bound at import: shortened here.
    real_measure = km.measure_models
    monkeypatch.setattr(km, "measure_models",
                        lambda paths, **kw: real_measure(paths, timeout=1, **kw))
    library, paths = _models(tmp_path, "a", "b")
    km.model_sizes(paths, library)
    assert km.load_stalls() == {}
    monkeypatch.setenv("FAKE_BLOCK_START", "")
    # Within the session the blocked start is not paid again (see the next
    # test); a restart of the program forgets it, which the reset stands for.
    km.reset_unstarted_measuring()
    sizes = km.model_sizes(paths, library)
    assert all(value is not None for value in sizes.values())


def test_r10_1_a_blocked_start_is_not_paid_again_in_the_same_session(fake_reader, monkeypatch,
                                                                    tmp_path):
    """@brief Positive: after one blocked start no child is launched, and the note names the cause."""
    monkeypatch.setenv("FAKE_BLOCK_START", "1")
    # The timeout default of measure_models is bound at import: shortened here.
    real_measure = km.measure_models
    monkeypatch.setattr(km, "measure_models",
                        lambda paths, **kw: real_measure(paths, timeout=1, **kw))
    library, (a,) = _models(tmp_path, "a")
    _, (b,) = _models(tmp_path, "b")
    km.model_sizes([a], library)
    assert fake_reader() == 1
    started = time.monotonic()
    sizes = km.model_sizes([b], library)
    assert fake_reader() == 1 and time.monotonic() - started < 5, "no second silent wait"
    assert sizes == {str(b): None} and sizes.unstarted == 1
    notes = []
    km._note_remembered(sizes, notes.append)
    assert len(notes) == 1 and "1 s" in notes[0]
    assert km.load_stalls() == {}, "an environment failure is never written to disk"


def test_r10_1_the_session_flag_clears_on_reset_and_for_another_freecad(fake_reader, monkeypatch,
                                                                        tmp_path):
    """@brief Negative: a reset (as a restart does) or another FreeCAD build measures again."""
    monkeypatch.setenv("FAKE_BLOCK_START", "1")
    _, (a,) = _models(tmp_path, "a")
    km.measure_models([a], timeout=1)
    monkeypatch.setenv("FAKE_BLOCK_START", "")
    # Another interpreter file stands for another FreeCAD build: the base
    # Python behind this virtual environment runs the fake reader as well.
    other = getattr(sys, "_base_executable", None)
    if not other or os.path.normcase(other) == os.path.normcase(sys.executable):
        pytest.skip("no second interpreter file to stand for another FreeCAD build")
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: other)
    assert km.measure_models([a], timeout=30)[str(a)] is not None, "another build is a new question"
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: sys.executable)
    assert dict(km.measure_models([a], timeout=30)) == {}
    km.reset_unstarted_measuring()
    assert km.measure_models([a], timeout=30)[str(a)] is not None


def test_e3_one_service_forgets_the_verdicts_and_the_session_flag(fake_reader, monkeypatch,
                                                                   tmp_path):
    """@brief Positive: after retry_unmeasurable_models both memo and blocked start are gone.

    Negative: `forget_unmeasurable` alone leaves the session flag in place.
    """
    monkeypatch.setenv("FAKE_BLOCK_START", "1")
    _, (a,) = _models(tmp_path, "a")
    km.measure_models([a], timeout=1)
    km.record_stalls({str(a): 300}, "fc:1:1")
    monkeypatch.setenv("FAKE_BLOCK_START", "")
    km.forget_unmeasurable()
    assert dict(km.measure_models([a], timeout=30)) == {}, "the session flag survived"
    km.record_stalls({str(a): 300}, "fc:1:1")
    assert km.retry_unmeasurable_models() == 1
    assert km.load_stalls() == {}
    assert km.measure_models([a], timeout=30)[str(a)] is not None


def test_r10_1_a_stall_is_bound_to_the_freecad_build(user_cache, tmp_path):
    """@brief Positive and negative: same build skips; another build or a legacy record retries."""
    model = tmp_path / "M.step"
    model.write_text("ISO-10303-21;", encoding="utf-8")
    km.record_stalls({str(model): 300}, "fc:1:1")
    memo = km.load_stalls()
    assert km.stall_remembered(memo, model, 300, "fc:1:1")
    assert not km.stall_remembered(memo, model, 300, "fc:2:2")
    legacy = {km.index_key(model): {"status": "stalled", "timeout_s": 300.0,
                                    "model": model_size_cache.file_identity(model)}}
    assert not km.stall_remembered(legacy, model, 300, "fc:1:1"), \
        "a record written before the reader key cannot be verified"
    km.record_stalls({str(model): 300}, None)
    assert km.load_stalls() == memo, "without a reader nothing is written"


def test_r10_1_forget_unmeasurable_clears_the_verdicts_and_keeps_the_sizes(user_cache, tmp_path):
    """@brief Positive: the memo goes, the measured index stays; negative: nothing to forget -> 0."""
    model = tmp_path / "M.step"
    model.write_text("ISO-10303-21;", encoding="utf-8")
    km.record_stalls({str(model): 300}, "fc:1:1")
    index = user_cache / km.INDEX_FILENAME
    index.write_text("{}", encoding="utf-8")
    assert km.forget_unmeasurable() == 1
    assert km.load_stalls() == {} and index.is_file()
    assert km.forget_unmeasurable() == 0


# ── R10-2: sleep, unopenable files, silent replays ───────────────────────

class _SilentChild:
    """@brief A child double: silent and idle, alive for `lifetime` seconds of real time."""

    def __init__(self, lifetime):
        self.pid = 4242
        self.returncode = None
        self._end = time.monotonic() + lifetime

    def poll(self):
        if self.returncode is None and time.monotonic() >= self._end:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is not None:
            return self.returncode
        time.sleep(min(timeout or 0.0, max(0.0, self._end - time.monotonic())))
        if self.poll() is None:
            raise subprocess.TimeoutExpired("freecadcmd", timeout)
        return self.returncode

    def kill(self):
        self.returncode = -9


def _watch_silent(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(km, "kill_process_tree", lambda process: process.kill())
    monkeypatch.setattr(process_log, "job_cpu_seconds", lambda process: None)
    if clock is not None:
        monkeypatch.setattr(km, "awake_clock", clock)
    (tmp_path / "progress.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "out.json").write_text("{}", encoding="utf-8")
    outcome, _, _ = km._watch_measuring_child(_SilentChild(1.5), tmp_path, ["fc"], 0.3, None)
    return outcome


def test_r10_2_time_the_computer_slept_is_not_silence(monkeypatch, tmp_path):
    """@brief Positive: the awake clock stood still (a suspended PC) -> the child is not killed."""
    assert _watch_silent(monkeypatch, tmp_path, clock=lambda: 1000.0) == "done"


def test_r10_2_real_silence_is_still_a_stall(monkeypatch, tmp_path):
    """@brief Negative: the same silence on a running clock is declared stalled."""
    assert _watch_silent(monkeypatch, tmp_path, clock=None) == "stalled"


def test_r10_2_run_logged_counts_silence_on_the_awake_clock(monkeypatch, tmp_path):
    """@brief Positive and negative: a 1.5 s silence with timeout 0.4 s passes only on a still clock."""
    command = [sys.executable, "-c", "import time; time.sleep(1.5); print('done', flush=True)"]
    monkeypatch.setattr(process_log, "job_cpu_seconds", lambda process: None)
    monkeypatch.setattr(process_log, "awake_clock", lambda: 50.0)
    result = process_log.run_logged(command, tmp_path / "still.log", timeout=0.4)
    assert "done" in result.stdout
    monkeypatch.undo()
    monkeypatch.setattr(process_log, "job_cpu_seconds", lambda process: None)
    with pytest.raises(subprocess.TimeoutExpired):
        process_log.run_logged(command, tmp_path / "running.log", timeout=0.4)


def test_r10_2_awake_clock_runs_like_the_monotonic_clock_while_awake():
    """@brief The awake clock advances with real time when the PC does not sleep."""
    before, reference = process_log.awake_clock(), time.monotonic()
    time.sleep(0.3)
    elapsed = process_log.awake_clock() - before
    assert abs(elapsed - (time.monotonic() - reference)) < 0.1


def test_r10_2_a_file_the_child_cannot_open_is_not_a_verdict(fake_reader, tmp_path):
    """@brief Positive: an unopenable model is absent (retryable), the others are measured, one child."""
    library, (a, c) = _models(tmp_path, "a", "c")
    held = library / "X.3dshapes" / "b.step"
    held.mkdir()  # opening a folder as a file fails with an OSError, like a held file
    sizes = km.model_sizes([a, held, c], library)
    assert sizes[str(a)] is not None and sizes[str(c)] is not None
    assert sizes[str(held)] is None
    assert km.index_key(held) not in km.load_stalls(), "nothing remembered for it"
    assert fake_reader() == 1


def test_r10_2_a_model_the_reader_cannot_parse_is_remembered(fake_reader, monkeypatch, tmp_path):
    """@brief Negative: a readable file FreeCAD answers `null` for is a verdict, as before."""
    monkeypatch.setenv("FAKE_UNREADABLE", "b.step")
    library, (a, b) = _models(tmp_path, "a", "b")
    km.model_sizes([a, b], library)
    assert km.load_stalls()[km.index_key(b)]["status"] == "unreadable"


def test_r10_2_a_candidate_skipped_by_a_verdict_is_reported(fake_reader, monkeypatch, tmp_path):
    """@brief Positive: the second run names the skipped model; negative: the first run has none."""
    monkeypatch.setenv("FAKE_UNREADABLE", "b.step")
    library, (a, b) = _models(tmp_path, "a", "b")
    notes = []
    first = km.model_sizes([a, b], library)
    km._note_remembered(first, notes.append)
    assert first.remembered == {} and notes == []
    second = km.model_sizes([a, b], library)
    km._note_remembered(second, notes.append)
    assert second.remembered == {str(b): "unreadable"}
    assert len(notes) == 1 and "1" in notes[0]


# ── R10-3: outputs lost after the STEP are said ───────────────────────────

def test_r10_3_a_failed_wrl_and_preview_become_warnings_with_their_reason():
    """@brief Positive: the reason printed by the child, else the exit code, reaches the warning."""
    output = ("KICADGEN_STEP_OK:x.step\nKICADGEN_WRL_FAILED:disk full\n")
    warnings = step_generator.missing_output_warnings(output, 3)
    assert len(warnings) == 2
    assert "disk full" in warnings[0]
    assert "3" in warnings[1], "the preview has no reason of its own: the exit code explains it"


def test_r10_3_a_complete_run_has_no_output_warning_even_with_a_failing_exit():
    """@brief Negative: every output present -> nothing, whatever the exit code."""
    output = "KICADGEN_STEP_OK:x\nKICADGEN_WRL_OK:x.wrl\nKICADGEN_MESH_OK:x.json\n"
    assert step_generator.missing_output_warnings(output, 0) == []
    assert step_generator.missing_output_warnings(output, 1) == []


def _fake_build(monkeypatch, behaviour):
    """@brief Replace the builder's child with `behaviour(payload, call_index)` -> (output, code).

    The solo import check sends a payload with only ``solo_import``.
    """
    calls = []

    def run_logged(command, log_path, **kwargs):
        payload = json.loads(Path(command[2]).read_text(encoding="utf-8"))
        calls.append(payload)
        output, code = behaviour(payload, len(calls))
        return subprocess.CompletedProcess(command, code, output, "")
    monkeypatch.setattr(process_log, "run_logged", run_logged)
    return calls


def _write_step(payload):
    Path(payload["step_path"]).write_text(COMPLETE_STEP, encoding="utf-8")
    return f"KICADGEN_STEP_OK:{payload['step_path']}\n"


def test_r10_3_a_child_dying_after_the_step_keeps_the_step_and_warns(monkeypatch, tmp_path):
    """@brief Positive: STEP published, no WRL, a warning with the exit code."""
    _fake_build(monkeypatch, lambda payload, n: (_write_step(payload), 3))
    result = step_generator._run_freecad("M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
                                         freecadcmd_path=sys.executable)
    assert result["step_path"].is_file() and result["wrl_path"] is None
    assert any("WRL" in warning and "3" in warning for warning in result["warnings"])


def test_r10_3_the_child_tessellates_inside_a_handler():
    """@brief Structural: every `tessellate` call of the builder sits inside a `try`.

    The STEP is published before the tessellation; an exception there used to
    end the script, losing WRL and preview without a FAILED marker.
    """
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    main = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    guarded = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Try) and node.handlers:
            for inner in node.body:
                guarded.update(id(sub) for sub in ast.walk(inner))
    calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "tessellate"]
    assert calls and all(id(call) in guarded for call in calls)


# ── R10-4: a library model that crashes the builder ───────────────────────

def test_r10_4_the_import_in_progress_is_the_one_without_an_outcome():
    """@brief Positive: an open import gives its payload index; negative: a closed one (OK or FAILED) does not."""
    assert step_generator.import_in_progress(
        "KICADGEN_IMPORT_START:0:/lib/a.step\nKICADGEN_IMPORT_OK:/lib/a.step; parts=1\n"
        "KICADGEN_IMPORT_START:3:C:\\lib\\b.step\n") == 3
    assert step_generator.import_in_progress(
        "KICADGEN_IMPORT_START:0:/lib/a.step\nKICADGEN_IMPORT_FAILED:0:/lib/a.step\n") is None
    assert step_generator.import_in_progress("KICADGEN_COMPONENT_OK:U1\n") is None


def test_h_the_stage_of_an_open_import_reads_the_loaded_marker():
    """@brief Positive: LOADED after START -> "analysis"; negative: START alone -> "reading", closed -> None."""
    loaded = ("KICADGEN_IMPORT_START:1:/lib/a.step\n"
              "KICADGEN_IMPORT_LOADED:1:/lib/a.step\n")
    assert step_generator.import_crash_stage(loaded) == "analysis"
    assert step_generator.import_in_progress(loaded) == 1, "LOADED does not close the import"
    assert step_generator.import_crash_stage("KICADGEN_IMPORT_START:1:/lib/a.step\n") == "reading"
    assert step_generator.import_crash_stage(
        loaded + "KICADGEN_IMPORT_OK:/lib/a.step; parts=1\n") is None
    assert step_generator.import_crash_stage(
        loaded + "KICADGEN_IMPORT_START:2:/lib/b.step\n") == "reading", \
        "the LOADED of the previous import does not carry over"


def test_r10_4_the_echoed_path_never_decides_the_identity():
    """@brief Positive: «;» or «:» in the path still close the import; negative: a START without index blames nobody."""
    path = "C:/lib/odd;na:me.step"
    assert step_generator.import_in_progress(
        f"KICADGEN_IMPORT_START:2:{path}\nKICADGEN_IMPORT_OK:{path}; parts=1\n") is None
    assert step_generator.import_in_progress(f"KICADGEN_IMPORT_START:2:{path}\n") == 2
    assert step_generator.import_in_progress(f"KICADGEN_IMPORT_START:{path}\n") is None


def _crashing_build(model, solo_crashes=True):
    """@brief A child that dies inside the import of `model` unless the payload blocks it.

    @param solo_crashes Whether the model imported ALONE kills the child too.
    """
    def behaviour(payload, n):
        if "solo_import" in payload:
            assert payload["solo_import"] == str(model)
            return (("KICADGEN_SOLO_IMPORT_START\n", 3221225477) if solo_crashes
                    else ("KICADGEN_SOLO_IMPORT_START\nKICADGEN_SOLO_IMPORT_DONE\n", 0))
        blocked = any(c.get("model_import_blocked") for c in payload["components"])
        if not blocked:
            return f"KICADGEN_IMPORT_START:0:{model}\n", 3221225477
        return _write_step(payload) + "KICADGEN_WRL_OK:x\nKICADGEN_MESH_OK:x\n", 0
    return behaviour


def _component(model):
    return {"name": "U1", "kind": "ic", "x": 0.0, "y": 0.0, "length_mm": 5.0, "width_mm": 4.0,
            "height_mm": 1.0, "model_path": str(model)}


def test_r10_4_a_model_that_crashes_the_import_is_remembered_and_skipped(
        monkeypatch, tmp_path, user_cache):
    """@brief Positive: crash, confirmed by a solo import -> remembered; the next run skips it at once."""
    model = tmp_path / "lib" / "BAD.step"
    model.parent.mkdir()
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    calls = _fake_build(monkeypatch, _crashing_build(model))
    component = _component(model)
    result = step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
        freecadcmd_path=sys.executable, extra_payload={"components": [component]})
    assert len(calls) == 3 and "solo_import" in calls[1]
    assert calls[2]["components"][0]["model_import_blocked"] is True
    assert "model_import_blocked" not in component, "the caller's payload is not changed"
    assert any("BAD.step" in warning for warning in result["warnings"])
    record = km.load_stalls()[km.index_key(model)]
    assert record["status"] == km.IMPORT_CRASH_STATUS
    assert record["reader"] == model_size_cache.file_identity(Path(sys.executable))
    step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
        freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    assert len(calls) == 4, "the remembered model is blocked from the first launch"
    assert calls[3]["components"][0]["model_import_blocked"] is True


def test_e2_a_second_crashing_model_is_solo_checked_and_every_notice_kept(
        monkeypatch, tmp_path, user_cache):
    """@brief Positive: A crashes, then B crashes in the retry -> both checked, blocked, named."""
    first, second = tmp_path / "A.step", tmp_path / "B.step"
    for model in (first, second):
        model.write_text(COMPLETE_STEP, encoding="utf-8")

    def behaviour(payload, n):
        if "solo_import" in payload:
            return "KICADGEN_SOLO_IMPORT_START\n", 3221225477
        blocked = {c["model_path"] for c in payload["components"] if c.get("model_import_blocked")}
        for index, model in enumerate((first, second)):
            if str(model) not in blocked:
                return f"KICADGEN_IMPORT_START:{index}:{model}\n", 3221225477
        return _write_step(payload) + "KICADGEN_WRL_OK:x\nKICADGEN_MESH_OK:x\n", 0
    calls = _fake_build(monkeypatch, behaviour)
    result = step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out", freecadcmd_path=sys.executable,
        extra_payload={"components": [_component(first), dict(_component(second), name="U2")]})
    assert [("solo" if "solo_import" in call else "build") for call in calls] == [
        "build", "solo", "build", "solo", "build"]
    assert any("A.step" in w for w in result["warnings"])
    assert any("B.step" in w for w in result["warnings"])
    memo = km.load_stalls()
    assert all(memo[km.index_key(m)]["status"] == km.IMPORT_CRASH_STATUS for m in (first, second))


def test_e2_a_crash_on_an_already_blocked_model_is_a_plain_failure(monkeypatch, tmp_path,
                                                                   user_cache):
    """@brief Negative: a crash naming a model already blocked ends the loop with Cad3DError."""
    model = tmp_path / "A.step"
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    calls = _fake_build(monkeypatch, lambda payload, n: (
        ("KICADGEN_SOLO_IMPORT_START\n", 3221225477) if "solo_import" in payload
        else (f"KICADGEN_IMPORT_START:0:{model}\n", 3221225477)))
    with pytest.raises(Cad3DError):
        step_generator._run_freecad(
            "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
            freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    assert len(calls) == 3, "build, solo check, one retry: then it stops"


@pytest.mark.parametrize("loaded", [True, False], ids=["analysis", "reading"])
def test_h_a_crash_in_the_analysis_is_checked_and_remembered_as_such(monkeypatch, tmp_path,
                                                                      user_cache, loaded):
    """@brief Positive: died after LOADED -> the solo child gets the whole component, the verdict says "analysis".

    Negative: died before LOADED -> the verdict says "reading" and the
    import wording is used.
    """
    model = tmp_path / "A.step"
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    solo_payloads = []

    def behaviour(payload, n):
        if "solo_import" in payload:
            solo_payloads.append(payload)
            return "KICADGEN_SOLO_IMPORT_START\n", 3221225477
        if not payload["components"][0].get("model_import_blocked"):
            text = f"KICADGEN_IMPORT_START:0:{model}\n"
            if loaded:
                text += f"KICADGEN_IMPORT_LOADED:0:{model}\n"
            return text, 3221225477
        return _write_step(payload) + "KICADGEN_WRL_OK:x\nKICADGEN_MESH_OK:x\n", 0
    _fake_build(monkeypatch, behaviour)
    result = step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
        freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    solo = solo_payloads[0]
    assert solo["component"]["model_path"] == str(model)
    assert "model_import_blocked" not in solo["component"]
    assert Path(solo["cad_dir"]) == CAD_DIR
    record = km.load_stalls()[km.index_key(model)]
    assert record["status"] == km.IMPORT_CRASH_STATUS
    assert record["stage"] == ("analysis" if loaded else "reading")
    warning = next(w for w in result["warnings"] if "A.step" in w)
    assert ("analisi" in warning) is loaded


def test_g1_a_solo_check_that_never_reached_the_import_blames_nobody(monkeypatch, tmp_path,
                                                                     user_cache):
    """@brief Negative: the solo child failed before its START (antivirus, full disk) -> nothing remembered.

    The positive twin (START then a crash) is
    `test_r10_4_a_model_that_crashes_the_import_is_remembered_and_skipped`.
    """
    model = tmp_path / "A.step"
    model.write_text(COMPLETE_STEP, encoding="utf-8")

    def behaviour(payload, n):
        if "solo_import" in payload:
            return "Traceback: cannot open the script\n", 1
        if not payload["components"][0].get("model_import_blocked"):
            return f"KICADGEN_IMPORT_START:0:{model}\n", 3221225477
        return _write_step(payload) + "KICADGEN_WRL_OK:x\nKICADGEN_MESH_OK:x\n", 0
    calls = _fake_build(monkeypatch, behaviour)
    result = step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
        freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    assert len(calls) == 3 and km.load_stalls() == {}
    assert any("A.step" in w for w in result["warnings"])


def test_g2_a_non_ascii_model_is_blocked_by_its_index_not_by_the_echoed_path(
        monkeypatch, tmp_path, user_cache):
    """@brief Positive: the child echoes a mangled path, the payload's own model is blocked and remembered.

    Negative: another component of the same run keeps its import.
    """
    model = tmp_path / "Ωmega_模型.step"
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    other = tmp_path / "OTHER.step"
    other.write_text(COMPLETE_STEP, encoding="utf-8")

    def behaviour(payload, n):
        if "solo_import" in payload:
            assert payload["solo_import"] == str(model), "the solo check uses the payload's path"
            return "KICADGEN_SOLO_IMPORT_START\n", 3221225477
        if not payload["components"][1].get("model_import_blocked"):
            # errors="replace" decoding turned the non-ASCII letters into «?».
            return f"KICADGEN_IMPORT_START:1:{tmp_path}/?mega_??.step\n", 3221225477
        return _write_step(payload) + "KICADGEN_WRL_OK:x\nKICADGEN_MESH_OK:x\n", 0
    calls = _fake_build(monkeypatch, behaviour)
    result = step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out", freecadcmd_path=sys.executable,
        extra_payload={"components": [dict(_component(other), name="U0"), _component(model)]})
    assert result["step_path"].is_file()
    final = calls[-1]["components"]
    assert final[1].get("model_import_blocked") is True
    assert not final[0].get("model_import_blocked")
    assert km.load_stalls()[km.index_key(model)]["status"] == km.IMPORT_CRASH_STATUS


def test_r10_4_a_crash_the_solo_import_does_not_repeat_is_not_remembered(
        monkeypatch, tmp_path, user_cache):
    """@brief Negative: the model alone reads fine -> approximate shape this run, nothing remembered."""
    model = tmp_path / "lib" / "GOOD.step"
    model.parent.mkdir()
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    calls = _fake_build(monkeypatch, _crashing_build(model, solo_crashes=False))
    result = step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
        freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    assert len(calls) == 3 and calls[2]["components"][0]["model_import_blocked"] is True
    assert km.load_stalls() == {}, "an unconfirmed crash poisons no model"
    assert any("GOOD.step" in warning for warning in result["warnings"])
    step_generator._run_freecad(
        "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
        freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    assert "model_import_blocked" not in calls[3]["components"][0], "the next run tries it again"


def test_r10_4_a_crash_outside_any_import_blames_no_model(monkeypatch, tmp_path, user_cache):
    """@brief Negative: the child died after the import closed -> failed run, nothing remembered."""
    model = tmp_path / "GOOD.step"
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    calls = _fake_build(monkeypatch, lambda payload, n: (
        f"KICADGEN_IMPORT_START:0:{model}\nKICADGEN_IMPORT_OK:{model}; parts=1\n", 3))
    with pytest.raises(Cad3DError):
        step_generator._run_freecad(
            "M", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
            freecadcmd_path=sys.executable, extra_payload={"components": [_component(model)]})
    assert len(calls) == 1 and km.load_stalls() == {}


def test_r10_4_another_freecad_build_imports_the_model_again(tmp_path, user_cache):
    """@brief Negative: the verdict belongs to (file, FreeCAD build)."""
    model = tmp_path / "BAD.step"
    model.write_text(COMPLETE_STEP, encoding="utf-8")
    km.record_import_crash(model, "fc:1:1", 3)
    memo = km.load_stalls()
    assert km.import_crash_remembered(memo, model, "fc:1:1")
    assert not km.import_crash_remembered(memo, model, "fc:2:2")
    assert not km.import_crash_remembered(memo, model, None)


def test_r10_4_the_child_does_not_import_a_blocked_model(monkeypatch, capsys):
    """@brief Positive: blocked -> no import attempted, failed-import placeholder; negative: not blocked -> imported."""
    import importlib.util
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(Vector=lambda *v: v))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("c3_parts", CAD_DIR / "freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reads = []
    monkeypatch.setattr(module, "_read_library_parts",
                        lambda path, color, heartbeat: reads.append(path) or (_ for _ in ()).throw(
                            RuntimeError("not a real reader")))
    monkeypatch.setattr(module.Part, "read", lambda path: (_ for _ in ()).throw(
        RuntimeError("not a real reader")), raising=False)
    blocked = dict(_component("lib/BAD.step"), model_import_blocked=True)
    assert module._library_model(blocked, 0, 0, 0, "#000") is None
    assert reads == [] and "KICADGEN_IMPORT_START" not in capsys.readouterr().out
    assert blocked["_model_import_error"]
    plain = _component("lib/OTHER.step")
    assert module._library_model(plain, 0, 0, 0, "#000") is None
    out = capsys.readouterr().out
    assert reads == ["lib/OTHER.step"]
    assert "KICADGEN_IMPORT_START:lib/OTHER.step" in out
    assert "KICADGEN_IMPORT_FAILED:lib/OTHER.step" in out, "a handled failure closes the import"
    numbered = dict(_component("lib/OTHER.step"), _payload_index=4)
    module._library_model(numbered, 0, 0, 0, "#000")
    out = capsys.readouterr().out
    assert "KICADGEN_IMPORT_START:4:lib/OTHER.step" in out
    assert "KICADGEN_IMPORT_FAILED:4:lib/OTHER.step" in out
    assert "KICADGEN_IMPORT_LOADED" not in out, "a read that failed never reached the analysis"
    # A read that succeeds announces LOADED before the analysis (which then
    # fails here on the fake Part module, closing the import as FAILED).
    monkeypatch.setattr(module, "_read_library_parts",
                        lambda path, color, heartbeat: [(object(), color)])
    module._library_model(dict(numbered), 0, 0, 0, "#000")
    out = capsys.readouterr().out
    assert out.index("KICADGEN_IMPORT_LOADED:4:lib/OTHER.step") < out.index(
        "KICADGEN_IMPORT_FAILED:4:lib/OTHER.step")


# ── R10-5: where the KiCad 3D library is found ────────────────────────────

@pytest.fixture
def bare_machine(monkeypatch, tmp_path):
    """@brief A machine with no KiCad anywhere: every source of roots points into tmp_path."""
    from kicadgen import app_paths
    from kicadgen.services import installed_programs
    for name in list(os.environ):
        if name.upper().startswith("KICAD") and ("3DMODEL" in name.upper()
                                                 or "3RD_PARTY" in name.upper()):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(km, "_INSTALL_BASES", ())
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [])
    monkeypatch.setattr(installed_programs, "registered_installs", lambda name: [])
    monkeypatch.setattr(app_paths, "_documents_dir", lambda: tmp_path / "nodocs")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


def _library(folder):
    (folder / "Package_SO.3dshapes").mkdir(parents=True)
    return folder


def test_r10_5_bare_machine_has_no_library(bare_machine):
    """@brief Negative baseline: nothing declared, nothing found."""
    assert km.find_library() is None


def test_r10_5_kicad_versioned_variable_names_the_library(bare_machine, monkeypatch):
    """@brief Positive: KICAD9_3DMODEL_DIR; negative: a variable naming a folder without models."""
    empty = bare_machine / "empty"
    empty.mkdir()
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(empty))
    assert km.find_library() is None
    library = _library(bare_machine / "moved" / "3dmodels")
    monkeypatch.setenv("KICAD9_3DMODEL_DIR", str(library))
    assert km.find_library() == library


def test_r10_5_declared_program_root_and_registry_are_searched(bare_machine, monkeypatch):
    """@brief Positive: another drive's Program Files, then a registry folder; negative: a folder without share."""
    from kicadgen.services import installed_programs
    elsewhere = bare_machine / "D" / "KiCad" / "9.0" / "bin"
    elsewhere.mkdir(parents=True)
    monkeypatch.setattr(installed_programs, "registered_installs",
                        lambda name: [(elsewhere, None)])
    assert km.find_library() is None, "a registered folder without its 3D models is not a library"
    library = _library(bare_machine / "D" / "KiCad" / "9.0" / "share" / "kicad" / "3dmodels")
    assert km.find_library() == library
    monkeypatch.setattr(installed_programs, "registered_installs", lambda name: [])
    root = bare_machine / "Programmi"
    library = _library(root / "KiCad" / "10.0" / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [root])
    assert km.find_library() == library


def test_r10_5_the_newest_kicad_wins_whatever_the_source(bare_machine, monkeypatch):
    """@brief Positive: KiCad 10 under a program root beats a stale KICAD8 variable and a KiCad 9 registry entry.

    Negative: with KiCad 10 gone, the variable for 8 still loses to the
    registered 9; the explicit unversioned override always comes first.
    """
    from kicadgen.services import installed_programs
    stale = _library(bare_machine / "old" / "kicad8" / "3dmodels")
    monkeypatch.setenv("KICAD8_3DMODEL_DIR", str(stale))
    registered = bare_machine / "R" / "KiCad" / "9.0"
    nine = _library(registered / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "registered_installs",
                        lambda name: [(registered, None)])
    root = bare_machine / "PF"
    ten = _library(root / "KiCad" / "10.0" / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [root])
    assert km.find_library() == ten
    assert km._search_roots(None)[:3] == [ten, nine, stale]
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [])
    assert km.find_library() == nine
    explicit = _library(bare_machine / "explicit")
    monkeypatch.setenv(km.LIBRARY_ENV, str(explicit))
    assert km.find_library() == explicit


def test_e1_a_registered_kicad_in_a_custom_folder_is_ranked_by_its_declared_version(
        bare_machine, monkeypatch):
    """@brief Positive: DisplayVersion 10.0.1 in D:/Tools/KiCad beats KiCad 8.0 in Program Files.

    Negative: the same entry without a declared version has no version and
    comes after every versioned library.
    """
    from kicadgen.services import installed_programs
    custom = bare_machine / "Tools" / "KiCad"
    ten = _library(custom / "share" / "kicad" / "3dmodels")
    root = bare_machine / "PF"
    eight = _library(root / "KiCad" / "8.0" / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [root])
    monkeypatch.setattr(installed_programs, "registered_installs",
                        lambda name: [(custom / "bin", "10.0.1-1")])
    assert km.find_library() == ten
    monkeypatch.setattr(installed_programs, "registered_installs",
                        lambda name: [(custom / "bin", None)])
    assert km.find_library() == eight


def test_e1_registered_installs_carries_the_declared_version():
    """@brief Positive: (folder, DisplayVersion); negative: the folder list keeps its old shape."""
    from kicadgen.services import installed_programs
    entries = [{"DisplayName": "KiCad 10.0", "InstallLocation": r"D:\Tools\KiCad",
                "DisplayVersion": "10.0.1"},
               {"DisplayName": "KiCad 8.0", "InstallLocation": r"C:\Program Files\KiCad\8.0"}]
    assert installed_programs.registered_installs("KiCad", entries) == [
        (Path(r"D:\Tools\KiCad"), "10.0.1"), (Path(r"C:\Program Files\KiCad\8.0"), None)]
    assert installed_programs.registered_install_dirs("KiCad", entries) == [
        Path(r"D:\Tools\KiCad"), Path(r"C:\Program Files\KiCad\8.0")]


def test_r10_5_redirected_documents_hold_the_third_party_repositories(bare_machine, monkeypatch):
    """@brief Positive: OneDrive Documents; negative: the same tree under an undeclared folder."""
    from kicadgen import app_paths
    undeclared = bare_machine / "elsewhere" / "KiCad" / "9.0" / "3rdparty" / "3dmodels" / "vendor"
    _library(undeclared)
    assert undeclared not in list(km._candidate_roots())
    documents = bare_machine / "OneDrive" / "Documenti"
    repository = _library(documents / "KiCad" / "9.0" / "3rdparty" / "3dmodels" / "vendor")
    monkeypatch.setattr(app_paths, "_documents_dir", lambda: documents)
    assert repository in list(km._candidate_roots())


# ── R10-6: removing an old output waits for a transient refusal ───────────

class _HeldFile:
    """@brief A path whose unlink is refused `refusals` times, then succeeds."""

    def __init__(self, refusals):
        self.refusals = refusals
        self.removed = False

    def unlink(self):
        if self.refusals > 0:
            self.refusals -= 1
            raise PermissionError(32, "in use")
        self.removed = True


def test_r10_6_a_briefly_held_output_is_removed():
    """@brief Positive: two refusals then success -> removed, no error."""
    held = _HeldFile(2)
    step_generator._remove_output(held)
    assert held.removed


def test_r10_6_a_file_held_for_good_still_fails_the_run():
    """@brief Negative: always refused -> Cad3DError, as before."""
    with pytest.raises(Cad3DError):
        step_generator._remove_output(_HeldFile(100))


# ── R10-7: a failed attachment is all or nothing ──────────────────────────

def test_r10_7_a_failed_attachment_leaves_every_payload_as_it_was(monkeypatch):
    """@brief Positive: the first payload got a model before the error -> undone."""
    components = [{"name": "U1", "match": {"a": 1}}, {"name": "U2"}]
    before = json.loads(json.dumps(components))

    def half_done(items, **kwargs):
        items[0]["model_path"] = "/lib/U1.step"
        items[0]["match"]["a"] = 2
        raise RuntimeError("index broken")
    monkeypatch.setattr(km, "attach_models", half_done)
    reports, notes = [], []
    step_generator.attach_library_models(components, [], notes, reports.append)
    assert components == before and len(reports) == 1


def test_r10_7_a_successful_attachment_is_kept(monkeypatch):
    """@brief Negative: no error -> the models chosen stay."""
    components = [{"name": "U1"}]
    monkeypatch.setattr(km, "attach_models",
                        lambda items, **kwargs: items[0].update(model_path="/lib/U1.step"))
    step_generator.attach_library_models(components, [], [], lambda message: None)
    assert components[0]["model_path"] == "/lib/U1.step"


# ── R10-8: a transient read error is not cached ───────────────────────────

def test_r10_8_link_hints_read_again_after_a_transient_error(monkeypatch, tmp_path):
    """@brief Positive: a refused read is not remembered for the stamp; negative: a non-UTF-8 file is."""
    footprint = tmp_path / "A.kicad_mod"
    footprint.write_text('(footprint "A" (model "x/A.step"))', encoding="utf-8")
    km._link_hints.cache_clear()
    answers = [PermissionError(32, "in use")]
    real = km._scan_link_hints

    def scan(path):
        if answers:
            raise answers.pop()
        return real(path)
    monkeypatch.setattr(km, "_scan_link_hints", scan)
    assert km._link_hints(footprint, "stamp") == (frozenset(), False)
    assert km._link_hints(footprint, "stamp") == (frozenset({"a.step"}), False)

    reads = []
    monkeypatch.setattr(km, "_scan_link_hints",
                        lambda path: reads.append(path) or (_ for _ in ()).throw(
                            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")))
    km._link_hints(footprint, "binary")
    km._link_hints(footprint, "binary")
    assert len(reads) == 1, "a content failure is a property of the stamp"


def test_r10_8_footprint_record_read_again_after_a_transient_error(tmp_path):
    """@brief Positive: missing then present under one stamp -> parsed; negative: bad content stays cached."""
    km._footprint_record.cache_clear()
    footprint = tmp_path / "F.kicad_mod"
    first = km._footprint_record(footprint, "s1")
    assert first[2], "a missing file is an error for this call"
    footprint.write_text('(footprint "F" (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")) '
                         '(model "x/F.step"))', encoding="utf-8")
    size, model, error = km._footprint_record(footprint, "s1")
    assert error is None and size == [1.0, 1.0]

    bad = tmp_path / "B.kicad_mod"
    bad.write_text("(not_a_footprint)", encoding="utf-8")
    assert km._footprint_record(bad, "s2")[2]
    bad.write_text('(footprint "B" (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")) '
                   '(model "x/B.step"))', encoding="utf-8")
    assert km._footprint_record(bad, "s2")[2], "same stamp, same cached content verdict"


# ── R4-2 (from C9): the preview carries each contact's pin number as text ──

def test_r4_2_preview_parts_carry_the_pin_number_as_text(tmp_path):
    """@brief Positive: «A1», «001» and a duplicated «A1» keep their text; negative: the body has none.

    Needs a real FreeCAD (skipped on the GitHub runner): the field is written
    by the child from the contact it built, and FreeCAD renames the second
    «PinA1» to «PinA001», which is exactly what the field must survive.
    """
    try:
        step_generator.find_freecadcmd()
    except Cad3DError as exc:
        pytest.skip(f"FreeCAD unavailable: {exc}")
    result = step_generator._run_freecad(
        "M", [["A1", 0.0, 0.0], ["001", 2.54, 0.0], ["A1", 5.08, 0.0]],
        Body3DParams(10.0, 8.0, 2.0), tmp_path / "out")
    parts = json.loads(Path(result["mesh_path"]).read_text(encoding="utf-8"))["parts"]
    pins = sorted(part["pin"] for part in parts if "pin" in part)
    assert pins == ["001", "A1", "A1"]
    assert all(isinstance(pin, str) for pin in pins)
    renamed = [part for part in parts if part.get("pin") == "A1" and part["name"] != "PinA1"]
    assert renamed, "FreeCAD renamed the duplicate, the field still names the pin"
    assert not any("pin" in part for part in parts if part["name"] == "Body")
    assert result["warnings"] == [], "a clean run has no output warning"


# ── R10-2 residual: the candidate preview's limit ignores sleep too ──────

def test_r10_2_the_candidate_preview_limit_runs_on_the_awake_clock(monkeypatch):
    """@brief Positive: a still awake clock (a suspended PC) lets a 2 s child finish under a 1 s limit.

    Negative: on the running clock the same child is killed at the limit.
    """
    from kicadgen.gui import model_candidates_dialog as mcd
    command = [sys.executable, "-c", "import time; time.sleep(2); print('done', flush=True)"]
    monkeypatch.setattr(mcd, "PREVIEW_TIMEOUT_S", 1.0)
    monkeypatch.setattr(process_log, "awake_clock", lambda: 7.0)
    assert "done" in mcd._run_preview_child(command).stdout
    monkeypatch.undo()
    monkeypatch.setattr(mcd, "PREVIEW_TIMEOUT_S", 1.0)
    with pytest.raises(subprocess.TimeoutExpired):
        mcd._run_preview_child(command)
