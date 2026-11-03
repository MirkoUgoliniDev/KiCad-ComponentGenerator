"""@brief Second 2026-11-03 review of the 3D package: liveness, bounded reads, edge cases.

C1: a FreeCAD parse holds the GIL, so the heartbeat threads stay silent; the
watchdogs now also count the CPU time of the child's Windows job. C2-C7, G6,
G9, O3: see each section. No FreeCAD: plain Python children stand in for it,
and the job accounting is faked where a test must not depend on the machine.
"""
import os
import subprocess
import sys
import threading
import time

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import process_log, step_generator
from kicadgen.services.messages import set_translator
from tests.test_run_logged_orphans import _alive, _wait_pid


def _busy_job(monkeypatch):
    """Fake job accounting: the job burns one full core all the time."""
    monkeypatch.setattr(process_log, "job_cpu_seconds", lambda process: time.monotonic())


def _idle_job(monkeypatch):
    """Fake job accounting: the job exists but burns nothing."""
    monkeypatch.setattr(process_log, "job_cpu_seconds", lambda process: 5.0)


def _kill_pid(pid_file):
    """End a test grandchild by the pid it wrote, whatever the platform."""
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return
    if sys.platform == "win32":
        process_log._windows_terminate(pid)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# ── C1: CPU activity as liveness ──────────────────────────────────────────

#: Child that prints nothing for 2.5 s, then its marker: a parse holding the GIL.
_SILENT_WORK = "import time; time.sleep(2.5); print('KICADGEN_STEP_OK', flush=True)\n"


def test_cpu_activity_tells_a_working_job_from_an_idle_one(monkeypatch):
    """@brief Positive and negative on the same sampler: CPU grows -> busy; flat -> not."""
    clock = [100.0]
    cpu = [10.0]
    monkeypatch.setattr(process_log, "job_cpu_seconds", lambda process: cpu[0])
    activity = process_log.CpuActivity(object(), timeout=5, clock=lambda: clock[0])
    assert activity.available
    clock[0] += 1.0
    cpu[0] += 0.9          # 90 % of a core
    assert activity.busy() is True
    clock[0] += 1.0
    cpu[0] += 0.015        # one scheduler tick: an idle process
    assert activity.busy() is False
    clock[0] += 0.1        # between two samples: never a verdict
    cpu[0] += 5.0
    assert activity.busy() is False


def test_without_a_job_there_is_no_cpu_evidence():
    """@brief Negative: no job (another platform, a double) -> output-only liveness."""
    activity = process_log.CpuActivity(object(), timeout=5)
    assert activity.available is False
    assert activity.busy(time.monotonic() + 100) is False


def test_a_silent_child_burning_cpu_is_not_killed(monkeypatch, tmp_path):
    """@brief Positive: no output for longer than the timeout, but the job works -> completes."""
    _busy_job(monkeypatch)
    result = process_log.run_logged([sys.executable, "-c", _SILENT_WORK],
                                    tmp_path / "freecad.log", timeout=1)
    assert result.returncode == 0 and "KICADGEN_STEP_OK" in result.stdout


def test_a_silent_idle_child_is_still_killed(monkeypatch, tmp_path):
    """@brief Negative: same child, a job that burns nothing -> the watchdog fires."""
    _idle_job(monkeypatch)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        process_log.run_logged([sys.executable, "-c", _SILENT_WORK],
                               tmp_path / "freecad.log", timeout=1)
    assert time.monotonic() - started < 2.4


