"""@brief Review of 2026-10-27, GUI findings C1-C8: each fix proved with a positive and a negative case.

C1 a checkpoint that vanishes while «Scegli la rete SMD» is open is skipped;
C2 `StartPage.set_projects_available` is defined once; C3 no Italian comment
in the GUI scope (enforced by test_documentation); C5 a BaseException inside
a QRunnable is reported, never escapes; C6 the extraction worker stops
waiting for the photo review when the extraction is cancelled; C7 the
verification and pin-type threads survive the deletion of their dialog;
C8 a non-numeric height is refused with the geometry message.
"""
import ast
import inspect
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6 import sip
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest

from kicadgen.gui import start_page as start_page_module


# ── C1: a .pt that vanishes while the picker is open ─────────────────────────

def test_a_checkpoint_deleted_while_the_picker_is_open_is_skipped(qapp, tmp_path, monkeypatch):
    """@brief Positive: the vanished file is left out without an exception; negative: the others keep their size."""
    from pathlib import Path
    from kicadgen.gui.smd_model_dialog import SmdModelDialog
    from kicadgen.gui.yolo_model_dialog import _size_text
    folder = tmp_path / "smd"
    folder.mkdir()
    kept = folder / "kept.pt"
    kept.write_bytes(b"x" * 2048)
    doomed = folder / "doomed.pt"
    doomed.write_bytes(b"y" * 4096)
    dialog = SmdModelDialog("", folder, default=str(kept))
    try:
        assert dialog.model_list.count() == 2
        # The file disappears between the listing and the stat: the first
        # call sees it as a file, the stat then raises as the OS would.
        real_stat = Path.stat

        def vanishing_stat(self, *args, **kwargs):
            if self.name == "doomed.pt":
                raise FileNotFoundError(str(self))
            return real_stat(self, *args, **kwargs)
        monkeypatch.setattr(Path, "stat", vanishing_stat)
        dialog._fill()
        names = [dialog.model_list.item(i).text().split("\n")[0].split("  ")[0]
                 for i in range(dialog.model_list.count())]
        assert names == ["kept.pt"]
        assert _size_text(2048) in dialog.model_list.item(0).text()
        # Negative: once the file system is quiet again every file is listed with its size.
        monkeypatch.setattr(Path, "stat", real_stat)
        dialog._fill()
        assert dialog.model_list.count() == 2
        assert all("\nPyTorch · " in dialog.model_list.item(i).text() for i in range(2))
    finally:
        dialog.shutdown()
        dialog.close()


def test_an_unreadable_models_folder_leaves_only_the_extra_files(qapp, tmp_path, monkeypatch):
    """@brief A folder whose listing raises is treated as empty; the extra file is still offered."""
    from pathlib import Path
    from kicadgen.gui.smd_model_dialog import SmdModelDialog
    folder = tmp_path / "smd"
    folder.mkdir()
    (folder / "inside.pt").write_bytes(b"x")
    extra = tmp_path / "extra.pt"
    extra.write_bytes(b"z" * 10)
    dialog = SmdModelDialog(str(extra), folder)
    try:
        assert dialog.model_list.count() == 2

        def broken_glob(self, pattern):
            raise PermissionError(str(self))
        monkeypatch.setattr(Path, "glob", broken_glob)
        dialog._fill()
        assert [dialog.model_list.item(i).text().split("\n")[0].split("  ")[0]
                for i in range(dialog.model_list.count())] == ["extra.pt"]
    finally:
        dialog.shutdown()
        dialog.close()


# ── C2: one definition of set_projects_available ─────────────────────────────

def test_set_projects_available_is_defined_once_and_still_works(qapp):
    """@brief The duplicate (AST-identical) method is gone; the behaviour is unchanged."""
    tree = ast.parse(inspect.getsource(start_page_module))
    page_class = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "StartPage")
    definitions = [node for node in page_class.body
                   if isinstance(node, ast.FunctionDef) and node.name == "set_projects_available"]
    assert len(definitions) == 1
    widget = start_page_module.StartPage()
    try:
        widget.set_projects_available(False)
        assert not widget.btn_open.isEnabled()
        assert widget.btn_open.toolTip()
        widget.set_projects_available(True)
        assert widget.btn_open.isEnabled()
    finally:
        widget.close()
        widget.deleteLater()


