"""@brief Emit AI-call lifecycle diagnostics, with opt-in prompt auditing.

Only validated model/tool identifiers, elapsed time, exception class and shallow
result counts are logged by default.  The application can explicitly enable
prompt auditing: textual prompts and tool schemas are then written verbatim,
while binary/data-URL image payloads and credentials remain excluded.
"""

from functools import wraps
import hashlib
import inspect
from itertools import count
import json
import math
import re
import time
from time import perf_counter
from typing import Callable, Optional


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}\Z")
_SENSITIVE = re.compile(r"sk-|api.?key|secret|password|bearer|authorization|credential|://", re.I)
_COUNT_FIELDS = ("components", "pins", "terminals", "detections", "items", "results")


def _identifier(value):
    """Reject malformed or credential-like identifiers instead of echoing them."""
    if type(value) is not str or not _IDENTIFIER.fullmatch(value) or _SENSITIVE.search(value):
        return "[unavailable]"
    return value


def _result_summary(result):
    """Count only builtin containers; never traverse or stringify result values."""
    if type(result) is dict:
        summary = f"object fields={len(result)}"
        for key in _COUNT_FIELDS:
            value = result.get(key)
            if type(value) in (list, tuple):
                summary += f" {key}={len(value)}"
        return summary
    if type(result) in (list, tuple):
        return f"array items={len(result)}"
    if result is None:
        return "null"
    if type(result) in (str, int, float, bool, bytes):
        return "scalar (content omitted)"
    return "opaque (content omitted)"


def _redact_secret_text(value: str) -> str:
    """@brief Remove credentials if an editable prompt accidentally contains one."""
    value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
    return value


def _prompt_snapshot(arguments: dict) -> tuple[str, list[str]]:
    """@brief Serialize the sent textual prompt without serializing image bytes.

    The hash covers the exact system/user text and the metadata of every
    content block.  Image/file data are intentionally represented by a SHA-256
    and length: logging their base64 would make the log huge and would persist
    the user's board photo unnecessarily.
    """
    system_prompt = arguments.get("system_prompt")
    system_text = _redact_secret_text(system_prompt) if isinstance(system_prompt, str) else "[omitted]"
    raw_blocks = arguments.get("content_blocks")
    blocks = []
    if isinstance(raw_blocks, (list, tuple)):
        for block in raw_blocks:
            if not isinstance(block, dict):
                blocks.append({"type": "unknown", "value": type(block).__name__})
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                blocks.append({"type": "text", "text": _redact_secret_text(block["text"])})
                continue
            if block_type == "image_url" and isinstance(block.get("image_url"), dict):
                url = block["image_url"].get("url")
                if isinstance(url, str):
                    blocks.append({
                        "type": "image_url",
                        "sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        "encoded_chars": len(url),
                        "detail": block["image_url"].get("detail"),
                    })
                else:
                    blocks.append({"type": "image_url", "payload": "[omitted]"})
                continue
            # File/PDF blocks can contain large encoded data as well.
            compact = {"type": block_type if isinstance(block_type, str) else "unknown"}
            for key in ("image", "file", "filename", "mime_type", "detail"):
                if key in block and isinstance(block[key], (str, int, float, bool)):
                    compact[key] = block[key] if key in ("filename", "mime_type", "detail") else type(block[key]).__name__
            for key in ("file_data", "data", "url"):
                value = block.get(key)
                if isinstance(value, str):
                    compact[key] = {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                                    "encoded_chars": len(value)}
            blocks.append(compact)
    tool = arguments.get("tool")
    tool_value = tool if isinstance(tool, dict) else {"type": type(tool).__name__}
    options = {key: arguments.get(key) for key in ("max_tokens", "has_pdf", "pdf_engine", "compatibility_retry")
               if key in arguments}
    canonical = {"model": _identifier(arguments.get("model")), "system_prompt": system_text,
                 "content_blocks": blocks, "tool": tool_value, "request_options": options}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    lines = [f"AI_CALL_PROMPT_SYSTEM {system_text}"]
    for index, block in enumerate(blocks, 1):
        lines.append("AI_CALL_PROMPT_BLOCK " + str(index) + " " +
                     json.dumps(block, ensure_ascii=False, sort_keys=True))
    lines.append("AI_CALL_PROMPT_TOOL " + json.dumps(tool_value, ensure_ascii=False, sort_keys=True))
    lines.append("AI_CALL_PROMPT_OPTIONS " + json.dumps(options, ensure_ascii=False, sort_keys=True))
    return digest, lines


