"""@brief Footprint parameter extraction from datasheets through the OpenRouter API.

Estrazione parametri footprint da datasheet tramite l'API OpenRouter.

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
import copy
import mimetypes
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..cad3d.annotations import EDGE_NAMES
from ..core.package_geometry import package_from_mapping
from ..generators.castellated_module import (
    CONTACT_STYLES, EDGE_LABELS, EDGES,
)
from .ai_diagnostics import CallCancelled
from .openrouter import (DEFAULT_MODEL, OpenRouterError, api_key, call_tool, check_credit,
                         reset_usage, usage_totals)
from .messages import end_sentence, tx

MAX_FILES = 5

#: Mechanical body kinds a photo or a drawing may declare for a mounted part,
#: each with the description the model reads (Italian, like every prompt).
#:
#: A kind is a STRUCTURAL class -- the shape of the body and of its terminals,
#: which decides the 3D shape and the plausible height of an unmeasured body --
#: never a brand, a part number or an electrical function. The set covers the
#: shapes whose height or outline differs enough to matter for a mechanical
#: envelope (a flat chip, a gull-wing package, a can standing on the board, a
#: strip of posts, a flat panel...); anything else is "generic", and an
#: unknown value sent by a model degrades to "generic" with a note
#: (`_components_3d`). The 3D side keeps its own copy (cad3d/freecad_parts.
#: BUILDERS, which only runs inside FreeCAD): a kind it does not draw yet is
#: drawn as the generic block.
COMPONENT_KIND_DESCRIPTIONS: dict[str, str] = {
    "generic": "corpo non riconoscibile: blocco semplice",
    "ic": "integrato di package non riconoscibile",
    "ic_soic": ("integrato rettangolare con reofori ad ala di gabbiano su due lati opposti "
                "(SO, SOIC, SSOP, TSSOP, MSOP)"),
    "ic_sot": ("piccolo package plastico con pochi reofori corti su uno o due lati (famiglia "
               "SOT e SC-70, anche con linguetta)"),
    "ic_qfn": ("integrato piatto senza reofori sporgenti, contatti sotto il bordo del corpo "
               "(QFN, DFN, LGA)"),
    "ic_qfp": "integrato con reofori ad ala di gabbiano su quattro lati (QFP, LQFP, TQFP)",
    "ic_bga": "integrato con sfere sotto il corpo e nessun terminale visibile ai lati (BGA, CSP)",
    "power_tab": ("package di potenza con linguetta metallica di dissipazione (TO-252/DPAK, "
                  "TO-263/D2PAK, TO-220)"),
    "transformer": "trasformatore o avvolgimento su nucleo, corpo alto",
    "module": "sotto-modulo su una propria PCB montato sulla scheda",
    "shield_can": "schermo metallico di lamiera sopra un gruppo di componenti",
    "led": "LED a montaggio superficiale con lente",
    "capacitor_tantalum": "condensatore stampato con striscia di polarita' (tantalio o polimero)",
    "capacitor": "condensatore chip rettangolare con due terminazioni metalliche",
    "capacitor_electrolytic": ("condensatore elettrolitico cilindrico (lattina su base plastica "
                               "o radiale), in piedi sulla scheda"),
    "resistor": "resistenza chip rettangolare con due terminazioni metalliche",
    "inductor": "induttore chip o bobinato di piccole dimensioni",
    "inductor_power": ("induttore di potenza stampato o schermato, quadrato o tondo, con "
                       "altezza paragonabile al lato"),
    "diode": "diodo in package plastico piatto con banda (SOD, SMA, SMB, SMC)",
    "diode_melf": "diodo cilindrico di vetro con banda e cappucci metallici, da non confondere con un chip",
    "connector": "connettore non meglio classificabile (USB, coassiale, morsettiera, FPC...)",
    "pin_header": "fila o matrice di pin maschi a passo regolare su una base plastica",
    "socket_header": "fila o matrice di contatti femmina in un corpo plastico chiuso",
    "switch": "pulsante o interruttore",
    "relay": "rele': blocco plastico chiuso, alto",
    "potentiometer": "trimmer o potenziometro con vite o albero di regolazione",
    "crystal": "quarzo o oscillatore piatto in contenitore ceramico o metallico",
    "crystal_can": "quarzo in contenitore metallico ovale alto",
    "battery": "batteria o portabatteria (a bottone o cilindrica)",
    "display": "display piatto (vetro LCD od OLED), con o senza cornice",
    "buzzer": "cicalino o trasduttore cilindrico",
}

#: The kinds, in the order the schemas offer them.
COMPONENT_KINDS = tuple(COMPONENT_KIND_DESCRIPTIONS)
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB, limite ragionevole per singolo file API

_SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

_TOOL_NAME = "report_pin_header_params"

#: Length fields of the answer, per level: every one is a length in the
#: declared drawing unit and becomes millimetres after `_canonicalize_answer`.
#: They are also the key dimensions whose provenance the model declares
#: (`value_sources`).
_TOP_LENGTH_KEYS = ("pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm",
                    "body_length_mm", "body_width_mm", "body_height_mm", "pcb_thickness_mm",
                    "body_offset_x_mm", "body_offset_y_mm", "pin_length_3d_mm")
_SIDE_LENGTH_KEYS = ("pitch_mm", "offset_mm", "pad_length_mm", "pad_width_mm",
                     "half_hole_diameter_mm", "through_hole_drill_mm")

#: Provenance the model may declare for a value (`value_sources`): "read"
#: written in a document (a dimension, a table), "derived" computed from read
#: values only, "estimated" judged by eye on a photograph, scaled from other
#: dimensions or taken from a design rule because no document gives it.
VALUE_SOURCES = ("read", "derived", "estimated")
#: Provenance recorded for a value the answer carries without a valid source:
#: it is treated as needing review, exactly like an estimated one, so a value
#: whose origin nobody declared never passes for a read one.
UNDECLARED_SOURCE = "undeclared"
#: Provenances that ask for a review before the value is used.
REVIEW_SOURCES = ("estimated", UNDECLARED_SOURCE)


def _value_sources_schema(keys, where: str) -> dict:
    """Schema of a `value_sources` object: one optional provenance per length key."""
    return {
        "type": "object",
        "description": (
            f"Provenienza di ogni quota che compili {where}, campo per campo (chiave = "
            "nome del campo): \"read\" se e' scritta nel documento (quota del disegno, "
            "tabella); \"derived\" se la calcoli SOLO da quote lette (somma, differenza, "
            "meta'); \"estimated\" se la stimi: a occhio da una foto, in proporzione ad "
            "altre quote o con una regola di progetto perche' il documento non la riporta. "
            "Dichiarala per OGNI quota che compili: una quota senza provenienza viene "
            "trattata come stimata e segnalata all'utente."
        ),
        "properties": {key: {"type": "string", "enum": list(VALUE_SOURCES)} for key in keys},
    }

_EXTRACTION_TOOL = {
    "name": _TOOL_NAME,
    "description": (
        "Riporta i parametri meccanici di un componente estratti dai documenti: "
        "contatti (matrice di pin passanti, lati di un modulo castellated oppure "
        "pad espliciti in package_geometry), corpo, pinout, unita' di misura e "
        "vista del disegno letto."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "layout": {
                "type": "string",
                "enum": ["matrix", "castellated"],
                "description": (
                    "Come sono disposti i contatti del componente.\n"
                    "- \"matrix\": pin a foro passante su una griglia regolare righe x "
                    "colonne (pin header, moduli DIP-style, componenti THT). Compila "
                    "rows, cols, pad_diameter_mm, drill_mm, pitch_mm se cols > 1 e "
                    "row_spacing_mm se rows > 1. Usa \"matrix\" anche quando i pad "
                    "sono descritti uno per uno in package_geometry (componenti SMD, "
                    "package irregolari): allora package_geometry e' il footprint e i "
                    "campi della matrice sono facoltativi.\n"
                    "- \"castellated\": modulo su PCB con contatti metallizzati sul "
                    "bordo (semi-fori). Compila castellated_sides e i campi body_*; NON "
                    "forzare i contatti in una matrice, perche' lati diversi hanno "
                    "spesso numero di contatti e passo diversi: in quel caso "
                    "\"matrix\" darebbe un footprint sbagliato."
                ),
            },
            "drawing_unit": {
                "type": "string",
                "enum": ["mm", "inch", "mil"],
                "description": (
                    "Unita' di misura delle quote del disegno che leggi: \"mm\" "
                    "millimetri, \"inch\" pollici, \"mil\" millesimi di pollice. TUTTE "
                    "le lunghezze di questa risposta (ogni campo con suffisso _mm, "
                    "anche dentro castellated_sides, package_geometry, components_3d "
                    "ed edge_fillets) vanno riportate in questa unita', con i numeri "
                    "COSI' COME LI LEGGI, senza convertirli: la conversione in "
                    "millimetri la fa il programma. Se il disegno riporta due unita' "
                    "affiancate, usa i millimetri. Senza disegno quotato usa \"mm\"."
                ),
            },
            "contacts_view": {
                "type": "string",
                "enum": ["top", "bottom", "unknown"],
                "description": (
                    "Da che lato e' visto il disegno (o la foto) da cui leggi la "
                    "disposizione dei contatti: pin_map, l'ordine di numerazione della "
                    "matrice, castellated_sides, body_offset_x_mm e body_offset_y_mm. "
                    "\"bottom\" se e' etichettato BOTTOM VIEW o mostra il lato "
                    "inferiore, \"top\" se e' una vista dall'alto o una foto del lato "
                    "componenti, \"unknown\" se il documento non permette di "
                    "stabilirlo. Riporta tutto COME LO VEDI, senza ribaltare nulla: il "
                    "footprint KiCad si disegna visto dall'alto e la specchiatura "
                    "sinistra-destra di una bottom view la fa il programma. "
                    "package_geometry dichiara la sua vista in source_view."
                ),
            },
            "package_geometry": {
                "type": "object",
                "description": (
                    "Pad espliciti, uno per pin: il modo di descrivere i componenti SMD "
                    "(pad senza foro, pad_type \"smd\") e i package non rappresentabili "
                    "come matrice (SOIC, TSSOP, QFN, QFP, DIP irregolari, package "
                    "custom). Quando e' presente e' la fonte del footprint. Coordinate "
                    "locali del centro di ogni pad, come appaiono nella vista indicata "
                    "da source_view (la specchiatura della vista dal basso la fa il "
                    "programma). Un pad esposto/termico e' un pin a se'. Non inventare "
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
                "description": (
                    "Passo tra pin ADIACENTI sulla STESSA riga, nell'unita' di "
                    "drawing_unit. Ometti con una sola colonna."
                ),
            },
            "row_spacing_mm": {
                "type": "number",
                "description": (
                    "Distanza PERPENDICOLARE tra i centri di due file di pin adiacenti, "
                    "nell'unita' di drawing_unit. Ometti con una sola riga (rows=1). "
                    "IMPORTANTE: e' una quota distinta dal pitch e nei package "
                    "DIP-style e' di norma molto maggiore: leggile separatamente, non "
                    "riusare un numero per l'altro. "
                    "Anche con layout=\"castellated\", se i contatti stanno su due lati "
                    "opposti: distanza fra i CENTRI dei contatti delle due file, se quotata "
                    "o leggibile sulla griglia; ometti se non la conosci."
                ),
            },
            "pad_diameter_mm": {
                "type": "number",
                "description": "Diametro del pad a foro passante della matrice, nell'unita' di drawing_unit.",
            },
            "drill_mm": {
                "type": "number",
                "description": (
                    "Diametro del foro (drill) dei pad della matrice, nell'unita' di "
                    "drawing_unit: sempre maggiore di zero e minore di pad_diameter_mm. "
                    "I pad SMD non hanno foro: per un componente SMD ometti questo "
                    "campo e descrivi i pad in package_geometry."
                ),
            },
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
                    "corpo (asse X, stessa direzione del pitch) nell'unita' di "
                    "drawing_unit. Ometti se "
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
                    "laterale del disegno (o nel documento dichiarato come vista "
                    "laterale), nell'unita' di drawing_unit; non riusare un numero "
                    "letto in pianta."
                ),
            },
            "body_offset_x_mm": {
                "type": "number",
                "description": (
                    "Posizione del bordo del corpo lungo X rispetto al pin 1 (che e' "
                    "sempre nell'origine x=0 del footprint). Tipicamente un valore "
                    "negativo pari alla distanza dal bordo sinistro del corpo al pin 1, "
                    "presa dalla quota del disegno meccanico (se il pin 1 dista d dal "
                    "bordo sinistro, body_offset_x_mm = -d). Leggilo sullo STESSO "
                    "disegno da cui leggi i pin, cosi' come lo vedi: se contacts_view e' "
                    "\"bottom\" lo ribalta il programma insieme alle colonne."
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
                    "nel corpo vale -(body_width_mm - distanza fra prima e ultima riga)/2, "
                    "non -body_width_mm/2. Meta' della larghezza col segno meno e' giusta "
                    "solo con UNA "
                    "sola riga centrata. Tutti i centri dei pin devono cadere dentro il "
                    "corpo."
                ),
            },
            "pin_length_3d_mm": {
                "type": "number",
                "description": (
                    "Lunghezza del pin che sporge sotto il corpo (protrusione verso il "
                    "PCB), nell'unita' di drawing_unit -- quota meccanica, NON il "
                    "drill del footprint."
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
                                "lato in BASSO della vista, back = lato in alto, top = "
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
                                "Raggio stimato dalla foto, nell'unita' di drawing_unit "
                                "(nessuna quota "
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
                    "a contacts_view). Serve quando la numerazione NON segue, nel "
                    "disegno letto, l'ordine colonna per colonna (1,2 poi 3,4...) "
                    "tipico dei pin header: i package DIP-style numerano a U (1..N "
                    "lungo una fila, poi a ritroso sull'altra). Se la numerazione segue "
                    "l'ordine delle colonne puoi ometterla: contacts_view vale comunque, "
                    "e una bottom view viene specchiata anche senza pin_map."
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
            "pinout": {
                "type": "array",
                "description": (
                    "Tabella pinout del datasheet: per ogni pin FISICAMENTE PRESENTE, "
                    "il numero e la funzione. Se il datasheet elenca piu' configurazioni "
                    "(varianti in colonne diverse), riporta la prima e aggiungi "
                    "l'alternativa tra parentesi solo se diversa. "
                    "Ometti se nel documento non c'e' una tabella pinout."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "integer", "description": "Numero del pin"},
                        "name": {
                            "type": "string",
                            "description": (
                                "Funzione del pin come la scrive il documento (NC se "
                                "non collegato)."
                            ),
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
                    "modulo che porta contatti (fino a quattro), letto COSI' COME "
                    "APPARE nel disegno o nella foto, senza ribaltare nulla: la vista "
                    "letta va dichiarata in contacts_view e la specchiatura di una "
                    "bottom view la fa il programma. Ogni lato ha il suo numero di "
                    "contatti e il suo passo: e' proprio il motivo per cui questi "
                    "moduli non stanno in una matrice. L'origine e' il centro del corpo."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "edge": {
                            "type": "string",
                            "enum": list(EDGES),
                            "description": (
                                "Lato del modulo come appare nella vista letta: "
                                "\"left\"/\"right\" i lati verticali, \"top\"/\"bottom\" "
                                "quelli orizzontali. \"top\" e' il lato in alto nel "
                                "disegno."
                            ),
                        },
                        "count": {
                            "type": "integer",
                            "description": (
                                "Numero di contatti FISICI su questo lato: i fori "
                                "metallizzati tagliati dal bordo (semi-fori) o i fori "
                                "interi del bordo, contati uno per uno sulla foto e sugli "
                                "ingrandimenti dei bordi quando ci sono. Le sigle "
                                "serigrafate, le etichette dei LED o dei componenti e il "
                                "testo vicino al bordo NON sono contatti: conta i fori, "
                                "non le scritte. Verifica che (count-1) x pitch_mm stia "
                                "nel bordo quotato."
                            ),
                        },
                        "pitch_mm": {
                            "type": "number",
                            "description": (
                                "Passo tra contatti adiacenti SU QUESTO LATO, "
                                "nell'unita' di drawing_unit: puo' essere diverso da "
                                "quello degli altri lati."
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
                                "rispetto alla posizione centrata, nell'unita' di "
                                "drawing_unit "
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
                                "piu' dentro. Ometti se la foto non mostra il bordo o se "
                                "non distingui il tipo: non dedurlo e non scegliere un "
                                "valore abituale, il programma lo chiedera' all'utente."
                            ),
                        },
                        "half_hole_diameter_mm": {
                            "type": "number", "exclusiveMinimum": 0,
                            "description": (
                                "Diametro del semi-foro metallizzato tagliato dal bordo "
                                "(il cerchio intero di cui il bordo mostra meta'), "
                                "nell'unita' di drawing_unit: dalla quota del disegno "
                                "o, se non quotato, stimato dalla foto in proporzione "
                                "alle quote note (\"estimated\" in value_sources). Ometti "
                                "se il lato non ha semi-fori o non si vedono."
                            ),
                        },
                        "through_hole_drill_mm": {
                            "type": "number", "exclusiveMinimum": 0,
                            "description": (
                                "Diametro del foro intero (foro passante vicino al "
                                "bordo) dei contatti di questo lato, nell'unita' di "
                                "drawing_unit: dalla quota del disegno o, se non "
                                "quotato, stimato dalla foto in proporzione alle quote "
                                "note (\"estimated\" in value_sources). Solo se il lato "
                                "ha fori interi; altrimenti ometti."
                            ),
                        },
                        "value_sources": _value_sources_schema(_SIDE_LENGTH_KEYS, "per questo lato"),
                        "pin_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Sigle serigrafate accanto ai contatti di questo lato, "
                                "nell'ordine in cui si incontrano lungo il bordo nella "
                                "vista letta (dall'alto in basso sui lati verticali, da "
                                "sinistra a destra su quelli orizzontali). Esattamente "
                                "un nome per contatto contato in count: una sigla "
                                "stampata a cavallo di due fori (es. due lettere "
                                "vicine) va divisa fra i due contatti, non unita in un "
                                "nome solo; le etichette di LED o componenti vicini non "
                                "sono nomi di contatti. Servono a documentare il "
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
                    "Elenca TUTTI i componenti visibili, uno per uno, grandi e piccoli, "
                    "compreso OGNI singolo passivo, ciascuno con la propria posizione e "
                    "il proprio orientamento (length_mm asse lungo locale, width_mm asse "
                    "corto, rotation_deg orientamento). Non esiste un numero atteso: "
                    "riporta i corpi che vedi, senza aggiungerne ne' toglierne per "
                    "arrivare a un conteggio. Nessuna quota di questi e' data dal "
                    "datasheet -- sono stime visive in proporzione alle dimensioni note "
                    "del modulo, ed e' normale che sia cosi'. "
                    "Ometti il campo se non c'e' una foto dall'alto."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Tipo del corpo in parole semplici, senza codici "
                                "commerciali: finisce nel nome del solido."
                            ),
                        },
                        "x_mm": {
                            "type": "number",
                            "description": (
                                "Posizione del CENTRO del componente lungo X rispetto al "
                                "centro del modulo, nell'unita' di drawing_unit: negativa verso "
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
                                "Lunghezza del componente lungo il SUO asse lungo, "
                                "indipendentemente da come e' ruotato sul modulo: "
                                "l'orientamento va in rotation_deg."
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
                                "Altezza sopra il PCB: da una vista laterale quotata se "
                                "c'e', altrimenti stimata dal tipo di corpo (kind) e dalle "
                                "sue dimensioni in pianta; da una sola foto dall'alto e' "
                                "sempre una stima. Non superare l'altezza totale del "
                                "modulo meno lo spessore del PCB."
                            ),
                        },
                        "color_hex": {
                            "type": "string",
                            "description": "Colore visto in foto, '#rrggbb'.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": list(COMPONENT_KINDS),
                            "description": (
                                "Tipo di corpo riconosciuto dalla foto dalla sua FORMA "
                                "(non dalla funzione elettrica ne' dalla marca): decide la "
                                "forma nel modello 3D e l'altezza plausibile. \"generic\" "
                                "se non riconoscibile. Valori: "
                                + "; ".join(f"{kind} = {text}" for kind, text
                                            in COMPONENT_KIND_DESCRIPTIONS.items()) + "."
                            ),
                        },
                        "marking": {
                            "type": "string",
                            "description": (
                                "Scritta stampata sul componente, SOLO se leggibile nella "
                                "foto, copiata carattere per carattere: serve a cercare il "
                                "modello di libreria per marcatura, non viene stampata sul "
                                "solido. Non inventare codici."
                            ),
                        },
                    },
                    "required": ["x_mm", "y_mm", "length_mm", "width_mm", "height_mm"],
                    # The model may describe only these fields: internal
                    # provenance and verification flags (`_`-prefixed keys)
                    # are set by the program, never by an answer. The
                    # decoder enforces the same rule (_components_3d).
                    "additionalProperties": False,
                },
            },
            "value_sources": _value_sources_schema(
                _TOP_LENGTH_KEYS, "a questo livello (i lati castellated hanno il loro)"),
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
        # castellated sono richiesti a seconda di cio' che la risposta e'
        # (layout, pad espliciti, numero di righe e colonne), e il controllo
        # si fa in _params_from_tool_arguments() dove si conoscono i valori.
        # Unita' e vista sono obbligatorie perche' il modello le dichiari
        # sempre; una risposta che le omette resta leggibile (millimetri,
        # vista ignota dichiarata come dubbio nelle note).
        "required": ["layout", "confidence", "drawing_unit", "contacts_view"],
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
    "Sei un assistente che legge datasheet di componenti, connettori e moduli "
    "elettronici (PDF o immagini di disegni meccanici quotati) ed estrae i parametri "
    "geometrici necessari per generare un footprint KiCad: una matrice di pin a foro "
    "passante, i lati di un modulo castellated oppure i pad espliciti in "
    "package_geometry (l'unico modo di descrivere pad SMD, che non hanno foro). "
    "Riporta le quote COSI' COME LE LEGGI: dichiara in drawing_unit l'unita' del "
    "disegno (mm, inch o mil) e in contacts_view da che lato e' visto il disegno dei "
    "contatti (top, bottom o unknown); conversione e specchiatura le fa il programma. "
    "Distingui SEMPRE con attenzione pitch_mm (passo tra pin adiacenti sulla stessa "
    "riga) da row_spacing_mm (distanza perpendicolare tra file di pin adiacenti): sono "
    "quasi sempre valori molto diversi tra loro nei moduli DIP-style, non usare lo "
    "stesso numero per entrambi a meno che il disegno meccanico non lo confermi "
    "esplicitamente. Se il pinout mostra che solo alcune posizioni della matrice hanno "
    "un pin fisico, usa populated_pins per elencarle invece di generare una matrice "
    "completa. Per ogni quota che compili dichiara in value_sources (e, per i lati "
    "castellated, nel value_sources del lato) se l'hai letta (\"read\"), ricavata "
    "solo da quote lette (\"derived\") o stimata (\"estimated\"). Se un valore "
    "necessario non e' presente nei documenti, stimalo dalle altre quote o dalle "
    "regole di progetto del tipo di contatto, dichiaralo \"estimated\" e scrivi in "
    "notes da cosa lo hai ricavato: un valore stimato non deve mai sembrare letto. "
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
    "\"matrix\" per i pin a foro passante su griglia regolare (pin header, "
    "moduli DIP-style) e per i componenti descritti pad per pad in "
    "package_geometry, \"castellated\" per i moduli su PCB con semi-fori "
    "metallizzati sul bordo. Nei moduli castellated ogni lato ha spesso un "
    "numero di contatti e un passo diversi dagli altri: descrivili in castellated_sides, un "
    "elemento per lato, invece di comprimerli in una matrice righe x colonne che "
    "non li rappresenta e produrrebbe un footprint sbagliato. "
    "Per ogni lato castellated indica anche contact_style guardando il bordo nella "
    "foto: half_hole se il foro metallizzato e' tagliato dal bordo (mezzaluna aperta "
    "verso l'esterno), through_hole se il foro e' intero, con corona chiusa e scheda "
    "intatta fra la corona e il bordo, half_and_through se lo stesso contatto ha "
    "entrambi; ometti il campo se il bordo non si vede o il tipo non si distingue "
    "(il programma lo chiedera' all'utente). Se quotati o visibili, "
    "riporta per lato anche half_hole_diameter_mm e through_hole_drill_mm. "
    "Per i moduli su PCB, se c'e' una foto del lato superiore, elenca in "
    "components_3d TUTTI i componenti visibili, uno per uno, compresi i passivi "
    "piccoli (resistenze, condensatori chip, diodi): posizione, ingombro (asse "
    "lungo x asse corto), orientamento in rotation_deg (0 orizzontale, 90 "
    "verticale, letto dalla foto) e colore stimati a occhio in proporzione alle dimensioni note "
    "del modulo. Servono a dare al modello 3D l'aspetto del pezzo reale, e "
    "nessuna di quelle quote e' riportata dal datasheet -- e' normale e atteso "
    "stimarle. Non esiste un numero atteso di componenti: elenca quelli che vedi, "
    "senza aggiungerne ne' toglierne per raggiungere un conteggio. Compila sempre "
    "anche pinout con la funzione di ogni contatto, leggendola dalle sigle "
    "serigrafate accanto ai contatti nella foto o dalle etichette di "
    "un'immagine descrittiva del prodotto. "
    "Rispondi SOLO chiamando lo strumento fornito."
)

# Alias pubblico: la GUI (tab "Prompt LLM") lo mostra come testo di partenza
# editabile, e passa la versione modificata a extract_pin_header_params().
DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT

#: SHA-256 (hex digest of the UTF-8 text) of every earlier built-in system
#: prompt. A profile database whose global system prompt is still exactly one
#: of them receives the current DEFAULT_SYSTEM_PROMPT (see
#: analysis_profiles.ProfileStore); a prompt the user edited never matches and
#: is kept. Digests rather than texts because each old prompt is several kB;
#: the texts are in the git history of this file. The last two: the prompt
#: that described only a through-hole pin matrix, before `drawing_unit`,
#: `contacts_view` and the SMD route through package_geometry; then the one of
#: commit 8970c7e, which still carried numbers and names of the sample board
#: (a 30-50 component count, its silkscreen names, a 2.54 mm drill/pad
#: "standard") and no per-value provenance (replaced 2026-09-26).
SUPERSEDED_SYSTEM_PROMPT_DIGESTS = frozenset({
    "c744e556d5e1eba95f41d69b5eb635c2ab8bcc0cf77f5c1d163b18ca3a41feea",
    "920e38029a2f7b6da293539e9a4ec3ea9b2d8465b51f45a49cea33a008671d57",
    "4d9766aa02d962df5d1da6a726280f44e9404247e23e59f1cc1443bbde17ec82",
    "eefd487b98f9217c71c14976fe71ac0095a14dd8ed9b8bd1b5d58e8231c0aafa",
    "31a4c60ce3dedcce3efe236114fa2b963fcbfed652a40f62cbc6609e5e426715",
    "2e420a121ee16fc0877e6d74ac4efdc7e0731f2e3cc29b4c32c0d014364877d0",
    "d00743ee29a91d396eae57cd56e40dbc2e03825d571960dd09f88ba36d0f754f",
})


@dataclass
class ExtractedParams:
    """@brief Extraction result for matrix and castellated contact layouts.

    Risultato dell'estrazione, per entrambe le disposizioni di contatti.

    `layout` dice quali campi sono significativi: "matrix" valorizza
    righe/colonne/passo/pad/drill, "castellated" valorizza
    `castellated_sides` (un elemento per lato del modulo) e lascia a None i
    campi della matrice, che per quei componenti non esistono.

    @note Every length is in millimetres and every contact position is seen
        from above, whatever unit and view the model read (see
        `_canonicalize_answer`); conversions and doubts are in `notes`.
    @note When `package_geometry` is set (explicit pads, e.g. an SMD part) it
        is the footprint: the matrix fields are informative and may be None,
        and `drill_mm` is None for pads without hole. Otherwise a "matrix"
        result is a through-hole grid with rows, cols, pad and drill always
        set, `pitch_mm` set when cols > 1 and `row_spacing_mm` when rows > 1
        (None otherwise: a single column or row has no such distance).
    @note Keys of each `castellated_sides` dict: edge, count, pitch_mm,
        first_pin, contact_style, and optionally reverse, offset_mm,
        pad_length_mm, pad_width_mm, pin_names, half_hole_diameter_mm (plated
        half-hole cut by the edge) and through_hole_drill_mm (drill of the full
        hole), all lengths in mm. `contact_style` is always present: one of
        castellated_module.CONTACT_STYLES when the model observed it, None
        when it did not -- never a default, so the caller must ask the user
        (see `unobserved_contact_style_edges`).
    @note `value_sources` maps every key dimension the result carries to its
        provenance: "read", "derived", "estimated" (VALUE_SOURCES) or
        "undeclared" when the model gave none. Keys are the field names of
        this class (e.g. "body_height_mm") and, for a castellated side,
        "castellated_sides.<edge>.<field>" with the edge seen from above
        (e.g. "castellated_sides.left.half_hole_diameter_mm"). A value the
        program itself estimated (e.g. `fit_body_to_pin_field`) is
        "estimated". Absent keys are values the result does not carry.
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
    value_sources: Optional[dict] = None

    def values_to_review(self) -> list[str]:
        """@brief Return the keys of `value_sources` whose value was not read nor derived.

        @return Sorted keys whose provenance is "estimated" or "undeclared":
            the values to show as estimates and to confirm before use.
        """
        return sorted(key for key, source in (self.value_sources or {}).items()
                      if source in REVIEW_SOURCES)

    def unobserved_contact_style_edges(self) -> list[str]:
        """@brief Return the castellated edges whose contact style nobody observed.

        @return Edges (seen from above, in side order) whose side has
            `contact_style` None: the style must be asked to the user, never
            assumed. Empty for a non-castellated result.
        """
        return [side["edge"] for side in (self.castellated_sides or ())
                if side.get("contact_style") is None]

    def has_3d_body(self) -> bool:
        """@brief Return whether body length, width and height are all known, so a 3D body can be built."""
        return self.body_length_mm is not None and self.body_width_mm is not None and self.body_height_mm is not None

    def is_castellated(self) -> bool:
        """@brief Return whether the contacts use the castellated (per-edge) layout."""
        return self.layout == "castellated"


