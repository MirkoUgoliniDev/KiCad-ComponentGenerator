"""@brief Regression tests of the block-C CAD cleanup (HANDOFF 130: C2-C4, C7-C10).

Each behaviour change has a positive and a negative case that differ for a
structural reason: declared origin, stroke width, board frame, numeric type,
footprint size, pin names, preview JSON shape, component kind.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from kicadgen.cad3d import kicad_models, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.kicad_models import MODULE_KINDS, _module_total_fits
from kicadgen.cad3d.lead_sides import local_lead_sides
from kicadgen.core import footprint as footprint_module
from kicadgen.core.footprint import FpCircle, Footprint, Pad
from kicadgen.core.package_geometry import PackageGeometry, PinSpec, package_from_mapping
from kicadgen.core.silk import (
    SILK_PAD_CLEARANCE_MM, keepout_box, pin_one_marker, push_marker_clear,
)
from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, ComponentBox, build_castellated_module,
)
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.services import kicad_library, messages


# -- helpers -----------------------------------------------------------------

def _pad_box(pad: Pad) -> tuple:
    """Axis-aligned copper box of an unrotated pad, mm."""
    half_x, half_y = pad.size[0] / 2, pad.size[1] / 2
    return (pad.at[0] - half_x, pad.at[1] - half_y, pad.at[0] + half_x, pad.at[1] + half_y)


def _ink_gap(marker: FpCircle, pads) -> float:
    """Smallest distance between the marker ink and any pad copper box, mm."""
    ink = marker.radius + marker.width / 2
    cx, cy = marker.center
    gaps = []
    for pad in pads:
        x0, y0, x1, y1 = _pad_box(pad)
        dx, dy = max(x0 - cx, 0.0, cx - x1), max(y0 - cy, 0.0, cy - y1)
        gaps.append(math.hypot(dx, dy) - ink)
    return min(gaps)


def _markers(fp):
    return [g for g in fp.graphics if isinstance(g, FpCircle) and g.layer == "F.SilkS"]


def _pins_pin1_frame():
    """SOIC-8-like pads given from pin 1 (x=0, y=0), pitch 1.27, rows 5.4 mm apart."""
    pins = [PinSpec(str(i + 1), 0.0, i * 1.27, 1.5, 0.6) for i in range(4)]
    pins += [PinSpec(str(8 - i), 5.4, i * 1.27, 1.5, 0.6) for i in range(4)]
    return pins


# -- C2: pad origin --------------------------------------------------------------

def test_pin1_origin_is_recentred_on_the_pad_field_with_a_note():
    geometry = PackageGeometry(family="soic", origin="pin1", body_length_mm=4.0,
                               body_width_mm=5.0, pins=_pins_pin1_frame())
    pins = {p.number: p for p in geometry.pins_seen_from_top()}
    # Pad field 0..5.4 x 0..3.81 -> centre (2.7, 1.905) becomes the origin.
    assert (pins["1"].x_mm, pins["1"].y_mm) == pytest.approx((-2.7, -1.905))
    assert (pins["5"].x_mm, pins["5"].y_mm) == pytest.approx((2.7, 1.905))
    # Relative geometry (pitch along a row, row spacing across) is unchanged.
    assert pins["2"].y_mm - pins["1"].y_mm == pytest.approx(1.27)
    assert pins["8"].x_mm - pins["1"].x_mm == pytest.approx(5.4)
    assert geometry.frame_notes() and "stima" in geometry.frame_notes()[0]
    fp = build_package(geometry, "SOIC8_PIN1")
    assert "stimato" in fp.description
    # The body outline drawn around (0, 0) now encloses the pad field centre.
    xs = [p.at[0] for p in fp.pads]
    assert (min(xs) + max(xs)) / 2 == pytest.approx(0.0)


def test_body_center_origin_keeps_the_declared_coordinates_and_no_note():
    pins = [PinSpec("1", -2.7, -1.0, 1.5, 0.6), PinSpec("2", 2.0, 3.0, 1.5, 0.6)]
    geometry = PackageGeometry(family="soic", origin="body_center", pins=pins)
    seen = geometry.pins_seen_from_top()
    # An asymmetric field is NOT recentred when the body centre is declared.
    assert [(p.x_mm, p.y_mm) for p in seen] == [(-2.7, -1.0), (2.0, 3.0)]
    assert geometry.frame_notes() == []
    assert "stimato" not in build_package(geometry, "X").description


def test_custom_origin_is_recentred_like_pin1():
    geometry = PackageGeometry(family="custom", origin="custom", pins=[
        PinSpec("1", 10.0, 10.0, 1, 1), PinSpec("2", 14.0, 10.0, 1, 1)])
    assert [(p.x_mm, p.y_mm) for p in geometry.pins_seen_from_top()] == [(-2.0, 0.0), (2.0, 0.0)]


# -- C3: pin-1 marker clearance ----------------------------------------------------

def test_marker_ink_keeps_the_promised_clearance_from_the_copper():
    pad = Pad("1", "smd", "rect", (0.0, 0.0), (1.0, 1.0), ["F.Cu"])
    keepouts = [keepout_box(*_pad_box(pad), line_width=0.0)]
    # Preferred centre just outside the old (stroke-less) limit.
    start = (0.5 + SILK_PAD_CLEARANCE_MM + 0.25 + 0.01, 0.0)
    marker = pin_one_marker(start, keepouts, (1.0, 0.0))
    assert _ink_gap(marker, [pad]) >= SILK_PAD_CLEARANCE_MM - 1e-6
    # Negative: with no stroke the same start is already clear and is kept.
    thin = pin_one_marker(start, keepouts, (1.0, 0.0), width=0.0)
    assert thin.center == start


@pytest.mark.parametrize("build", ["package", "header", "castellated"])
def test_every_generator_keeps_the_marker_ink_off_the_copper(build):
    if build == "package":
        fp = build_package(PackageGeometry(family="soic", origin="pin1", pins=_pins_pin1_frame()))
    elif build == "header":
        fp = build_pin_header(PinHeaderParams(rows=2, cols=4, margin=0.3))
    else:
        fp = build_castellated_module(CastellatedModuleParams(
            name="M", body_length=20.0, body_width=15.0,
            sides=[CastellatedSide(edge="left", count=5, pitch=2.54, first_pin=1),
                   CastellatedSide(edge="right", count=5, pitch=2.54, first_pin=6)]))
    [marker] = _markers(fp)
    copper = [p for p in fp.pads if p.rotation == 0 and p.shape != "custom"]
    assert _ink_gap(marker, copper) >= SILK_PAD_CLEARANCE_MM - 0.02


def test_marker_that_cannot_leave_the_copper_raises_instead_of_lying():
    blocked = [(-100.0, -100.0, 100.0, 100.0)]
    with pytest.raises(ValueError):
        push_marker_clear((0.0, 0.0), (1.0, 0.0), 0.31, blocked, limit=5.0)
    # Negative: a reachable free area is found and no error is raised.
    near = [(-1.0, -1.0, 1.0, 1.0)]
    assert push_marker_clear((0.0, 0.0), (1.0, 0.0), 0.31, near)[0] >= 1.31 - 1e-6


# -- C4: one component policy for castellated and populated THT --------------------

def _tht_run(monkeypatch, tmp_path, components, attach=None, height=5.0):
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options)
        return {"warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    monkeypatch.setattr(kicad_models, "attach_models", attach or (lambda c, **k: 0))
    body = Body3DParams(length_mm=20.0, width_mm=10.0, height_mm=height, offset_x_mm=-2.0,
                        offset_y_mm=-3.0, pcb_thickness_mm=1.6,
                        populated_board=True, components=components)
    result = step_generator.generate_3d_model(PinHeaderParams(), body, tmp_path)
    return captured["extra_payload"]["components"], result


def test_populated_tht_applies_placement_height_cap_and_notes(monkeypatch, tmp_path):
    boxes = [ComponentBox(x=9.5, y=0.0, length=2.0, width=1.0, height=9.0, name="TALL",
                          match_evidence={"photo_scale_px_per_mm": 50.0}),
             ComponentBox(x=0.0, y=0.0, length=0.0, width=1.0, height=1.0, name="FLAT")]
    payload, result = _tht_run(monkeypatch, tmp_path, boxes)
    assert [c["name"] for c in payload] == ["TALL"]
    tall = payload[0]
    # Board centre (8, 2); a 0.5 mm overhang above a 50 px/mm tolerance is kept.
    assert tall["x"] == pytest.approx(8.0 + 9.5) and tall["y"] == pytest.approx(2.0)
    assert tall["height_mm"] == pytest.approx(5.0 - 1.6)
    notes = " ".join(result["notes"])
    assert "FLAT" in notes and "TALL" in notes and "ridotta" in notes


def test_populated_tht_leaves_a_body_inside_the_board_untouched(monkeypatch, tmp_path):
    payload, result = _tht_run(monkeypatch, tmp_path, [
        ComponentBox(x=1.0, y=-1.0, length=2.0, width=1.0, height=1.0, name="R1")])
    assert (payload[0]["x"], payload[0]["y"], payload[0]["height_mm"]) == (9.0, 1.0, 1.0)
    assert not any("R1" in note for note in result["notes"])


def test_populated_tht_falls_back_when_the_kicad_library_fails(monkeypatch, tmp_path):
    def broken(components, **kwargs):
        raise RuntimeError("library index unreadable")

    payload, result = _tht_run(monkeypatch, tmp_path, [
        ComponentBox(x=0.0, y=0.0, length=2.0, width=1.0, height=1.0, name="U1")], broken)
    assert [c["name"] for c in payload] == ["U1"]
    assert any("library index unreadable" in note for note in result["notes"])


def test_castellated_notes_an_unusable_component_instead_of_dropping_it(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(step_generator, "_run_freecad",
                        lambda *a, **k: captured.update(k) or {"warnings": [], "notes": []})
    monkeypatch.setattr(kicad_models, "attach_models", lambda c, **k: 0)
    params = CastellatedModuleParams(
        name="M", body_length=30.0, body_width=20.0, pcb_thickness=1.0,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1)],
        components=[ComponentBox(x=0, y=0, length=2, width=1, height=1, name="OK"),
                    ComponentBox(x=0, y=5, length=-1, width=1, height=1, name="BAD")])
    result = step_generator.generate_castellated_3d_model(
        params, Body3DParams(length_mm=30.0, width_mm=20.0, height_mm=3.0), tmp_path)
    assert [c["name"] for c in captured["extra_payload"]["components"]] == ["OK"]
    assert any("BAD" in note for note in result["notes"])
    assert not any("OK" in note for note in result["notes"])


# -- C7: numpy numbers -----------------------------------------------------------

def test_lead_sides_accept_numpy_numbers():
    assert local_lead_sides({"left": np.int64(3), "right": np.int32(3)},
                            np.float32(0.0)) == {"left": 3, "right": 3}
    assert local_lead_sides({"top": np.int64(2)}, np.float64(90.0)) is not None


def test_lead_sides_still_refuse_booleans_and_float_counts():
    assert local_lead_sides({"left": True}, 0.0) is None
    assert local_lead_sides({"left": 2.0}, 0.0) is None
    assert local_lead_sides({"left": 2}, True) is None
    assert local_lead_sides({"left": 2}, np.bool_(True)) is None


# -- C8: localized pin-header validation -------------------------------------------

@pytest.mark.parametrize("params", [
    PinHeaderParams(pitch=float("nan")),
    PinHeaderParams(body_length=5.0),
    PinHeaderParams(body_length=-1.0, body_width=2.0),
    PinHeaderParams(margin=-0.1),
    PinHeaderParams(rows=1, cols=2, populated_pins={3}),
])
def test_pin_header_errors_go_through_the_translator(params, monkeypatch):
    seen = []

    def translator(key, default=""):
        seen.append(key)
        return "XX " + default

    monkeypatch.setattr(messages, "_translator", translator)
    with pytest.raises(ValueError, match="^XX "):
        params.validate()
    assert seen and all(key.startswith("ui:") for key in seen)


def test_valid_pin_header_raises_nothing():
    PinHeaderParams(rows=2, cols=3, body_length=10.0, body_width=6.0).validate()


def test_file_descriptions_do_not_follow_the_interface_language(monkeypatch):
    monkeypatch.setattr(messages, "_translator", lambda key, default="": "XX " + default)
    assert footprint_module.FOOTPRINT_TEXT_LANGUAGE == "it"
    assert not build_pin_header(PinHeaderParams()).description.startswith("XX")
    geometry = PackageGeometry(family="soic", pins=[PinSpec("1", 0, 0, 1, 1)])
    assert not build_package(geometry).description.startswith("XX")


# -- C9: preview board outline ------------------------------------------------------

def test_preview_outline_grows_around_a_large_courtyard():
    text = build_pin_header(PinHeaderParams(rows=2, cols=40)).render()
    x0, y0, x1, y1 = kicad_library.preview_board_outline(text)
    extent = kicad_library.footprint_extent(text)
    assert x1 >= 100 + extent[2] + kicad_library.PREVIEW_OUTLINE_MARGIN_MM - 1e-6
    assert (x0, y0, y1) == (65.0, 75.0, 125.0)


def test_preview_outline_keeps_the_historic_frame_for_a_small_part():
    text = build_pin_header(PinHeaderParams(rows=1, cols=2)).render()
    assert kicad_library.preview_board_outline(text) == (65.0, 75.0, 135.0, 125.0)


def test_preview_board_file_uses_the_grown_outline(tmp_path):
    root = tmp_path / "Big"
    fp = build_pin_header(PinHeaderParams(name="Big", rows=2, cols=40))
    fp.save(str(root / "Big.pretty" / "Big.kicad_mod"))
    shapes = root / "Big.3dshapes"
    shapes.mkdir()
    (shapes / "Big.step").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
    artifacts = kicad_library.discover_component_artifacts(root)
    board = kicad_library.create_preview_project(artifacts).board_path.read_text(encoding="utf-8")
    x0, y0, x1, y1 = kicad_library.preview_board_outline(fp.render())
    assert f"(gr_rect (start 65 75) (end {kicad_library._mm(x1)} 125)" in board
    assert "(at 100 100)" in board


def test_footprint_extent_falls_back_to_pads_without_a_courtyard():
    fp = Footprint(name="P")
    fp.add_pad(Pad("1", "smd", "rect", (30.0, 0.0), (2.0, 2.0), ["F.Cu"]))
    extent = kicad_library.footprint_extent(fp.render())
    assert extent[2] == pytest.approx(30.0 + math.sqrt(2.0))
    assert kicad_library.footprint_extent(Footprint(name="E").render()) is None


# -- C10: atomic save, strict mirror flag, shared pad numbers ------------------------

def test_footprint_save_failure_keeps_the_previous_file(tmp_path, monkeypatch):
    # Both footprints are named "A" like their file (L-4, 2026-10-14: save
    # refuses a mismatched name); their descriptions tell them apart.
    target = tmp_path / "A.pretty" / "A.kicad_mod"
    Footprint(name="A", description="OLD").save(str(target))
    old = target.read_text(encoding="utf-8")

    import kicadgen.fsutil as fsutil

    def refuse(source, dest, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(fsutil, "replace_file", refuse)
    with pytest.raises(PermissionError):
        Footprint(name="A", description="NEW").save(str(target))
    assert target.read_text(encoding="utf-8") == old
    assert sorted(p.name for p in target.parent.iterdir()) == ["A.kicad_mod"]


def test_footprint_save_writes_lf_utf8(tmp_path):
    target = tmp_path / "B.pretty" / "B.kicad_mod"
    Footprint(name="B", description="più").save(str(target))
    data = target.read_bytes()
    assert b"\r\n" not in data and "più".encode("utf-8") in data


def test_mirrored_accepts_only_real_booleans():
    base = {"family": "soic", "pins": [{"number": "1", "x_mm": 1, "y_mm": 0,
                                         "pad_length_mm": 1, "pad_width_mm": 1}]}
    with pytest.raises(ValueError):
        package_from_mapping(dict(base, mirrored="false"))
    assert package_from_mapping(dict(base, mirrored=False)).pins_seen_from_top()[0].x_mm == 1
    assert package_from_mapping(dict(base, mirrored=True)).pins_seen_from_top()[0].x_mm == -1


def test_pads_may_share_a_number_like_a_split_thermal_pad():
    pins = [PinSpec("1", -3, 0, 1, 0.6, name="IN"), PinSpec("2", 3, 0, 1, 0.6, name="OUT")]
    pins += [PinSpec("3", x, y, 0.8, 0.8, name="GND") for x in (-0.6, 0.6) for y in (-0.6, 0.6)]
    geometry = PackageGeometry(family="qfn", body_length_mm=4, body_width_mm=4, pins=pins)
    geometry.validate()
    fp = build_package(geometry)
    assert [p.number for p in fp.pads].count("3") == 4
    # Every shared-number pad has its own silk keepout: no silk crosses them.
    for graphic in fp.graphics:
        if getattr(graphic, "layer", "") == "F.SilkS" and hasattr(graphic, "start"):
            for pad in fp.pads:
                x0, y0, x1, y1 = _pad_box(pad)
                (sx, sy), (ex, ey) = graphic.start, graphic.end
                assert not (x0 < (sx + ex) / 2 < x1 and y0 < (sy + ey) / 2 < y1)


def test_shared_number_with_conflicting_names_is_a_misread():
    pins = [PinSpec("5", 0, 0, 1, 1, name="RX"), PinSpec("5", 3, 0, 1, 1, name="TX")]
    with pytest.raises(ValueError, match="pin duplicato"):
        PackageGeometry(pins=pins).validate()


def test_mechanical_holes_may_have_no_number_but_pads_may_not():
    hole = PinSpec("", 5, 0, 1.2, 1.2, pad_type="np_thru_hole", shape="circle", drill_mm=1.0)
    PackageGeometry(pins=[PinSpec("1", 0, 0, 1, 1), hole]).validate()
    with pytest.raises(ValueError):
        PinSpec("", 0, 0, 1, 1).validate()


def test_node_scan_still_finds_only_root_children():
    text = '(root (lib (name "a")) (x (lib (name "inner"))) (lib (name "b")))'
    ranges = kicad_library._direct_child_ranges(text, "lib")
    assert [text[a:b] for a, b in ranges] == ['(lib (name "a"))', '(lib (name "b"))']


# -- Block A leftovers ------------------------------------------------------------

_BAD_MESH_CHILD = """
import json, os, sys
data = json.load(open(sys.argv[-1], encoding="utf-8"))
staging = data.get("staging_paths", {})
def write(key, final, text):
    target = staging.get(key, final)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(target, final)
