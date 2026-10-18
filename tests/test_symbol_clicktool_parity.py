"""@brief Characterisation of the symbol editor's click tools, pinned before the shared click-tool module.

Plan `docs/canvas-clicktools-plan.md` §4 step 1 (2026-10-18): the wire, the
six shape tools and glyph placing of the symbol editor collect points by
clicking, as the footprint preview's draw mode does. Before the shared
`PointCollector` / `ClickToolRouter` replace that code, every observable
behaviour is recorded here as it is TODAY, through real QTest events on the
view's viewport (press, release, double click, key, leave, hide).

What each test pins:

- the committed rows (`draft_rows`), the history (length AND content) and the
  recovery JSON, compared with a TWIN tab driven through the direct APIs
  (`add_wire_point`, `add_shape_point`, `finish_wire`, `finish_shape`): the
  event path and the programmatic path must give the same committed output;
- the tool state after each step: `view.wiring`, `_wire_points`,
  `_shape_points`, `_shape_tool`, the toolbar button and the palette
  buttons, the status text and the path of the rubber band;
- the per-phase counts of `_draw` (a mouse move while a tool is armed must
  redraw nothing: the rubber band is reshaped in place);
- a golden JSON of one scripted session (`tests/data/clicktool_golden/
  symbol_session.json`), regenerated only with `KICADGEN_REGEN_CLICKTOOL_GOLDEN=1`
  and read independent of line endings.

Where today's behaviour is one of the open differences the plan lists
(decisions C1, C2, C3, C6, C7, C8, C9, C10, C11 and X) the test carries the id
in its NAME and says so in its docstring: it records the value of today, which
phase 2 of the migration flips on purpose (and says why).
"""

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PyQt6.QtGui import QContextMenuEvent, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu

import kicadgen.gui.symbol_tab as symbol_tab_module
import kicadgen.services.symbol_draft as symbol_draft
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab

#: Golden record of the scripted session (`test_golden_session`).
GOLDEN_DIR = Path(__file__).parent / 'data' / 'clicktool_golden'
GOLDEN_SESSION = GOLDEN_DIR / 'symbol_session.json'
#: Set to 1 to rewrite the golden file from the current code.
REGEN_ENV = 'KICADGEN_REGEN_CLICKTOOL_GOLDEN'

#: Five pins, two columns (1-3 left, 4-5 right): the inner ends sit at x = -5.08 / 5.08.
ROWS5 = [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)]

S = SCENE_UNITS_PER_MM
LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton
MIDDLE = Qt.MouseButton.MiddleButton
NO_MOD = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier

#: The shapes with a fixed click count, with a valid set of clicks (symbol mm, all on the 0.254 mm grid).
FIXED_SHAPES = {
    'rectangle': [(-2.54, -2.54), (2.54, 2.54)],
    'circle': [(0.0, 0.0), (2.54, 0.0)],
    'arc': [(-2.54, 0.0), (2.54, 0.0), (0.0, 2.54)],
    'bezier': [(-5.08, 0.0), (-2.54, 5.08), (2.54, -5.08), (5.08, 0.0)],
}
#: The primitive kind each shape commits.
SHAPE_KIND = {'rectangle': 'rectangle', 'circle': 'circle', 'arc': 'arc', 'bezier': 'bezier', 'line': 'polyline'}


def _settle(qapp, times=10):
    """@brief Process pending events until the layout, the fit and the deferred redraws are done."""
    for _ in range(times):
        qapp.processEvents()


def _build(qapp, tmp_path, folder=''):
    """@brief A shown five-pin symbol tab with a recovery file bound under `tmp_path/folder`."""
    tab = SymbolTab()
    tab.resize(1400, 900)
    tab.set_data('Board', deepcopy(ROWS5), pad_ids=[r['pin'] for r in ROWS5])
    path = tmp_path / folder / '.symbol-draft.json' if folder else tmp_path / '.symbol-draft.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tab.set_recovery_path(path)
    tab.show()
    _settle(qapp)
    return tab


@pytest.fixture
def tab(qapp, tmp_path):
    """@brief The five-pin tab the events are sent to."""
    tab = _build(qapp, tmp_path)
    yield tab
    tab.deleteLater()


@pytest.fixture
def twin(qapp, tmp_path):
    """@brief A second five-pin tab, driven by the direct APIs only."""
    tab = _build(qapp, tmp_path, 'twin')
    yield tab
    tab.deleteLater()


class _Counter:
    """@brief Counts the tab methods and module functions a phase runs."""

    def __init__(self, tab, monkeypatch):
        self.calls = {}
        for name in ('_changed', '_draw', '_accept_geometry'):
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
        """@brief The counts since the last call, then cleared."""
        calls, self.calls = self.calls, {}
        return calls


# ── Event helpers ───────────────────────────────────────────────────────────

def _vp(tab, x_mm, y_mm):
    """@brief Viewport point of a symbol point (mm, Y up)."""
    return tab.view.mapFromScene(QPointF(x_mm * S, -y_mm * S))


def _click(tab, qapp, x_mm, y_mm, button=LEFT, modifier=NO_MOD):
    """@brief Press and release `button` at a symbol point (one real click)."""
    QTest.mouseClick(tab.view.viewport(), button, modifier, _vp(tab, x_mm, y_mm))
    qapp.processEvents()


def _press(tab, qapp, pos, button=LEFT, modifier=NO_MOD):
    QTest.mousePress(tab.view.viewport(), button, modifier, pos)
    qapp.processEvents()


def _release(tab, qapp, pos, button=LEFT, modifier=NO_MOD):
    QTest.mouseRelease(tab.view.viewport(), button, modifier, pos)
    qapp.processEvents()


def _move(tab, qapp, pos):
    QTest.mouseMove(tab.view.viewport(), pos)
    qapp.processEvents()


def _send(tab, qapp, kind, pos, button=LEFT):
    """@brief Send one explicit mouse event of `kind` to the viewport (a double click without its press)."""
    viewport = tab.view.viewport()
    buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(pos), QPointF(viewport.mapToGlobal(pos)), button, buttons, NO_MOD)
    QApplication.sendEvent(viewport, event)
    qapp.processEvents()


def _double_click(tab, qapp, pos):
    """@brief What Windows delivers for a double click: press, release, double click, release.

    `QTest.mouseDClick` sends the double-click event alone (see
    `test_qtest_double_click_sends_only_the_double_click_event`), so the real
    sequence is built from explicit events.
    """
    _send(tab, qapp, QEvent.Type.MouseButtonPress, pos)
    _send(tab, qapp, QEvent.Type.MouseButtonRelease, pos)
    _send(tab, qapp, QEvent.Type.MouseButtonDblClick, pos)
    _send(tab, qapp, QEvent.Type.MouseButtonRelease, pos)


def _key(tab, qapp, key):
    QTest.keyClick(tab.view, key)
    qapp.processEvents()


