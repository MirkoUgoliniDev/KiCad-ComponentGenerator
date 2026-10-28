"""@brief Parity of the symbol editor's element menu and clipboard (plan `docs/canvas-menus-plan.md`, phase C).

Written against the menu built by hand in `SymbolTab._graphic_context_menu`
BEFORE it was moved onto the shared `kicadgen.gui.canvas_menus`: the words,
order, separators, enabled states, icons and the operation each entry runs
must stay identical, except decision C-M1 («Specchia» greyed out on a single
text element), flipped deliberately below.

The menu is driven without blocking: `QMenu.exec` is replaced by a fake that
records the menu's structure and "chooses" an entry by triggering it and
returning it, which works both for a menu wired through `triggered` signals
and for one dispatched from the returned action.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QMenu

from kicadgen.generators import symbol_glyphs as g
from kicadgen.gui.symbol_tab import FILL_LABELS, GRID_MM, STYLE_LABELS, SymbolTab

ROT_CCW = 'Ruota di 90 gradi (antiorario)'
ROT_CW = 'Ruota di 90 gradi (orario)'
MIRROR = 'Specchia'
DELETE = 'Elimina elemento'
COPY = 'Copia (Ctrl+C)'
DUPLICATE = 'Duplica (Ctrl+D)'
PROPERTIES = 'Proprietà elemento…'
FIT = 'Adatta il rettangolo del corpo all’elemento'


def _tab():
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 6)],
                 pad_ids=['1', '2', '3', '4', '5'])
    return tab


@pytest.fixture
def board(qapp, glyph_library):
    """A tab holding, in order: a glyph with a body hint, a rectangle, a text, a wire.

    The glyph comes from the suite's own symbol library (`glyph_library`):
    without KiCad the built-in catalogue has no glyph with a body hint and
    this fixture errored on the GitHub runner (CI review 2026-10-27, I2).
    """
    tab = _tab()
    hinted = next(glyph.key for glyph in g.catalogue() if glyph.body_hint is not None)
    assert tab.place_glyph(hinted, 30.48, 30.48)
    tab.start_shape('rectangle')
    tab.add_shape_point(-30.48, 20.32)
    tab.add_shape_point(-25.4, 25.4)
    tab.view.collector.cancel()
    tab._text_prompt = lambda *args: ('IN+', 1.27)
    assert tab.start_shape('text')
    tab.add_shape_point(-30.48, -30.48)
    pin_end = g.pin_inner_ends(tab._symbol)['1']
    tab.start_wire()
    tab.add_wire_point(*pin_end)
    tab.add_wire_point(pin_end[0] + 2.54, pin_end[1])
    assert tab.finish_wire(True)
    graphics = tab.graphics()
    assert len(graphics) == 4
    assert graphics[1]['primitives'][0]['kind'] == 'rectangle'
    assert graphics[2]['primitives'][0]['kind'] == 'text'
    assert g.is_wire(graphics[3])
    yield tab
    tab.deleteLater()


def _describe(menu):
    """Structure of a menu: '-' for a separator, else (text, enabled, icon shown, checked or None, children)."""
    rows = []
    for action in menu.actions():
        if action.isSeparator():
            rows.append('-')
            continue
        icon = not action.icon().isNull() and action.isIconVisibleInMenu()
        checked = action.isChecked() if action.isCheckable() else None
        children = _describe(action.menu()) if action.menu() is not None else None
        rows.append((action.text(), action.isEnabled(), icon, checked, children))
    return rows


def _find(menu, path):
    """The action reached by following the texts in `path` through submenus."""
    for depth, text in enumerate(path):
        action = next(a for a in menu.actions() if not a.isSeparator() and a.text() == text)
        if depth == len(path) - 1:
            return action
        menu = action.menu()
    raise AssertionError(path)


def _open(tab, index, monkeypatch, choose=None):
    """Open the element menu of `index`; return its structure, choosing `choose` (a text path) if given."""
    seen = {}

    def fake_exec(menu, *args):
        seen['rows'] = _describe(menu)
        seen['tooltips'] = menu.toolTipsVisible()
        if choose is None:
            return None
        action = _find(menu, choose)
        assert action.isEnabled(), choose
        action.trigger()
        return action

    monkeypatch.setattr(QMenu, 'exec', fake_exec)
    tab._graphic_context_menu(index, QPoint(10, 10))
    return seen


def _record(tab, monkeypatch):
    """Replace every operation the menu can run by a recorder; return the call log."""
    calls = []
    for name in ('_edit_graphic', '_edit_selected_graphics', 'copy_selected', 'duplicate_selected',
                 '_edit_element_properties', 'edit_text', '_ask_stroke_width', 'fit_body_to_graphic'):
        monkeypatch.setattr(tab, name, lambda *a, _n=name, **k: calls.append((_n, a, k)))
    return calls


def _style_rows(record):
    first = (record.get('primitives') or [{}])[0]
    fill = str(first.get('fill', 'none'))
    style = str(first.get('style', 'default'))
    return [('Riempimento', True, False, None, [(label, True, False, mode == fill, None) for mode, label in FILL_LABELS]),
            ('Tipo di linea', True, False, None, [(label, True, False, mode == style, None) for mode, label in STYLE_LABELS]),
            ('Spessore del tratto…', True, False, None, None)]


def _head(title, rotate=True, mirror_enabled=True):
    rows = [(title, False, False, None, None), '-']
    if rotate:
        rows += [(ROT_CCW, True, True, None, None), (ROT_CW, True, True, None, None),
                 (MIRROR, mirror_enabled, True, None, None)]
    rows += [(DELETE, True, True, None, None), (COPY, True, False, None, None), (DUPLICATE, True, False, None, None)]
    return rows


# ── Structure ──────────────────────────────────────────────────────────────

def test_glyph_menu_has_title_turns_style_entries_and_body_fit(board, monkeypatch):
    record = board.graphics()[0]
    seen = _open(board, 0, monkeypatch)
    assert seen['rows'] == (_head(record['label']) + [(PROPERTIES, True, False, None, None), '-']
                            + _style_rows(record) + [(FIT, True, False, None, None)])
    assert seen['tooltips'] is True
    assert board.selected_graphics() == [0], 'the right-clicked element is selected'


def test_shape_menu_has_style_entries_and_no_body_fit(board, monkeypatch):
    record = board.graphics()[1]
    seen = _open(board, 1, monkeypatch)
    assert seen['rows'] == (_head(record['label']) + [(PROPERTIES, True, False, None, None), '-']
                            + _style_rows(record))


def test_text_menu_greys_out_mirror_and_offers_text_edit(board, monkeypatch):
    """@brief A single text: «Specchia» shown disabled (decision C-M1), then «Modifica testo…».

    Flipped 2026-10-18 (C-M1): was `(MIRROR, True, ...)` — the entry was
    enabled although mirroring a symbol text never had a visible effect (the
    text's origin is its anchor and KiCad never mirrors glyphs).
    """
    record = board.graphics()[2]
    seen = _open(board, 2, monkeypatch)
    assert seen['rows'] == (_head(record['label'], mirror_enabled=False)
                            + [(PROPERTIES, True, False, None, None), '-', ('Modifica testo…', True, False, None, None)])


def test_a_shape_keeps_mirror_enabled(board, monkeypatch):
    """@brief Negative twin of C-M1: an element a mirror changes keeps the entry enabled."""
    for index in (0, 1):
        rows = _open(board, index, monkeypatch)['rows']
        assert (MIRROR, True, True, None, None) in rows, index


def test_wire_menu_offers_no_turn_mirror_or_style(board, monkeypatch):
    record = board.graphics()[3]
    seen = _open(board, 3, monkeypatch)
    assert seen['rows'] == _head(record['label'], rotate=False) + [(PROPERTIES, True, False, None, None)]


def _keep_group(tab, monkeypatch, group):
    """Select `group` and keep it through the menu's own `_select_graphic(index)` call.

    The right-click today collapses the selection to the clicked element
    before building the menu, so the several-element menu is reached only
    when that call keeps the group (it is what this helper simulates).
    """
    tab._selected_graphics = set(group)
    tab._selected_graphic = max(group)
    monkeypatch.setattr(tab, '_select_graphic', lambda *args, **kwargs: None)


def test_a_right_click_on_a_group_member_keeps_the_group(board, monkeypatch):
    """@brief Flipped 2026-10-18 (user: «fixa tutto»): was "a right click on a group member collapses the selection".

    Positive: right-clicking a member of a two-element selection keeps both
    selected and opens the «2 elementi selezionati» menu, as the pin menu does.
    Negative twin: `test_a_right_click_outside_the_group_selects_only_that_element`.
    """
    board._selected_graphics = {1, 2}
    board._selected_graphic = 2
    seen = _open(board, 1, monkeypatch)
    assert seen['rows'] == _head('2 elementi selezionati')
    assert board.selected_graphics() == [1, 2]


def test_a_right_click_outside_the_group_selects_only_that_element(board, monkeypatch):
    """@brief Negative twin: an element that is not selected becomes the only selection, single menu."""
    board._selected_graphics = {1, 2}
    board._selected_graphic = 2
    seen = _open(board, 0, monkeypatch)
    assert seen['rows'][0][0] == board.graphics()[0]['label']
    assert board.selected_graphics() == [0]


def test_several_elements_menu_stops_after_duplicate_and_keeps_mirror_with_a_text(board, monkeypatch):
    """@brief Negative twin of C-M1: a multi-selection holding a text keeps «Specchia» enabled."""
    _keep_group(board, monkeypatch, {1, 2})
    seen = _open(board, 2, monkeypatch)
    assert seen['rows'] == _head('2 elementi selezionati')


def test_several_elements_menu_holding_a_wire_still_turns(board, monkeypatch):
    _keep_group(board, monkeypatch, {0, 3})
    seen = _open(board, 3, monkeypatch)
    assert seen['rows'] == _head('2 elementi selezionati')


def test_an_unknown_index_opens_no_menu(board, monkeypatch):
    seen = _open(board, 9, monkeypatch)
    assert seen == {}


# ── Dispatch ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('index, path, expected', [
    (0, (ROT_CCW,), ('_edit_graphic', (0, 'rotate', 90))),
    (0, (ROT_CW,), ('_edit_graphic', (0, 'rotate', -90))),
    (0, (MIRROR,), ('_edit_graphic', (0, 'mirror', None))),
    (0, (DELETE,), ('_edit_graphic', (0, 'delete', None))),
    (0, (COPY,), ('copy_selected', ())),
    (0, (DUPLICATE,), ('duplicate_selected', ())),
    (0, (PROPERTIES,), ('_edit_element_properties', (0,))),
    (0, (FIT,), ('fit_body_to_graphic', (0,))),
    (1, ('Riempimento', 'Sfondo'), ('_edit_graphic', (1, 'fill', 'background'))),
    (1, ('Riempimento', 'Nessuno'), ('_edit_graphic', (1, 'fill', 'none'))),
    (1, ('Tipo di linea', 'Tratteggiata'), ('_edit_graphic', (1, 'style', 'dash'))),
    (1, ('Spessore del tratto…',), ('_ask_stroke_width', (1,))),
    (2, ('Modifica testo…',), ('edit_text', (2,))),
    (2, (PROPERTIES,), ('_edit_element_properties', (2,))),
    (3, (DELETE,), ('_edit_graphic', (3, 'delete', None))),
])
def test_each_single_element_entry_runs_its_operation_once(board, monkeypatch, index, path, expected):
    calls = _record(board, monkeypatch)
    _open(board, index, monkeypatch, choose=path)
    assert [(name, args) for name, args, _ in calls] == [expected]


@pytest.mark.parametrize('path, expected', [
    ((ROT_CCW,), ('_edit_selected_graphics', ('rotate', 90))),
    ((ROT_CW,), ('_edit_selected_graphics', ('rotate', -90))),
    ((MIRROR,), ('_edit_selected_graphics', ('mirror', None))),
    ((DELETE,), ('_edit_selected_graphics', ('delete', None))),
    ((COPY,), ('copy_selected', ())),
    ((DUPLICATE,), ('duplicate_selected', ())),
])
def test_each_several_elements_entry_runs_its_group_operation_once(board, monkeypatch, path, expected):
    _keep_group(board, monkeypatch, {0, 1})
    calls = _record(board, monkeypatch)
    _open(board, 1, monkeypatch, choose=path)
    assert [(name, args) for name, args, _ in calls] == [expected]


def test_a_dismissed_menu_runs_nothing(board, monkeypatch):
    calls = _record(board, monkeypatch)
    _open(board, 0, monkeypatch)
    assert calls == []


def test_the_entries_really_edit_the_drawing(board, monkeypatch):
    """@brief End to end, no recorder: rotate a glyph from the menu, then delete it (one undo step each)."""
    _open(board, 0, monkeypatch, choose=(ROT_CCW,))
    assert board.graphics()[0]['rotation'] == 90
    _open(board, 0, monkeypatch, choose=(DELETE,))
    assert len(board.graphics()) == 3
    board.undo()
    assert len(board.graphics()) == 4 and board.graphics()[0]['rotation'] == 90


# ── Clipboard ──────────────────────────────────────────────────────────────

def _at(tab, index):
    record = tab.graphics()[index]
    return record['x'], record['y']


def test_paste_offset_grows_only_after_a_committed_paste(board, monkeypatch):
    board._select_graphic(1)
    x, y = _at(board, 1)
    assert board.copy_selected() == 1
    refuse = {'left': 1}
    accept = board._accept_geometry

    def once_refused(rows):
        if refuse['left']:
            refuse['left'] -= 1
            return False
        return accept(rows)

    monkeypatch.setattr(board, '_accept_geometry', once_refused)
    assert board.paste() is False and len(board.graphics()) == 4, 'a refused paste places nothing'
    assert board.paste() and _at(board, 4) == (round(x + GRID_MM, 4), round(y - GRID_MM, 4)), \
        'the refused paste did not move the next one'
    assert board.paste() and _at(board, 5) == (round(x + 2 * GRID_MM, 4), round(y - 2 * GRID_MM, 4))
    assert board.graphics()[5]['id'] not in {r['id'] for r in board.graphics()[:5]}, 'a paste gets a new id'


def test_duplicate_keeps_the_previous_clipboard_and_its_offset(board):
    board._select_graphic(1)
    x, y = _at(board, 1)
    assert board.copy_selected() == 1 and board.paste()
    board._select_graphic(2)
    assert board.duplicate_selected() and board.selected_graphics() == [5]
    assert board.graphics()[5]['primitives'][0]['kind'] == 'text', 'the duplicate is the text'
    assert board.paste() and board.graphics()[6]['primitives'][0]['kind'] == 'rectangle', \
        'the clipboard still holds the rectangle'
    assert _at(board, 6) == (round(x + 2 * GRID_MM, 4), round(y - 2 * GRID_MM, 4)), 'and its offset'


def test_an_empty_clipboard_pastes_nothing(board):
    assert board.paste() is False and len(board.graphics()) == 4
    board._select_graphic(None)
    assert board.copy_selected() == 0 and board.paste() is False
