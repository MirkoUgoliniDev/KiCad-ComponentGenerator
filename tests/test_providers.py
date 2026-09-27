"""@brief DeepSeek's own API as a second provider (user, 2026-09-27), offline."""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from kicadgen.services import openrouter, providers


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

def test_the_picker_starts_on_the_provider_of_the_current_model(qapp, monkeypatch):
    from kicadgen.gui import model_dialog
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


def test_the_balance_line_follows_the_provider(qapp, monkeypatch):
    from kicadgen.gui.ai_account import AiAccountLine
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


def test_the_balance_line_asks_for_the_provider_at_every_refresh(qapp):
    """User, 27/09: after a DeepSeek run the line still said «Saldo OpenRouter».

    The model can change through paths that emit no event (project restore):
    the line reads the provider of the model in use at every refresh.
    """
    from kicadgen.gui.ai_account import AiAccountLine
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




def test_restoring_a_project_model_moves_the_balance_to_its_provider(window):
    """User, 27/09: a project reopened with a DeepSeek session model still showed
    «Saldo OpenRouter». The restore path goes through _refresh_model_combo."""
    window._session_model = "deepseek-api/deepseek-flash"     # as the restore does
    window._refresh_model_combo()
    assert window.ai_account.provider == "deepseek"
    window._session_model = None
    window._refresh_model_combo()
    assert window.ai_account.provider == providers.provider_of(window._default_model())


# ── DeepSeek cost from the balance (user, 27/09: «il costo non e' 1.05 USD») ──

def _records(tmp_path, *records):
    from kicadgen.services import project_costs as pc
    for record in records:
        pc.add_call(tmp_path, record)
    return pc


# ── DeepSeek cost = tokens x official price (HANDOFF 128) ───────────────

from datetime import datetime, timezone  # noqa: E402

from kicadgen.services import deepseek_pricing  # noqa: E402

SUNDAY = datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc)      # off-peak all day
MONDAY_PEAK = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)   # 06-10 UTC
MONDAY_OFF = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def test_the_cost_is_tokens_times_the_official_price():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert deepseek_pricing.call_cost("deepseek-flash", usage, SUNDAY) == pytest.approx(0.75)
    assert deepseek_pricing.call_cost("deepseek-v4-pro", usage, SUNDAY) == pytest.approx(2.64)


def test_peak_hours_cost_twice_and_only_on_weekdays():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert deepseek_pricing.call_cost("deepseek-flash", usage, MONDAY_PEAK) == pytest.approx(0.30)
    assert deepseek_pricing.call_cost("deepseek-flash", usage, MONDAY_OFF) == pytest.approx(0.15)
    assert not deepseek_pricing.is_peak(datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 1_000_000, "completion_tokens": 0, "prompt_cache_hit_tokens": 400_000},
    {"prompt_tokens": 1_000_000, "completion_tokens": 0,
     "prompt_tokens_details": {"prompt_cache_hit_tokens": 400_000}},
    {"prompt_tokens": 1_000_000, "completion_tokens": 0,
     "prompt_tokens_details": {"cached_tokens": 400_000}},
])
def test_cache_hits_are_read_wherever_deepseek_puts_them(usage):
    """Le docs mostrano i token in cache in cima a usage o sotto prompt_tokens_details."""
    expected = (400_000 * 0.003 + 600_000 * 0.15) / 1e6
    assert deepseek_pricing.call_cost("deepseek-flash", usage, SUNDAY) == pytest.approx(expected)


def test_an_unknown_model_gets_no_cost_and_an_alias_gets_its_price():
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    assert deepseek_pricing.call_cost("deepseek-something-new", usage, SUNDAY) is None
    assert deepseek_pricing.call_cost("deepseek-v4-flash", usage, SUNDAY) == \
        deepseek_pricing.call_cost("deepseek-flash", usage, SUNDAY)
    assert deepseek_pricing.call_cost("deepseek-flash", {}, SUNDAY) is None


def test_a_deepseek_call_is_recorded_with_its_computed_cost(monkeypatch):
    sent = []
    monkeypatch.setattr(openrouter, "_usage_listener", sent.append)
    monkeypatch.setattr(providers, "deepseek_request", lambda *a, **k: {
        "model": "deepseek-flash",
        "usage": {"prompt_tokens": 10_000, "completion_tokens": 1_000},
        "choices": [{"message": {"tool_calls": [{"function": {
            "name": "report", "arguments": "{}"}}]}}]})
    openrouter.call_tool("deepseek-api/deepseek-flash", "s", [], _TOOL)
    expected = deepseek_pricing.call_cost(
        "deepseek-flash", {"prompt_tokens": 10_000, "completion_tokens": 1_000})
    assert sent[0]["cost_usd"] == pytest.approx(expected) and sent[0]["cost_usd"] > 0
    assert sent[0]["cost_source"] == "token_price"


