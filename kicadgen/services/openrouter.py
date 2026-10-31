"""@brief Minimal OpenRouter client with privacy-safe provider diagnostics.

OpenRouter exposes, behind one key (OPENROUTER_API_KEY), the models of every
vendor (Anthropic, OpenAI, Google, ...) through an OpenAI-compatible API: the
model is chosen by passing its id (e.g. "anthropic/claude-sonnet-5") in the
request body.

This module holds only the bare minimum needed to extract parameters from a
datasheet:

* :func:`list_models`  -- the model list with prices and capabilities, used
  by the GUI to let the user pick a model filtered by cost;
* :func:`call_tool`    -- one chat/completions call with forced *tool
  calling*, returning the argument dictionary directly.

Only the standard library (``urllib``) is used: the API is HTTP+JSON, and
adding an external dependency for two calls would not be justified.
"""

from __future__ import annotations

import json
import http.client
from http.client import IncompleteRead
import math
import numbers
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import uuid
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from . import credentials
from .ai_diagnostics import CallCancelled
from .messages import tx
from ..app_paths import env_files, is_frozen, user_data_dir
from ..core.validation import finite_number
from ..fsutil import replace_file

API_BASE = "https://openrouter.ai/api/v1"
MODELS_URL = f"{API_BASE}/models"
CHAT_URL = f"{API_BASE}/chat/completions"
CREDITS_URL = f"{API_BASE}/credits"
KEY_STATUS_URL = f"{API_BASE}/auth/key"

API_KEY_ENV = "OPENROUTER_API_KEY"

#: Model used when the user has not chosen one yet.
DEFAULT_MODEL = "anthropic/claude-opus-5"

#: Local cache of the model list (it is ~1 MB long and changes rarely).
MODELS_CACHE_PATH = user_data_dir() / ".openrouter_models.json"
#: File name of the cache inside the user data directory; `_models_cache_path`
#: resolves it at use time, so a data directory chosen after import is honoured.
_MODELS_CACHE_NAME = ".openrouter_models.json"
_IMPORT_TIME_CACHE_PATH = MODELS_CACHE_PATH
MODELS_CACHE_TTL_S = 24 * 3600

# Optional OpenRouter headers: they identify the calling application in
# the statistics of its account.
_APP_HEADERS = {
    "HTTP-Referer": "https://github.com/kicadgen-tool",
    "X-Title": "KiCadGen",
}

_HTTP_TIMEOUT_MODELS_S = 30
_HTTP_TIMEOUT_CHAT_S = 300
_HTTP_TIMEOUT_CREDITS_S = 20
#: Largest successful (2xx) body the program decodes, in bytes. Error bodies
#: are already capped at 64 KiB and the update check at 1 MB; a successful
#: answer (a chat completion with tool arguments, the /models list of a few
#: MB, a credit status) has no reason to approach this size, so anything
#: larger is treated as an undecodable answer instead of being read into
#: memory without bound (review 2026-10-27, E6).
_MAX_RESPONSE_BODY_BYTES = 16 * 1024 * 1024


def _json_from_response(response: Any, limit: int = _MAX_RESPONSE_BODY_BYTES) -> Any:
    """@brief Decode the JSON body of a successful HTTP response, bounded in size.

    @param response The object returned by ``urllib.request.urlopen`` (or any
        object with ``read(n)``).
    @param limit Largest body accepted, in bytes.
    @return The decoded JSON value.
    @throws ValueError When the body is longer than ``limit`` or is not JSON:
        the callers already treat a ValueError as «risposta non decodificabile»
        (possibly billed for a chat call), which is also what an over-sized
        body is -- the answer arrived but cannot be used.
    @throws http.client.IncompleteRead, OSError As ``read`` raises them: the
        callers classify them as before.
    @note ``limit + 1`` bytes are requested so that a body of exactly
        ``limit`` bytes is accepted and a longer one is recognised without
        reading it to the end.
    """
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"HTTP body longer than {limit} bytes")
    return json.loads(data)

#: Minimum credit (USD) below which an extraction is not even started.
#: A single extraction with photo and crops typically costs a few cents, but
#: starting with the tank nearly empty means failing halfway through the
#: chain, after having already spent: better to stop first with a clear message.
MIN_CREDIT_USD = 0.50

#: Above MIN_CREDIT_USD but below this threshold the work starts anyway,
#: reporting that the credit is about to run out.
LOW_CREDIT_USD = 2.00

#: The credit changes only when money is spent: a chain of dozens of calls
#: must not query the endpoint at every step.
_CREDIT_CACHE_TTL_S = 60
_credit_cache: Optional[tuple[float, "CreditStatus"]] = None


class OpenRouterError(Exception):
    """@brief Report an OpenRouter failure without exposing request content.

    @param message Privacy-safe diagnostic text.
    @param retryable Whether the failure is transient (timeout, conflict, rate
        limit, 5xx): the SAME request may succeed after a backoff.
    @param retry_after Provider-requested wait in seconds (Retry-After
        header), or None when not given or not parseable.
    @param compatibility Whether the provider rejected the request FORM
        (HTTP 400 about tool_choice), so one retry in the portable
        required-tool form may succeed. A plain 400 is neither retryable nor
        compatibility: repeating an invalid request only pays for the refusal.
    @param http_status HTTP status of a refused status request, when known.
    @param unreachable Whether no HTTP answer arrived at all (network down,
        DNS, timeout).
    """

    def __init__(self, message: str, *, retryable: bool = False,
                 retry_after: Optional[float] = None, compatibility: bool = False,
                 http_status: Optional[int] = None, unreachable: bool = False):
        """@brief Build the error with its retry and reachability flags.

        @param message Privacy-safe diagnostic text.
        @param retryable Transient failure: the same request may succeed later.
        @param retry_after Provider-requested wait in seconds, or None.
        @param compatibility The request FORM was refused (tool_choice 400).
        @param http_status HTTP status of the refusal, or None.
        @param unreachable No HTTP answer arrived at all.
        """
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.compatibility = compatibility
        #: HTTP status of the refusal, or None when no HTTP answer arrived.
        self.http_status = http_status
        #: True when OpenRouter could not be reached at all (DNS, connection,
        #: timeout): the request, and so the key, were never judged.
        self.unreachable = unreachable
        #: True when the request was SENT and its answer was lost afterwards
        #: (read timeout, undecodable body): the provider may have billed it.
        #: Such a call is recorded as a paid call without cost
        #: (`_record_usage`), so every total says «at least» (audit 138, B10).
        self.possibly_billed = False


class MissingApiKeyError(OpenRouterError):
    """@brief No OpenRouter API key in any of the sources `api_key` reads.

    @note A subclass, so every caller that catches OpenRouterError keeps
        working; the GUI can tell this case apart and offer «Account AI...».
    """


class InvalidApiKeyError(MissingApiKeyError):
    """@brief The configured key cannot be a key (blank inside, line break, non-ASCII).

    @note Raised before any request is built: sent as a header, such a key
        makes http.client fail before the socket opens, which used to be
        read as an undecodable, possibly billed answer (review 2026-10-29,
        E1). A MissingApiKeyError subclass because the remedy is the same,
        paste the key again in «Account AI...», and every caller that offers
        that window for a missing key offers it for an unusable one too.
    """


class RequestCancelled(OpenRouterError, CallCancelled):
    """@brief The in-flight request was closed because the user cancelled the run.

    @note Raised when `abort_request` closed the connection of a request
        while it was being sent or while its answer was awaited. It is never
        retried (`retryable` stays False) and, when the request had already
        been sent, it is `possibly_billed` and recorded as a call without
        cost, like a lost answer (review 2026-10-29, E6). It is also a
        CallCancelled, so the pipeline treats it as the user's decision,
        never as a failure of the step.
    """


