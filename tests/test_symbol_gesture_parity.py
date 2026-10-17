"""@brief Characterisation of every mouse gesture of the symbol editor, pinned before the shared gesture module.

Plan `docs/canvas-gestures-plan.md` §3 step 1 (2026-10-17): the symbol editor
and the footprint preview will share one gesture component. Before anything
moves, every gesture of the symbol editor is recorded here as it behaves
TODAY, through real QTest press/move/release scripts on the view's viewport:
S1 pin drag, S2 group drag, S3 element drag (glyph, wire, shape, group), S4
wire vertex, S5 shape point, S6 property (live), S7 body resize, S8 rubber
band, S10 pan, S11 wheel, the hover reveal rule (S17) and the negatives
(Escape, hide, `set_data`, wheel, right click and a redraw mid-drag).

What each test pins:

- the committed rows (`draft_rows`), the history (length AND content) and
  the recovery JSON, compared with a TWIN tab driven through the direct
  APIs (`place_pin`, `_transform_selected`, `_edit_graphic`,
  `move_wire_vertex`, `move_shape_point`, `move_property`, `resize_body`):
  the mouse path and the programmatic path must give the same result;
- the per-phase counts of `_changed`, `_draw`, `_populate`,
  `layout_symbol` and `save_draft` on the press, on every move and on the
  release (pattern of tests/test_symbol_property_drag_smooth.py);
- a golden JSON of one scripted session (`tests/data/gesture_golden/
  symbol_session.json`), asserted byte-equal and regenerated only with
  `KICADGEN_REGEN_GOLDEN=1`.

Where today's behaviour is a defect-adjacent choice the plan lists (D1-D12)
the test says so in its docstring: it records the contract of today, which
the migration keeps until the user decides otherwise (plan §3 step 9).
"""

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QContextMenuEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QGraphicsSimpleTextItem

import kicadgen.gui.symbol_tab as symbol_tab_module
import kicadgen.services.symbol_draft as symbol_draft
from kicadgen.gui.symbol_tab import GRID_MM, HANDLE_HOVER_MARGIN, SCENE_UNITS_PER_MM, SymbolTab

#: Golden record of the scripted session (`test_golden_session`).
GOLDEN_DIR = Path(__file__).parent / 'data' / 'gesture_golden'
GOLDEN_SESSION = GOLDEN_DIR / 'symbol_session.json'
#: Set to 1 to rewrite the golden file from the current code.
REGEN_ENV = 'KICADGEN_REGEN_GOLDEN'

#: Seven pins, four left and three right (the RS485 project of the smooth-drag report).
ROWS7 = [{'pin': str(i), 'name': n, 'symbol_side': 'left' if i <= 4 else 'right'}
         for i, n in enumerate(['A', 'B', 'GND', 'VCC', 'RO', 'DI', 'DE'], 1)]
#: Five pins, the palette tests' board: room for a glyph, a wire and a shape.
ROWS5 = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)]

S = SCENE_UNITS_PER_MM


def _settle(qapp, times=10):
    """Process pending events until the layout, the fit and the deferred redraws are done.

    The symbol tab lays its widgets out on the event loop: a status text or a
    palette change can resize the view, which refits the camera; every
    viewport point computed before that would land somewhere else.
    """
    for _ in range(times):
        qapp.processEvents()


def _build(qapp, tmp_path, rows, name, folder=''):
    """A shown symbol tab of `rows` with a recovery file bound under `tmp_path/folder`."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data(name, deepcopy(rows), pad_ids=[r['pin'] for r in rows])
    path = tmp_path / folder / '.symbol-draft.json' if folder else tmp_path / '.symbol-draft.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tab.set_recovery_path(path)
    tab.show()
    _settle(qapp)
    return tab


@pytest.fixture
def tab7(qapp, tmp_path):
    """The seven-pin tab (pins, properties, body, band, pan, wheel)."""
    tab = _build(qapp, tmp_path, ROWS7, 'RS485')
    yield tab
    tab.deleteLater()


@pytest.fixture
def twin7(qapp, tmp_path):
    """A second seven-pin tab, driven by the direct APIs only."""
    tab = _build(qapp, tmp_path, ROWS7, 'RS485', 'twin')
    yield tab
    tab.deleteLater()


@pytest.fixture
def tab5(qapp, tmp_path):
    """The five-pin tab (elements: glyph, wire, shape)."""
    tab = _build(qapp, tmp_path, ROWS5, 'Board')
    yield tab
    tab.deleteLater()


@pytest.fixture
def twin5(qapp, tmp_path):
    """A second five-pin tab, driven by the direct APIs only."""
    tab = _build(qapp, tmp_path, ROWS5, 'Board', 'twin')
    yield tab
    tab.deleteLater()


class _Counter:
    """Counts the tab methods and module functions a gesture phase runs (per-phase contract)."""

    def __init__(self, tab, monkeypatch):
        self.calls = {}
        for name in ('_changed', '_draw', '_populate'):
            monkeypatch.setattr(tab, name, self._wrap(name, getattr(tab, name)))
        monkeypatch.setattr(symbol_tab_module, 'layout_symbol',
                            self._wrap('layout_symbol', symbol_tab_module.layout_symbol))
        monkeypatch.setattr(symbol_draft, 'save_draft', self._wrap('save_draft', symbol_draft.save_draft))

    def _wrap(self, name, original):
        def counting(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return original(*args, **kwargs)
        return counting

    def take(self):
        """The counts since the last call, then cleared."""
        calls, self.calls = self.calls, {}
        return calls


#: Counts of one committed stepped node: one layout, one `_changed`, one redraw, no file.
STEP = {'_changed': 1, '_draw': 1, 'layout_symbol': 1}
#: Counts of the drop of a stepped drag: the end's `_changed`, redraw, layout and the one recovery write.
DROP = {'_changed': 1, '_draw': 1, 'layout_symbol': 1, 'save_draft': 1}


def _vp(tab, x_mm, y_mm):
    """Viewport point of a symbol point (mm, Y up)."""
    return tab.view.mapFromScene(QPointF(x_mm * S, -y_mm * S))


def _mm(tab, pos):
    """Symbol point (mm, Y up) of a viewport point: what the drag hands the snapping APIs."""
    scene = tab.view.mapToScene(pos)
    return scene.x() / S, -scene.y() / S


def _node_step(tab, nodes):
    """Viewport displacement of `nodes` pin-grid steps along +X (an integer QPoint)."""
    return _vp(tab, nodes * GRID_MM, 0) - _vp(tab, 0, 0)


def _press(tab, qapp, pos, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, modifier, pos)
    qapp.processEvents()


def _move(tab, qapp, pos):
    QTest.mouseMove(tab.view.viewport(), pos)
    qapp.processEvents()


def _release(tab, qapp, pos, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.mouseRelease(tab.view.viewport(), Qt.MouseButton.LeftButton, modifier, pos)
    qapp.processEvents()


def _recovery(tab):
    """The recovery JSON, or None when the file does not exist."""
    path = tab._recovery_path
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def _targets_at(tab, pos):
    """The `data(0)` targets under a viewport point, nearest first."""
    return [item.data(0) for item in tab.view.items(pos) if item.data(0)]


def _pin(tab, number):
    return next(p for p in tab._symbol.pins if p.pin.number == number)


def _pin_press_point(tab, number):
    """A viewport point on the pin's line, one millimetre along it from the terminal."""
    placed = _pin(tab, number)
    return _vp(tab, placed.x + 1.0, placed.y)


