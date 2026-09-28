"""@brief Minimal OpenRouter client with privacy-safe provider diagnostics.

OpenRouter espone con una sola chiave (OPENROUTER_API_KEY) i modelli di
tutti i fornitori (Anthropic, OpenAI, Google, ...) dietro un'API
OpenAI-compatible: si sceglie il modello passando il suo id (es.
"anthropic/claude-sonnet-5") nel corpo della richiesta.

Qui dentro c'e' solo il minimo indispensabile per l'estrazione dei
parametri dal datasheet:

* :func:`list_models`  -- elenco dei modelli con prezzi e capacita', usato
  dalla GUI per far scegliere il modello filtrando per costo;
* :func:`call_tool`    -- una chiamata chat/completions con *tool calling*
  forzato, che ritorna direttamente il dizionario degli argomenti.

Si usa solo la stdlib (``urllib``): l'API e' HTTP+JSON e aggiungere una
dipendenza esterna per due chiamate non si giustifica.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import uuid
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from . import credentials
from .messages import tx
from ..app_paths import env_files, is_frozen, user_data_dir
from ..fsutil import replace_file

API_BASE = "https://openrouter.ai/api/v1"
MODELS_URL = f"{API_BASE}/models"
CHAT_URL = f"{API_BASE}/chat/completions"
CREDITS_URL = f"{API_BASE}/credits"
KEY_STATUS_URL = f"{API_BASE}/auth/key"

API_KEY_ENV = "OPENROUTER_API_KEY"

#: Modello usato quando l'utente non ne ha ancora scelto uno.
DEFAULT_MODEL = "anthropic/claude-opus-5"

#: Cache locale dell'elenco modelli (e' lungo ~1 MB e cambia di rado).
MODELS_CACHE_PATH = user_data_dir() / ".openrouter_models.json"
#: File name of the cache inside the user data directory; `_models_cache_path`
#: resolves it at use time, so a data directory chosen after import is honoured.
_MODELS_CACHE_NAME = ".openrouter_models.json"
_IMPORT_TIME_CACHE_PATH = MODELS_CACHE_PATH
MODELS_CACHE_TTL_S = 24 * 3600

# Header opzionali di OpenRouter: identificano l'applicazione chiamante
# nelle statistiche del proprio account.
_APP_HEADERS = {
    "HTTP-Referer": "https://github.com/kicadgen-tool",
    "X-Title": "KiCadGen",
}

_HTTP_TIMEOUT_MODELS_S = 30
_HTTP_TIMEOUT_CHAT_S = 300
_HTTP_TIMEOUT_CREDITS_S = 20

#: Credito minimo (USD) sotto il quale un'estrazione non viene nemmeno avviata.
#: Una singola estrazione con foto e crop costa tipicamente pochi centesimi, ma
#: partire con il serbatoio quasi vuoto significa fallire a meta' catena, dopo
#: aver gia' speso: meglio fermarsi prima con un messaggio chiaro.
MIN_CREDIT_USD = 0.50

#: Sopra MIN_CREDIT_USD ma sotto questa soglia il lavoro parte comunque,
#: segnalando che il credito sta per finire.
LOW_CREDIT_USD = 2.00

#: Il credito cambia solo quando si spende: una catena di decine di chiamate
#: non deve interrogare l'endpoint a ogni passo.
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
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.compatibility = compatibility
        #: HTTP status of the refusal, or None when no HTTP answer arrived.
        self.http_status = http_status
        #: True when OpenRouter could not be reached at all (DNS, connection,
        #: timeout): the request, and so the key, were never judged.
        self.unreachable = unreachable


class MissingApiKeyError(OpenRouterError):
    """@brief No OpenRouter API key in any of the sources `api_key` reads.

    @note A subclass, so every caller that catches OpenRouterError keeps
        working; the GUI can tell this case apart and offer «Account AI...».
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
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class ModelInfo:
    """@brief OpenRouter catalogue model with prices in USD per million tokens and capabilities.

    Un modello dell'elenco OpenRouter, con quello che serve per sceglierlo.

    I prezzi arrivati dall'API sono in dollari per singolo token; qui sono
    gia' convertiti in dollari per milione di token, che e' l'unita' con
    cui i prezzi vengono normalmente confrontati.
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
        """@brief Return whether the model accepts image input."""
        return "image" in self.input_modalities

    @property
    def supports_pdf(self) -> bool:
        """@brief Return whether the model accepts file (PDF) input."""
        return "file" in self.input_modalities

    @property
    def is_free(self) -> bool:
        """@brief Return whether both prompt and completion prices are zero."""
        return self.prompt_usd_per_mtok <= 0 and self.completion_usd_per_mtok <= 0

    def usable_for_datasheets(self) -> bool:
        """@brief Return whether the model supports tool calling plus PDF or image input for datasheets.

        True se il modello puo' fare il lavoro: tool calling + PDF/immagini.

        L'estrazione manda disegni meccanici (immagini o PDF) e pretende la
        risposta come chiamata a uno strumento: un modello senza queste due
        capacita' fallirebbe la richiesta, quindi la GUI lo nasconde.
        """
        return self.usable_for(DATASHEET_CAPABILITIES)

    def usable_for(self, capabilities) -> bool:
        """@brief True se il modello ha tutte le capacita' richieste.

        @param capabilities Nomi fra 'tools', 'images', 'files'.
        @return Se il modello dichiara tutte quelle capacita'.
        @note Il requisito dipende da cosa si chiedera' al modello, non dal
            campo in cui viene scritto: il percorso fotografico manda solo
            immagini, quindi pretendere anche la lettura dei PDF nasconderebbe
            modelli che quel lavoro lo fanno benissimo.
        """
        have = {"tools": self.supports_tools, "images": self.supports_images,
                "files": self.supports_pdf}
        unknown = set(capabilities) - set(have)
        if unknown:
            raise ValueError(tx("Capacita' di modello sconosciute: {sorted}", sorted=sorted(unknown)))
        return all(have[name] for name in capabilities)


#: Cosa serve per leggere un datasheet: risposta strutturata, disegni
#: meccanici come immagini e documenti allegati.
DATASHEET_CAPABILITIES = ("tools", "images", "files")
#: Cosa serve sulle fotografie: risposta strutturata e lettura di immagini.
#: I PDF non entrano mai in questo percorso.
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
    return key


def _key_for_redaction() -> str:
    """The key in use for scrubbing error texts; empty when none or unreadable."""
    try:
        return _resolve_api_key()[0]
    except Exception:  # noqa: BLE001 - redaction must never replace the error it cleans
        return ""


@dataclass(frozen=True)
class CreditStatus:
    """@brief Remaining OpenRouter credit as the minimum of account balance and key limit.

    Credito residuo su OpenRouter, visto dai due vincoli indipendenti.

    Il saldo dell'account (``/credits``) e il tetto di spesa della singola
    chiave (``/auth/key``) sono limiti separati e non coincidono: una chiave
    con 13.73 USD ancora concessi su un account che ne ha 11.92 puo' spendere
    solo 11.92. Il vincolo che conta e' quindi il **minimo** dei due, ed e'
    quello che :attr:`remaining` espone.

    Un valore ``None`` significa "nessun limite di questo tipo" (chiave senza
    tetto) oppure "non riportato dall'API": in entrambi i casi non partecipa al
    minimo, e se nessuno dei due e' noto :attr:`remaining` resta ``None``.
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
        """@brief Return the binding credit limit, or None when unknown."""
        known = [v for v in (self.account_remaining, self.key_remaining) if v is not None]
        return min(known) if known else None

    @property
    def limiting_factor(self) -> str:
        """@brief Name which of the two ceilings is the binding one."""
        remaining = self.remaining
        if remaining is None:
            return ""
        if self.key_remaining is not None and remaining == self.key_remaining:
            return tx("tetto della chiave") if self.account_remaining is None or \
                self.key_remaining < self.account_remaining else tx("account e chiave")
        return tx("saldo dell'account")

    def describe(self) -> str:
        """@brief Render the credit status as one line for the AI log."""
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
    # Singolare e plurale sono due frasi intere nel catalogo: ogni lingua li
    # accorda a modo suo.
    return tx("{count} chiamata", count=count) if count == 1 else tx("{count} chiamate", count=count)


