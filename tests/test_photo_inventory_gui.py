"""Verifica Qt del confronto e del log; il renderer VTK non e' coinvolto."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QLabel, QMessageBox
from kicadgen.gui import main_window as gui
from kicadgen.services.datasheet_extractor import ExtractedParams
from kicadgen.services.photo_inventory import draw_inventory
import numpy as np


class PreviewStub(QWidget):
    def fit_view(self): pass
    def clear_preview(self): pass
    def render_params(self, *args): pass


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    w = gui.MainWindow()
    yield w
    w.close()
    app.processEvents()


def test_progress_is_visible_on_project_tab_and_literal(window):
    window._on_ai_progress("Crop 1: <b>R1</b> & C1")
    assert "<b>R1</b> & C1" in window.ai_progress_text.toPlainText()
    assert "<b>R1</b> & C1" in window.log_text.toPlainText()


def test_comparison_button_opens_valid_image_and_resets_on_next_result(window,monkeypatch):
    png = draw_inventory(np.full((80,160,3),40,np.uint8), [])
    params = ExtractedParams(name="",description="",tags="",confidence="medium",notes="",
                             layout="castellated",castellated_sides=[],photo_overlay_png=png,
                             photo_source="top.png")
    window._on_extracted(params)
    assert window.btn_photo_comparison.isEnabled()
    def inspect(dialog):
        assert "top.png" in dialog.windowTitle()
        assert any(not label.pixmap().isNull() for label in dialog.findChildren(QLabel))
        return 0
    monkeypatch.setattr(QDialog,"exec",inspect)
    window.btn_photo_comparison.click()
    params.photo_overlay_png = None
    window._on_extracted(params)
    assert not window.btn_photo_comparison.isEnabled()
