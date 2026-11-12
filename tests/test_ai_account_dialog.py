"""@brief «Account AI»: one «Verifica e salva» button, the «Chiave» cell per source, the .env move.

Offscreen Qt, a fake checker instead of OpenRouter and the in-memory vault
that conftest installs instead of the Windows Credential Manager.
Layout approved by the user on 2026-11-12 (the previous one was «troppo
confusionaria», too confusing): the save button verifies first, a refused
key is never saved, an unjudged one only after a confirmation.
"""
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QToolButton

from kicadgen.gui.ai_account_dialog import AiAccountDialog
from kicadgen.services import credentials, dotenv_keys, openrouter

KEY = "sk-or-v1-incollata-dallutente"


@pytest.fixture
def app(qapp, monkeypatch):
    """@brief The session QApplication, on a platform that has the Credential Manager.

    `dotenv_keys.other_files_setting` reads its own `env_files` name:
    pointed at the test's own `.env` like conftest does for the providers, so
    no test reads the developer's real `.env` files.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dotenv_keys, "env_files", lambda: list(openrouter.env_files()))
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


def _visible_texts(dialog):
    """Every text the window shows or offers as a tooltip: labels, buttons, cells, title."""
    texts = [dialog.windowTitle(), dialog.panel.title(), dialog.action_show.toolTip()]
    for widget in dialog.findChildren(QLabel) + dialog.findChildren(QPushButton):
        texts += [widget.text(), widget.toolTip()]
    table = dialog.table
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            texts += [table.item(row, column).text(), table.item(row, column).toolTip()]
    return " ".join(texts)


def test_verify_and_save_saves_an_accepted_key_and_the_cell_follows(app):
    """@brief Positive: one click verifies off the GUI thread, then saves what the provider accepted."""
    seen = []
    dialog = _dialog(lambda key: seen.append((key, threading.current_thread()))
                     or _status())
    assert dialog.table.item(0, 1).text() == "✗ mancante"
    assert "Nessuna chiave configurata" in dialog.label_source.text()
    assert dialog.label_keys_link.isVisibleTo(dialog), "no key: the link to create one is offered"
    _paste(dialog)
    changed = []
    dialog.key_changed.connect(lambda: changed.append(True))
    dialog.btn_save.click()
    _finish(dialog)
    assert seen[0][0] == KEY
    assert seen[0][1] is not threading.main_thread(), "the check ran on the GUI thread"
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    result = dialog.label_result.text()
    assert "Chiave valida" in result and "12.50" in result and "Salvata in Windows" in result
    assert dialog.field_key.text() == ""
    assert changed == [True]
    assert dialog.table.item(0, 1).text() == "✓ salvata in Windows"
    assert dialog.label_source.isHidden(), "nothing needs attention with a key saved in Windows"
    assert KEY not in _visible_texts(dialog)
    assert dialog.btn_remove.isVisibleTo(dialog) and dialog.btn_remove.isEnabled()
    # CONTRACT CHANGED 2026-11-12 (user: «se la key c'è perchè appare questo?»):
    # the link to create a key is hidden once a key exists.
    assert not dialog.label_keys_link.isVisibleTo(dialog), "a key exists: no link to create one"
    dialog.done(0)


def test_save_key_alone_never_saves_an_unverified_key(app):
    """@brief Negative: the button verifies first; the bare `save_key` refuses an unverified key."""
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    assert dialog.btn_save.isEnabled()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_verify_alone_does_not_save(app):
    """@brief Negative: `start_check` (verification only) leaves the Credential Manager alone."""
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert "Chiave valida" in dialog.label_result.text()
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_editing_the_key_after_verification_voids_it(app):
    """@brief Negative: only the verified text is saved, not the one changed afterwards."""
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    _paste(dialog, KEY + "x")
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_a_result_for_a_key_edited_during_the_check_is_dropped(app):
    """@brief Negative: a key changed while the check ran neither inherits the outcome nor is saved."""
    release = threading.Event()

    def slow(key):
        release.wait(5)
        return _status()
    dialog = _dialog(slow)
    _paste(dialog)
    dialog.verify_and_save()
    _paste(dialog, KEY + "altro")
    release.set()
    _finish(dialog)
    assert "Chiave valida" not in dialog.label_result.text()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


def test_a_refused_key_is_not_saved_and_the_reason_is_shown(app, monkeypatch):
    """@brief Negative: HTTP 401 -> «rifiutata», no question, nothing saved."""
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("asked"))

    def refuse(key):
        raise openrouter.OpenRouterError("HTTP 401 unauthorized", http_status=401)
    dialog = _dialog(refuse)
    _paste(dialog)
    dialog.verify_and_save()
    _finish(dialog)
    assert "rifiutata" in dialog.label_result.text()
    assert "Chiave non salvata" in dialog.label_result.text()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    assert dialog.field_key.text() == KEY, "a refused key stays in the field to be corrected"
    dialog.done(0)


def test_an_unjudged_key_is_saved_only_after_confirmation(app, monkeypatch):
    """@brief Network down: «No» saves nothing; a second try with «Sì» saves it."""
    def offline(key):
        raise openrouter.OpenRouterError("connessione fallita: timeout", unreachable=True)
    dialog = _dialog(offline)
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args) or QMessageBox.StandardButton.No)
    _paste(dialog)
    dialog.verify_and_save()
    _finish(dialog)
    assert "non verificabile" in dialog.label_result.text()
    assert "Chiave non salvata" in dialog.label_result.text()
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args) or QMessageBox.StandardButton.Yes)
    dialog.verify_and_save()
    _finish(dialog)
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    assert "Salvata in Windows" in dialog.label_result.text()
    assert len(asked) == 2
    assert KEY not in " ".join(str(part) for args in asked for part in args[1:])
    dialog.done(0)


def test_a_verified_key_is_saved_without_asking(app, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("asked"))
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    dialog.verify_and_save()
    _finish(dialog)
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    dialog.done(0)


def test_a_failing_store_is_reported_not_raised(app, monkeypatch):
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    credentials._advapi32.fail_code = 5
    dialog.verify_and_save()
    _finish(dialog)
    assert "errore Windows 5" in dialog.label_result.text()
    assert dialog.field_key.text() == KEY
    dialog.done(0)


def test_remove_asks_and_deletes_only_the_stored_key(app, monkeypatch):
    """@brief «Rimuovi»: asks, deletes the entry, leaves the environment alone; gone without a stored key."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-v1-ambiente")
    dialog = _dialog(lambda key: _status())
    assert dialog.table.item(0, 1).text() == "⚠ variabile d'ambiente"
    assert openrouter.API_KEY_ENV in dialog.table.item(0, 1).toolTip()
    assert "ha la precedenza" in dialog.label_source.text()
    assert not dialog.btn_move.isVisibleTo(dialog), "no move for an environment variable"
    assert dialog.btn_remove.isVisibleTo(dialog) and dialog.btn_remove.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    assert dialog.remove_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == KEY
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert dialog.remove_key() is True
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    assert os.environ[openrouter.API_KEY_ENV] == "sk-or-v1-ambiente"
    assert not dialog.btn_remove.isVisibleTo(dialog)
    assert "sk-or-v1-ambiente" not in _visible_texts(dialog)
    dialog.done(0)


