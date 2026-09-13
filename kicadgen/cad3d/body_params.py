"""Parametri del modello 3D parametrico (corpo rettangolare + pin cilindrici).

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

    def validate(self) -> None:
        """@brief Validate dimensions, colors and pin extrusion constraints."""
        from .annotations import validate_hex_color

        validate_hex_color(self.color_hex, "Colore del corpo")
        validate_hex_color(self.pin_color_hex, "Colore dei pin")
        if self.length_mm <= 0 or self.width_mm <= 0 or self.height_mm <= 0:
            raise ValueError(
                "Le dimensioni del corpo (lunghezza/larghezza/altezza) devono essere > 0."
            )
        if self.pin_diameter_mm <= 0:
            raise ValueError("Il diametro pin (3D) deve essere > 0.")
        if self.pin_length_mm <= 0:
            raise ValueError("La lunghezza pin (protrusione sotto il corpo) deve essere > 0.")
