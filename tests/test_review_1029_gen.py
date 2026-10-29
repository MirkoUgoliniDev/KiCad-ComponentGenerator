"""@brief Review of 2026-10-29, generator findings G1-G8.

G1/G2: placed glyph records from a project are fully checked (kind, point
count, width, radius, rotation, terminals) and an unknown fill is normalised
like the stroke style; G3: pads and parametric footprints are bounded by the
coordinate range KiCad can represent; G4: build_package validates once; G5:
the castellated per-side checks no longer wait for the quadratic land
fitting, and side_of reads a cached index; G6: KiCad unescaping and markup
parsing are iterative, with the same output as the former recursion; G7:
host pad overrides follow one rule on every path; G8: negative castellated
margin and half-hole inset are refused.

Every rule has a positive case (the defect is refused or written right) and a
negative case (ordinary data is untouched), told apart for structural reasons.
"""
from __future__ import annotations

import random
import shutil
import subprocess

import pytest

from kicadgen.core import kicad_text
from kicadgen.core.footprint import KICAD_COORD_LIMIT_MM, require_kicad_reach
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators import castellated_module as cm
from kicadgen.generators import package as package_module
from kicadgen.generators import symbol_glyphs as glyphs
from kicadgen.generators.castellated_module import (AssumedSize, CastellatedModuleParams, CastellatedSide,
                                                    pad_positions)
from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.package import build_package, preview_grid_origin
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.generators.symbol import SymbolPin, serialize_symbol
from kicadgen.services.system_dependencies import find_kicad_cli


def _shape(primitive: dict, **record) -> dict:
    """A placed shape record carrying one primitive, as the symbol editor stores it."""
    base = {"id": "s1", "key": "shape:x", "label": "S", "source": "shape", "x": 0.0, "y": 0.0,
            "rotation": 0, "mirror": False, "terminals": [], "primitives": [primitive]}
    base.update(record)
    return base


RECTANGLE = {"kind": "rectangle", "points": [[-1, -1], [1, 1]], "radius": 0.0, "fill": "none",
             "width": 0.254, "style": "default"}
CIRCLE = {"kind": "circle", "points": [[0, 0]], "radius": 1.0, "fill": "none", "width": 0.254,
          "style": "default"}


# ── G1: primitive fields ──────────────────────────────────────────────────

def test_g1_an_unknown_fill_is_written_as_none_like_an_unknown_style():
    """@brief Positive: «bogus value» no longer reaches `(fill (type ...))`; KiCad refused the library."""
    record = _shape(dict(RECTANGLE, fill="bogus value", style="bogus"))
    glyphs.validate_instances([record])
    lines = glyphs.serialize_graphics([record])
    assert lines and "(fill (type none))" in lines[0] and "bogus" not in lines[0]


@pytest.mark.parametrize("fill", ["none", "outline", "background"])
def test_g1_the_kicad_fills_are_kept(fill):
    """@brief Negative: the three KiCad fill modes are written as stored."""
    lines = glyphs.serialize_graphics([_shape(dict(RECTANGLE, fill=fill))])
    assert f"(fill (type {fill}))" in lines[0]


@pytest.mark.parametrize("primitive", [
    dict(RECTANGLE, kind="hexagon"),
    dict(RECTANGLE, width=float("nan")),
    dict(RECTANGLE, width=-0.1),
    dict(RECTANGLE, width=None),
    dict(RECTANGLE, width=True),
    dict(RECTANGLE, radius=float("inf")),
    dict(CIRCLE, radius=0.0),
    dict(CIRCLE, radius=-1.0),
    dict(CIRCLE, points=[[0, 0], [1, 1]]),
    dict(RECTANGLE, points=[[0, 0]]),
    {"kind": "arc", "points": [[0, 0], [1, 1]]},
    {"kind": "bezier", "points": [[0, 0], [1, 1], [2, 2]]},
    {"kind": "polyline", "points": None},
])
def test_g1_a_malformed_primitive_is_a_value_error(primitive):
    """@brief Positive: every malformed primitive field is refused where the record is checked."""
    with pytest.raises(ValueError):
        glyphs.validate_instances([_shape(primitive)])


