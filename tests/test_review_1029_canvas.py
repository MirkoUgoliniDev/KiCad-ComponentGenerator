"""@brief Fixes of the canvas review of 2026-10-29 (findings C1-C7, C9), each with its negative twin.

C1 symbol: a plain click on an element lays nothing out and records nothing
(the press already drew the selection); a null snapped step rebuilds
nothing; every row references ONE graphics list, kept by `copy_rows`.
Negative: a real drag still commits once; unequal lists are left apart.

C2 both tabs: a left press during a middle-button pan starts no drag.
Negative: the same press without a pan drags.

C3 symbol (footprint already): an edit command pressed during a drag
cancels the drag once and then applies. Negative: Ctrl+C copies without
cancelling; a command with nothing to do leaves the drag running.

C4 both tabs: a background click keeps auto-fit; a pan that moves turns it off.

C5 symbol: Escape on a drag restores the rows without rebuilding the pin
table, and writes no recovery file when the restored rows are the ones
already written. Negative: a dropped drag writes once; an external change
of the file makes the next checkpoint write again.

C6 symbol: a left drag from inside the body pans and a click there clears
the selection. Negative: a press on an element inside the body drags it.

C7 both tabs: `cancel_interaction` drops the rubber band and the pan.
Negative: an armed draw tool stays armed.

C9 both tabs: a right click during a middle pan opens no menu. Negative:
the same right click without a pan opens it.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt                    # noqa: E402
from PyQt6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent        # noqa: E402
from PyQt6.QtTest import QTest                                           # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu                          # noqa: E402

from kicadgen.gui import canvas_menus                                    # noqa: E402
from kicadgen.gui import symbol_tab as st                                # noqa: E402
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab, copy_rows  # noqa: E402

NO_MOD = Qt.KeyboardModifier.NoModifier
LEFT = Qt.MouseButton.LeftButton
MID = Qt.MouseButton.MiddleButton
RIGHT = Qt.MouseButton.RightButton
SHIFT = Qt.KeyboardModifier.ShiftModifier
FP_PX = 20.0


# ── Symbol fixtures and helpers ───────────────────────────────────────

def _make_tab(pins=5, shapes=2):
    """A symbol editor with `pins` pins and `shapes` 5.08 mm rectangles left of the body."""
    widget = SymbolTab()
    widget.resize(1200, 800)
    widget.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, pins + 1)],
                    pad_ids=[str(i) for i in range(1, pins + 1)])
    for k in range(shapes):
        x = -40.64 - 7.62 * k
        widget.start_shape('rectangle')
        widget.add_shape_point(x, 20.32)
        widget.add_shape_point(x + 5.08, 25.4)
    widget.view.collector.cancel()
    widget._select_graphic(None)
    return widget


@pytest.fixture
def tab(qapp):
    """A symbol editor with five pins and two drawn rectangles."""
    widget = _make_tab()
    yield widget
    widget.deleteLater()


def _vp(view, x_mm, y_mm):
    return view.mapFromScene(QPointF(x_mm * SCENE_UNITS_PER_MM, -y_mm * SCENE_UNITS_PER_MM))


def _mouse(view, kind, pos, button=LEFT, modifiers=NO_MOD, buttons=None):
    if buttons is None:
        buttons = button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(pos), view.viewport().mapToGlobal(QPointF(pos)), button, buttons, modifiers)
    {QEvent.Type.MouseButtonPress: view.mousePressEvent, QEvent.Type.MouseMove: view.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)


def _press(view, pos, **kw):
    _mouse(view, QEvent.Type.MouseButtonPress, pos, **kw)


def _move(view, pos, **kw):
    kw.setdefault('button', Qt.MouseButton.NoButton)
    _mouse(view, QEvent.Type.MouseMove, pos, **kw)


def _release(view, pos, **kw):
    _mouse(view, QEvent.Type.MouseButtonRelease, pos, **kw)


def _escape(view):
    QApplication.sendEvent(view, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, NO_MOD))


def _background(view):
    pos = _vp(view, 0.0, -60.96)
    assert not view.items(pos)
    return pos


def _shape_pos(view):
    """Inside the first rectangle (a drawn element)."""
    pos = _vp(view, -40.64 + 2.54, 22.86)
    assert any(item.data(0) == ('graphic', 0) for item in view.items(pos))
    return pos


def _body_pos(view):
    """The centre of the body: covered only by the untagged body outline."""
    pos = _vp(view, 0.0, 0.0)
    items = view.items(pos)
    assert items and all(item.data(0) is None for item in items)
    return pos


class _Count:
    """Wrap `owner.name` and count its calls."""

    def __init__(self, monkeypatch, owner, name):
        self.n = 0
        real = getattr(owner, name)

        def wrapper(*args, **kwargs):
            self.n += 1
            return real(*args, **kwargs)
        monkeypatch.setattr(owner, name, wrapper)


# ── C1: a click costs nothing, a null step rebuilds nothing, graphics shared ──

def test_c1_a_click_on_an_element_lays_out_nothing_and_records_nothing(tab, monkeypatch):
    view = tab.view
    pos = _shape_pos(view)
    history = (len(tab._history), tab._history_index)
    rows = tab.draft_rows()
    layout = _Count(monkeypatch, st, 'layout_symbol')
    changed = _Count(monkeypatch, tab, '_changed')
    draws = _Count(monkeypatch, tab, '_draw')
    _press(view, pos)
    _release(view, pos)
    assert view.gesture is None
    assert tab.selected_graphics() == [0], 'the click still selects the element'
    assert layout.n == 0 and changed.n == 0, 'a click lays out and commits nothing'
    assert draws.n == 1, 'only the selection redraw of the press'
    assert (len(tab._history), tab._history_index) == history
    assert tab.draft_rows() == rows


def test_c1_negative_a_real_drag_of_an_element_still_commits_once(tab, monkeypatch):
    view = tab.view
    pos = _shape_pos(view)
    before = tab.graphics()[0]['x']
    history = len(tab._history)
    changed = _Count(monkeypatch, tab, '_changed')
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    _release(view, pos + QPoint(60, 0))
    assert tab.graphics()[0]['x'] > before
    assert len(tab._history) == history + 1, 'one undo step for the drag'
    assert changed.n >= 2, 'the step and the drop committed'


def test_c1_a_move_that_stays_on_the_press_node_rebuilds_nothing(tab, monkeypatch):
    view = tab.view
    pos = _shape_pos(view)
    _press(view, pos)
    layout = _Count(monkeypatch, st, 'layout_symbol')
    draws = _Count(monkeypatch, tab, '_draw')
    _move(view, pos + QPoint(1, 0))    # well under half a drawing-grid step
    assert layout.n == 0 and draws.n == 0
    _escape(view)


def test_c1_negative_a_drag_back_to_its_start_restores_the_initial_rows(tab):
    view = tab.view
    pos = _shape_pos(view)
    rows = tab.draft_rows()
    history = len(tab._history)
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    assert tab.draft_rows() != rows, 'the step committed the move'
    _move(view, pos)
    _release(view, pos)
    assert tab.graphics() == SymbolTab._graphics(tab, rows)
    assert len(tab._history) == history, 'no undo step for a drag that ends where it began'


def test_c1_every_row_references_one_graphics_list(tab):
    rows = tab._draft
    assert len(rows) == 5
    assert all(row['symbol_graphics'] is rows[0]['symbol_graphics'] for row in rows)
    copied = copy_rows(rows)
    assert all(row['symbol_graphics'] is copied[0]['symbol_graphics'] for row in copied)
    assert copied[0]['symbol_graphics'] is not rows[0]['symbol_graphics']
    assert copied == rows


def test_c1_negative_copy_rows_keeps_separate_lists_separate():
    a = [{'id': 'e1', 'x': 1.0}]
    rows = [{'pin': '1', 'symbol_graphics': a}, {'pin': '2', 'symbol_graphics': [{'id': 'e1', 'x': 1.0}]}]
    copied = copy_rows(rows)
    assert copied == rows
    assert copied[0]['symbol_graphics'] is not copied[1]['symbol_graphics']
    copied[0]['symbol_graphics'][0]['x'] = 5.0
    assert copied[1]['symbol_graphics'][0]['x'] == 1.0 and a[0]['x'] == 1.0


def test_c1_rows_loaded_with_one_list_per_row_are_shared_and_saved_per_row(qapp, tmp_path):
    graphics = [{'id': 'r1', 'key': 'shape:rectangle', 'label': 'R', 'source': 'shape', 'x': -40.64,
                 'y': 20.32, 'rotation': 0, 'mirror': False,
                 'primitives': [{'kind': 'rectangle', 'points': [[0, 0], [5.08, 5.08]]}], 'terminals': []}]
    pinout = [{'pin': str(i), 'name': f'P{i}', 'symbol_graphics': json.loads(json.dumps(graphics))}
              for i in range(1, 4)]
    widget = SymbolTab()
    try:
        widget.set_data('Board', pinout, pad_ids=['1', '2', '3'])
        draft = widget._draft
        assert all(row['symbol_graphics'] is draft[0]['symbol_graphics'] for row in draft)
        assert all(row['symbol_graphics'] is widget._baseline[0]['symbol_graphics'] for row in widget._baseline)
        # On disk each row still carries the list (the format every reader expects).
        path = tmp_path / '.symbol-draft.json'
        widget.set_recovery_path(path)
        widget._select_graphic(0)
        widget._edit_selected_graphics('rotate', 90)
        saved = json.loads(path.read_text(encoding='utf-8'))['rows']
        assert len(saved) == 3 and all(row['symbol_graphics'] == saved[0]['symbol_graphics'] for row in saved)
    finally:
        widget.deleteLater()


def test_c1_negative_unequal_graphics_lists_are_not_merged():
    rows = [{'pin': '1', 'symbol_graphics': [{'id': 'a'}]}, {'pin': '2', 'symbol_graphics': [{'id': 'b'}]}]
    SymbolTab._share_graphics(rows)
    assert rows[0]['symbol_graphics'] is not rows[1]['symbol_graphics']
    assert rows[1]['symbol_graphics'] == [{'id': 'b'}]


# ── C2: no drag starts during a middle pan ─────────────────────────────

def test_c2_symbol_a_left_press_on_an_element_during_a_middle_pan_starts_no_drag(tab):
    view = tab.view
    bg = _background(view)
    _press(view, bg, button=MID)
    assert view.camera.pan_active
    _press(view, _shape_pos(view), buttons=LEFT | MID)
    assert view.gesture is None and view.camera.pan_active
    _release(view, bg, button=MID)


def test_c2_symbol_negative_the_same_press_without_a_pan_drags(tab):
    view = tab.view
    _press(view, _shape_pos(view))
    assert view.gesture is not None
    _escape(view)


# ── C3: an edit command during a drag cancels it once, then applies ────

def test_c3_symbol_undo_during_a_drag_cancels_it_then_undoes(tab):
    view = tab.view
    pos = _shape_pos(view)
    index = tab._history_index
    count = len(tab.graphics())
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    ends = []
    tab._gestures.finished.connect(ends.append)
    tab.undo()
    assert view.gesture is None and ends == [False], 'cancelled once, never dropped'
    assert tab._history_index == index - 1
    assert len(tab.graphics()) == count - 1, 'the undo removed the last drawn rectangle'
    _release(view, pos + QPoint(60, 0))
    assert tab._history_index == index - 1, 'the release after the cancel commits nothing'


def test_c3_symbol_delete_during_a_drag_cancels_it_then_deletes(tab):
    view = tab.view
    pos = _shape_pos(view)
    count = len(tab.graphics())
    history = len(tab._history)
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    tab._delete_selected_graphic()
    assert view.gesture is None
    assert len(tab.graphics()) == count - 1
    assert len(tab._history) == history + 1, 'one undo step: the delete, not the cancelled move'
    _release(view, pos + QPoint(60, 0))
    assert len(tab._history) == history + 1


def test_c3_symbol_negative_copy_during_a_drag_keeps_the_drag(tab):
    view = tab.view
    pos = _shape_pos(view)
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    assert tab.copy_selected()
    assert view.gesture is not None, 'Ctrl+C is not an edit: the drag goes on'
    _escape(view)


def test_c3_symbol_negative_a_redo_with_nothing_to_redo_keeps_the_drag(tab):
    view = tab.view
    pos = _shape_pos(view)
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    tab.redo()
    assert view.gesture is not None
    _escape(view)


# ── C4: a background click keeps auto-fit ──────────────────────────────

def test_c4_symbol_a_background_click_keeps_auto_fit(tab):
    view = tab.view
    view.fit()
    assert view.auto_fit
    pos = _background(view)
    _press(view, pos)
    _release(view, pos)
    assert view.auto_fit
    _press(view, pos, button=MID)
    _release(view, pos, button=MID)
    assert view.auto_fit, 'a middle click without a move too'


def test_c4_symbol_negative_a_pan_that_moves_turns_auto_fit_off(tab):
    view = tab.view
    view.fit()
    pos = _background(view)
    _press(view, pos)
    _move(view, pos + QPoint(20, 10), buttons=LEFT)
    _release(view, pos + QPoint(20, 10))
    assert not view.auto_fit


# ── C5: Escape on a drag restores without a table rebuild or a second write ──

@pytest.fixture
def bound(tab, tmp_path, monkeypatch):
    """The symbol tab bound to a recovery file holding its (dirty) draft, with write counters."""
    path = tmp_path / '.symbol-draft.json'
    tab.set_recovery_path(path)
    tab._select_graphic(0)
    tab._edit_selected_graphics('rotate', 90)     # a commit: the draft file is written
    tab._select_graphic(None)
    assert path.is_file()
    from kicadgen.services import symbol_draft
    writes = _Count(monkeypatch, symbol_draft, 'save_draft')
    populate = _Count(monkeypatch, tab, '_populate')
    rebuild = _Count(monkeypatch, tab.table, 'setRowCount')
    return tab, path, writes, populate, rebuild


def test_c5_escape_on_a_drag_rebuilds_no_table_and_writes_nothing(bound):
    tab, path, writes, populate, rebuild = bound
    view = tab.view
    pos = _shape_pos(view)
    rows = tab.draft_rows()
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    assert tab.draft_rows() != rows
    _escape(view)
    assert view.gesture is None
    assert tab.draft_rows() == rows
    assert populate.n == 0 and rebuild.n == 0, 'no pin table rebuild'
    assert writes.n == 0, 'the recovery file already holds these rows'


def test_c5_negative_a_dropped_drag_writes_the_draft_once(bound):
    tab, path, writes, populate, rebuild = bound
    view = tab.view
    pos = _shape_pos(view)
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    _release(view, pos + QPoint(60, 0))
    assert writes.n == 1 and populate.n == 0


def test_c5_negative_a_file_changed_by_someone_else_is_written_again(bound):
    tab, path, writes, populate, rebuild = bound
    view = tab.view
    path.unlink()
    pos = _shape_pos(view)
    _press(view, pos)
    _move(view, pos + QPoint(60, 0))
    _escape(view)
    assert writes.n == 1 and path.is_file(), 'the missing draft is written back'


# ── C6: the inside of the body is background ───────────────────────────

def test_c6_a_left_drag_from_inside_the_body_pans_and_a_click_there_clears(tab):
    view = tab.view
    pos = _body_pos(view)
    before = view.mapToScene(QPoint(0, 0))
    _press(view, pos)
    assert view.camera.pan_active
    _move(view, pos + QPoint(40, 30), buttons=LEFT)
    _release(view, pos + QPoint(40, 30))
    assert view.mapToScene(QPoint(0, 0)) != before
    tab._select_graphic(0)
    _press(view, pos)
    _release(view, pos)
    assert tab.selected_graphics() == []


def test_c6_negative_a_press_on_an_element_drags_it_instead(tab):
    view = tab.view
    pos = _shape_pos(view)
    _press(view, pos)
    assert view.gesture == ('graphic', 0) and not view.camera.pan_active
    _escape(view)


# ── C7 symbol side: cancel_interaction drops band and pan ──────────────

def test_c7_symbol_cancel_interaction_drops_band_and_pan(tab):
    view = tab.view
    bg = _background(view)
    _press(view, bg, modifiers=SHIFT)
    _move(view, bg + QPoint(30, 30), buttons=LEFT, modifiers=SHIFT)
    assert view.band.active
    view.cancel_interaction()
    assert not view.band.active
    _press(view, bg, button=MID)
    assert view.camera.pan_active
    view.cancel_interaction()
    assert not view.camera.pan_active


def test_c7_symbol_negative_cancel_interaction_keeps_an_armed_shape_tool(tab):
    tab.start_shape('rectangle')
    tab.view.cancel_interaction()
    assert tab.view.collector.armed
    tab.view.collector.cancel()


# ── C9 symbol side: no menu during a pan ───────────────────────────────

@pytest.fixture
def no_menus(monkeypatch):
    """Menus that would open return at once (the request signals are what is tested)."""
    monkeypatch.setattr(QMenu, 'exec', lambda self, *args: None)
    monkeypatch.setattr(canvas_menus, 'exec_element_menu', lambda *args, **kwargs: None)


def _context(view, pos):
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, view.viewport().mapToGlobal(pos))
    view.contextMenuEvent(event)


def test_c9_symbol_a_right_click_during_a_middle_pan_opens_no_menu(tab, no_menus):
    view = tab.view
    asked = []
    view.graphic_context_requested.connect(lambda *args: asked.append(args))
    bg = _background(view)
    _press(view, bg, button=MID)
    pos = _shape_pos(view)
    _press(view, pos, button=RIGHT, buttons=RIGHT | MID)
    _context(view, pos)
    assert asked == [] and view.camera.pan_active
    _release(view, bg, button=MID)


def test_c9_symbol_negative_without_a_pan_the_menu_is_asked(tab, no_menus):
    view = tab.view
    asked = []
    view.graphic_context_requested.connect(lambda *args: asked.append(args))
    _context(view, _shape_pos(view))
    assert [args[0] for args in asked] == [0]


# ── Footprint side ─────────────────────────────────────────────────────

@pytest.fixture
def shown(window, qapp, monkeypatch):
    """The preview tab shown with three drawings and a camera at scale 1."""
    pytest.importorskip("pyvistaqt")
    from kicadgen.core.footprint import validate_footprint_drawings
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    window.chk_grid.setChecked(False)

    def record(kind, points):
        return {"kind": kind, "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in points]}

    window.footprint_drawings = validate_footprint_drawings([
        record("line", ((0, -10), (5, -10))),
        record("rectangle", ((8, -11), (10, -9))),
        record("circle", ((15, -10), (16, -10)))])
    window._draw_preview()
    view = window.preview_view
    view.resetTransform()
    view.centerOn(0, 0)
    view.auto_fit = False
    return window


def _fp(window, x_mm, y_mm):
    return window.preview_view.mapFromScene(QPointF(x_mm * FP_PX, y_mm * FP_PX))


def _fp_background(window):
    return _fp(window, 5, 10)


def test_c2_footprint_a_left_press_on_a_drawing_during_a_middle_pan_starts_no_drag(shown):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    bg = _fp_background(w)
    QTest.mousePress(vpt, MID, NO_MOD, bg)
    assert view.camera.pan_active
    pos = _fp(w, 2.5, -10)
    QTest.mousePress(vpt, LEFT, NO_MOD, pos)
    assert not view.gestures.active and view.camera.pan_active
    QTest.mouseRelease(vpt, MID, NO_MOD, bg)
    QTest.mouseMove(vpt, pos + QPoint(40, 0))
    QTest.mouseRelease(vpt, LEFT, NO_MOD, pos + QPoint(40, 0))
    assert w.footprint_drawings[0]["points"] == [[0.0, -10.0], [5.0, -10.0]]
    assert len(w._fp_drawings_undo) == 0


def test_c2_footprint_negative_the_same_press_without_a_pan_drags(shown):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    QTest.mousePress(vpt, LEFT, NO_MOD, _fp(w, 2.5, -10))
    assert view.gestures.active
    view.cancel_interaction()
    # QTest keeps the held buttons per process: release what was pressed, or
    # every later move of the session is sent as a drag (2026-10-29).
    QTest.mouseRelease(vpt, LEFT, NO_MOD, _fp(w, 2.5, -10))


def test_c3_footprint_undo_during_a_drag_cancels_it_then_undoes(shown):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    w._select_footprint_drawing(0)
    w._duplicate_drawings([0])
    count = len(w.footprint_drawings)
    start = _fp(w, 2.5, -10)
    QTest.mousePress(vpt, LEFT, NO_MOD, start)
    QTest.mouseMove(vpt, start + QPoint(40, 0))
    assert view.gestures.active
    w._undo_footprint_drawing()
    assert not view.gestures.active and len(w.footprint_drawings) == count - 1
    QTest.mouseRelease(vpt, LEFT, NO_MOD, start + QPoint(40, 0))


def test_c4_footprint_a_background_click_keeps_auto_fit_and_a_moving_pan_does_not(shown):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    view.auto_fit = True
    pos = _fp_background(w)
    QTest.mouseClick(vpt, LEFT, NO_MOD, pos)
    assert view.auto_fit
    QTest.mousePress(vpt, LEFT, NO_MOD, pos)
    QTest.mouseMove(vpt, pos + QPoint(20, 10))
    QTest.mouseRelease(vpt, LEFT, NO_MOD, pos + QPoint(20, 10))
    assert not view.auto_fit


def test_c7_footprint_cancel_interaction_drops_band_and_pan(shown):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    bg = _fp_background(w)
    QTest.mousePress(vpt, LEFT, SHIFT, bg)
    QTest.mouseMove(vpt, bg + QPoint(30, 30))
    assert view.band.active
    w._toggle_footprint_draw_tool("line")
    assert not view.band.active, 'arming a tool ends the band'
    assert view.click_collector.armed
    QTest.mouseRelease(vpt, LEFT, SHIFT, bg + QPoint(30, 30))
    w._cancel_footprint_drawing()
    QTest.mousePress(vpt, MID, NO_MOD, bg)
    assert view.camera.pan_active
    view.cancel_interaction()
    assert not view.camera.pan_active and view._background_press is None
    # QTest keeps the held buttons per process: release what was pressed, or
    # every later move of the session is sent as a drag (2026-10-29).
    QTest.mouseRelease(vpt, MID, NO_MOD, bg)


def test_c7_footprint_negative_cancel_interaction_keeps_an_armed_draw_tool(shown):
    w = shown
    view = w.preview_view
    w._toggle_footprint_draw_tool("line")
    assert view.click_collector.armed
    view.cancel_interaction()
    assert view.click_collector.armed
    w._cancel_footprint_drawing()


def test_c9_footprint_a_right_click_during_a_middle_pan_opens_no_menu(shown, no_menus):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    asked = []
    view.drawing_context_requested.connect(asked.append)
    bg = _fp_background(w)
    QTest.mousePress(vpt, MID, NO_MOD, bg)
    pos = _fp(w, 2.5, -10)
    view.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), view.viewport().mapToGlobal(QPointF(pos)),
                                    Qt.MouseButton.NoButton, MID, NO_MOD))
    QTest.mousePress(vpt, RIGHT, NO_MOD, pos)
    assert asked == [] and view.camera.pan_active
    QTest.mouseRelease(vpt, MID, NO_MOD, bg)
    # QTest keeps the held buttons per process: release what was pressed, or
    # every later move of the session is sent as a drag (2026-10-29).
    QTest.mouseRelease(vpt, RIGHT, NO_MOD, pos)


def test_c9_footprint_negative_without_a_pan_the_menu_is_asked(shown, no_menus):
    w = shown
    view, vpt = w.preview_view, w.preview_view.viewport()
    asked = []
    view.drawing_context_requested.connect(asked.append)
    pos = _fp(w, 2.5, -10)
    QTest.mouseMove(vpt, pos)
    QTest.mousePress(vpt, RIGHT, NO_MOD, pos)
    assert asked == [0]
    # QTest keeps the held buttons per process: release what was pressed, or
    # every later move of the session is sent as a drag (2026-10-29).
    QTest.mouseRelease(vpt, RIGHT, NO_MOD, pos)
