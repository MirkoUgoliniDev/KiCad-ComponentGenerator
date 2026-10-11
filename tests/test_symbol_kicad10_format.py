"""@brief The symbol writer emits the KiCad 10 library format and KiCad reads it back.

The format assertions use the shape of KiCad's own `Relay.kicad_sym`
(version 20251024): no `(id n)` on properties, `(hide yes)` as a form,
`exclude_from_sim`/`in_pos_files`/`duplicate_pin_numbers_are_jumpers`
flags. The round trip through `kicad-cli` runs only when KiCad 10 is
installed; it is the proof that the text is not just plausible.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from kicadgen.core.footprint import Footprint, FpRect, Pad
from kicadgen.generators import symbol_glyphs as g
from kicadgen.generators.symbol import (
    SYMBOL_FORMAT_VERSION, SymbolMetadata, SymbolPin, export_symbol, serialize_symbol,
)


def _kicad_cli():
    cli = shutil.which('kicad-cli')
    if not cli:
        candidate = Path('C:/Program Files/KiCad/10.0/bin/kicad-cli.exe')
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        pytest.skip('KiCad CLI not installed')
    return cli


def _relay_symbol(**metadata):
    """A relay drawn with the built-in coil/contact glyphs and a wire between them."""
    coil, _link, changeover = g.relay_part_glyphs()[:3]
    coil_instance = g.instance_from_glyph(coil, -7.62, 0)
    contact_instance = g.instance_from_glyph(changeover, 7.62, 0)
    wire = {'key': g.WIRE_KEY, 'label': 'Filo', 'source': 'wire', 'x': 0.0, 'y': 0.0, 'rotation': 0,
            'mirror': False, 'terminals': [],
            'primitives': [{'kind': 'polyline', 'points': [[-7.62, 7.62], [-7.62, 12.7], [-17.78, 12.7]],
                            'radius': 0.0, 'fill': 'none', 'width': 0.254}]}
    pins = [SymbolPin('A1', 'A1', 'passive', position=(-17.78, 12.7), angle=0),
            SymbolPin('A2', 'A2', 'passive', position=(-17.78, -12.7), angle=0),
            SymbolPin('11', 'COM', 'passive', position=(17.78, -12.7), angle=180),
            SymbolPin('12', 'NC', 'passive', position=(17.78, 12.7), angle=180)]
    return serialize_symbol('RelayTest', pins, footprint='Relay_THT:Relay_Test', body=[40.64, 30.48],
                            graphics=[coil_instance, contact_instance, wire],
                            metadata=SymbolMetadata(**metadata) if metadata else None)


def test_header_and_symbol_flags_follow_the_kicad_10_library_format():
    text = serialize_symbol('M', [SymbolPin('1', 'A')])
    assert text.startswith(f'(kicad_symbol_lib (version {SYMBOL_FORMAT_VERSION}) (generator "kicadgen") '
                           '(generator_version "10.0")')
    assert SYMBOL_FORMAT_VERSION == 20251024
    for flag in ('(exclude_from_sim no)', '(in_bom yes)', '(on_board yes)', '(in_pos_files yes)',
                 '(duplicate_pin_numbers_are_jumpers no)', '(pin_names (offset 0.508))', '(embedded_fonts no)'):
        assert flag in text
    assert '(id ' not in text and ' hide)' not in text, 'KiCad 6 property syntax must be gone'
    assert '20211014' not in text


def test_properties_carry_show_name_autoplace_and_hide_forms():
    text = serialize_symbol('M', [SymbolPin('1', 'A')], footprint='L:F')
    visible = re.findall(r'\(property "(Reference|Value)" "([^"]*)" \(at 0 ([\d.]+) 0\) \(show_name no\) '
                         r'\(do_not_autoplace no\) \(effects \(font \(size 1.27 1.27\)\)\)\)', text)
    assert [(key, value) for key, value, _ in visible] == [('Reference', 'U'), ('Value', 'M')]
    assert float(visible[0][2]) > float(visible[1][2]) > 5.08, 'Reference above Value, both above the body'
    hidden = re.findall(r'\(property "(\w+)" "([^"]*)" \(at 0 0 0\) \(show_name no\) \(do_not_autoplace no\) '
                        r'\(hide yes\) \(effects \(font \(size 1.27 1.27\)\)\)\)', text)
    assert hidden == [('Footprint', 'L:F'), ('Datasheet', ''), ('Description', '')]
    assert 'ki_keywords' not in text and 'ki_fp_filters' not in text, 'empty optional properties are omitted'


def test_metadata_fills_the_properties_and_its_reference_wins():
    text = serialize_symbol('M', [SymbolPin('1', 'A')], reference='U', metadata=SymbolMetadata(
        reference='K', description='Relè 5 V', keywords='relay spdt',
        datasheet='https://example.org/r.pdf', fp_filters='Relay_* Relay_THT:Relay_?'))
    assert '(property "Reference" "K"' in text and '"U"' not in text
    assert '(property "Description" "Relè 5 V" (at 0 0 0) (show_name no) (do_not_autoplace no) (hide yes)' in text
    assert '(property "Datasheet" "https://example.org/r.pdf"' in text
    assert '(property "ki_keywords" "relay spdt"' in text
    assert '(property "ki_fp_filters" "Relay_* Relay_THT:Relay_?"' in text
    assert text.index('"Footprint"') < text.index('"Datasheet"') < text.index('"Description"') \
        < text.index('"ki_keywords"') < text.index('"ki_fp_filters"')


def test_the_reference_keyword_still_works_without_metadata():
    assert '(property "Reference" "RV"' in serialize_symbol('M', [SymbolPin('1', 'A')], reference='RV')


@pytest.mark.parametrize('metadata', [
    SymbolMetadata(reference=''), SymbolMetadata(reference='U1'), SymbolMetadata(reference='U 1'),
    SymbolMetadata(reference='K?'), SymbolMetadata(description='a\nb'), SymbolMetadata(keywords='a\x00'),
    SymbolMetadata(datasheet='x\ny'), SymbolMetadata(datasheet='x\ty'),
    SymbolMetadata(fp_filters='a\nb'), 'not metadata'])
def test_invalid_metadata_is_rejected_before_writing(tmp_path, metadata):
    target = tmp_path / 'm.kicad_sym'
    target.write_text('previous', encoding='utf-8')
    with pytest.raises(ValueError):
        export_symbol(target, 'M', [SymbolPin('1', 'A')], metadata=metadata)
    assert target.read_text(encoding='utf-8') == 'previous'


@pytest.mark.parametrize('metadata', [
    SymbolMetadata(reference='RV'), SymbolMetadata(description='one line, with "quotes"'),
    SymbolMetadata(datasheet='C:/docs/relay.pdf'), SymbolMetadata(fp_filters='A* B?'),
    SymbolMetadata(datasheet='C:/Documenti del progetto/relay.pdf')])  # blanks: a Windows path (review 2026-10-10)
def test_valid_metadata_is_accepted(metadata):
    assert '(symbol "M"' in serialize_symbol('M', [SymbolPin('1', 'A')], metadata=metadata)


def test_pins_follow_the_library_pin_syntax():
    text = serialize_symbol('M', [SymbolPin('1', 'A', 'input'), SymbolPin('2', '', 'power_in', side='right')])
    assert re.search(r'\(pin input line \(at -\d+\.?\d* 0 0\) \(length 2.54\)\n\s+\(name "A" \(effects \(font '
                     r'\(size 1.27 1.27\)\)\)\)\n\s+\(number "1" \(effects \(font \(size 1.27 1.27\)\)\)\)\)', text)
    assert '(name "~" (effects' in text, 'an unnamed pin is written as ~ like KiCad does'


def test_graphics_still_go_inside_the_graphic_unit():
    text = _relay_symbol()
    unit = text.split('(symbol "RelayTest_0_1"')[1].split('(symbol "RelayTest_1_1"')[0]
    assert unit.count('(rectangle') >= 2 and '(polyline' in unit
    assert '(xy -17.78 12.7)' in unit, 'the wire reaches the pin'


def test_kicad_10_reads_the_symbol_with_graphics_and_metadata_and_exports_svg(tmp_path):
    cli = _kicad_cli()
    text = _relay_symbol(reference='K', description='Relay test', keywords='relay',
                         datasheet='https://example.org/r.pdf', fp_filters='Relay_*')
    path = tmp_path / 'relay.kicad_sym'
    path.write_text(text, encoding='utf-8')
    result = subprocess.run([cli, 'sym', 'export', 'svg', '-o', str(tmp_path / 'svg'), str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / 'svg').glob('*.svg'))


def test_kicad_10_reads_a_generated_footprint_with_metadata_and_exports_svg(tmp_path):
    cli = _kicad_cli()
    fp = Footprint(name='FP_Test', description='Test footprint', tags='test',
                   datasheet='https://example.org/fp.pdf', attr='smd')
    fp.add_pad(Pad(number='1', pad_type='smd', shape='rect', at=(0, 0), size=(1, 1),
                   layers=['F.Cu', 'F.Paste', 'F.Mask']))
    fp.add_graphic(FpRect(start=(-1, -1), end=(1, 1), layer='F.CrtYd', width=0.05))
    fp.add_graphic(FpRect(start=(-0.8, -0.8), end=(0.8, 0.8), layer='F.Fab', width=0.1))
    folder = tmp_path / 'Test.pretty'
    fp.save(str(folder / 'FP_Test.kicad_mod'))
    (tmp_path / 'svg').mkdir()   # kicad-cli fp export does not create the output folder
    result = subprocess.run([cli, 'fp', 'export', 'svg', '-o', str(tmp_path / 'svg'), str(folder)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / 'svg').glob('*.svg'))


def test_footprint_properties_carry_datasheet_and_description():
    fp = Footprint(name='FP', description='Una descrizione', datasheet='https://example.org/fp.pdf')
    text = fp.render()
    assert '(property "Datasheet" "https://example.org/fp.pdf"' in text
    assert '(property "Description" "Una descrizione"' in text
    assert text.count('(hide yes)') == 2, 'both stay hidden'
    bare = Footprint(name='FP').render()
    assert '(property "Datasheet" ""' in bare and '(property "Description" ""' in bare


def test_build_package_uses_the_project_description_and_keeps_the_generic_one_without():
    """The project's description reaches the package footprint; empty keeps the family text."""
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    from kicadgen.generators.package import build_package
    geometry = PackageGeometry(name='PKG', family='soic', pins=[
        PinSpec('1', -2.0, 0.0, 1.0, 0.6, pad_type='smd'), PinSpec('2', 2.0, 0.0, 1.0, 0.6, pad_type='smd')])
    with_text = build_package(geometry, description='Relè di segnale 2 contatti').render()
    assert '(descr "Relè di segnale 2 contatti")' in with_text
    assert '(property "Description" "Relè di segnale 2 contatti"' in with_text
    generic = build_package(geometry, description='   ').render()
    assert '(descr "soic package generico")' in generic
