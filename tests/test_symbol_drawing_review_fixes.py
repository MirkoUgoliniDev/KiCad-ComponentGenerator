"""@brief Symbol editor fixes of the 2026-11-08 drawing review (wave F2, plan docs/footprint-drawing-kicad-plan.md).

Each fix is pinned with a positive and a negative case (CLAUDE.md «Correzioni»):

- M2: Shift pressed / released without a mouse move re-runs the hover, so the
  ghost leaves the snapped point and comes back, and a click equals the ghost;
- M3: the angle constraint is the shared `constrain_to_grid`;
- M4: the object snap is the shared `object_snap.snap_point` (the last
  collected point is never a candidate), the reach is the shared constant;
- M5: `tool_snap_kind` lets a kind change on the same point update the marker;
- M11: «(vari)» and the fill / style drop-downs follow a language switch;
- polygon: `polygon_has_area` (bow tie kept, collinear refused);
- S4: the wire, the ruler and a glyph placing show no grid button, not Select;
- S5: the remembered snap kind is reset per snap and on disarm;
- S7: stroke, fill and style are read over every primitive;
- P3/P4: Ctrl+Shift+M toggles the ruler, whose steps are shown;
- P7: E with several pins opens the first pin's dialog;
- contract 13: an object within reach beats the angle mode, a point outside
  reach is constrained;
- parity gaps: a drag does not refresh the panel, E through its shortcut,
  hovering does not rebuild the snap index.
"""

import math
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from kicadgen.core import object_snap
from kicadgen.core.draw_constraints import constrain_to_grid
from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui.canvas_tool_hints import step_hint
from kicadgen.gui.symbol_tab import OBJECT_SNAP_REACH_PX, SCENE_UNITS_PER_MM, SymbolTab
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
from kicadgen.services.messages import set_translator, tx

S = SCENE_UNITS_PER_MM
LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ROWS5 = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)]


def _settle(qapp, times=10):
    """@brief Process pending events (layout, fit, deferred redraws)."""
    for _ in range(times):
        qapp.processEvents()


@pytest.fixture
def tab(qapp):
    """@brief A shown five-pin symbol tab with the finest drawing grid, no recovery file."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    tab.show()
    _settle(qapp)
    # The finest grid: a pointer a few pixels off a hot point rounds to another node.
    tab.set_drawing_grid(0.254)
    _settle(qapp)
    yield tab
    tab.hide()
    tab.deleteLater()
    _settle(qapp, 2)


def _vp(tab, x_mm, y_mm):
    """@brief Viewport point of a symbol point (mm, Y up)."""
    return tab.view.mapFromScene(QPointF(x_mm * S, -y_mm * S))


def _mm(tab, pos):
    """@brief Symbol point (mm, Y up) of a viewport point."""
    scene = tab.view.mapToScene(pos)
    return scene.x() / S, -scene.y() / S


def _grid_at(tab, pos):
    """@brief The grid node a free click at a viewport point lands on."""
    return tab._grid_point(*_mm(tab, pos))


def _click_at(tab, qapp, pos, modifier=NO_MOD):
    """@brief One real left click at a viewport position."""
    QTest.mouseClick(tab.view.viewport(), LEFT, modifier, pos)
    qapp.processEvents()


def _move_at(tab, qapp, pos):
    """@brief One real mouse move (no button) to a viewport position."""
    QTest.mouseMove(tab.view.viewport(), pos)
    qapp.processEvents()


def _pin_end(tab, index=0):
    """@brief The connection point of a pin, mm, rounded as the snap stores it."""
    pin = tab._symbol.pins[index]
    return (round(pin.x, 4), round(pin.y, 4))


def _shortcut(tab, sequence):
    """@brief The tab view's QShortcut for a key sequence."""
    wanted = QKeySequence(sequence)
    return next(s for s in tab.view.findChildren(QShortcut) if s.key() == wanted)


def _free_point(tab, near):
    """@brief A grid point near `near` with no hot point of the drawing within reach (precondition helper)."""
    reach = tab._object_snap_reach_mm()
    point = tab._grid_point(*near)
    assert tab._snap_index().nearest(point[0], point[1], reach * 3) is None, 'precondition: empty area'
    return point


# ── M2: Shift press / release without a mouse move ───────────────────────────

