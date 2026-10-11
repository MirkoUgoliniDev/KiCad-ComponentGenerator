"""@brief A QGroupBox whose title doubles as a collapse/expand control.

The title stays where every other group box of the application draws it, on
the frame line at the top left, with the same font and colour, prefixed by
an arrow that shows the state. Clicking the title toggles the body. The
user asked for it (2026-10-11): a tool button inside the frame did not
look like the other blocks of the same tab.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QGroupBox, QStyle, QStyleOptionGroupBox, QVBoxLayout, QWidget

#: Arrow glyphs in front of the title, collapsed and expanded.
ARROW_COLLAPSED = "▸"  # ▸
ARROW_EXPANDED = "▾"   # ▾


class CollapsibleGroupBox(QGroupBox):
    """@brief Group box with a body that the title click shows or hides.

    @note Translation: `setTitle` is what the i18n pass calls with the
        rendered title, so the arrow is added here and never reaches the
        catalogue; the source string is pinned in `i18n_title_source` so the
        pass never reads the decorated title back as a source.
    """

    #: Emitted with the new state after a toggle.
    toggled = pyqtSignal(bool)

    def __init__(self, title: str, parent: QWidget | None = None, expanded: bool = False):
        """@brief Build the group with its body hidden or shown.

        @param title Source (Italian) title, translated by the i18n pass.
        @param parent Owner widget.
        @param expanded Initial state; collapsed by default.
        """
        super().__init__(parent)
        self._base_title = ""
        self._expanded = bool(expanded)
        self.setProperty("i18n_title_source", title)
        self.setTitle(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 4)
        outer.setSpacing(0)
        self.body = QWidget(self)
        outer.addWidget(self.body)
        self.body.setVisible(self._expanded)

    def setTitle(self, title: str) -> None:  # noqa: N802 - Qt override
        """@brief Store the bare title and draw it with the state arrow.

        @param title Title without arrow (the i18n pass passes the rendered text).
        """
        self._base_title = title
        super().setTitle(f"{ARROW_EXPANDED if self._expanded else ARROW_COLLAPSED} {title}")

    def baseTitle(self) -> str:  # noqa: N802 - Qt naming
        """@brief The title without the arrow."""
        return self._base_title

    def isExpanded(self) -> bool:  # noqa: N802 - Qt naming
        """@brief True when the body is shown."""
        return self._expanded

    def setExpanded(self, expanded: bool) -> None:  # noqa: N802 - Qt naming
        """@brief Show or hide the body and redraw the arrow.

        @param expanded Target state; a no-op when already there.
        """
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.body.setVisible(expanded)
        self.setTitle(self._base_title)
        self.toggled.emit(expanded)

    def toggle(self) -> None:
        """@brief Flip the state."""
        self.setExpanded(not self._expanded)

    def _title_rect(self):
        option = QStyleOptionGroupBox()
        self.initStyleOption(option)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox, option,
            QStyle.SubControl.SC_GroupBoxLabel, self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """@brief A left click on the title toggles the body; elsewhere, default."""
        if (event.button() == Qt.MouseButton.LeftButton
                and self._title_rect().contains(event.position().toPoint())):
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)
