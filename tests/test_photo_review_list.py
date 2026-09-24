"""@brief Sidebar list, parameter modal and selection highlight of PhotoReviewDialog.

Real offscreen Qt interaction tests: the list must dominate the sidebar with
a live count above it, the parameter form lives in a window-modal dialog
opened with ``QDialog.open()`` (input-blocked, no nested event loop), and
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
    PHOTO_DIMMED, PHOTO_HIDDEN, PhotoReviewDialog, SELECTED_PEN_WIDTH_PX,
    SELECTION_HALO_COLOR, SELECTION_HALO_WIDTH_PX, UNDETERMINED_LIST_MARKER,
    UNDETERMINED_TYPE_COLOR, UNSELECTED_PEN_WIDTH_PX,
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
    """@brief "Componenti: N" is above the list and tracks the working inventory.

    Contract change: the toolbar button now arms the drawing mode, so the
    programmatic ``add_component()`` is what adds a body here. The label still
    has to follow every addition and deletion.
    """
    assert dialog.count_label.text() == "Componenti: 3"
    assert dialog.count_label.geometry().bottom() <= dialog.component_list.geometry().top()
    assert dialog.count_label.parentWidget() is dialog.component_list.parentWidget()
    dialog.add_component()
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
    others += [dialog.parameters_button, dialog.model_button, dialog.category_filter]
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
    """@brief A placeholder outline needs a name: the modal follows with it selected.

    Contract change: this is the programmatic path, where the body gets
    placeholder geometry and no name, so the modal asks for the name. A body
    drawn on the photo already has its geometry and is asked for its type
    instead (see tests/test_photo_review_dialog.py).
    """
    dialog.add_component()
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


def _click_outline(dialog, app, row, modifier):
    """Real press/release on the center of outline ``row`` with a keyboard modifier."""
    item = dialog._items[row]
    dialog.view.centerOn(item)
    app.processEvents()
    QTest.mouseClick(dialog.view.viewport(), Qt.MouseButton.LeftButton, modifier,
                     dialog.view.mapFromScene(item.scenePos()))
    app.processEvents()


def test_ctrl_click_selects_the_clicked_outline_not_the_lowest_index(dialog, app):
    """@brief Ctrl-click resolves to the outline under the pointer, like a plain click.

    Qt's default would extend the selection to two items and the editor,
    which edits one component, would stay bound to the lowest list index.
    Here the sidebar, the form and the halo must all follow the clicked row.
    """
    dialog.view.zoom(3)
    app.processEvents()
    dialog.component_list.setCurrentRow(0)
    before = deepcopy(dialog.get_components())
    _click_outline(dialog, app, 1, Qt.KeyboardModifier.ControlModifier)
    assert dialog.component_list.currentRow() == 1
    assert _highlighted(dialog) == [1]
    assert [item.isSelected() for item in dialog._items] == [False, True, False]
    assert dialog.fields["length_mm"].value() == before[1]["length_mm"]
    _click_outline(dialog, app, 2, Qt.KeyboardModifier.ControlModifier)
    assert dialog.component_list.currentRow() == 2
    assert _highlighted(dialog) == [2]
    assert dialog.get_components() == before


def test_ctrl_click_on_the_selected_outline_keeps_it_selected(dialog, app):
    """@brief Negative: Ctrl-click on the current outline is not a toggle that clears the selection."""
    dialog.view.zoom(3)
    app.processEvents()
    dialog.component_list.setCurrentRow(0)
    before = deepcopy(dialog.get_components())
    _click_outline(dialog, app, 0, Qt.KeyboardModifier.ControlModifier)
    assert dialog.component_list.currentRow() == 0
    assert _highlighted(dialog) == [0]
    assert dialog._items[0].isSelected()
    assert dialog.form_widget.isEnabled()
    assert dialog.get_components() == before


def test_minimize_and_restore_keep_the_parameter_modal(dialog, app):
    """@brief A spontaneous hide (minimize) must not close the modal for good.

    The window system hides the editor on minimize; closing the modal then
    would leave it hidden after ``showNormal()``. Only program-driven hides
    (close, reject) take the modal down.
    """
    dialog.component_list.setCurrentRow(1)
    dialog.open_parameters()
    app.processEvents()
    assert dialog.parameters_dialog.isVisible()
    dialog.showMinimized()
    app.processEvents()
    QTest.qWait(50)
    if not dialog.isMinimized():
        pytest.skip("this platform plugin cannot minimize a window")
    dialog.showNormal()
    app.processEvents()
    QTest.qWait(50)
    assert not dialog.isMinimized()
    assert dialog.isVisible()
    assert dialog.parameters_dialog.isVisible()
    assert dialog.parameters_dialog.isModal()
    # The modal is still live: an edit made after the round trip reaches the outline.
    dialog.fields["length_mm"].setValue(4.5)
    assert dialog.get_components()[1]["length_mm"] == 4.5


def test_program_driven_hide_still_takes_the_modal_down(dialog, app):
    """@brief Negative: a non-spontaneous hide (the editor's own hide/close) closes the modal."""
    dialog.component_list.setCurrentRow(1)
    dialog.open_parameters()
    app.processEvents()
    assert dialog.parameters_dialog.isVisible()
    dialog.hide()
    app.processEvents()
    assert not dialog.parameters_dialog.isVisible()


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


# --------------------------------------------------------------------------
# 5. Category filter above the list
# --------------------------------------------------------------------------

def _visible_rows(dialog):
    """Row indices the list actually shows, in order."""
    return [index for index in range(dialog.component_list.count())
            if not dialog.component_list.item(index).isHidden()]


def _select_category(dialog, category):
    """Choose a category in the filter combo by its visible text."""
    index = dialog.category_filter.findText(category)
    assert index >= 0, f"missing category: {category}"
    dialog.category_filter.setCurrentIndex(index)


def test_category_filter_sits_above_the_list_with_the_categories_of_the_inventory(dialog):
    """@brief The combo is above the list and offers "all" plus the kinds present.

    The vocabulary is derived from the components themselves: nothing is
    hard-coded, so an inventory of connectors offers connectors.
    """
    combo = dialog.category_filter
    assert combo.objectName() == "category_filter"
    assert combo.parentWidget() is dialog.component_list.parentWidget()
    assert combo.geometry().bottom() <= dialog.component_list.geometry().top()
    assert combo.count() == 4
    assert combo.itemText(0) == "Tutte le categorie"
    assert combo.itemData(0) is None
    assert [combo.itemText(i) for i in range(1, 4)] == ["capacitor", "ic", "resistor"]
    assert dialog.active_category() is None
    assert _visible_rows(dialog) == [0, 1, 2]


def test_categories_are_deduplicated_case_insensitively_without_renaming(app, png):
    """@brief "Resistor" and "resistor" are one category, spelled as first met.

    Negative side: the editor must not rewrite what the detection wrote, so
    the component keeps its own spelling of ``kind`` and both rows match the
    single entry.
    """
    inventory = [
        dict(name="R1", kind="resistor", x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="R2", kind="Resistor", x_mm=4, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="U1", kind="ic", x_mm=-8, y_mm=0, length_mm=4, width_mm=4, height_mm=1),
    ]
    widget = PhotoReviewDialog(png, inventory, 40, 20)
    try:
        assert [widget.category_filter.itemText(i)
                for i in range(widget.category_filter.count())] == [
            "Tutte le categorie", "ic", "resistor"]
        _select_category(widget, "resistor")
        assert _visible_rows(widget) == [0, 1]
        assert widget.count_label.text() == "Componenti: 2 di 3"
        assert [entry["kind"] for entry in widget.get_components()] == ["resistor", "Resistor", "ic"]
    finally:
        widget.close()


def test_a_component_without_a_kind_adds_no_category_and_matches_none(app, png):
    """@brief Negative: a body with no declared type is not invented a category.

    It stays reachable with no filter and through its rectangle on the photo,
    but no category claims it.
    """
    inventory = [
        dict(name="R1", kind="resistor", x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="?", kind="   ", x_mm=6, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
    ]
    widget = PhotoReviewDialog(png, inventory, 40, 20)
    try:
        assert [widget.category_filter.itemText(i)
                for i in range(widget.category_filter.count())] == [
            "Tutte le categorie", "resistor"]
        widget.component_list.setCurrentRow(0)
        _select_category(widget, "resistor")
        assert _visible_rows(widget) == [0]
        assert widget.count_label.text() == "Componenti: 1 di 2"
    finally:
        widget.close()


def test_a_kind_that_is_not_a_string_declares_no_category(app, png):
    """@brief Negative: only a string is a spelling a detector could have written.

    ``str()`` on the raw value published "['diode']" or "True" as categories in
    the combo and in the type completer — a vocabulary no detection ever wrote,
    which is the opposite of taking it from the components themselves. Such a
    body simply declares no category, exactly like one with no ``kind`` at all,
    and the stored value is left untouched for whoever wrote it.
    """
    inventory = [
        dict(name="R1", kind="resistor", x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="D1", kind=["diode"], x_mm=6, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="Q1", kind=True, x_mm=-6, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="L1", kind=0, x_mm=-12, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
    ]
    widget = PhotoReviewDialog(png, inventory, 40, 20)
    try:
        assert [widget.category_filter.itemText(i)
                for i in range(widget.category_filter.count())] == [
            "Tutte le categorie", "resistor"]
        assert widget.kind_completer.model().stringList() == ["resistor"]
        # Nothing was rewritten: the inventory still holds what arrived.
        assert [entry["kind"] for entry in widget.get_components()] == [
            "resistor", ["diode"], True, 0]
        _select_category(widget, "resistor")
        assert _visible_rows(widget) == [0], "no non-string kind matches a category"
        assert widget.count_label.text() == "Componenti: 1 di 4"
    finally:
        widget.close()


def test_surrounding_whitespace_is_the_only_liberty_taken_with_a_spelling(app, png):
    """@brief Positive: " Resistor " and "resistor" are one category, spelled as first met.

    Trimming is what keeps two visually identical entries from both appearing;
    it is the only rewriting the vocabulary does, and the components keep their
    own spelling, padding included.
    """
    inventory = [
        dict(name="R1", kind="  Resistor  ", x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
        dict(name="R2", kind="resistor", x_mm=6, y_mm=0, length_mm=2, width_mm=1, height_mm=.5),
    ]
    widget = PhotoReviewDialog(png, inventory, 40, 20)
    try:
        assert [widget.category_filter.itemText(i)
                for i in range(widget.category_filter.count())] == [
            "Tutte le categorie", "Resistor"]
        assert [entry["kind"] for entry in widget.get_components()] == [
            "  Resistor  ", "resistor"]
        _select_category(widget, "Resistor")
        assert _visible_rows(widget) == [0, 1]
        assert widget.count_label.text() == "Componenti: 2 di 2"
    finally:
        widget.close()


def test_a_row_outline_desync_fails_loudly_instead_of_hiding_the_wrong_rows(dialog):
    """@brief Negative: index alignment is an invariant, not a case to tolerate.

    Rows and outlines are created and destroyed together, so a missing row can
    only mean the two have drifted apart. Filtering used to skip it in silence
    and hide rows by a stale index, while ``_refresh_item`` already raised on
    exactly the same state; one of the two had to change, and a broken
    invariant must be the loud one. The positive side follows: once the row is
    back, filtering works again.
    """
    orphan = dialog.component_list.takeItem(2)
    try:
        with pytest.raises(AssertionError, match="index-aligned"):
            dialog._apply_filter()
        with pytest.raises(AttributeError):
            dialog._refresh_item(dialog._items[2])
    finally:
        dialog.component_list.insertItem(2, orphan)
    dialog._apply_filter()
    assert _visible_rows(dialog) == [0, 1, 2]


def test_selecting_a_category_hides_the_other_rows_and_states_both_counts(dialog, app):
    """@brief The filter hides rows and the count says how many of how many."""
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "resistor")
    assert dialog.active_category() == "resistor"
    assert _visible_rows(dialog) == [1]
    assert dialog.count_label.text() == "Componenti: 1 di 3"
    # Row indices and texts are untouched: rows are hidden, not rebuilt.
    assert dialog.component_list.count() == 3
    assert dialog.component_list.item(2).text().startswith("3. C1")
    assert [entry["name"] for entry in dialog.get_components()] == ["U1", "R1", "C1"]
    # Dal 2026-09-24 il filtro filtra anche la foto (decisione dell'utente):
    # si vede solo il rettangolo della categoria.
    assert [item.isVisible() for item in dialog._items] == [False, True, False]


def test_clearing_the_filter_shows_every_row_and_the_plain_count(dialog):
    """@brief Negative: with no category the label is back to one number."""
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "resistor")
    assert dialog.count_label.text() == "Componenti: 1 di 3"
    dialog.category_filter.setCurrentIndex(0)
    assert dialog.active_category() is None
    assert _visible_rows(dialog) == [0, 1, 2]
    assert dialog.count_label.text() == "Componenti: 3"


def test_changing_the_filter_deselects_a_component_outside_the_category(dialog, app):
    """@brief Contratto cambiato di proposito il 2026-09-24 (decisione dell'utente).

    Prima il filtro non deselezionava mai e lasciava visibile la riga scelta
    fuori categoria (il «1. SW1 (switch)» in cima alla lista filtrata dello
    screenshot dell'utente). Ora il filtro nasconde anche i rettangoli, e una
    scelta fuori categoria sarebbe l'unica riga e l'unico rettangolo estranei:
    cambiando il filtro la scelta si toglie, quindi non resta nulla di
    invisibile da modificare. Negativo: una scelta DENTRO la categoria resta.
    """
    dialog.component_list.setCurrentRow(0)
    _select_category(dialog, "resistor")
    assert not dialog._items[0].isSelected()
    assert _visible_rows(dialog) == [1]
    assert not dialog._items[0].isVisible()
    assert not dialog.delete_button.isEnabled()
    dialog.category_filter.setCurrentIndex(0)
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "resistor")
    assert dialog._items[1].isSelected() and _visible_rows(dialog) == [1]


def test_a_component_whose_type_is_edited_out_of_the_filter_stays_visible(dialog, app):
    """@brief Si sta lavorando su di lui: ritoccarne il tipo non lo fa sparire.

    La deselezione scatta solo quando si CAMBIA il filtro.
    """
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "resistor")
    dialog.text_fields["kind"].setText("capacitor")
    dialog.text_fields["kind"].editingFinished.emit()
    assert dialog._items[1].isSelected()
    assert dialog._items[1].isVisible()
    assert not dialog.component_list.item(1).isHidden()


