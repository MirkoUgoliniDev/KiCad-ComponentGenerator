"""@brief Follow-ups of the 2026-10-27 review sweep (S2-S7).

Each item has a positive and a negative case that the rule must tell apart
for a structural reason:

- S3  the footprint parameter validators use the shared file-name rule: a
      path such as "..\\evil" is refused, a plain name such as "ESP32-S3" is
      accepted, by every validator.
- S4  `_run_freecad` without progress and without cancel goes through
      `run_logged` too: the silence timeout restarts at every line and, on
      expiry, the whole process tree is killed (no overall-duration cap).
- S5  tools/evaluate_photo_inventory.py reads the crop through
      `crop_box_from_metadata`: the original layout is used, the deskewed
      one is refused with an explicit message, even with --crop.
- S6  a pin name with a space is written as KiCad rewrites it and the symbol
      tab says so on its status line, once.
- S7  the resolution warning names the coarse axis.

`FakeFreecadProcess` is also the shared test double of the freecadcmd child
for the tests that used to replace `subprocess.run` (CONTRACT CHANGED
2026-10-27): they now replace `subprocess.Popen`, the seam `run_logged`
starts the child through.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from kicadgen.cad3d import process_log, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide
from kicadgen.generators.pin_header import PinHeaderParams

ROOT = Path(__file__).resolve().parents[1]


# ── Shared double of the freecadcmd child ───────────────────────────────────

class FakeFreecadProcess:
    """@brief Stand-in for the `subprocess.Popen` that `run_logged` opens.

    @param command The command line `run_logged` received.
    @param lines Output lines the fake child prints, in order.
    @param hang True to keep the child "alive" and silent after the lines,
        until it is killed: that is what the silence watchdog must detect.
    @param kwargs The Popen keyword arguments (env, encoding, ...), kept in
        `self.kwargs` for the assertions.
    @note `poll()` reports the child as exited, so the real
        `kill_process_tree` never enumerates the descendants of a fake pid;
        tests that check the tree kill replace `process_log.kill_process_tree`.
    """

    def __init__(self, command, lines=(), hang=False, **kwargs):
        """@brief Record the call and prepare the scripted output."""
        self.command = list(command)
        self.kwargs = kwargs
        self.pid = -1
        self.returncode = None
        self.killed = threading.Event()
        self._lines = list(lines)
        self._hang = hang
        self.stdout = self._output()

    def _output(self):
        """Yield the scripted lines, then stay silent until killed when hanging."""
        yield from self._lines
        if self._hang:
            # Bounded wait: a broken watchdog fails the test instead of hanging the suite.
            self.killed.wait(10)

    def poll(self):
        """@brief Report the child as exited, so no real descendant scan runs."""
        return 0

    def kill(self):
        """@brief End the fake child: its output stops at once."""
        self.killed.set()
        self.returncode = -9

    def wait(self, timeout=None):
        """@brief Return the exit code (0 when the child ended on its own)."""
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def __enter__(self):
        """@brief Context-manager protocol of Popen."""
        return self

    def __exit__(self, *exc):
        """@brief Context-manager protocol of Popen; nothing to release."""
        return False


def fake_freecad_writing_the_step(monkeypatch, seen):
    """@brief Replace the freecadcmd child with one that reads its JSON and "writes" the STEP.

    @param monkeypatch The pytest fixture.
    @param seen List receiving every payload the child was given.
    @note CONTRACT CHANGED 2026-10-27: the double used to replace
        `subprocess.run`; every `_run_freecad` call now goes through
        `run_logged`, which starts the child with `subprocess.Popen`.
    """
    def popen(command, **kwargs):
        payload = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        seen.append(payload)
        Path(payload["step_path"]).write_text("ISO-10303-21;", encoding="utf-8")
        return FakeFreecadProcess(command, ["KICADGEN_STEP_OK\n"], **kwargs)

    monkeypatch.setattr(process_log.subprocess, "Popen", popen)


# ── S3: the parameter validators use the shared name rule ───────────────────

def _castellated(name):
    return CastellatedModuleParams(name=name, body_length=20.0, body_width=10.0,
                                   sides=[CastellatedSide(edge="left", count=3, pitch=2.54)])


def _package(name):
    return PackageGeometry(name=name, family="custom", body_length_mm=8.0, body_width_mm=2.0,
                           body_height_mm=1.0, pins=[PinSpec("1", -3.0, 0.0, 1.0, 0.6),
                                                     PinSpec("2", 3.0, 0.0, 1.0, 0.6)])


BUILDERS = {"castellated": _castellated, "pin_header": lambda name: PinHeaderParams(name=name),
            "package": _package}


@pytest.mark.parametrize("kind", sorted(BUILDERS))
@pytest.mark.parametrize("name", ["..\\evil", "../evil", "a:b", "name."])
def test_s3_every_validator_refuses_a_path_as_name(kind, name):
    """@brief Negative: a name that would leave the output folder is refused."""
    with pytest.raises(ValueError, match="nome di file valido"):
        BUILDERS[kind](name).validate()


@pytest.mark.parametrize("kind", sorted(BUILDERS))
def test_s3_every_validator_accepts_a_plain_name(kind):
    """@brief Positive: an ordinary component name passes the same check."""
    BUILDERS[kind]("ESP32-S3").validate()


def test_s3_the_rule_lives_in_core_and_cad3d_re_exports_it():
    """@brief One rule: the 3D layer's name is the core function, not a copy."""
    from kicadgen.cad3d import output_names as cad_names
    from kicadgen.core import output_names as core_names
    assert cad_names.validate_output_name is core_names.validate_output_name
    assert step_generator.validate_output_name is core_names.validate_output_name


