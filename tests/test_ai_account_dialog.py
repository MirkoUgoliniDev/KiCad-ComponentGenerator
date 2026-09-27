"""@brief «Account AI»: verification in the background, save only after it, source line.

Offscreen Qt, a fake checker instead of OpenRouter and the in-memory vault
that conftest installs instead of the Windows Credential Manager.
"""
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton, QToolButton

from kicadgen.gui.ai_account_dialog import AiAccountDialog
from kicadgen.services import credentials, openrouter

KEY = "sk-or-v1-incollata-dallutente"


@pytest.fixture
def app(qapp, monkeypatch):
    """@brief The session QApplication, on a platform that has the Credential Manager."""
    monkeypatch.setattr(sys, "platform", "win32")
    return qapp


def _status(**values):
    return openrouter.CreditStatus(**{"account_remaining": 12.5, "key_remaining": None,
                                      "total_credits": 20.0, "total_usage": 7.5, **values})


def _dialog(checker):
    return AiAccountDialog(None, checker=checker)


def _finish(dialog):
    """Wait for the background check and deliver its queued signals."""
    worker = dialog._worker
    if worker is not None:
        assert worker.wait(5000)
    for _ in range(5):
        QApplication.processEvents()


def _paste(dialog, text=KEY):
    dialog.field_key.setText(text)


def test_a_verified_key_can_be_saved_and_the_source_line_follows(app):
    """@brief Positivo: verifica riuscita -> credito mostrato -> Salva scrive nel Gestore credenziali."""
    seen = []
    dialog = _dialog(lambda key: seen.append((key, threading.current_thread()))
                     or _status())
    assert "Nessuna chiave configurata" in dialog.label_source.text()
    _paste(dialog)
    assert not dialog.btn_save.isEnabled()
    dialog.start_check()
    _finish(dialog)
    assert seen[0][0] == KEY
    assert seen[0][1] is not threading.main_thread(), "the check ran on the GUI thread"
    assert "Chiave valida" in dialog.label_result.text()
    assert "12.50" in dialog.label_result.text()
    assert dialog.btn_save.isEnabled()
    changed = []
    dialog.key_changed.connect(lambda: changed.append(True))
    assert dialog.save_key() is True
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    assert dialog.field_key.text() == ""
    assert changed == [True]
    assert "Gestore credenziali di Windows" in dialog.label_source.text()
    assert KEY not in dialog.label_source.text() + dialog.label_result.text()
    assert dialog.btn_remove.isEnabled()
    dialog.done(0)


def test_nothing_is_saved_without_a_verification(app):
    """@brief Negativo: senza verifica «Salva» e' spento e save_key non scrive nulla."""
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    assert not dialog.btn_save.isEnabled()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_editing_the_key_after_verification_voids_it(app):
    """@brief Negativo: si salva solo il testo verificato, non quello cambiato dopo."""
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert dialog.btn_save.isEnabled()
    _paste(dialog, KEY + "x")
    assert not dialog.btn_save.isEnabled()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_a_result_for_a_key_edited_during_the_check_is_dropped(app):
    """@brief Negativo: la chiave cambiata mentre la verifica girava non eredita l'esito."""
    release = threading.Event()

    def slow(key):
        release.wait(5)
        return _status()
    dialog = _dialog(slow)
    _paste(dialog)
    dialog.start_check()
    _paste(dialog, KEY + "altro")
    release.set()
    _finish(dialog)
    assert "Chiave valida" not in dialog.label_result.text()
    assert not dialog.btn_save.isEnabled()
    assert dialog.save_key() is False
    dialog.done(0)


def test_a_refused_key_cannot_be_saved(app):
    """@brief Negativo: HTTP 401 -> «rifiutata», e nessun salvataggio nemmeno con conferma."""
    def refuse(key):
        raise openrouter.OpenRouterError("HTTP 401 unauthorized", http_status=401)
    dialog = _dialog(refuse)
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert "rifiutata" in dialog.label_result.text()
    assert not dialog.btn_save.isEnabled()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_an_unreachable_network_allows_saving_only_after_confirmation(app, monkeypatch):
    """@brief Rete giu': si puo' salvare, ma solo dopo un «Si'» esplicito."""
    def offline(key):
        raise openrouter.OpenRouterError("connessione fallita: timeout", unreachable=True)
    dialog = _dialog(offline)
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert "non verificabile" in dialog.label_result.text()
    assert dialog.btn_save.isEnabled()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args) or QMessageBox.StandardButton.No)
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args) or QMessageBox.StandardButton.Yes)
    assert dialog.save_key() is True
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    assert len(asked) == 2
    dialog.done(0)