def _usage_snapshot():
    """@brief Token totals at the moment of the call, when available (None otherwise)."""
    try:
        from .openrouter import usage_totals
        totals = usage_totals()
        return (totals.prompt_tokens, totals.completion_tokens, totals.cost_usd)
    except Exception:  # noqa: BLE001 - diagnostics must never bring down a call
        return None


def _usage_delta(before):
    """@brief Tokens and cost attributable to the call that just ended.

    @param before Snapshot taken before the call, or None.
    @return Text to append to the diagnostic line, possibly empty.
    @note This is the difference of a global total: with parallel calls it
        would attribute to one call what another one spent. Today the calls
        of an analysis run in sequence; should they ever run in parallel,
        this line would have to be derived from the response instead of
        from the total.
    """
    after = _usage_snapshot()
    if before is None or after is None:
        return ""
    tokens_in, tokens_out, cost = (a - b for a, b in zip(after, before))
    return f" tokens_in={tokens_in} tokens_out={tokens_out} cost_usd={cost:.4f}"


class CallCancelled(Exception):
    """@brief Base class of the cancellation raised before a paid AI call.

    @note Optional pipeline steps that downgrade their own failures to notes
        (photo inventory, detection tiles, reviews) must re-raise this class:
        a cancellation is the user's decision, not a failure of the step.
    """


