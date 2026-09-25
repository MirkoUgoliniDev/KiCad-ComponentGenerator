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
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from .messages import tx
from ..app_paths import env_files, is_frozen, user_data_dir

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
MODELS_CACHE_TTL_S = 24 * 3600

# Header opzionali di OpenRouter: identificano l'applicazione chiamante
# nelle statistiche del proprio account.
_APP_HEADERS = {
    "HTTP-Referer": "https://github.com/kicadgen-tool",
    "X-Title": "KiCadGen-Tool",
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
    @param retryable Whether one controlled application retry may be attempted.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ModelInfo:
    """Un modello dell'elenco OpenRouter, con quello che serve per sceglierlo.

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

    @property
    def supports_images(self) -> bool:
        """@brief Provide the supports images operation for this workflow."""
        return "image" in self.input_modalities

    @property
    def supports_pdf(self) -> bool:
        """@brief Provide the supports pdf operation for this workflow."""
        return "file" in self.input_modalities

    @property
    def is_free(self) -> bool:
        """@brief Provide the is free operation for this workflow."""
        return self.prompt_usd_per_mtok <= 0 and self.completion_usd_per_mtok <= 0

    def usable_for_datasheets(self) -> bool:
        """True se il modello puo' fare il lavoro: tool calling + PDF/immagini.

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


def api_key() -> str:
    """Ritorna la chiave OpenRouter dall'ambiente, o solleva OpenRouterError."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise OpenRouterError(
            tx("Variabile d'ambiente {API_KEY_ENV} non impostata. Inseriscila nel "
               "file {path} (vedi .env.example).",
               API_KEY_ENV=API_KEY_ENV, path=env_files()[-1] if not is_frozen()
               else env_files()[0])
        )
    return key


@dataclass(frozen=True)
class CreditStatus:
    """Credito residuo su OpenRouter, visto dai due vincoli indipendenti.

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
            detail += f"; chiave {self.key_remaining:.2f} USD"
        return detail + "."


def _calls(count: int) -> str:
    """@brief Render a call count with the right Italian plural."""
    return f"{count} chiamata" if count == 1 else f"{count} chiamate"


@dataclass
class UsageTotals:
    """Consuntivo di una elaborazione: chiamate, token e costo effettivo.

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
    with _usage_lock:
        _usage_totals.calls += 1
        if cost is None:
            _usage_totals.calls_without_cost += 1
        else:
            _usage_totals.cost_usd += cost
        if isinstance(usage, dict):
            _usage_totals.prompt_tokens += _usage_int(usage, "prompt_tokens")
            _usage_totals.completion_tokens += _usage_int(usage, "completion_tokens")
            _usage_totals.cached_tokens += _usage_int(
                usage.get("prompt_tokens_details"), "cached_tokens")
            _usage_totals.reasoning_tokens += _usage_int(
                usage.get("completion_tokens_details"), "reasoning_tokens")
    listener = _usage_listener
    if listener is not None:
        record = {"cost_usd": cost, "model": str((payload.get("model") if isinstance(payload, dict)
                                              else "") or ""),
                  "prompt_tokens": _usage_int(usage, "prompt_tokens"),
                  "completion_tokens": _usage_int(usage, "completion_tokens")}
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


def _get_json(url: str, timeout: int) -> dict:
    """GET autenticata che ritorna il corpo JSON; solleva OpenRouterError."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key()}", **_APP_HEADERS},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise OpenRouterError(_error_message(exc)) from None
    except urllib.error.URLError as exc:
        raise OpenRouterError(tx("connessione fallita: {safe_error_text}",
                                 safe_error_text=_safe_error_text(exc.reason))
                              + _diagnostic_context()) from None
    except (ValueError, OSError) as exc:
        raise OpenRouterError(tx("risposta non decodificabile") + _diagnostic_context()) from None
    return payload if isinstance(payload, dict) else {}