def test_a_rectangle_outside_the_category_cannot_be_clicked(dialog, app):
    """@brief Il filtro filtra la foto: un rettangolo nascosto non prende il clic.

    Positivo: il rettangolo della categoria si sceglie col clic.
    """
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "resistor")
    assert not dialog._items[2].isVisible()
    dialog.view.zoom(3)
    _click_outline(dialog, app, 2, Qt.KeyboardModifier.NoModifier)
    assert not dialog._items[2].isSelected()
    dialog.scene.clearSelection()
    _click_outline(dialog, app, 1, Qt.KeyboardModifier.NoModifier)
    assert dialog._items[1].isSelected()


def test_the_filter_follows_added_and_deleted_categories(dialog, app):
    """@brief A new category appears, a vanished one falls back to "all categories".

    Keeping a filter on a category no longer present would hide the whole
    list with nothing explaining why; keeping a filter that still has
    components must instead survive an unrelated addition.
    """
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "capacitor")
    assert dialog.active_category() == "capacitor"
    # A new body arrives with its own type: the vocabulary grows, the filter stays.
    dialog.add_component()
    dialog.text_fields["kind"].setText("connector")
    dialog.text_fields["kind"].editingFinished.emit()
    assert "connector" in [dialog.category_filter.itemText(i)
                           for i in range(dialog.category_filter.count())]
    assert dialog.active_category() == "capacitor"
    # Deleting the only capacitor retires its category and clears the filter.
    dialog.component_list.setCurrentRow(2)
    dialog.delete_selected()
    assert "capacitor" not in [dialog.category_filter.itemText(i)
                               for i in range(dialog.category_filter.count())]
    assert dialog.active_category() is None
    assert _visible_rows(dialog) == [0, 1, 2]
    assert dialog.count_label.text() == "Componenti: 3"


