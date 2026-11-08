"""@brief Shared grid of drawing tools for the symbol editor and the footprint preview.

@details One widget, `ToolGrid`, replaces the hand-built button grids of the
two tabs (plan docs/footprint-drawing-kicad-plan.md, A1): a Select (arrow)
button first, then one exclusive button per tool, the active one drawn with a
filled background and a 2 px border in an accent colour the host chooses (the
footprint passes the active layer colour, the symbol the theme accent).

Contract:

* `set_active` only shows a state, it never emits (a host that arms a tool
  from a shortcut or a menu mirrors it here without a feedback loop);
* `set_active(OUTSIDE_KEY)` shows that a tool living OUTSIDE the grid is
  armed (the symbol's wire, the «Misura» ruler): no button is checked, not
  even Select, so the palette never claims the user is selecting;
* a user click emits `tool_triggered(key)`; a click on the active tool, or on
  Select, emits "" meaning "back to Select";
* a tool may carry a drop-down of variants (the five dimension types): the
  button remembers the last chosen variant, shows its icon, and a click on its
  main part emits that variant's key;
* buttons never take the keyboard focus, so the canvas keeps its shortcuts.
"""
from __future__ import annotations

from typing import Optional, Sequence

from PyQt6.QtCore import QPointF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QGridLayout, QMenu, QToolButton, QWidget

from .theme import BLUE, SURFACE0, SURFACE2, TEXT

#: Key under which the Select button is stored and the value `tool_triggered`
#: carries for "back to Select".
SELECT_KEY = ""

#: `set_active` value meaning "a tool outside this grid is armed": no button
#: checked, Select included. A reserved key no tool may use (`add_tool` refuses it).
OUTSIDE_KEY = "__outside__"

#: Dynamic property set to True on the active button; the style sheet keys on it.
ACTIVE_PROPERTY = "active"


