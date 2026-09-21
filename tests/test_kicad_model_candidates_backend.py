"""@brief Review-first candidate matching against a mocked KiCad library."""

import json
from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture
def library(tmp_path, monkeypatch):
    sizes = {}
    measured = []

    def add(folder, name, size=None):
        path = tmp_path / (folder + ".3dshapes") / (name + ".step")
        path.parent.mkdir(exist_ok=True)
        path.touch()
        sizes[str(path)] = size if size is not None else {"body": [1.6, .8, .4], "total": [1.6, .8, .4]}
        return path

    def measure(paths, library=None):
        measured.extend(paths)
        return {str(p): sizes[str(p)] for p in paths}

    monkeypatch.setattr(km, "model_sizes", measure)
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    return tmp_path, add, measured


def component(**kwargs):
    return dict(dict(kind="resistor", package="0603", length_mm=1.6,
                     width_mm=.8, bbox_basis="body"), **kwargs)


def supported(**kwargs):
    return component(**dict(dict(presence_confidence="high", class_confidence="medium",
        pin_count=2, pin_count_confidence="high", pin_count_source="photo_observation",
        terminal_count=2), **kwargs))


@pytest.mark.parametrize("package", ["", "two-terminal chip, approximately 1.6x0.8mm"])
def test_family_fallback_and_automatic_mechanical_estimate(library, package):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    add("Capacitor_SMD", "C_0603_1608Metric")
    c = supported(package=package, rotation_deg=37, model_rotation_deg=90)
    before = {key: c[key] for key in ("length_mm", "width_mm", "bbox_basis", "pin_count", "terminal_count")}
    assert km.attach_models([c], library=root) == 1
    assert c["model_match"]["status"] == "auto_matched"
    assert c["model_match"]["discovery"] == "family_dimensions"
    assert c["model_path"] == str(good)
    assert "model_selection" not in c and "model_rotation_deg" not in c
    assert c["rotation_deg"] == 37
    assert {key: c[key] for key in before} == before
    assert all(km._candidate_family(p) == "resistor" for p in measured)
    assert "identity and variant not verified" in c["model_match"]["candidates"][0]["reasons"][-1]


def test_literal_marking_selects_user_installed_module_model(library):
    root, add, measured = library
    exact = add("espressif", "ESP32-S3-WROOM-1U",
                {"total": [19.2, 18.0, 3.2], "body": [19.2, 18.0, 3.2]})
    component = dict(
        kind="module", **{"class": "rf_module"},
        package="Shielded castellated SMD module with U.FL/IPEX antenna connector",
        marking="ESPRESSIF ESP32-S3-WROOM-1U FCC ID:2AC7Z-ESPS3WROOM1U",
        length_mm=19.2, width_mm=18.0, bbox_basis="total",
        presence_confidence="high", class_confidence="high",
    )
    assert km.attach_models([component], library=root) == 1
    assert component["model_match"]["status"] == "auto_matched"
    assert component["model_path"] == str(exact)
    assert component["model_match"]["discovery_scope"] == "literal_marking_model"
    assert measured == [exact]


def _marked_module(**overrides):
    """Un modulo schermato la cui scritta nomina letteralmente un modello.

    Il riquadro fotografico (base 'body') e' volutamente diverso dalla sezione
    del modello: 21,0 x 14,0 contro 17,5 x 15,65, scarto XY 0,20 sul lato
    lungo. E' il caso della corsa reale: il rilevatore aveva disegnato il
    modulo piu' largo e meno alto di com'e', e i soli criteri dimensionali
    lo lasciavano provvisorio nonostante la marcatura letterale.
    """
    base = dict(kind="module", **{"class": "rf_module"},
                package="shielded SMD module with castellated edge pads",
                marking="ACME-RF-MOD-7A FCC ID:2AC7Z-MOD7A",
                length_mm=21.0, width_mm=14.0, bbox_basis="body",
                presence_confidence="high", class_confidence="high")
    base.update(overrides)
    return base


# Stessa forma che model_sizes produce per un modulo non standard: la lettura
# 'body' e' una sezione orizzontale non verificata, quella che i soli criteri
# dimensionali dell'automatico respingono su base 'body'.
_MARKED_SIZE = {"total": [18.0, 19.2, 3.2], "body": [15.65, 17.5, 3.2],
                "body_method": "section_65pct_unverified"}


def test_literal_marking_identity_is_independent_of_the_box(library):
    """@brief Positivo (decisione B): marcatura letterale unica -> auto_matched.

    La scoperta e' ristretta ai file nominati dalla scritta
    (discovery_scope 'literal_marking_model'), ne resta uno, la presenza e'
    'high' e la ricerca completa: l'identita' e' data dalla marcatura, non
    dal riquadro. La base 'body' con lettura di sezione non verificata e lo
    scarto XY del 20% -- che i criteri puramente dimensionali respingono --
    non contano piu': il riquadro serve solo a escludere un corpo che non
    puo' essere quel modello (entro DEFAULT_SIZE_TOLERANCE).
    """
    root, add, _ = library
    exact = add("vendor", "ACME-RF-MOD-7A", _MARKED_SIZE)
    c = _marked_module()
    assert km.attach_models([c], library=root) == 1
    report = c["model_match"]
    assert report["discovery_scope"] == "literal_marking_model"
    assert report["status"] == "auto_matched"
    assert report["identity_evidence"] == "literal_marking"
    assert report["attachment"] == "automatic"
    assert c["model_path"] == str(exact)
    choice = report["candidates"][0]
    assert choice["measurement_method"] == "section_65pct_unverified"
    assert 0.15 < choice["relative_xy_error"] <= km.DEFAULT_SIZE_TOLERANCE
    assert "literal model marking" in choice["reasons"][-1]


def test_two_models_for_the_same_marking_stay_provisional(library):
    """@brief Negativo (B): due file per la stessa scritta non sono un'identita'.

    Varianti che contengono entrambe il codice marcato: la marcatura non dice
    quale delle due c'e', quindi niente automatico. Resta la proposta
    provvisoria della marcatura (agganciata e dichiarata tale, decisione A).
    """
    root, add, _ = library
    add("vendor", "ACME-RF-MOD-7A", _MARKED_SIZE)
    add("vendor", "ACME-RF-MOD-7A-N8", _MARKED_SIZE)
    c = _marked_module()
    km.attach_models([c], library=root)
    report = c["model_match"]
    assert report["discovery_scope"] == "literal_marking_model"
    assert len(report["candidates"]) == 2
    assert report["status"] == "provisional_matched"
    assert report["attachment"] == "provisional"
    assert c["model_path"] == report["selected_path"]


def test_a_file_that_only_contains_the_marking_is_a_variant_not_an_identity(library):
    """@brief Negativo (B): sottostringa nel nome del file != identita'.

    Marcatura ``ACME-RF-MOD-7A``, in libreria il solo ``ACME-RF-MOD-7AU``:
    la scoperta lo trova (il codice marcato e' contenuto nel nome, e cosi'
    deve essere, perche' le varianti si nominano aggiungendo un suffisso),
    ma non e' il file che la scritta nomina. Caso reale: la scritta ``…-1``
    con installata la sola variante ``…-1U`` a connettore d'antenna, che ha
    un'altra geometria. Resta la proposta provvisoria (decisione A): il
    modello si aggancia dichiarandosi tale, senza identity_evidence.

    Positivo speculare: test_literal_marking_identity_is_independent_of_the_box
    (stessa marcatura, file ``ACME-RF-MOD-7A`` esatto -> auto_matched).
    """
    root, add, _ = library
    variant = add("vendor", "ACME-RF-MOD-7AU", _MARKED_SIZE)
    c = _marked_module()
    assert km._marked_model_matches(variant, c)
    assert not km._marked_model_is_named(variant, c)
    assert km.attach_models([c], library=root) == 1
    report = c["model_match"]
    assert report["discovery_scope"] == "literal_marking_model"
    assert len(report["candidates"]) == 1
    assert report["status"] == "provisional_matched"
    assert report["attachment"] == "provisional"
    assert "identity_evidence" not in report
    assert c["model_path"] == str(variant)
    assert "Exact literal" not in report["candidates"][0]["reasons"][-1]


