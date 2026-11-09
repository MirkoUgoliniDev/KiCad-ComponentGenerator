"""@brief Symbol editor: unfilled elements hit on their outline (H1) and one closing handle (H2), real mouse events.

User decisions of 2026-11-09 («Sì, come KiCad», «Sì, una maniglia sola»),
twin of tests/test_footprint_outline_hit.py. (H1) an unfilled rectangle,
polygon, circle and an open line take a press only on their stroke (within
the shared press margin), as KiCad's `EDA_SHAPE::hitTest`; a filled one
also inside; a pin inside an empty outline keeps its press. (H2) a polygon
(stored with its first point repeated) and a line that ends on its first
point show one handle at the closing point, and dragging it moves both ends.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtTest import QTest

from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S, SymbolTab

ROWS = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 5)]
#: Far from the pins and the body (body centred on the origin), on the 1.27 mm grid.
RECT = [(20.32, 20.32), (30.48, 27.94)]
POLYGON = [(20.32, 20.32), (30.48, 20.32), (30.48, 27.94), (20.32, 27.94)]
CIRCLE = [(25.4, 24.13), (30.48, 24.13)]
INSIDE = (25.4, 24.13)
#: A point on the left side of every shape above (the circle's left rim).
OUTLINE = (20.32, 24.13)


def _settle(qapp, times=6):
    for _ in range(times):
        qapp.processEvents()


def _draw(tab, key, points):
    """Draw one shape through the click-tool API, then release the (persistent) tool."""
    count = len(tab.graphics())
    assert tab.start_shape(key) is not False
    for x, y in points:
        tab.add_shape_point(x, y)
    if len(tab.graphics()) == count:
        assert tab.finish_wire(True), key
    tab._router.disarm()
    assert len(tab.graphics()) == count + 1, key


@pytest.fixture
def tab(qapp):
    """@brief A shown symbol tab with four pins on the 1.27 mm drawing grid."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS), pad_ids=[r['pin'] for r in ROWS])
    tab.show()
    _settle(qapp)
    tab.set_drawing_grid(1.27)
    yield tab
    tab.hide()
    tab.deleteLater()
    _settle(qapp, 2)


def _vp(tab, x, y):
    return tab.view.mapFromScene(QPointF(x * S, -y * S))


def _click(tab, qapp, x, y):
    pos = _vp(tab, x, y)
    QTest.mouseMove(tab.view.viewport(), pos)
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    _settle(qapp, 2)


def _shape(tab, key, points, fill=None):
    _draw(tab, key, points)
    index = len(tab.graphics()) - 1
    if fill is not None:
        assert tab._edit_graphic(index, 'fill', fill)
    return index


def _deselect(tab, qapp):
    """Click far from everything: the background clears the selection."""
    _click(tab, qapp, -40.64, -40.64)
    assert tab.selected_graphics() == []


SHAPES = {'rectangle': RECT, 'polygon': POLYGON, 'circle': CIRCLE}


# ── H1 ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('key', sorted(SHAPES))
def test_a_click_inside_an_unfilled_shape_is_the_background_and_its_outline_selects_it(tab, qapp, key):
    index = _shape(tab, key, SHAPES[key])
    assert g.is_shape(tab.graphics()[index])
    _deselect(tab, qapp)
    _click(tab, qapp, *INSIDE)
    assert tab.selected_graphics() == [], 'negative: inside an empty outline is the background'
    _click(tab, qapp, *OUTLINE)
    assert tab.selected_graphics() == [index], 'positive: the outline selects the element'


@pytest.mark.parametrize('key', sorted(SHAPES))
def test_a_filled_shape_is_selected_inside(tab, qapp, key):
    index = _shape(tab, key, SHAPES[key], fill='background')
    _deselect(tab, qapp)
    _click(tab, qapp, *INSIDE)
    assert tab.selected_graphics() == [index]


def test_a_pin_inside_an_unfilled_rectangle_keeps_its_press(tab, qapp):
    """The rectangle drawn around a pin: the press on the pin selects the pin, not the rectangle."""
    pin = next(item for item in tab.view.scene().items()
               if item.data(0) and item.data(0)[0] == 'pin' and item.data(0)[1] == '1')
    centre = pin.sceneBoundingRect().center()
    cx, cy = centre.x() / S, -centre.y() / S
    _shape(tab, 'rectangle', [(round(cx - 6.35, 2), round(cy - 6.35, 2)), (round(cx + 6.35, 2), round(cy + 6.35, 2))])
    _deselect(tab, qapp)
    pos = tab.view.mapFromScene(centre)
    assert tab.view.press_target(pos) == ('pin', '1')
    QTest.mouseMove(tab.view.viewport(), pos)
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    _settle(qapp, 2)
    assert tab._selected_pin == '1' and tab.selected_graphics() == []


