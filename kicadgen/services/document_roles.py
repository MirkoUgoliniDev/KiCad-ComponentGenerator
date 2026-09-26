"""@brief Explicit document roles per project, with recoverable local copies.

@details Since 2026-09-26 (manifest version 2) one file can carry SEVERAL
roles: a datasheet PDF often holds the pinout, the dimensioned top view and
the recommended land pattern at once, and asking the user to load it three
times -- or to pick only one of its roles -- lost information. The manifest is

    {"version": 2,
     "roles": {"<file name>": ["pinout", "top", ...], ...},
     "original_names": {"<file name>": "<name of the file the user chose>"}}

Only the files declared as a photograph (`PHOTO_ROLE`) reach the photo
inventory, the YOLO detector and the photo calibration: a dimensioned drawing
is read for its dimensions, never searched for component bodies.

Version 1 manifests (one file per role, `{"roles": {role: {"filename": ...}}}`)
are migrated on read: every file gets its role, and when the project has a
'top' file and NO 'hires' file the 'top' file also gets 'hires', because
under version 1 the top view doubled as the photograph when no photograph was
loaded. The migrated manifest is written back only by the next change.
"""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from .messages import tx
from ..fsutil import replace_file

#: Roles a document can carry, with the label shown to the user and sent to
#: the AI. The order is the display order.
ROLES = {'pinout': 'Pinout / disposizione contatti', 'top': 'Immagine con quote',
         'hires': 'Foto ad alta risoluzione',
         'land_pattern': 'Land pattern consigliato',
         'side_view': 'Vista laterale / altezze'}
#: Short labels for the role check boxes of a document card; the full label
#: and the help stay in the tooltip.
ROLE_SHORT = {'pinout': 'Pinout', 'top': 'Quote', 'hires': 'Foto',
              'land_pattern': 'Land pattern', 'side_view': 'Vista laterale'}
#: The role that sends a file to the photo inventory (detector, calibration).
PHOTO_ROLE = 'hires'
#: Roles whose files the photo inventory examines. Kept as a tuple for the
#: callers written for version 1; since version 2 it is the photograph only.
INVENTORY_ROLES = (PHOTO_ROLE,)
ROLE_HELP = {
    'pinout': 'Usa questo documento per contatti, numerazione e quote del pinout. '
              'Non assumere che il disegno sia visto dall’alto: verifica le indicazioni presenti.',
    'top': 'Disegno quotato visto dall’alto, dichiarato dall’utente: usalo per le quote '
           'meccaniche -- lunghezza e larghezza del corpo, posizione dei fori, offset delle '
           'file di contatti. Non sostituirlo con il pinout.',
    'hires': 'Foto dall’alto ad alta risoluzione, dichiarata dall’utente: usa questa '
             'immagine per individuare i componenti piccoli, che in una vista a bassa '
             'risoluzione non sono distinguibili. Deve essere una fotografia del pezzo '
             'reale, non un disegno tecnico a tratto.',
    'land_pattern': 'Land pattern (impronta) consigliato dal costruttore, dichiarato '
                    'dall’utente: usalo per forma, misure e posizione delle piazzole e dei fori.',
    'side_view': 'Vista laterale dichiarata dall’utente: usala per le altezze (corpo, '
                 'componenti, spessore della scheda); le quote in pianta vengono dalle altre viste.',
}
SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.pdf'}
#: Suffixes a photograph can have: the inventory decodes images, not PDFs.
PHOTO_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
MANIFEST = '.document-roles.json'
MANIFEST_VERSION = 2
#: Longest stem kept from the user's file name for the local copy.
_MAX_STEM = 60
_WINDOWS_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                     *(f'lpt{i}' for i in range(1, 10))}


class DocumentError(ValueError):
    """@brief Report invalid guided-document assignments."""
    pass


