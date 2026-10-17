"""@brief Symbol editor drags redraw only when a step really changes the draft.

A drag sends one step per mouse move. Most moves stay on the same grid node,
and each of them used to rebuild the layout and redraw the whole scene
(about 40 ms with 80 pins) to show the same picture. The contracts here are
call counts and identical drafts, never wall-clock times:

- steps that land on the node already shown redraw nothing (pin and
  property drags, and a drag that returns to its start);
- a step to another node, an edit of the draft between two steps or a
  status line written by a refused step redraws (negative twins: a cache
  must never hide a change);
- the draft a drag commits is the one a single step to the drop point gives;
- one redraw deep-copies the draft once.
"""

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPointF

from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab

#: Scene units of one 2.54 mm pin-grid step.
STEP = 2.54 * SCENE_UNITS_PER_MM


def _tab(count=16):
    """A symbol tab holding `count` pins named P1, P2, ..."""
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, count + 1)],
                 pad_ids=[str(i) for i in range(1, count + 1)])
    return tab


def _count_redraws(tab, monkeypatch):
    """Wrap `_draw` on the instance; return the list that collects one entry per redraw."""
    calls = []
    original = tab._draw

    def counting(symbol, rows=None):
        calls.append(symbol)
        return original(symbol, rows)
    monkeypatch.setattr(tab, '_draw', counting)
    return calls


def _pin_start(tab, number='1'):
    """Scene point of a pin's outer end."""
    placed = next(p for p in tab._symbol.pins if p.pin.number == number)
    return QPointF(placed.x * SCENE_UNITS_PER_MM, -placed.y * SCENE_UNITS_PER_MM)


def test_pin_drag_steps_on_the_same_node_redraw_once(qapp, monkeypatch):
    """@brief Positive: twenty moves within one grid node after the first step redraw nothing more."""
    tab = _tab()
    try:
        start = _pin_start(tab)
        tab._begin_gesture(('pin', '1'), start)
        target = start + QPointF(0, -2 * STEP)
        tab._move_gesture(('pin', '1'), target)
        redraws = _count_redraws(tab, monkeypatch)
        for offset in range(1, 21):
            tab._move_gesture(('pin', '1'), target + QPointF(offset * 0.25, -offset * 0.25))
        assert redraws == [], 'moves inside the node already shown must not redraw'
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_pin_drag_to_another_node_redraws_and_moves_the_pin(qapp, monkeypatch):
    """@brief Negative: each step that reaches another node redraws and the draft follows it."""
    tab = _tab()
    try:
        start = _pin_start(tab)
        tab._begin_gesture(('pin', '1'), start)
        tab._move_gesture(('pin', '1'), start + QPointF(0, -2 * STEP))
        redraws = _count_redraws(tab, monkeypatch)
        first = next(r for r in tab._draft if str(r['pin']) == '1')['symbol_position']
        tab._move_gesture(('pin', '1'), start + QPointF(0, -4 * STEP))
        second = next(r for r in tab._draft if str(r['pin']) == '1')['symbol_position']
        assert len(redraws) == 1
        assert second != first
        assert second[1] == pytest.approx(first[1] + 2 * 2.54)
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_a_drag_back_to_its_start_redraws_once_then_rests(qapp, monkeypatch):
    """@brief The «no move» path (draft restored to the drag's start) is keyed the same way."""
    tab = _tab()
    try:
        start = _pin_start(tab)
        tab._begin_gesture(('pin', '1'), start)
        tab._move_gesture(('pin', '1'), start + QPointF(0, -2 * STEP))
        redraws = _count_redraws(tab, monkeypatch)
        tab._move_gesture(('pin', '1'), start)
        assert len(redraws) == 1, 'coming back is a real change'
        for offset in range(1, 6):
            tab._move_gesture(('pin', '1'), start + QPointF(offset * 0.5, 0))
        assert len(redraws) == 1, 'resting on the start node redraws nothing more'
        assert tab._draft == tab._gesture_before
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_an_edit_of_the_draft_between_two_equal_steps_is_redrawn(qapp, monkeypatch):
    """@brief Negative: the key holds the draft object, so a draft replaced in between is not hidden."""
    tab = _tab()
    try:
        start = _pin_start(tab)
        tab._begin_gesture(('pin', '1'), start)
        target = start + QPointF(0, -2 * STEP)
        tab._move_gesture(('pin', '1'), target)
        redraws = _count_redraws(tab, monkeypatch)
        tab._draft = [dict(row) for row in tab._draft]
        tab._move_gesture(('pin', '1'), target)
        assert len(redraws) == 1
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_a_status_written_between_two_equal_steps_is_replaced(qapp, monkeypatch):
    """@brief Negative: a refused step's message does not stay on screen behind a skipped step."""
    tab = _tab()
    try:
        start = _pin_start(tab)
        tab._begin_gesture(('pin', '1'), start)
        target = start + QPointF(0, -2 * STEP)
        tab._move_gesture(('pin', '1'), target)
        shown = tab.status.text()
        redraws = _count_redraws(tab, monkeypatch)
        tab.set_status('refused step')
        tab._move_gesture(('pin', '1'), target)
        assert len(redraws) == 1
        assert tab.status.text() == shown
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_property_drag_steps_on_the_same_node_redraw_once(qapp, monkeypatch):
    """@brief Positive: the property drag follows the same rule as the pins."""
    tab = _tab()
    try:
        start = QPointF(0, 0)
        tab._begin_gesture(('property', 'Reference'), start)
        target = start + QPointF(3 * STEP, -STEP)
        tab._move_gesture(('property', 'Reference'), target)
        redraws = _count_redraws(tab, monkeypatch)
        # The same pointer position again: the property's default anchor is
        # off the grid, so a jitter could legitimately cross a node here.
        for _ in range(10):
            tab._move_gesture(('property', 'Reference'), target)
        assert redraws == []
        tab._move_gesture(('property', 'Reference'), target + QPointF(2 * STEP, 0))
        assert len(redraws) == 1, 'another node is redrawn'
        tab._end_gesture(True)
    finally:
        tab.deleteLater()


def test_a_jittery_drag_commits_the_same_draft_as_a_single_step(qapp):
    """@brief Identical output: skipped steps change nothing in what the drag commits."""
    jittery, direct = _tab(), _tab()
    try:
        start = _pin_start(jittery)
        drop = start + QPointF(STEP, -3 * STEP)
        jittery._begin_gesture(('pin', '1'), start)
        for step in range(1, 41):
            fraction = step / 40
            jittery._move_gesture(('pin', '1'), start + (drop - start) * fraction
                                  + QPointF(0.2 * (step % 3), 0))
        jittery._move_gesture(('pin', '1'), drop)
        jittery._end_gesture(True)
        direct._begin_gesture(('pin', '1'), start)
        direct._move_gesture(('pin', '1'), drop)
        direct._end_gesture(True)
        assert jittery.draft_rows() == direct.draft_rows()
        assert jittery._history == direct._history
    finally:
        jittery.deleteLater()
        direct.deleteLater()


def test_one_redraw_copies_the_draft_once(qapp, monkeypatch):
    """@brief A redraw reads the draft once; before, properties and elements copied it three times."""
    tab = _tab()
    try:
        calls = []
        original = tab.draft_rows

        def counting():
            calls.append(1)
            return original()
        monkeypatch.setattr(tab, 'draft_rows', counting)
        tab._draw(tab._symbol)
        assert len(calls) == 1
    finally:
        tab.deleteLater()