def test_a_verified_key_is_saved_without_asking(app, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("asked"))
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert dialog.save_key() is True
    dialog.done(0)


def test_a_failing_store_is_reported_not_raised(app, monkeypatch):
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    credentials._advapi32.fail_code = 5
    assert dialog.save_key() is False
    assert "errore Windows 5" in dialog.label_result.text()
    dialog.done(0)


def test_remove_asks_and_deletes_only_the_stored_key(app, monkeypatch):
    """@brief «Rimuovi chiave»: chiede conferma, cancella la voce, lascia stare l'ambiente."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-v1-ambiente")
    dialog = _dialog(lambda key: _status())
    assert "variabile d'ambiente" in dialog.label_source.text()
    assert "resta inutilizzata" in dialog.label_source.text()
    assert dialog.btn_remove.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    assert dialog.remove_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert dialog.remove_key() is True
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    assert os.environ[openrouter.API_KEY_ENV] == "sk-or-v1-ambiente"
    assert not dialog.btn_remove.isEnabled()
    dialog.done(0)


def test_the_source_line_names_the_dotenv_file(app):
    path = openrouter.env_files()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{openrouter.API_KEY_ENV}=sk-or-v1-dalfile\n", encoding="utf-8")
    dialog = _dialog(lambda key: _status())
    assert str(path) in dialog.label_source.text()
    assert "sk-or-v1-dalfile" not in dialog.label_source.text()
    assert not dialog.btn_remove.isEnabled()
    dialog.done(0)


def test_closing_during_a_check_ignores_its_late_result(app):
    """@brief Chiudere abbandona la verifica: l'esito tardivo non tocca piu' la finestra."""
    release = threading.Event()

    def slow(key):
        release.wait(5)
        return _status()
    dialog = _dialog(slow)
    _paste(dialog)
    dialog.start_check()
    worker = dialog._worker
    assert not dialog.btn_verify.isEnabled()
    dialog.done(0)
    release.set()
    assert worker.wait(5000)
    for _ in range(5):
        QApplication.processEvents()
    assert dialog._verified_key is None
    assert "Chiave valida" not in dialog.label_result.text()
    assert dialog.field_key.text() == ""


def test_every_button_has_a_tooltip_and_the_key_starts_masked(app):
    dialog = _dialog(lambda key: _status())
    buttons = dialog.findChildren(QPushButton) + dialog.findChildren(QToolButton)
    assert len(buttons) >= 5
    assert all(button.toolTip() for button in buttons)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Password
    dialog.btn_show.setChecked(True)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Normal
    dialog.btn_show.setChecked(False)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Password
    dialog.done(0)


def test_off_windows_save_and_remove_are_disabled(app, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert not dialog.btn_save.isEnabled() and not dialog.btn_remove.isEnabled()
    assert "non è disponibile" in dialog.label_source.text()
    dialog.done(0)


# ── Elenco dei fornitori, saldi e chiave DeepSeek (HANDOFF 129) ─────────

from kicadgen.gui.ai_account_dialog import ProviderAccount, default_accounts  # noqa: E402
from kicadgen.services import providers  # noqa: E402

DS_KEY = "sk-deepseek-incollata-dallutente"


def _balance(amount=20.0):
    return providers.ProviderBalance("deepseek", True, amount, "USD")


def _accounts(ds_checker=lambda key: _balance(), or_balance=lambda: _status(),
              ds_balance=lambda: _balance()):
    """I due fornitori veri, con verifiche e saldi finti (nessuna rete)."""
    real = {a.provider: a for a in default_accounts()}
    return [
        ProviderAccount("openrouter", real["openrouter"].keys_url, credentials.OPENROUTER_TARGET,
                        lambda key: _status(), openrouter.api_key_source, or_balance,
                        openrouter.key_rejected),
        ProviderAccount("deepseek", real["deepseek"].keys_url, credentials.DEEPSEEK_TARGET,
                        ds_checker, providers.deepseek_key_source, ds_balance,
                        providers.deepseek_key_rejected),
    ]


def _wait_balances(dialog):
    for worker in list(dialog._balance_workers):
        assert worker.wait(5000)
    for _ in range(10):
        QApplication.processEvents()


def test_every_provider_is_listed_with_the_source_of_its_key_never_the_key(app, monkeypatch):
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    monkeypatch.setenv(providers.DEEPSEEK_KEY_ENV, DS_KEY)
    dialog = AiAccountDialog(None, accounts=_accounts())
    rows = {dialog.table.item(r, 0).text(): dialog.table.item(r, 1).text()
            for r in range(dialog.table.rowCount())}
    assert set(rows) == {"OpenRouter", "DeepSeek (API diretta)"}
    assert rows["OpenRouter"] == "Gestore credenziali di Windows"
    assert providers.DEEPSEEK_KEY_ENV in rows["DeepSeek (API diretta)"]
    shown = " ".join(dialog.table.item(r, c).text() for r in range(2) for c in range(3))
    assert KEY not in shown and DS_KEY not in shown
    dialog.done(0)


def test_the_balance_of_each_configured_provider_is_shown_beside_it(app):
    """Utente, 27/09: «mostrerei di lato anche il balance corrente»."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    credentials.write_secret(credentials.DEEPSEEK_TARGET, DS_KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=lambda: _balance(19.94)),
                             read_balances=True)
    _wait_balances(dialog)
    assert dialog.table.item(0, 2).text() == "12.50 USD"
    assert dialog.table.item(1, 2).text() == "19.94 USD"
    dialog.done(0)