class ToolArgumentsError(OpenRouterError):
    """@brief The model answered with the requested tool, but its arguments are not a JSON object.

    @param message Privacy-safe diagnostic text.
    @param raw The arguments as the model sent them, for the stage audit
        (never logged: it is model output, possibly long); None when absent.
    @note A subclass, so every caller that catches OpenRouterError keeps
        working. It tells a broken answer apart from a refused or unreachable
        request: the same request can be answered well the next time, which
        a caller may decide to pay for once (HANDOFF point 137).
    """

    def __init__(self, message: str, *, raw=None):
        """@brief Build the error, keeping the unusable arguments for the log.

        @param message Privacy-safe diagnostic text.
        @param raw The raw arguments text of the tool call, or None when absent.
        """
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class ModelInfo:
    """@brief OpenRouter catalogue model with prices in USD per million tokens and capabilities.

    One model of the OpenRouter list, with what is needed to choose it.

    @note The prices delivered by the API are in dollars for a single token;
        here they are already converted to dollars for a million tokens, the
        unit in which prices are normally compared.
    """

    id: str
    name: str
    prompt_usd_per_mtok: float
    completion_usd_per_mtok: float
    context_length: int
    input_modalities: tuple[str, ...]
    supports_tools: bool
    #: Provider that serves the model (providers.PROVIDER_*); OpenRouter by default.
    provider: str = "openrouter"
    #: False when the provider does not declare capabilities (DeepSeek's own
    #: API): the model is neither known to fit nor known not to.
    capabilities_declared: bool = True

    @property
    def supports_images(self) -> bool:
        """@brief Return whether the model accepts image input.

        @return True when "image" is among the declared input modalities.
        """
        return "image" in self.input_modalities

    @property
    def supports_pdf(self) -> bool:
        """@brief Return whether the model accepts file (PDF) input.

        @return True when "file" is among the declared input modalities.
        """
        return "file" in self.input_modalities

    @property
    def is_free(self) -> bool:
        """@brief Return whether both prompt and completion prices are zero.

        @return True when neither price is positive (an unknown NaN price is
            never free).
        """
        return self.prompt_usd_per_mtok <= 0 and self.completion_usd_per_mtok <= 0

    def usable_for_datasheets(self) -> bool:
        """@brief Return whether the model supports tool calling plus PDF or image input for datasheets.

        @return True when the model can do the job: tool calling plus
            PDF/image input (DATASHEET_CAPABILITIES).
        @note The extraction sends mechanical drawings (images or PDF) and
            demands the answer as a tool call: a model without these two
            capabilities would fail the request, so the GUI hides it.
        """
        return self.usable_for(DATASHEET_CAPABILITIES)

    def usable_for(self, capabilities) -> bool:
        """@brief Return whether the model has every requested capability.

        @param capabilities Names among 'tools', 'images', 'files'.
        @return True when the model declares all of those capabilities.
        @throws ValueError When a name is not one of the known capabilities.
        @note The requirement depends on what the model will be asked to do,
            not on the field it is written in: the photo path sends only
            images, so demanding PDF reading as well would hide models that
            do that job perfectly well.
        """
        have = {"tools": self.supports_tools, "images": self.supports_images,
                "files": self.supports_pdf}
        unknown = set(capabilities) - set(have)
        if unknown:
            raise ValueError(tx("Capacita' di modello sconosciute: {sorted}", sorted=sorted(unknown)))
        return all(have[name] for name in capabilities)


#: What is needed to read a datasheet: structured answer, mechanical
#: drawings as images and attached documents.
DATASHEET_CAPABILITIES = ("tools", "images", "files")
#: What is needed on photographs: structured answer and image reading.
#: PDFs never enter this path.
PHOTO_CAPABILITIES = ("tools", "images")


#: Where the key in use comes from; values of `ApiKeySource.kind`.
KEY_SOURCE_ENVIRONMENT = "environment"
KEY_SOURCE_CREDENTIAL_MANAGER = "credential_manager"
KEY_SOURCE_DOTENV = "dotenv"
KEY_SOURCE_NONE = "none"


@dataclass(frozen=True)
class ApiKeySource:
    """@brief Where the OpenRouter key in use comes from, without the key itself.

    @note `kind` is one of the KEY_SOURCE_* constants. `location` names the
        place (variable name, credential target or .env path) and is empty
        for KEY_SOURCE_NONE. `stored` tells whether the Credential Manager
        holds a key even when another source outranks it; `credential_error`
        is the reason it could not be read, empty when the read worked.
    """

    kind: str
    location: str = ""
    stored: bool = False
    credential_error: str = ""


def _stored_key() -> tuple[str, str]:
    """The key saved in the Windows Credential Manager and the read error, if any.

    Never raises: a store that cannot be read must not hide a `.env` key.
    """
    if not credentials.is_supported():
        return "", ""
    try:
        return (credentials.read_secret(credentials.OPENROUTER_TARGET) or "").strip(), ""
    except credentials.CredentialError as exc:
        return "", str(exc)


def _dotenv_key() -> tuple[str, Optional[Path]]:
    """The key written in the first `.env` file that sets it, and that file.

    Same files and same parser as the launcher used to load into the process
    environment (`app_paths.env_files`, python-dotenv). Without python-dotenv
    no `.env` is read, exactly as before.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return "", None
    for path in env_files():
        try:
            if not path.is_file():
                continue
            value = dotenv_values(path).get(API_KEY_ENV)
        except (OSError, ValueError):
            continue
        if value and value.strip():
            return value.strip(), path
    return "", None


def _resolve_api_key() -> tuple[str, ApiKeySource]:
    """The key in use (empty when none) and its source, in `api_key` order."""
    environment = os.environ.get(API_KEY_ENV, "").strip()
    stored, credential_error = _stored_key()
    if environment:
        return environment, ApiKeySource(KEY_SOURCE_ENVIRONMENT, API_KEY_ENV, bool(stored),
                                          credential_error)
    if stored:
        return stored, ApiKeySource(KEY_SOURCE_CREDENTIAL_MANAGER,
                                    credentials.OPENROUTER_TARGET, True)
    from_file, path = _dotenv_key()
    if from_file:
        return from_file, ApiKeySource(KEY_SOURCE_DOTENV, str(path), False, credential_error)
    return "", ApiKeySource(KEY_SOURCE_NONE, "", False, credential_error)


def api_key_source() -> ApiKeySource:
    """@brief Tell where the OpenRouter key in use comes from, never the key itself.

    @return The source `api_key` would use now (KEY_SOURCE_NONE when there is
        no key), plus whether the Credential Manager holds a key.
    @note Reads the same sources as `api_key`, in the same order; no network.
    """
    return _resolve_api_key()[1]


def api_key() -> str:
    """@brief Return the OpenRouter API key, looked up in three sources in order.

    Resolution order (the first non-empty value wins):
    1. the process environment variable ``OPENROUTER_API_KEY`` -- a key set
       on purpose for this process (shell, CI, script) outranks the others;
    2. the Windows Credential Manager entry `credentials.OPENROUTER_TARGET`,
       written by the «Account AI...» window;
    3. the ``.env`` files of `app_paths.env_files()`, read directly -- the
       place existing installations keep the key, still valid.

    @return The key, stripped of surrounding blanks.
    @throws MissingApiKeyError (an OpenRouterError) when no source has a key;
        the message points to «Account AI...» and never contains a key.
    @note The launcher (`main.py`) loads the ``.env`` files into the process
        environment for the other variables they may set, but takes the
        OpenRouter key back out when it was not already in the environment:
        left there, a ``.env`` key would pose as source 1 and hide a key
        saved later with «Account AI...».
    @note A Credential Manager that cannot be read is not fatal: the lookup
        continues with the ``.env`` files and the read error is appended to
        the «no key» message.
    """
    key, source = _resolve_api_key()
    if not key:
        message = tx("Chiave OpenRouter non configurata: inseriscila da Configurazione → "
                     "«Account AI...». In alternativa imposta la variabile d'ambiente "
                     "{API_KEY_ENV} o scrivila nel file {path} (vedi .env.example).",
                     API_KEY_ENV=API_KEY_ENV, path=env_files()[-1] if not is_frozen()
                     else env_files()[0])
        if source.credential_error:
            message += " " + tx("Gestore credenziali di Windows non leggibile: {error}",
                                error=source.credential_error)
        raise MissingApiKeyError(message)
    _refuse_unusable_key(key, "OpenRouter", source.location)
    return key


def _key_problem(key: str) -> Optional[str]:
    """Why `key` cannot be sent as a key (see `providers.key_text_problem`), or None."""
    from .providers import key_text_problem
    return key_text_problem(key)


def _refuse_unusable_key(key: str, provider: str, location: str,
                         error_class: type = None) -> None:
    """Raise `error_class` (default InvalidApiKeyError) when a configured key cannot be a key.

    The check runs where the key is resolved, before any request exists: a
    key with a line break or a non-Latin-1 character makes http.client fail
    while it builds the headers, before the socket opens (review 2026-10-29,
    E1). The message names the provider and the source (variable, credential
    target or `.env` path), never the key, and tells the user to paste it
    again.
    """
    problem = _key_problem(key)
    if problem:
        raise (error_class or InvalidApiKeyError)(tx(
            "Chiave {provider} letta da {location} non utilizzabile: {problem} Inseriscila di "
            "nuovo da Configurazione → «Account AI...».",
            provider=provider, location=location, problem=problem))


#: Same test http.client applies to a header value (`_is_illegal_header_value`):
#: a line break not followed by a blank would split the header.
_ILLEGAL_HEADER_VALUE = re.compile(r"\n(?![ \t])|\r(?![ \t\n])")


def _refuse_unsendable_headers(request: urllib.request.Request) -> None:
    """Raise OpenRouterError when http.client would refuse a header of `request`.

    http.client encodes every header value in Latin-1 and rejects a bare
    line break; both failures happen inside ``urlopen`` before anything is
    sent, as a ValueError that the answer-decoding branch would read as a
    possibly billed answer. Checked before ``urlopen``, such a request is
    reported as never sent and never billed (review 2026-10-29, E1). The
    keys are already validated where they are resolved: this is the last
    guard for any other header. The message never carries a header value.
    """
    for _name, value in request.header_items():
        text = str(value)
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            bad = True
        else:
            bad = bool(_ILLEGAL_HEADER_VALUE.search(text))
        if bad:
            raise OpenRouterError(tx(
                "Richiesta non inviata: un'intestazione HTTP contiene caratteri non ammessi. "
                "Inserisci di nuovo la chiave da Configurazione → «Account AI...»."))


def _key_for_redaction() -> str:
    """The key in use for scrubbing error texts; empty when none or unreadable."""
    try:
        return _resolve_api_key()[0]
    except Exception:  # noqa: BLE001 - redaction must never replace the error it cleans
        return ""


@dataclass(frozen=True)
class CreditStatus:
    """@brief Remaining OpenRouter credit as the minimum of account balance and key limit.

    The remaining credit on OpenRouter, seen through two independent limits.

    @note The account balance (``/credits``) and the spending ceiling of the
        single key (``/auth/key``) are separate limits and do not coincide: a
        key still granted 13.73 USD on an account holding 11.92 can spend only
        11.92. The binding limit is therefore the **minimum** of the two, and
        that is what :attr:`remaining` exposes.
    @note A ``None`` value means "no limit of this kind" (a key without a
        ceiling) or "not reported by the API": in both cases it takes no part
        in the minimum, and when neither is known :attr:`remaining` stays
        ``None``.
    """

    account_remaining: Optional[float] = None
    key_remaining: Optional[float] = None
    total_credits: Optional[float] = None
    total_usage: Optional[float] = None
    is_free_tier: bool = False
    expires_at: str = ""
    #: Why the account balance (/credits) could not be read; empty when it
    #: was read. With a key without its own ceiling, `remaining` is then None
    #: for lack of data, not because there is no limit.
    account_error: str = ""

    @property
    def remaining(self) -> Optional[float]:
        """@brief Return the binding credit limit, or None when unknown.

        @return The minimum of the known account and key remainders in USD;
            None when neither is known.
        """
        known = [v for v in (self.account_remaining, self.key_remaining) if v is not None]
        return min(known) if known else None

    @property
    def limiting_factor(self) -> str:
        """@brief Name which of the two ceilings is the binding one.

        @return A translated label (key ceiling, account balance, or both when
            they coincide); an empty string when no limit is known.
        """
        remaining = self.remaining
        if remaining is None:
            return ""
        if self.key_remaining is not None and remaining == self.key_remaining:
            return tx("tetto della chiave") if self.account_remaining is None or \
                self.key_remaining < self.account_remaining else tx("account e chiave")
        return tx("saldo dell'account")

    def describe(self) -> str:
        """@brief Render the credit status as one line for the AI log.

        @return The translated line with the remaining USD and its limiting
            factor, the account usage when known and the key ceiling when it
            differs; or a line saying the API reported no limit.
        """
        remaining = self.remaining
        if remaining is None:
            return tx("Credito OpenRouter: nessun limite riportato dall'API.")
        detail = tx("Credito OpenRouter: {remaining} USD residui ({limit})",
                    remaining=f"{remaining:.2f}", limit=self.limiting_factor)
        if self.total_credits is not None and self.total_usage is not None:
            detail += tx("; account {total_usage:.2f}/{total_credits:.2f} USD usati",
                         total_usage=self.total_usage,
                         total_credits=self.total_credits)
        if self.key_remaining is not None and self.key_remaining != remaining:
            detail += tx("; chiave {key_remaining:.2f} USD", key_remaining=self.key_remaining)
        return detail + "."


def _calls(count: int) -> str:
    """@brief Render a call count with the singular or plural of the active language."""
    # Singular and plural are two whole sentences in the catalogue: each
    # language inflects them its own way.
    return tx("{count} chiamata", count=count) if count == 1 else tx("{count} chiamate", count=count)


@dataclass
class UsageTotals:
    """@brief Totals of one processing run: calls, tokens and actual cost reported by OpenRouter.

    @note The cost comes from OpenRouter with the response of every call
        (field ``usage.cost``, in USD), not from an estimate on the price
        lists: it already accounts for the provider that actually served the
        request, for the discounts on tokens read from the model cache and
        for the reasoning tokens.
    @note Responses served by the local disk cache do not pass through here
        and count as zero, which is correct: they never touched the API.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    calls_without_cost: int = 0

    @property
    def complete(self) -> bool:
        """@brief Tell whether every call reported its own cost.

        @return True when no call of the run lacks a cost.
        """
        return self.calls_without_cost == 0

    def describe(self) -> str:
        """@brief Render the run total as one line for the AI log.

        @return The translated line with the call count, the tokens and the
            cost in USD, or a line saying no API call was made.
        """
        if not self.calls:
            return (tx("Costo elaborazione AI: nessuna chiamata all'API (risposte servite "
                       "dalla cache locale)."))
        # Below one cent four digits are not enough to tell anything apart.
        amount = f"{self.cost_usd:.4f}" if self.cost_usd >= 0.01 else f"{self.cost_usd:.6f}"
        detail = (tx("Costo elaborazione AI: {amount} USD su {calls}; {prompt_tokens} token "
                     "in ingresso, {completion_tokens} in uscita",
                     amount=amount,
                     calls=_calls(self.calls),
                     prompt_tokens=self.prompt_tokens,
                     completion_tokens=self.completion_tokens))
        extra = []
        if self.cached_tokens:
            extra.append(tx("{cached_tokens} letti dalla cache del modello",
                            cached_tokens=self.cached_tokens))
        if self.reasoning_tokens:
            extra.append(tx("{reasoning_tokens} di ragionamento", reasoning_tokens=self.reasoning_tokens))
        if extra:
            detail += " (" + ", ".join(extra) + ")"
        if not self.complete:
            detail += (tx("; {calls} senza costo riportato, totale sottostimato",
                          calls=_calls(self.calls_without_cost)))
        return detail + "."