def test_a_marked_body_outside_the_size_tolerance_is_not_that_model(library):
    """@brief Negativo (B): oltre DEFAULT_SIZE_TOLERANCE la marcatura non basta.

    Un riquadro che si scosta dal modello oltre la tolleranza generale e' un
    corpo che NON puo' essere quel modello: la scritta e' stata letta su
    qualcos'altro, o il riquadro e' sbagliato. Nessun candidato, quindi
    nessun automatismo e nessun provvisorio.
    """
    root, add, _ = library
    add("vendor", "ACME-RF-MOD-7A", _MARKED_SIZE)
    c = _marked_module(length_mm=26.0, width_mm=14.0)
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "unresolved"
    assert "model_path" not in c


@pytest.mark.parametrize("fields", [
    {"presence_confidence": "medium"}, {"presence_confidence": "low"},
    {"_yolo_review_only": True}, {"_yolo_origin": "low_confidence_review"},
    {"_global_inventory_status": "rejected"}])
def test_literal_marking_without_verified_presence_is_not_automatic(library, fields):
    """@brief Negativo (B): senza il corpo verificato la marcatura non ha un soggetto.

    La presenza 'high' resta richiesta: la scritta identifica un modello solo
    se il corpo su cui e' letta e' stato verificato. Con presenza inferiore o
    con un rilevamento da rivedere il modulo non e' automatico (ne'
    provvisorio, che pretende la stessa presenza).
    """
    root, add, _ = library
    add("vendor", "ACME-RF-MOD-7A", _MARKED_SIZE)
    c = _marked_module(**fields)
    km.attach_models([c], library=root)
    assert c["model_match"]["status"] == "review_required"
    assert c["model_match"]["selected_path"] is None
    assert "model_path" not in c


def test_literal_marking_identity_requires_a_complete_search(library, monkeypatch):
    """@brief Negativo (B): una ricerca troncata non dimostra l'unicita'.

    Se la misura di un file nominato dalla marcatura fallisce, la lista ha
    un solo candidato ma la ricerca non e' completa: il secondo file potrebbe
    essere quello giusto. Niente automatico; resta la proposta provvisoria
    della marcatura.
    """
    root, add, _ = library
    good = add("vendor", "ACME-RF-MOD-7A", _MARKED_SIZE)
    add("vendor", "ACME-RF-MOD-7A-N8", _MARKED_SIZE)
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {str(good): _MARKED_SIZE})
    c = _marked_module()
    km.attach_models([c], library=root)
    assert len(c["model_match"]["candidates"]) == 1
    assert c["model_match"]["status"] == "provisional_matched"


def test_three_lead_tab_unknown_semiconductor_gets_sot223_preview(library):
    """@brief La morfologia a tre reofori e linguetta propone il SOT-223.

    Decisione dell'utente (A): la proposta provvisoria torna ad agganciare
    il modello, dichiarandolo tale. Lasciare il corpo generico per un solido
    compatibile non aggiungeva informazione: il dubbio sull'identita' lo
    porta gia' il report (status provisional_matched, attachment
    'provisional'), e il selettore puo' cambiare il modello.
    """
    root, add, _ = library
    sot = add("Package_TO_SOT_SMD", "SOT-223",
              {"total": [7.0, 6.5, 1.7]})
    add("Package_TO_SOT_SMD", "LFPAK88", {"total": [8.0, 8.0, 1.6]})
    c = dict(kind="ic", **{"class": "semiconductor_unknown"}, package="",
             terminal_layout="Three wide gull-wing leads; one large rectangular tab.",
             package_evidence="Three leads and one large tab are visible.",
             length_mm=7.37, width_mm=6.91, bbox_basis="total",
             presence_confidence="high", class_confidence="low")
    assert km.attach_models([c], library=root) == 1
    assert c["model_match"]["status"] == "provisional_matched"
    assert c["model_match"]["selected_path"] == str(sot)
    assert c["model_path"] == str(sot)
    assert c["model_match"]["attachment"] == "provisional"
    assert c["model_review"] == "provisional_matched"
    assert "model variant" in c["model_match"]["candidates"][0]["reasons"][-1].lower() or \
           "package" in c["model_match"]["candidates"][0]["reasons"][-1].lower()


def test_detector_only_yolo_hypothesis_stays_in_report_but_not_3d(library):
    root, add, _ = library
    c = dict(kind="generic", name="YOLO candidate 43", package="",
             length_mm=11.47, width_mm=8.53, bbox_basis="unknown",
             presence_confidence=None, class_confidence=None)
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "unresolved"
    assert c["render_3d"] is False
    assert c["color_hex"] == "#FF00FF"
    assert c["render_placeholder"] is True and c["model_review"] == "unresolved"


def test_detector_only_hypothesis_in_review_is_still_omitted_from_3d(library):
    """@brief Negativo: l'omissione dal 3D non dipende dal colore.

    Un'ipotesi del solo rilevatore con candidati e' review_required, non piu'
    magenta: deve restare fuori dal solido esattamente come prima, perche'
    il motivo (una proposta non classificata) non e' cambiato.
    """
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    c = component(name="YOLO candidate 7", color_hex="#4a4a4a")
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "review_required"
    assert c["render_3d"] is False
    assert c["color_hex"] == "#4a4a4a"


def test_the_magenta_placeholder_is_reserved_for_the_unresolved(library):
    """@brief Il magenta dice "non so nulla di questo corpo", non "e' in revisione".

    Positivo/negativo sullo stesso colore campionato dalla foto: un corpo con
    candidati (review_required) lo conserva e dichiara lo stato nei campi
    espliciti; un corpo senza candidati (unresolved) lo perde a favore del
    segnaposto, e la provenienza del colore lo dice. Prima entrambi
    diventavano magenta, e il colore vero campionato dalla foto andava perso
    per ripetere un'informazione che model_match.status gia' porta.
    """
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    reviewed = component(color_hex="#5a4b3c", _color_source="photo_median")
    unresolved = component(length_mm=10, color_hex="#5a4b3c", _color_source="photo_median")
    assert km.attach_models([reviewed, unresolved], library=root) == 0
    assert reviewed["model_match"]["status"] == "review_required"
    assert reviewed["color_hex"] == "#5a4b3c"
    assert reviewed["_color_source"] == "photo_median"
    assert reviewed["render_placeholder"] is False
    assert reviewed["model_review"] == "review_required"
    assert reviewed["model_match"]["render_placeholder"] is False
    assert unresolved["model_match"]["status"] == "unresolved"
    assert unresolved["color_hex"] == "#FF00FF"
    assert unresolved["_color_source"] == "placeholder_unresolved"
    assert unresolved["render_placeholder"] is True
    assert unresolved["model_review"] == "unresolved"
    assert unresolved["model_match"]["render_placeholder"] is True


def test_a_provisional_proposal_keeps_the_photo_colour_and_attaches_declared(library):
    """@brief provisional_matched: geometria nota, identita' no, modello applicato ma dichiarato.

    Decisione dell'utente (A): il provvisorio aggancia il model_path come
    l'automatico, ma il report lo dice (attachment 'provisional', con la
    ragione), il colore campionato dalla foto resta e nulla e' magenta.
    Positivo/negativo sullo stesso stato: con evidenza indipendente
    (conteggio osservato) la stessa lista diventa auto_matched, attachment
    'automatic'; con la sola geometria resta provvisoria. E, nel negativo
    che segue, review_required non aggancia nulla e unresolved e' magenta.
    """
    root, add, _ = library
    first = add("Button_Switch_SMD", "SW_SPST_EVQP7A", {"total": [4.7, 3.55, 1.35]})
    add("Button_Switch_SMD", "SW_SPST_EVQP7C", {"total": [4.7, 3.55, 1.85]})
    proposal = supported(kind="switch", package="small tactile switch",
                         length_mm=4.74, width_mm=3.53, bbox_basis="total",
                         color_hex="#2b2b2b", _color_source="photo_median",
                         model_rotation_deg=90)
    assert km.attach_models([proposal], library=root) == 1
    assert proposal["model_match"]["status"] == "provisional_matched"
    assert proposal["model_match"]["selected_path"] == str(first)
    assert proposal["model_path"] == str(first)
    assert proposal["model_match"]["attachment"] == "provisional"
    assert "PROVISIONALLY" in proposal["model_match"]["attachment_reason"]
    assert "picker can change it" in proposal["model_match"]["attachment_reason"]
    assert "provisional" in proposal["model_match"]["candidates"][0]["reasons"][-1].lower()
    assert proposal["color_hex"] == "#2b2b2b" and proposal["_color_source"] == "photo_median"
    assert proposal["render_placeholder"] is False
    assert proposal["model_review"] == "provisional_matched"
    # Un modello e' agganciato: l'angolo assoluto di una corsa precedente era
    # stato calcolato su un altro modello e va tolto, come per auto_matched.
    assert "model_rotation_deg" not in proposal

    accepted = supported(package="", color_hex="#2b2b2b")
    add("Resistor_SMD", "R_0603_1608Metric")
    assert km.attach_models([accepted], library=root) == 1
    assert accepted["model_match"]["status"] == "auto_matched"
    assert accepted["model_path"] == accepted["model_match"]["selected_path"]
    assert accepted["model_match"]["attachment"] == "automatic"
    assert accepted["render_placeholder"] is False


