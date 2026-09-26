"""@brief Parameters of the parametric 3D model: rectangular body plus cylindrical THT pins.

Parametri del modello 3D parametrico (corpo rettangolare + pin cilindrici).

Il modello geometrico e' volutamente semplice: un parallelepipedo per il
corpo del componente (ingombro meccanico, es. per il controllo di
interferenza/keepout) piu' un cilindro per ogni pin THT, posizionati con
la stessa geometria del footprint 2D (kicadgen.generators.pin_header) cosi'
il modello 3D risulta sempre allineato ai pad generati.

Il piano di riferimento (Z=0) e' il piano di saldatura/PCB: i pin si
estendono da Z=-pin_length_mm a Z=0, il corpo da Z=0 a Z=height_mm.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from ..services.messages import tx


def _finite(value) -> bool:
    """True for a finite real number (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positive(value) -> bool:
    """True for a finite number > 0: `value <= 0` alone lets NaN through."""
    return _finite(value) and value > 0


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
    color_hex: str = "#1a1a1a"       # colore del case
    pin_color_hex: str = "#b0b0b0"   # colore dei pin (metallo)
    pcb_thickness_mm: float | None = None
    components: list | None = None
    populated_board: bool = False
    # Scritte lette sui componenti (campo `marking` di ComponentBox) stampate
    # sul solido. Spento per contratto: il percorso standard ricostruisce
    # ingombri meccanici generici, senza marcature -- una sigla letta dalla
    # foto ("330?", "123", il nome di un modulo radio) e' un'evidenza per
    # l'abbinamento ai modelli di libreria, non un dato geometrico da
    # incidere. Chi vuole davvero le scritte lo dichiara qui.
    show_markings_3d: bool = False
    # Cilindri dei pin sotto il piano di saldatura (utente, 2026-09-26: «se
    # scegli fori passanti non devi mettere i pin, oppure un'opzione»). Spento,
    # il solido non ha i pin ma i fori restano: e' il modulo come si compra,
    # senza strip saldati. Il footprint non cambia.
    show_pins_3d: bool = True

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
        if not all(_finite(v) for v in (self.offset_x_mm, self.offset_y_mm)):
            raise ValueError(tx("Lo scostamento del corpo (offset X/Y) deve essere un numero "
                                "finito."))
        if not _positive(self.pin_diameter_mm):
            raise ValueError(tx("Il diametro pin (3D) deve essere > 0."))
        if not _positive(self.pin_length_mm):
            raise ValueError(tx("La lunghezza pin (protrusione sotto il corpo) deve essere > 0."))
