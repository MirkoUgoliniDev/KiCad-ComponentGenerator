"""@brief Versioned SQLite catalog of component types and inherited AI profiles.

Connections are short-lived; analysis workers receive resolved snapshots, never
connections. The legacy JSON is backed up and imported once, never overwritten.
"""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
import os
import sqlite3
import threading
import uuid

from .component_types import COMPONENT_TYPES
from .messages import tx

PROFILE_FIELDS = ('model','vision_model','verification_model','system_prompt','grounding_prompt',
                  'verification_prompt','terminal_prompt','lead_prompt','crop_prompt',
                  'crop_model','image_instructions','system_prompt_single','numbering_prompt')
#: 'system_prompt_single' is the datasheet prompt of a single component
#: (families single_tht/single_smd), 'numbering_prompt' the prompt of the
#: dedicated pin-numbering call (both 2026-10-10, schema 6).
#: Model identifier fields: stored stripped, never composed with instructions.
#: 'crop_model' (HANDOFF 150) is the model of the per-body crop reading; its
#: factory value is EMPTY and means «the model of the paid call on the
#: photograph», so it is the one model field a blank value is valid for.
MODEL_FIELDS = ('model', 'vision_model', 'verification_model', 'crop_model')
#: Fields whose value may legitimately be blank: no type notes, or a crop
#: reading that follows the photo model. Every prompt must carry a text.
BLANK_ALLOWED_FIELDS = ('image_instructions', 'crop_model')
#: Prompt fields sent with a photograph of the whole board: the type's image
#: instructions are appended to each of them by ProfileStore.snapshot.
#: 'crop_prompt' is deliberately absent (user decision 2026-09-28, HANDOFF 146
#: V3): it reads ONE body from its own crop, and the image instructions
#: describe the whole board; the per-body text is customised per type in its
#: own profile tab instead.
PHOTO_PROMPT_FIELDS = ('grounding_prompt','verification_prompt','terminal_prompt','lead_prompt')
FAMILIES = {t.key:t.label for t in COMPONENT_TYPES}
#: Marks a read-cache miss (None is a valid cached value: an absent type).
_MISSING = object()
#: Database schema version. 2: verification_model; 3: translations table;
#: 4: crop_prompt (per-body crop reading prompt, HANDOFF 146); 5: crop_model
#: (model of that reading, blank = photo model, HANDOFF 150).
SCHEMA_VERSION = 6


def _default_translations():
    """@brief Return the bundled translation catalog for a new database."""
    from .i18n import DEFAULT_TRANSLATIONS
    return [(key, language, text)
            for key, values in DEFAULT_TRANSLATIONS.items()
            for language, text in values.items()]


def _superseded_translations():
    """@brief Rows (new text, key, language, old text) for outdated bundled translations.

    @return One row per superseded default text in i18n.SUPERSEDED_DEFAULTS.
    """
    from .i18n import DEFAULT_TRANSLATIONS, SUPERSEDED_DEFAULTS
    return [(DEFAULT_TRANSLATIONS[key][language], key, language, old)
            for key, languages in SUPERSEDED_DEFAULTS.items()
            for language, olds in languages.items()
            for old in olds]


