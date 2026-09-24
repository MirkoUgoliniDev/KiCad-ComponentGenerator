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


# Contratto cambiato di proposito (nota 5 dell'utente, 2026-09-23): il clic
# sul cubo non salta piu' alla vista con show_view, chiede un cambio di vista
# animato verso la stessa vista (animate_to_view). Che l'animazione finisca
# esattamente su show_view(nome) lo verificano i test dell'animazione piu' sotto.

def test_clicking_the_face_in_front_of_the_camera_selects_that_view(viewer, monkeypatch):
    view, app = viewer
    view.show_view("top")
    chosen = []
    monkeypatch.setattr(view, "animate_to_view", chosen.append)
    _click(app, view, *_cube_centre(view))
    assert chosen == ["top"]


def test_the_house_restores_the_initial_view(viewer, monkeypatch):
    view, app = viewer
    chosen = []
    monkeypatch.setattr(view, "animate_to_view", chosen.append)
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


# ── Scritte delle facce: misurate, non stimate (nota 7) ─────────────

#: Etichette reali delle quattro lingue dell'interfaccia (catalogo i18n).
LANGUAGE_LABELS = {
    "it": dict(top="Sopra", bottom="Sotto", front="Fronte", back="Retro",
               right="Destra", left="Sinistra"),
    "en": dict(top="Top", bottom="Bottom", front="Front", back="Back",
               right="Right", left="Left"),
    "fr": dict(top="Dessus", bottom="Dessous", front="Avant", back="Arrière",
               right="Droite", left="Gauche"),
    "de": dict(top="Oben", bottom="Unten", front="Vorne", back="Hinten",
               right="Rechts", left="Links"),
}


def _text_extents_on_faces(cube_actor):
    """(larghezza, altezza) sul piano della faccia di ogni scritta vera del cubo."""
    from vtkmodules.vtkRenderingCore import vtkPropCollection
    parts = vtkPropCollection()
    cube_actor.GetActors(parts)
    extents = []
    for index in range(parts.GetNumberOfItems()):
        actor = parts.GetItemAsObject(index)
        source = actor.GetMapper().GetInputAlgorithm()
        if source is None or source.GetClassName() != "vtkVectorText":
            continue
        x0, x1, y0, y1, z0, z1 = actor.GetBounds()
        spans = sorted((x1 - x0, y1 - y0, z1 - z0))
        extents.append((spans[2], spans[1]))  # la terza e' lo spessore, nulla
    return extents


@pytest.mark.parametrize("language", sorted(LANGUAGE_LABELS))
def test_every_face_label_fits_inside_its_face(viewer, language):
    """Prima "FRONT" toccava il bordo e "RIGHT" usciva dalla faccia."""
    view, _ = viewer
    view.set_view_cube_labels(LANGUAGE_LABELS[language])
    extents = _text_extents_on_faces(view.view_cube.cube)
    assert len(extents) == 6
    for width, height in extents:
        # Faccia di lato 1: la scritta resta entro la frazione dichiarata.
        assert width <= vc.TEXT_FACE_FRACTION + 1e-6
        assert height <= vc.TEXT_FACE_FRACTION + 1e-6
    # Le etichette vere non si accorciano mai: si adatta solo la scala.
    assert not any(text.endswith(".") for text in view.view_cube.labels.values())
    # E restano leggibili: maiuscole di almeno MIN_TEXT_PX sul cubo reale.
    scale = view.view_cube.cube.GetFaceTextScale()
    assert scale * 1.0 * vc.CUBE_PX >= vc.MIN_TEXT_PX


def test_the_old_character_count_estimate_overflowed_in_english():
    """Negativo della misura: la stima 0.85/(0.8*caratteri) faceva uscire RIGHT/FRONT."""
    old_scale = min(0.28, 0.85 / (0.8 * len("BOTTOM")))
    assert vc.vector_text_extent("FRONT")[0] * old_scale > vc.TEXT_FACE_FRACTION
    _, scale = vc.fit_face_labels({key: text.upper()
                                   for key, text in LANGUAGE_LABELS["en"].items()})
    assert vc.vector_text_extent("FRONT")[0] * scale <= vc.TEXT_FACE_FRACTION + 1e-9


def test_the_scale_follows_the_glyph_widths_not_the_character_count():
    """Stesso numero di lettere, larghezze diverse: conta la geometria vera."""
    base = {key: "A" for key in vc.DEFAULT_LABELS}
    kept_narrow, narrow = vc.fit_face_labels({**base, "left": "IIIIII"})
    kept_wide, wide = vc.fit_face_labels({**base, "left": "WWWWWW"})
    assert kept_narrow["left"] == "IIIIII" and kept_wide["left"] == "WWWWWW"
    assert wide < narrow
    assert vc.vector_text_extent("WWWWWW")[0] * wide == pytest.approx(vc.TEXT_FACE_FRACTION)


