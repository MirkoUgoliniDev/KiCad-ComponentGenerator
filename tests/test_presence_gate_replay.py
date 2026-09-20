"""@brief Replay offline della corsa 2026-09-21 (55 componenti, 0 modelli abbinati).

Nella corsa reale tutti i 55 componenti sono arrivati al matcher con
`presence_confidence='low'` (un errore su un solo componente, U1_MODULE, ha
degradato l'intero inventario globale) e il report ha chiuso con 40
`review_required`, 15 `unresolved` e nessun `model_path`. Sulla stessa foto
la corsa precedente ne aveva 11.

Qui NON si rifa' la ricerca in libreria (servirebbe la libreria KiCad e il
CAD): si prendono i candidati veri e le misure vere dal report della corsa
(`tests/fixtures/run0004_presence_gate.json`) e si rigiocano le sole
decisioni finali di `kicad_models`, cioe' le funzioni che trasformano una
lista di candidati in `auto_matched` / `provisional_matched`:

* `_automatic_mechanical`  -- pretende `presence_confidence == 'high'`;
* `_provisional_geometry_choice` -- idem;
* `_tentative_endcap_family` -- idem, e decide a monte lo SCOPO della
  ricerca per i chip passivi (il percorso "standard_chip_endcaps" non
  tronca la ricerca, quello generico si').

Il test rende visibile la catena: presenza 'low' -> nessuna delle tre porte
si apre -> ogni componente con candidati resta `review_required` -> nessun
`model_path` -> `freecad_parts._library_model` non importa nulla (nessun
KICADGEN_IMPORT_START nel log). Non modifica il comportamento del matcher:
registra quello attuale, con i numeri della corsa.
"""
import copy
import json
from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "run0004_presence_gate.json"


@pytest.fixture(scope="module")
def run():
    """I 55 componenti della corsa, con evidenza fotografica e candidati veri."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))["components"]
    assert len(data) == 55
    return data


def _basis(component):
    """Stessa normalizzazione di model_candidates (leads/body_and_leads -> total)."""
    basis = component.get("bbox_basis") or "unknown"
    return {"leads": "total", "body_and_leads": "total"}.get(str(basis), basis)


def _decide(component, presence):
    """Rigioca le due porte finali su una copia del componente.

    `complete` viene dai flag del report (search_truncated / topology_incomplete):
    e' un limite superiore, perche' una lettura CAD fallita lo azzera senza
    lasciare traccia nel JSON. Per questo il replay puo' solo sovrastimare gli
    abbinamenti, mai inventare un blocco che il codice non ha.
    """
    report = component["model_match"]
    choices = report["candidates"]
    if not choices:
        return "unresolved"
    c = copy.deepcopy(component)
    c["presence_confidence"] = presence
    if report.get("topology_incomplete"):
        c["topology_incomplete"] = True
    complete = not (report.get("search_truncated") or report.get("topology_incomplete"))
    if km._automatic_mechanical(c, choices, complete, _basis(c)):
        return "auto_matched"
    if (not report.get("topology_incomplete")
            and km._provisional_geometry_choice(c, choices, complete, _basis(c)) is not None):
        return "provisional_matched"
    return "review_required"


def test_the_run_really_arrived_with_every_presence_low(run):
    """I numeri di partenza, come li ha scritti la corsa: e' la premessa del replay."""
    assert all(c["presence_confidence"] == "low" for c in run)
    assert all(c["_global_inventory_status"] == "partial" for c in run)
    statuses = [c["model_match"]["status"] for c in run]
    assert statuses.count("review_required") == 40
    assert statuses.count("unresolved") == 15
    assert not any(c["model_path"] for c in run)
    # L'errore che ha degradato tutti riguarda un solo componente, ma e'
    # ripetuto su ogni riga dell'inventario.
    errors = {tuple(c.get("_global_inventory_errors") or ()) for c in run}
    assert len(errors) == 1 and "U1_MODULE" in next(iter(errors))[0]


def test_with_presence_low_the_gates_reproduce_the_run(run):
    """Negativo: con la presenza 'low' della corsa le due porte finali non
    lasciano passare nessuno dei 40 componenti con candidati, esattamente
    come nel report reale (40 review_required, 0 selezionati)."""
    decided = {c["name"]: _decide(c, "low") for c in run}
    assert sum(1 for s in decided.values() if s == "review_required") == 40
    assert sum(1 for s in decided.values() if s == "unresolved") == 15
    assert not any(s in ("auto_matched", "provisional_matched") for s in decided.values())
    # E' davvero la porta sulla presenza: gli stessi candidati, con gli stessi
    # scarti, hanno un primo candidato entro tolleranza per gran parte dei 40.
    within = [c for c in run if c["model_match"]["candidates"]
              and c["model_match"]["candidates"][0]["relative_xy_error"] <= km.DEFAULT_SIZE_TOLERANCE]
    assert len(within) == 40


