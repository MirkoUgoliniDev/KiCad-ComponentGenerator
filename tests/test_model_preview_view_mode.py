"""@brief STEP preview: visible rotation, plan/isometric camera toggle, no flicker.

Segnalazione dell'utente: con un modello scelto, portare la rotazione da 0° a
90° faceva sparire l'anteprima («Caricamento…») e la riportava identica. Il
valore arrivava al renderer (i test del dialogo lo verificano), ma la camera
isometrica fissa guarda lungo una diagonale a 45°: per un pezzo quasi quadrato
e simmetrico rispetto a un proprio asse, un quarto di giro disegna soltanto lo
specchio della sagoma di partenza. Da qui la vista in pianta, predefinita e
confrontabile con la foto, in cui la rotazione avviene nel piano dell'immagine.
"""

import math
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PyQt6')
from PyQt6.QtCore import QThreadPool
from PyQt6.QtGui import QColor, QImage

from kicadgen.gui import model_candidates_dialog as gui


# --- Synthetic meshes -------------------------------------------------------

_BOX_FACES = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 5, 1), (0, 4, 5),
              (1, 6, 2), (1, 5, 6), (2, 7, 3), (2, 6, 7), (3, 4, 0), (3, 7, 4)]


def _box(x0, x1, y0, y1, z0, z1):
    return ([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], _BOX_FACES)


def _merge(*boxes):
    vertices, faces = [], []
    for box_vertices, box_faces in boxes:
        offset = len(vertices)
        vertices += box_vertices
        faces += [tuple(index + offset for index in face) for face in box_faces]
    return vertices, faces


#: Near-square module board (19.2 x 18 mm, the proportions that hid the turn)
#: carrying one tall marker body on its +X side, centred in Y: the part is
#: mirror-symmetric about its own X axis and about nothing else.
_BOARD = _box(-9.6, 9.6, -9.0, 9.0, 0.0, 0.8)
_MARKER = _box(4.0, 8.0, -2.0, 2.0, 0.8, 3.2)
_MODULE = _merge(_BOARD, _MARKER)


def _render(monkeypatch, mesh, rotation=0.0, view=gui.VIEW_PLAN, color=None):
    """Render a synthetic mesh through the real _thumbnail, no FreeCAD needed."""
    from kicadgen.cad3d import step_generator
    vertices, triangles = mesh

    class Shape:
        def tessellate(self, tolerance):
            return ([SimpleNamespace(x=x, y=y, z=z) for x, y, z in vertices],
                    list(triangles))

    monkeypatch.setattr(step_generator, 'find_freecadcmd', lambda: 'fake-freecad')
    monkeypatch.setitem(sys.modules, 'Part', SimpleNamespace(
        read=lambda path: Shape(), makeCompound=lambda shapes: shapes[0]))

    def run(command, **kwargs):
        exec(compile(Path(command[1]).read_text(encoding='utf-8'), command[1], 'exec'), {})

    monkeypatch.setattr(gui.subprocess, 'run', run)
    return gui._thumbnail('example.step', rotation, None, color, view)


def _mask(image):
    background = QColor(gui.PREVIEW_BACKGROUND).rgb()
    return {(x, y) for y in range(image.height()) for x in range(image.width())
            if image.pixel(x, y) != background}


def _iou(first, second):
    return len(first & second) / len(first | second)


def _tones(image, minimum=400):
    """rgb -> (pixel count, mean x, mean y) for every flat area of the render."""
    background = QColor(gui.PREVIEW_BACKGROUND).rgb()
    stats = {}
    for y in range(image.height()):
        for x in range(image.width()):
            rgb = image.pixel(x, y)
            if rgb == background:
                continue
            count, sx, sy = stats.get(rgb, (0, 0, 0))
            stats[rgb] = (count + 1, sx + x, sy + y)
    return {rgb: (count, sx / count, sy / count) for rgb, (count, sx, sy) in stats.items()
            if count >= minimum}


def _brightest_centre(image):
    """Screen position of the brightest large area: the top of the marker body."""
    tones = _tones(image)
    rgb = max(tones, key=lambda value: QColor.fromRgb(value).lightnessF())
    return tones[rgb][1] - 170, tones[rgb][2] - 170


