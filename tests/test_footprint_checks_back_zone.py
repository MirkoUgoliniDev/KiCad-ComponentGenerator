"""@brief Geometric checks with back layers and keepout zones (`services.footprint_checks`, plan F1/F4, 2026-11-09).

Silkscreen rules per side (F.SilkS against the front copper, B.SilkS against
the back copper), the stroke-width rule on both silkscreens, the pin-1 rule
kept front-only (KLC F5.1), keepout zones that are neither ink nor body, the
pad-in-keepout warning (KiCad's DRC epsilon, a 0.5 um length; copper sides by
pad type and layer list) and the silkscreen text-size warning (KiCad's DRC
board defaults), whose positive and negative cases are checked against
KiCad's own DRC (`kicad-cli pcb drc`, tests below when installed).
"""
import json
import math
import re
import subprocess

import pytest

from kicadgen.core.footprint import (FpCircle, FpDimension, FpLine, FpRect, FpText, FpTextBox, FpZone, Footprint,
                                     Pad, ZONE_KEEPOUT_DEFAULTS)
from kicadgen.services import footprint_checks as fc
from kicadgen.services.system_dependencies import find_kicad_cli

THT = ["*.Cu", "*.Mask"]
SMD_FRONT = ["F.Cu", "F.Paste", "F.Mask"]
SMD_BACK = ["B.Cu", "B.Paste", "B.Mask"]
SQUARE = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]


def _fp(*pads, graphics=()):
    fp = Footprint(name="T")
    for pad in pads:
        fp.add_pad(pad)
    for graphic in graphics:
        fp.add_graphic(graphic)
    return fp


def _codes(findings):
    return [finding.code for finding in findings]


def _over_pad(layer, width=0.12):
    """A silkscreen line straight across the pad at the origin."""
    return FpLine((-2.0, 0.0), (2.0, 0.0), layer, width)


# ── pad copper sides ───────────────────────────────────────────────────────

@pytest.mark.parametrize("pad, sides", [
    (Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT), {"F.Cu"}),
    (Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_BACK), {"B.Cu"}),
    (Pad("1", "thru_hole", "circle", (0, 0), (1.6, 1.6), THT, drill=0.8), {"F.Cu", "B.Cu"}),
    (Pad("", "np_thru_hole", "circle", (0, 0), (1, 1), THT, drill=1.0), {"F.Cu", "B.Cu"}),
    (Pad("1", "smd", "rect", (0, 0), (1, 1), ["F&B.Cu"]), {"F.Cu", "B.Cu"}),
    (Pad("1", "smd", "rect", (0, 0), (1, 1), ["In1.Cu"]), set()),            # inner copper only
], ids=["smd-front", "smd-back", "pth", "npth-both", "legacy-both", "inner-only"])
def test_pad_copper_sides_follow_the_layers(pad, sides):
    assert fc.pad_copper_sides(pad) == sides


def test_a_plated_barrel_has_both_sides_and_a_mechanical_hole_only_its_named_layers():
    """Same layer list, different pad type: the plating, not the list, gives a PTH its back copper (KiCad DRC agrees)."""
    front_only = ["F.Cu", "F.Mask"]
    pth = Pad("1", "thru_hole", "circle", (0, 0), (1.6, 1.6), front_only, drill=0.8)
    npth = Pad("", "np_thru_hole", "circle", (0, 0), (1.6, 1.6), front_only, drill=0.8)
    assert fc.pad_copper_sides(pth) == {"F.Cu", "B.Cu"}
    assert fc.pad_copper_sides(npth) == {"F.Cu"}


@pytest.mark.parametrize("layers", [["F.Mask"], ["F.Paste", "F.Mask"], ["*.Mask"]],
                         ids=["mask", "paste-mask", "both-masks"])
def test_an_aperture_naming_no_copper_has_no_copper_side(layers):
    """A paste or mask opening is not copper: no side (the old fallback gave it the front)."""
    assert fc.pad_copper_sides(Pad("1", "smd", "rect", (0, 0), (1, 1), layers)) == set()
    assert fc.pad_copper_sides(Pad("", "np_thru_hole", "circle", (0, 0), (1, 1), layers, drill=1.0)) == set()
    # The same pad with a copper layer named does have that side.
    assert fc.pad_copper_sides(Pad("1", "smd", "rect", (0, 0), (1, 1), ["F.Cu", *layers])) == {"F.Cu"}


