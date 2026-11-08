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

from ..core.footprint import (COURTYARD_WIDTH_MM, FAB_WIDTH_MM, SILK_OUTSIDE_FAB_MM, SILK_WIDTH_MM, Footprint, FpRect, Pad,
                              courtyard_corners, fab_reference_text, require_kicad_reach,
                              validate_pad_number)
from ..core.output_names import validate_output_name
from ..core.silk import (PIN_ONE_UNMARKED_NOTE, SILK_PAD_CLEARANCE_MM, keepout_box, marker_ink_box,
                         pin_one, pin_one_marker, silk_rectangle)
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

#: Courtyard clearance around body and pads, mm: the KLC F5.3 value for
#: CONNECTORS (0.5 mm), the one of KiCad's own pin header footprints. This
#: generator builds header footprints (connectors and modules mounted on
#: headers), so the clearance follows the generator, a structural attribute,
#: not the part's name; packages and castellated modules keep the general
#: 0.25 mm (`generators.package.COURTYARD_MARGIN_MM`). User decision
#: 2026-11-03 («adeguiamoci»). It used to be 1.0 mm and doubled as the offset
#: of the silk frame of a header without a body; the two are separate now
#: (see `SILK_WIDTH_MM` and `build_pin_header`).
COURTYARD_MARGIN_MM = 0.5

#: Note recorded (in the footprint description, fixed file language) when
#: the row spacing of a multi-row header was not declared and the pitch was
#: used in its place: an estimate, never a measure.
ROW_SPACING_ASSUMED_NOTE = "distanza fra le righe non dichiarata: assunta uguale al passo (stima)"


def pin_key(pin) -> str:
    """@brief The one identity of a header pin: its number as text.

    `pin_map`, `populated_pins` and `pad_overrides` reach this module from
    JSON (text keys), from the GUI (ints) and from tests (either): every
    comparison between them goes through this key, so `{"1": ...}` and
    `{1: ...}` name the same pin and `1` is pin 1 whatever its type (review
    2026-10-27, G4: an int comparison missed a str-keyed pin 1 and a set
    difference reported str populated pins as missing from an int map).

    @param pin A pin number, int or str.
    @return `str(pin)` with surrounding blanks removed.
    """
    return str(pin).strip()


def automatic_number(pin) -> Optional[int]:
    """@brief The automatic header number a pin label stands for, or None.

    @param pin A pin number, int or str.
    @return The int when `pin_key` is a plain decimal number without leading
        zeros ("1", "12"); None otherwise ("01", "A1", "1.0", a superscript).
    @note Labels are text, compared as text everywhere (`pin_key`): «01» is
        not pin 1, because `pin_positions` would look for «01» among the
        automatic labels «1», «2»... and silently emit nothing (review
        2026-11-03, N6). One rule for the check and for the layout.
    """
    key = pin_key(pin)
    if key.isascii() and key.isdigit() and str(int(key)) == key:
        return int(key)
    return None


def _pin_order(pin) -> tuple:
    """Sort key: numeric pins in numeric order, then text pins alphabetically."""
    key = pin_key(pin)
    return (0, int(key), "") if key.isascii() and key.isdigit() else (1, 0, key)


