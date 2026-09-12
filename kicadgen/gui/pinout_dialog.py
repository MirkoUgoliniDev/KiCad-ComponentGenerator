"""Finestra con la tabella pinout estratta dalla documentazione."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)


class PinoutDialog(QDialog):
    """Tabella numero pin -> funzione, in una finestra a parte.

    Sta in una modale e non sotto l'anteprima per lasciare al disegno tutta
    l'altezza del tab: il footprint con le quote e' gia' fitto di suo.
    """

    def __init__(self, pinout: list[dict], component_name: str = "", parent=None):
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
            for i, entry in enumerate(sorted(pinout, key=lambda e: e.get("pin", 0))):
                table.setItem(i, 0, QTableWidgetItem(str(entry.get("pin", ""))))
                table.setItem(i, 1, QTableWidgetItem(str(entry.get("name", ""))))
            layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
