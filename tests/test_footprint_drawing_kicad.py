"""@brief KiCad-grade drawing tools of the footprint preview (plan docs/footprint-drawing-kicad-plan.md, wave 2).

Drives the real window: the «Disegno» ToolGrid with Select, per-layer widths
(KLC), «Riempi», polygon / Bezier / text box / radial, centre and leader
dimensions, the angle mode with Shift+Space and Shift = free, the object snap
with its typed marker and its 10 px reach, Backspace and the step hint, the
active layer chosen from the «Layer» group and the KiCad shortcuts.
Every rule has a positive and a negative case (CLAUDE.md «Correzioni»).
"""
import math

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QPointF, Qt                              # noqa: E402
from PyQt6.QtTest import QTest                                     # noqa: E402

from kicadgen.core.footprint import drawing_graphics, validate_footprint_drawings  # noqa: E402

#: Scene px per mm of the preview.
PX = 20.0


# ── Fixtures and helpers ───────────────────────────────────────────────

@pytest.fixture
def fp(window, qapp, monkeypatch):
    """The preview tab shown, camera at 1 screen px per scene px (20 px/mm), 1 mm drawing grid."""
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: None)
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))
    window._draw_preview()
    view = window.preview_view
    view.auto_fit = False
    view.resetTransform()
    view.centerOn(0, 0)
    # Object snap off (the pin header's pads would catch grid clicks); the
    # object-snap tests switch it on and give their own targets.
    window.chk_fp_object_snap.setChecked(False)
    window._fp_geometry_targets = None
    return window


@pytest.fixture
def dialogs(monkeypatch):
    """Fake QInputDialog: `answers['text']`, `answers['multi']` are what getText / getMultiLineText return."""
    from kicadgen.gui import main_window as gui
    answers = {"text": ("Hi", True), "multi": ("one\ntwo", True)}

    class _Fake:
        @staticmethod
        def getText(*args, **kwargs):
            return answers["text"]

        @staticmethod
        def getMultiLineText(*args, **kwargs):
            return answers["multi"]

    monkeypatch.setattr(gui, "QInputDialog", _Fake)
    return answers


def _vp(window, x_mm, y_mm):
    return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))


def _click(window, x_mm, y_mm, modifier=Qt.KeyboardModifier.NoModifier):
    pos = _vp(window, x_mm, y_mm)
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, modifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifier, pos)


def _move(window, x_mm, y_mm):
    """A button-less move WITHOUT modifiers (QTest.mouseMove repeats the modifiers of the last
    QTest mouse event, so a Shift click earlier in the session would make it a free move)."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
    viewport = window.preview_view.viewport()
    pos = _vp(window, x_mm, y_mm)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(viewport.mapToGlobal(pos)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, event)


def _snap(window, x_mm, y_mm, points=(), free=False, mode="free", key="line"):
    """What the host's snap adds for a pointer at (x, y) mm with the given tool."""
    return window.tool_snap_ex(window._fp_tool_spec(key), QPointF(x_mm * PX, y_mm * PX), free,
                               tuple(points), mode)[0]


def _last(window):
    return window.footprint_drawings[-1]


# ── Palette: Select, active button, layer widths, fill ─────────────────

def test_select_disarms_and_the_active_button_follows_arm_and_disarm(fp):
    grid = fp.fp_tool_grid
    assert grid.keys()[0] == "" and grid.keys()[1:] == list(fp.FOOTPRINT_PALETTE_TOOLS) + ["dimension"]
    grid.button("rectangle").click()
    assert fp._fp_draw_tool == "rectangle" and grid.active_key() == "rectangle"
    grid.button("").click()
    assert fp._fp_draw_tool is None and grid.active_key() is None
    # Arming from code (not a grid click) is mirrored too, and Escape returns to Select.
    fp._toggle_footprint_draw_tool("circle")
    assert grid.active_key() == "circle" and grid.button("circle").isChecked()
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Escape)
    assert fp._fp_draw_tool is None and grid.active_key() is None


def test_a_new_line_takes_its_layer_klc_width(fp):
    for layer, width in (("F.Fab", 0.10), ("F.SilkS", 0.12), ("Dwgs.User", 0.10)):
        fp._activate_fp_draw_layer(layer)
        fp._toggle_footprint_draw_tool("line")
        _click(fp, 0, 0)
        _click(fp, 3, 0)
        QTest.keyClick(fp.preview_view, Qt.Key.Key_Return)
        assert _last(fp)["layer"] == layer and _last(fp)["width"] == width