def test_review_required_attaches_nothing_and_unresolved_is_magenta(library):
    """@brief Negativo della decisione A: solo il provvisorio aggancia.

    Un corpo con candidati ma senza proposta (review_required) resta senza
    model_path e conserva il colore della foto; un corpo senza candidati
    (unresolved) e' magenta. La regola del magenta non cambia con A.
    """
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    reviewed = component(color_hex="#5a4b3c", _color_source="photo_median")
    unresolved = component(length_mm=10, color_hex="#5a4b3c", _color_source="photo_median")
    assert km.attach_models([reviewed, unresolved], library=root) == 0
    assert reviewed["model_match"]["status"] == "review_required"
    assert "model_path" not in reviewed and "attachment" not in reviewed["model_match"]
    assert reviewed["color_hex"] == "#5a4b3c" and reviewed["render_placeholder"] is False
    assert unresolved["model_match"]["status"] == "unresolved"
    assert "model_path" not in unresolved
    assert unresolved["color_hex"] == "#FF00FF" and unresolved["render_placeholder"] is True


def test_same_family_switches_share_common_preview_variant(library):
    """@brief Due pulsanti provvisori della stessa famiglia propongono un candidato comune.

    Contratto aggiornato su due punti. La proposta condivisa resta provvisoria
    come quella del singolo e, per la decisione dell'utente (A), aggancia il
    modello comune come model_path dichiarandosi provvisoria: il selettore puo'
    cambiarla. E il secondo pulsante misura ora 4,95 x 3,05 invece di
    4,84 x 3,12: con la vecchia misura la sua banda di pareggio conteneva un
    terzo solido di inviluppo diverso (4,6 x 3,2 a meno del margine dai
    5,0 x 3,0), quindi con la banda che guarda tutti i candidati non e' piu'
    una proposta ma una revisione -- vedi il test negativo che segue.
    """
    root, add, _ = library
    add("Button_Switch_SMD", "SW_SPST_EVQP7A", {"total": [4.7, 3.55, 1.35]})
    add("Button_Switch_SMD", "SW_SPST_EVQP7C", {"total": [4.7, 3.55, 1.85]})
    common = add("Button_Switch_SMD", "SW_SPST_PTS810", {"total": [4.6, 3.2, 2.5]})
    add("Button_Switch_SMD", "SW_SPST_TS-1088-xR020", {"total": [5.0, 3.0, 2.0]})
    add("Button_Switch_SMD", "SW_SPST_TS-1088-xR025", {"total": [5.0, 3.0, 2.5]})
    first = dict(kind="switch", package="SMD tactile switch (~4.7x3.5mm visible)",
                 length_mm=4.74, width_mm=3.53, bbox_basis="total",
                 presence_confidence="high", class_confidence="medium")
    second = dict(kind="switch", package="SMD tactile switch (~4.9x3.1mm visible)",
                  length_mm=4.95, width_mm=3.05, bbox_basis="total",
                  presence_confidence="high", class_confidence="medium")
    assert km.attach_models([first, second], library=root) == 2
    assert first["model_match"]["selected_path"] == second["model_match"]["selected_path"] == str(common)
    assert first["model_path"] == second["model_path"] == str(common)
    assert first["model_match"]["attachment"] == second["model_match"]["attachment"] == "provisional"
    assert first["model_match"]["status"] == second["model_match"]["status"] == "provisional_matched"
    assert first["model_match"]["shared_preview_group"] == "smd tactile switch"


def test_a_switch_whose_band_is_mixed_does_not_join_the_shared_proposal(library):
    """@brief Negativo: chi non e' una proposta per conto suo non entra nel gruppo.

    Stessa libreria, ma il secondo pulsante misura 4,84 x 3,12: la sua banda
    contiene le due varianti 5,0 x 3,0 e il 4,6 x 3,2, cioe' due inviluppi
    che la fotografia non distingue. Resta review_required, senza candidato
    condiviso, e il primo pulsante -- solo nel suo gruppo -- tiene la sua
    proposta individuale.
    """
    root, add, _ = library
    first_choice = add("Button_Switch_SMD", "SW_SPST_EVQP7A", {"total": [4.7, 3.55, 1.35]})
    add("Button_Switch_SMD", "SW_SPST_EVQP7C", {"total": [4.7, 3.55, 1.85]})
    add("Button_Switch_SMD", "SW_SPST_PTS810", {"total": [4.6, 3.2, 2.5]})
    add("Button_Switch_SMD", "SW_SPST_TS-1088-xR020", {"total": [5.0, 3.0, 2.0]})
    add("Button_Switch_SMD", "SW_SPST_TS-1088-xR025", {"total": [5.0, 3.0, 2.5]})
    first = dict(kind="switch", package="SMD tactile switch (~4.7x3.5mm visible)",
                 length_mm=4.74, width_mm=3.53, bbox_basis="total",
                 presence_confidence="high", class_confidence="medium")
    mixed = dict(kind="switch", package="SMD tactile switch (~4.8x3.1mm visible)",
                 length_mm=4.84, width_mm=3.12, bbox_basis="total",
                 presence_confidence="high", class_confidence="medium")
    assert km.attach_models([first, mixed], library=root) == 1
    assert mixed["model_match"]["status"] == "review_required"
    assert mixed["model_match"]["selected_path"] is None
    assert "model_path" not in mixed
    assert len(km._tie_band(mixed["model_match"]["candidates"])) == 3
    assert first["model_match"]["status"] == "provisional_matched"
    assert first["model_match"]["selected_path"] == str(first_choice)
    assert "shared_preview_group" not in first["model_match"]


def test_same_xy_variants_attach_a_reviewable_preview_model(library):
    """@brief Due varianti con lo stesso ingombro XY: modello applicato, ma provvisorio.

    Decisione dell'utente (A): il candidato provvisorio e' agganciato come
    model_path e dichiarato tale nel report; resta una proposta per il
    selettore, non un'identita' verificata.
    """
    root, add, measured = library
    first = add("Button_Switch_SMD", "SW_SPST_EVQP7A",
                {"total": [4.7, 3.55, 1.35]})
    add("Button_Switch_SMD", "SW_SPST_EVQP7C",
        {"total": [4.7, 3.55, 1.85]})
    c = supported(kind="switch", package="small tactile switch",
                  length_mm=4.74, width_mm=3.53, bbox_basis="total")
    assert km.attach_models([c], library=root) == 1
    assert c["model_match"]["status"] == "provisional_matched"
    assert c["model_match"]["selected_path"] == str(first)
    assert c["model_path"] == str(first)
    assert c["model_match"]["attachment"] == "provisional"
    assert c["model_match"]["candidates"][0]["status"] == "provisional_matched"
    assert "variant" in c["model_match"]["candidates"][0]["reasons"][-1]
    assert measured == [first, first.parent / "SW_SPST_EVQP7C.step"]


@pytest.mark.parametrize("fields", [
    {"presence_confidence": "low"}, {"presence_confidence": None},
    {"class_confidence": "low"}, {"class_confidence": None},
    {"_yolo_review_only": True}, {"_yolo_origin": "low_confidence_review"},
    {"kind": "unknown"}, {"kind": "other"}, {"kind": "semiconductor_unknown"},
    {"bbox_basis": "unknown"}, {"pin_count_confidence": "low"},
    {"terminal_count": 3}, {"terminal_count": True}, {"pin_count": 3}])
def test_auto_requires_independent_supported_evidence(library, fields):
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    c = supported(**fields)
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] != "confirmed"
    assert c["model_match"]["selected_path"] is None


