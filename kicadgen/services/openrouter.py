"""Client minimale per l'API OpenRouter (https://openrouter.ai/api/v1).

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
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

API_BASE = "https://openrouter.ai/api/v1"
MODELS_URL = f"{API_BASE}/models"
CHAT_URL = f"{API_BASE}/chat/completions"

API_KEY_ENV = "OPENROUTER_API_KEY"

#: Modello usato quando l'utente non ne ha ancora scelto uno.
DEFAULT_MODEL = "anthropic/claude-sonnet-5"

#: Cache locale dell'elenco modelli (e' lungo ~1 MB e cambia di rado).
MODELS_CACHE_PATH = Path(__file__).resolve().parents[2] / ".openrouter_models.json"
MODELS_CACHE_TTL_S = 24 * 3600

# Header opzionali di OpenRouter: identificano l'applicazione chiamante
# nelle statistiche del proprio account.
_APP_HEADERS = {
    "HTTP-Referer": "https://github.com/kicadgen-tool",
    "X-Title": "KiCadGen-Tool",
}

_HTTP_TIMEOUT_MODELS_S = 30
_HTTP_TIMEOUT_CHAT_S = 300


class OpenRouterError(Exception):
    """Errore di configurazione, rete o risposta inattesa da OpenRouter."""


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
        return "image" in self.input_modalities

    @property
    def supports_pdf(self) -> bool:
        return "file" in self.input_modalities

    @property
    def is_free(self) -> bool:
        return self.prompt_usd_per_mtok <= 0 and self.completion_usd_per_mtok <= 0

    def usable_for_datasheets(self) -> bool:
        """True se il modello puo' fare il lavoro: tool calling + PDF/immagini.

        L'estrazione manda disegni meccanici (immagini o PDF) e pretende la
        risposta come chiamata a uno strumento: un modello senza queste due
        capacita' fallirebbe la richiesta, quindi la GUI lo nasconde.
        """
        return self.supports_tools and self.supports_images and self.supports_pdf


def api_key() -> str:
    """Ritorna la chiave OpenRouter dall'ambiente, o solleva OpenRouterError."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise OpenRouterError(
            f"Variabile d'ambiente {API_KEY_ENV} non impostata. "
            "Inseriscila nel file .env accanto a main.py (vedi .env.example)."
        )
    return key


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
                raise OpenRouterError(f"Elenco modelli non scaricabile: {exc}") from exc
        else:
            _write_models_cache(payload)

    models = [m for m in (_model_from_payload(raw) for raw in payload) if m is not None]
    models.sort(key=lambda m: (m.prompt_usd_per_mtok, m.completion_usd_per_mtok, m.id))
    return models


def _error_message(exc: urllib.error.HTTPError) -> str:
    """Estrae il messaggio d'errore dal corpo JSON della risposta, se c'e'."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        message = (body.get("error") or {}).get("message")
        if message:
            return f"{exc.code} {message}"
    except (ValueError, OSError):
        pass
    return f"HTTP {exc.code} {exc.reason}"


def call_tool(
    model: str,
    system_prompt: str,
    content_blocks: Sequence[dict],
    tool: dict,
    max_tokens: int = 2048,
    has_pdf: bool = False,
    pdf_engine: str = "native",
) -> dict:
    """Chiama il modello forzando la risposta come chiamata allo strumento.

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
        "tool_choice": {
            "type": "function",
            "function": {"name": tool["function"]["name"]},
        },
    }
    if has_pdf:
        body["plugins"] = [{"id": "file-parser", "pdf": {"engine": pdf_engine}}]

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
        raise OpenRouterError(
            f"OpenRouter ha risposto con un errore: {_error_message(exc)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise OpenRouterError(f"Connessione a OpenRouter fallita: {exc.reason}") from exc
    except ValueError as exc:
        raise OpenRouterError("Risposta non decodificabile da OpenRouter.") from exc

    return tool_arguments(payload, tool["function"]["name"])


def tool_arguments(payload: dict, tool_name: str) -> dict:
    """Estrae gli argomenti della chiamata allo strumento dalla risposta."""
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise OpenRouterError(str(message))

    choices = payload.get("choices") or []
    if not choices:
        raise OpenRouterError("La risposta di OpenRouter non contiene alcun risultato.")

    message = choices[0].get("message") or {}
    for call in message.get("tool_calls") or ():
        function = call.get("function") or {}
        if function.get("name") != tool_name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            return arguments
        try:
            return json.loads(arguments or "{}")
        except ValueError as exc:
            raise OpenRouterError(
                "Gli argomenti dello strumento non sono JSON valido."
            ) from exc

    raise OpenRouterError(
        "Il modello non ha chiamato lo strumento richiesto (potrebbe non "
        "supportare il tool calling con immagini/PDF): scegline un altro "
        "dall'elenco, filtrando per costo se serve."
    )
