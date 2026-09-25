"""@brief Dialog showing the pinout table extracted from the documentation."""

from __future__ import annotations

import re

from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)


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
    return (0, tuple((0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
                     for part in parts if part != ""))


class PinoutDialog(QDialog):
    """@brief Modal table mapping each pin number to its function.

    Sta in una modale e non sotto l'anteprima per lasciare al disegno tutta
    l'altezza del tab: il footprint con le quote e' gia' fitto di suo.
    """

    def __init__(self, pinout: list[dict], component_name: str = "", parent=None):
        """@brief Build the dialog.

        @param pinout Rows {"pin": ..., "name": ...} as extracted; shown in
            natural pin order.
        @param component_name Shown in the window title.
        @param parent Optional parent widget.
        """
        super().__init__(parent)
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
            layout.addWidget(QLabel("Corrispondenza estratta dalla documentazione:"))
            table = QTableWidget(len(pinout), 2)
            table.setHorizontalHeaderLabels(["Pin", "Funzione"])
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            for i, entry in enumerate(sorted(pinout, key=pin_sort_key)):
                table.setItem(i, 0, QTableWidgetItem(str(entry.get("pin", ""))))
                table.setItem(i, 1, QTableWidgetItem(str(entry.get("name", ""))))
            layout.addWidget(table)
            ## @brief The pinout table, exposed for tests and callers.
            self.table = table

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
