"""@brief Super review 2026-11-16, fixer C9: other GUI files.

Unreadable working folder in the project dialogs; R4-2 alphanumeric pin
labels in the 3D preview; R4-3 YOLO settings returned exactly when
untouched; R4-4 one document job at a time with a balanced busy cursor;
R4-5 bounded exit waits of the background threads; R4-6 model checks kept
for the session when the window closes first; the YOLO pretest's
cancellation channel. Each rule has a positive and a negative case.
"""
import json
import os
from copy import deepcopy
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEventLoop, QThread  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog  # noqa: E402

from kicadgen.gui import project_dialogs as pd  # noqa: E402
from kicadgen.gui import quit_wait  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────


def _deny_listing(monkeypatch, root):
    """Make `root` (and only it) refuse to be listed, as a folder without read permission."""
    original = Path.iterdir
    denied = Path(root).resolve()

    def iterdir(self):
        if self.resolve() == denied:
            raise PermissionError(13, "Accesso negato", str(self))
        return original(self)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    return lambda: monkeypatch.setattr(Path, "iterdir", original)


def _project(root, name):
    folder = Path(root) / name
    (folder / "docs").mkdir(parents=True)
    return folder


# ── unreadable working folder ───────────────────────────────────────────


def test_an_unreadable_working_folder_keeps_the_open_dialog_usable(qapp, tmp_path, monkeypatch):
    """Positive: no exception, the reason is written, Open stays disabled."""
    _project(tmp_path, "Alpha")
    _deny_listing(monkeypatch, tmp_path)
    with pytest.raises(OSError):
        pd.list_projects(tmp_path)  # the listing says it, it does not pretend «empty»
    dialog = pd.OpenProjectDialog(tmp_path)
    try:
        assert dialog.read_error is not None
        assert dialog.projects == [] and dialog.list.count() == 0
        assert not dialog.empty.isHidden()
        text = dialog.empty.text()
        assert pd.UNREADABLE_ROOT in text and "Accesso negato" in text and str(tmp_path) in text
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled()
    finally:
        dialog.deleteLater()


def test_a_readable_working_folder_lists_its_projects(qapp, tmp_path):
    """Negative: a readable folder lists its project and reports nothing."""
    _project(tmp_path, "Alpha")
    dialog = pd.OpenProjectDialog(tmp_path)
    try:
        assert dialog.read_error is None
        assert [entry.name for entry in dialog.projects] == ["Alpha"]
        assert dialog.empty.isHidden()
    finally:
        dialog.deleteLater()


def test_a_missing_working_folder_is_empty_not_unreadable(qapp, tmp_path):
    """Negative: a folder that does not exist yet is no error (it is created later)."""
    missing = tmp_path / "not-yet"
    assert pd.list_projects(missing) == []
    assert pd.name_problem(missing, "Nuovo") == ""
    dialog = pd.OpenProjectDialog(missing)
    try:
        assert dialog.read_error is None
        assert pd.UNREADABLE_ROOT not in dialog.empty.text()
    finally:
        dialog.deleteLater()


def test_the_name_dialog_reports_an_unreadable_folder_at_once_and_refuses_ok(qapp, tmp_path,
                                                                            monkeypatch):
    """Positive: shown before typing, OK disabled, accept() does not close; fixed -> usable."""
    restore = _deny_listing(monkeypatch, tmp_path)
    dialog = pd.ProjectNameDialog(tmp_path, "Nuovo progetto")
    try:
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert pd.UNREADABLE_ROOT_NAME in dialog.problem.text()
        assert "Accesso negato" in dialog.problem.text()
        assert not ok.isEnabled()
        dialog.field.setText("Nuovo")
        assert not ok.isEnabled()
        dialog.accept()
        assert dialog.result() == 0 and dialog.problem.text()
        # The permission is fixed: the next key reads the folder again.
        restore()
        dialog.field.setText("Nuovo2")
        assert ok.isEnabled() and dialog.problem.text() == ""
    finally:
        dialog.deleteLater()


def test_name_problem_refuses_a_name_it_cannot_check(tmp_path, monkeypatch):
    """Positive and negative: unreadable -> refused with the reason; readable -> usable."""
    assert pd.name_problem(tmp_path, "Libero") == ""
    _deny_listing(monkeypatch, tmp_path)
    assert pd.name_problem(tmp_path, "Libero") == pd.UNREADABLE_ROOT_NAME


def test_one_unreadable_project_folder_does_not_hide_the_others(tmp_path, monkeypatch):
    """Negative of the root rule: a project folder that cannot be inspected is only left out."""
    _project(tmp_path, "Alpha")
    beta = _project(tmp_path, "Beta")
    _deny_listing(monkeypatch, beta)
    assert [entry.name for entry in pd.list_projects(tmp_path)] == ["Alpha"]


