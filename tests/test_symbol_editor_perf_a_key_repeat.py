"""@brief Auto-repeated keys of the symbol editor write the recovery draft once, when the repeat ends.

Contracts (write counts and file contents):

- the real press writes, as before; each auto-repeat moves the pin without
  writing; the real release writes once, and the file then holds the whole
  draft, last repeat included;
- the end of a repeat is also the view losing the focus or being hidden, and
  a project change flushes before rebinding;
- negative twins: an edit that is not an auto-repeated key writes at once,
  even while a key is held; a release with nothing deferred writes nothing.

The key events go through `QApplication.sendEvent`, which runs Qt's real
shortcut dispatch (ShortcutOverride to the focus widget, then the shortcut).
"""

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QApplication, QLineEdit

import kicadgen.services.symbol_draft as symbol_draft
from kicadgen.gui.symbol_tab import SymbolTab
from kicadgen.services.symbol_draft import load_draft


def _tab(tmp_path, qapp):
    """A shown tab with recovery file, pin 4 selected and the view focused."""
    tab = SymbolTab()
    tab.resize(1000, 700)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}', 'symbol_side': ('left', 'right', 'top', 'bottom')[i % 4]}
                           for i in range(1, 9)], pad_ids=[str(i) for i in range(1, 9)])
    tab.set_recovery_path(tmp_path / '.symbol-draft.json')
    tab.show()
    tab.activateWindow()
    tab._select_pins({'4'}, '4')
    tab.view.setFocus()
    qapp.processEvents()
    return tab


def _count_writes(monkeypatch):
    writes = []
    real = symbol_draft.save_draft
    monkeypatch.setattr(symbol_draft, 'save_draft', lambda *a: writes.append(a[2]) or real(*a))
    return writes


def _press(tab, key, repeat):
    QApplication.sendEvent(tab.view, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, '', repeat))


def _release(tab, key, repeat=False):
    QApplication.sendEvent(tab.view, QKeyEvent(QEvent.Type.KeyRelease, key, Qt.KeyboardModifier.NoModifier, '', repeat))


def _pin(tab, number='4'):
    return next(p for p in tab._symbol.pins if p.pin.number == number)


def test_a_held_arrow_writes_on_press_and_on_release_only(qapp, tmp_path, monkeypatch):
    """@brief Positive: press writes, repeats do not, the real release writes the final draft."""
    tab = _tab(tmp_path, qapp)
    try:
        writes = _count_writes(monkeypatch)
        start = _pin(tab).y
        _press(tab, Qt.Key.Key_Up, False)
        assert len(writes) == 1
        for _ in range(4):
            _release(tab, Qt.Key.Key_Up, True)   # the auto-repeat pairs of some platforms
            _press(tab, Qt.Key.Key_Up, True)
        assert len(writes) == 1, 'auto-repeats do not write'
        assert _pin(tab).y == pytest.approx(start + 5 * 2.54), 'but they move the pin'
        _release(tab, Qt.Key.Key_Up)
        assert len(writes) == 2
        assert load_draft(tmp_path / '.symbol-draft.json', tab._baseline) == tab.draft_rows()
        _release(tab, Qt.Key.Key_Up)
        assert len(writes) == 2, 'nothing deferred: a release writes nothing'
    finally:
        tab.close()
        tab.deleteLater()


def test_an_edit_during_a_held_key_is_written_at_once(qapp, tmp_path, monkeypatch):
    """@brief Negative: only the repeated key's own steps wait; any other edit writes the whole draft."""
    tab = _tab(tmp_path, qapp)
    try:
        writes = _count_writes(monkeypatch)
        _press(tab, Qt.Key.Key_Up, False)
        _press(tab, Qt.Key.Key_Up, True)
        assert len(writes) == 1
        tab.table.item(0, 1).setText('EDITED')
        assert len(writes) == 2
        assert load_draft(tmp_path / '.symbol-draft.json', tab._baseline) == tab.draft_rows()
        _release(tab, Qt.Key.Key_Up)
        assert len(writes) == 2, 'the edit already wrote the repeat with it'
    finally:
        tab.close()
        tab.deleteLater()


