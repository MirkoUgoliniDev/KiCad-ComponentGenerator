"""@brief A property drag in the symbol editor is as smooth as the footprint preview's text drag.

User, 2026-10-17: «il trascinamento di REF** in Anteprima Footprint è molto
smooth, ma se provo a muovere U? in Simbolo elettrico è un disastro» (the
footprint text drag is very smooth, the symbol's is a disaster). The project
rule is one UX in every tab: the footprint preview moves the text item live
and commits once on release (`FootprintView._text_drag`). The symbol editor
now does the same for Reference and Value.

The contracts are counts and identical outputs, never wall-clock times:

- N real mouse moves (QTest on the viewport) run no layout, no redraw, no
  draft write and no recovery-file write, and rebuild no scene item; the
  text and its anchor square follow the pointer;
- the release commits once: the rows, the history and the recovery file
  equal those of a direct `move_property` to the same point;
- a cancelled drag (Escape) leaves the rows untouched and puts the items
  back;
- negative twins: an edit outside a drag still writes at once, and a pin
  drag still commits its snapped steps (unchanged contract).
"""

import json
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

import kicadgen.gui.symbol_tab as symbol_tab_module
import kicadgen.services.symbol_draft as symbol_draft
from kicadgen.gui.symbol_tab import GRID_MM, SCENE_UNITS_PER_MM, SymbolTab

#: The RS485 project of the report: seven pins, four left and three right.
ROWS = [{'pin': str(i), 'name': n, 'symbol_side': 'left' if i <= 4 else 'right'}
        for i, n in enumerate(['A', 'B', 'GND', 'VCC', 'RO', 'DI', 'DE'], 1)]


