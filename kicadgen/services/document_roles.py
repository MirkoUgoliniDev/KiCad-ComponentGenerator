"""Explicit document roles per project, with recoverable local copies."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from .messages import tx

ROLES = {'pinout': 'Pinout / disposizione contatti', 'top': 'Immagine con quote',
         'hires': 'Foto ad alta risoluzione'}
#: Ruolo preferito per l'inventario fotografico, quando disponibile: a
#: parita' di algoritmo e' la risoluzione a decidere quanti passivi
#: piccoli si riescono a distinguere.
INVENTORY_ROLES = ('hires', 'top')
ROLE_HELP = {
    'pinout': 'Usa questo documento per contatti, numerazione e quote del pinout. '
              'Non assumere che il disegno sia visto dall’alto: verifica le indicazioni presenti.',
    'top': 'Immagine quotata vista dall’alto, dichiarata dall’utente: usa questa per le '
           'quote meccaniche -- lunghezza e larghezza del corpo, posizione dei fori, offset '
           'delle file di contatti. Non sostituirla con il pinout. Serve anche '
           'a localizzare i componenti quando manca la foto ad alta risoluzione.',
    'hires': 'Foto dall’alto ad alta risoluzione, dichiarata dall’utente: usa questa '
             'immagine per individuare i componenti piccoli, che in una vista a bassa '
             'risoluzione non sono distinguibili. Deve essere una fotografia del pezzo '
             'reale, non un disegno tecnico a tratto.',
}
SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.pdf'}
MANIFEST = '.document-roles.json'


class DocumentError(ValueError):
    """@brief Report invalid guided-document assignments."""
    pass


class DocumentSet:
    """@brief Represent the canonical pinout and top-view source files."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / MANIFEST

    def read(self):
        """@brief Provide the read operation for this workflow."""
        if not self.path.exists():
            return {'version': 1, 'roles': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('roles'), dict):
                raise ValueError(tx("versione non riconosciuta"))
            if set(data['roles']) - set(ROLES):
                raise ValueError(tx("ruolo non riconosciuto"))
            for role, row in data['roles'].items():
                if not isinstance(row, dict) or type(row.get('unavailable', False)) is not bool:
                    raise ValueError(tx("ruolo non valido"))
                name = row.get('filename')
                if name is not None and (not isinstance(name, str) or name not in {role + ext for ext in SUFFIXES}):
                    raise ValueError(tx("nome file non valido"))
                if name and row.get('unavailable'):
                    raise ValueError('ruolo ambiguo')
            return data
        except (ValueError, AttributeError, TypeError) as exc:
            raise DocumentError(tx("Associazioni documenti non valide: {exc}. Il file non è stato "
                                   "modificato.",
                                   exc=exc)) from exc

    def _write(self, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix='.roles-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(path, self.path)
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

    def import_file(self, role, source, *, replace=False):
        """@brief Provide the import file operation for this workflow."""
        if role not in ROLES:
            raise DocumentError(tx("Ruolo non riconosciuto."))
        source = Path(source)
        suffix = source.suffix.lower()
        if suffix not in SUFFIXES or not source.is_file():
            raise DocumentError(tx("Carica un’immagine PNG, JPEG, WEBP, GIF o un PDF."))
        if not 0 < source.stat().st_size <= 20 * 1024 * 1024:
            raise DocumentError(tx("File vuoto o superiore al limite di 20 MB."))
        if suffix == '.pdf':
            with source.open('rb') as stream:
                if b'%PDF-' not in stream.read(1024):
                    raise DocumentError(tx("Il file non contiene un PDF riconoscibile."))
        else:
            from PIL import Image
            try:
                with Image.open(source) as img:
                    expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP', '.gif': 'GIF'}
                    if img.format != expected[suffix]:
                        raise DocumentError(tx("Estensione e formato dell’immagine non corrispondono."))
                    img.verify()
            except (OSError, Image.DecompressionBombError) as exc:
                raise DocumentError(tx("Immagine non leggibile o troppo grande.")) from exc
        data = self.read()
        target = self.directory / (role + suffix)
        old_name = data['roles'].get(role, {}).get('filename')
        if (old_name or target.exists()) and not replace:
            raise DocumentError(tx("Confermare la sostituzione del documento esistente."))
        self.directory.mkdir(parents=True, exist_ok=True)
        previous = self.directory / old_name if old_name else None
        protected = list(dict.fromkeys(p for p in (target, previous, self.path) if p is not None))
        archive = self._archive(protected)
        fd, staged = tempfile.mkstemp(prefix='.import-', suffix='.tmp', dir=self.directory)
        os.close(fd)
        installed = False
        try:
            shutil.copyfile(source, staged)
            os.replace(staged, target)
            installed = True
            data['roles'][role] = {'filename': target.name, 'original_name': source.name, 'unavailable': False}
            self._write(data)
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

    def clear(self, role, *, unavailable=False):
        """@brief Provide the clear operation for this workflow."""
        if role not in ROLES:
            raise DocumentError(tx("Ruolo non riconosciuto."))
        data = self.read()
        name = data['roles'].get(role, {}).get('filename')
        old = self.directory / name if name else None
        archive = self._archive([p for p in (old, self.path) if p is not None])
        data['roles'][role] = {'unavailable': unavailable}
        self._write(data)
        if old and old.is_file():
            old.unlink()  # Recoverable from the archive made before changing the manifest.
        return archive

    def selected(self):
        """@brief Provide the selected operation for this workflow."""
        rows = self.read()['roles']
        paths, roles = [], {}
        for role, label in ROLES.items():
            row = rows.get(role, {})
            if row.get('unavailable'):
                continue
            name = row.get('filename')
            if not name:
                raise DocumentError(tx("{label}: carica un file oppure indica “Non disponibile”.", label=label))
            path = self.directory / name
            if not path.is_file():
                raise DocumentError(tx("File mancante per {label}: {name}. Caricalo nuovamente.",
                                       label=label,
                                       name=name))
            paths.append(path)
            roles[name] = role
        if not paths:
            raise DocumentError(tx("Carica almeno un documento per avviare l’analisi."))
        return paths, roles
