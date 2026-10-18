"""@brief Shared element menu and clipboard of the symbol editor and the footprint preview.

Both tabs open a right-click menu on an element (symbol: a placed glyph,
shape or text; footprint: a user drawing) and copy/paste elements. What they
share is handled here and only here (plan `docs/canvas-menus-plan.md`,
section 3.1):

- `MenuEntry`: the immutable description of one menu entry (key, label,
  icon, state, submenu).
- The standard entries (`rotate_entries`, `mirror_entry`, `delete_entry`,
  `copy_entry`, `duplicate_entry`, `properties_entry`): same words, icons,
  keys and order in both tabs.
- `build_element_menu` / `exec_element_menu`: the `QMenu` itself, its
  disabled title line and the action -> key map.
- `ElementClipboard`: copied records and the paste offset that grows by one
  step at every paste, as in KiCad.

The module imports no model and no tab: labels arrive translated through the
caller's `tx` function, records are opaque and moved by a caller-supplied
`translate` callback.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QMenu, QWidget

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
