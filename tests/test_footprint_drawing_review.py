"""@brief Fixes of the 2026-11-08 adversarial review of the footprint drawing tools (wave F2).

Each finding of HANDOFF.md «08/11» that the footprint tab owns gets a positive
case and a negative one that differ for a structural reason (CLAUDE.md
«Correzioni»): stale pad snap after the preview is cleared (M1), Shift without
a mouse move (M2), one angle+grid rule (M3), one snap classifier (M4), the
marker following a kind change (M5, P8), pad hot points from the declared
copper shape (M6), the orthogonal dimension's axis from the placement click
(M7), dimensions at the layer width (M13), and the low findings A3-A8, G6,
B3, B6-B8, P7, P9, P10, P12.
"""
import math

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QPointF, Qt                                         # noqa: E402
from PyQt6.QtTest import QTest                                                # noqa: E402

from kicadgen.core.footprint import validate_footprint_drawings              # noqa: E402
from kicadgen.core.package_geometry import PackageGeometry, PinSpec           # noqa: E402
from test_footprint_drawing_kicad import PX, _click, _last, _move, _snap, dialogs, fp  # noqa: E402,F401


def _pad_targets(window, x_mm, y_mm):
    """One pad centre as the only footprint snap target, object snap on."""
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui.canvas_measure import SnapTargets
    window.chk_fp_object_snap.setChecked(True)
    QApplication.processEvents()
    window._fp_geometry_targets = SnapTargets(centres=((x_mm * PX, y_mm * PX, "1", ()),))
    window._fp_pad_hot_points = []
    window._fp_snap_cache = None


def _markers(window):
    from kicadgen.gui.canvas_snap_marker import SNAP_MARKER_ROLE
    return [i for i in window.preview_scene.items() if i.data(SNAP_MARKER_ROLE) == window._FP_SNAP_MARKER_TAG]


# ── M1: nothing drawn, nothing caught ──────────────────────────────────

def test_a_cleared_preview_leaves_no_pad_to_snap_to(fp, monkeypatch, qapp):
    fp._draw_preview()
    qapp.processEvents()
    targets = fp._fp_geometry_targets
    assert targets is not None and targets.centres
    x, y = targets.centres[0][0] / PX, targets.centres[0][1] / PX
    fp.chk_fp_object_snap.setChecked(True)
    fp._fp_snap_cache = None
    hit = fp._fp_object_snap(x + 0.2, y + 0.1, ())
    assert hit is not None and hit.kind == "pad", "drawn: the pad is caught"
    monkeypatch.setattr(fp, "_preview_params", lambda: None)
    fp._draw_preview()
    qapp.processEvents()
    assert fp._fp_geometry_targets is None and fp._fp_pad_hot_points == []
    assert fp._fp_object_snap(x + 0.2, y + 0.1, ()) is None, "cleared: no pad of the old footprint"


# ── M2: Shift pressed and released without a mouse move ────────────────

def test_shift_press_and_release_update_the_ghost_and_the_click_takes_it(fp, monkeypatch):
    monkeypatch.setattr(fp, "_sync_footprint_measures", lambda *args, **kwargs: None)
    fp._toggle_footprint_draw_tool("line")
    _pad_targets(fp, 2.3, 0.4)
    _click(fp, 0, 0)
    _move(fp, 2.2, 0.3)
    collector = fp._fp_click_collector()
    assert collector.preview == [(2.3, 0.4)], "snapped to the pad"
    QTest.keyPress(fp.preview_view, Qt.Key.Key_Shift)
    assert collector.preview == [(2.0, 0.0)], "Shift held: the free grid point, no mouse move"
    QTest.keyRelease(fp.preview_view, Qt.Key.Key_Shift)
    assert collector.preview == [(2.3, 0.4)], "Shift released: the snap again, no mouse move"
    _click(fp, 2.2, 0.3)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Return)
    assert _last(fp)["points"] == [[0, 0], [2.3, 0.4]], "the click adds the ghost's point"


