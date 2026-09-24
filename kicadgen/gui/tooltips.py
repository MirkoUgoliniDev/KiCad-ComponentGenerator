"""@brief Tooltips that wrap to a readable column and never leave the window.

Qt shows a plain-text tooltip on ONE line, however long, and keeps it inside
the SCREEN, not inside the application: the explanation of a toolbar command
ran past the window over the desktop. Rich text wraps, but at a width Qt
picks on its own. This module installs one application-wide event filter that
takes over every tooltip:

* the text is laid out as rich text, at most `TOOLTIP_MAX_WIDTH_PX` wide and
  never wider than the window it belongs to, so a long explanation grows in
  height instead of in width;
* a tooltip written as «name\\nexplanation» (the convention of the icon-only
  commands) shows the name in bold on its own line;
* the box is moved inside the window's geometry, below the pointer when it
  fits and above it otherwise.

Short tooltips keep their natural size: the width cap applies only to text
that would be wider.
"""
import html

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QToolTip, QWidget

#: Widest a tooltip may be, in pixels. A reading choice, not a measurement:
#: about 60 characters of the default 9 pt interface font, the usual limit of
#: a comfortable line. Narrower windows lower it further (see below).
TOOLTIP_MAX_WIDTH_PX = 420

#: Distance kept between a tooltip and the edges of its window, in pixels.
WINDOW_MARGIN_PX = 8

#: Offset Qt adds to the requested position before placing a tooltip (see
#: QTipLabel::placeTip); undone here so the box lands where it is computed.
_QT_TIP_OFFSET = QPoint(2, 16)

#: Horizontal room the stylesheet adds around the text (padding 8 px per
#: side plus a 1 px border, theme.APP_QSS), used only to keep the whole box,
#: not just its text, inside the window.
_FRAME_X_PX = 2 * (8 + 1)


def tooltip_html(text: str) -> str:
    """@brief Rich-text body of a tooltip.

    @param text Tooltip as the widget stores it: plain or already rich text.
    @return Rich text with the line breaks kept; for a multi-line text the
        first line (the command name) is bold.
    @note Every tooltip is treated as PLAIN text and escaped, even when Qt's
        heuristic would call it rich: no widget of this application writes
        rich-text tooltips, while a file name such as «<b>x</b>.png» must read
        as written. A future rich tooltip would show its tags literally.
    """
    lines = [html.escape(line, quote=False) for line in text.split("\n")]
    if len(lines) > 1 and lines[0]:
        lines[0] = f"<b>{lines[0]}</b>"
    return "<br>".join(lines)


def _natural_width(body: str, font) -> float:
    """Width the body would take on one line per paragraph, in pixels."""
    document = QTextDocument()
    document.setDefaultFont(font)
    document.setDocumentMargin(0)
    document.setHtml(body)
    return document.idealWidth()


def fitted_tooltip(text: str, window_width: int, font=None) -> str:
    """@brief Tooltip text ready for QToolTip, wrapped to the allowed width.

    @param text Tooltip as the widget stores it.
    @param window_width Width of the window the tooltip belongs to, pixels.
    @param font Font the tooltip is drawn with (QToolTip.font() by default).
    @return Rich text. When the natural width exceeds the allowed one, the
        body sits in a fixed-width table, which is the one width Qt's rich
        text honours; otherwise it keeps its natural width.
    """
    body = tooltip_html(text)
    limit = max(120, min(TOOLTIP_MAX_WIDTH_PX, window_width - 2 * WINDOW_MARGIN_PX - _FRAME_X_PX))
    # `<qt>` dichiara il rich text: senza, un testo breve con un «&lt;»
    # scappato verrebbe mostrato alla lettera.
    if _natural_width(body, font or QToolTip.font()) <= limit:
        return f"<qt>{body}</qt>"
    return (f"<qt><table width='{limit}' cellspacing='0' cellpadding='0'>"
            f"<tr><td>{body}</td></tr></table></qt>")


def _window_rect(widget: QWidget) -> QRect:
    """Global rectangle of the window that owns `widget`."""
    window = widget.window()
    return QRect(window.mapToGlobal(QPoint(0, 0)), window.size())


