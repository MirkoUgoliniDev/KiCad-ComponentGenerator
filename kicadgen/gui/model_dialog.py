"""@brief LLM model picker over the OpenRouter catalogue, with a cost filter.

Choice of the LLM model from the OpenRouter list, with a cost filter.

OpenRouter publishes hundreds of models at prices ranging from zero to a
few tens of dollars per million tokens: the table below shows them with
their cost and allows filtering by maximum price, so that one chooses
knowingly between a cheap model for simple datasheets and a more capable
one for the difficult ones.

By default only the models usable for this job are listed (tool calling +
reading of images and PDFs): the others would fail the call.
"""

from __future__ import annotations

from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..services.openrouter import (DATASHEET_CAPABILITIES, ModelInfo, OpenRouterError,
                                   list_models)
from ..services.providers import (PROVIDER_DEEPSEEK, PROVIDER_LABELS, PROVIDER_SHORT_LABELS,
                                  PROVIDERS,
                                  deepseek_models, provider_of)
from ..services.messages import tx

#: What each required capability is called, for the viewer.
_CAPABILITY_LABELS = {"tools": "tool calling", "images": "immagini", "files": "PDF"}

#: Columns of the table.
_COL_ID, _COL_NAME, _COL_IN, _COL_OUT, _COL_CTX = range(5)


def _find_i18n(widget):
    """@brief Return the translation service owned by the nearest parent.

    @param widget Widget from which the parent chain is walked upwards.
    @return The first `i18n` attribute found on the chain, or None.
    """
    current = widget
    while current is not None:
        i18n = getattr(current, "i18n", None)
        if i18n is not None:
            return i18n
        current = current.parentWidget() if hasattr(current, "parentWidget") else None
    return None


class _ModelsWorker(QThread):
    """@brief Download the model list off the GUI thread.

    The list is about one megabyte and the timeout reaches 30 seconds: doing
    it on the GUI thread would mean a frozen window, without even a way to
    cancel.

    @note The thread does not belong to the window that starts it but to
    QApplication. A QThread still running and destroyed together with the
    window terminates the whole process — the same risk noted in
    `main_window.choose_model` for the photo review workers — and the 30 s
    timeout makes the window closed early the normal case, not the
    exception. With the ownership in Qt's hands the worker outlives the
    window, deletes itself when finished and the application exit waits for
    it instead of destroying it.
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, force_refresh: bool = False, parent=None, provider="openrouter"):
        """@brief Prepare one model-list download owned by @p parent.

        @param force_refresh True to ignore the local models cache.
        @param provider Which provider's list to read (providers.PROVIDERS).
        @param parent Owner of the thread; pass QApplication.instance() so that
            no dialog can destroy the download while it is still running.
        """
        super().__init__(parent)
        self.force_refresh = force_refresh
        self.provider = provider
        application = QApplication.instance()
        if application is not None:
            # Quitting the application while the download is in flight would
            # destroy the running thread: instead we wait for it to leave
            # list_models. The wait is not unbounded, the limit is the HTTP
            # timeout of the request.
            application.aboutToQuit.connect(self.wait)
        self.finished.connect(self._release)

    def run(self) -> None:
        """@brief QThread override: download the provider's model list and emit `done` or `error`."""
        try:
            if self.provider == PROVIDER_DEEPSEEK:
                self.done.emit(deepseek_models())
            else:
                self.done.emit(list_models(force_refresh=self.force_refresh))
        except OpenRouterError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported in the dialog
            self.error.emit(tx("Errore inatteso: {exc}", exc=exc))

    def _release(self) -> None:
        """Drop the exit-time wait and delete the thread, once finished.

        The `wait` promise is valid only while the thread exists: left
        connected, at exit Qt would call `wait` on an object already removed
        by `deleteLater` and the application would terminate with a
        RuntimeError. It is the same sequence as the photo review workers.
        """
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self.wait)
            except TypeError:
                # Never connected (no QApplication at construction) or already
                # disconnected: in both cases there is nothing to disconnect.
                pass
        self.deleteLater()