def test_a_provider_without_a_key_or_with_an_unreadable_balance_says_so(app):
    """Negativo: senza chiave nessuna lettura; un errore di lettura non e' un saldo."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)

    def down():
        raise openrouter.OpenRouterError("network down")
    dialog = AiAccountDialog(None, accounts=_accounts(or_balance=down), read_balances=True)
    _wait_balances(dialog)
    assert dialog.table.item(0, 2).text() == "non disponibile"
    assert "network down" in dialog.table.item(0, 2).toolTip()
    assert dialog.table.item(1, 2).text() == "—"                 # DeepSeek: no key
    dialog.done(0)


def test_no_balance_is_read_unless_asked(app):
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(
        or_balance=lambda: pytest.fail("network")))
    assert not dialog._balance_workers
    dialog.done(0)


def test_a_deepseek_key_is_verified_saved_and_removed_in_its_own_entry(app, monkeypatch):
    seen = []
    dialog = AiAccountDialog(None, accounts=_accounts(
        ds_checker=lambda key: seen.append(key) or _balance()))
    dialog.select_provider("deepseek")
    assert "DeepSeek" in dialog.field_key.placeholderText()
    assert "platform.deepseek.com" in dialog.label_provider.text()
    assert "Nessuna chiave configurata" in dialog.label_source.text()
    dialog.field_key.setText(DS_KEY)
    dialog.start_check()
    _finish(dialog)
    assert seen == [DS_KEY]
    assert "Chiave valida" in dialog.label_result.text() and "20.00" in dialog.label_result.text()
    assert dialog.save_key() is True
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) == DS_KEY
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None   # other entry untouched
    assert providers.deepseek_api_key() == DS_KEY
    assert dialog.table.item(1, 1).text() == "Gestore credenziali di Windows"
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert dialog.remove_key() is True
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


def test_a_key_refused_by_deepseek_cannot_be_saved(app):
    def refuse(key):
        error = providers.ProviderError("HTTP 401 Authentication Fails")
        error.status = 401
        raise error
    dialog = AiAccountDialog(None, accounts=_accounts(ds_checker=refuse))
    dialog.select_provider("deepseek")
    dialog.field_key.setText(DS_KEY)
    dialog.start_check()
    _finish(dialog)
    assert "rifiutata da DeepSeek" in dialog.label_result.text()
    assert not dialog.btn_save.isEnabled() and dialog.save_key() is False
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


def test_switching_provider_drops_the_pasted_key_and_its_verification(app):
    dialog = AiAccountDialog(None, accounts=_accounts())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert dialog.btn_save.isEnabled()
    dialog.select_provider("deepseek")
    assert dialog.field_key.text() == "" and not dialog.btn_save.isEnabled()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


# ── Correzioni della revisione avversariale (129) ───────────────────────


def test_a_key_with_a_line_break_is_never_sent_nor_saved(app):
    """Una chiave incollata con un a capo, mandata come intestazione HTTP, faceva
    riportare alla libreria la chiave intera nel testo d'errore."""
    dialog = AiAccountDialog(None, accounts=_accounts(
        ds_checker=lambda key: pytest.fail("sent")))
    dialog.select_provider("deepseek")
    dialog.field_key.setText("sk-SECRET1\nSECRET2")
    dialog.start_check()
    assert dialog._worker is None
    assert "a capo" in dialog.label_result.text() and "SECRET" not in dialog.label_result.text()
    dialog._unverified_key = dialog._key()                    # even if marked unverified
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