@dataclass
class UsageTotals:
    """@brief Totals of one processing run: calls, tokens and actual cost reported by OpenRouter.

    Consuntivo di una elaborazione: chiamate, token e costo effettivo.

    Il costo arriva da OpenRouter con la risposta di ogni chiamata (campo
    ``usage.cost``, in USD), non da una stima sui listini: tiene gia' conto
    del provider che ha effettivamente servito la richiesta, degli sconti sui
    token letti dalla cache del modello e dei token di ragionamento.

    Le risposte servite dalla cache locale su disco non passano di qui e
    valgono zero, che e' corretto: non hanno toccato l'API.
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
        """@brief Tell whether every call reported its own cost."""
        return self.calls_without_cost == 0

    def describe(self) -> str:
        """@brief Render the run total as one line for the AI log."""
        if not self.calls:
            return (tx("Costo elaborazione AI: nessuna chiamata all'API (risposte servite "
                       "dalla cache locale)."))
        # Sotto il centesimo le quattro cifre non bastano a distinguere nulla.
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


#: L'estrazione gira in un thread di lavoro mentre la GUI resta viva: il
#: consuntivo va protetto, non lasciato a un aggiornamento non atomico.
_usage_lock = threading.Lock()
_usage_totals = UsageTotals()


def reset_usage() -> None:
    """@brief Start a new accounting run, discarding the previous totals."""
    global _usage_totals
    with _usage_lock:
        _usage_totals = UsageTotals()


def usage_totals() -> UsageTotals:
    """@brief Return a snapshot of the current run totals."""
    with _usage_lock:
        return UsageTotals(**vars(_usage_totals))


def _usage_int(container: Any, key: str) -> int:
    value = (container or {}).get(key) if isinstance(container, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if math.isfinite(value) and value > 0 else 0


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
        ``prompt_tokens``, ``completion_tokens``, ``model``), or None to stop.
    @note Called from the worker thread that made the call: a GUI must hop to
        its own thread (e.g. with a Qt signal). A listener that raises never
        breaks the call it is told about: the answer was paid and is used.
    """
    global _usage_listener
    _usage_listener = listener


