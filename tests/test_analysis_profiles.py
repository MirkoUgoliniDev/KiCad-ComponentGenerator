import json
import sqlite3

import pytest

from kicadgen.services.analysis_profiles import ProfileStore, ProfileError, factory_defaults, SCHEMA_VERSION
from kicadgen.services.component_types import COMPONENT_TYPES


def test_migration_preserves_custom_config_once_and_secrets_are_not_imported(tmp_path):
    legacy = {'model': 'vendor/custom', 'system_prompt': 'keep exactly\n',
              'vision_model': 'vendor/photo', 'component_type': 'single_smd',
              'api_key': 'secret-not-for-sqlite'}
    path = tmp_path / 'legacy.json'
    raw = json.dumps(legacy).encode()
    path.write_bytes(raw)
    store = ProfileStore(tmp_path / 'ai.sqlite3', legacy_config=legacy, legacy_path=path)
    assert store.defaults()['model'] == 'vendor/custom'
    assert store.defaults()['verification_model'] == 'vendor/custom'
    assert store.defaults()['system_prompt'] == 'keep exactly\n'
    assert path.read_bytes() == raw
    assert next(tmp_path.glob('config-before-sqlite-*.json')).read_bytes() == raw
    assert b'secret-not-for-sqlite' not in store.path.read_bytes()
    for source in COMPONENT_TYPES:
        row = store.get_type(source.key)
        assert (row['label'], row['family'], row['instructions']) == (source.label, source.key, source.prompt)
    store.save_defaults({'model': 'changed'})
    reopened = ProfileStore(store.path, legacy_config=legacy, legacy_path=path)
    assert reopened.defaults()['model'] == 'changed'
    assert reopened.defaults()['verification_model'] == 'vendor/custom'
    assert len(list(tmp_path.glob('config-before-sqlite-*.json'))) == 1


