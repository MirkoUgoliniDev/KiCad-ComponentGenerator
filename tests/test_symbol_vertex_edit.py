"""@brief Add / remove a vertex of a line or polygon in the symbol editor (plan F3).

Plan docs/footprint-back-anchor-vertices-zones-plan.md, F3, contracts 5 and 8:
a right click on a SEGMENT of a line or polygon offers «Aggiungi vertice», on
a VERTEX «Rimuovi vertice»; each edit is one undo step, never leaves an
invalid drawing and says why it was refused. The menu is driven through the
real `_graphic_context_menu` with `QMenu.exec` replaced by a fake that
triggers the chosen entry.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QContextMenuEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication, QMenu

from kicadgen.core import vertex_edit as ve
from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui import canvas_menus
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S, SymbolTab

ADD = 'Aggiungi vertice'
REMOVE = 'Rimuovi vertice'
ROWS = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 5)]
# Far from the pins and the body (body centred on the origin).
LINE = [(-30.48, 20.32), (-25.4, 20.32), (-25.4, 25.4)]
POLYGON = [(20.32, 20.32), (25.4, 20.32), (25.4, 25.4), (20.32, 25.4)]


def _settle(qapp, times=6):
    for _ in range(times):
        qapp.processEvents()


def _draw(tab, key, points):
    assert tab.start_shape(key) is not False
    for x, y in points:
        tab.add_shape_point(x, y)
    assert tab.finish_wire(True), key


@pytest.fixture
def tab(qapp):
    """@brief A shown symbol tab holding a line (element 0) and a polygon (element 1) on the 1.27 mm grid."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS), pad_ids=[r['pin'] for r in ROWS])
    tab.show()
    _settle(qapp)
    tab.set_drawing_grid(1.27)
    _draw(tab, 'line', LINE)
    _draw(tab, 'polygon', POLYGON)
    graphics = tab.graphics()
    assert [r['key'] for r in graphics] == ['shape:line', 'shape:polygon']
    yield tab
    tab.hide()
    tab.deleteLater()
    _settle(qapp, 2)


def _vertices(tab, index):
    return g.vertex_edit_shape(tab.graphics()[index])[0]


def _global(tab, x, y):
    """Screen position of a symbol point (mm, Y up)."""
    return tab.view.viewport().mapToGlobal(tab.view.mapFromScene(QPointF(x * S, -y * S)))


def _menu_at(tab, index, x, y, monkeypatch, choose=None):
    """Open the element menu of `index` with the pointer at (x, y) mm; return {text: enabled}."""
    seen = {}

    def fake_exec(menu, *args):
        seen.update({a.text(): a.isEnabled() for a in menu.actions() if not a.isSeparator()})
        if choose is None:
            return None
        action = next(a for a in menu.actions() if a.text() == choose)
        assert action.isEnabled(), choose
        action.trigger()
        return action

    monkeypatch.setattr(QMenu, 'exec', fake_exec)
    tab._graphic_context_menu(index, _global(tab, x, y))
    return seen


# ── Menu: segment vs vertex ────────────────────────────────────────────────

def test_a_click_on_a_segment_enables_add_only(tab, monkeypatch):
    seen = _menu_at(tab, 0, -27.94, 20.32, monkeypatch)
    assert seen[ADD] is True and seen[REMOVE] is False


def test_a_click_on_a_vertex_enables_remove_only(tab, monkeypatch):
    seen = _menu_at(tab, 0, -25.4, 20.32, monkeypatch)
    assert seen[ADD] is False and seen[REMOVE] is True


def test_a_click_inside_a_polygon_off_its_outline_enables_neither(tab, monkeypatch):
    seen = _menu_at(tab, 1, 22.86, 22.86, monkeypatch)
    assert seen[ADD] is False and seen[REMOVE] is False


