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


# ── Ordine dei raccordi (audit 2026-09-25) ──────────────────────────────────
# OpenCascade accetta o rifiuta secondo l'ordine: r=0.5 sul bordo superiore
# prima di r=3 sugli angoli faceva fallire gli angoli, l'ordine inverso no
# (provato con FreeCAD 1.1). La correzione prova gli ordini e tiene il migliore.

@pytest.fixture(scope="module")
def fillets():
    """Estrae le funzioni dei raccordi con un _warn che registra."""
    names = {"_edge_name", "_fillet_conflicts", "_apply_fillets", "_fillet_groups",
             "_fillet_orders", "_try_fillet_groups"}
    # Contratto aggiornato (fix E5): la prova degli ordini batte per il
    # watchdog del silenzio, quindi servono anche la classe del battito, il
    # suo intervallo e i moduli standard che usa.
    constants = {"_FACE_SIDES", "TOL", "_MAX_PERMUTED_FILLET_GROUPS", "_HEARTBEAT_INTERVAL_S"}
    classes = {"_StepHeartbeat"}
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = [node for node in tree.body
              if (isinstance(node, ast.FunctionDef) and node.name in names)
              or (isinstance(node, ast.ClassDef) and node.name in classes)
              or (isinstance(node, ast.Assign)
                  and any(getattr(t, "id", "") in constants for t in node.targets))]
    warnings = []
    import sys
    import threading
    import time
    namespace = {"_warn": lambda template, **values: warnings.append(values),
                 "sys": sys, "threading": threading, "time": time}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SRC), "exec"), namespace)
    namespace["warnings"] = warnings
    return namespace


class _Point:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Edge:
    def __init__(self, a, b):
        self.Vertexes = [type("V", (), {"Point": _Point(*a)})(),
                         type("V", (), {"Point": _Point(*b)})()]


class _Box:
    """Box 20x10x1.6 che, come OpenCascade nel caso visto, rifiuta r=3 sugli
    angoli verticali se prima sono stati raccordati i bordi superiori."""

    def __init__(self, applied=()):
        self.applied = list(applied)
        self.Edges = [_Edge((0, 0, 0), (0, 0, 1.6)), _Edge((20, 0, 0), (20, 0, 1.6)),
                      _Edge((0, 10, 0), (0, 10, 1.6)), _Edge((20, 10, 0), (20, 10, 1.6)),
                      _Edge((0, 0, 1.6), (20, 0, 1.6)), _Edge((0, 10, 1.6), (20, 10, 1.6)),
                      _Edge((0, 0, 1.6), (0, 10, 1.6)), _Edge((20, 0, 1.6), (20, 10, 1.6))]

    def makeFillet(self, radius, edges):
        if radius == 3.0 and 0.5 in self.applied:
            raise RuntimeError("BRep_API: command not done")
        return _Box(self.applied + [radius])


TOP = [("top-front", 0.5), ("top-back", 0.5), ("top-left", 0.5), ("top-right", 0.5)]
CORNERS = [("front-left", 3.0), ("front-right", 3.0), ("back-left", 3.0), ("back-right", 3.0)]


@pytest.mark.parametrize("order", ["bordi prima", "angoli prima"])
def test_the_result_does_not_depend_on_the_order_of_the_answer(fillets, order):
    fillets["warnings"].clear()
    items = TOP + CORNERS if order == "bordi prima" else CORNERS + TOP
    shape = fillets["_apply_fillets"](_Box(), _f(*items), (0, 0, 20, 10, 1.6))
    assert sorted(shape.applied) == [0.5, 3.0]
    assert fillets["warnings"] == []


def test_a_fillet_that_fails_in_every_order_is_still_reported(fillets):
    """Negativo: se nessun ordine regge, l'avviso arriva e il resto e' applicato."""

    class Stubborn(_Box):
        def makeFillet(self, radius, edges):
            if radius == 3.0:
                raise RuntimeError("BRep_API: command not done")
            return Stubborn(self.applied + [radius])
    fillets["warnings"].clear()
    shape = fillets["_apply_fillets"](Stubborn(), _f(*(TOP + CORNERS)), (0, 0, 20, 10, 1.6))
    assert shape.applied == [0.5]
    assert len(fillets["warnings"]) == 1 and fillets["warnings"][0]["radius"] == 3.0


def test_the_canonical_order_puts_larger_radii_and_corners_first(fillets):
    groups = fillets["_fillet_groups"](_f(("top-front", 0.5), ("front-left", 0.5),
                                          ("back-right", 3.0)))
    assert [radius for radius, _ in groups] == [3.0, 0.5]
