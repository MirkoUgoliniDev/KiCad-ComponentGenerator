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


# ── crop_prompt: per-body crop reading prompt in the profile (HANDOFF 146) ──

def test_crop_prompt_factory_default_is_the_hardcoded_crop_reading_prompt(tmp_path):
    from kicadgen.services.photo_crop_reading import SYSTEM_PROMPT
    assert factory_defaults()['crop_prompt'] == SYSTEM_PROMPT
    assert ProfileStore(tmp_path / 'ai.sqlite3').defaults()['crop_prompt'] == SYSTEM_PROMPT


def _downgrade_to_v3(path):
    """Turn a current database back into the schema 3 layout: no crop_prompt/crop_model rows."""
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM ai_defaults WHERE name IN ('crop_prompt','crop_model')")
        db.execute("DELETE FROM ai_overrides WHERE name IN ('crop_prompt','crop_model')")
        db.execute('PRAGMA user_version=3')


def test_v3_database_receives_the_crop_prompt_and_keeps_every_user_text(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'system_prompt': 'my datasheet prompt', 'lead_prompt': 'my leads',
                         'image_instructions': 'my photo notes'})
    key = store.save_type(None, label='custom', family='single_smd',
                          overrides={'terminal_prompt': 'type terminals'})
    added = {'crop_prompt', 'crop_model'}          # the rows steps 3->4 and 4->5 add
    before = {k: v for k, v in store.defaults().items() if k not in added}
    _downgrade_to_v3(store.path)
    upgraded = ProfileStore(store.path)
    defaults = upgraded.defaults()
    assert defaults['crop_prompt'] == factory_defaults()['crop_prompt']
    assert {k: v for k, v in defaults.items() if k not in added} == before
    assert upgraded.overrides(key) == {'terminal_prompt': 'type terminals'}
    assert upgraded.resolve(key)['crop_prompt'] == factory_defaults()['crop_prompt']
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_a_crop_prompt_the_user_already_edited_is_never_replaced(tmp_path):
    """Negative of the migration: an existing row, even at schema 3, keeps its text."""
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'crop_prompt': 'my crop prompt'})
    assert ProfileStore(store.path).defaults()['crop_prompt'] == 'my crop prompt'
    with sqlite3.connect(store.path) as db:
        db.execute('PRAGMA user_version=3')
    assert ProfileStore(store.path).defaults()['crop_prompt'] == 'my crop prompt'


def test_v1_database_climbs_every_step_up_to_the_crop_prompt(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM ai_defaults WHERE name IN ('verification_model','crop_prompt',"
                   "'crop_model')")
        db.execute('DROP TABLE translations')
        db.execute('PRAGMA user_version=1')
    upgraded = ProfileStore(store.path)
    assert upgraded.defaults()['crop_prompt'] == factory_defaults()['crop_prompt']
    assert upgraded.defaults()['crop_model'] == ''          # step 4->5 climbed as well
    assert upgraded.translation('ui:Lettura ritagli', 'en') == 'Crop reading'


def test_crop_prompt_is_inherited_overridden_and_never_gets_the_image_instructions(tmp_path):
    """HANDOFF 146 V3: image instructions describe the whole board, not one body's crop."""
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'crop_prompt': 'global crop'})
    inherit = store.save_type(None, label='inherits', family='single_smd',
                              overrides={'image_instructions': 'extra photo'})
    own = store.save_type(None, label='own', family='single_smd',
                          overrides={'crop_prompt': 'type crop', 'image_instructions': 'extra photo'})
    assert store.resolve(inherit)['crop_prompt'] == 'global crop'
    assert store.resolve(own)['crop_prompt'] == 'type crop'
    for key in (inherit, own):
        snapshot = store.snapshot(key)
        # Positive: every whole-board photo prompt still receives them.
        for field in ('grounding_prompt', 'verification_prompt', 'terminal_prompt', 'lead_prompt'):
            assert snapshot[field].startswith(store.resolve(key)[field])
            assert snapshot[field].endswith('extra photo')
        # Negative: the per-body crop prompt goes out exactly as resolved.
        assert snapshot['crop_prompt'] == store.resolve(key)['crop_prompt']
        assert 'extra photo' not in snapshot['crop_prompt']


