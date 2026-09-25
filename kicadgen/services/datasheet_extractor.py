"""Estrazione parametri footprint da datasheet tramite l'API OpenRouter.

Manda i file di documentazione (PDF/immagini) a OpenRouter -- che con una
sola chiave da' accesso ai modelli di tutti i fornitori -- e richiede di
compilare uno strumento (tool calling) con lo schema dei parametri del
pin header, cosi' la risposta e' sempre JSON strutturato valido, mai
testo libero da fare il parsing a mano.

Il modello si sceglie dalla GUI (id OpenRouter, es.
"anthropic/claude-sonnet-5"): l'impostazione della GUI sta nei profili SQLite,
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
from ..core.package_geometry import package_from_mapping
from ..generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_STYLE_NAMES, CONTACT_STYLES, EDGE_LABELS, EDGES,
)
from .openrouter import (DEFAULT_MODEL, OpenRouterError, api_key, call_tool, check_credit,
                         reset_usage, usage_totals)
from .messages import tx

MAX_FILES = 5

#: Tipi di package che il modello 3D sa disegnare (cad3d/freecad_parts.py:
#: BUILDERS). Duplicato qui perche' quel modulo gira solo dentro FreeCAD.
COMPONENT_KINDS = ('generic', 'ic', 'ic_soic', 'ic_qfn', 'transformer', 'module', 'led', 'capacitor_tantalum', 'capacitor', 'resistor', 'inductor', 'diode', 'diode_melf', 'connector', 'switch', 'crystal')
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
            "package_geometry": {
                "type": "object",
                "description": (
                    "Geometria generica opzionale per IC e package non rappresentabili "
                    "come matrice. Usala per SOIC, TSSOP, QFN, QFP, DIP irregolari e "
                    "package custom: riporta un pad per pin con coordinate locali "
                    "viste dall'alto o indica source_view=bottom. Non inventare "
                    "coordinate non supportate dal documento."
                ),
                "properties": {
                    "family": {"type": "string", "enum": [
                        "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp",
                        "module", "castellated", "custom"]},
                    "body_length_mm": {"type": "number", "exclusiveMinimum": 0},
                    "body_width_mm": {"type": "number", "exclusiveMinimum": 0},
                    "body_height_mm": {"type": "number", "exclusiveMinimum": 0},
                    "origin": {"type": "string", "enum": ["body_center", "pin1", "custom"]},
                    "source_view": {"type": "string", "enum": ["top", "bottom"]},
                    "orientation_deg": {"type": "number"},
                    "mirrored": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "needs_review": {"type": "boolean"},
                    "pins": {"type": "array", "items": {"type": "object", "properties": {
                        "number": {"type": "string"}, "name": {"type": "string"},
                        "x_mm": {"type": "number"}, "y_mm": {"type": "number"},
                        "pad_length_mm": {"type": "number", "exclusiveMinimum": 0},
                        "pad_width_mm": {"type": "number", "exclusiveMinimum": 0},
                        "side": {"type": "string", "enum": ["left", "right", "top", "bottom", "center"]},
                        "pad_type": {"type": "string", "enum": ["smd", "thru_hole", "np_thru_hole", "connect"]},
                        "shape": {"type": "string", "enum": ["circle", "rect", "oval", "roundrect", "trapezoid", "custom"]},
                        "rotation_deg": {"type": "number"}, "drill_mm": {"type": "number", "exclusiveMinimum": 0},
                    }, "required": ["number", "x_mm", "y_mm", "pad_length_mm", "pad_width_mm"]}},
                },
                "required": ["family", "source_view", "pins"],
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
                    "contro un pitch di 2.54mm) -- non confonderla col pitch. "
                    "Anche con layout=\"castellated\", se i contatti stanno su due lati "
                    "opposti: distanza fra i CENTRI dei contatti delle due file, se quotata "
                    "o leggibile sulla griglia; ometti se non la conosci."
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
            "pcb_thickness_mm": {
                "type": "number", "exclusiveMinimum": 0,
                "description": "Spessore del solo PCB, esclusi componenti e pin. Ometti se non quotato; non confonderlo con l'altezza totale.",
            },
            "body_height_mm": {
                "type": "number",
                "description": (
                    "Spessore/altezza del corpo lungo l'asse Z (perpendicolare al PCB, "
                    "disegno meccanico quotato o misura diretta) -- e' una quota "
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
                    "dal bordo sinistro, body_offset_x_mm = -3.0). Leggilo sullo STESSO "
                    "disegno da cui leggi pin_map, cosi' come lo vedi: se e' una vista "
                    "dal basso lo ribalta il programma insieme alle colonne."
                ),
            },
            "body_offset_y_mm": {
                "type": "number",
                "description": (
                    "Posizione del bordo del corpo lungo Y rispetto alla PRIMA riga di "
                    "pin (y=0), stessa logica di body_offset_x_mm ma per l'asse "
                    "perpendicolare: valore negativo pari alla distanza fra il bordo del "
                    "corpo dal lato della prima riga e la prima riga stessa. Si misura "
                    "dalla prima riga, NON dal centro del corpo: con piu' righe centrate "
                    "nel corpo vale -(body_width_mm - distanza fra prima e ultima riga)/2 "
                    "(es. due file distanti 22.86mm in un corpo largo 25.4mm: -1.27, non "
                    "-12.7). Meta' della larghezza col segno meno e' giusta solo con UNA "
                    "sola riga centrata. Tutti i centri dei pin devono cadere dentro il "
                    "corpo."
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
                            "description": (
                                "Nome dello spigolo, fra due facce del parallelepipedo. "
                                "Convenzione, vista dall'alto con l'asse lungo "
                                "orizzontale: left/right = lati sinistro/destro, front = "
                                "lato in ALTO della vista, back = lato in basso, top = "
                                "faccia superiore, bottom = faccia di appoggio. Gli angoli "
                                "arrotondati del contorno VISTO DALL'ALTO (es. scheda con "
                                "angoli tondi) sono gli spigoli verticali front-left, "
                                "front-right, back-left, back-right. top-*/bottom-* sono "
                                "invece gli spigoli orizzontali della faccia superiore o "
                                "inferiore (bordo arrotondato visto di profilo): NON "
                                "usarli per gli angoli visti dall'alto. Su un corpo sottile "
                                "(es. una PCB) i raccordi di due spigoli opposti della "
                                "stessa faccia devono sommare meno del suo spessore."
                            ),
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
                                "a destra (lati orizzontali). Deve essere il minimo della "
                                "sequenza anche con reverse=true. I numeri fisici devono "
                                "essere univoci tra TUTTI i lati: non ricominciare da 1 "
                                "su ogni lato e non usare i numeri GPIO come numeri pad."
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
                        "contact_style": {
                            "type": "string",
                            "enum": list(CONTACT_STYLES),
                            "description": (
                                "Che feature metallizzata porta ogni contatto di questo "
                                "lato, GUARDANDO LA FOTO del bordo (non dedurlo dalle "
                                "quote): \"half_hole\" se il foro dorato e' tagliato dal "
                                "bordo della scheda (mezzaluna aperta verso l'esterno); "
                                "\"through_hole\" se il foro e' un cerchio intero chiuso, "
                                "con una corona dorata completa e scheda intatta fra la "
                                "corona e il bordo, che resta dritto; "
                                "\"half_and_through\" se sullo stesso contatto ci sono "
                                "entrambi, la mezzaluna sul bordo e un foro intero poco "
                                "piu' dentro. Ometti se la foto non mostra il bordo."
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
                    "Per schede popolate, sia THT (matrix) sia castellated, solo se tra i "
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
                            "enum": ['generic', 'ic', 'ic_soic', 'ic_qfn', 'transformer', 'module', 'led', 'capacitor_tantalum', 'capacitor', 'resistor', 'inductor', 'diode', 'diode_melf', 'connector', 'switch', 'crystal'],
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
                                "foto (es. \"SP3485\", \"VPT\", \"102\"): serve a cercare il "
                                "modello di libreria per marcatura, non viene stampata sul "
                                "solido. Non inventare codici."
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
    "Per ogni lato castellated indica anche contact_style guardando il bordo nella "
    "foto: half_hole se il foro metallizzato e' tagliato dal bordo (mezzaluna aperta "
    "verso l'esterno), through_hole se il foro e' intero, con corona chiusa e scheda "
    "intatta fra la corona e il bordo, half_and_through se lo stesso contatto ha "
    "entrambi; ometti il campo se il bordo non si vede. "
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
    pcb_thickness_mm: Optional[float] = None
    body_offset_x_mm: Optional[float] = None
    body_offset_y_mm: Optional[float] = None
    pin_length_3d_mm: Optional[float] = None
    edge_fillets: Optional[Sequence[dict]] = None
    pinout: Optional[Sequence[dict]] = None
    pin_map: Optional[Sequence[dict]] = None
    body_color_hex: Optional[str] = None
    text_color_hex: Optional[str] = None
    package_geometry: Optional[dict] = None

    def has_3d_body(self) -> bool:
        """@brief Provide the has 3d body operation for this workflow."""
        return self.body_length_mm is not None and self.body_width_mm is not None and self.body_height_mm is not None

    def is_castellated(self) -> bool:
        """@brief Provide the is castellated operation for this workflow."""
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
            tx("Formato non supportato: {name} (ammessi: {join})",
               name=path.name,
               join=', '.join(sorted(_SUPPORTED_SUFFIXES)))
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


def _offset_x_seen_from_top(offset, length, cols, pitch, view):
    """Body X offset of a BOTTOM VIEW drawing, taken to the view from above.

    `_pin_map_seen_from_top` mirrors the columns of a bottom view; the body
    must be mirrored with them, or its left and right margins swap and the
    whole body shifts by their difference (audit 25/09, e.g. 3.0 and 5.05 mm
    margins -> 2.05 mm). In the drawing the body spans [offset, offset +
    length] over pins [0, span]; mirrored, its left margin is the old right
    one: length - span + offset. Only when the columns ARE mirrored (a
    pin_map read on a bottom view); otherwise the offset is returned as is.
    """
    if view != "bottom" or offset is None or length is None or not pitch:
        return offset
    try:
        cols = int(cols)
    except (TypeError, ValueError):
        return offset
    if cols < 1:
        return offset
    span = (cols - 1) * pitch
    return round(-(length - span + offset), 3)


def _merge_photo_components(groups):
    """Compatibilita' per chiamanti che usano l'inventario in millimetri."""
    from .photo_inventory import merge_components
    return merge_components([_components_3d(g) or [] for g in groups])


#: Massimo numero di caratteri con cui un nome file viene mostrato nella riga
#: di avanzamento. E' un ripiego di PRESENTAZIONE, non una regola sui
#: documenti: non dipende da estensione, contenuto o marca.
#:
#: La modale ora mette ogni documento su una riga propria
#: (`analysis_overlay.enumeration_lines`), quindi un nome lungo non spinge piu'
#: fuori i nomi successivi: accorcia soltanto SE STESSO. Il limite resta perche'
#: una riga della scheda porta circa ottantotto caratteri (e' la larghezza su
#: cui la scheda e' dimensionata), e una voce e' «nome — ruolo»: con i quaranta
#: caratteri di qui piu' il separatore e l'etichetta di ruolo piu' lunga fra quelle
#: dichiarate in `document_roles.ROLES` (trenta caratteri) si resta sotto quella
#: larghezza, cioe' il ruolo si legge sempre. Oltre il limite la scheda
#: accorcerebbe comunque la riga, ma tagliando la CODA, quindi il ruolo: qui si
#: accorcia il nome nel mezzo, e l'estensione sopravvive.
_LOG_NAME_CHARS = 40


def _log_name(name: str) -> str:
    """@brief Shorten one file name so a multi-file progress line stays readable.

    @param name File name as the user sees it, without any directory part.
    @return The name unchanged when short enough, otherwise its head and tail
        joined by an ellipsis, so both the prefix and the extension stay
        visible and the document remains recognizable.
    @note Presentation only: the name sent to the model is never shortened.
    """
    if len(name) <= _LOG_NAME_CHARS:
        return name
    tail = _LOG_NAME_CHARS // 3
    return name[:_LOG_NAME_CHARS - 1 - tail] + '…' + name[-tail:]


def _files_sent_message(doc_paths, document_roles: Optional[dict[str, str]] = None) -> str:
    """@brief Name the documents about to travel in the paid analysis request.

    @param doc_paths Documents in request order; only their file names are
        reported, because the directories say nothing about what is being sent
        and would take the room the names and the roles need.
    @param document_roles Optional file name -> role mapping declared by the
        user. A name missing from it, or a role that is not a known one, is
        reported as undeclared: the role is never guessed from the file name.
    @return One readable line listing every document and, where known, the
        role it is sent as.
    @note The count and the noun agree for one document as well as for many,
        so the line never reads as a plural applied to a single file.
    @note Punctuation is part of the contract, not taste, and now for two
        readers at once.

        The modal hides the lines a machine writes for itself, recognizing them
        by an UPPERCASE_UNDERSCORE token in FIRST position (`analysis_overlay.
        _MACHINE_MARKER`). A file name is user data and is never rewritten, so
        the line is built so that no name ever opens it: the lead-in does. That
        is what keeps a document called «MOD_REV_B» from hiding the one line
        that says what is being paid for; the old requirement that no bare
        space follow a name dated from an unanchored marker rule and no longer
        applies. A name that carries the separators below inside itself is
        still beyond this line's reach.

        Three signs are read by the card (`analysis_overlay`), while the line
        written to the log stays ONE line with one timestamp, as every other
        log entry:

        - the colon closes the lead-in and every middle dot starts a new line
          (`enumeration_lines`): the documents are listed one per line;
        - " — " (space, em dash, space) separates each file name from its role
          (`enumeration_term`): the card shows the name in bold, set apart from
          the role, as the user asked («i nomi dei file non sono evidenziati,
          troppo attaccati al testo»). It replaced the comma, which the card
          could not tell apart from a comma inside a file name without guessing
          what a file name looks like.

        Replacing the middle dot with a semicolon, dropping the colon, or going
        back to the comma would silently take the list or the highlighting away
        without breaking anything else -- hence this note. The role labels
        never contain " — ", so the first occurrence in an item always ends the
        name unless the name itself contains the sign.
    """
    from .document_roles import ROLES
    if not doc_paths:
        # Invariante difensiva dell'helper: la riga descrive un invio, e senza
        # documenti non c'e' invio da descrivere. Un «Invio a AI 0 file»
        # farebbe credere a una chiamata partita a vuoto, quindi pagata. Il
        # chiamante dell'estrazione rifiuta prima l'elenco vuoto
        # («Nessun file selezionato.»), percio' qui non e' un caso atteso in
        # produzione: e' il comportamento definito dell'helper per chi lo
        # riusa, cosi' il ramo resta esercitabile e non produce una frase
        # sbagliata se un secondo chiamante arriva.
        return tx("Nessun documento da inviare a AI: layout, quote e pinout non sono "
                  "estraibili.")
    described = []
    for path in doc_paths:
        name = Path(path).name
        label = ROLES.get((document_roles or {}).get(name))
        described.append(f'{_log_name(name)} — {tx(label)}' if label
                         else tx("{log_name} — ruolo non dichiarato", log_name=_log_name(name)))
    count = len(described)
    # Singolare e plurale sono due frasi intere: ogni lingua li accorda a modo
    # suo, un suffisso «o»/«i» incollato vale solo per l'italiano.
    lead = (tx("Invio a AI 1 documento per layout, quote e pinout: ") if count == 1 else
            tx("Invio a AI {count} documenti per layout, quote e pinout: ", count=count))
    return lead + ' · '.join(described) + '.'


def extract_pin_header_params(
    doc_paths: list[Path],
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    include_nc_pins: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    vision_model: Optional[str] = None,
    photo_cache_dir: Optional[Path] = None,
    grounding_prompt: Optional[str] = None,
    verification_prompt: Optional[str] = None,
    verification_model: Optional[str] = None,
    terminal_prompt: Optional[str] = None,
    document_roles: Optional[dict[str, str]] = None,
    component_family: Optional[str] = None,
    photo_detector: str = "ai",
    yolo_model_path: Optional[Path] = None,
    phase: Optional[Callable[..., None]] = None,
    review_proposals: Optional[Callable[..., object]] = None,
) -> ExtractedParams:
    """@brief Extract dimensions and pinout, optionally using local YOLO photo proposals.

    @param photo_detector Photo localization backend: ai (legacy), yolo (local
        proposals plus one AI reconciliation) or vlm (no local detector: the
        vision model enumerates the bodies in that same single request).
    @param yolo_model_path Optional compatible ONNX file; validated before paid calls.
    @param phase Optional callable(tool, detail) telling the caller WHICH tool
        is working, so a progress modal can distinguish a paid remote call
        ('llm') from local detection ('yolo'). It is forwarded to the photo
        inventory, which announces the local and the paid steps as they start.
        None disables the notification; a failing callback never aborts the
        extraction, exactly like `progress`.
    @note YOLO localization is local; datasheet, package and terminal review still use AI.

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
        raise ExtractionError(tx("Nessun file selezionato."))
    if photo_detector not in ("ai", "yolo", "vlm"):
        raise ExtractionError(tx("Rilevatore fotografico non supportato."))
    if photo_detector == "yolo":
        from .photo_yolo import validate_yolo_setup
        try:
            validate_yolo_setup(yolo_model_path)
        except (ValueError, OSError, ImportError) as exc:
            raise ExtractionError(tx("YOLO locale non disponibile: {exc}", exc=exc)) from exc
    from .ai_diagnostics import make_logged_call
    # L'analisi applicativa deve essere auditabile: il log persistente conserva
    # il testo esatto dei prompt/tool e gli hash delle immagini (mai i byte).
    diagnostic_call = make_logged_call(call_tool, progress, log_prompt=True)
    if len(doc_paths) > MAX_FILES:
        raise ExtractionError(tx("Troppi file selezionati (max {MAX_FILES} per chiamata).", MAX_FILES=MAX_FILES))
    from .document_roles import ROLES, ROLE_HELP
    if document_roles is not None:
        if (set(document_roles) != {p.name for p in doc_paths}
                or len(set(document_roles.values())) != len(doc_paths)
                or any(role not in ROLES for role in document_roles.values())):
            raise ExtractionError(tx("Ruoli documenti incompleti, duplicati o non validi."))

    # Il consuntivo riguarda questa elaborazione: azzerarlo qui, non alla fine,
    # cosi' un'estrazione interrotta non lascia residui nella successiva.
    reset_usage()
    try:
        api_key()
        # Il credito va verificato qui, prima del primo blocco di contenuto:
        # l'estrazione e' una catena di decine di chiamate (analisi principale,
        # crop, verifica, terminali) e restare a secco a meta' strada significa
        # aver speso senza ottenere un componente.
        check_credit(progress=progress)
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    def announce(tool, detail):
        """Segnalare la fase e' informativo: una modale rotta non deve far
        perdere un'elaborazione che si paga, quindi l'errore si registra e si
        prosegue -- stesso trattamento riservato a `progress`."""
        if phase is None:
            return
        try:
            phase(tool, detail)
        except Exception as exc:  # noqa: BLE001 - la GUI non deve poter abortire l'analisi
            if progress:
                progress(tx("Segnalazione della fase non riuscita ({exc}); elaborazione proseguita.",
                            exc=exc))

    # Quale strumento sta lavorando non si deduce dai messaggi di
    # avanzamento, e la differenza pesa: questa e' una chiamata remota che
    # si paga, il rilevamento locale no.
    announce("llm", model or DEFAULT_MODEL)
    if progress:
        # L'utente deve poter verificare QUALI dei suoi documenti sono partiti
        # nella chiamata a pagamento, e con quale ruolo dichiarato.
        progress(_files_sent_message(doc_paths, document_roles))
    content_blocks = []
    for index, path in enumerate(doc_paths):
        description = f"FILE {index}: {path.name}"
        if document_roles is not None:
            role = document_roles[path.name]
            description += f'\nRUOLO DICHIARATO DALL’UTENTE: {ROLES[role]}. {ROLE_HELP[role]}'
        content_blocks.append({"type": "text", "text": description})
        content_blocks.append(_file_to_content_block(path))
    request = "Estrai i parametri del pin header da questi documenti e chiama lo strumento."
    if document_roles is not None:
        missing = [label for role, label in ROLES.items() if role not in document_roles.values()]
        request += ('\nI ruoli dei file sono espliciti: rispettali, non dedurli dai nomi originali. '
                    'Non usare il pinout come vista dall’alto per gli ingombri. '
                    'Segnala dati mancanti e incongruenze, senza inventare quote per completare le viste assenti.')
        if missing:
            request += '\nViste non fornite: ' + ', '.join(missing) + '.'
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
        data = diagnostic_call(
            model=model or DEFAULT_MODEL,
            system_prompt=system_prompt or _SYSTEM_PROMPT,
            content_blocks=content_blocks,
            tool=_openai_tool(),
            max_tokens=16384,
            has_pdf=has_pdf,
        )
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    if not isinstance(data, dict):
        raise ExtractionError(tx("Risposta AI non strutturata."))
    # Validate geometry before it can be used to calibrate a photograph.
    for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
        original = data.get(key)
        data[key] = _optional_number(original, positive=True)
        if original is not None and data[key] is None:
            warning = tx("{key} scartato: dimensione non valida (serve un numero positivo "
                         "finito, non booleano).",
                         key=key)
            data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
            if progress:
                progress(warning)
    if data.get("layout") == "castellated":
        try:
            _castellated_sides(data.get("castellated_sides"))
        except _PinNumberConflict as exc:
            if progress:
                progress(tx("Numerazione contatti incoerente: {exc}. Richiesta correzione prima "
                            "dell'analisi foto.",
                            exc=exc))
            repair_blocks = list(content_blocks) + [{"type": "text", "text": (
                "Correggi esclusivamente la numerazione fisica dei contatti della risposta precedente. "
                "Confronta il pinout originale: GPIO e sigle elettriche sono pin_names, non numeri pad. "
                "first_pin e' il minimo della sequenza; reverse ne inverte l'ordine spaziale. "
                "Non cambiare lati, conteggi, geometria o ordine delle sigle. "
                "Se i documenti non stabiliscono la numerazione, ometti first_pin su TUTTI i lati "
                "per adottare una convenzione progressiva esplicitamente da verificare. "
                f"Conflitto: {exc}. Lati precedenti: {data.get('castellated_sides')!r}"
            )}]
            try:
                repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                    system_prompt=system_prompt or _SYSTEM_PROMPT,
                    content_blocks=repair_blocks, tool=_openai_tool(),
                    max_tokens=16384, has_pdf=has_pdf)
                corrected = _castellated_sides(repaired.get("castellated_sides"))
                original = data["castellated_sides"]
                if len(corrected) != len(original) or any(
                    row["edge"] != old.get("edge") or row["count"] != old.get("count")
                    for row, old in zip(corrected, original)
                ):
                    raise ExtractionError(tx("La correzione della numerazione ha cambiato i lati o i conteggi."))
                data["castellated_sides"] = [dict(old, first_pin=row["first_pin"],
                    reverse=row.get("reverse", False)) for old, row in zip(original, corrected)]
                warning = tx("Numerazione contatti corretta dopo conflitto AI; verificare la "
                             "tabella pinout prima dell'utilizzo.")
                data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
                if progress:
                    progress(warning)
            except (OpenRouterError, AttributeError) as repair_error:
                raise ExtractionError(tx("Correzione numerazione non riuscita; analisi foto non avviata.")) from repair_error
        # Dopo l'eventuale correzione, cosi' la riga descrive i lati definitivi.
        notice = _unobserved_contact_style_notice(_castellated_sides(data.get("castellated_sides")))
        if notice:
            data["notes"] = (str(data.get("notes") or "") + "\n" + notice).strip()
            if progress:
                progress(notice)
    _params_from_tool_arguments(data)

    if progress:
        progress(
            tx("Analisi principale completata: layout={get}, componenti "
               "iniziali={n_items}.",
               get=data.get('layout', 'matrix'),
               n_items=len(data.get('components_3d') or []))
        )

    # La foto richiede un passaggio separato: nell'estrazione globale il modello
    # deve anche leggere quote, pinout e layout, e tende a sacrificare i passivi
    # piccoli. I crop sono best-effort: se falliscono, resta il risultato globale.
    photo_report = None
    top_index = data.get('top_photo_index')
    if document_roles is not None:
        # User-assigned roles are authoritative. Never calibrate the wrong view.
        # Tra le viste dall'alto vince la piu' dettagliata: a parita' di
        # algoritmo e' la risoluzione a decidere quanti passivi piccoli si
        # riescono a distinguere. Se manca, si ricade sulla vista superiore.
        # Si passano TUTTE le viste dall'alto assegnate: l'inventario sceglie
        # la piu' dettagliata fra quelle che riesce a calibrare. Indicare solo
        # la foto ad alta risoluzione lascerebbe senza inventario quando e'
        # un primo piano parziale, invece di ricadere sulla vista superiore.
        from .document_roles import INVENTORY_ROLES
        by_role = {role: i for i, path in enumerate(doc_paths)
                   if (role := document_roles[path.name]) in INVENTORY_ROLES}
        top_index = [by_role[r] for r in INVENTORY_ROLES if r in by_role] or [-1]
        if progress:
            chosen = ', '.join(f'{doc_paths[by_role[r]].name} ({tx(ROLES[r])})'
                               for r in INVENTORY_ROLES if r in by_role)
            progress(tx('Viste dall’alto disponibili: ') + (
                chosen + tx(". Verrà usata la più dettagliata fra quelle calibrabili.")
                if chosen else tx("nessuna; nessun’altra vista verrà usata al suo posto.")))
    populated_board = data.get("layout") == "castellated" or component_family == "aggregate_tht"
    if (populated_board and data.get("body_length_mm")
            and data.get("body_width_mm")):
        from .photo_inventory import ReviewCancelled, extract_inventory
        from .photo_grounding import DEFAULT_VISION_MODEL
        # La fase del rilevatore locale NON si annuncia qui: il contratto di
        # `extract_inventory` (vedi photo_inventory.extract_inventory, @param
        # phase) e' che 'yolo' arrivi immediatamente prima del rilevatore, e
        # qui siamo prima della calibrazione. Annunciarlo adesso mostrava una
        # fase che poteva non partire mai -- senza vista calibrabile
        # l'inventario esce prima di caricare l'ONNX -- e la modale restava sul
        # rilevatore locale per il resto della corsa. Con 'ai' e 'vlm' invece
        # il prossimo passo e' davvero la chiamata a pagamento al modello di
        # visione, e va detto perche' e' cio' che si paga.
        if photo_detector != "yolo":
            announce("llm", vision_model or DEFAULT_VISION_MODEL)
        try:
            photo_report = extract_inventory(
                doc_paths, vision_model or DEFAULT_VISION_MODEL, data["body_length_mm"],
                data["body_width_mm"], COMPONENT_KINDS, diagnostic_call,
                progress=progress, phase=announce, top_photo_index=top_index,
                **({'top_photo_source': 'utente'} if document_roles is not None else {}),
                extra_instructions=extra_instructions,
                cache_dir=photo_cache_dir,
                classifier_model=verification_model or model or DEFAULT_MODEL,
                grounding_prompt=grounding_prompt, verification_prompt=verification_prompt,
                **({'detector': photo_detector, 'yolo_model_path': yolo_model_path,
                    'review_proposals': review_proposals, 'declared': data}
                   if photo_detector in ('yolo', 'vlm') else {}),
            )
        except ReviewCancelled as exc:
            # Un annullo non e' un guasto dell'inventario: proseguire qui
            # vorrebbe dire pagare la riconciliazione sulle proposte rifiutate.
            raise ExtractionError(str(exc)) from exc
        except (OpenRouterError, ValueError, OSError) as exc:
            # L'errore viene inghiottito nelle note e l'elaborazione prosegue
            # senza inventario: bisogna chiudere la fase annunciata, altrimenti
            # la modale resta per tutto il resto della corsa sull'ultimo
            # strumento annunciato dall'inventario (il rilevatore locale, se il
            # file ONNX e' sparito fra la validazione e il rilevamento) mentre
            # quello strumento non sta piu' lavorando. Tool vuoto = nessuna
            # fase rivendicata, non una fase sbagliata.
            announce("", "")
            data["notes"] = (str(data.get("notes") or "") + f"\nInventario fotografico fallito: {exc}").strip()
        if photo_report and photo_report.components:
            # The global YOLO reconciliation already reports terminal evidence.
            # Further paid crop calls are an explicit optional UI action only.
            if photo_detector not in ('yolo', 'vlm') and isinstance(getattr(photo_report, "photo_png", None), bytes):
                from .photo_terminals import observe_terminals
                try:
                    photo_report.components = observe_terminals(
                        photo_report.photo_png, photo_report.components, vision_model or DEFAULT_VISION_MODEL,
                        diagnostic_call, progress or (lambda message: None),
                        photo_cache_dir,
                        **({'system_prompt': terminal_prompt} if terminal_prompt else {}),
                    )
                except (OpenRouterError, ValueError, OSError) as exc:
                    photo_report.warnings.append(tx("Dettaglio terminali non disponibile: {exc}", exc=exc))
            data["components_3d"] = photo_report.components
            old_notes = str(data.get("notes") or "").strip()
            detailed = sum(bool(c.get("terminal_geometry")) for c in photo_report.components)
            suffix = (tx("Inventario fotografico: {n_components} ingombri su {photo}; "
                         "{detailed} con terminali osservati. Modello meccanico semplificato, "
                         "senza marcature. XY stimati dalla foto calibrata; altezze indicative, "
                         "non misure certificate: verificare con una misura diretta.",
                         n_components=len(photo_report.components),
                         photo=photo_report.photo,
                         detailed=detailed))
            data["notes"] = f"{old_notes} {suffix}".strip()
        if photo_report and photo_report.warnings:
            details = "\n".join(photo_report.warnings[:6])
            if len(photo_report.warnings) > 6:
                details += tx("\nAltri {value} avvisi nel log AI.", value=len(photo_report.warnings) - 6)
            data["notes"] = (str(data.get("notes") or "") + "\n" + details).strip()
    elif progress and populated_board:
        progress(tx("Secondo passaggio fotografico non avviato: mancano "
                    "body_length/body_width."))

    params = _params_from_tool_arguments(data)
    if populated_board and not (photo_report and photo_report.components):
        for index, component in enumerate(params.components_3d or [], 1):
            component.update(kind="generic", name=f"Ingombro {index}", marking="",
                             confidence="low", _uncertain=True,
                             _height_source="global_visual_estimate_not_measured")
        if params.components_3d:
            params.notes = (str(params.notes or "") + tx("\nIngombri dall'estrazione globale, senza verifica fotografica e senza "
                                                         "marcature; altezze indicative, non misure certificate.")).strip()
    if photo_report and photo_report.components:
        params.photo_overlay_png = photo_report.overlay_png
        params.photo_png = photo_report.photo_png
        params.photo_source = photo_report.photo
    for message in fit_body_to_pin_field(params, component_family):
        if progress:
            progress(message)
    if progress:
        progress(usage_totals().describe())
    return params


