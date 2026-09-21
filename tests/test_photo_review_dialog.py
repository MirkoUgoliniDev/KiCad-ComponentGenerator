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
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from kicadgen.gui.photo_review_dialog import PhotoReviewDialog, PhotoTransform


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


def test_add_delete_renumber_and_apply_detached_result(dialog, components):
    original = deepcopy(components)
    spy = QSignalSpy(dialog.components_applied)
    QTest.mouseClick(dialog.add_button, Qt.MouseButton.LeftButton)
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
