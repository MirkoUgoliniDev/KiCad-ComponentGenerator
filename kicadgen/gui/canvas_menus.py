"""@brief Shared element menu and clipboard of the symbol editor and the footprint preview.

Both tabs open a right-click menu on an element (symbol: a placed glyph,
shape or text; footprint: a user drawing) and copy/paste elements. What they
share is handled here and only here (plan `docs/canvas-menus-plan.md`,
section 3.1):

- `MenuEntry`: the immutable description of one menu entry (key, label,
  icon, state, submenu).
- The standard entries (`rotate_entries`, `mirror_entry`, `delete_entry`,
  `copy_entry`, `duplicate_entry`, `properties_entry`, `add_vertex_entry`,
  `remove_vertex_entry`): same words, icons, keys and order in both tabs.
- `build_element_menu` / `exec_element_menu`: the `QMenu` itself, its
  disabled title line and the action -> key map.
- `ElementClipboard`: copied records and the paste offset that grows by one
  step at every paste, as in KiCad.
- The vertex editing words and reach: `VERTEX_REFUSED` / `VERTEX_REFUSALS`
  (the refusal messages by `vertex_edit` reason), `VERTEX_PICK_PX`,
  `CONTEXT_HIT_MARGIN_PX`, `pick_reach_mm` (with `half_stroke_mm`) and
  `scene_point_mm`, the screen-to-drawing point of a right click.

The module imports no model and no tab: labels arrive translated through the
caller's `tx` function, records are opaque and moved by a caller-supplied
`translate` callback.
"""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QMenu, QWidget

from ..core import vertex_edit
from .canvas_gestures import HANDLE_SIZE_PX

#: Folder of the menu icons (SVG files shipped with the GUI).
ASSETS = Path(__file__).parent / "assets"

#: Keys of the standard entries; a tab dispatches the chosen key.
ROTATE_CCW = "rotate_ccw"
ROTATE_CW = "rotate_cw"
MIRROR = "mirror"
DELETE = "delete"
COPY = "copy"
DUPLICATE = "duplicate"
PROPERTIES = "properties"
ADD_VERTEX = "add_vertex"
REMOVE_VERTEX = "remove_vertex"

#: Reach, in SCREEN pixels at any zoom, of the vertex/segment pick of a right
#: click on a line or polygon (`kicadgen.core.vertex_edit.pick`): the HALF-DIAGONAL
#: of a vertex handle square (`canvas_gestures.HANDLE_SIZE_PX` / 2 * sqrt(2), about
#: 4.24 px), the smallest circle that holds the whole visible square, corners
#: included; a click that close to an outline picks the segment. Derived from the
#: handle size, not a tuned number (the former full side, 6 px, let the two vertex
#: reaches of a short segment hide it: generality finding of 2026-11-09). When a
#: click is within reach of both, `vertex_edit.pick` decides by its midpoint rule.
#: A UI reach shared by both tabs; each converts it to mm with `pick_reach_mm`.
VERTEX_PICK_PX = HANDLE_SIZE_PX / 2.0 * math.sqrt(2.0)

#: Margin, in SCREEN pixels at any zoom, of the right-click hit test of both
#: tabs: a context click this close to a drawn stroke opens that element's menu,
#: and anywhere on a vertex handle opens its element's menu. The same value as
#: `footprint_view.HIT_MARGIN_PX` (a test pins them together); a UI tolerance
#: (strokes and handles are a few px wide at any zoom), not geometry.
CONTEXT_HIT_MARGIN_PX = 4

#: Status message of a refused vertex insertion or removal whose reason has no
#: wording of its own (untranslated Italian source, «Modifica dei vertici rifiutata.»: vertex edit refused).
VERTEX_REFUSED = 'Modifica dei vertici rifiutata.'

#: Status messages of a refused vertex insertion or removal, keyed by
#: `vertex_edit.VertexEditRefused.reason` (untranslated Italian sources, shown
#: through the tab's `tx`); a reason missing here falls back to VERTEX_REFUSED.
VERTEX_REFUSALS = {
    vertex_edit.TOO_FEW_POINTS: 'Vertice non rimosso: una linea ne richiede almeno 2, un poligono almeno 3.',
    vertex_edit.NO_AREA: 'Vertice non rimosso: il poligono resterebbe senza area.',
    vertex_edit.NO_LENGTH: 'Vertice non rimosso: la linea resterebbe senza lunghezza.',
    vertex_edit.COINCIDENT: 'Vertice non aggiunto: coinciderebbe con un vertice esistente.',
    vertex_edit.DUPLICATE: 'Vertice non rimosso: due vertici consecutivi coinciderebbero.',
}


