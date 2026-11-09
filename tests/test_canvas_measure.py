"""@brief Shared measuring tool (`kicadgen.gui.canvas_measure`), plan docs/canvas-measure-plan.md.

Pure geometry first (pad hit areas, snap priority, alignment guides,
linear dimensions, perpendicular foot, label), then the Qt overlay (add,
remove, hover highlight, hit test, sync, hover feedback) and the host glue on
a real `PointCollector` (two-click and three-click measures, the Escape
ladder, removal by menu and Delete). The adoption in each tab, through real
events, lives in `tests/test_footprint_measure_parity.py` and
`tests/test_symbol_measure_parity.py` (plan §6). Every rule has a positive
and a negative case.
"""
import math

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt                # noqa: E402
from PyQt6.QtGui import QKeyEvent                                  # noqa: E402
from PyQt6.QtWidgets import QGraphicsLineItem, QGraphicsScene, QGraphicsView  # noqa: E402

from kicadgen.gui import canvas_measure as cm                      # noqa: E402
from kicadgen.gui import canvas_menus                              # noqa: E402
from kicadgen.gui.canvas_clicktools import PointCollector         # noqa: E402

#: Two 20 x 10 pads (scene units) at (0, 0) and (100, 0), a third at (100, 60), a body 200 x 160.
PAD1 = cm.pad_polygon(0.0, 0.0, 20.0, 10.0, "rect")
PAD2 = cm.pad_polygon(100.0, 0.0, 20.0, 10.0, "rect")
PAD3 = cm.pad_polygon(100.0, 60.0, 20.0, 10.0, "rect")
BODY_CORNERS, BODY_EDGES = cm.SnapTargets.rectangle(-50.0, -80.0, 150.0, 80.0)
TARGETS = cm.SnapTargets(centres=((0.0, 0.0, "1", PAD1), (100.0, 0.0, "2", PAD2), (100.0, 60.0, "3", PAD3)),
                         corners=BODY_CORNERS, edges=BODY_EDGES)
FIRST = cm.MeasureAnchor(0.0, 0.0, cm.KIND_CENTRE, "1", shape=PAD1)


def _esc():
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)


# ── Pad hit areas and snap priority ────────────────────────────────────────

def test_anywhere_on_the_pad_copper_takes_the_pad_centre():
    anchor = cm.snap_point(9.0, 4.0, TARGETS, scale=1.0)       # a corner of pad 1's copper
    assert (anchor.kind, anchor.x, anchor.y, anchor.label) == (cm.KIND_CENTRE, 0.0, 0.0, "1")
    assert anchor.shape == PAD1


def test_the_pad_margin_is_in_screen_pixels():
    # 4 px outside the copper: within the 6 px margin at scale 1, out of it at scale 4.
    assert cm.snap_point(14.0, 0.0, TARGETS, scale=1.0).kind == cm.KIND_CENTRE
    assert cm.snap_point(14.0, 0.0, TARGETS, scale=4.0).kind == cm.KIND_FREE


def test_a_pad_centre_beats_a_body_edge_and_corner_in_reach():
    targets = cm.SnapTargets(centres=((0.0, 0.0, "1", cm.pad_polygon(0.0, 0.0, 20.0, 20.0, "rect")),),
                             corners=((12.0, 12.0),), edges=((-50.0, 8.0, 50.0, 8.0),))
    assert cm.snap_point(9.0, 9.0, targets).kind == cm.KIND_CENTRE


def test_corner_within_twelve_pixels_edge_within_ten():
    corner = cm.snap_point(-50.0 + 8.0, -80.0 + 8.0, TARGETS)          # 11.3 px from the corner
    assert (corner.kind, corner.x, corner.y) == (cm.KIND_CORNER, -50.0, -80.0)
    edge = cm.snap_point(60.0, -80.0 + 9.0, TARGETS)
    assert edge.kind == cm.KIND_EDGE and (edge.x, edge.y) == pytest.approx((60.0, -80.0))
    far = cm.snap_point(60.0, -80.0 + 11.0, TARGETS)
    assert far.kind == cm.KIND_FREE