def test_cpu_alone_keeps_a_child_alive_only_up_to_the_declared_bound(monkeypatch, tmp_path):
    """@brief Negative: an endless silent loop is still ended, after cpu_silence_limit."""
    _busy_job(monkeypatch)
    monkeypatch.setattr(process_log, "CPU_ONLY_SILENCE_FACTOR", 3)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        process_log.run_logged([sys.executable, "-c", "import time; time.sleep(30)"],
                               tmp_path / "freecad.log", timeout=0.5)
    elapsed = time.monotonic() - started
    # Not before the bound (3 x 0.5 s), well before the child's own end.
    assert 1.4 <= elapsed < 10, elapsed


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object accounting exists only on Windows")
def test_real_job_accounting_keeps_a_silent_busy_child_alive(tmp_path):
    """@brief Positive, no fake: a real silent busy loop is not mistaken for a hang."""
    busy = ("import time\nend = time.monotonic() + 3\nwhile time.monotonic() < end: pass\n"
            "print('KICADGEN_STEP_OK', flush=True)\n")
    result = process_log.run_logged([sys.executable, "-c", busy], tmp_path / "freecad.log",
                                    timeout=1)
    assert "KICADGEN_STEP_OK" in result.stdout


class _FakeMeasurer:
    """A measuring child double: silent, alive until `lifetime` elapses or it is killed."""

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


def _watch(monkeypatch, tmp_path, lifetime, timeout):
    monkeypatch.setattr(km, "kill_process_tree", lambda process: process.kill())
    (tmp_path / "out.json").write_text("{}", encoding="utf-8")
    started = time.monotonic()
    outcome, _, _ = km._watch_measuring_child(_FakeMeasurer(lifetime), tmp_path, ["fc"],
                                              timeout, None)
    return outcome, time.monotonic() - started


def test_a_measure_silent_but_busy_is_not_declared_stalled(monkeypatch, tmp_path):
    """@brief Positive: Part.read prints nothing for 2 s (timeout 0.5 s) but burns CPU -> done."""
    _busy_job(monkeypatch)
    assert _watch(monkeypatch, tmp_path, lifetime=2.0, timeout=0.5)[0] == "done"


def test_a_measure_silent_and_idle_is_still_stalled(monkeypatch, tmp_path):
    """@brief Negative: same silence, no CPU -> stalled at the timeout."""
    _idle_job(monkeypatch)
    outcome, elapsed = _watch(monkeypatch, tmp_path, lifetime=30.0, timeout=0.5)
    assert outcome == "stalled" and elapsed < 3


def test_a_measure_busy_forever_is_stalled_at_the_bound(monkeypatch, tmp_path):
    """@brief Negative: an endless OCCT loop still ends, after cpu_silence_limit(timeout)."""
    _busy_job(monkeypatch)
    monkeypatch.setattr(process_log, "CPU_ONLY_SILENCE_FACTOR", 3)
    outcome, elapsed = _watch(monkeypatch, tmp_path, lifetime=30.0, timeout=0.4)
    assert outcome == "stalled" and 1.1 <= elapsed < 10, elapsed


# ── C4: a descendant printing after the child exited ─────────────────────

#: Child that leaves a grandchild printing every 0.3 s on the shared pipe, then exits.
_LEAVES_A_CHATTY_GRANDCHILD = r"""
import subprocess, sys
subprocess.Popen([sys.executable, "-c",
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "for _ in range(200):\n"
    "    print('tick', flush=True); time.sleep(0.3)\n", sys.argv[1]],
    stdout=sys.stdout, stderr=sys.stdout)
print("KICADGEN_STEP_OK", flush=True)
"""


@pytest.mark.parametrize("contained", [True, False])
def test_a_chatty_descendant_no_longer_keeps_the_run_alive(monkeypatch, tmp_path, contained):
    """@brief Positive: the run ends within the drain grace of the child's exit, job or not."""
    if not contained:
        # Another platform: no job, and the kill reaches only the child.
        monkeypatch.setattr(process_log, "contain_process", lambda process: None)
        monkeypatch.setattr(process_log, "kill_process_tree", lambda process: process.kill())
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    try:
        result = process_log.run_logged(
            [sys.executable, "-c", _LEAVES_A_CHATTY_GRANDCHILD, str(pid_file)],
            tmp_path / "freecad.log", timeout=8)
        elapsed = time.monotonic() - started
    finally:
        _wait_pid(pid_file)
        _kill_pid(pid_file)
    assert result.returncode == 0 and "KICADGEN_STEP_OK" in result.stdout
    assert elapsed < 15, f"run_logged followed the descendant for {elapsed:.1f} s"
    assert "timeout=False" in (tmp_path / "freecad.log").read_text(encoding="utf-8")