def _leave(tab, qapp):
    """@brief Deliver the Leave event to the view (the offscreen platform sends none by itself)."""
    QApplication.sendEvent(tab.view, QEvent(QEvent.Type.Leave))
    qapp.processEvents()


def _recovery(tab):
    """@brief The recovery JSON, or None when the file does not exist."""
    path = tab._recovery_path
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def _anonymised(tab, value):
    """@brief `value` with every element id of `tab` replaced by the element's index (ids are random per tab)."""
    ids = {record['id']: f'<element {index}>' for index, record in enumerate(tab.graphics()) if record.get('id')}
    if isinstance(value, dict):
        return {key: _anonymised(tab, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_anonymised(tab, item) for item in value]
    if isinstance(value, tuple):
        return [_anonymised(tab, item) for item in value]
    if isinstance(value, str):
        return ids.get(value, value)
    return value


def _band(tab):
    """@brief The rubber band path in symbol mm, or None when there is no live band."""
    item = tab._wire_cursor_item
    if item is None or sip.isdeleted(item):
        return None
    path = item.path()
    return [[round(path.elementAt(i).x / S, 3), round(-path.elementAt(i).y / S, 3)]
            for i in range(path.elementCount())]


def _tool_state(tab):
    """@brief Everything a user can see of the armed tool: flags, points, buttons, status, band."""
    return {
        'wiring': tab.view.wiring,
        'placing': tab.view.placing,
        'wire_points': _anonymised(tab, tab._wire_points),
        'shape_points': _anonymised(tab, tab._shape_points),
        'shape_tool': tab._shape_tool,
        'wire_button': tab.wire_button.defaultAction().isChecked(),
        'palette': sorted(key for key, action in tab.palette._tool_actions.items() if action.isChecked()),
        'glyph_checked': any(action.isChecked() for action in tab.palette_actions()),
        'status': tab.status.text(),
        'band': _band(tab),
    }


def _committed(tab):
    """@brief What the editor committed: rows, history and recovery write, ids anonymised."""
    return {'rows': _anonymised(tab, tab.draft_rows()), 'history': _anonymised(tab, tab._history),
            'history_index': tab._history_index, 'recovery': _anonymised(tab, _recovery(tab)),
            'graphics': _anonymised(tab, tab.graphics())}


def _last_points(tab, index=-1):
    """@brief The points of the first primitive of a committed graphic."""
    return tab.graphics()[index]['primitives'][0]['points']


# ── Scripts: the same steps through events and through the direct API ───────

def _play_events(tab, qapp, steps):
    """@brief Run a script through real mouse and key events.

    Steps: ('click', x, y), ('shift_click', x, y), ('dbl', x, y) (a real
    double click: press, release, double click, release), ('right', x, y),
    ('enter',), ('esc',), ('start_wire',), ('start_shape', key).
    """
    for step in steps:
        kind = step[0]
        if kind == 'click':
            _click(tab, qapp, step[1], step[2])
        elif kind == 'shift_click':
            _click(tab, qapp, step[1], step[2], modifier=SHIFT)
        elif kind == 'dbl':
            _double_click(tab, qapp, _vp(tab, step[1], step[2]))
        elif kind == 'right':
            _click(tab, qapp, step[1], step[2], button=RIGHT)
        elif kind == 'enter':
            _key(tab, qapp, Qt.Key.Key_Return)
        elif kind == 'esc':
            _key(tab, qapp, Qt.Key.Key_Escape)
        elif kind == 'start_wire':
            tab.start_wire()
        elif kind == 'start_shape':
            tab.start_shape(step[1])
        else:
            raise AssertionError(step)


def _play_api(tab, steps):
    """@brief Run the same script through the direct API (`add_*_point`, `finish_*`): the twin oracle."""
    for step in steps:
        kind = step[0]
        if kind in ('click', 'shift_click', 'dbl'):
            if tab._shape_tool is not None:
                tab.add_shape_point(step[1], step[2])
            else:
                tab.add_wire_point(step[1], step[2], free=kind == 'shift_click')
            if kind == 'dbl':
                tab.finish_wire(True)
        elif kind in ('right', 'enter'):
            tab.finish_wire(True)
        elif kind == 'esc':
            tab.finish_wire(False)
        elif kind == 'start_wire':
            tab.start_wire()
        elif kind == 'start_shape':
            tab.start_shape(step[1])
        else:
            raise AssertionError(step)


def _assert_same(tab, twin):
    """@brief The event-driven tab and the API-driven twin agree on the committed output and the tool state."""
    assert _committed(tab) == _committed(twin)
    state, other = _tool_state(tab), _tool_state(twin)
    for key in ('wiring', 'wire_points', 'shape_points', 'shape_tool', 'wire_button', 'palette', 'status'):
        assert state[key] == other[key], key


def _run_both(tab, twin, qapp, steps):
    """@brief Play `steps` through events on `tab` and through the API on `twin`, then compare."""
    _play_events(tab, qapp, steps)
    _play_api(twin, steps)
    _assert_same(tab, twin)


# ── Wire ────────────────────────────────────────────────────────────────────

def test_wire_clicks_project_on_the_longer_axis_and_enter_commits_one_polyline(tab, twin, qapp):
    """@brief Wire: orthogonal mode projects a free click on the longer axis; Enter commits; the tool is one-shot.

    Pinned contract (plan §1 S13): a left press adds the snapped point, a
    click that is not aligned with the last becomes its projection (here
    the target (-2.54, 2.54) from (-7.62, 5.08) gives (-2.54, 5.08)); Enter
    finishes: one history entry, the wire selected, the tool released (wiring
    off, button off), status "Filo inserito.".
    """
    steps = [('start_wire',), ('click', -7.62, 5.08), ('click', -2.54, 2.54), ('click', -2.54, 0.0)]
    _play_events(tab, qapp, steps)
    assert tab.view.wiring and tab.wire_button.defaultAction().isChecked()
    assert tab._wire_points == [[-7.62, 5.08], [-2.54, 5.08], [-2.54, 0.0]]
    assert tab.graphics() == [] and len(tab._history) == 1
    _key(tab, qapp, Qt.Key.Key_Return)
    assert not tab.view.wiring and not tab.wire_button.defaultAction().isChecked()
    assert 'wire' not in _tool_state(tab)['palette'] and tab._wire_points == []
    assert _last_points(tab) == [[-7.62, 5.08], [-2.54, 5.08], [-2.54, 0.0]]
    assert len(tab._history) == 2 and tab._selected_graphic == 0
    assert tab.status.text() == 'Filo inserito.'
    _play_api(twin, steps + [('enter',)])
    _assert_same(tab, twin)
    # Negative: one-shot, a further click does not add a wire.
    _click(tab, qapp, 0.0, 0.0)
    assert len(tab.graphics()) == 1 and tab._wire_points == []


def test_wire_end_on_a_pin_is_stored_anchored_with_the_corner_before_it(tab, twin, qapp):
    """@brief Wire: a click within the snap tolerance of a pin end is anchored; an unaligned anchor gets a corner first."""
    steps = [('start_wire',), ('click', -5.08, 2.54), ('click', 5.08, 0.0), ('enter',)]
    _run_both(tab, twin, qapp, steps)
    points = _last_points(tab)
    assert [p['anchor']['number'] for p in points] == ['1', '2', '5'], \
        'the corner (-5.08, 0) falls exactly on the end of pin 2 and is anchored too'
    assert all(p['anchor']['kind'] == 'pin' for p in points)
    # Negative: a click away from every pin end is a plain [x, y] point.
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    assert tab._wire_points == [[-7.62, 5.08]]


def test_shift_frees_the_segment_from_the_orthogonal_constraint(tab, twin, qapp):
    """@brief Wire: Shift on the press keeps a diagonal segment; without it the same click is projected."""
    steps = [('start_wire',), ('click', -7.62, 5.08), ('shift_click', -2.54, 2.54)]
    _play_events(tab, qapp, steps)
    assert tab._wire_points == [[-7.62, 5.08], [-2.54, 2.54]]
    _play_api(twin, steps)
    assert twin._wire_points == tab._wire_points
    # Negative: without Shift the target is projected.
    tab.finish_wire(False)
    _play_events(tab, qapp, [('start_wire',), ('click', -7.62, 5.08), ('click', -2.54, 2.54)])
    assert tab._wire_points == [[-7.62, 5.08], [-2.54, 5.08]]


def test_c3_a_repeated_wire_or_shape_point_is_refused_on_the_status_line(tab, twin, qapp, monkeypatch):
    """@brief C3 (flipped: was "refused silently"): a click equal to the last point changes nothing but says so.

    Positive: the point list and the redraw count are untouched and the
    status line reads "Punto coincidente con il precedente." (the footprint's
    toast text). Negative twins: a click elsewhere is taken and redraws once;
    a double click on the last vertex is quiet (see C2); with an invalid
    draft the click is ignored without a message.
    """
    message = 'Punto coincidente con il precedente.'
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    assert tab.status.text() != message
    counter = _Counter(tab, monkeypatch)
    _click(tab, qapp, -7.62, 5.08)
    assert tab._wire_points == [[-7.62, 5.08]] and tab.status.text() == message
    assert counter.take() == {}, 'no redraw for a refused point'
    _click(tab, qapp, -2.54, 5.08)
    assert len(tab._wire_points) == 2 and counter.take() == {'_draw': 1}
    tab.finish_wire(False)
    tab.start_shape('line')
    _click(tab, qapp, 0.0, 0.0)
    assert tab.status.text() != message
    _click(tab, qapp, 0.0, 0.0)
    assert tab._shape_points == [(0.0, 0.0)] and tab.status.text() == message
    # The direct API reports the same refusal.
    twin.start_shape('line')
    assert twin.add_shape_point(0.0, 0.0) and not twin.add_shape_point(0.0, 0.0)
    assert twin.status.text() == message
    # Negative: an invalid draft ignores the click silently.
    tab.table.item(0, 1).setText('VCC\tGND')
    tab.set_status('')
    _click(tab, qapp, 0.0, 0.0)
    assert tab.status.text() == ''


@pytest.mark.parametrize('finisher', ['right', 'enter', 'dbl'])
def test_wire_finishes_by_right_click_enter_or_double_click(tab, twin, qapp, finisher):
    """@brief Wire: each finishing gesture commits the same polyline; the double click adds its own first press.

    The harness double click is press, release, double click, release: the
    first press adds a point (C2 for the wire: here it is a vertex), the
    double-click event itself adds none.
    """
    last = ('right', -2.54, 5.08) if finisher == 'right' else (finisher, -2.54, 5.08)
    steps = [('start_wire',), ('click', -7.62, 5.08), last]
    if finisher == 'enter':
        steps = [('start_wire',), ('click', -7.62, 5.08), ('click', -2.54, 5.08), ('enter',)]
    if finisher == 'right':
        steps = [('start_wire',), ('click', -7.62, 5.08), ('click', -2.54, 5.08), ('right', -2.54, 5.08)]
    _run_both(tab, twin, qapp, steps)
    assert _last_points(tab) == [[-7.62, 5.08], [-2.54, 5.08]]
    assert not tab.view.wiring and len(tab._history) == 2 and tab._wire_points == []


def test_c2_a_double_click_event_adds_its_point_quietly_then_finishes(tab, qapp):
    """@brief C2 (flipped: was "the bare double-click event finishes without a point"): the point under it is taken, THEN the tool finishes.

    Positive: a bare double-click event at a new vertex commits it as the last
    point. Negative twin: a double click on the LAST vertex (a genuine
    double click) adds nothing, shows no refusal and still finishes with the
    points it has. In both, the release after it adds nothing.
    """
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    _click(tab, qapp, -2.54, 5.08)
    _send(tab, qapp, QEvent.Type.MouseButtonDblClick, _vp(tab, 5.08, 5.08))
    assert _last_points(tab) == [[-7.62, 5.08], [-2.54, 5.08], [5.08, 5.08]]
    assert not tab.view.wiring and tab.status.text() == 'Filo inserito.'
    _send(tab, qapp, QEvent.Type.MouseButtonRelease, _vp(tab, 5.08, 5.08))
    assert len(tab.graphics()) == 1 and tab._wire_points == []
    tab.start_wire()
    _click(tab, qapp, -7.62, 0.0)
    _click(tab, qapp, -2.54, 0.0)
    status = tab.status.text()
    _send(tab, qapp, QEvent.Type.MouseButtonDblClick, _vp(tab, -2.54, 0.0))
    assert _last_points(tab, -1) == [[-7.62, 0.0], [-2.54, 0.0]] and len(tab.graphics()) == 2
    assert tab.status.text() == 'Filo inserito.' != status, 'finished, with no refusal message in between'


def test_qtest_double_click_sends_only_the_double_click_event(tab, qapp):
    """@brief Documents why the harness builds the double click from explicit events (`_double_click`)."""
    seen = []

    class Spy(QObject):
        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                                QEvent.Type.MouseButtonDblClick):
                seen.append(event.type())
            return False

    spy = Spy()
    tab.view.viewport().installEventFilter(spy)
    QTest.mouseDClick(tab.view.viewport(), LEFT, NO_MOD, QPoint(5, 5))
    qapp.processEvents()
    tab.view.viewport().removeEventFilter(spy)
    assert QEvent.Type.MouseButtonDblClick in seen and QEvent.Type.MouseButtonPress not in seen


