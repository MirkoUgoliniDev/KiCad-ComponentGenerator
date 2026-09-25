"""@brief LLM model picker over the OpenRouter catalogue, with a cost filter.

Scelta del modello LLM dall'elenco OpenRouter, con filtro sul costo.

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

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..services.openrouter import (DATASHEET_CAPABILITIES, ModelInfo, OpenRouterError,
                                   list_models)
from ..services.messages import tx

#: Come si chiama, per chi guarda, ogni capacita' richiesta.
_CAPABILITY_LABELS = {"tools": "tool calling", "images": "immagini", "files": "PDF"}

#: Colonne della tabella.
_COL_ID, _COL_NAME, _COL_IN, _COL_OUT, _COL_CTX = range(5)


def _find_i18n(widget):
    """@brief Return the translation service owned by the nearest parent."""
    current = widget
    while current is not None:
        i18n = getattr(current, "i18n", None)
        if i18n is not None:
            return i18n
        current = current.parentWidget() if hasattr(current, "parentWidget") else None
    return None


class _ModelsWorker(QThread):
    """Scarica l'elenco modelli fuori dal thread grafico.

    L'elenco e' circa un megabyte e il timeout arriva a 30 secondi: farlo
    sul thread della GUI significherebbe una finestra congelata, senza
    nemmeno il modo di annullare.

    @note Il thread non appartiene alla finestra che lo avvia ma a
    QApplication. Un QThread ancora in corsa distrutto insieme alla finestra
    fa terminare l'intero processo — lo stesso rischio annotato in
    `main_window.choose_model` per i worker della revisione fotografica — e i
    30 secondi di timeout rendono la finestra chiusa in anticipo il caso
    normale, non l'eccezione. Con la proprieta' in mano a Qt il worker
    sopravvive alla finestra, si elimina da se' quando ha finito e l'uscita
    dall'applicazione lo attende invece di distruggerlo.
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, force_refresh: bool = False, parent=None):
        """@brief Prepare one model-list download owned by @p parent.

        @param force_refresh True to ignore the local models cache.
        @param parent Owner of the thread; pass QApplication.instance() so that
            no dialog can destroy the download while it is still running.
        """
        super().__init__(parent)
        self.force_refresh = force_refresh
        application = QApplication.instance()
        if application is not None:
            # Uscire dall'applicazione mentre il download e' in volo
            # distruggerebbe il thread in corsa: si attende invece che esca da
            # list_models. L'attesa non e' indefinita, il limite e' il timeout
            # HTTP della richiesta.
            application.aboutToQuit.connect(self.wait)
        self.finished.connect(self._release)

    def run(self) -> None:
        """@brief Provide the run operation for this workflow."""
        try:
            self.done.emit(list_models(force_refresh=self.force_refresh))
        except OpenRouterError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - riportato nel dialogo
            self.error.emit(tx("Errore inatteso: {exc}", exc=exc))

    def _release(self) -> None:
        """Scioglie l'attesa all'uscita e si elimina, una volta finito.

        La promessa di `wait` vale solo finche' il thread esiste: lasciandola
        attiva, all'uscita Qt chiamerebbe `wait` su un oggetto gia' eliminato
        da `deleteLater` e l'applicazione terminerebbe con RuntimeError. E' la
        stessa sequenza dei worker della revisione fotografica.
        """
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self.wait)
            except TypeError:
                # Mai connessa (nessuna QApplication alla costruzione) o gia'
                # sciolta: in entrambi i casi non c'e' nulla da sciogliere.
                pass
        self.deleteLater()


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
    """@brief Dialog listing OpenRouter models with search, cost filter and selection; it saves nothing.

    Elenco modelli OpenRouter con ricerca, filtro costo e selezione.

    Il dialogo non salva nulla: ritorna solo l'id del modello scelto
    (`selected_model()`), e sta al chiamante decidere se diventa il default
    globale o vale solo per l'estrazione in corso.
    """

    def __init__(self, current_model: Optional[str] = None, parent=None,
                 title: str = "Scegli il modello LLM", capabilities=DATASHEET_CAPABILITIES):
        super().__init__(parent)
        self.capabilities = tuple(capabilities)
        self.i18n = _find_i18n(parent)
        self.setWindowTitle(title)
        self.resize(900, 620)

        self._models: list[ModelInfo] = []
        self._selected = current_model or ""
        self._worker: Optional[_ModelsWorker] = None
        self._closed = False

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
        # Tradotto con l'i18n della finestra genitore, come il resto del
        # dialogo: il testo e' composto (le capacita' cambiano con lo stadio),
        # quindi la ritraduzione generica non lo troverebbe nel catalogo.
        source = "Solo modelli compatibili ({capabilities})"
        labels = " + ".join(self.i18n.text(_CAPABILITY_LABELS[name]) if self.i18n is not None
                            else _CAPABILITY_LABELS[name] for name in self.capabilities)
        self.check_compatible = QCheckBox(
            self.i18n.tr("ui:" + source, default=source, capabilities=labels)
            if self.i18n is not None else source.format(capabilities=labels))
        # Testo composto e gia' tradotto: la ritraduzione generica non deve
        # cercarlo nel catalogo come sorgente.
        self.check_compatible.setProperty("i18n_skip", True)
        self.check_compatible.setChecked(True)
        tooltip = ("L'estrazione manda disegni meccanici e pretende una risposta "
                   "strutturata: un modello senza queste capacita' non puo' funzionare.")
        self.check_compatible.setToolTip(self.i18n.text(tooltip) if self.i18n is not None
                                         else tooltip)
        self.check_compatible.toggled.connect(self._apply_filters)
        options_row.addWidget(self.check_compatible)
        options_row.addStretch()

        self.btn_refresh = QPushButton("Aggiorna elenco")
        self.btn_refresh.setToolTip(
            "Riscarica l'elenco da OpenRouter ignorando la cache locale."
        )
        self.btn_refresh.clicked.connect(lambda: self._load_models(force_refresh=True))
        options_row.addWidget(self.btn_refresh)
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

        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)
            self.spin_max_cost.setSpecialValueText(self.i18n.text("nessun limite"))
        self._update_selected_label()
        self._load_models()

    # ── Dati ────────────────────────────────────────────────────────

    def _alive(self) -> bool:
        """Vero finche' questa finestra puo' ancora essere aggiornata.

        Il download dura fino a 30 secondi e non viene interrotto: il risultato
        puo' quindi arrivare quando la finestra e' stata chiusa, oppure quando
        i suoi widget sono stati distrutti perche' e' morto il genitore. Un
        aggiornamento in uno dei due casi solleverebbe RuntimeError dentro uno
        slot Qt, e PyQt fa terminare il processo: si controlla prima.
        """
        return not self._closed and not sip.isdeleted(self)

    def _load_models(self, force_refresh: bool = False) -> None:
        if self._closed:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self.label_selected.setText(
            self.i18n.text("Caricamento elenco modelli da OpenRouter...")
            if self.i18n is not None else "Caricamento elenco modelli da OpenRouter..."
        )
        self.btn_refresh.setEnabled(False)

        # Proprieta' a QApplication, non a questa finestra: vedi _ModelsWorker.
        self._worker = _ModelsWorker(force_refresh, parent=QApplication.instance())
        self._worker.done.connect(self._on_models_loaded)
        self._worker.error.connect(self._on_models_error)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self) -> None:
        """Riabilita l'aggiornamento, se c'e' ancora una finestra da riabilitare.

        Un metodo e non una lambda: la lambda resterebbe viva insieme al worker
        (che ora sopravvive alla finestra) e toccherebbe widget gia' eliminati.
        """
        self._worker = None
        if not self._alive():
            return
        self.btn_refresh.setEnabled(True)

    def _detach_worker(self) -> None:
        """Slega il download in corso da questa finestra, senza fermarlo.

        Chi chiude non deve attendere il megabyte dell'elenco, e il thread non
        si puo' distruggere mentre gira: resta vivo sotto QApplication e si
        elimina da se' alla fine. Qui si tagliano solo i tre collegamenti verso
        questa finestra, cosi' un risultato tardivo non tocca piu' nulla.
        """
        worker = self._worker
        self._worker = None
        if worker is None or sip.isdeleted(worker):
            return
        for signal, slot in ((worker.done, self._on_models_loaded),
                             (worker.error, self._on_models_error),
                             (worker.finished, self._worker_finished)):
            try:
                signal.disconnect(slot)
            except TypeError:
                # Gia' sciolto: il worker puo' essere finito prima della
                # chiusura, e disconnettere due volte non e' un errore qui.
                pass

    def closeEvent(self, event) -> None:
        """@brief Detach a running download even when the dialog was never shown.

        @param event Qt close event.
        @note QDialog.closeEvent only calls reject() on a *visible* dialog, so a
        window closed before it was ever shown would never pass through done():
        this override is what covers that path.
        """
        self._closed = True
        self._detach_worker()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        """@brief Close immediately, letting a running download finish on its own.

        @param result Qt Accepted or Rejected dialog result.
        """
        self._closed = True
        self._detach_worker()
        super().done(result)

    def _on_models_loaded(self, models: list) -> None:
        if not self._alive():
            return
        self._models = models
        self._apply_filters()
        self._update_selected_label()

    def _on_models_error(self, message: str) -> None:
        if not self._alive():
            return
        self._update_selected_label()
        title = self.i18n.text("Elenco modelli") if self.i18n is not None else "Elenco modelli"
        QMessageBox.warning(self, title, message)

    def _visible_models(self) -> list[ModelInfo]:
        needle = self.field_search.text().strip().lower()
        max_cost = self.spin_max_cost.value()
        only_compatible = self.check_compatible.isChecked()

        result = []
        for model in self._models:
            if only_compatible and not model.usable_for(self.capabilities):
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
                _NumericItem(
                    _price_text(model.prompt_usd_per_mtok, self.i18n),
                    model.prompt_usd_per_mtok,
                ),
            )
            self.table.setItem(
                row, _COL_OUT,
                _NumericItem(
                    _price_text(model.completion_usd_per_mtok, self.i18n),
                    model.completion_usd_per_mtok,
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
            self.label_selected.setText(
                self.i18n.tr("model.selected", model=self._selected)
                if self.i18n is not None else f"Modello scelto: {self._selected}"
            )
        else:
            self.label_selected.setText(
                self.i18n.text("Nessun modello scelto.")
                if self.i18n is not None else "Nessun modello scelto."
            )
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(bool(self._selected))

    def selected_model(self) -> str:
        """@brief Return the id of the model chosen in the dialog."""
        return self._selected


def _price_text(value: float, i18n=None) -> str:
    """Prezzo leggibile: 'gratis' a costo zero, altrimenti in dollari."""
    if value <= 0:
        return i18n.text("gratis") if i18n is not None else "gratis"
    if value < 1:
        return f"{value:.3f}"
    return f"{value:.2f}"


def pick_model(current_model: Optional[str], parent=None, title: str = "Scegli il modello LLM",
               capabilities=DATASHEET_CAPABILITIES) -> Optional[str]:
    """@brief Open the model picker and return the chosen model id, or None when cancelled.

    Apre il dialogo e ritorna l'id scelto, o None se si annulla.

    @param capabilities Capacita' che il modello dovra' avere per il lavoro a
        cui e' destinato: vedi ModelInfo.usable_for. Chiederne piu' del
        necessario nasconde modelli validi.
    """
    dialog = ModelPickerDialog(current_model, parent, title=title, capabilities=capabilities)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_model():
        return dialog.selected_model()
    return None


__all__ = ["ModelPickerDialog", "pick_model"]