class ExtractionError(Exception):
    """@brief Application error of the extraction: missing key, invalid file or unexpected answer.

    Errore applicativo (chiave mancante, file non valido, risposta inattesa).

    @note `raw_response`, when set, is the paid main answer that failed the
        validation, kept so a retry can be compared with it (it is also
        written to the AI log as an AI_RESPONSE_REJECTED line).
    """

    raw_response = None


class ExtractionCancelled(ExtractionError, CallCancelled):
    """@brief The user cancelled the extraction before the next paid call.

    @note Subclass of ExtractionError, so existing handlers still stop the
        run; also a CallCancelled, so no optional step (photo inventory,
        tiles, reviews) downgrades it to a note.
    """


#: Bound of the rejected main answer written to the log, in characters: the
#: tool arguments of one component are a few kB, so this keeps every real
#: answer whole while a runaway one cannot flood the log.
_REJECTED_RESPONSE_MAX_CHARS = 200_000


def _log_rejected_response(data, exc: ExtractionError, progress) -> None:
    """Keep the paid answer that failed validation, so a retry is not blind.

    The answer is attached to the exception and written to the AI log as one
    machine line (AI_RESPONSE_REJECTED ...). It is never cached: an invalid
    answer must not be replayed as a success.
    """
    exc.raw_response = data
    if not progress:
        return
    import json
    try:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        text = repr(data)
    if len(text) > _REJECTED_RESPONSE_MAX_CHARS:
        text = text[:_REJECTED_RESPONSE_MAX_CHARS] + " …[truncated]"
    try:
        progress(tx("Risposta principale scartata dalla validazione: {exc}. Il testo della "
                    "risposta resta nel log AI (riga AI_RESPONSE_REJECTED) per confrontarlo "
                    "con un nuovo tentativo.", exc=exc))
        progress("AI_RESPONSE_REJECTED " + text)
    except Exception:  # noqa: BLE001 - il log non deve nascondere l'errore vero
        pass


