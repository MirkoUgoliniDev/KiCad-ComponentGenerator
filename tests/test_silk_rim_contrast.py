"""@brief La cornice serigrafica va dove il corpo sparirebbe nel fondo."""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"


@pytest.fixture(scope="module")
def rim():
    """Estrae le sole funzioni del contrasto: il modulo intero pretende FreeCAD."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = {"_relative_luminance", "_needs_silk_rim"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in wanted)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "SILK_RIM_MIN_CONTRAST" for t in n.targets))]
    assert len(body) == 3, [getattr(n, "name", n) for n in body]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<rim>", "exec"), namespace)
    return namespace


#: Colore del substrato usato dal generatore per i moduli su PCB.
PCB = "#2f3f1f"


@pytest.mark.parametrize("color", [
    "#1a1a1a",   # case nero: e' il caso per cui la cornice esiste
    "#303030",   # grigio scurissimo, il default dei corpi dalla foto
    "#2f3f1f",   # identico al substrato
    "#243018",   # verde scuro appena diverso
])
def test_a_body_that_would_vanish_into_the_board_gets_its_frame(rim, color):
    assert rim["_needs_silk_rim"](color, PCB) is True


@pytest.mark.parametrize("color", [
    "#ffffff",   # il modulo schermato e il guscio USB
    "#c8c8c8",   # metallo
    "#e8e8e0",   # ceramica chiara
    "#FF00FF",   # il magenta dei componenti senza modello
])
def test_a_body_that_stands_out_on_its_own_does_not(rim, color):
    assert rim["_needs_silk_rim"](color, PCB) is False


def test_the_threshold_is_the_published_one_not_one_chosen_here(rim):
    assert rim["SILK_RIM_MIN_CONTRAST"] == 3.0


def test_the_relation_is_symmetric_and_survives_a_broken_colour(rim):
    assert rim["_needs_silk_rim"]("#ffffff", PCB) == rim["_needs_silk_rim"](PCB, "#ffffff")
    # Un colore illeggibile non deve far cadere la generazione: vale nero, e
    # nero su PCB scuro la cornice se la merita.
    assert rim["_needs_silk_rim"]("non-un-colore", PCB) is True
