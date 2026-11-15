from types import SimpleNamespace
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QDialog, QMessageBox
from kicadgen.gui import pin_type_dialog


def test_verification_is_guarded_and_uses_applied_snapshot(window, monkeypatch):
    # The pending draft would ask in a modal (2026-10-14): answer «stay».
    monkeypatch.setattr(window, '_ask_pending_edits_choice', lambda editors, can_apply: window.PENDING_EDITS_STAY)
    window.symbol_tab.set_data('Demo', [{'pin': '1', 'name': 'GND'}])
    window.symbol_tab.table.item(0, 1).setText('Draft')
    captured = []

    def run(args, actions=None, keep_status=False):
        # CONTRACT CHANGED 2026-11-14 (review P2): the window gives an args BUILDER,
        # called for every run; the stub calls it once.
        captured.append(args() if callable(args) else args)
        return True
    # CONTRACT CHANGED 2026-11-15 (guided KiCad tab): the check runs in the panel
    # of the KiCad tab, not in a modal dialog, and does not count as work in
    # progress: it reads scratch copies, so nothing else has to wait for it
    # (a file changed meanwhile only makes its outcome stale).
    monkeypatch.setattr(window.kicad_check_panel, 'run', run)
    window._verify_component()
    assert captured[0]['pending']
    assert captured[0]['rows'][0]['name'] == 'GND'
    assert not window._background_work_running()


def test_verification_refuses_concurrent_generation(window, monkeypatch):
    window._automatic_generation_active = True
    notices = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: notices.append(args))
    started = []
    monkeypatch.setattr(window.kicad_check_panel, 'run', lambda *a, **kw: started.append(a))
    window._verify_component()
    assert len(notices) == 1 and started == []
    window._automatic_generation_active = False


def test_ai_account_binding_only_during_analysis(window, monkeypatch, tmp_path):
    window.symbol_tab.set_data('Demo', [{'pin': '1', 'name': 'GND'}])
    monkeypatch.setattr(window, '_selected_documents', lambda: ([tmp_path/'sheet.pdf'], {'sheet.pdf': ['datasheet']}))
    monkeypatch.setattr(window, '_ensure_api_key', lambda *args, **kwargs: True)
    events = []
    monkeypatch.setattr(window.ai_account, 'bind_run_project', lambda p: events.append('bind'))
    monkeypatch.setattr(window.ai_account, 'release_run_project', lambda: events.append('release'))
    class Dialog(QObject):
        analysis_started = pyqtSignal()
        analysis_finished = pyqtSignal()
        # CONTRACT CHANGED 2026-11-13: `actions` (the dialog's fixes) accepted, ignored.
        def __init__(self, args, parent, tx, actions=None):
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
