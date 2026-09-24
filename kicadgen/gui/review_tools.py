"""@brief Shared pieces of the review windows: icon tool buttons and the info popup.

User decision of 2026-09-24, after trying a mock-up: both review windows
(«Corpi rilevati» and «Revisione componenti») get ONE bar of icons at the top,
like the 3D tab, with the command name, its shortcut and a short explanation
in the tooltip, and the long instructions behind an «i» icon that opens a
popup instead of sitting above the photo as fixed text.
"""
from pathlib import Path
from time import monotonic

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QFrame, QLabel, QToolButton, QVBoxLayout, QWidget

ASSETS = Path(__file__).resolve().parent / "assets"

#: Widest the info popup may be, in pixels: a comfortable reading column
#: (the same choice as tooltips.TOOLTIP_MAX_WIDTH_PX), narrowed further by a
#: narrow window.
INFO_POPUP_MAX_WIDTH_PX = 440

#: Distance kept between the popup and the edges of its window, in pixels.
INFO_POPUP_MARGIN_PX = 8

#: A click on the «i» that has just closed the popup (Qt closes a popup on any
#: press outside it, the button included) must not reopen it at once: within
#: this time, in seconds, the click counts as the closing gesture.
_REOPEN_GUARD_S = 0.25


def asset_icon(name: str) -> QIcon:
    """@brief Icon loaded from `gui/assets`.

    @param name SVG file name.
    @return The QIcon (null when the file is missing).
    """
    return QIcon(str(ASSETS / name))


def set_tool_texts(button: QToolButton, title: str, shortcut: str = "",
                   detail: str = "", tr=None) -> None:
    """@brief Put name, shortcut and explanation of a command on an icon button.

    @param button Target button.
    @param title Italian source of the command name.
    @param shortcut Key shown after the name, e.g. «Canc»; empty for none.
    @param detail Italian source of the one-line explanation; may be empty.
    @param tr Translation function (source -> shown text); identity if None.
    @note The tooltip is «name (key)\\nexplanation»: the first line is the
        name, which the tooltip filter shows in bold. The accessible name is
        the name alone, as a screen reader expects, and the explanation is
        the accessible description.
    @note The texts are translated HERE, so the button is marked
        `i18n_skip`: the generic retranslation would otherwise look up the
        composed text as a key.
    """
    translate = tr or (lambda text: text)
    name = translate(title)
    explanation = translate(detail) if detail else ""
    first = f"{name} ({shortcut})" if shortcut else name
    button.setToolTip(f"{first}\n{explanation}" if explanation else first)
    button.setAccessibleName(name)
    button.setAccessibleDescription(explanation)
    button.setProperty("i18n_skip", True)


def tool_button(asset: str, title: str, shortcut: str = "", detail: str = "",
                checkable: bool = False, tr=None) -> QToolButton:
    """@brief Icon-only button of a review toolbar.

    @param asset SVG file in `gui/assets`.
    @param title Italian source of the command name.
    @param shortcut Key shown in the tooltip; the caller binds it.
    @param detail Italian source of the explanation.
    @param checkable True for a mode or a toggle.
    @param tr Translation function; identity if None.
    @return The configured QToolButton (same size and look as the 3D tab's).
    """
    button = QToolButton()
    button.setIcon(asset_icon(asset))
    button.setIconSize(QSize(22, 22))
    button.setAutoRaise(True)
    button.setCheckable(checkable)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    set_tool_texts(button, title, shortcut, detail, tr)
    return button


def toolbar_divider() -> QFrame:
    """@brief Thin vertical line between groups of toolbar icons."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedWidth(9)
    line.setStyleSheet("color: #45475a;")
    return line


class InfoPopup(QFrame):
    """@brief Instructions shown on demand under an «i» button, never outside the window.

    @note A Qt popup: any click outside it closes it, as a menu. The text is
        rich text, wrapped to at most INFO_POPUP_MAX_WIDTH_PX and never wider
        than the window it opens in; the box is placed under the button,
        aligned to its right edge, and moved inside the window if needed.
    """

    def __init__(self, html_text: str, parent: QWidget):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("info_popup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#info_popup { background: #11111b; border: 1px solid #45475a; "
            "border-radius: 6px; } QLabel { color: #cdd6f4; background: transparent; }")
        self.label = QLabel(html_text)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(self.label)
        self._hidden_at = 0.0

    def set_text(self, html_text: str) -> None:
        """@brief Replace the popup text (e.g. after a language change).

        @param html_text Rich text.
        """
        self.label.setText(html_text)

    def hideEvent(self, event):  # noqa: N802 - Qt override
        """@brief Remember when the popup closed, for the reopen guard."""
        self._hidden_at = monotonic()
        super().hideEvent(event)

    def toggle_under(self, anchor: QWidget) -> None:
        """@brief Open the popup under `anchor`, or leave it closed.

        @param anchor The «i» button.
        @note Called by the button's click. When the same click has just
            closed the popup (Qt closes it on the press), nothing reopens it,
            so the «i» works as an on/off switch.
        """
        if self.isVisible():
            self.hide()
            return
        if monotonic() - self._hidden_at < _REOPEN_GUARD_S:
            return
        self.open_under(anchor)

    def open_under(self, anchor: QWidget) -> None:
        """@brief Show the popup below `anchor`, inside the anchor's window.

        @param anchor Widget the popup belongs to.
        """
        window = anchor.window()
        area = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        margins = self.layout().contentsMargins()
        width = max(160, min(INFO_POPUP_MAX_WIDTH_PX, area.width() - 2 * INFO_POPUP_MARGIN_PX))
        text_width = width - margins.left() - margins.right()
        self.label.setFixedWidth(text_width)
        height = self.label.heightForWidth(text_width) + margins.top() + margins.bottom()
        self.resize(width, height)
        below = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height() + 4))
        x = below.x() - width
        y = below.y()
        x = min(max(x, area.left() + INFO_POPUP_MARGIN_PX), area.right() - INFO_POPUP_MARGIN_PX - width)
        y = min(max(y, area.top() + INFO_POPUP_MARGIN_PX), area.bottom() - INFO_POPUP_MARGIN_PX - height)
        self.move(x, y)
        self.show()
