"""@brief Inside a scrolling page the mouse wheel scrolls the page, never a drop-down or a number.

@details On 2026-09-26 the component type of a project changed twice in one
second while the user was scrolling the Project tab: the wheel passed over
the «Tipologia componente» drop-down, which took it and changed value. The
type decides which body dimensions are read, so the photo review came out
six times off scale. The type had been chosen with a click a minute
before, so the drop-down still had the focus: a rule «the wheel works after
a click» would not have prevented it. The rule is therefore (user's
decision, 2026-09-26), for every QComboBox and spin box:

* inside a scroll area the wheel always scrolls the area, as if the widget
  were not there; values change by click, arrows or keyboard;
* outside a scroll area (a small dialog) the wheel changes the value only
  of the widget that has the focus; otherwise it is dropped;
* such widgets take the focus by click or Tab, not by wheel (Qt's default
  WheelFocus gives the focus *before* an event filter sees the wheel event).
"""
from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox

_GUARDED = (QComboBox, QAbstractSpinBox)


class WheelGuard(QObject):
    """@brief Application event filter implementing the rule of this module."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        """@brief Keep unfocused drop-downs and number fields out of the wheel.

        @param watched Object receiving the event.
        @param event The event.
        @return True when the wheel event was redirected or dropped.
        """
        if not isinstance(watched, _GUARDED):
            return False
        kind = event.type()
        if kind == QEvent.Type.Polish:
            if watched.focusPolicy() == Qt.FocusPolicy.WheelFocus:
                watched.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            return False
        if kind != QEvent.Type.Wheel:
            return False
        area = _scroll_area_of(watched)
        if area is not None:
            QApplication.sendEvent(area.viewport(), event)
            return True
        return not watched.hasFocus()


def _scroll_area_of(widget):
    """Nearest scroll area containing `widget` (a list or a page), or None."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


_INSTALLED: WheelGuard | None = None


def install(application=None) -> WheelGuard | None:
    """@brief Install the guard once on the application (idempotent).

    @param application QApplication; the current instance when None.
    @return The installed guard, or None without an application.
    @note Idempotent per application: calling it again for the same one
        installs nothing more; a new QApplication gets its own guard.
    """
    global _INSTALLED
    application = application or QApplication.instance()
    if application is None:
        return None
    # Installed once per application: if the previous one was destroyed
    # (it happens between one test and the next), the filter must be put back.
    if _INSTALLED is None or sip.isdeleted(_INSTALLED) or _INSTALLED.parent() is not application:
        _INSTALLED = WheelGuard(application)
        application.installEventFilter(_INSTALLED)
    return _INSTALLED
