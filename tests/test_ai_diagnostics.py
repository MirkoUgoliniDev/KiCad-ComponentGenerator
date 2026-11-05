"""@brief Verify private-data exclusion and transparent AI diagnostic wrapping."""

import inspect

import pytest

from kicadgen.services import ai_diagnostics as diagnostics
from kicadgen.services.openrouter import OpenRouterError


def test_start_is_live_and_success_preserves_arguments_result_and_timing(monkeypatch):
    events = []
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_IMAGE"}}]
    tool = {"type": "function", "function": {"name": "report_parts", "description": "PRIVATE_SCHEMA"}}
    result = {"components": [{"label": "PRIVATE_RESULT"}] * 3, "pins": [1, 2], "PRIVATE_KEY": "secret"}
    ticks = iter([100.0, 102.125])
    monkeypatch.setattr(diagnostics, "perf_counter", lambda: next(ticks))

    def call(*, model, system_prompt, content_blocks, tool, **kwargs):
        assert len(events) == 1 and events[0].startswith("AI_CALL_START ")
        assert model == "provider/model:free"
        assert system_prompt == "PRIVATE_PROMPT"
        assert content_blocks is content and tool is original_tool
        assert kwargs == {"api_key": "sk-private-key", "headers": {"Authorization": "PRIVATE_HEADER"}}
        return result

    original_tool = tool
    wrapped = diagnostics.make_logged_call(call, events.append)
    returned = wrapped(model="provider/model:free", system_prompt="PRIVATE_PROMPT",
                       content_blocks=content, tool=tool, api_key="sk-private-key",
                       headers={"Authorization": "PRIVATE_HEADER"})
    assert returned is result
    assert len(events) == 2
    assert events[1].startswith("AI_CALL_END ")
    assert "model=provider/model:free tool=report_parts" in events[1]
    assert "elapsed=2.125s result=object fields=3 components=3 pins=2" in events[1]
    assert "PRIVATE" not in "\n".join(events) and "sk-private-key" not in "\n".join(events)
    assert wrapped.__wrapped__ is call
    assert inspect.signature(wrapped) == inspect.signature(call)