def vertex_refusal_text(reason: str) -> str:
    """@brief The untranslated status message of a refused vertex edit.

    @param reason `vertex_edit.VertexEditRefused.reason`.
    @return Its VERTEX_REFUSALS wording, VERTEX_REFUSED for an unknown reason.
    """
    return VERTEX_REFUSALS.get(reason, VERTEX_REFUSED)


def pick_reach_mm(view_scale: float, scene_units_per_mm: float, px: float = VERTEX_PICK_PX,
                  stroke_width_mm: float = 0.0) -> float:
    """@brief A screen reach converted to drawing mm at the current zoom, widened by half the stroke.

    @param view_scale The view transform's horizontal scale (`transform().m11()`;
        its sign is ignored, a mirrored view has the same reach).
    @param scene_units_per_mm Scene units per mm of the calling tab.
    @param px Reach in screen pixels; VERTEX_PICK_PX by default.
    @param stroke_width_mm Stroke width, mm, of the element picked; 0 (the
        default, and a zone, which has no stroke) adds nothing, as does a
        negative or non-finite width.
    @return `px` / (|view_scale| * scene_units_per_mm) + stroke_width_mm / 2,
        mm; 0.0 for a degenerate or non-finite scale (nothing is picked
        rather than everything).
    @note The half width follows KiCad's own hit test of a drawn shape,
        `EDA_SHAPE::hitTest` (common/eda_shape.cpp): its reach is the
        accuracy plus GetWidth() / 2 when the width is positive, so a click
        on the outer edge of a thick stroke still hits it. Recalled from the
        KiCad source, not re-read for this change.
    """
    try:
        px_per_mm = abs(float(view_scale)) * float(scene_units_per_mm)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(px_per_mm) or px_per_mm <= 0.0:
        return 0.0
    return float(px) / px_per_mm + half_stroke_mm(stroke_width_mm)


def half_stroke_mm(stroke_width_mm) -> float:
    """@brief Half a stroke width, mm, as a pick reach adds it; 0 for no usable width.

    @param stroke_width_mm Stroke width, mm (any value read from a record).
    @return width / 2 for a finite positive number; 0.0 otherwise (a zone
        or a KiCad width of 0, which `EDA_SHAPE::hitTest` does not add either).
    """
    try:
        width = float(stroke_width_mm)
    except (TypeError, ValueError):
        return 0.0
    return width / 2.0 if math.isfinite(width) and width > 0.0 else 0.0


def scene_point_mm(view, global_pos, scene_units_per_mm: float, y_sign: float) -> tuple:
    """@brief The drawing point (mm) under a screen position of a canvas view.

    @param view The tab's `QGraphicsView`.
    @param global_pos Screen `QPoint`.
    @param scene_units_per_mm Scene units per mm of the calling tab.
    @param y_sign +1 when the drawing's Y grows downwards like the scene's
        (footprint), -1 when it grows upwards (symbol).
    @return (x, y) mm in the tab's own drawing axes.
    """
    scene = view.mapToScene(view.viewport().mapFromGlobal(global_pos))
    return scene.x() / scene_units_per_mm, y_sign * scene.y() / scene_units_per_mm


@dataclass(frozen=True)
class MenuEntry:
    """@brief One entry of an element menu.

    @param key Value returned by `exec_element_menu` when the entry is chosen;
        unused (may be empty) for an entry with `children`.
    @param label Text shown, already translated by the caller.
    @param icon File name of an icon in `ASSETS`, or None.
    @param enabled False shows the entry greyed out (never returned).
    @param separator_before True puts a separator line above the entry.
    @param tooltip Tooltip text (menus built here show tooltips).
    @param checkable True shows a check state.
    @param checked Initial check state of a checkable entry.
    @param children Non-empty: the entry is a submenu holding these entries.
    @param icon_object A ready `QIcon` (e.g. a layer colour swatch); wins over `icon`.
    """

    key: str
    label: str
    icon: Optional[str] = None
    enabled: bool = True
    separator_before: bool = False
    tooltip: str = ""
    checkable: bool = False
    checked: bool = False
    children: tuple = ()
    icon_object: Optional[QIcon] = None