write("step", data["step_path"], STEP)
print("KICADGEN_STEP_OK:" + data["step_path"], flush=True)
write("mesh", data["mesh_path"], MESH)
print("KICADGEN_MESH_OK:" + data["mesh_path"], flush=True)
"""

_STEP = ("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
         "#1 = ( GEOMETRIC_REPRESENTATION_CONTEXT(3) );\n"
         "#10 = MANIFOLD_SOLID_BREP('Body',#11);\n#11 = CLOSED_SHELL('',());\n"
         "ENDSEC;\nEND-ISO-10303-21;\n")


@pytest.mark.parametrize("mesh,coloured", [
    ('[1, 2, 3]', False),                                   # not an object
    ('{"parts": [{"color": "#112233", "solids": null}]}', False),  # bad count
    ('{"parts": [{"color": "#112233", "solids": 1}]}', True),      # valid
])
def test_a_bad_preview_json_only_skips_the_colours(monkeypatch, tmp_path, mesh, coloured):
    script = tmp_path / "child.py"
    script.write_text(f"STEP = {_STEP!r}\nMESH = {mesh!r}\n" + _BAD_MESH_CHILD, encoding="utf-8")
    monkeypatch.setattr(step_generator, "_SCRIPT_PATH", script)
    out = tmp_path / "out"
    result = step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), out,
                                         freecadcmd_path=sys.executable)
    step = out / "Demo.3dshapes" / "Demo.step"
    assert step.is_file() and result["step_path"] == step
    assert ("COLOUR_RGB" in step.read_text(encoding="utf-8")) is coloured
    assert bool(result["warnings"]) is not coloured


def test_shield_can_is_checked_like_a_board_module():
    assert "shield_can" in MODULE_KINDS and "module" in MODULE_KINDS
    notes = []
    oversized = {"total": (20.0, 20.0, 3.0)}
    path = Path("Custom.3dshapes") / "can.step"
    assert _module_total_fits(path, oversized, {"kind": "shield_can"}, 10.0, 10.0, 0.1,
                              notes.append) is False
    # Negative: an ordinary IC keeps its body/lead alternatives (no module veto).
    assert _module_total_fits(path, oversized, {"kind": "ic"}, 10.0, 10.0, 0.1,
                              notes.append) is True


def test_footprint_temp_file_lives_next_to_the_target(tmp_path, monkeypatch):
    seen = {}
    import kicadgen.fsutil as fsutil
    real = fsutil.replace_file

    def spy(source, dest, **kwargs):
        seen["dir"] = os.path.dirname(source)
        return real(source, dest, **kwargs)

    monkeypatch.setattr(fsutil, "replace_file", spy)
    target = tmp_path / "C.pretty" / "C.kicad_mod"
    Footprint(name="C").save(str(target))
    assert Path(seen["dir"]) == target.parent


def _soic(orientation):
    from kicadgen.core.package_geometry import package_from_mapping
    pins = [dict(number=str(i + 1), x_mm=-2.7 if i < 4 else 2.7,
                 y_mm=(-1.905 + 1.27 * (i % 4)) if i < 4 else (1.905 - 1.27 * (i % 4)),
                 pad_length_mm=1.5, pad_width_mm=0.6) for i in range(8)]
    return package_from_mapping(dict(name="SOIC8", family="soic", body_length_mm=3.9,
                                     body_width_mm=4.9, orientation_deg=orientation, pins=pins))


def _fab_points(footprint):
    points = []
    for graphic in footprint.graphics:
        # Outline only: the ${REFERENCE} text on F.Fab (L-3, 2026-10-14) is no edge.
        if getattr(graphic, "layer", "") == "F.Fab" and type(graphic).__name__ != "FpText":
            points += [graphic.start, graphic.end]
    return points


def test_a_rotated_package_turns_its_body_outline_with_its_pads():
    """Blocco C (131): orientation_deg ruotava i pad ma non il rettangolo
    corpo/fab/silk. A 90 gradi il corpo 3.9 x 4.9 diventa 4.9 x 3.9; a 30 gradi
    e' un quadrilatero ruotato, non piu' un rettangolo allineato agli assi."""
    from kicadgen.generators.package import build_package
    upright = build_package(_soic(0))
    turned = build_package(_soic(90))
    xs0 = [p[0] for p in _fab_points(upright)]
    xs90 = [p[0] for p in _fab_points(turned)]
    assert max(xs0) - min(xs0) == pytest.approx(3.9)
    assert max(xs90) - min(xs90) == pytest.approx(4.9)
    slanted = build_package(_soic(30))
    fab = [g for g in slanted.graphics if getattr(g, "layer", "") == "F.Fab"
           and type(g).__name__ != "FpText"]   # outline only (L-3 adds the F.Fab reference)
    assert len(fab) == 4 and all(type(g).__name__ == "FpLine" for g in fab)
    assert any(abs(g.start[0] - g.end[0]) > 1e-6 and abs(g.start[1] - g.end[1]) > 1e-6
               for g in fab)


def test_an_unrotated_package_keeps_its_axis_aligned_outline():
    """Negativo: a 0 gradi il corpo resta un FpRect di 3.9 x 4.9, come prima."""
    from kicadgen.generators.package import build_package
    fab = [g for g in build_package(_soic(0)).graphics if getattr(g, "layer", "") == "F.Fab"
           and type(g).__name__ != "FpText"]   # outline only (L-3 adds the F.Fab reference)
    assert len(fab) == 1 and type(fab[0]).__name__ == "FpRect"
    assert abs(fab[0].end[0] - fab[0].start[0]) == pytest.approx(3.9)


def test_a_null_mirrored_means_not_mirrored_and_a_string_is_refused():
    """Revisione del blocco C: mirrored e' facoltativo nello schema; un null
    esplicito vale «non specchiato» (prima della C10 anche); una stringa no."""
    from kicadgen.core.package_geometry import package_from_mapping
    pins = [dict(number="1", x_mm=0, y_mm=0, pad_length_mm=1, pad_width_mm=1)]
    assert package_from_mapping(dict(name="X", pins=pins, mirrored=None)).mirrored is False
    with pytest.raises(ValueError):
        package_from_mapping(dict(name="X", pins=pins, mirrored="false")).validate()


def test_mounting_holes_do_not_move_the_estimated_body_centre():
    """Il centro stimato (origine pin1) e' quello del campo dei pin: un foro di
    fissaggio asimmetrico non lo sposta; senza fori il risultato e' lo stesso."""
    from kicadgen.core.package_geometry import package_from_mapping
    pads = [dict(number=str(i + 1), x_mm=i * 2.54, y_mm=0, pad_length_mm=1.5, pad_width_mm=1)
            for i in range(4)]
    hole = dict(number="", x_mm=20.0, y_mm=0, pad_length_mm=3, pad_width_mm=3,
                pad_type="np_thru_hole", drill_mm=3.0)
    plain = package_from_mapping(dict(name="X", origin="pin1", pins=pads))
    holed = package_from_mapping(dict(name="X", origin="pin1", pins=pads + [hole]))
    assert holed.body_frame_offset() == pytest.approx(plain.body_frame_offset())
    assert plain.body_frame_offset() == pytest.approx((3.81, 0.0))
