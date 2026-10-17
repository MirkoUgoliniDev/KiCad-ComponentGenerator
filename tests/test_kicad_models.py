# -*- coding: utf-8 -*-
"""@brief Test legacy proposals and explicit attachment using a tiny mock library.

No installed KiCad library or FreeCAD measurement is needed. Dimensional
proposals remain available, but attaching a solid requires explicit selection.
"""

from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture(autouse=True)
def isolated_library_discovery(monkeypatch, tmp_path):
    """Use a tiny named library so legacy proposal tests never scan an installation."""
    root = tmp_path / "models"
    folder = root / "Package_TO_SOT_SMD.3dshapes"
    folder.mkdir(parents=True)
    for name in ("SOT-223.step", "SOT-223-8.step", "R_0402.step", "R_0402.wrl"):
        (folder / name).touch()
    monkeypatch.setattr(km, "find_library", lambda: root)
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})


# ── confronto delle misure, senza dipendenze esterne ──────────────────

def test_a_model_matching_the_measurement_is_accepted():
    assert km.size_matches([1.0, 0.5, 0.35], 1.0, 0.5)


def test_the_neighbouring_package_size_is_refused():
    """Fra 0402 e 0603 corre il 60%: devono restare distinguibili."""
    assert not km.size_matches([1.0, 0.5, 0.35], 1.6, 0.8)
    assert not km.size_matches([1.6, 0.8, 0.45], 1.0, 0.5)


def test_a_rotated_model_still_matches():
    """Il modello segue la convenzione del footprint, la foto come e' posato.

    Confrontare X con X scarterebbe un modello giusto solo perche' ruotato.
    """
    assert km.size_matches([9.17, 8.94, 5.56], 8.94, 9.17)


@pytest.mark.parametrize("bad", [None, [], [1.0], ["a", "b"], [0, 5], [-1, -2]])
def test_an_unusable_size_never_matches(bad):
    assert not km.size_matches(bad, 1.0, 0.5)
    assert km.size_distance(bad, 1.0, 0.5) is None


def test_the_distance_is_the_worst_of_the_two_sides():
    """Un lato giusto non deve coprire l'altro sbagliato."""
    assert km.size_distance([1.0, 1.0], 1.0, 0.5) == pytest.approx(1.0)


def test_the_tolerance_leaves_adjacent_sizes_separable():
    """Derivazione della soglia, non taratura.

    Fra due taglie adiacenti corre almeno il 60%; perche' la scelta per
    vicinanza prenda quella sbagliata servirebbe un errore oltre il 30%.
    """
    assert km.DEFAULT_SIZE_TOLERANCE <= 0.30


# ── ricerca per nome ──────────────────────────────────────────────────

def test_separators_do_not_break_the_match():
    """I nomi reali mescolano trattini, underscore e punti senza coerenza."""
    assert km._normalize("SOT-223") == km._normalize("SOT_223") == "sot223"
    assert km._normalize("U.FL") == "ufl"


def test_an_empty_name_finds_nothing():
    assert km.candidates("", library=Path("qualunque")) == []
    assert km.candidates(None, library=Path("qualunque")) == []


def test_a_missing_library_is_not_an_error(monkeypatch):
    """Senza KiCad si ripiega sulle forme sintetiche, non si fallisce."""
    monkeypatch.setattr(km, "find_library", lambda: None)
    assert km.candidates("SOT-223") == []
    assert km.resolve_model("SOT-223", 6.5, 3.5) is None


# ── selezione fra piu' candidati ──────────────────────────────────────

def test_the_closest_candidate_wins_not_the_first(monkeypatch, tmp_path):
    """E' la vicinanza a discriminare, non l'ordine della ricerca."""
    far, near = tmp_path / "A.step", tmp_path / "B.step"
    for path in (far, near):
        path.write_bytes(b"")
    monkeypatch.setattr(km, "candidates", lambda package, library=None: [far, near])
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        str(far): [1.20, 0.60, 0.3], str(near): [1.00, 0.50, 0.3]})
    assert km.resolve_model("R_0402", 1.0, 0.5) == near


