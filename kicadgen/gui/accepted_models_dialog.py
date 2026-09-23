"""@brief Dialogs for the archive of accepted KiCad model matches.

- `ConsolidateDialog` opens from «Accetto, mi piace» after the solid is
  generated. It lists every board component, pre-checked when it has a model
  worth remembering; the others stay listed, not checkable, with the reason
  (user decision: the whole board in one click, with opt-out per row).
- `AcceptedModelsDialog` lists the archive entries and deletes wrong ones: a
  match accepted by mistake propagates to later boards, so removing it must be
  easy.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..services.accepted_models import (
    AcceptedModelStore, AcceptedModelsError, category_of, eligibility,
)


def _millimetres(*values) -> str:
    """Millimetre values with two decimals, "?" for a missing value."""
    parts = []
    for value in values:
        try:
            parts.append(f"{float(value):.2f}")
        except (TypeError, ValueError):
            parts.append("?")
    return " × ".join(parts)


def _item(text, *, data=None) -> QTableWidgetItem:
    """Read-only cell: rows are inspected and checked, never edited."""
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


class ConsolidateDialog(QDialog):
    """@brief Confirm which components to remember, showing why others are excluded."""

    #: Colonna della spunta (e del nome del componente).
    COLUMN_NAME = 0

    def __init__(self, components: list[dict], parent=None, i18n=None):
        """@brief Build the list from the board components.

        @param components Components with matching metadata (model_path,
            model_match, model_selection) as read from the table.
        @param parent Owner window.
        @param i18n Optional translator for text composed at runtime.
        """
        super().__init__(parent)
        self.i18n = i18n
        self.setWindowTitle("Ricorda questi modelli KiCad")
        self.resize(900, 520)
        self._components = list(components or [])
        layout = QVBoxLayout(self)
        intro = QLabel(
            "I componenti spuntati entrano nell'archivio degli abbinamenti accettati: "
            "in una scheda futura un corpo con la stessa categoria e le stesse misure "
            "(entro il 15%) ricevera' subito questo modello, come proposta da confermare. "
            "Togli la spunta a quelli che non ti convincono.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(len(self._components), 5)
        self.table.setHorizontalHeaderLabels(
            ["Componente", "Categoria", "Misure L × W (mm)", "Modello KiCad", "Stato"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._eligible = []
        for row, component in enumerate(self._components):
            ok, reason = eligibility(component)
            self._eligible.append(ok)
            name = _item(component.get("name") or "?")
            if ok:
                name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name.setCheckState(Qt.CheckState.Checked)
            else:
                # Non spuntabile: non c'e' niente da ricordare, e il motivo lo dice.
                name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
                              & ~Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, _item(category_of(component)))
            self.table.setItem(row, 2, _item(_millimetres(
                component.get("length_mm"), component.get("width_mm"))))
            model = component.get("model_path")
            self.table.setItem(row, 3, _item(Path(model).stem if ok else "—"))
            self.table.setItem(row, 4, _item(self._tr("da ricordare") if ok else self._tr(reason)))
        layout.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        # Testo composto a runtime e gia' tradotto da `_tr`: il passaggio di
        # `retranslate_widgets` non deve prenderlo come sorgente.
        self.summary.setProperty("i18n_skip", True)
        layout.addWidget(self.summary)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Ricorda i selezionati")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.table.itemChanged.connect(self._refresh_summary)
        self._refresh_summary()

    def _tr(self, source: str) -> str:
        """Translate runtime text when a translator is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def _refresh_summary(self, *_):
        chosen = len(self.selected_components())
        excluded = self._eligible.count(False)
        self.summary.setText(self._tr("Da ricordare: {chosen}. Senza modello da ricordare: {excluded}.")
                             .format(chosen=chosen, excluded=excluded))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(chosen > 0)

    def selected_components(self) -> list[dict]:
        """@brief Checked components, in table order.

        @return List of the original component mappings.
        """
        chosen = []
        for row, component in enumerate(self._components):
            item = self.table.item(row, self.COLUMN_NAME)
            if (self._eligible[row] and item is not None
                    and item.checkState() == Qt.CheckState.Checked):
                chosen.append(component)
        return chosen


class AcceptedModelsDialog(QDialog):
    """@brief List archive entries and delete the wrong ones."""

    def __init__(self, store: AcceptedModelStore | None = None, parent=None, i18n=None):
        """@brief Build the dialog and load the entries.

        @param store Archive to show; default the user's archive.
        @param parent Owner window.
        @param i18n Optional translator for text composed at runtime.
        """
        super().__init__(parent)
        self.i18n = i18n
        self.store = store or AcceptedModelStore()
        self.setWindowTitle("Archivio abbinamenti accettati")
        self.resize(980, 520)
        layout = QVBoxLayout(self)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Porta un percorso dell'utente: non si traduce.
        self.path_label.setProperty("i18n_skip", True)
        layout.addWidget(self.path_label)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Categoria", "Base", "Misure L × W (mm)", "Terminali", "Modello KiCad",
             "Rotazione", "Volte", "Ultima accettazione"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("i18n_skip", True)
        layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.btn_delete = QPushButton("Elimina le voci selezionate")
        self.btn_delete.setToolTip(
            "Toglie dall'archivio gli abbinamenti sbagliati: le schede future non li "
            "proporranno piu'. Non tocca i componenti gia' generati.")
        self.btn_delete.clicked.connect(self._delete_selected)
        row.addWidget(self.btn_delete)
        row.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)
        self.reload()

    def _tr(self, source: str) -> str:
        """Translate runtime text when a translator is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def reload(self) -> None:
        """@brief Reload the entries; an unreadable archive is reported, not hidden."""
        self.path_label.setText(str(self.store.path))
        try:
            entries = self.store.entries()
        except AcceptedModelsError as exc:
            entries = []
            self.status_label.setText(str(exc))
        else:
            self.status_label.setText(
                self._tr("Nessun abbinamento accettato finora.") if not entries else
                self._tr("Voci nell'archivio: {count}.").format(count=len(entries)))
        self.table.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            rotation = entry.get("rotation_deg")
            accepted = str(entry.get("last_accepted") or "")
            try:
                accepted = datetime.fromisoformat(accepted).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            values = [entry.get("category"), entry.get("basis"),
                      _millimetres(entry.get("long_mm"), entry.get("short_mm")),
                      entry.get("terminal_count") if entry.get("terminal_count") is not None else "—",
                      entry.get("model_name"),
                      f"{float(rotation):g}°" if rotation is not None else self._tr("automatica"),
                      entry.get("samples"), accepted]
            for column, value in enumerate(values):
                item = _item(value, data=entry.get("id") if column == 0 else None)
                if column == 4:
                    item.setToolTip(str(entry.get("model_ref") or ""))
                self.table.setItem(index, column, item)
        self._refresh_buttons()

    def _selected_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        ids = []
        for row in rows:
            item = self.table.item(row, 0)
            value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if type(value) is int:
                ids.append(value)
        return ids

    def _refresh_buttons(self) -> None:
        self.btn_delete.setEnabled(bool(self._selected_ids()))

    def _delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        try:
            removed = self.store.delete(ids)
        except AcceptedModelsError as exc:
            self.status_label.setText(str(exc))
            return
        self.reload()
        self.status_label.setText(
            self._tr("Voci eliminate: {count}.").format(count=removed))