#: The extraction runs in a worker thread while the GUI stays alive: the
#: cost tally must be protected, not left to a non-atomic update.
_usage_lock = threading.Lock()
_usage_totals = UsageTotals()


def reset_usage() -> None:
    """@brief Start a new accounting run, discarding the previous totals."""
    global _usage_totals
    with _usage_lock:
        _usage_totals = UsageTotals()


def usage_totals() -> UsageTotals:
    """@brief Return a snapshot of the current run totals.

    @return A UsageTotals copy taken under the usage lock, independent of
        later updates.
    """
    with _usage_lock:
        return UsageTotals(**vars(_usage_totals))


def _usage_int(container: Any, key: str) -> int:
    """A positive token count for the run totals, 0 when absent or unusable.

    An integer too large for a float (``10**400``) is unusable, never an
    OverflowError: `finite_number` rejects it (review D5).
    """
    value = (container or {}).get(key) if isinstance(container, dict) else None
    number = finite_number(value)
    return int(number) if number is not None and number > 0 else 0


def _cached_prompt_tokens(usage: dict) -> int:
    """Input tokens served from the model's cache, wherever the provider reports them.

    OpenRouter uses ``prompt_tokens_details.cached_tokens``; DeepSeek's own
    API reports ``prompt_cache_hit_tokens`` at the top of `usage` (its docs
    also show it under ``prompt_tokens_details``). Same reading order as
    `deepseek_pricing.call_cost`; the first source that reports a positive
    count wins, so one call is never counted twice.
    """
    details = usage.get("prompt_tokens_details")
    return (_usage_int(usage, "prompt_cache_hit_tokens")
            or _usage_int(details, "prompt_cache_hit_tokens")
            or _usage_int(details, "cached_tokens"))


#: Called once per paid call, from the thread that made it (see
#: `set_usage_listener`). None when nobody listens.
_usage_listener = None


def set_usage_listener(listener) -> None:
    """@brief Be told about every paid call as soon as its answer arrives.

    @param listener Callable taking one dict (``cost_usd`` float or None,
        ``prompt_tokens`` and ``completion_tokens`` int or None when not
        reported, ``model``, ``time`` ISO UTC of the request, optional
        ``cost_source``), or None to stop.
    @note Called from the worker thread that made the call: a GUI must hop to
        its own thread (e.g. with a Qt signal). A listener that raises never
        breaks the call it is told about: the answer was paid and is used.
    """
    global _usage_listener
    _usage_listener = listener


def _token_count(usage: Any, key: str) -> Optional[int]:
    """A token count as reported, or None when absent or unusable.

    Unlike `_usage_int` (0, for the run totals), a missing or damaged count
    stays unknown: a recorded 0 would later be priced as a known cheap call
    (audit 138, B5).
    """
    value = usage.get(key) if isinstance(usage, dict) else None
    number = finite_number(value)
    if number is None or number < 0 or number != int(number):
        return None
    return int(value)


#: Request in flight on this thread (call_tool): when it was sent and to
#: which model, read by `_record_usage` for the record of the call.
_request_context = threading.local()


def _begin_request(model: str) -> datetime:
    """Remember, for this thread, that a request to `model` is being sent now."""
    _request_context.sent_at = datetime.now(timezone.utc)
    _request_context.model = model
    return _request_context.sent_at


def _end_request() -> None:
    """Forget the request of this thread (see `_begin_request`)."""
    _request_context.sent_at = None
    _request_context.model = ""


class _InflightRequest:
    """Connection of the paid request a thread has in flight, closable from another thread.

    `sock` is the socket currently carrying the request (None before the
    connection exists); `aborted` tells that `abort_request` closed it.
    Both are read and written only under `_inflight_lock`.
    """

    __slots__ = ("sock", "aborted")

    def __init__(self):
        """@brief A record with no connection yet and not aborted."""
        self.sock = None
        self.aborted = False


#: Paid requests in flight, by thread identifier (review 2026-10-29, E6).
_inflight: dict = {}
_inflight_lock = threading.Lock()
_closable_context_cache: Optional[ssl.SSLContext] = None