def test_shift_press_and_release_move_the_ghost_without_a_mouse_move(tab, qapp):
    """@brief Near a pin end: Shift frees the ghost onto the grid, its release puts it back; clicks equal the ghost."""
    end = _pin_end(tab)
    tab.start_shape('line')
    pos = _vp(tab, *end) + QPoint(8, 0)
    free_node = _grid_at(tab, pos)
    assert free_node != end, 'precondition: the free grid node is not the pin end'
    _move_at(tab, qapp, pos)
    assert tab._collector.preview == [end] and tab.tool_snap_kind() == 'pad'
    QTest.keyPress(tab.view, Qt.Key.Key_Shift, SHIFT)
    qapp.processEvents()
    assert tab._collector.preview == [free_node], 'Shift held: the free grid point'
    assert tab.snap_marker() is None and tab.tool_snap_kind() is None
    _click_at(tab, qapp, pos, SHIFT)
    assert tab._shape_points == [free_node], 'the Shift click equals the ghost'
    QTest.keyRelease(tab.view, Qt.Key.Key_Shift, NO_MOD)
    qapp.processEvents()
    assert tab._collector.preview == [end], 'Shift released: back on the pin end'
    assert tab.snap_marker() == ('pad', end[0], end[1])
    _click_at(tab, qapp, pos)
    assert tab._shape_points == [free_node, end], 'the click equals the ghost'


# ── M3 and contract 13: angle constraint and object snap ─────────────────────

def test_the_angle_constraint_is_the_shared_rule(tab, monkeypatch):
    """@brief The symbol's constrained point is `constrain_to_grid` with the drawing grid, or 0.01 mm without snap."""
    anchor, pointer = (0.1, 0.2), (3.0, 2.1)
    expected = constrain_to_grid(anchor, pointer, '45', tab.drawing_grid(), ndigits=4)
    assert tab._constrained_point(anchor, *pointer, '45') == tuple(expected)
    dx, dy = expected[0] - anchor[0], expected[1] - anchor[1]
    assert math.isclose(dx, dy, abs_tol=1e-4), 'on the diagonal even from an off-grid anchor'
    monkeypatch.setattr(tab, '_snap', False)
    fine = tab._constrained_point(anchor, *pointer, '45')
    assert fine == tuple(constrain_to_grid(anchor, pointer, '45', 0.01, ndigits=4))
    assert fine != tuple(expected), 'without the grid snap the step is 0.01 mm, not the drawing grid'


def test_an_object_in_reach_beats_the_angle_mode_and_a_point_out_of_reach_is_constrained(tab, qapp):
    """@brief Contract 13: angle 90 + object snap; a pin end off the ray is caught, an empty point is constrained."""
    tab.set_object_snap(True)
    tab.set_angle_mode('90')
    end = _pin_end(tab)
    anchor = (round(end[0] - 5.08, 4), round(end[1] - 2.54, 4))
    tab.start_shape('line')
    assert tab.add_shape_point(*anchor)
    _click_at(tab, qapp, _vp(tab, *end) + QPoint(4, 3))
    assert tab._shape_points[-1] == end, 'the pin end within reach wins, off the 90 degree ray'
    tab.finish_shape(False)
    tab.start_shape('line')
    assert tab.add_shape_point(*anchor)
    pos = _vp(tab, anchor[0] - 4.0, anchor[1] + 1.0)
    raw = _mm(tab, pos)
    assert tab._snap_index().nearest(raw[0], raw[1], tab._object_snap_reach_mm()) is None, 'precondition'
    _click_at(tab, qapp, pos)
    x, y = tab._shape_points[-1]
    assert y == anchor[1] and x < anchor[0], 'out of reach: constrained to the horizontal ray'


# ── M4: one classifier, one reach ────────────────────────────────────────────

def test_the_reach_is_the_shared_constant():
    """@brief The tab's reach is an alias of `object_snap.SNAP_REACH_PX`, not its own number."""
    assert OBJECT_SNAP_REACH_PX is object_snap.SNAP_REACH_PX or OBJECT_SNAP_REACH_PX == object_snap.SNAP_REACH_PX
    assert not hasattr(SymbolTab, '_snap_rank'), 'the private ranking was replaced by snap_point'


def test_the_last_collected_point_is_not_a_snap_candidate(tab, qapp):
    """@brief After one click the point itself does not attract; after a second click it does (as an endpoint)."""
    tab.set_object_snap(True)
    a = _free_point(tab, (2.54, 1.27))
    tab.start_shape('line')
    assert tab.add_shape_point(*a)
    pos = _vp(tab, *a) + QPoint(8, 0)
    assert _grid_at(tab, pos) != a, 'precondition: the free node is another node'
    _move_at(tab, qapp, pos)
    assert tab.tool_snap_kind() is None and tab._collector.preview == [_grid_at(tab, pos)], \
        'the anchor of the segment is not a candidate'
    assert tab.add_shape_point(a[0], round(a[1] + 2.54, 4))
    _move_at(tab, qapp, pos + QPoint(1, 0))
    assert tab._collector.preview == [a] and tab.tool_snap_kind() == 'endpoint', 'an earlier point is'


