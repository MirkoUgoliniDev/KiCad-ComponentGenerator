"""@brief Graphics items whose wrappers a widget keeps must report their deletion to PyQt.

Root cause of the flaky
`test_package_preview_drills.py::test_header_preview_has_44_circular_pads_and_visible_drill_openings`
(2026-10-14): the symbol tab kept, in `_handle_groups`, the wrappers of handle
squares made by `scene.addRect()`. Those are plain C++ objects: when Qt
deleted them together with a destroyed window, PyQt was not told, and the
wrappers, still referenced from the window's surviving Python side, stayed
registered at the freed addresses. A pad ellipse later allocated at one of
those addresses came back from `scene.items()` as a `QGraphicsRectItem`.
Items built with their Python constructor report their deletion, so PyQt
unregisters their wrappers.
"""

import re

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from kicadgen.gui.symbol_tab import SymbolTab


def _rows():
    return [dict(pin='1', name='VCC', electrical_type='power_in'),
            dict(pin='2', name='GND', electrical_type='power_in')]


def _kept_wrappers(tab):
    kept = [item for items, _base, _ring, _owner in tab._handle_groups for item in items]
    if tab._wire_cursor_item is not None:
        kept.append(tab._wire_cursor_item)
    return kept


def test_plain_cpp_items_do_not_report_their_deletion(qapp):
    """@brief Negative: the PyQt behaviour the fix avoids, kept as evidence.

    A wrapper of an `addRect()` item outlives the C++ object without being
    flagged deleted; a Python-constructed item is flagged.
    """
    owner = QWidget()
    view = QGraphicsView(owner)
    view.setScene(QGraphicsScene(view))
    plain = view.scene().addRect(0, 0, 1, 1)
    from PyQt6.QtWidgets import QGraphicsRectItem
    built = QGraphicsRectItem(0, 0, 1, 1)
    view.scene().addItem(built)
    sip.delete(owner)
    assert not sip.isdeleted(plain)
    assert sip.isdeleted(built)


@pytest.mark.parametrize('wiring', [False, True])
def test_symbol_tab_kept_items_are_unregistered_when_qt_deletes_them(qapp, wiring):
    """@brief Positive: every handle and the rubber band the tab keeps is flagged deleted.

    @param wiring True also draws the wire rubber band (`_wire_cursor_item`).
    """
    host = QWidget()
    tab = SymbolTab(host)
    tab.set_data('Board', _rows())
    if wiring:
        tab.start_wire()
        tab._wire_points = [[0.0, 0.0]]
        tab._draw(tab._symbol)
    kept = _kept_wrappers(tab)
    assert kept, 'the symbol must draw resize handles'
    assert (tab._wire_cursor_item is not None) == wiring
    assert not any(sip.isdeleted(item) for item in kept)
    sip.delete(host)  # Qt destroys tab, view, scene and items, as with a closed window
    assert all(sip.isdeleted(item) for item in kept)


#: A scene.add*() result stored on an object (attribute or list attribute).
KEPT_SCENE_ADD = re.compile(
    r"self\.\w+(?:\[[^\]]*\])?\s*=\s*[\w.]*\.add(?:Rect|Ellipse|Path|Pixmap|SimpleText|Text|Line|Polygon)\("
    r"|self\.\w+\.(?:append|extend|add)\([\w.]*\.add(?:Rect|Ellipse|Path|Pixmap|SimpleText|Text|Line|Polygon)\(")

def test_no_gui_module_keeps_a_wrapper_made_by_a_scene_add_call():
    """Guard for the class of defect (2026-10-14): a wrapper returned by
    QGraphicsScene.add*() and stored on an object outlives its C++ item and
    can be handed back for a later item at the same address (the flaky drill
    test). Kept items must be built in Python and added with addItem
    (symbol_tab._add_kept_item).
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "kicadgen" / "gui"
    kept = KEPT_SCENE_ADD
    offenders = [f"{path.name}:{number}"
                 for path in sorted(root.glob("*.py"))
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if kept.search(line)]
    assert not offenders, offenders


def test_the_guard_pattern_sees_a_kept_wrapper():
    """Negative twin: the pattern does flag the two shapes it must refuse."""
    kept = KEPT_SCENE_ADD
    assert kept.search("        self.photo_item = self.scene.addPixmap(pixmap)")
    assert kept.search("        self._handles.append(scene.addRect(0, 0, 1, 1))")
    assert not kept.search("        items.append(scene.addRect(0, 0, 1, 1))")


def _graphics_items_held(value, depth=0):
    """Every QGraphicsItem reachable from `value` through lists, tuples, sets and dicts (bounded depth)."""
    from PyQt6.QtWidgets import QGraphicsItem
    if isinstance(value, QGraphicsItem):
        return [value]
    if depth >= 4:
        return []
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for element in value for item in _graphics_items_held(element, depth + 1)]
    return []


def _deleted_items_held(tab):
    """Attributes of the tab that still refer to a graphics item Qt has deleted."""
    return [name for name, value in vars(tab).items()
            if any(sip.isdeleted(item) for item in _graphics_items_held(value))]


@pytest.mark.parametrize('tool', ['wire', 'line'])
def test_no_attribute_refers_to_a_deleted_item_after_an_invalid_draft(qapp, tool):
    """Guard for the class of defect (audit M1, 2026-10-15): `_draw` clears
    the scene and returned early for an invalid draft before resetting the
    rubber band it kept, so the next mouse move called setPath on a deleted
    item. The check walks every attribute, so a future kept item that is not
    reset by `_forget_scene_items` fails here too.
    """
    tab = SymbolTab()
    try:
        tab.set_data('Board', _rows())
        if tool == 'wire':
            tab.start_wire()
            assert tab.add_wire_point(0, 2.54)
        else:
            tab.start_shape(tool)
            assert tab.add_shape_point(0, 0)
        assert tab._wire_cursor_item is not None
        tab.table.item(0, 1).setText('VCC\tGND')  # a control character: invalid draft
        assert tab._symbol is None
        assert _deleted_items_held(tab) == []
    finally:
        tab.deleteLater()


def test_the_attribute_walk_sees_a_deleted_kept_item(qapp):
    """Negative twin: a wrapper kept across scene.clear() is reported."""
    tab = SymbolTab()
    try:
        tab.set_data('Board', _rows())
        stale = tab._handle_groups
        assert stale, 'the symbol draws resize handles'
        tab.view.scene().clear()
        tab._stale_for_test = stale
        assert '_stale_for_test' in _deleted_items_held(tab)
    finally:
        tab.deleteLater()
