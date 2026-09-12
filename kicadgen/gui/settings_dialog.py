"""Dialog di configurazione: prompt di sistema usato dall'estrazione AI."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QTextEdit, QVBoxLayout,
)

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT


class SystemPromptDialog(QDialog):
    """Editor del prompt di sistema, con ripristino del default.

    Il prompt e' un'impostazione globale (vale per ogni componente) e viene
    salvato in config.json: serve a correggere il comportamento del modello --
    per esempio una quota letta dalla vista sbagliata del disegno -- senza
    dover toccare il codice.
    """

    def __init__(self, current_prompt: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Prompt di sistema LLM")
        self.resize(760, 560)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Istruzioni inviate al modello durante l'estrazione dal datasheet "
            "(tab Progetto -> \"Estrai parametri con AI\"). Valgono per tutti i "
            "componenti e restano salvate tra una sessione e l'altra.\n\n"
            "Nota: lo schema dei campi restituiti (righe, colonne, pitch, body_*, "
            "edge_fillets, colori...) e' fisso e NON si modifica da qui -- e' quello "
            "che garantisce che la risposta arrivi sempre come JSON strutturato valido."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.editor = QTextEdit()
        self.editor.setPlainText(current_prompt)
        layout.addWidget(self.editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.RestoreDefaults
            | QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText(
            "Ripristina default"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            lambda: self.editor.setPlainText(DEFAULT_SYSTEM_PROMPT)
        )
        layout.addWidget(buttons)

    def prompt(self) -> str:
        return self.editor.toPlainText().strip()
