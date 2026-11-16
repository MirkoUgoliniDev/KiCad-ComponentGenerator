"""@brief A step's result line starts under the step's text, never under its mark (user, 2026-11-15).

The user saw «3 trovati · 31 non trovati · 12 da verificare» start below the
«✓» of step 2 and stick out of the column, on the generation card and on the
STEP regeneration card: the result was a line break inside the cell that also
held the mark. The marks now have a column of their own. Checked on the
document the label really draws (positions laid out by Qt), not on its text.
"""
import html

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QTextDocument  # noqa: E402
from PyQt6.QtWidgets import QWidget  # noqa: E402

from kicadgen.gui.analysis_overlay import AnalysisOverlay  # noqa: E402

RESULT = "3 trovati · 31 non trovati · 12 da verificare"
#: The step marks the label draws: done, running, waiting.
MARKS = ("✓", "●", "○")


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
    return _layout_of(_document(card.steps.text()))


def _layout_of(document):
    """@brief (text, x of the block, x of each of its lines) of every block of a laid-out document."""
    layout = document.documentLayout()
    blocks, block = [], document.begin()
    while block.isValid():
        lines = block.layout()
        blocks.append((block.text(), layout.blockBoundingRect(block).x(),
                       [lines.lineAt(i).x() for i in range(lines.lineCount())]))
        block = block.next()
    return blocks


def _document(markup):
    """@brief The document Qt lays out for `markup`, at the card's width, laid out now."""
    document = QTextDocument()
    document.setHtml(markup)
    document.setTextWidth(900)
    # Lay out before any block layout is read: an unlaid block crashes Qt.
    document.documentLayout()
    document.size()
    return document


def _x(document, cursor, position):
    """@brief ABSOLUTE x on the page of a text position, from the lines Qt laid out.

    @param position Document position inside the block of `cursor`.
    @note blockBoundingRect places the block (the table cell), cursorToX the
        character inside it, line offset included: the sum compares
        positions that lie in different cells.
    """
    block = cursor.block()
    offset = position - block.position()
    line = block.layout().lineForTextPosition(offset)
    return document.documentLayout().blockBoundingRect(block).x() + line.cursorToX(offset)[0]


def _alignment(document, step_text):
    """@brief (x where `step_text` starts, x where RESULT starts, right edge of the rightmost mark)."""
    text = document.find(step_text)
    result = document.find(RESULT)
    assert not text.isNull() and not result.isNull()
    edges, cursor = [], None
    for mark in MARKS:
        cursor = document.find(mark)
        while not cursor.isNull():
            edges.append(_x(document, cursor, cursor.selectionEnd()))
            cursor = document.find(mark, cursor)
    assert edges
    return (_x(document, text, text.selectionStart()), _x(document, result, result.selectionStart()),
            max(edges))


def _old_markup(card):
    """@brief The step label as drawn before 2026-11-15: mark, text and result in ONE cell.

    Rebuilt here, from the card's own state, as the negative twin of the
    alignment check: the result's line break went back to the cell's left
    edge, under the mark.
    """
    parts = []
    for index, label in enumerate(card._step_labels):
        text = html.escape(f"{index + 1} · {label}")
        result = card._step_results.get(index)
        if result:
            text += '<br/>' + html.escape(result)
        if index < card._step_current:
            parts.append(f'<span style="color:#a6e3a1">&#10003; {text}</span>')
        elif index == card._step_current:
            parts.append(f'<span style="color:#f5e0dc; font-weight:600">&#9679; {text}</span>')
        else:
            parts.append(f'<span style="color:#a6adc8">&#9675; {text}</span>')
    rows = "".join(f"<tr><td style='padding:1px 0'>{part}</td></tr>" for part in parts)
    return f"<table align='center' cellspacing='0'>{rows}</table>"


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
    # The result is the second line of the step's own block.
    assert len(step[2]) == 2
    assert {entry[1] for entry in texts} == {step[1]}
    assert marks and all(mark[1] < step[1] for mark in marks)
    # CONTRACT CHANGED 2026-11-15: the old check compared the two lines' x
    # inside their own block, which the one-cell layout also passed (both
    # 0.0). Now ABSOLUTE positions on the page: the result starts exactly
    # where its step's text starts, right of every mark.
    text_x, result_x, marks_right = _alignment(_document(overlay.steps.text()),
                                               f"{with_result + 1} · {labels[with_result]}")
    assert result_x == pytest.approx(text_x, abs=0.5)
    assert result_x > marks_right


@pytest.mark.parametrize("labels, with_result, running", [
    (["Footprint 2D (KiCadGen)", "Confronto con i consolidati", "Solido 3D con FreeCAD"], 1, 2),
    (["Confronto con i consolidati", "Modelli dalla libreria KiCad"], 0, 1),
])
def test_the_old_one_cell_layout_fails_the_alignment_check(overlay, labels, with_result, running):
    """Negative twin: the check tells the old layout apart, which the old assertion did not."""
    overlay.start("Generazione modello STEP", abortable=True)
    overlay.set_steps(labels)
    overlay.set_step_result(with_result, RESULT)
    overlay.set_current_step(running)
    old = _document(_old_markup(overlay))
    # The old assertion passed on it: both lines of the block start at x 0.
    step = next(entry for entry in _layout_of(old) if RESULT in entry[0])
    assert len(step[2]) == 2 and step[2][1] == step[2][0]
    text_x, result_x, marks_right = _alignment(old, f"{with_result + 1} · {labels[with_result]}")
    # The new one does not: the result starts left of its text, under a mark.
    assert result_x < text_x - 0.5
    assert result_x < marks_right


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
