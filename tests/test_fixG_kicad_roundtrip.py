"""@brief Round trip through KiCad's own pcbnew: what we write is what KiCad reads.

The unit tests of the generators check our model of the file; these check the
model against KiCad itself, because two defects (hole and copper swapped by the
drill offset, pad angles rotated the wrong way) passed every unit test: the
tests shared the generator's misreading of the format.

Runs KiCad's bundled Python in a subprocess (pcbnew cannot be imported into
the project's interpreter). Skipped when no KiCad installation with pcbnew is
found; set KICAD_PYTHON to point at another one.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from kicadgen.core.footprint import Footprint, Pad
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, build_castellated_module, continuous_pads,
)
from kicadgen.generators.package import build_package

_CANDIDATES = [os.environ.get("KICAD_PYTHON", ""),
               r"C:\Program Files\KiCad\10.0\bin\python.exe",
               r"C:\Program Files\KiCad\9.0\bin\python.exe"]

#: Loads every footprint of a .pretty and reports what pcbnew understood.
_PROBE = r"""
import json, sys
import pcbnew
lib, name, points = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
fp = pcbnew.FootprintLoad(lib, name)
if fp is None:
    print(json.dumps({"loaded": False}))
    sys.exit(0)
def mm(v):
    return [pcbnew.ToMM(v.x), pcbnew.ToMM(v.y)]
pads = []
for pad in fp.Pads():
    try:
        shape = pad.ShapePos(pcbnew.F_Cu)
    except TypeError:
        shape = pad.ShapePos()
    hits = {}
    for key, (x, y) in points.get(pad.GetNumber(), {}).items():
        hits[key] = bool(pad.HitTest(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))))
    layers = pad.GetLayerSet()
    pads.append({"number": pad.GetNumber(), "pos": mm(pad.GetPosition()), "shape_pos": mm(shape),
                 "angle": pad.GetOrientation().AsDegrees(), "attr": int(pad.GetAttribute()),
                 "paste": bool(layers.Contains(pcbnew.F_Paste)), "hits": hits})
