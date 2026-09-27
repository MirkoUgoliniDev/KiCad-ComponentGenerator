"""@brief The STEP preview reads a file with FreeCAD once; view and rotation reuse it (user, 2026-09-26).

Every click on the view button used to start FreeCAD again (about 7.5 s for a
module) with the old picture on screen, so the button looked broken. The mesh
does not depend on the view nor on a manual rotation, which are applied in
Python afterwards.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.gui import model_candidates_dialog as gui


@pytest.fixture(autouse=True)
def app(qapp):
    """@brief Every test builds widgets: the session QApplication must exist first."""
    return qapp


@pytest.fixture
def freecad(monkeypatch):
    """Fake freecadcmd: runs the child script in-process and counts the runs."""
    from kicadgen.cad3d import step_generator
    vertices = [(-4, -2, 0), (4, -2, 0), (4, 2, 0), (-4, 2, 0),
                (-4, -2, 2), (4, -2, 2), (4, 2, 2), (-4, 2, 2)]
    triangles = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                 (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]

    class Shape:
        def tessellate(self, tolerance):
            return ([SimpleNamespace(x=x, y=y, z=z) for x, y, z in vertices], list(triangles))

    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: "fake-freecad")
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        read=lambda path: Shape(), makeCompound=lambda shapes: shapes[0]))
    runs = []

    def run(command, **kwargs):
        runs.append(command)
        exec(compile(Path(command[1]).read_text(encoding="utf-8"), command[1], "exec"), {})

    monkeypatch.setattr(gui.subprocess, "run", run)
    gui._MESH_CACHE.clear()
    gui._IMAGE_CACHE.clear()
    yield runs
    gui._MESH_CACHE.clear()
    gui._IMAGE_CACHE.clear()


@pytest.fixture
def step_file(tmp_path):
    path = tmp_path / "modulo.step"
    path.write_text("ISO-10303-21;", encoding="utf-8")
    return path


def test_switching_view_and_turning_reuse_the_mesh(freecad, step_file):
    plan = gui._thumbnail(str(step_file), 0.0, None, None, gui.VIEW_PLAN, colors=False)
    iso = gui._thumbnail(str(step_file), 0.0, None, None, gui.VIEW_ISOMETRIC, colors=False)
    turned = gui._thumbnail(str(step_file), 90.0, None, None, gui.VIEW_PLAN, colors=False)
    back = gui._thumbnail(str(step_file), 0.0, None, None, gui.VIEW_PLAN, colors=False)
    assert len(freecad) == 1
    # Stessa mesh, immagini diverse dove la vista o l'angolo cambiano.
    assert plan != iso and plan != turned and plan == back


def test_what_changes_the_mesh_reads_the_file_again(freecad, step_file):
    """@brief Negativo: colori diversi o file sostituito non riusano la mesh vecchia."""
    gui._thumbnail(str(step_file), 0.0, None, None, gui.VIEW_PLAN, colors=False)
    gui._thumbnail(str(step_file), 0.0, None, None, gui.VIEW_PLAN, colors=True)
    assert len(freecad) == 2
    step_file.write_text("ISO-10303-21; altro contenuto", encoding="utf-8")
    stat = step_file.stat()
    os.utime(step_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    gui._thumbnail(str(step_file), 0.0, None, None, gui.VIEW_PLAN, colors=False)
    assert len(freecad) == 3


def test_a_file_that_cannot_be_identified_is_never_cached(freecad):
    """@brief Negativo: senza dimensione e data non si sa se e' lo stesso file."""
    gui._thumbnail("non-esiste.step", 0.0, None, None, gui.VIEW_PLAN, colors=False)
    gui._thumbnail("non-esiste.step", 0.0, None, None, gui.VIEW_ISOMETRIC, colors=False)
    assert len(freecad) == 2 and not gui._MESH_CACHE


def test_the_cache_is_bounded(freecad, tmp_path):
    for index in range(gui._MESH_CACHE_SIZE + 3):
        path = tmp_path / f"m{index}.step"
        path.write_text("x", encoding="utf-8")
        gui._thumbnail(str(path), 0.0, None, None, gui.VIEW_PLAN, colors=False)
    assert len(gui._MESH_CACHE) == gui._MESH_CACHE_SIZE


def test_a_picture_already_drawn_is_not_drawn_again(freecad, step_file, monkeypatch):
    """@brief Tornare alla vista di prima non ridisegna; un colore diverso si'."""
    first = gui._thumbnail(str(step_file), 0.0, None, "#336699", gui.VIEW_PLAN, colors=False)
    drawn = []

    class CountingPainter(gui.QPainter):
        def __init__(self, *args):
            drawn.append(1)
            super().__init__(*args)

    monkeypatch.setattr(gui, "QPainter", CountingPainter)
    again = gui._thumbnail(str(step_file), 0.0, None, "#336699", gui.VIEW_PLAN, colors=False)
    assert again == first and drawn == []
    other = gui._thumbnail(str(step_file), 0.0, None, "#993311", gui.VIEW_PLAN, colors=False)
    assert other != first and drawn == [1]
