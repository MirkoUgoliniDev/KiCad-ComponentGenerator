"""@brief Post-processing annotations for the 3D body: text on a face and fillet on an edge.

The body is always an axis-aligned box, so faces and edges have fixed,
predictable names that can be chosen from a list (no click-to-pick 3D
selector on the preview is needed):

Faces (6): top, bottom, left, right, front, back
    - top/bottom: normal along Z (top = maximum height, bottom = soldering plane Z=0)
    - left/right: normal along X (left = minimum X, right = maximum X)
    - front/back: normal along the Y axis OF THE 3D MODEL (Y pointing up, as
      in KiCad): front = the -Y side, i.e. the BOTTOM side of the top view;
      back = the +Y side, at the top. It is the face the 3D view cube calls
      «Fronte» (convention 2, since 2026-09-25; before that, front was the
      upper side of the 2D view and the cube called it «Retro»).

Edges (12): combination of two adjacent faces, e.g. "top-front",
"front-left" -- the FreeCAD script (freecad_build_body.py) classifies the
edges of the solid at run time by direction and position, with the same
logic as FACE_NAMES/EDGE_NAMES below.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from ..core.validation import positive_number
from ..services.messages import tx

FACE_NAMES = ("top", "bottom", "left", "right", "front", "back")

#: Front/back naming convention written into projects. 1 (or absent): front
#: was the upper side of the top view; 2: front is the lower side,
#: like the 3D cube and KiCad.
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
    return positive_number(value) is not None


def validate_hex_color(value: str, what: str) -> None:
    """@brief Raise ValueError unless `value` is a '#rrggbb' colour; `what` names it in the message.

    @param value Candidate colour string in '#rrggbb' format.
    @param what Human-readable name of the colour being validated, used in
        the error message.
    @throws ValueError When `value` is not a string or does not match '#rrggbb'.
    """
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        raise ValueError(tx("{what} non valido: {value!r} (atteso formato '#rrggbb')",
                            what=what,
                            value=value))


@dataclass
class FaceTextAnnotation:
    """@brief Printed text layer laid on one face of the body, exported as its own coloured solid.

    It is not an engraving: it is a thin layer of "paint" laid on top of the
    surface (a solid of its own, with its own colour), like the silkscreen
    printed on real cases. It stays a solid separate from the body precisely
    so that it can have a different colour in the export.
    """

    face: str
    text: str
    size_mm: float = 2.0
    thickness_mm: float = 0.06  # thickness of the paint layer
    color_hex: str = "#ffffff"
    #: Centre of the text on the face, in footprint mm (both or neither).
    #: None: the text sits at the centre of the face. Used for the pin
    #: numbers printed next to each lead on the bottom face (2026-10-10):
    #: a label is tied to its pin, not to the face.
    x_mm: float | None = None
    y_mm: float | None = None

    def validate(self) -> None:
        """@brief Validate the object against its supported contract.

        @throws ValueError When the face or text is unusable, a size is not
            positive, the colour is malformed, or only one of x_mm/y_mm is
            given or either is not a finite number.
        """
        anchors = (self.x_mm, self.y_mm)
        if any(a is not None for a in anchors):
            if any(a is None or isinstance(a, bool) or not isinstance(a, (int, float))
                   or not math.isfinite(float(a)) for a in anchors):
                raise ValueError(tx("Posizione della scritta non valida: servono x_mm e y_mm "
                                    "finiti, oppure nessuno dei due."))
            if self.face not in ("top", "bottom"):
                # The anchor is a point of the top or bottom face; on a side
                # face it would be silently ignored by the script (review B).
                raise ValueError(tx("Posizione della scritta ammessa solo sulle facce top e "
                                    "bottom, non su '{face}'.", face=self.face))
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

    @note The edge name must be one of EDGE_NAMES; the FreeCAD script
        resolves it to the actual edge of the solid at run time.
    """

    edge: str
    radius_mm: float = 0.5

    def validate(self) -> None:
        """@brief Validate the object against its supported contract.

        @throws ValueError When the edge name is not in EDGE_NAMES or the
            radius is not a positive finite number.
        """
        if self.edge not in EDGE_NAMES:
            raise ValueError(tx("Spigolo non valido: {edge!r} (ammessi: {EDGE_NAMES})",
                                edge=self.edge,
                                EDGE_NAMES=EDGE_NAMES))
        if not _positive(self.radius_mm):
            raise ValueError(tx("Il raggio del fillet deve essere > 0."))
