"""@brief Package pads dragged in the footprint preview, as the pins of the symbol editor (user, 2026-11-02).

Agreed behaviour (HANDOFF 02/11): a left press on a package pad and a drag
moves it on the grid (the selected pads together), one undo step, Escape puts
it back, a drop without displacement changes nothing; Ctrl+click and the
rubber band select pads; a background click clears. The displacement, not the
position, is snapped, so a pad measured off the grid keeps its offset. Each
rule has a positive case and a negative one that differ structurally.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from kicadgen.core.package_geometry import PackageGeometry, PinSpec  # noqa: E402

#: Scene px per mm of the preview.
PX = 20.0
NO, CTRL, SHIFT = (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                   Qt.KeyboardModifier.ShiftModifier)

#: Four 1.2 x 0.6 mm pads at (±3, ±2) mm on a 15 x 10 mm body.
PINS = [PinSpec("1", -3.0, -2.0, 1.2, .6), PinSpec("2", 3.0, -2.0, 1.2, .6),
        PinSpec("3", 3.0, 2.0, 1.2, .6), PinSpec("4", -3.0, 2.0, 1.2, .6)]


def _show(window, qapp, monkeypatch, package, grid=False):
    """Show the preview of `package`, camera at 1 px per scene px on the origin."""
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    window.chk_grid.setChecked(grid)
    window.package_geometry = package
    window._draw_preview()
    view = window.preview_view
    view.resetTransform()
    view.scale(2.0, 2.0)
    view.centerOn(0, 0)
    view.auto_fit = False
    qapp.processEvents()
    return window


@pytest.fixture
def fp(window, qapp, monkeypatch):
    """Four SMD pads, grid hidden (displacement snapped to 0.01 mm)."""
    return _show(window, qapp, monkeypatch,
                 PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10, pins=list(PINS)))


def vp(window, x_mm, y_mm):
    """Viewport point of a footprint point (mm, top view)."""
    return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))


def click(window, pos, modifier=NO):
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, modifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifier, pos)


def drag(window, start, end, modifier=NO):
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, modifier, start)
    QTest.mouseMove(viewport, (start + end) / 2)
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifier, end)


def top(window):
    """{pad number: (x, y)} of the pads as KiCad receives them (mm, top view)."""
    return {p.number: (round(p.x_mm, 3), round(p.y_mm, 3))
            for p in window.package_geometry.pins_seen_from_top()}


# ── Drag ────────────────────────────────────────────────────────────────

def test_dragging_a_pad_moves_only_that_pad_as_one_undo_step(fp):
    window = fp
    before = top(window)
    undo = len(window._fp_drawings_undo)
    drag(window, vp(window, -3, -2), vp(window, -3, -5))
    after = top(window)
    assert after["1"] == (-3.0, -5.0)
    assert {k: v for k, v in after.items() if k != "1"} == {k: v for k, v in before.items() if k != "1"}
    assert len(window._fp_drawings_undo) == undo + 1, "one drag = one undo step"
    assert window.has_pending_footprint_edits(), "the move is a draft edit (Applica/Annulla)"
    assert window._fp_selected_pads() == [0], "the moved pad stays selected"
    window._undo_footprint_drawing()
    assert top(window) == before
    assert window._fp_selected_pads() == [], "undo clears the selection, as for the drawings"


def test_a_click_on_a_pad_selects_it_and_changes_nothing(fp):
    window = fp
    before = top(window)
    undo = len(window._fp_drawings_undo)
    click(window, vp(window, 3, 2))
    assert window._fp_selected_pads() == [2]
    assert top(window) == before and len(window._fp_drawings_undo) == undo
    assert not window.has_pending_footprint_edits()
    click(window, vp(window, 0, 4))                 # empty background
    assert window._fp_selected_pads() == []


def test_escape_during_a_drag_puts_the_pad_back(fp, qapp):
    window = fp
    before = top(window)
    viewport = window.preview_view.viewport()
    start, end = vp(window, -3, -2), vp(window, -6, -2)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, start)
    QTest.mouseMove(viewport, end)
    pad = _pad_item(window, 0)
    assert pad.pos() != QPointF(0, 0), "the pad follows the pointer during the drag"
    QTest.keyClick(window.preview_view, Qt.Key.Key_Escape)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, end)
    assert top(window) == before
    assert pad.pos() == QPointF(0, 0), "the cancelled pad is drawn where it was"
    assert not window.has_pending_footprint_edits()


def _pad_item(window, index):
    from kicadgen.gui.footprint_view import PAD_KEY_ROLE
    return next(i for i in window.preview_scene.items()
                if str(i.data(PAD_KEY_ROLE) or "").startswith(f"package:{index}:"))


def test_the_selected_pads_move_together(fp):
    window = fp
    click(window, vp(window, -3, -2))
    click(window, vp(window, 3, -2), CTRL)
    assert window._fp_selected_pads() == [0, 1]
    drag(window, vp(window, 3, -2), vp(window, 3, -4))
    after = top(window)
    assert after["1"] == (-3.0, -4.0) and after["2"] == (3.0, -4.0)
    assert after["3"] == (3.0, 2.0) and after["4"] == (-3.0, 2.0), "unselected pads stay"
    # Negative: a press on a pad OUTSIDE the selection moves that pad alone.
    drag(window, vp(window, 3, 2), vp(window, 3, 3))
    after = top(window)
    assert after["3"] == (3.0, 3.0) and after["1"] == (-3.0, -4.0)
    assert window._fp_selected_pads() == [2]


def test_ctrl_click_toggles_a_pad_and_starts_no_drag(fp):
    window = fp
    before = top(window)
    click(window, vp(window, -3, 2), CTRL)
    click(window, vp(window, 3, 2), CTRL)
    assert window._fp_selected_pads() == [2, 3]
    click(window, vp(window, -3, 2), CTRL)
    assert window._fp_selected_pads() == [2]
    assert top(window) == before


def test_the_band_selects_only_wholly_enclosed_pads(fp):
    window = fp
    drag(window, vp(window, -5, -3), vp(window, 5, -1), SHIFT)
    assert window._fp_selected_pads() == [0, 1]
    # Negative: a band cutting a pad in half does not take it.
    drag(window, vp(window, -3, 1), vp(window, 5, 3), SHIFT)
    assert window._fp_selected_pads() == [2]


def _with_line(window, points=((-6, 4), (6, 4))):
    """Add one silkscreen line to the draft and redraw."""
    from kicadgen.core.footprint import validate_footprint_drawings
    window.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in points]}])
    window._draw_preview()


def test_a_press_on_a_drawing_drops_the_pad_selection(fp):
    window = fp
    _with_line(window)
    click(window, vp(window, -3, -2))
    assert window._fp_selected_pads() == [0]
    click(window, vp(window, 0, 4))
    assert window._fp_selected_indices() == [0] and window._fp_selected_pads() == []
    # And the other way round: a press on a pad drops the drawing selection.
    click(window, vp(window, -3, -2))
    assert window._fp_selected_indices() == [] and window._fp_selected_pads() == [0]


def test_pads_and_drawings_are_never_selected_together(fp):
    """The symbol editor's rule for pins and elements (`_select_pins`)."""
    window = fp
    _with_line(window)
    click(window, vp(window, 0, 4))
    click(window, vp(window, -3, -2), CTRL)
    assert window._fp_selected_pads() == [0] and window._fp_selected_indices() == []
    click(window, vp(window, 0, 4), CTRL)
    assert window._fp_selected_indices() == [0] and window._fp_selected_pads() == []
    # A band enclosing pads AND the line selects only the pads ...
    drag(window, vp(window, -8, -3), vp(window, 8, 5), SHIFT)
    assert window._fp_selected_pads() == [0, 1, 2, 3] and window._fp_selected_indices() == []
    # ... and one enclosing only the line selects the line, dropping the pads.
    drag(window, vp(window, -7, 3.5), vp(window, 7, 4.5), SHIFT)
    assert window._fp_selected_indices() == [0] and window._fp_selected_pads() == []