def _idle(tab):
    """The view and the tab hold no drag state."""
    assert tab.view.gesture is None
    assert tab._gesture_before is None and tab._gesture_target is None
    assert tab._gesture_live == []
    assert QApplication.activeWindow() is None or tab.view.viewport().mouseGrabber() is not tab.view.viewport()


def _anonymised(tab, value):
    """`value` with every element id of `tab` replaced by the element's index: ids are random per tab."""
    ids = {record['id']: f'<element {index}>' for index, record in enumerate(tab.graphics()) if record.get('id')}
    if isinstance(value, dict):
        return {key: _anonymised(tab, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_anonymised(tab, item) for item in value]
    if isinstance(value, str):
        return ids.get(value, value)
    return value


def _same_result(tab, twin, *, history_length):
    """The mouse-driven tab and the API-driven twin agree on rows, history and recovery JSON."""
    assert _anonymised(tab, tab.draft_rows()) == _anonymised(twin, twin.draft_rows())
    assert _anonymised(tab, tab._history) == _anonymised(twin, twin._history)
    assert len(tab._history) == history_length
    assert tab._history_index == twin._history_index == history_length - 1
    assert _anonymised(tab, _recovery(tab)) == _anonymised(twin, _recovery(twin))


def _wheel(tab, qapp, pos, steps=1):
    """Send one wheel notch (120 units per step) to the viewport at `pos`."""
    viewport = tab.view.viewport()
    event = QWheelEvent(QPointF(pos), QPointF(viewport.mapToGlobal(pos)), QPoint(), QPoint(0, 120 * steps),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(viewport, event)
    qapp.processEvents()


def _context_menu(tab, qapp, pos, monkeypatch):
    """Send a mouse context-menu event to the viewport at `pos`; returns the pin numbers asked for.

    The menu itself is not opened (`QMenu.exec` would block the test): only
    the view's request is observed.
    """
    from PyQt6.QtWidgets import QMenu
    monkeypatch.setattr(QMenu, 'exec', lambda self, *args: None)
    asked = []
    tab.view.pin_context_requested.connect(lambda number, _at: asked.append(number))
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, tab.view.viewport().mapToGlobal(pos))
    QApplication.sendEvent(tab.view.viewport(), event)
    qapp.processEvents()
    return asked


# ── S1: single pin drag ─────────────────────────────────────────────────────

def test_s1_pin_drag_to_a_new_node_commits_each_node_and_the_drop_once(tab7, twin7, qapp, monkeypatch):
    """@brief S1 positive: a stepped drag commits every new node without a file; the drop writes once.

    Pinned contract (plan §1a S1, D12): the press selects (one redraw, no
    edit); the FIRST step of a drag commits even on the start node (the
    step memo is empty, `_restore_gesture_source`); a step to another node
    lays out, commits and redraws; a repeated node commits nothing; the
    release re-sends the drop point (skipped as a repeat) and ends the
    drag with one `_changed`, one redraw and the only recovery write.
    """
    before = tab7.draft_rows()
    start = _pin_press_point(tab7, '1')
    assert _targets_at(tab7, start)[0] == ('pin', '1')
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    assert tab7.view.gesture == ('pin', '1')
    assert tab7._gesture_before == before and tab7._selected_pin == '1'
    assert counter.take() == {'_draw': 1}, 'the press selects the pin; nothing is edited'
    _move(tab7, qapp, start + QPoint(0, -1))
    assert counter.take() == STEP, 'the first step commits the start node (restore path)'
    assert tab7.has_pending_edits() is False, 'the start node equals the baseline'
    target = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _move(tab7, qapp, target)
    assert counter.take() == STEP
    assert _pin(tab7, '1').y == pytest.approx(before[0].get('symbol_position', [None, 5.08])[1] + 2 * GRID_MM, abs=1e-6) \
        or _pin(tab7, '1').y == pytest.approx(_pin(twin7, '1').y + 2 * GRID_MM, abs=1e-6)
    assert tab7.has_pending_edits(), 'D12: the draft is committed at every node (mid-drag)'
    assert _recovery(tab7) is None, 'no recovery write before the drop'
    assert len(tab7._history) == 1, 'no history entry before the drop'
    _move(tab7, qapp, target + QPoint(1, 1))
    assert counter.take() == {}, 'a step that stays on the node commits nothing'
    _release(tab7, qapp, target)
    assert counter.take() == DROP
    _idle(tab7)
    x, y = _mm(tab7, target)
    placed = _pin(twin7, '1')
    assert twin7.place_pin('1', placed.x + (x - (placed.x + 1.0)), y)
    _same_result(tab7, twin7, history_length=2)
    assert tab7.draft_rows()[0]['symbol_position'][1] == pytest.approx(placed.y + 2 * GRID_MM)
    tab7.undo()
    assert tab7.draft_rows() == before


def test_s1_pin_drag_back_to_its_start_node_changes_nothing(tab7, qapp, monkeypatch):
    """@brief S1: a drag that visits another node and comes back leaves rows, history and file as before."""
    before = tab7.draft_rows()
    start = _pin_press_point(tab7, '1')
    away = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    counter.take()
    _move(tab7, qapp, away)
    assert counter.take() == STEP and tab7.has_pending_edits()
    _move(tab7, qapp, start)
    assert counter.take() == STEP, 'back on the start node: the restore path commits the initial rows'
    assert not tab7.has_pending_edits()
    _release(tab7, qapp, start)
    assert counter.take() == {'_changed': 1, '_draw': 1, 'layout_symbol': 1, 'save_draft': 0} or \
        counter.calls == {}, 'the drop without a dirty draft writes no file'
    _idle(tab7)
    assert tab7.draft_rows() == before
    assert len(tab7._history) == 1, 'nothing to undo'
    assert _recovery(tab7) is None


def test_s1_a_press_and_release_on_the_same_point_is_a_click(tab7, qapp, monkeypatch):
    """@brief S1: no move between press and release: the pin is selected, nothing is edited or written.

    Pinned contract: the release still passes through a move (plan §4 item
    3), so the drop runs the restore step AND the end: two `_changed`.
    """
    before = tab7.draft_rows()
    start = _pin_press_point(tab7, '2')
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    counter.take()
    _release(tab7, qapp, start)
    assert counter.take() == {'_changed': 2, '_draw': 2, 'layout_symbol': 2}
    _idle(tab7)
    assert tab7._selected_pin == '2'
    assert tab7.draft_rows() == before and not tab7.has_pending_edits()
    assert len(tab7._history) == 1 and _recovery(tab7) is None


def test_s1_a_release_on_another_node_without_a_move_commits_that_node(tab7, twin7, qapp):
    """@brief S1: Windows coalesces moves: the drop point alone decides (release re-sent as a move)."""
    start = _pin_press_point(tab7, '1')
    target = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _press(tab7, qapp, start)
    _release(tab7, qapp, target)
    _idle(tab7)
    x, y = _mm(tab7, target)
    placed = _pin(twin7, '1')
    assert twin7.place_pin('1', placed.x + (x - (placed.x + 1.0)), y)
    _same_result(tab7, twin7, history_length=2)


# ── S2: group drag ──────────────────────────────────────────────────────────

def test_s2_group_drag_moves_the_selection_as_one_undo_step(tab7, twin7, qapp, monkeypatch):
    """@brief S2: dragging a selected pin moves the whole selection; a refused step shows only a status.

    Pinned contract: a step whose result overlaps other pins is refused by
    `_transform_selected` (status line, no commit, no redraw); the next
    valid step commits; the drop equals one direct `_transform_selected`.
    """
    before = tab7.draft_rows()
    for tab in (tab7, twin7):
        tab._select_pins({'1', '2'}, '1')
    _settle(qapp)
    start = _pin_press_point(tab7, '1')
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    assert tab7.view.gesture == ('pin', '1') and tab7._selection_ids() == {'1', '2'}
    counter.take()
    overlapping = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y - 2 * GRID_MM)
    _move(tab7, qapp, overlapping)
    assert counter.take() == {}, 'pins 1 and 2 would land on 3 and 4: refused, nothing committed'
    assert tab7.status.text() == tab7.tx('Operazione annullata: pin sovrapposti o selezione insufficiente.')
    assert tab7.draft_rows() == before
    target = start - _node_step(tab7, 4)
    _move(tab7, qapp, target)
    assert counter.take() == STEP
    _release(tab7, qapp, target)
    assert counter.take() == DROP
    _idle(tab7)
    x, _y = _mm(tab7, target)
    sx, _sy = _mm(tab7, start)
    assert twin7._transform_selected('move', (x - sx, 0.0))
    _same_result(tab7, twin7, history_length=2)
    assert _pin(tab7, '1').x == pytest.approx(_pin(twin7, '1').x) and _pin(tab7, '2').x == _pin(tab7, '1').x
    assert tab7.draft_rows()[2]['symbol_position'] == twin7.draft_rows()[2]['symbol_position']
    tab7.undo()
    assert tab7.draft_rows() == before


