"""@brief «Modello AI»: the default AI model, and a model of their own for single analysis steps.

The source (DeepSeek or OpenRouter) is the provider of the chosen model; only
the models of sources that have a key are offered. The prompts are edited in
the profile manager only (user, 2026-11-12). Every analysis step
(prompt_models.STEPS) runs on the default model unless a model of its own is
chosen in the step table (user request 2026-11-12, global, never per type),
kept in a section collapsed by default (user, 2026-11-12: «dobbiamo
semplificare la UX»).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from ..services.datasheet_extractor import DEFAULT_PAD_POSITIONS_MODEL
from ..services.messages import tx
from ..services.openrouter import (DATASHEET_CAPABILITIES, DEFAULT_MODEL, IMAGES_NOT_READ,
                                   IMAGES_UNDECLARED, PHOTO_CAPABILITIES, catalog_id,
                                   image_capability)
from ..services.prompt_models import (GROUP_DOCUMENTS, GROUP_PHOTO, GROUP_SYMBOL,
                                      LEGACY_PAD_STEPS, STEPS,
                                      step_models as resolve_step_models)
from ..services.providers import PROVIDER_DEEPSEEK, PROVIDER_LABELS, PROVIDERS, provider_of
from .collapsible_group import CollapsibleGroupBox
from .model_dialog import pick_model
from .theme import SUBTEXT, YELLOW

#: Columns of the step table: step title (with its hint), model actually used, buttons.
COL_STEP, COL_MODEL, COL_ACTIONS = range(3)

#: Capabilities the model picker filters on, by step group: documents are
#: sent as PDF and images (the pin-type step sends the documents too),
#: photos as images; a step without images (`Step.images` False) needs
#: tool calling only.
_GROUP_CAPABILITIES = {GROUP_DOCUMENTS: DATASHEET_CAPABILITIES, GROUP_PHOTO: PHOTO_CAPABILITIES,
                       GROUP_SYMBOL: DATASHEET_CAPABILITIES}
_TEXT_ONLY_CAPABILITIES = ("tools",)

#: Item data role holding the step field of a table row (None on a group row).
_FIELD_ROLE = Qt.ItemDataRole.UserRole
#: Item data role holding the translated hint drawn under a step title.
HINT_ROLE = Qt.ItemDataRole.UserRole + 1

#: Muted text (SUBTEXT on the table's SURFACE0 is about 5.5:1, above WCAG's
#: 4.5:1) and the application's warning colour (theme.YELLOW, the one the
#: main window uses for STATUS_WARNING).
MUTED_COLOR = QColor(SUBTEXT)
WARNING_COLOR = QColor(YELLOW)

#: Horizontal padding of the two-line step cell, in pixels.
_CELL_PADDING = 6
#: Height kept free for the window's title bar and frame when the window is
#: not yet framed by the window manager (frameGeometry still equals geometry).
_FRAME_ALLOWANCE = 40


def factory_step_models() -> dict[str, str]:
    """@brief The step models of a fresh installation: every step on the default model.

    @return {field: ""} for every step, except the two steps the old
        «Posizioni pad» model served (prompt_models.LEGACY_PAD_STEPS), which
        keep DEFAULT_PAD_POSITIONS_MODEL: the factory behaviour since 2026-10-30.
    """
    return resolve_step_models({}, DEFAULT_PAD_POSITIONS_MODEL)


def _hint_font(font: QFont) -> QFont:
    """The smaller font of a step hint: 90 % of `font`, in its own unit (points or pixels)."""
    small = QFont(font)
    if font.pointSizeF() > 0:
        small.setPointSizeF(max(6.0, font.pointSizeF() * 0.9))
    elif font.pixelSize() > 0:
        small.setPixelSize(max(8, round(font.pixelSize() * 0.9)))
    return small


def _column_width(option, index) -> int:
    """The width of the cell's column in its view, or 0 when the view is unknown."""
    view = option.widget
    width_of = getattr(view, "columnWidth", None)
    return int(width_of(index.column())) if callable(width_of) else 0


def _hint_lines_height(metrics: QFontMetrics, hint: str, width: int) -> int:
    """Height of `hint` word-wrapped to `width` pixels (one line when `width` is unknown)."""
    if width <= 0:
        return metrics.height()
    flags = int(Qt.TextFlag.TextWordWrap.value)
    return metrics.boundingRect(QRect(0, 0, width, 10 ** 5), flags, hint).height()


