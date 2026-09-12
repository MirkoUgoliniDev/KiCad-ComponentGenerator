"""Estrazione parametri footprint da datasheet tramite l'API OpenRouter.

Manda i file di documentazione (PDF/immagini) a OpenRouter -- che con una
sola chiave da' accesso ai modelli di tutti i fornitori -- e richiede di
compilare uno strumento (tool calling) con lo schema dei parametri del
pin header, cosi' la risposta e' sempre JSON strutturato valido, mai
testo libero da fare il parsing a mano.

Il modello si sceglie dalla GUI (id OpenRouter, es.
"anthropic/claude-sonnet-5"): l'impostazione globale sta in config.json,
mentre per una singola estrazione se ne puo' usare uno piu' potente senza
cambiare il default.

Richiede la variabile d'ambiente OPENROUTER_API_KEY (di norma nel file
.env accanto a main.py). Nessuna chiave viene inserita nel codice.
"""

from __future__ import annotations

import base64
import mimetypes
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..cad3d.annotations import EDGE_NAMES
from ..generators.castellated_module import EDGES
from .openrouter import DEFAULT_MODEL, OpenRouterError, api_key, call_tool

MAX_FILES = 5

#: Tipi di package che il modello 3D sa disegnare (cad3d/freecad_parts.py:
#: BUILDERS). Duplicato qui perche' quel modulo gira solo dentro FreeCAD.
COMPONENT_KINDS = ('generic', 'ic_soic', 'ic_qfn', 'transformer', 'module', 'led', 'capacitor_tantalum', 'capacitor', 'resistor', 'inductor', 'diode', 'diode_melf', 'connector', 'crystal')
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB, limite ragionevole per singolo file API

_SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

_TOOL_NAME = "report_pin_header_params"