# ── C5: BaseException inside a QRunnable is reported, never escapes ──────────

class _Escape(BaseException):
    """A BaseException that is not an Exception, like SystemExit or KeyboardInterrupt."""


def test_preview_and_index_runnables_report_a_base_exception(qapp, monkeypatch):
    """@brief Positive: SystemExit-like errors become an empty result; negative: Exception still does the same."""
    from kicadgen.gui import model_candidates_dialog as gui

    def escaping(*args, **kwargs):
        raise _Escape("cancelled")
    monkeypatch.setattr(gui, "_thumbnail", escaping)
    monkeypatch.setattr(gui, "library_models", escaping)
    got = []
    preview = gui._Preview(7, "model.step", None)
    preview.signals.ready.connect(lambda token, result: got.append(("preview", token, result)))
    preview.run()
    index = gui._Index(8)
    index.signals.ready.connect(lambda token, result: got.append(("index", token, result)))
    index.run()
    assert got == [("preview", 7, None), ("index", 8, [])]

    def failing(*args, **kwargs):
        raise RuntimeError("broken")
    monkeypatch.setattr(gui, "_thumbnail", failing)
    monkeypatch.setattr(gui, "library_models", failing)
    got.clear()
    preview = gui._Preview(9, "model.step", None)
    preview.signals.ready.connect(lambda token, result: got.append(("preview", token, result)))
    preview.run()
    index = gui._Index(10)
    index.signals.ready.connect(lambda token, result: got.append(("index", token, result)))
    index.run()
    assert got == [("preview", 9, None), ("index", 10, [])]


def test_balance_task_reports_a_base_exception_by_its_class_name(qapp, monkeypatch):
    """@brief Positive: the error text names the BaseException class; negative: an Exception keeps its message."""
    from kicadgen.gui import ai_account
    from kicadgen.services import providers

    def escaping(*args, **kwargs):
        raise _Escape("cancelled")
    monkeypatch.setattr(providers, "deepseek_balance", escaping)
    got = []
    task = ai_account._BalanceTask(3, providers.PROVIDER_DEEPSEEK)
    task.signals.ready.connect(lambda token, status, error: got.append((token, status, error)))
    task.run()
    assert got == [(3, None, "_Escape")]

    def failing(*args, **kwargs):
        raise RuntimeError("no network")
    monkeypatch.setattr(providers, "deepseek_balance", failing)
    got.clear()
    task = ai_account._BalanceTask(4, providers.PROVIDER_DEEPSEEK)
    task.signals.ready.connect(lambda token, status, error: got.append((token, status, error)))
    task.run()
    assert got == [(4, None, "no network")]


# ── C6: the review wait ends on cancel ───────────────────────────────────────