def _cost_file(tmp_path, entries, total=0.0, missing=None):
    import json
    from kicadgen.services import project_costs as pc
    (tmp_path / pc.COSTS_FILE).write_text(json.dumps({
        "format": pc.FORMAT, "version": 1, "since": "2026-09-27T08:00:00+00:00",
        "total_usd": total, "calls": len(entries),
        "calls_without_cost": sum(e.get("cost_usd") is None for e in entries)
        if missing is None else missing, "entries": entries}), encoding="utf-8")
    return pc


def test_calls_recorded_without_a_cost_are_costed_from_their_tokens(tmp_path):
    """Il caso RS485: chiamate DeepSeek registrate senza costo (o a 0.0 dal
    saldo) prendono il costo dai loro token e dall'ora; nessuna resta senza."""
    pc = _cost_file(tmp_path, [
        {"time": "2026-09-27T08:26:46+00:00", "model": "anthropic/claude-opus-5",
         "cost_usd": 0.25, "prompt_tokens": 1, "completion_tokens": 1},
        {"time": "2026-09-27T12:29:41+00:00", "model": "deepseek-flash", "cost_usd": None,
         "prompt_tokens": 11522, "completion_tokens": 2344},
        {"time": "2026-09-27T12:58:03+00:00", "model": "deepseek-api/deepseek-flash",
         "cost_usd": 0.0, "cost_source": "balance_delta",
         "prompt_tokens": 11522, "completion_tokens": 4737}], total=0.25)
    costs = pc.load_project_costs(tmp_path)
    expected = ((11522 + 11522) * 0.15 + (2344 + 4737) * 0.60) / 1e6
    assert costs.complete
    assert costs.by_provider["deepseek"] == pytest.approx((expected, 2, 0))
    assert costs.by_provider["openrouter"] == (0.25, 1, 0)
    assert costs.total_usd == pytest.approx(0.25 + expected)


def test_costed_calls_are_left_as_they_are(tmp_path):
    """Negativo: una chiamata DeepSeek con il costo gia' calcolato non si ricalcola,
    e una chiamata OpenRouter senza costo resta senza costo."""
    pc = _cost_file(tmp_path, [
        {"time": "2026-09-28T07:00:00+00:00", "model": "deepseek-api/deepseek-flash",
         "cost_usd": 0.123, "cost_source": "token_price",
         "prompt_tokens": 10, "completion_tokens": 10},
        {"time": "2026-09-27T12:00:00+00:00", "model": "anthropic/claude-opus-5",
         "cost_usd": None, "prompt_tokens": 10, "completion_tokens": 10}], total=0.123)
    costs = pc.load_project_costs(tmp_path)
    assert costs.total_usd == pytest.approx(0.123) and costs.calls_without_cost == 1


def test_pricing_old_calls_is_stable_across_reads_and_writes(tmp_path):
    pc = _cost_file(tmp_path, [
        {"time": "2026-09-27T12:29:41+00:00", "model": "deepseek-flash", "cost_usd": None,
         "prompt_tokens": 1000, "completion_tokens": 1000}])
    first = pc.load_project_costs(tmp_path)
    after_write = pc.add_call(tmp_path, {"model": "anthropic/claude-opus-5", "cost_usd": 0.1})
    again = pc.load_project_costs(tmp_path)
    assert after_write.total_usd == pytest.approx(first.total_usd + 0.1) == again.total_usd
    assert again.calls_without_cost == 0


def test_a_deepseek_call_is_recorded_with_its_full_identifier(monkeypatch):
    sent = []
    monkeypatch.setattr(openrouter, "_usage_listener", sent.append)
    monkeypatch.setattr(providers, "deepseek_request", lambda *a, **k: {
        "model": "deepseek-flash",
        "choices": [{"message": {"tool_calls": [{"function": {
            "name": "report", "arguments": "{}"}}]}}]})
    openrouter.call_tool("deepseek-api/deepseek-flash", "s", [], _TOOL)
    assert sent[0]["model"] == "deepseek-api/deepseek-flash"


