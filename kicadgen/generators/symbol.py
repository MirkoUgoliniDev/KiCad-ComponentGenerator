"""@brief Electrical symbols: one shared millimetre layout for preview and KiCad export.

Pin IDs are footprint pad IDs, never renumbered. Electrical types must be reviewed
by the user; a GPIO name alone is not evidence of an electrical configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import tempfile
import unicodedata
from typing import Iterable

from kicadgen.core import kicad_text as _kicad_text
from kicadgen.core.kicad_text import (  # noqa: F401 (re-exported, see kicad_text_width)
    KICAD_ADVANCE_FALLBACK_UNITS as _KICAD_ADVANCE_FALLBACK_UNITS,
    KICAD_ADVANCE_GROUPS as _KICAD_ADVANCE_GROUPS,
    KICAD_ADVANCE_UNITS as _KICAD_ADVANCE_UNITS,
    KICAD_CENTRE_TO_BOTTOM as _KICAD_CENTRE_TO_BOTTOM,
    SCHEMATIC_DEFAULT_PEN_MM,
    kicad_corner_from_centre,
    kicad_text_margin,
)
from kicadgen.core.sexpr import format_number, quote
from kicadgen.fsutil import replace_file

ELECTRICAL_TYPES = (
    'input', 'output', 'bidirectional', 'tri_state', 'passive', 'free',
    'unspecified', 'power_in', 'power_out', 'open_collector', 'open_emitter',
    'no_connect',
)
TEXT_SIZE = 1.27
PIN_NAME_OFFSET = 0.508
PIN_GRID_MM = 2.54
#: Symbol library file format written by `serialize_symbol`: the version
#: KiCad 10 writes itself (see its bundled `Relay.kicad_sym`). Older KiCad
#: releases cannot read it; KiCad 8/9 would need 20231120 or 20241209.
SYMBOL_FORMAT_VERSION = 20251024
#: `generator_version` written beside `(generator "kicadgen")`. It is this
#: program's file revision, not a KiCad release. «10.0» is ambiguous: it
#: marked files with symbol text angles in degrees (`0389777`..`bc01ca2`)
#: and, in `af3a92a`, in KiCad's tenths; «10.0.1» marks tenths only. Readers
#: apply the value fallback only to the «10.0» header
#: (`symbol_glyphs.text_angle_from_file`), so this must never go back to «10.0».
SYMBOL_GENERATOR_VERSION = '10.0.1'
#: Characters a symbol name must not contain, besides control characters:
#: `:` separates the library nickname in «Library:Name»; KiCad 10 refuses to
#: load a library whose symbol name holds `"`, `\\`, `<` or `>` (verified
#: with `kicad-cli sym upgrade`, 2026-10-13). `/` is accepted by KiCad for
#: symbols and is therefore allowed. Shared with `klc_checks` so the export
#: and the check refuse the same names.
SYMBOL_NAME_ILLEGAL_CHARACTERS = ':"\\<>'


class LocalizedText:
    """@brief A message kept as an Italian catalogue template plus its values.

    @param template Italian template, looked up as `ui:<template>` in the
        translation catalogue (`kicadgen.services.i18n`).
    @param english English rendering used by `str()`: logs and tests read it.
    @param values Named placeholder values; a value may itself be a
        `LocalizedText` (a field label), rendered in the same language.
    """

    def __init__(self, template: str, english: str, **values) -> None:
        """@brief Keep the Italian template, its English rendering and the placeholder values.

        @param template Italian catalogue template (`ui:<template>` key).
        @param english English rendering with the same placeholders.
        @param values Named placeholder values, possibly `LocalizedText`.
        """
        self.template = template
        self.english = english
        self.values = values

    def __str__(self) -> str:
        """@brief The English text, for logs and exception messages."""
        return self.english.format(**{key: str(value) for key, value in self.values.items()})

    def localized(self, translate=None) -> str:
        """@brief The message in the interface language.

        @param translate Lookup `template -> unformatted text` of the GUI
            (e.g. `SymbolTab.tx`); None uses `services.messages.tx`, which
            falls back to the Italian template outside the GUI.
        @return The formatted message; never raises for a broken translation
            (the Italian template is formatted instead).
        """
        values = {key: value.localized(translate) if isinstance(value, LocalizedText) else value
                  for key, value in self.values.items()}
        if translate is None:
            from ..services.messages import tx
            return tx(self.template, **values)
        try:
            candidates = (str(translate(self.template)), self.template)
        except Exception:  # noqa: BLE001 - a broken catalogue must not hide the error
            candidates = (self.template,)
        for candidate in candidates:
            try:
                return candidate.format(**values)
            except (KeyError, IndexError, ValueError, AttributeError, TypeError):
                continue
        return self.template


class SymbolError(ValueError):
    """@brief A symbol validation error that the interface can show in its own language.

    @param text The message: Italian catalogue template, English rendering
        and values (`LocalizedText`). `str(exc)` is the English text, for logs.
    @note A ValueError subclass: every caller that already catches
        ValueError keeps working; a GUI calls `localized(self.tx)`.
    """

    def __init__(self, text: LocalizedText) -> None:
        """@brief Wrap the localized message; `str(self)` is its English text.

        @param text The message as a `LocalizedText`.
        """
        self.text = text
        super().__init__(str(text))

    def localized(self, translate=None) -> str:
        """@brief The message in the interface language, see `LocalizedText.localized`.

        @param translate Optional translation callable; None uses the
            catalogue of the current interface language.
        @return The localized message text.
        """
        return self.text.localized(translate)


def _message(template: str, english: str, /, **values) -> LocalizedText:
    """A translatable message: Italian catalogue template, English text, values.

    Every template given here needs a four-language entry `ui:<template>`
    in the catalogue (checked by tests/test_symbol_review_fixes.py).
    """
    return LocalizedText(template, english, **values)


#: Field labels are messages too, rendered inside the error that names them.
_label = _message


@dataclass(frozen=True)
class SymbolPin:
    """@brief Store a pin identity, electrical role and optional editor placement.

    @note `length` (mm, None = the layout's 2.54) must be a multiple of
        PIN_LENGTH_STEP_MM so the inner end of a grid-placed pin stays on a
        50 mil grid; `hidden` writes `(hide yes)`; `text_size` (mm, None =
        1.27) is the font size of the pin name and number.
    """
    number: str
    name: str
    electrical_type: str = 'passive'
    side: str = 'left'
    offset: float | None = None
    position: tuple[float, float] | None = None
    angle: int | None = None
    length: float | None = None
    hidden: bool = False
    text_size: float | None = None


#: Step of a custom pin length (50 mil) and its allowed range, mm.
PIN_LENGTH_STEP_MM = 1.27
PIN_LENGTH_MAX_MM = 12.7
#: Allowed font size of a pin text, mm (KLC: 1.27).
PIN_TEXT_SIZE_RANGE_MM = (0.5, 5.08)


def pin_length_of(pin: SymbolPin, default: float = PIN_GRID_MM) -> float:
    """@brief The length of a pin in mm: its own or the layout default.

    @param pin The symbol pin.
    @param default Length used when the pin declares none, mm.
    @return The pin length as a float, mm.
    """
    return float(pin.length) if pin.length is not None else float(default)


def pin_text_size_of(pin: SymbolPin) -> float:
    """@brief The font size of a pin's texts in mm: its own or TEXT_SIZE.

    @param pin The symbol pin.
    @return The text size as a float, mm.
    """
    return float(pin.text_size) if pin.text_size is not None else TEXT_SIZE


@dataclass(frozen=True)
class PositionedPin:
    """@brief Pair a symbol pin with its resolved terminal coordinates and angle."""
    pin: SymbolPin
    x: float
    y: float
    angle: int


@dataclass(frozen=True)
class SymbolMetadata:
    """@brief Library metadata of a symbol, the KiCad properties beside Reference/Value.

    @param reference Designator prefix (letters only: U, K, J, RV...), as the
        KiCad Library Conventions require for the Reference property.
    @param description One-line description (property "Description").
    @param keywords Space-separated keywords (property "ki_keywords").
    @param datasheet URL or path of the datasheet (property "Datasheet").
    @param fp_filters Space-separated footprint filter patterns, `*` and `?`
        wildcards (property "ki_fp_filters").
    @note Validation happens in `serialize_symbol`: every field is checked
        there so a GUI can build the record freely and get one error.
    """
    reference: str = 'U'
    description: str = ''
    keywords: str = ''
    datasheet: str = ''
    fp_filters: str = ''


@dataclass(frozen=True)
class SymbolLayout:
    """@brief Describe the shared body and pin geometry used for preview and export."""
    width: float
    height: float
    pin_length: float
    pin_spacing: float
    pins: tuple[PositionedPin, ...]


def _text(value: str, field: LocalizedText, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise SymbolError(_message('{field}: atteso un testo.', '{field}: expected text', field=field))
    if any(unicodedata.category(c) in ('Cc', 'Cs') for c in value):
        raise SymbolError(_message('{field}: caratteri di controllo non ammessi.',
                          '{field}: control characters are not allowed', field=field))
    value = value.strip()
    if required and not value:
        raise SymbolError(_message('{field}: non può essere vuoto.', '{field}: must not be empty', field=field))
    return value


def _reference_prefix(value: str) -> str:
    """Validate a designator prefix: KLC S6.1, letters only, no digits or spaces."""
    value = _text(value, _label('Designatore', 'Reference'), required=True)
    if not value.isalpha():
        raise SymbolError(_message('Designatore: solo lettere (per esempio U, K, J, RV).',
                          'Reference: use letters only (e.g. U, K, J, RV)'))
    return value


def _single_line(value: str, field: LocalizedText) -> str:
    """Validate a property KiCad shows on one line; `_text` already rejects control characters."""
    return _text(value, field)


def _datasheet(value: str) -> str:
    """Validate a datasheet reference: empty, or a URL/path on one line.

    Blanks are accepted: KiCad stores a path here and Windows paths carry
    them (review, 2026-10-10: refusing them left the symbol file stale
    while the footprint took the value); `_text` rejects control characters.
    """
    return _text(value, _label('Datasheet', 'Datasheet'))


#: The character KiCad substitutes for every space of a pin name when it
#: loads a symbol (`SCH_PIN::SetName`): a name written with spaces does not
#: survive the round trip, so the writer applies the same substitution.
PIN_NAME_SPACE_SUBSTITUTE = '_'


def normalize_pin_name(name: str) -> str:
    """@brief Apply to a pin name the rewriting KiCad applies when it loads the symbol.

    KiCad replaces each space of a pin name with an underscore on load, so
    a name saved as «A 5» is shown as «A_5». Writing the substituted name
    keeps the file equal to what KiCad will keep (review 2026-10-27, G2).

    @param name The pin name, already free of control characters.
    @return The name with every space replaced by `PIN_NAME_SPACE_SUBSTITUTE`.
    """
    return name.replace(' ', PIN_NAME_SPACE_SUBSTITUTE)


def normalize_pins(pins: Iterable[SymbolPin], notes: list | None = None) -> tuple[SymbolPin, ...]:
    """@brief Validate identities; preserve names/order and default unknown types passive.

    @param pins The pins to check, in their intended order.
    @param notes Optional list that receives a `LocalizedText` for every
        silent normalisation worth telling the user (a pin name whose spaces
        became underscores); None discards them.
    @return A tuple of copies with number, name, electrical type, length,
        hidden flag and text size normalized; order preserved.
    @throws SymbolError When an item is not a SymbolPin, a number is empty,
        duplicated or holds whitespace, a side is unknown, a length is not
        a multiple of 1.27 mm up to 12.7 mm, a text size is outside
        0.5-5.08 mm, or no pin is given.
    @note A pin NUMBER with inner whitespace is refused, not rewritten: the
        footprint pad keeps the number as given while KiCad rewrites the
        symbol's on load (space to underscore), so the two would no longer
        match and the mismatch would be silent. A pin NAME is only a label:
        it gets KiCad's own rewriting (`normalize_pin_name`) and a note.
    """
    result = []
    seen = set()
    for pin in pins:
        if not isinstance(pin, SymbolPin):
            raise SymbolError(_message('Pin non valido: atteso un SymbolPin.', 'Expected SymbolPin'))
        number = _text(pin.number, _label('Numero del pin', 'Pin number'), required=True)
        if any(ch.isspace() for ch in number):
            raise SymbolError(_message(
                'Numero di pin «{number}»: non può contenere spazi (KiCad lo riscriverebbe e non '
                'corrisponderebbe più al pad del footprint).',
                'Pin number «{number}»: must not contain spaces (KiCad would rewrite it and it would '
                'no longer match the footprint pad)', number=number))
        if number in seen:
            raise SymbolError(_message('Numero di pin duplicato: {number}.', 'Duplicate pin number: {number}',
                              number=number))
        seen.add(number)
        name = _text(pin.name, _label('Nome del pin {number}', 'Pin {number} name', number=number))
        normalized_name = normalize_pin_name(name)
        if normalized_name != name:
            if notes is not None:
                notes.append(_message(
                    'Pin {number}: nome «{name}» scritto come «{normalized}», come farebbe KiCad al '
                    'caricamento (gli spazi diventano «_»).',
                    'Pin {number}: name «{name}» written as «{normalized}», as KiCad does on load '
                    '(spaces become «_»)', number=number, name=name, normalized=normalized_name))
            name = normalized_name
        if pin.side not in ('left', 'right', 'top', 'bottom'):
            raise SymbolError(_message('Pin {number}: lato non valido.', 'Pin {number}: invalid side', number=number))
        electrical_type = pin.electrical_type if pin.electrical_type in ELECTRICAL_TYPES else 'passive'
        length = pin.length
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(length) \
                    or length < PIN_LENGTH_STEP_MM - 1e-7 or length > PIN_LENGTH_MAX_MM + 1e-7 \
                    or not math.isclose(length / PIN_LENGTH_STEP_MM, round(length / PIN_LENGTH_STEP_MM), abs_tol=1e-7):
                raise SymbolError(_message('Pin {number}: la lunghezza deve essere un multiplo di 1,27 mm fino a 12,7 mm.',
                                  'Pin {number}: length must be a multiple of 1.27 mm up to 12.7 mm',
                                  number=number))
            length = round(float(length), 4)
        text_size = pin.text_size
        if text_size is not None:
            if isinstance(text_size, bool) or not isinstance(text_size, (int, float)) or not math.isfinite(text_size) \
                    or not PIN_TEXT_SIZE_RANGE_MM[0] - 1e-7 <= text_size <= PIN_TEXT_SIZE_RANGE_MM[1] + 1e-7:
                raise SymbolError(_message('Pin {number}: la dimensione dei testi deve essere fra 0,5 e 5,08 mm.',
                                  'Pin {number}: text size must be between 0.5 and 5.08 mm', number=number))
            text_size = round(float(text_size), 4)
        result.append(replace(pin, number=number, name=name, electrical_type=electrical_type,
                              length=length, hidden=bool(pin.hidden), text_size=text_size))
    if not result:
        raise SymbolError(_message('Serve almeno un pin.', 'At least one pin is required'))
    return tuple(result)


def symbol_inputs(rows):
    """@brief Decode project metadata without discarding pin identities or electrical roles.

    @param rows Project pin rows (dicts with `pin`, `name`, `electrical_type`,
        the `symbol_*` layout fields and, optionally, `symbol_body`).
    @return (pins, body): the SymbolPin list in row order and the shared
        body size, None when no row declares one.
    @throws SymbolError When the rows declare different body sizes.
    """
    pins = [SymbolPin(str(row.get('pin', '')), str(row.get('name') or ''),
                      row.get('electrical_type', 'passive'),
                      row.get('symbol_side', 'left'), row.get('symbol_offset'),
                      row.get('symbol_position'), row.get('symbol_angle'),
                      row.get('symbol_pin_length'), bool(row.get('symbol_pin_hidden', False)),
                      row.get('symbol_pin_text_size')) for row in rows]
    bodies = [row['symbol_body'] for row in rows if 'symbol_body' in row]
    if bodies and any(body != bodies[0] for body in bodies):
        raise SymbolError(_message('Dimensioni del corpo del simbolo incoerenti fra i pin.',
                          'Inconsistent symbol body dimensions'))
    return pins, bodies[0] if bodies else None


#: Visible symbol properties whose position the user may choose, in the
#: order they are written. The others are hidden and sit at the origin.
PROPERTY_KEYS = ('Reference', 'Value')
#: Row key carrying the chosen property positions, mm, symbol coordinates
#: (y up), as {"Value": [x, y], ...}; identical on every row like `symbol_body`.
PROPERTIES_KEY = 'symbol_properties'


#: Grid the default property positions snap to, mm: KiCad's 50 mil text grid.
PROPERTY_GRID_MM = 1.27
# The height a horizontal pin's number reaches above its line is KiCad's
# own rule, `kicadgen.core.kicad_text.kicad_pin_number_extent` (measured
# with kicad-cli 10, 2026-10-16). It replaced the fixed estimate
# «0.508 mm + size», which KiCad's numbers exceed from size 1.27 upwards
# (1.921 mm against 1.778 mm at 1.27).
#: Upper bound of the advance of one stroke-font character, as a fraction of
#: the font size. KiCad's stroke font advances digits and capitals by less
#: than the font size, so 1.0 overestimates a pin number's length: the
#: estimate may lift the properties a grid step too far, never too little.
_CHAR_ADVANCE_BOUND = 1.0


def _drawn_name_length(name: str) -> int:
    """Characters KiCad draws for a pin name: none for an empty name or «~».

    `serialize_symbol` writes an empty name as `~`, which KiCad treats as
    «no name» and does not draw; anything else is drawn in full.
    """
    return 0 if name in ('', '~') else len(name)


def _pin_drawing_top(placed: 'PositionedPin', default_length: float,
                     half_width: float, half_height: float) -> float:
    """Highest y (mm, symbol coordinates, y up) reached by a pin's line, number and outside name.

    The pin line runs from the connection point (`placed.x`, `placed.y`) to
    the inner end, one pin length along the pin angle (0 = +x, 90 = +y,
    180 = -x, 270 = -y, as KiCad stores it). The texts are drawn as KiCad
    places them with the names inside (`pin_names (offset 0.508)`):
    - the number, on a vertical pin, is rotated and centred on the middle of
      the line, so it overshoots the outer end only when the text is longer
      than the pin (half the excess on each side); on a horizontal pin it
      sits above the line and reaches `kicad_pin_number_extent` (size) higher
      (KiCad's anchor, baseline, glyph top and half pen);
    - the name starts PIN_NAME_OFFSET beyond the inner end and runs on along
      the pin direction (rotated on a vertical pin, centred on the line on a
      horizontal one). It is decided by geometry, never by the `side` field:
      a pin rotated or dragged so that it points away from the body (e.g. at
      the top edge with angle 90) draws its name above the body.
    The text length is bounded by `_CHAR_ADVANCE_BOUND` x size per character.

    @param placed The pin with its resolved connection point and angle.
    @param default_length Pin length used when the pin has none, mm.
    @param half_width Half the body width, mm (the body is centred on 0, 0).
    @param half_height Half the body height, mm.
    @return The highest y the pin draws, mm.
    @note The name counts only when it STARTS outside the body rectangle
        (the point PIN_NAME_OFFSET beyond the inner end lies outside it).
        `layout_symbol` puts every inner end on the body edge with the pin
        pointing inwards, so each of its names starts inside the body and is
        ignored: those symbols keep their exact property positions and file.
        A name that starts inside but is longer than the body (e.g. a long
        name on a bottom pin of a low body) is a body-size problem, not a
        property one: it is deliberately left out, so that a symbol without
        any pin above the body never moves its properties.
    """
    length = pin_length_of(placed.pin, default_length)
    dx, dy = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[placed.angle]
    inner_x, inner_y = placed.x + dx * length, placed.y + dy * length
    top = max(placed.y, inner_y)
    size = pin_text_size_of(placed.pin)
    if dx == 0:
        # Vertical pin: the rotated number spans len x advance along y,
        # centred on the middle of the line.
        half_text = len(placed.pin.number) * size * _CHAR_ADVANCE_BOUND / 2
        top = max(top, (placed.y + inner_y) / 2 + half_text)
    else:
        # Horizontal pin: the number stands above the line where KiCad draws it.
        top = max(top, placed.y + _kicad_text.kicad_pin_number_extent(size))
    characters = _drawn_name_length(placed.pin.name)
    if characters:
        start_x, start_y = inner_x + dx * PIN_NAME_OFFSET, inner_y + dy * PIN_NAME_OFFSET
        # Closed rectangle with a small tolerance: a name starting exactly on
        # the edge would be drawn over the outline, which we treat as inside.
        inside = abs(start_x) <= half_width + 1e-7 and abs(start_y) <= half_height + 1e-7
        if not inside:
            if dx == 0:
                # Vertical pin: the rotated name runs along y from its start;
                # pointing down (dy = -1) its highest point is the start.
                top = max(top, start_y + max(dy, 0) * characters * size * _CHAR_ADVANCE_BOUND)
            else:
                # Horizontal pin: the name is centred on the line, half a
                # text height above it (below the number, kept for clarity).
                top = max(top, start_y + size / 2)
    return top


def default_property_positions(layout: 'SymbolLayout') -> dict:
    """@brief Where KiCad's visible properties go unless the user moved them.

    The base line is the body top edge (h = half body height), lifted to the
    highest point drawn by any pin (its line and its number, see
    `_pin_drawing_top`) when that point is above the body; the lifted value
    is rounded up to the 1.27 mm grid. Value then sits 2.54 mm and Reference
    5.08 mm above the base line, as before.

    @param layout The symbol geometry.
    @return {'Reference': (0, base + 5.08), 'Value': (0, base + 2.54)}, mm,
        on the x axis, both on the 1.27 mm grid.
    @note Rule (2026-10-15): a pin on the top side sticks out of the body by
        its own length, so the old fixed `h + 2.54` put the Value text on the
        tip of a top pin. With the base line at the highest drawn pin point,
        the Value text (1.27 mm tall, centred on its anchor) keeps at least
        2.54 - 1.27 / 2 = 1.905 mm of clearance from every pin line and
        number, whatever the pin length or text size.
    @note Backward compatibility: pins placed by `layout_symbol` on the
        left, right or bottom side never reach above the body with the
        default 1.27 mm text (a side pin is at least 2.54 mm below the top
        edge, its 1.27 mm number at most 1.921 mm above its line, see
        `kicad_pin_number_extent`), so those symbols keep exactly `h + 2.54`
        / `h + 5.08` and write the same file. A side pin in the top row with
        a number larger than 1.798 mm does reach above the body and lifts the
        properties too: same class of overlap (2026-10-16: the threshold was
        2.04 mm with the earlier fixed 0.508 mm gap estimate).
    @note The rule ignores the horizontal extent of the texts: any pin drawn
        above the body lifts the properties, even one far from x = 0. That
        errs on the side of clearance and needs no text-width estimate of
        the Value, which depends on the symbol name.
    @note Pin names count when they are drawn outside the body (a pin rotated
        or dragged to point away from it), see `_pin_drawing_top`.
    @note Hidden pins (`SymbolPin.hidden`, written `(hide yes)`) are left
        out: KiCad draws neither their line nor their texts in the
        schematic, so there is nothing for the properties to clear, and the
        file stays the one of a symbol without that pin.
    """
    h = layout.height / 2
    base = h
    drawn = [placed for placed in layout.pins if not placed.pin.hidden]
    if drawn:
        highest = max(_pin_drawing_top(placed, layout.pin_length, layout.width / 2, h) for placed in drawn)
        if highest > h + 1e-7:
            # Round up to the 50 mil grid so the properties stay where KiCad
            # can place texts; the small tolerance keeps a tip already on the
            # grid (body edge and pin lengths are grid multiples) unchanged.
            base = math.ceil(highest / PROPERTY_GRID_MM - 1e-7) * PROPERTY_GRID_MM
    return {'Reference': (0.0, round(base + 5.08, 4)), 'Value': (0.0, round(base + 2.54, 4))}


#: Value of the `PROPERTY_ANCHOR_FIELD` entry of `symbol_properties` once the
#: stored positions are the bottom-left corners of the texts (user decision,
#: 2026-10-16: Reference and Value anchored like the drawn texts of
#: 2026-10-14). A mapping without it holds the text CENTRES written before.
PROPERTY_ANCHOR_BOTTOM_LEFT = 'bottom_left'
#: Anchor convention of the positions stored before 2026-10-16: text centres.
PROPERTY_ANCHOR_CENTRE = 'centre'
#: Entry of the `symbol_properties` mapping that marks its anchor convention.
PROPERTY_ANCHOR_FIELD = 'anchor'

# KiCad's stroke-font geometry lives in `kicadgen.core.kicad_text`, shared
# with the footprint writer (2026-10-16); the names here keep this module's
# earlier API (`kicad_text_width`, `property_anchor_from_centre` and the
# measured `_KICAD_*` constants imported at the top) for its callers and tests.

#: Constant part of the width KiCad centres a symbol property on, mm, at the
#: schematic default pen `serialize_symbol` always writes (25/19 x 0.1524 =
#: 0.2005, see `kicadgen.core.kicad_text`).
_KICAD_TEXT_WIDTH_MARGIN_MM = kicad_text_margin(TEXT_SIZE, SCHEMATIC_DEFAULT_PEN_MM)


def kicad_text_width(text: str, size: float) -> float:
    """@brief Width KiCad centres a one-line symbol text on, mm (stroke font, default pen).

    @param text The text as KiCad shows it (for the Reference of a library
        symbol «U?», see `property_texts`).
    @param size Font size, mm.
    @return `kicadgen.core.kicad_text.kicad_text_width` at the schematic
        default pen: size x the sum of the advances / 21 + 0.2005 mm.
    """
    return _kicad_text.kicad_text_width(text, size, SCHEMATIC_DEFAULT_PEN_MM)


def property_anchor_from_centre(text: str, size: float, x: float, y: float, angle: int = 0) -> tuple:
    """@brief The bottom-left anchor at which KiCad draws a text where it drew it centred at (x, y).

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @param x Centre x, mm, symbol coordinates (y up).
    @param y Centre y, mm.
    @param angle Text angle, degrees: 0 (reads +x) or 90 (reads +y, glyph up
        towards -x, as KiCad turns a text); other values are read modulo 180.
    @return (x, y) of the anchor with `(justify left bottom)`, mm, rounded to 0.1 um.
    @note KiCad's own geometry, not the editor's
        (`kicadgen.core.kicad_text.kicad_corner_from_centre` at the
        schematic default pen, y up). In KiCad the converted text does not
        move; the editor draws every text at KiCad's width
        (`kicadgen.gui.kicad_text_item`, 2026-10-16), so on screen it stays
        put too, apart from the side bearings of the first and last glyph.
    """
    return kicad_corner_from_centre(text, size, x, y, thickness=SCHEMATIC_DEFAULT_PEN_MM,
                                    angle=angle, y_down=False)


def property_texts(reference: str, name: str) -> dict:
    """@brief The texts KiCad shows for the visible properties of an unannotated symbol.

    @param reference Designator prefix (e.g. «U»).
    @param name Symbol name (the Value).
    @return {'Reference': reference + '?', 'Value': name}: kicad-cli and the
        schematic show an unannotated Reference with «?», so that is the
        width the anchor conversion uses.
    @note Shown unescaped (`kicad_text.kicad_unescape`): KiCad draws a
        property's «{slash}» as «/» (measured with kicad-cli 10, 2026-10-17).
    """
    return {'Reference': _kicad_text.kicad_unescape(f'{reference}?'),
            'Value': _kicad_text.kicad_unescape(name)}


class PropertyPositions(dict):
    """@brief Chosen property positions {key: (x, y)} mm, with the anchor convention they use.

    @note `anchor` is PROPERTY_ANCHOR_BOTTOM_LEFT for positions stored since
        2026-10-16 (bottom-left corners) and PROPERTY_ANCHOR_CENTRE for those
        stored before (text centres, converted by `property_anchors`). A
        plain dict passed as `properties` is read as centres, the meaning it
        always had. Equality ignores the convention (plain dict comparison).
    """

    def __init__(self, mapping=(), anchor: str = PROPERTY_ANCHOR_BOTTOM_LEFT):
        """@brief Build the mapping.

        @param mapping Initial {key: (x, y)}.
        @param anchor PROPERTY_ANCHOR_BOTTOM_LEFT or PROPERTY_ANCHOR_CENTRE.
        """
        super().__init__(mapping)
        self.anchor = anchor


def property_anchors(layout: 'SymbolLayout', texts: dict, chosen=None, size: float = TEXT_SIZE) -> dict:
    """@brief Bottom-left anchors of Reference and Value: the chosen ones, else the converted defaults.

    @param layout The symbol geometry (for `default_property_positions`).
    @param texts {key: shown text}, see `property_texts`.
    @param chosen Positions carried by the rows (`symbol_property_positions`);
        a `PropertyPositions` states its convention, any other mapping holds
        centres; None or {} for none.
    @param size Font size of the properties, mm.
    @return {key: (x, y)} mm for every key of PROPERTY_KEYS, bottom-left anchors.
    @note The defaults stay CENTRES (`default_property_positions`, which
        clears top pins) and are converted here at every call, so an
        unmoved property keeps following the rule and its centred place in
        KiCad. Legacy chosen centres are converted the same way; nothing is
        converted twice because converted positions are only ever stored
        with the PROPERTY_ANCHOR_BOTTOM_LEFT mark.
    """
    centres = default_property_positions(layout)
    chosen = chosen or {}
    anchored = isinstance(chosen, PropertyPositions) and chosen.anchor == PROPERTY_ANCHOR_BOTTOM_LEFT
    result = {}
    for key in PROPERTY_KEYS:
        if key in chosen and anchored:
            result[key] = tuple(chosen[key])
            continue
        x, y = chosen[key] if key in chosen else centres[key]
        result[key] = property_anchor_from_centre(texts[key], size, x, y)
    return result


def _property_position(value, key: str) -> tuple:
    """Validate one [x, y] of `symbol_properties`: two finite numbers, mm."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return (round(float(value[0]), 4), round(float(value[1]), 4))
    raise SymbolError(_message('Proprietà {key}: la posizione richiede [x, y] in mm.',
                      '{properties}: {key} needs [x, y] in mm', properties=PROPERTIES_KEY, key=key))


def symbol_property_positions(rows) -> dict:
    """@brief The chosen positions of the visible properties carried by the rows.

    @param rows Symbol rows of the project (every row holds the same mapping,
        like `symbol_body`).
    @return A `PropertyPositions` {key: (x, y)} for the keys present, mm,
        symbol coordinates, whose `anchor` is PROPERTY_ANCHOR_BOTTOM_LEFT
        when the mapping carries that mark (`PROPERTY_ANCHOR_FIELD`) and
        PROPERTY_ANCHOR_CENTRE otherwise (projects saved before 2026-10-16);
        empty when no row carries `symbol_properties` or it holds no position.
    @throws ValueError When rows disagree, the value is not a mapping, a key
        is not one of `PROPERTY_KEYS`, the mark has another value or a
        position is not [x, y] of finite numbers.
    """
    carried = [row[PROPERTIES_KEY] for row in rows if isinstance(row, dict) and PROPERTIES_KEY in row]
    if not carried:
        return PropertyPositions()
    if any(entry != carried[0] for entry in carried):
        raise SymbolError(_message('Posizioni delle proprietà del simbolo incoerenti fra i pin.',
                          'Inconsistent symbol property positions'))
    mapping = carried[0]
    if mapping is None:
        return PropertyPositions()
    if not isinstance(mapping, dict):
        raise SymbolError(_message('Posizioni delle proprietà del simbolo non valide.',
                          '{properties}: expected a mapping', properties=PROPERTIES_KEY))
    anchor = mapping.get(PROPERTY_ANCHOR_FIELD, PROPERTY_ANCHOR_CENTRE)
    if anchor != PROPERTY_ANCHOR_BOTTOM_LEFT and PROPERTY_ANCHOR_FIELD in mapping:
        raise SymbolError(_message('Posizioni delle proprietà del simbolo non valide.',
                          '{properties}: unknown anchor {anchor}', properties=PROPERTIES_KEY,
                          anchor=repr(anchor)))
    result = PropertyPositions(anchor=anchor)
    for key, value in mapping.items():
        if key == PROPERTY_ANCHOR_FIELD:
            continue
        if key not in PROPERTY_KEYS:
            raise SymbolError(_message('Proprietà sconosciuta: {key}.', '{properties}: unknown property {key}',
                              properties=PROPERTIES_KEY, key=repr(key)))
        result[key] = _property_position(value, key)
    return result


def layout_symbol(pins: Iterable[SymbolPin], body=None) -> SymbolLayout:
    """@brief Shared geometry for editor and export; every terminal is on the 100 mil grid.

    Custom offsets are y coordinates for left/right and x for top/bottom.
    The body stays centred; dimensions are multiples of two grid steps.

    @param pins The pins to place (normalized here).
    @param body Optional explicit (width, height) of the body, mm; None
        sizes the body from the pin counts and name lengths.
    @return The SymbolLayout with every positioned pin and the body size.
    @throws SymbolError When the pins do not validate, the body is not a
        pair of finite grid-aligned sizes within 10.16-1000 mm, a custom
        position or offset is invalid or off the grid, or two terminals
        would coincide.
    """
    pins = normalize_pins(pins)
    spacing = PIN_GRID_MM
    sides = {side: [p for p in pins if p.side == side]
             for side in ('left', 'right', 'top', 'bottom')}
    lengths = [max((len(p.name) for p in sides[side]), default=0) for side in ('left', 'right')]
    half_width = max(5.08, math.ceil((sum(lengths) * TEXT_SIZE + 5.08) / 5.08) * spacing,
                     math.ceil((max(len(sides['top']), len(sides['bottom'])) + 1) / 2) * spacing)
    half_height = max(5.08, math.ceil((max(len(sides['left']), len(sides['right'])) + 1) / 2) * spacing)
    if body is not None:
        if not isinstance(body, (list, tuple)) or len(body) != 2:
            raise SymbolError(_message('Dimensioni del corpo del simbolo non valide.', 'Invalid symbol body dimensions'))
        try:
            width, height = map(float, body)
        except (TypeError, ValueError):
            raise SymbolError(_message('Dimensioni del corpo del simbolo non valide.',
                              'Invalid symbol body dimensions')) from None
        for value in (width, height):
            if not math.isfinite(value) or value < 4 * spacing or value > 1000:
                raise SymbolError(_message('Le dimensioni del corpo del simbolo devono essere fra 10,16 e 1000 mm.',
                                  'Symbol body dimensions must be between 10.16 and 1000 mm'))
            if not math.isclose(value / (2 * spacing), round(value / (2 * spacing)), abs_tol=1e-7):
                raise SymbolError(_message('Il corpo del simbolo deve essere allineato alla griglia.',
                                  'Symbol body must align to the grid'))
        half_width, half_height = width / 2, height / 2
    positioned, occupied = [], set()
    counters = dict.fromkeys(sides, 0)
    vertical_rows = max(len(sides['left']), len(sides['right']))
    horizontal_rows = max(len(sides['top']), len(sides['bottom']))
    for pin in pins:
        if pin.position is not None:
            if not isinstance(pin.position, (list, tuple)) or len(pin.position) != 2:
                raise SymbolError(_message('Posizione del pin non valida.', 'Invalid pin position'))
            try:
                x, y = map(float, pin.position)
            except (TypeError, ValueError):
                raise SymbolError(_message('Posizione del pin non valida.', 'Invalid pin position')) from None
            for value in (x, y):
                if not math.isfinite(value) or abs(value) > 1000 or not math.isclose(
                        value / spacing, round(value / spacing), abs_tol=1e-7):
                    raise SymbolError(_message('Il punto di connessione del pin deve stare sulla griglia da 2,54 mm.',
                                      'Pin connection must align to the grid'))
            angle = pin.angle
            if angle not in (0, 90, 180, 270):
                raise SymbolError(_message('Orientamento del pin non valido.', 'Invalid pin angle'))
            positioned.append(PositionedPin(pin, round(x, 4), round(y, 4), int(angle)))
            continue
        vertical = pin.side in ('left', 'right')
        count = vertical_rows if vertical else horizontal_rows
        index = counters[pin.side]
        counters[pin.side] += 1
        offset = ((count // 2) - index) * spacing
        if not vertical:
            offset = -offset
        if pin.offset is not None:
            try:
                offset = float(pin.offset)
            except (TypeError, ValueError):
                raise SymbolError(_message('Scostamento del pin non valido.', 'Invalid pin offset')) from None
        if not math.isfinite(offset) or not math.isclose(offset / spacing, round(offset / spacing), abs_tol=1e-7):
            raise SymbolError(_message('Il punto di connessione del pin deve stare sulla griglia da 2,54 mm.', 'Pin connection must align to the grid'))
        offset = round(offset, 4)
        if abs(offset) >= (half_height if vertical else half_width):
            raise SymbolError(_message('Il pin deve restare entro il lato del corpo.', 'Pin must remain inside its body edge'))
        if (pin.side, offset) in occupied:
            raise SymbolError(_message('Due pin non possono occupare la stessa posizione.', 'Two pins cannot occupy the same position'))
        occupied.add((pin.side, offset))
        # The outer end sits one pin length outside the body edge: with a
        # custom length it must still fall on the 100 mil grid (KLC S4.1),
        # which a length of 3.81 mm on a 2.54 mm edge would break.
        length = pin_length_of(pin, spacing)
        x, y, angle = {
            'left': (-half_width - length, offset, 0),
            'right': (half_width + length, offset, 180),
            'top': (offset, half_height + length, 270),
            'bottom': (offset, -half_height - length, 90),
        }[pin.side]
        for value in (x, y):
            if not math.isclose(value / spacing, round(value / spacing), abs_tol=1e-7):
                raise SymbolError(_message('Il punto di connessione del pin deve stare sulla griglia da 2,54 mm.',
                                  'Pin connection must align to the grid'))
        positioned.append(PositionedPin(pin, round(x, 4), round(y, 4), angle))
    points = [(p.x, p.y) for p in positioned]
    if len(points) != len(set(points)):
        raise SymbolError(_message('Due pin non possono occupare la stessa posizione.', 'Two pins cannot occupy the same position'))
    return SymbolLayout(half_width * 2, half_height * 2, spacing, spacing, tuple(positioned))


#: Quoting and number formatting are the shared ones of `core.sexpr`, so a
#: symbol file escapes the same characters and writes the same digits as a
#: footprint file (review 2026-10-27, G3/G5: the former private copies wrote
#: `-0` for a centred pin and escaped only `\` and `"`). The old names stay
#: for the callers written against them.
_quote = quote
_num = format_number


def serialize_symbol(name: str, pins: Iterable[SymbolPin], *,
                     footprint: str = '', reference: str = 'U', body=None, graphics=None,
                     metadata: SymbolMetadata | None = None, properties=None) -> str:
    """@brief Return a KiCad 10 symbol library (format 20251024) with one single-unit symbol.

    @param name Symbol name, also the Value property; no library separator.
    @param pins Pins with identities and electrical roles; see `layout_symbol`.
    @param footprint Footprint reference «Library:Name» (hidden property).
    @param reference Designator prefix, letters only; ignored when `metadata`
        is given (its `reference` wins).
    @param body Optional explicit body size in mm, see `layout_symbol`.
    @param graphics Placed glyph records (`symbol_glyphs.instance_from_glyph`)
        and wires drawn inside the body, after the body rectangle; None or []
        for none. Wire points anchored to pins or element terminals are
        exported at their resolved position (`symbol_glyphs.pin_inner_ends`).
    @param metadata Library metadata (description, keywords, datasheet,
        footprint filters) written as hidden properties; the empty keyword
        and filter properties are left out, as KiCad itself does.
    @param properties Chosen positions of the visible properties, {key: (x, y)}
        mm in symbol coordinates (`symbol_property_positions`); a key left
        out takes its `default_property_positions` place. A
        `PropertyPositions` states whether its points are bottom-left
        anchors or text centres; a plain mapping holds centres. None for none.
    @return The `.kicad_sym` text, newline-terminated.
    @note A Reference or Value the user placed is written with
        `(justify left bottom)` at its bottom-left anchor (`property_anchors`;
        chosen centres stored before 2026-10-16 are converted with KiCad's
        own text geometry). CONTRACT CHANGED 2026-10-17 (user, «Predefinito
        centrato»): an unmoved one is written centred, without justify, at
        its `default_property_positions` centre, so it stays centred whatever
        Value the schematic shows.
    @throws SymbolError (a ValueError) On an invalid name, reference,
        metadata or geometry; `localized()` gives the translated message.
        A name holding a character of SYMBOL_NAME_ILLEGAL_CHARACTERS or a
        control character is refused.
    @note Layout: one form per line with KiCad's 100 mil grid coordinates;
        KiCad accepts any whitespace, so the file reads back like one saved
        by the symbol editor. The body rectangle and the pin texts follow
        the KLC defaults (stroke 0.254 mm, background fill, 1.27 mm fonts).
    """
    name = _text(name, _label('Nome del simbolo', 'Symbol name'), required=True)
    illegal = ''.join(c for c in SYMBOL_NAME_ILLEGAL_CHARACTERS if c in name)
    if illegal:
        raise SymbolError(_message('Nome del simbolo: non può contenere {characters} (KiCad non caricherebbe la libreria).',
                          'Symbol name must not contain {characters} (the library separator (:) or a '
                          'character KiCad refuses to load).', characters=' '.join(illegal)))
    footprint = _text(footprint, _label('Footprint', 'Footprint'))
    if metadata is None:
        metadata = SymbolMetadata(reference=reference)
    elif not isinstance(metadata, SymbolMetadata):
        raise SymbolError(_message('Metadati del simbolo non validi.', 'metadata: expected SymbolMetadata'))
    reference = _reference_prefix(metadata.reference)
    description = _single_line(metadata.description, _label('Descrizione', 'Description'))
    keywords = _single_line(metadata.keywords, _label('Parole chiave', 'Keywords'))
    datasheet = _datasheet(metadata.datasheet)
    fp_filters = _single_line(metadata.fp_filters, _label('Filtri footprint', 'Footprint filters'))
    layout = layout_symbol(pins, body)
    w, h = layout.width / 2, layout.height / 2
    effects = '(effects (font (size 1.27 1.27)))'
    lines = [f'(kicad_symbol_lib (version {SYMBOL_FORMAT_VERSION}) (generator "kicadgen") '
             f'(generator_version {_quote(SYMBOL_GENERATOR_VERSION)})',
             f'  (symbol {_quote(name)}',
             '    (exclude_from_sim no) (in_bom yes) (on_board yes) (in_pos_files yes)',
             '    (duplicate_pin_numbers_are_jumpers no)',
             '    (pin_names (offset 0.508))']
    chosen = PropertyPositions(anchor=getattr(properties, 'anchor', PROPERTY_ANCHOR_CENTRE))
    for key, value in (properties or {}).items():
        if key not in PROPERTY_KEYS:
            raise SymbolError(_message('Proprietà sconosciuta: {key}.', 'properties: unknown property {key}', key=repr(key)))
        chosen[key] = _property_position(value, key)
    positions = property_anchors(layout, property_texts(reference, name), chosen)
    visible = [('Reference', reference), ('Value', name)]
    hidden = [('Footprint', footprint), ('Datasheet', datasheet), ('Description', description)]
    if keywords:
        hidden.append(('ki_keywords', keywords))
    if fp_filters:
        hidden.append(('ki_fp_filters', fp_filters))
    centres = default_property_positions(layout)
    # A property the user placed is anchored at its bottom-left corner, like
    # the drawn texts (user, 2026-10-16): KiCad starts the text exactly at
    # the point the editor shows. An unmoved one stays a centred KiCad text
    # at its default centre (user, 2026-10-17, «Predefinito centrato»): the
    # schematic shows the Value the user types, of any width, and a centred
    # text stays centred over the body. Hidden ones keep the plain effects.
    entries = [(key, value, positions[key] if key in chosen else centres[key], key in chosen, False)
               for key, value in visible]
    entries += [(key, value, (0.0, 0.0), False, True) for key, value in hidden]
    anchored_effects = '(effects (font (size 1.27 1.27)) (justify left bottom))'
    for key, value, (x, y), anchored, hide in entries:
        flags = '(show_name no) (do_not_autoplace no)' + (' (hide yes)' if hide else '')
        lines.append(f'    (property {_quote(key)} {_quote(value)} (at {_num(x)} {_num(y)} 0) {flags} '
                     f'{anchored_effects if anchored else effects})')
    lines.extend((f'    (symbol {_quote(name + "_0_1")}',
                  f'      (rectangle (start {_num(-w)} {_num(h)}) (end {_num(w)} {_num(-h)})',
                  '        (stroke (width 0.254) (type default)) (fill (type background)))'))
    if graphics:
        from .symbol_glyphs import pin_inner_ends, serialize_graphics, validate_instances
        lines.extend(serialize_graphics(validate_instances(graphics), pin_ends=pin_inner_ends(layout)))
    lines.extend(('    )', f'    (symbol {_quote(name + "_1_1")}'))
    for item in layout.pins:
        pin = item.pin
        size = _num(pin_text_size_of(pin))
        pin_effects = f'(effects (font (size {size} {size})))'
        hide = ' (hide yes)' if pin.hidden else ''
        lines.extend((f'      (pin {pin.electrical_type} line (at {_num(item.x)} {_num(item.y)} {item.angle}) '
                      f'(length {_num(pin_length_of(pin, layout.pin_length))}){hide}',
                      f'        (name {_quote(pin.name or "~")} {pin_effects})',
                      f'        (number {_quote(pin.number)} {pin_effects}))'))
    lines.extend(('    )', '    (embedded_fonts no)', '  )', ')', ''))
    return '\n'.join(lines)


def export_symbol(path: str | Path, name: str, pins: Iterable[SymbolPin], *,
                  footprint: str = '', reference: str = 'U', body=None, graphics=None,
                  metadata: SymbolMetadata | None = None, properties=None) -> Path:
    """@brief Validate before touching disk and atomically replace an explicitly chosen file.

    @param path Destination .kicad_sym path; parent folders are created.
    @param name Symbol name.
    @param pins The symbol pins, see `serialize_symbol`.
    @param footprint Footprint property value ("" when none).
    @param reference Reference designator prefix.
    @param body Optional explicit body size, see `layout_symbol`.
    @param graphics Placed glyph records, see `serialize_symbol`.
    @param metadata Library metadata, see `serialize_symbol`.
    @param properties Chosen positions of the visible properties, see `serialize_symbol`.
    @return The written path.
    @throws ValueError When the symbol does not validate; the file is untouched.
    @throws OSError When the temporary file cannot be written or moved in place.
    """
    text = serialize_symbol(name, pins, footprint=footprint, reference=reference, body=body,
                            graphics=graphics, metadata=metadata, properties=properties)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        replace_file(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