@pytest.mark.parametrize("primitive", [
    RECTANGLE,
    dict(RECTANGLE, width=0),
    CIRCLE,
    {"points": [[0, 0], [1, 1]]},  # a legacy primitive without kind is a polyline
    {"kind": "polyline", "points": [[0, 0], [1, 1], [2, 0]], "width": 0.0},
    {"kind": "arc", "points": [[0, 0], [1, 1], [2, 0]]},
    {"kind": "bezier", "points": [[0, 0], [1, 1], [2, 1], [3, 0]]},
])
def test_g1_a_well_formed_primitive_is_accepted(primitive):
    """@brief Negative: valid kinds with their own point count, width >= 0, positive radius."""
    record = _shape(primitive)
    assert glyphs.validate_instances([record]) == [record]


def test_g1_every_catalogue_glyph_passes_its_own_validation():
    """@brief Negative: a placed catalogue glyph (KiCad libraries or built-ins) is never refused."""
    records = [glyphs.instance_from_glyph(glyph, 0.0, 0.0) for glyph in glyphs.catalogue()]
    assert glyphs.validate_instances(records) == records


def test_g1_the_library_reader_skips_a_degenerate_circle():
    """@brief A zero-radius circle of a library is dropped, so no catalogue glyph is refused later."""
    form = glyphs.parse_sexpr('(symbol "X_0_1" (circle (center 0 0) (radius 0) (stroke (width 0)) '
                              '(fill (type none))) (circle (center 1 1) (radius 0.5) (stroke (width 0)) '
                              '(fill (type none))))')[0]
    circles = [p for p in glyphs.primitives_of(form) if p.kind == "circle"]
    assert [(p.points, p.radius) for p in circles] == [(((1.0, 1.0),), 0.5)]


def test_g1_kicad_loads_a_library_whose_record_had_an_unknown_fill(tmp_path):
    """@brief kicad-cli proof: the normalised library loads (exit 2 before the fix)."""
    cli = find_kicad_cli() or shutil.which("kicad-cli")
    if not cli:
        pytest.skip("KiCad CLI not installed")
    record = _shape(dict(RECTANGLE, fill="bogus value"))
    text = serialize_symbol("Sym", [SymbolPin("1", "A"), SymbolPin("2", "B", side="right")],
                            graphics=[record])
    target = tmp_path / "Fill.kicad_sym"
    target.write_text(text, encoding="utf-8", newline="\n")
    result = subprocess.run([cli, "sym", "upgrade", "--force", str(target)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# ── G2: rotation and terminals ────────────────────────────────────────────

@pytest.mark.parametrize("rotation", [None, "90", True, 45, 90.0, float("nan")])
def test_g2_a_rotation_that_is_not_an_int_multiple_of_90_is_a_value_error(rotation):
    """@brief Positive: None used to escape as TypeError from int(None)."""
    with pytest.raises(ValueError):
        glyphs.validate_instances([_shape(RECTANGLE, rotation=rotation)])


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 360, -90])
def test_g2_an_int_multiple_of_90_is_accepted(rotation):
    """@brief Negative: the rotations the editor writes."""
    assert glyphs.validate_instances([_shape(RECTANGLE, rotation=rotation)])


@pytest.mark.parametrize("terminals", [
    None, "abc", [{}], [1], [{"x": 0.0}], [{"x": 0.0, "y": "a"}],
    [{"x": 0.0, "y": float("nan")}], [{"x": 0.0, "y": 0.0, "angle": None}],
    [{"x": 0.0, "y": 0.0, "angle": 90.0}], [{"x": 0.0, "y": 0.0, "angle": True}],
    [{"x": 0.0, "y": 0.0, "length": float("inf")}], [{"x": 0.0, "y": 0.0, "length": None}],
])
def test_g2_malformed_terminals_are_a_value_error(terminals):
    """@brief Positive: [{}] used to raise KeyError and [1] TypeError in placed_terminals."""
    with pytest.raises(ValueError):
        glyphs.validate_instances([_shape(RECTANGLE, terminals=terminals)])