def test_an_answer_that_is_neither_a_verdict_nor_an_outage_does_not_allow_saving(app):
    """HTTP 400/404/429: il fornitore ha risposto, la chiave non e' stata accettata."""
    def bad_request(key):
        raise openrouter.OpenRouterError("HTTP 429 too many requests", http_status=429)
    dialog = _dialog(bad_request)
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert "non verificata" in dialog.label_result.text()
    assert not dialog.btn_save.isEnabled() and dialog.save_key() is False
    dialog.done(0)


def test_a_late_balance_of_a_removed_key_does_not_overwrite_the_row(app, monkeypatch):
    release = threading.Event()

    def slow():
        release.wait(5)
        return _balance(99.0)
    credentials.write_secret(credentials.DEEPSEEK_TARGET, DS_KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=slow), read_balances=True)
    running = list(dialog._balance_workers)
    dialog.select_provider("deepseek")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert dialog.remove_key() is True
    assert dialog.table.item(1, 2).text() == "—"
    release.set()
    for worker in running:
        assert worker.wait(5000)
    for _ in range(10):
        QApplication.processEvents()
    assert dialog.table.item(1, 2).text() == "—"
    dialog.done(0)



def test_a_loader_runs_while_the_provider_is_called(app):
    """Utente, 27/09: «fai partire un loader per far capire che fai la chiamata»."""
    release = threading.Event()

    def slow(key):
        release.wait(5)
        return _status()
    dialog = _dialog(slow)
    assert dialog.busy.isHidden()                             # nothing running
    _paste(dialog)
    dialog.start_check()
    assert not dialog.busy.isHidden()
    release.set()
    _finish(dialog)
    assert dialog.busy.isHidden()
    assert "Chiave valida" in dialog.label_result.text()
    dialog.done(0)


def test_the_loader_also_covers_the_balance_reads(app):
    release = threading.Event()

    def slow():
        release.wait(5)
        return _status()
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(or_balance=slow), read_balances=True)
    assert not dialog.busy.isHidden()
    release.set()
    _wait_balances(dialog)
    assert dialog.busy.isHidden()
    dialog.done(0)



def test_with_an_empty_field_verify_checks_the_key_in_use(app):
    """Utente, 27/09: a campo vuoto «Verifica» controlla la chiave gia' in uso,
    con la barra durante la chiamata; il saldo va anche nell'elenco."""
    release = threading.Event()

    def slow():
        release.wait(5)
        return _balance(19.93)
    credentials.write_secret(credentials.DEEPSEEK_TARGET, DS_KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=slow))
    dialog.select_provider("deepseek")
    assert dialog.field_key.text() == "" and dialog.btn_verify.isEnabled()
    dialog.start_check()
    assert not dialog.busy.isHidden()
    assert "chiave in uso" in dialog.label_result.text()
    release.set()
    _finish(dialog)
    assert dialog.busy.isHidden()
    assert "Chiave in uso valida" in dialog.label_result.text()
    assert dialog.table.item(1, 2).text() == "19.93 USD"
    assert not dialog.btn_save.isEnabled() and dialog.save_key() is False
    dialog.done(0)


def test_with_no_key_at_all_verify_stays_off(app):
    """Negativo: nessuna chiave in uso e campo vuoto -> niente da verificare."""
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=lambda: pytest.fail("sent")))
    dialog.select_provider("deepseek")
    assert not dialog.btn_verify.isEnabled()
    dialog.start_check()
    assert dialog._worker is None
    dialog.done(0)


def test_a_refused_key_in_use_is_reported(app):
    def refused():
        raise openrouter.OpenRouterError("HTTP 401", http_status=401)
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(or_balance=refused))
    dialog.start_check()
    _finish(dialog)
    assert "Chiave in uso rifiutata da OpenRouter" in dialog.label_result.text()
    dialog.done(0)