def _record_usage(payload: Any) -> None:
    """Somma al consuntivo l'uso dichiarato da una risposta dell'API.

    Va chiamata per ogni risposta ricevuta, anche quando poi risultera'
    inutilizzabile: una chiamata che non ha prodotto la tool call e' stata
    comunque pagata, e tacerla renderebbe il consuntivo piu' basso del vero.
    """
    usage = payload.get("usage") if isinstance(payload, dict) else None
    cost = _credit_number((usage or {}).get("cost")) if isinstance(usage, dict) else None
    # A negative cost is not a refund to subtract from the run: it is a
    # malformed report, counted as missing exactly like project_costs does.
    if cost is not None and cost < 0:
        cost = None
    with _usage_lock:
        _usage_totals.calls += 1
        if cost is None:
            _usage_totals.calls_without_cost += 1
        else:
            _usage_totals.cost_usd += cost
        if isinstance(usage, dict):
            _usage_totals.prompt_tokens += _usage_int(usage, "prompt_tokens")
            _usage_totals.completion_tokens += _usage_int(usage, "completion_tokens")
            _usage_totals.cached_tokens += _cached_prompt_tokens(usage)
            _usage_totals.reasoning_tokens += _usage_int(
                usage.get("completion_tokens_details"), "reasoning_tokens")
    listener = _usage_listener
    if listener is not None:
        record = {"cost_usd": cost, "model": str((payload.get("model") if isinstance(payload, dict)
                                              else "") or ""),
                  "prompt_tokens": _usage_int(usage, "prompt_tokens"),
                  "completion_tokens": _usage_int(usage, "completion_tokens")}
        if isinstance(usage, dict) and isinstance(usage.get("cost_source"), str):
            record["cost_source"] = usage["cost_source"]
        try:
            listener(record)
        except Exception:  # noqa: BLE001 - la chiamata e' pagata e resta valida
            pass


def _credit_number(value: Any) -> Optional[float]:
    """Numero finito dalla risposta API, oppure None: i booleani non valgono."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _get_json(url: str, timeout: int, key: Optional[str] = None) -> dict:
    """GET autenticata che ritorna il corpo JSON; solleva OpenRouterError.

    `key` e' la chiave da usare al posto di quella in uso (verifica di una
    chiave appena incollata, non ancora salvata). L'errore dice se e' arrivato
    un rifiuto HTTP (`http_status`) o se OpenRouter non e' stato raggiunto
    (`unreachable`).
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key if key is not None else api_key()}",
                 **_APP_HEADERS},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        status = exc.code if type(exc.code) is int else None
        raise OpenRouterError(_error_message(exc), http_status=status) from None
    except urllib.error.URLError as exc:
        raise OpenRouterError(tx("connessione fallita: {safe_error_text}",
                                 safe_error_text=_safe_error_text(exc.reason))
                              + _diagnostic_context(), unreachable=True) from None
    except (ValueError, OSError) as exc:
        # Un OSError che non e' un ValueError e' un guasto di trasporto
        # (timeout in lettura, connessione chiusa): la risposta non e' arrivata.
        raise OpenRouterError(tx("risposta non decodificabile") + _diagnostic_context(),
                              unreachable=not isinstance(exc, ValueError)) from None
    return payload if isinstance(payload, dict) else {}


