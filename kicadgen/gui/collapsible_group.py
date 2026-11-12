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
from PyQt6.QtWidgets import QGroupBox, QSizePolicy, QStyle, QStyleOptionGroupBox, QVBoxLayout, QWidget

#: Arrow glyphs in front of the title, collapsed and expanded.
ARROW_COLLAPSED = "▸"  # ▸
ARROW_EXPANDED = "▾"   # ▾


class CollapsibleGroupBox(QGroupBox):
    """@brief Group box with a body that the title click shows or hides.

    @note Translation: `setTitle` is what the i18n pass calls with the
        rendered title, so the arrow is added here and never reaches the
        catalogue; the source string is pinned in `i18n_title_source` so the
        pass never reads the decorated title back as a source.
    @note A collapsed group takes only the height of its title, whatever
        stretch factor or size policy its layout gives it: before, a group
        given the free height of a column (the footprint tab's «Proprietà»)
        kept that height, empty, when closed (user report, 2026-11-12). The
        vertical policy is Fixed while collapsed; the caller's policy is
        restored on expanding (`setSizePolicy` while collapsed sets the one
        restored).
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
        #: Vertical policy to restore on expanding; the caller's choice.
        self._open_vertical_policy = self.sizePolicy().verticalPolicy()
        self._apply_height_policy()

    def setSizePolicy(self, *args) -> None:  # noqa: N802 - Qt override
        """@brief Record the caller's policy, keeping the collapsed height fixed.

        @param args Either a QSizePolicy or (horizontal, vertical) policies,
            as `QWidget.setSizePolicy` takes them.
        @note While collapsed the vertical part is remembered for the next
            expand and Fixed is kept, so a caller cannot reopen the empty gap.
        """
        policy = args[0] if len(args) == 1 else QSizePolicy(*args)
        self._open_vertical_policy = policy.verticalPolicy()
        super().setSizePolicy(policy)
        self._apply_height_policy()

    def _apply_height_policy(self) -> None:
        """Fixed vertical policy while collapsed, the caller's one while expanded."""
        policy = self.sizePolicy()
        wanted = (self._open_vertical_policy if self._expanded
                  else QSizePolicy.Policy.Fixed)
        if policy.verticalPolicy() != wanted:
            policy.setVerticalPolicy(wanted)
            # The base method: the override would record Fixed as the caller's.
            QGroupBox.setSizePolicy(self, policy)

    def setTitle(self, title: str) -> None:  # noqa: N802 - Qt override
        """@brief Store the bare title and draw it with the state arrow.

        @param title Title without arrow (the i18n pass passes the rendered text).
        """
        self._base_title = title
        super().setTitle(f"{ARROW_EXPANDED if self._expanded else ARROW_COLLAPSED} {title}")

    def baseTitle(self) -> str:  # noqa: N802 - Qt naming
        """@brief The title without the arrow.

        @return The title text last given to `setTitle`, without the arrow prefix.
        """
        return self._base_title

    def isExpanded(self) -> bool:  # noqa: N802 - Qt naming
        """@brief True when the body is shown.

        @return True while the body is visible, False when collapsed.
        """
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
        self._apply_height_policy()
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
        """@brief A left click on the title toggles the body; elsewhere, default.

        @param event Qt mouse event; accepted when it toggled the body.
        """
        if (event.button() == Qt.MouseButton.LeftButton
                and self._title_rect().contains(event.position().toPoint())):
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)