def test_accented_letters_are_folded_because_vector_text_has_no_glyph_for_them():
    # vtkVectorText salta la È: senza piegatura si disegnerebbe "ARRIRE".
    assert vc.vector_text_extent("ARRIÈRE")[0] < vc.vector_text_extent("ARRIERE")[0]
    assert vc.vector_text_safe("ARRIÈRE") == "ARRIERE"
    assert vc.vector_text_safe("DESSOUS") == "DESSOUS"   # negativo: ASCII intatto


def test_a_label_without_latin_letters_falls_back_to_the_default():
    assert vc.vector_text_safe("ΠΑΝΩ", "SOPRA") == "SOPRA"
    assert vc.vector_text_safe("OBEN", "SOPRA") == "OBEN"


def test_the_drawn_french_label_has_all_its_letters(viewer):
    view, _ = viewer
    view.set_view_cube_labels(LANGUAGE_LABELS["fr"])
    assert view.view_cube.cube.GetYPlusFaceText() == "ARRIERE"


def test_a_pathological_label_is_shortened_instead_of_shrinking_every_face(viewer):
    view, _ = viewer
    labels = dict(LANGUAGE_LABELS["en"], left="L" + "E" * 60 + "FT")
    view.set_view_cube_labels(labels)
    drawn = view.view_cube.labels
    assert drawn["left"].endswith(".") and len(drawn["left"]) < 20
    assert drawn["front"] == "FRONT"            # le altre restano intere
    for width, height in _text_extents_on_faces(view.view_cube.cube):
        assert width <= vc.TEXT_FACE_FRACTION + 1e-6
    scale = view.view_cube.cube.GetFaceTextScale()
    tallest = max(vc.vector_text_extent(text)[1] for text in drawn.values())
    assert scale * tallest * vc.CUBE_PX >= vc.MIN_TEXT_PX - 1e-6


def test_the_house_is_proportional_and_does_not_overlap_the_cube(viewer):
    view, _ = viewer
    cx, cy, cw, ch = view.view_cube.cube_rect()
    hx, hy, hw, hh = view.view_cube.home_rect()
    assert cw == vc.CUBE_PX and vc.CUBE_PX >= 120
    assert hw == hh == round(vc.CUBE_PX * 26 / 96)
    assert hy >= cy + ch                                   # sotto il cubo
    assert hx + hw / 2 == pytest.approx(cx + cw / 2)       # centrata
    assert view.view_cube.hit(hx + hw / 2, hy + hh / 2) == "home"
    assert view.view_cube.hit(hx + hw / 2, hy - 1) != "home"


# ── Animazione della camera (nota 5), matematica senza VTK ──────────

import numpy as np  # noqa: E402


def _pose(direction, up, distance=10.0, focal=(0.0, 0.0, 0.0), scale=1.0, angle=30.0):
    direction = np.asarray(direction, dtype=float) / np.linalg.norm(direction)
    return vc.CameraPose(tuple(np.asarray(focal) + direction * distance), tuple(focal),
                         tuple(up), scale, angle)


def _direction(pose):
    offset = np.subtract(pose.position, pose.focal)
    return offset / np.linalg.norm(offset)


def _angle(a, b):
    return float(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0)))


def test_easing_is_slow_at_both_ends_and_symmetric():
    assert vc.ease_in_out(0) == 0 and vc.ease_in_out(1) == 1
    assert vc.ease_in_out(0.5) == pytest.approx(0.5)
    assert vc.ease_in_out(0.05) < 0.05 and vc.ease_in_out(0.95) > 0.95
    samples = [vc.ease_in_out(t) for t in np.linspace(0, 1, 50)]
    assert samples == sorted(samples)
    assert vc.ease_in_out(-3) == 0 and vc.ease_in_out(7) == 1


