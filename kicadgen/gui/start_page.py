"""@brief Start page shown while no project is open: new, open, recent projects.

PIANO-FIX item 1. Before it, a window with no project showed three «Nessun
file» boxes, a disabled name field and a large button that could not be used,
without saying why. This page replaces that view: two large commands («Nuovo
progetto…», «Apri progetto…») and the projects used last, each reopened with
one click.

The page only emits signals; creating, opening and forgetting projects stay in
the main window, which owns the guards (no project change while a job writes
in the open project) and the configuration (`services.recent_projects`).

Texts follow the catalogue like every other window: the labels and buttons
are retranslated by `I18n.retranslate_widgets`; the rows of the list are data
and are drawn with the `tr` function given to the constructor, so a language
change needs only `refresh()`.
"""
import math
from time import monotonic

from PyQt6.QtCore import QEvent, QModelIndex, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QLabel, QListView,
                             QListWidget, QListWidgetItem, QMenu, QPushButton, QStyle,
                             QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget)

from ..services import recent_projects
from .review_tools import asset_icon
from .theme import BLUE, SUBTEXT, SURFACE0, SURFACE1, SURFACE2, TEXT

#: Tooltip of «Apri progetto…» while the working folder holds no project
#: (HANDOFF 170): the command is disabled and this says why.
NO_PROJECTS_TOOLTIP = ("Nessun progetto nell'area di lavoro: crea prima un progetto con «Nuovo "
                       "progetto…» oppure scegli un'altra cartella.")

#: Item data role that holds the `recent_projects.RecentProject` of a row.
SUMMARY_ROLE = Qt.ItemDataRole.UserRole + 1

#: Widest the page column may grow, in average character widths of the page
#: font. A reading choice: about 120 characters (some 840 logical pixels at
#: 10 pt) leave room for the German introduction on one line and project paths,
#: while the column does not stretch
#: across a maximised wide window. It is measured in characters, not pixels,
#: so it follows the font; logical pixels then follow the display scaling.
COLUMN_WIDTH_CHARS = 120

#: Side of the application icon above the title, in logical pixels (Qt scales
#: logical pixels with the display, so it stays proportionate at 200%).
LOGO_SIZE_PX = 56

#: Side of the icons of the two large commands, in logical pixels.
ACTION_ICON_PX = 28

#: Background of a recent project row under the mouse: between SURFACE0 (row)
#: and SURFACE1 (border), so hover is visible without looking selected.
_ROW_HOVER = "#3a3b50"

_PAGE_QSS = f"""
    QPushButton#startAction {{
        background: {SURFACE0};
        color: {TEXT};
        border: 1px solid {SURFACE1};
        border-radius: 8px;
        padding: 12px 22px;
        font-size: 11pt;
        font-weight: 600;
        text-align: left;
    }}
    QPushButton#startAction:hover {{
        background: {SURFACE1};
        border: 1px solid {SURFACE2};
    }}
    QPushButton#startAction:focus {{
        border: 1px solid {BLUE};
    }}
    QPushButton#startAction[primaryAction="true"] {{
        border: 1px solid {BLUE};
    }}
    QListWidget#recentList {{
        background: transparent;
        border: none;
        outline: none;
    }}
    QListWidget#recentList QScrollBar::add-page:vertical,
    QListWidget#recentList QScrollBar::sub-page:vertical {{
        background: none;
    }}
    QLabel#startTitle {{
        font-size: 20pt;
        font-weight: 600;
    }}
    QPushButton#rootButton {{
        background: {SURFACE0};
        border: 1px solid {SURFACE1};
        border-radius: 4px;
        padding: 4px 8px;
    }}
    QPushButton#rootButton:hover {{
        background: {SURFACE1};
        border-color: {BLUE};
    }}
    QLabel#startSubtitle, QLabel#recentHint, QLabel#recentEmpty, QLabel#rootCaption {{
        color: {SUBTEXT};
    }}
    QLabel#recentHeader {{
        font-size: 11pt;
        font-weight: 600;
    }}
    QLabel#recentEmpty {{
        background: {SURFACE0};
        border: 1px dashed {SURFACE1};
        border-radius: 8px;
        padding: 18px;
    }}
"""


