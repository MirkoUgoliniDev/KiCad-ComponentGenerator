"""@brief Shared element selection of the symbol editor and the footprint preview.

Both tabs select elements (symbol: placed glyphs, shapes, texts; footprint:
user drawings) with the same gestures (plan `docs/canvas-selection-plan.md`):

- a click on an element selects it; Ctrl+click adds or removes it;
- Shift+drag (or the tab's selection mode) draws a rubber band; the elements
  whose whole bounds lie inside it are selected (Ctrl held: added);
- a click on the empty background (no drag) or Escape clears the selection.

What they share is handled here and only here:

- `IndexSelection`: the selected element indices and the primary one.
- `RubberBand`: the band widget, its mouse capture and its scene rectangle.
- `is_click`: whether a press and its release are a click, not a drag.

The module is Qt-only and imports no model and no tab.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PyQt6.QtCore import QPoint, QRect, QRectF
from PyQt6.QtWidgets import QApplication, QGraphicsView, QRubberBand


def is_click(press: QPoint, release: QPoint) -> bool:
    """@brief Whether a release is close enough to its press to be a click.

    @param press Viewport position (px) of the press.
    @param release Viewport position (px) of the release.
    @return True when the manhattan distance is below `QApplication.startDragDistance()`
        (at least 1 px: a platform reporting 0, as the offscreen one, still
        counts a release on the pressed pixel as a click).
    """
    delta = release - press
    return abs(delta.x()) + abs(delta.y()) < max(QApplication.startDragDistance(), 1)


def encloses(band: QRectF, bounds) -> bool:
    """@brief Whether a rubber band wholly contains an element's bounds.

    @param band The band's scene rectangle.
    @param bounds (x0, y0, x1, y1) of the element in scene units, any order;
        a zero width or height (a horizontal or vertical line, a text anchor
        point) is valid.
    @return True when every edge of the bounds lies inside the band (edges included).
    @note `QRectF.contains(QRectF)` refuses an empty rectangle, so a flat line
        or a point would never be selected: the edges are compared one by one.
    """
    x0, y0, x1, y1 = (float(v) for v in bounds)
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    return (band.left() <= left and right <= band.right()
            and band.top() <= top and bottom <= band.bottom())


class IndexSelection:
    """@brief Selected element indices plus the primary (last chosen) one.

    @note Indices are positions in the tab's element list: the tab clears or
        remaps the selection when that list changes under it (deletion,
        undo, a new project).
    """

    def __init__(self):
        """@brief Empty selection."""
        self.primary: Optional[int] = None
        self.members: set = set()

    def indices(self, count: int) -> list:
        """@brief Every selected index below `count`, ascending, the primary included.

        @param count Number of elements now in the list.
        @return Sorted list of valid indices.
        """
        chosen = {i for i in self.members if 0 <= i < count}
        if self.primary is not None and 0 <= self.primary < count:
            chosen.add(self.primary)
        return sorted(chosen)

    def __contains__(self, index) -> bool:
        """@brief True when `index` is selected (member or primary)."""
        return index is not None and (index == self.primary or index in self.members)

    def __bool__(self) -> bool:
        """@brief True when anything is selected."""
        return self.primary is not None or bool(self.members)

    def select(self, index: Optional[int], additive: bool = False) -> None:
        """@brief Make `index` the primary; `additive` keeps the others.

        @param index Element index, or None to clear.
        @param additive True adds to the selection instead of replacing it.
        """
        if index is None:
            self.clear()
            return
        self.members = (self.members | {index}) if additive else {index}
        self.primary = index

    def toggle(self, index: int) -> bool:
        """@brief Add `index` when absent, remove it when present (Ctrl+click).

        @param index Element index.
        @return True when `index` is selected afterwards.
        """
        if index in self:
            self.members.discard(index)
            if self.primary == index:
                self.primary = max(self.members) if self.members else None
            return False
        self.select(index, additive=True)
        return True

    def set_many(self, indices: Iterable[int], additive: bool = False) -> None:
        """@brief Select several indices (a rubber band); the highest becomes the primary.

        @param indices Element indices.
        @param additive True adds them to the current selection.
        """
        chosen = set(indices) | (set(self.members) | ({self.primary} if self.primary is not None else set())
                                 if additive else set())
        self.members = chosen
        self.primary = max(chosen) if chosen else None

    def snapshot(self) -> tuple:
        """@brief The selection as a value, for `restore` across a commit.

        @return (primary, frozenset(members)).
        """
        return self.primary, frozenset(self.members)

    def restore(self, state: tuple) -> None:
        """@brief Put back a `snapshot` (or any (primary, members) pair).

        @param state (primary, members); the primary is added to the members.
        """
        primary, members = state
        self.members = set(members) | ({primary} if primary is not None else set())
        self.primary = primary

    def clear(self) -> None:
        """@brief Select nothing."""
        self.primary = None
        self.members = set()


class RubberBand:
    """@brief The selection rubber band of a `QGraphicsView`.

    @param view The view; the band is a child of its viewport.
    @note The band grabs the viewport's mouse between `begin` and
        `finish`/`cancel`, so the release reaches the view even outside it.
    """

    def __init__(self, view: QGraphicsView):
        """@brief Hidden band on `view`'s viewport.

        @param view See the class parameter.
        """
        self._view = view
        self._band = QRubberBand(QRubberBand.Shape.Rectangle, view.viewport())
        self._start: Optional[QPoint] = None
        self.additive = False

    @property
    def active(self) -> bool:
        """@brief Whether a band is being drawn.

        @return True between `begin` and `finish`/`cancel`.
        """
        return self._start is not None

    @property
    def visible(self) -> bool:
        """@brief Whether the band widget is shown.

        @return True while the band is drawn on the viewport.
        """
        return self._band.isVisible()

    def begin(self, pos: QPoint, additive: bool) -> None:
        """@brief Start the band at a viewport position and grab the mouse.

        @param pos Viewport position (px).
        @param additive True when the band adds to the selection (Ctrl held).
        """
        self._start = QPoint(pos)
        self.additive = bool(additive)
        self._band.setGeometry(QRect(pos, pos))
        self._band.show()
        self._view.setFocus()
        self._view.viewport().grabMouse()

    def move(self, pos: QPoint) -> bool:
        """@brief Stretch the band to a viewport position.

        @param pos Viewport position (px).
        @return True when the band is active (the move is consumed).
        """
        if self._start is None:
            return False
        self._band.setGeometry(QRect(self._start, pos).normalized())
        return True

    def finish(self, pos: QPoint) -> Optional[QRectF]:
        """@brief End the band at a viewport position.

        @param pos Viewport position (px) of the release.
        @return The band's scene rectangle; None when no band was active or
            the release is a click (`is_click`): nothing is selected or cleared.
        """
        if self._start is None:
            return None
        start = self._start
        self._release()
        if is_click(start, pos):
            # A Shift+click without a drag is not a band: it must not empty the selection.
            return None
        rect = QRect(start, pos).normalized()
        return self._view.mapToScene(rect).boundingRect()

    def cancel(self) -> bool:
        """@brief Drop the band without selecting.

        @return True when a band was active.
        """
        if self._start is None:
            return False
        self._release()
        self._view.viewport().unsetCursor()
        return True

    def _release(self) -> None:
        """Hide the band and give the mouse back."""
        self._start = None
        self._band.hide()
        self._view.viewport().releaseMouse()
