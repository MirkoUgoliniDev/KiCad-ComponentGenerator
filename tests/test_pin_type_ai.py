import pytest
from kicadgen.services import pin_type_ai as ai
from kicadgen.gui.symbol_tab import SymbolTab

ROWS = [{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': 'GPIO1'}]


def proposal(number='1', kind='power_in', source='sheet.pdf'):
    return dict(pin=number, electrical_type=kind, source=source, reason='Page 2: module supply return.')


@pytest.mark.parametrize('pins', [[proposal('9')], [proposal(), proposal()], [proposal(kind='bogus')]])
def test_rejects_invalid_ai_identity_or_type(pins):
    with pytest.raises(ValueError):
        ai.validate_suggestions({'pins': pins}, ROWS, ['sheet.pdf'])


def test_unknown_sources_and_missing_pins_are_unresolved():
    result = ai.validate_suggestions({'pins': [proposal(source='invented.pdf')]}, ROWS, ['sheet.pdf'])
    assert all(p['electrical_type'] == 'unknown' for p in result)
    assert {p['pin'] for p in result} == {'1', '2'}


def test_call_uses_documents_and_only_pin_metadata(tmp_path, monkeypatch):
    source = tmp_path / 'sheet.pdf'
    source.write_bytes(b'PDF')
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a: None)
    monkeypatch.setattr(ai, '_file_to_content_block', lambda p: {'type': 'file', 'file': {'filename': p.name}})
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        return {'pins': [proposal()]}
    monkeypatch.setattr(ai, 'call_tool', call)
    result = ai.suggest_pin_types('Demo', ROWS, [source], 'model')
    assert calls[0]['has_pdf'] is True
    assert calls[0]['content_blocks'][-1]['type'] == 'file'
    assert 'power direction' in calls[0]['system_prompt']
    assert result[0]['electrical_type'] == 'power_in'


def test_accept_is_one_undoable_draft_preserving_geometry(qapp):
    tab = SymbolTab()
    tab.set_data('Demo', ROWS)
    before = tab.draft_rows()
    positions = [(p.x, p.y, p.angle) for p in tab._symbol.pins]
    tab.use_pin_type_suggestions([proposal()])
    assert tab.rows() == before
    assert tab.draft_rows()[0]['electrical_type_reviewed'] is True
    assert tab.draft_rows()[0]['electrical_type'] == 'power_in'
    assert [(p.x, p.y, p.angle) for p in tab._symbol.pins] == positions
    tab.undo()
    assert tab.draft_rows() == before
    tab.redo()
    assert tab.draft_rows()[0]['electrical_type'] == 'power_in'


def test_review_does_not_preselect_confirmed_or_unknown_types(qapp, tmp_path):
    from kicadgen.gui.pin_type_dialog import PinTypeDialog
    rows = [dict(ROWS[0], electrical_type_reviewed=True), ROWS[1]]
    dialog = PinTypeDialog(dict(name='Demo', rows=rows, documents=[tmp_path/'sheet.pdf'], model='test'))
    dialog._done([proposal(), proposal('2', 'unknown')])
    assert dialog.selected() == []
    dialog.close()