def test_a_dotenv_key_shows_a_short_cell_with_the_path_in_its_tooltip(app):
    path = openrouter.env_files()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{openrouter.API_KEY_ENV}=sk-or-v1-dalfile\n", encoding="utf-8")
    dialog = _dialog(lambda key: _status())
    assert dialog.table.item(0, 1).text() == "⚠ nel file .env"
    assert str(path) in dialog.table.item(0, 1).toolTip()
    assert dialog.label_source.text() == "⚠ La chiave è scritta in un file .env."
    assert str(path) in dialog.label_source.toolTip()
    assert "sk-or-v1-dalfile" not in _visible_texts(dialog)
    assert not dialog.btn_remove.isVisibleTo(dialog)
    dialog.done(0)


def test_closing_during_a_check_ignores_its_late_result(app):
    """@brief Closing abandons the check: its late outcome neither touches the window nor saves."""
    release = threading.Event()

    def slow(key):
        release.wait(5)
        return _status()
    dialog = _dialog(slow)
    _paste(dialog)
    dialog.verify_and_save()
    worker = dialog._worker
    assert not dialog.btn_save.isEnabled()
    dialog.done(0)
    release.set()
    assert worker.wait(5000)
    for _ in range(5):
        QApplication.processEvents()
    assert dialog._verified_key is None
    assert "Chiave valida" not in dialog.label_result.text()
    assert dialog.field_key.text() == ""
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None


