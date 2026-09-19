"""Non-blocking progress overlay: real status, elapsed time, no fake percentage."""
from time import monotonic

from PyQt6.QtCore import QEvent, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


class AnalysisSpinner(QWidget):
    """@brief Show an indeterminate animated indicator during AI work."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.angle = 0

    def paintEvent(self, event):
        """@brief Provide the paintEvent operation for this workflow."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(7, 7, 42, 42)
        painter.setPen(QPen(QColor("#45475a"), 3))
        painter.drawEllipse(rect)
        pen = QPen(QColor("#89b4fa"), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self.angle * 16, 100 * 16)


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
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setMinimumHeight(60)
        content.addWidget(self.status)
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
        """@brief Provide the eventFilter operation for this workflow."""
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def start(self, title='Analisi AI in corso', status='Preparazione dei documenti…'):
        """@brief Provide the start operation for this workflow."""
        self.title.setText(self.i18n.text(title) if self.i18n is not None else title)
        self.started_at = monotonic()
        self.spinner.angle = 0
        self.cost.clear()
        self.cost.hide()
        self.set_status(status)
        self.setGeometry(self.parentWidget().rect())
        self._tick()
        self.show()
        self.raise_()
        self.log_button.setFocus()
        self.timer.start()

    def set_cost(self, text):
        """Mostra il consuntivo dell'analisi AI dentro la modale.

        Durante la generazione automatica la modale resta aperta e copre la
        label sotto il pulsante: senza questa riga il costo passava senza
        essere visto, pur essendo gia' calcolato.
        """
        self.cost.setText(str(text or ""))
        self.cost.setVisible(bool(text))

    def set_status(self, message):
        """@brief Provide the set status operation for this workflow."""
        # Full messages stay in the log; keep the card stable for long replies.
        source = " ".join(str(message).split())
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
        """@brief Provide the stop operation for this workflow."""
        self.timer.stop()
        self.hide()

    def hideEvent(self, event):
        """@brief Provide the hideEvent operation for this workflow."""
        self.timer.stop()
        super().hideEvent(event)