def test_the_right_click_and_the_cursor_follow_the_outline(tab, qapp):
    index = _shape(tab, 'rectangle', RECT)
    inside, outline = _vp(tab, *INSIDE), _vp(tab, *OUTLINE)
    assert tab.view._context_target(inside) is None, 'negative: no element menu inside'
    assert tab.view._context_target(outline) == ('graphic', index)
    QTest.mouseMove(tab.view.viewport(), inside)
    _settle(qapp, 2)
    assert tab.view.viewport().cursor().shape() != Qt.CursorShape.SizeAllCursor, \
        'negative: no move cursor inside an empty outline'
    assert not [item for item in tab.view.items(inside) if item.data(0) == ('graphic', index)], \
        'the Qt shape (cursor, tooltip) holds no inside'


def test_an_open_l_line_is_not_hit_inside_its_bend(tab, qapp):
    """An «L»: Qt's default path shape closes it by its chord; KiCad's hit is the stroke."""
    index = _shape(tab, 'line', [(20.32, 20.32), (30.48, 20.32), (30.48, 30.48)])
    bend = (27.94, 22.86)  # inside the triangle closed by the chord, 2.54 mm from both legs
    _deselect(tab, qapp)
    assert tab.view.press_target(_vp(tab, *bend)) is None
    _click(tab, qapp, *bend)
    assert tab.selected_graphics() == []
    _click(tab, qapp, 25.4, 20.32)
    assert tab.selected_graphics() == [index], 'positive: the leg selects the line'


def test_a_press_near_a_thin_stroke_still_grabs_it(tab, qapp):
    """The press margin (KiCad's accuracy term) on a stroke a pixel wide; beyond it, the background."""
    from kicadgen.gui import canvas_menus
    index = _shape(tab, 'rectangle', RECT)
    edge = _vp(tab, *OUTLINE)
    near = edge + type(edge)(canvas_menus.CONTEXT_HIT_MARGIN_PX - 1, 0)
    far = edge + type(edge)(canvas_menus.CONTEXT_HIT_MARGIN_PX + 4, 0)
    assert tab.view.press_target(near) == ('graphic', index)
    assert tab.view.press_target(far) is None


# ── H2 ─────────────────────────────────────────────────────────────────

CLOSED_LINE = [(20.32, 20.32), (30.48, 20.32), (30.48, 27.94), (20.32, 20.32)]
OPEN_LINE = [(20.32, 20.32), (30.48, 20.32), (30.48, 27.94), (20.32, 27.94)]


def _handle_targets(tab, index):
    tab._select_graphic(index)
    return sorted(item.data(0) for item in tab.view.scene().items()
                  if item.data(0) and item.data(0)[0] == 'shape_point' and item.data(0)[1] == index)


@pytest.mark.parametrize('key, points', [('polygon', POLYGON), ('line', CLOSED_LINE)])
def test_a_closed_outline_shows_one_handle_at_its_closing_point(tab, qapp, key, points):
    index = _shape(tab, key, points)
    stored = tab.graphics()[index]['primitives'][0]['points']
    assert stored[0] == stored[-1], 'stored with the closing repeat'
    targets = _handle_targets(tab, index)
    assert len(targets) == len(stored) - 1, 'one handle per corner, none for the repeat'


def test_an_open_line_keeps_a_handle_per_point(tab, qapp):
    index = _shape(tab, 'line', OPEN_LINE)
    assert len(_handle_targets(tab, index)) == len(OPEN_LINE)