#: Maximum attempts of one call when the provider reports a transient failure
#: (rate limit, timeout, 5xx): the first request plus two retries.
MAX_ATTEMPTS = 3
#: First backoff delay in seconds; doubled at every further retry.
BACKOFF_BASE_S = 2.0
#: Upper bound of any single wait, including a provider Retry-After: a longer
#: pause would look like a hang in the GUI, and failing is then clearer.
BACKOFF_CAP_S = 30.0
#: Granularity with which a cancellation is noticed during a backoff wait.
_CANCEL_POLL_S = 0.25


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep, so tests can observe waits without waiting."""
    time.sleep(seconds)


def retry_delay(attempt: int, retry_after=None) -> float:
    """@brief Seconds to wait before retry number `attempt` (1 = first retry).

    @param attempt 1-based retry index.
    @param retry_after Provider Retry-After in seconds, or None.
    @return The provider delay when it is a finite non-negative number, else
        BACKOFF_BASE_S * 2**(attempt-1); always bounded by BACKOFF_CAP_S.
    """
    if (not isinstance(retry_after, bool) and isinstance(retry_after, (int, float))
            and math.isfinite(retry_after) and retry_after >= 0):
        return min(float(retry_after), BACKOFF_CAP_S)
    return min(BACKOFF_BASE_S * 2 ** (max(1, attempt) - 1), BACKOFF_CAP_S)


def make_logged_call(call_tool: Callable, progress: Optional[Callable[[str], None]], *,
                     log_prompt: bool = False,
                     cancel: Optional[Callable[[], None]] = None) -> Callable:
    """@brief Wrap a synchronous AI call with lifecycle and optional prompt diagnostics.

    @param call_tool Callable receiving the original positional and keyword arguments.
    @param progress Optional string callback, invoked immediately at each lifecycle event.
    @param log_prompt When true, log the exact textual request components and a
        hash/size summary for binary blocks.  Disabled by default for callers
        that do not explicitly opt into an audit trail.
    @param cancel Optional checker called before EVERY paid attempt (retries
        included) and during backoff waits; it aborts the chain by raising,
        preferably a CallCancelled subclass. None = no cancellation channel.
    @return Callable preserving argument objects, result identity and original exceptions;
        the original callable itself when both progress and cancel are None.
    @throws BaseException The original callable's exception, re-raised unchanged,
        or the exception raised by `cancel`.
    @note Callback failures are best-effort and cannot replace a call result/error.
        Failure logs contain exception type only, never the exception message.
        Each wrapper numbers its own calls; elapsed time uses a monotonic clock.
        Retry policy, decided by the OpenRouter boundary and never by guessing
        here: an error marked `retryable` (transient: 408/409/429/5xx) is
        retried with the SAME request up to MAX_ATTEMPTS attempts, after a
        bounded exponential backoff that honours the provider Retry-After; an
        error marked `compatibility` (a 400 about the tool_choice form) gets one
        immediate retry in the portable required-tool form when the callable
        supports it. Any other error, a plain 400 included, is not retried:
        repeating an invalid request only pays for the same refusal.
    """
    if progress is None and cancel is None:
        return call_tool
    try:
        signature = inspect.signature(call_tool)
    except (TypeError, ValueError):
        signature = None
    sequence = count(1)

    def emit(message):
        if progress is None:
            return
        try:
            progress(message)
        except Exception:
            # A disconnected GUI/logger must not cancel a paid request or hide
            # its original outcome. In particular, never log the callback error.
            pass

    def bound_arguments(args, call_kwargs):
        arguments = call_kwargs
        if signature is not None:
            try:
                bound = signature.bind_partial(*args, **call_kwargs)
                bound.apply_defaults()
                arguments = {**call_kwargs, **bound.arguments}
            except TypeError:
                pass  # Let the actual callable raise its own argument error.
        return arguments

    def check_cancel():
        if cancel is not None:
            cancel()

    def wait(seconds):
        """Backoff wait that still notices a cancellation within a fraction of a second."""
        remaining = seconds
        while remaining > 0:
            check_cancel()
            step = min(_CANCEL_POLL_S, remaining)
            _sleep(step)
            remaining -= step
        check_cancel()

    def log_prompt_lines(context, arguments, suffix=""):
        digest, lines = _prompt_snapshot(arguments)
        emit(f"AI_CALL_PROMPT_BEGIN {context}{suffix} sha256={digest} "
             f"blocks={len(arguments.get('content_blocks') or [])}")
        for line in lines:
            emit(line)
        emit(f"AI_CALL_PROMPT_END {context}{suffix} sha256={digest}")

    @wraps(call_tool)
    def logged_call(*args, **kwargs):
        """@brief Forward one invocation and emit allowlisted diagnostics."""
        # Before anything is logged or paid: a cancelled chain starts nothing.
        check_cancel()
        arguments = bound_arguments(args, kwargs)
        tool = arguments.get("tool")
        name = None
        if type(tool) is dict:
            function = tool.get("function")
            name = function.get("name") if type(function) is dict else tool.get("name")
        context = (f"id={next(sequence)} model={_identifier(arguments.get('model'))} "
                   f"tool={_identifier(name)}")
        emit(f"AI_CALL_START {context}")
        if log_prompt:
            log_prompt_lines(context, arguments)
        started = perf_counter()
        # The token tally is global: the before/after difference is the only
        # way to know how much went into THIS call. It shows at once whether
        # a provider dropped the images: a request with four photographs that
        # costs a few thousand tokens did not receive them, however legitimate
        # the answer looks.
        before = _usage_snapshot()
        attempt_kwargs = kwargs
        attempt = 1
        compatibility_used = False
        while True:
            try:
                result = call_tool(*args, **attempt_kwargs)
                break
            except BaseException as exc:
                if isinstance(exc, CallCancelled):
                    raise
                compatibility = (getattr(exc, "compatibility", False) is True
                                 and not compatibility_used and signature is not None
                                 and "compatibility_retry" in signature.parameters)
                transient = getattr(exc, "retryable", False) is True
                if attempt >= MAX_ATTEMPTS or not (compatibility or transient):
                    elapsed = perf_counter() - started
                    emit(f"AI_CALL_FAILURE {context} elapsed={elapsed:.3f}s attempts={attempt} "
                         f"error={_identifier(type(exc).__name__)}")
                    raise
                attempt += 1
                if compatibility:
                    compatibility_used = True
                    attempt_kwargs = dict(attempt_kwargs, compatibility_retry=True)
                    emit(f"AI_CALL_RETRY {context} attempt={attempt} reason=compatibility")
                    check_cancel()
                else:
                    delay = retry_delay(attempt - 1, getattr(exc, "retry_after", None))
                    emit(f"AI_CALL_RETRY {context} attempt={attempt} reason=retryable_http "
                         f"delay={delay:.1f}s")
                    try:
                        wait(delay)
                    except BaseException:
                        emit(f"AI_CALL_CANCELLED {context} attempts={attempt - 1}")
                        raise
                if log_prompt:
                    log_prompt_lines(context, bound_arguments(args, attempt_kwargs),
                                     f" attempt={attempt}")
        elapsed = perf_counter() - started
        emit(f"AI_CALL_END {context} elapsed={elapsed:.3f}s result={_result_summary(result)}"
             + _usage_delta(before))
        return result

    return logged_call
