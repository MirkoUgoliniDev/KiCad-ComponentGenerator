"""@brief The animated splash runs in its own process and ends with the pipe (user, 2026-10-13).

The GUI thread is busy building the window for seconds, so an animated
splash cannot live in the application's process. These tests cover the
parent-side handle with a fake process, the launcher's fallback to the still
splash, and the real child process started offscreen: alive while the pipe
is open, gone shortly after it closes.
"""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kicadgen import splash

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("project_launcher", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeProcess:
    """A Popen stand-in: `exits_after` waits before the process is 'gone'."""

    def __init__(self, exits_after=0):
        self.stdin = FakeStdin()
        self.waits = 0
        self.killed = False
        self.exits_after = exits_after

    def wait(self, timeout=None):
        self.waits += 1
        if self.killed or self.waits > self.exits_after:
            return 0
        raise subprocess.TimeoutExpired("splash", timeout)

    def kill(self):
        self.killed = True


def test_finish_closes_the_pipe_and_waits_for_the_child():
    process = FakeProcess()
    handle = splash.ProcessSplash(process)
    handle.finish(window=object())
    assert process.stdin.closed and process.waits == 1 and not process.killed
    handle.close()
    assert process.waits == 1, "a second call does nothing"


def test_a_child_that_ignores_the_pipe_is_killed():
    process = FakeProcess(exits_after=5)
    splash.ProcessSplash(process).close()
    assert process.killed and process.waits == 2


def test_start_passes_the_language_and_hides_the_console(monkeypatch):
    seen = {}

    def popen(command, **kwargs):
        seen.update(command=command, **kwargs)
        return FakeProcess()

    handle = splash.ProcessSplash.start("de", popen=popen)
    assert isinstance(handle, splash.ProcessSplash)
    assert seen["command"][-2:] == [splash.SPLASH_FLAG, "de"]
    assert seen["command"][0] == sys.executable
    assert seen["stdin"] is subprocess.PIPE
    assert seen["stdout"] is subprocess.DEVNULL and seen["stderr"] is subprocess.DEVNULL
    if os.name == "nt":
        assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_the_command_runs_the_launcher_from_source_and_the_executable_when_frozen(monkeypatch):
    assert splash.splash_command("it")[1] == str(ROOT / "main.py")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert splash.splash_command("it") == [sys.executable, splash.SPLASH_FLAG, "it"]


def test_create_splash_falls_back_to_the_still_splash_when_the_process_fails(launcher, qapp, capsys):
    def failing(language):
        raise OSError("no such executable")

    widget = launcher.create_splash("it", start_process=failing)
    try:
        assert widget.isVisible() and widget.message().strip() == splash.SPLASH_MESSAGES["it"]
        assert "splash animato non avviato" in capsys.readouterr().err
    finally:
        widget.close()
        widget.deleteLater()


def test_create_splash_returns_the_process_handle(launcher):
    handle = object()
    assert launcher.create_splash("fr", start_process=lambda language: handle) is handle


def test_dots_cycle_from_none_to_three():
    assert [splash.dots_message("Avvio in corso…", step) for step in range(5)] == [
        "Avvio in corso", "Avvio in corso.", "Avvio in corso..", "Avvio in corso...", "Avvio in corso"]
    assert splash.dots_message("Starting...", 2) == "Starting.."


def test_the_message_falls_back_to_english():
    assert splash.splash_message("xx") == splash.SPLASH_MESSAGES["en"]
    assert splash.splash_message("de") == splash.SPLASH_MESSAGES["de"]


def test_the_real_child_lives_on_the_pipe_and_exits_when_it_closes():
    """The child process itself, offscreen: no window on the tester's desktop."""
    pytest.importorskip("PyQt6")
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", KICADGEN_BOOTSTRAP_ROOT=str(ROOT))
    process = subprocess.Popen(splash.splash_command("it"), stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment)
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.1)
        assert process.poll() is None, f"the splash quit on its own: {process.stderr.read().decode(errors='replace')}"
        handle = splash.ProcessSplash(process)
        started = time.monotonic()
        handle.close()
        assert process.poll() is not None, "still alive after the pipe closed"
        assert time.monotonic() - started < 3
        assert process.returncode == 0, process.stderr.read().decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