_EXTRACTION_TOOL = {
    "name": _TOOL_NAME,
    "description": (
        "Riporta i parametri meccanici di un connettore/header a matrice di pin "
        "(righe x colonne, passo, diametro pad, drill) estratti dal datasheet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "layout": {
                "type": "string",
                "enum": ["matrix", "castellated"],
                "description": (
                    "Come sono disposti i contatti del componente.\n"
                    "- \"matrix\": pin su una griglia regolare righe x colonne con UN "
                    "SOLO passo (pin header, moduli DIP-style, componenti THT o SMD "
                    "a due file simmetriche). Compila rows, cols, pitch_mm, "
                    "row_spacing_mm, pad_diameter_mm, drill_mm.\n"
                    "- \"castellated\": modulo su PCB con semi-fori metallizzati sul "
                    "bordo (es. moduli WiFi/BLE, convertitori isolati). Compila "
                    "castellated_sides e i campi body_*; NON forzare i contatti in una "
                    "matrice, perche' lati diversi hanno spesso numero di contatti e "
                    "passo diversi (es. 3 contatti a 5.08mm a sinistra e 5 a 2.54mm a "
                    "destra): in quel caso \"matrix\" darebbe un footprint sbagliato."
                ),
            },
            "top_photo_index": {
                "type": "integer", "minimum": -1,
                "description": (
                    "Indice FILE (0-based, etichetta FILE nel messaggio) della migliore FOTO "
                    "dall'alto del lato con componenti montati, asse lungo orizzontale. "
                    "Escludi vista inferiore, laterale, prospettica e disegni. "
                    "Usa -1 se nessuna foto e' adatta. Non scegliere solo per risoluzione."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Nome KiCad suggerito per il footprint, stile "
                    "PinHeader_<righe>x<colonne>_P<passo>mm_THT"
                ),
            },
            "description": {"type": "string", "description": "Descrizione breve del componente"},
            "tags": {"type": "string", "description": "Tag separati da spazio"},
            "rows": {"type": "integer", "description": "Numero di righe della matrice di pin (es. 2 per un componente a doppia fila)"},
            "cols": {"type": "integer", "description": "Numero di colonne della matrice (pin per riga)"},
            "pitch_mm": {
                "type": "number",
                "description": "Passo tra pin ADIACENTI sulla STESSA riga, in millimetri (es. 2.54mm)",
            },
            "row_spacing_mm": {
                "type": "number",
                "description": (
                    "Distanza PERPENDICOLARE tra le due file di pin, in millimetri. "
                    "IMPORTANTE: nei package DIP-style (es. moduli/convertitori DC-DC) "
                    "questa e' quasi sempre molto maggiore del pitch (es. 7.62/10.16/15.24mm "
                    "contro un pitch di 2.54mm) -- non confonderla col pitch."
                ),
            },
            "pad_diameter_mm": {"type": "number", "description": "Diametro pad THT in millimetri"},
            "drill_mm": {"type": "number", "description": "Diametro foro (drill) in millimetri"},
            "populated_pins": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "SOLO se il pinout del datasheet mostra che non tutte le posizioni "
                    "della matrice rows x cols hanno un pin fisico (pinout parziale, "
                    "tipico dei moduli DIP-style): elenca i numeri di pin EFFETTIVAMENTE "
                    "presenti secondo la numerazione del datasheet. Se la matrice e' "
                    "completamente popolata (es. un normale pin header), OMETTI questo campo."
                ),
            },
            "body_length_mm": {
                "type": "number",
                "description": (
                    "SOLO se il datasheet ha un disegno meccanico (\"Outline Dimensions\" "
                    "o simile) con l'ingombro del corpo del componente: lunghezza del "
                    "corpo (asse X, stessa direzione del pitch) in millimetri. Ometti se "
                    "il disegno meccanico non e' presente."
                ),
            },
            "body_width_mm": {
                "type": "number",
                "description": (
                    "Larghezza del corpo, asse Y (vista dall'alto/bottom view del "
                    "disegno meccanico -- e' la dimensione del CONTORNO ESTERNO del "
                    "case, NON la distanza tra le due righe di pin: e' quasi sempre "
                    "un valore diverso e leggermente maggiore di row_spacing_mm, "
                    "perche' il case e' piu' largo della sola distanza tra i pin."
                ),
            },
            "body_height_mm": {
                "type": "number",
                "description": (
                    "Spessore/altezza del corpo lungo l'asse Z (perpendicolare al PCB, "
                    "vista laterale/side view del disegno meccanico) -- e' una quota "
                    "COMPLETAMENTE DIVERSA da row_spacing_mm e body_width_mm (che sono "
                    "quote della vista dall'alto, assi X/Y). Cercala nella vista "
                    "laterale del disegno, di solito la quota piu' piccola vicino ai "
                    "pin (es. per un case tipo DIP-16 di pochi mm di spessore e' "
                    "spesso 5-10mm, quasi sempre diversa dalla riga precedente)."
                ),
            },
            "body_offset_x_mm": {
                "type": "number",
                "description": (
                    "Posizione del bordo del corpo lungo X rispetto al pin 1 (che e' "
                    "sempre nell'origine x=0 del footprint). Tipicamente un valore "
                    "negativo pari alla distanza dal bordo sinistro del corpo al pin 1, "
                    "presa dalla quota del disegno meccanico (es. se il pin 1 e' a 3.0mm "
                    "dal bordo sinistro, body_offset_x_mm = -3.0)."
                ),
            },
            "body_offset_y_mm": {
                "type": "number",
                "description": (
                    "Posizione del bordo del corpo lungo Y rispetto alla prima riga di "
                    "pin (y=0), stessa logica di body_offset_x_mm ma per l'asse "
                    "perpendicolare (tipicamente meta' della larghezza del corpo, con "
                    "segno negativo, se le righe di pin sono centrate rispetto al corpo)."
                ),
            },
            "pin_length_3d_mm": {
                "type": "number",
                "description": (
                    "Lunghezza del pin che sporge sotto il corpo (protrusione verso il "
                    "PCB), in millimetri -- quota meccanica, NON il drill del footprint."
                ),
            },
            "edge_fillets": {
                "type": "array",
                "description": (
                    "Se una foto reale del componente (non il disegno meccanico quotato) "
                    "mostra spigoli arrotondati/smussati sul case, elenca quali spigoli "
                    "del parallelepipedo appaiono arrotondati con un raggio STIMATO A "
                    "OCCHIO. Le foto prodotto non riportano MAI un valore quotato per "
                    "questi raggi: e' normale e atteso stimarlo visivamente (es. 0.5-1.5mm "
                    "per un case plastico stampato con spigoli visibilmente smussati), "
                    "NON serve una quota esplicita per compilare questo campo -- una stima "
                    "ragionevole e' meglio di ometterlo quando l'arrotondamento e' visibile. "
                    "Se il case in foto ha chiaramente spigoli vivi (angoli a 90 gradi "
                    "netti), o non c'e' nessuna foto del prodotto, ometti il campo."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "edge": {
                            "type": "string",
                            "enum": list(EDGE_NAMES),
                            "description": "Nome dello spigolo (vedi convenzione: top/bottom-front/back/left/right)",
                        },
                        "radius_mm": {
                            "type": "number",
                            "description": (
                                "Raggio stimato in millimetri dalla foto (nessuna quota "
                                "disponibile per un raggio visivo: usa una stima standard "
                                "plausibile, es. 0.5-1.5mm per un case plastico stampato)."
                            ),
                        },
                    },
                    "required": ["edge", "radius_mm"],
                },
            },
            "pin_map": {
                "type": "array",
                "description": (
                    "Posizione di ogni pin nella matrice, ESATTAMENTE COME APPARE NEL "
                    "DISEGNO: non ribaltare e non ruotare nulla, riporta solo cio' che "
                    "vedi (dell'eventuale specchiatura si occupa il programma, in base "
                    "a pin_map_view). Serve quando la numerazione NON segue l'ordine "
                    "colonna per colonna (1,2 poi 3,4...) tipico dei pin header: i "
                    "package DIP-style numerano a U (1..N lungo una fila, poi a ritroso "
                    "sull'altra). Ometti solo se il documento non mostra la disposizione "
                    "fisica dei pin."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "integer", "description": "Numero del pin"},
                        "row": {
                            "type": "integer",
                            "description": "Indice riga, 0 = prima fila (y=0), 1 = fila successiva",
                        },
                        "col": {
                            "type": "integer",
                            "description": "Indice colonna, 0 = colonna piu' a sinistra NEL DISEGNO",
                        },
                    },
                    "required": ["pin", "row", "col"],
                },
            },
            "pin_map_view": {
                "type": "string",
                "enum": ["top", "bottom"],
                "description": (
                    "Da che lato e' visto il disegno da cui hai letto pin_map: 'bottom' "
                    "se e' etichettato BOTTOM VIEW (vista da sotto, quella tipica dei "
                    "datasheet dei moduli), 'top' se e' una vista dall'alto. E' "
                    "un'informazione critica: il footprint KiCad si disegna visto "
                    "dall'alto, quindi una bottom view va specchiata -- ci pensa il "
                    "programma, a te basta dire quale vista hai letto."
                ),
            },
            "pinout": {
                "type": "array",
                "description": (
                    "Tabella pinout del datasheet: per ogni pin FISICAMENTE PRESENTE, "
                    "il numero e la funzione. Se il datasheet elenca piu' configurazioni "
                    "(es. colonne 'Single' e 'Dual'), riporta la prima e aggiungi "
                    "l'alternativa tra parentesi solo se diversa (es. 'NC (Dual: Common)'). "
                    "Ometti se nel documento non c'e' una tabella pinout."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "integer", "description": "Numero del pin"},
                        "name": {
                            "type": "string",
                            "description": "Funzione del pin, es. '-Vin (GND)', 'NC', '+Vout'",
                        },
                    },
                    "required": ["pin", "name"],
                },
            },
            "body_color_hex": {
                "type": "string",
                "description": (
                    "Colore del case letto dalla FOTO del prodotto, formato '#rrggbb' "
                    "(es. '#1a1a1a' per un case nero, '#c41230' per uno rosso). Usa il "
                    "colore dell'involucro, non quello di un'etichetta applicata sopra "
                    "-- a meno che l'etichetta copra di fatto tutta la faccia superiore, "
                    "nel qual caso e' quello il colore che si vede. "
                    "Ometti se non c'e' una foto del prodotto."
                ),
            },
            "text_color_hex": {
                "type": "string",
                "description": (
                    "Colore della serigrafia/scritte sul case nella foto, formato "
                    "'#rrggbb' (es. '#ffffff' per scritte bianche su case scuro). "
                    "Ometti se non c'e' una foto o non si leggono scritte."
                ),
            },
            "castellated_sides": {
                "type": "array",
                "description": (
                    "SOLO con layout=\"castellated\": un elemento per ogni lato del "
                    "modulo che porta contatti, letto dal disegno/foto visto DALL'ALTO "
                    "(top view). Ogni lato ha il suo numero di contatti e il suo passo: "
                    "e' proprio il motivo per cui questi moduli non stanno in una "
                    "matrice. L'origine e' il centro del corpo."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "edge": {
                            "type": "string",
                            "enum": list(EDGES),
                            "description": (
                                "Lato del modulo visto dall'alto: \"left\"/\"right\" i "
                                "due lati corti o lunghi verticali, \"top\"/\"bottom\" "
                                "quelli orizzontali. \"top\" e' il lato in alto nel "
                                "disegno."
                            ),
                        },
                        "count": {
                            "type": "integer",
                            "description": "Numero di contatti su questo lato.",
                        },
                        "pitch_mm": {
                            "type": "number",
                            "description": (
                                "Passo tra contatti adiacenti SU QUESTO LATO, in "
                                "millimetri: puo' essere diverso da quello degli altri "
                                "lati (es. 5.08 su un lato e 2.54 sull'altro)."
                            ),
                        },
                        "first_pin": {
                            "type": "integer",
                            "description": (
                                "Numero del primo contatto di questo lato, seguendo il "
                                "bordo dall'alto in basso (lati verticali) o da sinistra "
                                "a destra (lati orizzontali)."
                            ),
                        },
                        "reverse": {
                            "type": "boolean",
                            "description": (
                                "true se su questo lato la numerazione cresce nel verso "
                                "opposto (dal basso verso l'alto, o da destra a sinistra)."
                            ),
                        },
                        "offset_mm": {
                            "type": "number",
                            "description": (
                                "Spostamento del gruppo di contatti lungo il bordo "
                                "rispetto alla posizione centrata, in millimetri "
                                "(positivo verso il basso o verso destra). Ometti o 0 "
                                "se i contatti sono centrati sul lato."
                            ),
                        },
                        "pad_length_mm": {
                            "type": "number",
                            "description": (
                                "Lunghezza della piazzola perpendicolarmente al bordo "
                                "(quanto entra sotto il modulo piu' quanto sporge), "
                                "dalla \"Recommended PCB land pattern\". Ometti se il "
                                "datasheet non la riporta."
                            ),
                        },
                        "pad_width_mm": {
                            "type": "number",
                            "description": (
                                "Larghezza della piazzola lungo il bordo (di norma poco "
                                "piu' del diametro del semi-foro). Ometti se assente."
                            ),
                        },
                        "pin_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Sigle serigrafate accanto ai contatti di questo lato, "
                                "nell'ordine in cui si incontrano lungo il bordo (es. "
                                "[\"PE\", \"B-\", \"A+\"]). Servono a documentare il "
                                "pinout, non a numerare i pad."
                            ),
                        },
                    },
                    "required": ["edge", "count", "pitch_mm"],
                },
            },
            "components_3d": {
                "type": "array",
                "description": (
                    "SOLO per i moduli su PCB (layout=\"castellated\") e solo se tra i "
                    "file c'e' una FOTO del lato superiore: i componenti visibili, "
                    "approssimati a blocchi rettangolari, per dare al modello 3D "
                    "l'aspetto del modulo reale invece di una lastra liscia. "
                    "Elenca TUTTI i componenti visibili, uno per uno: trasformatore, "
                    "integrati, connettori, LED e anche OGNI singolo passivo piccolo "
                    "(resistenze e condensatori chip 0402/0603/0805, diodi), ciascuno "
                    "con la propria posizione e il proprio orientamento (length_mm "
                    "asse lungo locale, width_mm asse corto, rotation_deg orientamento). Un modulo tipico ne ha 30-50: la "
                    "lista e' lunga ed e' giusto cosi'. Nessuna quota di questi e' "
                    "data dal datasheet -- sono stime visive in proporzione alle "
                    "dimensioni note del modulo, ed e' normale che sia cosi'. "
                    "Ometti il campo se non c'e' una foto dall'alto."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Sigla o tipo del componente, es. \"trasformatore\", "
                                "\"SP3485\", \"LED_R\": finisce nel nome del solido."
                            ),
                        },
                        "x_mm": {
                            "type": "number",
                            "description": (
                                "Posizione del CENTRO del componente lungo X rispetto al "
                                "centro del modulo, in millimetri: negativa verso "
                                "sinistra, positiva verso destra nella foto vista "
                                "dall'alto (il modulo e' lungo body_length_mm)."
                            ),
                        },
                        "y_mm": {
                            "type": "number",
                            "description": (
                                "Posizione del CENTRO lungo Y rispetto al centro del "
                                "modulo: negativa verso l'alto della foto, positiva "
                                "verso il basso (il modulo e' largo body_width_mm)."
                            ),
                        },
                        "length_mm": {
                            "type": "number",
                            "description": (
                                "Lunghezza del componente lungo il SUO asse lungo (es. 1.6 "
                                "per un chip 0603, 2.0 per un diodo MELF), indipendentemente "
                                "da come e' ruotato sul modulo: l'orientamento va in rotation_deg."
                            ),
                        },
                        "width_mm": {
                            "type": "number",
                            "description": "Larghezza lungo l'asse corto del componente.",
                        },
                        "rotation_deg": {
                            "type": "number",
                            "enum": [0, 90, 180, 270],
                            "description": (
                                "Come il componente sta sul modulo visto dall'alto: 0 se il "
                                "suo asse lungo e' orizzontale (parallelo al lato lungo del "
                                "modulo, X), 90 se e' verticale (Y). Guardalo nella foto: un "
                                "diodo o una resistenza \"in piedi\" e' 90. 180/270 solo se "
                                "conta il verso (banda del diodo, pin 1) e lo vedi."
                            ),
                        },
                        "height_mm": {
                            "type": "number",
                            "description": (
                                "Altezza sopra il PCB, stimata dal tipo di package (es. "
                                "~1.0mm per un SOIC, ~0.5mm per resistenze/LED 0805, "
                                "qualche mm per un trasformatore). Non superare "
                                "l'altezza totale del modulo meno lo spessore del PCB."
                            ),
                        },
                        "color_hex": {
                            "type": "string",
                            "description": "Colore visto in foto, '#rrggbb'.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ['generic', 'ic_soic', 'ic_qfn', 'transformer', 'module', 'led', 'capacitor_tantalum', 'capacitor', 'resistor', 'inductor', 'diode', 'diode_melf', 'connector', 'crystal'],
                            "description": (
                                "Tipo di package, riconosciuto dalla foto: decide la forma "
                                "nel modello 3D (un SOIC ha i reofori sui lati, un LED la "
                                "lente, un tantalio la striscia di polarita'). "
                                "\"capacitor\"/\"resistor\" sono i chip SMD rettangolari con le "
                                "due terminazioni metalliche; \"diode_melf\" e' il diodo "
                                "CILINDRICO di vetro (spesso rosso o nero) con banda e cappucci, "
                                "da non confondere con un chip; \"generic\" se non riconoscibile."
                            ),
                        },
                        "marking": {
                            "type": "string",
                            "description": (
                                "Scritta stampata sul componente, SOLO se leggibile nella "
                                "foto (es. \"SP3485\", \"VPT\", \"102\"): viene stampata "
                                "sul solido. Non inventare codici."
                            ),
                        },
                    },
                    "required": ["x_mm", "y_mm", "length_mm", "width_mm", "height_mm"],
                },
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Quanto sei sicuro dei valori estratti dal datasheet",
            },
            "notes": {
                "type": "string",
                "description": "Ambiguita', assunzioni fatte o valori non trovati nel datasheet",
            },
        },
        # Obbligatori sempre; i campi della matrice e quelli dei lati
        # castellated sono richiesti a seconda del layout, e il controllo
        # si fa in _params_from_tool_arguments() dove si conosce il valore.
        "required": ["layout", "confidence"],
    },
}