# ── silkscreen per side ────────────────────────────────────────────────────

def test_back_silk_over_a_through_hole_pad_is_a_finding():
    pad = Pad("1", "thru_hole", "circle", (0, 0), (1.6, 1.6), THT, drill=0.8)
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[_over_pad("B.SilkS")]))) == ["silk_clearance"]


def test_back_silk_over_a_front_only_pad_is_not_a_finding():
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    assert fc.check_silk_clearance(_fp(pad, graphics=[_over_pad("B.SilkS")])) == []
    # The same ink on the front silkscreen does cover that pad.
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[_over_pad("F.SilkS")]))) == ["silk_clearance"]


def test_front_silk_over_a_back_only_pad_is_not_a_finding_and_back_silk_is():
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_BACK)
    assert fc.check_silk_clearance(_fp(pad, graphics=[_over_pad("F.SilkS")])) == []
    assert _codes(fc.check_silk_clearance(_fp(pad, graphics=[_over_pad("B.SilkS")]))) == ["silk_clearance"]


def test_back_fabrication_ink_is_never_a_silk_finding():
    pad = Pad("1", "thru_hole", "circle", (0, 0), (1.6, 1.6), THT, drill=0.8)
    assert fc.check_silk_clearance(_fp(pad, graphics=[_over_pad("B.Fab")])) == []


def test_thin_back_silk_stroke_is_flagged_and_thin_back_fab_is_not():
    far = Pad("1", "smd", "rect", (20, 20), (1, 1), SMD_FRONT)
    findings = fc.check_silk_stroke_width(_fp(far, graphics=[_over_pad("B.SilkS", 0.05)]))
    assert _codes(findings) == ["silk_stroke_width"] and findings[0].graphics == (0,)
    assert fc.check_silk_stroke_width(_fp(far, graphics=[_over_pad("B.Fab", 0.05)])) == []


# ── pin 1 stays front-only (KLC F5.1) ──────────────────────────────────────

def test_a_back_circle_beside_pin_one_does_not_mark_it_and_a_front_one_does():
    pads = [Pad(str(n), "smd", "rect", (2.0 * n, 0), (1, 1), SMD_FRONT) for n in (1, 2, 3)]
    back = FpCircle((2.0, -1.5), 0.2, "B.SilkS")
    assert _codes(fc.check_pin_one_marker(_fp(*pads, graphics=[back]))) == ["pin_one_marker"]
    front = FpCircle((2.0, -1.5), 0.2, "F.SilkS")
    assert fc.check_pin_one_marker(_fp(*pads, graphics=[front])) == []


# ── zones are neither ink nor body ─────────────────────────────────────────

def test_a_zone_that_allows_pads_over_pads_gives_no_finding_of_any_rule():
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    allowing = {**ZONE_KEEPOUT_DEFAULTS, "pads": False}
    fp = _fp(pad, graphics=[FpZone(SQUARE, ("F.Cu", "B.Cu"), allowing),
                            FpRect((-1.5, -1.5), (1.5, 1.5), "F.CrtYd", 0.05),
                            FpCircle((0.0, -0.95), 0.05, "F.Fab", 0.1)])  # the pin-1 mark the rule wants
    assert fc.check_footprint_geometry(fp) == []


def test_the_courtyard_need_not_enclose_a_zone_but_must_enclose_a_fab_outline_there():
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    courtyard = FpRect((-1.0, -1.0), (1.0, 1.0), "F.CrtYd", 0.05)
    far = [(10.0, 10.0), (12.0, 10.0), (12.0, 12.0)]
    zone = FpZone(far, ("F.Cu",))
    assert fc.check_courtyard_enclosure(_fp(pad, graphics=[courtyard, zone])) == []
    fab = FpLine(far[0], far[1], "F.Fab", 0.1)
    assert _codes(fc.check_courtyard_enclosure(_fp(pad, graphics=[courtyard, fab]))) == ["courtyard_enclosure"]


