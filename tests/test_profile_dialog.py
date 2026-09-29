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
        assert len(dialog.fields) == 11
        assert all(check.isChecked() for check in dialog.inherit.values())
        dialog.name.setText('My module')
        dialog.family.setCurrentIndex(dialog.family.findData('aggregate_castellated'))
        dialog.instructions.setPlainText('specific instructions')
        dialog.inherit['verification_model'].setChecked(False)
        dialog.fields['verification_model'].setText('custom/verifier')
        dialog.inherit['grounding_prompt'].setChecked(False)
        dialog.fields['grounding_prompt'].setPlainText('custom grounding')
        dialog._save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        key = dialog.key
        assert store.get_type(key)['instructions'] == 'specific instructions'
        assert store.get_type(key)['family'] == 'aggregate_castellated'
        assert store.overrides(key) == {'verification_model': 'custom/verifier', 'grounding_prompt': 'custom grounding'}
    finally:
        dialog.close()
    edit = gui.ProfileEditor(store, key)
    try:
        edit.name.setText('discarded')
        edit.inherit['verification_model'].setChecked(True)
        assert edit.fields['verification_model'].isReadOnly()
        assert edit.fields['verification_model'].text() == store.defaults()['verification_model']
        edit.reject()
        assert store.get_type(key)['label'] == 'My module'
        assert store.resolve(key)['verification_model'] == 'custom/verifier'
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


def test_global_editor_saves_three_independent_models_and_all_prompts(app, store):
    editor = gui.ProfileEditor(store)
    try:
        assert not editor.inherit
        for key in ('model', 'vision_model', 'verification_model'):
            editor.fields[key].setText('vendor/' + key)
        editor.fields['system_prompt'].setPlainText('global custom')
        editor._save()
        assert store.defaults()['system_prompt'] == 'global custom'
        for key in ('model', 'vision_model', 'verification_model'):
            assert store.defaults()[key] == 'vendor/' + key
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


def _note_labels(widget):
    from PyQt6.QtWidgets import QLabel
    return [label for label in widget.findChildren(QLabel)
            if label.objectName() == 'crop_model_note']


@pytest.mark.parametrize('new', [False, True])
def test_the_crop_tab_says_which_model_the_crop_reading_uses(app, store, new):
    """@brief HANDOFF 146 V4: a note only in «Lettura ritagli», not under Terminali or Reofori.

    The crop reading is sent with the model of the paid call on the
    photograph, not with this page's «Modello LLM» (vision_model).
    """
    from PyQt6.QtWidgets import QTabWidget
    editor = gui.ProfileEditor(store, new=new)
    try:
        [sub] = editor.tabs.widget(1).findChildren(QTabWidget)
        pages = {sub.tabText(i): sub.widget(i) for i in range(sub.count())}
        [note] = _note_labels(pages['Lettura ritagli'])
        assert note.wordWrap() and note.text() == gui.CROP_MODEL_NOTE
        assert 'Modello LLM' in note.text()
        for other in ('Terminali', 'Reofori', 'Prompt base', 'Istruzioni foto'):
            assert _note_labels(pages[other]) == []
        assert len(_note_labels(editor)) == 1
    finally:
        editor.close()


def test_the_crop_model_note_is_translated(app, store):
    """@brief The note goes through retranslate_widgets like the other captions, in 4 languages."""
    from PyQt6.QtWidgets import QWidget
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, I18n
    entry = DEFAULT_TRANSLATIONS['ui:' + gui.CROP_MODEL_NOTE]
    assert set(entry) == {'it', 'en', 'fr', 'de'} and entry['it'] == gui.CROP_MODEL_NOTE
    parent = QWidget()
    parent.i18n = I18n(store, 'en')
    editor = gui.ProfileEditor(store, parent=parent)
    try:
        [note] = _note_labels(editor)
        assert note.text() == entry['en']
    finally:
        editor.close()
        parent.close()


# ── crop_model: «Modello lettura ritagli» line above the crop prompt (HANDOFF 150) ──

def _crop_pages(editor):
    from PyQt6.QtWidgets import QTabWidget
    [sub] = editor.tabs.widget(1).findChildren(QTabWidget)
    return {sub.tabText(i): sub.widget(i) for i in range(sub.count())}


