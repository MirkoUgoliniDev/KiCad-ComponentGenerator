"""@brief Regression tests for the residual KiCad findings L-1, L-3, L-4 and L-5.

@details
- L-1: the isolated preview board declares every standard layer, so pad
  solder mask/paste and Cmts.User drawings stay enabled in KiCad.
- L-3: every generator writes one ``${REFERENCE}`` text on F.Fab, at the
  centre of the fabrication outline (or of the pad field without one).
- L-4: a footprint file is always named after the footprint inside it.
- L-5: scratch files of the packaging have unique names in the target folder.
"""

from __future__ import annotations

import re
import sys
import shutil
import subprocess
from pathlib import Path

import pytest

from kicadgen.core import footprint as core_fp
from kicadgen.core.footprint import Footprint, Pad, fab_reference_text, footprint_file_name
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.castellated_module import build_castellated_module
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.services import kicad_library
from tests.test_contact_style import dev_kit, four_sided, rs485


STEP_TEXT = "ISO-10303-21;\nEND-ISO-10303-21;\n"


def _component(root: Path, footprint: Footprint) -> Path:
    """Write a component folder (footprint + minimal STEP) named after `footprint`."""
    component = root / footprint.name
    footprint.save(str(component / f"{footprint.name}.pretty" / footprint_file_name(footprint.name)))
    shapes = component / f"{footprint.name}.3dshapes"
    shapes.mkdir(parents=True, exist_ok=True)
    (shapes / f"{footprint.name}.step").write_text(STEP_TEXT, encoding="utf-8")
    return component


def _smd_package(name="Pkg", body=(3.0, 1.5)):
    pins = [PinSpec("1", -1.0, 0.0, 1.0, 0.6), PinSpec("2", 1.0, 0.0, 1.0, 0.6)]
    length, width = body if body else (None, None)
    return PackageGeometry(name=name, pins=pins, body_length_mm=length, body_width_mm=width)


GENERATED = {
    "pin_header_body": lambda: build_pin_header(PinHeaderParams(
        name="Hdr", rows=2, cols=4, body_length=12.0, body_width=6.0,
        body_offset_x=-1.0, body_offset_y=-2.0)),
    "pin_header_bare": lambda: build_pin_header(PinHeaderParams(name="Hdr", rows=1, cols=5)),
    "package_body": lambda: build_package(_smd_package()),
    "package_bare": lambda: build_package(_smd_package(body=None)),
    "castellated": lambda: build_castellated_module(dev_kit()),
}


# -- L-1: preview board layer table --------------------------------------------------

_LAYER_ROW = re.compile(r'^\t\t\(\d+ "([^"]+)" (?:signal|user)', re.M)
_USED_LAYERS = re.compile(r'\(layers? ((?:"[^"]+"\s*)+)\)')


def _declared(board: str) -> set[str]:
    return set(_LAYER_ROW.findall(board.split("\t(setup", 1)[0]))


def _undeclared(board: str) -> set[str]:
    """Layers drawn on by the board items but missing from its layer table."""
    used: set[str] = set()
    for group in _USED_LAYERS.findall(board):
        for name in re.findall(r'"([^"]+)"', group):
            if name.startswith("*."):
                used.update({"F." + name[2:], "B." + name[2:]})
            else:
                used.add(name)
    return used - _declared(board)


@pytest.mark.parametrize("case", sorted(GENERATED))
def test_preview_board_declares_every_layer_its_footprint_uses(tmp_path, case):
    component = _component(tmp_path, GENERATED[case]())
    board = kicad_library.create_preview_project(
        kicad_library.discover_component_artifacts(component)).board_path.read_text(encoding="utf-8")
    assert {"F.Mask", "B.Mask", "F.Paste", "B.Paste", "Cmts.User"} <= _declared(board)
    assert _undeclared(board) == set()


