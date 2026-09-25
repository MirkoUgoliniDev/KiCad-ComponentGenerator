"""@brief Offline checks of the OpenRouter hardening of 2026-09-25 (audit D8, D12, D13).

No network: `urlopen` is always replaced, and the model cache lives in tmp_path.
"""
import io
import json
import urllib.error

import pytest

from kicadgen.services import openrouter as client

_KEY = "test-key-without-a-recognizable-prefix"


def _http_error(status, message="provider said no", headers=None):
    body = json.dumps({"error": {"message": message, "code": status}}).encode()
    return urllib.error.HTTPError("https://example.invalid", status, "error",
                                  headers or {}, io.BytesIO(body))


def _call_with(monkeypatch, error):
    monkeypatch.setenv(client.API_KEY_ENV, _KEY)

    def urlopen(*args, **kwargs):
        raise error
    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    with pytest.raises(client.OpenRouterError) as caught:
        client.call_tool("vendor/model", "prompt", [], {"function": {"name": "report"}})
    return caught.value


# ── D8: which failures may be retried, and how ─────────────────────────────

def test_a_plain_400_is_neither_retryable_nor_a_compatibility_failure(monkeypatch):
    error = _call_with(monkeypatch, _http_error(400, "invalid schema"))
    assert error.retryable is False and error.compatibility is False


def test_a_400_about_tool_choice_asks_for_the_compatibility_form(monkeypatch):
    error = _call_with(monkeypatch, _http_error(400, "tool_choice is not supported"))
    assert error.retryable is False and error.compatibility is True


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_statuses_are_retryable_with_retry_after(monkeypatch, status):
    error = _call_with(monkeypatch, _http_error(status, headers={"Retry-After": "12"}))
    assert error.retryable is True and error.compatibility is False
    assert error.retry_after == 12


def test_a_malformed_retry_after_is_ignored(monkeypatch):
    error = _call_with(monkeypatch, _http_error(429, headers={"Retry-After": "soon"}))
    assert error.retryable is True and error.retry_after is None


def test_connection_errors_are_translated_messages(monkeypatch):
    error = _call_with(monkeypatch, urllib.error.URLError("timed out"))
    assert str(error).startswith("Connessione a OpenRouter fallita: timed out")


# ── D13: a negative cost is a missing cost, not a refund ───────────────────

def test_negative_or_non_finite_cost_counts_as_missing():
    client.reset_usage()
    client._record_usage({"usage": {"cost": 0.5}})
    client._record_usage({"usage": {"cost": -0.3}})
    client._record_usage({"usage": {"cost": float("inf")}})
    totals = client.usage_totals()
    assert totals.cost_usd == pytest.approx(0.5)
    assert totals.calls == 3 and totals.calls_without_cost == 2
    client.reset_usage()


def test_call_plural_is_translated_per_count():
    assert client._calls(1) == "1 chiamata" and client._calls(3) == "3 chiamate"


# ── D12: model list robustness ─────────────────────────────────────────────

@pytest.fixture
def cache(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "dir" / ".openrouter_models.json"
    monkeypatch.setattr(client, "MODELS_CACHE_PATH", path)
    return path


def _serve(monkeypatch, payload):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda *a, **k: Response(json.dumps(payload).encode()))


GOOD = {"id": "vendor/good", "name": "Good", "context_length": 8000,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "architecture": {"input_modalities": ["text", "image"]},
        "supported_parameters": ["tools"]}


def test_invalid_items_are_skipped_and_valid_ones_kept(monkeypatch, cache):
    _serve(monkeypatch, {"data": [
        "not a model", 42, None,
        {"id": 7}, {"id": "vendor/odd", "context_length": "lots", "pricing": "free",
                    "architecture": [], "supported_parameters": "tools"},
        GOOD]})
    models = {m.id: m for m in client.list_models(force_refresh=True)}
    assert set(models) == {"vendor/good", "vendor/odd"}
    assert models["vendor/odd"].context_length == 0
    assert models["vendor/odd"].supports_tools is False
    assert models["vendor/good"].supports_images and models["vendor/good"].supports_tools
    # The parent directory is created and the write is atomic (no temp left).
    assert cache.is_file() and not list(cache.parent.glob("*.tmp"))


def test_a_top_level_list_payload_is_accepted(monkeypatch, cache):
    _serve(monkeypatch, [GOOD])
    assert [m.id for m in client.list_models(force_refresh=True)] == ["vendor/good"]


def test_a_corrupt_dict_cache_is_ignored_not_fatal(monkeypatch, cache):
    """Negative: a cache holding a dict of the wrong shape triggers a download."""
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"unexpected": True}), encoding="utf-8")
    _serve(monkeypatch, {"data": [GOOD]})
    assert [m.id for m in client.list_models()] == ["vendor/good"]


def test_an_unusable_payload_falls_back_to_the_expired_cache(monkeypatch, cache):
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps([GOOD]), encoding="utf-8")
    _serve(monkeypatch, {"data": "garbage"})
    assert [m.id for m in client.list_models(force_refresh=True)] == ["vendor/good"]