class _NumericItem(QTableWidgetItem):
    """@brief Cell that sorts by numeric value but reads formatted.

    Without it the table ordering would be alphabetical and "10.00" would
    end up before "2.00": useless precisely in the column meant to compare
    costs.
    """

    def __init__(self, text: str, value: float):
        """@brief Show `text` while sorting by `value`.

        @param text Formatted cell text shown to the user (for example "2.00").
        @param value Numeric sort key; NaN (undeclared price) sorts last.
        """
        super().__init__(text)
        # An undeclared price (NaN, DeepSeek API) goes to the bottom: NaN is
        # not orderable and an inconsistent comparison breaks Qt's sorting.
        self._value = value if value == value else float("inf")
        self.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

    def __lt__(self, other) -> bool:  # noqa: D105 - QTableWidgetItem contract
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class ModelPickerDialog(QDialog):
    """@brief Dialog listing OpenRouter models with search, cost filter and selection; it saves nothing.

    List of OpenRouter models with search, cost filter and selection.

    The dialog saves nothing: it only returns the id of the chosen model
    (`selected_model()`), and it is up to the caller to decide whether it
    becomes the global default or applies only to the extraction in progress.
    """

    def __init__(self, current_model: Optional[str] = None, parent=None,
                 title: str = "Scegli il modello LLM", capabilities=DATASHEET_CAPABILITIES,
                 provider: Optional[str] = None):
        """@brief Build the picker.

        @param current_model Model selected when the dialog opens.
        @param parent Parent widget; its `i18n` service, if any, translates the dialog.
        @param title Window title.
        @param capabilities Capabilities the model must have (ModelInfo.usable_for).
        @param provider Source fixed by the caller (the «Modello» tab of the
            LLM settings): only its models are listed and the source cannot be
            changed here. None lets the user choose, starting from the
            provider of `current_model`.
        """
        super().__init__(parent)
        self.capabilities = tuple(capabilities)
        self.i18n = _find_i18n(parent)
        self.setWindowTitle(title)
        self.resize(900, 620)

        self._models: list[ModelInfo] = []
        #: What the table rows show (models, language); see `_apply_filters`.
        self._shown_key = None
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

        # API provider (user, 2026-09-27): OpenRouter or the direct DeepSeek
        # API (key DEEPSEEK_API_KEY). It starts from the one of the model
        # already chosen.
        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Fornitore:"))
        fixed_provider = provider
        self.combo_provider = QComboBox()
        for source in PROVIDERS:
            # Translated here: retranslate_widgets does not touch combo items (B7).
            self.combo_provider.addItem(tx(PROVIDER_LABELS[source]), source)
        self.combo_provider.setCurrentIndex(
            max(0, self.combo_provider.findData(fixed_provider or provider_of(current_model))))
        # The source chosen in the «Modello» (model) tab is not changed here:
        # the list is only its own.
        self.combo_provider.setEnabled(fixed_provider is None)
        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self.combo_provider)
        self.label_provider_note = QLabel()
        self.label_provider_note.setWordWrap(True)
        provider_row.addWidget(self.label_provider_note, 1)
        layout.addLayout(provider_row)

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
        # Translated with the parent window's i18n, like the rest of the
        # dialog: the text is composed (the capabilities change with the
        # stage), so the generic retranslation would not find it in the catalogue.
        source = "Solo modelli compatibili ({capabilities})"
        labels = " + ".join(self.i18n.text(_CAPABILITY_LABELS[name]) if self.i18n is not None
                            else _CAPABILITY_LABELS[name] for name in self.capabilities)
        self.check_compatible = QCheckBox(
            self.i18n.tr("ui:" + source, default=source, capabilities=labels)
            if self.i18n is not None else source.format(capabilities=labels))
        # Text composed and already translated: the generic retranslation must
        # not look it up in the catalogue as a source.
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

    # ── Data ────────────────────────────────────────────────────────

    def _alive(self) -> bool:
        """True while this window can still be updated.

        The download lasts up to 30 seconds and is not interrupted: the result
        may therefore arrive when the window has been closed, or when its
        widgets have been destroyed because the parent died. An update in
        either case would raise RuntimeError inside a Qt slot, and PyQt
        terminates the process: it is checked first.
        """
        return not self._closed and not sip.isdeleted(self)

    def _load_models(self, force_refresh: bool = False) -> None:
        if self._closed:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        # The list comes from the provider chosen in the dialog, not always
        # from OpenRouter: the message names it.
        template = "Caricamento elenco modelli da {provider}..."
        provider = PROVIDER_SHORT_LABELS.get(self.provider(), self.provider())
        self.label_selected.setText(
            self.i18n.tr("ui:" + template, default=template, provider=provider)
            if self.i18n is not None else template.format(provider=provider)
        )
        self.btn_refresh.setEnabled(False)

        # Property of QApplication, not of this window: see _ModelsWorker.
        self._update_provider_note()
        self._worker = _ModelsWorker(force_refresh, parent=QApplication.instance(),
                                     provider=self.provider())
        self._worker.done.connect(self._on_models_loaded)
        self._worker.error.connect(self._on_models_error)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def provider(self) -> str:
        """@brief Provider chosen in the dialog (providers.PROVIDERS).

        @return The provider key of the current combo entry, or the first
            provider when the combo carries no data.
        """
        return self.combo_provider.currentData() or PROVIDERS[0]

    def _on_provider_changed(self, _index) -> None:
        """Another provider: its list replaces the current one."""
        self._models = []
        self._apply_filters()
        if self._worker is not None and self._worker.isRunning():
            self._detach_worker()
        self._load_models()

    def _update_provider_note(self) -> None:
        """What the chosen provider does not declare, said once above the table."""
        note = ("L'API di DeepSeek non dichiara prezzi ne' capacita' dei modelli: la spesa "
                "non viene riportata e le capacita' vanno verificate sulla sua "
                "documentazione (le immagini le legge deepseek-flash, non la versione Pro)."
                if self.provider() == PROVIDER_DEEPSEEK else "")
        self.label_provider_note.setText(self.i18n.text(note) if (note and self.i18n is not None)
                                         else note)

    def _worker_finished(self) -> None:
        """Re-enable the refresh, if there is still a window to re-enable it on.

        A method and not a lambda: the lambda would stay alive together with
        the worker (which now outlives the window) and would touch widgets
        already deleted.
        """
        self._worker = None
        if not self._alive():
            return
        self.btn_refresh.setEnabled(True)

    def _detach_worker(self) -> None:
        """Detach the running download from this window, without stopping it.

        Whoever closes must not wait for the megabyte of the list, and the
        thread cannot be destroyed while it runs: it stays alive under
        QApplication and deletes itself at the end. Only the three connections
        towards this window are cut here, so that a late result touches
        nothing any more.
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
                # Already disconnected: the worker may have finished before
                # closing, and disconnecting twice is not an error here.
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
            # Undeclared capabilities (DeepSeek API): neither known compatible
            # nor known incompatible, so the filter does not hide it.
            if (only_compatible and getattr(model, "capabilities_declared", True)
                    and not model.usable_for(self.capabilities)):
                continue
            if max_cost > 0 and model.prompt_usd_per_mtok > max_cost:
                continue
            if needle and needle not in model.id.lower() and needle not in model.name.lower():
                continue
            result.append(model)
        return result

    def _apply_filters(self) -> None:
        """Show the models that pass the filters, sorted by input price, the chosen one selected.

        The rows are rebuilt only when what they show changes (the visible
        models, field by field, or the language of the price texts): a
        keystroke that keeps the same models visible, as most do while a
        prefix is typed, re-sorts and re-selects without recreating several
        thousand cells.
        """
        models = self._visible_models()
        key = (tuple(models), getattr(self.i18n, "language", None))
        if key == self._shown_key and self.table.rowCount() == len(models):
            self.table.sortItems(_COL_IN, Qt.SortOrder.AscendingOrder)
            self._restore_selection()
            return
        self._shown_key = key

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
        """Re-select the current model, if still visible with the filters."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COL_ID)
            if item is not None and item.text() == self._selected:
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                return

    # ── Selection ───────────────────────────────────────────────────

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
        """@brief Return the id of the model chosen in the dialog.

        @return The selected model id, or "" when nothing is selected.
        """
        return self._selected