def _drag_handle(tab, qapp, start, end):
    a, b = _vp(tab, *start), _vp(tab, *end)
    QTest.mouseMove(tab.view.viewport(), a)
    _settle(qapp, 2)
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
    QTest.mouseMove(tab.view.viewport(), (a + b) / 2)
    QTest.mouseMove(tab.view.viewport(), b)
    QTest.mouseRelease(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
    _settle(qapp, 2)


@pytest.mark.parametrize('key, points', [('polygon', POLYGON), ('line', CLOSED_LINE)])
def test_dragging_the_closing_handle_moves_both_ends_as_one_undo_step(tab, qapp, key, points):
    index = _shape(tab, key, points)
    tab._select_graphic(index)
    before = deepcopy(tab.graphics())
    history = len(tab._history)
    _drag_handle(tab, qapp, points[0], (17.78, 17.78))
    after = g.shape_handles(tab.graphics()[index])
    stored = tab.graphics()[index]['primitives'][0]['points']
    assert stored[0] == stored[-1], 'the outline stays closed'
    assert after[0] == pytest.approx((17.78, 17.78)), after
    assert len(after) == len(stored) - 1, 'still one handle at the closing point'
    assert len(tab._history) == history + 1, 'one drag, one undo step'
    tab.undo()
    assert tab.graphics() == before


def test_dragging_an_end_of_an_open_line_moves_that_end_alone(tab, qapp):
    index = _shape(tab, 'line', OPEN_LINE)
    tab._select_graphic(index)
    _drag_handle(tab, qapp, OPEN_LINE[0], (17.78, 17.78))
    handles = g.shape_handles(tab.graphics()[index])
    assert handles[0] == pytest.approx((17.78, 17.78))
    assert handles[-1] == pytest.approx(OPEN_LINE[-1]), 'negative: the other end stays'


# ── Glyph exception (coordinator decision, 2026-11-09) ─────────────────

def _glyph_point(tab, index, kind):
    """A point inside the unfilled `kind` primitive of a placed glyph, far from its other strokes."""
    prims = [p for p in g.placed_primitives(tab.graphics()[index]) if p.kind == kind]
    assert prims and all(p.fill == 'none' for p in prims), 'the glyph body is UNFILLED'
    p = prims[0]
    if kind == 'rectangle':
        (x0, y0), (x1, y1) = p.points
        return ((x0 + x1) / 2, (y0 + y1) / 2), p
    (cx, cy), = p.points
    # Below the centre, inside the rim, away from the «M» and the leads.
    return (cx - 0.3 * p.radius, cy - 0.6 * p.radius), p


@pytest.mark.parametrize('key, kind', [('resistor', 'rectangle'), ('motor_dc', 'circle')])
def test_a_library_glyph_is_selected_and_dragged_by_a_press_inside_its_unfilled_body(tab, qapp, key, kind):
    """Positive: a placed glyph keeps the hit it always had, its body inside too (KiCad selects a
    symbol by its body); negative twin: a user-drawn unfilled rectangle around the same point
    lets the press through. The split is structural (a glyph record vs a `shape:*` record), not
    a size or a name."""
    from kicadgen.gui import canvas_menus
    from kicadgen.gui.canvas_hit import items_near
    tab.place_glyph(key, 25.4, 25.4)
    index = len(tab.graphics()) - 1
    assert not g.is_shape(tab.graphics()[index]) and not g.is_wire(tab.graphics()[index])
    point, _prim = _glyph_point(tab, index, kind)
    _settle(qapp, 2)
    pos = _vp(tab, *point)
    strokes = [item for item in items_near(tab.view, pos, canvas_menus.CONTEXT_HIT_MARGIN_PX)
               if item.data(0) == ('graphic', index) and item.contains(item.mapFromScene(tab.view.mapToScene(pos)))]
    assert strokes, 'the body interior is under the press'
    _deselect(tab, qapp)
    assert tab.view.press_target(pos) == ('graphic', index)
    _click(tab, qapp, *point)
    assert tab.selected_graphics() == [index], 'positive: a press inside the body selects the glyph'
    before = (tab.graphics()[index]['x'], tab.graphics()[index]['y'])
    _drag_handle(tab, qapp, point, (point[0] + 2.54, point[1]))
    after = (tab.graphics()[index]['x'], tab.graphics()[index]['y'])
    assert after[0] == pytest.approx(before[0] + 2.54) and after[1] == pytest.approx(before[1]), (before, after)
    # Negative twin: a drawn unfilled rectangle elsewhere, clicked at its centre.
    shape = _shape(tab, 'rectangle', [(-40.64, 20.32), (-30.48, 27.94)])
    _deselect(tab, qapp)
    assert tab.view.press_target(_vp(tab, -35.56, 24.13)) is None
    _click(tab, qapp, -35.56, 24.13)
    assert tab.selected_graphics() == [], 'negative: a drawn empty outline is not hit inside'
    assert g.is_shape(tab.graphics()[shape])


# ── Open shapes: arc and Bézier ────────────────────────────────────────

OPEN_SHAPES = {'arc': [(20.32, 20.32), (25.4, 25.4), (30.48, 20.32)],
               'bezier': [(20.32, 20.32), (20.32, 30.48), (30.48, 30.48), (30.48, 20.32)]}


@pytest.mark.parametrize('key', sorted(OPEN_SHAPES))
def test_an_open_arc_or_curve_is_not_hit_across_its_chord(tab, qapp, key):
    """Negative: the area closed by the chord of an arc or a Bézier is not the element; positive: its stroke is."""
    index = _shape(tab, key, OPEN_SHAPES[key])
    outline = g.primitive_outline(g.placed_primitives(tab.graphics()[index])[0])
    first, last, middle = outline[0], outline[-1], outline[len(outline) // 2]
    chord_mid = ((first[0] + last[0]) / 2, (first[1] + last[1]) / 2)
    chord_side = ((chord_mid[0] + middle[0]) / 2, (chord_mid[1] + middle[1]) / 2)
    _deselect(tab, qapp)
    assert tab.view.press_target(_vp(tab, *chord_side)) is None
    assert not [item for item in tab.view.items(_vp(tab, *chord_side)) if item.data(0) == ('graphic', index)]
    _click(tab, qapp, *chord_side)
    assert tab.selected_graphics() == []
    assert tab.view.press_target(_vp(tab, *middle)) == ('graphic', index)
    _click(tab, qapp, *middle)
    assert tab.selected_graphics() == [index], 'positive: the stroke selects it'


# ── Right click, Ctrl+click and double click inside ────────────────────

def _context(tab, pos, monkeypatch):
    """A right press, its release and the context-menu event at `pos`: the view's menu signals heard."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QContextMenuEvent, QMouseEvent
    from PyQt6.QtWidgets import QApplication, QMenu
    monkeypatch.setattr(QMenu, 'exec', lambda *a, **k: None)
    heard = []
    view, viewport = tab.view, tab.view.viewport()
    slots = [(view.pin_context_requested, lambda key, _p: heard.append(('pin', key))),
             (view.graphic_context_requested, lambda index, _p: heard.append(('graphic', index))),
             (view.property_context_requested, lambda key, _p: heard.append(('property', key)))]
    for signal, slot in slots:
        signal.connect(slot)
    try:
        point, gpos = QPointF(pos), QPointF(viewport.mapToGlobal(pos))
        QApplication.sendEvent(viewport, QMouseEvent(QEvent.Type.MouseButtonPress, point, gpos, Qt.MouseButton.RightButton,
                                                     Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier))
        QApplication.sendEvent(viewport, QMouseEvent(QEvent.Type.MouseButtonRelease, point, gpos, Qt.MouseButton.RightButton,
                                                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, viewport.mapToGlobal(pos))
        QApplication.sendEvent(viewport, event)
    finally:
        for signal, slot in slots:
            signal.disconnect(slot)
    return heard


def test_a_right_click_inside_an_unfilled_rectangle_is_the_background(tab, qapp, monkeypatch):
    index = _shape(tab, 'rectangle', RECT)
    assert _context(tab, _vp(tab, *INSIDE), monkeypatch) == [], 'negative: no element menu inside'
    assert _context(tab, _vp(tab, *OUTLINE), monkeypatch) == [('graphic', index)], 'the outline keeps its menu'


def test_a_ctrl_click_inside_an_unfilled_rectangle_toggles_nothing(tab, qapp):
    index = _shape(tab, 'rectangle', RECT)
    _deselect(tab, qapp)
    for point, expected in ((INSIDE, []), (OUTLINE, [index])):
        pos = _vp(tab, *point)
        QTest.mouseMove(tab.view.viewport(), pos)
        QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, pos)
        _settle(qapp, 2)
        assert tab.selected_graphics() == expected, point


def _double_click(tab, pos):
    """Press, release, double click, release at `pos`: what Qt delivers for a fast second click."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
    viewport = tab.view.viewport()
    point, gpos = QPointF(pos), QPointF(viewport.mapToGlobal(pos))
    left, none, plain = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier
    for kind, buttons in ((QEvent.Type.MouseButtonPress, left), (QEvent.Type.MouseButtonRelease, none),
                          (QEvent.Type.MouseButtonDblClick, left), (QEvent.Type.MouseButtonRelease, none)):
        QApplication.sendEvent(viewport, QMouseEvent(kind, point, gpos, left, buttons, plain))


def test_a_double_click_inside_an_unfilled_rectangle_selects_nothing(tab, qapp):
    index = _shape(tab, 'rectangle', RECT)
    for point, expected in ((OUTLINE, [index]), (INSIDE, [])):
        _deselect(tab, qapp)
        pos = _vp(tab, *point)
        QTest.mouseMove(tab.view.viewport(), pos)
        _double_click(tab, pos)
        _settle(qapp, 2)
        assert tab.selected_graphics() == expected, point


# ── Hover feedback follows the press reach (coordinator decision) ──────

def test_the_hover_cursor_and_tooltip_follow_the_press_reach(tab, qapp):
    """3 px off a thin stroke the press takes it, so the move cursor and the tooltip show there;
    beyond the margin the arrow comes back and no tooltip is offered (real mouse moves)."""
    from PyQt6.QtCore import QPoint
    from kicadgen.gui import canvas_menus
    index = _shape(tab, 'rectangle', RECT)
    _deselect(tab, qapp)
    viewport = tab.view.viewport()
    edge = _vp(tab, *OUTLINE)
    near = edge - QPoint(canvas_menus.CONTEXT_HIT_MARGIN_PX - 1, 0)
    far = edge - QPoint(canvas_menus.CONTEXT_HIT_MARGIN_PX + 4, 0)
    hint = next(item.toolTip() for item in tab.view.scene().items() if item.data(0) == ('graphic', index))
    assert hint
    assert not [item for item in tab.view.items(near) if item.data(0) == ('graphic', index)], 'not exactly under'
    for _ in range(2):
        QTest.mouseMove(viewport, near)
        _settle(qapp, 2)
        assert tab.view.press_target(near) == ('graphic', index)
        assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, 'the press reach shows the move cursor'
        assert tab.view.reach_tooltip(near) == hint
        QTest.mouseMove(viewport, edge)
        _settle(qapp, 2)
        assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, 'exactly on the stroke'
        QTest.mouseMove(viewport, far)
        _settle(qapp, 2)
        assert tab.view.press_target(far) is None
        assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor, 'negative: beyond the margin, the arrow'
        assert tab.view.reach_tooltip(far) == '', 'negative: no tooltip beyond the margin'


def test_an_armed_tool_shows_the_cross_over_an_element_stroke(tab, qapp):
    """Plan C10 in both tabs: with a click tool armed the stroke under the pointer keeps the
    cross (the symbol runs the base mouse move, which used to show the element's move cursor);
    a middle-button pan still shows the closed hand."""
    _shape(tab, 'rectangle', RECT)
    viewport = tab.view.viewport()
    edge = _vp(tab, *OUTLINE)
    assert tab.start_shape('line') is not False
    QTest.mouseMove(viewport, edge - type(edge)(20, 0))
    QTest.mouseMove(viewport, edge)
    _settle(qapp, 2)
    assert viewport.cursor().shape() == Qt.CursorShape.CrossCursor
    QTest.mousePress(viewport, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, edge)
    QTest.mouseMove(viewport, edge + type(edge)(5, 5))
    _settle(qapp, 2)
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor, 'negative: the pan keeps its hand'
    QTest.mouseRelease(viewport, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, edge + type(edge)(5, 5))
    _settle(qapp, 2)
    assert viewport.cursor().shape() == Qt.CursorShape.CrossCursor, 'the pan over, the cross is back'
    tab._router.disarm()


# ── One precedence for both buttons (coordinator decision) ─────────────

def _pin_geometry(tab, number='1'):
    """(outer end, unit vector pointing away from the body) of a pin, symbol mm."""
    placed = next(p for p in tab._symbol.pins if p.pin.number == number)
    inner = g.pin_inner_ends(tab._symbol)[number]
    dx, dy = placed.x - inner[0], placed.y - inner[1]
    length = (dx * dx + dy * dy) ** 0.5
    return (placed.x, placed.y), (dx / length, dy / length)


def _zoom_to(tab, qapp, px_per_mm, centre):
    from PyQt6.QtGui import QTransform
    tab.view.auto_fit = False
    tab.view.setTransform(QTransform.fromScale(px_per_mm / S, px_per_mm / S))
    tab.view.centerOn(QPointF(centre[0] * S, -centre[1] * S))
    _settle(qapp, 2)


def _kinds_at(view, pos, number, index):
    from kicadgen.gui import canvas_menus
    from kicadgen.gui.canvas_hit import items_near
    exact = {item.data(0) for item in view.items(pos) if item.data(0)}
    near = {item.data(0) for item in items_near(view, pos, canvas_menus.CONTEXT_HIT_MARGIN_PX) if item.data(0)}
    return (('pin', number) in exact, ('pin', number) in near, ('graphic', index) in exact, ('graphic', index) in near)


def _axis_layout(tab, number, index, tip, stroke):
    """Along the screen axis from a pin's outer end to a stroke: (pixels, last pin pixel, first stroke pixel).

    The pin's own hit (its wide transparent pen, the end dot) and the stroke's
    are MEASURED with the view's exact query, never assumed."""
    dx, dy = stroke.x() - tip.x(), stroke.y() - tip.y()
    sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
    assert (sx == 0) != (sy == 0), 'the pins lie on a screen axis'
    pixels = [type(tip)(tip.x() + i * sx, tip.y() + i * sy) for i in range(abs(dx) + abs(dy) + 6)]
    exact = [{item.data(0) for item in tab.view.items(pos)} for pos in pixels]
    on_pin = [i for i, found in enumerate(exact) if ('pin', number) in found]
    on_stroke = [i for i, found in enumerate(exact) if ('graphic', index) in found]
    if not on_pin:
        return pixels, None, None
    after = [i for i in on_stroke if i > max(on_pin)]
    return pixels, max(on_pin), (min(after) if after else None)


def _pin_and_crossing_line(tab, qapp):
    """Pin 1 and a user line across its axis 1.27 mm beyond its outer end: (outer end, line centre, line index)."""
    (ox, oy), (ux, uy) = _pin_geometry(tab)
    cx, cy = ox + 1.27 * ux, oy + 1.27 * uy
    index = _shape(tab, 'line', [(round(cx - 2.54 * uy, 2), round(cy + 2.54 * ux, 2)),
                                 (round(cx + 2.54 * uy, 2), round(cy - 2.54 * ux, 2))])
    _deselect(tab, qapp)
    return (ox, oy), (cx, cy), index


def _zoom_for_gap(tab, qapp, ends, index, gap):
    """Zoom until the pixels between the pin's last exact pixel and the stroke's first number `gap`."""
    (ox, oy), (cx, cy) = ends
    for tenth in range(20, 200):
        _zoom_to(tab, qapp, tenth / 10, (ox, oy))
        pixels, pin_end, stroke_start = _axis_layout(tab, '1', index, _vp(tab, ox, oy), _vp(tab, cx, cy))
        if pin_end is not None and stroke_start is not None and stroke_start - pin_end == gap:
            return pixels, pin_end, stroke_start
    raise AssertionError(f'no zoom gives a gap of {gap} px')


def test_a_pin_two_pixels_away_beats_a_stroke_three_pixels_away_for_both_buttons(tab, qapp, monkeypatch):
    """Inside the margin pass the rank decides (pins above strokes), for the left press and the
    right click alike; twin of the footprint's pad test."""
    outer, centre, index = _pin_and_crossing_line(tab, qapp)
    pixels, pin_end, _stroke_start = _zoom_for_gap(tab, qapp, (outer, centre), index, 5)
    pos = pixels[pin_end + 2]              # 2 px past the pin's hit, 3 px before the stroke's
    assert _kinds_at(tab.view, pos, '1', index) == (False, True, False, True), 'both within the margin only'
    assert tab.view.press_target(pos) == ('pin', '1')
    assert tab.view._context_target(pos) == ('pin', '1')
    assert _context(tab, pos, monkeypatch) == [('pin', '1')], 'the right click opens the pin menu'
    QTest.mouseMove(tab.view.viewport(), pos)
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    _settle(qapp, 2)
    assert tab._selected_pin == '1' and tab.selected_graphics() == [], 'the left press takes the pin'


def test_a_stroke_exactly_under_the_pointer_beats_a_pin_within_the_margin(tab, qapp, monkeypatch):
    """Negative twin: the exact pass comes before the margin pass, whatever the rank."""
    outer, centre, index = _pin_and_crossing_line(tab, qapp)
    pixels, _pin_end, stroke_start = _zoom_for_gap(tab, qapp, (outer, centre), index, 3)
    pos = pixels[stroke_start]             # exactly on the stroke, 3 px from the pin's hit
    assert _kinds_at(tab.view, pos, '1', index) == (False, True, True, True), 'stroke exact, pin within the margin'
    assert tab.view.press_target(pos) == ('graphic', index)
    assert _context(tab, pos, monkeypatch) == [('graphic', index)]


# ── Texts above pins (coordinator decision after verification D2) ──────

def _value_over_pin(tab, qapp):
    """Move the Value text onto pin 1: (pixel exactly under both, pixel on the pin away from the text).

    The anchor is tried on the grid points around the pin until the text's
    glyphs and the pin's hit share a pixel (measured with the view's exact
    query, never assumed), and a pin pixel lies beyond the margin of the text.
    """
    from itertools import product
    from kicadgen.gui import canvas_menus
    from kicadgen.gui.canvas_hit import items_near
    (ox, oy), (ux, uy) = _pin_geometry(tab)
    _deselect(tab, qapp)
    for i, j in product(range(-2, 3), repeat=2):
        assert tab.move_property('Value', round(ox + 1.27 * i, 2), round(oy + 1.27 * j, 2))
        _settle(qapp, 2)
        text = tab._property_items['Value'][0]
        area = tab.view.mapFromScene(text.sceneBoundingRect()).boundingRect()
        both = None
        for x, y in product(range(area.left(), area.right() + 1), range(area.top(), area.bottom() + 1)):
            found = {item.data(0) for item in tab.view.items(type(area.topLeft())(x, y))}
            if ('pin', '1') in found and ('property', 'Value') in found:
                both = type(area.topLeft())(x, y)
                break
        if both is None:
            continue
        for k in range(1, 60):
            # Walk along the pin from its outer end inwards: a pixel of the pin alone.
            pos = _vp(tab, ox - 0.1 * k * ux, oy - 0.1 * k * uy)
            near = {item.data(0) for item in items_near(tab.view, pos, canvas_menus.CONTEXT_HIT_MARGIN_PX)}
            exact = {item.data(0) for item in tab.view.items(pos)}
            if ('pin', '1') in exact and ('property', 'Value') not in near:
                return both, pos
    raise AssertionError('no grid anchor lays the Value text over pin 1')


def test_a_text_over_a_pin_takes_the_press_the_right_click_and_the_hover(tab, qapp, monkeypatch):
    """Precedence handles > texts > pins > strokes (coordinator decision 2026-11-09, verification
    D2): on a pixel where the Value's glyphs lie over pin 1 the left press drags the TEXT, the
    right click opens the text's menu and the hover shows the text's tooltip. CONTRACT CHANGED:
    the pin used to win. Twin of the footprint's text over a pad."""
    both, pin_only = _value_over_pin(tab, qapp)
    view = tab.view
    assert view.press_target(both) == ('property', 'Value')
    assert view._context_target(both) == ('property', 'Value')
    assert _context(tab, both, monkeypatch) == [('property', 'Value')]
    assert view.reach_tooltip(both) == tab._property_items['Value'][0].toolTip()
    QTest.mouseMove(view.viewport(), both)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, both)
    assert view.gesture == ('property', 'Value'), 'the press drags the text'
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, both)
    _settle(qapp, 2)
    # Negative: the same pin where no text lies over it keeps every button.
    assert view.press_target(pin_only) == ('pin', '1')
    assert _context(tab, pin_only, monkeypatch) == [('pin', '1')]