def test_g2_well_formed_terminals_are_accepted_and_placed():
    """@brief Negative: full and minimal (angle/length defaulted) terminals keep working."""
    record = _shape(RECTANGLE, rotation=90, terminals=[
        {"x": 1.0, "y": 0.0, "angle": 180, "length": 2.54, "name": "A", "number": "1"},
        {"x": -1.0, "y": 0.0}])
    assert glyphs.validate_instances([record]) == [record]
    placed = glyphs.placed_terminals(record)
    assert [(round(t.x, 6), round(t.y, 6), t.angle, t.length) for t in placed] == [
        (0.0, 1.0, 270, 2.54), (0.0, -1.0, 90, 2.54)]


# ── G3: KiCad's coordinate range ──────────────────────────────────────────

def _two_pad_geometry(x: float, size: float = 1.0, orientation: float = 0.0, y: float = 0.0) -> PackageGeometry:
    return PackageGeometry(name="FAR", family="custom", body_length_mm=4.0, body_width_mm=4.0,
                           orientation_deg=orientation,
                           pins=[PinSpec("1", -x, -y, size, size), PinSpec("2", x, y, size, size)])


def test_g3_a_pad_far_beyond_the_range_is_refused():
    """@brief Positive: pads at ±5000 mm were written and KiCad saturated them silently."""
    with pytest.raises(ValueError, match="KiCad"):
        build_package(_two_pad_geometry(5000.0))


def test_g3_the_copper_not_only_the_centre_is_bounded():
    """@brief Positive vs negative at the same centre: only the pad whose copper crosses the limit."""
    centre = KICAD_COORD_LIMIT_MM - 1.0
    build_package(_two_pad_geometry(centre, size=1.0))          # copper ends 0.5 mm inside
    with pytest.raises(ValueError, match="KiCad"):
        _two_pad_geometry(centre, size=4.0).validate()           # copper ends 1 mm outside


def test_g3_the_package_orientation_is_bounded_in_the_final_frame():
    """@brief Positive: in range in the source view, out of range once turned by 45 degrees."""
    geometry = _two_pad_geometry(1600.0, y=1600.0)
    geometry.validate()  # |x|, |y| = 1600.5 mm in the source frame
    with pytest.raises(ValueError, match="KiCad"):
        build_package(_two_pad_geometry(1600.0, y=1600.0, orientation=45.0))  # x = 2263 mm turned


def test_g3_the_pin_header_envelope_is_bounded():
    """@brief Positive: a grid reaching 3 m is refused; negative: the same pitch with fewer columns."""
    with pytest.raises(ValueError, match="KiCad"):
        PinHeaderParams(name="H", rows=1, cols=4, pitch=1000.0).validate()
    build_pin_header(PinHeaderParams(name="H", rows=1, cols=2, pitch=1000.0))


def test_g3_an_overflowing_header_envelope_keeps_the_non_finite_message():
    """@brief Positive: finite inputs summing to infinity are named as non-finite bounds, not «inf mm».

    Negative: a finite envelope beyond the range keeps the KiCad-range
    message; the two differ because one bound is not a number at all.
    """
    overflowing = PinHeaderParams(name="H", body_length=1e308, body_width=3.0, body_offset_x=1e308)
    with pytest.raises(ValueError, match="finiti") as caught:
        overflowing.validate()
    assert "inf" not in str(caught.value)
    with pytest.raises(ValueError, match="KiCad"):
        PinHeaderParams(name="H", body_length=3000.0, body_width=3.0).validate()


def test_g3_the_castellated_envelope_is_bounded():
    """@brief Positive: a 5 m body is refused; negative: the ordinary module."""
    with pytest.raises(ValueError, match="KiCad"):
        CastellatedModuleParams(name="M", body_length=5000.0, body_width=10.0,
                                sides=[CastellatedSide("left", 3, 2.54)]).validate()
    _castellated().validate()