def _shutdown_socket(sock) -> None:
    """Wake every thread blocked on `sock`: shut both directions down, then close the handle.

    Shutdown alone wakes a blocked receive on POSIX but NOT on Windows
    (checked on Windows 11 / Python 3.13: a recv blocked on a shut-down
    socket stays blocked until its timeout); closing the handle wakes it on
    Windows. Both are done, each at the level of the C socket object:

    * the plain `socket.socket.shutdown`, because `ssl.SSLSocket.shutdown`
      also drops the TLS object the reading thread is still using;
    * the C-level close (`_socket.socket.close`), because the Python-level
      close does nothing while http.client holds a file made with
      ``makefile`` (io refs), and the TLS override would drop the TLS object.
      The C close sets the descriptor to -1 BEFORE releasing the handle, so
      a later close by http.client never closes a handle reused by another
      connection.

    A socket already closed raises OSError, ignored: nothing is left to wake.
    """
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        socket._socket.socket.close(sock)
    except (OSError, ValueError, TypeError):
        pass


def _track_socket(sock) -> None:
    """Remember `sock` as the connection of this thread's in-flight request, if any.

    Called by the TLS context of `_closable_context` with the raw socket
    before the handshake and with the TLS socket after it. A request already
    aborted gets its new socket shut down at once, so a cancellation that
    arrived while connecting is not lost.
    """
    with _inflight_lock:
        record = _inflight.get(threading.get_ident())
        if record is None:
            return
        record.sock = sock
        aborted = record.aborted
    if aborted:
        _shutdown_socket(sock)


class _ClosableContext(ssl.SSLContext):
    """TLS context that hands every connection it wraps to `_track_socket`."""

    def wrap_socket(self, sock, *args, **kwargs):
        """@brief Wrap `sock` as ssl.SSLContext does, registering it with the in-flight request.

        @param sock Connected TCP socket.
        @param args Positional arguments of ssl.SSLContext.wrap_socket.
        @param kwargs Keyword arguments of ssl.SSLContext.wrap_socket.
        @return The TLS socket.
        """
        # The handshake is run here, AFTER the TLS socket is tracked: done
        # inside wrap_socket it would run on a socket nobody can close (the
        # raw one is already detached), and a Cancel during a slow handshake
        # would wait for the connection timeout.
        handshake = kwargs.pop("do_handshake_on_connect", True)
        _track_socket(sock)
        wrapped = super().wrap_socket(sock, *args, do_handshake_on_connect=False, **kwargs)
        _track_socket(wrapped)
        if handshake:
            wrapped.do_handshake()
        return wrapped


def _closable_context() -> ssl.SSLContext:
    """The TLS context of the paid requests: http.client's default, plus socket tracking.

    Built once from the context http.client itself would create for an
    HTTP/1.1 connection (same certificates, verification flags and ALPN);
    only its class changes, to `_ClosableContext`, so the configuration
    cannot drift from the library's default.
    """
    global _closable_context_cache
    with _inflight_lock:
        if _closable_context_cache is None:
            factory = getattr(http.client, "_create_https_context", None)
            context = factory(11) if factory is not None else ssl.create_default_context()
            context.__class__ = _ClosableContext
            _closable_context_cache = context
        return _closable_context_cache


class _ClosableRequest:
    """Context manager registering this thread's paid request in `_inflight`."""

    def __enter__(self) -> _InflightRequest:
        self.record = _InflightRequest()
        with _inflight_lock:
            _inflight[threading.get_ident()] = self.record
        return self.record

    def __exit__(self, *exc_info) -> bool:
        with _inflight_lock:
            if _inflight.get(threading.get_ident()) is self.record:
                del _inflight[threading.get_ident()]
            self.record.sock = None
        return False


def abort_request(thread_id: int) -> bool:
    """@brief Close the connection of the paid request that thread `thread_id` has in flight.

    @param thread_id `threading.get_ident()` of the thread running the request.
    @return True when that thread had a request in flight (it is now
        aborted), False when it had none.
    @note Thread-safe; meant for the cancel path (`ai_diagnostics`), which
        runs in another thread while the request blocks in ``urlopen``. The
        blocked call then fails and raises RequestCancelled: never retried,
        possibly billed when the request had been sent (review 2026-10-29, E6).
    """
    with _inflight_lock:
        record = _inflight.get(thread_id)
        if record is None:
            return False
        record.aborted = True
        sock = record.sock
    if sock is not None:
        _shutdown_socket(sock)
    return True


def _cancelled_error(exc: OpenRouterError, provider: str) -> RequestCancelled:
    """The RequestCancelled raised in place of `exc` for a request closed by `abort_request`."""
    error = RequestCancelled(tx("Richiesta a {provider} interrotta dall'utente.",
                                provider=provider))
    error.possibly_billed = bool(getattr(exc, "possibly_billed", False))
    return error


def _record_usage(payload: Any, *, sent_at: Optional[datetime] = None,
                  model: str = "") -> None:
    """Add the usage declared by an API response to the run totals.

    It must be called for every response received, even one that later
    turns out to be unusable: a call that produced no tool call was paid
    all the same, and keeping quiet about it would make the totals lower
    than the truth. Also with `payload` None: request sent and response
    lost (read timeout), possibly billed, counted as a call without cost.

    `sent_at` is the time the request left (UTC) and goes into the record
    as ``time``: a deferred write (cost file busy) stays dated at the call,
    not at the write (audit 138, B10). When absent, the time registered by
    `_begin_request` in the same thread (call_tool) is used, then now.
    `model` applies when the response does not carry it, with the same
    fallback. The fallback keeps the one-argument form of this hook.
    """
    sent_at = sent_at or getattr(_request_context, "sent_at", None)
    model = model or getattr(_request_context, "model", "") or ""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    cost = _credit_number((usage or {}).get("cost")) if isinstance(usage, dict) else None
    # A negative cost is not a refund to subtract from the run: it is a
    # malformed report, counted as missing exactly like project_costs does.
    if cost is not None and cost < 0:
        cost = None
    # Every delta is computed BEFORE the lock: a value that cannot be read
    # must never leave the totals half-updated (review D5). The helpers return
    # 0 for anything unusable, so nothing below can raise.
    usage_dict = usage if isinstance(usage, dict) else {}
    prompt_delta = _usage_int(usage_dict, "prompt_tokens")
    completion_delta = _usage_int(usage_dict, "completion_tokens")
    cached_delta = _cached_prompt_tokens(usage_dict)
    reasoning_delta = _usage_int(usage_dict.get("completion_tokens_details"), "reasoning_tokens")
    with _usage_lock:
        _usage_totals.calls += 1
        if cost is None:
            _usage_totals.calls_without_cost += 1
        else:
            _usage_totals.cost_usd += cost
        _usage_totals.prompt_tokens += prompt_delta
        _usage_totals.completion_tokens += completion_delta
        _usage_totals.cached_tokens += cached_delta
        _usage_totals.reasoning_tokens += reasoning_delta
    listener = _usage_listener
    if listener is not None:
        moment = sent_at or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        record = {"cost_usd": cost, "model": str((payload.get("model") if isinstance(payload, dict)
                                              else "") or model or ""),
                  # None, not 0, when not reported or unusable (B5).
                  "prompt_tokens": _token_count(usage, "prompt_tokens"),
                  "completion_tokens": _token_count(usage, "completion_tokens"),
                  # ISO text: the record crosses a queued Qt signal.
                  "time": moment.astimezone(timezone.utc).isoformat(timespec="seconds")}
        if isinstance(usage, dict) and isinstance(usage.get("cost_source"), str):
            record["cost_source"] = usage["cost_source"]
        try:
            listener(record)
        except Exception:  # noqa: BLE001 - the call is paid and stays valid
            pass


def _credit_number(value: Any) -> Optional[float]:
    """A finite number from an API answer, or None (bools and overflowing ints excluded)."""
    return finite_number(value)


def _get_json(url: str, timeout: int, key: Optional[str] = None) -> dict:
    """Authenticated GET returning the JSON body; raises OpenRouterError.

    `key` is the key to use instead of the configured one (check of a key
    just pasted, not yet saved). The error tells whether an HTTP refusal
    arrived (`http_status`) or whether OpenRouter was not reached
    (`unreachable`).
    """
    if key is not None:
        # A pasted key is checked here, as the configured one is in
        # api_key: never sent when it cannot be a header (2026-10-29, E1).
        problem = _key_problem(key)
        if problem:
            raise OpenRouterError(problem)
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key if key is not None else api_key()}",
                 **_APP_HEADERS},
    )
    _refuse_unsendable_headers(request)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Bounded read (review 2026-10-27, E6): an over-sized body is a
            # ValueError, hence «risposta non decodificabile» below.
            payload = _json_from_response(response)
    except urllib.error.HTTPError as exc:
        status = exc.code if type(exc.code) is int else None
        raise OpenRouterError(_error_message(exc), http_status=status) from None
    except urllib.error.URLError as exc:
        raise OpenRouterError(tx("connessione fallita: {safe_error_text}",
                                 safe_error_text=_safe_error_text(exc.reason))
                              + _diagnostic_context(), unreachable=True) from None
    except (ValueError, OSError, http.client.HTTPException) as exc:
        # An OSError is a transport failure (read timeout, connection
        # closed): the answer did not arrive. A ValueError or a malformed
        # status line (HTTPException) is an answer that arrived broken.
        raise OpenRouterError(tx("risposta non decodificabile") + _diagnostic_context(),
                              unreachable=isinstance(exc, OSError)) from None
    return payload if isinstance(payload, dict) else {}