def _openai_tool() -> dict:
    """Lo stesso schema nel formato "function" richiesto da OpenRouter.

    Lo schema JSON dei parametri e' identico: cambia solo l'involucro
    (Anthropic usa ``input_schema``, l'API OpenAI-compatible di OpenRouter
    usa ``function.parameters``).
    """
    return {
        "type": "function",
        "function": {
            "name": _EXTRACTION_TOOL["name"],
            "description": _EXTRACTION_TOOL["description"],
            "parameters": _EXTRACTION_TOOL["input_schema"],
        },
    }




_SYSTEM_PROMPT = (
    "Sei un assistente che legge datasheet di connettori/header/moduli elettronici "
    "(PDF o immagini di disegni meccanici quotati) ed estrae i parametri "
    "geometrici necessari per generare un footprint KiCad THT a matrice di pin. "
    "Distingui SEMPRE con attenzione il pitch (passo tra pin adiacenti sulla stessa "
    "riga) dalla row_spacing (distanza perpendicolare tra le due righe di pin): sono "
    "quasi sempre valori molto diversi tra loro nei moduli DIP-style, non usare lo "
    "stesso numero per entrambi a meno che il disegno meccanico non lo confermi "
    "esplicitamente. Se il pinout mostra che solo alcune posizioni della matrice hanno "
    "un pin fisico, usa populated_pins per elencarle invece di generare una matrice "
    "completa. Se un valore non e' presente nel datasheet, stima il valore standard "
    "piu' plausibile (es. drill 1.0mm per pad 1.7mm passo 2.54mm) e scrivilo nelle note. "
    "Se il datasheet include un disegno meccanico con l'ingombro del corpo (es. sezione "
    "\"Outline Dimensions\"), compila anche i campi body_* e pin_length_3d_mm; se non "
    "c'e' alcun disegno meccanico dell'ingombro 3D, ometti tutti i campi body_* e "
    "pin_length_3d_mm piuttosto che indovinare le dimensioni del corpo. "
    "ATTENZIONE - errore comune da evitare: body_height_mm (spessore del case, vista "
    "laterale, asse Z) NON e' la stessa quota di row_spacing_mm (distanza tra le righe "
    "di pin, vista dall'alto, asse Y), anche se nel disegno possono comparire vicine "
    "come numeri: leggi ogni quota dalla vista corretta (dall'alto per row_spacing_mm/"
    "body_width_mm, laterale per body_height_mm) invece di riusare un valore gia' letto "
    "altrove. Se hai dubbi su quale numero appartenga a quale quota, guarda quale vista "
    "(dall'alto vs laterale) del disegno la mostra, e scrivi il dubbio nelle note "
    "invece di indovinare silenziosamente. "
    "Se tra i file c'e' anche una foto reale del prodotto (non un disegno tecnico) e "
    "mostra spigoli arrotondati/smussati, compila edge_fillets con gli spigoli "
    "visibilmente arrotondati e un raggio stimato a occhio (le foto prodotto non hanno "
    "MAI un raggio quotato: e' normale stimarlo visivamente, non serve certezza numerica "
    "per compilare questo campo). Ometti edge_fillets solo se il case ha chiaramente "
    "spigoli vivi a 90 gradi, o se non c'e' nessuna foto del prodotto -- non omettere "
    "solo perche' il raggio non e' quotato esplicitamente. "
    "Dalla stessa foto ricava anche body_color_hex (colore del case) e "
    "text_color_hex (colore delle scritte serigrafate sul case): servono per dare "
    "al modello 3D l'aspetto del componente reale. "
    "PRIMA DI TUTTO scegli la disposizione dei contatti (campo layout): "
    "\"matrix\" per i pin su griglia regolare con un solo passo (pin header, "
    "moduli DIP-style), \"castellated\" per i moduli su PCB con semi-fori "
    "metallizzati sul bordo. Nei moduli castellated ogni lato ha spesso un "
    "numero di contatti e un passo diversi dagli altri (es. 3 contatti a 5.08mm "
    "su un lato e 5 a 2.54mm sull'altro): descrivili in castellated_sides, un "
    "elemento per lato, invece di comprimerli in una matrice righe x colonne che "
    "non li rappresenta e produrrebbe un footprint sbagliato. "
    "Per i moduli su PCB, se c'e' una foto del lato superiore, elenca in "
    "components_3d TUTTI i componenti visibili, uno per uno, compresi i passivi "
    "piccoli (resistenze, condensatori chip, diodi): posizione, ingombro (asse "
    "lungo x asse corto), orientamento in rotation_deg (0 orizzontale, 90 "
    "verticale, letto dalla foto) e colore stimati a occhio in proporzione alle dimensioni note "
    "del modulo. Servono a dare al modello 3D l'aspetto del pezzo reale, e "
    "nessuna di quelle quote e' riportata dal datasheet -- e' normale e atteso "
    "stimarle; una lista di 30-50 elementi e' il risultato giusto. Compila sempre "
    "anche pinout con la funzione di ogni contatto, leggendola dalle sigle "
    "serigrafate accanto ai contatti nella foto (es. PE, B-, A+, G, R, T, P, V) "
    "o dalle etichette di un'immagine descrittiva del prodotto. "
    "Rispondi SOLO chiamando lo strumento fornito."
)

