"""Real offscreen Qt interaction tests, with no main_window/VTK imports."""

from copy import deepcopy
import math
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QGraphicsItem, QGraphicsRectItem,
)

from kicadgen.gui.photo_review_dialog import (
    BOARD_BACKDROP_COLOR, BOARD_BORDER_COLOR, DRAW_CLICK_PX,
    MIN_HANDLE_SIZE_MM, NUMBER_LABEL_BACKING_COLOR, PHOTO_DIMMED,
    PHOTO_DIMMED_OPACITY, PHOTO_FULL, PHOTO_HIDDEN, PHOTO_STATES,
    PhotoReviewDialog, PhotoTransform, SELECTION_HALO_COLOR,
    _BoardBackdrop, _ResizeHandle,
)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def png(app):
    # Deliberately anisotropic: 10 px/mm X, 5 px/mm Y for a 40 x 20 mm PCB.
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#243b28"))
    image.setPixelColor(0, 0, QColor("#ff00ff"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


@pytest.fixture
def components():
    return [
        dict(name="U1", kind="ic", x_mm=-5.123456789, y_mm=2.0,
             length_mm=8.0, width_mm=2.0, height_mm=1.5, rotation_deg=90.0,
             confidence="high", _box=(.3, .4, .6, .8),
             bbox=[.1, .2, .3, .4], _source="Crop 1", custom={"pins": [1, 2]}),
        dict(name="R1", kind="resistor", x_mm=8.0, y_mm=-3.0,
             length_mm=3.0, width_mm=1.0, height_mm=.6, rotation_deg=0.0,
             confidence="low", _uncertain=True, marking="100"),
    ]


@pytest.fixture
def dialog(app, png, components):
    widget = PhotoReviewDialog(png, components, 40, 20)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


def test_module_does_not_import_3d_stack():
    script = """
import importlib.abc
import sys
class No3D(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'vtk', 'vtkmodules', 'pyvista', 'pyvistaqt'}:
            raise AssertionError('Unexpected 3D import: ' + fullname)
sys.meta_path.insert(0, No3D())
from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_transform_edge_center_and_round_trip():
    transform = PhotoTransform(401, 103, 40, 20)
    assert transform.mm_to_pixel(-20, -10) == QPointF(0, 0)
    assert transform.mm_to_pixel(20, 10) == QPointF(401, 103)
    assert transform.mm_to_pixel(0, 0) == QPointF(200.5, 51.5)
    for x, y in [(-17.39, 9.61), (0, 0), (29.8, -15.4)]:
        pixel = transform.mm_to_pixel(x, y)
        actual = transform.pixel_to_mm(pixel.x(), pixel.y())
        assert (actual.x(), actual.y()) == pytest.approx((x, y), abs=1e-12)


@pytest.mark.parametrize("angle", [0, 90, 180, 270, 37, -23])
def test_rotated_local_axes_use_metric_geometry_before_pixel_scaling(dialog, angle):
    dialog.fields["rotation_deg"].setValue(angle)
    item = dialog._items[0]
    transform = dialog.photo_transform
    polygon = item.mapToScene(item.polygon())
    mm = [transform.pixel_to_mm(point.x(), point.y()) for point in polygon]
    length_vector = mm[1] - mm[0]
    width_vector = mm[2] - mm[1]
    assert math.hypot(length_vector.x(), length_vector.y()) == pytest.approx(8)
    assert math.hypot(width_vector.x(), width_vector.y()) == pytest.approx(2)
    assert math.degrees(math.atan2(length_vector.y(), length_vector.x())) % 360 == pytest.approx(angle % 360)
    assert length_vector.x()*width_vector.x() + length_vector.y()*width_vector.y() == pytest.approx(0, abs=1e-10)
    assert dialog.get_components()[0]["length_mm"] == 8
    assert dialog.get_components()[0]["width_mm"] == 2


def test_raw_photo_and_numbered_vector_boxes(dialog, components):
    assert dialog.photo_item.pixmap().toImage().pixelColor(0, 0) == QColor("#ff00ff")
    assert dialog.photo_item.pixmap().size().width() == 400
    assert [item.number.text() for item in dialog._items] == ["1", "2"]
    assert dialog.get_components() == components
    bounds = dialog._items[0].photo_bounds()
    assert (bounds.width(), bounds.height()) == pytest.approx((20, 40))
    assert dialog.fields["length_mm"].value() == 8
    assert dialog.fields["width_mm"].value() == 2


def test_side_controls_edit_selected_component_and_refresh_cache(dialog, components):
    dialog.component_list.setCurrentRow(0)
    dialog.fields["x_mm"].setValue(1.25)
    dialog.fields["y_mm"].setValue(-2.5)
    dialog.fields["length_mm"].setValue(6)
    dialog.fields["width_mm"].setValue(1)
    dialog.fields["height_mm"].setValue(2.5)
    actual = dialog.get_components()[0]
    assert (actual["x_mm"], actual["y_mm"], actual["length_mm"],
            actual["width_mm"], actual["height_mm"], actual["rotation_deg"]) == (1.25, -2.5, 6, 1, 2.5, 90)
    assert actual["_box"] == pytest.approx((.51875, .225, .54375, .525))
    assert actual["bbox"] == components[0]["bbox"]
    assert actual["custom"] == components[0]["custom"]
    assert dialog.get_components()[1] == components[1]


def _drag(app, view, start, end, button):
    """Send a real held-button move, independent of the offscreen OS cursor."""
    QTest.mousePress(view.viewport(), button, Qt.KeyboardModifier.NoModifier, start)
    event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(end),
                        QPointF(view.viewport().mapToGlobal(end)),
                        Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)
    QTest.mouseRelease(view.viewport(), button, Qt.KeyboardModifier.NoModifier, end)
    app.processEvents()


def test_mouse_select_and_drag_at_zoom_updates_mm_without_swapping_axes(dialog, app):
    dialog.view.zoom(2)
    item = dialog._items[1]
    start = dialog.view.mapFromScene(item.pos())
    end = start + QPoint(35, 15)
    displacement = dialog.view.mapToScene(end) - dialog.view.mapToScene(start)
    before = deepcopy(item.component)
    _drag(app, dialog.view, start, end, Qt.MouseButton.LeftButton)
    assert dialog.component_list.currentRow() == 1
    actual = dialog.get_components()[1]
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + displacement.x()/10)
    assert actual["y_mm"] == pytest.approx(before["y_mm"] + displacement.y()/5)
    assert dialog.fields["x_mm"].value() == pytest.approx(actual["x_mm"], abs=1e-6)
    assert (actual["length_mm"], actual["width_mm"], actual["rotation_deg"]) == (3, 1, 0)


def _draw_new_component(app, dialog, scene_start, scene_end):
    """Arm the drawing mode and trace one rectangle over the photo.

    Scene points are photo pixels; the gesture goes through the real viewport
    so the view's own press/move/release handling is exercised.
    """
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert dialog.draw_mode()
    _drag(app, dialog.view, dialog.view.mapFromScene(QPointF(*scene_start)),
          dialog.view.mapFromScene(QPointF(*scene_end)), Qt.MouseButton.LeftButton)


def test_add_delete_renumber_and_apply_detached_result(dialog, components, app):
    """@brief Add-draw-delete-apply, with the new body created by a drawn rectangle.

    Contract change: "Aggiungi componente" no longer drops a placeholder in
    the middle of the photo, it arms a drawing mode, so the click alone must
    create nothing and the following drag must create exactly one body. The
    rest of the transaction (renumbering, detached Apply, inputs untouched) is
    unchanged and still asserted here.
    """
    original = deepcopy(components)
    spy = QSignalSpy(dialog.components_applied)
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 2
    _drag(app, dialog.view, dialog.view.mapFromScene(QPointF(100, 30)),
          dialog.view.mapFromScene(QPointF(160, 60)), Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 3
    assert dialog.component_list.currentRow() == 2
    dialog.text_fields["name"].setText("C_missing")
    dialog.text_fields["kind"].setText("capacitor")
    dialog.fields["length_mm"].setValue(1.6)
    # Explicitly commit text as switching focus would do in the live dialog.
    dialog.text_fields["name"].editingFinished.emit()
    dialog.text_fields["kind"].editingFinished.emit()
    dialog.component_list.setCurrentRow(1)
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    assert [item.number.text() for item in dialog._items] == ["1", "2"]
    QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(spy) == 1
    result = spy[0][0]
    assert [entry["name"] for entry in result] == ["U1", "C_missing"]
    assert result[1]["kind"] == "capacitor"
    assert result[1]["length_mm"] == 1.6
    assert result[0]["custom"] == original[0]["custom"]
    result[0]["custom"]["pins"].append(99)
    assert dialog.get_components()[0]["custom"] == original[0]["custom"]
    assert components == original
    dialog.accept()
    assert len(spy) == 1


def test_drawing_a_rectangle_creates_that_body_and_asks_its_type(dialog, app):
    """@brief The drawn rectangle becomes the body's millimeters, and the type is asked.

    The gesture is the measurement: the rectangle is converted through
    PhotoTransform on both axes independently (the photo is anisotropic),
    the body is axis aligned so its rotation is 0, and the parameter modal
    opens on the type field, because that is what the user still has to say.
    """
    _draw_new_component(app, dialog, (100, 30), (160, 60))
    assert not dialog.draw_mode(), "one rectangle, one body: the mode disarms itself"
    assert not dialog.add_button.isChecked()
    assert dialog.add_button.isEnabled()
    assert len(dialog._items) == 3
    assert dialog.component_list.count() == 3
    assert dialog.count_label.text() == "Componenti: 3"
    assert dialog.component_list.currentRow() == 2
    assert dialog._items[2].isSelected()
    drawn = dialog.get_components()[2]
    # (100, 30)-(160, 60) px of a 400 x 100 px crop of a 40 x 20 mm board.
    assert drawn["x_mm"] == pytest.approx(-7, abs=.3)
    assert drawn["y_mm"] == pytest.approx(-1, abs=.3)
    assert drawn["length_mm"] == pytest.approx(6, abs=.3)
    assert drawn["width_mm"] == pytest.approx(6, abs=.3)
    assert drawn["rotation_deg"] == 0
    assert drawn["_uncertain"] is True
    assert dialog.parameters_dialog.isVisible()
    assert dialog.parameters_dialog.focusWidget() is dialog.text_fields["kind"]
    assert dialog.text_fields["kind"].selectedText() == dialog.text_fields["kind"].text()
    # The vocabulary offered is the inventory's own, never a built-in taxonomy.
    assert dialog.kind_completer.model().stringList() == ["generic", "ic", "resistor"]


def test_the_type_field_offers_the_categories_without_forcing_them(dialog, app):
    """@brief Negative: a type outside the vocabulary is accepted as typed.

    The completer is a convenience; the next board may carry a package that
    nobody in this inventory has named yet, and refusing it would lose data.
    """
    _draw_new_component(app, dialog, (100, 30), (160, 60))
    assert "crystal-oscillator" not in dialog.kind_completer.model().stringList()
    dialog.text_fields["kind"].setText("crystal-oscillator")
    dialog.text_fields["kind"].editingFinished.emit()
    assert dialog.get_components()[2]["kind"] == "crystal-oscillator"
    assert "crystal-oscillator" in dialog.kind_completer.model().stringList()


def test_a_still_click_on_the_photo_creates_nothing_and_keeps_the_mode_armed(dialog, app):
    """@brief Negative: a stroke under the click threshold is not a measurement.

    Below DRAW_CLICK_PX of travel the gesture is a click that missed; nothing
    is created and the mode stays armed, so the user simply tries again
    instead of having to press the button once more.
    """
    before = deepcopy(dialog.get_components())
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    start = dialog.view.mapFromScene(QPointF(200, 50))
    _drag(app, dialog.view, start, start + QPoint(2, 2), Qt.MouseButton.LeftButton)
    assert dialog.get_components() == before
    assert dialog.component_list.count() == 2
    assert dialog.draw_mode() and dialog.add_button.isChecked()
    assert not dialog.parameters_dialog.isVisible()


def test_a_drag_started_outside_the_photo_creates_nothing_and_keeps_the_mode_armed(dialog, app):
    """@brief Negative: outside the board crop there is nothing to measure.

    The view has empty space around the photo; a stroke starting there must
    not invent a body off the board, and must not silently disarm the mode.
    """
    before = deepcopy(dialog.get_components())
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    start = dialog.view.mapFromScene(QPointF(200, -30))
    assert dialog.view.viewport().rect().contains(start), "the stroke starts in the viewport"
    assert not dialog.photo_rect.contains(dialog.view.mapToScene(start)), "...but off the photo"
    _drag(app, dialog.view, start, start + QPoint(40, 40), Qt.MouseButton.LeftButton)
    assert dialog.get_components() == before
    assert dialog.draw_mode() and dialog.add_button.isChecked()


@pytest.mark.parametrize("disarm", ["escape", "button"])
def test_escape_or_the_button_again_disarms_without_creating_anything(dialog, app, disarm):
    """@brief Negative: disarming is not an edit, and Escape does not close the editor.

    An armed mode is the innermost thing Escape can cancel: giving up on a
    rectangle must not reject the whole review.
    """
    before = deepcopy(dialog.get_components())
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert dialog.draw_mode()
    if disarm == "escape":
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not dialog.draw_mode()
    assert not dialog.add_button.isChecked()
    # Rejecting would hide the editor; it is still up, mode off.
    assert dialog.isVisible()
    assert dialog.get_components() == before
    # Disarmed, the left button belongs to the outlines again.
    item = dialog._items[1]
    start = dialog.view.mapFromScene(item.pos())
    _drag(app, dialog.view, start, start + QPoint(20, 10), Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 2
    assert dialog.get_components()[1]["x_mm"] != before[1]["x_mm"]


def test_escape_still_rejects_the_editor_when_no_mode_is_armed(dialog, app):
    """@brief Negative: the drawing mode must not swallow Escape for good."""
    assert not dialog.draw_mode()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_a_rectangle_drawn_smaller_than_the_handle_floor_is_held_at_it(dialog, app):
    """@brief A real stroke at deep zoom still cannot leave a degenerate body.

    Zoomed in, a stroke long enough on screen can cover less than
    MIN_HANDLE_SIZE_MM of board: the body is held at that floor, the same one
    the resize handles enforce, otherwise no handle could grab it again.
    """
    dialog.view.zoom(64)
    dialog.view.centerOn(QPointF(200, 50))
    app.processEvents()
    start = dialog.view.viewport().rect().center()
    scale = dialog.view.transform().m11()
    travel = QPoint(10, 10)
    assert travel.x() / scale / 10 < MIN_HANDLE_SIZE_MM, "the stroke is under the floor in mm"
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    _drag(app, dialog.view, start, start + travel, Qt.MouseButton.LeftButton)
    drawn = dialog.get_components()[2]
    assert drawn["length_mm"] == pytest.approx(MIN_HANDLE_SIZE_MM)
    assert drawn["width_mm"] == pytest.approx(MIN_HANDLE_SIZE_MM)


def _bands(dialog):
    """Rubber bands currently on the scene.

    The resize grips are not bands, and neither is the board backdrop: both
    are permanent furniture of the canvas, while a band exists only while a
    drawing gesture is in progress. They are told apart by type on purpose, so
    that a band left behind still fails this check.
    """
    return [item for item in dialog.scene.items()
            if isinstance(item, QGraphicsRectItem)
            and not isinstance(item, (_ResizeHandle, _BoardBackdrop))]


def _move_event(dialog, point, buttons):
    """A viewport move at ``point`` declaring ``buttons`` as held."""
    return QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(point),
                       QPointF(dialog.view.viewport().mapToGlobal(point)),
                       Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)


def _start_a_gesture(app, dialog, scene_start, scene_move):
    """Arm the mode, press inside the photo and drag without ever releasing."""
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    start = dialog.view.mapFromScene(QPointF(*scene_start))
    QTest.mousePress(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, start)
    move = dialog.view.mapFromScene(QPointF(*scene_move))
    QApplication.sendEvent(dialog.view.viewport(),
                           _move_event(dialog, move, Qt.MouseButton.LeftButton))
    app.processEvents()
    return start, move


def _idle_match_worker(monkeypatch):
    """Replace the measurement thread with a stand-in that only records starts."""
    from kicadgen.gui import photo_review_dialog as module

    started = []

    class _Idle(module.QThread):
        """@brief Stand-in worker: no thread and no modal, only the request itself."""

        done = module.pyqtSignal(object)
        failed = module.pyqtSignal(str)

        def __init__(self, snapshot):
            super().__init__()

        def start(self):
            started.append(self)

    monkeypatch.setattr(module, "_MatchWorker", _Idle)
    return started


def test_a_double_click_while_armed_keeps_the_gesture_instead_of_choosing_a_model(
        dialog, app, monkeypatch):
    """@brief Negative: while armed the left button never reaches the outlines, doubled or not.

    MouseButtonDblClick is a distinct event type from MouseButtonPress, so the
    natural retry after a stroke refused as too short (the mode deliberately
    stays armed) used to fall through to the outline under the pointer: the
    selection jumped, a measurement thread started and the model chooser opened
    over a canvas the user was still drawing on. The doubled press must behave
    like a press: it starts a gesture and nothing else.
    """
    started = _idle_match_worker(monkeypatch)
    dialog.view.zoom(3)
    dialog.component_list.setCurrentRow(0)
    item = dialog._items[1]
    dialog.view.centerOn(item)
    app.processEvents()
    point = dialog.view.mapFromScene(item.scenePos())
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    _drag(app, dialog.view, point, point + QPoint(2, 2), Qt.MouseButton.LeftButton)
    assert dialog.draw_mode(), "a stroke under the threshold leaves the mode armed"
    before = deepcopy(dialog.get_components())
    QTest.mouseDClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier, point)
    app.processEvents()
    assert started == [], "the doubled press must not ask for CAD candidates"
    assert dialog._match_worker is None
    assert dialog.component_list.currentRow() == 0, "the selection must not jump"
    assert dialog.get_components() == before
    assert dialog.draw_mode() and dialog.add_button.isChecked()
    # ...and the retry still measures: the doubled press armed the gesture.
    _drag(app, dialog.view, point, point + QPoint(40, 25), Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 3


def test_a_double_click_reaches_the_outline_when_no_mode_is_armed(dialog, app, monkeypatch):
    """@brief Positive counterpart: disarmed, a double click is still the model chooser."""
    started = _idle_match_worker(monkeypatch)
    dialog.view.zoom(3)
    dialog.component_list.setCurrentRow(0)
    item = dialog._items[1]
    dialog.view.centerOn(item)
    app.processEvents()
    assert not dialog.draw_mode()
    QTest.mouseDClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier,
                      dialog.view.mapFromScene(item.scenePos()))
    app.processEvents()
    assert dialog.component_list.currentRow() == 1
    assert len(started) == 1


def test_a_long_thin_stroke_is_a_measurement_and_not_a_slip(dialog, app):
    """@brief One axis clearing DRAW_CLICK_PX is enough: thin bodies are real.

    A pin-header edge or a shield seam is long and deliberately thin, so
    requiring the travel on BOTH axes discarded it in silence however long the
    stroke was — contradicting the threshold's own rationale, which is about a
    slip of the hand. The thin side is then held by MIN_HANDLE_SIZE_MM, not by
    a screen threshold. The counterpart, a stroke inside the dead zone on both
    axes, stays refused (see the still-click test above).
    """
    start = dialog.view.mapFromScene(QPointF(100, 40))
    end = dialog.view.mapFromScene(QPointF(160, 40.5))
    travel = end - start
    assert abs(travel.x()) >= DRAW_CLICK_PX > abs(travel.y()), (
        "the stroke must be long on one axis and inside the dead zone on the other")
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    _drag(app, dialog.view, start, end, Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 3, "a 6 mm stroke is not a stray click"
    drawn = dialog.get_components()[2]
    assert drawn["length_mm"] == pytest.approx(6, abs=.3)
    assert drawn["width_mm"] == pytest.approx(MIN_HANDLE_SIZE_MM)
    assert not dialog.draw_mode()


@pytest.mark.parametrize("dismiss", ["hide", "close", "reject"])
def test_an_interrupted_gesture_does_not_survive_the_editor_being_dismissed(
        dialog, app, dismiss):
    """@brief An unfinished rectangle belongs to the gesture, not to the scene.

    A minimize or a close taken with the button down kills the pointer grab
    with the window, so the release that would finish the stroke never arrives.
    Left alone, the dashed band came back on restore and followed the bare
    pointer, and a release delivered without a press created a body the user
    never drew.
    """
    start, move = _start_a_gesture(app, dialog, (120, 40), (200, 70))
    assert len(_bands(dialog)) == 1, "a held drag does show its band"
    before = deepcopy(dialog.get_components())
    getattr(dialog, dismiss)()
    app.processEvents()
    assert _bands(dialog) == []
    dialog.show()
    app.processEvents()
    far = dialog.view.mapFromScene(QPointF(300, 90))
    QApplication.sendEvent(dialog.view.viewport(),
                           _move_event(dialog, far, Qt.MouseButton.NoButton))
    app.processEvents()
    assert _bands(dialog) == [], "the band must not come back on the bare pointer"
    QTest.mouseRelease(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, far)
    app.processEvents()
    assert dialog.get_components() == before, "a release without a press creates nothing"


def test_a_move_without_the_left_button_ends_the_gesture_instead_of_extending_it(dialog, app):
    """@brief Negative: the band tracks a held button, never the bare pointer.

    The draw branch used to key off the pending origin alone, so any motion
    after a lost release kept re-drawing the band and the next release
    committed a rectangle nobody traced. Positive counterpart in the same test:
    a properly held drag still draws.
    """
    start, _move = _start_a_gesture(app, dialog, (120, 40), (200, 70))
    assert len(_bands(dialog)) == 1
    before = deepcopy(dialog.get_components())
    far = dialog.view.mapFromScene(QPointF(300, 90))
    QApplication.sendEvent(dialog.view.viewport(),
                           _move_event(dialog, far, Qt.MouseButton.NoButton))
    app.processEvents()
    assert _bands(dialog) == [], "the gesture died with the button, and so did its band"
    QTest.mouseRelease(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, far)
    app.processEvents()
    assert dialog.get_components() == before
    assert dialog.draw_mode(), "the mode stays armed: the user simply tries again"
    _drag(app, dialog.view, start, far, Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == len(before) + 1, "a held drag still draws"


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_return_applies_the_corrections_instead_of_discarding_them(dialog, app, key):
    """@brief Return confirms the review; it must never be a disguised Cancel.

    "Applica correzioni" carries ApplyRole, so this button box has no
    AcceptRole button and Qt made Cancel the default one: the most ordinary
    keystroke in a dialog threw away every correction. Both the default button
    and the key handler are asserted, because either alone would leave a focus
    path where Return still cancels.
    """
    apply_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Apply)
    cancel_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
    assert apply_button.isDefault() and not cancel_button.isDefault()
    spy = QSignalSpy(dialog.components_applied)
    dialog.component_list.setCurrentRow(1)
    dialog.fields["height_mm"].setValue(2.0)
    # The editor opens with the focus on Cancel, which is where the keystroke
    # really lands: an auto-default button answers Return itself, so the key is
    # sent to that button and not straight to the dialog.
    cancel_button.setFocus()
    app.processEvents()
    assert cancel_button.hasFocus()
    assert apply_button.isDefault(), "a focused Cancel must not take the default away"
    QTest.keyClick(cancel_button, key)
    app.processEvents()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(spy) == 1
    assert spy[0][0][1]["height_mm"] == 2.0


def test_the_focused_cancel_button_still_discards_when_pressed(dialog, app):
    """@brief Negative: Cancel gives up its auto-default, not its job.

    Return must not reject, but the button itself must still reject when it is
    the button the user presses: Space on the focused widget, and Escape.
    """
    cancel_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
    spy = QSignalSpy(dialog.components_applied)
    cancel_button.setFocus()
    app.processEvents()
    QTest.keyClick(cancel_button, Qt.Key.Key_Space)
    app.processEvents()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert len(spy) == 0


def test_escape_disarms_first_and_rejects_afterwards(dialog, app):
    """@brief Negative: Escape is still the way out, and still cancels the mode first.

    Confirming with Return must not cost the editor its cancel path: one
    Escape gives up on the rectangle, the next one gives up on the review.
    """
    spy = QSignalSpy(dialog.components_applied)
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert dialog.draw_mode()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert not dialog.draw_mode()
    assert dialog.isVisible(), "giving up on the rectangle is not giving up on the review"
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert len(spy) == 0


def test_add_component_disarms_the_mode_on_the_programmatic_path_too(dialog, app):
    """@brief The invariant belongs to the addition, not to one of its callers.

    Only the drawn path disarmed, so a programmatic ``add_component()`` with
    the mode armed opened the parameter modal over a live canvas: the user's
    next drag created a second body instead of adjusting the one just added.
    """
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert dialog.draw_mode()
    dialog.add_component()
    app.processEvents()
    assert not dialog.draw_mode()
    assert not dialog.add_button.isChecked()
    assert not dialog.draw_hint.isVisible()
    assert dialog.view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor
    dialog.parameters_buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    app.processEvents()
    # The canvas is an editor again: the next drag moves the new body. Zoomed
    # in, because the grips are 8 screen px wide whatever the zoom and a 2 x 1 mm
    # placeholder is otherwise all grip.
    dialog.view.zoom(4)
    item = dialog._items[2]
    before = deepcopy(item.component)
    dialog.view.centerOn(item)
    app.processEvents()
    start = dialog.view.mapFromScene(item.scenePos())
    _drag(app, dialog.view, start, start + QPoint(20, 10), Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 3, "no second body was created"
    assert dialog.get_components()[2]["x_mm"] != before["x_mm"]


def test_a_stroke_on_the_board_edge_stays_inside_after_the_metric_floor(dialog, app):
    """@brief The floor may grow a clipped side, but never back off the board.

    The release handler clips the rectangle to the photo, then raises a side to
    MIN_HANDLE_SIZE_MM: a stroke traced on the very edge of the board and
    dragged off it came back out on the other side of the edge, and the new
    outline was flagged "Fuori dai bordi PCB" the instant it appeared. The
    centre is clamped per axis, so the axis that was already inside is left
    exactly where the stroke put it.
    """
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    start = dialog.view.mapFromScene(QPointF(300, 99.6))
    end = dialog.view.mapFromScene(QPointF(300, 160))
    assert dialog.photo_rect.contains(dialog.view.mapToScene(start)), "the stroke starts on the board"
    assert not dialog.photo_rect.contains(dialog.view.mapToScene(end)), "...and ends off it"
    _drag(app, dialog.view, start, end, Qt.MouseButton.LeftButton)
    assert len(dialog.get_components()) == 3
    drawn = dialog.get_components()[2]
    assert drawn["width_mm"] == pytest.approx(MIN_HANDLE_SIZE_MM), "the clipped side is at the floor"
    assert drawn["y_mm"] == pytest.approx(10 - MIN_HANDLE_SIZE_MM / 2)
    # Negative side: the length axis never left the board, so it is untouched.
    assert drawn["x_mm"] == pytest.approx(10, abs=.05)
    item = dialog._items[2]
    assert dialog.photo_rect.adjusted(-1e-7, -1e-7, 1e-7, 1e-7).contains(item.photo_bounds())
    assert item.pen().color().name() != "#ef5350"
    assert not any("Fuori" in warning for warning in dialog._warnings(item))


def test_apply_commits_pending_keyboard_text(dialog):
    spy = QSignalSpy(dialog.components_applied)
    field = dialog.fields["length_mm"]
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, field.locale().toString(6.75, "f", 2))
    # Keyboard tracking is disabled, so accept must interpret the current text.
    dialog.text_fields["name"].setText("Manual U1")
    dialog.accept()
    assert spy[0][0][0]["length_mm"] == 6.75
    assert spy[0][0][0]["name"] == "Manual U1"


@pytest.mark.parametrize("cancel", ["button", "escape", "close"])
def test_cancel_never_emits_or_mutates_inputs(dialog, components, cancel):
    original = deepcopy(components)
    spy = QSignalSpy(dialog.components_applied)
    dialog.fields["x_mm"].setValue(3)
    dialog.delete_selected()
    dialog.add_component()
    detached = dialog.get_components()
    detached[0]["marking"] = "changed outside dialog"
    if cancel == "button":
        dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
    elif cancel == "escape":
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        dialog.close()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert len(spy) == 0
    assert components == original
    assert dialog.get_components()[0]["marking"] == "100"


def test_warning_colors_and_outside_bounds_are_live(dialog, tmp_path):
    # Detection confidence is not a confirmed CAD match.
    assert dialog._items[0].pen().color().name() == "#ffad33"
    chosen = tmp_path/'chosen.step'
    chosen.touch()
    dialog._items[0].component['model_selection'] = {'status':'confirmed','path':str(chosen)}
    dialog._refresh_item(dialog._items[0])
    assert dialog._items[0].pen().color().name() == "#40dc80"
    assert dialog._items[1].pen().color().name() == "#ffad33"
    dialog.fields["x_mm"].setValue(20)
    assert dialog._items[0].pen().color().name() == "#ef5350"
    assert "Fuori" in dialog.warning_label.text()
    dialog.fields["x_mm"].setValue(0)
    assert dialog._items[0].pen().color().name() == "#ffad33"
    dialog.component_list.setCurrentRow(1)
    assert "incerto" in dialog.warning_label.text()


def test_zoom_pan_fit_do_not_change_geometry(dialog, app):
    original = dialog.get_components()
    fit_scale = dialog.view.transform().m11()
    dialog.zoom_in_button.click()
    assert dialog.view.transform().m11() > fit_scale
    position = QPointF(dialog.view.viewport().rect().center())
    event = QWheelEvent(position, QPointF(dialog.view.viewport().mapToGlobal(position.toPoint())),
                        QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(dialog.view.viewport(), event)
    assert dialog.view.transform().m11() == pytest.approx(fit_scale*1.2*1.2)
    dialog.view.zoom(4)
    app.processEvents()
    before = dialog.view.horizontalScrollBar().value()
    _drag(app, dialog.view, QPoint(100, 100), QPoint(150, 110), Qt.MouseButton.MiddleButton)
    assert dialog.view.horizontalScrollBar().value() == before - 50
    dialog.fit_button.click()
    assert dialog.view.transform().m11() == pytest.approx(fit_scale, rel=.02)
    assert dialog.get_components() == original


def test_resizing_refits_whole_photo_and_apply_keeps_geometry(dialog, app):
    original = dialog.get_components()
    scales = []
    for width, height in [(1600, 1000), (900, 680), (1120, 740)]:
        dialog.resize(width, height)
        QTest.qWait(20)
        viewport = dialog.view.viewport().rect()
        photo = dialog.view.mapFromScene(dialog.photo_rect).boundingRect()
        assert viewport.adjusted(-2, -2, 2, 2).contains(photo)
        scales.append(dialog.view.transform().m11())
        assert not dialog.grab().isNull()
        assert dialog.get_components() == original
    assert scales[0] > scales[1]
    spy = QSignalSpy(dialog.components_applied)
    dialog.buttons.button(QDialogButtonBox.StandardButton.Apply).click()
    app.processEvents()
    assert len(spy) == 1
    assert spy[0][0] == original
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_resize_preserves_manual_zoom_until_fit_requested(dialog):
    dialog.view.zoom(3)
    scale = dialog.view.transform().m11()
    dialog.resize(1500, 1000)
    QTest.qWait(20)
    assert dialog.view.transform().m11() == pytest.approx(scale)
    dialog.fit_button.click()
    dialog.resize(900, 680)
    QTest.qWait(20)
    photo = dialog.view.mapFromScene(dialog.photo_rect).boundingRect()
    assert dialog.view.viewport().rect().adjusted(-2, -2, 2, 2).contains(photo)


def test_empty_inventory_delete_last_and_add(app, png):
    dialog = PhotoReviewDialog(png, [], 40, 20)
    try:
        assert not dialog.form_widget.isEnabled()
        assert not dialog.delete_button.isEnabled()
        dialog.delete_selected()
        dialog.add_component()
        assert len(dialog.get_components()) == 1
        assert dialog.form_widget.isEnabled()
        dialog.delete_selected()
        assert dialog.get_components() == []
        assert not dialog.delete_button.isEnabled()
        spy = QSignalSpy(dialog.components_applied)
        dialog.accept()
        assert spy[0][0] == []
    finally:
        dialog.close()


def test_unchanged_apply_preserves_exact_values_and_nested_metadata(dialog, components):
    spy = QSignalSpy(dialog.components_applied)
    dialog.accept()
    assert spy[0][0] == components
    components[0]["custom"]["pins"].append(3)
    assert dialog.get_components()[0]["custom"]["pins"] == [1, 2]


@pytest.mark.parametrize("length,width", [(0, 20), (40, -1), (float("nan"), 20), (40, float("inf"))])
def test_invalid_calibration_rejected(app, png, length, width):
    with pytest.raises(ValueError):
        PhotoReviewDialog(png, [], length, width)


@pytest.mark.parametrize("key,value", [("length_mm", 0), ("width_mm", -1),
                                       ("x_mm", float("nan")), ("rotation_deg", float("inf"))])
def test_invalid_component_geometry_rejected(app, png, components, key, value):
    components[0][key] = value
    with pytest.raises(ValueError):
        PhotoReviewDialog(png, components, 40, 20)


def test_invalid_image_rejected(app):
    with pytest.raises(ValueError, match="PNG"):
        PhotoReviewDialog(b"not a PNG", [], 40, 20)


def test_the_quit_promise_is_released_when_the_measurement_thread_ends(dialog, app, monkeypatch):
    """@brief La promessa di aspettare vale finche' il thread esiste.

    `aboutToQuit` teneva un riferimento a `worker.wait` che sopravviveva a
    `deleteLater`: alla chiusura dell'applicazione Qt chiamava quel metodo su
    un oggetto C++ gia' distrutto e l'uscita finiva con
    «RuntimeError: wrapped C/C++ object of type _MatchWorker has been deleted».
    Il thread vero non serve a dimostrarlo: serve che la connessione presa
    all'avvio venga sciolta alla fine.
    """
    from kicadgen.gui import photo_review_dialog as module

    started = []

    class _Idle(module.QThread):
        """@brief Thread che non fa nulla: qui conta solo il ciclo di vita."""

        done = module.pyqtSignal(object)
        failed = module.pyqtSignal(str)

        def __init__(self, snapshot):
            super().__init__()
            self.snapshot = snapshot

        def start(self):
            started.append(self)

        def run(self):
            return None

    monkeypatch.setattr(module, '_MatchWorker', _Idle)
    dialog._select_row(0)
    dialog.choose_model()
    assert len(started) == 1
    worker = started[0]

    # Finche' il thread e' in corso la promessa c'e': disconnetterla riesce.
    app.aboutToQuit.disconnect(worker.wait)
    app.aboutToQuit.connect(worker.wait)

    worker.finished.emit()
    app.processEvents()
    # Sciolta: una seconda disconnessione non ha piu' nulla da sciogliere.
    with pytest.raises(TypeError):
        app.aboutToQuit.disconnect(worker.wait)


_TEARDOWN_SCRIPT = """
import gc, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from kicadgen.gui.photo_review_dialog import PhotoReviewDialog

app = QApplication([])
image = QImage(400, 100, QImage.Format.Format_RGB32)
image.fill(QColor("#243b28"))
data = QByteArray()
buffer = QBuffer(data)
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
image.save(buffer, "PNG")
components = [dict(name="U1", kind="ic", x_mm=-5.0, y_mm=2.0, length_mm=8.0,
                   width_mm=2.0, height_mm=1.5, rotation_deg=90.0)]
dialog = PhotoReviewDialog(bytes(data), components, 40, 20)
dialog.show()
app.processEvents()
dialog.component_list.setCurrentRow(0)
dialog.open_parameters()
dialog.parameters_dialog.activateWindow()
app.processEvents()
dialog.text_fields["name"].setFocus()
app.processEvents()
QTest.keyClicks(dialog.text_fields["name"], "typed")
assert dialog.text_fields["name"].hasFocus(), "the editor never took real focus"
if sys.argv[1] == "drop":
    del dialog
    gc.collect()
    app.processEvents()
print("SURVIVED")
"""


@pytest.mark.parametrize("mode", ["drop", "quit"])
def test_abnormal_teardown_with_a_focused_editor_does_not_abort(mode):
    """@brief Regression: the focused line edit emitting from its destructor must not abort.

    An application quitting with the modal up, or the last reference to the
    editor being dropped, destroys the dialog from C++ without hideEvent or
    closeEvent: the focused QLineEdit then emits ``editingFinished`` from its
    destructor. Closure slots were called with their defaults already torn
    down (TypeError → PyQt abort, exit 134); bound-method slots are dropped
    by PyQt together with the receiver. The process must end normally in
    both modes, with and without an explicit ``del`` + ``gc.collect()``.
    """
    result = subprocess.run([sys.executable, "-c", _TEARDOWN_SCRIPT, mode],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "SURVIVED" in result.stdout
    assert "TypeError" not in result.stderr and "Aborted" not in result.stderr


def test_field_slots_resolve_their_key_from_the_sender(dialog):
    """@brief Positive: a field signal commits to the key of the emitting widget only.

    The slots are bound methods shared by every field, so the key must come
    from ``sender().objectName()``: an edit to ``kind`` must not land in
    ``name`` or ``marking``, and a numeric edit must reach its own key.
    """
    dialog.component_list.setCurrentRow(1)
    before = deepcopy(dialog.get_components()[1])
    dialog.text_fields["kind"].setText("inductor")
    dialog.text_fields["kind"].editingFinished.emit()
    dialog.fields["height_mm"].setValue(2.2)
    actual = dialog.get_components()[1]
    assert actual["kind"] == "inductor"
    assert actual["height_mm"] == 2.2
    assert (actual["name"], actual["marking"]) == (before["name"], before["marking"])
    assert (actual["length_mm"], actual["width_mm"]) == (before["length_mm"], before["width_mm"])


def test_field_slots_ignore_a_direct_call_without_a_sender(dialog):
    """@brief Negative: called outside a signal there is no field to read, so nothing changes."""
    dialog.component_list.setCurrentRow(1)
    before = dialog.get_components()
    dialog.text_fields["name"].setText("would be lost")
    dialog._text_field_finished()
    dialog._number_field_changed(99.0)
    assert dialog.get_components() == before


def _handle_view_point(dialog, item, edge):
    """Viewport pixel of a resize grip; grips ignore zoom, so the center is enough."""
    return dialog.view.mapFromScene(item.handle(edge).scenePos())


def _drag_handle(dialog, app, item, edge, offset):
    """Drag one grip by a viewport offset and return the scene displacement in px."""
    start = _handle_view_point(dialog, item, edge)
    end = start + offset
    displacement = dialog.view.mapToScene(end) - dialog.view.mapToScene(start)
    _drag(app, dialog.view, start, end, Qt.MouseButton.LeftButton)
    return displacement


def test_length_handle_at_rotation_0_grows_length_and_moves_center_by_half(dialog, app):
    """@brief The far length grip of an unrotated part edits only length_mm.

    The opposite edge stays put, so the center follows by half the growth;
    the vertical part of the stroke is ignored because a mid-edge grip only
    owns one local axis.
    """
    dialog.component_list.setCurrentRow(1)
    item = dialog._items[1]
    before = deepcopy(item.component)
    displacement = _drag_handle(dialog, app, item, (1, 0), QPoint(30, 7))
    grown = displacement.x() / 10
    assert grown > 0
    actual = dialog.get_components()[1]
    assert actual["length_mm"] == pytest.approx(before["length_mm"] + grown)
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + grown / 2)
    assert (actual["y_mm"], actual["width_mm"], actual["rotation_deg"]) == (
        before["y_mm"], before["width_mm"], before["rotation_deg"])
    assert dialog.fields["length_mm"].value() == pytest.approx(actual["length_mm"], abs=1e-6)
    assert dialog.fields["x_mm"].value() == pytest.approx(actual["x_mm"], abs=1e-6)
    assert dialog.component_list.currentRow() == 1


def test_screen_right_grip_of_a_90_degree_part_is_its_local_width_side(dialog, app):
    """@brief Grips follow the rotation: at 90 degrees the screen-right grip is (0, -1).

    Local +u points down the screen, local +v points left, so the grip on
    the right of the screen is the NEAR width side: dragging it rightwards
    grows width_mm, leaves length_mm alone and shifts the center by half.
    """
    dialog.component_list.setCurrentRow(0)
    item = dialog._items[0]
    before = deepcopy(item.component)
    side_grips = [edge for edge in item.handles if 0 in edge]
    right_grip = max(side_grips, key=lambda edge: item.handle(edge).scenePos().x())
    assert right_grip == (0, -1)
    assert item.handle((1, 0)).scenePos().y() > item.scenePos().y()
    displacement = _drag_handle(dialog, app, item, right_grip, QPoint(20, 0))
    grown = displacement.x() / 10
    actual = dialog.get_components()[0]
    assert actual["width_mm"] == pytest.approx(before["width_mm"] + grown)
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + grown / 2)
    assert actual["length_mm"] == before["length_mm"]
    assert actual["y_mm"] == pytest.approx(before["y_mm"])
    assert dialog.fields["width_mm"].value() == pytest.approx(actual["width_mm"], abs=1e-6)


@pytest.mark.parametrize("angle", [180, 270, 37, -23])
def test_length_grip_edits_the_local_axis_at_any_rotation(dialog, app, angle):
    """@brief A stroke on the +u grip changes length only, whatever the angle.

    The pointer displacement is projected on the local axes through the
    anisotropic pixel scales; the mid-edge grip discards the v component,
    and the center moves by half the growth along the rotated u direction.
    """
    dialog.component_list.setCurrentRow(1)
    dialog.fields["rotation_deg"].setValue(angle)
    item = dialog._items[1]
    before = deepcopy(item.component)
    displacement = _drag_handle(dialog, app, item, (1, 0), QPoint(24, -9))
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    grown = displacement.x() / 10 * c + displacement.y() / 5 * s
    actual = dialog.get_components()[1]
    assert actual["length_mm"] == pytest.approx(before["length_mm"] + grown)
    assert actual["width_mm"] == before["width_mm"]
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + grown / 2 * c)
    assert actual["y_mm"] == pytest.approx(before["y_mm"] + grown / 2 * s)
    # The far edge stayed exactly where it was: it is the stroke's anchor.
    far = dialog.photo_transform.pixel_to_mm(item.handle((-1, 0)).scenePos().x(),
                                             item.handle((-1, 0)).scenePos().y())
    assert (far.x(), far.y()) == pytest.approx(
        (before["x_mm"] - before["length_mm"] / 2 * c, before["y_mm"] - before["length_mm"] / 2 * s))


def test_dragging_inside_the_outline_moves_without_resizing(dialog, app):
    """@brief Negative: the interior is a move grip, and unselected parts have no handles.

    Grips are 8 screen px wide whatever the zoom, so a 1 mm part must be
    zoomed in before its interior is wider than the grips that frame it.
    """
    dialog.view.zoom(4)
    dialog.component_list.setCurrentRow(1)
    selected = dialog._items[1]
    before = deepcopy(selected.component)
    start = dialog.view.mapFromScene(selected.scenePos())
    _drag(app, dialog.view, start, start + QPoint(12, 6), Qt.MouseButton.LeftButton)
    actual = dialog.get_components()[1]
    assert (actual["length_mm"], actual["width_mm"]) == (before["length_mm"], before["width_mm"])
    assert actual["x_mm"] != before["x_mm"]
    # The other part is not selected: its grips are hidden and pressing where
    # one would be moves the outline instead of resizing it.
    other = dialog._items[0]
    assert not other.handle((1, 0)).isVisible()
    before_other = deepcopy(other.component)
    start = _handle_view_point(dialog, other, (1, 0))
    _drag(app, dialog.view, start, start + QPoint(10, 10), Qt.MouseButton.LeftButton)
    actual = dialog.get_components()[0]
    assert (actual["length_mm"], actual["width_mm"]) == (before_other["length_mm"], before_other["width_mm"])
    assert actual["x_mm"] != before_other["x_mm"]


def test_resize_stops_at_the_declared_minimum_instead_of_inverting(dialog, app):
    """@brief Negative: pulling a grip past the opposite edge leaves the minimum extent."""
    from kicadgen.gui.photo_review_dialog import MIN_HANDLE_SIZE_MM
    dialog.component_list.setCurrentRow(1)
    item = dialog._items[1]
    before = deepcopy(item.component)
    far_left_mm = before["x_mm"] - before["length_mm"] / 2
    # 3 mm long at 10 px/mm: a 200 px stroke to the left crosses the far edge.
    _drag_handle(dialog, app, item, (1, 0), QPoint(-200, 0))
    actual = dialog.get_components()[1]
    assert actual["length_mm"] == pytest.approx(MIN_HANDLE_SIZE_MM)
    assert actual["x_mm"] == pytest.approx(far_left_mm + MIN_HANDLE_SIZE_MM / 2)
    assert actual["width_mm"] == before["width_mm"]
    assert dialog.fields["length_mm"].value() == pytest.approx(MIN_HANDLE_SIZE_MM, abs=1e-6)
    # The outline is still a proper rectangle inside the photo, not flipped.
    bounds = item.photo_bounds()
    assert bounds.width() == pytest.approx(MIN_HANDLE_SIZE_MM * 10)


def _scene_polygon_mm(dialog, item):
    """Corners of the outline in board millimeters, in polygon order."""
    transform = dialog.photo_transform
    return [transform.pixel_to_mm(p.x(), p.y()) for p in item.mapToScene(item.polygon())]


def test_corner_handle_at_37_degrees_edits_both_local_axes_and_keeps_a_rectangle(dialog, app):
    """@brief The (1, 1) corner grip of a 37 degree part grows length AND width.

    The pointer displacement is projected on both rotated local axes through
    the anisotropic pixel scales; the opposite corner is the anchor, so the
    center moves by half of each growth along its own axis. Afterwards the
    outline is still a rectangle (metric right angles, opposite sides equal)
    at the same 37 degrees: the resize never shears or re-rotates it.
    """
    angle = 37
    dialog.component_list.setCurrentRow(1)
    dialog.fields["rotation_deg"].setValue(angle)
    item = dialog._items[1]
    before = deepcopy(item.component)
    anchor = _scene_polygon_mm(dialog, item)[0]
    displacement = _drag_handle(dialog, app, item, (1, 1), QPoint(22, 13))
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    dx_mm, dy_mm = displacement.x() / 10, displacement.y() / 5
    grown_u = dx_mm * c + dy_mm * s
    grown_v = -dx_mm * s + dy_mm * c
    assert grown_u > 0 and grown_v > 0
    actual = dialog.get_components()[1]
    assert actual["length_mm"] == pytest.approx(before["length_mm"] + grown_u)
    assert actual["width_mm"] == pytest.approx(before["width_mm"] + grown_v)
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + grown_u / 2 * c - grown_v / 2 * s)
    assert actual["y_mm"] == pytest.approx(before["y_mm"] + grown_u / 2 * s + grown_v / 2 * c)
    assert actual["rotation_deg"] == angle
    corners = _scene_polygon_mm(dialog, item)
    length_vector = corners[1] - corners[0]
    width_vector = corners[2] - corners[1]
    assert math.hypot(length_vector.x(), length_vector.y()) == pytest.approx(actual["length_mm"])
    assert math.hypot(width_vector.x(), width_vector.y()) == pytest.approx(actual["width_mm"])
    assert length_vector.x() * width_vector.x() + length_vector.y() * width_vector.y() == pytest.approx(0, abs=1e-9)
    assert math.degrees(math.atan2(length_vector.y(), length_vector.x())) == pytest.approx(angle)
    # The opposite corner (-1, -1) is the anchor of a corner stroke.
    assert (corners[0].x(), corners[0].y()) == pytest.approx((anchor.x(), anchor.y()))
    for key in ("length_mm", "width_mm", "x_mm", "y_mm"):
        assert dialog.fields[key].value() == pytest.approx(actual[key], abs=1e-6)


def test_corner_handle_at_37_degrees_moving_along_one_local_axis_leaves_the_other(dialog, app):
    """@brief Negative: a corner stroke parallel to the local length axis does not touch width.

    The screen displacement is chosen along the rotated u direction, so its
    projection on v is zero: only length_mm changes, even though the stroke
    has both a horizontal and a vertical screen component.
    """
    angle = 37
    dialog.component_list.setCurrentRow(1)
    dialog.fields["rotation_deg"].setValue(angle)
    item = dialog._items[1]
    before = deepcopy(item.component)
    # 2 mm along +u in board mm, converted through the anisotropic scales to
    # a viewport offset (the view is at fit scale, so scene px = crop px).
    u_mm = 2.0
    scene_offset = dialog.photo_transform.local_to_item_pixel(angle, u_mm, 0)
    scale = dialog.view.transform().m11()
    offset = QPoint(round(scene_offset.x() * scale), round(scene_offset.y() * scale))
    displacement = _drag_handle(dialog, app, item, (1, 1), offset)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    dx_mm, dy_mm = displacement.x() / 10, displacement.y() / 5
    grown_u = dx_mm * c + dy_mm * s
    grown_v = -dx_mm * s + dy_mm * c
    # Rounding to whole viewport pixels leaves a sub-pixel v component.
    assert abs(grown_v) < .05 < grown_u
    actual = dialog.get_components()[1]
    assert actual["length_mm"] == pytest.approx(before["length_mm"] + grown_u)
    assert actual["width_mm"] == pytest.approx(before["width_mm"] + grown_v)
    assert abs(actual["width_mm"] - before["width_mm"]) < .05


@pytest.mark.parametrize("angle", [90, 37])
def test_width_floor_holds_at_rotation(dialog, app, angle):
    """@brief Pulling the (0, 1) width grip through the part at any rotation stops at the floor.

    The clamp is applied in the LOCAL frame: the stroke is aimed along the
    rotated -v direction on screen so it crosses the opposite width edge,
    and width_mm ends exactly at MIN_HANDLE_SIZE_MM with the far edge fixed;
    length_mm and the rotation are untouched.
    """
    from kicadgen.gui.photo_review_dialog import MIN_HANDLE_SIZE_MM
    dialog.component_list.setCurrentRow(1)
    dialog.fields["rotation_deg"].setValue(angle)
    item = dialog._items[1]
    before = deepcopy(item.component)
    far = _scene_polygon_mm(dialog, item)[0]      # on the (-1, -1) corner: the -v edge
    scene_offset = dialog.photo_transform.local_to_item_pixel(angle, 0, -20.0)
    scale = dialog.view.transform().m11()
    _drag_handle(dialog, app, item, (0, 1),
                 QPoint(round(scene_offset.x() * scale), round(scene_offset.y() * scale)))
    actual = dialog.get_components()[1]
    assert actual["width_mm"] == pytest.approx(MIN_HANDLE_SIZE_MM)
    assert actual["length_mm"] == before["length_mm"]
    assert actual["rotation_deg"] == angle
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    # Center moved from the old center towards the -v edge by (w_before - floor) / 2.
    shift = (before["width_mm"] - MIN_HANDLE_SIZE_MM) / 2
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + shift * s)
    assert actual["y_mm"] == pytest.approx(before["y_mm"] - shift * c)
    corners = _scene_polygon_mm(dialog, item)
    assert (corners[0].x(), corners[0].y()) == pytest.approx((far.x(), far.y()))
    width_vector = corners[2] - corners[1]
    assert math.hypot(width_vector.x(), width_vector.y()) == pytest.approx(MIN_HANDLE_SIZE_MM)
    assert dialog.fields["width_mm"].value() == pytest.approx(MIN_HANDLE_SIZE_MM, abs=1e-6)


def test_selection_halo_stacks_behind_and_handles_above_the_label(dialog):
    """@brief Z-order contract: halo behind the status outline, grips above the number label.

    The halo must never cover the status-coloured pen it highlights, and the
    top-left grip must stay grabbable where the number label sits.
    """
    dialog.component_list.setCurrentRow(0)
    item = dialog._items[0]
    assert item.halo.flags() & QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent
    assert item.halo.parentItem() is item
    assert item.halo.isVisible()
    for handle in item.handles.values():
        assert handle.parentItem() is item
        assert handle.zValue() > item.number.zValue()
        assert handle.isVisible()
    # Negative: the outline itself and the number label do not stack behind
    # the parent, otherwise the photo would hide them.
    assert not (item.flags() & QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent)
    assert not (item.number.flags() & QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent)
    # A selected outline is drawn above unselected neighbours.
    assert item.zValue() > dialog._items[1].zValue()


def test_handle_cursors_follow_the_on_screen_edge_direction(dialog):
    """@brief Cursors are chosen from the rotated, anisotropically scaled normal."""
    dialog.component_list.setCurrentRow(1)
    item = dialog._items[1]
    assert item.handle((1, 0)).cursor().shape() == Qt.CursorShape.SizeHorCursor
    assert item.handle((0, 1)).cursor().shape() == Qt.CursorShape.SizeVerCursor
    # Top-right corner: normal (+10, -5) px points up-right, the "/" diagonal.
    assert item.handle((1, -1)).cursor().shape() == Qt.CursorShape.SizeBDiagCursor
    assert item.handle((-1, -1)).cursor().shape() == Qt.CursorShape.SizeFDiagCursor
    dialog.fields["rotation_deg"].setValue(90)
    assert item.handle((1, 0)).cursor().shape() == Qt.CursorShape.SizeVerCursor
    assert item.handle((0, 1)).cursor().shape() == Qt.CursorShape.SizeHorCursor


def test_the_armed_pointer_survives_a_pan_and_goes_back_to_normal_when_disarmed(dialog, app):
    """@brief The crosshair says the mode is armed, so it must not lie after a pan.

    Panning borrows the viewport cursor; when it gives it back the pointer has
    to say again what the next left drag will do.
    """
    assert dialog.view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    assert dialog.view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    centre = dialog.view.viewport().rect().center()
    _drag(app, dialog.view, centre, centre + QPoint(20, 10), Qt.MouseButton.MiddleButton)
    assert dialog.draw_mode()
    assert dialog.view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    dialog.set_draw_mode(False)
    assert dialog.view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor


# --------------------------------------------------------------------------
# Backdrop of the canvas: the photograph whole, attenuated or taken away
# --------------------------------------------------------------------------

def _toolbar_buttons(dialog):
    """Buttons of the top toolbar, in the order the user reads them."""
    layout = dialog.layout()
    for index in range(layout.count()):
        row = layout.itemAt(index).layout()
        if row is None:
            continue
        widgets = [row.itemAt(position).widget() for position in range(row.count())]
        if dialog.fit_button in widgets:
            return [widget for widget in widgets if widget is not None]
    raise AssertionError("the toolbar holding the view controls was not found")


def _view_image(dialog, app):
    """Picture of the canvas exactly as the user sees it, backdrop included."""
    app.processEvents()
    return dialog.view.grab().toImage()


def _region(dialog, item):
    """Viewport rectangle around one outline, with room for the pen and halo."""
    return dialog.view.mapFromScene(item.photo_bounds()).boundingRect().adjusted(-8, -8, 8, 8)


def _pixels_near(image, rect, color, tolerance=40):
    """How many pixels of a region are within ``tolerance`` of a colour, per channel."""
    found = 0
    for y in range(max(0, rect.top()), min(image.height() - 1, rect.bottom()) + 1):
        for x in range(max(0, rect.left()), min(image.width() - 1, rect.right()) + 1):
            pixel = image.pixelColor(x, y)
            if (abs(pixel.red() - color.red()) <= tolerance
                    and abs(pixel.green() - color.green()) <= tolerance
                    and abs(pixel.blue() - color.blue()) <= tolerance):
                found += 1
    return found


def _outline_pixels(dialog, app):
    """Pixels drawn in each outline's own status colour, one count per outline."""
    image = _view_image(dialog, app)
    return [_pixels_near(image, _region(dialog, item), item.pen().color())
            for item in dialog._items]


def _scene_geometry(dialog):
    """Every outline's scene polygon and envelope, the geometry an edit would move."""
    return [([(point.x(), point.y()) for point in item.mapToScene(item.polygon())],
             item.photo_bounds().getCoords())
            for item in dialog._items]


def _relative_luminance(color):
    """WCAG relative luminance of a QColor, sRGB channels linearised."""
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(value / 12.92 if value <= .03928 else ((value + .055) / 1.055) ** 2.4)
    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]


def _contrast_ratio(first, second):
    """WCAG contrast ratio between two colours, 1.0 (identical) to 21.0."""
    light, dark = sorted((_relative_luminance(QColor(first)), _relative_luminance(QColor(second))))
    return (dark + .05) / (light + .05)


def test_the_backdrop_control_is_a_view_control_and_cycles_three_states(dialog, app):
    """@brief One button next to fit and zoom, cycling photograph -> attenuated -> hidden.

    It is a view control, so it belongs after the zoom buttons and not next to
    the editing ones; it states the state it is in, and stays visibly pressed
    while the canvas is not showing the real photograph, so a hidden board is
    never mistaken for an empty one.
    """
    buttons = _toolbar_buttons(dialog)
    assert dialog.photo_button in buttons
    assert buttons.index(dialog.photo_button) > buttons.index(dialog.zoom_out_button)
    assert buttons.index(dialog.photo_button) > buttons.index(dialog.delete_button)
    assert dialog.photo_button.isCheckable()
    assert dialog.photo_state() == PHOTO_FULL
    assert dialog.photo_button.text() == "Foto: intera"
    assert not dialog.photo_button.isChecked()
    QTest.mouseClick(dialog.photo_button, Qt.MouseButton.LeftButton)
    assert dialog.photo_state() == PHOTO_DIMMED
    assert dialog.photo_button.text() == "Foto: attenuata"
    assert dialog.photo_button.isChecked()
    QTest.mouseClick(dialog.photo_button, Qt.MouseButton.LeftButton)
    assert dialog.photo_state() == PHOTO_HIDDEN
    assert dialog.photo_button.text() == "Foto: nascosta"
    assert dialog.photo_button.isChecked()
    # Negative: the cycle closes, it does not get stuck on the last state.
    QTest.mouseClick(dialog.photo_button, Qt.MouseButton.LeftButton)
    assert dialog.photo_state() == PHOTO_FULL
    assert not dialog.photo_button.isChecked()


def test_hiding_the_photograph_keeps_every_outline_in_place_and_drawn(dialog, app):
    """@brief The geometry does not move and the outlines are still painted.

    This is the whole point of the control: on a crowded board a thin outline
    over a dark photograph is unreadable, so the photograph goes away — but an
    outline that moved, or that stopped being drawn, would make the review
    worse than the problem it fixes. Both are measured: the scene geometry is
    compared before and after, and the canvas is rendered and counted.
    """
    dialog.component_list.setCurrentRow(0)
    before_geometry = _scene_geometry(dialog)
    before_pixels = _outline_pixels(dialog, app)
    assert all(count > 0 for count in before_pixels)
    dialog.set_photo_state(PHOTO_HIDDEN)
    assert not dialog.photo_item.isVisible()
    assert dialog.board_backdrop.isVisible()
    assert _scene_geometry(dialog) == before_geometry
    assert _outline_pixels(dialog, app) == before_pixels
    image = _view_image(dialog, app)
    board = dialog.view.mapFromScene(dialog.photo_rect).boundingRect()
    # Negative: not one pixel of the board still shows the photograph itself.
    assert _pixels_near(image, board, QColor("#243b28"), tolerance=6) == 0
    # ...and what replaced it is the declared neutral backdrop.
    assert _pixels_near(image, board, QColor(BOARD_BACKDROP_COLOR), tolerance=2) > 0
    assert image.pixelColor(dialog.view.mapFromScene(QPointF(360, 15))) == QColor(BOARD_BACKDROP_COLOR)
    dialog.set_photo_state(PHOTO_FULL)
    assert dialog.photo_item.isVisible()
    assert dialog.photo_item.opacity() == 1.0
    assert not dialog.board_backdrop.isVisible()
    assert _scene_geometry(dialog) == before_geometry


def test_the_attenuated_photograph_fades_into_the_neutral_backdrop(dialog, app):
    """@brief The middle state keeps the board as context instead of removing it.

    Attenuating is the state that keeps the review possible — whether an
    outline sits on its component is only judgeable against the board — so the
    photograph must still be there, but composited onto the neutral backdrop
    and therefore much closer to it than to itself.
    """
    with_photograph = _outline_pixels(dialog, app)
    dialog.set_photo_state(PHOTO_DIMMED)
    assert dialog.photo_item.isVisible()
    assert dialog.photo_item.opacity() == PHOTO_DIMMED_OPACITY
    assert dialog.board_backdrop.isVisible()
    assert _outline_pixels(dialog, app) == with_photograph
    image = _view_image(dialog, app)
    pixel = image.pixelColor(dialog.view.mapFromScene(QPointF(360, 15)))

    def distance(color):
        return (abs(pixel.red() - color.red()) + abs(pixel.green() - color.green())
                + abs(pixel.blue() - color.blue()))

    photograph, backdrop = QColor("#243b28"), QColor(BOARD_BACKDROP_COLOR)
    assert distance(backdrop) * 3 < distance(photograph), (
        "the attenuated board must sit close to the backdrop, not to the photograph")
    # Negative: attenuated is not hidden — the board is still being shown.
    assert distance(backdrop) > 0


def test_the_backdrop_contrasts_with_every_outline_colour(dialog):
    """@brief Measured contrast of the backdrop against the pens it must not swallow.

    The status colours carry meaning and are explained in the legend, so the
    backdrop is chosen around them, never the reverse. Every status colour and
    the white selection halo clear a 3:1 WCAG contrast ratio against it, the
    board border clears it too, and the backdrop is lighter than the empty
    canvas around it so the board area stays a readable region.
    """
    status_colours = {"#40dc80", "#ffad33", "#ef5350"}
    assert {dialog._items[0].pen().color().name(),
            dialog._items[1].pen().color().name()} <= status_colours
    for colour in status_colours | {SELECTION_HALO_COLOR, BOARD_BORDER_COLOR}:
        assert _contrast_ratio(colour, BOARD_BACKDROP_COLOR) >= 3.0, colour
    assert (_relative_luminance(QColor(BOARD_BACKDROP_COLOR))
            > _relative_luminance(dialog.view.backgroundBrush().color()))
    # Negative: a photograph offers no such guarantee — a bright shield or a
    # white silkscreen area defeats the orange pen wherever it happens to be,
    # which is exactly why a known backdrop is needed.
    assert _contrast_ratio("#ffad33", "#ffffff") < 3.0


def test_the_number_labels_stay_legible_without_the_photograph(dialog, app):
    """@brief The ID labels keep their own opaque backing when the photo goes.

    The labels are drawn over the photograph; with it gone they must not melt
    into the backdrop. The backing is darker than the backdrop and the white
    text clears the 4.5:1 ratio for body text on it.
    """
    assert _contrast_ratio("#ffffff", NUMBER_LABEL_BACKING_COLOR) >= 4.5
    assert _contrast_ratio(NUMBER_LABEL_BACKING_COLOR, BOARD_BACKDROP_COLOR) > 1.4
    assert (_relative_luminance(QColor(NUMBER_LABEL_BACKING_COLOR))
            < _relative_luminance(QColor(BOARD_BACKDROP_COLOR)))
    dialog.set_photo_state(PHOTO_HIDDEN)
    image = _view_image(dialog, app)
    for item in dialog._items:
        label = dialog.view.mapFromScene(
            item.number.mapToScene(item.number.boundingRect())).boundingRect()
        assert _pixels_near(image, label, QColor(NUMBER_LABEL_BACKING_COLOR), tolerance=12) > 0
        assert _pixels_near(image, label, QColor("white"), tolerance=12) > 0


def test_the_board_rectangle_is_still_drawn_and_stays_inert(dialog, app):
    """@brief With the pixmap gone the PCB edge is the only spatial reference left.

    It is drawn at the photograph's own rectangle, in a grey that no status
    colour uses, below everything else, and it takes no mouse button: clicking
    "on the board" still reaches the outlines and the drawing gesture.
    """
    assert dialog.board_backdrop.rect() == dialog.photo_rect
    assert dialog.board_backdrop.pen().color() == QColor(BOARD_BORDER_COLOR)
    assert dialog.board_backdrop.brush().color() == QColor(BOARD_BACKDROP_COLOR)
    assert dialog.board_backdrop.zValue() < dialog.photo_item.zValue()
    assert dialog.board_backdrop.acceptedMouseButtons() == Qt.MouseButton.NoButton
    dialog.set_photo_state(PHOTO_HIDDEN)
    image = _view_image(dialog, app)
    edge = dialog.view.mapFromScene(dialog.photo_rect).boundingRect().adjusted(-3, -3, 3, 3)
    assert _pixels_near(image, edge, QColor(BOARD_BORDER_COLOR), tolerance=30) > 0
    # "Fuori dai bordi PCB" stays judgeable with the photograph gone.
    dialog.component_list.setCurrentRow(0)
    dialog.fields["x_mm"].setValue(20)
    assert dialog._items[0].pen().color().name() == "#ef5350"
    assert "Fuori" in dialog.warning_label.text()


def test_the_saved_inventory_is_identical_whatever_the_backdrop(dialog, app, components):
    """@brief Negative: the backdrop is a view setting and must not leak into the data.

    The working copy is compared byte for byte across the three states, and
    Apply emits exactly what it emits with the photograph shown.
    """
    reference = repr(dialog.get_components()).encode()
    for state in PHOTO_STATES:
        dialog.set_photo_state(state)
        app.processEvents()
        assert repr(dialog.get_components()).encode() == reference
    dialog.set_photo_state(PHOTO_HIDDEN)
    assert repr(dialog.get_components()).encode() == reference
    spy = QSignalSpy(dialog.components_applied)
    dialog.accept()
    assert len(spy) == 1
    assert spy[0][0] == components


def test_apply_emits_the_same_bytes_with_the_photograph_hidden(app, png, components):
    """@brief Negative: two identical reviews, two backdrops, one identical result.

    The same inventory is applied by two editors that differ only in what the
    canvas is showing; what reaches the caller is compared byte for byte.
    """
    emitted = []
    for state in (PHOTO_FULL, PHOTO_HIDDEN):
        widget = PhotoReviewDialog(png, components, 40, 20)
        try:
            widget.set_photo_state(state)
            spy = QSignalSpy(widget.components_applied)
            widget.accept()
            assert len(spy) == 1
            emitted.append(repr(spy[0][0]).encode())
        finally:
            widget.close()
    assert emitted[0] == emitted[1]


def test_the_backdrop_state_survives_every_edit_and_the_parameter_modal(dialog, app):
    """@brief The state is not a casualty of adding, deleting, filtering, zooming or fitting.

    A control that silently reset itself on the next edit would be useless on
    the board that motivates it, where the user hides the photograph precisely
    to go and fix the outlines that were lost under it.
    """
    dialog.set_photo_state(PHOTO_HIDDEN)

    def still_hidden(step):
        assert dialog.photo_state() == PHOTO_HIDDEN, step
        assert not dialog.photo_item.isVisible(), step
        assert dialog.board_backdrop.isVisible(), step
        assert dialog.photo_button.text() == "Foto: nascosta", step

    dialog.add_component()
    still_hidden("add")
    dialog.open_parameters()
    app.processEvents()
    still_hidden("parameters open")
    dialog.parameters_dialog.accept()
    app.processEvents()
    still_hidden("parameters closed")
    _draw_new_component(app, dialog, (100, 30), (160, 60))
    still_hidden("draw")
    dialog.category_filter.setCurrentIndex(1)
    still_hidden("filter")
    dialog.component_list.setCurrentRow(0)
    QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    still_hidden("delete")
    dialog.zoom_in_button.click()
    dialog.zoom_out_button.click()
    still_hidden("zoom")
    dialog.view.zoom(4)
    app.processEvents()
    scrolled = dialog.view.horizontalScrollBar().value()
    _drag(app, dialog.view, QPoint(100, 100), QPoint(150, 110), Qt.MouseButton.MiddleButton)
    assert dialog.view.horizontalScrollBar().value() == scrolled - 50
    still_hidden("pan")
    dialog.fit_button.click()
    app.processEvents()
    still_hidden("fit")
    dialog.resize(900, 680)
    QTest.qWait(20)
    still_hidden("resize")


def test_a_new_editor_opens_on_the_photograph(app, png, components):
    """@brief Negative: the state does not survive the dialog; a review starts from the photo."""
    first = PhotoReviewDialog(png, components, 40, 20)
    try:
        first.set_photo_state(PHOTO_HIDDEN)
        assert first.photo_state() == PHOTO_HIDDEN
    finally:
        first.close()
    second = PhotoReviewDialog(png, components, 40, 20)
    try:
        assert second.photo_state() == PHOTO_FULL
        assert second.photo_item.isVisible()
        assert not second.board_backdrop.isVisible()
        assert second.photo_button.text() == "Foto: intera"
    finally:
        second.close()


def test_an_unknown_backdrop_state_is_refused(dialog):
    """@brief Negative: an unknown state would leave button and canvas disagreeing."""
    dialog.set_photo_state(PHOTO_DIMMED)
    with pytest.raises(ValueError):
        dialog.set_photo_state("transparent")
    assert dialog.photo_state() == PHOTO_DIMMED
    assert dialog.photo_button.text() == "Foto: attenuata"


def test_selecting_and_moving_an_outline_still_work_with_the_photograph_hidden(dialog, app):
    """@brief The scene stays fully usable: the same drag as with the photograph shown."""
    dialog.set_photo_state(PHOTO_HIDDEN)
    dialog.view.zoom(2)
    item = dialog._items[1]
    start = dialog.view.mapFromScene(item.pos())
    end = start + QPoint(35, 15)
    displacement = dialog.view.mapToScene(end) - dialog.view.mapToScene(start)
    before = deepcopy(item.component)
    _drag(app, dialog.view, start, end, Qt.MouseButton.LeftButton)
    assert dialog.component_list.currentRow() == 1
    assert item.isSelected() and item.halo.isVisible()
    actual = dialog.get_components()[1]
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + displacement.x() / 10)
    assert actual["y_mm"] == pytest.approx(before["y_mm"] + displacement.y() / 5)
    assert all(handle.isVisible() for handle in item.handles.values())


