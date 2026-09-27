"""@brief The STEP chooser shows the model as it will sit on the board.

User report (2026-09-26): SOT-223 turned to 270 degrees in the chooser, turned
differently in the generated 3D. The photo crop is in the board frame; the
preview showed only the local angle, while the CAD adds the rectangle's
rotation (with the Y flip, `freecad_build_body._flip_y`). Verified once with
real FreeCAD: preview and top view of the generated solid now coincide.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")

from kicadgen.gui import model_candidates_dialog as mcd


@pytest.fixture(autouse=True)
def app(qapp):
    """@brief Every test builds widgets: the session QApplication must exist first."""
    return qapp


def _dialog(**component):
    base = {"name": "U3", "length_mm": 8.5, "width_mm": 6.3, "height_mm": 1.6}
    base.update(component)
    return mcd.ModelCandidatesDialog(base, {"candidates": []})


def test_a_rotated_rectangle_turns_the_preview_like_the_cad():
    dialog = _dialog(rotation_deg=90.0, landmark_side="top", landmark_feature="tab")
    # Verso del CAD: Y ribaltata, quindi angolo col segno opposto.
    assert dialog._board_rotation == -90.0
    # La correzione automatica di verso riceve la stessa rotazione del CAD.
    assert dialog._placement["rotation_deg"] == -90.0


def test_an_unrotated_rectangle_changes_nothing():
    """@brief Negativo: rettangolo a 0 gradi, anteprima e posizionamento come prima."""
    dialog = _dialog(rotation_deg=0.0)
    assert dialog._board_rotation == 0.0
    assert "rotation_deg" not in dialog._placement


def test_the_saved_choice_stays_the_local_angle():
    """@brief La rotazione mostrata cambia, il valore salvato no: FreeCAD vuole l'angolo locale."""
    dialog = _dialog(rotation_deg=90.0,
                     model_selection={"status": "confirmed", "path": "", "rotation_deg": 270.0})
    assert dialog.rotation.value() == 270.0


def test_the_preview_worker_draws_with_the_board_rotation(monkeypatch):
    """@brief Positivo: il disegno riceve la rotazione della scheda; negativo: a 0 non riceve nulla."""
    calls = []
    monkeypatch.setattr(mcd, "_thumbnail", lambda *args, **kwargs: calls.append(kwargs))
    mcd._Preview(1, "model.step", 270.0, board_rotation=-90.0).run()
    mcd._Preview(2, "model.step", 270.0).run()
    assert calls == [{"board_rotation": -90.0}, {}]
