import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
pytest.importorskip('PyQt6')
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen.gui import profile_dialog as gui
from kicadgen.services.analysis_profiles import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / 'ai.sqlite3')


def test_new_profile_save_inheritance_and_cancel(app, store):
    dialog = gui.ProfileEditor(store, new=True)
    try:
        assert dialog.tabs.count() == 3
        # Prompts only: the model is one for every analysis (user, 2026-10-02).
        assert set(dialog.fields) == {'system_prompt', 'grounding_prompt', 'image_instructions',
                                      'terminal_prompt', 'lead_prompt', 'crop_prompt',
                                      'verification_prompt', 'system_prompt_single',
                                      'numbering_prompt'}
        assert all(check.isChecked() for check in dialog.inherit.values())
        dialog.name.setText('My module')
        dialog.family.setCurrentIndex(dialog.family.findData('aggregate_castellated'))
        dialog.instructions.setPlainText('specific instructions')
        dialog.inherit['verification_prompt'].setChecked(False)
        dialog.fields['verification_prompt'].setPlainText('custom verifier')
        dialog.inherit['grounding_prompt'].setChecked(False)
        dialog.fields['grounding_prompt'].setPlainText('custom grounding')
        dialog._save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        key = dialog.key
        assert store.get_type(key)['instructions'] == 'specific instructions'
        assert store.get_type(key)['family'] == 'aggregate_castellated'
        assert store.overrides(key) == {'verification_prompt': 'custom verifier',
                                        'grounding_prompt': 'custom grounding'}
    finally:
        dialog.close()
    edit = gui.ProfileEditor(store, key)
    try:
        edit.name.setText('discarded')
        edit.inherit['verification_prompt'].setChecked(True)
        assert edit.fields['verification_prompt'].isReadOnly()
        assert edit.fields['verification_prompt'].toPlainText() == store.defaults()['verification_prompt']
        edit.reject()
        assert store.get_type(key)['label'] == 'My module'
        assert store.resolve(key)['verification_prompt'] == 'custom verifier'
    finally:
        edit.close()


def test_invalid_save_keeps_editor_open_and_database_unchanged(app, store, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))
    editor = gui.ProfileEditor(store, new=True)
    try:
        original = store.list_types(True)
        editor._save()
        assert warnings
        assert editor.result() != QDialog.DialogCode.Accepted
        assert store.list_types(True) == original
    finally:
        editor.close()


def test_global_editor_saves_prompts_and_never_a_model(app, store):
    """@brief Negative: no model field in the profile editor; the global model is untouched."""
    before = store.defaults()['model']
    editor = gui.ProfileEditor(store)
    try:
        assert not editor.inherit
        assert not any(key.endswith('model') for key in editor.fields)
        editor.fields['system_prompt'].setPlainText('global custom')
        editor._save()
        assert store.defaults()['system_prompt'] == 'global custom'
        assert store.defaults()['model'] == before
    finally:
        editor.close()


def test_manager_duplicate_disable_and_confirmed_delete(app, store, monkeypatch):
    manager = gui.ProfilesDialog(store)
    try:
        assert manager.list.count() == 5
        assert not manager.delete_button.isEnabled()
        assert not manager.copy_button.isEnabled()
        assert not manager.edit_button.icon().isNull()
        assert not manager.copy_button.icon().isNull()
        manager.list.setCurrentRow(1)
        source = manager._key()
        manager.copy_button.click()
        copy = manager._key()
        assert copy != source
        assert manager.list.count() == 6
        assert manager.changed
        store.save_type(copy, label='Inactive', family='single_smd', active=False)
        manager._reload(copy)
        assert 'disattivata' in manager.list.currentItem().text()
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.No)
        manager.delete_button.click()
        assert store.get_type(copy) is not None
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
        manager.delete_button.click()
        assert store.get_type(copy) is None
        assert store.get_type(source) is not None
        assert manager.list.count() == 5
    finally:
        manager.close()


@pytest.mark.parametrize('index', range(3))
def test_info_buttons_route_to_correct_stage_without_changing_fields(app, store, monkeypatch, index):
    calls = []
    monkeypatch.setattr(gui, 'show_info', lambda *args, **kwargs: calls.append(args))
    editor = gui.ProfileEditor(store, 'single_smd')
    try:
        editor.tabs.setCurrentIndex(index)
        before = store.resolve('single_smd')
        button = editor.info_buttons[index]
        assert not button.icon().isNull()
        button.click()
        assert calls[0][0] is editor
        assert calls[0][1] == gui.STAGES[index][0]
        assert gui.HELP[index] in calls[0][2]
        assert store.resolve('single_smd') == before
    finally:
        editor.close()


def test_crop_prompt_is_editable_next_to_the_other_photo_prompts(app, store):
    """@brief The crop reading prompt has its own sub-tab in the photo stage (HANDOFF 146)."""
    from PyQt6.QtWidgets import QTabWidget
    from kicadgen.services.photo_crop_reading import SYSTEM_PROMPT
    editor = gui.ProfileEditor(store)
    try:
        field = editor.fields['crop_prompt']
        assert field.toPlainText() == SYSTEM_PROMPT and not field.isReadOnly()
        page = editor.tabs.widget(1)
        [sub] = page.findChildren(QTabWidget)
        captions = [sub.tabText(i) for i in range(sub.count())]
        assert captions[-3:] == ['Terminali', 'Reofori', 'Lettura ritagli']
        assert sub.widget(captions.index('Lettura ritagli')).isAncestorOf(field)
        field.setPlainText('global crop')
        editor._save()
        assert store.defaults()['crop_prompt'] == 'global crop'
    finally:
        editor.close()


def test_crop_prompt_inherits_read_only_in_a_type_until_unticked(app, store):
    store.save_defaults({'crop_prompt': 'global crop'})
    editor = gui.ProfileEditor(store, new=True)
    try:
        field = editor.fields['crop_prompt']
        assert editor.inherit['crop_prompt'].isChecked() and field.isReadOnly()
        assert field.toPlainText() == 'global crop'
        editor.name.setText('Crop type')
        editor.instructions.setPlainText('x')
        editor._save()
        assert 'crop_prompt' not in store.overrides(editor.key)
    finally:
        editor.close()
    edit = gui.ProfileEditor(store, editor.key)
    try:
        edit.inherit['crop_prompt'].setChecked(False)
        assert not edit.fields['crop_prompt'].isReadOnly()
        edit.fields['crop_prompt'].setPlainText('type crop')
        edit._save()
        assert store.overrides(editor.key) == {'crop_prompt': 'type crop'}
        assert store.defaults()['crop_prompt'] == 'global crop'
    finally:
        edit.close()
