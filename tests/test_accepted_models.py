"""@brief Archive of accepted KiCad model matches: signature, reuse, and its limits.

Scelta dell'utente (2026-09-23): dopo aver visto il solido, «Accetto, mi
piace» ricorda i modelli KiCad della scheda; su una scheda successiva un corpo
con la stessa firma geometrica riceve subito quel modello come proposta da
confermare, senza la misura dei candidati.

Ogni regola ha il suo caso negativo, costruito perche' differisca per una
ragione STRUTTURALE (categoria, base di misura, numero di terminali, taglia
standard vicina), non per una coincidenza numerica.
"""
import sqlite3

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.services import accepted_models as am


# ── Strumenti ───────────────────────────────────────────────────────


@pytest.fixture
def library(tmp_path):
    """Radice di libreria 3D finta con due modelli di resistenza e un IC."""
    root = tmp_path / "3dmodels"
    for relative in ("Resistor_SMD.3dshapes/R_0603_1608Metric.step",
                     "Resistor_SMD.3dshapes/R_0805_2012Metric.step",
                     "Package_TO_SOT_SMD.3dshapes/SOT-23-5.step",
                     "Package_TO_SOT_SMD.3dshapes/SOT-23-6.step"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ISO-10303-21;\n", encoding="utf-8")
    return root


def _body(name="R1", kind="resistor", length=1.6, width=0.8, **extra):
    """Corpo come esce dalla tabella dopo una generazione riuscita."""
    component = {"name": name, "kind": kind, "length_mm": length, "width_mm": width,
                 "bbox_basis": "body"}
    component.update(extra)
    return component


def _accepted(library, relative, **extra):
    """Corpo con un modello agganciato, pronto da ricordare."""
    component = _body(**extra)
    component["model_path"] = str(library / relative)
    component["model_match"] = {"status": "auto_matched"}
    return component


R0603 = "Resistor_SMD.3dshapes/R_0603_1608Metric.step"
R0805 = "Resistor_SMD.3dshapes/R_0805_2012Metric.step"
SOT5 = "Package_TO_SOT_SMD.3dshapes/SOT-23-5.step"
SOT6 = "Package_TO_SOT_SMD.3dshapes/SOT-23-6.step"


def _counted(count):
    """Numero di terminali osservato con fiducia (contratto di `_observed_count`)."""
    return {"pin_count": count, "pin_count_confidence": "high"}


# ── Lettura e scrittura dell'archivio ───────────────────────────────


def test_reading_a_missing_archive_finds_nothing_and_creates_no_file(library):
    store = am.AcceptedModelStore()
    assert store.find(_body(), [library]) is None
    assert store.entries() == []
    assert not store.path.exists()


def test_same_signature_on_another_board_reuses_the_accepted_model(library):
    store = am.AcceptedModelStore()
    result = store.consolidate([_accepted(library, R0603)], [library], source="Scheda A")
    assert (result.created, result.confirmed, result.skipped) == (1, 0, [])
    # Nuova foto: misure un po' diverse e lati scambiati (ruotato di 90 gradi).
    found = store.find(_body(name="R7", length=0.85, width=1.66), [library])
    assert found is not None
    assert found["path"] == library / R0603
    assert found["relative_xy_error"] < am.ACCEPTED_MATCH_TOLERANCE
    assert found["entry"]["model_ref"] == R0603  # relativo alla libreria


def test_the_adjacent_standard_size_is_not_the_same_signature(library):
    """Negativo: 0805 dista dal 0603 piu' della tolleranza, per costruzione della serie."""
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    assert store.find(_body(length=2.0, width=1.25), [library]) is None


def test_a_different_category_never_inherits_the_model(library):
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    assert store.find(_body(kind="capacitor"), [library]) is None
    # Un corpo senza tipo trova solo voci senza tipo.
    assert store.find(_body(kind=""), [library]) is None


def test_a_different_measurement_basis_is_a_different_signature(library):
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    assert store.find(_body(bbox_basis="pads"), [library]) is None
    # Gli alias del matcher valgono anche qui: leads e' la stessa base di total.
    store.consolidate([_accepted(library, R0603, bbox_basis="total")], [library])
    assert store.find(_body(bbox_basis="leads"), [library]) is not None


def test_terminal_counts_must_agree_when_both_are_observed(library):
    """SOT-23-5 e SOT-23-6 hanno lo stesso corpo: li separa solo il conteggio."""
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, SOT5, name="U1", kind="ic", length=2.9,
                                 width=1.6, **_counted(5))], [library])
    same_body = dict(name="U2", kind="ic", length=2.9, width=1.6)
    assert store.find(_body(**same_body, **_counted(6)), [library]) is None
    found = store.find(_body(**same_body, **_counted(5)), [library])
    assert found is not None and found["terminal_count_checked"] is True
    # Separazione FRAGILE dichiarata: senza osservazione sulla nuova scheda il
    # conteggio non si confronta, e il report lo dice.
    unobserved = store.find(_body(**same_body), [library])
    assert unobserved is not None and unobserved["terminal_count_checked"] is False


