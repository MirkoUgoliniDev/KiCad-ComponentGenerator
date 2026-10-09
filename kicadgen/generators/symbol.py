"""Electrical symbols: one shared millimetre layout for preview and KiCad export.

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
    number: str
    name: str
    electrical_type: str = 'passive'
    side: str = 'left'


@dataclass(frozen=True)
class PositionedPin:
    pin: SymbolPin
    x: float
    y: float
    angle: int


@dataclass(frozen=True)
class SymbolLayout:
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
    """Validate identities; preserve names/order and default unknown types passive."""
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
        if pin.side not in ('left', 'right'):
            raise ValueError(f'Pin {number}: invalid side')
        electrical_type = pin.electrical_type if pin.electrical_type in ELECTRICAL_TYPES else 'passive'
        result.append(replace(pin, number=number, name=name, electrical_type=electrical_type))
    if not result:
        raise ValueError('At least one pin is required')
    return tuple(result)


def layout_symbol(pins: Iterable[SymbolPin]) -> SymbolLayout:
    """Deterministic 100 mil spacing, connection points on the 100 mil grid.

    Both columns retain input order from top to bottom. Width grows with the
    longest names on each side instead of shrinking text or overlapping labels.
    """
    pins = normalize_pins(pins)
    spacing = PIN_GRID_MM
    sides = {side: [p for p in pins if p.side == side] for side in ('left', 'right')}
    # Conservative full-em estimate accommodates Unicode and wide glyphs.
    lengths = [max((len(p.name) for p in group), default=0) for group in sides.values()]
    half_width = max(5.08, math.ceil((sum(lengths) * TEXT_SIZE + 5.08) / 5.08) * 2.54)
    rows = max(map(len, sides.values()))
    height = (rows + 1) * spacing
    # With an even row count exact centring offsets every pin by half a
    # grid step. Prefer on-grid connections to exact visual symmetry.
    first_y = (rows // 2) * spacing
    counters = {'left': 0, 'right': 0}
    positioned = []
    for pin in pins:
        index = counters[pin.side]
        counters[pin.side] += 1
        x = half_width + spacing
        if pin.side == 'left':
            x = -x
        positioned.append(PositionedPin(pin, x, round(first_y - index * spacing, 4),
                                        0 if pin.side == 'left' else 180))
    return SymbolLayout(half_width * 2, height, spacing, spacing, tuple(positioned))


def _quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _num(value: float) -> str:
    return f'{value:.4f}'.rstrip('0').rstrip('.') or '0'


def serialize_symbol(name: str, pins: Iterable[SymbolPin], *,
                     footprint: str = '', reference: str = 'U') -> str:
    """Return a KiCad 6+ symbol library with a single, single-unit symbol."""
    name = _text(name, 'Symbol name', required=True)
    if ':' in name:
        raise ValueError('Symbol name must not contain a library separator (:).')
    footprint = _text(footprint, 'Footprint')
    reference = _text(reference, 'Reference', required=True)
    layout = layout_symbol(pins)
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
                  '        (stroke (width 0.254) (type default)) (fill (type background)))',
                  '    )', f'    (symbol {_quote(name + "_1_1")}'))
    for item in layout.pins:
        pin = item.pin
        lines.extend((f'      (pin {pin.electrical_type} line (at {_num(item.x)} {_num(item.y)} {item.angle}) (length {_num(layout.pin_length)})',
                      f'        (name {_quote(pin.name or "~")} {effects})',
                      f'        (number {_quote(pin.number)} {effects}))'))
    lines.extend(('    )', '  )', ')', ''))
    return '\n'.join(lines)


def export_symbol(path: str | Path, name: str, pins: Iterable[SymbolPin], *,
                  footprint: str = '', reference: str = 'U') -> Path:
    """Validate before touching disk and atomically replace an explicitly chosen file."""
    text = serialize_symbol(name, pins, footprint=footprint, reference=reference)
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