# Alias pubblico: la GUI (tab "Prompt LLM") lo mostra come testo di partenza
# editabile, e passa la versione modificata a extract_pin_header_params().
DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT


@dataclass
class ExtractedParams:
    """Risultato dell'estrazione, per entrambe le disposizioni di contatti.

    `layout` dice quali campi sono significativi: "matrix" valorizza
    righe/colonne/passo/pad/drill, "castellated" valorizza
    `castellated_sides` (un elemento per lato del modulo) e lascia a None i
    campi della matrice, che per quei componenti non esistono.
    """

    name: str
    description: str
    tags: str
    confidence: str
    notes: str
    layout: str = "matrix"
    rows: Optional[int] = None
    cols: Optional[int] = None
    pitch_mm: Optional[float] = None
    row_spacing_mm: Optional[float] = None
    pad_diameter_mm: Optional[float] = None
    drill_mm: Optional[float] = None
    castellated_sides: Optional[Sequence[dict]] = None
    components_3d: Optional[Sequence[dict]] = None
    photo_overlay_png: Optional[bytes] = None
    photo_png: Optional[bytes] = None
    photo_source: str = ""
    populated_pins: Optional[Sequence[int]] = None
    body_length_mm: Optional[float] = None
    body_width_mm: Optional[float] = None
    body_height_mm: Optional[float] = None
    body_offset_x_mm: Optional[float] = None
    body_offset_y_mm: Optional[float] = None
    pin_length_3d_mm: Optional[float] = None
    edge_fillets: Optional[Sequence[dict]] = None
    pinout: Optional[Sequence[dict]] = None
    pin_map: Optional[Sequence[dict]] = None
    body_color_hex: Optional[str] = None
    text_color_hex: Optional[str] = None

    def has_3d_body(self) -> bool:
        return self.body_length_mm is not None and self.body_width_mm is not None and self.body_height_mm is not None

    def is_castellated(self) -> bool:
        return self.layout == "castellated"