def credit_status(force_refresh: bool = False) -> CreditStatus:
    """@brief Query both credit endpoints (cached) and return the effective credit constraint.

    Interroga i due endpoint di credito e ne ricava il vincolo effettivo.

    I due endpoint sono indipendenti: basta che ne risponda uno perche' il
    controllo abbia comunque un limite da applicare. Solleva OpenRouterError
    solo quando falliscono entrambi, cosi' il chiamante puo' distinguere
    "credito esaurito" da "credito non verificabile".

    Il risultato resta in cache per :data:`_CREDIT_CACHE_TTL_S` secondi: una
    catena di decine di chiamate non deve interrogare l'API a ogni passo.
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
    # limit = None significa chiave senza tetto proprio: non e' un vincolo.
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

    Verifica il credito PRIMA di spendere; solleva se non basta.

    Tre esiti distinti, perche' vanno trattati in modo diverso:

    * credito noto e sotto `minimum_usd` -> OpenRouterError: il lavoro non
      parte, invece di fallire a meta' catena dopo aver gia' speso;
    * credito noto ma basso -> passa, con un avviso su `progress`;
    * credito **non verificabile** (rete giu', endpoint cambiato) -> passa con
      un avviso. Un controllo di cortesia non deve rendere l'applicazione meno
      utilizzabile di quando il controllo non c'era: e' l'API stessa a
      rifiutare la chiamata se davvero non c'e' credito.

    Ritorna lo stato letto, oppure None se non e' stato possibile leggerlo.
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
    """Prezzo per token (stringa dall'API) -> dollari per milione di token.

    A non-finite price ("nan", "inf") is not a price: it is unknown (NaN, the
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
    # I modelli "auto" espongono prezzi sentinella negativi: valgono 0.
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
    """Legge la cache su disco; None se manca, e' illeggibile, corrotta o scaduta."""
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
        # La cache e' un'ottimizzazione: se non si puo' scrivere, pazienza.
        try:
            temporary.unlink()
        except OSError:
            pass


def list_models(force_refresh: bool = False) -> list[ModelInfo]:
    """@brief Return the OpenRouter models sorted by cost, using a one-day disk cache and offline fallback.

    Elenco dei modelli disponibili su OpenRouter, ordinato per costo.

    L'endpoint /models e' pubblico (non serve la API key) e restituisce
    prezzi e modalita' di input di ogni modello: e' quello che permette
    alla GUI di filtrare per costo prima di scegliere.

    Il risultato viene messo in cache su disco per un giorno; con
    `force_refresh=True` la cache viene ignorata e riscritta. Se la rete
    non risponde si ripiega sulla cache anche scaduta, cosi' la scelta del
    modello resta possibile offline.
    """
    payload = None if force_refresh else _load_models_cache()
    if payload is None:
        request = urllib.request.Request(MODELS_URL, headers=_APP_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_MODELS_S) as response:
                payload = _models_list(json.load(response))
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


def call_tool(
    model: str,
    system_prompt: str,
    content_blocks: Sequence[dict],
    tool: dict,
    max_tokens: int = 2048,
    has_pdf: bool = False,
    pdf_engine: str = "native",
    compatibility_retry: bool = False,
) -> dict:
    """@brief Call the model once and decode explicit structured tool arguments.

    @param model Provider model identifier.
    @param system_prompt Private instructions, excluded from error diagnostics.
    @param content_blocks Request text/images/files, excluded from error diagnostics.
    @param tool OpenAI-compatible function schema.
    @param max_tokens Maximum completion tokens.
    @param has_pdf Whether PDF parsing is required.
    @param pdf_engine PDF parser selection.
    @param compatibility_retry Use the less provider-specific required-tool form.
    @return Explicit argument object; an explicitly supplied empty object is valid.
    @throws OpenRouterError Safe network/provider diagnostics or invalid/missing arguments.
    @note Retry orchestration belongs to the logged application wrapper; paid response
        usage is recorded before argument validation.

    `tool` e' lo schema in formato OpenAI
    (``{"type": "function", "function": {"name", "description", "parameters"}}``);
    il valore di ritorno e' il dizionario degli argomenti gia' decodificato,
    cosi' il chiamante non deve mai fare parsing di testo libero.

    `pdf_engine` conta solo quando tra i file c'e' un PDF: "native" lascia
    il PDF al modello (indispensabile per leggere i disegni meccanici, che
    nel PDF sono immagini), mentre gli altri motori di OpenRouter ne
    estrarrebbero solo il testo.
    """
    from .providers import (DEEPSEEK_CHAT_URL, PROVIDER_DEEPSEEK, ProviderError,
                            deepseek_request, provider_model_name, provider_of)
    body: dict[str, Any] = {
        "model": provider_model_name(model),
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": list(content_blocks)},
        ],
        "tools": [tool],
        "tool_choice": "required" if compatibility_retry else {
            "type": "function", "function": {"name": tool["function"]["name"]}},
    }
    if provider_of(model) == PROVIDER_DEEPSEEK:
        # API diretta di DeepSeek (compatibile OpenAI): niente plugin PDF ne'
        # costo nella risposta; il costo si calcola dai token (sotto).
        if has_pdf:
            raise ProviderError(tx("L'API diretta di DeepSeek non legge i PDF: per un "
                                   "datasheet PDF scegli un modello OpenRouter che li legga."))
        # Il ragionamento e' attivo di default e rifiuta tool_choice con una
        # funzione nominata (HTTP 400 «Thinking mode does not support this
        # tool_choice», 27/09): la risposta strutturata conta piu' del
        # ragionamento, quindi lo si spegne (scelta dell'utente).
        body["thinking"] = {"type": "disabled"}
        payload = deepseek_request(DEEPSEEK_CHAT_URL, body, timeout=_HTTP_TIMEOUT_CHAT_S)
        if isinstance(payload, dict):
            # L'identificativo completo, non il nome che risponde DeepSeek: il
            # file dei costi riconosce cosi' il fornitore di ogni chiamata.
            payload["model"] = model
            # DeepSeek non riporta il costo: si calcola come fa la sua
            # fatturazione, token x prezzo del listino ufficiale (HANDOFF 128).
            from .deepseek_pricing import call_cost
            usage = payload.get("usage")
            cost = call_cost(provider_model_name(model), usage)
            if isinstance(usage, dict) and cost is not None:
                usage["cost"], usage["cost_source"] = cost, "token_price"
        _record_usage(payload)
        return tool_arguments(payload, tool["function"]["name"], provider="DeepSeek")
    if has_pdf:
        body["plugins"] = [{"id": "file-parser", "pdf": {"engine": pdf_engine}}]
    # Senza questo flag la risposta porta i token ma non il costo, e il
    # consuntivo finale dovrebbe stimarlo dai listini invece di leggerlo.
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
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_CHAT_S) as response:
            payload = json.load(response)
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
    except ValueError:
        raise OpenRouterError(tx("Risposta non decodificabile da OpenRouter.")
                              + _diagnostic_context()) from None
    except OSError as exc:
        raise OpenRouterError(tx("Connessione a OpenRouter fallita: {reason}",
                                 reason=_safe_error_text(str(exc) if isinstance(exc, TimeoutError)
                                                         else None))
                              + _diagnostic_context()) from None

    # Prima di tool_arguments, che puo' sollevare: la chiamata e' stata pagata
    # comunque, e un consuntivo che ignora i tentativi falliti e' falso.
    _record_usage(payload)
    return tool_arguments(payload, tool["function"]["name"])