def test_nothing_in_reach_is_a_free_point_never_a_grid_node():
    anchor = cm.snap_point(37.3, 21.7, TARGETS)
    assert (anchor.kind, anchor.x, anchor.y) == (cm.KIND_FREE, 37.3, 21.7)


def test_shift_gives_the_raw_point_even_on_a_pad():
    anchor = cm.snap_point(3.0, 2.0, TARGETS, free=True)
    assert (anchor.kind, anchor.x, anchor.y) == (cm.KIND_FREE, 3.0, 2.0)


def test_pad_polygons_follow_shape_and_rotation():
    turned = cm.pad_polygon(0.0, 0.0, 20.0, 10.0, "rect", 90.0)
    xs, ys = [p[0] for p in turned], [p[1] for p in turned]
    assert max(xs) == pytest.approx(5.0) and max(ys) == pytest.approx(10.0)
    oval = cm.pad_polygon(0.0, 0.0, 30.0, 10.0, "oval")
    assert cm.point_in_polygon(13.0, 0.0, oval) and not cm.point_in_polygon(14.5, 4.5, oval)
    circle = cm.pad_polygon(0.0, 0.0, 10.0, 10.0, "circle")
    assert cm.point_in_polygon(3.0, 3.0, circle) and not cm.point_in_polygon(4.0, 4.0, circle)


def test_the_edge_foot_is_perpendicular_and_clamped_to_the_segment():
    assert cm.segment_foot(3.0, 7.0, (0.0, 0.0, 10.0, 0.0)) == (3.0, 0.0)
    assert cm.segment_foot(15.0, 7.0, (0.0, 0.0, 10.0, 0.0)) == (10.0, 0.0)
    assert cm.line_foot(15.0, 7.0, (0.0, 0.0, 10.0, 0.0)) == (15.0, 0.0)


# ── Alignment guides and linear dimensions ─────────────────────────────────

def test_crossing_the_vertical_of_another_pad_snaps_x_and_starts_a_horizontal_dimension():
    index = cm.GuideIndex(TARGETS)
    anchor = cm.snap_second(104.0, 30.0, TARGETS, index, FIRST)
    assert (anchor.kind, anchor.x, anchor.y, anchor.axis) == (cm.KIND_ALIGNED, 100.0, 30.0, "x")
    assert anchor.guides[0][0] == "v" and anchor.guides[0][1] == 100.0
    # Negative: 9 px off the vertical is out of the 8 px guide reach.
    assert cm.snap_second(109.0, 30.0, TARGETS, index, FIRST).kind == cm.KIND_FREE


def test_crossing_the_horizontal_of_a_pad_starts_a_vertical_dimension():
    index = cm.GuideIndex(TARGETS)
    anchor = cm.snap_second(40.0, 63.0, TARGETS, index, FIRST)
    assert (anchor.kind, anchor.y, anchor.axis) == (cm.KIND_ALIGNED, 60.0, "y")
    assert anchor.guides[0][0] == "h"


def test_the_first_anchor_own_vertical_is_not_a_guide():
    index = cm.GuideIndex(TARGETS)
    anchor = cm.snap_second(3.0, 30.0, TARGETS, index, FIRST)
    assert anchor.kind == cm.KIND_FREE


def test_a_second_pad_hit_is_linear_when_one_delta_dominates_else_diagonal():
    index = cm.GuideIndex(TARGETS)
    same_row = cm.snap_second(104.0, 2.0, TARGETS, index, FIRST)
    assert (same_row.kind, same_row.label, same_row.axis) == (cm.KIND_CENTRE, "2", "x")
    diagonal_targets = cm.SnapTargets(centres=TARGETS.centres + ((60.0, 50.0, "4",
                                                                  cm.pad_polygon(60.0, 50.0, 20.0, 10.0)),))
    diagonal = cm.snap_second(60.0, 50.0, diagonal_targets, cm.GuideIndex(diagonal_targets), FIRST)
    assert (diagonal.label, diagonal.axis) == ("4", "")