def test_g3_require_kicad_reach_names_the_pad_or_the_footprint():
    """@brief The shared helper accepts the limit itself and refuses beyond or non-finite."""
    require_kicad_reach(KICAD_COORD_LIMIT_MM)
    with pytest.raises(ValueError, match="7"):
        require_kicad_reach(KICAD_COORD_LIMIT_MM + 1, "7")
    with pytest.raises(ValueError):
        require_kicad_reach(float("nan"))


# ── G4: build_package validates once ──────────────────────────────────────

def test_g4_build_package_validates_the_geometry_once(monkeypatch):
    """@brief Positive: one validation per build (there were three, each O(n^2))."""
    calls = []
    original = PackageGeometry.validate
    monkeypatch.setattr(PackageGeometry, "validate", lambda self: (calls.append(1), original(self))[1])
    pins = [PinSpec(str(i + 1), i * 1.27, 0.0, 0.6, 1.5) for i in range(8)]
    geometry = PackageGeometry(name="ROW", family="custom", pins=pins, origin="pin1")
    footprint = build_package(geometry)
    assert len(calls) == 1
    calls.clear()
    # Negative: the public preview origin still validates and agrees with the build.
    assert preview_grid_origin(geometry) == package_module._first_pin_origin(geometry.pins_seen_from_top())
    assert calls
    assert footprint.pads[0].at == preview_grid_origin(geometry)


def test_g4_an_invalid_geometry_is_still_refused_by_build_package():
    """@brief Negative: dropping the explicit validate() kept the refusal (overlapping pads)."""
    geometry = PackageGeometry(name="BAD", family="custom",
                               pins=[PinSpec("1", 0.0, 0.0, 1.0, 1.0), PinSpec("2", 0.5, 0.0, 1.0, 1.0)])
    with pytest.raises(ValueError):
        build_package(geometry)


# ── G5: castellated checks in a cheap order, cached side index ────────────

def _castellated(**changes) -> CastellatedModuleParams:
    params = dict(name="M", body_length=20.0, body_width=10.0,
                  sides=[CastellatedSide("left", 3, 2.54), CastellatedSide("right", 3, 2.54, first_pin=4)])
    params.update(changes)
    return CastellatedModuleParams(**params)


def test_g5_a_span_refusal_does_not_run_the_land_fitting(monkeypatch):
    """@brief Positive: 2 x 2000 contacts on a 20 mm body are refused before the quadratic fitting."""
    calls = []
    original = CastellatedModuleParams._shorten_assumed_lengths
    monkeypatch.setattr(CastellatedModuleParams, "_shorten_assumed_lengths",
                        lambda self, *a: (calls.append(1), original(self, *a))[1])
    params = _castellated(body_length=20.0, body_width=20.0,
                          sides=[CastellatedSide("left", 2000, 1.27), CastellatedSide("right", 2000, 1.27,
                                                                                         first_pin=2001)])
    with pytest.raises(ValueError, match="2000 pad occupano"):
        params.validate()
    assert calls == []


def test_g5_assumed_lengths_are_still_shortened_for_facing_rows():
    """@brief Negative: a narrow body whose facing assumed lands collide is still fitted, then accepted."""
    params = _castellated(body_length=4.0, sides=[
        CastellatedSide("left", 3, 2.54, contact_style=cm.CONTACT_HALF_HOLE),
        CastellatedSide("right", 3, 2.54, first_pin=4, contact_style=cm.CONTACT_HALF_HOLE)])
    params.validate()
    sources = {size.length_source for size in params.pad_sizes()}
    assert cm.PAD_ESTIMATE_CLEARANCE in sources


def _linear_side_of(params, placement):
    """The former side_of: a scan of every side, numbers rebuilt each time."""
    fallback = None
    for side in params.sides:
        if side.edge != placement.edge:
            continue
        if fallback is None:
            fallback = side
        if placement.number in side.numbers():
            return side
    return fallback


