"""@brief Drawing origin of a canvas: a reference point for reading and typing coordinates.

A drawing origin is a VIEW aid (user, 2026-11-02: «mi basta come riferimento
per il disegno», a reference for the drawing is enough): it never changes the
file the canvas edits. The KiCad footprint keeps its own (0, 0); the canvas
only shows coordinates measured from the chosen point, with X to the right and
Y UPWARDS, as a dimensioned datasheet drawing measured from a body corner
reads them (KiCad's scene Y points down).

- `to_drawing_mm` / `from_drawing_mm`: scene px <-> drawing mm (Y up).
- `nearest_point`: the candidate closest to a point (the context menu offers it).
- `paint_origin_marker`: the marker, painted by the view in `drawForeground`
  so it survives the scene being cleared at every redraw.
- `readout_text`: the pointer line under the canvas, the same in both tabs.

The module holds no model and no translated string: the host chooses the
point, names it and formats the text.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QGraphicsView

#: Colour of the origin marker (the light accent of the dark canvases).
ORIGIN_MARKER_COLOR = '#f5c2e7'
#: Radius of the marker circle, device px (constant at every zoom).
ORIGIN_MARKER_RADIUS_PX = 5.0
#: Half length of the marker cross arms, device px.
ORIGIN_MARKER_ARM_PX = 10.0


def to_drawing_mm(point_px: tuple[float, float], origin_px: tuple[float, float],
                  mm_to_px: float) -> tuple[float, float]:
    """@brief Coordinates of a scene point measured from the drawing origin.

    @param point_px Scene point (x, y), px, Y down.
    @param origin_px Drawing origin in the scene, px.
    @param mm_to_px Scene px per mm (> 0).
    @return (x, y) in mm, X to the right and Y UPWARDS from the origin.
    @throws ValueError When `mm_to_px` is not a positive finite number.
    """
    if not (math.isfinite(mm_to_px) and mm_to_px > 0):
        raise ValueError("mm_to_px must be a positive finite number")
    return ((point_px[0] - origin_px[0]) / mm_to_px, -(point_px[1] - origin_px[1]) / mm_to_px)


def from_drawing_mm(point_mm: tuple[float, float], origin_px: tuple[float, float],
                    mm_to_px: float) -> tuple[float, float]:
    """@brief Scene point of drawing coordinates; the inverse of `to_drawing_mm`.

    @param point_mm (x, y) in mm, X right and Y up from the drawing origin.
    @param origin_px Drawing origin in the scene, px.
    @param mm_to_px Scene px per mm (> 0).
    @return Scene point (x, y), px, Y down.
    @throws ValueError When `mm_to_px` is not a positive finite number.
    """
    if not (math.isfinite(mm_to_px) and mm_to_px > 0):
        raise ValueError("mm_to_px must be a positive finite number")
    return (origin_px[0] + point_mm[0] * mm_to_px, origin_px[1] - point_mm[1] * mm_to_px)


def nearest_point(candidates: dict, point: tuple[float, float]):
    """@brief Key of the candidate closest to `point`.

    @param candidates Mapping key -> (x, y), all in the same units as `point`.
    @param point The reference point.
    @return The key at the smallest distance, None without candidates. Ties
        keep the first key in the mapping's order (the host lists the
        preferred points first).
    """
    best, best_distance = None, math.inf
    for key, (x, y) in candidates.items():
        distance = math.hypot(x - point[0], y - point[1])
        if distance < best_distance:
            best, best_distance = key, distance
    return best


def paint_origin_marker(painter: QPainter, view: QGraphicsView, origin: QPointF | None,
                        color: str = ORIGIN_MARKER_COLOR) -> None:
    """@brief Paint the origin marker (a circle and a cross) at `origin`.

    @param painter Painter in scene coordinates (the view's `drawForeground`).
    @param view The view: its scale turns the device-px sizes into scene units.
    @param origin Scene point of the drawing origin; None paints nothing.
    @param color Marker colour.
    @note Cosmetic 1 px pens and sizes in device px: the marker keeps its
        size at every zoom, as KiCad's anchor marker. The painter state is
        saved and restored.
    """
    if origin is None:
        return
    scale = abs(view.transform().m11()) or 1.0
    radius, arm = ORIGIN_MARKER_RADIUS_PX / scale, ORIGIN_MARKER_ARM_PX / scale
    painter.save()
    try:
        pen = QPen(QColor(color))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawEllipse(QRectF(origin.x() - radius, origin.y() - radius, 2 * radius, 2 * radius))
        painter.drawLine(QLineF(origin.x() - arm, origin.y(), origin.x() + arm, origin.y()))
        painter.drawLine(QLineF(origin.x(), origin.y() - arm, origin.x(), origin.y() + arm))
    finally:
        painter.restore()


def readout_text(point_mm, scale: float) -> str:
    """@brief The pointer line under a canvas: «X … Y … mm  |  zoom%».

    @param point_mm (x, y) in mm, Y UP, of the last pointer position, or None
        before the pointer has been over the canvas (only the zoom is shown).
    @param scale The view's horizontal scale (`transform().m11()`): the zoom
        percentage is `scale * 100`.
    @return The text both tabs show under their canvas (symbol editor since
        before 2026-11-02, footprint preview from then on), same format.
    """
    value = '' if point_mm is None else f'X {point_mm[0]:.2f}  Y {point_mm[1]:.2f} mm  |  '
    return value + f'{scale * 100:.0f}%'
