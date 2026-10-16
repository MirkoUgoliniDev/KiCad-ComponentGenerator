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
  fits and above it otherwise, and never outside the screen either (the part
  of the window that is on screen; the screen alone when none is).
* header sections of tables and trees go through the same path.

Short tooltips keep their natural size: the width cap applies only to text
that would be wider.
"""
import html

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QToolTip, QWidget

#: Widest a tooltip may be, in pixels. A reading choice, not a measurement:
#: about 60 characters of the default 9 pt interface font, the usual limit of
#: a comfortable line. Narrower windows lower it further (see below).
TOOLTIP_MAX_WIDTH_PX = 420

#: Distance kept between a tooltip and the edges of its window, in pixels.
WINDOW_MARGIN_PX = 8

#: Offset Qt adds to the requested position before placing a tooltip (see
#: QTipLabel::placeTip); undone here so the box lands where it is computed.
_QT_TIP_OFFSET = QPoint(2, 16)

#: First estimate of the horizontal room the tooltip box adds around its text
#: column (stylesheet padding 8 px + 1 px border per side, theme.APP_QSS).
#: It is only a first guess: Qt's tooltip label adds its own margin and the
#: document margin on top (44 px in all, measured on Windows with APP_QSS), so
#: `show_tooltip` measures the real box and narrows the column if it is still
#: wider than the room available.
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
    # `<qt>` declares the rich text: without it, a short text with an escaped
    # «&lt;» would be shown literally.
    if _natural_width(body, font or QToolTip.font()) <= limit:
        return f"<qt>{body}</qt>"
    return (f"<qt><table width='{limit}' cellspacing='0' cellpadding='0'>"
            f"<tr><td>{body}</td></tr></table></qt>")


def _window_rect(widget: QWidget) -> QRect:
    """Global rectangle of the window that owns `widget`."""
    window = widget.window()
    return QRect(window.mapToGlobal(QPoint(0, 0)), window.size())


def visible_area(window: QRect, screen) -> QRect:
    """@brief Where a tooltip may go: the part of the window that is on screen.

    @param window Global rectangle of the window.
    @param screen Available geometry of the screen under the pointer, or None.
    @return The intersection; the screen alone when the window is entirely
        off it; the window when the screen is unknown.
    @note Reviewer 3, defect 12: a window partly off screen kept the tooltip
        inside the window but pushed it off the screen.
    """
    if screen is None or screen.isEmpty():
        return QRect(window)
    common = window.intersected(screen)
    return common if not common.isEmpty() else QRect(screen)


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
    screen = QApplication.screenAt(pointer) or widget.screen()
    area = visible_area(_window_rect(widget), screen.availableGeometry() if screen else None)
    QToolTip.showText(pointer, fitted_tooltip(text, area.width()), widget)
    label = _tip_label()
    if label is None:
        return
    # The real frame (Qt margins included) is known only once the tooltip is
    # shown: if the box is still wider than the space, the column is narrowed
    # by the overflow. Previously the fixed estimate of 18 px let a long
    # tooltip go out on the right in a narrow window.
    excess = label.frameGeometry().width() - (area.width() - 2 * WINDOW_MARGIN_PX)
    if excess > 0:
        QToolTip.showText(pointer, fitted_tooltip(text, area.width() - excess), widget)
        label = _tip_label()
        if label is None:
            return
    label.move(place_inside(label.frameGeometry(), pointer, area))


def _text_for(watched: QWidget, event) -> str:
    """Tooltip text of the widget, or of the item or header section under the pointer."""
    text = watched.toolTip()
    if text:
        return text
    view = watched.parent() if not isinstance(watched, QHeaderView) else watched
    if isinstance(view, QHeaderView) and watched in (view, view.viewport()):
        # A header has no rows: `indexAt` finds nothing and the section
        # tooltip went to Qt, on a single line and outside the window
        # (reviewer 3, defect 11).
        section = view.logicalIndexAt(event.pos())
        model = view.model()
        value = (model.headerData(section, view.orientation(), Qt.ItemDataRole.ToolTipRole)
                 if section >= 0 and model is not None else None)
        return value if isinstance(value, str) else ""
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
    @note Every window gets `WA_AlwaysShowToolTips` when first shown. Qt
        otherwise sends no tooltip event at all to a window that is not the
        active one: after switching to another program and coming back with
        the mouse, without clicking, no icon showed its tooltip (user,
        2026-09-24, progress window and project bar). Measured: 0 of 4
        tooltips on an inactive window, 4 of 4 with the attribute.
    """

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        """@brief Take over tooltip events that have a text to show.

        @param watched Object receiving the event.
        @param event The event.
        @return True when the tooltip was shown here.
        """
        if not isinstance(watched, QWidget):
            return False
        if event.type() == QEvent.Type.Show:
            if watched.isWindow() and not watched.testAttribute(
                    Qt.WidgetAttribute.WA_AlwaysShowToolTips):
                watched.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            return False
        if event.type() != QEvent.Type.ToolTip:
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
    # No animation: on Windows the tooltip enters with an effect that keeps it
    # hidden for a fraction of a second, and during that time it cannot be
    # moved inside the window; moved afterwards, it would jump before the eyes.
    QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateTooltip, False)
    QApplication.setEffectEnabled(Qt.UIEffect.UI_FadeTooltip, False)
    existing = app.findChild(ToolTipFilter)
    if existing is not None:
        return existing
    tooltip_filter = ToolTipFilter(app)
    app.installEventFilter(tooltip_filter)
    return tooltip_filter
