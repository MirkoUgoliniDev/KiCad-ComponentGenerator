"""@brief Robustness of the symbol editor found by the GUI audit of 2026-10-15.

Each defect has a positive case (the behaviour now required) and a negative
twin that the same rule must keep distinct, both failing or meaningless on
the code before the fix:

- M1: the wire rubber band kept after the scene was cleared for an invalid
  draft (a pin name pasted with a TAB) crashed every mouse move.
- M2: the «Proprietà pin…» dialog committed properties and geometry in two
  steps; a refused position left the properties in the draft.
- M3: OK without changes dirtied the draft and froze the automatic layout.
- M4: a gesture on several selected elements was one commit per element.
- L3: drafts recreated a renamed or deleted project folder; the element
  selection survived a project change.
- L1: the palette actions piled up at every language change.
"""

from copy import deepcopy

from PyQt6.QtCore import QCoreApplication, QEvent, QPointF
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox

import kicadgen.services.symbol_draft as symbol_draft
from kicadgen.gui.symbol_tab import SymbolTab
from kicadgen.services.symbol_draft import load_draft, save_draft


def _tab(name='Board', count=3):
    """A symbol tab holding `count` pins named A, B, C..."""
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data(name, [{'pin': str(i), 'name': chr(64 + i)} for i in range(1, count + 1)],
                 pad_ids=[str(i) for i in range(1, count + 1)])
    return tab


def _glyph_key():
    from kicadgen.generators.symbol_glyphs import catalogue
    return catalogue()[0].key


# ── M1: kept scene items after an invalid draft ──────────────────────────────

def _make_name_invalid(tab, row=0):
    """Paste a spreadsheet cell pair into a pin name: the TAB is refused by the layout."""
    tab.table.item(row, 1).setText('VCC\tGND')
    assert tab._symbol is None, 'precondition: the draft must be invalid'


def test_mouse_move_after_the_draft_becomes_invalid_while_wiring_does_not_raise(qapp):
    tab = _tab()
    try:
        tab.start_wire()
        assert tab.add_wire_point(0, 2.54)
        assert tab._wire_cursor_item is not None
        _make_name_invalid(tab)
        # Before the fix: RuntimeError, the path item was deleted by scene.clear().
        tab._update_readout(QPointF(30, 30))
        assert tab._wire_cursor_item is None
    finally:
        tab.deleteLater()


def test_the_wire_resumes_once_the_name_is_fixed_and_ignores_clicks_meanwhile(qapp):
    """Negative twin: the tool stays armed with its points, no point is taken while invalid."""
    tab = _tab()
    try:
        tab.start_wire()
        assert tab.add_wire_point(0, 2.54)
        points = deepcopy(tab._wire_points)
        _make_name_invalid(tab)
        assert not tab.add_wire_point(10.16, 2.54), 'an invisible point must not be taken'
        assert tab._wire_points == points
        tab.table.item(0, 1).setText('VCC')
        assert tab._symbol is not None
        assert tab.view.wiring and tab._wire_points == points
        assert tab._wire_cursor_item is not None, 'the rubber band is drawn again'
        tab._update_readout(QPointF(40, -20))
    finally:
        tab.deleteLater()


def test_shape_rubber_band_survives_an_invalid_draft(qapp):
    tab = _tab()
    try:
        tab.start_shape('line')
        assert tab.add_shape_point(0, 0)
        assert tab._wire_cursor_item is not None
        _make_name_invalid(tab)
        tab._update_readout(QPointF(25, 25))
        assert not tab.add_shape_point(5, 5)
        assert tab._shape_points == [tab._grid_point(0, 0)]
    finally:
        tab.deleteLater()


# ── M2/M3: «Proprietà pin…» is one atomic edit ───────────────────────────────

def _run_pin_dialog(tab, monkeypatch, script):
    """Open the pin dialog of pin 1 and let `script(dialog)` drive it."""
    tab._selected_pin = '1'
    monkeypatch.setattr(QDialog, 'exec', script)
    tab._edit_selected_pin()


def _ok(dialog):
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()


def _cancel(dialog):
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()


