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
    # The property texts and squares moved live by a drag (2026-10-17).
    for item, square in tab._property_items.values():
        kept.extend((item, square))
    kept.extend(item for item, _origin in tab._gesture_live)
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
        assert tab.add_wire_point(0.0, 0.0), 'a wire point in progress draws the rubber band'
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
    """Every QGraphicsItem reachable from `value` through lists, tuples, sets, dicts and the
    shared gesture objects (`canvas_gestures`: controller, drag plan, hover groups), bounded depth."""
    from PyQt6.QtWidgets import QGraphicsItem
    from kicadgen.gui import canvas_gestures
    if isinstance(value, QGraphicsItem):
        return [value]
    if depth >= 5:
        return []
    if isinstance(value, dict):
        value = list(value.values())
    elif type(value).__module__ == canvas_gestures.__name__ and hasattr(value, '__dict__'):
        # The shared objects hold items in their own attributes (2026-10-17):
        # a stale reference inside them must fail like one on the tab.
        value = list(vars(value).values())
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


def test_the_attribute_walk_enters_the_shared_gesture_objects(qapp):
    """Negative twin (2026-10-17): a stale item kept inside a HoverGroups or a drag plan is reported."""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtWidgets import QGraphicsRectItem
    from kicadgen.gui.canvas_gestures import DragPlan, HoverGroups
    tab = SymbolTab()
    try:
        tab.set_data('Board', _rows())
        square = QGraphicsRectItem(0, 0, 2, 2)
        tab.view.scene().addItem(square)
        hover = HoverGroups(tab.view)
        hover.add([square], QRectF(0, 0, 2, 2), False, ('test', 0))
        plan = DragPlan('live', live_items=[(square, QPointF(0, 0))])
        tab._stale_hover_for_test = hover
        tab._stale_plan_for_test = plan
        assert _deleted_items_held(tab) == [], 'alive items are not reported'
        tab.view.scene().clear()
        assert {'_stale_hover_for_test', '_stale_plan_for_test'} <= set(_deleted_items_held(tab))
    finally:
        tab.deleteLater()


def test_a_drag_cancelled_by_a_scene_clear_leaves_no_stale_item_in_the_controller(qapp):
    """@brief Positive: after a redraw during a live property drag, the controller holds live items only."""
    from PyQt6.QtCore import QPointF
    tab = SymbolTab()
    try:
        tab.set_data('Board', _rows())
        assert tab._gestures.press(('property', 'Value'), QPointF(0, 0))
        tab._gestures.move(QPointF(5, 5))
        tab._draw(tab._symbol)  # rebuilds the property items and re-binds them
        assert tab._gestures.active and tab._gestures.live_items
        assert _deleted_items_held(tab) == []
        tab._gestures.cancel()
        assert _deleted_items_held(tab) == []
    finally:
        tab.deleteLater()


def test_footprint_preview_shared_objects_hold_no_deleted_item_across_a_redraw(window, qapp):
    """@brief Positive (footprint side, 2026-10-17): the preview's hover groups and controller survive a redraw.

    The window clears `preview_scene` on every redraw: the shared objects
    must be emptied first (`_forget_preview_items`) and, during a live text
    drag, re-bound to the new items, never left pointing at deleted ones.
    """
    from PyQt6.QtCore import QPointF
    view = window.preview_view
    window._draw_preview()
    qapp.processEvents()
    assert view.hover.groups, 'the preview registers its text squares as hover groups'
    assert _deleted_items_held(view) == [] and _deleted_items_held(window) == []
    assert view.gestures.press(('text', 'Reference'), QPointF(0, 0))
    view.gestures.move(QPointF(3, 3))
    window._draw_preview()  # a redraw under the live drag: items rebuilt and re-bound
    qapp.processEvents()
    assert view.gestures.active and view.gestures.live_items
    assert _deleted_items_held(view) == [] and _deleted_items_held(window) == []
    view.gestures.cancel()
    assert _deleted_items_held(view) == [] and _deleted_items_held(window) == []


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
