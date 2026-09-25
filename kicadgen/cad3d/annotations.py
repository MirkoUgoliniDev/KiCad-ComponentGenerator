"""@brief Post-processing annotations for the 3D body: text on a face and fillet on an edge.

Annotazioni di post-elaborazione per il corpo 3D (testo su faccia, fillet su spigolo).

Il corpo e' sempre un parallelepipedo assi-allineato, quindi facce e
spigoli hanno nomi fissi e prevedibili, selezionabili da una lista (non
serve un picker 3D a click sull'anteprima):

Facce (6): top, bottom, left, right, front, back
    - top/bottom: normale lungo Z (top = altezza massima, bottom = piano di saldatura Z=0)
    - left/right: normale lungo X (left = X minimo, right = X massimo)
    - front/back: normale lungo Y DEL MODELLO 3D (Y verso l'alto, come in
      KiCad): front = lato -Y, cioe' il lato in BASSO della vista dall'alto;
      back = lato +Y, in alto. E' la faccia che il cubo della vista 3D chiama
      «Fronte» (convenzione 2, dal 2026-09-25; prima front era il lato in alto
      della vista 2D e il cubo lo chiamava «Retro»).

Spigoli (12): combinazione di due facce adiacenti, es. "top-front",
"front-left" -- lo script FreeCAD (freecad_build_body.py) classifica gli
spigoli del solido a runtime in base a direzione e posizione, con la
stessa logica di FACE_NAMES/EDGE_NAMES qui sotto.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from ..services.messages import tx

FACE_NAMES = ("top", "bottom", "left", "right", "front", "back")

#: Convenzione dei nomi front/back scritta nei progetti. 1 (o assente): front
#: era il lato in alto della vista dall'alto; 2: front e' il lato in basso,
#: come il cubo 3D e KiCad.
ANNOTATION_CONVENTION = 2


def swap_front_back(name: str) -> str:
    """@brief Swap «front» and «back» in a face or edge name.

    @param name Face or edge name, e.g. "front-left".
    @return The same name with front/back exchanged, e.g. "back-left".
    @note Used once to bring annotations saved with convention 1 to the
        current one: the physical side stays the same, only its name changes.
    """
    swap = {"front": "back", "back": "front"}
    return "-".join(swap.get(part, part) for part in str(name).split("-"))

EDGE_NAMES = (
    "top-front", "top-back", "top-left", "top-right",
    "bottom-front", "bottom-back", "bottom-left", "bottom-right",
    "front-left", "front-right", "back-left", "back-right",
)


def _positive(value) -> bool:
    """True for a finite number > 0; NaN and infinity are rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def validate_hex_color(value: str, what: str) -> None:
    """@brief Raise ValueError unless `value` is a '#rrggbb' colour; `what` names it in the message.

    Valida un colore nel formato '#rrggbb'.
    """
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        raise ValueError(tx("{what} non valido: {value!r} (atteso formato '#rrggbb')",
                            what=what,
                            value=value))


@dataclass
class FaceTextAnnotation:
    """@brief Printed text layer laid on one face of the body, exported as its own coloured solid.

    Scritta serigrafata su una faccia del corpo.

    Non e' un'incisione: e' uno strato sottile di "vernice" appoggiato sopra la
    superficie (un solido a se', con un colore proprio), come la serigrafia
    stampata sui case reali. Resta un solido separato dal corpo proprio perche'
    cosi' puo' avere un colore diverso nell'export.
    """

    face: str
    text: str
    size_mm: float = 2.0
    thickness_mm: float = 0.06  # spessore dello strato di vernice
    color_hex: str = "#ffffff"

    def validate(self) -> None:
        """@brief Validate the object against its supported contract."""
        if self.face not in FACE_NAMES:
            raise ValueError(tx("Faccia non valida: {face!r} (ammesse: {FACE_NAMES})",
                                face=self.face,
                                FACE_NAMES=FACE_NAMES))
        if not self.text.strip():
            raise ValueError(tx("Il testo dell'annotazione non puo' essere vuoto."))
        if not _positive(self.size_mm):
            raise ValueError(tx("La dimensione del testo deve essere > 0."))
        if not _positive(self.thickness_mm):
            raise ValueError(tx("Lo spessore della scritta deve essere > 0."))
        validate_hex_color(self.color_hex, tx("Colore del testo"))


@dataclass
class EdgeFilletAnnotation:
    """@brief Fillet of a given radius (mm) on one named edge of the body.

    Raccordo (fillet) su uno spigolo del corpo.
    """

    edge: str
    radius_mm: float = 0.5

    def validate(self) -> None:
        """@brief Validate the object against its supported contract."""
        if self.edge not in EDGE_NAMES:
            raise ValueError(tx("Spigolo non valido: {edge!r} (ammessi: {EDGE_NAMES})",
                                edge=self.edge,
                                EDGE_NAMES=EDGE_NAMES))
        if not _positive(self.radius_mm):
            raise ValueError(tx("Il raggio del fillet deve essere > 0."))