def _replace_superseded_texts(db):
    """@brief Replace outdated built-in instructions and global prompts still stored verbatim.

    @param db Open connection inside the initialization transaction.
    @note A stored text is replaced only when its SHA-256 digest is one of the
        known former defaults (component_types.SUPERSEDED_INSTRUCTION_DIGESTS,
        datasheet_extractor.SUPERSEDED_SYSTEM_PROMPT_DIGESTS,
        photo_crop_reading.SUPERSEDED_CROP_PROMPT_DIGESTS,
        photo_terminals.SUPERSEDED_PROMPT_DIGESTS): a text the user
        edited, even by one character, never matches and is kept. Only the
        global system prompt, the global crop prompt and the built-in types
        are touched; a per-type
        override or a custom type copied from a built-in one is a deliberate
        choice of the user and stays as it is. The UPDATE repeats the old text
        in its condition, so a concurrent edit is never overwritten.
    """
    from .component_types import SUPERSEDED_INSTRUCTION_DIGESTS, get as builtin_type, text_digest
    from .datasheet_extractor import (
        DEFAULT_SYSTEM_PROMPT, SUPERSEDED_SYSTEM_PROMPT_DIGESTS, DEFAULT_SINGLE_SYSTEM_PROMPT,
        SUPERSEDED_SINGLE_PROMPT_DIGESTS, DEFAULT_PIN_NUMBERING_PROMPT,
        SUPERSEDED_NUMBERING_PROMPT_DIGESTS)
    rows = db.execute('SELECT key,instructions FROM component_types WHERE builtin=1').fetchall()
    for key, instructions in rows:
        current = builtin_type(key)
        if (current is not None and instructions != current.prompt
                and text_digest(instructions) in SUPERSEDED_INSTRUCTION_DIGESTS.get(key, ())):
            db.execute('UPDATE component_types SET instructions=? '
                       'WHERE key=? AND builtin=1 AND instructions=?',
                       (current.prompt, key, instructions))
    from .photo_crop_reading import SUPERSEDED_CROP_PROMPT_DIGESTS, SYSTEM_PROMPT as CROP_PROMPT
    from .photo_terminals import SUPERSEDED_PROMPT_DIGESTS as SUPERSEDED_TERMINAL_PROMPT_DIGESTS
    from .photo_terminals import PROMPT as TERMINAL_PROMPT
    for name, current, superseded in (
            ('system_prompt', DEFAULT_SYSTEM_PROMPT, SUPERSEDED_SYSTEM_PROMPT_DIGESTS),
            ('system_prompt_single', DEFAULT_SINGLE_SYSTEM_PROMPT, SUPERSEDED_SINGLE_PROMPT_DIGESTS),
            ('numbering_prompt', DEFAULT_PIN_NUMBERING_PROMPT, SUPERSEDED_NUMBERING_PROMPT_DIGESTS),
            ('crop_prompt', CROP_PROMPT, SUPERSEDED_CROP_PROMPT_DIGESTS),
            ('terminal_prompt', TERMINAL_PROMPT, SUPERSEDED_TERMINAL_PROMPT_DIGESTS)):
        row = db.execute('SELECT value FROM ai_defaults WHERE name=?', (name,)).fetchone()
        if row is not None and row[0] != current and text_digest(row[0]) in superseded:
            db.execute('UPDATE ai_defaults SET value=? WHERE name=? AND value=?',
                       (current, name, row[0]))


def factory_defaults():
    """@brief Return the built-in AI defaults (models and prompts) used to seed the profile database.

    @return Mapping of default name to its factory value (a fresh dict).
    """
    from .datasheet_extractor import (DEFAULT_SYSTEM_PROMPT, DEFAULT_SINGLE_SYSTEM_PROMPT,
                                      DEFAULT_PIN_NUMBERING_PROMPT)
    from .openrouter import DEFAULT_MODEL
    from .photo_grounding import DEFAULT_VISION_MODEL, GROUNDING_PROMPT, VERIFICATION_PROMPT
    from .photo_terminals import PROMPT as TERMINAL_PROMPT
    from .photo_leads import PROMPT as LEAD_PROMPT
    from .photo_crop_reading import SYSTEM_PROMPT as CROP_PROMPT
    return dict(model=DEFAULT_MODEL,vision_model=DEFAULT_VISION_MODEL,verification_model=DEFAULT_MODEL,
        system_prompt=DEFAULT_SYSTEM_PROMPT,grounding_prompt=GROUNDING_PROMPT,
        verification_prompt=VERIFICATION_PROMPT,terminal_prompt=TERMINAL_PROMPT,
        lead_prompt=LEAD_PROMPT,crop_prompt=CROP_PROMPT,crop_model='',image_instructions='',
        system_prompt_single=DEFAULT_SINGLE_SYSTEM_PROMPT,
        numbering_prompt=DEFAULT_PIN_NUMBERING_PROMPT)