def role_list(value) -> list:
    """@brief Normalise a declared role value to a list of known roles.

    @param value A role key, a list/tuple of role keys, or None.
    @return The known roles in `ROLES` order, without duplicates; unknown
        values are dropped (a role is never guessed from a near miss).
    @note Accepts the single strings of version 1 so callers written before
        the list interface keep working.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [role for role in ROLES if role in value]


def photo_paths(doc_paths, document_roles) -> list:
    """@brief The documents declared as a photograph, in request order.

    @param doc_paths Selected documents.
    @param document_roles File name -> role list (or single role), or None.
    @return Paths carrying `PHOTO_ROLE`; an empty list when roles are None
        (then nothing is declared and the caller decides what to do).
    """
    if document_roles is None:
        return []
    return [Path(p) for p in doc_paths
            if PHOTO_ROLE in role_list(document_roles.get(Path(p).name))]


def valid_document_name(name) -> bool:
    """@brief Whether `name` may be a document of the manifest.

    @param name Candidate file name.
    @return True for a plain file name inside the docs folder: no directory
        part, not hidden, a known suffix, no character Windows refuses, not a
        reserved device name. Protects every path built from the manifest.
    """
    if not isinstance(name, str) or not 0 < len(name) <= _MAX_STEM + 12:
        return False
    if name != Path(name).name or name.startswith('.') or name == MANIFEST:
        return False
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name != name.rstrip(' .'):
        return False
    path = Path(name)
    return path.suffix.lower() in SUFFIXES and path.stem.lower() not in _WINDOWS_RESERVED \
        and bool(path.stem.strip())


def _local_name(source_name: str) -> str:
    """The user's file name made safe for the docs folder (ASCII, short, suffix lowered)."""
    path = Path(source_name)
    stem = re.sub(r'[^A-Za-z0-9._ -]+', '_', path.stem).strip(' ._')[:_MAX_STEM].rstrip(' ._')
    if not stem or stem.lower() in _WINDOWS_RESERVED:
        stem = 'documento' + ('_' + stem if stem else '')
    return stem + path.suffix.lower()


def _migrate_v1(data: dict) -> dict:
    """Convert a validated version 1 manifest into version 2 (see module doc)."""
    roles, originals = {}, {}
    for role, row in data['roles'].items():
        name = row.get('filename')
        if not name:
            continue
        roles.setdefault(name, [])
        if role not in roles[name]:
            roles[name].append(role)
        if isinstance(row.get('original_name'), str):
            originals.setdefault(name, row['original_name'])
    has_photo = any(PHOTO_ROLE in r for r in roles.values())
    if not has_photo:
        # Version 1 used the top view as the photograph when none was loaded:
        # same behaviour, now declared.
        for name, declared in roles.items():
            if 'top' in declared and Path(name).suffix.lower() in PHOTO_SUFFIXES:
                declared.append(PHOTO_ROLE)
    return {'version': MANIFEST_VERSION,
            'roles': {name: role_list(r) for name, r in roles.items()},
            'original_names': originals}


