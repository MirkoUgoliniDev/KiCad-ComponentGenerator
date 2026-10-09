"""Background component verification with a copyable, explicit report."""
from PyQt6.QtCore import QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                             QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)
from ..services.component_check import verify_component, Check


class _CheckWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, args, parent):
        super().__init__(parent)
        self.args = args

    def run(self):
        try:
            verify_component(**self.args, emit=self.result.emit)
        except Exception as exc:
            self.result.emit(Check('ERROR', 'Verifica interrotta', str(exc)))


class ComponentCheckDialog(QDialog):
    def __init__(self, args, parent=None, tx=lambda s: s):
        super().__init__(parent)
        self.tx = tx
        self.results = []
        self.setWindowTitle(tx('Verifica componente per KiCad'))
        self.resize(820, 570)
        layout = QVBoxLayout(self)
        self.status = QLabel(tx('Verifica in corso: ogni comando KiCad ha un limite di 60 secondi.'))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        layout.addWidget(self.report)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setEnabled(False)
        buttons.rejected.connect(self.reject)
        copy = QPushButton(tx('Copia rapporto'))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText()))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)
        self.worker = _CheckWorker(args, self)
        self.worker.result.connect(self._result)
        self.worker.finished.connect(self._finished)
        self._running = True
        QTimer.singleShot(0, self.worker.start)

    def _result(self, result):
        self.results.append(result)
        labels = {'OK': 'Superato', 'ERROR': 'Errore', 'WARN': 'Avviso', 'SKIP': 'Non verificato'}
        self.report.appendPlainText(f'[{self.tx(labels[result.status])}] {self.tx(result.title)}\n{self.tx(result.detail)}\n')

    def _finished(self):
        self._running = False
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        counts = {status: sum(r.status == status for r in self.results) for status in ('OK', 'ERROR', 'WARN', 'SKIP')}
        self.status.setText(self.tx('Verifica terminata') + ': ' + ', '.join(
            f'{self.tx(label)}: {counts[key]}' for key, label in
            [('OK', 'Superato'), ('ERROR', 'Errore'), ('WARN', 'Avviso'), ('SKIP', 'Non verificato')]))
        self.close_button.setEnabled(True)

    def reject(self):
        if not self._running:
            super().reject()

    def closeEvent(self, event):
        if self._running:
            event.ignore()
        else:
            super().closeEvent(event)
