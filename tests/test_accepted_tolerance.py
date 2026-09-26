"""@brief Accepted-matches archive: tolerance from the measurement uncertainty, terminal count, search always run.

Decisione dell'utente (2026-09-26, punto 84 medio 8). La vecchia tolleranza
relativa del 15% lasciava un QFN 7x7 riusare il modello di un 6x6 (14%), e un
abbinamento dall'archivio saltava del tutto la ricerca in libreria.

Ogni caso negativo differisce dal positivo per una ragione STRUTTURALE: la
taglia standard vicina (passo assoluto di 1 mm), lo stesso ingombro con
piedinature diverse, un modello della libreria piu' vicino alla misura.
"""
import sqlite3

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import measurement_uncertainty as mu
from kicadgen.services import accepted_models as am


QFN6 = "Package_DFN_QFN.3dshapes/QFN-48-1EP_6x6mm_P0.4mm_EP4.2x4.2mm.step"
QFN7 = "Package_DFN_QFN.3dshapes/QFN-48-1EP_7x7mm_P0.5mm_EP5.6x5.6mm.step"
R0603 = "Resistor_SMD.3dshapes/R_0603_1608Metric.step"
R0805 = "Resistor_SMD.3dshapes/R_0805_2012Metric.step"

#: Ingombri misurati dal finto misuratore (corpo e totale, mm).
SIZES = {"QFN-48-1EP_6x6mm_P0.4mm_EP4.2x4.2mm": [6.0, 6.0, 0.9],
         "QFN-48-1EP_7x7mm_P0.5mm_EP5.6x5.6mm": [7.0, 7.0, 0.9],
         "R_0603_1608Metric": [1.6, 0.8, 0.45],
         "R_0805_2012Metric": [2.0, 1.25, 0.5]}


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Libreria finta con misure note; registra quali modelli vengono misurati."""
    root = tmp_path / "3dmodels"
    for relative in (QFN6, QFN7, R0603, R0805):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ISO-10303-21;\n", encoding="utf-8")
    measured = []

    def measure(paths, *args, **kwargs):
        measured.extend(paths)
        from pathlib import Path
        return {str(p): {"body": SIZES[Path(p).stem], "total": SIZES[Path(p).stem]}
                for p in paths if Path(p).stem in SIZES}
    monkeypatch.setattr(km, "model_sizes", measure)
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    return root, measured


def _body(kind="resistor", length=1.6, width=0.8, **extra):
    component = {"name": "X1", "kind": kind, "length_mm": length, "width_mm": width,
                 "bbox_basis": "body"}
    component.update(extra)
    return component


def _accepted(library, relative, **extra):
    component = _body(**extra)
    component["model_path"] = str(library / relative)
    component["model_match"] = {"status": "auto_matched"}
    return component


def _counted(count):
    return {"pin_count": count, "pin_count_confidence": "high"}


# ── Tolleranza ──────────────────────────────────────────────────────


def test_qfn_7x7_does_not_reuse_an_archived_6x6_with_the_same_terminal_count(library):
    """Negativo: stesso conteggio (48), taglia vicina a 1 mm; la vecchia soglia del 15% la accettava."""
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, QFN6, kind="ic", length=6.0, width=6.0,
                                 **_counted(48))], [library])
    assert store.find(_body(kind="ic", length=7.0, width=7.0, **_counted(48)), [library]) is None
    # Il vecchio criterio (scarto relativo 1/7 = 14%) lo avrebbe accettato.
    assert am.xy_error(6.0, 6.0, 7.0, 7.0) < 0.15


def test_the_same_qfn_size_is_reused(library):
    """Positivo della regola precedente: stessa taglia, rumore di misura."""
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, QFN6, kind="ic", length=6.0, width=6.0,
                                 **_counted(48))], [library])
    found = store.find(_body(kind="ic", length=6.15, width=5.9, **_counted(48)), [library])
    assert found is not None and found["path"] == library / QFN6


def test_an_0603_still_reuses_its_archived_0603(library):
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    found = store.find(_body(length=1.7, width=0.86), [library])
    assert found is not None and found["path"] == library / R0603
    assert found["tolerance_source"] == mu.SOURCE_ASSUMED
    assert found["tolerance_mm"] == pytest.approx(
        2 * mu.side_uncertainty_mm(mu.EDGE_UNCERTAINTY_PX / mu.ASSUMED_SCALE_PX_PER_MM))
    # La taglia vicina resta separata anche con il ripiego.
    assert store.find(_body(length=2.0, width=1.25), [library]) is None


def test_the_tolerance_follows_the_declared_photo_scale(library):
    """Una foto grossolana non distingue 0.3 mm; una fine si'."""
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    shifted = dict(length=1.9, width=0.8)
    assert store.find(_body(**shifted, photo_scale_px_per_mm=8.0), [library]) is not None
    assert store.find(_body(**shifted, photo_scale_px_per_mm=80.0), [library]) is None


