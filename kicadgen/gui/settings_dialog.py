"""@brief LLM settings dialog: default models and system prompts of the extraction.

Impostazioni LLM: modello di default e prompt di sistema dell'estrazione.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout, QTabWidget, QWidget,
)

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.openrouter import DEFAULT_MODEL, PHOTO_CAPABILITIES
from ..services.photo_grounding import DEFAULT_VISION_MODEL, GROUNDING_PROMPT, VERIFICATION_PROMPT
from .model_dialog import pick_model


class LlmSettingsDialog(QDialog):
    """@brief Editor of the default models and of the system prompts used by the analyses.

    Editor del modello di default e del prompt di sistema.

    Ogni analisi ha modello e prompt globali, ereditati dalle tipologie
    che non hanno un override nel profilo SQLite:

    * il **modello di default**, scelto dall'elenco OpenRouter filtrando per
      costo -- e' quello usato quando nel tab "Progetto" non se ne indica
      uno diverso per la singola estrazione;
    * il **prompt di sistema**, che serve a correggere il comportamento del
      modello (per esempio una quota letta dalla vista sbagliata del
      disegno) senza dover toccare il codice.
    """

    def __init__(self, current_prompt: str, current_model: str | None = None, parent=None,
                 current_vision_model: str | None = None,
                 current_grounding_prompt=None, current_verification_prompt=None,
                 current_verification_model=None):
        super().__init__(parent)
        self.i18n = getattr(parent, 'i18n', None)
        self.setWindowTitle("Modello e prompt di sistema LLM")
        self.resize(820, 680)
        self.setModal(True)

        self._model = (current_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._vision_model = (current_vision_model or DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL
        self._verification_model = (current_verification_model or self._model).strip() or self._model

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
        self.info_buttons = []
        model_row.addWidget(self._info_button(0))
        model_form.addRow("Modello di default:", model_row)
        self.field_vision_model = QLineEdit(self._vision_model)
        self.field_vision_model.setReadOnly(True)
        vision_button = QPushButton("Scegli modello foto...")
        vision_button.clicked.connect(self._pick_vision_model)
        vision_row = QHBoxLayout()
        vision_row.addWidget(self.field_vision_model)
        vision_row.addWidget(vision_button)
        vision_row.addWidget(self._info_button(1))
        photo_form = QFormLayout()
        photo_form.addRow("Modello foto:", vision_row)
        photo_layout.addLayout(photo_form)
        datasheet_layout.addLayout(model_form)

        model_note = (
            "Il modello si sceglie dall'elenco OpenRouter, filtrabile per costo. "
            "Questo e' il default generale, ereditato dalle tipologie senza override. Per un datasheet "
            "difficile se ne puo' usare uno piu' potente dal tab \"Progetto\", "
            "solo per quella sessione, senza cambiare il default. "
            "La chiave OpenRouter si inserisce da Configurazione → «Account AI...»."
        )
        vision_note = (
            "Le foto usano un modello dedicato alla localizzazione; il modello di verifica foto conferma "
            "poi solo la presenza di corpi montati, senza spostarli o riconoscere il circuito. "
            "L'affidabilità delle coordinate dipende dal modello e dalla qualità della foto. "
            "Le risposte foto sono memorizzate "
            "nella cartella photo-cache del progetto; a input invariati non vengono "
            "ripetute le chiamate. "
            "Il 3D usa ingombri semplici senza scritte. Le quote XY sono stime calibrate; "
            "le altezze sono indicative e richiedono una misura diretta."
        )
        intro = (
            "Prompt di sistema: istruzioni inviate al modello durante "
            "l'estrazione dal datasheet (tab Progetto -> \"Estrai parametri con "
            "AI\"). Sono default generali salvati in SQLite, personalizzabili "
            "per tipologia da Gestione tipologie e profili AI.\n\n"
            "Nota: lo schema dei campi restituiti (righe, colonne, pitch, body_*, "
            "edge_fillets, colori...) e' fisso e NON si modifica da qui -- e' quello "
            "che garantisce che la risposta arrivi sempre come JSON strutturato valido."
        )
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
        self.field_verification_model = QLineEdit(self._verification_model)
        self.field_verification_model.setReadOnly(True)
        verification_button = QPushButton('Scegli modello...')
        verification_button.clicked.connect(self._pick_verification_model)
        verify_row = QHBoxLayout()
        verify_row.addWidget(self.verification_model_label)
        verify_row.addWidget(self.field_verification_model, 1)
        verify_row.addWidget(verification_button)
        verify_row.addWidget(self._info_button(2))
        verify_layout.addLayout(verify_row)
        verify_note = ("Conferma solo la presenza dei corpi montati nei rettangoli proposti. "
                            "Non modifica coordinate né aggiunge ID. Usa un modello indipendente, "
                            "inclusa l'eventuale scelta di verifica per la sessione.\n\n"
                            "Lo schema JSON e i controlli geometrici restano gestiti dal codice.")
        self._help_texts = (model_note + "\n\n" + intro, vision_note, verify_note)
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

    def _info_button(self, index: int) -> QPushButton:
        """Keep explanations out of the editor, with keyboard-accessible help."""
        title = self.tabs.tabText(index)
        button = QPushButton()
        button.setAutoDefault(False)
        button.setIcon(QIcon(str(Path(__file__).parent / "assets" / "info.svg")))
        button.setIconSize(QSize(20, 20))
        button.setFixedSize(32, 32)
        info_label = (
            self.i18n.tr("tooltip.info_section", section=self.i18n.text(title))
            if self.i18n is not None else f"Informazioni: {title}"
        )
        button.setToolTip(info_label)
        button.setAccessibleName(info_label)
        button.setObjectName(f"analysis_info_{index}")
        button.clicked.connect(lambda checked=False: self._show_info(index))
        self.info_buttons.append(button)
        return button

    def _show_info(self, index: int) -> None:
        dialog = QDialog(self)
        prefix = self.i18n.text("Informazioni") if self.i18n is not None else "Informazioni"
        dialog.setWindowTitle(f"{prefix} — {self.tabs.tabText(index)}")
        dialog.setModal(True)
        dialog.resize(640, 360)
        layout = QVBoxLayout(dialog)
        explanation = QTextEdit()
        explanation.setReadOnly(True)
        explanation.setPlainText(
            self.i18n.tr(f"help.settings.stage.{index}", default=self._help_texts[index])
            if self.i18n is not None else self._help_texts[index]
        )
        layout.addWidget(explanation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Chiudi")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(dialog)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

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
            self._verification_model = DEFAULT_MODEL
            self.field_verification_model.setText(DEFAULT_MODEL)

    def _refresh_verification_label(self):
        source = 'Modello verifica:'
        self.verification_model_label.setText(
            self.i18n.text(source) if self.i18n is not None else source
        )

    def _pick_verification_model(self):
        # La verifica lavora solo su fotografie: chiedere anche la lettura dei
        # PDF escluderebbe modelli fatti per le immagini.
        chosen = pick_model(self._verification_model, self, title='Modello per verifica foto',
                            capabilities=PHOTO_CAPABILITIES)
        if chosen:
            self._verification_model = chosen
            self.field_verification_model.setText(chosen)

    def verification_model(self):
        """@brief Return the model chosen for the verification pass (defaults to the main model)."""
        return self._verification_model

    def grounding_prompt(self):
        """@brief Return the edited grounding prompt, or the built-in one when left empty."""
        return self.grounding_editor.toPlainText().strip() or GROUNDING_PROMPT

    def verification_prompt(self):
        """@brief Return the edited verification prompt, or the built-in one when left empty."""
        return self.verification_editor.toPlainText().strip() or VERIFICATION_PROMPT

    def _pick_vision_model(self) -> None:
        chosen = pick_model(self._vision_model, self, title="Modello per localizzazione fotografica",
                            capabilities=PHOTO_CAPABILITIES)
        if chosen:
            self._vision_model = chosen
            self.field_vision_model.setText(chosen)

    def vision_model(self) -> str:
        """@brief Return the chosen vision model, or the default vision model when empty."""
        return self._vision_model.strip() or DEFAULT_VISION_MODEL

    def prompt(self) -> str:
        """@brief Return the edited extraction system prompt, stripped."""
        return self.editor.toPlainText().strip()

    def model(self) -> str:
        """@brief Return the chosen default model, or DEFAULT_MODEL when empty."""
        return self._model.strip() or DEFAULT_MODEL


#: Nome storico della classe, prima che il dialogo ospitasse anche il modello.
SystemPromptDialog = LlmSettingsDialog
