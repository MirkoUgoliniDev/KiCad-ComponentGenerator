"""Non-blocking progress overlay: real status, elapsed time, no fake percentage."""
from time import monotonic

from PyQt6.QtCore import QEvent, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


class AnalysisSpinner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.angle = 0

    def paintEvent(self, event):
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
    log_requested = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
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
        title = QLabel("Analisi AI in corso")
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
        self.hide()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def start(self):
        self.started_at = monotonic()
        self.spinner.angle = 0
        self.set_status("Preparazione dei documenti…")
        self.setGeometry(self.parentWidget().rect())
        self._tick()
        self.show()
        self.raise_()
        self.log_button.setFocus()
        self.timer.start()

    def set_status(self, message):
        # Full messages stay in the log; keep the card stable for long replies.
        text = " ".join(str(message).split())
        self.status.setText(text if len(text) <= 220 else text[:217] + "…")

    def _tick(self):
        seconds = max(0, int(monotonic() - self.started_at))
        self.elapsed.setText(f"Tempo trascorso  {seconds // 60:02d}:{seconds % 60:02d}")
        self.spinner.angle = (self.spinner.angle + 9) % 360
        self.spinner.update()

    def stop(self):
        self.timer.stop()
        self.hide()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)
