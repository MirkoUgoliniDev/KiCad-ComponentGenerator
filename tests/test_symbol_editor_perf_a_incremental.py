"""@brief The symbol editor redraws only what changed, and the picture is the full redraw's.

Contracts (call counts and identical outputs, never wall-clock times):

- a redraw rebuilds only the pins whose picture changes (moved, renamed,
  selected, restyled): one drag step to a new node, one name edit or one
  nudge rebuilds one pin; a theme change rebuilds them all (negative twin);
- the scene after any number of incremental redraws is item for item, in
  stacking order, the scene a full redraw of the same draft gives, with the
  same pin text areas;
- an accepted geometry edit lays the symbol out once (the layout validated by
  `_accept_geometry` is reused by `_changed` only for equal inputs);
- auto-fit refits only when the camera changed;
- `copy_rows` copies like `deepcopy`.
"""

from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor

import kicadgen.gui.symbol_tab as symbol_tab_module
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab, copy_rows

#: Scene units of one 2.54 mm pin-grid step.
STEP = 2.54 * SCENE_UNITS_PER_MM


def _tab(count=16):
    """A symbol tab holding `count` pins named P1, P2, ... on the four sides."""
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}', 'symbol_side': ('left', 'right', 'top', 'bottom')[i % 4]}
                           for i in range(1, count + 1)], pad_ids=[str(i) for i in range(1, count + 1)])
    return tab


def _count_pin_builds(tab, monkeypatch):
    """Wrap `_draw_pin`; return the list of pin numbers rebuilt."""
    built = []
    original = tab._draw_pin

    def counting(scene, placed, pin_length, selected):
        built.append(placed.pin.number)
        return original(scene, placed, pin_length, selected)
    monkeypatch.setattr(tab, '_draw_pin', counting)
    return built


def _scene_signature(tab):
    """Every item of the scene in stacking order: type, target, rounded scene rect, text, pen/brush colours."""
    result = []
    for item in tab.view.scene().items(Qt.SortOrder.AscendingOrder):
        rect = item.sceneBoundingRect()
        pen = item.pen().color().name(QColor.NameFormat.HexArgb) if hasattr(item, 'pen') else None
        brush = item.brush().color().name(QColor.NameFormat.HexArgb) if hasattr(item, 'brush') else None
        result.append((type(item).__name__, item.data(0), item.isVisible(),
                       tuple(round(v, 3) for v in (rect.x(), rect.y(), rect.width(), rect.height())),
                       item.text() if hasattr(item, 'text') else None, pen, brush, item.toolTip()))
    # Contentless pin roots are bookkeeping, not picture.
    return [entry for entry in result if not (entry[0] == 'QGraphicsPathItem' and entry[1] is None
                                              and entry[3][2:] == (0, 0))]


def _full_redraw_signature(tab):
    """The signature of a redraw from scratch of the same draft (caches dropped)."""
    tab._clear_scene(tab.view.scene())
    tab._draw(tab._symbol)
    return _scene_signature(tab), list(tab._text_bounds)


def _pin_start(tab, number):
    placed = next(p for p in tab._symbol.pins if p.pin.number == number)
    return QPointF(placed.x * SCENE_UNITS_PER_MM, -placed.y * SCENE_UNITS_PER_MM)


def test_a_drag_step_to_a_new_node_rebuilds_only_the_dragged_pin(qapp, monkeypatch):
    """@brief Positive: each step to another node rebuilds one pin, not all of them."""
    tab = _tab(40)
    try:
        start = _pin_start(tab, '1')
        tab._begin_gesture(('pin', '1'), start)
        tab._move_gesture(('pin', '1'), start + QPointF(-2 * STEP, 0))
        built = _count_pin_builds(tab, monkeypatch)
        tab._move_gesture(('pin', '1'), start + QPointF(-3 * STEP, 0))
        tab._move_gesture(('pin', '1'), start + QPointF(-4 * STEP, 0))
        assert built == ['1', '1']
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_a_name_edit_rebuilds_one_pin_and_a_theme_change_all(qapp, monkeypatch):
    """@brief A name edit rebuilds its pin; negative: a style change reaches every pin."""
    tab = _tab(12)
    try:
        built = _count_pin_builds(tab, monkeypatch)
        # Same length: the auto-sized body keeps its width, so no other pin moves.
        tab.table.item(4, 1).setText('Q5')
        assert built == ['5']
        built.clear()
        # A longer name widens the body: the pins it moves are rebuilt too.
        tab.table.item(4, 1).setText('A_MUCH_LONGER_NAME')
        moved = len(built)
        assert '5' in built and moved > 1
        built.clear()
        tab.set_kicad_colors(tab._theme_key != 'kicad')
        assert sorted(built, key=int) == [str(i) for i in range(1, 13)]
    finally:
        tab.deleteLater()


def test_a_selection_change_rebuilds_the_two_pins_concerned(qapp, monkeypatch):
    """@brief Negative: the selection is part of a pin's picture."""
    tab = _tab(12)
    try:
        tab._select_pins({'2'}, '2')
        built = _count_pin_builds(tab, monkeypatch)
        tab._select_pins({'3'}, '3')
        assert sorted(built) == ['2', '3']
    finally:
        tab.deleteLater()


