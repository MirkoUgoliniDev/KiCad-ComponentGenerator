"""@brief LLM settings dialog: the single AI model with its source, and the system prompts.

Impostazioni LLM: il modello unico con la sua sorgente (DeepSeek o OpenRouter)
e i prompt di sistema dell'estrazione.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout, QTabWidget, QWidget,
)

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.messages import tx
from ..services.openrouter import DEFAULT_MODEL
from ..services.photo_grounding import GROUNDING_PROMPT, VERIFICATION_PROMPT
from ..services.providers import (PROVIDER_DEEPSEEK, PROVIDER_LABELS, PROVIDERS,
                                  provider_of)
from .model_dialog import pick_model

#: Index of the «Modello» tab and of the three prompt tabs after it.
MODEL_TAB = 0
#: Model proposed when the source changes and the current model belongs to
#: the other one: the source's usual default, to be confirmed with the picker.
SOURCE_DEFAULT_MODEL = {PROVIDER_DEEPSEEK: "deepseek-api/deepseek-flash"}


class LlmSettingsDialog(QDialog):
    """@brief Editor of the single AI model (with its source) and of the system prompts.

    Il primo tab, «Modello», sceglie la sorgente (DeepSeek, API diretta, o
    OpenRouter) e l'UNICO modello di tutte le interrogazioni: documenti, foto,
    ritagli e sigle (utente, 2026-10-02: prima tre tab mostravano tre modelli,
    ma l'analisi ne usava uno solo). Gli altri tab tengono solo i prompt.
    La scelta di sessione nella finestra principale resta una scorciatoia
    che vale fino alla chiusura.

    @note `vision_model()` e `verification_model()` restano per compatibilita'
        con chi salva i default: restituiscono lo stesso modello unico.
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

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        pages = []
        for title in ("Modello", "Datasheet", "Localizzazione foto", "Verifica foto"):
            page = QWidget()
            pages.append(QVBoxLayout(page))
            self.tabs.addTab(page, title)
        model_layout, datasheet_layout, photo_layout, verify_layout = pages
        self.info_buttons = []

        # ── Tab «Modello»: sorgente + modello unico + stato della chiave ──
        form = QFormLayout()
        self.combo_source = QComboBox()
        for provider in PROVIDERS:
            # Tradotta qui: retranslate_widgets non tocca le voci dei combo (B7).
            self.combo_source.addItem(tx(PROVIDER_LABELS[provider]), provider)
        self.combo_source.setCurrentIndex(
            max(0, self.combo_source.findData(provider_of(self._model))))
        self.combo_source.currentIndexChanged.connect(self._source_changed)
        source_row = QHBoxLayout()
        source_row.addWidget(self.combo_source, 1)
        source_row.addWidget(self._info_button(MODEL_TAB, 0))
        form.addRow("Sorgente:", source_row)
        self.field_model = QLineEdit(self._model)
        self.field_model.setReadOnly(True)
        btn_pick = QPushButton("Scegli modello...")
        btn_pick.clicked.connect(self._pick_model)
        model_row = QHBoxLayout()
        model_row.addWidget(self.field_model, 1)
        model_row.addWidget(btn_pick)
        form.addRow("Modello:", model_row)
        self.label_key = QLabel()
        self.label_key.setObjectName("model_key_state")
        self.label_key.setWordWrap(True)
        form.addRow("Chiave:", self.label_key)
        model_layout.addLayout(form)
        model_layout.addStretch(1)
        self._refresh_key_state()

        model_note = (
            "Un solo modello per tutte le interrogazioni: lettura dei documenti, "
            "fotografia, ritagli dei componenti e sigle. La sorgente e' il servizio a "
            "cui vengono inviate le richieste: DeepSeek (API diretta, chiave DeepSeek) "
            "oppure OpenRouter (chiave OpenRouter). Le chiavi si inseriscono da "
            "Configurazione → «Account AI...»; il saldo della sorgente in uso compare "
            "sotto «Estrai parametri con AI». Per una prova si puo' scegliere un altro "
            "modello solo per la sessione dal tab \"Progetto\", senza cambiare questo."
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
        vision_note = (
            "Istruzioni per la localizzazione dei corpi sulla fotografia, usate con il "
            "modello del tab «Modello». "
            "L'affidabilità delle coordinate dipende dal modello e dalla qualità della foto. "
            "Le risposte foto sono memorizzate "
            "nella cartella photo-cache del progetto; a input invariati non vengono "
            "ripetute le chiamate. "
            "Il 3D usa ingombri semplici senza scritte. Le quote XY sono stime calibrate; "
            "le altezze sono indicative e richiedono una misura diretta."
        )
        verify_note = ("Conferma solo la presenza dei corpi montati nei rettangoli proposti. "
                       "Non modifica coordinate né aggiunge ID. Usa il modello del tab «Modello».\n\n"
                       "Lo schema JSON e i controlli geometrici restano gestiti dal codice.")
        self._help_texts = (model_note, intro, vision_note, verify_note)

        # ── Tab dei prompt ──
        for prompt_layout, help_index in ((datasheet_layout, 1), (photo_layout, 2),
                                          (verify_layout, 3)):
            header = QHBoxLayout()
            header.addWidget(QLabel("Prompt di sistema:"), 1)
            header.addWidget(self._info_button(help_index, help_index - 1))
            prompt_layout.addLayout(header)
        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlainText(current_prompt)
        datasheet_layout.addWidget(self.editor)
        self.grounding_editor = QTextEdit()
        self.grounding_editor.setAcceptRichText(False)
        self.grounding_editor.setPlainText(current_grounding_prompt or GROUNDING_PROMPT)
        photo_layout.addWidget(self.grounding_editor)
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

    def _info_button(self, index: int, key_index=None) -> QPushButton:
        """Keep explanations out of the editor, with keyboard-accessible help.

        `index` is the tab (and the entry of `_help_texts`); `key_index` the
        translated help text ``help.settings.stage.N`` of a prompt tab, None
        for the «Modello» tab, whose text has its own key.
        """
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
        button.clicked.connect(lambda checked=False: self._show_info(index, key_index))
        self.info_buttons.append(button)
        return button

    def _show_info(self, index: int, key_index=None) -> None:
        dialog = QDialog(self)
        prefix = self.i18n.text("Informazioni") if self.i18n is not None else "Informazioni"
        dialog.setWindowTitle(f"{prefix} — {self.tabs.tabText(index)}")
        dialog.setModal(True)
        dialog.resize(640, 360)
        layout = QVBoxLayout(dialog)
        explanation = QTextEdit()
        explanation.setReadOnly(True)
        explanation.setPlainText(
            self.i18n.tr(f"help.settings.stage.{key_index}" if key_index is not None
                         else "help.settings.model", default=self._help_texts[index])
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

    def source(self) -> str:
        """@brief The source chosen in the «Modello» tab (providers.PROVIDERS)."""
        return self.combo_source.currentData() or PROVIDERS[0]

    def _source_changed(self, _index) -> None:
        """Another source: a model of the other one cannot stay, propose the source's default."""
        if provider_of(self._model) != self.source():
            self._model = SOURCE_DEFAULT_MODEL.get(self.source(), DEFAULT_MODEL)
            self.field_model.setText(self._model)
        self._refresh_key_state()

    def _refresh_key_state(self) -> None:
        """Say whether the chosen source has a key, and where it comes from (never the key)."""
        try:
            if self.source() == PROVIDER_DEEPSEEK:
                from ..services.providers import deepseek_key_source
                kind = deepseek_key_source().kind
            else:
                from ..services.openrouter import api_key_source
                kind = api_key_source().kind
        except Exception:  # noqa: BLE001 - uno stato illeggibile non blocca la finestra
            kind = "none"
        source = tx(PROVIDER_LABELS.get(self.source(), self.source()))
        text = ("Chiave {source} presente." if kind != "none" else
                "Chiave {source} mancante: inseriscila da Configurazione → «Account AI...».")
        text = (self.i18n.tr("ui:" + text, default=text, source=source) if self.i18n is not None
                else text.format(source=source))
        self.label_key.setText(text)

    def _pick_model(self) -> None:
        chosen = pick_model(self._model, self, title="Modello", provider=self.source())
        if chosen:
            self._model = chosen
            self.field_model.setText(chosen)
            # Il selettore mostra solo la sorgente scelta; se mai tornasse un
            # modello dell'altra, la sorgente lo segue.
            index = self.combo_source.findData(provider_of(chosen))
            if index >= 0 and index != self.combo_source.currentIndex():
                blocked = self.combo_source.blockSignals(True)
                self.combo_source.setCurrentIndex(index)
                self.combo_source.blockSignals(blocked)
                self._refresh_key_state()

    def _restore_defaults(self) -> None:
        index = self.tabs.currentIndex()
        if index == MODEL_TAB:
            self._model = DEFAULT_MODEL
            self.field_model.setText(DEFAULT_MODEL)
            blocked = self.combo_source.blockSignals(True)
            self.combo_source.setCurrentIndex(
                max(0, self.combo_source.findData(provider_of(DEFAULT_MODEL))))
            self.combo_source.blockSignals(blocked)
            self._refresh_key_state()
        elif index == 1:
            self.editor.setPlainText(DEFAULT_SYSTEM_PROMPT)
        elif index == 2:
            self.grounding_editor.setPlainText(GROUNDING_PROMPT)
        else:
            self.verification_editor.setPlainText(VERIFICATION_PROMPT)

    def verification_model(self):
        """@brief The single model (kept for callers that save the default per stage)."""
        return self.model()

    def grounding_prompt(self):
        """@brief Return the edited grounding prompt, or the built-in one when left empty."""
        return self.grounding_editor.toPlainText().strip() or GROUNDING_PROMPT

    def verification_prompt(self):
        """@brief Return the edited verification prompt, or the built-in one when left empty."""
        return self.verification_editor.toPlainText().strip() or VERIFICATION_PROMPT

    def vision_model(self) -> str:
        """@brief The single model (kept for callers that save the default per stage)."""
        return self.model()

    def prompt(self) -> str:
        """@brief Return the edited extraction system prompt, stripped."""
        return self.editor.toPlainText().strip()

    def model(self) -> str:
        """@brief Return the single chosen model, or DEFAULT_MODEL when empty."""
        return self._model.strip() or DEFAULT_MODEL


#: Nome storico della classe, prima che il dialogo ospitasse anche il modello.
SystemPromptDialog = LlmSettingsDialog