def test_an_edge_in_reach_wins_over_a_guide():
    index = cm.GuideIndex(TARGETS)
    anchor = cm.snap_second(100.0 + 3.0, 80.0 - 5.0, TARGETS, index, FIRST)
    assert anchor.kind == cm.KIND_EDGE


def test_the_linear_dimension_measures_one_delta_and_has_extension_lines():
    second = cm.MeasureAnchor(100.0, 30.0, cm.KIND_ALIGNED, "2", axis="x")
    place = cm.MeasureAnchor(50.0, 120.0, cm.KIND_PLACE)
    m = cm.measure_linear(FIRST, second, 10.0, place)
    assert (m.axis, m.distance_mm, m.dx_mm, m.dy_mm) == ("x", pytest.approx(10.0), pytest.approx(10.0), 0.0)
    assert m.start == (0.0, 120.0) and m.end == (100.0, 120.0)
    gap, over = cm.EXTENSION_GAP_MM * 10, cm.EXTENSION_OVERSHOOT_MM * 10
    assert m.extensions[0] == (0.0, gap, 0.0, 120.0 + over)
    vertical = cm.measure_linear(FIRST, cm.MeasureAnchor(40.0, 60.0, cm.KIND_ALIGNED, axis="y"), 10.0,
                                 cm.MeasureAnchor(-30.0, 0.0, cm.KIND_PLACE))
    assert vertical.start == (-30.0, 0.0) and vertical.end == (-30.0, 60.0)
    assert vertical.distance_mm == pytest.approx(6.0)
    with pytest.raises(ValueError):
        cm.measure_linear(FIRST, cm.MeasureAnchor(1.0, 1.0), 10.0)


def test_measure_points_picks_linear_or_true_distance():
    linear = cm.measure_points((FIRST, cm.MeasureAnchor(100.0, 30.0, cm.KIND_ALIGNED, axis="x")), 10.0)
    assert linear.axis == "x" and linear.start == (0.0, 0.0)          # no placement: through the first anchor
    diagonal = cm.measure_points((FIRST, cm.MeasureAnchor(30.0, 40.0)), 10.0)
    assert diagonal.axis == "" and diagonal.distance_mm == pytest.approx(5.0)


def test_a_centre_to_an_edge_is_the_perpendicular_distance():
    centre = cm.MeasureAnchor(10.0, 50.0, cm.KIND_CENTRE, "1")
    edge = cm.MeasureAnchor(90.0, 0.0, cm.KIND_EDGE, edge=(-100.0, 0.0, 100.0, 0.0))
    m = cm.measure(centre, edge, 10.0)
    assert m.perpendicular and m.end == pytest.approx((10.0, 0.0))
    assert m.distance_mm == pytest.approx(5.0)
    assert not cm.measure(centre, cm.MeasureAnchor(90.0, 0.0, cm.KIND_CENTRE), 10.0).perpendicular


def test_two_parallel_edges_give_the_width_two_crossing_edges_a_straight_line():
    top = cm.MeasureAnchor(5.0, -20.0, cm.KIND_EDGE, edge=(-30.0, -20.0, 30.0, -20.0))
    bottom = cm.MeasureAnchor(-12.0, 20.0, cm.KIND_EDGE, edge=(30.0, 20.0, -30.0, 20.0))
    assert cm.measure(top, bottom, 10.0).distance_mm == pytest.approx(4.0)
    side = cm.MeasureAnchor(30.0, 7.0, cm.KIND_EDGE, edge=(30.0, -20.0, 30.0, 20.0))
    assert not cm.measure(top, side, 10.0).perpendicular


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_a_non_positive_scale_is_refused(bad):
    with pytest.raises(ValueError):
        cm.measure(cm.MeasureAnchor(0.0, 0.0), cm.MeasureAnchor(1.0, 0.0), bad)


