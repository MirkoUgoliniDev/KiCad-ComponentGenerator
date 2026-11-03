"""@brief «Verifica componente»: quitting never waits unbounded for a KiCad command.

Review finding 2026-11-03: `_CheckWorker._stop_for_quit` waited without limit
for `verify_component`, whose single kicad-cli command may last 60 s.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time

import pytest
pytest.importorskip("PyQt6")

from kicadgen.gui import component_check_dialog as ccd
from kicadgen.services.component_check import Check


def _worker():
    return ccd._CheckWorker(dict(directory=".", name="X", rows=[]))


def test_a_command_deaf_to_the_stop_cannot_hold_the_exit(qapp, monkeypatch):
    """@brief Positive: the wait is bounded and the running thread leaves QApplication.

    Left owned by QApplication, a still-running QThread would be destroyed
    with it and abort the process.
    """
    release, entered = threading.Event(), threading.Event()

    def deaf(**_):
        entered.set()
        release.wait(10)
    monkeypatch.setattr(ccd, "verify_component", deaf)
    monkeypatch.setattr(ccd, "CHECK_QUIT_WAIT_MS", 200)
    worker = _worker()
    worker.start()
    try:
        assert entered.wait(5), "the fake KiCad command must be running"
        started = time.monotonic()
        worker._stop_for_quit()
        assert time.monotonic() - started < 2.0
        assert worker.isRunning() and worker.parent() is None
    finally:
        release.set()
        assert worker.wait(3000)


def test_a_verification_that_sees_the_stop_ends_and_stays_owned(qapp, monkeypatch):
    """@brief Negative: checks emitted between commands see the stop at once.

    The stop is sent once the first check was emitted (a start marker, not a
    fixed sleep that a loaded machine can outlast or undershoot), and the
    thread is always reaped, even when an assertion fails.
    """
    first = threading.Event()

    def checks(*, emit, **_):
        for _ in range(1000):
            emit(Check("OK", "step", ""))
            first.set()
            time.sleep(0.01)
    monkeypatch.setattr(ccd, "verify_component", checks)
    worker = _worker()
    worker.start()
    try:
        assert first.wait(5), "the fake verification must have started"
        worker._stop_for_quit()
        assert worker.isFinished() and worker.aborted
        assert worker.parent() is qapp
    finally:
        worker.stop()
        worker.wait(15000)