# ── M3: one rule for angle + grid ──────────────────────────────────────

def test_the_angle_constraint_counts_grid_steps_from_an_off_grid_anchor(fp):
    from kicadgen.core.draw_constraints import constrain_to_grid
    step, origin = fp._fp_draw_grid_mm()
    got = _snap(fp, 4.1, 1.0, points=[(0.3, 0.2)], mode="90")
    assert got == constrain_to_grid((0.3, 0.2), (4.1, 1.0), "90", step, grid_origin=origin, ndigits=4)
    assert got == (4.3, 0.2), "four whole steps along the ray from the anchor"
    assert got != (4.0, 0.2), "not the old constrain -> absolute grid -> constrain result"
    diagonal = _snap(fp, 3.6, 3.0, points=[(0.5, 0.0)], mode="45")
    assert diagonal == (3.5, 3.0) and diagonal[0] - 0.5 == pytest.approx(diagonal[1]), "exactly on 45 degrees"
    # Negative: a free point keeps the absolute grid.
    assert _snap(fp, 4.1, 1.0, points=[(0.3, 0.2)], mode="free") == (4, 1)


def test_tool_snap_uses_the_current_angle_mode(fp):
    fp._toggle_footprint_draw_tool("line")
    fp.preview_view.click_router.set_angle_mode("90")
    spec = fp._fp_tool_spec("line")
    assert fp.tool_snap(spec, QPointF(4.1 * PX, 1.0 * PX), False, ((0.3, 0.2),))[0] == (4.3, 0.2)
    fp.preview_view.click_router.set_angle_mode("free")
    assert fp.tool_snap(spec, QPointF(4.1 * PX, 1.0 * PX), False, ((0.3, 0.2),))[0] == (4, 1)


def test_rectangle_and_circle_are_never_angle_constrained(fp):
    for key in ("rectangle", "circle"):
        assert _snap(fp, 4.1, 1.2, points=[(0, 0)], mode="45", key=key) == (4, 1), key
    assert _snap(fp, 4.1, 1.2, points=[(0, 0)], mode="45", key="line") != (4, 1), "a line is"


# ── M4: one classifier, the last point excluded ────────────────────────

def test_the_last_collected_point_is_not_a_candidate_the_others_are(fp):
    fp.chk_fp_object_snap.setChecked(True)
    fp._fp_geometry_targets = None
    fp._fp_snap_cache = None
    points = [(5.3, 5.3), (8.3, 8.3)]
    assert _snap(fp, 8.4, 8.4, points=points) == (8, 8), "the last point: the grid"
    assert _snap(fp, 5.4, 5.4, points=points) == (5.3, 5.3), "an earlier point: caught"
    assert fp.FOOTPRINT_SNAP_REACH_PX == pytest.approx(10.0)


def test_snap_wins_over_the_angle_constraint_contract_13(fp):
    fp.chk_fp_object_snap.setChecked(True)
    fp._fp_geometry_targets = None
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[4.3, 1.3], [6.0, 3.0]]}])
    fp._fp_snap_cache = None
    assert _snap(fp, 4.4, 1.2, points=[(0, 0)], mode="90") == (4.3, 1.3), "the endpoint, off the 0/90 rays"
    assert _snap(fp, 7.4, 1.2, points=[(0, 0)], mode="90") == (7, 0), "nothing in reach: the 90 degree ray"
    assert _snap(fp, 4.4, 1.2, points=[(0, 0)], mode="90", free=True) == (4, 1), "Shift skips both"


