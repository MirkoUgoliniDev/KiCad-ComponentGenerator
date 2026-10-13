"""@brief Preview regressions: header pads must show the same copper and drills as KiCad."""

import pytest
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsSimpleTextItem

from kicadgen.core.package_geometry import PackageGeometry, PinSpec
from kicadgen.generators.host_mounting import header_footprint_params
from kicadgen.generators.package import build_package
from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
from kicadgen.gui.theme import BASE, BLUE
from tests.test_contact_style import dev_kit


def test_header_preview_has_44_circular_pads_and_visible_drill_openings(window):
    host = header_footprint_params(dev_kit(), 1.7, 1.0)
    window._draw_preview_package(host)
    items = window.preview_scene.items()
    copper = [item for item in items if item.toolTip()
              and not isinstance(item, QGraphicsSimpleTextItem)
              and item.data(TEXT_KEY_ROLE) is None]
    holes = [item for item in items if isinstance(item, QGraphicsEllipseItem)
             and item.brush().color().name() == BASE]
    # Pin numbers only: the footprint's Reference/Value texts (draggable,
    # with a tooltip of their own since 2026-10-13) are not pin labels.
    labels = [item for item in items if isinstance(item, QGraphicsSimpleTextItem)
              and item.toolTip() and item.data(0) != 'pin-function'
              and item.data(TEXT_KEY_ROLE) is None]
    assert len(copper) == len(holes) == len(labels) == 44
    assert {item.text() for item in labels} == {str(n) for n in range(1, 45)}
    annotations = [item for item in items if isinstance(item, QGraphicsSimpleTextItem)
                   and item.data(0) == 'pin-function']
    assert all(item.data(1) in {str(n) for n in range(1, 45)} for item in annotations)
    exported = build_package(host)
    for pad in exported.pads:
        center = (pad.at[0] * 20, pad.at[1] * 20)
        ring = next(item for item in copper if item.rect().center().x() == pytest.approx(center[0])
                    and item.rect().center().y() == pytest.approx(center[1]))
        assert isinstance(ring, QGraphicsEllipseItem)
        assert ring.brush().color().name() == BLUE
        assert ring.rect().width() == pytest.approx(pad.size[0] * 20)
        hole = next(item for item in holes if item.rect().center() == ring.rect().center())
        assert hole.rect().width() == pytest.approx(pad.drill * 20)
        assert hole.zValue() > ring.zValue()
        assert not any(label.sceneBoundingRect().intersects(hole.sceneBoundingRect())
                       for label in labels + annotations)


@pytest.mark.parametrize('shape', ['oval', 'roundrect'])
def test_rotated_package_pad_retains_rounded_shape_and_has_no_spurious_drill(window, shape):
    host = PackageGeometry(pins=[PinSpec('1', 0, 0, 3, 1, shape=shape, rotation_deg=30)])
    window._draw_preview_package(host)
    pads = [item for item in window.preview_scene.items()
            if isinstance(item, QGraphicsPathItem) and item.toolTip()]
    assert len(pads) == 1
    assert pads[0].rotation() == -30
    assert not [item for item in window.preview_scene.items() if isinstance(item, QGraphicsEllipseItem)]