def test_with_presence_high_on_the_sound_components_matches_return(run):
    """Positivo: rimettendo 'high' sui componenti sani (quelli senza un errore
    proprio nell'inventario globale, cioe' tutti tranne U1_MODULE, il solo con
    la fusione dei riquadri fallita) le porte finali tornano ad aprirsi, e per
    ragioni strutturali diverse fra loro, non per un numero fortunato:

    * SW_BOOT: pareggio fra i primi due candidati (stessa famiglia, stesso
      inviluppo XY) -> provisional;
    * J_USBC, U2_CH334F, U3_CH343P: ricerca completa e primo candidato che
      domina il secondo oltre NAME_TIEBREAK_MARGIN -> provisional;
    * SW_RESET resta review: stesso scarto di SW_BOOT ma i primi due
      candidati non pareggiano e la ricerca e' troncata, quindi la dominanza
      non e' dimostrabile. E' il controllo che la porta non si apre "a caso".

    I 29 passivi restano review anche con 'high': nella corsa la loro ricerca
    e' stata TRONCATA (`search_truncated`), e un abbinamento automatico
    pretende una ricerca completa. Quel troncamento e' a sua volta figlio
    della presenza 'low' (vedi il test successivo), ma il replay non puo'
    rifare la ricerca senza libreria: qui si registra solo che la porta
    finale, da sola, non basta a spiegare gli 11 abbinamenti della corsa
    precedente.
    """
    sound = [c for c in run if c["name"] != "U1_MODULE (rf_module)"]
    decided = {c["name"]: _decide(c, "high") for c in sound}
    matched = sorted(name for name, s in decided.items() if s == "provisional_matched")
    assert matched == ["J_USBC (connector)", "SW_BOOT (switch)",
                       "U2_CH334F (ic)", "U3_CH343P (ic)"]
    assert decided["SW_RESET (switch)"] == "review_required"
    assert not any(s == "auto_matched" for s in decided.values())
    passives = [c for c in sound if c["kind"] in ("resistor", "capacitor")]
    assert len(passives) == 29
    assert all(c["model_match"].get("search_truncated") for c in passives)
    assert all(decided[c["name"]] == "review_required" for c in passives)


def test_presence_also_decides_the_discovery_scope_of_chip_passives(run):
    """La stessa porta sta a monte della ricerca: `_tentative_endcap_family`
    apre il percorso 'standard_chip_endcaps' (ricerca completa su tutte le
    taglie) solo con presenza 'high'. Con 'low' i chip cadono nella ricerca
    generica, che tronca i modelli freddi: da li' il `search_truncated` che
    nella corsa ha chiuso i 29 passivi. Positivo/negativo sullo stesso dato:
    i chip con package nominato e due terminazioni osservate aprono il
    percorso con 'high' e non con 'low'; quelli senza package non lo aprono
    in nessun caso (manca la taglia nominata da verificare)."""
    chips = [c for c in run if c["kind"] in ("resistor", "capacitor")]
    nominated = [c for c in chips if c["package"]]
    bare = [c for c in chips if not c["package"]]
    assert len(nominated) == 20 and len(bare) == 9
    for c in nominated:
        low, high = copy.deepcopy(c), copy.deepcopy(c)
        high["presence_confidence"] = "high"
        assert km._tentative_endcap_family(low) is None
        assert km._tentative_endcap_family(high) == c["kind"]
    for c in bare:
        high = copy.deepcopy(c)
        high["presence_confidence"] = "high"
        assert km._tentative_endcap_family(high) is None


def test_the_module_has_no_model_path_only_because_of_its_presence(run):
    """U1_MODULE: un solo candidato (lo STEP trovato per marcatura letterale,
    `discovery_scope == 'literal_marking_model'`), ricerca completa, basi
    'total'. Con 'low' la porta automatica lo respinge e il report non ha
    `selected_path`; con 'high' lo stesso candidato passa per marcatura
    letterale. Senza `model_path` `freecad_parts._library_model` esce prima di
    stampare KICADGEN_IMPORT_START: e' questo, e non un errore di import, il
    motivo per cui nel freecad.log della corsa quella riga manca."""
    module = next(c for c in run if c["name"] == "U1_MODULE (rf_module)")
    report = module["model_match"]
    assert report["discovery_scope"] == "literal_marking_model"
    assert len(report["candidates"]) == 1 and report["selected_path"] is None
    assert not report.get("search_truncated") and not report.get("topology_incomplete")
    assert _decide(module, "low") == "review_required"
    assert _decide(module, "high") == "auto_matched"
    assert km._marked_model_matches(Path(report["candidates"][0]["path"]), module)
    assert module["model_path"] is None
