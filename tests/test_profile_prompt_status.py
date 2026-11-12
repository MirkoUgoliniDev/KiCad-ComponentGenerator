"""@brief Prompt status in the profile store and the profile dialog (P1 of the 2026-11-11 plan).

A stored prompt that is not the current default and not a KNOWN former
default used to be kept silently; now the store reports its status, the
dialog shows it with a diff and a restore button, and nothing ever changes a
custom text by itself.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest

from kicadgen.services import prompt_catalog as pc
from kicadgen.services.analysis_profiles import (
    PROFILE_FIELDS, SCHEMA8_PROMPT_FIELDS, SCHEMA9_PROMPT_FIELDS, SCHEMA_VERSION, ProfileStore,
    factory_defaults)
from kicadgen.services.component_types import text_digest

REPOSITORY_DB = Path(__file__).resolve().parents[1] / 'data' / 'analysis_profiles.sqlite3'


def _statuses(store, key=''):
    """{field: row} of ProfileStore.prompt_statuses."""
    return {row['field']: row for row in store.prompt_statuses(key)}


# ── store ──

def test_a_fresh_store_reports_every_prompt_current(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    rows = store.prompt_statuses()
    assert [row['field'] for row in rows] == list(pc.fields())
    assert {row['status'] for row in rows} == {pc.CURRENT}
    assert not any(row['is_override'] for row in rows)
    # CONTRACT CHANGED 2026-11-11 (step P2): the schema-8 prompts are wired, so
    # every registered prompt is sent from the profile (before: in_profile
    # False exactly for SCHEMA8_PROMPT_FIELDS).
    assert all(row['in_profile'] for row in rows)


def test_custom_and_outdated_texts_are_reported_and_never_changed(tmp_path, monkeypatch):
    from kicadgen.services import photo_crop_reading
    old = 'crop reading instructions of a former release'
    path = tmp_path / 'ai.sqlite3'
    store = ProfileStore(path)
    store.save_defaults({'system_prompt': 'my datasheet prompt  '})
    key = store.save_type(None, label='own', family='single_smd',
                          overrides={'crop_prompt': old, 'lead_prompt': 'type leads'})
    # Listed only now: the override stays (an override is a user choice) and reads outdated.
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    before = sqlite3.connect(path).execute('SELECT * FROM ai_overrides ORDER BY name').fetchall()
    reopened = ProfileStore(path)
    rows = _statuses(reopened, key)
    assert rows['system_prompt']['status'] == pc.CUSTOM and not rows['system_prompt']['is_override']
    assert rows['crop_prompt']['status'] == pc.OUTDATED and rows['crop_prompt']['is_override']
    assert rows['lead_prompt']['status'] == pc.CUSTOM and rows['lead_prompt']['is_override']
    assert rows['grounding_prompt']['status'] == pc.CURRENT
    assert reopened.defaults()['system_prompt'] == 'my datasheet prompt  '
    after = sqlite3.connect(path).execute('SELECT * FROM ai_overrides ORDER BY name').fetchall()
    assert after == before
    # The global view of the same database: no override, global texts only.
    assert _statuses(reopened)['crop_prompt']['status'] == pc.CURRENT


def test_a_built_in_type_reports_its_instructions_and_a_custom_type_does_not(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    builtin = store.list_types()[0]['key']
    rows = _statuses(store, builtin)
    assert rows['instructions']['status'] == pc.CURRENT
    store.save_type(builtin, label=store.get_type(builtin)['label'],
                    family=store.get_type(builtin)['family'], instructions='mine')
    assert _statuses(store, builtin)['instructions']['status'] == pc.CUSTOM
    own = store.duplicate(builtin)
    assert 'instructions' not in _statuses(store, own)


# ── schema 8 migration ──

def _downgrade_to_v7(path):
    """Turn a current database back into the schema 7 layout: no schema-8 or schema-9 prompt rows."""
    later = (*SCHEMA8_PROMPT_FIELDS, *SCHEMA9_PROMPT_FIELDS)
    with sqlite3.connect(path) as db:
        db.executemany('DELETE FROM ai_defaults WHERE name=?', [(n,) for n in later])
        db.executemany('DELETE FROM ai_overrides WHERE name=?', [(n,) for n in later])
        db.execute('PRAGMA user_version=7')


def test_v7_database_receives_the_code_prompts_and_keeps_every_custom_text_byte_identical(tmp_path):
    path = tmp_path / 'ai.sqlite3'
    store = ProfileStore(path)
    customs = {'system_prompt': '  my datasheet prompt\n', 'crop_prompt': 'my crop\r\nprompt',
               'image_instructions': 'notes'}
    store.save_defaults(customs)
    key = store.save_type(None, label='own', family='single_tht',
                          overrides={'system_prompt_single': 'type single\n'})
    _downgrade_to_v7(path)
    upgraded = ProfileStore(path)
    defaults = upgraded.defaults()
    assert set(defaults) == set(PROFILE_FIELDS)
    for field, text in customs.items():
        assert defaults[field] == text
    assert upgraded.overrides(key) == {'system_prompt_single': 'type single\n'}
    factory = factory_defaults()
    for field in (*SCHEMA8_PROMPT_FIELDS, *SCHEMA9_PROMPT_FIELDS):
        assert defaults[field] == factory[field] == pc.restore_default(field)
    with sqlite3.connect(path) as db:
        # CONTRACT CHANGED 2026-11-11 (step P2): a schema-7 database is carried
        # through 8 to the current schema 9, not left at 8.
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 9


def test_a_schema8_row_already_there_is_never_replaced_by_the_migration(tmp_path):
    """Negative of the migration: INSERT OR IGNORE, as every previous step."""
    path = tmp_path / 'ai.sqlite3'
    store = ProfileStore(path)
    store.save_defaults({'marking_prompt': 'my marking prompt'})
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=7')
    assert ProfileStore(path).defaults()['marking_prompt'] == 'my marking prompt'


def test_the_committed_database_copy_reports_its_custom_prompts(tmp_path):
    """The tracked database, opened as a COPY (the original is never written).

    State on 2026-11-11: its `system_prompt_single` (dc7044b8...) and
    `system_prompt` (a1538d55...) are neither the current default nor a known
    former default, so they are CUSTOM: the program keeps them and now SAYS
    so. Every schema-8 prompt arrives with the current default. If the user
    decides to list those digests as former defaults, this test changes with
    that decision.
    """
    if not REPOSITORY_DB.is_file():
        pytest.skip('no repository profile database in this checkout')
    original = hashlib.sha256(REPOSITORY_DB.read_bytes()).hexdigest()
    copy = tmp_path / 'copy.sqlite3'
    shutil.copyfile(REPOSITORY_DB, copy)
    with sqlite3.connect(copy) as db:
        stored = dict(db.execute('SELECT name,value FROM ai_defaults'))
    store = ProfileStore(copy)
    rows = _statuses(store)
    assert text_digest(stored['system_prompt_single']).startswith('dc7044b8')
    assert rows['system_prompt_single']['status'] == pc.CUSTOM
    assert rows['system_prompt']['status'] == pc.CUSTOM
    for field in SCHEMA8_PROMPT_FIELDS:
        assert rows[field]['status'] == pc.CURRENT
    # Opening replaced only OUTDATED global texts: none is left, and every
    # custom one is byte-identical.
    assert pc.OUTDATED not in {row['status'] for row in rows.values()}
    defaults = store.defaults()
    for field, row in rows.items():
        if row['status'] == pc.CUSTOM:
            assert defaults[field] == stored[field]
    assert hashlib.sha256(REPOSITORY_DB.read_bytes()).hexdigest() == original


# ── dialog ──

@pytest.fixture
def gui():
    pytest.importorskip('PyQt6')
    from kicadgen.gui import profile_dialog
    return profile_dialog


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / 'ai.sqlite3')


def test_the_status_label_follows_the_text_as_the_user_types(app, gui, store, monkeypatch):
    from kicadgen.services import photo_crop_reading
    old = 'crop reading instructions of a former release'
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    editor = gui.ProfileEditor(store)
    try:
        label = editor.status_labels['crop_prompt']
        assert label.text() == pc.status_label(pc.CURRENT)
        assert not editor.diff_buttons['crop_prompt'].isEnabled()
        assert not editor.restore_buttons['crop_prompt'].isEnabled()
        editor.fields['crop_prompt'].setPlainText(old)
        assert label.text() == pc.status_label(pc.OUTDATED)
        assert editor.restore_buttons['crop_prompt'].isEnabled()
        editor.fields['crop_prompt'].setPlainText(old + '!')
        assert label.text() == pc.status_label(pc.CUSTOM)
        assert editor.diff_buttons['crop_prompt'].isEnabled()
        # Every shown prompt has its row. CONTRACT CHANGED 2026-11-11 (step
        # P2): the schema-8 and schema-9 prompts are wired and now shown
        # (before: none of them was).
        assert set(editor.status_labels) == set(editor.fields)
        assert set(SCHEMA8_PROMPT_FIELDS) | set(SCHEMA9_PROMPT_FIELDS) <= set(editor.fields)
    finally:
        editor.close()


def test_restore_puts_the_default_in_the_editor_and_ok_saves_it(app, gui, store):
    store.save_defaults({'system_prompt': 'my datasheet prompt'})
    editor = gui.ProfileEditor(store)
    try:
        assert editor.status_labels['system_prompt'].text() == pc.status_label(pc.CUSTOM)
        assert store.defaults()['system_prompt'] == 'my datasheet prompt'   # not yet
        editor.restore_buttons['system_prompt'].click()
        assert editor.fields['system_prompt'].toPlainText() == pc.restore_default('system_prompt')
        assert editor.status_labels['system_prompt'].text() == pc.status_label(pc.CURRENT)
        editor._save()
        assert store.defaults()['system_prompt'] == pc.restore_default('system_prompt')
    finally:
        editor.close()


def test_restore_then_cancel_keeps_the_stored_custom_text(app, gui, store):
    store.save_defaults({'system_prompt': 'my datasheet prompt'})
    editor = gui.ProfileEditor(store)
    try:
        editor.restore_buttons['system_prompt'].click()
        editor.reject()
        assert store.defaults()['system_prompt'] == 'my datasheet prompt'
    finally:
        editor.close()


def test_an_inherited_prompt_cannot_be_restored_until_unticked(app, gui, store):
    key = store.save_type(None, label='own', family='single_smd', overrides={})
    store.save_defaults({'lead_prompt': 'global leads'})
    editor = gui.ProfileEditor(store, key)
    try:
        assert editor.status_labels['lead_prompt'].text() == pc.status_label(pc.CUSTOM)
        assert not editor.restore_buttons['lead_prompt'].isEnabled()    # read-only, inherited
        editor.inherit['lead_prompt'].setChecked(False)
        assert editor.restore_buttons['lead_prompt'].isEnabled()
        editor.restore_buttons['lead_prompt'].click()
        editor._save()
        assert store.overrides(key) == {'lead_prompt': pc.restore_default('lead_prompt')}
        assert store.defaults()['lead_prompt'] == 'global leads'
    finally:
        editor.close()


def test_the_diff_dialog_shows_the_difference_and_is_released(app, gui, store, monkeypatch):
    from PyQt6 import sip
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QTextEdit
    shown = []

    def fake_exec(dialog):
        """Record the diff dialog and its text instead of blocking."""
        shown.append((dialog, dialog.windowTitle(), dialog.findChild(QTextEdit).toPlainText()))
        return 0
    editor = gui.ProfileEditor(store)
    try:
        monkeypatch.setattr(gui.QDialog, 'exec', fake_exec)
        default = pc.restore_default('numbering_prompt')
        editor.fields['numbering_prompt'].setPlainText(default + ' Frase in piu.')
        editor.diff_buttons['numbering_prompt'].click()
        [(dialog, title, text)] = shown
        assert 'Frase in piu' in text and text.splitlines()[0].startswith('---')
        assert 'Differenze dal predefinito' in title
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        assert sip.isdeleted(dialog)
    finally:
        editor.close()


def test_saving_an_unchanged_default_keeps_its_surrounding_blanks(app, gui, store, monkeypatch):
    """A default whose trailing blanks matter is stored byte-identical when the user presses OK."""
    from kicadgen.services import photo_leads
    monkeypatch.setattr(photo_leads, 'PROMPT', 'lead prompt with a trailing newline\n')
    store.save_defaults({'lead_prompt': 'lead prompt with a trailing newline\n'})
    editor = gui.ProfileEditor(store)
    try:
        editor.fields['lead_prompt'].setPlainText('lead prompt with a trailing newline')
        editor._save()
        assert store.defaults()['lead_prompt'] == 'lead prompt with a trailing newline\n'
    finally:
        editor.close()


def test_a_type_save_keeps_an_override_of_a_prompt_the_dialog_does_not_show(app, gui, store,
                                                                              monkeypatch):
    """CONTRACT CHANGED 2026-11-11 (step P2): every registered prompt is now shown, so
    a prompt the dialog does not show is simulated by flagging one entry not
    wired; the rule tested (save_type replaces every override, the unshown one
    is carried over) is unchanged."""
    import dataclasses
    monkeypatch.setattr(pc, 'PROMPTS', tuple(
        dataclasses.replace(e, in_profile=False) if e.field == 'marking_prompt' else e
        for e in pc.PROMPTS))
    key = store.save_type(None, label='own', family='single_smd',
                          overrides={'marking_prompt': 'type marking', 'lead_prompt': 'type leads'})
    editor = gui.ProfileEditor(store, key)
    try:
        assert 'marking_prompt' not in editor.fields
        editor.inherit['lead_prompt'].setChecked(True)
        editor._save()
        assert store.overrides(key) == {'marking_prompt': 'type marking'}
    finally:
        editor.close()


def test_a_wired_registry_prompt_appears_in_its_stage_without_changing_the_dialog(app, gui, store):
    """CONTRACT CHANGED 2026-11-11 (step P2 flipped in_profile for real): the marking
    prompt is shown with its status in its stage's own sub-tab, no longer after
    a monkeypatch; the photo stage now ends with the read-only «Testi nel
    codice» sub-tab, so the caption is looked up instead of taken last."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QTabWidget
    editor = gui.ProfileEditor(store)
    try:
        field = editor.fields['marking_prompt']
        assert editor.status_labels['marking_prompt'].text() == pc.status_label(pc.CURRENT)
        [sub] = editor.tabs.widget(1).findChildren(
            QTabWidget, '', Qt.FindChildOption.FindDirectChildrenOnly)
        captions = [sub.tabText(i) for i in range(sub.count())]
        index = captions.index('Foto: lettura delle marcature')
        assert sub.widget(index).isAncestorOf(field)
        assert captions[-1] == 'Testi nel codice'
    finally:
        editor.close()


