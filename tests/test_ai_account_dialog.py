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

#: One QApplication for the module (see test_photo_inventory_gui for why a
#: local reference is not enough).
_APP = None


@pytest.fixture
def app(monkeypatch):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    monkeypatch.setattr(sys, "platform", "win32")
    return _APP


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
