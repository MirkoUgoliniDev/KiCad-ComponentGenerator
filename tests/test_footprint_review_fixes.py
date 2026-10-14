"""@brief Regressions of the footprint review fixes A1, C-2, C-3 and L-2.

A1: user arcs without a circumcentre KiCad can represent are dropped; flat
    arcs with a large but representable radius are kept (R5, 2026-10-14).
C-2: the pin-header silk avoids the copper actually emitted (pad overrides).
C-3: host-mounting lands are checked with their real size and shape.
L-2: the global fp-lib-table version comes from ``kicad-cli version`` first.
"""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from kicadgen.core.footprint import (
    KICAD_COORD_LIMIT_MM, Footprint, FpCircle, FpLine, FpRect,
    drawing_graphics, validate_footprint_drawings,
)
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.core.silk import SILK_PAD_CLEARANCE_MM
from kicadgen.generators.castellated_module import MIN_COPPER_GAP_MM
from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.services.kicad_library import (
    KiCadLibraryError, global_fp_lib_table_path, kicad_settings_version,
)
from kicadgen.services.system_dependencies import find_kicad_cli


def _arc(points):
    """@brief Validate one F.SilkS arc record through the public loader."""
    return validate_footprint_drawings([{"kind": "arc", "layer": "F.SilkS", "points": points}])


# ------------------------------------------------------------------ A1 arcs

def test_a_real_arc_is_kept():
    """@brief Positive case: three points on a 2.83 mm circle stay an arc."""
    assert len(_arc([[0, 0], [2, 2], [4, 0]])) == 1


@pytest.mark.parametrize("points", [
    [[0, 0], [2, 0], [4, 0]],        # exactly collinear, mid on the chord
    [[0, 0], [6, 0], [4, 0]],        # collinear, mid outside the chord
    [[0, 0], [2, 1e-4], [4, 0]],     # 0.1 um sag: radius ~20 m
    [[0.1, 0.2], [0.2, 0.4], [0.3, 0.6]],  # collinear with inexact floats
])
def test_arcs_without_a_finite_circumcentre_are_dropped(points):
    """@brief Negative case: KiCad would read these with an infinite radius."""
    assert _arc(points) == []


def test_the_centre_bound_is_kicads_coordinate_range():
    """@brief The cut-off is KiCad's 32-bit nanometre range, not a tuned value.

    A 20 mm chord whose sag puts the centre just inside / just outside
    KICAD_COORD_LIMIT_MM is kept / dropped respectively.
    """
    def sag(radius):
        return radius - (radius * radius - 100.0) ** 0.5

    inside = KICAD_COORD_LIMIT_MM * 0.5
    outside = KICAD_COORD_LIMIT_MM * 2.0
    assert len(_arc([[-10, 0], [0, sag(inside)], [10, 0]])) == 1
    assert _arc([[-10, 0], [0, sag(outside)], [10, 0]]) == []


def test_a_flat_arc_beyond_the_text_limit_is_kept():
    """@brief Positive: 0.1 mm of bow over a 20 mm chord (radius 500.05 mm) is a real arc.

    The text-position bound (TEXT_POSITION_LIMIT_MM) limits where a point may
    be, not how flat an arc may be: its centre is well inside KiCad's range.
    """
    kept = _arc([[-10, 0], [0, 0.1], [10, 0]])
    assert len(kept) == 1 and kept[0]["points"] == [[-10.0, 0.0], [0.0, 0.1], [10.0, 0.0]]
    assert len(_arc([[-50, 0], [0, 1], [50, 0]])) == 1     # radius 1250.5 mm


def test_a_collinear_arc_is_still_refused():
    """@brief Negative: the same chord with no bow has no centre at all."""
    assert _arc([[-10, 0], [0, 0], [10, 0]]) == []