def test_the_incremental_scene_is_the_full_redraw_scene(qapp):
    """@brief Identical output: after drags, edits, a swap and a refusal, the scene is a full redraw's."""
    tab = _tab(20)
    try:
        start = _pin_start(tab, '1')
        tab._begin_gesture(('pin', '1'), start)
        for step in range(1, 6):
            tab._move_gesture(('pin', '1'), start + QPointF(-step * STEP, step * STEP))
        tab._end_gesture(True)
        tab.table.item(2, 1).setText('A_LONGER_NAME')
        tab._select_pins({'5', '6'}, '5')
        # A drop on an occupied terminal swaps the two pins.
        other = next(p for p in tab._symbol.pins if p.pin.number == '5')
        tab.place_pin('9', other.x, other.y)
        tab.rotate_pin('7', 90)
        incremental = (_scene_signature(tab), list(tab._text_bounds))
        assert incremental == _full_redraw_signature(tab)
    finally:
        tab.deleteLater()


def test_an_accepted_edit_lays_the_symbol_out_once(qapp, monkeypatch):
    """@brief The layout validated by `_accept_geometry` serves `_changed`; a name edit still lays out."""
    tab = _tab(12)
    try:
        calls = []
        original = symbol_tab_module.layout_symbol
        monkeypatch.setattr(symbol_tab_module, 'layout_symbol',
                            lambda *a, **k: calls.append(1) or original(*a, **k))
        tab.rotate_pin('1', 90)
        assert len(calls) == 1
        calls.clear()
        tab.table.item(1, 1).setText('OTHER')
        assert len(calls) == 1, 'an edit outside _accept_geometry lays out in _changed'
        assert any(p.pin.name == 'OTHER' for p in tab._symbol.pins)
    finally:
        tab.deleteLater()


def test_stale_accepted_layout_is_not_used_for_other_inputs(qapp):
    """@brief Negative: a handed-over layout whose inputs differ from the draft is ignored."""
    tab = _tab(8)
    try:
        wrong = tab._symbol
        blocked = tab.table.blockSignals(True)
        tab.table.item(0, 1).setText('NEW')
        tab.table.blockSignals(blocked)
        pins, body = symbol_tab_module.symbol_inputs(tab._draft)
        tab._accepted_layout = (pins, body, wrong)
        tab._changed()
        assert any(p.pin.name == 'NEW' for p in tab._symbol.pins)
    finally:
        tab.deleteLater()


def test_auto_fit_refits_only_when_the_camera_changed(qapp, monkeypatch):
    """@brief A redraw with the same bounds keeps the fit; a resize refits (negative twin)."""
    tab = _tab(8)
    tab.show()
    qapp.processEvents()
    try:
        assert tab.view.auto_fit
        tab.view.fit()
        fits = []
        original = tab.view.fitInView
        monkeypatch.setattr(tab.view, 'fitInView', lambda *a: fits.append(1) or original(*a))
        tab.table.item(0, 1).setText('X')
        tab._draw(tab._symbol)
        assert fits == []
        tab.resize(900, 700)
        qapp.processEvents()
        assert fits, 'a resized viewport is fitted again'
        fits.clear()
        tab.view.scene().setSceneRect(tab.view.scene().sceneRect().adjusted(-50, -50, 50, 50))
        tab.view.fit()
        assert fits == [1]
    finally:
        tab.close()
        tab.deleteLater()


def test_copy_rows_equals_deepcopy_and_shares_nothing():
    """@brief `copy_rows` gives `deepcopy`'s value with no shared container; other types fall back."""
    graphics = [{'id': 'e1', 'x': 1.0, 'primitives': [{'kind': 'polyline', 'points': [[0, 0], [1, 1]]}]}]
    rows = [{'pin': '1', 'name': 'A', 'symbol_position': [1.0, 2.0], 'flag': True, 'none': None,
             'pair': (1, [2]), 'symbol_graphics': deepcopy(graphics), 'other': {1, 2}}]
    copied = copy_rows(rows)
    assert copied == deepcopy(rows)
    copied[0]['symbol_graphics'][0]['primitives'][0]['points'][0][0] = 9
    copied[0]['pair'][1].append(3)
    copied[0]['other'].add(3)
    assert rows[0]['symbol_graphics'] == graphics
    assert rows[0]['pair'] == (1, [2]) and rows[0]['other'] == {1, 2}


def test_the_scene_comparison_sees_a_stale_pin(qapp, monkeypatch):
    """@brief Negative: with a key that ignored the style, the stale pins would differ from a full redraw.

    Proves the signature of `test_the_incremental_scene_is_the_full_redraw_scene`
    is sensitive to what the key must cover.
    """
    tab = _tab(8)
    try:
        monkeypatch.setattr(tab, '_pin_style', lambda: 'constant')
        tab._draw(tab._symbol)  # the pins now carry the constant key
        tab.set_kicad_colors(tab._theme_key != 'kicad')
        assert (_scene_signature(tab), list(tab._text_bounds)) != _full_redraw_signature(tab)
    finally:
        tab.deleteLater()
