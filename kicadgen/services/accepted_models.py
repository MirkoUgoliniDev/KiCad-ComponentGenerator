"""@brief Archive of KiCad model matches the user accepted, reused on later boards.

When the user inspects the generated solid and presses «Accetto, mi piace»,
every component of the board with an attached KiCad model becomes an entry:
category, measurement basis, photographic envelope, observed terminal count
and chosen model. On a later board a body with the same geometric signature
receives that model at once, without measuring candidates in FreeCAD, as a
PROPOSAL to confirm (user decision: "preselect, you confirm").

@note Current crop examples are compared visually before mechanical filtering.
    Strong visual matches with conflicting physical evidence remain review-only
    proposals. Entries without usable crops retain mechanical matching, never
    rules based on component names or markings. Signatures are compared within a declared tolerance,
    never for equality, because two photographs never yield identical sizes.
    The tolerance is the measurement uncertainty of the two photographs
    (`cad3d.measurement_uncertainty`), in millimetres per side: neighbouring
    standard sizes are separated by an absolute step (QFN 6x6 / 7x7 / 8x8 by
    1 mm, 0402 / 0603 by 0.6 x 0.3 mm), not by a constant percentage.
@note Families whose same outline exists with several terminal counts
    (TERMINAL_COUNT_CATEGORIES: SOT-23-3/5/6, QFN-40/48 in 6x6 and 7x7...) are
    reused only when the terminal count was observed on BOTH boards and is
    equal for automatic reuse; a conflicting visual candidate requires review.
@note The archive is a separate SQLite file (`data/accepted_components.sqlite3`
    or `KICADGEN_MATCH_DB`) so it can be copied, backed up or shared without
    touching AI profiles and translations. Reading never creates the file.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Iterable, Optional
from .messages import tx
from ..core.validation import finite_number, positive_number

#: Nome del file dentro `data/` quando non c'e' la variabile d'ambiente.
DEFAULT_FILENAME = "accepted_components.sqlite3"

#: Variabile che sposta l'archivio (usata anche dai test per isolarsi).
DB_ENV = "KICADGEN_MATCH_DB"

#: 2: colonna `side_uncertainty_mm` (incertezza della misura archiviata).
#: Un archivio della versione 1 resta leggibile (la colonna manca = incertezza
#: non nota) e viene aggiornato alla prima scrittura.
SCHEMA_VERSION = 5
IMAGE_CROP_VERSION = 2

#: CORREZIONE (audit 26/09, punto 82 medio 8): la vecchia tolleranza relativa
#: fissa del 15% si giustificava con «le taglie adiacenti distano almeno
#: ~35%», che e' falso: QFN 6x6 contro 7x7 distano il 14%, 7x7 contro 8x8 il
#: 12.5%, e piu' il package e' grande piu' la distanza relativa si riduce.
#: Le taglie standard sono separate da un passo ASSOLUTO, quindi la tolleranza
#: e' ora in millimetri per lato e viene dall'incertezza delle due misure
#: fotografiche (vedi `match_tolerance_mm`).

#: Categorie in cui lo stesso ingombro esiste con numeri di terminali diversi
#: (SOT-23-3/5/6, SOIC/QFN con passi diversi nello stesso corpo, QFN-48 sia
#: 6x6 sia 7x7): per queste l'ingombro non identifica il modello e il numero
#: di terminali deve essere stato osservato su ENTRAMBE le schede, uguale.
#: Sono i tipi strutturati che il matcher tratta come circuito integrato o
#: semiconduttore (`kicad_models._compatible_candidates`), non nomi di parti.
TERMINAL_COUNT_CATEGORIES = frozenset({
    "ic", "ic_soic", "ic_qfn", "ic_qfp", "ic_bga", "ic_sot", "power_tab",
    "semiconductor", "semiconductor_unknown", "transistor"})

#: Entro questo scarto due voci sono sostenute dalla misura allo stesso modo
#: (stesso valore di `kicad_models.NAME_TIEBREAK_MARGIN`): decide la scelta
#: piu' recente, cioe' l'ultima correzione dell'utente.
RECENCY_TIE_MARGIN = 0.05

#: Basi di misura riconosciute, con gli alias del matcher.
_BASIS_ALIASES = {"leads": "total", "body_and_leads": "total"}
_BASES = ("unknown", "body", "total", "pads")


class AcceptedModelsError(RuntimeError):
    """@brief The archive exists but is unreadable or written by a newer schema."""


@dataclass
class ConsolidationResult:
    """@brief Outcome of a consolidation: new entries, confirmations, skipped rows with reasons."""
    created: int = 0
    confirmed: int = 0
    images_saved: int = 0
    images_skipped: int = 0
    skipped: list = field(default_factory=list)

    @property
    def stored(self) -> int:
        """@brief Components actually recorded (new entries plus confirmations)."""
        return self.created + self.confirmed


def default_path() -> Path:
    """@brief Archive path: `KICADGEN_MATCH_DB` or the tool's `data/` folder.

    @return Path of the SQLite file (it may not exist yet).
    """
    override = os.environ.get(DB_ENV)
    if override:
        return Path(override)
    from ..app_paths import data_file
    return data_file(DEFAULT_FILENAME)


def category_of(component: dict) -> str:
    """@brief Declared body category, lower-case.

    @param component Component mapping.
    @return The structured `kind`, or "unknown".
    @note Only the structured field is read, never marking or name. An untyped
        body becomes "unknown" and only finds "unknown" entries: a 3x3 black
        box must not inherit an IC model accepted elsewhere just by size.
    """
    kind = str(component.get("kind") or "").strip().lower()
    return kind or "unknown"


def basis_of(component: dict) -> str:
    """@brief Measurement basis (body, total, pads, unknown), normalised as in the matcher.

    @param component Component mapping.
    @return One of "unknown", "body", "total", "pads".
    """
    basis = str(component.get("bbox_basis") or "unknown")
    basis = _BASIS_ALIASES.get(basis, basis)
    return basis if basis in _BASES else "unknown"


def signature(component: dict) -> Optional[dict]:
    """@brief Geometric signature of a body: the archive key.

    @param component Component as produced by the inventory or the table.
    @return Mapping with category, basis, long_mm, short_mm, height_mm and
        terminal_count; None when the XY size is unusable.
    @note Sides are sorted (long, short) so a quarter turn between boards does
        not change the signature. Height is kept only when declared
        (`kicad_models.declared_height`) and is informational; the terminal
        count only when confidently observed
        (`kicad_models.observed_terminal_count`).
    """
    if not isinstance(component, dict):
        return None
    length = positive_number(component.get("length_mm"))
    width = positive_number(component.get("width_mm"))
    if length is None or width is None:
        return None
    # Import pigro: kicad_models importa questo modulo durante il matching.
    from ..cad3d.kicad_models import declared_height, observed_terminal_count
    long_mm, short_mm = sorted((length, width), reverse=True)
    return {"category": category_of(component), "basis": basis_of(component),
            "long_mm": long_mm, "short_mm": short_mm,
            "height_mm": declared_height(component),
            "terminal_count": observed_terminal_count(component)}


def match_tolerance_mm(component: dict, stored_side_uncertainty=None) -> tuple[float, str]:
    """@brief Largest per-side difference, in mm, that two photographic envelopes may show and still be one size.

    @param component New measurement; it may declare its own uncertainty or
        the scale of its photograph (see `cad3d.measurement_uncertainty`).
    @param stored_side_uncertainty Side uncertainty of the archived
        measurement, in mm, when it was recorded (None for entries written
        before it was, or without a known scale).
    @return (tolerance_mm, source) where source says where the NEW
        measurement's uncertainty comes from (declared, photo_scale, assumed).
    @note Two independent measurements: the tolerance is the sum of their side
        uncertainties. An archived entry without its own uncertainty is
        assumed as good as the new one (declared estimate). Without any data
        the fallback of `measurement_uncertainty` applies (24 px/mm, the
        coarsest scale at which the inventory claims completeness): about
        0.33 mm per side, which separates 0402/0603/0805 and QFN 6x6/7x7 but
        not 01005 from 0201 -- sizes that such a photo cannot resolve anyway.
    """
    from ..cad3d.measurement_uncertainty import (
        component_edge_uncertainty, side_uncertainty_mm)
    edge, source = component_edge_uncertainty(component)
    new_side = side_uncertainty_mm(edge)
    stored = positive_number(stored_side_uncertainty)
    return new_side + (stored if stored is not None else new_side), source


def side_uncertainty_of(component: dict) -> Optional[float]:
    """@brief Side uncertainty of a measurement, in mm, only when it is known.

    @param component Component mapping.
    @return The uncertainty derived from a declared value or from the photo
        scale; None when only the fallback would apply (nothing to record).
    """
    from ..cad3d.measurement_uncertainty import (
        SOURCE_ASSUMED, component_edge_uncertainty, side_uncertainty_mm)
    edge, source = component_edge_uncertainty(component)
    return None if source == SOURCE_ASSUMED else side_uncertainty_mm(edge)


def within_tolerance(long_a, short_a, long_b, short_b, tolerance_mm: float) -> bool:
    """@brief Tell whether two sorted envelopes differ by at most tolerance_mm on both sides.

    @param long_a Long side of the first envelope, mm.
    @param short_a Short side of the first envelope, mm.
    @param long_b Long side of the second envelope, mm.
    @param short_b Short side of the second envelope, mm.
    @param tolerance_mm Largest accepted difference per side, mm.
    @return True when both sides agree within the tolerance.
    """
    return (abs(long_a - long_b) <= tolerance_mm + 1e-9
            and abs(short_a - short_b) <= tolerance_mm + 1e-9)


def xy_error(long_a, short_a, long_b, short_b) -> float:
    """@brief Worst-axis relative error of (a) against the current measurement (b).

    @return max(|La-Lb|/Lb, |Sa-Sb|/Sb), the metric of `kicad_models.size_distance`.
    """
    return max(abs(long_a - long_b) / long_b, abs(short_a - short_b) / short_b)


def model_reference(path, roots: Iterable[Path]) -> str:
    """@brief Portable model reference, relative to a library root when possible.

    @param path Chosen STEP file.
    @param roots KiCad 3D library roots in use.
    @return `Folder.3dshapes/Name.step` when the file lies under a root,
        otherwise the absolute path (personal models outside the library).
    @note A relative reference survives a KiCad upgrade (9 -> 10) and a
        different PC, where the root changes but the layout does not.
    """
    path = Path(path)
    for root in roots or ():
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            continue
    return str(path)


def resolve_reference(reference: str, roots: Iterable[Path]) -> Optional[Path]:
    """@brief Locate an entry's STEP file on disk.

    @param reference Stored reference (relative or absolute).
    @param roots Library roots to try for relative references.
    @return Existing file path, or None when it is gone.
    """
    if not isinstance(reference, str) or not reference.strip():
        return None
    candidate = Path(reference)
    try:
        if candidate.is_absolute():
            return candidate if candidate.is_file() else None
        for root in roots or ():
            full = Path(root) / candidate
            if full.is_file():
                return full
    except OSError:
        return None
    return None


def _explicit_rotation(component: dict) -> Optional[float]:
    """Local model angle explicitly chosen by the user, if any.

    The angle belongs to this example's deskewed crop, never to the entire
    archive row. Reused angles must come from an individually matched example.
    The caller includes any explicit context-menu delta when storing the crop.
    """
    selection = component.get("model_selection")
    if isinstance(selection, dict) and selection.get("status") == "confirmed":
        return finite_number(selection.get("rotation_deg"))
    report = component.get("model_match")
    if isinstance(report, dict):
        base = finite_number(component.get("model_rotation_deg"))
        if base is not None:
            return base
        rendered = finite_number(report.get("rendered_local_rotation_deg"))
        if rendered is not None:
            return rendered - (finite_number(report.get("rendered_manual_delta_deg")) or 0.0)
    if isinstance(report, dict) and report.get("status") == "library_matched":
        entry = report.get("accepted_entry")
        if isinstance(entry, dict):
            orientation = report.get("accepted_orientation") or {}
            return finite_number(orientation.get("rotation_deg"))
    return None


def eligibility(component: dict) -> tuple[bool, str]:
    """@brief Tell whether a component can enter the archive, and why not.

    @param component Component with matching metadata.
    @return (True, "") or (False, user-facing reason in Italian, translated by the GUI).
    """
    if not isinstance(component, dict):
        return False, "voce non valida"
    if component.get("render_3d") is False:
        return False, "escluso dal modello 3D"
    selection = component.get("model_selection")
    if isinstance(selection, dict) and selection.get("status") == "rejected":
        return False, "hai scelto il segnaposto generico, nessun modello da ricordare"
    report = component.get("model_match")
    status = report.get("status") if isinstance(report, dict) else None
    if status in ("unresolved", "import_failed", "review_required") or (
            isinstance(report, dict) and report.get('render_placeholder') is True):
        return False, "nessun modello KiCad importato"
    path = component.get("model_path")
    if not isinstance(path, str) or not path.strip():
        return False, "nessun modello KiCad agganciato"
    try:
        if Path(path).suffix.lower() not in (".step", ".stp") or not Path(path).is_file():
            return False, "file del modello non trovato"
    except OSError:
        return False, "file del modello non trovato"
    if signature(component) is None:
        return False, "dimensioni non valide"
    return True, ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class AcceptedModelStore:
    """@brief SQLite archive of the model matches accepted by the user.

    @note Connections are short-lived. Reads never create the file; a missing
        file means "no entries". A newer schema is never modified.
    """

    def __init__(self, path=None):
        """@brief Bind the store to a file.

        @param path Optional explicit path; default `default_path()`.
        """
        self.path = Path(path) if path else default_path()

    # ── Connessione ─────────────────────────────────────────────────

    @contextmanager
    def _connect(self, *, create: bool):
        if not create and not self.path.is_file():
            yield None
            return
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path, timeout=5)
        except sqlite3.Error as exc:
            raise AcceptedModelsError(tx("Archivio abbinamenti non apribile: {exc}", exc=exc)) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                self._ensure_schema(connection, create)
                yield connection
        except sqlite3.DatabaseError as exc:
            raise AcceptedModelsError(tx("Archivio abbinamenti non leggibile: {exc}", exc=exc)) from exc
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(db, create: bool) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise AcceptedModelsError(
                tx("Archivio abbinamenti creato da una versione piu' recente: non viene "
                   "toccato."))
        if version == SCHEMA_VERSION:
            return
        if version == 1:
            # Versione 1: stessa tabella senza l'incertezza. In lettura la
            # colonna mancante vale «non nota»; alla prima scrittura si
            # aggiunge, senza toccare le voci esistenti.
            if create:
                db.execute("ALTER TABLE accepted_models ADD COLUMN side_uncertainty_mm REAL")
                db.execute("PRAGMA user_version=2")
                version = 2
            else:
                return
        if version == 2:
            if not create:
                return
            db.execute(
                "CREATE TABLE IF NOT EXISTS accepted_model_images ("
                " id INTEGER PRIMARY KEY,"
                " accepted_model_id INTEGER NOT NULL REFERENCES accepted_models(id) ON DELETE CASCADE,"
                " image_png BLOB NOT NULL,"
                " descriptor BLOB NOT NULL,"
                " crop_version INTEGER NOT NULL,"
                " created_at TEXT NOT NULL,"
                " component_name TEXT NOT NULL DEFAULT '')")
            db.execute("CREATE INDEX IF NOT EXISTS accepted_model_images_parent "
                       "ON accepted_model_images(accepted_model_id)")
            db.execute("PRAGMA user_version=4")
            AcceptedModelStore._ensure_schema(db, create)
            return
        if version == 3:
            if not create:
                return
            db.execute("ALTER TABLE accepted_model_images ADD COLUMN component_name "
                       "TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA user_version=4")
            AcceptedModelStore._ensure_schema(db, create)
            return
        if version == 4:
            if not create:
                return
            columns = {row[1] for row in db.execute("PRAGMA table_info(accepted_model_images)")}
            if "model_rotation_deg" not in columns:
                db.execute("ALTER TABLE accepted_model_images ADD COLUMN model_rotation_deg REAL")
            db.execute("PRAGMA user_version=5")
            return
        if not create:
            # Un file vuoto o estraneo in sola lettura: nessuna voce, nessuna scrittura.
            raise AcceptedModelsError(tx("Archivio abbinamenti senza schema riconosciuto."))
        db.execute(
            "CREATE TABLE IF NOT EXISTS accepted_models ("
            " id INTEGER PRIMARY KEY,"
            " category TEXT NOT NULL,"
            " basis TEXT NOT NULL,"
            " long_mm REAL NOT NULL,"
            " short_mm REAL NOT NULL,"
            " height_mm REAL,"
            " terminal_count INTEGER,"
            " model_ref TEXT NOT NULL,"
            " model_name TEXT NOT NULL,"
            " rotation_deg REAL,"
            " samples INTEGER NOT NULL DEFAULT 1,"
            " first_accepted TEXT NOT NULL,"
            " last_accepted TEXT NOT NULL,"
            " source TEXT NOT NULL DEFAULT '',"
            " example_name TEXT NOT NULL DEFAULT '',"
            " side_uncertainty_mm REAL)")
        db.execute("CREATE INDEX IF NOT EXISTS accepted_models_key "
                   "ON accepted_models(category, basis)")
        db.execute("PRAGMA user_version=2")
        # Continue through the same migration used for existing schema v2.
        AcceptedModelStore._ensure_schema(db, create)

    # ── Lettura ─────────────────────────────────────────────────────

    def entries(self) -> list[dict]:
        """@brief All entries, most recent first.

        @return List of row mappings; empty when the archive does not exist.
        @throws AcceptedModelsError When the file is unreadable or newer.
        """
        with self._connect(create=False) as db:
            if db is None:
                return []
            has_images = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accepted_model_images'").fetchone()
            image_count = ("(SELECT count(*) FROM accepted_model_images i "
                           "WHERE i.accepted_model_id=a.id)" if has_images else "0")
            return [dict(row) for row in db.execute(
                f"SELECT a.*, {image_count} AS image_count "
                "FROM accepted_models a ORDER BY last_accepted DESC, id DESC")]

    def images(self, entry_id: int) -> list[dict]:
        """@brief Return verified crop examples stored for one accepted model entry."""
        with self._connect(create=False) as db:
            if db is None:
                return []
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accepted_model_images'").fetchone()
            if not exists:
                return []
            columns = {row[1] for row in db.execute("PRAGMA table_info(accepted_model_images)")}
            name_column = "component_name" if "component_name" in columns else "'' AS component_name"
            rotation_column = "model_rotation_deg" if "model_rotation_deg" in columns else "NULL AS model_rotation_deg"
            return [dict(row) for row in db.execute(
                "SELECT id, accepted_model_id, image_png, descriptor, crop_version, created_at, "
                f"{name_column}, {rotation_column} "
                "FROM accepted_model_images WHERE accepted_model_id=? ORDER BY id",
                (int(entry_id),))]

    def find(self, component: dict, roots: Iterable[Path], diagnostic=None) -> Optional[dict]:
        """@brief Compare all current crop examples first, then validate physical evidence.

        Strong visual candidates survive conflicting AI observations as review-only
        proposals. A visual score is similarity, not a probability of correctness.
        Legacy entries without usable images retain the mechanical fallback.
        """
        roots = list(roots or ())
        visual = self._find_visual(component, roots, diagnostic)
        if visual is not None:
            return visual
        return self._find_mechanical(component, roots, diagnostic)

    def _find_visual(self, component, roots, diagnostic):
        """@brief Rank crops before category/terminal gates; never auto-attach conflicts."""
        from .component_image_match import (descriptor_from_bytes, similarity,
                                             rotated_image_similarity, match_orientation)
        payload = component.get('_accepted_image_descriptor')
        if not payload:
            return None
        try:
            query = descriptor_from_bytes(payload)
        except (TypeError, ValueError):
            return None
        with self._connect(create=False) as db:
            if db is None or not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accepted_model_images'").fetchone():
                return None
            entries = {r['id']: dict(r) for r in db.execute('SELECT * FROM accepted_models')}
            columns = {r[1] for r in db.execute('PRAGMA table_info(accepted_model_images)')}
            angle = 'model_rotation_deg' if 'model_rotation_deg' in columns else 'NULL AS model_rotation_deg'
            images = [dict(r) for r in db.execute(
                f'SELECT id, accepted_model_id, descriptor, image_png, {angle} FROM accepted_model_images WHERE crop_version=?',
                (IMAGE_CROP_VERSION,))]
        scores, examples = {}, {}
        for image in images:
            key = image['accepted_model_id']
            if key not in entries:
                continue
            try:
                score = max(similarity(query, descriptor_from_bytes(image['descriptor'])),
                            rotated_image_similarity(query, image['image_png']))
            except (TypeError, ValueError):
                continue
            scores[key] = max(scores.get(key, 0.0), score)
            examples.setdefault(key, []).append(image)
        if diagnostic is not None:
            diagnostic['visual_examples_compared'] = sum(map(len, examples.values()))
        ranked = sorted(scores, key=lambda key: scores[key], reverse=True)
        if not ranked or scores[ranked[0]] < .92:
            return None
        key = ranked[0]
        sig = signature(component)
        reference = entries[key]['model_ref']
        best_score = scores[key]
        # Several verified examples can share one model. Prefer complete,
        # compatible evidence within its visual tie band, not a row missing pins.
        def evidence_rank(k):
            row = entries[k]
            if sig is None:
                return (1, 1, 1, 1, -scores[k])
            tolerance, _ = match_tolerance_mm(component, row.get('side_uncertainty_mm'))
            a, b = sig['terminal_count'], row.get('terminal_count')
            count_penalty = 0 if a is not None and b == a else 1 if b is not None else 2
            return (sig['category'] != row['category'],
                    not within_tolerance(row['long_mm'], row['short_mm'], sig['long_mm'], sig['short_mm'], tolerance),
                    count_penalty, sig['basis'] != row['basis'], -scores[k])
        same_model = [k for k in ranked if entries[k]['model_ref'] == reference
                      and scores[k] >= max(.92, best_score - .04)]
        key = min(same_model, key=evidence_rank)
        entry, score = entries[key], scores[key]
        conflicts = []
        rival = max((scores[k] for k in ranked if entries[k]['model_ref'] != entry['model_ref']), default=0.0)
        if best_score - rival < .08:
            conflicts.append(tx('Ritagli simili associati a modelli diversi: scelta ambigua.'))
        sig = signature(component)
        tolerance, source = match_tolerance_mm(component, entry.get('side_uncertainty_mm'))
        error = None
        count_checked = False
        if sig is None:
            conflicts.append(tx('Categoria o misure insufficienti.'))
        else:
            if sig['category'] != entry['category']:
                conflicts.append(tx('Categoria diversa da quella del ritaglio consolidato.'))
            if not within_tolerance(entry['long_mm'], entry['short_mm'], sig['long_mm'], sig['short_mm'], tolerance):
                conflicts.append(tx('Misure fuori dalla tolleranza dei consolidati.'))
            error = xy_error(entry['long_mm'], entry['short_mm'], sig['long_mm'], sig['short_mm'])
            a, b = sig['terminal_count'], entry.get('terminal_count')
            count_checked = a is not None and b is not None and a == b
            if a is not None and b is not None and a != b:
                conflicts.append(tx('Numero di terminali diverso dal consolidato.'))
            elif (sig['category'] in TERMINAL_COUNT_CATEGORIES or entry['category'] in TERMINAL_COUNT_CATEGORIES) and not count_checked:
                conflicts.append(tx('Conteggio dei terminali mancante o da verificare.'))
            if sig['basis'] != entry['basis'] and {sig['basis'], entry['basis']} != {'body', 'total'}:
                conflicts.append(tx('Base di misura non confrontabile con il consolidato.'))
        path = resolve_reference(entry['model_ref'], roots)
        if path is None:
            # Preserve the candidate in the diagnostic report, without a usable attachment.
            if diagnostic is not None:
                diagnostic['visual_unavailable_model'] = entry['model_ref']
            return None
        entry['image_count'] = len(examples[key])
        if diagnostic is not None:
            diagnostic['visual_conflicts'] = conflicts
        return dict(entry=entry, path=path, relative_xy_error=error,
                    tolerance_mm=tolerance, tolerance_source=source,
                    terminal_count_checked=count_checked, alternatives=len(ranked)-1,
                    visual_score=score, image_examples=len(examples[key]),
                    basis_recovered_visually=bool(sig and sig['basis'] != entry['basis']),
                    orientation=match_orientation(query, examples[key]),
                    review_required=bool(conflicts), visual_conflicts=conflicts,
                    visual_first=True)

    def _find_mechanical(self, component: dict, roots: Iterable[Path], diagnostic=None) -> Optional[dict]:
        """@brief Accepted entry compatible with the body, or None.

        @param component Body of the new board.
        @param roots 3D library roots used to locate the STEP file.
        @return Mapping with `entry`, `path` (resolved file),
            `relative_xy_error`, `terminal_count_checked` and `alternatives`
            (other compatible entries not chosen).
        @throws AcceptedModelsError When the file is unreadable or newer.
        @return The mapping also carries `tolerance_mm` and
            `tolerance_source` (where the new measurement's uncertainty
            comes from).
        @note Rules: same category; a body/total basis change requires a visual score
            of at least .92 and a .08 margin over competing models. Both bases
            still require compatible measured sizes. When both terminal counts are
            known they must be equal, and for TERMINAL_COUNT_CATEGORIES both
            must be known; each side within `match_tolerance_mm`. The closest
            entry (worst-axis relative error) wins; within RECENCY_TIE_MARGIN
            the most recent acceptance wins (the user's latest correction).
            Entries whose file vanished are skipped.
        """
        def explain(reason):
            if diagnostic is not None:
                diagnostic['reason'] = reason
        sig = signature(component)
        if sig is None:
            explain(tx('Categoria o misure insufficienti.'))
            return None
        roots = list(roots or ())
        with self._connect(create=False) as db:
            if db is None:
                explain(tx('Archivio consolidati non presente.'))
                return None
            has_images = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accepted_model_images'").fetchone()
            image_count = ("(SELECT count(*) FROM accepted_model_images i "
                           "WHERE i.accepted_model_id=accepted_models.id)" if has_images else "0")
            rows = [dict(row) for row in db.execute(
                f"SELECT accepted_models.*, {image_count} AS image_count "
                "FROM accepted_models WHERE category=?",
                (sig["category"],))]
        if not rows:
            explain(tx('Nessuna voce consolidata per questa categoria.'))
            return None
        needs_count = sig["category"] in TERMINAL_COUNT_CATEGORIES
        if needs_count and sig["terminal_count"] is None:
            explain(tx('Conteggio dei terminali mancante o da verificare.'))
            # Stesso ingombro, piedinature diverse: senza il conteggio su
            # questa scheda l'archivio non sa quale modello proporre.
            return None
        compatible = []
        passed_count, passed_size = 0, 0
        for row in rows:
            stored_count = row.get("terminal_count")
            if needs_count and stored_count is None:
                continue
            if (sig["terminal_count"] is not None and stored_count is not None
                    and int(stored_count) != sig["terminal_count"]):
                continue
            passed_count += 1
            tolerance, source = match_tolerance_mm(component, row.get("side_uncertainty_mm"))
            if not within_tolerance(row["long_mm"], row["short_mm"],
                                    sig["long_mm"], sig["short_mm"], tolerance):
                continue
            passed_size += 1
            path = resolve_reference(row["model_ref"], roots)
            if path is None:
                continue
            error = xy_error(row["long_mm"], row["short_mm"], sig["long_mm"], sig["short_mm"])
            compatible.append((error, row, path, tolerance, source, 0.0))
        if not compatible:
            explain(tx('Terminali incompatibili o non documentati nei consolidati.') if not passed_count
                    else tx('Misure fuori dalla tolleranza dei consolidati.') if not passed_size
                    else tx('File dei modelli consolidati non trovato nella libreria.'))
            return None
        orientation_examples = {}
        query_descriptor = component.get("_accepted_image_descriptor")
        if query_descriptor:
            try:
                from .component_image_match import descriptor_from_bytes, similarity, rotated_image_similarity
                query_descriptor = descriptor_from_bytes(query_descriptor)
                with self._connect(create=False) as db:
                    has_images = (db is not None and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='accepted_model_images'").fetchone())
                    if has_images:
                        ids = [item[1]["id"] for item in compatible]
                        placeholders = ",".join("?" * len(ids))
                        scores = {}
                        columns = {r[1] for r in db.execute("PRAGMA table_info(accepted_model_images)")}
                        angle_column = "model_rotation_deg" if "model_rotation_deg" in columns else "NULL AS model_rotation_deg"
                        rows_images = db.execute(
                            f"SELECT id, accepted_model_id, descriptor, image_png, {angle_column} FROM accepted_model_images "
                            f"WHERE accepted_model_id IN ({placeholders}) "
                            "AND crop_version=?",
                            (*ids, IMAGE_CROP_VERSION))
                        for image in rows_images:
                            score = similarity(query_descriptor,
                                               descriptor_from_bytes(image["descriptor"]))
                            score = max(score, rotated_image_similarity(query_descriptor, image["image_png"]))
                            key = image["accepted_model_id"]
                            scores[key] = max(scores.get(key, 0.0), score)
                            if image["model_rotation_deg"] is not None:
                                orientation_examples.setdefault(key, []).append(dict(image))
                        compatible = [(*item[:5], scores.get(item[1]["id"], 0.0))
                                      for item in compatible]
            except (TypeError, ValueError, sqlite3.Error, AcceptedModelsError):
                # Immagine/descriptor non valido: il match meccanico resta valido.
                pass
        # A basis label can change between AI runs. Recover only with strong,
        # unambiguous visual evidence, compatible dimensions AND terminal count.
        strict = [item for item in compatible if item[1]["basis"] == sig["basis"]]
        recovered = []
        for item in compatible:
            if (item in strict or item[5] < .92
                    or {item[1]["basis"], sig["basis"]} != {"body", "total"}):
                continue
            competitors = [other[5] for other in compatible
                           if other[1]["model_ref"] != item[1]["model_ref"]]
            if item[5] - max(competitors, default=0.0) >= .08:
                recovered.append(item)
        compatible = strict + recovered
        if not compatible:
            explain(tx('Base di misura diversa e confronto visivo insufficiente o ambiguo.'))
            return None
        closest = min(item[0] for item in compatible)
        band = [item for item in compatible if item[0] <= closest + RECENCY_TIE_MARGIN]
        best_visual = max((item[5] for item in band), default=0.0)
        visual_tie = [item for item in band if item[5] >= best_visual - 0.04]
        # Let images break a geometric/recency tie only when a reference is
        # recognizably similar. Weak visual scores leave the established rule.
        if best_visual >= 0.45 and len(visual_tie) < len(band):
            band = visual_tie
        band.sort(key=lambda item: (item[1]["last_accepted"], item[1]["id"]), reverse=True)
        error, row, path, tolerance, source, visual_score = band[0]
        from .component_image_match import match_orientation
        orientation = match_orientation(query_descriptor, orientation_examples.get(row["id"], []))
        return {"entry": row, "path": path, "relative_xy_error": error,
                "basis_recovered_visually": row["basis"] != sig["basis"],
                "orientation": orientation,
                "terminal_count_checked": (sig["terminal_count"] is not None
                                           and row.get("terminal_count") is not None),
                "tolerance_mm": tolerance, "tolerance_source": source,
                "alternatives": len(compatible) - 1,
                "visual_score": visual_score,
                "image_examples": row.get("image_count", 0)}

    # ── Scrittura ───────────────────────────────────────────────────

    def consolidate(self, components: Iterable[dict], roots: Iterable[Path], *,
                    source: str = "") -> ConsolidationResult:
        """@brief Record accepted components; confirm entries already present.

        @param components Board components with attached models.
        @param roots 3D library roots, for portable references.
        @param source Component/project name, for display only.
        @return Counts of new entries and confirmations plus skipped rows.
        @throws AcceptedModelsError When the archive cannot be written.
        @note An entry with the same category, basis, terminal count and SAME
            model within `match_tolerance_mm` is confirmed: its envelope becomes the mean
            of the measurements, its recorded uncertainty the larger of the
            two known ones (conservative: a mean does not remove a systematic
            error), `samples` grows and the angle becomes the
            latest EXPLICIT choice: a confirmation without an explicit angle
            keeps the stored one instead of erasing it. A different model for the same signature creates a
            new entry, which wins on reuse because it is more recent.
        """
        result = ConsolidationResult()
        roots = list(roots or ())
        pending = []
        for component in components or ():
            ok, reason = eligibility(component)
            name = str(component.get("name") or "?") if isinstance(component, dict) else "?"
            if not ok:
                result.skipped.append((name, reason))
                continue
            pending.append((name, component))
        if not pending:
            return result
        with self._connect(create=True) as db:
            db.execute("BEGIN IMMEDIATE")
            for name, component in pending:
                sig = signature(component)
                reference = model_reference(component["model_path"], roots)
                rotation = _explicit_rotation(component)
                now = _now()
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM accepted_models WHERE category=? AND basis=? AND model_ref=?"
                    " AND terminal_count IS ?",
                    (sig["category"], sig["basis"], reference, sig["terminal_count"]))]
                uncertainty = side_uncertainty_of(component)
                near = [(xy_error(r["long_mm"], r["short_mm"], sig["long_mm"], sig["short_mm"]), r)
                        for r in rows
                        if within_tolerance(r["long_mm"], r["short_mm"],
                                            sig["long_mm"], sig["short_mm"],
                                            match_tolerance_mm(
                                                component, r.get("side_uncertainty_mm"))[0])]
                if near:
                    _, row = min(near, key=lambda item: item[0])
                    accepted_id = int(row["id"])
                    n = int(row["samples"])
                    height = sig["height_mm"]
                    if row["height_mm"] is not None and height is not None:
                        height = (row["height_mm"] * n + height) / (n + 1)
                    elif height is None:
                        height = row["height_mm"]
                    known = [value for value in (row.get("side_uncertainty_mm"), uncertainty)
                             if positive_number(value) is not None]
                    db.execute(
                        "UPDATE accepted_models SET long_mm=?, short_mm=?, height_mm=?,"
                        " rotation_deg=?, samples=?, last_accepted=?, source=?, example_name=?,"
                        " side_uncertainty_mm=?"
                        " WHERE id=?",
                        ((row["long_mm"] * n + sig["long_mm"]) / (n + 1),
                         (row["short_mm"] * n + sig["short_mm"]) / (n + 1),
                         height,
                         # Una nuova accettazione senza scelta esplicita
                         # dell'angolo non e' una scelta di "nessun angolo":
                         # l'angolo confermato in precedenza resta valido.
                         rotation if rotation is not None else row["rotation_deg"],
                         n + 1, now, source, name, max(known) if known else None,
                         row["id"]))
                    result.confirmed += 1
                else:
                    db.execute(
                        "INSERT INTO accepted_models (category, basis, long_mm, short_mm,"
                        " height_mm, terminal_count, model_ref, model_name, rotation_deg,"
                        " samples, first_accepted, last_accepted, source, example_name,"
                        " side_uncertainty_mm)"
                        " VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
                        (sig["category"], sig["basis"], sig["long_mm"], sig["short_mm"],
                         sig["height_mm"], sig["terminal_count"], reference,
                         Path(component["model_path"]).stem, rotation, now, now,
                         source, name, uncertainty))
                    accepted_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                    result.created += 1
                image_png = component.get("_accepted_image_png")
                descriptor = component.get("_accepted_image_descriptor")
                if image_png is not None or descriptor is not None:
                    valid_png = (isinstance(image_png, (bytes, bytearray))
                                 and bytes(image_png).startswith(b"\x89PNG\r\n\x1a\n"))
                    valid_descriptor = (isinstance(descriptor, (bytes, bytearray))
                                        and len(descriptor) == 4 * 4 * 9 * 4)
                    if valid_png and valid_descriptor:
                        # Old crops used a wider contextual window. Once a
                        # fresh verified crop is available for this entry,
                        # discard stale derived examples so matching and the
                        # viewer only see the corrected crop geometry.
                        db.execute(
                            "DELETE FROM accepted_model_images WHERE accepted_model_id=? "
                            "AND crop_version<>?",
                            (accepted_id, IMAGE_CROP_VERSION))
                        db.execute(
                            "INSERT INTO accepted_model_images (accepted_model_id, image_png, "
                            "descriptor, crop_version, created_at, component_name, model_rotation_deg) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (accepted_id, bytes(image_png), bytes(descriptor),
                             IMAGE_CROP_VERSION, now,
                             str(component.get("name") or "").strip(),
                             ((rotation + (finite_number(component.get("model_rotation_delta_deg")) or 0.0)) % 360
                              if rotation is not None else None)))
                        result.images_saved += 1
                    else:
                        result.images_skipped += 1
        return result

    def delete(self, ids: Iterable[int]) -> int:
        """@brief Delete the given entries.

        @param ids Entry ids (integers only).
        @return Number of rows removed.
        """
        ids = [int(i) for i in ids or () if type(i) is int]
        if not ids:
            return 0
        with self._connect(create=False) as db:
            if db is None:
                return 0
            cursor = db.execute(
                f"DELETE FROM accepted_models WHERE id IN ({','.join('?' * len(ids))})", ids)
            return cursor.rowcount