def test_a_typed_width_wins_until_the_klc_default_is_restored(fp):
    fp._activate_fp_draw_layer("F.Fab")
    fp.spin_fp_draw_width.setValue(0.25)
    assert fp._fp_layer_width("F.Fab") == 0.25
    assert fp._fp_layer_width("F.SilkS") == 0.12, "the override is per layer"
    fp._activate_fp_draw_layer("F.SilkS")
    assert fp.spin_fp_draw_width.value() == pytest.approx(0.12)
    fp._activate_fp_draw_layer("F.Fab")
    assert fp.spin_fp_draw_width.value() == pytest.approx(0.25)
    fp.btn_fp_draw_width_reset.click()
    assert fp._fp_layer_width("F.Fab") == 0.10 and fp.spin_fp_draw_width.value() == pytest.approx(0.10)


def test_fill_is_enabled_only_for_tools_with_an_area_and_fills_new_shapes(fp):
    # CONTRACT CHANGED (review A3): the line keeps «Riempi» enabled, since it
    # closes into a polygon on its first point; an OPEN line is still never filled.
    for tool, enabled in (("rectangle", True), ("polygon", True), ("circle", True),
                          ("line", True), ("arc", False), ("bezier", False), ("text", False)):
        fp._toggle_footprint_draw_tool(tool)
        assert fp.chk_fp_draw_fill.isEnabled() is enabled, tool
        fp._cancel_footprint_drawing()
    fp.chk_fp_draw_fill.setChecked(True)
    fp._toggle_footprint_draw_tool("rectangle")
    _click(fp, 0, 0)
    _click(fp, 3, 2)
    assert _last(fp).get("fill") is True
    fp._toggle_footprint_draw_tool("line")
    _click(fp, 0, 5)
    _click(fp, 3, 5)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Return)
    assert "fill" not in _last(fp), "a line has no area: never filled"


# ── New kinds ──────────────────────────────────────────────────────────

def test_the_polygon_closes_at_three_vertices_not_two_and_writes_fp_poly(fp):
    fp._toggle_footprint_draw_tool("polygon")
    for point in ((0, 0), (3, 0), (0, 0)):
        _click(fp, *point)
    assert fp._fp_draw_tool == "polygon" and not fp.footprint_drawings, "A,B,A does not close"
    _click(fp, 3, 3)
    _click(fp, 0, 0)
    assert fp._fp_draw_tool is None
    assert _last(fp)["kind"] == "polygon" and len(_last(fp)["points"]) == 4
    assert "fp_poly" in drawing_graphics([_last(fp)])[0].to_sexpr().render()


def test_a_polygon_finished_with_two_vertices_is_discarded(fp):
    fp._toggle_footprint_draw_tool("polygon")
    _click(fp, 0, 0)
    _click(fp, 3, 0)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Return)
    assert not fp.footprint_drawings


def test_the_bezier_tool_takes_four_clicks_and_writes_fp_curve(fp):
    fp._toggle_footprint_draw_tool("bezier")
    for point in ((0, 0), (1, 3), (4, -3)):
        _click(fp, *point)
    assert fp._fp_draw_tool == "bezier" and not fp.footprint_drawings
    _click(fp, 5, 0)
    record = _last(fp)
    assert record["kind"] == "curve" and record["points"] == [[0, 0], [1, 3], [4, -3], [5, 0]]
    assert "fp_curve" in drawing_graphics([record])[0].to_sexpr().render()


def test_the_text_box_takes_two_corners_and_a_multi_line_text(fp, dialogs):
    fp._toggle_footprint_draw_tool("textbox")
    _click(fp, 0, 0)
    _click(fp, 6, 3)
    record = _last(fp)
    assert record["kind"] == "textbox" and record["text"] == "one\ntwo"
    assert "fp_text_box" in drawing_graphics([record])[0].to_sexpr().render()
    dialogs["multi"] = ("  ", True)
    count = len(fp.footprint_drawings)
    fp._toggle_footprint_draw_tool("textbox")
    _click(fp, 0, 5)
    _click(fp, 6, 8)
    assert len(fp.footprint_drawings) == count, "a blank text box is not stored"