#: Thin central post as tall as the marker: present in both renders below, it
#: pins the fitted scale and centre, so the two pictures differ only where the
#: marker is.
_POST = _box(-0.1, 0.1, -0.1, 0.1, 0.8, 3.2)


def _marker_centre(monkeypatch, rotation, view):
    """Screen offset from the canvas centre of the marker body, in any view.

    Works where the brightest tone cannot, in the isometric view the board top
    and the marker top share one tone: the marker is found as the pixels that
    change when it is added to the same board.
    """
    board = _merge(_BOARD, _POST)
    marked = _merge(_BOARD, _POST, _MARKER)
    plain = _render(monkeypatch, board, rotation, view)
    image = _render(monkeypatch, marked, rotation, view)
    changed = [(x, y) for y in range(340) for x in range(340)
               if plain.pixel(x, y) != image.pixel(x, y)]
    assert len(changed) > 200
    return (sum(x for x, _ in changed)/len(changed) - 170,
            sum(y for _, y in changed)/len(changed) - 170)


# --- Renderer ---------------------------------------------------------------

@pytest.mark.parametrize('rotation,expected', [
    (0.0, 'right'), (90.0, 'up'), (-90.0, 'down'), (180.0, 'left')])
def test_plan_view_turns_the_model_in_the_image_plane(app, monkeypatch, rotation, expected):
    """@brief La vista in pianta mostra la rotazione dove la si guarda: nel piano.

    Il corpo alto sta sul lato +X: a 0° e' a destra, e una rotazione positiva
    (antioraria vista dall'alto, convenzione KiCad) lo porta in alto, una
    negativa in basso, mezzo giro a sinistra. Quattro esiti diversi per quattro
    angoli: nessuno puo' coincidere con un altro per simmetria della camera.
    """
    dx, dy = _brightest_centre(_render(monkeypatch, _MODULE, rotation))
    direction = {'right': dx > 40 and abs(dy) < 15, 'left': dx < -40 and abs(dy) < 15,
                 'up': dy < -40 and abs(dx) < 15, 'down': dy > 40 and abs(dx) < 15}
    assert direction[expected], (rotation, dx, dy)


def test_plan_view_quarter_turn_is_the_picture_rotated_not_mirrored(app, monkeypatch):
    """@brief Positivo e negativo della stessa sagoma: ruotata si', specchiata no."""
    from PyQt6.QtGui import QTransform
    plan = _render(monkeypatch, _MODULE, 0.0)
    turned = _mask(_render(monkeypatch, _MODULE, 90.0))
    # Qt ha l'asse y verso il basso: -90 e' un quarto di giro antiorario a schermo.
    assert _iou(turned, _mask(plan.transformed(QTransform().rotate(-90)))) > 0.98
    # Lo specchio della vista a 0° mette il corpo alto dal lato opposto.
    mirrored = plan.mirrored(True, False)
    assert _brightest_centre(mirrored)[0] < -40
    assert _brightest_centre(_render(monkeypatch, _MODULE, 90.0))[1] < -40


def test_isometric_quarter_turn_of_a_symmetric_part_draws_only_a_mirror(app, monkeypatch):
    """@brief Riproduzione della causa: in isometrica il quarto di giro non si vede.

    La camera isometrica guarda lungo la diagonale x = -y. Per un pezzo
    simmetrico rispetto al proprio asse X, ruotarlo di -90° equivale a
    riflettere l'immagine: la sagoma coincide con lo specchio di quella a 0°,
    e su un pezzo quasi quadrato lo specchio e' quasi la stessa figura — cio'
    che l'utente ha visto. In pianta la stessa rotazione e' inconfondibile.
    """
    iso = _mask(_render(monkeypatch, _MODULE, 0.0, gui.VIEW_ISOMETRIC))
    iso_turned = _mask(_render(monkeypatch, _MODULE, -90.0, gui.VIEW_ISOMETRIC))
    mirror = {(339 - x, y) for x, y in iso}
    assert _iou(iso_turned, mirror) > 0.98
    # Quasi quadrato: anche senza specchiare la sagoma cambia pochissimo.
    assert _iou(iso_turned, iso) > 0.85
    # Negativo strutturale: in pianta la stessa rotazione non e' uno specchio.
    plan = _render(monkeypatch, _MODULE, 0.0)
    plan_turned = _render(monkeypatch, _MODULE, -90.0)
    assert _brightest_centre(plan.mirrored(True, False))[0] < -40
    assert _brightest_centre(plan_turned)[1] > 40


