"""@brief Generate a parametric through-hole header or module on a pin grid.

Used both by examples/generate_pin_header.py and by the GUI
(kicadgen.gui.main_window) to build THT pin-matrix footprints from
parameters (rows, columns, pitch, row spacing, pad diameter, drill).

`pitch` and `row_spacing` are deliberately separate: in DIP-style
connectors/modules (e.g. DC/DC converters such as the TRACO TEL8WI) the
pitch between adjacent pins on the same row (typically 2.54 mm) is much
smaller than the spacing between the two rows of pins (e.g. 10.16 mm) --
using the same value for both produces a wrong square grid.

`populated_pins` handles modules with a partial pinout (not every position
of the RxC matrix has a physical pin): when None, every position is
generated (the behaviour of an ordinary pin header).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor, hypot, isfinite
from typing import Iterator, Optional, Tuple

from ..core.footprint import Footprint, FpRect, Pad, fab_reference_text
from ..core.silk import keepout_box, pin_one, pin_one_marker, silk_rectangle
from ..services.messages import tx
from .castellated_module import MIN_HOLE_WALL_MM

#: Minimum free copper between two adjacent lands, in mm.
#:
#: It is the same minimum as the castellated generator (`MIN_HOLE_WALL_MM`, 0.2
#: mm), reused on purpose: it holds as a lower bound between two copper lands
#: too, because 0.2 mm is the default clearance of a KiCad project (Default
#: class) and sits above the minimum spacing of standard PCB manufacturers
#: (typically 0.1-0.15 mm). A footprint that does not respect it has lands
#: that touch or that the DRC flags at once. It is a LOWER BOUND of
#: manufacturability, not a recommended distance.
MIN_PAD_GAP_MM = MIN_HOLE_WALL_MM


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
    #: Datasheet URL or path for the footprint's "Datasheet" property; "" when unknown.
    datasheet: str = ""
    rows: int = 2
    cols: int = 4
    pitch: float = 2.54
    row_spacing: Optional[float] = None
    pad_diameter: float = 1.7
    drill: float = 1.0
    pad_shape: str = "circle"
    pad_overrides: Optional[dict] = field(default=None)
    margin: float = 1.0
    populated_pins: Optional[set] = field(default=None)
    # Explicit pin positions: {pin_number: (row, column)}. Needed when the
    # numbering is not the column-by-column one of pin headers, for example
    # in DIP-style packages numbered in a U. If None the automatic order is used.
    pin_map: Optional[dict] = field(default=None)
    body_length: Optional[float] = None
    body_width: Optional[float] = None
    body_offset_x: float = 0.0
    body_offset_y: float = 0.0

    def effective_row_spacing(self) -> float:
        """@brief Return row spacing in mm, defaulting to the pin pitch.

        @return `row_spacing` when declared, otherwise `pitch` (a single-row
            header or a square grid).
        """
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
                raise ValueError(tx("{name} deve essere un numero finito (mm).", name=name))
        if (self.body_length is None) != (self.body_width is None):
            raise ValueError(tx("Lunghezza e larghezza del corpo vanno indicate insieme."))
        if self.body_length is not None and (self.body_length <= 0 or self.body_width <= 0):
            raise ValueError(tx("Le dimensioni del corpo devono essere > 0."))
        if self.margin < 0:
            raise ValueError(tx("Il margine del courtyard deve essere >= 0."))
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
        if self.pad_shape not in {"circle", "rect"}:
            raise ValueError(tx("La forma dei pad header deve essere circle o rect."))
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
        if self.pad_overrides:
            positions = {str(pin) for pin, _, _ in pin_positions(self)}
            for number, override in self.pad_overrides.items():
                if str(number) not in positions or not isinstance(override, dict):
                    raise ValueError(tx("Override pad header non valido: {number}.", number=number))
                shape = override.get("shape")
                size = override.get("size")
                if shape not in {"circle", "rect"} or isinstance(size, bool) \
                        or not isinstance(size, (int, float)) or not isfinite(size) \
                        or size <= self.drill:
                    raise ValueError(tx("Forma o dimensione non valida per il pad {number}.",
                                        number=number))
        if self.populated_pins is not None:
            # Without a map the numbers are the automatic column-by-column ones,
            # so they lie in 1..rows*columns. With an explicit map the numbering is a
            # convention of the drawing (DIP numbered in a U with empty positions:
            # pin 16 on a 2x3 grid) and the only criterion is that the map gives the
            # pin a position.
            if not self.pin_map:
                max_pin = self.rows * self.cols
                for p in self.populated_pins:
                    if p < 1 or p > max_pin:
                        raise ValueError(
                            tx("Pin {pin} in populated_pins fuori range (1..{max_pin}).",
                               pin=p, max_pin=max_pin)
                        )
            # With an explicit map a populated pin without a position would be
            # dropped silently (pin_positions iterates only the map): the footprint
            # would lose a pin of the source pinout.
            if self.pin_map:
                missing = sorted(set(self.populated_pins) - set(self.pin_map))
                if missing:
                    raise ValueError(
                        tx("I pin {pins} sono popolati ma non hanno una posizione nella mappa "
                           "dei pin.",
                           pins=", ".join(str(pin) for pin in missing))
                    )
        self._validate_pad_clearance()

    def _validate_pad_clearance(self) -> None:
        """Refuse populated pads closer than MIN_PAD_GAP_MM of free copper.

        Grid-bucketed neighbour check, equivalent to a scan of every pair
        (same pairs judged, same first pair reported), see the comment below.

        The pads actually emitted are checked (pin_positions), so a sparse
        pin map with a small grid pitch is accepted when the populated pads
        are far apart, and a full grid is checked along the pitch, the row
        spacing and the diagonal alike. Square header pads use their corners
        when checking copper clearance.
        """
        # Only pads whose centres are close in BOTH axes can leave less than
        # the minimum gap: for a circle the copper gap is at least the centre
        # distance minus the two radii, for an axis-aligned square it is
        # max(|dx|, |dy|) minus the two half-sides, so a violating pair has
        # |dx| < ra + rb + gap and |dy| < ra + rb + gap, both at most
        # `window` (the two largest pads). Pads are bucketed on a grid of
        # that window and only pairs of adjacent cells are judged: O(n) on a
        # regular matrix instead of the O(n²) scan of every later pad (review
        # pass 4, 2026-10-15: a 30 x 30 matrix cost ~300 ms per validation).
        # The judged pairs are visited in the order of the former scan
        # (sorted by x, then original order), so the first pair reported is
        # the same; tests/test_pad_clearance_equivalence.py checks the
        # equivalence against that scan on random matrices.
        overrides = self.pad_overrides or {}
        pads = sorted(pin_positions(self), key=lambda item: item[1])
        if not pads:
            return
        sizes = [float(overrides.get(str(pin), {}).get("size", self.pad_diameter))
                 for pin, _x, _y in pads]
        window = max(sizes) + MIN_PAD_GAP_MM
        if not window > 0:
            return
        cells: dict = {}
        for index, (_pin, x, y) in enumerate(pads):
            cells.setdefault((floor(x / window), floor(y / window)), []).append(index)
        # Pad by pad in sorted order, its LATER neighbours of the adjacent
        # cells in sorted order, stopping at the first violation: no pair is
        # collected that a violation found earlier makes useless (review
        # pass 4: one pad far larger than the pitch puts every pad in one
        # cell, and collecting all n²/2 pairs first cost 333 ms where the
        # former scan stopped after 2 ms).
        for index_a, (pin_a, xa, ya) in enumerate(pads):
            cell_x, cell_y = floor(xa / window), floor(ya / window)
            later = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for index_b in cells.get((cell_x + dx, cell_y + dy), ()):
                        if index_b > index_a:
                            later.add(index_b)
            for index_b in sorted(later):
                pin_b, xb, yb = pads[index_b]
                radius_a, radius_b = sizes[index_a] / 2, sizes[index_b] / 2
                if xb - xa >= radius_a + radius_b + MIN_PAD_GAP_MM:
                    continue
                shape_a = overrides.get(str(pin_a), {}).get(
                    "shape", "rect" if self.pad_shape == "rect" or pin_a == 1 else "circle")
                shape_b = overrides.get(str(pin_b), {}).get(
                    "shape", "rect" if self.pad_shape == "rect" or pin_b == 1 else "circle")
                gap = pad_copper_gap(xa, ya, shape_a, radius_a,
                                     xb, yb, shape_b, radius_b)
                if gap < MIN_PAD_GAP_MM - 1e-9:
                    distance = hypot(xb - xa, yb - ya)
                    raise ValueError(tx(
                        "Pad {a} e {b}: piazzole di lato/dimensione {pad:.2f} mm con i centri a {distance:.2f} "
                        "mm lasciano {gap:.2f} mm di rame libero, meno del minimo di "
                        "{minimum:.2f} mm (si toccherebbero o quasi). Riduci la dimensione del "
                        "pad o controlla passo e distanza tra righe.",
                        a=pin_a, b=pin_b, pad=self.pad_diameter, distance=distance,
                        gap=max(gap, 0.0), minimum=MIN_PAD_GAP_MM))


def pad_copper_gap(xa, ya, shape_a, radius_a, xb, yb, shape_b, radius_b) -> float:
    """@brief Free copper between two axis-aligned round or square pads, mm.

    @param xa Centre X of the first pad, mm.
    @param ya Centre Y of the first pad, mm.
    @param shape_a "rect" for a square pad; any other value is a circle.
    @param radius_a Half the side (square) or the radius (circle), mm.
    @param xb Centre X of the second pad, mm.
    @param yb Centre Y of the second pad, mm.
    @param shape_b As shape_a, for the second pad.
    @param radius_b As radius_a, for the second pad.
    @return Euclidean distance between the two copper areas, mm; negative
        when they overlap. Square corners are counted, so diagonal squares
        are not mistaken for circles.
    @note Shared by the header clearance check and the host-mounting lands,
        so both apply the same geometry to MIN_PAD_GAP_MM.
    """
    square_a, square_b = shape_a == "rect", shape_b == "rect"
    if not square_a and not square_b:
        return hypot(xb - xa, yb - ya) - radius_a - radius_b
    if square_a and square_b:
        dx, dy = abs(xb - xa) - radius_a - radius_b, abs(yb - ya) - radius_a - radius_b
        return hypot(dx, dy) if dx > 0 and dy > 0 else max(dx, dy)
    # Distance from the circular centre to the nearest point on the square.
    if square_a:
        dx = max(0.0, abs(xb - xa) - radius_a)
        dy = max(0.0, abs(yb - ya) - radius_a)
        return hypot(dx, dy) - radius_b
    dx = max(0.0, abs(xb - xa) - radius_b)
    dy = max(0.0, abs(yb - ya) - radius_b)
    return hypot(dx, dy) - radius_a


def pin_positions(params: PinHeaderParams) -> Iterator[Tuple[int, float, float]]:
    """@brief Yield (pin number, x_mm, y_mm) for each populated pin.

    The same geometry used by build_pin_header() for the 2D footprint and by
    the 3D generator (kicadgen.cad3d) to place the cylindrical pins -- the
    single source of truth for rows/columns/pitch/row_spacing/populated_pins.

    With `pin_map` set the positions are the given ones; otherwise the
    numbers follow the column-by-column order (1,2 then 3,4...) of pin headers.

    @param params Header parameters (not validated here).
    @return An iterator of (pin number, x_mm, y_mm): X along the pitch, Y
        along the row spacing.
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


