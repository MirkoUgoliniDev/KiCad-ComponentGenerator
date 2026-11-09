"""@brief Shared outline hit (H1, 2026-11-09) and the closing handle of a closed outline (H2).

`kicadgen.gui.canvas_hit`: an unfilled rectangle, ellipse, polygon or path is
hit on its stroke only, a filled one also inside, as KiCad's
`EDA_SHAPE::hitTest` (common/eda_shape.cpp). `kicadgen.core.vertex_edit`
`handle_points` / `move_handle_point`: a polyline closed on its first point
has one handle there, and moving it moves both ends. Each rule has a positive
and a negative case that differ structurally (filled or not, closed or open).
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF  # noqa: E402
from PyQt6.QtGui import QPainterPath, QPen, QPolygonF  # noqa: E402

from kicadgen.core import vertex_edit as ve  # noqa: E402
from kicadgen.gui import canvas_hit as ch  # noqa: E402


def _pen(width=2.0):
    pen = QPen()
    pen.setWidthF(width)
    return pen


def _item(cls, *geometry, width=2.0, filled=False):
    item = cls(*geometry)
    item.setPen(_pen(width))
    item.set_interior_hit(filled)
    return item


SQUARE = QPolygonF([QPointF(0, 0), QPointF(100, 0), QPointF(100, 80), QPointF(0, 80)])


@pytest.mark.parametrize("make", [
    lambda filled: _item(ch.OutlineRectItem, 0, 0, 100, 80, filled=filled),
    lambda filled: _item(ch.OutlinePolygonItem, SQUARE, filled=filled),
    lambda filled: _item(ch.OutlineEllipseItem, 0, 0, 100, 80, filled=filled),
], ids=["rectangle", "polygon", "ellipse"])
def test_an_unfilled_closed_shape_is_hit_on_its_outline_and_a_filled_one_also_inside(qapp, make):
    empty, full = make(False), make(True)
    centre, rim = QPointF(50, 40), QPointF(50, 0)
    assert empty.contains(rim) and full.contains(rim), "the outline is hit in both"
    assert not empty.contains(centre), "negative: the inside of an unfilled shape is not hit"
    assert full.contains(centre), "a filled shape is hit inside"
    assert not empty.shape().contains(centre) and full.shape().contains(centre), "Qt's shape says the same"


def test_the_outline_reach_is_half_the_pen_width(qapp):
    """KiCad: maxdist = width / 2 (+ accuracy, the view's own margin)."""
    item = _item(ch.OutlineRectItem, 0, 0, 100, 80, width=6.0)
    assert item.contains(QPointF(50, 2.9)) and item.contains(QPointF(50, -2.9))
    assert not item.contains(QPointF(50, 3.5)), "negative: beyond width / 2 inside"
    assert not item.contains(QPointF(50, -3.5)), "negative: beyond width / 2 outside"


def test_a_wider_pen_widens_the_hit_without_a_new_item(qapp):
    """The selection restyle thickens the pen: the cached shape follows it."""
    item = _item(ch.OutlineRectItem, 0, 0, 100, 80, width=2.0)
    assert not item.contains(QPointF(50, 2.0))
    item.setPen(_pen(6.0))
    assert item.contains(QPointF(50, 2.0))


def test_an_open_l_is_not_hit_inside_its_bend_unless_filled(qapp):
    """An «L» path: Qt's default shape holds the triangle closed by its chord; KiCad does not."""
    path = QPainterPath(QPointF(0, 0))
    path.lineTo(100, 0)
    path.lineTo(100, 100)
    inside_bend = QPointF(80, 20)
    empty = _item(ch.OutlinePathItem, path)
    assert empty.contains(QPointF(50, 0)) and empty.contains(QPointF(100, 50)), "both legs are hit"
    assert not empty.contains(inside_bend), "negative: the chord's triangle is not the element"
    assert not empty.contains(QPointF(50, 50)), "negative: nor the chord itself"
    assert _item(ch.OutlinePathItem, path, filled=True).contains(inside_bend), \
        "a filled polyline is closed by its chord, as KiCad fills it"


def test_the_qt_default_shape_holds_the_inside_the_mixin_removes(qapp):
    """Witness of the defect: the plain Qt items answer inside an unfilled outline."""
    from PyQt6.QtWidgets import QGraphicsRectItem
    plain = QGraphicsRectItem(0, 0, 100, 80)
    plain.setPen(_pen())
    assert plain.contains(QPointF(50, 40)), "Qt's own rectangle is hit inside"
    assert not _item(ch.OutlineRectItem, 0, 0, 100, 80).contains(QPointF(50, 40))


def test_a_zone_never_takes_an_interior_hit(qapp):
    """A keepout zone is hatched over its area but selected by its border only."""
    from kicadgen.gui.footprint_view import ZoneOutlineItem
    zone = ZoneOutlineItem(SQUARE)
    zone.setPen(_pen(1.0))
    zone.set_interior_hit(True)
    assert not zone.contains(QPointF(50, 40)) and zone.contains(QPointF(0, 40))


def test_a_zero_width_pen_still_has_a_hittable_outline(qapp):
    item = _item(ch.OutlineRectItem, 0, 0, 100, 80, width=0.0)
    assert not item.shape().isEmpty()
    assert not item.contains(QPointF(50, 40))


# ── H2: the closing handle of a closed outline ─────────────────────────

CLOSED = [(0, 0), (4, 0), (4, 3), (0, 0)]
OPEN = [(0, 0), (4, 0), (4, 3), (0, 3)]


def test_a_closed_outline_has_one_handle_at_its_closing_point():
    assert ve.handle_points(CLOSED) == [(0, 0), (4, 0), (4, 3)]
    assert ve.handle_points(OPEN) == OPEN, "negative: an open line keeps a handle per point"


def test_an_a_b_a_back_and_forth_keeps_both_end_handles():
    """Two distinct vertices enclose nothing: not a closed outline (`is_closed_outline`)."""
    assert ve.handle_points([(0, 0), (4, 0), (0, 0)]) == [(0, 0), (4, 0), (0, 0)]


@pytest.mark.parametrize("position", [0, 3])
def test_moving_the_closing_point_moves_both_ends(position):
    moved = ve.move_handle_point(CLOSED, position, (1, 1))
    assert moved == [(1, 1), (4, 0), (4, 3), (1, 1)]
    assert ve.is_closed_outline(moved), "the outline stays closed"
    assert CLOSED == [(0, 0), (4, 0), (4, 3), (0, 0)], "the input is not mutated"


def test_moving_an_end_of_an_open_line_moves_that_end_alone():
    assert ve.move_handle_point(OPEN, 0, (1, 1)) == [(1, 1), (4, 0), (4, 3), (0, 3)]
    assert ve.move_handle_point(OPEN, 3, (1, 1)) == [(0, 0), (4, 0), (4, 3), (1, 1)]


def test_moving_a_middle_vertex_of_a_closed_outline_keeps_the_ends():
    assert ve.move_handle_point(CLOSED, 1, (5, 0)) == [(0, 0), (5, 0), (4, 3), (0, 0)]


def test_stored_list_points_stay_lists():
    moved = ve.move_handle_point([list(p) for p in CLOSED], 0, (2, 2))
    assert moved[0] == [2, 2] and moved[-1] == [2, 2] and moved[0] is not moved[-1]


def test_a_position_outside_the_points_is_refused():
    with pytest.raises(IndexError):
        ve.move_handle_point(OPEN, 4, (0, 0))


# -- pick: one precedence (coordinator decision, 2026-11-09) ------------------

@pytest.fixture
def scene_view(qapp):
    """A shown 400 x 300 view at 1 view px per scene px, scene origin at the top-left corner."""
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView
    scene = QGraphicsScene()
    view = QGraphicsView(scene)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setSceneRect(QRectF(0, 0, 400, 300))
    view.resize(400, 300)
    view.show()
    qapp.processEvents()
    yield scene, view
    view.close()
    view.deleteLater()
    qapp.processEvents()


def _tagged(scene, x, y, w, h, tag, z=0.0):
    item = scene.addRect(x, y, w, h)
    item.setData(0, tag)
    item.setZValue(z)
    return item


def _classify(item):
    tag = item.data(0)
    return None if tag is None else ({"handle": ch.RANK_HANDLE, "pin": ch.RANK_PIN, "text": ch.RANK_TEXT,
                                      "stroke": ch.RANK_STROKE}[tag], tag)


def _at(view, x, y):
    return view.mapFromScene(QPointF(x, y))


def test_pick_ranks_inside_the_margin_pass(scene_view):
    """A pin 2 px away beats a stroke 3 px away (ranked); none is exactly under the point."""
    scene, view = scene_view
    _tagged(scene, 100, 100, 10, 10, "pin")             # right edge at x = 110
    _tagged(scene, 115, 90, 1, 30, "stroke", z=5)       # left edge at x = 115, above the pin
    found = ch.pick(view, _at(view, 112, 105), _classify, 4)
    assert found is not None and found.target == "pin" and found.exact is False


def test_pick_takes_the_exact_pass_before_the_margin_whatever_the_rank(scene_view):
    """Negative twin: the stroke exactly under the point wins over a pin within the margin."""
    scene, view = scene_view
    _tagged(scene, 100, 100, 10, 10, "pin")
    _tagged(scene, 113, 90, 1, 30, "stroke")
    found = ch.pick(view, _at(view, 113, 105), _classify, 4)
    assert found is not None and found.target == "stroke" and found.exact is True


def test_pick_ranks_inside_the_exact_pass_and_breaks_ties_by_z(scene_view):
    scene, view = scene_view
    _tagged(scene, 100, 100, 20, 20, "stroke", z=9)
    _tagged(scene, 100, 100, 20, 20, "handle", z=1)
    assert ch.pick(view, _at(view, 110, 110), _classify, 4).target == "handle", "the rank beats the z order"
    low = _tagged(scene, 200, 100, 20, 20, "pin", z=1)
    high = _tagged(scene, 200, 100, 20, 20, "pin", z=2)
    assert ch.pick(view, _at(view, 210, 110), _classify, 4).item is high and low is not high


def test_pick_finds_nothing_beyond_the_margin_and_honours_the_filter(scene_view):
    scene, view = scene_view
    _tagged(scene, 100, 100, 10, 10, "pin")
    assert ch.pick(view, _at(view, 118, 105), _classify, 4) is None, "8 px away: beyond the margin"
    no_pins = lambda item: None if item.data(0) == "pin" else _classify(item)  # noqa: E731
    assert ch.pick(view, _at(view, 105, 105), no_pins, 4) is None, "a filtered kind is no target"


# -- PointerTracker: the pointer across camera and layout changes (review 2026-11-09) --

def test_the_tracked_pointer_follows_a_layout_shift_of_the_viewport(scene_view):
    """Point 3: the layout moved the viewport (a panel expanding) without a mouse move. The tracker
    keeps a SCREEN point, so the viewport point is mapped through the viewport's current geometry.
    Positive: 30 px of left margin shift the answer 30 px left; negative: no shift, same point."""
    from PyQt6.QtCore import QPoint
    _scene, view = scene_view
    tracker = ch.PointerTracker(view)
    assert tracker.viewport_point() is None, "no move seen yet"
    tracker.moved(QPoint(120, 80))
    assert tracker.viewport_point() == QPoint(120, 80), "negative: no layout change, the move's own point"
    view.setViewportMargins(30, 0, 0, 0)
    assert tracker.viewport_point() == QPoint(90, 80), "the viewport moved 30 px right under a still pointer"
    assert tracker.scene_point() == view.mapToScene(QPoint(90, 80))


def test_the_tracked_pointer_is_gone_when_it_leaves_or_the_viewport_slides_away(scene_view):
    """A pointer the layout left outside the viewport, or one that left, is no pointer."""
    from PyQt6.QtCore import QPoint
    _scene, view = scene_view
    tracker = ch.PointerTracker(view)
    tracker.moved(QPoint(10, 80))
    view.setViewportMargins(30, 0, 0, 0)
    assert tracker.viewport_point() is None, "now left of the viewport"
    view.setViewportMargins(0, 0, 0, 0)
    assert tracker.viewport_point() == QPoint(10, 80)
    tracker.left()
    assert tracker.viewport_point() is None and tracker.scene_point() is None


def test_the_tracked_scene_point_follows_the_camera(scene_view):
    """Point 1: the pointer stays, the camera moves: the scene point under it changes with the camera."""
    from PyQt6.QtCore import QPoint
    _scene, view = scene_view
    tracker = ch.PointerTracker(view)
    tracker.moved(QPoint(100, 100))
    before = tracker.scene_point()
    view.scale(2.0, 2.0)
    after = tracker.scene_point()
    assert after != before and after == view.mapToScene(QPoint(100, 100))


# -- PointerTracker: the live branch reads the platform cursor (VER5 F) --

class _FakeCursor:
    """Stands in for `QCursor` inside `canvas_hit`: `pos()` answers a screen point the test sets."""

    screen = None

    @classmethod
    def pos(cls):
        from PyQt6.QtCore import QPoint
        return QPoint(cls.screen)


def test_after_a_real_move_the_tracker_reads_the_platform_cursor(scene_view, monkeypatch):
    """A real move (the platform cursor on the event's point, the viewport under the mouse) makes the
    tracker follow the platform cursor, also to a point whose move event never came. Negatives: the
    viewport no longer under the mouse gives the stored point; a synthetic move (the platform cursor
    elsewhere when it came) keeps its own point whatever the cursor does afterwards."""
    from PyQt6.QtCore import QPoint
    _scene, view = scene_view
    viewport = view.viewport()
    monkeypatch.setattr(ch, "QCursor", _FakeCursor)
    under = [True]
    monkeypatch.setattr(viewport, "underMouse", lambda: under[0])
    tracker = ch.PointerTracker(view)
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(120, 80))
    tracker.moved(QPoint(120, 80))
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(170, 100))
    assert tracker.viewport_point() == QPoint(170, 100), "live: the cursor moved without an event"
    under[0] = False
    assert tracker.viewport_point() == QPoint(120, 80), "negative: not under the mouse, the stored point"
    under[0] = True
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(300, 250))
    tracker.moved(QPoint(40, 40))
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(170, 100))
    assert tracker.viewport_point() == QPoint(40, 40), "negative: a synthetic move keeps its own point"


