"""Non-blocking progress overlay: which tool is working, real progress only."""
import math
import re
from time import monotonic

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QVBoxLayout, QWidget)

#: Gli strumenti che possono lavorare durante un'analisi, ognuno col proprio
#: segno grafico. La distinzione che conta per chi guarda non e' estetica: il
#: rilevatore locale non costa nulla e dura poco, la chiamata al modello si
#: paga, il CAD tiene occupata la macchina per minuti. Il colore da solo non
#: basta a dirlo, quindi ogni strumento ha anche una forma diversa.
TOOLS = {
    "llm": ("Modello AI (remoto, a consumo)", "#f9e2af", "cloud"),
    "yolo": ("YOLO (rilevamento locale)", "#a6e3a1", "frame"),
    "freecad": ("FreeCAD (modellazione locale)", "#cba6f7", "cube"),
    "kicad": ("KiCad (footprint e libreria)", "#89b4fa", "pads"),
}

#: Righe che il programma scrive per se' stesso: marcatori delle chiamate AI,
#: esiti di importazione, impronte sha256. Vanno nel log, dove servono a
#: ricostruire una corsa, non sulla scheda, dove occupano il posto di cio' che
#: sta accadendo senza dirlo a nessuno.
#:
#: Il marcatore si cerca ovunque nella riga e non solo in testa: chi inoltra
#: l'uscita di freecadcmd le antepone "FreeCAD: ", e con l'ancora iniziale
#: passavano tutte. Serve almeno un underscore fra maiuscole, cosi' una parola
#: sola come "ATTENZIONE:" resta una frase per chi guarda.
_MACHINE_MARKER = re.compile(r"(?<![\w])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\s*[: =]")

#: Il marcatore sopra riconosce solo cio' che *questo* programma stampa per se'.
#: Sulla scheda arrivavano anche le righe che i programmi esterni scrivono sul
#: proprio stdout (esiti di scrittura file, intestazioni di sessione, conteggi
#: di entita'): non hanno un marcatore, quindi vanno riconosciute dalla *forma*.
#: Le regole qui sotto non nominano nessun prodotto e nessuna frase precisa --
#: agganciarle a "FreeCAD" o all'elenco delle frasi viste in una corsa vorrebbe
#: dire rifare il lavoro alla prossima versione dello stesso strumento.
#:
#: Ogni regola e' un'euristica tipografica, tarata su come scrive una macchina
#: e su come scrive una persona; sono dichiarate tali perche' nessuna e' una
#: verita' generale, e diventerebbero superflue se gli strumenti esterni
#: marcassero le proprie righe come fa questo programma.

#: Decorazione: tre o piu' ripetizioni dello stesso segno di interpunzione
#: ("******", "=====", "-----"). Il punto e' escluso: i puntini di sospensione
#: sono normalissimi in una frase scritta per chi guarda.
_DECORATION_RUN = re.compile(r"([^\w\s.])\1{2,}")

#: Chiave e valore separati da spazio-due punti-spazio ("Step File Name : C:/…",
#: "WorkSession : Sending all data"). In prosa italiana o inglese i due punti
#: stanno attaccati alla parola che precede, quindi lo spazio davanti e' il
#: segno che la riga e' un campo di un tracciato, non una frase.
_SPACED_KEY_VALUE = re.compile(r"\S\s+:\s+\S")

#: Percorso assoluto del filesystem: lettera di unita' ("C:/…", "D:\\…"),
#: percorso UNC, o percorso POSIX con almeno due livelli ("/home/x/…"). Un
#: percorso e' un dettaglio da log: identifica un file sul disco, non dice
#: cosa sta accadendo. Il solo nome di un file (senza separatori) resta una
#: frase leggibile e non viene toccato.
_DRIVE_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/]")
_UNC_PATH = re.compile(r"\\\\[\w.$-]+\\")
_POSIX_PATH = re.compile(r"(?:^|(?<=[\s(\"'=]))~?/[\w.+-]+/")

#: Conteggio di entita' fra parentesi: numero grande seguito da un'abbreviazione
#: corta ("(134238 ents)"). Il vincolo sulle quattro cifre e sull'abbreviazione
#: breve serve a non confondere questa forma con un conteggio scritto per chi
#: guarda, del tipo "(3 componenti)", che ha pochi numeri e la parola intera.
_ENTITY_COUNT = re.compile(r"\(\s*\d{4,}\s+\w{1,6}\.?\s*\)")