def _cross(a, b):
    return (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0])


#: The isometric projection this module used before the view toggle: screen x
#: along x-y, screen y down along (x+y)/2-z, depth along x+y+z.
_OLD_ISOMETRIC = ((0.7071, -0.7071, 0.0), (0.4082, 0.4082, -0.8165), (0.5774, 0.5774, 0.5774))


@pytest.mark.parametrize('view', [gui.VIEW_PLAN, gui.VIEW_ISOMETRIC])
def test_both_cameras_are_rotations_never_mirrors(view):
    """@brief Destra x su = verso l'osservatore: altrimenti l'immagine e' speculare."""
    right, down, towards = gui._CAMERAS[view]
    for row in (right, down, towards):
        assert math.isclose(sum(value*value for value in row), 1.0, abs_tol=1e-6)
    up = tuple(-value for value in down)
    assert _cross(right, up) == pytest.approx(towards, abs=1e-6)
    # Negativo: la vecchia proiezione dava l'opposto, cioe' uno specchio.
    old_right, old_down, old_towards = _OLD_ISOMETRIC
    old_up = tuple(-value for value in old_down)
    assert _cross(old_right, old_up) == pytest.approx(
        tuple(-value for value in old_towards), abs=1e-3)


@pytest.mark.parametrize('view', [gui.VIEW_PLAN, gui.VIEW_ISOMETRIC])
def test_positive_rotation_reads_counter_clockwise_in_both_views(app, monkeypatch, view):
    """@brief Stesso verso di rotazione in pianta e in isometrica.

    Angolo a schermo (y verso l'alto) del corpo alto: +90° lo fa crescere,
    -90° lo fa diminuire. Con la vecchia camera speculare l'isometrica girava
    al contrario della pianta.
    """
    def screen_angle(rotation):
        dx, dy = _marker_centre(monkeypatch, rotation, view)
        return math.atan2(-dy, dx)

    start = screen_angle(0.0)
    turn_positive = (screen_angle(90.0) - start + math.pi) % (2*math.pi) - math.pi
    turn_negative = (screen_angle(-90.0) - start + math.pi) % (2*math.pi) - math.pi
    assert turn_positive > 0.5
    assert turn_negative < -0.5


def test_plan_view_separates_heights_that_share_a_normal(app, monkeypatch):
    """@brief Positivo: scheda e corpo alto, stessa normale, quote diverse -> due toni."""
    tones = _tones(_render(monkeypatch, _MODULE))
    assert len(tones) == 2
    board, marker = sorted(tones, key=lambda rgb: tones[rgb][0], reverse=True)
    # Il corpo piu' alto e' il piu' chiaro, ed entrambi restano sopra lo sfondo.
    assert QColor.fromRgb(marker).lightnessF() > QColor.fromRgb(board).lightnessF() + 0.05
    assert QColor.fromRgb(board).lightnessF() > QColor(gui.PREVIEW_BACKGROUND).lightnessF() + 0.1


def test_plan_view_keeps_one_tone_for_faces_at_one_height(app, monkeypatch):
    """@brief Negativo: due corpi separati alla stessa quota non inventano toni."""
    twins = _merge(_box(-8, -2, -3, 3, 0, 2), _box(2, 8, -3, 3, 0, 2))
    tones = _tones(_render(monkeypatch, twins))
    assert len(tones) == 1


