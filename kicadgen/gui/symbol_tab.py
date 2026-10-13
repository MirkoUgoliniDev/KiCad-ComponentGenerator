"""@brief Editable electrical pin mapping and a preview of the exported symbol layout."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import json
import math
import os
import tempfile
import time
from PyQt6.QtCore import Qt, pyqtSignal, QLineF, QSize, QRect, QRectF, QPointF, QItemSelectionModel
from PyQt6.QtGui import QPalette, QAction, QColor, QFont, QPainter, QPen, QShortcut, QKeySequence, QIcon, QPolygonF
from PyQt6.QtWidgets import (QHBoxLayout, QGridLayout, QFrame, QScrollArea, QAbstractItemView, QComboBox, QGraphicsScene, QGraphicsView, QHeaderView, QDialog, QDialogButtonBox, QPushButton, QDoubleSpinBox, QFormLayout, QLabel, QMenu, QToolButton, QSplitter, QTableWidget, QTableWidgetItem, QListWidget, QRubberBand, QToolBar, QSizePolicy, QVBoxLayout, QWidget, QLineEdit, QCheckBox)

from ..generators.symbol import (ELECTRICAL_TYPES, PIN_GRID_MM, PROPERTIES_KEY, PROPERTY_KEYS,
                                 default_property_positions, symbol_inputs, layout_symbol,
                                 symbol_property_positions)
from .pinout_dialog import pin_sort_key
from ..generators.symbol_edit import transform_selection

SCENE_UNITS_PER_MM = 10.0
GRID_MM = PIN_GRID_MM

#: Superseded drafts kept per project beside the recovery file (one file per
#: pinout change that could not carry a pending draft over). Disk-usage bound,
#: not a semantic limit: beyond it the OLDEST snapshot is removed, so a
#: project folder holds at most this many files of `_SNAPSHOT_MAX_BYTES` each.
MAX_SUPERSEDED_DRAFTS = 5

#: A superseded snapshot larger than this (bytes) is not read back: it holds
#: two copies of the rows (old baseline and draft), twice the 2 MB bound the
#: recovery file itself is read with (`symbol_draft.load_draft`).
_SNAPSHOT_MAX_BYTES = 4_000_000

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
    from ..generators.symbol_glyphs import instance_from_glyph, placed_primitives, placed_terminals, primitive_outline
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
        if p.kind in ('polyline', 'arc', 'bezier'):
            points = primitive_outline(p)
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
        elif p.kind == 'text':
            text_font = QFont(painter.font())
            text_font.setPixelSize(max(6, int(size * 0.6)))
            painter.setFont(text_font)
            painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, p.text)
    painter.setPen(QPen(QColor('#a6e3a1'), 1.0))
    painter.setBrush(QColor('#a6e3a1'))
    for t in terminals:
        px, py = to_px(t.x, t.y)
        painter.drawEllipse(QPointF(px, py), 1.6, 1.6)
    painter.end()
    return QIcon(pixmap)


#: The KiCad fill modes and their labels (Italian source, translated by `tx`).
FILL_LABELS = (('none', 'Nessuno'), ('outline', 'Contorno'), ('background', 'Sfondo'))

#: Grid steps offered for the drawing tools, mm (100, 50, 25 and 10 mil).
DRAWING_GRIDS_MM = (2.54, 1.27, 0.635, 0.254)

#: Default step of the drawing grid, mm: one tenth of the pin grid (user,
#: 2026-10-13: «una griglia puntinata a un decimo di quella attuale per
#: ancorare gli elementi grafici»). Pins keep snapping to GRID_MM.
DEFAULT_DRAWING_GRID_MM = GRID_MM / 10

#: Smallest on-screen spacing, in viewport pixels, at which the dots of the
#: drawing grid are painted. This is a legibility threshold, not geometry:
#: below it the dots would merge into a smear that hides the drawing, so
#: the canvas shows only the pin-grid lines. Snapping is unaffected.
FINE_GRID_MIN_PX = 5.0

#: The KiCad stroke types and their labels (Italian source, translated by `tx`).
STYLE_LABELS = (('default', 'Predefinito'), ('solid', 'Continua'), ('dash', 'Tratteggiata'),
                ('dot', 'Punteggiata'), ('dash_dot', 'Tratto-punto'), ('dash_dot_dot', 'Tratto-punto-punto'))

#: Qt pen style of every KiCad stroke type ("default" is drawn solid, as KiCad does).
PEN_STYLES = {'default': Qt.PenStyle.SolidLine, 'solid': Qt.PenStyle.SolidLine, 'dash': Qt.PenStyle.DashLine,
              'dot': Qt.PenStyle.DotLine, 'dash_dot': Qt.PenStyle.DashDotLine,
              'dash_dot_dot': Qt.PenStyle.DashDotDotLine}

#: The horizontal justifications of a text and their labels.
JUSTIFY_LABELS = (('left', 'Sinistra'), ('center', 'Centro'), ('right', 'Destra'))

#: Palette tooltip of every tool, by key (Italian source, translated by `tx`).
#: Side of a handle square and the margin around the selected element
#: inside which its handles are shown, both in SCREEN pixels at any zoom:
#: the squares ignore the view transform and the margin is divided by the
#: view scale when the pointer is tested (review, 2026-10-12).
HANDLE_SIZE_PX = 6
HANDLE_HOVER_MARGIN = 12

TOOL_HINTS = {
    'wire': 'Filo: clicca i punti nel simbolo; doppio clic o tasto destro per chiudere',
    'line': 'Linea: clicca i punti sulla griglia; doppio clic, Invio o tasto destro per chiudere, Esc per annullare',
    'rectangle': 'Rettangolo: clicca un angolo e poi quello opposto; Esc per annullare',
    'circle': 'Cerchio: clicca il centro e poi un punto sul bordo; Esc per annullare',
    'arc': 'Arco: clicca l’inizio, la fine e poi un punto intermedio; Esc per annullare',
    'bezier': 'Bézier: clicca l’inizio, i due punti di controllo e la fine; Esc per annullare',
    'text': 'Testo: clicca il punto di ancoraggio, poi scrivi il testo; Esc per annullare',
}


class TextDialog(QDialog):
    """@brief Content, font size and justification of a symbol text (insert or edit)."""

    def __init__(self, tx, text='', size=1.27, justify='center', parent=None):
        """@brief Build the dialog with the current values.

        @param tx Translator of Italian source strings.
        @param text Current text.
        @param size Current font size in mm.
        @param justify Current horizontal justification (left, center, right).
        @param parent Parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(tx('Testo del simbolo'))
        form = QFormLayout(self)
        self.text_edit = QLineEdit(text)
        self.text_edit.setMaxLength(200)
        form.addRow(tx('Testo'), self.text_edit)
        self.size_box = QDoubleSpinBox()
        self.size_box.setRange(0.1, 25.4)
        self.size_box.setSingleStep(0.127)
        self.size_box.setDecimals(3)
        self.size_box.setSuffix(' mm')
        self.size_box.setValue(float(size))
        form.addRow(tx('Dimensione'), self.size_box)
        self.justify_box = QComboBox()
        for key, label in JUSTIFY_LABELS:
            self.justify_box.addItem(tx(label), key)
        self.justify_box.setCurrentIndex(max(0, self.justify_box.findData(justify)))
        form.addRow(tx('Giustificazione'), self.justify_box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.text_edit.setFocus()
        self.text_edit.selectAll()

    def values(self):
        """@brief (text, size in mm, justify) as typed."""
        return (self.text_edit.text(), float(self.size_box.value()), str(self.justify_box.currentData()))

#: Colours of the symbol canvas. "dark": the application's own theme.
#: "kicad": the default colour scheme of KiCad's schematic editor, so the
#: preview shows what the schematic will (user, 2026-10-10: body yellow
#: with dark drawings, as in KiCad).
CANVAS_THEMES = {
    'dark': {
        'canvas': '#181825', 'grid': '#36364b', 'grid_dots': '#4d4d6b', 'body_fill': '#242438', 'outline': '#89b4fa',
        'graphic': '#cdd6f4', 'graphic_fill': '#242438', 'pin': '#89b4fa', 'pin_name': '#cdd6f4',
        'pin_number': '#cdd6f4', 'selected': '#f9e2af', 'terminal': '#a6e3a1', 'handle': '#a6e3a1',
        'wire': '#f9e2af',
    },
    'kicad': {
        'canvas': '#ffffff', 'grid': '#d9d9d9', 'grid_dots': '#bdbdbd', 'body_fill': '#ffffc2', 'outline': '#840000',
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
        # Free drawing (user, 2026-10-12: «tracciare disegni, linee curve»):
        # shapes without terminals, closed by a fixed number of clicks.
        from ..generators.symbol_glyphs import shape_tool_glyphs
        self._add_section('drawing', 'Disegno', shape_tool_glyphs(), open_state.get('drawing', True), tool=True)
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
                    action.setToolTip(self.tx(TOOL_HINTS.get(glyph.key, glyph.label)))
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
            header.toggled.connect(lambda checked, b=body, h=header: (
                b.setVisible(checked),
                h.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)))
            self._content_layout.addWidget(header)
            self._content_layout.addWidget(body)
            # Mounted first, shown after: an unparented body made visible is a
            # top-level window, one flash per open group at start-up
            # (user, 2026-10-13).
            body.setVisible(header.isChecked())
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
    #: Ctrl+click on a placed element: toggle it in the element selection.
    graphic_toggled = pyqtSignal(int)
    region_selected = pyqtSignal(object, bool)
    pointer_changed = pyqtSignal(object)
    #: The pointer left the viewport: hover-revealed handles go away.
    pointer_left = pyqtSignal()
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
        #: Step of the dotted drawing grid, mm; equal to GRID_MM means no dots.
        self.drawing_grid_mm = GRID_MM
        self.gesture = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mouseDoubleClickEvent(self, event):
        """@brief Qt override: a left double click ends the wire being drawn.

        @param event The mouse event; other double clicks reach the base class.
        """
        if self.wiring and event.button() == Qt.MouseButton.LeftButton:
            self.wiring = False
            self.wire_finished.emit(True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        """@brief Qt override: route a press to wiring, placement, selection, pan or a drag gesture.

        @param event The mouse event; unhandled presses reach the base class.
        @note The view grabs the mouse for rubber-band selection, pan and drags
            until the matching release or `cancel_interaction`.
        """
        # Hidden handles are not hit-tested: a press whose pointer arrived
        # without a move (touch, automation, coalesced events) must first
        # reveal them like a move would (review, 2026-10-12).
        self.pointer_changed.emit(self.mapToScene(event.pos()))
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
                    if target and target[0] == 'graphic':
                        self.graphic_toggled.emit(target[1])
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
        """@brief Qt override: ask for the context menu of the pin or element under the pointer.

        @param event The context-menu event; ignored on the background or during a drag.
        """
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

    def leaveEvent(self, event):
        """@brief Qt override: report that the pointer left the view (hides the hover handles).

        @param event The leave event.
        """
        self.pointer_left.emit()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        """@brief Qt override: update the pointer readout, rubber band, pan or the running drag.

        @param event The mouse event.
        """
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
        """@brief Qt override: finish the rubber band, pan or drag at the actual drop point.

        @param event The mouse event.
        """
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
        """@brief Qt override: Enter/Escape finish or cancel wiring, placement and drags.

        @param event The key event; other keys reach the base class.
        """
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
        """@brief Qt override: roll back an unfinished interaction before the view is hidden.

        @param event The hide event.
        """
        self.cancel_interaction()
        super().hideEvent(event)

    def set_grid_visible(self, visible):
        self.show_grid = bool(visible)
        self.viewport().update()

    def set_drawing_grid(self, step_mm):
        """@brief Set the step (mm) of the dotted drawing grid and repaint.

        @param step_mm Drawing grid step in mm; when it equals GRID_MM the
            dots coincide with the pin-grid crossings and none are drawn.
        """
        self.drawing_grid_mm = float(step_mm)
        self.viewport().update()

    def fine_grid_visible(self):
        """@brief True when the dotted drawing grid is painted at the current zoom.

        @return False when the grid is hidden, when the drawing step equals
            the pin grid or when the dots would be closer than FINE_GRID_MIN_PX.
        """
        if not self.show_grid or math.isclose(self.drawing_grid_mm, GRID_MM, abs_tol=1e-9):
            return False
        return self.drawing_grid_mm * SCENE_UNITS_PER_MM * self.transform().m11() >= FINE_GRID_MIN_PX

    def drawBackground(self, painter, rect):
        """@brief Paint the pin grid as lines and the drawing grid as dots.

        @param painter Scene painter.
        @param rect Exposed scene rectangle.
        @note Both grids share the scene origin, so every pin-grid crossing
            is also a node of the drawing grid; the dots are skipped when the
            drawing step equals the pin grid or is too dense on screen
            (`fine_grid_visible`).
        """
        super().drawBackground(painter, rect)
        if not self.show_grid:
            return
        painter.save()
        if self.fine_grid_visible():
            fine = self.drawing_grid_mm * SCENE_UNITS_PER_MM
            dots = QPolygonF()
            for column in range(math.floor(rect.left() / fine), math.ceil(rect.right() / fine) + 1):
                for row in range(math.floor(rect.top() / fine), math.ceil(rect.bottom() / fine) + 1):
                    dots.append(QPointF(column * fine, row * fine))
            dot_pen = QPen(QColor(self.theme['grid_dots']), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            dot_pen.setCosmetic(True)
            # Crisp dots: antialiasing would smear a 1.5 px point into a
            # faint halo and lose the dot colour at the node itself.
            antialiased = painter.testRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(dot_pen)
            painter.drawPoints(dots)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, antialiased)
        step = GRID_MM * SCENE_UNITS_PER_MM
        pen = QPen(QColor(self.theme['grid']))
        pen.setCosmetic(True)
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
        """@brief Qt override: zoom about the pointer, never during a drag or pan.

        @param event The wheel event; always accepted so the page does not scroll.
        """
        delta = event.angleDelta().y()
        if delta and self.gesture is None and self._pan_last is None:
            self.zoom(1.2 ** max(-4, min(4, delta / 120)), event.position().toPoint())
        event.accept()

    def resizeEvent(self, event):
        """@brief Qt override: refit the symbol after a resize while auto-fit is on.

        @param event The resize event.
        """
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit()

    def showEvent(self, event):
        """@brief Qt override: refit the symbol when the view is shown while auto-fit is on.

        @param event The show event.
        """
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
        #: Designator prefix drawn as «U?» (KiCad's symbol editor shows the
        #: Reference property that way); see `set_reference`.
        self._reference = 'U'
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
        self._handle_groups = []
        self._pointer_inside = True
        self._body_locked = False
        self._recovery_path = None
        self._recoverable = None
        #: Draft `set_data` could not rebase (different pins), offered by «Recupera bozza».
        self._stale_recoverable = None
        #: Superseded snapshot file the offered draft comes from, retired on recovery.
        self._recoverable_snapshot = None
        #: Recovery file of the project the editor content belongs to: the
        #: project identity (two projects may share a component name).
        self._draft_owner = None
        #: Draft carried over by `set_data`, confirmed or dropped by the next
        #: `set_recovery_path`: {'old_baseline', 'draft', 'kept'}.
        self._carry = None
        self._applying = False
        self._text_bounds = []
        self._symbol = None
        #: Index of the selected placed glyph in the rows' `symbol_graphics`, or None.
        self._selected_graphic = None
        #: Every selected element index (the primary included), for group operations.
        self._selected_graphics = set()
        #: Copied element records (`detached_copy`), pasted with Ctrl+V.
        self._clipboard = []
        #: Grid step of the drawing tools and shape handles, mm (pins keep GRID_MM).
        self._grid_mm = DEFAULT_DRAWING_GRID_MM
        #: False: shape points are not snapped (rounded to 0.01 mm).
        self._snap = True
        #: Canvas theme key: 'dark' (application) or 'kicad' (schematic colours).
        self._theme_key = 'dark'
        #: Points of the wire being drawn, [] when none: `[x, y]` (symbol mm)
        #: for a free grid point, `{"x", "y", "anchor"}` for a point attached
        #: to a pin's inner end or an element terminal (symbol_glyphs anchors).
        self._wire_points = []
        self._wire_cursor_item = None
        #: True: each new wire segment is horizontal or vertical (Shift frees it).
        self._wire_orthogonal = True
        #: Key of the shape tool in use (SHAPE_TOOLS) and its clicked points,
        #: `(x, y)` in symbol mm on the grid; None / [] when no shape is being drawn.
        self._shape_tool = None
        self._shape_points = []
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
        self.view.set_drawing_grid(self._grid_mm)
        self.view.selection_toggled.connect(self._toggle_selection)
        self.view.graphic_toggled.connect(self._toggle_graphic)
        self.view.region_selected.connect(self._select_region)
        self.view.pointer_changed.connect(self._update_readout)
        self.view.pointer_left.connect(self._on_pointer_left)
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
        # Drawing grid (user, 2026-10-12: precision): the step of the shape
        # tools and handles. Pins and glyph terminals stay on the 2.54 mm
        # pin grid whatever is chosen here, so connections remain valid.
        self.grid_combo = QComboBox()
        for step in DRAWING_GRIDS_MM:
            self.grid_combo.addItem(f'{step:g} mm', step)
        self.grid_combo.setCurrentIndex(self.grid_combo.findData(DEFAULT_DRAWING_GRID_MM))
        self.grid_combo.setToolTip(self.tx('Passo della griglia del disegno (i pin restano sulla griglia da 2.54 mm)'))
        self.grid_combo.setProperty('i18n_skip', True)
        self.grid_combo.currentIndexChanged.connect(lambda _i: self.set_drawing_grid(self.grid_combo.currentData()))
        toolbar.addWidget(self.grid_combo)
        self.snap_button = icon_button('symbol-grid.svg', 'Aggancio alla griglia del disegno')
        self.snap_button.defaultAction().setCheckable(True)
        self.snap_button.defaultAction().setChecked(True)
        self.snap_button.defaultAction().toggled.connect(self.set_snap)
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
        self.palette.tool_chosen.connect(self._tool_chosen)
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
                self._edit_selected_graphics('rotate', d)
                if self._selected_pin is None and self._selected_graphic is not None
                else self.rotate_pin(self._selected_pin, d)))
        delete_shortcut = QShortcut(QKeySequence('Delete'), self.view)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._delete_selected_graphic)
        # Clipboard of elements (user, 2026-10-12): copy, paste, duplicate.
        for key, callback in [('Ctrl+C', self.copy_selected), ('Ctrl+V', self.paste),
                              ('Ctrl+D', self.duplicate_selected)]:
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)
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
        """@brief Load a changed project baseline without ever losing an unapplied draft.

        @param name Component name; a different name is another project. The
            same name is NOT enough to be the same project: the identity is
            the recovery file given by the next `set_recovery_path`.
        @param pinout Pinout rows (`pin`, `name`, metadata). Pin numbers are
            stripped of surrounding blanks here, once: «1 » and «1» are one pin.
        @param footprint Footprint identifier «Library:Name».
        @param pad_ids Actual footprint pad numbers (stripped too), or None.
        @param force Reload even when the inputs did not change.
        @note A pending draft of the same project is rebased onto the new
            baseline when the pin identities are unchanged (each edited field
            keeps the draft value, the others take the new values) unless the
            new data changed one of the same fields differently. With
            different pins, such a conflict, or a merge whose layout does not
            validate while the draft's did, it is kept recoverable instead:
            «Recupera bozza» brings back the edits of the pins that still exist.
            The status tells the user which case happened. Nothing is written
            here: the owner calls `set_recovery_path` right after, which
            confirms the project and makes the outcome durable (rebased: the
            recovery file bound to the new baseline; kept: a superseded
            snapshot, `superseded_draft_paths`), or drops the carried draft
            when the path is another project's.
        """
        key = deepcopy((name, pinout, footprint, list(pad_ids) if pad_ids is not None else None))
        if not force and key == self._source_key:
            return
        self.view.cancel_interaction()
        pending = self._pending_draft()
        carry = self._carry
        if pending is None and carry is not None and carry['kept']:
            # A kept draft not yet confirmed by `set_recovery_path` has no
            # file yet: it is still the pending draft of its project.
            pending = (carry['old_baseline'], carry['draft'])
        self._carry = None
        self._recovery_path = None
        self._recoverable = None
        self._recoverable_snapshot = None
        self._stale_recoverable = None
        self.recovery_button.defaultAction().setEnabled(False)
        self._source_key = key
        self._error = ''
        # The previous layout belongs to this project only when the name is
        # the same: another project's pins must not freeze this one's wires.
        same_project = bool(self._baseline) and str(name) == self._name
        if not same_project:
            # Another component: whatever the editor held belongs elsewhere.
            self._draft_owner = None
        self._name, self._footprint = str(name), str(footprint)
        self.footprint_label.setText(f"{self.tx('Footprint associato')}: {footprint}" if footprint else '')
        source = [deepcopy(row) for row in pinout if isinstance(row, dict)]
        # One normalisation of the identities at the entry point: every
        # later comparison (pads, duplicates, selection) uses the stripped text.
        for row in source:
            row['pin'] = str(row.get('pin', '')).strip()
        source_ids = [row['pin'] for row in source]
        if len(source_ids) != len(set(source_ids)):
            self._error = self.tx('Il pinout contiene numeri duplicati: verifica la corrispondenza.')
        if pad_ids is not None:
            ids = list(dict.fromkeys(str(value).strip() for value in pad_ids if str(value).strip()))
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
        # baseline is copied, and the same id at every load
        # (`_assign_legacy_ids`), so an upstream change is not a graphics
        # conflict merely because random ids were drawn again: baseline and draft must agree on the ids, or an
        # untouched project would load dirty (Apply enabled, Export disabled)
        # and Cancel would regenerate the ids.
        self._assign_legacy_ids(source)
        graphics = self._graphics(source)
        # Wires anchored to a pin the new pinout no longer has keep its last
        # position as a fixed point. Only the layout of the SAME project can
        # tell where that pin was; at the first load, or coming from another
        # project, nothing is frozen and the anchor falls back to its stored
        # x/y, refreshed at every commit (`refresh_wire_anchors`). The layout
        # is the APPLIED baseline's: the incoming rows derive from it, while
        # an unapplied draft may have moved the pin elsewhere.
        if same_project and graphics:
            from ..generators.symbol_glyphs import freeze_pin_anchors
            previous_ends = self._pin_ends_of(self._baseline)
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
            from ..generators.symbol_glyphs import adopt_coincident_anchors
            if adopt_coincident_anchors(graphics, self._pin_ends_of(source)):
                source = self._with_graphics(source, graphics)
        self._history = []
        self._history_index = -1
        self._selected_pin = None
        self._selected_pins = set()
        self._baseline = deepcopy(source)
        self._original = deepcopy(source)
        self.title.setText(f"{self.tx('Simbolo elettrico')} · {self._name}" if name else self.tx('Simbolo elettrico'))
        if pending is None:
            self._populate(source)
            return
        old_baseline, draft = pending
        if not same_project:
            # Another component: its draft stays in its own recovery file,
            # still bound to its own (unchanged) baseline.
            self._populate(source)
            return
        rebased, conflicts = self._rebase_rows(old_baseline, draft, source)
        same_pins = [str(r.get('pin', '')) for r in draft] == [str(r.get('pin', '')) for r in source]
        # Each side may be valid alone and the merge not (two pins moved onto
        # one point, one by the draft and one by the new data): that is a
        # conflict too. A draft that was already invalid on its own (the user
        # is mid-edit) is carried as it is: the merge did not break it.
        broken = (same_pins and not conflicts and self._layout_fails(rebased)
                  and not self._layout_fails(draft))
        kept = not same_pins or conflicts or broken
        self._carry = {'old_baseline': old_baseline, 'draft': draft, 'kept': kept}
        if not kept:
            self._populate(rebased)
            if self._symbol is not None and not self._error:
                self.set_status(self.tx('Pinout aggiornato: le modifiche non applicate sono state riportate sui nuovi dati.'))
            return
        # The new data is shown as it is and the draft (its edits winning,
        # on the pins that still exist) waits for «Recupera bozza».
        self._populate(source)
        if rebased != source:
            self._stale_recoverable = rebased
            self._recoverable = deepcopy(rebased)
            self.recovery_button.defaultAction().setEnabled(True)
        if broken:
            self.set_status(self.tx('Pinout aggiornato: la bozza non applicata, riportata sui nuovi dati, darebbe '
                                    'una disposizione non valida ed è conservata, usa Recupera bozza per riprenderla.'))
        elif same_pins:
            self.set_status(self.tx('Pinout aggiornato: la bozza non applicata modifica gli stessi dati ed è '
                                    'conservata, usa Recupera bozza per riprenderla.'))
        else:
            self.set_status(self.tx('Pinout aggiornato con pin diversi: la bozza non applicata è conservata, '
                                    'usa Recupera bozza per riprendere le modifiche dei pin rimasti.'))

    def error_text(self, exc):
        """@brief The message of a validation error in the interface language.

        @param exc Any exception; a `SymbolError` of the generators is
            translated through the catalogue, anything else shows `str(exc)`.
        @return Text for the status line or a dialog.
        """
        localized = getattr(exc, 'localized', None)
        if callable(localized):
            try:
                return str(localized(self.tx))
            except Exception:  # noqa: BLE001 - never hide the error behind a translation fault
                pass
        return str(exc)

    def _pending_draft(self):
        """(applied baseline, draft rows) when the editor holds unapplied edits, else None."""
        if getattr(self, '_draft', None) is None or self.table.rowCount() != len(self._draft):
            return None
        draft = self.draft_rows()
        return (deepcopy(self._baseline), draft) if draft != self._baseline else None

    @staticmethod
    def superseded_draft_path(path, stamp):
        """@brief The file of one superseded draft, beside the recovery file.

        @param path The recovery file (`set_recovery_path`).
        @param stamp Creation stamp, nanoseconds since the epoch, written with
            20 digits so that the names sort by age.
        @return `<stem>.superseded-<stamp><suffix>` in the same folder.
        """
        path = Path(path)
        return path.with_name(f'{path.stem}.superseded-{int(stamp):020d}{path.suffix}')

    @staticmethod
    def superseded_draft_paths(path):
        """@brief The superseded drafts kept beside a recovery file, newest first.

        @param path The recovery file (`set_recovery_path`).
        @return Existing snapshot files; [] when the folder cannot be read.
        @note Each file is a normal recovery file bound to the baseline the
            draft was made on (`symbol_draft.load_draft` reads it with that
            baseline) and also carries those baseline rows (`old_baseline`),
            so the draft can be carried onto any later baseline.
        """
        path = Path(path)
        prefix = f'{path.stem}.superseded-'
        try:
            found = [p for p in path.parent.iterdir()
                     if p.name.startswith(prefix) and p.name.endswith(path.suffix) and p.is_file()]
        except OSError:
            return []
        return sorted(found, key=lambda p: p.name, reverse=True)

    @staticmethod
    def _read_snapshot(file):
        """(old baseline rows, draft rows) of a superseded snapshot, None when unusable."""
        from ..services.symbol_draft import fingerprint
        try:
            if file.stat().st_size > _SNAPSHOT_MAX_BYTES:
                return None
            data = json.loads(file.read_text(encoding='utf-8'))
            old, rows = data.get('old_baseline'), data.get('rows')
            if data.get('version') != 1:
                return None
            for value in (old, rows):
                if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
                    return None
            if data.get('baseline') != fingerprint(old):
                return None
            if [str(r.get('pin')) for r in rows] != [str(r.get('pin')) for r in old]:
                return None
            return old, rows
        except (OSError, ValueError, AttributeError, TypeError):
            return None

    def _write_snapshot(self, path, old_baseline, draft):
        """Persist a superseded draft beside `path`; return its file, or None when not written.

        An identical snapshot already on disk is reused instead of written
        again; a different one is never overwritten (one file per stamp).
        Beyond MAX_SUPERSEDED_DRAFTS the oldest files are removed, only after
        the new one is in place.
        """
        from ..fsutil import replace_file
        from ..services.symbol_draft import fingerprint
        old_baseline, draft = json.loads(json.dumps([old_baseline, draft], ensure_ascii=False))
        for existing in self.superseded_draft_paths(path):
            if self._read_snapshot(existing) == (old_baseline, draft):
                return existing
        text = json.dumps({'version': 1, 'baseline': fingerprint(old_baseline), 'rows': draft,
                           'old_baseline': old_baseline}, ensure_ascii=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.time_ns()
            while self.superseded_draft_path(path, stamp).exists():
                stamp += 1
            target = self.superseded_draft_path(path, stamp)
            fd, temp = tempfile.mkstemp(prefix='.symbol-recovery-', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                replace_file(temp, target)
            finally:
                Path(temp).unlink(missing_ok=True)
        except OSError:
            self.set_status(self.tx('Bozza non salvata: verifica lo spazio e i permessi della cartella.'))
            return None
        for stale in self.superseded_draft_paths(path)[MAX_SUPERSEDED_DRAFTS:]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
        return target

    def _offerable_snapshot(self, path):
        """(rows, file) of the newest snapshot that still changes the applied baseline, else (None, None)."""
        for file in self.superseded_draft_paths(path):
            data = self._read_snapshot(file)
            if data is None:
                continue
            rows = self._rebase_rows(data[0], data[1], self._baseline)[0]
            if rows != self._baseline:
                return rows, file
        return None, None

    @staticmethod
    def _layout_fails(rows):
        """True when `rows` carry pins whose symbol layout does not validate."""
        try:
            pins, body = symbol_inputs(rows)
            if pins:
                layout_symbol(pins, body)
        except ValueError:
            return True
        return False

    @staticmethod
    def _assign_legacy_ids(rows):
        """Give every graphics record without an id one derived from its position and content.

        Legacy projects (saved before records had ids) otherwise draw new
        random ids at every load, and a draft made on one load would differ
        from the next load in `symbol_graphics` even when nothing changed.
        Every row carries the same list, so every row gets the same ids.
        """
        for row in rows:
            records = row.get('symbol_graphics') if isinstance(row, dict) else None
            if not isinstance(records, list):
                continue
            for index, record in enumerate(records):
                if isinstance(record, dict) and not (isinstance(record.get('id'), str) and record['id']):
                    content = json.dumps([index, {k: v for k, v in record.items() if k != 'id'}],
                                         sort_keys=True, ensure_ascii=False, default=str)
                    record['id'] = 'legacy' + hashlib.sha256(content.encode('utf-8')).hexdigest()[:26]

    @staticmethod
    def _pin_ends_of(rows):
        """Inner pin ends {number: (x, y)} of the layout of `rows`; {} when it does not validate."""
        from ..generators.symbol_glyphs import pin_inner_ends
        try:
            pins, body = symbol_inputs(rows)
            return pin_inner_ends(layout_symbol(pins, body)) if pins else {}
        except ValueError:
            return {}

    @staticmethod
    def _rebase_rows(old_baseline, draft, new_baseline):
        """Carry the unapplied edits of `draft` (made on `old_baseline`) onto `new_baseline`.

        Three-way per field: a field the draft changed keeps the draft
        value, every other field takes the new baseline's. Per-pin fields
        are matched by pin number; a pin only in the new baseline keeps its
        new values and the edits of a pin that disappeared are dropped. The
        fields shared by every row (body, graphics, property positions) are
        merged once. Wires of the draft anchored to a dropped pin are frozen
        where the draft's layout had that pin.

        Returns (rows, conflicts): the draft value wins every field, and
        `conflicts` is True when some field was ALSO changed by the new
        baseline to a different value (the caller then must not apply the
        result silently).
        """
        missing = object()
        shared = ('symbol_body', 'symbol_graphics', PROPERTIES_KEY)
        old_by = {str(row.get('pin', '')): row for row in old_baseline}
        draft_by = {str(row.get('pin', '')): row for row in draft}
        result = deepcopy(new_baseline)
        conflicts = False
        for row in result:
            number = str(row.get('pin', ''))
            before_row, after_row = old_by.get(number), draft_by.get(number)
            if before_row is None or after_row is None:
                continue
            for key in set(before_row) | set(after_row):
                before, after = before_row.get(key, missing), after_row.get(key, missing)
                if key in shared or before == after:
                    continue
                incoming = row.get(key, missing)
                conflicts = conflicts or (incoming != before and incoming != after)
                if after is missing:
                    row.pop(key, None)
                else:
                    row[key] = deepcopy(after)
        first_before = old_baseline[0] if old_baseline else {}
        first_after = draft[0] if draft else {}
        first_incoming = result[0] if result else {}
        for key in shared:
            before, after = first_before.get(key, missing), first_after.get(key, missing)
            if before == after:
                continue
            incoming = first_incoming.get(key, missing)
            conflicts = conflicts or (incoming != before and incoming != after)
            if key == 'symbol_graphics' and after is not missing:
                from ..generators.symbol_glyphs import freeze_pin_anchors
                after = deepcopy(after)
                ends = SymbolTab._pin_ends_of(draft)
                kept = {str(row.get('pin', '')) for row in result}
                for number in [n for n in ends if n not in kept]:
                    freeze_pin_anchors(after, number, ends)
            for row in result:
                if after is missing:
                    row.pop(key, None)
                else:
                    row[key] = deepcopy(after)
        return result, conflicts

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
        """@brief Bind the editor to the recovery file of the current project and offer what it holds.

        @param path Recovery file of the project (`<project>/.symbol-draft.json`),
            or None to stop checkpointing.
        @note The path is the identity of the project: two projects may share
            the component name in different library roots. The owner calls
            this right after `set_data`, so a draft `set_data` carried over is
            settled here. Same project: it becomes durable (rebased: the
            recovery file is rewritten bound to the new baseline; kept: a
            superseded snapshot is written and offered). Another project:
            whatever the editor held beyond the applied data is dropped from
            the editor (it stays in its own project's recovery file) and is
            never written here. Without a carried draft, the recovery file
            matching the applied baseline is offered, else the newest
            superseded snapshot that still changes something.
        """
        path = Path(path) if path else None
        if path == self._recovery_path:
            return
        if path is None:
            self._recovery_path = None
            self._recoverable = None
            self._recoverable_snapshot = None
            self.recovery_button.defaultAction().setEnabled(False)
            return
        carry, self._carry = self._carry, None
        if self._draft_owner is not None and path != self._draft_owner:
            self._recovery_path = None
            self._recoverable = None
            self._stale_recoverable = None
            carry = None
            if self.has_pending_edits():
                self._history = []
                self._history_index = -1
                self._populate(deepcopy(self._baseline))
        self._recovery_path = path
        self._draft_owner = path
        self._recoverable = None
        self._recoverable_snapshot = None
        if carry is not None:
            if not carry['kept']:
                # Bound to the NEW baseline now: a restart finds it.
                self._checkpoint_draft()
                return
            snapshot = self._write_snapshot(path, carry['old_baseline'], carry['draft'])
            if self._stale_recoverable is not None:
                self._recoverable = deepcopy(self._stale_recoverable)
                self._recoverable_snapshot = snapshot
            self.recovery_button.defaultAction().setEnabled(self._recoverable is not None)
            return
        from ..services.symbol_draft import load_draft
        try:
            self._recoverable = load_draft(path, self._baseline)
        except OSError:
            self.set_status(self.tx('Impossibile leggere la bozza di recupero.'))
        if self._recoverable is not None:
            self.recovery_button.defaultAction().setEnabled(True)
            self.set_status(self.tx('Bozza disponibile: usa Recupera bozza per riprenderla.'))
            return
        self._recoverable, self._recoverable_snapshot = self._offerable_snapshot(path)
        self.recovery_button.defaultAction().setEnabled(self._recoverable is not None)
        if self._recoverable is not None:
            self.set_status(self.tx('Bozza superata da un pinout successivo disponibile: usa Recupera bozza '
                                    'per riprenderla sui dati attuali.'))

    def recover_draft(self):
        """@brief Restore recovery rows as an editable, unapplied draft.

        @note A superseded snapshot is removed only once the recovery file
            holds the recovered draft, so a failed write never loses it.
        """
        if self._recoverable is not None:
            rows = deepcopy(self._recoverable)
            snapshot = self._recoverable_snapshot
            self._recoverable = None
            self._recoverable_snapshot = None
            self._stale_recoverable = None
            self.recovery_button.defaultAction().setEnabled(False)
            self._populate(rows)
            if snapshot is not None and self._recovery_path is not None:
                from ..services.symbol_draft import load_draft
                try:
                    if load_draft(self._recovery_path, self._baseline) == self.draft_rows():
                        snapshot.unlink(missing_ok=True)
                except OSError:
                    pass

    def _checkpoint_draft(self):
        if self._recovery_path is None or self._applying or self._gesture_before is not None:
            return
        from ..services.symbol_draft import save_draft
        try:
            if self.has_pending_edits():
                save_draft(self._recovery_path, self._baseline, self.draft_rows())
                self._recoverable = None
                self._recoverable_snapshot = None
                self._stale_recoverable = None
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
    def _property_positions(rows, symbol):
        """Positions of Reference and Value, mm: the rows' choice, else the file's defaults."""
        positions = default_property_positions(symbol)
        try:
            positions.update(symbol_property_positions(rows))
        except ValueError:
            pass
        return positions

    @staticmethod
    def _with_properties(rows, positions):
        """Rows with `symbol_properties` set to the same mapping on every row (like `symbol_body`)."""
        rows = deepcopy(rows)
        mapping = {key: [round(x, 4), round(y, 4)] for key, (x, y) in positions.items()}
        for row in rows:
            row[PROPERTIES_KEY] = deepcopy(mapping)
        return rows

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

    def _tool_chosen(self, key):
        """A palette tool click: the wire or one of the shape tools."""
        if key == 'wire':
            self.start_wire()
        else:
            self.start_shape(key)

    def _release_tools(self):
        """Uncheck every tool and drop any shape or wire in progress (no commit)."""
        from ..generators.symbol_glyphs import SHAPE_TOOLS
        self.wire_button.defaultAction().setChecked(False)
        self.palette.set_tool_checked('wire', False)
        for key, _label, _kind, _count in SHAPE_TOOLS:
            self.palette.set_tool_checked(key, False)
        self._wire_points = []
        self._shape_tool = None
        self._shape_points = []
        self._wire_cursor_item = None

    def start_wire(self):
        """@brief Arm the wire tool: every click adds a point until the wire is closed."""
        if self._symbol is None:
            self.wire_button.defaultAction().setChecked(False)
            return
        self.view.cancel_interaction()
        self.view.placing = False
        self._release_tools()
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
        x, y = point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM
        if self._shape_tool is not None:
            self.add_shape_point(x, y)
        else:
            self.add_wire_point(x, y, free)

    # ── Shape tools (user, 2026-10-12) ──────────────────────────────────────

    def start_shape(self, key):
        """@brief Arm a shape tool (SHAPE_TOOLS): clicks on the grid draw the shape.

        @param key Tool key: line, rectangle, circle, arc or bezier.
        @return True when the tool was armed (a symbol is loaded, key known).
        @note The line is closed by the user (double click, Enter, right
            button) like a wire; the other shapes close themselves at their
            last click. Shape points snap to the grid only: they are
            drawings, not connections, so they do not anchor to pins.
        """
        from ..generators.symbol_glyphs import shape_tool
        tool = shape_tool(key)
        if self._symbol is None or tool is None:
            self.palette.set_tool_checked(key, False)
            return False
        self.view.cancel_interaction()
        self.view.placing = False
        self._release_tools()
        self.view.wiring = True
        self._shape_tool = key
        self._shape_points = []
        self.palette.set_tool_checked(key, True)
        self.view.setFocus()
        self.set_status(self.tx(TOOL_HINTS[key]))
        self._draw(self._symbol)
        return True

    def shape_tool(self):
        """@brief The key of the shape tool in use, None when none is armed."""
        return self._shape_tool

    def _grid_point(self, x, y):
        """The drawing-grid point nearest to (x, y), symbol mm; 0.01 mm steps with snapping off."""
        step = self._grid_mm if self._snap else 0.01
        return tuple(round(round(v / step) * step, 4) for v in (x, y))

    def set_drawing_grid(self, step):
        """@brief Set the grid step (mm) of the drawing tools and shape handles.

        @param step One of DRAWING_GRIDS_MM; other values are ignored.
        """
        if step not in DRAWING_GRIDS_MM:
            return
        self._grid_mm = float(step)
        self.view.set_drawing_grid(self._grid_mm)
        if self.grid_combo.currentData() != step:
            self.grid_combo.setCurrentIndex(self.grid_combo.findData(step))

    def drawing_grid(self):
        """@brief The current drawing grid step in mm."""
        return self._grid_mm

    def set_snap(self, enabled):
        """@brief Turn the snapping of shape points to the drawing grid on or off."""
        self._snap = bool(enabled)
        action = self.snap_button.defaultAction()
        if action.isChecked() != self._snap:
            blocked = action.blockSignals(True)
            action.setChecked(self._snap)
            action.blockSignals(blocked)

    def add_shape_point(self, x, y):
        """@brief Add a clicked point (symbol mm, Y up) to the shape being drawn.

        @param x Symbol X in mm.
        @param y Symbol Y in mm.
        @return True when the point was taken; False with no shape tool armed
            or when it repeats the previous point.
        @note When the tool needs a fixed number of points, the last one
            closes the shape (`finish_shape`).
        """
        from ..generators.symbol_glyphs import shape_tool
        if self._shape_tool is None or self._symbol is None:
            return False
        point = self._grid_point(x, y)
        if self._shape_points and point == self._shape_points[-1]:
            return False
        self._shape_points.append(point)
        count = shape_tool(self._shape_tool)[3]
        if count and len(self._shape_points) >= count:
            self.finish_shape(True)
        elif not count and len(self._shape_points) >= 4 and point == self._shape_points[0]:
            # A line back onto its first point is a closed polygon
            # (user, 2026-10-12): three or more sides, closed by the click.
            self.finish_shape(True)
        else:
            self._draw(self._symbol)
        return True

    def finish_shape(self, commit=True):
        """@brief Close the shape being drawn and insert it as a graphic element.

        @param commit False abandons the points.
        @return True when a shape was added; False when abandoned, when the
            points do not make the shape (see `shape_instance`), or when the
            geometry was refused.
        @note A tool drawn by points stays armed after a shape is inserted
            so several shapes can be drawn in a row (as in KiCad); Esc or
            another tool releases it, and the status line says so. A tool
            that inserts through a dialog (the text) is one-shot: it is
            released after the insertion, because while it is armed every
            click is a new element and the one just placed cannot be
            dragged (user, 2026-10-12).
        """
        from ..generators.symbol_glyphs import shape_instance
        key, points = self._shape_tool, list(self._shape_points)
        self._shape_points = []
        self._wire_cursor_item = None
        one_shot = False
        if not commit or key is None or self._symbol is None:
            self.view.wiring = False
            self._release_tools()
            self.set_status('')
            self._draw(self._symbol)
            return False
        if key == 'text':
            answer = self._text_prompt()
            if answer is None:
                self.view.wiring = True
                self._draw(self._symbol)
                return False
            record = shape_instance(key, points, text=answer[0], size=answer[1], justify=answer[2])
            one_shot = True
            if record is None:
                self.set_status(self.tx('Testo non valido: serve una riga non vuota con dimensione maggiore di zero.'))
                self.view.wiring = True
                self._draw(self._symbol)
                return False
        else:
            record = shape_instance(key, points)
        if record is None:
            self.set_status(self.tx('Forma non valida: i punti non la definiscono (serve un’estensione diversa da zero).'))
            self.view.wiring = True
            self._draw(self._symbol)
            return False
        rows = self._free_rows(self.draft_rows(), self._symbol)
        graphics = self._graphics(rows) + [record]
        if not self._accept_geometry(self._with_graphics(rows, graphics)):
            self.view.wiring = True
            return False
        if one_shot:
            self.view.wiring = False
            self._release_tools()
            self.set_status(self.tx(
                'Testo inserito: trascinalo dal corpo o dalla maniglia; «Modifica testo…» nel menu per cambiarlo.'))
        else:
            self.view.wiring = True
            self.set_status(self.tx('Forma inserita.') + ' ' + self.tx(TOOL_HINTS[key]) + ' '
                            + self.tx('Esc per lasciare lo strumento.'))
        self._select_graphic(len(graphics) - 1)
        return True

    def _text_prompt(self, text='', size=1.27, justify='center'):
        """@brief Ask the user a text, its size and justification; None when cancelled.

        @note Tests replace this method to answer without a dialog.
        """
        dialog = TextDialog(self.tx, text, size, justify, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.values()
        finally:
            dialog.deleteLater()

    def edit_text(self, index):
        """@brief Open the text dialog on a text element and apply the answer.

        @return True when the element was changed.
        """
        graphics = self._graphics()
        if not 0 <= index < len(graphics):
            return False
        primitives = graphics[index].get('primitives') or [{}]
        if primitives[0].get('kind') != 'text':
            return False
        answer = self._text_prompt(str(primitives[0].get('text', '')), float(primitives[0].get('size', 1.27)),
                                   str(primitives[0].get('justify', 'center')))
        if answer is None:
            return False
        if not self._edit_graphic(index, 'text', answer):
            self.set_status(self.tx('Testo non valido: serve una riga non vuota con dimensione maggiore di zero.'))
            return False
        return True

    def _shape_outline(self, points):
        """The points to draw, in symbol mm, for the shape tool in use through
        `points` (clicked, plus possibly the cursor): the partial shape when
        fewer points than needed are known."""
        from ..generators.symbol_glyphs import arc_points, bezier_points
        kind = self._shape_tool
        points = list(points)
        if len(points) < 2:
            return points
        if kind == 'rectangle':
            (x0, y0), (x1, y1) = points[:2]
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        if kind == 'circle':
            (cx, cy), (rx, ry) = points[:2]
            r = math.hypot(rx - cx, ry - cy)
            return [(cx + r * math.cos(2 * math.pi * i / 32), cy + r * math.sin(2 * math.pi * i / 32))
                    for i in range(33)]
        if kind == 'arc' and len(points) >= 3:
            start, end, mid = points[:3]
            return arc_points(start, mid, end)
        if kind == 'bezier' and len(points) >= 4:
            return bezier_points(*points[:4])
        return points

    def finish_wire(self, commit=True):
        """@brief Close the wire being drawn: with two or more points it becomes a graphic element.

        @param commit False abandons the points.
        @return True when a wire was added.
        @note With a shape tool armed this closes the shape instead
            (`finish_shape`): the view sends one signal for both.
        """
        if self._shape_tool is not None:
            return self.finish_shape(commit)
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

    def _select_graphic(self, index, additive=False):
        """@brief Select one element (the primary); `additive` keeps the others selected."""
        self._selected_graphic = index
        if index is None:
            self._selected_graphics = set()
        else:
            self._selected_graphics = (self._selected_graphics | {index}) if additive else {index}
            self._selected_pins = set()
            self._selected_pin = None
        self._draw(self._symbol)

    def selected_graphics(self):
        """@brief The indices of every selected element, ascending (the primary included)."""
        count = len(self._graphics())
        chosen = {i for i in self._selected_graphics if 0 <= i < count}
        if self._selected_graphic is not None and 0 <= self._selected_graphic < count:
            chosen.add(self._selected_graphic)
        return sorted(chosen)

    def _toggle_graphic(self, index):
        """Ctrl+click on an element: add it to or remove it from the selection."""
        chosen = set(self.selected_graphics())
        chosen.symmetric_difference_update({index})
        self._selected_graphics = chosen
        self._selected_graphic = index if index in chosen else (max(chosen) if chosen else None)
        if chosen:
            self._selected_pins = set()
            self._selected_pin = None
        self._draw(self._symbol)

    def _edit_selected_graphics(self, operation, value=None):
        """@brief Apply rotate/mirror/delete/fill/style/width to every selected element.

        @return True when at least one element changed. Each element turns
            about its own origin (a group turn about a common centre would
            move glyph terminals off their pins).
        """
        chosen = self.selected_graphics()
        if not chosen:
            return False
        if operation == 'delete':
            # Highest first: lower indices stay valid while deleting.
            done = False
            for index in reversed(chosen):
                done = self._edit_graphic(index, 'delete') or done
            self._select_graphic(None)
            return done
        done = False
        for index in chosen:
            done = self._edit_graphic(index, operation, value) or done
        if done:
            self._selected_graphics = set(chosen)
            self._draw(self._symbol)
        return done

    # ── Clipboard (user, 2026-10-12) ────────────────────────────────────────

    def copy_selected(self):
        """@brief Copy the selected elements to the tab's clipboard.

        @return Number of elements copied; 0 leaves the clipboard as it was.
            A wire is copied with its anchors frozen: the copy is a drawing,
            not the same connection.
        """
        from ..generators.symbol_glyphs import detached_copy
        graphics = self._graphics()
        chosen = self.selected_graphics()
        if not chosen:
            return 0
        pin_ends = self._pin_ends()
        self._clipboard = [detached_copy(graphics[i], graphics, pin_ends) for i in chosen]
        self.set_status(self.tx('{count} elementi copiati.').format(count=len(chosen)))
        return len(chosen)

    def paste(self, offset=None):
        """@brief Paste the clipboard, moved by `offset` (default one pin grid step right and down).

        @param offset (dx, dy) in mm applied to every pasted element.
        @return True when something was pasted; the pasted elements become the selection.
        """
        from ..generators.symbol_glyphs import detached_copy
        if not self._clipboard or self._symbol is None or self._gesture_before is not None:
            return False
        dx, dy = (GRID_MM, -GRID_MM) if offset is None else (float(offset[0]), float(offset[1]))
        rows = self._free_rows(self.draft_rows(), self._symbol)
        graphics = self._graphics(rows)
        first = len(graphics)
        for record in self._clipboard:
            copy = detached_copy(record)
            copy['x'] = round(float(copy.get('x', 0.0)) + dx, 4)
            copy['y'] = round(float(copy.get('y', 0.0)) + dy, 4)
            graphics.append(copy)
        if not self._accept_geometry(self._with_graphics(rows, graphics)):
            return False
        # The next paste lands one step further, as in KiCad.
        for record in self._clipboard:
            record['x'] = round(float(record.get('x', 0.0)) + dx, 4)
            record['y'] = round(float(record.get('y', 0.0)) + dy, 4)
        self._selected_graphics = set(range(first, len(graphics)))
        self._select_graphic(len(graphics) - 1, additive=True)
        self.set_status(self.tx('{count} elementi incollati.').format(count=len(graphics) - first))
        return True

    def duplicate_selected(self):
        """@brief Duplicate the selected elements one grid step away (Ctrl+D); True when done."""
        kept = list(self._clipboard)
        if not self.copy_selected():
            return False
        done = self.paste()
        self._clipboard = kept
        return done

    def _edit_graphic(self, index, operation, value=None):
        """@brief Turn, mirror, move or delete one placed glyph; an unknown index does nothing.

        @return True when the draft was updated; False for an unknown index or
            operation, and for 'rotate'/'mirror' on a wire: its free vertices
            would turn about the wire's origin while the anchored ends stayed
            on their pins and terminals, destroying the shape.
        @note 'fill' (value: none/outline/background), 'style' (a KiCad stroke
            type) and 'width' (value: mm) restyle every primitive of the
            element (`set_primitive_style`); 'text' (value: (text, size mm,
            justify)) rewrites a text element (`set_text`).
        """
        from ..generators.symbol_glyphs import is_wire, set_primitive_style, set_text
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
        elif operation == 'fill':
            if not set_primitive_style(instance, fill=value):
                return False
        elif operation == 'width':
            if not set_primitive_style(instance, width=value):
                return False
        elif operation == 'style':
            if not set_primitive_style(instance, style=value):
                return False
        elif operation == 'text':
            if not set_text(instance, *value):
                return False
        elif operation == 'delete':
            # Wires attached to the element keep their ends where they are.
            from ..generators.symbol_glyphs import freeze_wire_anchors
            freeze_wire_anchors(graphics, str(instance.get('id', '')), self._pin_ends())
            del graphics[index]
            self._selected_graphic = None
        else:
            return False
        return self._accept_geometry(self._with_graphics(rows, graphics))

    def move_shape_point(self, index, position, x, y):
        """@brief Move one defining point of a shape (handle drag), snapped to the drawing grid.

        @param index Element index.
        @param position Handle index (`shape_handles`).
        @param x Symbol X in mm.
        @param y Symbol Y in mm.
        @return True when the draft was updated (also when nothing moved);
            False for a non-shape or a degenerate result.
        @note During a drag the edit is rebuilt from the gesture's starting
            rows, like every other gesture, so it is one undo step.
        """
        from ..generators.symbol_glyphs import is_shape, set_shape_point
        if self._symbol is None:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        rows = self._free_rows(source, symbol)
        graphics = self._graphics(rows)
        if not 0 <= index < len(graphics) or not is_shape(graphics[index]):
            return False
        sx, sy = self._grid_point(float(x), float(y))
        if not set_shape_point(graphics[index], position, sx, sy):
            return False
        return self._accept_geometry(self._with_graphics(rows, graphics))

    def set_element_geometry(self, index, x, y, rotation=None, mirror=None,
                             width=None, height=None, radius=None):
        """@brief Set the placement of an element and, for a shape, its size, as one edit.

        @param index Element index.
        @param x Origin X in mm: a typed value, taken as is for a shape
            (rounded to 0.0001 mm) and snapped to the pin grid for a glyph
            or a wire, whose terminals and anchors must meet the pins.
        @param y Origin Y in mm.
        @param rotation 0/90/180/270, None keeps (ignored for wires).
        @param mirror True/False, None keeps (ignored for wires).
        @param width Bounding width of a shape, mm; None keeps.
        @param height Bounding height of a shape, mm; None keeps.
        @param radius Radius of a circle shape, mm; None keeps.
        @return True when the draft was updated.
        """
        from ..generators.symbol_glyphs import is_shape, is_wire, scale_shape
        if self._symbol is None or self._gesture_before is not None:
            return False
        rows = self._free_rows(self.draft_rows(), self._symbol)
        graphics = self._graphics(rows)
        if index is None or not 0 <= index < len(graphics):
            return False
        instance = graphics[index]
        if is_shape(instance):
            instance['x'], instance['y'] = round(float(x), 4), round(float(y), 4)
            if any(v is not None for v in (width, height, radius)) \
                    and not scale_shape(instance, width=width, height=height, radius=radius):
                return False
        else:
            instance['x'], instance['y'] = (round(round(float(v) / GRID_MM) * GRID_MM, 4) for v in (x, y))
        if not is_wire(instance):
            if rotation is not None:
                if int(rotation) % 90:
                    return False
                instance['rotation'] = int(rotation) % 360
            if mirror is not None:
                instance['mirror'] = bool(mirror)
        return self._accept_geometry(self._with_graphics(rows, graphics))

    def _edit_element_properties(self, index):
        """Dialog with X, Y, rotation, mirror and, for a shape, its size."""
        from ..generators.symbol_glyphs import is_shape, is_wire, instance_bounds
        graphics = self._graphics()
        if not 0 <= index < len(graphics):
            return
        instance = graphics[index]
        shape, wire = is_shape(instance), is_wire(instance)
        kind = (instance.get('primitives') or [{}])[0].get('kind', '')
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tx('Proprietà elemento…').rstrip('…'))
        form = QFormLayout(dialog)
        fields = {}
        for key, caption, value in [('x', 'X (mm)', instance.get('x', 0.0)), ('y', 'Y (mm)', instance.get('y', 0.0))]:
            field = QDoubleSpinBox()
            field.setDecimals(3)
            field.setRange(-998.22, 998.22)
            field.setSingleStep(self._grid_mm if shape else GRID_MM)
            field.setValue(float(value))
            form.addRow(caption, field)
            fields[key] = field
        rotation = QComboBox()
        for angle in (0, 90, 180, 270):
            rotation.addItem(str(angle) + '\u00b0', angle)
        rotation.setCurrentIndex(rotation.findData(int(instance.get('rotation', 0)) % 360))
        rotation.setEnabled(not wire)
        form.addRow(self.tx('Rotazione'), rotation)
        mirror = QCheckBox()
        mirror.setChecked(bool(instance.get('mirror', False)))
        mirror.setEnabled(not wire)
        form.addRow(self.tx('Specchia'), mirror)
        if shape and kind == 'circle':
            field = QDoubleSpinBox()
            field.setDecimals(3)
            field.setRange(0.01, 500)
            field.setSingleStep(self._grid_mm)
            field.setValue(float((instance.get('primitives') or [{}])[0].get('radius', 0.0)))
            form.addRow(self.tx('Raggio (mm)'), field)
            fields['radius'] = field
        elif shape and kind != 'text':
            # Sizes are those of the shape's own frame (before its turn and
            # mirror): what `scale_shape` changes.
            bounds = instance_bounds(dict(instance, rotation=0, mirror=False)) or (0, 0, 0, 0)
            for key, caption, value in [('width', 'Larghezza (mm)', bounds[2] - bounds[0]),
                                        ('height', 'Altezza (mm)', bounds[3] - bounds[1])]:
                field = QDoubleSpinBox()
                field.setDecimals(3)
                field.setRange(0.0, 1000)
                field.setSingleStep(self._grid_mm)
                field.setValue(float(value))
                field.setEnabled(value > 0)
                form.addRow(self.tx(caption), field)
                fields[key] = field
        message = QLabel('')
        message.setWordWrap(True)
        form.addRow(message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tx('Annulla'))

        def accept():
            sizes = {key: fields[key].value() for key in ('width', 'height', 'radius')
                     if key in fields and fields[key].isEnabled()}
            # Only a changed size is applied: a bounds value typed back
            # unchanged must not rescale the shape by rounding.
            if 'width' in sizes and 'height' in sizes:
                bounds = instance_bounds(dict(instance, rotation=0, mirror=False)) or (0, 0, 0, 0)
                if abs(sizes['width'] - (bounds[2] - bounds[0])) < 1e-6:
                    sizes.pop('width')
                if abs(sizes['height'] - (bounds[3] - bounds[1])) < 1e-6:
                    sizes.pop('height')
            if self.set_element_geometry(index, fields['x'].value(), fields['y'].value(),
                                         rotation.currentData(), mirror.isChecked(), **sizes):
                dialog.accept()
            else:
                message.setText(self.tx('Valori non validi: dimensioni maggiori di zero e rotazione a passi di 90 gradi.'))
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

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
            self._edit_selected_graphics('delete')

    def _graphic_context_menu(self, index, global_pos):
        """@brief Rotate, mirror, delete a placed glyph, or fit the body to it."""
        self._select_graphic(index)
        graphics = self._graphics()
        if not 0 <= index < len(graphics):
            return
        menu = QMenu(self)
        group = self.selected_graphics()
        several = len(group) > 1 and index in group
        title = menu.addAction(self.tx('{count} elementi selezionati').format(count=len(group)) if several
                               else self.tx(str(graphics[index].get('label', ''))))
        title.setEnabled(False)
        menu.addSeparator()
        menu.setToolTipsVisible(True)
        from ..generators.symbol_glyphs import catalogue, is_wire
        wire = is_wire(graphics[index])
        # A wire is reshaped by dragging its vertices: turning or mirroring it
        # would detach its shape from the anchored ends (see `_edit_graphic`).
        entries = [] if wire and not several else [
            ('rotate-ccw.svg', 'Ruota di 90 gradi (antiorario)', 'rotate', 90),
            ('rotate-cw.svg', 'Ruota di 90 gradi (orario)', 'rotate', -90),
            ('format-painter.svg', 'Specchia', 'mirror', None),
        ]
        entries.append(('delete.svg', 'Elimina elemento', 'delete', None))
        for asset, label, operation, value in entries:
            action = menu.addAction(QIcon(str(Path(__file__).parent / 'assets' / asset)), self.tx(label))
            action.setIconVisibleInMenu(True)
            if several:
                action.triggered.connect(lambda checked=False, op=operation, v=value: self._edit_selected_graphics(op, v))
            else:
                action.triggered.connect(lambda checked=False, op=operation, v=value: self._edit_graphic(index, op, v))
        copy_action = menu.addAction(self.tx('Copia (Ctrl+C)'))
        copy_action.triggered.connect(lambda checked=False: self.copy_selected())
        duplicate_action = menu.addAction(self.tx('Duplica (Ctrl+D)'))
        duplicate_action.triggered.connect(lambda checked=False: self.duplicate_selected())
        if several:
            try:
                menu.exec(global_pos)
            finally:
                menu.deleteLater()
            return
        properties_action = menu.addAction(self.tx('Proprietà elemento…'))
        properties_action.triggered.connect(lambda checked=False: self._edit_element_properties(index))
        primitives = graphics[index].get('primitives') or [{}]
        is_text = primitives[0].get('kind') == 'text'
        if is_text:
            menu.addSeparator()
            text_action = menu.addAction(self.tx('Modifica testo…'))
            text_action.triggered.connect(lambda checked=False: self.edit_text(index))
        elif not wire:
            # Stroke width, fill (the three KiCad fill modes) and stroke type
            # for shapes and glyphs (user, 2026-10-12: properties of the drawn
            # shapes). A wire is a connection: it keeps the menu of deletion only.
            menu.addSeparator()
            current_fill = str(primitives[0].get('fill', 'none'))
            fill_menu = menu.addMenu(self.tx('Riempimento'))
            for mode, label in FILL_LABELS:
                action = fill_menu.addAction(self.tx(label))
                action.setCheckable(True)
                action.setChecked(mode == current_fill)
                action.triggered.connect(lambda checked=False, m=mode: self._edit_graphic(index, 'fill', m))
            current_style = str(primitives[0].get('style', 'default'))
            style_menu = menu.addMenu(self.tx('Tipo di linea'))
            for mode, label in STYLE_LABELS:
                action = style_menu.addAction(self.tx(label))
                action.setCheckable(True)
                action.setChecked(mode == current_style)
                action.triggered.connect(lambda checked=False, m=mode: self._edit_graphic(index, 'style', m))
            width_action = menu.addAction(self.tx('Spessore del tratto…'))
            width_action.triggered.connect(lambda checked=False: self._ask_stroke_width(index))
        glyph = None if wire else next((g for g in catalogue() if g.key == graphics[index].get('key')), None)
        if glyph is not None and glyph.body_hint is not None:
            fit = menu.addAction(self.tx('Adatta il rettangolo del corpo all’elemento'))
            fit.triggered.connect(lambda checked=False: self.fit_body_to_graphic(index))
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

    def _ask_stroke_width(self, index):
        """Ask the stroke width in mm for one element and apply it (0 = KiCad default)."""
        from PyQt6.QtWidgets import QInputDialog
        graphics = self._graphics()
        if not 0 <= index < len(graphics):
            return
        primitives = graphics[index].get('primitives') or [{}]
        current = float(primitives[0].get('width', 0.0) or 0.0)
        value, accepted = QInputDialog.getDouble(
            self, self.tx('Spessore del tratto…'), self.tx('Spessore del tratto in mm (0 = predefinito di KiCad)'),
            current, 0.0, 5.0, 3)
        if accepted:
            self._edit_graphic(index, 'width', value)

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

    def set_reference(self, prefix):
        """@brief Set the designator prefix drawn as the Reference text («U?»).

        @param prefix Letters of the designator; blank falls back to «U».
        @note Only the drawing changes: the prefix itself lives in the
            project metadata and reaches the file through `SymbolMetadata`.
        """
        prefix = str(prefix or '').strip() or 'U'
        if prefix != self._reference:
            self._reference = prefix
            if self._symbol is not None:
                self._draw(self._symbol)

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
            self.status.setText(self.error_text(exc))
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
            row.pop(PROPERTIES_KEY, None)
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
            self._selected_graphics = set()
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
        # Elements whose whole bounds lie in the band are selected too
        # (user, 2026-10-12: multi-selection of drawings), after the pins:
        # a band with pins selects pins, as before.
        from ..generators.symbol_glyphs import instance_bounds
        graphics = self._graphics()
        pin_ends = self._pin_ends()
        elements = set(self.selected_graphics()) if additive else set()
        for index, instance in enumerate(graphics):
            bounds = instance_bounds(instance, graphics, pin_ends)
            if bounds is None:
                continue
            x0, y0, x1, y1 = bounds
            scene_rect = QRectF(x0 * SCENE_UNITS_PER_MM, -y1 * SCENE_UNITS_PER_MM,
                                (x1 - x0) * SCENE_UNITS_PER_MM, (y1 - y0) * SCENE_UNITS_PER_MM)
            if rect.contains(scene_rect):
                elements.add(index)
        if chosen:
            self._select_pins(chosen)
            return
        self._selected_graphics = elements
        # `_select_graphic` also drops the pin selection, so a later band
        # with Ctrl held adds elements instead of reselecting the pins.
        self._select_graphic(max(elements) if elements else None, additive=True)

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

    @staticmethod
    def _outline_path(points, scale):
        """A scene path through `points` (symbol mm, Y up); empty for no points."""
        from PyQt6.QtGui import QPainterPath
        path = QPainterPath()
        points = list(points)
        if not points:
            return path
        path.moveTo(points[0][0] * scale, -points[0][1] * scale)
        for x, y in points[1:]:
            path.lineTo(x * scale, -y * scale)
        return path

    def _on_pointer_left(self):
        """The pointer left the drawing: nothing is hovered until it comes back."""
        self._pointer_inside = False
        self._update_handle_visibility(None)

    def _update_readout(self, point=None):
        if point is not None:
            self._cursor_position = point
            self._pointer_inside = True
            self._update_handle_visibility(point)
            item = self._wire_cursor_item
            if item is not None and self._shape_tool is not None and self._shape_points and self.view.wiring:
                cursor = self._grid_point(point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM)
                item.setPath(self._outline_path(self._shape_outline(self._shape_points + [cursor]),
                                                SCENE_UNITS_PER_MM))
            elif item is not None and self._wire_points and self.view.wiring:
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
            if index not in self.selected_graphics():
                self._select_graphic(index)
            else:
                self._select_graphic(index, additive=True)
            # Every selected element moves with the dragged one.
            self._gesture_group_origins = {i: (graphics[i]['x'], graphics[i]['y'])
                                           for i in self.selected_graphics() if 0 <= i < len(graphics)}
        elif target[0] == 'vertex':
            self._select_graphic(target[1])
        elif target[0] == 'shape_point':
            self._select_graphic(target[1])
        elif target[0] == 'property':
            self._gesture_property_origin = self._property_positions(
                self._gesture_before, self._gesture_layout).get(target[1])

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

    def move_property(self, key, x, y):
        """@brief Put a visible property (Reference or Value) at a point, snapped to the pin grid.

        @param key One of `PROPERTY_KEYS`.
        @param x Target x, mm in symbol coordinates.
        @param y Target y, mm in symbol coordinates (y up).
        @return True when the draft was updated (or already there), False on an unknown key.
        @note The chosen position is written on every row (`symbol_properties`)
            and reaches the file through `symbol_property_positions`; the
            other property keeps its position, chosen or default.
        """
        if self._symbol is None or key not in PROPERTY_KEYS:
            return False
        source = self._gesture_before if self._gesture_before is not None else self.draft_rows()
        symbol = self._gesture_layout if self._gesture_before is not None else self._symbol
        snapped = tuple(round(round(v / GRID_MM) * GRID_MM, 4) for v in (x, y))
        try:
            chosen = symbol_property_positions(source)
        except ValueError:
            chosen = {}
        if chosen.get(key) == snapped:
            self._draft = deepcopy(source)
            self._changed()
            return True
        positions = self._property_positions(source, symbol)
        positions[key] = snapped
        return self._accept_geometry(self._with_properties(source, positions))

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

    def set_pin_properties(self, number, length=None, text_size=None, hidden=None):
        """@brief Set the length, text size and visibility of one pin as one edit.

        @param number Pin number.
        @param length Pin length in mm (multiple of 1.27 up to 12.7); None keeps.
        @param text_size Font size of name and number, mm (0.5 to 5.08); None keeps.
        @param hidden True hides the pin in the schematic; None keeps.
        @return True when the draft was updated (also when nothing changed);
            False when the layout refuses the values (a side-placed pin whose
            outer end would leave the 2.54 mm grid, for example).
        """
        from ..generators.symbol import PIN_GRID_MM, TEXT_SIZE
        if self._symbol is None or self._gesture_before is not None:
            return False
        rows = self._free_rows(self.draft_rows(), self._symbol)
        row = next((r for r in rows if str(r['pin']) == str(number)), None)
        if row is None:
            return False
        if length is not None:
            if math.isclose(float(length), PIN_GRID_MM, abs_tol=1e-7):
                row.pop('symbol_pin_length', None)
            else:
                row['symbol_pin_length'] = round(float(length), 4)
        if text_size is not None:
            if math.isclose(float(text_size), TEXT_SIZE, abs_tol=1e-7):
                row.pop('symbol_pin_text_size', None)
            else:
                row['symbol_pin_text_size'] = round(float(text_size), 4)
        if hidden is not None:
            if hidden:
                row['symbol_pin_hidden'] = True
            else:
                row.pop('symbol_pin_hidden', None)
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
        # Length, text size and visibility (user, 2026-10-12): the KiCad pin
        # properties the KLC checks (2.54 mm, 1.27 mm, visible power pins).
        from ..generators.symbol import PIN_LENGTH_STEP_MM, PIN_LENGTH_MAX_MM, PIN_TEXT_SIZE_RANGE_MM
        from ..generators.symbol import pin_length_of, pin_text_size_of
        length = QComboBox()
        steps = int(round(PIN_LENGTH_MAX_MM / PIN_LENGTH_STEP_MM))
        for i in range(1, steps + 1):
            value = round(i * PIN_LENGTH_STEP_MM, 4)
            length.addItem(f'{value:g} mm', value)
        length.setCurrentIndex(max(0, length.findData(round(pin_length_of(pin.pin, self._symbol.pin_length), 4))))
        form.addRow(self.tx('Lunghezza'), length)
        text_size = QDoubleSpinBox()
        text_size.setDecimals(3)
        text_size.setRange(*PIN_TEXT_SIZE_RANGE_MM)
        text_size.setSingleStep(0.127)
        text_size.setSuffix(' mm')
        text_size.setValue(pin_text_size_of(pin.pin))
        form.addRow(self.tx('Dimensione testi'), text_size)
        hidden = QCheckBox()
        hidden.setChecked(bool(pin.pin.hidden))
        form.addRow(self.tx('Pin nascosto'), hidden)
        message = QLabel(self.tx('Le coordinate si agganciano alla griglia da 2.54 mm.'))
        message.setWordWrap(True)
        form.addRow(message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tx('Annulla'))
        def accept():
            if self.set_pin_properties(pin.pin.number, length.currentData(), text_size.value(), hidden.isChecked()) \
                    and self.set_pin_geometry(pin.pin.number, coordinates[0].value(), coordinates[1].value(),
                                              orientation.currentData()):
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
        menu.addSeparator()
        properties = menu.addAction(QIcon(str(Path(__file__).parent / 'assets' / 'sliders.svg')),
                                    self.tx('Proprietà pin…'))
        properties.setIconVisibleInMenu(True)
        properties.setToolTip(self.tx('Posizione, orientamento, lunghezza, dimensione dei testi e visibilità del pin'))
        properties.triggered.connect(lambda checked=False: self._edit_selected_pin())
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
            from ..generators.symbol_glyphs import is_shape
            delta = point - self._gesture_start
            rows = self._free_rows(self._gesture_before, symbol)
            graphics = self._graphics(rows)
            origins = getattr(self, '_gesture_group_origins', None) or {target[1]: origin}
            # Every member moves by the same delta. Shapes alone: the DELTA
            # is snapped on the drawing grid, so an odd-sized shape keeps its
            # corners on it. As soon as a glyph or a wire moves along, the
            # reference member's resulting ABSOLUTE position is snapped on
            # the pin grid (the leader, or the first glyph or wire when a
            # shape leads): its terminals meet the pins again even when it
            # had been left off the grid, and the others follow by the same
            # delta.
            raw = (delta.x() / SCENE_UNITS_PER_MM, -delta.y() / SCENE_UNITS_PER_MM)
            members = [index for index in origins if 0 <= index < len(graphics)]
            pinned = [index for index in members if not is_shape(graphics[index])]
            if members and not pinned:
                step = self._grid_mm if self._snap else 0.01
                dx, dy = (round(round(v / step) * step, 4) for v in raw)
            else:
                reference = target[1] if target[1] in pinned else (pinned[0] if pinned else target[1])
                rx, ry = origins.get(reference, origin)
                dx, dy = (round(round((o + v) / GRID_MM) * GRID_MM - o, 4)
                          for o, v in ((rx, raw[0]), (ry, raw[1])))
            for index, (ox, oy) in origins.items():
                if 0 <= index < len(graphics):
                    graphics[index]['x'], graphics[index]['y'] = round(ox + dx, 4), round(oy + dy, 4)
            self._accept_geometry(self._with_graphics(rows, graphics))
            return
        if target[0] == 'vertex':
            self.move_wire_vertex(target[1], target[2], point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM)
            return
        if target[0] == 'shape_point':
            self.move_shape_point(target[1], target[2], point.x() / SCENE_UNITS_PER_MM, -point.y() / SCENE_UNITS_PER_MM)
            return
        if target[0] == 'property':
            origin = getattr(self, '_gesture_property_origin', None)
            if origin is None:
                return
            delta = point - self._gesture_start
            self.move_property(target[1], origin[0] + delta.x() / SCENE_UNITS_PER_MM,
                               origin[1] - delta.y() / SCENE_UNITS_PER_MM)
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
        from ..generators.symbol_glyphs import placed_primitives, placed_terminals, primitive_outline
        from ..generators.symbol_glyphs import elements_by_id, is_wire, is_shape, shape_handles
        graphics = self._graphics()
        # Resolved once per draw and shared by every wire.
        pin_ends = self._pin_ends()
        by_id = elements_by_id(graphics)
        chosen = set(self.selected_graphics())
        for index, instance in enumerate(graphics):
            selected = index in chosen
            element_handles = []
            theme = self.theme
            base_pen = QPen(QColor(theme['selected']) if selected else QColor(theme['graphic']), 2.2 if selected else 1.3)
            items = []
            # Anchored wire points are resolved against the current elements
            # and pins: a moved element or pin drags its wires along.
            primitives = placed_primitives(instance, graphics=graphics, pin_ends=pin_ends, by_id=by_id)
            if selected and is_wire(instance) and primitives:
                # Vertices of the selected wire: small squares to drag on
                # the grid, onto a terminal/pin (anchors) or away (frees).
                for position, (x, y) in enumerate(primitives[0].points):
                    square = self._add_handle(scene, x * scale, -y * scale, element_handles)
                    square.setData(0, ('vertex', index, position))
                    square.setCursor(Qt.CursorShape.CrossCursor)
                    square.setToolTip(self.tx('Vertice del filo: trascinalo; su un pin o un terminale si aggancia.'))
            if selected and is_shape(instance) and len(chosen) == 1:
                # Defining points of the selected shape (user, 2026-10-12):
                # corners, centre and rim, arc and curve points, text anchor.
                for position, (x, y) in enumerate(shape_handles(instance)):
                    square = self._add_handle(scene, x * scale, -y * scale, element_handles)
                    square.setData(0, ('shape_point', index, position))
                    square.setCursor(Qt.CursorShape.CrossCursor)
                    square.setToolTip(self.tx('Punto della forma: trascinalo sulla griglia del disegno.'))
            for p in primitives:
                pen = QPen(base_pen)
                pen.setStyle(PEN_STYLES.get(p.style, Qt.PenStyle.SolidLine))
                if p.kind == 'text':
                    items.append(self._draw_text_primitive(scene, scale, p, selected))
                elif p.kind in ('polyline', 'arc', 'bezier'):
                    points = primitive_outline(p)
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
            if element_handles:
                # The area that reveals the handles: the selected element and
                # its handle points; the margin (screen pixels) is added when
                # the pointer is tested (user, 2026-10-12: discreet handles).
                area = QRectF()
                for item in items:
                    area = area.united(item.sceneBoundingRect())
                for handle in element_handles:
                    area = area.united(QRectF(handle.pos(), handle.pos()))
                self._handle_groups.append((element_handles, area, False, ('graphic', index)))
                self._update_handle_visibility(self._cursor_position)

    def _add_handle(self, scene, sx, sy, group):
        """@brief A draggable handle square at a scene point, hidden until the pointer comes near.

        @param scene Scene to draw in.
        @param sx Scene X of the handle centre.
        @param sy Scene Y of the handle centre.
        @param group List collecting the handles that share one reveal area
            (registered in `_handle_groups` by the caller).
        @return The rect item; the caller sets its `data(0)` target.
        @note Discreet by request (user, 2026-10-12): small, with a
            translucent fill, and visible only while the pointer is inside
            the group's area or a drag is in progress
            (`_update_handle_visibility`).
        """
        from PyQt6.QtWidgets import QGraphicsItem
        theme = self.theme
        fill = QColor(theme['handle'])
        fill.setAlpha(150)
        half = HANDLE_SIZE_PX / 2
        square = scene.addRect(-half, -half, HANDLE_SIZE_PX, HANDLE_SIZE_PX,
                               QPen(QColor(theme['selected']), 1.0), fill)
        square.setPos(sx, sy)
        # Constant screen size at any zoom (the rect is in device pixels
        # about the item position).
        square.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        square.setZValue(12)
        square.setCursor(Qt.CursorShape.CrossCursor)
        square.setVisible(False)
        group.append(square)
        return square

    def _update_handle_visibility(self, point):
        """@brief Show the handles of the selected element while the pointer is in its area.

        @param point Scene position of the pointer; None hides the handles.
        @note Each group of `_handle_groups` is (items, base rect, ring, owner):
            the reveal area is the base rect grown by `HANDLE_HOVER_MARGIN`
            screen pixels (divided by the view scale, so the margin is the
            same on screen at any zoom); with `ring` the rect shrunk by the
            same margin does not count: the body covers the whole symbol, so
            its handles show only near its outline. While a gesture drags a
            target of the group, its handles stay visible wherever the
            pointer is (a handle hidden under the pointer that drags it
            would be absurd); the other groups follow the pointer rule.
        """
        if not self._pointer_inside:
            point = None
        scale = self.view.transform().m11() or 1.0
        m = HANDLE_HOVER_MARGIN / abs(scale)
        gesture = self.view.gesture
        dragged = None
        if gesture is not None:
            dragged = ('body',) if gesture[0] == 'resize' else \
                ('graphic', gesture[1]) if gesture[0] in ('graphic', 'shape_point', 'vertex') else None
        for items, base, ring, owner in self._handle_groups:
            if dragged is not None and owner == dragged:
                visible = True
            elif point is None:
                visible = False
            else:
                outer = base.adjusted(-m, -m, m, m)
                visible = outer.contains(point) and not (ring and base.adjusted(m, m, -m, -m).contains(point))
            for item in items:
                item.setVisible(visible)

    def _draw_text_primitive(self, scene, scale, p, selected):
        """A text primitive as a scene item: font size `p.size` mm, turned by
        `p.angle`, justified about its anchor point; returns the item."""
        theme = self.theme
        font = QFont(self.font())
        font.setPixelSize(max(1, int(round(p.size * scale))))
        item = scene.addSimpleText(p.text, font)
        item.setBrush(QColor(theme['selected']) if selected else QColor(theme['graphic']))
        bounds = item.boundingRect()
        dx = bounds.width() if p.justify == 'right' else bounds.width() / 2 if p.justify == 'center' else 0
        (x, y), = p.points
        if p.angle % 180 == 90:
            item.setRotation(-90)
            item.setPos(x * scale - bounds.height() / 2, -y * scale + dx)
        else:
            item.setPos(x * scale - dx, -y * scale - bounds.height() / 2)
        return item

    def _draw(self, symbol):
        self.properties_button.defaultAction().setEnabled(symbol is not None and any(
            p.pin.number == self._selected_pin for p in symbol.pins))
        self.align_button.defaultAction().setEnabled(len(self._selection_ids()) >= 2)
        self._update_readout()
        scene = self.view.scene()
        scene.clear()
        self._handle_groups = []
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
        elif self._shape_points:
            # The clicked points of the shape and, from the cursor, the
            # shape they would make (`_update_readout`).
            from PyQt6.QtGui import QPainterPath
            shape_pen = QPen(QColor(theme['wire']), 1.6, Qt.PenStyle.DashLine)
            for x, y in self._shape_points:
                scene.addEllipse(x * scale - 2.5, -y * scale - 2.5, 5, 5, shape_pen, QColor(theme['wire']))
            self._wire_cursor_item = scene.addPath(
                self._outline_path(self._shape_outline(self._shape_points), scale), shape_pen, QColor(0, 0, 0, 0))
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
                if target[0] == 'pin':
                    self._text_bounds.append((target[1], item.sceneBoundingRect()))
            return item

        from ..generators.symbol import pin_length_of, pin_text_size_of
        # Reference and Value where the file puts them (user, 2026-10-13):
        # the editor shows what KiCad will show, and both can be dragged.
        positions = self._property_positions(self.draft_rows(), symbol)
        texts = {'Reference': f'{self._reference}?', 'Value': self._name}
        hints = {'Reference': self.tx('Riferimento (U?): trascinalo dove deve stare nello schema.'),
                 'Value': self.tx('Nome del simbolo (Value): trascinalo dove deve stare nello schema.')}
        for key in PROPERTY_KEYS:
            px, py = positions[key]
            item = label(texts[key], px * scale, -py * scale, 'center', target=('property', key))
            item.setToolTip(hints[key])
        for placed in symbol.pins:
            pin = placed.pin
            x, y = placed.x * scale, -placed.y * scale
            vx, vy = {0: (1, 0), 180: (-1, 0), 270: (0, 1), 90: (0, -1)}[placed.angle]
            pin_length = pin_length_of(pin, symbol.pin_length)
            end_x, end_y = x + vx * pin_length * scale, y + vy * pin_length * scale
            target = ('pin', pin.number)
            selected = pin.number in self._selection_ids()
            pin_pen = QPen(QColor(theme['selected']) if selected else QColor(theme['pin']), 2.8 if selected else 1.3)
            if pin.hidden:
                # A hidden pin is drawn dotted: present in the symbol, invisible in the schematic.
                pin_pen.setStyle(Qt.PenStyle.DotLine)
            text_scale = pin_text_size_of(pin) / 1.27
            font.setPixelSize(max(1, int(round(12 * text_scale))))
            small_font.setPixelSize(max(1, int(round(10 * text_scale))))
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
        font.setPixelSize(12)
        small_font.setPixelSize(10)
        handles = [] if self._body_locked else [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
        body_handles = []
        for hx, hy in handles:
            x, y = hx * width / 2, hy * height / 2
            item = tag(self._add_handle(scene, x, y, body_handles), ('resize', hx, hy))
            item.setCursor(Qt.CursorShape.SizeHorCursor if not hy else Qt.CursorShape.SizeVerCursor if not hx else
                           Qt.CursorShape.SizeFDiagCursor if hx == hy else Qt.CursorShape.SizeBDiagCursor)
        if body_handles:
            # The body covers the whole symbol: its handles are revealed in a
            # ring around the outline (same margin inside and outside), not
            # anywhere over the body (user, 2026-10-12).
            body = QRectF(-width / 2, -height / 2, width, height)
            self._handle_groups.append((body_handles, body, True, ('body',)))
            self._update_handle_visibility(self._cursor_position)
        if self._gesture_before is not None:
            scene.setSceneRect(self._gesture_scene_rect)
        else:
            scene.setSceneRect(scene.itemsBoundingRect().adjusted(-35, -35, 35, 35))
        if self.view.auto_fit:
            self.view.fit()