def test_a_kept_arc_exports_with_a_finite_viewbox(tmp_path):
    """@brief KiCad reads a kept arc as a finite drawing (skipped without KiCad)."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    library = tmp_path / "arc.pretty"
    library.mkdir()
    fp = Footprint(name="ARC")
    for item in drawing_graphics(_arc([[0, 0], [2, 2], [4, 0]])):
        fp.add_graphic(item)
    (library / "ARC.kicad_mod").write_text(fp.render(), encoding="utf-8")
    out = tmp_path / "svg"
    out.mkdir()
    subprocess.run([cli, "fp", "export", "svg", "--fp", "ARC", "-o", str(out), str(library)],
                   capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    svgs = list(out.glob("*.svg"))
    assert svgs
    box = re.search(r'viewBox="([^"]+)"', svgs[0].read_text(encoding="utf-8")).group(1)
    width, height = (float(v) for v in box.split()[2:])
    assert 0 < width < 100 and 0 < height < 100


# ------------------------------------------------- C-2 silk vs real pads

def _silk_hits_copper(fp):
    """@brief Silk segments whose stroke comes closer than the clearance to a pad box."""
    hits = []
    for graphic in fp.graphics:
        if getattr(graphic, "layer", "") != "F.SilkS":
            continue
        for pad in fp.pads:
            hx, hy = pad.size[0] / 2, pad.size[1] / 2
            reach = SILK_PAD_CLEARANCE_MM + graphic.width / 2 - 1e-6
            if isinstance(graphic, FpLine):
                (x0, y0), (x1, y1) = graphic.start, graphic.end
                lo_x, hi_x = sorted((x0, x1))
                lo_y, hi_y = sorted((y0, y1))
                dx = max(0.0, pad.at[0] - hx - hi_x, lo_x - pad.at[0] - hx)
                dy = max(0.0, pad.at[1] - hy - hi_y, lo_y - pad.at[1] - hy)
                if (dx * dx + dy * dy) ** 0.5 < reach:
                    hits.append((graphic.start, graphic.end, pad.number))
            elif isinstance(graphic, FpCircle):
                cx, cy = graphic.center
                dx = max(0.0, abs(cx - pad.at[0]) - hx)
                dy = max(0.0, abs(cy - pad.at[1]) - hy)
                if (dx * dx + dy * dy) ** 0.5 < graphic.radius + reach:
                    hits.append((graphic.center, pad.number))
    return hits


def test_silk_avoids_an_enlarged_override_pad():
    """@brief Positive case: a 3.6 mm override at 5 mm pitch keeps the outline off its copper."""
    fp = build_pin_header(PinHeaderParams(
        name="T", rows=1, cols=3, pitch=5.0, pad_diameter=1.7, drill=1.0, margin=1.0,
        pad_overrides={"2": {"shape": "circle", "size": 3.6}}))
    assert next(p for p in fp.pads if p.number == "2").size == (3.6, 3.6)
    assert _silk_hits_copper(fp) == []


def test_silk_avoids_an_enlarged_pin_one_pad():
    """@brief The pin-1 dot is pushed off an override pad, not off the default size."""
    fp = build_pin_header(PinHeaderParams(
        name="T", rows=1, cols=3, pitch=5.0, pad_diameter=1.7, drill=1.0, margin=0.5,
        pad_overrides={"1": {"shape": "rect", "size": 3.6}}))
    assert _silk_hits_copper(fp) == []


def test_header_without_overrides_keeps_its_outline():
    """@brief Negative case: with no override the outline is the uninterrupted legacy one.

    A margin larger than pad radius + clearance never needs cutting, so the
    silk is still the four full sides it always was.
    """
    fp = build_pin_header(PinHeaderParams(
        name="T", rows=1, cols=3, pitch=5.0, pad_diameter=1.7, drill=1.0, margin=1.5))
    lines = [g for g in fp.graphics if isinstance(g, FpLine) and g.layer == "F.SilkS"]
    rects = [g for g in fp.graphics if isinstance(g, FpRect) and g.layer == "F.SilkS"]
    assert len(lines) + 4 * len(rects) == 4
    assert _silk_hits_copper(fp) == []


# --------------------------------------------- C-3 host land clearance

def _board(*centres):
    """@brief A module whose thru-hole lands sit at the given centres, mm."""
    return PackageGeometry(name="M", family="module", pins=[
        PinSpec(str(i), x, y, 1.0, 1.0, pad_type="thru_hole", shape="circle", drill_mm=0.6)
        for i, (x, y) in enumerate(centres, start=1)])


def test_an_override_that_bridges_two_lands_is_rejected():
    """@brief 3.2 mm next to 1.7 mm at 2.54 mm leaves negative copper."""
    with pytest.raises(ValueError, match="troppo grandi"):
        header_footprint_params(_board((0, 0), (2.54, 0)), 1.7, 1.0,
                                pad_overrides={"2": {"shape": "circle", "size": 3.2}})


def test_diagonal_squares_are_checked_by_their_corners():
    """@brief 1.8 mm squares at (0,0),(1.9,1.0): 0.1 mm of copper, under the minimum.

    As circles the same lands would be 2.15 - 1.8 = 0.35 mm apart and pass:
    only the shape-aware check catches it.
    """
    with pytest.raises(ValueError, match="troppo grandi"):
        header_footprint_params(_board((0, 0), (1.9, 1.0)), 1.8, 1.0, shape="rect")
    assert header_footprint_params(_board((0, 0), (1.9, 1.0)), 1.8, 1.0, shape="circle")


def test_square_override_on_a_round_layout_is_checked_as_a_square():
    """@brief A square override next to a circle uses the square's corner."""
    # Diagonal at 45 deg, centres 2.05 mm apart on each axis: circle-circle
    # gap is 2.9 - 1.7 = 1.2 mm; a 3.6 mm square reaches to 1.8 on each axis,
    # leaving the circle (radius 0.85) only 0.25 * sqrt(2) - 0.85 < 0 mm.
    with pytest.raises(ValueError, match="troppo grandi"):
        header_footprint_params(_board((0, 0), (2.05, 2.05)), 1.7, 1.0,
                                pad_overrides={"1": {"shape": "rect", "size": 3.6}})


