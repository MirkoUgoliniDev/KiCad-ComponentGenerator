"""@brief AI providers besides OpenRouter: DeepSeek's own API, reached with DEEPSEEK_API_KEY.

User request (2026-09-27): the key of DeepSeek's API is in the `.env` file as
DEEPSEEK_API_KEY; the model pickers must filter by kind of API and the balance
must follow the provider of the chosen model.

The provider of a model is written in its identifier, never guessed from the
model's name: DeepSeek's own models carry the prefix DEEPSEEK_PREFIX
(``deepseek-api/deepseek-flash``), while ``deepseek/...`` stays the same model
reached through OpenRouter. Everything else is OpenRouter.

DeepSeek's API is OpenAI-compatible (chat/completions with tools). It does not
report the cost of a call nor the capabilities of its models: the cost of a
call is computed from its tokens and DeepSeek's published prices
(`deepseek_pricing`, cost_source "token_price"; a model missing from the price
list is recorded without cost) and the models are listed with capabilities
«not declared». The key is never printed or logged, and provider error texts
are reduced to the fixed categories of `openrouter._safe_error_text`.
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from ..app_paths import env_files
from .messages import tx
from .openrouter import (TRANSIENT_HTTP_STATUSES, OpenRouterError, _diagnostic_context,
                         _retry_after, _safe_error_text)

PROVIDER_OPENROUTER = "openrouter"
PROVIDER_DEEPSEEK = "deepseek"
#: Providers in the order the pickers show them.
PROVIDERS = (PROVIDER_OPENROUTER, PROVIDER_DEEPSEEK)
#: Italian source labels of the providers (translated by the GUI).
PROVIDER_LABELS = {PROVIDER_OPENROUTER: "OpenRouter",
                   PROVIDER_DEEPSEEK: "DeepSeek (API diretta)"}
#: Short names, for places where the label sits inside other text.
PROVIDER_SHORT_LABELS = {PROVIDER_OPENROUTER: "OpenRouter", PROVIDER_DEEPSEEK: "DeepSeek"}

DEEPSEEK_PREFIX = "deepseek-api/"
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_CHAT_URL = f"{DEEPSEEK_API_BASE}/chat/completions"
DEEPSEEK_MODELS_URL = f"{DEEPSEEK_API_BASE}/models"
DEEPSEEK_BALANCE_URL = f"{DEEPSEEK_API_BASE}/user/balance"
DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
_HTTP_TIMEOUT_S = 30
#: Largest error body read from DeepSeek (bytes), the same bound as OpenRouter
#: (`openrouter._error_message`): a hostile or broken server cannot make the
#: application buffer an unbounded answer just to build an error message.
_MAX_ERROR_BODY_BYTES = 65536


def provider_of(model: Optional[str]) -> str:
    """@brief Provider that serves `model`, read from its identifier.

    @param model Model identifier as stored in settings and sessions.
    @return PROVIDER_DEEPSEEK for DEEPSEEK_PREFIX identifiers, else PROVIDER_OPENROUTER.
    """
    return (PROVIDER_DEEPSEEK if isinstance(model, str) and model.startswith(DEEPSEEK_PREFIX)
            else PROVIDER_OPENROUTER)


def provider_model_name(model: str) -> str:
    """@brief The model name the provider's own API expects (prefix removed).

    @param model Model identifier.
    @return `model` without DEEPSEEK_PREFIX for DeepSeek, unchanged otherwise.
    """
    return model[len(DEEPSEEK_PREFIX):] if provider_of(model) == PROVIDER_DEEPSEEK else model


def _deepseek_dotenv_key() -> tuple[str, Optional[str]]:
    """DEEPSEEK_API_KEY of the first `.env` file that sets it, and that file."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return "", None
    for path in env_files():
        try:
            if path.is_file():
                found = (dotenv_values(path).get(DEEPSEEK_KEY_ENV) or "").strip()
                if found:
                    return found, str(path)
        except (OSError, ValueError):
            continue
    return "", None


