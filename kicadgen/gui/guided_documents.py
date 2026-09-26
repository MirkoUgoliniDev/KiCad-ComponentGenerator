"""@brief Guided document slots with explicit roles and local-copy previews."""
from pathlib import Path
import webbrowser

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QImageReader
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..services.document_roles import DocumentSet, ROLES, ROLE_HELP, SUFFIXES
from .profile_dialog import icon_button, show_info
from ..services.messages import tx


#: PDF pages are rendered at this multiple of their size in points when the
#: caller wants detail for zooming (about 216 dpi).
PDF_ZOOM_RENDER_SCALE = 3


#: Largest side decoded for a card thumbnail before the final scaling: well
#: above any card, far below Qt's allocation limit.
_CARD_DECODE_PX = 2048


def _on_white(image):
    """A rendered PDF page on white paper: QPdfDocument leaves the background
    transparent, and on the dark viewer black text became unreadable."""
    from PyQt6.QtGui import QImage, QPainter
    paper = QImage(image.size(), QImage.Format.Format_RGB32)
    paper.fill(Qt.GlobalColor.white)
    painter = QPainter(paper)
    painter.drawImage(0, 0, image)
    painter.end()
    return paper


def _decode_size(reader, size, upscale):
    """Size to decode at, in the file's own (untransformed) orientation.

    Never larger than the file. For the zoomable preview (`upscale` False)
    the transformed image must fit `size`; for a card, a moderate size that
    the final smooth scaling reduces further.
    """
    from PyQt6.QtGui import QImageIOHandler
    raw = reader.size()
    if not raw.isValid() or raw.isEmpty():
        return QSize()
    width, height = raw.width(), raw.height()
    turned = bool(reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90)
    shown_w, shown_h = (height, width) if turned else (width, height)
    limit_w, limit_h = ((_CARD_DECODE_PX, _CARD_DECODE_PX) if upscale
                        else (size.width(), size.height()))
    factor = min(1.0, limit_w / shown_w, limit_h / shown_h)
    if factor >= 1.0:
        return QSize()
    return QSize(max(1, round(width * factor)), max(1, round(height * factor)))


def document_pixmap(path, size, upscale=True):
    """@brief Pixmap of a document: an image with its EXIF orientation, or a PDF's first page.

    @param path Document file.
    @param size Box the result must fit into.
    @param upscale True (cards): the result fills `size`, small images are
        enlarged. False (zoomable preview): an image keeps its own pixels
        unless larger than `size`, and a PDF page is rendered at
        PDF_ZOOM_RENDER_SCALE times its point size, so zooming shows detail
        instead of an enlarged thumbnail.
    @return The pixmap, or a null pixmap when the document cannot be drawn.
    """
    if path.suffix.lower() == '.pdf':
        try:
            from PyQt6.QtPdf import QPdfDocument
            doc = QPdfDocument(None)
            try:
                doc.load(str(path))
                if doc.pageCount():
                    page = doc.pagePointSize(0).toSize()
                    if upscale:
                        page.scale(size, Qt.AspectRatioMode.KeepAspectRatio)
                    else:
                        page = page * PDF_ZOOM_RENDER_SCALE
                        if page.width() > size.width() or page.height() > size.height():
                            page.scale(size, Qt.AspectRatioMode.KeepAspectRatio)
                    return QPixmap.fromImage(_on_white(doc.render(0, page)))
            finally:
                doc.close()
        except ImportError:
            pass
        return QPixmap()
    # Le foto dei telefoni salvano i pixel in un verso e il verso giusto nel
    # tag EXIF di orientamento. L'analisi (OpenCV, imread e imdecode) lo
    # applica; QPixmap no, e la foto orizzontale compariva verticale nella
    # card (utente, 2026-09-24). L'anteprima deve mostrare cio' che l'analisi
    # vede: il lettore applica il tag.
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    # Riduzione GIA' in lettura: decodificare a piena risoluzione e poi
    # ridurre falliva oltre il limite di memoria di Qt (256 MB, ~8192x8192):
    # una foto da 108 MP dava un'anteprima vuota (audit 25/09).
    reader.setScaledSize(_decode_size(reader, size, upscale))
    image = reader.read()
    if image.isNull():
        return QPixmap()
    if not upscale and image.width() <= size.width() and image.height() <= size.height():
        return QPixmap.fromImage(image)
    return QPixmap.fromImage(image).scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)


