from types import SimpleNamespace
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QDialog, QMessageBox
from kicadgen.gui import component_check_dialog, pin_type_dialog


def test_verification_is_guarded_and_uses_applied_snapshot(window, monkeypatch):
    # The pending draft would ask in a modal (2026-10-14): answer «stay».
    monkeypatch.setattr(window, '_ask_pending_symbol_choice', lambda can_apply: window.SYMBOL_EDITS_STAY)
    window.symbol_tab.set_data('Demo', [{'pin': '1', 'name': 'GND'}])
    window.symbol_tab.table.item(0, 1).setText('Draft')
    captured = []
    class Dialog:
        def __init__(self, args, parent, tx):
            captured.append(args)
        def exec(self):
            assert window._background_work_running()
            assert not window._can_leave_project()
    monkeypatch.setattr(component_check_dialog, 'ComponentCheckDialog', Dialog)
    window._verify_component()
    assert captured[0]['pending']
    assert captured[0]['rows'][0]['name'] == 'GND'
    assert not window._background_work_running()


def test_verification_refuses_concurrent_generation(window, monkeypatch):
    window._automatic_generation_active = True
    notices = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: notices.append(args))
    monkeypatch.setattr(component_check_dialog, 'ComponentCheckDialog', lambda *a, **kw: None)
    window._verify_component()
    assert len(notices) == 1
    window._automatic_generation_active = False


def test_ai_account_binding_only_during_analysis(window, monkeypatch, tmp_path):
    window.symbol_tab.set_data('Demo', [{'pin': '1', 'name': 'GND'}])
    monkeypatch.setattr(window, '_selected_documents', lambda: ([tmp_path/'sheet.pdf'], {'sheet.pdf': ['datasheet']}))
    monkeypatch.setattr(window, '_ensure_api_key', lambda: True)
    events = []
    monkeypatch.setattr(window.ai_account, 'bind_run_project', lambda p: events.append('bind'))
    monkeypatch.setattr(window.ai_account, 'release_run_project', lambda: events.append('release'))
    class Dialog(QObject):
        analysis_started = pyqtSignal()
        analysis_finished = pyqtSignal()
        def __init__(self, args, parent, tx):
            super().__init__()
        def exec(self):
            assert events == []
            assert window._background_work_running()
            self.analysis_started.emit()
            assert events == ['bind']
            self.analysis_finished.emit()
            assert events == ['bind', 'release']
            return QDialog.DialogCode.Rejected
    monkeypatch.setattr(pin_type_dialog, 'PinTypeDialog', Dialog)
    window._suggest_pin_types()
    assert events == ['bind', 'release']
    assert not window._background_work_running()