def test_layer_check_detects_a_missing_mask_layer(tmp_path):
    """Negative case: the checker is not vacuous, a dropped row is reported."""
    component = _component(tmp_path, build_package(_smd_package()))
    board = kicad_library.create_preview_project(
        kicad_library.discover_component_artifacts(component)).board_path.read_text(encoding="utf-8")
    without_mask = re.sub(r'^\t\t\(\d+ "F\.Mask" user\)\n', "", board, flags=re.M)
    assert without_mask != board
    assert _undeclared(without_mask) == {"F.Mask"}


def test_preview_layer_ordinals_are_unique_and_kicad10_numbered():
    ordinals = [row[0] for row in kicad_library.PREVIEW_BOARD_LAYERS]
    names = [row[1] for row in kicad_library.PREVIEW_BOARD_LAYERS]
    assert len(set(ordinals)) == len(ordinals) and len(set(names)) == len(names)
    by_name = dict(zip(names, ordinals))
    # KiCad 9+ numbering: copper even, technical layers odd.
    assert (by_name["F.Cu"], by_name["B.Cu"], by_name["F.Mask"], by_name["F.Paste"]) == (0, 2, 1, 13)


def _kicad_cli():
    cli = shutil.which("kicad-cli")
    if not cli:
        candidate = Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")
        cli = str(candidate) if candidate.is_file() else None
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


def test_kicad_keeps_mask_and_paste_enabled_and_plots_the_pads(tmp_path):
    """Read-only KiCad check: upgrade a COPY and plot mask/paste of the preview."""
    cli = _kicad_cli()
    component = _component(tmp_path, build_package(_smd_package()))
    preview = kicad_library.create_preview_project(kicad_library.discover_component_artifacts(component))
    copy = tmp_path / "copy" / preview.board_path.name
    copy.parent.mkdir()
    shutil.copy2(preview.board_path, copy)
    subprocess.run([cli, "pcb", "upgrade", "--force", str(copy)], check=True,
                   capture_output=True, timeout=120)
    kept = _declared(copy.read_text(encoding="utf-8"))
    assert {"F.Mask", "B.Mask", "F.Paste", "B.Paste", "Cmts.User"} <= kept
    out = tmp_path / "svg"
    out.mkdir()
    subprocess.run([cli, "pcb", "export", "svg", "--mode-multi", "-o", str(out),
                    "-l", "F.Mask,F.Paste", "--exclude-drawing-sheet", "--page-size-mode", "2",
                    str(copy)], check=True, capture_output=True, timeout=120)
    for layer in ("F_Mask", "F_Paste"):
        svg = next(out.glob(f"*-{layer}.svg")).read_text(encoding="utf-8")
        assert svg.count("<path") == 2, layer   # the two SMD pads


# -- L-3: ${REFERENCE} on F.Fab ------------------------------------------------------

def _fab_rects(fp):
    return [g for g in fp.graphics if type(g).__name__ == "FpRect" and g.layer == "F.Fab"]


@pytest.mark.parametrize("case", sorted(GENERATED))
def test_every_generator_writes_one_fab_reference(case):
    fp = GENERATED[case]()
    texts = [g for g in fp.graphics if type(g).__name__ == "FpText" and g.text == "${REFERENCE}"]
    assert len(texts) == 1 and texts[0].layer == "F.Fab"
    assert '(fp_text user "${REFERENCE}"' in fp.render()
    x, y = texts[0].at
    rects = _fab_rects(fp)
    if rects:
        (x0, y0), (x1, y1) = rects[0].start, rects[0].end
        assert (x, y) == pytest.approx(((x0 + x1) / 2, (y0 + y1) / 2))
    else:
        xs = [p.at[0] for p in fp.pads]
        ys = [p.at[1] for p in fp.pads]
        assert min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys)


def test_pin_header_reference_follows_the_body_not_the_pin_field():
    """Positive + negative: the same pins, with and without an offset body."""
    with_body = GENERATED["pin_header_body"]()
    bare = build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=4))
    at = lambda fp: next(g.at for g in fp.graphics if getattr(g, "text", "") == "${REFERENCE}")
    assert at(with_body) == pytest.approx((-1.0 + 6.0, -2.0 + 3.0))
    assert at(bare) == pytest.approx((3 * 2.54 / 2, 2.54 / 2))
    assert at(with_body) != pytest.approx(at(bare))


