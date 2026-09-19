"""Modelli 3D reali dalla libreria installata con KiCad.

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
from typing import Iterable, Optional

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
INDEX_FILENAME = ".kicadgen-model-sizes-v3.json"


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
    for base in ("/usr/share/kicad/3dmodels", "/usr/local/share/kicad/3dmodels",
                 "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"):
        yield Path(base)


def find_library() -> Optional[Path]:
    """Cartella dei modelli 3D di KiCad, oppure None se KiCad non c'e'.

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


def candidates(package: str, library: Optional[Path] = None) -> list[Path]:
    """Modelli il cui nome e' compatibile col package proposto, i piu' vicini prima.

    L'ordinamento conta: ``SOT-223`` deve trovare ``SOT-223.step`` prima di
    ``SOT-223-8.step``, che e' un altro componente.
    """
    library = library or find_library()
    wanted = _normalize(package)
    if not library or not wanted:
        return []
    exact, prefix, partial = [], [], []
    for path in library.rglob("*"):
        if path.suffix.lower() not in MODEL_SUFFIXES:
            continue
        stem = _normalize(path.stem)
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
    cache.mkdir(parents=True, exist_ok=True)
    return cache / INDEX_FILENAME


def load_index(library: Optional[Path] = None) -> dict:
    """@brief Read the measured-size index, tolerating a missing or broken file."""
    library = library or find_library()
    if not library:
        return {}
    try:
        return json.loads(_index_path(library).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_index(index: dict, library: Optional[Path] = None) -> None:
    """@brief Persist the measured-size index; a failure is never fatal."""
    library = library or find_library()
    if not library:
        return
    try:
        _index_path(library).write_text(json.dumps(index, indent=1), encoding="utf-8")
    except OSError:
        # L'indice e' solo una cache: senza, si torna a misurare.
        pass


def index_key(path: Path) -> str:
    """Chiave che cambia se il file cambia, cosi' l'indice non invecchia male."""
    try:
        stat = path.stat()
        return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return path.name


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
    """Ingombro (X, Y, Z in mm) di ogni modello, misurato con FreeCAD.

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
    """Ingombri dei modelli richiesti, dall'indice o misurandoli una volta."""
    paths = [Path(p) for p in paths]
    library = library or find_library()
    index = load_index(library)
    missing = [p for p in paths if index_key(p) not in index]
    if missing:
        measured = measure_models(missing)
        for path in missing:
            index[index_key(path)] = measured.get(str(path))
        save_index(index, library)
    return {str(p): index.get(index_key(p)) for p in paths}


def size_distance(model_size, length_mm, width_mm) -> Optional[float]:
    """Quanto il modello si discosta dalla misura, in scarto relativo.

    `model_size` puo' essere una coppia di lati oppure il dizionario prodotto
    dalla misura, con `total` (reofori compresi) e `body` (solo corpo). In
    quel caso vince la lettura piu' vicina: il rettangolo tracciato sulla
    foto puo' aver seguito l'uno o l'altro, e sono entrambe legittime.

    Confronta lato lungo con lato lungo: il modello della libreria segue la
    convenzione del suo footprint, la misura fotografica segue come il pezzo
    e' posato. Confrontare X con X scarterebbe un modello giusto solo perche'
    ruotato di novanta gradi.
    """
    if isinstance(model_size, dict):
        readings = [v for v in (model_size.get("body"), model_size.get("total")) if v]
        distances = [d for d in (size_distance(r, length_mm, width_mm) for r in readings)
                     if d is not None]
        return min(distances) if distances else None
    if not model_size or len(model_size) < 2:
        return None
    try:
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


def resolve_model(package: str, length_mm: float, width_mm: float, *,
                  library: Optional[Path] = None,
                  tolerance: float = DEFAULT_SIZE_TOLERANCE,
                  log=None) -> Optional[Path]:
    """Modello della libreria KiCad verificato contro le quote misurate.

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
    shortlist = found[:8]
    sizes = model_sizes(shortlist, library)
    scored = []
    for path in shortlist:
        distance = size_distance(sizes.get(str(path)), length_mm, width_mm)
        if distance is not None and distance <= tolerance:
            scored.append((distance, str(path), path))
    if not scored:
        closest = min((d for d in (size_distance(sizes.get(str(p)), length_mm, width_mm)
                                   for p in shortlist) if d is not None), default=None)
        detail = f"; il piu' vicino sbaglia del {closest*100:.0f}%" if closest is not None else ""
        note(f"{package}: {len(found)} modelli trovati, nessuno compatibile con "
             f"{length_mm:.2f} x {width_mm:.2f} mm{detail}; si usa la forma approssimata.")
        return None
    # La misura non distingue due varianti con lo stesso corpo (SOT-223 e
    # SOT-223-8 differiscono per il numero di reofori, non per l'ingombro).
    # Fra candidati che la misura sostiene allo stesso modo decide il nome:
    # la corrispondenza esatta prima, poi quella piu' corta.
    best = min(d for d, _, _ in scored)
    wanted = _normalize(package)
    scored.sort(key=lambda item: (item[0] > best + NAME_TIEBREAK_MARGIN,
                                  _normalize(item[2].stem) != wanted,
                                  len(item[2].stem), item[1]))
    distance, _, path = scored[0]
    note(f"{package}: modello reale {path.name}, {describe_size(sizes[str(path)])} "
         f"contro misura {length_mm:.2f} x {width_mm:.2f} mm, scarto {distance*100:.0f}%.")
    return path


def describe_size(size) -> str:
    """@brief Render a measured model size for the log, body and total."""
    if isinstance(size, dict):
        parts = []
        for key, label in (("body", "corpo"), ("total", "con reofori")):
            value = size.get(key)
            if value:
                parts.append(f"{label} {value[0]:.2f} x {value[1]:.2f}")
        return "; ".join(parts) or "ingombro ignoto"
    if size and len(size) >= 2:
        return f"{size[0]:.2f} x {size[1]:.2f}"
    return "ingombro ignoto"


def attach_models(components, log=None, library: Optional[Path] = None) -> int:
    """Associa a ogni componente il modello 3D reale, quando esiste.

    Aggiunge `model_path` ai componenti risolti; gli altri restano senza e il
    generatore ripiega sulla forma sintetica. Non solleva mai: la libreria e'
    un miglioramento, non un requisito.

    Ritorna quanti componenti hanno ottenuto un modello reale.
    """
    note = log or (lambda message: None)
    library = library or find_library()
    if library is None:
        note("Libreria 3D di KiCad non trovata: si usano le forme approssimate.")
        return 0
    resolved = 0
    for component in components or []:
        if not isinstance(component, dict):
            continue
        package = str(component.get("package") or "").strip()
        if not package:
            continue
        try:
            path = resolve_model(package, float(component["length_mm"]),
                                 float(component["width_mm"]),
                                 library=library, log=note)
        except (KeyError, TypeError, ValueError):
            continue
        if path is not None:
            component["model_path"] = str(path)
            resolved += 1
    note(f"Modelli reali dalla libreria KiCad: {resolved} su "
         f"{len(components or [])} componenti.")
    return resolved
