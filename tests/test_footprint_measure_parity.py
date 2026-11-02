"""@brief Footprint preview behaviour the measuring tool must not change (plan docs/canvas-measure-plan.md §6).

Written BEFORE the measuring tool against the code of 2026-10-31:

- the overall dimension lines of the three preview drawers (pin header,
  castellated module, package): every yellow line and label, in stacking
  order, with geometry, pen, text, rotation and font. `_add_dim_h` /
  `_add_dim_v` are factored into the shared `canvas_measure.add_dimension_line`;
  this record proves the factoring draws the same items (golden
  `tests/data/measure_golden/footprint_dimensions.json`, regenerated only
  with `KICADGEN_REGEN_MEASURE_GOLDEN=1`, parsed so line endings never matter);
- the existing preview toolbar buttons keep their relative order;
- a draw tool still commits its record and Escape with nothing armed still
  reaches `escape_idle`;
- the draw-mode markers (`_screen_dot`, factored too) keep their role, size
  and z value.
"""
import json
import math
import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QPointF, Qt                                # noqa: E402
from PyQt6.QtGui import QColor                                      # noqa: E402
from PyQt6.QtWidgets import QGraphicsLineItem, QGraphicsSimpleTextItem  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "data" / "measure_golden" / "footprint_dimensions.json"
REGEN_ENV = "KICADGEN_REGEN_MEASURE_GOLDEN"


def _dimension_items(window):
    """Every yellow dimension line and label of the preview, bottom to top."""
    from kicadgen.gui import main_window as gui
    yellow = QColor(gui.YELLOW)
    found = []
    for item in window.preview_scene.items(Qt.SortOrder.AscendingOrder):
        if isinstance(item, QGraphicsLineItem) and item.pen().color() == yellow:
            line = item.line()
            found.append(["line", *(round(v, 4) for v in (line.x1(), line.y1(), line.x2(), line.y2())),
                          item.pen().widthF(), item.pen().isCosmetic(), item.zValue(), item.rotation()])
        elif isinstance(item, QGraphicsSimpleTextItem) and item.brush().color() == yellow:
            found.append(["text", item.text(), round(item.pos().x(), 4), round(item.pos().y(), 4),
                          item.rotation(), item.zValue(), item.font().pointSizeF(), item.font().bold()])
    return found


def _castellated():
    from test_contact_style import dev_kit
    from kicadgen.generators.castellated_module import CONTACT_HALF_HOLE
    return dev_kit(CONTACT_HALF_HOLE)


def _package():
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    return PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10,
                           pins=[PinSpec("1", -3.17, -2.11, 1.2, .6), PinSpec("2", 3.17, -2.11, 1.2, .6),
                                 PinSpec("3", 3.17, 2.11, 1.2, .6), PinSpec("4", -3.17, 2.11, 1.2, .6)])


def test_the_dimension_lines_of_the_three_drawers_are_unchanged(window):
    """The factored dimension helper draws exactly the items of the old `_add_dim_h` / `_add_dim_v`."""
    records = {}
    window._draw_preview()
    records["header"] = _dimension_items(window)
    window._draw_preview_castellated(_castellated())
    records["castellated"] = _dimension_items(window)
    window._draw_preview_package(_package())
    records["package"] = _dimension_items(window)
    assert all(records.values()), "every drawer draws dimension lines"
    text = json.dumps(records, indent=1, sort_keys=True) + "\n"
    if os.environ.get(REGEN_ENV):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_bytes(text.encode("utf-8"))
    assert GOLDEN.exists(), f"golden missing: run once with {REGEN_ENV}=1"
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == json.loads(text)


def test_a_total_dimension_label_reads_the_overall_size(window):
    """Negative/positive twin of the golden: the label is the catalogue's «totale» with 2 decimals."""
    window._draw_preview_package(_package())
    texts = [r[1] for r in _dimension_items(window) if r[0] == "text"]
    assert any(t.endswith(" mm") and "." in t for t in texts)
    assert not any("measure" in t.lower() for t in texts)