# ── C5: pipe closed while the child is still alive ───────────────────────

#: The base interpreter, not the venv launcher: the launcher would keep its
#: own copy of the pipe open, and the EOF this section is about never came.
_PYTHON = getattr(sys, "_base_executable", None) or sys.executable

#: Child that closes its output (stdout and stderr share the pipe) and lives on.
_CLOSES_ITS_PIPE = ("import os, sys, time\nsys.stdout.flush()\nos.close(1)\nos.close(2)\n"
                    "time.sleep({sleep})\nsys.exit({code})\n")


def test_cancel_still_works_after_the_child_closed_its_pipe(tmp_path):
    """@brief Positive: EOF with the child alive no longer falls into an unbounded wait()."""
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(process_log.ProcessCancelled):
        process_log.run_logged([_PYTHON, "-c", _CLOSES_ITS_PIPE.format(sleep=30, code=0)],
                               tmp_path / "freecad.log", timeout=60, cancel=cancel)
    assert time.monotonic() - started < 10


def test_the_watchdog_still_works_after_the_child_closed_its_pipe(tmp_path):
    """@brief Positive: a mute child alive after EOF is still a timeout."""
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        process_log.run_logged([_PYTHON, "-c", _CLOSES_ITS_PIPE.format(sleep=30, code=0)],
                               tmp_path / "freecad.log", timeout=1)
    assert time.monotonic() - started < 10


def test_a_child_that_closed_its_pipe_and_exits_returns_its_code(tmp_path):
    """@brief Negative: the loop after EOF still ends with the child's own exit code."""
    result = process_log.run_logged(
        [_PYTHON, "-c", _CLOSES_ITS_PIPE.format(sleep=0.5, code=3)],
        tmp_path / "freecad.log", timeout=10)
    assert result.returncode == 3


# ── C3: the preview child never waits for a descendant without a bound ──

_PREVIEW_TREE = r"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
    "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
    sys.argv[1]], stdout=sys.stdout)
print("KICADGEN_OK", flush=True)
if sys.argv[2] != "exit":
    time.sleep(60)
