import pytest
from copy import deepcopy

from PyQt6.QtCore import Qt

from kicadgen.gui.symbol_tab import SymbolTab


def test_drafts_apply_cancel_and_readonly_numbers(qapp):
    source = [{'pin': '1', 'name': 'G', 'source': 'photo'}, {'pin': '2', 'name': '5V'}]
    before = deepcopy(source)
    tab = SymbolTab()
    tab.set_data('Board', source, 'KiCadGen:Board', ['1', '2'])
    applied = []
    tab.edits_applied.connect(applied.append)
    assert not tab.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    tab.table.item(0, 1).setText('GND')
    assert not tab.export_button.isEnabled()
    assert tab.rows()[0]['name'] == 'G'
    tab.set_data('Board', source, 'KiCadGen:Board', ['1', '2'])
    assert tab.table.item(0, 1).text() == 'GND'
    tab.apply_button.click()
    assert applied[0][0]['name'] == 'GND'
    assert applied[0][0]['source'] == 'photo'
    assert tab.export_button.isEnabled()
    tab.table.item(0, 1).setText('WRONG')
    tab.cancel_button.click()
    assert tab.table.item(0, 1).text() == 'GND'
    assert source == before
    assert tab.rows()[1]['symbol_side'] == 'right'
    tab.deleteLater()


def test_missing_names_can_be_entered_but_mapping_errors_block_export(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [], pad_ids=['1', '2'])
    assert tab.table.rowCount() == 2
    assert tab.rows()[0]['electrical_type'] == 'passive'
    tab.table.item(0, 1).setText('VCC')
    types = tab.table.cellWidget(0, 2)
    types.setCurrentIndex(types.findData('power_in'))
    tab.apply()
    assert tab.rows()[0]['electrical_type'] == 'power_in'
    tab.set_data('Other', [{'pin': '9', 'name': 'Unknown'}], pad_ids=['1'])
    assert not tab.export_button.isEnabled()
    assert tab.table.rowCount() == 2
    tab.set_data('Empty', [], pad_ids=[])
    assert not tab.export_button.isEnabled()
    tab.deleteLater()