def test_refused_position_leaves_the_properties_out_of_the_draft(qapp, monkeypatch):
    tab = _tab()
    try:
        other = tab._symbol.pins[1]
        before, steps = tab.draft_rows(), tab._history_index
        seen = {}

        def script(dialog):
            spins = dialog.findChildren(QDoubleSpinBox)
            spins[0].setValue(other.x)
            spins[1].setValue(other.y)  # onto pin 2: refused
            dialog.findChildren(QComboBox)[1].setCurrentIndex(3)  # length 5.08 mm
            dialog.findChild(QCheckBox).setChecked(True)
            _ok(dialog)
            seen['open'] = dialog.result() != QDialog.DialogCode.Accepted
            _cancel(dialog)

        _run_pin_dialog(tab, monkeypatch, script)
        assert seen['open'], 'the refusal keeps the dialog open'
        assert tab.draft_rows() == before, 'length and hidden must not stay in the draft'
        assert tab._history_index == steps
    finally:
        tab.deleteLater()


def test_accepted_dialog_is_one_undo_step_for_properties_and_position(qapp, monkeypatch):
    """Positive twin: a valid position commits the properties with it, undone by one click."""
    tab = _tab()
    try:
        before, steps = tab.draft_rows(), tab._history_index

        def script(dialog):
            spins = dialog.findChildren(QDoubleSpinBox)
            spins[0].setValue(50.8)
            spins[1].setValue(25.4)
            dialog.findChildren(QComboBox)[1].setCurrentIndex(3)
            dialog.findChild(QCheckBox).setChecked(True)
            _ok(dialog)

        _run_pin_dialog(tab, monkeypatch, script)
        row = tab.draft_rows()[0]
        assert row['symbol_position'] == [50.8, 25.4]
        assert row['symbol_pin_length'] == 5.08 and row['symbol_pin_hidden'] is True
        assert tab._history_index == steps + 1, 'one edit, one undo step'
        tab.undo()
        assert tab.draft_rows() == before
    finally:
        tab.deleteLater()


def test_ok_without_changes_keeps_the_draft_clean_and_the_layout_automatic(qapp, monkeypatch):
    tab = _tab()
    try:
        before, steps = tab.draft_rows(), tab._history_index
        _run_pin_dialog(tab, monkeypatch, _ok)
        assert tab.draft_rows() == before
        assert not tab.has_pending_edits()
        assert 'symbol_position' not in tab.draft_rows()[0], 'the automatic layout stays automatic'
        assert tab._history_index == steps
    finally:
        tab.deleteLater()


def test_ok_with_only_visibility_changed_commits_it(qapp, monkeypatch):
    """Negative twin of the no-op: one changed field is an edit."""
    tab = _tab()
    try:
        steps = tab._history_index

        def script(dialog):
            dialog.findChild(QCheckBox).setChecked(True)
            _ok(dialog)

        _run_pin_dialog(tab, monkeypatch, script)
        assert tab.draft_rows()[0].get('symbol_pin_hidden') is True
        assert tab.has_pending_edits() and tab._history_index == steps + 1
    finally:
        tab.deleteLater()


def test_set_pin_properties_with_the_current_values_commits_nothing(qapp):
    from kicadgen.generators.symbol import pin_length_of, pin_text_size_of
    tab = _tab()
    try:
        placed = tab._symbol.pins[0]
        before = tab.draft_rows()
        assert tab.set_pin_properties('1', pin_length_of(placed.pin, tab._symbol.pin_length),
                                      pin_text_size_of(placed.pin), bool(placed.pin.hidden))
        assert tab.set_pin_geometry('1', placed.x, placed.y, placed.angle)
        assert tab.draft_rows() == before and not tab.has_pending_edits()
        assert tab.set_pin_properties('1', length=5.08)
        assert tab.draft_rows()[0]['symbol_pin_length'] == 5.08
    finally:
        tab.deleteLater()


# ── M4: one commit per gesture on a multi-selection ─────────────────────────

def _with_glyphs(tab, count=3):
    key = _glyph_key()
    for i in range(count):
        assert tab.place_glyph(key, 20 + 10 * i, 20)
    tab._selected_graphics = set(range(count))
    tab._selected_graphic = count - 1