def credit_status(force_refresh: bool = False) -> CreditStatus:
    """@brief Query both credit endpoints (cached) and return the effective credit constraint.

    @param force_refresh True ignores the cached status and queries the API
        again; False (default) reuses a status younger than
        :data:`_CREDIT_CACHE_TTL_S` seconds.
    @return The CreditStatus read from the endpoints that answered.
    @throws OpenRouterError Only when BOTH endpoints fail, so that the caller
        can tell "credit exhausted" from "credit not verifiable".
    @note The two endpoints are independent: one answering is enough for the
        check to have a limit to apply.
    @note The result stays cached for :data:`_CREDIT_CACHE_TTL_S` seconds: a
        chain of dozens of calls must not query the API at every step.
    """
    global _credit_cache
    if not force_refresh and _credit_cache is not None:
        stamp, cached = _credit_cache
        if time.time() - stamp <= _CREDIT_CACHE_TTL_S:
            return cached

    failures = []
    account_error = ""
    account = (None, None, None)
    try:
        account = _account_balance(None)
    except OpenRouterError as exc:
        account_error = str(exc)
        failures.append(tx("saldo account ({exc})", exc=exc))

    limits = (None, False, "")
    try:
        limits = _key_limits(None)
    except OpenRouterError as exc:
        failures.append(tx("stato chiave ({exc})", exc=exc))

    if len(failures) == 2:
        raise OpenRouterError("; ".join(failures))

    status = _status_from(account, limits, account_error)
    _credit_cache = (time.time(), status)
    return status


def _account_balance(key: Optional[str]) -> tuple:
    """(remaining, total_credits, total_usage) from /credits; raises OpenRouterError."""
    data = _get_json(CREDITS_URL, _HTTP_TIMEOUT_CREDITS_S, key).get("data") or {}
    total_credits = _credit_number(data.get("total_credits"))
    total_usage = _credit_number(data.get("total_usage"))
    remaining = (total_credits - total_usage
                 if total_credits is not None and total_usage is not None else None)
    return remaining, total_credits, total_usage


def _key_limits(key: Optional[str]) -> tuple:
    """(key_remaining, is_free_tier, expires_at) from /auth/key; raises OpenRouterError."""
    data = _get_json(KEY_STATUS_URL, _HTTP_TIMEOUT_CREDITS_S, key).get("data") or {}
    # limit = None means a key without its own cap: it is not a constraint.
    key_remaining = (_credit_number(data.get("limit_remaining"))
                     if _credit_number(data.get("limit")) is not None else None)
    return key_remaining, bool(data.get("is_free_tier")), str(data.get("expires_at") or "")


def _status_from(account: tuple, limits: tuple, account_error: str) -> "CreditStatus":
    """CreditStatus from the two partial readings."""
    account_remaining, total_credits, total_usage = account
    key_remaining, is_free_tier, expires_at = limits
    return CreditStatus(
        account_remaining=account_remaining, key_remaining=key_remaining,
        total_credits=total_credits, total_usage=total_usage,
        is_free_tier=is_free_tier, expires_at=expires_at, account_error=account_error,
    )


def invalidate_credit_cache() -> None:
    """@brief Forget the cached credit status, e.g. after the key in use changed.

    @note The cache belongs to the key in use: a key saved or removed with
        «Account AI...» must not inherit the previous key's credit.
    """
    global _credit_cache
    _credit_cache = None


def key_rejected(error: OpenRouterError) -> bool:
    """@brief Tell whether OpenRouter answered that the key itself is not valid.

    @param error The failure of a key check.
    @return True for HTTP 401 and 403, the answers to a missing, revoked or
        unknown key. False when OpenRouter was unreachable or failed for
        another reason: then the key was never judged.
    """
    return error.http_status in (401, 403)


def verify_api_key(key: str) -> "CreditStatus":
    """@brief Check a key against OpenRouter without spending credit.

    @param key The key to check, typically pasted and not yet saved; the key
        in use is neither read nor changed.
    @return Credit and limits read with that key. The account balance is
        best effort (`account_error` says why it is missing); the key status
        is required.
    @throws OpenRouterError When the key is empty or /auth/key fails: see
        `key_rejected` (HTTP 401/403) and `OpenRouterError.unreachable` to
        tell a refused key from a network that is down. The message never
        contains the key.
    @note Only the two free status endpoints are called (/auth/key and
        /credits): no model, no tokens, no cost. The result is not cached,
        since it may belong to a key other than the one in use.
    """
    key = (key or "").strip()
    if not key:
        raise OpenRouterError(tx("La chiave è vuota."))
    # A key that cannot be a header is refused here, never sent: http.client
    # would fail before the socket opens (review 2026-10-29, E1).
    problem = _key_problem(key)
    if problem:
        raise OpenRouterError(problem)
    limits = _key_limits(key)
    account_error = ""
    account = (None, None, None)
    try:
        account = _account_balance(key)
    except OpenRouterError as exc:
        account_error = str(exc)
    return _status_from(account, limits, account_error)


def check_credit(minimum_usd: float = MIN_CREDIT_USD, progress=None,
                 force_refresh: bool = False) -> Optional[CreditStatus]:
    """@brief Check the credit before spending; raise OpenRouterError only when it is known to be too low.

    @param minimum_usd Credit, in USD, below which the work must not start.
    @param progress Optional callable(str) receiving the status line and the
        warnings; None discards them.
    @param force_refresh Forwarded to `credit_status`: True queries the API
        again instead of using the cached status.
    @return The status read, or None when it could not be read.
    @throws OpenRouterError When the credit is known and below `minimum_usd`.
    @note Three distinct outcomes, because they must be treated differently:

        * credit known and below `minimum_usd` -> OpenRouterError: the work
          does not start, instead of failing halfway through the chain after
          having already spent;
        * credit known but low -> passes, with a warning on `progress`;
        * credit **not verifiable** (network down, endpoint changed) ->
          passes with a warning. A courtesy check must not make the
          application less usable than it was before the check existed: the
          API itself refuses the call when there really is no credit.
    """
    log = progress or (lambda message: None)
    try:
        status = credit_status(force_refresh=force_refresh)
    except OpenRouterError as exc:
        log(tx("Credito OpenRouter non verificabile: {exc}. Si prosegue comunque.", exc=exc))
        return None

    log(status.describe())
    remaining = status.remaining
    if remaining is not None and remaining < minimum_usd:
        raise OpenRouterError(
            tx("Credito OpenRouter insufficiente: {remaining:.2f} USD residui "
               "({limiting_factor}), ne servono almeno {minimum_usd:.2f}. Ricarica il "
               "credito su https://openrouter.ai/settings/credits oppure alza il "
               "tetto di spesa della chiave.",
               remaining=remaining,
               limiting_factor=status.limiting_factor,
               minimum_usd=minimum_usd)
        )
    if remaining is not None and remaining < LOW_CREDIT_USD:
        log(tx("Attenzione: credito quasi esaurito ({remaining:.2f} USD). "
               "L'estrazione parte, ma potrebbe non bastare per quelle successive.",
               remaining=remaining))
    return status


def _parse_price(value: Any) -> float:
    """Price of one token (string from the API) -> dollars for a million tokens.

    A nonfinite price ("nan", "inf") is not a price: it is unknown (NaN, the
    same marker DeepSeek's models carry) and never free, so the model is not
    offered as a free one. An absent or unparsable price keeps the historical
    0.0; the negative sentinels of the "auto" models are 0 as well.
    """
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(price):
        return math.nan
    # The "auto" router models publish negative sentinel prices (the real
    # price depends on the model chosen at call time): they count as 0 here.
    return price * 1_000_000 if price > 0 else 0.0


def _price_sort_key(price: float) -> tuple:
    """Sort key for a price: known prices first in ascending order, unknown (NaN) last."""
    return (0, price) if math.isfinite(price) else (1, 0.0)


def _model_from_payload(raw: Any) -> Optional[ModelInfo]:
    """One catalog entry as ModelInfo; None for an entry that cannot be used.

    Malformed entries (not an object, id not a string, odd containers) are
    skipped one by one, so a single bad record never hides the others.
    """
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    context = raw.get("context_length")
    try:
        context_length = (int(context) if not isinstance(context, bool) and context is not None
                          and math.isfinite(float(context)) and float(context) > 0 else 0)
    except (TypeError, ValueError, OverflowError):
        context_length = 0
    modalities = architecture.get("input_modalities")
    parameters = raw.get("supported_parameters")
    name = raw.get("name")
    return ModelInfo(
        id=model_id,
        name=name if isinstance(name, str) and name else model_id,
        prompt_usd_per_mtok=_parse_price(pricing.get("prompt")),
        completion_usd_per_mtok=_parse_price(pricing.get("completion")),
        context_length=context_length,
        input_modalities=tuple(m for m in modalities if isinstance(m, str))
        if isinstance(modalities, (list, tuple)) else (),
        supports_tools=isinstance(parameters, (list, tuple)) and "tools" in parameters,
    )


def _models_cache_path() -> Path:
    """Where the model list cache lives NOW.

    MODELS_CACHE_PATH is kept for callers that patch it (tests, tools); when
    it still holds its import-time default the path is recomputed, so a user
    data directory configured after import is honoured.
    """
    default = _IMPORT_TIME_CACHE_PATH
    if MODELS_CACHE_PATH != default:
        return MODELS_CACHE_PATH
    return user_data_dir() / _MODELS_CACHE_NAME


def _models_list(payload: Any) -> Optional[list]:
    """The model list of a /models payload or cache file; None when unusable.

    Accepts the API envelope {"data": [...]} and the bare list the cache
    stores; any other shape is unusable rather than an exception.
    """
    if isinstance(payload, dict):
        payload = payload.get("data")
    return payload if isinstance(payload, list) else None