def test_the_existing_preview_toolbar_buttons_keep_their_order(window):
    """New buttons may be inserted, the existing ones keep their relative order."""
    header = window.btn_fp_undo.parentWidget().layout()
    widgets = []
    stack = [header]
    # The header is a QHBoxLayout inside the tab's outer layout: find it by the undo button.
    from PyQt6.QtWidgets import QHBoxLayout
    for layout in stack:
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.layout() is not None:
                stack.append(item.layout())
    row = next(lay for lay in stack if isinstance(lay, QHBoxLayout)
               and any(lay.itemAt(i).widget() is window.btn_fp_undo for i in range(lay.count())))
    for index in range(row.count()):
        widget = row.itemAt(index).widget()
        if widget is not None:
            widgets.append(widget)
    expected = [window.btn_fp_undo, window.btn_fp_redo, window.btn_fp_cancel, window.btn_fp_apply,
                window.btn_fp_recover, window.btn_pinout_2d, window.chk_grid, window.combo_grid_unit,
                window.combo_fine_grid]
    positions = [widgets.index(w) for w in expected]
    assert positions == sorted(positions)


def test_a_draw_tool_still_commits_its_record_and_escape_idle_still_fires(window):
    """The draw tools and the idle Escape are untouched by the measuring tool."""
    window._toggle_footprint_draw_tool("rectangle")
    assert window._fp_draw_tool == "rectangle"
    window._on_footprint_draw_clicked(0.0, 0.0)
    window._on_footprint_draw_clicked(40.0, 20.0)
    assert window._fp_draw_tool is None
    assert window.footprint_drawings[-1]["kind"] == "rectangle"
    assert window.footprint_drawings[-1]["points"] == [[0.0, 0.0], [2.0, 1.0]]
    fired = []
    window.preview_view.escape_idle.connect(lambda: fired.append(1))
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent
    window.preview_view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                Qt.KeyboardModifier.NoModifier))
    assert fired == [1]


def test_the_draw_markers_keep_their_role_size_and_z(window):
    """`_screen_dot` (factored into the shared module) keeps its marker contract."""
    from kicadgen.gui import main_window as gui
    dot = gui._screen_dot(window.preview_scene, 10.0, 20.0, 5, gui.YELLOW, gui.YELLOW, 0x44524D, 7)
    assert dot.data(0x44524D) is True
    assert dot.zValue() == 7
    assert dot.pos() == QPointF(10.0, 20.0)
    assert dot.rect().width() == 5 and dot.rect().x() == -2.5
    assert dot.flags() & dot.GraphicsItemFlag.ItemIgnoresTransformations
    assert dot.pen().widthF() == pytest.approx(1.6)


# ── The measuring tool in the footprint preview (plan §6) ────────────────────

from copy import deepcopy                                           # noqa: E402

from PyQt6.QtCore import QEvent, QPoint                             # noqa: E402
from PyQt6.QtGui import QKeyEvent                                   # noqa: E402
from PyQt6.QtTest import QTest                                      # noqa: E402

from kicadgen.gui import canvas_measure as cm                       # noqa: E402
from kicadgen.gui import canvas_menus                               # noqa: E402
from kicadgen.gui.canvas_measure import EDIT_MEASURE                 # noqa: E402

PX = 20.0
SHIFT = Qt.KeyboardModifier.ShiftModifier


def _esc():
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)


@pytest.fixture
def fp(window, qapp):
    """The preview shown with four 1.2 x 0.6 mm pads at (±3, ±2) mm and a 15 x 10 mm body, 1 px per scene px."""
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    from kicadgen.gui import main_window as gui
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    window.package_geometry = PackageGeometry(
        family="soic", body_length_mm=15, body_width_mm=10,
        pins=[PinSpec("1", -3.0, -2.0, 1.2, .6), PinSpec("2", 3.0, -2.0, 1.2, .6),
              PinSpec("3", 3.0, 2.0, 1.2, .6), PinSpec("4", -3.0, 2.0, 1.2, .6)])
    window._draw_preview()
    view = window.preview_view
    view.resetTransform()
    view.centerOn(0, 0)
    view.auto_fit = False
    qapp.processEvents()
    return window