def test_a_polygon_closes_on_its_first_vertex_through_the_snap(fp):
    fp.chk_fp_object_snap.setChecked(True)
    fp._fp_geometry_targets = None
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0.3, 0.3], [0.3, 6.0]]}])
    fp._fp_snap_cache = None

    def draw():
        fp._toggle_footprint_draw_tool("polygon")
        for x, y in ((0.4, 0.4), (4, 0), (4, 3), (0.45, 0.2)):
            _click(fp, x, y)

    draw()
    assert fp._fp_draw_tool is None, "the closing click snapped onto the off-grid first vertex"
    assert _last(fp)["kind"] == "polygon" and _last(fp)["points"][0] == [0.3, 0.3]
    assert len(_last(fp)["points"]) == 3
    # Negative: a Shift closing click (free: no object snap) lands on the grid
    # node (0, 0), not on the off-grid first vertex, so the polygon stays open.
    fp._cancel_footprint_drawing()
    count = len(fp.footprint_drawings)
    fp._toggle_footprint_draw_tool("polygon")
    for x, y in ((0.4, 0.4), (4, 0), (4, 3)):
        _click(fp, x, y)
    _click(fp, 0.45, 0.2, Qt.KeyboardModifier.ShiftModifier)
    assert fp._fp_draw_tool == "polygon" and len(fp.footprint_drawings) == count, "still open"
    fp._cancel_footprint_drawing()


# ── M5, P8: the marker follows a kind change at the same node ──────────

def test_switching_the_object_snap_updates_the_marker_at_the_same_node(fp, monkeypatch):
    monkeypatch.setattr(fp, "_sync_footprint_measures", lambda *args, **kwargs: None)
    fp._toggle_footprint_draw_tool("line")
    _pad_targets(fp, 2.0, 2.0)
    _move(fp, 2.1, 2.0)
    assert fp._fp_draw_hover == (2.0, 2.0) and len(_markers(fp)) == 1, "the pad on a grid node"
    fp.chk_fp_object_snap.setChecked(False)
    assert fp._fp_draw_hover == (2.0, 2.0), "same node"
    assert not _markers(fp), "box off: the marker goes at once, no mouse move (P8)"
    fp.chk_fp_object_snap.setChecked(True)
    assert len(_markers(fp)) == 1, "box on: back at once"


def test_the_marker_goes_on_leave_and_on_a_real_escape(fp, monkeypatch):
    monkeypatch.setattr(fp, "_sync_footprint_measures", lambda *args, **kwargs: None)
    fp._toggle_footprint_draw_tool("line")
    _pad_targets(fp, 2.0, 2.0)
    _move(fp, 2.1, 2.1)
    assert _markers(fp)
    fp._on_footprint_draw_hover_left()
    assert not _markers(fp), "leave"
    _move(fp, 2.1, 2.1)
    assert _markers(fp)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Escape)
    assert not _markers(fp) and fp._fp_draw_tool is None, "Escape"


# ── M6: pad hot points from the declared copper shape ──────────────────

def test_pad_hot_points_follow_the_declared_shape():
    from kicadgen.gui.main_window import MainWindow
    rect = MainWindow._pad_hot_points("1", "rect", 0.0, 0.0, 2.0, 1.0, 0.0)
    assert {(round(c.x, 6), round(c.y, 6)) for c in rect} == {(-1, -0.5), (1, -0.5), (1, 0.5), (-1, 0.5)}
    assert {c.kind for c in rect} == {"corner"}
    rounded = MainWindow._pad_hot_points("2", "roundrect", 0.0, 0.0, 2.0, 1.0, 0.0)
    assert {(round(c.x, 6), round(c.y, 6)) for c in rounded} == {(0, -0.5), (1, 0), (0, 0.5), (-1, 0)}
    assert {c.kind for c in rounded} == {"midpoint"}, "no corner: a rounded corner has no copper there"
    trapezoid = MainWindow._pad_hot_points("3", "trapezoid", 0.0, 0.0, 2.0, 1.0, 0.0, (0.4, 0.0))
    # KiCad's TransformTrapezoidToPolygon: rect_delta x > 0 makes the left side taller.
    assert [(round(c.x, 6), round(c.y, 6)) for c in trapezoid] == [(-1, -0.7), (1, -0.3), (1, 0.3), (-1, 0.7)]
    turned = MainWindow._pad_hot_points("4", "rect", 0.0, 0.0, 2.0, 1.0, 90.0)
    assert {(round(c.x, 6) + 0.0, round(c.y, 6) + 0.0) for c in turned} == {(-0.5, 1), (-0.5, -1), (0.5, -1),
                                                                             (0.5, 1)}
    assert MainWindow._pad_hot_points("5", "circle", 0, 0, 1, 1, 0) == []
    assert MainWindow._pad_hot_points("6", "oval", 0, 0, 2, 1, 0) == []