def test_a_name_match_with_the_wrong_size_is_refused(monkeypatch, tmp_path):
    """Un package sbagliato darebbe un solido sbagliato con l'aria di esatto."""
    path = tmp_path / "SOT-223.step"
    path.write_bytes(b"")
    monkeypatch.setattr(km, "candidates", lambda package, library=None: [path])
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        str(path): [7.0, 6.5, 1.7]})
    messages = []
    assert km.resolve_model("SOT-223", 2.9, 1.3, log=messages.append) is None
    assert any("nessuno compatibile" in m for m in messages)
    assert any("%" in m for m in messages), "va detto di quanto sbaglia"


def test_an_unmeasurable_candidate_does_not_break_the_others(monkeypatch, tmp_path):
    broken, good = tmp_path / "rotto.step", tmp_path / "buono.step"
    for path in (broken, good):
        path.write_bytes(b"")
    monkeypatch.setattr(km, "candidates", lambda package, library=None: [broken, good])
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        str(broken): None, str(good): [1.0, 0.5, 0.3]})
    assert km.resolve_model("R_0402", 1.0, 0.5) == good


# ── name discovery against the bounded fixture library ──────────────

@pytest.fixture
def library():
    found = km.find_library()
    if found is None:
        pytest.skip("KiCad non installato: la libreria 3D non e' disponibile")
    return found


def test_the_library_is_found_and_populated(library):
    assert any(library.glob("*.3dshapes"))


def test_the_exact_name_ranks_before_a_longer_variant(library):
    """SOT-223 deve trovare SOT-223.step prima di SOT-223-8, altro componente."""
    found = km.candidates("SOT-223", library)
    if not found:
        pytest.skip("libreria priva di SOT-223")
    assert found[0].stem.lower() == "sot-223"


def test_step_is_preferred_over_the_display_mesh(library):
    found = km.candidates("R_0402", library)
    if not found:
        pytest.skip("libreria priva di R_0402")
    assert found[0].suffix.lower() in (".step", ".stp")


# ── preferenza al nome esatto quando la misura non discrimina ─────────

def test_the_exact_name_wins_when_the_measurement_cannot_tell(monkeypatch, tmp_path):
    """SOT-223 e SOT-223-8 hanno lo stesso corpo: li distingue solo il nome."""
    variant, exact = tmp_path / "SOT-223-8.step", tmp_path / "SOT-223.step"
    for path in (variant, exact):
        path.write_bytes(b"")
    monkeypatch.setattr(km, "candidates", lambda package, library=None: [variant, exact])
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        # La variante e' pure marginalmente piu' vicina: non deve bastare.
        str(variant): {"body": [3.45, 6.45, 1.8], "total": [7.0, 6.5, 1.8]},
        str(exact): {"body": [3.47, 6.47, 1.7], "total": [7.0, 6.5, 1.7]}})
    assert km.resolve_model("SOT-223", 6.55, 3.21) == exact


def test_a_clearly_closer_model_still_wins_over_the_exact_name(monkeypatch, tmp_path):
    """Il nome decide solo a parita' di prove, non contro la misura."""
    exact, better = tmp_path / "R_0402.step", tmp_path / "R_0402_1005Metric.step"
    for path in (exact, better):
        path.write_bytes(b"")
    monkeypatch.setattr(km, "candidates", lambda package, library=None: [exact, better])
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        str(exact): {"body": [1.25, 0.62, 0.3]},     # 25% di scarto
        str(better): {"body": [1.00, 0.50, 0.3]}})   # esatto
    assert km.resolve_model("R_0402", 1.0, 0.5) == better


# ── lettura doppia: corpo ed estensione coi reofori ───────────────────

def test_either_reading_of_the_model_can_confirm_the_measurement():
    """Il rettangolo sulla foto puo' aver seguito il corpo o i reofori."""
    sot223 = {"body": [3.47, 6.47, 1.7], "total": [7.00, 6.50, 1.7]}
    assert km.size_matches(sot223, 6.55, 3.21), "misura sul corpo rifiutata"
    assert km.size_matches(sot223, 6.50, 7.00), "misura coi reofori rifiutata"


def test_a_wrong_package_is_refused_by_both_readings():
    sot223 = {"body": [3.47, 6.47, 1.7], "total": [7.00, 6.50, 1.7]}
    assert not km.size_matches(sot223, 2.9, 1.3), "un SOT-23 non e' un SOT-223"


# ── associazione ai componenti ────────────────────────────────────────