def test_fab_reference_shrinks_on_small_bodies_only():
    assert fab_reference_text((0, 0), (10.0, 5.0)).size == core_fp.FAB_REFERENCE_MAX_MM
    small = fab_reference_text((0, 0), (1.0, 0.5))
    assert small.size == core_fp.FAB_REFERENCE_MIN_MM
    assert small.thickness == pytest.approx(0.15 * small.size)
    assert fab_reference_text((0, 0), (2.0, 1.0)).size == pytest.approx(0.8)
    assert fab_reference_text((0, 0)).size == core_fp.FAB_REFERENCE_MAX_MM


@pytest.mark.parametrize("bad", [(float("nan"), 0.0), (0.0, float("inf")),
                                 (core_fp.KICAD_COORD_LIMIT_MM + 1, 0.0)])
def test_fab_reference_rejects_positions_kicad_cannot_hold(bad):
    with pytest.raises(ValueError):
        fab_reference_text(bad)


def test_castellated_fab_reference_is_unchanged():
    """The castellated output keeps its historical text (pinned digests rely on it)."""
    text = next(g for g in build_castellated_module(dev_kit()).graphics
                if getattr(g, "text", "") == "${REFERENCE}")
    assert (text.at, text.size, text.thickness, text.layer) == ((0.0, 0.0), 1.0, 0.15, "F.Fab")


# -- L-4: footprint name == file name ------------------------------------------------

def test_save_refuses_a_file_named_differently_from_the_footprint(tmp_path):
    target = tmp_path / "Lib.pretty" / "Other.kicad_mod"
    with pytest.raises(ValueError):
        Footprint(name="Mine").save(str(target))
    assert not target.parent.exists() or not any(target.parent.iterdir())
    with pytest.raises(ValueError):   # case matters: KiCad names are case-sensitive
        Footprint(name="Mine").save(str(tmp_path / "Lib.pretty" / "mine.kicad_mod"))
    good = tmp_path / "Lib.pretty" / "Mine.kicad_mod"
    Footprint(name="Mine").save(str(good))
    assert good.read_text(encoding="utf-8").startswith('(footprint "Mine"')


@pytest.mark.parametrize("case", sorted(GENERATED))
def test_every_generator_saves_under_its_own_name(tmp_path, case):
    fp = GENERATED[case]()
    target = tmp_path / f"{fp.name}.pretty" / footprint_file_name(fp.name)
    fp.save(str(target))
    assert target.read_text(encoding="utf-8").startswith(f'(footprint "{fp.name}"')


def test_installed_footprint_is_renamed_after_its_file(tmp_path):
    component = _component(tmp_path / "src", Footprint(name="Part"))
    source = component / "Part.pretty" / "Part.kicad_mod"
    # A source whose internal name drifted from the file (renamed by hand).
    drifted = source.read_text(encoding="utf-8").replace('(footprint "Part"', '(footprint "Old \\"x\\""', 1)
    source.write_text(drifted, encoding="utf-8", newline="\n")
    result = kicad_library.install_component_library(
        kicad_library.discover_component_artifacts(component), tmp_path / "project")
    installed = result.footprint_path.read_text(encoding="utf-8")
    assert installed.startswith('(footprint "Part"') and result.identifier.endswith(":Part")
    assert source.read_text(encoding="utf-8") == drifted   # the source is left alone


def test_name_footprint_keeps_a_matching_file_and_rejects_non_footprints():
    text = Footprint(name="Same").render()
    assert kicad_library.name_footprint(text, "Same") == text
    assert kicad_library.name_footprint("(footprint Bare (layer F.Cu))", "N") == '(footprint "N" (layer F.Cu))'
    with pytest.raises(kicad_library.KiCadLibraryError) as error:
        kicad_library.name_footprint('(kicad_pcb (version 1))', "N")
    assert error.value.code == "invalid_footprint"


# -- L-5: unique scratch names -------------------------------------------------------

def test_scratch_names_are_unique_siblings(tmp_path):
    target = tmp_path / "X.kicad_mod"
    first, second = (kicad_library._temporary_sibling(target) for _ in range(2))
    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.name.startswith("X.kicad_mod.") and first.name.endswith(kicad_library.TEMPORARY_SUFFIX)