def test_escape_abandons_the_wire_and_releases_the_tool(tab, twin, qapp):
    """@brief Wire: Escape drops the points, commits nothing, unchecks the button and clears the status."""
    steps = [('start_wire',), ('click', -7.62, 5.08), ('click', -2.54, 5.08), ('esc',)]
    _run_both(tab, twin, qapp, steps)
    assert not tab.view.wiring and tab._wire_points == [] and tab.graphics() == []
    assert len(tab._history) == 1 and tab.status.text() == '' and _band(tab) is None
    assert not tab.wire_button.defaultAction().isChecked()
    # Negative twin: with nothing armed, Escape changes nothing.
    _key(tab, qapp, Qt.Key.Key_Escape)
    assert tab.graphics() == [] and len(tab._history) == 1


@pytest.mark.parametrize('finisher', ['enter', 'right'])
def test_a_wire_with_fewer_than_two_points_is_abandoned_not_committed(tab, twin, qapp, finisher):
    """@brief Wire: finishing with 0 or 1 points commits nothing and releases the tool (`finish_wire`)."""
    for clicks in ([], [('click', -7.62, 5.08)]):
        steps = [('start_wire',)] + clicks + [(finisher, -2.54, 5.08) if finisher == 'right' else ('enter',)]
        _run_both(tab, twin, qapp, steps)
        assert tab.graphics() == [] and len(tab._history) == 1
        assert not tab.view.wiring and tab._wire_points == [] and tab.status.text() == ''
        assert not tab.wire_button.defaultAction().isChecked()
    assert tab.finish_wire(True) is False