def test_every_button_has_a_tooltip_and_the_key_starts_masked(app):
    dialog = _dialog(lambda key: _status())
    buttons = dialog.findChildren(QPushButton) + dialog.findChildren(QToolButton)
    assert len(buttons) >= 4
    assert all(button.toolTip() for button in buttons)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Password
    # The eye inside the field (2026-11-12): its tooltip says what a click does and the key.
    assert dialog.action_show.text() == "Mostra la chiave"
    assert dialog.action_show.toolTip().startswith("Mostra la chiave (")
    assert not dialog.action_show.icon().isNull()
    dialog.action_show.setChecked(True)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Normal
    assert dialog.action_show.text() == "Nascondi la chiave"
    dialog.action_show.setChecked(False)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Password
    dialog.done(0)


def test_the_eye_has_a_keyboard_shortcut(app):
    """@brief The eye is not reachable with Tab: Alt+F8 toggles it; F8 alone does not."""
    dialog = _dialog(lambda key: _status())
    assert dialog.action_show.shortcut() == QKeySequence("Alt+F8")
    assert "F8" in dialog.action_show.toolTip()
    assert "Alt+F8" in dialog.field_key.accessibleDescription()
    dialog.show()
    dialog.activateWindow()
    dialog.field_key.setFocus()
    QApplication.processEvents()
    QTest.keyClick(dialog.field_key, Qt.Key.Key_F8)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Password
    QTest.keyClick(dialog.field_key, Qt.Key.Key_F8, Qt.KeyboardModifier.AltModifier)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Normal
    QTest.keyClick(dialog.field_key, Qt.Key.Key_F8, Qt.KeyboardModifier.AltModifier)
    assert dialog.field_key.echoMode() == dialog.field_key.EchoMode.Password
    dialog.done(0)


