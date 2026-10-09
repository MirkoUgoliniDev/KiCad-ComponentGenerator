from copy import deepcopy
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog
from kicadgen.gui.pinout_dialog import PinoutDialog


def test_editor_preserves_identity_and_does_not_mutate_input(qapp):
    original = [{'pin': 10, 'name': 'G', 'source': 'photo'}, {'pin': '2', 'name': '5V'}]
    before = deepcopy(original)
    dialog = PinoutDialog(original)
    assert not dialog.table.item(1, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert dialog.table.item(1, 1).flags() & Qt.ItemFlag.ItemIsEditable
    dialog.table.item(1, 1).setText(' GND ')
    assert dialog.edited_pinout() == [dict(before[0], name='GND'), before[1]]
    dialog.reject()
    assert original == before
    dialog.deleteLater()


@pytest.mark.parametrize('accepted,saved', [(False, True), (True, False), (True, True)])
def test_apply_saves_before_refresh_and_cancel_or_failure_keeps_old_names(window, monkeypatch, accepted, saved):
    from kicadgen.gui import main_window as gui
    window.pinout = [{'pin': 1, 'name': 'G'}]
    calls = []
    def edit(dialog):
        dialog.table.item(0, 1).setText('GND')
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
    monkeypatch.setattr(PinoutDialog, 'exec', edit)
    monkeypatch.setattr(window, '_save_project_config', lambda **kw: calls.append('save') or saved)
    monkeypatch.setattr(window, '_draw_preview', lambda: calls.append('2d'))
    monkeypatch.setattr(window, '_sync_3d_pin_functions', lambda: calls.append('3d'))
    monkeypatch.setattr(gui.QMessageBox, 'warning', lambda *a: None)
    window._show_pinout_dialog()
    assert window.pinout[0]['name'] == ('GND' if accepted and saved else 'G')
    assert calls == (['save', '2d', '3d'] if accepted and saved else ['save'] if accepted else [])