def test_moving_while_a_tool_is_armed_redraws_nothing_and_reshapes_the_band_in_place(tab, qapp, monkeypatch):
    """@brief Perf contract: zero `_draw` per mouse move; the rubber band follows the cursor with the click's constraint.

    The wire band runs from the last point to the point(s) a click would
    append (corner included); the shape band is the outline the shape would
    have through the cursor.
    """
    counter = _Counter(tab, monkeypatch)
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    counter.take()
    assert _band(tab) == [], 'a lone move-to: the band is an empty path until the pointer moves'
    for i, (x, y) in enumerate([(-5.08, 5.08), (-2.54, 5.08), (-2.54, 2.54), (0.0, 0.0)]):
        _move(tab, qapp, _vp(tab, x, y) + QPoint(0, i % 2))
    assert counter.take() == {}, 'no redraw while the pointer moves'
    assert _band(tab) == [[-7.62, 5.08], [0.0, 5.08]], 'orthogonal: projected on the longer axis'
    tab.finish_wire(False)
    tab.start_shape('rectangle')
    _click(tab, qapp, -2.54, -2.54)
    counter.take()
    for x, y in [(0.0, 0.0), (2.54, 0.0), (2.54, 2.54)]:
        _move(tab, qapp, _vp(tab, x, y))
    assert counter.take() == {}
    assert _band(tab) == [[-2.54, -2.54], [2.54, -2.54], [2.54, 2.54], [-2.54, 2.54], [-2.54, -2.54]]


def test_clicks_are_ignored_while_the_draft_is_invalid_and_the_points_survive(tab, qapp):
    """@brief Wire and shapes: an invalid draft keeps the tool armed and its points; clicks are not taken.

    Negative twin: once the name is fixed the same click is taken and the
    band is back.
    """
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    tab.table.item(0, 1).setText('VCC\tGND')
    assert tab._symbol is None
    _click(tab, qapp, -2.54, 5.08)
    assert tab._wire_points == [[-7.62, 5.08]] and tab.view.wiring
    _move(tab, qapp, QPoint(40, 40))
    tab.table.item(0, 1).setText('VCC')
    assert tab._symbol is not None and _band(tab) == []
    _click(tab, qapp, -2.54, 5.08)
    assert len(tab._wire_points) == 2
    tab.finish_wire(False)
    tab.start_shape('rectangle')
    _click(tab, qapp, 0.0, 0.0)
    tab.table.item(0, 1).setText('VCC\tGND')
    _click(tab, qapp, 2.54, 2.54)
    assert tab._shape_points == [(0.0, 0.0)] and tab._shape_tool == 'rectangle' and tab.view.wiring
    tab.table.item(0, 1).setText('VCC')
    _click(tab, qapp, 2.54, 2.54)
    assert len(tab.graphics()) == 1


# ── Shape tools ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('key', sorted(FIXED_SHAPES))
def test_a_fixed_count_shape_finishes_at_its_last_click_and_the_tool_persists(tab, twin, qapp, key):
    """@brief Shapes: the last of N clicks commits; the tool stays armed for the next shape (plan C5 «keep»).

    Positive: rectangle 2, circle 2, arc 3, bezier 4 clicks commit one
    graphic each, the tool, its palette button and `view.wiring` stay on and
    the status says "Forma inserita."; a second shape is a second history
    entry. Negative: Escape releases the tool, and the next click draws
    nothing.
    """
    clicks = FIXED_SHAPES[key]
    steps = [('start_shape', key)] + [('click', x, y) for x, y in clicks]
    _play_events(tab, qapp, steps[:-1])
    assert len(tab.graphics()) == 0 and len(tab._shape_points) == len(clicks) - 1
    _play_events(tab, qapp, steps[-1:])
    assert len(tab.graphics()) == 1 and tab.graphics()[0]['primitives'][0]['kind'] == SHAPE_KIND[key]
    assert tab.shape_tool() == key and tab.view.wiring and tab._shape_points == []
    assert key in _tool_state(tab)['palette'] and tab.status.text().startswith('Forma inserita.')
    assert len(tab._history) == 2
    _play_api(twin, steps)
    _assert_same(tab, twin)
    _play_events(tab, qapp, steps[1:])
    _play_api(twin, steps[1:])
    assert len(tab.graphics()) == 2 and len(tab._history) == 3
    _assert_same(tab, twin)
    _key(tab, qapp, Qt.Key.Key_Escape)
    assert tab.shape_tool() is None and not tab.view.wiring and _tool_state(tab)['palette'] == []
    assert tab.status.text() == ''
    before = tab.draft_rows()
    _click(tab, qapp, 0.0, 0.0)
    assert tab.draft_rows() == before


@pytest.mark.parametrize('finisher', ['right', 'enter', 'dbl'])
def test_a_line_finishes_by_right_click_enter_or_double_click_and_persists(tab, twin, qapp, finisher):
    """@brief Line: three clicks then a finishing gesture commit one polyline; the tool stays armed.

    The harness double click adds its own first press as the last vertex.
    """
    base = [('start_shape', 'line'), ('click', -5.08, 0.0), ('click', 0.0, 2.54)]
    steps = base + ([('click', 5.08, 0.0), ('enter',)] if finisher == 'enter' else
                    [('click', 5.08, 0.0), ('right', 5.08, 0.0)] if finisher == 'right' else
                    [('dbl', 5.08, 0.0)])
    _run_both(tab, twin, qapp, steps)
    assert _last_points(tab) == [[-5.08, 0.0], [0.0, 2.54], [5.08, 0.0]] or len(_last_points(tab)) == 3
    assert len(tab.graphics()) == 1 and tab.shape_tool() == 'line' and tab.view.wiring
    assert tab._shape_points == [] and len(tab._history) == 2