def _fp_pos(window, x_mm, y_mm):
    return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))


def _fp_click(window, qapp, x_mm, y_mm, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.mouseClick(window.preview_view.viewport(), Qt.MouseButton.LeftButton, modifier, _fp_pos(window, x_mm, y_mm))
    qapp.processEvents()


def _fp_move(window, qapp, x_mm, y_mm):
    viewport = window.preview_view.viewport()
    pos = _fp_pos(window, x_mm, y_mm)
    QTest.mouseMove(viewport, pos + QPoint(1, 1))
    QTest.mouseMove(viewport, pos)
    qapp.processEvents()


def _fp_escape(window, qapp):
    window.preview_view.keyPressEvent(_esc())
    qapp.processEvents()


def _diagonal(window, qapp):
    """Pad 1 -> pad 3: ΔX 6, ΔY 4 mm, no dominant delta: a two-click true distance."""
    _fp_click(window, qapp, -3, -2)
    _fp_click(window, qapp, 3, 2)


def test_footprint_targets_are_the_pads_with_their_copper_and_the_fab_outline(fp):
    targets = fp._fp_measure.targets
    assert sorted(c[2] for c in targets.centres) == ["1", "2", "3", "4"]
    centre = next(c for c in targets.centres if c[2] == "1")
    assert centre[:2] == (-3.0 * PX, -2.0 * PX) and len(centre[3]) == 4
    assert len(targets.corners) == 4 and len(targets.edges) == 4
    assert (7.5 * PX, 5.0 * PX) in targets.corners


def test_footprint_a_click_anywhere_on_the_copper_takes_the_pad_centre_with_its_tag(fp, qapp):
    fp._toggle_footprint_measure()
    assert fp._fp_measure.armed and fp.btn_fp_measure.isChecked()
    assert fp._fp_draw_tool is None and fp._fp_draw_clicks == []
    _fp_move(fp, qapp, -3.5, -1.8)              # on pad 1's copper, 0.5 mm from its centre
    hover = fp._fp_measure._hover
    assert (hover.kind, hover.label) == (cm.KIND_CENTRE, "1")
    assert fp._fp_measure.tag(hover) == "Pad 1 · centro"
    from PyQt6.QtWidgets import QGraphicsPolygonItem
    assert [i for i in fp._fp_measure.overlay.items() if isinstance(i, QGraphicsPolygonItem)], "pad highlighted"
    _fp_click(fp, qapp, -3.5, -1.8)
    (first,) = fp._fp_measure._collector.points
    assert (first.x, first.y) == (-3.0 * PX, -2.0 * PX)
    # Negative: off the copper and away from any target the point is free (no grid node).
    _fp_escape(fp, qapp)
    _fp_move(fp, qapp, -0.37, 0.53)
    assert fp._fp_measure._hover.kind == cm.KIND_FREE
    assert fp._fp_measure.tag(fp._fp_measure._hover) == "Punto libero"


def test_footprint_horizontal_dimension_through_the_vertical_of_the_next_pad(fp, qapp):
    """Pad 1, move right across pad 2's vertical: guide and highlight, click, place below: |ΔX|."""
    fp._toggle_footprint_measure()
    _fp_click(fp, qapp, -3, -2)
    _fp_move(fp, qapp, 3.2, 0.0)
    hover = fp._fp_measure._hover
    assert (hover.kind, hover.axis, hover.x) == (cm.KIND_ALIGNED, "x", 3.0 * PX)
    assert hover.guides and hover.guides[0][0] == "v" and hover.guides[0][4] is not None
    from PyQt6.QtWidgets import QGraphicsLineItem
    guides = [i for i in fp._fp_measure.overlay.items() if isinstance(i, QGraphicsLineItem)
              and i.pen().style() == Qt.PenStyle.DashLine and i.line().x1() == i.line().x2() == 3.0 * PX]
    assert guides, "a dashed vertical guide through the pad"
    _fp_click(fp, qapp, 3.2, 0.0)
    assert len(fp._fp_measure._collector.points) == 2 and fp._fp_measure.overlay.count == 0
    _fp_move(fp, qapp, 0.0, 4.0)                # live placement preview
    _fp_click(fp, qapp, 0.0, 4.0)
    (m,) = fp._fp_measure.overlay.measurements
    assert (m.axis, m.distance_mm) == ("x", pytest.approx(6.0))
    assert m.start[1] == pytest.approx(4.0 * PX) and len(m.extensions) == 2
    assert fp._fp_measure.armed


def test_footprint_vertical_dimension_through_the_horizontal_of_the_next_pad(fp, qapp):
    fp._toggle_footprint_measure()
    _fp_click(fp, qapp, -3, -2)
    _fp_move(fp, qapp, 0.0, 2.2)
    assert (fp._fp_measure._hover.kind, fp._fp_measure._hover.axis) == (cm.KIND_ALIGNED, "y")
    _fp_click(fp, qapp, 0.0, 2.2)
    _fp_click(fp, qapp, -6.0, 0.0)
    (m,) = fp._fp_measure.overlay.measurements
    assert (m.axis, m.distance_mm) == ("y", pytest.approx(4.0))
    assert m.start[0] == pytest.approx(-6.0 * PX)


def test_footprint_free_diagonal_is_the_true_distance_in_two_clicks(fp, qapp):
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    (m,) = fp._fp_measure.overlay.measurements
    assert (m.axis, m.first.label, m.second.label) == ("", "1", "3")
    assert m.distance_mm == pytest.approx(math.hypot(6.0, 4.0))
    _fp_click(fp, qapp, -0.37, 0.53, SHIFT)
    _fp_click(fp, qapp, 1.63, 1.53, SHIFT)
    assert fp._fp_measure.overlay.measurements[-1].first.kind == cm.KIND_FREE


def test_footprint_escape_at_the_placement_stage_cancels_that_dimension(fp, qapp):
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_click(fp, qapp, -3, -2)
    _fp_click(fp, qapp, 3.2, 0.0)
    assert len(fp._fp_measure._collector.points) == 2
    _fp_escape(fp, qapp)
    assert fp._fp_measure.armed and fp._fp_measure._collector.points == []
    assert fp._fp_measure.overlay.count == 1, "the earlier measure stays"


def test_footprint_centre_to_body_edge_is_perpendicular(fp, qapp):
    fp._toggle_footprint_measure()
    _fp_click(fp, qapp, -3.0, -2.0)
    _fp_click(fp, qapp, 1.0, -4.9)          # near the top edge (y = -5 mm), far from a corner
    (m,) = fp._fp_measure.overlay.measurements
    assert m.second.kind == cm.KIND_EDGE and m.perpendicular
    assert m.end == pytest.approx((-3.0 * PX, -5.0 * PX)) and m.distance_mm == pytest.approx(3.0)


def test_footprint_measures_persist_escape_ladder_and_clear_button(fp, qapp):
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_click(fp, qapp, -3, 2)
    _fp_click(fp, qapp, 3, -2)
    assert fp._fp_measure.overlay.count == 2 and fp.btn_fp_measure_clear.isEnabled()
    fp._draw_preview()
    assert fp._fp_measure.overlay.count == 2
    _fp_click(fp, qapp, 3, 2)
    _fp_escape(fp, qapp)
    assert fp._fp_measure.armed and fp._fp_measure.overlay.count == 2
    _fp_escape(fp, qapp)
    assert fp._fp_measure.armed and fp._fp_measure.overlay.count == 0 and not fp.btn_fp_measure_clear.isEnabled()
    _fp_escape(fp, qapp)
    assert not fp._fp_measure.armed and not fp.btn_fp_measure.isChecked()
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp.btn_fp_measure_clear.click()
    assert fp._fp_measure.overlay.count == 0


def test_footprint_right_click_on_a_measure_offers_remove_and_not_the_pad_menu(fp, qapp, monkeypatch):
    pads = []
    # The pad editor is a modal dialog: record the request instead of opening it.
    fp.preview_view.pad_context_requested.disconnect()
    fp.preview_view.pad_context_requested.connect(pads.append)
    offered = []
    monkeypatch.setattr(canvas_menus, "exec_element_menu",
                        lambda menu, keys, pos: (offered.append(list(keys.values())), canvas_menus.DELETE)[1])
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp._toggle_footprint_measure()
    assert not fp._fp_measure.armed and fp._fp_measure.overlay.count == 1
    count = fp._fp_measure.overlay.count
    QTest.mouseClick(fp.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _fp_pos(fp, 0.0, 0.0))
    qapp.processEvents()
    # flipped: was [[DELETE]]; the footprint also offers «Modifica quota…» (edit the value,
    # user 2026-11-02), the symbol editor does not (no on_edit host).
    assert offered == [[EDIT_MEASURE, canvas_menus.DELETE]] and fp._fp_measure.overlay.count == count - 1
    assert pads == []
    # Negative: a right click on a pad away from any measure still opens the pad editor.
    QTest.mouseClick(fp.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _fp_pos(fp, -3.0, 2.0))
    qapp.processEvents()
    assert pads and len(offered) == 1


def test_footprint_hovering_a_measure_highlights_it_and_delete_removes_it(fp, qapp):
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp._toggle_footprint_measure()
    drawings = deepcopy(fp.footprint_drawings)
    _fp_move(fp, qapp, 0.0, 0.0)
    assert fp._fp_measure.overlay.hovered == 0
    fp.preview_view.setFocus()
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Delete)
    qapp.processEvents()
    assert fp._fp_measure.overlay.count == 0 and fp.footprint_drawings == drawings
    # Negative: away from the measure nothing is hovered.
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_move(fp, qapp, -6.0, 4.0)
    assert fp._fp_measure.overlay.hovered is None