class StepCellDelegate(QStyledItemDelegate):
    """@brief Draw a step cell as the step title and, below it, the hint in a muted, smaller font.

    @note The hint is always visible (UX review 2026-11-12, finding 8: a hint
        reachable only by hovering was missed); a group caption row has no
        hint and is drawn by the base delegate. When the column is narrower
        than the hint (a small screen, a longer translation) the hint wraps
        onto more lines instead of being cut (practical test 2026-11-14).
    """

    def paint(self, painter, option, index) -> None:
        """@brief Qt override: paint the title and the muted hint, each elided to the cell width.

        @param painter Active QPainter.
        @param option Style option of the cell.
        @param index Model index of the cell.
        """
        hint = index.data(HINT_ROLE)
        if not hint:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        title = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = opt.rect.adjusted(_CELL_PADDING, 0, -_CELL_PADDING, 0)
        title_metrics = QFontMetrics(opt.font)
        small = _hint_font(opt.font)
        hint_metrics = QFontMetrics(small)
        hint_height = _hint_lines_height(hint_metrics, str(hint), rect.width())
        top = rect.top() + max(0, (rect.height() - title_metrics.height() - hint_height) // 2)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
        painter.drawText(QRect(rect.left(), top, rect.width(), title_metrics.height()), flags,
                         title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, rect.width()))
        painter.setFont(small)
        painter.setPen(MUTED_COLOR)
        hint_top = top + title_metrics.height()
        painter.drawText(QRect(rect.left(), hint_top, rect.width(),
                               max(hint_metrics.height(), rect.bottom() - hint_top)),
                         int(Qt.AlignmentFlag.AlignLeft.value | Qt.AlignmentFlag.AlignTop.value
                             | Qt.TextFlag.TextWordWrap.value), str(hint))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt override
        """@brief Qt override: wide enough for the longer of the two lines, tall enough for both.

        @param option Style option of the cell.
        @param index Model index of the cell.
        @return The natural (one-line hint) width of the cell, and the height
            of the title plus the hint wrapped to the column's current width;
            the base size on a group row.
        """
        hint = index.data(HINT_ROLE)
        if not hint:
            return super().sizeHint(option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        title_metrics = QFontMetrics(opt.font)
        hint_metrics = QFontMetrics(_hint_font(opt.font))
        width = max(title_metrics.horizontalAdvance(opt.text),
                    hint_metrics.horizontalAdvance(str(hint))) + 2 * _CELL_PADDING + 4
        column = _column_width(opt, index) - 2 * _CELL_PADDING
        hint_height = _hint_lines_height(hint_metrics, str(hint), column)
        return QSize(width, title_metrics.height() + hint_height + 4)


class LlmSettingsDialog(QDialog):
    """@brief «Modello AI»: one line for the default model, a collapsed section for the steps' own models.

    The window reads top to bottom (user, 2026-11-12: «dobbiamo semplificare
    la UX»): one short sentence with the «i» button holding the full
    explanation; one row «Modello: <id> (<source>) [Cambia…]», the source
    being the provider of the model; the section «Modelli diversi per singoli passi»,
    collapsed by default, whose title counts the steps with a model of their
    own; a warning row only when a model in use has no key.
    @note Only the models of sources with a key are offered (user,
        2026-11-12: «se la chiave non è presente non dovrebbe neppure
        apparire il modello»); with no key at all the window shows only the
        warning row with «Account AI...».
    @note Compact while the section is collapsed; expanding it grows the
        window so every step row is visible (user, 2026-11-12: «aprila di
        default più alta»), computed from the real row heights and capped to
        the screen's available height: on a smaller screen the table scrolls,
        and Tab scrolls it to the focused row button.
    @note `vision_model()` and `verification_model()` remain for callers that
        save the defaults per stage: they return the same default model.
        `pad_model()` returns the model of the pad-position step, for callers
        of the old «Posizioni pad» field.
    """

    def __init__(self, current_prompt: str = "", current_model: str | None = None, parent=None,
                 current_vision_model: str | None = None,
                 current_grounding_prompt=None, current_verification_prompt=None,
                 current_verification_model=None, current_single_prompt=None,
                 current_numbering_prompt=None, current_family: str | None = None,
                 current_pad_model: str | None = None,
                 current_step_models: dict | None = None,
                 stored_step_fields=None, account_opener=None):
        """@brief Build the «Modello AI» window.

        @param current_prompt Ignored (the prompts moved to the profile manager);
            kept so older callers keep working.
        @param current_model The default model; None shows DEFAULT_MODEL.
        @param parent Owner window; its ``i18n`` translates the labels.
        @param current_vision_model Ignored (one default model).
        @param current_grounding_prompt Ignored (prompts are in the profile manager).
        @param current_verification_prompt Ignored (prompts are in the profile manager).
        @param current_verification_model Ignored (one default model).
        @param current_single_prompt Ignored (prompts are in the profile manager).
        @param current_numbering_prompt Ignored (prompts are in the profile manager).
        @param current_family Ignored.
        @param current_pad_model Old «Posizioni pad» model: used only when
            `current_step_models` is None, as the model of the two pad steps
            (None = the factory default, "" = the default model).
        @param current_step_models The model of every step,
            {prompt field: identifier or "" for the default model}
            (ProfileStore.step_models); missing steps follow the default model.
        @param stored_step_fields Fields with a SAVED choice
            (ProfileStore.step_models_stored keys), or None for none. A saved
            or changed choice whose key is missing blocks a paid run
            (MainWindow._ensure_api_key); an unsaved default (the pad steps'
            old reader) falls back on the default model. The warning row says
            which of the two happens.
        @param account_opener Callable opening «Account AI...» (the main
            window's), or None to open the dialog directly.
        """
        super().__init__(parent)
        self.i18n = getattr(parent, 'i18n', None)
        self.setWindowTitle("Modello AI")
        self.resize(900, 260)
        self.setModal(True)

        self._model = (current_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        if current_step_models is None:
            legacy = DEFAULT_PAD_POSITIONS_MODEL if current_pad_model is None else current_pad_model
            current_step_models = resolve_step_models({}, legacy)
        #: {step field: model identifier, or "" for the default model}.
        self._steps = {item.field: str(current_step_models.get(item.field) or "").strip()
                       for item in STEPS}
        #: The table as it opened: a step differing from it is a change the user made.
        self._initial_steps = dict(self._steps)
        #: Steps with a saved choice (see `stored_step_fields`).
        self._stored_fields = frozenset(stored_step_fields or ())
        self._account_opener = account_opener

        layout = QVBoxLayout(self)
        # The same air as the model picker (user, 2026-11-12: «dai un po' di
        # aria», «puoi staccare un po' qui?»).
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        self.info_buttons = []
        self._help_text = (
            "Il modello predefinito esegue ogni passo dell'analisi; la sua sorgente è quella del "
            "modello, indicata fra parentesi: DeepSeek (API diretta, chiave DeepSeek) oppure "
            "OpenRouter (chiave OpenRouter). «Cambia…» elenca solo i modelli delle sorgenti che "
            "hanno una chiave; le chiavi si inseriscono con Configurazione → «Account AI...» e "
            "il saldo della sorgente in uso compare sotto «Estrai parametri con AI». Nella "
            "sezione «Modelli diversi per singoli passi» un passo può avere un modello proprio: "
            "«Cambia…» lo sceglie, «Usa il predefinito» lo riporta al modello predefinito, «Ripristina passi "
            "predefiniti» riporta solo la tabella alla scelta iniziale (posizioni dei pad e "
            "quote del corpo di un componente singolo su un modello proprio, perché sui disegni "
            "quotati i modelli economici sbagliano spesso). La scelta vale per tutte le "
            "tipologie. Prima di un'analisi a pagamento si controlla che la sorgente di ogni "
            "passo scelto che l'analisi usa abbia la sua chiave; se poi il modello di un passo "
            "non può essere usato (per esempio credito esaurito), quel passo lo esegue il "
            "modello predefinito e il log lo dice. I testi inviati ai modelli (prompt) si modificano "
            "in Configurazione → «Gestione tipologie e profili AI...». Nel tab Progetto la riga "
            "«Modelli AI» di «Strumenti AI» riassume il modello predefinito e i passi con un "
            "modello proprio; il pulsante accanto alla riga apre questa finestra."
        )

        intro_row = QHBoxLayout()
        #: One sentence; the details are behind the «i» button.
        self.prompts_note = QLabel("Quale modello AI usa l'analisi.")
        self.prompts_note.setObjectName("prompts_note")
        # One short line: a wrapping label would reserve height for a second one.
        self.prompts_note.setWordWrap(False)
        intro_row.addWidget(self.prompts_note, 1)
        intro_row.addWidget(self._info_button(), 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(intro_row)

        #: «Modello: <id> (<source>) [Cambia…]».
        self.single_row = QWidget()
        self.single_row.setObjectName("single_model_row")
        single_layout = QVBoxLayout(self.single_row)
        single_layout.setContentsMargins(0, 0, 0, 0)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Modello:"))
        self.field_model = QLineEdit(self._model)
        self.field_model.setReadOnly(True)
        self.field_model.setObjectName("single_model")
        model_row.addWidget(self.field_model, 1)
        #: Warning icon shown when the default model is declared text only (see _refresh_single).
        self.label_default_warning = QLabel()
        self.label_default_warning.setObjectName("single_model_warning")
        self.label_default_warning.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(16, 16))
        self.label_default_warning.setVisible(False)
        model_row.addWidget(self.label_default_warning)
        #: The source, i.e. the provider of the default model, in parentheses.
        self.label_source = QLabel()
        self.label_source.setObjectName("single_model_source")
        self.label_source.setProperty("i18n_skip", True)
        model_row.addWidget(self.label_source)
        # Same verb as the rows' button (finding 13): «Cambia…» everywhere.
        self.btn_pick_single = QPushButton(tx("Cambia…"))
        self.btn_pick_single.setObjectName("single_model_change")
        self.btn_pick_single.setAutoDefault(False)
        self.btn_pick_single.setAccessibleName(tx("Cambia il modello predefinito"))
        self.btn_pick_single.setToolTip(tx("Sceglie il modello predefinito fra quelli delle sorgenti con "
                                           "una chiave."))
        self.btn_pick_single.setProperty("i18n_skip", True)
        self.btn_pick_single.clicked.connect(self._pick_model)
        model_row.addWidget(self.btn_pick_single)
        single_layout.addLayout(model_row)
        layout.addWidget(self.single_row)

        #: The steps' own models, collapsed by default; the title counts them.
        self.steps_box = CollapsibleGroupBox("Modelli diversi per singoli passi")
        self.steps_box.setObjectName("step_models_section")
        # The title is composed here with its count, already translated.
        self.steps_box.setProperty("i18n_skip", True)
        steps_layout = QVBoxLayout(self.steps_box.body)
        steps_layout.setContentsMargins(4, 6, 4, 4)
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("step_models_table")
        # «Passo dell'analisi», not «Passo»: that catalogue source is the pin
        # pitch (finding 1, the header read «Pitch» in English).
        self.table.setHorizontalHeaderLabels(["Passo dell'analisi", "Modello usato", ""])
        self.table.verticalHeader().setVisible(False)
        # Every row as tall as its content (two-line step cell, buttons), no more.
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # The buttons are the controls (Tab reaches them); the cells are text.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setItemDelegateForColumn(COL_STEP, StepCellDelegate(self.table))
        header = self.table.horizontalHeader()
        # The model column is fitted to its header and texts (`_fit_to_rows`),
        # the step column takes the rest and wraps its hints when narrow: no
        # horizontal scroll, and a model id is elided only on a screen too
        # small for it (its tooltip names it in full). Practical test
        # 2026-11-14: the stretching model column was squeezed to «= mod…».
        header.setSectionResizeMode(COL_STEP, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_MODEL, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_ACTIONS, QHeaderView.ResizeMode.ResizeToContents)
        # A narrower step column wraps the hints onto more lines: the rows follow.
        header.sectionResized.connect(self._on_column_resized)
        #: {step field: (row, «Cambia…» button, «Usa il predefinito» button)}.
        self._rows: dict[str, tuple[int, QPushButton, QPushButton]] = {}
        #: {row button: its table row}, read by the focus filter.
        self._button_rows: dict[QPushButton, int] = {}
        self._build_table()
        steps_layout.addWidget(self.table)
        # A clear gap between the model row and the steps section.
        layout.addSpacing(10)
        layout.addWidget(self.steps_box, 1)
        # Spare height (a wrapped label's estimate) goes here, below the
        # closed section, never between the intro and the model row; while
        # the section is open its stretch is 0 and the table takes it all.
        layout.addStretch(1)
        #: Layout index of that spacer (see `_fit_to_rows`).
        self._spare_index = layout.count() - 1
        self.steps_box.toggled.connect(lambda _expanded: self._fit_to_rows())

        #: The key warning row: shown ONLY when a model in use has no key
        #: (user, 2026-11-12: «togliamo questo» about the line saying that
        #: every key is present), or alone, in place of the model controls,
        #: when no source has a key.
        self.keys_row = QWidget()
        self.keys_row.setObjectName("step_key_row")
        keys_row = QHBoxLayout(self.keys_row)
        keys_row.setContentsMargins(0, 0, 0, 0)
        #: Which models lack their source's key, and what happens to them.
        self.label_keys_used = QLabel()
        self.label_keys_used.setObjectName("step_key_state")
        self.label_keys_used.setWordWrap(True)
        self.label_keys_used.setProperty("i18n_skip", True)
        self.label_keys_used.setStyleSheet(f"color: {YELLOW};")
        keys_row.addWidget(self.label_keys_used, 1)
        self.btn_account = QPushButton("Account AI...")
        self.btn_account.setObjectName("open_ai_account")
        self.btn_account.setAutoDefault(False)
        self.btn_account.setToolTip("Chiavi OpenRouter e DeepSeek, credito e limiti.")
        self.btn_account.clicked.connect(self._open_account)
        keys_row.addWidget(self.btn_account, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.keys_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.RestoreDefaults
            | QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        #: «Ripristina passi predefiniti»: the step table only (finding 6).
        self.btn_restore_steps = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        self.btn_restore_steps.setText("Ripristina passi predefiniti")
        self.btn_restore_steps.setObjectName("restore_step_defaults")
        self.btn_restore_steps.setToolTip(tx(
            "Riporta ogni passo alla scelta iniziale: posizioni dei pad e quote del corpo su "
            "{model}, tutti gli altri sul modello predefinito. Il modello predefinito e la sorgente non "
            "cambiano.", model=DEFAULT_PAD_POSITIONS_MODEL))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.btn_restore_steps.clicked.connect(self._restore_step_defaults)
        layout.addWidget(buttons)

        self._refresh_single()
        self._refresh_key_state()

    # -- step table ------------------------------------------------------------------------

    def _build_table(self) -> None:
        """Fill the table: one caption row per group, then one row per step of that group."""
        groups = list(dict.fromkeys(item.group for item in STEPS))
        self.table.setRowCount(len(groups) + len(STEPS))
        row = 0
        for group in groups:
            caption = QTableWidgetItem(tx(group))
            caption.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = caption.font()
            font.setBold(True)
            caption.setFont(font)
            caption.setData(_FIELD_ROLE, None)
            self.table.setItem(row, COL_STEP, caption)
            self.table.setSpan(row, COL_STEP, 1, 3)
            row += 1
            for item in (step for step in STEPS if step.group == group):
                title = QTableWidgetItem(tx(item.title))
                title.setFlags(Qt.ItemFlag.ItemIsEnabled)
                # The hint is drawn under the title (StepCellDelegate) and
                # repeated as tooltip for an elided line.
                title.setData(HINT_ROLE, tx(item.hint))
                title.setToolTip(tx(item.hint))
                title.setData(_FIELD_ROLE, item.field)
                self.table.setItem(row, COL_STEP, title)
                model = QTableWidgetItem()
                model.setFlags(Qt.ItemFlag.ItemIsEnabled)
                model.setData(_FIELD_ROLE, item.field)
                self.table.setItem(row, COL_MODEL, model)
                self.table.setCellWidget(row, COL_ACTIONS, self._row_buttons(item, row))
                row += 1
        self._refresh_rows()

    def _row_buttons(self, item, row: int) -> QWidget:
        """The «Cambia…» and «Usa il predefinito» buttons of one step, named after the step for screen readers."""
        cell = QWidget()
        cell_layout = QHBoxLayout(cell)
        cell_layout.setContentsMargins(2, 1, 2, 1)
        cell_layout.setSpacing(4)
        title = tx(item.title)
        hint = tx(item.hint)
        change = QPushButton(tx("Cambia…"))
        change.setObjectName(f"step_change_{item.field}")
        change.setAutoDefault(False)
        change.setToolTip(tx("Scegli un modello solo per il passo «{step}».", step=title)
                          + "\n" + hint)
        change.setAccessibleName(tx("Cambia il modello del passo «{step}»", step=title))
        change.setAccessibleDescription(hint)
        change.clicked.connect(lambda checked=False, field=item.field: self._pick_step_model(field))
        # An action, shown only when it does something (finding 4): a step
        # already on the default model hides it, keeping its room so the
        # «Cambia…» buttons stay aligned.
        single = QPushButton(tx("Usa il predefinito"))
        single.setObjectName(f"step_single_{item.field}")
        single.setAutoDefault(False)
        single.setToolTip(tx("Il passo «{step}» torna al modello predefinito.", step=title))
        single.setAccessibleName(tx("Riporta il passo «{step}» al modello predefinito", step=title))
        single.setAccessibleDescription(hint)
        policy = single.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        single.setSizePolicy(policy)
        single.clicked.connect(lambda checked=False, field=item.field: self._step_follows_single(field))
        for button in (change, single):
            # Already translated and composed: the generic retranslation must
            # not look them up again as catalogue sources.
            button.setProperty("i18n_skip", True)
            # Compact rows: the application's 6 px button padding would make
            # every row taller than its two lines of text.
            button.setStyleSheet("padding: 3px 10px;")
            button.installEventFilter(self)
            self._button_rows[button] = row
            cell_layout.addWidget(button)
        self._rows[item.field] = (row, change, single)
        return cell

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        """@brief Qt override: a row button taking the focus scrolls its row into view.

        @param watched The object the event is for.
        @param event The event.
        @return Always False: the event goes on to the button.
        @note Tab moves the focus to buttons inside the table's cells, which
            the table itself never scrolls to (finding 3).
        """
        if event.type() == QEvent.Type.FocusIn and watched in self._button_rows:
            self.table.scrollTo(self.table.model().index(self._button_rows[watched], COL_STEP))
        return super().eventFilter(watched, event)

    def _own_step_count(self) -> int:
        """How many steps have a model of their own."""
        return sum(1 for model in self._steps.values() if model)

    def _refresh_section_title(self) -> None:
        """Title of the step section with its live count (finding: the count tells what is inside)."""
        count = self._own_step_count()
        if count == 0:
            what = tx("nessuno")
        elif count == 1:
            what = tx("1 passo con un modello proprio")
        else:
            what = tx("{n} passi con un modello proprio", n=count)
        self.steps_box.setTitle(tx("Modelli diversi per singoli passi ({count})", count=what))

    def _refresh_rows(self) -> None:
        """Show in every row the model the step really uses: an own model stands out, a following one is muted."""
        for field, (row, _change, single) in self._rows.items():
            own = self._steps.get(field, "")
            cell = self.table.item(row, COL_MODEL)
            font = cell.font()
            font.setItalic(False)
            cell.setIcon(QIcon())
            capability = self._step_image_capability(field, own)
            if own and capability == IMAGES_NOT_READ:
                # Shown without opening the picker: at the analysis this step
                # falls back to the default model (prompt_models.usable_model).
                cell.setText(own)
                cell.setToolTip(tx("Il modello {model} non legge immagini (dichiarato da "
                                   "OpenRouter per {source}) e questo passo manda immagini: "
                                   "all'analisi il passo userà il modello predefinito. Scegline "
                                   "un altro con «Cambia…».", model=own, source=catalog_id(own)))
                cell.setForeground(WARNING_COLOR)
                cell.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning))
                font.setBold(True)
            elif own:
                cell.setText(own)
                # The full id first: the cell elides it on a small screen.
                tooltip = own + "\n" + tx("Modello proprio di questo passo.")
                if capability == IMAGES_UNDECLARED:
                    # A subtle note only: undeclared is never a refusal.
                    tooltip += "\n" + tx("Capacità immagini non dichiarata: va verificata sulla "
                                         "documentazione del fornitore.")
                cell.setToolTip(tooltip)
                # No colour of its own: the style sheet's table text colour applies.
                # The table palette's Text is not that colour under the application
                # style sheet (it drew the ids black on the dark table, practical
                # test 2026-11-14).
                cell.setData(Qt.ItemDataRole.ForegroundRole, None)
                font.setBold(True)
            else:
                # The default model is written once, above (finding 12).
                cell.setText(tx("= modello predefinito"))
                tooltip = tx("Segue il modello predefinito ({model}).", model=self.model())
                # The default model's image capability, for an image step that
                # follows it: the same subtle note as an own undeclared model
                # (the warning itself is on the default model's row).
                default_capability = self._step_image_capability(field, self.model())
                if default_capability == IMAGES_UNDECLARED:
                    tooltip += "\n" + tx("Capacità immagini non dichiarata: va verificata sulla "
                                          "documentazione del fornitore.")
                cell.setToolTip(tooltip)
                cell.setForeground(MUTED_COLOR)
                font.setBold(False)
            cell.setFont(font)
            single.setVisible(bool(own))
        self._refresh_section_title()

    def _step_image_capability(self, field: str, model: str):
        """The image capability of a step's own model, or None when it does not matter.

        None for a step that sends no image or follows the default model;
        otherwise openrouter.image_capability, the rule the picker and the
        send-time check share. The parsed catalogue is shared and keyed on
        the cache file's version (openrouter.fresh_catalog): a refresh made
        by the picker is seen at the next row refresh, without a re-parse
        of an unchanged file.
        """
        needs_images = next((item.images for item in STEPS if item.field == field), True)
        if not model or not needs_images:
            return None
        return image_capability(model)

    def _step_capabilities(self, field: str) -> tuple:
        """The capabilities the picker requires for the step `field`."""
        for item in STEPS:
            if item.field == field:
                if not item.images:
                    return _TEXT_ONLY_CAPABILITIES
                return _GROUP_CAPABILITIES.get(item.group, PHOTO_CAPABILITIES)
        return DATASHEET_CAPABILITIES

    def _pick_step_model(self, field: str) -> None:
        """«Cambia…»: choose the model of one step among the models of the sources with a key."""
        title = next((tx(item.title) for item in STEPS if item.field == field), field)
        following = not self._steps.get(field)
        chosen = pick_model(self._steps.get(field) or self.model(), self, title=title,
                            capabilities=self._step_capabilities(field),
                            providers=self._keyed_providers())
        if chosen and following and chosen.strip() == self.model():
            # OK on the default the picker opened with is not a choice: the
            # step keeps following the default model (it would otherwise be
            # pinned and stop following a later change of the default).
            chosen = None
        if chosen:
            self._steps[field] = chosen.strip()
        # Redrawn even without a choice: the picker may have refreshed the
        # catalogue, and the marks follow it.
        self._refresh_single()
        self._refresh_rows()
        self._refresh_key_state()

    def _step_follows_single(self, field: str) -> None:
        """«Usa il predefinito»: the step goes back to the default model."""
        self._steps[field] = ""
        self._refresh_rows()
        self._refresh_key_state()

    # -- size --------------------------------------------------------------------------------

    def _available_geometry(self) -> QRect:
        """@brief The available area of the screen the window opens on (task bar excluded).

        @return QScreen.availableGeometry of the window's screen, the
            parent's or the primary one; an empty QRect when there is none.
        """
        screen = self.screen()
        if screen is None and self.parentWidget() is not None:
            screen = self.parentWidget().screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        """@brief Qt override: on every show, fit the window to its current content.

        @param event The show event.
        @note Here and not in __init__: the application style sheet's font and
            the retranslation the caller applies after construction change the
            row and column sizes (finding 2: the step column was measured with
            the font of an unpolished table and cut the titles).
        """
        super().showEvent(event)
        if not event.spontaneous():
            self._fit_to_rows()

    def _fit_to_rows(self) -> None:
        """@brief Compact window while the step section is closed; every row visible while it is open.

        @note Computed from the real sizes after polishing, never from a
            fixed pixel count, and capped to the screen's available area; the
            window stays centred where it was.
        """
        self.ensurePolished()
        # The centre before any resize below: the window is re-centred on it.
        start_centre = self.geometry().center()
        start_size = (self.width(), self.height())
        layout = self.layout()
        open_section = not self.steps_box.isHidden() and self.steps_box.isExpanded()
        layout.setStretch(self._spare_index, 0 if open_section else 1)
        layout.activate()
        area = self._available_geometry()
        frame = max(self.frameGeometry().height() - self.height(), 0) or _FRAME_ALLOWANCE
        max_height = area.height() - frame if area.isValid() else 10 ** 6
        max_width = area.width() - 16 if area.isValid() else 10 ** 6
        width = self.width()
        if self.steps_box.isHidden() or not self.steps_box.isExpanded():
            # Collapsed section, or no key at all: the natural height only.
            height = min(self.sizeHint().height(), max_height)
        else:
            table = self.table
            header = table.horizontalHeader()
            step_rows = [row for row, _change, _single in self._rows.values()]
            step_width = max((table.sizeHintForIndex(table.model().index(row, COL_STEP)).width()
                              for row in step_rows), default=header.sectionSize(COL_STEP))
            step_width = max(step_width, header.sectionSizeHint(COL_STEP))
            # The model column holds its longest text (header included); only
            # a screen too small for it caps it at a third of the width.
            model_width = min(self._model_column_width(), max(200, max_width // 3))
            header.resizeSection(COL_MODEL, model_width)
            # Width: the step column at its natural width, the model column,
            # the buttons; capped to the screen, the step column then wraps.
            actions = max(header.sectionSizeHint(COL_ACTIONS),
                          table.sizeHintForColumn(COL_ACTIONS))
            scrollbar = table.verticalScrollBar().sizeHint().width()
            wanted_table = (step_width + model_width + actions + scrollbar
                            + 2 * table.frameWidth())
            width = min(max(width, width + wanted_table - table.width()), max_width)
            if width != self.width():
                self.resize(width, self.height())
                layout.activate()
            table.resizeRowsToContents()
            # Height: every row, the header and the frame, whatever the rest takes.
            rows = sum(table.rowHeight(row) for row in range(table.rowCount()))
            wanted = rows + header.height() + 2 * table.frameWidth()
            height = min(self.height() + max(0, wanted - table.height()), max_height)
        if (width, height) == start_size:
            return
        centre = start_centre
        self.resize(width, height)
        if area.isValid() and self.isVisible():
            geometry = self.geometry()
            geometry.moveCenter(centre)
            # Keep the frame inside the available area (title bar included).
            left = min(max(geometry.left(), area.left()), area.right() - geometry.width())
            top = min(max(geometry.top(), area.top() + frame // 2),
                      area.bottom() - geometry.height() - frame // 2)
            self.move(max(area.left(), left), max(area.top(), top))

    def _model_column_width(self) -> int:
        """The width the «Modello usato» column needs: its header and its longest text, with the icon.

        At least 200 px, so an id chosen later rarely needs eliding.
        """
        table = self.table
        header = table.horizontalHeader()
        metrics = QFontMetrics(table.font())
        bold = QFont(table.font())
        bold.setBold(True)
        bold_metrics = QFontMetrics(bold)
        icon = table.iconSize().width() if table.iconSize().isValid() else 16
        widest = 0
        for row, _change, _single in self._rows.values():
            cell = table.item(row, COL_MODEL)
            if cell is None:
                continue
            used = bold_metrics if cell.font().bold() else metrics
            widest = max(widest, used.horizontalAdvance(cell.text())
                         + (icon + 6 if not cell.icon().isNull() else 0))
        return max(200, header.sectionSizeHint(COL_MODEL), widest + 2 * _CELL_PADDING + 8)

    def _on_column_resized(self, index: int, _old: int, _new: int) -> None:
        """A resized step column re-measures the rows (its hints wrap differently)."""
        if index == COL_STEP:
            self.table.resizeRowsToContents()

    # -- help ------------------------------------------------------------------------------

    def _info_button(self) -> QPushButton:
        """Keep the explanation out of the window, with a keyboard-accessible help button."""
        title = "Modello AI"
        button = QPushButton()
        button.setAutoDefault(False)
        button.setIcon(QIcon(str(Path(__file__).parent / "assets" / "info.svg")))
        button.setIconSize(QSize(20, 20))
        button.setFixedSize(32, 32)
        info_label = (
            self.i18n.tr("tooltip.info_section", section=self.i18n.text(title))
            if self.i18n is not None else f"Informazioni: {title}"
        )
        button.setToolTip(info_label)
        button.setAccessibleName(info_label)
        button.setObjectName("analysis_info_0")
        button.clicked.connect(lambda checked=False: self._show_info())
        self.info_buttons.append(button)
        return button

    def _show_info(self) -> None:
        """Open the read-only explanation of the window (catalogue key ``help.settings.model``)."""
        dialog = QDialog(self)
        prefix = self.i18n.text("Informazioni") if self.i18n is not None else "Informazioni"
        dialog.setWindowTitle(f"{prefix} — {self.windowTitle()}")
        dialog.setModal(True)
        dialog.resize(640, 360)
        layout = QVBoxLayout(dialog)
        explanation = QTextEdit()
        explanation.setReadOnly(True)
        explanation.setPlainText(
            self.i18n.tr("help.settings.model", default=self._help_text)
            if self.i18n is not None else self._help_text
        )
        layout.addWidget(explanation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Chiudi")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(dialog)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    # -- default model ---------------------------------------------------------------------

    def source(self) -> str:
        """@brief The source of the default model: the provider of the model itself.

        @return A providers.PROVIDERS key.
        """
        return provider_of(self.model())

    def _set_single_model(self, model: str) -> None:
        """Show `model` as the default model, with its source, and in every row that follows it."""
        self._model = model
        self._refresh_single()
        self._refresh_rows()

    def _refresh_single(self) -> None:
        """The default model's id and its source in parentheses, marked when it cannot read images.

        The default model serves every step without a model of its own, and
        every step that sends images refuses a model declared text only
        (openrouter.image_capability, the picker's rule): the row says so in
        the warning colour, with the warning icon. Undeclared is never marked.
        """
        self.field_model.setText(self._model)
        source = self.source()
        self.label_source.setText("(" + tx(PROVIDER_LABELS.get(source, source)) + ")")
        text_only = (any(item.images for item in STEPS)
                     and image_capability(self._model) == IMAGES_NOT_READ)
        if text_only:
            tooltip = tx("Il modello predefinito {model} non legge immagini (dichiarato da "
                         "OpenRouter per {source}): i passi che mandano immagini e lo usano non "
                         "possono leggerle. Scegline un altro con «Cambia…».",
                         model=self._model, source=catalog_id(self._model))
            self.field_model.setStyleSheet(f"color: {WARNING_COLOR.name()};")
        else:
            tooltip = ""
            self.field_model.setStyleSheet("")
        self.field_model.setToolTip(tooltip)
        self.label_default_warning.setToolTip(tooltip)
        self.label_default_warning.setVisible(text_only)

    @staticmethod
    def _key_present(provider: str) -> bool:
        """Whether `provider` has a key (never reads the key itself, never the network)."""
        try:
            if provider == PROVIDER_DEEPSEEK:
                from ..services.providers import deepseek_key_source
                kind = deepseek_key_source().kind
            else:
                from ..services.openrouter import api_key_source
                kind = api_key_source().kind
        except Exception:  # noqa: BLE001 - an unreadable state does not block the window
            kind = "none"
        return kind != "none"

    def _keyed_providers(self) -> tuple:
        """The sources that have a key, in providers.PROVIDERS order (never reads a key, never the network)."""
        return tuple(provider for provider in PROVIDERS if self._key_present(provider))

    def _blocks_without_key(self, field: str) -> bool:
        """Whether a missing key of the step's source stops a paid run (a saved or changed choice).

        A step on the pad steps' unsaved factory reader instead falls back on
        the default model (prompt_models.usable_model), as the key check of
        MainWindow._chosen_step_models counts only saved choices.
        """
        return field in self._stored_fields or self._steps.get(field) != self._initial_steps.get(field)

    def _refresh_key_state(self) -> None:
        """Warn, in one row, about every model whose source has no key; with no key at all, only that row.

        With no key the model controls are hidden and the row says so, with
        «Account AI...». Otherwise the row appears only when the single
        model or a step model belongs to a source without a key, naming the
        steps and what happens to them (finding 7), in the warning colour;
        with every key present nothing is shown.
        """
        keyed = self._keyed_providers()
        none = not keyed
        self.single_row.setVisible(not none)
        self.steps_box.setVisible(not none)
        self.btn_restore_steps.setVisible(not none)
        lines = []
        if none:
            lines.append(tx("Nessuna sorgente AI ha una chiave: inseriscine una con «Account AI...» "
                            "per scegliere i modelli."))
        else:
            if self.source() not in keyed:
                lines.append(tx("Chiave {source} mancante per il modello predefinito {model}: un'analisi "
                                "non parte finché non la inserisci o non scegli un altro modello.",
                                source=tx(PROVIDER_LABELS.get(self.source(), self.source())),
                                model=self.model()))
            by_provider: dict[str, list] = {}
            for item in STEPS:
                model = self._steps.get(item.field, "")
                if model and provider_of(model) not in keyed:
                    by_provider.setdefault(provider_of(model), []).append(item)
            for provider, items in by_provider.items():
                name = tx(PROVIDER_LABELS.get(provider, provider))
                blocked = [tx(item.title) for item in items if self._blocks_without_key(item.field)]
                fallback = [tx(item.title) for item in items
                            if not self._blocks_without_key(item.field)]
                if blocked:
                    lines.append(tx("Chiave {source} mancante per: {steps}. Un'analisi che usa "
                                    "questi passi non parte finché non la inserisci.",
                                    source=name, steps=", ".join(blocked)))
                if fallback:
                    lines.append(tx("Chiave {source} mancante per: {steps}. Finché manca, questi "
                                    "passi li esegue il modello predefinito.",
                                    source=name, steps=", ".join(fallback)))
        # One line per case: the default model, the steps that stop a run, those that fall back.
        self.label_keys_used.setText("\n".join(lines))
        self.keys_row.setVisible(bool(lines))

    def _open_account(self) -> None:
        """«Account AI...»: open the keys window, then read the key state again (a new key shows the models)."""
        if callable(self._account_opener):
            self._account_opener()
        else:
            from .ai_account_dialog import AiAccountDialog
            dialog = AiAccountDialog(self)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()
        self._refresh_key_state()
        if self.isVisible():
            self._fit_to_rows()

    def _pick_model(self) -> None:
        """«Cambia…» of the default model: the models of the sources that have a key."""
        chosen = pick_model(self._model, self, title="Modello", providers=self._keyed_providers())
        if chosen:
            self._set_single_model(chosen.strip())
        else:
            # The picker may have refreshed the catalogue: the marks follow it.
            self._refresh_single()
            self._refresh_rows()
        self._refresh_key_state()

    def _restore_step_defaults(self) -> None:
        """«Ripristina passi predefiniti»: the step table back to the factory choice; default model kept."""
        self._steps = factory_step_models()
        self._refresh_rows()
        self._refresh_key_state()

    # -- results ---------------------------------------------------------------------------

    def step_models(self) -> dict[str, str]:
        """@brief The model of every step, as the table shows it.

        @return {prompt field: model identifier, or "" for the default model},
            one entry per prompt_models.STEPS field (a copy).
        """
        return dict(self._steps)

    def changed_step_models(self) -> dict[str, str]:
        """@brief Only the steps whose model differs from the one the window opened with.

        @return {prompt field: model identifier or ""}; empty when nothing changed.
        @note The save path stores only these (finding B): an untouched pad
            step that follows the old «Posizioni pad» default must not become
            a saved choice by a mere OK.
        """
        return {field: model for field, model in self._steps.items()
                if model != self._initial_steps.get(field, "")}

    def pad_model(self) -> str:
        """@brief The model of the pad-position step (the old «Posizioni pad» field).

        @return The stripped identifier, or "" when it follows the default model.
        """
        return self._steps.get(LEGACY_PAD_STEPS[0], "").strip()

    def verification_model(self):
        """@brief The default model (kept for callers that save the default per stage).

        @return The same value as `model()`.
        """
        return self.model()

    def vision_model(self) -> str:
        """@brief The default model (kept for callers that save the default per stage).

        @return The same value as `model()`.
        """
        return self.model()

    def model(self) -> str:
        """@brief Return the single chosen model, or DEFAULT_MODEL when empty.

        @return The stripped model identifier, or DEFAULT_MODEL.
        """
        return self._model.strip() or DEFAULT_MODEL
