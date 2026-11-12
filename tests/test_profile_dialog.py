import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path

import pytest
pytest.importorskip('PyQt6')
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen.gui import profile_dialog as gui
from kicadgen.services.analysis_profiles import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / 'ai.sqlite3')


def test_new_profile_save_inheritance_and_cancel(app, store, monkeypatch):
    dialog = gui.ProfileEditor(store, new=True)
    try:
        assert dialog.tabs.count() == 3
        # Prompts only: the model is one for every analysis (user, 2026-10-02).
        # CONTRACT CHANGED 2026-11-11 (step P2): the prompts that lived only in
        # the code are profile fields the analysis sends, so they are edited
        # here too (schema 8 and 9); still no model field.
        from kicadgen.services.analysis_profiles import (SCHEMA8_PROMPT_FIELDS,
                                                         SCHEMA9_PROMPT_FIELDS)
        assert set(dialog.fields) == {'system_prompt', 'grounding_prompt', 'image_instructions',
                                      'terminal_prompt', 'lead_prompt', 'crop_prompt',
                                      'verification_prompt', 'system_prompt_single',
                                      'numbering_prompt', *SCHEMA8_PROMPT_FIELDS,
                                      *SCHEMA9_PROMPT_FIELDS}
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
        # CONTRACT CHANGED 2026-11-12: ticking inherit over the type's own text
        # asks first; the general text comes back, editable.
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
        edit.inherit['verification_prompt'].setChecked(True)
        assert edit.inherit['verification_prompt'].isChecked()
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
    # CONTRACT CHANGED 2026-11-12 (user, option 2): the commands write the
    # manager's WORKING COPY (`manager.store`); the real store changes only on
    # «Salva», the ONE saving button (user: «ne lascerei uno solo»).
    manager = gui.ProfilesDialog(store)
    try:
        assert manager.list.count() == 5
        assert not manager.delete_button.isEnabled()
        assert not manager.copy_button.isEnabled()
        assert not manager.edit_button.icon().isNull()
        assert not manager.copy_button.icon().isNull()
        assert not manager.save_button.isEnabled()
        assert not hasattr(manager, 'apply_button'), "one saving button only"
        assert [b.text() for b in manager.buttons.buttons()].count('Applica') == 0
        # Disabled, it says why; one row with the commands (2026-11-12).
        assert manager.save_button.text() == 'Salva'
        assert 'niente da salvare' in manager.save_button.toolTip()
        manager.show()
        assert manager.save_button.mapTo(manager, manager.save_button.rect().center()).y() == pytest.approx(
            manager.new_button.mapTo(manager, manager.new_button.rect().center()).y(), abs=6)
        manager.list.setCurrentRow(1)
        source = manager._key()
        manager.copy_button.click()
        copy = manager._key()
        assert copy != source
        assert manager.list.count() == 6
        assert manager.pending == 1 and not manager.changed
        assert manager.save_button.isEnabled() and manager.save_button.text() == 'Salva (1)'
        assert store.get_type(copy) is None, "nothing written before Salva"
        manager.store.save_type(copy, label='Inactive', family='single_smd', active=False)
        manager._reload(copy)
        assert 'disattivata' in manager.list.currentItem().text()
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.No)
        manager.delete_button.click()
        assert manager.store.get_type(copy) is not None and manager.pending == 1
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
        manager.delete_button.click()
        assert manager.store.get_type(copy) is None and manager.pending == 2
        assert manager.store.get_type(source) is not None
        assert manager.list.count() == 5
    finally:
        manager._discard()


def test_save_writes_the_real_database_and_cancel_discards(app, store):
    """@brief Positive «Salva» and negative «Annulla» (2026-11-12)."""
    manager = gui.ProfilesDialog(store)
    work_dir = Path(manager._work_dir)
    try:
        manager.list.setCurrentRow(1)
        manager.copy_button.click()
        copy = manager._key()
        manager.copy_button.click()
        second = manager._key()
        assert manager.save_button.text() == 'Salva (2)'
        manager.save_button.click()
        assert store.get_type(copy) is not None and store.get_type(second) is not None
        assert manager.changed and manager.result() == QDialog.DialogCode.Accepted
    finally:
        manager.done(0)
    assert not work_dir.exists(), "the working copy is removed when the dialog finishes"
    other = gui.ProfilesDialog(store)
    try:
        other.list.setCurrentRow(1)
        other.copy_button.click()
        dropped = other._key()
        other.cancel_button.click()
        assert store.get_type(dropped) is None and not other.changed
    finally:
        other.done(0)


def test_apply_refuses_when_the_real_database_changed_meanwhile(app, store, monkeypatch):
    """@brief Negative: another writer's change is never erased by «Salva»."""
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[2]))
    manager = gui.ProfilesDialog(store)
    try:
        manager.list.setCurrentRow(1)
        manager.copy_button.click()
        copy = manager._key()
        other_key = ProfileStore(store.path).save_type(None, label='Altra finestra', family='single_smd')
        manager.save_button.click()
        assert warnings and manager.pending == 1 and manager.result() != QDialog.DialogCode.Accepted
        assert store.get_type(copy) is None and store.get_type(other_key) is not None
    finally:
        manager._discard()


