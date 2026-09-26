"""@brief Bar of the workflow steps shown above the tabs.

@details PIANO-FIX item 4 (2026-09-26): the work has precise phases —
documents, AI analysis, body review, 3D model, KiCad — and the user should see
at a glance where the open project stands. This widget only draws the steps
and reports clicks; the main window decides the state of each step from facts
of the project (files on disk, what happened in this session).
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

#: The steps, in order: (key, Italian source label).
WORKFLOW_STEPS = (
    ("documents", "Documenti"),
    ("analysis", "Analisi AI"),
    ("review", "Revisione corpi"),
    ("model3d", "Modello 3D"),
    ("kicad", "KiCad"),
)

_DONE, _CURRENT, _TODO, _SKIPPED = "#a6e3a1", "#f5e0dc", "#a6adc8", "#6c7086"


class WorkflowBar(QWidget):
    """@brief Show the workflow steps: done ones ticked, the current one highlighted."""

    #: Emitted with the step key when the user clicks a step.
    step_clicked = pyqtSignal(str)

    def __init__(self, translate=None, parent=None):
        """@brief Build one clickable label per step.

        @param translate Callable mapping an Italian source text to the
            interface language; None keeps the Italian text.
        @param parent Owner widget.
        """
        super().__init__(parent)
        self._translate = translate or (lambda text: text)
        self.setObjectName("workflowBar")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(6)
        self.labels: dict[str, QLabel] = {}
        self._state = {"done": set(), "current": None, "skipped": {}}
        for index, (key, _label) in enumerate(WORKFLOW_STEPS):
            if index:
                arrow = QLabel("›")
                arrow.setProperty("i18n_skip", True)
                arrow.setStyleSheet(f"color: {_SKIPPED};")
                layout.addWidget(arrow)
            label = QLabel()
            label.setProperty("i18n_skip", True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.mousePressEvent = (lambda event, step=key: self._clicked(event, step))
            layout.addWidget(label)
            self.labels[key] = label
        self._render()

    def _clicked(self, event, step):
        if event.button() == Qt.MouseButton.LeftButton:
            self.step_clicked.emit(step)

    def set_state(self, done, current, skipped=None):
        """@brief Show which steps are done, which one is current, which do not apply.

        @param done Keys of the completed steps.
        @param current Key of the step to do next, or None.
        @param skipped Optional {key: reason}: steps that do not apply to this
            project (e.g. the body review for a single component), drawn dimmed
            with the reason in the tooltip.
        """
        self._state = {"done": set(done), "current": current, "skipped": dict(skipped or {})}
        self._render()

    def retranslate(self):
        """@brief Redraw the labels in the current interface language."""
        self._render()

    def state(self):
        """@brief Return (done keys, current key, skipped keys) as last set."""
        return (set(self._state["done"]), self._state["current"], set(self._state["skipped"]))

    def _render(self):
        done, current, skipped = (self._state["done"], self._state["current"],
                                  self._state["skipped"])
        for number, (key, source) in enumerate(WORKFLOW_STEPS, 1):
            text = f"{number} · {self._translate(source)}"
            label = self.labels[key]
            font = label.font()
            font.setStrikeOut(key in skipped)
            font.setBold(key == current and key not in skipped and key not in done)
            label.setFont(font)
            if key in skipped:
                label.setText(text)
                label.setStyleSheet(f"color: {_SKIPPED};")
                label.setToolTip(self._translate(skipped[key]))
            elif key in done:
                label.setText(f"✓ {text}")
                label.setStyleSheet(f"color: {_DONE};")
                label.setToolTip("")
            elif key == current:
                label.setText(f"● {text}")
                label.setStyleSheet(f"color: {_CURRENT};")
                label.setToolTip("")
            else:
                label.setText(f"○ {text}")
                label.setStyleSheet(f"color: {_TODO};")
                label.setToolTip("")
            # Mai tagliata: «Revisione corpi» finiva sotto la freccia perche'
            # la barra non ricalcolava la sua larghezza dopo il cambio dei
            # testi (vedi sotto). Larghezza dal testo stesso piu' una «M» di
            # margine per i segni ✓ ● ○ presi da un carattere di riserva; non da
            # sizeHint, che includerebbe la larghezza del giro precedente.
            metrics = label.fontMetrics()
            label.setFixedWidth(metrics.horizontalAdvance(label.text())
                                + metrics.horizontalAdvance("M"))
        # La barra ha dimensione fissa: dopo aver cambiato le etichette va
        # ricalcolata, altrimenti il layout le sovrappone nello spazio vecchio.
        self.layout().invalidate()
        self.adjustSize()
        self.updateGeometry()
