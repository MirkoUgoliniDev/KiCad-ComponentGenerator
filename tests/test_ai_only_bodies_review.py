"""@brief AI-only bodies in the photo review: proposed, not selected, kept by hand (HANDOFF 141).

A body the vision model added with no detector proposal behind it arrives
with ``_ai_only_excluded`` True. The review shows it weak and dashed, like
the weak proposals of «Corpi rilevati», and it stays out of what Apply emits
until the user keeps it (key T, or any edit of it). Every positive case has a
negative one that differs by structure: a body with a detector proposal
behind it (no ``_ai_only_excluded`` key) is never touched by these rules.
"""

from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest

from kicadgen.gui.photo_review_dialog import (
    AI_ONLY_EXCLUDED_COLOR, PhotoReviewDialog, ai_only_excluded, ai_only_proposal,
)


@pytest.fixture
def png(app):
    # 10 px/mm along X, 5 px/mm along Y for a 40 x 20 mm PCB.
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#243b28"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _components():
    """U1 has a detector proposal behind it; L1 was added by the model alone."""
    return [
        dict(name="U1", kind="ic", x_mm=-5.0, y_mm=2.0, length_mm=8.0, width_mm=4.0,
             height_mm=1.5, rotation_deg=0.0, confidence="high", source_candidate_ids=[3]),
        dict(name="L1", kind="inductor", x_mm=12.0, y_mm=-4.0, length_mm=3.0, width_mm=3.0,
             height_mm=1.2, rotation_deg=0.0, confidence="medium", source_candidate_ids=[],
             _ai_only_excluded=True,
             _review_reason="added by the model with no detector proposal"),
    ]


@pytest.fixture
def dialog(app, png):
    widget = PhotoReviewDialog(png, _components(), 40, 20)
    widget.show()
    app.processEvents()
    yield widget
    widget.reject()
    widget.close()


def _applied(dialog):
    emitted = []
    dialog.components_applied.connect(emitted.append)
    dialog.accept()
    assert len(emitted) == 1
    return emitted[0]


def test_an_excluded_ai_only_body_is_drawn_weak_and_dashed(dialog):
    """Positive: dashed, in the weak tint, marked in the list and counted in the summary.
    Negative: the detector-backed body keeps its solid status outline."""
    ai_item, detected = dialog._items[1], dialog._items[0]
    assert ai_item.pen().style() == Qt.PenStyle.DashLine
    assert ai_item.pen().color().name() == QColor(AI_ONLY_EXCLUDED_COLOR).name()
    assert detected.pen().style() == Qt.PenStyle.SolidLine
    assert detected.pen().color().name() != QColor(AI_ONLY_EXCLUDED_COLOR).name()
    assert "[escluso]" in dialog.component_list.item(1).text()
    assert "[escluso]" not in dialog.component_list.item(0).text()
    assert dialog.ai_only_label.isVisible()
    assert "1" in dialog.ai_only_label.text() and "fuori" in dialog.ai_only_label.text()
    # The legend explains the dashed outline only because one is on screen.
    assert dialog.legend.text().count("&#9679;") == 5


def test_without_ai_only_bodies_nothing_new_is_shown(app, png):
    """Negative: an inventory whose bodies all have a detector proposal shows
    no summary, no fifth legend entry, and «Tieni» stays disabled."""
    components = [_components()[0]]
    widget = PhotoReviewDialog(png, components, 40, 20)
    try:
        widget.show()
        app.processEvents()
        assert not widget.ai_only_label.isVisible()
        assert widget.legend.text().count("&#9679;") == 4
        widget._select_row(0)
        assert not widget.keep_button.isEnabled()
        assert widget.toggle_keep() is None
        assert "_ai_only_excluded" not in widget.get_components()[0]
    finally:
        widget.reject()
        widget.close()


def test_apply_emits_only_the_kept_bodies(dialog):
    """The body nobody kept does not reach the table: Apply leaves it out and
    it stays readable through left_out_components."""
    emitted = _applied(dialog)
    assert [c["name"] for c in emitted] == ["U1"]
    assert [c["name"] for c in dialog.left_out_components()] == ["L1"]
    # The working copy still has both: nothing is deleted by the review itself.
    assert [c["name"] for c in dialog.get_components()] == ["U1", "L1"]


def test_key_t_keeps_the_selected_ai_only_body_and_apply_carries_it(dialog, app):
    dialog._select_row(1)
    assert dialog.keep_button.isEnabled() and not dialog.keep_button.isChecked()
    dialog.activateWindow()
    QTest.keyClick(dialog, Qt.Key.Key_T)
    app.processEvents()
    component = dialog.get_components()[1]
    assert ai_only_proposal(component) and not ai_only_excluded(component)
    assert dialog.keep_button.isChecked()
    assert dialog._items[1].pen().style() == Qt.PenStyle.SolidLine
    assert "[escluso]" not in dialog.component_list.item(1).text()
    assert [c["name"] for c in _applied(dialog)] == ["U1", "L1"]
    assert dialog.left_out_components() == []


def test_t_again_leaves_it_out_again(dialog):
    """The choice is reversible, as in «Corpi rilevati»."""
    assert dialog.toggle_keep(1) is True
    assert dialog.toggle_keep(1) is False
    assert ai_only_excluded(dialog.get_components()[1])
    assert [c["name"] for c in _applied(dialog)] == ["U1"]


def test_moving_an_ai_only_body_keeps_it(dialog, app):
    """An edit is a look (as mark_manual in the detector review)."""
    item = dialog._items[1]
    item.setPos(item.pos().x() + 20, item.pos().y())
    app.processEvents()
    assert not ai_only_excluded(dialog.get_components()[1])
    assert [c["name"] for c in _applied(dialog)] == ["U1", "L1"]


def test_editing_a_parameter_keeps_it(dialog):
    dialog._select_row(1)
    dialog.fields["height_mm"].setValue(2.5)
    assert not ai_only_excluded(dialog.get_components()[1])


def test_selecting_it_without_editing_does_not_keep_it(dialog):
    """Negative: looking at the list or clicking the outline is not a decision."""
    dialog._select_row(1)
    dialog._select_row(0)
    dialog._commit_pending_text()
    assert ai_only_excluded(dialog.get_components()[1])
    assert [c["name"] for c in _applied(dialog)] == ["U1"]


def test_the_callers_list_is_never_touched(app, png):
    components = _components()
    snapshot = deepcopy(components)
    widget = PhotoReviewDialog(png, components, 40, 20)
    try:
        widget.toggle_keep(1)
        widget.accept()
        assert components == snapshot
    finally:
        widget.close()
