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
from pathlib import Path
from functools import lru_cache
from typing import Iterable, Optional

from .model_measurement import normalize_model_measurement
from . import model_size_cache

#: Variabile d'ambiente per indicare una libreria diversa da quella trovata.
LIBRARY_ENV = "KICAD_3DMODEL_DIR"

#: Estensioni dei modelli, in ordine di preferenza: lo STEP e' geometria
#: solida ed e' cio' che serve per un componente montato; il VRML e' solo
#: una mesh per la visualizzazione.
MODEL_SUFFIXES = (".step", ".stp", ".wrl")

#: Scarto relativo massimo fra l'ingombro del modello e quello misurato sulla
#: fotografia.
#:
#: Il valore e' derivato, non tarato su un caso. Fra due taglie standard
#: adiacenti corre almeno il 60% (0402 e' 1.0x0.5, 0603 e' 1.6x0.8), e la
#: scelta avviene comunque per vicinanza: perche' il modello sbagliato risulti
#: piu' vicino di quello giusto servirebbe un errore di misura superiore al
#: 30%. Questa soglia e' quindi il limite oltre il quale la misura non
#: conferma piu' nulla, non il criterio di scelta.
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


def _candidate_roots() -> Iterable[Path]:
    """Percorsi in cui cercare la libreria, dal piu' esplicito al piu' generico."""
    override = os.environ.get(LIBRARY_ENV)
    if override:
        yield Path(override)
    for base in (r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad"):
        root = Path(base)
        if root.is_dir():
            # Versioni piu' recenti per prime: 10.0 prima di 9.0.
            for version in sorted(root.iterdir(), reverse=True):
                yield version / "share" / "kicad" / "3dmodels"
    # KiCad mantiene le librerie 3rd-party installate dall'utente fuori da
    # Program Files. Ogni repository puo' avere una propria cartella
    # ``*.3dshapes`` (es. ``espressif.3dshapes``), quindi si espongono i
    # repository come radici indipendenti e si evita di assumere un vendor.
    user_kicad = Path.home() / "Documents" / "KiCad"
    if user_kicad.is_dir():
        for version in sorted(user_kicad.iterdir(), reverse=True):
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


def _marking_model_tokens(component: dict) -> tuple[str, ...]:
    """Extract conservative multi-part model identifiers from literal marking."""
    marking = str(component.get("marking") or "")
    # A model identifier such as ESP32-S3-WROOM-1U is useful only as a literal
    # filename hint. Require at least two separators so FCC/IC identifiers and
    # arbitrary single words do not become packages by accident.
    matches = re.findall(r"(?<![A-Z0-9])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})(?![A-Z0-9])",
                         marking.upper())
    unique = {}
    for value in matches:
        unique[_normalize(value)] = value
    return tuple(unique.values())


def _marked_model_matches(path: Path, component: dict) -> bool:
    """Whether a model filename contains an exact literal identifier from marking."""
    stem = _normalize(path.stem)
    return any(stem == _normalize(token) or _normalize(token) in stem
               for token in _marking_model_tokens(component))


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
    cache = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "kicadgen"
    return cache / INDEX_FILENAME


def _index_paths(library):
    """Try actual cache writes, not just os.access, before falling back to user data."""
    user_cache = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "kicadgen" / INDEX_FILENAME
    return list(dict.fromkeys((_index_path(library), library / INDEX_FILENAME, user_cache)))


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
            log("Footprint KiCad non disponibili: confronto solo corpo/reofori.")
        return result
    wanted = {Path(p).resolve(): str(p) for p in paths}
    names = {p.name.casefold() for p in wanted}
    rejected = 0
    try:
        for footprint in sorted(root.rglob("*.kicad_mod")):
            stamp = index_key(footprint)
            hints, variable = _link_hints(footprint, stamp)
            if not variable and not names.intersection(hints):
                continue
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
        log(f"Footprint KiCad: {sum(map(len, result.values()))} letture collegate; "
            f"{rejected} footprint pertinenti senza ingombro utilizzabile.")
    return result


#: Script eseguito dentro FreeCAD: misura l'ingombro di piu' modelli in una
#: sola invocazione. Aprire un processo per file costerebbe secondi a pezzo.
#: I percorsi passano da un file di scambio e non da argv, perche' freecadcmd
#: non inoltra gli argomenti allo script come farebbe python.
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
for path in paths:
    try:
        sizes[path] = measure(path)
    except Exception:
        sizes[path] = None
with open(os.path.join(folder, "out.json"), "w", encoding="utf-8") as handle:
    json.dump(sizes, handle)
"""


def measure_models(paths, timeout: int = 300) -> dict:
    """@brief Measure model body and total X/Y/Z dimensions in mm using FreeCAD.

    Una sola invocazione per l'intero lotto: aprire un processo per file
    costerebbe secondi a pezzo. I modelli illeggibili finiscono a ``None``
    invece di far fallire il lotto -- un file rotto nella libreria non deve
    impedire di risolvere gli altri.

    Solo STEP: il VRML e' una mesh per la visualizzazione e FreeCAD non lo
    importa come solido.
    """
    import subprocess
    import tempfile

    readable = [str(p) for p in paths if Path(p).suffix.lower() in (".step", ".stp")]
    if not readable:
        return {}
    try:
        from .step_generator import find_freecadcmd
        freecad = find_freecadcmd()
    except Exception:  # noqa: BLE001 - senza FreeCAD si resta senza misure
        return {}
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        (base / "in.json").write_text(json.dumps(readable), encoding="utf-8")
        script = base / "measure.py"
        script.write_text(_MEASURE_SCRIPT, encoding="utf-8")
        environment = dict(os.environ, KICADGEN_MEASURE_DIR=str(base))
        try:
            subprocess.run([freecad, str(script)], capture_output=True, text=True,
                           timeout=timeout, env=environment)
            return json.loads((base / "out.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}


def model_sizes(paths, library: Optional[Path] = None) -> dict:
    """@brief Retrieve valid cached dimensions and retry missing/failed measurements."""
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
        measured = measure_models(missing)
        for path in missing:
            value = measured.get(str(path))
            # A missing FreeCAD installation or failed batch must be retryable.
            if size_distance(value, 1, 1) is not None:
                index[index_key(path)] = normalize_model_measurement(path, value)
        save_index(index, library)
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
    if kind in ("module", "rf_module", "pcb_module") and _marked_model_matches(path, component):
        return "module"
    return None


def _observed_count(component):
    """Use confident explicit observations only, never a count guessed from kind."""
    counts = set()
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
    """Only count-bearing KiCad package syntax: SOT-23 does NOT mean 23 pins."""
    # SOT's first number identifies the package; only a separate variant suffix
    # declares terminals. Bare SOT packages deliberately remain uncounted.
    sot = re.match(r"(?:SOT|SC)[-_]?\d+[-_]([1-9]\d*)(?=[_]|$)", path.stem, re.IGNORECASE)
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


def _compatible_candidates(paths, component, note):
    """Reject explicit type/topology contradictions before comparing dimensions."""
    if not component:
        return paths
    aliases = {"ic_soic": "ic", "ic_qfn": "ic", "ic_qfp": "ic", "ic_bga": "ic",
               "tantalum": "capacitor", "diode_melf": "diode",
               "rf_module": "module", "pcb_module": "module",
               "semiconductor_unknown": "semiconductor", "transistor": "semiconductor"}
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
            reason = f"tipo {','.join(sorted(kinds))} incompatibile con famiglia {family}"
        stem = path.stem.upper()
        is_so = path.parent.name.lower() == "package_so.3dshapes" or bool(
            re.match(r"(?:SOIC|SSOP|TSSOP|MSOP|TSOP|SOP)[-_]", stem))
        is_qfn = bool(re.match(r"(?:QFN|DFN)[-_]", stem))
        if ("ic_qfn" in kinds and is_so) or ("ic_soic" in kinds and is_qfn):
            reason = "topologia QFN/DFN e SO incompatibile con il tipo osservato"
        declared = _declared_count(path)
        if len(counts) > 1:
            reason = "conteggi terminali osservati in conflitto"
        elif counts and declared is not None and declared not in counts:
            reason = f"terminali osservati {next(iter(counts))}, package dichiara {declared}"
        if reason:
            evidence = "tipo verificato" if verified else "tipo provvisorio/non verificato"
            if provisional is not None:
                evidence += "; observed terminal layout/count is provisional"
            note(f"{component.get('package', '')}: rifiutato {path.name}: {reason} ({evidence}); "
                 "il solo ingombro non conferma il package.")
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
    is_module = path.parent.name.lower() == "rf_module.3dshapes" or any(
        str(component.get(key) or "").lower() in ("module", "rf_module", "pcb_module")
        for key in ("kind", "detected_kind", "_detected_kind"))
    if not is_module:
        return True
    total = size.get("total") if isinstance(size, dict) else size
    if size_distance(total, length_mm, width_mm) is None:
        note(f"{path.name}: modulo rifiutato, ingombro totale non disponibile; "
             "schermatura/piazzole non verificano il PCB completo.")
        return False
    model_axes = sorted(map(float, total[:2]))
    observed_axes = sorted((float(length_mm), float(width_mm)))
    excess = max((m - observed) / observed for m, observed in zip(model_axes, observed_axes))
    if excess > tolerance:
        note(f"{path.name}: modulo rifiutato, ingombro totale "
             f"{float(total[0]):.2f} x {float(total[1]):.2f} mm supera il rettangolo osservato "
             f"del {excess*100:.1f}% (limite {tolerance*100:.0f}%); "
             "schermatura/piazzole non possono nascondere estensioni del PCB.")
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
        note(f"{package}: nessun modello nella libreria KiCad.")
        return None
    # All solid candidates participate. WRL files are display meshes and must
    # neither consume a candidate budget nor be selected for Part.read.
    shortlist = [p for p in found if p.suffix.lower() in (".step", ".stp")]
    shortlist = _compatible_candidates(shortlist, _component, note)
    if not shortlist:
        note(f"{package}: nessun solido compatibile con le evidenze di tipo/terminali; "
             "si usa la forma approssimata.")
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
        detail = (f"; il piu' vicino sbaglia del {closest*100:.0f}%" if closest is not None
                  else "; nessun solido con misure utilizzabili dopo i controlli")
        note(f"{package}: {len(found)} modelli trovati, nessuno compatibile con "
             f"{length_mm:.2f} x {width_mm:.2f} mm{detail}; si usa la forma approssimata.")
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
        note(f"{package}: topologia contatti sconosciuta; candidati equivalenti dichiarano "
             f"conteggi diversi ({', '.join(map(str, sorted(contact_counts)))}), "
             "il package proposto non specifica il conteggio; si usa la forma approssimata.")
        return None
    scored.sort(key=lambda item: (item[0] > best + NAME_TIEBREAK_MARGIN,
                                  _normalize(item[2].stem) != wanted,
                                  len(item[2].stem), item[1]))
    distance, _, path = scored[0]
    equivalent = sum(d <= best + NAME_TIEBREAK_MARGIN for d, _, _ in scored)
    if equivalent > 1:
        note(f"{package}: {equivalent} modelli con ingombri comparabili; "
             "la misura non distingue le varianti, scelta per nome non identificativa.")
    evidence = min(((d, label) for label, value in _readings(sizes[str(path)])
                    if (d := size_distance(value, length_mm, width_mm)) is not None))
    note(f"{package}: modello reale {path.name}, {describe_size(sizes[str(path)])} "
         f"contro misura {length_mm:.2f} x {width_mm:.2f} mm, scarto {distance*100:.0f}%; "
         f"lettura: {evidence[1]}. Compatibilità dimensionale, identità/variante non verificata.")
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
    aliases = {"ic_soic": "ic", "ic_qfn": "ic", "ic_qfp": "ic", "ic_bga": "ic",
               "rf_module": "module", "pcb_module": "module", "tantalum": "capacitor",
               "diode_melf": "diode", "semiconductor_unknown": "semiconductor",
               "transistor": "semiconductor", "regulator": "semiconductor"}
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


def _automatic_mechanical(component, choices, complete, basis):
    """Require independent presence, family, terminal and envelope support.

    Uniqueness is evaluated before UI truncation, never inferred from a budgeted
    or unreadable search. This conservative estimate does not certify identity.
    """
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
                "connector", "switch", "module", "rf_module", "pcb_module", "crystal", "transistor")
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
    if (declared is None and choices[0]["family"] == "module"
            and _marked_model_matches(path, component)):
        return True
    return declared is not None and counts == {declared} and choices[0]["family"] is not None


def _provisional_geometry_choice(component, choices, complete, basis):
    """Return a safe preview candidate without claiming package identity.

    The photo often cannot distinguish variants which have the same XY
    footprint (for example two tactile switches differing only in height, or
    two QFN models with the same outline).  Leaving those candidates entirely
    synthetic made the 3D preview useless even though KiCad had a compatible
    solid.  This path therefore attaches only a *provisional* model: the
    report remains reviewable and the UI keeps the candidate picker visible.

    A single candidate is accepted only for a literal manufacturer/model
    marking.  Otherwise the first two candidates must be from the same KiCad
    family and have effectively the same XY envelope.  Truncated searches are
    allowed only under that tie condition; a distant or topology-incomplete
    search is never promoted to a preview.
    """
    if (not choices or basis not in ("body", "total")
            or component.get("presence_confidence") != "high"
            or component.get("_yolo_review_only") is True
            or component.get("_yolo_origin") == "low_confidence_review"
            or component.get("_global_inventory_status") in ("invalid", "rejected")):
        return None
    morphology = " ".join(str(component.get(key) or "")
                           for key in ("terminal_layout", "package_evidence", "package"))
    three_lead_tab = bool(re.search(
        r"\b(?:three|3)\s+(?:wide\s+)?(?:gull[- ]wing\s+)?leads?\b"
        r".*\b(?:one|1)\s+(?:large\s+)?(?:rectangular\s+)?tab\b",
        morphology, re.I | re.S,
    )) and not bool(re.search(
        r"\b(?:unknown|unresolved|uncertain|unclear|maybe|possibly|not visible|hidden)\b",
        morphology, re.I,
    ))
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
                    "connector", "switch", "module", "rf_module", "pcb_module"):
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
    if morphology_exception:
        # SOT-223 is the only common KiCad semiconductor preview whose
        # mechanical evidence is three leads plus a large tab.  Do not accept
        # a distant second family alternative as if it were equivalent.
        if (first.get("family") == "semiconductor"
                and best_error <= 0.12
                and re.search(r"(?:^|[-_])SOT[-_]?223(?:\.|$)",
                              Path(first["path"]).name, re.I)):
            return first
    if len(choices) < 2 or component.get("topology_incomplete"):
        return None
    second = choices[1]
    if first.get("family") != second.get("family"):
        return None
    a, b = first.get("dimensions_mm"), second.get("dimensions_mm")
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
            and len(a) >= 2 and len(b) >= 2):
        return None
    try:
        a = sorted(float(v) for v in a[:2])
        b = sorted(float(v) for v in b[:2])
        tie_error = max(abs(x - y) / max(x, y) for x, y in zip(a, b))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not all(math.isfinite(v) and v > 0 for v in (*a, *b)):
        return None
    if tie_error > NAME_TIEBREAK_MARGIN:
        return None
    return first


def _native_model_rotation_override(component: dict, path: Path) -> Optional[float]:
    """Return an explicit local STEP rotation when the observed feature fixes it.

    The WROOM-1U STEP has a meaningful asymmetric RF connector.  XY dimensions
    can infer its long axis but cannot decide between 0 and 180 degrees.  Only
    an exact literal model marking plus an affirmative observation of the RF
    jack on the lower/right side is strong enough to apply this local CAD
    correction.  It never changes the component's photographic rotation.
    """
    if not _marked_model_matches(path, component):
        return None
    if str(component.get("kind") or component.get("detected_kind") or "").lower() not in (
            "module", "rf_module", "pcb_module"):
        return None
    text = " ".join(str(component.get(key) or "") for key in (
        "terminal_layout", "package_evidence", "package"))
    # Vision models commonly emit either "lower right" or "lower-right".
    # Both describe the same asymmetric RF-jack landmark.
    if not re.search(r"\b(?:lower|bottom|inferior)(?:[-\s]+)right\b", text, re.I):
        return None
    if not re.search(r"\b(?:rf|u\.fl|ipex|coax|antenna)\b", text, re.I):
        return None
    return 180.0


def _is_yolo_only_hypothesis(component: dict) -> bool:
    """Identify detector-only rows which are useful for review, not for 3D."""
    name = str(component.get("name") or "").strip().lower()
    return name.startswith("yolo candidate") and not (
        isinstance(component.get("model_selection"), dict)
        and component["model_selection"].get("status") == "confirmed"
    )


def _switch_group_key(component: dict) -> str:
    """Normalize descriptive switch prose without using measured dimensions."""
    text = str(component.get("package") or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _synchronize_preview_variants(components, note) -> None:
    """Use one common preview variant for visually same-family switches.

    Independent dimensions of cropped buttons often differ by a few tenths of
    a millimetre.  If their review lists share a compatible native model, using
    that common candidate avoids rendering two visibly different switches,
    while keeping the choice explicitly provisional.
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
            component["model_path"] = selected
            component.pop("model_rotation_deg", None)
        note(f"Switch preview group '{key}': shared candidate {Path(selected).name} for {len(group)} components.")


def model_candidates(component: dict, *, limit=5, library=None, log=None) -> dict:
    """@brief Propose bounded, JSON-safe model choices for explicit human review.

    @param component Component evidence, dimensions, bbox_basis and model_selection.
    @param limit Maximum result count, clamped to zero through five.
    @param library Optional KiCad model root; cached measurements are reused.
    @param log Optional diagnostic callback accepting a string.
    @return Mapping with status, candidates and selected_path. Candidate dimensions_mm
        is the matched XY reading (and Z when available), in native model axes.
    @note Dimensional evidence never confirms identity. Explicit confirmation wins;
        independently supported unique estimates use auto_matched instead;
        same-footprint alternatives may use provisional_matched for a real 3D
        preview while keeping identity and variant reviewable.
        Rejection stops discovery. Missing/descriptive packages use family discovery.
        Unknown basis compares independent readings; body/total/pads are strict.
        leads and body_and_leads mean total. At most 64 structurally compatible
        paths are measured; search_truncated flags omitted names, including cold
        family models without compatible dimensional hints, not a full search.
    """
    result = {"status": "unresolved", "candidates": [], "selected_path": None}
    note = log or (lambda message: None)
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
    length, width = component.get("length_mm"), component.get("width_mm")
    if size_distance([length, width], length, width) is None:
        return result
    basis = component.get("bbox_basis") or "unknown"
    basis = {"leads": "total", "body_and_leads": "total"}.get(str(basis), basis)
    if basis not in ("unknown", "body", "total", "pads"):
        return result
    package = str(component.get("package") or "").strip()
    roots = _search_roots(library)
    root = roots[0] if roots else None
    if root is None:
        note("Libreria 3D di KiCad non trovata: si usano le forme approssimate.")
        return result
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
                                 component.get("_detected_kind") or "").lower() in (
                                     "module", "rf_module", "pcb_module"):
            # An exact literal model identifier is a stronger discovery key
            # than a library-wide module-family scan. Keep only those models;
            # dimensions and the existing module envelope checks still apply.
            paths, omitted = marking_paths, False
            result["discovery_scope"] = "literal_marking_model"
            note("Literal model marking: restricting KiCad search to matching STEP filenames; dimensions still require verification.")
        elif _tentative_endcap_family(component):
            result["discovery_scope"] = "standard_chip_endcaps"
            note("Estimated chip package: checking all standard endcap sizes and variants; nominated size is not an exact constraint.")
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
            note("Family discovery omitted cold models without compatible dimension hints; automatic selection disabled.")
    else:
        paths = named
    rejections = []
    paths = _compatible_candidates(paths, component, rejections.append)
    if rejections:
        result["rejected_candidate_count"] = len(rejections)
        note(f"{package}: rejected {len(rejections)} family/terminal-incompatible candidates; "
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
            note(f"Withheld {len(unknown_topology)} IC models with unreadable terminal topology; automatic uniqueness disabled.")
    if len(paths) > MAX_CANDIDATE_MEASUREMENTS:
        result["search_truncated"] = True
        note(f"Candidate measurement budget: first {MAX_CANDIDATE_MEASUREMENTS} of {len(paths)} compatible names.")
    paths = paths[:MAX_CANDIDATE_MEASUREMENTS]
    if not paths:
        return result
    result["discovery"] = "family_dimensions" if fallback else "package"
    sizes = model_sizes(paths, root)
    complete = not (result.get("search_truncated") or result.get("topology_incomplete"))
    lands = footprint_sizes(paths, root, log=note) if basis in ("unknown", "pads") else {}
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
            "relative_xy_error": distance, "measurement_basis": label,
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
        return (not equivalent, not (equivalent and supported), item["relative_xy_error"],
                _normalize(item["package"]) != _package_name(package), item["path"])

    result["candidates"].sort(key=_rank)
    automatic = _automatic_mechanical(component, result["candidates"], complete, basis)
    provisional = (None if automatic or result.get("topology_incomplete")
                   else _provisional_geometry_choice(
                       component, result["candidates"], complete, basis))
    result["candidates"] = result["candidates"][:limit]
    if result["candidates"]:
        result["status"] = "review_required"
        if automatic:
            choice = result["candidates"][0]
            choice["status"] = "auto_matched"
            if _marked_model_matches(Path(choice["path"]), component):
                choice["reasons"][-1] = "Exact literal model marking and compatible dimensions; orientation remains reviewable"
            else:
                choice["reasons"][-1] = "Mechanical estimate; identity and variant not verified"
            result.update(status="auto_matched", selected_path=choice["path"])
        elif provisional is not None:
            # Keep the candidate list and the warning semantics: this is a
            # rendering aid, not a confirmed package identity.
            choice = next((item for item in result["candidates"]
                           if item["path"] == provisional["path"]), None)
            if choice is not None:
                choice["status"] = "provisional_matched"
                choice["reasons"][-1] = (
                    "Preview geometry only; package variant and orientation require user review")
                result.update(status="provisional_matched", selected_path=choice["path"])
        note(f"{package}: {len(result['candidates'])} dimensional candidates; {result['status']}; identity not verified.")
    return result


def attach_models(components, log=None, library: Optional[Path] = None) -> int:
    """@brief Attach explicit choices or supported mechanical estimates, clearing stale paths.

    @param components Mutable component dictionaries containing model_selection.
    @param log Optional diagnostic callback.
    @param library Optional library root for API compatibility.
    @return Number of explicit or inferred mechanical paths attached.
    @note Rejected or invalid explicit selections use synthetic geometry. Missing
        selections permit auto_matched mechanical estimates or provisional_matched
        same-footprint previews when evidence supports them. Rotation
        is copied to model_rotation_deg; None or an omitted selection rotation clears
        that override for automatic native-axis inference. Explicit zero is retained.
        Unconfirmed native placement metadata and component photo rotation are preserved.
        The JSON report is stored in model_match; unresolved/review placeholders are magenta.
    """
    note = log or (lambda message: None)
    resolved = 0
    for component in components or []:
        if not isinstance(component, dict):
            continue
        # Il ripristino a True ripulisce lo stato calcolato nella corsa
        # precedente, non la scelta dell'utente: render_3d_override e' l'unico
        # canale esplicito e vince in entrambe le direzioni, sia per togliere
        # dal solido un falso positivo del rilevatore sia per rimettere un
        # pezzo che l'automatismo aveva omesso.
        override = component.get("render_3d_override")
        component["render_3d"] = True if override is None else bool(override)
        component.pop("model_path", None)
        report = model_candidates(component, library=library, log=note)
        component["model_match"] = report
        explicit = _explicit_selection(component) if report["status"] == "confirmed" else None
        if explicit is not None:
            path, rotation = explicit
            component["model_path"] = str(path)
            if rotation is None:
                component.pop("model_rotation_deg", None)
            else:
                component["model_rotation_deg"] = rotation
            resolved += 1
        elif report["status"] in ("auto_matched", "provisional_matched"):
            component["model_path"] = report["selected_path"]
            override = _native_model_rotation_override(
                component, Path(report["selected_path"] or ""))
            if override is None:
                component.pop("model_rotation_deg", None)
            else:
                component["model_rotation_deg"] = override
                report["native_model_rotation_deg"] = override
                report["native_orientation_note"] = (
                    "Native STEP rotated 180° to place the observed RF connector "
                    "on the lower-right side; photo rotation is unchanged")
            resolved += 1
        else:
            component["color_hex"] = "#FF00FF"
            if _is_yolo_only_hypothesis(component) and override is not True:
                # Keep detector-only hypotheses in the JSON/review evidence,
                # but never turn an unclassified proposal into a misleading
                # magenta CAD body which looks like a real component.
                component["render_3d"] = False
                report["render_3d"] = False
                note(f"YOLO-only hypothesis {component.get('name')}: kept for review, omitted from 3D.")
    _synchronize_preview_variants(components, note)
    note(f"Attached KiCad models (explicit choices or mechanical estimates): {resolved}.")
    return resolved
