"""Three document slots, with explicit roles and local-copy previews."""
from pathlib import Path
import webbrowser

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..services.document_roles import DocumentSet, ROLES, ROLE_HELP, SUFFIXES
from .profile_dialog import icon_button, show_info


def document_pixmap(path, size):
    """@brief Provide the document pixmap operation for this workflow."""
    if path.suffix.lower() == '.pdf':
        try:
            from PyQt6.QtPdf import QPdfDocument
            doc = QPdfDocument(None)
            try:
                doc.load(str(path))
                if doc.pageCount():
                    page = doc.pagePointSize(0).toSize()
                    page.scale(size, Qt.AspectRatioMode.KeepAspectRatio)
                    return QPixmap.fromImage(doc.render(0, page))
            finally:
                doc.close()
        except ImportError:
            pass
        return QPixmap()
    return QPixmap(str(path)).scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)


class GuidedDocuments(QWidget):
    """@brief Manage the guided pinout, top-view and front-view documents."""
    changed = pyqtSignal()

    def __init__(self, directory, parent=None, *, i18n=None):
        super().__init__(parent)
        self.directory = directory
        self.i18n = i18n
        self.cards = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        top.addWidget(self.status, 1)
        top.addWidget(icon_button('', 'info.svg', self._info))
        info_label = (
            self.i18n.text('Informazioni caricamento guidato')
            if self.i18n is not None else 'Informazioni caricamento guidato'
        )
        top.itemAt(1).widget().setAccessibleName(info_label)
        top.itemAt(1).widget().setToolTip(info_label)
        layout.addLayout(top)
        row = QHBoxLayout()
        layout.addLayout(row)
        for role, label in ROLES.items():
            card = QGroupBox(label)
            column = QVBoxLayout(card)
            preview = QLabel('Da caricare')
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setFixedHeight(76)
            preview.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(preview)
            filename = QLabel('—')
            # File names are user data, never localization source strings.
            filename.setProperty('i18n_skip', True)
            filename.setTextFormat(Qt.TextFormat.PlainText)
            filename.setWordWrap(True)
            column.addWidget(filename)
            actions = QHBoxLayout()
            load = icon_button('Carica…', 'docs-add.svg', lambda checked=False, r=role: self._load(r))
            view = icon_button('', 'docs-open.svg', lambda checked=False, r=role: self._preview(r))
            remove = icon_button('', 'docs-remove.svg', lambda checked=False, r=role: self._remove(r))
            view.setToolTip('Anteprima documento')
            localized_label = self.i18n.text(label) if self.i18n is not None else label
            view.setAccessibleName(
                self.i18n.tr('tooltip.preview_role', role=localized_label)
                if self.i18n is not None else 'Anteprima: ' + label
            )
            remove.setToolTip('Rimuovi copia locale (recuperabile)')
            remove.setAccessibleName(
                self.i18n.tr('tooltip.remove_role', role=localized_label)
                if self.i18n is not None else 'Rimuovi: ' + label
            )
            actions.addWidget(load, 1)
            actions.addWidget(view)
            actions.addWidget(remove)
            column.addLayout(actions)
            unavailable = QCheckBox('Non disponibile')
            unavailable.toggled.connect(lambda checked, r=role: self._unavailable(r, checked))
            column.addWidget(unavailable)
            self.cards[role] = dict(preview=preview, filename=filename, load=load, view=view,
                                    remove=remove, unavailable=unavailable)
            row.addWidget(card, 1)
        self.refresh()

    def store(self):
        """@brief Provide the store operation for this workflow."""
        return DocumentSet(self.directory())

    def _tr(self, source):
        """@brief Translate a visible source string when i18n is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def refresh(self):
        """@brief Provide the refresh operation for this workflow."""
        try:
            rows = self.store().read()['roles']
            self.status.setText('Assegna un file a ogni ruolo, oppure indica “Non disponibile”.')
        except (ValueError, OSError) as exc:
            rows = {}
            self.status.setText(str(exc))
        for role, widgets in self.cards.items():
            entry = rows.get(role, {})
            filename = entry.get('filename')
            path = Path(self.directory()) / filename if filename else None
            exists = bool(path and path.is_file())
            # Reset the stable source on every refresh. A card may have been
            # refreshed after a language switch, so never trust its rendered text.
            unavailable_text = self._tr('Non disponibile')
            widgets['unavailable'].setText(unavailable_text)
            widgets['unavailable'].setProperty('i18n_source', 'Non disponibile')
            widgets['unavailable'].setProperty('i18n_rendered', unavailable_text)
            widgets['unavailable'].blockSignals(True)
            widgets['unavailable'].setChecked(entry.get('unavailable', False))
            widgets['unavailable'].blockSignals(False)
            widgets['filename'].setText(filename or ('Non disponibile' if entry.get('unavailable') else '—'))
            original_name = str(entry.get('original_name', ''))
            widgets['filename'].setToolTip(
                self.i18n.tr('tooltip.original_name', name=original_name)
                if self.i18n is not None else 'Originale: ' + original_name
            )
            widgets['load'].setText('Sostituisci…' if filename else 'Carica…')
            widgets['view'].setEnabled(exists)
            widgets['remove'].setEnabled(bool(filename))
            widgets['preview'].clear()
            if exists:
                try:
                    pixmap = document_pixmap(path, QSize(240, 76))
                    if pixmap.isNull():
                        widgets['preview'].setText('PDF' if path.suffix == '.pdf' else 'Anteprima non disponibile')
                    else:
                        widgets['preview'].setPixmap(pixmap)
                except (ValueError, OSError):
                    widgets['preview'].setText('Anteprima non disponibile')
            else:
                widgets['preview'].setText('File mancante' if filename else 'Nessun file')
            # These labels are state-dependent; refresh their localization source.
            widgets['load'].setProperty('i18n_source', widgets['load'].text())
            widgets['preview'].setProperty('i18n_source', widgets['preview'].text())
            role_label = self._tr(ROLES[role])
            widgets['view'].setAccessibleName(
                self.i18n.tr('tooltip.preview_role', role=role_label)
                if self.i18n is not None else 'Anteprima: ' + ROLES[role]
            )
            widgets['remove'].setAccessibleName(
                self.i18n.tr('tooltip.remove_role', role=role_label)
                if self.i18n is not None else 'Rimuovi: ' + ROLES[role]
            )
        if not self.store().path.exists() and Path(self.directory()).is_dir():
            if any(p.is_file() and p.suffix.lower() in SUFFIXES for p in Path(self.directory()).iterdir()):
                self.status.setText('Documenti precedenti conservati: usa Carica per assegnarli ai tre ruoli.')
        self.status.setProperty('i18n_source', self.status.text())
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)

    def _load(self, role):
        source, _ = QFileDialog.getOpenFileName(
            self, self._tr(ROLES[role]), str(self.directory()),
            self._tr('Immagini e PDF (*.png *.jpg *.jpeg *.webp *.gif *.pdf)'))
        if not source:
            return
        try:
            store = self.store()
            replace = bool(store.read()['roles'].get(role, {}).get('filename')) or (store.directory / (role + Path(source).suffix.lower())).exists()
            role_label = self._tr(ROLES[role])
            if replace and QMessageBox.question(self, self._tr('Sostituisci documento'),
                    self.i18n.tr('document.replace_question', role=role_label)
                    if self.i18n is not None else
                    f'Sostituire il file per “{ROLES[role]}”? La copia precedente resterà in docs/.history.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            store.import_file(role, source, replace=replace)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non caricato'), str(exc))
        finally:
            self.refresh()

    def _remove(self, role, unavailable=False):
        try:
            store = self.store()
            if store.read()['roles'].get(role, {}).get('filename'):
                role_label = self._tr(ROLES[role])
                if QMessageBox.question(self, self._tr('Rimuovi documento'),
                        self.i18n.tr('document.remove_question', role=role_label)
                        if self.i18n is not None else
                        f'Rimuovere la copia locale di “{ROLES[role]}”? L’originale non verrà modificato. '
                        'La copia resterà recuperabile in docs/.history.',
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
            store.clear(role, unavailable=unavailable)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non aggiornato'), str(exc))
        finally:
            self.refresh()

    def _unavailable(self, role, checked):
        self._remove(role, unavailable=checked)

    def _preview(self, role):
        try:
            name = self.store().read()['roles'].get(role, {}).get('filename')
            if not name:
                return
            path = Path(self.directory()) / name
            if not path.is_file():
                raise ValueError('Il file non è più presente. Caricalo nuovamente.')
            dialog = QDialog(self)
            role_label = self.i18n.text(ROLES[role]) if self.i18n is not None else ROLES[role]
            dialog.setWindowTitle(f'{role_label} — {name}')
            dialog.resize(900, 700)
            layout = QVBoxLayout(dialog)
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = document_pixmap(path, QSize(1600, 1200))
            if pixmap.isNull():
                label.setText('Anteprima non disponibile. Usa “Apri file completo” per consultare il documento.')
                label.setWordWrap(True)
            else:
                label.setPixmap(pixmap)
            scroll = QScrollArea()
            scroll.setWidget(label)
            scroll.setWidgetResizable(True)
            layout.addWidget(scroll, 1)
            open_file = QPushButton('Apri file completo')
            open_file.clicked.connect(lambda: webbrowser.open(path.resolve().as_uri()))
            layout.addWidget(open_file)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if self.i18n is not None:
                self.i18n.retranslate_widgets(dialog)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Anteprima non disponibile'), str(exc))

    def _info(self):
        show_info(self, 'Documenti per l’AI', '\n\n'.join(ROLE_HELP.values()) + '\n\n'
            'L’app copia i file in docs con nomi pinout, top e front, mantenendo l’estensione reale. '
            'Gli originali restano invariati. Sostituzioni e rimozioni sono recuperabili in docs/.history. '
            'Per il rilevamento fotografico dei componenti, top deve essere un’immagine PNG/JPEG/WEBP. '
            'Un PDF viene letto nell’analisi generale (l’anteprima mostra la prima pagina). '
            'L’AI riceve i ruoli esplicitamente. Le viste non disponibili non vengono sostituite con altri file.',
            text_key='help.guided_documents')
