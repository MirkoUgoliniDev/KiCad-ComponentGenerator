"""@brief Cubo di navigazione e barra di icone del tab 3D.

Richiesta dell'utente (2026-09-23, con screenshot di uno slicer): togliere i
testi che distraggono, un cubo cliccabile per ruotare la vista al posto dei
pulsanti «Vista», e una barra di icone con numeri pin e pinout, con tooltip
tradotti. Scelte: righe di stato solo per errori e avvisi; cubo cliccabile
come lo slicer; nella barra gli strumenti di vista.
"""
import pytest

from kicadgen.gui import view_cube as vc


# ── Geometria del clic, senza VTK ───────────────────────────────────


@pytest.mark.parametrize("point, expected", [
    ((0.0, 0.0, 0.5), (0, 0, 1)),        # centro della faccia SOPRA
    ((0.1, -0.2, -0.5), (0, 0, -1)),     # SOTTO
    ((0.0, -0.5, 0.1), (0, -1, 0)),      # FRONTE
    ((0.5, 0.0, 0.45), (1, 0, 1)),       # vicino allo spigolo destra/sopra
    ((0.45, -0.45, 0.5), (1, -1, 1)),    # vicino all'angolo
])
def test_a_point_on_the_cube_selects_face_edge_or_corner(point, expected):
    assert vc.classify_cube_point(point) == expected


def test_the_inner_part_of_a_face_is_the_face_not_the_edge():
    """Negativo della fascia di bordo: poco dentro la soglia resta la faccia."""
    inside = vc.HALF * vc.EDGE_FRACTION * 0.95
    assert vc.classify_cube_point((inside, 0.0, 0.5)) == (0, 0, 1)
    assert vc.classify_cube_point((vc.HALF * vc.EDGE_FRACTION, 0.0, 0.5)) == (1, 0, 1)


@pytest.mark.parametrize("point", [(0.0, 0.0, 0.0), (0.1, 0.2, 0.3), ("a", 0, 0),
                                   (float("nan"), 0, 0.5), (0.5, 0.5)])
def test_points_not_on_the_surface_select_nothing(point):
    assert vc.classify_cube_point(point) is None


def test_ray_enters_the_box_on_the_near_face_or_misses_it():
    assert vc.ray_box_entry((0, 0, 5), (0, 0, -5)) == pytest.approx((0, 0, 0.5))
    assert vc.ray_box_entry((0.2, -5, 0.1), (0.2, 5, 0.1)) == pytest.approx((0.2, -0.5, 0.1))
    assert vc.ray_box_entry((0.8, 0, 5), (0.8, 0, -5)) is None


def test_only_faces_have_a_standard_view():
    assert vc.view_for_direction((0, 0, 1)) == "top"
    assert vc.view_for_direction((0, -1, 0)) == "front"
    assert vc.view_for_direction((1, 0, 1)) is None
    assert vc.view_for_direction(None) is None


# ── Il cubo vero nell'anteprima VTK ─────────────────────────────────

pytest.importorskip("pyvistaqt")


@pytest.fixture
def viewer():
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui.preview_3d_vtk import Preview3DVtk
    app = QApplication.instance() or QApplication([])
    view = Preview3DVtk()
    view.resize(500, 360)
    view.show()
    app.processEvents()
    if view.view_cube is None:
        pytest.skip("modulo delle annotazioni VTK non disponibile")
    yield view, app
    view.close()
    app.processEvents()


def _click(app, view, x, y):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    button = Qt.MouseButton.LeftButton
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        point = QPointF(x, y)
        app.sendEvent(view, QMouseEvent(kind, point, QPointF(view.mapToGlobal(point.toPoint())),
                                        button, button, Qt.KeyboardModifier.NoModifier))


def _cube_centre(view):
    x, y, w, h = view.view_cube.cube_rect()
    return x + w / 2, y + h / 2


def test_the_cube_sits_in_the_top_right_corner_and_follows_resizes(viewer):
    view, app = viewer
    x, y, w, h = view.view_cube.cube_rect()
    assert x + w == pytest.approx(view.width() - vc.MARGIN_PX)
    assert y == vc.MARGIN_PX
    view.resize(800, 400)
    app.processEvents()
    assert view.view_cube.cube_rect()[0] + w == pytest.approx(800 - vc.MARGIN_PX)


def test_clicking_the_face_in_front_of_the_camera_selects_that_view(viewer, monkeypatch):
    view, app = viewer
    view.show_view("top")
    chosen = []
    monkeypatch.setattr(view, "show_view", chosen.append)
    _click(app, view, *_cube_centre(view))
    assert chosen == ["top"]


def test_the_house_restores_the_initial_view(viewer, monkeypatch):
    view, app = viewer
    chosen = []
    monkeypatch.setattr(view, "show_view", chosen.append)
    x, y, w, h = view.view_cube.home_rect()
    _click(app, view, x + w / 2, y + h / 2)
    assert chosen == ["iso"]


def test_a_click_on_the_cube_never_reaches_the_vtk_rotation(viewer, monkeypatch):
    """Il clic sul cubo non deve anche far partire la rotazione della camera."""
    from pyvistaqt import QtInteractor
    view, app = viewer
    view.show_view("top")
    reached = []
    monkeypatch.setattr(QtInteractor, "mousePressEvent",
                        lambda self, event: reached.append("press"))
    monkeypatch.setattr(QtInteractor, "mouseReleaseEvent",
                        lambda self, event: reached.append("release"))
    monkeypatch.setattr(view, "show_view", lambda name: None)
    _click(app, view, *_cube_centre(view))
    assert reached == []
    # Negativo: lontano dal cubo il mouse resta quello di VTK.
    _click(app, view, 40, view.height() - 40)
    assert reached == ["press", "release"]


def test_the_empty_corner_of_the_cube_square_is_not_a_click_on_the_cube(viewer, monkeypatch):
    """Nella vista dall'alto la faccia non riempie il quadrato: l'angolo e' vuoto."""
    view, app = viewer
    view.show_view("top")
    x, y, _, _ = view.view_cube.cube_rect()
    assert view.view_cube.hit(x + 2, y + 2) is None


def test_an_edge_click_looks_along_the_diagonal(viewer):
    import numpy as np
    view, _ = viewer
    view.view_direction((1, 0, 1))
    position = np.asarray(view.camera.position) - np.asarray(view.camera.focal_point)
    direction = position / np.linalg.norm(position)
    assert direction == pytest.approx(np.array([1, 0, 1]) / np.sqrt(2), abs=1e-6)


def test_the_cube_survives_clearing_the_preview(viewer):
    view, _ = viewer
    view.clear_preview()
    renderers = view.ren_win.GetRenderers()
    assert any(renderers.GetItemAsObject(i) is view.view_cube.renderer
               for i in range(renderers.GetNumberOfItems()))


def test_face_labels_are_translatable(viewer):
    view, _ = viewer
    view.set_view_cube_labels({"top": "Top", "bottom": "Bottom", "front": "Front",
                               "back": "Back", "right": "Right", "left": "Left"})
    assert view.view_cube.cube.GetZPlusFaceText() == "TOP"
    assert view.view_cube.cube.GetYMinusFaceText() == "FRONT"
