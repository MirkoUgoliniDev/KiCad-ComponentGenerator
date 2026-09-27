"""@brief DeepSeek's own API as a second provider (user, 2026-09-27), offline."""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from kicadgen.services import openrouter, providers


# ── provider written in the identifier, never guessed from the name ─────

@pytest.mark.parametrize("model,provider,name", [
    ("deepseek-api/deepseek-flash", "deepseek", "deepseek-flash"),
    ("deepseek/deepseek-v4-pro", "openrouter", "deepseek/deepseek-v4-pro"),   # via OpenRouter
    ("anthropic/claude-opus-5", "openrouter", "anthropic/claude-opus-5"),
    (None, "openrouter", None)])
def test_the_provider_is_read_from_the_identifier(model, provider, name):
    assert providers.provider_of(model) == provider
    if model:
        assert providers.provider_model_name(model) == name


def test_a_missing_key_names_the_variable_never_a_value(monkeypatch):
    monkeypatch.delenv(providers.DEEPSEEK_KEY_ENV, raising=False)
    monkeypatch.setattr(providers, "env_files", lambda: [])
    with pytest.raises(providers.ProviderKeyMissing, match=providers.DEEPSEEK_KEY_ENV):
        providers.deepseek_api_key()
    assert issubclass(providers.ProviderError, openrouter.OpenRouterError)


def test_the_environment_key_is_used(monkeypatch):
    monkeypatch.setenv(providers.DEEPSEEK_KEY_ENV, "  sk-test  ")
    assert providers.deepseek_api_key() == "sk-test"


# ── models and balance ─────────────────────────────────────────────────────

def test_models_are_listed_with_undeclared_capabilities_and_prices(monkeypatch):
    monkeypatch.setattr(providers, "deepseek_request", lambda url, *a, **k: {
        "data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}, {"id": ""}, "junk"]})
    models = providers.deepseek_models()
    assert [m.id for m in models] == ["deepseek-api/deepseek-flash", "deepseek-api/deepseek-v4-pro"]
    assert all(m.provider == "deepseek" and not m.capabilities_declared for m in models)
    assert all(math.isnan(m.prompt_usd_per_mtok) for m in models)


def test_the_balance_is_read_and_described(monkeypatch):
    monkeypatch.setattr(providers, "deepseek_request", lambda url, *a, **k: {
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "20.00"}]})
    balance = providers.deepseek_balance()
    assert (balance.available, balance.amount, balance.currency) == (True, 20.0, "USD")
    assert "20.00 USD" in balance.describe()


def test_an_exhausted_deepseek_account_stops_the_run_and_an_unreadable_one_does_not(monkeypatch):
    monkeypatch.setenv(providers.DEEPSEEK_KEY_ENV, "sk-test")
    monkeypatch.setattr(providers, "deepseek_balance", lambda: providers.ProviderBalance(
        "deepseek", False, 0.0, "USD"))
    with pytest.raises(providers.ProviderError, match="esaurito"):
        providers.ensure_providers_ready(["deepseek-api/deepseek-flash"])

    def unreadable():
        raise providers.ProviderError("rete giu'")
    monkeypatch.setattr(providers, "deepseek_balance", unreadable)
    logs = []
    providers.ensure_providers_ready(["deepseek-api/deepseek-flash"], logs.append)
    assert any("non verificabile" in line for line in logs)


# ── call routing ───────────────────────────────────────────────────────────

_TOOL = {"type": "function", "function": {"name": "report", "parameters": {"type": "object"}}}


def test_a_deepseek_model_is_called_on_deepseek_with_its_own_name(monkeypatch):
    sent = {}

    def fake(url, body=None, timeout=None):
        sent.update(url=url, body=body)
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "report", "arguments": '{"ok": true}'}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    monkeypatch.setattr(providers, "deepseek_request", fake)
    result = openrouter.call_tool("deepseek-api/deepseek-flash", "sys",
                                  [{"type": "text", "text": "hi"}], _TOOL)
    assert result == {"ok": True}
    assert sent["url"] == providers.DEEPSEEK_CHAT_URL
    assert sent["body"]["model"] == "deepseek-flash"
    assert "usage" not in sent["body"] and "plugins" not in sent["body"]
    # 27/09: thinking mode refuses a named tool_choice (HTTP 400), so it is off.
    assert sent["body"]["thinking"] == {"type": "disabled"}
    assert sent["body"]["tool_choice"]["function"]["name"] == "report"