def test_plan_view_keeps_the_declared_body_colour(app, monkeypatch):
    """@brief Il colore del corpo sopravvive anche alla vista in pianta."""
    red = QColor('#b01818')
    tones = _tones(_render(monkeypatch, _MODULE, color='#b01818'))
    assert tones
    for rgb in tones:
        assert QColor.fromRgb(rgb).hslHueF() == pytest.approx(red.hslHueF(), abs=0.02)


def test_unknown_view_is_rejected_before_any_cad_run(monkeypatch):
    monkeypatch.setattr(gui.subprocess, 'run', lambda *a, **k: pytest.fail('CAD launched'))
    with pytest.raises(ValueError, match='Unknown preview view'):
        gui._thumbnail('example.step', 0.0, None, None, 'perspective')


# --- Dialog -----------------------------------------------------------------

@pytest.fixture
def dialog(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: None)
    candidates = []
    for index in range(3):
        path = tmp_path / f'{index}.step'
        path.write_text('test fixture')
        candidates.append(dict(path=str(path), package=f'Package {index}'))
    component = dict(name='U1', length_mm=19.2, width_mm=18, height_mm=3.2,
                     model_rotation_deg=0)
    window = gui.ModelCandidatesDialog(component, dict(candidates=candidates))
    yield window, candidates
    window.reject()
    QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()


def _wait(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def _image(color):
    image = QImage(12, 12, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def _shown_color(window):
    return QColor(window.preview_label.pixmap().toImage().pixel(5, 5)).name()


_COLORS = {0.0: '#0000ff', 90.0: '#00ff00', 45.0: '#ff0000'}


def test_the_chosen_rotation_is_the_one_drawn(dialog, app, monkeypatch):
    """@brief Positivo: 0° -> 90° disegna l'immagine a 90°, mai quella a 0°.

    Il renderer stub colora l'immagine secondo l'angolo ricevuto; il worker a 0°
    resta bloccato mentre l'utente porta la manopola a 90°, cosi' il risultato
    a 0° arriva *dopo* la richiesta a 90° e deve essere scartato.
    """
    window, candidates = dialog
    entered, release = threading.Event(), threading.Event()
    calls = []

    def thumbnail(path, rotation, dimensions, color, view):
        calls.append((rotation, view))
        if len(calls) == 2:
            entered.set()
            release.wait(2)
        return _image(_COLORS[rotation])

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    assert not window.auto_rotation.isChecked()
    window.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not window._workers and calls)
    assert _shown_color(window) == _COLORS[0.0]
    window.rotation.setValue(45)
    assert entered.wait(1)
    window.rotation.setValue(90)
    release.set()
    _wait(app, lambda: not window._workers and len(calls) == 3)
    assert calls == [(0.0, gui.VIEW_PLAN), (45.0, gui.VIEW_PLAN), (90.0, gui.VIEW_PLAN)]
    assert _shown_color(window) == _COLORS[90.0]


def test_rerendering_the_same_model_keeps_the_previous_picture(dialog, app, monkeypatch):
    """@brief Positivo: cambiare angolo non fa lampeggiare «Caricamento…»."""
    window, _ = dialog
    entered, release = threading.Event(), threading.Event()
    calls = []

    def thumbnail(path, rotation, *_):
        calls.append(rotation)
        if len(calls) == 2:
            entered.set()
            release.wait(2)
        return _image(_COLORS[rotation])

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    window.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not window._workers and calls)
    first = window.preview_label.pixmap().cacheKey()
    window.rotation.setValue(90)
    assert entered.wait(1)
    # Durante il nuovo calcolo resta l'immagine precedente, non il testo.
    assert window.preview_label.pixmap().cacheKey() == first
    assert window.preview_label.text() == ''
    release.set()
    _wait(app, lambda: not window._workers and len(calls) == 2)
    assert window.preview_label.pixmap().cacheKey() != first
    assert _shown_color(window) == _COLORS[90.0]


