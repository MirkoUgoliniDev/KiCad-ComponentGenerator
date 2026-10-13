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
SYMBOL_GENERATOR_VERSION = '10.0'


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
    """@brief The length of a pin in mm: its own or the layout default."""
    return float(pin.length) if pin.length is not None else float(default)


def pin_text_size_of(pin: SymbolPin) -> float:
    """@brief The font size of a pin's texts in mm: its own or TEXT_SIZE."""
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


def _text(value: str, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field}: expected text')
    if any(unicodedata.category(c) in ('Cc', 'Cs') for c in value):
        raise ValueError(f'{field}: control characters are not allowed')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{field}: must not be empty')
    return value


def _reference_prefix(value: str) -> str:
    """Validate a designator prefix: KLC S6.1, letters only, no digits or spaces."""
    value = _text(value, 'Reference', required=True)
    if not value.isalpha():
        raise ValueError('Reference: use letters only (e.g. U, K, J, RV)')
    return value


def _single_line(value: str, field: str) -> str:
    """Validate a property KiCad shows on one line; `_text` already rejects control characters."""
    return _text(value, field)


def _datasheet(value: str) -> str:
    """Validate a datasheet reference: empty, or a URL/path on one line.

    Blanks are accepted: KiCad stores a path here and Windows paths carry
    them (review, 2026-10-10: refusing them left the symbol file stale
    while the footprint took the value); `_text` rejects control characters.
    """
    return _text(value, 'Datasheet')


def normalize_pins(pins: Iterable[SymbolPin]) -> tuple[SymbolPin, ...]:
    """@brief Validate identities; preserve names/order and default unknown types passive."""
    result = []
    seen = set()
    for pin in pins:
        if not isinstance(pin, SymbolPin):
            raise ValueError('Expected SymbolPin')
        number = _text(pin.number, 'Pin number', required=True)
        if number in seen:
            raise ValueError(f'Duplicate pin number: {number}')
        seen.add(number)
        name = _text(pin.name, f'Pin {number} name')
        if pin.side not in ('left', 'right', 'top', 'bottom'):
            raise ValueError(f'Pin {number}: invalid side')
        electrical_type = pin.electrical_type if pin.electrical_type in ELECTRICAL_TYPES else 'passive'
        length = pin.length
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(length) \
                    or length < PIN_LENGTH_STEP_MM - 1e-7 or length > PIN_LENGTH_MAX_MM + 1e-7 \
                    or not math.isclose(length / PIN_LENGTH_STEP_MM, round(length / PIN_LENGTH_STEP_MM), abs_tol=1e-7):
                raise ValueError(f'Pin {number}: length must be a multiple of 1.27 mm up to 12.7 mm')
            length = round(float(length), 4)
        text_size = pin.text_size
        if text_size is not None:
            if isinstance(text_size, bool) or not isinstance(text_size, (int, float)) or not math.isfinite(text_size) \
                    or not PIN_TEXT_SIZE_RANGE_MM[0] - 1e-7 <= text_size <= PIN_TEXT_SIZE_RANGE_MM[1] + 1e-7:
                raise ValueError(f'Pin {number}: text size must be between 0.5 and 5.08 mm')
            text_size = round(float(text_size), 4)
        result.append(replace(pin, number=number, name=name, electrical_type=electrical_type,
                              length=length, hidden=bool(pin.hidden), text_size=text_size))
    if not result:
        raise ValueError('At least one pin is required')
    return tuple(result)


def symbol_inputs(rows):
    """@brief Decode project metadata without discarding pin identities or electrical roles."""
    pins = [SymbolPin(str(row.get('pin', '')), str(row.get('name') or ''),
                      row.get('electrical_type', 'passive'),
                      row.get('symbol_side', 'left'), row.get('symbol_offset'),
                      row.get('symbol_position'), row.get('symbol_angle'),
                      row.get('symbol_pin_length'), bool(row.get('symbol_pin_hidden', False)),
                      row.get('symbol_pin_text_size')) for row in rows]
    bodies = [row['symbol_body'] for row in rows if 'symbol_body' in row]
    if bodies and any(body != bodies[0] for body in bodies):
        raise ValueError('Inconsistent symbol body dimensions')
    return pins, bodies[0] if bodies else None


