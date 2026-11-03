"""@brief Regression tests for optional THT bodies and union courtyards."""

import pytest

import math

from kicadgen.core.footprint import SILK_OUTSIDE_FAB_MM, FpCircle, FpLine, FpRect
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header


def _rect(fp, layer):
    return next(g for g in fp.graphics if isinstance(g, FpRect) and g.layer == layer)


def _silk_segments(fp):
    """Silk outline as segments, whether written as one FpRect or as clipped FpLines."""
    segments = []
    for g in fp.graphics:
        if g.layer != "F.SilkS":
            continue
        if isinstance(g, FpRect):
            (x0, y0), (x1, y1) = g.start, g.end
            c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            segments += [(c[i], c[(i + 1) % 4], g.width) for i in range(4)]
        elif isinstance(g, FpLine):
            segments.append((g.start, g.end, g.width))
    return segments


def _segment_to_point(a, b, p):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _assert_silk_clear_of_pads(fp, clearance=0.2):
    """No silk ink closer than `clearance` to any (round) pad copper."""
    for a, b, width in _silk_segments(fp):
        for pad in fp.pads:
            gap = _segment_to_point(a, b, pad.at) - pad.size[0] / 2 - width / 2
            assert gap >= clearance - 1e-6, (a, b, pad.number, gap)


def _on_outline(point, start, end):
    (x, y), (x0, y0), (x1, y1) = point, start, end
    on_x = abs(x - x0) < 1e-6 or abs(x - x1) < 1e-6
    on_y = abs(y - y0) < 1e-6 or abs(y - y1) < 1e-6
    return (on_x and y0 - 1e-6 <= y <= y1 + 1e-6) or (on_y and x0 - 1e-6 <= x <= x1 + 1e-6)


@pytest.mark.parametrize("offset", [(-3.2, -1.1), (4.3, 7.2)])
def test_body_contours_use_absolute_corner_and_leave_pads_unchanged(offset):
    base = dict(rows=2, cols=4, pitch=2.0, row_spacing=4.0)
    original = build_pin_header(PinHeaderParams(**base))
    fp = build_pin_header(PinHeaderParams(
        **base, body_length=12.5, body_width=8.0,
        body_offset_x=offset[0], body_offset_y=offset[1],
    ))
    rect = _rect(fp, "F.Fab")
    assert rect.start == offset
    assert rect.end == pytest.approx((offset[0] + 12.5, offset[1] + 8.0))
    # CONTRACT CHANGED 2026-09-25: the silk outline follows the same body
    # contour but is interrupted where it would come within the silk-to-pad
    # clearance of a pad (offset (-3.2, -1.1) puts the top edge 0.25 mm from
    # the pad copper, less than clearance + half stroke). Every silk piece
    # still lies on the body contour, and no ink is on copper.
    # CONTRACT CHANGED 2026-11-03 (KLC F5.1/F5.2): the silk contour lies
    # SILK_OUTSIDE_FAB_MM (0.11 mm, half of each stroke) outside the F.Fab
    # body outline instead of on it.
    g = SILK_OUTSIDE_FAB_MM
    end = (offset[0] + 12.5 + g, offset[1] + 8.0 + g)
    start = (offset[0] - g, offset[1] - g)
    for a, b, _ in _silk_segments(fp):
        assert _on_outline(a, start, end) and _on_outline(b, start, end)
    _assert_silk_clear_of_pads(fp)
    assert fp.pads == original.pads


def test_courtyard_unites_asymmetric_body_and_protruding_pads():
    fp = build_pin_header(PinHeaderParams(
        rows=2, cols=3, pitch=3.0, row_spacing=5.0, pad_diameter=2.0,
        body_length=9.0, body_width=3.0, body_offset_x=-4.0,
        body_offset_y=1.0, margin=0.25,
    ))
    rect = _rect(fp, "F.CrtYd")
    # CONTRACT CHANGED 2026-11-03 (second review, N4): the courtyard encloses
    # the pin-1 dot's ink too; the dot lies outside the pin-1 corner, so the
    # courtyard grows on that side only.
    assert rect.start == (-5.17, -1.25)
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
    # CONTRACT CHANGED 2026-11-03: the default courtyard margin is the KLC
    # connector value 0.5 mm (was 1.0 mm; user decision «adeguiamoci»);
    # without a body and without pads the silk frames the declared grid at
    # the silk clearance plus half a stroke (0.26 mm), and the courtyard adds
    # the margin: 0.76 mm.
    # CONTRACT CHANGED 2026-11-03 (second review, N4): the courtyard encloses
    # the pin-1 dot's ink too; the dot lies outside the pin-1 corner, so the
    # courtyard grows on that side only.
    # The unpopulated pin 1 keeps its mark at its grid position (N3).
    assert rect.start == ((-1.42, -1.42) if body else (-1.57, -1.57))
    assert rect.end == ((5.5, 3.5) if body else (0.76, 0.76))