# ── pad in keepout ─────────────────────────────────────────────────────────

def test_a_front_pad_in_a_front_keepout_forbidding_pads_is_a_warning():
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    findings = fc.check_pad_in_keepout(_fp(pad, graphics=[FpZone(SQUARE, ("F.Cu",))]))
    assert _codes(findings) == ["pad_in_keepout"] and findings[0].severity == "warning"
    assert findings[0].pads == (0,) and findings[0].graphics == (0,)
    assert "KLC" in findings[0].message and fc.geometry_hint("pad_in_keepout")


@pytest.mark.parametrize("zone", [
    FpZone(SQUARE, ("B.Cu",)),                                            # the pad has no back copper
    FpZone(SQUARE, ("F.Cu",), {**ZONE_KEEPOUT_DEFAULTS, "pads": False}),  # pads allowed
    FpZone([(5, 5), (7, 5), (7, 7)], ("F.Cu",)),                          # far away
    FpZone([(0.5, -1), (2, -1), (2, 1), (0.5, 1)], ("F.Cu",)),            # only touches the pad edge
], ids=["other-side", "pads-allowed", "far", "touching"])
def test_a_pad_the_keepout_does_not_forbid_is_not_a_warning(zone):
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    assert fc.check_pad_in_keepout(_fp(pad, graphics=[zone])) == []


def test_through_hole_and_mechanical_pads_count_on_both_sides():
    tht = Pad("1", "thru_hole", "circle", (0, 0), (1.6, 1.6), THT, drill=0.8)
    hole = Pad("", "np_thru_hole", "circle", (0, 0), (1.0, 1.0), THT, drill=1.0)
    for pad in (tht, hole):
        assert _codes(fc.check_pad_in_keepout(_fp(pad, graphics=[FpZone(SQUARE, ("B.Cu",))]))) == ["pad_in_keepout"]


def test_a_mechanical_hole_without_copper_layers_is_in_no_keepout():
    """An NPTH written with only «*.Mask» names no copper: KiCad's DRC reports it in no keepout."""
    hole = Pad("", "np_thru_hole", "circle", (0, 0), (1.0, 1.0), ["*.Mask"], drill=1.0)
    for layers in (("F.Cu",), ("B.Cu",), ("F.Cu", "B.Cu")):
        assert fc.check_pad_in_keepout(_fp(hole, graphics=[FpZone(SQUARE, layers)])) == []


def _sliver(depth):
    """A 1 mm SMD pad at the origin and a front zone whose left side lies `depth` mm inside the pad's right edge."""
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    return _fp(pad, graphics=[FpZone([(0.5 - depth, -1), (2, -1), (2, 1), (0.5 - depth, 1)], ("F.Cu",))])


@pytest.mark.parametrize("depth", [0.0006, 0.001, 0.01])
def test_copper_crossing_the_border_by_the_drc_epsilon_or_more_is_a_warning(depth):
    assert _codes(fc.check_pad_in_keepout(_sliver(depth))) == ["pad_in_keepout"]


@pytest.mark.parametrize("depth", [-0.001, 0.0, 0.0001, 0.0003, 0.0004])
def test_copper_crossing_the_border_by_less_than_the_drc_epsilon_is_not(depth):
    """KiCad deflates the rule area by its DRC epsilon, a LENGTH (0.5 um), before testing: a thinner sliver is none."""
    assert fc.check_pad_in_keepout(_sliver(depth)) == []


def _round_pad_and_slanted_zone(depth, radius=5.0, theta=math.pi / 160):
    """A round pad at the origin and zone corners whose straight side, normal at `theta`, lies `depth` mm inside the rim."""
    nx, ny = math.cos(theta), math.sin(theta)
    s = radius - depth
    px, py = nx * s, ny * s
    tx, ty = -ny, nx
    corners = [(px + tx * 4, py + ty * 4), (px + tx * 4 + nx * 3, py + ty * 4 + ny * 3),
               (px - tx * 4 + nx * 3, py - ty * 4 + ny * 3), (px - tx * 4, py - ty * 4)]
    return Pad("1", "smd", "circle", (0, 0), (2 * radius, 2 * radius), SMD_FRONT), corners


