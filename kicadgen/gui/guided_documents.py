"""@brief Guided documents: one card per file with the roles it carries, and local-copy previews."""
from pathlib import Path
import webbrowser

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QImageReader
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget)

from ..services.document_roles import (DocumentSet, PHOTO_ROLE, ROLES, ROLE_HELP, ROLE_SHORT,
                                       allowed_roles,
                                       SUFFIXES, role_list)
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


#: Width of a document card, in pixels: a thumbnail plus two columns of role
#: check boxes with their short labels, in the 10 pt interface font.
CARD_WIDTH = 236
#: Position (row, column, column span) of each role check box on a card.
#: «Foto alta risoluzione» and its translations (up to «Photo haute
#: résolution») do not fit half of CARD_WIDTH, so the photograph role has a
#: row of its own; the other pairs are the ones that already fitted in all
#: four languages.
ROLE_GRID = {'pinout': (0, 0, 1), 'top': (0, 1, 1), 'hires': (1, 0, 2),
             'land_pattern': (2, 0, 1), 'side_view': (2, 1, 1), 'datasheet': (3, 0, 1)}
#: Height of the thumbnail on a card, in pixels.
PREVIEW_HEIGHT = 76
#: Key of the «new document» card in `set_busy`.
ADD_KEY = '__add__'
#: Colours of the card verdicts (Catppuccin Mocha, as `theme`).
_OK, _WARN, _BAD, _NEUTRAL = '#a6e3a1', '#f9e2af', '#f38ba8', '#a6adc8'