def test_ctrl_click_on_a_pad_next_to_a_line_toggles_the_pad(fp):
    """Ctrl+click keeps the plain press's priority: the pad before a drawing body."""
    window = fp
    _with_line(window, ((-6, -2.1), (6, -2.1)))     # through pads 1 and 2
    click(window, vp(window, -3, -2), CTRL)
    assert window._fp_selected_pads() == [0] and window._fp_selected_indices() == []
    # Negative: away from any pad the line is toggled.
    click(window, vp(window, 0, -2.1), CTRL)
    assert window._fp_selected_indices() == [0] and window._fp_selected_pads() == []


def test_with_pads_selected_the_keys_never_act_on_the_hovered_drawing(fp):
    window = fp
    _with_line(window)
    lines = [dict(r) for r in window.footprint_drawings]
    click(window, vp(window, -3, -2))
    window._fp_hovered_drawing = 0                   # pointer left on the line
    for key in (Qt.Key.Key_Delete, Qt.Key.Key_R):
        QTest.keyClick(window.preview_view, key)
    QTest.keyClick(window.preview_view, Qt.Key.Key_D, CTRL)
    assert [dict(r) for r in window.footprint_drawings] == lines, "the line was neither deleted, turned nor duplicated"
    assert top(window)["1"] == (-3.0, -2.0), "Delete and R do not touch pads"
    # Negative: with no pad selected Delete removes the hovered line, as before.
    window._clear_footprint_selection()
    window._fp_hovered_drawing = 0
    QTest.keyClick(window.preview_view, Qt.Key.Key_Delete)
    assert window.footprint_drawings == []


