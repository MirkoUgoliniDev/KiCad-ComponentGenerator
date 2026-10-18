"""@brief Shared element selection (`kicadgen.gui.canvas_selection`)."""
import pytest
from PyQt6.QtCore import QPoint, QRectF
from PyQt6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView

from kicadgen.gui.canvas_selection import IndexSelection, RubberBand, encloses, is_click


def test_select_replaces_unless_additive():
    s = IndexSelection()
    s.select(2)
    s.select(5)
    assert s.indices(10) == [5] and s.primary == 5
    s.select(1, additive=True)
    assert s.indices(10) == [1, 5] and s.primary == 1


def test_toggle_adds_then_removes_and_moves_the_primary():
    s = IndexSelection()
    assert s.toggle(3) is True and s.toggle(7) is True
    assert s.indices(10) == [3, 7] and s.primary == 7
    assert s.toggle(7) is False
    assert s.indices(10) == [3] and s.primary == 3
    assert s.toggle(3) is False and not s


def test_set_many_replaces_or_adds_and_picks_the_highest_primary():
    s = IndexSelection()
    s.select(1)
    s.set_many({4, 2})
    assert s.indices(10) == [2, 4] and s.primary == 4
    s.set_many({0}, additive=True)
    assert s.indices(10) == [0, 2, 4]
    s.set_many(set())
    assert not s and s.primary is None


def test_indices_drop_out_of_range_entries_and_contains():
    s = IndexSelection()
    s.set_many({1, 8})
    assert s.indices(5) == [1]
    assert 8 in s and 2 not in s and None not in s
    s.select(None)
    assert not s


def test_is_click_uses_the_drag_distance():
    d = max(QApplication.startDragDistance(), 1)
    assert is_click(QPoint(10, 10), QPoint(10, 10))
    assert is_click(QPoint(10, 10), QPoint(10 + d - 1, 10))
    assert not is_click(QPoint(10, 10), QPoint(10 + d, 10))


@pytest.fixture
def view(qapp):
    scene = QGraphicsScene()
    scene.setSceneRect(QRectF(-500, -500, 1000, 1000))
    v = QGraphicsView(scene)
    v.resize(300, 200)
    v.show()
    qapp.processEvents()
    yield v
    v.hide()
    v.deleteLater()


def test_rubber_band_returns_the_scene_rect_and_releases(view):
    band = RubberBand(view)
    assert not band.active and band.finish(QPoint(1, 1)) is None
    band.begin(QPoint(10, 20), additive=True)
    assert band.active and band.additive
    assert band.move(QPoint(60, 80))
    rect = band.finish(QPoint(60, 80))
    expected = view.mapToScene(10, 20)
    assert rect.topLeft() == expected and not band.active


def test_rubber_band_cancel(view):
    band = RubberBand(view)
    assert band.cancel() is False
    band.begin(QPoint(5, 5), additive=False)
    assert band.cancel() is True and not band.active and not band.move(QPoint(9, 9))


def test_snapshot_and_restore_round_trip_and_include_the_primary():
    s = IndexSelection()
    s.set_many({2, 5})
    state = s.snapshot()
    s.clear()
    s.restore(state)
    assert s.indices(10) == [2, 5] and s.primary == 5
    s.restore((7, {1}))
    assert s.indices(10) == [1, 7] and s.primary == 7


def test_band_visible_follows_begin_and_finish(view):
    band = RubberBand(view)
    assert not band.visible
    band.begin(QPoint(5, 5), additive=False)
    assert band.visible
    band.finish(QPoint(20, 20))
    assert not band.visible


def test_encloses_accepts_flat_lines_and_points_inside_and_refuses_cut_shapes():
    """@brief Positive: horizontal line, vertical line, a text point inside. Negative: a shape the band cuts."""
    band = QRectF(0, 0, 10, 10)
    assert encloses(band, (1, 5, 9, 5))          # horizontal line
    assert encloses(band, (5, 9, 5, 1))          # vertical line, reversed order
    assert encloses(band, (3, 3, 3, 3))          # a text anchor point
    assert encloses(band, (0, 0, 10, 10))        # touching the edges
    assert not encloses(band, (5, 5, 12, 6))     # cut by the band
    assert not encloses(band, (-1, 5, -1, 5))    # outside


def test_a_band_released_where_it_started_is_a_click_not_a_band(view):
    """@brief Shift+click without a drag selects nothing and clears nothing. Negative: a real drag returns a rect."""
    band = RubberBand(view)
    band.begin(QPoint(40, 40), additive=False)
    assert band.finish(QPoint(40, 40)) is None and not band.active
    band.begin(QPoint(40, 40), additive=False)
    assert band.finish(QPoint(90, 90)) is not None