def test_a_line_back_onto_its_first_point_closes_a_polygon_only_from_four_points(tab, twin, qapp):
    """@brief Line: the click that returns to the first point closes the shape when four points are known.

    Positive: A, B, C, then A commits a closed triangle at once. Negative
    twin: A, B, A (the third point only) does not close: three points stay
    collected.
    """
    ring = [(0.0, 0.0), (5.08, 0.0), (5.08, 5.08), (0.0, 0.0)]
    steps = [('start_shape', 'line')] + [('click', x, y) for x, y in ring]
    _run_both(tab, twin, qapp, steps)
    assert len(tab.graphics()) == 1 and tab._shape_points == []
    points = _last_points(tab)
    assert len(points) == 4 and points[0] == points[-1], 'closed: first point repeated'
    _key(tab, qapp, Qt.Key.Key_Escape)
    tab.start_shape('line')
    for x, y in [(0.0, 0.0), (5.08, 0.0), (0.0, 0.0)]:
        _click(tab, qapp, x, y)
    assert len(tab.graphics()) == 1 and len(tab._shape_points) == 3 and tab.view.wiring


def test_text_is_one_shot_and_a_cancelled_or_invalid_prompt_leaves_the_tool_armed(tab, twin, qapp):
    """@brief Text: one click, the prompt, a committed text, and the tool is released (the only one-shot shape).

    Negatives: a cancelled prompt and an invalid answer both leave the
    tool armed, points cleared, nothing committed.
    """
    answers = [None, ('', 1.27), ('Hi', 1.27)]
    tab._text_prompt = lambda *args: answers.pop(0)
    tab.start_shape('text')
    _click(tab, qapp, 0.0, 0.0)
    assert tab.shape_tool() == 'text' and tab.view.wiring and tab._shape_points == [] and tab.graphics() == []
    _click(tab, qapp, 0.0, 0.0)
    assert tab.shape_tool() == 'text' and tab.view.wiring and tab.graphics() == []
    assert tab.status.text().startswith('Testo non valido')
    _click(tab, qapp, 0.0, 0.0)
    assert len(tab.graphics()) == 1 and tab.graphics()[0]['primitives'][0]['kind'] == 'text'
    assert tab.shape_tool() is None and not tab.view.wiring and _tool_state(tab)['palette'] == []
    assert tab.status.text().startswith('Testo inserito')
    assert len(tab._history) == 2
    twin._text_prompt = lambda *args: ('Hi', 1.27)
    twin.start_shape('text')
    twin.add_shape_point(0.0, 0.0)
    assert _committed(tab)['graphics'] == _committed(twin)['graphics'] and tab._history_index == twin._history_index


def test_a_degenerate_shape_says_so_and_leaves_the_tool_armed_with_the_points_cleared(tab, twin, qapp):
    """@brief Shapes: a rectangle of zero width is refused ("Forma non valida"), the tool stays armed, points cleared."""
    steps = [('start_shape', 'rectangle'), ('click', 0.0, 0.0), ('click', 0.0, 2.54)]
    _run_both(tab, twin, qapp, steps)
    assert tab.graphics() == [] and len(tab._history) == 1
    assert tab.status.text().startswith('Forma non valida')
    assert tab.shape_tool() == 'rectangle' and tab.view.wiring and tab._shape_points == []
    # Negative twin: the next valid pair of clicks is a rectangle.
    _play_events(tab, qapp, [('click', -2.54, -2.54), ('click', 2.54, 2.54)])
    assert len(tab.graphics()) == 1


def test_c4_today_a_right_click_with_no_points_on_a_shape_is_invalid_but_stays_armed_on_a_wire_it_releases(tab, qapp):
    """@brief C4 (kept): finishing with no points: a shape tool says "Forma non valida" and stays armed; the wire releases."""
    tab.start_shape('rectangle')
    _click(tab, qapp, 0.0, 0.0, button=RIGHT)
    assert tab.shape_tool() == 'rectangle' and tab.view.wiring and tab.status.text().startswith('Forma non valida')
    _key(tab, qapp, Qt.Key.Key_Return)
    assert tab.shape_tool() == 'rectangle' and tab.view.wiring
    tab.start_wire()
    _click(tab, qapp, 0.0, 0.0, button=RIGHT)
    assert not tab.view.wiring and not tab.wire_button.defaultAction().isChecked() and tab.status.text() == ''


def test_escape_abandons_a_shape_in_progress(tab, twin, qapp):
    """@brief Shapes: Escape with points collected drops them, commits nothing, releases the tool."""
    steps = [('start_shape', 'arc'), ('click', -2.54, 0.0), ('click', 2.54, 0.0), ('esc',)]
    _run_both(tab, twin, qapp, steps)
    assert tab.graphics() == [] and tab.shape_tool() is None and not tab.view.wiring
    assert tab._shape_points == [] and _band(tab) is None and len(tab._history) == 1


def test_a_shape_does_not_anchor_to_a_pin_but_snaps_to_the_drawing_grid(tab, qapp):
    """@brief Shape points are drawings, not connections: a click on a pin end is a plain grid point; the wire anchors."""
    tab.start_shape('line')
    _click(tab, qapp, -5.08, 2.54)
    assert tab._shape_points == [(-5.08, 2.54)]
    tab.finish_shape(False)
    tab.start_wire()
    _click(tab, qapp, -5.08, 2.54)
    assert isinstance(tab._wire_points[0], dict)


# ── Open differences recorded as of today ───────────────────────────────────

def test_c1_a_drag_draws_a_shape_but_never_adds_a_wire_vertex(tab, qapp):
    """@brief C1 (flipped: was "press-move-release adds only the press point"): the shape tools take the release point of a drag.

    Positive: press at A, move, release at B draws the rectangle; a circle
    and a line drag likewise leave two points. Negatives: the WIRE ignores
    the release (a polyline would get a second vertex per drag); a release
    at the press, or within the drag distance, adds nothing; a press that
    completes a shape does not start the next one by dragging.
    """
    # The camera refits after a commit: the points are mapped when they are used.
    def A():
        return _vp(tab, -2.54, -2.54)

    def B():
        return _vp(tab, 2.54, 2.54)

    def arm(key):
        tab.start_shape(key)
        _settle(qapp)

    arm('rectangle')
    _press(tab, qapp, A())
    _move(tab, qapp, B())
    _release(tab, qapp, B())
    assert len(tab.graphics()) == 1 and tab.graphics()[0]['primitives'][0]['kind'] == 'rectangle'
    assert tab._shape_points == [] and tab.shape_tool() == 'rectangle'
    arm('circle')
    _press(tab, qapp, A())
    _release(tab, qapp, B())
    assert len(tab.graphics()) == 2 and tab.graphics()[1]['primitives'][0]['kind'] == 'circle'
    arm('line')
    _press(tab, qapp, A())
    _release(tab, qapp, B())
    assert tab._shape_points == [(-2.54, -2.54), (2.54, 2.54)] and len(tab.graphics()) == 2
    tab.finish_shape(False)
    # Negative: a release at the press (or a tiny jitter) is only the press point.
    arm('rectangle')
    _press(tab, qapp, A())
    _release(tab, qapp, A() + QPoint(1, 1))
    assert tab._shape_points == [(-2.54, -2.54)] and len(tab.graphics()) == 2
    # Negative: a press that completes the shape does not start another by dragging.
    _press(tab, qapp, B())
    assert len(tab.graphics()) == 3 and tab._shape_points == []
    _settle(qapp)
    _release(tab, qapp, A())
    assert len(tab.graphics()) == 3 and tab._shape_points == []
    tab.finish_shape(False)
    # Negative: the wire takes the press point only.
    tab.start_wire()
    _settle(qapp)
    _press(tab, qapp, A())
    _move(tab, qapp, B())
    _release(tab, qapp, B())
    assert tab._wire_points == [[-2.54, -2.54]]