def _load_models_cache(ignore_ttl: bool = False) -> Optional[list[dict]]:
    """Read the disk cache; None when it is missing, unreadable, corrupt or expired (unless ignore_ttl)."""
    path = _models_cache_path()
    if not path.is_file():
        return None
    try:
        if not ignore_ttl:
            age = time.time() - path.stat().st_mtime
            if age > MODELS_CACHE_TTL_S:
                return None
        with open(path, "r", encoding="utf-8") as fh:
            return _models_list(json.load(fh))
    except (OSError, ValueError):
        return None


def _write_models_cache(payload: list[dict]) -> None:
    """Atomic cache write: a temporary file in the same directory, then fsutil.replace_file.

    An interrupted write can never leave a truncated cache behind; the parent
    directory is created when missing. The temporary name is unique per write
    (process id plus a random suffix), so two threads of the same process
    refreshing the list at once never write into the same temporary file.
    """
    path = _models_cache_path()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        replace_file(temporary, path)
    except OSError:
        # The cache is an optimisation: if it cannot be written, never mind.
        try:
            temporary.unlink()
        except OSError:
            pass


def list_models(force_refresh: bool = False) -> list[ModelInfo]:
    """@brief Return the OpenRouter models sorted by cost, using a one-day disk cache and offline fallback.

    @param force_refresh True ignores the disk cache and rewrites it; False
        (default) uses a cache younger than one day.
    @return The available models as ModelInfo, sorted by prompt price, then
        completion price, then id.
    @throws OpenRouterError When the list cannot be downloaded and no cached
        list, even an expired one, exists.
    @note The /models endpoint is public (no API key needed) and returns the
        prices and input modalities of every model: that is what lets the
        GUI filter by cost before choosing.
    @note When the network does not answer, the cache is used even when
        expired, so the model choice stays possible offline.
    """
    payload = None if force_refresh else _load_models_cache()
    if payload is None:
        request = urllib.request.Request(MODELS_URL, headers=_APP_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_MODELS_S) as response:
                # Bounded read (review 2026-10-27, E6): an over-sized list is
                # a ValueError, handled with the cache like a damaged one.
                payload = _models_list(_json_from_response(response))
            if payload is None:
                raise ValueError("unexpected /models payload shape")
        except (urllib.error.URLError, ValueError, OSError) as exc:
            payload = _load_models_cache(ignore_ttl=True)
            if payload is None:
                detail = (_error_message(exc) if isinstance(exc, urllib.error.HTTPError)
                          else _safe_error_text(getattr(exc, "reason", None)) + _diagnostic_context())
                raise OpenRouterError(tx("Elenco modelli non scaricabile: ")
                                      + detail) from None
        else:
            _write_models_cache(payload)

    models = [m for m in (_model_from_payload(raw) for raw in payload) if m is not None]
    models.sort(key=lambda m: (_price_sort_key(m.prompt_usd_per_mtok),
                               _price_sort_key(m.completion_usd_per_mtok), m.id))
    return models


def _safe_error_text(value):
    """Return a fixed error category, never arbitrary provider/request text.

    Redaction alone cannot identify an echoed private prompt. After removing
    known credentials/media, recognize only fixed categories and discard the
    remainder. Do not stringify containers or exception objects.
    """
    if not isinstance(value, str):
        return tx("dettagli del provider omessi")
    key = _key_for_redaction()
    if key:
        value = value.replace(key, "[redacted]")
    value = value[:65536]
    value = re.sub(r"(?i)\b(?:authorization\s*[:=]\s*|bearer\s+)\S+", "[redacted]", value)
    value = re.sub(r"(?i)\bsk-[a-z0-9_-]+|data:[^\s]+", "[redacted]", value)
    lower = value.lower()
    for phrase in ("credito esaurito", "insufficient credits", "insufficient quota",
                   "rate limit", "too many requests", "invalid api key", "unauthorized",
                   "authentication failed", "context length", "content filter",
                   "bad gateway", "service unavailable", "timed out", "timeout",
                   "image exceeds", "image too large", "maximum image size",
                   "invalid base64", "invalid image", "unsupported image",
                   "invalid schema", "invalid tool", "tool_choice",
                   "max_tokens", "too many images", "request too large",
                   "invalid_request_error", "provider returned error",
                   "content-length header is required"):
        if phrase in lower:
            return phrase
    return tx("dettagli del provider omessi")


def _diagnostic_context(payload=None, status=None):
    """Allowlist completion reasons and provider codes without inspecting raw metadata."""
    payload = payload if isinstance(payload, dict) else {}
    error = payload.get("error")
    error = error if isinstance(error, dict) else {}
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    code = error.get("code", metadata.get("code", status))
    allowed_codes = {"rate_limit_exceeded", "insufficient_quota", "invalid_api_key",
                     "authentication_error", "permission_denied", "invalid_request_error",
                     "context_length_exceeded", "content_filter", "server_error",
                     "model_not_found", "provider_error", "timeout"}
    if type(code) is int and 100 <= code <= 599:
        code = str(code)
    elif type(code) is str and re.fullmatch(r"[1-5][0-9]{2}", code):
        pass
    elif not isinstance(code, str) or code not in allowed_codes:
        code = "unknown"
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    allowed_reasons = {"stop", "length", "tool_calls", "function_call", "content_filter",
                       "error", "max_tokens", "end_turn", "stop_sequence", "tool_use"}
    reason = choice.get("finish_reason")
    if not isinstance(reason, str) or reason not in allowed_reasons:
        reason = "unknown"
    native = choice.get("native_finish_reason")
    suffix = f"; native_finish_reason={native}" if isinstance(native, str) and native in allowed_reasons else ""
    return f" [provider_code={code}; finish_reason={reason}{suffix}]"


def _error_message(exc: urllib.error.HTTPError) -> str:
    """Read a bounded error body and expose only safe categories and codes."""
    status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else "unknown"
    try:
        body = json.loads(exc.read(65536).decode("utf-8", "replace"))
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
        metadata = error.get("metadata", {}) if isinstance(error, dict) else {}
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        nested = raw.get("error", raw) if isinstance(raw, dict) else raw
        detail = nested.get("message") if isinstance(nested, dict) else nested
        # Provider envelopes often hide the actionable reason in metadata.raw.
        # Log fixed categories and numeric limits only, never echoed input.
        category = _safe_error_text(detail)
        limits = ""
        if isinstance(detail, str):
            matches = re.findall(r"(?i)\b\d+(?:\.\d+)?\s*(?:MB|MiB|bytes|pixels)\b", detail[:65536])
            if matches:
                limits = "; limits=" + ",".join(matches[:6])
        previous = metadata.get("previous_errors", []) if isinstance(metadata, dict) else []
        history = []
        for item in previous[:8] if isinstance(previous, list) else []:
            if isinstance(item, dict):
                history.append(_safe_error_text(item.get("raw")))
        previous_text = "; previous_errors=" + ",".join(history) if history else ""
        return (f"HTTP {status} {_safe_error_text(message)}; provider_detail={category}{limits}{previous_text}"
                + _diagnostic_context(body, status))
    except (ValueError, OSError):
        pass
    return f"HTTP {status} {_safe_error_text(exc.reason)}" + _diagnostic_context(status=status)


#: HTTP statuses of transient failures: the same request may succeed later.
#: 400 is deliberately absent (see OpenRouterError.compatibility).
TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    """Provider Retry-After in seconds (delta-seconds or HTTP date), or None."""
    try:
        value = exc.headers.get("Retry-After") if exc.headers is not None else None
    except Exception:  # noqa: BLE001 - header objects of odd shape are just absent
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            when = parsedate_to_datetime(value.strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _post_chat(request: urllib.request.Request, content_blocks: Sequence[dict]) -> Any:
    """Send one chat request to OpenRouter and decode its JSON answer.

    The request goes through `_closable_context`, so `abort_request` can
    close it from the cancel path. Every failure becomes an OpenRouterError:

    * HTTPError -> the provider's refusal, retryable for a transient status;
    * URLError -> nothing was sent: urllib wraps every failure while SENDING
      the request in URLError (checked on Python 3.13, `do_open`);
    * any other OSError (read timeout, RemoteDisconnected, connection reset
      while waiting) and any HTTPException or ValueError (malformed status
      line, truncated or undecodable body) come AFTER the request was sent:
      the provider may have billed it, so the call is recorded as a call
      without cost and marked `possibly_billed` (audit 138 B10; review
      2026-10-29, E5).
    """
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_CHAT_S,
                                    context=_closable_context()) as response:
            # Bounded read (review 2026-10-27, E6): an over-sized answer is a
            # ValueError, hence a possibly billed undecodable answer below.
            return _json_from_response(response)
    except urllib.error.HTTPError as exc:
        status = exc.code if type(exc.code) is int else 0
        image_sizes = [len(block.get("image_url", {}).get("url", ""))
                       for block in content_blocks if block.get("type") == "image_url"]
        error_message = _error_message(exc)
        raise OpenRouterError(
            tx("OpenRouter ha risposto con un errore: {error_message} "
               "[request_bytes={n_data}; images={n_image_sizes}; "
               "largest_image_encoded_bytes={max}]",
               error_message=error_message,
               n_data=len(request.data),
               n_image_sizes=len(image_sizes),
               max=max(image_sizes, default=0)),
            retryable=status in TRANSIENT_HTTP_STATUSES,
            retry_after=_retry_after(exc) if status in TRANSIENT_HTTP_STATUSES else None,
            # Only a 400 whose safe category names the tool_choice parameter is
            # attributable to the request form that the compatibility retry
            # changes; any other 400 would be refused again, and paid again.
            compatibility=status == 400 and "tool_choice" in error_message,
        ) from None
    except urllib.error.URLError as exc:
        raise OpenRouterError(tx("Connessione a OpenRouter fallita: {reason}",
                                 reason=_safe_error_text(exc.reason))
                              + _diagnostic_context()) from None
    except OSError as exc:
        # Sent, then the connection failed while waiting for or reading the
        # answer: possibly billed, never silently free (B10, E5).
        _record_usage(None)
        error = OpenRouterError(tx("Connessione a OpenRouter fallita: {reason}",
                                   reason=_safe_error_text(str(exc)))
                                + _diagnostic_context())
        error.possibly_billed = True
        raise error from None
    except (ValueError, http.client.HTTPException):
        # The answer arrived but is incomplete or cannot be decoded: a
        # potentially paid call without cost, never a silently free one.
        _record_usage(None)
        error = OpenRouterError(tx("Risposta non decodificabile da OpenRouter.")
                                + _diagnostic_context())
        error.possibly_billed = True
        raise error from None