@pytest.mark.parametrize('choice, applied, closed', [('Salva', True, True), ('Scarta', False, True),
                                                      ('Resta', False, False)])
def test_closing_with_pending_changes_asks(app, store, monkeypatch, choice, applied, closed):
    """@brief Esc / close box with pending changes: Salva, Scarta or Resta."""
    def exec_choosing(box):
        box._chosen = next(b for b in box.buttons() if b.text() == choice)
        return 0
    monkeypatch.setattr(QMessageBox, 'exec', exec_choosing)
    monkeypatch.setattr(QMessageBox, 'clickedButton', lambda box: box._chosen)
    manager = gui.ProfilesDialog(store)
    manager.show()
    try:
        manager.list.setCurrentRow(1)
        manager.copy_button.click()
        copy = manager._key()
        manager.reject()
        assert (store.get_type(copy) is not None) is applied
        assert manager.isVisible() is not closed
    finally:
        manager._discard()


def test_closing_without_pending_changes_does_not_ask(app, store, monkeypatch):
    """@brief Negative: nothing pending, no question."""
    monkeypatch.setattr(QMessageBox, 'exec', lambda box: pytest.fail('asked without pending changes'))
    manager = gui.ProfilesDialog(store)
    manager.reject()
    assert manager.result() == QDialog.DialogCode.Rejected


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
        # CONTRACT CHANGED 2026-11-11 (step P2): the photo stage now also holds
        # the wired registry prompts (one nested group, «Inventario
        # fotografico») and the read-only «Testi nel codice»: the stage's own
        # sub-tab row is the direct child, and the hand-made tabs come first.
        from PyQt6.QtCore import Qt
        [sub] = page.findChildren(QTabWidget, '', Qt.FindChildOption.FindDirectChildrenOnly)
        captions = [sub.tabText(i) for i in range(sub.count())]
        # CONTRACT CHANGED 2026-11-12 (UX review, finding 5): a prompt that is an
        # analysis step is captioned with the step's title, as in «Modello AI».
        assert captions[2:5] == ['Terminali', 'Piedini', 'Lettura dei ritagli']
        assert sub.widget(captions.index('Lettura dei ritagli')).isAncestorOf(field)
        field.setPlainText('global crop')
        editor._save()
        assert store.defaults()['crop_prompt'] == 'global crop'
    finally:
        editor.close()


def test_crop_prompt_inherits_in_a_type_until_unticked(app, store):
    # CONTRACT CHANGED 2026-11-12: an inherited prompt is editable (typing
    # detaches it, see the tests below); it is still the general text.
    store.save_defaults({'crop_prompt': 'global crop'})
    editor = gui.ProfileEditor(store, new=True)
    try:
        field = editor.fields['crop_prompt']
        assert editor.inherit['crop_prompt'].isChecked() and not field.isReadOnly()
        assert 'Default generali' in editor.source_labels['crop_prompt'].text()
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


def test_a_vanished_or_foreign_working_copy_never_wipes_the_database(tmp_path, store):
    """@brief Negative (review 2026-11-12, finding 2): restore_from refuses a missing, empty or foreign file."""
    import sqlite3
    from kicadgen.services.analysis_profiles import ProfileError
    before = store.list_types(True)
    for source in (tmp_path / 'vanished.sqlite3', tmp_path / 'empty.sqlite3', tmp_path / 'foreign.sqlite3'):
        if source.name == 'empty.sqlite3':
            source.write_bytes(b'')
        if source.name == 'foreign.sqlite3':
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE other (x)')
        with pytest.raises(ProfileError):
            store.restore_from(source, store.contents_signature())
        assert not (tmp_path / 'vanished.sqlite3').exists(), "a lookup never creates the file"
    assert store.list_types(True) == before