def test_other_elements_have_no_vertex_entries(tab, monkeypatch):
    """@brief Negative: a rectangle, a wire and a multi-selection get no vertex entries."""
    tab.start_shape('rectangle')
    tab.add_shape_point(-30.48, -20.32)
    tab.add_shape_point(-25.4, -25.4)
    tab.view.collector.cancel()
    rect = len(tab.graphics()) - 1
    assert tab.graphics()[rect]['primitives'][0]['kind'] == 'rectangle'
    seen = _menu_at(tab, rect, -30.48, -22.86, monkeypatch)
    assert ADD not in seen and REMOVE not in seen
    pin_end = g.pin_inner_ends(tab._symbol)['1']
    tab.start_wire()
    tab.add_wire_point(*pin_end)
    tab.add_wire_point(pin_end[0] + 2.54, pin_end[1])
    tab.add_wire_point(pin_end[0] + 2.54, pin_end[1] + 2.54)
    assert tab.finish_wire(True)
    wire = len(tab.graphics()) - 1
    seen = _menu_at(tab, wire, pin_end[0] + 1.27, pin_end[1], monkeypatch)
    assert ADD not in seen and REMOVE not in seen
    tab._selected_graphics = {0, 1}
    tab._selected_graphic = 1
    monkeypatch.setattr(tab, '_select_graphic', lambda *a, **k: None)
    seen = _menu_at(tab, 0, -27.94, 20.32, monkeypatch)
    assert ADD not in seen and REMOVE not in seen


def test_the_pick_reach_is_the_shared_screen_reach(tab):
    """@brief The UI reach is the shared screen reach; the pick adds half the line's stored stroke width."""
    ui = tab._vertex_pick_reach_mm()
    px_per_mm = abs(tab.view.transform().m11()) * S
    assert ui == pytest.approx(canvas_menus.VERTEX_PICK_PX / px_per_mm)
    assert ui == canvas_menus.pick_reach_mm(tab.view.transform().m11(), S), 'the shared helper'
    width = tab.graphics()[0]['primitives'][0]['width']
    reach = tab._vertex_pick_reach_mm(width)
    assert reach == pytest.approx(ui + width / 2)
    # Just inside the reach picks the vertex, just outside picks the segment.
    assert tab.vertex_pick(0, -25.4 - reach * 0.9, 20.32).kind == ve.VERTEX
    assert tab.vertex_pick(0, -25.4 - reach * 1.5, 20.32).kind == ve.SEGMENT


def _thick_line(tab, width):
    """Restyle the line (element 0) `width` mm wide through the editor's own width edit."""
    assert tab._edit_graphic(0, 'width', width)
    return tab.graphics()[0]['primitives'][0]['width']


def test_a_click_on_the_outer_edge_of_a_thick_line_picks_it_and_beyond_does_not(tab, monkeypatch):
    """@brief Positive: within width/2 + the UI reach of a 2 mm line the vertex entries are on; negative: beyond, none.

    KiCad's own rule (`EDA_SHAPE::hitTest`): reach = accuracy + width / 2.
    """
    assert _thick_line(tab, 2.0) == 2.0
    ui = tab._vertex_pick_reach_mm()
    # Below the middle of the first segment (y = 20.32), on the outer edge of the stroke.
    edge = 20.32 - 1.0 - ui * 0.5
    assert tab.vertex_pick(0, -27.94, edge).kind == ve.SEGMENT
    seen = _menu_at(tab, 0, -27.94, edge, monkeypatch)
    assert seen[ADD] is True and seen[REMOVE] is False
    beyond = 20.32 - 1.0 - ui * 1.5
    assert tab.vertex_pick(0, -27.94, beyond) is None
    seen = _menu_at(tab, 0, -27.94, beyond, monkeypatch)
    assert seen[ADD] is False and seen[REMOVE] is False


# ── Add ────────────────────────────────────────────────────────────────────

def test_add_vertex_from_the_menu_inserts_the_snapped_projection(tab, monkeypatch):
    _menu_at(tab, 0, -27.8, 20.4, monkeypatch, choose=ADD)
    assert _vertices(tab, 0) == [(-30.48, 20.32), (-27.94, 20.32), (-25.4, 20.32), (-25.4, 25.4)]


def test_add_vertex_on_the_polygon_closing_side_keeps_it_closed(tab, monkeypatch):
    _menu_at(tab, 1, 20.32, 22.86, monkeypatch, choose=ADD)
    assert _vertices(tab, 1) == POLYGON + [(20.32, 22.86)]
    stored = tab.graphics()[1]['primitives'][0]['points']
    assert stored[0] == stored[-1] and len(stored) == 6, 'closing repeat kept'


