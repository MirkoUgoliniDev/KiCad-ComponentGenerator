"""@brief Emit AI-call lifecycle diagnostics, with opt-in prompt and result auditing.

Only validated model/tool identifiers, elapsed time, exception class and shallow
result counts are logged by default.  The application can explicitly enable
prompt auditing: textual prompts and tool schemas are then written verbatim,
while binary/data-URL image payloads and credentials remain excluded.  The
same switch (or `log_result`) also writes the arguments of every SUCCESSFUL
tool answer, redacted (review 2026-11-04, M17): without them a well-formed but
wrong reading could not be checked against the log.
"""

from functools import wraps
import hashlib
import inspect
from itertools import count
import json
import math
import re
import threading
import time
from time import perf_counter
from typing import Callable, Optional


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}\Z")
_SENSITIVE = re.compile(r"sk-|api.?key|secret|password|bearer|authorization|credential|://", re.I)
#: Upper bound of the list fields counted on the AI_CALL_END line: the line
#: must stay readable (and bounded) whatever the shape of the answer.
_MAX_COUNTED_FIELDS = 24
#: Maximum characters of one logged result (AI_CALL_RESULT). Same value as
#: datasheet_extractor._REJECTED_RESPONSE_MAX_CHARS, the cap of the rejected
#: answers written to the same log: that module imports this one, so the
#: constant cannot be imported here without a cycle (a test keeps them equal).
RESULT_LOG_MAX_CHARS = 200_000
#: A string longer than this (2 kB) inside a logged result is replaced by its
#: SHA-256 and length: it is a document, an image or a transcript, not an
#: argument anyone compares by eye.
RESULT_STRING_MAX_CHARS = 2048
#: Result keys whose string values are payloads (encoded files, URLs, images)
#: and are therefore always replaced by SHA-256 and length, whatever their size.
_PAYLOAD_KEY = re.compile(r"data|url|image|file|base64", re.I)
#: Nesting depth beyond which a logged result is cut: a tool answer is a
#: few levels deep; deeper structures are cycles or garbage.
_RESULT_MAX_DEPTH = 32


def _identifier(value):
    """Reject malformed or credential-like identifiers instead of echoing them."""
    if type(value) is not str or not _IDENTIFIER.fullmatch(value) or _SENSITIVE.search(value):
        return "[unavailable]"
    return value


def _result_summary(result):
    """Count only builtin containers; never traverse or stringify result values.

    Every top-level list field of an object is counted, whatever its name:
    the old fixed list of names (components, pins ...) was a per-case rule
    that left the lists of newer tools uncounted (review 2026-11-04, M17).
    Keys are validated like identifiers so that the line cannot carry
    arbitrary text, and at most _MAX_COUNTED_FIELDS are written.
    """
    if type(result) is dict:
        summary = f"object fields={len(result)}"
        counted = 0
        for key, value in result.items():
            if type(value) not in (list, tuple):
                continue
            if counted >= _MAX_COUNTED_FIELDS:
                summary += " …"
                break
            summary += f" {_identifier(key)}={len(value)}"
            counted += 1
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


def _digest_stub(value: str) -> dict:
    """SHA-256 and length standing in for a string that is not logged verbatim."""
    return {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(), "length": len(value)}


def _redacted_result(value, key=None, depth=0, seen=None):
    """@brief Copy of a tool answer that is safe to write to the AI log.

    @param value Builtin container or scalar of the answer.
    @param key Name under which `value` sits in its parent object, or None.
    @param depth Current nesting depth (cut at _RESULT_MAX_DEPTH).
    @param seen Ids of the containers on the current path (cycle guard).
    @return A JSON-serialisable copy: strings pass through
        _redact_secret_text; a string longer than RESULT_STRING_MAX_CHARS, or
        under a key matching _PAYLOAD_KEY, becomes {"sha256", "length"};
        objects that are not builtin containers or scalars become
        {"type": <class name>} and are never stringified or measured.
    """
    if isinstance(value, bool) or value is None or type(value) in (int, float):
        return value
    if type(value) is str:
        if len(value) > RESULT_STRING_MAX_CHARS or (type(key) is str and _PAYLOAD_KEY.search(key)):
            return _digest_stub(value)
        return _redact_secret_text(value)
    if type(value) in (dict, list, tuple):
        if depth >= _RESULT_MAX_DEPTH or (seen is not None and id(value) in seen):
            return "[cut]"
        seen = (seen or set()) | {id(value)}
        if type(value) is dict:
            return {str(k): _redacted_result(v, str(k), depth + 1, seen) for k, v in value.items()}
        return [_redacted_result(v, key, depth + 1, seen) for v in value]
    return {"type": type(value).__name__}


