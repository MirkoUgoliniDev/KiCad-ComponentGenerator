"""@brief KiCad preview and installations blocked while background work runs (audit 2026-10-15, M3).

«Prova in KiCad», «Installa nel progetto KiCad» and «Installa come libreria
globale» (KiCad-tab buttons, «Strumenti» entries, usage wizard) stayed
clickable during a 3D regeneration, a footprint rewrite or an analysis: the
old or half-written STEP got installed. Now they are disabled with the reason
while any worker runs (or the automatic generation owns the window), every
method refuses on its own (defence in depth), and they come back when the
work ends.
"""
import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from tests.test_generated_files_alignment import FakeCad3D, _enable_3d, _project, dialogs  # noqa: F401
from tests.test_photo_inventory_gui import gui

KICAD_COMMANDS = gui.MainWindow.KICAD_COMMAND_ATTRS
WORKER_SLOTS = ("_worker", "_extract_worker", "_cad3d_worker", "_terminal_worker",
                "_reconcile_worker")


def _enabled(window):
    return {attr: getattr(window, attr).isEnabled() for attr in KICAD_COMMANDS}


def _running():
    """A worker stand-in that reports itself running."""
    return SimpleNamespace(isRunning=lambda: True)


@pytest.mark.parametrize("slot", WORKER_SLOTS + ("automatic",))
def test_running_work_disables_and_refuses_every_kicad_command(
        window, monkeypatch, tmp_path, dialogs, slot):  # noqa: F811
    """Negative: with work running each command is off with the reason and
    each entry point refuses with no folder dialog, install or preview.
    Positive: the same project with the work finished is fully enabled."""
    _project(window, monkeypatch, tmp_path)
    assert all(_enabled(window).values())
    if slot == "automatic":
        monkeypatch.setattr(window, "_automatic_generation_active", True)
    else:
        monkeypatch.setattr(window, slot, _running())
    window._refresh_output_actions()
    assert not any(_enabled(window).values()), _enabled(window)
    for attr in KICAD_COMMANDS:
        assert "lavoro" in getattr(window, attr).toolTip(), attr
    for command in (window._test_in_kicad, window._install_project_library,
                    window._install_global_library):
        command()
    window._install_kicad_library(tmp_path / "dest", project_library=True)
    assert dialogs["folder"] == [] and dialogs["installed"] == [] and dialogs["preview"] == []
    assert len(dialogs["warning"]) == 4
    # The work ends.
    if slot == "automatic":
        monkeypatch.setattr(window, "_automatic_generation_active", False)
    else:
        monkeypatch.setattr(window, slot, None)
    window._refresh_output_actions()
    assert all(_enabled(window).values()), _enabled(window)


def test_the_wizard_buttons_are_refused_too(window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """The usage wizard calls the same methods: refused while work runs."""
    _project(window, monkeypatch, tmp_path)
    monkeypatch.setattr(window, "_cad3d_worker", _running())
    shown = []
    monkeypatch.setattr(gui.QWizard, "exec", lambda wizard: shown.append(wizard) or 0)
    window._show_kicad_usage_wizard()
    buttons = [b for b in shown[0].findChildren(gui.QPushButton)
               if b.text().startswith("Installa")]
    assert len(buttons) == 2
    for button in buttons:
        button.click()
    assert dialogs["folder"] == [] and dialogs["installed"] == []
    assert len(dialogs["warning"]) == 2


class _BlockingThread(QThread):
    """A real QThread that runs until released."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def run(self):  # noqa: D401 - QThread override
        self.release.wait(10)


def _wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_commands_go_off_when_work_starts_and_back_when_it_ends(
        window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """A real thread: off right after the start (without any other refresh),
    on again once `finished` has been delivered."""
    _project(window, monkeypatch, tmp_path)
    thread = _BlockingThread()
    monkeypatch.setattr(window, "_terminal_worker", thread)
    thread.start()
    try:
        window._track_background_work(thread)
        assert not any(_enabled(window).values()), _enabled(window)
    finally:
        thread.release.set()
        thread.wait(5000)
    assert _wait_until(lambda: all(_enabled(window).values())), _enabled(window)


def test_a_3d_regeneration_switches_the_menu_entries_off_while_it_runs(
        window, monkeypatch, tmp_path, dialogs):  # noqa: F811
    """The audited path: «Rigenera 3D» disables the tabs but the «Strumenti»
    entries stayed clickable. Now they are off until the worker ends."""
    _project(window, monkeypatch, tmp_path)
    _enable_3d(window)

    class SlowCad3D(FakeCad3D):
        def start(self, *_args):
            QThread.start(self)

        def run(self):  # noqa: D401 - QThread override
            self.gate.wait(10)

    SlowCad3D.gate = threading.Event()
    monkeypatch.setattr(gui, "Cad3DWorker", SlowCad3D)
    window._regenerate_3d()
    worker = window._cad3d_worker
    try:
        assert isinstance(worker, SlowCad3D) and worker.isRunning()
        assert not window.action_export.isEnabled()
        assert not window.action_install_global.isEnabled()
        assert not window.action_test_kicad.isEnabled()
    finally:
        SlowCad3D.gate.set()
        worker.wait(5000)
    assert _wait_until(lambda: window.action_export.isEnabled()), window.action_export.toolTip()