def test_legacy_silk_and_marker_survive_and_large_pads_fit_courtyard():
    """@brief Large pads, small margin: silk and marker stay off the copper.

    CONTRACT CHANGED 2026-09-25. This test used to lock the legacy silk
    rectangle (-0.25, -0.25)-(4.25, 5.25) and the marker at (-0.75, -0.75),
    both lying ON the 4 mm pads (a 0.25 mm margin is far less than the 2 mm
    pad radius): KiCad's DRC flags silk over copper and the fabricator clips
    it. Now the outline keeps only the pieces clear of the pads and the
    marker is pushed diagonally out of the pin-1 corner until it clears them.
    """
    # CONTRACT CHANGED 2026-09-26 (punto 84 medio 9): pad da 4 mm a passo 2 mm
    # si sovrapponevano e ora sono rifiutati (distanza minima di rame 0.2 mm).
    # 1.8 mm e' il pad piu' grande ammesso a questo passo: resta molto piu'
    # grande del margine di 0.25 mm, quindi il caso provato (serigrafia e
    # marcatore sopra il rame) e' lo stesso.
    fp = build_pin_header(PinHeaderParams(
        rows=2, cols=3, pitch=2.0, row_spacing=5.0,
        pad_diameter=1.8, margin=0.25, body_offset_x=99.0,
    ))
    # CONTRACT CHANGED 2026-11-03: the margin is no longer also the offset
    # of the silk frame of a body-less header. The frame is derived from the
    # emitted pads (copper envelope + 0.2 mm clearance + half stroke), so it
    # is whole: one rectangle, never cut, no ink on copper.
    frame = (-0.9 - 0.26, -0.9 - 0.26), (4.9 + 0.26, 5.9 + 0.26)
    assert [type(g).__name__ for g in fp.graphics if g.layer == "F.SilkS"].count("FpRect") == 1
    for a, b, _ in _silk_segments(fp):
        assert _on_outline(a, *frame)
        assert _on_outline(b, *frame)
    _assert_silk_clear_of_pads(fp)
    marker = next(g for g in fp.graphics if isinstance(g, FpCircle))
    pad1 = next(p for p in fp.pads if p.number == "1")
    assert marker.center[0] < -0.75 and marker.center[1] < -0.75      # same corner, further out
    assert marker.center[0] == pytest.approx(marker.center[1])        # along the diagonal
    for pad in fp.pads:
        assert math.dist(marker.center, pad.at) - pad.size[0] / 2 - marker.radius >= 0.2 - 1e-6
    assert math.dist(marker.center, pad1.at) == min(math.dist(marker.center, p.at) for p in fp.pads)
    # CONTRACT CHANGED 2026-10-14 (L-3): every footprint carries the KLC
    # ${REFERENCE} text on F.Fab; without a body there is still no F.Fab OUTLINE.
    assert not any(g.layer == "F.Fab" for g in fp.graphics if type(g).__name__ != "FpText")
    courtyard = _rect(fp, "F.CrtYd")
    # CONTRACT CHANGED 2026-11-03: the silk frame (see above) plus the margin.
    assert courtyard.end == pytest.approx((5.41, 6.41))
    # CONTRACT CHANGED 2026-11-03 (second review, N4): the courtyard encloses
    # the pin-1 dot's ink too; the dot lies outside the pin-1 corner, so the
    # courtyard grows on that side only.
    assert courtyard.start == pytest.approx((-2.22, -2.22))


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
    # Localized since HANDOFF 130 C8: the Italian source names the body, not the field.
    with pytest.raises(ValueError, match="corpo"):
        build_pin_header(PinHeaderParams(**body))


def test_negative_margin_rejected():
    with pytest.raises(ValueError, match="margin"):
        build_pin_header(PinHeaderParams(margin=-0.1))


def test_overflowing_body_bounds_rejected():
    with pytest.raises(ValueError, match="finiti"):
        build_pin_header(PinHeaderParams(
            body_length=1e308, body_width=3.0, body_offset_x=1e308,
        ))


def test_serialized_footprint_contains_body_and_courtyard_layers():
    text = build_pin_header(PinHeaderParams(body_length=8.0, body_width=4.0)).render()
    for layer in ("F.Fab", "F.SilkS", "F.CrtYd"):
        assert f'(layer "{layer}")' in text
    assert '(start 0 0)' in text
    assert '(end 8 4)' in text