def test_the_latest_acceptance_wins_for_the_same_geometry(library):
    """Una correzione dell'utente (altro modello, stessa firma) prevale."""
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, SOT5, name="U1", kind="ic", length=2.9, width=1.6)],
                      [library])
    store.consolidate([_accepted(library, SOT6, name="U1", kind="ic", length=2.9, width=1.6)],
                      [library])
    found = store.find(_body(kind="ic", length=2.92, width=1.58), [library])
    assert found["path"] == library / SOT6
    assert found["alternatives"] == 1


def test_a_clearly_closer_entry_beats_a_more_recent_one(library):
    """Negativo della regola precedente: la recenza decide solo a parita' di misura."""
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, SOT5, kind="ic", length=3.0, width=1.6)], [library])
    store.consolidate([_accepted(library, SOT6, kind="ic", length=3.3, width=1.6)], [library])
    found = store.find(_body(kind="ic", length=3.0, width=1.6), [library])
    assert found["path"] == library / SOT5


def test_accepting_the_same_model_again_confirms_and_averages(library):
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603, length=1.6, width=0.8)], [library])
    result = store.consolidate([_accepted(library, R0603, length=1.7, width=0.84)], [library])
    assert (result.created, result.confirmed) == (0, 1)
    [entry] = store.entries()
    assert entry["samples"] == 2
    assert entry["long_mm"] == pytest.approx(1.65)
    assert entry["short_mm"] == pytest.approx(0.82)


@pytest.mark.parametrize("change, reason", [
    ({"render_3d": False}, "escluso dal modello 3D"),
    ({"model_selection": {"status": "rejected"}}, "segnaposto"),
    ({"model_match": {"status": "import_failed"}}, "nessun modello KiCad importato"),
    ({"model_path": None}, "nessun modello KiCad agganciato"),
    ({"length_mm": 0}, "dimensioni non valide"),
])
def test_components_without_a_model_to_remember_are_skipped_with_the_reason(
        library, change, reason):
    component = _accepted(library, R0603)
    component.update(change)
    ok, why = am.eligibility(component)
    assert not ok and reason in why
    result = am.AcceptedModelStore().consolidate([component], [library])
    assert result.stored == 0 and result.skipped == [("R1", why)]


def test_a_missing_model_file_is_not_remembered(library):
    component = _accepted(library, R0603)
    component["model_path"] = str(library / "Resistor_SMD.3dshapes/sparito.step")
    ok, why = am.eligibility(component)
    assert not ok and "non trovato" in why


def test_relative_references_survive_a_new_library_root(library, tmp_path):
    """Nuova versione di KiCad o altro PC: stessa struttura, radice diversa."""
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    moved = tmp_path / "kicad10" / "3dmodels"
    (moved / "Resistor_SMD.3dshapes").mkdir(parents=True)
    (moved / R0603).write_text("ISO-10303-21;\n", encoding="utf-8")
    found = store.find(_body(), [moved])
    assert found is not None and found["path"] == moved / R0603


