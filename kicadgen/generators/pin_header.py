"""@brief Generate a parametric through-hole header or module on a pin grid.

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
from math import isfinite
from typing import Iterator, Optional, Tuple

from ..core.footprint import Footprint, FpCircle, FpRect, Pad
from ..services.messages import tx


@dataclass
class PinHeaderParams:
    """@brief Store parameters for a through-hole pin header, in millimetres.

    @param body_length Optional body extent along X; requires body_width.
    @param body_width Optional body extent along Y; requires body_length.
    @param body_offset_x Minimum body X relative to the pin-grid origin.
    @param body_offset_y Minimum body Y relative to the pin-grid origin.
    @note Offsets match Body3DParams, with positive Y downward in KiCad.
    Without body dimensions, offsets are ignored and the legacy silk remains.
    margin expands the courtyard beyond the union of body and populated pads;
    without a body, the legacy silk rectangle serves as the body envelope.
    """
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
    body_length: Optional[float] = None
    body_width: Optional[float] = None
    body_offset_x: float = 0.0
    body_offset_y: float = 0.0

    def effective_row_spacing(self) -> float:
        """@brief Return row spacing in mm, defaulting to the pin pitch."""
        return self.row_spacing if self.row_spacing is not None else self.pitch

    def validate(self) -> None:
        """@brief Validate finite geometry and a complete optional body.

        @throws ValueError If dimensions, offsets or pin mapping are invalid.
        """
        for name in ("pitch", "row_spacing", "pad_diameter", "drill", "margin",
                     "body_length", "body_width", "body_offset_x", "body_offset_y"):
            value = getattr(self, name)
            if value is None and name in ("row_spacing", "body_length", "body_width"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number in mm.")
        if (self.body_length is None) != (self.body_width is None):
            raise ValueError("body_length and body_width must be supplied together.")
        if self.body_length is not None and (self.body_length <= 0 or self.body_width <= 0):
            raise ValueError("body_length and body_width must be > 0.")
        if self.margin < 0:
            raise ValueError("margin must be >= 0.")
        if not self.name.strip():
            raise ValueError(tx("Il nome del footprint non puo' essere vuoto."))
        if self.rows < 1 or self.cols < 1:
            raise ValueError(tx("Righe e colonne devono essere >= 1."))
        if self.pitch <= 0:
            raise ValueError(tx("Il passo (pitch) deve essere > 0."))
        if self.effective_row_spacing() <= 0:
            raise ValueError(tx("La distanza tra righe (row spacing) deve essere > 0."))
        if self.drill <= 0:
            raise ValueError(tx("Il drill deve essere > 0."))
        if self.pad_diameter <= self.drill:
            raise ValueError(tx("Il diametro pad deve essere maggiore del drill."))
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
                        tx("Pin {pin}: posizione (riga {row}, colonna {col}) fuori dalla matrice "
                           "{rows}x{cols}.",
                           pin=pin,
                           row=row,
                           col=col,
                           rows=self.rows,
                           cols=self.cols)
                    )
                if position in seen:
                    raise ValueError(
                        tx("Pin {pin} e pin {seen} occupano la stessa posizione (riga {row}, "
                           "colonna {col}).",
                           pin=pin,
                           seen=seen[position],
                           row=row,
                           col=col)
                    )
                seen[position] = pin


def pin_positions(params: PinHeaderParams) -> Iterator[Tuple[int, float, float]]:
    """@brief Yield (pin number, x_mm, y_mm) for each populated pin.

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
    """@brief Build a THT footprint with body contours and a union courtyard.

    @param params Grid, pad and optional body dimensions in mm.
    @return Footprint with unchanged pin coordinates and numbering.
    @throws ValueError If params contain invalid geometry.
    """
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
    start, end = (-m, -m), (width + m, height + m)
    if params.body_length is not None:
        start = (params.body_offset_x, params.body_offset_y)
        end = (start[0] + params.body_length, start[1] + params.body_width)
        fp.add_graphic(FpRect(start=start, end=end, layer="F.Fab", width=0.1))

    fp.add_graphic(FpRect(
        start=start,
        end=end,
        layer="F.SilkS",
    ))
    fp.add_graphic(FpCircle(
        center=(start[0] - 0.5, start[1] - 0.5),
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

    # Include full copper extents, even when pads protrude beyond the body.
    # Only emitted pads contribute; sparse pin maps retain their exact layout.
    min_x, min_y = start
    max_x, max_y = end
    for pad in fp.pads:
        min_x = min(min_x, pad.at[0] - pad.size[0] / 2)
        min_y = min(min_y, pad.at[1] - pad.size[1] / 2)
        max_x = max(max_x, pad.at[0] + pad.size[0] / 2)
        max_y = max(max_y, pad.at[1] + pad.size[1] / 2)
    courtyard_start = (min_x - m, min_y - m)
    courtyard_end = (max_x + m, max_y + m)
    if not all(isfinite(v) for v in (*start, *end, *courtyard_start, *courtyard_end)):
        raise ValueError("Computed footprint bounds must be finite.")
    fp.add_graphic(FpRect(
        start=courtyard_start, end=courtyard_end, layer="F.CrtYd", width=0.05,
    ))
    return fp
