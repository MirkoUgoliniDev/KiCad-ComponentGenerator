"""@brief Nomi front/back delle annotazioni 3D allineati al cubo della vista 3D (utente, 2026-09-25).

Prima un raccordo «front-left» compariva dalla parte che il cubo chiama
«Retro». Convenzione 2: front = lato -Y del modello 3D, cioe' la faccia
«Fronte» del cubo e il lato in basso della vista dall'alto, come in KiCad. I
progetti salvati prima si convertono una volta all'apertura: il lato fisico
resta quello, cambia solo il nome.
"""
import ast
from pathlib import Path

import pytest

from kicadgen.cad3d.annotations import (ANNOTATION_CONVENTION, EdgeFilletAnnotation,
                                        FaceTextAnnotation, swap_front_back)
from tests.test_photo_inventory_gui import window, gui  # noqa: F401

SRC = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"


@pytest.fixture(scope="module")
def script():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name in {"_flip_y", "_swap_front_back",
                                                                "_edge_name"})
              or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("_FB_SWAP", "TOL")
                                                     for t in n.targets))]
    namespace = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace


def test_the_names_reach_freecad_unchanged(script):
    """Il nome scelto e' gia' quello del modello 3D: nessuno scambio nel passaggio."""
    data = {"body": {"offset_y_mm": -2.0, "width_mm": 4.0},
            "fillets": [{"edge": "front-left", "radius_mm": 1.0}],
            "face_texts": [{"face": "back", "text": "U1"}]}
    flipped = script["_flip_y"](data)
    assert flipped["fillets"][0]["edge"] == "front-left"
    assert flipped["face_texts"][0]["face"] == "back"


def test_front_is_the_face_the_3d_cube_calls_front(script):
    """Nel solido (Y verso l'alto) lo spigolo a Y minima si chiama front: e' la
    faccia vista da -Y, che il cubo chiama «Fronte»."""
    from kicadgen.gui import view_cube
    assert view_cube.FACE_VIEWS[(0, -1, 0)] == "front"

    class Point:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class Edge:
        Vertexes = [type("V", (), {"Point": Point(0, 0, 0)})(),
                    type("V", (), {"Point": Point(0, 0, 1.6)})()]
    assert script["_edge_name"](Edge(), 0, 0, 20, 10, 1.6) == "front-left"


@pytest.mark.parametrize("old, new", [("front-left", "back-left"), ("top-back", "top-front"),
                                      ("front", "back"), ("top-left", "top-left")])
def test_swap_front_back(old, new):
    assert swap_front_back(old) == new


def _state_with(window, fillets, texts, convention):  # noqa: F811
    state = dict(window._general_project_state)
    annotations = {"fillets": fillets, "texts": texts}
    if convention is not None:
        annotations["convention"] = convention
    state["annotations"] = annotations
    return state


def test_a_project_saved_before_is_converted_once_and_says_so(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    (tmp_path / "VECCHIO").mkdir()
    state = _state_with(window, [{"edge": "front-left", "radius_mm": 1.0},
                                 {"edge": "top-right", "radius_mm": 0.5}],
                        [{"face": "front", "text": "U1", "size_mm": 1.0}], None)
    window._switch_project("VECCHIO", state)
    assert [a.edge for a in window.fillet_annotations] == ["back-left", "top-right"]
    assert window.face_text_annotations[0].face == "back"
    assert "convertiti" in window.log_text.toPlainText()
    saved = window._project_state()["annotations"]
    assert saved["convention"] == ANNOTATION_CONVENTION
    assert saved["fillets"][0]["edge"] == "back-left"


def test_a_project_saved_with_the_new_convention_is_not_touched(window, tmp_path):  # noqa: F811
    """Negativo: gia' convenzione 2 -> nessuna conversione, nessuna riga di log."""
    window.field_library_root.setText(str(tmp_path))
    (tmp_path / "NUOVO").mkdir()
    state = _state_with(window, [{"edge": "front-left", "radius_mm": 1.0}], [],
                        ANNOTATION_CONVENTION)
    window._switch_project("NUOVO", state)
    assert [a.edge for a in window.fillet_annotations] == ["front-left"]
    assert "convertiti" not in window.log_text.toPlainText()
