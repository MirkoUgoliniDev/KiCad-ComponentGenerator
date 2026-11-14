"""@brief Dialog showing the pinout table extracted from the documentation."""

from __future__ import annotations

import re
from copy import deepcopy
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from ..services.messages import tx

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

#: Row key marking a name the dialog's caller PROPOSED (e.g. read from the pad
#: drawing) rather than one the pinout states. Such a row is drawn italic and
#: grey, and `edited_pinout` returns it only when the user changed its text.
PREFILLED_KEY = "_prefilled"
#: Foreground of a prefilled, still unedited name.
PREFILLED_COLOR = "#9e9e9e"
#: Tooltip of a prefilled name (Italian source, translated by the window).
PREFILLED_TIP = ("Nome proposto dal disegno dei pad, non dalla documentazione: da "
                 "verificare. Viene applicato solo se lo modifichi.")
#: Label of the «primary name only» switch (user, 2026-11-13; Italian source,
#: translated by the window's retranslation pass).
PRIMARY_NAMES_LABEL = "Solo nome principale (le altre funzioni diventano alternative in KiCad)"
#: Tooltip of that switch.
PRIMARY_NAMES_TIP = ("Il simbolo e l'anteprima del footprint usano il testo prima di «(»; le "
                     "funzioni fra parentesi, separate da virgole o \"/\", diventano funzioni "
                     "alternative del pin in KiCad. Un nome con \"/\" fuori dalle parentesi resta "
                     "intero. La tabella mostra sempre il testo completo.")


def pin_sort_key(entry: dict) -> tuple:
    """@brief Natural sort key for one pinout entry, robust to mixed pin types.

    @param entry Pinout row, e.g. {"pin": 3, "name": "GND"}; "pin" may be an
        int, a numeric string ("10"), an alphanumeric one ("A2") or missing.
    @return Key ordering 1, 2, 10 (not "1", "10", "2") and "A1", "A2", "A10";
        ints and strings compare by their text, never against each other, so
        a table mixing 3 and "3" or "A1" no longer raises TypeError. Rows
        without a pin come last.
    """
    pin = entry.get("pin") if isinstance(entry, dict) else None
    text = "" if pin is None else str(pin).strip()
    if not text:
        return (1, ())
    parts = re.split(r"(\d+)", text)
    # `isascii()` first: `str.isdigit()` is also true for «²» or «①», which
    # `\d` does not split off and `int()` refuses (review 2026-11-11, F9-3).
    return (0, tuple((0, int(part), "") if part.isascii() and part.isdigit() else (1, 0, part.casefold())
                     for part in parts if part != ""))


