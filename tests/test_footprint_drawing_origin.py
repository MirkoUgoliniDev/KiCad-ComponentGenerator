"""@brief Drawing origin of the footprint preview (user, 2026-11-02).

«mi basta come riferimento per il disegno» (a reference for the drawing is
enough): the user picks a body corner, the body centre, the footprint origin
or a pad; the preview shows a marker there, passes the grid through it, reads
the pointer coordinates from it with Y UP and lets the pad dialog type a pad
position from it. The .kicad_mod never changes and the setting is not a draft
edit: it is saved with the project at once.
"""
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QMenu

from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.package import build_package
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import QApplication, gui

PX = gui._FP_PREVIEW_MM_TO_PX


def _relay(orientation=0.0, origin="body_center"):
    """A five-pin THT part with a 22.5 x 16.5 mm body, like the relay of the screenshot."""
    def tht(number, x, y):
        return PinSpec(number, x, y, 1.6, 1.6, pad_type="thru_hole", shape="circle", drill_mm=1.0)
    return PackageGeometry(
        family="custom", body_length_mm=22.5, body_width_mm=16.5, orientation_deg=orientation,
        origin=origin,
        pins=[tht("14", -4.0, -6.0), tht("12", -4.0, 6.0), tht("A1", 6.0, -7.0),
              tht("A2", 6.0, 7.0), tht("11", 8.0, 0.0)])


def _corner(window, name):
    """Key of the body corner the window names `name`."""
    return next(key for key in window._fp_origin_points
                if key.startswith("corner:") and window._footprint_origin_name(key) == name)


def _draw(window, package):
    window.package_geometry = package
    window._draw_preview_package(package)
    QApplication.processEvents()


def _file(package):
    """The .kicad_mod text without its item UUIDs (regenerated at every render)."""
    import re
    return re.sub(r'\(uuid "[^"]+"\)', "(uuid)", build_package(package).render())


def test_default_origin_is_the_footprint_origin_with_marker_and_readout(window):
    """@brief Positive: marker at (0, 0) and readout from «origine del footprint»; negative: no footprint, no readout."""
    _draw(window, _relay())
    view = window.preview_view
    assert window.footprint_drawing_origin == gui.FP_ORIGIN_DEFAULT
    assert view.drawing_origin() == QPointF(0.0, 0.0)
    text = window._footprint_readout_text(QPointF(2 * PX, -3 * PX))
    assert text.startswith("X 2.00  Y 3.00 mm  |  ") and text.endswith("%  |  da: origine del footprint")
    window._preview_params = lambda: None
    window._draw_preview()
    assert view.drawing_origin() is None
    # No footprint: the zoom only, as the symbol editor before the pointer comes.
    assert window._footprint_readout_text(QPointF(0, 0)).endswith("%")
    assert "da:" not in window._footprint_readout_text(QPointF(0, 0))


def test_a_body_corner_moves_marker_grid_and_readout_but_never_the_footprint(window):
    """@brief Positive: bottom-left corner -> pad 14 reads (7.25, 14.25) with Y up; negative: file and draft unchanged."""
    package = _relay()
    _draw(window, package)
    before_file = _file(package)
    undo_before = list(window._fp_drawings_undo)
    corner = _corner(window, "spigolo in basso a sinistra")
    assert window._fp_origin_points[corner] == pytest.approx((-11.25, 8.25))
    window._set_footprint_drawing_origin(corner)
    view = window.preview_view
    assert view.drawing_origin() == QPointF(-11.25 * PX, 8.25 * PX)
    # The grid passes through the chosen point.
    assert view._grid[1] == pytest.approx((-11.25 * PX, 8.25 * PX))
    # Pad 14 at (-4, -6) Y down: 7.25 right of the corner and 14.25 ABOVE it.
    assert window._to_drawing_mm(-4.0, -6.0) == pytest.approx((7.25, 14.25))
    text = window._footprint_readout_text(QPointF(-4.0 * PX, -6.0 * PX))
    assert text.startswith("X 7.25  Y 14.25 mm  |  ") and text.endswith("da: spigolo in basso a sinistra")
    assert _file(window.package_geometry) == before_file
    assert not window.has_pending_footprint_edits()
    assert window._fp_drawings_undo == undo_before
    # Back to the footprint origin: the grid goes back to the drawer's origin.
    window._set_footprint_drawing_origin(gui.FP_ORIGIN_DEFAULT)
    assert view.drawing_origin() == QPointF(0.0, 0.0)
    assert view._grid[1] == pytest.approx(tuple(v * PX for v in window._preview_grid_spec["origin"]))