_MACHINE_LINE_RULES = (_MACHINE_MARKER, _DECORATION_RUN, _SPACED_KEY_VALUE,
                       _DRIVE_PATH, _UNC_PATH, _POSIX_PATH, _ENTITY_COUNT)

#: Lo stato viene troncato: oltre questa lunghezza la riga non si legge a colpo
#: d'occhio e il testo integrale e' comunque nel log.
_STATUS_CHAR_LIMIT = 220

#: Righe di testo riservate allo stato. La scheda e' dimensionata su questo
#: numero, non sulla frase che passa in quel momento: una scheda che cambia
#: altezza a ogni messaggio e' piu' fastidiosa di una scheda un po' piu' alta.
_STATUS_LINES = 3

#: Margine interno orizzontale della scheda (per lato), usato sia dal layout sia
#: dal calcolo della larghezza: vanno d'accordo per costruzione.
_CARD_PADDING = 28

#: Estremi della larghezza calcolata. Il minimo evita una scheda smilza con
#: caratteri molto stretti, il massimo evita una riga larga mezzo monitor, che
#: si legge male qualunque sia il font. Non dipendono dai testi di una corsa.
_CARD_WIDTH_BOUNDS = (480, 760)


def is_machine_line(text):
    """@brief Tell a line written for the log from a line written for a person.

    @param text One status line, already collapsed to single spaces.
    @return True when the line is machine bookkeeping and belongs in the log.
    @note Recognition is structural (markers, decoration runs, spaced key/value
        fields, absolute paths, entity counters); no rule names a product or a
        specific sentence, so a new external tool needs no new rule. A human
        sentence carrying quotes, parentheses, units or a double dash keeps
        none of those shapes and still reaches the card.
    """
    line = " ".join(str(text or "").split())
    return any(rule.search(line) for rule in _MACHINE_LINE_RULES)