def test_c2_two_fast_clicks_on_a_rectangle_make_the_rectangle(tab, qapp):
    """@brief C2 (flipped: was "ONE point and Forma non valida"): the second press of a fast pair arrives as a double click.

    Click at A, then a double-click event at the neighbouring node B (what
    Windows delivers for two quick clicks): the point under the double click
    is taken THEN the tool finishes, so the rectangle is committed from its
    two corners and the persistent tool stays armed; the release after it
    adds nothing. Negative twin: the same pair for a LINE (open-ended) gives
    a two-point line.
    """
    tab.start_shape('rectangle')
    a, b = _vp(tab, 0.0, 0.0), _vp(tab, 2.54, 2.54)
    _send(tab, qapp, QEvent.Type.MouseButtonPress, a)
    _send(tab, qapp, QEvent.Type.MouseButtonRelease, a)
    assert tab._shape_points == [(0.0, 0.0)]
    _send(tab, qapp, QEvent.Type.MouseButtonDblClick, b)
    assert len(tab.graphics()) == 1 and tab._shape_points == []
    assert tab.graphics()[0]['primitives'][0]['kind'] == 'rectangle'
    assert tab.shape_tool() == 'rectangle' and tab.view.wiring and tab.status.text().startswith('Forma inserita.')
    _send(tab, qapp, QEvent.Type.MouseButtonRelease, b)
    assert len(tab.graphics()) == 1 and tab._shape_points == [] and len(tab._history) == 2
    _key(tab, qapp, Qt.Key.Key_Escape)
    tab.start_shape('line')
    _send(tab, qapp, QEvent.Type.MouseButtonPress, a)
    _send(tab, qapp, QEvent.Type.MouseButtonRelease, a)
    _send(tab, qapp, QEvent.Type.MouseButtonDblClick, b)
    assert len(tab.graphics()) == 2 and len(_last_points(tab)) == 2


def test_c6_leaving_the_view_clears_the_rubber_band_and_the_next_move_restores_it(tab, qapp):
    """@brief C6 (flipped: was "the band stays where it was"): the Leave event clears the preview; the points stay.

    Positive: after the leave the band is just its start point (no segment to
    the cursor), the collected points and the tool are untouched; negative
    twin: the next move reshapes the band as before, and a leave with no tool
    armed changes nothing.
    """
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    _move(tab, qapp, _vp(tab, -2.54, 5.08))
    assert _band(tab) == [[-7.62, 5.08], [-2.54, 5.08]]
    emitted = []
    tab.view.pointer_left.connect(lambda: emitted.append(True))
    _leave(tab, qapp)
    assert emitted == [True]
    assert _band(tab) == [[-7.62, 5.08]], 'no ghost segment while the pointer is outside'
    assert tab._wire_points == [[-7.62, 5.08]] and tab.view.wiring
    _move(tab, qapp, _vp(tab, 0.0, 5.08))
    assert _band(tab) == [[-7.62, 5.08], [0.0, 5.08]]
    tab.finish_wire(False)
    _leave(tab, qapp)
    assert _tool_state(tab)['band'] is None and not tab.view.wiring


@pytest.mark.parametrize('tool', ['wire', 'rectangle'])
def test_c7_hiding_the_view_cancels_the_armed_tool_and_its_points(tab, qapp, tool):
    """@brief C7 (flipped: was "the tool and its points survive a hide"): a hide (a tab switch) cancels the tool, as it cancels a drag or a pan.

    Positive: after the hide the tool is released (flags, buttons, status,
    band) and the points are gone, nothing committed; a click after the show
    does not draw. Negative twin: a hide with no tool armed changes nothing
    (the status stays), and a pan running before the hide is cancelled too.
    """
    tab.start_wire() if tool == 'wire' else tab.start_shape(tool)
    _click(tab, qapp, -2.54, 0.0)
    assert tab._wire_points or tab._shape_points
    tab.hide()
    qapp.processEvents()
    tab.show()
    _settle(qapp)
    after = _tool_state(tab)
    assert not after['wiring'] and after['wire_points'] == [] and after['shape_points'] == []
    assert after['shape_tool'] is None and after['palette'] == [] and not after['wire_button']
    assert after['status'] == '' and after['band'] is None
    assert tab.graphics() == [] and len(tab._history) == 1
    before = tab.draft_rows()
    _click(tab, qapp, 2.54, 2.54)
    assert tab.draft_rows() == before and tab._wire_points == [] and tab._shape_points == []
    # Negative twin: nothing armed, the hide leaves the status alone.
    tab.set_status('keep me')
    tab.hide()
    qapp.processEvents()
    tab.show()
    _settle(qapp)
    assert tab.status.text() == 'keep me'
    # A running middle pan does not survive the hide either.
    _press(tab, qapp, QPoint(300, 300), button=MIDDLE)
    assert tab.view.camera.pan_active
    tab.hide()
    qapp.processEvents()
    tab.show()
    _settle(qapp)
    assert not tab.view.camera.pan_active


def test_c8_escape_during_a_middle_pan_cancels_the_pan_and_the_wire_survives(tab, qapp):
    """@brief C8 (flipped: was "Escape abandons the wire and the pan goes on"): the pan takes the key; the tool survives.

    Positive: the first Escape ends the pan only (the wire keeps its point and
    the tool stays armed); negative twin: a second Escape, with no pan, abandons the wire.
    """
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    _press(tab, qapp, QPoint(300, 300), button=MIDDLE)
    _move(tab, qapp, QPoint(310, 305))
    assert tab.view.camera.pan_active
    _key(tab, qapp, Qt.Key.Key_Escape)
    assert not tab.view.camera.pan_active
    assert tab.view.wiring and tab._wire_points == [[-7.62, 5.08]]
    assert tab.view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor, 'the cross cursor is back after the pan'
    _release(tab, qapp, QPoint(310, 305), button=MIDDLE)
    _key(tab, qapp, Qt.Key.Key_Escape)
    assert not tab.view.wiring and tab._wire_points == [] and tab.status.text() == ''


