"""@brief Candidate search of the photo review: quitting never destroys a running thread.

Review finding 2026-11-03 (O2): `_MatchWorker._stop_for_quit` waited a bounded
time and then did nothing, so QApplication destroyed a still-running QThread
at exit, which aborts the process.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time

import pytest
pytest.importorskip("PyQt6")

from kicadgen.cad3d import kicad_models
from kicadgen.gui import photo_review_dialog as prd


def test_a_search_deaf_to_the_stop_leaves_qapplication(qapp, monkeypatch):
    """@brief Positive: after the bounded wait the running thread has no owner."""
    release, entered = threading.Event(), threading.Event()

    def deaf(*_args, **_kwargs):
        entered.set()
        release.wait(10)
        return {}
    monkeypatch.setattr(kicad_models, "model_candidates", deaf)
    monkeypatch.setattr(prd, "MATCH_QUIT_WAIT_MS", 200)
    worker = prd._MatchWorker({"kind": "ic"})
    worker.start()
    try:
        assert entered.wait(5), "the fake search must be running"
        started = time.monotonic()
        worker._stop_for_quit()
        assert time.monotonic() - started < 2.0
        assert worker.isRunning() and worker.parent() is None
    finally:
        release.set()
        assert worker.wait(3000)


def test_a_search_that_sees_the_stop_ends_and_stays_owned(qapp, monkeypatch):
    """@brief Negative: a search that honours the cancel finishes within the wait."""
    entered = threading.Event()

    def polite(*_args, cancel=None, **_kwargs):
        entered.set()
        cancel.wait(10)
        return {}
    monkeypatch.setattr(kicad_models, "model_candidates", polite)
    worker = prd._MatchWorker({"kind": "ic"})
    worker.start()
    assert entered.wait(5)
    worker._stop_for_quit()
    assert worker.isFinished()
    assert worker.parent() is qapp
