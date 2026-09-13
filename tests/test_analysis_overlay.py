import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QWidget
from kicadgen.gui.analysis_overlay import AnalysisOverlay


def test_overlay_animation_resize_literal_status_and_cleanup():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(900, 650)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start()
        app.processEvents()
        assert overlay.isVisible() and overlay.timer.isActive()
        assert overlay.geometry() == parent.rect()
        angle = overlay.spinner.angle
        overlay._tick()
        assert overlay.spinner.angle != angle
        overlay.set_status("<b>Crop 2: analisi</b>")
        assert overlay.status.text() == "<b>Crop 2: analisi</b>"
        overlay.set_status("x" * 1000)
        assert len(overlay.status.text()) <= 220
        parent.resize(650, 500)
        app.processEvents()
        assert overlay.geometry() == parent.rect()
        spy = QSignalSpy(overlay.log_requested)
        overlay.log_button.click()
        assert len(spy) == 1
        overlay.stop()
        assert not overlay.isVisible() and not overlay.timer.isActive()
        overlay.start()
        assert overlay.status.text() == "Preparazione dei documenti…"
        assert overlay.elapsed.text().endswith("00:00")
        overlay.start('Generazione footprint e modello 3D', 'Generazione…')
        assert overlay.title.text() == 'Generazione footprint e modello 3D'
        assert overlay.status.text() == 'Generazione…'
    finally:
        overlay.stop()
        parent.close()
