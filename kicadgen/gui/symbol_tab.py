"""@brief Editable electrical pin mapping and a preview of the exported symbol layout."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import math
from PyQt6.QtCore import Qt, pyqtSignal, QLineF, QSize, QRect, QRectF, QPointF, QItemSelectionModel
from PyQt6.QtGui import QPalette, QAction, QColor, QFont, QPainter, QPen, QShortcut, QKeySequence, QIcon
from PyQt6.QtWidgets import (QHBoxLayout, QGridLayout, QFrame, QScrollArea, 
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


def glyph_icon(glyph, size: int = 36) -> QIcon:
    """@brief Icon of a palette element, drawn from its own primitives (no asset file).

    @param glyph Catalogue glyph.
    @param size Icon side in pixels.
    @return A transparent icon with the glyph's polylines, arcs, circles and
        rectangles scaled to fit, light strokes, and its terminals as dots:
        the stylised picture of what will be inserted (user, 2026-10-10:
        «una palette grafica stilizzata»).
    """
    from PyQt6.QtGui import QPixmap, QPainterPath
    from ..generators.symbol_glyphs import arc_points, instance_from_glyph, placed_primitives, placed_terminals
    instance = instance_from_glyph(glyph, 0, 0)
    primitives = placed_primitives(instance)
    terminals = placed_terminals(instance)
    xs, ys = [], []
    for p in primitives:
        for x, y in p.points:
            xs.append(x)
            ys.append(y)
        if p.kind == 'circle':
            (cx, cy), r = p.points[0], p.radius
            xs.extend((cx - r, cx + r))
            ys.extend((cy - r, cy + r))
    for t in terminals:
        xs.append(t.x)
        ys.append(t.y)
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    if not xs:
        return QIcon(pixmap)
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    margin = 4
    scale = (size - 2 * margin) / span
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def to_px(x, y):
        return (size / 2 + (x - cx) * scale, size / 2 - (y - cy) * scale)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor('#cdd6f4'), 1.4)
    painter.setPen(pen)
    for p in primitives:
        fill = QColor('#cdd6f4') if p.fill == 'outline' else QColor(0, 0, 0, 0)
        painter.setBrush(fill)
        if p.kind in ('polyline', 'arc'):
            points = p.points if p.kind == 'polyline' else arc_points(*p.points)
            path = QPainterPath()
            path.moveTo(*to_px(*points[0]))
            for x, y in points[1:]:
                path.lineTo(*to_px(x, y))
            painter.drawPath(path)
        elif p.kind == 'circle':
            (x, y), r = p.points[0], p.radius * scale
            px, py = to_px(x, y)
            painter.drawEllipse(QPointF(px, py), r, r)
        elif p.kind == 'rectangle':
            (x0, y0), (x1, y1) = p.points
            left, top = to_px(min(x0, x1), max(y0, y1))
            painter.drawRect(QRectF(left, top, abs(x1 - x0) * scale, abs(y1 - y0) * scale))
    painter.setPen(QPen(QColor('#a6e3a1'), 1.0))
    painter.setBrush(QColor('#a6e3a1'))
    for t in terminals:
        px, py = to_px(t.x, t.y)
        painter.drawEllipse(QPointF(px, py), 1.6, 1.6)
    painter.end()
    return QIcon(pixmap)


#: Colours of the symbol canvas. "dark": the application's own theme.
#: "kicad": the default colour scheme of KiCad's schematic editor, so the
#: preview shows what the schematic will (user, 2026-10-10: body yellow
#: with dark drawings, as in KiCad).
CANVAS_THEMES = {
    'dark': {
        'canvas': '#181825', 'grid': '#36364b', 'body_fill': '#242438', 'outline': '#89b4fa',
        'graphic': '#cdd6f4', 'graphic_fill': '#242438', 'pin': '#89b4fa', 'pin_name': '#cdd6f4',
        'pin_number': '#cdd6f4', 'selected': '#f9e2af', 'terminal': '#a6e3a1', 'handle': '#a6e3a1',
        'wire': '#f9e2af',
    },
    'kicad': {
        'canvas': '#ffffff', 'grid': '#d9d9d9', 'body_fill': '#ffffc2', 'outline': '#840000',
        'graphic': '#840000', 'graphic_fill': '#ffffc2', 'pin': '#840000', 'pin_name': '#008484',
        'pin_number': '#840000', 'selected': '#0a7bd6', 'terminal': '#2e8b57', 'handle': '#2e8b57',
        'wire': '#0a7bd6',
    },
}


class PalettePanel(QWidget):
    """@brief Vertical palette with collapsible groups of element icons (user, 2026-10-10).

    @note One checkable action per element; choosing one emits `chosen(key)`
        and leaves it checked until `release()`. Groups open and close
        independently; the state lives for the session only.
    """
    chosen = pyqtSignal(str)
    #: A tool of the palette was clicked ("wire").
    tool_chosen = pyqtSignal(str)

    def __init__(self, tx, parent=None):
        """@brief Build the panel from the catalogue.

        @param tx Translation callable for labels.
        @param parent Parent widget.
        """
        super().__init__(parent)
        self.tx = tx
        self._actions = []
        self._tool_actions = {}
        self._groups = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        # The content never asks for more width than the panel: long group
        # titles elide instead of widening the hidden-scroll area, which
        # pushed the icon columns out of view (offscreen check, 2026-10-10).
        self._content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(2)
        self.scroll.setWidget(self._content)
        outer.addWidget(self.scroll)
        self.setFixedWidth(150)
        # Same dark ground as the canvas, whatever the application style: the
        # element strokes are light and would vanish on a light panel.
        for widget in (self.scroll.viewport(), self._content):
            widget.setAutoFillBackground(True)
            palette = widget.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor('#181825'))
            palette.setColor(QPalette.ColorRole.WindowText, QColor('#cdd6f4'))
            palette.setColor(QPalette.ColorRole.ButtonText, QColor('#cdd6f4'))
            widget.setPalette(palette)
        self.rebuild()

    def rebuild(self):
        """@brief Fill (again) the groups and icons from the catalogue, keeping open/closed states."""
        from ..generators.symbol_glyphs import grouped_catalogue
        open_state = {key: section.isChecked() for key, (section, _body) in self._groups.items()}
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._actions = []
        self._tool_actions = {}
        self._groups = {}
        # Tools first (user, 2026-10-10: «il filo lo vorrei nella palette»).
        from ..generators.symbol_glyphs import Glyph, Primitive
        wire_glyph = Glyph('wire', 'Filo', (Primitive('polyline', ((0, 0), (2.54, 0), (2.54, 2.54), (5.08, 2.54)),
                                                      0.0, 'none', 0.254),), (), 'tool')
        self._add_section('tools', 'Collegamenti', [wire_glyph], open_state.get('tools', True), tool=True)
        for key, label, glyphs in grouped_catalogue():
            self._add_section(key, label, glyphs, open_state.get(key, True))
        self._content_layout.addStretch(1)

    def _add_section(self, key, label, glyphs, is_open, tool=False):
        """One collapsible group: a header and a grid of icon buttons (elements or tools)."""
        if True:
            header = QToolButton()
            header.setText(self.tx(label))
            header.setCheckable(True)
            header.setChecked(is_open)
            header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            header.setArrowType(Qt.ArrowType.DownArrow if header.isChecked() else Qt.ArrowType.RightArrow)
            header.setAutoRaise(True)
            header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            header.setMinimumWidth(0)
            header.setProperty('i18n_skip', True)
            body = QWidget()
            body.setAutoFillBackground(True)
            body.setPalette(self._content.palette())
            grid = QGridLayout(body)
            grid.setContentsMargins(4, 0, 0, 4)
            grid.setSpacing(2)
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for index, glyph in enumerate(glyphs):
                action = QAction(glyph_icon(glyph), self.tx(glyph.label), self)
                action.setCheckable(True)
                action.setData(glyph.key)
                action.setToolTip(self.tx(glyph.label) + ' — ' + self.tx('clicca, poi clicca nel simbolo'))
                if tool:
                    action.setToolTip(self.tx('Filo: clicca i punti nel simbolo; doppio clic o tasto destro per chiudere'))
                    action.triggered.connect(lambda checked=False, k=glyph.key: self.tool_chosen.emit(k))
                else:
                    action.triggered.connect(lambda checked=False, k=glyph.key: self._pick(k))
                button = QToolButton()
                button.setDefaultAction(action)
                button.setIconSize(QSize(36, 36))
                button.setFixedSize(42, 42)
                button.setAutoRaise(True)
                button.setProperty('i18n_skip', True)
                button.setAccessibleName(self.tx(glyph.label))
                grid.addWidget(button, index // 3, index % 3, Qt.AlignmentFlag.AlignLeft)
                if tool:
                    self._tool_actions[glyph.key] = action
                else:
                    self._actions.append(action)
            body.setVisible(header.isChecked())
            header.toggled.connect(lambda checked, b=body, h=header: (
                b.setVisible(checked),
                h.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)))
            self._content_layout.addWidget(header)
            self._content_layout.addWidget(body)
            self._groups[key] = (header, body)

    def set_tool_checked(self, key, checked):
        """@brief Show a palette tool as active or not; unknown keys are ignored."""
        action = self._tool_actions.get(key)
        if action is not None:
            action.setChecked(bool(checked))

    def _pick(self, key):
        for action in self._actions:
            action.setChecked(action.data() == key)
        self.chosen.emit(key)

    def actions(self):
        """@brief Every element action, in palette order (checkable, data = catalogue key)."""
        return list(self._actions)

    def release(self):
        """@brief Uncheck every element (placement done or cancelled)."""
        for action in self._actions:
            action.setChecked(False)

    def set_group_open(self, key, open_):
        """@brief Open or close one group; unknown keys are ignored."""
        section = self._groups.get(key)
        if section is not None:
            section[0].setChecked(bool(open_))

    def group_is_open(self, key):
        """@brief Whether a group is open; False for an unknown key."""
        section = self._groups.get(key)
        return bool(section is not None and section[0].isChecked())


class _SymbolView(QGraphicsView):
    selection_toggled = pyqtSignal(str)
    region_selected = pyqtSignal(object, bool)
    pointer_changed = pyqtSignal(object)
    camera_changed = pyqtSignal()
    gesture_started = pyqtSignal(object, object)
    pin_context_requested = pyqtSignal(str, object)
    gesture_moved = pyqtSignal(object, object)
    gesture_finished = pyqtSignal(bool)
    #: A click while placing a glyph (scene point), or None when placing is cancelled.
    place_requested = pyqtSignal(object)
    graphic_context_requested = pyqtSignal(int, object)
    #: A click while drawing a wire: (scene point, Shift held). Shift frees
    #: the segment from the orthogonal constraint, as in KiCad.
    wire_point = pyqtSignal(object, bool)
    #: The wire is finished (True) or abandoned (False).
    wire_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        #: True while the next left click places the chosen glyph.
        self.placing = False
        #: True while left clicks add wire points.
        self.wiring = False
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
        self.theme = CANVAS_THEMES['dark']
        self.setBackgroundBrush(QColor(self.theme['canvas']))
        self.auto_fit = True
        self.show_grid = True
        self.gesture = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mouseDoubleClickEvent(self, event):
        if self.wiring and event.button() == Qt.MouseButton.LeftButton:
            self.wiring = False
            self.wire_finished.emit(True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if self.wiring and event.button() == Qt.MouseButton.LeftButton:
            self.wire_point.emit(self.mapToScene(event.pos()),
                                 bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            event.accept()
            return
        if self.wiring and event.button() == Qt.MouseButton.RightButton:
            self.wiring = False
            self.wire_finished.emit(True)
            event.accept()
            return
        if self.placing and event.button() == Qt.MouseButton.LeftButton:
            self.place_requested.emit(self.mapToScene(event.pos()))
            event.accept()
            return
        if self.placing and event.button() == Qt.MouseButton.RightButton:
            self.placing = False
            self.place_requested.emit(None)
            event.accept()
            return
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
            if target is not None and target[0] == 'graphic':
                self.graphic_context_requested.emit(target[1], event.globalPos())
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
        if self.wiring and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.wiring = False
            self.wire_finished.emit(True)
            event.accept()
            return
        if self.wiring and event.key() == Qt.Key.Key_Escape:
            self.wiring = False
            self.wire_finished.emit(False)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.placing:
            self.placing = False
            self.place_requested.emit(None)
            event.accept()
            return
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
        pen = QPen(QColor(self.theme['grid']))
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
    """@brief Draft edits are local until Apply; exports always use applied rows.

    ``set_data`` accepts existing pinout dictionaries (``pin``, ``name``),
    preserving their metadata. Pin IDs cannot be edited. ``pad_ids`` optionally
    supplies the actual footprint IDs, including pads without a pinout name.
    The owner persists edits_applied and performs export_requested.
    """
    edits_applied = pyqtSignal(list)
    pin_types_ai_requested = pyqtSignal()
    export_requested = pyqtSignal()
    export_enabled_changed = pyqtSignal(bool)

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
        #: Index of the selected placed glyph in the rows' `symbol_graphics`, or None.
        self._selected_graphic = None
        #: Canvas theme key: 'dark' (application) or 'kicad' (schematic colours).
        self._theme_key = 'dark'
        #: Points of the wire being drawn, [] when none: `[x, y]` (symbol mm)
        #: for a free grid point, `{"x", "y", "anchor"}` for a point attached
        #: to a pin's inner end or an element terminal (symbol_glyphs anchors).
        self._wire_points = []
        self._wire_cursor_item = None
        #: True: each new wire segment is horizontal or vertical (Shift frees it).
        self._wire_orthogonal = True
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
        self.table.itemChanged.connect(self._name_changed)
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
        self.view.place_requested.connect(self._place_requested)
        self.view.graphic_context_requested.connect(self._graphic_context_menu)
        self.view.wire_point.connect(self._wire_point)
        self.view.wire_finished.connect(self.finish_wire)
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
        # The export command is hosted by the dedicated KiCad tab. This button
        # is NOT in the toolbar (no duplicate command, nothing in the overflow
        # menu) and stays hidden; its action carries the editor's validity
        # state for the KiCad tab. It is not an invisible toolbar action
        # because Qt reports an invisible action as disabled, and the validity
        # read False whatever the rows (review of 2026-10-10: six symbol tests).
        export_action = QAction(QIcon(str(Path(__file__).parent / 'assets' / 'install-project.svg')),
                                self.tx('Esporta simbolo KiCad…'), self)
        export_action.triggered.connect(lambda: self.export_requested.emit())
        self.export_button = QToolButton(self)
        self.export_button.setDefaultAction(export_action)
        self.export_button.hide()
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
        self.theme_button = icon_button('tab-kicad.svg', 'Colori come KiCad (anteprima con il tema dello schema)')
        self.theme_button.defaultAction().setCheckable(True)
        self.theme_button.defaultAction().toggled.connect(self.set_kicad_colors)
        self.lock_button = icon_button('symbol-lock.svg', 'Blocca/sblocca rettangolo')
        self.lock_button.defaultAction().setCheckable(True)
        self.lock_button.defaultAction().toggled.connect(self._set_body_locked)
        self.review_button = icon_button('wf-review.svg', 'Controlla simbolo', self.show_review)
        self.pin_types_ai_button = icon_button('wf-relaunch-ai.svg', 'Suggerisci tipi pin con AI', self.pin_types_ai_requested.emit)
        self.recovery_button = icon_button('project-open.svg', 'Recupera bozza', self.recover_draft)
        self.recovery_button.defaultAction().setEnabled(False)
        self.properties_button = icon_button('sliders.svg', 'Posizione e orientamento del pin', self._edit_selected_pin)
        separator()
        # Palette of graphic elements (user, 2026-10-10): KiCad's own drawings
        # when KiCad is installed, standard shapes otherwise. One click on
        # «Inserisci», one click in the symbol.
        # The chosen element lives in a hidden combo (one source of truth for
        # tests and for placement); the user sees a strip of icons, each the
        # stylised drawing of its element, below the main toolbar.
        self.glyph_combo = QComboBox()
        self.glyph_combo.setToolTip(self.tx('Elemento grafico da inserire nel simbolo'))
        self.glyph_combo.setProperty('i18n_skip', True)
        self.glyph_combo.hide()
        self._fill_glyph_combo()
        self.glyph_insert_button = icon_button('tool-draw.svg', 'Inserisci elemento grafico', self._start_placing)
        # Wires (user, 2026-10-10): polylines from a pin's inner end to an
        # element terminal, or between any grid points.
        self.wire_button = icon_button('wf-contact.svg', 'Filo: clic sui punti, doppio clic o tasto destro per chiudere, Esc per annullare', self.start_wire)
        self.wire_button.defaultAction().setCheckable(True)
        # Orthogonal wires like KiCad (user, 2026-10-10: «deve essere un CAD
        # un po' come KiCad»): each segment horizontal or vertical, Shift frees it.
        self.orthogonal_button = icon_button('symbol-align.svg', 'Fili ortogonali (Maiusc libera il segmento)')
        self.orthogonal_button.defaultAction().setCheckable(True)
        self.orthogonal_button.defaultAction().setChecked(True)
        self.orthogonal_button.defaultAction().toggled.connect(self.set_wire_orthogonal)
        icon_button('info.svg', 'Trascina i pin sulla griglia. Tasto destro per ruotare. Maniglie per ridimensionare; Esc annulla.')
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.dimensions = QLabel()
        self.dimensions.setProperty('i18n_skip', True)
        toolbar.addWidget(self.dimensions)
        # Vertical palette on the left of the canvas, groups that open and
        # close independently (user, 2026-10-10).
        self.palette = PalettePanel(self.tx)
        self.palette.setToolTip(self.tx('Palette: clicca un elemento, poi clicca nel simbolo per inserirlo.'))
        self.palette.chosen.connect(self._palette_chosen)
        self.palette.tool_chosen.connect(lambda key: self.start_wire() if key == 'wire' else None)
        canvas_row = QHBoxLayout()
        canvas_row.setContentsMargins(0, 0, 0, 0)
        canvas_row.addWidget(self.view, 1)
        canvas_row.addWidget(self.palette)       # on the right (user, 2026-10-10)
        preview_layout.addLayout(canvas_row, 1)
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
            shortcut.activated.connect(lambda d=degrees: (
                self._edit_graphic(self._selected_graphic, 'rotate', d)
                if self._selected_pin is None and self._selected_graphic is not None
                else self.rotate_pin(self._selected_pin, d)))
        delete_shortcut = QShortcut(QKeySequence('Delete'), self.view)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._delete_selected_graphic)
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
        """@brief Load a changed project baseline while preserving drafts for unchanged inputs."""
        key = deepcopy((name, pinout, footprint, list(pad_ids) if pad_ids is not None else None))
        if not force and key == self._source_key:
            return
        self.view.cancel_interaction()
        self._recovery_path = None
        self._recoverable = None
        self.recovery_button.defaultAction().setEnabled(False)
        self._source_key = key
        self._error = ''
        # The previous layout belongs to this project only when the name is
        # the same: another project's pins must not freeze this one's wires.
        same_project = self._symbol is not None and str(name) == self._name
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
                row['electrical_type_reviewed'] = False
                row.pop('electrical_type_evidence', None)
            row.setdefault('symbol_side', 'left' if index < (len(source) + 1) // 2 else 'right')
        # Legacy records (no `id`) get their stable id HERE, before the
        # baseline is copied: baseline and draft must agree on the ids, or an
        # untouched project would load dirty (Apply enabled, Export disabled)
        # and Cancel would regenerate the ids.
        graphics = self._graphics(source)
        # Wires anchored to a pin the new pinout no longer has keep its last
        # position as a fixed point. Only the layout of the SAME project can
        # tell where that pin was; at the first load, or coming from another
        # project, nothing is frozen and the anchor falls back to its stored
        # x/y, refreshed at every commit (`refresh_wire_anchors`).
        if same_project and graphics:
            from ..generators.symbol_glyphs import freeze_pin_anchors, pin_inner_ends
            previous_ends = pin_inner_ends(self._symbol)
            new_numbers = {str(row.get('pin', '')) for row in source}
            dropped = [number for number in previous_ends if number not in new_numbers]
            if dropped:
                for number in dropped:
                    freeze_pin_anchors(graphics, number, previous_ends)
                source = self._with_graphics(source, graphics)
        # Wires drawn before anchors existed: a free point that coincides
        # with a pin end or a terminal end was snapped there on purpose and
        # becomes anchored now, still before the baseline (not «modified»).
        if graphics:
            from ..generators.symbol_glyphs import adopt_coincident_anchors, pin_inner_ends
            try:
                pins, body = symbol_inputs(source)
                ends = pin_inner_ends(layout_symbol(pins, body)) if pins else {}
            except ValueError:
                ends = {}
            if adopt_coincident_anchors(graphics, ends):
                source = self._with_graphics(source, graphics)
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
        # Legacy records get their stable id here, in place, before the first
        # history entry is recorded: wire anchors name elements by id, and an
        # undo must never reach a draft whose ids differ.
        self._graphics(self._draft)
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
            # Re-selecting the existing default is an explicit confirmation too.
            types.activated.connect(lambda value, i=index: self._type_changed(i))
            self.table.setCellWidget(index, 2, types)
        self._loading = False
        self._changed()

    def _name_changed(self, item):
        if self._loading:
            return
        if item.column() == 1:
            row = self._draft[item.row()]
            name = item.text().strip()
            if name != str(row.get('name', '')).strip():
                # A reviewed role/source describes the previous function, not
                # just this terminal number. Keep the choice, revoke its proof.
                row['name'] = name
                row['electrical_type_reviewed'] = False
                row.pop('electrical_type_evidence', None)
        self._changed()

    def _type_changed(self, index):
        if not self._loading:
            self._draft[index]['electrical_type_reviewed'] = True
            self._draft[index].pop('electrical_type_evidence', None)
            self._changed()

    def set_recovery_path(self, path):
        """@brief Locate a recovery draft matching the current applied baseline."""
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
        """@brief Restore recovery rows as an editable, unapplied draft."""
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
        """@brief Report invalid geometry, missing names, unreviewed roles and overlapping labels."""
        issues = []
        rows = self.draft_rows()
        if not rows or self._symbol is None or self._error:
            issues.append(self.tx('Geometria o corrispondenza pin non valida.'))
        for row in rows:
            number = str(row.get('pin', ''))
            if not str(row.get('name') or '').strip():
                issues.append(f"{self.tx('Nome mancante')}: {number}")
            if (row.get('electrical_type_reviewed') is False or
                    row.get('electrical_type', 'passive') in ('passive', 'unspecified')
                    and not row.get('electrical_type_reviewed')):
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
        """@brief Display symbol findings and return whether export may continue."""
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
        """@brief Require acceptance of outstanding symbol findings before export."""
        return self.show_review(exporting=True)

    def rows(self):
        """@brief Return independent applied rows for persistence/export."""
        return deepcopy(self._baseline)

    def use_pin_type_suggestions(self, proposals):
        """@brief One undoable draft operation, preserving all pin and body geometry."""
        rows = self.draft_rows()
        by_number = {str(p['pin']): p for p in proposals}
        for row in rows:
            proposal = by_number.get(str(row['pin']))
            if proposal and proposal['electrical_type'] in ELECTRICAL_TYPES:
                row['electrical_type'] = proposal['electrical_type']
                row['electrical_type_reviewed'] = True
                row['electrical_type_evidence'] = {'source': proposal['source'], 'reason': proposal['reason']}
        self._populate(rows)

    def _fill_glyph_combo(self):
        """Fill the palette selector from the catalogue, keeping the chosen entry."""
        from ..generators.symbol_glyphs import catalogue
        current = self.glyph_combo.currentData()
        blocked = self.glyph_combo.blockSignals(True)
        self.glyph_combo.clear()
        for glyph in catalogue():
            self.glyph_combo.addItem(self.tx(glyph.label), glyph.key)
        index = self.glyph_combo.findData(current)
        self.glyph_combo.setCurrentIndex(max(0, index))
        self.glyph_combo.blockSignals(blocked)

    def _fill_palette(self):
        """Rebuild the palette panel from the catalogue (language change)."""
        self.palette.rebuild()

    def palette_actions(self):
        """@brief The element actions of the palette, in order."""
        return self.palette.actions()

    def _palette_chosen(self, key):
        """@brief A palette icon was clicked: choose that element and arm the placement."""
        index = self.glyph_combo.findData(key)
        if index >= 0:
            self.glyph_combo.setCurrentIndex(index)
        self._start_placing()

    def _graphics(self, rows=None):
        """The placed glyphs the draft carries (validated), [] when none or malformed."""
        from ..generators.symbol_glyphs import symbol_graphics
        try:
            return symbol_graphics(self.draft_rows() if rows is None else rows)
        except ValueError:
            return []

    @staticmethod
    def _with_graphics(rows, graphics):
        """Rows with `symbol_graphics` set to the same list on every row (like `symbol_body`)."""
        rows = deepcopy(rows)
        for row in rows:
            row['symbol_graphics'] = deepcopy(graphics)
        return rows

    @property
    def theme(self):
        """@brief The colour table of the canvas in use (see CANVAS_THEMES)."""
        return CANVAS_THEMES[self._theme_key]

    def set_kicad_colors(self, enabled):
        """@brief Draw the preview with KiCad's schematic colours (True) or the app's (False).

        @param enabled True for the KiCad scheme.
        @note Display only: the exported symbol keeps «fill background» and
            takes the colours of the schematic's own theme in KiCad.
        """
        self._theme_key = 'kicad' if enabled else 'dark'
        self.view.theme = self.theme
        self.view.setBackgroundBrush(QColor(self.theme['canvas']))
        self.view.viewport().update()
        action = self.theme_button.defaultAction()
        if action.isChecked() != bool(enabled):
            blocked = action.blockSignals(True)
            action.setChecked(bool(enabled))
            action.blockSignals(blocked)
        self._draw(self._symbol)

    def start_wire(self):
        """@brief Arm the wire tool: every click adds a point until the wire is closed."""
        if self._symbol is None:
            self.wire_button.defaultAction().setChecked(False)
            return
        self.view.cancel_interaction()
        self.view.placing = False
        self.view.wiring = True
        self.wire_button.defaultAction().setChecked(True)
        self.palette.set_tool_checked('wire', True)
        self._wire_points = []
        self.view.setFocus()
        self.set_status(self.tx('Filo: clicca i punti (si agganciano a pin e terminali); doppio clic, Invio o tasto destro per chiudere, Esc per annullare.'))
        self._draw(self._symbol)

    @staticmethod
    def _xy(point):
        """The (x, y) of a wire point, stored as `[x, y]` or `{"x", "y", "anchor"?}`."""
        if isinstance(point, dict):
            return (float(point.get('x', 0.0)), float(point.get('y', 0.0)))
        return (float(point[0]), float(point[1]))

    def _pin_ends(self):
        """`pin_inner_ends` of the current layout, {} without a symbol."""
        from ..generators.symbol_glyphs import pin_inner_ends
        return pin_inner_ends(self._symbol) if self._symbol is not None else {}

    def _wire_snap(self, x, y, graphics=None):
        """A wire point: anchored to the pin end or element terminal within the snap
        tolerance (`anchor_for_point`), else the free grid point `[x, y]`.

        @param graphics The records to anchor to; None reads the draft.
        """
        from ..generators.symbol_glyphs import anchor_for_point
        found = anchor_for_point(self._graphics() if graphics is None else graphics, self._pin_ends(), x, y)
        if found is not None:
            anchor, (px, py) = found
            return {'x': px, 'y': py, 'anchor': anchor}
        return [round(round(v / GRID_MM) * GRID_MM, 4) for v in (x, y)]

    def _exact_attachment(self, x, y, graphics):
        """A computed wire point (projection or corner): anchored when it falls
        EXACTLY on a pin end or terminal (tolerance 1e-6 mm, no snapping), so
        the element drags it afterwards; else the free `[x, y]`."""
        from ..generators.symbol_glyphs import anchor_for_point
        found = anchor_for_point(graphics, self._pin_ends(), x, y, tolerance=1e-6)
        if found is not None:
            anchor, (px, py) = found
            return {'x': px, 'y': py, 'anchor': anchor}
        return [round(x, 4), round(y, 4)]

    def _next_wire_points(self, x, y, free=False):
        """The point(s) a click at (x, y) appends to the wire being drawn.

        With the orthogonal constraint on (and Shift not held) the segment
        from the last point is horizontal or vertical: a free target is
        projected on the longer axis of its offset; an anchored target that is
        not on the same row/column gets a corner before it, so the attachment
        stays where the pin or terminal is. The corner is chosen so that the
        LAST segment enters the attachment along the pin's own axis: a
        horizontal pin or terminal (angle 0/180) is reached horizontally, a
        vertical one (90/270) vertically; when the direction is unknown the
        corner is horizontal-first. A projection or corner that lands exactly
        on a pin end or terminal is stored anchored (`_exact_attachment`).
        @return [] when nothing changes (the target repeats the last point).
        """
        from ..generators.symbol_glyphs import anchor_direction
        graphics = self._graphics()
        point = self._wire_snap(float(x), float(y), graphics)
        if not self._wire_points:
            return [point]
        lx, ly = self._xy(self._wire_points[-1])
        px, py = self._xy(point)
        if self._wire_orthogonal and not free:
            dx, dy = px - lx, py - ly
            aligned = math.isclose(dx, 0.0, abs_tol=1e-6) or math.isclose(dy, 0.0, abs_tol=1e-6)
            if isinstance(point, dict):
                if not aligned:
                    direction = anchor_direction(point.get('anchor'), graphics, self._symbol)
                    corner = (lx, py) if direction in (0, 180) else (px, ly)
                    return [self._exact_attachment(corner[0], corner[1], graphics), point]
            elif not aligned:
                corner = (px, ly) if abs(dx) >= abs(dy) else (lx, py)
                point = self._exact_attachment(corner[0], corner[1], graphics)
                px, py = self._xy(point)
        if math.isclose(px, lx, abs_tol=1e-6) and math.isclose(py, ly, abs_tol=1e-6):
            return []
        return [point]

    def set_wire_orthogonal(self, enabled):
        """@brief Constrain new wire segments to horizontal/vertical (True) or draw them free.

        @param enabled The constraint state; the toolbar action follows it.
        """
        self._wire_orthogonal = bool(enabled)
        action = self.orthogonal_button.defaultAction()
        if action.isChecked() != self._wire_orthogonal:
            blocked = action.blockSignals(True)
            action.setChecked(self._wire_orthogonal)
            action.blockSignals(blocked)

    def add_wire_point(self, x, y, free=False):
        """@brief Add a point (symbol mm, Y up) to the wire being drawn, snapped; True when added.

        @param x Symbol X in mm.
        @param y Symbol Y in mm.
        @param free True (Shift held) lets the segment be diagonal even with
            the orthogonal constraint on.
        @return False when no wire is being drawn or the point repeats the last one.
        @note A point within the snap tolerance of a pin's inner end or of an
            element terminal is stored anchored to it and follows it afterwards.
        """
        if not self.view.wiring and not self._wire_points and self._symbol is None:
            return False
        added = self._next_wire_points(x, y, free)
        if not added:
            return False
        self._wire_points.extend(added)
        self._draw(self._symbol)
        return True

    def _wire_point(self, point, free=False):
        self.add_wire_point(point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM, free)

    def finish_wire(self, commit=True):
        """@brief Close the wire being drawn: with two or more points it becomes a graphic element.

        @param commit False abandons the points.
        @return True when a wire was added.
        """
        points, self._wire_points = self._wire_points, []
        self.view.wiring = False
        self.wire_button.defaultAction().setChecked(False)
        self.palette.set_tool_checked('wire', False)
        self._wire_cursor_item = None
        if not commit or len(points) < 2 or self._symbol is None:
            self.set_status('')
            self._draw(self._symbol)
            return False
        from ..generators.symbol_glyphs import WIRE_KEY, new_instance_id
        rows = self._free_rows(self.draft_rows(), self._symbol)
        # `_free_rows` ran `symbol_graphics` on the rows: every element the
        # anchors name carries its id in the rows being committed.
        graphics = self._graphics(rows) + [{
            'id': new_instance_id(),
            'key': WIRE_KEY, 'label': 'Filo', 'source': 'wire', 'x': 0.0, 'y': 0.0,
            'rotation': 0, 'mirror': False,
            'primitives': [{'kind': 'polyline', 'points': [deepcopy(p) for p in points], 'radius': 0.0,
                            'fill': 'none', 'width': 0.254}],
            'terminals': []}]
        if not self._accept_geometry(self._with_graphics(rows, graphics)):
            return False
        self.set_status(self.tx('Filo inserito.'))
        self._select_graphic(len(graphics) - 1)
        return True

    def _start_placing(self):
        """@brief Arm the next click in the symbol to place the chosen glyph."""
        if self._symbol is None or self.glyph_combo.currentData() is None:
            return
        self.view.cancel_interaction()
        self.view.placing = True
        self.view.setFocus()
        self.set_status(self.tx('Clicca nel simbolo dove posizionare l’elemento; Esc o tasto destro per annullare.'))

    def _place_requested(self, point):
        """@brief Place the chosen glyph at a scene point snapped to the grid; None cancels."""
        self.view.placing = False
        if hasattr(self, 'palette'):
            self.palette.release()
        if point is None or self._symbol is None:
            self.set_status('')
            return
        from ..generators.symbol_glyphs import catalogue, instance_from_glyph
        key = self.glyph_combo.currentData()
        glyph = next((g for g in catalogue() if g.key == key), None)
        if glyph is None:
            return
        x = round(round(point.x() / SCENE_UNITS_PER_MM / GRID_MM) * GRID_MM, 4)
        y = round(round(-point.y() / SCENE_UNITS_PER_MM / GRID_MM) * GRID_MM, 4)
        rows = self._free_rows(self.draft_rows(), self._symbol)
        graphics = self._graphics(rows) + [instance_from_glyph(glyph, x, y)]
        if self._accept_geometry(self._with_graphics(rows, graphics)):
            self._select_graphic(len(graphics) - 1)
            self.set_status(self.tx('Elemento inserito: trascinalo per spostarlo, tasto destro per ruotare, specchiare o eliminare; i pin trascinati sui suoi terminali vi si agganciano.'))

    def place_glyph(self, key, x, y, rotation=0, mirror=False):
        """@brief Place a catalogue glyph programmatically (tests and automation).

        @param key Catalogue key.
        @param x Symbol X in mm (snapped to the grid).
        @param y Symbol Y in mm, Y up (snapped to the grid).
        @param rotation Quarter turns in degrees.
        @param mirror Mirror about the vertical axis.
        @return True when placed.
        """
        from ..generators.symbol_glyphs import catalogue, instance_from_glyph
        glyph = next((g for g in catalogue() if g.key == key), None)
        if glyph is None or self._symbol is None:
            return False
        rows = self._free_rows(self.draft_rows(), self._symbol)
        snapped = [round(round(v / GRID_MM) * GRID_MM, 4) for v in (x, y)]
        graphics = self._graphics(rows) + [instance_from_glyph(glyph, snapped[0], snapped[1], rotation, mirror)]
        if not self._accept_geometry(self._with_graphics(rows, graphics)):
            return False
        self._select_graphic(len(graphics) - 1)
        return True

    def graphics(self):
        """@brief The placed glyph records of the current draft."""
        return self._graphics()

    def _select_graphic(self, index):
        self._selected_graphic = index
        if index is not None:
            self._selected_pins = set()
            self._selected_pin = None
        self._draw(self._symbol)

    def _edit_graphic(self, index, operation, value=None):
        """@brief Turn, mirror, move or delete one placed glyph; an unknown index does nothing.

        @return True when the draft was updated; False for an unknown index or
            operation, and for 'rotate'/'mirror' on a wire: its free vertices
            would turn about the wire's origin while the anchored ends stayed
            on their pins and terminals, destroying the shape.
        """
        from ..generators.symbol_glyphs import is_wire
        if self._symbol is None or self._gesture_before is not None:
            return False
        rows = self._free_rows(self.draft_rows(), self._symbol)
        graphics = self._graphics(rows)
        if index is None or not 0 <= index < len(graphics):
            return False
        instance = graphics[index]
        if operation in ('rotate', 'mirror') and is_wire(instance):
            return False
        if operation == 'rotate':
            instance['rotation'] = (int(instance.get('rotation', 0)) + int(value)) % 360
        elif operation == 'mirror':
            instance['mirror'] = not bool(instance.get('mirror', False))
        elif operation == 'move':
            instance['x'], instance['y'] = float(value[0]), float(value[1])
        elif operation == 'delete':
            # Wires attached to the element keep their ends where they are.
            from ..generators.symbol_glyphs import freeze_wire_anchors
            freeze_wire_anchors(graphics, str(instance.get('id', '')), self._pin_ends())
            del graphics[index]
            self._selected_graphic = None
        else:
            return False
        return self._accept_geometry(self._with_graphics(rows, graphics))

    def move_wire_vertex(self, index, position, x, y):
        """@brief Move one vertex of a wire: anchored within the snap tolerance of a pin end or
        element terminal, else free on the grid.

        @param index Index of the wire in the draft's graphics.
        @param position Index of the vertex in the wire's polyline.
        @param x Target symbol X in mm.
        @param y Target symbol Y in mm (Y up).
        @return True when the draft was updated (also when nothing changed).
        @note Inside a drag gesture the edit is rebuilt from the gesture's
            starting rows, so the drop point alone decides the result; a free
            vertex is stored in the wire's own frame (its x/y/rotation/mirror).
        """
        from ..generators.symbol_glyphs import is_wire
        if self._symbol is None:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        rows = self._free_rows(source, symbol)
        graphics = self._graphics(rows)
        if not 0 <= index < len(graphics) or not is_wire(graphics[index]):
            return False
        wire = graphics[index]
        points = (wire.get('primitives') or [{}])[0].get('points', [])
        if not 0 <= position < len(points):
            return False
        point = self._wire_snap(float(x), float(y), graphics)
        if isinstance(point, list):
            point = list(self._wire_local(wire, *point))
        else:
            # The anchored point's fallback, like a free vertex, lives in the
            # wire's own frame (`refresh_wire_anchors` keeps it there).
            point['x'], point['y'] = self._wire_local(wire, point['x'], point['y'])
        if points[position] == point:
            self._draft = deepcopy(source)
            self._changed()
            return True
        points[position] = point
        return self._accept_geometry(self._with_graphics(rows, graphics))

    @staticmethod
    def _wire_local(wire, x, y):
        """The wire-frame coordinates of an absolute point: the inverse of the
        move/rotate/mirror `placed_primitives` applies to a free point
        (`symbol_glyphs.wire_local_point`)."""
        from ..generators.symbol_glyphs import wire_local_point
        return wire_local_point(wire, x, y)

    def fit_body_to_graphic(self, index):
        """@brief Resize the body to the outline the library symbol had, when it had one."""
        graphics = self._graphics()
        if not 0 <= index < len(graphics):
            return False
        from ..generators.symbol_glyphs import catalogue
        glyph = next((g for g in catalogue() if g.key == graphics[index].get('key')), None)
        if glyph is None or glyph.body_hint is None:
            return False
        width, height = glyph.body_hint
        if int(graphics[index].get('rotation', 0)) % 180:
            width, height = height, width
        return self.resize_body(width, height)

    def _delete_selected_graphic(self):
        if self._selected_graphic is not None:
            self._edit_graphic(self._selected_graphic, 'delete')

    def _graphic_context_menu(self, index, global_pos):
        """@brief Rotate, mirror, delete a placed glyph, or fit the body to it."""
        self._select_graphic(index)
        graphics = self._graphics()
        if not 0 <= index < len(graphics):
            return
        menu = QMenu(self)
        title = menu.addAction(self.tx(str(graphics[index].get('label', ''))))
        title.setEnabled(False)
        menu.addSeparator()
        menu.setToolTipsVisible(True)
        from ..generators.symbol_glyphs import catalogue, is_wire
        wire = is_wire(graphics[index])
        # A wire is reshaped by dragging its vertices: turning or mirroring it
        # would detach its shape from the anchored ends (see `_edit_graphic`).
        entries = [] if wire else [
            ('rotate-ccw.svg', 'Ruota di 90 gradi (antiorario)', 'rotate', 90),
            ('rotate-cw.svg', 'Ruota di 90 gradi (orario)', 'rotate', -90),
            ('format-painter.svg', 'Specchia', 'mirror', None),
        ]
        entries.append(('delete.svg', 'Elimina elemento', 'delete', None))
        for asset, label, operation, value in entries:
            action = menu.addAction(QIcon(str(Path(__file__).parent / 'assets' / asset)), self.tx(label))
            action.setIconVisibleInMenu(True)
            action.triggered.connect(lambda checked=False, op=operation, v=value: self._edit_graphic(index, op, v))
        glyph = None if wire else next((g for g in catalogue() if g.key == graphics[index].get('key')), None)
        if glyph is not None and glyph.body_hint is not None:
            fit = menu.addAction(self.tx('Adatta il rettangolo del corpo all’elemento'))
            fit.triggered.connect(lambda checked=False: self.fit_body_to_graphic(index))
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

    def retranslate(self):
        """@brief Refresh dynamic labels without losing unapplied edits."""
        if hasattr(self, 'glyph_combo'):
            self._fill_glyph_combo()
        if hasattr(self, 'palette'):
            self._fill_palette()
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
        """@brief Copy draft metadata and merge table names and electrical types."""
        result = deepcopy(self._draft)
        for index, row in enumerate(result):
            row['name'] = self.table.item(index, 1).text().strip()
            row['electrical_type'] = self.table.cellWidget(index, 2).currentData()
        return result

    def has_pending_edits(self):
        """@brief Compare editable rows with the applied baseline."""
        return self.draft_rows() != self._baseline

    def footprint(self):
        """@brief Return the associated footprint library identifier."""
        return self._footprint

    def set_error(self, message):
        """@brief Record a blocking error and refresh editor action states."""
        self._error = str(message)
        self._changed()

    def set_status(self, message):
        """@brief Show a nonblocking export result without changing draft validity."""
        self.status.setText(str(message))

    def apply(self):
        """@brief Publish valid draft rows for persistence while retaining a rollback baseline."""
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
        """@brief Keep the editable draft and its history when persistence fails."""
        self._applying = False
        self._baseline = deepcopy(self._apply_previous_baseline)
        self._changed()
        self.set_status(message)

    def accept_apply(self):
        """@brief Synchronize the saved source without throwing away undo history."""
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
        export_enabled = valid and not dirty
        self.export_button.defaultAction().setEnabled(export_enabled)
        self.export_enabled_changed.emit(export_enabled)
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
        """@brief Restore the preceding draft snapshot from local edit history."""
        self._restore_history(self._history_index - 1)

    def redo(self):
        """@brief Restore the next draft snapshot from local edit history."""
        self._restore_history(self._history_index + 1)

    def reset_layout(self):
        """@brief Restore two-column placement while preserving pin identities and roles."""
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
        if self._selected_pins:
            self._selected_graphic = None
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
            item = self._wire_cursor_item
            if item is not None and self._wire_points and self.view.wiring:
                # The rubber band from the last wire point follows the cursor
                # with the same constraint a click would apply (corner included).
                from PyQt6.QtGui import QPainterPath
                from PyQt6.QtWidgets import QApplication
                free = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
                lx, ly = self._xy(self._wire_points[-1])
                path = QPainterPath(QPointF(lx * SCENE_UNITS_PER_MM, -ly * SCENE_UNITS_PER_MM))
                for x, y in map(self._xy, self._next_wire_points(
                        point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM, free)):
                    path.lineTo(x * SCENE_UNITS_PER_MM, -y * SCENE_UNITS_PER_MM)
                item.setPath(path)
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
        elif target[0] == 'graphic':
            graphics = self._graphics(self._gesture_before)
            index = target[1]
            self._gesture_graphic_origin = ((graphics[index]['x'], graphics[index]['y'])
                                            if 0 <= index < len(graphics) else None)
            self._select_graphic(index)
        elif target[0] == 'vertex':
            self._select_graphic(target[1])

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
            layout = layout_symbol(pins, body)
        except ValueError:
            self.status.setText(self.tx('Posizione non valida: verifica griglia e punti di connessione.'))
            return False
        # Every committed draft carries, in each anchored wire point, the
        # position the anchor resolves to against the geometry just accepted:
        # the fallback a reload without that pin or element will use.
        graphics = self._graphics(rows)
        if graphics:
            from ..generators.symbol_glyphs import pin_inner_ends, refresh_wire_anchors
            refresh_wire_anchors(graphics, pin_inner_ends(layout))
            rows = self._with_graphics(rows, graphics)
        # Geometry changes do not rebuild the table or interrupt its editor.
        self._draft = deepcopy(rows)
        self._changed()
        return True

    def move_pin(self, number, side, offset, *, swap=False):
        """@brief Snap a pin to a body side and offset, optionally swapping an occupied position."""
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
        graphics = self._graphics(rows)
        if graphics:
            from ..generators.symbol_glyphs import pin_inner_ends, refresh_wire_anchors
            refresh_wire_anchors(graphics, pin_inner_ends(symbol))
        rows = deepcopy(rows)
        for row, placed in zip(rows, symbol.pins):
            row['symbol_position'] = [placed.x, placed.y]
            row['symbol_angle'] = placed.angle
            row['symbol_body'] = [symbol.width, symbol.height]
            row['symbol_graphics'] = deepcopy(graphics)
        return rows

    def place_pin(self, number, x, y):
        """@brief Place a pin at snapped free coordinates and swap any occupied terminal."""
        if self._symbol is None:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        rows = self._free_rows(source, symbol)
        row = next((r for r in rows if str(r['pin']) == str(number)), None)
        if row is None:
            return False
        position = [round(round(v / GRID_MM) * GRID_MM, 4) for v in (x, y)]
        # A pin dropped on a glyph terminal takes the terminal's place and
        # direction (the pin position KiCad's own symbol used).
        from ..generators.symbol_glyphs import snap_to_terminal
        terminal = snap_to_terminal(self._graphics(rows), x, y)
        angle = row.get('symbol_angle')
        if terminal is not None:
            position = [round(terminal.x, 4), round(terminal.y, 4)]
            angle = terminal.angle
        if position == row['symbol_position'] and angle == row.get('symbol_angle'):
            self._draft = deepcopy(source)
            self._changed()
            return True
        occupied = next((r for r in rows if r is not row and r['symbol_position'] == position), None)
        if occupied is not None:
            occupied['symbol_position'] = row['symbol_position'][:]
        row['symbol_position'] = position
        if terminal is not None:
            row['symbol_angle'] = angle
        return self._accept_geometry(rows)

    def rotate_pin(self, number, degrees):
        """@brief Rotate a pin or selected group in grid-preserving quarter turns."""
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
        """@brief Validate and apply a terminal position and orientation as one edit."""
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
        """@brief Resize the unlocked body on the grid while preserving free pin positions."""
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
        if target[0] == 'graphic':
            # Dragging a record translates its placement (x, y). For a glyph
            # that moves the whole drawing. For a wire the semantics are:
            # MOVE when no point is anchored (every vertex translates, the
            # wire keeps its shape); STRETCH when at least one end is anchored
            # (free vertices translate, anchored ends stay on their pin or
            # terminal, since a resolved anchor is absolute). Detaching an end
            # is a deliberate act: drag its vertex handle away instead.
            origin = getattr(self, '_gesture_graphic_origin', None)
            if origin is None:
                return
            delta = point - self._gesture_start
            x = round(round((origin[0] + delta.x() / SCENE_UNITS_PER_MM) / GRID_MM) * GRID_MM, 4)
            y = round(round((origin[1] - delta.y() / SCENE_UNITS_PER_MM) / GRID_MM) * GRID_MM, 4)
            rows = self._free_rows(self._gesture_before, symbol)
            graphics = self._graphics(rows)
            if 0 <= target[1] < len(graphics):
                graphics[target[1]]['x'], graphics[target[1]]['y'] = x, y
                self._accept_geometry(self._with_graphics(rows, graphics))
            return
        if target[0] == 'vertex':
            self.move_wire_vertex(target[1], target[2], point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM)
            return
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

    def _draw_graphics(self, scene, scale):
        """Draw every placed glyph (primitives and terminals), tagged for selection and dragging."""
        from PyQt6.QtGui import QPainterPath
        from ..generators.symbol_glyphs import arc_points, placed_primitives, placed_terminals
        from ..generators.symbol_glyphs import elements_by_id, is_wire
        graphics = self._graphics()
        # Resolved once per draw and shared by every wire.
        pin_ends = self._pin_ends()
        by_id = elements_by_id(graphics)
        for index, instance in enumerate(graphics):
            selected = index == self._selected_graphic
            theme = self.theme
            pen = QPen(QColor(theme['selected']) if selected else QColor(theme['graphic']), 2.2 if selected else 1.3)
            items = []
            # Anchored wire points are resolved against the current elements
            # and pins: a moved element or pin drags its wires along.
            primitives = placed_primitives(instance, graphics=graphics, pin_ends=pin_ends, by_id=by_id)
            if selected and is_wire(instance) and primitives:
                # Vertices of the selected wire: small squares to drag on
                # the grid, onto a terminal/pin (anchors) or away (frees).
                for position, (x, y) in enumerate(primitives[0].points):
                    square = scene.addRect(x * scale - 3.5, -y * scale - 3.5, 7, 7,
                                           QPen(QColor(theme['selected']), 1.0), QColor(theme['handle']))
                    square.setZValue(12)
                    square.setData(0, ('vertex', index, position))
                    square.setCursor(Qt.CursorShape.CrossCursor)
                    square.setToolTip(self.tx('Vertice del filo: trascinalo; su un pin o un terminale si aggancia.'))
            for p in primitives:
                if p.kind in ('polyline', 'arc'):
                    points = p.points if p.kind == 'polyline' else arc_points(*p.points)
                    path = QPainterPath()
                    path.moveTo(points[0][0] * scale, -points[0][1] * scale)
                    for x, y in points[1:]:
                        path.lineTo(x * scale, -y * scale)
                    brush = QColor(theme['graphic']) if p.fill == 'outline' else QColor(theme['graphic_fill']) if p.fill == 'background' else QColor(0, 0, 0, 0)
                    items.append(scene.addPath(path, pen, brush))
                elif p.kind == 'circle':
                    (cx, cy), r = p.points[0], p.radius * scale
                    brush = QColor(theme['graphic']) if p.fill == 'outline' else QColor(theme['graphic_fill']) if p.fill == 'background' else QColor(0, 0, 0, 0)
                    items.append(scene.addEllipse(cx * scale - r, -cy * scale - r, 2 * r, 2 * r, pen, brush))
                elif p.kind == 'rectangle':
                    (x0, y0), (x1, y1) = p.points
                    left, top = min(x0, x1) * scale, -max(y0, y1) * scale
                    brush = QColor(theme['graphic']) if p.fill == 'outline' else QColor(theme['graphic_fill']) if p.fill == 'background' else QColor(0, 0, 0, 0)
                    items.append(scene.addRect(left, top, abs(x1 - x0) * scale, abs(y1 - y0) * scale, pen, brush))
            occupied = {(round(p.x, 4), round(p.y, 4)) for p in (self._symbol.pins if self._symbol else ())}
            for terminal in placed_terminals(instance):
                if (round(terminal.x, 4), round(terminal.y, 4)) not in occupied:
                    # The terminal is the OUTER end of the pin to come: until a
                    # pin sits there, a dotted guide shows the pin's place
                    # between the dot and the drawing (user, 2026-10-10).
                    vx, vy = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[terminal.angle % 360]
                    guide = QPen(QColor(theme['terminal']), 1.0, Qt.PenStyle.DotLine)
                    scene.addLine(terminal.x * scale, -terminal.y * scale,
                                  (terminal.x + vx * terminal.length) * scale,
                                  -(terminal.y + vy * terminal.length) * scale, guide)
                dot = scene.addEllipse(terminal.x * scale - 3, -terminal.y * scale - 3, 6, 6,
                                       QPen(QColor(theme['terminal']), 1.0), QColor(0, 0, 0, 0))
                dot.setToolTip(self.tx('Terminale dell’elemento: trascina qui un pin per agganciarlo.')
                               + (f' ({terminal.name or terminal.number})' if terminal.name or terminal.number else ''))
                dot.setZValue(5)
            hint = self.tx('Filo: trascinalo per spostarlo; se ha estremi agganciati si spostano solo i vertici liberi.') \
                if is_wire(instance) else self.tx(str(instance.get('label', '')))
            for item in items:
                item.setData(0, ('graphic', index))
                item.setCursor(Qt.CursorShape.SizeAllCursor)
                item.setToolTip(hint)

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
        theme = self.theme
        pen = QPen(QColor(theme['outline']), 1.3)
        text_color = QColor(theme['pin_name'])
        font = QFont(self.font())
        font.setPixelSize(12)
        small_font = QFont(font)
        small_font.setPixelSize(10)
        width, height = symbol.width * scale, symbol.height * scale
        scene.addRect(-width / 2, -height / 2, width, height, pen, QColor(theme['body_fill']))
        self._draw_graphics(scene, scale)
        if self._wire_points:
            from PyQt6.QtGui import QPainterPath
            wire_pen = QPen(QColor(theme['wire']), 1.6, Qt.PenStyle.DashLine)
            points = [self._xy(p) for p in self._wire_points]
            for (x0, y0), (x1, y1) in zip(points, points[1:]):
                scene.addLine(x0 * scale, -y0 * scale, x1 * scale, -y1 * scale, wire_pen)
            for x, y in points:
                scene.addEllipse(x * scale - 2.5, -y * scale - 2.5, 5, 5, wire_pen, QColor(theme['wire']))
            last = points[-1]
            self._wire_cursor_item = scene.addPath(
                QPainterPath(QPointF(last[0] * scale, -last[1] * scale)), wire_pen, QColor(0, 0, 0, 0))
        else:
            self._wire_cursor_item = None

        def tag(item, target):
            item.setData(0, target)
            item.setCursor(Qt.CursorShape.SizeAllCursor)
            if target[0] == 'pin':
                item.setToolTip(target[1])
            return item

        def label(value, x, y, align='left', small=False, target=None, rotate=False):
            item = scene.addSimpleText(value, small_font if small else font)
            item.setBrush(QColor(theme['pin_number']) if small else text_color)
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
            pin_pen = QPen(QColor(theme['selected']) if selected else QColor(theme['pin']), 2.8 if selected else 1.3)
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
            item = tag(scene.addRect(x - 4, y - 4, 8, 8, pen, QColor(theme['handle'])), ('resize', hx, hy))
            item.setZValue(10)
            item.setCursor(Qt.CursorShape.SizeHorCursor if not hy else Qt.CursorShape.SizeVerCursor if not hx else
                           Qt.CursorShape.SizeFDiagCursor if hx == hy else Qt.CursorShape.SizeBDiagCursor)
        if self._gesture_before is not None:
            scene.setSceneRect(self._gesture_scene_rect)
        else:
            scene.setSceneRect(scene.itemsBoundingRect().adjusted(-35, -35, 35, 35))
        if self.view.auto_fit:
            self.view.fit()
