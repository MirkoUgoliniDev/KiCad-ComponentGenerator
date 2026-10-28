"""@brief Review of 2026-10-27, generator findings G1-G8: one quoter, one number
formatter, no control characters in symbol texts, pin numbers kept equal to
pad numbers, str/int pin keys of the header, honest trapezoid pads and a
lexer that decodes KiCad's escapes.

Every rule has a positive case (the defect is refused or written right) and a
negative case (ordinary data is untouched). The kicad-cli proofs run read-only
in a temporary folder and are skipped where KiCad is not installed.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from kicadgen.core import sexpr
from kicadgen.core.footprint import Footprint, FpText, Pad, footprint_file_name
from kicadgen.core.package_geometry import PAD_SHAPES as GEOMETRY_PAD_SHAPES, PinSpec
from kicadgen.core.sexpr import format_number, quote
from kicadgen.generators import symbol as symbol_module
from kicadgen.generators import symbol_glyphs as glyphs
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header, pin_positions
from kicadgen.generators.symbol import SymbolError, SymbolPin, normalize_pins, serialize_symbol
from kicadgen.services import klc_checks
from kicadgen.services.system_dependencies import find_kicad_cli

#: A number token «-0» standing alone (not «-0.5», not «10»).
NEGATIVE_ZERO = re.compile(r"(?<![\w.])-0(?![\w.])")


def _kicad_cli() -> str:
    """The installed kicad-cli, or a skip: the proofs need the real reader."""
    cli = find_kicad_cli() or shutil.which("kicad-cli")
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


def _upgrade(cli: str, kind: str, target: Path) -> subprocess.CompletedProcess:
    """Run `kicad-cli <kind> upgrade --force` on a copy inside %TEMP%, read-only for the repository."""
    return subprocess.run([cli, kind, "upgrade", "--force", str(target)],
                          capture_output=True, text=True, timeout=120)


def _text_instance(text: str) -> dict:
    """A placed text glyph record as the symbol editor stores it."""
    return {"id": "t1", "key": "shape:text", "label": "T", "source": "shape", "x": 0, "y": 0,
            "rotation": 0, "mirror": False,
            "primitives": [{"kind": "text", "points": [[0, 0]], "text": text, "size": 1.27,
                            "angle": 0, "justify": "left", "vjustify": "bottom"}],
            "terminals": []}


# ── G1: control characters never reach a .kicad_sym ───────────────────────

@pytest.mark.parametrize("bad", ["Riga1\nRiga2", "a\rb", "tab\there", "nul\x00"])
def test_g1_a_symbol_text_with_a_control_character_is_refused(bad):
    """@brief Positive: the record validation names the cause instead of writing a broken library."""
    with pytest.raises(ValueError, match="caratteri di controllo"):
        glyphs.validate_instances([_text_instance(bad)])


def test_g1_an_ordinary_symbol_text_is_kept_unchanged():
    """@brief Negative: quotes, backslashes and non-ASCII letters are legitimate text."""
    record = _text_instance('q"t\\b µF 3.3Ω ~{OE}')
    assert glyphs.validate_instances([record]) == [record]


def test_g1_g5_the_symbol_writers_use_the_shared_quoter():
    """@brief The two symbol writers quote through core.sexpr: one escaping rule for every file."""
    assert symbol_module._quote is quote
    assert glyphs._quote_text is quote
    assert sexpr._quote is quote
    assert quote('a"b\\c\nd\re\tf') == '"a\\"b\\\\c\\nd\\re\\tf"'


def test_g1_a_serialized_symbol_text_with_quote_and_backslash_loads_in_kicad():
    """@brief kicad-cli proof: the escaped text is accepted by the KiCad 10 reader."""
    cli = _kicad_cli()
    text = serialize_symbol("ODD", [SymbolPin("1", 'A"B', "input"), SymbolPin("2", "C\\D", "output", "right")],
                            graphics=[_text_instance('q"t\\b µ')])
    assert "\\n" not in text and "\n\"" not in text.replace('\n"', "")  # no raw newline inside quotes
    folder = Path(tempfile.mkdtemp(prefix="kicadgen-g1-"))
    try:
        target = folder / "Odd.kicad_sym"
        target.write_text(text, encoding="utf-8", newline="\n")
        result = _upgrade(cli, "sym", target)
        assert result.returncode == 0, result.stderr + result.stdout
        assert '"q\\"t\\\\b µ"' in target.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ── G2: pin numbers stay equal to pad numbers ─────────────────────────────

def test_g2_a_pin_number_with_an_inner_space_is_refused_naming_the_pin():
    """@brief Positive: KiCad would rewrite «A 5» as «A_5» and the pad would keep «A 5»."""
    with pytest.raises(SymbolError) as info:
        normalize_pins([SymbolPin("A 5", "X")])
    assert "A 5" in str(info.value) and "spaces" in str(info.value)


def test_g2_a_pin_name_with_spaces_gets_kicad_s_rewriting_and_a_note():
    """@brief Positive: the name is written as KiCad will keep it, and the user is told."""
    notes = []
    (pin,) = normalize_pins([SymbolPin("3", "µV ~{OE}", "passive", "top")], notes)
    assert pin.name == "µV_~{OE}"
    assert len(notes) == 1
    assert "µV ~{OE}" in str(notes[0]) and "µV_~{OE}" in str(notes[0])
    assert "Pin 3" in notes[0].localized()


def test_g2_a_clean_pin_is_untouched_and_gives_no_note():
    """@brief Negative: «A5» / «GPIO_1» pass through with nothing to report."""
    notes = []
    (pin,) = normalize_pins([SymbolPin("A5", "GPIO_1")], notes)
    assert (pin.number, pin.name) == ("A5", "GPIO_1")
    assert notes == []


def _pin_form(number: str, name: str) -> str:
    return (f'(pin passive line (at -7.62 0 0) (length 2.54) (name "{name}" (effects (font (size 1.27 1.27))))'
            f' (number "{number}" (effects (font (size 1.27 1.27)))))')


def _library(*pins: str) -> str:
    return ('(kicad_symbol_lib (version 20251024) (generator "kicadgen") (symbol "S" '
            '(symbol "S_0_1" (rectangle (start -5.08 2.54) (end 5.08 -2.54) (stroke (width 0.254) (type default)) '
            '(fill (type background)))) (symbol "S_1_1" ' + " ".join(pins) + ")))")


def test_g2_the_klc_check_flags_spaces_in_pin_numbers_as_errors_and_in_names_as_advice():
    """@brief Positive: a hand-edited library with «A 5» gets an error, a spaced name a warning."""
    findings = klc_checks.check_symbol_text(_library(_pin_form("A 5", "X"), _pin_form("2", "SPACED NAME")))
    by_code = {finding.code: finding for finding in findings}
    assert by_code["pin_number_spaces"].severity == "error"
    assert "A 5" in by_code["pin_number_spaces"].message
    assert by_code["pin_name_spaces"].severity == "warning"
    assert all(finding.hint for finding in findings)


def test_g2_the_klc_check_is_silent_on_clean_pins():
    """@brief Negative: the generated symbol raises neither of the two codes."""
    pins = [SymbolPin("A5", "GPIO_1"), SymbolPin("2", "GND", "passive", "right")]
    findings = klc_checks.check_symbol_text(serialize_symbol("S", pins))
    assert {finding.code for finding in findings} & {"pin_number_spaces", "pin_name_spaces"} == set()


# ── G3 / G5: one number formatter, never «-0» ─────────────────────────────

@pytest.mark.parametrize("value", [-0.0, -0.0000004, -1e-7, 0.0])
def test_g3_negative_zero_and_values_rounding_to_it_are_written_as_zero(value):
    """@brief Positive: the shared formatter never writes «-0».

    The shared formatter keeps 6 decimals (KiCad's own precision), so a
    value such as -0.00004 is a real number and is written as it is; only
    what rounds to zero AT that precision becomes «0».
    """
    assert format_number(value) == "0"
    assert symbol_module._num(value) == "0"
    assert glyphs._num(value) == "0"


def test_g3_other_numbers_keep_their_sign_and_digits():
    """@brief Negative: «-0.5», «2.54» and «-7.62» are untouched."""
    assert [format_number(v) for v in (-0.5, 2.54, -7.62, 3)] == ["-0.5", "2.54", "-7.62", "3"]


def test_g3_g5_the_symbol_writers_share_the_formatter_and_a_centred_pin_has_no_minus_zero():
    """@brief A centred top pin used to be written `(at -0 7.62 270)`."""
    assert symbol_module._num is format_number
    assert glyphs._num is format_number
    text = serialize_symbol("C", [SymbolPin("1", "A", "passive", "top"), SymbolPin("2", "B", "passive", "bottom")])
    assert "(at 0 " in text
    assert not NEGATIVE_ZERO.search(text), text


# ── G4: header pins keyed by their text identity ──────────────────────────

def test_g4_a_str_keyed_pin_map_still_gives_pin_1_the_square_pad():
    """@brief Positive: JSON gives text keys; pin 1 is pin 1 whatever the type."""
    params = PinHeaderParams(rows=2, cols=2, pin_map={"1": (0, 0), "2": (0, 1), "3": (1, 1), "4": (1, 0)})
    params.validate()
    pads = {pad.number: pad for pad in build_pin_header(params).pads}
    assert pads["1"].shape == "rect"
    assert {pads[n].shape for n in ("2", "3", "4")} == {"circle"}


def test_g4_mixed_int_and_str_populated_pins_validate_against_an_int_map():
    """@brief Positive: the set difference compares text keys, not Python types."""
    params = PinHeaderParams(rows=2, cols=2, pin_map={1: (0, 0), 2: (0, 1), 3: (1, 1)},
                             populated_pins={"1", 2, "3"})
    params.validate()
    assert [pin for pin, _x, _y in pin_positions(params)] == [1, 2, 3]


def test_g4_a_populated_pin_without_a_position_is_still_an_error():
    """@brief Negative: the normalisation must not hide a real gap in the map."""
    params = PinHeaderParams(rows=2, cols=2, pin_map={1: (0, 0), 2: (0, 1)}, populated_pins={1, "4"})
    with pytest.raises(ValueError, match="4"):
        params.validate()


def test_g4_the_same_pin_spelled_twice_in_the_map_is_refused():
    """@brief Negative: {1: ..., "1": ...} would place one pin twice."""
    with pytest.raises(ValueError, match="due volte"):
        PinHeaderParams(rows=2, cols=2, pin_map={1: (0, 0), "1": (0, 1)}).validate()


def test_g4_the_pin_one_marker_and_the_square_pad_agree_for_text_keys():
    """@brief The silk dot sits at the corner nearest the square pad, also with text keys."""
    square = build_pin_header(PinHeaderParams(rows=2, cols=3, pin_map={"1": (1, 2), "2": (0, 0)}))
    assert {pad.number: pad.shape for pad in square.pads} == {"1": "rect", "2": "circle"}


# ── G7: a trapezoid pad is written with its geometry or not at all ────────

def test_g7_a_trapezoid_pad_requires_its_rect_delta():
    """@brief Positive: without the delta KiCad would silently draw a rectangle."""
    with pytest.raises(ValueError, match="rect_delta"):
        Pad(number="1", pad_type="smd", shape="trapezoid", at=(0, 0), size=(2, 1), layers=["F.Cu"])


@pytest.mark.parametrize("delta", [(0.5, 0.5), (0.0, 0.0), (2.0, 0.0), (0.0, float("nan"))])
def test_g7_an_impossible_rect_delta_is_refused(delta):
    """@brief Positive: both deltas, neither, or a delta that crosses the sides."""
    with pytest.raises(ValueError, match="rect_delta"):
        Pad(number="1", pad_type="smd", shape="trapezoid", at=(0, 0), size=(2, 1), layers=["F.Cu"],
            rect_delta=delta)


def test_g7_rect_delta_is_refused_on_a_rectangle():
    """@brief Negative: the delta belongs to the trapezoid shape only."""
    with pytest.raises(ValueError, match="trapezoid"):
        Pad(number="1", pad_type="smd", shape="rect", at=(0, 0), size=(2, 1), layers=["F.Cu"], rect_delta=(0, 0.5))


def test_g7_a_trapezoid_with_its_delta_is_written_and_a_pinspec_cannot_ask_for_one():
    """@brief The writer emits `(rect_delta 0 0.5)`; PinSpec has no delta, so it refuses the shape."""
    pad = Pad(number="1", pad_type="smd", shape="trapezoid", at=(0, 0), size=(2, 1), layers=["F.Cu"],
              rect_delta=(0, 0.5))
    assert "(rect_delta 0 0.5)" in str(pad.to_sexpr())
    assert "trapezoid" not in GEOMETRY_PAD_SHAPES
    with pytest.raises(ValueError, match="shape"):
        PinSpec("1", 0.0, 0.0, 1.0, 0.5, shape="trapezoid").validate()
    PinSpec("1", 0.0, 0.0, 1.0, 0.5, shape="rect").validate()


def test_g7_kicad_keeps_the_trapezoid_and_the_escaped_text_of_a_footprint():
    """@brief kicad-cli proof: `fp upgrade --force` keeps rect_delta and the control-character escapes."""
    cli = _kicad_cli()
    fp = Footprint(name="TRAP", attr="smd")
    fp.add_pad(Pad(number="1", pad_type="smd", shape="trapezoid", at=(0, 0), size=(2, 1), layers=["F.Cu", "F.Mask"],
                   rect_delta=(0, 0.5)))
    fp.add_pad(Pad(number="2", pad_type="smd", shape="rect", at=(3, -0.0), size=(2, 1), layers=["F.Cu", "F.Mask"]))
    fp.add_graphic(FpText(kind="user", text='tab\there "q" back\\slash', at=(0, 3), layer="F.Fab"))
    folder = Path(tempfile.mkdtemp(prefix="kicadgen-g7-"))
    try:
        library = folder / "Trap.pretty"
        library.mkdir()
        fp.save(str(library / footprint_file_name(fp.name)))
        result = _upgrade(cli, "fp", library)
        assert result.returncode == 0, result.stderr + result.stdout
        upgraded = (library / footprint_file_name(fp.name)).read_text(encoding="utf-8")
        assert "trapezoid" in upgraded and "(rect_delta 0 0.5)" in upgraded
        # KiCad reads `\t` back as a tab and rewrites it verbatim; quotes and
        # the backslash come back escaped: the text survived the round trip.
        assert 'tab\there \\"q\\" back\\\\slash' in upgraded
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ── G8: the glyph lexer decodes KiCad's escapes ───────────────────────────

@pytest.mark.parametrize("escaped, expected", [
    ('"a\\nb"', "a\nb"), ('"a\\rb"', "a\rb"), ('"a\\tb"', "a\tb"),
    ('"a\\\\b"', "a\\b"), ('"a\\"b"', 'a"b'), ('"a\\qb"', "aqb"),
])
def test_g8_parse_sexpr_decodes_the_escapes_kicad_writes(escaped, expected):
    """@brief Positive: `\\n` is a newline, an unknown escape stands for the character itself."""
    assert glyphs.parse_sexpr(f"(text {escaped})") == [["text", expected]]


def test_g8_quote_and_parse_round_trip_every_escaped_character():
    """@brief What `quote` writes, `parse_sexpr` reads back: one lexer for one writer."""
    original = 'line1\nline2\ttab "q" back\\slash µ'
    assert glyphs.parse_sexpr(f"(text {quote(original)})") == [["text", original]]


def test_g8_plain_text_is_read_unchanged():
    """@brief Negative: a string without escapes is returned as is."""
    assert glyphs.parse_sexpr('(name "GPIO_1" (x 1.5))') == [["name", "GPIO_1", ["x", 1.5]]]