class ExtractionError(Exception):
    """Errore applicativo (chiave mancante, file non valido, risposta inattesa)."""


def _guess_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type:
        return media_type
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def _file_to_content_block(path: Path) -> dict:
    """Un file di documentazione come blocco di contenuto per OpenRouter.

    Le immagini viaggiano come ``image_url`` con una data-URL base64, i PDF
    come blocco ``file``: sono i due formati previsti dall'API
    OpenAI-compatible di OpenRouter.
    """
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ExtractionError(
            f"Formato non supportato: {path.name} (ammessi: {', '.join(sorted(_SUPPORTED_SUFFIXES))})"
        )
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ExtractionError(f"{path.name} troppo grande ({size / 1_048_576:.1f} MB, max 20 MB)")

    data_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    media_type = _guess_media_type(path)
    data_url = f"data:{media_type};base64,{data_b64}"

    if media_type == "application/pdf":
        return {"type": "file", "file": {"filename": path.name, "file_data": data_url}}
    return {"type": "image_url", "image_url": {"url": data_url}}


def _pin_map_seen_from_top(
    pin_map: Optional[Sequence[dict]], cols: int, view: Optional[str]
) -> Optional[list]:
    """Converte le posizioni lette dal disegno in coordinate viste dall'alto.

    Il footprint KiCad si disegna dal lato componenti, mentre i datasheet dei
    moduli mostrano quasi sempre una BOTTOM VIEW: senza specchiare si otterrebbe
    un footprint speculare, con i fori in posizioni che non coincidono con il
    componente reale.

    La trasformazione si fa qui e non nel prompt perche' e' una riflessione
    geometrica esatta: il modello legge (compito percettivo), il codice ribalta
    (compito deterministico).
    """
    if not pin_map:
        return None

    entries = [
        {"pin": int(e["pin"]), "row": int(e["row"]), "col": int(e["col"])}
        for e in pin_map
    ]
    if view == "bottom":
        for entry in entries:
            entry["col"] = cols - 1 - entry["col"]
    return entries