# ── M5 and S5: the snapped kind ──────────────────────────────────────────────

def test_a_kind_change_on_the_same_point_updates_the_marker(tab, qapp):
    """@brief Snap off on a pin end (on the grid): no marker; snap on, same snapped point: the pad marker appears."""
    end = _pin_end(tab)
    tab.set_object_snap(False)
    tab.start_shape('line')
    pos = _vp(tab, *end)
    _move_at(tab, qapp, pos)
    assert tab._collector.preview == [end], 'precondition: the pin end is a grid node'
    assert tab.snap_marker() is None and tab.tool_snap_kind() is None
    tab.set_object_snap(True)
    _move_at(tab, qapp, pos + QPoint(1, 0))
    assert tab._collector.preview == [end], 'the same point'
    assert tab.tool_snap_kind() == 'pad' and tab.snap_marker() == ('pad', end[0], end[1]), \
        'the kind changed: the marker follows'
    calls = []
    original = tab.tool_preview_changed
    tab.tool_preview_changed = lambda *args: (calls.append(args), original(*args))
    _move_at(tab, qapp, pos + QPoint(2, 0))
    assert calls == [], 'same point and same kind: no re-emission'


def test_the_snapped_kind_is_reset_by_another_tool_and_by_a_disarm(tab, qapp):
    """@brief A pad snap is remembered while hovering; a disarm and a measure snap report no kind."""
    end = _pin_end(tab)
    tab.start_shape('line')
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(3, 0))
    assert tab.tool_snap_kind() == 'pad'
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert tab.tool_snap_kind() is None and tab._last_snap is None, 'disarm resets it'
    tab.start_shape('line')
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(2, 0))
    assert tab.tool_snap_kind() == 'pad'
    # The same tool, an invalid draft (no layout): the snap adds nothing and reports no kind.
    spec, symbol = tab._collector.spec, tab._symbol
    tab._symbol = None
    try:
        assert tab.tool_snap_ex(spec, QPointF(end[0] * S, -end[1] * S), False, (), 'free') == []
        assert tab.tool_snap_kind() is None, 'reset at the top of every snap'
    finally:
        tab._symbol = symbol
    assert tab.toggle_measure()
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(3, 0))
    assert tab.tool_snap_kind() is None and tab.snap_marker() is None, 'the ruler draws its own markers'
    tab.toggle_measure()
    tab.start_wire()
    _move_at(tab, qapp, _vp(tab, *end) + QPoint(3, 0))
    assert tab.tool_snap_kind() is None and tab.snap_marker() is None, 'the wire shows no typed marker'
    tab.finish_wire(False)


# ── M11: language switch ─────────────────────────────────────────────────────

def _english(key, default=None):
    """Catalogue lookup of the English text, as `I18n.raw` with English active."""
    return DEFAULT_TRANSLATIONS.get(key, {}).get('en', default)


def _two_mixed_rectangles(tab):
    """Two rectangles with different stroke widths and fills, both selected."""
    tab.set_object_snap(False)
    tab.start_shape('rectangle')
    for p in ((10.16, 0.0), (12.7, 2.54), (15.24, 0.0), (17.78, 2.54)):
        tab.add_shape_point(*p)
    tab.select_tool()
    tab._edit_graphic(1, 'width', 0.5)
    tab._select_graphic(0)
    tab._select_graphic(1, additive=True)


@pytest.fixture
def translated_tab(qapp):
    """@brief A five-pin tab translating through `messages.tx`, which follows the registered catalogue."""
    tab = SymbolTab(tx=tx)
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    tab.show()
    _settle(qapp)
    yield tab
    set_translator(None)
    tab.hide()
    tab.deleteLater()
    _settle(qapp, 2)


def test_mixed_placeholder_and_choices_follow_a_language_switch(translated_tab, qapp):
    """@brief Italian «(vari)» and «Contorno»; after switching to English and retranslating, «(various)» and «Outline»."""
    tab = translated_tab
    _two_mixed_rectangles(tab)
    panel = tab.properties_panel
    width = panel.field_widget('width')
    assert width.is_mixed() and width.lineEdit().placeholderText() == '(vari)'
    fill = panel.field_widget('fill')
    assert fill.itemText(fill.findData('outline')) == 'Contorno'
    set_translator(_english)
    try:
        tab.retranslate()
        qapp.processEvents()
        width = panel.field_widget('width')
        assert width.is_mixed() and width.lineEdit().placeholderText() == '(various)'
        fill = panel.field_widget('fill')
        style = panel.field_widget('style')
        assert fill.itemText(fill.findData('outline')) == 'Outline'
        assert style.itemText(style.findData('dash')) == 'Dashed'
    finally:
        set_translator(None)