def test_the_arrows_move_the_selected_pads_by_one_grid_step(fp):
    window = fp
    _with_line(window)
    lines = [dict(r) for r in window.footprint_drawings]
    click(window, vp(window, -3, -2))
    window._fp_hovered_drawing = 0
    QTest.keyClick(window.preview_view, Qt.Key.Key_Left)
    QTest.keyClick(window.preview_view, Qt.Key.Key_Up)
    assert top(window)["1"] == (-3.01, -2.01), "grid hidden: one 0.01 mm step per key"
    assert [dict(r) for r in window.footprint_drawings] == lines
    assert window._fp_selected_pads() == [0]


def test_a_redraw_during_the_drag_keeps_the_pads_following(fp):
    window = fp
    viewport = window.preview_view.viewport()
    start, end = vp(window, -3, -2), vp(window, -3, -5)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, start)
    QTest.mouseMove(viewport, end)
    window._draw_preview()                           # e.g. a theme or grid change
    assert _pad_item(window, 0).pos() != QPointF(0, 0), "the new pad item follows the drag"
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, end)
    assert top(window)["1"] == (-3.0, -5.0)


def test_a_package_replaced_during_the_drag_is_not_edited_at_the_drop(fp):
    window = fp
    viewport = window.preview_view.viewport()
    start, end = vp(window, -3, -2), vp(window, -3, -5)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, start)
    QTest.mouseMove(viewport, end)
    # An analysis finishes under the drag: another package, same pad numbers.
    window.package_geometry = PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10,
                                              pins=list(PINS))
    window._draw_preview()
    replaced = top(window)
    undo = len(window._fp_drawings_undo)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, end)
    assert top(window) == replaced and len(window._fp_drawings_undo) == undo


def test_hiding_the_view_cancels_the_drag(fp, qapp):
    window = fp
    before = top(window)
    viewport = window.preview_view.viewport()
    start, end = vp(window, -3, -2), vp(window, -3, -5)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, start)
    QTest.mouseMove(viewport, end)
    window.preview_view.hide()
    qapp.processEvents()
    assert not window.preview_view.gestures.active
    assert top(window) == before and not window.has_pending_footprint_edits()
    window.preview_view.show()


def test_pads_show_the_move_cursor_only_when_they_can_move(fp):
    window = fp
    assert _pad_item(window, 0).cursor().shape() == Qt.CursorShape.SizeAllCursor
    index = window.combo_mounting.findData("headers")
    window.combo_mounting.blockSignals(True)
    try:
        window.combo_mounting.setCurrentIndex(index)
        window._draw_preview()
        from kicadgen.gui.footprint_view import PAD_KEY_ROLE
        pads = [i for i in window.preview_scene.items()
                if str(i.data(PAD_KEY_ROLE) or "").startswith("package:")]
        assert all(not p.hasCursor() for p in pads)
    finally:
        window.combo_mounting.setCurrentIndex(0)
        window.combo_mounting.blockSignals(False)


def test_selected_pads_are_outlined_in_the_selection_colour(fp):
    window = fp
    plain = _pad_item(window, 1).pen().color().name()
    click(window, vp(window, 3, -2))
    assert _pad_item(window, 1).pen().color().name() == window._fp_selected_colour().lower()
    assert _pad_item(window, 0).pen().color().name() == plain


def test_a_drop_on_another_pad_is_refused(fp, monkeypatch):
    window = fp
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda *args: toasts.append(args))
    before = top(window)
    drag(window, vp(window, -3, -2), vp(window, 3, -2))
    assert top(window) == before, "overlapping pads: the package stays as it was"
    assert toasts, "the refusal is said"
    assert _pad_item(window, 0).pos() == QPointF(0, 0)