def _kicad_mod_text(window):
    """The .kicad_mod text of the previewed package, without its random UUIDs."""
    import re
    from kicadgen.generators.package import build_package
    text = build_package(window._preview_params()).to_sexpr().render()
    return re.sub(r'\(uuid "[^"]*"\)', '(uuid)', text)


def test_footprint_measures_are_not_in_the_draft_the_undo_the_kicad_text_or_the_framing(fp, qapp):
    state = deepcopy(fp._footprint_edit_state())
    undo = len(fp._fp_drawings_undo)
    before = _kicad_mod_text(fp)
    framing = fp._preview_framing_rect()
    fp._toggle_footprint_measure()
    _fp_click(fp, qapp, -3, -2)
    _fp_click(fp, qapp, 3.2, 0.0)
    _fp_click(fp, qapp, 0.0, 6.0)
    assert fp._fp_measure.overlay.count == 1
    assert fp._footprint_edit_state() == state and len(fp._fp_drawings_undo) == undo
    after = _kicad_mod_text(fp)
    assert after == before and "⊥" not in after and "ΔX" not in after
    assert [i for i in fp.preview_scene.items() if i.data(cm.MEASURE_ITEM_ROLE)]
    assert fp._preview_framing_rect() == framing


def test_footprint_measures_and_a_pending_point_go_when_the_geometry_changes(fp, qapp):
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_click(fp, qapp, -3, -2)
    fp._draw_preview()                                  # same geometry: both kept
    assert fp._fp_measure.overlay.count == 1 and len(fp._fp_measure._collector.points) == 1
    fp.package_geometry = PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10,
                                          pins=[PinSpec("1", -4.0, -2.0, 1.2, .6), PinSpec("2", 4.0, 2.0, 1.2, .6)])
    fp._draw_preview()
    assert fp._fp_measure.overlay.count == 0
    assert fp._fp_measure.armed and fp._fp_measure._collector.points == []