def test_a_stale_scratch_entry_does_not_block_preview_or_install(tmp_path):
    """The old fixed scratch names, occupied by folders, no longer collide."""
    component = _component(tmp_path / "src", Footprint(name="Part"))
    preview_dir = component / ".kicad-preview"
    preview_dir.mkdir()
    stale_board = preview_dir / "Part-preview.kicad_pcb.kicadgen.tmp"
    stale_board.mkdir()
    project = tmp_path / "project"
    stale_fp = project / "KiCadGen.pretty" / "Part.kicad_mod.kicadgen.tmp"
    stale_fp.mkdir(parents=True)
    preview = kicad_library.create_preview_project(kicad_library.discover_component_artifacts(component))
    assert preview.board_path.is_file() and stale_board.is_dir()
    result = kicad_library.install_component_library(
        kicad_library.discover_component_artifacts(component), project)
    assert result.footprint_path.is_file() and stale_fp.is_dir()
    leftovers = {p for p in tmp_path.rglob(f"*{kicad_library.TEMPORARY_SUFFIX}")}
    assert leftovers == {stale_board, stale_fp}


def test_failed_atomic_write_removes_its_unique_scratch_file(tmp_path, monkeypatch):
    target = tmp_path / "board.kicad_pcb"
    target.write_text("old", encoding="utf-8")

    def refuse(source, destination, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(kicad_library, "replace_file", refuse)
    with pytest.raises(PermissionError):
        kicad_library._atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["board.kicad_pcb"]


# -- D1: default Reference/Value positions from the footprint envelope -------------

def _soic8(orientation=0.0, offset=(0.0, 0.0)):
    """SOIC-8 (JEDEC MS-012 lands): pins 1-4 bottom row, 5-8 top row, 1.27 mm pitch."""
    dx, dy = offset
    pins = [PinSpec(str(i + 1), dx - 1.905 + i * 1.27, dy + 2.7, 0.6, 1.55) for i in range(4)]
    pins += [PinSpec(str(8 - i), dx - 1.905 + i * 1.27, dy - 2.7, 0.6, 1.55) for i in range(4)]
    return PackageGeometry(name="SOIC8", family="soic", pins=pins, body_length_mm=4.9,
                           body_width_mm=3.9, orientation_deg=orientation)


#: Half height of the property text box: (size 1.0 + stroke 0.15) / 2, mm.
_TEXT_HALF = 0.575


def _pad_boxes(fp):
    return [core_fp._pad_box(pad) for pad in fp.pads]


def _clear_of_pads(fp, at):
    """True when the text box band [y - half, y + half] keeps 0.2 mm from every pad."""
    _x, y = at
    return all(y + _TEXT_HALF + 0.2 <= y0 + 1e-9 or y - _TEXT_HALF - 0.2 >= y1 - 1e-9
               for _x0, y0, _x1, y1 in _pad_boxes(fp))


D1_CASES = {
    "soic8": lambda: build_package(_soic8()),
    "soic8_rotated_offset": lambda: build_package(_soic8(orientation=90.0)),
    "pin_header_2x5": lambda: build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=5)),
    "pin_header_body": GENERATED["pin_header_body"],
    "castellated": GENERATED["castellated"],
}


@pytest.mark.parametrize("case", sorted(D1_CASES))
def test_reference_above_and_value_below_the_envelope_clear_of_the_pads(case):
    fp = D1_CASES[case]()
    x0, y0, x1, y1 = core_fp.footprint_envelope(fp)
    assert fp.reference_at[1] + _TEXT_HALF <= y0 - 0.2 + 1e-9      # above everything
    assert fp.value_at[1] - _TEXT_HALF >= y1 + 0.2 - 1e-9          # below everything
    assert _clear_of_pads(fp, fp.reference_at) and _clear_of_pads(fp, fp.value_at)
    assert fp.reference_at[0] == fp.value_at[0] == pytest.approx((x0 + x1) / 2, abs=core_fp.TEXT_GRID_MM / 2 + 1e-9)
    # On the preview's 0.1 mm text grid, counted from the first numbered pad.
    ox, oy = next(p.at for p in fp.pads if p.number)
    for x, y in (fp.reference_at, fp.value_at):
        assert abs((x - ox) / 0.1 - round((x - ox) / 0.1)) < 1e-6
        assert abs((y - oy) / 0.1 - round((y - oy) / 0.1)) < 1e-6
    render = fp.render()
    assert f'(property "Reference" "REF**" (at {fp.reference_at[0]:g} {fp.reference_at[1]:g})' \
        in re.sub(r"\s+", " ", render).replace("( ", "(")