def test_retyping_the_kind_of_the_last_member_retires_its_category(dialog):
    """@brief Negative: a category with no component left must not stay in the combo."""
    dialog.component_list.setCurrentRow(2)
    _select_category(dialog, "capacitor")
    assert _visible_rows(dialog) == [2]
    dialog.text_fields["kind"].setText("tantalum-capacitor")
    dialog.text_fields["kind"].editingFinished.emit()
    categories = [dialog.category_filter.itemText(i)
                  for i in range(dialog.category_filter.count())]
    assert "capacitor" not in categories
    assert "tantalum-capacitor" in categories
    assert dialog.active_category() is None
    assert _visible_rows(dialog) == [0, 1, 2]


# --------------------------------------------------------------------------
# 5. Sidebar and list with the photograph attenuated or taken away
# --------------------------------------------------------------------------

def test_the_list_and_the_outlines_stay_in_step_with_the_photograph_hidden(dialog, app):
    """@brief Selection keeps travelling both ways when the canvas shows no photo.

    Taking the photograph away is a view setting: choosing a row must still
    highlight its outline, and clicking an outline must still make its row
    current, halo included.
    """
    dialog.set_photo_state(PHOTO_HIDDEN)
    dialog.view.zoom(3)
    app.processEvents()
    for row in (2, 0, 1):
        dialog.component_list.setCurrentRow(row)
        assert _highlighted(dialog) == [row]
        assert dialog.text_fields["name"].text() == dialog._components[row]["name"]
    for row in (0, 2):
        item = dialog._items[row]
        dialog.view.centerOn(item)
        app.processEvents()
        QTest.mouseClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, dialog.view.mapFromScene(item.scenePos()))
        app.processEvents()
        assert dialog.component_list.currentRow() == row
        assert _highlighted(dialog) == [row]
    assert dialog.photo_state() == PHOTO_HIDDEN