def test_a_package_rect_pad_offers_corners_a_roundrect_pad_does_not(fp):
    fp.package_geometry = PackageGeometry(
        family="soic", body_length_mm=15, body_width_mm=10,
        pins=[PinSpec("1", -3.0, -2.0, 1.2, 0.6, shape="rect"), PinSpec("2", 3.0, -2.0, 1.2, 0.6)])
    fp._draw_preview()
    kinds = {}
    for candidate in fp._fp_pad_hot_points:
        kinds.setdefault(candidate.owner[1], set()).add(candidate.kind)
    assert kinds == {"1": {"corner"}, "2": {"midpoint"}}


# ── M7: the orthogonal dimension's axis from the placement ─────────────

def test_the_orthogonal_axis_comes_from_the_placement_like_kicad():
    from kicadgen.gui.main_window import MainWindow
    axis = MainWindow._orthogonal_dimension_axis
    assert axis((0, 0), (5, 1), (8, 0.5)) == "y", "beside the box: the vertical extent (reviewer's case)"
    assert axis((0, 0), (5, 1), (2.5, 4)) == "x", "above or below: the horizontal extent"
    assert axis((0, 0), (5, 1), (9, 3)) == "y", "corner region, farther sideways"
    assert axis((0, 0), (5, 1), (6, 8)) == "x", "corner region, farther up/down"
    assert axis((0, 0), (5, 1), (2, 0.5)) == "x", "inside the box: the longer extent (declared fallback)"
    assert axis((0, 0), (0, 4), (3, 9)) == "y" and axis((0, 0), (4, 0), (9, 3)) == "x", "degenerate boxes"


def test_the_orthogonal_tool_and_its_ghost_follow_the_third_click(fp):
    from kicadgen.core.footprint import dimension_offset_from_crossbar, record_dimension
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    _click(fp, 0, 0)
    _click(fp, 5, 1)
    ghost = fp._ghost_path([(0, 0), (5, 1), (8, 0.5)]).boundingRect()
    offset = dimension_offset_from_crossbar((0, 0), (5, 1), (8, 0.5), "y")
    ink = record_dimension({"kind": "dimension", "layer": "F.SilkS", "points": [[0, 0], [5, 1]],
                            "offset": offset, "axis": "y"}).ink_segments()
    xs = [v[0] * PX for seg in ink for v in seg]
    assert ghost.left() == pytest.approx(min(xs), abs=0.5) and ghost.right() == pytest.approx(max(xs), abs=0.5)
    _click(fp, 8, 0.5)
    assert _last(fp)["axis"] == "y", "the placement beside the points measures Y"
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    for point in ((0, 0), (5, 1), (3, 4)):
        _click(fp, *point)
    assert _last(fp)["axis"] == "x" and _last(fp)["offset"] == 4.0


def test_inside_the_box_the_orthogonal_axis_keeps_the_previous_one():
    from kicadgen.gui.main_window import MainWindow
    axis = MainWindow._orthogonal_dimension_axis
    inside = (2, 0.5)
    assert axis((0, 0), (5, 1), inside, previous="y") == "y", "inside: the preview's last axis stays"
    assert axis((0, 0), (5, 1), inside, previous="x") == "x"
    assert axis((0, 0), (5, 1), inside) == "x", "no previous axis: the longer extent"
    assert axis((0, 0), (5, 1), (2.5, 4), previous="y") == "x", "outside the box the region decides, not memory"


