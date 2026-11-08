"""@brief Typed snap marker shown while hovering with a drawing tool, in both canvases.

@details Plan docs/footprint-drawing-kicad-plan.md, B4 and contract 4: the
marker's shape says what the object snap caught. The shapes extend the ones
of `canvas_measure.anchor_marker` (ring = centre, square = corner, diamond =
edge foot) with the same screen size, cosmetic 1.6 px pen and default teal:

* ring: centre of a circle or arc;
* square: endpoint or corner;
* triangle: midpoint;
* diamond: edge (foot of the perpendicular);
* X cross: intersection of two segments;
* filled ring: pad centre or pin end;
* small circle with a tick: quadrant point of a circle.

Markers keep their pixel size at any zoom (ItemIgnoresTransformations), take
no mouse button and no hover, and carry a data tag so the host removes
exactly its own markers with `remove_snap_markers`.
"""
from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from .canvas_measure import MEASURE_COLOR, MEASURE_TRANSIENT_Z

#: Snap kinds, named as in the object snap of plan B1.
KIND_PAD_CENTRE = "pad_centre"
KIND_PIN_END = "pin_end"
KIND_ENDPOINT = "endpoint"
KIND_CORNER = "corner"
KIND_MIDPOINT = "midpoint"
KIND_CENTRE = "centre"
KIND_QUADRANT = "quadrant"
KIND_INTERSECTION = "intersection"
KIND_EDGE = "edge"
#: The kind `kicadgen.core.object_snap.PAD` returns for a pad or pin hot point:
#: drawn as the filled ring, so a snap result feeds the marker unchanged.
KIND_PAD = "pad"

SNAP_KINDS = (KIND_PAD, KIND_PAD_CENTRE, KIND_PIN_END, KIND_ENDPOINT, KIND_CORNER, KIND_MIDPOINT,
              KIND_CENTRE, KIND_QUADRANT, KIND_INTERSECTION, KIND_EDGE)

#: Item data role carrying the marker tag (value: the caller's tag, or True).
SNAP_MARKER_ROLE = 0x534E4150

#: Half size of the markers in screen px: 12 px wide, as `anchor_marker`.
_HALF = 6.0

#: Opacity (0-255) of the filled ring's fill: the outline stays opaque.
_FILL_ALPHA = 120


def snap_marker_path(kind: str) -> QPainterPath:
    """@brief Outline of a snap marker in screen px, centred on (0, 0).

    @param kind One of SNAP_KINDS.
    @return The path (open strokes for the crosses and the tick).
    @throws ValueError For an unknown kind: a marker that does not say what
        was caught would break contract 4.
    """
    h = _HALF
    path = QPainterPath()
    if kind in (KIND_CENTRE, KIND_PAD, KIND_PAD_CENTRE, KIND_PIN_END):
        path.addEllipse(-h, -h, 2 * h, 2 * h)
    elif kind in (KIND_ENDPOINT, KIND_CORNER):
        path.addRect(-5.0, -5.0, 10.0, 10.0)
    elif kind == KIND_MIDPOINT:
        path.moveTo(0.0, -h)
        path.lineTo(h, 5.0)
        path.lineTo(-h, 5.0)
        path.closeSubpath()
    elif kind == KIND_EDGE:
        path.moveTo(0.0, -h)
        path.lineTo(h, 0.0)
        path.lineTo(0.0, h)
        path.lineTo(-h, 0.0)
        path.closeSubpath()
    elif kind == KIND_INTERSECTION:
        path.moveTo(-5.0, -5.0)
        path.lineTo(5.0, 5.0)
        path.moveTo(-5.0, 5.0)
        path.lineTo(5.0, -5.0)
    elif kind == KIND_QUADRANT:
        path.addEllipse(-4.0, -4.0, 8.0, 8.0)
        path.moveTo(0.0, -4.0)
        path.lineTo(0.0, -8.0)
    else:
        raise ValueError(f"unknown snap kind: {kind!r}")
    return path


def make_snap_marker_items(kind: str, x: float, y: float, colour: str = MEASURE_COLOR, *,
                           tag: Any = None, z: float = MEASURE_TRANSIENT_Z, scene=None) -> list:
    """@brief The items of one snap marker at a scene point.

    @param kind One of SNAP_KINDS.
    @param x Scene x of the snapped point (scene units of the host: px of
        the footprint scene, symbol scene units).
    @param y Scene y of the snapped point.
    @param colour Stroke colour (the filled ring uses it translucent inside).
    @param tag Value stored under SNAP_MARKER_ROLE (True when None), for
        `remove_snap_markers`.
    @param z Z value (default: above pads, drawings and committed measures).
    @param scene Optional scene the items are added to.
    @return A list of QGraphicsItem (one path item per marker today; a list
        so a later shape may add a halo without changing callers).
    @throws ValueError For an unknown kind (from `snap_marker_path`).
    """
    item = QGraphicsPathItem(snap_marker_path(kind))
    pen = QPen(QColor(colour), 1.6)
    pen.setCosmetic(True)
    item.setPen(pen)
    if kind in (KIND_PAD, KIND_PAD_CENTRE, KIND_PIN_END):
        fill = QColor(colour)
        fill.setAlpha(_FILL_ALPHA)
        item.setBrush(QBrush(fill))
    else:
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
    item.setPos(float(x), float(y))
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
    item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    item.setAcceptHoverEvents(False)
    item.setData(SNAP_MARKER_ROLE, True if tag is None else tag)
    item.setZValue(z)
    items = [item]
    if scene is not None:
        for each in items:
            scene.addItem(each)
    return items


def remove_snap_markers(scene, tag: Optional[Any] = None) -> int:
    """@brief Remove snap markers from a scene.

    @param scene The scene.
    @param tag Remove only markers stored with this tag; None removes every snap marker.
    @return Number of items removed.
    """
    removed = 0
    for item in list(scene.items()):
        value = item.data(SNAP_MARKER_ROLE)
        if value is None:
            continue
        if tag is not None and value != tag:
            continue
        scene.removeItem(item)
        removed += 1
    return removed