def _resolve_deepseek_key():
    """The DeepSeek key in use (empty when none) and its source, in api-key order.

    Same order as OpenRouter (openrouter._resolve_api_key): the environment
    variable, then the Windows Credential Manager entry written by «Account
    AI», then the `.env` files. Never raises: a Credential Manager that
    cannot be read must not hide a `.env` key.
    """
    from . import credentials
    from .openrouter import (ApiKeySource, KEY_SOURCE_CREDENTIAL_MANAGER, KEY_SOURCE_DOTENV,
                             KEY_SOURCE_ENVIRONMENT, KEY_SOURCE_NONE)
    stored, credential_error = "", ""
    if credentials.is_supported():
        try:
            stored = (credentials.read_secret(credentials.DEEPSEEK_TARGET) or "").strip()
        except credentials.CredentialError as exc:
            credential_error = str(exc)
    environment = os.environ.get(DEEPSEEK_KEY_ENV, "").strip()
    if environment:
        return environment, ApiKeySource(KEY_SOURCE_ENVIRONMENT, DEEPSEEK_KEY_ENV, bool(stored),
                                         credential_error)
    if stored:
        return stored, ApiKeySource(KEY_SOURCE_CREDENTIAL_MANAGER, credentials.DEEPSEEK_TARGET,
                                    True)
    from_file, path = _deepseek_dotenv_key()
    if from_file:
        return from_file, ApiKeySource(KEY_SOURCE_DOTENV, path, False, credential_error)
    return "", ApiKeySource(KEY_SOURCE_NONE, "", False, credential_error)


def deepseek_key_source():
    """@brief Tell where the DeepSeek key in use comes from, never the key itself.

    @return An openrouter.ApiKeySource (same kinds as for OpenRouter).
    """
    return _resolve_deepseek_key()[1]


def deepseek_api_key() -> str:
    """@brief The DeepSeek key: environment, then Windows Credential Manager, then `.env`.

    @return The key, stripped.
    @throws ProviderKeyMissing When no source sets it. The message names the
        variable and the «Account AI» window, never a value.
    """
    key, _source = _resolve_deepseek_key()
    if key:
        return key
    raise ProviderKeyMissing(tx("Chiave DeepSeek non configurata: inseriscila da "
                                "Configurazione → «Account AI...», oppure imposta {name} nel "
                                "file .env o nell'ambiente.", name=DEEPSEEK_KEY_ENV))


class ProviderError(OpenRouterError):
    """@brief A provider request failed; the text never contains the key.

    @note A subclass of OpenRouterError so every caller that already handles
        an AI failure handles this one too.
    """


class ProviderKeyMissing(ProviderError):
    """@brief The provider's key is not configured."""


def _redact(text: str, key: Optional[str] = None) -> str:
    """Remove the key (the one in use, or `key`) from a text that might echo it.

    Also its escaped form: a library that reports a bad header shows the
    key's repr, where a line break is the two characters backslash-n.
    """
    if key is None:
        try:
            key = deepseek_api_key()
        except ProviderError:
            return text
    if not key:
        return text
    for form in {key, repr(key)[1:-1], ascii(key)[1:-1]}:
        if form:
            text = text.replace(form, "***")
    return text


def key_text_problem(key: str) -> Optional[str]:
    """@brief Tell why a pasted key cannot be a key, before it reaches any request.

    @param key The text as pasted (ends already stripped by the caller or not).
    @return A translated reason, or None when the text can be a key.
    @note API keys are printable ASCII without blanks. A line break or a
        space in the middle comes from a bad copy; sent as a header it makes
        the HTTP library echo the whole key in its error text.
    """
    text = (key or "").strip()
    if not text:
        return tx("La chiave è vuota.")
    if not text.isascii() or not text.isprintable() or any(c.isspace() for c in text):
        return tx("La chiave contiene spazi, a capo o caratteri non validi: ricopiala dal "
                  "sito del fornitore.")
    return None