#: Tolleranza, in mm, con cui un centro di pin sul bordo conta come dentro:
#: solo arrotondamento numerico, non un margine fisico.
_PIN_FIELD_TOLERANCE_MM = 1e-6


def _pin_field_positions(params) -> list:
    """Positions (x, y) in mm of the pins that exist, as the footprint draws them.

    Same function as the footprint and the 3D pins (`pin_header.pin_positions`),
    so populated_pins and an explicit pin_map count exactly as they will be
    drawn. Empty when the grid is not usable.
    """
    from ..generators.pin_header import PinHeaderParams, pin_positions
    mapping = None
    if params.pin_map:
        mapping = {int(entry["pin"]): (int(entry["row"]), int(entry["col"]))
                   for entry in params.pin_map}
    grid = PinHeaderParams(
        rows=int(params.rows), cols=int(params.cols), pitch=float(params.pitch_mm),
        row_spacing=params.row_spacing_mm or None,
        populated_pins=set(params.populated_pins) if params.populated_pins else None,
        pin_map=mapping)
    try:
        return [(x, y) for _, x, y in pin_positions(grid)]
    except (TypeError, ValueError, KeyError):
        return []


def fit_body_to_pin_field(params, component_family) -> list[str]:
    """@brief Tiene tutti i pin di una scheda popolata a fori passanti dentro la scheda.

    @param params ExtractedParams di layout «matrix»; corretto sul posto.
    @param component_family Famiglia della tipologia scelta (attributo
        dichiarato, non il nome del pezzo).
    @return I messaggi da mostrare nel log: vuoto quando non c'era nulla da dire.
    @note Solo per `aggregate_tht`: su una scheda popolata a fori passanti i
        pin passano nei fori DELLA scheda, quindi ogni centro di pin sta dentro
        il suo contorno. E' un vincolo fisico, non una stima. Un componente
        singolo THT puo' invece avere gambe fuori dal corpo, e non si tocca.
    @note Caso visto dall'utente il 2026-09-24 (TEST4): l'AI ha dato
        `body_offset_y_mm = -12.7`, meta' larghezza, misurando dal centro della
        scheda invece che dalla prima fila, e la seconda fila e' finita fuori.
        Quando il campo dei pin esce dalla scheda ma ci sta, lo spostamento di
        QUELL'asse viene ricentrato: e' una STIMA (il bordo non era quotato in
        modo coerente) e lo si scrive nelle note e nel log. Quando la scheda e'
        piu' piccola del campo dei pin nessun valore e' giusto: nessuna
        correzione, solo l'avviso di quote incoerenti.
    """
    if component_family != "aggregate_tht" or params.is_castellated():
        return []
    rows, cols, pitch = params.rows, params.cols, params.pitch_mm
    length, width = params.body_length_mm, params.body_width_mm
    if not all(isinstance(v, (int, float)) and v > 0 for v in (rows, cols, pitch, length, width)):
        return []
    positions = _pin_field_positions(params)
    if not positions:
        return []
    messages = []
    for axis, index, extent, key in (("X", 0, length, "body_offset_x_mm"),
                                     ("Y", 1, width, "body_offset_y_mm")):
        offset = getattr(params, key)
        if offset is None:
            continue
        # Campo dei pin PRESENTI (populated_pins, pin_map), non della matrice
        # dichiarata: una colonna vuota in fondo non e' un pin fuori scheda
        # (audit 25/09: un offset giusto veniva «corretto»). I pin occupano
        # [low, high] sull'asse; la scheda [offset, offset + extent].
        low = min(point[index] for point in positions)
        high = max(point[index] for point in positions)
        span = high - low
        if (offset <= low + _PIN_FIELD_TOLERANCE_MM
                and offset + extent >= high - _PIN_FIELD_TOLERANCE_MM):
            continue
        if extent + _PIN_FIELD_TOLERANCE_MM < span:
            messages.append(tx(
                "ATTENZIONE quote incoerenti sull'asse {axis}: i pin occupano {span:.2f} mm "
                "ma la scheda misura {extent:.2f} mm. Nessuna correzione automatica: "
                "verificare le quote.", axis=axis, span=span, extent=extent))
            continue
        centred = round(low - (extent - span) / 2, 3)
        setattr(params, key, centred)
        messages.append(tx(
            "ATTENZIONE spostamento del corpo sull'asse {axis} corretto da {old:.2f} a "
            "{new:.2f} mm: con il valore dell'analisi una fila di pin cadeva fuori dalla "
            "scheda. Valore STIMATO (campo dei pin centrato sulla scheda): verificarlo "
            "con una quota.", axis=axis, old=offset, new=centred))
    if messages:
        params.notes = (str(params.notes or "") + "\n" + "\n".join(messages)).strip()
    return messages