class AnalysisSpinner(QWidget):
    """@brief Animated ring that also names the tool at work by its shape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.angle = 0
        self.glyph = ""
        self.color = "#89b4fa"

    def set_tool(self, glyph, color):
        """@brief Draw this tool's mark inside the ring.

        @param glyph One of the shapes in TOOLS, or "" for the bare ring.
        @param color Ring and mark color.
        """
        self.glyph, self.color = glyph, color
        self.update()

    def paintEvent(self, event):
        """@brief Paint the ring, its moving arc and the tool mark."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(7, 7, 42, 42)
        painter.setPen(QPen(QColor("#45475a"), 3))
        painter.drawEllipse(rect)
        pen = QPen(QColor(self.color), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self.angle * 16, 100 * 16)
        if self.glyph:
            self._paint_glyph(painter)

    def _paint_glyph(self, painter):
        """@brief Draw the 22x22 mark centred in the ring."""
        pen = QPen(QColor(self.color), 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.glyph == "cloud":
            # Nuvola: la chiamata esce dalla macchina e si paga. I tre cerchi
            # vanno *uniti*, non sovrapposti: gli archi interni si leggevano
            # come tre bolle, cioe' come nessuna forma riconoscibile.
            path = QPainterPath()
            for box in (QRectF(18, 27, 12, 11), QRectF(25, 22, 15, 15),
                        QRectF(22, 30, 15, 8)):
                blob = QPainterPath()
                blob.addEllipse(box)
                path = path.united(blob)
            painter.drawPath(path.simplified())
        elif self.glyph == "frame":
            # Mirino: quattro squadrette d'angolo piu' il bersaglio al centro.
            # Un riquadro chiuso si confondeva col footprint e col cubo alla
            # dimensione a cui viene disegnato; le squadrette staccate sono il
            # segno con cui un rilevatore marca un corpo, e il puntino pieno
            # dice che il riquadro e' *misurato*, non disegnato a mano.
            arm = 7.0
            for x, y, dx, dy in ((17, 17, 1, 1), (39, 17, -1, 1),
                                 (17, 39, 1, -1), (39, 39, -1, -1)):
                painter.drawLine(QPointF(x, y), QPointF(x + arm * dx, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y + arm * dy))
            painter.drawLine(QPointF(28, 21), QPointF(28, 24))
            painter.drawLine(QPointF(28, 32), QPointF(28, 35))
            painter.drawLine(QPointF(21, 28), QPointF(24, 28))
            painter.drawLine(QPointF(32, 28), QPointF(35, 28))
            painter.setBrush(QColor(self.color))
            painter.drawEllipse(QRectF(26, 26, 4, 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif self.glyph == "cube":
            # Cubo isometrico: si sta costruendo un solido.
            top, left, right = QPointF(28, 17), QPointF(18, 23), QPointF(38, 23)
            low_l, low_r, bottom = QPointF(18, 34), QPointF(38, 34), QPointF(28, 40)
            centre = QPointF(28, 28)
            for start, end in ((top, left), (top, right), (left, low_l),
                               (right, low_r), (low_l, bottom), (low_r, bottom),
                               (left, centre), (right, centre), (top, centre)):
                painter.drawLine(start, end)
        elif self.glyph == "pads":
            # Footprint: due file di piazzole *piene* attorno al contorno del
            # corpo. Le piazzole vuote si leggevano come un altro riquadro; il
            # pieno del rame contro il filo di contorno distingue subito questo
            # segno dal solido del CAD, che e' tutto wireframe.
            painter.setBrush(QColor(self.color))
            for row in (17.0, 35.0):
                for column in (18, 25, 32):
                    painter.drawRoundedRect(QRectF(column, row, 6, 4), 1.2, 1.2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Contorno del corpo fra le due file, staccato dalle piazzole, col
            # segno del pin 1 a sinistra: e' come si legge un footprint.
            painter.drawRect(QRectF(20, 24, 16, 8))
            painter.drawLine(QPointF(20, 27), QPointF(24, 27))


class AnalysisOverlay(QWidget):
    """@brief Present analysis progress and expose the AI log action."""
    log_requested = pyqtSignal()

    def __init__(self, parent, *, i18n=None):
        super().__init__(parent)
        self.i18n = i18n
        self.setObjectName("analysisOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#analysisOverlay { background: rgba(10, 12, 22, 205); }
            QFrame#analysisCard { background: #1e1e2e; border: 1px solid #585b70;
                                 border-radius: 16px; }
            QLabel { background: transparent; border: none; color: #cdd6f4; }
            QProgressBar { background: #313244; border: none; border-radius: 5px;
                           height: 10px; text-align: center; color: transparent; }
            QProgressBar::chunk { background: #89b4fa; border-radius: 5px; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch()
        self.card = QFrame()
        self.card.setObjectName("analysisCard")
        content = QVBoxLayout(self.card)
        content.setContentsMargins(_CARD_PADDING, 24, _CARD_PADDING, 24)
        content.setSpacing(16)
        self.spinner = AnalysisSpinner()
        content.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.title = title = QLabel("Analisi AI in corso")
        title.setStyleSheet("font-size: 18pt; font-weight: 600;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(title)
        # Chi sta lavorando, in chiaro e accanto al suo segno. Senza questa
        # riga l'unico indizio erano i messaggi di servizio che scorrevano al
        # posto dello stato, che e' esattamente cio' che non si doveva leggere.
        self.tool = QLabel()
        self.tool.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tool.setProperty("i18n_skip", True)
        self.tool.setStyleSheet("font-weight: 600;")
        self.tool.hide()
        content.addWidget(self.tool)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.status)
        # La barra compare solo quando esiste un conteggio vero da mostrare.
        # Una percentuale stimata sul tempo si ferma al 90% e resta li': dice
        # meno del nulla, perche' sembra un'informazione.
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress_label = QLabel()
        self.progress_label.setProperty("i18n_skip", True)
        self.progress_label.setStyleSheet("color: #a6adc8;")
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)
        self.progress_row = QWidget()
        self.progress_row.setLayout(progress_row)
        self.progress_row.hide()
        content.addWidget(self.progress_row)
        self.elapsed = QLabel()
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed.setStyleSheet("color: #a6adc8;")
        content.addWidget(self.elapsed)
        # Il consuntivo dell'analisi AI resta qui finche' la modale e' aperta.
        # Sta in una riga propria, non nello stato, perche' i messaggi di
        # avanzamento della generazione lo sovrascriverebbero subito: e' il
        # motivo per cui il costo non si riusciva a leggere.
        self.cost = QLabel()
        self.cost.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cost.setWordWrap(True)
        self.cost.setTextFormat(Qt.TextFormat.PlainText)
        self.cost.setStyleSheet("color: #a6e3a1;")
        self.cost.setProperty('i18n_skip', True)
        self.cost.hide()
        content.addWidget(self.cost)
        self.log_button = QPushButton("Apri log AI")
        self.log_button.clicked.connect(self.log_requested.emit)
        content.addWidget(self.log_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._size_the_card()
        outer.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)
        self.started_at = 0.0
        #: Istante in cui l'overlay e' stato nascosto a operazione in corso
        #: (revisione dei corpi): al ritorno l'orologio salta quel tempo.
        self._hidden_at = None
        parent.installEventFilter(self)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)
        self.hide()

    def _size_the_card(self):
        """Dimensiona la scheda sul contenuto, non sui testi di una corsa.

        La scheda era troppo bassa e strozzata: lo stato andava a capo male e
        le righe che compaiono a meta' operazione (strumento, barra, costo) la
        facevano crescere e rimpicciolire sotto gli occhi.

        Larghezza: lo stato e' troncato a `_STATUS_CHAR_LIMIT` caratteri, quindi
        la larghezza che serve e' quella che li distribuisce su `_STATUS_LINES`
        righe, misurata con le metriche del font effettivo (`averageCharWidth`),
        non con un numero di pixel scelto a occhio su una frase. Gli estremi
        `_CARD_WIDTH_BOUNDS` tengono il risultato leggibile con font estremi.

        Altezza: allo stato si riservano `_STATUS_LINES` righe piene (minimo, non
        massimo: una quarta riga imprevista si vede ancora), e le tre righe
        facoltative mantengono il proprio spazio anche da nascoste, cosi'
        l'altezza complessiva e' quella di una scheda piena dall'inizio.
        """
        metrics = self.status.fontMetrics()
        # Mezza riga di margine: `averageCharWidth` e' una media, e con una frase
        # di lettere larghe il testo troncato sborderebbe sulla quarta riga
        # facendo crescere la scheda. Dimensionando la larghezza su due righe e
        # mezza, tre righe bastano anche nel caso peggiore.
        chars_per_line = math.ceil(_STATUS_CHAR_LIMIT / (_STATUS_LINES - 0.5))
        low, high = _CARD_WIDTH_BOUNDS
        width = 2 * _CARD_PADDING + metrics.averageCharWidth() * chars_per_line
        width = max(low, min(high, width))
        self.card.setMinimumWidth(width)
        self.card.setMaximumWidth(width)
        self.status.setMinimumHeight(_STATUS_LINES * metrics.lineSpacing()
                                     + metrics.descent())
        # Spazio riservato anche quando la riga e' nascosta: evita che la scheda
        # salti quando lo strumento, la barra o il costo compaiono a operazione
        # avviata. La visibilita' logica non cambia (isVisibleTo resta falso).
        for row in (self.tool, self.progress_row, self.cost):
            policy = row.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            row.setSizePolicy(policy)
        self.card.setMinimumHeight(self.card.sizeHint().height())

    def eventFilter(self, watched, event):
        """@brief Keep the overlay covering its parent as the window resizes."""
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def start(self, title='Analisi AI in corso', status='Preparazione dei documenti…'):
        """@brief Show the overlay for a new operation, with no tool yet claimed."""
        self.title.setText(self.i18n.text(title) if self.i18n is not None else title)
        self.started_at = monotonic()
        self._hidden_at = None
        self.spinner.angle = 0
        self.set_tool(None)
        self.set_status(status)
        self.setGeometry(self.parentWidget().rect())
        self._tick()
        self.show()
        self.raise_()
        self.log_button.setFocus()
        self.timer.start()

    def set_tool(self, tool, detail=""):
        """@brief Name the tool now at work, with its own mark and color.

        @param tool Key in TOOLS, or None/unknown to show the bare ring.
        @param detail Free text appended in parentheses, e.g. the model name.
        @note An unknown key is treated as no tool rather than raising: a new
            phase must never be able to take down a running analysis.
        """
        name, color, glyph = TOOLS.get(str(tool or ""), ("", "#89b4fa", ""))
        self.spinner.set_tool(glyph, color)
        detail = " ".join(str(detail or "").split())
        self.tool.setText(f"{name} · {detail}" if name and detail else name)
        self.tool.setStyleSheet(f"font-weight: 600; color: {color};")
        self.tool.setVisible(bool(name))
        self.progress.setStyleSheet(
            f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}")

    def set_progress(self, done=None, total=None, unit=""):
        """@brief Show a percentage only when a real count backs it.

        @param done Items completed; None hides the bar.
        @param total Items expected; a non-positive total hides the bar too.
        @param unit Word for the items, e.g. "componenti".
        @note Deliberately no time-based estimate: a bar that stops at 90%
            because a call is slower than usual reports a guess as a fact.
        """
        countable = (type(done) is int and type(total) is int
                     and total > 0 and 0 <= done <= total)
        self.progress_row.setVisible(countable)
        if not countable:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        percent = round(done * 100 / total)
        self.progress_label.setText(
            f"{percent}% · {done}/{total} {unit}".rstrip() if unit
            else f"{percent}% · {done}/{total}")

    def clear_cost(self):
        """Azzera il consuntivo all'inizio di una NUOVA analisi AI.

        Non in start(): la modale riparte anche per la generazione FreeCAD,
        che non fa chiamate al modello. Azzerare li' faceva sparire il costo
        un attimo dopo averlo mostrato.
        """
        self.cost.clear()
        self.cost.hide()

    def set_cost(self, text):
        """Mostra il consuntivo dell'analisi AI dentro la modale.

        Durante la generazione automatica la modale resta aperta e copre la
        label sotto il pulsante: senza questa riga il costo passava senza
        essere visto, pur essendo gia' calcolato.
        """
        self.cost.setText(str(text or ""))
        self.cost.setVisible(bool(text))

    def set_status(self, message):
        """@brief Show one line of human status, never a service line.

        @param message Status line; machine bookkeeping is dropped, not shown.
        @note Le righe che il programma scrive per se' (AI_CALL_..., sha256,
            esiti di importazione) e quelle che gli strumenti esterni scrivono
            sul proprio stdout (percorsi, campi "chiave : valore", conteggi di
            entita', righe di sole decorazioni) restano nel log e non arrivano
            qui: sulla scheda occupavano il posto di cio' che stava accadendo
            senza dirlo. Il pulsante del log resta a un clic di distanza.
        @note Quando una riga viene scartata l'ultimo stato leggibile *resta*
            scritto: azzerare la label farebbe sembrare l'operazione bloccata,
            che e' il difetto che questo filtro deve evitare, non causare.
        """
        # Full messages stay in the log; keep the card stable for long replies.
        source = " ".join(str(message).split())
        if is_machine_line(source):
            return
        text = self.i18n.text(source) if self.i18n is not None else source
        self.status.setText(text if len(text) <= _STATUS_CHAR_LIMIT
                            else text[:_STATUS_CHAR_LIMIT - 3] + "…")

    def _tick(self):
        seconds = max(0, int(monotonic() - self.started_at))
        elapsed = (
            self.i18n.tr("analysis.elapsed", time=f"{seconds // 60:02d}:{seconds % 60:02d}")
            if self.i18n is not None else f"Tempo trascorso  {seconds // 60:02d}:{seconds % 60:02d}"
        )
        self.elapsed.setText(elapsed)
        self.spinner.angle = (self.spinner.angle + 9) % 360
        self.spinner.update()

    def stop(self):
        """@brief Hide the overlay and stop the animation."""
        self.timer.stop()
        self.set_progress(None, None)
        self.hide()

    def hideEvent(self, event):
        """@brief Stop the animation whenever the overlay goes away.

        @note Records when it went away: the overlay is hidden mid-operation
            while the reviewer edits the detected bodies, and the clock must
            not count that time as waiting for the model.
        """
        self.timer.stop()
        self._hidden_at = monotonic()
        super().hideEvent(event)

    def showEvent(self, event):
        """@brief Resume the animation when the overlay comes back mid-operation.

        @note `hideEvent` stops the timer; a plain `show()` after the review
            used to leave the spinner and the clock frozen at the value they
            had when hidden (seen by the user as a crash). The time spent
            hidden is subtracted, so the clock measures the operation, not
            the reviewer.
        """
        super().showEvent(event)
        if self._hidden_at is not None:
            self.started_at += max(0.0, monotonic() - self._hidden_at)
            self._hidden_at = None
        if not self.timer.isActive():
            self._tick()
            self.timer.start()
