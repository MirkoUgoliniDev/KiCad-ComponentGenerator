"""@brief Qt text items drawn with KiCad's size, width and placement, shared by the symbol and footprint tabs.

KiCad draws texts with its stroke font; the editors draw them with the
desktop font, which is narrower or wider depending on the text (Segoe UI:
REF** about 3.3 mm instead of 3.9 mm at 1.0 mm). The user decided
(2026-10-16, «ok procedi») that every text of the symbol editor and of the
footprint preview is drawn at KiCad's width, text by text, and aligned
vertically with KiCad's own box, through ONE helper for both tabs (rule
«same UX in every tab»).

The item built here:

- has capitals `size` mm tall (the outline of «H», user decision 2026-10-14:
  KiCad's text size is the cap height, which is also the height of KiCad's
  stroke centreline of «H»);
- is stretched along its reading direction so the width of its layout box
  (the Qt glyph advances) equals KiCad's summed advance cells
  (`core.kicad_text.kicad_advance_width`), i.e. the user's rule
  «stretch = KiCad width / Qt width» without the pen margin, which KiCad
  adds as half a margin before the first cell and half after the last;
- is placed so its cells start where KiCad's start and its baseline lies on
  KiCad's baseline (`core.kicad_text.kicad_text_cells`), for left, centre
  and right and bottom, centre and top justification, unrotated or turned a
  quarter (reading up the page). The anchor therefore stays where the file
  puts it.

The remaining difference with KiCad is the glyph shapes and their side
bearings: measured against kicad-cli (tests/test_kicad_width_texts.py), the
visible ends of a text lie within about a fifth of the size of KiCad's.
"""
from __future__ import annotations

from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainterPath, QTextLayout, QTransform
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from ..core.kicad_text import SCHEMATIC_DEFAULT_PEN_MM, kicad_advance_width, kicad_text_cells

#: Pixel size of the reference font a text is laid out in before the item is
#: scaled to KiCad's size: large, so the measured cap height is precise and
#: the scale is not quantised by integer pixel sizes.
REFERENCE_PX = 100

#: Most entries each measurement cache keeps before it is emptied (the
#: symbol editor redraws on every drag step; a few thousand distinct texts
#: is far more than one symbol shows).
_CACHE_LIMIT = 4096

_cap_cache: dict = {}
_line_cache: dict = {}
#: The application whose fontDatabaseChanged signal empties the caches.
_watched_app = None


def clear_text_caches() -> None:
    """@brief Forget every cached cap height and baseline.

    @note Connected to QGuiApplication.fontDatabaseChanged: the caches are
        keyed by QFont.key(), which names the REQUESTED font; when fonts are
        added or removed the same key may resolve to another face.
    """
    _cap_cache.clear()
    _line_cache.clear()


def _watch_font_database() -> None:
    """Empty the caches whenever the application's available fonts change (once per application)."""
    global _watched_app
    from PyQt6.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    if app is not None and app is not _watched_app:
        app.fontDatabaseChanged.connect(clear_text_caches)
        _watched_app = app


def reference_font(base: QFont) -> QFont:
    """@brief The font a KiCad-sized text is laid out in: `base` at `REFERENCE_PX` pixels.

    @param base The tab's font (family, weight and style are kept).
    @return A copy of `base` with pixel size `REFERENCE_PX`.
    """
    font = QFont(base)
    font.setPixelSize(REFERENCE_PX)
    return font


def cap_height_px(font: QFont) -> float:
    """@brief Cap height of `font`, px: the height of the outline of «H» (cached per font).

    @param font The font to measure.
    @return The QPainterPath outline height of "H" (real glyph geometry,
        not the hinted capHeight metric); the tight bounding rect, the
        capHeight metric or 0.7 em when the outline is empty.
    """
    _watch_font_database()
    key = font.key()
    cap = _cap_cache.get(key)
    if cap is None:
        outline = QPainterPath()
        outline.addText(0.0, 0.0, font, "H")
        metrics = QFontMetricsF(font)
        cap = (outline.boundingRect().height() or metrics.tightBoundingRect("H").height()
               or metrics.capHeight() or 0.7 * max(1, font.pixelSize()))
        if len(_cap_cache) >= _CACHE_LIMIT:
            _cap_cache.clear()
        _cap_cache[key] = cap
    return cap