def test_the_old_fixed_reference_fell_on_soic_pads():
    """Negative case: the pre-D1 anchor (0, -2) is not clear of the SOIC-8 pads."""
    fp = build_package(_soic8())
    assert not _clear_of_pads(fp, core_fp.DEFAULT_TEXT_POSITIONS["Reference"])


def test_a_rotated_package_uses_its_real_extent():
    upright, turned = build_package(_soic8()), build_package(_soic8(orientation=90.0))
    # Turned by 90 degrees the pad rows stand left and right: the envelope is
    # the 4.9 mm body side, no longer the pad rows 2 x 2.7 + 1.55 / 2 apart.
    assert turned.reference_at[1] > upright.reference_at[1]
    courtyard = next(g for g in turned.graphics if g.layer == "F.CrtYd")
    assert core_fp.footprint_envelope(turned)[1] == pytest.approx(courtyard.start[1])
    assert courtyard.start[1] < -4.9 / 2   # the turned body edge, plus its courtyard margin


def test_an_asymmetric_pad_field_centres_the_texts_on_it():
    pins = [PinSpec("1", 10.0, 0.0, 1.0, 1.0), PinSpec("2", 14.0, 0.0, 1.0, 1.0)]
    fp = build_package(PackageGeometry(name="A", pins=pins))
    assert fp.reference_at[0] == pytest.approx(12.0) and fp.value_at[0] == pytest.approx(12.0)


def test_a_user_choice_is_written_unchanged():
    fp = build_package(_soic8()).place_texts({"Reference": (1.23, -7.5)})
    assert fp.reference_at == (1.23, -7.5)
    assert '(at 1.23 -7.5)' in fp.render()
    assert fp.value_at == core_fp.default_text_positions(build_package(_soic8()))["Value"]


def test_an_empty_footprint_falls_back_to_the_fixed_positions():
    assert core_fp.default_text_positions(Footprint(name="E")) == core_fp.DEFAULT_TEXT_POSITIONS
    assert Footprint(name="E").place_default_texts().reference_at == (0.0, -2.0)


def test_user_drawings_added_after_generation_do_not_move_the_default():
    fp = build_package(_soic8())
    before = (fp.reference_at, fp.value_at)
    fp.add_graphic(core_fp.FpRect(start=(-20, -20), end=(20, 20), layer="F.SilkS", width=0.12))
    assert (fp.reference_at, fp.value_at) == before


@pytest.mark.parametrize("case", ["soic8", "pin_header_2x5"])
def test_kicad_drc_finds_no_silk_over_copper(tmp_path, case):
    """Read-only KiCad check on the isolated preview board (skipped without KiCad)."""
    import json
    cli = _kicad_cli()
    fp = D1_CASES[case]()
    component = _component(tmp_path, fp)
    preview = kicad_library.create_preview_project(kicad_library.discover_component_artifacts(component))
    report = tmp_path / "drc.json"
    subprocess.run([cli, "pcb", "drc", "--format", "json", "-o", str(report), str(preview.board_path)],
                   check=True, capture_output=True, timeout=120)
    violations = json.loads(report.read_text(encoding="utf-8")).get("violations", [])
    assert not [v for v in violations if v.get("type") == "silk_over_copper"], violations


# -- Review fixes (2026-10-14): offset copper, grid origin, limits, rename, scratch ---

