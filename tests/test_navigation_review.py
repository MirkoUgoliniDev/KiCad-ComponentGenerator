"""Regression checks for navigation when the current project's save fails."""

import pytest


@pytest.mark.parametrize('destination', ['open', 'new', 'root', 'recent', 'close'])
def test_failed_save_keeps_current_project_and_settings(window, monkeypatch, tmp_path, destination):
    from PyQt6.QtWidgets import QDialog, QMessageBox
    from kicadgen.gui import project_dialogs, project_state

    assert window._save_project_config(force=True)
    original_name = window._component_name()
    original_root = window._library_root()
    original_folder = window._project_dir()
    recent_target = tmp_path / 'other-root' / 'RecentComponent'
    project_state.write_project_config(recent_target, {})
    window.spin_header_drill.setValue(.85)
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))

    def fail_write(*args, **kwargs):
        raise OSError('simulated full disk')

    with monkeypatch.context() as failure:
        failure.setattr(project_state, 'write_project_config', fail_write)
        if destination == 'open':
            target = original_root / 'Other'
            (target / 'docs').mkdir(parents=True)
            window._open_project_at(target)
        elif destination == 'new':
            failure.setattr(project_dialogs.ProjectNameDialog, 'exec',
                            lambda dialog: QDialog.DialogCode.Accepted)
            failure.setattr(project_dialogs.ProjectNameDialog, 'project_name',
                            lambda dialog: 'NewComponent')
            window._new_project()
        elif destination == 'root':
            window.field_library_root.setText(str(tmp_path / 'other-root'))
        elif destination == 'close':
            from PyQt6.QtGui import QCloseEvent
            event = QCloseEvent()
            window.closeEvent(event)
            assert not event.isAccepted()
            assert not getattr(window, '_window_closed', False)
        else:
            window._open_recent_project(str(recent_target))

    assert window._component_name() == original_name
    assert window._library_root() == original_root
    assert window.start_page.library_root_text() == str(original_root)
    assert window._open_project_folder == original_folder
    assert window.spin_header_drill.value() == .85
    assert warnings


@pytest.mark.parametrize('discard', [False, True])
def test_refused_config_requires_explicit_discard_of_changed_settings(window, monkeypatch, discard):
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.gui.project_state import PROJECT_CONFIG_NAME
    original = window._component_name()
    folder = window._project_dir()
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / PROJECT_CONFIG_NAME
    config.write_bytes(b'unsupported project configuration')
    window._project_config_refused = folder
    window._last_saved_project_state = window._project_state_text()
    window.spin_header_drill.setValue(.85)
    questions = []

    def answer(*args):
        questions.append(args)
        return QMessageBox.StandardButton.Yes if discard else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, 'question', answer)
    window._return_to_projects()
    assert window._component_name() == ('' if discard else original)
    assert config.read_bytes() == b'unsupported project configuration'
    assert len(questions) == 1
    window._last_saved_project_state = window._project_state_text()


def test_refused_unchanged_config_can_return_to_projects_without_prompt(window):
    window._project_config_refused = window._project_dir()
    window._last_saved_project_state = window._project_state_text()
    window._return_to_projects()
    assert not window._component_name()


def test_recent_project_with_same_name_in_other_root_saves_to_its_own_folder(window, tmp_path):
    from kicadgen.gui.project_state import read_project_config, write_project_config
    assert window._save_project_config(force=True)
    original = window._project_dir()
    target = tmp_path / 'other-root' / original.name
    write_project_config(target, window._project_state())
    window.spin_header_drill.setValue(.8)
    window._open_recent_project(str(target))
    assert window._open_project_folder == target
    assert window._library_root() == target.parent
    assert window.start_page.library_root_text() == str(target.parent)
    assert window.spin_header_drill.value() == 1.0
    window.spin_header_drill.setValue(.9)
    assert window._save_project_config()
    assert read_project_config(original)['host_mounting']['drill'] == .8
    assert read_project_config(target)['host_mounting']['drill'] == .9
