"""Anteprima 3D con VTK/PyVista: renderer con z-buffer reale e interattivo.

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

os.environ.setdefault("QT_API", "pyqt6")

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
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
    """Vista 3D interattiva: mostra la mesh reale generata o l'approssimazione."""

    component_requested = pyqtSignal(int)
    #: Tasto destro su un componente: indice di riga e punto globale dove
    #: aprire il menu. La vista non sa cosa si possa fare su un componente --
    #: le voci le costruisce la finestra, che ha la tabella e i modelli.
    component_menu_requested = pyqtSignal(int, object)

    def __init__(self, parent=None):
        """@brief Initialize the viewport and optional component picking.

        @param parent Optional Qt parent widget.
        """
        self._component_actors = {}
        self._right_press = None
        self._menu_served = False
        super().__init__(parent)
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

    def mousePressEvent(self, event):  # noqa: N802 - firma Qt
        """@brief Ricorda dove inizia il tasto destro, per distinguere clic e zoom."""
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
        """@brief Clear actors and invalidate component picking for the old mesh."""
        self._component_actors.clear()
        super().clear()

    def set_pin_numbers(self, enabled: bool) -> None:
        """Attiva/disattiva le etichette dei pin (restano comunque limitate alla
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
        """Inquadra tutto il componente lasciando un margine."""
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
        """Vista standard: iso / top / bottom / front / back / left / right."""
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
        """@brief Provide the clear preview operation for this workflow."""
        self._reset()
        self.render()

    def render_params(self, pin_params: PinHeaderParams, body: Body3DParams) -> None:
        """Anteprima approssimata (prima della generazione): scatola + cilindri."""
        import pyvista as pv

        body.validate()
        self._reset()

        box = pv.Box(bounds=(
            body.offset_x_mm, body.offset_x_mm + body.length_mm,
            body.offset_y_mm, body.offset_y_mm + body.width_mm,
            0.0, body.height_mm,
        ))
        self.add_mesh(box, color=_APPROX_BODY_COLOR, opacity=0.65, specular=0.2)

        radius = body.pin_diameter_mm / 2.0
        anchors = []
        for number, x, y in pin_positions(pin_params):
            pin = pv.Cylinder(
                center=(x, y, -body.pin_length_mm / 2.0), direction=(0, 0, 1),
                radius=radius, height=body.pin_length_mm,
            )
            self.add_mesh(pin, color=_APPROX_PIN_COLOR, specular=0.4)
            anchors.append((number, x, y, -body.pin_length_mm))

        self._add_pin_labels(anchors)
        self._finish()

    def render_mesh(self, mesh_path) -> None:
        """Mesh tassellata scritta da FreeCAD: geometria reale, un colore per pezzo."""
        import pyvista as pv

        with open(mesh_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        parts = data.get("parts")
        if not parts:
            raise ValueError("mesh vuota")

        self._reset()
        drawn = 0
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
            # ambient alto: i moduli reali sono quasi neri (#1a1a1a) e con la
            # sola luce diffusa, su sfondo scuro, diventavano una macchia
            # indistinguibile.
            actor = self.add_mesh(
                mesh,
                color=part.get("color", _APPROX_BODY_COLOR),
                smooth_shading=True, specular=0.4, specular_power=20,
                ambient=0.35, diffuse=0.75,
            )
            index = _component_row(part)
            if index is not None:
                self._component_actors[actor.GetAddressAsString('')] = index
            drawn += 1

        if not drawn:
            raise ValueError("mesh senza triangoli")
        self._add_pin_labels(anchors)
        self._finish()