def test_footprint_project_switch_clears_the_measures_of_both_tabs(fp, qapp):
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    tab = fp.symbol_tab
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)], pad_ids=[str(i) for i in range(1, 6)])
    assert tab.toggle_measure()
    tab._collector.click(QPointF(3.7, 5.3), free=True)
    tab._collector.click(QPointF(23.7, 15.3), free=True)
    assert tab._measure.overlay.count == 1
    fp._apply_project_state(fp._project_state())
    qapp.processEvents()
    assert fp._fp_measure.overlay.count == 0 and not fp._fp_measure.armed
    assert tab._measure.overlay.count == 0


def test_footprint_escape_during_a_press_drag_adds_no_point_at_the_release(fp, qapp):
    """D1: Esc between the press and the release drops the point; the release adds none."""
    fp._toggle_footprint_measure()
    view = fp.preview_view
    start, end = _fp_pos(fp, -3, -2), _fp_pos(fp, 3, 2)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), end)
    assert len(fp._fp_measure._collector.points) == 1
    _fp_escape(fp, qapp)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp.processEvents()
    assert fp._fp_measure.armed and fp._fp_measure._collector.points == [] and fp._fp_measure.overlay.count == 0
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp.processEvents()
    assert fp._fp_measure.overlay.count == 1


def test_arming_a_draw_tool_disarms_the_measure_and_back(fp, qapp):
    fp._toggle_footprint_measure()
    fp._toggle_footprint_draw_tool("rectangle")
    assert not fp._fp_measure.armed and fp._fp_draw_tool == "rectangle" and not fp.btn_fp_measure.isChecked()
    fp._toggle_footprint_measure()
    assert fp._fp_measure.armed and fp._fp_draw_tool is None
    assert not fp._fp_draw_buttons["rectangle"].isChecked()