# ── polygon: shared area rule ────────────────────────────────────────────────

def test_a_bow_tie_polygon_is_kept_and_collinear_vertices_are_refused():
    """@brief Positive: a bow tie (signed area zero) has area; negative: collinear sets, rounded ones included."""
    bow_tie = g.shape_instance('polygon', [(0, 0), (2.54, 2.54), (2.54, 0), (0, 2.54)])
    assert bow_tie is not None and len(bow_tie['primitives'][0]['points']) == 5
    assert g.shape_instance('polygon', [(0, 0), (1.27, 0), (2.54, 0)]) is None
    assert g.shape_instance('polygon', [(0, 0), (1, 0.3333), (3, 1)]) is None, 'collinear up to the 0.0001 rounding'
    assert g.shape_instance('polygon', [(0, 0), (1, 0.34), (3, 1)]) is not None, 'a real offset is an area'


# ── S4: tools outside the grid ───────────────────────────────────────────────

def test_tools_outside_the_grid_do_not_show_select(tab, qapp):
    """@brief Wire, ruler and glyph placing: no grid button checked; Select again once they end; a shape is shown."""
    grid = tab.palette.tool_grid
    assert grid.button('').isChecked() and not grid.outside_active()
    tab.toggle_measure()
    assert grid.outside_active() and not grid.button('').isChecked(), 'the ruler'
    tab.toggle_measure()
    assert not grid.outside_active() and grid.button('').isChecked()
    tab.palette_actions()[0].trigger()
    assert tab.view.placing and grid.outside_active() and not grid.button('').isChecked(), 'a glyph placing'
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not tab.view.placing and grid.button('').isChecked() and not grid.outside_active()
    tab.start_wire()
    assert grid.outside_active()
    tab.start_shape('circle')
    assert not grid.outside_active() and grid.active_key() == 'circle', 'a grid tool is shown, not the outside state'
    tab.retranslate()
    assert tab.palette.tool_grid.active_key() == 'circle'
    tab.start_wire()
    tab.retranslate()
    assert tab.palette.tool_grid.outside_active(), 'a language change keeps the outside state'
    tab.finish_wire(False)


# ── S7: style over every primitive ───────────────────────────────────────────

def test_style_fields_read_every_primitive(tab):
    """@brief A record whose second primitive differs is mixed; identical primitives show their value."""
    same = {'primitives': [{'kind': 'polyline', 'width': 0.254, 'fill': 'none', 'style': 'default'},
                           {'kind': 'circle', 'width': 0.254, 'fill': 'none', 'style': 'default'}]}
    specs = {spec.key: spec for spec in tab._style_fields([same])}
    assert not specs['width'].mixed and specs['width'].value == 0.254
    assert not specs['fill'].mixed and specs['fill'].value == 'none'
    differ = deepcopy(same)
    differ['primitives'][1].update(width=0.5, fill='background', style='dash')
    specs = {spec.key: spec for spec in tab._style_fields([differ])}
    assert specs['width'].mixed and specs['fill'].mixed and specs['style'].mixed, \
        'a commit restyles every primitive, so a hidden difference is shown as «(vari)»'
    # The commit side writes every primitive (`set_primitive_style`): the read matches it.
    assert g.set_primitive_style(differ, width=0.3)
    assert {p['width'] for p in differ['primitives']} == {0.3}


# ── P3/P4: the ruler's shortcut and steps ────────────────────────────────────

def test_ctrl_shift_m_toggles_the_ruler_and_its_steps_are_shown(tab, qapp):
    """@brief Ctrl+Shift+M arms and disarms the ruler; arm and each point print the next step."""
    shortcut = _shortcut(tab, 'Ctrl+Shift+M')
    shortcut.activated.emit()
    assert tab._measure.armed and tab.measure_button.defaultAction().isChecked()
    assert tab.status.text() == tx(step_hint('measure', 0))
    _click_at(tab, qapp, _vp(tab, 10.16, 5.08))
    assert tab.status.text() == tx(step_hint('measure', 1)) != tx(step_hint('measure', 0))
    shortcut.activated.emit()
    assert not tab._measure.armed and not tab.measure_button.defaultAction().isChecked()
    assert tab.measure_button.toolTip().endswith('(Ctrl+Shift+M)')


