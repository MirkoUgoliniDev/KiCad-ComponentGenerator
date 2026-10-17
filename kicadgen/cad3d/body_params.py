"""@brief Parameters of the parametric 3D model: rectangular body plus cylindrical THT pins.

The geometric model is deliberately simple: a box for the component body
(mechanical envelope, e.g. for interference/keepout checks) plus one
cylinder for each THT pin, placed with the same geometry as the 2D
footprint (kicadgen.generators.pin_header) so that the 3D model is always
aligned with the generated pads.

The reference plane (Z=0) is the soldering/PCB plane: the pins extend from
Z=-pin_length_mm to Z=0, the body from Z=0 to Z=height_mm.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..core.validation import is_finite_number, positive_number
from ..services.messages import tx


def _positive(value) -> bool:
    """True for a finite number > 0: `value <= 0` alone lets NaN through."""
    return positive_number(value) is not None


@dataclass
class Body3DParams:
    """@brief Define the dimensions and colors of a generated 3D body."""
    length_mm: float
    width_mm: float
    height_mm: float
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    pin_diameter_mm: float = 0.5
    pin_length_mm: float = 3.0
    color_hex: str = "#1a1a1a"       # case colour
    pin_color_hex: str = "#b0b0b0"   # pin colour (metal)
    pcb_thickness_mm: float | None = None
    components: list | None = None
    populated_board: bool = False
    # Markings read on the components (`marking` field of ComponentBox) printed
    # on the solid. Off by contract: the standard path rebuilds generic
    # mechanical envelopes, without markings -- a code read from the
    # photo ("330?", "123", the name of a radio module) is evidence for
    # matching against library models, not geometric data to
    # engrave. Whoever really wants the markings declares it here.
    show_markings_3d: bool = False
    # Pin cylinders below the soldering plane (user, 2026-09-26: «se
    # scegli fori passanti non devi mettere i pin, oppure un'opzione» (if you
    # choose through holes you must not add the pins, or an option)). Off:
    # the solid has no pins but the holes stay: it is the module as sold,
    # without soldered strips. The footprint does not change.
    show_pins_3d: bool = True
    # Pin numbers printed on the bottom face next to each lead
    # of a single THT component (user, 2026-10-10: «come nel
    # castellated» (like in the castellated one), where the labels sit next to
    # the contacts). Silkscreen, not geometry: off by contract, whoever wants it
    # turns it on.
    pin_numbers_3d: bool = False

    def validate(self) -> None:
        """@brief Validate dimensions, colors and pin extrusion constraints.

        @throws ValueError When a colour is malformed or a dimension, offset or
            pin value is not a finite number in range (NaN and infinity are
            rejected explicitly: `x <= 0` is False for NaN).
        """
        from .annotations import validate_hex_color

        validate_hex_color(self.color_hex, tx("Colore del corpo"))
        validate_hex_color(self.pin_color_hex, tx("Colore dei pin"))
        if self.pcb_thickness_mm is not None and (
                not math.isfinite(self.pcb_thickness_mm) or self.pcb_thickness_mm <= 0):
            raise ValueError(tx("Spessore PCB non valido."))
        if not all(_positive(v) for v in (self.length_mm, self.width_mm, self.height_mm)):
            raise ValueError(
                tx("Le dimensioni del corpo (lunghezza/larghezza/altezza) devono essere > "
                   "0.")
            )
        if not all(is_finite_number(v) for v in (self.offset_x_mm, self.offset_y_mm)):
            raise ValueError(tx("Lo scostamento del corpo (offset X/Y) deve essere un numero "
                                "finito."))
        if not _positive(self.pin_diameter_mm):
            raise ValueError(tx("Il diametro pin (3D) deve essere > 0."))
        if not _positive(self.pin_length_mm):
            raise ValueError(tx("La lunghezza pin (protrusione sotto il corpo) deve essere > 0."))