def test_corners_are_named_by_where_the_user_sees_them_on_a_turned_body(window):
    """@brief Positive: at 45 degrees a corner straight above the centre is «spigolo in alto»; negative: no «in alto a sinistra» then."""
    _draw(window, _relay(orientation=45.0))
    names = {window._footprint_origin_name(key) for key in window._fp_origin_points if key.startswith("corner:")}
    assert "spigolo in alto a sinistra" not in names
    _draw(window, _relay(orientation=0.0))
    names = {window._footprint_origin_name(key) for key in window._fp_origin_points if key.startswith("corner:")}
    assert names == {"spigolo in alto a sinistra", "spigolo in alto a destra",
                     "spigolo in basso a sinistra", "spigolo in basso a destra"}


def test_a_pad_origin_that_disappears_falls_back_but_comes_back(window):
    """@brief Positive: pad 11 as origin follows the pad; negative: without pad 11 the footprint origin is used, setting kept."""
    _draw(window, _relay())
    window._set_footprint_drawing_origin("pad:11")
    assert window.preview_view.drawing_origin() == QPointF(8.0 * PX, 0.0)
    without = _relay()
    without = PackageGeometry(**{**without.__dict__, "pins": without.pins[:-1]})
    _draw(window, without)
    assert window.footprint_drawing_origin == "pad:11"
    assert window.preview_view.drawing_origin() == QPointF(0.0, 0.0)
    assert window._footprint_readout_text(QPointF(0, 0)).endswith("da: origine del footprint")
    # Review 2026-11-02 (2): the unresolved key keeps the drawer's grid, not (0, 0).
    assert window.preview_view._grid[1] == pytest.approx(
        tuple(v * PX for v in window._preview_grid_spec["origin"]))
    _draw(window, _relay())
    assert window.preview_view.drawing_origin() == QPointF(8.0 * PX, 0.0)


def test_right_click_on_the_background_offers_the_nearest_point(window, monkeypatch):
    """@brief Positive: a right click near the bottom-left corner sets it; negative: a right click on a pad opens no origin menu."""
    window.show()
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    _draw(window, _relay())
    offered = []

    def first_action(menu, *_args):
        actions = [a for a in menu.actions() if not a.isSeparator()]
        offered.append([a.text() for a in actions])
        return actions[0]

    monkeypatch.setattr(QMenu, "exec", first_action)
    pads = []
    monkeypatch.setattr(window, "_edit_preview_pad", pads.append)
    window.preview_view.pad_context_requested.disconnect()
    window.preview_view.pad_context_requested.connect(lambda token: pads.append(token))
    view = window.preview_view
    near_corner = view.mapFromScene(QPointF(-11.0 * PX, 8.0 * PX))
    QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, pos=near_corner)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.RightButton, pos=near_corner)
    assert offered and offered[-1][0] == "Imposta origine di disegno: spigolo in basso a sinistra"
    assert window.footprint_drawing_origin == _corner(window, "spigolo in basso a sinistra")
    count = len(offered)
    on_pad = view.mapFromScene(QPointF(8.0 * PX, 0.0))
    QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, pos=on_pad)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.RightButton, pos=on_pad)
    assert len(offered) == count
    assert pads