def test_the_labels_have_two_decimals():
    a, b = cm.MeasureAnchor(0.0, 0.0), cm.MeasureAnchor(25.4, 0.0)
    assert cm.measurement_label(cm.measure(a, b, 10.0)) == "2.54 mm · ΔX 2.54 · ΔY 0.00"
    overlay = cm.MeasureOverlay(10.0)
    linear = cm.measure_linear(a, cm.MeasureAnchor(25.4, 9.0, axis="x"), 10.0)
    assert overlay.label(linear) == "2.54 mm"


# ── Qt overlay ─────────────────────────────────────────────────────────────

def test_the_dimension_line_draws_ticks_line_label_in_the_old_order(qapp):
    scene = QGraphicsScene()
    items = cm.add_dimension_line(scene, (0.0, 30.0), (100.0, 30.0), "totale 5.00 mm",
                                  cm.DIMENSION_STYLE_HORIZONTAL, normal=(0.0, 1.0))
    lines = [i.line() for i in items if isinstance(i, QGraphicsLineItem)]
    assert [(ln.x1(), ln.y1(), ln.x2(), ln.y2()) for ln in lines] == [
        (0.0, 25.0, 0.0, 35.0), (100.0, 25.0, 100.0, 35.0), (0.0, 30.0, 100.0, 30.0)]
    assert items[-1].text() == "totale 5.00 mm" and items[-1].pos().y() == 12.0


def test_the_overlay_adds_removes_and_hit_tests_its_measures(qapp):
    scene = QGraphicsScene()
    counts = []
    overlay = cm.MeasureOverlay(10.0, on_changed=counts.append)
    overlay.add(scene, cm.measure(cm.MeasureAnchor(0.0, 0.0), cm.MeasureAnchor(100.0, 0.0), 10.0))
    overlay.add(scene, cm.measure_linear(cm.MeasureAnchor(0.0, 50.0), cm.MeasureAnchor(100.0, 50.0, axis="x"),
                                         10.0, cm.MeasureAnchor(0.0, 90.0, cm.KIND_PLACE)))
    assert overlay.count == 2 and counts == [1, 2]
    assert overlay.measurement_at(QPointF(50.0, 3.0), 1.0) == 0
    assert overlay.measurement_at(QPointF(50.0, 89.0), 1.0) == 1          # the dimension line
    assert overlay.measurement_at(QPointF(100.0, 70.0), 1.0) == 1         # an extension line
    assert overlay.measurement_at(QPointF(50.0, 30.0), 1.0) is None
    items = overlay.items()
    assert all(i.data(cm.MEASURE_ITEM_ROLE) is True for i in items)
    assert all(i.acceptedMouseButtons() == Qt.MouseButton.NoButton for i in items)
    assert overlay.set_hovered(scene, 1) and overlay.hovered == 1
    assert not overlay.set_hovered(scene, 1)
    assert overlay.remove(scene, 0) and overlay.count == 1 and overlay.hovered is None
    assert not overlay.remove(scene, 5)
    assert overlay.clear() and overlay.count == 0 and counts[-1] == 0


def test_sync_keeps_the_measures_of_the_same_geometry_and_drops_the_others(qapp):
    scene = QGraphicsScene()
    overlay = cm.MeasureOverlay(10.0)
    overlay.sync(scene, TARGETS)
    overlay.add(scene, cm.measure(cm.MeasureAnchor(0.0, 0.0), cm.MeasureAnchor(100.0, 0.0), 10.0))
    overlay.forget_items()
    scene.clear()
    overlay.sync(scene, cm.SnapTargets(centres=TARGETS.centres, corners=TARGETS.corners, edges=TARGETS.edges))
    assert overlay.count == 1 and [i for i in scene.items() if i.data(cm.MEASURE_ITEM_ROLE)]
    overlay.sync(scene, cm.SnapTargets(centres=((0.0, 0.0, "1"),)))
    assert overlay.count == 0 and not [i for i in scene.items() if i.data(cm.MEASURE_ITEM_ROLE)]


