"""@brief Le marcature lette dalla foto non finiscono sul solido senza consenso.

Il percorso standard ricostruisce ingombri meccanici generici, senza scritte
(strategia di progetto). Il campo `marking` dei componenti resta nel payload
-- e' un'evidenza per l'abbinamento ai modelli di libreria e un'informazione
per l'utente -- ma lo script FreeCAD lo stampa solo se `Body3DParams`
dichiara `show_markings_3d=True`. Qui si verifica il contratto dai due lati:
il costruttore (senza FreeCAD, estraendo la sola funzione di decisione) e
l'orchestratore (il flag arriva nel JSON passato a freecadcmd).
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import step_generator as cad, kicad_models
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide, ComponentBox
from kicadgen.generators.pin_header import PinHeaderParams
from tests.test_review_1027_sweep import fake_freecad_writing_the_step

SRC = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"

#: Marcature come le legge davvero l'inventario fotografico: sono DATI del
#: test, non regole -- la decisione non guarda il contenuto della stringa.
OBSERVED_MARKINGS = (
    "ESPRESSIF ESP32-S3-WROOM-1U MCN16R8 FCC ID:2AC7Z-ESP32S3WROOM1U",
    "CH343P HDM", "T120 MN6S", "330?", "123", "01C", "0",
)


@pytest.fixture(scope="module")
def decide():
    """Estrae la sola funzione di decisione: il modulo intero pretende FreeCAD."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_marking_to_print"]
    assert len(body) == 1
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<marking>", "exec"), namespace)
    return namespace["_marking_to_print"]


@pytest.mark.parametrize("marking", OBSERVED_MARKINGS)
def test_standard_payload_prints_no_marking(decide, marking):
    """Negativo: senza consenso esplicito la scritta non diventa geometria,
    qualunque cosa sia stata letta e anche se il payload non ha il campo
    (JSON scritti prima che esistesse) o lo ha spento."""
    component = {"marking": marking}
    assert decide(component, {}) == ""
    assert decide(component, {"show_markings_3d": False}) == ""
    # Un valore "vero" ma non booleano non e' un consenso dichiarato.
    assert decide(component, {"show_markings_3d": "yes"}) == ""


@pytest.mark.parametrize("marking", OBSERVED_MARKINGS)
def test_markings_are_printed_only_on_explicit_request(decide, marking):
    """Positivo: l'opzione resta disponibile e, se dichiarata, stampa il
    testo ripulito; una marcatura vuota non produce comunque nulla."""
    assert decide({"marking": f"  {marking} "}, {"show_markings_3d": True}) == marking
    assert decide({"marking": ""}, {"show_markings_3d": True}) == ""
    assert decide({}, {"show_markings_3d": True}) == ""


def _capture_payload(monkeypatch):
    """Sostituisce freecadcmd con un finto che legge il JSON e "scrive" lo STEP.

    CONTRACT CHANGED 2026-10-27 (review sweep, S4): every `_run_freecad` call
    now goes through `process_log.run_logged`, also without progress and
    cancel, so the double moved from `subprocess.run` to the
    `subprocess.Popen` that run_logged starts the child with
    (`tests.test_review_1027_sweep.fake_freecad_writing_the_step`). The
    recorded payload is the same.
    """
    seen = []
    monkeypatch.setattr(cad, "find_freecadcmd", lambda: "freecadcmd")
    fake_freecad_writing_the_step(monkeypatch, seen)
    monkeypatch.setattr(kicad_models, "attach_models", lambda components, **kw: None)
    return seen


@pytest.mark.parametrize("layout", ["castellated", "matrix"])
def test_orchestrator_defaults_to_no_markings_but_keeps_the_evidence(monkeypatch, tmp_path, layout):
    """Negativo lato orchestratore: il default di Body3DParams spegne la
    stampa in ENTRAMBI i generatori, mentre `marking` resta nel payload
    (chi abbina i modelli di libreria e chi rilegge il JSON ne ha bisogno)."""
    seen = _capture_payload(monkeypatch)
    c = ComponentBox(0, 0, 4, 2, 1, name="Q1", marking="330?")
    body = Body3DParams(40, 20, 5, populated_board=True, components=[c])
    if layout == "castellated":
        params = CastellatedModuleParams(name="Marks", body_length=40, body_width=20,
            sides=[CastellatedSide(edge="left", count=2, pitch=2.54)], components=[c])
        cad.generate_castellated_3d_model(params, body, tmp_path)
    else:
        cad.generate_3d_model(PinHeaderParams(name="Marks"), body, tmp_path)
    assert len(seen) == 1
    payload = seen[0]
    assert payload["show_markings_3d"] is False
    assert [comp["marking"] for comp in payload["components"]] == ["330?"]


def test_orchestrator_forwards_an_explicit_request(monkeypatch, tmp_path):
    """Positivo lato orchestratore: la scelta dichiarata arriva allo script."""
    seen = _capture_payload(monkeypatch)
    c = ComponentBox(0, 0, 4, 2, 1, name="Q1", marking="330?")
    body = Body3DParams(40, 20, 5, populated_board=True, components=[c], show_markings_3d=True)
    cad.generate_3d_model(PinHeaderParams(name="Marks"), body, tmp_path)
    assert seen[0]["show_markings_3d"] is True


def test_module_contact_labels_are_a_separate_channel(monkeypatch, tmp_path):
    """Le sigle accanto ai contatti (serigrafia del footprint) viaggiano in
    `silk_texts`, governate da `show_silk_labels_3d`: non sono marcature dei
    componenti e la nuova opzione non le tocca, in nessuno dei due versi."""
    seen = _capture_payload(monkeypatch)
    params = CastellatedModuleParams(name="Silk", body_length=40, body_width=20,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54)],
        silk_labels={"1": "G", "2": "5V"}, show_silk_labels_3d=True)
    body = Body3DParams(40, 20, 5)
    cad.generate_castellated_3d_model(params, body, tmp_path)
    payload = seen[0]
    assert sorted(t["text"] for t in payload["silk_texts"]) == ["5V", "G"]
    assert payload["show_markings_3d"] is False
