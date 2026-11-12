"""@brief «Prompt inviati»: from the number in the analysis log back to the exact prompt text.

@details User request (2026-11-12): the progress card and the analysis log
name each prompt sent by a registry number («n. 17») instead of a digest.
This window takes that number and shows the title, the status the text had
when first sent, the date of that first send and the whole text, read from the registry
file (`kicadgen.services.sent_prompts`).
"""
from __future__ import annotations

from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
                             QSpinBox, QVBoxLayout, QWidget)


class SentPromptsDialog(QDialog):
    """@brief Modal window: a number, and the prompt registered under it."""

    def __init__(self, translate, parent: QWidget | None = None, number: int = 1, path=None):
        """@brief Build the window and show the prompt of `number`.

        @param translate Callable mapping an Italian source text to the
            interface language.
        @param parent Owner window; the dialog is centred on it.
        @param number First number shown.
        @param path Registry file; None uses the program's
            (`sent_prompts.registry_path`).
        """
        super().__init__(parent)
        self._path = path
        self._translate = translate
        self.setObjectName("sentPromptsDialog")
        self.setWindowTitle(translate("Prompt inviati"))
        self.setModal(True)
        self.resize(720, 520)
        # Texts are composed here in the interface language; the window's
        # i18n pass must not read them back as catalogue sources.
        self.setProperty("i18n_skip", True)
        column = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel(translate("Numero del prompt (n. nel log dell'analisi):")))
        self.number = QSpinBox()
        self.number.setRange(1, 10 ** 9)
        self.number.setValue(max(1, int(number)))
        row.addWidget(self.number)
        row.addStretch(1)
        column.addLayout(row)
        self.summary = QLabel()
        self.summary.setObjectName("sentPromptSummary")
        self.summary.setWordWrap(True)
        column.addWidget(self.summary)
        self.text = QPlainTextEdit()
        self.text.setObjectName("sentPromptText")
        self.text.setReadOnly(True)
        column.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(translate("Chiudi"))
        column.addWidget(buttons)
        self.number.valueChanged.connect(self.show_number)
        self.show_number(self.number.value())

    def show_number(self, number: int) -> bool:
        """@brief Show the prompt registered under `number`.

        @param number Registry number.
        @return True when a prompt has that number; False shows a line
            saying so and clears the text.
        """
        import sqlite3
        from ..services import prompt_catalog, sent_prompts
        try:
            row = sent_prompts.lookup(number, self._path)
        except (sqlite3.Error, OSError) as exc:
            # A slot must never raise (PyQt6 aborts): the reason is shown.
            self.summary.setText(str(exc))
            self.summary.setToolTip("")
            self.text.clear()
            return False
        if row is None:
            self.summary.setText(self._translate("Nessun prompt registrato con questo numero."))
            self.summary.setToolTip("")
            self.text.clear()
            return False
        try:
            title = self._translate(prompt_catalog.entry(row["field"]).title)
        except KeyError:
            # A field a later version renamed or removed: its key still names it.
            title = row["field"]
        try:
            status = self._translate(prompt_catalog.log_status_label(row["status"]))
        except KeyError:
            status = row["status"]
        self.summary.setText(self._translate(
            "n. {number} · «{title}» · {status} quando è stato inviato · primo invio {date}").format(
                number=row["number"], title=title, status=status, date=row["first_sent"]))
        # The full digest only on demand: it matches the AI_PROMPT_SENT line
        # of an old analysis log if the registry file was replaced since.
        self.summary.setToolTip(f"sha256 {row['sha256']}")
        self.text.setPlainText(row["text"])
        return True