def _guess_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type:
        return media_type
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


#: EXIF tag of the orientation; 1 means «stored upright».
_EXIF_ORIENTATION = 0x0112


def _upright_image_bytes(data: bytes) -> bytes:
    """The image as the analysis and the preview see it, EXIF orientation applied.

    Phones store the pixels one way and the right way in the orientation tag.
    OpenCV (the analysis) and the preview apply it; a provider may not, and
    the model would then read a rotated photo (utente, 2026-09-25). A photo
    without a rotation tag is returned byte for byte; one with it is rotated
    and re-encoded in the same format, without the tag. Any decoding problem
    returns the original bytes: sending the file as it is was the old
    behaviour, never worse than failing.
    """
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
        with Image.open(BytesIO(data)) as image:
            orientation = image.getexif().get(_EXIF_ORIENTATION, 1)
            if orientation in (None, 1):
                return data
            file_format = image.format or "PNG"
            upright = ImageOps.exif_transpose(image)
            out = BytesIO()
            options = {"quality": 95} if file_format.upper() in ("JPEG", "WEBP") else {}
            upright.save(out, format=file_format, **options)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - si ripiega sul file originale
        return data


#: Component family whose contacts are the holes cut by the board edge.
CASTELLATED_FAMILY = "aggregate_castellated"


