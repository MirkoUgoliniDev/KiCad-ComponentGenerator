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


def test_retryable_http_failure_gets_one_logged_compatibility_retry():
    events, modes = [], []

    def call(*, model, tool, compatibility_retry=False):
        modes.append(compatibility_retry)
        if len(modes) == 1:
            raise OpenRouterError("safe HTTP 400", retryable=True)
        return {"components": []}

    result = diagnostics.make_logged_call(call, events.append)(
        model="provider/model", tool={"function": {"name": "report"}})
    assert result == {"components": []}
    assert modes == [False, True]
    assert any(event.startswith("AI_CALL_RETRY ") and "attempt=2" in event for event in events)
    assert events[-1].startswith("AI_CALL_END ")


def test_retryable_http_failure_stops_after_second_attempt():
    events, errors = [], []

    def call(*, model, tool, compatibility_retry=False):
        error = OpenRouterError("safe HTTP 400", retryable=True)
        errors.append(error)
        raise error

    wrapped = diagnostics.make_logged_call(call, events.append)
    with pytest.raises(OpenRouterError) as caught:
        wrapped(model="provider/model", tool={"function": {"name": "report"}})
    assert caught.value is errors[-1] and len(errors) == 2
    assert sum(event.startswith("AI_CALL_RETRY ") for event in events) == 1
    assert events[-1].startswith("AI_CALL_FAILURE ") and "attempts=2" in events[-1]