def test_a_round_pad_is_measured_on_its_true_rim_not_a_sampled_one():
    """3 um of overlap between two chord samples of a 10 mm pad: an inscribed outline misses it, the true rim does not."""
    pad, corners = _round_pad_and_slanted_zone(0.003)
    assert fc.copper_meets_keepout(fc.pad_copper(pad), corners)
    assert _codes(fc.check_pad_in_keepout(_fp(pad, graphics=[FpZone(corners, ("F.Cu",))]))) == ["pad_in_keepout"]
    # The same side moved 3 um OUTSIDE the rim is clear: the rule follows the copper, not a margin.
    pad, corners = _round_pad_and_slanted_zone(-0.003)
    assert not fc.copper_meets_keepout(fc.pad_copper(pad), corners)


def test_a_pad_across_a_crossed_outline_is_found_in_its_lobes():
    """A bow tie the validation refuses, built directly: the pad over the crossing lies in both lobes."""
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    bow = FpZone([(-2, -2), (2, 2), (2, -2), (-2, 2)], ("F.Cu",))
    assert _codes(fc.check_pad_in_keepout(_fp(pad, graphics=[bow]))) == ["pad_in_keepout"]
    beside = Pad("1", "smd", "rect", (0, 3.5), (1, 1), SMD_FRONT)    # beyond the lobes' tips
    assert fc.check_pad_in_keepout(_fp(beside, graphics=[bow])) == []


def test_a_rounded_pad_beside_a_concave_zone_is_measured_on_its_copper():
    """An L-shaped zone wraps a round pad without covering it; the same pad moved into the arm is covered."""
    zone = FpZone([(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)], ("F.Cu",))
    outside = Pad("1", "smd", "circle", (4.0, 4.0), (2.0, 2.0), SMD_FRONT)
    assert fc.check_pad_in_keepout(_fp(outside, graphics=[zone])) == []
    inside = Pad("1", "smd", "circle", (4.0, 1.0), (1.0, 1.0), SMD_FRONT)
    assert _codes(fc.check_pad_in_keepout(_fp(inside, graphics=[zone]))) == ["pad_in_keepout"]
    assert fc.copper_meets_keepout(fc.pad_copper(inside), zone.points)
    assert not fc.copper_meets_keepout(fc.pad_copper(outside), zone.points)


def _border_pads():
    """Pads straddling the border of the L zone of the test above (inside, outside, across, on a corner)."""
    centres = [(-0.3, 3.0), (0.4, 3.0), (6.4, 1.0), (5.7, 1.0), (2.2, 4.0), (1.9, 4.0), (4.0, 2.4),
               (2.0, 2.0), (6.0, 0.0), (0.0, 6.2), (3.0, 3.0), (1.0, 7.0)]
    return [Pad(str(i + 1), "smd", "circle" if i % 2 else "rect", centre, (0.6, 0.6), SMD_FRONT)
            for i, centre in enumerate(centres)]


def test_the_zone_geometry_is_prepared_once_per_zone_not_per_pad(monkeypatch):
    """@brief Structural (no timing): the zone's carriers are built once per zone however many pads are tested.

    The carrier crossings are quadratic in the zone's corners; building them
    per pad made the rule quadratic per pad (verification review
    2026-11-09). Positive: one zone, twelve pads, one build. Negative: two
    zones, two builds (the preparation is per zone, not one global cache).
    """
    built = []
    original = fc._zone_carriers
    monkeypatch.setattr(fc, "_zone_carriers", lambda corners, epsilon: built.append(1) or original(corners, epsilon))
    zone = FpZone([(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)], ("F.Cu",))
    fc.check_pad_in_keepout(_fp(*_border_pads(), graphics=[zone]))
    assert len(built) == 1
    built.clear()
    other = FpZone([(10, 10), (12, 10), (12, 12)], ("F.Cu",))
    fc.check_pad_in_keepout(_fp(*_border_pads(), Pad("99", "smd", "rect", (11.5, 10.5), (1, 1), SMD_FRONT),
                                graphics=[zone, other]))
    assert len(built) == 2


