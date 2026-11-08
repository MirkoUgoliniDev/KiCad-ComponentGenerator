"""@brief Typed snap markers (plan footprint-drawing-kicad B4, contract 4)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene

from kicadgen.gui.canvas_snap_marker import (KIND_CENTRE, KIND_PAD_CENTRE, SNAP_KINDS, SNAP_MARKER_ROLE,
                                             make_snap_marker_items, remove_snap_markers, snap_marker_path)


def test_every_kind_gives_one_screen_constant_inert_marker(qapp):
    for kind in SNAP_KINDS:
        items = make_snap_marker_items(kind, 10.0, -4.0)
        assert len(items) == 1, kind
        item = items[0]
        assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        assert item.acceptedMouseButtons() == Qt.MouseButton.NoButton
        assert item.pen().isCosmetic()
        assert (item.pos().x(), item.pos().y()) == (10.0, -4.0)


def test_shapes_tell_the_kinds_apart(qapp):
    # Kinds sharing an outline on purpose: corner = endpoint, pin end = pad
    # centre, and the pad ring is the centre ring filled (checked below).
    shared = {"corner": "endpoint", "pin_end": "pad_centre", "pad_centre": "centre", "pad": "pad_centre"}
    outlines = {}
    for kind in SNAP_KINDS:
        if kind in shared:
            continue
        path = snap_marker_path(kind)
        outlines[kind] = (path.elementCount(), round(path.boundingRect().width(), 2),
                          round(path.boundingRect().height(), 2), round(path.boundingRect().top(), 2))
    assert len(set(outlines.values())) == len(outlines)
    # A pad ring is filled, a plain centre ring is not, though the outline is the same.
    pad = make_snap_marker_items(KIND_PAD_CENTRE, 0, 0)[0]
    centre = make_snap_marker_items(KIND_CENTRE, 0, 0)[0]
    assert pad.brush().style() != Qt.BrushStyle.NoBrush
    assert centre.brush().style() == Qt.BrushStyle.NoBrush


def test_markers_keep_their_pixel_size_at_any_zoom(qapp):
    from PyQt6.QtWidgets import QGraphicsView
    scene = QGraphicsScene()
    item = make_snap_marker_items("midpoint", 0, 0, scene=scene)[0]
    view = QGraphicsView(scene)
    sizes = []
    for factor in (1.0, 8.0):
        view.resetTransform()
        view.scale(factor, factor)
        sizes.append(item.deviceTransform(view.viewportTransform()).mapRect(item.boundingRect()).width())
    assert sizes[0] == pytest.approx(sizes[1])


def test_unknown_kind_is_refused(qapp):
    with pytest.raises(ValueError):
        make_snap_marker_items("nearest", 0, 0)


def test_remove_takes_only_the_tagged_markers(qapp):
    scene = QGraphicsScene()
    make_snap_marker_items("endpoint", 0, 0, tag="hover", scene=scene)
    make_snap_marker_items("edge", 1, 1, tag="other", scene=scene)
    plain = scene.addRect(0, 0, 1, 1)
    assert remove_snap_markers(scene, "hover") == 1
    assert [i.data(SNAP_MARKER_ROLE) for i in scene.items() if i is not plain] == ["other"]
    assert remove_snap_markers(scene) == 1
    assert scene.items() == [plain]