def test_off_windows_save_and_remove_are_disabled(app, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    dialog = _dialog(lambda key: _status())
    _paste(dialog)
    assert not dialog.btn_save.isEnabled()
    dialog.verify_and_save()
    _finish(dialog)
    assert not dialog.btn_remove.isEnabled() and not dialog.btn_remove.isVisibleTo(dialog)
    assert "non è disponibile" in dialog.label_source.text()
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    dialog.done(0)


# ── Provider list, balances and DeepSeek key (HANDOFF 129) ──────────────

from kicadgen.gui.ai_account_dialog import ProviderAccount, default_accounts  # noqa: E402
from kicadgen.services import providers  # noqa: E402

DS_KEY = "sk-deepseek-incollata-dallutente"


def _balance(amount=20.0):
    return providers.ProviderBalance("deepseek", True, amount, "USD")


def _accounts(ds_checker=lambda key: _balance(), or_balance=lambda: _status(),
              ds_balance=lambda: _balance()):
    """The two real providers, with fake checks and balances (no network)."""
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
    rows = {dialog.table.item(r, 0).text(): dialog.table.item(r, 1)
            for r in range(dialog.table.rowCount())}
    assert set(rows) == {"OpenRouter", "DeepSeek (API diretta)"}
    assert rows["OpenRouter"].text() == "✓ salvata in Windows"
    assert rows["DeepSeek (API diretta)"].text() == "⚠ variabile d'ambiente"
    assert providers.DEEPSEEK_KEY_ENV in rows["DeepSeek (API diretta)"].toolTip()
    shown = _visible_texts(dialog)
    assert KEY not in shown and DS_KEY not in shown
    dialog.done(0)


def test_the_balance_of_each_configured_provider_is_shown_beside_it(app):
    """User, 27/09: «mostrerei di lato anche il balance corrente» (I would show the balance beside it)."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    credentials.write_secret(credentials.DEEPSEEK_TARGET, DS_KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=lambda: _balance(19.94)),
                             read_balances=True)
    _wait_balances(dialog)
    assert dialog.table.item(0, 2).text() == "12.50 USD"
    assert dialog.table.item(1, 2).text() == "19.94 USD"
    dialog.done(0)


def test_a_provider_without_a_key_or_with_an_unreadable_balance_says_so(app):
    """Negative: no read without a key; a read error is not a balance."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)

    def down():
        raise openrouter.OpenRouterError("network down")
    dialog = AiAccountDialog(None, accounts=_accounts(or_balance=down), read_balances=True)
    _wait_balances(dialog)
    assert dialog.table.item(0, 2).text() == "non disponibile"
    assert "network down" in dialog.table.item(0, 2).toolTip()
    assert dialog.table.item(1, 2).text() == "—"                 # DeepSeek: no key
    assert dialog.table.item(1, 1).text() == "✗ mancante"
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
    assert dialog.panel.title() == "DeepSeek (API diretta)"
    assert "DeepSeek" in dialog.field_key.placeholderText()
    assert "platform.deepseek.com" in dialog.label_keys_link.text()
    assert "Nessuna chiave configurata" in dialog.label_source.text()
    assert not dialog.btn_remove.isVisibleTo(dialog)
    dialog.field_key.setText(DS_KEY)
    dialog.verify_and_save()
    _finish(dialog)
    assert seen == [DS_KEY]
    assert "Chiave valida" in dialog.label_result.text() and "20.00" in dialog.label_result.text()
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) == DS_KEY
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None   # other entry untouched
    assert providers.deepseek_api_key() == DS_KEY
    assert dialog.table.item(1, 1).text() == "✓ salvata in Windows"
    assert dialog.btn_remove.isVisibleTo(dialog)
    assert not dialog.label_keys_link.isVisibleTo(dialog), "a key exists: no link to create one"
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert dialog.remove_key() is True
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    assert dialog.label_keys_link.isVisibleTo(dialog), "key removed: the link comes back"
    dialog.done(0)


def test_a_key_refused_by_deepseek_cannot_be_saved(app):
    def refuse(key):
        error = providers.ProviderError("HTTP 401 Authentication Fails")
        error.status = 401
        raise error
    dialog = AiAccountDialog(None, accounts=_accounts(ds_checker=refuse))
    dialog.select_provider("deepseek")
    dialog.field_key.setText(DS_KEY)
    dialog.verify_and_save()
    _finish(dialog)
    assert "rifiutata da DeepSeek" in dialog.label_result.text()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


def test_switching_provider_drops_the_pasted_key_and_its_verification(app):
    dialog = AiAccountDialog(None, accounts=_accounts())
    _paste(dialog)
    dialog.start_check()
    _finish(dialog)
    assert dialog._verified_key == KEY
    dialog.select_provider("deepseek")
    assert dialog.field_key.text() == "" and dialog._verified_key is None
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


# ── Fixes of the adversarial review (129) ───────────────────────────────


def test_a_key_with_a_line_break_is_never_sent_nor_saved(app):
    """A key pasted with a line break, sent as an HTTP header, made the library
    echo the whole key in its error text."""
    dialog = AiAccountDialog(None, accounts=_accounts(
        ds_checker=lambda key: pytest.fail("sent")))
    dialog.select_provider("deepseek")
    dialog.field_key.setText("sk-SECRET1\nSECRET2")
    dialog.verify_and_save()
    assert dialog._worker is None
    assert "a capo" in dialog.label_result.text() and "SECRET" not in dialog.label_result.text()
    dialog._unverified_key = dialog._key()                    # even if marked unverified
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.DEEPSEEK_TARGET) is None
    dialog.done(0)