def _pin_one_marker(start, end, positions, pads):
    """Silk dot outside the outline corner nearest the pad numbered 1.

    The corner is chosen from where pin 1 actually is (a pin map can put it
    anywhere in the grid, e.g. a bottom-view DIP numbering), not assumed to be
    the top-left one; without pads the top-left corner is kept. The dot sits
    0.5 mm diagonally outside that corner and is pushed further out along the
    same diagonal when it would lie on copper; `pads` are the emitted Pad
    objects, so the copper avoided is each pad's real size.
    """
    number = pin_one(pin for pin, _, _ in positions)
    corners = [(start[0], start[1]), (end[0], start[1]), (end[0], end[1]), (start[0], end[1])]
    corner = corners[0]
    if number is not None:
        _, px, py = next(item for item in positions if str(item[0]) == number)
        corner = min(corners, key=lambda c: (c[0] - px) ** 2 + (c[1] - py) ** 2)
    sx = -1.0 if corner[0] == start[0] else 1.0
    sy = -1.0 if corner[1] == start[1] else 1.0
    keepouts = [keepout_box(pad.at[0] - pad.size[0] / 2, pad.at[1] - pad.size[1] / 2,
                            pad.at[0] + pad.size[0] / 2, pad.at[1] + pad.size[1] / 2,
                            line_width=0.0) for pad in pads]
    return pin_one_marker((corner[0] + 0.5 * sx, corner[1] + 0.5 * sy), keepouts, (sx, sy))


