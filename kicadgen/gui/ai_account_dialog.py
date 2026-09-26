"""@brief «Account AI»: paste, verify, save and remove the OpenRouter API key.

PIANO-FIX item 2: a freshly installed user must reach the first analysis
without editing any file. The key is pasted into a masked field, checked
against OpenRouter's two free status endpoints (no model call, no cost) and
saved in the Windows Credential Manager (`services.credentials`). A line
tells where the key in use comes from (environment variable, Credential
Manager, `.env` file or none) without ever showing it.

@note The key never reaches the log, a message box or a tooltip. It lives in
    the password field until it is saved, then the field is cleared.
@note Saving requires a successful verification of exactly the text in the
    field. When OpenRouter cannot judge the key (network down, timeout,
    server error) saving is still possible, after an explicit confirmation;
    a key that OpenRouter refused (HTTP 401/403) cannot be saved.
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6 import sip
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ..services import credentials, openrouter
from ..services.messages import tx


class _KeyCheckWorker(QThread):
    """Checks one key off the GUI thread.

    The check makes up to two HTTP requests of `openrouter._HTTP_TIMEOUT_CREDITS_S`
    each, and urllib cannot abort one in flight: closing the window detaches
    the worker instead of stopping it. Like `model_dialog._ModelsWorker`, the
    thread belongs to QApplication, so a closed window cannot destroy it while
    it runs; it deletes itself when done and application exit waits for it.
    """

    checked = pyqtSignal(object, object)

    def __init__(self, key: str, checker: Callable, parent=None):
        super().__init__(parent)
        self._key = key
        self._checker = checker
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.wait)
        self.finished.connect(self._release)

    def run(self) -> None:
        """@brief QThread override: run the checker and emit (status, None) or (None, error)."""
        key, self._key = self._key, ""
        try:
            status = self._checker(key)
        except Exception as exc:  # noqa: BLE001 - shown in the dialog, never fatal
            # Without its traceback the error keeps no frame that holds the key.
            self.checked.emit(None, exc.with_traceback(None))
            return
        self.checked.emit(status, None)

    def _release(self) -> None:
        """Drop the exit wait and delete itself once finished (see model_dialog)."""
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self.wait)
            except TypeError:
                pass
        self.deleteLater()


class AiAccountDialog(QDialog):
    """@brief Modal window to paste, verify, save and remove the OpenRouter key.

    @note Emits `key_changed` after a key was saved or removed, so the caller
        can read the balance again; the credit cache of `openrouter` is
        already invalidated at that point.
    """

    #: A key was saved in, or removed from, the Credential Manager.
    key_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None, *,
                 checker: Optional[Callable] = None,
                 source: Optional[Callable] = None):
        """@brief Build the window and show where the key in use comes from.

        @param parent Parent window; its `i18n` translates the texts.
        @param checker Function key -> CreditStatus that raises OpenRouterError;
            `openrouter.verify_api_key` by default (tests pass a fake).
        @param source Function returning an `openrouter.ApiKeySource`;
            `openrouter.api_key_source` by default.
        """
        super().__init__(parent)
        self.i18n = getattr(parent, "i18n", None)
        self._checker = checker or openrouter.verify_api_key
        self._source = source or openrouter.api_key_source
        self._worker: Optional[_KeyCheckWorker] = None
        self._closed = False
        #: Text of the field that OpenRouter accepted, or could not judge.
        self._verified_key: Optional[str] = None
        self._unverified_key: Optional[str] = None
        self._checking_key: Optional[str] = None
        #: Whether the Credential Manager holds a key (see refresh_source).
        self._stored = False

        self.setWindowTitle(self._tr("Account AI"))
        self.setModal(True)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        intro = QLabel(self._tr(
            "La chiave API di OpenRouter serve per le analisi con l'AI. Creala su "
            "https://openrouter.ai/keys, incollala qui e premi «Verifica»: la verifica "
            "legge credito e limiti e non consuma credito. «Salva» la conserva nel "
            "Gestore credenziali di Windows del tuo utente, non in un file."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.label_source = QLabel()
        self.label_source.setObjectName("ai_key_source")
        self.label_source.setWordWrap(True)
        self.label_source.setToolTip(self._tr(
            "Da dove arriva la chiave usata adesso dalle analisi. La chiave non viene "
            "mai mostrata."))
        layout.addWidget(self.label_source)

        form = QFormLayout()
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.field_key = QLineEdit()
        self.field_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.field_key.setPlaceholderText(self._tr("Incolla qui la chiave OpenRouter"))
        self.field_key.setToolTip(self._tr(
            "Chiave API di OpenRouter. Resta nascosta finché non premi «Mostra»."))
        self.field_key.textChanged.connect(self._on_key_edited)
        row_layout.addWidget(self.field_key, 1)
        self.btn_show = QToolButton()
        self.btn_show.setCheckable(True)
        self.btn_show.setText(self._tr("Mostra"))
        self.btn_show.setToolTip(self._tr("Mostra o nasconde i caratteri della chiave."))
        self.btn_show.toggled.connect(self._on_show_toggled)
        row_layout.addWidget(self.btn_show)
        form.addRow(self._tr("Chiave API:"), row)
        layout.addLayout(form)

        self.label_result = QLabel()
        self.label_result.setObjectName("ai_key_result")
        self.label_result.setWordWrap(True)
        self.label_result.setToolTip(self._tr("Esito dell'ultima verifica o operazione."))
        layout.addWidget(self.label_result)

        actions = QHBoxLayout()
        self.btn_verify = QPushButton(self._tr("Verifica"))
        self.btn_verify.setToolTip(self._tr(
            "Controlla la chiave con OpenRouter leggendo credito e limiti: nessun modello "
            "viene chiamato e non si consuma credito."))
        self.btn_verify.clicked.connect(self.start_check)
        self.btn_save = QPushButton(self._tr("Salva"))
        self.btn_save.setToolTip(self._tr(
            "Salva la chiave nel Gestore credenziali di Windows. Si attiva dopo una "
            "verifica riuscita, oppure con conferma se OpenRouter non era raggiungibile."))
        self.btn_save.clicked.connect(self.save_key)
        self.btn_remove = QPushButton(self._tr("Rimuovi chiave"))
        self.btn_remove.setToolTip(self._tr(
            "Cancella la chiave salvata nel Gestore credenziali di Windows. Non tocca "
            "il file .env né le variabili d'ambiente."))
        self.btn_remove.clicked.connect(self.remove_key)
        for button in (self.btn_verify, self.btn_save, self.btn_remove):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setText(self._tr("Chiudi"))
        close.setToolTip(self._tr(
            "Chiude la finestra. Una verifica in corso viene abbandonata e il suo esito "
            "ignorato."))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh_source()
        self._update_buttons()

    # ── Texts ──────────────────────────────────────────────────────────

    def _tr(self, source: str) -> str:
        """Text in the interface language, or the Italian source."""
        return self.i18n.text(source) if self.i18n is not None else source

    def refresh_source(self) -> None:
        """@brief Say where the key in use comes from, never the key itself.

        @note Reads the same sources as `openrouter.api_key`, in the same
            order; no network. Also decides whether «Rimuovi chiave» applies.
        """
        try:
            source = self._source()
        except Exception as exc:  # noqa: BLE001 - a broken probe must not close the window
            source = openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE,
                                             credential_error=str(exc))
        self._stored = bool(source.stored)
        kind = source.kind
        if kind == openrouter.KEY_SOURCE_ENVIRONMENT:
            lines = [tx("Chiave in uso: variabile d'ambiente {name}.", name=source.location)]
            if source.stored:
                lines.append(self._tr(
                    "La chiave salvata nel Gestore credenziali resta inutilizzata finché la "
                    "variabile d'ambiente è impostata."))
        elif kind == openrouter.KEY_SOURCE_CREDENTIAL_MANAGER:
            lines = [self._tr("Chiave in uso: Gestore credenziali di Windows.")]
        elif kind == openrouter.KEY_SOURCE_DOTENV:
            lines = [tx("Chiave in uso: file {path}.", path=source.location),
                     self._tr("Una chiave salvata qui avrà la precedenza sul file.")]
        else:
            lines = [self._tr("Nessuna chiave configurata: le analisi con l'AI non partono.")]
        if source.credential_error:
            lines.append(tx("Gestore credenziali di Windows non leggibile: {error}",
                            error=source.credential_error))
        if not credentials.is_supported():
            lines.append(self._tr(
                "Il Gestore credenziali di Windows non è disponibile su questo sistema: "
                "usa la variabile d'ambiente o il file .env."))
        self.label_source.setText("\n".join(lines))
        self._update_buttons()

    def _set_result(self, text: str) -> None:
        self.label_result.setText(text)

    # ── State ──────────────────────────────────────────────────────────

    def _key(self) -> str:
        return self.field_key.text().strip()

    def _checking(self) -> bool:
        return self._worker is not None

    def _update_buttons(self) -> None:
        """Enable each action only when it can do something sensible now."""
        supported = credentials.is_supported()
        key = self._key()
        checking = self._checking()
        self.btn_verify.setEnabled(bool(key) and not checking)
        self.btn_save.setEnabled(supported and bool(key) and not checking
                                 and key in (self._verified_key, self._unverified_key))
        self.btn_remove.setEnabled(supported and not checking and self._stored)

    def _on_key_edited(self, _text: str) -> None:
        """A different key voids the previous verification."""
        if self._key() not in (self._verified_key, self._unverified_key):
            self._verified_key = self._unverified_key = None
            if not self._checking():
                self._set_result("")
        self._update_buttons()

    def _on_show_toggled(self, shown: bool) -> None:
        self.field_key.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                   else QLineEdit.EchoMode.Password)
        self.btn_show.setText(self._tr("Nascondi") if shown else self._tr("Mostra"))

    # ── Verification ───────────────────────────────────────────────────

    def start_check(self) -> None:
        """@brief Verify the key in the field in the background (free: no model call).

        @note The result updates the window only if it is still open and the
            field still holds the key that was checked.
        """
        key = self._key()
        if self._closed or self._checking():
            return
        if not key:
            self._set_result(self._tr("Incolla prima la chiave."))
            return
        self._verified_key = self._unverified_key = None
        self._checking_key = key
        self._set_result(self._tr("Verifica in corso…"))
        # Owned by QApplication, not by this window: see _KeyCheckWorker.
        worker = _KeyCheckWorker(key, self._checker, parent=QApplication.instance())
        worker.checked.connect(self._on_checked)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._update_buttons()
        worker.start()

    def _alive(self) -> bool:
        return not self._closed and not sip.isdeleted(self)

    def _on_checked(self, status, error) -> None:
        """Show the credit, or why the key could not be verified."""
        if not self._alive():
            return
        key, self._checking_key = self._checking_key, None
        if key != self._key():
            # The field changed while the check ran: its outcome is about a
            # key no longer on screen, so it neither shows nor enables Save.
            self._set_result("")
            self._update_buttons()
            return
        if error is None:
            self._verified_key = key
            account_error = getattr(status, "account_error", "")
            if account_error and status.remaining is None:
                # «No limit reported» would be wrong: the balance is unknown.
                text = self._tr("Chiave valida.") + " " + tx("Saldo non letto: {error}",
                                                             error=account_error)
            else:
                text = self._tr("Chiave valida.") + " " + status.describe()
        elif isinstance(error, openrouter.OpenRouterError) and openrouter.key_rejected(error):
            text = tx("Chiave rifiutata da OpenRouter: {error}", error=error)
        elif isinstance(error, openrouter.OpenRouterError):
            # Empty key or not judged (network, timeout, server): not a refusal.
            if key:
                self._unverified_key = key
            text = tx("Chiave non verificabile adesso: {error}. Puoi salvarla comunque, "
                      "dopo una conferma.", error=error)
        else:
            text = tx("Errore inatteso: {exc}", exc=error)
        self._set_result(text)
        self._update_buttons()

    def _on_worker_finished(self) -> None:
        self._worker = None
        if self._alive():
            self._update_buttons()

    def _detach_worker(self) -> None:
        """Cut the running check off this window without stopping it."""
        worker, self._worker = self._worker, None
        self._checking_key = None
        if worker is None or sip.isdeleted(worker):
            return
        for signal, slot in ((worker.checked, self._on_checked),
                             (worker.finished, self._on_worker_finished)):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

    # ── Save and remove ────────────────────────────────────────────────

    def save_key(self) -> bool:
        """@brief Save the verified key in the Windows Credential Manager.

        @return True when the key was saved.
        @note Without a successful verification the key is saved only if
            OpenRouter could not judge it and the user confirms. On success
            the field is cleared, the credit cache is invalidated and
            `key_changed` is emitted.
        """
        key = self._key()
        if not key or self._checking() or key not in (self._verified_key, self._unverified_key):
            return False
        if key != self._verified_key:
            answer = QMessageBox.question(
                self, self._tr("Account AI"),
                self._tr("OpenRouter non ha potuto verificare la chiave. Salvarla comunque "
                         "nel Gestore credenziali di Windows?"))
            if answer != QMessageBox.StandardButton.Yes:
                return False
        try:
            credentials.write_secret(credentials.OPENROUTER_TARGET, key)
        except credentials.CredentialError as exc:
            self._set_result(str(exc))
            return False
        self._verified_key = self._unverified_key = None
        self.field_key.clear()
        self._key_changed(self._tr("Chiave salvata nel Gestore credenziali di Windows."))
        return True

    def remove_key(self) -> bool:
        """@brief Delete the key saved in the Windows Credential Manager, after confirmation.

        @return True when a key was deleted.
        @note The `.env` file and the environment variables are left alone.
        """
        if self._checking():
            return False
        answer = QMessageBox.question(
            self, self._tr("Account AI"),
            self._tr("Rimuovere la chiave OpenRouter dal Gestore credenziali di Windows?"))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            deleted = credentials.delete_secret(credentials.OPENROUTER_TARGET)
        except credentials.CredentialError as exc:
            self._set_result(str(exc))
            return False
        self._key_changed(self._tr("Chiave rimossa dal Gestore credenziali di Windows.")
                          if deleted else self._tr("Nessuna chiave salvata da rimuovere."))
        return deleted

    def _key_changed(self, message: str) -> None:
        openrouter.invalidate_credit_cache()
        self.refresh_source()
        self._set_result(message)
        self.key_changed.emit()

    # ── Closing ────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """@brief Abandon a running check even when the window was never shown.

        @param event Qt close event.
        """
        self._closed = True
        self._detach_worker()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        """@brief Close at once; a running check finishes on its own and is ignored.

        @param result Qt dialog result.
        """
        self._closed = True
        self._detach_worker()
        self.field_key.clear()
        super().done(result)
