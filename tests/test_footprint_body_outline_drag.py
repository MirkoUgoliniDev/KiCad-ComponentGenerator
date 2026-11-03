"""@brief The package body outline moved and resized with the mouse (user, 2026-11-03).

Agreed behaviour (HANDOFF 03/11): the outline stroke moves the body, eight
handles resize it (a corner two sides, a side one, the opposite one still),
Shift held during the drag resizes symmetrically about the centre; the grid
magnet of the pads applies; Escape cancels; a drop is ONE draft step. The
footprint origin stays the body centre, so a moved body shifts the pads,
drawings and placed texts by minus the centre shift and the camera follows:
on screen only the outline moves. Only a declared body square to the screen
with origin «body_center» is editable. Each rule has a positive and a
negative case that differ structurally.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from kicadgen.core.package_geometry import PackageGeometry, PinSpec  # noqa: E402

from test_footprint_pad_drag import PX, _show, top  # noqa: E402

PINS = [PinSpec("1", -6.0, -6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle"),
        PinSpec("2", 6.0, -6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle"),
        PinSpec("3", 6.0, 6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle"),
        PinSpec("4", -6.0, 6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle")]


def _package(**changes):
    values = dict(family="soic", body_length_mm=22.0, body_width_mm=16.0, pins=list(PINS))
    values.update(changes)
    return PackageGeometry(**values)


@pytest.fixture
def body(window, qapp, monkeypatch):
    return _show(window, qapp, monkeypatch, _package())


def vp(window, x_mm, y_mm):
    return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))


def drag(window, start, end):
    viewport = window.preview_view.viewport()
    QTest.mouseMove(viewport, start)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(viewport, (start + end) / 2)
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


def size(window):
    return (window.package_geometry.body_length_mm, window.package_geometry.body_width_mm)


def test_dragging_a_corner_resizes_two_sides_and_keeps_the_opposite_corner(body):
    window = body
    before = top(window)
    undo = len(window._fp_drawings_undo)
    drag(window, vp(window, 11.0, 8.0), vp(window, 13.0, 10.0))
    assert size(window) == pytest.approx((24.0, 18.0))
    # Opposite corner (-11, -8) unchanged: the centre moved by (+1, +1), so
    # the pads are now 1 mm further left and up in the body frame.
    assert top(window)["1"] == pytest.approx((-7.0, -7.0))
    assert {k: (x + 1, y + 1) for k, (x, y) in top(window).items()} == pytest.approx(before)
    assert len(window._fp_drawings_undo) == undo + 1 and window.has_pending_footprint_edits()
    window._undo_footprint_drawing()
    assert size(window) == (22.0, 16.0) and top(window) == before


def test_dragging_a_side_changes_one_size_only(body):
    window = body
    drag(window, vp(window, 0.0, 8.0), vp(window, 3.0, 11.0))
    assert size(window) == pytest.approx((22.0, 19.0)), "the bottom side moves only in Y"


def test_dragging_the_outline_moves_the_body_and_not_its_size(body):
    window = body
    before = top(window)
    # On the stroke, away from the side handle at (-11, 0).
    drag(window, vp(window, -11.0, 4.0), vp(window, -9.0, 5.0))
    assert size(window) == (22.0, 16.0)
    assert top(window)["1"] == pytest.approx((before["1"][0] - 2.0, before["1"][1] - 1.0))


def test_a_press_inside_the_body_moves_nothing(body):
    """Negative: only the stroke moves the body; the inside pans as the background."""
    window = body
    before = (size(window), top(window))
    drag(window, vp(window, 0.0, 0.0), vp(window, 2.0, 2.0))
    assert (size(window), top(window)) == before and not window.has_pending_footprint_edits()


def test_shift_resizes_symmetrically_about_the_centre(body, monkeypatch):
    from PyQt6.QtWidgets import QApplication
    monkeypatch.setattr(QApplication, "keyboardModifiers",
                        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier))
    window = body
    before = top(window)
    drag(window, vp(window, 11.0, 0.0), vp(window, 12.0, 0.0))
    assert size(window) == pytest.approx((24.0, 16.0))
    assert top(window) == before, "the centre stayed: no pad shifted"


def test_escape_cancels_the_drag(body):
    window = body
    viewport = window.preview_view.viewport()
    start, end = vp(window, 11.0, 8.0), vp(window, 14.0, 11.0)
    QTest.mouseMove(viewport, start)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(viewport, end)
    QTest.keyClick(window.preview_view, Qt.Key.Key_Escape)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    assert size(window) == (22.0, 16.0) and not window.has_pending_footprint_edits()


def test_a_body_whose_origin_is_not_its_centre_cannot_be_dragged(window, qapp, monkeypatch):
    """Negative: with origin «pin1» the body centre is only estimated, so the outline stays still."""
    _show(window, qapp, monkeypatch, _package(origin="pin1"))
    assert not window._body_outline_editable(window.package_geometry)
    from kicadgen.gui.footprint_view import BODY_PART_ROLE
    assert not [item for item in window.preview_scene.items() if item.data(BODY_PART_ROLE)]


def test_the_body_never_shrinks_below_the_minimum(body):
    from kicadgen.gui.main_window import MIN_LENGTH_MM
    window = body
    drag(window, vp(window, 11.0, 0.0), vp(window, -30.0, 0.0))
    assert window.package_geometry.body_length_mm == pytest.approx(MIN_LENGTH_MM)


def test_a_moved_body_carries_drawings_and_placed_texts_with_the_pads(body):
    window = body
    window.footprint_drawings = [{"kind": "line", "layer": "F.SilkS", "width": 0.12,
                                  "points": [[-6.0, -8.0], [6.0, -8.0]]}]
    window.footprint_text_positions = {"Value": (0.0, 10.0)}
    window._draw_preview()
    drag(window, vp(window, -11.0, 4.0), vp(window, -10.0, 4.0))
    assert window.footprint_drawings[0]["points"] == [[-7.0, -8.0], [5.0, -8.0]]
    assert window.footprint_text_positions["Value"] == pytest.approx((-1.0, 10.0))