# ── R4-2: pin labels as text ────────────────────────────────────────────


def test_pin_numbers_of_preview_parts_stay_text():
    """Positive: A1 and EP get a label, 001 is not «1»; the declared field wins."""
    vtk = pytest.importorskip("kicadgen.gui.preview_3d_vtk")
    number = vtk._part_pin_number
    assert number({"name": "PinA1"}) == "A1"
    assert number({"name": "PinEP"}) == "EP"
    assert number({"name": "Pin001"}) == "001"
    assert number({"name": "Pin12"}) == "12"
    # FreeCAD renamed a duplicate «Pin1»: the declared number is the truth.
    assert number({"name": "Pin001", "pin": "1"}) == "1"
    assert number({"name": "PinA_1", "pin": "A-1"}) == "A-1"


def test_parts_that_are_no_contact_get_no_pin_label():
    """Negative: bodies, header boxes, colour pieces and empty suffixes are no contacts."""
    vtk = pytest.importorskip("kicadgen.gui.preview_3d_vtk")
    number = vtk._part_pin_number
    for name in ("Body", "HeaderPin1", "Pin", "Pin1_color3", "Silkscreen0", "FR4_substrate"):
        assert number({"name": name}) is None, name
    assert number({"name": "Body", "pin": True}) is None
    assert number({"name": "Body", "pin": "  "}) is None


def test_the_mesh_scene_anchors_alphanumeric_pins(tmp_path):
    """Positive end to end: the prepared scene carries one anchor per contact, as text."""
    vtk = pytest.importorskip("kicadgen.gui.preview_3d_vtk")
    pytest.importorskip("pyvista")
    triangle = {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "triangles": [[0, 1, 2]]}
    parts = [dict(triangle, name=name, color="#888888") for name in
             ("PinA1", "PinEP", "Pin7_color2", "Body")]
    path = tmp_path / "m.preview.json"
    path.write_text(json.dumps({"parts": parts}), encoding="utf-8")
    scene = vtk._prepare_mesh_scene(path)
    assert [anchor[0] for anchor in scene.anchors] == ["A1", "EP"]


# ── R4-3: YOLO settings returned exactly when untouched ─────────────────


def _settings_dialog(values):
    from kicadgen.gui.yolo_settings_dialog import YoloSettingsDialog
    return YoloSettingsDialog(values)


def test_ok_without_touching_returns_every_value_unchanged(qapp):
    """Positive: values the service accepts but the fields round or clamp come back exact."""
    from kicadgen.services.photo_yolo import yolo_settings
    incoming = yolo_settings({"confidence": 0.9999, "nms_iou": 0.3333, "tile_overlap": 0.1234,
                              "tile_sizes": [1280]})
    dialog = _settings_dialog(incoming)
    try:
        assert dialog.values() == incoming
    finally:
        dialog.deleteLater()


def test_a_touched_field_returns_the_new_value(qapp):
    """Negative: a field the user changed gives the shown value, the others stay exact."""
    from kicadgen.services.photo_yolo import yolo_settings
    incoming = yolo_settings({"confidence": 0.9999, "nms_iou": 0.3333})
    dialog = _settings_dialog(incoming)
    try:
        dialog.spin_confidence.setValue(0.4)
        values = dialog.values()
        assert values["confidence"] == 0.4
        assert values["nms_iou"] == 0.3333
    finally:
        dialog.deleteLater()


def test_the_fields_span_exactly_what_the_service_accepts(qapp):
    """Positive: both ends of every field are accepted; just outside, the service refuses."""
    from kicadgen.services.photo_yolo import yolo_settings
    dialog = _settings_dialog(None)
    try:
        for key, spin in dialog._spins():
            for end in (spin.minimum(), spin.maximum()):
                assert yolo_settings({key: end})[key] == end
            step = 10.0 ** -spin.decimals()
            for outside in (spin.minimum() - step, spin.maximum() + step):
                with pytest.raises(ValueError):
                    yolo_settings({key: outside})
        assert dialog.spin_confidence.maximum() > 0.95  # 0.97 was clamped before
    finally:
        dialog.deleteLater()


def test_restoring_the_defaults_returns_the_defaults(qapp):
    """Negative of «untouched»: after «Ripristina predefiniti» the defaults come back exactly."""
    from kicadgen.services.photo_yolo import YOLO_SETTING_DEFAULTS, yolo_settings
    dialog = _settings_dialog({"confidence": 0.9999})
    try:
        dialog.btn_defaults.click()
        assert dialog.values() == yolo_settings(YOLO_SETTING_DEFAULTS)
    finally:
        dialog.deleteLater()


# ── R4-4: guided documents ──────────────────────────────────────────────