def test_deleting_a_multi_selection_is_one_undo_step_and_one_checkpoint(qapp, tmp_path, monkeypatch):
    tab = _tab()
    try:
        tab.set_recovery_path(tmp_path / '.symbol-draft.json')
        _with_glyphs(tab)
        steps = tab._history_index
        writes = []
        real = symbol_draft.save_draft
        monkeypatch.setattr(symbol_draft, 'save_draft', lambda *a: writes.append(1) or real(*a))
        assert tab._edit_selected_graphics('delete')
        assert tab.graphics() == []
        assert tab._history_index == steps + 1
        assert len(writes) == 1, 'one checkpoint per gesture'
        tab.undo()
        assert len(tab.graphics()) == 3, 'one undo brings every element back'
    finally:
        tab.deleteLater()


def test_turning_a_selection_with_a_wire_turns_the_glyphs_in_one_step(qapp):
    """Negative twin: the element the operation refuses (a wire) is skipped, not a separate failure."""
    tab = _tab()
    try:
        _with_glyphs(tab, 2)
        tab.start_wire()
        assert tab.add_wire_point(-30.48, 30.48) and tab.add_wire_point(-20.32, 30.48)
        assert tab.finish_wire(True)
        tab._selected_graphics = {0, 1, 2}
        tab._selected_graphic = 2
        steps = tab._history_index
        assert tab._edit_selected_graphics('rotate', 90)
        rotations = [g.get('rotation', 0) for g in tab.graphics()]
        assert rotations == [90, 90, 0]
        assert tab._history_index == steps + 1
        assert tab.selected_graphics() == [0, 1, 2]
    finally:
        tab.deleteLater()


def test_turning_only_wires_commits_nothing(qapp):
    tab = _tab()
    try:
        tab.start_wire()
        assert tab.add_wire_point(-30.48, 30.48) and tab.add_wire_point(-20.32, 30.48)
        assert tab.finish_wire(True)
        tab._selected_graphics = {0}
        tab._selected_graphic = 0
        before, steps = tab.draft_rows(), tab._history_index
        assert not tab._edit_selected_graphics('rotate', 90)
        assert tab.draft_rows() == before and tab._history_index == steps
    finally:
        tab.deleteLater()


# ── L3: drafts never create the project folder ─────────────────────────────

def test_save_draft_does_not_create_a_missing_project_folder(tmp_path):
    rows = [{'pin': '1', 'name': 'A'}]
    changed = [{'pin': '1', 'name': 'B'}]
    gone = tmp_path / 'OLD' / '.symbol-draft.json'
    assert save_draft(gone, rows, changed) is False
    assert not gone.parent.exists()
    kept = tmp_path / '.symbol-draft.json'
    assert save_draft(kept, rows, changed) is True
    assert load_draft(kept, rows) == changed


def test_editor_bound_to_a_renamed_project_leaves_no_ghost_folder(qapp, tmp_path):
    old, new = tmp_path / 'OLD', tmp_path / 'NEW'
    old.mkdir()
    tab = _tab()
    try:
        tab.set_recovery_path(old / '.symbol-draft.json')
        assert tab.place_pin('1', 50.8, 25.4)
        assert (old / '.symbol-draft.json').is_file(), 'crash recovery still works for an existing folder'
        old.rename(new)
        assert tab.place_pin('1', 55.88, 25.4)
        assert not old.exists(), 'no ghost OLD folder'
        assert tab._write_snapshot(old / '.symbol-draft.json', tab._baseline, tab.draft_rows()) is None
        assert not old.exists()
        # CONTRACT CHANGED (review L9, 2026-10-15): the skip is told, not only logged.
        from kicadgen.gui.symbol_tab import DRAFT_FOLDER_MISSING
        assert tab.status.text() == tab.tx(DRAFT_FOLDER_MISSING)
    finally:
        tab.deleteLater()


# ── L3: the element selection belongs to its project ───────────────────────

def test_element_selection_is_dropped_when_another_project_is_loaded(qapp):
    key = _glyph_key()
    other = _tab('B', 2)
    other.place_glyph(key, 30, 10)
    other.place_glyph(key, 40, 10)
    other.apply()
    rows_b = other.rows()
    other.deleteLater()
    tab = _tab('A', 1)
    try:
        tab.place_glyph(key, 20, 20)
        tab.apply()
        tab._select_graphic(0)
        tab.set_data('B', rows_b, pad_ids=['1', '2'])
        assert tab.selected_graphics() == []
        assert not tab._edit_selected_graphics('delete')
        assert len(tab.graphics()) == 2
    finally:
        tab.deleteLater()


