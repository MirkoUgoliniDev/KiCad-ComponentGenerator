import pytest

from kicadgen.core.package_geometry import PinSpec, PackageGeometry, package_from_mapping
from kicadgen.generators.package import build_package


def test_bottom_view_is_normalized_before_generation():
    geometry = PackageGeometry(
        family="soic", source_view="bottom",
        pins=[PinSpec("1", -2, -1, 1.2, .6), PinSpec("2", 2, -1, 1.2, .6)],
    )
    pins = geometry.pins_seen_from_top()
    assert [(p.number, p.x_mm, p.y_mm) for p in pins] == [("1", 2, -1), ("2", -2, -1)]


def test_explicit_package_generates_smd_pads_and_body():
    geometry = package_from_mapping({
        "family": "tssop", "body_length_mm": 5, "body_width_mm": 4,
        "pins": [
            {"number": "1", "x_mm": -2.5, "y_mm": -1.27,
             "pad_length_mm": 1.2, "pad_width_mm": .6, "side": "left"},
            {"number": "2", "x_mm": -2.5, "y_mm": 1.27,
             "pad_length_mm": 1.2, "pad_width_mm": .6, "side": "left"},
        ],
    })
    footprint = build_package(geometry, "TSSOP_TEST")
    rendered = footprint.render()
    assert rendered.count("(pad ") == 2
    assert "F.Paste" in rendered and "F.SilkS" in rendered


def test_duplicate_pin_and_invalid_side_are_rejected():
    # CONTRACT CHANGED 2026-09-27 (HANDOFF 130 C10): KiCad lets several pads
    # share a number (one electrical pin); only a shared number with
    # conflicting names is the sign of a misread and is refused.
    PackageGeometry(pins=[PinSpec("1", 0, 0, 1, 1), PinSpec("1", 2, 0, 1, 1)]).validate()
    with pytest.raises(ValueError, match="pin duplicato"):
        PackageGeometry(pins=[PinSpec("1", 0, 0, 1, 1, name="VCC"),
                              PinSpec("1", 2, 0, 1, 1, name="GND")]).validate()
    with pytest.raises(ValueError, match="lato non valido"):
        PinSpec("1", 0, 0, 1, 1, side="diagonal").validate()


def test_overlapping_pad_areas_are_rejected_even_with_distinct_centers():
    geometry = PackageGeometry(pins=[
        PinSpec("1", 0, 0, 2, 1), PinSpec("2", 1, 0, 2, 1),
    ])
    with pytest.raises(ValueError, match="pad sovrapposti"):
        geometry.validate()