@pytest.fixture
def panel(qapp, tmp_path):
    from PIL import Image
    from kicadgen.gui.guided_documents import GuidedDocuments
    from kicadgen.services.document_roles import DocumentSet
    docs = tmp_path / "docs"
    docs.mkdir()
    # Two different images: the store keeps one copy of identical content.
    for name, colour in (("a.png", "blue"), ("b.png", "red")):
        source = tmp_path / name
        Image.new("RGB", (40, 30), colour).save(source)
        DocumentSet(docs).import_file([], source)
    widget = GuidedDocuments(lambda: docs)
    widget.refresh()
    assert {"a.png", "b.png"} <= set(widget.cards)
    yield widget
    while QApplication.overrideCursor() is not None:
        QApplication.restoreOverrideCursor()
    widget.deleteLater()


def test_the_busy_cursor_is_restored_even_when_the_card_is_gone(panel):
    """Positive: a refresh that dropped the card in the middle no longer leaves the cursor on."""
    assert QApplication.overrideCursor() is None
    panel.set_busy("a.png", True)
    assert QApplication.overrideCursor() is not None
    panel.cards.pop("a.png")  # what a refresh in between does to a vanished document
    panel.set_busy("a.png", False)
    assert QApplication.overrideCursor() is None
    assert panel._busy_role is None


def test_a_second_job_while_one_runs_is_refused(panel, monkeypatch):
    """Positive: no nested busy state, no second file dialog; the first job stays the owner."""
    panel.set_busy("a.png", True)
    panel.set_busy("b.png", True)
    assert panel._busy_role == "a.png"
    asked = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: asked.append(a) or ("", ""))
    panel._replace("b.png")
    panel._add()
    assert asked == []
    panel.set_busy("b.png", False)  # not the owner: pops nobody's cursor
    assert QApplication.overrideCursor() is not None
    panel.set_busy("a.png", False)
    assert QApplication.overrideCursor() is None


def test_a_job_at_rest_still_opens_the_file_dialog(panel, monkeypatch):
    """Negative: with nothing running the replacement asks for the file as before."""
    asked = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: asked.append(a) or ("", ""))
    panel._replace("b.png")
    assert len(asked) == 1


def test_the_redraw_during_a_job_processes_no_user_input(panel, monkeypatch):
    """Positive: the forced redraw excludes user input (a click there re-entered the actions)."""
    flags = []
    monkeypatch.setattr(QApplication, "processEvents", staticmethod(lambda *a: flags.append(a)))
    panel.set_busy("a.png", True)
    panel.set_busy("a.png", False)
    assert flags and all(args and args[0] & QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
                         for args in flags)


# ── R4-5: bounded exit waits ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_exit_state(monkeypatch):
    """Every test starts before the exit and leaves no «quitting» state behind.

    `is_quitting()` is process-wide: a test that ran a hook must not make a
    later test (or the crash excepthook) believe the application is quitting.
    """
    monkeypatch.setattr(quit_wait, "_deadline", None)
    monkeypatch.setattr(quit_wait, "_quitting", False)


