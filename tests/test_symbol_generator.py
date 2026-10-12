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
    [SymbolPin('1', 'GND', side='diagonal')]])
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


@pytest.mark.parametrize('free_positions', [False, True])
def test_real_kicad_can_load_all_electrical_types_and_export_svg(tmp_path, free_positions):
    cli = shutil.which('kicad-cli')
    if not cli:
        candidate = Path('C:/Program Files/KiCad/10.0/bin/kicad-cli.exe')
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        pytest.skip('KiCad CLI not installed')
    pins = [SymbolPin(str(i), 'GND' if i % 2 else 'GPIO"1\\test', kind,
                      ('left', 'right', 'top', 'bottom')[(i - 1) // 3],
                      position=(round((i - 6) * 5.08, 4), -25.4) if free_positions else None,
                      angle=(i % 4) * 90 if free_positions else None)
            for i, kind in enumerate(ELECTRICAL_TYPES, 1)]
    path = export_symbol(tmp_path / 'module.kicad_sym', 'Module', pins,
                         footprint='MyLib:MyFootprint')
    result = subprocess.run([cli, 'sym', 'export', 'svg', '-o', str(tmp_path / 'svg'), str(path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / 'svg').glob('*.svg'))


def test_custom_four_side_layout_and_export():
    import re
    pins = [SymbolPin(str(i), 'GPIO' + str(i), 'input', side, 2.54)
            for i, side in enumerate(('left', 'right', 'top', 'bottom'), 1)]
    layout = layout_symbol(pins, [30.48, 20.32])
    assert [(p.x, p.y, p.angle) for p in layout.pins] == [
        (-17.78, 2.54, 0), (17.78, 2.54, 180), (2.54, 12.7, 270), (2.54, -12.7, 90)]
    text = serialize_symbol('Custom', pins, body=[30.48, 20.32])
    assert '(start -15.24 10.16) (end 15.24 -10.16)' in text
    positions = re.findall(r'\(pin input line \(at ([-\d.]+) ([-\d.]+) (\d+)\)', text)
    assert [(float(x), float(y), int(a)) for x, y, a in positions] == [
        (p.x, p.y, p.angle) for p in layout.pins]


@pytest.mark.parametrize('pins,body', [
    ([SymbolPin('1', 'A', offset=1.27)], [20.32, 20.32]),
    ([SymbolPin('1', 'A', offset=float('nan'))], [20.32, 20.32]),
    ([SymbolPin('1', 'A', offset=10.16)], [20.32, 20.32]),
    ([SymbolPin('1', 'A', offset=0), SymbolPin('2', 'B', offset=0)], [20.32, 20.32]),
    ([SymbolPin('1', 'A')], [20, 20.32]),
    ([SymbolPin('1', 'A')], [float('inf'), 20.32]),
])
def test_invalid_custom_geometry_does_not_replace_export(tmp_path, pins, body):
    target = tmp_path / 'custom.kicad_sym'
    target.write_text('previous')
    with pytest.raises(ValueError):
        export_symbol(target, 'Custom', pins, body=body)
    assert target.read_text() == 'previous'


@pytest.mark.parametrize('angle', [0, 90, 180, 270])
def test_free_pin_export_keeps_terminal_and_orientation(angle):
    pin = SymbolPin('42', 'GPIO', 'input', position=(50.8, -25.4), angle=angle)
    assert f'(at 50.8 -25.4 {angle})' in serialize_symbol('Free', [pin], body=[10.16, 10.16])


@pytest.mark.parametrize('position,angle', [((1, 2.54), 0), ((float('nan'), 0), 0),
    ((0, 0), 45), ((0,), 90), ((0, 0), None)])
def test_invalid_free_pin_is_rejected(position, angle):
    with pytest.raises(ValueError):
        layout_symbol([SymbolPin('1', 'A', position=position, angle=angle)])



def test_pin_length_text_size_and_hidden_reach_the_symbol_and_stay_on_the_grid():
    """Pin properties (user, 2026-10-12): a custom length is a multiple of 1.27 mm
    whose outer end, for a side-placed pin, must still fall on the 2.54 mm grid."""
    from kicadgen.generators import symbol as sym
    text = sym.serialize_symbol('P', [sym.SymbolPin('1', 'A', length=5.08, hidden=True, text_size=1.0),
                                      sym.SymbolPin('2', 'B', side='right')])
    assert '(pin passive line (at -10.16 0 0) (length 5.08) (hide yes)' in text
    assert '(name "A" (effects (font (size 1 1))))' in text
    assert '(pin passive line (at 7.62 0 180) (length 2.54)\n' in text
    with pytest.raises(ValueError):
        sym.layout_symbol([sym.SymbolPin('1', 'A', length=3.81)])
    assert sym.layout_symbol([sym.SymbolPin('1', 'A', position=(-7.62, 0), angle=0, length=3.81)]).pins[0].x == -7.62
    for bad in ({'length': 1.0}, {'length': 15.24}, {'length': True}, {'text_size': 0.1}, {'text_size': 9.0}):
        with pytest.raises(ValueError):
            sym.layout_symbol([sym.SymbolPin('1', 'A', **bad)])
    pins, _body = sym.symbol_inputs([{'pin': '1', 'name': 'A', 'symbol_pin_length': 5.08,
                                      'symbol_pin_hidden': True, 'symbol_pin_text_size': 1.0}])
    assert (pins[0].length, pins[0].hidden, pins[0].text_size) == (5.08, True, 1.0)
    from kicadgen.generators import symbol_glyphs as g
    layout = sym.layout_symbol(pins)
    assert g.pin_inner_ends(layout)['1'] == (round(layout.pins[0].x + 5.08, 4), 0.0)