def _merge_photo_components(groups):
    """Compatibilita' per chiamanti che usano l'inventario in millimetri."""
    from .photo_inventory import merge_components
    return merge_components([_components_3d(g) or [] for g in groups])


def extract_pin_header_params(
    doc_paths: list[Path],
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    include_nc_pins: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    vision_model: Optional[str] = None,
    photo_cache_dir: Optional[Path] = None,
) -> ExtractedParams:
    """Chiama il modello via OpenRouter e ritorna i parametri estratti.

    Due livelli di istruzioni, entrambi opzionali:
    - `system_prompt` sovrascrive DEFAULT_SYSTEM_PROMPT ed e' l'impostazione
      GLOBALE (menu Configurazione della GUI), valida per ogni componente;
    - `extra_instructions` sono note specifiche del SINGOLO componente
      ("il case e' metallico", "ignora il connettore laterale"), accodate al
      messaggio utente insieme ai documenti.

    `include_nc_pins` (default True) decide se i pin marcati NC nel pinout
    vanno comunque generati: sono fisicamente presenti sul componente, e senza
    il loro pad il pezzo non entrerebbe nella scheda.

    `model` e' un id OpenRouter (es. "anthropic/claude-sonnet-5"): serve a
    usare un modello piu' capace sui datasheet difficili senza toccare il
    default globale.

    Lo schema del tool resta invece fisso: e' quello che garantisce che la
    risposta sia sempre JSON strutturato valido.

    Solleva ExtractionError per qualunque problema applicativo: chiave
    mancante, file non supportati o troppo grandi, errori di rete/API,
    risposta senza la chiamata allo strumento.
    """
    if not doc_paths:
        raise ExtractionError("Nessun file selezionato.")
    if len(doc_paths) > MAX_FILES:
        raise ExtractionError(f"Troppi file selezionati (max {MAX_FILES} per chiamata).")

    try:
        api_key()
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    if progress:
        progress(f"Invio a AI {len(doc_paths)} file per layout, quote e pinout...")
    content_blocks = []
    for index, path in enumerate(doc_paths):
        content_blocks.append({"type": "text", "text": f"FILE {index}: {path.name}"})
        content_blocks.append(_file_to_content_block(path))
    request = "Estrai i parametri del pin header da questi documenti e chiama lo strumento."
    if include_nc_pins:
        request += (
            "\n\nPin NC: i pin marcati NC (Not Connected) nella tabella pinout sono "
            "comunque FISICAMENTE PRESENTI sul componente -- non sono collegati "
            "elettricamente, ma il piedino c'e'. Includili quindi in populated_pins: "
            "senza il loro pad il footprint non avrebbe il foro e il componente non "
            "entrerebbe nella scheda."
        )
    else:
        request += (
            "\n\nPin NC: escludi da populated_pins i pin marcati NC (Not Connected), "
            "anche se fisicamente presenti."
        )
    if extra_instructions and extra_instructions.strip():
        request += (
            "\n\nNote specifiche su QUESTO componente, fornite dall'utente "
            "(hanno la precedenza se contraddicono quanto deduci dai documenti):\n"
            + extra_instructions.strip()
        )
    content_blocks.append({"type": "text", "text": request})

    has_pdf = any(p.suffix.lower() == ".pdf" for p in doc_paths)
    try:
        data = call_tool(
            model=model or DEFAULT_MODEL,
            system_prompt=system_prompt or _SYSTEM_PROMPT,
            content_blocks=content_blocks,
            tool=_openai_tool(),
            max_tokens=16384,
            has_pdf=has_pdf,
        )
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    if progress:
        progress(
            f"Analisi principale completata: layout={data.get('layout', 'matrix')}, "
            f"componenti iniziali={len(data.get('components_3d') or [])}."
        )

    # La foto richiede un passaggio separato: nell'estrazione globale il modello
    # deve anche leggere quote, pinout e layout, e tende a sacrificare i passivi
    # piccoli. I crop sono best-effort: se falliscono, resta il risultato globale.
    photo_report = None
    if (data.get("layout") == "castellated" and data.get("body_length_mm")
            and data.get("body_width_mm")):
        from .photo_inventory import extract_inventory
        from .photo_grounding import DEFAULT_VISION_MODEL
        photo_report = extract_inventory(
            doc_paths, vision_model or DEFAULT_VISION_MODEL, float(data["body_length_mm"]),
            float(data["body_width_mm"]), COMPONENT_KINDS, call_tool,
            progress=progress, top_photo_index=data.get("top_photo_index"),
            extra_instructions=extra_instructions,
            cache_dir=photo_cache_dir if photo_cache_dir is not None else Path(__file__).resolve().parents[2]/"generated"/"photo-cache",
            classifier_model=model or DEFAULT_MODEL,
        )
        if photo_report.components:
            data["components_3d"] = photo_report.components
            old_notes = str(data.get("notes") or "").strip()
            suffix = (f"Inventario fotografico: {len(photo_report.components)} ingombri su {photo_report.photo}. "
                      "Modello meccanico semplificato, senza marcature. XY stimati dalla foto calibrata; "
                      "altezze indicative, non misure certificate: verificare con vista laterale o misura.")
            data["notes"] = f"{old_notes} {suffix}".strip()
        if photo_report.warnings:
            details = "\n".join(photo_report.warnings[:6])
            if len(photo_report.warnings) > 6:
                details += f"\nAltri {len(photo_report.warnings)-6} avvisi nel log AI."
            data["notes"] = (str(data.get("notes") or "") + "\n" + details).strip()
    elif progress and data.get("layout") == "castellated":
        progress("Secondo passaggio fotografico non avviato: mancano body_length/body_width.")

    params = _params_from_tool_arguments(data)
    if photo_report and photo_report.components:
        params.photo_overlay_png = photo_report.overlay_png
        params.photo_png = photo_report.photo_png
        params.photo_source = photo_report.photo
    return params


