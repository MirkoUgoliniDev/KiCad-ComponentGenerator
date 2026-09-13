"""Configurazione applicativa persistente (config.json accanto a main.py).

Qui restano le preferenze dell'interfaccia. Prompt e modelli precedenti vengono
letti solo per la migrazione iniziale al database SQLite dei profili AI;
save_config non li riscrive nel JSON. I parametri del singolo footprint
vivono nei campi della finestra.

Il file e' un JSON in chiaro, cosi' e' ispezionabile e modificabile a mano;
non contiene segreti (la API key resta nel .env).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.openrouter import DEFAULT_MODEL
from ..services.photo_grounding import DEFAULT_VISION_MODEL, GROUNDING_PROMPT, VERIFICATION_PROMPT

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"


def config_path() -> Path:
    """@brief Provide the config path operation for this workflow."""
    return Path(os.environ.get("KICADGEN_CONFIG_PATH", CONFIG_PATH))

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
    return hashlib.sha256((prompt if isinstance(prompt, str) else '').strip().encode("utf-8")).hexdigest()

DEFAULTS = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # Modello di default dell'estrazione AI: id OpenRouter, scelto dalla
    # finestra "Modello e prompt di sistema LLM". Per una singola estrazione
    # se ne puo' usare un altro senza toccare questo valore.
    "model": DEFAULT_MODEL,
    "vision_model": DEFAULT_VISION_MODEL,
    "grounding_prompt": GROUNDING_PROMPT,
    "verification_prompt": VERIFICATION_PROMPT,
    # Ultima tipologia di componente selezionata: si ripropone all'avvio,
    # perche' di solito si generano in serie componenti dello stesso tipo.
    "component_type": "",
    # I pin NC (Not Connected) sono elettricamente scollegati ma fisicamente
    # presenti: di default vanno generati, altrimenti manca il foro e il
    # componente non entra nella scheda.
    "include_nc_pins": True,
    "advanced_user": False,
    "show_grid": True,
    "grid_unit": "mm",  # "mm" oppure "inch" (passo 2.54mm, standard THT)
    "show_pin_numbers_3d": True,
}


def load_config() -> dict:
    """Legge config.json, completando le chiavi mancanti con i default."""
    config = dict(DEFAULTS)
    if config_path().is_file():
        try:
            with open(config_path(), "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if not isinstance(loaded, dict):
                    raise ValueError('La configurazione deve essere un oggetto JSON.')
                config.update(loaded)
            if _prompt_hash(config.get("system_prompt") or "") in LEGACY_DEFAULT_PROMPT_HASHES:
                config["system_prompt"] = DEFAULT_SYSTEM_PROMPT
        except (OSError, ValueError):
            # File illeggibile o corrotto: si riparte dai default invece di
            # impedire l'avvio dell'applicazione.
            pass
    return config


def save_config(config: dict) -> None:
    """@brief Provide the save config operation for this workflow."""
    # AI profiles are persisted only in SQLite after startup migration.
    from ..services.analysis_profiles import PROFILE_FIELDS
    preferences = {k: v for k, v in config.items() if k not in PROFILE_FIELDS}
    with open(config_path(), "w", encoding="utf-8") as fh:
        json.dump(preferences, fh, indent=2, ensure_ascii=False)
