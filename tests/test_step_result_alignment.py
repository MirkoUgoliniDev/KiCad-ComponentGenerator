"""@brief A step's result line starts under the step's text, never under its mark (user, 2026-11-15).

The user saw «3 trovati · 31 non trovati · 12 da verificare» start below the
«✓» of step 2 and stick out of the column, on the generation card and on the
STEP regeneration card: the result was a line break inside the cell that also
held the mark. The marks now have a column of their own. Checked on the
document the label really draws (positions laid out by Qt), not on its text.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QTextDocument  # noqa: E402
from PyQt6.QtWidgets import QWidget  # noqa: E402

from kicadgen.gui.analysis_overlay import AnalysisOverlay  # noqa: E402

RESULT = "3 trovati · 31 non trovati · 12 da verificare"


@pytest.fixture
def overlay(qapp):
    """@brief A card on a host widget, released after the test."""
    host = QWidget()
    host.resize(1000, 800)
    card = AnalysisOverlay(host)
    yield card
    card.stop()
    host.deleteLater()
    qapp.processEvents()


def _layout(card):
    """@brief (text, x of the block, x of each of its lines) of every block the step label draws."""
    document = QTextDocument()
    document.setHtml(card.steps.text())
    document.setTextWidth(900)
    layout = document.documentLayout()
    blocks, block = [], document.begin()
    while block.isValid():
        lines = block.layout()
        blocks.append((block.text(), layout.blockBoundingRect(block).x(),
                       [lines.lineAt(i).x() for i in range(lines.lineCount())]))
        block = block.next()
    return blocks


@pytest.mark.parametrize("title, labels, with_result, running", [
    # The generation card of the user's first screenshot (result on step 2).
    ("Generazione footprint e modello STEP",
     ["Footprint 2D (KiCadGen)", "Confronto con i consolidati", "Modelli dalla libreria KiCad",
      "Solido 3D con FreeCAD"], 1, 4),
    # The STEP regeneration card of the second one (result on step 1, step 2 running).
    ("Generazione modello STEP",
     ["Confronto con i consolidati", "Modelli dalla libreria KiCad", "Solido 3D con FreeCAD"], 0, 1),
])
def test_the_result_line_starts_where_the_step_text_starts(overlay, title, labels, with_result,
                                                           running):
    overlay.start(title, abortable=True)
    overlay.set_steps(labels)
    overlay.set_step_result(with_result, RESULT)
    overlay.set_current_step(running)
    blocks = _layout(overlay)
    texts = [entry for entry in blocks if entry[0][:1].isdigit()]
    marks = [entry for entry in blocks if entry[0] in ("✓", "●", "○")]
    assert len(texts) == len(labels)
    step = next(entry for entry in texts if RESULT in entry[0])
    # The result is the second line of the step's own block: same left edge
    # as the step's first line, which is the left edge of every step text.
    assert len(step[2]) == 2 and step[2][1] == step[2][0]
    assert {entry[1] for entry in texts} == {step[1]}
    # Negative twin: the marks stand in their own column, left of the text,
    # where the result line used to start.
    assert marks and all(mark[1] < step[1] for mark in marks)


def test_a_step_without_result_takes_one_line_and_plain_text_keeps_mark_and_step_together(overlay):
    overlay.start("Generazione modello STEP", abortable=True)
    overlay.set_steps(["Confronto con i consolidati", "Modelli dalla libreria KiCad"])
    overlay.set_current_step(1)
    assert all(len(entry[2]) == 1 for entry in _layout(overlay))
    assert overlay.steps_plain_text().splitlines() == [
        "✓ 1 · Confronto con i consolidati", "● 2 · Modelli dalla libreria KiCad"]
    overlay.set_step_result(0, RESULT)
    assert overlay.steps_plain_text().splitlines()[1] == f"    {RESULT}"
    # A single-phase operation after it: no step row, no stale text.
    overlay.clear_steps()
    assert overlay.steps_plain_text() == ""


def test_an_unavailable_step_keeps_its_reason_in_the_text_column(overlay):
    overlay.start("Generazione footprint e modello STEP")
    overlay.set_steps(["Footprint 2D (KiCadGen)", "Solido 3D con FreeCAD"],
                      unavailable={1: "FreeCAD non installato"})
    blocks = _layout(overlay)
    struck = next(entry for entry in blocks if "FreeCAD non installato" in entry[0])
    first = next(entry for entry in blocks if entry[0].startswith("1 ·"))
    assert struck[1] == first[1]
    assert overlay.steps_plain_text().splitlines()[1] == "2 · Solido 3D con FreeCAD (FreeCAD non installato)"