def _edge_strip_blocks(doc_paths, document_roles) -> list:
    """Enlarged strips of the board edges of each declared photograph, as content blocks.

    The model counts the physical contacts of a castellated module in its
    first answer (user decision 2026-09-28, HANDOFF point 142): each strip
    shows one edge from close up, with the labels printed beside the holes,
    so that holes and labels can be told apart. Cut without the board
    dimensions (edge_strips.board_box); a photo where the board cannot be
    found adds nothing. Never fatal: the strips are help, not a measure.
    """
    import cv2
    import numpy as np
    from .edge_strips import edge_strips
    blocks = []
    for index, path in enumerate(doc_paths):
        if PHOTO_ROLE not in _declared_role_keys(document_roles.get(path.name) or ()):
            continue
        try:
            payload = _upright_image_bytes(Path(path).read_bytes())
            image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            strips = edge_strips(image) if image is not None else []
        except (OSError, ValueError, cv2.error):
            strips = []
        for edge, png in strips:
            blocks.append({"type": "text", "text": (
                f"FILE {index}, ingrandimento del bordo {edge} della scheda (stessa vista "
                "della foto, bordo scheda verso l'esterno dell'immagine): conta qui i fori "
                "metallizzati tagliati dal bordo, uno per contatto; le scritte accanto sono "
                "sigle, non contatti.")})
            data_b64 = base64.b64encode(png).decode("ascii")
            blocks.append({"type": "image_url",
                           "image_url": {"url": f"data:image/png;base64,{data_b64}"}})
    return blocks


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
        raise ExtractionError(tx("{name} troppo grande ({size:.1f} MB, max {limit} MB)",
                                 name=path.name, size=size / 1_048_576,
                                 limit=MAX_FILE_BYTES // 1_048_576))

    media_type = _guess_media_type(path)
    payload = path.read_bytes()
    if media_type.startswith("image/"):
        payload = _upright_image_bytes(payload)
    data_b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:{media_type};base64,{data_b64}"

    if media_type == "application/pdf":
        return {"type": "file", "file": {"filename": path.name, "file_data": data_url}}
    return {"type": "image_url", "image_url": {"url": data_url}}


def _pin_map_seen_from_top(
    pin_map: Optional[Sequence[dict]], cols: int, view: Optional[str],
    rows: Optional[int] = None,
) -> Optional[list]:
    """Converte le posizioni lette dal disegno in coordinate viste dall'alto.

    Il footprint KiCad si disegna dal lato componenti, mentre i datasheet dei
    moduli mostrano quasi sempre una BOTTOM VIEW: senza specchiare si otterrebbe
    un footprint speculare, con i fori in posizioni che non coincidono con il
    componente reale.

    La trasformazione si fa qui e non nel prompt perche' e' una riflessione
    geometrica esatta: il modello legge (compito percettivo), il codice ribalta
    (compito deterministico).

    @throws ExtractionError When an entry is not an object with integral
        pin >= 1, row >= 0 and col >= 0 (booleans and fractions rejected),
        lies outside the rows x cols matrix (rows checked when given), or
        repeats a pin number or a position. The map is a numbering convention
        as a whole, so one bad entry invalidates it.
    """
    if not pin_map:
        return None
    if not isinstance(pin_map, (list, tuple)):
        raise ExtractionError(tx("pin_map non e' un elenco di posizioni."))
    entries = []
    pins, positions = {}, {}
    for index, raw in enumerate(pin_map, 1):
        values = {}
        if isinstance(raw, dict):
            values = {key: _optional_number(raw.get(key), integer=True)
                      for key in ("pin", "row", "col")}
        if (not values or any(v is None for v in values.values())
                or values["pin"] < 1 or values["row"] < 0 or values["col"] < 0):
            raise ExtractionError(tx("pin_map, voce {index}: servono pin, row e col interi "
                                     "(pin da 1, row e col da 0).", index=index))
        pin, row, col = values["pin"], values["row"], values["col"]
        if col >= cols or (rows is not None and row >= rows):
            raise ExtractionError(tx("pin_map: pin {pin} in riga {row}, colonna {col}, fuori "
                                     "dalla matrice {rows}x{cols}.", pin=pin, row=row, col=col,
                                     rows=rows if rows is not None else "?", cols=cols))
        if pin in pins:
            raise ExtractionError(tx("pin_map: il pin {pin} compare piu' volte.", pin=pin))
        if (row, col) in positions:
            raise ExtractionError(tx("pin_map: i pin {pin} e {other} occupano la stessa "
                                     "posizione.", pin=pin, other=positions[(row, col)]))
        pins[pin] = True
        positions[(row, col)] = pin
        entries.append({"pin": pin, "row": row, "col": col})
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
    if view != "bottom" or offset is None or length is None:
        return offset
    try:
        cols = int(cols)
    except (TypeError, ValueError):
        return offset
    if cols < 1:
        return offset
    # One column: every pin is at x = 0, so the pitch plays no part and the
    # mirror only swaps the body margins. With more columns the span needs it.
    if cols > 1 and not pitch:
        return offset
    span = (cols - 1) * pitch if cols > 1 else 0.0
    return round(-(length - span + offset), 3)


#: Millimetres per unit, for the drawing units the schema accepts
#: (`drawing_unit`). Exact by definition: 1 inch = 25.4 mm, 1 mil = 0.001 inch.
_UNIT_TO_MM = {"mm": 1.0, "inch": 25.4, "mil": 0.0254}

_PACKAGE_LENGTH_KEYS = ("body_length_mm", "body_width_mm", "body_height_mm")
_PACKAGE_PIN_LENGTH_KEYS = ("x_mm", "y_mm", "pad_length_mm", "pad_width_mm", "drill_mm")
_COMPONENT_LENGTH_KEYS = ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")
_FILLET_LENGTH_KEYS = ("radius_mm",)

#: Views a contact drawing can be read from (`contacts_view`).
_CONTACT_VIEWS = ("top", "bottom")

#: Heuristic threshold, in mm, below which a pitch declared in millimetres is
#: reported as a probable unit error. DECLARED HEURISTIC, not a rule: the
#: finest centre-to-centre pitch of production packages is about 0.3 mm
#: (wafer-level chip-scale packages), so a smaller pitch describes no real
#: part, while an inch drawing read as millimetres gives 0.1, 0.05 or
#: 0.0197 for 2.54, 1.27 and 0.5 mm. The margin below 0.3 mm keeps every
#: real pitch clear of it. A body/pitch ratio would not help: it is
#: dimensionless, identical in both units. It never converts anything, it
#: only writes a doubt; a declared `drawing_unit` is what makes it
#: superfluous, and it would miss an inch drawing whose pitch is 0.3 inch or
#: more (7.62 mm), which reads as a plausible millimetre pitch.
_UNIT_DOUBT_PITCH_MM = 0.25


def _scale_lengths(entry, keys, factor: float) -> None:
    """Multiply the numeric length fields of one dict by `factor`, in place.

    Values that are not numbers are left untouched, so the validation that
    follows still sees (and reports) them exactly as the model sent them.
    """
    if not isinstance(entry, dict):
        return
    for key in keys:
        number = _optional_number(entry.get(key))
        if number is not None:
            entry[key] = round(number * factor, 6)


def _convert_units(data: dict, factor: float) -> None:
    """Convert every length of the answer to millimetres, in place."""
    _scale_lengths(data, _TOP_LENGTH_KEYS, factor)
    if isinstance(data.get("castellated_sides"), (list, tuple)):
        for side in data["castellated_sides"]:
            _scale_lengths(side, _SIDE_LENGTH_KEYS, factor)
    geometry = data.get("package_geometry")
    if isinstance(geometry, dict):
        _scale_lengths(geometry, _PACKAGE_LENGTH_KEYS, factor)
        if isinstance(geometry.get("pins"), (list, tuple)):
            for pin in geometry["pins"]:
                _scale_lengths(pin, _PACKAGE_PIN_LENGTH_KEYS, factor)
    for key, keys in (("components_3d", _COMPONENT_LENGTH_KEYS),
                      ("edge_fillets", _FILLET_LENGTH_KEYS)):
        if isinstance(data.get(key), (list, tuple)):
            for entry in data[key]:
                _scale_lengths(entry, keys, factor)


def _declared_pitches(data: dict, layout: str) -> list:
    """(label, pitch in mm) of every pitch the answer declares, for the unit doubt.

    Matrix pitch (with more than one column), per-side castellated pitches
    (with more than one contact) and the smallest centre distance between the
    explicit pads of package_geometry: all are centre-to-centre distances of
    real contacts, the quantity whose physical floor the heuristic uses.
    """
    pitches = []
    if layout == "matrix":
        cols = _optional_number(data.get("cols"), positive=True, integer=True)
        pitch = _optional_number(data.get("pitch_mm"), positive=True)
        if cols and cols > 1 and pitch:
            pitches.append(("pitch_mm", pitch))
    sides = data.get("castellated_sides")
    if layout == "castellated" and isinstance(sides, (list, tuple)):
        for side in sides:
            if not isinstance(side, dict):
                continue
            count = _optional_number(side.get("count"), positive=True, integer=True)
            pitch = _optional_number(side.get("pitch_mm"), positive=True)
            if count and count > 1 and pitch:
                pitches.append((f"castellated_sides[{side.get('edge')}].pitch_mm", pitch))
    geometry = data.get("package_geometry")
    if isinstance(geometry, dict) and isinstance(geometry.get("pins"), (list, tuple)):
        centres = []
        for pin in geometry["pins"]:
            if isinstance(pin, dict):
                x, y = _optional_number(pin.get("x_mm")), _optional_number(pin.get("y_mm"))
                if x is not None and y is not None:
                    centres.append((x, y))
        distances = [math.dist(a, b) for i, a in enumerate(centres) for b in centres[i + 1:]]
        distances = [d for d in distances if d > 0]
        if distances:
            pitches.append(("package_geometry", min(distances)))
    return pitches


def _unit_doubt(data: dict, layout: str) -> Optional[str]:
    """A doubt note when a pitch read as millimetres is below any real package.

    See `_UNIT_DOUBT_PITCH_MM` for why this is a declared heuristic.
    """
    suspicious = [(label, pitch) for label, pitch in _declared_pitches(data, layout)
                  if pitch < _UNIT_DOUBT_PITCH_MM]
    if not suspicious:
        return None
    label, pitch = min(suspicious, key=lambda item: item[1])
    return tx("DUBBIO unita' di misura: {label} vale {pitch:g} mm, meno del passo piu' fine "
              "dei package reali (circa 0.3 mm). Se il disegno e' in pollici varrebbe "
              "{inch:.4g} mm: verificare l'unita' del disegno e ripetere l'analisi. Nessuna "
              "conversione applicata.", label=label, pitch=pitch, inch=pitch * 25.4)


def _mirror_castellated_sides(sides) -> None:
    """Take castellated sides read on a BOTTOM VIEW to the view from above, in place.

    The bottom view is the part turned over about the vertical axis of the
    drawing: left and right swap, top and bottom stay. Along a vertical edge
    the order from top to bottom is unchanged; along a horizontal edge the
    order from left to right is reversed, so the numbering direction flips,
    the offset (positive to the right) changes sign and the silkscreen names,
    listed in the order met along the edge, are reversed with the contacts.
    """
    if not isinstance(sides, (list, tuple)):
        return
    for side in sides:
        if not isinstance(side, dict):
            continue
        edge = str(side.get("edge") or "").strip().lower()
        if edge in ("left", "right"):
            side["edge"] = "right" if edge == "left" else "left"
            continue
        if edge not in ("top", "bottom"):
            continue
        side["reverse"] = side.get("reverse") is not True
        offset = _optional_number(side.get("offset_mm"))
        if offset is not None:
            side["offset_mm"] = -offset + 0.0
        if isinstance(side.get("pin_names"), list):
            side["pin_names"] = list(reversed(side["pin_names"]))


def _mirrored_column_order(rows: int, cols: int) -> list:
    """Column-order numbering (pin_header.pin_positions) read on a bottom view, seen from top."""
    return [{"pin": col * rows + row + 1, "row": row, "col": cols - 1 - col}
            for col in range(cols) for row in range(rows)]


def _has_explicit_pads(data: dict) -> bool:
    """True when package_geometry carries pads, i.e. it is the footprint source."""
    geometry = data.get("package_geometry")
    return isinstance(geometry, dict) and bool(geometry.get("pins"))


def _canonicalize_answer(data: dict) -> None:
    """Bring a tool answer to millimetres and to the view from above, in place.

    The model reports the numbers and the contact layout as it reads them
    (`drawing_unit`, `contacts_view`); every conversion is done here, once,
    because it is deterministic geometry, not perception. Afterwards the
    answer says `drawing_unit="mm"` and, when the view was known,
    `contacts_view="top"`; the legacy `pin_map_view` is folded in and
    removed. What was done, and every doubt, is kept in `_canonical_notes`;
    `_canonical` marks the answer so that a second call does nothing: the
    photo stage and `_params_from_tool_arguments` read the same answer again.
    Invalid values are left for the validation to reject or report.
    """
    if data.get("_canonical"):
        return
    notes: list[str] = []
    layout = data.get("layout") or "matrix"

    unit = data.get("drawing_unit")
    if unit is None:
        unit = "mm"
    elif unit not in _UNIT_TO_MM:
        notes.append(tx("DUBBIO unita' di misura {unit!r} non riconosciuta: quote trattate come "
                        "millimetri, da verificare.", unit=unit))
        unit = "mm"
    if unit != "mm":
        _convert_units(data, _UNIT_TO_MM[unit])
        notes.append(tx("Quote lette in {unit} e convertite in millimetri (x {factor:g}).",
                        unit=unit, factor=_UNIT_TO_MM[unit]))
    else:
        doubt = _unit_doubt(data, layout)
        if doubt:
            notes.append(doubt)
    data["drawing_unit"] = "mm"

    view = data.get("contacts_view")
    if view is None:
        view = data.get("pin_map_view")
    view = view if view in _CONTACT_VIEWS else "unknown"
    data.pop("pin_map_view", None)
    reads_contacts = layout == "castellated" or (layout == "matrix" and not _has_explicit_pads(data))
    if view == "unknown" and reads_contacts:
        notes.append(tx("DUBBIO vista: il documento non dice se i contatti sono visti dall'alto o "
                        "dal basso; interpretati come vista dall'alto. Se il disegno era una "
                        "BOTTOM VIEW il footprint risulta specchiato: verificarlo sul disegno."))
    if layout == "castellated" and view == "bottom":
        _mirror_castellated_sides(data.get("castellated_sides"))
        notes.append(tx("Lati del modulo letti su una vista dal basso: specchiati nella vista "
                        "dall'alto del footprint."))
    if layout == "matrix":
        rows = _optional_number(data.get("rows"), positive=True, integer=True)
        cols = _optional_number(data.get("cols"), positive=True, integer=True)
        pitch = _optional_number(data.get("pitch_mm"), positive=True)
        if data.get("pin_map"):
            if cols is None:
                notes.append(tx("pin_map ignorata: la matrice non ha un numero di colonne valido."))
                data["pin_map"] = None
            else:
                try:
                    data["pin_map"] = _pin_map_seen_from_top(
                        data["pin_map"], cols, "bottom" if view == "bottom" else "top", rows)
                except ExtractionError as exc:
                    # Optional field: the paid response survives, but automatic
                    # numbering replaces the map, so the note must say it loudly.
                    notes.append(tx("ATTENZIONE pin_map scartata ({exc}): numerazione automatica "
                                    "colonna per colonna, da verificare sul pinout.", exc=exc))
                    data["pin_map"] = None
        if view == "bottom" and cols is not None:
            if not data.get("pin_map") and rows is not None:
                # Column order read on a bottom view is NOT column order seen
                # from above: the mirrored numbering needs an explicit map.
                data["pin_map"] = _mirrored_column_order(rows, cols)
            offset = _optional_number(data.get("body_offset_x_mm"))
            length = _optional_number(data.get("body_length_mm"), positive=True)
            mirrored = _offset_x_seen_from_top(offset, length, cols, pitch, "bottom")
            if offset is not None and mirrored is not None:
                data["body_offset_x_mm"] = mirrored
            notes.append(tx("Pin letti su una vista dal basso: colonne e spostamento del corpo "
                            "specchiati nella vista dall'alto del footprint."))
    if view in _CONTACT_VIEWS:
        data["contacts_view"] = "top"
    else:
        data["contacts_view"] = "unknown"
    data["_canonical_notes"] = notes
    data["_canonical"] = True


def _merge_photo_components(groups):
    """Compatibilita' per chiamanti che usano l'inventario in millimetri.

    The groups are inventories built by the program, so their internal flags
    are kept (trusted).
    """
    from .photo_inventory import merge_components
    return merge_components([_components_3d(g, trusted=True) or [] for g in groups])


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


#: Roles a document may carry (document manifest version 2). One file may
#: carry several at once: a datasheet page is often pinout, dimensioned top
#: view and land pattern together.
DOCUMENT_ROLE_KEYS = ("pinout", "top", "hires", "land_pattern", "side_view", "datasheet")
#: Views a complete manufacturer datasheet may contain. When a file is
#: declared `datasheet`, these views, if not declared on another file, are
#: not "missing": the model looks for them in the datasheet and reports the
#: sections it does not find. The photograph is never one of them.
DATASHEET_VIEWS = ("pinout", "top", "land_pattern", "side_view")
#: The role of the photographs of the real part: only files carrying it go to
#: the photo inventory (component recognition).
PHOTO_ROLE = "hires"

#: How each role is presented to the model: (name, instruction). Prompt text
#: (Italian, like every prompt), not a user-visible message; the names are
#: the labels of document_roles.ROLES, so the model and the user read the same
#: words (tests/test_extraction_prompts.py checks it).
_ROLE_PROMPTS = {
    "pinout": ("Pinout / disposizione contatti",
               "usalo per contatti, numerazione, funzioni dei pin e quote dei contatti. "
               "Non assumere che il disegno sia visto dall'alto: dichiara in contacts_view "
               "la vista che il documento indica."),
    "top": ("Immagine con quote",
            "disegno quotato visto dall'alto: usalo per le quote meccaniche in pianta -- "
            "lunghezza e larghezza del corpo, posizione dei contatti e delle file, "
            "spostamenti del corpo rispetto ai contatti."),
    "hires": ("Foto ad alta risoluzione",
              "fotografia del pezzo reale vista dall'alto: usala per cio' che si vede -- "
              "tipo di contatto sul bordo, colori, sigle serigrafate, spigoli arrotondati, "
              "componenti montati. Non e' un disegno quotato: una misura ricavata da qui e' "
              "una stima (\"estimated\" in value_sources)."),
    "land_pattern": ("Piazzole consigliate",
                     "piazzole consigliate (land pattern) sulla scheda che ospita il "
                     "componente: usalo per "
                     "dimensioni e posizioni di piazzole e fori (pad_diameter_mm, drill_mm, "
                     "pad_length_mm, pad_width_mm, package_geometry). Le piazzole non sono il "
                     "corpo: non ricavarne le quote body_*."),
    "side_view": ("Vista laterale / altezze",
                  "vista di profilo: usala per le quote in altezza -- body_height_mm, "
                  "pcb_thickness_mm, pin_length_3d_mm e altezze dei componenti. Lunghezze, "
                  "larghezze e passi in pianta non si leggono da questa vista."),
    "datasheet": ("Datasheet del costruttore",
                  "documento completo del costruttore: puo' contenere tabella dei pin e pinout, "
                  "disegni quotati visti dall'alto e di profilo, piazzole consigliate (land "
                  "pattern). Cerca ciascuna di queste sezioni e usala per il suo scopo, come se "
                  "fosse dichiarata a parte; dichiara in contacts_view la vista del disegno dei "
                  "contatti. Una sezione che non c'e' va segnalata in notes, non ricostruita. "
                  "Non e' una foto del pezzo: non ricavarne components_3d ne' colori."),
}


def _role_label(role: str) -> str:
    """User-visible label of a role: the document catalogue's, else the model-facing name."""
    from .document_roles import ROLES
    return ROLES.get(role) or _ROLE_PROMPTS[role][0]


def _declared_role_keys(value) -> tuple:
    """The known role keys in one declared value (a key or a collection of keys), tolerantly.

    Used where an unknown role must read as «not declared» instead of failing
    (the log line); `normalize_document_roles` is the strict version.
    """
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(key for key in DOCUMENT_ROLE_KEYS if key in value)


def normalize_document_roles(document_roles, file_names) -> Optional[dict[str, tuple]]:
    """@brief Validate the user-declared document roles and give every file its tuple of roles.

    @param document_roles None (no roles declared), or a mapping file name ->
        role key (legacy form, one role per file, as in manifest version 1)
        or file name -> list/tuple/set of role keys (manifest version 2).
        Role keys are those of DOCUMENT_ROLE_KEYS.
    @param file_names Names of the documents sent, without directories.
    @return None when `document_roles` is None; otherwise file name -> tuple
        of role keys, without repetitions, in DOCUMENT_ROLE_KEYS order.
    @throws ExtractionError When the mapping does not name exactly the files
        sent, a file has no role or an unknown one, or -- legacy form only --
        two files declare the same role (a version 1 manifest has one file
        per role).
    @note Legacy form only: when no file is declared a photograph ('hires'),
        the files declared 'top' also become photographs. That was the
        behaviour of version 1, where the dimensioned top view stood in for a
        missing photo, and it is the same migration the manifest applies
        (version 1 -> 2), so an old caller sees the same result. A version 2
        mapping is taken as declared: only files marked 'hires' are photos.
    """
    if document_roles is None:
        return None
    invalid = ExtractionError(tx("Ruoli documenti incompleti, duplicati o non validi."))
    names = list(file_names)
    if not isinstance(document_roles, dict) or set(document_roles) != set(names):
        raise invalid
    legacy = all(isinstance(value, str) for value in document_roles.values())
    if legacy and len(set(document_roles.values())) != len(names):
        raise invalid
    roles = {}
    for name, value in document_roles.items():
        keys = (value,) if isinstance(value, str) else value
        if (not isinstance(keys, (list, tuple, set, frozenset)) or not keys
                or any(not isinstance(key, str) or key not in DOCUMENT_ROLE_KEYS
                       for key in keys)):
            raise invalid
        roles[name] = _declared_role_keys(keys)
    if legacy and not any(PHOTO_ROLE in keys for keys in roles.values()):
        roles = {name: _declared_role_keys(keys + (PHOTO_ROLE,)) if "top" in keys else keys
                 for name, keys in roles.items()}
    return roles


def photo_document_indices(doc_paths, document_roles) -> Optional[list[int]]:
    """@brief Indices of the documents declared photographs, the only inputs of the photo inventory.

    @param doc_paths Documents in request order.
    @param document_roles Declared roles, in any form `normalize_document_roles`
        accepts, or None.
    @return None when no roles are declared (the model's `top_photo_index`
        decides); otherwise the indices of the files carrying PHOTO_ROLE, in
        request order, or [-1] when none does (no other document is used in
        their place).
    @throws ExtractionError When the roles are invalid (see
        `normalize_document_roles`).
    """
    names = [Path(path).name for path in doc_paths]
    roles = normalize_document_roles(document_roles, names)
    if roles is None:
        return None
    return [index for index, name in enumerate(names) if PHOTO_ROLE in roles[name]] or [-1]


def _role_description(keys) -> str:
    """How one file and its declared roles are introduced to the model."""
    names = " + ".join(_ROLE_PROMPTS[key][0] for key in keys)
    lead = "RUOLO DICHIARATO DALL’UTENTE" if len(keys) == 1 else "RUOLI DICHIARATI DALL’UTENTE"
    lines = [f"{lead}: {names}."]
    lines += [f"- {_ROLE_PROMPTS[key][0]}: {_ROLE_PROMPTS[key][1]}" for key in keys]
    if len(keys) > 1:
        lines.append("Lo stesso file vale per tutti questi ruoli: leggi da ogni parte del "
                     "documento le informazioni del ruolo corrispondente.")
    if "pinout" in keys and "top" not in keys and "datasheet" not in keys:
        # Only when the SAME file is not also the dimensioned top view: a
        # datasheet page that is both must be read for both.
        lines.append("Questo file non e' dichiarato disegno quotato dall'alto: non usarlo "
                     "per gli ingombri del corpo.")
    return "\n".join(lines)


def _files_sent_message(doc_paths, document_roles: Optional[dict] = None) -> str:
    """@brief Name the documents about to travel in the paid analysis request.

    @param doc_paths Documents in request order; only their file names are
        reported, because the directories say nothing about what is being sent
        and would take the room the names and the roles need.
    @param document_roles Optional file name -> role key, or file name ->
        collection of role keys, as declared by the user. A name missing from
        it, or without any known role, is reported as undeclared: the role is
        never guessed from the file name. Several roles of one file are listed
        joined by « + ».
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
        keys = _declared_role_keys((document_roles or {}).get(name))
        label = ' + '.join(tx(_role_label(key)) for key in keys)
        described.append(f'{_log_name(name)} — {label}' if label
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
    crop_prompt: Optional[str] = None,
    document_roles: Optional[dict] = None,
    component_family: Optional[str] = None,
    photo_detector: str = "ai",
    yolo_model_path: Optional[Path] = None,
    yolo_confidence: Optional[float] = None,
    yolo_settings: Optional[dict] = None,
    phase: Optional[Callable[..., None]] = None,
    review_proposals: Optional[Callable[..., object]] = None,
    cancel: Optional[object] = None,
) -> ExtractedParams:
    """@brief Extract dimensions and pinout, optionally using local YOLO photo proposals.

    @param cancel Optional cancellation channel: a threading.Event (or any
        object with is_set()) or a zero-argument callable returning True when
        the user asked to stop. It is checked before EVERY paid call, retries
        and the photo inventory included, and during retry backoff waits.
        When set, ExtractionCancelled is raised and no further call is paid.
        None (default) keeps the previous behaviour.
    @param document_roles None, or the roles the user declared for each file:
        file name -> list of role keys among DOCUMENT_ROLE_KEYS ('pinout',
        'top', 'hires', 'land_pattern', 'side_view'; one file may carry
        several), or the legacy file name -> single role key. Validated
        before any paid call (see `normalize_document_roles`); only the files
        carrying 'hires' go to the photo inventory (`photo_document_indices`).

    @param crop_prompt Instructions of the per-body crop reading (profile
        field 'crop_prompt'). Used only with photo_detector 'yolo' or 'vlm',
        the paths that run photo_crop_reading.read_bodies; None or blank sends
        its factory default.
    @param photo_detector Photo localization backend: ai (legacy), yolo (local
        proposals plus one AI reconciliation) or vlm (no local detector: the
        vision model enumerates the bodies in that same single request).
    @param yolo_model_path Optional compatible ONNX file; validated before paid calls.
    @param yolo_confidence Operating confidence threshold of the local YOLO
        detector; None keeps its default (photo_yolo.operating_confidence).
    @param yolo_settings Other local-detector settings (photo_yolo.yolo_settings);
        None keeps the defaults. Validated before any paid call.
    @param phase Optional callable(tool, detail) telling the caller WHICH tool
        is working, so a progress modal can distinguish a paid remote call
        ('llm' for the documents, 'llm_inventory' for the board photo) from
        local detection ('yolo'). It is forwarded to the photo
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
    risposta senza la chiamata allo strumento. A main answer rejected by the
    validation keeps its content in `ExtractionError.raw_response` and in
    the AI log.
    """
    # Il consuntivo riguarda questa elaborazione: azzerarlo PRIMA di ogni
    # controllo, cosi' anche un rifiuto in validazione (YOLO, ruoli, file)
    # non lascia mostrare il costo della corsa precedente come se fosse suo.
    reset_usage()
    if not doc_paths:
        raise ExtractionError(tx("Nessun file selezionato."))
    if photo_detector not in ("ai", "yolo", "vlm"):
        raise ExtractionError(tx("Rilevatore fotografico non supportato."))
    if photo_detector == "yolo":
        from .photo_yolo import operating_confidence, validate_yolo_setup
        from .photo_yolo import yolo_settings as check_yolo_settings
        try:
            # Anche soglia e parametri si controllano qui, prima di pagare.
            operating_confidence(yolo_confidence)
            check_yolo_settings(yolo_settings)
            validate_yolo_setup(yolo_model_path)
        except (ValueError, OSError, ImportError) as exc:
            raise ExtractionError(tx("YOLO locale non disponibile: {exc}", exc=exc)) from exc
    from .ai_diagnostics import make_logged_call
    # L'analisi applicativa deve essere auditabile: il log persistente conserva
    # il testo esatto dei prompt/tool e gli hash delle immagini (mai i byte).
    cancelled = _cancel_predicate(cancel)

    def check_cancel():
        """Raise ExtractionCancelled when the user asked to stop."""
        if cancelled is not None and cancelled():
            raise ExtractionCancelled(tx("Estrazione annullata dall'utente: nessuna altra "
                                         "chiamata a pagamento."))

    diagnostic_call = make_logged_call(call_tool, progress, log_prompt=True,
                                       cancel=check_cancel if cancelled is not None else None)
    if len(doc_paths) > MAX_FILES:
        raise ExtractionError(tx("Troppi file selezionati (max {MAX_FILES} per chiamata).", MAX_FILES=MAX_FILES))
    # Legacy strings and lists alike become tuples of roles, checked before
    # anything is paid; from here on only the normalized form travels.
    document_roles = normalize_document_roles(document_roles, [p.name for p in doc_paths])

    check_cancel()
    try:
        # Il credito va verificato qui, prima del primo blocco di contenuto:
        # l'estrazione e' una catena di decine di chiamate (analisi principale,
        # crop, verifica, terminali) e restare a secco a meta' strada significa
        # aver speso senza ottenere un componente. Si verifica ogni fornitore
        # dei modelli usati (OpenRouter e/o DeepSeek diretto, 27/09).
        # The EFFECTIVE models, defaults applied exactly as the run applies
        # them below: a DeepSeek main model with no explicit vision model
        # still calls the default vision model, whose provider must be ready.
        from .photo_grounding import DEFAULT_VISION_MODEL
        from .providers import ensure_providers_ready

        def openrouter_ready(report):
            """OpenRouter key and credit, through this module's references."""
            api_key()
            check_credit(progress=report)

        ensure_providers_ready(
            [model or DEFAULT_MODEL, vision_model or DEFAULT_VISION_MODEL,
             verification_model or model or DEFAULT_MODEL],
            progress, openrouter_check=openrouter_ready)
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
            description += "\n" + _role_description(document_roles[path.name])
        content_blocks.append({"type": "text", "text": description})
        content_blocks.append(_file_to_content_block(path))
    if component_family == CASTELLATED_FAMILY and document_roles is not None:
        # Il numero dei contatti lo riconosce l'AI in QUESTA chiamata (utente,
        # 28/09, punto 142): i bordi della foto ingranditi, non una correzione dopo.
        strips = _edge_strip_blocks(doc_paths, document_roles)
        content_blocks.extend(strips)
        if strips and progress:
            progress(tx("Allegati all'AI gli ingrandimenti dei bordi della foto, per contare i "
                        "contatti fisici: {count} immagini.",
                        count=sum(1 for block in strips if block.get("type") == "image_url")))
    request = ("Estrai i parametri meccanici del componente da questi documenti e chiama lo "
               "strumento.")
    if document_roles is not None:
        declared = {key for keys in document_roles.values() for key in keys}
        # A datasheet is a container: the views it may hold are looked for in
        # it, not declared missing. The datasheet role itself is optional.
        in_datasheet = set(DATASHEET_VIEWS) if "datasheet" in declared else set()
        missing = [_ROLE_PROMPTS[key][0] for key in DOCUMENT_ROLE_KEYS
                   if key not in declared and key not in in_datasheet and key != "datasheet"]
        to_search = [_ROLE_PROMPTS[key][0] for key in DATASHEET_VIEWS
                     if key in in_datasheet and key not in declared]
        request += ('\nI ruoli dei file sono espliciti: rispettali, non dedurli dai nomi originali. '
                    'Un file con piu\' ruoli vale per ciascuno di essi. '
                    'Segnala dati mancanti e incongruenze, senza inventare quote per completare le viste assenti.')
        if missing:
            request += '\nViste non fornite: ' + ', '.join(missing) + '.'
        if to_search:
            request += ('\nViste da cercare nel datasheet (non dichiarate in altri file): '
                        + ', '.join(to_search) + '.')
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
        error = ExtractionError(tx("Risposta AI non strutturata."))
        _log_rejected_response(data, error, progress)
        raise error
    raw_main = copy.deepcopy(data)
    try:
        _validate_main_response(data, content_blocks, diagnostic_call, model, system_prompt,
                                has_pdf, progress)
    except ExtractionCancelled:
        raise
    except ExtractionError as exc:
        _log_rejected_response(raw_main, exc, progress)
        raise
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        # A shape no rule above anticipated: still an application error with
        # the answer preserved, never a raw exception out of the extraction.
        error = ExtractionError(tx("Risposta AI non valida: {exc}", exc=exc))
        _log_rejected_response(raw_main, error, progress)
        raise error from exc

    if progress:
        progress(
            tx("Analisi principale completata: layout={get}, componenti "
               "iniziali={n_items}.",
               get=data.get('layout', 'matrix'),
               n_items=len(data.get('components_3d') or []))
        )
    return _extract_photo_stage(
        data, doc_paths, progress=progress, announce=announce, check_cancel=check_cancel,
        document_roles=document_roles, component_family=component_family,
        photo_detector=photo_detector, vision_model=vision_model, model=model,
        verification_model=verification_model, diagnostic_call=diagnostic_call,
        extra_instructions=extra_instructions, photo_cache_dir=photo_cache_dir,
        grounding_prompt=grounding_prompt, verification_prompt=verification_prompt,
        terminal_prompt=terminal_prompt, crop_prompt=crop_prompt,
        yolo_model_path=yolo_model_path,
        review_proposals=review_proposals, yolo_confidence=yolo_confidence,
        yolo_settings=yolo_settings,
        recount_contacts=lambda disagreements: _recount_sides_from_photo(
            data, disagreements, content_blocks, diagnostic_call, model, system_prompt,
            has_pdf, progress))


#: Fields of a castellated side a photo recount may change: the number of its
#: contacts and how they are named and numbered. Pitch, offset, contact style
#: and pad sizes stay those of the main answer: the recount is asked about the
#: COUNT (HANDOFF point 139, G2 a), and a geometry changed on the way would be
#: adopted unchecked. (value_sources cover only lengths, so the provenance of
#: the side stays valid.)
_RECOUNT_FIELDS = ("count", "pin_names", "first_pin", "reverse")


#: Private mark set on each side of a recount reply before canonicalization:
#: True when the model wrote `reverse` itself (a boolean). The mirror of a
#: bottom view writes `reverse` on every horizontal side, so after
#: canonicalization its presence no longer says whether the model stated it.
_REVERSE_STATED = "_reverse_stated"


def _recounted_side(old: dict, reply: dict, count: int) -> dict:
    """The declared side with only the _RECOUNT_FIELDS taken from the reply.

    `first_pin` stays the old one when the reply gives none; `reverse` is the
    reply's only when the model stated it (_REVERSE_STATED), otherwise the
    declared one is kept: a reply that omits it says nothing about the
    numbering direction (HANDOFF point 139, R-M1); `pin_names` are the
    reply's, or none.
    """
    side = copy.deepcopy(old)
    side["count"] = count
    names = reply.get("pin_names")
    if isinstance(names, (list, tuple)) and names:
        side["pin_names"] = [str(name) for name in names]
    else:
        side.pop("pin_names", None)
    first = _optional_number(reply.get("first_pin"), positive=True, integer=True)
    if first is not None:
        side["first_pin"] = first
    if reply.get(_REVERSE_STATED) and isinstance(reply.get("reverse"), bool):
        if reply["reverse"]:
            side["reverse"] = True
        else:
            side.pop("reverse", None)
    return side


def _recount_refusal(side: dict, old: dict, reply: dict, item, data: dict) -> Optional[str]:
    """Why a recounted side cannot replace the declared one, or None when it can.

    Structural checks, each for a failure reproduced in the audit (HANDOFF
    points 138-139): names of another length than the count (they would be
    dropped in silence later), a side whose names the recount dropped, a side
    contradicting itself (_edge_contradictions), and -- the check that breaks
    the circle «the model now says what the photo said» -- contact centres
    that do not fall on the notch centres measured on the photo.

    The centres come from the geometry the footprint uses
    (contact_zones.contact_positions: the new count with the DECLARED pitch
    and offset). They are compared RELATIVELY (HANDOFF point 139, R-G2): the
    photo measures from the crop box, whose edge may sit off the board corner
    by the tilt find_board accepts, so the whole row may be shifted by the
    calibration alone. One common shift is allowed, up to
    EdgeCount.shift_bound_mm (edge_notches.row_shift_bound_mm, derived from
    the calibration contract; None = no shift allowed); after removing it,
    every centre must match within the photo's own tolerance
    (EdgeCount.tolerance_mm), so a row of another spacing is refused. A count
    that equals the photo's but whose row sits a notch off (half a pitch or
    more, e.g. the model dropped a contact hidden at an end of the row) is
    refused because its shift exceeds the bound.
    """
    count = side["count"]
    names = reply.get("pin_names")
    if isinstance(names, (list, tuple)) and names and len(names) != count:
        return tx("{n_names} nomi per {count} contatti", n_names=len(names), count=count)
    if old.get("pin_names") and not side.get("pin_names"):
        return tx("il ricontrollo non elenca i nomi dei contatti")
    length = _optional_number(data.get("body_length_mm"), positive=True)
    width = _optional_number(data.get("body_width_mm"), positive=True)
    edge = _side_edge(side)
    if any(problem_edge == edge for problem_edge, _ in _edge_contradictions([side], length, width)):
        return tx("il lato ricontato si contraddice")
    if length is None or width is None:
        return tx("quote della scheda mancanti")
    from .contact_zones import contact_positions
    from .edge_notches import row_offset_from
    along = "y_mm" if edge in ("left", "right") else "x_mm"
    start = width / 2 if edge in ("left", "right") else length / 2
    centres = sorted(row[along] + start
                     for row in contact_positions({"castellated_sides": [side]}, length, width)
                     if row.get("edge") == edge)
    measured = sorted(float(c) for c in (getattr(item, "centres_mm", None) or ()))

    def positive(value):
        if isinstance(value, bool) or not isinstance(value, (int, float))                 or not math.isfinite(value) or not value > 0:
            return 0.0
        return float(value)

    tolerance = positive(getattr(item, "tolerance_mm", None))
    bound = positive(getattr(item, "shift_bound_mm", None))
    if len(centres) != len(measured) or row_offset_from(centres, measured, tolerance,
                                                        float("inf")) is None:
        return tx("i contatti ricontati non cadono sui semi-fori misurati sulla foto "
                  "(centri {declared} mm contro {measured} mm)",
                  declared=", ".join(f"{c:.2f}" for c in centres),
                  measured=", ".join(f"{c:.2f}" for c in measured))
    shift = row_offset_from(centres, measured, tolerance, float("inf"))
    if abs(shift) > bound:
        return tx("la fila ricontata e' spostata di {shift:.2f} mm rispetto ai semi-fori "
                  "misurati, piu' di quanto la calibrazione della foto possa spostarla "
                  "({bound:.2f} mm)", shift=shift, bound=bound)
    return None


def _recount_sides_from_photo(data, disagreements, content_blocks, diagnostic_call, model,
                              system_prompt, has_pdf, progress) -> bool:
    """One paid recount of the sides whose half-holes the photo counts differently.

    User decision of 2026-09-28 (HANDOFF point 137): when the calibrated top
    photo shows another number of half-holes than a declared side, the model
    is asked once, with the same documents, to recount THOSE sides, and is
    told what the photo measured (count and notch centres).

    Adoption (HANDOFF point 139, G2), per disagreeing side:
    - only the recount fields change (_RECOUNT_FIELDS): count, pin_names,
      first_pin, reverse; pitch, offset, contact style, pad sizes and their
      provenance stay those of the main answer;
    - the recounted count must equal the photo's and the recounted contacts
      must sit on the measured notch centres (see _recount_refusal);
    - with that side replaced, the pad numbering of ALL sides must stay valid
      and contiguous (_castellated_sides, _numbering_is_contiguous);
    - sides the photo did not dispute are never changed, whatever the reply
      says about them.
    A side that fails keeps its declared contacts, the photo check keeps
    reporting the disagreement, and the log says why.

    @param data Validated main answer, in millimetres and seen from above
        (after `_canonicalize_answer`); updated in place.
    @param disagreements edge_notches.EdgeCount list (edge, found, declared,
        centres_mm, tolerance_mm).
    @return True when at least one side was replaced.
    @note The recount answer is canonicalized like the main one, so a model
        that answers in another unit or view is converted, not misread; the
        notes of that conversion are logged and, when a side is adopted, kept
        with the answer's own (`_canonical_notes`). A reply that does not say
        its view is read as the top view the request asks for, so no «DUBBIO
        vista» note is made up for it; a view it states but that is not
        recognized still gives that note.
    @note Failures are contained (HANDOFF point 139, minors): any exception of
        the call or of reading the reply (OpenRouterError, a malformed reply
        raising KeyError/ValueError, a truncated transfer...) is logged and
        the answer is kept unchanged -- the photo inventory goes on. Only a
        cancellation (CallCancelled, hence ExtractionCancelled) propagates.
    """
    say = progress or (lambda _message: None)
    try:
        return _recount_sides_attempt(data, disagreements, content_blocks, diagnostic_call,
                                      model, system_prompt, has_pdf, say)
    except CallCancelled:
        raise
    except Exception as exc:  # contenuto: vedi la @note sopra
        detail = str(exc) if isinstance(exc, OpenRouterError) else f"{type(exc).__name__}: {exc}"
        say(tx("Ricontrollo dei contatti non riuscito ({exc}): restano quelli dell'analisi.",
               exc=detail))
        return False


def _recount_sides_attempt(data, disagreements, content_blocks, diagnostic_call, model,
                           system_prompt, has_pdf, say) -> bool:
    """Body of _recount_sides_from_photo; `data` is changed only at the very end."""
    facts = "; ".join(
        f"lato {item.edge}: la foto calibrata vista dall'alto mostra {item.found} semi-fori "
        f"aperti sul bordo, con i centri a {', '.join(f'{c:.2f}' for c in item.centres_mm)} mm "
        f"dall'inizio del bordo (da sinistra per top/bottom, dall'alto per left/right); "
        f"la risposta ne dichiarava {item.declared}"
        for item in disagreements)
    summary = ", ".join(f"{EDGE_LABELS.get(item.edge, item.edge)} {item.declared} -> "
                        f"{item.found}" for item in disagreements)
    say(tx("Contatti: la foto non conferma l'analisi ({summary}). Richiesta di ricontarli sugli "
           "stessi documenti: una chiamata in piu'.", summary=summary))
    blocks = list(content_blocks) + [{"type": "text", "text": (
        "Confronto con la fotografia: " + facts + ". Riconta sui documenti (pinout, disegno "
        "quotato, foto) i contatti FISICI di questi lati: count e' il loro numero, pin_names "
        "ne elenca esattamente uno per contatto nell'ordine lungo il bordo; le sigle dei LED "
        "o di altri componenti non sono contatti. La foto puo' sbagliare solo se un semi-foro "
        "e' coperto: se i documenti mostrano un altro numero, tienilo e spiegalo in notes. "
        "Restituisci la risposta completa con i lati visti dall'alto (contacts_view=\"top\") "
        "e le quote in millimetri (drawing_unit=\"mm\"). Lati attuali, visti dall'alto e in "
        f"mm: {data.get('castellated_sides')!r}")}]
    repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                               system_prompt=system_prompt or _SYSTEM_PROMPT,
                               content_blocks=blocks, tool=_openai_tool(),
                               max_tokens=16384, has_pdf=has_pdf)
    if not isinstance(repaired, dict) or not isinstance(repaired.get("castellated_sides"), list):
        say(tx("Il ricontrollo dei contatti non ha restituito lati utilizzabili: restano "
               "quelli dell'analisi."))
        return False
    repaired = copy.deepcopy(repaired)
    repaired["layout"] = "castellated"
    # Chiavi private: una risposta del modello non e' mai gia' convertita.
    repaired.pop("_canonical", None)
    repaired.pop("_canonical_notes", None)
    if repaired.get("contacts_view") is None and repaired.get("pin_map_view") is None:
        # La richiesta chiede la vista dall'alto: una risposta che non dice la
        # vista si legge cosi', senza il DUBBIO della risposta principale.
        repaired["contacts_view"] = "top"
    for entry in repaired["castellated_sides"]:
        if isinstance(entry, dict):
            entry[_REVERSE_STATED] = isinstance(entry.get("reverse"), bool)
    _canonicalize_answer(repaired)
    conversion = [str(note) for note in repaired.get("_canonical_notes") or ()]
    for note in conversion:
        say(tx("Ricontrollo dei contatti: {note}", note=note))
    edges = [_side_edge(entry) for entry in repaired["castellated_sides"] if _side_edge(entry)]
    # Un lato elencato due volte non dice quale conteggio valga: non si adotta.
    recounted = {_side_edge(entry): entry for entry in repaired["castellated_sides"]
                 if _side_edge(entry) and edges.count(_side_edge(entry)) == 1}
    merged = list(data.get("castellated_sides") or ())
    index = {_side_edge(entry): i for i, entry in enumerate(merged) if _side_edge(entry)}
    length = _optional_number(data.get("body_length_mm"), positive=True)
    width = _optional_number(data.get("body_width_mm"), positive=True)
    adopted, confirmed = [], []
    for item in disagreements:
        reply, position = recounted.get(item.edge), index.get(item.edge)
        count = (_optional_number(reply.get("count"), positive=True, integer=True)
                 if isinstance(reply, dict) else None)
        if position is None or count != item.found:
            confirmed.append(item.edge)
            continue
        old = merged[position]
        side = _recounted_side(old, reply, count)
        reason = _recount_refusal(side, old, reply, item, data)
        if reason is None:
            trial = merged[:position] + [side] + merged[position + 1:]
            try:
                _castellated_sides(copy.deepcopy(trial), body_length=length, body_width=width)
            except ExtractionError as exc:
                reason = tx("numerazione dei pad incoerente ({exc})", exc=exc)
            else:
                if not _numbering_is_contiguous(copy.deepcopy(trial)):
                    reason = tx("la numerazione dei pad di tutti i lati avrebbe un buco")
        if reason is not None:
            say(tx("Ricontrollo dei contatti non adottato per il lato {label}: {reason}; "
                   "restano quelli dell'analisi.",
                   label=EDGE_LABELS.get(item.edge, item.edge), reason=reason))
            continue
        merged[position] = side
        adopted.append(item.edge)
    if confirmed and not adopted:
        say(tx("Ricontrollo dei contatti: l'AI conferma i contatti dichiarati; il disaccordo "
               "con la foto resta da verificare."))
    if not adopted:
        return False
    data["castellated_sides"] = merged
    if conversion:
        data["_canonical_notes"] = list(data.get("_canonical_notes") or ()) + [
            tx("Ricontrollo dei contatti: {note}", note=note) for note in conversion]
    note = tx("Contatti ricontati dopo il confronto con la foto: {edges}. Verificare la "
              "tabella pinout prima dell'uso.",
              edges=", ".join(EDGE_LABELS.get(edge, edge) for edge in adopted))
    data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
    say(note)
    return True


def _side_contradictions(raw, body_length, body_width) -> list[str]:
    """Self-contradictions of the castellated sides of ONE answer, for the model.

    Structural, not tuned: a side cannot hold a different number of contacts
    than the names it lists for them, and (count - 1) * pitch contact centres
    cannot run past the edge they sit on. Both sides of each comparison come
    from the same answer, in its own drawing unit. Sides the normalization
    would discard are left to it.
    """
    return [problem for _edge, problem in _edge_contradictions(raw, body_length, body_width)]


def _edge_contradictions(raw, body_length, body_width) -> list[tuple[str, str]]:
    """Same checks as _side_contradictions, each problem paired with its edge."""
    try:
        sides = _normalized_castellated_sides(raw)
    except ExtractionError:
        return []
    names = {}
    for entry in raw:
        if (isinstance(entry, dict) and isinstance(entry.get("pin_names"), (list, tuple))
                and entry["pin_names"]):
            names[str(entry.get("edge") or "").strip().lower()] = len(entry["pin_names"])
    problems = []
    for side in sides:
        edge, count = side["edge"], side["count"]
        if names.get(edge, count) != count:
            problems.append((edge, f"lato {edge}: count={count} ma pin_names ha "
                                   f"{names[edge]} voci"))
        edge_length = body_width if edge in ("left", "right") else body_length
        span = (count - 1) * side["pitch_mm"]
        if edge_length is not None and span > edge_length + _PIN_FIELD_TOLERANCE_MM:
            problems.append((edge, f"lato {edge}: (count-1) x pitch_mm = ({count}-1) x "
                                   f"{side['pitch_mm']:g} = {span:.2f}, piu' del bordo lungo "
                                   f"{edge_length:g}"))
    return problems


def _numbering_is_contiguous(raw) -> bool:
    """True when the sides, numbered exactly as the extraction will number them, leave no gap.

    Judged on the output of _castellated_sides (discarded entries dropped,
    missing first_pin assigned), so what is checked is what will be drawn.
    A list that numbering refuses (overlap, repeated edge, nothing usable) is
    not contiguous. Ranges are compared as intervals, never materialized.
    """
    try:
        sides = _castellated_sides(raw)
    except ExtractionError:
        return False
    ranges = sorted((int(side["first_pin"]), int(side["first_pin"]) + side["count"])
                    for side in sides)
    return all(stop == start for (_a, stop), (start, _b) in zip(ranges, ranges[1:]))


def _side_edge(entry) -> str:
    """Edge name of one raw castellated side, as _side_contradictions keys it."""
    return str(entry.get("edge") or "").strip().lower() if isinstance(entry, dict) else ""


def _recount_contradictory_sides(data, content_blocks, diagnostic_call, model, system_prompt,
                                 has_pdf, progress, dims) -> None:
    """One paid recount of castellated sides that contradict themselves (user, 2026-09-26).

    The model is not deterministic: the same documents once gave 22 contacts
    per side, once 28 with 22 names on a 63.3 mm edge. Failing there wasted the
    whole analysis; guessing the count from the names would not be safer (the
    two sides listed 22 and 23). The contradiction is sent back with the same
    documents, its exact numbers and the edge lengths; the recounted sides
    replace the old ones, provenance included. Whatever is still wrong is
    handled as before: contacts that do not fit are an error, names of the
    wrong length are dropped with a note. At most one extra call.

    Only the CONTRADICTED edges are replaced. The recount returns every side,
    and a side that was coherent is not made more reliable by being answered
    twice: on a real run one edge failed by 0.04 mm and the recount also
    changed the coherent opposite edge from 6 to 5 contacts, unchecked.
    Changes the recount proposes for coherent edges are reported, not adopted
    -- unless the merged pad numbering would have a gap or an overlap
    (first_pin of one edge depends on the counts of the others): then the
    recount is adopted whole, as before, and the warning says so. A recount
    that lists an edge twice is also passed whole, so the normalization
    refuses it as it always did.
    """
    contradicted = _edge_contradictions(data.get("castellated_sides"), **dims)
    if not contradicted:
        return
    problems = [problem for _edge, problem in contradicted]
    wrong_edges = {edge for edge, _problem in contradicted}
    summary = "; ".join(problems)
    if progress:
        progress(tx("Risposta AI incoerente sui contatti ({summary}). Richiesta di ricontarli "
                    "sugli stessi documenti: una chiamata in piu'.", summary=summary))
    unit = data.get("drawing_unit") or "mm"
    repair_blocks = list(content_blocks) + [{"type": "text", "text": (
        "La risposta precedente si contraddice sui contatti dei lati castellated: "
        f"{summary}. Riconta sui documenti (pinout e disegno quotato) i contatti FISICI di "
        "ogni lato: count e' il loro numero, pin_names ne elenca esattamente uno per "
        "contatto nell'ordine lungo il bordo, e (count-1) x pitch_mm deve stare nel bordo "
        f"(body_length_mm={dims['body_length']} per i lati top e bottom, "
        f"body_width_mm={dims['body_width']} per left e right). Usa la stessa unita' della "
        f"risposta precedente (drawing_unit={unit}). Non inventare valori che i documenti "
        "non danno: se non puoi stabilirli, ometti i nomi e spiegalo in notes. Restituisci "
        f"la risposta completa. Lati precedenti: {data.get('castellated_sides')!r}"
    )}]
    try:
        repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                                   system_prompt=system_prompt or _SYSTEM_PROMPT,
                                   content_blocks=repair_blocks, tool=_openai_tool(),
                                   max_tokens=16384, has_pdf=has_pdf)
    except OpenRouterError as error:
        raise ExtractionError(tx("Ricontrollo dei contatti non riuscito; analisi foto non "
                                 "avviata.")) from error
    sides = repaired.get("castellated_sides") if isinstance(repaired, dict) else None
    repaired_unit = repaired.get("drawing_unit") if isinstance(repaired, dict) else None
    if not isinstance(sides, list) or not sides or repaired_unit not in (None, unit):
        # Lati mancanti o in un'altra unita': non confrontabili con corpo e
        # passo della risposta principale, che restano quelli.
        raise ExtractionError(tx("Il ricontrollo dei contatti non ha restituito lati "
                                 "utilizzabili; analisi foto non avviata."))
    recounted_edges = [_side_edge(entry) for entry in sides if _side_edge(entry)]
    recounted = {_side_edge(entry): entry for entry in sides if _side_edge(entry)}
    previous = data.get("castellated_sides")
    previous = list(previous) if isinstance(previous, list) else []
    before = {_side_edge(entry): entry for entry in previous if _side_edge(entry)}
    # Coherent edges the recount would change: normally said, not adopted.
    changed = sorted(edge for edge, entry in recounted.items()
                     if edge not in wrong_edges and (edge not in before or any(
                         entry.get(key) != before[edge].get(key)
                         for key in ("count", "pitch_mm", "first_pin", "reverse"))))
    merged = [recounted.get(_side_edge(entry), entry) if _side_edge(entry) in wrong_edges
              else entry for entry in previous]
    # A contradicted edge missing from the first answer can only come from the recount.
    merged.extend(entry for edge, entry in recounted.items()
                  if edge in wrong_edges and edge not in before)
    whole = (len(recounted_edges) != len(set(recounted_edges))
             or not _numbering_is_contiguous(merged))
    # The recount as a whole, as before this rule, in two cases: it lists an
    # edge twice (the normalization then refuses it with its own message,
    # instead of a silent «last one wins»), or the pad numbering of the merged
    # sides has a gap or an overlap -- first_pin of one edge depends on the
    # counts of the others, so mixing two answers can silently lose a pad.
    data["castellated_sides"] = sides if whole else merged
    warning = tx("Contatti dei lati ricontati dopo una risposta AI incoerente ({summary}); "
                 "verificare conteggi e nomi prima dell'utilizzo.", summary=summary)
    if changed and whole:
        warning += " " + tx("Il ricontrollo cambiava anche lati che erano coerenti ({edges}) e la "
                            "numerazione dei contatti dipende da tutti i lati: adottato il "
                            "riconteggio intero, verificare anche quei lati.",
                            edges=", ".join(changed))
    elif changed:
        warning += " " + tx("Il ricontrollo cambiava anche lati che erano coerenti ({edges}): "
                            "modifica non adottata, restano quelli della prima risposta.",
                            edges=", ".join(changed))
    data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
    if progress:
        progress(warning)


