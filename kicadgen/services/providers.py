"""@brief AI providers besides OpenRouter: DeepSeek's own API, reached with DEEPSEEK_API_KEY.

User request (2026-09-27): the key of DeepSeek's API is in the `.env` file as
DEEPSEEK_API_KEY; the model pickers must filter by kind of API and the balance
must follow the provider of the chosen model.

The provider of a model is written in its identifier, never guessed from the
model's name: DeepSeek's own models carry the prefix DEEPSEEK_PREFIX
(``deepseek-api/deepseek-flash``), while ``deepseek/...`` stays the same model
reached through OpenRouter. Everything else is OpenRouter.

DeepSeek's API is OpenAI-compatible (chat/completions with tools). It does not
report the cost of a call nor the capabilities of its models: calls are
recorded without cost (the project total says «at least») and the models are
listed with capabilities «not declared». The key is never printed or logged.
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
from .openrouter import OpenRouterError

PROVIDER_OPENROUTER = "openrouter"
PROVIDER_DEEPSEEK = "deepseek"
#: Providers in the order the pickers show them.
PROVIDERS = (PROVIDER_OPENROUTER, PROVIDER_DEEPSEEK)
#: Italian source labels of the providers (translated by the GUI).
PROVIDER_LABELS = {PROVIDER_OPENROUTER: "OpenRouter",
                   PROVIDER_DEEPSEEK: "DeepSeek (API diretta)"}

DEEPSEEK_PREFIX = "deepseek-api/"
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_CHAT_URL = f"{DEEPSEEK_API_BASE}/chat/completions"
DEEPSEEK_MODELS_URL = f"{DEEPSEEK_API_BASE}/models"
DEEPSEEK_BALANCE_URL = f"{DEEPSEEK_API_BASE}/user/balance"
DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
_HTTP_TIMEOUT_S = 30


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


def deepseek_api_key() -> str:
    """@brief DeepSeek key from the environment, else from the `.env` files.

    @return The key, stripped.
    @throws ProviderKeyMissing When no source sets DEEPSEEK_API_KEY. The
        message names the variable, never a value.
    """
    value = os.environ.get(DEEPSEEK_KEY_ENV, "").strip()
    if value:
        return value
    try:
        from dotenv import dotenv_values
    except ImportError:
        dotenv_values = None
    if dotenv_values is not None:
        for path in env_files():
            try:
                if path.is_file():
                    found = (dotenv_values(path).get(DEEPSEEK_KEY_ENV) or "").strip()
                    if found:
                        return found
            except (OSError, ValueError):
                continue
    raise ProviderKeyMissing(tx("Chiave DeepSeek non configurata: imposta {name} nel file "
                                ".env o nell'ambiente.", name=DEEPSEEK_KEY_ENV))


class ProviderError(OpenRouterError):
    """@brief A provider request failed; the text never contains the key.

    @note A subclass of OpenRouterError so every caller that already handles
        an AI failure handles this one too.
    """


class ProviderKeyMissing(ProviderError):
    """@brief The provider's key is not configured."""


def _redact(text: str) -> str:
    """Remove the key from a text that might echo it."""
    try:
        key = deepseek_api_key()
    except ProviderError:
        return text
    return text.replace(key, "***") if key else text


def deepseek_request(url: str, body: Optional[dict] = None, timeout: int = _HTTP_TIMEOUT_S) -> Any:
    """@brief One authenticated request to DeepSeek's API.

    @param url Endpoint URL.
    @param body JSON body for a POST; None for a GET.
    @param timeout Seconds.
    @return Decoded JSON response.
    @throws ProviderError On missing key, HTTP error or unreadable answer.
    """
    headers = {"Authorization": f"Bearer {deepseek_api_key()}", "Accept": "application/json"}
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
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str((payload.get("error") or {}).get("message") or "")[:300]
        except (ValueError, OSError, AttributeError):
            pass
        raise ProviderError(_redact(tx("DeepSeek ha risposto con un errore: HTTP {code} {detail}",
                                       code=exc.code, detail=detail).strip())) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(_redact(tx("Connessione a DeepSeek fallita: {reason}",
                                       reason=getattr(exc, "reason", exc)))) from None


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


def deepseek_balance() -> ProviderBalance:
    """@brief DeepSeek account balance (free endpoint, no tokens).

    @return ProviderBalance; the amount is the first currency the API reports.
    @throws ProviderError When it cannot be read.
    """
    payload = deepseek_request(DEEPSEEK_BALANCE_URL)
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
    available = bool(payload.get("is_available")) if isinstance(payload, dict) else False
    return ProviderBalance(PROVIDER_DEEPSEEK, available, amount, currency)


def ensure_providers_ready(models, progress=None) -> None:
    """@brief Before a paid run: every provider used has a key and some credit.

    @param models Model identifiers the run will call (duplicates allowed).
    @param progress Optional log callback.
    @throws ProviderError (DeepSeek) or OpenRouterError (OpenRouter) when a key
        is missing or the credit is known to be exhausted. A credit that cannot
        be read is logged and does not stop the run, as for OpenRouter.
    """
    from .openrouter import api_key, check_credit
    log = progress or (lambda message: None)
    used = {provider_of(model) for model in models if model} or {PROVIDER_OPENROUTER}
    if PROVIDER_OPENROUTER in used:
        api_key()
        check_credit(progress=progress)
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