def test_a_pdf_is_refused_before_any_deepseek_request(monkeypatch):
    monkeypatch.setattr(providers, "deepseek_request",
                        lambda *a, **k: pytest.fail("no request for an unreadable PDF"))
    with pytest.raises(providers.ProviderError, match="PDF"):
        openrouter.call_tool("deepseek-api/deepseek-flash", "sys", [], _TOOL, has_pdf=True)


# ── GUI: picker filter and balance line ───────────────────────────────────

def test_the_picker_starts_on_the_provider_of_the_current_model(monkeypatch):
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui import model_dialog
    app = QApplication.instance() or QApplication([])  # noqa: F841 - must stay alive
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models", lambda self, **k: None)
    dialog = model_dialog.ModelPickerDialog("deepseek-api/deepseek-flash")
    try:
        assert dialog.provider() == "deepseek"
        undeclared = openrouter.ModelInfo(
            id="deepseek-api/deepseek-flash", name="x", prompt_usd_per_mtok=math.nan,
            completion_usd_per_mtok=math.nan, context_length=0, input_modalities=(),
            supports_tools=False, provider="deepseek", capabilities_declared=False)
        text_only = openrouter.ModelInfo(
            id="deepseek/deepseek-v4-pro", name="y", prompt_usd_per_mtok=.2,
            completion_usd_per_mtok=.7, context_length=1, input_modalities=("text",),
            supports_tools=True)
        dialog._models = [undeclared, text_only]
        dialog.check_compatible.setChecked(True)
        # Undeclared is not "incompatible": kept; a declared text-only model goes.
        assert [m.id for m in dialog._visible_models()] == ["deepseek-api/deepseek-flash"]
        assert model_dialog._price_text(math.nan) == "n.d."
    finally:
        dialog.close()


def test_the_balance_line_follows_the_provider(monkeypatch):
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui.ai_account import AiAccountLine
    app = QApplication.instance() or QApplication([])  # noqa: F841 - must stay alive
    line = AiAccountLine(lambda: None, lambda _m: None)
    try:
        line.set_provider("deepseek")
        line._status = providers.ProviderBalance("deepseek", True, 20.0, "USD")
        line._balance_state = "ok"
        line.refresh()
        assert "Saldo DeepSeek: 20.00 USD" in line.text()
        line.set_provider("openrouter")
        assert "DeepSeek" not in line.text()
    finally:
        line.detach()


def test_the_balance_line_asks_for_the_provider_at_every_refresh():
    """User, 27/09: after a DeepSeek run the line still said «Saldo OpenRouter».

    The model can change through paths that emit no event (project restore):
    the line reads the provider of the model in use at every refresh.
    """
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui.ai_account import AiAccountLine
    app = QApplication.instance() or QApplication([])  # noqa: F841 - must stay alive
    line = AiAccountLine(lambda: None, lambda _m: None)
    try:
        in_use = ["openrouter"]
        line.provider_source = lambda: in_use[0]
        line.refresh()
        assert line.provider == "openrouter"
        in_use[0] = "deepseek"                      # changed without any signal
        line.refresh()
        assert line.provider == "deepseek"
        assert "OpenRouter" not in line.text()
    finally:
        line.detach()


from tests.test_photo_inventory_gui import window  # noqa: E402,F401 - fixture condivisa


def test_restoring_a_project_model_moves_the_balance_to_its_provider(window):
    """User, 27/09: a project reopened with a DeepSeek session model still showed
    «Saldo OpenRouter». The restore path goes through _refresh_model_combo."""
    window._session_model = "deepseek-api/deepseek-flash"     # as the restore does
    window._refresh_model_combo()
    assert window.ai_account.provider == "deepseek"
    window._session_model = None
    window._refresh_model_combo()
    assert window.ai_account.provider == providers.provider_of(window._default_model())