def _price_text(value: float, i18n=None) -> str:
    """Readable price: 'gratis' at zero cost, 'n.d.' when the provider does not declare it."""
    if value != value:  # NaN: undeclared price (DeepSeek API)
        return i18n.text("n.d.") if i18n is not None else "n.d."
    if value <= 0:
        return i18n.text("gratis") if i18n is not None else "gratis"
    if value < 1:
        return f"{value:.3f}"
    return f"{value:.2f}"


def pick_model(current_model: Optional[str], parent=None, title: str = "Scegli il modello LLM",
               capabilities=DATASHEET_CAPABILITIES, provider: Optional[str] = None) -> Optional[str]:
    """@brief Open the model picker and return the chosen model id, or None when cancelled.

    Opens the dialog and returns the chosen id, or None on cancel.

    @param current_model Model pre-selected when the dialog opens.
    @param parent Parent widget of the dialog.
    @param title Window title.
    @param capabilities Capabilities the model must have for the job it is
        meant for: see ModelInfo.usable_for. Asking for more than needed
        hides valid models.
    @param provider Source fixed by the caller, or None (see ModelPickerDialog).
    @return The chosen model id, or None when the dialog is cancelled or
        nothing is selected.
    """
    dialog = ModelPickerDialog(current_model, parent, title=title, capabilities=capabilities,
                               provider=provider)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_model():
        return dialog.selected_model()
    return None


__all__ = ["ModelPickerDialog", "pick_model"]
