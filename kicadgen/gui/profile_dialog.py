"""@brief SQLite profile CRUD. Editors stage changes locally until explicit Save."""
from pathlib import Path
import sqlite3

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ..services.analysis_profiles import FAMILIES, ProfileError

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
    'Localizza gli ingombri nelle foto calibrate. Lo stesso modello è usato per i dettagli '
    'dei terminali. Le istruzioni foto sono aggiunte ai prompt fotografici, inclusa la verifica, '
    'ma non al prompt Lettura ritagli: descrivono la scheda intera, non il singolo corpo. '
    'I prompt tecnici Terminali e Reofori sono avanzati: modificarli non riattiva i pulsanti '
    'manuali nascosti. Il prompt Lettura ritagli guida la rilettura di ogni corpo dal suo '
    'ritaglio ingrandito (rilevatori «YOLO locale + verifica AI» e «Solo modello di visione»); '
    'il Modello lettura ritagli, se impostato, sostituisce per quelle sole letture il modello '
    'della chiamata sulla fotografia, altrimenti vale quest\'ultimo. '
    'XY sono stime calibrate e le altezze richiedono conferma. '
    'La cache nella cartella photo-cache del progetto distingue input, modello e prompt.',
    'Verifica la presenza dei corpi nei rettangoli proposti, senza spostare coordinate '
    'o aggiungere ID. Il modello è indipendente da Datasheet e Localizzazione foto.',
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
    """@brief Create a non-default push button with a 20 px asset icon wired to `callback`."""
    button = QPushButton(text)
    button.setIcon(QIcon(str(ASSETS / asset)))
    button.setIconSize(QSize(20, 20))
    button.setAutoDefault(False)
    button.clicked.connect(callback)
    return button


def show_info(parent, title, text, *, text_key=None, **values):
    """@brief Show a modal read-only information dialog, translated when an i18n catalogue is reachable."""
    i18n = _find_i18n(parent)
    dialog = QDialog(parent)
    dialog.setWindowTitle(
        (i18n.text('Informazioni') + ' — ' + i18n.text(title))
        if i18n is not None else 'Informazioni — ' + title
    )
    dialog.setModal(True)
    dialog.resize(660, 380)
    layout = QVBoxLayout(dialog)
    editor = QTextEdit()
    editor.setReadOnly(True)
    editor.setPlainText(
        i18n.tr(text_key, default=text, **values)
        if i18n is not None and text_key else text
    )
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
        super().__init__(parent)
        self.i18n = _find_i18n(parent)
        self.store, self.key, self.new = store, key, new
        self.is_type = bool(key) or new
        self.setWindowTitle('Tipologia e profilo AI' if self.is_type else 'Default generali AI')
        self.resize(900, 720)
        self.setModal(True)
        self.defaults = store.defaults()
        overrides = store.overrides(key) if key else {}
        row = store.get_type(key) if key else None
        self.fields, self.inherit = {}, {}
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
            # Nessun modello per tipologia (utente, 2026-10-02): il modello e'
            # unico, si sceglie nel tab «Modello» delle impostazioni LLM.
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
            elif index == 0 and self.is_type:
                sub = QTabWidget()
                column.addWidget(sub, 1)
                base = QWidget()
                inner = QVBoxLayout(base)
                sub.addTab(base, 'Prompt base')
                self._add_field(inner, prompt, overrides)
                self.instructions = QTextEdit()
                self.instructions.setAcceptRichText(False)
                self.instructions.setPlainText(row['instructions'] if row else '')
                sub.addTab(self.instructions, 'Istruzioni tipologia')
            else:
                column.addWidget(QLabel('Prompt base:'))
                self._add_field(column, prompt, overrides)
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
        if self.is_type:
            inherit.toggled.connect(lambda checked, field=editor, name=key:
                                    self._inherit_changed(name, checked, field))

    def _inherit_changed(self, name, checked, editor):
        editor.setReadOnly(checked)
        if checked:
            editor.setPlainText(self.defaults[name])

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

    def _save(self):
        values = {}
        for key, editor in self.fields.items():
            values[key] = (editor.text() if isinstance(editor, QLineEdit) else editor.toPlainText()).strip()
            if self.is_type and self.inherit[key].isChecked():
                values[key] = None
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