"""


def _no_job(monkeypatch):
    """Simulate a platform without a job: the kill reaches only the direct child."""
    monkeypatch.setattr(process_log, "contain_process", lambda process: None)
    monkeypatch.setattr(process_log, "kill_process_tree", lambda process: process.kill())


def test_preview_without_a_job_returns_despite_a_surviving_grandchild(monkeypatch, tmp_path):
    """@brief Positive: exited child + grandchild holding stdout -> its output, promptly."""
    from kicadgen.gui import model_candidates_dialog as mcd
    _no_job(monkeypatch)
    monkeypatch.setattr(mcd, "PREVIEW_TIMEOUT_S", 30)
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    try:
        done = mcd._run_preview_child([sys.executable, "-c", _PREVIEW_TREE, str(pid_file), "exit"])
        elapsed = time.monotonic() - started
    finally:
        _wait_pid(pid_file)
        _kill_pid(pid_file)
    assert "KICADGEN_OK" in done.stdout
    assert elapsed < 20, elapsed


def test_preview_timeout_without_a_job_is_bounded(monkeypatch, tmp_path):
    """@brief Positive: the whole-run limit fires and returns although a grandchild lives on."""
    from kicadgen.gui import model_candidates_dialog as mcd
    _no_job(monkeypatch)
    monkeypatch.setattr(mcd, "PREVIEW_TIMEOUT_S", 1.5)
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            mcd._run_preview_child([sys.executable, "-c", _PREVIEW_TREE, str(pid_file), "stay"])
        elapsed = time.monotonic() - started
        if _wait_pid(pid_file):
            assert _alive(int(pid_file.read_text())), "the double must leave the grandchild alive"
    finally:
        _kill_pid(pid_file)
    assert elapsed < 15, elapsed


def test_preview_output_is_decoded_as_utf8(tmp_path):
    """@brief Positive: a non-ASCII result line reaches the caller intact, newlines normalised."""
    from kicadgen.gui import model_candidates_dialog as mcd
    script = "import sys; sys.stdout.buffer.write('gradi °\\r\\nok\\r\\n'.encode('utf-8'))"
    done = mcd._run_preview_child([sys.executable, "-c", script])
    assert done.stdout == "gradi °\nok\n"


# ── C2: a rotation a hair below zero ─────────────────────────────────────

def test_rectangle_axes_of_a_rotation_just_below_zero():
    """@brief Positive: 90 - 90.00000000000001 (-1.4e-14) gives the identity, not IndexError."""
    assert step_generator._rectangle_axes(90 - 90.00000000000001) == ((1.0, 0.0), (-0.0, 1.0))
    # A slightly larger negative angle is not 360.0 after the modulo: the
    # general cos/sin path, within rounding of the identity.
    (c, s), _ = step_generator._rectangle_axes(-1e-13)
    assert c == pytest.approx(1.0) and s == pytest.approx(0.0, abs=1e-12)


def test_rectangle_axes_cardinal_turns_unchanged():
    """@brief Negative: the four exact turns keep their exact axes."""
    assert step_generator._rectangle_axes(90) == ((0.0, 1.0), (-1.0, 0.0))
    assert step_generator._rectangle_axes(180) == ((-1.0, 0.0), (-0.0, -1.0))
    assert step_generator._rectangle_axes(-90) == ((0.0, -1.0), (1.0, 0.0))


# ── C6: the interruption is a type, translated when raised ───────────────

def test_the_interruption_is_a_cad3d_error_recognised_by_type(tmp_path):
    """@brief Positive: Cad3DCancelled is a Cad3DError; a real fault is not a Cad3DCancelled."""
    cancelled = step_generator.Cad3DCancelled()
    assert isinstance(cancelled, step_generator.Cad3DError)
    assert str(cancelled) == step_generator.CAD_ABORT_MESSAGE
    assert not isinstance(step_generator.Cad3DError("STEP fallito"), step_generator.Cad3DCancelled)


def test_the_interruption_text_follows_the_language_at_raise_time():
    """@brief Positive: with a translator set after import, the message is translated."""
    set_translator(lambda key, default=None: "Stopped" if key.endswith("dell'utente.") else default)
    try:
        assert str(step_generator.Cad3DCancelled()) == "Stopped"
    finally:
        set_translator(None)


def test_a_cancel_before_the_child_starts_raises_cad3d_cancelled(monkeypatch, tmp_path):
    """@brief Positive: the cancel path of `_run_freecad` raises the subclass."""
    cancel = threading.Event()
    cancel.set()
    body = step_generator.Body3DParams(length_mm=5, width_mm=5, height_mm=1)
    with pytest.raises(step_generator.Cad3DCancelled):
        step_generator._run_freecad("X", [], body, tmp_path, freecadcmd_path=sys.executable,
                                    cancel=cancel)


# ── C7: one definition each ───────────────────────────────────────────────

def test_no_duplicate_child_environment_or_reaper_is_left():
    """@brief Structural: the launchers share process_log's helpers."""
    assert not hasattr(step_generator, "child_environment")
    assert not hasattr(km, "_reap_killed_child")
    assert km.reap_killed_child is process_log.reap_killed_child


# ── G6: the quoted lead diameter reaches the solid ───────────────────────

def test_a_quoted_lead_diameter_that_fits_is_used():
    """@brief Positive: a 1.3 mm power-relay lead in 1.6 mm holes is drawn at 1.3 mm."""
    assert step_generator.single_part_lead_diameter(1.3, [1.6, 1.6]) == (1.3, None)


def test_without_a_quoted_diameter_the_estimate_is_kept():
    """@brief Negative: no quote -> the declared fallback from the smallest hole, unchanged."""
    diameter, note = step_generator.single_part_lead_diameter(None, [1.0, 3.2])
    assert diameter == pytest.approx(0.65) and note is None
    assert step_generator.single_part_lead_diameter(None, [1.6])[0] == 0.8