def test_the_orthogonal_ghost_remembers_its_axis_for_one_dimension_only(fp):
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    _click(fp, 0, 0)
    _click(fp, 5, 1)
    _move(fp, 8, 0.5)                                      # beside the box: Y
    _move(fp, 2, 0.5)                                      # back inside: still Y (KiCad)
    _click(fp, 2, 0.5)
    assert _last(fp)["axis"] == "y", "the click inside writes the axis the ghost showed"
    # Negative: the next dimension, placed inside straight away, starts from the longer extent.
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    for point in ((0, 0), (5, 1), (2, 0.5)):
        _click(fp, *point)
    assert _last(fp)["axis"] == "x", "the memory belonged to the previous dimension"


@pytest.mark.parametrize("how", ["backspace", "escape"])
def test_the_orthogonal_axis_memory_ends_with_the_dimension(fp, how):
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    _click(fp, 0, 0)
    _click(fp, 5, 1)
    _move(fp, 8, 0.5)
    assert fp._fp_ortho_axis_memory[1] == "y"
    if how == "backspace":
        fp._fp_click_collector().undo_last()               # the second point is gone
    else:
        fp._cancel_footprint_drawing()
        fp._toggle_footprint_draw_tool("dimension_orthogonal")
        _click(fp, 0, 0)
    assert fp._fp_ortho_axis_memory is None
    # Same second point again, placed inside straight away: a fresh dimension, longer extent.
    # Each click is preceded by its move, as with a real mouse: the collector
    # recomputes the ghost from the last hover, which must not be the old (8, 0.5).
    _move(fp, 5, 1)
    _click(fp, 5, 1)
    _move(fp, 2, 0.5)
    _click(fp, 2, 0.5)
    assert _last(fp)["axis"] == "x"


# ── M13: dimensions take the layer width ───────────────────────────────

def test_a_dimension_takes_the_active_layer_width_from_both_routes(fp):
    from kicadgen.gui import canvas_measure as cm
    fp._activate_fp_draw_layer("F.SilkS")
    fp._finish_footprint_drawing("dimension", [(0.0, 0.0), (5.0, 0.0), (2.0, 2.0)])
    assert _last(fp)["width"] == 0.12, "silkscreen: KLC 0.12, no longer 0.10"
    fp._activate_fp_draw_layer("F.Fab")
    fp.spin_fp_draw_width.setValue(0.2)
    fp._finish_footprint_drawing("dimension_orthogonal", [(0.0, 0.0), (5.0, 1.0), (2.0, 4.0)])
    assert _last(fp)["width"] == 0.2, "a typed width wins, as for a line"
    measure = cm.measure(cm.MeasureAnchor(-200.0, 100.0), cm.MeasureAnchor(0.0, 100.0), PX)
    assert fp._export_measure_as_dimension(measure)
    assert _last(fp)["width"] == 0.2 and _last(fp)["layer"] == "F.Fab", "«Esporta come quota» too"
    # Negative: a stored dimension keeps its own width.
    old = validate_footprint_drawings([{"kind": "dimension", "layer": "F.SilkS", "width": 0.1,
                                        "points": [[0, 0], [3, 0]], "offset": 1.0}])
    fp.footprint_drawings = old
    fp._draw_preview()
    assert fp.footprint_drawings[0]["width"] == 0.1


# ── Low findings of the palette ────────────────────────────────────────

def test_a_line_closed_into_a_polygon_is_filled_only_with_riempi(fp):
    fp._toggle_footprint_draw_tool("line")
    assert fp.chk_fp_draw_fill.isEnabled(), "the line can close into an area: «Riempi» stays enabled (A3)"
    fp.chk_fp_draw_fill.setChecked(True)
    for point in ((0, 0), (4, 0), (4, 3), (0, 0)):
        _click(fp, *point)
    assert _last(fp)["kind"] == "polygon" and _last(fp).get("fill") is True
    fp.chk_fp_draw_fill.setChecked(False)
    fp._toggle_footprint_draw_tool("line")
    for point in ((0, 5), (4, 5), (4, 8), (0, 5)):
        _click(fp, *point)
    assert _last(fp)["kind"] == "polygon" and "fill" not in _last(fp)
    fp._toggle_footprint_draw_tool("arc")
    assert not fp.chk_fp_draw_fill.isEnabled(), "an arc never has an area"