# ── S4: every _run_freecad call goes through run_logged ─────────────────────

def test_s4_without_progress_and_cancel_the_silence_watchdog_kills_the_tree(monkeypatch, tmp_path):
    """@brief Positive: a child that falls silent is stopped after the SILENCE
    timeout, through the tree kill, and its tail reaches the error."""
    started, killed = [], []

    def popen(command, **kwargs):
        child = FakeFreecadProcess(command, ["riga 1\n", "ultima riga\n"], hang=True, **kwargs)
        started.append(child)
        return child

    def kill_tree(process, *args, **kwargs):
        killed.append(process)
        process.kill()

    monkeypatch.setattr(process_log.subprocess, "Popen", popen)
    monkeypatch.setattr(process_log, "kill_process_tree", kill_tree)
    monkeypatch.setattr(step_generator, "FREECAD_SILENCE_TIMEOUT_S", 0.3)
    with pytest.raises(step_generator.Cad3DError, match="ultima riga"):
        step_generator._run_freecad("T", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
                                    freecadcmd_path="freecadcmd")
    assert killed == started and len(started) == 1
    assert started[0].command[0] == "freecadcmd"
    assert started[0].kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "nessun output per 0.3 s" in (tmp_path / "freecad.log").read_text(encoding="utf-8")


def test_s4_a_child_that_keeps_talking_is_not_capped_by_overall_duration(monkeypatch, tmp_path):
    """@brief Negative: lines spaced below the silence timeout, for longer
    than the timeout overall, are never killed (no overall-duration cap)."""
    killed = []

    class Talking(FakeFreecadProcess):
        """A child that beats every 0.1 s for 0.6 s, then reports success."""

        def _output(self):
            for index in range(6):
                self.killed.wait(0.1)
                yield f"battito {index}\n"
            yield "KICADGEN_STEP_OK\n"

    def popen(command, **kwargs):
        payload = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        Path(payload["step_path"]).write_text("ISO-10303-21;", encoding="utf-8")
        return Talking(command, **kwargs)

    monkeypatch.setattr(process_log.subprocess, "Popen", popen)
    monkeypatch.setattr(process_log, "kill_process_tree", lambda process, *a, **k: killed.append(process))
    monkeypatch.setattr(step_generator, "FREECAD_SILENCE_TIMEOUT_S", 0.35)
    result = step_generator._run_freecad("T", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
                                         freecadcmd_path="freecadcmd")
    assert killed == []
    assert Path(result["step_path"]).is_file()


def test_s4_the_subprocess_run_branch_is_gone():
    """@brief Structural: `_run_freecad` no longer launches freecadcmd with subprocess.run."""
    import inspect
    source = inspect.getsource(step_generator._run_freecad)
    assert "subprocess.run(" not in source
    assert "run_logged(" in source


# ── S5: the evaluation tool reads the crop through the shared reader ────────