@pytest.fixture
def tab(qapp, tmp_path):
    """A shown symbol tab of the seven-pin project with a recovery file bound."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('RS485', deepcopy(ROWS), pad_ids=[r['pin'] for r in ROWS])
    tab.set_recovery_path(tmp_path / '.symbol-draft.json')
    tab.show()
    for _ in range(10):
        qapp.processEvents()
    yield tab
    tab.deleteLater()


class _Counter:
    """Counts the calls of the tab methods and module functions a drag step must not run."""

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

    def reset(self):
        self.calls = {}


def _property_items(tab, key):
    """(text item, anchor square) drawn for a property."""
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    items = [item for item in tab.view.scene().items() if item.data(0) == ('property', key)]
    text = next(item for item in items if isinstance(item, QGraphicsSimpleTextItem))
    square = next(item for item in items if not isinstance(item, QGraphicsSimpleTextItem))
    return text, square


def _press_point(tab, key='Reference'):
    """Viewport point inside the drawn property text, a little right and up of its anchor."""
    px, py = tab._property_positions(tab.draft_rows(), tab._symbol)[key]
    scene_point = QPointF(px * SCENE_UNITS_PER_MM + 6, -py * SCENE_UNITS_PER_MM - 6)
    return tab.view.mapFromScene(scene_point)


def _press(tab, qapp, key='Reference'):
    """Press the left button on the property; returns the viewport point."""
    start = _press_point(tab, key)
    hits = [item.data(0) for item in tab.view.items(start) if item.data(0)]
    assert ('property', key) in hits, 'precondition: the press lands on the property text'
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    qapp.processEvents()
    assert tab.view.gesture == ('property', key)
    return start


def _moves(tab, qapp, start, steps):
    """Send `steps` mouse moves (1 px, then 13 px, so some stay on a node and some cross one)."""
    pos = QPoint(start)
    for index in range(steps):
        pos = pos + QPoint(1 if index < steps // 2 else 13, 1)
        QTest.mouseMove(tab.view.viewport(), pos)
        qapp.processEvents()
    return pos


def _release(tab, qapp, pos):
    QTest.mouseRelease(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    qapp.processEvents()


def _scene_delta(tab, start, pos):
    """Scene displacement between two viewport points."""
    return tab.view.mapToScene(pos) - tab.view.mapToScene(start)


def test_mouse_moves_of_a_property_drag_write_nothing_and_rebuild_nothing(tab, qapp, monkeypatch):
    """@brief Positive: forty real moves run no layout, redraw, draft or file write; the items follow."""
    start = _press(tab, qapp)
    text, square = _property_items(tab, 'Reference')
    text_at, square_at = QPointF(text.pos()), QPointF(square.pos())
    ids_before = {id(item) for item in tab.view.scene().items()}
    draft_before = deepcopy(tab._draft)
    counter = _Counter(tab, monkeypatch)
    pos = _moves(tab, qapp, start, 40)
    assert counter.calls == {}, counter.calls
    assert {id(item) for item in tab.view.scene().items()} == ids_before, 'no scene item rebuilt'
    assert tab._draft == draft_before, 'the draft is untouched during the drag'
    assert not (tab._recovery_path).exists(), 'no recovery file during the drag'
    delta = _scene_delta(tab, start, pos)
    assert (text.pos() - (text_at + delta)).manhattanLength() < 1e-6, 'the text follows the pointer'
    assert (square.pos() - (square_at + delta)).manhattanLength() < 1e-6, 'its anchor square too'
    assert square.isVisible(), 'the square stays on screen while dragged'
    _release(tab, qapp, pos)


def test_the_release_commits_once_what_a_direct_move_property_gives(tab, qapp, monkeypatch, tmp_path):
    """@brief Identical output: rows, history and the recovery file equal a direct `move_property`."""
    twin = SymbolTab()
    try:
        twin.resize(1400, 900)
        twin.set_data('RS485', deepcopy(ROWS), pad_ids=[r['pin'] for r in ROWS])
        twin_path = tmp_path / 'twin' / '.symbol-draft.json'
        twin_path.parent.mkdir()
        twin.set_recovery_path(twin_path)
        start = _press(tab, qapp)
        origin = tab._property_positions(tab.draft_rows(), tab._symbol)['Reference']
        pos = _moves(tab, qapp, start, 40)
        counter = _Counter(tab, monkeypatch)
        _release(tab, qapp, pos)
        assert counter.calls.get('_changed') == 1 and counter.calls.get('_draw') == 1
        assert counter.calls.get('save_draft') == 1, 'one recovery write, on the drop'
        assert counter.calls.get('_populate') is None
        delta = _scene_delta(tab, start, pos)
        assert twin.move_property('Reference', origin[0] + delta.x() / SCENE_UNITS_PER_MM,
                                  origin[1] - delta.y() / SCENE_UNITS_PER_MM)
        assert tab.draft_rows() == twin.draft_rows()
        assert tab._history == twin._history
        assert tab._history_index == twin._history_index == 1
        chosen = tab.draft_rows()[0]['symbol_properties']['Reference']
        assert all(abs(v / GRID_MM - round(v / GRID_MM)) < 1e-6 for v in chosen), 'snapped on the pin grid'
        assert json.loads(tab._recovery_path.read_text(encoding='utf-8')) == \
            json.loads(twin_path.read_text(encoding='utf-8'))
        assert tab.view.gesture is None and tab._gesture_before is None
    finally:
        twin.deleteLater()


def test_escape_cancels_the_drag_and_puts_the_text_back(tab, qapp):
    """@brief A cancelled drag leaves the rows untouched and redraws the text where it was."""
    before = tab.draft_rows()
    history = deepcopy(tab._history)
    text_at = QPointF(_property_items(tab, 'Reference')[0].pos())
    start = _press(tab, qapp)
    _moves(tab, qapp, start, 20)
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert tab.view.gesture is None
    assert tab.draft_rows() == before
    assert tab._history == history
    assert not tab._recovery_path.exists()
    text, _square = _property_items(tab, 'Reference')
    assert (text.pos() - text_at).manhattanLength() < 1e-6, 'drawn again where it was'


def test_a_redraw_during_the_drag_keeps_the_text_on_the_pointer(tab, qapp):
    """@brief Review (2026-10-17): a keyboard-driven redraw mid-drag must not freeze the text.

    A rename or a shortcut-driven selection while the button is held redraws
    the scene and rebuilds the property items where the drag began; the live
    move resumes on the new items at the last pointer position, the following
    moves still follow and the drop commits the pointer position.
    """
    text_at, square_at = (QPointF(item.pos()) for item in _property_items(tab, 'Reference'))
    origin = tab._property_positions(tab.draft_rows(), tab._symbol)['Reference']
    start = _press(tab, qapp)
    pos = _moves(tab, qapp, start, 20)
    old_text, _old_square = _property_items(tab, 'Reference')
    tab._draw(tab._symbol)
    qapp.processEvents()
    text, square = _property_items(tab, 'Reference')
    assert text is not old_text, 'precondition: the redraw rebuilt the text'
    delta = _scene_delta(tab, start, pos)
    assert (text.pos() - (text_at + delta)).manhattanLength() < 1e-6, 'resumed at the pointer'
    assert (square.pos() - (square_at + delta)).manhattanLength() < 1e-6
    pos = _moves(tab, qapp, pos, 10)
    delta = _scene_delta(tab, start, pos)
    assert (text.pos() - (text_at + delta)).manhattanLength() < 1e-6, 'the new text follows'
    _release(tab, qapp, pos)
    twin = SymbolTab()
    try:
        twin.set_data('RS485', deepcopy(ROWS), pad_ids=[r['pin'] for r in ROWS])
        assert twin.move_property('Reference', origin[0] + delta.x() / SCENE_UNITS_PER_MM,
                                  origin[1] - delta.y() / SCENE_UNITS_PER_MM)
        assert tab.draft_rows() == twin.draft_rows(), 'the drop commits the pointer position'
    finally:
        twin.deleteLater()


def test_a_drop_without_a_move_changes_nothing(tab, qapp):
    """@brief A click on the property (press and release at the same point) is not a move."""
    before = tab.draft_rows()
    start = _press(tab, qapp)
    _release(tab, qapp, start)
    assert tab.draft_rows() == before
    assert all('symbol_properties' not in row for row in tab.draft_rows())


def test_an_edit_outside_a_drag_still_writes_at_once(tab, qapp, monkeypatch):
    """@brief Negative twin: the deferral is the drag's; a direct edit writes draft, history and file now."""
    counter = _Counter(tab, monkeypatch)
    assert tab.move_property('Reference', 10.16, 12.7)
    assert counter.calls.get('_changed') == 1 and counter.calls.get('save_draft') == 1
    assert tab._recovery_path.exists()
    assert tab.draft_rows()[0]['symbol_properties']['Reference'] == [10.16, 12.7]
    assert tab._history_index == 1


def test_a_pin_drag_keeps_committing_its_snapped_steps(tab, qapp, monkeypatch):
    """@brief Negative twin: pins stay on the grid while dragged (KiCad's behaviour), so their steps still commit."""
    placed = tab._symbol.pins[0]
    start = tab.view.mapFromScene(QPointF((placed.x + 1.0) * SCENE_UNITS_PER_MM, -placed.y * SCENE_UNITS_PER_MM))
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    qapp.processEvents()
    assert tab.view.gesture == ('pin', '1')
    counter = _Counter(tab, monkeypatch)
    step = round(2 * GRID_MM * SCENE_UNITS_PER_MM * tab.view.transform().m11())
    pos = start + QPoint(0, -step)
    QTest.mouseMove(tab.view.viewport(), pos)
    qapp.processEvents()
    assert counter.calls.get('_changed') == 1, 'a pin step to another node commits and redraws'
    assert counter.calls.get('save_draft') is None, 'but writes no file before the drop'
    _release(tab, qapp, pos)
    assert counter.calls.get('save_draft') == 1
