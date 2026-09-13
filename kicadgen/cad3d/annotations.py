"""Annotazioni di post-elaborazione per il corpo 3D (testo su faccia, fillet su spigolo).

Il corpo e' sempre un parallelepipedo assi-allineato, quindi facce e
spigoli hanno nomi fissi e prevedibili, selezionabili da una lista (non
serve un picker 3D a click sull'anteprima):

Facce (6): top, bottom, left, right, front, back
    - top/bottom: normale lungo Z (top = altezza massima, bottom = piano di saldatura Z=0)
    - left/right: normale lungo X (left = X minimo, right = X massimo)
    - front/back: normale lungo Y (front = Y minimo, back = Y massimo)

Spigoli (12): combinazione di due facce adiacenti, es. "top-front",
"front-left" -- lo script FreeCAD (freecad_build_body.py) classifica gli
spigoli del solido a runtime in base a direzione e posizione, con la
stessa logica di FACE_NAMES/EDGE_NAMES qui sotto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FACE_NAMES = ("top", "bottom", "left", "right", "front", "back")

EDGE_NAMES = (
    "top-front", "top-back", "top-left", "top-right",
    "bottom-front", "bottom-back", "bottom-left", "bottom-right",
    "front-left", "front-right", "back-left", "back-right",
)


def validate_hex_color(value: str, what: str) -> None:
    """Valida un colore nel formato '#rrggbb'."""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        raise ValueError(f"{what} non valido: {value!r} (atteso formato '#rrggbb')")


@dataclass
class FaceTextAnnotation:
    """Scritta serigrafata su una faccia del corpo.

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
            raise ValueError(f"Faccia non valida: {self.face!r} (ammesse: {FACE_NAMES})")
        if not self.text.strip():
            raise ValueError("Il testo dell'annotazione non puo' essere vuoto.")
        if self.size_mm <= 0:
            raise ValueError("La dimensione del testo deve essere > 0.")
        if self.thickness_mm <= 0:
            raise ValueError("Lo spessore della scritta deve essere > 0.")
        validate_hex_color(self.color_hex, "Colore del testo")


@dataclass
class EdgeFilletAnnotation:
    """Raccordo (fillet) su uno spigolo del corpo."""

    edge: str
    radius_mm: float = 0.5

    def validate(self) -> None:
        """@brief Validate the object against its supported contract."""
        if self.edge not in EDGE_NAMES:
            raise ValueError(f"Spigolo non valido: {self.edge!r} (ammessi: {EDGE_NAMES})")
        if self.radius_mm <= 0:
            raise ValueError("Il raggio del fillet deve essere > 0.")