def test_the_hover_feedback_highlights_the_target_and_shows_the_tag_and_guides(qapp):
    from PyQt6.QtWidgets import QGraphicsPolygonItem, QGraphicsSimpleTextItem
    scene = QGraphicsScene()
    overlay = cm.MeasureOverlay(10.0)
    overlay.show_transient(scene, [], cm.snap_point(9.0, 4.0, TARGETS), "Pad 1 · centro")
    polygons = [i for i in overlay.items() if isinstance(i, QGraphicsPolygonItem)]
    texts = [c.text() for i in overlay.items() for c in i.childItems() if isinstance(c, QGraphicsSimpleTextItem)]
    assert polygons and "Pad 1 · centro" in texts
    aligned = cm.snap_second(104.0, 30.0, TARGETS, cm.GuideIndex(TARGETS), FIRST)
    overlay.show_transient(scene, [FIRST], aligned, "Allineato: Pad 2")
    dashed = [i for i in overlay.items() if isinstance(i, QGraphicsLineItem)
              and i.pen().style() == Qt.PenStyle.DashLine]
    assert dashed, "the guide and the live preview are dashed"
    assert any(i.line().x1() == i.line().x2() == 100.0 for i in dashed), "a vertical guide through pad 2"
    overlay.clear_transient()
    assert overlay.items() == []


# ── Host glue on a real collector ──────────────────────────────────────────

class _Host:
    """A minimal ClickToolHost forwarding the measuring tool to a MeasureTool."""

    def __init__(self):
        self.tool = None
        self.disarmed = 0

    def tool_snap(self, spec, point, free, points):
        return self.tool.snap(point, free, points)

    def tool_points_changed(self, spec, points):
        self.tool.points_changed(points)

    def tool_preview_changed(self, spec, points, preview):
        self.tool.preview_changed(points, preview)

    def tool_refused(self, spec, reason):
        pass

    def tool_finished(self, spec, points, commit):
        return self.tool.finished(points, commit)

    def tool_armed(self, spec):
        pass

    def tool_disarmed(self, spec):
        self.disarmed += 1
        self.tool.disarmed()


@pytest.fixture
def glue(qapp):
    scene = QGraphicsScene()
    view = QGraphicsView(scene)
    view.resize(400, 300)
    host = _Host()
    collector = PointCollector(host)
    host.tool = cm.MeasureTool(view, collector, units_per_mm=10.0)
    host.tool.sync(TARGETS)
    yield host, collector, view
    view.deleteLater()


def test_a_diagonal_measure_takes_two_clicks(glue):
    host, collector, _view = glue
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(1.0, 1.0))
    collector.click(QPointF(101.0, 61.0))
    assert collector.armed and collector.points == []
    (m,) = host.tool.overlay.measurements
    assert (m.axis, m.distance_mm) == ("", pytest.approx(math.hypot(10.0, 6.0)))


def test_a_linear_dimension_takes_a_placement_click(glue):
    host, collector, _view = glue
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(0.0, 0.0))
    collector.click(QPointF(104.0, 30.0))
    assert len(collector.points) == 2 and host.tool.overlay.count == 0
    collector.click(QPointF(40.0, 120.0))
    (m,) = host.tool.overlay.measurements
    assert (m.axis, m.distance_mm, m.start[1]) == ("x", pytest.approx(10.0), 120.0)
    assert collector.armed and collector.points == []


def test_the_escape_ladder_points_then_measures_then_the_router(glue):
    host, collector, _view = glue
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(0.0, 0.0))
    collector.click(QPointF(104.0, 30.0))
    assert host.tool.escape(_esc()) is True                  # the placement stage is cancelled
    assert collector.armed and collector.points == [] and host.tool.overlay.count == 0
    collector.click(QPointF(0.0, 0.0))
    collector.click(QPointF(101.0, 61.0))
    assert host.tool.overlay.count == 1
    assert host.tool.escape(_esc()) is True
    assert collector.armed and host.tool.overlay.count == 0
    assert host.tool.escape(_esc()) is False