def test_an_entry_whose_file_vanished_is_skipped_not_used(library):
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603)], [library])
    (library / R0603).unlink()
    assert store.find(_body(), [library]) is None


def test_models_outside_the_library_are_kept_by_absolute_path(library, tmp_path):
    custom = tmp_path / "miei_modelli" / "Modulo.step"
    custom.parent.mkdir()
    custom.write_text("ISO-10303-21;\n", encoding="utf-8")
    component = _body(kind="module", length=18, width=25.5)
    component.update(model_path=str(custom), model_match={"status": "confirmed"})
    store = am.AcceptedModelStore()
    store.consolidate([component], [library])
    assert store.entries()[0]["model_ref"] == str(custom)
    assert store.find(_body(kind="module", length=18.2, width=25.3), [library])["path"] == custom


def test_delete_removes_only_the_chosen_entries(library):
    store = am.AcceptedModelStore()
    store.consolidate([_accepted(library, R0603),
                       _accepted(library, R0805, length=2.0, width=1.25)], [library])
    entries = store.entries()
    assert store.delete([entries[0]["id"]]) == 1
    assert [e["id"] for e in store.entries()] == [entries[1]["id"]]
    assert store.delete([]) == 0


def test_a_newer_schema_is_refused_and_never_modified(library):
    store = am.AcceptedModelStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(am.AcceptedModelsError):
        store.find(_body(), [library])
    with pytest.raises(am.AcceptedModelsError):
        store.consolidate([_accepted(library, R0603)], [library])
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_only_an_explicit_angle_is_stored(library):
    """L'angolo scelto nel selettore si trasferisce; il delta del menu 3D no."""
    explicit = _accepted(library, R0603, model_selection={
        "status": "confirmed", "path": str(library / R0603), "rotation_deg": 90.0})
    explicit["model_rotation_delta_deg"] = 180
    automatic = _accepted(library, R0805, length=2.0, width=1.25)
    automatic["model_rotation_delta_deg"] = 90
    store = am.AcceptedModelStore()
    store.consolidate([explicit, automatic], [library])
    rotations = {e["model_name"]: e["rotation_deg"] for e in store.entries()}
    assert rotations == {"R_0603_1608Metric": 90.0, "R_0805_2012Metric": None}


# ── Aggancio nel matching ───────────────────────────────────────────


@pytest.fixture
def no_measurement(monkeypatch):
    """Il riuso deve saltare la misura: se viene chiamata, il test fallisce."""
    def refuse(*args, **kwargs):
        raise AssertionError("misura dei candidati chiamata nonostante l'archivio")
    monkeypatch.setattr(km, "model_sizes", refuse)
    monkeypatch.setattr(km, "footprint_sizes", refuse)


def test_model_candidates_uses_the_archive_before_any_measurement(library, no_measurement):
    am.AcceptedModelStore().consolidate([_accepted(library, R0603)], [library])
    notes = []
    report = km.model_candidates(_body(length=1.62, width=0.79), library=library,
                                 log=notes.append)
    assert report["status"] == "library_matched"
    assert report["selected_path"] == str(library / R0603)
    assert [c["status"] for c in report["candidates"]] == ["library_matched"]
    assert report["candidates"][0]["reasons"][0].startswith("Gia' accettato da te")
    assert any("misura dei candidati saltata" in note for note in notes)


def test_an_explicit_choice_on_this_board_beats_the_archive(library, no_measurement):
    am.AcceptedModelStore().consolidate([_accepted(library, R0603)], [library])
    chosen = _body(model_selection={"status": "confirmed", "path": str(library / R0805),
                                    "rotation_deg": None})
    report = km.model_candidates(chosen, library=library)
    assert report["status"] == "confirmed"
    assert report["selected_path"] == str(library / R0805)
    rejected = _body(model_selection={"status": "rejected"})
    assert km.model_candidates(rejected, library=library)["status"] == "unresolved"