def test_auto_never_uses_ui_limit_to_hide_ambiguity(library):
    root, add, _ = library
    for i in range(5):
        add("Resistor_SMD", f"R_0603_variant{i}")
    c = supported()
    assert len(km.model_candidates(c, library=root)["candidates"]) == 5
    report = km.model_candidates(c, library=root, limit=1)
    assert report["status"] == "review_required" and report["selected_path"] is None


def test_truncated_family_search_never_auto_selects(library):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    for i in range(80):
        add("Resistor_SMD", f"Other_{i}", {"body": [10, 10]})
    report = km.model_candidates(supported(package=""), library=root)
    assert measured[0] == good
    assert measured == [good]
    assert report["search_truncated"] and report["status"] == "review_required"


def test_unreadable_alternative_prevents_auto(library, monkeypatch):
    root, add, _ = library
    good = add("Resistor_SMD", "R_0603")
    add("Resistor_SMD", "R_0603_Unreadable")
    monkeypatch.setattr(km, "model_sizes", lambda *a: {str(good): {"body": [1.6, .8]}})
    assert km.model_candidates(supported(), library=root)["status"] == "review_required"


@pytest.mark.parametrize("status", ["rejected", "confirmed"])
def test_explicit_choice_wins_over_auto_eligible(library, status):
    root, add, measured = library
    add("Resistor_SMD", "R_0603")
    choice = add("Custom", "Chosen")
    c = supported(model_selection={"status": status, "path": str(choice), "rotation_deg": 0})
    assert km.attach_models([c], library=root) == (status == "confirmed")
    assert not measured
    assert c["model_match"]["status"] == ("confirmed" if status == "confirmed" else "unresolved")


@pytest.mark.parametrize("text", ["SOT23/SC70", "SOT-23-6 / SC-70-6 class",
    "small outline (SOT23 or SC70)"])
def test_sot_sc_ambiguous_prose_preserved(text):
    assert km._package_token(text) == ""


def test_sc_terminal_variant_and_metric_namespace(library):
    root, add, _ = library
    good = add("Package_TO_SOT_SMD", "SC-70-6")
    add("Package_TO_SOT_SMD", "SC-70-5")
    c = component(kind="semiconductor_unknown", package="small outline SC70-6 package",
                  pin_count=6, pin_count_confidence="high")
    assert [x["path"] for x in km.model_candidates(c, library=root)["candidates"]] == [str(good)]
    assert km._package_token("chip 0603 metric") == "0603Metric"


def test_ambiguous_prose_searches_only_declared_alternatives(library):
    root, add, measured = library
    expected = {add("Package_TO_SOT_SMD", "SOT-23-6"),
                add("Package_TO_SOT_SMD", "SC-70-6")}
    add("Package_TO_SOT_SMD", "SOT-223-6")
    add("Package_DFN_QFN", "QFN-6_1.6x0.8mm")
    c = supported(kind="ic", package="small outline SOT23-6 / SC70-6 class",
                  pin_count=6, terminal_count=6)
    report = km.model_candidates(c, library=root)
    assert set(measured) == expected
    assert report["status"] == "review_required"
    assert {item["path"] for item in report["candidates"]} == {str(p) for p in expected}


def test_cached_family_dimensions_keep_basis_and_allow_complete_search(library, monkeypatch):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    bad = add("Resistor_SMD", "Other", {"body": [10, 10], "total": [1.6, .8]})
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(good): {"body": [1.6, .8]},
        km.index_key(bad): {"body": [10, 10], "total": [1.6, .8]}})
    report = km.model_candidates(supported(package=""), library=root)
    assert measured == [good]
    assert report["status"] == "auto_matched" and not report.get("search_truncated")


def test_cold_structured_switch_discovery_is_bounded_and_never_proves_uniqueness(library):
    root, add, measured = library
    for i in range(90):
        add("Button_Switch_SMD", f"Switch_Vendor{i}")
    report = km.model_candidates(component(kind="switch", package="small tactile switch"), library=root)
    assert len(measured) == km.MAX_CANDIDATE_MEASUREMENTS
    assert report["status"] == "review_required"
    assert report["selected_path"] is None
    assert report["search_truncated"]


def test_structural_rejection_logging_is_bounded(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    for i in range(100):
        add("Fuse", f"Fuse_0603_{i}")
    logs = []
    report = km.model_candidates(component(), library=root, log=logs.append)
    assert report["rejected_candidate_count"] == 100
    assert len(logs) == 2


@pytest.mark.parametrize("observed_class", ["semiconductor_unknown", "transistor", "regulator"])
def test_production_ic_kind_preserves_semiconductor_class(library, monkeypatch, observed_class):
    root, add, measured = library
    sot = add("Package_TO_SOT_SMD", "SOT-23-6")
    wrong_count = add("Package_TO_SOT_SMD", "SOT-23-5")
    so = add("Package_SO", "SOIC-6")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (sot, wrong_count, so)})
    c = supported(kind="ic", package="", pin_count=6, terminal_count=6,
                  **{"class": observed_class})
    report = km.model_candidates(c, library=root)
    expected = {sot, so} if observed_class == "semiconductor_unknown" else {sot}
    assert set(measured) == expected
    assert {x["path"] for x in report["candidates"]} == {str(p) for p in expected}
    assert c["kind"] == "ic" and c["class"] == observed_class
    if observed_class == "semiconductor_unknown":
        assert report["status"] == "review_required"


@pytest.mark.parametrize("package", ["QFN-style leadless body", "QFN/LQFN-style plastic package"])
def test_semiconductor_unknown_class_keeps_qfn_prose_narrowing(library, monkeypatch, package):
    root, add, measured = library
    qfn = add("Package_DFN_QFN", "QFN-6")
    sot = add("Package_TO_SOT_SMD", "SOT-23-6")
    so = add("Package_SO", "SOIC-6")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (qfn, sot, so)})
    c = supported(kind="ic", package=package, pin_count=6, terminal_count=6,
                  **{"class": "semiconductor_unknown"})
    report = km.model_candidates(c, library=root)
    assert measured == [qfn]
    assert report["status"] == "review_required" and report["selected_path"] is None


@pytest.mark.parametrize("class_fields", [{}, {"class": "ic"}, {"class": "semiconductor_unknown"}])
def test_generic_ic_never_fabricates_unique_tssop_by_omitting_sot(library, monkeypatch, class_fields):
    root, add, measured = library
    tssop = add("Package_SO", "TSSOP-6")
    sot = add("Package_TO_SOT_SMD", "SOT-23-6")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (tssop, sot)})
    c = supported(kind="ic", package="", pin_count=6, terminal_count=6, **class_fields)
    assert km.attach_models([c], library=root) == 0
    assert set(measured) == {tssop, sot}
    report = c["model_match"]
    assert {x["path"] for x in report["candidates"]} == {str(tssop), str(sot)}
    assert report["status"] == "review_required" and report["selected_path"] is None
    assert "model_path" not in c


@pytest.mark.parametrize("count,layout", [
    (4, "three broad leads on one edge and one wide tab on the opposite edge"),
    (6, "three visible terminals on each of two opposite sides")])
def test_provisional_observed_morphology_filters_unknown_class(library, monkeypatch, count, layout):
    root, add, measured = library
    good = add("Package_TO_SOT_SMD", f"SOT-23-{count}")
    htssop = add("Package_SO", "HTSSOP-24-1EP_4.4x7.8mm")
    bga = add("Package_BGA", "NXP_SOT1982-1_VFBGA-98_7x7mm")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (good, htssop, bga)})
    c = supported(kind="ic", package="", confidence="low", class_confidence="low",
                  pin_count=count, pin_count_confidence="low", terminal_count=count,
                  terminal_layout=layout, **{"class": "semiconductor_unknown"})
    assert km._observed_count(c) == set()
    assert km.attach_models([c], library=root) == 0
    assert measured == [good]
    assert c["model_match"]["status"] == "review_required"
    assert c["model_match"]["rejected_candidate_count"] == 2
    assert c["terminal_count"] == count and c["pin_count_confidence"] == "low"


@pytest.mark.parametrize("fields", [
    {"presence_confidence": "low"}, {"terminal_layout": ""},
    {"terminal_layout": "unknown"}, {"terminal_layout": None},
    {"terminal_count": True}, {"terminal_count": 0}, {"terminal_count": "4"}])
def test_provisional_count_requires_presence_and_valid_observed_layout(library, fields):
    _, add, _ = library
    path = add("Package_BGA", "BGA-98")
    c = component(**dict(dict(kind="ic", pin_count=4, pin_count_confidence="low",
        terminal_count=4, terminal_layout="three leads and one tab", presence_confidence="high"), **fields))
    assert km._compatible_candidates([path], c, lambda message: None) == [path]