def _record_menu(monkeypatch):
    offered = []
    monkeypatch.setattr(canvas_menus, "exec_element_menu",
                        lambda menu, keys, pos: (offered.append(list(keys.values())), None)[1])
    return offered


def test_footprint_right_click_or_enter_commit_a_dimension_at_its_placement_stage(fp, qapp, monkeypatch):
    """Finding 1: at the placement stage a right click over an existing measure commits, no menu."""
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    offered = _record_menu(monkeypatch)
    _fp_click(fp, qapp, -3, -2)
    _fp_click(fp, qapp, 3.2, 0.0)
    QTest.mouseClick(fp.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _fp_pos(fp, 0.0, 0.0))
    qapp.processEvents()
    assert offered == [] and fp._fp_measure.overlay.count == 2
    assert fp._fp_measure.overlay.measurements[-1].axis == "x" and fp._fp_measure.armed
    _fp_click(fp, qapp, -3, -2)
    _fp_click(fp, qapp, 3.2, 0.0)
    fp.preview_view.setFocus()
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Return)
    qapp.processEvents()
    assert fp._fp_measure.overlay.count == 3


def test_footprint_right_click_over_a_measure_finishes_the_line_tool(fp, qapp, monkeypatch):
    """Finding 1: another click tool with points keeps its right click (the line is committed)."""
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp._toggle_footprint_draw_tool("line")
    count = len(fp.footprint_drawings)
    _fp_click(fp, qapp, -6, 4)
    _fp_click(fp, qapp, -1, 4)
    offered = _record_menu(monkeypatch)
    QTest.mouseClick(fp.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _fp_pos(fp, 0.0, 0.0))
    qapp.processEvents()
    assert offered == [] and len(fp.footprint_drawings) == count + 1
    # Negative: with no tool collecting, the same right click opens the measure menu.
    QTest.mouseClick(fp.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                     _fp_pos(fp, 0.0, 0.0))
    qapp.processEvents()
    assert offered == [[EDIT_MEASURE, canvas_menus.DELETE]]     # flipped: «Modifica quota…» added