def test_the_review_wait_returns_none_promptly_when_the_extraction_is_cancelled(qapp, monkeypatch):
    """@brief Positive: cancel during the wait -> None within a few slices; negative: an answer is returned."""
    from kicadgen.gui import workers
    monkeypatch.setattr(workers.ExtractWorker, "REVIEW_WAIT_SLICE_S", 0.05)
    worker = workers.ExtractWorker([])
    answers = []
    thread = threading.Thread(
        target=lambda: answers.append(worker._review_proposals(b"png", [{"name": "U1"}], 20.0, 10.0)))
    thread.start()
    time.sleep(0.1)
    assert thread.is_alive(), "the worker must wait while nobody has answered"
    started = time.monotonic()
    worker.cancel_event.set()
    thread.join(2.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 1.0
    assert answers == [None]

    worker = workers.ExtractWorker([])
    answers = []
    thread = threading.Thread(
        target=lambda: answers.append(worker._review_proposals(b"png", [{"name": "U1"}], 20.0, 10.0)))
    thread.start()
    worker.deliver_review([{"name": "U1"}])
    thread.join(2.0)
    assert not thread.is_alive()
    assert answers == [[{"name": "U1"}]]
    assert not worker.cancel_event.is_set()


# ── C7: the dialog threads belong to QApplication ────────────────────────────

def _wait_for(predicate, timeout_ms=3000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not predicate():
        if time.monotonic() > deadline:
            return False
        QTest.qWait(10)
    return True


def test_the_verification_thread_survives_the_deletion_of_its_dialog(qapp, monkeypatch):
    """@brief Positive: the dialog is deleted while the check runs and the thread still finishes; negative: no thread is left afterwards."""
    from kicadgen.gui import component_check_dialog as ui
    release, started = threading.Event(), threading.Event()

    def slow_verify(**kwargs):
        started.set()
        release.wait(5)
        return []
    monkeypatch.setattr(ui, "verify_component", slow_verify)
    dialog = ui.ComponentCheckDialog(dict(directory=".", name="Demo", rows=[]))
    worker = dialog.worker
    assert worker.parent() is qapp
    assert _wait_for(started.is_set)
    # Destroyed without passing through closeEvent, as a parent window
    # deleted during an application quit would do.
    sip.delete(dialog)
    assert worker.isRunning()
    release.set()
    assert worker.wait(3000)
    assert _wait_for(lambda: sip.isdeleted(worker))


def test_the_pin_type_thread_survives_the_deletion_of_its_dialog(qapp, tmp_path, monkeypatch):
    """@brief Positive: the dialog is deleted while the AI request runs and the thread still finishes; negative: no thread is left afterwards."""
    from kicadgen.gui import pin_type_dialog as ui
    release, started = threading.Event(), threading.Event()

    def slow_suggest(**kwargs):
        started.set()
        release.wait(5)
        return []
    monkeypatch.setattr(ui, "suggest_pin_types", slow_suggest)
    dialog = ui.PinTypeDialog(dict(name="Demo", rows=[], documents=[tmp_path / "sheet.pdf"], model="test"))
    dialog._start()
    worker = dialog.worker
    assert worker.parent() is qapp
    assert _wait_for(started.is_set)
    sip.delete(dialog)
    assert worker.isRunning()
    release.set()
    assert worker.wait(3000)
    assert _wait_for(lambda: sip.isdeleted(worker))


def test_a_finished_pin_type_worker_is_dropped_before_it_deletes_itself(qapp, tmp_path, monkeypatch):
    """@brief After the request ends the dialog holds no reference to the self-deleting thread, and a new request still starts."""
    from kicadgen.gui import pin_type_dialog as ui
    monkeypatch.setattr(ui, "suggest_pin_types", lambda **kwargs: [])
    dialog = ui.PinTypeDialog(dict(name="Demo", rows=[], documents=[tmp_path / "sheet.pdf"], model="test"))
    try:
        dialog._start()
        assert dialog.worker.wait(3000)
        assert _wait_for(lambda: dialog.worker is None)
        assert dialog.start.isEnabled()
        dialog._start()
        assert dialog.worker is not None and dialog.worker.wait(3000)
        assert _wait_for(lambda: dialog.worker is None)
    finally:
        dialog.close()


# ── C8: a non-numeric height is refused with the geometry message ────────────

def _png():
    image = QImage(40, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("#243b28"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_a_textual_height_is_refused_as_invalid_geometry(qapp):
    """@brief Positive: height "n/a" -> the geometry ValueError; negative: a numeric text is accepted."""
    from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
    row = dict(name="U1", kind="ic", x_mm=0.0, y_mm=0.0, length_mm=4.0, width_mm=2.0,
               height_mm="n/a", rotation_deg=0.0)
    with pytest.raises(ValueError, match="Invalid PCB dimensions or component geometry"):
        PhotoReviewDialog(_png(), [row], 40, 20)
    dialog = PhotoReviewDialog(_png(), [dict(row, height_mm="1.5")], 40, 20)
    try:
        assert dialog._components[0]["height_mm"] == "1.5"
    finally:
        dialog.close()