def _result_snapshot(result) -> tuple[str, int, str]:
    """@brief Serialise a dict/list result for the AI_CALL_RESULT line.

    @param result The answer (dict, list or tuple) a successful call returned.
    @return (sha256 of the complete redacted JSON, its length in characters,
        the JSON cut at RESULT_LOG_MAX_CHARS with a «…[truncated]» marker
        when it was longer).
    """
    encoded = json.dumps(_redacted_result(result), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    text = encoded
    if len(text) > RESULT_LOG_MAX_CHARS:
        text = text[:RESULT_LOG_MAX_CHARS] + " …[truncated]"
    return digest, len(encoded), text


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
    if _sent_strict(arguments.get("model"), tool):
        # The schema above is the caller's; DeepSeek receives its strict form
        # on the beta endpoint (normal mode again only if that is refused).
        lines.append("AI_CALL_PROMPT_STRICT deepseek-beta")
    return digest, lines


def _sent_strict(model, tool) -> bool:
    """Whether the call is sent to DeepSeek in strict mode (see openrouter.strict_tool)."""
    try:
        from .openrouter import strict_tool
        from .providers import PROVIDER_DEEPSEEK, provider_of
        return (isinstance(tool, dict) and provider_of(str(model or "")) == PROVIDER_DEEPSEEK
                and strict_tool(tool) is not None)
    except Exception:  # noqa: BLE001 - a log line must never break the call
        return False


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
#: Compatibility retries of one call, each in a less specific `tool_choice`
#: form (openrouter._call_tool: «required», then «auto»). They share the
#: MAX_ATTEMPTS budget with the transient retries.
COMPATIBILITY_LEVELS = 2
#: Granularity with which a cancellation is noticed during a backoff wait.
_CANCEL_POLL_S = 0.25


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep, so tests can observe waits without waiting."""
    time.sleep(seconds)


class _CancelWatch:
    """Poll `cancel` while a paid attempt runs and close its connection when it fires.

    The checker of make_logged_call is otherwise asked only between
    attempts: a request already waiting for its answer (up to the 300 s
    HTTP timeout) ignored «Interrompi» (review 2026-10-29, E6). The watch
    runs in its own daemon thread, calls `cancel` every _CANCEL_POLL_S and,
    the first time it raises, keeps that exception and closes the in-flight
    request of the calling thread with `openrouter.abort_request`. The
    attempt then fails and the wrapper raises the kept exception instead,
    without any retry. `cancel` must therefore be callable from another
    thread (the application passes checks of a threading.Event or of
    QThread.isInterruptionRequested).
    """

    def __init__(self, cancel: Callable[[], None]):
        """@brief Prepare the watch of the calling thread's next attempt (not started yet).

        @param cancel Thread-safe checker that raises to cancel.
        """
        self._cancel = cancel
        self._thread_id = threading.get_ident()
        self._stop = threading.Event()
        #: The exception raised by `cancel`, or None while it has not fired.
        self.cancelled: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, name="ai-cancel-watch", daemon=True)

    def __enter__(self) -> "_CancelWatch":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> bool:
        self._stop.set()
        self._thread.join()
        return False

    def _run(self) -> None:
        while not self._stop.wait(_CANCEL_POLL_S):
            try:
                self._cancel()
            except BaseException as exc:  # noqa: BLE001 - any raise of the checker is a cancel
                self.cancelled = exc
                try:
                    from .openrouter import abort_request
                    abort_request(self._thread_id)
                except Exception:  # noqa: BLE001 - the kept exception still stops the chain
                    pass
                return


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


def _after_budget_retry(truncation: BaseException, failure: BaseException, budget) -> BaseException:
    """The error of a completion-budget retry that did not succeed, saying BOTH outcomes.

    The second failure is raised ITSELF -- its type and attributes
    (http_status, unreachable, possibly_billed, retryable...) are what the
    callers read (third review, L8) -- with the truncation said in its
    message and chained as its cause: a second cut says it was cut again
    with the larger budget; any other failure (a 400 on the larger request,
    a network error) says it followed a truncated answer.
    """
    from .messages import tx
    if type(failure) is type(truncation):
        note = tx("Ripetuta una volta con {budget} token di completamento: troncata di nuovo.",
                  budget=budget)
    else:
        note = tx("(Secondo tentativo con {budget} token di completamento, dopo una risposta "
                  "troncata dal budget: {truncation})", budget=budget, truncation=truncation)
    if failure.args and isinstance(failure.args[0], str):
        failure.args = (failure.args[0] + " " + note,) + tuple(failure.args[1:])
    failure.__cause__ = truncation
    return failure


def make_logged_call(call_tool: Callable, progress: Optional[Callable[[str], None]], *,
                     log_prompt: bool = False,
                     cancel: Optional[Callable[[], None]] = None,
                     log_result: Optional[bool] = None) -> Callable:
    """@brief Wrap a synchronous AI call with lifecycle and optional prompt diagnostics.

    @param call_tool Callable receiving the original positional and keyword arguments.
    @param progress Optional string callback, invoked immediately at each lifecycle event.
    @param log_prompt When true, log the exact textual request components and a
        hash/size summary for binary blocks.  Disabled by default for callers
        that do not explicitly opt into an audit trail.
    @param log_result When true, write after AI_CALL_END one line
        `AI_CALL_RESULT {context} sha256=<digest> chars=<n> <json>` with the
        arguments of a successful dict/list answer, redacted by
        _redacted_result (credentials removed, long strings and payload keys
        replaced by SHA-256 and length) and cut at RESULT_LOG_MAX_CHARS;
        `sha256` and `chars` describe the complete redacted JSON. Scalar or
        opaque results and exceptions are never written. None (default) =
        the value of `log_prompt`: the switch that writes the prompts
        verbatim writes the answers too. With both off, nothing of the
        result but its shallow counts reaches the log.
    @param cancel Optional checker called before EVERY paid attempt (retries
        included), during backoff waits and, from a watcher thread, every
        _CANCEL_POLL_S while an attempt is in flight; it aborts the chain by
        raising, preferably a CallCancelled subclass. Raised during an
        attempt, it closes that attempt's connection
        (`openrouter.abort_request`) and its exception is raised in place of
        the attempt's failure. It must be thread-safe. None = no
        cancellation channel.
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
        error marked `compatibility` (a 400 about the tool_choice form) gets up
        to COMPATIBILITY_LEVELS immediate retries, each in a less specific
        form (`compatibility_retry` 1, then 2), when the callable supports it; an
        answer cut by the completion budget (an error carrying `budget_retry`,
        openrouter.TruncatedAnswerError) gets ONE more attempt with that larger
        `max_tokens`, announced as `AI_CALL_RETRY … reason=completion_budget`,
        apart from the other two counts (model benchmark 2026-11-14). An error
        after that attempt is reported WITH the truncation, never in place of
        it. Any other error, a plain 400 included, is not retried: repeating
        an invalid request only pays for the same refusal.
    """
    if progress is None and cancel is None:
        return call_tool
    if log_result is None:
        log_result = bool(log_prompt)
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
        compatibility_level = 0
        # The one completion-budget retry: the truncation it answers, kept
        # to be reported with whatever the larger attempt ends in.
        budget_error = None
        budget_attempts = 0
        while True:
            watch = _CancelWatch(cancel) if cancel is not None else None
            try:
                if watch is None:
                    result = call_tool(*args, **attempt_kwargs)
                else:
                    with watch:
                        result = call_tool(*args, **attempt_kwargs)
                break
            except BaseException as exc:
                if watch is not None and watch.cancelled is not None:
                    # Cancel pressed while the attempt was in flight: its
                    # connection was closed; the user's own cancellation is
                    # raised, never a retry (review 2026-10-29, E6).
                    emit(f"AI_CALL_CANCELLED {context} attempts={attempt}")
                    raise watch.cancelled from exc
                if isinstance(exc, CallCancelled):
                    raise
                budget = getattr(exc, "budget_retry", None)
                if (budget_error is None and isinstance(budget, int) and not isinstance(budget, bool)
                        and signature is not None and "max_tokens" in signature.parameters):
                    budget_error = exc
                    attempt_kwargs = dict(attempt_kwargs, max_tokens=budget)
                    # Counted in the attempts the log shows, never against
                    # MAX_ATTEMPTS of the transient retries (third review, L8).
                    attempt += 1
                    budget_attempts = 1
                    emit(f"AI_CALL_RETRY {context} attempt={attempt} reason=completion_budget "
                         f"max_tokens={budget}")
                    check_cancel()
                    if log_prompt:
                        log_prompt_lines(context, bound_arguments(args, attempt_kwargs),
                                         f" attempt={attempt}")
                    continue
                compatibility = (getattr(exc, "compatibility", False) is True
                                 and compatibility_level < COMPATIBILITY_LEVELS
                                 and signature is not None
                                 and "compatibility_retry" in signature.parameters)
                transient = getattr(exc, "retryable", False) is True
                if attempt - budget_attempts >= MAX_ATTEMPTS or not (compatibility or transient):
                    elapsed = perf_counter() - started
                    emit(f"AI_CALL_FAILURE {context} elapsed={elapsed:.3f}s attempts={attempt} "
                         f"error={_identifier(type(exc).__name__)}")
                    if budget_error is not None:
                        raise _after_budget_retry(budget_error, exc,
                                                  attempt_kwargs.get("max_tokens"))
                    raise
                attempt += 1
                if compatibility:
                    compatibility_level += 1
                    # 1 is True for the callables that only know one form.
                    attempt_kwargs = dict(attempt_kwargs, compatibility_retry=compatibility_level)
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
        if log_result and type(result) in (dict, list, tuple):
            # Opt-in only: the redacted arguments of the answer that was
            # accepted, so that a well-formed but wrong reading can be
            # checked afterwards (review 2026-11-04, M17). An exception never
            # reaches this point and a scalar carries no arguments.
            digest, chars, text = _result_snapshot(result)
            emit(f"AI_CALL_RESULT {context} sha256={digest} chars={chars} {text}")
        return result

    return logged_call