def test_resizing_by_grip_still_works_with_the_photograph_hidden(dialog, app):
    """@brief The eight grips keep editing the local axes without the photograph."""
    dialog.set_photo_state(PHOTO_HIDDEN)
    dialog.component_list.setCurrentRow(1)
    item = dialog._items[1]
    before = deepcopy(item.component)
    displacement = _drag_handle(dialog, app, item, (1, 0), QPoint(30, 7))
    grown = displacement.x() / 10
    assert grown > 0
    actual = dialog.get_components()[1]
    assert actual["length_mm"] == pytest.approx(before["length_mm"] + grown)
    assert actual["x_mm"] == pytest.approx(before["x_mm"] + grown / 2)
    assert (actual["y_mm"], actual["width_mm"]) == (before["y_mm"], before["width_mm"])
    assert dialog.photo_state() == PHOTO_HIDDEN


def test_drawing_a_new_rectangle_still_works_with_the_photograph_hidden(dialog, app):
    """@brief The drawing gesture is bounded by the board, not by the pixmap being shown."""
    dialog.set_photo_state(PHOTO_HIDDEN)
    _draw_new_component(app, dialog, (100, 30), (160, 60))
    assert len(dialog._items) == 3
    drawn = dialog.get_components()[2]
    assert drawn["x_mm"] == pytest.approx(-7, abs=.3)
    assert drawn["y_mm"] == pytest.approx(-1, abs=.3)
    assert drawn["length_mm"] == pytest.approx(6, abs=.3)
    assert drawn["width_mm"] == pytest.approx(6, abs=.3)
    # Negative: the board still bounds the gesture, the empty canvas is not board.
    before = deepcopy(dialog.get_components())
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
    start = dialog.view.mapFromScene(QPointF(200, -30))
    _drag(app, dialog.view, start, start + QPoint(40, 40), Qt.MouseButton.LeftButton)
    assert dialog.get_components() == before
