"""Regression coverage for circular host lands and physical-model isolation."""

from copy import deepcopy
from math import hypot

import pytest

from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, CONTACT_THROUGH_HOLE, pad_positions,
)
from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.package import build_package


def corner_board():
    return CastellatedModuleParams(
        name="Corner", body_length=20, body_width=20, thru_hole_offset=2,
        thru_hole_drill=.8, thru_pad_diameter=1.2,
        sides=[CastellatedSide(edge="top", count=1, pitch=2.54, offset=6.6,
                               first_pin=1, contact_style=CONTACT_THROUGH_HOLE),
               CastellatedSide(edge="right", count=1, pitch=2.54, offset=-6.6,
                               first_pin=2, contact_style=CONTACT_THROUGH_HOLE)])


def test_diagonal_host_lands_with_clear_copper_are_exportable():
    physical = corner_board()
    before = deepcopy(physical)
    host = header_footprint_params(physical, 1.7, 1)
    pads = build_package(host).pads
    assert [p.at for p in pads] == [(6.6, -8), (8, -6.6)]
    assert hypot(1.4, 1.4) - 1.7 > .2
    assert all(p.shape == "circle" and p.drill == 1 for p in pads)
    assert physical == before


@pytest.mark.parametrize("orientation", [0, 30, 90, 217])
@pytest.mark.parametrize("view,mirrored", [("top", False), ("bottom", False), ("top", True)])
def test_rotating_and_reflecting_diagonal_circles_preserves_clearance(orientation, view, mirrored):
    host = header_footprint_params(corner_board(), 1.7, 1)
    host.orientation_deg = orientation
    host.source_view = view
    host.mirrored = mirrored
    pads = build_package(host).pads
    assert hypot(pads[1].at[0] - pads[0].at[0],
                 pads[1].at[1] - pads[0].at[1]) == pytest.approx(hypot(1.4, 1.4), abs=2e-6)


@pytest.mark.parametrize("distance", [0, .5, 1.999, 2])
@pytest.mark.parametrize("rotation", [0, 45])
def test_touching_or_overlapping_circles_are_still_rejected(distance, rotation):
    geometry = PackageGeometry(pins=[
        PinSpec("1", 0, 0, 2, 2, shape="circle", rotation_deg=rotation),
        PinSpec("2", distance, 0, 2, 2, shape="circle")])
    with pytest.raises(ValueError, match="pad sovrapposti"):
        geometry.validate()


@pytest.mark.parametrize("edge", ["top", "bottom", "left", "right"])
def test_header_export_keeps_offset_and_explicit_pin_order_on_every_edge(edge):
    physical = CastellatedModuleParams(
        name="Offset", body_length=30, body_width=20, thru_hole_offset=2,
        sides=[CastellatedSide(edge=edge, count=3, pitch=2.54, offset=1.2,
                               pin_numbers=["7", "GND", "3"],
                               contact_style=CONTACT_THROUGH_HOLE)])
    before = deepcopy(physical)
    pads = build_package(header_footprint_params(physical, 1.6, .8)).pads
    assert [p.number for p in pads] == ["7", "GND", "3"]
    assert [p.at for p in pads] == [physical.thru_hole_centre(p) for p in pad_positions(physical)]
    assert physical == before


def test_invalid_host_update_preserves_existing_artifacts(qapp, tmp_path, monkeypatch):
    from kicadgen.gui.workers import GenerateWorker
    physical = corner_board()
    destination = tmp_path / "Corner.pretty" / "Corner.kicad_mod"
    destination.parent.mkdir()
    destination.write_bytes(b"previous footprint")
    shapes = tmp_path / "Corner.3dshapes"
    shapes.mkdir()
    for suffix in ("step", "wrl", "preview.json"):
        (shapes / f"Corner.{suffix}").write_bytes(b"physical model")
    worker = GenerateWorker(physical, tmp_path)
    worker.footprint_params = PackageGeometry(pins=[])
    monkeypatch.setattr(worker, "_generate_model", lambda: pytest.fail("must not build CAD"))
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert errors
    assert destination.read_bytes() == b"previous footprint"
    assert all(path.read_bytes() == b"physical model" for path in shapes.iterdir())


def explicit_board():
    return PackageGeometry(name="Irregular", family="module", body_length_mm=20,
                           body_width_mm=15, origin="pin1", source_view="bottom",
                           orientation_deg=30, mirrored=True, pins=[
        PinSpec("7", 0, 0, 1.5, 1.5, pad_type="thru_hole", shape="circle", drill_mm=.7),
        PinSpec("GND", 3, 2, 1.5, 1.5, pad_type="thru_hole", shape="circle", drill_mm=.7),
        PinSpec("", 9, 8, 2, 2, pad_type="np_thru_hole", shape="circle", drill_mm=2)])


def test_explicit_header_lands_keep_source_frame_and_mechanical_holes():
    physical = explicit_board()
    before = deepcopy(physical)
    host = header_footprint_params(physical, 1.8, 1.1)
    assert (host.origin, host.source_view, host.orientation_deg, host.mirrored) == (
        "pin1", "bottom", 30, True)
    source_pads = build_package(physical).pads
    host_pads = build_package(host).pads
    assert [(p.number, p.at) for p in host_pads] == [(p.number, p.at) for p in source_pads]
    assert all(p.size == (1.8, 1.8) and p.drill == 1.1 for p in host_pads[:2])
    assert host.pins[-1] == physical.pins[-1]
    assert physical == before


@pytest.mark.parametrize("kind", ["smd", "connect", "only_mechanical"])
def test_explicit_header_mode_never_invents_electrical_holes(kind):
    physical = explicit_board()
    if kind == "only_mechanical":
        physical.pins = physical.pins[-1:]
    else:
        from dataclasses import replace
        physical.pins[0] = replace(physical.pins[0], pad_type=kind, drill_mm=None)
    with pytest.raises(ValueError, match="foro passante|fori passanti"):
        header_footprint_params(physical)


def test_explicit_header_mode_enforces_host_copper_clearance():
    physical = explicit_board()
    with pytest.raises(ValueError, match="troppo grandi"):
        header_footprint_params(physical, 3.5, 1)


def test_aggregate_tht_gui_derives_host_from_explicit_board(window):
    index = window.combo_component_type.findData("aggregate_tht")
    assert index >= 0
    window.combo_component_type.setCurrentIndex(index)
    physical = explicit_board()
    before = deepcopy(physical)
    window.package_geometry = physical
    window.combo_mounting.setCurrentIndex(window.combo_mounting.findData("headers"))
    window.spin_header_pad.setValue(1.8)
    window.spin_header_drill.setValue(1.1)
    host = window._host_footprint_params(physical)
    assert host.pins[0].drill_mm == 1.1
    assert host.pins[0].pad_width_mm == 1.8
    assert host.pins[-1] == physical.pins[-1]
    assert physical == before
