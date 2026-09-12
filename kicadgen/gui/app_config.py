"""Configurazione applicativa persistente (config.json accanto a main.py).

Qui stanno le impostazioni GLOBALI, che valgono per tutti i componenti e
sopravvivono alla chiusura dell'app -- a differenza dei parametri del singolo
footprint, che vivono solo nei campi della finestra.

Il file e' un JSON in chiaro, cosi' e' ispezionabile e modificabile a mano;
non contiene segreti (la API key resta nel .env).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"

DEFAULTS = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
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
        except (OSError, ValueError):
            # File illeggibile o corrotto: si riparte dai default invece di
            # impedire l'avvio dell'applicazione.
            pass
    return config


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