def _tip_label():
    """The label Qt uses for the visible tooltip, or None."""
    for candidate in QApplication.topLevelWidgets():
        if candidate.objectName() == "qtooltip_label" and candidate.isVisible():
            return candidate
    return None


def place_inside(box: QRect, pointer: QPoint, area: QRect) -> QPoint:
    """@brief Top-left corner that keeps a tooltip box inside `area`.

    @param box Tooltip rectangle as Qt placed it (only its size is used).
    @param pointer Global position of the mouse.
    @param area Global rectangle of the window.
    @return Below and right of the pointer when that fits; above it when the
        bottom would leave the window; always clamped inside `area` with
        WINDOW_MARGIN_PX, and to its top-left when the box is larger.
    """
    x = pointer.x() + _QT_TIP_OFFSET.x()
    y = pointer.y() + _QT_TIP_OFFSET.y()
    if y + box.height() > area.bottom() - WINDOW_MARGIN_PX:
        y = pointer.y() - box.height() - 4
    x = min(x, area.right() - WINDOW_MARGIN_PX - box.width())
    y = min(y, area.bottom() - WINDOW_MARGIN_PX - box.height())
    x = max(x, area.left() + WINDOW_MARGIN_PX)
    y = max(y, area.top() + WINDOW_MARGIN_PX)
    return QPoint(x, y)


def show_tooltip(widget: QWidget, pointer: QPoint, text: str) -> None:
    """@brief Show `text` as a wrapped tooltip kept inside the widget's window.

    @param widget Widget the tooltip describes.
    @param pointer Global position of the mouse.
    @param text Tooltip text, plain or rich.
    """
    area = _window_rect(widget)
    QToolTip.showText(pointer, fitted_tooltip(text, area.width()), widget)
    label = _tip_label()
    if label is not None:
        label.move(place_inside(label.frameGeometry(), pointer, area))


def _text_for(watched: QWidget, event) -> str:
    """Tooltip text of the widget, or of the item under the pointer in a view."""
    text = watched.toolTip()
    if text:
        return text
    view = watched.parent()
    if isinstance(view, QAbstractItemView) and watched is view.viewport():
        index = view.indexAt(event.pos())
        value = index.data(Qt.ItemDataRole.ToolTipRole) if index.isValid() else None
        return value if isinstance(value, str) else ""
    return ""


class ToolTipFilter(QObject):
    """@brief Application event filter that shows every tooltip wrapped and inside the window.

    @note Only QEvent.ToolTip events that carry a text are taken over; any
        other event, or a widget without a tooltip, goes on to Qt untouched,
        so a parent's tooltip still answers for a child without one.
    """

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        """@brief Take over tooltip events that have a text to show.

        @param watched Object receiving the event.
        @param event The event.
        @return True when the tooltip was shown here.
        """
        if event.type() != QEvent.Type.ToolTip or not isinstance(watched, QWidget):
            return False
        text = _text_for(watched, event)
        if not text:
            return False
        show_tooltip(watched, event.globalPos(), text)
        return True


def install_tooltip_filter(app: QApplication) -> ToolTipFilter:
    """@brief Install the tooltip filter on the application, once.

    @param app The running QApplication.
    @return The installed filter (parented to `app`, so it lives as long).
    @note Also turns off the tooltip roll/fade effects: the box is moved inside
        the window right after Qt shows it, which an animation would prevent.
    """
    # Senza animazione: su Windows il tooltip entra con un effetto che lo tiene
    # nascosto per qualche frazione di secondo, e in quel tempo non si puo'
    # spostarlo dentro la finestra; spostato dopo, salterebbe sotto gli occhi.
    QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateTooltip, False)
    QApplication.setEffectEnabled(Qt.UIEffect.UI_FadeTooltip, False)
    existing = app.findChild(ToolTipFilter)
    if existing is not None:
        return existing
    tooltip_filter = ToolTipFilter(app)
    app.installEventFilter(tooltip_filter)
    return tooltip_filter
