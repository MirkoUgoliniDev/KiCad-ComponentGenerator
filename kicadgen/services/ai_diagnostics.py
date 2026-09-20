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
    """@brief Consuntivo dei token al momento della chiamata, se disponibile."""
    try:
        from .openrouter import usage_totals
        totals = usage_totals()
        return (totals.prompt_tokens, totals.completion_tokens, totals.cost_usd)
    except Exception:  # noqa: BLE001 - la diagnostica non deve mai far cadere una chiamata
        return None


def _usage_delta(before):
    """@brief Token e costo attribuibili alla chiamata appena conclusa.

    @param before Istantanea presa prima della chiamata, o None.
    @return Testo da accodare alla riga di diagnostica, eventualmente vuoto.
    @note Differenza di un totale globale: con chiamate in parallelo
        attribuirebbe a una cio' che ha speso un'altra. Oggi le chiamate
        dell'analisi sono in sequenza; se un giorno non lo fossero, questa
        riga andrebbe ricavata dalla risposta invece che dal totale.
    """
    after = _usage_snapshot()
    if before is None or after is None:
        return ""
    tokens_in, tokens_out, cost = (a - b for a, b in zip(after, before))
    return f" tokens_in={tokens_in} tokens_out={tokens_out} cost_usd={cost:.4f}"


def make_logged_call(call_tool: Callable, progress: Optional[Callable[[str], None]], *,
                     log_prompt: bool = False) -> Callable:
    """@brief Wrap a synchronous AI call with lifecycle and optional prompt diagnostics.

    @param call_tool Callable receiving the original positional and keyword arguments.
    @param progress Optional string callback, invoked immediately at each lifecycle event.
    @param log_prompt When true, log the exact textual request components and a
        hash/size summary for binary blocks.  Disabled by default for callers
        that do not explicitly opt into an audit trail.
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

    @wraps(call_tool)
    def logged_call(*args, **kwargs):
        """@brief Forward one invocation and emit allowlisted diagnostics."""
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
            digest, lines = _prompt_snapshot(arguments)
            emit(f"AI_CALL_PROMPT_BEGIN {context} sha256={digest} blocks={len(arguments.get('content_blocks') or [])}")
            for line in lines:
                emit(line)
            emit(f"AI_CALL_PROMPT_END {context} sha256={digest}")
        started = perf_counter()
        # Il consuntivo dei token e' globale: la differenza prima/dopo e'
        # l'unico modo per sapere quanto e' entrato in QUESTA chiamata. Serve
        # a vedere subito se un fornitore ha scartato le immagini: una
        # richiesta con quattro fotografie che costa poche migliaia di token
        # non le ha ricevute, per quanto la risposta sembri legittima.
        before = _usage_snapshot()
        try:
            result = call_tool(*args, **kwargs)
        except BaseException as exc:
            if getattr(exc, "retryable", False) is True:
                emit(f"AI_CALL_RETRY {context} attempt=2 reason=retryable_http")
                retry_kwargs = dict(kwargs)
                if signature is not None and "compatibility_retry" in signature.parameters:
                    retry_kwargs["compatibility_retry"] = True
                if log_prompt:
                    retry_arguments = bound_arguments(args, retry_kwargs)
                    retry_digest, retry_lines = _prompt_snapshot(retry_arguments)
                    emit(f"AI_CALL_PROMPT_BEGIN {context} attempt=2 sha256={retry_digest} blocks={len(retry_arguments.get('content_blocks') or [])}")
                    for line in retry_lines:
                        emit(line)
                    emit(f"AI_CALL_PROMPT_END {context} attempt=2 sha256={retry_digest}")
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
        emit(f"AI_CALL_END {context} elapsed={elapsed:.3f}s result={_result_summary(result)}"
             + _usage_delta(before))
        return result

    return logged_call
