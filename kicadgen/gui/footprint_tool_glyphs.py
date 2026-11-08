"""@brief Palette glyphs of the footprint-only drawing tools: text box, the five dimension types, fallbacks.

@details Plan docs/footprint-drawing-kicad-plan.md, A2 and D1-D4. The shared
`symbol_glyphs.shape_tool_glyphs` covers the shape tools both tabs have
(line, polygon, rectangle, circle, arc, Bezier, text); the footprint adds the
text box and the dimension types, which the symbol editor does not offer
(plan §4: dimensions and text box are footprint only). The glyphs are plain
`symbol_glyphs.Glyph` records drawn by `symbol_tab.glyph_icon`, so the icons
share one renderer, one stroke style and the per-layer tint of the footprint.

The module is pure (no Qt): coordinates are symbol millimetres, Y up, as
every glyph; only the proportions matter, the icon renderer scales them to fit.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from ..generators.symbol_glyphs import SHAPE_WIDTH_MM, Glyph, Primitive

Point = Tuple[float, float]

#: Palette key of the one «Quota» drop-down button (the variants carry their own keys).
DIMENSION_BUTTON_KEY = "dimension"

#: Dimension variants of the «Quota» drop-down, in KiCad's order:
#: (variant key, Italian source label). The variant key of the aligned
#: dimension differs from the button key because a drop-down variant may not
#: reuse its parent's key (`ToolGrid.add_tool`).
DIMENSION_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("dimension_aligned", "Quota allineata"),
    ("dimension_orthogonal", "Quota ortogonale"),
    ("dimension_radial", "Quota radiale"),
    ("dimension_center", "Segno di centro"),
    ("dimension_leader", "Richiamo"),
)

#: Length, glyph mm, of an arrowhead stroke in the icons (proportion only).
_ARROW = 1.0
#: Half opening angle of an icon arrowhead, degrees (KiCad draws 27.5).
_ARROW_DEG = 27.5


def _line(*points: Point) -> Primitive:
    """An open polyline primitive with the shared shape stroke."""
    return Primitive("polyline", tuple(points), 0.0, "none", SHAPE_WIDTH_MM)


def _arrow(tip: Point, towards: Point) -> List[Primitive]:
    """The two strokes of an arrowhead at `tip` opening towards `towards`."""
    dx, dy = towards[0] - tip[0], towards[1] - tip[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    out = []
    for sign in (1.0, -1.0):
        a = math.radians(sign * _ARROW_DEG)
        rx, ry = ux * math.cos(a) - uy * math.sin(a), ux * math.sin(a) + uy * math.cos(a)
        out.append(_line(tip, (tip[0] + rx * _ARROW, tip[1] + ry * _ARROW)))
    return out


def _linear_dimension(a: Point, b: Point, offset: float) -> Tuple[Primitive, ...]:
    """Extension lines, dimension line and arrowheads of a linear dimension from a to b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    a2 = (a[0] + nx * offset, a[1] + ny * offset)
    b2 = (b[0] + nx * offset, b[1] + ny * offset)
    over = 0.5 if offset >= 0 else -0.5
    return tuple([
        _line(a, (a2[0] + nx * over, a2[1] + ny * over)),
        _line(b, (b2[0] + nx * over, b2[1] + ny * over)),
        _line(a2, b2),
        *_arrow(a2, b2),
        *_arrow(b2, a2),
    ])


def _glyph(key: str, label: str, primitives) -> Glyph:
    """A palette glyph of group "tool" with no terminals."""
    return Glyph(key, label, tuple(primitives), (), "", group="tool")


def textbox_glyph() -> Glyph:
    """@brief The text box tool: a frame with three text lines inside.

    @return Glyph «Casella di testo» (key "textbox").
    """
    return _glyph("textbox", "Casella di testo", (
        Primitive("rectangle", ((0.0, 0.0), (5.08, 3.81)), 0.0, "none", SHAPE_WIDTH_MM),
        _line((0.7, 2.9), (4.2, 2.9)),
        _line((0.7, 1.9), (3.6, 1.9)),
        _line((0.7, 0.9), (4.0, 0.9)),
    ))


def polygon_glyph() -> Glyph:
    """@brief Fallback polygon icon, used when the shared shape glyphs have no polygon.

    @return Glyph «Poligono» (key "polygon"): a closed five-sided outline.
    """
    return _glyph("polygon", "Poligono", (
        _line((0.0, 1.27), (2.54, 3.81), (5.08, 2.54), (4.445, 0.0), (1.27, 0.0), (0.0, 1.27)),
    ))


def bezier_glyph() -> Glyph:
    """@brief Fallback Bezier icon, used when the shared shape glyphs have no Bezier.

    @return Glyph «Bézier» (key "bezier").
    """
    return _glyph("bezier", "Bézier", (
        Primitive("bezier", ((0, 0), (1.27, 5.08), (3.81, -2.54), (5.08, 2.54)), 0.0, "none", SHAPE_WIDTH_MM),
    ))


def dimension_glyphs() -> Dict[str, Glyph]:
    """@brief One icon per dimension variant of `DIMENSION_VARIANTS`.

    @return Mapping variant key -> Glyph: a slanted aligned dimension, a
        horizontal orthogonal one, a radius with its leader and shelf, a
        centre cross in a circle, an arrow with a leader shelf.
    """
    labels = dict(DIMENSION_VARIANTS)
    circle = Primitive("circle", ((2.0, 2.0),), 2.0, "none", SHAPE_WIDTH_MM)
    rim = (2.0 + 2.0 * math.sqrt(0.5), 2.0 + 2.0 * math.sqrt(0.5))
    knee = (5.0, 5.0)
    return {
        "dimension_aligned": _glyph("dimension_aligned", labels["dimension_aligned"],
                                    _linear_dimension((0.0, 0.0), (5.0, 3.0), 1.6)),
        "dimension_orthogonal": _glyph("dimension_orthogonal", labels["dimension_orthogonal"],
                                       _linear_dimension((0.0, 0.0), (5.08, 0.0), 2.54)),
        "dimension_radial": _glyph("dimension_radial", labels["dimension_radial"], (
            circle,
            _line((1.4, 2.0), (2.6, 2.0)), _line((2.0, 1.4), (2.0, 2.6)),
            _line(rim, knee), _line(knee, (6.5, 5.0)),
            *_arrow(rim, knee),
        )),
        "dimension_center": _glyph("dimension_center", labels["dimension_center"], (
            circle,
            _line((0.6, 2.0), (3.4, 2.0)), _line((2.0, 0.6), (2.0, 3.4)),
        )),
        "dimension_leader": _glyph("dimension_leader", labels["dimension_leader"], (
            _line((0.0, 0.0), (3.0, 3.0)), _line((3.0, 3.0), (5.5, 3.0)),
            *_arrow((0.0, 0.0), (3.0, 3.0)),
        )),
    }