def test_duplicate_numbers_and_control_characters_are_invalid(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': '1', 'name': 'A'}, {'pin': '1', 'name': 'B'}])
    assert not tab.export_button.isEnabled()
    tab.set_data('Board', [{'pin': '1', 'name': 'A'}])
    tab.table.item(0, 1).setText('bad\nname')
    assert not tab.apply_button.isEnabled()
    assert not tab.export_button.isEnabled()
    tab.deleteLater()


def test_failed_save_can_restore_baseline_and_duplicates_with_pad_ids_block(qapp):
    tab = SymbolTab()
    source = [{'pin': '1', 'name': 'A'}]
    tab.set_data('Board', source, pad_ids=['1'])
    tab.edits_applied.connect(lambda rows: tab.set_data('Board', source, pad_ids=['1'], force=True))
    tab.table.item(0, 1).setText('B')
    tab.apply()
    assert tab.rows()[0]['name'] == 'A'
    assert tab.table.item(0, 1).text() == 'A'
    tab.set_status('Saved')
    assert tab.export_button.isEnabled()
    tab.set_data('Board', source * 2, pad_ids=['1'])
    assert not tab.export_button.isEnabled()
    tab.deleteLater()


def test_graphic_edits_history_reset_and_reload(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': str(n), 'name': f'GPIO{n}'} for n in range(1, 9)])
    assert tab.table.columnCount() == 3
    original = tab.draft_rows()
    assert tab.move_pin('1', 'top', 0.3)
    assert tab.draft_rows()[0]['symbol_offset'] == 0
    assert tab.draft_rows()[0]['symbol_side'] == 'top'
    moved = tab.draft_rows()
    assert tab.resize_body(60.96, 40.64)
    resized = tab.draft_rows()
    assert resized[0]['symbol_body'] == [60.96, 40.64]
    tab.undo()
    assert tab.draft_rows() == moved
    tab.undo()
    assert tab.draft_rows() == original
    tab.redo()
    tab.redo()
    assert tab.draft_rows() == resized
    tab.apply()
    tab.set_data('Board', tab.rows(), force=True)
    assert tab.draft_rows() == resized
    assert (tab._symbol.width, tab._symbol.height) == (60.96, 40.64)
    tab.reset_layout()
    assert all('symbol_offset' not in row for row in tab.draft_rows())
    tab.cancel_button.click()
    assert tab.draft_rows() == resized
    tab.deleteLater()


def test_collision_and_shrinking_cannot_destroy_pin_positions(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': str(n), 'name': f'P{n}'} for n in range(1, 45)])
    before = tab.draft_rows()
    other = tab._symbol.pins[1]
    assert not tab.move_pin('1', 'left', other.y)
    assert tab.draft_rows() == before
    positions = [(p.x, p.y) for p in tab._symbol.pins]
    assert tab.resize_body(tab._symbol.width, 10.16)
    assert [(p.x, p.y) for p in tab._symbol.pins] == positions
    tab.undo()
    assert tab.move_pin('1', 'bottom', 0)
    tab.table.item(0, 1).setText('NEW')
    tab.undo()
    assert tab.draft_rows()[0]['name'] == 'P1'
    assert tab.draft_rows()[0]['symbol_side'] == 'bottom'
    tab.deleteLater()


def test_mouse_drag_pin_resize_and_escape(qapp):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtTest import QTest
    tab = SymbolTab()
    tab.resize(1200, 750)
    tab.set_data('Board', [{'pin': str(n), 'name': f'P{n}'} for n in range(1, 9)])
    tab.show()
    qapp.processEvents()
    view = tab.view
    before = tab.draft_rows()
    pin = tab._symbol.pins[0]
    start = view.mapFromScene(QPointF(pin.x * 10, -pin.y * 10))
    end = view.mapFromScene(QPointF(0, -(tab._symbol.height / 2 + 2.54) * 10))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    assert view.gesture == ('pin', '1')
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert tab.draft_rows()[0]['symbol_position'][0] == 0
    assert tab.draft_rows()[0]['symbol_angle'] == 0
    assert len(tab._history) == 2
    tab.undo()
    assert tab.draft_rows() == before
    # A corner drag changes both dimensions, and Esc restores the whole gesture.
    w, h = tab._symbol.width, tab._symbol.height
    start = view.mapFromScene(QPointF(w * 5, h * 5))
    end = view.mapFromScene(QPointF(w * 5 + 50.8, h * 5 + 50.8))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    assert view.gesture[0] == 'resize'
    QTest.mouseMove(view.viewport(), end)
    assert tab._symbol.width > w and tab._symbol.height > h
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert tab.draft_rows() == before
    assert view.gesture is None
    tab.close()
    tab.deleteLater()


def test_full_column_drag_swaps_only_destination_and_release_updates_position(qapp):
    """Regression: selected yellow pin appeared stuck on the fully occupied board."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtTest import QTest
    tab = SymbolTab()
    tab.resize(1200, 850)
    tab.set_data('Board', [{'pin': str(n), 'name': f'GPIO{n}'} for n in range(1, 45)])
    tab.show()
    qapp.processEvents()
    view = tab.view
    before = tab.draft_rows()
    initial = {p.pin.number: (p.x, p.y) for p in tab._symbol.pins}
    def point(number):
        x, y = initial[number]
        return view.mapFromScene(QPointF(x * 10, -y * 10))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point('25'))
    # Cross one occupied row before releasing on another: no chain of swaps.
    QTest.mouseMove(view.viewport(), point('26'))
    assert next(p for p in tab._symbol.pins if p.pin.number == '25').y == initial['26'][1]
    # No mouseMove to final point: release itself must finish the move.
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point('27'))
    actual = {p.pin.number: (p.x, p.y) for p in tab._symbol.pins}
    assert actual['25'] == initial['27']
    assert actual['27'] == initial['25']
    assert all(actual[n] == initial[n] for n in initial if n not in ('25', '27'))
    assert len(tab._history) == 2
    tab.undo()
    assert tab.draft_rows() == before
    tab.redo()
    assert next(p for p in tab._symbol.pins if p.pin.number == '25').y == initial['27'][1]
    tab.close()
    tab.deleteLater()


def test_selecting_pin_without_moving_keeps_project_clean(qapp):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtTest import QTest
    tab = SymbolTab()
    tab.resize(1200, 750)
    tab.set_data('Board', [{'pin': str(n), 'name': f'P{n}'} for n in range(1, 9)])
    tab.show()
    qapp.processEvents()
    pin = tab._symbol.pins[0]
    point = tab.view.mapFromScene(QPointF(pin.x * 10, -pin.y * 10))
    QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert tab._selected_pin == '1'
    assert not tab.has_pending_edits()
    assert len(tab._history) == 1
    tab.close()
    tab.deleteLater()


def test_free_position_rotation_menu_undo_and_resize(qapp, monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QMenu
    from kicadgen.generators.symbol import symbol_inputs, serialize_symbol
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 5)])
    assert tab.place_pin('1', 50.7, 40.7)
    p = tab._symbol.pins[0]
    assert (p.x, p.y, p.angle) == (50.8, 40.64, 0)
    def choose_rotation(menu, pos):
        menu.actions()[2].trigger()
    monkeypatch.setattr(QMenu, 'exec', choose_rotation)
    tab._pin_context_menu('1', QPoint())
    p = tab._symbol.pins[0]
    assert (p.x, p.y, p.angle) == (50.8, 40.64, 90)
    tab.undo()
    assert tab._symbol.pins[0].angle == 0
    tab.redo()
    for angle in (180, 270, 0):
        assert tab.rotate_pin('1', 90)
        assert tab._symbol.pins[0].angle == angle
        assert (tab._symbol.pins[0].x, tab._symbol.pins[0].y) == (50.8, 40.64)
    assert tab.rotate_pin('1', -90)
    assert tab.resize_body(10.16, 10.16)
    assert (tab._symbol.pins[0].x, tab._symbol.pins[0].y) == (50.8, 40.64)
    tab.apply()
    tab.set_data('Board', tab.rows(), force=True)
    pins, body = symbol_inputs(tab.rows())
    assert '(at 50.8 40.64 270)' in serialize_symbol('Board', pins, body=body)
    tab.reset_layout()
    assert all('symbol_position' not in r for r in tab.draft_rows())
    tab.deleteLater()


def test_context_event_on_pin_opens_rotation_menu(qapp, monkeypatch):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu, QApplication
    tab = SymbolTab()
    tab.resize(1200, 750)
    tab.set_data('Board', [{'pin': '1', 'name': 'GND'}])
    tab.show()
    qapp.processEvents()
    pin = tab._symbol.pins[0]
    point = tab.view.mapFromScene(QPointF(pin.x * 10, -pin.y * 10))
    def choose(menu, pos):
        assert len(menu.actions()) == 6
        assert [a.text() for a in menu.actions()[2:]] == ["+90\u00b0", "-90\u00b0", "180\u00b0", "-180\u00b0"]
        assert all(not a.icon().isNull() and a.toolTip() for a in menu.actions()[2:])
        menu.actions()[3].trigger()
    monkeypatch.setattr(QMenu, 'exec', choose)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point,
                             tab.view.viewport().mapToGlobal(point))
    QApplication.sendEvent(tab.view.viewport(), event)
    result = tab._symbol.pins[0]
    assert (result.x, result.y, result.angle) == (pin.x, pin.y, 270)
    assert tab.has_pending_edits()
    tab.close()
    tab.deleteLater()


@pytest.mark.parametrize('button', [Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton])
def test_cad_pan_zoom_anchor_and_fit_do_not_edit_symbol(qapp, button):
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    tab = SymbolTab()
    tab.resize(1200, 750)
    tab.set_data('Board', [{'pin': str(n), 'name': f'P{n}'} for n in range(1, 9)])
    tab.show()
    qapp.processEvents()
    view = tab.view
    rows = tab.draft_rows()
    pin = tab._symbol.pins[0]
    world = QPointF(pin.x * 10, -pin.y * 10)
    initial_pin = view.mapFromScene(world)
    start = QPoint(30, 180) if button == Qt.MouseButton.LeftButton else initial_pin
    assert button != Qt.MouseButton.LeftButton or not view.items(start)
    end = start + QPoint(180, -90)
    QTest.mousePress(view.viewport(), button, pos=start)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), button, pos=end)
    assert (view.mapFromScene(world) - initial_pin - (end - start)).manhattanLength() <= 2
    assert view.gesture is None and view._pan_last is None
    assert not view.auto_fit
    anchor = view.viewport().rect().center() + QPoint(80, 50)
    fixed = view.mapToScene(anchor)
    scale = view.transform().m11()
    event = QWheelEvent(QPointF(anchor), QPointF(view.viewport().mapToGlobal(anchor)),
                       QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), event)
    assert view.transform().m11() > scale
    assert (view.mapFromScene(fixed) - anchor).manhattanLength() <= 3
    assert tab.draft_rows() == rows
    assert not tab.has_pending_edits()
    view.fit()
    assert view.auto_fit
    assert view.viewport().rect().contains(view.mapFromScene(world))
    tab.close()
    tab.deleteLater()


def test_precise_pin_edit_and_keyboard_step(qapp, monkeypatch):
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QComboBox
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': '1', 'name': 'GND'}])
    tab.show()
    tab._selected_pin = '1'
    tab.view.setFocus()
    qapp.processEvents()
    original = tab._symbol.pins[0]
    QTest.keyClick(tab.view, Qt.Key.Key_Right)
    assert tab._symbol.pins[0].x == original.x + 2.54
    QTest.keyClick(tab.view, Qt.Key.Key_R)
    assert tab._symbol.pins[0].angle == 270
    def edit(dialog):
        coordinates = dialog.findChildren(QDoubleSpinBox)
        coordinates[0].setValue(50.8)
        coordinates[1].setValue(25.4)
        dialog.findChild(QComboBox).setCurrentIndex(1)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
    monkeypatch.setattr(QDialog, 'exec', edit)
    tab._edit_selected_pin()
    result = tab._symbol.pins[0]
    assert (result.x, result.y, result.angle) == (50.8, 25.4, 90)
    tab.undo()
    result = tab._symbol.pins[0]
    assert (result.x, result.y, result.angle) == (original.x + 2.54, original.y, 270)
    tab.close()
    tab.deleteLater()


def test_noop_resize_and_numeric_accept_keep_project_clean(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 9)])
    initial = tab.draft_rows()
    pin = tab._symbol.pins[0]
    assert tab.resize_body(tab._symbol.width, tab._symbol.height)
    assert tab.set_pin_geometry('1', pin.x, pin.y, pin.angle)
    assert tab.draft_rows() == initial
    assert not tab.has_pending_edits()
    assert len(tab._history) == 1
    tab.deleteLater()


def test_leaving_view_cancels_unfinished_drag_and_project_switch_is_clean(qapp):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtTest import QTest
    tab = SymbolTab()
    tab.resize(1200, 750)
    tab.set_data('Board', [{'pin': str(i), 'name': f'P{i}'} for i in range(1, 9)])
    tab.show()
    qapp.processEvents()
    initial = tab.draft_rows()
    view = tab.view
    def start_drag():
        pin = tab._symbol.pins[0]
        start = view.mapFromScene(QPointF(pin.x * 10, -pin.y * 10))
        end = view.mapFromScene(QPointF(pin.x * 10 - 50.8, -pin.y * 10))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end)
        assert tab.has_pending_edits()
    start_drag()
    tab.hide()
    assert view.gesture is None
    assert tab._gesture_before is None
    assert tab.draft_rows() == initial
    tab.show()
    qapp.processEvents()
    start_drag()
    tab.set_data('Other', [{'pin': '99', 'name': 'NEW'}], force=True)
    assert view.gesture is None
    assert tab._gesture_before is None
    assert not tab.has_pending_edits()
    assert tab.draft_rows()[0]['pin'] == '99'
    assert len(tab._history) == 1
    tab.close()
    tab.deleteLater()


def test_failed_apply_preserves_draft_history_and_retry(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': '1', 'name': 'GND'}])
    baseline = tab.rows()
    tab.place_pin('1', 50.8, 25.4)
    draft = tab.draft_rows()
    history = len(tab._history)
    def fail(_):
        tab.reject_apply('Disk full')
    tab.edits_applied.connect(fail)
    tab.apply()
    assert tab.rows() == baseline
    assert tab.draft_rows() == draft
    assert tab.has_pending_edits()
    assert tab.apply_button.isEnabled()
    assert not tab.export_button.isEnabled()
    assert tab.status.text() == 'Disk full'
    assert len(tab._history) == history
    tab.undo()
    assert tab.draft_rows() == baseline
    tab.redo()
    assert tab.draft_rows() == draft
    tab.edits_applied.disconnect(fail)
    tab.apply()
    assert not tab.has_pending_edits()
    assert tab.rows() == draft
    tab.deleteLater()


def test_escape_ends_background_pan_without_edit(qapp):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    tab = SymbolTab()
    tab.resize(1200, 750)
    tab.set_data('Board', [{'pin': '1', 'name': 'GND'}])
    tab.show()
    qapp.processEvents()
    tab.table.setFocus()
    start = QPoint(15, 15)
    assert not tab.view.items(start)
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(tab.view.viewport(), QPoint(80, 80))
    assert tab.view._pan_last is not None
    QTest.keyClick(tab.view, Qt.Key.Key_Escape)
    assert tab.view._pan_last is None
    assert tab.view.gesture is None
    assert not tab.has_pending_edits()
    assert len(tab._history) == 1
    tab.close()
    tab.deleteLater()


def test_compact_toolbar_overflow_keeps_commands_and_states(qapp):
    from PyQt6.QtWidgets import QToolBar, QToolButton
    tab = SymbolTab()
    tab.resize(800, 750)
    tab.set_data('Board', [{'pin': '1', 'name': 'GND'}])
    tab.table_toggle.defaultAction().setChecked(True)
    tab.show()
    qapp.processEvents()
    toolbar = tab.findChild(QToolBar)
    extension = toolbar.findChild(QToolButton, 'qt_toolbar_ext_button')
    assert extension is not None and extension.isVisible()
    assert tab.table.width() >= 260
    assert tab.apply_button.defaultAction() in toolbar.actions()
    assert not tab.apply_button.defaultAction().isEnabled()
    assert tab.export_button.defaultAction().isEnabled()
    tab.place_pin('1', 50.8, 25.4)
    assert tab.apply_button.defaultAction().isEnabled()
    assert not tab.export_button.defaultAction().isEnabled()
    tab.apply_button.defaultAction().trigger()
    assert not tab.has_pending_edits()
    tab.grid_check.defaultAction().trigger()
    assert not tab.view.show_grid
    assert not tab.grid_check.isChecked()
    for button, _ in tab._icon_buttons:
        assert button.defaultAction().text()
        assert button.defaultAction().toolTip()
    tab.close()
    tab.deleteLater()