def _error_detail(exc: urllib.error.HTTPError, key: Optional[str]) -> str:
    """Fixed category and provider code of a DeepSeek HTTP error, never its free text.

    The body is read up to _MAX_ERROR_BODY_BYTES. The server message goes
    through `openrouter._safe_error_text`, which keeps only known categories
    («rate limit», «invalid api key», ...): an echoed prompt or key can never
    reach the log. The key is still redacted, as a last guard.
    """
    status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
    try:
        payload = json.loads(exc.read(_MAX_ERROR_BODY_BYTES).decode("utf-8", "replace"))
    except (ValueError, OSError, AttributeError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    return _redact(_safe_error_text(message) + _diagnostic_context(payload, status), key)


def deepseek_request(url: str, body: Optional[dict] = None, timeout: int = _HTTP_TIMEOUT_S,
                     *, key: Optional[str] = None) -> Any:
    """@brief One authenticated request to DeepSeek's API.

    @param url Endpoint URL.
    @param body JSON body for a POST; None for a GET.
    @param timeout Seconds.
    @param key Key to use instead of the configured one (verifying a key
        pasted in «Account AI» before it is saved).
    @return Decoded JSON response.
    @throws ProviderError On missing key, HTTP error or unreadable answer.
        A transient HTTP status (TRANSIENT_HTTP_STATUSES) is marked
        `retryable`, with `retry_after` from the Retry-After header; a network
        failure is `unreachable` and, as for OpenRouter, not retried; its
        text carries only a fixed category (`_network_reason`). A POST whose
        answer timed out or could not be decoded after it was sent is marked
        `possibly_billed` (the caller records it as a call without cost).
    """
    key = key if key else deepseek_api_key()
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        transient = type(exc.code) is int and exc.code in TRANSIENT_HTTP_STATUSES
        detail = _error_detail(exc, key)
        error = ProviderError(_redact(tx("DeepSeek ha risposto con un errore: HTTP {code} {detail}",
                                         code=exc.code, detail=detail).strip(), key),
                              http_status=exc.code,
                              # Same transient classification as OpenRouter, so a
                              # 429/5xx is retried by make_logged_call with the
                              # provider-requested wait; 400/401 are final.
                              retryable=transient,
                              retry_after=_retry_after(exc) if transient else None)
        error.status = exc.code
        raise error from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # URLError: nothing was sent (urllib wraps every failure while
        # sending). A bare TimeoutError / OSError while waiting for or reading
        # the answer, or an undecodable body, come AFTER the request was sent:
        # a chat request may have been billed (audit 138, B10).
        sent = not isinstance(exc, urllib.error.URLError)
        error = ProviderError(_redact(tx("Connessione a DeepSeek fallita: {reason}",
                                         reason=_network_reason(exc)), key),
                              unreachable=not isinstance(exc, ValueError))
        error.possibly_billed = sent and body is not None and (
            isinstance(exc, (TimeoutError, ValueError)))
        raise error from None


def _network_reason(exc: BaseException) -> str:
    """Fixed category of a network failure, never its free text (audit 138, B11).

    A proxy error can echo the proxy URL with its credentials, a socket error
    a host name: like OpenRouter's, the text is reduced to known categories
    by `openrouter._safe_error_text` («timed out», ...); anything else becomes
    «provider details omitted».
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
        return _safe_error_text("timed out")
    return _safe_error_text(reason if isinstance(reason, str) else None)


def deepseek_models() -> list:
    """@brief DeepSeek's own models as ModelInfo, capabilities and prices not declared.

    @return One ModelInfo per model listed by /models, identifier with DEEPSEEK_PREFIX.
    @throws ProviderError When the list cannot be read.
    """
    from .openrouter import ModelInfo
    payload = deepseek_request(DEEPSEEK_MODELS_URL)
    rows = payload.get("data") if isinstance(payload, dict) else None
    models = []
    for row in rows or ():
        name = row.get("id") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        models.append(ModelInfo(
            id=DEEPSEEK_PREFIX + name.strip(), name=f"DeepSeek API: {name.strip()}",
            prompt_usd_per_mtok=math.nan, completion_usd_per_mtok=math.nan, context_length=0,
            input_modalities=(), supports_tools=False, provider=PROVIDER_DEEPSEEK,
            capabilities_declared=False))
    return models


@dataclass(frozen=True)
class ProviderBalance:
    """@brief Remaining credit of a provider other than OpenRouter."""

    provider: str
    available: bool
    amount: Optional[float]
    currency: str

    def describe(self) -> str:
        """@brief One log line."""
        label = PROVIDER_LABELS.get(self.provider, self.provider)
        if self.amount is None:
            return tx("Credito {label}: {state}.", label=label,
                      state=tx("disponibile") if self.available else tx("esaurito"))
        return tx("Credito {label}: {amount:.2f} {currency} residui.", label=label,
                  amount=self.amount, currency=self.currency)


def deepseek_balance(key: Optional[str] = None) -> ProviderBalance:
    """@brief DeepSeek account balance (free endpoint, no tokens).

    @param key Key to check instead of the configured one (see deepseek_request).
    @return ProviderBalance; the amount is the first currency the API reports.
    @throws ProviderError When it cannot be read.
    """
    payload = deepseek_request(DEEPSEEK_BALANCE_URL, key=key)
    infos = payload.get("balance_infos") if isinstance(payload, dict) else None
    amount, currency = None, ""
    for info in infos or ():
        try:
            value = float(info.get("total_balance"))
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(value):
            amount, currency = value, str(info.get("currency") or "")
            break
    # Only a JSON true is availability: a string "false" or a number is a
    # malformed answer, and a malformed answer never unlocks a paid run.
    available = isinstance(payload, dict) and payload.get("is_available") is True
    return ProviderBalance(PROVIDER_DEEPSEEK, available, amount, currency)


def ensure_providers_ready(models, progress=None, *, openrouter_check=None) -> None:
    """@brief Before a paid run: every provider used has a key and some credit.

    @param models Model identifiers the run will call (duplicates allowed).
        Pass the EFFECTIVE identifiers (defaults already applied): an empty
        entry is skipped, and a list with no model at all checks OpenRouter.
    @param progress Optional log callback.
    @param openrouter_check Optional callable taking `progress` that performs
        the OpenRouter key and credit check; None uses `openrouter.api_key`
        followed by `openrouter.check_credit`. Lets a caller keep its own
        module-level references to those functions.
    @throws ProviderError (DeepSeek) or OpenRouterError (OpenRouter) when a key
        is missing or the credit is known to be exhausted. A credit that cannot
        be read is logged and does not stop the run, as for OpenRouter.
    """
    log = progress or (lambda message: None)
    used = {provider_of(model) for model in models if model} or {PROVIDER_OPENROUTER}
    if PROVIDER_OPENROUTER in used:
        if openrouter_check is None:
            from .openrouter import api_key, check_credit

            def openrouter_check(report):
                api_key()
                check_credit(progress=report)
        openrouter_check(progress)
    if PROVIDER_DEEPSEEK in used:
        deepseek_api_key()
        try:
            balance = deepseek_balance()
        except ProviderKeyMissing:
            raise
        except ProviderError as exc:
            log(tx("Credito DeepSeek non verificabile: {exc}. Si prosegue comunque.", exc=exc))
            return
        log(balance.describe())
        if not balance.available:
            raise ProviderError(tx("Credito DeepSeek esaurito: ricarica il conto su "
                                   "platform.deepseek.com."))


def verify_deepseek_key(key: str) -> ProviderBalance:
    """@brief Check a DeepSeek key by reading its balance (free: no model call).

    @param key The key to check, as pasted.
    @return The balance the key sees.
    @throws ProviderError When DeepSeek refuses the key (`status` 401/403,
        see deepseek_key_rejected) or cannot be reached.
    """
    problem = key_text_problem(key if isinstance(key, str) else "")
    if problem:
        raise ProviderError(problem)
    return deepseek_balance(key.strip())


def deepseek_key_rejected(error: BaseException) -> bool:
    """@brief Tell whether DeepSeek refused a key (HTTP 401/403), not merely failed to answer.

    @param error The ProviderError raised by a request.
    @return True for an authentication refusal.
    """
    return (getattr(error, "http_status", None) or getattr(error, "status", None)) in (401, 403)