class _FakeClock:
    """A monotonic clock moved by hand, for the shared exit deadline."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _record_waits(monkeypatch):
    """Replace stop_and_wait with a recorder of the timeouts the hooks give it."""
    waits = []

    def record(thread, stop=None, timeout_ms=0):
        if stop is not None:
            stop()  # the stop request is part of the contract, the wait is recorded
        waits.append(timeout_ms)
        return True
    monkeypatch.setattr(quit_wait, "stop_and_wait", record)
    return waits


def test_n_threads_share_one_exit_deadline(qapp, monkeypatch):
    """Positive: the second hook waits only what the first left, never a second full budget."""
    clock = _FakeClock()
    monkeypatch.setattr(quit_wait, "_clock", clock)
    waits = _record_waits(monkeypatch)
    threads = [_Listening(), _Listening(), _Listening()]
    try:
        for thread in threads:
            quit_wait.connect_quit_wait(thread, timeout_ms=5000)
        hooks = [getattr(thread, quit_wait._HOOK) for thread in threads]
        hooks[0]()
        clock.now += 3.0  # the first thread used 3 s of the budget
        hooks[1]()
        clock.now += 4.0  # past the deadline
        hooks[2]()
        assert waits == [5000, 2000, 0]
    finally:
        for thread in threads:
            quit_wait.disconnect_quit_wait(thread)
            thread.deleteLater()


def test_the_first_hook_gets_the_whole_budget(qapp, monkeypatch):
    """Negative: before any exit hook ran there is no deadline, so the first wait is full."""
    clock = _FakeClock()
    monkeypatch.setattr(quit_wait, "_clock", clock)
    waits = _record_waits(monkeypatch)
    thread = _Listening()
    try:
        quit_wait.connect_quit_wait(thread, timeout_ms=5000)
        clock.now += 60.0  # time spent BEFORE the exit does not count
        getattr(thread, quit_wait._HOOK)()
        assert waits == [5000]
    finally:
        quit_wait.disconnect_quit_wait(thread)
        thread.deleteLater()


def test_is_quitting_turns_on_with_the_first_hook_only(qapp, monkeypatch):
    """Positive and negative: False while the program runs, True from the first exit hook on."""
    _record_waits(monkeypatch)
    thread = _Listening()
    try:
        quit_wait.connect_quit_wait(thread)
        assert quit_wait.is_quitting() is False  # connecting is not quitting
        quit_wait.disconnect_quit_wait(thread)
        assert quit_wait.is_quitting() is False
        quit_wait.connect_quit_wait(thread)
        getattr(thread, quit_wait._HOOK)()
        assert quit_wait.is_quitting() is True
    finally:
        quit_wait.disconnect_quit_wait(thread)
        thread.deleteLater()


def test_a_direct_stop_and_wait_also_marks_the_exit(qapp):
    """Positive: stop_and_wait may abandon a thread, so it marks the exit as begun."""
    thread = _Listening()
    try:
        assert quit_wait.is_quitting() is False
        assert quit_wait.stop_and_wait(thread, timeout_ms=0) is True  # never started
        assert quit_wait.is_quitting() is True
    finally:
        thread.deleteLater()


def _dialog_hook_workers():
    """The two dialog threads with an exit hook of their own: (worker, its budget in ms)."""
    from kicadgen.gui import component_check_dialog as ccd
    from kicadgen.gui import photo_review_dialog as prd
    return [(ccd._CheckWorker(dict(directory=".", name="X", rows=[])), ccd.CHECK_QUIT_WAIT_MS),
            (prd._MatchWorker({"kind": "ic"}), prd.MATCH_QUIT_WAIT_MS)]


def test_the_dialogs_own_exit_hooks_wait_only_what_is_left(qapp, monkeypatch):
    """Positive: after another hook used 4 s of the exit, each dialog hook waits only the rest."""
    clock = _FakeClock()
    monkeypatch.setattr(quit_wait, "_clock", clock)
    waits = _record_waits(monkeypatch)
    for worker, budget in _dialog_hook_workers():
        try:
            monkeypatch.setattr(quit_wait, "_deadline", None)
            quit_wait.begin_exit(5000)  # an earlier hook of the same exit
            clock.now += 4.0
            worker._stop_for_quit()
            assert waits[-1] == 1000, type(worker).__name__
            assert worker.cancel.is_set()  # still asked to stop first
        finally:
            worker._release()
            worker.deleteLater()


def test_the_first_dialog_hook_gets_its_whole_budget_and_marks_the_exit(qapp, monkeypatch):
    """Negative: with no exit begun the hook waits its full budget; afterwards the exit is marked."""
    waits = _record_waits(monkeypatch)
    for worker, budget in _dialog_hook_workers():
        try:
            monkeypatch.setattr(quit_wait, "_deadline", None)
            monkeypatch.setattr(quit_wait, "_quitting", False)
            assert quit_wait.is_quitting() is False
            worker._stop_for_quit()
            assert budget - 50 <= waits[-1] <= budget, type(worker).__name__
            assert quit_wait.is_quitting() is True
        finally:
            worker._release()
            worker.deleteLater()


def test_a_deaf_dialog_thread_past_the_deadline_is_let_go_at_once(qapp, monkeypatch):
    """Positive: the deadline is already spent, so the running thread is detached without waiting."""
    from kicadgen.gui import photo_review_dialog as prd
    from kicadgen.cad3d import kicad_models
    release, entered = threading.Event(), threading.Event()

    def deaf(*_args, **_kwargs):
        entered.set()
        release.wait(10)
        return {}
    monkeypatch.setattr(kicad_models, "model_candidates", deaf)
    worker = prd._MatchWorker({"kind": "ic"})
    worker.start()
    try:
        assert entered.wait(5)
        quit_wait.begin_exit(0)  # deadline now: nothing left to wait
        started = time.monotonic()
        worker._stop_for_quit()
        assert time.monotonic() - started < 1.0
        assert worker.isRunning() and worker.parent() is None
    finally:
        release.set()
        assert worker.wait(3000)


def test_the_candidate_preview_exit_hook_marks_the_exit(qapp):
    """Positive: quitting kills the preview child and marks the exit as begun."""
    from types import SimpleNamespace
    from kicadgen.gui.model_candidates_dialog import ModelCandidatesDialog
    stub = SimpleNamespace(_preview_cancel=threading.Event())
    assert quit_wait.is_quitting() is False
    ModelCandidatesDialog._stop_previews(stub)
    assert stub._preview_cancel.is_set() and quit_wait.is_quitting() is True


def test_closing_a_candidate_dialog_is_not_quitting(qapp):
    """Negative: closing the dialog kills its preview but does not mark the program as quitting."""
    from kicadgen.gui import model_candidates_dialog as mcd
    dialog = mcd.ModelCandidatesDialog({"name": "U1", "length_mm": 3, "width_mm": 3,
                                        "height_mm": 1}, {"candidates": []})
    try:
        dialog.reject()
        assert dialog._preview_cancel.is_set()
        assert quit_wait.is_quitting() is False
    finally:
        dialog.deleteLater()


class _Deaf(QThread):
    """A thread that ignores every stop until released."""

    def __init__(self):
        super().__init__(QApplication.instance())
        self.entered, self.release = threading.Event(), threading.Event()

    def run(self):
        self.entered.set()
        self.release.wait(10)


class _Listening(QThread):
    """A thread that ends as soon as an interruption is requested."""

    def __init__(self):
        super().__init__(QApplication.instance())
        self.entered = threading.Event()

    def run(self):
        self.entered.set()
        while not self.isInterruptionRequested():
            time.sleep(0.01)


def test_a_thread_deaf_to_the_stop_cannot_hold_the_exit(qapp):
    """Positive: the wait is bounded and the running thread leaves QApplication."""
    thread = _Deaf()
    thread.start()
    try:
        assert thread.entered.wait(5)
        started = time.monotonic()
        assert quit_wait.stop_and_wait(thread, timeout_ms=200) is False
        assert time.monotonic() - started < 2.0
        assert thread.isRunning() and thread.parent() is None
    finally:
        thread.release.set()
        assert thread.wait(3000)


def test_a_thread_that_hears_the_stop_ends_and_stays_owned(qapp):
    """Negative: a stoppable thread ends within the wait and is not let go."""
    thread = _Listening()
    thread.start()
    try:
        assert thread.entered.wait(5)
        assert quit_wait.stop_and_wait(thread, stop=thread.requestInterruption) is True
        assert thread.isFinished() and thread.parent() is qapp
    finally:
        thread.requestInterruption()
        thread.wait(3000)


def test_the_exit_hook_is_connected_and_dropped(qapp):
    """Positive and negative: connect stores a hook; disconnect removes it, twice is harmless."""
    thread = _Listening()
    quit_wait.connect_quit_wait(thread, stop=thread.requestInterruption)
    assert callable(getattr(thread, quit_wait._HOOK))
    quit_wait.disconnect_quit_wait(thread)
    assert getattr(thread, quit_wait._HOOK) is None
    quit_wait.disconnect_quit_wait(thread)
    thread.deleteLater()


def test_every_application_owned_worker_has_a_bounded_exit_hook(qapp):
    """Positive: the five workers that waited without a deadline now use quit_wait."""
    from kicadgen.gui import ai_account_dialog, model_dialog, pin_type_dialog, workers
    built = [workers.UpdateCheckWorker(),
             workers.PreviewRenderWorker(None, None, has_step=False),
             model_dialog._ModelsWorker(parent=qapp),
             ai_account_dialog._KeyCheckWorker("", lambda key: None, parent=qapp),
             pin_type_dialog._Worker({})]
    try:
        for worker in built:
            assert callable(getattr(worker, quit_wait._HOOK, None)), type(worker).__name__
    finally:
        for worker in built:
            quit_wait.disconnect_quit_wait(worker)
            worker.deleteLater()


def test_the_pin_type_worker_is_stopped_by_its_exit_hook(qapp, monkeypatch):
    """Positive: the hook asks the AI request to stop between calls, then waits for it."""
    from kicadgen.gui import pin_type_dialog
    entered = threading.Event()

    def slow(*, cancelled, **_):
        entered.set()
        for _ in range(1000):
            cancelled()
            time.sleep(0.01)
        return []
    monkeypatch.setattr(pin_type_dialog, "suggest_pin_types", slow)
    worker = pin_type_dialog._Worker({})
    errors, done = [], []
    worker.error.connect(errors.append)
    worker.done.connect(done.append)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        getattr(worker, quit_wait._HOOK)()
        assert time.monotonic() - started < 3.0
        assert worker.isFinished() and worker.parent() is qapp
        qapp.processEvents()
        assert errors == [] and done == []  # cancelled: nothing reported
    finally:
        worker.requestInterruption()
        worker.wait(3000)


def test_the_render_runner_is_killed_by_stop(qapp):
    """Positive: a hung kicad-cli stand-in is killed at once, not waited for its timeout."""
    from kicadgen.gui.workers import PreviewRenderWorker
    worker = PreviewRenderWorker(None, None, has_step=True)
    outcome = {}

    def call():
        started = time.monotonic()
        outcome["proc"] = worker._run([sys.executable, "-c", "import time; time.sleep(30)"],
                                      capture_output=True, text=True, timeout=60)
        outcome["seconds"] = time.monotonic() - started
    runner = threading.Thread(target=call)
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while worker._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker._process is not None
        worker.stop()
        runner.join(10)
        assert not runner.is_alive()
        assert outcome["seconds"] < 10 and outcome["proc"].returncode != 0
        # Stopped: no further render starts.
        with pytest.raises(subprocess.SubprocessError):
            worker._run([sys.executable, "-c", "pass"], capture_output=True)
    finally:
        quit_wait.disconnect_quit_wait(worker)
        worker.deleteLater()


def test_the_render_runner_behaves_like_subprocess_run(qapp):
    """Negative: without a stop the runner returns the output and exit code as subprocess.run."""
    from kicadgen.gui.workers import PreviewRenderWorker
    worker = PreviewRenderWorker(None, None, has_step=True)
    try:
        proc = worker._run([sys.executable, "-c", "print('ok'); raise SystemExit(3)"],
                           capture_output=True, text=True, timeout=30)
        assert proc.returncode == 3 and proc.stdout.strip() == "ok"
        with pytest.raises(subprocess.TimeoutExpired):
            worker._run([sys.executable, "-c", "import time; time.sleep(30)"],
                        capture_output=True, timeout=0.5)
        assert worker._process is None
    finally:
        quit_wait.disconnect_quit_wait(worker)
        worker.deleteLater()


def test_the_render_gets_the_runner_only_where_it_is_accepted(qapp, monkeypatch):
    """Positive and negative: runner passed to a function that takes it, never a TypeError."""
    from kicadgen.gui.workers import PreviewRenderWorker
    from kicadgen.services import kicad_library
    seen = []

    def with_runner(preview, kicad_cli, *, has_step, runner=None):
        seen.append(runner)
        return kicad_library.PreviewRender(None, "no_step")

    def without_runner(preview, kicad_cli, *, has_step):
        seen.append("old")
        return kicad_library.PreviewRender(None, "no_step")

    for render in (with_runner, without_runner):
        monkeypatch.setattr(kicad_library, "render_preview_image", render)
        worker = PreviewRenderWorker(None, None, has_step=True)
        results = []
        worker.done.connect(lambda result, token: results.append(result))
        try:
            worker.run()
            qapp.processEvents()
            assert results and results[0].reason == "no_step"
        finally:
            quit_wait.disconnect_quit_wait(worker)
            worker.deleteLater()
    assert seen[0] is not None and callable(seen[0]) and seen[1] == "old"


# ── YOLO pretest cancellation channel ───────────────────────────────────


def _pretest(tmp_path):
    from kicadgen.gui.workers import YoloPretestWorker
    worker = YoloPretestWorker([], None, 10.0, 10.0, None, tmp_path)
    done, errors = [], []
    worker.done.connect(done.append)
    worker.error.connect(errors.append)
    return worker, done, errors


def test_the_pretest_passes_its_cancel_event_where_it_is_accepted(qapp, tmp_path, monkeypatch):
    """Positive: extract_inventory receives the worker's own cancel_event."""
    from kicadgen.services import photo_inventory
    received = []

    def fake(*args, cancel=None, **kwargs):
        received.append(cancel)
        return "report"
    monkeypatch.setattr(photo_inventory, "extract_inventory", fake)
    worker, done, errors = _pretest(tmp_path)
    worker.run()
    qapp.processEvents()
    assert received == [worker.cancel_event] and done == ["report"] and errors == []
    worker.deleteLater()


