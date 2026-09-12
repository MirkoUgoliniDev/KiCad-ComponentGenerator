"""Scelta del modello LLM dall'elenco OpenRouter, con filtro sul costo.

OpenRouter pubblica centinaia di modelli a prezzi che vanno da zero a
qualche decina di dollari per milione di token: la tabella qui sotto li
mostra con il loro costo e permette di filtrare per prezzo massimo, cosi'
si sceglie consapevolmente fra un modello economico per i datasheet
semplici e uno piu' capace per quelli difficili.

Di default sono elencati solo i modelli utilizzabili per questo lavoro
(tool calling + lettura di immagini e PDF): gli altri fallirebbero la
chiamata.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..services.openrouter import ModelInfo, OpenRouterError, list_models

#: Colonne della tabella.
_COL_ID, _COL_NAME, _COL_IN, _COL_OUT, _COL_CTX = range(5)


class _NumericItem(QTableWidgetItem):
    """Cella che si ordina per valore numerico ma si legge formattata.

    Senza questo, l'ordinamento della tabella sarebbe alfabetico e "10.00"
    finirebbe prima di "2.00": inutile proprio nella colonna che serve a
    confrontare i costi.
    """

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self._value = value
        self.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

    def __lt__(self, other) -> bool:  # noqa: D105 - contratto di QTableWidgetItem
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class ModelPickerDialog(QDialog):
    """Elenco modelli OpenRouter con ricerca, filtro costo e selezione.

    Il dialogo non salva nulla: ritorna solo l'id del modello scelto
    (`selected_model()`), e sta al chiamante decidere se diventa il default
    globale o vale solo per l'estrazione in corso.
    """

    def __init__(self, current_model: Optional[str] = None, parent=None, title: str = "Scegli il modello LLM"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 620)

        self._models: list[ModelInfo] = []
        self._selected = current_model or ""

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Modelli disponibili su OpenRouter. I prezzi sono in dollari per "
            "milione di token (input / output): un'estrazione da datasheet "
            "consuma soprattutto token di input, perche' manda il PDF o le "
            "immagini del disegno meccanico."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Cerca:"))
        self.field_search = QLineEdit()
        self.field_search.setPlaceholderText("es. claude, gemini, gpt...")
        self.field_search.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self.field_search, 2)

        filter_row.addWidget(QLabel("Costo input max ($/Mtok):"))
        self.spin_max_cost = QDoubleSpinBox()
        self.spin_max_cost.setDecimals(2)
        self.spin_max_cost.setRange(0.0, 1000.0)
        self.spin_max_cost.setSingleStep(0.5)
        self.spin_max_cost.setSpecialValueText("nessun limite")
        self.spin_max_cost.setValue(0.0)
        self.spin_max_cost.setToolTip(
            "0 = nessun limite. Filtra sui soli modelli il cui costo di input "
            "non supera il valore indicato."
        )
        self.spin_max_cost.valueChanged.connect(self._apply_filters)
        filter_row.addWidget(self.spin_max_cost)
        layout.addLayout(filter_row)

        options_row = QHBoxLayout()
        self.check_compatible = QCheckBox(
            "Solo modelli compatibili (tool calling + immagini + PDF)"
        )
        self.check_compatible.setChecked(True)
        self.check_compatible.setToolTip(
            "L'estrazione manda disegni meccanici e pretende una risposta "
            "strutturata: un modello senza queste capacita' non puo' funzionare."
        )
        self.check_compatible.toggled.connect(self._apply_filters)
        options_row.addWidget(self.check_compatible)
        options_row.addStretch()

        btn_refresh = QPushButton("Aggiorna elenco")
        btn_refresh.setToolTip("Riscarica l'elenco da OpenRouter ignorando la cache locale.")
        btn_refresh.clicked.connect(lambda: self._load_models(force_refresh=True))
        options_row.addWidget(btn_refresh)
        layout.addLayout(options_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Modello (id)", "Nome", "Input $/Mtok", "Output $/Mtok", "Contesto"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(
            _COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self.accept)
        layout.addWidget(self.table)

        self.label_selected = QLabel()
        self.label_selected.setWordWrap(True)
        layout.addWidget(self.label_selected)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._load_models()
        self._update_selected_label()

    # ── Dati ────────────────────────────────────────────────────────

    def _load_models(self, force_refresh: bool = False) -> None:
        error: Optional[str] = None
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._models = list_models(force_refresh=force_refresh)
        except OpenRouterError as exc:
            error = str(exc)
        finally:
            QGuiApplication.restoreOverrideCursor()

        if error is not None:
            QMessageBox.warning(self, "Elenco modelli", error)
            return
        self._apply_filters()

    def _visible_models(self) -> list[ModelInfo]:
        needle = self.field_search.text().strip().lower()
        max_cost = self.spin_max_cost.value()
        only_compatible = self.check_compatible.isChecked()

        result = []
        for model in self._models:
            if only_compatible and not model.usable_for_datasheets():
                continue
            if max_cost > 0 and model.prompt_usd_per_mtok > max_cost:
                continue
            if needle and needle not in model.id.lower() and needle not in model.name.lower():
                continue
            result.append(model)
        return result

    def _apply_filters(self) -> None:
        models = self._visible_models()

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(models))
        for row, model in enumerate(models):
            id_item = QTableWidgetItem(model.id)
            id_item.setData(Qt.ItemDataRole.UserRole, model.id)
            self.table.setItem(row, _COL_ID, id_item)
            self.table.setItem(row, _COL_NAME, QTableWidgetItem(model.name))
            self.table.setItem(
                row, _COL_IN,
                _NumericItem(_price_text(model.prompt_usd_per_mtok), model.prompt_usd_per_mtok),
            )
            self.table.setItem(
                row, _COL_OUT,
                _NumericItem(
                    _price_text(model.completion_usd_per_mtok), model.completion_usd_per_mtok
                ),
            )
            self.table.setItem(
                row, _COL_CTX,
                _NumericItem(f"{model.context_length:,}".replace(",", "."), model.context_length),
            )
        self.table.setSortingEnabled(True)
        self.table.sortItems(_COL_IN, Qt.SortOrder.AscendingOrder)
        self.table.resizeColumnToContents(_COL_ID)

        self._restore_selection()

    def _restore_selection(self) -> None:
        """Riseleziona il modello corrente, se ancora visibile coi filtri."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COL_ID)
            if item is not None and item.text() == self._selected:
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                return

    # ── Selezione ───────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            item = self.table.item(rows[0].row(), _COL_ID)
            if item is not None:
                self._selected = item.text()
        self._update_selected_label()

    def _update_selected_label(self) -> None:
        if self._selected:
            self.label_selected.setText(f"Modello scelto: {self._selected}")
        else:
            self.label_selected.setText("Nessun modello scelto.")
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(bool(self._selected))

    def selected_model(self) -> str:
        return self._selected


def _price_text(value: float) -> str:
    """Prezzo leggibile: 'gratis' a costo zero, altrimenti in dollari."""
    if value <= 0:
        return "gratis"
    if value < 1:
        return f"{value:.3f}"
    return f"{value:.2f}"


def pick_model(current_model: Optional[str], parent=None, title: str = "Scegli il modello LLM") -> Optional[str]:
    """Apre il dialogo e ritorna l'id scelto, o None se si annulla."""
    dialog = ModelPickerDialog(current_model, parent, title=title)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_model():
        return dialog.selected_model()
    return None


__all__ = ["ModelPickerDialog", "pick_model"]