def test_crud_inheritance_independent_models_and_snapshot(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    key = store.save_type(None, label='Personalizzata', family='aggregate_castellated',
                         instructions='specific', overrides={'model': 'custom-data', 'vision_model': None,
                                                             'verification_model': 'custom-verify'})
    store.save_defaults({'model': 'global-data', 'vision_model': 'global-photo',
                         'verification_model': 'global-verify', 'image_instructions': 'extra photo'})
    effective = store.resolve(key)
    assert (effective['model'], effective['vision_model'], effective['verification_model']) == (
        'custom-data', 'global-photo', 'custom-verify')
    snapshot = store.snapshot(key)
    assert 'extra photo' in snapshot['grounding_prompt']
    assert 'extra photo' in snapshot['verification_prompt']
    assert 'extra photo' in snapshot['terminal_prompt']
    assert 'extra photo' not in store.resolve(key)['grounding_prompt']
    copy = store.duplicate(key)
    assert store.overrides(copy) == store.overrides(key)
    store.save_type(copy, label='Off', family='single_smd', active=False, overrides={})
    assert copy not in [row['key'] for row in store.list_types()]
    assert copy in [row['key'] for row in store.list_types(True)]
    store.delete_type(key)
    assert store.get_type(key) is None
    assert store.overrides(key) == {}
    assert store.get_type(copy) is not None
    assert snapshot['model'] == 'custom-data'  # Independent of subsequent edits/deletes.
    assert ProfileStore(store.path).get_type(key) is None
    with pytest.raises(ProfileError):
        store.resolve(key)


@pytest.mark.parametrize('values', [{'model': ''}, {'model': None}, {'bad_key': 'oops'}, {'vision_model': 123}])
def test_invalid_defaults_are_atomic(tmp_path, values):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    original = store.defaults()
    with pytest.raises(ProfileError):
        store.save_defaults({'system_prompt': 'changed', **values})
    assert store.defaults() == original


def test_invalid_type_does_not_overwrite_existing_profile(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    original = store.get_type('single_smd')
    with pytest.raises(ProfileError):
        store.save_type('single_smd', label='new', family='unsupported')
    assert store.get_type('single_smd') == original
    with pytest.raises(ProfileError):
        store.save_type(None, label='  ', family='single_smd')


def test_backup_and_future_database_are_safe(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'verification_model': 'custom'})
    backup = store.backup(tmp_path / 'backup.sqlite3')
    assert ProfileStore(backup).defaults() == store.defaults()
    with pytest.raises(ProfileError):
        store.backup(backup)
    with pytest.raises(ProfileError):
        store.backup(store.path)
    with sqlite3.connect(store.path) as db:
        db.execute('PRAGMA user_version=999')
    raw = store.path.read_bytes()
    with pytest.raises(ProfileError, match='recente'):
        ProfileStore(store.path)
    assert store.path.read_bytes() == raw


def test_v1_upgrade_copies_existing_datasheet_defaults_and_overrides(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'model': 'old-data'})
    key = store.save_type(None, label='custom', family='single_smd', overrides={'model': 'override'})
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM ai_defaults WHERE name='verification_model'")
        db.execute('PRAGMA user_version=1')
    upgraded = ProfileStore(store.path)
    assert upgraded.defaults()['verification_model'] == 'old-data'
    assert upgraded.resolve(key)['verification_model'] == 'override'
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_corrupt_legacy_is_backed_up_and_recorded_not_fatal(tmp_path):
    """@brief A corrupt legacy config no longer blocks the first start (finding G1).

    The contract changed from "raise" to "never crash, never lose": the broken
    bytes are kept both in place and in the migration backup, and the database
    records why nothing was imported, so the replacement is not silent.
    """
    source = tmp_path / 'broken.json'
    source.write_text('{ broken')
    store = ProfileStore(tmp_path / 'ai.sqlite3', legacy_path=source)
    assert source.read_text() == '{ broken'
    assert next(tmp_path.glob('config-before-sqlite-*.json')).read_text() == '{ broken'
    assert store.defaults()['model'] == factory_defaults()['model']
    with sqlite3.connect(tmp_path / 'ai.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT value FROM metadata WHERE name='legacy_import_skipped'").fetchone()[0].startswith('corrupt')


def test_json_now_only_persists_preferences(tmp_path):
    from kicadgen.gui.app_config import save_config, config_path
    save_config(dict(factory_defaults(), component_type='single_smd', show_grid=False))
    assert json.loads(config_path().read_text()) == {'component_type': 'single_smd', 'show_grid': False}


def test_missing_override_table_fails_at_open_without_recreating_it(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    with sqlite3.connect(store.path) as db:
        db.execute('DROP TABLE ai_overrides')
    with pytest.raises(sqlite3.DatabaseError):
        ProfileStore(store.path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_overrides'").fetchone() is None


def test_partial_type_update_keeps_overrides_and_deleted_editor_cannot_recreate_type(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    key = store.save_type(None, label='type', family='single_smd', overrides={'model': 'custom'})
    store.save_type(key, label='renamed', family='single_smd', active=False)
    assert store.resolve(key)['model'] == 'custom'
    store.delete_type(key)
    with pytest.raises(ProfileError):
        store.save_type(key, label='stale editor', family='single_smd')
    assert store.get_type(key) is None


def test_legacy_import_metadata_never_stores_a_path_or_a_message(tmp_path):
    """@brief The committed database must not carry the user's paths (block A review).

    Positive: a corrupt legacy file records only the exception class, and the
    backup only by file name. Negative: neither value contains the folder of
    the legacy file or the backup (which would include the user name).
    """
    folder = tmp_path / 'Users' / 'someone'
    folder.mkdir(parents=True)
    source = folder / 'config.json'
    source.write_text('{ broken')
    ProfileStore(tmp_path / 'ai.sqlite3', legacy_path=source)
    with sqlite3.connect(tmp_path / 'ai.sqlite3') as db:
        metadata = dict(db.execute('SELECT name,value FROM metadata'))
    assert metadata['legacy_import_skipped'] == 'corrupt: JSONDecodeError'
    assert metadata['legacy_backup'].startswith('config-before-sqlite-')
    for value in metadata.values():
        assert 'someone' not in value and str(tmp_path) not in value


def test_unreadable_legacy_records_only_the_error_class(tmp_path, monkeypatch):
    """@brief An OSError message (it names the file) is reduced to its class."""
    source = tmp_path / 'config.json'
    source.write_text('{}')
    from pathlib import Path

    def refuse(self):
        raise PermissionError(13, 'Permission denied', str(self))
    monkeypatch.setattr(Path, 'read_bytes', refuse)
    ProfileStore(tmp_path / 'ai.sqlite3', legacy_path=source)
    monkeypatch.undo()
    with sqlite3.connect(tmp_path / 'ai.sqlite3') as db:
        value = db.execute("SELECT value FROM metadata WHERE name='legacy_import_skipped'").fetchone()[0]
    assert value == 'unreadable: PermissionError'


def _count_connections(monkeypatch):
    """Count the SQLite connections opened by the profile store."""
    from kicadgen.services import analysis_profiles
    opened = []
    real = analysis_profiles.sqlite3.connect

    def counting(*args, **kwargs):
        opened.append(args[0] if args else kwargs.get('database'))
        return real(*args, **kwargs)
    monkeypatch.setattr(analysis_profiles.sqlite3, 'connect', counting)
    return opened


def test_translations_open_one_connection_per_language_not_per_string(tmp_path, monkeypatch):
    """@brief T2: lookups are served from memory after one query per language.

    Positive: many lookups in one language open one connection; a second
    language opens exactly one more; going back opens none. Negative: a
    missing key in a language with an explicit fallback opens nothing more.
    """
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    keys = [row['key'] for row in store.list_translations('it')][:50]
    opened = _count_connections(monkeypatch)
    for key in keys:
        store.translation(key, 'it')
    assert len(opened) == 1
    for key in keys:
        store.translation(key, 'fr')
    assert len(opened) == 2
    store.translation(keys[0], 'it')
    assert store.translation('no.such.key', 'it', 'fallback') == 'fallback'
    assert len(opened) == 2


def test_an_edited_translation_is_visible_immediately(tmp_path):
    """@brief The cache never hides a change.

    Positive: save_translation on the same store is visible at the next
    lookup. Positive: a change written by ANOTHER store on the same file is
    visible too. Negative: an untouched key keeps its text.
    """
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    before = store.translation('tab.project', 'de')
    untouched = store.translation('tab.preview.name', 'de')
    store.save_translation('tab.project', 'de', 'Mein Projekt')
    assert store.translation('tab.project', 'de') == 'Mein Projekt' != before
    other = ProfileStore(tmp_path / 'ai.sqlite3')
    other.save_translation('tab.project', 'de', 'Projekt (lang)')
    assert store.translation('tab.project', 'de') == 'Projekt (lang)'
    assert store.translation('tab.preview.name', 'de') == untouched


def test_unknown_key_falls_back_to_english_then_to_the_key(tmp_path):
    """@brief The fallback chain is unchanged by the cache."""
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_translation('only.en', 'en', 'English only')
    assert store.translation('only.en', 'it') == 'English only'
    assert store.translation('missing.everywhere', 'it') == 'missing.everywhere'
