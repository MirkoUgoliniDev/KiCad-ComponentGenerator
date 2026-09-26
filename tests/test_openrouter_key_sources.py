"""@brief Where `openrouter.api_key` finds the key, and the free key verification.

Order (PIANO-FIX item 2): process environment, then the Windows Credential
Manager, then the `.env` files. No network (urlopen is replaced) and no real
Credential Manager (conftest installs an in-memory vault and points the
`.env` lookup at the test's tmp folder).
"""
import importlib.util
import io
import json
import os
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from kicadgen.services import credentials, openrouter

ENV_KEY = "sk-or-v1-dallambiente"
STORED_KEY = "sk-or-v1-dalgestore"
FILE_KEY = "sk-or-v1-dalfile"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    return credentials._advapi32


def _dotenv(value):
    path = openrouter.env_files()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"OTHER=1\n{openrouter.API_KEY_ENV}={value}\n", encoding="utf-8")
    return path


def test_the_environment_wins_over_the_store_and_the_file(windows, monkeypatch):
    """@brief Positivo: la variabile d'ambiente del processo vince su tutto."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, STORED_KEY)
    _dotenv(FILE_KEY)
    monkeypatch.setenv(openrouter.API_KEY_ENV, ENV_KEY)
    assert openrouter.api_key() == ENV_KEY
    source = openrouter.api_key_source()
    assert source.kind == openrouter.KEY_SOURCE_ENVIRONMENT
    assert source.stored is True


def test_the_store_wins_over_the_file(windows):
    """@brief Positivo: senza variabile, la chiave salvata con «Account AI...» vince sul .env."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, STORED_KEY)
    _dotenv(FILE_KEY)
    assert openrouter.api_key() == STORED_KEY
    assert openrouter.api_key_source().kind == openrouter.KEY_SOURCE_CREDENTIAL_MANAGER


def test_the_dotenv_file_still_works_alone(windows):
    """@brief Compatibilita': chi ha solo il .env continua a funzionare, senza toccare os.environ."""
    path = _dotenv(f"  {FILE_KEY}  ")
    assert openrouter.api_key() == FILE_KEY
    source = openrouter.api_key_source()
    assert source.kind == openrouter.KEY_SOURCE_DOTENV and source.location == str(path)
    assert openrouter.API_KEY_ENV not in os.environ


def test_blank_values_do_not_count_as_keys(windows, monkeypatch):
    """@brief Negativo: variabile vuota e .env vuoto non sono chiavi; si passa alla fonte dopo."""
    monkeypatch.setenv(openrouter.API_KEY_ENV, "   ")
    _dotenv("")
    credentials.write_secret(credentials.OPENROUTER_TARGET, STORED_KEY)
    assert openrouter.api_key() == STORED_KEY


def test_no_key_anywhere_raises_a_missing_key_error_pointing_to_account_ai(windows):
    """@brief Negativo: nessuna fonte -> MissingApiKeyError (un OpenRouterError) con la via d'uscita."""
    with pytest.raises(openrouter.MissingApiKeyError) as caught:
        openrouter.api_key()
    assert isinstance(caught.value, openrouter.OpenRouterError)
    assert openrouter.API_KEY_ENV in str(caught.value)
    assert "Account AI" in str(caught.value)
    assert openrouter.api_key_source().kind == openrouter.KEY_SOURCE_NONE


def test_an_unreadable_store_falls_through_to_the_file_and_is_reported(windows):
    """@brief Un Gestore credenziali in errore non nasconde il .env e non fa cadere nulla."""
    credentials.write_secret(credentials.OPENROUTER_TARGET, STORED_KEY)
    windows.fail_code = 5
    _dotenv(FILE_KEY)
    assert openrouter.api_key() == FILE_KEY
    source = openrouter.api_key_source()
    assert source.kind == openrouter.KEY_SOURCE_DOTENV
    assert "5" in source.credential_error
    path = openrouter.env_files()[0]
    path.unlink()
    with pytest.raises(openrouter.MissingApiKeyError, match="Gestore credenziali"):
        openrouter.api_key()


def test_the_source_never_carries_the_key(windows, monkeypatch):
    credentials.write_secret(credentials.OPENROUTER_TARGET, STORED_KEY)
    monkeypatch.setenv(openrouter.API_KEY_ENV, ENV_KEY)
    text = repr(openrouter.api_key_source())
    assert STORED_KEY not in text and ENV_KEY not in text


def test_off_windows_the_store_is_skipped(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _dotenv(FILE_KEY)
    assert openrouter.api_key() == FILE_KEY
    assert openrouter.api_key_source().credential_error == ""


def test_error_texts_redact_a_key_that_came_from_the_store(windows):
    """@brief La pulizia dei messaggi d'errore copre anche la chiave che non sta nell'ambiente."""
    key = "chiave-senza-prefisso-riconoscibile"
    credentials.write_secret(credentials.OPENROUTER_TARGET, key)
    assert key not in openrouter._safe_error_text(f"rate limit for {key}")


# ── Verifica della chiave, senza spendere ───────────────────────────────

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_urlopen(answers, seen):
    def urlopen(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization")))
        answer = answers[request.full_url]
        if isinstance(answer, Exception):
            raise answer
        return _Response(json.dumps(answer).encode())
    return urlopen


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "Unauthorized", {}, io.BytesIO(
        b'{"error": {"message": "No auth credentials found", "code": 401}}'))


def test_verification_uses_the_pasted_key_and_reads_credit(monkeypatch):
    """@brief Positivo: solo i due endpoint gratuiti, con la chiave incollata, e nessuna cache."""
    monkeypatch.setenv(openrouter.API_KEY_ENV, ENV_KEY)
    seen = []
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen({
        openrouter.KEY_STATUS_URL: {"data": {"limit": 10, "limit_remaining": 7.5}},
        openrouter.CREDITS_URL: {"data": {"total_credits": 20, "total_usage": 4}},
    }, seen))
    status = openrouter.verify_api_key("  sk-or-v1-nuova  ")
    assert status.key_remaining == 7.5 and status.account_remaining == 16
    assert {url for url, _ in seen} == {openrouter.KEY_STATUS_URL, openrouter.CREDITS_URL}
    assert all(header == "Bearer sk-or-v1-nuova" for _, header in seen)
    assert openrouter._credit_cache is None


