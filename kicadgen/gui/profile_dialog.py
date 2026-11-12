"""@brief SQLite profile CRUD. Editors stage changes locally until explicit Save."""
from pathlib import Path
import sqlite3

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ..services import prompt_catalog
from ..services.analysis_profiles import FAMILIES, ProfileError
from ..services.messages import tx

ASSETS = Path(__file__).parent / 'assets'
STAGES = (
    ('Datasheet', 'model', 'system_prompt'),
    ('Localizzazione foto', 'vision_model', 'grounding_prompt'),
    ('Verifica foto', 'verification_model', 'verification_prompt'),
)
HELP = (
    'Legge quote e parametri dai documenti. Il prompt base definisce il comportamento '
    'dell’analisi; le istruzioni specifiche della tipologia vengono aggiunte al prompt '
    'del componente nel tab Progetto, dove puoi aggiungere note per la singola estrazione.',
    'Localizza gli ingombri nelle foto calibrate. Tutte le chiamate usano il modello unico '
    'del tab «Modello» delle Impostazioni; il profilo sceglie solo i prompt. Le istruzioni foto '
    'sono aggiunte ai prompt fotografici, inclusa la verifica, '
    'ma non al prompt Lettura ritagli: descrivono la scheda intera, non il singolo corpo. '
    'I prompt tecnici Terminali e Reofori sono avanzati: modificarli non riattiva i pulsanti '
    'manuali nascosti. Il prompt Lettura ritagli guida la rilettura di ogni corpo dal suo '
    'ritaglio ingrandito (corpi che la rete SMD locale non risolve; rilevatori «YOLO locale + '
    'verifica AI» e «Solo modello di visione»). '
    'XY sono stime calibrate e le altezze richiedono conferma. '
    'La cache nella cartella photo-cache del progetto distingue input, modello e prompt.',
    'Verifica la presenza dei corpi nei rettangoli proposti, senza spostare coordinate '
    'o aggiungere ID. Usa il modello unico del tab «Modello» delle Impostazioni.',
)


def _find_i18n(widget):
    """@brief Return the translation service owned by the nearest parent."""
    current = widget
    while current is not None:
        i18n = getattr(current, "i18n", None)
        if i18n is not None:
            return i18n
        current = current.parentWidget() if hasattr(current, "parentWidget") else None
    return None


def icon_button(text, asset, callback):
    """@brief Create a non-default push button with a 20 px asset icon wired to `callback`.

    @param text Button caption.
    @param asset File name of the icon inside the GUI assets folder.
    @param callback Slot connected to the button's `clicked` signal.
    @return The configured QPushButton.
    """
    button = QPushButton(text)
    button.setIcon(QIcon(str(ASSETS / asset)))
    button.setIconSize(QSize(20, 20))
    button.setAutoDefault(False)
    button.clicked.connect(callback)
    return button


def show_info(parent, title, text, *, text_key=None, **values):
    """@brief Show a modal read-only information dialog, translated when an i18n catalogue is reachable.

    @param parent Widget whose ancestry is searched for an i18n catalogue;
        also the dialog's parent.
    @param title Window title (translated as a source string when possible).
    @param text Body text, used verbatim when no catalogue or no `text_key`.
    @param text_key Catalogue key of the body text; `text` is its default.
    @param values Placeholder values formatted into the translated body.
    """
    i18n = _find_i18n(parent)
    show_text(
        parent,
        (i18n.text('Informazioni') + ' — ' + i18n.text(title))
        if i18n is not None else 'Informazioni — ' + title,
        i18n.tr(text_key, default=text, **values)
        if i18n is not None and text_key else text)