def _cancel_predicate(cancel):
    """Normalize the cancellation channel to a zero-argument predicate, or None."""
    if cancel is None:
        return None
    if hasattr(cancel, "is_set"):
        return cancel.is_set
    if callable(cancel):
        return lambda: bool(cancel())
    raise ExtractionError(tx("Canale di annullamento non valido."))


def _validate_main_response(data, content_blocks, diagnostic_call, model, system_prompt,
                            has_pdf, progress) -> None:
    """Sanitize and validate the main answer in place; ExtractionError when unusable.

    Body dimensions are sanitized with a note; castellated sides that
    contradict themselves (count against pin_names, contacts longer than
    their edge) get one paid recount, numbering conflicts get one paid
    repair, and the complete answer must then build ExtractedParams.
    """
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
    dims = dict(body_length=data.get("body_length_mm"), body_width=data.get("body_width_mm"))
    if data.get("layout") == "castellated":
        _recount_contradictory_sides(data, content_blocks, diagnostic_call, model,
                                     system_prompt, has_pdf, progress, dims)
        try:
            _castellated_sides(data.get("castellated_sides"), **dims)
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
                f"Conflitto: {end_sentence(exc)} Lati precedenti: {data.get('castellated_sides')!r}"
            )}]
            try:
                repaired = diagnostic_call(model=model or DEFAULT_MODEL,
                    system_prompt=system_prompt or _SYSTEM_PROMPT,
                    content_blocks=repair_blocks, tool=_openai_tool(),
                    max_tokens=16384, has_pdf=has_pdf)
                corrected = _castellated_sides(repaired.get("castellated_sides"), **dims)
                # Normalized against normalized: the raw original may hold
                # entries the normalization drops, or edges written
                # differently, which are not changes made by the correction.
                original = _normalized_castellated_sides(data["castellated_sides"])
                if len(corrected) != len(original) or any(
                    row["edge"] != old["edge"] or row["count"] != old["count"]
                    for row, old in zip(corrected, original)
                ):
                    raise ExtractionError(tx("La correzione della numerazione ha cambiato i lati o i conteggi."))
                # The normalized sides drop the declared provenance: carry it
                # over by edge (edges are unique), or every value of the
                # repaired sides would read as undeclared.
                provenance = {str(entry.get("edge") or "").strip().lower(): entry["value_sources"]
                              for entry in data["castellated_sides"]
                              if isinstance(entry, dict) and "value_sources" in entry}
                data["castellated_sides"] = [
                    dict(old, first_pin=row["first_pin"], reverse=row.get("reverse", False),
                         **({"value_sources": provenance[old["edge"]]}
                            if old["edge"] in provenance else {}))
                    for old, row in zip(original, corrected)]
                warning = tx("Numerazione contatti corretta dopo conflitto AI; verificare la "
                             "tabella pinout prima dell'utilizzo.")
                data["notes"] = (str(data.get("notes") or "") + "\n" + warning).strip()
                if progress:
                    progress(warning)
            except (OpenRouterError, AttributeError) as repair_error:
                raise ExtractionError(tx("Correzione numerazione non riuscita; analisi foto non avviata.")) from repair_error
    if (data.get("layout") or "matrix") in ("matrix", "castellated"):
        # After the numbering repair, which compares sides in the drawing's own
        # unit and view, and before the photo stage, which calibrates on the
        # body dimensions and masks the declared contacts: both must read
        # millimetres seen from above.
        _canonicalize_answer(data)
        if progress:
            for note in data["_canonical_notes"]:
                progress(note)
    # Dopo l'eventuale correzione, cosi' le righe descrivono i lati definitivi:
    # stile dei contatti non osservato e valori stimati vanno nel log subito,
    # e restano nelle note del risultato (_params_from_tool_arguments).
    params = _params_from_tool_arguments(data)
    if progress:
        for line in _review_notes(params.castellated_sides, params.value_sources):
            progress(line)


