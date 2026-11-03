"""@brief A selected weights file that vanishes is examined once, not forever.

Review finding 2026-11-03: in «Scegli il modello YOLO» and «Scegli la rete SMD»
a selected file deleted from disk had no identity, so its failed result was not
remembered and every result restarted the same job (thousands of starts in two
seconds). The failure is now kept until the selection really changes.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QThreadPool

from kicadgen.gui import smd_model_dialog as smd_dlg
from kicadgen.gui import yolo_model_dialog as yolo_dlg
from kicadgen.services import photo_yolo


@pytest.fixture(autouse=True)
def _clean_results(qapp):
    """@brief Empty session cache before and after each test."""
    yolo_dlg._RESULTS.clear()
    yield
    yolo_dlg._RESULTS.clear()


def _settle(app, rounds=20):
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()


def _count_starts(dialog):
    starts = []
    original = dialog._start

    def counting(kind, path):
        starts.append((kind, os.path.basename(path)))
        original(kind, path)
    dialog._start = counting
    return starts


def _select(dialog, name):
    for row in range(dialog.model_list.count()):
        if dialog.model_list.item(row).data(0x0100).endswith(name):
            dialog.model_list.setCurrentRow(row)
            return
    raise AssertionError(name)


def _yolo_dialog(tmp_path, monkeypatch):
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "base.onnx").write_bytes(b"onnx")
    (folder / "other.onnx").write_bytes(b"onnx")

    def validate(path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        return {"model_classes": ["ic"], "missing_classes": [], "class_order": "by name"}
    monkeypatch.setattr(photo_yolo, "validate_yolo_setup", validate)
    return folder, yolo_dlg.YoloModelDialog(str(folder / "base.onnx"), folder)


def test_a_vanished_yolo_model_is_examined_once(qapp, tmp_path, monkeypatch):
    """@brief Negative: the job result of a deleted file never starts the job again."""
    folder, dialog = _yolo_dialog(tmp_path, monkeypatch)
    try:
        _settle(qapp)
        (folder / "base.onnx").unlink()
        yolo_dlg._RESULTS.clear()
        starts = _count_starts(dialog)
        dialog._show_selected()
        _settle(qapp)
        assert sorted(starts) == [("check", "base.onnx"), ("sha256", "base.onnx")]
        assert not dialog._jobs
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_a_real_new_selection_examines_the_vanished_yolo_model_again(qapp, tmp_path,
                                                                    monkeypatch):
    """@brief Positive: leaving the file and selecting it again retries it, once."""
    folder, dialog = _yolo_dialog(tmp_path, monkeypatch)
    try:
        _settle(qapp)
        (folder / "base.onnx").unlink()
        yolo_dlg._RESULTS.clear()
        dialog._show_selected()
        _settle(qapp)
        starts = _count_starts(dialog)
        _select(dialog, "other.onnx")
        _settle(qapp)
        _select(dialog, "base.onnx")
        _settle(qapp)
        assert starts.count(("check", "base.onnx")) == 1
        assert starts.count(("sha256", "base.onnx")) == 1
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def _smd_dialog(tmp_path):
    folder = tmp_path / "smd"
    folder.mkdir()
    (folder / "net.pt").write_bytes(b"pt")
    (folder / "net.onnx").write_bytes(b"onnx")
    return folder, smd_dlg.SmdModelDialog(str(folder / "net.pt"), folder)


def test_a_vanished_smd_network_is_examined_once(qapp, tmp_path):
    """@brief Negative: same contract in the SMD window."""
    folder, dialog = _smd_dialog(tmp_path)
    try:
        _settle(qapp)
        (folder / "net.pt").unlink()
        yolo_dlg._RESULTS.clear()
        starts = _count_starts(dialog)
        dialog._show_selected()
        _settle(qapp)
        assert sorted(starts) == [("sha256", "net.pt"), ("smd_check", "net.pt")]
        assert not dialog._jobs
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_an_existing_smd_network_keeps_its_result_in_the_session_cache(qapp, tmp_path):
    """@brief Positive counterpart: a readable file is remembered by identity."""
    folder, dialog = _smd_dialog(tmp_path)
    try:
        _settle(qapp)
        assert yolo_dlg._remembered(str(folder / "net.pt"), "sha256")
        assert not dialog._results._unstored
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_the_smd_list_names_each_file_format_from_its_suffix(qapp, tmp_path):
    """@brief Review finding 2026-11-03: an ONNX export is not labelled «PyTorch»."""
    _folder, dialog = _smd_dialog(tmp_path)
    try:
        lines = {dialog.model_list.item(r).text().split("\n")[0].split()[0]:
                 dialog.model_list.item(r).text().split("\n")[1]
                 for r in range(dialog.model_list.count())}
        assert lines["net.onnx"].startswith("ONNX ·")
        assert lines["net.pt"].startswith("PyTorch ·")
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_a_yolo_model_still_being_copied_is_examined_once_per_selection(qapp, tmp_path,
                                                                       monkeypatch):
    """@brief Review O8, positive: a file growing during its jobs starts each job once.

    Every job appends to the file, as a copy in progress does: its identity at
    the end is never the one examined, which used to restart the jobs on every
    redraw. The result is remembered for the content the job read.
    """
    from kicadgen.services import yolo_models
    folder = tmp_path / "models"
    folder.mkdir()
    growing = folder / "base.onnx"
    growing.write_bytes(b"onnx")
    sizes = []

    def grow():
        sizes.append(os.path.getsize(growing))
        with open(growing, "ab") as handle:
            handle.write(b"more")

    def digest(path):
        grow()
        return "d" * 64

    def validate(path):
        grow()
        return {"model_classes": ["ic"], "missing_classes": [], "class_order": "by name"}
    monkeypatch.setattr(yolo_models, "sha256_of", digest)
    monkeypatch.setattr(photo_yolo, "validate_yolo_setup", validate)
    dialog = yolo_dlg.YoloModelDialog(str(growing), folder)
    starts = _count_starts(dialog)
    try:
        for _ in range(3):
            dialog._show_selected()
            _settle(qapp)
        assert not starts, "the first jobs already answered for this selection"
        assert len(sizes) == 2, "one sha256 and one check, never restarted"
        assert dialog._results.get(str(growing), "sha256") == "d" * 64
        examined = {identity[1] for identity in yolo_dlg._RESULTS}
        assert examined <= set(sizes), "remembered under the size the job read"
        assert os.path.getsize(growing) not in examined
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_a_stable_yolo_model_answers_from_the_session_cache(qapp, tmp_path, monkeypatch):
    """@brief Review O8, negative: an unchanged file keeps nothing per selection."""
    folder, dialog = _yolo_dialog(tmp_path, monkeypatch)
    try:
        _settle(qapp)
        assert yolo_dlg._remembered(str(folder / "base.onnx"), "check")
        assert not dialog._results._settled and not dialog._results._unstored
    finally:
        dialog.shutdown()
        dialog.deleteLater()