def test_offset_drill_copper_counts_in_the_envelope_without_a_courtyard():
    from kicadgen.generators.castellated_module import OffsetDrillPad
    fp = Footprint(name="O")
    fp.add_pad(OffsetDrillPad("1", "thru_hole", "oval", (0.0, 0.0), (4.0, 1.0), ["*.Cu"],
                              drill=0.8, drill_offset=(-1.5, 0.0)))
    assert core_fp.footprint_envelope(fp) == pytest.approx((-3.5, -0.5, 0.5, 0.5))
    # Rotated 90 degrees the offset turns with the pad (KiCad ShapePos).
    turned = Footprint(name="T")
    turned.add_pad(OffsetDrillPad("1", "thru_hole", "oval", (0.0, 0.0), (4.0, 1.0), ["*.Cu"],
                                  rotation=90.0, drill=0.8, drill_offset=(-1.5, 0.0)))
    assert core_fp.footprint_envelope(turned) == pytest.approx((-0.5, -0.5, 0.5, 3.5))
    # Negative: the same pad without offset is centred on its hole.
    plain = Footprint(name="P")
    plain.add_pad(Pad("1", "thru_hole", "oval", (0.0, 0.0), (4.0, 1.0), ["*.Cu"], drill=0.8))
    assert core_fp.footprint_envelope(plain) == pytest.approx((-2.0, -0.5, 2.0, 0.5))


def test_castellated_offset_copper_reaches_the_envelope():
    fp = build_castellated_module(rs485())
    reach = max(core_fp._pad_box(p)[2] for p in fp.pads)
    assert reach == pytest.approx(21.9)
    assert core_fp.footprint_envelope(fp)[2] >= reach


def _head_castellated_origin(params):
    """Grid origin of the castellated drawer at HEAD 3b28250: first pad_positions contact."""
    from kicadgen.generators.castellated_module import pad_positions
    return next(((pad.x, pad.y) for pad in pad_positions(params)), (0., 0.))


def _head_package_origin(geometry):
    """Grid origin of the package drawer at HEAD 3b28250: first numbered pin seen from the top."""
    return next(((pin.x_mm, pin.y_mm) for pin in geometry.pins_seen_from_top()
                 if str(pin.number).strip()), (0., 0.))


def _head_header_origin(fp):
    """Grid origin of the pin-header drawer at HEAD 3b28250: first numbered pad."""
    return next((pad.at for pad in fp.pads if pad.number), (0., 0.))


#: name -> (build footprint, the grid origin the preview drawer used at HEAD).
GRID_CASES = {
    "rs485": (lambda: build_castellated_module(rs485()), lambda fp: _head_castellated_origin(rs485())),
    "devkit": (lambda: build_castellated_module(dev_kit()), lambda fp: _head_castellated_origin(dev_kit())),
    "four_sided": (lambda: build_castellated_module(four_sided()),
                   lambda fp: _head_castellated_origin(four_sided())),
    "pin_header": (lambda: build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=5)), _head_header_origin),
    "soic8": (lambda: build_package(_soic8()), lambda fp: _head_package_origin(_soic8())),
    "soic8_45": (lambda: build_package(_soic8(orientation=45.0)),
                 lambda fp: _head_package_origin(_soic8(orientation=45.0))),
}


def test_the_preview_grid_origins_are_those_of_head():
    """The per-generator origins equal what the drawers used before (user, 2026-10-09)."""
    from kicadgen.generators import castellated_module as cm, package as pk
    assert cm.preview_grid_origin(rs485()) == pytest.approx((-21.0, -5.08))
    assert cm.preview_grid_origin(four_sided()) == pytest.approx((-14.6, -2.54))
    assert cm.preview_grid_origin(dev_kit()) == pytest.approx(_head_castellated_origin(dev_kit()))
    assert pk.preview_grid_origin(_soic8()) == pytest.approx(_head_package_origin(_soic8()))
    header = build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=5))
    assert core_fp.text_grid_origin(header) == pytest.approx(_head_header_origin(header))


def _is_node(value, origin):
    """On the grid to KiCad's resolution (1 nm): anchors are written with 6 decimals."""
    steps = (value - origin) / core_fp.TEXT_GRID_MM
    return abs(steps - round(steps)) * core_fp.TEXT_GRID_MM <= 1e-6