def show_text(parent, window_title, text, *, monospace=False):
    """@brief Show a modal read-only text dialog with a Close button, released after it closes.

    @param parent Owner widget; its ancestry gives the i18n catalogue that
        translates the Close button.
    @param window_title Window title, already in the user's language.
    @param text Body text, already in the user's language, shown as plain text.
    @param monospace True to show the text in a fixed-width font (a diff).
    @note The one modal helper of this file: `show_info` and the prompt diff
        («Mostra differenze») both go through it, so both are released with
        deleteLater after exec (a dialog kept alive can crash a later test
        through the garbage collector).
    """
    i18n = _find_i18n(parent)
    dialog = QDialog(parent)
    dialog.setWindowTitle(window_title)
    dialog.setModal(True)
    dialog.resize(660, 380)
    layout = QVBoxLayout(dialog)
    editor = QTextEdit()
    editor.setReadOnly(True)
    editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
    if monospace:
        editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    editor.setPlainText(text)
    layout.addWidget(editor)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.button(QDialogButtonBox.StandardButton.Close).setText('Chiudi')
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if i18n is not None:
        i18n.retranslate_widgets(dialog)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()


class ProfileEditor(QDialog):
    """@brief Edit one AI profile or the shared defaults."""
    def __init__(self, store, key='', parent=None, *, new=False):
        """@brief Build the editor for one component type, a new one, or the shared defaults.

        @param store Profile store (read on open, written only on OK).
        @param key Component type key to edit; "" with `new` False edits the
            shared defaults.
        @param parent Owner window; its ``i18n`` translates the labels.
        @param new True to create a new type (the key is typed in the form).
        """
        super().__init__(parent)
        self.i18n = _find_i18n(parent)
        self.store, self.key, self.new = store, key, new
        self.is_type = bool(key) or new
        self.setWindowTitle('Tipologia e profilo AI' if self.is_type else 'Default generali AI')
        self.resize(900, 720)
        self.setModal(True)
        self.defaults = store.defaults()
        overrides = store.overrides(key) if key else {}
        self._overrides = overrides
        row = store.get_type(key) if key else None
        self.fields, self.inherit = {}, {}
        # Prompt status widgets, by field («instructions» for the built-in
        # type's instructions): the label, «Mostra differenze» (show the
        # differences) and «Ripristina predefinito» (restore the default),
        # plus the registry entry each one is compared with.
        self.status_labels, self.diff_buttons, self.restore_buttons = {}, {}, {}
        self._entries = {}
        layout = QVBoxLayout(self)
        if self.is_type:
            form = QFormLayout()
            self.name = QLineEdit(row['label'] if row else '')
            self.family = QComboBox()
            for family, label in FAMILIES.items():
                localized = self.i18n.component_type(family, label) if self.i18n else label
                self.family.addItem(localized, family)
            if row:
                self.family.setCurrentIndex(self.family.findData(row['family']))
            self.active = QCheckBox('Attiva nell’elenco di importazione')
            self.active.setChecked(bool(row['active']) if row else True)
            form.addRow('Nome:', self.name)
            form.addRow('Famiglia geometrica:', self.family)
            form.addRow('', self.active)
            layout.addLayout(form)
        self.tabs = QTabWidget()
        self.info_buttons = []
        layout.addWidget(self.tabs, 1)
        for index, (title, model, prompt) in enumerate(STAGES):
            page = QWidget()
            column = QVBoxLayout(page)
            self.tabs.addTab(page, title)
            header = QHBoxLayout()
            # No model per component type (user, 2026-10-02): the model is
            # unique, chosen in the «Modello» (model) tab of the LLM settings.
            header.addStretch(1)
            info = icon_button('', 'info.svg', lambda checked=False, i=index: self._info(i))
            info_label = (
                self.i18n.tr("tooltip.info_section", section=self.i18n.text(title))
                if self.i18n is not None else 'Informazioni: ' + title
            )
            info.setAccessibleName(info_label)
            info.setToolTip(info_label)
            info.setFixedSize(32, 32)
            self.info_buttons.append(info)
            header.addWidget(info)
            column.addLayout(header)
            if index == 1:
                sub = QTabWidget()
                column.addWidget(sub, 1)
                for field, caption in ((prompt, 'Prompt base'), ('image_instructions', 'Istruzioni foto'),
                                       ('terminal_prompt', 'Terminali'), ('lead_prompt', 'Reofori'),
                                       ('crop_prompt', 'Lettura ritagli')):
                    child = QWidget()
                    inner = QVBoxLayout(child)
                    sub.addTab(child, caption)
                    self._add_field(inner, field, overrides)
                self._add_registry_fields(sub, None, prompt_catalog.STAGE_PHOTO, overrides)
            elif index == 0:
                sub = QTabWidget()
                column.addWidget(sub, 1)
                for field, caption in ((prompt, 'Schede e moduli'),
                                       ('system_prompt_single', 'Componente singolo'),
                                       ('numbering_prompt', 'Numerazione pin')):
                    child = QWidget()
                    inner = QVBoxLayout(child)
                    sub.addTab(child, caption)
                    self._add_field(inner, field, overrides)
                self._add_registry_fields(sub, None, prompt_catalog.STAGE_DATASHEET, overrides)
                if self.is_type:
                    self.instructions = QTextEdit()
                    self.instructions.setAcceptRichText(False)
                    self.instructions.setPlainText(row['instructions'] if row else '')
                    # A built-in type's instructions have a program default:
                    # they get the same status row as the prompts. A custom
                    # type's have none, so the editor stands alone.
                    entry = (prompt_catalog.instruction_entry(key)
                             if row and row['builtin'] else None)
                    if entry is None:
                        sub.addTab(self.instructions, 'Istruzioni tipologia')
                    else:
                        child = QWidget()
                        inner = QVBoxLayout(child)
                        inner.addWidget(self.instructions, 1)
                        self._add_status_row(inner, 'instructions', self.instructions, entry)
                        sub.addTab(child, 'Istruzioni tipologia')
            else:
                column.addWidget(QLabel('Prompt base:'))
                self._add_field(column, prompt, overrides)
                self._add_registry_fields(None, column, prompt_catalog.STAGE_VERIFICATION, overrides)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Salva')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Annulla')
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)

    def _add_field(self, layout, key, overrides):
        """Prompt editor of `key`, with «Eredita default generale» on a type profile.

        Only prompts are edited here: the model is one for every analysis and
        is chosen in the «Modello» tab of the LLM settings (user, 2026-10-02).
        """
        value = overrides.get(key, self.defaults[key])
        editor = QTextEdit()
        editor.setAcceptRichText(False)
        editor.setPlainText(value)
        editor.setObjectName(key)
        self.fields[key] = editor
        if self.is_type:
            inherit = QCheckBox('Eredita default generale')
            inherit.setChecked(key not in overrides)
            self.inherit[key] = inherit
            layout.addWidget(inherit)
            # Read-only rather than disabled: inherited prompts remain scrollable/copyable.
            editor.setReadOnly(inherit.isChecked())
        layout.addWidget(editor, 1)
        try:
            entry = prompt_catalog.entry(key)
        except KeyError:
            entry = None
        if entry is not None:
            self._add_status_row(layout, key, editor, entry)
        if self.is_type:
            inherit.toggled.connect(lambda checked, field=editor, name=key:
                                    self._inherit_changed(name, checked, field))

    def _add_registry_fields(self, tabs, column, stage, overrides):
        """Editors of the registry prompts of `stage` that the hand-made layout does not place.

        A prompt the analysis reads from the profile (prompt_catalog entry
        with `in_profile` True) and that has no editor yet gets one, in a
        sub-tab of `tabs` captioned with its registry title, or below
        `column` when the stage has no sub-tabs. Entries sharing a `group`
        (the six document roles, the three inventory prompts) go into ONE
        sub-tab of that name holding a nested tab per entry, so a stage keeps
        a readable row of sub-tabs (step P2, 2026-11-11). Entries with
        `in_profile` False are never editable here: the texts the program
        keeps in the code (prompt_catalog.CODE_ONLY_PROMPTS) are listed
        read-only, with their reason, in a last sub-tab «Testi nel codice».
        """
        groups = {}
        for entry in prompt_catalog.PROMPTS:
            if entry.stage != stage or not entry.in_profile or entry.field in self.fields:
                continue
            if entry.field not in self.defaults:
                continue
            if tabs is not None:
                target = tabs
                if entry.group:
                    target = groups.get(entry.group)
                    if target is None:
                        target = groups[entry.group] = QTabWidget()
                        tabs.addTab(target, entry.group)
                child = QWidget()
                inner = QVBoxLayout(child)
                target.addTab(child, entry.title)
                self._add_field(inner, entry.field, overrides)
            else:
                column.addWidget(QLabel(entry.title))
                self._add_field(column, entry.field, overrides)
        code_only = [entry for entry in prompt_catalog.CODE_ONLY_PROMPTS if entry.stage == stage]
        if code_only and tabs is not None:
            tabs.addTab(self._code_only_page(code_only), 'Testi nel codice')

    def _code_only_page(self, entries):
        """Read-only page of the texts kept in the code: title, reason and the text itself.

        Nothing here is saved: the texts are not profile fields
        (prompt_catalog.CODE_ONLY_PROMPTS). They are shown so that no text
        sent to a model is hidden from the user.
        """
        page = QWidget()
        inner = QVBoxLayout(page)
        for entry in entries:
            title = QLabel(entry.title)
            title.setStyleSheet('font-weight: bold;')
            inner.addWidget(title)
            reason = QLabel(entry.reason)
            reason.setWordWrap(True)
            inner.addWidget(reason)
            text = QTextEdit()
            text.setAcceptRichText(False)
            text.setReadOnly(True)
            text.setObjectName(entry.field)
            # The prompt itself is model input, not interface text.
            text.setProperty('i18n_skip', True)
            text.setPlainText(entry.current_default())
            inner.addWidget(text, 1)
        return page

    def _add_status_row(self, layout, key, editor, entry):
        """Status label, «Mostra differenze» and «Ripristina predefinito» under one prompt editor.

        The label is re-composed at every keystroke from the editor's text, so
        it is translated here and marked `i18n_skip`: retranslate_widgets must
        not take a status already shown as the source of the next lookup.
        """
        self._entries[key] = entry
        row = QHBoxLayout()
        label = QLabel()
        label.setObjectName(f'{key}_status')
        label.setProperty('i18n_skip', True)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        diff = QPushButton('Mostra differenze')
        diff.setAutoDefault(False)
        diff.clicked.connect(lambda checked=False, name=key: self._show_diff(name))
        restore = QPushButton('Ripristina predefinito')
        restore.setAutoDefault(False)
        restore.clicked.connect(lambda checked=False, name=key: self._restore_default(name))
        row.addWidget(diff)
        row.addWidget(restore)
        layout.addLayout(row)
        self.status_labels[key] = label
        self.diff_buttons[key] = diff
        self.restore_buttons[key] = restore
        editor.textChanged.connect(lambda name=key: self._refresh_status(name))
        self._refresh_status(key)

    def _status_editor(self, key):
        """The editor whose text the status row of `key` describes."""
        return self.instructions if key == 'instructions' else self.fields[key]

    def _t(self, source):
        """Catalog text of an Italian source, or the source without a catalogue."""
        return self.i18n.text(source) if self.i18n is not None else source

    def prompt_status(self, key):
        """@brief Live status of the text in the editor of `key`.

        @param key Prompt field, or "instructions" for a built-in type.
        @return prompt_catalog CURRENT, CUSTOM or OUTDATED.
        """
        return prompt_catalog.entry_status(self._entries[key], self._status_editor(key).toPlainText())

    def _refresh_status(self, key):
        """Update the status label and the two buttons of `key` from the editor's current text."""
        status = self.prompt_status(key)
        label = self.status_labels[key]
        label.setText(self._t(prompt_catalog.status_label(status)))
        # An outdated text is the one that needs the user's eye.
        label.setStyleSheet('font-weight: bold;' if status == prompt_catalog.OUTDATED else '')
        editor = self._status_editor(key)
        self.diff_buttons[key].setEnabled(status != prompt_catalog.CURRENT)
        self.restore_buttons[key].setEnabled(
            status != prompt_catalog.CURRENT and not editor.isReadOnly())

    def _show_diff(self, key):
        """Read-only dialog with the diff from the editor's text to the current default."""
        entry = self._entries[key]
        lines = prompt_catalog.entry_diff(entry, self._status_editor(key).toPlainText())
        text = '\n'.join(lines) if lines else tx(
            "Nessuna differenza dal predefinito di questa versione.")
        title = self._t('Differenze dal predefinito') + ' — ' + self._t(entry.title)
        show_text(self, title, text, monospace=True)

    def _restore_default(self, key):
        """Put the current default in the editor of `key`; saved on OK like any edit, dropped by Cancel."""
        editor = self._status_editor(key)
        if editor.isReadOnly():
            return
        editor.setPlainText(self._entries[key].current_default())

    def _inherit_changed(self, name, checked, editor):
        editor.setReadOnly(checked)
        if checked:
            editor.setPlainText(self.defaults[name])
        if name in self.status_labels:
            self._refresh_status(name)

    def _info(self, index):
        show_info(self, STAGES[index][0], HELP[index] + '\n\n'
                  'I default generali valgono per tutti i profili che li ereditano. '
                  'Togli “Eredita default generale” per salvare un valore custom nella tipologia. '
                  'Le scelte di modello nel tab Progetto valgono solo per la sessione. '
                  'Salva conferma le modifiche; Annulla le scarta. '
                  'La famiglia geometrica deve essere una di quelle supportate dal generatore. '
                  'Gli schemi JSON e i controlli geometrici sono gestiti dal codice, non dai prompt. '
                  'La chiave OpenRouter resta nel Gestore credenziali di Windows (o nel file .env), '
                  'mai nel database.',
                  text_key=f'help.profile.stage.{index}')

    def _stored_text(self, key, text):
        """Text to store for the editor of `key`: stripped, but byte-identical when unchanged.

        Every text is stripped on save, as before. When the stripped text is
        the one the field held, or the current program default, the exact
        original is stored instead: a default whose surrounding blanks are
        part of how the program joins it (an appended fragment, a trailing
        newline) must not turn into a different text just because the user
        pressed OK.
        """
        value = text.strip()
        original = self._overrides.get(key, self.defaults.get(key))
        if isinstance(original, str) and value == original.strip():
            return original
        try:
            default = prompt_catalog.restore_default(key)
        except KeyError:
            return value
        return default if value == default.strip() else value

    def _save(self):
        values = {}
        for key, editor in self.fields.items():
            values[key] = self._stored_text(key, editor.text() if isinstance(editor, QLineEdit)
                                            else editor.toPlainText())
            if self.is_type and self.inherit[key].isChecked():
                values[key] = None
        if self.is_type:
            # save_type replaces EVERY override of the type: an override of a
            # registered prompt this dialog does not show (in_profile False)
            # is carried over unchanged instead of being dropped.
            values.update({name: value for name, value in self._overrides.items()
                           if name in prompt_catalog.fields() and name not in self.fields})
        try:
            if self.is_type:
                self.key = self.store.save_type(self.key or None, label=self.name.text(),
                    family=self.family.currentData(), instructions=self.instructions.toPlainText(),
                    active=self.active.isChecked(), overrides=values)
            else:
                self.store.save_defaults(values)
        except (ProfileError, sqlite3.Error, OSError) as exc:
            title = self.i18n.text('Profilo non salvato') if self.i18n else 'Profilo non salvato'
            QMessageBox.warning(self, title, str(exc))
            return
        self.accept()