def test_a_prepared_outline_answers_as_the_plain_corners():
    """@brief The shared preparation changes no answer: every border pad gets the same verdict either way."""
    corners = [(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)]
    outline = fc.KeepoutOutline(corners)
    verdicts = []
    for pad in _border_pads():
        plain = fc.copper_meets_keepout(fc.pad_copper(pad), corners)
        assert fc.copper_meets_keepout(fc.pad_copper(pad), outline) == plain, pad.at
        verdicts.append(plain)
    assert True in verdicts and False in verdicts, "the set has pads on both sides of the rule"
    built = outline._crossings
    assert built is not None, "a border pad needed the carrier crossings"
    fc.copper_meets_keepout(fc.pad_copper(_border_pads()[1]), outline)
    assert outline._crossings is built, "built once, reused by the next pad"
    # A different epsilon is not answered with the prepared one.
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    sliver = [(0.5 - 0.0006, -1), (2, -1), (2, 1), (0.5 - 0.0006, 1)]
    assert fc.copper_meets_keepout(fc.pad_copper(pad), fc.KeepoutOutline(sliver))
    assert not fc.copper_meets_keepout(fc.pad_copper(pad), fc.KeepoutOutline(sliver), epsilon=0.001)


# ── silkscreen text size (KiCad DRC board defaults) ────────────────────────

def test_kicad_silk_text_minimums_come_from_its_board_defaults():
    # BOARD_DESIGN_SETTINGS: DEFAULT_SILK_TEXT_SIZE 1.0 x 0.8 and DEFAULT_SILK_TEXT_WIDTH 0.1 x 0.8.
    assert fc.SILK_TEXT_MIN_HEIGHT_MM == pytest.approx(0.8) and fc.SILK_TEXT_MIN_THICKNESS_MM == pytest.approx(0.08)


@pytest.mark.parametrize("item", [
    FpText("user", "A", (0, 0), "F.SilkS", 0.7, 0.1),                 # too low
    FpText("user", "A", (0, 0), "F.SilkS", 1.0, 0.07),                # too thin
    FpText("user", "A", (0, 0), "B.SilkS", 0.5, 0.075),               # the back silkscreen prints too
    FpTextBox((0, 0), (5, 3), "A", "F.SilkS", 0.6, 0.09),             # a text box's text
    FpDimension((0, 0), (4, 0), 1.0, "B.SilkS", text_size=0.5),       # a dimension's value
], ids=["low", "thin", "back", "textbox", "dimension"])
def test_a_small_silkscreen_text_is_a_warning(item):
    findings = fc.check_silk_text_size(_fp(graphics=[item]))
    assert _codes(findings) == ["silk_text_size"] and findings[0].graphics == (0,)
    assert findings[0].severity == "warning" and "KLC" in findings[0].message and fc.geometry_hint("silk_text_size")
    assert "silk_text_size" in _codes(fc.check_footprint_geometry(_fp(graphics=[item])))


@pytest.mark.parametrize("item", [
    FpText("user", "A", (0, 0), "F.SilkS", 0.8, 0.08),                # exactly the minimums
    FpText("user", "A", (0, 0), "F.Fab", 0.5, 0.075),                 # same small text, not printed
    FpDimension((0, 0), (4, 0), 0.0, "F.SilkS", text_size=0.5, dtype="center"),  # a centre mark has no text
    FpLine((0, 0), (1, 0), "F.SilkS", 0.12),                          # a stroke, not a text
], ids=["at-minimum", "fab-layer", "centre-mark", "line"])
def test_a_text_kicad_does_not_flag_is_not_a_warning(item):
    assert fc.check_silk_text_size(_fp(graphics=[item])) == []


def test_the_pen_is_measured_as_kicad_draws_it():
    """A 0.4 mm text written with a 0.15 mm pen is drawn with 0.25 x 0.4 = 0.10 mm (KiCad's clamp): low, not thin."""
    message = fc.check_silk_text_size(_fp(graphics=[FpText("user", "A", (0, 0), "F.SilkS", 0.4, 0.15)]))[0].message
    assert "0.100 mm" in message
    clamped_thin = FpText("user", "A", (0, 0), "F.SilkS", 0.3, 0.15)    # 0.075 mm drawn: below 0.08 too
    assert "0.075 mm" in fc.check_silk_text_size(_fp(graphics=[clamped_thin]))[0].message