@pytest.mark.parametrize('value', ['', '   ', None])
def test_a_blank_crop_prompt_is_rejected_like_every_other_prompt(tmp_path, value):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    original = store.defaults()
    with pytest.raises(ProfileError):
        store.save_defaults({'crop_prompt': value})
    assert store.defaults() == original


# ── crop_prompt: former factory texts reach existing databases (HANDOFF 146, V1) ──

def _store_with_global_crop_prompt(path, text):
    """Create a current database and write `text` verbatim as the global crop prompt."""
    store = ProfileStore(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ai_defaults SET value=? WHERE name='crop_prompt'", (text,))
    db.close()
    return store


def test_a_former_factory_crop_prompt_is_replaced_on_open(tmp_path, monkeypatch):
    from kicadgen.services import photo_crop_reading
    from kicadgen.services.component_types import text_digest
    old = 'crop reading instructions of a former release'
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    path = tmp_path / 'ai.sqlite3'
    _store_with_global_crop_prompt(path, old)
    assert ProfileStore(path).defaults()['crop_prompt'] == photo_crop_reading.SYSTEM_PROMPT


def test_an_edited_former_crop_prompt_is_kept(tmp_path, monkeypatch):
    """Negative case: one character more than the former default is a user edit."""
    from kicadgen.services import photo_crop_reading
    from kicadgen.services.component_types import text_digest
    old = 'crop reading instructions of a former release'
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    path = tmp_path / 'ai.sqlite3'
    _store_with_global_crop_prompt(path, old + '.')
    assert ProfileStore(path).defaults()['crop_prompt'] == old + '.'


def test_a_per_type_crop_prompt_override_holding_a_former_default_is_untouched(tmp_path, monkeypatch):
    """Only the global default follows the factory text: an override is a user choice."""
    from kicadgen.services import photo_crop_reading
    from kicadgen.services.component_types import text_digest
    old = 'crop reading instructions of a former release'
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    path = tmp_path / 'ai.sqlite3'
    store = _store_with_global_crop_prompt(path, old)
    key = store.save_type(None, label='own', family='single_smd', overrides={'crop_prompt': old})
    reopened = ProfileStore(path)
    assert reopened.defaults()['crop_prompt'] == photo_crop_reading.SYSTEM_PROMPT
    assert reopened.overrides(key) == {'crop_prompt': old}


def test_the_current_crop_prompt_is_not_listed_as_superseded():
    """A current text listed as superseded would be a copy-paste error in the digests."""
    from kicadgen.services.component_types import text_digest
    from kicadgen.services.photo_crop_reading import SUPERSEDED_CROP_PROMPT_DIGESTS, SYSTEM_PROMPT
    assert text_digest(SYSTEM_PROMPT) not in SUPERSEDED_CROP_PROMPT_DIGESTS


def test_repository_database_holds_the_factory_crop_prompt():
    """The tracked database (read-only here) carries the current factory crop prompt.

    It is the source-run database and the reference users compare against: a
    stale text there would make rows report a PROMPT_VERSION whose text was
    never sent. tools/make_seed_db.verify_seed is not reused: it demands a
    fresh seed (no developer metadata, every default equal to the code), which
    the tracked database is documented not to be. When SYSTEM_PROMPT changes,
    opening the database with ProfileStore applies SUPERSEDED_CROP_PROMPT_DIGESTS.
    """
    from pathlib import Path
    from kicadgen.services.photo_crop_reading import SYSTEM_PROMPT
    path = Path(__file__).resolve().parents[1] / 'data' / 'analysis_profiles.sqlite3'
    if not path.is_file():
        pytest.skip('no repository profile database in this checkout')
    db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
    try:
        row = db.execute("SELECT value FROM ai_defaults WHERE name='crop_prompt'").fetchone()
    finally:
        db.close()
    assert row is not None and row[0] == SYSTEM_PROMPT


# ── crop_model: model of the crop reading, blank = photo model (HANDOFF 150) ──

def _dump(path):
    """Every row of every user table, for a before/after comparison."""
    with sqlite3.connect(path) as db:
        return {table: sorted(db.execute(f'SELECT * FROM {table}').fetchall())
                for table in ('ai_defaults', 'ai_overrides', 'component_types', 'metadata',
                              'translations')}


def test_crop_model_factory_default_is_blank_and_it_is_not_a_photo_prompt(tmp_path):
    from kicadgen.services.analysis_profiles import PHOTO_PROMPT_FIELDS, PROFILE_FIELDS
    assert factory_defaults()['crop_model'] == ''
    assert ProfileStore(tmp_path / 'ai.sqlite3').defaults()['crop_model'] == ''
    assert 'crop_model' in PROFILE_FIELDS and 'crop_model' not in PHOTO_PROMPT_FIELDS


def _downgrade_to_v4(path):
    """Turn a current database back into the schema 4 layout: no crop_model row."""
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM ai_defaults WHERE name='crop_model'")
        db.execute("DELETE FROM ai_overrides WHERE name='crop_model'")
        db.execute('PRAGMA user_version=4')


def test_v4_database_receives_a_blank_crop_model_and_keeps_every_user_text(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'system_prompt': 'my datasheet prompt', 'crop_prompt': 'my crop prompt',
                         'verification_model': 'vendor/mine', 'image_instructions': 'my notes'})
    key = store.save_type(None, label='custom', family='single_smd',
                          overrides={'crop_prompt': 'type crop', 'vision_model': 'type/photo'})
    before = {k: v for k, v in store.defaults().items() if k != 'crop_model'}
    _downgrade_to_v4(store.path)
    upgraded = ProfileStore(store.path)
    defaults = upgraded.defaults()
    assert defaults['crop_model'] == ''
    assert {k: v for k, v in defaults.items() if k != 'crop_model'} == before
    assert upgraded.overrides(key) == {'crop_prompt': 'type crop', 'vision_model': 'type/photo'}
    assert upgraded.resolve(key)['crop_model'] == ''
    with sqlite3.connect(store.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 5


def test_a_crop_model_the_user_already_set_is_never_replaced(tmp_path):
    """Negative of the migration: an existing row, even at schema 4, keeps its value."""
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'crop_model': 'vendor/my-crop-reader'})
    with sqlite3.connect(store.path) as db:
        db.execute('PRAGMA user_version=4')
    assert ProfileStore(store.path).defaults()['crop_model'] == 'vendor/my-crop-reader'