print(json.dumps({"loaded": True, "descr": fp.GetLibDescription(), "pads": pads}))
"""


def _kicad_python():
    for candidate in _CANDIDATES:
        if candidate and Path(candidate).is_file():
            probe = subprocess.run([candidate, "-c", "import pcbnew"], capture_output=True,
                                   timeout=120)
            if probe.returncode == 0:
                return candidate
    return None


KICAD_PYTHON = _kicad_python()
pytestmark = pytest.mark.skipif(KICAD_PYTHON is None, reason="KiCad python with pcbnew not found")


def _load(fp: Footprint, tmp_path: Path, points=None) -> dict:
    """Save `fp` in a scratch library and read it back with pcbnew."""
    library = tmp_path / "roundtrip.pretty"
    fp.save(str(library / f"{fp.name}.kicad_mod"))
    result = subprocess.run([KICAD_PYTHON, "-c", _PROBE, str(library), fp.name,
                             json.dumps(points or {})],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _castellated():
    return CastellatedModuleParams(
        name="RoundTrip", body_length=20, body_width=12,
        sides=[CastellatedSide(edge=edge, count=2, pitch=2.54, first_pin=1 + 2 * i)
               for i, edge in enumerate(("left", "top", "right", "bottom"))])


# -- G1: drill offset ----------------------------------------------------------

def test_kicad_reads_the_hole_at_the_drill_and_the_copper_on_the_stadium(tmp_path):
    """@brief Positive: GetPosition() is the hole, ShapePos() the stadium centre, on every edge."""
    params = _castellated()
    lands = {land.number: land for land in continuous_pads(params)}
    assert len(lands) == 8
    read = _load(build_castellated_module(params), tmp_path)
    assert read["loaded"]
    assert sorted(p["number"] for p in read["pads"]) == sorted(lands)
    for pad in read["pads"]:
        land = lands[pad["number"]]
        assert pad["pos"] == pytest.approx([land.drill_x, land.drill_y], abs=1e-4)
        assert pad["shape_pos"] == pytest.approx([land.x, land.y], abs=1e-4)
        # The two differ: this is the case that tells the models apart.
        assert math.dist(pad["pos"], pad["shape_pos"]) > 0.1


def test_kicad_puts_the_copper_at_the_position_when_there_is_no_offset(tmp_path):
    """@brief Negative: a centred drill has hole and copper at the same place."""
    fp = Footprint(name="Centred")
    fp.add_pad(Pad(number="1", pad_type="thru_hole", shape="oval", at=(1.5, -2.0),
                   size=(3.0, 1.7), layers=["*.Cu", "*.Mask"], drill=1.0))
    pad = _load(fp, tmp_path)["pads"][0]
    assert pad["pos"] == pytest.approx([1.5, -2.0]) == pad["shape_pos"]


# -- G3: rotation convention ---------------------------------------------------

def _radial_package(orientation, source_view="top", mirrored=False, pin_rotation=0.0):
    """Long pads pointing at the centre: their axis must stay radial at any angle."""
    pins = []
    for i, angle in enumerate((0, 90, 180, 270)):
        # In the source view each pad's long axis (local X, rotated by
        # pin_rotation + angle KiCad-wise) points along its position vector.
        a = math.radians(angle + pin_rotation)
        x, y = 3.0 * math.cos(a), -3.0 * math.sin(a)   # KiCad: CCW on screen, Y down
        pins.append(PinSpec(str(i + 1), round(x, 9), round(y, 9), 2.0, 0.4,
                            rotation_deg=angle + pin_rotation))
    return PackageGeometry(name="Radial", family="custom", orientation_deg=orientation,
                           source_view=source_view, mirrored=mirrored, pins=pins)


@pytest.mark.parametrize("orientation,view,mirrored,pin_rotation", [
    (30.0, "top", False, 0.0), (-45.0, "top", False, 15.0), (30.0, "bottom", False, 10.0),
    (60.0, "top", True, 20.0), (0.0, "bottom", True, 33.0), (0.0, "bottom", False, 25.0),
])
def test_kicad_sees_rotated_pads_aligned_with_their_positions(tmp_path, orientation, view,
                                                              mirrored, pin_rotation):
    """@brief Positive: after normalization every radial pad is still radial in pcbnew.

    The copper is probed with pcbnew's own HitTest: a point near the tip of the
    long axis (along the radius) is on the pad, a point just past the half
    width across it is not. With the old clockwise position rotation the pads
    turned one way and their positions the other, and the tips missed.
    """
    geometry = _radial_package(orientation, view, mirrored, pin_rotation)
    pins = geometry.pins_seen_from_top()
    points = {}
    for pin in pins:
        r = math.hypot(pin.x_mm, pin.y_mm)
        ux, uy = pin.x_mm / r, pin.y_mm / r
        points[pin.number] = {
            "tip": (pin.x_mm + ux * 0.9, pin.y_mm + uy * 0.9),
            "across": (pin.x_mm - uy * 0.3, pin.y_mm + ux * 0.3),
        }
    read = _load(build_package(geometry, "Radial"), tmp_path, points)
    for pad in read["pads"]:
        assert pad["hits"] == {"tip": True, "across": False}, pad


def test_old_clockwise_positions_would_miss_the_pad_tips(tmp_path):
    """@brief Negative: the same probe tells the wrong convention apart.

    A pad whose angle follows KiCad but whose position was rotated clockwise
    (the pre-2026-09-25 code) is not radial: the tip probe misses the copper.
    """
    fp = Footprint(name="Wrong")
    a = math.radians(30)
    x, y = 3 * math.cos(a), 3 * math.sin(a)             # clockwise on screen: wrong
    fp.add_pad(Pad(number="1", pad_type="smd", shape="rect", at=(x, y), size=(2.0, 0.4),
                   layers=["F.Cu"], rotation=30.0))
    tip = (x + math.cos(a) * 0.9, y + math.sin(a) * 0.9)
    pad = _load(fp, tmp_path, {"1": {"tip": tip}})["pads"][0]
    assert pad["hits"] == {"tip": False}


# -- G4 / G5: layers and strings -----------------------------------------------

def test_kicad_reads_npth_as_npth_without_paste_and_escaped_descriptions(tmp_path):
    """@brief NPTH pads load as NPTH with no paste; newline and tab survive in descr."""
    geometry = PackageGeometry(name="Npth", family="custom", pins=[
        PinSpec("1", -2, 0, 1.0, 0.6),
        PinSpec("MH", 2, 0, 1.2, 1.2, pad_type="np_thru_hole", shape="circle", drill_mm=1.2),
    ])
    fp = build_package(geometry, "Npth")
    fp.description = 'line one\nline\ttwo "quoted" back\\slash'
    read = _load(fp, tmp_path)
    assert read["loaded"]
    assert read["descr"] == fp.description
    by_attr = {p["attr"]: p for p in read["pads"]}
    npth = by_attr[3]          # PAD_ATTRIB::NPTH
    assert npth["paste"] is False
    assert by_attr[1]["paste"] is True   # PAD_ATTRIB::SMD keeps its paste