def test_a_new_text_box_keeps_its_leading_indentation(fp, dialogs):
    """Review C5 on the creation path: only trailing newlines are dropped
    (positive: the indent stays; negative: the trailing newline goes)."""
    dialogs["multi"] = ("    indented\n  second  \n", True)
    fp._toggle_footprint_draw_tool("textbox")
    _click(fp, 0, 0)
    _click(fp, 6, 3)
    assert _last(fp)["text"] == "    indented\n  second  "


@pytest.mark.parametrize("variant, dtype", [("dimension_radial", "radial"), ("dimension_center", "center"),
                                            ("dimension_leader", "leader")])
def test_the_two_click_dimensions_write_their_type(fp, dialogs, variant, dtype):
    dialogs["text"] = ("NOTE", True)
    fp._toggle_footprint_draw_tool(variant)
    _click(fp, 0, 0)
    assert not fp.footprint_drawings
    _click(fp, 3, 0)
    record = _last(fp)
    assert record["kind"] == "dimension" and record["dtype"] == dtype
    text = drawing_graphics([record])[0].to_sexpr().render()
    assert f"(type {dtype})" in text
    if dtype == "radial":
        from kicadgen.core.footprint import DIMENSION_RADIAL_LEADER_MM
        assert record["leader_length"] == DIMENSION_RADIAL_LEADER_MM
    if dtype == "leader":
        assert record["text"] == "NOTE"


def test_the_orthogonal_dimension_measures_the_axis_its_placement_chooses(fp):
    # CONTRACT CHANGED (review M7, 2026-11-08): the axis comes from the third
    # click as in KiCad (above/below the points -> X, beside them -> Y), no
    # longer from the dominant direction of the two points.
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    for point in ((0, 0), (5, 1), (0, 3)):
        _click(fp, *point)
    assert _last(fp)["axis"] == "x" and _last(fp)["offset"] == 3.0
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    for point in ((0, 0), (1, 5), (3, 2)):
        _click(fp, *point)
    assert _last(fp)["axis"] == "y" and _last(fp)["offset"] == 3.0
    fp._toggle_footprint_draw_tool("dimension_orthogonal")
    for point in ((0, 0), (5, 1), (8, 0)):
        _click(fp, *point)
    assert _last(fp)["axis"] == "y", "beside the points: Y, although |dX| > |dY|"


def test_the_dimension_button_remembers_its_variant_and_ctrl_shift_h_arms_it(fp, dialogs):
    grid = fp.fp_tool_grid
    action = next(a for a in grid.button("dimension").menu().actions() if a.data() == "dimension_center")
    action.trigger()
    assert fp._fp_draw_tool == "dimension_center"
    fp._cancel_footprint_drawing()
    QTest.keyClick(fp.preview_view, Qt.Key.Key_H,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert fp._fp_draw_tool == "dimension_center"


def test_new_kinds_are_drawn_tagged_with_their_layer(fp, dialogs):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, LAYER_ROLE
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "curve", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [1, 2], [3, -2], [4, 0]]},
        {"kind": "textbox", "layer": "Dwgs.User", "width": 0.1, "points": [[0, 3], [5, 6]], "text": "a\nb"},
        {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[0, 8], [2, 8]], "dtype": "radial"},
        {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[5, 8], [6, 8]], "dtype": "leader",
         "text": "X", "frame": "circle"},
    ])
    fp._draw_preview()
    tagged = {}
    for item in fp.preview_scene.items():
        index = item.data(DRAWING_KEY_ROLE)
        if index is not None:
            tagged.setdefault(index, set()).add(item.data(LAYER_ROLE))
    assert tagged == {0: {"F.Fab"}, 1: {"Dwgs.User"}, 2: {"F.Fab"}, 3: {"F.Fab"}}
    # Properties of every new kind open without error (values built for each).
    for index in range(4):
        values = fp._drawing_properties_values(fp.footprint_drawings[index])
        assert values["layer"]
    assert fp._drawing_properties_values(fp.footprint_drawings[1])["text"] == "a\nb"
    assert fp.set_drawing_properties(3, {"frame": "rect", "text": "Y"})
    assert fp.footprint_drawings[3]["frame"] == "rect" and fp.footprint_drawings[3]["text"] == "Y"