def test_another_model_never_borrows_the_previous_picture(dialog, app, monkeypatch):
    """@brief Negativo: un modello diverso sostituisce subito l'immagine vecchia.

    Tenere la figura del modello A mentre si calcola B farebbe credere di
    vedere B: qui, e alla prima selezione (nessuna immagine precedente), resta
    il testo di caricamento.
    """
    window, _ = dialog
    entered, release = threading.Event(), threading.Event()
    calls = []

    def thumbnail(path, rotation, *_):
        calls.append(path)
        if len(calls) == 2:
            entered.set()
            release.wait(2)
        return _image('#0000ff')

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    window.candidate_list.setCurrentRow(0)
    assert window.preview_label.text() == 'Caricamento della geometria STEP reale…'
    _wait(app, lambda: not window._workers and calls)
    assert not window.preview_label.pixmap().isNull()
    window.candidate_list.setCurrentRow(1)
    assert entered.wait(1)
    assert window.preview_label.pixmap().isNull()
    assert window.preview_label.text() == 'Caricamento della geometria STEP reale…'
    release.set()
    _wait(app, lambda: not window._workers and len(calls) == 2)


def test_view_toggle_defaults_to_plan_and_rerenders_with_the_same_rotation(dialog, app, monkeypatch):
    """@brief Pulsante a levetta: rilasciato = pianta (predefinita), premuto = isometrica."""
    window, candidates = dialog
    calls = []
    monkeypatch.setattr(gui, '_thumbnail',
                        lambda path, rotation, dims, color, view: calls.append((rotation, view))
                        or _image('#0000ff'))
    assert window.view_button.isCheckable()
    assert not window.view_button.isChecked()
    # Contratto cambiato di proposito (utente, 2026-09-24): il cubo e' un'icona
    # come le altre della colonna, con nome e spiegazione nel tooltip.
    assert window.view_button.toolTip() == (
        'Vista isometrica\nPremuto: vista isometrica, per ispezionare il modello. '
        'Rilasciato: vista in pianta, confrontabile con la foto.')
    assert not window.view_button.icon().isNull()
    window.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not window._workers and calls)
    window.rotation.setValue(90)
    _wait(app, lambda: not window._workers and len(calls) == 2)
    window.view_button.setChecked(True)
    _wait(app, lambda: not window._workers and len(calls) == 3)
    window.view_button.setChecked(False)
    _wait(app, lambda: not window._workers and len(calls) == 4)
    assert calls == [(0.0, gui.VIEW_PLAN), (90.0, gui.VIEW_PLAN),
                     (90.0, gui.VIEW_ISOMETRIC), (90.0, gui.VIEW_PLAN)]
    # La vista e' solo un modo di guardare: la selezione non cambia.
    window.accept()
    assert window.selected_selection() == dict(
        status='confirmed', path=candidates[0]['path'], rotation_deg=90.0)


def test_view_toggle_without_a_model_only_remembers_the_choice(dialog, app, monkeypatch):
    """@brief Negativo: senza modello scelto nessun calcolo e nessun testo d'errore."""
    window, _ = dialog
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: calls.append(args))
    before, token = window.preview_label.text(), window._token
    window.view_button.setChecked(True)
    app.processEvents()
    assert window.preview_label.text() == before
    assert window._token == token and not window._workers and not calls
    assert window._view == gui.VIEW_ISOMETRIC


def test_view_toggle_sits_in_the_top_right_corner_of_the_preview(dialog, app):
    """@brief Il pulsante e' agganciato all'angolo in alto a destra dell'anteprima."""
    window, _ = dialog
    window.show()
    app.processEvents()
    label, button = window.preview_label, window.view_button
    # Contratto cambiato di proposito (utente, 2026-09-24): il cubo e' il
    # primo di una colonna di icone (sotto, le frecce di rotazione), quindi il
    # confronto si fa in coordinate della finestra, non fra fratelli.
    assert button.isVisible()
    area = label.geometry().translated(label.parent().mapTo(window, label.parent().rect().topLeft()))
    box = button.rect().translated(button.mapTo(window, button.rect().topLeft()))
    assert box.top() == area.top()
    assert box.right() == area.right()
    # Sopra l'anteprima, non accanto: il pulsante cade dentro l'area del label.
    assert area.contains(box)
    # Le frecce stanno sotto il cubo, dentro la stessa area.
    ccw = window.rotate_ccw_button
    ccw_box = ccw.rect().translated(ccw.mapTo(window, ccw.rect().topLeft()))
    assert ccw_box.top() > box.bottom() and area.contains(ccw_box)
    # Negativo: la foto a sinistra non ospita il pulsante.
    photo = window.photo_label
    photo_right = photo.mapTo(window, photo.rect().topRight()).x()
    assert button.mapTo(window, button.rect().topLeft()).x() > photo_right