def _extract_photo_stage(data, doc_paths, *, progress, announce, check_cancel, document_roles,
                         component_family, photo_detector, vision_model, model,
                         verification_model, diagnostic_call, extra_instructions,
                         photo_cache_dir, grounding_prompt, verification_prompt,
                         terminal_prompt, yolo_model_path, review_proposals,
                         yolo_confidence, yolo_settings,
                         recount_contacts=None, crop_prompt=None) -> ExtractedParams:
    """Optional photo inventory and final assembly of a validated main answer."""
    # La foto richiede un passaggio separato: nell'estrazione globale il modello
    # deve anche leggere quote, pinout e layout, e tende a sacrificare i passivi
    # piccoli. I crop sono best-effort: se falliscono, resta il risultato globale.
    photo_report = None
    top_index = data.get('top_photo_index')
    if document_roles is not None:
        # User-assigned roles are authoritative. Never calibrate the wrong view.
        # Only the files declared photographs (PHOTO_ROLE) go to the
        # inventory, all of them: it picks the most detailed one it can
        # calibrate. A dimensioned drawing is not a photograph, whatever
        # other role the same file carries (user decision 2026-09-26).
        top_index = photo_document_indices(doc_paths, document_roles)
        if progress:
            chosen = ', '.join(f'{doc_paths[i].name} ({tx(_role_label(PHOTO_ROLE))})'
                               for i in top_index if i >= 0)
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
        # rilevatore locale per il resto della corsa.
        # Nemmeno la chiamata a pagamento al modello di visione ('ai', 'vlm')
        # si annuncia qui (punto 139, C1): prima viene la calibrazione con
        # OpenCV, che `extract_inventory` annuncia da se', e l'annuncio
        # anticipato restava una fase «✓» vuota subito coperta da OpenCV.
        # 'llm_inventory' lo annuncia `extract_inventory` quando la foto parte.
        check_cancel()
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
                    'review_proposals': review_proposals, 'declared': data,
                    'yolo_confidence': yolo_confidence, 'yolo_settings': yolo_settings,
                    'recount_contacts': recount_contacts, 'crop_prompt': crop_prompt}
                   if photo_detector in ('yolo', 'vlm') else {}),
            )
        except ReviewCancelled as exc:
            # Un annullo non e' un guasto dell'inventario: proseguire qui
            # vorrebbe dire pagare la riconciliazione sulle proposte rifiutate.
            raise ExtractionError(str(exc)) from exc
        except CallCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - l'inventario e' facoltativo
            # Qualunque guasto dell'inventario (anche KeyError/TypeError di
            # una risposta malformata) non deve far perdere la risposta
            # principale, gia' pagata e valida: diventa una nota, con la
            # traccia completa nel log per chi deve correggerlo.
            # L'errore viene inghiottito nelle note e l'elaborazione prosegue
            # senza inventario: bisogna chiudere la fase annunciata, altrimenti
            # la modale resta per tutto il resto della corsa sull'ultimo
            # strumento annunciato dall'inventario (il rilevatore locale, se il
            # file ONNX e' sparito fra la validazione e il rilevamento) mentre
            # quello strumento non sta piu' lavorando. Tool vuoto = nessuna
            # fase rivendicata, non una fase sbagliata.
            announce("", "")
            if progress:
                import traceback
                from ..crash_report import scrub
                # The AI log may be attached to a public issue: home folder,
                # user name and keys are masked as in a crash report.
                progress("AI_PHOTO_INVENTORY_TRACEBACK " + scrub("".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()))
            data["notes"] = (str(data.get("notes") or "") + "\n"
                             + tx("Inventario fotografico fallito: {exc}", exc=exc)).strip()
        if photo_report and photo_report.components:
            # The global YOLO reconciliation already reports terminal evidence.
            # Further paid crop calls are an explicit optional UI action only.
            if photo_detector not in ('yolo', 'vlm') and isinstance(getattr(photo_report, "photo_png", None), bytes):
                from .photo_terminals import observe_terminals
                check_cancel()
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

    # Only the inventory built by the program is trusted with internal flags;
    # without it, data["components_3d"] is still the raw AI answer.
    params = _params_from_tool_arguments(
        data, trusted_components=bool(photo_report and photo_report.components))
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
        # With one column every pin is at x = 0 whatever the pitch, and a single
        # column declares none: 1.0 is a placeholder with no geometric effect.
        rows=int(params.rows), cols=int(params.cols),
        pitch=float(params.pitch_mm) if params.pitch_mm else 1.0,
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
        modo coerente) e lo si scrive nelle note, nel log e in
        `params.value_sources` ("estimated"). Quando la scheda e'
        piu' piccola del campo dei pin nessun valore e' giusto: nessuna
        correzione, solo l'avviso di quote incoerenti.
    """
    if component_family != "aggregate_tht" or params.is_castellated():
        return []
    rows, cols, pitch = params.rows, params.cols, params.pitch_mm
    length, width = params.body_length_mm, params.body_width_mm
    if not all(isinstance(v, (int, float)) and v > 0 for v in (rows, cols, length, width)):
        return []
    if cols > 1 and not (isinstance(pitch, (int, float)) and pitch > 0):
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
        # The program chose this value: it is an estimate, whatever the
        # model declared for the value it replaces.
        params.value_sources = dict(params.value_sources or {}, **{key: "estimated"})
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


#: Optional per-side hole sizes, in mm, as observed on the board: the diameter
#: of the plated half-hole cut by the edge and the drill of the full through
#: hole. Which of the two a contact carries is `contact_style`.
_SIDE_HOLE_KEYS = ("half_hole_diameter_mm", "through_hole_drill_mm")

#: Contact styles that carry each hole (see castellated_module.CONTACT_STYLES).
_STYLES_WITH_HOLE = {"half_hole_diameter_mm": ("half_hole", "half_and_through"),
                     "through_hole_drill_mm": ("through_hole", "half_and_through")}


def _add_hole_sizes(side: dict, entry: dict, notes: Optional[list]) -> None:
    """Copy the optional hole diameters of one castellated side, validated.

    A diameter must be a finite number > 0 and, with more than one contact,
    smaller than the pitch: two holes of that diameter at that pitch would
    merge into one slot, which is not a row of contacts (physical bound, not a
    tuned limit). An invalid value is dropped with a note; a hole the declared
    contact style does not carry is kept, with a note, because the two
    observations contradict each other and nothing says which one is wrong.
    """
    label = EDGE_LABELS.get(side["edge"], side["edge"])
    for key in _SIDE_HOLE_KEYS:
        raw = entry.get(key)
        if raw is None:
            continue
        value = _optional_number(raw, positive=True)
        if value is None:
            if notes is not None:
                notes.append(tx("Lato {label}: {key} scartato, valore non valido ({value!r}).",
                                label=label, key=key, value=raw))
            continue
        if side["count"] > 1 and value >= side["pitch_mm"]:
            if notes is not None:
                notes.append(tx("Lato {label}: {key} {value:g} mm non minore del passo "
                                "{pitch:g} mm, i fori adiacenti si unirebbero; valore scartato.",
                                label=label, key=key, value=value, pitch=side["pitch_mm"]))
            continue
        side[key] = value
        style = side.get("contact_style")
        if style is not None and style not in _STYLES_WITH_HOLE[key] and notes is not None:
            notes.append(tx("Lato {label}: {key} indicato ma il tipo di contatto osservato "
                            "({style}) non ha quel foro; valore conservato, da verificare.",
                            label=label, key=key, style=style))


def _normalized_castellated_sides(raw, notes: Optional[list] = None) -> list[dict]:
    """Per-side normalization of castellated sides, WITHOUT pad numbering.

    Separated from the numbering so that a numbering repair can compare the
    normalized original with the normalized correction: comparing the raw list
    with the normalized one rejected valid corrections whenever the raw list
    held a discarded entry or a differently written edge.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ExtractionError(
            tx("Il modello ha indicato un modulo con piazzole a mezzo foro ma non ha "
               "descritto alcun lato. Riprova, eventualmente con un modello piu' "
               "capace.")
        )

    sides = []
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            if notes is not None:
                notes.append(tx("Lato {index} scartato: servono lato, numero di contatti e "
                                "passo validi.", index=index))
            continue
        edge = str(entry.get("edge") or "").strip().lower()
        count = _optional_number(entry.get("count"), positive=True, integer=True)
        pitch = _optional_number(entry.get("pitch_mm", 0))
        if edge not in EDGES or count is None or pitch is None or pitch < 0 or (count > 1 and pitch == 0):
            if notes is not None:
                notes.append(tx("Lato {index} scartato: servono lato, numero di contatti e "
                                "passo validi.", index=index))
            continue
        side = {"edge": edge, "count": count, "pitch_mm": pitch}
        for key in ("first_pin", "offset_mm", "pad_length_mm", "pad_width_mm"):
            value = _optional_number(entry.get(key), positive=key != "offset_mm", integer=key == "first_pin")
            if value is not None:
                side[key] = value
        if entry.get("reverse") is True:
            side["reverse"] = True
        if isinstance(entry.get("pin_names"), (list, tuple)) and entry["pin_names"]:
            names = [str(n) for n in entry["pin_names"]]
            if len(names) == count:
                side["pin_names"] = names
            elif notes is not None:
                # A name list of the wrong length cannot be aligned with the
                # pads: any alignment would put a name on the wrong contact.
                notes.append(tx("Lato {label}: {n_names} nomi per {count} contatti; nomi "
                                "scartati per non assegnarli ai pad sbagliati.",
                                label=EDGE_LABELS.get(edge, edge), n_names=len(names),
                                count=count))
        # Il tipo di contatto si vede nella foto e non si ricava dalle quote:
        # un valore fuori elenco non si corregge ne' si scarta in silenzio,
        # perche' qualunque sostituzione sarebbe proprio la deduzione che il
        # campo esiste per evitare. Assente diventa None, cioe' «sconosciuto»:
        # nessun tipo predefinito (decisione dell'utente 26/09, era quello della
        # scheda campione), lo sceglie l'utente (vedi
        # _unobserved_contact_style_notice).
        side["contact_style"] = None
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
        _add_hole_sizes(side, entry, notes)
        sides.append(side)

    if not sides:
        raise ExtractionError(
            tx("Nessun lato valido tra quelli restituiti dal modello (servono lato, "
               "numero di contatti e passo).")
        )
    # One row of contacts per edge: two descriptions of the same edge would
    # put two pad rows on top of each other, and nothing says which is right.
    seen = set()
    for side in sides:
        if side["edge"] in seen:
            raise ExtractionError(
                tx("Il lato {label} compare piu' volte nella risposta: non si puo' stabilire "
                   "quale descrizione sia quella giusta.",
                   label=EDGE_LABELS.get(side["edge"], side["edge"])))
        seen.add(side["edge"])
    return sides