def test_the_credit_line_splits_the_project_cost_by_provider(qapp, tmp_path):
    """Utente, 27/09: «Costo del progetto: almeno 1.05 USD» accanto al saldo
    DeepSeek sembrava la spesa DeepSeek. La riga dice il totale e la parte di
    ciascun fornitore; con un solo fornitore resta il solo totale."""
    from kicadgen.gui.ai_account import AiAccountLine
    _cost_file(tmp_path, [
        {"time": "2026-09-27T08:26:46+00:00", "model": "anthropic/claude-opus-5",
         "cost_usd": 1.05, "prompt_tokens": 1, "completion_tokens": 1},
        {"time": "2026-09-27T12:29:41+00:00", "model": "deepseek-api/deepseek-flash",
         "cost_usd": 0.05, "cost_source": "token_price",
         "prompt_tokens": 1, "completion_tokens": 1}], total=1.10)
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    try:
        line.refresh()
        text = line.text()
        assert "Costo del progetto: 1.10 USD" in text and "almeno" not in text
        assert "DeepSeek" in text and "OpenRouter" in text and "0.05" in text
    finally:
        line.detach()
    only = tmp_path / "solo"
    only.mkdir()
    _cost_file(only, [{"time": "2026-09-27T08:26:46+00:00", "model": "anthropic/claude-opus-5",
                       "cost_usd": 1.05, "prompt_tokens": 1, "completion_tokens": 1}], total=1.05)
    line = AiAccountLine(lambda: only, lambda _m: None)
    try:
        line.refresh()
        assert "(" not in line.text().split("Costo del progetto:")[1]
    finally:
        line.detach()


def test_a_call_with_no_reported_tokens_stays_without_cost(tmp_path):
    """Revisione 128: zero token = consumo non riportato, non una chiamata
    gratuita; prezzarla trasformava un costo ignoto in uno zero noto."""
    pc = _cost_file(tmp_path, [
        {"time": "2026-09-27T12:00:00+00:00", "model": "deepseek-api/deepseek-flash",
         "cost_usd": None, "prompt_tokens": 0, "completion_tokens": 0}])
    costs = pc.load_project_costs(tmp_path)
    assert costs.calls_without_cost == 1 and not costs.complete


@pytest.mark.parametrize("damage", [
    {"prompt_tokens": 10 ** 400},
    {"time": "0001-01-01T00:30:00+01:00"},
    {"time": "not a time"},
])
def test_a_damaged_entry_is_left_without_cost_never_fatal(tmp_path, damage):
    entry = {"time": "2026-09-27T12:00:00+00:00", "model": "deepseek-api/deepseek-flash",
             "cost_usd": None, "prompt_tokens": 100, "completion_tokens": 100}
    entry.update(damage)
    pc = _cost_file(tmp_path, [entry])
    assert pc.load_project_costs(tmp_path).calls_without_cost == 1


def test_estimated_calls_are_counted_for_the_tooltip(tmp_path):
    pc = _cost_file(tmp_path, [
        {"time": "2026-09-27T12:29:41+00:00", "model": "deepseek-flash", "cost_usd": None,
         "prompt_tokens": 1000, "completion_tokens": 1000},
        {"time": "2026-09-27T12:30:00+00:00", "model": "deepseek-api/deepseek-flash",
         "cost_usd": 0.001, "cost_source": "token_price",
         "prompt_tokens": 1000, "completion_tokens": 1000}], total=0.001)
    assert pc.load_project_costs(tmp_path).estimated_calls == 1


# ── Chiave DeepSeek: ambiente, Gestore credenziali, .env (HANDOFF 129) ───

def test_the_deepseek_key_is_looked_up_like_the_openrouter_one(monkeypatch):
    import sys
    from kicadgen.services import credentials
    monkeypatch.setattr(sys, "platform", "win32")
    path = providers.env_files()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{providers.DEEPSEEK_KEY_ENV}=sk-dal-file\n", encoding="utf-8")
    assert providers.deepseek_api_key() == "sk-dal-file"
    assert providers.deepseek_key_source().kind == openrouter.KEY_SOURCE_DOTENV
    credentials.write_secret(credentials.DEEPSEEK_TARGET, "sk-dal-gestore")
    assert providers.deepseek_api_key() == "sk-dal-gestore"          # outranks .env
    assert providers.deepseek_key_source().kind == openrouter.KEY_SOURCE_CREDENTIAL_MANAGER
    monkeypatch.setenv(providers.DEEPSEEK_KEY_ENV, "sk-da-ambiente")
    assert providers.deepseek_api_key() == "sk-da-ambiente"          # outranks both
    source = providers.deepseek_key_source()
    assert source.kind == openrouter.KEY_SOURCE_ENVIRONMENT and source.stored


def test_a_missing_deepseek_key_points_to_the_account_window():
    with pytest.raises(providers.ProviderKeyMissing, match="Account AI"):
        providers.deepseek_api_key()


