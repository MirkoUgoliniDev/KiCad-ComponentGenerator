"""Non-blocking progress overlay: which tool is working, real progress only."""
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
_MACHINE_MARKER = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\s*[: =]")


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
            # Nuvola: la chiamata esce dalla macchina e si paga.
            path = QPainterPath()
            path.addEllipse(QRectF(19, 26, 12, 12))
            path.addEllipse(QRectF(26, 22, 14, 14))
            path.addEllipse(QRectF(23, 30, 14, 9))
            painter.drawPath(path.simplified())
        elif self.glyph == "frame":
            # Riquadro con gli angoli staccati: e' cio' che il rilevatore
            # disegna intorno a un corpo.
            for x, y, dx, dy in ((18, 18, 1, 1), (38, 18, -1, 1),
                                 (18, 38, 1, -1), (38, 38, -1, -1)):
                painter.drawLine(QPointF(x, y), QPointF(x + 6 * dx, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y + 6 * dy))
            painter.drawEllipse(QRectF(26, 26, 4, 4))
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
            # Due file di piazzole: il footprint.
            for row in (21, 33):
                for column in (18, 25, 32):
                    painter.drawRoundedRect(QRectF(column, row, 6, 4), 1.5, 1.5)


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
        self.card.setMaximumWidth(560)
        content = QVBoxLayout(self.card)
        content.setContentsMargins(28, 24, 28, 24)
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
        self.status.setMinimumHeight(60)
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
        outer.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)
        self.started_at = 0.0
        parent.installEventFilter(self)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)
        self.hide()

    def eventFilter(self, watched, event):
        """@brief Keep the overlay covering its parent as the window resizes."""
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def start(self, title='Analisi AI in corso', status='Preparazione dei documenti…'):
        """@brief Show the overlay for a new operation, with no tool yet claimed."""
        self.title.setText(self.i18n.text(title) if self.i18n is not None else title)
        self.started_at = monotonic()
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
        """@brief Show one line of human status, never a service marker.

        @note Le righe che il programma scrive per se' (AI_CALL_..., sha256,
            esiti di importazione) restano nel log e non arrivano qui: sulla
            scheda occupavano il posto di cio' che stava accadendo senza dirlo.
            Il pulsante del log resta a un clic di distanza.
        """
        # Full messages stay in the log; keep the card stable for long replies.
        source = " ".join(str(message).split())
        if _MACHINE_MARKER.match(source):
            return
        text = self.i18n.text(source) if self.i18n is not None else source
        self.status.setText(text if len(text) <= 220 else text[:217] + "…")

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
        """@brief Stop the animation whenever the overlay goes away."""
        self.timer.stop()
        super().hideEvent(event)