def test_a_preview_taller_than_its_label_is_shown_whole(dialog, app, monkeypatch):
    """@brief Positivo: la miniatura 340x340 in un'area piu' bassa non si taglia.

    Tagliata al centro, un pezzo lungo ruotato di 90° perdeva proprio le
    estremita' che mostrano la rotazione.
    """
    window, _ = dialog
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: _image('#0000ff').scaled(340, 340))
    window.resize(820, 650)
    window.show()
    window.preview_label.setFixedHeight(240)
    window.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not window._workers and not window.preview_label.pixmap().isNull())
    app.processEvents()
    room = window.preview_label.contentsRect().size()
    shown = window.preview_label.pixmap().size()
    assert shown.height() <= room.height() and shown.width() <= room.width()
    assert shown.width() == shown.height()


def test_a_preview_with_room_is_never_enlarged(dialog, app, monkeypatch):
    """@brief Negativo: con spazio in abbondanza resta 340 px, non si sfoca."""
    window, _ = dialog
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: _image('#0000ff').scaled(340, 340))
    window.show()
    window.preview_label.setFixedSize(600, 600)
    window.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not window._workers and not window.preview_label.pixmap().isNull())
    app.processEvents()
    assert window.preview_label.pixmap().size().width() == 340
    assert window.preview_label.pixmap().size().height() == 340


def test_the_cube_matches_the_rotation_icons_in_size_and_column(dialog, app):
    """@brief Il cubo ha la misura delle icone sotto e sta nella loro colonna.

    Positivo: stessa misura d'icona e stessa larghezza di pulsante, bordo
    destro comune. Negativo di controllo: l'etichetta dell'angolo non e' un
    pulsante e puo' essere piu' larga senza spostare le icone.
    """
    window, _ = dialog
    window.show()
    app.processEvents()
    buttons = (window.view_button, window.rotate_ccw_button,
               window.rotate_cw_button, window.rotate_auto_button)
    assert {b.iconSize().width() for b in buttons} == {window.rotate_ccw_button.iconSize().width()}
    assert len({b.size().width() for b in buttons}) == 1
    assert len({b.size().height() for b in buttons}) == 1
    rights = {b.mapTo(window, b.rect().topRight()).x() for b in buttons}
    lefts = {b.mapTo(window, b.rect().topLeft()).x() for b in buttons}
    assert len(rights) == 1 and len(lefts) == 1
    for button in buttons:
        first, _, rest = button.toolTip().partition(chr(10))
        assert first and rest


def test_a_ring_turns_while_the_preview_is_being_drawn(dialog, app, monkeypatch):
    """@brief Utente 26/09: «qui ci vuole un loader». Visibile durante il disegno, via all'arrivo."""
    window, _ = dialog
    entered, release = threading.Event(), threading.Event()

    def thumbnail(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return _image('#0000ff')

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    window.show()
    window.candidate_list.setCurrentRow(0)
    assert entered.wait(1)
    assert window.preview_loader.isVisibleTo(window) and window._loader_timer.isActive()
    angle = window.preview_spinner.angle
    _wait(app, lambda: window.preview_spinner.angle != angle)
    release.set()
    _wait(app, lambda: not window._workers)
    assert not window.preview_loader.isVisibleTo(window) and not window._loader_timer.isActive()


def test_no_ring_when_there_is_nothing_to_draw(dialog, app, monkeypatch, tmp_path):
    """@brief Negativo: un file STEP non disponibile non fa girare nulla."""
    window, _ = dialog
    window._path = str(tmp_path / "manca.step")
    window._start_preview()
    assert not window.preview_loader.isVisibleTo(window) and not window._loader_timer.isActive()