#: Visible symbol properties whose position the user may choose, in the
#: order they are written. The others are hidden and sit at the origin.
PROPERTY_KEYS = ('Reference', 'Value')
#: Row key carrying the chosen property positions, mm, symbol coordinates
#: (y up), as {"Value": [x, y], ...}; identical on every row like `symbol_body`.
PROPERTIES_KEY = 'symbol_properties'


def default_property_positions(layout: 'SymbolLayout') -> dict:
    """@brief Where KiCad's visible properties go unless the user moved them.

    @param layout The symbol geometry.
    @return {'Reference': (0, h + 5.08), 'Value': (0, h + 2.54)}, mm,
        above the body on the x axis: the positions every symbol had before
        they became movable, so files without a choice do not change.
    """
    h = layout.height / 2
    return {'Reference': (0.0, round(h + 5.08, 4)), 'Value': (0.0, round(h + 2.54, 4))}


def _property_position(value, key: str) -> tuple:
    """Validate one [x, y] of `symbol_properties`: two finite numbers, mm."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return (round(float(value[0]), 4), round(float(value[1]), 4))
    raise ValueError(f'{PROPERTIES_KEY}: {key} needs [x, y] in mm')


def symbol_property_positions(rows) -> dict:
    """@brief The chosen positions of the visible properties carried by the rows.

    @param rows Symbol rows of the project (every row holds the same mapping,
        like `symbol_body`).
    @return {key: (x, y)} for the keys present, mm, symbol coordinates; {}
        when no row carries `symbol_properties` or it is empty.
    @throws ValueError When rows disagree, the value is not a mapping, a key
        is not one of `PROPERTY_KEYS` or a position is not [x, y] of finite
        numbers.
    """
    carried = [row[PROPERTIES_KEY] for row in rows if isinstance(row, dict) and PROPERTIES_KEY in row]
    if not carried:
        return {}
    if any(entry != carried[0] for entry in carried):
        raise ValueError('Inconsistent symbol property positions')
    mapping = carried[0]
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise ValueError(f'{PROPERTIES_KEY}: expected a mapping')
    result = {}
    for key, value in mapping.items():
        if key not in PROPERTY_KEYS:
            raise ValueError(f'{PROPERTIES_KEY}: unknown property {key!r}')
        result[key] = _property_position(value, key)
    return result


def layout_symbol(pins: Iterable[SymbolPin], body=None) -> SymbolLayout:
    """@brief Shared geometry for editor and export; every terminal is on the 100 mil grid.

    Custom offsets are y coordinates for left/right and x for top/bottom.
    The body stays centred; dimensions are multiples of two grid steps.
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
            raise ValueError('Invalid symbol body dimensions')
        try:
            width, height = map(float, body)
        except (TypeError, ValueError):
            raise ValueError('Invalid symbol body dimensions') from None
        for value in (width, height):
            if not math.isfinite(value) or value < 4 * spacing or value > 1000:
                raise ValueError('Symbol body dimensions must be between 10.16 and 1000 mm')
            if not math.isclose(value / (2 * spacing), round(value / (2 * spacing)), abs_tol=1e-7):
                raise ValueError('Symbol body must align to the grid')
        half_width, half_height = width / 2, height / 2
    positioned, occupied = [], set()
    counters = dict.fromkeys(sides, 0)
    vertical_rows = max(len(sides['left']), len(sides['right']))
    horizontal_rows = max(len(sides['top']), len(sides['bottom']))
    for pin in pins:
        if pin.position is not None:
            if not isinstance(pin.position, (list, tuple)) or len(pin.position) != 2:
                raise ValueError('Invalid pin position')
            try:
                x, y = map(float, pin.position)
            except (TypeError, ValueError):
                raise ValueError('Invalid pin position') from None
            for value in (x, y):
                if not math.isfinite(value) or abs(value) > 1000 or not math.isclose(
                        value / spacing, round(value / spacing), abs_tol=1e-7):
                    raise ValueError('Pin connection must align to the grid')
            angle = pin.angle
            if angle not in (0, 90, 180, 270):
                raise ValueError('Invalid pin angle')
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
                raise ValueError('Invalid pin offset') from None
        if not math.isfinite(offset) or not math.isclose(offset / spacing, round(offset / spacing), abs_tol=1e-7):
            raise ValueError('Pin connection must align to the grid')
        offset = round(offset, 4)
        if abs(offset) >= (half_height if vertical else half_width):
            raise ValueError('Pin must remain inside its body edge')
        if (pin.side, offset) in occupied:
            raise ValueError('Two pins cannot occupy the same position')
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
                raise ValueError('Pin connection must align to the grid')
        positioned.append(PositionedPin(pin, round(x, 4), round(y, 4), angle))
    points = [(p.x, p.y) for p in positioned]
    if len(points) != len(set(points)):
        raise ValueError('Two pins cannot occupy the same position')
    return SymbolLayout(half_width * 2, half_height * 2, spacing, spacing, tuple(positioned))