# ── Grid: a magnet and a crosshair, never a forced position ────────────
# CONTRACT CHANGED twice (user, 2026-11-02): first «si sposta, ma a salti non
# è uniforme» (whole-step jumps), then «non vorrei avere pad fantasma ... mi
# serve un mirino al centro del pad ... diversificare quando sono nel
# reticolo a 2.54 oppure fuori». Now: the pad follows the pointer (0.01 mm),
# a grid node within PAD_GRID_MAGNET_PX catches its centre, a crosshair marks
# the centre, green on a node and orange off the grid; no dashed outline.

def _crosshair(window):
    """The crosshair item of the running drag (the only z-20 path item), or None."""
    found = [i for i in window.preview_scene.items() if i.zValue() == 20 and hasattr(i, "path")]
    return found[0] if found else None


def _grid(window):
    view = window.preview_view
    return view.grid_step() / PX, tuple(v / PX for v in view._grid[1])


def _grid_window(window, qapp, monkeypatch, pins):
    return _show(window, qapp, monkeypatch,
                 PackageGeometry(family="soic", body_length_mm=10, body_width_mm=6, pins=pins), grid=True)


def test_near_a_node_the_centre_is_caught_and_the_crosshair_is_green(window, qapp, monkeypatch):
    from kicadgen.gui.footprint_view import PAD_CROSSHAIR_ON_GRID
    window = _grid_window(window, qapp, monkeypatch,
                          [PinSpec("1", -3.1, 0.0, 1.0, 1.0), PinSpec("2", 3.1, 0.0, 1.0, 1.0)])
    step, (ox, oy) = _grid(window)
    node = (ox + round((-3.1 - step - ox) / step) * step, oy + round((0.0 - oy) / step) * step)
    # 1 screen px (0.025 mm at 2 px per scene px) from the node: inside the magnet.
    near = (node[0] + 0.025, node[1] - 0.025)
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, vp(window, -3.1, 0))
    QTest.mouseMove(viewport, vp(window, *near))
    assert _crosshair(window).pen().color().name() == PAD_CROSSHAIR_ON_GRID
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, vp(window, *near))
    assert top(window)["1"] == pytest.approx(node, abs=1e-6), "the centre sits exactly on the node"
    assert _crosshair(window) is None, "the crosshair goes with the drop"


def test_off_the_grid_the_pad_stays_where_it_is_put_and_the_crosshair_is_orange(window, qapp, monkeypatch):
    from kicadgen.gui.footprint_view import PAD_CROSSHAIR_OFF_GRID
    window = _grid_window(window, qapp, monkeypatch,
                          [PinSpec("1", -3.1, 0.0, 1.0, 1.0), PinSpec("2", 3.1, 0.0, 1.0, 1.0)])
    step, (ox, oy) = _grid(window)
    # Half a step from every node: far outside the magnet.
    far = (ox + round((-4.0 - ox) / step) * step + step / 2, oy + step / 2)
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, vp(window, -3.1, 0))
    QTest.mouseMove(viewport, vp(window, *far))
    assert _crosshair(window).pen().color().name() == PAD_CROSSHAIR_OFF_GRID
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, vp(window, *far))
    assert top(window)["1"] == pytest.approx(far, abs=0.0251), "free placement where it was dropped"


def test_with_the_grid_hidden_the_pad_lands_where_it_is_dropped(fp):
    window = fp
    drag(window, vp(window, -3, -2), vp(window, -3.37, -4.53))
    assert top(window)["1"] == pytest.approx((-3.37, -4.53), abs=0.011),         "no grid: free placement to 0.01 mm, no magnet"


def test_the_pad_follows_the_pointer_smoothly_and_no_outline_is_drawn(fp):
    window = fp
    view = window.preview_view
    viewport = view.viewport()
    start = vp(window, -3, -2)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, start)
    pad = _pad_item(window, 0)
    for k in range(1, 9):
        QTest.mouseMove(viewport, start + QPoint(0, -3 * k))
        moved = view.mapToScene(start + QPoint(0, -3 * k)) - view.mapToScene(start)
        assert pad.pos().y() == pytest.approx(moved.y(), abs=0.2 + 1e-6),             "the pad is under the pointer at every move (to 0.01 mm)"
    dashed = [i for i in window.preview_scene.items()
              if hasattr(i, "pen") and i.pen().style() == Qt.PenStyle.DashLine]
    assert dashed == [], "no ghost pad"
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, start + QPoint(0, -24))