def test_focus_out_and_hide_end_the_repeat(qapp, tmp_path, monkeypatch):
    """@brief The release may never reach the view: leaving it or hiding it writes the deferred steps."""
    tab = _tab(tmp_path, qapp)
    other = QLineEdit(tab)
    other.show()
    try:
        writes = _count_writes(monkeypatch)
        _press(tab, Qt.Key.Key_Up, False)
        _press(tab, Qt.Key.Key_Up, True)
        QApplication.sendEvent(tab.view, QFocusEvent(QEvent.Type.FocusOut))
        assert len(writes) == 2
        tab.view.setFocus()
        _press(tab, Qt.Key.Key_Down, False)
        _press(tab, Qt.Key.Key_Down, True)
        assert len(writes) == 3
        tab.hide()
        assert len(writes) == 4
        assert load_draft(tmp_path / '.symbol-draft.json', tab._baseline) == tab.draft_rows()
    finally:
        tab.close()
        tab.deleteLater()


def test_a_project_change_flushes_into_the_old_recovery_file(qapp, tmp_path, monkeypatch):
    """@brief Deferred steps belong to the project they were made in."""
    tab = _tab(tmp_path, qapp)
    try:
        _press(tab, Qt.Key.Key_Up, False)
        _press(tab, Qt.Key.Key_Up, True)
        held = tab.draft_rows()
        baseline = tab._baseline
        other = tmp_path / 'other'
        other.mkdir()
        tab.set_recovery_path(other / '.symbol-draft.json')
        assert load_draft(tmp_path / '.symbol-draft.json', baseline) == held
    finally:
        tab.close()
        tab.deleteLater()


def test_a_shortcut_without_a_repeated_key_never_inherits_the_flag(qapp, tmp_path, monkeypatch):
    """@brief Negative: the repeat flag is read once; a later activation without a key writes at once."""
    tab = _tab(tmp_path, qapp)
    try:
        writes = _count_writes(monkeypatch)
        _press(tab, Qt.Key.Key_A, True)   # an unbound key that repeats: no shortcut consumed it
        tab.view.take_key_repeat()
        tab._key_action(lambda: tab.rotate_pin('4', 90))
        assert len(writes) == 1
    finally:
        tab.close()
        tab.deleteLater()


def test_drawn_texts_are_unescaped_and_pin_names_are_not(qapp):
    """@brief KiCad shows a symbol text «N{slash}A» as «N/A», a pin named so with its braces."""
    from types import SimpleNamespace
    tab = SymbolTab()
    try:
        tab.set_data('Board', [{'pin': '1', 'name': 'N{slash}A'}])
        assert tab.drawn_text_metrics('N{slash}A', 1.27) == tab.drawn_text_metrics('N/A', 1.27)
        assert tab.drawn_text_metrics('N{slash}A', 1.27) != tab.drawn_text_metrics('N{slash}B', 1.27)
        scene = tab.view.scene()
        primitive = SimpleNamespace(points=[(0.0, 0.0)], text='N{slash}A', size=1.27, angle=0,
                                    justify='left', vjustify='bottom')
        escaped = tab._draw_text_primitive(scene, 10.0, primitive, False)
        plain = tab._draw_text_primitive(scene, 10.0, SimpleNamespace(**{**vars(primitive), 'text': 'N/A'}), False)
        assert escaped.text() == 'N/A'
        assert escaped.sceneBoundingRect() == plain.sceneBoundingRect()
        names = [item.text() for item in scene.items()
                 if item.data(0) == ('pin', '1') and hasattr(item, 'text') and item.text() != '1']
        assert names == ['N{slash}A']
    finally:
        tab.deleteLater()