def test_the_type_instructions_of_a_built_in_type_have_a_status_row(app, gui, store):
    key = store.list_types()[0]['key']
    editor = gui.ProfileEditor(store, key)
    try:
        assert editor.status_labels['instructions'].text() == pc.status_label(pc.CURRENT)
        editor.instructions.setPlainText('mine')
        assert editor.status_labels['instructions'].text() == pc.status_label(pc.CUSTOM)
        editor.restore_buttons['instructions'].click()
        assert editor.status_labels['instructions'].text() == pc.status_label(pc.CURRENT)
    finally:
        editor.close()
    new = gui.ProfileEditor(store, new=True)
    try:
        assert 'instructions' not in new.status_labels
    finally:
        new.close()


def test_the_status_and_buttons_follow_the_interface_language(app, gui, store):
    from kicadgen.services.i18n import I18n
    i18n = I18n(store, 'en')
    from PyQt6.QtWidgets import QWidget
    owner = QWidget()
    owner.i18n = i18n
    editor = gui.ProfileEditor(store, parent=owner)
    try:
        assert editor.status_labels['system_prompt'].text() == 'Default of this version'
        assert editor.diff_buttons['system_prompt'].text() == 'Show differences'
        assert editor.restore_buttons['system_prompt'].text() == 'Restore default'
        editor.fields['system_prompt'].setPlainText('x')
        assert editor.status_labels['system_prompt'].text() == 'Customized'
    finally:
        editor.close()
        owner.deleteLater()