def test_a_user_drawings_layer_drawing_round_trips(fp):
    raw = {"kind": "line", "layer": "Dwgs.User", "width": 0.1, "points": [[0, 0], [1, 1]]}
    clean = validate_footprint_drawings([raw])
    assert clean == [raw]
    assert '(layer "Dwgs.User")' in drawing_graphics(clean)[0].to_sexpr().render()
    assert "Dwgs.User" in fp.fp_layer_boxes and fp.combo_fp_draw_layer.findData("Dwgs.User") >= 0


# ── Angle mode ─────────────────────────────────────────────────────────

def test_the_45_degree_mode_snaps_a_40_degree_segment_and_shift_frees_it(fp):
    angle = math.radians(40)
    x, y = 5 * math.cos(angle), 5 * math.sin(angle)
    fp.chk_fp_object_snap.setChecked(False)
    constrained = _snap(fp, x, y, points=[(0, 0)], mode="45")
    assert constrained[0] == pytest.approx(constrained[1]) and constrained[0] > 0, "on the 45 degree ray"
    free = _snap(fp, x, y, points=[(0, 0)], free=True, mode="45")
    assert free == (round(x), round(y)), "Shift: grid only, the angle stays 40 degrees"
    # A rectangle corner is never constrained.
    assert _snap(fp, x, y, points=[(0, 0)], mode="45", key="rectangle") == (round(x), round(y))


def test_the_angle_constraint_applies_through_real_clicks(fp):
    fp.chk_fp_object_snap.setChecked(False)
    fp.combo_fp_angle_mode.setCurrentIndex(fp.combo_fp_angle_mode.findData("90"))
    fp._toggle_footprint_draw_tool("line")
    _click(fp, 0, 0)
    _click(fp, 4, 1)
    _click(fp, 5, 5, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Return)
    assert _last(fp)["points"] == [[0, 0], [4, 0], [5, 5]]


def test_shift_space_cycles_the_mode_and_the_combo_follows(fp):
    combo = fp.combo_fp_angle_mode
    assert combo.currentData() == "free"
    fp._toggle_footprint_draw_tool("line")
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)
    assert combo.currentData() == "90" and fp.preview_view.click_router.angle_mode == "90"
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)
    assert combo.currentData() == "45"
    assert fp._config["fp_angle_mode"] == "45", "saved"
    combo.setCurrentIndex(combo.findData("free"))
    assert fp.preview_view.click_router.angle_mode == "free", "the control drives the router too"


# ── Object snap ────────────────────────────────────────────────────────

def _pad_targets(fp, x_mm, y_mm):
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui.canvas_measure import SnapTargets
    fp.chk_fp_object_snap.setChecked(True)
    # A redraw still queued from the fixture would put the real footprint's targets back.
    QApplication.processEvents()
    fp._fp_geometry_targets = SnapTargets(centres=((x_mm * PX, y_mm * PX, "1"),))


def test_the_object_snap_catches_a_pad_within_ten_pixels_not_at_twelve(fp):
    _pad_targets(fp, 2.03, 2.03)
    # 1 screen px per scene px: 10 px = 0.5 mm.
    assert _snap(fp, 2.03 + 0.45, 2.03) == (2.03, 2.03)
    assert fp._fp_snap_hit[1] == "pad"
    assert _snap(fp, 2.03 + 0.6, 2.03) == (3, 2), "12 px away: the grid"
    fp.chk_fp_object_snap.setChecked(False)
    assert _snap(fp, 2.03 + 0.45, 2.03) == (2, 2), "box off: the grid only"
    fp.chk_fp_object_snap.setChecked(True)
    assert _snap(fp, 2.03 + 0.45, 2.03, free=True) == (2, 2), "Shift: no object snap"


def test_the_object_snap_catches_a_drawing_endpoint(fp):
    fp.chk_fp_object_snap.setChecked(True)
    fp.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[1.3, 1.3], [4.3, 1.3]]}])
    assert _snap(fp, 4.6, 1.4) == (4.3, 1.3)
    assert fp._fp_snap_hit[1] == "endpoint"
    fp.fp_layer_boxes["F.SilkS"].setChecked(False)
    assert _snap(fp, 4.6, 1.4) == (5, 1), "a hidden layer is not caught"


