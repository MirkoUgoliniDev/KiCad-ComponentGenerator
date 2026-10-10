"""Regression cases found by the adversarial editor review."""
from copy import deepcopy

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from kicadgen.gui.symbol_tab import SymbolTab
from kicadgen.services.symbol_draft import load_draft


def reviewed_rows():
    return [dict(pin='1', name='GPIO1', electrical_type='bidirectional',
                 electrical_type_reviewed=True,
                 electrical_type_evidence={'source': 'datasheet p. 4', 'reason': 'I/O'}),
            dict(pin='2', name='GND', electrical_type='power_in',
                 electrical_type_reviewed=True)]


def test_renaming_revokes_evidence_and_review_with_atomic_undo(qapp, tmp_path):
    tab = SymbolTab()
    tab.set_data('Board', reviewed_rows())
    baseline = tab.draft_rows()
    path = tmp_path / '.symbol-draft.json'
    tab.set_recovery_path(path)
    tab.table.item(0, 1).setText('VCC')
    changed = tab.draft_rows()
    assert changed[0]['electrical_type'] == 'bidirectional'
    assert changed[0]['electrical_type_reviewed'] is False
    assert 'electrical_type_evidence' not in changed[0]
    assert changed[1] == baseline[1]
    assert any('Tipo elettrico da verificare: 1' in issue for issue in tab.review_issues())
    assert load_draft(path, baseline) == changed
    tab.undo()
    assert tab.draft_rows() == baseline
    assert not path.exists()
    tab.redo()
    assert tab.draft_rows() == changed
    assert load_draft(path, baseline) == changed
    tab.deleteLater()


def test_geometry_and_translation_preserve_electrical_evidence(qapp):
    tab = SymbolTab()
    tab.set_data('Board', reviewed_rows())
    baseline = tab.draft_rows()
    tab._select_pins({'1', '2'}, '1')
    assert tab._transform_selected('move', (5.08, 5.08))
    tab.retranslate()
    for before, after in zip(baseline, tab.draft_rows()):
        for key in ('name', 'electrical_type', 'electrical_type_reviewed', 'electrical_type_evidence'):
            assert before.get(key) == after.get(key)
    assert not any('Tipo elettrico da verificare' in issue for issue in tab.review_issues())
    tab.deleteLater()


def test_invalid_loaded_type_cannot_keep_reviewed_proof(qapp):
    rows = deepcopy(reviewed_rows())
    rows[0]['electrical_type'] = 'invalid-provider-type'
    tab = SymbolTab()
    tab.set_data('Board', rows)
    row = tab.draft_rows()[0]
    assert row['electrical_type'] == 'passive'
    assert row['electrical_type_reviewed'] is False
    assert 'electrical_type_evidence' not in row
    assert any('Tipo elettrico da verificare: 1' in issue for issue in tab.review_issues())
    tab.deleteLater()


def test_explicit_confirmation_of_current_passive_type_is_undoable(qapp):
    tab = SymbolTab()
    tab.set_data('Board', [dict(pin='1', name='CONTACT')])
    tab.table.show()
    tab.show()
    qapp.processEvents()
    combo = tab.table.cellWidget(0, 2)
    assert combo.currentData() == 'passive'
    assert any('Tipo elettrico da verificare' in issue for issue in tab.review_issues())
    combo.showPopup()
    qapp.processEvents()
    # Enter confirms the current item without a currentIndexChanged signal.
    QTest.keyClick(combo.view(), Qt.Key.Key_Return)
    assert tab.draft_rows()[0]['electrical_type_reviewed'] is True
    assert not any('Tipo elettrico da verificare' in issue for issue in tab.review_issues())
    tab.undo()
    assert not tab.draft_rows()[0].get('electrical_type_reviewed')
    tab.close()
    tab.deleteLater()
