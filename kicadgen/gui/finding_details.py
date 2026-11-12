"""@brief Modal window with the whole text of one check finding and its remedy.

@details User request (2026-11-12): the «Controlli» rows of the footprint tab
are cut by the 250 px column and the remedy was only a tooltip. A click on a
row of a finding list — the footprint's «Controlli» and the KiCad tab's
«Controlli KiCad (KLC)» alike — opens this window, centred on the main window
by Qt (a dialog with a parent is placed over it), with the severity, the full
message and the remedy, all selectable for copying.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

#: Item data role, on a row of a finding list, holding (severity, message,
#: hint) for `show_finding_details`; rows without it (the empty state
#: «Nessun problema trovato») open nothing.
FINDING_DETAILS_ROLE = Qt.ItemDataRole.UserRole + 2

#: (mark, Italian source word) of the two severities; the word is translated
#: by the caller's `translate`, the mark is the one of the list rows.
SEVERITY_TITLES = {"error": ("⛔", "Errore"), "warning": ("⚠", "Avviso")}


def finding_details(severity: str, message: str, hint: str) -> tuple:
    """@brief The value stored under FINDING_DETAILS_ROLE for one finding.

    @param severity `error` or `warning` (anything else reads as a warning).
    @param message The translated finding text.
    @param hint The translated remedy, '' when the rule has none.
    @return (severity, message, hint) as plain strings.
    """
    return (str(severity or ""), str(message or ""), str(hint or ""))


def build_finding_dialog(parent: QWidget | None, details, translate) -> QDialog | None:
    """@brief Build (not show) the modal window of one finding.

    @param parent Owner window; the dialog is centred on it.
    @param details The (severity, message, hint) tuple of a row, or None.
    @param translate Callable mapping an Italian source text to the
        interface language.
    @return The dialog, or None when `details` is not a finding (the
        empty-state row), so the caller opens nothing.
    """
    if not isinstance(details, tuple) or len(details) != 3 or not details[1]:
        return None
    severity, message, hint = details
    dialog = QDialog(parent)
    dialog.setObjectName("findingDetails")
    dialog.setWindowTitle(translate("Dettagli del controllo"))
    dialog.setModal(True)
    dialog.setMinimumWidth(460)
    # The texts are already translated: the window's i18n pass must not read
    # them back as catalogue sources.
    dialog.setProperty("i18n_skip", True)
    column = QVBoxLayout(dialog)
    mark, word = SEVERITY_TITLES.get(severity, SEVERITY_TITLES["warning"])
    title = QLabel(f"{mark} {translate(word)}")
    title.setObjectName("findingSeverity")
    font = title.font()
    font.setBold(True)
    title.setFont(font)
    column.addWidget(title)
    column.addWidget(_selectable(message, "findingMessage"))
    if hint:
        remedy = QLabel(translate("Come correggere:"))
        remedy.setObjectName("findingRemedyTitle")
        remedy_font = remedy.font()
        remedy_font.setBold(True)
        remedy.setFont(remedy_font)
        column.addSpacing(6)
        column.addWidget(remedy)
        column.addWidget(_selectable(hint, "findingRemedy"))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    close = buttons.button(QDialogButtonBox.StandardButton.Close)
    close.setText(translate("Chiudi"))
    column.addWidget(buttons)
    return dialog


def _selectable(text: str, name: str) -> QLabel:
    """A wrapped label whose text the mouse and the keyboard can select."""
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                  | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    return label


def show_finding_details(parent: QWidget | None, details, translate) -> bool:
    """@brief Open the modal window of one finding and wait for it to close.

    @param parent Owner window; the dialog is centred on it.
    @param details The (severity, message, hint) tuple of a row, or None.
    @param translate Callable mapping an Italian source text to the
        interface language.
    @return True when a window was shown; False for a row that is not a
        finding.
    @note The dialog is released after `exec()` (tests rule: a dialog kept
        alive can crash a later test through the garbage collector).
    """
    dialog = build_finding_dialog(parent, details, translate)
    if dialog is None:
        return False
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()
    return True
