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


def test_corrupt_legacy_is_not_silently_replaced(tmp_path):
    source = tmp_path / 'broken.json'
    source.write_text('{ broken')
    with pytest.raises(ValueError):
        ProfileStore(tmp_path / 'ai.sqlite3', legacy_path=source)
    assert source.read_text() == '{ broken'
    with sqlite3.connect(tmp_path / 'ai.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0


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