def build_pin_header(params: PinHeaderParams) -> Footprint:
    """@brief Build a THT footprint with body contours and a union courtyard.

    @param params Grid, pad and optional body dimensions in mm.
    @return Footprint with unchanged pin coordinates and numbering and a
        ``${REFERENCE}`` text on F.Fab (see core.footprint.fab_reference_text);
        Reference/Value anchored bottom-left at the KiCad corner of
        core.footprint.default_text_positions (place_default_texts).
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
        datasheet=params.datasheet,
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
    if not all(isfinite(v) for v in (*start, *end)):
        raise ValueError(tx("I limiti calcolati del footprint non sono numeri finiti."))

    positions = list(pin_positions(params))
    pads = []
    for pin, x, y in positions:
        override = (params.pad_overrides or {}).get(str(pin), {})
        shape = override.get("shape", "rect" if pin == 1 and params.pad_shape == "circle"
                             else params.pad_shape)
        size = float(override.get("size", params.pad_diameter))
        pads.append(Pad(
            number=str(pin),
            pad_type="thru_hole",
            shape=shape,
            at=(x, y),
            size=(size, size),
            drill=params.drill,
            layers=["*.Cu", "*.Mask"],
        ))
    # Silk never covers copper: the keepouts come from the pads actually
    # emitted, so a per-pin size override is avoided as well as the default.
    keepouts = [keepout_box(pad.at[0] - pad.size[0] / 2, pad.at[1] - pad.size[1] / 2,
                            pad.at[0] + pad.size[0] / 2, pad.at[1] + pad.size[1] / 2,
                            line_width=0.12) for pad in pads]
    for graphic in silk_rectangle(start, end, keepouts):
        fp.add_graphic(graphic)
    fp.add_graphic(_pin_one_marker(start, end, positions, pads))

    for pad in pads:
        fp.add_pad(pad)

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
        raise ValueError(tx("I limiti calcolati del footprint non sono numeri finiti."))
    fp.add_graphic(FpRect(
        start=courtyard_start, end=courtyard_end, layer="F.CrtYd", width=0.05,
    ))
    # KLC F5.2 reference on F.Fab, centred on the body outline when one is
    # declared, otherwise on the pin field the silkscreen frames.
    fp.add_graphic(fab_reference_text(
        (round((start[0] + end[0]) / 2, 6), round((start[1] + end[1]) / 2, 6)),
        (end[0] - start[0], end[1] - start[1])))
    # Decision D1 (user, 2026-10-14): Reference above and Value below the
    # envelope; a user-chosen position is applied afterwards (place_texts).
    return fp.place_default_texts()
