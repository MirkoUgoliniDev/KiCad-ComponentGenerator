"""@brief Phases stay listed on the progress card, with their outcome (HANDOFF points 135-136).

User, 2026-09-28: «la fase OpenCV è talmente veloce che non si vede
neppure». Instead of slowing the analysis down, every phase that ends (another
tool is announced) stays on the card. Contract of point 136:
- the outcome is the FIRST status the phase showed (the last one is often the
  preparation of the next phase), replaced by a warning of the phase, if any;
- the running phase is the last row, «● … in corso», so finished and running
  phases are told apart («sono già tutte spuntate le voci?»).
"""
import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from kicadgen.gui.analysis_overlay import PHASE_HISTORY_ROWS, AnalysisOverlay
from kicadgen.services.messages import as_warning


@pytest.fixture
def overlay(qapp):
    parent = QWidget()
    parent.resize(1100, 900)
    card = AnalysisOverlay(parent)
    parent.show()
    card.start()
    QApplication.processEvents()
    yield card
    card.stop()
    parent.close()
    parent.deleteLater()


def test_a_finished_phase_shows_its_first_status(overlay):
    overlay.set_tool("llm", "deepseek-flash")
    overlay.set_status("Analisi principale completata: layout=castellated.")
    overlay.set_status("Viste dall'alto disponibili: foto.jpg.")          # next phase's prelude
    overlay.set_tool("opencv", "ricerca e calibrazione della scheda")
    overlay.set_status("OpenCV (locale): scheda trovata in foto.jpg, 987 x 426 px.")
    overlay.set_status("Risoluzione della foto usata per l'inventario: 23.1 px/mm.")
    overlay.set_tool("yolo", "pcb_yolov8s.onnx")
    text = overlay.phases_plain_text()
    assert "✓ Modello AI (remoto, a consumo) — Analisi principale completata" in text
    assert "✓ OpenCV (analisi immagine locale) — OpenCV (locale): scheda trovata" in text
    assert "Viste dall'alto" not in text and "Risoluzione della foto" not in text


def test_the_running_phase_is_the_last_row(overlay):
    overlay.set_tool("opencv")
    overlay.set_status("OpenCV: scheda trovata.")
    overlay.set_tool("yolo")
    lines = [line for line in overlay.phases_plain_text().splitlines() if line]
    assert lines[0].startswith("✓ OpenCV")
    assert lines[-1] == "● YOLO (rilevamento locale) — in corso"


def test_a_warning_of_the_phase_replaces_its_outcome(overlay):
    overlay.set_tool("opencv")
    overlay.set_status("OpenCV (locale): scheda trovata.")
    overlay.set_status(as_warning("Lato destro: sulla foto si contano 4 semi-fori, "
                                  "l'analisi dichiara 3 contatti."))
    overlay.set_status("Lato sinistro: 3 semi-fori, come i contatti dichiarati.")
    overlay.set_tool("yolo")
    assert "⚠ OpenCV (analisi immagine locale) — Lato destro: sulla foto si contano 4" \
        in overlay.phases_plain_text()
    assert "#f9e2af" in overlay.phases.text()                    # drawn in the warning colour


def test_a_plain_status_is_never_taken_for_a_warning(overlay):
    """Negative: the words do not matter, only the declared kind."""
    overlay.set_tool("opencv")
    overlay.set_status("Avviso: da verificare")                   # a warning-sounding plain line
    overlay.set_tool("yolo")
    assert "✓ OpenCV" in overlay.phases_plain_text()
    assert "⚠" not in overlay.phases_plain_text()


def test_the_same_tool_with_another_detail_is_not_a_new_phase(overlay):
    """Negative: only a change of tool ends a phase."""
    overlay.set_tool("llm", "model A")
    overlay.set_tool("llm", "model A, verifica")
    assert "✓" not in overlay.phases_plain_text()
    assert overlay.phases_plain_text().count("● Modello AI") == 1


def test_service_lines_never_become_an_outcome(overlay):
    overlay.set_tool("opencv")
    overlay.set_status("AI_CALL_START id=3 model=x tool=y")
    overlay.set_status("OpenCV: scheda trovata.")
    overlay.set_tool("yolo")
    assert "AI_CALL" not in overlay.phases_plain_text()
    assert "OpenCV: scheda trovata." in overlay.phases_plain_text()


def test_a_new_operation_starts_with_an_empty_list(overlay):
    overlay.set_tool("opencv")
    overlay.set_tool("yolo")
    assert overlay.phases_plain_text()
    overlay.start()
    assert overlay.phases_plain_text() == "" and not overlay.phases.isVisibleTo(overlay)


def test_a_job_with_steps_lists_no_phases(overlay):
    """Negative: the generation ticks its own steps; a second list would repeat them."""
    overlay.set_steps(["Footprint", "Modello 3D"])
    overlay.set_tool("footprint")
    overlay.set_tool("freecad")
    assert overlay.phases_plain_text() == ""


def test_the_completed_job_closes_its_last_phase(overlay):
    overlay.set_tool("yolo")
    overlay.set_status("Revisione dei corpi: 42 proposte tenute su 43.")
    overlay.show_completed("Fatto.")
    text = overlay.phases_plain_text()
    assert "✓ YOLO (rilevamento locale) — Revisione dei corpi" in text
    assert "in corso" not in text


def test_the_card_does_not_grow_as_phases_end(overlay):
    before = overlay.card.height()
    for tool in ("llm", "opencv", "yolo", "llm_inventory", "opencv", "yolo"):
        overlay.set_tool(tool)
        overlay.set_status(f"Fase {tool} conclusa con un esito abbastanza lungo da occupare la riga.")
        QApplication.processEvents()
        assert overlay.card.height() == before
    lines = [line for line in overlay.phases_plain_text().splitlines() if line]
    assert len(lines) == PHASE_HISTORY_ROWS                     # completed ones + the running one
    assert lines[-1].startswith("● ")
    assert overlay.phases.toolTip().count("✓") == 5             # the tooltip keeps them all