def test_a_pin_without_a_text_over_it_still_beats_a_stroke_exactly_under_the_pointer(tab, qapp, monkeypatch):
    """Negative twin of the text rule: the pin keeps its rank over a drawn stroke crossing it."""
    (ox, oy), (ux, uy) = _pin_geometry(tab)
    mx, my = ox - 1.27 * ux, oy - 1.27 * uy            # on the pin, inside its length
    index = _shape(tab, 'line', [(round(mx - 2.54 * uy, 2), round(my + 2.54 * ux, 2)),
                                 (round(mx + 2.54 * uy, 2), round(my - 2.54 * ux, 2))])
    _deselect(tab, qapp)
    pos = _vp(tab, mx, my)
    exact = {item.data(0) for item in tab.view.items(pos)}
    assert ('pin', '1') in exact and ('graphic', index) in exact, exact
    assert tab.view.press_target(pos) == ('pin', '1')
    assert _context(tab, pos, monkeypatch) == [('pin', '1')]


# ── The hover cursor is undone when an interaction begins (D3) ─────────

def test_the_hover_move_cursor_is_undone_when_a_glyph_placement_starts(tab, qapp):
    """Verification 2026-11-09, D3: the reach feedback runs only in an idle view, so the move
    cursor set 3 px off a stroke stayed through the whole glyph placement. Starting the
    placement (through `cancel_interaction`) undoes it; negative: without a placement the
    idle hover keeps it."""
    from PyQt6.QtCore import QPoint
    from kicadgen.gui import canvas_menus
    _shape(tab, 'rectangle', RECT)
    _deselect(tab, qapp)
    viewport = tab.view.viewport()
    near = _vp(tab, *OUTLINE) - QPoint(canvas_menus.CONTEXT_HIT_MARGIN_PX - 1, 0)
    QTest.mouseMove(viewport, near)
    _settle(qapp, 2)
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, 'negative: idle, the reach cursor shows'
    tab.glyph_combo.setCurrentIndex(max(0, tab.glyph_combo.findData('resistor')))
    tab._start_placing()
    assert tab.view.placing
    assert viewport.cursor().shape() != Qt.CursorShape.SizeAllCursor, 'the placement does not keep the move cursor'
    # Along the side, at the same distance: within the press margin, not exactly on the stroke.
    QTest.mouseMove(viewport, near + QPoint(0, 1))
    _settle(qapp, 2)
    assert viewport.cursor().shape() != Qt.CursorShape.SizeAllCursor, 'nor after a move during the placement'
    tab.view.cancel_interaction()
    assert not tab.view.placing