def _check_sides_fit_edges(sides, body_length, body_width) -> None:
    """Reject a side whose contact centres cannot fit along its board edge.

    Physical bound, not a tuned limit: the centres of `count` contacts at
    `pitch` span (count - 1) * pitch, which cannot exceed the edge the row sits
    on (left/right edges run along the board width, top/bottom along its
    length). Skipped when the board dimensions are unknown; the generator
    repeats a stricter check that includes the pad land.
    """
    for side in sides:
        edge_length = body_width if side["edge"] in ("left", "right") else body_length
        if edge_length is None:
            continue
        span = (side["count"] - 1) * side["pitch_mm"]
        if span > edge_length + _PIN_FIELD_TOLERANCE_MM:
            raise ExtractionError(
                tx("Lato {label}: {count} contatti a passo {pitch} mm occupano {span:.2f} mm, "
                   "ma il bordo e' lungo {edge:.2f} mm.",
                   label=EDGE_LABELS.get(side["edge"], side["edge"]), count=side["count"],
                   pitch=side["pitch_mm"], span=span, edge=edge_length))


def _castellated_sides(raw: Optional[Sequence[dict]], *, body_length: Optional[float] = None,
                       body_width: Optional[float] = None,
                       notes: Optional[list] = None) -> list[dict]:
    """Normalizza i lati di un modulo castellated, scartando quelli inutilizzabili.

    Il modello puo' restituire un lato senza passo o con zero contatti: si
    preferisce ignorarlo e dirlo (in `notes`, quando fornito), piuttosto che
    propagare un lato che farebbe fallire la generazione con un errore
    geometrico oscuro. Two sides on the same edge, or a side whose contacts
    cannot fit along the known board edge (`body_length`/`body_width`, mm),
    raise ExtractionError.

    Pad ranges are compared as intervals and never materialized: a huge
    declared count must not allocate memory proportional to it.
    """
    sides = _normalized_castellated_sides(raw, notes)
    _check_sides_fit_edges(sides, body_length, body_width)

    # Numerazione progressiva sui lati privi di first_pin, nell'ordine in cui
    # sono stati elencati: meglio una numerazione coerente da correggere a
    # mano che dei pad senza numero.
    used: list[tuple[int, int]] = []  # half-open [start, stop) pad ranges
    for side in sides:
        if "first_pin" in side:
            start, stop = side["first_pin"], side["first_pin"] + side["count"]
            shared = [max(start, a) for a, b in used if max(start, a) < min(stop, b)]
            if shared:
                raise _PinNumberConflict(tx("Numero pad duplicato tra lati: {min}.", min=min(shared)))
            used.append((start, stop))
    next_pin = 1
    for side in sides:
        if "first_pin" not in side:
            while True:
                clash = [b for a, b in used
                         if max(next_pin, a) < min(next_pin + side["count"], b)]
                if not clash:
                    break
                # Jump past the blocking range instead of stepping by one pad.
                next_pin = max(clash)
            side["first_pin"] = next_pin
            used.append((next_pin, next_pin + side["count"]))
        next_pin = int(side["first_pin"]) + side["count"]
    return sides


def _unobserved_contact_style_notice(sides: Sequence[dict]) -> Optional[str]:
    """@brief Say which castellated sides came back without a contact style.

    @param sides Normalized sides, as returned by _castellated_sides.
    @return One Italian log line naming those sides and saying that no style
        is assumed for them, or None when every side declares its own style.
    @note The style of such a side is None (unknown) in the extracted data:
        whether a contact is a half-hole, a full hole or both is seen on the
        board edge and cannot be deduced from dimensions, so no default is
        substituted (user decision 2026-09-26; the former default was the
        style of the sample board). The GUI asks the user for it
        (ExtractedParams.unobserved_contact_style_edges).
    """
    missing = [EDGE_LABELS.get(side["edge"], side["edge"])
               for side in sides if side.get("contact_style") is None]
    if not missing:
        return None
    return tx("Lato {join}: tipo di contatto non osservato (contact_style vuoto). Nessun tipo "
              "assunto: va scelto a mano fra solo semi-foro, solo foro passante o entrambi.",
              join=', '.join(missing))


def _declared_sources(raw, keys, notes: list, label: str) -> dict:
    """Valid provenances declared at one level of the answer: {field: source}.

    A value outside VALUE_SOURCES, or a field that is not a key dimension of
    that level, is ignored: the field then counts as undeclared, which is
    reviewed exactly like an estimate, so nothing is lost by ignoring it. A
    `value_sources` that is not an object is dropped with a note.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=label, value=raw))
        return {}
    return {key: source for key, source in raw.items()
            if key in keys and isinstance(source, str) and source in VALUE_SOURCES}


def _value_sources(data: dict, params: "ExtractedParams", sides, notes: list) -> dict:
    """Provenance of every key dimension the validated result carries.

    @param data Canonical answer (millimetres, contacts seen from above): its
        castellated sides were mirrored in place, together with their nested
        `value_sources`, so the edges here are those of the result.
    @param params The result built from `data`; only the values it carries
        get a provenance.
    @param sides Normalized castellated sides, or None.
    @param notes Notes of the result, for a malformed `value_sources`.
    @return {field: source} with the key scheme of ExtractedParams.value_sources;
        a value without a valid declaration is UNDECLARED_SOURCE, never "read".
    """
    sources = {}
    declared = _declared_sources(data.get("value_sources"), _TOP_LENGTH_KEYS, notes,
                                 "value_sources")
    for key in _TOP_LENGTH_KEYS:
        if getattr(params, key) is not None:
            sources[key] = declared.get(key, UNDECLARED_SOURCE)
    if not sides:
        return sources
    raw_by_edge = {}
    for entry in data.get("castellated_sides") or ():
        if isinstance(entry, dict) and "value_sources" in entry:
            raw_by_edge[str(entry.get("edge") or "").strip().lower()] = entry["value_sources"]
    for side in sides:
        edge = side["edge"]
        declared = _declared_sources(raw_by_edge.get(edge), _SIDE_LENGTH_KEYS, notes,
                                     f"castellated_sides[{edge}].value_sources")
        for key in _SIDE_LENGTH_KEYS:
            # A single contact has no pitch: its placeholder is not a value.
            if side.get(key) is None or (key == "pitch_mm" and side["count"] < 2):
                continue
            sources[f"castellated_sides.{edge}.{key}"] = declared.get(key, UNDECLARED_SOURCE)
    return sources


def _review_notes(sides, value_sources) -> list[str]:
    """@brief Lines telling what the user must decide or check before using a result.

    @param sides Normalized castellated sides, or None.
    @param value_sources Provenance map (ExtractedParams.value_sources), or None.
    @return Italian log lines: sides whose contact style was not observed,
        values estimated by the model and values whose provenance it did not
        declare. Empty when there is nothing to review.
    """
    lines = []
    notice = _unobserved_contact_style_notice(sides) if sides else None
    if notice:
        lines.append(notice)
    sources = value_sources or {}
    estimated = sorted(key for key, source in sources.items() if source == "estimated")
    undeclared = sorted(key for key, source in sources.items() if source == UNDECLARED_SOURCE)
    if estimated:
        lines.append(tx("Valori STIMATI, non letti dai documenti: {fields}. Verificarli prima "
                        "dell'uso.", fields=", ".join(estimated)))
    if undeclared:
        lines.append(tx("Valori senza provenienza dichiarata dal modello, trattati come stime "
                        "da verificare: {fields}.", fields=", ".join(undeclared)))
    return lines


#: Verification and provenance fields of a component that only the program's
#: own photo pipeline may set: confidences, counts declared complete, the
#: inventory's identifiers and class, and `object_type` (whose presence lets a
#: body named like a PCB feature through). A raw AI answer loses them, together
#: with every `_`-prefixed internal flag (`_yolo_manual_box`,
#: `_height_estimated`, `_global_inventory_status`, ...). Descriptive fields
#: (name, kind, package, mating, colour, ...) are kept.
_TRUSTED_ONLY_FIELDS = frozenset({
    "object_type", "confidence", "presence_confidence", "class_confidence", "class",
    "component_id", "source_candidate_ids", "pin_count_confidence", "pin_count_source",
    "terminal_count_basis", "lead_count_complete", "lead_count_confidence",
    "landmark_confidence",
})
#: `_height_source` of a height taken from a raw AI answer: the schema asks for
#: a visual estimate from a top photo, never a measurement.
AI_ANSWER_HEIGHT_SOURCE = "ai_answer_visual_estimate_not_measured"


def _components_3d(raw: Optional[Sequence[dict]],
                   notes: Optional[list] = None, *,
                   trusted: bool = False) -> Optional[list[dict]]:
    """@brief Components read from the photo, dropping those without a real footprint.

    Sono stime visive: un blocco con una dimensione nulla o negativa non e'
    un componente mal misurato, e' rumore, e nel solido 3D darebbe errore.
    A `kind` outside COMPONENT_KINDS becomes "generic", with a line in
    `notes` when given.

    @param raw Entries of components_3d.
    @param notes Optional list that receives the review lines.
    @param trusted True only for components built by the program (photo
        inventory, saved projects): their internal flags are kept. False
        (default) for a raw AI answer: every `_`-prefixed key and every field
        of _TRUSTED_ONLY_FIELDS is dropped first, so a text in a document can
        never mark a body as drawn by hand, verified or measured, and every
        height is marked as an estimate (`_height_estimated` True,
        AI_ANSWER_HEIGHT_SOURCE).
    @return The valid components, or None when there is none.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    components = []
    for entry in raw:
        if not isinstance(entry, dict):
            # Con liste lunghe il modello a volte
            # deraglia dallo schema e infila una voce come semplice stringa
            # invece che come oggetto: si scarta quella voce, non l'intera
            # estrazione -- gli altri componenti restano validi.
            continue
        if not trusted:
            entry = {key: value for key, value in entry.items()
                     if not (isinstance(key, str) and key.startswith("_"))
                     and key not in _TRUSTED_ONLY_FIELDS}
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
        if kind not in COMPONENT_KINDS:
            # A kind outside the vocabulary has no shape nor height rule: the
            # body stays a generic block, and the note says so instead of
            # letting the change pass unseen.
            if notes is not None:
                notes.append(tx("{name}: tipo di corpo {kind!r} non previsto, trattato come "
                                "generico (blocco semplice).", name=component["name"], kind=kind))
            kind = "generic"
        component["kind"] = kind
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
                    # Corpo aggiunto dal solo modello, escluso finche' l'utente
                    # non lo tiene (punto 141): senza, la prima estrazione lo
                    # consegnava come un corpo qualunque, dentro il 3D.
                    '_ai_only_excluded',
                    '_package_verified', '_orientation_verified', 'pin_count_confidence', 'pin_count_source',
                    'class', 'component_id', 'source_candidate_ids', 'terminal_count', 'terminal_layout',
                    'terminal_count_basis', 'lead_count', 'lead_count_complete', 'lead_count_confidence',
                    '_reported_rotation_deg', '_height_estimated', '_global_inventory_status',
                    # Misura dai bordi, regola dell'altezza stimata e scelta
                    # dell'utente sulle proposte deboli: la GUI li mostra.
                    '_edges_measured', '_height_basis', '_yolo_low_confidence', '_yolo_user_kept',
                    '_global_inventory_errors', '_global_inventory_uncertainties',
                    '_global_inventory_prompt_version', '_global_inventory_prompt_hash',
                    # Il riferimento asimmetrico serve al costruttore 3D per
                    # stabilire il verso del modello nativo. Senza questa riga
                    # veniva scartato qui, e ogni componente arrivava al
                    # matching dichiarando "nessun riferimento osservato" anche
                    # quando il modello ne aveva visto uno ad alta confidenza.
                    'landmark_feature', 'landmark_side', 'landmark_confidence',
                    # Rilettura per ritaglio (HANDOFF 144): esito, le due
                    # letture e i valori dell'inventario sostituiti devono
                    # arrivare alla revisione, altrimenti una riga «da
                    # rivedere» non direbbe piu' perche'.
                    'package_confidence', '_crop_reading', '_crop_readings',
                    '_crop_reading_version', '_crop_reading_prompt_digest',
                    '_package_inventory', '_class_inventory',
                    '_lead_count_inventory'):
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
        if not trusted:
            component["_height_estimated"] = True
            component["_height_source"] = AI_ANSWER_HEIGHT_SOURCE
        components.append(component)
    return components or None


