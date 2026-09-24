"""@brief Raccordi impossibili scartati con il motivo, prima di OpenCascade.

Caso visto il 2026-09-24 (TEST4): r=1.0 su 'top-left' e 'bottom-left' di una
scheda spessa 1.6 mm sommano 2.0 mm sulla faccia sinistra, che sparirebbe;
FreeCAD rispondeva solo «BRep_API: command not done» e il gruppo intero
(anche gli spigoli buoni) cadeva.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"


@pytest.fixture(scope="module")
def conflicts():
    """Estrae la sola funzione pura: il modulo intero pretende FreeCAD."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = [node for node in tree.body
              if (isinstance(node, ast.FunctionDef) and node.name == "_fillet_conflicts")
              or (isinstance(node, ast.Assign)
                  and any(getattr(t, "id", "") == "_FACE_SIDES" for t in node.targets))]
    namespace = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace["_fillet_conflicts"]


BOARD = {"x": 63.3, "y": 25.4, "z": 1.6}


def _f(*pairs):
    return [{"edge": edge, "radius_mm": radius} for edge, radius in pairs]


def test_opposite_fillets_thicker_than_the_board_are_rejected(conflicts):
    """Negativo (TEST4): 1.0 + 1.0 sulla faccia sinistra alta 1.6 mm."""
    found = conflicts(_f(("top-left", 1.0), ("top-right", 1.0),
                         ("bottom-left", 1.0), ("bottom-right", 1.0)), BOARD)
    assert set(found) == {"top-left", "top-right", "bottom-left", "bottom-right"}
    face, axis, total, extent = found["top-left"]
    assert face == "left" and axis == "z" and total == pytest.approx(2.0) and extent == 1.6


def test_the_same_radius_on_the_vertical_corners_is_buildable(conflicts):
    """Positivo: stessi quattro raggi sugli spigoli verticali (angoli visti dall'alto)."""
    assert conflicts(_f(("front-left", 1.0), ("front-right", 1.0),
                        ("back-left", 1.0), ("back-right", 1.0)), BOARD) == {}


def test_top_only_fillets_fit_when_they_leave_face_left(conflicts):
    """Positivo: solo il lato alto, 1.0 < 1.6: la faccia sinistra resta."""
    assert conflicts(_f(("top-left", 1.0), ("top-right", 1.0)), BOARD) == {}


def test_a_single_fillet_as_large_as_the_face_is_rejected(conflicts):
    """Negativo: un raggio pari all'altezza consuma tutta la faccia laterale."""
    assert set(conflicts(_f(("top-front", 1.6)), BOARD)) == {"top-front"}


def test_only_the_edges_of_the_violated_face_are_dropped(conflicts):
    """Gli spigoli non coinvolti restano, anche con lo stesso raggio."""
    found = conflicts(_f(("top-left", 1.0), ("bottom-left", 1.0), ("front-right", 1.0)), BOARD)
    assert set(found) == {"top-left", "bottom-left"}


def test_unknown_edge_names_are_left_to_the_existing_warning(conflicts):
    assert conflicts(_f(("diagonal", 5.0)), BOARD) == {}
