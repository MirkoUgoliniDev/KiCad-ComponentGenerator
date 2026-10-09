"""Symbol connectivity, safe export and compatibility with the real KiCad parser."""
from pathlib import Path
import shutil
import subprocess

import pytest

from kicadgen.generators.symbol import (
    ELECTRICAL_TYPES, SymbolPin, export_symbol, layout_symbol, normalize_pins,
    serialize_symbol,
)


def test_pin_identity_and_types_survive_layout_and_export():
    pins = [SymbolPin(str(n), 'GND' if n % 2 else 'GPIO1',
                      'power_in' if n % 2 else 'bidirectional',
                      'left' if n <= 22 else 'right') for n in range(1, 45)]
    layout = layout_symbol(pins)
    assert [p.pin.number for p in layout.pins] == [str(n) for n in range(1, 45)]
    assert len({(p.x, p.y) for p in layout.pins}) == 44
    assert all(abs(p.x / 2.54 - round(p.x / 2.54)) < 1e-9 for p in layout.pins)
    assert all(abs(p.y / 2.54 - round(p.y / 2.54)) < 1e-9 for p in layout.pins)
    assert layout.pins[0].y == layout.pins[22].y
    text = serialize_symbol('ESP32', pins, footprint='ESP32:ESP32')
    assert text.count('(pin power_in line') == 22
    assert text.count('(pin bidirectional line') == 22
    assert '(property "Footprint" "ESP32:ESP32"' in text
    assert '(property "Reference" "U"' in text


@pytest.mark.parametrize('pins', [[], [SymbolPin('', 'GND')],
    [SymbolPin('1', 'GND'), SymbolPin(' 1 ', 'GND')],
    [SymbolPin('1\n', 'GND')], [SymbolPin('1', 'G\x00ND')],
    [SymbolPin('1', 'GND', side='top')]])
def test_invalid_identity_never_exports(tmp_path, pins):
    path = tmp_path / 'module.kicad_sym'
    path.write_text('previous', encoding='utf-8')
    with pytest.raises(ValueError):
        export_symbol(path, 'Module', pins)
    assert path.read_text(encoding='utf-8') == 'previous'


def test_unknown_electrical_role_is_never_guessed():
    result = normalize_pins([SymbolPin('A1', 'GPIO2', 'invented'), SymbolPin('A2', '5V')])
    assert [p.electrical_type for p in result] == ['passive', 'passive']


@pytest.mark.parametrize('left,right', [(1, 0), (2, 2), (3, 3), (22, 22), (4, 1), (0, 6)])
def test_exported_connection_points_match_visible_grid(left, right):
    import re
    from kicadgen.gui.symbol_tab import GRID_MM
    pins = [SymbolPin(str(n), 'GPIO', side='left' if n < left else 'right')
            for n in range(left + right)]
    layout = layout_symbol(pins)
    exported = serialize_symbol('Board', pins)
    points = re.findall(r'\(pin \w+ line \(at ([-\d.]+) ([-\d.]+) (\d+)\)', exported)
    assert len(points) == len(pins)
    for placed, (x, y, angle) in zip(layout.pins, points):
        assert (float(x), float(y), int(angle)) == (placed.x, placed.y, placed.angle)
        for value in (float(x), float(y)):
            assert value / GRID_MM == pytest.approx(round(value / GRID_MM))
        assert abs(placed.y) < layout.height / 2


def test_long_names_expand_body_without_changing_grid():
    short = layout_symbol([SymbolPin('1', 'GPIO1')])
    long = layout_symbol([SymbolPin('1', 'LONG_FUNCTION_NAME' * 3)])
    assert long.width > short.width
    assert long.pin_spacing == short.pin_spacing == 2.54


def test_failed_atomic_replace_preserves_original(tmp_path, monkeypatch):
    import kicadgen.generators.symbol as module
    path = tmp_path / 'module.kicad_sym'
    path.write_text('previous', encoding='utf-8')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(module, 'replace_file', fail)
    with pytest.raises(OSError):
        export_symbol(path, 'Module', [SymbolPin('1', 'GND')])
    assert path.read_text(encoding='utf-8') == 'previous'
    assert list(tmp_path.iterdir()) == [path]


def test_user_strings_are_quoted():
    text = serialize_symbol('Module "test"', [SymbolPin('A1', 'GPIO"2\\x')])
    assert '"Module \\"test\\""' in text
    assert '"GPIO\\"2\\\\x"' in text


def test_real_kicad_can_load_all_electrical_types_and_export_svg(tmp_path):
    cli = shutil.which('kicad-cli')
    if not cli:
        candidate = Path('C:/Program Files/KiCad/10.0/bin/kicad-cli.exe')
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        pytest.skip('KiCad CLI not installed')
    pins = [SymbolPin(str(i), 'GND' if i % 2 else 'GPIO"1\\test', kind,
                      'left' if i < 6 else 'right')
            for i, kind in enumerate(ELECTRICAL_TYPES, 1)]
    path = export_symbol(tmp_path / 'module.kicad_sym', 'Module', pins,
                         footprint='MyLib:MyFootprint')
    result = subprocess.run([cli, 'sym', 'export', 'svg', '-o', str(tmp_path / 'svg'), str(path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / 'svg').glob('*.svg'))