def test_an_ic_needs_the_terminal_count_on_both_boards(library):
    """Stesso ingombro, piedinature diverse: senza conteggio non si riusa."""
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, QFN6, kind="ic", length=6.0, width=6.0)], [library])
    # Voce senza conteggio: non riusabile per un integrato, nemmeno con conteggio nuovo.
    assert store.find(_body(kind="ic", length=6.0, width=6.0, **_counted(48)), [library]) is None
    store.consolidate([_accepted(library, QFN6, kind="ic", length=6.0, width=6.0,
                                 **_counted(48))], [library])
    assert store.find(_body(kind="ic", length=6.0, width=6.0), [library]) is None
    assert store.find(_body(kind="ic", length=6.0, width=6.0, **_counted(48)),
                      [library]) is not None


def test_a_passive_does_not_need_a_terminal_count(library):
    """Negativo della regola precedente: un resistore non ha varianti di piedinatura."""
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    assert store.find(_body(), [library])["terminal_count_checked"] is False


def test_a_known_uncertainty_is_recorded_and_used(library):
    library, _ = library
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603, photo_scale_px_per_mm=10.0)], [library])
    [entry] = store.entries()
    assert entry["side_uncertainty_mm"] == pytest.approx(0.4)
    # Archiviata grossolana (0.4) + nuova fine (0.08): 0.48 mm per lato.
    found = store.find(_body(length=2.0, width=0.8, photo_scale_px_per_mm=50.0), [library])
    assert found is not None and found["tolerance_mm"] == pytest.approx(0.48)


def test_a_version_1_archive_is_read_and_upgraded_on_write(library):
    library, _ = library
    store = am.AcceptedModelStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TABLE accepted_models (id INTEGER PRIMARY KEY, category TEXT NOT NULL,"
            " basis TEXT NOT NULL, long_mm REAL NOT NULL, short_mm REAL NOT NULL,"
            " height_mm REAL, terminal_count INTEGER, model_ref TEXT NOT NULL,"
            " model_name TEXT NOT NULL, rotation_deg REAL, samples INTEGER NOT NULL DEFAULT 1,"
            " first_accepted TEXT NOT NULL, last_accepted TEXT NOT NULL,"
            " source TEXT NOT NULL DEFAULT '', example_name TEXT NOT NULL DEFAULT '')")
        db.execute("INSERT INTO accepted_models (category, basis, long_mm, short_mm,"
                   " model_ref, model_name, first_accepted, last_accepted) VALUES"
                   " ('resistor', 'body', 1.6, 0.8, ?, 'R', '2026-01-01', '2026-01-01')",
                   (R0603,))
        db.execute("PRAGMA user_version=1")
    assert store.find(_body(), [library])["path"] == library / R0603
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1  # lettura: nessuna scrittura
    store.consolidate([_accepted(library, R0805, length=2.0, width=1.25)], [library])
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == am.SCHEMA_VERSION
    assert len(store.entries()) == 2


# ── Ricerca in libreria sempre eseguita ─────────────────────────────


def test_the_library_search_always_runs_and_the_archive_keeps_an_equal_model(library):
    library, measured = library
    am.AcceptedModelStore().consolidate([_accepted(library, R0603)], [library])
    report = km.model_candidates(_body(length=1.62, width=0.79), library=library)
    assert measured, "la ricerca in libreria non e' stata eseguita"
    assert report["status"] == "library_matched"
    assert report["selected_path"] == str(library / R0603)
    assert report["candidates"][0]["status"] == "library_matched"


def test_a_clearly_closer_library_model_beats_the_archive(library):
    """Voce accettata su un ingombro misurato largo: la libreria ha un modello piu' vicino."""
    library, _ = library
    am.AcceptedModelStore().consolidate(
        [_accepted(library, R0805, length=1.8, width=1.0)], [library])
    notes = []
    report = km.model_candidates(_body(length=1.6, width=0.8), library=library,
                                 log=notes.append)
    assert report["status"] != "library_matched"
    assert report["candidates"][0]["path"] == str(library / R0603)
    assert any("piu' vicino alla misura" in note for note in notes)
