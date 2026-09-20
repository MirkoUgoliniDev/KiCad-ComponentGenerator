import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QWidget
from kicadgen.gui.analysis_overlay import TOOLS, AnalysisOverlay


@pytest.fixture
def overlay():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(900, 650)
    widget = AnalysisOverlay(parent)
    parent.show()
    widget.start()
    app.processEvents()
    yield widget
    widget.stop()
    parent.close()


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_each_tool_is_named_and_drawn_differently(overlay, tool):
    """Il colore da solo non basta: ogni strumento ha anche una forma sua."""
    overlay.set_tool(tool, "dettaglio")
    name, color, glyph = TOOLS[tool]
    assert overlay.tool.isVisibleTo(overlay)
    assert overlay.tool.text() == f"{name} · dettaglio"
    assert (overlay.spinner.glyph, overlay.spinner.color) == (glyph, color)
    assert len({shape for _, _, shape in TOOLS.values()}) == len(TOOLS)


def test_an_unknown_tool_leaves_the_bare_ring_instead_of_failing(overlay):
    """Una fase nuova non deve poter far cadere un'analisi in corso."""
    overlay.set_tool("strumento-mai-visto")
    assert not overlay.tool.isVisibleTo(overlay)
    assert overlay.spinner.glyph == ""


def test_percentage_only_when_a_real_count_backs_it(overlay):
    overlay.set_progress(12, 45, "componenti")
    assert overlay.progress_row.isVisibleTo(overlay)
    assert overlay.progress.value() == 12 and overlay.progress.maximum() == 45
    assert overlay.progress_label.text() == "27% · 12/45 componenti"
    for done, total in ((None, None), (3, 0), (3, None), (7, 5), (-1, 5)):
        overlay.set_progress(done, total)
        assert not overlay.progress_row.isVisibleTo(overlay)


def test_service_markers_stay_in_the_log_and_off_the_card(overlay):
    """Sulla scheda occupavano il posto di cio' che stava accadendo."""
    overlay.set_status("Generazione del footprint KiCad…")
    for marker in ("AI_CALL_PROMPT_END id=1 model=anthropic/claude-opus-5",
                   "KICADGEN_IMPORT_OK: ESP32-S3-WROOM-1U.STEP; parts=231",
                   "AI_CALL_START id=2"):
        overlay.set_status(marker)
        assert overlay.status.text() == "Generazione del footprint KiCad…"
    # Una frase che comincia per maiuscolo non e' un marcatore: resta.
    overlay.set_status("Viste dall’alto disponibili: nessuna.")
    assert overlay.status.text() == "Viste dall’alto disponibili: nessuna."


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
