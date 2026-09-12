"""Impostazioni LLM: modello di default e prompt di sistema dell'estrazione."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout,
)

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.openrouter import DEFAULT_MODEL
from .model_dialog import pick_model


class LlmSettingsDialog(QDialog):
    """Editor del modello di default e del prompt di sistema.

    Sono le due impostazioni globali dell'estrazione AI (valgono per ogni
    componente e restano salvate in config.json):

    * il **modello di default**, scelto dall'elenco OpenRouter filtrando per
      costo -- e' quello usato quando nel tab "Progetto" non se ne indica
      uno diverso per la singola estrazione;
    * il **prompt di sistema**, che serve a correggere il comportamento del
      modello (per esempio una quota letta dalla vista sbagliata del
      disegno) senza dover toccare il codice.
    """

    def __init__(self, current_prompt: str, current_model: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Modello e prompt di sistema LLM")
        self.resize(760, 620)

        self._model = (current_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL

        layout = QVBoxLayout(self)

        model_form = QFormLayout()
        self.field_model = QLineEdit(self._model)
        self.field_model.setReadOnly(True)
        self.field_model.setToolTip(
            "Id OpenRouter del modello usato di default dall'estrazione AI."
        )
        btn_pick = QPushButton("Scegli modello...")
        btn_pick.clicked.connect(self._pick_model)
        model_row = QHBoxLayout()
        model_row.addWidget(self.field_model)
        model_row.addWidget(btn_pick)
        model_form.addRow("Modello di default:", model_row)
        layout.addLayout(model_form)

        model_note = QLabel(
            "Il modello si sceglie dall'elenco OpenRouter, filtrabile per costo. "
            "Questo e' il default di tutte le estrazioni; per un datasheet "
            "difficile se ne puo' usare uno piu' potente dal tab \"Progetto\", "
            "solo per quella sessione, senza cambiare il default. "
            "Serve la chiave OPENROUTER_API_KEY nel file .env."
        )
        model_note.setWordWrap(True)
        layout.addWidget(model_note)

        intro = QLabel(
            "Prompt di sistema: istruzioni inviate al modello durante "
            "l'estrazione dal datasheet (tab Progetto -> \"Estrai parametri con "
            "AI\"). Valgono per tutti i componenti e restano salvate tra una "
            "sessione e l'altra.\n\n"
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
            self._restore_defaults
        )
        layout.addWidget(buttons)

    def _pick_model(self) -> None:
        chosen = pick_model(self._model, self, title="Modello di default")
        if chosen:
            self._model = chosen
            self.field_model.setText(chosen)

    def _restore_defaults(self) -> None:
        self.editor.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self._model = DEFAULT_MODEL
        self.field_model.setText(DEFAULT_MODEL)

    def prompt(self) -> str:
        return self.editor.toPlainText().strip()

    def model(self) -> str:
        return self._model.strip() or DEFAULT_MODEL


#: Nome storico della classe, prima che il dialogo ospitasse anche il modello.
SystemPromptDialog = LlmSettingsDialog