def test_add_vertex_landing_on_an_end_is_refused_with_a_reason(tab):
    before = tab.graphics()
    assert tab.add_vertex(0, 0, -25.6, 20.32) is False, 'the 1.27 grid snaps it onto vertex 1'
    assert tab.graphics() == before
    assert 'coinciderebbe' in tab.status.text()


def test_add_vertex_on_a_turned_polygon_lands_where_clicked(tab):
    """@brief The new vertex is stored in the shape's own frame: a turned polygon shows it at the click."""
    assert tab._edit_graphic(1, 'rotate', 90)
    turned = _vertices(tab, 1)
    a, b = turned[0], turned[1]
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    hit = tab.vertex_pick(1, *mid, reach=0.01)
    assert hit.kind == ve.SEGMENT
    assert tab.add_vertex(1, hit.index, *hit.point)
    assert tab._grid_point(*mid) in _vertices(tab, 1)


def test_add_then_undo_restores_the_drawing_in_one_step(tab):
    before = tab.graphics()
    assert tab.add_vertex(0, 0, -27.94, 20.32)
    assert tab.graphics() != before
    tab.undo()
    assert tab.graphics() == before


# ── Remove ─────────────────────────────────────────────────────────────────

def test_remove_vertex_from_the_menu(tab, monkeypatch):
    _menu_at(tab, 1, 25.4, 25.4, monkeypatch, choose=REMOVE)
    assert _vertices(tab, 1) == [POLYGON[0], POLYGON[1], POLYGON[3]]
    stored = tab.graphics()[1]['primitives'][0]['points']
    assert stored[0] == stored[-1], 'still closed'


def test_removing_the_first_polygon_vertex_moves_the_closing_repeat(tab):
    assert tab.remove_vertex(1, 0)
    stored = [tuple(p) for p in tab.graphics()[1]['primitives'][0]['points']]
    assert len(stored) == 4 and stored[0] == stored[-1]
    assert _vertices(tab, 1) == POLYGON[1:]


def test_a_line_of_two_vertices_refuses_a_removal(tab):
    assert tab.remove_vertex(0, 1)
    before = tab.graphics()
    assert tab.remove_vertex(0, 0) is False
    assert tab.graphics() == before
    assert 'almeno 2' in tab.status.text()


def test_a_triangle_refuses_a_removal(tab):
    assert tab.remove_vertex(1, 3)
    before = tab.graphics()
    assert tab.remove_vertex(1, 0) is False
    assert tab.graphics() == before
    assert 'almeno 3' in tab.status.text()


def test_a_polygon_that_would_lose_its_area_refuses_the_removal(tab):
    """@brief Four vertices, three on one line: removing the fourth leaves no area; removing a middle one is fine."""
    _draw(tab, 'polygon', [(-30.48, -20.32), (-27.94, -20.32), (-25.4, -20.32), (-27.94, -25.4)])
    index = len(tab.graphics()) - 1
    before = tab.graphics()
    assert tab.remove_vertex(index, 3) is False
    assert tab.graphics() == before
    assert 'senza area' in tab.status.text()
    assert tab.remove_vertex(index, 1)


def test_remove_then_undo_restores_the_drawing_in_one_step(tab):
    before = tab.graphics()
    assert tab.remove_vertex(1, 2)
    tab.undo()
    assert tab.graphics() == before
    tab.redo()
    assert len(_vertices(tab, 1)) == 3


def test_vertex_edits_refuse_non_editable_elements(tab):
    tab._text_prompt = lambda *args: ('IN+', 1.27)
    assert tab.start_shape('text')
    tab.add_shape_point(-30.48, -30.48)
    text = len(tab.graphics()) - 1
    before = tab.graphics()
    assert tab.vertex_pick(text, -30.48, -30.48, reach=1.0) is None
    assert tab.remove_vertex(text, 0) is False
    assert tab.add_vertex(text, 0, -30.0, -30.0) is False
    assert tab.graphics() == before


# ── A1: a line ending on its first point is closed (user, 2026-11-09) ──────

CLOSED_LINE = [(-30.48, -20.32), (-25.4, -20.32), (-25.4, -25.4), (-30.48, -25.4)]