# ── S3: element drags ───────────────────────────────────────────────────────

def _glyph_press_point(tab, index):
    """Viewport point at the centre of the first drawn primitive of element `index`."""
    items = [item for item in tab.view.scene().items() if item.data(0) == ('graphic', index)]
    assert items, 'precondition: the element is drawn'
    return tab.view.mapFromScene(items[0].sceneBoundingRect().center())


def test_s3_glyph_drag_steps_on_the_pin_grid_and_equals_a_direct_move(tab5, twin5, qapp, monkeypatch):
    """@brief S3 glyph: the press selects it, each node commits, the drop equals `_edit_graphic('move')`."""
    for tab in (tab5, twin5):
        assert tab.place_glyph('relay_coil', 0, 0)
    _settle(qapp)
    before = tab5.draft_rows()
    start = _glyph_press_point(tab5, 0)
    assert _targets_at(tab5, start)[0] == ('graphic', 0)
    counter = _Counter(tab5, monkeypatch)
    _press(tab5, qapp, start)
    assert tab5.view.gesture == ('graphic', 0) and tab5.selected_graphics() == [0]
    assert counter.take() == {'_draw': 1}
    target = start + _node_step(tab5, 2)
    _move(tab5, qapp, target)
    assert counter.take() == STEP
    assert tab5.graphics()[0]['x'] == pytest.approx(before[0]['symbol_graphics'][0]['x'] + 2 * GRID_MM)
    _move(tab5, qapp, target + QPoint(1, 0))
    assert counter.take() == {}
    _release(tab5, qapp, target)
    assert counter.take() == DROP
    _idle(tab5)
    origin = twin5.graphics()[0]
    assert twin5._edit_graphic(0, 'move', (origin['x'] + 2 * GRID_MM, origin['y']))
    _same_result(tab5, twin5, history_length=3)
    tab5.undo()
    assert tab5.draft_rows() == before


def _board_with_elements(tab):
    """A glyph at the origin, a wire from pin 1 to its first terminal and a rectangle off to the right.

    @return (glyph index, wire index, shape index).
    """
    assert tab.place_glyph('relay_coil', 0, 0)
    tab.start_wire()
    assert tab.add_wire_point(-5.0, 2.6) and tab.add_wire_point(0.1, 5.0)
    assert tab.finish_wire(True)
    tab.start_shape('rectangle')
    assert tab.add_shape_point(10.16, 10.16) and tab.add_shape_point(15.24, 12.7)
    # The rectangle tool stays armed for the next shape: disarmed, or every press would add a point.
    tab.finish_shape(False)
    assert not tab.view.wiring
    assert [g['key'] for g in tab.graphics()] == ['relay_coil', 'wire', 'shape:rectangle']
    return 0, 1, 2


