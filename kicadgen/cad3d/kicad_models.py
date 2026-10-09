"""@brief Resolve installed KiCad solids against photographic mechanical envelopes.

Perche' esiste questo modulo
----------------------------
Finora ogni package veniva **approssimato** a partire dal solo ingombro letto
dalla fotografia: una forma disegnata a mano che imita un SOT-223, un USB-C,
un coassiale. Ma quei modelli esistono gia', modellati correttamente, dentro
la libreria che KiCad installa sul disco: oltre settemila file STEP/WRL
organizzati per nome di footprint.

Usarli risolve due problemi in una volta:

* la forma e' quella vera, non un'imitazione ricavata da un rettangolo;
* il vocabolario smette di essere una lista di `kind` inventata qui dentro e
  diventa la **nomenclatura standard** dell'industria (``SOT-223``, ``0402``,
  ``USB_C_Receptacle``), che un modello linguistico conosce molto meglio.

Non e' un'API: non c'e' nessun servizio da interrogare. Sono file locali,
quindi niente rete, niente credenziali, niente costo per pezzo.

Il punto delicato: la proposta va verificata
--------------------------------------------
Chiedere al modello *quale* package sia sposta il problema, non lo elimina:
una risposta sbagliata darebbe un solido sbagliato con l'aria di essere
esatto. Per questo :func:`resolve_model` non si fida del nome: confronta
l'ingombro del modello candidato con quello **misurato sulla fotografia** e
rifiuta quando non combaciano. Una proposta controllata contro una misura
non e' un indovinello.

Quando non c'e' corrispondenza il chiamante resta libero di ripiegare sulle
forme sintetiche di ``freecad_parts``: questo modulo non decide, propone.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from functools import lru_cache
from typing import Iterable, Optional

from .model_measurement import normalize_model_measurement
from .process_log import CANCEL_POLL_INTERVAL_S, ProcessCancelled
from . import model_size_cache
from ..services.messages import tx
from ..kicad_versions import newest_version_first

#: Variabile d'ambiente per indicare una libreria diversa da quella trovata.
LIBRARY_ENV = "KICAD_3DMODEL_DIR"

#: Chi vuole sapere quando FreeCAD sta DAVVERO misurando dei modelli, per
#: thread: la finestra di avanzamento lo mostra invece di attribuire tutta la
#: fase alla libreria di KiCad (utente, 2026-09-26). Per thread perche' ogni
#: generazione gira nel proprio QThread e non deve sentire le misure altrui.
_measuring_listener = threading.local()
_archive_state = threading.local()


@contextmanager
def archive_progress_listener(callback):
    """@brief Report the separate archive pass as (done, total, records)."""
    previous = getattr(_archive_state, 'callback', None)
    _archive_state.callback = callback
    try:
        yield
    finally:
        _archive_state.callback = previous


@contextmanager
def _cached_archive_results(results):
    previous = getattr(_archive_state, 'results', None)
    _archive_state.results = results
    try:
        yield
    finally:
        _archive_state.results = previous


def _archive_prepass(components, library, note, cancel):
    roots = _search_roots(library)
    results, records = {}, []
    callback = getattr(_archive_state, 'callback', None)
    def notify():
        if callback:
            try:
                callback(len(records), len(components), list(records))
            except Exception as exc:
                note(tx("Consolidati: errore nel resoconto: {exc}", exc=exc))
    notify()
    for component in components:
        if cancel is not None and cancel.is_set():
            raise MatchingCancelled(measured={})
        if not isinstance(component, dict):
            records.append({'name': '?', 'status': 'skipped',
                            'reason': tx('Componente non idoneo al confronto con i consolidati.')})
            notify()
            continue
        selection = component.get('model_selection')
        selection = selection if isinstance(selection, dict) else {}
        record = {'name': component.get('name', '?'), 'status': 'not_found',
                  'reason': tx('Nessuna voce compatibile con categoria, misure, terminali e ritaglio.')}
        found = None
        if selection.get('status') in ('confirmed', 'rejected'):
            record.update(status='skipped', reason=tx('Scelta manuale presente: confronto non applicato.'))
        elif not archive_can_match(component):
            record.update(status='skipped', reason=tx('Componente non idoneo al confronto con i consolidati.'))
        elif not roots:
            record.update(status='unavailable', reason=tx('Libreria 3D di KiCad non trovata.'))
        else:
            diagnostic = {}
            found = _accepted_match(component, roots, note, diagnostic=diagnostic)
            record['diagnostic'] = diagnostic
            if diagnostic.get('error'):
                record.update(status='unavailable', reason=diagnostic['error'])
            elif found:
                # Stesso controllo di model_candidates: un modello d'archivio
                # fuori dal package visto dalla rete non e' «trovato», e il
                # resoconto non deve dirlo (verifica avversariale A4, 2026-10-02).
                outside_network = (_local_classifier_identity(component)
                                   and not _classifier_admits(
                                       Path(found['candidates'][0]['path']), component))
                review = (found.get('status') == 'review_required'
                          or component.get('_crop_class_status') == 'conflict'
                          or component.get('_category_status') == 'conflict'
                          or _crop_terminal_review_blocks(component))
                record.update(status='review_required' if review or outside_network else 'matched',
                              reason=tx("L'archivio propone un modello fuori dal package riconosciuto "
                                        "dalla rete locale: da verificare prima di usarlo.")
                              if outside_network else
                              tx('Corrispondenza trovata, ma osservazioni in conflitto: da verificare.')
                              if review else tx('Corrispondenza trovata nei consolidati.'),
                              model=Path(found['candidates'][0]['path']).stem,
                              conflicts=found.get('accepted_visual_conflicts', []),
                              entry_id=found['accepted_entry']['id'],
                              visual_score=found.get('accepted_visual_score', 0),
                              orientation=found.get('accepted_orientation', {}))
            elif diagnostic.get('reason'):
                record['reason'] = diagnostic['reason']
        results[id(component)] = found
        component['consolidated_match'] = record
        records.append(record)
        note(f"Consolidati — {record['name']}: {record['reason']}"
             + (f" Modello: {record['model']}." if record.get('model') else '')
             + (' ' + ' '.join(record.get('conflicts', []))))
        notify()
    return results


@contextmanager
def measuring_listener(callback):
    """@brief Tell `callback(started, count)` when FreeCAD starts/ends measuring models.

    @param callback Called with (True, n) right before a `freecadcmd` child is
        launched to measure n candidate models, and with (False, n) when it is
        over. Models already in the measurement cache launch nothing, so the
        callback is not called for them.
    @note Scoped to the calling thread and to the `with` block; a failing
        callback is ignored, because progress reporting must never stop a
        generation.
    """
    previous = getattr(_measuring_listener, "callback", None)
    _measuring_listener.callback = callback
    try:
        yield
    finally:
        _measuring_listener.callback = previous


def _announce_measuring(started: bool, count: int) -> None:
    callback = getattr(_measuring_listener, "callback", None)
    if callback is None:
        return
    try:
        callback(started, count)
    except Exception:  # noqa: BLE001 - l'avanzamento non deve fermare il lavoro
        pass

#: Estensioni dei modelli, in ordine di preferenza: lo STEP e' geometria
#: solida ed e' cio' che serve per un componente montato; il VRML e' solo
#: una mesh per la visualizzazione.
MODEL_SUFFIXES = (".step", ".stp", ".wrl")

#: Scarto relativo massimo fra l'ingombro del modello e quello misurato sulla
#: fotografia.
#:
#: EURISTICA DICHIARATA. Non e' il criterio di scelta -- fra i candidati entro
#: la soglia la scelta avviene per vicinanza -- ma il limite oltre il quale la
#: misura non conferma piu' nulla e il candidato non viene nemmeno proposto.
#:
#: CORREZIONE (audit 26/09, punto 82 medio 8): il commento precedente diceva
#: che fra due taglie standard adiacenti corre «almeno il 60%». Vale per i
#: passivi piccoli (0402 1.0x0.5 contro 0603 1.6x0.8), non in generale: QFN
#: 6x6 contro 7x7 distano il 14%, 7x7 contro 8x8 il 12.5%, e la distanza
#: relativa cala al crescere del package. Il 30% quindi NON separa le taglie
#: vicine dei package grandi: le lascia entrambe nella lista, e a separarle
#: sono la vicinanza (ordinamento), il numero di terminali dichiarato e la
#: revisione dell'utente. Tarato come limite largo per misure fotografiche a
#: bassa risoluzione; lo renderebbe superfluo una tolleranza per candidato
#: ricavata dall'incertezza di misura (`measurement_uncertainty`).
DEFAULT_SIZE_TOLERANCE = 0.30

#: Entro questo scarto due candidati sono sostenuti dalla misura allo stesso
#: modo: la fotografia non puo' distinguerli, quindi a decidere e' il nome.
NAME_TIEBREAK_MARGIN = 0.05

#: Indice degli ingombri gia' misurati, accanto alla libreria o nella cache
#: utente. Misurare un STEP richiede FreeCAD: si fa una volta per modello.
INDEX_FILENAME = ".kicadgen-model-sizes-v4.json"

# Resource budget, not a geometric threshold: never launch a library-wide CAD batch.
MAX_CANDIDATE_MEASUREMENTS = 64

_NAMED_PACKAGE_PATTERN = r"(?<![\w-])(?:SC|SOT|SOD|SOIC|SSOP|TSSOP|MSOP|TSOP|SOP|QFN|DFN|LQFP|TQFP|QFP|BGA|DIP|TO)[ _-]?\d+(?:[-_]\d+)?(?![\w])"
_CHIP_SIZE_CODES = r"(?:008004|01005|0201|0402|0603|0805|1008|1206|1210|1806|1812|2010|2512)"

#: Famiglie che hanno in libreria un catalogo SMD a sigle di taglia standard e
#: corpo rettangolare a due terminali metallizzati. Solo per queste una taglia
#: nominata piu' due endcap osservati delimitano una ricerca *completa*: il
#: catalogo copre per costruzione tutte le taglie e le varianti di suffisso.
#: I diodi ne restano fuori di proposito: condividono il catalogo a sigle
#: (D_0603_1608Metric) ma anche gli involucri MELF cilindrici, che con due
#: terminali metallizzati si vedono uguali in fotografia e non entrerebbero
#: nell'elenco -- dichiarare completa una ricerca che li esclude significa
#: poter agganciare da soli il modello sbagliato.
_ENDCAP_CHIP_CATALOG = {"resistor": ("R", "Resistor_SMD.3dshapes"),
                        "capacitor": ("C", "Capacitor_SMD.3dshapes"),
                        "led": ("LED", "LED_SMD.3dshapes")}


def _package_token(package: str) -> str:
    """Extract declared package syntax; never derive a package from dimensions.

    Bare identifiers retain their complete variant suffix. In prose, numeric
    codes require explicit package context. Metric codes keep their namespace,
    including overlapping codes such as 0603; no imperial conversion is guessed.
    Ambiguous prose is deliberately left unresolved.
    """
    text = str(package or "").strip()
    codes = _CHIP_SIZE_CODES
    # A standalone size-class label is package syntax, not a vendor identifier.
    shorthand = re.fullmatch(r"(" + codes + r")-class", text, re.I)
    if shorthand:
        return shorthand.group(1)
    # Full filenames/identifiers must not be reduced to a broader family.
    if re.fullmatch(r"[\w.+-]+", text) and not text.isdigit():
        return re.sub(r"\.(?:step|stp|wrl)$", "", text, flags=re.I)
    if re.fullmatch(codes, text):
        return text
    tokens = []
    for match in re.finditer(_NAMED_PACKAGE_PATTERN, text, re.I):
        tokens.append(re.sub(r"\s+", "-", match.group()))
    # Explicit metric notation is matched directly against KiCad's Metric token.
    metric = r"(?<!\w)(?:(\d{4})\s*[-( ]?\s*metric\b|metric\s*[-: ]?\s*(\d{4})(?!\w))"
    spans = []
    for match in re.finditer(metric, text, re.I):
        tokens.append((match.group(1) or match.group(2)) + "Metric")
        spans.append(match.span())
    for match in re.finditer(r"(?<![\w.-])" + codes + r"(?![\w.])", text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        before, after = text[:match.start()], text[match.end():]
        # A product/part number is not package evidence even in passive prose.
        if re.search(r"(?:part|product|model|id|p/n|mpn|number)\s*[:#=-]?\s*$", before, re.I):
            continue
        if (re.search(r"(?:package|case|size|imperial|inch|smd|chip|resistor|capacitor|passive)\s*[:=( -]*$", before, re.I)
                or re.match(r"\s*[- ]?\s*(?:class|package|case|imperial|inch)\b", after, re.I)):
            tokens.append(match.group())
    unique = {re.sub(r"[^a-z0-9]", "", token.lower()): token for token in tokens}
    if len(unique) == 1:
        return next(iter(unique.values()))
    # Preserve established word-order aliases, but not arbitrary prose.
    if not unique and re.fullmatch(r"(?:smd (?:crystal|resonator|oscillator)|(?:crystal|resonator|oscillator) smd)(?:[ _-][\w.-]+)*", text, re.I):
        return text
    return ""


def _package_name(package: str) -> str:
    """Normalize mechanical vocabulary, without inferring electrical identity."""
    words = re.split(r"[^a-z0-9]+", _package_token(package).lower())
    # Word-order aliases preserve any size/pin suffix supplied by the caller.
    if len(words) >= 2 and words[:2] in (["smd", "crystal"],
                                         ["smd", "resonator"],
                                         ["smd", "oscillator"]):
        words[:2] = words[1::-1]
    return _normalize("_".join(words))


#: Windows folders holding one sub-folder per installed KiCad version.
_INSTALL_BASES = (r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad")


def _candidate_roots() -> Iterable[Path]:
    """Percorsi in cui cercare la libreria, dal piu' esplicito al piu' generico."""
    override = os.environ.get(LIBRARY_ENV)
    if override:
        yield Path(override)
    for base in _INSTALL_BASES:
        root = Path(base)
        if root.is_dir():
            # Newest version first by numeric value: as text, 9.0 > 10.0.
            for version in newest_version_first(root.iterdir(), lambda p: p.name):
                yield version / "share" / "kicad" / "3dmodels"
    # KiCad mantiene le librerie 3rd-party installate dall'utente fuori da
    # Program Files. Ogni repository puo' avere una propria cartella
    # ``*.3dshapes`` (es. ``espressif.3dshapes``), quindi si espongono i
    # repository come radici indipendenti e si evita di assumere un vendor.
    user_kicad = Path.home() / "Documents" / "KiCad"
    if user_kicad.is_dir():
        for version in newest_version_first(user_kicad.iterdir(), lambda p: p.name):
            models = version / "3rdparty" / "3dmodels"
            if not models.is_dir():
                continue
            try:
                children = sorted(models.iterdir(), reverse=True)
            except OSError:
                continue
            for repository in children:
                if repository.is_dir() and any(repository.glob("*.3dshapes")):
                    yield repository
    for base in ("/usr/share/kicad/3dmodels", "/usr/local/share/kicad/3dmodels",
                 "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"):
        yield Path(base)


def _search_roots(library: Optional[Path]) -> list[Path]:
    """Return the explicit root plus installed user repositories once each."""
    if library is not None:
        return [Path(library)]
    result = []
    # Keep the established primary-library contract (and test doubles that
    # replace find_library), then add every discovered user repository.
    roots = []
    primary = find_library()
    if primary is None:
        return []
    roots.append(Path(primary))
    roots.extend(_candidate_roots())
    for index, root in enumerate(roots):
        root = Path(root)
        if root in result:
            continue
        try:
            if root.is_dir() and (index == 0 or any(root.glob("*.3dshapes"))):
                result.append(root)
        except OSError:
            continue
    return result


def find_library() -> Optional[Path]:
    """@brief Find the installed KiCad model directory, or return None.

    Assenza non e' un errore: il chiamante ripiega sulle forme sintetiche.
    """
    for root in _candidate_roots():
        try:
            if root.is_dir() and any(root.glob("*.3dshapes")):
                return root
        except OSError:
            continue
    return None


def _normalize(text: str) -> str:
    """Forma confrontabile di un nome di package.

    I nomi reali mescolano trattini, trattini bassi e punti senza coerenza
    (``SOT-223``, ``SOT_223``, ``U.FL``): normalizzarli evita di perdere una
    corrispondenza per un separatore diverso.
    """
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


#: Caratteri alfanumerici minimi di una sigla letta sul componente perche'
#: diventi una chiave di ricerca nei nomi dei file STEP.
#:
#: EURISTICA DICHIARATA: la chiave si cerca come SOTTOSTRINGA fra migliaia di
#: nomi di file. Codici di 3-4 caratteri (valori «103», «R10», lotti «A1B2»,
#: taglie «0805») compaiono per caso dentro nomi che non c'entrano; da 5
#: caratteri con lettere E cifre la coincidenza casuale diventa rara. Non
#: dipende da una marca: vale per qualunque sigla. Lo renderebbe superfluo un
#: confronto per token interi invece che per sottostringa.
MIN_MARKING_TOKEN_CHARS = 5


def _marking_model_tokens(component: dict) -> tuple[str, ...]:
    """Extract part-number-like identifiers from the literal marking.

    A token is a run of letters, digits and inner hyphens/underscores/dots
    (``ESP32-S3-WROOM-1U``, ``ESP-12F``, ``RFM95W``, ``E73-2G4M08S1C``) that
    contains BOTH letters and digits and at least MIN_MARKING_TOKEN_CHARS
    alphanumerics. The shape is structural: plain words (``WIFI``, ``MADE``)
    have no digits, values and date codes (``103``, ``2023``) have no letters.
    Tokens are only literal filename hints for discovery; identity still
    requires the exact filename (_marked_model_is_named) and compatible
    dimensions. No brand or vendor list is involved.
    """
    # Una sigla letta con affidabilita' bassa e' in gran parte indovinata: come
    # chiave di ricerca porterebbe a un file sbagliato con l'aria di un'identita'.
    if str(component.get("marking_confidence") or "").lower() == "low":
        return ()
    marking = str(component.get("marking") or "")
    matches = re.findall(r"[A-Z0-9]+(?:[-_.][A-Z0-9]+)*", marking.upper())
    unique = {}
    for value in matches:
        normalized = _normalize(value)
        if (len(normalized) >= MIN_MARKING_TOKEN_CHARS
                and re.search(r"[a-z]", normalized) and re.search(r"\d", normalized)):
            unique[normalized] = value
    return tuple(unique.values())


def _marked_model_matches(path: Path, component: dict) -> bool:
    """Whether a model filename CONTAINS a literal identifier from the marking.

    This is the DISCOVERY key: a substring is enough to put a file in the
    candidate list, because a vendor library may name a variant by appending
    a suffix to the marked code. It is not identity evidence -- see
    _marked_model_is_named for that -- and every consumer that promotes a
    candidate on the strength of the marking must use the exact form.
    """
    stem = _normalize(path.stem)
    return any(stem == _normalize(token) or _normalize(token) in stem
               for token in _marking_model_tokens(component))


def _marked_model_is_named(path: Path, component: dict) -> bool:
    """Whether the marking names the model file EXACTLY (normalized stem).

    Identity requires the whole filename stem to equal a marking token. A
    substring match (marking ``X-Y-1`` on file ``X-Y-1U.step``) is the
    signature of a different variant with the same prefix, and such variants
    can differ in geometry (an antenna-connector version versus a PCB-antenna
    version): they may be discovered and proposed provisionally, never
    promoted as the identified model. The comparison is between the observed
    marking and the filename, never against a vendor list.
    """
    stem = _normalize(path.stem)
    return any(stem == _normalize(token) for token in _marking_model_tokens(component))


def _marked_model_paths(component: dict, root: Path) -> list[Path]:
    """Find STEP files named by a literal marking inside one KiCad root."""
    tokens = _marking_model_tokens(component)
    if not tokens:
        return []
    paths = []
    try:
        for path in root.rglob("*"):
            if path.suffix.lower() in (".step", ".stp") and _marked_model_matches(path, component):
                paths.append(path)
    except OSError:
        return []
    return sorted(paths, key=lambda path: (
        min(0 if _normalize(path.stem) == _normalize(token) else 1 for token in tokens),
        len(path.stem), str(path)))


def candidates(package: str, library: Optional[Path] = None) -> list[Path]:
    """@brief Find package-compatible models, ordered by name and solid format.

    @param package Mechanical package name, including common word-order aliases.
    @param library Optional installed 3D library root.
    @return All matching paths, without an arbitrary candidate count limit.

    L'ordinamento conta: ``SOT-223`` deve trovare ``SOT-223.step`` prima di
    ``SOT-223-8.step``, che e' un altro componente.
    """
    library = Path(library) if library is not None else find_library()
    token = _package_token(package)
    wanted = _package_name(token)
    if not library or not wanted:
        return []
    exact, prefix, partial = [], [], []
    for path in library.rglob("*"):
        if path.suffix.lower() not in MODEL_SUFFIXES:
            continue
        stem = _normalize(path.stem)
        # Numeric size codes must be complete tokens, never digits embedded in
        # a screw/vendor identifier. Separators in named packages remain flexible.
        if re.fullmatch(r"\d+(?:metric)?", wanted) and wanted not in re.split(r"[^a-z0-9]+", path.stem.lower()):
            continue
        if any(not re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", path.stem)
               for number in re.findall(r"\d+", token)):
            continue
        if stem == wanted:
            exact.append(path)
        elif stem.startswith(wanted):
            prefix.append(path)
        elif wanted in stem:
            partial.append(path)

    def order(path: Path) -> tuple:
        # A parita' di attinenza vince lo STEP, poi il nome piu' corto: un
        # nome piu' lungo porta quasi sempre una variante piu' specifica.
        return (MODEL_SUFFIXES.index(path.suffix.lower()), len(path.stem), str(path))

    return (sorted(exact, key=order) + sorted(prefix, key=order)
            + sorted(partial, key=order))


def _index_path(library: Path) -> Path:
    """File dell'indice degli ingombri, scrivibile anche senza permessi di sistema."""
    beside = library / INDEX_FILENAME
    if os.access(library, os.W_OK):
        return beside
    return model_size_cache.user_cache_dirs()[0] / INDEX_FILENAME