def test_escape_is_left_to_the_router_during_a_pan_and_when_not_armed(glue):
    host, collector, _view = glue

    class _Pan:
        pan_active = True

    assert host.tool.escape(_esc()) is False
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(0.0, 0.0))
    assert host.tool.escape(_esc(), _Pan()) is False and collector.points


def test_a_cancel_without_escape_disarms_and_keeps_the_measures(glue):
    host, collector, _view = glue
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(0.0, 0.0))
    collector.click(QPointF(101.0, 61.0))
    collector.click(QPointF(0.0, 0.0))
    collector.cancel()
    assert not collector.armed and host.tool.overlay.count == 1


def test_the_hover_tag_names_the_target(glue):
    host, _collector, _view = glue
    tool = host.tool
    assert tool.tag(cm.snap_point(9.0, 4.0, TARGETS)) == "Pad 1 · centro"
    assert tool.tag(cm.snap_point(-45.0, -75.0, TARGETS)) == "Spigolo"
    assert tool.tag(cm.snap_point(60.0, -78.0, TARGETS)) == "Bordo"
    assert tool.tag(cm.snap_point(37.0, 21.0, TARGETS)) == "Punto libero"
    assert tool.tag(cm.snap_second(104.0, 30.0, TARGETS, cm.GuideIndex(TARGETS), FIRST)).startswith("Allineato: Pad ")


def test_a_measure_is_removed_by_its_menu_or_by_delete_when_hovered(glue, monkeypatch):
    host, collector, view = glue
    view.resetTransform()
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(0.0, 0.0))
    collector.click(QPointF(101.0, 61.0))
    collector.click(QPointF(-45.0, -75.0))
    collector.click(QPointF(101.0, 61.0))
    assert host.tool.overlay.count == 2
    chosen = []
    monkeypatch.setattr(canvas_menus, "exec_element_menu",
                        lambda menu, keys, pos: (chosen.append([a.text() for a in keys]), canvas_menus.DELETE)[1])
    on_line = view.mapFromScene(QPointF(50.0, 30.0))
    assert host.tool.context_menu(on_line, QPoint(0, 0)) is True
    assert chosen == [["Rimuovi misura"]] and host.tool.overlay.count == 1
    # Negative: away from every measure there is no menu.
    assert host.tool.context_menu(view.mapFromScene(QPointF(-40.0, 60.0)), QPoint(0, 0)) is False
    # Delete acts on the measure under the CURSOR at the key press (finding 5).
    from PyQt6.QtGui import QCursor
    view.show()
    QCursor.setPos(view.viewport().mapToGlobal(view.mapFromScene(QPointF(-40.0, 60.0))))
    assert host.tool.remove_hovered() is False
    on_second = view.mapFromScene(QPointF(25.0, -10.0))
    host.tool.hover_measure(on_second)
    assert host.tool.overlay.hovered == 0
    QCursor.setPos(view.viewport().mapToGlobal(on_second))
    assert host.tool.remove_hovered() is True and host.tool.overlay.count == 0


def test_delete_on_a_measure_reads_the_view_s_shared_pointer(glue, monkeypatch):
    """VER5 D: in both tabs the view carries the shared `PointerTracker` (`view.pointer`), which E
    and the keyboard fallback read; Delete on a measure read `QCursor.pos()` instead, so one key
    press could see two pointers. With the platform cursor parked far away, a tracked pointer on the
    measure removes it; negative: the tracked pointer gone (left), the platform cursor ON the
    measure removes nothing."""
    from PyQt6.QtGui import QCursor
    from kicadgen.gui.canvas_hit import PointerTracker
    host, collector, view = glue
    view.resetTransform()
    view.show()
    collector.arm(cm.MEASURE_SPEC)
    collector.click(QPointF(-45.0, -75.0))
    collector.click(QPointF(101.0, 61.0))
    assert host.tool.overlay.count == 1
    on_line = view.mapFromScene(QPointF(25.0, -10.0))
    view.pointer = PointerTracker(view)
    on_screen = view.viewport().mapToGlobal(on_line)
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(on_screen)))
    view.pointer.left()
    assert host.tool.remove_hovered() is False, "negative: no tracked pointer, the cursor is not read"
    far = view.viewport().mapToGlobal(QPoint(-500, -500))
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(far)))
    view.pointer.moved(on_line)
    assert host.tool.remove_hovered() is True and host.tool.overlay.count == 0