def test_intermediate_directions_lie_on_the_arc_between_start_and_end():
    start = _pose((0, -1, 0), (0, 0, 1), distance=10, focal=(0, 0, 0), angle=30)
    end = _pose((0, 0, 1), (0, 1, 0), distance=40, focal=(2, 0, 0), angle=35)
    total = _angle(_direction(start), _direction(end))
    previous = 0.0
    for s in np.linspace(0.05, 0.95, 19):
        pose = vc.interpolate_pose(start, end, s)
        d = _direction(pose)
        # Sull'arco di cerchio massimo: le due parti sommano l'angolo totale.
        assert _angle(_direction(start), d) + _angle(d, _direction(end)) == pytest.approx(total)
        assert _angle(_direction(start), d) > previous             # avanza sempre
        previous = _angle(_direction(start), d)
        assert abs(np.dot(pose.up, d)) < 1e-9                       # up sempre ortogonale
        distance = np.linalg.norm(np.subtract(pose.position, pose.focal))
        assert 10 < distance < 40
        assert 0 < pose.focal[0] < 2 and 30 < pose.view_angle < 35
    final = vc.interpolate_pose(start, end, 1.0)
    for got, want in zip(final, end):
        assert np.allclose(got, want, atol=1e-9)


def test_opposite_directions_orbit_sideways_about_the_start_up():
    """Sopra -> sotto: nessun arco unico; si gira attorno all'alto dello schermo."""
    start = _pose((0, 0, 1), (0, 1, 0))
    end = _pose((0, 0, -1), (0, 1, 0))
    middle = vc.interpolate_pose(start, end, 0.5)
    d = _direction(middle)
    assert abs(np.dot(d, (0, 0, 1))) < 1e-9 and abs(np.dot(d, (0, 1, 0))) < 1e-9
    for s in np.linspace(0, 1, 21):
        assert vc.interpolate_pose(start, end, s).up == pytest.approx((0, 1, 0))
    # Deterministico: il rumore numerico sull'arrivo non cambia il lato.
    noisy = _pose((1e-12, 0, -1), (0, 1, 0))
    assert _direction(vc.interpolate_pose(start, noisy, 0.5)) == pytest.approx(d, abs=1e-6)
    assert np.allclose(vc.interpolate_pose(start, end, 1.0).position, end.position)


def test_non_opposite_directions_do_not_take_the_sideways_orbit():
    """Negativo del caso degenere: fronte -> sopra resta nel piano dei due versi."""
    start = _pose((0, -1, 0), (0, 0, 1))
    end = _pose((0, 0, 1), (0, 1, 0))
    d = _direction(vc.interpolate_pose(start, end, 0.5))
    assert abs(d[0]) < 1e-9                        # nessuna componente laterale
    assert d == pytest.approx(np.array([0, -1, 1]) / np.sqrt(2))


def test_the_up_vector_never_flips_when_passing_near_a_pole():
    start = _pose((1, -1, 1), (0, 0, 1))
    end = _pose((0, 0, -1), (0, 1, 0))
    ups = [np.asarray(vc.interpolate_pose(start, end, s).up) for s in np.linspace(0, 1, 201)]
    steps = [_angle(a, b) for a, b in zip(ups, ups[1:])]
    assert max(steps) < np.radians(5)              # nessun salto da un fotogramma all'altro
    assert ups[-1] == pytest.approx((0, 1, 0))


def test_a_pure_half_turn_roll_is_deterministic():
    start = _pose((0, 0, 1), (0, 1, 0))
    end = _pose((0, 0, 1), (0, -1, 0))
    first = vc.interpolate_pose(start, end, 0.5).up
    assert first == pytest.approx(vc.interpolate_pose(start, end, 0.5).up)
    assert abs(first[0]) == pytest.approx(1)


# ── Animazione nel widget vero ──────────────────────────────────────

@pytest.fixture
def scene(viewer):
    """Anteprima con un modello e un orologio finto, per fotogrammi deterministici."""
    import pyvista as pv
    view, app = viewer
    view.add_mesh(pv.Box(bounds=(-10, 10, -5, 5, 0, 2)))
    view.show_view("front")
    clock = [0.0]
    view._camera_clock = lambda: clock[0]
    view.camera_animation_ms = 250
    return view, app, clock


def _reference(view, apply):
    """Posa che la chiamata immediata produce, poi si torna dov'eravamo."""
    before = view._camera_pose()
    apply()
    pose = view._camera_pose()
    view._set_camera_pose(before)
    return pose


def _same_pose(a, b):
    return all(np.allclose(x, y, rtol=1e-9, atol=1e-9) for x, y in zip(a, b))


def _tick(view, clock, seconds):
    clock[0] = seconds
    view._on_camera_tick()


