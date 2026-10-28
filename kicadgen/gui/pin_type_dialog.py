"""@brief Review AI suggestions before adding them to the symbol draft."""
from html import escape
from pathlib import Path
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QDialogButtonBox, QPushButton, QHeaderView,
    QHBoxLayout, QProgressBar)
from ..services.pin_type_ai import suggest_pin_types
from ..services.ai_diagnostics import CallCancelled
from .symbol_tab import _TYPE_LABELS


class _Worker(QThread):
    """Ask the AI for pin types off the GUI thread, cancellable between calls.

    @note Ownership (review 2026-10-27, C7): the thread belongs to
        QApplication, as `model_dialog._ModelsWorker` does. A QThread
        destroyed while running aborts the process, and the dialog can be
        destroyed without `closeEvent` (parent window deleted, application
        quit); owned by QApplication the worker outlives the dialog, deletes
        itself when finished and the application exit waits for it.
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, args, parent=None):
        """@brief Prepare one AI pin-type request owned by QApplication.

        @param args Keyword arguments of `suggest_pin_types`.
        @param parent Ignored for ownership when a QApplication exists (see
            the class note); kept for signature compatibility.
        """
        application = QApplication.instance()
        super().__init__(application if application is not None else parent)
        self.args = args
        if application is not None:
            # Quitting with the request in flight would destroy the running
            # thread: wait for it instead, bounded by the HTTP timeout.
            application.aboutToQuit.connect(self.wait)
        self.finished.connect(self._release)

    def run(self):
        """@brief QThread override: emit `done` with the proposals, `error` with a message, nothing when cancelled."""
        try:
            result = suggest_pin_types(**self.args, progress=self.progress.emit,
                                       cancelled=self._check_cancel)
            self._check_cancel()
            self.done.emit(result)
        except CallCancelled:
            pass
        except Exception as exc:
            self.error.emit(str(exc))

    def _check_cancel(self):
        if self.isInterruptionRequested():
            raise CallCancelled()

    def _release(self):
        """Drop the exit-time wait and delete the thread once finished.

        Left connected, at exit Qt would call `wait` on an object already
        removed by `deleteLater` (same sequence as `model_dialog._ModelsWorker`).
        """
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self.wait)
            except TypeError:
                # Never connected or already disconnected: nothing to undo.
                pass
        self.deleteLater()


class PinTypeDialog(QDialog):
    """@brief Present document-backed AI proposals for explicit user review."""
    analysis_started = pyqtSignal()
    analysis_finished = pyqtSignal()

    def __init__(self, args, parent=None, tx=lambda s: s):
        """@brief Build the proposal table; the AI request starts only on «Analizza con AI».

        @param args Keyword arguments of `suggest_pin_types`: name, rows,
            documents and model; the file names are listed in the window.
        @param parent Owner window.
        @param tx Translation function (Italian source -> shown text).
        """
        super().__init__(parent)
        self.args, self.tx = args, tx
        self.proposals = []
        self.worker = None
        self._closing = False
        self.setWindowTitle(tx('Suggerisci tipi pin con AI'))
        self.resize(960, 560)
        layout = QVBoxLayout(self)
        note = QLabel(tx('Invia pinout e documenti selezionati al modello AI configurato. Può comportare costi. Le proposte richiedono revisione.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        files = QLabel(args['model'] + '\n' + '\n'.join(str(p.name) for p in args['documents']))
        files.setTextFormat(Qt.TextFormat.PlainText)
        files.setWordWrap(True)
        layout.addWidget(files)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([tx(s) for s in ['Usa', 'Pin', 'Funzione', 'Tipo attuale', 'Proposta AI', 'Fonte e motivazione']])
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._update_use)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        assets = Path(__file__).parent / 'assets'
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText('')
        cancel.setIcon(QIcon(str(assets / 'cancel.svg')))
        cancel.setIconSize(QSize(20, 20))
        cancel.setToolTip(tx('Annulla'))
        self.start = QPushButton(tx('Analizza con AI'))
        self.start.setIcon(QIcon(str(assets / 'pin-type-analyze.svg')))
        self.start.setIconSize(QSize(20, 20))
        self.start.setText('')
        self.start.setToolTip(tx('Analizza con AI'))
        self.start.clicked.connect(self._start)
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.ActionRole)
        self.use = QPushButton(tx('Usa proposte selezionate'))
        self.use.setIcon(QIcon(str(assets / 'pin-type-apply.svg')))
        self.use.setIconSize(QSize(20, 20))
        self.use.setText('')
        self.use.setToolTip(tx('Usa proposte selezionate'))
        self.use.setEnabled(False)
        self.use.clicked.connect(self.accept)
        buttons.addButton(self.use, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)
        self.loading = QProgressBar()
        self.loading.setRange(0, 0)
        self.loading.setTextVisible(False)
        self.loading.setFixedHeight(6)
        layout.insertWidget(layout.count() - 1, self.loading)
        self.loading.hide()

    def _start(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.proposals = []
        self.table.setRowCount(0)
        self.use.setEnabled(False)
        self._closing = False
        self.start.setEnabled(False)
        self.start.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'wf-loading.svg')))
        self.loading.show()
        self.status.setText(self.tx('Analisi in corso…'))
        # Owned by QApplication, not by this window: see _Worker.
        self.worker = _Worker(self.args)
        self.worker.progress.connect(self.status.setText)
        self.worker.error.connect(self.status.setText)
        self.worker.done.connect(self._done)
        self.worker.finished.connect(self._finished)
        self.analysis_started.emit()
        self.worker.start()

    def _finished(self):
        self.analysis_finished.emit()
        if self._closing:
            super().reject()
            return
        self.start.setEnabled(True)
        self.start.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'pin-type-analyze.svg')))
        self.loading.hide()
        self._update_use()
        # The finished worker deletes itself on the next event-loop turn
        # (see _Worker._release): a reference kept past that point would be a
        # deleted C++ object, so it is dropped here, after the last use.
        self.worker = None

    def _update_use(self):
        running = self.worker is not None and self.worker.isRunning()
        self.use.setEnabled(not running and bool(self.selected()))

    def _done(self, proposals):
        if self._closing:
            return
        self.table.blockSignals(True)
        self.proposals = proposals
        rows = {str(r['pin']): r for r in self.args['rows']}
        self.table.setRowCount(len(proposals))
        for index, p in enumerate(proposals):
            row = rows[p['pin']]
            known = p['electrical_type'] != 'unknown'
            values = ['', p['pin'], row.get('name', ''),
                self.tx(_TYPE_LABELS.get(row.get('electrical_type', 'passive'), 'Passivo')),
                self.tx(_TYPE_LABELS[p['electrical_type']]) if known else self.tx('Da verificare'),
                p['source'] + ': ' + p['reason']]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == 0 and known:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked if row.get('electrical_type_reviewed') else Qt.CheckState.Checked)
                item.setToolTip('<qt>' + escape(value).replace('\n', '<br>') + '</qt>')
                self.table.setItem(index, column, item)
        self.status.setText(self.tx('Controlla le fonti. I tipi già confermati non sono selezionati automaticamente.'))
        self.table.blockSignals(False)
        self._update_use()

    def selected(self):
        """@brief Return checked proposals with known electrical types.

        @return The proposal dicts whose table row is checked and whose
            `electrical_type` is not 'unknown', in table order.
        """
        return [p for i, p in enumerate(self.proposals)
                if self.table.item(i, 0) is not None
                and self.table.item(i, 0).checkState() == Qt.CheckState.Checked
                and p['electrical_type'] != 'unknown']

    def reject(self):
        """@brief Request cooperative cancellation before dismissing an active AI worker."""
        if self.worker is not None and self.worker.isRunning():
            self._closing = True
            self.worker.requestInterruption()
            self.status.setText(self.tx('Annullamento in corso: attendo la richiesta AI già inviata.'))
            self.use.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event):
        """@brief Defer closure until the AI worker can finish safely.

        @param event Qt close event; ignored while the worker is running.
        """
        if self.worker is not None and self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
