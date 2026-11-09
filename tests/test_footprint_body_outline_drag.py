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


# ── Second review 2026-11-03 ───────────────────────────────────────────

def _click(window, pos):
    viewport = window.preview_view.viewport()
    QTest.mouseMove(viewport, pos)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


def test_a_click_on_a_handle_off_the_grid_changes_nothing(window, qapp, monkeypatch):
    """U1/V1: a body whose sides are off the 0.01 mm grid; a click is not a resize. A drag still resizes."""
    _show(window, qapp, monkeypatch, _package(body_length_mm=22.005))
    _click(window, vp(window, 11.0025, 8.0))
    assert size(window) == (22.005, 16.0) and not window.has_pending_footprint_edits()
    assert not window._fp_drawings_undo
    drag(window, vp(window, 11.0025, 8.0), vp(window, 13.0025, 8.0))
    assert window.package_geometry.body_length_mm != 22.005


def test_a_click_on_a_pad_off_the_grid_changes_nothing(window, qapp, monkeypatch):
    """V1 for pads: a pad at -6.0025 stays there after a click."""
    pins = [PinSpec("1", -6.0025, -6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle")] + PINS[1:]
    _show(window, qapp, monkeypatch, _package(pins=pins))
    _click(window, vp(window, -6.0025, -6.0))
    pin = window.package_geometry.pins_seen_from_top()[0]
    assert (pin.x_mm, pin.y_mm) == pytest.approx((-6.0025, -6.0), abs=1e-9)
    assert not window.has_pending_footprint_edits()


def test_undo_of_a_body_move_keeps_the_pads_still_on_screen(body):
    """V2/B5: undo re-centres the footprint like the drop did; the pads do not jump."""
    window = body
    view = window.preview_view
    pad_on_screen = lambda: view.mapFromScene(QPointF(*(c * PX for c in top(window)["1"])))
    before = pad_on_screen()
    drag(window, vp(window, -11.0, 4.0), vp(window, -8.0, 4.0))
    after_drag = pad_on_screen()
    window._undo_footprint_drawing()
    after_undo = pad_on_screen()
    assert (after_drag - before).manhattanLength() <= 2
    assert (after_undo - before).manhattanLength() <= 2


def test_the_handles_show_only_near_the_outline(body):
    """V3: the shared ring rule, as the symbol body: not over the middle of the body."""
    from kicadgen.gui.footprint_view import BODY_PART_ROLE
    window = body
    view = window.preview_view
    handles = [i for i in window.preview_scene.items() if i.data(BODY_PART_ROLE) not in (None, "move")]
    view._report_body_hover(vp(window, 0.0, 0.0))
    assert handles and not any(h.isVisible() for h in handles)
    view._report_body_hover(vp(window, 11.0, 0.5))
    assert all(h.isVisible() for h in handles)


def test_ctrl_press_on_the_outline_starts_no_body_drag(body):
    """V6: Ctrl+click selects and never drags."""
    window = body
    viewport = window.preview_view.viewport()
    pos = vp(window, -11.0, 4.0)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, pos)
    QTest.mouseMove(viewport, vp(window, -8.0, 4.0))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, vp(window, -8.0, 4.0))
    assert size(window) == (22.0, 16.0) and not window.has_pending_footprint_edits()


def test_a_move_keeps_the_body_size_exactly(window, qapp, monkeypatch):
    """B2: a move never changes the stored size by float noise, at any quarter turn."""
    _show(window, qapp, monkeypatch, _package(body_length_mm=4.9, body_width_mm=3.9, orientation_deg=90.0,
                                              pins=[PinSpec("1", 0.0, 0.0, .6, .6)]))
    drag(window, vp(window, -1.95, 1.0), vp(window, -0.95, 1.0))
    assert (window.package_geometry.body_length_mm, window.package_geometry.body_width_mm) == (4.9, 3.9)


def test_the_outline_menu_keeps_the_drawing_origin_and_adds_the_body_sizes(body, monkeypatch):
    """A right click on a body corner offers the nearest drawing origin first, then the two sizes.

    Regression found by the suite (2026-11-03): the outline's own menu had
    taken the origin entries away from the corners. Negative twin: a right
    click inside the body, off the outline, opens the background menu with
    the origin entries only.
    """
    from PyQt6.QtWidgets import QMenu
    window = body
    offered, edited = [], []
    # CONTRACT CHANGED 2026-11-09 (review, item 3): the anchor entries sit
    # between the origin entries and the body sizes, so the first size is
    # the fifth entry.
    pick = {"index": 4}

    def choose(menu, *_args):
        actions = [a for a in menu.actions() if not a.isSeparator()]
        offered.append([a.text() for a in actions])
        return actions[pick["index"]] if pick["index"] < len(actions) else None

    monkeypatch.setattr(QMenu, "exec", choose)
    monkeypatch.setattr(window, "_edit_body_dimension", edited.append)
    viewport = window.preview_view.viewport()
    corner = vp(window, -11.0, 8.0)
    QTest.mouseMove(viewport, corner)
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=corner)
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=corner)
    assert offered[-1][0].startswith("Imposta origine di disegno:")
    assert offered[-1][2:] == ["Sposta ancora qui", "Ripristina ancora del footprint",
                               "Modifica quota orizzontale…", "Modifica quota verticale…"]
    assert edited == [window.BODY_DIM_X]
    inside = vp(window, 0.0, 3.0)
    QTest.mouseMove(viewport, inside)
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=inside)
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=inside)
    # CONTRACT CHANGED 2026-11-09 (plan F2): the background menu also offers the anchor entries;
    # still no body size entry off the outline.
    assert offered[-1][0].startswith("Imposta origine di disegno:")
    assert offered[-1][1:] == ["Ripristina origine del footprint", "Sposta ancora qui",
                               "Ripristina ancora del footprint"]
    assert edited == [window.BODY_DIM_X]


def test_the_outline_menu_moves_the_anchor_to_the_corner(body, monkeypatch):
    """Review 2026-11-09, item 3: «Sposta ancora qui» on the outline, as on the background.

    Positive: chosen on a body corner, the anchor goes to that corner (the
    snapped click), one draft step. Negative: «Ripristina ancora del
    footprint» is disabled while the anchor is unmoved.
    """
    from PyQt6.QtWidgets import QMenu
    window = body
    seen = {}

    def choose(menu, *_args):
        actions = {a.text(): a for a in menu.actions() if not a.isSeparator()}
        seen.update({text: action.isEnabled() for text, action in actions.items()})
        return actions["Sposta ancora qui"]

    monkeypatch.setattr(QMenu, "exec", choose)
    viewport = window.preview_view.viewport()
    corner = vp(window, -11.0, 8.0)
    QTest.mouseMove(viewport, corner)
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=corner)
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=corner)
    assert seen["Ripristina ancora del footprint"] is False, "negative: nothing to restore yet"
    assert "Modifica quota orizzontale…" in seen, "the outline's own menu, not the background's"
    assert window.footprint_anchor == pytest.approx((-11.0, 8.0))
    assert window.has_pending_footprint_edits()