def test_a_move_within_the_live_tolerance_still_counts_as_real(scene_view, monkeypatch):
    """Scaling may round a pixel each way: within `LIVE_TOLERANCE_PX` the move is real; beyond, not."""
    from PyQt6.QtCore import QPoint
    _scene, view = scene_view
    viewport = view.viewport()
    monkeypatch.setattr(ch, "QCursor", _FakeCursor)
    monkeypatch.setattr(viewport, "underMouse", lambda: True)
    tracker = ch.PointerTracker(view)
    tolerance = ch.PointerTracker.LIVE_TOLERANCE_PX
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(100 + tolerance, 50))
    tracker.moved(QPoint(100, 50))
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(10, 10))
    assert tracker.viewport_point() == QPoint(10, 10)
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(100 + tolerance + 1, 50))
    tracker.moved(QPoint(100, 50))
    _FakeCursor.screen = viewport.mapToGlobal(QPoint(10, 10))
    assert tracker.viewport_point() == QPoint(100, 50), "negative: one pixel beyond, synthetic"


# -- ReachFeedback.refresh: cursor and tooltip for a still pointer (VER5 E) --

class _FakeToolTip:
    """Stands in for `QToolTip` inside `canvas_hit`: records what is shown or hidden."""

    visible = True
    calls = []

    @classmethod
    def isVisible(cls):
        return cls.visible

    @classmethod
    def showText(cls, pos, text, widget=None):
        cls.calls.append(("show", text))

    @classmethod
    def hideText(cls):
        cls.calls.append(("hide", None))