class ProfilesDialog(QDialog):
    """@brief Provide CRUD management for AI analysis profiles."""
    def __init__(self, store, parent=None):
        """@brief Build the list of profiles with its new/edit/delete commands.

        @param store Profile store the list is read from and written to.
        @param parent Owner window; its ``i18n`` translates the labels.
        @note `changed` tells the caller whether anything was written, so
            that the main window reloads its profile cache only when needed.
        """
        super().__init__(parent)
        self.i18n = _find_i18n(parent)
        self.store = store
        self.changed = False
        self.setWindowTitle('Gestione tipologie e profili AI')
        self.resize(840, 500)
        self.setModal(True)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda item: self._edit())
        self.list.currentRowChanged.connect(self._selection_changed)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.new_button = icon_button('Nuova', 'docs-add.svg', self._new)
        self.edit_button = icon_button('Modifica', 'profile-edit.svg', self._edit)
        self.copy_button = icon_button('Duplica', 'profile-copy.svg', self._duplicate)
        self.delete_button = icon_button('Elimina', 'docs-remove.svg', self._delete)
        for button in (self.new_button, self.edit_button, self.copy_button, self.delete_button):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(icon_button('Backup DB…', 'docs-open.svg', self._backup))
        info = icon_button('', 'info.svg', self._info)
        info.setAccessibleName('Informazioni gestione profili')
        info.setToolTip('Informazioni gestione profili')
        row.addWidget(info)
        layout.addLayout(row)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText('Chiudi')
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self._reload()

    def _key(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ''

    def _reload(self, key=''):
        self.list.clear()
        for data in [dict(key='', label='Default generali AI', active=True)] + self.store.list_types(True):
            label = data['label']
            if self.i18n is not None:
                label = (self.i18n.text('Default generali AI') if not data['key']
                         else self.i18n.component_type(data['key'], label))
            disabled = self.i18n.text('disattivata') if self.i18n is not None else 'disattivata'
            caption = label + (f' — {disabled}' if not data['active'] else '')
            item = QListWidgetItem(caption)
            item.setData(Qt.ItemDataRole.UserRole, data['key'])
            self.list.addItem(item)
            if data['key'] == key:
                self.list.setCurrentItem(item)
        self._selection_changed()

    def _selection_changed(self):
        enabled = bool(self._key())
        self.copy_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def _open_editor(self, key='', new=False):
        dialog = None
        try:
            dialog = ProfileEditor(self.store, key, self, new=new)
            if self.i18n is not None:
                self.i18n.retranslate_widgets(dialog)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.changed = True
                self._reload(dialog.key)
        except (ProfileError, sqlite3.Error, OSError) as exc:
            title = self.i18n.text('Profilo non disponibile') if self.i18n else 'Profilo non disponibile'
            QMessageBox.warning(self, title, str(exc))
        finally:
            if dialog is not None:
                dialog.deleteLater()

    def _new(self):
        self._open_editor(new=True)

    def _edit(self):
        self._open_editor(self._key())

    def _duplicate(self):
        if self._key():
            try:
                key = self.store.duplicate(self._key())
                self.changed = True
                self._reload(key)
            except (ProfileError, sqlite3.Error, OSError) as exc:
                title = self.i18n.text('Duplicazione non riuscita') if self.i18n else 'Duplicazione non riuscita'
                QMessageBox.warning(self, title, str(exc))

    def _delete(self):
        key = self._key()
        if not key:
            return
        type_name = self.list.currentItem().text()
        title = self.i18n.text('Elimina tipologia') if self.i18n else 'Elimina tipologia'
        question = (
            self.i18n.tr('profile.delete_question', name=type_name)
            if self.i18n else
            f'Eliminare “{type_name}” e il relativo profilo custom?\n'
            'I componenti e i file già generati non verranno modificati.'
        )
        answer = QMessageBox.question(self, title, question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete_type(key)
            self.changed = True
            self._reload()
        except (ProfileError, sqlite3.Error, OSError) as exc:
            title = self.i18n.text('Eliminazione non riuscita') if self.i18n else 'Eliminazione non riuscita'
            QMessageBox.warning(self, title, str(exc))

    def _backup(self):
        title = self.i18n.text('Nuovo backup database') if self.i18n else 'Nuovo backup database'
        path, _ = QFileDialog.getSaveFileName(self, title, '', 'SQLite (*.sqlite3)')
        if not path:
            return
        try:
            self.store.backup(path)
            title = self.i18n.text('Backup completato') if self.i18n else 'Backup completato'
            QMessageBox.information(self, title, path)
        except (ProfileError, sqlite3.Error, OSError) as exc:
            title = self.i18n.text('Backup non riuscito') if self.i18n else 'Backup non riuscito'
            QMessageBox.warning(self, title, str(exc))

    def _info(self):
        show_info(self, 'Gestione profili',
            'Crea, modifica, duplica o elimina le tipologie di componente. '
            'Per disattivare una tipologia senza cancellarla, apri Modifica e togli Attiva. '
            'Ogni operazione confermata viene salvata subito in SQLite; Chiudi non annulla '
            'le operazioni già salvate. I default generali non si possono eliminare. '
            'Per conservare una copia usa Backup DB.\n\nDatabase: ' + str(self.store.path),
            text_key='help.profile.management', database=str(self.store.path))