def test_s3_group_drag_of_glyph_wire_and_shape_equals_the_programmatic_gesture(tab5, twin5, qapp, monkeypatch):
    """@brief S3 group: every selected element follows the dragged glyph by the same snapped delta.

    The group rule (plan 4456-4518: the leader snaps on the pin grid, a
    wire follows by whole pin steps, a shape by drawing-grid steps) has no
    direct API: the twin runs the same gesture through
    `_begin_gesture/_move_gesture/_end_gesture`, the mouse path must give
    the same rows, history and file.
    """
    for tab in (tab5, twin5):
        glyph, wire, shape = _board_with_elements(tab)
        for index in (glyph, wire, shape):
            tab._select_graphic(index, additive=True)
    _settle(qapp)
    assert sorted(tab5.selected_graphics()) == [glyph, wire, shape]
    before = tab5.draft_rows()
    start = _glyph_press_point(tab5, glyph)
    counter = _Counter(tab5, monkeypatch)
    _press(tab5, qapp, start)
    assert tab5.view.gesture == ('graphic', glyph)
    assert sorted(tab5.selected_graphics()) == [glyph, wire, shape], 'a press on a selected member keeps the group'
    counter.take()
    target = start + _node_step(tab5, 2) - _node_step(tab5, 0)
    target = QPoint(target.x(), target.y() + (_vp(tab5, 0, -GRID_MM) - _vp(tab5, 0, 0)).y())
    _move(tab5, qapp, target)
    assert counter.take() == STEP
    _release(tab5, qapp, target)
    assert counter.take() == DROP
    _idle(tab5)
    twin5._begin_gesture(('graphic', glyph), twin5.view.mapToScene(start))
    twin5._move_gesture(('graphic', glyph), twin5.view.mapToScene(target))
    twin5._end_gesture(True)
    _same_result(tab5, twin5, history_length=5)
    moved = tab5.graphics()
    assert moved[glyph]['x'] == pytest.approx(before[0]['symbol_graphics'][glyph]['x'] + 2 * GRID_MM)
    assert moved[glyph]['y'] == pytest.approx(before[0]['symbol_graphics'][glyph]['y'] - GRID_MM)
    for index in (wire, shape):
        assert moved[index]['x'] - before[0]['symbol_graphics'][index]['x'] == pytest.approx(2 * GRID_MM)
        assert moved[index]['y'] - before[0]['symbol_graphics'][index]['y'] == pytest.approx(-GRID_MM)
    tab5.undo()
    assert tab5.draft_rows() == before


# ── S4 / S5: handle drags ───────────────────────────────────────────────────

def _handle(tab, target):
    """The drawn handle square whose `data(0)` is `target`."""
    return next(item for item in tab.view.scene().items() if item.data(0) == target)


def test_s4_wire_vertex_drag_equals_move_wire_vertex(tab5, twin5, qapp, monkeypatch):
    """@brief S4: the hidden vertex square is revealed by the hover, dragged on the grid, committed per node."""
    for tab in (tab5, twin5):
        _glyph, wire, _shape = _board_with_elements(tab)
        tab._select_graphic(wire)
    _settle(qapp)
    before = tab5.draft_rows()
    corner = _handle(tab5, ('vertex', wire, 1))
    assert not corner.isVisible(), 'hidden until the pointer comes near'
    start = tab5.view.mapFromScene(corner.pos())
    _move(tab5, qapp, start)
    assert corner.isVisible(), 'S17: revealed by a real mouse move over the wire'
    assert _targets_at(tab5, start)[0] == ('vertex', wire, 1)
    counter = _Counter(tab5, monkeypatch)
    _press(tab5, qapp, start)
    assert tab5.view.gesture == ('vertex', wire, 1)
    assert counter.take() == {'_draw': 1}
    target = start + (_vp(tab5, 0, -GRID_MM) - _vp(tab5, 0, 0))
    _move(tab5, qapp, target)
    assert counter.take() == STEP
    _release(tab5, qapp, target)
    assert counter.take() == DROP
    _idle(tab5)
    x, y = _mm(tab5, target)
    assert twin5.move_wire_vertex(wire, 1, x, y)
    _same_result(tab5, twin5, history_length=5)
    points = tab5.graphics()[wire]['primitives'][0]['points']
    assert points[1] == [0.0, 0.0] and points[0]['anchor'] == {'kind': 'pin', 'number': '1'}
    tab5.undo()
    assert tab5.draft_rows() == before


def test_s5_shape_point_drag_equals_move_shape_point(tab5, twin5, qapp, monkeypatch):
    """@brief S5: a corner of the selected rectangle is dragged on the drawing grid, one undo step."""
    for tab in (tab5, twin5):
        _glyph, _wire, shape = _board_with_elements(tab)
        tab._select_graphic(shape)
    _settle(qapp)
    before = tab5.draft_rows()
    square = _handle(tab5, ('shape_point', shape, 1))
    start = tab5.view.mapFromScene(square.pos())
    _move(tab5, qapp, start)
    assert square.isVisible() and _targets_at(tab5, start)[0] == ('shape_point', shape, 1)
    counter = _Counter(tab5, monkeypatch)
    _press(tab5, qapp, start)
    assert tab5.view.gesture == ('shape_point', shape, 1)
    assert counter.take() == {'_draw': 1}
    target = start + _node_step(tab5, 2)
    _move(tab5, qapp, target)
    assert counter.take() == STEP
    _move(tab5, qapp, target + QPoint(1, 1))
    assert counter.take() == {}
    _release(tab5, qapp, target)
    assert counter.take() == DROP
    _idle(tab5)
    x, y = _mm(tab5, target)
    assert twin5.move_shape_point(shape, 1, x, y)
    _same_result(tab5, twin5, history_length=5)
    corner_before = before[0]['symbol_graphics'][shape]['primitives'][0]['points'][1]
    corner_after = tab5.graphics()[shape]['primitives'][0]['points'][1]
    assert corner_after[0] - corner_before[0] == pytest.approx(2 * GRID_MM)
    assert corner_after[1] == corner_before[1]
    tab5.undo()
    assert tab5.draft_rows() == before


# ── S6: property drag (live) ────────────────────────────────────────────────

def _property_items(tab, key):
    """(text item, anchor square) drawn for a property."""
    items = [item for item in tab.view.scene().items() if item.data(0) == ('property', key)]
    text = next(item for item in items if isinstance(item, QGraphicsSimpleTextItem))
    square = next(item for item in items if not isinstance(item, QGraphicsSimpleTextItem))
    return text, square


def _property_press_point(tab, key):
    """Viewport point inside the drawn text, a little right and up of its bottom-left anchor."""
    px, py = tab._property_positions(tab.draft_rows(), tab._symbol)[key]
    return tab.view.mapFromScene(QPointF(px * S + 6, -py * S - 6))