def test_g5_the_cached_side_index_answers_like_the_linear_scan():
    """@brief Same side for every placement, two sides on one edge, explicit and reversed numbers."""
    params = _castellated(body_width=20.0, sides=[
        CastellatedSide("left", 2, 2.54, offset=-5.0), CastellatedSide("left", 2, 2.54, first_pin=3, offset=5.0,
                                                                       reverse=True),
        CastellatedSide("right", 3, 2.54, pin_numbers=["A", "B", "C"])])
    placements = list(pad_positions(params))
    assert all(params.side_of(p) is _linear_side_of(params, p) for p in placements)
    stray = placements[0]._replace(number="99")
    assert params.side_of(stray) is _linear_side_of(params, stray) is params.sides[0]
    assert params.side_of(stray._replace(edge="top")) is None


def test_g5_the_side_index_follows_an_edit_in_place():
    """@brief Negative for the cache: editing a side's numbering rebuilds the index."""
    params = _castellated()
    placement = next(p for p in pad_positions(params) if p.edge == "right")
    assert params.side_of(placement._replace(number="5")) is params.sides[1]
    params.sides[1].pin_numbers = ["X", "Y", "Z"]
    assert params.side_of(placement._replace(number="X")) is params.sides[1]
    params.sides[0].first_pin = 50
    left = next(p for p in pad_positions(params) if p.edge == "left")
    assert left.number == "50" and params.side_of(left) is params.sides[0]


# ── G6: iterative unescape and markup parsing ─────────────────────────────

def _reference_unescape(text: str) -> str:
    """The recursive kicad_unescape before 2026-10-29, verbatim, as the oracle."""
    if len(text) <= 2:
        return text
    out = []
    previous = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char != "{":
            out.append(char)
            previous = char
            index += 1
            continue
        depth, end = 1, index + 1
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        token = text[index + 1:end]
        if end >= len(text):
            out.append("{" + _reference_unescape(token))
            break
        if previous in ("$", "~", "^", "_") or token not in kicad_text.KICAD_ESCAPES:
            out.append("{" + _reference_unescape(token) + "}")
        else:
            out.append(kicad_text.KICAD_ESCAPES[token])
        previous = "}"
        index = end + 1
    return "".join(out)


def _reference_markup(text: str, index: int, memo: dict):
    """The recursive _parse_markup before 2026-10-29, verbatim, as the oracle."""
    if index in memo:
        return memo[index]
    result = None
    if text[index:index + 1] in ("~", "^", "_") and text[index + 1:index + 2] == "{":
        children = []
        literal = []
        position = index + 2
        while position < len(text):
            if text[position] == "}":
                if literal:
                    children.append("".join(literal))
                result = ((text[index], tuple(children)), position + 1)
                break
            nested = _reference_markup(text, position, memo)
            if nested is not None:
                if literal:
                    children.append("".join(literal))
                    literal = []
                children.append(nested[0])
                position = nested[1]
                continue
            escape_end = kicad_text._escape_sequence_end(text, position)
            if escape_end > 0:
                literal.append(text[position:escape_end])
                position = escape_end
                continue
            literal.append(text[position])
            position += 1
    memo[index] = result
    return result


def _reference_tree(text: str) -> tuple:
    """parse_kicad_markup driven by the recursive oracle."""
    memo: dict = {}
    children, literal, index = [], [], 0
    while index < len(text):
        markup = _reference_markup(text, index, memo)
        if markup is not None:
            if literal:
                children.append("".join(literal))
                literal = []
            children.append(markup[0])
            index = markup[1]
            continue
        url_end = kicad_text._url_end(text, index)
        if url_end > 0:
            literal.append(text[index:url_end])
            index = url_end
            continue
        literal.append(text[index])
        index += 1
    if literal:
        children.append("".join(literal))
    return tuple(children)