def test_a_pasted_key_is_verified_with_itself_and_a_refusal_is_recognised(monkeypatch):
    import io
    import urllib.error
    seen = []

    def refuse(request, timeout):
        seen.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error": {"message": "bad key sk-x"}}'))
    monkeypatch.setattr(providers.urllib.request, "urlopen", refuse)
    with pytest.raises(providers.ProviderError) as caught:
        providers.verify_deepseek_key("sk-x")
    assert seen == ["Bearer sk-x"]
    assert providers.deepseek_key_rejected(caught.value)
    assert "sk-x" not in str(caught.value)                          # redacted
    assert not providers.deepseek_key_rejected(providers.ProviderError("timeout"))



def test_the_escaped_form_of_the_key_is_redacted_too():
    key = "sk-SECRET1\nSECRET2"
    text = f"Invalid header value b'Bearer {key!r}'"
    assert "SECRET" not in providers._redact(text, key)


def test_a_key_with_blanks_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(providers.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("request sent"))
    for bad in ("sk-a\nb", "sk-a b", "sk-\u00e8", ""):
        with pytest.raises(providers.ProviderError):
            providers.verify_deepseek_key(bad)
    assert providers.key_text_problem("sk-abc123") is None


# ── transient DeepSeek failures are retried like OpenRouter's ──────────────

def _deepseek_http_failures(monkeypatch, status, headers=None):
    """Make every DeepSeek request fail with `status`; return the attempt counter."""
    import email.message
    import io
    import urllib.error

    from kicadgen.services import ai_diagnostics
    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(request.full_url)
        message = email.message.Message()
        for name, value in (headers or {}).items():
            message[name] = value
        raise urllib.error.HTTPError(request.full_url, status, "err", message,
                                     io.BytesIO(b'{"error": {"message": "x"}}'))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    waits = []
    monkeypatch.setattr(ai_diagnostics, "_sleep", waits.append)
    call = ai_diagnostics.make_logged_call(
        lambda: providers.deepseek_request("https://deepseek.invalid/x", {"a": 1}, key="sk-t"),
        lambda *_a, **_k: None)
    with pytest.raises(providers.ProviderError) as info:
        call()
    return attempts, waits, info.value


def test_a_deepseek_rate_limit_is_retried_with_the_requested_wait(monkeypatch):
    attempts, waits, error = _deepseek_http_failures(monkeypatch, 429, {"Retry-After": "7"})
    assert len(attempts) == 3
    assert error.retryable is True and error.retry_after == 7.0
    assert sum(waits) == pytest.approx(14.0)


def test_a_deepseek_server_error_without_retry_after_is_retried(monkeypatch):
    attempts, _waits, error = _deepseek_http_failures(monkeypatch, 503)
    assert len(attempts) == 3
    assert error.retryable is True and error.retry_after is None


@pytest.mark.parametrize("status", [400, 401])
def test_a_deepseek_request_error_is_not_retried(monkeypatch, status):
    attempts, _waits, error = _deepseek_http_failures(monkeypatch, status, {"Retry-After": "7"})
    assert len(attempts) == 1
    assert error.retryable is False and error.retry_after is None
    assert error.http_status == status


def test_an_unreachable_deepseek_is_not_retried_like_openrouter(monkeypatch):
    import urllib.error

    from kicadgen.services import ai_diagnostics
    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ai_diagnostics, "_sleep", lambda s: None)
    call = ai_diagnostics.make_logged_call(
        lambda: providers.deepseek_request("https://deepseek.invalid/x", key="sk-t"),
        lambda *_a, **_k: None)
    with pytest.raises(providers.ProviderError) as info:
        call()
    assert len(attempts) == 1
    assert info.value.unreachable is True and info.value.retryable is False


# ── block C: S4, S6, S7, S10 ───────────────────────────────────────────────

def _http_error(body: bytes, status=400):
    """An HTTPError whose body records how many bytes were asked for."""
    import email.message
    import io
    import urllib.error

    class Body(io.BytesIO):
        asked = []

        def read(self, size=-1):
            Body.asked.append(size)
            return super().read(size)
    stream = Body(body)
    return urllib.error.HTTPError("https://deepseek.invalid", status, "err",
                                  email.message.Message(), stream), Body.asked


def _deepseek_failing_with(monkeypatch, error):
    def fake_urlopen(request, timeout=None):
        raise error
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(providers.ProviderError) as info:
        providers.deepseek_request("https://deepseek.invalid/x", {"a": 1}, key="sk-test-key-1")
    return str(info.value)


