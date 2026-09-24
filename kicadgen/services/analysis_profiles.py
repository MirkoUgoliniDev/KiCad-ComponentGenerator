"""Versioned SQLite catalog of component types and inherited AI profiles.

Connections are short-lived; analysis workers receive resolved snapshots, never
connections. The legacy JSON is backed up and imported once, never overwritten.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import uuid

from .component_types import COMPONENT_TYPES
from .messages import tx

PROFILE_FIELDS = ('model','vision_model','verification_model','system_prompt','grounding_prompt',
                  'verification_prompt','terminal_prompt','lead_prompt','image_instructions')
FAMILIES = {t.key:t.label for t in COMPONENT_TYPES}
SCHEMA_VERSION = 3


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


def factory_defaults():
    """@brief Provide the factory defaults operation for this workflow."""
    from .datasheet_extractor import DEFAULT_SYSTEM_PROMPT
    from .openrouter import DEFAULT_MODEL
    from .photo_grounding import DEFAULT_VISION_MODEL, GROUNDING_PROMPT, VERIFICATION_PROMPT
    from .photo_terminals import PROMPT as TERMINAL_PROMPT
    from .photo_leads import PROMPT as LEAD_PROMPT
    return dict(model=DEFAULT_MODEL,vision_model=DEFAULT_VISION_MODEL,verification_model=DEFAULT_MODEL,
        system_prompt=DEFAULT_SYSTEM_PROMPT,grounding_prompt=GROUNDING_PROMPT,
        verification_prompt=VERIFICATION_PROMPT,terminal_prompt=TERMINAL_PROMPT,
        lead_prompt=LEAD_PROMPT,image_instructions='')


class ProfileError(ValueError):
    """@brief Report invalid or unavailable AI profile data."""
    pass


class ProfileStore:
    """@brief Persist and retrieve component analysis profiles in SQLite."""
    def __init__(self,path=None,*,legacy_config=None,legacy_path=None):
        root = Path(__file__).resolve().parents[2]
        self.path = Path(path or os.environ.get('KICADGEN_PROFILE_DB',root/'data'/'analysis_profiles.sqlite3'))
        self.path.parent.mkdir(parents=True,exist_ok=True)
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

    def _initialize(self,legacy,legacy_path):
        with self._connect() as db:
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
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                version = SCHEMA_VERSION
            if version == SCHEMA_VERSION:
                # Fail explicitly on a damaged schema; never silently reset user profiles.
                db.execute('SELECT key,label,family,instructions,active,builtin,position FROM component_types LIMIT 1')
                db.execute('SELECT type_key,name,value FROM ai_overrides LIMIT 1')
                db.execute('SELECT name,value FROM metadata LIMIT 1')
                db.execute('SELECT key,language,text FROM translations LIMIT 1')
                # Add newly bundled UI keys without overwriting user edits.
                db.executemany('INSERT OR IGNORE INTO translations VALUES (?,?,?)', _default_translations())
                # Replace bundled texts that were corrected since, but only where
                # the stored text is still exactly the old default: an entry the
                # user edited never matches and is left alone.
                db.executemany('UPDATE translations SET text=? WHERE key=? AND language=? AND text=?',
                               _superseded_translations())
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
            if legacy_path and legacy_path.is_file():
                raw = legacy_path.read_bytes()
                # A corrupt source must not silently become a new empty configuration.
                parsed = json.loads(raw)
                if not isinstance(parsed,dict):
                    raise ProfileError(tx("La configurazione precedente non è un oggetto JSON."))
                if legacy is None:
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
                if isinstance(legacy.get(key),str) and (legacy[key].strip() or key=='image_instructions'):
                    defaults[key] = legacy[key]
            if not isinstance(legacy.get('verification_model'), str) or not legacy['verification_model'].strip():
                defaults['verification_model'] = defaults['model']
            for key in ('model', 'vision_model', 'verification_model'):
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
            db.execute('INSERT INTO metadata VALUES (?,?)',('legacy_backup',str(backup or '')))
            db.executemany('INSERT INTO translations VALUES (?,?,?)', _default_translations())
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def translation(self, key, language, fallback=None):
        """@brief Resolve one translation with an English fallback."""
        from .i18n import LANGUAGES
        if language not in LANGUAGES:
            language = 'en'
        with self._connect() as db:
            row = db.execute('SELECT text FROM translations WHERE key=? AND language=?',
                             (key, language)).fetchone()
            if row:
                return row['text']
            if fallback is not None:
                return fallback
            row = db.execute('SELECT text FROM translations WHERE key=? AND language=?',
                             (key, 'en')).fetchone()
            return row['text'] if row else key

    def save_translation(self, key, language, text):
        """@brief Create or update one translation in the catalog."""
        from .i18n import LANGUAGES
        if not key or language not in LANGUAGES or not isinstance(text, str) or not text.strip():
            raise ProfileError('Translation key, language and text are required.')
        with self._connect() as db:
            db.execute('INSERT INTO translations(key,language,text) VALUES (?,?,?) '
                       'ON CONFLICT(key,language) DO UPDATE SET text=excluded.text',
                       (key, language, text))

    def list_translations(self, language=None):
        """@brief List translation rows, optionally filtered by language."""
        with self._connect() as db:
            if language:
                rows = db.execute('SELECT key,language,text FROM translations WHERE language=? '
                                  'ORDER BY key', (language,)).fetchall()
            else:
                rows = db.execute('SELECT key,language,text FROM translations ORDER BY key,language').fetchall()
            return [dict(row) for row in rows]

    def defaults(self):
        """@brief Provide the defaults operation for this workflow."""
        with self._connect() as db:
            return dict(db.execute('SELECT name,value FROM ai_defaults'))

    @staticmethod
    def _validate_values(values,allow_inherit=False):
        if set(values)-set(PROFILE_FIELDS):
            raise ProfileError(tx("Campo profilo non riconosciuto."))
        for key,value in values.items():
            if value is None and allow_inherit:
                continue
            if not isinstance(value,str) or (not value.strip() and key!='image_instructions'):
                raise ProfileError(tx("{key}: specificare un valore oppure ereditare il default.", key=key))

    def save_defaults(self,values):
        """@brief Provide the save defaults operation for this workflow."""
        self._validate_values(values)
        values = {k: v.strip() if k.endswith('model') else v for k, v in values.items()}
        with self._connect() as db:
            db.executemany('UPDATE ai_defaults SET value=? WHERE name=?',[(v,k) for k,v in values.items()])

    def list_types(self,include_inactive=False):
        """@brief Provide the list types operation for this workflow."""
        with self._connect() as db:
            query = 'SELECT * FROM component_types'
            if not include_inactive:
                query += ' WHERE active=1'
            return [dict(r) for r in db.execute(query+' ORDER BY position,label,key')]

    def get_type(self,key):
        """@brief Provide the get type operation for this workflow."""
        with self._connect() as db:
            row = db.execute('SELECT * FROM component_types WHERE key=?',(key,)).fetchone()
            return dict(row) if row else None

    def overrides(self,key):
        """@brief Provide the overrides operation for this workflow."""
        with self._connect() as db:
            return dict(db.execute('SELECT name,value FROM ai_overrides WHERE type_key=?',(key,)))

    def resolve(self,key=''):
        """Effective values without composing instructions or changing persisted text."""
        with self._connect() as db:
            db.execute('BEGIN')
            values = dict(db.execute('SELECT name,value FROM ai_defaults'))
            if key:
                if not db.execute('SELECT 1 FROM component_types WHERE key=?',(key,)).fetchone():
                    raise ProfileError(tx("Tipologia non trovata nel database."))
                values.update(dict(db.execute('SELECT name,value FROM ai_overrides WHERE type_key=?',(key,))))
            return values

    def snapshot(self,key=''):
        """@brief Provide the snapshot operation for this workflow."""
        values = self.resolve(key)
        extra = values['image_instructions'].strip()
        if extra:
            for field in ('grounding_prompt','verification_prompt','terminal_prompt','lead_prompt'):
                values[field] += '\n\nIstruzioni specifiche per le immagini:\n'+extra
        return values

    def save_type(self,key,*,label,family,instructions='',active=True,overrides=None):
        """@brief Provide the save type operation for this workflow."""
        if not isinstance(label,str) or not label.strip():
            raise ProfileError(tx("Il nome della tipologia è obbligatorio."))
        if family not in FAMILIES or not isinstance(instructions,str) or type(active) is not bool:
            raise ProfileError(tx("Famiglia geometrica o dati della tipologia non validi."))
        values = overrides or {}
        self._validate_values(values,allow_inherit=True)
        values = {k: v.strip() if k.endswith('model') and v is not None else v for k, v in values.items()}
        creating = not key
        key = key or 'custom_'+uuid.uuid4().hex
        with self._connect() as db:
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
        """@brief Provide the duplicate operation for this workflow."""
        original = self.get_type(key)
        if not original:
            raise ProfileError(tx("Tipologia da duplicare non trovata."))
        return self.save_type(None,label=original['label']+' — copia',family=original['family'],
            instructions=original['instructions'],active=True,overrides=self.overrides(key))

    def delete_type(self,key):
        """@brief Provide the delete type operation for this workflow."""
        with self._connect() as db:
            if db.execute('DELETE FROM component_types WHERE key=?',(key,)).rowcount != 1:
                raise ProfileError(tx("Tipologia da eliminare non trovata."))

    def backup(self,destination):
        """@brief Provide the backup operation for this workflow."""
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