NORMAL_TEXTS = ["", "R", "{}", "{a", "a}", "{lt}", "{gt}x", "N{slash}A", "${REFERENCE}", "~{RESET}",
                "~{OE}/~{WE}", "V_{CC}", "x^{2}", "~{a_{b}c}", "{brace}", "{{slash}}", "${a{slash}}",
                "~{unclosed", "_{^{~{}}}", "http://a.b/{slash} ~{x}", "{a_b}", "~{{a_b}}", "{dblquote}q",
                "{unknown}", "{{{", "}}}", "a{b{c}d}e", "µF 3.3Ω ~{CS}"]


def _random_texts(count: int) -> list:
    rng = random.Random(20261029)
    alphabet = list("{}~^_$ab/ ") + ["{slash}", "{lt}", "{brace}", "~{", "{gt}", "http://x ", "{a_b}", "_{"]
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16))) for _ in range(count)]


@pytest.mark.parametrize("text", NORMAL_TEXTS)
def test_g6_the_iterative_functions_match_the_former_recursion(text):
    """@brief Negative: identical output on normal and edge texts."""
    assert kicad_text.kicad_unescape(text) == _reference_unescape(text)
    assert kicad_text.parse_kicad_markup(text) == _reference_tree(text)


def test_g6_the_iterative_functions_match_the_former_recursion_on_a_random_corpus():
    """@brief Negative: 20000 random strings of braces, markups, escapes and URLs."""
    for text in _random_texts(20000):
        assert kicad_text.kicad_unescape(text) == _reference_unescape(text), text
        assert kicad_text.parse_kicad_markup(text) == _reference_tree(text), text


DEEP_TEXTS = {"open braces": "{" * 6000, "closed braces": "{a" * 6000 + "}" * 6000,
              "open markups": "~{" * 6000, "closed markups": "~{" * 6000 + "}" * 6000,
              "open subscripts": "_{x" * 6000, "closed scripts": "^{_{" * 3000 + "}" * 6000}


@pytest.mark.parametrize("text", list(DEEP_TEXTS.values()), ids=list(DEEP_TEXTS))
def test_g6_deep_nesting_no_longer_raises_recursion_error(text):
    """@brief Positive: the recursion used to fail at a few thousand nested braces."""
    kicad_text.kicad_unescape(text)
    kicad_text.parse_kicad_markup(text)
    kicad_text.kicad_text_layout(text)


def _reference_layout(text: str) -> kicad_text.KicadTextLayout:
    """kicad_text_layout with the recursive walk it had before 2026-10-29, as the oracle."""
    runs, bars = [], []
    units = {"plain": 0, "script": 0}

    def cursor():
        return (units["plain"] + kicad_text.KICAD_SUPER_SUB_SIZE * units["script"]
                if units["script"] else units["plain"])

    def walk(children, script):
        for child in children:
            if isinstance(child, str):
                count = kicad_text._string_units(child)
                scale = kicad_text.KICAD_SUPER_SUB_SIZE if script else 1.0
                rise = {"^": kicad_text.KICAD_SUPERSCRIPT_RISE, "_": kicad_text.KICAD_SUBSCRIPT_RISE}.get(script, 0.0)
                start = cursor()
                units["script" if script else "plain"] += count
                previous = runs[-1] if runs else None
                if previous is not None and previous.scale == scale and previous.rise == rise:
                    runs[-1] = kicad_text.KicadTextRun(previous.text + child, previous.start,
                                                       cursor() - previous.start, scale, rise)
                else:
                    runs.append(kicad_text.KicadTextRun(child, start, cursor() - start, scale, rise))
                continue
            kind, nested = child
            if not nested:
                continue
            inner = "_" if "_" in (kind, script) else ("^" if "^" in (kind, script) else None)
            start = cursor()
            walk(nested, inner)
            if kind == "~":
                trim = kicad_text.KICAD_OVERBAR_TRIM * 21
                bars.append(kicad_text.KicadOverbar(start + trim, cursor() - trim, kicad_text.KICAD_OVERBAR_HEIGHT))

    walk(_reference_tree(text), None)
    return kicad_text.KicadTextLayout(tuple(runs), tuple(bars), cursor())