#: The `tool_choice` forms of the compatibility retries, in order (see
#: `_call_tool`, @param compatibility_retry).
_COMPATIBLE_TOOL_CHOICES = ("required", "auto")
#: Number of compatibility retries `_call_tool` has a form for.
COMPATIBILITY_LEVELS = len(_COMPATIBLE_TOOL_CHOICES)


def call_tool(*args, **kwargs) -> dict:
    """@brief Call the model once and decode explicit structured tool arguments.

    @param args Positional parameters of `_call_tool` (model, system_prompt,
        content_blocks, tool, max_tokens, has_pdf, pdf_engine,
        compatibility_retry), forwarded unchanged.
    @param kwargs Keyword parameters of `_call_tool`, forwarded unchanged.
    @return The decoded argument object returned by `_call_tool`.
    @throws OpenRouterError Propagated from `_call_tool` (safe network or
        provider diagnostics, invalid or missing arguments).
    @throws ProviderError Propagated from `_call_tool` (a direct-provider
        request that cannot be served, e.g. a PDF sent to DeepSeek).
    @note Same parameters, result and errors as `_call_tool`, which does the
        work; this wrapper only guarantees that the request context read by
        `_record_usage` (time and model of the call) never outlives the call.
    """
    try:
        return _call_tool(*args, **kwargs)
    finally:
        _end_request()


def _call_tool(
    model: str,
    system_prompt: str,
    content_blocks: Sequence[dict],
    tool: dict,
    max_tokens: int = 2048,
    has_pdf: bool = False,
    pdf_engine: str = "native",
    compatibility_retry: int = 0,
) -> dict:
    """@brief Call the model once and decode explicit structured tool arguments.

    @param model Provider model identifier.
    @param system_prompt Private instructions, excluded from error diagnostics.
    @param content_blocks Request text/images/files, excluded from error diagnostics.
    @param tool OpenAI-compatible function schema.
    @param max_tokens Maximum completion tokens.
    @param has_pdf Whether PDF parsing is required.
    @param pdf_engine PDF parser selection.
    @param compatibility_retry Form of `tool_choice` (True counts as 1): 0
        names the tool; 1 is the portable «required»; 2 is «auto», for models
        that refuse any forced tool (Claude 5.5 through OpenRouter answers
        HTTP 400 «tool_choice: type "tool" and "any" are not supported» to
        both other forms and cannot turn its reasoning off, real calls of
        2026-10-30). With «auto» the system prompt still asks for the tool,
        and an answer without the call is the usual missing-tool error.
    @return Explicit argument object; an explicitly supplied empty object is valid.
    @throws OpenRouterError Safe network/provider diagnostics or invalid/missing arguments.
    @throws ProviderError When the direct DeepSeek API is asked to read a PDF,
        which it cannot do.
    @note Retry orchestration belongs to the logged application wrapper; paid response
        usage is recorded before argument validation.
    @note `tool` is the schema in OpenAI format
        (``{"type": "function", "function": {"name", "description", "parameters"}}``);
        the return value is the argument dictionary already decoded, so the
        caller never has to parse free text.
    @note `pdf_engine` matters only when a PDF is among the files: "native"
        leaves the PDF to the model (indispensable to read mechanical
        drawings, which are images inside the PDF), whereas the other
        OpenRouter engines would extract only its text.
    """
    from .providers import (DEEPSEEK_BETA_CHAT_URL, DEEPSEEK_CHAT_URL, PROVIDER_DEEPSEEK,
                            ProviderError, deepseek_request, provider_model_name, provider_of)
    body: dict[str, Any] = {
        "model": provider_model_name(model),
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": list(content_blocks)},
        ],
        "tools": [tool],
        "tool_choice": (_COMPATIBLE_TOOL_CHOICES[min(int(compatibility_retry), 2) - 1]
                        if compatibility_retry else
                        {"type": "function", "function": {"name": tool["function"]["name"]}}),
    }
    if provider_of(model) == PROVIDER_DEEPSEEK:
        # DeepSeek direct API (OpenAI compatible): no PDF plugin and no cost
        # in the answer; the cost is computed from the tokens (below).
        if has_pdf:
            raise ProviderError(tx("L'API diretta di DeepSeek non legge i PDF: per un "
                                   "datasheet PDF scegli un modello OpenRouter che li legga."))
        # Reasoning rejects any tool_choice (HTTP 400, 27/09 and 29/09) and
        # consumes paid tokens: the structured answer matters more.
        # Always off (user, 2026-10-02: the «Ragionamento DeepSeek» (DeepSeek
        # reasoning) checkbox of point 161 was removed).
        body["thinking"] = {"type": "disabled"}
        # Without a schema-enforcing decoder deepseek-flash broke the SHAPE of
        # 25-45 % of the single-part answers (pads dropped, or one pad's
        # fields flattened to the top level; real calls of 2026-10-31); in
        # strict mode 1 of 16. Used whenever the schema converts losslessly.
        strict = strict_tool(tool)
        if strict is not None:
            body["tools"] = [strict]
        sent_at = _begin_request(model)
        try:
            try:
                payload = deepseek_request(DEEPSEEK_BETA_CHAT_URL if strict is not None
                                           else DEEPSEEK_CHAT_URL, body,
                                           timeout=_HTTP_TIMEOUT_CHAT_S)
            except ProviderError as exc:
                if strict is None or exc.http_status not in _STRICT_REFUSED_STATUSES:
                    raise
                # The beta endpoint refused the strict request (schema it does
                # not take, endpoint changed or withdrawn): refused, not
                # billed. The same request once more in normal mode.
                strict = None
                body["tools"] = [tool]
                payload = deepseek_request(DEEPSEEK_CHAT_URL, body, timeout=_HTTP_TIMEOUT_CHAT_S)
        except (ProviderError, RequestCancelled) as exc:
            if exc.possibly_billed:
                # Sent, answer lost: maybe billed, never silently free (B10).
                _record_usage(None)
            raise
        if isinstance(payload, dict):
            # The full identifier, not the name DeepSeek answers with: this
            # way the cost file recognises the provider of every call.
            payload["model"] = model
            # DeepSeek does not report the cost: it is computed as its billing
            # does, tokens x official list price (HANDOFF 128).
            from .deepseek_pricing import call_cost
            usage = payload.get("usage")
            # Priced at the time the request was sent (peak / off-peak hours).
            cost = call_cost(provider_model_name(model), usage, sent_at)
            if isinstance(usage, dict) and cost is not None:
                usage["cost"], usage["cost_source"] = cost, "token_price"
        _record_usage(payload)
        arguments = tool_arguments(payload, tool["function"]["name"], provider="DeepSeek")
        return _drop_nulls(arguments) if strict is not None else arguments
    if has_pdf:
        body["plugins"] = [{"id": "file-parser", "pdf": {"engine": pdf_engine}}]
    # Without this flag the answer carries the tokens but not the cost, and
    # the final tally would have to estimate it from price lists instead of reading it.
    body["usage"] = {"include": True}

    request = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            **_APP_HEADERS,
        },
        method="POST",
    )
    # Outside the try: a header http.client would refuse is a request never
    # sent, never billed, not an undecodable answer (review 2026-10-29, E1).
    _refuse_unsendable_headers(request)
    _begin_request(model)
    with _ClosableRequest() as inflight:
        try:
            payload = _post_chat(request, content_blocks)
        except OpenRouterError as exc:
            if inflight.aborted:
                # Closed by the user's Cancel (E6): its usage, when it had
                # been sent, is already recorded by _post_chat.
                raise _cancelled_error(exc, "OpenRouter") from None
            raise

    # Before tool_arguments, which may raise: the call was paid anyway, and
    # a cost tally that ignores the failed attempts is false.
    _record_usage(payload)
    return tool_arguments(payload, tool["function"]["name"])