def test_the_category_filter_and_the_backdrop_are_independent(dialog, app):
    """@brief Negative: neither control resets the other.

    The filter hides rows and rectangles, the backdrop control hides the
    photograph; engaging one never changes the other.
    """
    dialog.set_photo_state(PHOTO_DIMMED)
    dialog.component_list.setCurrentRow(1)
    _select_category(dialog, "resistor")
    assert _visible_rows(dialog) == [1]
    assert dialog.count_label.text() == "Componenti: 1 di 3"
    assert dialog.photo_state() == PHOTO_DIMMED
    dialog.set_photo_state(PHOTO_HIDDEN)
    assert dialog.active_category() == "resistor"
    assert _visible_rows(dialog) == [1]
    assert [item.isVisible() for item in dialog._items] == [False, True, False]
    dialog.category_filter.setCurrentIndex(0)
    assert dialog.photo_state() == PHOTO_HIDDEN
    assert all(item.isVisible() for item in dialog._items)


def test_the_parameter_modal_edits_live_with_the_photograph_hidden(dialog, app):
    """@brief The modal keeps driving the outline, and does not restore the photograph."""
    dialog.set_photo_state(PHOTO_HIDDEN)
    dialog.component_list.setCurrentRow(1)
    item = dialog._items[1]
    QTest.mouseClick(dialog.parameters_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert dialog.parameters_dialog.isVisible()
    assert dialog.photo_state() == PHOTO_HIDDEN
    assert not dialog.photo_item.isVisible()
    before = item.photo_bounds()
    dialog.fields["length_mm"].setValue(6)
    assert item.photo_bounds().width() > before.width()
    assert dialog.get_components()[1]["length_mm"] == 6
    dialog.parameters_dialog.accept()
    app.processEvents()
    assert dialog.photo_state() == PHOTO_HIDDEN
    assert not dialog.photo_item.isVisible()


# --------------------------------------------------------------------------
# 8. The list row of a body the model declined to type
#
# The row already showed "N. NAME [!]", where "[!]" only means "this row has
# at least one warning". Before a CAD model is chosen that is true of nearly
# every row, so "[!]" could not single out the one body worth choosing a model
# for by hand. It needs a marker of its own.
# --------------------------------------------------------------------------

def _undetermined_component(name, **overrides):
    """A body the global inventory left without a family, as it writes it."""
    component = dict(name=name, kind="ic", x_mm=0.0, y_mm=0.0, length_mm=6.0,
                     width_mm=4.0, height_mm=2.0, rotation_deg=0.0, package="",
                     confidence="medium", class_confidence="low",
                     presence_confidence="high")
    component["class"] = "semiconductor_unknown"
    component.update(overrides)
    return component


def test_the_list_row_distinguishes_an_undetermined_type_from_an_ordinary_warning(app, png):
    """@brief "[?]" says "the model did not type this"; "[!]" only says "there are warnings".

    Both bodies here carry warnings (neither has a confirmed CAD model), so
    both rows end in "[!]": that marker cannot tell them apart, which is the
    whole reason a second one exists. The row of the undetermined body also
    takes the status colour, so the list carries the same distinction as the
    canvas without needing the photograph to be visible.
    """
    components = [
        _undetermined_component("C19 (semiconductor_unknown)"),
        dict(name="R7", kind="resistor", x_mm=12.0, y_mm=5.0, length_mm=1.6,
             width_mm=.8, height_mm=.45, rotation_deg=0.0, confidence="high",
             class_confidence="high", package="0603", **{"class": "resistor"}),
    ]
    widget = PhotoReviewDialog(png, components, 40, 20)
    try:
        undetermined, ordinary = (widget.component_list.item(0).text(),
                                  widget.component_list.item(1).text())
        assert undetermined == f"1. C19 (semiconductor_unknown) {UNDETERMINED_LIST_MARKER} [!]"
        # Negative: the typed body gets the ordinary marker and only that one.
        assert ordinary == "2. R7 [!]"
        assert UNDETERMINED_LIST_MARKER not in ordinary
        assert widget.component_list.item(0).foreground().color() == QColor(UNDETERMINED_TYPE_COLOR)
        assert widget.component_list.item(1).foreground().color() == QColor("#ffad33")
        # La riga e' corta per forza: la frase che dice cosa fare resta
        # raggiungibile dal tooltip della riga, oltre che dall'avviso laterale.
        assert "non ha stabilito il tipo" in widget.component_list.item(0).toolTip()
        assert "non ha stabilito il tipo" not in widget.component_list.item(1).toolTip()
    finally:
        widget.close()


def test_the_marker_follows_the_declared_type_when_it_is_edited(app, png):
    """@brief Typing a real category in the form retires the marker; the data decides.

    The user is allowed to answer the question the model left open by typing
    the type. The list must then stop claiming the model declined — but only
    because the declared class changed with it, never because a row was
    repainted on its own.
    """
    widget = PhotoReviewDialog(png, [_undetermined_component("C19")], 40, 20)
    try:
        assert UNDETERMINED_LIST_MARKER in widget.component_list.item(0).text()
        widget.component_list.setCurrentRow(0)
        widget._items[0].component.update({"class": "regulator", "class_confidence": "high"})
        widget.text_fields["kind"].setText("ic_soic")
        widget.text_fields["kind"].editingFinished.emit()
        assert widget.get_components()[0]["kind"] == "ic_soic"
        assert UNDETERMINED_LIST_MARKER not in widget.component_list.item(0).text()
        # Negative: the geometry and the rest of the row are untouched by it.
        assert widget.component_list.item(0).text().startswith("1. C19")
    finally:
        widget.close()


def test_the_undetermined_row_survives_the_category_filter(app, png):
    """@brief Filtering hides rows, it never rewrites what a visible row says.

    The filter works on ``kind``, and the undetermined body has a perfectly
    ordinary kind ("ic") because the inventory maps unknown families onto the
    CAD renderer's vocabulary. Selecting that category must therefore keep the
    row, the marker and the colour exactly as they were.
    """
    components = [
        _undetermined_component("C19"),
        dict(name="R7", kind="resistor", x_mm=12.0, y_mm=5.0, length_mm=1.6,
             width_mm=.8, height_mm=.45, rotation_deg=0.0, confidence="high",
             class_confidence="high", **{"class": "resistor"}),
    ]
    widget = PhotoReviewDialog(png, components, 40, 20)
    try:
        before = widget.component_list.item(0).text()
        widget.category_filter.setCurrentIndex(widget.category_filter.findData("ic"))
        assert widget.active_category() == "ic"
        assert not widget.component_list.item(0).isHidden()
        assert widget.component_list.item(0).text() == before
        assert UNDETERMINED_LIST_MARKER in widget.component_list.item(0).text()
        assert widget.component_list.item(0).foreground().color() == QColor(UNDETERMINED_TYPE_COLOR)
    finally:
        widget.close()