def test_a_quoted_diameter_wider_than_a_hole_is_refused_with_a_note():
    """@brief Negative: a lead that cannot pass through the smallest hole is not drawn."""
    diameter, note = step_generator.single_part_lead_diameter(1.2, [1.0, 1.6])
    assert diameter == pytest.approx(0.65)
    assert note and "1.2" in note and "1" in note
    for bad in (0, -1, float("nan"), True, "x"):
        assert step_generator.single_part_lead_diameter(bad, [1.0])[1] is None


def test_the_package_path_passes_the_quoted_diameter_to_freecad(monkeypatch, tmp_path):
    """@brief Positive: `body.lead_diameter_mm` sets the payload's pin diameter."""
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured["body"] = body
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    package = PackageGeometry(name="Relay", family="dip", body_length_mm=20,
                              body_width_mm=10, body_height_mm=15, pins=[
                                  PinSpec(str(i + 1), -5.0 + 10 * i, 0.0, 2.5, 2.5,
                                          pad_type="thru_hole", drill_mm=1.6)
                                  for i in range(2)])
    body = step_generator.Body3DParams(length_mm=20, width_mm=10, height_mm=15)
    step_generator.generate_single_package_3d_model(package, body, tmp_path)
    assert captured["body"].pin_diameter_mm == 0.8
    body.lead_diameter_mm = 1.3
    step_generator.generate_single_package_3d_model(package, body, tmp_path)
    assert captured["body"].pin_diameter_mm == 1.3


# ── G9: the gold pad is never longer than the land inside the board ──────

def test_the_3d_pad_is_never_longer_than_the_footprint_land_inside_the_board():
    """@brief Positive: a 1.0 mm land overhanging 0.7 mm gives a 0.3 mm gold pad, not 0.6."""
    from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide
    params = CastellatedModuleParams(
        name="Short", body_length=20.0, body_width=15.0, pcb_thickness=1.0,
        pad_length=1.0, pad_overhang=0.7,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1)])
    params.validate()
    _, contacts, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    assert contacts and all(c["pad_length_mm"] == pytest.approx(0.3) for c in contacts)


def test_a_long_land_keeps_its_inner_length():
    """@brief Negative: a land well inside the board is unchanged (length - overhang)."""
    from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide
    params = CastellatedModuleParams(
        name="Long", body_length=20.0, body_width=15.0, pcb_thickness=1.0,
        pad_length=2.4, pad_overhang=0.4,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1)])
    params.validate()
    _, contacts, _, _ = step_generator.castellated_contact_payload(params, "#d4af37")
    assert all(c["pad_length_mm"] == pytest.approx(2.0) for c in contacts)


# ── O3: the STEP model choice dialog follows the language ────────────────

def test_the_model_choice_dialog_is_built_in_the_interface_language(app, tmp_path, monkeypatch):
    """@brief Positive: in English the title, labels and check boxes are English."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QCheckBox, QLabel
    from kicadgen.gui import model_candidates_dialog as gui
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    monkeypatch.setattr(gui, "_thumbnail", lambda *args, **kwargs: None)
    i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    set_translator(i18n.raw)
    try:
        dialog = gui.ModelCandidatesDialog(dict(name="U1", length_mm=3, width_mm=2, height_mm=1),
                                           dict(candidates=[]))
        try:
            texts = {w.text() for w in dialog.findChildren(QLabel)}
            boxes = {w.text() for w in dialog.findChildren(QCheckBox)}
            assert dialog.windowTitle() == "STEP model choice"
            assert "Local Z rotation of the native CAD" in texts
            assert "Colours" in boxes
            assert "Automatic: infer the CAD axes from the outline" in boxes
            assert not {"Colori", "Rotazione Z locale del CAD nativo"} & (texts | boxes)
        finally:
            dialog.reject()
    finally:
        set_translator(None)
    italian = gui.ModelCandidatesDialog(dict(name="U1", length_mm=3, width_mm=2, height_mm=1),
                                        dict(candidates=[]))
    try:
        # Negative: without a translator the Italian source stays.
        assert italian.windowTitle() == "Scelta del modello STEP"
    finally:
        italian.reject()