class DocumentSet:
    """@brief The documents of one project and the roles the user declared for each."""
    def __init__(self, directory):
        """@brief Bind the set to a docs folder; nothing is read or created yet.

        @param directory The project's docs folder.
        """
        self.directory = Path(directory)
        self.path = self.directory / MANIFEST

    def read(self):
        """@brief Read and validate the role manifest; a missing manifest yields an empty one.

        @return Version 2 manifest dictionary (`version`, `roles`,
            `original_names`); a version 1 file is migrated in memory.
        @throws DocumentError When the manifest is malformed; the file is left untouched.
        """
        if not self.path.exists():
            return {'version': MANIFEST_VERSION, 'roles': {}, 'original_names': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('roles'), dict):
                raise ValueError(tx("versione non riconosciuta"))
            if data.get('version') == 1:
                return _migrate_v1(self._validated_v1(data))
            if data.get('version') != MANIFEST_VERSION:
                raise ValueError(tx("versione non riconosciuta"))
            for name, roles in data['roles'].items():
                if not valid_document_name(name):
                    raise ValueError(tx("nome file non valido"))
                if not isinstance(roles, list) or any(r not in ROLES for r in roles):
                    raise ValueError(tx("ruolo non riconosciuto"))
            originals = data.get('original_names', {})
            if not isinstance(originals, dict):
                raise ValueError(tx("nome file non valido"))
            return {'version': MANIFEST_VERSION,
                    'roles': {name: role_list(roles) for name, roles in data['roles'].items()},
                    'original_names': {k: v for k, v in originals.items()
                                       if k in data['roles'] and isinstance(v, str)}}
        except (ValueError, AttributeError, TypeError) as exc:
            raise DocumentError(tx("Associazioni documenti non valide: {exc}. Il file non è stato "
                                   "modificato.",
                                   exc=exc)) from exc

    @staticmethod
    def _validated_v1(data):
        """The checks of the version 1 reader, kept for the migration."""
        v1_roles = ('pinout', 'top', 'hires')
        if set(data['roles']) - set(v1_roles):
            raise ValueError(tx("ruolo non riconosciuto"))
        for role, row in data['roles'].items():
            if not isinstance(row, dict) or type(row.get('unavailable', False)) is not bool:
                raise ValueError(tx("ruolo non valido"))
            name = row.get('filename')
            if name is not None and (not isinstance(name, str)
                                     or name not in {role + ext for ext in SUFFIXES}):
                raise ValueError(tx("nome file non valido"))
            if name and row.get('unavailable'):
                raise ValueError('ruolo ambiguo')
        return data

    def _write(self, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='.roles-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            replace_file(path, self.path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _archive(self, paths):
        existing = [p for p in paths if p.is_file()]
        if not existing:
            return None
        directory = self.directory / '.history' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        for path in existing:
            shutil.copy2(path, directory / path.name)
        return directory

    @staticmethod
    def _check_source(source):
        """Refuse a file that is not a readable image/PDF of at most 20 MB."""
        suffix = source.suffix.lower()
        if suffix not in SUFFIXES or not source.is_file():
            raise DocumentError(tx("Carica un’immagine PNG, JPEG, WEBP, GIF o un PDF."))
        if not 0 < source.stat().st_size <= 20 * 1024 * 1024:
            raise DocumentError(tx("File vuoto o superiore al limite di 20 MB."))
        if suffix == '.pdf':
            with source.open('rb') as stream:
                if b'%PDF-' not in stream.read(1024):
                    raise DocumentError(tx("Il file non contiene un PDF riconoscibile."))
            return
        from PIL import Image
        try:
            with Image.open(source) as img:
                expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP', '.gif': 'GIF'}
                if img.format != expected[suffix]:
                    raise DocumentError(tx("Estensione e formato dell’immagine non corrispondono."))
                img.verify()
        except (OSError, Image.DecompressionBombError) as exc:
            raise DocumentError(tx("Immagine non leggibile o troppo grande.")) from exc

    def _same_content(self, source, data):
        """Name of a document of the manifest with exactly the bytes of `source`, or None."""
        size = source.stat().st_size
        content = None
        for name in data['roles']:
            path = self.directory / name
            try:
                if not path.is_file() or path.stat().st_size != size:
                    continue
                if content is None:
                    content = source.read_bytes()
                if path.read_bytes() == content:
                    return name
            except OSError:
                continue
        return None

    def _free_name(self, wanted, data, keep=None):
        """`wanted`, or `stem-2`, `stem-3`... when a file or a manifest entry already uses it."""
        stem, suffix = Path(wanted).stem, Path(wanted).suffix
        candidate, index = wanted, 1
        taken = {n.lower() for n in data['roles'] if n != keep}
        while candidate.lower() in taken or (
                candidate != keep and (self.directory / candidate).exists()):
            index += 1
            candidate = f'{stem}-{index}{suffix}'
        return candidate

    def import_file(self, roles, source, *, replace=None):
        """@brief Copy a validated image or PDF into the project with the given roles.

        @param roles Role key or list of role keys the new document carries
            (may be empty: the card then asks the user to tick them).
        @param source File to import, at most 20 MB.
        @param replace Name of an existing document this file replaces; its
            roles are kept (and `roles` added). The old copy is archived in
            docs/.history. None adds a new document.
        @return Path of the installed local copy. When a document with the
            very same bytes is already in the project, no copy is made: the
            roles are added to that document and its path is returned.
        @throws DocumentError On unknown role, unsupported or unreadable file,
            unknown document to replace, or a photograph role on a PDF.
        @note The previous files are archived first and restored if the update fails.
        """
        wanted = role_list(roles)
        if (isinstance(roles, str) and roles not in ROLES) or (
                isinstance(roles, (list, tuple)) and len(wanted) != len(set(roles))):
            raise DocumentError(tx("Ruolo non riconosciuto."))
        source = Path(source)
        self._check_source(source)
        suffix = source.suffix.lower()
        if PHOTO_ROLE in wanted and suffix not in PHOTO_SUFFIXES:
            raise DocumentError(tx("Una foto deve essere un’immagine, non un PDF."))
        data = self.read()
        if replace is not None and replace not in data['roles']:
            raise DocumentError(tx("Documento da sostituire non trovato: {name}.", name=replace))
        if replace is None:
            existing = self._same_content(source, data)
            if existing is not None:
                if PHOTO_ROLE in wanted and Path(existing).suffix.lower() not in PHOTO_SUFFIXES:
                    raise DocumentError(tx("Una foto deve essere un’immagine, non un PDF."))
                self.set_roles(existing, data['roles'][existing] + wanted)
                return self.directory / existing
        keep_roles = data['roles'].get(replace, []) if replace else []
        new_roles = role_list(keep_roles + wanted)
        if PHOTO_ROLE in new_roles and suffix not in PHOTO_SUFFIXES:
            raise DocumentError(tx("Una foto deve essere un’immagine, non un PDF."))
        target_name = self._free_name(_local_name(source.name), data, keep=replace)
        target = self.directory / target_name
        self.directory.mkdir(parents=True, exist_ok=True)
        previous = self.directory / replace if replace else None
        protected = list(dict.fromkeys(p for p in (target, previous, self.path) if p is not None))
        archive = self._archive(protected)
        fd, staged = tempfile.mkstemp(prefix='.import-', suffix='.tmp', dir=self.directory)
        os.close(fd)
        installed = False
        try:
            shutil.copyfile(source, staged)
            replace_file(staged, target)
            installed = True
            ordered = {}
            for name, declared in data['roles'].items():
                # The replacement takes the place of the old document in the order.
                if name == replace:
                    ordered[target_name] = new_roles
                else:
                    ordered[name] = declared
            ordered.setdefault(target_name, new_roles)
            originals = {k: v for k, v in data['original_names'].items() if k != replace}
            originals[target_name] = source.name
            self._write({'version': MANIFEST_VERSION, 'roles': ordered,
                         'original_names': originals})
        except Exception:
            if installed:
                backup = archive / target.name if archive else None
                if backup and backup.is_file():
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            raise
        finally:
            Path(staged).unlink(missing_ok=True)
        # Old versions are retained in .history, never selected by the AI.
        if previous and previous != target and previous.is_file() and previous.resolve() != source.resolve():
            previous.unlink()
        return target

    def set_roles(self, name, roles):
        """@brief Declare the roles of one document (replacing its previous ones).

        @param name Document file name in the manifest.
        @param roles Role keys; an empty list is allowed and blocks the
            analysis until the user ticks a role or removes the file.
        @throws DocumentError For an unknown document or role, or a photograph role on a PDF.
        """
        data = self.read()
        if name not in data['roles']:
            raise DocumentError(tx("Documento non trovato: {name}.", name=name))
        roles = [roles] if isinstance(roles, str) else list(roles or ())
        wanted = role_list(roles)
        if len(wanted) != len(set(roles)):
            raise DocumentError(tx("Ruolo non riconosciuto."))
        if PHOTO_ROLE in wanted and Path(name).suffix.lower() not in PHOTO_SUFFIXES:
            raise DocumentError(tx("Una foto deve essere un’immagine, non un PDF."))
        data['roles'][name] = wanted
        self._write(data)

    def remove(self, name):
        """@brief Remove one document after archiving it.

        @param name Document file name in the manifest.
        @return The .history folder holding the previous files, or None when nothing was archived.
        @throws DocumentError When the document is not in the manifest.
        """
        data = self.read()
        if name not in data['roles']:
            raise DocumentError(tx("Documento non trovato: {name}.", name=name))
        old = self.directory / name
        archive = self._archive([old, self.path])
        del data['roles'][name]
        data['original_names'].pop(name, None)
        self._write(data)
        if old.is_file():
            old.unlink()  # Recoverable from the archive made before changing the manifest.
        return archive

    def documents(self):
        """@brief The documents of the manifest, in order.

        @return List of (file name, roles, original name) tuples.
        @throws DocumentError When the manifest is malformed.
        """
        data = self.read()
        return [(name, list(roles), data['original_names'].get(name, ''))
                for name, roles in data['roles'].items()]

    def selected(self):
        """@brief Return the documents to analyse and their roles.

        @return Tuple (paths, roles) where roles maps each file name to the
            list of its roles.
        @throws DocumentError When a document has no role, a file is missing,
            or nothing is loaded.
        @note A role no document carries is simply not provided: the
            extraction tells the AI which views are missing and never fills
            them with another file.
        """
        paths, roles = [], {}
        for name, declared in self.read()['roles'].items():
            if not declared:
                raise DocumentError(tx("«{name}» non ha un ruolo: spunta cosa contiene oppure "
                                       "rimuovilo.", name=name))
            path = self.directory / name
            if not path.is_file():
                raise DocumentError(tx("File mancante: {name}. Caricalo nuovamente.", name=name))
            paths.append(path)
            roles[name] = list(declared)
        if not paths:
            raise DocumentError(tx("Carica almeno un documento per avviare l’analisi."))
        return paths, roles