class GuidedDocuments(QWidget):
    """@brief The project's documents, one card per file, each with the roles it carries.

    @details Since 2026-09-26 a file can carry several roles (see
    `document_roles`): a datasheet PDF is often pinout, dimensioned drawing
    and land pattern at once. Every card shows the file, its role check boxes
    and, for a photograph, whether the photo can be calibrated. A strip above
    the cards says which roles are covered. A new document arrives with NO
    role: the user ticks what it contains, and until then the analysis does
    not start (`DocumentSet.selected`), so no role is ever guessed.

    Public attributes used by the window and the tests: ``cards`` (file name
    -> dict of widgets: ``group``, ``preview``, ``verdict``, ``roles`` (role ->
    QCheckBox), ``replace``, ``view``, ``remove``), ``add_button``,
    ``role_badges`` (role -> QLabel) and ``status``.
    """
    changed = pyqtSignal()

    def __init__(self, directory, parent=None, *, i18n=None, config=None, body_size=None):
        """@brief Build the panel and show the current documents.

        @param directory Callable returning the docs folder (may raise ValueError
            while the component has no name).
        @param parent Parent widget.
        @param i18n Optional translator of the window.
        @param config Live preferences dictionary (last browse folder), or None.
        @param body_size Callable returning the board (length, width) in mm, or None.
        """
        super().__init__(parent)
        self.directory = directory
        self.i18n = i18n
        # Dizionario vivo delle preferenze della finestra, per ricordare la
        # cartella di provenienza dei documenti. None nei test e negli usi
        # isolati del widget: il dialogo parte allora dalla cartella docs.
        self.config = config
        # Quote del corpo (lunghezza, larghezza) per verificare se una
        # foto e' calibrabile. None quando non sono note.
        self.body_size = body_size
        # La verifica costa ~150 ms su una foto da 4 Mpx: si ricalcola
        # solo quando cambiano file o quote, non a ogni refresh.
        self._calibration_cache = {}
        self.cards = {}
        # Card attualmente in caricamento, per non sbilanciare lo stack dei
        # cursori di override di Qt.
        self._busy_role = None
        # Avviso di un'operazione appena fatta (file gia' presente), mostrato
        # nella riga di stato fino all'azione successiva.
        self._notice = ''
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setProperty('i18n_skip', True)
        top.addWidget(self.status, 1)
        info = icon_button('', 'info.svg', self._info)
        info_label = self._tr('Informazioni caricamento guidato')
        info.setAccessibleName(info_label)
        info.setToolTip(info_label)
        top.addWidget(info)
        layout.addLayout(top)
        # Striscia dei ruoli: quali sono coperti e da quale file.
        badges = QHBoxLayout()
        badges.setSpacing(16)
        self.role_badges = {}
        for role in ROLES:
            badge = QLabel()
            badge.setTextFormat(Qt.TextFormat.PlainText)
            badge.setProperty('i18n_skip', True)
            badges.addWidget(badge)
            self.role_badges[role] = badge
        badges.addStretch(1)
        layout.addLayout(badges)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self._row = QHBoxLayout(host)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.addWidget(self._make_add_card())
        self._row.addStretch(1)
        self.scroll.setWidget(host)
        layout.addWidget(self.scroll)
        self.refresh()

    # ── Costruzione ─────────────────────────────────────────────────

    def _make_add_card(self):
        card = QGroupBox('Nuovo documento')
        card.setFixedWidth(CARD_WIDTH)
        column = QVBoxLayout(card)
        hint = QLabel('Un file può avere più ruoli: dopo averlo aggiunto spunta tutti '
                      'quelli che contiene.')
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        hint.setStyleSheet(f'color: {_NEUTRAL};')
        column.addWidget(hint, 1)
        self.add_hint = hint
        self.add_button = icon_button('Aggiungi…', 'docs-add.svg', lambda checked=False: self._add())
        self.add_button.setToolTip(self._tr('Copia un’immagine o un PDF nella cartella docs del '
                                            'progetto; l’originale non viene modificato.'))
        column.addWidget(self.add_button)
        return card

    def _make_card(self, name):
        card = QGroupBox()
        # Il titolo e' il nome del file: un dato, non un testo da tradurre.
        card.setProperty('i18n_skip', True)
        card.setFixedWidth(CARD_WIDTH)
        column = QVBoxLayout(card)
        column.setSpacing(4)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setFixedHeight(PREVIEW_HEIGHT)
        preview.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(preview)
        verdict = QLabel()
        verdict.setTextFormat(Qt.TextFormat.PlainText)
        verdict.setProperty('i18n_skip', True)
        column.addWidget(verdict)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        boxes = {}
        for role in ROLES:
            box = QCheckBox(ROLE_SHORT[role])
            box.toggled.connect(lambda checked, n=name, r=role: self._toggle_role(n, r, checked))
            row, col, span = ROLE_GRID[role]
            grid.addWidget(box, row, col, 1, span)
            boxes[role] = box
        column.addLayout(grid)
        actions = QHBoxLayout()
        # Solo icone, come anteprima e rimozione: la card e' stretta e
        # «Sostituisci…» per esteso veniva troncato.
        replace = icon_button('', 'docs-open.svg', lambda checked=False, n=name: self._replace(n))
        replace.setToolTip(self._tr('Sostituisci il file…\nMantiene i ruoli spuntati; la copia '
                                    'precedente resta in docs/.history.'))
        replace.setAccessibleName(self._tr('Sostituisci il file…'))
        # Occhio, non cartella (utente, 2026-09-24): il pulsante apre
        # un'anteprima nel programma, non sfoglia file sul disco.
        view = icon_button('', 'tab-preview.svg', lambda checked=False, n=name: self._preview(n))
        remove = icon_button('', 'docs-remove.svg', lambda checked=False, n=name: self._remove(n))
        view.setToolTip(self._tr('Anteprima documento\nApre il documento in una finestra con lo '
                                 'zoom; il file non viene modificato.'))
        remove.setToolTip(self._tr('Rimuovi copia locale (recuperabile)'))
        view.setAccessibleName(self.i18n.tr('tooltip.preview_role', role=name)
                               if self.i18n is not None else 'Anteprima: ' + name)
        remove.setAccessibleName(self.i18n.tr('tooltip.remove_role', role=name)
                                 if self.i18n is not None else 'Rimuovi: ' + name)
        actions.addWidget(replace)
        actions.addWidget(view)
        actions.addWidget(remove)
        column.addLayout(actions)
        return dict(group=card, preview=preview, verdict=verdict, roles=boxes,
                    replace=replace, view=view, remove=remove)

    def _sync_cards(self, names):
        """Create, drop and order the cards so that they follow `names`."""
        for name in [n for n in self.cards if n not in names]:
            widgets = self.cards.pop(name)
            self._row.removeWidget(widgets['group'])
            widgets['group'].hide()
            # deleteLater: la card puo' essere quella il cui segnale e' in corso.
            widgets['group'].deleteLater()
        for index, name in enumerate(names):
            if name not in self.cards:
                self.cards[name] = self._make_card(name)
            group = self.cards[name]['group']
            if self._row.indexOf(group) != index:
                self._row.removeWidget(group)
                self._row.insertWidget(index, group)

    # ── Stato ───────────────────────────────────────────────────────

    def store(self):
        """@brief Return a DocumentSet for the folder currently provided by the `directory` callable."""
        return DocumentSet(self.directory())

    def _tr(self, source):
        """@brief Translate a visible source string when i18n is available."""
        return self.i18n.text(source) if self.i18n is not None else source

    def set_busy(self, key, busy):
        """@brief Show or clear the waiting state of a card while its file is copied and previewed.

        @param key File name of the card, or ADD_KEY for the «new document» card.
        @param busy True while the work runs.
        @note La copia e' immediata, ma generare l'anteprima costa decine di
            millisecondi per una foto e puo' arrivare a secondi su un PDF: senza
            un segnale la card resta muta e sembra che il pulsante non abbia
            fatto nulla. I comandi vengono disabilitati per non accodare una
            seconda richiesta.
        """
        if key == ADD_KEY:
            controls, label = [self.add_button], self.add_hint
        else:
            widgets = self.cards.get(key)
            if not widgets:
                return
            controls = [widgets['replace'], widgets['view'], widgets['remove'],
                        *widgets['roles'].values()]
            label = widgets['preview']
        if busy == (self._busy_role == key):
            # Ripristinare un cursore mai impostato -- il caso dell'utente che
            # annulla la conferma di sostituzione -- toglierebbe dallo stack
            # quello di qualcun altro.
            return
        for control in controls:
            control.setEnabled(not busy)
        if busy:
            if key != ADD_KEY:
                # L'anteprima precedente sparisce subito: e' gia' superata.
                label.setPixmap(QPixmap())
            label.setText(self._tr('Caricamento…'))
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        else:
            QApplication.restoreOverrideCursor()
            if key == ADD_KEY:
                label.setText(self._tr('Un file può avere più ruoli: dopo averlo aggiunto '
                                       'spunta tutti quelli che contiene.'))
        self._busy_role = key if busy else None
        # Il lavoro blocca il thread della GUI: senza questo la card non
        # verrebbe ridisegnata prima della fine, e l'attesa non si vedrebbe.
        QApplication.processEvents()

    def _calibration_verdict(self, roles, path):
        """Esito per la card di un documento: (icona, testo, spiegazione, colore).

        Risponde alla domanda che conta guardando la card: questa immagine
        verra' davvero usata per individuare i componenti? Solo un file
        dichiarato foto (`PHOTO_ROLE`) viene calibrato; per gli altri si dice
        che il file e' a posto e a cosa serve, perche' «nessun segno» non
        distingue «non serve» da «non l'ho guardata». Un file senza ruoli
        viene segnalato: blocca l'analisi finche' non se ne spunta uno.

        @param roles Ruoli del file (lista; una stringa vale come un ruolo).
        @param path File, o None se manca.
        @return La quaterna, o None senza file.
        """
        roles = role_list(roles)
        if path is None:
            return None
        if not roles:
            return ('⚠', self._tr('nessun ruolo'),
                    self._tr('Spunta cosa contiene questo documento: finché non ha un ruolo '
                             'l’analisi non parte.'), _WARN)
        if PHOTO_ROLE not in roles:
            return ('✓', '', self._tr('documento caricato; non richiede calibrazione.') + ' '
                    + ' '.join(self._tr(ROLE_HELP[r]) for r in roles), _NEUTRAL)
        size = self.body_size() if callable(self.body_size) else None
        if not size or not all(isinstance(v, (int, float)) and v > 0 for v in size):
            return ('·', '', self._tr('quote del corpo non ancora note'), _NEUTRAL)
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
            from ..services import photo_refine
            crop = prepare_photo(path, length, width, None)
        except ImportError:
            return None
        except (OSError, ValueError):
            crop = None
        if crop is None:
            return ('✗', '',
                    self._tr('non calibrabile: la scheda non è riconoscibile qui'), _BAD)
        scale = crop.shape[1] / length
        # Pixel sul lato corto del corpo piu' piccolo che si assume montato:
        # ipotesi dichiarata una sola volta in photo_refine (0402, 0.5 mm).
        readable = scale * getattr(photo_refine, 'SMALLEST_EXPECTED_PART_MM', 0.5)
        summary = f'{scale:.0f} px/mm'
        if readable < MIN_PACKAGE_PIXELS_HINT:
            return ('⚠', summary, self._tr('calibrata, ma grossolana:') + f' {summary} '
                    + self._tr('(i passivi piccoli sfuggono)'), _WARN)
        return ('✓', summary, self._tr('utilizzabile:') + f' {summary}', _OK)

    def refresh(self):
        """@brief Rilegge i documenti e i loro ruoli e aggiorna le card.

        @note La cartella puo' non esistere ancora: finche' il componente non
            ha un nome non c'e' un posto dove i documenti andrebbero, e
            chiederlo solleva. Qui non e' un errore da segnalare come guasto:
            e' lo stato normale di un lavoro appena cominciato, e le card
            restano vuote finche' il nome non c'e'.
        """
        directory, documents, status = None, [], ''
        try:
            directory = Path(self.directory())
        except ValueError as exc:
            status = str(exc)
        else:
            try:
                documents = self.store().documents()
            except (ValueError, OSError) as exc:
                status = str(exc)
        self._sync_cards([name for name, _roles, _original in documents])
        covered = {role: [] for role in ROLES}
        roleless = []
        for name, roles, original in documents:
            path = directory / name
            for role in roles:
                covered[role].append(name)
            if not roles:
                roleless.append(name)
            self._fill_card(self.cards[name], name, roles, original,
                            path if path.is_file() else None)
        self._fill_badges(covered)
        self.add_button.setEnabled(directory is not None and self._busy_role != ADD_KEY)
        if not status:
            if roleless:
                status = tx("«{name}» non ha un ruolo: spunta cosa contiene oppure rimuovilo.",
                            name=roleless[0])
            elif self._notice:
                status = self._notice
            elif not documents and directory is not None and directory.is_dir() and any(
                    p.is_file() and p.suffix.lower() in SUFFIXES for p in directory.iterdir()):
                status = self._tr('Documenti precedenti conservati: usa Aggiungi per assegnare '
                                  'loro un ruolo.')
            else:
                status = self._tr('Aggiungi i documenti e spunta per ciascuno cosa contiene: '
                                  'un file può avere più ruoli.')
        self.status.setText(status)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)
        # L'area scorre solo in orizzontale: la sua altezza segue le card.
        bar = self.scroll.horizontalScrollBar().sizeHint().height()
        self.scroll.setFixedHeight(self.scroll.widget().sizeHint().height() + bar + 2)

    def _fill_card(self, widgets, name, roles, original, path):
        """Show one document: title, thumbnail, verdict and role boxes."""
        group = widgets['group']
        metrics = group.fontMetrics()
        group.setTitle(metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, CARD_WIDTH - 40))
        group.setToolTip(self.i18n.tr('tooltip.original_name', name=original or name)
                         if self.i18n is not None else 'Originale: ' + (original or name))
        is_pdf = Path(name).suffix.lower() == '.pdf'
        allowed = allowed_roles(name)
        for role, box in widgets['roles'].items():
            box.blockSignals(True)
            box.setChecked(role in roles)
            box.blockSignals(False)
            tip = self._tr(ROLES[role]) + '\n' + self._tr(ROLE_HELP[role])
            if role == PHOTO_ROLE and is_pdf:
                # Il rilevamento dei componenti decodifica immagini, non PDF.
                tip = self._tr('Una foto deve essere un’immagine, non un PDF.')
            elif role not in allowed:
                # Utente, 27/09: un PDF e' solo un datasheet.
                tip = self._tr('Un PDF può avere solo il ruolo Datasheet: le viste che '
                               'contiene le cerca l’AI.')
            # Esplicito in entrambi i versi: set_busy riaccende tutte le caselle.
            box.setEnabled(role in allowed)
            box.setToolTip(tip)
        verdict = self._calibration_verdict(roles, path)
        if verdict is None:
            widgets['verdict'].setText(self._tr('File mancante') if path is None else '')
            widgets['verdict'].setToolTip('')
            widgets['verdict'].setStyleSheet(f'color: {_BAD};')
        else:
            icon, summary, explanation, colour = verdict
            # La card e' stretta: resta l'icona con il dato che conta, la
            # spiegazione si legge passandoci sopra.
            widgets['verdict'].setText(f'{icon}  {summary}'.rstrip())
            widgets['verdict'].setToolTip(explanation)
            widgets['verdict'].setStyleSheet(f'color: {colour};')
        widgets['view'].setEnabled(path is not None)
        widgets['preview'].clear()
        if path is not None:
            try:
                pixmap = document_pixmap(path, QSize(CARD_WIDTH - 24, PREVIEW_HEIGHT))
                if pixmap.isNull():
                    widgets['preview'].setText('PDF' if is_pdf else 'Anteprima non disponibile')
                else:
                    widgets['preview'].setPixmap(pixmap)
            except (ValueError, OSError):
                widgets['preview'].setText('Anteprima non disponibile')
        else:
            widgets['preview'].setText('File mancante')

    def _fill_badges(self, covered):
        """One label per role above the cards: covered (by which files) or not provided."""
        for role, badge in self.role_badges.items():
            files = covered[role]
            label = self._tr(ROLE_SHORT[role])
            if files:
                badge.setText(f'✓ {label}')
                badge.setStyleSheet(f'color: {_OK};')
                badge.setToolTip(self._tr(ROLES[role]) + ': ' + ', '.join(files))
            else:
                badge.setText(f'– {label}')
                badge.setStyleSheet(f'color: {_NEUTRAL};')
                tip = self._tr(ROLES[role]) + ': ' + self._tr('non fornito.')
                if role == PHOTO_ROLE:
                    tip += ' ' + self._tr('Senza una foto i componenti montati non vengono '
                                          'individuati.')
                badge.setToolTip(tip)

    # ── Azioni ──────────────────────────────────────────────────────

    def _choose_file(self, title):
        from .app_config import browse_start_dir, remember_browse_dir
        source, _ = QFileDialog.getOpenFileName(
            self, self._tr(title), browse_start_dir(self.config, self.directory()),
            self._tr('Immagini e PDF (*.png *.jpg *.jpeg *.webp *.gif *.pdf)'))
        if source:
            remember_browse_dir(self.config, source)
        return source

    def _add(self):
        """Copy a new document into the project; its roles are ticked afterwards."""
        self._notice = ''
        source = self._choose_file('Aggiungi documento')
        if not source:
            return
        try:
            store = self.store()
            before = {name for name, _roles, _original in store.documents()}
            self.set_busy(ADD_KEY, True)
            target = store.import_file([], source)
            if target.name in before:
                self._notice = tx("Questo file è già nel progetto come «{name}»: spunta lì i "
                                  "ruoli che contiene.", name=target.name)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non caricato'), str(exc))
        finally:
            self.set_busy(ADD_KEY, False)
            self.refresh()

    def _replace(self, name):
        """Replace the file of one card, keeping its roles; the old copy goes to docs/.history."""
        self._notice = ''
        source = self._choose_file('Sostituisci documento')
        if not source:
            return
        try:
            if QMessageBox.question(
                    self, self._tr('Sostituisci documento'),
                    self.i18n.tr('document.replace_question', role=name)
                    if self.i18n is not None else
                    tx("Sostituire il file per “{ROLES}”? La copia precedente resterà in "
                       "docs/.history.", ROLES=name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.set_busy(name, True)
            self.store().import_file([], source, replace=name)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non caricato'), str(exc))
        finally:
            self.set_busy(name, False)
            self.refresh()

    def _remove(self, name):
        self._notice = ''
        try:
            if QMessageBox.question(
                    self, self._tr('Rimuovi documento'),
                    self.i18n.tr('document.remove_question', role=name)
                    if self.i18n is not None else
                    tx("Rimuovere la copia locale di “{ROLES}”? L’originale non verrà "
                       "modificato. La copia resterà recuperabile in docs/.history.",
                       ROLES=name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.store().remove(name)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non aggiornato'), str(exc))
        finally:
            self.refresh()

    def _toggle_role(self, name, role, checked):
        """Store the roles ticked on one card; a refused change is shown and undone."""
        widgets = self.cards.get(name)
        if widgets is None:
            return
        roles = [r for r, box in widgets['roles'].items() if box.isChecked()]
        self._notice = ''
        try:
            self.store().set_roles(name, roles)
            self.changed.emit()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self._tr('Documento non aggiornato'), str(exc))
        finally:
            self.refresh()

    def _preview(self, name):
        try:
            path = Path(self.directory()) / name
            if not path.is_file():
                raise ValueError(tx("Il file non è più presente. Caricalo nuovamente."))
            dialog = QDialog(self)
            dialog.setWindowTitle(name)
            dialog.setProperty('i18n_skip', True)
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
        show_info(self, 'Documenti per l’AI', '\n\n'.join(
            f'{ROLES[role]} ({role}): {ROLE_HELP[role]}' for role in ROLES) + '\n\n'
            'Un PDF è sempre un datasheet (datasheet): riceve quel ruolo da solo e l’AI vi '
            'cerca pinout, quote, piazzole e vista laterale. Un’immagine può avere più ruoli: '
            'spunta tutti quelli che contiene; '
            'un file senza ruoli blocca l’analisi finché non ne scegli uno o lo rimuovi. '
            'Solo i file spuntati come foto (hires) vanno al riconoscimento dei componenti e '
            'alla calibrazione: devono essere immagini PNG/JPEG/WEBP, non PDF. '
            'L’app copia i file in docs mantenendo nome ed estensione; gli originali restano '
            'invariati. Sostituzioni e rimozioni sono recuperabili in docs/.history. '
            'L’AI riceve i ruoli esplicitamente; i ruoli non forniti non vengono sostituiti '
            'con altri file.',
            text_key='help.guided_documents')