def test_choosing_a_hidden_layer_in_the_combo_shows_it(fp):
    fp.fp_layer_boxes["F.Fab"].setChecked(False)
    fp.fp_layer_boxes["Cmts.User"].setChecked(False)
    fp.combo_fp_draw_layer.setCurrentIndex(fp.combo_fp_draw_layer.findData("F.Fab"))
    assert fp.fp_layer_boxes["F.Fab"].isChecked(), "the chosen layer becomes visible (A4)"
    assert not fp.fp_layer_boxes["Cmts.User"].isChecked(), "another hidden layer stays hidden"


def test_a_saved_width_outside_the_box_range_and_a_string_flag_are_ignored(fp):
    fp._config["fp_draw_widths"] = {"F.SilkS": 7.5, "F.Fab": 0.3, "Cmts.User": True}
    assert fp._fp_layer_width("F.SilkS") == 0.12, "above the box's 5 mm: the default (A5)"
    assert fp._fp_layer_width("F.Fab") == 0.3, "inside the range: kept"
    assert fp._fp_layer_width("Cmts.User") == 0.10, "a bool is no width"
    fp._activate_fp_draw_layer("F.Fab")
    fp._activate_fp_draw_layer("F.SilkS")
    assert fp.spin_fp_draw_width.value() == pytest.approx(fp._fp_layer_width("F.SilkS"))
    fp._config["fp_object_snap"] = "false"
    assert fp._fp_config_flag("fp_object_snap", True) is True, "a string is not a bool: the default"
    fp._config["fp_object_snap"] = False
    assert fp._fp_config_flag("fp_object_snap", True) is False


def test_the_start_up_restore_writes_nothing_and_restores_the_angle_mode(fp, monkeypatch):
    saves = []
    monkeypatch.setattr(fp, "_save_config_safely", lambda *a, **k: saves.append(1))
    fp.preview_view.click_router.set_angle_mode("free")
    saves.clear()
    fp._config["fp_angle_mode"] = "45"
    panel = fp._build_footprint_palette()
    try:
        assert fp.preview_view.click_router.angle_mode == "45" and fp.combo_fp_angle_mode.currentData() == "45"
        assert saves == [], "restoring writes nothing (A6)"
        fp.combo_fp_angle_mode.setCurrentIndex(fp.combo_fp_angle_mode.findData("90"))
        assert saves and fp._config["fp_angle_mode"] == "90", "a user change is saved (P10)"
    finally:
        panel.deleteLater()


def test_one_arming_path_and_buttons_derived_from_the_grid(fp, monkeypatch):
    armed = []
    real = fp._arm_footprint_tool
    monkeypatch.setattr(fp, "_arm_footprint_tool", lambda key: armed.append(key) or real(key))
    fp._toggle_footprint_draw_tool("circle")
    fp.fp_tool_grid.button("rectangle").click()
    fp._arm_footprint_tool_shortcut("line")
    assert armed == ["circle", "rectangle", "line"], "A7: grid, shortcut and toggle arm through one function"
    fp._toggle_footprint_draw_tool("line")
    assert fp._fp_draw_tool is None and armed == ["circle", "rectangle", "line"], "the toggle disarms"
    assert set(fp._fp_draw_buttons) == {k for k in fp.fp_tool_grid.buttons() if k}


