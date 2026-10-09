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
