"""@brief Modal helper windows are deleted after use, not kept until the parent closes.

Review finding 2026-11-03: the model picker, the crop previews of the accepted
models archive and the CAD candidates window of the photo review stayed alive
as children of their parent after `exec()`; a late worker result could still
reach the model picker.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtGui import QImage
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtWidgets import QDialog, QWidget

from kicadgen.gui import model_dialog
from kicadgen.gui.accepted_models_dialog import ConsolidateDialog


def _flush_deletes():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    QCoreApplication.processEvents()


def test_pick_model_deletes_its_picker_and_still_returns_the_choice(qapp, monkeypatch):
    """@brief Positive: the choice is returned; the picker no longer hangs on the parent."""
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models",
                        lambda self, force_refresh=False: None)
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "selected_model",
                        lambda self: "vendor/model")
    parent = QWidget()
    try:
        assert model_dialog.pick_model("vendor/model", parent) == "vendor/model"
        _flush_deletes()
        assert parent.findChildren(model_dialog.ModelPickerDialog) == []
    finally:
        parent.deleteLater()


def test_a_cancelled_pick_is_deleted_too_and_returns_none(qapp, monkeypatch):
    """@brief Negative: cancelling returns None; the window goes the same way."""
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models",
                        lambda self, force_refresh=False: None)
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)
    parent = QWidget()
    try:
        assert model_dialog.pick_model("vendor/model", parent) is None
        _flush_deletes()
        assert parent.findChildren(model_dialog.ModelPickerDialog) == []
    finally:
        parent.deleteLater()


def _png() -> bytes:
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def test_a_crop_preview_lives_only_while_it_is_shown(qapp, monkeypatch):
    """@brief Positive: shown as a child during exec, gone afterwards."""
    dialog = ConsolidateDialog([])
    # Only the crop matters here, not whether the body may be remembered.
    dialog._components = [dict(name="R1", kind="resistor", _accepted_image_png=_png())]
    seen = []

    def inspect(preview):
        seen.append(preview.parent() is dialog)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", inspect)
    try:
        dialog._show_component_crop(0, 0)
        assert seen == [True]
        _flush_deletes()
        assert dialog.findChildren(QDialog) == []
    finally:
        dialog.deleteLater()


def test_an_unreadable_crop_opens_no_preview(qapp, monkeypatch):
    """@brief Negative: no image, no window to keep or to delete."""
    dialog = ConsolidateDialog([])
    dialog._components = [dict(name="R1", kind="resistor", _accepted_image_png=b"x")]
    seen = []
    monkeypatch.setattr(QDialog, "exec", lambda preview: seen.append(preview))
    try:
        dialog._show_component_crop(0, 0)
        assert seen == []
    finally:
        dialog.deleteLater()
