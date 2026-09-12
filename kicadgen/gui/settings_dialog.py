"""Impostazioni LLM: modello di default e prompt di sistema dell'estrazione."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout, QTabWidget, QWidget,
)

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.openrouter import DEFAULT_MODEL
from ..services.photo_grounding import DEFAULT_VISION_MODEL, GROUNDING_PROMPT, VERIFICATION_PROMPT
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

    def __init__(self, current_prompt: str, current_model: str | None = None, parent=None,
                 current_vision_model: str | None = None,
                 current_grounding_prompt=None, current_verification_prompt=None):
        super().__init__(parent)
        self.setWindowTitle("Modello e prompt di sistema LLM")
        self.resize(820, 680)
        self.setModal(True)

        self._model = (current_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._vision_model = (current_vision_model or DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        pages = []
        for title in ("Datasheet", "Localizzazione foto", "Verifica foto"):
            page = QWidget()
            pages.append(QVBoxLayout(page))
            self.tabs.addTab(page, title)
        datasheet_layout, photo_layout, verify_layout = pages

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
        self.field_vision_model = QLineEdit(self._vision_model)
        self.field_vision_model.setReadOnly(True)
        vision_button = QPushButton("Scegli modello foto...")
        vision_button.clicked.connect(self._pick_vision_model)
        vision_row = QHBoxLayout()
        vision_row.addWidget(self.field_vision_model)
        vision_row.addWidget(vision_button)
        photo_form = QFormLayout()
        photo_form.addRow("Modello foto:", vision_row)
        photo_layout.addLayout(photo_form)
        datasheet_layout.addLayout(model_form)

        model_note = QLabel(
            "Il modello si sceglie dall'elenco OpenRouter, filtrabile per costo. "
            "Questo e' il default di tutte le estrazioni; per un datasheet "
            "difficile se ne puo' usare uno piu' potente dal tab \"Progetto\", "
            "solo per quella sessione, senza cambiare il default. "
            "Serve la chiave OPENROUTER_API_KEY nel file .env."
        )
        model_note.setWordWrap(True)
        datasheet_layout.addWidget(model_note)
        vision_note = QLabel(
            "Le foto usano un modello dedicato alla localizzazione; il modello datasheet verifica "
            "poi solo la presenza di corpi montati, senza spostarli o riconoscere il circuito. "
            "Il default Gemini 3.1 Pro è stato verificato sul modulo di esempio; altri modelli "
            "possono produrre coordinate meno affidabili. Le risposte foto sono memorizzate "
            "in generated/photo-cache; a input invariati non vengono ripetute le chiamate. "
            "Il 3D usa ingombri semplici senza scritte. Le quote XY sono stime calibrate; "
            "le altezze sono indicative e richiedono una vista laterale o una misura."
        )
        vision_note.setWordWrap(True)
        photo_layout.addWidget(vision_note)

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
        datasheet_layout.addWidget(intro)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlainText(current_prompt)
        datasheet_layout.addWidget(self.editor)
        self.grounding_editor = QTextEdit()
        self.grounding_editor.setAcceptRichText(False)
        self.grounding_editor.setPlainText(current_grounding_prompt or GROUNDING_PROMPT)
        photo_layout.addWidget(self.grounding_editor)
        self.verification_model_label = QLabel()
        self._refresh_verification_label()
        verify_layout.addWidget(self.verification_model_label)
        verify_note = QLabel("Conferma solo la presenza dei corpi montati nei rettangoli proposti. "
                            "Non modifica coordinate né aggiunge ID. Usa il modello datasheet, "
                            "inclusa l'eventuale scelta per la sessione.\n\n"
                            "Lo schema JSON e i controlli geometrici restano gestiti dal codice.")
        verify_note.setWordWrap(True)
        verify_layout.addWidget(verify_note)
        self.verification_editor = QTextEdit()
        self.verification_editor.setAcceptRichText(False)
        self.verification_editor.setPlainText(current_verification_prompt or VERIFICATION_PROMPT)
        verify_layout.addWidget(self.verification_editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.RestoreDefaults
            | QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText(
            "Ripristina default di questo tab"
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
            self._refresh_verification_label()

    def _restore_defaults(self) -> None:
        if self.tabs.currentIndex() == 0:
            self.editor.setPlainText(DEFAULT_SYSTEM_PROMPT)
            self._model = DEFAULT_MODEL
            self.field_model.setText(DEFAULT_MODEL)
            self._refresh_verification_label()
        elif self.tabs.currentIndex() == 1:
            self.grounding_editor.setPlainText(GROUNDING_PROMPT)
            self._vision_model = DEFAULT_VISION_MODEL
            self.field_vision_model.setText(DEFAULT_VISION_MODEL)
        else:
            self.verification_editor.setPlainText(VERIFICATION_PROMPT)

    def _refresh_verification_label(self):
        self.verification_model_label.setText(f"Modello condiviso con Datasheet: {self._model}")

    def grounding_prompt(self):
        return self.grounding_editor.toPlainText().strip() or GROUNDING_PROMPT

    def verification_prompt(self):
        return self.verification_editor.toPlainText().strip() or VERIFICATION_PROMPT

    def _pick_vision_model(self) -> None:
        chosen = pick_model(self._vision_model, self, title="Modello per localizzazione fotografica")
        if chosen:
            self._vision_model = chosen
            self.field_vision_model.setText(chosen)

    def vision_model(self) -> str:
        return self._vision_model.strip() or DEFAULT_VISION_MODEL

    def prompt(self) -> str:
        return self.editor.toPlainText().strip()

    def model(self) -> str:
        return self._model.strip() or DEFAULT_MODEL


#: Nome storico della classe, prima che il dialogo ospitasse anche il modello.
SystemPromptDialog = LlmSettingsDialog