@pytest.mark.parametrize('new', [False, True])
def test_the_crop_model_line_sits_in_the_crop_tab_only_with_its_picker(app, store, new):
    from PyQt6.QtWidgets import QLabel, QLineEdit, QPushButton
    editor = gui.ProfileEditor(store, new=new)
    try:
        pages = _crop_pages(editor)
        page = pages['Lettura ritagli']
        [field] = [w for w in page.findChildren(QLineEdit) if w.objectName() == 'crop_model']
        assert field is editor.fields['crop_model'] and field.text() == ''
        [caption] = [w for w in page.findChildren(QLabel) if w.objectName() == 'crop_model_caption']
        assert caption.text() == gui.CROP_MODEL_CAPTION
        assert [b.text() for b in page.findChildren(QPushButton)] == ['Scegli modello…']
        # The line comes before the prompt editor, the note between them.
        layout = page.layout()
        order = [layout.itemAt(i).widget() or layout.itemAt(i).layout() for i in range(layout.count())]
        assert order.index(caption) < order.index(editor.fields['crop_prompt'])
        assert order.index(caption) < order.index(_note_labels(page)[0]) < order.index(editor.fields['crop_prompt'])
        for other in ('Terminali', 'Reofori', 'Prompt base', 'Istruzioni foto'):
            assert not [w for w in pages[other].findChildren(QLineEdit) if w.objectName() == 'crop_model']
            assert not pages[other].findChildren(QPushButton)
        assert len([w for w in editor.findChildren(QLineEdit) if w.objectName() == 'crop_model']) == 1
    finally:
        editor.close()


@pytest.mark.parametrize('typed,stored', [('vendor/crop-reader', 'vendor/crop-reader'), ('', ''),
                                          ('  vendor/crop-reader  ', 'vendor/crop-reader')])
def test_the_global_crop_model_saves_blank_or_stripped(app, store, typed, stored):
    store.save_defaults({'crop_model': 'vendor/previous'})
    editor = gui.ProfileEditor(store)
    try:
        assert editor.fields['crop_model'].text() == 'vendor/previous'
        editor.fields['crop_model'].setText(typed)
        editor._save()
        assert editor.result() == QDialog.DialogCode.Accepted
        assert store.defaults()['crop_model'] == stored
    finally:
        editor.close()


def test_crop_model_inherits_read_only_in_a_type_until_unticked(app, store):
    store.save_defaults({'crop_model': 'vendor/global-crop'})
    editor = gui.ProfileEditor(store, new=True)
    try:
        field = editor.fields['crop_model']
        assert editor.inherit['crop_model'].isChecked() and field.isReadOnly()
        assert field.text() == 'vendor/global-crop'
        editor.name.setText('T')
        editor._save()
        assert 'crop_model' not in store.overrides(editor.key)
    finally:
        editor.close()
    edit = gui.ProfileEditor(store, editor.key)
    try:
        edit.inherit['crop_model'].setChecked(False)
        assert not edit.fields['crop_model'].isReadOnly()
        edit.fields['crop_model'].setText('vendor/type-crop')
        edit._save()
        assert store.overrides(editor.key) == {'crop_model': 'vendor/type-crop'}
        assert store.defaults()['crop_model'] == 'vendor/global-crop'
    finally:
        edit.close()
    # A type may also say «the photo model» while the global default names one.
    blank = gui.ProfileEditor(store, editor.key)
    try:
        blank.fields['crop_model'].setText('')
        blank._save()
        assert blank.result() == QDialog.DialogCode.Accepted
        assert store.overrides(editor.key) == {'crop_model': ''}
        assert store.resolve(editor.key)['crop_model'] == ''
    finally:
        blank.close()


def test_the_crop_model_caption_and_note_are_translated(app, store):
    from PyQt6.QtWidgets import QLabel, QWidget
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, I18n
    for source in (gui.CROP_MODEL_CAPTION, gui.CROP_MODEL_NOTE):
        entry = DEFAULT_TRANSLATIONS['ui:' + source]
        assert set(entry) == {'it', 'en', 'fr', 'de'} and entry['it'] == source
        assert len({entry[lang] for lang in entry}) == 4
    parent = QWidget()
    parent.i18n = I18n(store, 'de')
    editor = gui.ProfileEditor(store, parent=parent)
    try:
        [caption] = [w for w in editor.findChildren(QLabel) if w.objectName() == 'crop_model_caption']
        assert caption.text() == DEFAULT_TRANSLATIONS['ui:' + gui.CROP_MODEL_CAPTION]['de']
        [note] = _note_labels(editor)
        assert note.text() == DEFAULT_TRANSLATIONS['ui:' + gui.CROP_MODEL_NOTE]['de']
    finally:
        editor.close()
        parent.close()