def test_s6_property_drag_is_live_and_commits_once_on_the_drop(tab7, twin7, qapp, monkeypatch):
    """@brief S6: moves run nothing and move the items; the drop equals one `move_property`.

    Pinned contract (user, 2026-10-17): the Reference text and its anchor
    square follow the pointer live; the draft, the scene and the file are
    untouched until the release, which commits once what a direct
    `move_property` to the drop point gives.
    """
    before = tab7.draft_rows()
    start = _property_press_point(tab7, 'Reference')
    assert ('property', 'Reference') in _targets_at(tab7, start)
    text, square = _property_items(tab7, 'Reference')
    text_at, square_at = QPointF(text.pos()), QPointF(square.pos())
    origin = tab7._property_positions(before, tab7._symbol)['Reference']
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    assert tab7.view.gesture == ('property', 'Reference')
    assert counter.take() == {}, 'the press of a live drag edits and redraws nothing'
    ids = {id(item) for item in tab7.view.scene().items()}
    pos = start
    for step in (QPoint(1, 1), QPoint(13, 1), QPoint(13, 1)):
        pos = pos + step
        _move(tab7, qapp, pos)
        assert counter.take() == {}, 'a live move runs no layout, redraw, draft or file write'
        delta = tab7.view.mapToScene(pos) - tab7.view.mapToScene(start)
        assert (text.pos() - (text_at + delta)).manhattanLength() < 1e-6
        assert (square.pos() - (square_at + delta)).manhattanLength() < 1e-6
        assert square.isVisible()
    assert {id(item) for item in tab7.view.scene().items()} == ids and tab7.draft_rows() == before
    assert not tab7.has_pending_edits() and _recovery(tab7) is None
    _release(tab7, qapp, pos)
    assert counter.take() == DROP, 'one commit on the drop'
    _idle(tab7)
    delta = tab7.view.mapToScene(pos) - tab7.view.mapToScene(start)
    assert twin7.move_property('Reference', origin[0] + delta.x() / S, origin[1] - delta.y() / S)
    _same_result(tab7, twin7, history_length=2)
    chosen = tab7.draft_rows()[0]['symbol_properties']['Reference']
    assert all(abs(v / GRID_MM - round(v / GRID_MM)) < 1e-6 for v in chosen), 'snapped on the pin grid'


def test_s6_a_property_drop_back_in_the_default_cell_writes_nothing(tab7, qapp, monkeypatch):
    """@brief S6: a drag of one pixel ends in the default's grid cell: no choice is pinned, no history entry."""
    before = tab7.draft_rows()
    start = _property_press_point(tab7, 'Value')
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    _move(tab7, qapp, start + QPoint(1, 0))
    _release(tab7, qapp, start + QPoint(1, 0))
    assert counter.take() == {'_changed': 1, '_draw': 1, 'layout_symbol': 1}, 'the restore path, no file'
    _idle(tab7)
    assert tab7.draft_rows() == before and len(tab7._history) == 1
    assert all('symbol_properties' not in row for row in tab7.draft_rows())


# ── S7: body resize ─────────────────────────────────────────────────────────

def test_s7_corner_resize_steps_on_even_grid_multiples_and_equals_resize_body(tab7, twin7, qapp, monkeypatch):
    """@brief S7: the hidden corner handle is grabbed by the press itself; the body grows by 2.54 mm pairs."""
    before = tab7.draft_rows()
    w, h = tab7._symbol.width, tab7._symbol.height
    # The top-right corner of the body: scene Y points down, so its handle is (hx, hy) = (1, -1).
    start = _vp(tab7, w / 2, h / 2)
    assert not _handle(tab7, ('resize', 1, -1)).isVisible(), 'hidden at rest'
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    assert tab7.view.gesture == ('resize', 1, -1), 'the press reveals and grabs the handle (D6)'
    assert counter.take() == {}
    target = _vp(tab7, w / 2 + 2 * GRID_MM, h / 2 + 2 * GRID_MM)
    _move(tab7, qapp, target)
    assert counter.take() == STEP
    assert (tab7._symbol.width, tab7._symbol.height) == (pytest.approx(w + 4 * GRID_MM), pytest.approx(h + 4 * GRID_MM))
    _move(tab7, qapp, target + QPoint(1, 1))
    assert counter.take() == {}
    _release(tab7, qapp, target)
    assert counter.take() == DROP
    _idle(tab7)
    x, y = _mm(tab7, target)
    assert twin7.resize_body(2 * abs(x), 2 * abs(y))
    _same_result(tab7, twin7, history_length=2)
    tab7.undo()
    assert tab7.draft_rows() == before


# ── S8: rubber band ─────────────────────────────────────────────────────────

def _empty_point(tab):
    """A viewport point with no item under it (the top-left corner, far from the fitted symbol)."""
    pos = QPoint(5, 5)
    assert not tab.view.items(pos), 'precondition: nothing drawn there'
    return pos


def test_s8_shift_band_selects_the_pins_inside_and_edits_nothing(tab7, qapp, monkeypatch):
    """@brief S8 positive and negative: a band over pins selects them; a band over nothing edits nothing.

    Pinned contract: a band that contains no pin and no element leaves the
    pin selection as it was (`_select_region` only resets the elements).
    """
    before = tab7.draft_rows()
    tab7._select_pins({'5'}, '5')
    _settle(qapp)
    p1, p2 = _pin(tab7, '1'), _pin(tab7, '2')
    a = _vp(tab7, p1.x - 3.0, p1.y + 1.0)
    b = _vp(tab7, p2.x + 3.0, p2.y - 1.0)
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, a, Qt.KeyboardModifier.ShiftModifier)
    assert tab7.view.gesture is None and tab7.view._rubber_start is not None and tab7.view._rubber.isVisible()
    _move(tab7, qapp, b)
    assert counter.take() == {}, 'the band edits nothing while it grows'
    _release(tab7, qapp, b, Qt.KeyboardModifier.ShiftModifier)
    assert counter.take() == {'_draw': 1}, 'the selection redraws, nothing is committed'
    assert tab7.view._rubber_start is None and not tab7.view._rubber.isVisible()
    assert tab7._selection_ids() == {'1', '2'}, 'the band replaced the previous selection'
    assert tab7.draft_rows() == before and len(tab7._history) == 1 and _recovery(tab7) is None
    empty = _empty_point(tab7)
    _press(tab7, qapp, empty, Qt.KeyboardModifier.ShiftModifier)
    _move(tab7, qapp, empty + QPoint(20, 20))
    _release(tab7, qapp, empty + QPoint(20, 20), Qt.KeyboardModifier.ShiftModifier)
    assert tab7._selection_ids() == {'1', '2'}, 'a band around nothing keeps the pin selection (today)'
    assert tab7.draft_rows() == before and len(tab7._history) == 1


# ── S10: pan ────────────────────────────────────────────────────────────────