def _optional_number(value, *, positive=False, integer=False):
    """Discard malformed optional numerics; signed offsets remain valid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or (positive and number <= 0):
            return None
        if integer and number != int(number):
            return None
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return None


class _PinNumberConflict(ExtractionError):
    """@brief Reject overlapping physical pad ranges before photographic analysis."""


def _castellated_sides(raw: Optional[Sequence[dict]]) -> list[dict]:
    """Normalizza i lati di un modulo castellated, scartando quelli inutilizzabili.

    Il modello puo' restituire un lato senza passo o con zero contatti: si
    preferisce ignorarlo e dirlo, piuttosto che propagare un lato che
    farebbe fallire la generazione con un errore geometrico oscuro.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ExtractionError(
            tx("Il modello ha indicato un modulo con piazzole a mezzo foro ma non ha "
               "descritto alcun lato. Riprova, eventualmente con un modello piu' "
               "capace.")
        )

    sides = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        edge = str(entry.get("edge") or "").strip().lower()
        count = _optional_number(entry.get("count"), positive=True, integer=True)
        pitch = _optional_number(entry.get("pitch_mm", 0))
        if edge not in EDGES or count is None or pitch is None or pitch < 0 or (count > 1 and pitch == 0):
            continue
        side = {"edge": edge, "count": count, "pitch_mm": pitch}
        for key in ("first_pin", "offset_mm", "pad_length_mm", "pad_width_mm"):
            value = _optional_number(entry.get(key), positive=key != "offset_mm", integer=key == "first_pin")
            if value is not None:
                side[key] = value
        if entry.get("reverse") is True:
            side["reverse"] = True
        if isinstance(entry.get("pin_names"), (list, tuple)) and entry["pin_names"]:
            side["pin_names"] = [str(n) for n in entry["pin_names"]]
        # Il tipo di contatto si vede nella foto e non si ricava dalle quote:
        # un valore fuori elenco non si corregge ne' si scarta in silenzio,
        # perche' qualunque sostituzione sarebbe proprio la deduzione che il
        # campo esiste per evitare. Assente resta assente: il default lo
        # applica chi costruisce il modulo, e il log lo dice (vedi
        # _unobserved_contact_style_notice).
        if "contact_style" in entry and entry["contact_style"] is not None:
            style = entry["contact_style"]
            if style not in CONTACT_STYLES:
                raise ExtractionError(
                    tx("Lato {get}: tipo di contatto {style!r} non riconosciuto (ammessi: "
                       "{join}). La risposta non viene adattata: ripeti l'estrazione o scegli "
                       "il tipo a mano.",
                       get=EDGE_LABELS.get(edge, edge),
                       style=style,
                       join=', '.join(CONTACT_STYLES))
                )
            side["contact_style"] = style
        sides.append(side)

    if not sides:
        raise ExtractionError(
            tx("Nessun lato valido tra quelli restituiti dal modello (servono lato, "
               "numero di contatti e passo).")
        )

    # Numerazione progressiva sui lati privi di first_pin, nell'ordine in cui
    # sono stati elencati: meglio una numerazione coerente da correggere a
    # mano che dei pad senza numero.
    used = set()
    for side in sides:
        if "first_pin" in side:
            numbers = set(range(side["first_pin"], side["first_pin"] + side["count"]))
            overlap = used & numbers
            if overlap:
                raise _PinNumberConflict(tx("Numero pad duplicato tra lati: {min}.", min=min(overlap)))
            used.update(numbers)
    next_pin = 1
    for side in sides:
        if "first_pin" not in side:
            while used.intersection(range(next_pin, next_pin + side["count"])):
                next_pin += 1
            side["first_pin"] = next_pin
            used.update(range(next_pin, next_pin + side["count"]))
        next_pin = int(side["first_pin"]) + side["count"]
    return sides