def test_the_pretest_never_passes_cancel_to_a_function_without_it(qapp, tmp_path, monkeypatch):
    """Negative: an older signature gets no `cancel` and no TypeError."""
    from kicadgen.services import photo_inventory

    def old(doc_paths, prompt, length, width, kinds, call, *, progress=None, phase=None,
            top_photo_index=None, top_photo_source=None, cache_dir=None, detector=None,
            yolo_model_path=None, yolo_confidence=None, yolo_settings=None,
            review_proposals=None):
        return "report"
    monkeypatch.setattr(photo_inventory, "extract_inventory", old)
    worker, done, errors = _pretest(tmp_path)
    worker.run()
    qapp.processEvents()
    assert done == ["report"] and errors == []
    worker.deleteLater()


def test_a_stopped_pretest_reports_no_error(qapp, tmp_path, monkeypatch):
    """Positive: whatever the detector raises once stopped is a cancellation, not an error."""
    from kicadgen.services import photo_inventory

    def stopped(*args, cancel=None, **kwargs):
        cancel.set()
        raise RuntimeError("interrupted inside the detector")
    monkeypatch.setattr(photo_inventory, "extract_inventory", stopped)
    worker, done, errors = _pretest(tmp_path)
    worker.run()
    qapp.processEvents()
    assert done == [None] and errors == []
    worker.deleteLater()