def text_baseline_px(text: str, font: QFont) -> float:
    """@brief Distance, item px, from the top of a QGraphicsSimpleTextItem to its baseline (cached).

    @param text The text (one line).
    @param font The item's font.
    @return The ascent of the single line QTextLayout lays out from y = 0,
        which is where the item draws its baseline (fallback fonts
        included); the font's ascent when no line can be laid out.
    """
    _watch_font_database()
    key = (font.key(), text)
    baseline = _line_cache.get(key)
    if baseline is None:
        layout = QTextLayout(text, font)
        layout.beginLayout()
        line = layout.createLine()
        layout.endLayout()
        baseline = line.ascent() if line.isValid() else QFontMetricsF(font).ascent()
        if len(_line_cache) >= _CACHE_LIMIT:
            _line_cache.clear()
        _line_cache[key] = baseline
    return baseline


def kicad_text_item(text: str, size: float, x: float, y: float, *, base_font: QFont, mm_to_px: float,
                    thickness: float = SCHEMATIC_DEFAULT_PEN_MM, justify: str = "left",
                    vjustify: str = "bottom", vertical: bool = False, colour=None) -> QGraphicsSimpleTextItem:
    """@brief An unparented text item drawn as KiCad draws the text: size, width and anchor.

    @param text The text (one line), as KiCad shows it.
    @param size KiCad text size, mm (= cap height).
    @param x Anchor, scene px (scene y grows down the page).
    @param y Anchor, scene px.
    @param base_font The tab's font; it is laid out at `REFERENCE_PX`.
    @param mm_to_px Scene px per mm.
    @param thickness Stroke thickness KiCad draws the text with, mm (the
        schematic default pen for symbol texts, the written thickness for
        footprint texts): it sets the half margin before the first cell.
    @param justify "left", "center" or "right", as in the KiCad file.
    @param vjustify "bottom", "center" or "top", as in the KiCad file.
    @param vertical True for a text turned 90 degrees (reads up the page,
        glyph tops towards -x), as KiCad turns it.
    @param colour Brush colour (QColor or anything QColor accepts); None
        keeps the item's default brush.
    @return A QGraphicsSimpleTextItem not in any scene: uniformly scaled so
        capitals are `size` mm tall, stretched along the reading direction
        by its base transform so its layout width equals KiCad's advance
        cells, rotated -90 when `vertical`, and positioned so its cells and
        baseline lie where KiCad lays them out (`kicad_text_cells`).
        sceneBoundingRect, shape and hit tests follow the stretch.
    @note Empty texts (no advance on either side) are not stretched.
    """
    font = reference_font(base_font)
    item = QGraphicsSimpleTextItem(text)
    item.setFont(font)
    if colour is not None:
        item.setBrush(QBrush(QColor(colour)))
    factor = float(size) * mm_to_px / cap_height_px(font)
    qt_width = item.boundingRect().width()
    kicad_width = kicad_advance_width(text, size) * mm_to_px
    stretch = kicad_width / (qt_width * factor) if qt_width > 0 and kicad_width > 0 else 1.0
    # Qt combines the rotation and scale properties first and the base
    # transform after them, in the parent's axes: the stretch goes on the
    # page axis the text reads along (y for a quarter-turned text). The
    # properties stay the uniform cap scale and the quarter turn, as before.
    item.setTransform(QTransform.fromScale(1.0, stretch) if vertical else QTransform.fromScale(stretch, 1.0))
    item.setScale(factor)
    left_mm, baseline_mm = kicad_text_cells(text, size, thickness, justify, vjustify)
    # Item origin (top-left of its layout) in the text's own frame, scene px:
    # `along` the reading direction and `up` towards the glyph tops.
    along = left_mm * mm_to_px
    up = baseline_mm * mm_to_px + text_baseline_px(text, font) * factor
    if vertical:
        item.setRotation(-90)
        item.setPos(x - up, y - along)
    else:
        item.setPos(x + along, y - up)
    return item


def kicad_text_stretch(item: QGraphicsSimpleTextItem) -> float:
    """@brief Horizontal stretch a `kicad_text_item` carries along its reading direction.

    @param item An item built by `kicad_text_item`.
    @return The scale of its base transform along the page axis it reads
        along (1.0 for an unstretched item).
    """
    transform = item.transform()
    return transform.m22() if item.rotation() % 180 else transform.m11()