def _unobserved_contact_style_notice(sides: Sequence[dict]) -> Optional[str]:
    """@brief Say which castellated sides came back without a contact style.

    @param sides Normalized sides, as returned by _castellated_sides.
    @return One Italian log line naming those sides and the style assumed for
        them, or None when every side declares its own style.
    @note The default is applied downstream (CastellatedSide.contact_style),
        never written into the side: the GUI keeps telling an observed style
        from an assumed one. This line is what keeps the assumption from being
        silent.
    """
    missing = [EDGE_LABELS.get(side["edge"], side["edge"])
               for side in sides if "contact_style" not in side]
    if not missing:
        return None
    return (tx("Lato {join}: tipo di contatto non osservato: si assume "
               "«{CONTACT_STYLE_NAMES}» ({CONTACT_HALF_AND_THROUGH}). Verificarlo "
               "sulla foto e correggerlo nella tabella dei lati se diverso.",
               join=', '.join(missing),
               CONTACT_STYLE_NAMES=CONTACT_STYLE_NAMES[CONTACT_HALF_AND_THROUGH],
               CONTACT_HALF_AND_THROUGH=CONTACT_HALF_AND_THROUGH))


def _components_3d(raw: Optional[Sequence[dict]]) -> Optional[list[dict]]:
    """Componenti letti dalla foto, scartando quelli senza un ingombro reale.

    Sono stime visive: un blocco con una dimensione nulla o negativa non e'
    un componente mal misurato, e' rumore, e nel solido 3D darebbe errore.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
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
            if any(isinstance(entry.get(k), bool) for k in
                   ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")):
                continue
            component = {
                "name": str(entry.get("name") or "Componente"),
                "x_mm": float(entry["x_mm"]),
                "y_mm": float(entry["y_mm"]),
                "length_mm": float(entry["length_mm"]),
                "width_mm": float(entry["width_mm"]),
                "height_mm": float(entry["height_mm"]),
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        # Il modello vede anche elementi della PCB che non sono componenti
        # montati: pad, via, semi-fori e contatti del bordo. Se passano dallo
        # schema, eliminarli qui evita che finiscano nella tabella 3D come
        # scatole. I componenti reali devono avere un package sopra la scheda.
        label = component["name"].strip().lower()
        # Global component IDs are arbitrary display labels; retained detector
        # proposals are physical hypotheses, even when their class is unknown.
        # Preserve explicit PCB-feature rejection and the geometry checks below.
        physical_hypothesis = (
            entry.get("_global_inventory_status") in ("validated_hypotheses", "partial")
            or (isinstance(entry.get("_yolo_proposal_id"), str)
                and bool(entry["_yolo_proposal_id"].strip())
                and entry.get("_yolo_origin") in ("operating_threshold", "low_confidence_review")))
        if (entry.get("object_type") == "pcb_feature" or
                (not physical_hypothesis and "object_type" not in entry and re.match(
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
            marking = str(entry["marking"]).strip()
            # Normalize only whole-field AI absence/illegibility sentinels;
            # literal substrings and the source observation remain untouched.
            component["marking"] = "" if marking.casefold() in ("none", "?") else marking
        component["rotation_deg"] = (_optional_number(entry.get("rotation_deg")) or 0.0) % 360
        # Verso di innesto dichiarato dal modello guardando la foto. Senza,
        # la forma 3D dovrebbe indovinarlo dalle sole dimensioni, che per un
        # connettore non bastano. Valori diversi da questi due si ignorano.
        if entry.get("mating") in ("edge", "top", "none"):
            component["mating"] = entry["mating"]
        # Keep the complete description: the matcher extracts declared tokens,
        # which may occur after dimensions or beyond a short display label.
        if entry.get("package"):
            component["package"] = str(entry["package"]).strip()
        if entry.get("bbox_basis") in ("body", "leads", "pads", "unknown"):
            component["bbox_basis"] = entry["bbox_basis"]
        if isinstance(entry.get("package_evidence"), str):
            component["package_evidence"] = entry["package_evidence"][:500]
        for key in ("_box", "_id", "_candidate_id", "_source", "_uncertain", "_review_reason",
                    "_detected_kind", "terminal_geometry",
                    "_height_source", "_grounding_kind", "_alternative_kind", "confidence"):
            if key in entry:
                component[key] = entry[key]
        for key in ('_yolo_class', '_yolo_confidence', '_yolo_original_box', '_geometry_source',
                    'presence_confidence', 'class_confidence', '_yolo_proposal_id',
                    '_yolo_class_ambiguous', '_yolo_review_only', '_yolo_origin',
                    # Riquadro tracciato o ritoccato da una persona: e' la
                    # decisione di presenza del revisore e deve arrivare al
                    # componente finale, non fermarsi ai candidati inviati.
                    '_yolo_manual_box',
                    '_package_verified', '_orientation_verified', 'pin_count_confidence', 'pin_count_source',
                    'class', 'component_id', 'source_candidate_ids', 'terminal_count', 'terminal_layout',
                    'terminal_count_basis', 'lead_count', 'lead_count_complete', 'lead_count_confidence',
                    '_reported_rotation_deg', '_height_estimated', '_global_inventory_status',
                    '_global_inventory_errors', '_global_inventory_uncertainties',
                    '_global_inventory_prompt_version', '_global_inventory_prompt_hash',
                    # Il riferimento asimmetrico serve al costruttore 3D per
                    # stabilire il verso del modello nativo. Senza questa riga
                    # veniva scartato qui, e ogni componente arrivava al
                    # matching dichiarando "nessun riferimento osservato" anche
                    # quando il modello ne aveva visto uno ad alta confidenza.
                    'landmark_feature', 'landmark_side', 'landmark_confidence'):
            if key in entry:
                component[key] = entry[key]
        if isinstance(entry.get("pin_count"), int) and not isinstance(entry.get("pin_count"), bool) and entry["pin_count"] > 0:
            component["pin_count"] = entry["pin_count"]
        if isinstance(entry.get("pins_by_side"), dict):
            sides = {k: v for k, v in entry["pins_by_side"].items()
                     if k in {"left", "right", "top", "bottom"}
                     and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            if sides and sum(sides.values()) > 0:
                component["pins_by_side"] = sides
        components.append(component)
    return components or None


def _params_from_tool_arguments(data: dict) -> ExtractedParams:
    """Costruisce ExtractedParams dagli argomenti dello strumento.

    Separato dalla chiamata di rete per poterlo collaudare senza API.
    """
    if not isinstance(data, dict):
        raise ExtractionError(tx("Risposta AI non strutturata."))
    layout = data.get("layout") or "matrix"
    if layout not in ("matrix", "castellated"):
        raise ExtractionError(tx("Disposizione dei contatti sconosciuta: {layout!r}.", layout=layout))

    def _opt_float(key: str) -> Optional[float]:
        return _optional_number(data.get(key), positive=key in (
            "body_length_mm", "body_width_mm", "body_height_mm", "pin_length_3d_mm"))

    if layout == "castellated":
        sides = _castellated_sides(data.get("castellated_sides"))
    else:
        sides = None
        for key in ("rows", "cols", "pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm"):
            if data.get(key) is None:
                raise ExtractionError(
                    tx("La risposta del modello non contiene il campo obbligatorio '{key}'. "
                       "Riprova, eventualmente con un modello piu' capace.",
                       key=key)
                )

    populated = data.get("populated_pins")

    package_geometry = data.get("package_geometry")
    if package_geometry is not None:
        try:
            # Validate now, while the source response and its warnings are still
            # available; the GUI must never receive malformed pad coordinates.
            package_from_mapping(package_geometry)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(tx("Geometria package non valida: {exc}", exc=exc)) from exc

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
        pcb_thickness_mm=_optional_number(data.get("pcb_thickness_mm"), positive=True),
        body_offset_y_mm=_opt_float("body_offset_y_mm"),
        pin_length_3d_mm=_opt_float("pin_length_3d_mm"),
        edge_fillets=data.get("edge_fillets") or None,
        pinout=data.get("pinout") or None,
        # La specchiatura di pin_map ha senso solo sulla matrice: per i moduli
        # castellated le posizioni vengono dai lati, non da righe/colonne.
        pin_map=_pin_map_seen_from_top(
            data.get("pin_map"), int(data["cols"]), data.get("pin_map_view"),
        ) if layout == "matrix" else None,
        body_offset_x_mm=_offset_x_seen_from_top(
            _opt_float("body_offset_x_mm"), _opt_float("body_length_mm"),
            data.get("cols"), _opt_float("pitch_mm"),
            data.get("pin_map_view") if layout == "matrix" and data.get("pin_map") else None),
        body_color_hex=data.get("body_color_hex") or None,
        text_color_hex=data.get("text_color_hex") or None,
        package_geometry=package_geometry,
    )