def _text_width(font: QFont, painter: QPainter, text: str) -> int:
    """Width of `text` on the painted device, rounded up, 0 for no text."""
    if not text:
        return 0
    return math.ceil(QFontMetricsF(font, painter.device()).horizontalAdvance(text)) + 1


def split_first_line(total: int, name_needed: int, kind_needed: int, gap: int):
    """@brief Share the first line of a card between the name and the type.

    @param total Width of the line, px.
    @param name_needed Width the whole name needs, px.
    @param kind_needed Width the whole type label needs, px (0 = no type).
    @param gap Space between the two when both are shown, px.
    @return (name_width, kind_width).
    @note Each text gets all it needs when both fit. Otherwise the type takes
        what the name leaves free, but never less than half the line: a short
        name must not cut the type (seen with «frrr» and «Scheda con
        componenti · connettori castellated (mezzo foro)», cut at half the
        card although the card was almost empty), and a long name must not
        push the type out.
    """
    if kind_needed <= 0:
        return max(0, total), 0
    room = max(0, total - gap)
    kind = min(kind_needed, max(room // 2, room - name_needed))
    return max(0, room - kind), kind


class RecentProjectDelegate(QStyledItemDelegate):
    """@brief Draws one recent project as a card of two lines.

    First line: the project name in bold, the component type on the right.
    Second line: the full folder path, smaller and elided in the middle (the
    two ends of a path are the parts that tell folders apart), the date of
    the last save of the project file on the right.

    @note Every size comes from the font metrics of the view, so the row
        grows with the font and with the display scaling.
    """

    def __init__(self, page, parent=None):
        """@brief Keep the page, which knows how to label a component type.

        @param page Owning StartPage (for the component type label).
        @param parent Qt parent.
        """
        super().__init__(parent)
        self._page = page

    @staticmethod
    def _fonts(base: QFont):
        """Name font (bold, one point larger) and detail font (one point smaller)."""
        size = base.pointSizeF() if base.pointSizeF() > 0 else 10.0
        name = QFont(base)
        name.setBold(True)
        name.setPointSizeF(size + 1.0)
        detail = QFont(base)
        detail.setPointSizeF(max(7.0, size - 1.0))
        return name, detail

    @staticmethod
    def _padding(metrics: QFontMetrics) -> int:
        """Inner padding of a card, proportional to the text height."""
        return max(6, round(metrics.height() * 0.6))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        """@brief Height of two text lines plus padding and the gap between cards.

        @param option Style option of the view (font).
        @param index Row index (unused: every row has the same height).
        @return The row size. The width asked is minimal on purpose: the view
            gives every row its viewport width, and asking for the full view
            width made a horizontal scroll bar appear, with cards drawn under
            the vertical one, whenever the list had to scroll.
        """
        name_font, detail_font = self._fonts(option.font)
        name_metrics, detail_metrics = QFontMetrics(name_font), QFontMetrics(detail_font)
        padding = self._padding(detail_metrics)
        height = name_metrics.height() + detail_metrics.height() + padding * 2 + padding // 2
        return QSize(1, height + self._gap(detail_metrics))

    @staticmethod
    def _gap(metrics: QFontMetrics) -> int:
        """Empty space under each card, so cards read as separate rows."""
        return max(4, metrics.height() // 3)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """@brief Paint the card: background, border, the two lines of text.

        @param painter Painter of the view.
        @param option Style option: rectangle, state (selected, hover, focus), font.
        @param index Row index; carries the RecentProject in SUMMARY_ROLE.
        """
        summary = index.data(SUMMARY_ROLE)
        if summary is None:
            super().paint(painter, option, index)
            return
        name_font, detail_font = self._fonts(option.font)
        # Metrics of the device being painted: the screen-independent ones
        # under-measure at 150% and elided text that fitted (seen at 150%
        # and 200%, «Strip / head…»).
        name_metrics = QFontMetrics(name_font, painter.device())
        detail_metrics = QFontMetrics(detail_font, painter.device())
        padding = self._padding(detail_metrics)
        card = QRect(option.rect)
        card.setBottom(card.bottom() - self._gap(detail_metrics))
        card = card.adjusted(1, 1, -1, -1)

        state = option.state
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        focused = bool(state & QStyle.StateFlag.State_HasFocus)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(_ROW_HOVER if hovered or selected else SURFACE0))
        border = BLUE if selected and focused else SURFACE2 if hovered or selected else SURFACE1
        painter.setPen(QPen(QColor(border), 1))
        radius = max(4.0, detail_metrics.height() * 0.45)
        painter.drawRoundedRect(card, radius, radius)

        inner = card.adjusted(padding, padding // 2 + padding // 4, -padding, -padding // 2)
        gap = max(8, detail_metrics.averageCharWidth() * 2)
        first = QRect(inner.left(), inner.top(), inner.width(), name_metrics.height())
        second = QRect(inner.left(), first.bottom() + 1 + padding // 4,
                       inner.width(), detail_metrics.height())

        kind = self._page.component_type_text(summary)
        stamp = summary.modified.strftime("%d/%m/%Y %H:%M") if summary.modified else ""

        # Right before left: name and path get shortened, type and date do
        # not (they are short and say things the name does not say).
        painter.setFont(detail_font)
        painter.setPen(QColor(SUBTEXT))
        kind_width = _text_width(detail_font, painter, kind)
        stamp_width = _text_width(detail_font, painter, stamp)
        name_width, kind_width = split_first_line(
            first.width(), _text_width(name_font, painter, summary.name), kind_width, gap)
        if kind:
            painter.drawText(QRect(first.right() - kind_width, first.top(), kind_width,
                                   first.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             detail_metrics.elidedText(kind, Qt.TextElideMode.ElideRight,
                                                       kind_width))
        if stamp:
            painter.drawText(QRect(second.right() - stamp_width, second.top(), stamp_width,
                                   second.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, stamp)
        path_width = max(0, second.width() - (stamp_width + gap if stamp else 0))
        painter.drawText(QRect(second.left(), second.top(), path_width, second.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         detail_metrics.elidedText(summary.path, Qt.TextElideMode.ElideMiddle,
                                                   path_width))

        painter.setFont(name_font)
        painter.setPen(QColor(TEXT))
        painter.drawText(QRect(first.left(), first.top(), name_width, first.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         name_metrics.elidedText(summary.name, Qt.TextElideMode.ElideRight,
                                                 name_width))
        painter.restore()


class StartPage(QWidget):
    """@brief What the main window shows while no project is open.

    @note Signals only: the page never creates, opens or forgets a project by
        itself. After `remove_recent_requested` the caller updates the stored
        list and calls `set_recent` again, so the list on screen is always
        the stored one.
    """

    #: «Nuovo progetto…» was chosen.
    new_requested = pyqtSignal()
    #: «Apri progetto…» was chosen.
    open_requested = pyqtSignal()
    #: A recent project was chosen (click, double click or Enter); the
    #: argument is its absolute folder.
    open_recent_requested = pyqtSignal(str)
    #: «Rimuovi dall'elenco» (context menu or the Delete key) on a recent
    #: project; the argument is its absolute folder. The folder is not touched.
    remove_recent_requested = pyqtSignal(str)
    #: The folder button of «Area di lavoro» was pressed: the window asks for
    #: a new working folder (the page only shows it).
    choose_root_requested = pyqtSignal()

    def __init__(self, tr=None, type_label=None, parent=None):
        """@brief Build the page with an empty recent list.

        @param tr Translation function for the texts drawn in the rows and the
            context menu (Italian source -> shown text); identity if None.
        @param type_label Function (key, saved label) -> shown label of a
            component type, e.g. `I18n.component_type`; None shows the label
            saved in the project file.
        @param parent Qt parent.
        """
        super().__init__(parent)
        self._tr = tr or (lambda text: text)
        self._type_label = type_label
        self._last_open = ("", float("-inf"))
        self.setObjectName("startPage")
        self.setStyleSheet(_PAGE_QSS)

        self.logo = QLabel()
        self.logo.setProperty("i18n_skip", True)
        self.logo.setPixmap(asset_icon("app-icon.svg").pixmap(QSize(LOGO_SIZE_PX, LOGO_SIZE_PX),
                                                                self.devicePixelRatioF()))
        self.title = QLabel("KiCadGen")
        self.title.setObjectName("startTitle")
        self.title.setProperty("i18n_skip", True)
        self.subtitle = QLabel("Nessun progetto aperto: creane uno nuovo o riprendi un "
                               "progetto recente.")
        self.subtitle.setObjectName("startSubtitle")
        self.subtitle.setWordWrap(True)

        self.btn_new = self._action_button(
            "project-new.svg", "Nuovo progetto...",
            "Crea la cartella di un nuovo componente nella cartella di lavoro (Ctrl+N)")
        self.btn_new.setProperty("primaryAction", True)
        self.btn_new.clicked.connect(self.new_requested)
        self.btn_open = self._action_button(
            "project-open.svg", "Apri progetto...",
            "Apre un componente esistente della cartella di lavoro (Ctrl+O)")
        self.btn_open.clicked.connect(self.open_requested)
        #: Tooltip of «Apri» while there is something to open (catalogue source).
        self._open_tooltip = "Apre un componente esistente della cartella di lavoro (Ctrl+O)"

        # «Nuovo» (new) and «Apri» (open) work in the working folder, whose
        # field is in the Project tab, hidden until there is a project:
        # without this row the start page would make it unreachable.
        self.root_caption = QLabel("Area di lavoro:")
        self.root_caption.setObjectName("rootCaption")
        self.root_path = QLabel()
        self.root_path.setObjectName("rootPath")
        self.root_path.setProperty("i18n_skip", True)
        self.root_path.setTextFormat(Qt.TextFormat.PlainText)
        self.root_path.setMinimumWidth(1)
        # The path is shortened again when the width of the label changes
        # (eventFilter), not of the page: the layout resizes it afterwards.
        self.root_path.installEventFilter(self)
        self._root_text = ""
        self.btn_root = QPushButton(asset_icon("docs-open.svg"), "")
        self.btn_root.setObjectName("rootButton")
        self.btn_root.setToolTip("Scegli la cartella di lavoro")
        self.btn_root.setAccessibleName("Scegli la cartella di lavoro")
        self.btn_root.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_root.clicked.connect(self.choose_root_requested)

        self.recent_header = QLabel("Progetti recenti")
        self.recent_header.setObjectName("recentHeader")
        self.recent_hint = QLabel("Un clic riapre il progetto; con il tasto destro lo "
                                  "togli dall'elenco.")
        self.recent_hint.setObjectName("recentHint")
        self.recent_hint.setWordWrap(True)
        self.recent_list = QListWidget()
        self.recent_list.setObjectName("recentList")
        self.recent_list.setItemDelegate(RecentProjectDelegate(self, self.recent_list))
        self.recent_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.recent_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.recent_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.recent_list.setUniformItemSizes(True)
        self.recent_list.setMouseTracking(True)
        self.recent_list.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.recent_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.recent_list.setAccessibleName("Progetti recenti")
        self.recent_list.clicked.connect(self._on_clicked)
        self.recent_list.doubleClicked.connect(self._on_clicked)
        self.recent_list.customContextMenuRequested.connect(self._on_context_menu)
        self.recent_list.installEventFilter(self)
        self.recent_empty = QLabel("Nessun progetto recente. I progetti che crei o apri "
                                   "compariranno qui.")
        self.recent_empty.setObjectName("recentEmpty")
        self.recent_empty.setWordWrap(True)
        self.recent_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # The application style sheet sets the font size; polishing first
        # measures that font and not Qt's default one.
        self.ensurePolished()
        column = QWidget()
        column.setObjectName("startColumn")
        column.setMaximumWidth(self.fontMetrics().averageCharWidth() * COLUMN_WIDTH_CHARS)
        content = QVBoxLayout(column)
        content.setContentsMargins(0, 0, 0, 0)
        spacing = self.fontMetrics().height()
        heading = QHBoxLayout()
        heading.setSpacing(spacing)
        heading.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        heading.addLayout(titles, 1)
        content.addLayout(heading)
        content.addSpacing(spacing * 2)
        actions = QHBoxLayout()
        actions.setSpacing(spacing)
        actions.addWidget(self.btn_new, 1)
        actions.addWidget(self.btn_open, 1)
        content.addLayout(actions)
        content.addSpacing(spacing // 2)
        root_row = QHBoxLayout()
        root_row.setSpacing(spacing // 2)
        root_row.addWidget(self.root_caption)
        root_row.addWidget(self.root_path, 1)
        root_row.addWidget(self.btn_root)
        content.addLayout(root_row)
        content.addSpacing(spacing * 2)
        content.addWidget(self.recent_header)
        content.addWidget(self.recent_hint)
        content.addSpacing(spacing // 2)
        content.addWidget(self.recent_list, 1)
        content.addWidget(self.recent_empty)
        content.addStretch(0)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(spacing * 2, spacing * 3, spacing * 2, spacing * 2)
        outer.addStretch(1)
        outer.addWidget(column, 100)
        outer.addStretch(1)
        self.set_recent([])

    def _action_button(self, asset: str, text: str, tooltip: str) -> QPushButton:
        """One of the two large commands: icon, text and tooltip (catalogue sources)."""
        button = QPushButton(asset_icon(asset), text)
        button.setObjectName("startAction")
        button.setIconSize(QSize(ACTION_ICON_PX, ACTION_ICON_PX))
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def set_recent(self, entries) -> None:
        """@brief Show the given recent projects, in the given order.

        @param entries Iterable of absolute folder paths (str or Path) or of
            `recent_projects.RecentProject`; a path is described by reading
            its project file (`recent_projects.describe`).
        @note An empty list hides the list and shows the «Nessun progetto
            recente» line instead, so no empty box is left on screen.
        """
        self.recent_list.clear()
        for entry in entries or ():
            summary = (entry if isinstance(entry, recent_projects.RecentProject)
                       else recent_projects.describe(entry))
            item = QListWidgetItem(summary.name)
            item.setData(Qt.ItemDataRole.UserRole, summary.path)
            item.setData(SUMMARY_ROLE, summary)
            item.setToolTip(summary.path)
            self.recent_list.addItem(item)
        has_entries = self.recent_list.count() > 0
        self.recent_list.setVisible(has_entries)
        self.recent_hint.setVisible(has_entries)
        self.recent_empty.setVisible(not has_entries)

    def set_library_root(self, path) -> None:
        """@brief Show the working folder in which «Nuovo» and «Apri» work.

        @param path Absolute folder (str or Path); shown elided in the middle,
            complete in the tooltip.
        """
        self._root_text = str(path)
        self.root_path.setToolTip(self._root_text)
        self._elide_root()

    def library_root_text(self) -> str:
        """@brief Return the full working folder last given to `set_library_root`.

        @return The unelided folder text, "" before any root was set.
        """
        return self._root_text

    def set_projects_available(self, available: bool) -> None:
        """@brief Enable «Apri progetto…» only while the working folder holds a project.

        @param available True when at least one project exists in the folder.
        @note User decision (2026-09-30, HANDOFF 170): a command that would
            open an empty list is disabled, and its tooltip says why and what
            to do (create a project, or choose another folder). «Nuovo» stays
            enabled: it is the way out.
        """
        available = bool(available)
        self.btn_open.setEnabled(available)
        self.btn_open.setToolTip(self._tr(self._open_tooltip) if available else self._tr(
            NO_PROJECTS_TOOLTIP))
        self.btn_open.setCursor(Qt.CursorShape.PointingHandCursor if available
                                else Qt.CursorShape.ArrowCursor)

    def _elide_root(self) -> None:
        width = max(self.root_path.width(), self.fontMetrics().averageCharWidth() * 20)
        self.root_path.setText(self.root_path.fontMetrics().elidedText(
            self._root_text, Qt.TextElideMode.ElideMiddle, width))

    def recent_paths(self) -> list:
        """@brief Folders currently listed, top to bottom.

        @return The project paths stored in the recent-list items, in list order.
        """
        return [self.recent_list.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(self.recent_list.count())]

    def component_type_text(self, summary) -> str:
        """@brief Label of the component type of a recent project, or "".

        @param summary `recent_projects.RecentProject`.
        @return The translated label when `type_label` was given, the saved
            label otherwise; "" for «not specified» (empty key), which says
            nothing worth a column.
        """
        if not summary.component_type_key:
            return ""
        if self._type_label is not None:
            return self._type_label(summary.component_type_key, summary.component_type)
        return summary.component_type

    def refresh(self) -> None:
        """@brief Redraw the rows, e.g. after a language change."""
        self.recent_list.viewport().update()

    def _emit_open(self, path: str) -> None:
        """Emit `open_recent_requested` once per gesture.

        A double click delivers a click and then a double click on the same
        row: without this guard the project would be opened twice in a row.
        """
        now = monotonic()
        last_path, last_time = self._last_open
        if path == last_path and now - last_time < QApplication.doubleClickInterval() / 1000.0:
            return
        self._last_open = (path, now)
        self.open_recent_requested.emit(path)

    def _on_clicked(self, index: QModelIndex) -> None:
        """Left click or double click on a row: reopen that project."""
        path = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        if path:
            self._emit_open(path)

    def context_menu_for(self, item: QListWidgetItem) -> QMenu:
        """@brief Context menu of a recent project row (built, not shown).

        @param item Row the menu refers to.
        @return A QMenu with «Apri progetto» and «Rimuovi dall'elenco»; its
            actions emit the page signals for that row's folder.
        @note Kept apart from showing the menu so that tests can trigger the
            actions without a blocking `exec`.
        """
        path = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        open_action = menu.addAction(asset_icon("project-open.svg"), self._tr("Apri progetto"))
        open_action.triggered.connect(lambda _checked=False: self._emit_open(path))
        remove_action = menu.addAction(asset_icon("delete.svg"), self._tr("Rimuovi dall'elenco"))
        remove_action.triggered.connect(
            lambda _checked=False: self.remove_recent_requested.emit(path))
        return menu

    def _on_context_menu(self, position) -> None:
        """Right click: select the row under the pointer and show its menu."""
        item = self.recent_list.itemAt(position)
        if item is None:
            return
        self.recent_list.setCurrentItem(item)
        menu = self.context_menu_for(item)
        menu.exec(self.recent_list.viewport().mapToGlobal(position))
        menu.deleteLater()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        """@brief Enter reopens the current recent project, Delete forgets it.

        @param watched Object that received the event (the recent list, or
            the working-folder label, re-elided when it is resized).
        @param event The event.
        @return True when the key was handled here.
        """
        if watched is self.root_path and event.type() == QEvent.Type.Resize:
            self._elide_root()
        if watched is self.recent_list and event.type() == QEvent.Type.KeyPress:
            item = self.recent_list.currentItem()
            if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._emit_open(item.data(Qt.ItemDataRole.UserRole))
                return True
            if item is not None and event.key() == Qt.Key.Key_Delete:
                self.remove_recent_requested.emit(item.data(Qt.ItemDataRole.UserRole))
                return True
        return super().eventFilter(watched, event)