def _castellated_sides(raw: Optional[Sequence[dict]]) -> list[dict]:
    """Normalizza i lati di un modulo castellated, scartando quelli inutilizzabili.

    Il modello puo' restituire un lato senza passo o con zero contatti: si
    preferisce ignorarlo e dirlo, piuttosto che propagare un lato che
    farebbe fallire la generazione con un errore geometrico oscuro.
    """
    if not raw:
        raise ExtractionError(
            "Il modello ha indicato un modulo con piazzole a mezzo foro ma non ha "
            "descritto alcun lato. Riprova, eventualmente con un modello piu' capace."
        )

    sides = []
    for entry in raw:
        edge = str(entry.get("edge") or "").strip().lower()
        count = int(entry.get("count") or 0)
        pitch = float(entry.get("pitch_mm") or 0)
        if edge not in EDGES or count < 1 or (count > 1 and pitch <= 0):
            continue
        side = {"edge": edge, "count": count, "pitch_mm": pitch}
        for key in ("first_pin", "offset_mm", "pad_length_mm", "pad_width_mm"):
            if entry.get(key) is not None:
                side[key] = entry[key]
        if entry.get("reverse"):
            side["reverse"] = True
        if entry.get("pin_names"):
            side["pin_names"] = [str(n) for n in entry["pin_names"]]
        sides.append(side)

    if not sides:
        raise ExtractionError(
            "Nessun lato valido tra quelli restituiti dal modello (servono lato, "
            "numero di contatti e passo)."
        )

    # Numerazione progressiva sui lati privi di first_pin, nell'ordine in cui
    # sono stati elencati: meglio una numerazione coerente da correggere a
    # mano che dei pad senza numero.
    next_pin = 1
    for side in sides:
        if "first_pin" not in side:
            side["first_pin"] = next_pin
        next_pin = int(side["first_pin"]) + side["count"]
    return sides