def test_a_current_database_reopened_changes_no_row(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'crop_model': 'vendor/crop'})
    store.save_type(None, label='t', family='single_smd', overrides={'crop_model': ''})
    before = _dump(store.path)
    ProfileStore(store.path)
    assert _dump(store.path) == before


@pytest.mark.parametrize('value', ['', '   '])
def test_a_blank_crop_model_is_accepted_and_means_the_photo_model(tmp_path, value):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'crop_model': 'vendor/crop'})
    store.save_defaults({'crop_model': value})
    assert store.defaults()['crop_model'] == ''            # stripped, stored blank


@pytest.mark.parametrize('field', ['model', 'vision_model', 'verification_model'])
@pytest.mark.parametrize('value', ['', '   '])
def test_the_other_model_fields_still_reject_a_blank(tmp_path, field, value):
    """Negative: the blank exception is for crop_model alone; None is inherit, not blank."""
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    original = store.defaults()
    with pytest.raises(ProfileError):
        store.save_defaults({field: value})
    with pytest.raises(ProfileError):
        store.save_defaults({'crop_model': None})
    assert store.defaults() == original


def test_crop_model_is_stripped_inherited_or_overridden_even_with_a_blank(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'crop_model': '  vendor/global-crop  '})
    assert store.defaults()['crop_model'] == 'vendor/global-crop'
    inherit = store.save_type(None, label='inherits', family='single_smd',
                              overrides={'image_instructions': 'extra photo'})
    own = store.save_type(None, label='own', family='single_smd',
                          overrides={'crop_model': ' vendor/type-crop '})
    # An EXPLICIT blank override is a choice («the photo model for this
    # type»), distinct from inheriting the global model.
    photo = store.save_type(None, label='photo', family='single_smd',
                            overrides={'crop_model': ''})
    assert store.resolve(inherit)['crop_model'] == 'vendor/global-crop'
    assert store.resolve(own)['crop_model'] == 'vendor/type-crop'
    assert store.resolve(photo)['crop_model'] == ''
    assert store.overrides(photo) == {'crop_model': ''}
    assert 'crop_model' not in store.overrides(inherit)
    for key in (inherit, own, photo):
        # A model identifier never receives the image instructions.
        assert store.snapshot(key)['crop_model'] == store.resolve(key)['crop_model']