def test_element_selection_survives_a_reload_of_the_same_project(qapp):
    """Negative twin: the same project's indices still name the same elements."""
    tab = _tab('A', 2)
    try:
        tab.place_glyph(_glyph_key(), 20, 20)
        tab.apply()
        tab._select_graphic(0)
        tab.set_data('A', tab.rows(), pad_ids=['1', '2'], force=True)
        assert tab.selected_graphics() == [0]
    finally:
        tab.deleteLater()


# ── L1: palette actions are not leaked by retranslate ──────────────────────

def test_retranslate_does_not_pile_up_palette_actions(qapp):
    tab = _tab()
    try:
        count = len(tab.palette.findChildren(QAction))
        assert count > 0
        tab.retranslate()
        tab.retranslate()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert len(tab.palette.findChildren(QAction)) == count
    finally:
        tab.deleteLater()


# ── Review L9: the user is told once that crash recovery is off ─────────────

def _notices(tab, monkeypatch):
    """Record every status message equal to the missing-folder notice."""
    from kicadgen.gui.symbol_tab import DRAFT_FOLDER_MISSING
    shown = []
    real = tab.set_status
    monkeypatch.setattr(tab, 'set_status', lambda message: (
        shown.append(message) if message == tab.tx(DRAFT_FOLDER_MISSING) else None, real(message))[1])
    return shown


def test_missing_project_folder_is_told_once_and_withdrawn_when_writing_works(qapp, tmp_path, monkeypatch):
    from kicadgen.gui.symbol_tab import DRAFT_FOLDER_MISSING
    folder = tmp_path / 'OLD'
    folder.mkdir()
    tab = _tab()
    try:
        tab.set_recovery_path(folder / '.symbol-draft.json')
        shown = _notices(tab, monkeypatch)
        assert tab.place_pin('1', 50.8, 25.4)
        assert shown == [], 'an existing folder raises no notice'
        folder.rename(tmp_path / 'NEW')
        assert tab.place_pin('1', 55.88, 25.4)
        assert shown == [tab.tx(DRAFT_FOLDER_MISSING)]
        assert tab.status.text() == tab.tx(DRAFT_FOLDER_MISSING)
        assert tab.place_pin('1', 60.96, 25.4)
        assert tab.place_pin('1', 66.04, 25.4)
        assert len(shown) == 1, 'not repeated at every edit'
        folder.mkdir()
        tab._draft_folder_missing(folder)  # no-op: already told
        assert tab.place_pin('1', 71.12, 25.4)
        assert (folder / '.symbol-draft.json').is_file()
        assert tab.status.text() != tab.tx(DRAFT_FOLDER_MISSING)
        (folder / '.symbol-draft.json').unlink()
        folder.rmdir()
        assert tab.place_pin('1', 76.2, 25.4)
        assert len(shown) == 2, 'a new situation is told again'
    finally:
        tab.deleteLater()


def test_the_notice_text_has_all_four_translations():
    from kicadgen.gui.symbol_tab import DRAFT_FOLDER_MISSING
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    entry = DEFAULT_TRANSLATIONS['ui:' + DRAFT_FOLDER_MISSING]
    assert set(entry) == {'it', 'en', 'fr', 'de'} and all(v.strip() for v in entry.values())
    assert entry['it'] == DRAFT_FOLDER_MISSING


# ── Review L10: a refused delete keeps the selection ────────────────────────

def test_refused_delete_of_a_selection_keeps_the_selection(qapp, monkeypatch):
    tab = _tab()
    try:
        _with_glyphs(tab)
        before = tab.draft_rows()
        monkeypatch.setattr(tab, '_accept_geometry', lambda rows: False)
        assert not tab._edit_selected_graphics('delete')
        assert tab.draft_rows() == before
        assert tab.selected_graphics() == [0, 1, 2]
    finally:
        tab.deleteLater()


def test_accepted_delete_of_a_selection_clears_it(qapp):
    """Negative twin: deleted elements are no longer selected."""
    tab = _tab()
    try:
        _with_glyphs(tab)
        assert tab._edit_selected_graphics('delete')
        assert tab.selected_graphics() == [] and tab._selected_graphic is None
    finally:
        tab.deleteLater()