def test_a_failing_pretest_that_was_not_stopped_reports_its_error(qapp, tmp_path, monkeypatch):
    """Negative: the same exception without a stop is still an error."""
    from kicadgen.services import photo_inventory

    def failing(*args, cancel=None, **kwargs):
        raise RuntimeError("weights unreadable")
    monkeypatch.setattr(photo_inventory, "extract_inventory", failing)
    worker, done, errors = _pretest(tmp_path)
    worker.run()
    qapp.processEvents()
    assert done == [] and errors == ["weights unreadable"]
    worker.deleteLater()


# ── R4-6: model checks kept when the window closed first ────────────────


def test_a_yolo_check_finished_after_the_window_closed_is_kept(tmp_path, monkeypatch):
    """Positive: the job keeps its result for the session itself, with no window listening."""
    from kicadgen.gui import yolo_model_dialog as dlg
    from kicadgen.services import photo_yolo
    monkeypatch.setattr(dlg, "_RESULTS", {})
    monkeypatch.setattr(photo_yolo, "validate_yolo_setup",
                        lambda path: {"model_classes": ["ic"], "missing_classes": [],
                                      "class_order": ""})
    weights = tmp_path / "big.pt"
    weights.write_bytes(b"pt")
    job = dlg._Job("check", str(weights))  # its `ready` signal reaches nobody
    job.run()
    assert dlg._remembered(weights, "check") == {"ok": True, "classes": ["ic"], "missing": [],
                                                 "order": ""}
    hashing = dlg._Job("sha256", str(weights))
    hashing.run()
    assert dlg._remembered(weights, "sha256")