def test_footprint_right_press_during_a_rubber_band_opens_no_measure_menu(fp, qapp, monkeypatch):
    """Finding 2: the band swallows the right press, as in the symbol editor."""
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp._toggle_footprint_measure()
    offered = _record_menu(monkeypatch)
    viewport = fp.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, SHIFT, _fp_pos(fp, -6, 4))
    QTest.mouseMove(viewport, _fp_pos(fp, 1, 1))
    assert fp.preview_view.band.active
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, _fp_pos(fp, 0, 0))
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, _fp_pos(fp, 0, 0))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, _fp_pos(fp, 1, 1))
    assert offered == [] and fp._fp_measure.overlay.count == 1


def test_footprint_delete_with_no_measure_under_the_pointer_reaches_the_drawing(fp, qapp):
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp._toggle_footprint_measure()
    fp._toggle_footprint_draw_tool("rectangle")
    fp._on_footprint_draw_clicked(-120.0, 60.0)
    fp._on_footprint_draw_clicked(-80.0, 90.0)
    count = len(fp.footprint_drawings)
    _fp_move(fp, qapp, -6.0, 3.75)
    assert fp._fp_measure.overlay.hovered is None
    fp.preview_view.setFocus()
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Delete)
    qapp.processEvents()
    assert len(fp.footprint_drawings) == count - 1 and fp._fp_measure.overlay.count == 1


def test_footprint_delete_after_a_pan_removes_only_what_is_under_the_pointer(fp, qapp):
    """Finding 5: a hover made stale by a camera move does not decide what Delete removes."""
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    fp._toggle_footprint_measure()
    _fp_move(fp, qapp, 0.0, 0.0)
    assert fp._fp_measure.overlay.hovered == 0
    fp.preview_view.centerOn(600.0, 600.0)          # the measure leaves the pointer
    qapp.processEvents()
    fp.preview_view.setFocus()
    QTest.keyClick(fp.preview_view, Qt.Key.Key_Delete)
    qapp.processEvents()
    assert fp._fp_measure.overlay.count == 1


def test_footprint_hide_disarms_and_keeps_the_measures(fp, qapp):
    from kicadgen.gui import main_window as gui
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_click(fp, qapp, -3, -2)
    fp.tabs.setCurrentIndex(gui.TAB_PROJECT)
    qapp.processEvents()
    assert not fp._fp_measure.armed and not fp.btn_fp_measure.isChecked()
    assert fp._fp_measure.overlay.count == 1 and fp._fp_measure.overlay._transient == []


def test_footprint_clear_measures_also_drops_the_pending_point(fp, qapp):
    """Finding 6."""
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_click(fp, qapp, -3, -2)
    fp.btn_fp_measure_clear.click()
    assert fp._fp_measure.overlay.count == 0
    assert fp._fp_measure.armed and fp._fp_measure._collector.points == []


def test_footprint_a_transiently_invalid_draft_keeps_the_measures(fp, qapp, monkeypatch):
    """Decision a: nothing could be built for a moment; the measures and the pending point stay."""
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    _fp_click(fp, qapp, -3, -2)
    monkeypatch.setattr(fp, "_preview_params", lambda: None)
    fp._draw_preview()
    assert fp._fp_measure.overlay.count == 1 and len(fp._fp_measure._collector.points) == 1
    monkeypatch.undo()
    fp._draw_preview()
    assert fp._fp_measure.overlay.count == 1 and len(fp._fp_measure._collector.points) == 1
    assert [i for i in fp.preview_scene.items() if i.data(cm.MEASURE_ITEM_ROLE)]


def test_footprint_package_targets_come_from_the_drawn_pads_when_the_build_fails(fp, qapp, monkeypatch):
    """Decision b: the pads are drawn, so they can be measured."""
    from kicadgen.gui import main_window as gui

    def broken(_package):
        raise ValueError("geometry not buildable")

    monkeypatch.setattr(gui, "build_package", broken)
    fp._draw_preview()
    targets = fp._fp_measure.targets
    assert sorted(c[2] for c in targets.centres) == ["1", "2", "3", "4"]
    assert len(targets.corners) == 4
    fp._toggle_footprint_measure()
    _diagonal(fp, qapp)
    assert fp._fp_measure.overlay.measurements[-1].distance_mm == pytest.approx(math.hypot(6.0, 4.0))
