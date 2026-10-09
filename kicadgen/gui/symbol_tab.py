"""Editable electrical pin mapping and a preview of the exported symbol layout."""
from __future__ import annotations

from copy import deepcopy
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..generators.symbol import ELECTRICAL_TYPES, SymbolPin, layout_symbol
from .pinout_dialog import pin_sort_key

_TYPE_LABELS = {
    'input': 'Ingresso', 'output': 'Uscita', 'bidirectional': 'Bidirezionale',
    'tri_state': 'Tre stati', 'passive': 'Passivo', 'free': 'Libero',
    'unspecified': 'Non specificato', 'power_in': 'Ingresso alimentazione',
    'power_out': 'Uscita alimentazione', 'open_collector': 'Collettore aperto',
    'open_emitter': 'Emettitore aperto', 'no_connect': 'Non connesso',
}


class _SymbolView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor('#181825'))
        self.auto_fit = True

    def fit(self):
        self.auto_fit = True
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor):
        if 0.15 <= self.transform().m11() * factor <= 20:
            self.auto_fit = False
            self.scale(factor, factor)

    def wheelEvent(self, event):
        self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit()

    def showEvent(self, event):
        super().showEvent(event)
        if self.auto_fit:
            self.fit()


class SymbolTab(QWidget):
    """Draft edits are local until Apply; exports always use applied rows.

    ``set_data`` accepts existing pinout dictionaries (``pin``, ``name``),
    preserving their metadata. Pin IDs cannot be edited. ``pad_ids`` optionally
    supplies the actual footprint IDs, including pads without a pinout name.
    The owner persists edits_applied and performs export_requested.
    """
    edits_applied = pyqtSignal(list)
    export_requested = pyqtSignal()

    def __init__(self, parent=None, tx=None):
        super().__init__(parent)
        self.tx = tx or (lambda value: value)
        self._baseline = []
        self._original = []
        self._loading = False
        self._name = ''
        self._footprint = ''
        self._source_key = None
        self._error = ''
        layout = QVBoxLayout(self)
        self.title = QLabel(self.tx('Simbolo elettrico'))
        layout.addWidget(self.title)
        self.hint = QLabel(self.tx('Verifica nomi e tipi elettrici prima di esportare. I tipi non confermati sono passivi.'))
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.footprint_label = QLabel()
        self.footprint_label.setWordWrap(True)
        layout.addWidget(self.footprint_label)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([self.tx('Pin'), self.tx('Funzione'), self.tx('Tipo elettrico'), self.tx('Lato')])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(2, 180)
        self.table.setColumnWidth(3, 120)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.SelectedClicked)
        self.table.itemChanged.connect(self._changed)
        splitter.addWidget(self.table)
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        toolbar = QHBoxLayout()
        for text, callback in [('+', lambda: self.view.zoom(1.2)), ('−', lambda: self.view.zoom(1 / 1.2)), (self.tx('Adatta'), lambda: self.view.fit())]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        preview_layout.addLayout(toolbar)
        self.view = _SymbolView()
        preview_layout.addWidget(self.view)
        splitter.addWidget(preview)
        splitter.setSizes([550, 650])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.reset_button = QPushButton(self.tx('Ripristina'))
        self.reset_button.setToolTip(self.tx('Ripristina gli ultimi valori caricati.'))
        self.reset_button.clicked.connect(lambda: self._populate(self._original))
        actions.addWidget(self.reset_button)
        actions.addStretch()
        self.cancel_button = QPushButton(self.tx('Annulla'))
        self.cancel_button.clicked.connect(lambda: self._populate(self._baseline))
        actions.addWidget(self.cancel_button)
        self.apply_button = QPushButton(self.tx('Applica'))
        self.apply_button.clicked.connect(self.apply)
        actions.addWidget(self.apply_button)
        self.export_button = QPushButton(self.tx('Esporta simbolo KiCad…'))
        self.export_button.clicked.connect(self.export_requested.emit)
        actions.addWidget(self.export_button)
        layout.addLayout(actions)
        self.set_data('', [])

    def set_data(self, name, pinout, footprint='', pad_ids=None, force=False):
        key = deepcopy((name, pinout, footprint, list(pad_ids) if pad_ids is not None else None))
        if not force and key == self._source_key:
            return
        self._source_key = key
        self._error = ''
        self._name, self._footprint = str(name), str(footprint)
        self.footprint_label.setText(f"{self.tx('Footprint associato')}: {footprint}" if footprint else '')
        source = [deepcopy(row) for row in pinout if isinstance(row, dict)]
        source_ids = [str(row.get('pin', '')).strip() for row in source]
        if len(source_ids) != len(set(source_ids)):
            self._error = self.tx('Il pinout contiene numeri duplicati: verifica la corrispondenza.')
        if pad_ids is not None:
            ids = list(dict.fromkeys(str(value) for value in pad_ids if str(value).strip()))
            by_id = {str(row.get('pin', '')): row for row in source}
            extra = [row for row in source if str(row.get('pin', '')) not in ids]
            source = [by_id.get(value, {'pin': value, 'name': ''}) for value in ids] + extra
            if extra:
                self._error = self.tx('Il pinout contiene pin assenti dal footprint: verifica la corrispondenza.')
        source.sort(key=pin_sort_key)
        for index, row in enumerate(source):
            row.setdefault('electrical_type', 'passive')
            if row['electrical_type'] not in ELECTRICAL_TYPES:
                row['electrical_type'] = 'passive'
            row.setdefault('symbol_side', 'left' if index < (len(source) + 1) // 2 else 'right')
        self._baseline = deepcopy(source)
        self._original = deepcopy(source)
        self.title.setText(f"{self.tx('Simbolo elettrico')} · {self._name}" if name else self.tx('Simbolo elettrico'))
        self._populate(source)

    def _populate(self, rows):
        self._loading = True
        self._draft = deepcopy(rows)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            number = QTableWidgetItem(str(row.get('pin', '')))
            number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(index, 0, number)
            self.table.setItem(index, 1, QTableWidgetItem(str(row.get('name', ''))))
            types = QComboBox()
            for kind in ELECTRICAL_TYPES:
                types.addItem(self.tx(_TYPE_LABELS[kind]), kind)
            types.setToolTip(self.tx('Il tipo elettrico viene usato da KiCad per il controllo ERC.'))
            types.setCurrentIndex(max(0, types.findData(row.get('electrical_type', 'passive'))))
            types.currentIndexChanged.connect(self._changed)
            self.table.setCellWidget(index, 2, types)
            sides = QComboBox()
            sides.addItem(self.tx('Sinistra'), 'left')
            sides.addItem(self.tx('Destra'), 'right')
            sides.setCurrentIndex(max(0, sides.findData(row.get('symbol_side', 'left'))))
            sides.currentIndexChanged.connect(self._changed)
            self.table.setCellWidget(index, 3, sides)
        self._loading = False
        self._changed()

    def rows(self):
        """Return independent applied rows for persistence/export."""
        return deepcopy(self._baseline)

    def retranslate(self):
        """Refresh dynamic labels without losing unapplied edits."""
        self.title.setText(f"{self.tx('Simbolo elettrico')} · {self._name}" if self._name else self.tx('Simbolo elettrico'))
        self.footprint_label.setText(f"{self.tx('Footprint associato')}: {self._footprint}" if self._footprint else '')
        self.table.setHorizontalHeaderLabels([self.tx('Pin'), self.tx('Funzione'), self.tx('Tipo elettrico'), self.tx('Lato')])
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 2)
            for index in range(combo.count()):
                combo.setItemText(index, self.tx(_TYPE_LABELS[combo.itemData(index)]))
            side = self.table.cellWidget(row, 3)
            side.setItemText(0, self.tx('Sinistra'))
            side.setItemText(1, self.tx('Destra'))
        self._changed()

    def draft_rows(self):
        result = deepcopy(self._draft)
        for index, row in enumerate(result):
            row['name'] = self.table.item(index, 1).text().strip()
            row['electrical_type'] = self.table.cellWidget(index, 2).currentData()
            row['symbol_side'] = self.table.cellWidget(index, 3).currentData()
        return result

    def has_pending_edits(self):
        return self.draft_rows() != self._baseline

    def footprint(self):
        return self._footprint

    def set_error(self, message):
        self._error = str(message)
        self._changed()

    def set_status(self, message):
        """Show a nonblocking export result without changing draft validity."""
        self.status.setText(str(message))

    def apply(self):
        if not self.apply_button.isEnabled():
            return
        self._baseline = self.draft_rows()
        self._changed()
        self.edits_applied.emit(self.rows())

    def _changed(self, *_):
        if self._loading:
            return
        rows = self.draft_rows()
        dirty = rows != self._baseline
        valid = bool(rows) and not self._error
        try:
            pins = [SymbolPin(str(row.get('pin', '')), row['name'], row['electrical_type'], row['symbol_side']) for row in rows]
            symbol = layout_symbol(pins) if pins else None
        except ValueError as exc:
            valid = False
            symbol = None
            self.status.setText(str(exc))
        else:
            self.status.setText(self.tx('Nessun pin disponibile: carica o analizza un progetto.') if not rows else self.tx('Modifiche non applicate.') if dirty else self.tx('Simbolo pronto per la verifica e l’esportazione.'))
        if self._error:
            self.status.setText(self._error)
        self.apply_button.setEnabled(valid and dirty)
        self.cancel_button.setEnabled(dirty)
        self.reset_button.setEnabled(bool(rows) and rows != self._original)
        self.export_button.setEnabled(valid and not dirty)
        self._draw(symbol)

    def _draw(self, symbol):
        scene = self.view.scene()
        scene.clear()
        if symbol is None:
            return
        scale = 10.0
        pen = QPen(QColor('#89b4fa'), 1.3)
        text_color = QColor('#cdd6f4')
        font = QFont(self.font())
        font.setPixelSize(12)
        small_font = QFont(font)
        small_font.setPixelSize(10)
        width, height = symbol.width * scale, symbol.height * scale
        scene.addRect(-width / 2, -height / 2, width, height, pen, QColor('#242438'))
        def label(value, x, y, align='left', small=False):
            item = scene.addSimpleText(value, small_font if small else font)
            item.setBrush(text_color)
            bounds = item.boundingRect()
            item.setPos(x - (bounds.width() if align == 'right' else bounds.width() / 2 if align == 'center' else 0), y - bounds.height() / 2)
        label(self._name, 0, -height / 2 - 20, 'center')
        for placed in symbol.pins:
            pin = placed.pin
            x, y = placed.x * scale, -placed.y * scale
            left = pin.side == 'left'
            end = x + symbol.pin_length * scale * (1 if left else -1)
            scene.addLine(x, y, end, y, pen)
            scene.addEllipse(x - 1.8, y - 1.8, 3.6, 3.6, pen)
            label(pin.number, (x + end) / 2, y - 9, 'center', True)
            label(pin.name or '~', end + (5 if left else -5), y, 'left' if left else 'right')
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-24, -24, 24, 24))
        if self.view.auto_fit:
            self.view.fit()