def _draw_closed(tab, points):
    """Draw a line through `points` and click its first point again (the tool closes and ends it)."""
    count = len(tab.graphics())
    assert tab.start_shape('line') is not False
    for x, y in list(points) + [points[0]]:
        tab.add_shape_point(x, y)
    tab.select_tool()
    assert len(tab.graphics()) == count + 1
    return count


def _closed_line(tab):
    """Draw a line A, B, C, D and end it on A again; return its element index."""
    index = _draw_closed(tab, CLOSED_LINE)
    record = tab.graphics()[index]
    assert record['key'] == 'shape:line'
    stored = [tuple(p) for p in record['primitives'][0]['points']]
    assert stored[0] == stored[-1] and len(stored) == 5
    return index


def test_a_line_ending_on_its_first_point_is_edited_as_closed(tab):
    """@brief Positive (A1): distinct vertices, closing side pickable, removal keeps the repeat, floor 3."""
    index = _closed_line(tab)
    vertices, closed = g.vertex_edit_shape(tab.graphics()[index])
    assert closed is True and vertices == CLOSED_LINE
    # The closing side D -> A is a segment of its own.
    hit = tab.vertex_pick(index, -30.48, -22.86, reach=0.1)
    assert (hit.kind, hit.index) == (ve.SEGMENT, 3)
    assert tab.remove_vertex(index, 2)
    stored = [tuple(p) for p in tab.graphics()[index]['primitives'][0]['points']]
    assert stored[0] == stored[-1] and len(stored) == 4, 'still closed, the repeat kept'
    before = tab.graphics()
    assert tab.remove_vertex(index, 0) is False, 'a closed line keeps 3 vertices'
    assert tab.graphics() == before
    assert 'almeno 3' in tab.status.text()


def test_a_closed_line_refuses_a_removal_that_leaves_no_area(tab):
    """@brief A1: the polygon-area rule applies to a closed line too."""
    index = _draw_closed(tab, [(-30.48, -20.32), (-27.94, -20.32), (-25.4, -20.32), (-27.94, -25.4)])
    assert g.vertex_edit_shape(tab.graphics()[index])[1] is True
    before = tab.graphics()
    assert tab.remove_vertex(index, 3) is False
    assert tab.graphics() == before
    assert 'senza area' in tab.status.text()


def test_an_open_line_stays_open_and_can_go_down_to_two_points(tab):
    """@brief Negative (A1): A, B, C does not end on A: no closing side, removal down to 2 points."""
    vertices, closed = g.vertex_edit_shape(tab.graphics()[0])
    assert closed is False and vertices == LINE
    assert tab.vertex_pick(0, -27.94, 22.86, reach=0.1) is None, 'no closing side C -> A'
    assert tab.remove_vertex(0, 1)
    assert g.vertex_edit_shape(tab.graphics()[0]) == ([LINE[0], LINE[2]], False)


# ── D1: a right click on a vertex handle opens the menu (real event route) ──

def _context_at(tab, pos, monkeypatch):
    """Right press + context-menu event on the viewport at `pos`; return {text: enabled} or None (no menu)."""
    seen = {}

    def fake_exec(menu, *args):
        seen.update({a.text(): a.isEnabled() for a in menu.actions() if not a.isSeparator()})
        return None

    monkeypatch.setattr(QMenu, 'exec', fake_exec)
    viewport = tab.view.viewport()
    point = QPointF(pos)
    press = QMouseEvent(QEvent.Type.MouseButtonPress, point, QPointF(viewport.mapToGlobal(pos)),
                        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, press)
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, point, QPointF(viewport.mapToGlobal(pos)),
                          Qt.MouseButton.RightButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, release)
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, viewport.mapToGlobal(pos)))
    return seen or None


def _screen(tab, x, y):
    """Viewport position of a symbol point (mm, Y up)."""
    return tab.view.mapFromScene(QPointF(x * S, -y * S))