def test_without_an_archive_hit_the_ordinary_search_runs(library, monkeypatch):
    """Negativo: categoria diversa -> nessuna proposta dall'archivio, si misura."""
    am.AcceptedModelStore().consolidate([_accepted(library, R0603)], [library])
    measured = []
    monkeypatch.setattr(km, "model_sizes",
                        lambda paths, *a, **k: measured.extend(paths) or {})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    report = km.model_candidates(_body(kind="capacitor", package="C_0603"),
                                 library=library)
    assert report["status"] != "library_matched"


def test_an_unreadable_archive_is_noted_and_ignored(library, monkeypatch):
    store = am.AcceptedModelStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"questo non e' un database sqlite" * 10)
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    notes = []
    report = km.model_candidates(_body(), library=library, log=notes.append)
    assert report["status"] != "library_matched"
    assert any("Archivio abbinamenti accettati ignorato" in note for note in notes)


def test_attach_models_applies_the_archive_model_as_a_declared_proposal(library, no_measurement):
    explicit = _accepted(library, R0603, model_selection={
        "status": "confirmed", "path": str(library / R0603), "rotation_deg": 90.0})
    am.AcceptedModelStore().consolidate([explicit], [library])
    component = _body(name="R9", length=1.58, width=0.82)
    assert km.attach_models([component], library=library) == 1
    assert component["model_path"] == str(library / R0603)
    assert component["model_review"] == "library_matched"
    assert component["model_match"]["attachment"] == "accepted_library"
    assert component["model_rotation_deg"] == 90.0
    assert component["render_placeholder"] is False


def test_an_archive_entry_without_angle_leaves_the_axis_to_inference(library, no_measurement):
    am.AcceptedModelStore().consolidate([_accepted(library, R0603)], [library])
    component = _body(model_rotation_deg=45.0)  # residuo di una corsa precedente
    km.attach_models([component], library=library)
    assert component["model_match"]["status"] == "library_matched"
    assert "model_rotation_deg" not in component


def test_reaccepting_a_reused_model_confirms_the_same_entry(library, no_measurement):
    am.AcceptedModelStore().consolidate([_accepted(library, R0603)], [library])
    component = _body(name="R9", length=1.62, width=0.8)
    km.attach_models([component], library=library)
    result = am.AcceptedModelStore().consolidate([component], [library])
    assert (result.created, result.confirmed) == (0, 1)
    assert am.AcceptedModelStore().entries()[0]["samples"] == 2


def test_observed_terminal_count_is_none_when_observations_disagree():
    assert km.observed_terminal_count(_counted(5)) == 5
    assert km.observed_terminal_count({}) is None
    disagreeing = dict(_counted(5), terminal_count=6, terminal_layout="two rows",
                       presence_confidence="high")
    assert km.observed_terminal_count(disagreeing) is None


def test_a_confirmation_without_angle_keeps_the_confirmed_angle(library):
    """A later acceptance with no explicit angle must not erase the stored one."""
    explicit = _accepted(library, R0603, model_selection={
        "status": "confirmed", "path": str(library / R0603), "rotation_deg": 90.0})
    store = am.AcceptedModelStore()
    store.consolidate([explicit], [library])
    # Same model and signature, accepted again from an automatic match.
    result = store.consolidate([_accepted(library, R0603, length=1.62)], [library])
    assert result.confirmed == 1
    [entry] = store.entries()
    assert entry["rotation_deg"] == 90.0 and entry["samples"] == 2


def test_a_confirmation_with_a_new_explicit_angle_replaces_it(library):
    """Negative case: an explicit new choice is still the latest choice."""
    first = _accepted(library, R0603, model_selection={
        "status": "confirmed", "path": str(library / R0603), "rotation_deg": 90.0})
    second = _accepted(library, R0603, length=1.62, model_selection={
        "status": "confirmed", "path": str(library / R0603), "rotation_deg": 180.0})
    store = am.AcceptedModelStore()
    store.consolidate([first], [library])
    store.consolidate([second], [library])
    [entry] = store.entries()
    assert entry["rotation_deg"] == 180.0
