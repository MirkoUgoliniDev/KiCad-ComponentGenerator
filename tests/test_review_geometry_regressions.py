"""Regression cases found by the October 2026 geometry review."""

import pytest

from kicadgen.core.footprint import Pad
from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import PinHeaderParams
from kicadgen.cad3d import kicad_models, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.header_geometry import male_header_boxes_by_row


@pytest.mark.parametrize("drill", [1.0, 2.0])
@pytest.mark.parametrize("size", [(1.0, 2.0), (2.0, 1.0)])
def test_plated_hole_requires_copper_on_both_axes(drill, size):
    geometry = PackageGeometry(pins=[PinSpec(
        "1", 0, 0, *size, pad_type="thru_hole", shape="oval", drill_mm=drill)])
    with pytest.raises(ValueError, match="foro del pin"):
        build_package(geometry)


def test_valid_annulus_and_nonplated_hole_remain_supported():
    for kind, number, drill in [("thru_hole", "1", .8), ("np_thru_hole", "", 1.0)]:
        fp = build_package(PackageGeometry(pins=[PinSpec(
            number, 0, 0, 1, 1, pad_type=kind, shape="circle", drill_mm=drill)]))
        assert fp.pads[0].drill == drill


def test_custom_pad_without_primitives_is_rejected_at_both_entry_points():
    # KiCad otherwise makes this a circular 2x2 anchor; the geometry validator
    # would see 2x1, letting distinct pads overlap unnoticed on the real board.
    with pytest.raises(ValueError, match="shape"):
        build_package(PackageGeometry(pins=[PinSpec("1", 0, 0, 2, 1, shape="custom")]))
    with pytest.raises(ValueError, match="shape"):
        Pad("1", "smd", "custom", (0, 0), (2, 1), ["F.Cu"])


@pytest.mark.parametrize("angle, expected", [
    (0, (-1, -1, 3, 1)),
    (90, (-1, -3, 1, 1)),
    (180, (-3, -1, 1, 1)),
])
def test_copper_bounds_apply_drill_offset_in_rotated_pad_axes(angle, expected):
    pad = kicad_models._sexpr(f'''(pad "1" thru_hole oval
        (at 0 0 {angle}) (size 4 2) (drill 1 (offset 1 0)) (layers "*.Cu"))''')
    assert kicad_models._pad_bounds(pad) == pytest.approx(expected)


def test_linked_footprint_measurement_uses_offset_copper_envelope(tmp_path):
    path = tmp_path / "offset.kicad_mod"
    path.write_text('''(footprint "offset"
        (pad "1" thru_hole oval (at 0 0) (size 4 2)
          (drill 1 (offset 1 0)) (layers "*.Cu"))
        (pad "2" thru_hole circle (at 10 0) (size 2 2) (drill 1) (layers "*.Cu"))
        (model "part.step"))''', encoding="utf-8")
    size, _, error = kicad_models._footprint_record(path, "review")
    assert error is None
    assert size == pytest.approx([12, 2])


@pytest.mark.parametrize("populated, cols", [({1, 3}, 3), ({1}, 1)])
def test_populated_tht_header_retains_declared_pitch(monkeypatch, tmp_path, populated, cols):
    params = PinHeaderParams(rows=1, cols=cols, pitch=1.27,
                             populated_pins=populated, pad_diameter=1.0, drill=.8)
    body = Body3DParams(10, 5, 3, populated_board=True, pcb_thickness_mm=1.6)
    captured = {}

    def fake_run(name, pins, body, output, **kwargs):
        captured.update(pins=pins, **kwargs["extra_payload"])
        return {"warnings": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    monkeypatch.setattr(step_generator, "attach_library_models", lambda *a, **kw: None)
    monkeypatch.setattr("kicadgen.services.model_choices.save_candidate_report", lambda *a: None)
    step_generator.generate_3d_model(params, body, tmp_path)
    boxes = male_header_boxes_by_row(captured["pins"], 1.6,
                                     pitches=captured["header_pitches"])
    pins = [box for box in boxes if box["role"] == "pin"]
    assert len(pins) == len(populated)
    assert all(pin["dx"] == pytest.approx(.32) for pin in pins)
    assert all(pin["z"] == pytest.approx(-4.25) for pin in pins)
