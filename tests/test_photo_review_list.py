"""@brief Sidebar list, parameter modal and selection highlight of PhotoReviewDialog.

Real offscreen Qt interaction tests: the list must dominate the sidebar with
a live count above it, the parameter form lives in a non-blocking modal, and
list/outline selection is mirrored both ways with a high-contrast highlight.
"""

from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from kicadgen.gui.photo_review_dialog import (
    PhotoReviewDialog, SELECTED_PEN_WIDTH_PX, SELECTION_HALO_COLOR,
    SELECTION_HALO_WIDTH_PX, UNSELECTED_PEN_WIDTH_PX,
)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


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


@pytest.fixture
def components():
    return [
        dict(name="U1", kind="ic", x_mm=-5.0, y_mm=2.0,
             length_mm=8.0, width_mm=2.0, height_mm=1.5, rotation_deg=90.0,
             confidence="high"),
        dict(name="R1", kind="resistor", x_mm=8.0, y_mm=-3.0,
             length_mm=3.0, width_mm=1.0, height_mm=.6, rotation_deg=0.0,
             confidence="low", _uncertain=True, marking="100"),
        dict(name="C1", kind="capacitor", x_mm=-14.0, y_mm=-6.0,
             length_mm=2.0, width_mm=1.2, height_mm=.9, rotation_deg=0.0,
             confidence="high"),
    ]


@pytest.fixture
def dialog(app, png, components):
    widget = PhotoReviewDialog(png, components, 40, 20)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


def _highlighted(dialog):
    """Indices of outlines currently drawn with the selection halo."""
    return [index for index, item in enumerate(dialog._items) if item.halo.isVisible()]


# --------------------------------------------------------------------------
# 1. Component count above the list, list dominating the sidebar
# --------------------------------------------------------------------------

def test_count_label_sits_above_the_list_and_follows_add_and_delete(dialog, app):
    """@brief "Componenti: N" is above the list and tracks the working inventory."""
    assert dialog.count_label.text() == "Componenti: 3"
    assert dialog.count_label.geometry().bottom() <= dialog.component_list.geometry().top()
    assert dialog.count_label.parentWidget() is dialog.component_list.parentWidget()
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert dialog.count_label.text() == "Componenti: 4"
    assert dialog.component_list.count() == 4
    dialog.component_list.setCurrentRow(0)
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    assert dialog.count_label.text() == "Componenti: 2"
    assert dialog.component_list.count() == 2


def test_empty_inventory_counts_zero(app, png):
    """@brief Negative: no outlines means "Componenti: 0", not a stale or missing label."""
    widget = PhotoReviewDialog(png, [], 40, 20)
    try:
        assert widget.count_label.text() == "Componenti: 0"
        assert widget.component_list.count() == 0
        widget.add_component()
        assert widget.count_label.text() == "Componenti: 1"
    finally:
        widget.close()


def test_list_takes_the_sidebar_vertical_space(dialog, app):
    """@brief The list is the sidebar's main content: the form no longer competes with it.

    The parameter form is not in the sidebar at all, and the list is taller
    than every other sidebar row put together, so a long inventory is
    readable without scrolling through a few entries at a time.
    """
    sidebar = dialog.component_list.parentWidget()
    assert dialog.form_widget.parentWidget() is not sidebar
    assert not sidebar.isAncestorOf(dialog.form_widget)
    others = [child for child in sidebar.findChildren(type(dialog.count_label))
              if child.parentWidget() is sidebar and child is not dialog.component_list]
    others += [dialog.parameters_button, dialog.model_button]
    assert dialog.component_list.height() > sum(child.height() for child in others)


# --------------------------------------------------------------------------
# 2. Parameter modal
# --------------------------------------------------------------------------