def rotate_entries(tx: Callable[[str], str], enabled: bool = True) -> list:
    """@brief The two quarter-turn entries, counter-clockwise first.

    @param tx Translation function of the calling tab.
    @param enabled False greys both out (an element that cannot turn).
    @return [«Ruota di 90 gradi (antiorario)», «Ruota di 90 gradi (orario)»] entries.
    """
    return [MenuEntry(ROTATE_CCW, tx("Ruota di 90 gradi (antiorario)"), "rotate-ccw.svg", enabled),
            MenuEntry(ROTATE_CW, tx("Ruota di 90 gradi (orario)"), "rotate-cw.svg", enabled)]


def mirror_entry(tx: Callable[[str], str], enabled: bool = True) -> MenuEntry:
    """@brief The «Specchia» (mirror) entry.

    @param tx Translation function of the calling tab.
    @param enabled False greys it out (an element a mirror would not change).
    @return The entry, key `MIRROR`.
    """
    return MenuEntry(MIRROR, tx("Specchia"), "format-painter.svg", enabled)


def delete_entry(tx: Callable[[str], str], label: str) -> MenuEntry:
    """@brief The delete entry; each tab keeps its own wording.

    @param tx Translation function of the calling tab.
    @param label Untranslated label, e.g. «Elimina elemento» or «Elimina disegno».
    @return The entry, key `DELETE`.
    """
    return MenuEntry(DELETE, tx(label), "delete.svg")


def copy_entry(tx: Callable[[str], str]) -> MenuEntry:
    """@brief The «Copia (Ctrl+C)» entry, key `COPY`.

    @param tx Translation function of the calling tab.
    @return The entry.
    """
    return MenuEntry(COPY, tx("Copia (Ctrl+C)"))


def duplicate_entry(tx: Callable[[str], str]) -> MenuEntry:
    """@brief The «Duplica (Ctrl+D)» entry, key `DUPLICATE`.

    @param tx Translation function of the calling tab.
    @return The entry.
    """
    return MenuEntry(DUPLICATE, tx("Duplica (Ctrl+D)"))


def properties_entry(tx: Callable[[str], str]) -> MenuEntry:
    """@brief The «Proprietà elemento…» entry, key `PROPERTIES`.

    @param tx Translation function of the calling tab.
    @return The entry.
    """
    return MenuEntry(PROPERTIES, tx("Proprietà elemento…"))


def add_vertex_entry(tx: Callable[[str], str], enabled: bool = True,
                     separator_before: bool = True) -> MenuEntry:
    """@brief The «Aggiungi vertice» (add vertex) entry of a line or polygon, key `ADD_VERTEX`.

    @param tx Translation function of the calling tab.
    @param enabled True when the right click picked a segment (`vertex_edit.pick`).
    @param separator_before True (default) opens the vertex group with a separator.
    @return The entry.
    """
    return MenuEntry(ADD_VERTEX, tx("Aggiungi vertice"), enabled=enabled, separator_before=separator_before)


def remove_vertex_entry(tx: Callable[[str], str], enabled: bool = True) -> MenuEntry:
    """@brief The «Rimuovi vertice» (remove vertex) entry of a line or polygon, key `REMOVE_VERTEX`.

    @param tx Translation function of the calling tab.
    @param enabled True when the right click picked a vertex (`vertex_edit.pick`).
    @return The entry.
    @note Enabled even when the removal will be refused (too few vertices, no
        area left): choosing it then tells the user why, instead of a grey
        entry that explains nothing.
    """
    return MenuEntry(REMOVE_VERTEX, tx("Rimuovi vertice"), enabled=enabled)


def vertex_entries(tx: Callable[[str], str], picked) -> list:
    """@brief The two vertex entries, enabled by what a right click picked.

    @param tx Translation function of the calling tab.
    @param picked A `vertex_edit.Pick` or None (the click was off the outline).
    @return [«Aggiungi vertice» enabled on a segment, «Rimuovi vertice» enabled on a vertex].
    """
    kind = getattr(picked, "kind", None)
    return [add_vertex_entry(tx, enabled=kind == vertex_edit.SEGMENT),
            remove_vertex_entry(tx, enabled=kind == vertex_edit.VERTEX)]


def _icon(entry: MenuEntry) -> Optional[QIcon]:
    """The entry's icon, or None."""
    if entry.icon_object is not None:
        return entry.icon_object
    if entry.icon:
        return QIcon(str(ASSETS / entry.icon))
    return None