def _tool():
    spec = importlib.util.spec_from_file_location("evaluate_photo_inventory_s5",
                                                  ROOT / "tools" / "evaluate_photo_inventory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_s5_an_original_crop_is_used():
    """@brief Positive: the original layout gives its box back unchanged."""
    assert _tool().inventory_crop({"crop_bbox_original_px": [1, 2, 30, 40]}) == [1, 2, 30, 40]
    assert _tool().inventory_crop({}) is None


def test_s5_a_deskewed_crop_is_refused_with_an_explicit_message():
    """@brief Negative: a box of the rotated canvas is never applied to the original photo."""
    metadata = {"crop_bbox_deskewed_px": [1, 2, 30, 40],
                "deskew": {"angle_deg": 3.5, "canvas_width": 500, "canvas_height": 400}}
    with pytest.raises(ValueError, match="deskewed canvas.*3.5"):
        _tool().inventory_crop(metadata)


@pytest.mark.parametrize("layout,ok", [("original", True), ("deskewed", False)])
def test_s5_cli_with_both_layouts(tmp_path, layout, ok):
    """@brief End to end, without site-packages: original evaluated, deskewed
    refused even when an explicit --crop is given."""
    inventory = tmp_path / "inventory.json"
    data = ({"components": [], "crop_bbox_original_px": [315, 117, 1435, 519]} if layout == "original"
            else {"components": [], "crop_bbox_deskewed_px": [315, 117, 1435, 519],
                  "deskew": {"angle_deg": -2.0, "canvas_width": 1700, "canvas_height": 700}})
    inventory.write_text(json.dumps(data), encoding="utf-8")
    command = [sys.executable, "-S", str(ROOT / "tools" / "evaluate_photo_inventory.py"), str(inventory)]
    for extra in ([], ["--crop", "315", "117", "1435", "519"]):
        result = subprocess.run(command + extra, capture_output=True, text=True)
        if ok:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "deskewed canvas" in result.stderr


# ── S6: a pin name with a space is reported once on the status line ─────────

def _rows(name):
    return [{"pin": "1", "name": name, "electrical_type": "passive"},
            {"pin": "2", "name": "GND", "electrical_type": "power_in"}]


def test_s6_a_pin_name_with_a_space_is_reported_once(qapp):
    """@brief Positive: "A B" is written "A_B" and the status line says so, once."""
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    try:
        tab.set_data("ESP32-S3", _rows("A B"))
        text = tab.status.text()
        assert text.count("«A B»") == 1 and "«A_B»" in text
        tab._changed()
        assert tab.status.text().count("«A B»") == 1
    finally:
        tab.deleteLater()


def test_s6_a_plain_pin_name_adds_no_note(qapp):
    """@brief Negative: names KiCad keeps as they are produce no note."""
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    try:
        tab.set_data("ESP32-S3", _rows("A_B"))
        assert "«" not in tab.status.text()
    finally:
        tab.deleteLater()


# ── S7: the resolution warning names the coarse axis ────────────────────────

def test_s7_the_warning_names_the_coarse_axis():
    """@brief Positive for each axis; negative: a fine photo gives no warning."""
    from kicadgen.services.photo_inventory import _warn_if_too_coarse
    for crop_px, crop_py, axis in ((100, 4000, "X"), (4000, 100, "Y")):
        warnings = []
        assert _warn_if_too_coarse(crop_px, 20.0, warnings.append, 0.5,
                                   crop_py=crop_py, body_width=20.0) == axis
        assert len(warnings) == 1 and f"asse {axis}" in warnings[0]
    warnings = []
    assert _warn_if_too_coarse(4000, 20.0, warnings.append, 0.5, crop_py=4000, body_width=20.0) is None
    assert warnings == []


# -- Coordinator: a saved package name that is not a file name (review 2026-10-27, S3 follow-up)

def _saved_package(name):
    """A minimal valid package mapping as a project file stores it."""
    return {"name": name, "pins": [
        {"number": "1", "x_mm": -1.0, "y_mm": 0.0, "pad_length_mm": 0.8, "pad_width_mm": 0.8,
         "shape": "rect"},
        {"number": "2", "x_mm": 1.0, "y_mm": 0.0, "pad_length_mm": 0.8, "pad_width_mm": 0.8,
         "shape": "rect"}]}


def test_an_old_project_with_an_ai_package_name_still_loads():
    """Positive: the AI's raw name «LM358/LM2904» no longer blocks the project.

    The saved name was never authoritative (the folder replaces it after
    loading), so it is swapped for the placeholder before validation and the
    caller's mapping stays untouched.
    """
    from kicadgen.gui.project_state import SAVED_PACKAGE_PLACEHOLDER_NAME, saved_package_geometry
    saved = _saved_package("LM358/LM2904")
    geometry = saved_package_geometry(saved)
    assert geometry is not None and geometry.name == SAVED_PACKAGE_PLACEHOLDER_NAME
    assert saved["name"] == "LM358/LM2904", "the saved mapping is never modified"


def test_a_valid_saved_package_name_is_kept():
    """Negative: a name that is already a valid file name passes unchanged."""
    from kicadgen.gui.project_state import saved_package_geometry
    assert saved_package_geometry(_saved_package("ESP32-S3")).name == "ESP32-S3"


# -- Coordinator: an extracted package is not discarded for its label (review 2026-10-27, G7/S3)

def test_an_ai_package_name_and_a_legacy_trapezoid_do_not_discard_the_extraction():
    """Positive: «LM358/LM2904» and a cached «trapezoid» pad no longer reject a paid answer.

    The name becomes the placeholder (the window replaces it with the
    project folder), the trapezoid becomes the rectangle KiCad drew, with
    one note naming the pad; the caller's mapping is untouched.
    """
    from kicadgen.services import datasheet_extractor as ext
    from kicadgen.core.package_geometry import package_from_mapping
    raw = _saved_package("LM358/LM2904")
    raw["pins"][0] = {**raw["pins"][0], "shape": "trapezoid"}
    notes = []
    mapping = ext._package_mapping_for_validation(raw, notes)
    geometry = package_from_mapping(mapping)
    assert geometry.name == ext.EXTRACTED_PACKAGE_PLACEHOLDER_NAME
    assert [pin.shape for pin in geometry.pins][0] == "rect"
    assert len(notes) == 1 and "1" in notes[0]
    assert raw["name"] == "LM358/LM2904" and raw["pins"][0]["shape"] == "trapezoid"


def test_a_clean_extracted_package_passes_unchanged():
    """Negative: a valid name and supported shapes produce no note and keep the name."""
    from kicadgen.services import datasheet_extractor as ext
    notes = []
    mapping = ext._package_mapping_for_validation(_saved_package("SOIC-8"), notes)
    assert mapping["name"] == "SOIC-8" and notes == []