def test_parameters_open_in_a_modal_that_edits_the_outline_live(dialog, app):
    """@brief The button opens a modal holding the same form; edits show at once."""
    assert not dialog.parameters_dialog.isVisible()
    assert dialog.form_widget.window() is dialog.parameters_dialog
    dialog.component_list.setCurrentRow(1)
    QTest.mouseClick(dialog.parameters_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert dialog.parameters_dialog.isVisible()
    assert dialog.parameters_dialog.isModal()
    assert "2. R1" in dialog.parameters_dialog.windowTitle()
    assert dialog.form_widget.isVisible()
    assert dialog.fields["length_mm"].value() == 3
    dialog.fields["length_mm"].setValue(5)
    assert dialog._items[1].photo_bounds().width() == pytest.approx(50)
    assert dialog.get_components()[1]["length_mm"] == 5
    # OK commits text still being typed, then closes the modal.
    dialog.text_fields["name"].setText("R1_edited")
    dialog.parameters_buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    app.processEvents()
    assert not dialog.parameters_dialog.isVisible()
    assert dialog.get_components()[1]["name"] == "R1_edited"
    assert dialog.component_list.item(1).text().startswith("2. R1_edited")


def test_fields_work_without_the_modal_ever_being_shown(dialog):
    """@brief Headless callers drive the very same widgets; no modal is required."""
    assert not dialog.parameters_dialog.isVisible()
    dialog.component_list.setCurrentRow(2)
    dialog.fields["width_mm"].setValue(2.4)
    dialog.text_fields["kind"].setText("inductor")
    dialog.text_fields["kind"].editingFinished.emit()
    assert not dialog.parameters_dialog.isVisible()
    actual = dialog.get_components()[2]
    assert (actual["width_mm"], actual["kind"]) == (2.4, "inductor")
    assert dialog._items[2].photo_bounds().height() == pytest.approx(12)


def test_escape_closes_the_modal_and_commits_like_a_focus_change(dialog, app):
    """@brief The modal is a live editor, not a transaction: Escape is not an undo.

    Numeric edits were already applied while typing, so pending text is
    committed on Escape exactly as it would be on OK or on a focus change;
    the only way to discard is the editor's own Cancel.
    """
    dialog.component_list.setCurrentRow(0)
    dialog.open_parameters()
    app.processEvents()
    dialog.fields["height_mm"].setValue(4.0)
    dialog.text_fields["name"].setText("typed then escaped")
    QTest.keyClick(dialog.parameters_dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert not dialog.parameters_dialog.isVisible()
    actual = dialog.get_components()[0]
    assert actual["height_mm"] == 4.0
    assert actual["name"] == "typed then escaped"


def test_opening_and_closing_the_modal_without_edits_changes_nothing(dialog, app):
    """@brief Negative: the round trip through the modal is not itself an edit."""
    before = dialog.get_components()
    dialog.component_list.setCurrentRow(2)
    dialog.open_parameters()
    app.processEvents()
    dialog.parameters_buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    app.processEvents()
    dialog.open_parameters()
    app.processEvents()
    QTest.keyClick(dialog.parameters_dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert dialog.get_components() == before


def test_parameters_need_a_selection(app, png):
    """@brief Negative: nothing selected means no modal and a disabled button."""
    widget = PhotoReviewDialog(png, [], 40, 20)
    try:
        widget.show()
        app.processEvents()
        assert not widget.parameters_button.isEnabled()
        widget.open_parameters()
        app.processEvents()
        assert not widget.parameters_dialog.isVisible()
    finally:
        widget.close()
        app.processEvents()


def test_adding_a_component_opens_the_modal_on_its_name(dialog, app):
    """@brief A new outline needs a name: the modal follows with the name selected."""
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert dialog.parameters_dialog.isVisible()
    assert "4." in dialog.parameters_dialog.windowTitle()
    assert dialog.text_fields["name"].selectedText() == dialog.text_fields["name"].text()
    QTest.keyClicks(dialog.text_fields["name"], "J3")
    dialog.parameters_buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    app.processEvents()
    assert dialog.get_components()[3]["name"] == "J3"


def test_closing_the_editor_takes_the_modal_down(dialog, app):
    """@brief The modal never outlives the editor, whichever way it is closed."""
    dialog.open_parameters()
    app.processEvents()
    assert dialog.parameters_dialog.isVisible()
    dialog.text_fields["marking"].setText("typed at close")
    dialog.reject()
    app.processEvents()
    assert not dialog.parameters_dialog.isVisible()
    assert dialog.parameters_dialog.focusWidget() is None or not dialog.parameters_dialog.focusWidget().hasFocus()


# --------------------------------------------------------------------------
# 3. Selection highlight, both directions
# --------------------------------------------------------------------------

def test_selecting_a_row_highlights_that_outline_only(dialog):
    """@brief Positive: row N gets the halo and thick pen; every other outline is plain."""
    for row in (2, 0, 1):
        dialog.component_list.setCurrentRow(row)
        assert _highlighted(dialog) == [row]
        assert [item.isSelected() for item in dialog._items] == [i == row for i in range(3)]
        selected = dialog._items[row]
        assert selected.pen().widthF() == SELECTED_PEN_WIDTH_PX
        assert selected.pen().style() == Qt.PenStyle.SolidLine
        assert selected.halo.pen().color().name() == QColor(SELECTION_HALO_COLOR).name()
        assert selected.halo.pen().widthF() == SELECTION_HALO_WIDTH_PX > SELECTED_PEN_WIDTH_PX
        assert selected.halo.polygon() == selected.polygon()
        for other in dialog._items:
            if other is not selected:
                assert other.pen().widthF() == UNSELECTED_PEN_WIDTH_PX
    # The halo carries no status: the status colour stays on the outline's own pen.
    assert dialog._items[1].pen().color().name() == "#ffad33"


def test_deselecting_leaves_no_highlighted_outline(dialog):
    """@brief Negative: clearing the list selection clears every halo and thick pen."""
    dialog.component_list.setCurrentRow(1)
    assert _highlighted(dialog) == [1]
    dialog.component_list.setCurrentRow(-1)
    assert _highlighted(dialog) == []
    assert not any(item.isSelected() for item in dialog._items)
    assert all(item.pen().widthF() == UNSELECTED_PEN_WIDTH_PX for item in dialog._items)
    assert dialog.component_list.currentRow() == -1


def test_selecting_a_row_centres_the_view_on_the_outline(dialog, app):
    """@brief A zoomed-in view is re-centred, not merely scrolled until the outline is visible."""
    dialog.view.zoom(4)
    app.processEvents()
    for row in (2, 1):
        dialog.component_list.setCurrentRow(row)
        app.processEvents()
        centre = dialog.view.viewport().rect().center()
        on_screen = dialog.view.mapFromScene(dialog._items[row].scenePos())
        # Scrollbars are integer: centerOn can be off by at most one device pixel per axis.
        assert abs(on_screen.x() - centre.x()) <= 1
        assert abs(on_screen.y() - centre.y()) <= 1


def test_clicking_an_outline_selects_its_row_and_highlights_it(dialog, app):
    """@brief Reverse direction: a click in the photo selects the list row and only that outline."""
    dialog.view.zoom(3)
    app.processEvents()
    dialog.component_list.setCurrentRow(0)
    for row in (1, 2):
        item = dialog._items[row]
        before = deepcopy(item.component)
        # Center the target first so the click point is inside the viewport.
        dialog.view.centerOn(item)
        app.processEvents()
        QTest.mouseClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, dialog.view.mapFromScene(item.scenePos()))
        app.processEvents()
        assert dialog.component_list.currentRow() == row
        assert _highlighted(dialog) == [row]
        assert item.component == before
        assert dialog.fields["length_mm"].value() == before["length_mm"]


def test_double_clicking_an_outline_keeps_the_viewport_still(dialog, app, monkeypatch):
    """@brief Negative: the outline under the pointer is not re-centred, only selected."""
    from kicadgen.gui import photo_review_dialog as module

    class _Idle(module.QThread):
        """@brief Stand-in worker: the model chooser must not spin a real thread here."""

        done = module.pyqtSignal(object)
        failed = module.pyqtSignal(str)

        def __init__(self, snapshot):
            super().__init__()

        def start(self):
            return None

    monkeypatch.setattr(module, '_MatchWorker', _Idle)
    dialog.view.zoom(4)
    dialog.component_list.setCurrentRow(0)
    app.processEvents()
    item = dialog._items[1]
    dialog.view.centerOn(item)
    app.processEvents()
    dialog.view.horizontalScrollBar().setValue(dialog.view.horizontalScrollBar().value() + 40)
    scroll = (dialog.view.horizontalScrollBar().value(), dialog.view.verticalScrollBar().value())
    QTest.mouseDClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier, dialog.view.mapFromScene(item.scenePos()))
    app.processEvents()
    assert dialog.component_list.currentRow() == 1
    assert _highlighted(dialog) == [1]
    assert (dialog.view.horizontalScrollBar().value(), dialog.view.verticalScrollBar().value()) == scroll