def test_a_check_of_a_file_that_vanished_keeps_nothing(tmp_path, monkeypatch):
    """Negative: without an identity the job has nothing to answer for."""
    from kicadgen.gui import yolo_model_dialog as dlg
    monkeypatch.setattr(dlg, "_RESULTS", {})
    job = dlg._Job("check", str(tmp_path / "gone.pt"))
    job.run()
    assert dlg._RESULTS == {}


def test_an_smd_check_finished_after_the_window_closed_is_kept(tmp_path, monkeypatch):
    """Positive, SMD window: same rule through the shared helper."""
    from kicadgen.gui import smd_model_dialog as smd
    from kicadgen.gui import yolo_model_dialog as dlg
    from kicadgen.services import smd_component_classifier
    monkeypatch.setattr(dlg, "_RESULTS", {})
    monkeypatch.setattr(smd_component_classifier, "describe_smd_checkpoint",
                        lambda path: {"classes": ["R"]})
    weights = tmp_path / "smd.pt"
    weights.write_bytes(b"pt")
    smd._Job("smd_check", str(weights)).run()
    assert dlg._remembered(weights, "smd_check") == {"classes": ["R"], "ok": True}


# ── F4-1 / F4-2: photo review — a click is no edit; ask before discarding ──

from PyQt6.QtCore import QPoint as _QPoint  # noqa: E402
from PyQt6.QtCore import Qt as _Qt  # noqa: E402
from PyQt6.QtGui import QCloseEvent  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from tests.test_photo_review_dialog import (  # noqa: E402,F401 - pytest fixtures and helpers
    _drag, _handle_view_point, components, dialog, png)


@pytest.fixture
def drag_distance(qapp):
    """A real platform's start-drag distance (offscreen reports 0), restored afterwards."""
    previous = QApplication.startDragDistance()
    QApplication.setStartDragDistance(10)
    yield 10
    QApplication.setStartDragDistance(previous)


def _press_slip_release(app, dialog, start, offset):
    _drag(app, dialog.view, start, start + offset, _Qt.MouseButton.LeftButton)


def test_a_click_that_slips_one_pixel_is_no_edit(dialog, app, drag_distance, monkeypatch):
    """Positive (F4-1): the body does not move, its evidence stays, nothing to discard."""
    changed = []
    monkeypatch.setattr(dialog, "_geometry_changed", lambda *a, **k: changed.append(a))
    item = dialog._items[1]
    item.component["bbox_basis"] = "body"
    item.component["package_evidence"] = "two end caps"
    dialog._opened_with = deepcopy(dialog._components)
    before = deepcopy(item.component)
    start = dialog.view.mapFromScene(item.pos())
    _press_slip_release(app, dialog, start, _QPoint(1, 0))
    assert changed == []
    assert item.component == before
    assert not dialog.has_corrections()


def test_a_real_drag_still_moves_the_body(dialog, app, drag_distance):
    """Negative (F4-1): beyond the click tolerance the body follows the whole way."""
    item = dialog._items[1]
    x_before = item.component["x_mm"]
    start = dialog.view.mapFromScene(item.pos())
    _press_slip_release(app, dialog, start, _QPoint(30, 0))
    assert item.component["x_mm"] != x_before
    assert dialog.has_corrections()