def test_g6_the_iterative_layout_matches_the_former_recursive_walk():
    """@brief Negative: runs, overbars and advance unchanged on normal, edge and random texts."""
    for text in NORMAL_TEXTS + ["~{A}~{B}", "x^{2_{i}}", "~{~{a}b}", "W1_{x}^{y}"] + _random_texts(5000):
        assert repr(kicad_text.kicad_text_layout(text)) == repr(_reference_layout(text)), text


def test_g6_deep_nesting_keeps_the_structure():
    """@brief Positive: a deeply nested closed markup still yields the whole tree."""
    depth = 4000
    tree = kicad_text.parse_kicad_markup("~{" * depth + "}" * depth)
    for _ in range(depth - 1):
        (node,) = tree
        assert node[0] == "~"
        tree = node[1]
    assert tree == (("~", ()),)
    assert kicad_text.kicad_unescape("{a" * depth + "}" * depth) == "{a" * depth + "}" * depth


# ── G7: host pad overrides ────────────────────────────────────────────────

def _tht_geometry() -> PackageGeometry:
    return PackageGeometry(name="THT", family="custom", pins=[
        PinSpec("1", 0.0, 0.0, 1.7, 1.7, pad_type="thru_hole", shape="circle", drill_mm=1.0),
        PinSpec("2", 2.54, 0.0, 1.7, 1.7, pad_type="thru_hole", shape="circle", drill_mm=1.0)])


def _tht_castellated() -> CastellatedModuleParams:
    return _castellated(sides=[CastellatedSide("left", 3, 2.54, contact_style=cm.CONTACT_THROUGH_HOLE),
                               CastellatedSide("right", 3, 2.54, first_pin=4,
                                               contact_style=cm.CONTACT_THROUGH_HOLE)], row_spacing=17.0)


@pytest.mark.parametrize("override", [{"1": {"size": None, "shape": "circle"}}, {"1": {"size": 2.0}},
                                      {"1": {"size": 0.5, "shape": "rect"}}, {"1": None},
                                      {"9": {"size": 2.0, "shape": "rect"}}, ["1"]])
@pytest.mark.parametrize("module", ["package", "castellated"])
def test_g7_a_malformed_override_is_a_value_error_on_every_path(module, override):
    """@brief Positive: {"size": None} used to escape as TypeError on the package/castellated paths."""
    params = _tht_geometry() if module == "package" else _tht_castellated()
    with pytest.raises(ValueError):
        header_footprint_params(params, pad_diameter=1.7, drill=1.0, pad_overrides=override)


@pytest.mark.parametrize("module", ["package", "castellated"])
@pytest.mark.parametrize("key", ["1", 1])
def test_g7_a_valid_override_is_applied_by_the_text_key(module, key):
    """@brief Negative: a valid override still applies, int and str spellings alike."""
    params = _tht_geometry() if module == "package" else _tht_castellated()
    result = header_footprint_params(params, pad_diameter=1.7, drill=1.0,
                                     pad_overrides={key: {"size": 2.0, "shape": "rect"}})
    pin = next(p for p in result.pins if p.number == "1")
    assert (pin.pad_length_mm, pin.shape) == (2.0, "rect")
    assert all(p.pad_length_mm == 1.7 for p in result.pins if p.number != "1")


# ── G8: castellated margin and half-hole inset ────────────────────────────

@pytest.mark.parametrize("changes", [{"margin": -0.1}, {"hole_inset": -0.1}])
def test_g8_a_negative_margin_or_inset_is_refused(changes):
    """@brief Positive: both were accepted; PinHeaderParams already refused a negative margin."""
    with pytest.raises(ValueError):
        _castellated(**changes).validate()


@pytest.mark.parametrize("changes", [{"margin": 0.0}, {"hole_inset": 0.0}, {"hole_inset": None}])
def test_g8_zero_margin_and_inset_are_accepted(changes):
    """@brief Negative: zero is a legitimate courtyard margin and a half-hole centred on the edge."""
    _castellated(**changes).validate()