#: Matrix fields that are integer counts; every one must be >= 1.
_MATRIX_INT_FIELDS = ("rows", "cols")
#: Matrix fields that are physical lengths in mm; every one must be > 0.
_MATRIX_LENGTH_FIELDS = ("pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm")
#: Optional signed offsets in mm: any finite number is meaningful.
_SIGNED_OPTIONAL_FIELDS = ("body_offset_x_mm", "body_offset_y_mm")
#: Optional physical lengths in mm: only finite values > 0 are meaningful.
_POSITIVE_OPTIONAL_FIELDS = ("body_length_mm", "body_width_mm", "body_height_mm",
                             "pin_length_3d_mm", "pcb_thickness_mm")


def _required_matrix_value(data: dict, key: str):
    """One mandatory matrix field, validated; ExtractionError when unusable.

    Counts must be integral (2.0 accepted, 2.7 and True rejected) and >= 1;
    lengths must be finite and > 0. Nothing is truncated or coerced silently.
    """
    raw = data.get(key)
    if raw is None:
        raise ExtractionError(
            tx("La risposta del modello non contiene il campo obbligatorio '{key}'. "
               "Riprova, eventualmente con un modello piu' capace.",
               key=key)
        )
    if key in _MATRIX_INT_FIELDS:
        value = _optional_number(raw, positive=True, integer=True)
        if value is None:
            raise ExtractionError(tx("Campo obbligatorio '{key}' non valido: {value!r} (serve un "
                                     "intero maggiore di zero, non booleano).",
                                     key=key, value=raw))
        return value
    value = _optional_number(raw, positive=True)
    if value is None:
        raise ExtractionError(tx("Campo obbligatorio '{key}' non valido: {value!r} (serve un "
                                 "numero finito maggiore di zero, non booleano).",
                                 key=key, value=raw))
    return value


def _validated_populated_pins(raw, max_pin: Optional[int], notes: list) -> Optional[list]:
    """Optional populated_pins: keep the valid, in-range pin numbers once each.

    Invalid entries (booleans, fractions, non-numbers, out of 1..max_pin) are
    dropped and reported in `notes`, like repeated numbers; the valid ones are
    kept, as for any partial response. None when nothing valid remains.
    """
    if raw is None or (isinstance(raw, (list, tuple)) and not raw):
        return None
    if not isinstance(raw, (list, tuple)):
        notes.append(tx("populated_pins scartato: valore non valido ({value!r}).", value=raw))
        return None
    kept, rejected, repeated = [], [], []
    for item in raw:
        number = _optional_number(item, positive=True, integer=True)
        if number is None or (max_pin is not None and number > max_pin):
            rejected.append(item)
        elif number in kept:
            repeated.append(number)
        else:
            kept.append(number)
    if rejected:
        notes.append(tx("populated_pins: scartate le voci non valide o fuori matrice {values!r}; "
                        "verificare i pin presenti.", values=rejected))
    if repeated:
        notes.append(tx("populated_pins: rimossi i numeri ripetuti {values!r}.", values=repeated))
    return kept or None


def _informative_matrix(data: dict, notes: list) -> dict:
    """Matrix fields that do not define the footprint: kept when valid, else noted.

    Used for a castellated module and for a part whose explicit pads
    (package_geometry) are the footprint. A drill of 0 is not an error here:
    it is how a pad without hole (SMD) is written, so it becomes None silently.
    """
    matrix = {}
    for key in _MATRIX_INT_FIELDS:
        raw = data.get(key)
        matrix[key] = _optional_number(raw, positive=True, integer=True)
        if raw is not None and matrix[key] is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
    for key in _MATRIX_LENGTH_FIELDS:
        raw = data.get(key)
        matrix[key] = _optional_number(raw, positive=True)
        if key == "drill_mm" and _optional_number(raw) == 0:
            continue
        if raw is not None and matrix[key] is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
    return matrix


def _footprint_matrix(data: dict, notes: list) -> dict:
    """Matrix fields of a through-hole pin grid that IS the footprint.

    Required fields follow from what the grid structurally is: rows, cols,
    pad and drill always; the pitch only with more than one column and the
    row spacing only with more than one row, because a single column or row
    has no such distance (it is left None, never borrowed from the other
    quantity). A given but invalid optional distance is dropped with a note.

    @throws ExtractionError For a missing or invalid required field, for a
        drill not smaller than the pad, and for a pad without hole: the grid
        generator draws only drilled pads, and a pad without hole needs its
        own shape, which only package_geometry describes.
    """
    matrix = {key: _required_matrix_value(data, key) for key in _MATRIX_INT_FIELDS}
    for key, count in (("pitch_mm", matrix["cols"]), ("row_spacing_mm", matrix["rows"])):
        if count > 1:
            matrix[key] = _required_matrix_value(data, key)
            continue
        raw = data.get(key)
        matrix[key] = _optional_number(raw, positive=True)
        if raw is not None and matrix[key] is None and _optional_number(raw) != 0:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
    matrix["pad_diameter_mm"] = _required_matrix_value(data, "pad_diameter_mm")
    raw_drill = data.get("drill_mm")
    if raw_drill is None or _optional_number(raw_drill) == 0:
        raise ExtractionError(tx(
            "Pad senza foro (drill_mm assente o 0) e nessun pad esplicito in "
            "package_geometry: la matrice di pin genera solo pad a foro passante, e un "
            "componente SMD richiede i pad descritti uno per uno. Riprova, eventualmente "
            "con un modello piu' capace."))
    matrix["drill_mm"] = _required_matrix_value(data, "drill_mm")
    if matrix["drill_mm"] >= matrix["pad_diameter_mm"]:
        raise ExtractionError(tx("Foratura non valida: drill_mm {drill} deve essere minore di "
                                 "pad_diameter_mm {pad}.", drill=matrix["drill_mm"],
                                 pad=matrix["pad_diameter_mm"]))
    return matrix


def _params_from_tool_arguments(data: dict, *, trusted_components: bool = False) -> ExtractedParams:
    """@brief Build ExtractedParams from the tool arguments, without any network access.

    Costruisce ExtractedParams dagli argomenti dello strumento. Separato
    dalla chiamata di rete per poterlo collaudare senza API.

    @param data Tool arguments as the model returned them, or as
        `_validate_main_response` canonicalized them. The input is never
        modified: a raw answer is canonicalized on a copy
        (`_canonicalize_answer`: lengths to millimetres from `drawing_unit`,
        contacts to the view from above from `contacts_view`).
    @param trusted_components True only when `data["components_3d"]` is the
        photo inventory built by the program; False (default) treats it as the
        raw AI answer, whose internal flags are dropped (see `_components_3d`).
    @return The validated parameters, with every conversion, doubt and
        dropped optional value written in `notes`.
    @note What is required depends on what the answer structurally is. Valid
        explicit pads (package_geometry) are the footprint: the matrix fields
        become informative, and a drill of 0 or none means pads without hole.
        Without them, a "matrix" answer is a through-hole pin grid (see
        `_footprint_matrix`); a "castellated" one is its edge sides.
    @throws ExtractionError For any unusable MANDATORY field (missing,
        boolean, non-finite, non-integral count, non-positive length, drill
        not smaller than the pad, pad without hole and without explicit pads),
        for invalid explicit pads and for invalid castellated sides. Invalid
        OPTIONAL fields never fail the paid response: they are dropped and
        the reason is appended to `notes`.
    """
    if not isinstance(data, dict):
        raise ExtractionError(tx("Risposta AI non strutturata."))
    layout = data.get("layout") or "matrix"
    if layout not in ("matrix", "castellated"):
        raise ExtractionError(tx("Disposizione dei contatti sconosciuta: {layout!r}.", layout=layout))
    if not data.get("_canonical"):
        data = copy.deepcopy(data)
        _canonicalize_answer(data)
    notes: list[str] = list(data.get("_canonical_notes") or ())

    def _opt_float(key: str) -> Optional[float]:
        """Optional numeric field; an invalid value is dropped with a note."""
        raw = data.get(key)
        value = _optional_number(raw, positive=key in _POSITIVE_OPTIONAL_FIELDS)
        if raw is not None and value is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).", key=key, value=raw))
        return value

    package_geometry = data.get("package_geometry")
    if isinstance(package_geometry, dict) and not package_geometry.get("pins"):
        # Campo facoltativo tornato vuoto ({} o «pins»: []): nessun pad
        # esplicito, quindi nessuna geometria package. Non e' un errore della
        # risposta -- lo schema non impone un minimo di pin -- e rifiutarla
        # butterebbe un'estrazione pagata con dati di matrice o modulo validi.
        # La regola «senza pad non c'e' footprint» resta in
        # PackageGeometry.validate, dove si genera.
        if package_geometry:
            notes.append(tx("Geometria package senza pad espliciti ignorata: si usano i "
                            "parametri a matrice o del modulo."))
        package_geometry = None
    if package_geometry is not None:
        try:
            # Validate now, while the source response and its warnings are still
            # available; the GUI must never receive malformed pad coordinates.
            package_from_mapping(package_geometry)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(tx("Geometria package non valida: {exc}", exc=exc)) from exc

    if layout == "castellated":
        sides = _castellated_sides(
            data.get("castellated_sides"),
            body_length=_optional_number(data.get("body_length_mm"), positive=True),
            body_width=_optional_number(data.get("body_width_mm"), positive=True),
            notes=notes)
        # A castellated module has no matrix: counts or lengths it still sends
        # are informative only and dropped with a note when malformed.
        matrix = _informative_matrix(data, notes)
    elif package_geometry is not None:
        sides = None
        matrix = _informative_matrix(data, notes)
    else:
        sides = None
        matrix = _footprint_matrix(data, notes)

    rows, cols = matrix["rows"], matrix["cols"]
    max_pin = rows * cols if layout == "matrix" and rows and cols else None
    populated = _validated_populated_pins(data.get("populated_pins"), max_pin, notes)

    pin_map = None
    # La specchiatura di pin_map e' gia' stata fatta (_canonicalize_answer):
    # qui la mappa e' vista dall'alto e si ricontrolla soltanto. Per i moduli
    # castellated le posizioni vengono dai lati, non da righe/colonne.
    if layout == "matrix" and data.get("pin_map") and cols:
        try:
            pin_map = _pin_map_seen_from_top(data.get("pin_map"), cols, "top", rows)
        except ExtractionError as exc:
            notes.append(tx("ATTENZIONE pin_map scartata ({exc}): numerazione automatica colonna "
                            "per colonna, da verificare sul pinout.", exc=exc))

    body_length = _opt_float("body_length_mm")
    confidence = data.get("confidence", "low")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    base_notes = data.get("notes", "")
    base_notes = base_notes if isinstance(base_notes, str) else str(base_notes or "")

    def _text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    params = ExtractedParams(
        name=_text("name"),
        description=_text("description"),
        tags=_text("tags"),
        layout=layout,
        rows=rows,
        cols=cols,
        pitch_mm=matrix["pitch_mm"],
        row_spacing_mm=matrix["row_spacing_mm"],
        pad_diameter_mm=matrix["pad_diameter_mm"],
        drill_mm=matrix["drill_mm"],
        castellated_sides=sides,
        components_3d=_components_3d(data.get("components_3d"), notes,
                                     trusted=trusted_components),
        confidence=confidence,
        notes=base_notes,
        populated_pins=populated,
        body_length_mm=body_length,
        body_width_mm=_opt_float("body_width_mm"),
        body_height_mm=_opt_float("body_height_mm"),
        pcb_thickness_mm=_opt_float("pcb_thickness_mm"),
        body_offset_y_mm=_opt_float("body_offset_y_mm"),
        pin_length_3d_mm=_opt_float("pin_length_3d_mm"),
        edge_fillets=data.get("edge_fillets") or None,
        pinout=data.get("pinout") or None,
        pin_map=pin_map,
        body_offset_x_mm=_opt_float("body_offset_x_mm"),
        body_color_hex=data.get("body_color_hex") or None,
        text_color_hex=data.get("text_color_hex") or None,
        package_geometry=package_geometry,
    )
    params.value_sources = _value_sources(data, params, sides, notes)
    notes.extend(_review_notes(sides, params.value_sources))
    if notes:
        params.notes = (params.notes + "\n" + "\n".join(notes)).strip()
    return params