#: JSON-schema keywords a schema node may use to be converted to DeepSeek
#: strict mode (its documented support: types, properties/required, items,
#: enum, numeric bounds, pattern, format, const, default). Anything else --
#: lengths, combinators, references, nullable, free-key maps -- would be
#: dropped or refused, so such a schema is sent in normal mode, as before.
_STRICT_KEYWORDS = frozenset({
    "type", "description", "properties", "required", "additionalProperties", "items",
    "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "pattern", "format", "const", "default", "title"})

#: HTTP statuses of a refused strict request worth one normal-mode resend
#: (bad request, endpoint not found, unprocessable schema). Key, balance and
#: rate errors (401, 402, 429) would fail the same way on the normal endpoint.
_STRICT_REFUSED_STATUSES = frozenset({400, 404, 422})


def strict_tool(tool: dict) -> Optional[dict]:
    """@brief Convert an OpenAI function tool to DeepSeek strict mode, when it is lossless.

    @param tool OpenAI-format tool (``{"type": "function", "function": {...}}``).
    @return A new tool with ``strict: true`` whose parameters list every
        property as required but nullable (``anyOf[schema, null]``) and set
        ``additionalProperties: false`` on every object; None when the schema
        cannot be expressed that way without losing a declared constraint.
    @note DeepSeek strict mode (beta endpoint) enforces the schema while the
        answer is generated, so its SHAPE is guaranteed (no field outside the
        schema, no pad flattened to the top level); its rules: every property
        required, additionalProperties false. Every property is nullable,
        the originally required ones too: a value the model cannot read stays
        expressible as «left out» instead of a number forced by the decoder,
        and the caller's validators keep judging required fields as before.
        A schema that already uses null (type, enum), a list of types, a
        free-key object or a keyword outside _STRICT_KEYWORDS is left alone:
        a null in the answer then always means «field left out», which
        `_drop_nulls` restores.
    """
    function = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("parameters"), dict):
        return None
    try:
        parameters = _strict_schema(function["parameters"], optional=False)
    except _NotStrict:
        return None
    return {**tool, "function": {**function, "parameters": parameters, "strict": True}}


class _NotStrict(Exception):
    """The schema cannot be converted to strict mode without loss."""


def _strict_schema(schema, optional: bool):
    """Strict-mode copy of one schema node; raises _NotStrict when not lossless."""
    if not isinstance(schema, dict) or set(schema) - _STRICT_KEYWORDS:
        raise _NotStrict()
    kind = schema.get("type")
    if not isinstance(kind, str) or kind == "null" or None in (schema.get("enum") or ()):
        raise _NotStrict()
    node = dict(schema)
    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties or schema.get(
                "additionalProperties", False) is not False:
            # A free-key map cannot list its keys as required.
            raise _NotStrict()
        # Every property nullable: see strict_tool, a forced value would look read.
        node["properties"] = {name: _strict_schema(child, optional=True)
                              for name, child in properties.items()}
        node["required"] = list(properties)
        node["additionalProperties"] = False
    elif kind == "array":
        node["items"] = _strict_schema(schema.get("items"), optional=False)
    if not optional:
        return node
    description = node.pop("description", None)
    wrapped = {"anyOf": [node, {"type": "null"}]}
    if description is not None:
        wrapped["description"] = description
    return wrapped


def _drop_nulls(value):
    """Remove the nulls strict mode puts in place of omitted optional fields.

    Only used on answers to a schema `strict_tool` converted, which had no
    null of its own: every null there is an optional field left out.
    """
    if isinstance(value, dict):
        return {key: _drop_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_nulls(item) for item in value]
    return value


# inspect.signature(call_tool) shows the real parameters (ai_diagnostics
# looks for `compatibility_retry` there).
call_tool.__wrapped__ = _call_tool


def tool_arguments(payload: dict, tool_name: str, provider: str = "OpenRouter") -> dict:
    """@brief Extract explicit object arguments with safe provider diagnostics.

    @param payload Decoded provider response; message/prompt bodies are never logged.
    @param tool_name Expected function name.
    @param provider Display name of the provider that answered, used in the
        error texts (e.g. "DeepSeek" for its own API).
    @return Parsed argument object, including an explicitly supplied empty object.
    @throws OpenRouterError Missing/empty arguments, invalid JSON/envelope, truncation
        without a usable tool call, or provider error.
    @throws ToolArgumentsError Only for a COMPLETE answer whose arguments are
        not a JSON object, which a caller may pay once more for. An answer cut
        by the token budget (finish_reason length / max_tokens) with missing
        or partial arguments raises a plain OpenRouterError that states the
        truncation first, never retried (audit 138, A5).
    @note No fallback empty object and no automatic retry on incomplete responses.
    """
    context = _diagnostic_context(payload)
    if not isinstance(payload, dict):
        raise OpenRouterError(tx("La risposta di {provider} non contiene alcun risultato.",
                                 provider=provider) + context)
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise OpenRouterError(_safe_error_text(message) + context)

    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OpenRouterError(tx("La risposta di {provider} non contiene alcun risultato.",
                                 provider=provider) + context)

    message = choices[0].get("message") or {}
    message = message if isinstance(message, dict) else {}
    calls = message.get("tool_calls")
    truncated = _truncated(choices[0])
    for call in calls if isinstance(calls, list) else ():
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict) or function.get("name") != tool_name:
            continue
        try:
            return _decoded_arguments(function.get("arguments"), context)
        except ToolArgumentsError as exc:
            if truncated:
                # Partial arguments because the budget ran out, not a broken
                # answer: the same request would be cut the same way, so the
                # retryable error must never reach the caller (audit 138, A5).
                raise _truncation_error(str(exc)) from None
            raise

    if truncated:
        # A reasoning model can spend its entire completion budget before
        # emitting a tool call. This does not establish lack of tool support.
        raise _truncation_error(context)

    raise OpenRouterError(
        tx("Il modello non ha chiamato lo strumento richiesto (potrebbe non "
           "supportare il tool calling con immagini/PDF): scegline un altro "
           "dall'elenco, filtrando per costo se serve.") + context
    )


#: Normalized completion reasons meaning the token budget ran out (OpenAI
#: style «length»; «max_tokens» from providers that pass their own through).
#: Only `finish_reason` counts: it is the reason the gateway normalized.
_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})


def _truncated(choice: dict) -> bool:
    """Whether the provider declares the answer cut by the completion budget."""
    return choice.get("finish_reason") in _TRUNCATION_REASONS


def _truncation_error(detail: str) -> OpenRouterError:
    """The non-retryable error of an answer cut by the completion budget.

    `detail` is the diagnostic context, or the argument error (which already
    ends with it) when the cut left missing or partial arguments.
    """
    return OpenRouterError(
        tx("Risposta troncata: budget di token di completamento esaurito prima di "
           "restituire la chiamata allo strumento richiesto. Nessun tentativo "
           "automatico aggiuntivo.") + (" " + detail if not detail.startswith(" ") else detail))


def _non_finite_constant(_name: str) -> None:
    """`json.loads` hook: ``NaN``/``Infinity``/``-Infinity`` decode as null."""
    return None


def _finite_float(text: str) -> Optional[float]:
    """`json.loads` hook: a float literal, or None when it overflows to infinity (``1e999``)."""
    value = float(text)
    return value if math.isfinite(value) else None


def _finite_tree(value: Any) -> Any:
    """A decoded JSON tree with every nonfinite float replaced by None.

    Used when the provider already delivers the arguments as an object (its
    envelope was decoded by `json.load`, which accepts NaN). A tree without
    any nonfinite float is returned as the SAME object (callers rely on
    the identity of an explicitly supplied object); otherwise a copy.
    Any real scalar that is not integral counts as a float, NumPy's included
    (``numpy.float32`` is not a Python float); integers and booleans are
    always finite and are kept as they are.
    """
    if (isinstance(value, numbers.Real) and not isinstance(value, numbers.Integral)
            and not math.isfinite(value)):
        return None
    if isinstance(value, dict):
        items = {key: _finite_tree(item) for key, item in value.items()}
        return value if all(items[key] is value[key] for key in value) else items
    if isinstance(value, list):
        items = [_finite_tree(item) for item in value]
        return value if all(new is old for new, old in zip(items, value)) else items
    return value


def _decoded_arguments(arguments: Any, context: str) -> dict:
    """Tool arguments as a dict, or ToolArgumentsError carrying what was sent.

    Non-finite constants (``NaN``, ``Infinity``) are not JSON: they decode as
    None, and so does a float literal that overflows (``1e999``), so each caller's validation rejects them only in the fields that
    matter, and a valid answer stays storable with ``allow_nan=False``
    instead of failing after it was paid for (review D1).
    """
    if isinstance(arguments, dict):
        return _finite_tree(arguments)
    if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
        raise ToolArgumentsError(
            tx("Gli argomenti dello strumento sono assenti o vuoti.") + context, raw=arguments)
    if not isinstance(arguments, str):
        raise ToolArgumentsError(
            tx("Gli argomenti dello strumento non sono JSON valido (atteso oggetto).") + context,
            raw=repr(arguments))
    try:
        decoded = json.loads(arguments, parse_constant=_non_finite_constant,
                             parse_float=_finite_float)
    except ValueError as exc:
        raise ToolArgumentsError(
            tx("Gli argomenti dello strumento non sono JSON valido.") + context
            + f" [{type(exc).__name__}: {exc}]", raw=arguments
        ) from None
    if not isinstance(decoded, dict):
        raise ToolArgumentsError(
            tx("Gli argomenti dello strumento non sono JSON valido (atteso oggetto).") + context,
            raw=arguments)
    return decoded