@pytest.mark.parametrize("case", sorted(GRID_CASES))
def test_default_anchors_are_nodes_of_the_preview_grid(case):
    build, origin = GRID_CASES[case]
    fp = build()
    ox, oy = origin(fp)
    for x, y in (fp.reference_at, fp.value_at):
        assert _is_node(x, ox) and _is_node(y, oy)
    # Mirror images within one grid step when the geometry is symmetric.
    x0, y0, x1, y1 = core_fp.footprint_envelope(fp)
    if abs(x0 + x1) < 1e-6 and abs(y0 + y1) < 1e-6:
        assert abs(fp.reference_at[1] + fp.value_at[1]) < core_fp.TEXT_GRID_MM + 1e-9
        assert abs(fp.reference_at[0]) <= core_fp.TEXT_GRID_MM / 2 + 1e-9


def test_a_grid_symmetric_about_the_part_gives_exact_mirror_texts():
    """Positive: first pad 20 and 30 grid steps from the centre gives exact symmetry.

    Negative (the declared limit of a grid through a pad): pads at +-2.03 mm
    put the centre 20.3 steps from the grid origin, so the anchor stays on
    the grid, 0.03 mm off the centre line, within half a step.
    """
    def quad(half):
        # The body is large enough for the courtyard to hold the pin-1 dot,
        # so the envelope is the symmetric courtyard.
        pins = [PinSpec("1", -half, -3, 1, 1), PinSpec("2", half, -3, 1, 1),
                PinSpec("3", half, 3, 1, 1), PinSpec("4", -half, 3, 1, 1)]
        return build_package(PackageGeometry(name="Q", pins=pins, body_length_mm=10.0,
                                             body_width_mm=12.0))
    fp = quad(2.0)
    x0, y0, x1, y1 = core_fp.footprint_envelope(fp)
    assert (x0 + x1, y0 + y1) == pytest.approx((0.0, 0.0))
    assert fp.reference_at[0] == 0.0 == fp.value_at[0]
    assert fp.reference_at[1] == -fp.value_at[1]
    off_grid = quad(2.03)
    assert off_grid.reference_at[0] == pytest.approx(-0.03)
    assert abs(off_grid.reference_at[0]) <= core_fp.TEXT_GRID_MM / 2


@pytest.mark.parametrize("case", ["rs485", "four_sided", "devkit", "soic8"])
def test_preview_drawers_keep_their_grid_and_show_the_file_texts(window, case):
    """Grid origin as at HEAD; the drawn default texts are those the file gets."""
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    params = {"rs485": rs485, "four_sided": four_sided, "devkit": dev_kit, "soic8": _soic8}[case]()
    if case == "soic8":
        window._draw_preview_package(params)
        fp, head = build_package(params), _head_package_origin(params)
    else:
        window._draw_preview_castellated(params)
        fp, head = build_castellated_module(params), _head_castellated_origin(params)
    assert window._preview_grid_spec["origin"] == pytest.approx(head)
    groups = {i.data(TEXT_KEY_ROLE): i for i in window.preview_scene.items()
              if i.data(TEXT_KEY_ROLE) is not None and i.parentItem() is None}
    for key, at in (("Reference", fp.reference_at), ("Value", fp.value_at)):
        assert (groups[key].pos().x() / 20, groups[key].pos().y() / 20) == pytest.approx(at)


def test_each_text_is_clamped_to_the_limit_on_its_own():
    pins = [PinSpec("1", 0.0, -499.9, 1.0, 1.0), PinSpec("2", 0.0, -497.0, 1.0, 1.0)]
    fp = build_package(PackageGeometry(name="Far", pins=pins))
    assert fp.reference_at[1] == pytest.approx(-500.0)       # clamped, on a node
    assert _is_node(fp.reference_at[1], -499.9)
    assert fp.value_at[1] > -497.0                            # below the pads, not the fallback
    assert fp.value_at != core_fp.DEFAULT_TEXT_POSITIONS["Value"]