def test_a_deepseek_error_shows_a_fixed_category_never_the_server_text(monkeypatch):
    """@brief S4: an echoed prompt never reaches the error; a known category does.

    Positive: «rate limit» in the server message becomes that category.
    Negative: the free text around it (a private prompt echo) is dropped, and
    the body is read with the 64 KB bound, not whole.
    """
    import json
    body = json.dumps({"error": {"message": "Rate limit reached for PRIVATE PROMPT text"}}).encode()
    error, asked = _http_error(body, 429)
    text = _deepseek_failing_with(monkeypatch, error)
    assert "rate limit" in text and "PRIVATE PROMPT" not in text
    assert asked == [providers._MAX_ERROR_BODY_BYTES]

    error, _asked = _http_error(json.dumps({"error": {"message": "secret echo"}}).encode())
    text = _deepseek_failing_with(monkeypatch, error)
    assert "secret echo" not in text and "HTTP 400" in text


def test_a_non_finite_price_is_unknown_never_free():
    """@brief S6: "nan"/"inf" prices are unknown; ordinary prices are unchanged."""
    import math
    assert math.isnan(openrouter._parse_price("nan"))
    assert math.isnan(openrouter._parse_price("inf"))
    assert openrouter._parse_price("0") == 0.0
    model = openrouter._model_from_payload({"id": "v/m", "pricing": {"prompt": "nan",
                                                                     "completion": "nan"}})
    assert not model.is_free


def test_models_with_unknown_prices_are_listed_after_the_priced_ones(monkeypatch, tmp_path):
    """@brief S6: NaN prices do not scramble the sort by cost."""
    import io
    import json

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    rows = [{"id": "v/unknown", "pricing": {"prompt": "nan", "completion": "nan"}},
            {"id": "v/dear", "pricing": {"prompt": "0.00001", "completion": "0.00001"}},
            {"id": "v/cheap", "pricing": {"prompt": "0.000001", "completion": "0.000001"}}]
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", tmp_path / "models.json")
    monkeypatch.setattr(openrouter.urllib.request, "urlopen",
                        lambda *a, **k: Response(json.dumps({"data": rows}).encode()))
    assert [m.id for m in openrouter.list_models(force_refresh=True)] == [
        "v/cheap", "v/dear", "v/unknown"]


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("false", False),
                                             ("true", False), (1, False), (None, False)])
def test_the_deepseek_account_is_available_only_on_a_json_true(monkeypatch, value, expected):
    """@brief S6: a malformed availability never unlocks a paid run."""
    monkeypatch.setattr(providers, "deepseek_request", lambda url, *a, **k: {
        "is_available": value, "balance_infos": []})
    assert providers.deepseek_balance().available is expected


@pytest.mark.parametrize("usage, cached", [
    ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 40}, 40),                  # DeepSeek
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 30}}, 30),  # OpenRouter
    ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 40,
      "prompt_tokens_details": {"cached_tokens": 40}}, 40),                        # both: once
    ({"prompt_tokens": 100}, 0),
])
def test_cached_tokens_are_counted_once_wherever_they_are_reported(usage, cached):
    """@brief S7: DeepSeek's top-level cache hits reach the run total, never twice."""
    openrouter.reset_usage()
    openrouter._record_usage({"usage": usage})
    assert openrouter.usage_totals().cached_tokens == cached
    openrouter.reset_usage()


def test_a_deepseek_answer_without_result_names_deepseek(monkeypatch):
    """@brief S7: the error of an empty DeepSeek answer names DeepSeek, not OpenRouter."""
    monkeypatch.setattr(providers, "deepseek_request", lambda *a, **k: {"choices": []})
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter.call_tool("deepseek-api/deepseek-flash", "sys", [], _TOOL)
    assert "DeepSeek" in str(info.value) and "OpenRouter" not in str(info.value)
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter.tool_arguments({"choices": []}, "report")
    assert "OpenRouter" in str(info.value)
    openrouter.reset_usage()


def test_every_models_cache_write_uses_its_own_temporary_and_the_retrying_replace(
        monkeypatch, tmp_path):
    """@brief S10: two writes never share a temporary; publishing goes through replace_file.

    Negative: no temporary file is left behind.
    """
    import os
    target = tmp_path / "models.json"
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", target)
    sources = []

    def recording_replace(source, destination):
        sources.append(str(source))
        os.replace(source, destination)
    monkeypatch.setattr(openrouter, "replace_file", recording_replace)
    openrouter._write_models_cache([{"id": "a"}])
    openrouter._write_models_cache([{"id": "b"}])
    assert len(sources) == 2 and sources[0] != sources[1]
    assert openrouter._load_models_cache() == [{"id": "b"}]
    assert [p.name for p in tmp_path.iterdir()] == ["models.json"]