def test_an_answer_that_is_neither_a_verdict_nor_an_outage_does_not_allow_saving(app, monkeypatch):
    """HTTP 400/404/429: the provider answered, the key was not accepted; no question either."""
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("asked"))

    def bad_request(key):
        raise openrouter.OpenRouterError("HTTP 429 too many requests", http_status=429)
    dialog = _dialog(bad_request)
    _paste(dialog)
    dialog.verify_and_save()
    _finish(dialog)
    assert "non verificata" in dialog.label_result.text()
    assert "Chiave non salvata" in dialog.label_result.text()
    assert dialog.save_key() is False
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
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
    """User, 27/09: «fai partire un loader per far capire che fai la chiamata» (show a loader)."""
    release = threading.Event()

    def slow(key):
        release.wait(5)
        return _status()
    dialog = _dialog(slow)
    assert dialog.busy.isHidden()                             # nothing running
    _paste(dialog)
    dialog.verify_and_save()
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


def test_with_an_empty_field_the_button_checks_the_key_in_use_and_saves_nothing(app):
    """User, 27/09: with the field empty the check reads the key already in use,
    with the bar during the call; the balance also goes in the list."""
    release = threading.Event()

    def slow():
        release.wait(5)
        return _balance(19.93)
    credentials.write_secret(credentials.DEEPSEEK_TARGET, DS_KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=slow))
    dialog.select_provider("deepseek")
    assert dialog.field_key.text() == "" and dialog.btn_save.isEnabled()
    dialog.btn_save.click()
    assert not dialog.busy.isHidden()
    assert "chiave in uso" in dialog.label_result.text()
    release.set()
    _finish(dialog)
    assert dialog.busy.isHidden()
    assert "Chiave in uso valida" in dialog.label_result.text()
    assert dialog.table.item(1, 2).text() == "19.93 USD"
    assert dialog.save_key() is False
    dialog.done(0)


def test_with_no_key_at_all_the_button_stays_off(app):
    """Negative: no key in use and an empty field -> nothing to verify or save."""
    dialog = AiAccountDialog(None, accounts=_accounts(ds_balance=lambda: pytest.fail("sent")))
    dialog.select_provider("deepseek")
    assert not dialog.btn_save.isEnabled()
    dialog.verify_and_save()
    assert dialog._worker is None
    dialog.done(0)


def test_a_refused_key_in_use_is_reported(app):
    def refused():
        raise openrouter.OpenRouterError("HTTP 401", http_status=401)
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    dialog = AiAccountDialog(None, accounts=_accounts(or_balance=refused))
    dialog.verify_and_save()
    _finish(dialog)
    assert "Chiave in uso rifiutata da OpenRouter" in dialog.label_result.text()
    dialog.done(0)


# -- «Spostala in Windows» (user request 2026-11-12) ------------------------------------

def _dotenv_with_key(extra=""):
    path = openrouter.env_files()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# chiavi\n{openrouter.API_KEY_ENV}=sk-or-v1-dalfile\nALTRO=1\n{extra}", encoding="utf-8")
    return path


def test_a_dotenv_key_is_verified_moved_and_its_line_removed_on_yes(app, monkeypatch):
    """@brief Positive: the key of the file goes to the Credential Manager; only its line leaves the file."""
    path = _dotenv_with_key()
    checked = []
    asked = []
    dialog = _dialog(lambda key: checked.append(key) or _status())
    assert dialog.btn_move.text() == "Spostala in Windows"
    assert dialog.btn_move.isVisibleTo(dialog) and dialog.btn_move.isEnabled()
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: asked.append(args[2]) or QMessageBox.StandardButton.Yes)
    assert dialog.move_key() is True
    _finish(dialog)
    assert checked == ["sk-or-v1-dalfile"], "the key of the file is verified before moving"
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == "sk-or-v1-dalfile"
    assert path.read_text(encoding="utf-8") == "# chiavi\nALTRO=1\n"
    assert "editor" in asked[0], "the question warns about an editor holding the file open"
    assert "Riga tolta dal file .env" in dialog.label_result.text()
    assert "sk-or-v1-dalfile" not in _visible_texts(dialog) + " ".join(asked)
    assert dialog.table.item(0, 1).text() == "✓ salvata in Windows"
    assert not dialog.btn_move.isVisibleTo(dialog), "the key no longer comes from a file"
    dialog.done(0)