# ── The pointer target follows the camera (review 2026-11-09, points 1, 4, 5) ─

def _send_move(view, pos):
    """Deliver a button-less mouse move at viewport point `pos` to the symbol viewport."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)


def _pointer_on_graphic(tab, qapp, index):
    """Move the pointer onto a viewport pixel where a plain press takes element `index` (scanned, not assumed)."""
    from PyQt6.QtCore import QPoint
    view = tab.view
    items = [item for item in view.scene().items() if item.data(0) == ('graphic', index)]
    assert items
    box = items[0].sceneBoundingRect()
    for item in items[1:]:
        box = box.united(item.sceneBoundingRect())
    area = view.mapFromScene(box).boundingRect().intersected(view.viewport().rect())
    for y in range(area.top(), area.bottom() + 1):
        for x in range(area.left(), area.right() + 1):
            pos = QPoint(x, y)
            if view.press_target(pos) != ('graphic', index):
                continue
            for point in (pos + QPoint(0, 40), pos):
                _send_move(view, point)
            _settle(qapp, 2)
            if tab._hovered_graphic() == index:
                return pos
    raise AssertionError(f'no pixel where a press takes element {index}')


def _revealed(tab, index):
    """The shape-point handles of element `index` that are shown."""
    return [item for item in tab.view.scene().items()
            if item.data(0) and item.data(0)[0] == 'shape_point' and item.data(0)[1] == index and item.isVisible()]


def test_e_after_the_auto_fit_takes_what_is_under_the_cursor_now(tab, qapp, monkeypatch):
    """Twin of the footprint's second Delete after the auto-fit. The symbol read E's target from the
    SCENE point of the last move, mapped back to the viewport: after an auto-fit without a mouse move
    E opened the element under the OLD scene point, which the refit had carried away from the cursor.
    Positive: on the stroke E opens it; a far element drawn later refits the camera (no move), the
    rectangle leaves the cursor, and E answers what a press at the physical pointer takes."""
    index = _shape(tab, 'rectangle', RECT)
    _deselect(tab, qapp)
    tab.view.auto_fit = True
    tab._draw(tab._symbol)
    _settle(qapp)
    opened = []
    monkeypatch.setattr(tab, '_edit_element_properties', opened.append)
    pos = _pointer_on_graphic(tab, qapp, index)
    assert tab.edit_properties() and opened == [index], 'on the stroke, E opens it'
    opened.clear()
    scale = tab.view.transform().m11()
    _shape(tab, 'rectangle', [(-101.6, -101.6), (-96.52, -96.52)])
    tab.clear_selection()    # no click: the pointer must not move
    tab._draw(tab._symbol)
    _settle(qapp)
    assert tab.view.transform().m11() != scale, 'the auto-fit changed the camera without a move'
    physical = tab.view.press_target(pos)
    assert physical != ('graphic', index), 'the refit carried the rectangle away from the cursor'
    expected = physical[1] if physical and physical[0] in ('graphic', 'vertex', 'shape_point') else None
    assert tab._hovered_graphic() == expected
    assert tab.edit_properties() == (expected is not None) and opened == ([] if expected is None else [expected])


def test_a_wheel_zoom_under_the_pointer_keeps_the_element_under_it(tab, qapp):
    """Negative of the camera-follow rule: a wheel zoom anchored under the mouse keeps the same scene
    point under the same viewport point, so the element under the pointer stays E's target."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    index = _shape(tab, 'rectangle', RECT)
    tab._select_graphic(index)    # the handles show for the selected element
    pos = _pointer_on_graphic(tab, qapp, index)
    scale = tab.view.transform().m11()
    event = QWheelEvent(QPointF(pos), QPointF(tab.view.viewport().mapToGlobal(pos)), QPoint(0, 0), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                        False)
    QApplication.sendEvent(tab.view.viewport(), event)
    _settle(qapp, 2)
    assert tab.view.transform().m11() > scale, 'the wheel zoomed in'
    assert tab._hovered_graphic() == index
    assert _revealed(tab, index), 'its handles stay revealed'