def test_the_typed_marker_shows_while_hovering_and_goes_away(fp, monkeypatch):
    from kicadgen.gui.canvas_snap_marker import SNAP_MARKER_ROLE
    # A preview redraw during the moves must not put the real footprint's targets back.
    monkeypatch.setattr(fp, "_sync_footprint_measures", lambda *args, **kwargs: None)
    _pad_targets(fp, 2.03, 2.03)

    def markers():
        return [i for i in fp.preview_scene.items() if i.data(SNAP_MARKER_ROLE) == fp._FP_SNAP_MARKER_TAG]

    fp._toggle_footprint_draw_tool("line")
    _pad_targets(fp, 2.03, 2.03)
    _move(fp, 2.2, 2.1)
    assert len(markers()) == 1, (fp._fp_draw_hover, fp._fp_snap_hit)
    _move(fp, 6, 6)
    assert not markers(), "away from the pad: no marker"
    _move(fp, 2.2, 2.1)
    assert markers()
    fp._cancel_footprint_drawing()
    assert not markers(), "disarm removes it"


def test_hovering_does_not_rebuild_the_snap_index(fp, monkeypatch):
    fp.chk_fp_object_snap.setChecked(True)
    built = []
    real = fp._build_fp_snap_index
    monkeypatch.setattr(fp, "_build_fp_snap_index", lambda *a: built.append(1) or real(*a))
    fp._toggle_footprint_draw_tool("line")
    for x in (1, 2, 3, 4):
        _move(fp, x + 0.2, 1.1)
    assert len(built) <= 1


# ── Backspace and hints ────────────────────────────────────────────────

def test_backspace_removes_a_point_and_the_hint_follows(fp):
    from kicadgen.gui.canvas_tool_hints import step_hint
    hint = fp.fp_hint_label
    assert hint.text() == ""
    fp._toggle_footprint_draw_tool("rectangle")
    assert hint.text() == step_hint("rectangle", 0)
    _click(fp, 1, 1)
    assert hint.text() == step_hint("rectangle", 1)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Backspace)
    assert fp._fp_draw_clicks == [] and fp._fp_draw_tool == "rectangle"
    assert hint.text() == step_hint("rectangle", 0)
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Backspace)
    assert fp._fp_draw_tool == "rectangle", "with no point Backspace is a no-op"
    fp._cancel_footprint_drawing()
    assert hint.text() == ""


# ── Active layer from the «Layer» group ────────────────────────────────

def test_clicking_a_layer_name_activates_it_and_shows_a_hidden_layer(fp):
    names, boxes = fp.fp_layer_names, fp.fp_layer_boxes
    # B.SilkS and B.Fab are drawable since 2026-11-09 (plan F1); the zones row is not a drawing layer.
    assert set(names) == {"F.SilkS", "B.SilkS", "F.Fab", "B.Fab", "Cmts.User", "Dwgs.User"}
    assert "zones" not in names
    assert "pads" not in names and "F.CrtYd" not in names, "non-drawable rows are not selectable"
    boxes["Cmts.User"].setChecked(False)
    QTest.mouseClick(names["Cmts.User"], Qt.MouseButton.LeftButton)
    assert fp.combo_fp_draw_layer.currentData() == "Cmts.User"
    assert boxes["Cmts.User"].isChecked(), "a hidden layer becomes visible"
    assert names["Cmts.User"].property("active") and not names["F.SilkS"].property("active")
    from kicadgen.gui.main_window import KICAD_LAYER_COLORS
    assert fp.fp_tool_grid.accent() == KICAD_LAYER_COLORS["Cmts.User"], "accent = active layer colour"
    fp.combo_fp_draw_layer.setCurrentIndex(fp.combo_fp_draw_layer.findData("F.Fab"))
    assert names["F.Fab"].property("active") and not names["Cmts.User"].property("active")


# ── Shortcuts ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, tool", [(Qt.Key.Key_L, "line"), (Qt.Key.Key_P, "polygon"),
                                       (Qt.Key.Key_B, "bezier"), (Qt.Key.Key_X, "textbox")])
def test_ctrl_shift_shortcuts_arm_their_tool(fp, key, tool):
    fp.preview_view.setFocus()
    QTest.keyClick(fp.preview_view, key, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert fp._fp_draw_tool == tool


def test_the_shortcuts_dialog_lists_the_drawing_tools(fp, monkeypatch):
    monkeypatch.setattr(fp, "_run_modal", lambda box: None)
    fp._show_shortcuts()
    text = fp._shortcuts_box.text()
    for sequence in ("Ctrl+Shift+L", "Ctrl+Shift+H", "Shift+Space", "Backspace"):
        assert sequence in text