def test_a_view_change_animates_and_lands_exactly_on_the_immediate_view(scene):
    view, _, clock = scene
    start = view._camera_pose()
    target = _reference(view, lambda: view.show_view("top"))
    view.animate_to_view("top")
    assert view.is_camera_animating()
    assert _same_pose(view._camera_pose(), start)          # nessun salto al clic
    total = _angle(_direction(start), _direction(target))
    for seconds in (0.05, 0.125, 0.2):
        _tick(view, clock, seconds)
        d = _direction(view._camera_pose())
        assert 0 < _angle(_direction(start), d) < total
        assert _angle(_direction(start), d) + _angle(d, _direction(target)) == pytest.approx(total)
    _tick(view, clock, 0.26)
    assert not view.is_camera_animating()
    assert _same_pose(view._camera_pose(), target)


def test_an_edge_animation_lands_on_view_direction(scene):
    view, _, clock = scene
    target = _reference(view, lambda: view.view_direction((1, 0, 1)))
    view.animate_to_direction((1, 0, 1))
    _tick(view, clock, 0.1)
    assert not _same_pose(view._camera_pose(), target)
    _tick(view, clock, 1.0)
    assert _same_pose(view._camera_pose(), target)


def test_top_to_bottom_animates_through_a_side_view(scene):
    view, _, clock = scene
    view.show_view("top")
    target = _reference(view, lambda: view.show_view("bottom"))
    view.animate_to_view("bottom")
    _tick(view, clock, 0.125)
    d = _direction(view._camera_pose())
    assert abs(d[2]) < 1e-6                         # a meta' strada guarda di lato
    assert view._camera_pose().up == pytest.approx((0, 1, 0))
    _tick(view, clock, 0.3)
    assert _same_pose(view._camera_pose(), target)


def test_a_new_request_restarts_from_the_current_camera(scene):
    view, _, clock = scene
    view.animate_to_view("top")
    _tick(view, clock, 0.1)
    middle = view._camera_pose()
    target = _reference(view, lambda: view.show_view("right"))
    clock[0] = 0.1
    view.animate_to_view("right")
    assert _same_pose(view._camera_animation[0], middle)   # riparte da qui, non dall'inizio
    assert _same_pose(view._camera_pose(), middle)
    _tick(view, clock, 0.2)
    assert view.is_camera_animating()
    _tick(view, clock, 0.4)
    assert _same_pose(view._camera_pose(), target)


def test_clicking_the_house_starts_an_animation_to_the_initial_view(scene):
    view, app, clock = scene
    target = _reference(view, lambda: view.show_view("iso"))
    x, y, w, h = view.view_cube.home_rect()
    _click(app, view, x + w / 2, y + h / 2)
    assert view.is_camera_animating()
    _tick(view, clock, 1.0)
    assert _same_pose(view._camera_pose(), target)


def test_zero_duration_jumps_like_before(scene):
    view, _, _ = scene
    view.camera_animation_ms = 0
    target = _reference(view, lambda: view.show_view("top"))
    view.animate_to_view("top")
    assert not view.is_camera_animating()
    assert _same_pose(view._camera_pose(), target)


def test_clearing_or_grabbing_the_camera_stops_the_animation(scene):
    view, app, clock = scene
    view.animate_to_view("top")
    view.clear_preview()
    assert not view.is_camera_animating() and not view._camera_timer.isActive()
    # Un gesto del mouse fuori dal cubo prende la camera: l'animazione si ferma.
    import pyvista as pv
    view.add_mesh(pv.Box())
    view.show_view("front")
    view.animate_to_view("top")
    from pyvistaqt import QtInteractor
    from unittest import mock
    with mock.patch.object(QtInteractor, "mousePressEvent", lambda self, event: None), \
            mock.patch.object(QtInteractor, "mouseReleaseEvent", lambda self, event: None):
        _click(app, view, 40, view.height() - 40)
        assert not view.is_camera_animating()
        # Negativo: un clic sul cubo (la casetta: la camera e' ferma sul
        # fronte, la faccia centrale non chiederebbe alcun movimento) la avvia.
        x, y, w, h = view.view_cube.home_rect()
        _click(app, view, x + w / 2, y + h / 2)
    assert view.is_camera_animating()


def test_closing_the_widget_stops_the_timer(scene):
    view, app, _ = scene
    view.animate_to_view("top")
    assert view._camera_timer.isActive()
    view.close()
    app.processEvents()
    assert not view._camera_timer.isActive()
    view._on_camera_tick()                          # un tick tardivo non fa danni


def test_the_real_timer_completes_the_animation(scene):
    import time
    view, app, _ = scene
    view._camera_clock = time.monotonic
    target = _reference(view, lambda: view.show_view("right"))
    view.animate_to_view("right")
    deadline = time.monotonic() + 5
    while view.is_camera_animating() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert not view.is_camera_animating()
    assert _same_pose(view._camera_pose(), target)