def test_escape_removes_the_crosshair(fp):
    window = fp
    viewport = window.preview_view.viewport()
    start = vp(window, -3, -2)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, start)
    QTest.mouseMove(viewport, vp(window, -3, -4))
    assert _crosshair(window) is not None
    QTest.keyClick(window.preview_view, Qt.Key.Key_Escape)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, vp(window, -3, -4))
    assert _crosshair(window) is None


def test_untouched_pads_stay_put_on_screen_when_the_body_is_re_estimated(window, qapp, monkeypatch):
    """Origin «pin1»: the body centre is estimated from the pad field and moves
    when an outer pad moves; the camera follows, so the untouched pads do not."""
    pins = [PinSpec("1", 0.0, 0.0, 1.0, 1.0), PinSpec("2", 6.0, 0.0, 1.0, 1.0),
            PinSpec("3", 6.0, 4.0, 1.0, 1.0)]
    window = _show(window, qapp, monkeypatch,
                   PackageGeometry(family="soic", body_length_mm=12, body_width_mm=8, origin="pin1", pins=pins))
    view = window.preview_view
    view.resetTransform()
    view.scale(4.0, 4.0)          # zoomed in: the scroll range can absorb the shift
    view.centerOn(0, 0)
    qapp.processEvents()
    before = top(window)
    screen_2 = vp(window, *before["2"])
    x, y = before["1"]
    drag(window, vp(window, x, y), vp(window, x - 3.0, y))
    after = top(window)
    assert after["2"] != before["2"], "the footprint frame did re-centre (the case under test)"
    assert (vp(window, *after["2"]) - screen_2).manhattanLength() <= 2, \
        "pad 2, untouched, did not jump on screen"
    # Negative: with origin body_center nothing re-centres and the camera stays.
    window.package_geometry = PackageGeometry(family="soic", body_length_mm=12, body_width_mm=8,
                                              pins=list(PINS))
    window._draw_preview()
    centre = view.mapToScene(view.viewport().rect().center())
    drag(window, vp(window, -3, -2), vp(window, -3, -4))
    assert view.mapToScene(view.viewport().rect().center()) == centre


# ── Frames: the drop point at any orientation and view ──────────────────

@pytest.mark.parametrize("orientation, view, mirrored", [
    (0, "top", False), (90, "top", False), (30, "top", False),
    (0, "bottom", False), (90, "bottom", False), (0, "top", True)])
def test_the_pad_lands_where_it_is_dropped_whatever_the_package_frame(window, qapp, monkeypatch,
                                                                     orientation, view, mirrored):
    package = PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10, pins=list(PINS),
                              orientation_deg=orientation, source_view=view, mirrored=mirrored)
    window = _show(window, qapp, monkeypatch, package)
    before = top(window)
    x, y = before["1"]
    drag(window, vp(window, x, y), vp(window, x + 1.0, y - 2.0))
    after = top(window)
    # Grid hidden: the centre lands on the 0.01 mm nearest to the drop point.
    assert after["1"] == pytest.approx((x + 1.0, y - 2.0), abs=0.0051)
    assert {k: v for k, v in after.items() if k != "1"} == {k: v for k, v in before.items() if k != "1"}


# ── What cannot be dragged ──────────────────────────────────────────────

def test_a_part_mounted_on_a_header_refuses_the_pad_drag(fp):
    window = fp
    index = window.combo_mounting.findData("headers")
    assert index >= 0
    window.combo_mounting.blockSignals(True)
    try:
        window.combo_mounting.setCurrentIndex(index)
        assert window.gesture_begin(("pad", 0), QPointF(0, 0)) is None
    finally:
        window.combo_mounting.setCurrentIndex(0)
        window.combo_mounting.blockSignals(False)


def test_a_pin_header_pad_is_not_a_drag_target(fp):
    from PyQt6.QtWidgets import QGraphicsRectItem
    from kicadgen.gui.footprint_view import PAD_KEY_ROLE
    window = fp
    item = QGraphicsRectItem(-10 * PX - 10, -10 * PX - 10, 20, 20)
    item.setData(PAD_KEY_ROLE, "header:1")
    window.preview_scene.addItem(item)
    assert window.preview_view._pad_at(vp(window, -10, -10)) is None
    assert window.preview_view._pad_at(vp(window, -3, -2)) == 0
    assert window.preview_view._pad_at(QPoint(0, 0) + vp(window, 0, 4)) is None