def _index_paths(library):
    """Try actual cache writes, not just os.access, before falling back to user data.

    The user-level fallbacks follow `model_size_cache.user_cache_dirs`, so
    KICADGEN_DATA_DIR and the installed layout are honoured; the historic
    %LOCALAPPDATA%\\kicadgen index stays in the list, readable, and migrates
    on the next save.
    """
    user_caches = [folder / INDEX_FILENAME for folder in model_size_cache.user_cache_dirs()]
    return list(dict.fromkeys((_index_path(library), library / INDEX_FILENAME, *user_caches)))


def load_index(library: Optional[Path] = None) -> dict:
    """@brief Read the measured-size index, tolerating a missing or broken file."""
    library = library or find_library()
    if not library:
        return {}
    return model_size_cache.load(_index_paths(Path(library)))


def save_index(index: dict, library: Optional[Path] = None) -> None:
    """@brief Persist the measured-size index; a failure is never fatal."""
    library = library or find_library()
    if not library:
        return
    model_size_cache.save(index, _index_paths(Path(library)))


def index_key(path: Path) -> str:
    """@brief Identify a file by absolute path, byte size and nanosecond mtime."""
    try:
        stat = path.stat()
        return f"{path.absolute()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(path.absolute())


def _sexpr(text: str) -> list:
    """Parse balanced KiCad lists, keeping quoted parentheses and escapes literal."""
    tokens = re.compile(r'\s+|;[^\n]*|[()]|"(?:\\.|[^"\\])*"|[^\s();"]+')
    stack, roots, position = [], [], 0
    for match in tokens.finditer(text):
        if match.start() != position:
            raise ValueError("Invalid S-expression token")
        position = match.end()
        token = match.group()
        if token.isspace() or token.startswith(";"):
            continue
        if token == "(":
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced S-expression")
            stack.pop()
        else:
            if not stack:
                raise ValueError("Atom outside root")
            if token.startswith('"'):
                token = re.sub(r'\\(["\\])', r'\1', token[1:-1])
            stack[-1].append(token)
    if position != len(text) or stack or len(roots) != 1:
        raise ValueError("Incomplete S-expression")
    return roots[0]


def _children(node, name):
    return [item for item in node if isinstance(item, list) and item and item[0] == name]


def _field(node, name, default=()):
    found = _children(node, name)
    if len(found) > 1:
        raise ValueError(f"Duplicate {name}")
    return found[0][1:] if found else list(default)


def _numbers(values, count):
    result = [float(v) for v in values]
    if len(result) != count or not all(math.isfinite(v) for v in result):
        raise ValueError("Invalid geometry")
    return result


def _pad_bounds(pad):
    """Exact copper bounds for supported pad shapes; reject unknown geometry."""
    if len(pad) < 4:
        raise ValueError("Incomplete pad")
    layers = _field(pad, "layers")
    if not layers:
        raise ValueError("Pad without layers")
    if pad[2] == "np_thru_hole" or not any(str(v).endswith(".Cu") for v in layers):
        return None
    if pad[2] not in ("smd", "thru_hole", "connect"):
        raise ValueError("Unsupported pad type")
    at = _field(pad, "at")
    x, y, angle = _numbers(at if len(at) == 3 else [*at, 0], 3)
    # KiCad stores the hole at `at`; a drill offset moves the copper in
    # the pad's local axes (positive angle is counter-clockwise, Y down).
    drill = _field(pad, "drill")
    offset = _field(drill, "offset")
    if offset:
        ox, oy = _numbers(offset, 2)
        radians = math.radians(angle)
        cosine, sine = math.cos(radians), math.sin(radians)
        x += ox * cosine + oy * sine
        y += -ox * sine + oy * cosine
    width, height = _numbers(_field(pad, "size"), 2)
    if min(width, height) <= 0:
        raise ValueError("Nonpositive pad")
    c, s = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))
    shape = pad[3]
    if shape == "rect":
        dx, dy = (width*c + height*s)/2, (width*s + height*c)/2
    elif shape in ("circle", "oval", "roundrect"):
        if shape == "circle" and width != height:
            raise ValueError("Noncircular circle")
        ratio = (_numbers(_field(pad, "roundrect_rratio"), 1)[0]
                 if shape == "roundrect" else 0.5)
        if not 0 <= ratio <= 0.5 or _children(pad, "chamfer"):
            raise ValueError("Unsupported corner geometry")
        radius = min(width, height) * ratio
        dx = (width/2-radius)*c + (height/2-radius)*s + radius
        dy = (width/2-radius)*s + (height/2-radius)*c + radius
    else:
        # A custom/trapezoid pad's size is not its copper envelope. Reject the
        # entire footprint rather than silently measuring only the other pads.
        raise ValueError("Unsupported copper pad shape")
    return x-dx, y-dy, x+dx, y+dy


def _model_link(model, footprint: Path, library: Path):
    """Resolve explicit links only; never guess a STEP from a VRML basename."""
    if len(model) < 2 or not isinstance(model[1], str):
        raise ValueError("Invalid model link")
    if "hide" in model or _field(model, "hide", ("no",)) != ["no"]:
        return None
    # The consumer imports raw solids. Scaled/tilted instances cannot certify
    # that raw geometry; quarter-turns in the PCB plane preserve sorted sides.
    for name, default in (("scale", (1, 1, 1)), ("rotate", (0, 0, 0)),
                          ("offset", (0, 0, 0)), ("at", (0, 0, 0))):
        values = _field(model, name)
        xyz = _numbers(_field(values, "xyz") if values else default, 3)
        if name == "scale" and xyz != [1, 1, 1]:
            return None
        if name == "rotate" and (xyz[0] or xyz[1] or not math.isclose(xyz[2] % 90, 0, abs_tol=1e-9)):
            return None
    raw = model[1].replace("\\", "/")
    def expand(match):
        name = match.group(1)
        if re.fullmatch(r"KICAD(?:\d+)?_3DMODEL_DIR", name):
            return str(library)
        return os.environ.get(name, match.group(0))
    raw = re.sub(r"\$\{([^}]+)\}", expand, raw)
    if "$" in raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = footprint.parent / path
    return path.resolve() if path.suffix.lower() in (".step", ".stp") else None


@lru_cache(maxsize=32768)
def _link_hints(path: Path, stamp: str):
    """Cheap name prefilter only; the balanced parser still validates every match.

    Parsing all pad geometry in large connector libraries is unnecessary. Cache
    only link basenames, not their potentially megabyte-sized footprint text.
    Variable filenames cannot be filtered and therefore require full parsing.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
        links = re.findall(r'\(\s*model\s+("(?:\\.|[^"\\])*"|[^\s()]+)', text)
        names = [re.sub(r'\\(["\\])', r'\1', link.strip('"')).replace("\\", "/").rsplit("/", 1)[-1].casefold()
                 for link in links]
        return frozenset(names), any("$" in name for name in names)
    except (OSError, UnicodeError):
        return frozenset(), False


@lru_cache(maxsize=32768)
def _footprint_record(path: Path, stamp: str):
    """Cache parsing by full path, size and nanosecond mtime, including failures."""
    try:
        root = _sexpr(path.read_text(encoding="utf-8-sig"))
        if not root or root[0] not in ("footprint", "module"):
            raise ValueError("Not a footprint")
        models = _children(root, "model")
        # The whole land pattern cannot identify one member of an assembly.
        if len(models) != 1:
            return None, None, "multiple/no model links"
        bounds = [b for pad in _children(root, "pad") if (b := _pad_bounds(pad)) is not None]
        if not bounds:
            return None, None, "no copper pads"
        size = [max(b[2] for b in bounds)-min(b[0] for b in bounds),
                max(b[3] for b in bounds)-min(b[1] for b in bounds)]
        return size, models[0], None
    except (OSError, ValueError, TypeError, IndexError, RecursionError) as exc:
        return None, None, str(exc)


def _footprint_listing(root: Path) -> list:
    """Cheap change detector: (path, size, mtime_ns) of every *.kicad_mod under root.

    `os.scandir` returns size and mtime with the directory entries (no extra
    system call per file on Windows), so the whole KiCad library (~15k files)
    is listed in well under a tenth of the time the per-file `stat` of the
    original scan took. Directory mtimes alone are NOT enough: NTFS updates
    them at timer-tick granularity (two additions within ~15 ms keep the same
    value) and never on an in-place edit of a file.
    Symlinked directories are not descended, as `Path.rglob` does not.
    """
    listing, pending = [], [str(root)]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.name.casefold().endswith(".kicad_mod"):
                        try:
                            stat = entry.stat()
                            listing.append((entry.path, stat.st_size, stat.st_mtime_ns))
                        except OSError:
                            listing.append((entry.path, -1, -1))
        except OSError:
            # An unreadable folder is skipped by rglob too; keep it in the
            # snapshot so that it becoming readable triggers a re-index.
            listing.append((folder, -1, -1))
    listing.sort()
    return listing


class _FootprintIndex:
    """One scan of a footprint root: sorted footprints, their stamps and link basenames.

    Positions refer to `entries`, which keeps the `sorted(root.rglob(...))`
    order of the original per-call scan, so lookups visit footprints in the
    very same order and produce identical results.
    """

    __slots__ = ("listing", "entries", "by_name", "variable")

    def __init__(self, root: Path, listing: list):
        """Scan `root` once; `listing` is the detector snapshot taken BEFORE the scan."""
        self.listing = listing
        self.entries = []
        self.by_name = {}
        self.variable = []
        for position, footprint in enumerate(sorted(root.rglob("*.kicad_mod"))):
            stamp = index_key(footprint)
            hints, variable = _link_hints(footprint, stamp)
            self.entries.append((footprint, stamp))
            if variable:
                self.variable.append(position)
            for name in hints:
                self.by_name.setdefault(name, []).append(position)


#: Footprint indexes of this session, keyed by the footprint root as given.
_FOOTPRINT_INDEXES: dict = {}
_FOOTPRINT_INDEX_LOCK = threading.Lock()


def clear_footprint_index() -> None:
    """@brief Forget every cached KiCad footprint index of this session.

    @note Never needed for correctness: each lookup already re-indexes a root
        whose footprint files were added, removed, renamed or rewritten. It
        exists for tests and for callers that want to release the memory.
    """
    with _FOOTPRINT_INDEX_LOCK:
        _FOOTPRINT_INDEXES.clear()


def _footprint_index(root: Path) -> _FootprintIndex:
    """Session index of `root`, rebuilt whenever any footprint's size/mtime changed.

    @throws OSError when the library cannot be listed, like the original rglob.
    """
    listing = _footprint_listing(root)
    key = str(root)
    with _FOOTPRINT_INDEX_LOCK:
        index = _FOOTPRINT_INDEXES.get(key)
        if index is not None and index.listing == listing:
            return index
    index = _FootprintIndex(root, listing)
    with _FOOTPRINT_INDEX_LOCK:
        _FOOTPRINT_INDEXES[key] = index
    return index


def _linked_footprints(root: Path, names) -> list:
    """(footprint, stamp) whose link hints mention one of `names`, or are variable.

    Same selection and order as filtering `sorted(root.rglob("*.kicad_mod"))`
    with `_link_hints`, but through the session index. Each candidate is
    re-stamped here, so the stamp handed to the parsing caches is exactly the
    one the per-call scan would have computed.
    @throws OSError when the library cannot be listed.
    """
    index = _footprint_index(root)
    positions = set(index.variable)
    for name in names:
        positions.update(index.by_name.get(name, ()))
    selected = []
    for position in sorted(positions):
        footprint, indexed = index.entries[position]
        stamp = index_key(footprint)
        if stamp != indexed:
            hints, variable = _link_hints(footprint, stamp)
            if not variable and not set(names).intersection(hints):
                continue
        selected.append((footprint, stamp))
    return selected


def footprint_sizes(paths, library: Optional[Path] = None, log=None) -> dict:
    """@brief Read copper land-pattern envelopes linked to candidate solids.

    @param paths Candidate STEP paths (only explicit model links are accepted).
    @param library Model root; footprints are read from its sibling directory.
    @param log Optional diagnostic callback.
    @return Mapping of model paths to separate size/source footprint readings.
    @note Unsupported pad geometry or model transforms provide no evidence.
    """
    library = library or find_library()
    paths = list(paths)
    result = {str(p): [] for p in paths}
    if library is None:
        return result
    root = library.parent / "footprints"
    if not root.is_dir():
        if log:
            log(tx("Footprint KiCad non disponibili: confronto solo corpo/reofori."))
        return result
    wanted = {Path(p).resolve(): str(p) for p in paths}
    names = {p.name.casefold() for p in wanted}
    rejected = 0
    try:
        for footprint, stamp in _linked_footprints(root, names):
            size, model, error = _footprint_record(footprint, stamp)
            if error:
                rejected += 1
                continue
            try:
                linked = _model_link(model, footprint, library)
            except (ValueError, TypeError, IndexError, OSError):
                linked = None
            if linked is None:
                rejected += 1
            if linked in wanted:
                result[wanted[linked]].append({"size": size, "source": str(footprint)})
    except OSError as exc:
        if log:
            log(f"Lettura footprint KiCad incompleta: {exc}")
    if log:
        log(tx("Footprint KiCad: {sum} letture collegate; {rejected} footprint "
               "pertinenti senza ingombro utilizzabile.",
               sum=sum(map(len, result.values())),
               rejected=rejected))
    return result


class MatchingCancelled(ProcessCancelled):
    """@brief Raised when the KiCad model matching was stopped on request.

    @note Sottoclasse di `process_log.ProcessCancelled` DI PROPOSITO: la fase
        di abbinamento avvia i suoi figli `freecadcmd` (le misure dei
        candidati non ancora in cache) e chi la chiama deve poter scrivere UNA
        sola clausola -- `except ProcessCancelled` -- per entrambe le meta'
        del 3D, abbinamento e costruzione del solido. Una seconda gerarchia
        avrebbe voluto dire due convenzioni per la stessa cosa.
    @note Distinta da un lotto FALLITO, che resta `{}`: una misura che non
        riesce e' una diagnosi sul file (modello illeggibile, FreeCAD
        assente), una rinuncia e' una decisione di chi guarda. Confonderle
        farebbe registrare «nessun candidato» per componenti che nessuno ha
        mai misurato, cioe' un verdetto inventato.
    """

    def __init__(self, command=None, output="", measured=None):
        """@brief Carry the killed command and the measurements already paid for.

        @param command Command line of the child that was killed, or None when
            the stop happened between two components, senza nessun figlio vivo.
        @param output Text the child had already written, kept for the log.
        @param measured Mapping path -> measurement completed BEFORE the stop.
            Sono misure valide: il chiamante le mette in cache invece di
            buttarle, perche' sono gia' state pagate in tempo di CPU.
        """
        super().__init__(command, output=output)
        self.measured = dict(measured or {})


#: Script eseguito dentro FreeCAD: misura l'ingombro di piu' modelli in una
#: sola invocazione. Aprire un processo per file costerebbe secondi a pezzo.
#: I percorsi passano da un file di scambio e non da argv, perche' freecadcmd
#: non inoltra gli argomenti allo script come farebbe python.
#:
#: Protocollo del file di scambio (lo stesso che le prove sostituiscono con un
#: finto misuratore, visto che FreeCAD non c'e' nei test):
#:   in.json        -> elenco dei percorsi da misurare;
#:   progress.jsonl -> una riga {"path": ..., "size": ...} per OGNI misura
#:                     finita, scritta e scaricata subito;
#:   out.json       -> l'intera mappa, scritta solo se il lotto arriva in fondo.
_MEASURE_SCRIPT = """
import json, os
import FreeCAD
import Part
folder = os.environ["KICADGEN_MEASURE_DIR"]
with open(os.path.join(folder, "in.json"), encoding="utf-8") as handle:
    paths = json.load(handle)


def measure(path):
    shape = Part.read(path)
    box = shape.BoundBox
    total = [box.XLength, box.YLength, box.ZLength]
    # Ingombro del solo corpo: sezione presa in alto, non a meta'. I reofori
    # stanno bassi e a meta' altezza rientrano ancora nella sezione; sopra lo
    # standoff resta il solo corpo. Verificato: al 65% un SOT-223 da' 3.47 x
    # 6.47 (corpo reale 3.5 x 6.5) e un SOIC-8 da' 3.75 x 4.75 (3.9 x 4.9),
    # mentre al 50% davano rispettivamente 5.25 e 5.02 sul lato corto.
    #
    # Serve perche' un rettangolo misurato sulla foto puo' aver seguito il
    # corpo oppure l'estensione totale coi reofori aperti: sono due letture
    # entrambe legittime dello stesso pezzo, e confrontarne una sola faceva
    # rifiutare package giusti.
    body = None
    try:
        middle = box.ZMin + box.ZLength * 0.65
        slab = Part.makeBox(box.XLength + 2, box.YLength + 2, max(box.ZLength * 0.02, 1e-3),
                            FreeCAD.Vector(box.XMin - 1, box.YMin - 1, middle))
        section = shape.common(slab)
        if section.Volume > 0:
            inner = section.BoundBox
            body = [inner.XLength, inner.YLength, box.ZLength]
    except Exception:
        body = None
    return {"total": total, "body": body}