def test_a_refused_key_is_told_apart_from_an_unreachable_network(monkeypatch):
    """@brief 401 = chiave rifiutata; rete giu' = chiave non giudicata. Nessuna delle due contiene la chiave."""
    key = "sk-or-v1-rifiutata"
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen({
        openrouter.KEY_STATUS_URL: _http_error(openrouter.KEY_STATUS_URL, 401)}, []))
    with pytest.raises(openrouter.OpenRouterError) as refused:
        openrouter.verify_api_key(key)
    assert openrouter.key_rejected(refused.value) and not refused.value.unreachable
    assert key not in str(refused.value)

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen({
        openrouter.KEY_STATUS_URL: urllib.error.URLError("getaddrinfo failed")}, []))
    with pytest.raises(openrouter.OpenRouterError) as offline:
        openrouter.verify_api_key(key)
    assert offline.value.unreachable and not openrouter.key_rejected(offline.value)


def test_a_server_error_is_neither_refusal_nor_unreachable(monkeypatch):
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen({
        openrouter.KEY_STATUS_URL: _http_error(openrouter.KEY_STATUS_URL, 503)}, []))
    with pytest.raises(openrouter.OpenRouterError) as caught:
        openrouter.verify_api_key("sk-or-v1-x")
    assert caught.value.http_status == 503
    assert not openrouter.key_rejected(caught.value) and not caught.value.unreachable


def test_a_missing_balance_does_not_fail_a_valid_key(monkeypatch):
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", _fake_urlopen({
        openrouter.KEY_STATUS_URL: {"data": {"limit": None}},
        openrouter.CREDITS_URL: _http_error(openrouter.CREDITS_URL, 403)}, []))
    status = openrouter.verify_api_key("sk-or-v1-x")
    assert status.account_error and status.remaining is None


def test_an_empty_key_is_refused_without_any_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network used")
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", forbidden)
    with pytest.raises(openrouter.OpenRouterError):
        openrouter.verify_api_key("   ")


# ── Il lanciatore non lascia la chiave del .env nell'ambiente ────────────

def _run_launcher(monkeypatch, tmp_path, dotenv_text):
    env_file = tmp_path / "launcher.env"
    env_file.write_text(dotenv_text, encoding="utf-8")
    import kicadgen.app_paths as app_paths
    monkeypatch.setattr(app_paths, "env_files", lambda: [env_file])
    fake_gui = types.ModuleType("kicadgen.gui.main_window")
    fake_gui.main = lambda: 0
    monkeypatch.setitem(sys.modules, "kicadgen.gui.main_window", fake_gui)
    spec = importlib.util.spec_from_file_location(
        "launcher_under_test", Path(__file__).resolve().parents[1] / "main.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "_project_python", lambda root: None)
    monkeypatch.setattr(launcher.sys, "argv", ["main.py"])
    # setenv first, so teardown removes whatever load_dotenv puts there.
    monkeypatch.setenv("KICADGEN_TEST_OTHER", "segnaposto")
    monkeypatch.delenv("KICADGEN_TEST_OTHER")
    if openrouter.API_KEY_ENV not in os.environ:
        monkeypatch.setenv(openrouter.API_KEY_ENV, "segnaposto")
        monkeypatch.delenv(openrouter.API_KEY_ENV)
    assert launcher.main() == 0


def test_the_launcher_loads_other_variables_but_not_the_dotenv_key(monkeypatch, tmp_path):
    """@brief Positivo: le altre variabili del .env entrano, la chiave no (la legge api_key dopo il Gestore)."""
    pytest.importorskip("dotenv")
    _run_launcher(monkeypatch, tmp_path,
                  f"KICADGEN_TEST_OTHER=si\n{openrouter.API_KEY_ENV}={FILE_KEY}\n")
    assert os.environ.get("KICADGEN_TEST_OTHER") == "si"
    assert openrouter.API_KEY_ENV not in os.environ


def test_the_launcher_keeps_a_key_that_was_already_in_the_environment(monkeypatch, tmp_path):
    """@brief Negativo: una chiave gia' nell'ambiente vero resta, e il .env non la sovrascrive."""
    pytest.importorskip("dotenv")
    monkeypatch.setenv(openrouter.API_KEY_ENV, ENV_KEY)
    _run_launcher(monkeypatch, tmp_path, f"{openrouter.API_KEY_ENV}={FILE_KEY}\n")
    assert os.environ.get(openrouter.API_KEY_ENV) == ENV_KEY