def test_provisional_conflict_does_not_override_confident_count(library):
    _, add, _ = library
    paths = [add("Package_SO", "TSSOP-6"), add("Package_SO", "TSSOP-4")]
    c = supported(kind="ic", pin_count=6, terminal_count=4,
                  terminal_layout="three leads and one tab")
    assert km._compatible_candidates(paths, c, lambda message: None) == []


def test_provisional_count_alone_never_enables_auto(library):
    root, add, _ = library
    add("Package_SO", "TSSOP-6")
    c = supported(kind="ic", package="TSSOP-6", pin_count=6, terminal_count=6,
                  pin_count_confidence="low", terminal_layout="three leads each side")
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "review_required"


@pytest.mark.parametrize("name,expected", [
    ("HTSSOP-24-1EP_4.4x7.8mm", 24),
    ("NXP_SOT1982-1_VFBGA-98_7x7mm", 98),
    ("Vendor_TQFP-48_7x7mm", 48), ("Vendor_LQFP-64_10x10mm", 64),
    ("NXP_SOT1982-1", None), ("Vendor_TO-263-3_TabPin2", None),
    ("VendorNotVFBGA-98", None), ("Vendor_VFBGA-98ABC", None),
    ("SOT-23-6", 6), ("SOT-223", None)])
def test_bounded_embedded_package_count_tokens(name, expected):
    from pathlib import Path
    assert km._declared_count(Path(name + ".step")) == expected


def test_confident_raw_u4_count_filters_vendor_packages(library):
    _, add, _ = library
    htssop = add("Package_SO", "HTSSOP-24-1EP_4.4x7.8mm")
    bga = add("Package_BGA", "NXP_SOT1982-1_VFBGA-98_7x7mm")
    tab = add("Package_TO_SOT_SMD", "TO-263-3_TabPin2")
    c = component(kind="ic", pin_count=4, pin_count_confidence="medium",
                  pin_count_source="photo_observation", confidence="medium",
                  terminal_count=4, presence_confidence="high", class_confidence="low",
                  terminal_layout="three leads and a single wide tab",
                  **{"class": "semiconductor_unknown"})
    assert km._observed_count(c) == {4}
    assert km._compatible_candidates([htssop, bga, tab], c, lambda message: None) == [tab]


def test_structural_filters_before_measurement(library):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    add("Mounting", "Screw_ABC0603")
    add("Mounting", "Screw_0603")
    add("Fuse", "Fuse_0603")
    add("Resistor_SMD", "R_Array_0603")
    add("Capacitor_SMD", "C_0603")
    report = km.model_candidates(component(), library=root)
    assert measured == [good]
    assert report["status"] == "review_required"
    assert report["selected_path"] is None
    assert [c["path"] for c in report["candidates"]] == [str(good)]
    json.dumps(report, allow_nan=False)


def test_capacitor_never_matches_fuse(library):
    root, add, measured = library
    add("Fuse", "Fuse_0603")
    good = add("Capacitor_SMD", "C_0603")
    assert km.model_candidates(component(kind="capacitor"), library=root)["candidates"][0]["path"] == str(good)
    assert measured == [good]


def test_unknown_family_does_not_admit_hardware_or_unknown_numeric_models(library):
    root, add, measured = library
    add("Mounting", "Screw_0603")
    add("Custom", "Mystery_0603")
    assert km.model_candidates(component(kind="unknown"), library=root)["status"] == "unresolved"
    assert not measured


@pytest.mark.parametrize("basis,expected", [("body", "review_required"), ("total", "unresolved"),
                                           ("leads", "unresolved"), ("body_and_leads", "unresolved"),
                                           ("pads", "unresolved"), ("unknown", "review_required")])
def test_measurement_basis_is_strict(library, basis, expected):
    root, add, _ = library
    add("Resistor_SMD", "R_0603", {"body": [1.6, .8], "total": [3, 2]})
    assert km.model_candidates(component(bbox_basis=basis), library=root)["status"] == expected


def test_no_forced_alternatives_and_bounded_measurement(library):
    root, add, measured = library
    for i in range(100):
        add("Resistor_SMD", f"R_0603_{i:03}")
    report = km.model_candidates(component(), limit=999, library=root)
    assert len(report["candidates"]) == 5
    assert len(measured) == km.MAX_CANDIDATE_MEASUREMENTS
    assert km.model_candidates(component(length_mm=10), library=root)["candidates"] == []


def test_attach_stores_review_report_and_clears_stale_import(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    c = component(model_path="stale.step", model_rotation_deg=90)
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "review_required"
    # Contratto aggiornato: in revisione il colore non viene toccato (qui non
    # c'era, e non ne compare uno); lo stato si legge dai campi espliciti.
    assert "color_hex" not in c
    assert c["render_placeholder"] is False and c["model_review"] == "review_required"
    assert "model_path" not in c and c["model_rotation_deg"] == 90


def test_explicit_selection_and_local_rotation(library):
    root, add, measured = library
    selected = add("Custom", "ExactChoice")
    c = component(rotation_deg=37, model_selection={"status": "confirmed", "path": str(selected), "rotation_deg": 90})
    assert km.attach_models([c], library=root) == 1
    assert c["model_path"] == str(selected)
    assert c["model_rotation_deg"] == 90 and c["rotation_deg"] == 37
    assert c["model_match"]["status"] == "confirmed"
    assert c["model_match"]["selected_path"] == str(selected)
    assert not measured


@pytest.mark.parametrize("rotation_fields,expected", [({}, None),
    ({"rotation_deg": None}, None), ({"rotation_deg": 0}, 0)])
def test_confirmed_automatic_rotation_is_distinct_from_explicit_zero(library, rotation_fields, expected):
    root, add, measured = library
    selected = add("Custom", "ExactChoice")
    c = component(rotation_deg=37, model_rotation_deg=90,
        model_selection={"status": "confirmed", "path": str(selected), **rotation_fields})
    assert km.attach_models([c], library=root) == 1
    assert c["model_path"] == str(selected) and c["model_match"]["status"] == "confirmed"
    assert c["rotation_deg"] == 37
    if expected is None:
        assert "model_rotation_deg" not in c
    else:
        assert c["model_rotation_deg"] == 0
    assert not measured


@pytest.mark.parametrize("status,path,rotation", [("rejected", "R_0603.step", 0),
    ("confirmed", "missing.step", 0), ("confirmed", "", 0), ("confirmed", "R_0603.step", float("nan"))])
def test_rejected_or_invalid_selection_never_substituted(library, status, path, rotation):
    root, add, measured = library
    real = add("Resistor_SMD", "R_0603")
    c = component(model_path=str(real), model_selection={"status": status,
        "path": str(real.parent / path), "rotation_deg": rotation})
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "unresolved"
    assert c["color_hex"] == "#FF00FF"
    assert "model_path" not in c and not measured


def test_real_size_cache_reused(tmp_path, monkeypatch):
    path = tmp_path / "Resistor_SMD.3dshapes" / "R_0603.step"
    path.parent.mkdir()
    path.touch()
    monkeypatch.setattr(km, "load_index", lambda *a: {km.index_key(path): {"body": [1.6, .8]}})
    monkeypatch.setattr(km, "measure_models", lambda *a: pytest.fail("Cached sizes must not be remeasured"))
    assert km.model_candidates(component(), library=tmp_path)["status"] == "review_required"


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_dimensions_are_json_safe_and_do_not_measure(library, value):
    root, add, measured = library
    add("Resistor_SMD", "R_0603")
    result = km.model_candidates(component(length_mm=value), library=root)
    assert result["status"] == "unresolved" and not measured
    json.dumps(result, allow_nan=False)


def test_named_package_numbers_have_boundaries(library):
    root, add, _ = library
    good = add("Package_TO_SOT_SMD", "SOT-23")
    add("Package_TO_SOT_SMD", "SOT-230")
    assert km.candidates("SOT-23", root) == [good]


def test_ambiguous_topologies_are_candidates_not_confirmation(library):
    root, add, _ = library
    for count in (24, 32):
        add("Package_DFN_QFN", f"QFN-{count}")
    c = component(kind="ic_qfn", package="QFN")
    report = km.model_candidates(c, library=root)
    assert len(report["candidates"]) == 2
    assert report["status"] == "review_required" and report["selected_path"] is None
    c.update(pin_count=32, pin_count_confidence="high")
    report = km.model_candidates(c, library=root)
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["package"] == "QFN-32"


def test_package_family_filters_even_without_kind(library):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603")
    add("Fuse", "R_0603_Fuse")
    result = km.model_candidates(component(kind="unknown", package="R_0603"), library=root)
    assert [c["path"] for c in result["candidates"]] == [str(good)]
    assert measured == [good]


def test_missing_body_does_not_use_total(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603", [1.6, .8])
    assert km.model_candidates(component(), library=root)["candidates"] == []


def test_pads_require_a_measured_solid_and_keep_variants_separate(library, monkeypatch):
    root, add, _ = library
    p = add("Resistor_SMD", "R_0603", {"total": [4, 3]})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {str(p): [
        {"source": "A", "size": [1.6, 4]}, {"source": "B", "size": [4, .8]}]})
    assert km.model_candidates(component(bbox_basis="pads"), library=root)["candidates"] == []
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {str(p): [
        {"source": "C", "size": [1.6, .8]}]})
    assert km.model_candidates(component(bbox_basis="pads"), library=root)["candidates"][0]["measurement_basis"] == "piazzole C"
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {})
    assert km.model_candidates(component(bbox_basis="pads"), library=root)["candidates"] == []


