"""@brief Emit bounded AI-call lifecycle diagnostics without logging payload text.

Only validated model/tool identifiers, elapsed time, exception class and shallow
result counts are logged. Prompts, schemas, images, headers, arbitrary field
names, scalar values and exception messages are never serialized or inspected.
"""

from functools import wraps
import inspect
from itertools import count
import re
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


def make_logged_call(call_tool: Callable, progress: Optional[Callable[[str], None]]) -> Callable:
    """@brief Wrap a synchronous AI call with safe start/end/failure diagnostics.

    @param call_tool Callable receiving the original positional and keyword arguments.
    @param progress Optional string callback, invoked immediately at each lifecycle event.
    @return Callable preserving argument objects, result identity and original exceptions;
        the original callable itself when progress is None.
    @throws BaseException The original callable's exception, re-raised unchanged.
    @note Callback failures are best-effort and cannot replace a call result/error.
        Failure logs contain exception type only, never the exception message.
        Each wrapper numbers its own calls; elapsed time uses a monotonic clock.
        One retry is performed only when the OpenRouter boundary explicitly marks
        an HTTP failure as retryable. The retry is logged and uses the portable
        required-tool form when supported by the callable.
    """
    if progress is None:
        return call_tool
    try:
        signature = inspect.signature(call_tool)
    except (TypeError, ValueError):
        signature = None
    sequence = count(1)

    def emit(message):
        try:
            progress(message)
        except Exception:
            # A disconnected GUI/logger must not cancel a paid request or hide
            # its original outcome. In particular, never log the callback error.
            pass

    @wraps(call_tool)
    def logged_call(*args, **kwargs):
        """@brief Forward one invocation and emit only allowlisted diagnostics."""
        arguments = kwargs
        if signature is not None:
            try:
                bound = signature.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                arguments = {**kwargs, **bound.arguments}
            except TypeError:
                pass  # Let the actual callable raise its own argument error.
        tool = arguments.get("tool")
        name = None
        if type(tool) is dict:
            function = tool.get("function")
            name = function.get("name") if type(function) is dict else tool.get("name")
        context = (f"id={next(sequence)} model={_identifier(arguments.get('model'))} "
                   f"tool={_identifier(name)}")
        emit(f"AI_CALL_START {context}")
        started = perf_counter()
        try:
            result = call_tool(*args, **kwargs)
        except BaseException as exc:
            if getattr(exc, "retryable", False) is True:
                emit(f"AI_CALL_RETRY {context} attempt=2 reason=retryable_http")
                retry_kwargs = dict(kwargs)
                if signature is not None and "compatibility_retry" in signature.parameters:
                    retry_kwargs["compatibility_retry"] = True
                try:
                    result = call_tool(*args, **retry_kwargs)
                except BaseException as retry_exc:
                    elapsed = perf_counter() - started
                    emit(f"AI_CALL_FAILURE {context} elapsed={elapsed:.3f}s attempts=2 "
                         f"error={_identifier(type(retry_exc).__name__)}")
                    raise
            else:
                elapsed = perf_counter() - started
                emit(f"AI_CALL_FAILURE {context} elapsed={elapsed:.3f}s attempts=1 "
                     f"error={_identifier(type(exc).__name__)}")
                raise
        elapsed = perf_counter() - started
        emit(f"AI_CALL_END {context} elapsed={elapsed:.3f}s result={_result_summary(result)}")
        return result

    return logged_call