def bare_backup_name(value):
    """@brief File name of a recorded legacy backup when it was stored as an absolute path.

    Databases created before the bare-name rule recorded ``legacy_backup`` as
    an absolute path, which puts the user name in a file committed to the
    repository. The backup always sits next to the database, so its file
    name is enough.
    @param value Stored metadata value (any type).
    @return The last path component when @p value is an absolute Windows
        (drive or UNC) or POSIX path; otherwise @p value unchanged (empty,
        bare names and relative paths are left alone).
    """
    if not isinstance(value, str) or not value:
        return value
    for flavour in (PureWindowsPath, PurePosixPath):
        path = flavour(value)
        if path.is_absolute() and path.name:
            return path.name
    return value


class ProfileError(ValueError):
    """@brief Report invalid or unavailable AI profile data."""
    pass


class ProfileStore:
    """@brief Persist and retrieve component analysis profiles in SQLite."""
    def __init__(self,path=None,*,legacy_config=None,legacy_path=None):
        # From source, the repository database (as before); when installed,
        # the user's copy, created on first start from the distributed one.
        if not path and not os.environ.get('KICADGEN_PROFILE_DB'):
            from ..app_paths import data_file
            path = data_file('analysis_profiles.sqlite3', seed=True)
        self.path = Path(path or os.environ['KICADGEN_PROFILE_DB'])
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # In-memory read cache: the translation catalogue (one dict per
        # language, loaded with a single query) and the catalogue getters
        # (get_type, list_types, overrides, defaults, resolve). It is tagged
        # with the database file signature it was read from, so a write by
        # another store or process drops it on the next read, and every
        # write of this store drops it explicitly (_writing). The lock and
        # the generation make it safe for worker threads: a read that
        # straddles an invalidation is never stored (see _cached).
        self._cache_lock = threading.Lock()
        self._cache = {}
        self._cache_signature = None
        self._cache_generation = 0
        self._initialize(legacy_config,Path(legacy_path) if legacy_path else None)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path,timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _writing(self):
        """A connection for a write; the read cache is dropped after it, committed or not."""
        try:
            with self._connect() as db:
                yield db
        finally:
            self._invalidate_cache()

    def _invalidate_cache(self):
        """Drop every cached read and fence off the reads already in flight."""
        with self._cache_lock:
            self._cache = {}
            self._cache_signature = None
            self._cache_generation += 1

    def _cached(self, kind, argument, loader):
        """Value of `loader()` for (kind, argument), served from the read cache while the file is unchanged.

        The file signature is taken BEFORE the read: a write landing during
        the read changes the file, so the next read reloads instead of
        keeping a value older than its tag. A value is stored only if no
        invalidation happened since the read started (generation check), so
        an own write racing with a worker's read never leaves the old value
        cached. Exceptions are not cached. The cached object is shared:
        callers that hand it out copy it.
        """
        signature = self._database_signature()
        with self._cache_lock:
            if signature is None or signature != self._cache_signature:
                self._cache = {}
                self._cache_signature = signature
                self._cache_generation += 1
            generation = self._cache_generation
            value = self._cache.get((kind, argument), _MISSING)
        if value is not _MISSING:
            return value
        value = loader()
        with self._cache_lock:
            if signature is not None and generation == self._cache_generation:
                self._cache[(kind, argument)] = value
        return value

    def _initialize(self,legacy,legacy_path):
        with self._writing() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ProfileError(tx("Database profili creato da una versione più recente: nessuna modifica "
                                      "applicata."))
            if version == 1:
                db.execute("INSERT INTO ai_defaults SELECT 'verification_model',value FROM ai_defaults WHERE name='model'")
                db.execute("INSERT INTO ai_overrides SELECT type_key,'verification_model',value FROM ai_overrides WHERE name='model'")
                version = 2
            if version == 2:
                db.execute('CREATE TABLE IF NOT EXISTS translations '
                           '(key TEXT NOT NULL, language TEXT NOT NULL, text TEXT NOT NULL, '
                           'PRIMARY KEY(key,language))')
                db.executemany('INSERT OR IGNORE INTO translations VALUES (?,?,?)', _default_translations())
                db.execute('PRAGMA user_version=3')
                version = 3
            if version == 3:
                # New editable prompt: the factory text becomes the global
                # default. INSERT OR IGNORE never replaces a row already there,
                # and no per-type override is created, so every type inherits it.
                db.execute('INSERT OR IGNORE INTO ai_defaults VALUES (?,?)',
                           ('crop_prompt', factory_defaults()['crop_prompt']))
                db.execute('PRAGMA user_version=4')
                version = 4
            if version == 4:
                # Model of the crop reading (HANDOFF 150): the factory value is
                # blank («same model as the photo call»), so an upgraded
                # database behaves exactly as before. Same rules as step 3->4:
                # a row already there is never replaced, no override is added.
                db.execute('INSERT OR IGNORE INTO ai_defaults VALUES (?,?)',
                           ('crop_model', factory_defaults()['crop_model']))
                db.execute('PRAGMA user_version=5')
                version = 5
            if version == 5:
                # Datasheet prompt of a single component and prompt of the
                # dedicated numbering call (2026-10-10): factory texts become
                # the global defaults, rows already there are never replaced,
                # no per-type override is created.
                for name in ('system_prompt_single', 'numbering_prompt'):
                    db.execute('INSERT OR IGNORE INTO ai_defaults VALUES (?,?)',
                               (name, factory_defaults()[name]))
                db.execute('PRAGMA user_version=6')
                version = 6
            if version == SCHEMA_VERSION:
                # Fail explicitly on a damaged schema; never silently reset user profiles.
                db.execute('SELECT key,label,family,instructions,active,builtin,position FROM component_types LIMIT 1')
                db.execute('SELECT type_key,name,value FROM ai_overrides LIMIT 1')
                db.execute('SELECT name,value FROM metadata LIMIT 1')
                db.execute('SELECT key,language,text FROM translations LIMIT 1')
                # One-shot repair, idempotent: an absolute legacy_backup becomes
                # its bare file name; a bare or empty value is left as it is.
                row = db.execute("SELECT value FROM metadata WHERE name='legacy_backup'").fetchone()
                if row is not None and bare_backup_name(row['value']) != row['value']:
                    db.execute("UPDATE metadata SET value=? WHERE name='legacy_backup'",
                               (bare_backup_name(row['value']),))
                # Add newly bundled UI keys without overwriting user edits.
                db.executemany('INSERT OR IGNORE INTO translations VALUES (?,?,?)', _default_translations())
                # Replace bundled texts that were corrected since, but only where
                # the stored text is still exactly the old default: an entry the
                # user edited never matches and is left alone.
                db.executemany('UPDATE translations SET text=? WHERE key=? AND language=? AND text=?',
                               _superseded_translations())
                # Same rule for the labels of the built-in types.
                from .component_types import SUPERSEDED_LABELS, get as _builtin_type
                db.executemany(
                    'UPDATE component_types SET label=? WHERE key=? AND builtin=1 AND label=?',
                    [(_builtin_type(key).label, key, old)
                     for key, olds in SUPERSEDED_LABELS.items() for old in olds])
                # And for the instructions of the built-in types and the global
                # system prompt, known by digest (see _replace_superseded_texts).
                _replace_superseded_texts(db)
                defaults = dict(db.execute('SELECT name,value FROM ai_defaults'))
                if set(defaults) != set(PROFILE_FIELDS):
                    raise ProfileError(tx("Default del database incompleti: ripristinare un backup."))
                self._validate_values(defaults)
                for row in db.execute('SELECT name,value FROM ai_overrides'):
                    self._validate_values({row['name']: row['value']})
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ProfileError(tx("Relazioni del database incoerenti: ripristinare un backup."))
                return
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                raise ProfileError(tx("Database esistente senza versione riconosciuta: migrazione interrotta."))
            backup = None
            legacy_problem = ''
            raw = None
            if legacy_path and legacy_path.is_file():
                try:
                    raw = legacy_path.read_bytes()
                except OSError as exc:
                    # Unreadable legacy file: nothing to import or back up.
                    # Only the error class is recorded: the message carries
                    # the absolute path (user name), and the database is
                    # committed to the repository.
                    legacy_problem = f'unreadable: {type(exc).__name__}'
            if raw is not None:
                # A corrupt or truncated legacy config (e.g. a crash during an
                # old non-atomic save) must not block the first start. Its
                # bytes are still backed up below, so any custom prompt it
                # contained stays recoverable, and the metadata records why
                # nothing was imported: the replacement is never silent.
                try:
                    parsed = json.loads(raw)
                except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
                    parsed, legacy_problem = None, f'corrupt: {type(exc).__name__}'
                if parsed is not None and not isinstance(parsed, dict):
                    parsed, legacy_problem = None, 'corrupt: not a JSON object'
                # With a broken file the caller's in-memory config (if any) is
                # used as is: it is what load_config fell back to, exactly as
                # on a first start without any config.json.
                if parsed is not None and legacy is None:
                    legacy = parsed
                digest = hashlib.sha256(raw).hexdigest()[:16]
                backup = self.path.parent/f'config-before-sqlite-{digest}.json'
                if not backup.exists():
                    with backup.open('xb') as output:
                        output.write(raw)
                elif backup.read_bytes() != raw:
                    raise ProfileError(tx("Backup migrazione incoerente: nessuna modifica al database."))
            legacy = legacy or {}
            if not isinstance(legacy, dict):
                raise ProfileError(tx("Configurazione precedente non valida."))
            defaults = factory_defaults()
            for key in PROFILE_FIELDS:
                if isinstance(legacy.get(key),str) and (legacy[key].strip() or key in BLANK_ALLOWED_FIELDS):
                    defaults[key] = legacy[key]
            if not isinstance(legacy.get('verification_model'), str) or not legacy['verification_model'].strip():
                defaults['verification_model'] = defaults['model']
            for key in MODEL_FIELDS:
                defaults[key] = defaults[key].strip()
            db.execute('CREATE TABLE ai_defaults (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE component_types (key TEXT PRIMARY KEY, label TEXT NOT NULL, '
                       'family TEXT NOT NULL, instructions TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)), '
                       'builtin INTEGER NOT NULL CHECK(builtin IN (0,1)), position INTEGER NOT NULL)')
            db.execute('CREATE TABLE ai_overrides (type_key TEXT NOT NULL REFERENCES component_types(key) ON DELETE CASCADE, '
                       'name TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(type_key,name))')
            db.execute('CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE translations (key TEXT NOT NULL, language TEXT NOT NULL, '
                       'text TEXT NOT NULL, PRIMARY KEY(key,language))')
            db.executemany('INSERT INTO ai_defaults VALUES (?,?)',defaults.items())
            for index,t in enumerate(COMPONENT_TYPES):
                db.execute('INSERT INTO component_types VALUES (?,?,?,?,1,1,?)',
                           (t.key,t.label,t.key,t.prompt,index))
            # File name only: the backup always sits next to the database, and
            # an absolute path would put the user name in a committed file.
            db.execute('INSERT INTO metadata VALUES (?,?)',('legacy_backup',backup.name if backup else ''))
            if legacy_problem:
                db.execute('INSERT INTO metadata VALUES (?,?)',('legacy_import_skipped',legacy_problem))
            db.executemany('INSERT INTO translations VALUES (?,?,?)', _default_translations())
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _database_signature(self):
        """Modification time and size of the database file; None when it cannot be read."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _language_catalog(self, language):
        """All translations of `language` as a dict, read with one query and cached.

        The same dict object is returned while the catalogue is unchanged:
        I18n keys its reverse index on that identity. Callers must not
        mutate it.
        """
        def load():
            with self._connect() as db:
                return dict(db.execute('SELECT key,text FROM translations WHERE language=?',
                                       (language,)).fetchall())
        return self._cached('translations', language, load)

    def invalidate_translations(self):
        """@brief Drop the in-memory read cache (translations and catalogue getters); the next read uses the database.

        @note Reads already drop it when the database file changes (another
            store or process wrote it) and every write of this store drops
            it; call this after writing the file by other means within the
            same timestamp resolution.
        """
        self._invalidate_cache()

    def translation(self, key, language, fallback=None):
        """@brief Resolve one translation with an English fallback.

        @param key Translation key.
        @param language Language code; an unsupported one reads English.
        @param fallback Text returned when the key has no row in `language`;
            None falls back to the English row, then to the key itself.
        @return The translated text.
        @note Served from an in-memory catalog: one database query per
            language, not one connection per string (about 300 strings per
            main window). save_translation updates the cache at once, and a
            change of the database file (size or modification time) made
            elsewhere drops it on the next lookup.
        """
        from .i18n import LANGUAGES
        if language not in LANGUAGES:
            language = 'en'
        text = self._language_catalog(language).get(key)
        if text is not None:
            return text
        if fallback is not None:
            return fallback
        return self._language_catalog('en').get(key, key)

    def save_translation(self, key, language, text):
        """@brief Create or update one translation in the catalog.

        @param key Translation key.
        @param language Supported language code.
        @param text New non-blank text.
        @throws ProfileError When key, language or text is invalid.
        @note The new text is visible to the next `translation` call at once.
        """
        from .i18n import LANGUAGES
        if not key or language not in LANGUAGES or not isinstance(text, str) or not text.strip():
            raise ProfileError('Translation key, language and text are required.')
        with self._writing() as db:
            db.execute('INSERT INTO translations(key,language,text) VALUES (?,?,?) '
                       'ON CONFLICT(key,language) DO UPDATE SET text=excluded.text',
                       (key, language, text))

    def list_translations(self, language=None):
        """@brief List translation rows, optionally filtered by language.

        @param language Language code to keep, or None/empty for every language.
        @return List of dicts with key, language and text, ordered by key.
        """
        with self._connect() as db:
            if language:
                rows = db.execute('SELECT key,language,text FROM translations WHERE language=? '
                                  'ORDER BY key', (language,)).fetchall()
            else:
                rows = db.execute('SELECT key,language,text FROM translations ORDER BY key,language').fetchall()
            return [dict(row) for row in rows]

    def defaults(self):
        """@brief Return the global AI default values as a name-to-value dictionary.

        @return Mapping of default name to its stored value (a new dict).
        @note Served from the read cache (see _cached) while the database
            file is unchanged.
        """
        def load():
            with self._connect() as db:
                return dict(db.execute('SELECT name,value FROM ai_defaults'))
        return dict(self._cached('defaults', None, load))

    @staticmethod
    def _validate_values(values,allow_inherit=False):
        """Reject unknown fields and blank texts; None inherits only when allowed.

        A blank value is valid only for BLANK_ALLOWED_FIELDS: no image
        instructions, or a crop_model that follows the photo model. Every
        other field, the other three models included, must carry a text.
        """
        if set(values)-set(PROFILE_FIELDS):
            raise ProfileError(tx("Campo profilo non riconosciuto."))
        for key,value in values.items():
            if value is None and allow_inherit:
                continue
            if not isinstance(value,str) or (not value.strip() and key not in BLANK_ALLOWED_FIELDS):
                raise ProfileError(tx("{key}: specificare un valore oppure ereditare il default.", key=key))

    def save_defaults(self,values):
        """@brief Validate and store global AI default values; model ids are stripped.

        @param values Mapping of default names to their new values.
        @throws ProfileError When a value fails validation.
        """
        self._validate_values(values)
        values = {k: v.strip() if k.endswith('model') else v for k, v in values.items()}
        with self._writing() as db:
            db.executemany('UPDATE ai_defaults SET value=? WHERE name=?',[(v,k) for k,v in values.items()])

    def list_types(self,include_inactive=False):
        """@brief Return the component types ordered by position and label, only active ones unless `include_inactive`.

        @param include_inactive True to include the types marked inactive.
        @return List of type rows as new dicts (every column of component_types).
        @note Served from the read cache (see _cached) while the database
            file is unchanged.
        """
        def load():
            with self._connect() as db:
                query = 'SELECT * FROM component_types'
                if not include_inactive:
                    query += ' WHERE active=1'
                return [dict(r) for r in db.execute(query+' ORDER BY position,label,key')]
        return [dict(row) for row in self._cached('list_types', bool(include_inactive), load)]

    def get_type(self,key):
        """@brief Return the component type row for `key` as a dictionary, or None when absent.

        @param key Component type key.
        @return The row as a new dict, or None.
        @note Served from the read cache (see _cached) while the database
            file is unchanged: the GUI asks for the selected type's family
            on every refresh.
        """
        def load():
            with self._connect() as db:
                row = db.execute('SELECT * FROM component_types WHERE key=?',(key,)).fetchone()
                return dict(row) if row else None
        row = self._cached('get_type', key, load)
        return dict(row) if row is not None else None

    def overrides(self,key):
        """@brief Return the AI values overridden by the component type `key`.

        @param key Component type key.
        @return Mapping of field name to override text (empty when none), a new dict.
        @note Served from the read cache (see _cached) while the database
            file is unchanged.
        """
        def load():
            with self._connect() as db:
                return dict(db.execute('SELECT name,value FROM ai_overrides WHERE type_key=?',(key,)))
        return dict(self._cached('overrides', key, load))

    def resolve(self,key=''):
        """@brief Return the effective AI values: global defaults updated with the prompt overrides of `key`.

        No instructions are composed and no persisted text is changed.
        @param key Component type key; empty for the global defaults only.
        @return Mapping of every profile field to its effective value.
        @throws ProfileError When `key` is not in the catalogue.
        @note One model for every analysis (user, 2026-10-02): a type's
            override of a MODEL_FIELDS entry is ignored (see
            ignored_model_overrides), every model field resolves to the global
            'model', and 'crop_model' to blank (= that same model).
            Served from the read cache (see _cached) while the database file
            is unchanged; a missing type is not cached and raises every time.
        """
        def load():
            with self._connect() as db:
                db.execute('BEGIN')
                values = dict(db.execute('SELECT name,value FROM ai_defaults'))
                if key:
                    if not db.execute('SELECT 1 FROM component_types WHERE key=?',(key,)).fetchone():
                        raise ProfileError(tx("Tipologia non trovata nel database."))
                    values.update({name: value for name, value in db.execute(
                        'SELECT name,value FROM ai_overrides WHERE type_key=?',(key,))
                        if name not in MODEL_FIELDS})
                model = values.get('model')
                for name in ('vision_model', 'verification_model'):
                    if name in values:
                        values[name] = model
                if 'crop_model' in values:
                    values['crop_model'] = ''
                return values
        return dict(self._cached('resolve', key or '', load))

    def ignored_model_overrides(self,key):
        """@brief Model overrides stored for the type `key` that resolve() now ignores.

        @param key Component type key (empty: none).
        @return {field: model} of the non-blank model overrides, so the caller
            can say they are no longer used.
        """
        if not key:
            return {}
        return {name: value for name, value in self.overrides(key).items()
                if name in MODEL_FIELDS and isinstance(value, str) and value.strip()}

    def snapshot(self,key=''):
        """@brief Return resolved values with the type image instructions appended to the whole-board photo prompts.

        @param key Component type key; empty for the global defaults only.
        @return Resolved values; each field of PHOTO_PROMPT_FIELDS carries the
            image instructions when they are not blank.
        @throws ProfileError When `key` is not in the catalogue.
        @note 'crop_prompt' is returned as resolved, WITHOUT the image
            instructions (HANDOFF 146 V3): they describe the whole board,
            while the crop reading looks at one body; a type customises the
            per-body text in its own 'crop_prompt' override. Consequence: a
            type that inherits the global crop prompt sends the same text as
            the global default, and shares its crop-reading cache entries.
        """
        values = self.resolve(key)
        extra = values['image_instructions'].strip()
        if extra:
            for field in PHOTO_PROMPT_FIELDS:
                values[field] += '\n\nIstruzioni specifiche per le immagini:\n'+extra
        return values

    def save_type(self,key,*,label,family,instructions='',active=True,overrides=None):
        """@brief Create (empty `key`) or update a component type and optionally replace its overrides.

        @param key Existing type key, or a false value to create a new custom type.
        @param label Display name of the type; must not be blank.
        @param family Geometric family, one of FAMILIES.
        @param instructions Instruction block inserted in the component notes.
        @param active False to hide the type from the selectable list.
        @param overrides New override mapping (None values inherit); None keeps the stored ones.
        @return The key of the saved type.
        @throws ProfileError On missing label, invalid family or data, or a type removed meanwhile.
        """
        if not isinstance(label,str) or not label.strip():
            raise ProfileError(tx("Il nome della tipologia è obbligatorio."))
        if family not in FAMILIES or not isinstance(instructions,str) or type(active) is not bool:
            raise ProfileError(tx("Famiglia geometrica o dati della tipologia non validi."))
        values = overrides or {}
        self._validate_values(values,allow_inherit=True)
        values = {k: v.strip() if k.endswith('model') and v is not None else v for k, v in values.items()}
        creating = not key
        key = key or 'custom_'+uuid.uuid4().hex
        with self._writing() as db:
            exists = db.execute('SELECT 1 FROM component_types WHERE key=?',(key,)).fetchone()
            if not exists and not creating:
                raise ProfileError(tx("Tipologia non più disponibile: riaprire il catalogo."))
            if exists:
                db.execute('UPDATE component_types SET label=?,family=?,instructions=?,active=? WHERE key=?',
                           (label.strip(),family,instructions,int(active),key))
            else:
                position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM component_types').fetchone()[0]
                db.execute('INSERT INTO component_types VALUES (?,?,?,?,?,0,?)',
                           (key,label.strip(),family,instructions,int(active),position))
            if overrides is not None:
                db.execute('DELETE FROM ai_overrides WHERE type_key=?',(key,))
                db.executemany('INSERT INTO ai_overrides VALUES (?,?,?)',
                               [(key,k,v) for k,v in values.items() if v is not None])
        return key

    def duplicate(self,key):
        """@brief Create an active copy of type `key`, including its overrides, and return the new key.

        @param key Key of the type to copy.
        @return The key of the new custom type.
        @throws ProfileError When `key` does not exist.
        """
        original = self.get_type(key)
        if not original:
            raise ProfileError(tx("Tipologia da duplicare non trovata."))
        return self.save_type(None,label=original['label']+' — copia',family=original['family'],
            instructions=original['instructions'],active=True,overrides=self.overrides(key))

    def delete_type(self,key):
        """@brief Delete the component type `key` from the catalogue.

        @param key Key of the type to delete.
        @throws ProfileError When `key` does not exist.
        """
        with self._writing() as db:
            if db.execute('DELETE FROM component_types WHERE key=?',(key,)).rowcount != 1:
                raise ProfileError(tx("Tipologia da eliminare non trovata."))

    def backup(self,destination):
        """@brief Copy the database into a new file with the SQLite backup API.

        @param destination New file path, different from the active database.
        @return The destination path.
        @throws ProfileError When the destination is the active database or already exists.
        """
        destination = Path(destination)
        if destination.resolve()==self.path.resolve() or destination.exists():
            raise ProfileError(tx("Scegliere un nuovo file di backup, diverso dal database attivo."))
        destination.parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        return destination
