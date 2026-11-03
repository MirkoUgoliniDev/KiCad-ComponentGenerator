"""@brief The model-measuring child beats while a slow read works, and leaves a log (2026-11-03).

The measuring child had its own watchdog that counted silence as "no new
line in progress.jsonl", i.e. one line per FINISHED model, and the script had
no heartbeat: a healthy `Part.read` longer than the timeout was killed and
remembered as stalled forever. Its `freecad.out` lived in the temporary
exchange folder and was deleted with it. No FreeCAD here: a Python measurer
speaks the same file protocol.
"""
import json
import sys
import time

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import model_size_cache
from kicadgen.cad3d import process_log
from kicadgen.cad3d import step_generator as cad

#: Fake measurer: one slow model (longer than the watchdog's timeout), which
#: beats on stdout at the period the parent passed, exactly like the real
#: script, when `beat` is true.
_SLOW_MEASURER = """
import json, os, sys, threading, time
folder = os.environ["KICADGEN_MEASURE_DIR"]
interval = float(os.environ["KICADGEN_HEARTBEAT_S"])
with open(os.path.join(folder, "in.json"), encoding="utf-8") as handle:
    paths = json.load(handle)
if {beat}:
    def beat():
        while True:
            time.sleep(interval)
            print("KICADGEN_MEASURE_ALIVE", flush=True)
    threading.Thread(target=beat, daemon=True).start()
sizes = {{}}
with open(os.path.join(folder, "progress.jsonl"), "w", encoding="utf-8") as done:
    for path in paths:
        time.sleep({duration})
        sizes[path] = {{"total": [1.6, 0.8, 0.4], "body": [1.6, 0.8, 0.4]}}
        done.write(json.dumps({{"path": path, "size": sizes[path]}}) + chr(10))
        done.flush()
with open(os.path.join(folder, "out.json"), "w", encoding="utf-8") as handle:
    json.dump(sizes, handle)
"""


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    """User cache folder redirected into the test's temporary folder."""
    folder = tmp_path / "cache"
    monkeypatch.setattr(model_size_cache, "user_cache_dirs", lambda: [folder])
    return folder


def _arm(monkeypatch, *, beat, duration):
    monkeypatch.setattr(km, "_MEASURE_SCRIPT", _SLOW_MEASURER.format(beat=beat, duration=duration))
    monkeypatch.setattr(cad, "find_freecadcmd", lambda: sys.executable)


def _model(tmp_path, name="R_slow"):
    folder = tmp_path / "lib" / "Resistor_SMD.3dshapes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.step"
    path.write_text("STEP finto", encoding="utf-8")
    return path


def test_a_slow_read_that_beats_is_not_declared_stalled(monkeypatch, tmp_path, cache_dir):
    """@brief Positive: one model longer than the timeout, beating, is measured."""
    _arm(monkeypatch, beat=True, duration=3.0)
    model = _model(tmp_path)
    result = km.measure_models([model], timeout=1.5)
    assert result[str(model)]["total"] == [1.6, 0.8, 0.4]
    assert not getattr(result, "stalled", {})


def test_a_silent_slow_read_is_still_stalled(monkeypatch, tmp_path, cache_dir):
    """@brief Negative: the same model WITHOUT heartbeat is stalled at the timeout, as before."""
    _arm(monkeypatch, beat=False, duration=6.0)
    model = _model(tmp_path)
    started = time.monotonic()
    result = km.measure_models([model], timeout=1.5)
    assert result[str(model)] is None
    assert result.stalled == {str(model): 1.5}
    assert time.monotonic() - started < 6.0


def test_the_heartbeat_period_comes_from_the_watchdog_timeout(monkeypatch, tmp_path, cache_dir):
    """@brief The child receives heartbeat_interval(timeout), never a number of its own."""
    seen = {}
    script = ("import os, json\n"
              "folder = os.environ['KICADGEN_MEASURE_DIR']\n"
              "open(os.path.join(folder, 'out.json'), 'w').write(json.dumps("
              "{'beat': os.environ.get('KICADGEN_HEARTBEAT_S')}))\n")
    monkeypatch.setattr(km, "_MEASURE_SCRIPT", script)
    monkeypatch.setattr(cad, "find_freecadcmd", lambda: sys.executable)
    seen.update(km.measure_models([_model(tmp_path)], timeout=40))
    assert float(seen["beat"]) == process_log.heartbeat_interval(40)


def test_the_real_script_beats_and_announces_each_model():
    """@brief Structural: the FreeCAD script reads the parent's period and prints per model."""
    assert "KICADGEN_HEARTBEAT_S" in km._MEASURE_SCRIPT
    assert "KICADGEN_MEASURE_ALIVE" in km._MEASURE_SCRIPT
    assert "KICADGEN_MEASURE_DONE" in km._MEASURE_SCRIPT
    compile(km._MEASURE_SCRIPT, "<measure>", "exec")


def test_the_child_output_survives_in_a_persistent_log(monkeypatch, tmp_path, cache_dir):
    """@brief Positive: after the exchange folder is gone, the child's output tail is in the cache log."""
    script = ("import os, json\n"
              "print('FreeCAD says: reading model', flush=True)\n"
              "folder = os.environ['KICADGEN_MEASURE_DIR']\n"
              "open(os.path.join(folder, 'out.json'), 'w').write(json.dumps({}))\n")
    monkeypatch.setattr(km, "_MEASURE_SCRIPT", script)
    monkeypatch.setattr(cad, "find_freecadcmd", lambda: sys.executable)
    km.measure_models([_model(tmp_path)], timeout=40)
    log = (cache_dir / km.MEASURE_LOG_FILENAME).read_text(encoding="utf-8")
    assert "FreeCAD says: reading model" in log
    assert "measure outcome=done" in log


def test_an_unwritable_log_never_breaks_the_measurement(monkeypatch, tmp_path):
    """@brief Negative: a cache folder that cannot be written loses the log, not the haul."""
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setattr(model_size_cache, "user_cache_dirs", lambda: [blocker / "cache"])
    _arm(monkeypatch, beat=False, duration=0.0)
    model = _model(tmp_path)
    result = km.measure_models([model], timeout=40)
    assert result[str(model)]["total"] == [1.6, 0.8, 0.4]
    assert json.dumps(dict(result))