def credit_status(force_refresh: bool = False) -> CreditStatus:
    """Interroga i due endpoint di credito e ne ricava il vincolo effettivo.

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
    account_remaining = total_credits = total_usage = None
    try:
        data = _get_json(CREDITS_URL, _HTTP_TIMEOUT_CREDITS_S).get("data") or {}
        total_credits = _credit_number(data.get("total_credits"))
        total_usage = _credit_number(data.get("total_usage"))
        if total_credits is not None and total_usage is not None:
            account_remaining = total_credits - total_usage
    except OpenRouterError as exc:
        account_error = str(exc)
        failures.append(tx("saldo account ({exc})", exc=exc))

    key_remaining, is_free_tier, expires_at = None, False, ""
    try:
        data = _get_json(KEY_STATUS_URL, _HTTP_TIMEOUT_CREDITS_S).get("data") or {}
        # limit = None significa chiave senza tetto proprio: non e' un vincolo.
        if _credit_number(data.get("limit")) is not None:
            key_remaining = _credit_number(data.get("limit_remaining"))
        is_free_tier = bool(data.get("is_free_tier"))
        expires_at = str(data.get("expires_at") or "")
    except OpenRouterError as exc:
        failures.append(tx("stato chiave ({exc})", exc=exc))

    if len(failures) == 2:
        raise OpenRouterError("; ".join(failures))

    status = CreditStatus(
        account_remaining=account_remaining, key_remaining=key_remaining,
        total_credits=total_credits, total_usage=total_usage,
        is_free_tier=is_free_tier, expires_at=expires_at, account_error=account_error,
    )
    _credit_cache = (time.time(), status)
    return status


def check_credit(minimum_usd: float = MIN_CREDIT_USD, progress=None,
                 force_refresh: bool = False) -> Optional[CreditStatus]:
    """Verifica il credito PRIMA di spendere; solleva se non basta.

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
    """Prezzo per token (stringa dall'API) -> dollari per milione di token."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    # I modelli "auto" espongono prezzi sentinella negativi: valgono 0.
    return price * 1_000_000 if price > 0 else 0.0


def _model_from_payload(raw: dict) -> Optional[ModelInfo]:
    model_id = raw.get("id")
    if not model_id:
        return None
    pricing = raw.get("pricing") or {}
    architecture = raw.get("architecture") or {}
    return ModelInfo(
        id=model_id,
        name=raw.get("name") or model_id,
        prompt_usd_per_mtok=_parse_price(pricing.get("prompt")),
        completion_usd_per_mtok=_parse_price(pricing.get("completion")),
        context_length=int(raw.get("context_length") or 0),
        input_modalities=tuple(architecture.get("input_modalities") or ()),
        supports_tools="tools" in (raw.get("supported_parameters") or ()),
    )


def _load_models_cache(ignore_ttl: bool = False) -> Optional[list[dict]]:
    """Legge la cache su disco; None se manca, e' illeggibile o e' scaduta."""
    if not MODELS_CACHE_PATH.is_file():
        return None
    try:
        if not ignore_ttl:
            age = time.time() - MODELS_CACHE_PATH.stat().st_mtime
            if age > MODELS_CACHE_TTL_S:
                return None
        with open(MODELS_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_models_cache(payload: list[dict]) -> None:
    try:
        with open(MODELS_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        # La cache e' un'ottimizzazione: se non si puo' scrivere, pazienza.
        pass


def list_models(force_refresh: bool = False) -> list[ModelInfo]:
    """Elenco dei modelli disponibili su OpenRouter, ordinato per costo.

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
                payload = json.load(response).get("data") or []
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
    models.sort(key=lambda m: (m.prompt_usd_per_mtok, m.completion_usd_per_mtok, m.id))
    return models


def _safe_error_text(value):
    """Return a fixed error category, never arbitrary provider/request text.

    Redaction alone cannot identify an echoed private prompt. After removing
    known credentials/media, recognize only fixed categories and discard the
    remainder. Do not stringify containers or exception objects.
    """
    if not isinstance(value, str):
        return tx("dettagli del provider omessi")
    key = os.environ.get(API_KEY_ENV, "").strip()
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
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": list(content_blocks)},
        ],
        "tools": [tool],
        "tool_choice": "required" if compatibility_retry else {
            "type": "function", "function": {"name": tool["function"]["name"]}},
    }
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
        raise OpenRouterError(
            tx("OpenRouter ha risposto con un errore: {error_message} "
               "[request_bytes={n_data}; images={n_image_sizes}; "
               "largest_image_encoded_bytes={max}]",
               error_message=_error_message(exc),
               n_data=len(request.data),
               n_image_sizes=len(image_sizes),
               max=max(image_sizes, default=0)),
            retryable=status in {400, 408, 409, 429, 500, 502, 503, 504},
        ) from None
    except urllib.error.URLError as exc:
        raise OpenRouterError(f"Connessione a OpenRouter fallita: {_safe_error_text(exc.reason)}"
                              + _diagnostic_context()) from None
    except ValueError as exc:
        raise OpenRouterError(tx("Risposta non decodificabile da OpenRouter.")
                              + _diagnostic_context()) from None
    except OSError as exc:
        raise OpenRouterError("Connessione a OpenRouter fallita: "
                              + _safe_error_text(str(exc) if isinstance(exc, TimeoutError) else None)
                              + _diagnostic_context()) from None

    # Prima di tool_arguments, che puo' sollevare: la chiamata e' stata pagata
    # comunque, e un consuntivo che ignora i tentativi falliti e' falso.
    _record_usage(payload)
    return tool_arguments(payload, tool["function"]["name"])


def tool_arguments(payload: dict, tool_name: str) -> dict:
    """@brief Extract explicit object arguments with safe provider diagnostics.

    @param payload Decoded provider response; message/prompt bodies are never logged.
    @param tool_name Expected function name.
    @return Parsed argument object, including an explicitly supplied empty object.
    @throws OpenRouterError Missing/empty arguments, invalid JSON/envelope, truncation
        without a usable tool call, or provider error.
    @note No fallback empty object and no automatic retry on incomplete responses.
    """
    context = _diagnostic_context(payload)
    if not isinstance(payload, dict):
        raise OpenRouterError(tx("La risposta di OpenRouter non contiene alcun risultato.") + context)
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise OpenRouterError(_safe_error_text(message) + context)

    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OpenRouterError(tx("La risposta di OpenRouter non contiene alcun risultato.") + context)

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
            raise OpenRouterError(tx("Gli argomenti dello strumento sono assenti o vuoti.") + context)
        if not isinstance(arguments, str):
            raise OpenRouterError(tx("Gli argomenti dello strumento non sono JSON valido (atteso oggetto).") + context)
        try:
            decoded = json.loads(arguments)
        except ValueError as exc:
            raise OpenRouterError(
                tx("Gli argomenti dello strumento non sono JSON valido.") + context
            ) from None
        if not isinstance(decoded, dict):
            raise OpenRouterError(tx("Gli argomenti dello strumento non sono JSON valido (atteso oggetto).") + context)
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