def test_well_spaced_lands_are_accepted():
    """@brief Negative cases: standard 2.54 mm lands of either shape pass."""
    host = header_footprint_params(_board((0, 0), (2.54, 0), (5.08, 0)), 1.7, 1.0)
    assert [p.pad_length_mm for p in host.pins] == [1.7, 1.7, 1.7]
    square = header_footprint_params(_board((0, 0), (2.54, 0)), 1.8, 1.0, shape="rect")
    assert {p.shape for p in square.pins} == {"rect"}
    # Gap of exactly the minimum is allowed.
    edge = header_footprint_params(_board((0, 0), (1.7 + MIN_COPPER_GAP_MM, 0)), 1.7, 1.0)
    assert len(edge.pins) == 2


# ------------------------------------------------ L-2 settings version

@pytest.mark.parametrize("text,expected", [
    ("10.0.6\n", "10.0"),
    ("10.99.0-1234-gdeadbeef\n", "10.99"),
    ("\n  9.0.1\n", "9.0"),
    ("not a version", None),
    ("", None),
    (None, None),
])
def test_settings_version_parses_kicad_cli_output(text, expected):
    """@brief major.minor of the first non-blank line, nightly included."""
    assert kicad_settings_version(text) == expected


def test_cli_version_wins_over_the_install_folder(tmp_path):
    """@brief A nightly installed under a 10.0 folder still uses its 10.99 settings."""
    seen = []
    result = global_fp_lib_table_path(
        Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"),
        environ={"APPDATA": str(tmp_path)}, platform="win32",
        version_runner=lambda path: seen.append(path) or "10.99.0-55-gabc\n")
    assert result == tmp_path / "kicad" / "10.99" / "fp-lib-table"
    assert seen == [Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")]


def test_folder_is_the_fallback_when_the_cli_gives_nothing(tmp_path):
    """@brief Negative case: unusable output falls back to the path folder."""
    result = global_fp_lib_table_path(
        Path("C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"),
        environ={"APPDATA": str(tmp_path)}, platform="win32",
        version_runner=lambda path: "garbage")
    assert result == tmp_path / "kicad" / "9.0" / "fp-lib-table"


def test_no_version_anywhere_is_still_refused(tmp_path):
    """@brief Neither output nor folder: version_unknown, as before."""
    with pytest.raises(KiCadLibraryError) as info:
        global_fp_lib_table_path(tmp_path / "kicad-cli.exe", environ={}, platform="win32",
                                 version_runner=lambda path: None)
    assert info.value.code == "version_unknown"


def test_default_runner_reads_the_installed_kicad(tmp_path):
    """@brief With KiCad present, the real command gives a major.minor folder."""
    cli = find_kicad_cli() or shutil.which("kicad-cli")
    if not cli:
        pytest.skip("KiCad CLI not installed")
    result = global_fp_lib_table_path(cli, environ={"APPDATA": str(tmp_path)}, platform="win32")
    assert re.fullmatch(r"\d+\.\d+", result.parent.name)


def _counting_runner(monkeypatch):
    """@brief Replace the subprocess call of the version probe; return the spawn list."""
    from kicadgen.services import kicad_library
    spawned = []

    def run(args, **kwargs):
        spawned.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="10.0.6\n", stderr="")

    monkeypatch.setattr(kicad_library, "_KICAD_CLI_VERSION_CACHE", {})
    monkeypatch.setattr(kicad_library.subprocess, "run", run)
    return kicad_library, spawned


def test_the_cli_version_is_asked_once_per_executable(tmp_path, monkeypatch):
    """@brief R7 positive: the second call for the same unchanged file does not spawn."""
    kicad_library, spawned = _counting_runner(monkeypatch)
    cli = tmp_path / "kicad-cli.exe"
    cli.write_bytes(b"x")
    assert kicad_library._run_kicad_cli_version(cli) == "10.0.6\n"
    assert kicad_library._run_kicad_cli_version(cli) == "10.0.6\n"
    assert len(spawned) == 1


def test_a_changed_or_other_executable_is_asked_again(tmp_path, monkeypatch):
    """@brief R7 negative: a reinstalled file (new mtime/size) or another path spawns again."""
    import os
    kicad_library, spawned = _counting_runner(monkeypatch)
    cli = tmp_path / "kicad-cli.exe"
    cli.write_bytes(b"x")
    kicad_library._run_kicad_cli_version(cli)
    cli.write_bytes(b"xy")
    stat = cli.stat()
    os.utime(cli, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    kicad_library._run_kicad_cli_version(cli)
    other = tmp_path / "other" / "kicad-cli.exe"
    other.parent.mkdir()
    other.write_bytes(b"x")
    kicad_library._run_kicad_cli_version(other)
    assert len(spawned) == 3
    assert kicad_library._run_kicad_cli_version(tmp_path / "missing.exe") is None
    assert len(spawned) == 3