def _quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _num(value: float) -> str:
    return f'{value:.4f}'.rstrip('0').rstrip('.') or '0'


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
        out takes its `default_property_positions` place. None for none.
    @return The `.kicad_sym` text, newline-terminated.
    @throws ValueError On an invalid name, reference, metadata or geometry.
    @note Layout: one form per line with KiCad's 100 mil grid coordinates;
        KiCad accepts any whitespace, so the file reads back like one saved
        by the symbol editor. The body rectangle and the pin texts follow
        the KLC defaults (stroke 0.254 mm, background fill, 1.27 mm fonts).
    """
    name = _text(name, 'Symbol name', required=True)
    if ':' in name:
        raise ValueError('Symbol name must not contain a library separator (:).')
    footprint = _text(footprint, 'Footprint')
    if metadata is None:
        metadata = SymbolMetadata(reference=reference)
    elif not isinstance(metadata, SymbolMetadata):
        raise ValueError('metadata: expected SymbolMetadata')
    reference = _reference_prefix(metadata.reference)
    description = _single_line(metadata.description, 'Description')
    keywords = _single_line(metadata.keywords, 'Keywords')
    datasheet = _datasheet(metadata.datasheet)
    fp_filters = _single_line(metadata.fp_filters, 'Footprint filters')
    layout = layout_symbol(pins, body)
    w, h = layout.width / 2, layout.height / 2
    effects = '(effects (font (size 1.27 1.27)))'
    lines = [f'(kicad_symbol_lib (version {SYMBOL_FORMAT_VERSION}) (generator "kicadgen") '
             f'(generator_version {_quote(SYMBOL_GENERATOR_VERSION)})',
             f'  (symbol {_quote(name)}',
             '    (exclude_from_sim no) (in_bom yes) (on_board yes) (in_pos_files yes)',
             '    (duplicate_pin_numbers_are_jumpers no)',
             '    (pin_names (offset 0.508))']
    positions = default_property_positions(layout)
    for key, value in (properties or {}).items():
        if key not in PROPERTY_KEYS:
            raise ValueError(f'properties: unknown property {key!r}')
        positions[key] = _property_position(value, key)
    visible = [('Reference', reference), ('Value', name)]
    hidden = [('Footprint', footprint), ('Datasheet', datasheet), ('Description', description)]
    if keywords:
        hidden.append(('ki_keywords', keywords))
    if fp_filters:
        hidden.append(('ki_fp_filters', fp_filters))
    entries = [(key, value, positions[key], False) for key, value in visible]
    entries += [(key, value, (0.0, 0.0), True) for key, value in hidden]
    for key, value, (x, y), hide in entries:
        flags = '(show_name no) (do_not_autoplace no)' + (' (hide yes)' if hide else '')
        lines.append(f'    (property {_quote(key)} {_quote(value)} (at {_num(x)} {_num(y)} 0) {flags} {effects})')
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
