"""@brief Leads counted on the photograph shape the 3D body, whatever its function.

User, 2026-09-27: SOT transistors of unknown function (class semiconductor_unknown,
kind "ic", no package) were rendered as bare blocks although the photograph showed
two leads on one side and one on the other. The shape now follows the observed
leads (`lead_sides_local`), moved into the builder's local frame with the same
transformation as the observed terminal boxes (`_local_box`).
"""
import math

import pytest

from kicadgen.cad3d.lead_sides import local_lead_sides
from kicadgen.services.photo_terminals import _local_box
from tests.test_fixE_parts_orientation import parts, flip_y  # noqa: F401 - fixture condivise

_PROBE_BOXES = {'left': (0.0, 0.45, 0.05, 0.55), 'right': (0.95, 0.45, 1.0, 0.55),
                'top': (0.45, 0.0, 0.55, 0.05), 'bottom': (0.45, 0.95, 0.55, 1.0)}


def _side_of(box):
    left, top, right, bottom = box
    x, y = (left + right) / 2, (top + bottom) / 2
    return 'left' if x < .25 else 'right' if x > .75 else 'top' if y < .25 else 'bottom'


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
@pytest.mark.parametrize('side', ['left', 'right', 'top', 'bottom'])
def test_sides_move_exactly_like_the_observed_terminal_boxes(rotation, side):
    """Structural: one convention for boxes and side counts, at every quarter turn."""
    expected = _side_of(_local_box(_PROBE_BOXES[side], rotation))
    assert local_lead_sides({side: 2}, rotation) == {expected: 2}


def test_a_quarter_turn_is_not_the_identity_and_a_half_turn_swaps_opposites():
    assert local_lead_sides({'left': 2, 'right': 1}, 0) == {'left': 2, 'right': 1}
    assert local_lead_sides({'left': 2, 'right': 1}, 180) == {'right': 2, 'left': 1}
    turned = local_lead_sides({'left': 2, 'right': 1}, 90)
    assert set(turned) == {'top', 'bottom'} and sum(turned.values()) == 3


def test_an_edge_measured_angle_takes_the_nearest_quarter_and_45_has_none():
    assert local_lead_sides({'left': 2}, 88.0) == local_lead_sides({'left': 2}, 90)
    assert local_lead_sides({'left': 2}, -3.0) == {'left': 2}
    assert local_lead_sides({'left': 2}, 45.0) is None


@pytest.mark.parametrize('bad', [None, {}, {'left': -1}, {'centre': 2}, {'left': 2.0},
                                 {'left': True}, {'left': 0, 'right': 0}])
def test_malformed_or_empty_counts_give_nothing(bad):
    assert local_lead_sides(bad, 0) is None


def _metal(result, parts):
    return [shape for shape, colour in result if colour == parts.METAL]


def _sot(**changes):
    component = {'name': 'C23 (semiconductor_unknown)', 'kind': 'ic', 'x': 0.0, 'y': 0.0,
                 'length_mm': 3.0, 'width_mm': 2.4, 'height_mm': 1.1,
                 'lead_sides_local': {'left': 2, 'right': 1}}
    component.update(changes)
    return component


def test_a_bare_ic_with_observed_leads_gets_them(parts):
    """Positive: function unknown, package empty, three leads seen -> three leads drawn."""
    result = parts.build_component(_sot(), 1.6)
    pads = _metal(result, parts)
    assert len(pads) == 3
    body = result[0][0].bounds()
    left = [p for p in pads if p.bounds()[2] <= body[0] + 1e-9]
    right = [p for p in pads if p.bounds()[0] >= body[2] - 1e-9]
    assert (len(left), len(right)) == (2, 1)


@pytest.mark.parametrize('kind', ['ic_sot', 'ic_soic', 'ic_qfp', 'generic'])
def test_every_leaded_kind_follows_the_observation(parts, kind):
    assert len(_metal(parts.build_component(_sot(kind=kind), 1.6), parts)) == 3


def test_without_an_observation_nothing_is_invented(parts):
    """Negative: no counted leads -> the plain envelope, exactly as before."""
    component = _sot()
    del component['lead_sides_local']
    result = parts.build_component(component, 1.6)
    assert len(result) == 1 and not _metal(result, parts)


@pytest.mark.parametrize('changes', [
    {'kind': 'resistor'}, {'kind': 'capacitor'}, {'kind': 'ic_qfn'},
    {'kind': 'generic', '_detected_kind': 'connector'},
    {'kind': 'generic', 'mating': 'edge'}])
def test_bodies_without_ordinary_leads_keep_their_own_shape(parts, changes, monkeypatch):
    """Negative: end caps, leadless pads and connectors never get gull-wing leads here."""
    called = []
    monkeypatch.setattr(parts, '_ic_observed_pins', lambda *a, **k: called.append(k) or [])
    parts.build_component(_sot(**changes), 1.6)
    assert not any(k.get('pins_by_side') == {'left': 2, 'right': 1} for k in called)


@pytest.mark.parametrize('image_rotation', [0, 90, 180, 270])
def test_the_two_lead_side_ends_where_the_photograph_saw_it(parts, flip_y, image_rotation):
    """End to end: photo side -> local side -> builder -> the caller's rotation.

    The component is seen with two leads on the image LEFT of its box. After the
    production reflection (`_flip_y` negates the rotation) and the rotation the
    FreeCAD script applies, the two pads must be left of the body centre in the
    photograph, whatever the component rotation.
    """
    local = local_lead_sides({'left': 2, 'right': 1}, image_rotation)
    data = {'body': {'offset_y_mm': 0.0, 'width_mm': 10.0},
            'components': [{'x': 0.0, 'y': 0.0, 'rotation_deg': image_rotation}]}
    cad_rotation = math.radians(flip_y(data)['components'][0]['rotation_deg'])
    result = parts._ic_observed_pins(0.0, 0.0, 0.0, 3.0, 2.4, 1.1, '#000', 'ic',
                                     pins_by_side=local)
    image_x = []
    for pad in _metal(result, parts):
        x0, y0, x1, y1 = pad.bounds()
        px, py = (x0 + x1) / 2, (y0 + y1) / 2           # local, Y up
        rx = px * math.cos(cad_rotation) - py * math.sin(cad_rotation)
        image_x.append(rx)                               # CAD X == photo X
    assert sorted(x < 0 for x in image_x).count(True) == 2
    assert sorted(x > 0 for x in image_x).count(True) == 1