def test_the_full_check_list_runs_the_keepout_rule():
    pad = Pad("1", "smd", "rect", (0, 0), (1, 1), SMD_FRONT)
    assert "pad_in_keepout" in _codes(fc.check_footprint_geometry(_fp(pad, graphics=[FpZone(SQUARE, ("F.Cu",))])))


#: The two-layer board of KiCad 10 (ordinals as `kicad_library.PREVIEW_BOARD_LAYERS`), enough for a DRC run.
_BOARD_LAYERS = ('(0 "F.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user "F.Silkscreen") '
                 '(7 "B.SilkS" user "B.Silkscreen") (1 "F.Mask" user) (3 "B.Mask" user) (13 "F.Paste" user) '
                 '(15 "B.Paste" user) (25 "Edge.Cuts" user) (31 "F.CrtYd" user "F.Courtyard") '
                 '(29 "B.CrtYd" user "B.Courtyard") (35 "F.Fab" user) (33 "B.Fab" user)')




def _kicad_drc(fp, tmp_path):
    """Place `fp` on a two-layer board and return kicad-cli's DRC violations (skips without KiCad)."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    placed = fp.render().rstrip().rstrip(")") + "\t(at 100 100)\n)\n"
    board = ('(kicad_pcb (version 20260206) (generator "kicadgen_tool") (generator_version "10.0")\n'
             f'\t(general (thickness 1.6)) (paper "A4") (layers {_BOARD_LAYERS})\n'
             "\t(setup (pad_to_mask_clearance 0))\n" + placed +
             '\t(gr_rect (start 80 80) (end 180 140) (stroke (width 0.05) (type default)) (fill none)'
             ' (layer "Edge.Cuts") (uuid "55555555-5555-5555-5555-555555555555"))\n)\n')
    path = tmp_path / "b.kicad_pcb"
    path.write_text(board, encoding="utf-8")
    report = tmp_path / "drc.json"
    done = subprocess.run([cli, "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
                          capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    return json.loads(report.read_text(encoding="utf-8"))["violations"]


def _tags(violations, kind):
    """The «Knn» tags (pad numbers or texts) named by the violations of one type; KiCad's wording is localised."""
    found = []
    for violation in violations:
        if violation["type"] == kind:
            found.extend(re.findall(r"\bK\d\d\b", " ".join(item["description"] for item in violation["items"])))
    return sorted(found)


