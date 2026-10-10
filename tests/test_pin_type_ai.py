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


def test_ai_evidence_tooltip_does_not_interpret_markup(qapp, tmp_path):
    from kicadgen.gui.pin_type_dialog import PinTypeDialog
    dialog = PinTypeDialog(dict(name='Demo', rows=ROWS, documents=[tmp_path/'sheet.pdf'], model='test'))
    p = proposal()
    p['reason'] = '<img src="https://example.invalid/pixel">'
    dialog._done([p])
    assert '<img' not in dialog.table.item(0, 5).toolTip()
    assert '&lt;img' in dialog.table.item(0, 5).toolTip()
    dialog.close()

@pytest.mark.parametrize('field,value', [('reason', None), ('reason', {}), ('source', ['sheet.pdf']), ('pin', 1)])
def test_rejects_malformed_evidence_schema(field, value):
    item = proposal()
    item[field] = value
    with pytest.raises(ValueError):
        ai.validate_suggestions({'pins': [item]}, ROWS, ['sheet.pdf'])


def test_duplicate_document_names_rejected_before_call(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a: pytest.fail('network'))
    with pytest.raises(ValueError, match='nomi distinti'):
        ai.suggest_pin_types('Demo', ROWS, [tmp_path/'a'/'same.pdf', tmp_path/'b'/'same.pdf'], 'model')


def test_cancellation_before_document_load(tmp_path, monkeypatch):
    from kicadgen.services.ai_diagnostics import CallCancelled
    monkeypatch.setattr(ai, '_file_to_content_block', lambda p: pytest.fail('read after cancellation'))
    def cancelled():
        raise CallCancelled()
    with pytest.raises(CallCancelled):
        ai.suggest_pin_types('Demo', ROWS, [tmp_path/'sheet.pdf'], 'model', cancelled=cancelled)


def test_unknown_completion_preserves_row_order():
    rows = [{'pin': str(i)} for i in range(20)]
    assert [p['pin'] for p in ai.validate_suggestions({'pins': []}, rows, [])] == [r['pin'] for r in rows]


def test_cancel_running_dialog_stops_retry_and_closes_when_finished(qapp, tmp_path, monkeypatch):
    import threading
    from PyQt6.QtTest import QTest
    from kicadgen.gui import pin_type_dialog as ui
    started, release = threading.Event(), threading.Event()
    def fake(**kwargs):
        started.set()
        release.wait(3)
        kwargs['cancelled']()
        pytest.fail('cancel ignored')
    monkeypatch.setattr(ui, 'suggest_pin_types', fake)
    dialog = ui.PinTypeDialog(dict(name='Demo', rows=ROWS, documents=[tmp_path/'sheet.pdf'], model='test'))
    lifecycle = []
    dialog.analysis_started.connect(lambda: lifecycle.append('start'))
    dialog.analysis_finished.connect(lambda: lifecycle.append('finish'))
    dialog.show()
    dialog._start()
    assert started.wait(2)
    dialog.reject()
    assert dialog.worker.isInterruptionRequested()
    release.set()
    assert dialog.worker.wait(3000)
    QTest.qWait(30)
    assert not dialog.isVisible()
    assert dialog.selected() == []
    assert lifecycle == ['start', 'finish']


def test_failed_analysis_can_retry(qapp, tmp_path, monkeypatch):
    from PyQt6.QtTest import QTest
    from kicadgen.gui import pin_type_dialog as ui
    def failure(**kwargs):
        raise ValueError('bad response')
    monkeypatch.setattr(ui, 'suggest_pin_types', failure)
    dialog = ui.PinTypeDialog(dict(name='Demo', rows=ROWS, documents=[tmp_path/'sheet.pdf'], model='test'))
    dialog._start()
    assert dialog.worker.wait(3000)
    QTest.qWait(30)
    assert dialog.start.isEnabled()
    assert not dialog.use.isEnabled()
    monkeypatch.setattr(ui, 'suggest_pin_types', lambda **kwargs: [proposal()])
    dialog._start()
    assert dialog.worker.wait(3000)
    QTest.qWait(30)
    assert dialog.use.isEnabled()
    assert dialog.selected() == [proposal()]
    dialog.close()


def test_cancellation_discards_response_after_paid_call(tmp_path, monkeypatch):
    from kicadgen.services.ai_diagnostics import CallCancelled
    state = {'cancelled': False}
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a: None)
    monkeypatch.setattr(ai, '_file_to_content_block', lambda p: {'type': 'text', 'text': 'evidence'})
    def response(**kwargs):
        state['cancelled'] = True
        return {'pins': [proposal()]}
    def cancelled():
        if state['cancelled']:
            raise CallCancelled()
    monkeypatch.setattr(ai, 'call_tool', response)
    with pytest.raises(CallCancelled):
        ai.suggest_pin_types('Demo', ROWS, [tmp_path/'sheet.pdf'], 'test', cancelled=cancelled)


def test_use_tracks_selected_checkboxes(qapp, tmp_path):
    from PyQt6.QtCore import Qt
    from kicadgen.gui.pin_type_dialog import PinTypeDialog
    dialog = PinTypeDialog(dict(name='Demo', rows=ROWS, documents=[tmp_path/'sheet.pdf'], model='test'))
    dialog._done([proposal()])
    assert dialog.use.isEnabled()
    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.use.isEnabled()
    dialog.close()