def test_components_without_a_package_are_left_to_the_synthetic_shapes(monkeypatch):
    monkeypatch.setattr(km, "find_library", lambda: Path("qualunque"))
    components = [{"length_mm": 1.0, "width_mm": 0.5},
                  {"package": "", "length_mm": 1.0, "width_mm": 0.5}]
    assert km.attach_models(components) == 0
    assert not any("model_path" in c for c in components)


def test_a_candidate_requires_selection_before_carrying_the_model_path(monkeypatch, tmp_path):
    """@brief Un candidato in revisione non porta model_path e non e' magenta.

    Contratto aggiornato: il magenta e' riservato a chi non ha candidati
    (unresolved). Un corpo con candidati ha una geometria nota e tiene il suo
    colore (qui quello del rilevatore); lo stato di revisione sta nei campi
    espliciti model_review / render_placeholder, non nel colore.
    """
    model = tmp_path / "R_0402_1005Metric.step"
    model.write_bytes(b"")
    monkeypatch.setattr(km, "find_library", lambda: tmp_path)
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {str(model): [1, .5, .3]})
    components = [{"package": "R_0402", "length_mm": 1.0, "width_mm": 0.5, "color_hex": "#303030"}]
    assert km.attach_models(components) == 0
    component = components[0]
    assert "model_path" not in component
    assert component["color_hex"] == "#303030"
    assert component["render_placeholder"] is False
    assert component["model_review"] == "review_required"
    assert component["model_match"]["status"] == "review_required"
    assert component["model_match"]["candidates"][0]["dimensions_mm"] == [1, .5, .3]
    component["model_selection"] = {"status": "confirmed", "path": str(model), "rotation_deg": -90}
    assert km.attach_models(components) == 1
    assert components[0]["model_path"] == str(model)
    assert component["model_match"]["status"] == "confirmed"
    assert component["model_rotation_deg"] == -90


def test_attaching_never_raises_on_broken_entries(monkeypatch, tmp_path):
    """Un componente malformato non deve fermare gli altri."""
    monkeypatch.setattr(km, "find_library", lambda: tmp_path)
    monkeypatch.setattr(km, "resolve_model", lambda *a, **k: None)
    assert km.attach_models([None, "stringa", {"package": "X"},
                             {"package": "Y", "length_mm": "no", "width_mm": 1}]) == 0


def test_without_the_library_nothing_is_attached_and_it_is_said(monkeypatch):
    monkeypatch.setattr(km, "find_library", lambda: None)
    messages = []
    assert km.attach_models([{"package": "R_0402", "length_mm": 1, "width_mm": 0.5}],
                            log=messages.append) == 0
    assert any("non trovata" in m for m in messages)


# ── scelta esplicita dell'utente sul solido ───────────────────────────

@pytest.fixture
def no_library(monkeypatch):
    """Senza libreria nessun componente si risolve: resta solo la scelta."""
    monkeypatch.setattr(km, "find_library", lambda: None)


def test_an_explicit_exclusion_survives_the_next_attachment(no_library):
    """Togliere un falso positivo dal 3D e' una scelta, non stato calcolato.

    attach_models riparte da capo a ogni rigenerazione: senza questo il
    componente eliminato tornerebbe al primo "Rigenera modello 3D".
    """
    component = {"package": "R_0402", "length_mm": 1.0, "width_mm": 0.5,
                 "render_3d_override": False}
    km.attach_models([component])
    assert component["render_3d"] is False


def test_without_an_explicit_choice_visibility_stays_automatic(no_library):
    """Il negativo: nessuna scelta dell'utente, decide l'automatismo."""
    component = {"package": "R_0402", "length_mm": 1.0, "width_mm": 0.5}
    km.attach_models([component])
    assert component["render_3d"] is True


def test_an_explicit_inclusion_overrides_the_detector_only_omission(no_library):
    """La scelta vale in entrambi i versi, non solo per nascondere."""
    omitted = {"name": "YOLO candidate 12", "package": "", "length_mm": 1.0, "width_mm": 0.5}
    km.attach_models([omitted])
    assert omitted["render_3d"] is False, "ipotesi del solo rilevatore: fuori dal modello STEP"

    forced = dict(omitted, render_3d_override=True)
    km.attach_models([forced])
    assert forced["render_3d"] is True