@pytest.mark.parametrize('zoom', [0.5, 1.0, 3.0])
def test_a_right_click_anywhere_on_a_vertex_handle_opens_its_menu(tab, qapp, monkeypatch, zoom):
    """@brief Parity D1: offsets 0..3 px from a selected line's vertex open the line's menu with «Rimuovi vertice».

    The handle (6 px square) sits over the vertex; before the fix the 1 px hit
    test skipped the handle and only the thin stroke under its centre answered.
    """
    tab.select_tool()   # an armed tool would take the right press (cancel), by design
    tab.view.camera.zoom(zoom)
    vertex = LINE[1]
    tab.view.centerOn(QPointF(vertex[0] * S, -vertex[1] * S))
    tab._select_graphic(0)
    _settle(qapp, 2)
    handles = [item for item in tab.view.scene().items() if (item.data(0) or (None,))[0] == 'shape_point']
    assert handles, 'the selected line shows its vertex handles'
    for dx, dy in [(0, 0), (1, 0), (0, -2), (3, 0), (0, 3), (-3, 0), (2, 2), (-2, 2), (2, -3)]:
        centre = _screen(tab, *vertex)
        seen = _context_at(tab, QPoint(centre.x() + dx, centre.y() + dy), monkeypatch)
        assert seen is not None, (zoom, dx, dy)
        assert seen.get(REMOVE) is True, (zoom, dx, dy, seen)
        assert seen.get(ADD) is False, (zoom, dx, dy)


@pytest.mark.parametrize('zoom', [0.5, 1.0, 3.0])
def test_a_near_miss_of_a_stroke_within_the_margin_opens_its_menu(tab, qapp, monkeypatch, zoom):
    """@brief Positive: 3 px beside a segment (no handle shown) opens the menu; negative: 12 px away opens none.

    The clicks go BELOW the first segment, outside the L of the line: Qt's
    QGraphicsPathItem.shape() of an open path also holds the area its
    implicit closing would enclose (pre-existing, reported, not changed here).
    """
    tab.select_tool()
    tab.view.camera.zoom(zoom)
    middle = (-27.94, 20.32)
    tab.view.centerOn(QPointF(middle[0] * S, -middle[1] * S))
    _settle(qapp, 2)
    centre = _screen(tab, *middle)
    seen = _context_at(tab, QPoint(centre.x(), centre.y() + 3), monkeypatch)
    assert seen is not None and seen.get(ADD) is True, (zoom, seen)
    centre = _screen(tab, *middle)
    assert _context_at(tab, QPoint(centre.x(), centre.y() + 12), monkeypatch) is None


def test_a_pin_under_a_selected_wire_end_handle_keeps_its_own_menu(tab, qapp):
    """@brief Negative of D1: a wire-end handle sitting on a pin end does not steal the pin's right click.

    The exact pass comes first (verification review 2026-11-09): the pin lies
    exactly under the pointer, so it answers even though the selected wire
    shows its end handle there (asserted within the margin, so a handle-first
    order would have returned the wire). The positive side, a handle opening
    its element's menu where nothing lies exactly under the pointer, is
    `test_a_right_click_anywhere_on_a_vertex_handle_opens_its_menu`.
    """
    tab.select_tool()
    pin_end = g.pin_inner_ends(tab._symbol)['1']
    tab.start_wire()
    tab.add_wire_point(*pin_end)
    tab.add_wire_point(pin_end[0] + 2.54, pin_end[1] + 2.54)
    assert tab.finish_wire(True)
    wire = len(tab.graphics()) - 1
    tab.view.camera.zoom(3.0)
    tab.view.centerOn(QPointF(pin_end[0] * S, -pin_end[1] * S))
    tab._select_graphic(wire)
    _settle(qapp, 2)
    handles = [item for item in tab.view.scene().items() if (item.data(0) or (None,))[0] == 'vertex']
    assert handles, 'the selected wire shows its vertex handles'
    # The pointer over the pin end reveals the wire's handles (hover rule).
    tab._update_handle_visibility(QPointF(pin_end[0] * S, -pin_end[1] * S))
    centre = _screen(tab, *pin_end)
    m = canvas_menus.CONTEXT_HIT_MARGIN_PX
    near = tab.view.items(QRect(centre.x() - m, centre.y() - m, 2 * m + 1, 2 * m + 1))
    assert any((item.data(0) or (None,))[:2] == ('vertex', wire) for item in near), 'the handle is within reach'
    assert any((item.data(0) or (None,))[:2] == ('vertex', wire) for item in tab.view.items(centre)), 'and under it'
    target = tab.view._context_target(centre)
    assert target is not None and target[0] == 'pin', target
