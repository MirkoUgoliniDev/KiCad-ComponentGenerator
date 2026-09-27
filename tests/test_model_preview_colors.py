"""@brief The STEP chooser preview paints the colours stored in the model file.

User request (2026-09-26): the ESP32 module looked like a grey block. The child
reads colours with the production importer (`freecad_parts._read_library_parts`);
without colours, or on any failure, the historical single tint is kept.
Real FreeCAD check done once: ESP32-S3-WROOM-1U shows gold pads and shield,
4.2 s -> 7.0 s per preview (runs off the GUI thread).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtGui import QColor

from kicadgen.gui import model_candidates_dialog as gui


@pytest.fixture(autouse=True)
def app(qapp):
    """@brief Every test builds widgets: the session QApplication must exist first."""
    return qapp


# ── Parsing of the colour runs, no FreeCAD ───────────────────────────────

def test_runs_expand_to_one_colour_per_triangle():
    assert gui._triangle_colors([["#ff0000", 2], [None, 1]], 3) == ["#ff0000", "#ff0000", None]


@pytest.mark.parametrize("runs,count", [
    (None, 3),                                  # figlio vecchio o simulato
    ([[None, 3]], 3),                           # nessun colore nativo
    ([["#ff0000", 2]], 3),                      # conteggio che non torna
    ([["red", 3]], 3),                          # colore malformato
    ([["#ff0000", True]], 1),                   # conteggio non intero
    ([["#ff0000", 1, "x"]], 1),                 # forma sbagliata
])
def test_unusable_runs_mean_single_tint(runs, count):
    """@brief Negativo: dati incoerenti non attaccano colori ai triangoli sbagliati."""
    assert gui._triangle_colors(runs, count) is None


# ── The whole preview with a simulated FreeCAD child ─────────────────────

def _square(x0, x1, z):
    points = [SimpleNamespace(x=x, y=y, z=z) for x, y in ((x0, -3), (x1, -3), (x1, 3), (x0, 3))]
    return points, [[0, 1, 2], [0, 2, 3]]


class _Mesh:
    """A face or compound: tessellate() returns its own points and triangles."""

    def __init__(self, pieces):
        self.pieces = pieces
        self.Faces = pieces

    def tessellate(self, tolerance):
        points, triangles = [], []
        for piece in self.pieces:
            piece_points, piece_triangles = piece
            base = len(points)
            points.extend(piece_points)
            triangles.extend([base + i for i in t] for t in piece_triangles)
        return points, triangles


def _render(monkeypatch, face_colors, *, import_fails=False):
    from kicadgen.cad3d import step_generator
    faces = [_square(-8, -1, 1.0), _square(1, 8, 1.0)]
    shape = _Mesh(faces)
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: "fake-freecad")
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        read=lambda path: shape, makeCompound=lambda group: _Mesh(list(group))))

    def read_library_parts(path, color):
        if import_fails:
            raise RuntimeError("importatore non disponibile")
        return [(shape, SimpleNamespace(face_colors=face_colors) if face_colors else "#b0c7db")]

    monkeypatch.setitem(sys.modules, "freecad_parts",
                        SimpleNamespace(_read_library_parts=read_library_parts))

    def run(command, **kwargs):
        exec(compile(Path(command[1]).read_text(encoding="utf-8"), command[1], "exec"), {})

    monkeypatch.setattr(gui.subprocess, "run", run)
    return gui._thumbnail("model.step", 0.0, None, None, gui.VIEW_PLAN)


def _hue_at(image, x, y):
    return QColor(image.pixel(x, y)).hue()


def test_native_colours_reach_the_picture(monkeypatch):
    """@brief Positivo: faccia rossa a sinistra, verde a destra, entrambe visibili."""
    image = _render(monkeypatch, [(1.0, 0.0, 0.0, 0.0), (0.0, 0.8, 0.0, 0.0)])
    assert _hue_at(image, 90, 170) in range(0, 15)      # rosso
    assert _hue_at(image, 250, 170) in range(100, 140)  # verde


@pytest.mark.parametrize("face_colors,import_fails", [(None, False), ([(1, 0, 0, 0)], False),
                                                      ([(1, 0, 0, 0), (0, 1, 0, 0)], True)])
def test_without_usable_colours_the_single_tint_stays(monkeypatch, face_colors, import_fails):
    """@brief Negativo: file senza colori, colori che non corrispondono alle facce,
    importatore che fallisce -> la stessa tinta unica di prima, nessun errore."""
    image = _render(monkeypatch, face_colors, import_fails=import_fails)
    left, right = QColor(image.pixel(90, 170)), QColor(image.pixel(250, 170))
    # Stessa tinta: al piu' un'unita' o due di arrotondamento dell'antialiasing
    # lungo le diagonali interne, mai un colore diverso.
    assert all(abs(a - b) <= 2 for a, b in zip(left.getRgb()[:3], right.getRgb()[:3]))
    assert abs(left.hue() - QColor(gui.DEFAULT_BODY_COLOR).hue()) <= 10


# ── The «Colori» checkbox of the dialog (utente, 2026-09-26) ─────────────

def _dialog():
    return gui.ModelCandidatesDialog({"name": "U1", "length_mm": 3, "width_mm": 3,
                                      "height_mm": 1}, {"candidates": []})


def test_the_colours_checkbox_is_on_by_default_and_remembered():
    from kicadgen.gui.app_config import load_config
    dialog = _dialog()
    assert dialog.colors_checkbox.isChecked(), "predefinito: colori del modello"
    dialog.colors_checkbox.setChecked(False)
    assert load_config()[gui.PREVIEW_COLORS_KEY] is False
    assert not _dialog().colors_checkbox.isChecked(), "la finestra successiva la ricorda"


def test_the_preview_worker_gets_the_choice(monkeypatch):
    """@brief Positivo: spenta arriva colors=False; accesa la chiamata resta quella di prima."""
    calls = []
    monkeypatch.setattr(gui, "_thumbnail", lambda *args, **kwargs: calls.append(kwargs))
    gui._Preview(1, "model.step", 0.0, colors=False).run()
    gui._Preview(2, "model.step", 0.0).run()
    assert calls == [{"colors": False}, {}]


def test_without_colours_the_file_colours_are_not_even_read(monkeypatch):
    """@brief Negativo: con la spunta spenta l'importatore dei colori (la parte lenta) non parte."""
    from kicadgen.cad3d import step_generator
    shape = _Mesh([_square(-8, -1, 1.0), _square(1, 8, 1.0)])
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: "fake-freecad")
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        read=lambda path: shape, makeCompound=lambda group: _Mesh(list(group))))

    def forbidden(*args, **kwargs):
        raise AssertionError("colori letti con la spunta spenta")

    monkeypatch.setitem(sys.modules, "freecad_parts",
                        SimpleNamespace(_read_library_parts=forbidden))

    def run(command, **kwargs):
        exec(compile(Path(command[1]).read_text(encoding="utf-8"), command[1], "exec"), {})

    monkeypatch.setattr(gui.subprocess, "run", run)
    image = gui._thumbnail("model.step", 0.0, None, None, gui.VIEW_PLAN, colors=False)
    assert abs(QColor(image.pixel(90, 170)).hue() - QColor(gui.DEFAULT_BODY_COLOR).hue()) <= 10