def test_a_pad_field_600_mm_from_the_origin_still_builds():
    pins = [PinSpec("1", 600.0, 0.0, 1.0, 1.0), PinSpec("2", 603.0, 0.0, 1.0, 1.0)]
    fp = build_package(PackageGeometry(name="Off", pins=pins))
    fab = next(g for g in fp.graphics if getattr(g, "text", "") == "${REFERENCE}")
    assert fab.at == pytest.approx((601.5, 0.0))
    assert abs(fp.reference_at[0]) <= core_fp.TEXT_POSITION_LIMIT_MM   # property clamped
    with pytest.raises(ValueError):                                     # beyond KiCad itself
        fab_reference_text((core_fp.KICAD_COORD_LIMIT_MM + 1, 0.0))


def test_rename_carries_a_default_value_and_keeps_a_custom_one():
    default = Footprint(name="Old").render()
    renamed = kicad_library.name_footprint(default, "New")
    assert renamed.startswith('(footprint "New"') and '(property "Value" "New"' in renamed
    assert '"Old"' not in renamed
    custom = default.replace('(property "Value" "Old"', '(property "Value" "10k 1%"', 1)
    kept = kicad_library.name_footprint(custom, "New")
    assert kept.startswith('(footprint "New"') and '(property "Value" "10k 1%"' in kept


def test_scratch_names_are_short_exclusive_and_retried(tmp_path, monkeypatch):
    import secrets
    target = tmp_path / "Part.kicad_mod"
    tokens = iter(["deadbeef", "deadbeef", "cafef00d"])
    monkeypatch.setattr(secrets, "token_hex", lambda n: next(tokens))
    first = kicad_library._temporary_sibling(target)
    assert first.name == "Part.kicad_mod.deadbeef.kicadgen.tmp" and first.is_file()
    first.write_text("busy", encoding="utf-8")
    second = kicad_library._temporary_sibling(target)        # collision, retried
    assert second.name == "Part.kicad_mod.cafef00d.kicadgen.tmp"
    assert first.read_text(encoding="utf-8") == "busy"         # the concurrent file is untouched


def test_only_old_scratch_files_of_ours_are_removed(tmp_path):
    import os
    import time
    target = tmp_path / "fp-lib-table.kicadgen-backup"
    old = time.time() - kicad_library.STALE_SCRATCH_AGE_S - 60
    names = {
        "stale_short": "fp-lib-table.kicadgen-backup.0123abcd.kicadgen.tmp",
        "stale_long": "fp-lib-table.kicadgen-backup." + "a" * 32 + ".kicadgen.tmp",
        "stale_legacy": "fp-lib-table.kicadgen-backup.kicadgen.tmp",
        "young": "fp-lib-table.kicadgen-backup.89abcdef.kicadgen.tmp",
        "other_target": "fp-lib-table.0123abcd.kicadgen.tmp",
        "foreign": "fp-lib-table.kicadgen-backup.notours.tmp",
    }
    for key, name in names.items():
        path = tmp_path / name
        path.write_text(key, encoding="utf-8")
        if key != "young":
            os.utime(path, (old, old))
    folder = tmp_path / "fp-lib-table.kicadgen-backup.fedcba98.kicadgen.tmp"
    folder.mkdir()
    os.utime(folder, (old, old))
    removed = {p.name for p in kicad_library._remove_stale_scratch(target)}
    assert removed == {names["stale_short"], names["stale_long"], names["stale_legacy"]}
    left = {p.name for p in tmp_path.iterdir()}
    assert {names["young"], names["other_target"], names["foreign"], folder.name} <= left


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows refuses to delete open files")
def test_a_locked_stale_scratch_file_is_kept(tmp_path):
    import os
    import time
    target = tmp_path / "X.kicad_mod"
    stale = tmp_path / "X.kicad_mod.0123abcd.kicadgen.tmp"
    stale.write_text("x", encoding="utf-8")
    old = time.time() - kicad_library.STALE_SCRATCH_AGE_S - 60
    os.utime(stale, (old, old))
    with open(stale, "rb"):
        assert kicad_library._remove_stale_scratch(target) == []
    assert stale.exists()
