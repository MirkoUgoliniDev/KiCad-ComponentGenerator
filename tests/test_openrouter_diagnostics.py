"""@brief Verify safe provider failures and explicit tool-argument semantics offline."""

import io
import json
import traceback
import urllib.error

import pytest

from kicadgen.services import openrouter as client


_MISSING = object()
_PRIVATE = "PRIVATE_PROMPT_DO_NOT_LOG"
_KEY = "test-key-without-a-recognizable-prefix"


def response(arguments=_MISSING, reason="tool_calls"):
    function = {"name": "report"}
    if arguments is not _MISSING:
        function["arguments"] = arguments
    return {"choices": [{"finish_reason": reason, "native_finish_reason": "max_tokens",
                         "message": {"content": _PRIVATE, "tool_calls": [{"function": function}]}}]}


@pytest.mark.parametrize("arguments", [_MISSING, None, "", " ", "\n\t"])
def test_missing_or_empty_arguments_are_failures_not_empty_objects(arguments):
    with pytest.raises(client.OpenRouterError, match="assenti o vuoti") as caught:
        client.tool_arguments(response(arguments, reason="length"), "report")
    assert "finish_reason=length" in str(caught.value)
    assert "native_finish_reason=max_tokens" in str(caught.value)
    assert _PRIVATE not in str(caught.value)


@pytest.mark.parametrize("arguments", [{}, "{}", " \n{} \t"])
def test_explicit_empty_object_remains_valid(arguments):
    result = client.tool_arguments(response(arguments), "report")
    assert result == {}
    if isinstance(arguments, dict):
        assert result is arguments


@pytest.mark.parametrize("arguments", [[], False, 0, "[]", "null", "false", "123", '"text"', "{broken"])
def test_nonobject_or_invalid_json_arguments_fail_safely(arguments):
    with pytest.raises(client.OpenRouterError, match="JSON valido") as caught:
        client.tool_arguments(response(arguments), "report")
    assert "finish_reason=tool_calls" in str(caught.value)
    assert _PRIVATE not in str(caught.value)


@pytest.mark.parametrize("payload", [None, [], {"choices": "private"}, {"choices": [None]},
                                     {"choices": [{"message": "private"}]},
                                     {"choices": [{"message": {"tool_calls": [None, {"function": 42}]}}]}])
def test_malformed_envelopes_raise_safe_domain_errors(payload):
    with pytest.raises(client.OpenRouterError) as caught:
        client.tool_arguments(payload, "report")
    assert "provider_code=unknown" in str(caught.value)
    assert "finish_reason=unknown" in str(caught.value)
    assert "private" not in str(caught.value)


def test_provider_error_reports_allowlisted_context_without_echoing_text(monkeypatch):
    monkeypatch.setenv(client.API_KEY_ENV, _KEY)
    payload = response(reason="length")
    payload["error"] = {
        "code": 429,
        "message": f"rate limit {_KEY} Authorization: Bearer OTHER_KEY {_PRIVATE} data:image/png;base64,IMAGE",
        "metadata": {"raw": {"prompt": _PRIVATE}, "provider_name": _PRIVATE},
    }
    with pytest.raises(client.OpenRouterError) as caught:
        client.tool_arguments(payload, "report")
    message = str(caught.value)
    assert "rate limit" in message
    assert "provider_code=429" in message and "finish_reason=length" in message
    for private in (_KEY, "OTHER_KEY", _PRIVATE, "IMAGE", "Authorization", "data:image"):
        assert private not in message
    assert len(message) < 200


@pytest.mark.parametrize("code,expected", [(402, "402"), ("429", "429"),
    ("rate_limit_exceeded", "rate_limit_exceeded"), (_PRIVATE, "unknown"),
    ({"secret": _PRIVATE}, "unknown"), (True, "unknown")])
def test_provider_codes_are_bounded_and_allowlisted(code, expected):
    payload = {"error": {"message": _PRIVATE, "code": code},
               "choices": [{"finish_reason": _PRIVATE, "native_finish_reason": _PRIVATE}]}
    with pytest.raises(client.OpenRouterError) as caught:
        client.tool_arguments(payload, "report")
    assert f"provider_code={expected}" in str(caught.value)
    assert "finish_reason=unknown" in str(caught.value)
    assert _PRIVATE not in str(caught.value)


def test_metadata_code_is_safe_but_raw_provider_payload_is_never_logged():
    payload = {"error": {"message": {"prompt": _PRIVATE},
                         "metadata": {"code": "invalid_request_error", "raw": _PRIVATE}}}
    with pytest.raises(client.OpenRouterError) as caught:
        client.tool_arguments(payload, "report")
    assert "provider_code=invalid_request_error" in str(caught.value)
    assert _PRIVATE not in str(caught.value)


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b'{"error": "PRIVATE"}',
    json.dumps({"error": {"message": _PRIVATE + "x" * 100000}}).encode()],
    ids=["invalid-json", "array", "null", "string-error", "oversized"])
def test_http_error_body_and_reason_are_not_echoed(body):
    error = urllib.error.HTTPError("https://example.invalid", 502, _PRIVATE, {}, io.BytesIO(body))
    text = client._error_message(error)
    assert "HTTP 502" in text and "provider_code=502" in text
    assert _PRIVATE not in text and "PRIVATE" not in text
    assert len(text) < 200