def _components_3d(raw: Optional[Sequence[dict]]) -> Optional[list[dict]]:
    """Componenti letti dalla foto, scartando quelli senza un ingombro reale.

    Sono stime visive: un blocco con una dimensione nulla o negativa non e'
    un componente mal misurato, e' rumore, e nel solido 3D darebbe errore.
    """
    if not raw:
        return None
    components = []
    for entry in raw:
        if not isinstance(entry, dict):
            # Con liste lunghe (30-50 elementi richiesti) il modello a volte
            # deraglia dallo schema e infila una voce come semplice stringa
            # invece che come oggetto: si scarta quella voce, non l'intera
            # estrazione -- gli altri componenti restano validi.
            continue
        try:
            component = {
                "name": str(entry.get("name") or "Componente"),
                "x_mm": float(entry["x_mm"]),
                "y_mm": float(entry["y_mm"]),
                "length_mm": float(entry["length_mm"]),
                "width_mm": float(entry["width_mm"]),
                "height_mm": float(entry["height_mm"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        # Il modello vede anche elementi della PCB che non sono componenti
        # montati: pad, via, semi-fori e contatti del bordo. Se passano dallo
        # schema, eliminarli qui evita che finiscano nella tabella 3D come
        # scatole. I componenti reali devono avere un package sopra la scheda.
        label = component["name"].strip().lower()
        if (entry.get("object_type") == "pcb_feature" or
                ("object_type" not in entry and re.match(
                    r"^(?:via|pad|piazzola|terminal pad|contatto|castellated|semiforo|serigrafia)\b", label))):
            continue
        if not all(math.isfinite(component[k]) for k in
                   ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")):
            continue
        if min(component["length_mm"], component["width_mm"], component["height_mm"]) <= 0:
            continue
        if entry.get("color_hex"):
            component["color_hex"] = str(entry["color_hex"])
        kind = str(entry.get("kind") or "generic")
        component["kind"] = kind if kind in COMPONENT_KINDS else "generic"
        if entry.get("marking"):
            component["marking"] = str(entry["marking"]).strip()
        try:
            component["rotation_deg"] = float(entry.get("rotation_deg") or 0) % 360
        except (TypeError, ValueError):
            component["rotation_deg"] = 0.0
        for key in ("_box", "_id", "_candidate_id", "_source", "_uncertain", "_review_reason",
                    "_height_source", "_grounding_kind", "_alternative_kind", "confidence"):
            if key in entry:
                component[key] = entry[key]
        components.append(component)
    return components or None


def _params_from_tool_arguments(data: dict) -> ExtractedParams:
    """Costruisce ExtractedParams dagli argomenti dello strumento.

    Separato dalla chiamata di rete per poterlo collaudare senza API.
    """
    layout = data.get("layout") or "matrix"
    if layout not in ("matrix", "castellated"):
        raise ExtractionError(f"Disposizione dei contatti sconosciuta: {layout!r}.")

    def _opt_float(key: str) -> Optional[float]:
        value = data.get(key)
        return float(value) if value is not None else None

    if layout == "castellated":
        sides = _castellated_sides(data.get("castellated_sides"))
    else:
        sides = None
        for key in ("rows", "cols", "pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm"):
            if data.get(key) is None:
                raise ExtractionError(
                    f"La risposta del modello non contiene il campo obbligatorio "
                    f"'{key}'. Riprova, eventualmente con un modello piu' capace."
                )

    populated = data.get("populated_pins")

    return ExtractedParams(
        name=data.get("name") or "",
        description=data.get("description") or "",
        tags=data.get("tags") or "",
        layout=layout,
        rows=int(data["rows"]) if data.get("rows") is not None else None,
        cols=int(data["cols"]) if data.get("cols") is not None else None,
        pitch_mm=_opt_float("pitch_mm"),
        row_spacing_mm=_opt_float("row_spacing_mm"),
        pad_diameter_mm=_opt_float("pad_diameter_mm"),
        drill_mm=_opt_float("drill_mm"),
        castellated_sides=sides,
        components_3d=_components_3d(data.get("components_3d")),
        confidence=data.get("confidence", "low"),
        notes=data.get("notes", ""),
        populated_pins=[int(p) for p in populated] if populated else None,
        body_length_mm=_opt_float("body_length_mm"),
        body_width_mm=_opt_float("body_width_mm"),
        body_height_mm=_opt_float("body_height_mm"),
        body_offset_x_mm=_opt_float("body_offset_x_mm"),
        body_offset_y_mm=_opt_float("body_offset_y_mm"),
        pin_length_3d_mm=_opt_float("pin_length_3d_mm"),
        edge_fillets=data.get("edge_fillets") or None,
        pinout=data.get("pinout") or None,
        # La specchiatura di pin_map ha senso solo sulla matrice: per i moduli
        # castellated le posizioni vengono dai lati, non da righe/colonne.
        pin_map=_pin_map_seen_from_top(
            data.get("pin_map"), int(data["cols"]), data.get("pin_map_view"),
        ) if layout == "matrix" else None,
        body_color_hex=data.get("body_color_hex") or None,
        text_color_hex=data.get("text_color_hex") or None,
    )