# ── P7 and E through its shortcut ────────────────────────────────────────────

def test_e_with_several_pins_opens_the_first_pins_dialog(tab, qapp, monkeypatch):
    """@brief Decision P7: E with pins 4 and 2 selected opens the first one in the symbol's order; one pin opens its own."""
    opened = []
    monkeypatch.setattr(tab, '_edit_selected_pin', lambda: opened.append(tab._selected_pin))
    tab._select_pins({'4', '2'})
    first = next(p.pin.number for p in tab._symbol.pins if p.pin.number in {'4', '2'})
    _shortcut(tab, 'E').activated.emit()
    assert opened == [first]
    tab._select_pins({'5'})
    _shortcut(tab, 'E').activated.emit()
    assert opened == [first, '5'], 'one selected pin: its dialog'
    tab.clear_selection()
    _move_at(tab, qapp, _vp(tab, -30.0, -30.0))
    _shortcut(tab, 'E').activated.emit()
    assert opened == [first, '5'], 'nothing selected nor hovered: no dialog'


def test_e_with_a_selected_element_focuses_the_panel(tab, qapp, monkeypatch):
    """@brief E with an element selected focuses the panel's first field and opens no dialog."""
    dialogs = []
    monkeypatch.setattr(tab, '_edit_element_properties', dialogs.append)
    tab.set_object_snap(False)
    tab.start_shape('rectangle')
    tab.add_shape_point(10.16, 0.0)
    tab.add_shape_point(12.7, 2.54)
    tab.select_tool()
    tab._select_graphic(0)
    tab.activateWindow()
    tab.view.setFocus()
    _settle(qapp)
    _shortcut(tab, 'E').activated.emit()
    qapp.processEvents()
    first = tab.properties_panel.field_widget(tab.properties_panel.field_keys()[0])
    assert dialogs == [], 'a selection is edited in the panel, not in a dialog'
    assert QApplication.focusWidget() in (first, first.lineEdit()), 'the first field took the focus'


# ── Parity gaps: drag, hover and the snap index ──────────────────────────────

def test_a_drag_does_not_refresh_the_panel_and_the_drop_does(tab, qapp, monkeypatch):
    """@brief While a glyph is dragged the panel is not rebuilt; the drop rebuilds it with the new position."""
    assert tab.place_glyph('relay_coil', 0, 0)
    _settle(qapp)
    items = [item for item in tab.view.scene().items() if item.data(0) == ('graphic', 0)]
    start = tab.view.mapFromScene(items[0].sceneBoundingRect().center())
    built = []
    original = tab.properties_panel.set_fields
    monkeypatch.setattr(tab.properties_panel, 'set_fields', lambda *a, **k: (built.append(a), original(*a, **k)))
    QTest.mousePress(tab.view.viewport(), LEFT, NO_MOD, start)
    qapp.processEvents()
    assert tab.view.gesture == ('graphic', 0)
    built.clear()
    step = _vp(tab, 2.54, 0) - _vp(tab, 0, 0)
    for k in (1, 2, 3):
        QTest.mouseMove(tab.view.viewport(), start + step * k)
        qapp.processEvents()
    assert built == [], 'no panel rebuild during the drag'
    QTest.mouseRelease(tab.view.viewport(), LEFT, NO_MOD, start + step * 3)
    _settle(qapp)
    assert built, 'the drop refreshes the panel'
    assert tab.properties_panel.shown_value('x') == pytest.approx(tab.graphics()[0]['x'])


def test_hovering_does_not_rebuild_the_snap_index(tab, qapp, monkeypatch):
    """@brief Mouse moves with a tool armed reuse the index; a committed edit rebuilds it."""
    tab.start_shape('line')
    index = tab._snap_index()
    calls = []
    original = tab._snap_candidates
    monkeypatch.setattr(tab, '_snap_candidates', lambda: (calls.append(1), original())[1])
    end = _pin_end(tab)
    for dx in range(0, 30, 5):
        _move_at(tab, qapp, _vp(tab, *end) + QPoint(dx, 2))
    assert calls == [] and tab._snap_index() is index, 'hovering never rebuilds'
    tab.finish_shape(False)
    tab.set_object_snap(False)
    tab.start_shape('rectangle')
    tab.add_shape_point(10.16, 0.0)
    tab.add_shape_point(12.7, 2.54)
    assert tab._snap_index() is not index and calls == [1], 'a commit rebuilds once'