@pytest.mark.parametrize("endpoint", ["call", "credit", "models"])
@pytest.mark.parametrize("failure", ["http", "network", "timeout", "decode"])
def test_network_boundaries_never_leak_and_never_retry(monkeypatch, endpoint, failure):
    monkeypatch.setenv(client.API_KEY_ENV, _KEY)
    calls = []
    raw = f"Authorization: Bearer OTHER_KEY {_KEY} {_PRIVATE} data:image/png;base64,PRIVATE_IMAGE"
    errors = {
        "http": urllib.error.HTTPError("https://example.invalid", 401, raw, {},
            io.BytesIO(json.dumps({"error": {"message": raw, "code": 401}}).encode())),
        "network": urllib.error.URLError(raw),
        "timeout": TimeoutError(raw),
        "decode": ValueError(raw),
    }

    def urlopen(*args, **kwargs):
        calls.append(1)
        raise errors[failure]

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "_load_models_cache", lambda **kwargs: None)
    with pytest.raises(client.OpenRouterError) as caught:
        if endpoint == "call":
            client.call_tool("vendor/model", _PRIVATE, [], {"function": {"name": "report"}})
        elif endpoint == "credit":
            client._get_json(client.CREDITS_URL, 1)
        else:
            client.list_models(force_refresh=True)
    assert len(calls) == 1
    formatted = "".join(traceback.format_exception(caught.value))
    for private in (_KEY, _PRIVATE, "OTHER_KEY", "PRIVATE_IMAGE"):
        assert private not in str(caught.value) and private not in formatted
    assert "provider_code=" in str(caught.value) and "finish_reason=" in str(caught.value)


def test_missing_args_response_is_counted_once_and_never_retried(monkeypatch):
    monkeypatch.setenv(client.API_KEY_ENV, _KEY)
    calls, recorded = [], []
    payload = response(reason="length")

    def urlopen(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "_record_usage", recorded.append)
    with pytest.raises(client.OpenRouterError, match="assenti o vuoti"):
        client.call_tool("vendor/model", _PRIVATE, [], {"function": {"name": "report"}})
    assert calls == [1] and recorded == [payload]


def test_nested_provider_image_limit_is_reported_without_private_text():
    raw = {"error": {"message": "Provider returned error", "code": 400,
        "metadata": {"raw": json.dumps({"error": {"message":
            "image exceeds 5 MB maximum: " + _PRIVATE}})}}}
    error = urllib.error.HTTPError("https://example.invalid", 400, "Bad Request", {},
        io.BytesIO(json.dumps(raw).encode()))
    message = client._error_message(error)
    assert "image exceeds" in message and "5 MB" in message
    assert _PRIVATE not in message


def test_legacy_credit_error_substring_is_preserved():
    with pytest.raises(client.OpenRouterError, match="credito esaurito"):
        client.tool_arguments({"error": {"message": "credito esaurito"}}, "report")


@pytest.mark.parametrize("message", [None, {}, {"content": _PRIVATE},
    {"tool_calls": []}, {"tool_calls": None},
    {"tool_calls": [{"function": {"name": "other", "arguments": "{}"}}]}])
def test_length_without_requested_tool_reports_truncation_not_model_incompatibility(message):
    payload = {"choices": [{"finish_reason": "length", "native_finish_reason": "max_tokens",
                            "message": message}]}
    with pytest.raises(client.OpenRouterError, match="Risposta troncata") as caught:
        client.tool_arguments(payload, "report")
    text = str(caught.value)
    assert "budget di token di completamento esaurito" in text
    assert "finish_reason=length" in text and "native_finish_reason=max_tokens" in text
    assert "non supportare" not in text and "scegline un altro" not in text
    assert _PRIVATE not in text


def test_length_with_complete_explicit_tool_object_still_returns_it():
    assert client.tool_arguments(response("{}", reason="length"), "report") == {}


def test_other_finish_reason_keeps_existing_no_tool_diagnostic():
    with pytest.raises(client.OpenRouterError, match="Il modello non ha chiamato lo strumento"):
        client.tool_arguments({"choices": [{"finish_reason": "stop", "message": {}}]}, "report")


def test_reasoning_exhausted_response_counts_usage_once_without_retry(monkeypatch):
    monkeypatch.setenv(client.API_KEY_ENV, _KEY)
    calls = []
    payload = {"choices": [{"finish_reason": "length", "message": {"content": ""}}],
               "usage": {"completion_tokens": 3934, "prompt_tokens": 100,
                         "completion_tokens_details": {"reasoning_tokens": 3934}, "cost": 0.01}}

    def urlopen(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    client.reset_usage()
    try:
        with pytest.raises(client.OpenRouterError, match="budget di token"):
            client.call_tool("vendor/reasoning-model", _PRIVATE, [], {"function": {"name": "report"}})
        totals = client.usage_totals()
        assert calls == [1] and totals.calls == 1
        assert totals.completion_tokens == totals.reasoning_tokens == 3934
        assert totals.cost_usd == pytest.approx(0.01)
    finally:
        client.reset_usage()