def test_tooltips_take_their_numbers_from_the_constants(fp, monkeypatch):
    from kicadgen.core import object_snap
    fp._set_fp_palette_tooltips()
    assert "10 pixel" in fp.chk_fp_object_snap.toolTip()
    tip = fp.btn_fp_draw_width_reset.toolTip()
    # B.SilkS and B.Fab added 2026-11-09 (plan F1): front and back silkscreen 0.12 mm,
    # front and back fabrication, comments and user drawings 0.10 mm.
    counts = (tip.count("0.12 mm"), tip.count("0.10 mm"))
    assert counts == (2, 4), "silkscreen x2, fabrication x2, comments, user drawings"
    monkeypatch.setattr(object_snap, "SNAP_REACH_PX", 14.0)
    fp._set_fp_palette_tooltips()
    assert "14 pixel" in fp.chk_fp_object_snap.toolTip() and "10 pixel" not in fp.chk_fp_object_snap.toolTip()


def test_select_is_not_shown_while_the_ruler_is_armed(fp):
    from kicadgen.gui.canvas_toolpalette import SELECT_KEY
    grid = fp.fp_tool_grid
    assert grid.button(SELECT_KEY).isChecked(), "no tool: Select"
    fp._toggle_footprint_measure()
    assert not grid.button(SELECT_KEY).isChecked() and grid.active_key() is None, "«Misura»: nothing checked"
    fp._toggle_footprint_measure()
    assert grid.button(SELECT_KEY).isChecked()


# ── G6, P9: typed origin and body hot points ───────────────────────────

def test_the_drawing_origin_candidate_is_typed_after_what_it_stands_on(fp, monkeypatch, qapp):
    fp._draw_preview()
    qapp.processEvents()
    # Pads and body hidden: only the origin candidate can be caught there.
    for layer in ("pads", "F.Fab"):
        fp.fp_layer_boxes[layer].setChecked(False)
    fp.chk_fp_object_snap.setChecked(True)
    keys = {"corner:0": "corner", "center": "centre",
            next(k for k in fp._fp_origin_points if k.startswith("pad:")): "pad"}
    seen = {}
    for key, wanted in keys.items():
        if key not in fp._fp_origin_points:
            continue
        # monkeypatch puts the saved key back before the window closes.
        monkeypatch.setattr(fp, "footprint_drawing_origin", key)
        fp._apply_drawing_origin()
        x, y = fp._fp_origin_points[key]
        fp._fp_snap_cache = None
        hit = fp._fp_object_snap(x + 0.1, y + 0.1, ())
        seen[key] = (hit.kind if hit is not None else None, wanted)
    assert len(seen) >= 2 and all(got == wanted for got, wanted in seen.values()), seen


def test_the_body_offers_side_midpoints_and_its_centre(fp, qapp):
    fp.package_geometry = PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10,
                                          pins=[PinSpec("1", -3.0, -2.0, 1.2, .6), PinSpec("2", 3.0, -2.0, 1.2, .6)])
    fp._draw_preview()
    qapp.processEvents()
    fp.chk_fp_object_snap.setChecked(True)
    fp._fp_snap_cache = None
    targets = fp._fp_geometry_targets
    assert targets.edges and targets.corners
    body = [row[3] for row in fp._fp_snap_index()._rows if row[3].owner == ("body",)]
    x0, y0, x1, y1 = targets.edges[0]
    mid = ((x0 + x1) / 2 / PX, (y0 + y1) / 2 / PX)
    assert any(c.kind == "midpoint" and (c.x, c.y) == pytest.approx(mid) for c in body), "side midpoint (P9)"
    assert any(c.kind == "centre" and (c.x, c.y) == pytest.approx(fp._fp_body_centre_mm) for c in body)
    # Negative: the corners stay corners, no midpoint sits on one.
    corners = {(round(x / PX, 6), round(y / PX, 6)) for x, y in targets.corners}
    assert not any(c.kind == "midpoint" and (round(c.x, 6), round(c.y, 6)) in corners for c in body)
    fp.fp_layer_boxes["F.Fab"].setChecked(False)
    fp._fp_snap_cache = None
    assert not [row for row in fp._fp_snap_index()._rows if row[3].owner == ("body",)], "hidden F.Fab: none"


# ── B3, B6, B7, B8: preview and menu ───────────────────────────────────