class PinoutDialog(QDialog):
    """@brief Modal table mapping each pin number to its function.

    It lives in a modal and not under the preview, to leave the drawing the
    whole height of the tab: the dimensioned footprint is already dense.
    """

    def __init__(self, pinout: list[dict], component_name: str = "", parent=None, *,
                 primary_names: bool = True):
        """@brief Build the dialog.

        @param pinout Rows {"pin": ..., "name": ...} as extracted; shown in
            natural pin order. A row may carry ``PREFILLED_KEY: True`` when its
            name is only a proposal (see `edited_pinout`).
        @param component_name Shown in the window title.
        @param parent Optional parent widget.
        @param primary_names Initial state of the «Solo nome principale»
            switch (the caller's saved preference, ON by default); read back
            with `primary_names()`. The table always shows the full text.
        """
        super().__init__(parent)
        self._entries = deepcopy(pinout)
        self._row_indices = sorted(range(len(pinout)), key=lambda i: pin_sort_key(pinout[i]))
        self.setWindowTitle(f"Pinout {component_name}".strip())
        self.resize(480, 420)

        layout = QVBoxLayout(self)

        if not pinout:
            layout.addWidget(QLabel(
                "Nessun pinout disponibile.\n\n"
                "Viene estratto dalla documentazione insieme agli altri parametri: "
                "seleziona i file nel tab \"Progetto\" e premi \"Estrai parametri con AI\"."
            ))
        else:
            caption = QLabel("Pin del footprint: i nomi vengono dalla documentazione o, se "
                             "mancano, dal disegno dei pad (da verificare).")
            caption.setWordWrap(True)
            layout.addWidget(caption)
            table = QTableWidget(len(pinout), 2)
            table.setHorizontalHeaderLabels(["Pin", "Funzione"])
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed
                                  | QAbstractItemView.EditTrigger.SelectedClicked)
            for i, source_index in enumerate(self._row_indices):
                entry = self._entries[source_index]
                pin = QTableWidgetItem(str(entry.get("pin", "")))
                pin.setFlags(pin.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(i, 0, pin)
                # A missing or None name is an empty cell, never the text «None».
                name = QTableWidgetItem(str(entry.get("name") or ""))
                if entry.get(PREFILLED_KEY):
                    self._mark_prefilled(name, True)
                table.setItem(i, 1, name)
            layout.addWidget(table)
            ## @brief The pinout table, exposed for tests and callers.
            self.table = table
            table.itemChanged.connect(self._restyle_prefilled)

        # The switch is shown only with a pinout to name, but always exists so
        # `primary_names()` returns the caller's choice unchanged otherwise.
        ## @brief The «Solo nome principale» switch, exposed for tests and callers.
        self.primary_names_check = QCheckBox(PRIMARY_NAMES_LABEL)
        self.primary_names_check.setToolTip(PRIMARY_NAMES_TIP)
        self.primary_names_check.setChecked(bool(primary_names))
        if pinout:
            layout.addWidget(self.primary_names_check)
        else:
            self.primary_names_check.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
            if pinout else QDialogButtonBox.StandardButton.Close)
        if pinout:
            buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def primary_names(self) -> bool:
        """@brief The state of the «Solo nome principale» switch when the dialog closed.

        @return True: the symbol takes the text before «(» as the pin name
            and the listed functions as KiCad alternates; False: the full
            text. The caller saves it as a preference.
        """
        return self.primary_names_check.isChecked()

    def _shown_name(self, index: int) -> str:
        """The name text the cell of source row `index` was opened with."""
        return str(self._entries[index].get("name") or "").strip()

    @staticmethod
    def _mark_prefilled(item: QTableWidgetItem, prefilled: bool) -> None:
        """Draw a name cell as a proposal (italic, grey, tooltip) or as a plain name."""
        font = item.font()
        font.setItalic(prefilled)
        item.setFont(font)
        if prefilled:
            item.setForeground(QColor(PREFILLED_COLOR))
            # Translated here: a table item's tooltip is not reached by the
            # window's retranslation pass.
            item.setToolTip(tx(PREFILLED_TIP))
        else:
            # Back to the table's own text colour and no proposal tooltip.
            item.setData(Qt.ItemDataRole.ForegroundRole, None)
            item.setToolTip("")

    def _restyle_prefilled(self, item: QTableWidgetItem) -> None:
        """Show a prefilled name as the user's own once its text differs."""
        if item.column() != 1:
            return
        index = self._row_indices[item.row()]
        if not self._entries[index].get(PREFILLED_KEY):
            return
        edited = item.text().strip() != self._shown_name(index)
        # Restyling changes the item again: no second pass through here.
        blocked = self.table.blockSignals(True)
        try:
            self._mark_prefilled(item, not edited)
        finally:
            self.table.blockSignals(blocked)

    def edited_pinout(self) -> list[dict]:
        """@brief Copy edited functions, preserving original pin IDs, order and metadata.

        @return A deep copy of the input rows with each `name` replaced by the
            stripped text typed in the table. A row flagged ``PREFILLED_KEY``
            is returned only when the user changed its text (a proposal left
            as proposed is not a pinout entry); the flag itself is never
            returned.
        """
        result = []
        rows = {index: row for row, index in enumerate(self._row_indices)}
        for index, entry in enumerate(self._entries):
            row = rows[index]
            text = self.table.item(row, 1).text().strip()
            copy = deepcopy(entry)
            prefilled = bool(copy.pop(PREFILLED_KEY, False))
            if prefilled and text == self._shown_name(index):
                continue
            copy['name'] = text
            result.append(copy)
        return result