def test_readout_and_hover_handles_follow_a_camera_change_without_a_move(tab, qapp):
    """Points 4 and 5: a zoom anchored away from the pointer moves the scene under a still cursor.
    The readout's point is the one under the cursor NOW (it kept the old scene point), and the
    handles revealed for the element the pointer has left are hidden (they survived and, being the
    first rank of the press pick, could take a press)."""
    from PyQt6.QtCore import QPoint
    index = _shape(tab, 'rectangle', RECT)
    tab._select_graphic(index)    # the handles show for the selected element
    pos = _pointer_on_graphic(tab, qapp, index)
    assert _revealed(tab, index), 'precondition: the hovered element shows its handles'
    before = QPointF(tab._cursor_position)
    assert tab.view.camera.zoom(4.0, QPoint(0, 0)), 'the camera changed'
    _settle(qapp, 2)
    now = tab.view.mapToScene(pos)
    assert tab._cursor_position != before
    assert (tab._cursor_position.x(), tab._cursor_position.y()) == pytest.approx((now.x(), now.y()))
    assert tab.view.press_target(pos) != ('graphic', index), 'the element left the cursor'
    assert _revealed(tab, index) == [], 'its handles went away with it'
    assert tab._hovered_graphic() is None or tab._hovered_graphic() != index