def _fill(menu: QMenu, entries, keys: dict) -> None:
    """Add `entries` to `menu`, recursing into submenus, recording action -> key."""
    menu.setToolTipsVisible(True)
    for entry in entries:
        if entry.separator_before:
            menu.addSeparator()
        icon = _icon(entry)
        if entry.children:
            sub = menu.addMenu(entry.label)
            if icon is not None:
                sub.setIcon(icon)
            sub.setEnabled(entry.enabled)
            _fill(sub, entry.children, keys)
            continue
        action = menu.addAction(icon, entry.label) if icon is not None else menu.addAction(entry.label)
        if icon is not None:
            action.setIconVisibleInMenu(True)
        action.setEnabled(entry.enabled)
        if entry.tooltip:
            action.setToolTip(entry.tooltip)
        if entry.checkable:
            action.setCheckable(True)
            action.setChecked(entry.checked)
        keys[action] = entry.key


def build_element_menu(parent: Optional[QWidget], title: str, entries) -> tuple:
    """@brief Build an element menu: disabled title line, separator, then the entries.

    @param parent Parent widget of the menu (the tab or the window).
    @param title Title line, already translated; empty for no title.
    @param entries `MenuEntry` items in display order.
    @return (QMenu, {QAction: key}) — submenu actions are in the map too.
    """
    menu = QMenu(parent)
    keys: dict = {}
    if title:
        heading = menu.addAction(title)
        heading.setEnabled(False)
        menu.addSeparator()
    _fill(menu, entries, keys)
    return menu, keys


def exec_element_menu(menu: QMenu, keys: dict, global_pos) -> Optional[str]:
    """@brief Show the menu modally and return the chosen key.

    @param menu A menu from `build_element_menu`.
    @param keys Its action -> key map.
    @param global_pos Screen position (QPoint) of the menu.
    @return The chosen entry's key; None when dismissed or a non-entry was hit.
    @note The menu is scheduled for deletion whatever happens.
    """
    try:
        chosen: Optional[QAction] = menu.exec(global_pos)
        return keys.get(chosen) if chosen is not None else None
    finally:
        menu.deleteLater()


class ElementClipboard:
    """@brief Copied element records and the KiCad-like growing paste offset.

    @param translate Callback `(record, dx, dy) -> record` returning a COPY of
        a record moved by (dx, dy) mm in the tab's own model.
    @note Each paste lands one step further than the previous one: the step
        is accumulated by `advance()`, called by the tab only when the paste
        was committed (a refused paste does not move the next one).
    """

    def __init__(self, translate: Callable[[object, float, float], object]):
        """@brief Empty clipboard.

        @param translate See the class parameter.
        """
        self._translate = translate
        self._records: list = []
        self._offset = (0.0, 0.0)

    def __bool__(self) -> bool:
        """@brief True when something was copied."""
        return bool(self._records)

    def __len__(self) -> int:
        """@brief Number of records held."""
        return len(self._records)

    def copy(self, records) -> int:
        """@brief Replace the content with deep copies of `records`; the offset restarts.

        @param records Records in the tab's model.
        @return Number of records copied; 0 (empty input) leaves the clipboard as it was.
        """
        records = list(records)
        if not records:
            return 0
        self._records = deepcopy(records)
        self._offset = (0.0, 0.0)
        return len(records)

    def paste_batch(self, step) -> list:
        """@brief The records to paste now: moved by the accumulated offset plus `step`.

        @param step (dx, dy) mm of one paste.
        @return New records (the clipboard is not changed; call `advance` after a committed paste).
        """
        dx = self._offset[0] + float(step[0])
        dy = self._offset[1] + float(step[1])
        return [self._translate(deepcopy(r), round(dx, 4), round(dy, 4)) for r in self._records]

    def advance(self, step) -> None:
        """@brief Record a committed paste: the next one lands one more `step` away.

        @param step The (dx, dy) mm used by the committed `paste_batch`.
        """
        self._offset = (round(self._offset[0] + float(step[0]), 4),
                        round(self._offset[1] + float(step[1]), 4))

    def clear(self) -> None:
        """@brief Forget the content and the offset (another project was opened)."""
        self._records = []
        self._offset = (0.0, 0.0)

    def snapshot(self) -> tuple:
        """@brief The current content and offset, for `restore` (Duplicate keeps the clipboard).

        @return (records, offset), copies independent of the clipboard.
        """
        return deepcopy(self._records), self._offset

    def restore(self, state: tuple) -> None:
        """@brief Put back a `snapshot`.

        @param state A value returned by `snapshot`.
        """
        self._records, self._offset = deepcopy(state[0]), state[1]
