"""@brief Regression tests for optional THT bodies and union courtyards."""

import pytest

from kicadgen.core.footprint import FpCircle, FpRect
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header


def _rect(fp, layer):
    return next(g for g in fp.graphics if isinstance(g, FpRect) and g.layer == layer)


@pytest.mark.parametrize("offset", [(-3.2, -1.1), (4.3, 7.2)])
def test_body_contours_use_absolute_corner_and_leave_pads_unchanged(offset):
    base = dict(rows=2, cols=4, pitch=2.0, row_spacing=4.0)
    original = build_pin_header(PinHeaderParams(**base))
    fp = build_pin_header(PinHeaderParams(
        **base, body_length=12.5, body_width=8.0,
        body_offset_x=offset[0], body_offset_y=offset[1],
    ))
    for layer in ("F.SilkS", "F.Fab"):
        rect = _rect(fp, layer)
        assert rect.start == offset
        assert rect.end == pytest.approx((offset[0] + 12.5, offset[1] + 8.0))
    assert fp.pads == original.pads


def test_courtyard_unites_asymmetric_body_and_protruding_pads():
    fp = build_pin_header(PinHeaderParams(
        rows=2, cols=3, pitch=3.0, row_spacing=5.0, pad_diameter=2.0,
        body_length=9.0, body_width=3.0, body_offset_x=-4.0,
        body_offset_y=1.0, margin=0.25,
    ))
    rect = _rect(fp, "F.CrtYd")
    assert rect.start == (-4.25, -1.25)
    assert rect.end == (7.25, 6.25)
    assert rect.width == 0.05


def test_sparse_mapped_pads_only_contribute_when_populated():
    fp = build_pin_header(PinHeaderParams(
        rows=2, cols=8, pitch=2.0, row_spacing=5.0, pad_diameter=2.0,
        pin_map={1: (1, 3), 2: (0, 7)}, populated_pins={1},
        body_length=2.0, body_width=2.0, margin=0.25,
    ))
    assert [(p.number, p.at, p.shape) for p in fp.pads] == [("1", (6.0, 5.0), "rect")]
    rect = _rect(fp, "F.CrtYd")
    assert rect.start == (-0.25, -0.25)
    assert rect.end == (7.25, 6.25)


@pytest.mark.parametrize("body", [False, True])
def test_empty_population_has_finite_courtyard(body):
    extra = dict(body_length=5.0, body_width=3.0) if body else {}
    fp = build_pin_header(PinHeaderParams(rows=1, cols=1, populated_pins=set(), **extra))
    assert not fp.pads
    rect = _rect(fp, "F.CrtYd")
    assert rect.start == ((-1.0, -1.0) if body else (-2.0, -2.0))
    assert rect.end == ((6.0, 4.0) if body else (2.0, 2.0))


def test_legacy_silk_and_marker_survive_and_large_pads_fit_courtyard():
    fp = build_pin_header(PinHeaderParams(
        rows=2, cols=3, pitch=2.0, row_spacing=5.0,
        pad_diameter=4.0, margin=0.25, body_offset_x=99.0,
    ))
    silk = _rect(fp, "F.SilkS")
    assert silk.start == (-0.25, -0.25)
    assert silk.end == (4.25, 5.25)
    marker = next(g for g in fp.graphics if isinstance(g, FpCircle))
    assert marker.center == (-0.75, -0.75)
    assert not any(g.layer == "F.Fab" for g in fp.graphics)
    courtyard = _rect(fp, "F.CrtYd")
    assert courtyard.start == (-2.25, -2.25)
    assert courtyard.end == (6.25, 7.25)


@pytest.mark.parametrize("field", [
    "pitch", "row_spacing", "drill", "pad_diameter", "margin",
    "body_length", "body_width", "body_offset_x", "body_offset_y",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_geometry_rejected(field, value):
    params = PinHeaderParams(body_length=5.0, body_width=3.0)
    setattr(params, field, value)
    with pytest.raises(ValueError, match=field):
        build_pin_header(params)


@pytest.mark.parametrize("body", [
    dict(body_length=5.0), dict(body_width=3.0),
    dict(body_length=0.0, body_width=3.0),
    dict(body_length=5.0, body_width=-3.0),
])
def test_incomplete_or_nonpositive_body_rejected(body):
    with pytest.raises(ValueError, match="body_length"):
        build_pin_header(PinHeaderParams(**body))


def test_negative_margin_rejected():
    with pytest.raises(ValueError, match="margin"):
        build_pin_header(PinHeaderParams(margin=-0.1))


def test_overflowing_body_bounds_rejected():
    with pytest.raises(ValueError, match="finite"):
        build_pin_header(PinHeaderParams(
            body_length=1e308, body_width=3.0, body_offset_x=1e308,
        ))


def test_serialized_footprint_contains_body_and_courtyard_layers():
    text = build_pin_header(PinHeaderParams(body_length=8.0, body_width=4.0)).render()
    for layer in ("F.Fab", "F.SilkS", "F.CrtYd"):
        assert f'(layer "{layer}")' in text
    assert '(start 0 0)' in text
    assert '(end 8 4)' in text
