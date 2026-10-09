"""Editable electrical pin mapping and a preview of the exported symbol layout."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import math
from PyQt6.QtCore import Qt, pyqtSignal, QLineF, QSize, QRect, QItemSelectionModel
from PyQt6.QtGui import QAction, QColor, QFont, QPainter, QPen, QShortcut, QKeySequence, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QGraphicsScene, QGraphicsView,
    QHeaderView, QDialog, QDialogButtonBox, QPushButton, QDoubleSpinBox, QFormLayout, QLabel, QMenu, QToolButton, QSplitter, QTableWidget,
    QTableWidgetItem, QListWidget, QRubberBand, QToolBar, QSizePolicy, QVBoxLayout, QWidget,
)

from ..generators.symbol import ELECTRICAL_TYPES, PIN_GRID_MM, symbol_inputs, layout_symbol
from .pinout_dialog import pin_sort_key
from ..generators.symbol_edit import transform_selection

SCENE_UNITS_PER_MM = 10.0
GRID_MM = PIN_GRID_MM

_TYPE_LABELS = {
    'input': 'Ingresso', 'output': 'Uscita', 'bidirectional': 'Bidirezionale',
    'tri_state': 'Tre stati', 'passive': 'Passivo', 'free': 'Libero',
    'unspecified': 'Non specificato', 'power_in': 'Ingresso alimentazione',
    'power_out': 'Uscita alimentazione', 'open_collector': 'Collettore aperto',
    'open_emitter': 'Emettitore aperto', 'no_connect': 'Non connesso',
}


class _SymbolView(QGraphicsView):
    selection_toggled = pyqtSignal(str)
    region_selected = pyqtSignal(object, bool)
    pointer_changed = pyqtSignal(object)
    camera_changed = pyqtSignal()
    gesture_started = pyqtSignal(object, object)
    pin_context_requested = pyqtSignal(str, object)
    gesture_moved = pyqtSignal(object, object)
    gesture_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Navigation space is independent from the symbol bounds used by Fit.
        self.setSceneRect(-1000000, -1000000, 2000000, 2000000)
        self._pan_last = None
        self.selection_mode = False
        self._rubber_start = None
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self.setBackgroundBrush(QColor('#181825'))
        self.auto_fit = True
        self.show_grid = True
        self.gesture = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.gesture is None:
            if self.selection_mode or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._rubber_start = event.pos()
                self._rubber_add = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self._rubber.setGeometry(QRect(event.pos(), event.pos()))
                self._rubber.show()
                self.setFocus()
                self.viewport().grabMouse()
                event.accept()
                return
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                for item in self.items(event.pos()):
                    target = item.data(0)
                    if target and target[0] == 'pin':
                        self.selection_toggled.emit(target[1])
                        self.setFocus()
                        event.accept()
                        return
        background_drag = event.button() == Qt.MouseButton.LeftButton and not self.items(event.pos())
        if (event.button() == Qt.MouseButton.MiddleButton or background_drag) and self.gesture is None:
            self._pan_button = event.button()
            self._pan_last = event.pos()
            self.auto_fit = False
            self.setFocus()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            self.viewport().grabMouse()
            event.accept()
            return
        if self._pan_last is not None:
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            for item in self.items(event.pos()):
                target = item.data(0)
                if target is not None:
                    self.gesture = target
                    self.auto_fit = False
                    self.setFocus()
                    self.viewport().grabMouse()
                    self.gesture_started.emit(target, self.mapToScene(event.pos()))
                    event.accept()
                    return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        if self.gesture is not None:
            event.accept()
            return
        for item in self.items(event.pos()):
            target = item.data(0)
            if target is not None and target[0] == 'pin':
                self.pin_context_requested.emit(target[1], event.globalPos())
                event.accept()
                return
        event.ignore()

    def _pan_to(self, point):
        delta = point - self._pan_last
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        self._pan_last = point

    def mouseMoveEvent(self, event):
        self.pointer_changed.emit(self.mapToScene(event.pos()))
        if self._rubber_start is not None:
            self._rubber.setGeometry(QRect(self._rubber_start, event.pos()).normalized())
            event.accept()
            return
        if self._pan_last is not None:
            self._pan_to(event.pos())
            event.accept()
            return
        if self.gesture is not None:
            self.gesture_moved.emit(self.gesture, self.mapToScene(event.pos()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._rubber_start is not None and event.button() == Qt.MouseButton.LeftButton:
            rect = QRect(self._rubber_start, event.pos()).normalized()
            self._rubber_start = None
            self._rubber.hide()
            self.viewport().releaseMouse()
            self.region_selected.emit(self.mapToScene(rect).boundingRect(), self._rubber_add)
            event.accept()
            return
        if self._pan_last is not None and event.button() == self._pan_button:
            self._pan_to(event.pos())
            self._pan_last = None
            self.viewport().releaseMouse()
            self.viewport().unsetCursor()
            event.accept()
            return
        if self.gesture is not None and event.button() == Qt.MouseButton.LeftButton:
            # Windows can coalesce move events: always use the actual drop point.
            self.gesture_moved.emit(self.gesture, self.mapToScene(event.pos()))
            self.gesture = None
            self.viewport().releaseMouse()
            self.gesture_finished.emit(True)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and (self.gesture is not None or self._pan_last is not None or self._rubber_start is not None):
            self.cancel_interaction()
            event.accept()
            return
        super().keyPressEvent(event)

    def cancel_interaction(self):
        """Release capture and roll back unfinished edits before leaving the view."""
        editing = self.gesture is not None
        active = editing or self._pan_last is not None or self._rubber_start is not None
        self._rubber_start = None
        self._rubber.hide()
        self.gesture = None
        self._pan_last = None
        if active:
            self.viewport().releaseMouse()
            self.viewport().unsetCursor()
        if editing:
            self.gesture_finished.emit(False)

    def hideEvent(self, event):
        self.cancel_interaction()
        super().hideEvent(event)

    def set_grid_visible(self, visible):
        self.show_grid = bool(visible)
        self.viewport().update()

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if not self.show_grid:
            return
        step = GRID_MM * SCENE_UNITS_PER_MM
        pen = QPen(QColor('#36364b'))
        pen.setCosmetic(True)
        painter.save()
        painter.setPen(pen)
        for index in range(math.floor(rect.left() / step), math.ceil(rect.right() / step) + 1):
            x = index * step
            painter.drawLine(QLineF(x, rect.top(), x, rect.bottom()))
        for index in range(math.floor(rect.top() / step), math.ceil(rect.bottom() / step) + 1):
            y = index * step
            painter.drawLine(QLineF(rect.left(), y, rect.right(), y))
        painter.restore()

    def fit(self):
        self.auto_fit = True
        bounds = self.scene().sceneRect()
        if not bounds.isEmpty():
            self.fitInView(bounds, Qt.AspectRatioMode.KeepAspectRatio)
            self.camera_changed.emit()

    def zoom(self, factor, position=None):
        if 0.15 <= self.transform().m11() * factor <= 20:
            self.auto_fit = False
            position = position if position is not None else self.viewport().rect().center()
            before = self.mapToScene(position)
            self.scale(factor, factor)
            after = self.mapToScene(position)
            self.centerOn(self.mapToScene(self.viewport().rect().center()) + before - after)
            self.camera_changed.emit()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta and self.gesture is None and self._pan_last is None:
            self.zoom(1.2 ** max(-4, min(4, delta / 120)), event.position().toPoint())
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
    pin_types_ai_requested = pyqtSignal()
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
        self._history = []
        self._history_index = -1
        self._replaying = False
        self._gesture_before = None
        self._selected_pin = None
        self._selected_pins = set()
        self._syncing_selection = False
        self._cursor_position = None
        self._body_locked = False
        self._recovery_path = None
        self._recoverable = None
        self._applying = False
        self._text_bounds = []
        self._symbol = None
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
        self._splitter = splitter
        self._table_width = 400
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([self.tx('Pin'), self.tx('Funzione'), self.tx('Tipo elettrico')])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 180)
        self.table.setMaximumWidth(405)
        self.table.setMinimumWidth(260)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.SelectedClicked)
        self.table.itemChanged.connect(self._changed)
        self.table.itemSelectionChanged.connect(self._table_selection)
        splitter.addWidget(self.table)
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        toolbar = QToolBar()
        toolbar.setMovable(False)
        preview_layout.addWidget(toolbar)
        self._icon_buttons = []

        def icon_button(asset, text, callback=None):
            action = QAction(QIcon(str(Path(__file__).parent / 'assets' / asset)), self.tx(text), toolbar)
            action.setToolTip(self.tx(text))
            if callback is not None:
                action.triggered.connect(callback)
            toolbar.addAction(action)
            button = toolbar.widgetForAction(action)
            button.setProperty('i18n_skip', True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIcon(QIcon(str(Path(__file__).parent / 'assets' / asset)))
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(32, 32)
            button.setToolTip(self.tx(text))
            button.setAccessibleName(self.tx(text))
            self._icon_buttons.append((button, text))
            return button

        def separator():
            toolbar.addSeparator()

        self.view = _SymbolView()
        self.view.selection_toggled.connect(self._toggle_selection)
        self.view.region_selected.connect(self._select_region)
        self.view.pointer_changed.connect(self._update_readout)
        self.view.camera_changed.connect(self._update_readout)
        self.view.gesture_started.connect(self._begin_gesture)
        self.view.pin_context_requested.connect(self._pin_context_menu)
        self.view.gesture_moved.connect(self._move_gesture)
        self.view.gesture_finished.connect(self._end_gesture)
        self.view.setToolTip(self.tx('Sinistro sullo spazio vuoto: sposta la vista. Rotella: zoom sul cursore. Adatta: mostra tutto.'))
        self.table_toggle = icon_button('pinout-table.svg', 'Mostra/nascondi tabella pin')
        self.table_toggle.defaultAction().setCheckable(True)
        self.table_toggle.defaultAction().toggled.connect(self._toggle_pin_table)
        separator()
        icon_button('zoom-in.svg', 'Zoom avanti', lambda: self.view.zoom(1.2))
        icon_button('zoom-out.svg', 'Zoom indietro', lambda: self.view.zoom(1 / 1.2))
        icon_button('view-fit.svg', 'Adatta', self.view.fit)
        self.grid_check = icon_button('symbol-grid.svg', 'Griglia 0.1 in (2.54 mm)')
        self.grid_check.defaultAction().setCheckable(True)
        self.grid_check.defaultAction().setChecked(True)
        self.grid_check.defaultAction().toggled.connect(self.view.set_grid_visible)
        separator()
        self.undo_button = icon_button('edit-undo.svg', 'Annulla modifica', self.undo)
        self.redo_button = icon_button('edit-redo.svg', 'Ripeti modifica', self.redo)
        self.undo_button.setProperty('shortcut_hint', 'Ctrl+Z')
        self.redo_button.setProperty('shortcut_hint', 'Ctrl+Shift+Z / Ctrl+Y')
        self.layout_reset_button = icon_button('tab-symbol.svg', 'Ripristina disposizione', self.reset_layout)
        self.reset_button = icon_button('wf-regenerate.svg', 'Ripristina gli ultimi valori caricati.',
                                        lambda: self._populate(self._original))
        separator()
        self.cancel_button = icon_button('cancel.svg', 'Annulla', lambda: self._populate(self._baseline))
        self.apply_button = icon_button('confirm-use.svg', 'Applica', self.apply)
        self.export_button = icon_button('install-project.svg', 'Esporta simbolo KiCad…', lambda: self.export_requested.emit())
        separator()
        self.selection_button = icon_button('tool-select.svg', 'Selezione multipla (Maiusc + trascina)')
        self.selection_button.defaultAction().setCheckable(True)
        self.selection_button.defaultAction().toggled.connect(lambda value: setattr(self.view, 'selection_mode', value))
        self.align_button = icon_button('symbol-align.svg', 'Allinea e distribuisci')
        align_menu = QMenu(self.align_button)
        for text, operation in [('Allinea X al pin attivo', 'align_x'), ('Allinea Y al pin attivo', 'align_y'),
                                ('Distribuisci orizzontalmente', 'distribute_x'), ('Distribuisci verticalmente', 'distribute_y')]:
            action = align_menu.addAction(self.tx(text))
            action.setData(text)
            action.triggered.connect(lambda checked=False, op=operation: self._transform_selected(op))
        self.align_button.defaultAction().setMenu(align_menu)
        self.align_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.lock_button = icon_button('symbol-lock.svg', 'Blocca/sblocca rettangolo')
        self.lock_button.defaultAction().setCheckable(True)
        self.lock_button.defaultAction().toggled.connect(self._set_body_locked)
        self.review_button = icon_button('wf-review.svg', 'Controlla simbolo', self.show_review)
        self.pin_types_ai_button = icon_button('wf-relaunch-ai.svg', 'Suggerisci tipi pin con AI', self.pin_types_ai_requested.emit)
        self.recovery_button = icon_button('project-open.svg', 'Recupera bozza', self.recover_draft)
        self.recovery_button.defaultAction().setEnabled(False)
        self.properties_button = icon_button('sliders.svg', 'Posizione e orientamento del pin', self._edit_selected_pin)
        icon_button('info.svg', 'Trascina i pin sulla griglia. Tasto destro per ruotare. Maniglie per ridimensionare; Esc annulla.')
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.dimensions = QLabel()
        self.dimensions.setProperty('i18n_skip', True)
        toolbar.addWidget(self.dimensions)
        preview_layout.addWidget(self.view)
        for key, callback in [('Ctrl+Z', self.undo), ('Ctrl+Shift+Z', self.redo), ('Ctrl+Y', self.redo)]:
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)
        for key, dx, dy in [('Left', -1, 0), ('Right', 1, 0), ('Up', 0, 1), ('Down', 0, -1)]:
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda x=dx, y=dy: self._nudge_selected(x, y))
        for key, degrees in [('R', -90), ('Shift+R', 90)]:
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda d=degrees: self.rotate_pin(self._selected_pin, d))
        splitter.addWidget(preview)
        splitter.setSizes([400, 800])
        splitter.setStretchFactor(1, 1)
        self.table.hide()
        layout.addWidget(splitter, 1)
        self.readout = QLabel()
        self.readout.setProperty('i18n_skip', True)
        layout.addWidget(self.readout)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.set_data('', [])
        self.retranslate()

    def _toggle_pin_table(self, visible):
        self.view.cancel_interaction()
        if not visible:
            self._table_width = max(260, self.table.width())
        self.table.setVisible(visible)
        if visible:
            self._splitter.setSizes([self._table_width, max(260, self._splitter.width() - self._table_width)])

    def set_data(self, name, pinout, footprint='', pad_ids=None, force=False):
        key = deepcopy((name, pinout, footprint, list(pad_ids) if pad_ids is not None else None))
        if not force and key == self._source_key:
            return
        self.view.cancel_interaction()
        self._recovery_path = None
        self._recoverable = None
        self.recovery_button.defaultAction().setEnabled(False)
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
        self._history = []
        self._history_index = -1
        self._selected_pin = None
        self._selected_pins = set()
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
            types.currentIndexChanged.connect(lambda value, i=index: self._type_changed(i))
            self.table.setCellWidget(index, 2, types)
        self._loading = False
        self._changed()

    def _type_changed(self, index):
        if not self._loading:
            self._draft[index]['electrical_type_reviewed'] = True
            self._draft[index].pop('electrical_type_evidence', None)
            self._changed()

    def set_recovery_path(self, path):
        path = Path(path) if path else None
        if path == self._recovery_path:
            return
        self._recovery_path = path
        self._recoverable = None
        if path is not None:
            from ..services.symbol_draft import load_draft
            try:
                self._recoverable = load_draft(path, self._baseline)
            except OSError:
                self.set_status(self.tx('Impossibile leggere la bozza di recupero.'))
        self.recovery_button.defaultAction().setEnabled(self._recoverable is not None)
        if self._recoverable is not None:
            self.set_status(self.tx('Bozza disponibile: usa Recupera bozza per riprenderla.'))

    def recover_draft(self):
        if self._recoverable is not None:
            rows = deepcopy(self._recoverable)
            self._recoverable = None
            self.recovery_button.defaultAction().setEnabled(False)
            self._populate(rows)

    def _checkpoint_draft(self):
        if self._recovery_path is None or self._applying or self._gesture_before is not None:
            return
        from ..services.symbol_draft import save_draft
        try:
            if self.has_pending_edits():
                save_draft(self._recovery_path, self._baseline, self.draft_rows())
                self._recoverable = None
                self.recovery_button.defaultAction().setEnabled(False)
            elif self._recoverable is None:
                self._recovery_path.unlink(missing_ok=True)
        except OSError:
            self.set_status(self.tx('Bozza non salvata: verifica lo spazio e i permessi della cartella.'))

    def review_issues(self):
        issues = []
        rows = self.draft_rows()
        if not rows or self._symbol is None or self._error:
            issues.append(self.tx('Geometria o corrispondenza pin non valida.'))
        for row in rows:
            number = str(row.get('pin', ''))
            if not str(row.get('name') or '').strip():
                issues.append(f"{self.tx('Nome mancante')}: {number}")
            if row.get('electrical_type', 'passive') in ('passive', 'unspecified') and not row.get('electrical_type_reviewed'):
                issues.append(f"{self.tx('Tipo elettrico da verificare')}: {number}")
        overlaps = set()
        for index, (number, rect) in enumerate(self._text_bounds):
            for other, other_rect in self._text_bounds[index + 1:]:
                if number != other and rect.intersects(other_rect):
                    overlaps.add(tuple(sorted((number, other))))
        for first, second in sorted(overlaps):
            issues.append(f"{self.tx('Testi sovrapposti')}: {first}, {second}")
        return issues

    def show_review(self, exporting=False):
        issues = self.review_issues()
        if exporting and not issues:
            return True
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tx('Controlla simbolo'))
        dialog.resize(520, 380)
        layout = QVBoxLayout(dialog)
        entries = QListWidget()
        entries.addItems(issues or [self.tx('Nessun problema rilevato.')])
        layout.addWidget(entries)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel if exporting else QDialogButtonBox.StandardButton.Close)
        if exporting:
            proceed = buttons.addButton(self.tx('Esporta comunque'), QDialogButtonBox.ButtonRole.AcceptRole)
            proceed.clicked.connect(dialog.accept)
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tx('Annulla'))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.deleteLater()

    def confirm_export_review(self):
        return self.show_review(exporting=True)

    def rows(self):
        """Return independent applied rows for persistence/export."""
        return deepcopy(self._baseline)

    def use_pin_type_suggestions(self, proposals):
        """One undoable draft operation, preserving all pin and body geometry."""
        rows = self.draft_rows()
        by_number = {str(p['pin']): p for p in proposals}
        for row in rows:
            proposal = by_number.get(str(row['pin']))
            if proposal and proposal['electrical_type'] in ELECTRICAL_TYPES:
                row['electrical_type'] = proposal['electrical_type']
                row['electrical_type_reviewed'] = True
                row['electrical_type_evidence'] = {'source': proposal['source'], 'reason': proposal['reason']}
        self._populate(rows)

    def retranslate(self):
        """Refresh dynamic labels without losing unapplied edits."""
        self.title.setText(f"{self.tx('Simbolo elettrico')} · {self._name}" if self._name else self.tx('Simbolo elettrico'))
        self.footprint_label.setText(f"{self.tx('Footprint associato')}: {self._footprint}" if self._footprint else '')
        for button, source in self._icon_buttons:
            hint = button.property('shortcut_hint')
            tooltip = self.tx(source) + (f' ({hint})' if hint else '')
            button.defaultAction().setText(self.tx(source))
            button.defaultAction().setToolTip(tooltip)
            button.setToolTip(tooltip)
            button.setAccessibleName(self.tx(source))
        self.view.setToolTip(self.tx('Sinistro sullo spazio vuoto: sposta la vista. Rotella: zoom sul cursore. Adatta: mostra tutto.'))
        for action in self.align_button.defaultAction().menu().actions():
            action.setText(self.tx(action.data()))
        self.table.setHorizontalHeaderLabels([self.tx('Pin'), self.tx('Funzione'), self.tx('Tipo elettrico')])
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 2)
            for index in range(combo.count()):
                combo.setItemText(index, self.tx(_TYPE_LABELS[combo.itemData(index)]))
        self._changed()

    def draft_rows(self):
        result = deepcopy(self._draft)
        for index, row in enumerate(result):
            row['name'] = self.table.item(index, 1).text().strip()
            row['electrical_type'] = self.table.cellWidget(index, 2).currentData()
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
        self._applying = True
        self._apply_previous_baseline = deepcopy(self._baseline)
        self._baseline = self.draft_rows()
        self._changed()
        self.edits_applied.emit(self.rows())
        self._applying = False
        self._checkpoint_draft()

    def reject_apply(self, message):
        """Keep the editable draft and its history when persistence fails."""
        self._applying = False
        self._baseline = deepcopy(self._apply_previous_baseline)
        self._changed()
        self.set_status(message)

    def accept_apply(self):
        """Synchronize the saved source without throwing away undo history."""
        if self._source_key is not None:
            name, _, footprint, pad_ids = self._source_key
            self._source_key = deepcopy((name, self._baseline, footprint, pad_ids))

    def _changed(self, *_):
        if self._loading:
            return
        rows = self.draft_rows()
        dirty = rows != self._baseline
        valid = bool(rows) and not self._error
        try:
            pins, body = symbol_inputs(rows)
            symbol = layout_symbol(pins, body) if pins else None
        except ValueError as exc:
            valid = False
            symbol = None
            self.status.setText(str(exc))
        else:
            self.status.setText(self.tx('Nessun pin disponibile: carica o analizza un progetto.') if not rows else self.tx('Modifiche non applicate.') if dirty else self.tx('Simbolo pronto per la verifica e l’esportazione.'))
        if self._error:
            self.status.setText(self._error)
        self.apply_button.defaultAction().setEnabled(valid and dirty)
        self.cancel_button.defaultAction().setEnabled(dirty)
        self.reset_button.defaultAction().setEnabled(bool(rows) and rows != self._original)
        self.export_button.defaultAction().setEnabled(valid and not dirty)
        self._symbol = symbol
        if not self._replaying and self._gesture_before is None:
            self._record(rows)
        self.undo_button.defaultAction().setEnabled(self._history_index > 0)
        self.redo_button.defaultAction().setEnabled(self._history_index + 1 < len(self._history))
        self.layout_reset_button.defaultAction().setEnabled(bool(rows))
        self._draw(symbol)
        self._checkpoint_draft()

    def _record(self, rows):
        if self._history_index >= 0 and self._history[self._history_index] == rows:
            return
        del self._history[self._history_index + 1:]
        self._history.append(deepcopy(rows))
        self._history = self._history[-100:]
        self._history_index = len(self._history) - 1

    def _restore_history(self, index):
        if self._gesture_before is not None or not 0 <= index < len(self._history):
            return
        self._history_index = index
        self._replaying = True
        try:
            self._populate(self._history[index])
        finally:
            self._replaying = False

    def undo(self):
        self._restore_history(self._history_index - 1)

    def redo(self):
        self._restore_history(self._history_index + 1)

    def reset_layout(self):
        rows = self.draft_rows()
        for index, row in enumerate(rows):
            row.pop('symbol_body', None)
            row.pop('symbol_offset', None)
            row.pop('symbol_position', None)
            row.pop('symbol_angle', None)
            row['symbol_side'] = 'left' if index < (len(rows) + 1) // 2 else 'right'
        self._populate(rows)

    def _selection_ids(self):
        if self._selected_pin in self._selected_pins:
            return self._selected_pins.copy()
        return {self._selected_pin} if self._selected_pin is not None else set()

    def _select_pins(self, numbers, primary=None):
        if self._symbol is None:
            return
        valid = {p.pin.number for p in self._symbol.pins}
        self._selected_pins = set(numbers) & valid
        self._selected_pin = primary if primary in self._selected_pins else next(
            (p.pin.number for p in self._symbol.pins if p.pin.number in self._selected_pins), None)
        self._syncing_selection = True
        try:
            self.table.clearSelection()
            for index, row in enumerate(self._draft):
                if str(row['pin']) in self._selected_pins:
                    self.table.selectionModel().select(self.table.model().index(index, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        finally:
            self._syncing_selection = False
        self._draw(self._symbol)

    def _toggle_selection(self, number):
        chosen = self._selection_ids()
        chosen.symmetric_difference_update({number})
        self._select_pins(chosen, number)

    def _select_region(self, rect, additive):
        if self._symbol is None:
            return
        chosen = self._selection_ids() if additive else set()
        chosen.update(p.pin.number for p in self._symbol.pins if rect.contains(
            p.x * SCENE_UNITS_PER_MM, -p.y * SCENE_UNITS_PER_MM))
        self._select_pins(chosen)

    def _table_selection(self):
        if self._loading or self._gesture_before is not None or self._syncing_selection:
            return
        numbers = {self.table.item(item.row(), 0).text() for item in self.table.selectedItems()}
        current = self.table.currentRow()
        self._selected_pins = numbers
        candidate = self.table.item(current, 0).text() if current >= 0 else None
        self._selected_pin = candidate if candidate in numbers else next(iter(sorted(numbers)), None)
        self._draw(self._symbol)

    def _set_body_locked(self, locked):
        self.view.cancel_interaction()
        self._body_locked = bool(locked)
        self._draw(self._symbol)

    def _update_readout(self, point=None):
        if point is not None:
            self._cursor_position = point
        cursor = self._cursor_position
        value = '' if cursor is None else f'X {cursor.x()/10:.2f}  Y {-cursor.y()/10:.2f} mm  |  '
        value += f'{self.view.transform().m11()*100:.0f}%'
        chosen = self._selection_ids()
        if self._symbol is not None:
            pin = next((p for p in self._symbol.pins if p.pin.number == self._selected_pin), None)
            if pin:
                value += f'  |  Pin {pin.pin.number}: X {pin.x:.2f}  Y {pin.y:.2f} mm  {pin.angle}\u00b0'
            value += f'  |  {len(chosen)} pin'
        self.readout.setText(value)

    def _transform_selected(self, operation, values=()):
        if self._symbol is None:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        try:
            rows = transform_selection(self._free_rows(source, symbol), self._selection_ids(),
                                       self._selected_pin, operation, values)
        except ValueError:
            self.set_status(self.tx('Operazione annullata: pin sovrapposti o selezione insufficiente.'))
            return False
        if rows == self._free_rows(source, symbol):
            self._draft = deepcopy(source)
            self._changed()
            return True
        return self._accept_geometry(rows)

    def _begin_gesture(self, target, point):
        if self._symbol is None:
            return
        self._gesture_before = self.draft_rows()
        self._gesture_layout = self._symbol
        self._gesture_start = point
        self._gesture_scene_rect = self.view.scene().sceneRect()
        if target[0] == 'pin':
            chosen = self._selection_ids()
            if target[1] not in chosen:
                chosen = {target[1]}
            self._select_pins(chosen, target[1])

    def _editable_rows(self, rows, symbol):
        rows = deepcopy(rows)
        positions = {p.pin.number: p for p in symbol.pins}
        for row in rows:
            p = positions[str(row['pin'])]
            row['symbol_offset'] = p.y if p.pin.side in ('left', 'right') else p.x
            row['symbol_body'] = [symbol.width, symbol.height]
        return rows

    def _accept_geometry(self, rows):
        try:
            pins, body = symbol_inputs(rows)
            layout_symbol(pins, body)
        except ValueError:
            self.status.setText(self.tx('Posizione non valida: verifica griglia e punti di connessione.'))
            return False
        # Geometry changes do not rebuild the table or interrupt its editor.
        self._draft = deepcopy(rows)
        self._changed()
        return True

    def move_pin(self, number, side, offset, *, swap=False):
        if self._symbol is None:
            return False
        # Rebuild each drag preview from its starting state, so crossing occupied
        # rows does not shuffle every pin encountered along the mouse trajectory.
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        layout = self._gesture_layout if self._gesture_before is not None else self._symbol
        rows = self._editable_rows(source, layout)
        row = next((r for r in rows if str(r['pin']) == str(number)), None)
        if row is None:
            return False
        snapped = round(round(offset / GRID_MM) * GRID_MM, 4)
        if side == row['symbol_side'] and math.isclose(snapped, row['symbol_offset'], abs_tol=1e-7):
            self._draft = deepcopy(source)
            self._changed()
            return True
        occupied = next((r for r in rows if r is not row and
                         r['symbol_side'] == side and
                         math.isclose(r['symbol_offset'], snapped, abs_tol=1e-7)), None)
        if occupied is not None and swap:
            occupied['symbol_side'] = row['symbol_side']
            occupied['symbol_offset'] = row['symbol_offset']
        row['symbol_side'] = side
        row['symbol_offset'] = snapped
        return self._accept_geometry(rows)

    def _free_rows(self, rows, symbol):
        rows = deepcopy(rows)
        for row, placed in zip(rows, symbol.pins):
            row['symbol_position'] = [placed.x, placed.y]
            row['symbol_angle'] = placed.angle
            row['symbol_body'] = [symbol.width, symbol.height]
        return rows

    def place_pin(self, number, x, y):
        if self._symbol is None:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        rows = self._free_rows(source, symbol)
        row = next((r for r in rows if str(r['pin']) == str(number)), None)
        if row is None:
            return False
        position = [round(round(v / GRID_MM) * GRID_MM, 4) for v in (x, y)]
        if position == row['symbol_position']:
            self._draft = deepcopy(source)
            self._changed()
            return True
        occupied = next((r for r in rows if r is not row and r['symbol_position'] == position), None)
        if occupied is not None:
            occupied['symbol_position'] = row['symbol_position'][:]
        row['symbol_position'] = position
        return self._accept_geometry(rows)

    def rotate_pin(self, number, degrees):
        if self._symbol is None or self._gesture_before is not None:
            return False
        if number in self._selection_ids() and len(self._selection_ids()) > 1:
            return self._transform_selected('rotate', (degrees,))
        rows = self._free_rows(self.draft_rows(), self._symbol)
        row = next((r for r in rows if str(r['pin']) == str(number)), None)
        if row is None:
            return False
        row['symbol_angle'] = (row['symbol_angle'] + degrees) % 360
        return self._accept_geometry(rows)

    def _nudge_selected(self, dx, dy):
        if self._symbol is None or self._gesture_before is not None:
            return
        pin = next((p for p in self._symbol.pins if p.pin.number == self._selected_pin), None)
        if pin is not None and len(self._selection_ids()) > 1:
            self._transform_selected('move', (dx * GRID_MM, dy * GRID_MM))
        elif pin is not None:
            self.place_pin(pin.pin.number, pin.x + dx * GRID_MM, pin.y + dy * GRID_MM)

    def set_pin_geometry(self, number, x, y, angle):
        if self._symbol is None or self._gesture_before is not None:
            return False
        rows = self._free_rows(self.draft_rows(), self._symbol)
        row = next((r for r in rows if str(r['pin']) == str(number)), None)
        if row is None:
            return False
        position = [round(round(v / GRID_MM) * GRID_MM, 4) for v in (x, y)]
        if position == row['symbol_position'] and angle == row['symbol_angle']:
            return True
        row['symbol_position'] = position
        row['symbol_angle'] = angle
        return self._accept_geometry(rows)

    def _edit_selected_pin(self):
        if self._symbol is None:
            return
        pin = next((p for p in self._symbol.pins if p.pin.number == self._selected_pin), None)
        if pin is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{self.tx('Pin')} {pin.pin.number} ({pin.pin.name})")
        form = QFormLayout(dialog)
        coordinates = []
        for caption, value in [('X (mm)', pin.x), ('Y (mm)', pin.y)]:
            field = QDoubleSpinBox()
            field.setDecimals(2)
            field.setRange(-998.22, 998.22)
            field.setSingleStep(GRID_MM)
            field.setValue(value)
            form.addRow(caption, field)
            coordinates.append(field)
        orientation = QComboBox()
        for angle in (0, 90, 180, 270):
            orientation.addItem(str(angle) + '\u00b0', angle)
        orientation.setCurrentIndex(orientation.findData(pin.angle))
        form.addRow(self.tx('Orientamento'), orientation)
        message = QLabel(self.tx('Le coordinate si agganciano alla griglia da 2.54 mm.'))
        message.setWordWrap(True)
        form.addRow(message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tx('Annulla'))
        def accept():
            if self.set_pin_geometry(pin.pin.number, coordinates[0].value(), coordinates[1].value(), orientation.currentData()):
                dialog.accept()
            else:
                message.setText(self.tx('Posizione non valida: verifica griglia e punti di connessione.'))
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _pin_context_menu(self, number, global_pos):
        chosen = self._selection_ids()
        self._select_pins(chosen if number in chosen else {number}, number)
        menu = QMenu(self)
        title = menu.addAction(f"{self.tx('Pin')} {number}")
        title.setEnabled(False)
        menu.addSeparator()
        menu.setToolTipsVisible(True)
        for asset, value, label, degrees in [
            ('rotate-ccw.svg', '+90\u00b0', 'Ruota di 90 gradi (antiorario)', 90),
            ('rotate-cw.svg', '-90\u00b0', 'Ruota di 90 gradi (orario)', -90),
            ('rotate-180.svg', '180\u00b0', 'Ruota di 180 gradi', 180),
            ('rotate-minus-180.svg', '-180\u00b0', 'Ruota di -180 gradi', -180),
        ]:
            action = menu.addAction(QIcon(str(Path(__file__).parent / 'assets' / asset)), value)
            action.setIconVisibleInMenu(True)
            action.setToolTip(self.tx(label))
            action.triggered.connect(lambda checked=False, d=degrees: self.rotate_pin(number, d))
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

    def resize_body(self, width, height):
        if self._symbol is None or self._body_locked:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        body = [round(max(4 * GRID_MM, round(v / (2 * GRID_MM)) * 2 * GRID_MM), 4)
                for v in (width, height)]
        if all(math.isclose(value, original, abs_tol=1e-7)
               for value, original in zip(body, (symbol.width, symbol.height))):
            self._draft = deepcopy(source)
            self._changed()
            return True
        rows = self._free_rows(source, symbol)
        for row in rows:
            row['symbol_body'] = body[:]
        return self._accept_geometry(rows)

    def _move_gesture(self, target, point):
        if self._gesture_before is None:
            return
        symbol = self._gesture_layout
        if target[0] == 'pin':
            placed = next(p for p in symbol.pins if p.pin.number == target[1])
            delta = point - self._gesture_start
            if len(self._selection_ids()) > 1:
                self._transform_selected('move', (delta.x()/SCENE_UNITS_PER_MM, -delta.y()/SCENE_UNITS_PER_MM))
            else:
                self.place_pin(target[1], placed.x + delta.x() / SCENE_UNITS_PER_MM,
                               placed.y - delta.y() / SCENE_UNITS_PER_MM)
        else:
            horizontal, vertical = target[1], target[2]
            x, y = point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM
            self.resize_body(2 * abs(x) if horizontal else symbol.width,
                             2 * abs(y) if vertical else symbol.height)

    def _end_gesture(self, commit):
        before = self._gesture_before
        self._gesture_before = None
        if before is None:
            return
        if not commit:
            self._populate(before)
        else:
            self._changed()

    def _draw(self, symbol):
        self.properties_button.defaultAction().setEnabled(symbol is not None and any(
            p.pin.number == self._selected_pin for p in symbol.pins))
        self.align_button.defaultAction().setEnabled(len(self._selection_ids()) >= 2)
        self._update_readout()
        scene = self.view.scene()
        scene.clear()
        self._text_bounds = []
        self.dimensions.setText('' if symbol is None else f'{symbol.width:.2f} \u00d7 {symbol.height:.2f} mm')
        if symbol is None:
            return
        scale = SCENE_UNITS_PER_MM
        pen = QPen(QColor('#89b4fa'), 1.3)
        text_color = QColor('#cdd6f4')
        font = QFont(self.font())
        font.setPixelSize(12)
        small_font = QFont(font)
        small_font.setPixelSize(10)
        width, height = symbol.width * scale, symbol.height * scale
        scene.addRect(-width / 2, -height / 2, width, height, pen, QColor('#242438'))

        def tag(item, target):
            item.setData(0, target)
            item.setCursor(Qt.CursorShape.SizeAllCursor)
            if target[0] == 'pin':
                item.setToolTip(target[1])
            return item

        def label(value, x, y, align='left', small=False, target=None, rotate=False):
            item = scene.addSimpleText(value, small_font if small else font)
            item.setBrush(text_color)
            bounds = item.boundingRect()
            dx = bounds.width() if align == 'right' else bounds.width() / 2 if align == 'center' else 0
            if rotate:
                item.setRotation(-90)
                item.setPos(x - bounds.height() / 2, y + dx)
            else:
                item.setPos(x - dx, y - bounds.height() / 2)
            if target:
                tag(item, target)
                self._text_bounds.append((target[1], item.sceneBoundingRect()))
            return item

        label(self._name, 0, -height / 2 - symbol.pin_length * scale - 28, 'center')
        for placed in symbol.pins:
            pin = placed.pin
            x, y = placed.x * scale, -placed.y * scale
            vx, vy = {0: (1, 0), 180: (-1, 0), 270: (0, 1), 90: (0, -1)}[placed.angle]
            end_x, end_y = x + vx * symbol.pin_length * scale, y + vy * symbol.pin_length * scale
            target = ('pin', pin.number)
            selected = pin.number in self._selection_ids()
            pin_pen = QPen(QColor('#f9e2af') if selected else QColor('#89b4fa'), 2.8 if selected else 1.3)
            tag(scene.addLine(x, y, end_x, end_y, pin_pen), target)
            tag(scene.addEllipse(x - 3, y - 3, 6, 6, pin_pen), target)
            # Broad transparent target makes thin pin lines easy to grab.
            hit = scene.addLine(x, y, end_x, end_y, QPen(QColor(0, 0, 0, 0), 12))
            tag(hit, target)
            if vx:
                label(pin.number, (x + end_x) / 2, y - 9, 'center', True, target)
                label(pin.name or '~', end_x + vx * 5, y, 'left' if vx > 0 else 'right', target=target)
            else:
                label(pin.number, x - 9, (y + end_y) / 2, 'center', True, target, True)
                label(pin.name or '~', x, end_y + vy * 5, 'right' if vy > 0 else 'left', target=target, rotate=True)
        handles = [] if self._body_locked else [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
        for hx, hy in handles:
            x, y = hx * width / 2, hy * height / 2
            item = tag(scene.addRect(x - 4, y - 4, 8, 8, pen, QColor('#a6e3a1')), ('resize', hx, hy))
            item.setZValue(10)
            item.setCursor(Qt.CursorShape.SizeHorCursor if not hy else Qt.CursorShape.SizeVerCursor if not hx else
                           Qt.CursorShape.SizeFDiagCursor if hx == hy else Qt.CursorShape.SizeBDiagCursor)
        if self._gesture_before is not None:
            scene.setSceneRect(self._gesture_scene_rect)
        else:
            scene.setSceneRect(scene.itemsBoundingRect().adjusted(-35, -35, 35, 35))
        if self.view.auto_fit:
            self.view.fit()