def _box(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def test_the_keepout_warning_matches_kicad_drc(tmp_path):
    """Same pads flagged by the check and by kicad-cli's DRC (KiCad 10.0.8 on 2026-11-09).

    Inside, other side, mechanical holes with and without copper layers, a
    PTH written with front copper only, touching, slivers of 0.1, 0.3, 0.5 and
    1 um (KiCad's DRC epsilon is 0.5 um, a length), paste/mask apertures, a
    round pad crossed between two chord samples and a crossed (bow tie) zone.
    """
    fp = Footprint(name="T")
    cases = [  # (pad, zone corners, zone layers, KiCad reports it)
        (Pad("K01", "smd", "rect", (0, 0), (1, 1), SMD_FRONT), _box(-1, -1, 1, 1), ("F.Cu",), True),
        (Pad("K02", "smd", "rect", (5, 0), (1, 1), SMD_BACK), _box(4, -1, 6, 1), ("F.Cu",), False),
        (Pad("K03", "np_thru_hole", "circle", (10, 0), (1, 1), THT, drill=1.0), _box(9, -1, 11, 1), ("F.Cu",), True),
        (Pad("K04", "thru_hole", "circle", (15, 0), (1.6, 1.6), THT, drill=0.8), _box(14, -1, 16, 1), ("B.Cu",), True),
        (Pad("K05", "smd", "rect", (20, 0), (1, 1), SMD_FRONT), _box(20.5, -1, 22, 1), ("F.Cu",), False),
        (Pad("K06", "thru_hole", "circle", (25, 0), (1.6, 1.6), ["F.Cu", "F.Mask"], drill=0.8),
         _box(24, -1, 26, 1), ("B.Cu",), True),
        (Pad("K07", "np_thru_hole", "circle", (30, 0), (1, 1), ["*.Mask"], drill=1.0), _box(29, -1, 31, 1), ("F.Cu",), False),
        (Pad("K08", "np_thru_hole", "circle", (35, 0), (1, 1), ["F.Cu", "F.Mask"], drill=1.0),
         _box(34, -1, 36, 1), ("B.Cu",), False),
        (Pad("K09", "smd", "rect", (40, 0), (1, 1), ["F.Mask"]), _box(39, -1, 41, 1), ("F.Cu",), False),
        (Pad("K10", "smd", "rect", (45, 0), (1, 1), ["F.Paste", "F.Mask"]), _box(44, -1, 46, 1), ("F.Cu",), False),
        (Pad("K11", "smd", "rect", (50, 0), (1, 1), SMD_FRONT), [(48, -2), (52, 2), (52, -2), (48, 2)], ("F.Cu",), True),
    ]
    for number, depth, reported in ((12, 0.0001, False), (13, 0.0003, False), (14, 0.0005, True), (15, 0.001, True)):
        x = 5.0 * (number - 12)
        cases.append((Pad(f"K{number}", "smd", "rect", (x, 10), (1, 1), SMD_FRONT),
                      _box(x + 0.5 - depth, 9, x + 2, 11), ("F.Cu",), reported))
    for number, depth, reported in ((16, 0.003, True), (17, -0.003, False)):
        pad, corners = _round_pad_and_slanted_zone(depth)
        x = 30.0 + 15.0 * (number - 16)
        cases.append((Pad(f"K{number}", "smd", "circle", (x, 20), pad.size, SMD_FRONT),
                      [(cx + x, cy + 20) for cx, cy in corners], ("F.Cu",), reported))
    for pad, corners, layers, _reported in cases:
        fp.add_pad(pad)
        fp.add_graphic(FpZone(corners, layers))
    ours = sorted(fp.pads[f.pads[0]].number for f in fc.check_pad_in_keepout(fp))
    expected = sorted(pad.number for pad, _corners, _layers, reported in cases if reported)
    assert ours == expected
    # KiCad names an NPTH without its number: its pads are matched by position
    # (the footprint is placed at (100, 100), every pad sits at its own place).
    by_place = {(round(pad.at[0] + 100, 3), round(pad.at[1] + 100, 3)): pad.number for pad in fp.pads}
    theirs = sorted(by_place[(round(item["pos"]["x"], 3), round(item["pos"]["y"], 3))]
                    for violation in _kicad_drc(fp, tmp_path) if violation["type"] == "items_not_allowed"
                    for item in violation["items"])
    assert theirs == expected


def test_the_silk_text_warning_matches_kicad_drc(tmp_path):
    """Same silkscreen texts flagged by the check and by kicad-cli's DRC (text_height / text_thickness)."""
    fp = Footprint(name="T")
    for item in (FpText("user", "K01", (0, 30), "F.SilkS", 0.7, 0.1),     # low
                 FpText("user", "K02", (5, 30), "F.SilkS", 1.0, 0.07),    # thin
                 FpText("user", "K03", (10, 30), "F.SilkS", 0.8, 0.08),   # at the minimums: passes
                 FpText("user", "K04", (15, 30), "B.SilkS", 0.5, 0.075),  # back silkscreen
                 FpText("user", "K05", (20, 30), "F.Fab", 0.5, 0.075),    # not printed
                 FpText("user", "K06", (25, 30), "F.SilkS", 1.0, 0.15),   # KiCad's default text
                 FpTextBox((0, 35), (5, 38), "K07", "F.SilkS", 0.6, 0.09),
                 FpTextBox((10, 35), (15, 38), "K08", "F.SilkS", 1.0, 0.15)):
        fp.add_graphic(item)
    flagged = fc.check_silk_text_size(fp)
    ours = sorted(fp.graphics[i].text for f in flagged for i in f.graphics)
    assert ours == ["K01", "K02", "K04", "K07"]
    violations = _kicad_drc(fp, tmp_path)
    assert sorted(set(_tags(violations, "text_height") + _tags(violations, "text_thickness"))) == ours
