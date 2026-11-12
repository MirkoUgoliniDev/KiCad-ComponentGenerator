"""@brief «Account AI»: the API keys of every AI provider, with their balances.

PIANO-FIX item 2: a freshly installed user must reach the first analysis
without editing any file. With a second provider (DeepSeek's direct API,
HANDOFF 121) the window lists every provider on one row: where its key in use
comes from (Windows Credential Manager, `.env` file, environment variable or
missing) and its current balance, read in the background with the providers'
free status endpoints (user, 2026-09-27: «elencare le varie chiavi presenti e
la possibilità di editarle; mostrerei di lato anche il balance corrente»).

Layout approved by the user on 2026-11-12, after «troppo confusionaria» (too
confusing): one intro line, the table «Servizio | Chiave | Saldo», then one
titled panel for the selected service with, in this order, the warning about
a key read from a `.env` file and its «Spostala in Windows» button, the
«Nuova chiave» field with ONE «Verifica e salva» button, the link where a key
is created, «Rimuovi» when a key is saved in Windows, and the result line.

@note The key never reaches the log, a message box, a tooltip or the list.
    It lives in the password field until it is saved, then the field is
    cleared.
@note «Verifica e salva» saves only what the provider accepted. When the
    provider cannot judge the key (network down, timeout, server error) the
    key is saved only after an explicit confirmation; a key that the provider
    refused, or answered about without a verdict (HTTP 400/404/429), is not
    saved. With the field empty the button checks the key already in use
    (user, 2026-09-27) and saves nothing.
@note «Rimuovi» only deletes the Credential Manager entry: keys in the
    environment or in a `.env` file are never touched, the row says where
    they are.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from PyQt6 import sip
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..services import credentials, dotenv_keys, openrouter, providers
from ..services.messages import tx

#: Folder of the window icons.
_ASSETS = Path(__file__).parent / "assets"
#: Keyboard shortcut of the eye that shows or masks the key (the Windows
#: sign-in screen uses the same key to reveal a password).
_SHOW_KEY_SHORTCUT = "Alt+F8"


def _ICON_EYE() -> QIcon:  # noqa: N802 - reads like a constant at the call site
    """The open eye: a click shows the key."""
    return QIcon(str(_ASSETS / "eye.svg"))


def _ICON_EYE_OFF() -> QIcon:  # noqa: N802 - reads like a constant at the call site
    """The crossed eye: a click masks the key again."""
    return QIcon(str(_ASSETS / "eye-off.svg"))


class _KeyCheckWorker(QThread):
    """Runs one provider request (key check or balance) off the GUI thread.

    The request makes up to two HTTP calls, and urllib cannot abort one in
    flight: closing the window detaches the worker instead of stopping it.
    Like `model_dialog._ModelsWorker`, the thread belongs to QApplication, so a
    closed window cannot destroy it while it runs; it deletes itself when
    done and application exit waits for it.
    """

    checked = pyqtSignal(object, object)

    def __init__(self, key: str, checker: Callable, parent=None):
        """@brief Prepare one key check (or balance read) owned by QApplication.

        @param key API key passed to `checker`; forgotten by the worker as
            soon as the request starts, so no finished thread holds it.
        @param checker Callable(key) -> status; its exception is reported
            through `checked` with the tracebacks removed (see `run`).
        @param parent Owner of the thread; the dialog passes
            QApplication.instance() (see the class docstring).
        """
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
            # Without its tracebacks (its own and those of the errors it was
            # raised from) the error keeps no frame that holds the key.
            inner = exc
            while inner is not None:
                inner.__traceback__ = None
                inner = inner.__cause__ or inner.__context__
            self.checked.emit(None, exc)
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


@dataclass(frozen=True)
class ProviderAccount:
    """@brief How «Account AI» reaches one provider's key and balance.

    @note `checker(key)` verifies a pasted key and returns something with
        `describe()`; `source()` returns an openrouter.ApiKeySource;
        `balance()` reads the balance of the key in use; `rejected(error)`
        tells an authentication refusal from a failure to answer.
    """

    provider: str
    keys_url: str
    target: str
    checker: Callable
    source: Callable
    balance: Callable
    rejected: Callable


#: The variable each provider's key has in a `.env` file.
_KEY_ENV = {providers.PROVIDER_OPENROUTER: openrouter.API_KEY_ENV,
            providers.PROVIDER_DEEPSEEK: providers.DEEPSEEK_KEY_ENV}


def default_accounts() -> list:
    """@brief The providers «Account AI» lists, in the pickers' order.

    @return One ProviderAccount per provider, wired to the real services.
    """
    return [
        ProviderAccount(providers.PROVIDER_OPENROUTER, "https://openrouter.ai/keys",
                        credentials.OPENROUTER_TARGET, openrouter.verify_api_key,
                        openrouter.api_key_source,
                        lambda: openrouter.credit_status(force_refresh=True),
                        openrouter.key_rejected),
        ProviderAccount(providers.PROVIDER_DEEPSEEK, "https://platform.deepseek.com/api_keys",
                        credentials.DEEPSEEK_TARGET, providers.verify_deepseek_key,
                        providers.deepseek_key_source, providers.deepseek_balance,
                        providers.deepseek_key_rejected),
    ]


def balance_text(status) -> str:
    """@brief Short balance text for the list: amount and currency, or the state.

    @param status openrouter.CreditStatus or providers.ProviderBalance.
    @return E.g. «12.50 USD»; «nessun limite» for an OpenRouter key without a
        limit; «esaurito» for an exhausted DeepSeek account.
    """
    amount = getattr(status, "amount", None)
    if isinstance(status, providers.ProviderBalance):
        if amount is None:
            return tx("disponibile") if status.available else tx("esaurito")
        return f"{amount:.2f} {status.currency}"
    remaining = getattr(status, "remaining", None)
    if remaining is not None:
        return f"{remaining:.2f} USD"
    if getattr(status, "account_error", ""):
        return tx("non disponibile")
    return tx("nessun limite")


def _not_judged(error) -> bool:
    """True when the provider gave no verdict on the key: no answer, or a server error."""
    status = getattr(error, "http_status", None) or getattr(error, "status", None)
    return bool(getattr(error, "unreachable", False)) or (isinstance(status, int)
                                                           and status >= 500)


#: Columns of the provider list.
_COLUMN_PROVIDER, _COLUMN_SOURCE, _COLUMN_BALANCE = range(3)


class AiAccountDialog(QDialog):
    """@brief Modal window listing the AI providers' keys and balances, with an editor.

    @note Emits `key_changed` after a key was saved or removed, so the caller
        can read the balance again; the credit cache of `openrouter` is
        already invalidated at that point.
    """

    #: A key was saved in, or removed from, the Credential Manager.
    key_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None, *,
                 checker: Optional[Callable] = None,
                 source: Optional[Callable] = None,
                 accounts: Optional[list] = None,
                 read_balances: bool = False):
        """@brief Build the list of providers and the panel of the selected one.

        @param parent Parent window; its `i18n` translates the texts.
        @param checker OpenRouter key checker (key -> CreditStatus, raises
            OpenRouterError); kept for the callers written before the list:
            it replaces the checker of the OpenRouter row.
        @param source OpenRouter source probe; replaces that of the OpenRouter row.
        @param accounts Providers to list; `default_accounts()` when None.
        @param read_balances Read each configured provider's balance in the
            background when the window opens and after a save or removal.
            The application passes True; tests leave it off (no network).
        """
        super().__init__(parent)
        self.i18n = getattr(parent, "i18n", None)
        rows = list(accounts) if accounts is not None else default_accounts()
        if checker is not None or source is not None:
            rows = [ProviderAccount(a.provider, a.keys_url, a.target,
                                    checker or a.checker, source or a.source, a.balance,
                                    a.rejected)
                    if a.provider == providers.PROVIDER_OPENROUTER else a for a in rows]
        self._accounts = rows
        self._account = rows[0]
        self._read_balances = read_balances
        self._worker: Optional[_KeyCheckWorker] = None
        #: Running balance reads: worker -> (provider, generation). A result
        #: whose generation is older than the provider's latest request is
        #: ignored: after a save or «Rimuovi» a late read of the previous key
        #: must not overwrite the row.
        self._balance_workers = {}
        self._balance_generation = {}
        self._closed = False
        #: Text of the field that the provider accepted, or could not judge.
        self._verified_key: Optional[str] = None
        self._unverified_key: Optional[str] = None
        self._checking_key: Optional[str] = None
        #: The running check of a pasted key was started by «Verifica e
        #: salva»: its outcome saves the key (after a confirmation when the
        #: provider could not judge it).
        self._save_requested = False
        #: Whether the Credential Manager holds a key for the selected provider.
        self._stored = False
        #: Whether the selected provider has a key in use (any source).
        self._in_use = False
        #: The running check is of the key in use, not of a pasted one.
        self._checking_in_use = False
        #: The running check is the key read from a .env file to move:
        #: (key, file, variable name), or None.
        self._moving: Optional[tuple] = None
        #: Source of the selected provider's key (openrouter.ApiKeySource), or None.
        self._source = None

        self.setWindowTitle(self._tr("Account AI"))
        self.setModal(True)
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        # Same air as the other reworked dialogs (model_dialog, settings_dialog).
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        intro = QLabel(self._tr(
            "Le chiavi dei servizi AI. Sono salvate in Windows, non nei file del progetto."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(rows), 3)
        self.table.setObjectName("ai_accounts")
        self.table.setHorizontalHeaderLabels([self._tr("Servizio"), self._tr("Chiave"),
                                              self._tr("Saldo")])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_COLUMN_PROVIDER, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COLUMN_SOURCE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COLUMN_BALANCE, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setToolTip(self._tr(
            "Un servizio per riga: dove si trova la sua chiave (mai mostrata) e il saldo "
            "attuale, letto gratis. Selezionane uno per gestirne la chiave."))
        layout.addWidget(self.table)

        # The panel of the selected service: its title is the service name.
        self.panel = QGroupBox()
        self.panel.setObjectName("ai_account_panel")
        panel = QVBoxLayout(self.panel)
        panel.setContentsMargins(14, 14, 14, 12)
        panel.setSpacing(10)

        # Where the key in use is, when that needs attention: a .env file
        # (with the one-click move, user 2026-11-12: «non puoi mettere un
        # pulsante per trasferire la key?»), an environment variable that wins
        # over a saved key, no key, or an unreadable Credential Manager.
        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        self.label_source = QLabel()
        self.label_source.setObjectName("ai_key_source")
        self.label_source.setTextFormat(Qt.TextFormat.PlainText)
        self.label_source.setWordWrap(True)
        source_row.addWidget(self.label_source, 1)
        self.btn_move = QPushButton(self._tr("Spostala in Windows"))
        self.btn_move.setObjectName("ai_key_move")
        self.btn_move.setToolTip(self._tr(
            "Legge la chiave dal file .env, la verifica (senza consumare credito), la salva "
            "nel Gestore credenziali di Windows e poi chiede se toglierla dal file."))
        self.btn_move.clicked.connect(self.move_key)
        source_row.addWidget(self.btn_move, 0, Qt.AlignmentFlag.AlignTop)
        panel.addLayout(source_row)

        key_row = QHBoxLayout()
        key_row.setSpacing(10)
        key_label = QLabel(self._tr("Nuova chiave:"))
        key_row.addWidget(key_label)
        self.field_key = QLineEdit()
        self.field_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.field_key.setToolTip(self._tr(
            "Chiave API del fornitore selezionato. Resta nascosta finché non premi l'occhio."))
        self.field_key.textChanged.connect(self._on_key_edited)
        self.field_key.setAccessibleName(self._tr("Nuova chiave:"))
        key_label.setBuddy(self.field_key)
        key_row.addWidget(self.field_key, 1)
        # The classic eye inside the field (user, 2026-11-12) instead of a
        # «Mostra» (show) button beside it: open eye = show, crossed eye = hide.
        self.action_show = self.field_key.addAction(
            _ICON_EYE(), QLineEdit.ActionPosition.TrailingPosition)
        self.action_show.setCheckable(True)
        # The eye is not reachable with Tab: Alt+F8 toggles it, the same key
        # that shows the password on the Windows sign-in screen.
        self.action_show.setShortcut(QKeySequence(_SHOW_KEY_SHORTCUT))
        self.action_show.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.field_key.setAccessibleDescription(self._tr(
            "Alt+F8 mostra o nasconde la chiave."))
        self.action_show.toggled.connect(self._on_show_toggled)
        self._on_show_toggled(False)
        # ONE button: verify, then save what the provider accepted.
        self.btn_save = QPushButton(self._tr("Verifica e salva"))
        self.btn_save.setObjectName("ai_key_save")
        self.btn_save.setToolTip(self._tr(
            "Verifica la chiave incollata leggendo il saldo (nessun modello chiamato, nessun "
            "credito consumato) e, se è valida, la salva nel Gestore credenziali di Windows. "
            "Se il servizio non risponde chiede conferma prima di salvarla. Con il campo "
            "vuoto verifica la chiave già in uso."))
        self.btn_save.clicked.connect(self.verify_and_save)
        key_row.addWidget(self.btn_save)
        panel.addLayout(key_row)

        link_row = QHBoxLayout()
        link_row.setSpacing(10)
        self.label_keys_link = QLabel()
        self.label_keys_link.setObjectName("ai_keys_link")
        self.label_keys_link.setTextFormat(Qt.TextFormat.RichText)
        self.label_keys_link.setOpenExternalLinks(True)
        link_row.addWidget(self.label_keys_link)
        # The stretch keeps «Rimuovi» on the right when the link is hidden.
        link_row.addStretch(1)
        self.btn_remove = QPushButton(self._tr("Rimuovi"))
        self.btn_remove.setObjectName("ai_key_remove")
        self.btn_remove.setToolTip(self._tr(
            "Cancella la chiave salvata nel Gestore credenziali di Windows. Non tocca "
            "il file .env né le variabili d'ambiente."))
        self.btn_remove.clicked.connect(self.remove_key)
        link_row.addWidget(self.btn_remove)
        panel.addLayout(link_row)

        result_row = QHBoxLayout()
        result_row.setSpacing(10)
        self.label_result = QLabel()
        self.label_result.setObjectName("ai_key_result")
        # Plain text: a provider's error message must never render as markup.
        self.label_result.setTextFormat(Qt.TextFormat.PlainText)
        self.label_result.setWordWrap(True)
        self.label_result.setToolTip(self._tr("Esito dell'ultima verifica o operazione."))
        result_row.addWidget(self.label_result, 1)
        # Activity indicator (user, 27/09:
        # «fai partire un loader per far capire che fai la chiamata»
        # (start a loader to show that you are making the call)):
        # indeterminate bar, visible only while a check or a balance
        # reading is in progress.
        self.busy = QProgressBar()
        self.busy.setObjectName("ai_account_busy")
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setFixedWidth(140)
        self.busy.setToolTip(self._tr("Chiamata al fornitore in corso (gratuita)."))
        self.busy.setVisible(False)
        result_row.addWidget(self.busy, 0, Qt.AlignmentFlag.AlignTop)
        panel.addLayout(result_row)
        layout.addWidget(self.panel)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setText(self._tr("Chiudi"))
        close.setToolTip(self._tr(
            "Chiude la finestra. Una verifica in corso viene abbandonata e il suo esito "
            "ignorato."))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for index, account in enumerate(rows):
            item = QTableWidgetItem(self._label(account.provider))
            self.table.setItem(index, _COLUMN_PROVIDER, item)
            self.table.setItem(index, _COLUMN_SOURCE, QTableWidgetItem(""))
            self.table.setItem(index, _COLUMN_BALANCE, QTableWidgetItem("—"))
        self.table.selectRow(0)
        self.table.itemSelectionChanged.connect(self._on_provider_selected)
        self._show_account()
        self._fit_table()
        self.refresh_balances()

    # ── Texts ──────────────────────────────────────────────────────────

    def _tr(self, source: str) -> str:
        """Text in the interface language, or the Italian source."""
        return self.i18n.text(source) if self.i18n is not None else source

    def _label(self, provider: str) -> str:
        """The provider's display name in the interface language."""
        return self._tr(providers.PROVIDER_LABELS.get(provider, provider))

    def _short(self) -> str:
        """The selected provider's short name, used inside sentences."""
        return providers.PROVIDER_SHORT_LABELS.get(self._account.provider,
                                                   self._account.provider)

    def _probe(self, account: ProviderAccount):
        """The source of `account`'s key; a broken probe reads as «no key», never raises."""
        try:
            return account.source()
        except Exception as exc:  # noqa: BLE001 - a broken probe must not close the window
            return openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE,
                                           credential_error=str(exc))

    def _short_source(self, source) -> tuple:
        """The «Chiave» cell for `source`: (text, tooltip), never the key.

        The cell stays short; the file path or the variable name, which can be
        long, goes in the tooltip.
        """
        kind = source.kind
        if kind == openrouter.KEY_SOURCE_ENVIRONMENT:
            return (self._tr("⚠ variabile d'ambiente"),
                    tx("Chiave letta dalla variabile d'ambiente {name}.", name=source.location))
        if kind == openrouter.KEY_SOURCE_CREDENTIAL_MANAGER:
            return (self._tr("✓ salvata in Windows"),
                    self._tr("Chiave salvata nel Gestore credenziali di Windows."))
        if kind == openrouter.KEY_SOURCE_DOTENV:
            return (self._tr("⚠ nel file .env"),
                    tx("Chiave letta dal file {path}.", path=source.location))
        return (self._tr("✗ mancante"),
                self._tr("Nessuna chiave configurata: le analisi con l'AI non partono."))

    def refresh_source(self) -> None:
        """@brief Say where each provider's key comes from, never the key itself.

        @note Reads the same sources as the providers' key lookups, in the
            same order; no network. Also decides which actions of the panel
            apply to the selected provider.
        """
        for index, account in enumerate(self._accounts):
            source = self._probe(account)
            item = self.table.item(index, _COLUMN_SOURCE)
            text, tip = self._short_source(source)
            item.setText(text)
            item.setToolTip(tip)
            if account is self._account:
                self._describe_source(source)
        self._update_buttons()

    def _describe_source(self, source) -> None:
        """The attention line of the selected provider: empty when nothing needs saying."""
        self._stored = bool(source.stored)
        self._in_use = source.kind != openrouter.KEY_SOURCE_NONE
        self._source = source
        kind = source.kind
        lines = []
        tip = ""
        if kind == openrouter.KEY_SOURCE_ENVIRONMENT:
            lines.append(tx("La variabile d'ambiente {name} ha la precedenza su una chiave "
                            "salvata qui.", name=source.location))
        elif kind == openrouter.KEY_SOURCE_DOTENV:
            lines.append(self._tr("⚠ La chiave è scritta in un file .env."))
            tip = tx("Chiave letta dal file {path}.", path=source.location)
        elif kind == openrouter.KEY_SOURCE_NONE:
            lines.append(self._tr("Nessuna chiave configurata: le analisi con l'AI non partono."))
        if source.credential_error:
            lines.append(tx("Gestore credenziali di Windows non leggibile: {error}",
                            error=source.credential_error))
        if not credentials.is_supported():
            lines.append(self._tr(
                "Il Gestore credenziali di Windows non è disponibile su questo sistema: "
                "usa la variabile d'ambiente o il file .env."))
        self.label_source.setText("\n".join(lines))
        self.label_source.setToolTip(tip)
        self.label_source.setVisible(bool(lines))
        # «Non hai una chiave? Creala su …» ("No key? Create one at …") only for a
        # provider with no key at all (user, 2026-11-12).
        self.label_keys_link.setVisible(kind == openrouter.KEY_SOURCE_NONE)

    def _show_account(self) -> None:
        """Point the panel at the selected provider."""
        account = self._account
        self.panel.setTitle(self._label(account.provider))
        host = urlparse(account.keys_url).netloc or account.keys_url
        link = f'<a href="{escape(account.keys_url, quote=True)}">{escape(host)}</a>'
        self.label_keys_link.setText(tx("Non hai una chiave? Creala su {link}", link=link))
        self.label_keys_link.setToolTip(account.keys_url)
        self.field_key.setPlaceholderText(tx("Incolla qui la chiave {provider}",
                                             provider=self._short()))
        self.refresh_source()

    def _set_result(self, text: str) -> None:
        """Write the result line (plain text)."""
        self.label_result.setText(text)

    def _fit_table(self) -> None:
        """Size the list to show every provider row, without a scroll bar."""
        self.table.resizeRowsToContents()
        height = (self.table.horizontalHeader().sizeHint().height()
                  + sum(self.table.rowHeight(i) for i in range(self.table.rowCount()))
                  + 2 * self.table.frameWidth())
        self.table.setFixedHeight(height)

    # ── Provider list ──────────────────────────────────────────────────

    def select_provider(self, provider: str) -> None:
        """@brief Select the row of `provider` (programmatic, e.g. from the model in use).

        @param provider providers.PROVIDER_* value.
        """
        for index, account in enumerate(self._accounts):
            if account.provider == provider:
                self.table.selectRow(index)
                return

    def _on_provider_selected(self) -> None:
        """Another provider: the pasted key and its verification do not carry over."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        account = self._accounts[rows[0].row()]
        if account is self._account:
            return
        self._detach_worker()
        self._account = account
        self._verified_key = self._unverified_key = None
        self.field_key.clear()
        self._set_result("")
        self._show_account()

    def refresh_balances(self) -> None:
        """@brief Read, in the background, the balance of every provider with a key.

        @note Free status endpoints, no model call. Nothing is read unless
            the window was built with read_balances=True.
        """
        if not self._read_balances or self._closed:
            return
        for index, account in enumerate(self._accounts):
            item = self.table.item(index, _COLUMN_BALANCE)
            generation = self._balance_generation.get(account.provider, 0) + 1
            self._balance_generation[account.provider] = generation
            if self._probe(account).kind == openrouter.KEY_SOURCE_NONE:
                item.setText("—")
                item.setToolTip(self._tr("Nessuna chiave: saldo non leggibile."))
                continue
            item.setText(self._tr("lettura…"))
            item.setToolTip("")
            reader = account.balance
            worker = _KeyCheckWorker("", lambda _key, reader=reader: reader(),
                                     parent=QApplication.instance())
            # A bound method of this window: the result is queued to the GUI
            # thread (a lambda would run in the worker thread and touch widgets).
            worker.checked.connect(self._on_balance)
            self._balance_workers[worker] = (account.provider, generation)
            worker.start()
        self._update_busy()

    def _on_balance(self, status, error) -> None:
        """Show one provider's balance, or why it could not be read."""
        provider, generation = self._balance_workers.pop(self.sender(), (None, None))
        if provider is None:
            return
        if self._alive():
            self._update_busy()
        if generation != self._balance_generation.get(provider):
            if provider == providers.PROVIDER_OPENROUTER:
                # The late read of the previous key may have refilled the
                # credit cache after it was invalidated: drop it again.
                openrouter.invalidate_credit_cache()
            return
        if not self._alive():
            return
        index = next(i for i, account in enumerate(self._accounts)
                     if account.provider == provider)
        item = self.table.item(index, _COLUMN_BALANCE)
        if error is None:
            item.setText(balance_text(status))
            item.setToolTip(status.describe() if hasattr(status, "describe") else item.text())
        else:
            item.setText(self._tr("non disponibile"))
            item.setToolTip(tx("Saldo non letto: {error}", error=error))

    # ── State ──────────────────────────────────────────────────────────

    def _key(self) -> str:
        """The pasted key, without surrounding blanks."""
        return self.field_key.text().strip()

    def _checking(self) -> bool:
        """Whether a key check (pasted, in use or moving) is running."""
        return self._worker is not None

    def _update_busy(self) -> None:
        """Show the activity bar while a verification or a balance read runs."""
        self.busy.setVisible(self._alive() and (self._checking() or bool(self._balance_workers)))

    def _update_buttons(self) -> None:
        """Enable or show each action only when it can do something sensible now."""
        self._update_busy()
        supported = credentials.is_supported()
        key = self._key()
        checking = self._checking()
        # A pasted key needs the Credential Manager to be saved; with the
        # field empty the button checks the key in use, if there is one.
        self.btn_save.setEnabled(not checking and ((bool(key) and supported)
                                                   or (not key and self._in_use)))
        self.btn_remove.setVisible(supported and self._stored)
        self.btn_remove.setEnabled(supported and not checking and self._stored)
        from_file = (self._source is not None
                     and self._source.kind == openrouter.KEY_SOURCE_DOTENV)
        self.btn_move.setVisible(supported and from_file)
        self.btn_move.setEnabled(not checking)

    def _on_key_edited(self, _text: str) -> None:
        """A different key voids the previous verification."""
        if self._key() not in (self._verified_key, self._unverified_key):
            self._verified_key = self._unverified_key = None
            if not self._checking():
                self._set_result("")
        self._update_buttons()

    def _on_show_toggled(self, shown: bool) -> None:
        """Show or mask the key; the eye icon and its tooltip say what a click will do."""
        self.field_key.setEchoMode(QLineEdit.EchoMode.Normal if shown
                                   else QLineEdit.EchoMode.Password)
        self.action_show.setIcon(_ICON_EYE_OFF() if shown else _ICON_EYE())
        text = self._tr("Nascondi la chiave") if shown else self._tr("Mostra la chiave")
        # The text names the action for screen readers; the tooltip adds the key.
        self.action_show.setText(text)
        shortcut = self.action_show.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        self.action_show.setToolTip(f"{text} ({shortcut})" if shortcut else text)

    # ── Verification ───────────────────────────────────────────────────

    def verify_and_save(self) -> None:
        """@brief «Verifica e salva»: verify the pasted key and save it if the provider accepts it.

        @note With the field empty the key already in use is checked and
            nothing is saved. A key that the provider could not judge is
            saved only after the same confirmation as `save_key`; a refused
            key is never saved and the reason stays in the result line.
        """
        self._start_check(save=bool(self._key()))

    def start_check(self) -> None:
        """@brief Verify a key in the background (free: no model call), without saving it.

        @note With a key in the field, that key is checked. With the field
            empty, the key already in use for the selected provider is
            checked by reading its balance (user, 2026-09-27). The result
            updates the window only if it is still open and, for a pasted
            key, the field still holds the key that was checked.
        """
        self._start_check(save=False)

    def _start_check(self, save: bool) -> None:
        """Start the check of the pasted key (saving it afterwards when `save`) or of the key in use."""
        key = self._key()
        if self._closed or self._checking():
            return
        if not key:
            if self._in_use:
                self._start_in_use_check()
            else:
                self._set_result(self._tr("Incolla prima la chiave."))
            return
        problem = providers.key_text_problem(key)
        if problem:
            # Never sent: a line break in a header makes the HTTP library
            # echo the whole key in its error text.
            self._set_result(problem)
            return
        self._verified_key = self._unverified_key = None
        self._checking_key = key
        self._save_requested = save
        self._set_result(self._tr("Verifica in corso…"))
        # Owned by QApplication, not by this window: see _KeyCheckWorker.
        worker = _KeyCheckWorker(key, self._account.checker, parent=QApplication.instance())
        worker.checked.connect(self._on_checked)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._update_buttons()
        worker.start()

    def _start_in_use_check(self) -> None:
        """Check the key in use by reading its balance; never saves anything."""
        self._verified_key = self._unverified_key = None
        self._checking_key = None
        self._save_requested = False
        self._checking_in_use = True
        self._set_result(self._tr("Verifica della chiave in uso…"))
        reader = self._account.balance
        worker = _KeyCheckWorker("", lambda _key: reader(), parent=QApplication.instance())
        worker.checked.connect(self._on_checked)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._update_buttons()
        worker.start()

    def _on_in_use_checked(self, status, error) -> None:
        """Report on the key in use, and put its balance in the list."""
        provider = self._short()
        if error is None:
            index = self._accounts.index(self._account)
            item = self.table.item(index, _COLUMN_BALANCE)
            item.setText(balance_text(status))
            item.setToolTip(status.describe() if hasattr(status, "describe") else item.text())
            text = self._tr("Chiave in uso valida.") + " " + status.describe()
        elif isinstance(error, openrouter.OpenRouterError) and self._account.rejected(error):
            text = tx("Chiave in uso rifiutata da {provider}: {error}", provider=provider,
                      error=error)
        else:
            text = tx("Chiave in uso non verificabile adesso: {error}", error=error)
        self._set_result(text)
        self._update_buttons()

    def _alive(self) -> bool:
        """Whether the window is still open and its C++ object exists."""
        return not self._closed and not sip.isdeleted(self)

    def _on_checked(self, status, error) -> None:
        """Show the balance, or why the key could not be verified; save it when asked."""
        if not self._alive():
            return
        if self._checking_in_use:
            self._checking_in_use = False
            self._on_in_use_checked(status, error)
            return
        if self._moving is not None:
            moving, self._moving = self._moving, None
            self._finish_move(moving, status, error)
            return
        key, self._checking_key = self._checking_key, None
        save, self._save_requested = self._save_requested, False
        if key != self._key():
            # The field changed while the check ran: its outcome is about a
            # key no longer on screen, so it neither shows nor saves.
            self._set_result("")
            self._update_buttons()
            return
        provider = self._short()
        if error is None:
            self._verified_key = key
            account_error = getattr(status, "account_error", "")
            if account_error and getattr(status, "remaining", None) is None:
                # «No limit reported» would be wrong: the balance is unknown.
                text = self._tr("Chiave valida.") + " " + tx("Saldo non letto: {error}",
                                                             error=account_error)
            else:
                text = self._tr("Chiave valida.") + " " + status.describe()
        elif isinstance(error, openrouter.OpenRouterError) and self._account.rejected(error):
            text = tx("Chiave rifiutata da {provider}: {error}", provider=provider, error=error)
        elif isinstance(error, openrouter.OpenRouterError) and not _not_judged(error):
            # The provider answered with something else (HTTP 400, 404, 429…):
            # the key was neither accepted nor left unjudged, so it is not saved.
            text = tx("Chiave non verificata da {provider}: {error}", provider=provider,
                      error=error)
        elif isinstance(error, openrouter.OpenRouterError):
            # Not judged (network down, timeout, server error): not a refusal.
            self._unverified_key = key
            text = tx("Chiave non verificabile adesso: {error}.", error=error)
        else:
            text = tx("Errore inatteso: {exc}", exc=error)
        self._set_result(text)
        self._update_buttons()
        if not save:
            return
        if key not in (self._verified_key, self._unverified_key):
            self._set_result(text + " " + self._tr("Chiave non salvata."))
            return
        outcome = self._store_pasted(key, text + " " + self._tr("Salvata in Windows."))
        if outcome is None:
            # The user declined to save a key the provider could not judge.
            self._set_result(text + " " + self._tr("Chiave non salvata."))

    def _on_worker_finished(self) -> None:
        """The check thread ended: the buttons can act again."""
        self._worker = None
        if self._alive():
            self._update_buttons()

    def _detach_worker(self) -> None:
        """Cut the running check off this window without stopping it."""
        worker, self._worker = self._worker, None
        self._checking_key = None
        self._checking_in_use = False
        self._save_requested = False
        self._moving = None
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
        """@brief Save the verified key of the selected provider in the Credential Manager.

        @return True when the key was saved.
        @note Without a successful verification the key is saved only if the
            provider could not judge it and the user confirms. On success the
            field is cleared, the credit cache is invalidated, the balances
            are read again and `key_changed` is emitted. «Verifica e salva»
            reaches the same store through `verify_and_save`.
        """
        key = self._key()
        if not key or self._checking() or key not in (self._verified_key, self._unverified_key):
            return False
        return self._store_pasted(
            key, self._tr("Chiave salvata nel Gestore credenziali di Windows.")) is True

    def _store_pasted(self, key: str, message: str) -> Optional[bool]:
        """Write `key` to the Credential Manager, asking first when it was not judged.

        @return True when saved (the result line shows `message`), False when
            the store failed (the result line says why), None when the user
            declined, the system has no Credential Manager or the key cannot
            be a key.
        """
        if not credentials.is_supported() or providers.key_text_problem(key):
            return None
        if key != self._verified_key:
            answer = QMessageBox.question(
                self, self._tr("Account AI"),
                tx("{provider} non ha potuto verificare la chiave. Salvarla comunque nel "
                   "Gestore credenziali di Windows?", provider=self._short()))
            if answer != QMessageBox.StandardButton.Yes:
                return None
        try:
            credentials.write_secret(self._account.target, key)
        except credentials.CredentialError as exc:
            self._set_result(str(exc))
            return False
        self._verified_key = self._unverified_key = None
        self.field_key.clear()
        self._key_changed(message)
        return True

    def move_key(self) -> bool:
        """@brief Start moving the key of a .env file into the Credential Manager: verify it first.

        @return True when the verification started.
        @note The key is never shown: it is read from the file the key in
            use comes from, verified like a pasted key (balance, no credit),
            and saved by `_finish_move` only if the provider accepts it.
        """
        source = self._source
        if (self._closed or self._checking() or source is None
                or source.kind != openrouter.KEY_SOURCE_DOTENV or not credentials.is_supported()):
            return False
        name = _KEY_ENV.get(self._account.provider)
        key = dotenv_keys.read_key(source.location, name) if name else ""
        if not key or providers.key_text_problem(key):
            self._set_result(tx("Nessuna chiave {provider} leggibile nel file {path}.",
                                provider=self._short(), path=source.location))
            return False
        self._moving = (key, source.location, name)
        self._set_result(self._tr("Verifica della chiave del file .env…"))
        # Owned by QApplication, not by this window: see _KeyCheckWorker.
        worker = _KeyCheckWorker(key, self._account.checker, parent=QApplication.instance())
        worker.checked.connect(self._on_checked)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._update_buttons()
        worker.start()
        return True

    def _finish_move(self, moving, status, error) -> None:
        """Save the verified key of the .env file, then offer to remove it from the file."""
        key, path, name = moving
        provider = self._short()
        if error is not None:
            if isinstance(error, openrouter.OpenRouterError) and self._account.rejected(error):
                text = tx("Chiave del file rifiutata da {provider}: non spostata. {error}",
                          provider=provider, error=error)
            else:
                text = tx("Chiave del file non verificabile adesso: non spostata. {error}",
                          error=error)
            self._set_result(text)
            self._update_buttons()
            return
        try:
            credentials.write_secret(self._account.target, key)
        except credentials.CredentialError as exc:
            self._set_result(str(exc))
            self._update_buttons()
            return
        message = self._tr("Chiave spostata nel Gestore credenziali di Windows.")
        # An editor holding the file open would write the key back on its
        # next save: the question says so before anything is removed.
        answer = QMessageBox.question(
            self, self._tr("Account AI"),
            tx("Togliere la chiave anche dal file {path}? Verrà tolta solo la riga {name}; il "
               "resto del file resta com'è. Se il file è aperto in un editor, ricaricalo "
               "prima di salvarlo: altrimenti la chiave tornerebbe nel file.",
               path=path, name=name))
        if answer == QMessageBox.StandardButton.Yes:
            message += " " + self._removal_outcome(path, name)
        self._key_changed(message)

    def _removal_outcome(self, path: str, name: str) -> str:
        """Remove `name` from the .env file `path` and say exactly what happened.

        Success is claimed only when the line was really removed; the other
        `.env` files the program reads that still set `name` are named, since
        the key would keep coming from them.
        """
        try:
            outcome = dotenv_keys.remove_key_result(path, name)
        except OSError as exc:
            text = tx("Il file .env non è stato modificato: {error}", error=exc)
        else:
            if outcome == dotenv_keys.REMOVED:
                text = self._tr("Riga tolta dal file .env.")
            elif outcome == dotenv_keys.INDIRECT:
                text = tx("Nel file .env la riga {name} rimanda a un'altra variabile con "
                          "{syntax}: non è stato tolto nulla e la chiave resta in quella "
                          "variabile.", name=name, syntax="«${…}»")
            else:
                text = tx("Nessuna riga {name} trovata nel file .env: non è stato tolto nulla "
                          "e la chiave potrebbe essere ancora nel file.", name=name)
        others = dotenv_keys.other_files_setting(name, path)
        if others:
            text += " " + tx("La chiave è ancora in questi file .env: {paths}",
                             paths="; ".join(others))
        return text

    def remove_key(self) -> bool:
        """@brief Delete the selected provider's key from the Credential Manager, after confirmation.

        @return True when a key was deleted.
        @note The `.env` file and the environment variables are left alone.
        """
        if self._checking():
            return False
        answer = QMessageBox.question(
            self, self._tr("Account AI"),
            tx("Rimuovere la chiave {provider} dal Gestore credenziali di Windows?",
               provider=self._short()))
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            deleted = credentials.delete_secret(self._account.target)
        except credentials.CredentialError as exc:
            self._set_result(str(exc))
            return False
        self._key_changed(self._tr("Chiave rimossa dal Gestore credenziali di Windows.")
                          if deleted else self._tr("Nessuna chiave salvata da rimuovere."))
        return deleted

    def _key_changed(self, message: str) -> None:
        """A key was saved or removed: re-read sources and balances, report, notify."""
        openrouter.invalidate_credit_cache()
        self.refresh_source()
        self.refresh_balances()
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