def test_a_stale_scene_is_not_restyled_with_another_kind(fp):
    from kicadgen.core.footprint import USER_TEXT_ANCHOR
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 0], [3, 0]]}])
    fp._draw_preview()
    # The list replaced WITHOUT a redraw: a text record where the line item is.
    fp._footprint_drawings = validate_footprint_drawings([
        {"kind": "text", "layer": "F.SilkS", "points": [[1, 1]], "text": "HI", "size": 1.0,
         "anchor": USER_TEXT_ANCHOR}])
    fp._refresh_drawing_selection()                       # no AttributeError (setBrush on a line)
    fp._draw_preview()
    fp._select_footprint_drawing(0)
    from kicadgen.gui.footprint_view import DRAWING_KIND_ROLE
    assert {i.data(DRAWING_KIND_ROLE) for i in fp.preview_scene.items() if i.data(DRAWING_KIND_ROLE)} == {"text"}


def test_a_long_text_box_line_wraps_inside_the_box(fp):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    from kicadgen.gui.main_window import _wrap_textbox_line

    def lines(text, right):
        fp.footprint_drawings = validate_footprint_drawings([
            {"kind": "textbox", "layer": "Dwgs.User", "width": 0.1, "points": [[0, 0], [right, 20]],
             "text": text, "size": 1.0}])
        fp._draw_preview()
        return [i for i in fp.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 0 and i.boundingRect().height()
                and not hasattr(i, "rect")]

    long_line = "alpha beta gamma delta epsilon zeta"
    assert len(_wrap_textbox_line(long_line, 1.0, 8.0, 0.15)) > 1
    assert _wrap_textbox_line(long_line, 1.0, 200.0, 0.15) == [long_line], "a wide box: one line"
    assert len(lines(long_line, 10.0)) > len(lines(long_line, 200.0))
    assert _wrap_textbox_line("supercalifragilistic", 1.0, 2.0, 0.15) == ["supercalifragilistic"], "a word never breaks"


def test_a_leader_text_is_shown_unescaped(fp):
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [3, -3]], "dtype": "leader",
         "text": "A{slash}B"}])
    fp._draw_preview()
    texts = [i.text() for i in fp.preview_scene.items()
             if i.data(DRAWING_KEY_ROLE) == 0 and isinstance(i, QGraphicsSimpleTextItem)]
    assert texts == ["A/B"], texts


def test_the_stroke_width_menu_dialog_has_the_shared_bounds(fp, monkeypatch):
    from kicadgen.gui import main_window as gui
    seen = {}

    class _Fake:
        @staticmethod
        def getDouble(parent, title, label, value, low, high, decimals):
            seen.update(low=low, high=high, decimals=decimals)
            return 7.25, True

    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [3, 0]]}])
    monkeypatch.setattr(gui, "QInputDialog", _Fake)
    from kicadgen.gui import canvas_menus as cm
    monkeypatch.setattr(cm, "exec_element_menu", lambda menu, keys, pos: "stroke_width")
    fp._footprint_drawing_context_menu(0)
    assert seen == {"low": 0.0001, "high": 10.0, "decimals": 4}
    assert fp.footprint_drawings[0]["width"] == 7.25, "above the old 5 mm cap"


# ── P7: E with several pads ────────────────────────────────────────────

def test_e_with_several_selected_pads_opens_the_first(fp, monkeypatch):
    fp.package_geometry = PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10,
                                          pins=[PinSpec("1", -3.0, -2.0, 1.2, .6), PinSpec("2", 3.0, -2.0, 1.2, .6)])
    fp._draw_preview()
    opened = []
    monkeypatch.setattr(fp, "_edit_package_pad", lambda index: opened.append(index))
    fp._select_footprint_pad(1)
    fp._select_footprint_pad(0, additive=True)
    assert len(fp._fp_selected_pads()) == 2
    assert fp._edit_footprint_target_properties() is True and opened == [fp._fp_selected_pads()[0]]