def test_attach_models_counts_components_for_an_honest_progress_bar(library):
    """@brief L'unico passo della generazione con un totale vero.

    Ogni componente puo' costare una misura CAD, quindi il conto avanza al
    ritmo del lavoro. Una percentuale stimata sul tempo, invece, si ferma al
    90% quando una chiamata e' piu' lenta del solito e dice meno del nulla.
    """
    root, add, _ = library
    add("Resistor_SMD", "R_0603_1608Metric")
    seen = []
    km.attach_models([supported(), supported(), "non e' un componente"],
                     library=root, progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert all(total == 3 for _, total in seen)


def test_attach_models_without_a_progress_callback_still_works(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603_1608Metric")
    assert km.attach_models([supported()], library=root) == 1


def _usb_library(library, second_size=(8.94, 6.90, 4.26)):
    """Due ricettacoli della stessa famiglia, uno chiaramente piu' vicino."""
    root, add, measured = library
    best = add("Connector_USB", "USB_C_Receptacle_A_6P", {"body": [11.14, 6.90, 3.26]})
    other = add("Connector_USB", "USB_C_Receptacle_B_16P", {"body": list(second_size)})
    component = supported(kind="connector", length_mm=10.67, width_mm=6.87,
                          package="USB Type-C receptacle, SMD with through-hole shell tabs")
    return root, best, other, component


def test_a_dominant_candidate_earns_a_preview_like_a_tie_does(library):
    """@brief Il pareggio e la dominanza sono le due facce della stessa cosa.

    Il pareggio dice "la fotografia non distingue queste due varianti"; la
    dominanza dice "la fotografia le ha distinte, e una sola regge". In
    entrambi i casi l'incertezza residua riguarda identita' e variante, non la
    geometria XY che si sta mostrando. Riconoscendo solo il pareggio, un
    ricettacolo USB-C con un candidato al 4% e uno al 16% non otteneva alcuna
    anteprima, mentre sarebbe bastato aggiungere un doppione identico del
    migliore per ottenerla: le liste nitide erano punite e quelle ambigue
    premiate.
    """
    root, best, _, component = _usb_library(library)
    report = km.model_candidates(component, library=root)
    assert report["status"] == "provisional_matched"
    assert report["selected_path"] == str(best)
    assert "review" in report["candidates"][0]["reasons"][-1].lower()


def test_close_candidates_that_neither_tie_nor_dominate_stay_in_review(library):
    """Caso negativo strutturale: nessuno domina e nessuno pareggia.

    Il secondo dista meno del margine di pareggio dal primo, quindi la misura
    non lo ha scartato; ma i due ingombri non coincidono, quindi non sono due
    varianti dello stesso corpo. Non c'e' niente da mostrare senza scegliere.
    """
    root, _, _, component = _usb_library(library, second_size=(10.30, 6.90, 4.26))
    report = km.model_candidates(component, library=root)
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


def test_dominance_over_models_nobody_measured_is_not_dominance(library, monkeypatch):
    """Caso negativo: stessa dominanza, ma la ricerca non e' completa.

    Superare alternative che nessuno ha misurato non e' superarle. Il pareggio
    resta invece ammesso su una ricerca troncata, perche' non afferma nulla su
    cio' che non e' stato misurato.
    """
    root, best, other, component = _usb_library(library)
    _, add, _ = library
    unreadable = add("Connector_USB", "USB_C_Receptacle_C_Unreadable")
    readable = {str(best): {"body": [11.14, 6.90, 3.26]},
                str(other): {"body": [8.94, 6.90, 4.26]}}
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        str(p): readable[str(p)] for p in paths if str(p) in readable})

    report = km.model_candidates(component, library=root)

    assert unreadable.exists()
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


def _module(**changes):
    """@brief Un modulo radio come lo consegna l'inventario fotografico."""
    row = dict(kind="rf_module", package="shielded module with attached coaxial jack",
               length_mm=18.08, width_mm=16.33, bbox_basis="body",
               presence_confidence="high", class_confidence="medium")
    row.update(changes)
    return row


def test_between_two_solids_the_photo_cannot_separate_the_height_decides(library):
    """@brief La vista dall'alto non misura l'altezza: dentro la banda decide lei.

    Numeri della corsa vera: modulo alto ~3,1 mm, rettangolo fotografico
    18,08 x 16,33. Sull'ingombro il solido piatto sbaglia 0,0876 e quello alto
    0,1023 -- distanza 0,0146, cioe' il 29% del margine entro cui il codice
    stesso dichiara di non saper distinguere. Dentro quella banda vinceva il
    piatto per una frazione di percento; l'altezza, che la vista dall'alto non
    misura affatto, li separa di venti volte tanto.
    """
    root, add, _ = library
    flat = add("RF_Module", "FLAT-RADIO_16.5x16.0mm",
               {"body": [16.5, 16.0, 1.3], "total": [16.5, 16.0, 1.3]})
    tall = add("RF_Module", "TALL-MODULE_18.0x19.2mm",
               {"body": [18.0, 19.2, 3.2], "total": [18.0, 19.2, 3.2]})
    report = km.model_candidates(
        _module(height_mm=3.1, height_source="visual_estimate"), library=root)
    order = [item["package"] for item in report["candidates"]]
    xy = {item["package"]: item["relative_xy_error"] for item in report["candidates"]}
    assert xy["FLAT-RADIO_16.5x16.0mm"] < xy["TALL-MODULE_18.0x19.2mm"]
    assert xy["TALL-MODULE_18.0x19.2mm"] - xy["FLAT-RADIO_16.5x16.0mm"] < km.NAME_TIEBREAK_MARGIN
    errors = {item["package"]: item["relative_z_error"] for item in report["candidates"]}
    assert errors["TALL-MODULE_18.0x19.2mm"] == pytest.approx(.0323, abs=1e-3)
    assert errors["FLAT-RADIO_16.5x16.0mm"] == pytest.approx(.5806, abs=1e-3)
    assert order[:2] == ["TALL-MODULE_18.0x19.2mm", "FLAT-RADIO_16.5x16.0mm"]
    assert flat.exists() and tall.exists()


def test_a_placeholder_height_decides_nothing(library):
    """@brief Caso negativo: senza un'altezza osservata l'ordine non cambia.

    Quando il modello non sa dire l'altezza, l'inventario scrive 1.0 con
    provenienza di segnaposto. Usarla vorrebbe dire scegliere sul nulla -- e
    premierebbe proprio il solido piatto.
    """
    root, add, _ = library
    add("RF_Module", "FLAT-RADIO_16.5x16.0mm",
        {"body": [16.5, 16.0, 1.3], "total": [16.5, 16.0, 1.3]})
    add("RF_Module", "TALL-MODULE_18.0x19.2mm",
        {"body": [18.0, 19.2, 3.2], "total": [18.0, 19.2, 3.2]})
    report = km.model_candidates(
        _module(height_mm=1.0, _height_source="estimated_placeholder_not_measured"), library=root)
    assert all(item["relative_z_error"] is None for item in report["candidates"])
    # Resta l'ordine della sola misura dall'alto: il piu' vicino per ingombro.
    best = min(report["candidates"], key=lambda item: item["relative_xy_error"])
    assert report["candidates"][0]["package"] == best["package"]


