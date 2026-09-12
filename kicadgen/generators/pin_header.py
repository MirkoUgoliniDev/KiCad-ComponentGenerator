"""Generatore parametrico: header/modulo THT a matrice RxC.

Usato sia da examples/generate_pin_header.py sia dalla GUI
(kicadgen.gui.main_window) per costruire footprint THT a matrice di pin
a partire da parametri (righe, colonne, passo, distanza tra righe,
diametro pad, drill).

`pitch` e `row_spacing` sono volutamente separati: nei connettori/moduli
in stile DIP (es. convertitori DC/DC come TRACO TEL8WI) il passo tra pin
adiacenti sulla stessa riga (tipicamente 2.54mm) e' molto piu' piccolo
della distanza tra le due file di pin (es. 10.16mm) -- usare lo stesso
valore per entrambi produce una griglia quadrata errata.

`populated_pins` gestisce i moduli con pinout parziale (non tutte le
posizioni della matrice RxC hanno un pin fisico): se None, tutte le
posizioni vengono generate (comportamento di un normale pin header).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional, Tuple

from ..core.footprint import Footprint, FpCircle, FpRect, Pad


@dataclass
class PinHeaderParams:
    name: str = "PinHeader"
    description: str = ""
    tags: str = "pin header THT"
    rows: int = 2
    cols: int = 4
    pitch: float = 2.54
    row_spacing: Optional[float] = None
    pad_diameter: float = 1.7
    drill: float = 1.0
    margin: float = 1.0
    populated_pins: Optional[set] = field(default=None)
    # Posizione esplicita dei pin: {numero_pin: (riga, colonna)}. Serve quando la
    # numerazione non e' quella colonna-per-colonna dei pin header, per esempio
    # nei package DIP-style che numerano a U. Se None si usa l'ordine automatico.
    pin_map: Optional[dict] = field(default=None)

    def effective_row_spacing(self) -> float:
        """Distanza tra le righe: se non impostata, uguale al pitch (griglia quadrata,
        comportamento di un semplice pin header a 2 file)."""
        return self.row_spacing if self.row_spacing is not None else self.pitch

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("Il nome del footprint non puo' essere vuoto.")
        if self.rows < 1 or self.cols < 1:
            raise ValueError("Righe e colonne devono essere >= 1.")
        if self.pitch <= 0:
            raise ValueError("Il passo (pitch) deve essere > 0.")
        if self.effective_row_spacing() <= 0:
            raise ValueError("La distanza tra righe (row spacing) deve essere > 0.")
        if self.drill <= 0:
            raise ValueError("Il drill deve essere > 0.")
        if self.pad_diameter <= self.drill:
            raise ValueError("Il diametro pad deve essere maggiore del drill.")
        if self.populated_pins is not None:
            max_pin = self.rows * self.cols
            for p in self.populated_pins:
                if p < 1 or p > max_pin:
                    raise ValueError(
                        f"Pin {p} in populated_pins fuori range (1..{max_pin})."
                    )
        if self.pin_map:
            seen = {}
            for pin, position in self.pin_map.items():
                row, col = position
                if not (0 <= row < self.rows) or not (0 <= col < self.cols):
                    raise ValueError(
                        f"Pin {pin}: posizione (riga {row}, colonna {col}) fuori dalla "
                        f"matrice {self.rows}x{self.cols}."
                    )
                if position in seen:
                    raise ValueError(
                        f"Pin {pin} e pin {seen[position]} occupano la stessa "
                        f"posizione (riga {row}, colonna {col})."
                    )
                seen[position] = pin


def pin_positions(params: PinHeaderParams) -> Iterator[Tuple[int, float, float]]:
    """Genera (numero_pin, x_mm, y_mm) per ogni pin effettivamente popolato.

    Stessa geometria usata da build_pin_header() per il footprint 2D e dal
    generatore 3D (kicadgen.cad3d) per posizionare i pin cilindrici -- unica
    fonte di verita' per righe/colonne/pitch/row_spacing/populated_pins.

    Con `pin_map` valorizzata le posizioni sono quelle indicate; altrimenti i
    numeri seguono l'ordine colonna per colonna (1,2 poi 3,4...) dei pin header.
    """
    row_spacing = params.effective_row_spacing()

    if params.pin_map:
        for pin in sorted(params.pin_map):
            row, col = params.pin_map[pin]
            if params.populated_pins is None or pin in params.populated_pins:
                yield pin, col * params.pitch, row * row_spacing
        return

    pin = 1
    for col in range(params.cols):
        for row in range(params.rows):
            if params.populated_pins is None or pin in params.populated_pins:
                yield pin, col * params.pitch, row * row_spacing
            pin += 1


def build_pin_header(params: PinHeaderParams) -> Footprint:
    """Costruisce il Footprint per un header/modulo THT RxC in base a `params`."""
    params.validate()
    row_spacing = params.effective_row_spacing()

    fp = Footprint(
        name=params.name,
        description=params.description or (
            f"Header/modulo THT {params.rows}x{params.cols}, passo {params.pitch}mm, "
            f"distanza righe {row_spacing}mm, generato con kicadgen_tool"
        ),
        tags=params.tags,
        attr="through_hole",
    )

    width = (params.cols - 1) * params.pitch
    height = (params.rows - 1) * row_spacing
    m = params.margin

    fp.add_graphic(FpRect(
        start=(-m, -m),
        end=(width + m, height + m),
        layer="F.SilkS",
    ))
    fp.add_graphic(FpCircle(
        center=(-m - 0.5, -m - 0.5),
        radius=0.25,
        layer="F.SilkS",
        fill=True,
    ))

    for pin, x, y in pin_positions(params):
        shape = "rect" if pin == 1 else "circle"
        fp.add_pad(Pad(
            number=str(pin),
            pad_type="thru_hole",
            shape=shape,
            at=(x, y),
            size=(params.pad_diameter, params.pad_diameter),
            drill=params.drill,
            layers=["*.Cu", "*.Mask"],
        ))

    return fp
