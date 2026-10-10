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


@dataclass(frozen=True)
class SymbolPin:
    """@brief Store a pin identity, electrical role and optional editor placement."""
    number: str
    name: str
    electrical_type: str = 'passive'
    side: str = 'left'
    offset: float | None = None
    position: tuple[float, float] | None = None
    angle: int | None = None


@dataclass(frozen=True)
class PositionedPin:
    """@brief Pair a symbol pin with its resolved terminal coordinates and angle."""
    pin: SymbolPin
    x: float
    y: float
    angle: int


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
        result.append(replace(pin, number=number, name=name, electrical_type=electrical_type))
    if not result:
        raise ValueError('At least one pin is required')
    return tuple(result)


def symbol_inputs(rows):
    """@brief Decode project metadata without discarding pin identities or electrical roles."""
    pins = [SymbolPin(str(row.get('pin', '')), str(row.get('name') or ''),
                      row.get('electrical_type', 'passive'),
                      row.get('symbol_side', 'left'), row.get('symbol_offset'),
                      row.get('symbol_position'), row.get('symbol_angle')) for row in rows]
    bodies = [row['symbol_body'] for row in rows if 'symbol_body' in row]
    if bodies and any(body != bodies[0] for body in bodies):
        raise ValueError('Inconsistent symbol body dimensions')
    return pins, bodies[0] if bodies else None


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
        x, y, angle = {
            'left': (-half_width - spacing, offset, 0),
            'right': (half_width + spacing, offset, 180),
            'top': (offset, half_height + spacing, 270),
            'bottom': (offset, -half_height - spacing, 90),
        }[pin.side]
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
                     footprint: str = '', reference: str = 'U', body=None, graphics=None) -> str:
    """@brief Return a KiCad 6+ symbol library with a single, single-unit symbol.

    @param graphics Placed glyph records (`symbol_glyphs.instance_from_glyph`)
        and wires drawn inside the body, after the body rectangle; None or []
        for none. Wire points anchored to pins or element terminals are
        exported at their resolved position (`symbol_glyphs.pin_inner_ends`).
    """
    name = _text(name, 'Symbol name', required=True)
    if ':' in name:
        raise ValueError('Symbol name must not contain a library separator (:).')
    footprint = _text(footprint, 'Footprint')
    reference = _text(reference, 'Reference', required=True)
    layout = layout_symbol(pins, body)
    w, h = layout.width / 2, layout.height / 2
    effects = '(effects (font (size 1.27 1.27)))'
    lines = ['(kicad_symbol_lib (version 20211014) (generator kicadgen)',
             f'  (symbol {_quote(name)}',
             '    (pin_names (offset 0.508))', '    (in_bom yes) (on_board yes)']
    for ident, (key, value, y, hidden) in enumerate((
        ('Reference', reference, h + 5.08, False),
        ('Value', name, h + 2.54, False),
        ('Footprint', footprint, 0, True), ('Datasheet', '', 0, True),
    )):
        effect = '(effects (font (size 1.27 1.27)) hide)' if hidden else effects
        lines.append(f'    (property {_quote(key)} {_quote(value)} (id {ident}) (at 0 {_num(y)} 0) {effect})')
    lines.extend((f'    (symbol {_quote(name + "_0_1")}',
                  f'      (rectangle (start {_num(-w)} {_num(h)}) (end {_num(w)} {_num(-h)})',
                  '        (stroke (width 0.254) (type default)) (fill (type background)))'))
    if graphics:
        from .symbol_glyphs import pin_inner_ends, serialize_graphics, validate_instances
        lines.extend(serialize_graphics(validate_instances(graphics), pin_ends=pin_inner_ends(layout)))
    lines.extend(('    )', f'    (symbol {_quote(name + "_1_1")}'))
    for item in layout.pins:
        pin = item.pin
        lines.extend((f'      (pin {pin.electrical_type} line (at {_num(item.x)} {_num(item.y)} {item.angle}) (length {_num(layout.pin_length)})',
                      f'        (name {_quote(pin.name or "~")} {effects})',
                      f'        (number {_quote(pin.number)} {effects}))'))
    lines.extend(('    )', '  )', ')', ''))
    return '\n'.join(lines)


def export_symbol(path: str | Path, name: str, pins: Iterable[SymbolPin], *,
                  footprint: str = '', reference: str = 'U', body=None, graphics=None) -> Path:
    """@brief Validate before touching disk and atomically replace an explicitly chosen file.

    @param graphics Placed glyph records, see `serialize_symbol`.
    """
    text = serialize_symbol(name, pins, footprint=footprint, reference=reference, body=body,
                            graphics=graphics)
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