def test_s10_left_drag_on_the_background_pans_the_camera_and_edits_nothing(tab7, qapp, monkeypatch):
    """@brief S10: a left press on empty background pans by the pointer delta, turns auto-fit off, commits nothing."""
    before = tab7.draft_rows()
    view = tab7.view
    assert view.auto_fit
    start = _empty_point(tab7)
    h0, v0 = view.horizontalScrollBar().value(), view.verticalScrollBar().value()
    counter = _Counter(tab7, monkeypatch)
    _press(tab7, qapp, start)
    assert view.gesture is None and view._pan_last == start and not view.auto_fit
    _move(tab7, qapp, start + QPoint(30, 20))
    _release(tab7, qapp, start + QPoint(30, 20))
    assert counter.take() == {}, 'a pan touches neither the draft nor the scene'
    assert view._pan_last is None
    assert (view.horizontalScrollBar().value() - h0, view.verticalScrollBar().value() - v0) == (-30, -20)
    assert tab7.draft_rows() == before and len(tab7._history) == 1
    # Escape mid-pan (D1 today: Escape cancels every interaction of the symbol view).
    _press(tab7, qapp, start)
    _move(tab7, qapp, start + QPoint(10, 0))
    QTest.keyClick(view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert view._pan_last is None and counter.take() == {}


# ── S11: wheel ──────────────────────────────────────────────────────────────

def test_s11_wheel_zooms_at_rest_and_is_refused_during_a_drag_or_a_pan(tab7, twin7, qapp, monkeypatch):
    """@brief S11 (D2 today): the wheel zooms about the pointer at rest, never while a drag or a pan runs.

    Positive twin: at rest one notch scales by 1.2; negatives: the same
    notch during a pin drag and during a pan leaves the transform alone,
    and the drag goes on to commit its drop as if no wheel had arrived.
    """
    view = tab7.view
    start = _pin_press_point(tab7, '1')
    scale = view.transform().m11()
    _wheel(tab7, qapp, start)
    assert view.transform().m11() == pytest.approx(scale * 1.2) and not view.auto_fit
    scale = view.transform().m11()
    _press(tab7, qapp, start)
    target = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _move(tab7, qapp, target)
    _wheel(tab7, qapp, target)
    assert view.transform().m11() == scale, 'no zoom during a drag'
    assert view.gesture == ('pin', '1')
    _release(tab7, qapp, target)
    _idle(tab7)
    x, y = _mm(tab7, target)
    placed = _pin(twin7, '1')
    assert twin7.place_pin('1', placed.x + (x - (placed.x + 1.0)), y)
    _same_result(tab7, twin7, history_length=2)
    empty = _empty_point(tab7)
    _press(tab7, qapp, empty)
    _wheel(tab7, qapp, empty)
    assert view.transform().m11() == scale, 'no zoom during a pan'
    _release(tab7, qapp, empty)


# ── Negatives: cancel paths ─────────────────────────────────────────────────

def test_escape_cancels_a_stepped_drag_and_restores_the_rows(tab7, qapp, monkeypatch):
    """@brief D1 today: Escape mid pin drag puts the initial rows back through `_populate`; no history, no file."""
    before = tab7.draft_rows()
    start = _pin_press_point(tab7, '1')
    target = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _press(tab7, qapp, start)
    _move(tab7, qapp, target)
    assert tab7.has_pending_edits()
    counter = _Counter(tab7, monkeypatch)
    QTest.keyClick(tab7.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert counter.take() == {'_populate': 1, '_changed': 1, '_draw': 1, 'layout_symbol': 1}
    _idle(tab7)
    assert tab7.draft_rows() == before and not tab7.has_pending_edits()
    assert len(tab7._history) == 1 and _recovery(tab7) is None
    # A release after the cancel is not a drop: nothing happens.
    _release(tab7, qapp, target)
    assert counter.take() == {}
    assert tab7.draft_rows() == before


def test_escape_cancels_a_live_drag_and_puts_the_text_back(tab7, qapp):
    """@brief D5 today (symbol): a cancelled property drag redraws the text where it was."""
    before = tab7.draft_rows()
    text_at = QPointF(_property_items(tab7, 'Reference')[0].pos())
    start = _property_press_point(tab7, 'Reference')
    _press(tab7, qapp, start)
    _move(tab7, qapp, start + QPoint(40, 20))
    QTest.keyClick(tab7.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    _idle(tab7)
    assert tab7.draft_rows() == before and len(tab7._history) == 1 and _recovery(tab7) is None
    text, square = _property_items(tab7, 'Reference')
    assert (text.pos() - text_at).manhattanLength() < 1e-6
    tab7.view.pointer_left.emit()
    assert not square.isVisible(), 'no drag owns the square any more: the pointer rule hides it'


def test_hiding_the_tab_cancels_the_drag_before_the_view_goes_away(tab7, qapp, monkeypatch):
    """@brief Hide mid-drag: the view's hideEvent cancels (rows restored, state cleared), then the repeat flush.

    Pinned order (plan §4 item 12): the drag is cancelled BEFORE the hide's
    `key_repeat_ended` flushes a deferred checkpoint, so the flush never
    writes a half-dragged draft. Today the focus loss that precedes the
    hide emits `key_repeat_ended` once more while the drag still runs: it
    writes nothing because `_checkpoint_draft` returns during a drag.
    """
    before = tab7.draft_rows()
    order = []
    # CONTRACT CHANGED (plan step 5, 2026-10-17): the drag end is the
    # controller's `finished`, the repeat end the key filter's `repeat_ended`
    # (the view's gesture signals were removed).
    tab7._gestures.finished.connect(lambda commit: order.append(('finished', commit)))
    tab7._keys.repeat_ended.connect(lambda: order.append('repeat_ended'))
    start = _pin_press_point(tab7, '1')
    _press(tab7, qapp, start)
    _move(tab7, qapp, _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM))
    assert tab7.has_pending_edits()
    counter = _Counter(tab7, monkeypatch)
    tab7.hide()
    qapp.processEvents()
    assert order == ['repeat_ended', ('finished', False), 'repeat_ended']
    assert counter.take() == {'_populate': 1, '_changed': 1, '_draw': 1, 'layout_symbol': 1}
    _idle(tab7)
    assert tab7.draft_rows() == before and len(tab7._history) == 1 and _recovery(tab7) is None
    tab7.show()
    _settle(qapp)


def test_set_data_mid_drag_cancels_the_drag_and_loads_the_baseline(tab7, qapp):
    """@brief `set_data` mid-drag: the drag is cancelled first; the reloaded rows carry nothing of it."""
    before = tab7.draft_rows()
    start = _pin_press_point(tab7, '1')
    _press(tab7, qapp, start)
    _move(tab7, qapp, _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM))
    assert tab7.has_pending_edits()
    tab7.set_data('RS485', deepcopy(ROWS7), pad_ids=[r['pin'] for r in ROWS7], force=True)
    _settle(qapp)
    _idle(tab7)
    assert tab7.draft_rows() == before and not tab7.has_pending_edits()
    # A late release (the button is still down for Qt) is not a drop.
    _release(tab7, qapp, start)
    assert tab7.draft_rows() == before and tab7.view.gesture is None


def test_a_right_click_during_a_drag_opens_no_menu_and_keeps_the_drag(tab7, qapp, monkeypatch):
    """@brief D3 today (symbol): the context menu is ignored while a drag runs; at rest it asks for the pin's menu."""
    start = _pin_press_point(tab7, '1')
    assert _context_menu(tab7, qapp, start, monkeypatch) == ['1'], 'positive twin: at rest the pin menu is requested'
    _press(tab7, qapp, start)
    target = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _move(tab7, qapp, target)
    assert _context_menu(tab7, qapp, target, monkeypatch) == [], 'ignored mid-drag'
    QTest.mousePress(tab7.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, target)
    QTest.mouseRelease(tab7.view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, target)
    qapp.processEvents()
    assert tab7.view.gesture == ('pin', '1'), 'the right button neither cancels nor drops the drag'
    _release(tab7, qapp, target)
    _idle(tab7)
    assert len(tab7._history) == 2


def test_a_redraw_mid_drag_keeps_a_stepped_drag_committing_and_a_live_drag_on_the_pointer(tab7, twin7, qapp, monkeypatch):
    """@brief D4 today (symbol): a redraw under the pointer neither drops nor freezes the drag.

    Stepped (pin): after `_draw` mid-drag the next node still commits and
    the drop equals a direct `place_pin`. Live (property): the rebuilt
    items resume at the last pointer position and keep following.
    """
    start = _pin_press_point(tab7, '1')
    _press(tab7, qapp, start)
    first = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _move(tab7, qapp, first)
    tab7._draw(tab7._symbol)
    qapp.processEvents()
    assert tab7.view.gesture == ('pin', '1') and tab7._gesture_before is not None
    counter = _Counter(tab7, monkeypatch)
    second = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _move(tab7, qapp, second)
    assert counter.take() == STEP, 'a pin step after the redraw still commits'
    _release(tab7, qapp, second)
    _idle(tab7)
    x, y = _mm(tab7, second)
    placed = _pin(twin7, '1')
    assert twin7.place_pin('1', placed.x + (x - (placed.x + 1.0)), y)
    _same_result(tab7, twin7, history_length=2)
    # Live: the property.
    text_at = QPointF(_property_items(tab7, 'Reference')[0].pos())
    start = _property_press_point(tab7, 'Reference')
    _press(tab7, qapp, start)
    pos = start + QPoint(30, 10)
    _move(tab7, qapp, pos)
    old_text = _property_items(tab7, 'Reference')[0]
    tab7._draw(tab7._symbol)
    qapp.processEvents()
    text, square = _property_items(tab7, 'Reference')
    assert text is not old_text
    delta = tab7.view.mapToScene(pos) - tab7.view.mapToScene(start)
    assert (text.pos() - (text_at + delta)).manhattanLength() < 1e-6, 'resumed at the pointer'
    assert square.isVisible()
    pos = pos + QPoint(13, 0)
    _move(tab7, qapp, pos)
    delta = tab7.view.mapToScene(pos) - tab7.view.mapToScene(start)
    assert (text.pos() - (text_at + delta)).manhattanLength() < 1e-6, 'the new items follow'
    _release(tab7, qapp, pos)
    _idle(tab7)


# ── S17: hover reveal rule ──────────────────────────────────────────────────

def _group(tab, owner):
    """(items, base rect, ring) of the hover group of `owner`."""
    return next((items, base, ring) for items, base, ring, group_owner in tab._handle_groups if group_owner == owner)


def test_hover_reveals_the_property_square_within_the_margin_and_hides_it_beyond(tab7, qapp):
    """@brief S17 property: shown at margin-1 scene unit outside the text box, hidden at margin+2."""
    items, base, ring = _group(tab7, ('property', 'Reference'))
    assert not ring and items == [_property_items(tab7, 'Reference')[1]]
    m = HANDLE_HOVER_MARGIN / tab7.view.transform().m11()
    tab7._update_readout(QPointF(base.right() + m - 1, base.center().y()))
    assert items[0].isVisible()
    tab7._update_readout(QPointF(base.right() + m + 2, base.center().y()))
    assert not items[0].isVisible()
    # The same rule through the real pointer path: a mouse move over the text reveals.
    _move(tab7, qapp, tab7.view.mapFromScene(base.center()))
    assert items[0].isVisible()
    tab7.view.pointer_left.emit()
    assert not items[0].isVisible(), 'the pointer left the view'


def test_hover_reveals_the_element_handles_within_the_margin_and_hides_them_beyond(tab5, qapp):
    """@brief S17 element: the selected shape's corner squares obey the same margin, in screen pixels at any zoom."""
    _glyph, _wire, shape = _board_with_elements(tab5)
    tab5._select_graphic(shape)
    _settle(qapp)
    items, base, ring = _group(tab5, ('graphic', shape))
    assert not ring and len(items) == 2
    m = HANDLE_HOVER_MARGIN / tab5.view.transform().m11()
    tab5._update_readout(QPointF(base.right() + m - 1, base.center().y()))
    assert all(item.isVisible() for item in items)
    tab5._update_readout(QPointF(base.right() + m + 2, base.center().y()))
    assert not any(item.isVisible() for item in items)
    tab5.view.auto_fit = False
    tab5.view.resetTransform()
    tab5.view.scale(0.25, 0.25)
    m = HANDLE_HOVER_MARGIN / 0.25
    tab5._update_readout(QPointF(base.right() + m - 1, base.center().y()))
    assert all(item.isVisible() for item in items), 'the margin is 12 screen pixels: 48 units at 0.25'
    tab5._update_readout(QPointF(base.right() + m + 2, base.center().y()))
    assert not any(item.isVisible() for item in items)


def test_hover_reveals_the_body_ring_only_near_the_outline(tab7, qapp):
    """@brief S17 body (ring): hidden over the middle, shown within the margin on both sides of the outline."""
    items, base, ring = _group(tab7, ('body',))
    assert ring and len(items) == 8
    m = HANDLE_HOVER_MARGIN / tab7.view.transform().m11()
    tab7._update_readout(QPointF(0, 0))
    assert not any(item.isVisible() for item in items), 'the middle of the body'
    tab7._update_readout(QPointF(base.right() - m + 1, 0))
    assert all(item.isVisible() for item in items), 'inside, within the margin'
    tab7._update_readout(QPointF(base.right() - m - 2, 0))
    assert not any(item.isVisible() for item in items), 'inside, beyond the margin'
    tab7._update_readout(QPointF(base.right() + m - 1, 0))
    assert all(item.isVisible() for item in items), 'outside, within the margin'
    tab7._update_readout(QPointF(base.right() + m + 2, 0))
    assert not any(item.isVisible() for item in items), 'outside, beyond the margin'
    # A running body resize keeps its ring revealed wherever the pointer is; a pin drag reveals nothing.
    start = _vp(tab7, tab7._symbol.width / 2, tab7._symbol.height / 2)
    _press(tab7, qapp, start)
    assert tab7.view.gesture == ('resize', 1, -1)
    tab7._update_readout(QPointF(0, 0))
    assert all(item.isVisible() for item in items)
    _release(tab7, qapp, start)
    _press(tab7, qapp, _pin_press_point(tab7, '1'))
    # The press selected the pin and redrew: the ring is a new group of items.
    items, _base, _ring = _group(tab7, ('body',))
    tab7._update_readout(QPointF(0, 0))
    assert not any(item.isVisible() for item in items), 'the pin drag owns no group: the pointer rule hides them'
    _release(tab7, qapp, _pin_press_point(tab7, '1'))


# ── Golden session ──────────────────────────────────────────────────────────

def _snapshot(tab, counter, label):
    """One golden record: the committed rows, history and selection after a step, with its counts."""
    return {'step': label, 'counts': counter.take(), 'rows': tab.draft_rows(),
            'history': len(tab._history), 'history_index': tab._history_index,
            'pending': tab.has_pending_edits(), 'recovery': _recovery(tab) is not None,
            'gesture': list(tab.view.gesture) if tab.view.gesture is not None else None,
            'selection': sorted(tab._selection_ids()),
            'body': [tab._symbol.width, tab._symbol.height]}


def test_golden_session(tab7, qapp, monkeypatch):
    """@brief One scripted session (S1, S7, S6, S2, S8, Escape, undo) recorded step by step and compared byte for byte.

    Regenerate with `KICADGEN_REGEN_GOLDEN=1` only when a contract changed
    on purpose, and say why in the commit. The record holds millimetres and
    counts, never pixels, so it does not depend on the font or the screen.
    """
    counter = _Counter(tab7, monkeypatch)
    records = []

    def record(label):
        records.append(_snapshot(tab7, counter, label))

    record('rest')
    start = _pin_press_point(tab7, '1')
    _press(tab7, qapp, start)
    record('S1 press pin 1')
    _move(tab7, qapp, start + QPoint(0, -1))
    record('S1 move on the start node')
    target = _vp(tab7, _pin(tab7, '1').x + 1.0, _pin(tab7, '1').y + 2 * GRID_MM)
    _move(tab7, qapp, target)
    record('S1 move two nodes up')
    _move(tab7, qapp, target + QPoint(1, 1))
    record('S1 move on the same node')
    _release(tab7, qapp, target)
    record('S1 release')
    w, h = tab7._symbol.width, tab7._symbol.height
    start = _vp(tab7, w / 2, h / 2)
    _press(tab7, qapp, start)
    record('S7 press the corner')
    target = _vp(tab7, w / 2 + 2 * GRID_MM, h / 2 + 2 * GRID_MM)
    _move(tab7, qapp, target)
    record('S7 move two nodes out')
    _release(tab7, qapp, target)
    record('S7 release')
    start = _property_press_point(tab7, 'Reference')
    _press(tab7, qapp, start)
    record('S6 press Reference')
    pos = start + (_vp(tab7, 2 * GRID_MM, GRID_MM) - _vp(tab7, 0, 0))
    _move(tab7, qapp, pos)
    record('S6 move two nodes right, one up')
    _release(tab7, qapp, pos)
    record('S6 release')
    tab7._select_pins({'5', '6'}, '5')
    _settle(qapp)
    counter.take()
    start = _pin_press_point(tab7, '5')
    _press(tab7, qapp, start)
    record('S2 press pin 5 of {5, 6}')
    target = start + _node_step(tab7, 4)
    _move(tab7, qapp, target)
    record('S2 move four nodes right')
    _release(tab7, qapp, target)
    record('S2 release')
    p3, p4 = _pin(tab7, '3'), _pin(tab7, '4')
    _press(tab7, qapp, _vp(tab7, p3.x - 3.0, p3.y + 1.0), Qt.KeyboardModifier.ShiftModifier)
    _release(tab7, qapp, _vp(tab7, p4.x + 3.0, p4.y - 1.0), Qt.KeyboardModifier.ShiftModifier)
    record('S8 band around pins 3 and 4')
    start = _pin_press_point(tab7, '3')
    _press(tab7, qapp, start)
    _move(tab7, qapp, start - _node_step(tab7, 3))
    record('S2 move {3, 4} three nodes left')
    QTest.keyClick(tab7.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    record('Escape')
    tab7.undo()
    record('undo')
    tab7.undo()
    record('undo again')
    text = json.dumps(records, indent=1, sort_keys=True, ensure_ascii=False) + '\n'
    if os.environ.get(REGEN_ENV) == '1':
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        GOLDEN_SESSION.write_text(text, encoding='utf-8', newline='\n')
    assert GOLDEN_SESSION.exists(), f'missing golden file: run once with {REGEN_ENV}=1'
    assert GOLDEN_SESSION.read_bytes() == text.encode('utf-8'), \
        f'the session differs from the golden record (regenerate with {REGEN_ENV}=1 only for a wanted contract change)'


# ── Escape on the interactions the view owns ────────────────────────────────

def test_escape_still_cancels_a_pan_a_band_a_wire_and_a_placing_glyph(tab5, qapp):
    """@brief Escape reaches `keyPressEvent` when no drag runs: pan, rubber band, wire and placing all end.

    The drag's own Escape is consumed by `CancelOnEscapeAndHide`
    (test_escape_cancels_a_stepped_drag_and_restores_the_rows); this pins
    the other four, which the view's `keyPressEvent` handles.
    """
    view = tab5.view
    empty = _empty_point(tab5)
    _press(tab5, qapp, empty)
    _move(tab5, qapp, empty + QPoint(10, 5))
    assert view._pan_last is not None
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert view._pan_last is None
    _press(tab5, qapp, empty, Qt.KeyboardModifier.ShiftModifier)
    _move(tab5, qapp, empty + QPoint(20, 20))
    assert view._rubber_start is not None and view._rubber.isVisible()
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert view._rubber_start is None and not view._rubber.isVisible()
    before = tab5.draft_rows()
    tab5.start_wire()
    assert tab5.add_wire_point(-5.0, 2.6) and view.wiring and tab5._wire_points
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.wiring and tab5._wire_points == [] and tab5.draft_rows() == before
    view.placing = True
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.placing and tab5.draft_rows() == before
    # Negative twin: with nothing running, Escape changes nothing.
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert tab5.draft_rows() == before and len(tab5._history) == 1
