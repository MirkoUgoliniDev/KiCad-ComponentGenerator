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

os.environ.setdefault("QT_API", "pyqt6")

import numpy as np
from pyvistaqt import QtInteractor

from ..cad3d.body_params import Body3DParams
from ..generators.pin_header import PinHeaderParams, pin_positions

_BG_COLOR = "#1e1e2e"
_APPROX_BODY_COLOR = "#89b4fa"
_APPROX_PIN_COLOR = "#f9e2af"
_LABEL_COLOR = "#f9e2af"


class Preview3DVtk(QtInteractor):
    """Vista 3D interattiva: mostra la mesh reale generata o l'approssimazione."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_background(_BG_COLOR)
        self.show_pin_numbers = True
        self._label_actor = None
        # Le etichette si aggiornano anche quando l'utente ruota col mouse, non
        # solo premendo i pulsanti di vista.
        try:
            self.iren.add_observer("EndInteractionEvent", self._on_interaction_end)
        except Exception:  # noqa: BLE001 - senza interattore le etichette restano statiche
            pass

    def _on_interaction_end(self, *_args) -> None:
        self._update_label_visibility()

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
        """Etichetta ogni pin col suo numero, ancorata alla punta del piedino.

        `always_visible` tiene le etichette leggibili anche quando il pin e'
        nascosto dal corpo: guardando il componente dall'alto i piedini stanno
        sotto, ed e' proprio li' che serve sapere quale numero e' quale.
        """
        self._label_actor = None
        if not self.show_pin_numbers or not anchors:
            return
        import numpy as np

        points = np.array([[x, y, z] for _, x, y, z in anchors], dtype=float)
        labels = [str(pin) for pin, _, _, _ in anchors]
        self._label_actor = self.add_point_labels(
            points, labels, font_size=13, bold=True, text_color=_LABEL_COLOR,
            shape=None, show_points=False, always_visible=True,
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
            self.add_mesh(
                mesh,
                color=part.get("color", _APPROX_BODY_COLOR),
                smooth_shading=True, specular=0.5, specular_power=30,
            )
            drawn += 1

        if not drawn:
            raise ValueError("mesh senza triangoli")
        self._add_pin_labels(anchors)
        self._finish()