def test_the_height_never_promotes_a_solid_the_measurement_discarded(library):
    """@brief Fuori dalla banda comanda ancora l'ingombro."""
    root, add, _ = library
    add("RF_Module", "NEAR-BUT-UNKNOWN-Z_19.0x18.0mm", {"body": [19.0, 18.0], "total": [19.0, 18.0]})
    add("RF_Module", "FAR-BUT-RIGHT-Z_22.5x21.0mm",
        {"body": [22.5, 21.0, 3.1], "total": [22.5, 21.0, 3.1]})
    report = km.model_candidates(
        _module(height_mm=3.1, height_source="visual_estimate"), library=root)
    assert report["candidates"][0]["package"] == "NEAR-BUT-UNKNOWN-Z_19.0x18.0mm"


#: Le cinque quote misurate davvero dai solidi KiCad nella corsa dell'utente,
#: lette da kicad-model-candidates.json. Non sono inventate per il test.
_REAL_RF_MODELS = {
    'ZETA-433-SO_THT': [16.5, 16.0, 1.3],
    'ZETA-433-SO_SMD': [16.5, 16.0, 1.3],
    'ESP32-WROOM-32U': [18.0, 19.2, 3.2],
    'ESP-WROOM-02': [18.0, 20.0, 2.927],
    'ESP32-C3-WROOM-02': [18.0, 20.0, 3.235],
}


@pytest.fixture
def rf_library(tmp_path, monkeypatch):
    """@brief La famiglia RF della libreria vera, con le sue quote misurate."""
    sizes = {}
    for name, dims in _REAL_RF_MODELS.items():
        path = tmp_path/'RF_Module.3dshapes'/(name + '.step')
        path.parent.mkdir(exist_ok=True)
        path.touch()
        sizes[str(path)] = {'total': list(dims)}
    monkeypatch.setattr(km, 'model_sizes', lambda paths, library=None: {str(p): sizes[str(p)] for p in paths})
    monkeypatch.setattr(km, 'footprint_sizes', lambda *a, **k: {})
    monkeypatch.setattr(km, 'load_index',
                        lambda lib: {km.index_key(Path(k)): v for k, v in sizes.items()})
    return tmp_path


def _radio(length_mm, width_mm, **extra):
    return dict(kind='rf_module', package='shielded daughterboard/module with attached coaxial jack',
                length_mm=length_mm, width_mm=width_mm, bbox_basis='body_and_leads',
                presence_confidence='high', class_confidence='medium', **extra)


def test_the_module_of_the_real_run_with_its_real_numbers(rf_library):
    """@brief Regressione sul caso vero, nei tre stati in cui si e' trovato.

    Il modulo misura 18,0 x 19,2 mm. La pipeline gli aveva consegnato
    18,08 x 16,33 perche' il bordo destro era il confine di una tessera YOLO,
    non il bordo del corpo.

    1. Con quella misura il primo della lista e' un modulo radio 16,5 x 16,0
       alto 1,3 mm: il difetto che si vedeva nel 3D. Contratto aggiornato:
       le due varianti identiche di quel modulo "pareggiavano" fra loro e la
       promozione guardava solo loro due; ora la banda di pareggio comprende
       anche il modulo corretto, terzo a meno del margine, e con tre solidi
       di inviluppo diverso non si propone nulla. Il difetto resta
       registrato nell'ordine della lista, che e' l'unico modo di accorgersi
       se un domani tornasse.
    2. Con il bordo giusto vince il solido corretto, e non di misura: la banda
       contiene solo moduli che la fotografia non distingue fra loro, e la
       proposta scatta. SEPARAZIONE FRAGILE, dichiarata: la banda comprende
       anche ESP-WROOM-02 e ESP32-C3-WROOM-02 (18 x 20) accanto al
       ESP32-WROOM-32U (18 x 19.2). Sul lato lungo, misurato 19,23, gli
       scarti relativi sono +0,040 e -0,002: una dispersione di 0,0416, cioe'
       l'83% di NAME_TIEBREAK_MARGIN (0,05). Basta che il margine scenda
       sotto ~0,042 -- o che il rilevatore consegni il lato lungo un po' piu'
       corto -- perche' la banda diventi eterogenea e il modulo corretto
       torni review_required. La promozione qui regge quindi su un criterio
       con margine risicato, non su una distinzione netta; l'asserzione
       esplicita sotto lo rende visibile a chi cambiasse la costante.
    3. Anche con la misura sbagliata, se l'altezza e' stata osservata il solido
       corretto torna primo: la vista dall'alto non misura l'altezza, e i due
       candidati sono dentro la banda in cui non li distingue. La banda pero'
       resta eterogenea, quindi l'altezza riordina ma non promuove.
    """
    wrong = km.model_candidates(_radio(18.0848, 16.3314), library=rf_library)
    assert wrong['candidates'][0]['package'].startswith('ZETA-433')
    assert wrong['status'] == 'review_required' and wrong['selected_path'] is None
    errors = {c['package']: c['relative_xy_error'] for c in wrong['candidates']}
    assert errors['ZETA-433-SO_THT'] == pytest.approx(.0876, abs=1e-3)
    assert errors['ESP32-WROOM-32U'] == pytest.approx(.1022, abs=1e-3)
    # Il distacco e' sotto il margine entro cui il codice stesso dichiara di
    # non saper distinguere: il modulo giusto e' nella banda, e la banda non
    # e' omogenea.
    assert errors['ESP32-WROOM-32U'] - errors['ZETA-433-SO_THT'] < km.NAME_TIEBREAK_MARGIN

    right = km.model_candidates(_radio(19.23, 18.08), library=rf_library)
    assert right['status'] == 'provisional_matched'
    assert Path(right['selected_path']).stem == 'ESP32-WROOM-32U'
    assert right['candidates'][0]['relative_xy_error'] == pytest.approx(.0044, abs=1e-3)
    # La separazione fragile del punto 2, in numeri: la banda contiene i due
    # moduli 18 x 20 e il 18 x 19.2; sul lato lungo la dispersione degli
    # scarti dal lato misurato e' 0,8 / 19,23 e passa per meno di un quinto
    # del margine. Se NAME_TIEBREAK_MARGIN scende, questa asserzione cade
    # prima del comportamento, ed e' voluto.
    band = km._tie_band(right['candidates'])
    assert {Path(item['path']).stem for item in band} == {
        'ESP32-WROOM-32U', 'ESP-WROOM-02', 'ESP32-C3-WROOM-02'}
    assert km._band_is_homogeneous(band, 19.23, 18.08)
    long_spread = (20.0 - 19.2) / 19.23
    assert long_spread == pytest.approx(.0416, abs=1e-3)
    assert long_spread < km.NAME_TIEBREAK_MARGIN
    assert km.NAME_TIEBREAK_MARGIN - long_spread < 0.2 * km.NAME_TIEBREAK_MARGIN

    rescued = km.model_candidates(
        _radio(18.0848, 16.3314, height_mm=3.1, height_source='visual_estimate'),
        library=rf_library)
    assert rescued['candidates'][0]['package'] == 'ESP32-WROOM-32U'
    assert rescued['candidates'][0]['relative_z_error'] == pytest.approx(.0323, abs=1e-3)
    assert rescued['status'] == 'review_required' and rescued['selected_path'] is None


def _band_library(library, third):
    """@brief Tre ricettacoli della stessa famiglia; il terzo lo decide il test.

    I primi due sono varianti identiche di inviluppo (pareggio); il terzo
    entra nella banda di pareggio per scarto XY ma il suo inviluppo lo decide
    il chiamante: uguale (banda omogenea) o diverso (banda eterogenea).
    """
    root, add, _ = library
    twins = {add("Connector_USB", "USB_C_Receptacle_A_6P", {"body": [11.14, 6.90, 3.26]}),
             add("Connector_USB", "USB_C_Receptacle_A_16P", {"body": [11.14, 6.90, 3.46]})}
    add("Connector_USB", "USB_C_Receptacle_C", {"body": list(third)})
    component = supported(kind="connector", length_mm=10.67, width_mm=6.87,
                          package="USB Type-C receptacle, SMD with through-hole shell tabs")
    return root, {str(path) for path in twins}, component