def tool_arguments(payload: dict, tool_name: str, provider: str = "OpenRouter") -> dict:
    """@brief Extract explicit object arguments with safe provider diagnostics.

    @param payload Decoded provider response; message/prompt bodies are never logged.
    @param tool_name Expected function name.
    @param provider Display name of the provider that answered, used in the
        error texts (e.g. "DeepSeek" for its own API).
    @return Parsed argument object, including an explicitly supplied empty object.
    @throws OpenRouterError Missing/empty arguments, invalid JSON/envelope, truncation
        without a usable tool call, or provider error.
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
    for call in calls if isinstance(calls, list) else ():
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict) or function.get("name") != tool_name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            return arguments
        if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
            raise ToolArgumentsError(
                tx("Gli argomenti dello strumento sono assenti o vuoti.") + context, raw=arguments)
        if not isinstance(arguments, str):
            raise ToolArgumentsError(
                tx("Gli argomenti dello strumento non sono JSON valido (atteso oggetto).") + context,
                raw=repr(arguments))
        try:
            decoded = json.loads(arguments)
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

    if choices[0].get("finish_reason") == "length":
        # A reasoning model can spend its entire completion budget before
        # emitting a tool call. This does not establish lack of tool support.
        raise OpenRouterError(
            tx("Risposta troncata: budget di token di completamento esaurito prima di "
               "restituire la chiamata allo strumento richiesto. Nessun tentativo "
               "automatico aggiuntivo.") + context
        )

    raise OpenRouterError(
        tx("Il modello non ha chiamato lo strumento richiesto (potrebbe non "
           "supportare il tool calling con immagini/PDF): scegline un altro "
           "dall'elenco, filtrando per costo se serve.") + context
    )