class GuidedDocuments(QWidget):
    """@brief Manage the guided pinout and top-view documents."""
    changed = pyqtSignal()

    def __init__(self, directory, parent=None, *, i18n=None, config=None, body_size=None):
        super().__init__(parent)
        self.directory = directory
        self.i18n = i18n
        # Dizionario vivo delle preferenze della finestra, per ricordare la
        # cartella di provenienza dei documenti. None nei test e negli usi
        # isolati del widget: il dialogo parte allora dalla cartella docs.
        self.config = config
        # Quote del corpo (lunghezza, larghezza) per verificare se una
        # vista dall'alto e' calibrabile. None quando non sono note.
        self.body_size = body_size
        # La verifica costa ~150 ms su una foto da 4 Mpx: si ricalcola
        # solo quando cambiano file o quote, non a ogni refresh.
        self._calibration_cache = {}
        self.cards = {}
        # Ruolo attualmente in caricamento, per non sbilanciare lo
        # stack dei cursori di override di Qt.
        self._busy_role = None
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
            # Vuoto finche' non c'e' un file: «Da caricare» sopra lo dice gia',
            # e la lineetta sembrava un dato (PIANO-FIX, identita'). Una riga
            # riservata tiene ferma l'altezza della scheda quando il file arriva.
            filename = QLabel('')
            filename.setMinimumHeight(filename.fontMetrics().lineSpacing())
            # File names are user data, never localization source strings.
            filename.setProperty('i18n_skip', True)
            filename.setTextFormat(Qt.TextFormat.PlainText)
            filename.setWordWrap(True)
            column.addWidget(filename)
            verdict = QLabel()
            verdict.setTextFormat(Qt.TextFormat.PlainText)
            verdict.setWordWrap(True)
            verdict.setProperty('i18n_skip', True)
            verdict.hide()
            column.addWidget(verdict)
            actions = QHBoxLayout()
            load = icon_button('Carica…', 'docs-add.svg', lambda checked=False, r=role: self._load(r))
            # Occhio, non cartella (utente, 2026-09-24): il pulsante apre
            # un'anteprima nel programma, non sfoglia file sul disco.
            view = icon_button('', 'tab-preview.svg', lambda checked=False, r=role: self._preview(r))
            remove = icon_button('', 'docs-remove.svg', lambda checked=False, r=role: self._remove(r))
            view.setToolTip('Anteprima documento\nApre il documento in una finestra con lo '
                            'zoom; il file non viene modificato.')
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
                                    remove=remove, unavailable=unavailable, verdict=verdict)
            row.addWidget(card, 1)
        self.refresh()

    def store(self):
        """@brief Return a DocumentSet for the folder currently provided by the `directory` callable."""
        return DocumentSet(self.directory())

    def _tr(self, source):
        """@brief Translate a visible source string when i18n is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def set_busy(self, role, busy):
        """@brief Show or clear the waiting state of a role card while its file is copied and previewed.

        Mette la card in attesa mentre il file viene copiato e letto.

        La copia e' immediata, ma generare l'anteprima costa decine di
        millisecondi per una foto e puo' arrivare a secondi su un PDF: senza
        un segnale la card resta muta e sembra che il pulsante non abbia
        fatto nulla. I comandi vengono disabilitati per non accodare una
        seconda richiesta sullo stesso ruolo.
        """
        widgets = self.cards.get(role)
        if not widgets or busy == (self._busy_role == role):
            # Ripristinare un cursore mai impostato -- il caso dell'utente che
            # annulla la conferma di sostituzione -- toglierebbe dallo stack
            # quello di qualcun altro.
            return
        for key in ('load', 'view', 'remove', 'unavailable'):
            widgets[key].setEnabled(not busy)
        if busy:
            # L'anteprima precedente sparisce subito: e' gia' superata.
            widgets['preview'].setPixmap(QPixmap())
            widgets['preview'].setText(self._tr('Caricamento…'))
            widgets['filename'].setText('…')
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        else:
            QApplication.restoreOverrideCursor()
        self._busy_role = role if busy else None
        # Il lavoro blocca il thread della GUI: senza questo la card non
        # verrebbe ridisegnata prima della fine, e l'attesa non si vedrebbe.
        QApplication.processEvents()

    def _calibration_verdict(self, role, path):
        """Esito della calibrazione per una vista dall'alto: (icona, testo, colore).

        Risponde alla domanda che conta guardando la card: questa immagine
        verra' davvero usata per individuare i componenti? Senza, una foto
        rifiutata resta indistinguibile da una accettata e si scopre il
        problema solo dal risultato.

        None per i ruoli che non vengono calibrati: il pinout non e' una vista
        dall'alto e un esito li' sarebbe fuorviante.
        """
        from ..services.document_roles import INVENTORY_ROLES, ROLE_HELP
        if path is None:
            return None
        if role not in INVENTORY_ROLES:
            # Il pinout non viene calibrato, ma "nessun segno"
            # non distingue "non serve" da "non l'ho guardata": si dichiara
            # che il file e' a posto e a cosa serve.
            return ('✓', '', self._tr('documento caricato; non richiede calibrazione.')
                    + ' ' + self._tr(ROLE_HELP.get(role, '')), '#a6adc8')
        size = self.body_size() if callable(self.body_size) else None
        if not size or not all(isinstance(v, (int, float)) and v > 0 for v in size):
            return ('·', '', self._tr('quote del corpo non ancora note'), '#a6adc8')
        length, width = float(size[0]), float(size[1])
        try:
            key = (str(path), path.stat().st_mtime_ns, path.stat().st_size, length, width)
        except OSError:
            return None
        if key in self._calibration_cache:
            return self._calibration_cache[key]
        verdict = self._measure_calibration(path, length, width)
        self._calibration_cache[key] = verdict
        return verdict

    def _measure_calibration(self, path, length, width):
        try:
            from ..services.photo_inventory import prepare_photo
            from ..services.photo_refine import MIN_PACKAGE_PIXELS_HINT
            crop = prepare_photo(path, length, width, None)
        except ImportError:
            return None
        except (OSError, ValueError):
            crop = None
        if crop is None:
            return ('✗', '',
                    self._tr('non calibrabile: la scheda non è riconoscibile qui'), '#f38ba8')
        scale = crop.shape[1] / length
        readable = scale * 0.5  # lato corto di un 0402
        summary = f'{scale:.0f} px/mm'
        if readable < MIN_PACKAGE_PIXELS_HINT:
            return ('⚠', summary, self._tr('calibrata, ma grossolana:') + f' {summary} '
                    + self._tr('(i passivi piccoli sfuggono)'), '#f9e2af')
        return ('✓', summary, self._tr('utilizzabile:') + f' {summary}', '#a6e3a1')

    def refresh(self):
        """@brief Rilegge le assegnazioni e aggiorna le card.

        @note La cartella puo' non esistere ancora: finche' il componente non
            ha un nome non c'e' un posto dove i documenti andrebbero, e
            chiederlo solleva. Qui non e' un errore da segnalare come guasto:
            e' lo stato normale di un lavoro appena cominciato, e le card
            restano vuote finche' il nome non c'e'.
        """
        directory = None
        try:
            directory = Path(self.directory())
        except ValueError as exc:
            rows = {}
            self.status.setText(str(exc))
        else:
            try:
                rows = self.store().read()['roles']
                self.status.setText('Assegna un file a ogni ruolo, oppure indica “Non disponibile”.')
            except (ValueError, OSError) as exc:
                rows = {}
                self.status.setText(str(exc))
        for role, widgets in self.cards.items():
            entry = rows.get(role, {})
            filename = entry.get('filename')
            path = directory / filename if (directory is not None and filename) else None
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
            verdict = self._calibration_verdict(role, path if exists else None)
            if verdict is None:
                widgets['verdict'].hide()
            else:
                icon, summary, explanation, colour = verdict
                # La card e' stretta: la spiegazione per esteso mandava il
                # testo a capo su due righe. Resta l'icona con il dato che
                # conta, il resto si legge passandoci sopra.
                widgets['verdict'].setText(f'{icon}  {summary}'.rstrip())
                widgets['verdict'].setToolTip(explanation)
                widgets['verdict'].setStyleSheet(f'color: {colour};')
                widgets['verdict'].show()
            widgets['filename'].setText(filename or (self._tr('Non disponibile')
                                                     if entry.get('unavailable') else ''))
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
        if directory is not None and not self.store().path.exists() and directory.is_dir():
            if any(p.is_file() and p.suffix.lower() in SUFFIXES for p in directory.iterdir()):
                self.status.setText('Documenti precedenti conservati: usa Carica per assegnarli ai ruoli disponibili.')
        self.status.setProperty('i18n_source', self.status.text())
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)

    def _load(self, role):
        from .app_config import browse_start_dir, remember_browse_dir
        source, _ = QFileDialog.getOpenFileName(
            self, self._tr(ROLES[role]),
            browse_start_dir(self.config, self.directory()),
            self._tr('Immagini e PDF (*.png *.jpg *.jpeg *.webp *.gif *.pdf)'))
        if not source:
            return
        remember_browse_dir(self.config, source)
        try:
            store = self.store()
            replace = bool(store.read()['roles'].get(role, {}).get('filename')) or (store.directory / (role + Path(source).suffix.lower())).exists()
            role_label = self._tr(ROLES[role])
            if replace and QMessageBox.question(self, self._tr('Sostituisci documento'),
                    self.i18n.tr('document.replace_question', role=role_label)
                    if self.i18n is not None else
                    tx("Sostituire il file per “{ROLES}”? La copia precedente resterà in "
                       "docs/.history.",
                       ROLES=ROLES[role]),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.set_busy(role, True)
            store.import_file(role, source, replace=replace)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non caricato'), str(exc))
        finally:
            self.set_busy(role, False)
            self.refresh()

    def _remove(self, role, unavailable=False):
        try:
            store = self.store()
            if store.read()['roles'].get(role, {}).get('filename'):
                role_label = self._tr(ROLES[role])
                if QMessageBox.question(self, self._tr('Rimuovi documento'),
                        self.i18n.tr('document.remove_question', role=role_label)
                        if self.i18n is not None else
                        tx("Rimuovere la copia locale di “{ROLES}”? L’originale non verrà "
                           "modificato. La copia resterà recuperabile in docs/.history.",
                           ROLES=ROLES[role]),
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
                raise ValueError(tx("Il file non è più presente. Caricalo nuovamente."))
            dialog = QDialog(self)
            role_label = self.i18n.text(ROLES[role]) if self.i18n is not None else ROLES[role]
            dialog.setWindowTitle(f'{role_label} — {name}')
            dialog.resize(900, 700)
            layout = QVBoxLayout(dialog)
            # Pixel veri, non la miniatura ingrandita: si puo' zoomare
            # (utente, 2026-09-24). Un'immagine piu' grande di 8192 px viene
            # ridotta gia' in lettura (`_decode_size`), non rifiutata.
            pixmap = document_pixmap(path, QSize(8192, 8192), upscale=False)
            if pixmap.isNull():
                label = QLabel('Anteprima non disponibile. Usa “Apri file completo” per consultare il documento.')
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setWordWrap(True)
                layout.addWidget(label, 1)
            else:
                from .zoom_image import ZoomImagePanel
                dialog.zoom_panel = ZoomImagePanel(pixmap, tr=self._tr)
                layout.addWidget(dialog.zoom_panel, 1)
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
            'L’app copia i file in docs con nomi pinout, top e hires, mantenendo l’estensione reale. '
            'Gli originali restano invariati. Sostituzioni e rimozioni sono recuperabili in docs/.history. '
            'Per il rilevamento fotografico dei componenti, top deve essere un’immagine PNG/JPEG/WEBP. '
            'Un PDF viene letto nell’analisi generale (l’anteprima mostra la prima pagina). '
            'L’AI riceve i ruoli esplicitamente. Le viste non disponibili non vengono sostituite con altri file.',
            text_key='help.guided_documents')