def select_arrow_icon(size: int = 28, stroke: Optional[str] = None) -> QIcon:
    """@brief The Select tool's arrow pointer, drawn with QPainter (no asset file).

    @param size Icon side in pixels.
    @param stroke Outline colour; None uses the theme text colour, the colour
        `symbol_tab.glyph_icon` gives its strokes.
    @return A transparent icon with a classic pointer outline, in the light
        stroke style of the glyph icons.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    # Pointer outline in unit coordinates (tip at the top left), scaled to the icon.
    outline = ((0.30, 0.12), (0.30, 0.80), (0.45, 0.66), (0.56, 0.90),
               (0.67, 0.85), (0.56, 0.61), (0.76, 0.61))
    path = QPainterPath()
    path.moveTo(QPointF(outline[0][0] * size, outline[0][1] * size))
    for x, y in outline[1:]:
        path.lineTo(QPointF(x * size, y * size))
    path.closeSubpath()
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(stroke) if stroke is not None else QColor(TEXT), 1.4))
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def tool_grid_style(accent: str) -> str:
    """@brief Style sheet of a tool grid with the given accent colour.

    @param accent Colour of the active button's border and (translucent) fill.
    @return QSS for the grid widget: idle buttons flat with a transparent
        2 px border (so activation never shifts the icon), hover on the theme
        surface, the active one (`active` property true) filled with the
        accent at 35 % opacity and bordered with the full accent, readable on
        the dark #1e1e2e background.
    """
    colour = QColor(accent)
    if not colour.isValid():
        colour = QColor(BLUE)
    fill = f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, 0.35)"
    border = colour.name()
    return (
        "QToolButton { background: transparent; border: 2px solid transparent;"
        " border-radius: 4px; padding: 0px; }\n"
        f"QToolButton:hover {{ background: {SURFACE0}; border: 2px solid {SURFACE2}; }}\n"
        f"QToolButton[{ACTIVE_PROPERTY}=\"true\"] {{ background: {fill}; border: 2px solid {border}; }}\n"
        "QToolButton[popupMode=\"1\"] { padding-right: 8px; }\n"
        "QToolButton::menu-button { border: none; width: 9px; }\n"
    )


class ToolGrid(QWidget):
    """@brief Exclusive grid of tool buttons with a Select button first.

    @note Emits only on user clicks; every programmatic change
        (`set_active`, `set_icons`, `set_accent`) is silent.
    """

    #: A tool was clicked: its key (or the chosen variant's), or "" for Select.
    tool_triggered = pyqtSignal(str)

    def __init__(self, columns: int = 6, icon_size: int = 28, button_size: int = 34,
                 parent: Optional[QWidget] = None) -> None:
        """@brief Create an empty grid.

        @param columns Buttons per row (at least 1).
        @param icon_size Icon side in pixels.
        @param button_size Fixed button side in pixels.
        @param parent Parent widget.
        """
        super().__init__(parent)
        self._columns = max(1, int(columns))
        self._icon_size = int(icon_size)
        self._button_size = int(button_size)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._buttons: dict[str, QToolButton] = {}
        self._order: list[str] = []
        # Drop-down tools: parent key -> [(subkey, QAction)], and the variant shown.
        self._variants: dict[str, list[tuple[str, QAction]]] = {}
        self._current_variant: dict[str, str] = {}
        # Variant key -> parent key, so set_active and set_icons accept either.
        self._parent_of: dict[str, str] = {}
        self._active: Optional[str] = None
        # True while a tool outside the grid is armed (`set_active(OUTSIDE_KEY)`).
        self._outside = False
        self._accent = BLUE
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(tool_grid_style(self._accent))

    # ── Building ──────────────────────────────────────────────────────────

    def _new_button(self, key: str, icon: QIcon, tooltip: str, accessible_name: str) -> QToolButton:
        """Create, size, place and register one button at the next grid cell."""
        button = QToolButton(self)
        button.setIcon(icon)
        button.setIconSize(QSize(self._icon_size, self._icon_size))
        button.setFixedSize(self._button_size, self._button_size)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tooltip)
        button.setAccessibleName(accessible_name or tooltip)
        button.setProperty(ACTIVE_PROPERTY, False)
        index = len(self._order)
        self._layout.addWidget(button, index // self._columns, index % self._columns)
        self._buttons[key] = button
        self._order.append(key)
        return button

    def add_select(self, icon: Optional[QIcon] = None, tooltip: str = "Seleziona") -> QToolButton:
        """@brief Add the Select (arrow) button; it is placed first whenever it is added.

        @param icon Its icon; None draws the arrow with `select_arrow_icon`.
        @param tooltip Tooltip and accessible name (Italian source text; the
            host's translation pass keeps it in the current language).
        @return The button.
        @throws ValueError When a Select button already exists.
        """
        if SELECT_KEY in self._buttons:
            raise ValueError("the grid already has a Select button")
        button = self._new_button(SELECT_KEY, icon if icon is not None else select_arrow_icon(self._icon_size),
                                  tooltip, tooltip)
        button.clicked.connect(lambda _checked=False: self._user_clicked(SELECT_KEY))
        if self._order[0] != SELECT_KEY:
            # Added after some tools: move it to the front and re-lay the cells.
            self._order.remove(SELECT_KEY)
            self._order.insert(0, SELECT_KEY)
            self._relayout()
        self._apply_active()
        return button

    def add_tool(self, key: str, icon: QIcon, tooltip: str, accessible_name: str = "",
                 menu_items: Optional[Sequence[tuple[str, str, QIcon]]] = None) -> QToolButton:
        """@brief Add one tool button at the next cell.

        @param key Tool key emitted by `tool_triggered` (non-empty, unique).
        @param icon Button icon (for a drop-down tool, shown until a variant is chosen).
        @param tooltip Tooltip (the host composes hint + shortcut).
        @param accessible_name Accessible name; the tooltip when empty.
        @param menu_items Optional variants [(subkey, label, icon)]: the button
            gets a drop-down arrow (MenuButtonPopup), each entry emits its
            subkey, and the main part emits the last chosen subkey (the first
            one until a choice is made).
        @return The button.
        @throws ValueError When the key is empty or already used (also as a variant key).
        """
        if not key or key == OUTSIDE_KEY or key in self._buttons or key in self._parent_of:
            raise ValueError(f"invalid or duplicate tool key: {key!r}")
        button = self._new_button(key, icon, tooltip, accessible_name)
        if menu_items:
            menu = QMenu(button)
            entries: list[tuple[str, QAction]] = []
            for subkey, label, sub_icon in menu_items:
                if not subkey or subkey == OUTSIDE_KEY or subkey in self._buttons or subkey in self._parent_of:
                    raise ValueError(f"invalid or duplicate variant key: {subkey!r}")
                action = menu.addAction(sub_icon, label)
                action.setData(subkey)
                action.triggered.connect(lambda _checked=False, k=key, s=subkey: self._variant_chosen(k, s))
                entries.append((subkey, action))
                self._parent_of[subkey] = key
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            self._variants[key] = entries
            self._current_variant[key] = entries[0][0]
            button.setIcon(entries[0][1].icon())
        button.clicked.connect(lambda _checked=False, k=key: self._user_clicked(k))
        self._apply_active()
        return button

    def _relayout(self) -> None:
        """Put every button back in its cell after the order changed."""
        for button in self._buttons.values():
            self._layout.removeWidget(button)
        for index, key in enumerate(self._order):
            self._layout.addWidget(self._buttons[key], index // self._columns, index % self._columns)

    # ── State ─────────────────────────────────────────────────────────────

    def _button_key(self, key: Optional[str]) -> Optional[str]:
        """The button key that shows `key` (a variant maps to its parent), or None."""
        if key is None or key == SELECT_KEY:
            return SELECT_KEY if SELECT_KEY in self._buttons else None
        if key in self._parent_of:
            return self._parent_of[key]
        return key if key in self._buttons else None

    def set_active(self, key: Optional[str]) -> None:
        """@brief Show `key` as the active tool without emitting anything.

        @param key A tool key, a variant key (it also becomes the remembered
            variant), None / "" for Select, or `OUTSIDE_KEY` for a tool that
            lives outside the grid (no button checked). An unknown key shows
            Select.
        """
        self._outside = key == OUTSIDE_KEY
        if self._outside:
            self._active = None
            self._apply_active()
            return
        if key and key in self._parent_of:
            self._remember_variant(self._parent_of[key], key)
        button_key = self._button_key(key)
        self._active = None if button_key in (None, SELECT_KEY) else button_key
        self._apply_active()

    def active_key(self) -> Optional[str]:
        """@brief The active tool's key (for a drop-down tool, its current variant), or None for Select.

        @return The key, or None when Select is active or a tool outside the
            grid is armed (see `outside_active`).
        """
        if self._active is None:
            return None
        return self._current_variant.get(self._active, self._active)

    def outside_active(self) -> bool:
        """@brief Whether the grid shows the "tool outside the grid" state.

        @return True after `set_active(OUTSIDE_KEY)` until another state is set or a button is clicked.
        """
        return self._outside

    def current_variant(self, key: str) -> Optional[str]:
        """@brief The remembered variant of a drop-down tool.

        @param key Parent tool key.
        @return The variant key, or None for a tool without variants.
        """
        return self._current_variant.get(key)

    def set_accent(self, colour: str) -> None:
        """@brief Change the active button's accent colour (border and translucent fill).

        @param colour Any QColor name; an invalid one falls back to the theme accent.
        """
        self._accent = colour
        self.setStyleSheet(tool_grid_style(colour))

    def accent(self) -> str:
        """@brief The accent colour last passed to `set_accent` (theme accent by default).

        @return The colour string.
        """
        return self._accent

    def set_icons(self, icons: dict) -> None:
        """@brief Replace icons (e.g. tinted with a new layer colour); check states are kept.

        @param icons Mapping key -> QIcon; keys may be tool keys, variant keys
            or SELECT_KEY; unknown keys are ignored.
        """
        for key, icon in icons.items():
            parent = self._parent_of.get(key)
            if parent is not None:
                for subkey, action in self._variants[parent]:
                    if subkey == key:
                        action.setIcon(icon)
                if self._current_variant.get(parent) == key:
                    self._buttons[parent].setIcon(icon)
            elif key in self._buttons and key not in self._variants:
                # A drop-down tool shows its variant's icon, so its own key is ignored here.
                self._buttons[key].setIcon(icon)

    def buttons(self) -> dict:
        """@brief Every button by key (Select under SELECT_KEY), in grid order.

        @return A new dict key -> QToolButton.
        """
        return {key: self._buttons[key] for key in self._order}

    def button(self, key: str) -> Optional[QToolButton]:
        """@brief The button showing `key` (a variant maps to its drop-down button).

        @param key Tool key, variant key or SELECT_KEY.
        @return The button, or None when unknown.
        """
        button_key = self._button_key(key) if key else (SELECT_KEY if SELECT_KEY in self._buttons else None)
        return self._buttons.get(button_key) if button_key is not None else None

    def keys(self) -> list:
        """@brief Button keys in grid order (Select first when present).

        @return A new list.
        """
        return list(self._order)

    # ── User actions ──────────────────────────────────────────────────────

    def _user_clicked(self, key: str) -> None:
        """A click on a button's main part: arm the tool, or go back to Select."""
        self._outside = False
        if key == SELECT_KEY or key == self._active:
            self._active = None
            self._apply_active()
            self.tool_triggered.emit(SELECT_KEY)
            return
        self._active = key
        self._apply_active()
        self.tool_triggered.emit(self._current_variant.get(key, key))

    def _variant_chosen(self, key: str, subkey: str) -> None:
        """A drop-down entry: remember it and arm it (an explicit choice never disarms)."""
        self._outside = False
        self._remember_variant(key, subkey)
        self._active = key
        self._apply_active()
        self.tool_triggered.emit(subkey)

    def _remember_variant(self, key: str, subkey: str) -> None:
        """Store the variant shown by a drop-down tool and show its icon."""
        self._current_variant[key] = subkey
        for candidate, action in self._variants.get(key, ()):
            if candidate == subkey:
                self._buttons[key].setIcon(action.icon())

    def _apply_active(self) -> None:
        """Check exactly the active button (Select when none, nothing for an outside tool); re-polish."""
        shown = None if self._outside else (self._active if self._active is not None else SELECT_KEY)
        for key, button in self._buttons.items():
            on = key == shown
            if button.isChecked() != on:
                blocked = button.blockSignals(True)
                button.setChecked(on)
                button.blockSignals(blocked)
            if bool(button.property(ACTIVE_PROPERTY)) != on:
                button.setProperty(ACTIVE_PROPERTY, on)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()