def test_the_origin_is_saved_with_the_project_and_a_bad_value_falls_back(window):
    """@brief Positive: the key reaches the project state; negative: a non-string restores the default."""
    _draw(window, _relay())
    window._set_footprint_drawing_origin("pad:12")
    assert window._project_state()["footprint_drawing_origin"] == "pad:12"
    state = window._project_state()
    window._apply_project_state(state)
    assert window.footprint_drawing_origin == "pad:12"
    state["footprint_drawing_origin"] = 42
    window._apply_project_state(state)
    assert window.footprint_drawing_origin == gui.FP_ORIGIN_DEFAULT


def _drive_pad_dialog(monkeypatch, x=None, y=None, as_origin=None, seen=None):
    """Accept the pad dialog, setting the position fields and the origin box when asked."""
    def exec_(dialog):
        form = dialog.findChild(QFormLayout)
        spins = {form.labelForField(s).text(): s for s in dialog.findChildren(QDoubleSpinBox)
                 if form.labelForField(s) is not None}
        box = dialog.findChild(QCheckBox)
        if seen is not None:
            seen.update(x=spins["Posizione X:"].value(), y=spins["Posizione Y:"].value(),
                        enabled=spins["Posizione X:"].isEnabled(), box=box.isChecked())
        if as_origin is not None:
            box.setChecked(as_origin)
        if x is not None:
            spins["Posizione X:"].setValue(x)
        if y is not None:
            spins["Posizione Y:"].setValue(y)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", exec_)


def test_the_pad_dialog_types_a_position_from_the_drawing_origin(window, monkeypatch):
    """@brief Positive: X/Y typed from a corner land exactly (Y up), as ONE undo step; negative: an untouched dialog moves nothing.

    @note The acquired package is a bottom view, turned by 30 degrees and
        mirrored: the typed displacement goes back through all three. Its
        body centre is declared here, so nothing is estimated.
    """
    from dataclasses import replace
    acquire(window)
    window.package_geometry = replace(window.package_geometry, origin="body_center")
    window._draw_preview()
    corner = _corner(window, "spigolo in basso a sinistra")
    window._set_footprint_drawing_origin(corner)
    index = 0
    seen = {}
    _drive_pad_dialog(monkeypatch, seen=seen)
    pins_before = list(window.package_geometry.pins)
    window._edit_package_pad(index)
    assert window.package_geometry.pins == pins_before
    top = window.package_geometry.pins_seen_from_top()[index]
    assert (seen["x"], seen["y"]) == pytest.approx(window._to_drawing_mm(top.x_mm, top.y_mm), abs=5e-4)
    undo = len(window._fp_drawings_undo)
    target = (round(seen["x"] + 0.5, 3), round(seen["y"] + 1.0, 3))
    _drive_pad_dialog(monkeypatch, x=target[0], y=target[1])
    window._edit_package_pad(index)
    top = window.package_geometry.pins_seen_from_top()[index]
    assert window._to_drawing_mm(top.x_mm, top.y_mm) == pytest.approx(target, abs=1e-6)
    assert len(window._fp_drawings_undo) == undo + 1
    assert window.has_pending_footprint_edits()


@pytest.mark.parametrize("origin, recentres", [("pad", False), ("corner", True)])
def test_an_estimated_body_centre_is_said_when_a_typed_position_re_centres(window, monkeypatch, origin, recentres):
    """@brief Positive: from the other pad the typed position is exact; negative: from a body corner the estimate moves and a toast says so.

    @note The acquired package declares «pin1»: its body centre is the
        centre of the pad field (`body_frame_offset`), so moving pad 1 moves
        the body and its corners, never the distance between the pads.
    """
    acquire(window)
    window._draw_preview()
    other = window.package_geometry.pins[1].number
    key = f"pad:{other}" if origin == "pad" else _corner(window, "spigolo in basso a sinistra")
    window._set_footprint_drawing_origin(key)
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: toasts.append(title))
    seen = {}
    _drive_pad_dialog(monkeypatch, seen=seen)
    window._edit_package_pad(0)
    target = (round(seen["x"] + 1.0, 3), round(seen["y"] - 0.5, 3))
    _drive_pad_dialog(monkeypatch, x=target[0], y=target[1])
    window._edit_package_pad(0)
    top = window.package_geometry.pins_seen_from_top()[0]
    landed = window._to_drawing_mm(top.x_mm, top.y_mm)
    if recentres:
        assert landed != pytest.approx(target, abs=1e-6)
        assert toasts == ["Il disegno si è ricentrato."]
    else:
        assert landed == pytest.approx(target, abs=1e-6)
        assert toasts == []


