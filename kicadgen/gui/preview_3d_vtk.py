"""@brief Interactive 3D preview based on VTK/PyVista with a real z-buffer.

Anteprima 3D con VTK/PyVista: renderer con z-buffer reale e interattivo.

Sostituisce l'anteprima matplotlib (kicadgen.gui.preview_3d), che usa
l'algoritmo del pittore ordinando i poligoni per centroide: sulle geometrie
a filo -- le serigrafie appoggiate sulla superficie del corpo -- quell'ordine
sbaglia e "mangia" pezzi della scritta. VTK fa hidden-surface removal per
pixel, quindi il problema non si pone, e in piu' la vista si ruota col mouse.

Se pyvista/pyvistaqt non sono installati, main_window ricade sulla versione
matplotlib: l'anteprima e' comodita', non deve essere un requisito duro.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Optional
from ..services.messages import tx

os.environ.setdefault("QT_API", "pyqt6")

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from pyvistaqt import QtInteractor

from ..cad3d.body_params import Body3DParams
from ..generators.pin_header import PinHeaderParams, pin_positions

#: Spostamento massimo, in pixel, perche' un tasto destro conti ancora come
#: clic e non come l'inizio di uno zoom a trascinamento. E' la tolleranza di
#: mano ferma, non una soglia legata a un contenuto particolare.
_CLICK_TOLERANCE_PX = 4

_BG_COLOR = "#1e1e2e"
_APPROX_BODY_COLOR = "#89b4fa"
_APPROX_PIN_COLOR = "#f9e2af"
_LABEL_COLOR = "#f9e2af"


def _component_index(name):
    """Parse only FreeCAD component names; board bodies and pins are not rows."""
    match = re.fullmatch(r'Cmp([0-9]+)_.+', name) if isinstance(name, str) else None
    if match is None:
        return None
    try:
        index = int(match.group(1))
        return index if index <= 2_147_483_647 else None
    except ValueError:
        return None


def _component_row(part):
    """Prefer original photo rows over filtered CAD indices and legacy names.

    Only nonnegative JSON integers fitting the Qt int signal are accepted;
    booleans, numeric strings and floats are not row identifiers. Invalid or
    absent metadata falls back to the next source, never to render order.
    """
    for field in ('source_row', 'component_index'):
        value = part.get(field)
        if type(value) is int and 0 <= value <= 2_147_483_647:
            return value
    return _component_index(part.get('name'))


#: Prepared scenes kept in memory, least recently used dropped first. The
#: window shows one generated model at a time; two entries also cover going
#: back and forth between two files. The real 13 MB preview of a module (171
#: parts, 287k triangles) takes ~27 MB once prepared, so the cache stays small.
_MESH_CACHE_SIZE = 2


class _PreparedPart(NamedTuple):
    """One drawable part: normals already computed, row for picking."""
    mesh: object
    color: object
    row: Optional[int]


class _PreparedScene(NamedTuple):
    """Everything render_mesh needs from a preview JSON, ready to draw."""
    parts: tuple
    anchors: tuple


def _mesh_file_key(mesh_path) -> tuple:
    """@brief Identity of a preview file: resolved path, device, inode, size, mtime.

    @param mesh_path Path of the preview JSON.
    @return Hashable key; a rewritten file gets a new one.
    @throws OSError when the file cannot be inspected.
    @note Content is not hashed (hashing 13 MB at every tab switch is what we
        want to avoid). A rewrite that keeps size, inode and mtime_ns would go
        unnoticed; with nanosecond mtimes that needs a same-size rewrite within
        the filesystem timestamp resolution (2 s on FAT) -- regenerating a model
        takes much longer than that.
    """
    resolved = Path(mesh_path).resolve()
    stat = resolved.stat()
    return (str(resolved), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _prepare_mesh_scene(mesh_path) -> _PreparedScene:
    """@brief Read a FreeCAD preview JSON and build the meshes to draw.

    @param mesh_path Path of the preview JSON (parts with vertices/triangles).
    @return Prepared scene: one entry per part with triangles, pin label anchors.
    @throws ValueError when the file has no parts or no triangles.
    @note Pure data work, nothing touches the render window: a failure leaves
        the scene on screen as it was.
    """
    import pyvista as pv

    with open(mesh_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    parts = data.get("parts")
    if not parts:
        raise ValueError("mesh vuota")

    prepared = []
    anchors = []
    for part in parts:
        vertices = np.asarray(part["vertices"], dtype=float)
        triangles = np.asarray(part.get("triangles", []), dtype=int)
        if len(vertices) == 0 or len(triangles) == 0:
            continue

        # I pezzi dei piedini si chiamano "Pin<numero>" (vedi
        # freecad_build_body.py): da li' si ricava l'etichetta, ancorata
        # alla punta del pin.
        name = str(part.get("name", ""))
        if name.startswith("Pin") and name[3:].isdigit():
            label = part.get("label")
            if label:
                # Punto scelto da chi ha costruito il pezzo (es. accanto al
                # semi-foro di un modulo, non sopra): il numero non copre il metallo.
                point = _spaced_label(label, vertices)
                anchors.append((int(name[3:]), *point))
            else:
                centre = vertices.mean(axis=0)
                anchors.append(
                    (int(name[3:]), float(centre[0]), float(centre[1]),
                     float(vertices[:, 2].min()))
                )
        # PyVista vuole le facce come [n_punti, i, j, k, n_punti, ...]
        faces = np.hstack([np.full((len(triangles), 1), 3), triangles]).ravel()
        mesh = pv.PolyData(vertices, faces)
        # Normali con feature angle: gli spigoli vivi restano netti, mentre
        # raccordi e cilindri dei pin vengono sfumati e si leggono come curvi
        # (con l'ombreggiatura piatta i fillet piccoli sparivano sul nero).
        mesh = mesh.compute_normals(
            feature_angle=30, split_vertices=True, consistent_normals=True,
        )
        prepared.append(_PreparedPart(mesh, part.get("color", _APPROX_BODY_COLOR),
                                      _component_row(part)))

    if not prepared:
        raise ValueError(tx("mesh senza triangoli"))
    return _PreparedScene(tuple(prepared), tuple(anchors))


def _spaced_label(label, vertices):
    """Keep legacy mesh labels away from the contact, without changing CAD."""
    point = np.asarray(label, dtype=float).copy()
    centre = (vertices.min(axis=0) + vertices.max(axis=0)) / 2
    direction = point[:2] - centre[:2]
    distance = np.linalg.norm(direction)
    if distance > 1e-6:
        point[:2] += 1.2 * direction / distance
    return point


class Preview3DVtk(QtInteractor):
    """@brief Interactive 3D view: the real generated mesh or the parametric approximation."""

    component_requested = pyqtSignal(int)
    #: Tasto destro su un componente: indice di riga e punto globale dove
    #: aprire il menu. La vista non sa cosa si possa fare su un componente --
    #: le voci le costruisce la finestra, che ha la tabella e i modelli.
    component_menu_requested = pyqtSignal(int, object)

    #: Duration of the animated view change asked by the navigation cube, in
    #: ms; 0 or less jumps at once. 250 ms is the usual slicer/CAD feel.
    camera_animation_ms = 250
    #: Frame period of the animation (about 60 frames per second).
    CAMERA_FRAME_MS = 16

    def __init__(self, parent=None):
        """@brief Initialize the viewport and optional component picking.

        @param parent Optional Qt parent widget.
        """
        self._component_actors = {}
        self._right_press = None
        self._menu_served = False
        # Scena mostrata e scene gia' preparate, per chiave d'identita' del file.
        self._mesh_cache = OrderedDict()
        self._shown_mesh_key = None
        self._mesh_anchors = ()
        # Animazione della camera (cubo di navigazione).
        self._camera_animation = None
        self._camera_timer = None
        self._camera_clock = time.monotonic
        super().__init__(parent)
        self._camera_timer = QTimer(self)
        self._camera_timer.setInterval(self.CAMERA_FRAME_MS)
        self._camera_timer.timeout.connect(self._on_camera_tick)
        self.setToolTip('Doppio clic su un componente: scegli modello KiCad e ruota il modello.\n'
                        'Tasto destro: elimina, ruota o cambia il modello.')
        self._configure_component_picking()
        self._configure_component_double_click()
        self.set_background(_BG_COLOR)
        self.show_pin_numbers = True
        try:
            # Illuminazione a tre punti: da' rilievo ai pezzi scuri, dove la
            # luce frontale singola appiattisce tutto.
            self.enable_lightkit()
        except Exception:  # noqa: BLE001 - illuminazione di default
            pass
        self._label_actor = None
        # Cubo di navigazione in alto a destra (al posto della riga di
        # pulsanti «Vista»). E' una comodita': se VTK non ha il modulo delle
        # annotazioni la vista funziona lo stesso, si ruota col mouse.
        self.view_cube = None
        self._cube_press = False
        try:
            from .view_cube import ViewCube
            self.view_cube = ViewCube(self.ren_win, self.renderer)
            self.view_cube.layout(self.width(), self.height())
        except Exception:  # noqa: BLE001 - il cubo e' accessorio
            self.view_cube = None
        # Le etichette si aggiornano anche quando l'utente ruota col mouse, non
        # solo premendo i pulsanti di vista.
        try:
            self.iren.add_observer("EndInteractionEvent", self._on_interaction_end)
        except Exception:  # noqa: BLE001 - senza interattore le etichette restano statiche
            pass

    def _on_interaction_end(self, *_args) -> None:
        self._update_label_visibility()

    def _configure_component_picking(self):
        # Actor callbacks identify all subparts without replacing shaders or
        # adding a highlighted mesh. Left-button camera rotation stays enabled.
        try:
            self.enable_mesh_picking(callback=self._component_picked, use_actor=True,
                                     left_clicking=False, show=False, show_message=False)
        except (AttributeError, RuntimeError, ImportError):
            pass  # Optional interaction must not prevent rendering a model.

    def _configure_component_double_click(self):
        """Register a real VTK double-click without disabling camera rotation."""
        try:
            self.iren.add_observer("LeftButtonDoubleClickEvent",
                                   self._component_double_clicked)
        except (AttributeError, RuntimeError, ImportError):
            pass

    def _component_actor_at_cursor(self):
        try:
            return self._component_actor_at(*self.iren.GetEventPosition())
        except (AttributeError, RuntimeError, ImportError):
            return None

    def _component_actor_at(self, x, y):
        """@brief Attore sotto un punto gia' espresso in coordinate VTK."""
        try:
            from vtkmodules.vtkRenderingCore import vtkPropPicker
            picker = vtkPropPicker()
            if not picker.Pick(x, y, 0, self.renderer):
                return None
            return picker.GetActor()
        except (AttributeError, RuntimeError, ImportError):
            return None

    def _component_row_at_widget_point(self, x, y):
        """@brief Riga della tabella sotto un punto in coordinate Qt.

        L'asse Y va rovesciato: Qt conta dall'alto, VTK dal basso. Il
        rapporto di scala serve sui monitor ad alta densita', dove i pixel
        logici di Qt non sono quelli del framebuffer.
        """
        try:
            ratio = float(self.devicePixelRatioF())
        except (AttributeError, TypeError, ValueError):
            ratio = 1.0
        actor = self._component_actor_at(x * ratio, (self.height() - y) * ratio)
        if actor is None:
            return None
        return self._component_actors.get(actor.GetAddressAsString(''))

    def _request_component_menu(self, position, global_position) -> bool:
        """@brief Emetti la richiesta di menu, se sotto il punto c'e' un componente.

        @return True se la richiesta e' partita, cosi' il chiamante sa di aver
                gia' servito questo clic e non lo serve una seconda volta.
        """
        index = self._component_row_at_widget_point(position.x(), position.y())
        if index is None:
            return False
        self._menu_served = True
        self.component_menu_requested.emit(index, global_position)
        return True

    # ── Cubo di navigazione ─────────────────────────────────────────

    def set_view_cube_labels(self, labels: dict) -> None:
        """@brief Translate the face labels of the navigation cube.

        @param labels Mapping view name (top, bottom, front, back, right, left) -> text.
        """
        if self.view_cube is not None:
            self.view_cube.set_labels(labels)
            self.render()

    def view_direction(self, direction) -> None:
        """@brief Look at the model from a direction (edge or corner of the cube), at once.

        @param direction Vector pointing from the model towards the camera.
        @note Stops a running camera animation: the last request wins.
        """
        self.stop_camera_animation()
        norm = float(np.linalg.norm(direction))
        if norm <= 0:
            return
        unit = np.asarray(direction, dtype=float) / norm
        focal = np.asarray(self.camera.focal_point, dtype=float)
        distance = float(np.linalg.norm(np.asarray(self.camera.position) - focal)) or 1.0
        # Con la camera quasi verticale l'alto non puo' essere Z.
        up = (0.0, 0.0, 1.0) if abs(unit[2]) < 0.99 else (0.0, 1.0, 0.0)
        self.camera_position = [tuple(focal + unit * distance), tuple(focal), up]
        self.fit_view()

    def _cube_clicked(self, target) -> None:
        """Apply what a click on the cube selected: home, a face or a diagonal."""
        from .view_cube import view_for_direction
        if target == "home":
            self.animate_to_view("iso")
            return
        view = view_for_direction(target)
        if view is not None:
            self.animate_to_view(view)
        else:
            self.animate_to_direction(target)

    # ── Animazione della camera ─────────────────────────────────────

    def animate_to_view(self, view: str) -> None:
        """@brief Turn the camera smoothly to a standard view.

        @param view Name accepted by show_view (iso, top, bottom, front, ...).
        @note The last frame is show_view(view) itself, so the final camera
            is exactly the one the immediate call gives, framing included.
        """
        self._animate_camera(lambda: self.show_view(view))

    def animate_to_direction(self, direction) -> None:
        """@brief Turn the camera smoothly to look from a direction.

        @param direction Vector pointing from the model towards the camera.
        @note The last frame is view_direction(direction) itself.
        """
        self._animate_camera(lambda: self.view_direction(direction))

    def is_camera_animating(self) -> bool:
        """@brief Whether an animated view change is in progress."""
        return self._camera_animation is not None

    def stop_camera_animation(self) -> None:
        """@brief Stop an animated view change where it is (no jump to its end).

        @note Safe on a half-built widget: clear() can run before __init__ ends.
        """
        timer = getattr(self, "_camera_timer", None)
        if timer is not None:
            timer.stop()
        self._camera_animation = None

    def _camera_pose(self):
        from .view_cube import CameraPose
        camera = self.camera
        return CameraPose(tuple(camera.GetPosition()), tuple(camera.GetFocalPoint()),
                          tuple(camera.GetViewUp()), float(camera.GetParallelScale()),
                          float(camera.GetViewAngle()))

    def _set_camera_pose(self, pose) -> None:
        camera = self.camera
        camera.SetFocalPoint(*pose.focal)
        camera.SetPosition(*pose.position)
        camera.SetViewUp(*pose.up)
        camera.SetParallelScale(pose.parallel_scale)
        camera.SetViewAngle(pose.view_angle)
        self.renderer.ResetCameraClippingRange()

    @contextmanager
    def _rendering_suppressed(self):
        """Batch scene or camera changes into one final render."""
        previous = self.suppress_rendering
        self.suppress_rendering = True
        try:
            yield
        finally:
            self.suppress_rendering = previous

    def _pose_after(self, apply):
        """Camera pose that `apply` would produce, without showing it.

        The camera, the "camera already set" flag and the pin label
        visibility are restored afterwards; nothing is rendered meanwhile.
        """
        from vtkmodules.vtkRenderingCore import vtkCamera
        saved = vtkCamera()
        saved.DeepCopy(self.camera)
        camera_set = self.renderer.camera_set
        labels = self._label_actor.GetVisibility() if self._label_actor is not None else None
        try:
            with self._rendering_suppressed():
                apply()
            return self._camera_pose()
        finally:
            self.camera.DeepCopy(saved)
            self.renderer.camera_set = camera_set
            if labels is not None:
                self._label_actor.SetVisibility(labels)

    def _animate_camera(self, apply) -> None:
        """Start (or restart, from the current camera) an animation towards `apply`'s view."""
        self.stop_camera_animation()
        duration = float(self.camera_animation_ms or 0) / 1000.0
        if duration <= 0 or self._camera_timer is None:
            apply()
            return
        start = self._camera_pose()
        end = self._pose_after(apply)
        if all(np.allclose(a, b, rtol=1e-9, atol=1e-9) for a, b in zip(start, end)):
            apply()  # gia' li': niente da animare, solo l'inquadratura finale
            return
        self._camera_animation = (start, end, apply, self._camera_clock(), duration)
        self._camera_timer.start()

    def _on_camera_tick(self) -> None:
        """One animation frame; the last one applies the exact target view."""
        from .view_cube import ease_in_out, interpolate_pose
        animation = self._camera_animation
        if animation is None or getattr(self, "_closed", False):
            self.stop_camera_animation()
            return
        start, end, apply, started, duration = animation
        progress = (self._camera_clock() - started) / duration
        if progress >= 1.0:
            self.stop_camera_animation()
            apply()
            return
        self._set_camera_pose(interpolate_pose(start, end, ease_in_out(progress)))
        if self._label_actor is not None:
            self._label_actor.SetVisibility(self.show_pin_numbers and self._viewing_from_below())
        self.render()

    def _on_cube(self, event) -> bool:
        """True when a mouse event falls on the cube or on the house."""
        if self.view_cube is None:
            return False
        point = event.position()
        return self.view_cube.contains(point.x(), point.y())

    def resizeEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Keep the cube in the top-right corner at a fixed pixel size."""
        super().resizeEvent(event)
        if self.view_cube is not None:
            self.view_cube.layout(self.width(), self.height())

    def closeEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Stop the camera animation before the render window goes away."""
        self.stop_camera_animation()
        super().closeEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - firma Qt
        """@brief A wheel zoom takes over the camera: stop a running view animation."""
        self.stop_camera_animation()
        super().wheelEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Pointing-hand cursor over the cube, so it reads as clickable."""
        if self._on_cube(event):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - firma Qt
        """@brief A double click on the cube is two clicks on the cube, not a component pick."""
        if self._on_cube(event):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Serve clicks on the cube; remember where a right press starts.

        @note A left click on the cube never reaches VTK: it would also start
            a camera rotation, and the view would turn under the cursor.
            Outside the cube's hit area (and in the empty corners of its
            square) the mouse keeps its usual meaning, and any press there
            stops a running view animation.
        """
        if event.button() == Qt.MouseButton.LeftButton and self._on_cube(event):
            point = event.position()
            target = self.view_cube.hit(point.x(), point.y())
            if target is not None:
                self._cube_press = True
                event.accept()
                self._cube_clicked(target)
                return
        # Qualunque altro gesto prende la camera in mano: l'animazione del
        # cubo non deve continuare a muoverla sotto il mouse.
        self.stop_camera_animation()
        if event.button() == Qt.MouseButton.RightButton:
            self._right_press = event.pos()
            self._menu_served = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Apri il menu su un clic destro fermo sopra un componente.

        Qt sintetizza `contextMenuEvent` dal messaggio di sistema, e sopra la
        finestra nativa che VTK si crea non e' detto che arrivi; il rilascio
        del pulsante invece passa sempre di qui, perche' l'interattore Qt di
        VTK e' un QWidget normale che lo inoltra. Si serve quindi il clic qui,
        e `contextMenuEvent` resta per il tasto menu della tastiera.

        Il tasto destro di VTK e' anche lo zoom a trascinamento: si apre il
        menu solo se il mouse non si e' spostato, altrimenti si e' chiesto
        uno zoom e il menu sarebbe un fastidio.
        """
        if getattr(self, "_cube_press", False) and event.button() == Qt.MouseButton.LeftButton:
            # Il clic sul cubo e' gia' servito alla pressione: VTK non ha visto
            # la pressione e non deve vedere nemmeno il rilascio.
            self._cube_press = False
            event.accept()
            return
        start = getattr(self, "_right_press", None)
        self._right_press = None
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.RightButton or start is None:
            return
        if (event.pos() - start).manhattanLength() > _CLICK_TOLERANCE_PX:
            return
        self._request_component_menu(event.pos(), event.globalPosition().toPoint())

    def contextMenuEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Via alternativa al menu (tasto dedicato, o piattaforme che
        consegnano questo evento invece del rilascio del pulsante)."""
        if getattr(self, "_menu_served", False):
            # Il clic e' gia' stato servito dal rilascio: un secondo menu
            # per lo stesso gesto sarebbe solo un doppione.
            self._menu_served = False
            event.accept()
            return
        if self._request_component_menu(event.pos(), event.globalPos()):
            event.accept()
            return
        # Fuori dai componenti il tasto destro resta quello di VTK.
        super().contextMenuEvent(event)

    def _component_double_clicked(self, *_args):
        actor = self._component_actor_at_cursor()
        self._component_picked(actor)

    def _component_picked(self, actor):
        if actor is None:
            return
        index = self._component_actors.get(actor.GetAddressAsString(''))
        if index is not None:
            self.component_requested.emit(index)

    def clear(self) -> None:
        """@brief Clear actors and invalidate component picking for the old mesh.

        @note Also forgets which mesh file is shown (is_showing becomes False)
            and stops a camera animation; the prepared-mesh cache is kept, so
            showing the same file again does not re-read it.
        """
        self._component_actors.clear()
        self._shown_mesh_key = None
        self._mesh_anchors = ()
        self.stop_camera_animation()
        super().clear()

    def set_pin_numbers(self, enabled: bool) -> None:
        """@brief Enable or disable the pin number labels (still shown only from the pin side).

        Attiva/disattiva le etichette dei pin (restano comunque limitate alla
        vista dal lato piedini)."""
        self.show_pin_numbers = enabled
        self._update_label_visibility()

    def _viewing_from_below(self) -> bool:
        """True se la camera guarda il componente da sotto (Z negativo).

        Il piano Z=0 e' quello di saldatura: i piedini stanno sotto, il corpo
        sopra, quindi una camera a Z negativo sta guardando il lato pin.
        """
        try:
            return float(self.camera.position[2]) < 0.0
        except Exception:  # noqa: BLE001 - camera non disponibile
            return False

    def _update_label_visibility(self) -> None:
        """I numeri compaiono solo dal lato dei pin: sopra il corpo li copre e
        farebbero solo confusione."""
        if self._label_actor is None:
            return
        self._label_actor.SetVisibility(self.show_pin_numbers and self._viewing_from_below())
        self.render()

    def _add_pin_labels(self, anchors) -> None:
        """Numeri centrati sugli ancoraggi, visibili solo dal lato piedini."""
        self._label_actor = None
        if not self.show_pin_numbers or not anchors:
            return
        import numpy as np

        points = np.array([[x, y, z] for _, x, y, z in anchors], dtype=float)
        labels = [str(pin) for pin, _, _, _ in anchors]
        self._label_actor = self.add_point_labels(
            points, labels, font_size=13, bold=True, text_color=_LABEL_COLOR,
            shape=None, show_points=False, always_visible=True,
            justification_horizontal="center", justification_vertical="center",
        )

    def _reset(self) -> None:
        self.clear()
        self.set_background(_BG_COLOR)

    def _finish(self) -> None:
        self.show_axes()
        # Tre luci invece della singola frontale: una superficie curva illuminata
        # di fronte rende quasi piatta, e su un case scuro i raccordi sparirebbero.
        self.enable_3_lights()
        self.show_view("iso")

    def fit_view(self) -> None:
        """@brief Frame the whole component with a margin after resynchronizing the render window size.

        Inquadra tutto il componente lasciando un margine.
        """
        # VTK puo' avere in cache una dimensione della render window diversa
        # da quella attuale del widget Qt -- capita quando il widget vive
        # dentro un layout che cambia dimensione dopo il primo render.
        # Il renderer non e' contenuto nell'area scorrevole delle impostazioni.
        # Senza risincronizzarla,
        # reset_camera() inquadra rispetto alla vecchia area: il modello
        # finisce centrato in un riquadro che non e' piu' quello visibile,
        # e appare spostato rispetto alla finestra vera.
        try:
            ratio = self.devicePixelRatioF()
            self.window_size = (max(1, round(self.width() * ratio)),
                                max(1, round(self.height() * ratio)))
        except Exception:  # noqa: BLE001 - risincronizzazione best-effort
            pass
        self.reset_camera()
        self.camera.zoom(0.85)  # senza margine i pin restano fuori inquadratura
        self._update_label_visibility()
        self.render()

    def show_view(self, view: str) -> None:
        """@brief Standard view at once: iso / top / bottom / front / back / left / right.

        @param view View name; anything else is the initial isometric view.
        @note Stops a running camera animation: the last request wins.
        """
        self.stop_camera_animation()
        if view == "top":
            self.view_xy()
        elif view == "bottom":
            self.view_xy(negative=True)
        elif view == "front":
            self.view_xz()
        elif view == "back":
            self.view_xz(negative=True)
        elif view == "right":
            self.view_yz()
        elif view == "left":
            self.view_yz(negative=True)
        else:
            self.view_isometric()
            self.camera.azimuth = -35
            self.camera.elevation = 10
        self.fit_view()

    def clear_preview(self) -> None:
        """@brief Empty the preview; the next render_mesh() redraws even the same file."""
        self._reset()
        self.render()

    def is_showing(self, mesh_path) -> bool:
        """@brief Whether this very file, unchanged, is the mesh on screen.

        Cheap (one stat call): the window uses it to decide whether entering
        the 3D tab needs a loading indicator.

        @param mesh_path Path of a preview JSON.
        @return True when render_mesh(mesh_path) would redraw nothing: the
            same resolved file, with the same size and modification time, is
            displayed and the scene was not cleared or replaced since.
        """
        if self._shown_mesh_key is None or not mesh_path:
            return False
        try:
            return _mesh_file_key(mesh_path) == self._shown_mesh_key
        except (OSError, TypeError, ValueError):
            return False

    def render_params(self, pin_params: PinHeaderParams, body: Body3DParams) -> None:
        """@brief Approximate preview before generation: a box and pin cylinders.

        @param pin_params Pin layout.
        @param body Body dimensions.
        @throws ValueError when the body parameters are invalid.
        @note Drawn with one render at the end, not one per pin.
        """
        import pyvista as pv

        body.validate()
        with self._rendering_suppressed():
            self._draw_params(pv, pin_params, body)
        self.render()

    def _draw_params(self, pv, pin_params, body) -> None:
        """Build the approximate scene; the caller renders once at the end."""
        self._reset()

        box = pv.Box(bounds=(
            body.offset_x_mm, body.offset_x_mm + body.length_mm,
            body.offset_y_mm, body.offset_y_mm + body.width_mm,
            0.0, body.height_mm,
        ))
        self.add_mesh(box, color=_APPROX_BODY_COLOR, opacity=0.65, specular=0.2,
                      reset_camera=False, render=False)

        radius = body.pin_diameter_mm / 2.0
        anchors = []
        for number, x, y in pin_positions(pin_params):
            pin = pv.Cylinder(
                center=(x, y, -body.pin_length_mm / 2.0), direction=(0, 0, 1),
                radius=radius, height=body.pin_length_mm,
            )
            self.add_mesh(pin, color=_APPROX_PIN_COLOR, specular=0.4,
                          reset_camera=False, render=False)
            anchors.append((number, x, y, -body.pin_length_mm))

        self._add_pin_labels(anchors)
        self._finish()

    def render_mesh(self, mesh_path) -> None:
        """@brief Show the tessellated mesh written by FreeCAD, one colour per part.

        @param mesh_path Path of the preview JSON.
        @throws OSError when the file cannot be read.
        @throws ValueError when it holds no parts or no triangles; the scene on
            screen is then left untouched.
        @note Re-entering the 3D tab calls this every time. When the same
            unchanged file is already shown (see is_showing) nothing is rebuilt:
            scene and camera stay, only the pin labels follow the current
            setting. Otherwise the prepared meshes come from a small cache keyed
            by the file identity, so only a new or changed file is parsed.
        @note The scene is built with rendering suppressed and drawn once: a
            render per added part used to redraw the growing scene ~170 times
            for a real module.
        """
        key = _mesh_file_key(mesh_path)
        if key == self._shown_mesh_key:
            self._refresh_pin_labels()
            return
        scene = self._mesh_cache.get(key)
        if scene is None:
            scene = _prepare_mesh_scene(mesh_path)
            self._mesh_cache[key] = scene
            while len(self._mesh_cache) > _MESH_CACHE_SIZE:
                self._mesh_cache.popitem(last=False)
        else:
            self._mesh_cache.move_to_end(key)

        with self._rendering_suppressed():
            self._reset()
            for part in scene.parts:
                # ambient alto: i moduli reali sono quasi neri (#1a1a1a) e con la
                # sola luce diffusa, su sfondo scuro, diventavano una macchia
                # indistinguibile.
                actor = self.add_mesh(
                    part.mesh, color=part.color,
                    smooth_shading=True, specular=0.4, specular_power=20,
                    ambient=0.35, diffuse=0.75, reset_camera=False, render=False,
                )
                if part.row is not None:
                    self._component_actors[actor.GetAddressAsString('')] = part.row
            self._mesh_anchors = scene.anchors
            self._add_pin_labels(list(scene.anchors))
            self._finish()
        self._shown_mesh_key = key
        self.render()

    def _refresh_pin_labels(self) -> None:
        """Pin labels of the mesh on screen follow the current setting, no rebuild.

        Renders only when something changed: showing the tab repaints the
        widget anyway, and a full frame of a large module is not free.
        """
        changed = False
        if self.show_pin_numbers and self._label_actor is None and self._mesh_anchors:
            with self._rendering_suppressed():
                self._add_pin_labels(list(self._mesh_anchors))
            changed = True
        if self._label_actor is not None:
            visible = bool(self.show_pin_numbers and self._viewing_from_below())
            if bool(self._label_actor.GetVisibility()) != visible:
                self._label_actor.SetVisibility(visible)
                changed = True
        if changed:
            self.render()