def test_a_third_solid_within_the_margin_breaks_the_tie(library):
    """@brief Negativo: la banda di pareggio guarda tutti i candidati, non i primi due.

    Due varianti identiche pareggiano, e guardando solo loro si promuoveva il
    primo. Il terzo candidato dista dal migliore meno di NAME_TIEBREAK_MARGIN
    -- la misura non lo ha escluso -- ma ha un inviluppo XY diverso: la
    fotografia non sa dire quale dei due corpi c'e', e proporre uno dei due
    sarebbe scegliere al posto suo. E' il caso del modulo radio della corsa
    reale, dove il solido giusto era terzo e non partecipava al confronto.
    """
    root, twins, component = _band_library(library, third=(10.30, 6.90, 4.26))
    report = km.model_candidates(component, library=root)
    band = km._tie_band(report["candidates"])
    assert len(band) == 3
    assert {item["path"] for item in band} > twins
    assert not km._band_is_homogeneous(band, 10.67, 6.87)
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


def test_a_homogeneous_band_of_three_is_still_a_tie(library):
    """@brief Positivo: tre varianti con lo stesso inviluppo sono un pareggio."""
    root, twins, component = _band_library(library, third=(11.14, 6.90, 4.26))
    report = km.model_candidates(component, library=root)
    band = km._tie_band(report["candidates"])
    assert len(band) == 3 and km._band_is_homogeneous(band, 10.67, 6.87)
    assert report["status"] == "provisional_matched"
    # La proposta e' la testa della lista, che sta nella banda: quale delle
    # tre varianti identiche sia non e' una scelta della misura.
    assert report["selected_path"] == report["candidates"][0]["path"]
    assert report["selected_path"] in {item["path"] for item in band}


def test_a_band_of_one_is_dominance_only_on_a_complete_search(library, monkeypatch):
    """@brief Positivo/negativo sulla stessa lista: banda di un solo elemento.

    Con la ricerca completa il migliore ha distanziato tutti gli altri oltre
    il margine, e viene proposto. Con la stessa lista ma un modello non
    misurabile (ricerca incompleta) dominare alternative che nessuno ha
    misurato non e' dominare, e non si propone nulla.
    """
    root, best, other, component = _usb_library(library)
    report = km.model_candidates(component, library=root)
    assert len(km._tie_band(report["candidates"])) == 1
    assert report["status"] == "provisional_matched"
    assert report["selected_path"] == str(best)

    _, add, _ = library
    add("Connector_USB", "USB_C_Receptacle_C_Unreadable")
    readable = {str(best): {"body": [11.14, 6.90, 3.26]},
                str(other): {"body": [8.94, 6.90, 4.26]}}
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {
        str(p): readable[str(p)] for p in paths if str(p) in readable})
    truncated = km.model_candidates(component, library=root)
    assert len(km._tie_band(truncated["candidates"])) == 1
    assert truncated["status"] == "review_required"
    assert truncated["selected_path"] is None


def test_a_lone_candidate_is_not_a_comparison(library):
    """@brief Negativo: un candidato solo in tutta la lista non e' dominanza.

    La banda contiene un solo elemento e la ricerca e' completa, ma la misura
    non ha scartato nessuno: non c'e' stato confronto. Il contratto precedente
    resta: senza marcatura letterale o morfologia non si propone nulla.
    """
    root, add, _ = library
    add("Connector_USB", "USB_C_Receptacle_A_6P", {"body": [11.14, 6.90, 3.26]})
    component = supported(kind="connector", length_mm=10.67, width_mm=6.87,
                          package="USB Type-C receptacle, SMD with through-hole shell tabs")
    report = km.model_candidates(component, library=root)
    assert not report.get("search_truncated")
    assert len(report["candidates"]) == 1
    assert report["status"] == "review_required" and report["selected_path"] is None


def test_the_band_is_measured_from_the_best_error_not_from_the_list_head(library):
    """@brief La banda parte dal minimo scarto XY, anche se la lista e' riordinata.

    Dentro la banda l'altezza riordina i candidati: il primo della lista puo'
    non essere quello con lo scarto XY minimo. La banda deve comunque essere
    calcolata dal minimo, altrimenti un riordino allargherebbe il pareggio.
    """
    choices = [
        {"path": "b", "family": "module", "dimensions_mm": [18.0, 19.2, 3.2], "relative_xy_error": .10},
        {"path": "a", "family": "module", "dimensions_mm": [16.5, 16.0, 1.3], "relative_xy_error": .08},
        {"path": "c", "family": "module", "dimensions_mm": [22.0, 21.0, 3.0], "relative_xy_error": .135},
    ]
    band = km._tie_band(choices)
    assert [item["path"] for item in band] == ["b", "a"]
    assert km._tie_band([dict(choices[0], relative_xy_error=None)]) is None


def test_an_empty_list_has_no_band():
    """@brief Contratto di _tie_band: None quando la banda non si puo' stabilire.

    Senza candidati non c'e' un migliore da cui misurare gli altri: e' lo
    stesso esito di uno scarto illeggibile, non un'eccezione.
    """
    assert km._tie_band([]) is None


def test_band_homogeneity_is_measured_in_the_same_quantity_as_the_band():
    """@brief L'omogeneita' usa lo scarto dal lato MISURATO, per asse, come la banda.

    relative_xy_error e' il modulo dello scarto ``(modello - misurato) /
    misurato`` sull'asse peggiore; la banda ammette chi sta entro
    NAME_TIEBREAK_MARGIN dal migliore. L'omogeneita' chiede, nella stessa
    grandezza, che su ogni asse la dispersione degli scarti (con segno) dei
    membri stia entro lo stesso margine.

    Negativo strutturale: due solidi a cavallo della misura (10,3 e 9,7 su
    un lato misurato 10,0) hanno lo STESSO scarto assoluto 0,03 -- pareggio
    perfetto per la banda -- ma la fotografia li separa: distano 0,06 del
    lato misurato, oltre il margine. Positivo al limite: 10,25 e 9,75
    distano esattamente il margine.

    Normalizzazione: e' il lato misurato, non quello del modello. Con
    misurato 10,0 e modelli 9,0 e 8,52 gli scarti sono -0,100 e -0,148,
    dispersione 0,048 = la stessa differenza che ha ammesso il secondo nella
    banda; normalizzando sul lato del modello (0,48 / 9,0 = 0,053) la
    stessa coppia sarebbe stata dichiarata distinguibile pur essendo, per la
    banda, indistinguibile: due risposte diverse alla stessa domanda.
    """
    def item(long_side, short_side, measured=(10.0, 5.0)):
        return {"path": f"{long_side}x{short_side}", "family": "module",
                "dimensions_mm": [long_side, short_side, 1.0],
                "relative_xy_error": km.size_distance([long_side, short_side], *measured)}

    straddle = [item(10.3, 5.0), item(9.7, 5.0)]
    assert straddle[0]["relative_xy_error"] == pytest.approx(0.03)
    assert straddle[1]["relative_xy_error"] == pytest.approx(0.03)
    assert km._tie_band(straddle) == straddle
    assert not km._band_is_homogeneous(straddle, 10.0, 5.0)
    boundary = [item(10.25, 5.0), item(9.75, 5.0)]
    assert km._tie_band(boundary) == boundary
    assert km._band_is_homogeneous(boundary, 10.0, 5.0)

    smaller = [item(9.0, 5.0), item(8.52, 5.0)]
    assert km._tie_band(smaller) == smaller
    assert km._band_is_homogeneous(smaller, 10.0, 5.0)
    assert 0.48 / 9.0 > km.NAME_TIEBREAK_MARGIN > 0.48 / 10.0
    # Gli assi non si mescolano: lo stesso 0,48 sul lato corto, misurato 5,0,
    # e' una dispersione di 0,096 e la fotografia lo vede.
    short_axis = [item(9.0, 5.0), item(9.0, 4.52)]
    assert km._tie_band(short_axis) == short_axis
    assert not km._band_is_homogeneous(short_axis, 10.0, 5.0)
    # Il lato misurato puo' arrivare in qualunque ordine, come in size_distance.
    assert km._band_is_homogeneous(smaller, 5.0, 10.0)
    # Una misura illeggibile o una famiglia mista non e' mai omogenea.
    assert not km._band_is_homogeneous(smaller, None, 5.0)
    assert not km._band_is_homogeneous(smaller, 0.0, 5.0)
    assert not km._band_is_homogeneous([smaller[0], dict(smaller[1], family="ic")], 10.0, 5.0)