def test_opt_in_prompt_audit_logs_text_and_schema_but_not_image_bytes():
    events = []
    content = [
        {"type": "text", "text": "FULL_USER_PROMPT with dimensions 18.0 x 19.2 mm"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_IMAGE_BYTES", "detail": "high"}},
    ]
    tool = {"type": "function", "function": {"name": "report_global_inventory",
        "description": "FULL_TOOL_SCHEMA", "parameters": {"type": "object"}}}

    def call(**kwargs):
        return {"components": []}

    wrapped = diagnostics.make_logged_call(call, events.append, log_prompt=True)
    wrapped(model="provider/model", system_prompt="FULL_SYSTEM_PROMPT", content_blocks=content,
            tool=tool, max_tokens=24000, has_pdf=False, pdf_engine="native")
    joined = "\n".join(events)
    assert "AI_CALL_PROMPT_BEGIN" in joined and "AI_CALL_PROMPT_END" in joined
    assert "FULL_SYSTEM_PROMPT" in joined
    assert "FULL_USER_PROMPT with dimensions 18.0 x 19.2 mm" in joined
    assert "FULL_TOOL_SCHEMA" in joined and "report_global_inventory" in joined
    assert "PRIVATE_IMAGE_BYTES" not in joined
    assert "sha256=" in joined and "encoded_chars" in joined


@pytest.mark.parametrize("error", [RuntimeError("sk-KEY PRIVATE_PROMPT"),
                                  ValueError("Authorization: PRIVATE_HEADER"),
                                  KeyboardInterrupt("PRIVATE_INTERRUPT")])
def test_failure_logs_type_not_message_and_reraises_same_exception(error):
    events = []

    def call(**kwargs):
        raise error

    wrapped = diagnostics.make_logged_call(call, events.append)
    with pytest.raises(type(error)) as caught:
        wrapped(model="provider/model", tool={"name": "report_parts"})
    assert caught.value is error
    assert len(events) == 2 and events[1].startswith("AI_CALL_FAILURE ")
    assert f"error={type(error).__name__}" in events[1]
    assert "elapsed=" in events[1]
    assert "PRIVATE" not in "\n".join(events) and "sk-KEY" not in "\n".join(events)


@pytest.mark.parametrize("fail", [False, True])
def test_progress_callback_failure_does_not_change_call_outcome(fail):
    error = RuntimeError("original")
    result = object()
    calls = []

    def progress(message):
        raise ValueError("private logger error")

    def call(*args, **kwargs):
        calls.append((args, kwargs))
        if fail:
            raise error
        return result

    wrapped = diagnostics.make_logged_call(call, progress)
    if fail:
        with pytest.raises(RuntimeError) as caught:
            wrapped(result, extra=result)
        assert caught.value is error
    else:
        assert wrapped(result, extra=result) is result
    assert calls == [((result,), {"extra": result})]


def test_optional_callback_returns_original_callable():
    call = object()
    assert diagnostics.make_logged_call(call, None) is call


def test_positional_metadata_defaults_and_call_numbers_are_supported():
    events = []

    def call(model="provider/default", tool=None):
        return None

    wrapped = diagnostics.make_logged_call(call, events.append)
    wrapped("provider/positional", {"name": "detect_parts"})
    wrapped()
    assert "id=1 model=provider/positional tool=detect_parts" in events[0]
    assert "id=2 model=provider/default tool=[unavailable]" in events[2]
    assert len(events) == 4


@pytest.mark.parametrize("unsafe", ["sk-or-v1-private", "Bearer-secret", "api_key-private",
                                    "private\nprompt", "data:image/png;base64,PRIVATE",
                                    "https://host/private", "x" * 1000, {"private": "data"}])
def test_identifiers_cannot_inject_prompts_credentials_or_unbounded_text(unsafe):
    events = []
    wrapped = diagnostics.make_logged_call(lambda **kwargs: {}, events.append)
    wrapped(model=unsafe, tool={"function": {"name": unsafe}})
    assert all("model=[unavailable] tool=[unavailable]" in event for event in events)
    assert all(len(event) < 200 for event in events)


@pytest.mark.parametrize("result,expected", [([], "array items=0"), ([1, 2], "array items=2"),
    (None, "null"), ("PRIVATE", "scalar (content omitted)"), (b"PRIVATE", "scalar (content omitted)"),
    (42, "scalar (content omitted)"), (True, "scalar (content omitted)")])
def test_result_summary_never_logs_scalar_values(result, expected):
    events = []
    wrapped = diagnostics.make_logged_call(lambda: result, events.append)
    assert wrapped() is result
    assert f"result={expected}" in events[-1]
    assert "PRIVATE" not in events[-1]


def test_opaque_results_are_not_inspected_and_large_cyclic_results_are_bounded():
    class Hostile:
        def __str__(self):
            raise AssertionError("must not stringify")

        def __len__(self):
            raise AssertionError("must not count custom objects")

    opaque = Hostile()
    cyclic = {"components": [opaque] * 100000, "private": opaque}
    cyclic["self"] = cyclic
    events = []
    for result in (opaque, cyclic):
        wrapped = diagnostics.make_logged_call(lambda: result, events.append)
        assert wrapped() is result
    assert "opaque (content omitted)" in events[1]
    assert "object fields=3 components=100000" in events[3]
    assert all(len(event) < 200 for event in events)


def test_invalid_arguments_raise_from_original_callable():
    events = []

    def call(required):
        return required

    wrapped = diagnostics.make_logged_call(call, events.append)
    with pytest.raises(TypeError, match="required"):
        wrapped()
    assert len(events) == 2 and "error=TypeError" in events[-1]


# Contract changed (audit D8, 2026-09-25). Before: any "retryable" error got
# exactly one immediate retry in the compatibility form, a plain HTTP 400
# included. Now the boundary says WHY a retry may help: `compatibility` (a 400
# about tool_choice) gets one immediate retry in the portable form; `retryable`
# (transient 408/409/429/5xx) gets the SAME request again, up to MAX_ATTEMPTS,
# after a bounded exponential backoff honouring Retry-After.


@pytest.fixture
def waits(monkeypatch):
    """Record backoff waits instead of sleeping."""
    recorded = []
    monkeypatch.setattr(diagnostics, "_sleep", recorded.append)
    return recorded


def test_compatibility_failure_gets_one_logged_compatibility_retry(waits):
    events, modes = [], []

    def call(*, model, tool, compatibility_retry=False):
        modes.append(compatibility_retry)
        if len(modes) == 1:
            raise OpenRouterError("safe HTTP 400 tool_choice", compatibility=True)
        return {"components": []}

    result = diagnostics.make_logged_call(call, events.append)(
        model="provider/model", tool={"function": {"name": "report"}})
    assert result == {"components": []}
    assert modes == [False, True] and waits == []
    assert any(event.startswith("AI_CALL_RETRY ") and "attempt=2" in event
               and "reason=compatibility" in event for event in events)
    assert events[-1].startswith("AI_CALL_END ")


def test_a_refused_required_form_gets_the_auto_form(waits):
    """A model refusing every forced tool (Claude 5.5, 2026-10-30) is reached by the last form."""
    events, modes = [], []

    def call(*, model, tool, compatibility_retry=False):
        modes.append(compatibility_retry)
        if len(modes) < 3:
            raise OpenRouterError("safe HTTP 400 tool_choice", compatibility=True)
        return {"components": []}

    result = diagnostics.make_logged_call(call, events.append)(
        model="provider/model", tool={"function": {"name": "report"}})
    assert result == {"components": []}
    assert modes == [False, 1, 2] and waits == []


def test_compatibility_retries_stop_after_the_last_form(waits):
    """Negative: past the last form the same refusal is not paid again."""
    events, modes = [], []

    def call(*, model, tool, compatibility_retry=False):
        modes.append(compatibility_retry)
        raise OpenRouterError("safe HTTP 400 tool_choice", compatibility=True)

    with pytest.raises(OpenRouterError):
        diagnostics.make_logged_call(call, events.append)(
            model="provider/model", tool={"function": {"name": "report"}})
    assert modes == [False, 1, 2]


def test_openrouter_tool_choice_forms(monkeypatch):
    """Form 0 names the tool, 1 is «required», 2 is «auto» (body actually sent)."""
    from kicadgen.services import openrouter
    bodies = []

    def post(request, blocks):
        import json
        bodies.append(json.loads(request.data))
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "report", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(openrouter, "api_key", lambda: "test-key")
    monkeypatch.setattr(openrouter, "_post_chat", post)
    tool = {"type": "function", "function": {"name": "report", "parameters": {"type": "object"}}}
    for level in (0, 1, 2, True):
        openrouter.call_tool("vendor/model", "s", [], tool, compatibility_retry=level)
    assert [body["tool_choice"] for body in bodies] == [
        {"type": "function", "function": {"name": "report"}}, "required", "auto", "required"]


def test_transient_failure_is_retried_unchanged_with_exponential_backoff(waits):
    events, modes = [], []

    def call(*, model, tool, compatibility_retry=False):
        modes.append(compatibility_retry)
        if len(modes) < 3:
            raise OpenRouterError("safe HTTP 503", retryable=True)
        return {"components": []}

    result = diagnostics.make_logged_call(call, events.append)(
        model="provider/model", tool={"function": {"name": "report"}})
    assert result == {"components": []}
    assert modes == [False, False, False]
    # First retry after BACKOFF_BASE_S, the second after twice as long.
    assert sum(waits) == pytest.approx(diagnostics.BACKOFF_BASE_S * 3)


def test_retry_after_is_honoured_and_bounded():
    assert diagnostics.retry_delay(1, 7) == 7
    assert diagnostics.retry_delay(1, 10_000) == diagnostics.BACKOFF_CAP_S
    assert diagnostics.retry_delay(3, None) == diagnostics.BACKOFF_BASE_S * 4
    for bad in (True, -1, float("nan"), "5"):
        assert diagnostics.retry_delay(1, bad) == diagnostics.BACKOFF_BASE_S


def test_transient_failure_stops_after_max_attempts(waits):
    events, errors = [], []

    def call(*, model, tool, compatibility_retry=False):
        error = OpenRouterError("safe HTTP 429", retryable=True, retry_after=1.5)
        errors.append(error)
        raise error

    wrapped = diagnostics.make_logged_call(call, events.append)
    with pytest.raises(OpenRouterError) as caught:
        wrapped(model="provider/model", tool={"function": {"name": "report"}})
    assert caught.value is errors[-1] and len(errors) == diagnostics.MAX_ATTEMPTS
    assert sum(event.startswith("AI_CALL_RETRY ") for event in events) == diagnostics.MAX_ATTEMPTS - 1
    assert sum(waits) == pytest.approx(1.5 * (diagnostics.MAX_ATTEMPTS - 1))
    assert events[-1].startswith("AI_CALL_FAILURE ") and f"attempts={diagnostics.MAX_ATTEMPTS}" in events[-1]


def test_a_plain_error_is_not_retried(waits):
    """Negative: a 400 without the compatibility mark is refused again if repeated."""
    events, calls = [], []

    def call(*, model, tool, compatibility_retry=False):
        calls.append(1)
        raise OpenRouterError("safe HTTP 400")

    with pytest.raises(OpenRouterError):
        diagnostics.make_logged_call(call, events.append)(
            model="provider/model", tool={"function": {"name": "report"}})
    assert len(calls) == 1 and waits == []
    assert not any(event.startswith("AI_CALL_RETRY ") for event in events)


def test_cancel_is_checked_before_every_paid_attempt(waits):
    class Stop(diagnostics.CallCancelled):
        pass
    state = {"stop": False, "calls": 0}

    def cancel():
        if state["stop"]:
            raise Stop("cancelled")

    def call(*, model, tool):
        state["calls"] += 1
        state["stop"] = True  # the user presses Stop during the first request
        raise OpenRouterError("safe HTTP 503", retryable=True)

    events = []
    wrapped = diagnostics.make_logged_call(call, events.append, cancel=cancel)
    with pytest.raises(Stop):
        wrapped(model="provider/model", tool={"function": {"name": "report"}})
    assert state["calls"] == 1
    assert any(event.startswith("AI_CALL_CANCELLED ") for event in events)
    with pytest.raises(Stop):
        wrapped(model="provider/model", tool={"function": {"name": "report"}})
    assert state["calls"] == 1  # nothing paid once cancelled


def test_cancel_without_progress_still_wraps():
    """With a cancel channel the wrapper is applied even without a log callback."""
    wrapped = diagnostics.make_logged_call(lambda **kw: "ok", None, cancel=lambda: None)
    assert wrapped(model="m", tool={}) == "ok"
    original = lambda **kw: "ok"
    assert diagnostics.make_logged_call(original, None) is original


# -- M17: redacted arguments of successful answers, opt-in only -----------------

def _secret_free(joined: str) -> bool:
    """Whether no private marker of the M17 fixtures leaked into the log."""
    return all(marker not in joined for marker in ("PRIVATE_", "sk-live-"))


def _result_lines(events):
    """The AI_CALL_RESULT lines among the logged events."""
    return [event for event in events if event.startswith("AI_CALL_RESULT ")]


def test_log_result_writes_one_redacted_line_of_a_successful_answer():
    """@brief Positive: with the opt-in on, the arguments are in the log, redacted and hashed."""
    import hashlib
    import json
    events = []
    long_text = "x" * (diagnostics.RESULT_STRING_MAX_CHARS + 1)
    result = {"pads": [{"number": "A1", "x_mm": 1.5, "y_mm": -2.0,
                        "note": "Bearer sk-live-PRIVATE_TOKEN"}],
              "transcript": long_text,
              "image_data": "PRIVATE_BASE64",
              "ok": True, "height_mm": None,
              "opaque": object()}
    wrapped = diagnostics.make_logged_call(lambda **_kw: result, events.append, log_result=True)
    assert wrapped(model="provider/model", tool={"name": "report_pads"}) is result
    lines = _result_lines(events)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("AI_CALL_RESULT id=1 model=provider/model tool=report_pads sha256=")
    header, _, payload = line.partition(" chars=")
    chars, _, text = payload.partition(" ")
    logged = json.loads(text)
    assert int(chars) == len(text)
    assert header.split("sha256=")[1] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # The arguments a reader compares are there, verbatim.
    assert logged["pads"][0]["number"] == "A1" and logged["pads"][0]["x_mm"] == 1.5
    assert logged["ok"] is True and logged["height_mm"] is None
    # Long strings and payload keys become digest + length, secrets are
    # redacted, opaque objects are named, never stringified.
    assert logged["transcript"] == {"sha256": hashlib.sha256(long_text.encode()).hexdigest(),
                                    "length": len(long_text)}
    assert set(logged["image_data"]) == {"sha256", "length"}
    assert "[redacted]" in logged["pads"][0]["note"]
    assert logged["opaque"] == {"type": "object"}
    assert _secret_free("\n".join(events))
    # The shallow summary counts the list field whatever its name.
    end = [e for e in events if e.startswith("AI_CALL_END ")][0]
    assert "result=object fields=6 pads=1" in end
    # Order of the lines: END first, then the one RESULT line.
    assert events.index(end) < events.index(line)


def test_log_result_follows_log_prompt_and_is_off_by_default():
    """@brief Negative: without the opt-in nothing of the result is written; log_prompt turns it on."""
    result = {"pads": [{"number": "PRIVATE_NUMBER"}]}
    for kwargs, expected in (({}, False), ({"log_prompt": True}, True),
                             ({"log_prompt": True, "log_result": False}, False),
                             ({"log_result": True}, True)):
        events = []
        wrapped = diagnostics.make_logged_call(lambda **_kw: result, events.append, **kwargs)
        wrapped(model="provider/model", tool={"name": "report_pads"})
        joined = "\n".join(events)
        assert ("AI_CALL_RESULT" in joined) is expected, kwargs
        assert ("PRIVATE_NUMBER" in joined) is expected, kwargs


def test_log_result_never_writes_exceptions_or_scalars():
    """@brief Negative: a failed call and a scalar answer leave no AI_CALL_RESULT line."""
    events = []

    def fail(**_kw):
        raise RuntimeError("PRIVATE_MESSAGE")

    wrapped = diagnostics.make_logged_call(fail, events.append, log_result=True)
    with pytest.raises(RuntimeError):
        wrapped(model="provider/model", tool={"name": "report_pads"})
    scalar = diagnostics.make_logged_call(lambda **_kw: "PRIVATE_SCALAR", events.append,
                                          log_result=True)
    scalar(model="provider/model", tool={"name": "report_pads"})
    joined = "\n".join(events)
    assert not _result_lines(events) and _secret_free(joined)


def test_log_result_is_bounded_for_huge_and_cyclic_answers():
    """@brief Positive: the line is cut at the shared cap and a cycle does not recurse for ever."""
    from kicadgen.services import datasheet_extractor
    assert diagnostics.RESULT_LOG_MAX_CHARS == datasheet_extractor._REJECTED_RESPONSE_MAX_CHARS
    huge = {"items": [{"n": i} for i in range(60000)]}
    events = []
    wrapped = diagnostics.make_logged_call(lambda: huge, events.append, log_result=True)
    assert wrapped() is huge
    line = _result_lines(events)[0]
    assert line.endswith(" …[truncated]")
    assert len(line) < diagnostics.RESULT_LOG_MAX_CHARS + 200
    chars = int(line.partition(" chars=")[2].partition(" ")[0])
    assert chars > diagnostics.RESULT_LOG_MAX_CHARS
    # A cycle is cut where it closes; the rest of the answer is still there.
    cyclic = {"a": {"b": 1}}
    cyclic["a"]["self"] = cyclic
    events = []
    diagnostics.make_logged_call(lambda: cyclic, events.append, log_result=True)()
    line = _result_lines(events)[0]
    assert '{"a":{"b":1,"self":"[cut]"}}' in line and not line.endswith("[truncated]")


def test_result_summary_counts_every_list_field_and_validates_its_name():
    """@brief Positive/negative: any list field is counted; an unsafe key is not echoed."""
    result = {"holes": [1, 2, 3], "readings": (1,), "sk-PRIVATE": [1], "n": 4,
              "a key with spaces": [1, 2]}
    events = []
    diagnostics.make_logged_call(lambda: result, events.append)()
    end = events[-1]
    assert "holes=3 readings=1 [unavailable]=1 [unavailable]=2" in end
    assert "PRIVATE" not in end and "spaces" not in end