def test_c9_a_left_press_during_a_middle_pan_adds_no_point(tab, qapp):
    """@brief C9 (flipped: was "the press adds a point to the armed wire"): a press during a pan is swallowed.

    Positive: the left press and its release add no point and leave the pan
    running. Negative twin: once the pan is over, the same press adds a point.
    """
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    _press(tab, qapp, QPoint(300, 300), button=MIDDLE)
    assert tab.view.camera.pan_active
    pos = _vp(tab, -2.54, 5.08)
    _press(tab, qapp, pos)
    _release(tab, qapp, pos)
    assert tab._wire_points == [[-7.62, 5.08]] and tab.view.camera.pan_active
    _release(tab, qapp, QPoint(300, 300), button=MIDDLE)
    assert not tab.view.camera.pan_active
    pos = _vp(tab, -2.54, 5.08)
    _press(tab, qapp, pos)
    _release(tab, qapp, pos)
    assert len(tab._wire_points) == 2


@pytest.mark.parametrize('how', ['escape', 'right', 'enter'])
def test_c10_arming_a_tool_sets_the_cross_cursor_and_ending_it_unsets_it(tab, qapp, how):
    """@brief C10 (flipped: was "no cursor change"): the click tools show the cross cursor on the viewport while armed.

    Positive: `start_wire` and `start_shape` set the cross cursor on the
    viewport; Escape, a right click and Enter (any end of the tool) put the
    arrow back. Negative: an idle view keeps the arrow, and a shape tool that
    STAYS armed after a committed shape keeps the cross.
    """
    arrow, cross = Qt.CursorShape.ArrowCursor, Qt.CursorShape.CrossCursor
    assert tab.view.viewport().cursor().shape() == arrow
    for arm in (tab.start_wire, lambda: tab.start_shape('line')):
        arm()
        assert tab.view.viewport().cursor().shape() == cross
        if how == 'escape':
            _key(tab, qapp, Qt.Key.Key_Escape)
        elif how == 'right':
            _click(tab, qapp, 0.0, 0.0, button=RIGHT)
            if tab.shape_tool():
                tab.finish_shape(False)
        else:
            _key(tab, qapp, Qt.Key.Key_Return)
            if tab.shape_tool():
                tab.finish_shape(False)
        assert tab.view.viewport().cursor().shape() == arrow
    tab.start_shape('rectangle')
    _click(tab, qapp, -2.54, -2.54)
    _click(tab, qapp, 2.54, 2.54)
    assert len(tab.graphics()) == 1 and tab.shape_tool() == 'rectangle'
    assert tab.view.wiring and tab.view.viewport().cursor().shape() != arrow, \
        'a persistent shape tool stays armed and keeps a tool cursor (the cross, or the item cursor under the pointer)'


@pytest.mark.parametrize('target', ['graphic', 'pin'])
def test_x_the_right_press_that_finishes_a_wire_does_not_open_a_context_menu(tab, qapp, monkeypatch, target):
    """@brief X (flipped: was "the pin or element menu opens"): the context event of the finishing right press is swallowed.

    The wire is finished by the press; the context event arrives before the
    event loop turns, with the pointer still over the new element (or a pin).
    Positive: no menu is asked. Negatives: the next right press (no tool
    armed) over the same place DOES ask for its menu; a keyboard-reason
    context event right after the press is not swallowed; over nothing no menu is asked.
    """
    monkeypatch.setattr(QMenu, 'exec', lambda self, *args: None)
    asked = []
    tab.view.graphic_context_requested.connect(lambda index, _at: asked.append(('graphic', index)))
    tab.view.pin_context_requested.connect(lambda number, _at: asked.append(('pin', number)))
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    _click(tab, qapp, -2.54, 5.08)
    viewport = tab.view.viewport()
    pos = _vp(tab, -5.08, 5.08) if target == 'graphic' else _vp(tab, -5.08 - 1.0, 2.54)

    def context(reason=QContextMenuEvent.Reason.Mouse):
        QApplication.sendEvent(viewport, QContextMenuEvent(reason, pos, viewport.mapToGlobal(pos)))

    QTest.mousePress(viewport, RIGHT, NO_MOD, pos)
    assert not tab.view.wiring and len(tab.graphics()) == 1, 'the press finished the wire'
    QTest.mouseRelease(viewport, RIGHT, NO_MOD, pos)
    context()
    assert asked == [], 'no menu for the press that finished the wire'
    _settle(qapp)    # the camera refits after the commit: the element has moved on screen
    pos = _vp(tab, -5.08, 5.08) if target == 'graphic' else _vp(tab, -5.08 - 1.0, 2.54)
    # Negative: a new right press with no tool armed asks for the menu of what is under it.
    QTest.mousePress(viewport, RIGHT, NO_MOD, pos)
    QTest.mouseRelease(viewport, RIGHT, NO_MOD, pos)
    context()
    assert asked and asked[0][0] == target, asked
    # Negative: the keyboard menu key is never swallowed by an earlier tool press.
    asked.clear()
    tab.start_wire()
    _click(tab, qapp, 0.0, 0.0, button=RIGHT)
    context(QContextMenuEvent.Reason.Keyboard)
    assert asked and asked[0][0] == target, asked
    asked.clear()
    empty = QPoint(5, 5)
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, empty,
                                                       viewport.mapToGlobal(empty)))
    assert asked == []


def test_c11_arming_glyph_placement_releases_an_armed_wire_and_the_press_places_the_glyph(tab, qapp):
    """@brief C11 (flipped: was "the wire stays armed and wins the press"): one armed tool at a time.

    Positive: `_start_placing` disarms the wire (flags, button, points) and
    the next left press places the glyph. Negative twin: arming a wire
    afterwards turns placing off again.
    """
    tab.start_wire()
    _click(tab, qapp, -7.62, 5.08)
    tab._start_placing()
    assert not tab.view.wiring and tab.view.placing and tab._wire_points == []
    assert not tab.wire_button.defaultAction().isChecked()
    _click(tab, qapp, 2.6, 5.0)
    assert not tab.view.placing and len(tab.graphics()) == 1 and tab.graphics()[0]['x'] == 2.54
    tab._start_placing()
    tab.start_wire()
    assert tab.view.wiring and not tab.view.placing


# ── Glyph placing ───────────────────────────────────────────────────────────

def test_placing_a_glyph_left_click_places_it_snapped_and_is_one_shot(tab, twin, qapp):
    """@brief Placing: a left click places the chosen glyph on the snapped point; the palette button is released."""
    tab._start_placing()
    assert tab.view.placing and tab.status.text().startswith('Clicca')
    _click(tab, qapp, 2.6, 5.0)
    assert not tab.view.placing and len(tab.graphics()) == 1 and not _tool_state(tab)['glyph_checked']
    assert (tab.graphics()[0]['x'], tab.graphics()[0]['y']) == (2.54, 5.08)
    assert len(tab._history) == 2 and tab._selected_graphic == 0
    twin._start_placing()
    twin.view.place_requested.emit(QPointF(2.6 * S, -5.0 * S))
    assert _committed(tab)['graphics'] == _committed(twin)['graphics']
    # Negative: one-shot, the next click places nothing.
    _click(tab, qapp, 0.0, 0.0)
    assert len(tab.graphics()) == 1