def test_the_pad_dialog_makes_its_pad_the_origin_and_then_locks_its_position(window, monkeypatch):
    """@brief Positive: the box makes the pad the origin; negative: reopened, its position fields are disabled at 0, 0."""
    acquire(window)
    window._draw_preview()
    pin = window.package_geometry.pins[0]
    _drive_pad_dialog(monkeypatch, as_origin=True)
    window._edit_package_pad(0)
    assert window.footprint_drawing_origin == f"pad:{pin.number}"
    assert not window.has_pending_footprint_edits()
    seen = {}
    _drive_pad_dialog(monkeypatch, seen=seen)
    window._edit_package_pad(0)
    assert seen["box"] and not seen["enabled"]
    assert (seen["x"], seen["y"]) == (0.0, 0.0)


def test_the_pointer_line_follows_a_new_origin_and_keeps_the_last_point_when_the_pointer_leaves(window):
    """@brief Positive: a new origin rewrites the line at once (review 2026-11-02, 5); negative: leaving keeps the point."""
    _draw(window, _relay())
    view = window.preview_view
    view.pointer_changed.emit(QPointF(-4.0 * PX, -6.0 * PX))
    assert window.preview_readout.text().startswith("X -4.00  Y 6.00 mm")
    window._set_footprint_drawing_origin(_corner(window, "spigolo in basso a sinistra"))
    assert window.preview_readout.text().startswith("X 7.25  Y 14.25 mm")
    assert window.preview_readout.text().endswith("da: spigolo in basso a sinistra")
    view.pointer_left.emit()
    assert window.preview_readout.text().startswith("X 7.25  Y 14.25 mm")


def test_the_pointer_line_is_a_widget_under_the_canvas_so_a_pan_cannot_move_it(window):
    """@brief Positive: the line is not inside the viewport (review 2026-11-02, 1); negative: a pan leaves its geometry alone."""
    window.show()
    _draw(window, _relay())
    view = window.preview_view
    label = window.preview_readout
    assert not view.viewport().isAncestorOf(label)
    before = label.geometry()
    view.camera.pan_begin(Qt.MouseButton.MiddleButton, QPoint(100, 100), grab_mouse=False, keep_auto_fit=True)
    view.camera.pan_move(QPoint(160, 140))
    QApplication.processEvents()
    assert label.geometry() == before


def test_pads_sharing_a_number_are_distinct_origins(window):
    """@brief Positive: the second pad numbered «5» is its own origin (review 2026-11-02, 3); negative: the first keeps its key."""
    def land(x):
        return PinSpec("5", x, 0.0, 1.0, 1.0)
    _draw(window, PackageGeometry(family="custom", body_length_mm=10, body_width_mm=6,
                                  pins=[land(-2.0), land(2.0)]))
    assert window._fp_origin_points["pad:5"] == (-2.0, 0.0)
    assert window._fp_origin_points["pad:5#2"] == (2.0, 0.0)
    assert window._footprint_origin_name("pad:5#2") == "pad 5"


def test_a_failed_pin_header_build_leaves_no_stale_origin(window, monkeypatch):
    """@brief Positive: a header that cannot be built shows no marker (review 2026-11-02, 4); negative: before it, the package had one."""
    _draw(window, _relay())
    assert window.preview_view.drawing_origin() is not None

    def broken(_params):
        raise ValueError("broken header")

    monkeypatch.setattr(gui, "build_pin_header", broken)
    monkeypatch.setattr(window, "_preview_params", lambda: gui.PinHeaderParams(name="H", rows=1, cols=2))
    window._draw_preview()
    assert window.preview_view.drawing_origin() is None
    assert window._fp_origin_points == {}