def test_a_click_on_a_grip_that_slips_resizes_nothing(dialog, app, drag_distance):
    """Positive (F4-1, grips): a 1 px slip on a resize grip is a click too."""
    item = dialog._items[1]
    dialog._select_row(1)
    app.processEvents()
    before = deepcopy(item.component)
    start = _handle_view_point(dialog, item, (1, 0))
    _press_slip_release(app, dialog, start, _QPoint(1, 0))
    assert item.component == before
    assert not dialog.has_corrections()


def test_a_real_grip_drag_still_resizes(dialog, app, drag_distance):
    """Negative (F4-1, grips): beyond the tolerance the grip resizes the body."""
    item = dialog._items[1]
    dialog._select_row(1)
    app.processEvents()
    length = item.component["length_mm"]
    start = _handle_view_point(dialog, item, (1, 0))
    _press_slip_release(app, dialog, start, _QPoint(30, 0))
    assert item.component["length_mm"] != length


def _asking(monkeypatch, dialog, answer):
    asked = []
    monkeypatch.setattr(dialog, "_ask_discard", lambda: asked.append(True) or answer)
    return asked


@pytest.mark.parametrize("how", ["escape", "button"])
def test_without_corrections_the_review_closes_as_before(dialog, app, monkeypatch, how):
    """Negative (F4-2): nothing to lose, no question."""
    asked = _asking(monkeypatch, dialog, False)
    if how == "escape":
        QTest.keyClick(dialog, _Qt.Key.Key_Escape)
    else:
        dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
    app.processEvents()
    assert asked == [] and dialog.result() == QDialog.DialogCode.Rejected


@pytest.mark.parametrize("how", ["escape", "button"])
def test_continuing_the_review_keeps_the_corrections(dialog, app, monkeypatch, how):
    """Positive (F4-2): with corrections the question comes; «Continua la revisione» keeps all."""
    asked = _asking(monkeypatch, dialog, False)
    dialog.fields["x_mm"].setValue(3)
    if how == "escape":
        QTest.keyClick(dialog, _Qt.Key.Key_Escape)
    else:
        dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
    app.processEvents()
    assert asked == [True]
    assert dialog.isVisible()  # not rejected: a rejection hides the review
    assert dialog.get_components()[0]["x_mm"] == 3


class _UserClose(QCloseEvent):
    """A close event as the window's X sends it (spontaneous)."""

    def spontaneous(self):
        return True


def test_the_window_x_asks_and_can_be_refused(dialog, app, monkeypatch):
    """Positive (F4-2): X with corrections asks; «Continua la revisione» ignores the close."""
    asked = _asking(monkeypatch, dialog, False)
    dialog.fields["x_mm"].setValue(3)
    event = _UserClose()
    dialog.closeEvent(event)
    assert asked == [True] and not event.isAccepted() and dialog.isVisible()


def test_the_window_x_without_corrections_closes_without_asking(dialog, app, monkeypatch):
    """Negative (F4-2): X with nothing to lose does not ask."""
    asked = _asking(monkeypatch, dialog, False)
    dialog.closeEvent(_UserClose())
    app.processEvents()
    assert asked == [] and dialog.result() == QDialog.DialogCode.Rejected


def test_a_rejection_by_the_program_never_asks(dialog, app, monkeypatch):
    """Negative (F4-2): reject() and close() made by the program never ask."""
    asked = _asking(monkeypatch, dialog, False)
    dialog.fields["x_mm"].setValue(3)
    dialog.reject()
    assert asked == [] and dialog.result() == QDialog.DialogCode.Rejected


def test_an_edit_brought_back_is_no_correction(dialog, app):
    """Negative (F4-2): a value typed and restored leaves nothing to discard."""
    dialog._select_row(1)  # R1 sits at x = 8.0 mm, a value the field shows exactly
    dialog.fields["x_mm"].setValue(3)
    assert dialog.has_corrections()
    dialog.fields["x_mm"].setValue(8.0)
    assert not dialog.has_corrections()


@pytest.mark.parametrize("button, discarded", [("Scarta", True),
                                               ("Continua la revisione", False)])
def test_the_question_offers_discard_and_continue(dialog, monkeypatch, button, discarded):
    """Positive and negative: «Scarta» discards, «Continua la revisione» (default, Esc) keeps."""
    seen = {}

    def exec_(box):
        seen["default"] = box.defaultButton().text()
        seen["escape"] = box.escapeButton().text()
        seen["text"] = box.text()
        next(b for b in box.buttons() if b.text() == button).click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", exec_)
    assert dialog._ask_discard() is discarded
    assert seen == {"default": "Continua la revisione", "escape": "Continua la revisione",
                    "text": "Scartare le correzioni fatte?"}