@pytest.mark.parametrize('how', ['right', 'escape'])
def test_placing_a_glyph_is_cancelled_by_right_click_or_escape(tab, qapp, how):
    """@brief Placing: right click and Escape cancel; nothing is placed, the status is cleared.

    Negative twin: Enter while placing is ignored (C12): placing stays on.
    """
    tab._start_placing()
    _key(tab, qapp, Qt.Key.Key_Return)
    assert tab.view.placing, 'C12: Enter is ignored'
    if how == 'right':
        _click(tab, qapp, 2.54, 5.08, button=RIGHT)
    else:
        _key(tab, qapp, Qt.Key.Key_Escape)
    assert not tab.view.placing and tab.graphics() == [] and len(tab._history) == 1
    assert tab.status.text() == '' and not _tool_state(tab)['glyph_checked']


# ── Golden session ──────────────────────────────────────────────────────────

def _snapshot(tab, counter, label):
    """@brief One golden record: committed output, tool state and counts after a step."""
    record = {'step': label, 'counts': counter.take(), 'tool': _tool_state(tab),
              'rows': _anonymised(tab, tab.draft_rows()), 'history': len(tab._history),
              'history_index': tab._history_index, 'recovery': _recovery(tab) is not None,
              'selected_graphic': tab._selected_graphic}
    return record


def test_golden_session(tab, qapp, monkeypatch):
    """@brief One scripted session (wire, rectangle twice, polygon, text, degenerate, Escape) recorded step by step.

    Regenerate with `KICADGEN_REGEN_CLICKTOOL_GOLDEN=1` only when a contract
    changed on purpose, and say why in the commit. Millimetres and counts, no pixels.
    """
    counter = _Counter(tab, monkeypatch)
    tab._text_prompt = lambda *args: ('T1', 1.27)
    records = []

    def record(label):
        records.append(_snapshot(tab, counter, label))

    record('rest')
    tab.start_wire()
    record('arm wire')
    _click(tab, qapp, -7.62, 5.08)
    record('wire click 1')
    _move(tab, qapp, _vp(tab, -2.54, 2.54))
    record('wire hover')
    _click(tab, qapp, -2.54, 2.54)
    record('wire click 2 (projected)')
    _click(tab, qapp, -2.54, 2.54)
    record('wire duplicate click')
    _click(tab, qapp, 0.0, 0.0, modifier=SHIFT)
    record('wire shift click')
    _click(tab, qapp, 0.0, 0.0, button=RIGHT)
    record('wire right click')
    tab.start_shape('rectangle')
    record('arm rectangle')
    _click(tab, qapp, 2.54, -2.54)
    _click(tab, qapp, 7.62, 2.54)
    record('rectangle 1')
    _click(tab, qapp, -2.54, -5.08)
    _move(tab, qapp, _vp(tab, 2.54, -2.54))
    record('rectangle 2 first corner and hover')
    _click(tab, qapp, 2.54, -2.54)
    record('rectangle 2')
    _click(tab, qapp, 2.54, 2.54)
    _click(tab, qapp, 2.54, 2.54)
    record('rectangle degenerate (duplicate)')
    _key(tab, qapp, Qt.Key.Key_Escape)
    record('escape')
    tab.start_shape('line')
    for x, y in [(0.0, 5.08), (5.08, 5.08), (5.08, 7.62), (0.0, 5.08)]:
        _click(tab, qapp, x, y)
    record('polygon closed')
    _key(tab, qapp, Qt.Key.Key_Escape)
    tab.start_shape('text')
    _click(tab, qapp, -5.08, -5.08)
    record('text')
    # Flipped 2026-10-18 (phase D, golden regenerated): undo clears the element
    # selection; was "selected_graphic": 4, the stale index of the undone text.
    tab.undo()
    record('undo')
    text = json.dumps(records, indent=1, sort_keys=True, ensure_ascii=False) + '\n'
    if os.environ.get(REGEN_ENV) == '1':
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        GOLDEN_SESSION.write_text(text, encoding='utf-8', newline='\n')
    assert GOLDEN_SESSION.exists(), f'missing golden file: run once with {REGEN_ENV}=1'
    assert _golden_equals(GOLDEN_SESSION, text), \
        f'the session differs from the golden record (regenerate with {REGEN_ENV}=1 only for a wanted contract change)'


def _golden_equals(path, text):
    """@brief Whether the golden file at `path` holds `text`, whatever line endings the checkout gave it.

    @param path Golden file.
    @param text Expected content, LF line endings.
    @return True when the parsed JSON is equal and the text is equal after CRLF is normalised to LF.
    """
    raw = path.read_bytes().replace(b'\r\n', b'\n')
    return raw == text.encode('utf-8') and json.loads(raw.decode('utf-8')) == json.loads(text)


def test_golden_comparison_ignores_line_endings_but_not_content(tmp_path):
    """@brief Positive: a CRLF copy of a golden still matches; negative: a changed value does not."""
    text = json.dumps([{'rows': [1, 2], 'name': 'a'}], indent=1, sort_keys=True) + '\n'
    lf = tmp_path / 'lf.json'
    lf.write_bytes(text.encode('utf-8'))
    crlf = tmp_path / 'crlf.json'
    crlf.write_bytes(text.replace('\n', '\r\n').encode('utf-8'))
    assert _golden_equals(lf, text) and _golden_equals(crlf, text)
    changed = tmp_path / 'changed.json'
    changed.write_bytes(text.replace('"a"', '"b"').replace('\n', '\r\n').encode('utf-8'))
    assert not _golden_equals(changed, text)


def test_a_tool_ended_over_an_element_leaves_no_cross_cursor_behind(tab, qapp):
    """@brief QGraphicsView restores the item's "original" cursor on leaving it; that must not be the tool's cross.

    Positive: the wire ends (Escape) while the pointer is over a shape with its
    own cursor; after moving to empty space the cursor is not the CrossCursor.
    Negative twin: while the tool IS armed, empty space keeps the CrossCursor.
    """
    tab.start_shape('rectangle')
    _click(tab, qapp, -2.54, -2.54)
    _click(tab, qapp, 2.54, 2.54)
    tab.finish_shape(False)
    _settle(qapp)
    viewport = tab.view.viewport()
    tab.view.setMouseTracking(True)
    tab.start_wire()
    _settle(qapp)
    _move(tab, qapp, _vp(tab, 8, 8))
    assert viewport.cursor().shape() == Qt.CursorShape.CrossCursor
    _move(tab, qapp, _vp(tab, 2.54, 0))
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    _move(tab, qapp, _vp(tab, 8, 8))
    assert viewport.cursor().shape() != Qt.CursorShape.CrossCursor
