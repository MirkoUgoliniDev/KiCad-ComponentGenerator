"""@brief Shared pieces of the review windows: icon tool buttons and the info popup.

User decision of 2026-09-24, after trying a mock-up: both review windows
(«Corpi rilevati» and «Revisione componenti») get ONE bar of icons at the top,
like the 3D tab, with the command name, its shortcut and a short explanation
in the tooltip, and the long instructions behind an «i» icon that opens a
popup instead of sitting above the photo as fixed text.
"""
from pathlib import Path
from time import monotonic

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QComboBox, QFrame, QLabel, QScrollArea, QToolButton,
                             QVBoxLayout, QWidget)

from .theme import SURFACE1

ASSETS = Path(__file__).resolve().parent / "assets"

#: Widest the info popup box may be, in pixels, narrowed further by a narrow
#: window. A reading choice: minus the padding (POPUP_PADDING_PX, 28 px) the
#: text column is 412 px, about the tooltip column
#: (tooltips.TOOLTIP_MAX_WIDTH_PX, 420 px) so the two read alike.
INFO_POPUP_MAX_WIDTH_PX = 440

#: Zoom factor of one wheel notch and of one click on zoom +/- (or the keys
#: + and -), in BOTH review windows. A choice, not a measurement: 20% per step
#: is large enough that five steps more than double the scale and small
#: enough that a single notch never loses the spot under the pointer. Before
#: 2026-09-24 the windows used 1.15/1.25 and 1.2, with no reason for the
#: difference (reviewer 4, defect 17): one value, so the hand learns one
#: gesture.
ZOOM_STEP = 1.2

#: Distance kept between the popup and the edges of its window, in pixels.
INFO_POPUP_MARGIN_PX = 8

#: Inner padding of the popup box (left, top, right, bottom), in pixels.
POPUP_PADDING_PX = (14, 12, 14, 12)

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
    """@brief Thin vertical line between groups of toolbar icons.

    @note The one divider of every icon bar (3D tab and review windows), so
        the bars stay uniform.
    """
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setFixedWidth(9)
    line.setStyleSheet(f"color: {SURFACE1};")
    return line


class _TypingGoesToCombos(QObject):
    """Event filter: a focused, non-editable combo box keeps its printable keys."""

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        """@brief Accept ShortcutOverride for plain printable keys on a non-editable combo.

        @note Accepting the override is how a widget tells Qt that it wants the
            key itself: the window shortcut does not fire and the combo gets
            the key press, which it uses to jump to the entry with that
            initial. Modified keys (Ctrl, Alt) and non-printable ones (Delete,
            Escape, arrows) keep going to the window.
        """
        if (event.type() == QEvent.Type.ShortcutOverride and isinstance(watched, QComboBox)
                and not watched.isEditable()):
            plain = not (event.modifiers() & ~(Qt.KeyboardModifier.ShiftModifier
                                              | Qt.KeyboardModifier.KeypadModifier))
            text = event.text()
            if plain and text and text.isprintable():
                event.accept()
                return True
        return super().eventFilter(watched, event)


def give_combos_their_keys(root: QWidget) -> QObject:
    """@brief Let the combo boxes of `root` win over its single-key shortcuts.

    @param root Window whose QComboBox children get the filter.
    @return The installed filter, owned by `root`.
    @note Reviewer 1, defect 4: the single-letter window shortcuts (R, P, F,
        +, -) took the keys away from a focused combo box, so typing an
        initial in the category filter switched tool or photo state instead.
        Editable combos and line edits already claim printable keys in Qt.
    """
    guard = _TypingGoesToCombos(root)
    for combo in root.findChildren(QComboBox):
        combo.installEventFilter(guard)
    return guard


class AnchoredPopup(QFrame):
    """@brief A popup opened under a toolbar icon, never outside the window.

    @note A Qt popup: any click outside it closes it, as a menu. It holds any
        widgets, laid out vertically at most INFO_POPUP_MAX_WIDTH_PX wide and
        never wider than the window it opens in; the box is placed under the
        icon, from its left edge, and moved inside the window if needed.
    @note Never taller than the window either: longer content scrolls inside
        the popup. Before, a popup taller than a small window was pushed out
        through the top edge (reviewer 1, suspected defect).
    """

    def __init__(self, widgets, parent: QWidget):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("info_popup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#info_popup { background: #11111b; border: 1px solid #45475a; "
            "border-radius: 6px; } QLabel { color: #cdd6f4; background: transparent; }")
        self.content = QWidget()
        self.content.setObjectName("info_popup_content")
        self._content_layout = QVBoxLayout(self.content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        for widget in widgets:
            self._content_layout.addWidget(widget)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.content)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QWidget#info_popup_content "
                                  "{ background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*POPUP_PADDING_PX)
        layout.addWidget(self.scroll)
        self._hidden_at = 0.0
        self._anchor = None

    def hideEvent(self, event):  # noqa: N802 - Qt override
        """@brief Remember when the popup closed, for the reopen guard."""
        self._hidden_at = monotonic()
        super().hideEvent(event)

    def toggle_under(self, anchor: QWidget) -> None:
        """@brief Open the popup under `anchor`, or leave it closed.

        @param anchor The toolbar icon.
        @note Called by the icon's click. When the same click has just closed
            the popup (Qt closes it on the press), nothing reopens it, so the
            icon works as an on/off switch.
        """
        if self.isVisible():
            self.hide()
            return
        if monotonic() - self._hidden_at < _REOPEN_GUARD_S:
            return
        self.open_under(anchor)

    def refit(self) -> None:
        """@brief Re-measure and re-place an open popup whose content changed."""
        if self.isVisible() and self._anchor is not None:
            self.open_under(self._anchor)

    def open_under(self, anchor: QWidget) -> None:
        """@brief Show the popup below `anchor`, inside the anchor's window.

        @param anchor Widget the popup belongs to.
        """
        self._anchor = anchor
        window = anchor.window()
        area = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        width = max(160, min(INFO_POPUP_MAX_WIDTH_PX, area.width() - 2 * INFO_POPUP_MARGIN_PX))
        self.setFixedWidth(width)
        left, top, right, bottom = POPUP_PADDING_PX
        inner = width - left - right
        content = self._content_layout
        content.activate()
        needed = (content.totalHeightForWidth(inner) if content.hasHeightForWidth()
                  else content.totalSizeHint().height()) + top + bottom
        available = area.height() - 2 * INFO_POPUP_MARGIN_PX
        # Piu' alto della finestra: si ferma al suo bordo e il resto scorre.
        height = min(needed, available)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if needed > available
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(height)
        # Sotto l'icona, dal suo bordo sinistro; se uscirebbe a destra rientra
        # (per l'icona «i» in fondo alla barra finisce allineato a destra).
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        x = below.x()
        y = below.y()
        x = min(max(x, area.left() + INFO_POPUP_MARGIN_PX), area.right() - INFO_POPUP_MARGIN_PX - width)
        y = min(max(y, area.top() + INFO_POPUP_MARGIN_PX), area.bottom() - INFO_POPUP_MARGIN_PX - height)
        self.move(x, y)
        self.show()


class InfoPopup(AnchoredPopup):
    """@brief Instructions shown on demand under an «i» icon.

    @note Rich text in one wrapped label; see AnchoredPopup for placement.
    """

    def __init__(self, html_text: str, parent: QWidget):
        self.label = QLabel(html_text)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        super().__init__([self.label], parent)

    def set_text(self, html_text: str) -> None:
        """@brief Replace the popup text (e.g. after a language change).

        @param html_text Rich text.
        """
        self.label.setText(html_text)