sizes = {}
# Ogni misura finita viene scritta SUBITO, riga per riga: se il processo
# viene ucciso su richiesta a meta' lotto, cio' che era gia' stato misurato
# sopravvive e finisce in cache invece di essere rifatto alla corsa dopo.
with open(os.path.join(folder, "progress.jsonl"), "w", encoding="utf-8") as done:
    for path in paths:
        try:
            sizes[path] = measure(path)
        except Exception:
            sizes[path] = None
        done.write(json.dumps({"path": path, "size": sizes[path]}) + "\\n")
        done.flush()
with open(os.path.join(folder, "out.json"), "w", encoding="utf-8") as handle:
    json.dump(sizes, handle)
"""


def _partial_measurements(progress_path: Path) -> dict:
    """Rilegge le misure gia' finite da `progress.jsonl`, riga per riga.

    L'ultima riga puo' essere troncata: il figlio e' stato ucciso mentre la
    scriveva. Una riga rotta si scarta, le precedenti restano valide.
    """
    measured = {}
    try:
        text = progress_path.read_text(encoding="utf-8")
    except OSError:
        return measured
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("path"), str):
            measured[record["path"]] = record.get("size")
    return measured


def measure_models(paths, timeout: int = 300, *, cancel=None) -> dict:
    """@brief Measure model body and total X/Y/Z dimensions in mm using FreeCAD.

    Una sola invocazione per lotto: aprire un processo per file costerebbe
    secondi a pezzo. I modelli illeggibili finiscono a ``None`` invece di far
    fallire il lotto -- un file rotto nella libreria non deve impedire di
    risolvere gli altri.

    Solo STEP: il VRML e' una mesh per la visualizzazione e FreeCAD non lo
    importa come solido.

    @param paths Percorsi dei modelli da misurare.
    @param timeout Secondi di SILENZIO concessi a un singolo modello: il conto
        riparte a ogni misura conclusa (una riga in `progress.jsonl`), quindi
        non e' un budget per l'intero lotto.
    @param cancel Canale di rinuncia: un oggetto con `is_set()` (tipicamente
        `threading.Event`) che il chiamante mette a uno per chiedere la fine
        del lavoro. **None significa non interrompibile**, ed e' il valore
        predefinito.
    @return Mappa percorso -> misura. Contiene anche le misure concluse prima
        di un lotto fallito o di un modello fermo; un modello fermo oltre
        @p timeout vale ``None`` (non misurabile in questa corsa) e il lotto
        riparte dai modelli che lo seguono. `{}` quando nulla e' stato misurato
        (FreeCAD assente, lancio fallito).
    @throws MatchingCancelled Quando @p cancel viene messo a uno: il figlio
        `freecadcmd` viene UCCISO -- non abbandonato -- con la stessa logica di
        `process_log.run_logged`, e le misure gia' finite viaggiano
        nell'eccezione (`measured`) perche' chi chiama possa metterle in cache.
    @note Prima un budget unico di 300 s copriva fino a 64 file e, scaduto,
        restituiva `{}`: nulla finiva in cache e ogni corsa successiva si
        fermava di nuovo allo stesso punto. Ora il silenzio si misura per
        modello e il lavoro gia' fatto sopravvive sempre.
    @note Il canale si guarda ogni `process_log.CANCEL_POLL_INTERVAL_S`: e'
        la stessa promessa di reattivita' fatta alla costruzione del solido.
    """
    readable = [str(p) for p in paths if Path(p).suffix.lower() in (".step", ".stp")]
    if not readable:
        return {}
    if cancel is not None and cancel.is_set():
        # Rinuncia gia' chiesta: non si avvia un figlio che andrebbe ucciso al
        # primo respiro (stessa regola di `_run_freecad`).
        raise MatchingCancelled(measured={})
    try:
        from .step_generator import find_freecadcmd
        freecad = find_freecadcmd()
    except Exception:  # noqa: BLE001 - senza FreeCAD si resta senza misure
        return {}
    measured = {}
    remaining = list(readable)
    _announce_measuring(True, len(readable))
    try:
        return _measure_remaining(freecad, remaining, measured, timeout, cancel)
    finally:
        _announce_measuring(False, len(readable))


def _measure_remaining(freecad, remaining, measured, timeout, cancel) -> dict:
    """Measure `remaining` in batches, skipping a model that stalls (see `measure_models`)."""
    while remaining:
        outcome, batch, stopped = _measure_batch(freecad, remaining, timeout, cancel)
        measured.update(batch)
        if stopped is not None:
            stopped.measured = dict(measured)
            raise stopped
        if outcome != "stalled":
            return measured
        # Il modello in corso quando il silenzio e' scaduto e' il primo non
        # ancora concluso: lo si dichiara non misurabile in QUESTA corsa e si
        # riparte dai successivi, che non hanno colpa. Ogni giro toglie almeno
        # un file, quindi il ciclo termina.
        pending = [path for path in remaining if path not in batch]
        if not pending:
            return measured
        measured[pending[0]] = None
        remaining = pending[1:]
    return measured


def _measure_batch(freecad, paths, timeout, cancel):
    """@brief Run one measuring child with a per-model silence watchdog.

    @param freecad freecadcmd executable.
    @param paths Models for this child, in order.
    @param timeout Seconds without a completed measurement before the child
        is considered stuck.
    @param cancel Optional cancellation channel (see `measure_models`).
    @return (outcome, measured, stopped): outcome is "done", "stalled" or
        "failed"; measured holds every measurement already written to
        `progress.jsonl` (the whole `out.json` when done); stopped is a
        MatchingCancelled to raise, or None.
    """
    import subprocess
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        progress_path = base / "progress.jsonl"
        (base / "in.json").write_text(json.dumps(paths), encoding="utf-8")
        script = base / "measure.py"
        script.write_text(_MEASURE_SCRIPT, encoding="utf-8")
        # PYTHONDONTWRITEBYTECODE: il figlio non deve lasciare __pycache__
        # accanto ai moduli, che nell'installazione stanno in sola lettura.
        environment = dict(os.environ, KICADGEN_MEASURE_DIR=str(base),
                           PYTHONDONTWRITEBYTECODE="1")
        command = [freecad, str(script)]
        # L'uscita finisce su file e non su una pipe perche' nessuno la legge:
        # una pipe piena bloccherebbe il figlio.
        log_path = base / "freecad.out"
        outcome, stopped = "failed", None
        try:
            with log_path.open("wb") as sink, subprocess.Popen(
                    command, stdout=sink, stderr=subprocess.STDOUT,
                    env=environment) as process:

                def kill_child():
                    """Unico punto in cui il figlio della misura viene ucciso."""
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait()

                def progress_size():
                    try:
                        return progress_path.stat().st_size
                    except OSError:
                        return 0

                seen = progress_size()
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        process.wait(timeout=CANCEL_POLL_INTERVAL_S)
                        outcome = "done"
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel is not None and cancel.is_set():
                        kill_child()
                        # Letture difensive: un errore nel raccogliere il
                        # bottino non deve trasformare una rinuncia in un
                        # lotto «fallito», che e' l'altro verdetto.
                        try:
                            tail = log_path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            tail = ""
                        stopped = MatchingCancelled(command, output=tail)
                        break
                    size = progress_size()
                    if size != seen:
                        # Una misura conclusa: il figlio e' vivo, il conto
                        # del silenzio riparte per il modello successivo.
                        seen, deadline = size, time.monotonic() + timeout
                    elif time.monotonic() >= deadline:
                        kill_child()
                        outcome = "stalled"
                        break
        except (OSError, ValueError, subprocess.SubprocessError):
            return "failed", _partial_measurements(progress_path), None
        partial = _partial_measurements(progress_path)
        if outcome == "done":
            try:
                complete = json.loads((base / "out.json").read_text(encoding="utf-8"))
                if isinstance(complete, dict):
                    return "done", dict(partial, **complete), None
            except (OSError, ValueError):
                pass
            # Uscito senza out.json (FreeCAD fallito a meta'): resta cio' che
            # e' stato scritto riga per riga.
            return "failed", partial, None
        return outcome, partial, stopped


def model_sizes(paths, library: Optional[Path] = None, *, cancel=None) -> dict:
    """@brief Retrieve valid cached dimensions and retry missing/failed measurements.

    @param paths Modelli di cui servono le quote.
    @param library Radice della libreria, per la cache delle misure.
    @param cancel Canale di rinuncia inoltrato a `measure_models`. **None
        significa non interrompibile** ed e' il valore predefinito: senza
        canale il comportamento e' identico a prima, `measure_models`
        compreso, che non lo vede nemmeno arrivare.
    @return Mappa percorso -> misura (anche `None` per chi non si e' potuto misurare).
    @throws MatchingCancelled Quando la misura viene fermata su richiesta. Le
        misure gia' concluse prima della rinuncia vengono comunque SALVATE in
        cache: sono valide e sono gia' state pagate, e buttarle farebbe
        ricominciare da zero la corsa successiva. Quello che non viene
        restituito e' il risultato parziale, che il chiamante scambierebbe
        per completo.
    """
    paths = [Path(p) for p in paths]
    library = library or find_library()
    index = load_index(library)
    # Migrate the interpretation of legacy raw section measurements on every
    # read, without trusting a cache produced by the old body-section contract.
    for path in paths:
        key = index_key(path)
        if key in index:
            index[key] = normalize_model_measurement(path, index[key])
    missing = [p for p in paths if size_distance(index.get(index_key(p)), 1, 1) is None]
    if missing:
        stopped = None
        try:
            # Il canale si passa SOLO quando c'e': la firma che vedono i
            # chiamanti non interattivi (e le loro sostituzioni nei test)
            # resta quella di prima. Stessa regola di `_run_freecad`.
            measured = measure_models(missing, **({"cancel": cancel} if cancel is not None else {}))
        except MatchingCancelled as exc:
            measured, stopped = exc.measured, exc
        for path in missing:
            value = measured.get(str(path))
            # A missing FreeCAD installation or failed batch must be retryable.
            if size_distance(value, 1, 1) is not None:
                index[index_key(path)] = normalize_model_measurement(path, value)
        save_index(index, library)
        if stopped is not None:
            raise stopped
    return {str(p): index.get(index_key(p)) for p in paths}


def _readings(size):
    """Keep each source separate: never synthesize an envelope from two variants."""
    if isinstance(size, dict):
        for key in ("body", "total"):
            yield key, size.get(key)
        lands = size.get("land_patterns", [])
        for reading in lands if isinstance(lands, (list, tuple)) else []:
            if isinstance(reading, dict):
                yield "piazzole " + str(reading.get("source", "")), reading.get("size")
    else:
        yield "total", size


def declared_height(component):
    """@brief Altezza del componente, solo quando e' una stima dichiarata.

    @param component Componente come esce dall'inventario fotografico.
    @return Altezza in mm, oppure None se non e' stata osservata.
    @note Quando il modello non sa dire l'altezza, l'inventario scrive 1.0 con
        provenienza `estimated_placeholder_not_measured`: e' un segnaposto per
        far esistere un solido, non una misura, e usarlo per scegliere un
        modello significherebbe scegliere sul nulla.
    """
    source = str(component.get("_height_source") or component.get("height_source") or "")
    if not source or source in ("unknown", "estimated_placeholder_not_measured"):
        return None
    height = component.get("height_mm")
    if isinstance(height, bool) or not isinstance(height, (int, float)):
        return None
    height = float(height)
    return height if math.isfinite(height) and height > 0 else None


def height_distance(dimensions, height_mm) -> Optional[float]:
    """@brief Scarto relativo fra l'altezza di un solido e quella osservata.

    @param dimensions Quote del candidato; la terza, se c'e', e' l'altezza.
    @param height_mm Altezza osservata, o None.
    @return Scarto relativo, oppure None quando il confronto non si puo' fare.
    @note Serve solo a ordinare i candidati che la misura XY non distingue: un
        corpo alto 1,3 mm e uno alto 3,2 mm possono avere lo stesso ingombro
        visto dall'alto, e la fotografia dall'alto non li separa. Non esclude
        nessuno, perche' l'altezza qui e' una stima visiva e non una quota.
    """
    if height_mm is None or len(dimensions) < 3:
        return None
    model = dimensions[2]
    if isinstance(model, bool) or not isinstance(model, (int, float)):
        return None
    model = float(model)
    if not math.isfinite(model) or model <= 0:
        return None
    return abs(model - height_mm) / height_mm


def size_distance(model_size, length_mm, width_mm) -> Optional[float]:
    """@brief Return the smallest worst-axis relative error across valid readings.

    @param model_size Side pair or body/total/land_patterns measurement mapping.
    @param length_mm Photographic long or short side in millimetres.
    @param width_mm Other photographic side in millimetres.
    @return Relative error, or None when no valid measurement is available.
    @note Footprint variants are compared independently; sides are never mixed.

    The photograph may follow the body, leads or solder lands. Compare each
    available reading independently, long side to long side, so a quarter-turn
    between the model and photograph does not change the result.
    """
    if isinstance(model_size, dict):
        readings = [v for _, v in _readings(model_size) if isinstance(v, (list, tuple))]
        distances = [d for d in (size_distance(r, length_mm, width_mm) for r in readings)
                     if d is not None]
        return min(distances) if distances else None
    if not isinstance(model_size, (list, tuple)) or len(model_size) < 2:
        return None
    try:
        if any(isinstance(v, bool) for v in (*model_size[:2], length_mm, width_mm)):
            return None
        values = [float(v) for v in (model_size[0], model_size[1], length_mm, width_mm)]
    except (TypeError, ValueError):
        return None
    # Una dimensione nulla o negativa non e' una misura imprecisa, e' un dato
    # corrotto: prenderne il valore assoluto lo accetterebbe in silenzio.
    if not all(math.isfinite(v) and v > 0 for v in values):
        return None
    model_long, model_short = sorted(values[:2], reverse=True)
    measured_long, measured_short = sorted(values[2:], reverse=True)
    return max(abs(model_long - measured_long) / measured_long,
               abs(model_short - measured_short) / measured_short)


def size_matches(model_size, length_mm, width_mm,
                 tolerance: float = DEFAULT_SIZE_TOLERANCE) -> bool:
    """@brief Tell whether a model size is compatible with the measured one."""
    distance = size_distance(model_size, length_mm, width_mm)
    return distance is not None and distance <= tolerance


#: Tipo strutturato del corpo -> famiglia di modelli KiCad (vedi
#: _candidate_family). Unica tabella per il filtro di compatibilita' e per la
#: scoperta per famiglia: i tipi aggiunti al vocabolario dell'estrazione
#: (punto 84 medio 13) vi entrano qui una volta sola. Un tipo senza famiglia
#: (display, rele', batteria...) non restringe la ricerca per famiglia.
KIND_FAMILY_ALIASES = {
    "ic_soic": "ic", "ic_qfn": "ic", "ic_qfp": "ic", "ic_bga": "ic",
    "ic_sot": "semiconductor", "power_tab": "semiconductor",
    "tantalum": "capacitor", "capacitor_tantalum": "capacitor",
    "capacitor_electrolytic": "capacitor", "inductor_power": "inductor",
    "diode_melf": "diode", "crystal_can": "crystal",
    "pin_header": "connector", "socket_header": "connector",
    "rf_module": "module", "pcb_module": "module", "shield_can": "module",
    "semiconductor_unknown": "semiconductor", "transistor": "semiconductor",
}

#: Kinds treated as a board module by the library search: "module" itself and
#: every kind that KIND_FAMILY_ALIASES maps onto it. A shield can is the
#: visible body of a shielded module (radio, MCU module), so it follows the
#: same envelope checks, marking lookup and acceptance rules as a module;
#: deriving the set from the aliases keeps a new module kind from being
#: forgotten in one of the lists below.
MODULE_KINDS = ("module",) + tuple(sorted(
    kind for kind, family in KIND_FAMILY_ALIASES.items() if family == "module"))


def _candidate_family(path):
    """Recognize explicit KiCad taxonomy; never assign unknown packages by size."""
    folder = path.parent.name.lower().removesuffix(".3dshapes")
    if re.match(r"(?:mounting|screw|nut|washer)(?:[_-]|$)", path.stem, re.IGNORECASE):
        return "mechanical"
    if re.match(r"(?:fuse|F)(?:[_-]|$)", path.stem, re.IGNORECASE):
        return "fuse"
    if folder.startswith("resistor") and ("array" in folder or "network" in folder):
        return "resistor_array"
    if re.search(r"(?:array|network)", path.stem, re.IGNORECASE):
        if folder.startswith("resistor") or path.stem.upper().startswith("R_"):
            return "resistor_array"
    for prefix, family in (("fuse", "fuse"), ("mounting", "mechanical"),
                           ("screw", "mechanical"), ("nut", "mechanical")):
        if folder.startswith(prefix):
            return family
    for prefix, family in (("resistor_", "resistor"), ("capacitor_", "capacitor"),
                           ("diode_", "diode"), ("led_", "led"),
                           ("inductor_", "inductor"), ("connector_", "connector")):
        if folder.startswith(prefix):
            return family
    if folder == "crystal":
        return "crystal"
    if folder.startswith("button_switch"):
        return "switch"
    if folder == "rf_module":
        return "module"
    if folder == "package_to_sot_smd":
        return "semiconductor"
    if folder in ("package_so", "package_son", "package_dfn_qfn", "package_qfp",
                  "package_bga", "package_dip"):
        return "ic"
    # Standard package prefixes are useful for custom library folder names too.
    stem = path.stem.upper()
    for pattern, family in ((r"R_\d", "resistor"), (r"C_\d", "capacitor"),
                            (r"(?:SOD|DO)[-_]?\d", "diode"),
                            (r"(?:SOIC|SSOP|TSSOP|MSOP|TSOP|QFN|DFN|[LT]?QFP|BGA)[-_]", "ic")):
        if re.match(pattern, stem):
            return family
    return None


def _candidate_family_for_component(path: Path, component: dict) -> Optional[str]:
    """Add only marking-backed vendor models to the declared component family."""
    family = _candidate_family(path)
    if family is not None:
        return family
    kind = str(component.get("kind") or component.get("detected_kind") or
                component.get("_detected_kind") or "").lower()
    if kind in MODULE_KINDS and _marked_model_matches(path, component):
        return "module"
    return None


def _crop_terminal_review_blocks(component):
    """A disputed automatic count stays disputed; a supported manual count wins."""
    count = component.get('pin_count')
    manual = (component.get('pin_count_source') == 'manual'
              and component.get('pin_count_confidence') in ('medium', 'high')
              and type(count) is int and 0 < count <= 128)
    return component.get('_crop_terminal_review_required') is True and not manual


def _observed_count(component):
    """Use confident explicit observations only, never a count guessed from kind."""
    counts = set()
    if _crop_terminal_review_blocks(component):
        return counts
    confidence = component.get("pin_count_confidence")
    if confidence is None and component.get("pin_count_source") in ("photo_observation", "observed", "manual"):
        confidence = component.get("confidence")
    count = component.get("pin_count")
    # Older global inventories copied visible metal surfaces (including tabs)
    # into pin_count. That is not the total ordinary lead count used by package
    # names. Explicit manual/full-lead observations use a separate provenance.
    if (component.get("terminal_count_basis") == "visible_including_tabs"
            and component.get("pin_count_source") not in ("manual", "complete_lead_observation")):
        count = None
    if confidence in ("high", "medium") and type(count) is int and 0 < count <= 128:
        counts.add(count)
    if component.get('_crop_terminal_review_required') is True:
        return counts  # only the explicitly supported manual count passed the gate
    geometry = component.get("terminal_geometry")
    if (isinstance(geometry, dict) and geometry.get("confidence") in ("high", "medium")
            and geometry.get("resolved") is not False):
        terminals = geometry.get("terminals")
        if isinstance(terminals, list) and 3 <= len(terminals) <= 128:
            boxes = [t.get("bbox") if isinstance(t, dict) else None for t in terminals]
            if all(isinstance(b, (list, tuple)) and len(b) == 4
                   and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in b)
                   and b[0] < b[2] and b[1] < b[3] for b in boxes):
                if not any(min(a[2], b[2]) > max(a[0], b[0])
                           and min(a[3], b[3]) > max(a[1], b[1])
                           for i, a in enumerate(boxes) for b in boxes[i+1:]):
                    counts.add(len(boxes))
    return counts


def _declared_count(path):
    """Only count-bearing KiCad package syntax: SOT-23 does NOT mean 23 pins.

    DECLARED CONVENTION: this reads the KiCad library naming convention
    (KLC), where a package token such as SOIC-8 or QFN-32 carries the terminal
    count. It is a property of the library's file names, not of a component or
    brand; a custom model outside that convention simply declares no count.
    """
    # SOT's first number identifies the package; only a separate variant suffix
    # declares terminals. Bare SOT packages deliberately remain uncounted.
    sot = re.match(r"(?:T?SOT|SC)[-_]?\d+[-_]([1-9]\d*)(?=[_]|$)", path.stem, re.IGNORECASE)
    if sot:
        return int(sot.group(1))
    # Vendor-prefixed filenames can carry an explicit count-bearing package
    # token. Require token boundaries, and never apply this embedded search to
    # SOT/TO model codes (e.g. NXP_SOT1982-1 is not a one-terminal package).
    # An EP suffix is deliberately not added to the lead count: photographic
    # tabs and KiCad electrical/exposed pads do not share a universal convention.
    match = re.search(r"(?<![a-z0-9])(?:SOIC|SSOP|[A-Z]{0,2}TSSOP|MSOP|TSOP|SOP|[A-Z]{0,4}(?:QFN|DFN|QFP|BGA|SON)|SIP|DIP)[-_](\d+)(?=[_-]|$)",
                      path.stem, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _provisional_terminal_count(component):
    """Retain observed morphology for review filtering, not automatic attachment.

    Presence and a described layout support using an explicitly observed count
    as a provisional constraint even when electrical class/overall confidence is
    low. Do not invent hidden terminals, infer a count from prose, or promote this
    weaker evidence into the confident _observed_count contract.
    """
    if component.get('_crop_terminal_review_required') is True:
        return None
    if component.get("terminal_count_basis") == "visible_including_tabs":
        return None
    count = component.get("terminal_count")
    layout = component.get("terminal_layout")
    if (component.get("presence_confidence") == "high"
            and type(count) is int and 0 < count <= 512
            and isinstance(layout, str) and layout.strip()
            and layout.strip().lower() not in ("unknown", "unresolved", "?")):
        return count
    return None


def observed_terminal_count(component) -> Optional[int]:
    """@brief Observed terminal count, when a single supported value exists.

    @param component Component as produced by the inventory.
    @return The count when the confident observations (`_observed_count`) and
        the provisional one (`_provisional_terminal_count`) agree on one value;
        None when they are missing or disagree.
    @note Part of the accepted-matches signature: two disagreeing values are a
        doubt, not a signature.
    """
    if not isinstance(component, dict):
        return None
    counts = set(_observed_count(component))
    provisional = _provisional_terminal_count(component)
    if provisional is not None:
        counts.add(provisional)
    return next(iter(counts)) if len(counts) == 1 else None


def _compatible_candidates(paths, component, note):
    """Reject explicit type/topology contradictions before comparing dimensions."""
    if not component:
        return paths
    aliases = KIND_FAMILY_ALIASES
    known = {"resistor", "resistor_array", "capacitor", "diode", "led", "inductor",
             "connector", "crystal", "ic", "fuse", "mechanical", "switch", "module", "semiconductor"}
    kinds = {str(component.get(key) or "").lower() for key in
             ("kind", "detected_kind", "_detected_kind")} - {"", "generic", "unknown"}
    verified = component.get("type_verified") is True or component.get("_type_verified") is True
    public_kind = str(component.get("kind") or "").lower()
    # A verified public vote supersedes stale provisional metadata. Without a
    # verified vote, conflicting provisional claims remain grounds for fallback.
    if verified and public_kind not in ("", "generic", "unknown"):
        kinds = {public_kind}
    families = {aliases.get(kind, kind) for kind in kinds} & known
    # The CAD renderer uses 'ic' for an unidentified semiconductor. Its
    # electronic class can still be a diode; a rendering alias is not a veto.
    unknown_semiconductor = (component.get("class") == "semiconductor_unknown"
                             and kinds <= {"ic", "semiconductor_unknown"})
    counts = _observed_count(component)
    provisional = _provisional_terminal_count(component)
    if provisional is not None:
        counts.add(provisional)
    accepted = []
    for path in paths:
        family = _candidate_family(path)
        reason = None
        if len(families) > 1:
            reason = "tipi dichiarati/osservati in conflitto"
        elif family and families and family not in families and not (
                family == "semiconductor" and families <= {"ic", "diode"}) and not (
                unknown_semiconductor and family in ("ic", "semiconductor", "diode")):
            reason = tx("tipo {join} incompatibile con famiglia {family}",
                        join=','.join(sorted(kinds)),
                        family=family)
        stem = path.stem.upper()
        # DECLARED CONVENTION: KiCad library folder and package-token names
        # (KLC), used as the model's declared topology; not a component name.
        is_so = path.parent.name.lower() == "package_so.3dshapes" or bool(
            re.match(r"(?:SOIC|SSOP|TSSOP|MSOP|TSOP|SOP)[-_]", stem))
        is_qfn = bool(re.match(r"(?:QFN|DFN)[-_]", stem))
        if ("ic_qfn" in kinds and is_so) or ("ic_soic" in kinds and is_qfn):
            reason = tx("topologia QFN/DFN e SO incompatibile con il tipo osservato")
        declared = _declared_count(path)
        if len(counts) > 1:
            reason = "conteggi terminali osservati in conflitto"
        elif counts and declared is not None and declared not in counts:
            reason = tx("terminali osservati {next}, package dichiara {declared}",
                        next=next(iter(counts)),
                        declared=declared)
        if reason:
            evidence = tx("tipo verificato") if verified else tx("tipo provvisorio/non verificato")
            if provisional is not None:
                evidence += tx("; disposizione/numero dei terminali osservati ancora provvisori")
            note(tx("{get}: rifiutato {name}: {reason} ({evidence}); il solo ingombro non "
                    "conferma il package.",
                    get=component.get('package', ''),
                    name=path.name,
                    reason=reason,
                    evidence=evidence))
        else:
            accepted.append(path)
    return accepted


def _module_total_fits(path, size, component, length_mm, width_mm, tolerance, note):
    """Veto oversized board modules: shield sections and lands can omit PCB ends.

    Use the existing relative tolerance on sorted planar axes. This is a
    necessary containment check, not proof of a module's identity or variant.
    Ordinary ICs retain their body/lead alternatives.
    """
    component = component or {}
    # DECLARED CONVENTION: the KiCad library keeps board modules in the
    # RF_Module.3dshapes folder; the component side uses declared kinds.
    is_module = path.parent.name.lower() == "rf_module.3dshapes" or any(
        str(component.get(key) or "").lower() in MODULE_KINDS
        for key in ("kind", "detected_kind", "_detected_kind"))
    if not is_module:
        return True
    total = size.get("total") if isinstance(size, dict) else size
    if size_distance(total, length_mm, width_mm) is None:
        note(tx("{name}: modulo rifiutato, ingombro totale non disponibile; "
                "schermatura/piazzole non verificano il PCB completo.",
                name=path.name))
        return False
    model_axes = sorted(map(float, total[:2]))
    observed_axes = sorted((float(length_mm), float(width_mm)))
    excess = max((m - observed) / observed for m, observed in zip(model_axes, observed_axes))
    if excess > tolerance:
        note(tx("{name}: modulo rifiutato, ingombro totale {value:.2f} x {value2:.2f} "
                "mm supera il rettangolo osservato del {value3:.1f}% (limite "
                "{value4:.0f}%); schermatura/piazzole non possono nascondere "
                "estensioni del PCB.",
                name=path.name,
                value=float(total[0]),
                value2=float(total[1]),
                value3=excess * 100,
                value4=tolerance * 100))
        return False
    return True


def resolve_model(package: str, length_mm: float, width_mm: float, *,
                  library: Optional[Path] = None,
                  tolerance: float = DEFAULT_SIZE_TOLERANCE,
                  log=None, _land_patterns=None, _component=None) -> Optional[Path]:
    """@brief Resolve a solid against body, lead and linked land-pattern readings.

    @param package Proposed mechanical package name.
    @param length_mm Photographic rectangle length in millimetres.
    @param width_mm Photographic rectangle width in millimetres.
    @param library Optional installed model root.
    @param tolerance Maximum relative error, unchanged for footprint readings.
    @param log Optional callback for evidence and fallback diagnostics.
    @param _land_patterns Internal per-attachment snapshot to avoid library rescans.
    @param _component Optional component evidence used to veto incompatible candidates.
    @return Legacy dimensional proposal path or None for synthetic fallback.
    @note This low-level API does not certify identity. New callers must use
        model_candidates and explicit model_selection before attaching a solid.

    Non si fida del nome proposto: un package sbagliato darebbe un solido
    sbagliato con l'aria di essere esatto, che e' l'errore peggiore. Fra i
    candidati vince **il piu' vicino alla misura**, non il primo che rientra
    nella tolleranza: e' la vicinanza a discriminare fra due taglie dello
    stesso package, mentre la tolleranza serve solo a fermare gli assurdi.

    Ritorna None quando la libreria non c'e', il nome non trova nulla o
    nessun candidato e' abbastanza vicino: il chiamante ripiega sulla forma
    sintetica, che e' approssimata ma coerente con la misura. Rifiutare e'
    l'esito sicuro -- meglio una forma dichiaratamente approssimata di un
    modello reale ma sbagliato.
    """
    note = log or (lambda message: None)
    found = candidates(package, library)
    if not found:
        note(tx("{package}: nessun modello nella libreria KiCad.", package=package))
        return None
    # All solid candidates participate. WRL files are display meshes and must
    # neither consume a candidate budget nor be selected for Part.read.
    shortlist = [p for p in found if p.suffix.lower() in (".step", ".stp")]
    shortlist = _compatible_candidates(shortlist, _component, note)
    if not shortlist:
        note(tx("{package}: nessun solido compatibile con le evidenze di "
                "tipo/terminali; si usa la forma approssimata.",
                package=package))
        return None
    sizes = model_sizes(shortlist, library)
    lands = (_land_patterns if _land_patterns is not None
             else footprint_sizes(shortlist, library, log=note))
    sizes = dict(sizes)
    for path in shortlist:
        value = sizes.get(str(path))
        # A land pattern alone cannot demonstrate a readable solid.
        if size_distance(value, 1, 1) is not None:
            value = dict(value) if isinstance(value, dict) else {"total": value}
            value["land_patterns"] = lands.get(str(path), [])
            sizes[str(path)] = value
    scored, distances = [], []
    for path in shortlist:
        if not _module_total_fits(path, sizes.get(str(path)), _component,
                                  length_mm, width_mm, tolerance, note):
            continue
        distance = size_distance(sizes.get(str(path)), length_mm, width_mm)
        if distance is not None:
            distances.append(distance)
        if distance is not None and distance <= tolerance:
            scored.append((distance, str(path), path))
    if not scored:
        closest = min(distances, default=None)
        detail = (tx("; il piu' vicino sbaglia del {value:.0f}%", value=closest * 100) if closest is not None
                  else tx("; nessun solido con misure utilizzabili dopo i controlli"))
        note(tx("{package}: {n_found} modelli trovati, nessuno compatibile con "
                "{length_mm:.2f} x {width_mm:.2f} mm{detail}; si usa la forma "
                "approssimata.",
                package=package,
                n_found=len(found),
                length_mm=length_mm,
                width_mm=width_mm,
                detail=detail))
        return None
    # La misura non distingue due varianti con lo stesso corpo (SOT-223 e
    # SOT-223-8 differiscono per il numero di reofori, non per l'ingombro).
    # Fra candidati che la misura sostiene allo stesso modo decide il nome:
    # la corrispondenza esatta prima, poi quella piu' corta.
    best = min(d for d, _, _ in scored)
    wanted = _package_name(package)
    equivalent_paths = [p for d, _, p in scored if d <= best + NAME_TIEBREAK_MARGIN]
    contact_counts = {count for p in equivalent_paths if (count := _declared_count(p)) is not None}
    exact_identifier = any(_normalize(p.stem) == wanted for p in equivalent_paths)
    if (_declared_count(Path(str(package))) is None and not exact_identifier
            and len(contact_counts) > 1):
        note(tx("{package}: topologia contatti sconosciuta; candidati equivalenti "
                "dichiarano conteggi diversi ({join}), il package proposto non "
                "specifica il conteggio; si usa la forma approssimata.",
                package=package,
                join=', '.join(map(str, sorted(contact_counts)))))
        return None
    scored.sort(key=lambda item: (item[0] > best + NAME_TIEBREAK_MARGIN,
                                  _normalize(item[2].stem) != wanted,
                                  len(item[2].stem), item[1]))
    distance, _, path = scored[0]
    equivalent = sum(d <= best + NAME_TIEBREAK_MARGIN for d, _, _ in scored)
    if equivalent > 1:
        note(tx("{package}: {equivalent} modelli con ingombri comparabili; la misura "
                "non distingue le varianti, scelta per nome non identificativa.",
                package=package,
                equivalent=equivalent))
    evidence = min(((d, label) for label, value in _readings(sizes[str(path)])
                    if (d := size_distance(value, length_mm, width_mm)) is not None))
    note(tx("{package}: modello reale {name}, {describe_size} contro misura "
            "{length_mm:.2f} x {width_mm:.2f} mm, scarto {value:.0f}%; lettura: "
            "{evidence}. Compatibilità dimensionale, identità/variante non "
            "verificata.",
            package=package,
            name=path.name,
            describe_size=describe_size(sizes[str(path)]),
            length_mm=length_mm,
            width_mm=width_mm,
            value=distance * 100,
            evidence=evidence[1]))
    return path


def describe_size(size) -> str:
    """@brief Render a measured model size for the log, body and total."""
    if isinstance(size, dict):
        parts = []
        for label, value in _readings(size):
            if size_distance(value, 1, 1) is not None:
                parts.append(f"{label} {float(value[0]):.2f} x {float(value[1]):.2f}")
        return "; ".join(parts) or "ingombro ignoto"
    if size_distance(size, 1, 1) is not None:
        return f"{float(size[0]):.2f} x {float(size[1]):.2f}"
    return "ingombro ignoto"


def _explicit_selection(component):
    """Validate an exact user choice without substituting a similar filename."""
    selection = component.get("model_selection")
    if not isinstance(selection, dict) or selection.get("status") != "confirmed":
        return None
    raw = selection.get("path")
    rotation = selection.get("rotation_deg")
    if not isinstance(raw, str) or not raw.strip() or isinstance(rotation, bool):
        return None
    try:
        path = Path(raw)
        rotation = float(rotation) if rotation is not None else None
        if (path.suffix.lower() not in (".step", ".stp") or not path.is_file()
                or (rotation is not None and not math.isfinite(rotation))):
            return None
        return path, rotation
    except (OSError, ValueError, TypeError):
        return None


def _basis_readings(size, basis):
    """An explicit photographic basis must not borrow a better-fitting envelope."""
    for label, value in _readings(size):
        if basis in ("body", "total") and label != basis:
            continue
        if basis == "pads" and not label.startswith("piazzole "):
            continue
        yield label, value


def _discovery_structure(paths, component, package):
    """Use explicit mounting/subtype declarations for discovery, never identity.

    Unknown mounting stays eligible. A USB receptacle declaration excludes an
    explicitly different interface/plug, and a tactile actuator excludes named
    slide/rotary/reed banks. Vendor names are neither clues nor special cases.
    """
    surface = bool(re.search(r"(?<!\w)(?:SMD|SMT|surface[- ]mount)(?!\w)", package, re.I))
    through = bool(re.search(r"(?<!\w)(?:THT|through[- ]hole)(?!\w)", package, re.I))
    tactile = bool(re.search(r"(?<!\w)(?:tactile|push[- ]?button)(?!\w)", package, re.I))
    usb = re.search(r"(?<!\w)USB[ _-]*(?:Type[ _-]*)?([ABC])(?!\w)", package, re.I)
    socket = bool(re.search(r"\b(?:receptacle|socket)\b", package, re.I))
    plug = bool(re.search(r"\bplug\b", package, re.I))
    selected = []
    for path in paths:
        folder = path.parent.name.lower().removesuffix('.3dshapes')
        if surface != through and ((surface and folder.endswith('_tht')) or
                                   (through and folder.endswith('_smd'))):
            continue
        if tactile and _candidate_family(path) == 'switch' and re.search(
                r'(?:^|_)(?:DIP|Slide|Rotary|Reed)(?:_|$)', path.stem, re.I):
            continue
        if usb and _candidate_family(path) == 'connector':
            interface = re.search(r'(?:^|_)USB[_-]([ABC])(?:_|$)', path.stem, re.I)
            if not interface or interface.group(1).upper() != usb.group(1).upper():
                continue
            if socket != plug and ((socket and re.search(r'(?:^|_)Plug(?:_|$)', path.stem, re.I)) or
                                   (plug and re.search(r'(?:^|_)Receptacle(?:_|$)', path.stem, re.I))):
                continue
        selected.append(path)
    return selected, bool(usb or tactile or surface != through)


def _tentative_endcap_family(component):
    """Recognize an estimated chip form, without trusting its nominated size.

    A standalone standard chip-size proposal plus independently observed two
    endcaps supports the rectangular chip family, not an exact size or value.
    Search every size and suffix variant in that declared family. This is a
    scoped mechanical estimate, never a claim to have searched arbitrary
    electrolytic, trimmer, cylindrical, leaded or custom non-chip housings.
    Ambiguous namespaces, part numbers and absent morphology stay unresolved.
    """
    kind = component.get("kind")
    layout = str(component.get("terminal_layout") or "").strip()
    # Deliberately narrow affirmative grammar, not keyword classification of
    # arbitrary prose. Negated, hidden or projecting terminals cannot establish
    # an endcap topology; unsupported descriptions retain the review fallback.
    endcaps_observed = bool(re.match(
        r"^(?:two|2)\s+(?:(?:metal|metallized|plated|tinned|opposing|opposed|opposite|visible)\s+)*end[- ]?caps?\b",
        layout, re.I)) and not re.search(
            r"\b(?:no|not|without|neither|unknown|uncertain|unclear|occluded|hidden|absent|projecting|maybe|possibly)\b",
            layout, re.I)
    if (kind not in _ENDCAP_CHIP_CATALOG
            or component.get("class", kind) != kind
            or component.get("presence_confidence") != "high"
            or component.get("class_confidence") not in ("high", "medium")
            or not endcaps_observed):
        return None
    package = str(component.get("package") or "").strip()
    # Una sigla nuda ("0603") e' una dichiarazione, e la ricerca per nome la
    # tratta come tale. Qui interessa il caso opposto: la taglia *nominata*
    # dentro una descrizione -- "0603 (approx)", ma anche "0603 chip" o
    # "0603 chip LED", che il modello scrive molto piu' spesso della forma fra
    # parentesi. Il criterio e' strutturale, non un elenco di parole: il campo
    # non e' una sigla nuda, non risolve ad alcun package dichiarato, e nomina
    # una sola taglia. Due taglie ("0603/0805") restano un'ambiguita' da
    # risolvere a monte, non una proposta da verificare.
    #
    # Perche' la nomina vada verificata e non usata come vincolo: su un run
    # reale di 53 componenti, 21 dichiarano "0603 chip" e per 4 di questi la
    # misura fotografica indica una classe intera piu' grande (0805). Cercare
    # per nome un 0603 li avrebbe lasciati senza alcun candidato.
    nominated = set(re.findall(r"(?<![\w.-])" + _CHIP_SIZE_CODES + r"(?![\w.])", package))
    if (re.fullmatch(_CHIP_SIZE_CODES, package) or _package_token(package)
            or len(nominated) != 1):
        return None
    counts = _observed_count(component)
    visible_pair = (not counts and component.get("terminal_count_basis") == "visible_including_tabs"
                    and type(component.get("terminal_count")) is int
                    and component["terminal_count"] == 2
                    and component.get("confidence") in ("high", "medium"))
    return kind if counts == {2} or visible_pair else None


def _family_paths(component, root, basis):
    """Rank family solids cheaply; names prioritize measurement, never certify size.

    Cached readings retain their photographic basis. Filename millimetres and
    passive Metric codes are discovery hints only, including on a cold cache.
    Explicit mounting/subtype descriptions also admit cold family models into
    the same bounded CAD queue. Omitted candidates cannot prove uniqueness.
    The caller additionally applies the existing fixed CAD resource budget.
    """
    kind = str(component.get("kind") or component.get("detected_kind") or "").lower()
    observed_class = str(component.get("class") or "").lower()
    # Rendering normalizes these observations to 'ic'. Preserve the original
    # mechanical class for discovery without rewriting the rendering metadata.
    if observed_class in ("semiconductor_unknown", "transistor", "regulator"):
        kind = observed_class
    aliases = dict(KIND_FAMILY_ALIASES, regulator="semiconductor")
    family = aliases.get(kind, kind)
    # Discovery must cover every family accepted by the compatibility filter:
    # a generic IC can use SO/QFN or SOT. Omitting SOT would manufacture a unique
    # SO candidate. Explicit transistor/regulator classes may narrow the search.
    families = ({"ic", "semiconductor", "diode"} if kind == "semiconductor_unknown" else
                {"ic", "semiconductor"} if kind == "ic" else {family})
    if family not in {"resistor", "capacitor", "diode", "led", "inductor", "ic",
                      "connector", "switch", "module", "crystal", "semiconductor"}:
        return [], False
    package = str(component.get("package") or "")
    alternatives = re.findall(_NAMED_PACKAGE_PATTERN, package, re.I)
    endcap_family = _tentative_endcap_family(component)
    marked_models = _marked_model_paths(component, root) if family == "module" else []
    if marked_models:
        # A literal manufacturer marking can identify a model filename in a
        # user-installed library. It remains dimensional evidence plus a
        # reviewable proposal; it is not a blanket vendor/package inference.
        paths = marked_models
        structural_query = True
    elif endcap_family:
        # Approximate 0603 must not become an exact 0603 lookup: adjacent sizes
        # and suffix variants remain alternatives. Even clearly distant cold
        # chip sizes are measured, so filename hints cannot fabricate uniqueness.
        prefix, folder = _ENDCAP_CHIP_CATALOG[endcap_family]
        paths = [p for p in (root / folder).rglob("*")
                 if p.suffix.lower() in (".step", ".stp")
                 and re.match(prefix + r"_\d{4,6}(?:_|$)", p.stem, re.I)]
    elif alternatives:
        # Ambiguity broadens only to the stated alternatives, never to all ICs.
        paths = list(dict.fromkeys(p for token in alternatives for p in candidates(token, root)
                                   if p.suffix.lower() in (".step", ".stp")))
    else:
        paths = [p for p in root.rglob("*") if p.suffix.lower() in (".step", ".stp")
                 and _candidate_family(p) in families]
        # A family-only declaration is still structural evidence, even when it
        # lacks a numeric package identifier: QFN prose must not discover SO/SOT.
        topologies = {token.upper().replace("LQFN", "QFN") for token in re.findall(
            r"(?<![\w])(?:LQFN|QFN|DFN|SOIC|SSOP|TSSOP|MSOP|TSOP|SOP|SOT|SC|TO|LQFP|TQFP|QFP|BGA|DIP)(?![\w])",
            package, re.I)}
        if topologies:
            paths = [p for p in paths if any(re.match(re.escape(token) + r"[-_]?\d", p.stem, re.I)
                                            for token in topologies)]
    paths, structural_query = _discovery_structure(paths, component, package)
    index = load_index(root)
    ranked, omitted = [], False
    for path in paths:
        size = normalize_model_measurement(path, index.get(index_key(path)))
        distances = [d for _, value in _basis_readings(size, basis)
                     if (d := size_distance(value, component["length_mm"], component["width_mm"])) is not None]
        cached = bool(distances)
        if not cached:
            hint = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(?:x\d+(?:\.\d+)?)?mm", path.stem, re.I)
            metric = re.search(r"(?:^|_)(\d{2})(\d{2})Metric(?:_|$)", path.stem, re.I)
            dimensions = ([float(v) for v in hint.groups()] if hint else
                          [int(v)/10 for v in metric.groups()] if metric and family in ("resistor", "capacitor", "led") else None)
            distance = size_distance(dimensions, component["length_mm"], component["width_mm"])
            if distance is not None:
                distances.append(distance)
        distance = min(distances, default=float("inf"))
        # Standard TO/SOT names encode package IDs, not millimetres. Requiring
        # filename dimensions would permanently hide these families on a cold
        # index. Include them in the existing bounded CAD measurement budget.
        cold_discrete = not cached and _candidate_family(path) == "semiconductor"
        if (distance <= DEFAULT_SIZE_TOLERANCE or (endcap_family and not cached)
                or (alternatives and not cached) or cold_discrete
                or (structural_query and not cached and not distances)):
            ranked.append((distance, str(path), path))
        elif not cached:
            # A name hint is not a measured envelope. Skipping it saves cold CAD
            # work but must never turn the remaining choice into a proof.
            omitted = True
    return [path for _, _, path in sorted(ranked)], omitted


def _literal_marking_identity(component, choices, complete, discovery_scope):
    """Whether the marking on the body names the single library model found.

    Decisione dell'utente (B): la scritta letterale sul pezzo e' evidenza di
    IDENTITA', non di geometria. Quando la scoperta e' stata ristretta ai soli
    file nominati dalla marcatura (`discovery_scope == 'literal_marking_model'`)
    e ne e' rimasto uno solo dopo i vincoli d'ingombro, il riquadro fotografico
    non serve a dire *quale* modello e', ma solo a escludere che quel corpo NON
    possa essere quel modello. Per questo la porta accetta anche una base
    'body' con lettura di sezione non verificata, o uno scarto XY che i
    criteri puramente dimensionali respingerebbero, purche' resti entro
    DEFAULT_SIZE_TOLERANCE: oltre, la marcatura sarebbe stata letta su un corpo
    che non puo' essere quel modello (caso negativo). Due file diversi che
    corrispondono letteralmente alla stessa marcatura (varianti) NON sono
    identita': restano una proposta provvisoria (caso negativo).

    Identita' vuol dire che la scritta nomina il file per intero: il gambo
    normalizzato del nome deve essere UGUALE a un identificatore della
    marcatura (_marked_model_is_named). Un file che contiene la scritta come
    sottostringa (marcatura ``X-Y-1``, unico file ``X-Y-1U.step``) e' una
    variante con lo stesso prefisso, che puo' avere un'altra geometria: entra
    in lista per la scoperta, ma resta provvisorio (caso negativo).

    La regola non nomina alcun modello o marca: dipende dallo scopo della
    scoperta e da grandezze misurate. Presenza 'high' e ricerca completa
    restano richieste, perche' senza il corpo verificato la marcatura non ha
    un soggetto e una ricerca troncata non dimostra l'unicita'.

    @param component Component evidence (presence, marking).
    @param choices Candidates ranked by model_candidates, all within tolerance.
    @param complete True when every structurally compatible model was measured.
    @param discovery_scope The `discovery_scope` recorded by model_candidates.
    @return True when the single literal-marking candidate identifies the body.
    @note The final `relative_xy_error <= DEFAULT_SIZE_TOLERANCE` check is
        defence in depth: model_candidates already drops every candidate
        above the tolerance before ranking, so in production the list never
        holds one. It is kept because this function is also called on
        replayed candidate lists (tests, offline reports) where the caller,
        not model_candidates, built `choices`, and the gate's contract --
        "beyond the tolerance the marking was read on something else" --
        must hold on its own.
    """
    if (discovery_scope != "literal_marking_model" or not complete
            or len(choices) != 1
            or component.get("presence_confidence") != "high"
            or component.get("_yolo_review_only") is True
            or component.get("_yolo_origin") == "low_confidence_review"
            or component.get("_global_inventory_status") in ("invalid", "rejected")):
        return False
    choice = choices[0]
    if choice.get("family") != "module" or not _marked_model_is_named(Path(choice["path"]), component):
        return False
    try:
        error = float(choice.get("relative_xy_error"))
    except (TypeError, ValueError):
        return False
    return math.isfinite(error) and error <= DEFAULT_SIZE_TOLERANCE


def _automatic_mechanical(component, choices, complete, basis, discovery_scope=None):
    """Require independent presence, family, terminal and envelope support.

    Uniqueness is evaluated before UI truncation, never inferred from a budgeted
    or unreadable search. This conservative estimate does not certify identity,
    with one declared exception: a single model named literally by the marking
    on the body (see _literal_marking_identity) is identity evidence and is
    not subject to the envelope-basis and terminal gates below.

    @param discovery_scope The `discovery_scope` recorded by model_candidates,
        when the caller knows it; None keeps the purely dimensional gates.
    """
    if _literal_marking_identity(component, choices, complete, discovery_scope):
        return True
    if (not complete or len(choices) != 1 or basis not in ("body", "total")
            or (basis == "body" and choices[0].get("measurement_method") == "section_65pct_unverified")
            or component.get("presence_confidence") != "high"
            or component.get("class_confidence") not in ("medium", "high")
            or component.get("_yolo_review_only") is True
            or component.get("_yolo_origin") == "low_confidence_review"
            or str(component.get("class") or "").lower() in ("semiconductor_unknown", "unknown", "other")
            or str(component.get("kind") or "").lower() not in (
                "resistor", "capacitor", "tantalum", "diode", "diode_melf", "led",
                "inductor", "ic", "ic_soic", "ic_qfn", "ic_qfp", "ic_bga",
                "connector", "switch", *MODULE_KINDS, "crystal", "transistor")
            or component.get("_global_inventory_status") in ("invalid", "rejected")):
        return False
    counts = _observed_count(component)
    raw_count = component.get("terminal_count")
    visible_only = component.get("terminal_count_basis") == "visible_including_tabs"
    if not visible_only and raw_count is not None and (type(raw_count) is not int or raw_count not in counts):
        return False
    path = Path(choices[0]["path"])
    declared = _declared_count(path)
    # Standard single chip R/C packages have two end terminals; do not extend
    # that topology assumption to arrays, LEDs, inductors or arbitrary solids.
    if (declared is None and choices[0]["family"] in ("resistor", "capacitor")
            and re.fullmatch(r"[RC]_\d{4,6}(?:_\d{4}Metric)?", path.stem, re.I)):
        declared = 2
        # A recognized single chip R/C with both endcaps visible has the same
        # two-terminal mechanical topology as these standard models. This is
        # not generalized to arrays or an unknown semiconductor with two of
        # several leads visible. Conflicting explicit counts still veto.
        if (not counts and visible_only and type(raw_count) is int and raw_count == 2
                and component.get("confidence") in ("medium", "high")):
            counts = {2}
    # A literal manufacturer model identifier is stronger than a generic
    # family estimate. Permit the single exact-marking module to be imported
    # automatically, while retaining the dimensional and presence gates above.
    # Exact means the whole filename stem: a file that merely contains the
    # marked code is a sibling variant and stays with the provisional path.
    if (declared is None and choices[0]["family"] == "module"
            and _marked_model_is_named(path, component)):
        return True
    return declared is not None and counts == {declared} and choices[0]["family"] is not None


def _is_endcap_chip_model(path: Path, kind: str) -> bool:
    """Whether a model belongs to the standard chip catalogue of `kind` (_ENDCAP_CHIP_CATALOG).

    Membership is structural: the catalogue folder plus KiCad's naming scheme
    for it, ``<prefix>_<imperial code>_<metric code>Metric`` with nothing
    after it. It is NOT the list of size codes _CHIP_SIZE_CODES, which misses
    real catalogue sizes (1808, 2220, 0612...): filtering by that list
    removed the true size and promoted a wrong one (audit 2026-10-02).
    Variants with a suffix (_Castellated, _SideView, _Pad..._HandSolder)
    are other bodies or share the plain model's STEP file. The imperial code
    states length then width (4 digits: LLWW, 5 digits: LLLWW, hundredths of
    an inch); a "reverse geometry" part (0612, 0815, 1020, 1218) has its
    terminals on the LONG sides, so its length is smaller than its width.
    The classifier's chip classes have end caps on the short sides: only
    codes with length >= width belong to their catalogue (the same outline
    as a 1206 must not become an 0612).
    """
    prefix, folder = _ENDCAP_CHIP_CATALOG[kind]
    match = re.fullmatch(rf"{prefix}_(\d{{4,5}})_\d{{4,5}}Metric", path.stem)
    if path.parent.name != folder or match is None:
        return False
    code = match.group(1)
    split = 2 if len(code) == 4 else 3
    return int(code[:split]) >= int(code[split:])


def _classifier_admits(path: Path, component) -> bool:
    """Whether a model fits the package the local classifier declared for the body.

    A chip body (`_smd_body` endcap_chip) admits only its kind's chip
    catalogue; a class with a declared terminal count admits only models that
    declare that same count: in their KiCad name (_declared_count), or, when
    the name carries none (D_SOD-123F, D_MiniMELF, SOT-23), in the ordinary
    copper pads of the footprints that link the model (_declared_land_pattern:
    pads minus tabs, only when every linking footprint agrees). A model whose
    count cannot be read from either is not admitted: a count nobody can read
    cannot confirm the classifier's. When BOTH are readable both must agree
    with the class: the name gives the package family, the copper gives what
    the footprint actually solders (SOIC-8-N7 names 8 positions and has 7
    pads; adversarial review of 2026-10-02, A2).
    """
    kind = str(component.get("kind") or "").lower()
    if component.get("_smd_body") == "endcap_chip" and kind in _ENDCAP_CHIP_CATALOG:
        return _is_endcap_chip_model(path, kind)
    count = component.get("_smd_terminal_count")
    if type(count) is int and count > 0:
        lands = _declared_land_pattern(path)
        readable = [value for value in (_declared_count(path),
                                        lands[0] - lands[1] if lands else None)
                    if value is not None]
        return bool(readable) and all(value == count for value in readable)
    return False


def _local_classifier_identity(component):
    """@brief Whether a local package classifier identified this body on a box a person kept.

    @param component Component evidence dictionary.
    @return True when the local SMD network accepted the body's class
        (`_smd_accepted`: not `_other`, at or above max(0.5, the checkpoint's
        suggested threshold)), the detector review was done
        (`_presence_reviewed_by_user`), and the class narrows the package
        structurally: a chip body (`_smd_body`) or a declared terminal count
        (`_smd_terminal_count`). Classes that name only a kind (arrays, RGB
        LEDs, electrolytics, inductors, crystals, switches, QFN/DFN/MSOP...)
        keep the ordinary gates: the audit of 2026-10-02 found them promoted
        to arbitrary vendor models.
    @note This is independent package-family evidence, like a literal
        marking: the network looked at the body's pixels and named a package
        family (chip resistor, SOT-23, SOIC-8...). It does not replace the
        measurement, which still has to single out one size within that
        family (_provisional_geometry_choice). User request 2026-10-02:
        «l'importante come primo passo che venga riconosciuto package ed
        orientamento».
    """
    narrows = (component.get("_smd_body") == "endcap_chip"
               or (type(component.get("_smd_terminal_count")) is int
                   and component["_smd_terminal_count"] > 0))
    return (component.get("_smd_accepted") is True
            and component.get("_presence_reviewed_by_user") is True
            and bool(component.get("_smd_class")) and narrows)


def _classifier_geometry_choice(component, choices, complete):
    """Provisional preview for a body whose package family the local classifier named.

    The family comes from the classifier; the size from the measurement. The
    candidates are already restricted to that family by discovery and to the
    measurement tolerance, so a lone in-tolerance candidate of a complete
    search is the only size of the family the photograph allows -- the
    comparison happened against the family members the tolerance excluded.
    A homogeneous tie band (variants the photograph cannot tell apart) gives
    its head. A band of sizes the photograph barely separates (within
    NAME_TIEBREAK_MARGIN of the best, not homogeneous) gives its CLOSEST
    member, only on a complete search, and records the others in
    `tie_alternatives` (user decision 2026-10-02: «proporre la piu' vicina»;
    a chip measured 1.9 x 1.0 mm between 0603 and 0805 used to get no model).
    «Closest» weighs BOTH axes: the sum of the absolute per-axis relative
    deviations from the measured sides, worst-axis error as tie-break
    (_axis_deviation_sum). The worst axis alone picked a 0603 (21 % off on the
    length) over an 0805 (2.5 % off) for a 1.95 x 1.00 mm reading, because the
    short side of a tiny chip is the noisier one (adversarial review A1).
    That closest pick is a declared preference, not a measurement verdict:
    an inflated box can favour the larger size, so it stays provisional and
    the alternatives stay in the picker. The band is formed BEFORE the basis
    filter: a model that ties on another basis (body vs pads) is not compared
    with the chosen one, but it is still a size the photograph cannot rule
    out, so it is named among the alternatives and the pick is never presented
    as unique (review A3). What the classifier does NOT supply is the
    basis of the box (body or total), so this path compares with whichever
    envelope fits (`_basis_readings` with an unknown basis), and stays
    provisional for that reason too.
    """
    if (not choices or component.get("_yolo_review_only") is True
            or component.get("_yolo_origin") == "low_confidence_review"
            or component.get("_global_inventory_status") in ("invalid", "rejected")
            or component.get("topology_incomplete")):
        return None
    # Solo i modelli che il package dichiarato dalla rete ammette: catalogo
    # chip per un corpo a due cappucci, stesso numero di terminali per un
    # package che lo dichiara. Gli altri restano nel selettore.
    choices = [item for item in choices if _classifier_admits(Path(item["path"]), component)]
    if not choices:
        return None
    full_band = _tie_band(choices) or []
    # Una sola base di misura nella banda: il corpo di un package non si
    # confronta con l'ingombro totale o con le piazzole di un altro (audit
    # 2026-10-02: LQFP 7x7 letto come LQFP-32 5x5). Le piazzole restano una
    # base valida: il riquadro del rilevatore comprende le saldature, e per
    # una chip 0805 misurata 2.46 x 1.40 mm sono la lettura piu' vicina.
    def basis_kind(item):
        label = str(item.get("measurement_basis") or "")
        return "pads" if label.startswith("piazzole") else label
    kind_of_basis = basis_kind(choices[0])
    if kind_of_basis not in ("body", "total", "pads"):
        return None
    choices = [item for item in choices if basis_kind(item) == kind_of_basis]
    first = choices[0]
    if first.get("family") is None:
        return None
    try:
        best_error = float(first.get("relative_xy_error"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(best_error) or best_error > DEFAULT_SIZE_TOLERANCE:
        return None
    band = _tie_band(choices)
    if band is None or not any(item is first for item in band):
        return None
    # Concorrenti alla pari su un'altra base: non confrontabili, ma la foto
    # non li esclude, quindi vanno nominati (A3).
    other_bases = [item for item in full_band if basis_kind(item) != kind_of_basis]

    def with_alternatives(chosen, peers):
        names = [Path(item["path"]).stem for item in (*peers, *other_bases)
                 if item is not chosen]
        if names:
            chosen["tie_alternatives"] = names
        return chosen
    if len(band) == 1:
        return with_alternatives(first, ()) if complete else None
    if _band_is_homogeneous(band, component.get("length_mm"), component.get("width_mm")):
        return with_alternatives(first, ()) if other_bases else first
    if not complete:
        # La piu' vicina fra taglie misurate: con candidati mai misurati
        # «piu' vicina» non vuol dire nulla.
        return None
    length, width = component.get("length_mm"), component.get("width_mm")
    closest = min(band, key=lambda item: (_axis_deviation_sum(item, length, width),
                                          float(item["relative_xy_error"])))
    return with_alternatives(closest, band)


def _axis_deviation_sum(item, length_mm, width_mm) -> float:
    """Sum over both axes of |model - measured| / measured (long to long, short to short).

    The «closest size» measure of _classifier_geometry_choice. Infinite when
    a dimension cannot be read, so such a candidate is never preferred.
    """
    try:
        measured = sorted(float(v) for v in (length_mm, width_mm))
        model = sorted(float(v) for v in (item.get("dimensions_mm") or ())[:2])
    except (TypeError, ValueError):
        return math.inf
    if (len(model) != 2 or any(isinstance(v, bool) for v in (length_mm, width_mm))
            or not all(math.isfinite(v) and v > 0 for v in (*measured, *model))):
        return math.inf
    return sum(abs(m - side) / side for m, side in zip(model, measured))


def _provisional_geometry_choice(component, choices, complete, basis):
    """Return a safe preview candidate without claiming package identity.

    The photo often cannot distinguish variants which have the same XY
    footprint (for example two tactile switches differing only in height, or
    two QFN models with the same outline).  Leaving those candidates entirely
    synthetic made the 3D preview useless even though KiCad had a compatible
    solid.  This path therefore attaches only a *provisional* model: the
    report remains reviewable and the UI keeps the candidate picker visible.

    The decision is taken on the *tie band*: every candidate whose
    relative_xy_error lies within NAME_TIEBREAK_MARGIN of the best one, i.e.
    all the solids the top view cannot tell apart.  Two situations qualify,
    and they are opposites: either the band is homogeneous (one family, and
    on each axis the members' relative deviations from the measured side
    agree within NAME_TIEBREAK_MARGIN, i.e. the photo cannot tell any two of
    them apart; see _band_is_homogeneous), so the residual doubt is about
    identity and variant rather than about the XY geometry being previewed;
    or the band holds a single candidate, meaning
    the measurement separated it from every alternative.  A homogeneous tie
    is allowed on a truncated search, because a tie says nothing about what
    was not measured; a lone band member requires a complete search, since
    outranking alternatives nobody measured is not outranking them, and at
    least one other candidate in the list, since a single candidate overall
    was never compared with anything.  A band
    that mixes families or envelopes is never promoted, however many members
    agree with each other: a third solid that the measurement cannot exclude
    is part of the doubt, not a bystander.  A literal manufacturer/model
    marking and the three-lead-tab morphology are earlier exits and do not
    depend on the band.  A topology-incomplete search is never promoted.

    @param component Component evidence (presence, class, kind, package prose).
    @param choices Candidates ranked by model_candidates, all within tolerance.
    @param complete True when every structurally compatible model was measured.
    @param basis Normalised bbox basis; only body/total readings qualify.
    @return The ranked head candidate to preview, or None.
    @note A body identified by the local SMD classifier takes its own exit
        (_classifier_geometry_choice): the classifier's package family stands
        in for the class confidence, the presence and the kind gates below.
    """
    if _local_classifier_identity(component):
        return _classifier_geometry_choice(component, choices, complete)
    if (not choices or basis not in ("body", "total")
            or component.get("presence_confidence") != "high"
            or component.get("_yolo_review_only") is True
            or component.get("_yolo_origin") == "low_confidence_review"
            or component.get("_global_inventory_status") in ("invalid", "rejected")):
        return None
    three_lead_tab = _three_leads_one_tab(component)
    # A three-lead body with one large tab is enough to preview the mechanical
    # SOT-223 family even when the electrical class is still unknown.  It is
    # deliberately a geometry-only exception: the report remains provisional
    # and never claims that the device is a regulator or a transistor.
    morphology_exception = three_lead_tab
    if (component.get("class_confidence") not in ("medium", "high")
            and not morphology_exception):
        return None
    kind = str(component.get("kind") or "").lower()
    if kind not in ("ic", "ic_soic", "ic_qfn", "ic_qfp", "ic_bga",
                    "connector", "switch", *MODULE_KINDS):
        return None
    # Multiple explicitly named alternatives are a semantic ambiguity, even
    # when their measured outlines happen to tie.  Keep those in the picker;
    # a single QFN-24/LQFN-family declaration is handled by normal package
    # discovery and is not affected by this guard.
    package = str(component.get("package") or "")
    if re.search(r"\s/\s", package) and not _package_token(package):
        return None
    first = choices[0]
    try:
        best_error = float(first.get("relative_xy_error"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(best_error) or best_error > DEFAULT_SIZE_TOLERANCE:
        return None
    # A literal marking is stronger than a generic visual package guess.  It
    # may still have a body-only photographic envelope, so do not require an
    # exact body/total reading here; the report remains explicitly provisional.
    if (first.get("family") in ("module", "ic")
            and _marked_model_matches(Path(first["path"]), component)):
        return first
    if morphology_exception and _leads_and_tab_agree(first, choices, component):
        return first
    if component.get("topology_incomplete"):
        return None
    # La banda di pareggio e' l'insieme di TUTTI i candidati che la misura
    # dall'alto non distingue dal migliore, non i primi due della lista.
    # Guardare solo choices[0] e choices[1] lasciava fuori dal confronto un
    # terzo solido entro il margine: nella corsa reale due varianti identiche
    # di un modulo radio "pareggiavano" fra loro e venivano promosse, mentre
    # il modulo giusto, terzo a meno del margine, non partecipava alla
    # decisione. Un solido che la misura non sa escludere fa parte del dubbio.
    band = _tie_band(choices)
    if band is None or not any(item is first for item in band):
        return None
    # Il pareggio dice "la fotografia non distingue queste varianti"; la
    # dominanza dice "la fotografia le ha distinte, e una sola regge". Sono
    # le due situazioni in cui l'incertezza residua non riguarda la geometria
    # XY. Riconoscere solo la prima premiava le liste ambigue e puniva quelle
    # nitide: su un ricettacolo USB-C con candidati al 4% e al 16% non si
    # proponeva nulla, mentre sarebbe bastato un doppione identico del
    # migliore. La dominanza pretende pero' una ricerca completa: dominare
    # alternative che nessuno ha misurato non e' dominare. Il pareggio
    # omogeneo resta ammesso su una ricerca troncata, perche' non afferma
    # nulla su cio' che non e' stato misurato.
    # Un candidato solo in tutta la lista non e' dominanza, e' assenza di
    # confronto: la misura non ha scartato nessuno, quindi qui non decide
    # niente (restano la marcatura letterale e la morfologia, sopra).
    if len(band) == 1:
        return first if complete and len(choices) >= 2 else None
    return first if _band_is_homogeneous(
        band, component.get("length_mm"), component.get("width_mm")) else None


#: Rapporto d'area minimo perche' una piazzola del footprint conti come
#: aletta (tab) rispetto alla piazzola mediana del footprint.
#:
#: EURISTICA DICHIARATA: un'aletta dissipa e si stende su piu' larghezze di
#: reoforo. Nella libreria KiCad 10 l'aletta del SOT-223 e' 2.53 volte la
#: piazzola di un reoforo (2.0x3.8 contro 2.0x1.5), quella del TO-252 oltre
#: 14 volte, mentre la piazzola 1 allargata del SOT-143 (non un'aletta) e'
#: 1.67 volte le altre. Separazione con margine MODESTO fra 1.67 e 2.53,
#: dichiarata: la renderebbe superflua un attributo «tab» nel footprint.
TAB_AREA_RATIO = 2.0


def _declared_land_pattern(path: Path, library: Optional[Path] = None):
    """Pads declared by the KiCad footprints linked to a model: (copper pads, tabs).

    A tab is a copper pad whose area is at least TAB_AREA_RATIO times the
    median pad area of the same footprint. Pad NUMBERS are not used: the
    library links the same solid to footprints that number the tab
    differently (SOT-223: tab as 4, or as 2 in SOT-223-3_TabPin2), while the
    copper lands are the same. The footprint is read, never the file name.
    @return (pads, tabs) when every footprint linking the model agrees, else
        None (no footprint, unreadable, or disagreement).
    """
    if library is None:
        # Layout of a KiCad installation: 3dmodels/<Lib>.3dshapes/<Name>.step
        # beside footprints/<Lib>.pretty/<Name>.kicad_mod.
        library = Path(path).parent.parent
    root = library.parent / "footprints"
    if not root.is_dir():
        return None
    target = Path(path).resolve()
    found = set()
    try:
        for footprint, _stamp in _linked_footprints(root, {target.name.casefold()}):
            try:
                tree = _sexpr(footprint.read_text(encoding="utf-8-sig"))
                models = _children(tree, "model")
                if len(models) != 1 or _model_link(models[0], footprint, library) != target:
                    continue
                bounds = [b for pad in _children(tree, "pad")
                          if (b := _pad_bounds(pad)) is not None]
            except (OSError, ValueError, TypeError, IndexError, RecursionError):
                continue
            if not bounds:
                continue
            areas = sorted((b[2] - b[0]) * (b[3] - b[1]) for b in bounds)
            median = areas[len(areas) // 2] if len(areas) % 2 else (
                areas[len(areas) // 2 - 1] + areas[len(areas) // 2]) / 2
            tabs = sum(1 for area in areas if area >= TAB_AREA_RATIO * median)
            found.add((len(areas), tabs))
    except OSError:
        return None
    return next(iter(found)) if len(found) == 1 else None


def _leads_and_tab_agree(first, choices, component) -> bool:
    """Whether the head candidate's land pattern declares "N leads plus one tab" as observed.

    Replaces a rule keyed on the SOT-223 file name and a 0.12 error bound
    tuned on it. The observation is structural (`_three_leads_one_tab`: three
    ordinary leads and one tab); the model side is what its KiCad footprint
    declares -- N + 1 copper pads of which exactly one is a tab
    (_declared_land_pattern) -- never its name. A model whose footprint is
    missing declares nothing and is not promoted (it stays in the picker).
    Every candidate the photograph cannot separate from the head (the tie
    band) must declare the same land pattern and, when there are several,
    form a homogeneous band; otherwise the doubt goes to the picker.
    """
    band = _tie_band(choices)
    if not band or not any(item is first for item in band):
        return False
    leads = component.get("lead_count")
    leads = leads if type(leads) is int and leads > 0 else 3
    if any(_declared_land_pattern(Path(item["path"])) != (leads + 1, 1) for item in band):
        return False
    return len(band) == 1 or _band_is_homogeneous(
        band, component.get("length_mm"), component.get("width_mm"))


def _tie_band(choices):
    """Return the candidates the top view cannot separate from the best one.

    The band is every candidate whose relative_xy_error lies within
    NAME_TIEBREAK_MARGIN of the smallest error in the list, the same criterion
    model_candidates uses to rank.  None means no band can honestly be
    established: an error could not be read, or there is no candidate at all
    (an empty list has no best member to measure the others against).
    """
    errors = []
    for item in choices:
        try:
            error = float(item.get("relative_xy_error"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(error):
            return None
        errors.append(error)
    if not errors:
        return None
    best = min(errors)
    return [item for item, error in zip(choices, errors)
            if error <= best + NAME_TIEBREAK_MARGIN]


def _band_is_homogeneous(band, length_mm, width_mm):
    """True when the band is one family and the photo cannot tell its members apart on any axis.

    Homogeneity is expressed in the SAME quantity as the band itself: the
    relative deviation of a model side from the MEASURED side of the
    photograph, ``(model - measured) / measured``, taken per axis (long side
    to long side, short to short, like size_distance).  relative_xy_error is
    the worst-axis absolute value of exactly this deviation, and the band
    admits a member when that error is within NAME_TIEBREAK_MARGIN of the
    best.  The band therefore says "the photo cannot separate these from the
    best one"; this function asks the stricter question "can the photo
    separate any two of them on either axis?", and answers no only when, on
    each axis, the spread between the largest and the smallest deviation is
    within the same margin.  Spread (max minus min) rather than pairwise
    checks: it is the same test for every pair at once, so two members cannot
    drift twice the margin apart through a third one in the middle.

    No second constant is introduced: a model-versus-model comparison
    normalised by a model side would be a different quantity from the one
    the margin was declared for.  Any unreadable or non-positive dimension,
    on the models or on the measurement, disqualifies the band.

    @param band Candidates returned by _tie_band (non-empty).
    @param length_mm Photographic side in millimetres, either axis.
    @param width_mm The other photographic side in millimetres.
    @return True when one family and every axis has a deviation spread
        within NAME_TIEBREAK_MARGIN.
    """
    families = {item.get("family") for item in band}
    if len(families) != 1:
        return False
    try:
        measured = sorted(float(v) for v in (length_mm, width_mm))
    except (TypeError, ValueError):
        return False
    if (any(isinstance(v, bool) for v in (length_mm, width_mm))
            or not all(math.isfinite(v) and v > 0 for v in measured)):
        return False
    deviations = []
    for item in band:
        dims = item.get("dimensions_mm")
        if not (isinstance(dims, (list, tuple)) and len(dims) >= 2):
            return False
        try:
            xy = sorted(float(v) for v in dims[:2])
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(v) and v > 0 for v in xy):
            return False
        deviations.append([(model - side) / side for model, side in zip(xy, measured)])
    return all(max(axis) - min(axis) <= NAME_TIEBREAK_MARGIN
               for axis in zip(*deviations))


#: Verdetti di presenza di chi ha GIUDICATO il corpo (riconciliazione AI).
_PRESENCE_VERDICTS = ("high", "medium", "low")


def _three_leads_one_tab(component: dict) -> bool:
    """@brief Observed morphology "three ordinary leads plus one tab".

    @param component Component evidence.
    @return True when the observation describes exactly three leads and a tab.
    @note Structural first: the inventory schema declares `lead_count`
        (ordinary leads only) and `terminal_count` (leads INCLUDING a visible
        tab), so 3 + 1 is `lead_count == 3`, `terminal_count == 4` with a
        complete lead count. When both counts are present, only the counts
        decide -- prose cannot override them.
    @note HEURISTIC, declared: rows that do not carry the counts (older runs,
        or payloads that forward only the prose fields) fall back to reading
        the English prose of `terminal_layout`/`package_evidence`/`package`
        with a pattern, and any hedging word vetoes it. It was written on the
        SOT-223-like cases of the observed boards; it becomes superfluous once
        every payload carries `lead_count`/`terminal_count`.
    """
    leads, terminals = component.get("lead_count"), component.get("terminal_count")
    if all(type(value) is int for value in (leads, terminals)):
        return (type(leads) is int and leads == 3
                and type(terminals) is int and terminals == 4
                and component.get("lead_count_complete") is True)
    morphology = " ".join(str(component.get(key) or "")
                          for key in ("terminal_layout", "package_evidence", "package"))
    return bool(re.search(
        r"\b(?:three|3)\s+(?:wide\s+)?(?:gull[- ]wing\s+)?leads?\b"
        r".*\b(?:one|1)\s+(?:large\s+)?(?:rectangular\s+)?tab\b",
        morphology, re.I | re.S,
    )) and not bool(re.search(
        r"\b(?:unknown|unresolved|uncertain|unclear|maybe|possibly|not visible|hidden)\b",
        morphology, re.I,
    ))


def _is_yolo_only_hypothesis(component: dict) -> bool:
    """@brief Identify detector-only rows which are useful for review, not for 3D.

    @param component Component payload (dict).
    @return True for a detector proposal nobody has judged yet.
    @note Structural rule (E11): the row carries a detector identity
        (`_yolo_proposal_id`) and NO presence verdict -- neither the reconciliation's `presence_confidence` or
        `_global_inventory_status`, nor the `_presence_reviewed_by_user` field
        that the detector-only inventory (`photo_inventory`) writes on every
        row the user chose to keep as inventory, reviewed or not. A confirmed
        model selection always wins. Renaming a row no longer changes the
        verdict, in either direction.
    @note `_yolo_origin` alone is deliberately NOT an identity: payloads that
        forward it without `_presence_reviewed_by_user` (the GUI evidence list
        before fix E) would otherwise hide the detector-only inventory the
        user asked for. The proposal id and the review flag travel together.
    @note Legacy compatibility, declared: a row saved before these fields
        reached the 3D payload carries only its detector name ("YOLO candidate
        N"). When a row has neither a detector identity nor a verdict, that
        name is the only evidence left and keeps the historic exclusion. It is
        not a rule on a component or brand name: it is the label the detector
        itself wrote, and any structural field overrides it.
    """
    if (isinstance(component.get("model_selection"), dict)
            and component["model_selection"].get("status") == "confirmed"):
        return False
    proposal = component.get("_yolo_proposal_id")
    identity = isinstance(proposal, str) and bool(proposal.strip())
    verdict = (isinstance(component.get("_presence_reviewed_by_user"), bool)
               or component.get("presence_confidence") in _PRESENCE_VERDICTS
               or component.get("_global_inventory_status") in (
                   "validated_hypotheses", "partial"))
    if verdict:
        return False
    if identity:
        return True
    name = str(component.get("name") or "").strip().lower()
    return name.startswith("yolo candidate")


def _switch_group_key(component: dict) -> str:
    """Normalize descriptive switch prose without using measured dimensions."""
    text = str(component.get("package") or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _synchronize_preview_variants(components, note) -> None:
    """Propose one common candidate for visually same-family switches.

    Independent dimensions of cropped buttons often differ by a few tenths of
    a millimetre.  If their review lists share a compatible native model, the
    same candidate is proposed for all of them so that a later confirmation
    does not end with two visibly different switches.  The shared candidate
    replaces model_match.selected_path and, as for any provisional match
    (user decision A), model_path: the solids preview the same library model,
    declared provisional in the report, until the user confirms or changes it.
    """
    groups = {}
    for component in components or []:
        # Stesso contratto di attach_models: una voce malformata non deve
        # fermare le altre. Qui arriva la lista grezza, non quella filtrata.
        if not isinstance(component, dict):
            continue
        report = component.get("model_match") or {}
        if (str(component.get("kind") or "").lower() != "switch"
                or report.get("status") != "provisional_matched"
                or not _switch_group_key(component)):
            continue
        selection = component.get("model_selection")
        if isinstance(selection, dict) and selection.get("status") == "confirmed":
            continue
        groups.setdefault(_switch_group_key(component), []).append(component)
    for key, group in groups.items():
        if len(group) < 2:
            continue
        by_path = []
        for component in group:
            choices = {item.get("path"): item for item in
                       (component.get("model_match", {}).get("candidates") or [])
                       if item.get("path")}
            by_path.append(choices)
        common = set(by_path[0])
        for choices in by_path[1:]:
            common &= set(choices)
        ranked = []
        for path in common:
            items = [choices[path] for choices in by_path]
            try:
                errors = [float(item.get("relative_xy_error")) for item in items]
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(error) and error <= DEFAULT_SIZE_TOLERANCE
                   for error in errors):
                ranked.append((sum(errors), path, items))
        if not ranked:
            continue
        _, selected, items = min(ranked, key=lambda value: (value[0], value[1]))
        for component, item in zip(group, items):
            report = component["model_match"]
            report["status"] = "provisional_matched"
            report["selected_path"] = selected
            report["shared_preview_group"] = key
            report["shared_preview_reason"] = (
                "Common same-family switch candidate selected for consistent preview; "
                "package variant remains reviewable")
            for candidate in report.get("candidates", []):
                candidate["status"] = "provisional_matched" if candidate.get("path") == selected else "review_required"
                if candidate.get("path") == selected:
                    reasons = candidate.setdefault("reasons", [])
                    reasons[-1:] = [report["shared_preview_reason"]]
            # La proposta condivisa e' provvisoria come quella del singolo
            # componente e, come quella, si aggancia dichiarandosi tale
            # (decisione A): il solido mostra il modello comune, non il corpo
            # generico, e il selettore puo' cambiarlo.
            component["model_path"] = selected
            report["attachment"] = "provisional"
            report["attachment_reason"] = (
                "Model applied PROVISIONALLY: shared same-family switch candidate, "
                "identity not verified; the picker can change it.")
        note(tx("Gruppo di interruttori '{key}': proposto il candidato comune {name} "
                "per {n_group} componenti (anteprima coerente, variante da verificare).",
                key=key,
                name=Path(selected).name,
                n_group=len(group)))


#: Fixed reasons written by the matching into the candidate reports (data on
#: disk, kept in English as they always were) -> Italian source of the text
#: shown to the user. Translated when SHOWN (`localized_reason`), so reports
#: already saved are shown in the interface language too.
_REASON_SOURCES = {
    "Family and dimension discovery": "Trovato per famiglia e misure",
    "Package name compatible": "Nome del package compatibile",
    "No declared family or terminal conflict":
        "Nessun conflitto di famiglia dichiarata o di terminali",
    "Identity and variant require user review":
        "Identità e variante da verificare a mano",
    "Exact literal model marking and compatible dimensions; orientation remains reviewable":
        "Marcatura del modello identica e misure compatibili; orientamento da verificare",
    "Mechanical estimate; identity and variant not verified":
        "Stima meccanica; identità e variante non verificate",
    "Applied provisionally: identity not verified; package variant and "
    "orientation require user review and the picker can replace it":
        "Applicato in via provvisoria: identità non verificata; variante e "
        "orientamento da verificare, il selettore può sostituirlo",
    "Common same-family switch candidate selected for consistent preview; "
    "package variant remains reviewable":
        "Candidato comune della stessa famiglia di pulsanti, scelto per "
        "un'anteprima coerente; variante del package da verificare",
}

_COMPATIBLE_DIMENSIONS = re.compile(r"^Compatible (body|total|piazzole (.*)) dimensions$")


def localized_reason(reason) -> str:
    """@brief Text of one candidate reason in the interface language.

    @param reason Reason as stored in a candidate report.
    @return The translated text; a reason the matching did not write (or
        already Italian, e.g. from the accepted-matches archive) is returned
        unchanged.
    @note «Compatible piazzole <path> dimensions» used to be shown as it was
        stored: English words around an Italian word and a full path. The
        path is reduced to the file name of the footprint it was measured on.
    """
    text = str(reason)
    source = _REASON_SOURCES.get(text)
    if source is not None:
        return tx(source)
    match = _COMPATIBLE_DIMENSIONS.match(text)
    if match is None:
        return text
    if match.group(1) == "body":
        return tx("Misure del corpo compatibili")
    if match.group(1) == "total":
        return tx("Ingombro totale compatibile")
    return tx("Misure compatibili con le piazzole di {footprint}",
              footprint=Path(match.group(2).strip()).name or match.group(2))


def _matching_basis(component: dict) -> str:
    """Measurement basis as the matching understands it."""
    basis = component.get("bbox_basis") or "unknown"
    return {"leads": "total", "body_and_leads": "total"}.get(str(basis), basis)


def archive_can_match(component) -> bool:
    """@brief Tell whether the matching looks for a model for this component at all.

    @param component Component mapping from the table.
    @return False for a model rejected or confirmed by the user, for
        dimensions that cannot be compared, and for an unsupported basis:
        in those cases neither the archive nor the search is consulted.
    @note One place for the rule, used by `model_candidates` and by the
        archive window that highlights the entries this board would use
        (audit 2026-09-25: the window claimed entries the generation skipped).
    """
    if not isinstance(component, dict):
        return False
    selection = component.get("model_selection")
    if isinstance(selection, dict) and selection.get("status") in ("rejected", "confirmed"):
        return False
    length, width = component.get("length_mm"), component.get("width_mm")
    if size_distance([length, width], length, width) is None:
        return False
    return _matching_basis(component) in ("unknown", "body", "total", "pads")


def _accepted_match(component: dict, roots, note, diagnostic=None) -> Optional[dict]:
    """@brief Proposal from the accepted-matches archive, or None.

    @param component Body to match, without an explicit choice.
    @param roots 3D library roots in use.
    @param note Diagnostic callback.
    @return A `library_matched` report with a single candidate, or None.
        The ordinary search runs in both cases; `_prefer_closer` decides
        keeps this proposal and lists library alternatives for review.
    @note The archive helps, it is not required: an unreadable or newer file
        is logged and ignored, and matching proceeds as without it. Nothing is
        written: reading neither creates the archive nor changes its entries.
    @note Polarity depends on how the part sits on this board. Without a stored
        manual angle the observed asymmetric landmark decides, as for every
        automatic attachment; a stored angle is absolute like any explicit
        choice, so the verse must be checked by the user -- the reasons say so.
    """
    from ..services.accepted_models import AcceptedModelStore, AcceptedModelsError
    from .measurement_uncertainty import SOURCE_ASSUMED, SOURCE_DECLARED
    try:
        found = AcceptedModelStore().find(component, roots, diagnostic=diagnostic)
    except (AcceptedModelsError, sqlite3.Error, OSError) as exc:
        if diagnostic is not None:
            diagnostic['error'] = str(exc)
        note(tx("Archivio abbinamenti accettati ignorato: {exc}", exc=exc))
        return None
    if found is None:
        return None
    entry, path = found["entry"], found["path"]
    samples = int(entry.get("samples") or 1)
    accepted_on = str(entry.get("last_accepted") or "")[:10]
    count_reason = (tx("Numero di terminali confrontato: uguale")
                    if found["terminal_count_checked"] else
                    tx("Numero di terminali non confrontato: manca l'osservazione su una "
                       "delle due schede"))
    reasons = [
        tx("Gia' accettato da te in una scheda precedente ({samples} volte, "
           "l'ultima il {accepted_on})",
           samples=samples,
           accepted_on=accepted_on),
        tx("Stessa categoria ({category}) e stessa base di misura; ingombro entro "
           "{tolerance:.2f} mm per lato da quello accettato ({basis})",
           category=entry['category'], tolerance=found["tolerance_mm"],
           basis=(tx("incertezza dichiarata dalla misura")
                  if found["tolerance_source"] == SOURCE_DECLARED else
                  tx("incertezza stimata: scala della foto non nota")
                  if found["tolerance_source"] == SOURCE_ASSUMED else
                  tx("incertezza dalla scala della foto"))),
        count_reason,
        tx("Proposta da confermare: il verso (polarita') va verificato su questa "
           "scheda"),
    ]
    if found.get("visual_score", 0.0) >= 0.45:
        reasons.append(tx("Ritaglio simile a un esempio verificato (OpenCV: {score:.0%})",
                          score=found["visual_score"]))
    if found.get("basis_recovered_visually"):
        reasons[1] = tx("Base di misura diversa: abbinamento recuperato dal ritaglio OpenCV, con misure e terminali compatibili")
    if found["alternatives"]:
        reasons.append(tx("Altre {alternatives} voci compatibili nell'archivio: vince la piu' "
                          "vicina e, a parita', la piu' recente",
                          alternatives=found['alternatives']))
    review = found.get("review_required", False)
    if review:
        reasons = [tx("Ritaglio simile a un esempio verificato (OpenCV: {score:.0%})", score=found['visual_score'])] + found.get('visual_conflicts', [])
    status = "review_required" if review else "library_matched"
    candidate = {"path": str(path), "package": path.stem,
                 "relative_xy_error": found["relative_xy_error"],
                 "measurement_basis": "accepted_library",
                 "family": _candidate_family_for_component(path, component),
                 "status": status, "reasons": reasons}
    note(tx("{value}: modello dall'archivio degli abbinamenti accettati ({stem}); "
            "si confronta comunque con la ricerca in libreria.",
            value=component.get('name') or path.stem,
            stem=path.stem))
    return {"status": status, "candidates": [candidate],
            "accepted_visual_conflicts": found.get("visual_conflicts", []),
            "accepted_visual_first": found.get("visual_first", False),
            "selected_path": None if review else str(path), "discovery": "accepted_library",
            "accepted_orientation": found.get("orientation", {}),
            "accepted_visual_score": found.get("visual_score", 0.0),
            "accepted_basis_recovered_visually": found.get("basis_recovered_visually", False),
            "accepted_entry": {key: entry.get(key) for key in (
                "id", "category", "basis", "long_mm", "short_mm", "height_mm",
                "terminal_count", "model_ref", "rotation_deg", "samples",
                "last_accepted", "source")}}


def model_candidates(component: dict, *, limit=5, library=None, log=None,
                     cancel=None) -> dict:
    """@brief Propose bounded, JSON-safe model choices for explicit human review.

    @param component Component evidence, dimensions, bbox_basis and model_selection.
    @param limit Maximum result count, clamped to zero through five.
    @param library Optional KiCad model root; cached measurements are reused.
    @param log Optional diagnostic callback accepting a string.
    @param cancel Canale di rinuncia (`is_set()`) inoltrato alla misura dei
        candidati non ancora in cache. **None significa non interrompibile**
        ed e' il valore predefinito.
    @throws MatchingCancelled Quando la rinuncia arriva prima o durante la
        misura. NON viene restituito un report parziale: un rapporto senza
        candidati sarebbe indistinguibile da «nessun modello compatibile»,
        cioe' un verdetto su un componente che nessuno ha esaminato.
    @return Mapping with status, candidates and selected_path. Candidate dimensions_mm
        is the matched XY reading (and Z when available), in native model axes.
    @note Dimensional evidence never confirms identity. Explicit confirmation wins;
        independently supported unique estimates use auto_matched instead;
        same-footprint alternatives may use provisional_matched, which proposes
        selected_path; attach_models applies it provisionally and says so.
        Rejection stops discovery. Without an explicit choice, a body whose
        geometric signature matches an entry of the accepted-matches archive
        (`services.accepted_models`) gets that entry as a proposal, but the
        library search ALWAYS runs as well (user decision, 2026-09-26): the
        archive entry wins (status library_matched); nominal library geometry
        does not override a compatible accepted photographic envelope.
        Missing/descriptive packages use family discovery.
        Unknown basis compares independent readings; body/total/pads are strict.
        leads and body_and_leads mean total. At most 64 structurally compatible
        paths are measured; search_truncated flags omitted names, including cold
        family models without compatible dimensional hints, not a full search.
    """
    result = {"status": "unresolved", "candidates": [], "selected_path": None}
    note = log or (lambda message: None)
    # Prima di qualunque lavoro: chi ha gia' chiesto di fermarsi non deve
    # ricevere il verdetto di un esame mai cominciato.
    if cancel is not None and cancel.is_set():
        raise MatchingCancelled(measured={})
    if not isinstance(component, dict):
        return result
    selection = component.get("model_selection")
    if isinstance(selection, dict):
        if selection.get("status") == "rejected":
            return result
        if selection.get("status") == "confirmed":
            explicit = _explicit_selection(component)
            if explicit:
                result.update(status="confirmed", selected_path=str(explicit[0]))
            return result
    if type(limit) is not int or limit <= 0:
        return result
    limit = min(limit, 5)
    if not archive_can_match(component):
        return result
    roots = _search_roots(library)
    root = roots[0] if roots else None
    if root is None:
        note(tx("Libreria 3D di KiCad non trovata: si usano le forme approssimate."))
        return result
    # Un corpo con la stessa firma geometrica di uno che l'utente ha gia'
    # accettato riceve quel modello come proposta da confermare; la ricerca in
    # libreria gira comunque per offrire alternative alla scelta accettata.
    cached = getattr(_archive_state, 'results', None)
    accepted = cached[id(component)] if cached is not None and id(component) in cached else _accepted_match(component, roots, note)
    result = _library_search(component, result, limit=limit, roots=roots, root=root,
                             note=note, cancel=cancel)
    result = _prefer_closer(accepted, result, limit, note) if accepted else result
    if (accepted and _local_classifier_identity(component) and result.get("candidates")
            and not _classifier_admits(Path(result["candidates"][0]["path"]), component)):
        # Una scelta d'archivio fuori dal package che la rete ha visto (una
        # resistenza MELF su un corpo chip, un SOT-23-5 su un SOT-23) non vince
        # in silenzio: resta proposta, da confermare (audit 2026-10-02, A-D5).
        reason = tx("L'archivio propone un modello fuori dal package riconosciuto dalla "
                    "rete locale: da verificare prima di usarlo.")
        result["classifier_archive_conflict"] = True
        result["selected_path"] = None
        result["status"] = "review_required"
        result["candidates"] = [dict(candidate, status="review_required",
                                     reasons=list(candidate.get("reasons", [])) + [reason])
                                for candidate in result["candidates"]]
        note(reason)
    if component.get('_crop_class_status') == 'conflict' or component.get('_category_status') == 'conflict':
        reason = tx("Categoria in conflitto tra inventario e ritagli: scegli il modello dopo la verifica del componente.")
        result['classification_conflict'] = True
        result['selected_path'] = None
        result['status'] = 'review_required' if result.get('candidates') else 'unresolved'
        result['candidates'] = [dict(candidate, status='review_required',
                                     reasons=list(candidate.get('reasons', [])) + [reason])
                                for candidate in result.get('candidates', [])]
        note(reason)
    if _crop_terminal_review_blocks(component):
        reason = tx("Conteggio dei terminali in conflitto o incompleto nei ritagli: verifica il componente prima di assegnare il modello.")
        result['terminal_observation_conflict'] = True
        result['selected_path'] = None
        result['status'] = 'review_required' if result.get('candidates') else 'unresolved'
        result['candidates'] = [dict(candidate, status='review_required',
                                     reasons=list(candidate.get('reasons', [])) + [reason])
                                for candidate in result.get('candidates', [])]
        note(reason)
    return result


def _prefer_closer(accepted: dict, searched: dict, limit: int, note) -> dict:
    """Choose between the archive proposal and the library search result.

    Preserve the archive proposal and its review-only verdict on conflicting
    visual evidence. Keep photographic error separate from nominal geometry;
    a library estimate cannot silently promote a conflicting crop to attachment.
    """
    archived = accepted["candidates"][0]
    same = next((item for item in searched.get("candidates", ())
                 if item["path"] == archived["path"]), None)
    others = [item for item in searched.get("candidates", ()) if item["path"] != archived["path"]]
    # A nominal library body and a previously accepted photographic envelope
    # are different references. A smaller nominal error cannot revoke the
    # user's accepted association, already checked against the new evidence.
    merged = dict(accepted)
    if same is not None:
        archived = dict(archived, library_relative_xy_error=same["relative_xy_error"])
        if "dimensions_mm" in same:
            archived["dimensions_mm"] = same["dimensions_mm"]
    # The search's own verdict on the alternatives (auto/provisional) is
    # superseded by the archive proposal: they stay listed for review only.
    alternatives = [dict(item, status="review_required") for item in others[:max(0, limit - 1)]]
    merged["candidates"] = [archived] + alternatives
    for key in ("search_truncated", "topology_incomplete", "rejected_candidate_count"):
        if key in searched:
            merged[key] = searched[key]
    return merged


def _library_search(component: dict, result: dict, *, limit, roots, root, note, cancel) -> dict:
    """Ordinary library discovery and measurement for one component (see model_candidates)."""
    length, width = component.get("length_mm"), component.get("width_mm")
    basis = _matching_basis(component)
    package = str(component.get("package") or "").strip()
    named = list(dict.fromkeys(
        p for search_root in roots
        for p in candidates(package, search_root)
        if p.suffix.lower() in (".step", ".stp")))
    # Do not substitute another package for an explicit identifier that failed.
    fallback = not _package_token(package)
    if fallback:
        marking_paths = list(dict.fromkeys(
            path for search_root in roots
            for path in _marked_model_paths(component, search_root)))
        if marking_paths and str(component.get("kind") or component.get("detected_kind") or
                                 component.get("_detected_kind") or "").lower() in MODULE_KINDS:
            # An exact literal model identifier is a stronger discovery key
            # than a library-wide module-family scan. Keep only those models;
            # dimensions and the existing module envelope checks still apply.
            paths, omitted = marking_paths, False
            result["discovery_scope"] = "literal_marking_model"
            note(tx("Marcatura del modello letta sul componente: la ricerca KiCad si "
                    "limita ai file STEP con quel nome, ma le misure vanno comunque "
                    "verificate."))
        elif _tentative_endcap_family(component):
            result["discovery_scope"] = "standard_chip_endcaps"
            note(tx("Package chip stimato: si provano tutte le misure e varianti standard "
                    "a terminali di testa, perche' la misura indicata non e' un vincolo "
                    "esatto."))
            family_results = [_family_paths(component, search_root, basis)
                              for search_root in roots]
            paths = list(dict.fromkeys(path for family_paths, _ in family_results
                                       for path in family_paths))
            omitted = any(was_omitted for _, was_omitted in family_results)
        else:
            family_results = [_family_paths(component, search_root, basis)
                              for search_root in roots]
            paths = list(dict.fromkeys(path for family_paths, _ in family_results
                                       for path in family_paths))
            omitted = any(was_omitted for _, was_omitted in family_results)
        if omitted:
            result["search_truncated"] = True
            note(tx("Ricerca per famiglia incompleta: esclusi i modelli senza indizi di "
                    "misura compatibili, quindi niente scelta automatica."))
    else:
        paths = named
    rejections = []
    paths = _compatible_candidates(paths, component, rejections.append)
    if rejections:
        result["rejected_candidate_count"] = len(rejections)
        note(tx("{package}: scartati {n_rejections} candidati di famiglia o terminali "
                "incompatibili; ",
                package=package,
                n_rejections=len(rejections))
             + " | ".join(rejections[:3]))
    # A bare numeric package supplies no family evidence. Do not fill the UI
    # with unclassified custom solids or mechanical hardware of the same size.
    requested_family = _candidate_family(Path("Custom") / _package_token(package))
    mechanical = str(component.get("kind") or "").lower() == "mechanical"
    paths = [p for p in paths if (not re.fullmatch(r"\d+(?:metric)?", _package_name(package)) or _candidate_family(p))
             and (_candidate_family(p) != "mechanical" or mechanical)
             and (requested_family is None or _candidate_family(p) == requested_family)]
    paths = list(dict.fromkeys(paths))
    # A known observed topology cannot be checked against an IC whose package
    # count is unreadable. Withhold those suggestions rather than letting more
    # exotic vendor syntax bypass the terminal filter. This omission must also
    # disable automatic uniqueness, since the discarded models remain unknown.
    observed_counts = _observed_count(component)
    provisional_count = _provisional_terminal_count(component)
    if observed_counts or provisional_count is not None:
        unknown_topology = [p for p in paths if _candidate_family(p) == "ic"
                            and _declared_count(p) is None]
        if unknown_topology:
            result["topology_incomplete"] = True
            paths = [p for p in paths if p not in unknown_topology]
            note(tx("Esclusi {n_unknown_topology} modelli di circuito integrato con "
                    "terminali non leggibili: niente scelta automatica per unicita'.",
                    n_unknown_topology=len(unknown_topology)))
    if len(paths) > MAX_CANDIDATE_MEASUREMENTS:
        result["search_truncated"] = True
        note(tx("Troppi candidati compatibili: si misurano i primi "
                "{MAX_CANDIDATE_MEASUREMENTS} su {n_paths}.",
                MAX_CANDIDATE_MEASUREMENTS=MAX_CANDIDATE_MEASUREMENTS,
                n_paths=len(paths)))
    paths = paths[:MAX_CANDIDATE_MEASUREMENTS]
    if not paths:
        return result
    result["discovery"] = "family_dimensions" if fallback else "package"
    # Vedi `_run_freecad`: il canale si passa solo quando c'e', cosi' le
    # sostituzioni di `model_sizes` scritte prima di questo parametro
    # continuano a funzionare.
    sizes = model_sizes(paths, root, **({"cancel": cancel} if cancel is not None else {}))
    complete = not (result.get("search_truncated") or result.get("topology_incomplete"))
    lands = footprint_sizes(paths, root, log=note) if basis in ("unknown", "pads") else {}
    # L'altezza non esclude nessun candidato -- e' una stima visiva, non una
    # quota -- ma dentro la banda in cui la misura dall'alto non distingue
    # due solidi resta l'unica grandezza che li separa davvero.
    observed_height = declared_height(component)
    for path in paths:
        size = sizes.get(str(path))
        if size_distance(size, 1, 1) is None:
            complete = False
            continue
        size = dict(size) if isinstance(size, dict) else {"total": size}
        if str(path) in lands:
            size["land_patterns"] = lands[str(path)]
        if not _module_total_fits(path, size, component, length, width, DEFAULT_SIZE_TOLERANCE, note):
            continue
        readings = [(distance, label, value) for label, value in _basis_readings(size, basis)
                    if (distance := size_distance(value, length, width)) is not None]
        if not readings:
            complete = False
            continue
        distance, label, value = min(readings, key=lambda item: item[:2])
        if distance > DEFAULT_SIZE_TOLERANCE:
            continue
        dimensions = [float(v) for v in value[:2]]
        if len(value) > 2 and type(value[2]) in (int, float) and math.isfinite(value[2]) and value[2] > 0:
            dimensions.append(float(value[2]))
        result["candidates"].append({
            "path": str(path), "package": path.stem, "dimensions_mm": dimensions,
            "relative_xy_error": distance,
            "relative_z_error": height_distance(dimensions, observed_height),
            "measurement_basis": label,
            "measurement_method": size.get("body_method", "provided_body") if label == "body" else "measured_total" if label == "total" else "footprint_copper",
            "family": _candidate_family_for_component(path, component), "status": "review_required",
            "reasons": ["Family and dimension discovery" if fallback else "Package name compatible", "No declared family or terminal conflict",
                        f"Compatible {label} dimensions", "Identity and variant require user review"],
        })
    # Entro NAME_TIEBREAK_MARGIN la fotografia non distingue due solidi: una
    # frazione di percento di scarto misurato e' rumore, mentre un conteggio
    # dichiarato che coincide con quello osservato e' evidenza strutturale
    # indipendente. Ordinare per solo scarto la seppelliva -- un corpo con sei
    # reofori osservati metteva ultimo il suo unico candidato a sei terminali.
    # Fuori dalla banda comanda ancora la misura, e un conteggio che coincide
    # non promuove mai un solido che la misura ha gia' scartato.
    counts = set(_observed_count(component))
    if (provisional_count := _provisional_terminal_count(component)) is not None:
        counts.add(provisional_count)
    closest = min((item["relative_xy_error"] for item in result["candidates"]), default=0.0)

    def _rank(item):
        equivalent = item["relative_xy_error"] <= closest + NAME_TIEBREAK_MARGIN
        declared = _declared_count(Path(item["path"]))
        supported = bool(counts) and declared is not None and declared in counts
        # Dentro la banda la fotografia dall'alto ha gia' detto tutto quello
        # che sa: fra due solidi che le sembrano uguali decide l'altezza, che
        # la vista dall'alto non misura affatto. Fuori dalla banda comanda
        # ancora la misura, e un'altezza vicina non promuove mai un solido che
        # la misura ha gia' scartato.
        height_error = item.get("relative_z_error")
        # Fuori dalla banda il criterio deve essere NEUTRO, altrimenti
        # riordinerebbe candidati che la misura ha gia' separato solo perche'
        # di uno si conosce l'altezza.
        height_rank = ((height_error is None, height_error) if equivalent and height_error is not None
                       else (True, 0.0) if equivalent else (False, 0.0))
        return (not equivalent, not (equivalent and supported), height_rank,
                item["relative_xy_error"],
                _normalize(item["package"]) != _package_name(package), item["path"])

    result["candidates"].sort(key=_rank)
    automatic = _automatic_mechanical(component, result["candidates"], complete, basis,
                                      result.get("discovery_scope"))
    provisional = (None if automatic or result.get("topology_incomplete")
                   else _provisional_geometry_choice(
                       component, result["candidates"], complete, basis))
    result["candidates"] = result["candidates"][:limit]
    if result["candidates"]:
        result["status"] = "review_required"
        if automatic:
            choice = result["candidates"][0]
            choice["status"] = "auto_matched"
            if _marked_model_is_named(Path(choice["path"]), component):
                choice["reasons"][-1] = "Exact literal model marking and compatible dimensions; orientation remains reviewable"
                if result.get("discovery_scope") == "literal_marking_model":
                    result["identity_evidence"] = "literal_marking"
            else:
                choice["reasons"][-1] = "Mechanical estimate; identity and variant not verified"
            result.update(status="auto_matched", selected_path=choice["path"])
        elif provisional is not None:
            # Keep the candidate list and the warning semantics: this is a
            # proposal, not a confirmed package identity. attach_models applies
            # it PROVISIONALLY (decision A) and says so in the report, so the
            # picker can replace it.
            choice = next((item for item in result["candidates"]
                           if item["path"] == provisional["path"]), None)
            if choice is not None:
                choice["status"] = "provisional_matched"
                choice["reasons"][-1] = (
                    "Applied provisionally: identity not verified; package variant and "
                    "orientation require user review and the picker can replace it")
                if provisional.get("tie_alternatives"):
                    # Scelta per vicinanza fra taglie quasi alla pari: va detto.
                    result["size_tie_alternatives"] = list(provisional["tie_alternatives"])
                    choice["reasons"].append(
                        tx("Taglia piu' vicina alla misura fra taglie quasi alla pari "
                           "({alternatives}): verificala.",
                           alternatives=", ".join(provisional["tie_alternatives"])))
                result.update(status="provisional_matched", selected_path=choice["path"])
        note(f"{package}: {len(result['candidates'])} dimensional candidates; {result['status']}; identity not verified.")
    return result


def attach_models(components, log=None, library: Optional[Path] = None, progress=None,
                  *, cancel=None) -> int:
    """@brief Attach explicit choices or supported mechanical estimates, clearing stale paths.

    @param components Mutable component dictionaries containing model_selection.
    @param log Optional diagnostic callback.
    @param library Optional library root for API compatibility.
    @param progress Optional callback(done, total) called once per component.
        This is the one step of a generation with a real total: every component
        may cost a CAD measurement, so the count is what a progress bar can
        honestly be built on.
    @param cancel Canale di rinuncia: un oggetto con `is_set()` (tipicamente
        `threading.Event`) che chi ha avviato il lavoro mette a uno per
        chiedere la fine. **None significa non interrompibile** ed e' il
        valore predefinito: senza canale questa funzione fa esattamente quel
        che faceva prima. Il canale viene guardato PRIMA di ogni componente e
        inoltrato fino al figlio `freecadcmd` che misura i candidati, cosi'
        una richiesta non aspetta la fine del lotto in corso.
    @return Number of paths attached: explicit choices, automatic mechanical
        estimates and provisional proposals alike.
    @throws MatchingCancelled Quando la rinuncia arriva: il conteggio parziale
        NON viene restituito (sarebbe letto come il totale di una corsa
        completa) e il componente su cui la richiesta e' caduta viene
        rimesso esattamente com'era. I componenti gia' esaminati conservano il
        loro verdetto, che e' vero; quelli mai esaminati non ne ricevono
        nessuno -- un `model_match` «unresolved» scritto d'ufficio direbbe
        «nessun modello compatibile» di un pezzo che nessuno ha guardato.
    @note Rejected or invalid explicit selections use synthetic geometry. Missing
        selections permit auto_matched mechanical estimates when evidence supports
        them. A provisional_matched report is a proposal whose identity is not
        verified, yet it IS applied (user decision A): model_path receives
        model_match.selected_path so the solid previews the compatible library
        model instead of a generic body, and the report says so with
        model_match.attachment == "provisional" plus attachment_reason, so the
        picker can replace it. A library_matched report (accepted-matches
        archive) is applied the same way with attachment "accepted_library";
        its stored manual angle, if any, becomes model_rotation_deg.
        Rotation is copied to model_rotation_deg; None or an omitted selection
        rotation clears that override for automatic native-axis inference.
        Explicit zero is retained. Unconfirmed native placement metadata and
        component photo rotation are preserved. The JSON report is stored in
        model_match. Every component also receives model_review (the report
        status at match time) and render_placeholder: only an unresolved
        component, for which nothing beyond its rectangle is known, is painted
        the magenta placeholder colour. review_required and provisional_matched
        bodies keep the colour sampled from the photograph or supplied by the
        detector; the review state is expressed by explicit fields, never by
        the colour. The magenta of a failed STEP import is decided later by the
        FreeCAD builder.
    @note Single source of truth for consumers: `model_match.status`.
        `model_review` and `render_placeholder` are this matcher's own record
        of the state it produced, written once here and not maintained by
        later stages: the STEP build may rewrite `model_match.status` to
        `import_failed` (step_generator reconciles the import log into the
        report) without touching them, so a reader that needs the current
        state must read `model_match.status` and treat `model_review` as
        "what the matcher decided", not "what the component is now". They are
        kept because review tooling and tests read them as the matcher's
        verdict; they must not grow new consumers that need liveness.
    """
    note = log or (lambda message: None)
    resolved = 0
    pending = list(components or [])
    total = len(pending)
    archive_results = _archive_prepass(pending, library, note, cancel)
    for done, component in enumerate(pending, 1):
        if progress is not None:
            progress(done - 1, total)
        if not isinstance(component, dict):
            continue
        # Guardato PRIMA di toccare il componente: cosi' il pezzo su cui la
        # rinuncia cade resta intatto invece di restare a meta' strada, senza
        # modello agganciato e con il verdetto della corsa precedente.
        if cancel is not None and cancel.is_set():
            note(tx("Abbinamento modelli KiCad interrotto su richiesta dopo {value} "
                    "componenti su {total}.",
                    value=done - 1,
                    total=total))
            raise MatchingCancelled(measured={})
        # Il ripristino a True ripulisce lo stato calcolato nella corsa
        # precedente, non la scelta dell'utente: render_3d_override e' l'unico
        # canale esplicito e vince in entrambe le direzioni, sia per togliere
        # dal solido un falso positivo del rilevatore sia per rimettere un
        # pezzo che l'automatismo aveva omesso.
        override = component.get("render_3d_override")
        # Cio' che sto per sovrascrivere, per poterlo rimettere se la rinuncia
        # arriva a meta' esame: un componente mai esaminato fino in fondo deve
        # restare come l'ho trovato, non a meta'.
        restore = {key: component[key] for key in ("render_3d", "model_path")
                   if key in component}
        previous_model_rotation = component.get("model_rotation_deg")
        if previous_model_rotation is None:
            previous_report = component.get("model_match")
            if not isinstance(previous_report, dict):
                previous_report = {}
            rendered_angle = previous_report.get("rendered_local_rotation_deg")
            rendered_delta = previous_report.get("rendered_manual_delta_deg")
            if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in (rendered_angle, rendered_delta)):
                previous_model_rotation = rendered_angle - rendered_delta
        component["render_3d"] = True if override is None else bool(override)
        component.pop("model_path", None)
        try:
            with _cached_archive_results(archive_results):
                report = model_candidates(component, library=library, log=note,
                                          **({"cancel": cancel} if cancel is not None else {}))
        except MatchingCancelled:
            component.pop("render_3d", None)
            component.pop("model_path", None)
            component.update(restore)
            note(tx("Abbinamento modelli KiCad interrotto su richiesta durante il "
                    "componente {done} di {total}.",
                    done=done,
                    total=total))
            raise
        component["model_match"] = report
        report['consolidated_match'] = component.get('consolidated_match', {})
        # Lo stato di revisione si dice con un campo, non con un colore: il
        # magenta resta solo per chi non ha alcun candidato, perche' di quel
        # corpo non si sa nulla oltre al riquadro. Un corpo in revisione o
        # con una proposta provvisoria ha una geometria XY nota e un colore
        # campionato dalla foto, e cancellarlo con il magenta buttava via
        # un'informazione vera per segnalarne una che il report gia' porta.
        # model_review e' il verdetto del matcher al momento dell'abbinamento;
        # lo stato corrente e' model_match.status (vedi @note nel docstring).
        component["model_review"] = report["status"]
        component["render_placeholder"] = report["status"] == "unresolved"
        report["render_placeholder"] = component["render_placeholder"]
        explicit = _explicit_selection(component) if report["status"] == "confirmed" else None
        if explicit is not None:
            path, rotation = explicit
            component["model_path"] = str(path)
            report["attachment"] = "explicit"
            if rotation is None:
                component.pop("model_rotation_deg", None)
            else:
                component["model_rotation_deg"] = rotation
            resolved += 1
        elif (report["status"] in ("auto_matched", "provisional_matched", "library_matched")
              and report.get("selected_path")):
            # Decisione dell'utente (A): anche la proposta provvisoria aggancia
            # il modello, ma lo dichiara. Lasciare il corpo generico per un
            # candidato che la misura non sa distinguere dalle sue varianti
            # buttava via un solido compatibile (modulo, USB-C, QFN uscivano
            # come parallelepipedi) per segnalare un dubbio che il report
            # porta gia' in un campo. L'identita' resta NON verificata:
            # model_review e attachment lo dicono, e il selettore puo'
            # sostituire il modello.
            component["model_path"] = report["selected_path"]
            if report["status"] == "provisional_matched":
                report["attachment"] = "provisional"
                report["attachment_reason"] = (
                    "Model applied PROVISIONALLY: identity not verified, the photograph "
                    "cannot separate it from its variants; the picker can change it.")
            elif report["status"] == "library_matched":
                # Scelta dell'utente: «preseleziona, tu confermi». Il modello
                # viene da un abbinamento che hai gia' accettato su un'altra
                # scheda con la stessa firma geometrica; resta una proposta.
                report["attachment"] = "accepted_library"
                report["attachment_reason"] = (
                    tx("Modello dall'archivio degli abbinamenti accettati: stessa firma "
                       "geometrica di una scelta gia' approvata; da confermare nel selettore."))
            else:
                report["attachment"] = "automatic"
            # Il verso del modello nativo non si decide qui. Fino a poco fa lo
            # decideva una regex inglese su prosa libera, ancorata al nome del
            # file e valida di fatto per un modulo solo: una regola sui nomi,
            # che questo progetto vieta. Ora lo ricava il costruttore
            # confrontando dove pende il materiale del modello (baricentro
            # contro centro dell'ingombro) con il lato su cui la fotografia ha
            # visto il riferimento asimmetrico. Se uno dei due lati manca, il
            # verso resta non verificato invece di essere indovinato.
            #
            # L'angolo assoluto di una corsa precedente e' stato calcolato su
            # un altro modello e va comunque tolto.
            component.pop("model_rotation_deg", None)
            component.pop("model_rotation_native_delta_deg", None)
            # A row-wide angle may mix different physical instances. Only an
            # individually verified crop with an unambiguous relative angle transfers.
            orientation = report.get("accepted_orientation") or {}
            accepted_rotation = orientation.get("rotation_deg")
            if (report["status"] == "library_matched"
                    and orientation.get("status") == "matched"
                    and isinstance(accepted_rotation, (int, float))
                    and not isinstance(accepted_rotation, bool)
                    and math.isfinite(accepted_rotation)):
                manual_delta = component.get("model_rotation_delta_deg")
                preserve_manual_base = (
                    restore.get("model_path") == report["selected_path"]
                    and isinstance(previous_model_rotation, (int, float))
                    and not isinstance(previous_model_rotation, bool)
                    and math.isfinite(previous_model_rotation)
                    and isinstance(manual_delta, (int, float))
                    and not isinstance(manual_delta, bool)
                    and math.isfinite(manual_delta) and manual_delta != 0)
                # An archived example can already include this component's
                # context-menu correction. Replacing its base while retaining
                # the correction would apply that delta a second time. Keep
                # the existing base for a manually adjusted, unchanged model.
                component["model_rotation_deg"] = float(
                    previous_model_rotation if preserve_manual_base else accepted_rotation)
                if preserve_manual_base:
                    report["orientation_manual_delta_preserved"] = True
                    note(f"{component.get('name', '')}: orientamento manuale mantenuto "
                         f"sullo stesso modello: base {previous_model_rotation:g} gradi, "
                         f"correzione {manual_delta:g} gradi.")
                else:
                    note(f"{component.get('name', '')}: orientamento da ritaglio consolidato "
                         f"#{orientation.get('image_id')}: {accepted_rotation:g} gradi; "
                         f"rotazione relativa {orientation.get('relative_rotation_deg', 0):g} gradi.")
            elif report.get("discovery") == "accepted_library":
                report["orientation_unverified"] = "Orientamento da verificare: nessun ritaglio con angolo individuale e confronto univoco."
            if component.get("landmark_side"):
                report["orientation_landmark"] = {
                    "feature": component.get("landmark_feature"),
                    "side": component["landmark_side"],
                    "confidence": component.get("landmark_confidence")}
            elif orientation.get("status") != "matched" and not report.get("orientation_unverified"):
                report["orientation_unverified"] = (
                    "No asymmetric landmark observed: an outline alone cannot tell "
                    "one end from the other, so the model keeps its inferred axis.")
            resolved += 1
        else:
            if report["status"] == "unresolved":
                component["color_hex"] = "#FF00FF"
                component["_color_source"] = "placeholder_unresolved"
            if _is_yolo_only_hypothesis(component) and override is not True:
                # Keep detector-only hypotheses in the JSON/review evidence,
                # but never turn an unclassified proposal into a CAD body
                # which looks like a real component, whatever its colour.
                component["render_3d"] = False
                report["render_3d"] = False
                note(tx("Solo ipotesi del rilevatore per {get}: tenuta per la revisione, "
                        "esclusa dal 3D.",
                        get=component.get('name')))
    _synchronize_preview_variants(components, note)
    if progress is not None:
        progress(total, total)
    archived = [c for c in pending if isinstance(c, dict)
                and (c.get("model_match") or {}).get("attachment") == "accepted_library"]
    orientations = sum((c["model_match"].get("accepted_orientation") or {}).get("status") == "matched"
                       and not c["model_match"].get("orientation_manual_delta_preserved") for c in archived)
    note(tx("Consolidati: {count} modelli scelti su {total} componenti; "
            "{orientations} orientamenti ricavati dai ritagli con OpenCV. "
            "Gli altri orientamenti restano automatici, manuali o da verificare.",
            count=len(archived), total=total, orientations=orientations))
    note(tx("Modelli KiCad agganciati (scelte esplicite o compatibilita' "
            "meccanica): {resolved}.",
            resolved=resolved))
    return resolved