@dataclass
class PinHeaderParams:
    """@brief Store parameters for a through-hole pin header, in millimetres.

    @param body_length Optional body extent along X; requires body_width.
    @param body_width Optional body extent along Y; requires body_length.
    @param body_offset_x Minimum body X relative to the pin-grid origin.
    @param body_offset_y Minimum body Y relative to the pin-grid origin.
    @note Offsets match Body3DParams, with positive Y downward in KiCad.
    Without body dimensions, offsets are ignored, no F.Fab outline is drawn
    and the silk frames the emitted pads at the silk-to-pad clearance.
    margin (default COURTYARD_MARGIN_MM, 0.5 mm, KLC connectors) expands the courtyard
    beyond the union of body (or pad silk frame) and populated pads.
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
    #: Courtyard clearance, mm, around the union of body and pads.
    margin: float = COURTYARD_MARGIN_MM
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

        @return `row_spacing` when declared, otherwise `pitch`.
        @note With one row the fallback has no geometric effect. With more
            rows and no declared spacing the pitch is an ASSUMPTION (a square
            grid), which the project invariants allow only when it is
            reported: `row_spacing_assumed` tells it, `assumption_notes`
            gives the note, and `build_pin_header` writes it into the
            footprint description. A caller that needs the true positions
            (a physical check, not a drawing) must test
            `row_spacing_assumed` and not trust the result.
        """
        return self.row_spacing if self.row_spacing is not None else self.pitch

    def row_spacing_assumed(self) -> bool:
        """@brief Tell whether the row spacing is the pitch by assumption, not by evidence.

        @return True when there is more than one row and `row_spacing` is None.
        """
        return self.row_spacing is None and self.rows > 1

    def assumption_notes(self) -> list:
        """@brief The localized notes about values this header assumes instead of reading them.

        @return One note when `row_spacing_assumed`, otherwise [].
        """
        if not self.row_spacing_assumed():
            return []
        return [tx("Distanza fra le righe non dichiarata: assunta uguale al passo ({pitch} mm). "
                   "E' una stima, da verificare sul disegno quotato.", pitch=self.pitch)]

    def validate(self) -> None:
        """@brief Validate finite geometry and a complete optional body.

        @throws ValueError If dimensions, offsets or pin mapping are invalid,
            or two `pad_overrides`/`pin_map` keys name the same pin.
        @note Normalises in place, once: `pad_overrides` keys become their
            `pin_key` text (the lookups use it, so an int key was validated
            and then silently ignored) and `pin_map` positions become tuples
            (a JSON list position was unhashable).
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
        # The name becomes the stem of every generated file and of the
        # `.3dshapes` folder: the shared rule refuses a path such as
        # "..\evil" here too, so a script caller is protected exactly like
        # the window (2026-10-27 review, H4).
        validate_output_name(self.name)
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
            # A blank or a control character inside a number is refused here,
            # as `core.package_geometry` refuses it: it would pass this check
            # and fail only when the footprint is built, leaving the preview
            # blank (review 2026-11-03, N7).
            for pin in self.pin_map:
                validate_pad_number(pin_key(pin))
            # Two keys naming the same pin (1 and "1") would place one pin
            # twice: refused on the shared text identity.
            keys = [pin_key(pin) for pin in self.pin_map]
            if len(keys) != len(set(keys)):
                duplicate = next(key for key in keys if keys.count(key) > 1)
                raise ValueError(tx("Pin {pin} compare due volte nella mappa dei pin (come numero e "
                                    "come testo).", pin=duplicate))
            # A position read from JSON is a list: a tuple is hashable and
            # compares equal to the GUI's tuples.
            normalised = {}
            for pin, position in self.pin_map.items():
                if isinstance(position, (str, bytes)) or not hasattr(position, "__len__") \
                        or len(position) != 2:
                    raise ValueError(tx("Pin {pin}: posizione non valida nella mappa dei pin.", pin=pin))
                normalised[pin] = tuple(position)
            self.pin_map = normalised
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
            validate_pad_overrides(self.pad_overrides,
                                   {pin_key(pin) for pin, _, _ in pin_positions(self)}, self.drill)
            # Every lookup uses the text key: normalised once here, so an int
            # key is applied instead of validated and then ignored.
            self.pad_overrides = normalise_pad_overrides(self.pad_overrides)
        if self.populated_pins is not None:
            for pin in self.populated_pins:
                validate_pad_number(pin_key(pin))
            # Without a map the numbers are the automatic column-by-column ones,
            # so they lie in 1..rows*columns. With an explicit map the numbering is a
            # convention of the drawing (DIP numbered in a U with empty positions:
            # pin 16 on a 2x3 grid) and the only criterion is that the map gives the
            # pin a position.
            if not self.pin_map:
                max_pin = self.rows * self.cols
                for p in self.populated_pins:
                    # The automatic numbers are the labels "1".."rows*cols":
                    # "3" is pin 3, while "03" or "A1" are other labels that
                    # only a pin map can place (`automatic_number`).
                    index = automatic_number(p)
                    if index is None:
                        raise ValueError(
                            tx("Pin «{pin}» in populated_pins: senza mappa dei pin i numeri sono "
                               "1..{max_pin}, scritti senza zeri iniziali; per altre numerazioni "
                               "serve la mappa dei pin.", pin=pin_key(p), max_pin=max_pin)
                        )
                    if index < 1 or index > max_pin:
                        raise ValueError(
                            tx("Pin {pin} in populated_pins fuori range (1..{max_pin}).",
                               pin=p, max_pin=max_pin)
                        )
            # With an explicit map a populated pin without a position would be
            # dropped silently (pin_positions iterates only the map): the footprint
            # would lose a pin of the source pinout. Compared on the text key:
            # int and str spellings of one pin are the same pin.
            if self.pin_map:
                missing = sorted({pin_key(p) for p in self.populated_pins}
                                 - {pin_key(p) for p in self.pin_map}, key=_pin_order)
                if missing:
                    raise ValueError(
                        tx("I pin {pins} sono popolati ma non hanno una posizione nella mappa "
                           "dei pin.",
                           pins=", ".join(str(pin) for pin in missing))
                    )
        self._validate_kicad_reach()
        self._validate_pad_clearance()

    def _validate_kicad_reach(self) -> None:
        """Refuse a header whose courtyard would leave KiCad's coordinate range.

        The envelope is the one build_pin_header draws: the body with the
        silk frame just outside it (or, without a body, the silk frame around
        the pads), every emitted pad with its override, plus the courtyard
        margin (review 2026-10-29, G3)."""
        m = float(self.margin)
        if self.body_length is not None:
            reach = max(abs(self.body_offset_x), abs(self.body_offset_y),
                        abs(self.body_offset_x + self.body_length),
                        abs(self.body_offset_y + self.body_width)) + SILK_OUTSIDE_FAB_MM
            frame = 0.0
        else:
            reach = 0.0
            frame = SILK_PAD_CLEARANCE_MM + SILK_WIDTH_MM / 2
        overrides = self.pad_overrides or {}
        for pin, x, y in pin_positions(self):
            size = float(overrides.get(pin_key(pin), {}).get("size", self.pad_diameter))
            reach = max(reach, abs(x) + size / 2 + frame, abs(y) + size / 2 + frame)
        if not isfinite(reach + m):
            # Finite inputs whose sum overflows (a 1e308 body offset plus its
            # length): the bounds themselves are not numbers, which the
            # existing message says precisely; «inf mm» would not.
            raise ValueError(tx("I limiti calcolati del footprint non sono numeri finiti."))
        require_kicad_reach(reach + m)

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
        square = square_pad_pin((pad[0] for pad in pads), self)
        sizes = [float(overrides.get(pin_key(pin), {}).get("size", self.pad_diameter))
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
                # The pin-1 pad is square whatever the spelling of its
                # number (G4), chosen by the silk marker's rule.
                shape_a = overrides.get(pin_key(pin_a), {}).get("shape", default_pad_shape(self, pin_a, square))
                shape_b = overrides.get(pin_key(pin_b), {}).get("shape", default_pad_shape(self, pin_b, square))
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


def marked_pin(params: "PinHeaderParams") -> Optional[str]:
    """@brief The pin the pin-1 marks belong to, emitted or not; None when the numbering has no pin 1.

    @param params Header parameters (not validated here).
    @return "1" for the automatic numbering; with a pin map,
        `core.silk.pin_one` of ALL its keys (the map's "1", or the "A1" of
        a grid numbering), None otherwise.
    @note Taken from the numbering, not from the emitted pins: a DC/DC
        module whose pin 1 is not populated still has its pin 1 position,
        and the lowest emitted pin (2) must not be marked in its place
        (review 2026-11-03, N3).
    """
    if not params.pin_map:
        return "1"
    return pin_one(pin_key(pin) for pin in params.pin_map)


def marked_pin_position(params: "PinHeaderParams") -> Optional[Tuple[float, float]]:
    """@brief Grid position (x, y), mm, of `marked_pin`, populated or not; None without one.

    @param params Header parameters (not validated here).
    @return The map position of the marked pin, or its place in the
        automatic column-by-column order (pin n at column (n-1) // rows,
        row (n-1) % rows), in the frame of `pin_positions`.
    """
    marked = marked_pin(params)
    if marked is None:
        return None
    row_spacing = params.effective_row_spacing()
    if params.pin_map:
        row, col = next(position for pin, position in params.pin_map.items() if pin_key(pin) == marked)
    else:
        col, row = divmod(int(marked) - 1, int(params.rows))
    return (col * params.pitch, row * row_spacing)


def square_pad_pin(pins, params: Optional["PinHeaderParams"] = None) -> Optional[str]:
    """@brief The pin whose pad is square on a round-pad header: the pin-1 marker's pin.

    @param pins The emitted pin numbers (int or str).
    @param params The header whose numbering decides pin 1 (`marked_pin`),
        or None to judge the emitted pins alone.
    @return The marked pin when it is one of the emitted pins ("1", or "A1"
        of a grid numbering), else None: with pin 1 not populated NO pad is
        square, never the lowest emitted one (review 2026-11-03, N3/G4).
    @note Without `params` the emitted pins stand for the numbering; they
        give the same answer except for a numbering that is a letter grid
        only once its unpopulated labels are left out.
    """
    emitted = [pin_key(pin) for pin in pins]
    marked = pin_one(emitted) if params is None else marked_pin(params)
    return marked if marked is not None and marked in emitted else None


def default_pad_shape(params: "PinHeaderParams", pin, square: Optional[str]) -> str:
    """@brief Shape of a pad without an override: the header shape, or "rect" for the pin-1 pad.

    @param params Header parameters (`pad_shape`).
    @param pin Pin number, int or str.
    @param square `square_pad_pin` of the emitted pins.
    @return "rect" for a square header or for the pin-1 pad of a round one, else "circle".
    """
    if params.pad_shape == "rect" or (square is not None and pin_key(pin) == square):
        return "rect"
    return params.pad_shape


def normalise_pad_overrides(overrides) -> dict:
    """@brief Pad overrides keyed by `pin_key`, the key every lookup uses.

    @param overrides {pin number (int or str): override}, or None.
    @return A new dict with text keys; {} for None or an empty mapping.
    @throws ValueError When two keys name the same pin (1 and "1").
    """
    result = {}
    for number, override in (overrides or {}).items():
        key = pin_key(number)
        if key in result:
            raise ValueError(tx("Pin {pin} compare due volte negli override dei pad (come numero e "
                                "come testo).", pin=key))
        result[key] = override
    return result


def validate_pad_overrides(overrides, numbers, drill: float) -> None:
    """@brief Check per-pin host pad overrides: one rule for every header path.

    @param overrides {pin number: {"shape": "circle"|"rect", "size": mm}}.
    @param numbers The `pin_key` of every pin an override may name.
    @param drill Drill of the lands, mm: an override must be larger.
    @throws ValueError When `overrides` is not a mapping, an override names
        a pin not in `numbers` or is not a mapping, or its shape is not
        circle/rect or its size is not a finite number (bool excluded)
        larger than `drill`.
    @note Shared by PinHeaderParams.validate and
        host_mounting.header_footprint_params, whose package and castellated
        paths used to trust the overrides ({"size": None} escaped as a
        TypeError; review 2026-10-29, G7).
    """
    if not isinstance(overrides, dict):
        raise ValueError(tx("Override pad header non valido: {number}.", number=overrides))
    for number, override in overrides.items():
        if pin_key(number) not in numbers or not isinstance(override, dict):
            raise ValueError(tx("Override pad header non valido: {number}.", number=number))
        shape = override.get("shape")
        size = override.get("size")
        if shape not in {"circle", "rect"} or isinstance(size, bool) \
                or not isinstance(size, (int, float)) or not isfinite(size) \
                or size <= drill:
            raise ValueError(tx("Forma o dimensione non valida per il pad {number}.",
                                number=number))


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
    # Membership is decided on the text identity (`pin_key`): populated pins
    # and map keys may be ints or strings and still name the same pin. The
    # pin is yielded as the caller spelled it, so int-keyed data keeps ints.
    populated = (None if params.populated_pins is None
                 else {pin_key(p) for p in params.populated_pins})

    if params.pin_map:
        for pin in sorted(params.pin_map, key=_pin_order):
            row, col = params.pin_map[pin]
            if populated is None or pin_key(pin) in populated:
                yield pin, col * params.pitch, row * row_spacing
        return

    pin = 1
    for col in range(params.cols):
        for row in range(params.rows):
            if populated is None or pin_key(pin) in populated:
                yield pin, col * params.pitch, row * row_spacing
            pin += 1


def _pin_one_marker(start, end, marked_position, pads):
    """Silk dot outside the outline corner nearest pin 1's grid position, or None without a pin 1.

    The corner is chosen from where pin 1 is in the grid (a pin map can put
    it anywhere, e.g. a bottom-view DIP numbering), populated or not, so
    the dot never points at another pin. The dot sits 0.5 mm diagonally
    outside that corner and is pushed further out along the same diagonal
    when it would lie on copper; `pads` are the emitted Pad objects, so the
    copper avoided is each pad's real size.
    """
    if marked_position is None:
        return None
    px, py = marked_position
    corners = [(start[0], start[1]), (end[0], start[1]), (end[0], end[1]), (start[0], end[1])]
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
    description = params.description or (
        f"Header/modulo THT {params.rows}x{params.cols}, passo {params.pitch}mm, "
        f"distanza righe {row_spacing}mm, generato con kicadgen_tool"
    )
    if params.row_spacing_assumed():
        # Fixed file language, as the package generator's estimate note: the
        # assumption is recorded in the library itself, not only in a log.
        description += "; " + ROW_SPACING_ASSUMED_NOTE
    marked_position = marked_pin_position(params)
    if marked_position is None:
        # No label of the numbering is pin 1: no mark is guessed (G4).
        description += "; " + PIN_ONE_UNMARKED_NOTE

    fp = Footprint(
        name=params.name,
        description=description,
        tags=params.tags,
        datasheet=params.datasheet,
        attr="through_hole",
    )

    m = params.margin
    positions = list(pin_positions(params))
    square = square_pad_pin((pin for pin, _, _ in positions), params)
    pads = []
    for pin, x, y in positions:
        override = (params.pad_overrides or {}).get(pin_key(pin), {})
        # The square pad marks the pin-1 pad by its text identity and by the
        # silk marker's rule (`square_pad_pin`): an int-keyed and a str-keyed
        # pin map give the same footprint, an A1-numbered grid gets a square
        # A1, and an unpopulated pin 1 squares no other pad.
        shape = override.get("shape", default_pad_shape(params, pin, square))
        size = float(override.get("size", params.pad_diameter))
        pads.append(Pad(
            number=pin_key(pin),
            pad_type="thru_hole",
            shape=shape,
            at=(x, y),
            size=(size, size),
            drill=params.drill,
            layers=["*.Cu", "*.Mask"],
        ))
    if params.body_length is not None:
        start = (params.body_offset_x, params.body_offset_y)
        end = (start[0] + params.body_length, start[1] + params.body_width)
        fp.add_graphic(FpRect(start=start, end=end, layer="F.Fab", width=FAB_WIDTH_MM))
        # The silk frame lies just outside the assembly outline, never on it.
        g = SILK_OUTSIDE_FAB_MM
        silk_start, silk_end = (start[0] - g, start[1] - g), (end[0] + g, end[1] + g)
    else:
        # No declared body: no F.Fab outline is invented. The silk frames the
        # copper actually emitted, at the silk-to-pad clearance plus half the
        # stroke, so it is never cut by a pad; the envelope comes from the
        # pads, which are evidence, not from a guessed body. With no
        # populated pad at all the declared grid is framed instead, so the
        # (empty) footprint keeps finite outlines.
        grow = SILK_PAD_CLEARANCE_MM + SILK_WIDTH_MM / 2
        if pads:
            boxes = [(pad.at[0] - pad.size[0] / 2, pad.at[1] - pad.size[1] / 2,
                      pad.at[0] + pad.size[0] / 2, pad.at[1] + pad.size[1] / 2) for pad in pads]
        else:
            boxes = [(0.0, 0.0, (params.cols - 1) * params.pitch, (params.rows - 1) * row_spacing)]
        start = (min(b[0] for b in boxes) - grow, min(b[1] for b in boxes) - grow)
        end = (max(b[2] for b in boxes) + grow, max(b[3] for b in boxes) + grow)
        silk_start, silk_end = start, end
    if not all(isfinite(v) for v in (*start, *end)):
        raise ValueError(tx("I limiti calcolati del footprint non sono numeri finiti."))
    # Silk never covers copper: the keepouts come from the pads actually
    # emitted, so a per-pin size override is avoided as well as the default.
    keepouts = [keepout_box(pad.at[0] - pad.size[0] / 2, pad.at[1] - pad.size[1] / 2,
                            pad.at[0] + pad.size[0] / 2, pad.at[1] + pad.size[1] / 2,
                            line_width=SILK_WIDTH_MM) for pad in pads]
    for graphic in silk_rectangle(silk_start, silk_end, keepouts, width=SILK_WIDTH_MM):
        fp.add_graphic(graphic)
    marker = _pin_one_marker(silk_start, silk_end, marked_position, pads)
    if marker is not None:
        fp.add_graphic(marker)

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
    if marker is not None:
        # The dot lies outside the silk corner: the courtyard encloses its
        # ink as well (review 2026-11-03, N4).
        x0, y0, x1, y1 = marker_ink_box(marker)
        min_x, min_y, max_x, max_y = min(min_x, x0), min(min_y, y0), max(max_x, x1), max(max_y, y1)
    if not all(isfinite(v) for v in (min_x - m, min_y - m, max_x + m, max_y + m)):
        raise ValueError(tx("I limiti calcolati del footprint non sono numeri finiti."))
    # KLC F5.3: courtyard on the 0.01 mm grid, rounded outwards so the
    # clearance is never below the margin.
    courtyard_start, courtyard_end = courtyard_corners((min_x - m, min_y - m), (max_x + m, max_y + m))
    fp.add_graphic(FpRect(
        start=courtyard_start, end=courtyard_end, layer="F.CrtYd", width=COURTYARD_WIDTH_MM,
    ))
    # KLC F5.2 reference on F.Fab, centred on the body outline when one is
    # declared, otherwise on the pin field the silkscreen frames.
    fp.add_graphic(fab_reference_text(
        (round((start[0] + end[0]) / 2, 6), round((start[1] + end[1]) / 2, 6)),
        (end[0] - start[0], end[1] - start[1])))
    # Decision D1 (user, 2026-10-14): Reference above and Value below the
    # envelope; a user-chosen position is applied afterwards (place_texts).
    return fp.place_default_texts()