def test_reach_refresh_sets_and_undoes_the_cursor_for_a_still_pointer(scene_view):
    """The item with a cursor now under the pointer shows it; nothing under it gives the arrow back,
    whether the cursor was set here or by Qt for an exact hit."""
    from PyQt6.QtCore import QPoint, Qt
    scene, view = scene_view
    viewport = view.viewport()
    item = _tagged(scene, 100, 100, 20, 20, "stroke")
    item.setCursor(Qt.CursorShape.SizeAllCursor)
    reach = ch.ReachFeedback(view)
    reach.refresh(item, QPoint(5, 5))
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor
    reach.refresh(None, QPoint(5, 5))
    assert not viewport.testAttribute(Qt.WidgetAttribute.WA_SetCursor), "nothing under it: the arrow"
    viewport.setCursor(Qt.CursorShape.SizeAllCursor)        # as Qt sets it for an exact hit
    reach.refresh(None, QPoint(5, 5))
    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor
    plain = _tagged(scene, 200, 100, 20, 20, "stroke")
    reach.refresh(plain, QPoint(5, 5))
    assert not viewport.testAttribute(Qt.WidgetAttribute.WA_SetCursor), "an item without a cursor: the arrow"


def test_reach_refresh_follows_an_open_tooltip_only(scene_view, monkeypatch):
    """An open tooltip shows the new item's text, or hides when nothing is under the pointer.
    Negative: with no tooltip open, none is opened."""
    from PyQt6.QtCore import QPoint
    scene, view = scene_view
    monkeypatch.setattr(ch, "QToolTip", _FakeToolTip)
    monkeypatch.setattr(_FakeToolTip, "calls", [])
    item = _tagged(scene, 100, 100, 20, 20, "stroke")
    item.setToolTip("hint")
    reach = ch.ReachFeedback(view)
    _FakeToolTip.visible = True
    reach.refresh(item, QPoint(110, 110))
    reach.refresh(None, QPoint(110, 110))
    assert _FakeToolTip.calls == [("show", "hint"), ("hide", None)]
    _FakeToolTip.calls.clear()
    _FakeToolTip.visible = False
    reach.refresh(item, QPoint(110, 110))
    assert _FakeToolTip.calls == [], "negative: no tooltip open, none opened"
