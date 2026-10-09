"""Review AI suggestions before adding them to the symbol draft."""
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QDialogButtonBox, QPushButton, QHeaderView)
from ..services.pin_type_ai import suggest_pin_types
from .symbol_tab import _TYPE_LABELS


class _Worker(QThread):
    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, args, parent):
        super().__init__(parent)
        self.args = args

    def run(self):
        try:
            self.done.emit(suggest_pin_types(**self.args, progress=self.progress.emit))
        except Exception as exc:
            self.error.emit(str(exc))


class PinTypeDialog(QDialog):
    def __init__(self, args, parent=None, tx=lambda s: s):
        super().__init__(parent)
        self.args, self.tx = args, tx
        self.proposals = []
        self.worker = None
        self.setWindowTitle(tx('Suggerisci tipi pin con AI'))
        self.resize(960, 560)
        layout = QVBoxLayout(self)
        note = QLabel(tx('Invia pinout e documenti selezionati al modello AI configurato. Può comportare costi. Le proposte richiedono revisione.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        files = QLabel(args['model'] + '\n' + '\n'.join(str(p.name) for p in args['documents']))
        files.setWordWrap(True)
        layout.addWidget(files)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([tx(s) for s in ['Usa', 'Pin', 'Funzione', 'Tipo attuale', 'Proposta AI', 'Fonte e motivazione']])
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self.start = QPushButton(tx('Analizza con AI'))
        self.start.clicked.connect(self._start)
        buttons.addButton(self.start, QDialogButtonBox.ButtonRole.ActionRole)
        self.use = QPushButton(tx('Usa proposte selezionate'))
        self.use.setEnabled(False)
        self.use.clicked.connect(self.accept)
        buttons.addButton(self.use, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)

    def _start(self):
        self.start.setEnabled(False)
        self.worker = _Worker(self.args, self)
        self.worker.progress.connect(self.status.setText)
        self.worker.error.connect(self.status.setText)
        self.worker.done.connect(self._done)
        self.worker.finished.connect(lambda: self.use.setEnabled(bool(self.proposals)))
        self.worker.start()

    def _done(self, proposals):
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
                item.setToolTip(value)
                self.table.setItem(index, column, item)
        self.status.setText(self.tx('Controlla le fonti. I tipi già confermati non sono selezionati automaticamente.'))

    def selected(self):
        return [p for i, p in enumerate(self.proposals)
                if self.table.item(i, 0).checkState() == Qt.CheckState.Checked
                and p['electrical_type'] != 'unknown']

    def reject(self):
        if self.worker is None or not self.worker.isRunning():
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)
