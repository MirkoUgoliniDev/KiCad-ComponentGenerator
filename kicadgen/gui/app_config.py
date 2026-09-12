"""Configurazione applicativa persistente (config.json accanto a main.py).

Qui stanno le impostazioni GLOBALI, che valgono per tutti i componenti e
sopravvivono alla chiusura dell'app -- a differenza dei parametri del singolo
footprint, che vivono solo nei campi della finestra.

Il file e' un JSON in chiaro, cosi' e' ispezionabile e modificabile a mano;
non contiene segreti (la API key resta nel .env).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.openrouter import DEFAULT_MODEL

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"

#: SHA-256 dei prompt di sistema che sono stati il default in passato.
#: Un config.json che contiene ancora uno di questi non e' stato
#: personalizzato dall'utente: viene aggiornato al default corrente, cosi'
#: i miglioramenti al prompt arrivano anche a chi ha gia' il file salvato.
#: Chi invece l'ha modificato se lo tiene: la sua versione non e' in lista.
LEGACY_DEFAULT_PROMPT_HASHES = {
    # Default fino alla versione senza il campo "layout" (solo matrice).
    "c744e556d5e1eba95f41d69b5eb635c2ab8bcc0cf77f5c1d163b18ca3a41feea",
    # Default che chiedeva solo i componenti principali ("non serve mapparli tutti").
    "eefd487b98f9217c71c14976fe71ac0095a14dd8ed9b8bd1b5d58e8231c0aafa",
}


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()

DEFAULTS = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # Modello di default dell'estrazione AI: id OpenRouter, scelto dalla
    # finestra "Modello e prompt di sistema LLM". Per una singola estrazione
    # se ne puo' usare un altro senza toccare questo valore.
    "model": DEFAULT_MODEL,
    # Ultima tipologia di componente selezionata: si ripropone all'avvio,
    # perche' di solito si generano in serie componenti dello stesso tipo.
    "component_type": "",
    # I pin NC (Not Connected) sono elettricamente scollegati ma fisicamente
    # presenti: di default vanno generati, altrimenti manca il foro e il
    # componente non entra nella scheda.
    "include_nc_pins": True,
    "show_grid": True,
    "grid_unit": "mm",  # "mm" oppure "inch" (passo 2.54mm, standard THT)
    "show_pin_numbers_3d": True,
}


def load_config() -> dict:
    """Legge config.json, completando le chiavi mancanti con i default."""
    config = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                config.update(json.load(fh))
            if _prompt_hash(config.get("system_prompt") or "") in LEGACY_DEFAULT_PROMPT_HASHES:
                config["system_prompt"] = DEFAULT_SYSTEM_PROMPT
        except (OSError, ValueError):
            # File illeggibile o corrotto: si riparte dai default invece di
            # impedire l'avvio dell'applicazione.
            pass
    return config


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