def test_a_point_on_a_large_pad_beats_the_margin_of_a_small_neighbour():
    """Finding 3 (QFN): on the exposed pad, its centre wins over a lead 0.25 mm away."""
    exposed = cm.pad_polygon(0.0, 0.0, 62.0, 62.0, "rect")            # 3.1 mm at 20 px/mm
    lead = cm.pad_polygon(41.0 + 6.0, 0.0, 16.0, 6.0, "rect")         # its edge 5 px from the exposed pad
    targets = cm.SnapTargets(centres=((0.0, 0.0, "EP", exposed), (47.0, 0.0, "9", lead)))
    assert cm.snap_point(29.0, 0.0, targets).label == "EP"
    # Negative: just outside the exposed pad, inside the lead's margin, the lead is taken.
    assert cm.snap_point(35.0, 0.0, targets).label == "9"


def test_the_guide_index_skips_a_run_of_excluded_coordinates():
    """Finding 4: six centres on the first anchor's X must not hide the next X."""
    centres = tuple((0.0, float(i), str(i)) for i in range(6)) + ((1.0, 10.0, "X"),)
    index = cm.GuideIndex(cm.SnapTargets(centres=centres))
    assert index.nearest_x(-0.5, 2.0, exclude=0.0)[2] == "X"
    assert index.nearest_x(-0.5, 1.0, exclude=0.0) is None


def test_an_editable_dimension_carries_its_key_and_a_computed_one_none(qapp):
    """`add_dimension_line(edit_key=...)` tags every item in both label branches; None tags nothing."""
    scene = QGraphicsScene()
    tagged = cm.add_dimension_line(scene, (0.0, 0.0), (100.0, 0.0), "5.00 mm",
                                   cm.DIMENSION_STYLE_HORIZONTAL, edit_key="body_x")
    screen = cm.add_dimension_line(scene, (0.0, 50.0), (100.0, 50.0), "5.00 mm",
                                   cm.MEASURE_STYLE, edit_key="body_y")
    plain = cm.add_dimension_line(scene, (0.0, 90.0), (100.0, 90.0), "totale 5.00 mm",
                                  cm.DIMENSION_STYLE_HORIZONTAL)
    assert all(item.data(cm.DIMENSION_EDIT_ROLE) == "body_x" for item in tagged)
    assert all(item.data(cm.DIMENSION_EDIT_ROLE) == "body_y" for item in screen)
    assert all(item.data(cm.DIMENSION_EDIT_ROLE) is None for item in plain)


def test_a_hovered_editable_dimension_brightens_and_comes_back(qapp):
    """Hover feedback of an editable dimension; a computed one is never touched."""
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    scene = QGraphicsScene()
    items = cm.add_dimension_line(scene, (0.0, 0.0), (100.0, 0.0), "5.00 mm",
                                  cm.DIMENSION_STYLE_HORIZONTAL, edit_key="body_x")
    other = cm.add_dimension_line(scene, (0.0, 90.0), (100.0, 90.0), "totale",
                                  cm.DIMENSION_STYLE_HORIZONTAL)
    label = next(i for i in items if isinstance(i, QGraphicsSimpleTextItem))
    cm.set_dimension_highlight(scene, "body_x", True)
    assert label.brush().color() == QColor(cm.MEASURE_HIGHLIGHT_COLOR)
    assert all(i.pen().color() == QColor(cm.DIMENSION_COLOR) for i in other
               if isinstance(i, QGraphicsLineItem)), "a computed dimension is never highlighted"
    cm.set_dimension_highlight(scene, "body_x", False)
    assert label.brush().color() == QColor(cm.DIMENSION_COLOR)