def test_enter_in_the_list_opens_the_type_and_never_applies(app, store, monkeypatch):
    """@brief Negative (review 2026-11-12, finding 3): Enter never applies and closes."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    opened = []
    manager = gui.ProfilesDialog(store)
    monkeypatch.setattr(manager, '_edit', lambda: opened.append(manager._key()))
    manager.show()
    try:
        manager.list.setCurrentRow(1)
        manager.copy_button.click()
        copy = manager._key()
        manager.list.setFocus()
        QTest.keyClick(manager.list, Qt.Key.Key_Return)
        assert manager.isVisible() and store.get_type(copy) is None and manager.pending == 1
        assert opened == [copy]
        # QDialogButtonBox makes «Salva» the default again when shown; the
        # contract is the key's effect, so Enter on the dialog itself too.
        QTest.keyClick(manager, Qt.Key.Key_Enter)
        assert manager.isVisible() and store.get_type(copy) is None and manager.pending == 1
    finally:
        manager._discard()


def test_a_database_with_the_old_help_serves_the_new_one(tmp_path):
    """@brief Positive (review 2026-11-12, finding 1): the stored «saved at once» help is replaced."""
    import sqlite3
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, SUPERSEDED_DEFAULTS
    path = tmp_path / 'old.sqlite3'
    ProfileStore(path)
    old = SUPERSEDED_DEFAULTS['help.profile.management']['it'][0]
    assert 'salvata subito' in old
    with sqlite3.connect(path) as db:
        db.execute("UPDATE translations SET text=? WHERE key='help.profile.management' AND language='it'", (old,))
    store = ProfileStore(path)
    assert store.translation('help.profile.management', 'it') == DEFAULT_TRANSLATIONS['help.profile.management']['it']
    assert 'salvata subito' not in store.translation('help.profile.management', 'it')



def test_typing_in_an_inherited_prompt_makes_it_the_type_own_text(app, store):
    """@brief Positive (user, 2026-11-12): editing the prompt of a type stops it inheriting."""
    store.save_defaults({'crop_prompt': 'global crop'})
    key = store.save_type(None, label='SMD mio', family='single_smd')
    editor = gui.ProfileEditor(store, key)
    try:
        field = editor.fields['crop_prompt']
        assert editor.inherit['crop_prompt'].isChecked()
        field.setPlainText('crop of this type')
        assert not editor.inherit['crop_prompt'].isChecked()
        assert "Testo proprio di «SMD mio»" in editor.source_labels['crop_prompt'].text()
        editor._save()
        assert store.overrides(key) == {'crop_prompt': 'crop of this type'}
        assert store.defaults()['crop_prompt'] == 'global crop', "the general text is untouched"
    finally:
        editor.close()


def test_retyping_the_general_text_keeps_inheriting_and_no_keeps_the_own_text(app, store, monkeypatch):
    """@brief Negatives: a text equal to the general one does not detach; «No» to the question keeps the own text."""
    store.save_defaults({'crop_prompt': 'global crop'})
    key = store.save_type(None, label='SMD mio', family='single_smd')
    editor = gui.ProfileEditor(store, key)
    try:
        field = editor.fields['crop_prompt']
        field.setPlainText('global crop')
        assert editor.inherit['crop_prompt'].isChecked(), "same text as the general one: still inherited"
        field.setPlainText('own')
        assert not editor.inherit['crop_prompt'].isChecked()
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.No)
        editor.inherit['crop_prompt'].setChecked(True)
        assert not editor.inherit['crop_prompt'].isChecked() and field.toPlainText() == 'own'
        monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
        editor.inherit['crop_prompt'].setChecked(True)
        assert editor.inherit['crop_prompt'].isChecked() and field.toPlainText() == 'global crop'
        assert 'Default generali' in editor.source_labels['crop_prompt'].text()
    finally:
        editor.close()



def test_a_change_undone_ticks_inherit_back_and_saves_no_copy(app, store):
    """@brief Review 2026-11-12, finding 1: a key and Backspace, Ctrl+Z or an added blank line
    never leave an override equal to the general text; a box unticked by hand stays unticked."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QTextCursor
    from PyQt6.QtTest import QTest
    store.save_defaults({'crop_prompt': 'global crop'})
    key = store.save_type(None, label='SMD mio', family='single_smd')
    editor = gui.ProfileEditor(store, key)
    try:
        field = editor.fields['crop_prompt']
        field.moveCursor(QTextCursor.MoveOperation.End)
        QTest.keyClick(field, Qt.Key.Key_X)
        assert not editor.inherit['crop_prompt'].isChecked()
        QTest.keyClick(field, Qt.Key.Key_Backspace)
        assert editor.inherit['crop_prompt'].isChecked(), "back to the general text: inherited again"
        QTest.keyClick(field, Qt.Key.Key_Return)
        assert editor.inherit['crop_prompt'].isChecked(), "a blank line is not an edit"
        QTest.keyClick(field, Qt.Key.Key_Y)
        field.undo()
        assert editor.inherit['crop_prompt'].isChecked()
        editor._save()
        assert 'crop_prompt' not in store.overrides(key)
    finally:
        editor.close()
    edit = gui.ProfileEditor(store, key)
    try:
        edit.inherit['crop_prompt'].setChecked(False)           # by hand: the user's choice
        edit.fields['crop_prompt'].setPlainText('global crop')
        assert not edit.inherit['crop_prompt'].isChecked()
    finally:
        edit.close()


def test_invisible_differences_do_not_detach_or_ask(app, store, monkeypatch):
    """@brief Review 2026-11-12, finding 2: CRLF and outer blanks of the general text are not an edit."""
    asked = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: asked.append(args) or QMessageBox.StandardButton.No)
    store.save_defaults({'crop_prompt': 'line one\r\nline two'})
    key = store.save_type(None, label='SMD mio', family='single_smd')
    editor = gui.ProfileEditor(store, key)
    try:
        assert editor.inherit['crop_prompt'].isChecked()
        editor.fields['crop_prompt'].setPlainText('line one\nline two\n')
        assert editor.inherit['crop_prompt'].isChecked()
        editor.inherit['crop_prompt'].setChecked(False)
        editor.inherit['crop_prompt'].setChecked(True)
        assert asked == [] and editor.inherit['crop_prompt'].isChecked()
    finally:
        editor.close()
