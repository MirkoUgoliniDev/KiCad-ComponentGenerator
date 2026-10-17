"""@brief Non-modal notification card that appears next to the cursor and fades away.

User request (2026-09-27): the «Copia formato» messages in the status bar were
hard to see; a popup was wanted, graphically professional, and NOT modal (a
dialog to close after every click would defeat a quick copy on many
components). The card has no buttons and takes no focus: it informs and goes.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QIcon
from PyQt6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                             QVBoxLayout, QWidget)

from .theme import BLUE, MANTLE, SUBTEXT, SURFACE1, TEXT

#: How long a card stays fully visible, in milliseconds, before fading out.
#: Long enough to read two short lines, short enough not to pile up on quick
#: successive clicks (each new message replaces the previous one).
TOAST_VISIBLE_MS = 3200
#: Fade-in and fade-out duration, in milliseconds.
TOAST_FADE_MS = 180
#: Offset of the card from the cursor, in logical pixels, so it never covers
#: the component just clicked.
TOAST_OFFSET_PX = 18
#: Minimum width of the text column, in logical pixels: two short sentences
#: on two or three lines instead of one word per line.
TOAST_TEXT_MIN_PX = 300


class Toast(QWidget):
    """@brief One reusable card: accent bar, icon, bold title and a line of text."""

    def __init__(self, parent=None, icon_asset: str = "", accent: str = BLUE):
        """@brief Build the (hidden) card.

        @param parent Window the card belongs to; the card floats above it.
        @param icon_asset Optional SVG in `gui/assets` shown at the left.
        @param accent Colour of the accent bar and of the title.
        """
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setProperty("i18n_skip", True)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 16)        # room for the shadow
        self.card = QFrame(self)
        self.card.setObjectName("toastCard")
        self.card.setStyleSheet(
            f"#toastCard {{ background: {MANTLE}; border: 1px solid {SURFACE1};"
            f" border-left: 4px solid {accent}; border-radius: 10px; }}")
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)
        row = QHBoxLayout(self.card)
        row.setContentsMargins(14, 10, 16, 11)
        row.setSpacing(12)
        self.icon = QLabel(self.card)
        if icon_asset:
            path = Path(__file__).parent / "assets" / icon_asset
            self.icon.setPixmap(QIcon(str(path)).pixmap(26, 26))
        self.icon.setVisible(bool(icon_asset))
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        self.title = QLabel(self.card)
        self.title.setStyleSheet(f"color: {accent}; font-weight: 700; font-size: 10.5pt;"
                                 " background: transparent; border: none;")
        self.body = QLabel(self.card)
        self.body.setWordWrap(True)
        self.body.setMaximumWidth(340)
        self.body.setStyleSheet(f"color: {TEXT}; font-size: 9.5pt;"
                                " background: transparent; border: none;")
        self.hint = QLabel(self.card)
        self.hint.setWordWrap(True)
        self.hint.setMaximumWidth(340)
        self.hint.setStyleSheet(f"color: {SUBTEXT}; font-size: 8.5pt;"
                                " background: transparent; border: none;")
        for label in (self.title, self.body, self.hint):
            label.setTextFormat(Qt.TextFormat.PlainText)
            # A label that wraps shrinks to the minimum possible: without a
            # minimum width the card wrapped at every word.
            label.setMinimumWidth(TOAST_TEXT_MIN_PX)
            texts.addWidget(label)
        row.addLayout(texts)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._after_fade)
        self._hiding = False

    def show_message(self, title: str, body: str = "", hint: str = "",
                     at: QPoint | None = None) -> None:
        """@brief Show (or replace) the card next to a global point, then fade it away.

        @param title Already translated first line, in the accent colour.
        @param body Already translated main text; may be empty.
        @param hint Already translated secondary line (what to do next); may be empty.
        @param at Global position to appear next to; None uses the cursor.
        @note A new message replaces the one on screen and restarts its
            timer: quick successive pastes show the latest state, not a stack.
        """
        self.title.setText(title)
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.adjustSize()
        self.move(self._position(at))
        self._fade.stop()
        self._hiding = False
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self.raise_()
        self._animate(1.0)
        self._timer.start(TOAST_VISIBLE_MS)

    def _position(self, at):
        """Top-left corner next to `at`, kept inside the screen that contains it."""
        point = at if at is not None else self.cursor().pos()
        target = QRect(point + QPoint(TOAST_OFFSET_PX, TOAST_OFFSET_PX), self.size())
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        if screen is None:
            return target.topLeft()
        area = screen.availableGeometry()
        if target.right() > area.right():
            target.moveRight(point.x() - TOAST_OFFSET_PX)
        if target.bottom() > area.bottom():
            target.moveBottom(point.y() - TOAST_OFFSET_PX)
        return QPoint(max(area.left(), target.left()), max(area.top(), target.top()))

    def _animate(self, opacity):
        self._fade.stop()
        self._fade.setDuration(TOAST_FADE_MS)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(opacity)
        self._fade.start()

    def _fade_out(self):
        self._hiding = True
        self._animate(0.0)

    def _after_fade(self):
        if self._hiding:
            self.hide()
            self._hiding = False