def test_no_keeps_the_file_and_a_refused_key_is_not_moved(app, monkeypatch):
    """@brief Negatives: «No» leaves the file as it is; a key the provider refuses is never saved."""
    path = _dotenv_with_key()
    before = path.read_text(encoding="utf-8")
    dialog = _dialog(lambda key: _status())
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    dialog.move_key()
    _finish(dialog)
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) == "sk-or-v1-dalfile"
    assert path.read_text(encoding="utf-8") == before
    dialog.done(0)
    credentials.delete_secret(credentials.OPENROUTER_TARGET)

    def refuse(key):
        raise openrouter.OpenRouterError("401 Unauthorized", status=401)
    dialog = _dialog(refuse)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: asked.append(args) or QMessageBox.StandardButton.Yes)
    dialog.move_key()
    _finish(dialog)
    assert credentials.read_secret(credentials.OPENROUTER_TARGET) is None
    assert path.read_text(encoding="utf-8") == before and asked == []
    assert "non spostata" in dialog.label_result.text()
    dialog.done(0)


def test_the_move_button_is_absent_when_the_key_is_not_in_a_file(app):
    """@brief Negative: a key in the Credential Manager (or none) offers no move."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, KEY)
    dialog = _dialog(lambda key: _status())
    assert not dialog.btn_move.isVisibleTo(dialog)
    assert dialog.move_key() is False
    dialog.done(0)
    credentials.delete_secret(credentials.OPENROUTER_TARGET)
    dialog = _dialog(lambda key: _status())
    assert not dialog.btn_move.isVisibleTo(dialog) and dialog.move_key() is False
    dialog.done(0)


def test_a_line_gone_before_the_removal_is_never_reported_as_removed(app, monkeypatch):
    """@brief Negative (ABSENT): the line vanished while the question was open; no success claimed."""
    path = _dotenv_with_key()

    def edit_then_yes(*args):
        path.write_text("# chiavi\nALTRO=1\n", encoding="utf-8")
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", edit_then_yes)
    dialog = _dialog(lambda key: _status())
    dialog.move_key()
    _finish(dialog)
    result = dialog.label_result.text()
    assert "Riga tolta" not in result
    assert "non è stato tolto nulla" in result and openrouter.API_KEY_ENV in result
    dialog.done(0)


def test_an_indirect_line_is_left_and_the_window_says_where_the_secret_is(app, monkeypatch):
    """@brief Negative (INDIRECT): «KEY=${OTHER}» is not removed; the result names the reference."""
    path = openrouter.env_files()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"SEGRETO=sk-or-v1-dalfile\n{openrouter.API_KEY_ENV}=${{SEGRETO}}\n"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    checked = []
    dialog = _dialog(lambda key: checked.append(key) or _status())
    dialog.move_key()
    _finish(dialog)
    assert checked == ["sk-or-v1-dalfile"]
    assert path.read_text(encoding="utf-8") == text
    result = dialog.label_result.text()
    assert "Riga tolta" not in result and "${…}" in result and "altra variabile" in result
    assert "sk-or-v1-dalfile" not in result
    dialog.done(0)


def test_other_dotenv_files_that_still_hold_the_key_are_named(app, monkeypatch, tmp_path):
    """@brief Positive and negative: a second .env with the key is named; without one, nothing is added."""
    path = _dotenv_with_key()
    second = tmp_path / "second" / ".env"
    second.parent.mkdir()
    second.write_text(f"{openrouter.API_KEY_ENV}=sk-or-v1-altrofile\n", encoding="utf-8")
    monkeypatch.setattr(dotenv_keys, "env_files", lambda: [path, second])
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog = _dialog(lambda key: _status())
    dialog.move_key()
    _finish(dialog)
    result = dialog.label_result.text()
    assert "Riga tolta" in result and "ancora in questi file" in result and str(second) in result
    assert "sk-or-v1-altrofile" not in result
    dialog.done(0)

    credentials.delete_secret(credentials.OPENROUTER_TARGET)
    path = _dotenv_with_key()
    monkeypatch.setattr(dotenv_keys, "env_files", lambda: [path])
    dialog = _dialog(lambda key: _status())
    dialog.move_key()
    _finish(dialog)
    assert "ancora in questi file" not in dialog.label_result.text()
    dialog.done(0)
