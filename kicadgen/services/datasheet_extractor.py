"""@brief Footprint parameter extraction from datasheets through the OpenRouter API.

The documentation files (PDF/images) are sent to OpenRouter -- which gives
access to the models of every vendor through a single key -- with the request
to fill in a tool (tool calling) whose schema is that of the pin header
parameters, so the answer is always valid structured JSON, never free text
to be parsed by hand.

The model is chosen from the GUI (OpenRouter id, e.g.
"anthropic/claude-sonnet-5"): the GUI setting lives in the SQLite profiles,
while a single extraction may use a more capable one without changing the
default.

The environment variable OPENROUTER_API_KEY is required (normally in the
.env file next to main.py). No key is ever written into the code.
"""

from __future__ import annotations

import base64
import copy
import mimetypes
import math
import numbers
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..cad3d.annotations import EDGE_NAMES
from ..core.package_geometry import package_doubts, package_from_mapping
from ..core.validation import finite_number
from ..generators.castellated_module import (
    CONTACT_STYLES, EDGE_LABELS, EDGES,
)
from .ai_diagnostics import CallCancelled
from .openrouter import (DEFAULT_MODEL, OpenRouterError, ToolArgumentsError, api_key, call_tool,
                         check_credit, reset_usage, usage_totals)
from .edge_notches import EdgeCount
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
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB, reasonable limit per single API file

_SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

_TOOL_NAME = "report_pin_header_params"

#: Length fields of the answer, per level: every one is a length in the
#: declared drawing unit and becomes millimetres after `_canonicalize_answer`.
#: They are also the key dimensions whose provenance the model declares
#: (`value_sources`).
_TOP_LENGTH_KEYS = ("pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm",
                    "body_length_mm", "body_width_mm", "body_height_mm", "pcb_thickness_mm",
                    "body_offset_x_mm", "body_offset_y_mm", "pin_length_3d_mm",
                    "lead_diameter_mm")
#: Where the leads leave the body of a single through-hole part (`lead_exit`).
#: "bottom": straight out of the underside (potted modules, transformers,
#: relays, connectors), so every lead centre lies inside the body outline;
#: "side": out of the flanks and bent down (DIP, TO-220), so the rows may lie
#: outside the moulding; "unknown": the documents do not show it.
LEAD_EXITS = ("bottom", "side", "unknown")
_SIDE_LENGTH_KEYS = ("pitch_mm", "offset_mm", "pad_length_mm", "pad_width_mm",
                     "half_hole_diameter_mm", "through_hole_drill_mm",
                     "first_contact_from_edge_mm", "last_contact_from_edge_mm")

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
#: Provenance the PROGRAM gives a value it measured on the calibrated photo
#: (edge_lands, HANDOFF 169). Never accepted from the model's own
#: `value_sources` (see _declared_sources): the model reads documents, it
#: does not measure pixels.
MEASURED_SOURCE = "measured"


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
                    "programma). CONVENZIONE DEGLI ASSI, come il disegno appare sulla "
                    "pagina: X cresce verso DESTRA e Y cresce verso il BASSO (un pad piu' "
                    "in basso nel disegno ha y_mm MAGGIORE; non usare la convenzione "
                    "matematica con Y verso l'alto, che specchierebbe il footprint e "
                    "invertirebbe l'ordine dei pin). Ogni angolo (orientation_deg, "
                    "rotation_deg) e' in gradi in senso ANTIORARIO come appare sulla "
                    "pagina. Un pad esposto/termico e' un pin a se'. Non inventare "
                    "coordinate non supportate dal documento."
                ),
                "properties": {
                    "family": {"type": "string", "enum": [
                        "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp",
                        "module", "castellated", "custom"]},
                    "body_length_mm": {
                        "type": "number", "exclusiveMinimum": 0,
                        "description": (
                            "Estensione del corpo lungo l'asse X di queste coordinate "
                            "(orizzontale nella vista di source_view), qualunque nome le "
                            "dia il datasheet (D, E, E1, length). Con i pin sui lati "
                            "sinistro e destro (SOIC disegnato in verticale) e' la "
                            "dimensione TRASVERSALE alle file di pin, non quella lungo le "
                            "file."),
                    },
                    "body_width_mm": {
                        "type": "number", "exclusiveMinimum": 0,
                        "description": (
                            "Estensione del corpo lungo l'asse Y di queste coordinate "
                            "(verticale nella vista di source_view), qualunque nome le dia "
                            "il datasheet. Con i pin sui lati sinistro e destro e' la "
                            "dimensione LUNGO le file di pin."),
                    },
                    "body_height_mm": {"type": "number", "exclusiveMinimum": 0},
                    "lead_form": {
                        "type": "string", "enum": ["gull_wing", "j_lead", "unknown"],
                        "description": (
                            "Forma dei terminali come appare nella vista laterale: "
                            "\"gull_wing\" se escono dai fianchi e si piegano verso "
                            "l'esterno ad ala di gabbiano, \"j_lead\" se si piegano a J "
                            "sotto il bordo del corpo (le piazzole stanno sotto il bordo), "
                            "\"unknown\" se il disegno non mostra la vista laterale. Non "
                            "dedurlo dal nome del package."),
                    },
                    "origin": {"type": "string", "enum": ["body_center", "pin1", "custom"]},
                    "source_view": {"type": "string", "enum": ["top", "bottom"]},
                    "orientation_deg": {
                        "type": "number",
                        "description": (
                            "Rotazione da applicare al disegno per ottenere il footprint, "
                            "in gradi ANTIORARI come appare sulla pagina; 0 per disegnare "
                            "il footprint come il disegno."),
                    },
                    "mirrored": {
                        "type": "boolean",
                        "description": (
                            "true SOLO se il disegno stesso e' un'immagine speculare della "
                            "vista dichiarata in source_view per un motivo DIVERSO dall'essere "
                            "una vista dal basso (es. etichettato «mirror image» o «vista "
                            "dall'alto in trasparenza»). Una BOTTOM VIEW si dichiara SOLO con "
                            "source_view \"bottom\" e mirrored false: la specchiatura della "
                            "vista dal basso la fa gia' il programma, e mirrored true la "
                            "annullerebbe."),
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "needs_review": {"type": "boolean"},
                    "pins": {"type": "array", "items": {"type": "object", "properties": {
                        "number": {"type": "string"}, "name": {"type": "string"},
                        "x_mm": {"type": "number"}, "y_mm": {"type": "number"},
                        "pad_length_mm": {"type": "number", "exclusiveMinimum": 0,
                                          "description": "Dimensione del pad lungo X, prima di rotation_deg."},
                        "pad_width_mm": {"type": "number", "exclusiveMinimum": 0,
                                         "description": "Dimensione del pad lungo Y, prima di rotation_deg."},
                        "side": {"type": "string", "enum": ["left", "right", "top", "bottom", "center"]},
                        "pad_type": {"type": "string", "enum": ["smd", "thru_hole", "np_thru_hole", "connect"]},
                        # Only shapes a PinSpec can describe (review 2026-10-27,
                        # G7): a trapezoid needs KiCad's `rect_delta`, which a
                        # pin does not carry, and was written as a rectangle.
                        "shape": {"type": "string", "enum": ["circle", "rect", "oval", "roundrect"]},
                        "rotation_deg": {"type": "number",
                                         "description": "Angolo del pad in gradi ANTIORARI come appare sulla pagina."},
                        "drill_mm": {"type": "number", "exclusiveMinimum": 0},
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
            "cols": {
                "type": "integer",
                "description": (
                    "Numero di POSIZIONI per riga della griglia regolare a passo pitch_mm, "
                    "comprese quelle vuote: non il numero di pin fisici. Due pin della "
                    "stessa riga distanti un multiplo del passo sono tante colonne piu' "
                    "in la' quanti sono i passi, e le posizioni fra loro contano anche se "
                    "vuote (le vuote si escludono con populated_pins). Una griglia "
                    "compressa ai soli pin fisici darebbe un footprint con i pin nel "
                    "posto sbagliato."
                ),
            },
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
            "lead_diameter_mm": {
                "type": "number",
                "description": (
                    "Diametro del TERMINALE (reoforo, pin) quotato nel disegno, "
                    "nell'unita' di drawing_unit (es. \"Ø0.5\" accanto al pin nella "
                    "vista laterale). NON e' il foro: il foro deve essere piu' largo "
                    "del terminale, e il programma lo controlla. Ometti se non quotato."
                ),
            },
            "lead_exit": {
                "type": "string",
                "enum": ["bottom", "side", "unknown"],
                "description": (
                    "Da quale faccia del corpo escono i terminali di un componente "
                    "singolo a foro passante, guardando la vista laterale o la foto: "
                    "\"bottom\" se escono dritti dal fondo (moduli incapsulati, "
                    "trasformatori, rele', connettori: ogni pin sta dentro il contorno "
                    "del corpo), \"side\" se escono dai fianchi e sono piegati verso "
                    "il basso (DIP, TO-220: le file possono stare fuori dal corpo), "
                    "\"unknown\" se non si vede. Non dedurlo dal nome del pezzo."
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
                    "o simile) con l'ingombro del corpo del componente: estensione del "
                    "corpo lungo l'asse X del footprint (orizzontale nella vista), "
                    "nell'unita' di drawing_unit, qualunque nome le dia il datasheet. "
                    "In una matrice di pin X e' la direzione delle colonne, quindi del "
                    "pitch; con pad espliciti (package_geometry, o pads) X e' l'asse X delle loro "
                    "coordinate, anche quando le file di pin corrono in verticale (pin a "
                    "sinistra e a destra: e' la dimensione trasversale alle file). Ometti se "
                    "il disegno meccanico non e' presente."
                ),
            },
            "body_width_mm": {
                "type": "number",
                "description": (
                    "Larghezza del corpo, estensione lungo l'asse Y del footprint "
                    "(verticale nella vista; stessa convenzione di body_length_mm), "
                    "dalla vista dall'alto o dal basso del disegno meccanico: e' la "
                    "dimensione del CONTORNO ESTERNO del case. In una matrice di pin NON "
                    "e' la distanza tra le due righe di pin: e' quasi sempre "
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
                    "e una bottom view viene specchiata anche senza pin_map. Elenca SOLO "
                    "i pin il cui numero e' SCRITTO nel disegno accanto alla posizione: "
                    "non estendere la numerazione alle posizioni senza numero e non "
                    "applicare una convenzione (pin header o DIP) al posto della lettura; "
                    "una posizione senza numero nel disegno non va in pin_map."
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
                        "first_contact_from_edge_mm": {
                            "type": "number",
                            "description": (
                                "Distanza, lungo il bordo, dall'inizio del bordo (da "
                                "sinistra per top/bottom, dall'alto per left/right) al "
                                "centro del PRIMO contatto, nell'unita' di drawing_unit. "
                                "SOLO se e' una quota scritta nel disegno quotato; ometti "
                                "altrimenti, non derivarla."
                            ),
                        },
                        "last_contact_from_edge_mm": {
                            "type": "number",
                            "description": (
                                "Distanza, lungo il bordo, dalla fine del bordo al centro "
                                "dell'ULTIMO contatto, nell'unita' di drawing_unit. SOLO se "
                                "e' una quota scritta nel disegno quotato; ometti "
                                "altrimenti, non derivarla."
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
        # Always required; the matrix fields and the castellated-side fields
        # are required depending on what the answer is (layout, explicit
        # pads, number of rows and columns), and the check is made in
        # _params_from_tool_arguments() where the values are known.
        # Unit and view are required so that the model always declares them;
        # an answer that omits them stays readable (millimetres, unknown view
        # declared as a doubt in the notes).
        "required": ["layout", "confidence", "drawing_unit", "contacts_view"],
    },
}

#: System prompt of the DEDICATED pin-numbering call (user, 2026-10-10:
#: «separa, vediamo se in quel modo funziona»). One perceptual task only: the
#: main call reads every quote well and invents the pin numbering (a zig-zag
#: convention over the six numbers printed on the TEL8 drawing), so the
#: numbering is asked again, alone, with the drawing alone.
DEFAULT_PIN_NUMBERING_PROMPT = (
    "Sei un lettore di disegni meccanici. Ti viene mostrato il disegno quotato di un "
    "componente con i suoi pin disposti su una griglia regolare. Il tuo UNICO compito e' "
    "riportare, per ogni NUMERO DI PIN STAMPATO nel disegno accanto a una posizione, la "
    "riga e la colonna di quella posizione sulla griglia. Conta righe e colonne COSI' COME "
    "APPAIONO nel disegno: riga 0 la fila piu' in alto, colonna 0 la posizione piu' a "
    "sinistra della griglia intera a passo costante, posizioni vuote comprese (due pin "
    "distanti un multiplo del passo sono tante colonne piu' in la' quanti sono i passi). "
    "Non specchiare e non ruotare nulla: dichiara in contacts_view se il disegno e' una "
    "vista dal basso (BOTTOM VIEW) o dall'alto, e il programma fara' la specchiatura. "
    "Riporta SOLO i numeri che leggi stampati: non estendere la numerazione alle posizioni "
    "senza numero e non applicare alcuna convenzione (pin header, DIP). Se non leggi "
    "nessun numero, rispondi con un elenco vuoto. Rispondi SOLO chiamando lo strumento."
)

_PIN_NUMBERING_TOOL = {
    "name": "report_pin_numbering",
    "description": "Posizione sulla griglia di ogni numero di pin stampato nel disegno.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contacts_view": {
                "type": "string", "enum": ["top", "bottom", "unknown"],
                "description": "Da che lato e' visto il disegno dei pin (titolo BOTTOM VIEW = bottom).",
            },
            "pins": {
                "type": "array",
                "description": "Un elemento per ogni numero di pin STAMPATO nel disegno, nessun altro.",
                "items": {
                    "type": "object",
                    "properties": {
                        "pin": {"type": "integer", "description": "Numero stampato accanto alla posizione."},
                        "row": {"type": "integer", "description": "Indice riga: 0 = fila piu' in alto nel disegno."},
                        "col": {"type": "integer", "description": "Indice colonna: 0 = posizione piu' a sinistra nel disegno."},
                    },
                    "required": ["pin", "row", "col"],
                },
            },
            "notes": {"type": "string", "description": "Dubbi di lettura, in breve."},
        },
        "required": ["contacts_view", "pins"],
    },
}

#: Roles of the documents the dedicated numbering call looks at: the
#: dimensioned drawing and the pinout drawing, the only places where a pin
#: number is printed beside a position.
NUMBERING_DOCUMENT_ROLES = ("top", "pinout")


def _pin_numbering_tool() -> dict:
    """The dedicated numbering schema in OpenRouter's "function" wrapper."""
    return {"type": "function", "function": {
        "name": _PIN_NUMBERING_TOOL["name"],
        "description": _PIN_NUMBERING_TOOL["description"],
        "parameters": _PIN_NUMBERING_TOOL["input_schema"]}}


def numbering_documents(doc_paths, document_roles) -> list:
    """@brief The documents a pin number can be read from: those declared drawing or pinout.

    @param doc_paths Documents of the analysis.
    @param document_roles Normalized roles (file name -> tuple of keys), or None.
    @return The paths carrying a role in NUMBERING_DOCUMENT_ROLES, images only
        (a PDF cannot be enlarged and is read by the main call); [] without roles.
    """
    if document_roles is None:
        return []
    return [path for path in doc_paths
            if path.suffix.lower() != ".pdf"
            and any(role in NUMBERING_DOCUMENT_ROLES
                    for role in _declared_role_keys(document_roles.get(path.name) or ()))]


def _numbering_only_in_pdf(doc_paths, document_roles) -> bool:
    """True when a drawing or pinout role is declared only on PDF files (no image to enlarge)."""
    if document_roles is None:
        return False
    return (not numbering_documents(doc_paths, document_roles)
            and any(path.suffix.lower() == ".pdf"
                    and any(role in NUMBERING_DOCUMENT_ROLES
                            for role in _declared_role_keys(document_roles.get(path.name) or ()))
                    for path in doc_paths))


def read_pin_numbering(documents, *, rows, cols, model, call, prompt=None,
                       progress=None) -> Optional[dict]:
    """@brief Ask the model, alone, where each printed pin number sits on the rows x cols grid.

    @param documents Image documents of `numbering_documents`.
    @param rows Rows of the grid of the main answer (drawing orientation).
    @param cols Columns of the grid of the main answer.
    @param model Model identifier of the analysis (its provider sizes the image).
    @param call The logged, cancellable call wrapper of the analysis.
    @param prompt System prompt; None means DEFAULT_PIN_NUMBERING_PROMPT.
    @param progress Optional log callback.
    @return None without documents or grid (no call made); otherwise
        {"view": "top"|"bottom"|"unknown", "pins": [{"pin", "row", "col"}, ...],
        "notes": str} with the entries validated against the grid (row < rows,
        col < cols, no pin or position twice); "pins" is empty and "failed"
        (call error) or "discarded" (unusable answer) says why, so the note
        of `apply_dedicated_numbering` names the real outcome.
    @note Best-effort by design: a failure of this paid call must never lose
        the main answer. The result is in DRAWING coordinates, mirrored later
        by `_pin_map_seen_from_top` with the declared view, like pin_map.
    """
    if not documents or not rows or not cols:
        return None
    #: Output budget sized on the grid: about 30 tokens per entry when every
    #: position carries a printed number, plus room for the view and notes.
    max_tokens = int(min(8192, max(1024, 512 + 30 * int(rows) * int(cols))))
    blocks = []
    for index, path in enumerate(documents):
        blocks.append({"type": "text", "text": f"FILE {index}: {path.name}"})
        blocks.append(_file_to_content_block(path, model))
    blocks.append({"type": "text", "text": (
        f"La griglia dei pin ha {int(rows)} file (righe) e {int(cols)} posizioni per fila "
        f"(colonne) a passo costante. Riporta la posizione di ogni numero di pin stampato "
        f"nel disegno e chiama lo strumento.")})
    try:
        data = call(model=model, system_prompt=prompt or DEFAULT_PIN_NUMBERING_PROMPT,
                    content_blocks=blocks, tool=_pin_numbering_tool(),
                    max_tokens=max_tokens, has_pdf=False)
    except (OpenRouterError, ToolArgumentsError) as exc:
        if progress:
            progress(tx("Lettura dedicata della numerazione non riuscita ({exc}): resta la "
                        "numerazione della risposta principale.", exc=exc))
        return {"view": "unknown", "pins": [], "notes": "", "failed": str(exc)}
    if not isinstance(data, dict):
        return {"view": "unknown", "pins": [], "notes": "",
                "discarded": tx("risposta non strutturata")}
    view = data.get("contacts_view")
    view = view if view in ("top", "bottom", "unknown") else "unknown"
    try:
        entries = _pin_map_seen_from_top(data.get("pins") or [], int(cols), "top", int(rows))
    except ExtractionError as exc:
        if progress:
            progress(tx("Lettura dedicata della numerazione scartata ({exc}): resta la "
                        "numerazione della risposta principale.", exc=exc))
        return {"view": view, "pins": [], "notes": "", "discarded": str(exc)}
    notes = data.get("notes")
    return {"view": view, "pins": entries or [],
            "notes": notes if isinstance(notes, str) else ""}


#: System prompt of the DEDICATED pad-position call of a single part with
#: explicit pads (user, 2026-10-10: two readings of the VE12H-K relay drawing
#: placed the holes differently, both wrong, and nothing noticed). Since
#: 2026-10-30 the model only transcribes the dimension chains as printed and
#: the program adds them up (pads_from_chains): both readings had dropped one
#: quote of a chain while summing it, the same way.
DEFAULT_PAD_POSITIONS_PROMPT = (
    "Sei un lettore di disegni meccanici. Ti viene mostrato il disegno quotato dei fori o "
    "delle piazzole di UN SOLO componente. NON calcolare coordinate: trascrivi le CATENE DI "
    "QUOTE cosi' come sono stampate, le somme le fa il programma. Una catena e' una fila di "
    "quote consecutive lungo un asse: x orizzontale (da sinistra a destra) oppure y verticale "
    "(dall'alto in basso). Per ogni catena elenca in `stops` le fermate in ordine crescente "
    "sulla pagina e in `values` le quote stampate fra due fermate consecutive (una in meno "
    "delle fermate). Una fermata e' il numero del foro o della piazzola che la quota "
    "raggiunge; se piu' fori stanno sulla stessa linea di quota scrivili insieme separati da "
    "virgola; scrivi \"edge\" per il contorno del corpo, solo come prima o ultima fermata. "
    "Includi le quote fra parentesi (di riferimento) e quelle verso il contorno del corpo. "
    "Ogni foro numerato deve comparire in almeno una catena x e una catena y. Non saltare "
    "nessuna quota della catena e non sommarne due. Dichiara in drawing_unit l'unita' delle "
    "quote e in contacts_view se il disegno e' una vista dal basso (BOTTOM VIEW, lato "
    "saldature) o dall'alto. Non specchiare e non ruotare nulla. Se un foro non ha un numero e "
    "non si riconosce dalla vista del pinout, non includerlo. Rispondi SOLO chiamando lo "
    "strumento."
)

#: Default model of the dedicated pad-position reading (profile field
#: 'pad_model', user, 2026-10-30). Chosen on a bench of real calls on three
#: dimensioned drawings (two relays, one DC-DC converter): it read all 11
#: right, ~0.03 USD each; the default analysis model read 1 in 9 and no
#: transcription format fixed that. A small sample: a capable model lowers
#: the error rate, it does not replace the checks of apply_dedicated_pads.
DEFAULT_PAD_POSITIONS_MODEL = "openai/gpt-5.6-sol"

#: Completion budget of the dedicated pad-position reading. Models that
#: reason before answering spend most of it before the tool call (up to
#: ~3300 tokens measured on the bench); 4096 cut some of them, and a cut
#: answer is paid and lost. Only the tokens used are billed.
PAD_POSITIONS_MAX_TOKENS = 16384

_PAD_POSITIONS_TOOL = {
    "name": "report_pad_positions",
    "description": ("Catene di quote del disegno dei fori o delle piazzole numerate, nell'unita' "
                    "dichiarata: le coordinate le ricava il programma."),
    "input_schema": {
        "type": "object",
        "properties": {
            "drawing_unit": {"type": "string", "enum": ["mm", "inch", "mil"]},
            "contacts_view": {"type": "string", "enum": ["top", "bottom", "unknown"]},
            "chains": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "axis": {"type": "string", "enum": ["x", "y"],
                             "description": "x: orizzontale, verso destra; y: verticale, verso il basso."},
                    "stops": {"type": "array", "items": {"type": "string"},
                              "description": ("Fermate in ordine crescente sulla pagina: numeri dei "
                                              "fori sulla stessa linea di quota separati da virgola, "
                                              "oppure \"edge\" (contorno del corpo) agli estremi.")},
                    "values": {"type": "array", "items": {"type": "number"},
                               "description": "Quote stampate fra fermate consecutive, nello stesso ordine."},
                }, "required": ["axis", "stops", "values"]},
            },
            "notes": {"type": "string"},
        },
        "required": ["drawing_unit", "contacts_view", "chains"],
    },
}

#: Reading tolerance, in mm, between two readings of the same distance: the
#: rounding of a quote read twice (two decimals in mm, three in inches), not
#: a margin on the part. Beyond it the two readings disagree. The same bound
#: applies to two chains reaching one pad and to a chain closed on the body.
PAD_READING_TOLERANCE_MM = 0.1

#: Stop of a dimension chain on the body outline (only first or last).
CHAIN_EDGE = "edge"

#: Graph nodes of the two outline stops of an axis (never a pad number).
_CHAIN_LOW, _CHAIN_HIGH = "<edge-min>", "<edge-max>"


def _pad_positions_tool() -> dict:
    """The dedicated pad-position schema in OpenRouter's "function" wrapper."""
    return {"type": "function", "function": {
        "name": _PAD_POSITIONS_TOOL["name"],
        "description": _PAD_POSITIONS_TOOL["description"],
        "parameters": _PAD_POSITIONS_TOOL["input_schema"]}}


def _chain_node_name(node: str) -> str:
    """A pad number, or «bordo» for an outline stop, in messages."""
    return tx("bordo") if node in (_CHAIN_LOW, _CHAIN_HIGH) else node


def _chain_stop_names(stop) -> list:
    """Pad numbers of one chain stop (comma-separated), stripped."""
    return [name.strip() for name in str(stop).split(",") if name.strip()]


#: Rounding of ONE printed quote, in mm, keyed by the drawing unit's factor
#: to mm (_UNIT_TO_MM): half a unit of the coarsest usual precision -- 0.1 mm,
#: 0.01 in, 1 mil. A sum of n quotes may drift by n times this;
#: _sum_tolerance never goes below PAD_READING_TOLERANCE_MM.
CHAIN_QUOTE_ROUNDING_MM = {1.0: 0.05, 25.4: 0.127, 0.0254: 0.0127}


def _sum_tolerance(quotes: int, factor: float = 1.0) -> float:
    """Tolerance, mm, of a sum of `quotes` printed quotes (rounding only, no part tolerance)."""
    rounding = CHAIN_QUOTE_ROUNDING_MM.get(factor, CHAIN_QUOTE_ROUNDING_MM[1.0])
    return max(PAD_READING_TOLERANCE_MM, rounding * quotes)


def _axis_from_chains(chains, axis: str, factor: float, problems: list, malformed: list) -> tuple:
    """Solve one axis: (positions in mm, quotes summed to reach each node) of the main component.

    Nodes are pad numbers plus the two outline stops; a chain gives
    position(next) - position(previous) = value, and the pads of one stop
    share a position. A chain that cannot be read (stops and values that do
    not match, an outline stop in the middle, a non-positive value) goes to
    `malformed`: a transcription slip, not a fact about the drawing. Two
    paths giving one distance differently go to `problems`: the drawing, as
    transcribed, contradicts itself. Pads outside the component holding most
    pads are left out (nothing printed ties them to the others).
    """
    links = {}

    def link(a, b, value, quotes):
        links.setdefault(a, []).append((b, value, quotes))
        links.setdefault(b, []).append((a, -value, quotes))

    for index, chain in enumerate(chains, start=1):
        if not isinstance(chain, dict) or chain.get("axis") != axis:
            continue
        stops, values = chain.get("stops"), chain.get("values")
        if (not isinstance(stops, list) or not isinstance(values, list) or len(stops) < 2
                or len(values) != len(stops) - 1):
            malformed.append(tx("catena {axis} n. {index}: {stops} fermate e {values} quote",
                                axis=axis, index=index,
                                stops=len(stops) if isinstance(stops, list) else 0,
                                values=len(values) if isinstance(values, list) else 0))
            continue
        groups, valid = [], True
        for position, stop in enumerate(stops):
            names = _chain_stop_names(stop)
            if [name.lower() for name in names] == [CHAIN_EDGE]:
                # The outline closes a chain; in the middle it means nothing.
                if 0 < position < len(stops) - 1:
                    valid = False
                    break
                names = [_CHAIN_LOW if position == 0 else _CHAIN_HIGH]
            elif not names or any(name.lower() == CHAIN_EDGE for name in names):
                valid = False
                break
            groups.append(names)
        lengths = [_optional_number(value, positive=True) for value in values]
        if not valid or any(length is None for length in lengths):
            malformed.append(tx("catena {axis} n. {index}: fermate o quote non valide",
                                axis=axis, index=index))
            continue
        for names in groups:
            for other in names[1:]:
                link(names[0], other, 0.0, 0)
        for previous, following, length in zip(groups, groups[1:], lengths):
            link(previous[0], following[0], length * factor, 1)
    positions, quotes, components, reported = {}, {}, [], set()
    for start in links:
        if start in positions:
            continue
        positions[start], quotes[start] = 0.0, 0
        component, queue = [start], [start]
        while queue:
            node = queue.pop()
            for other, step, count in links[node]:
                expected = positions[node] + step
                if other not in positions:
                    positions[other] = expected
                    quotes[other] = quotes[node] + count
                    component.append(other)
                    queue.append(other)
                    continue
                # The loop closed here sums at most this many printed quotes.
                tolerance = _sum_tolerance(quotes[node] + quotes[other] + count, factor)
                if (abs(positions[other] - expected) > tolerance
                        and frozenset((node, other)) not in reported):
                    # A distance, not a position: the origin of the sums is arbitrary.
                    reported.add(frozenset((node, other)))
                    problems.append(tx(
                        "fra {a} e {b} le catene {axis} danno {direct:.2f} mm e, per un'altra "
                        "via, {other:.2f} mm", a=_chain_node_name(node), b=_chain_node_name(other),
                        axis=axis, direct=abs(step), other=abs(positions[other] - positions[node])))
        components.append(component)
    if not components:
        return {}, {}
    main = max(components,
               key=lambda nodes: sum(1 for n in nodes if n not in (_CHAIN_LOW, _CHAIN_HIGH)))
    return {node: positions[node] for node in main}, {node: quotes[node] for node in main}


def pads_from_chains(chains, factor: float = 1.0) -> dict:
    """@brief Pad centres added up from the dimension chains the model transcribed.

    @param chains `chains` of the report_pad_positions answer: per chain an
        axis ("x" to the right, "y" down the page), its stops in increasing
        order (pad numbers sharing a dimension line, comma-separated, or
        CHAIN_EDGE for the body outline at either end) and the printed
        values between consecutive stops.
    @param factor Drawing unit to millimetres.
    @return {"pads": {number: (x, y)} in mm from an arbitrary origin,
        "problems": [contradictions], "malformed": [unreadable chains],
        "spans": {"x": mm or None, "y": mm or None}, "span_quotes": {axis:
        quotes summed for the span}, "unplaced": [number]}.
    @note The sums are the program's, never the model's: a model that adds a
        chain up wrong (a quote dropped while summing) cannot pass that error
        on. What remains is the transcription; it is checked where the
        drawing allows it: two chains reaching one pad must agree, and a
        chain from outline to outline gives a span to compare with the body
        (apply_dedicated_pads). A pad that no chain ties to the others on
        both axes is listed in "unplaced", never guessed.
    @note Limits, declared: these checks see only what changes a SUM. Two
        quotes transposed, two pad labels swapped or a whole axis read in
        reverse keep every distance sum and every span, so the result is
        never trusted over another reading on their strength (see
        apply_dedicated_pads). Stops are pad centres or the body outline: a
        datum that is neither (a centreline, an unnumbered post), quotes to a
        pad's EDGE (common in SMD land patterns) and ordinate dimensioning
        cannot be expressed; such pads stay unplaced or fail a check.
    """
    problems, malformed = [], []
    chains = chains if isinstance(chains, list) else []
    solved, spans, span_quotes = {}, {}, {}
    for axis in ("x", "y"):
        solved[axis], quotes = _axis_from_chains(chains, axis, factor, problems, malformed)
        ends = (solved[axis].get(_CHAIN_LOW), solved[axis].get(_CHAIN_HIGH))
        spans[axis] = ends[1] - ends[0] if None not in ends else None
        # Upper bound: the span runs through the BFS root at worst.
        span_quotes[axis] = (quotes[_CHAIN_LOW] + quotes[_CHAIN_HIGH]
                             if None not in ends else 0)
    names = (set(solved["x"]) | set(solved["y"])) - {_CHAIN_LOW, _CHAIN_HIGH}
    mentioned = set()
    for chain in chains:
        if isinstance(chain, dict) and isinstance(chain.get("stops"), list):
            for stop in chain["stops"]:
                mentioned.update(name for name in _chain_stop_names(stop)
                                 if name.lower() != CHAIN_EDGE)
    pads = {name: (solved["x"][name], solved["y"][name]) for name in names
            if name in solved["x"] and name in solved["y"]}
    if pads:
        # Readable origin for the messages (only distances are compared): the
        # outline's low stop when known, else the lowest pad on that axis.
        shift = []
        for index, axis in enumerate(("x", "y")):
            low = solved[axis].get(_CHAIN_LOW)
            shift.append(low if low is not None else min(p[index] for p in pads.values()))
        pads = {name: (x - shift[0], y - shift[1]) for name, (x, y) in pads.items()}
    return {"pads": pads, "problems": problems, "malformed": malformed, "spans": spans,
            "span_quotes": span_quotes, "unplaced": sorted(mentioned - set(pads))}


def read_pad_positions(documents, *, model, call, prompt=None, progress=None) -> Optional[dict]:
    """@brief Ask the model, alone, for the dimension chains of the numbered pads.

    @param documents Image documents of `numbering_documents`.
    @param model Model identifier of the analysis.
    @param call The logged, cancellable call wrapper of the analysis.
    @param prompt System prompt; None means DEFAULT_PAD_POSITIONS_PROMPT.
    @param progress Optional log callback.
    @return None without documents; otherwise {"view", "pads": {number: (x, y) in mm},
        "notes"} plus the chain results of pads_from_chains ("problems",
        "spans", "edges", "unplaced"), or "failed"/"discarded" with the reason
        and empty "pads".
    @note Best-effort: a failure never loses the main answer. The model
        transcribes the chains; the program converts the declared unit and
        adds them up (pads_from_chains). The origin is arbitrary and is never
        compared, only distances are.
    """
    if not documents:
        return None
    blocks = []
    for index, path in enumerate(documents):
        blocks.append({"type": "text", "text": f"FILE {index}: {path.name}"})
        blocks.append(_file_to_content_block(path, model))
    blocks.append({"type": "text", "text": (
        "Trascrivi le catene di quote dei fori o delle piazzole numerate del disegno e chiama "
        "lo strumento.")})
    try:
        data = call(model=model, system_prompt=prompt or DEFAULT_PAD_POSITIONS_PROMPT,
                    content_blocks=blocks, tool=_pad_positions_tool(),
                    max_tokens=PAD_POSITIONS_MAX_TOKENS, has_pdf=False)
    except (OpenRouterError, ToolArgumentsError) as exc:
        if progress:
            progress(tx("Lettura dedicata delle posizioni dei pad non riuscita ({exc}).", exc=exc))
        return {"view": "unknown", "pads": {}, "notes": "", "failed": str(exc)}
    if not isinstance(data, dict):
        return {"view": "unknown", "pads": {}, "notes": "", "discarded": tx("risposta non strutturata")}
    view = data.get("contacts_view")
    view = view if view in ("top", "bottom", "unknown") else "unknown"
    solved = pads_from_chains(data.get("chains"), _UNIT_TO_MM.get(data.get("drawing_unit"), 1.0))
    notes = data.get("notes")
    return dict(solved, view=view, notes=notes if isinstance(notes, str) else "")


def _pad_summary(pads: dict) -> str:
    """'1@(2.54,0.00), 4@(0.00,7.62)' for the log and the conflict message."""
    return ", ".join(f"{number}@({x:.2f},{y:.2f})"
                     for number, (x, y) in sorted(pads.items(), key=lambda item: item[0]))


def _offset_disagreements(first: dict, second: dict, common, tolerance: float) -> list:
    """Pairs of `common` pads whose SIGNED offsets (dx, dy) differ between two readings.

    Signed offsets, not distances: both readings describe the drawing as it
    appears on the page (X to the right, Y down), so a mirrored or reversed
    transcription, or two labels swapped on a symmetric layout, changes the
    signs while keeping every distance (adversarial review, 2026-10-30). The
    origins are arbitrary and cancel in the offsets.
    """
    pairs = []
    for i, a in enumerate(common):
        for b in common[i + 1:]:
            da = (first[b][0] - first[a][0], first[b][1] - first[a][1])
            db = (second[b][0] - second[a][0], second[b][1] - second[a][1])
            if abs(da[0] - db[0]) > tolerance or abs(da[1] - db[1]) > tolerance:
                pairs.append((a, b, da, db))
    return pairs


def _offset_pairs(pairs) -> str:
    """'1-4: (0.00, 5.08) mm contro (-2.54, 7.62) mm; ...' for the conflict messages."""
    return "; ".join(
        tx("{a}-{b}: ({mx:.2f}, {my:.2f}) mm contro ({dx:.2f}, {dy:.2f}) mm", a=a, b=b,
           mx=da[0], my=da[1], dx=db[0], dy=db[1]) for a, b, da, db in pairs[:6])


def _write_chain_pads(data: dict, pins: list, candidate: dict, factor: float) -> None:
    """Write the chain sums of `candidate` into `pins`, body centre at the origin when known."""
    spans = candidate["spans"]
    shift = (spans["x"] / 2, spans["y"] / 2) if candidate["centred"] else (0.0, 0.0)
    for pin in pins:
        sx, sy = candidate["pads"][pin["number"]]
        pin["x_mm"] = round((sx - shift[0]) / factor, 4)
        pin["y_mm"] = round((sy - shift[1]) / factor, 4)
    data["package_geometry"]["origin"] = "body_center" if candidate["centred"] else "custom"


def _extend_chain_conflict(data: dict, extra: str, remark: str, progress) -> None:
    """Keep the main answer's own conflict and append what the dedicated reading added."""
    data["_pin_map_conflict"] = data["_pin_map_conflict"] + " " + extra + remark
    data["_numbering_note"] = data["_pin_map_conflict"]
    if progress:
        progress(data["_pin_map_conflict"])


class _HoleVerdicts:
    """Memoised check of pad layouts against the holes drawn in the documents.

    `check` is the `check_layout` of apply_dedicated_pads (None: no check,
    every layout unverified); `body` the body outline (along page X, along
    page Y) in mm, or None. A layout is checked once per distinct set of
    positions. It also carries what a choice by the holes must respect: the
    view each reading declares, whether two declared views differ, and the
    readings already discarded that still state a scale (_settle_by_holes).
    """

    def __init__(self, check, body, *, views=None, views_differ=False, discarded=()):
        """@brief Keep the check, the body outline and the context; nothing is checked yet.

        @param check The `check_layout` callable, or None.
        @param body Body outline (page X, page Y) in mm, or None.
        @param views {reading key: declared view} ("top", "bottom", "unknown").
        @param views_differ True when two readings declare different views.
        @param discarded Pad layouts of readings left out of the choice (e.g.
            chains contradicting the body), still compared by scale.
        """
        self.check, self.body, self.cache = check, body, {}
        self.views = dict(views or {})
        self.views_differ = views_differ
        self.discarded = list(discarded)

    def __call__(self, pads: dict):
        key = tuple(sorted((number, round(x, 4), round(y, 4)) for number, (x, y) in pads.items()))
        if key not in self.cache:
            verdict = None
            if self.check is not None:
                try:
                    verdict = self.check(dict(pads), self.body)
                except Exception as exc:  # noqa: BLE001 - a local check never loses a paid answer
                    # A local check that cannot run (OpenCV included) confirms
                    # nothing; it never stops the analysis.
                    verdict = _HoleFailure(type(exc).__name__)
            self.cache[key] = verdict
        return self.cache[key]


class _HoleFailure:
    """A hole check that could not run: unverifiable, with the exception type as reason."""

    status, image, max_error_mm, tolerance_mm, circles = "unverifiable", None, None, None, 0

    def __init__(self, reason):
        """@brief Record why the check could not run.

        @param reason The exception type name.
        """
        self.reason = reason


def _holes_confirm(verdict) -> bool:
    """True only for a hole check that positively confirmed the layout."""
    return getattr(verdict, "status", None) == "confirmed"


def _hole_text(verdict) -> str:
    """One clause saying what the drawn holes said about one layout."""
    if verdict is None:
        return tx("controllo dei fori disegnati non eseguito")
    image = verdict.image or "?"
    reason = verdict.reason
    if verdict.status == "confirmed":
        return tx("i fori disegnati in {image} coincidono (scarto massimo stimato {error:.2f} "
                  "mm, tolleranza {tolerance:.2f} mm)", image=image,
                  error=verdict.max_error_mm or 0.0, tolerance=verdict.tolerance_mm or 0.0)
    if reason == "distorted":
        return tx("i fori disegnati in {image} coincidono solo deformando il disegno", image=image)
    if reason == "body_scale":
        return tx("i fori disegnati in {image} coincidono solo con una scala diversa da quella "
                  "del contorno del corpo", image=image)
    if verdict.status == "not_confirmed":
        return tx("i fori disegnati in {image} non coincidono con queste posizioni", image=image)
    if reason == "not_a_drawing":
        return tx("nessuna immagine e' un disegno a linee: i fori non si possono controllare")
    if reason == "too_few_circles":
        return tx("nel disegno non si trovano abbastanza fori circolari da controllare")
    if reason == "too_many_pads":
        return tx("troppi pad per il controllo dei fori disegnati")
    if reason == "too_few_pads":
        return tx("meno di due pad: niente da controllare sui fori disegnati")
    if reason == "no_image":
        return tx("nessuna immagine leggibile per il controllo dei fori")
    return tx("controllo dei fori disegnati non riuscito ({reason})", reason=reason)


def _pad_block_remedy(pins) -> str:
    """What the user can do when the pad positions are blocked.

    Only through-hole pads have a manual editor («Piazzole del componente
    THT», shown for single THT parts); for any other pad the way out is a
    better input for the readings.
    """
    if pins and all(isinstance(pin, dict) and pin.get("pad_type", "thru_hole") == "thru_hole"
                    for pin in pins):
        return tx("Inserisci le posizioni a mano in «Piazzole del componente THT» e usa «Rigenera "
                  "footprint», oppure ripeti l'analisi con un modello piu' capace per la lettura "
                  "delle posizioni dei pad.")
    return tx("Aggiungi il disegno quotato delle piazzole come immagine con ruolo pinout o vista "
              "dall'alto, oppure ripeti l'analisi con un modello piu' capace per la lettura delle "
              "posizioni dei pad.")


def _reading_source(key: str) -> str:
    """The name of one reading in the messages."""
    return {"main": tx("coordinate della risposta principale"),
            "chains": tx("catene di quote della risposta principale"),
            "dedicated": tx("lettura dedicata")}[key]


def _hole_verdicts(readings, holes) -> str:
    """'<reading>: <what the holes said>; ...' for the block messages."""
    return "; ".join(f"{_reading_source(key)}: {_hole_text(holes(pads))}"
                     for key, pads, _spans, _centred in readings)


def _pick_by_holes(readings, holes):
    """The one reading the drawn holes confirm while they confirm none of the others.

    @param readings [(key, pads)] of complete readings (every pin placed).
    @param holes _HoleVerdicts.
    @return (key, pads, verdict) of that reading, or None: none confirmed, or
        more than one (they differ only where the holes cannot see, e.g. two
        pad numbers exchanged on the same positions), so nothing is chosen.
    @note The holes check GEOMETRY, never the numbering: the chosen reading's
        pad numbers are its own and the note says they are unverified.
    @note Two more conditions (adversarial review of drawing_holes,
        2026-10-31): every other reading must fail with "no_match" -- a pad
        landing where no hole is. A reading that fails only as "distorted"
        or "body_scale" may be the right one on a drawing not to scale, and
        an unverifiable one was not checked at all: neither lets another
        reading win. And the chosen layout's MIRROR, when it is a different
        point set, must not fit the same holes: a page printing both a top and
        a bottom view fits a mirrored reading too, so it cannot tell the side.
    """
    confirmed = [(key, pads, holes(pads)) for key, pads in readings
                 if _holes_confirm(holes(pads))]
    if len(confirmed) != 1:
        return None
    key, pads, _verdict = confirmed[0]
    for other_key, other in readings:
        verdict = holes(other)
        if other_key != key and (getattr(verdict, "status", None) != "not_confirmed"
                                 or getattr(verdict, "reason", None) != "no_match"):
            return None
    mirrored = {number: (-x, y) for number, (x, y) in pads.items()}
    if not _same_point_set(pads, mirrored) and _holes_confirm(holes(mirrored)):
        return None
    return confirmed[0]


def _same_point_set(first: dict, second: dict) -> bool:
    """True when two layouts are the same set of positions up to a translation (numbers ignored)."""
    def shape(pads):
        x0 = min(x for x, _y in pads.values())
        y0 = min(y for _x, y in pads.values())
        return sorted((x - x0, y - y0) for x, y in pads.values())

    if len(first) != len(second) or not first:
        return False
    return all(math.hypot(a[0] - b[0], a[1] - b[1]) <= PAD_READING_TOLERANCE_MM
               for a, b in zip(shape(first), shape(second)))


def _settle(data: dict, note: str, progress) -> None:
    """Lift the block: the pad positions are decided, `note` says how."""
    data.pop("_chain_candidate", None)
    data.pop("_pin_map_conflict", None)
    data["_numbering_note"] = note
    if progress:
        progress(note)


def _block(data: dict, message: str, progress) -> None:
    """Block the automatic generation of the pad positions with `message` and the remedy."""
    data["_pin_map_conflict"] = message + " " + _pad_block_remedy(
        data.get("package_geometry", {}).get("pins"))
    data["_numbering_note"] = data["_pin_map_conflict"]
    if progress:
        progress(data["_pin_map_conflict"])


def _settle_by_holes(data: dict, pins: list, readings: list, holes, factor: float,
                     progress, why: str, remark: str):
    """Decide between disagreeing or unconfirmed readings by the drawn holes alone.

    @param readings [(key, pads, spans, centred)]: key "main" (the model's
        coordinates, already in the pins), "chains" (the main answer's own
        chains) or "dedicated"; `spans` and `centred` say whether the
        reading may be written with the body centre as origin
        (_write_chain_pads). Only readings placing every pin count.
    @param why What the readings did not settle, for the note.
    @return The key of the reading chosen and written; "blocked" when the
        chosen reading disagrees in SCALE with another one (the block is
        written here); None when the holes decide nothing.
    @note What the holes cannot see is never decided by them (adversarial
        review, 2026-10-31): the VIEW (the check never mirrors, so a top and
        a bottom reading of one layout fit the same circles) -- declared views
        that differ are never settled here -- and the absolute SCALE (the fit
        scales freely; the body bounds only a ratio taken from the same
        answer) -- a reading of the same shape at another scale blocks. The
        chosen reading's declared view is written with its pads.
    """
    if holes.views_differ:
        return None
    numbers = {str(pin.get("number", "")).strip() for pin in pins if isinstance(pin, dict)}
    complete = [(key, pads) for key, pads, _spans, _centred in readings
                if numbers <= set(pads)]
    chosen = _pick_by_holes(complete, holes)
    if chosen is None:
        return None
    key, pads, verdict = chosen
    others = [p for k, p, _s, _c in readings if k != key] + holes.discarded
    for other in others:
        ratio = _scale_ratio(pads, other)
        if ratio is not None:
            _block(data, tx("I fori disegnati confermano la forma di una lettura ({source}), ma "
                            "un'altra lettura ha la stessa forma in scala {ratio:.3f}: i fori non "
                            "dicono quale scala e' giusta (unita' di misura o quote lette male).",
                            source=_reading_source(key), ratio=ratio) + remark, progress)
            return "blocked"
    spans, centred = next((s, c) for k, _p, s, c in readings if k == key)
    if key != "main":
        _write_chain_pads(data, pins, {"pads": pads, "spans": spans, "centred": centred}, factor)
    view = holes.views.get(key) or "unknown"
    _adopt_view(data, view)
    _settle(data, tx("Posizioni dei pad scelte dal controllo dei fori disegnati. Lettura "
                     "usata: {source} ({holes}); nessun'altra lettura coincide con i fori "
                     "({why}). I fori verificano le posizioni, non i numeri dei pad ne' la vista "
                     "dichiarata ({view}): controllarli sul disegno.",
                     source=_reading_source(key), holes=_hole_text(verdict), why=why,
                     view=view) + remark, progress)
    return key


def _adopt_view(data: dict, view) -> None:
    """Write a declared view ("top"/"bottom") where the pads take it from: `source_view`."""
    if view in ("top", "bottom"):
        data["contacts_view"] = view
        data["package_geometry"]["source_view"] = view


def _scale_ratio(chosen: dict, other: dict):
    """The uniform scale between two layouts of the SAME shape, or None.

    None when they share fewer than two pads, differ in shape (beyond
    PAD_READING_TOLERANCE_MM once scaled), or have the same scale. The
    ratio is fitted on the common pads' offsets from their centroid.
    """
    common = sorted(set(chosen) & set(other))
    if len(common) < 2:
        return None
    cx = sum(chosen[n][0] for n in common) / len(common)
    cy = sum(chosen[n][1] for n in common) / len(common)
    ox = sum(other[n][0] for n in common) / len(common)
    oy = sum(other[n][1] for n in common) / len(common)
    a = [(chosen[n][0] - cx, chosen[n][1] - cy) for n in common]
    b = [(other[n][0] - ox, other[n][1] - oy) for n in common]
    norm = sum(x * x + y * y for x, y in a)
    if norm <= 0:
        return None
    ratio = sum(x * u + y * v for (x, y), (u, v) in zip(a, b)) / norm
    shape = max(math.hypot(u - ratio * x, v - ratio * y) for (x, y), (u, v) in zip(a, b))
    extent = max(math.hypot(x, y) for x, y in a)
    if shape > PAD_READING_TOLERANCE_MM or abs(ratio - 1.0) * extent <= PAD_READING_TOLERANCE_MM:
        return None
    return ratio


def _resolve_chain_candidate(data: dict, pins: list, main: dict, reading: dict,
                             views_differ: bool, factor: float, progress, remark: str,
                             holes=None, exchanged: bool = False) -> None:
    """Settle the main answer's own disagreement (its coordinates against its chains) by the dedicated reading.

    The dedicated chains, summed by the program, side with whichever of the
    two describes the same layout by signed offsets over EVERY pin, with the
    same declared view: that one is kept and the conflict is lifted. Siding
    with neither keeps the block, extended with the third reading, unless
    the drawn holes confirm exactly one of the three (_settle_by_holes).

    The closure requirement is deliberately one-sided. Siding with the
    CHAINS needs none: two transcriptions summed by the program agree, and
    coordinates disagreeing with their own chains point at the model's
    arithmetic. Siding with the model's COORDINATES needs the dedicated
    reading closed on the body on both axes and compared with a declared
    body size: two readings never checked against the drawing may share one
    misread quote (the reported failure mode) and must not outvote chains
    the program summed. A drawing with no outline quote, or an answer with
    no body size, can therefore not be auto-confirmed when the model mis-sums:
    a false block at worst. Limit, declared: two calls of one model can
    repeat one transcription slip (a label swap) and agree on it; the result
    stays «needs_review».
    """
    holes = holes if holes is not None else _HoleVerdicts(None, None)
    candidate = data["_chain_candidate"]
    dedicated = reading["pads"]
    numbers = sorted(pin["number"] for pin in pins)
    # The candidate exists only when every pin has coordinates (see
    # _place_pins_from_chains); checked again so a reshaped answer cannot
    # raise here.
    complete = (not views_differ and set(numbers) <= set(dedicated)
                and set(numbers) <= set(main))
    spans = reading.get("spans") or {}
    # «Closed» means VERIFIED: a span exists on both axes AND a body size to
    # compare it with (apply_dedicated_pads has already rejected a mismatch).
    # A span with no body size proves nothing (review 5, finding A).
    geometry = data["package_geometry"]
    dedicated_closed = all(
        spans.get(axis) is not None and _optional_number(geometry.get(key), positive=True) is not None
        for axis, key in (("x", "body_length_mm"), ("y", "body_width_mm")))
    summary = _pad_summary(dedicated) or tx("nessuno")
    readings = [("main", main, {}, False),
                ("chains", candidate["pads"], candidate["spans"], candidate["centred"])]
    if not views_differ:
        readings.append(("dedicated", dedicated, spans, _centred(spans, exchanged)))
    if complete and not _offset_disagreements(candidate["pads"], dedicated, numbers,
                                              PAD_READING_TOLERANCE_MM):
        _write_chain_pads(data, pins, candidate, factor)
        note = tx("Posizioni dei pad prese dalle catene di quote della risposta principale: la "
                  "lettura dedicata concorda con esse (scostamenti entro {tolerance} mm) e non "
                  "con le coordinate calcolate dal modello.", tolerance=PAD_READING_TOLERANCE_MM)
    elif complete and not _offset_disagreements(main, dedicated, numbers, PAD_READING_TOLERANCE_MM):
        if not dedicated_closed:
            # Two readings that were never checked against the drawing may
            # share one misread quote (the reported failure mode): without a
            # closure on the body they cannot outvote chains the program
            # summed. Only a closed dedicated reading, or the drawn holes,
            # may side with the model's own sums.
            extra = tx("Lettura dedicata ({view}): {dedicated}. Concorda con le coordinate "
                       "calcolate dal modello, ma non e' chiusa sul contorno del corpo: non "
                       "basta a preferirle alle catene di quote.", view=reading["view"],
                       dedicated=summary)
            return _block_chain_conflict(data, pins, readings, holes, factor, extra, remark,
                                         progress)
        note = tx("Posizioni dei pad confermate dalla lettura dedicata ({count} pad, scostamenti "
                  "entro {tolerance} mm); le catene di quote della risposta principale, che non "
                  "concordavano, sono state scartate.", count=len(numbers),
                  tolerance=PAD_READING_TOLERANCE_MM)
    elif not complete:
        extra = tx("Lettura dedicata ({view}): {dedicated}. Incompleta o con vista diversa "
                   "dalla risposta principale: non basta a decidere.", view=reading["view"],
                   dedicated=summary)
        return _block_chain_conflict(data, pins, readings, holes, factor, extra, remark, progress)
    else:
        extra = tx("Lettura dedicata ({view}): {dedicated}. Non concorda ne' con le coordinate "
                   "del modello ne' con le sue catene di quote.", view=reading["view"],
                   dedicated=summary)
        return _block_chain_conflict(data, pins, readings, holes, factor, extra, remark, progress)
    _settle(data, note + remark, progress)


def _block_chain_conflict(data, pins, readings, holes, factor, extra, remark, progress) -> None:
    """Keep the main answer's chain conflict, unless the drawn holes pick one reading."""
    if _settle_by_holes(data, pins, readings, holes, factor, progress,
                        tx("le letture non concordano"), remark):
        return
    message = (data["_pin_map_conflict"] + " " + extra + " "
               + tx("Controllo dei fori disegnati: {verdicts}.",
                    verdicts=_hole_verdicts(readings, holes)) + remark)
    _block(data, message, progress)


def _main_pads(pins, factor: float):
    """The main answer's pads in mm ({number: (x, y)}) and the numbers given twice."""
    main, seen, repeated = {}, set(), set()
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        x = _optional_number(pin.get("x_mm"))
        y = _optional_number(pin.get("y_mm"))
        number = str(pin.get("number", "")).strip()
        if number in seen:
            repeated.add(number)
        seen.add(number)
        if number and x is not None and y is not None:
            main[number] = (x * factor, y * factor)
    return main, repeated


def _centred(spans: dict, exchanged: bool) -> bool:
    """Whether a chain reading may be written with the body centre as origin (outline on both axes)."""
    return spans.get("x") is not None and spans.get("y") is not None and not exchanged


def _body_on_page(geometry: dict, factor: float, exchanged: bool):
    """Body outline (along page X, along page Y) in mm for the hole check, or None."""
    sizes = [_optional_number(geometry.get(key), positive=True)
             for key in ("body_length_mm", "body_width_mm")]
    if None in sizes:
        return None
    length, width = (size * factor for size in sizes)
    return (width, length) if exchanged else (length, width)


def apply_dedicated_pads(data: dict, reading: Optional[dict], progress=None,
                         check_layout=None) -> None:
    """@brief Confront the dedicated pad reading with the explicit pads of the main answer.

    @param data Main answer with `package_geometry` pins, still in drawing
        units and view (before `_canonicalize_answer`).
    @param reading Result of `read_pad_positions`, or None.
    @param progress Optional log callback.
    @param check_layout Optional callable(pads, body) -> drawing_holes.LayoutCheck:
        the local check of a layout ({number: (x, y)} in mm, page
        orientation, any origin) against the holes drawn in the documents,
        `body` the outline (page X, page Y) in mm or None. None checks
        nothing (every layout unverified).
    @note Rules, the same for every part: the two readings choose their own
        origins, so SIGNED OFFSETS between pads of the same number are
        compared, both in millimetres, within PAD_READING_TOLERANCE_MM.
        Confirmed: every common pair agrees, the declared views agree and the
        dedicated reading placed every pin. Otherwise the positions are NOT
        confirmed and the generation is BLOCKED (`_pin_map_conflict`; user,
        2026-10-30: a footprint built on an unconfirmed reading came out
        wrong), with one way out: the holes drawn in the documents confirm
        exactly ONE of the readings at hand and none of the others -- that
        one is written. This covers no dedicated reading (failed, discarded,
        no pad), two readings that disagree, a partial dedicated reading and
        a chain reading that contradicts itself or the body.
    @note The hole check is independent of both models (drawing_holes: circles
        found by the program, a least-squares fit of each reading). It sees
        GEOMETRY only: two pad numbers exchanged on the same positions pass
        it, so it never decides between such readings, and a note asks to
        check the numbering of the reading it chose. When the two readings
        agree but the holes do not confirm them, they stand with a warning:
        two independent readings are the confirmation, and the detection may
        have matched another view of the page.
    @note Dimension chains (user, 2026-10-30): the dedicated reading is added
        up by the program from the printed chains (pads_from_chains), so it
        can be checked against the drawing itself. Chains that contradict
        each other, or a chain from outline to outline whose sum differs from
        the body (`package_geometry` body_length_mm along X, body_width_mm
        along Y, or the two exchanged) by more than the rounding of the
        quotes summed (_sum_tolerance), discard the dedicated reading EVEN
        when it agrees with the main answer. Chains that cannot be read are
        only named in the note.
    @note A body given as a maximum or with a one-sided tolerance is used as
        printed (nominal): a span off by more than the rounding blocks, a
        false block at worst, never a wrong footprint. Pads sharing a number
        (shield tabs, doubled pins) merge into one stop in the chains, so the
        chain checks are skipped for such parts and said so.
    """
    geometry = data.get("package_geometry")
    pins = geometry.get("pins") if isinstance(geometry, dict) else None
    if not isinstance(pins, (list, tuple)) or not pins:
        return
    factor = _UNIT_TO_MM.get(data.get("drawing_unit"), 1.0)
    main, repeated = _main_pads(pins, factor)
    # Pads sharing a number collapse into one entry: no hole check on them,
    # never an adoption (adversarial review, 2026-10-31).
    check_layout = None if repeated else check_layout
    main_view = data.get("contacts_view")
    candidate = data.get("_chain_candidate")
    own = ([("chains", candidate["pads"], candidate["spans"], candidate["centred"])]
           if candidate is not None else [])
    if not reading or not reading["pads"]:
        if reading and reading.get("failed"):
            why = tx("chiamata non riuscita: {exc}", exc=reading["failed"])
        elif reading and reading.get("discarded"):
            why = tx("risposta scartata: {exc}", exc=reading["discarded"])
        elif reading is None:
            why = tx("lettura non eseguita")
        elif reading.get("problems") or reading.get("malformed"):
            # Chains too broken to place any pad: say why, not just «none read».
            why = "; ".join((list(reading.get("problems") or [])
                             + list(reading.get("malformed") or []))[:6])
        else:
            why = tx("nessun pad letto")
        if reading and reading.get("skipped"):
            why = reading["skipped"]
        holes = _HoleVerdicts(check_layout, _body_on_page(geometry, factor, False),
                              views={"main": main_view, "chains": main_view})
        _unconfirmed(data, pins, [("main", main, {}, False)] + own, holes, factor,
                     progress, tx("lettura dedicata delle posizioni: {why}", why=why), "")
        return
    dedicated = reading["pads"]
    common = sorted(set(main) & set(dedicated))
    disagreements = _offset_disagreements(main, dedicated, common, PAD_READING_TOLERANCE_MM)
    views_differ = (main_view in ("top", "bottom") and reading["view"] in ("top", "bottom")
                    and main_view != reading["view"])
    problems, exchanged = ([], False) if repeated else _chain_contradictions(geometry, reading,
                                                                              factor)
    holes = _HoleVerdicts(check_layout, _body_on_page(geometry, factor, exchanged),
                          views={"main": main_view, "chains": main_view,
                                 "dedicated": reading["view"]},
                          views_differ=views_differ, discarded=[dedicated] if problems else [])
    if problems:
        # The drawing contradicts the transcribed chains: at least one quote
        # was read or attributed wrongly, so the dedicated reading is out;
        # only the drawn holes can still confirm the main answer.
        why = tx("le quote del disegno non tornano: {problems}. Lettura per catene di quote "
                 "({dedicated_view}): {dedicated}", problems="; ".join(problems[:6]),
                 dedicated_view=reading["view"], dedicated=_pad_summary(dedicated) or tx("nessuno"))
        _unconfirmed(data, pins, [("main", main, {}, False)] + own, holes, factor,
                     progress, why, "")
        return
    remarks = []
    if repeated:
        remarks.append(tx("catene non verificate: piu' pad con lo stesso numero ({numbers})",
                          numbers=", ".join(sorted(repeated))))
    if exchanged:
        remarks.append(_exchanged_body_remark(reading))
    remarks.extend(reading.get("malformed") or [])
    remark = (" " + tx("Nota sulla lettura per catene: {remarks}.", remarks="; ".join(remarks[:6]))
              if remarks else "")
    if candidate is not None:
        _resolve_chain_candidate(data, pins, main, reading, views_differ, factor, progress,
                                 remark, holes, exchanged)
        return
    readings = [("main", main, {}, False)]
    if not views_differ:
        spans = reading.get("spans") or {}
        readings.append(("dedicated", dedicated, spans, _centred(spans, exchanged)))
    if len(common) < 2:
        _unconfirmed(data, pins, readings, holes, factor, progress, tx(
            "la lettura dedicata ha in comune con la risposta principale meno di due pad "
            "numerati ({read})", read=", ".join(sorted(dedicated)) or tx("nessuno")), remark)
        return
    if not disagreements and not views_differ:
        if set(main) <= set(dedicated):
            note = tx("Posizioni dei pad confermate dalla lettura dedicata ({count} pad, "
                      "scostamenti entro {tolerance} mm).", count=len(common),
                      tolerance=PAD_READING_TOLERANCE_MM)
            verdict = holes(main)
            if verdict is not None and verdict.status == "not_confirmed":
                note += " " + tx("Attenzione: {holes}; controllare il disegno.",
                                 holes=_hole_text(verdict))
            if main_view not in ("top", "bottom"):
                # Same positions, and only the dedicated reading says from
                # which side: its view is the one the pads are seen from.
                _adopt_view(data, reading["view"])
            _settle(data, note + remark, progress)
            return
        _unconfirmed(data, pins, readings, holes, factor, progress, tx(
            "la lettura dedicata conferma solo {read} pad su {total}", read=len(common),
            total=len(main)), remark)
        return
    why = tx("Le due letture delle posizioni dei pad non coincidono{views}. Scostamenti diversi: "
             "{pairs}. Risposta principale ({main_view}): {main}. Lettura dedicata "
             "({dedicated_view}): {dedicated}",
             views=tx(" (e dichiarano viste diverse)") if views_differ else "",
             pairs=_offset_pairs(disagreements) or tx("nessuna"), main_view=main_view or "unknown",
             dedicated_view=reading["view"], main=_pad_summary(main),
             dedicated=_pad_summary(dedicated))
    if _settle_by_holes(data, pins, readings, holes, factor, progress, why, remark):
        return
    _block(data, why + ". " + tx("Controllo dei fori disegnati: {verdicts}.",
                                 verdicts=_hole_verdicts(readings, holes))
           + " " + tx("Nessuna delle due e' stata scelta.") + remark, progress)


def _unconfirmed(data, pins, readings, holes, factor, progress, why, remark) -> None:
    """Positions no second reading confirms: the drawn holes decide, or the generation is blocked."""
    if _settle_by_holes(data, pins, readings, holes, factor, progress, why, remark):
        return
    own = (data["_pin_map_conflict"] + " ") if data.get("_chain_candidate") else ""
    _block(data, own + tx("Posizioni dei pad NON confermate ({why}). Controllo dei fori "
                          "disegnati: {verdicts}.", why=why,
                          verdicts=_hole_verdicts(readings, holes)) + remark, progress)


def _layout_checker(documents):
    """The `check_layout` of apply_dedicated_pads on `documents`: their drawn holes (drawing_holes)."""
    def check(pads, body):
        from .drawing_holes import check_pad_layout
        return check_pad_layout(list(documents), pads, body)
    return check


def _pad_reading_model(pad_model, main_model: str, component_family, progress,
                       openrouter_check) -> str:
    """The model of the dedicated pad-position reading: `pad_model` when its provider is ready.

    Only a single part has that reading; blank means the main model. A
    provider other than the main model's is checked here (key and credit);
    when it is not ready the main model reads, and the log says why: an
    optional, more capable reader never refuses the run.
    """
    chosen = (pad_model or "").strip()
    if component_family not in SINGLE_COMPONENT_FAMILIES or not chosen or chosen == main_model:
        return main_model
    from .providers import ProviderError, ensure_providers_ready, provider_of
    if provider_of(chosen) != provider_of(main_model):
        try:
            ensure_providers_ready([chosen], progress, openrouter_check=openrouter_check)
        except (OpenRouterError, ProviderError) as exc:
            if progress:
                progress(tx("Modello della lettura delle posizioni dei pad {pad_model} non "
                            "utilizzabile ({exc}): legge il modello principale {model}.",
                            pad_model=chosen, exc=exc, model=main_model))
            return main_model
    return chosen


def _exchanged_body_remark(reading: dict) -> str:
    """The note for body sizes matching the chain spans the other way round (a footprint risk)."""
    spans = reading.get("spans") or {}
    return tx("le quote da bordo a bordo ({sx:.2f} x {sy:.2f} mm) corrispondono a lunghezza e "
              "larghezza del corpo scambiate: controllare l'orientamento del corpo",
              sx=spans.get("x") or 0.0, sy=spans.get("y") or 0.0)


def _chain_contradictions(geometry: dict, reading: dict, factor: float) -> tuple:
    """Contradictions of the chain reading with itself and with the body, as messages.

    The edge-to-edge spans are compared with body_length_mm (X) and
    body_width_mm (Y) of `geometry`, in drawing units times `factor`; when
    both axes fail but match the two body sizes exchanged, the body sizes
    were read in another frame and nothing is reported. The tolerance is the
    rounding of the quotes summed (_sum_tolerance), plus the body quote.

    @return (problems, exchanged): `exchanged` True when the spans match the
        body sizes the other way round (the pads must then not be centred on
        those sizes).
    """
    problems = list(reading.get("problems") or [])
    spans = reading.get("spans") or {}
    counts = reading.get("span_quotes") or {}
    sizes = {axis: _optional_number(geometry.get(key), positive=True)
             for axis, key in (("x", "body_length_mm"), ("y", "body_width_mm"))}

    def off(axis, size):
        span = spans.get(axis)
        if span is None or size is None:
            return False
        return abs(span - size * factor) > _sum_tolerance(int(counts.get(axis) or 0) + 1, factor)

    failing = [axis for axis in ("x", "y") if off(axis, sizes[axis])]
    if (failing == ["x", "y"] and not off("x", sizes["y"]) and not off("y", sizes["x"])):
        return problems, True
    for axis in failing:
        problems.append(tx(
            "lungo {axis} le quote da bordo a bordo sommano {span:.2f} mm, ma il corpo "
            "misura {body:.2f} mm", axis=axis.upper(), span=spans[axis],
            body=sizes[axis] * factor))
    return problems, False


def _numbering_summary(entries) -> str:
    """'1@(0,0), 7@(0,6)' for the log and the conflict message."""
    return ", ".join(f"{e['pin']}@({e['row']},{e['col']})"
                     for e in sorted(entries, key=lambda e: int(e["pin"])))


def apply_dedicated_numbering(data: dict, reading: Optional[dict], progress=None) -> None:
    """@brief Confront the dedicated numbering with the main answer's pin_map, before canonicalisation.

    @param data Main answer, still in drawing coordinates (`_canonicalize_answer`
        mirrors pin_map afterwards according to contacts_view).
    @param reading Result of `read_pin_numbering`, or None.
    @param progress Optional log callback.
    @note Rules, the same for every part: (a) no reading, or a reading with
        no printed number: the main pin_map stands and a note says the
        numbering was not confirmed; (b) the two readings place every pin
        the same way: confirmed; (c) they differ, or the main answer has no
        pin_map: the DEDICATED reading becomes pin_map (one task, read from
        the drawing alone), its view becomes contacts_view when the main
        answer left it unknown, and `_pin_map_conflict` carries both readings
        so the GUI blocks the automatic generation and shows them. The
        dedicated reading is compared only on the pins it read: pins of the
        main map that the dedicated call did not see are not a disagreement
        (an empty position cannot be read), they are dropped later by the
        populated_pins rule.
    """
    if not reading or not reading["pins"]:
        if reading and reading.get("failed"):
            why = tx("chiamata non riuscita: {exc}", exc=reading["failed"])
        elif reading and reading.get("discarded"):
            why = tx("risposta scartata: {exc}", exc=reading["discarded"])
        elif reading is None:
            why = tx("lettura non eseguita")
        else:
            why = tx("nessun numero letto")
        data["_numbering_note"] = tx(
            "Numerazione dei pin NON confermata dalla lettura dedicata ({why}): "
            "verificare la posizione dei pin sul disegno.", why=why)
        if progress:
            progress(data["_numbering_note"])
        return
    dedicated = {int(e["pin"]): (int(e["row"]), int(e["col"])) for e in reading["pins"]}
    main_entries = data.get("pin_map") if isinstance(data.get("pin_map"), (list, tuple)) else []
    main = {}
    for entry in main_entries:
        if isinstance(entry, dict):
            try:
                main[int(entry["pin"])] = (int(entry["row"]), int(entry["col"]))
            except (KeyError, TypeError, ValueError):
                continue
    main_view = data.get("contacts_view")
    views_differ = (main_view in ("top", "bottom") and reading["view"] in ("top", "bottom")
                    and main_view != reading["view"])
    if main_view not in ("top", "bottom") and reading["view"] in ("top", "bottom"):
        data["contacts_view"] = reading["view"]
    agree = all(main.get(pin) == position for pin, position in dedicated.items())
    if main and agree and not views_differ:
        if set(main) <= set(dedicated):
            note = tx("Numerazione dei pin confermata dalla lettura dedicata ({count} numeri "
                      "letti).", count=len(dedicated))
        else:
            note = tx("Numerazione dei pin confermata solo per {read} pin su {total} (i numeri "
                      "stampati); le altre posizioni seguono la risposta principale, non "
                      "verificate: controllarle sul disegno.", read=len(dedicated), total=len(main))
        data["_numbering_note"] = note
        if progress:
            progress(note)
        return
    data["pin_map"] = list(reading["pins"])
    data["_dedicated_map"] = True
    if views_differ:
        # The dedicated call looked at the drawing alone: its view wins, and
        # the disagreement is said, never silently resolved (review A, 2).
        data["contacts_view"] = reading["view"]
    if main or views_differ:
        data["_pin_map_conflict"] = tx(
            "Le due letture della numerazione non coincidono. Risposta principale ({main_view}): "
            "{main}. Lettura dedicata del disegno ({dedicated_view}, usata): {dedicated}. "
            "Controlla il footprint sul disegno: accettala rigenerando footprint e modello STEP, "
            "oppure ripeti l'analisi.",
            main_view=main_view or "unknown", dedicated_view=reading["view"],
            main=_numbering_summary([{"pin": p, "row": r, "col": c} for p, (r, c) in main.items()])
            or tx("nessuna"),
            dedicated=_numbering_summary(reading["pins"]))
        note = data["_pin_map_conflict"]
    else:
        note = tx("Numerazione dei pin letta dalla chiamata dedicata ({count} numeri): la "
                  "risposta principale non ne dava una.", count=len(dedicated))
    data["_numbering_note"] = note
    if progress:
        progress(note)


#: Component families that are ONE part, not a populated board: they get
#: the dedicated prompt and tool below (user, 2026-10-10).
SINGLE_COMPONENT_FAMILIES = ("single_tht", "single_smd")

#: System prompt of the single-component analysis. Short and about one
#: thing: reading the mechanical drawing of one part. No board photo, no
#: component inventory, no castellated sides; the pads are described one by
#: one, the regular grid is only a declared shortcut.
DEFAULT_SINGLE_SYSTEM_PROMPT = (
    "Sei un lettore di disegni meccanici di UN SOLO componente elettronico (connettore, "
    "rele', trasformatore, convertitore, circuito integrato, passivo): dal datasheet o dal "
    "disegno quotato ricavi le quote che servono per il footprint KiCad e per un solido 3D "
    "semplificato. Riporta le quote COSI' COME LE LEGGI: dichiara in drawing_unit l'unita' del "
    "disegno (mm, inch o mil) e in contacts_view da che lato e' vista la disposizione dei "
    "terminali (top, bottom o unknown); conversione e specchiatura le fa il programma. "
    "TERMINALI: descrivili uno per uno in pads, con il numero STAMPATO nel disegno, le "
    "coordinate del centro rispetto a un'origine che scegli e dichiari in pads_origin, il "
    "tipo (thru_hole con drill_mm, oppure smd), e la piazzola consigliata (pad_length_mm e "
    "pad_width_mm; per un foro circolare i due valori coincidono). Se le posizioni dei "
    "terminali sono quotate con catene di quote, trascrivile anche in pad_chains COSI' COME "
    "SONO STAMPATE: asse x verso destra o y verso il basso, fermate in ordine crescente sulla "
    "pagina (numeri dei terminali sulla stessa linea di quota separati da virgola, \"edge\" "
    "per il contorno del corpo solo come prima o ultima fermata), values con le quote fra "
    "fermate consecutive; le coordinate le ricava il programma sommandole, quindi non saltare "
    "e non accorpare quote. Usa grid SOLO se TUTTI i "
    "terminali stanno su una griglia a passo costante e lo dichiari: rows e cols sono le "
    "posizioni della griglia intera, vuote comprese, e populated_pins elenca quelle "
    "occupate. Distanze fra terminali che non sono multipli di un passo unico significano "
    "che la griglia NON esiste: usa pads. NUMERAZIONE: solo i numeri stampati accanto ai "
    "terminali; non estenderla per convenzione (pin header, DIP) e non inventare numeri per "
    "posizioni senza numero. FORI: se il disegno quota solo il diametro del terminale, "
    "riportalo in lead_diameter_mm e stima il foro come terminale piu' il gioco e la "
    "piazzola come foro piu' la corona, dichiarando entrambi \"estimated\". In lead_exit di' "
    "da quale faccia escono i terminali (bottom: dritti dal fondo; side: dai fianchi e "
    "piegati; unknown). CORPO: body_length_mm e body_width_mm sono le due quote in pianta "
    "(dalla vista dall'alto o dal basso, oppure le larghezze delle viste frontale e laterale); "
    "body_height_mm e' la quota VERTICALE del corpo in una vista frontale o laterale, dal "
    "piano di appoggio alla sommita', esclusa la sporgenza dei terminali: se nessuna vista "
    "mostra il corpo di profilo, ometti il campo invece di indovinarlo. pin_length_3d_mm e' la "
    "sporgenza dei terminali sotto il corpo. Per ogni quota dichiara in value_sources se l'hai letta (\"read\"), "
    "ricavata da quote lette (\"derived\") o stimata (\"estimated\"); una stima non deve mai "
    "sembrare una lettura. Se c'e' una foto del pezzo, da essa ricava solo body_color_hex, "
    "text_color_hex ed edge_fillets. Compila pinout con la funzione di ogni terminale se il "
    "documento la riporta. Scrivi in notes dubbi e incongruenze. Rispondi SOLO chiamando lo "
    "strumento."
)

#: Digests of former single-component defaults, replaced in the profile
#: database when still stored verbatim (see analysis_profiles).
SUPERSEDED_SINGLE_PROMPT_DIGESTS = frozenset({
    # 2026-10-10, first text: the height was asked from a side view only and
    # a front view with the body height quoted (VE12H-K relay) was omitted.
    "7e842bb65b2a4271b9d2700e72e86680984cec8a478bd2642b7ac831f47f1354",
    # 2026-10-30: the pad coordinates were summed by the model alone; the
    # dimension chains (pad_chains) were not asked, and the VE12H-K relay
    # lost one quote of a chain in both readings.
    "05ee7fa78de8f607612c7dd3bd378461ecfa6f5c2a34802aec5c2405cf214173",
})
#: Digests of former pin-numbering defaults (none yet: first version).
SUPERSEDED_NUMBERING_PROMPT_DIGESTS = frozenset()


def _single_component_tool() -> dict:
    """@brief The OpenRouter "function" tool of the single-component analysis.

    @return Tool definition `report_single_component`: the pads one by one as
        the primary form, an optional declared grid, body, lead and view
        fields, pinout, provenance. Built from the main schema's shared
        fragments (edge_fillets, pinout, value_sources) so the three stay one.
    """
    main = _EXTRACTION_TOOL["input_schema"]["properties"]
    pad = {
        "type": "object",
        "properties": {
            "number": {"type": "string", "description": "Numero STAMPATO accanto al terminale."},
            "name": {"type": "string", "description": "Funzione, se il documento la riporta."},
            "x_mm": {"type": "number", "description": "Centro, asse X (verso DESTRA nella vista "
                                                      "dichiarata), nell'unita' di drawing_unit."},
            "y_mm": {"type": "number", "description": "Centro, asse Y (verso il basso nella vista dichiarata)."},
            "pad_type": {"type": "string", "enum": ["thru_hole", "smd"]},
            "drill_mm": {"type": "number", "exclusiveMinimum": 0,
                         "description": "Foro, solo per thru_hole."},
            "pad_length_mm": {"type": "number", "exclusiveMinimum": 0,
                              "description": "Piazzola lungo X; diametro per una piazzola circolare."},
            "pad_width_mm": {"type": "number", "exclusiveMinimum": 0,
                             "description": "Piazzola lungo Y; diametro per una piazzola circolare."},
            "shape": {"type": "string", "enum": ["circle", "rect", "oval", "roundrect"]},
        },
        "required": ["number", "x_mm", "y_mm", "pad_type", "pad_length_mm", "pad_width_mm"],
    }
    grid = {
        "type": "object",
        "description": (
            "SOLO se tutti i terminali stanno su una griglia a passo costante. rows e cols "
            "sono le posizioni della griglia intera, vuote comprese; populated_pins le "
            "posizioni occupate; pin_map la posizione di ogni numero stampato."),
        "properties": {
            "rows": main["rows"], "cols": main["cols"], "pitch_mm": main["pitch_mm"],
            "row_spacing_mm": main["row_spacing_mm"], "pad_diameter_mm": main["pad_diameter_mm"],
            "drill_mm": main["drill_mm"], "populated_pins": main["populated_pins"],
            "pin_map": main["pin_map"],
            "body_offset_x_mm": main["body_offset_x_mm"],
            "body_offset_y_mm": main["body_offset_y_mm"],
        },
        "required": ["rows", "cols", "pad_diameter_mm", "drill_mm"],
    }
    properties = {
        "name": main["name"], "description": main["description"], "tags": main["tags"],
        "drawing_unit": main["drawing_unit"], "contacts_view": main["contacts_view"],
        "pads": {"type": "array", "items": pad,
                 "description": "Un elemento per terminale, nelle coordinate dichiarate in pads_origin."},
        "pads_origin": {"type": "string", "enum": ["body_center", "pin1", "custom"],
                        "description": "Origine delle coordinate di pads: centro del corpo, "
                                       "terminale 1, oppure un punto del disegno descritto in notes."},
        "pad_chains": dict(_PAD_POSITIONS_TOOL["input_schema"]["properties"]["chains"],
                           description=("Catene di quote dei terminali come stampate nel disegno, "
                                        "nell'unita' di drawing_unit: il programma le somma.")),
        "grid": grid,
        "body_length_mm": main["body_length_mm"], "body_width_mm": main["body_width_mm"],
        "body_height_mm": main["body_height_mm"], "pin_length_3d_mm": main["pin_length_3d_mm"],
        "lead_diameter_mm": main["lead_diameter_mm"], "lead_exit": main["lead_exit"],
        "body_color_hex": main["body_color_hex"], "text_color_hex": main["text_color_hex"],
        "edge_fillets": main["edge_fillets"], "pinout": main["pinout"],
        "value_sources": main["value_sources"],
        "confidence": main["confidence"], "notes": main["notes"],
    }
    return {"type": "function", "function": {
        "name": "report_single_component",
        "description": ("Quote di un singolo componente per footprint e 3D: terminali uno per uno "
                        "(pads) oppure, solo se dichiarata, una griglia a passo costante (grid)."),
        "parameters": {"type": "object", "properties": properties,
                       "required": ["drawing_unit", "contacts_view", "confidence"]}}}


def single_answer_to_data(answer: dict) -> dict:
    """@brief Bring an answer of `report_single_component` to the main answer's shape.

    @param answer The tool arguments as returned by the model.
    @return A dict in the shape of `report_pin_header_params` answers, so
        every step after the call (canonicalisation, validation, dedicated
        numbering, ExtractedParams) stays the same: `pads` becomes
        `package_geometry` (family unknown, origin from pads_origin, the
        declared view as source_view), `grid` is flattened into the matrix
        fields with layout «matrix».
    @throws AnswerShapeError When the answer has neither pads nor grid, or
        both, or is not an object: nothing to build a footprint from, and
        the same request may come back well formed.
    @note Through-hole pads with one size are circular: pad_length = pad_width
        = the diameter the model gave, shape circle unless it said otherwise.
    """
    if not isinstance(answer, dict):
        raise AnswerShapeError(tx("Risposta AI non strutturata."), raw=answer)
    pads = answer.get("pads")
    grid = answer.get("grid")
    has_pads = isinstance(pads, (list, tuple)) and len(pads) > 0
    has_grid = isinstance(grid, dict) and bool(grid)
    if not has_pads and not has_grid and any(
            key in answer for key in ("rows", "package_geometry", "castellated_sides")):
        # The main answer's shape, which the model may still produce beside
        # the schema (extra properties are not refused): taken as it is.
        return dict(answer)
    if has_pads == has_grid:
        raise AnswerShapeError(tx(
            "La risposta deve descrivere i terminali in pads OPPURE una griglia in grid "
            "(trovati: pads={pads}, grid={grid}).", pads=has_pads, grid=has_grid), raw=answer)
    data = {"layout": "matrix"}
    for key in ("name", "description", "tags", "drawing_unit", "contacts_view", "body_length_mm",
                "body_width_mm", "body_height_mm", "pin_length_3d_mm", "lead_diameter_mm",
                "lead_exit", "body_color_hex", "text_color_hex", "edge_fillets", "pinout",
                "value_sources", "confidence", "notes"):
        if key in answer:
            data[key] = answer[key]
    if has_grid:
        for key in ("rows", "cols", "pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm",
                    "populated_pins", "pin_map", "body_offset_x_mm", "body_offset_y_mm"):
            if key in grid:
                data[key] = grid[key]
        return data
    view = answer.get("contacts_view")
    pins = []
    for entry in pads:
        if not isinstance(entry, dict):
            continue
        pin = {"number": str(entry.get("number", "")).strip(), "x_mm": entry.get("x_mm"),
               "y_mm": entry.get("y_mm"), "pad_length_mm": entry.get("pad_length_mm"),
               "pad_width_mm": entry.get("pad_width_mm"),
               "pad_type": entry.get("pad_type", "thru_hole")}
        if entry.get("name"):
            pin["name"] = str(entry["name"])
        if entry.get("drill_mm") is not None:
            pin["drill_mm"] = entry["drill_mm"]
        pin["shape"] = entry.get("shape") or ("circle" if pin["pad_type"] == "thru_hole" else "roundrect")
        pins.append(pin)
    data["package_geometry"] = {
        "family": "unknown",
        "origin": answer.get("pads_origin") or "custom",
        "source_view": view if view in ("top", "bottom") else "top",
        "confidence": answer.get("confidence", "low"),
        "needs_review": True,
        "pins": pins,
    }
    for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
        if answer.get(key) is not None:
            data["package_geometry"][key] = answer[key]
    _place_pins_from_chains(data, answer.get("pad_chains"))
    return data


def _place_pins_from_chains(data: dict, chains) -> None:
    """Check the model's own pad coordinates against its dimension chains, summed by the program.

    User, 2026-10-30: the model transcribes the dimension chains beside its
    coordinates (pad_chains); the program adds them up (pads_from_chains).
    The chains count only when they place EVERY pin, contradict nothing and
    no two pins share a number (they would merge into one stop); otherwise
    the model's coordinates stay and a note says why. Then:
    * coordinates and chain sums describe the same layout (signed offsets,
      _offset_disagreements): the program's sums are written, with the body
      centre as origin when the outline is known on both axes and matches
      the body sizes (not exchanged);
    * they differ: NOTHING is chosen (adversarial review: a label swap or a
      reversed chain keeps every sum). The chain sums wait in
      `_chain_candidate`, `_pin_map_conflict` blocks the generation, and the
      dedicated reading, if any, settles it (_resolve_chain_candidate).
    """
    if not isinstance(chains, list) or not chains:
        return
    geometry = data["package_geometry"]
    pins = geometry["pins"]
    factor = _UNIT_TO_MM.get(data.get("drawing_unit"), 1.0)
    solved = pads_from_chains(chains, factor)
    numbers = [pin["number"] for pin in pins]
    missing = sorted(set(numbers) - set(solved["pads"]))
    # Contradictions between chains and with the body, then unreadable chains.
    problems, exchanged = _chain_contradictions(geometry, solved, factor)
    reasons = problems + list(solved["malformed"])
    if len(set(numbers)) != len(numbers):
        reasons.append(tx("piu' pad con lo stesso numero"))
    if missing:
        reasons.append(tx("terminali non raggiunti dalle catene: {numbers}",
                          numbers=", ".join(missing)))
    before = {}
    for pin in pins:
        x, y = _optional_number(pin.get("x_mm")), _optional_number(pin.get("y_mm"))
        if x is not None and y is not None:
            before[pin["number"]] = (x * factor, y * factor)
    unplaced = sorted(set(numbers) - set(before))
    if unplaced:
        # A pin the model left without coordinates cannot be compared: the
        # validation downstream reports it; the chains do not fill it in.
        reasons.append(tx("terminali senza coordinate: {numbers}", numbers=", ".join(unplaced)))
    if exchanged:
        note = _exchanged_body_remark(solved)
        data["notes"] = (str(data.get("notes") or "") + "\n" + note[:1].upper() + note[1:] + ".").strip()
    if reasons:
        note = tx("Coordinate dei pad prese dalle somme del modello, non dalle catene di quote "
                  "({reasons}).", reasons="; ".join(reasons[:6]))
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        return
    spans = solved["spans"]
    candidate = {"pads": solved["pads"], "spans": spans,
                 "centred": spans["x"] is not None and spans["y"] is not None and not exchanged}
    disagreements = _offset_disagreements(before, solved["pads"], numbers,
                                          PAD_READING_TOLERANCE_MM)
    if not disagreements:
        _write_chain_pads(data, pins, candidate, factor)
        return
    data["_chain_candidate"] = candidate
    data["_pin_map_conflict"] = tx(
        "Le coordinate dei pad calcolate dal modello non coincidono con le sue catene di quote "
        "sommate dal programma. Scostamenti diversi: {pairs}. Coordinate del modello: {main}. "
        "Somme delle catene: {chains}. Nessuna delle due e' stata scelta: decide la lettura "
        "dedicata, se concorda con una delle due; altrimenti controlla il disegno.",
        pairs=_offset_pairs(disagreements) or tx("nessuna"), main=_pad_summary(before),
        chains=_pad_summary(solved["pads"]))
    data["_numbering_note"] = data["_pin_map_conflict"]


def _openai_tool() -> dict:
    """The same schema in the "function" format required by OpenRouter.

    The JSON schema of the parameters is identical: only the wrapper changes
    (Anthropic uses ``input_schema``, the OpenAI-compatible API of OpenRouter
    uses ``function.parameters``).
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
    "completa: rows x cols restano quelli della griglia INTERA a passo costante, "
    "posizioni vuote comprese (due pin distanti un multiplo del passo sono tante "
    "colonne piu' in la' quanti sono i passi, non adiacenti). Per ogni quota che compili dichiara in value_sources (e, per i lati "
    "castellated, nel value_sources del lato) se l'hai letta (\"read\"), ricavata "
    "solo da quote lette (\"derived\") o stimata (\"estimated\"). Se un valore "
    "necessario non e' presente nei documenti, stimalo dalle altre quote o dalle "
    "regole di progetto del tipo di contatto, dichiaralo \"estimated\" e scrivi in "
    "notes da cosa lo hai ricavato: un valore stimato non deve mai sembrare letto. "
    "Per un componente singolo a foro passante riporta in lead_diameter_mm il diametro "
    "del terminale se e' quotato (e' il pin, non il foro) e in lead_exit da quale faccia "
    "escono i terminali (bottom: dritti dal fondo, side: dai fianchi e piegati, unknown): "
    "il programma usa il primo per controllare il foro e il secondo per controllare che "
    "i pin stiano nel corpo. "
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

# Public alias: the GUI ("Prompt LLM" tab) shows it as the editable starting
# text, and passes the modified version to extract_pin_header_params().
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
    # 2026-10-10: lead_diameter_mm and lead_exit added to the default prompt.
    "1b17e4a73222226e3547123f1e4c7b9ade87a100794ba89b0695ea3642885519",
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

    `layout` says which fields are meaningful: "matrix" fills in
    rows/columns/pitch/pad/drill, "castellated" fills in `castellated_sides`
    (one element for each side of the module) and leaves the matrix fields
    at None, since those components have none.

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
    @note `geometry_doubts` lists the structural contradictions of
        `package_geometry` (body covering the lead pads, with `swap_clears`
        when swapping body length and width would resolve it; clockwise lead
        numbering; bottom view also declared mirrored). The geometry is kept
        as read; each doubt's message is also in `notes`.
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
    #: Lead diameter read from the drawing (mm), None when not given.
    lead_diameter_mm: Optional[float] = None
    #: Both readings of the pin numbering when they disagree (the dedicated
    #: one is in pin_map); None when confirmed or not checked. The GUI blocks
    #: the automatic generation on it.
    pin_map_conflict: Optional[str] = None
    #: One of LEAD_EXITS, None when the model did not say.
    lead_exit: Optional[str] = None
    #: Structural doubts of `package_geometry` (core.package_geometry.package_doubts),
    #: each as GeometryDoubt.as_dict(): {"code", "message", "pins", "swap_clears"}.
    #: None when there is no package geometry or nothing is contradicted. The
    #: geometry is never corrected: the same messages are in `notes`, and a
    #: GUI can show `swap_clears` / the code to ask the user what is true.
    geometry_doubts: Optional[Sequence[dict]] = None

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
        """@brief Return whether body length, width and height are all known, so a 3D body can be built.

        @return True when none of the three body dimensions (mm) is None.
        """
        return self.body_length_mm is not None and self.body_width_mm is not None and self.body_height_mm is not None

    def is_castellated(self) -> bool:
        """@brief Return whether the contacts use the castellated (edge-by-edge) layout.

        @return True when `layout` is "castellated".
        """
        return self.layout == "castellated"


class ExtractionError(Exception):
    """@brief Application error of the extraction: missing key, invalid file or unexpected answer.

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


class AnswerShapeError(ExtractionError):
    """@brief The model's tool arguments parsed, but their shape contradicts the tool schema.

    @param message Translated diagnostic text.
    @param raw The arguments as the model sent them, for the AI log.
    @note Kept apart from a wrong VALUE: the shape of one answer can come back
        broken at random (deepseek-flash once flattened one pad to the top
        level, pads="1" beside x_mm/y_mm, on a request answered well twice
        out of three; 2026-10-31), so the same request earns the one paid
        retry given to unreadable arguments. A well-formed answer with wrong
        numbers is not this error and is never retried blindly.
    """

    def __init__(self, message: str, *, raw=None):
        """@brief Build the error, keeping the unusable arguments for the log.

        @param message Translated diagnostic text.
        @param raw The arguments as the model sent them (any JSON value).
        """
        super().__init__(message)
        import json
        try:
            self.raw = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=repr)
        except (TypeError, ValueError):
            self.raw = repr(raw)


#: Text appended to the retried request after an AnswerShapeError: it restates
#: the structural rule of `report_single_component` the answer broke, never a
#: value, so it holds for any component.
_SHAPE_CORRECTION = (
    "CORREZIONE: la risposta precedente non rispettava lo schema dello strumento. Richiama "
    "lo strumento con TUTTI i campi: pads e' una LISTA con un oggetto per ciascun terminale "
    "(number, x_mm, y_mm, pad_type, pad_length_mm, pad_width_mm, drill_mm se passante), mai i "
    "campi di un singolo pad al primo livello; grid solo se i terminali stanno su una griglia "
    "a passo costante, e mai insieme a pads.")


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
    except Exception:  # noqa: BLE001 - the log must not hide the real error
        pass


def _log_unreadable_arguments(exc, progress, attempt: int) -> None:
    """Write what the model sent when its tool arguments did not parse or broke the schema shape.

    `exc` is a ToolArgumentsError or an AnswerShapeError; both carry `raw`.

    One machine line in the AI log of the project (AI_RESPONSE_UNREADABLE
    attempt=N ...), next to AI_RESPONSE_REJECTED: before, the text was lost
    with the exception and a JSONDecodeError at «char 1874» could not be
    explained (user's run of 2026-10-02 18:20). Never cached.
    """
    if not progress:
        return
    raw = exc.raw if isinstance(exc.raw, str) else repr(exc.raw)
    if len(raw) > _REJECTED_RESPONSE_MAX_CHARS:
        raw = raw[:_REJECTED_RESPONSE_MAX_CHARS] + " …[truncated]"
    try:
        progress(f"AI_RESPONSE_UNREADABLE attempt={attempt} {raw}")
    except Exception:  # noqa: BLE001 - the log must not hide the real error
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

#: JPEG APP0 identifier kept: the JFIF header only declares density and colour
#: space, never the camera or the place.
_JPEG_JFIF_ID = b"JFIF\x00"
#: JPEG APP14 identifier kept: Adobe's segment tells the decoder how to convert
#: CMYK/YCCK colours, so dropping it would change the pixels.
_JPEG_ADOBE_ID = b"Adobe"

#: PNG chunks that describe pixels and are therefore kept; every other chunk
#: (eXIf, tEXt, zTXt, iTXt, iCCP, tIME, C2PA and any private chunk) is dropped.
#: acTL/fcTL/fdAT are the animation chunks of APNG, cICP/mDCv/cLLi the
#: colour-encoding chunks of the third edition: none carries free text.
_PNG_PIXEL_CHUNKS = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB",
    b"sBIT", b"bKGD", b"pHYs", b"hIST", b"acTL", b"fcTL", b"fdAT",
    b"cICP", b"mDCv", b"cLLi",
})

#: WebP chunks that describe pixels and are therefore kept (an allow-list,
#: like PNG): lossy and lossless bitstreams, the extended header, alpha and
#: animation. EXIF, XMP, ICCP, C2PA and any private chunk are dropped.
_WEBP_PIXEL_CHUNKS = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"})
#: VP8X flag bits announcing the metadata chunks (ICC 0x20, EXIF 0x08, XMP
#: 0x04): always cleared, since those chunks are never kept.
_WEBP_METADATA_FLAGS = 0x20 | 0x08 | 0x04


class _UncleanableImage(Exception):
    """An image that cannot be sent safely: its metadata or orientation cannot be handled.

    Private signal of the privacy boundary; `_file_to_content_block` turns it
    into a translated ExtractionError naming the file.
    """

    def __init__(self, reason: str):
        """@brief Record why the image cannot be sent.

        @param reason "metadata" (no cleaner understands the file) or
            "orientation" (a rotation tag that no decoder can apply).
        """
        super().__init__(reason)
        #: "metadata" (no cleaner understands the file) or "orientation"
        #: (a rotation tag that no decoder can apply).
        self.reason = reason


def _jpeg_without_metadata(data: bytes) -> Optional[bytes]:
    """The JPEG with its metadata segments removed, entropy-coded data untouched.

    Lossless: the compressed pixels are copied byte for byte, so quality and
    size do not change (re-encoding would lose quality at every upload).
    Removed: every APPn segment except the JFIF APP0 and the Adobe APP14
    (EXIF with GPS and device, XMP, ICC profile, IPTC, MPF and maker
    segments), every COM comment, and everything after the end-of-image
    marker (phones append there a second, full-metadata picture or a trailer).

    A file cut short (no end-of-image marker, or a scan truncated by a copy
    that did not finish) is cleaned up to the end of its data and closed with
    an end-of-image marker: the cleaning fails closed, it never falls back to
    the raw bytes, whose header still holds the metadata.

    @param data Encoded bytes.
    @return The cleaned bytes, or None when the data is not a JPEG or ends
        before its first scan (no pixels to send).
    """
    size = len(data)
    if size < 4 or data[:2] != b"\xff\xd8":
        return None
    out = bytearray(b"\xff\xd8")
    index = 2
    scanned = False  # True once compressed pixels have been copied

    def closed() -> Optional[bytes]:
        """The cleaned bytes so far, closed, when they hold pixels."""
        return bytes(out) + b"\xff\xd9" if scanned else None

    while index < size:
        if data[index] != 0xFF:
            return closed()
        while index < size and data[index] == 0xFF:  # fill bytes before a marker
            index += 1
        if index >= size:
            return closed()
        marker = data[index]
        index += 1
        if marker == 0xD9:  # end of image: whatever follows is not this picture
            out += b"\xff\xd9"
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            out += bytes((0xFF, marker))
            continue
        if index + 2 > size:
            return closed()
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > size:
            return closed()
        body = data[index + 2:index + length]
        segment = data[index - 2:index + length]
        index += length
        if marker == 0xFE:
            continue  # COM: free text
        if 0xE0 <= marker <= 0xEF:
            keep = ((marker == 0xE0 and body.startswith(_JPEG_JFIF_ID))
                    or (marker == 0xEE and body.startswith(_JPEG_ADOBE_ID)))
            if not keep:
                continue
        out += segment
        if marker == 0xDA:
            # Start of scan: entropy-coded data up to the next real marker
            # (0xFF followed by neither a stuffed 0x00, a restart marker nor
            # another fill byte). Progressive files have several scans: the
            # outer loop handles the segments between them.
            start = index
            while True:
                index = data.find(b"\xff", index)
                if index < 0 or index + 1 >= size:
                    # Scan cut short: keep the pixels that are there (a
                    # lone trailing 0xFF is half a marker, not data).
                    end = size - 1 if data.endswith(b"\xff") else size
                    out += data[start:end]
                    scanned = True
                    return closed()
                following = data[index + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
                    index += 1
                    continue
                break
            out += data[start:index]
            scanned = True
    return closed()


def _png_without_metadata(data: bytes) -> Optional[bytes]:
    """The PNG with only the chunks that describe pixels, compressed data untouched.

    @param data Encoded bytes.
    @return The cleaned bytes (chunks of `_PNG_PIXEL_CHUNKS`, up to IEND; any
        trailer after IEND dropped), or None when the data is not a PNG whose
        chunk list ends with IEND (a truncated PNG is then decoded and
        re-encoded, or refused).
    """
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return None
    out = bytearray(signature)
    index, size = len(signature), len(data)
    while index + 12 <= size:
        length = int.from_bytes(data[index:index + 4], "big")
        kind = data[index + 4:index + 8]
        end = index + 12 + length  # length + type + data + CRC
        if end > size:
            return None
        if kind in _PNG_PIXEL_CHUNKS:
            out += data[index:end]
        index = end
        if kind == b"IEND":
            return bytes(out)
    return None


def _webp_without_metadata(data: bytes) -> Optional[bytes]:
    """The WebP with only the chunks that describe pixels, compressed data untouched.

    @param data Encoded bytes.
    @return The cleaned bytes (chunks of `_WEBP_PIXEL_CHUNKS`) with the RIFF
        size and the VP8X metadata flags updated, or None when the data is
        not a well-formed RIFF/WEBP container holding a bitstream.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunks = []
    index, size = 12, len(data)
    while index + 8 <= size:
        kind = data[index:index + 4]
        length = int.from_bytes(data[index + 4:index + 8], "little")
        if index + 8 + length > size:
            return None
        end = index + 8 + length + (length & 1)  # chunks are padded to an even size
        if kind in _WEBP_PIXEL_CHUNKS:
            chunk = bytearray(data[index:min(end, size)])
            if len(chunk) & 1:
                chunk += b"\x00"  # a last chunk without its padding byte
            chunks.append(chunk)
        index = end
    if not any(chunk[:4] in (b"VP8 ", b"VP8L", b"ANMF") for chunk in chunks):
        return None
    for chunk in chunks:
        if chunk[:4] == b"VP8X" and len(chunk) > 8:
            chunk[8] &= ~_WEBP_METADATA_FLAGS & 0xFF
    body = b"WEBP" + b"".join(bytes(chunk) for chunk in chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _reencoded_without_metadata(data: bytes) -> Optional[bytes]:
    """The image decoded and saved again from its bare pixels, for other formats.

    Used for the formats without a lossless cleaner above (GIF), or for a
    container the walkers reject but PIL can decode. The new image is built
    from the pixel buffer alone, so no `info` entry (comment, XMP, ICC, EXIF)
    can reach the encoder whatever the Pillow version copies by default; only
    the palette and the transparency index, which define pixels, are copied.
    An animated GIF becomes its first frame: the models read a still image.

    @param data Encoded bytes.
    @return The re-encoded bytes in the same format, or None when PIL cannot
        decode the data.
    """
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            file_format = image.format or "PNG"
            image.load()
            bare = Image.frombytes(image.mode, image.size, image.tobytes())
            if image.mode == "P" and image.palette is not None:
                bare.putpalette(image.getpalette())
            options = {"quality": 95} if file_format.upper() in ("JPEG", "WEBP") else {}
            if "transparency" in image.info and file_format.upper() in ("GIF", "PNG"):
                options["transparency"] = image.info["transparency"]
            out = BytesIO()
            bare.save(out, format=file_format, **options)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - None tells the caller nothing could be cleaned
        return None


def _image_without_metadata(data: bytes) -> bytes:
    """The image bytes with every metadata block removed, pixels unchanged.

    Privacy boundary of every document upload (audit 2026-10-15, security
    M2): a phone photo carries the GPS position, the device make and model
    and capture times in EXIF/XMP, none of which the model needs to read a
    board. JPEG, PNG and WebP are cleaned losslessly by walking their
    container (`_jpeg_without_metadata`, `_png_without_metadata`,
    `_webp_without_metadata`); any other format, or a container those
    walkers reject, is decoded and re-encoded from its bare pixels.

    @param data Encoded image bytes.
    @return Metadata-free bytes in the same format.
    @throws _UncleanableImage ("metadata") When no cleaner applies and PIL
        cannot decode the data either (an HEIC photo renamed .jpg, a damaged
        file): it FAILS CLOSED, because the raw bytes may still carry the
        metadata (review 2026-10-15, M1). The caller refuses the upload.
    @note The ICC profile is dropped too: it can name the device, and the
        models read photos and drawings without colour management.
    """
    for cleaner in (_jpeg_without_metadata, _png_without_metadata,
                    _webp_without_metadata, _reencoded_without_metadata):
        cleaned = cleaner(data)
        if cleaned is not None:
            return cleaned
    raise _UncleanableImage("metadata")


class _UncleanablePdf(Exception):
    """A PDF that cannot be sent safely: the program cannot rebuild it without its metadata.

    Private signal of the privacy boundary for PDFs, the counterpart of
    `_UncleanableImage`; `_file_to_content_block` turns it into a translated
    ExtractionError naming the file.
    """


def _pdf_without_metadata(data: bytes) -> bytes:
    """The PDF rebuilt without its document metadata, pages and content unchanged.

    Privacy boundary of every PDF upload (review 2026-10-27, E1), the same
    one `_image_without_metadata` enforces for photos: a datasheet saved or
    printed by the user carries, in its Information dictionary (/Author,
    /Creator, /Producer, creation and modification dates) and in its XMP
    /Metadata stream, the user's name, the software and the machine's clock,
    none of which the model needs to read a drawing. Incremental updates may
    also keep an earlier metadata dictionary alive in the file: the output
    is written from scratch, so only the objects reachable from the new
    catalogue survive.

    Two passes are needed, not one: pypdf writes every object it cloned,
    including an XMP stream whose reference was removed, so the first pass
    drops the references and the second clones only what is still reachable.
    The result is then READ BACK and checked, so the boundary never depends
    on the library's internals: a PDF that still carries metadata after the
    rebuild is refused.

    @param data PDF bytes as read from the file.
    @return Metadata-free PDF bytes.
    @throws _UncleanablePdf When pypdf cannot read, decrypt or rewrite the
        file, or when the rebuilt file still carries metadata: it FAILS
        CLOSED, exactly like the image path, because the raw bytes may still
        carry the metadata. The caller refuses the upload.
    @note Attached files, page annotations and form fields are kept: they are
        part of the document the user chose to send, not metadata about it.
    """
    from io import BytesIO
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject

        def rebuilt(source: bytes) -> bytes:
            reader = PdfReader(BytesIO(source))
            if reader.is_encrypted and not reader.decrypt(""):
                raise _UncleanablePdf("encrypted")
            writer = PdfWriter(clone_from=reader)
            # Information dictionary: None removes /Info from the trailer
            # (pypdf would otherwise add its own /Producer).
            writer.metadata = None
            metadata = NameObject("/Metadata")
            if metadata in writer.root_object:
                del writer.root_object[metadata]
            for page in writer.pages:
                if metadata in page:
                    del page[metadata]
            out = BytesIO()
            writer.write(out)
            return out.getvalue()

        cleaned = rebuilt(rebuilt(data))
        check = PdfReader(BytesIO(cleaned))
        if (check.metadata or check.xmp_metadata is not None
                or any("/Metadata" in page for page in check.pages)):
            raise _UncleanablePdf("metadata")
        if len(check.pages) == 0:
            raise _UncleanablePdf("empty")
    except _UncleanablePdf:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure of the rebuild fails closed
        raise _UncleanablePdf("unreadable") from exc
    return cleaned


def _sniffed_extension(data: bytes) -> Optional[str]:
    """OpenCV file extension of the container, read from its magic bytes."""
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _oriented_with_opencv(data: bytes, orientation: int) -> Optional[bytes]:
    """The image rotated by OpenCV when PIL read the tag but cannot decode the pixels.

    A truncated phone photo still has its header: PIL reads the orientation
    but refuses the cut pixels, while OpenCV decodes what is there. The tag
    is applied here explicitly (decoding with IMREAD_IGNORE_ORIENTATION, so
    no decoder version applies it twice) with the same transforms as
    PIL's `ImageOps.exif_transpose`; OpenCV writes no metadata.

    @param data Encoded bytes (JPEG, PNG or WebP).
    @param orientation EXIF orientation 2..8.
    @return Re-encoded upright bytes in the same container (JPEG/WebP at
        quality 95, as the PIL path), or None when OpenCV cannot decode them.
    """
    try:
        import cv2
        import numpy as np
        extension = _sniffed_extension(data)
        if extension is None:
            return None
        pixels = cv2.imdecode(np.frombuffer(data, np.uint8),
                              cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if pixels is None:
            return None
        transforms = {
            2: lambda a: a[:, ::-1],                         # mirror left-right
            3: lambda a: a[::-1, ::-1],                      # rotate 180
            4: lambda a: a[::-1, :],                         # mirror top-bottom
            5: lambda a: a.transpose(1, 0, 2),               # main diagonal
            6: lambda a: np.rot90(a, -1),                    # rotate 90 clockwise
            7: lambda a: a[::-1, ::-1].transpose(1, 0, 2),   # anti-diagonal
            8: lambda a: np.rot90(a, 1),                     # rotate 90 counter-clockwise
        }
        upright = np.ascontiguousarray(transforms[orientation](pixels))
        params = ([cv2.IMWRITE_JPEG_QUALITY, 95] if extension == ".jpg"
                  else [cv2.IMWRITE_WEBP_QUALITY, 95] if extension == ".webp" else [])
        ok, encoded = cv2.imencode(extension, upright, params)
        return encoded.tobytes() if ok else None
    except Exception:  # noqa: BLE001 - None: the caller refuses the image
        return None


def _upright_image_bytes(data: bytes) -> bytes:
    """The image as the analysis and the preview see it, EXIF orientation applied, without metadata.

    Phones store the pixels one way and the right way in the orientation tag.
    OpenCV (the analysis) and the preview apply it; a provider may not, and
    the model would then read a rotated photo (user, 2026-09-25). A photo
    with a rotation tag is rotated and re-encoded in the same format; every
    photo, rotated or not, then loses its metadata (`_image_without_metadata`):
    sending an unrotated phone photo byte for byte used to leak its GPS
    position and device (audit 2026-10-15).

    When PIL reads a rotation tag but cannot decode the pixels (a truncated
    file), OpenCV rotates them (`_oriented_with_opencv`); when neither can,
    the image is refused rather than sent with its tag stripped and its
    pixels unrotated (review 2026-10-15, L7): the model would read it sideways
    while the analysis and the preview show it upright.

    @param data Encoded image bytes as read from the document file.
    @return Upright, metadata-free bytes in the same format.
    @throws _UncleanableImage "orientation" when a rotation tag cannot be
        applied; "metadata" when the metadata cannot be removed.
    """
    orientation = 1
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
        with Image.open(BytesIO(data)) as image:
            orientation = image.getexif().get(_EXIF_ORIENTATION, 1)
            if orientation not in (None, 1):
                file_format = image.format or "PNG"
                upright = ImageOps.exif_transpose(image)
                # The transposed copy may still hold EXIF/ICC/XMP in `info`;
                # they are removed below from the encoded result.
                out = BytesIO()
                options = {"quality": 95} if file_format.upper() in ("JPEG", "WEBP") else {}
                upright.save(out, format=file_format, **options)
                data = out.getvalue()
                orientation = 1
    except Exception:  # noqa: BLE001 - handled below with the orientation still pending
        pass
    if orientation in range(2, 9):
        # Cleaned first: the JPEG walker closes a truncated file with its
        # end-of-image marker, which OpenCV needs to decode it at all.
        rotated = _oriented_with_opencv(_image_without_metadata(data), orientation)
        if rotated is None:
            raise _UncleanableImage("orientation")
        data = rotated
    return _image_without_metadata(data)


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
        except (OSError, ValueError, cv2.error, _UncleanableImage):
            # An image refused by the privacy boundary gives no strips; the
            # upload of the photo itself reports the reason.
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


def _enlarged_image_bytes(data: bytes, model: Optional[str]) -> bytes:
    """The image enlarged to what the receiving model can resolve, when that helps.

    A dimensioned drawing cropped from a datasheet is often a few hundred
    pixels wide; a provider with a coarse visual token and an upsampling
    floor (DeepSeek, measured in HANDOFF 144) then reads a dimension label
    with about one token and the numbers come out different at every call
    (TEL8WI, 2026-10-10: seven analyses, seven readings). The crop path
    already enlarges for such providers (`photo_grounding.fit_to_model`);
    this applies the same declared limits to the documents of the analysis.

    @param data Encoded image bytes, already upright.
    @param model Model identifier; its provider decides the limits. None
        (caller without a model) sends the bytes unchanged.
    @return The same bytes when the provider does not gain from enlargement,
        the image already reaches its limits, decoding fails, or the enlarged
        PNG would exceed MAX_FILE_BYTES; otherwise a lossless PNG enlarged
        (bicubic) to the largest size within the provider's limits.
    @note Enlarging adds no information: it makes the provider spend more
        tokens on the same pixels. Never shrinks: shrinking is the provider's job.
    """
    if not model:
        # Caller without a model: the historical bytes, no assumed provider.
        return data
    from .photo_grounding import model_image_limits
    limits = model_image_limits(model)
    if not limits.enlarges:
        return data
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width < 1 or height < 1:
                return data
            scale = min(limits.long_side_px / max(width, height),
                        math.sqrt(limits.max_pixels / (width * height)))
            if scale <= 1.0:
                return data
            # A transparent background (RGBA, LA, palette with transparency:
            # the usual export of a vector drawing) would turn black through
            # convert("RGB"); it is composited on white first (review A, 3).
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
            enlarged = image.convert("RGB").resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                resample=Image.BICUBIC)
            out = BytesIO()
            enlarged.save(out, format="PNG")
            payload = out.getvalue()
    except Exception:  # noqa: BLE001 - the original file is never worse than failing
        return data
    return payload if len(payload) <= MAX_FILE_BYTES else data


def _file_to_content_block(path: Path, model: Optional[str] = None) -> dict:
    """A documentation file as an OpenRouter content block.

    Images travel as ``image_url`` with a base64 data URL, PDFs as a
    ``file`` block: these are the two formats foreseen by the
    OpenAI-compatible API of OpenRouter.

    @param path Document file (image or PDF).
    @param model Receiving model identifier, or None: an image is enlarged to
        that model's provider limits when the provider resolves more detail
        from an enlarged image (`_enlarged_image_bytes`); a PDF keeps its
        pages and is rebuilt without its document metadata
        (`_pdf_without_metadata`).
    @return The content block; an image always upright and without metadata,
        a PDF always without Information dictionary and XMP stream.
    @throws ExtractionError For an unsupported, unreadable (vanished,
        locked) or too large file, for an image whose metadata cannot be removed or whose rotation tag cannot
        be applied, and for a PDF the program cannot rebuild without its
        metadata: such a file is refused, never sent raw (fail closed).
    @note The 20 MB cap (MAX_FILE_BYTES) applies to the bytes that leave the
        computer: a rebuilt PDF is measured again after cleaning.
    """
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ExtractionError(
            tx("Formato non supportato: {name} (ammessi: {join})",
               name=path.name,
               join=', '.join(sorted(_SUPPORTED_SUFFIXES)))
        )
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ExtractionError(tx("{name} troppo grande ({size:.1f} MB, max {limit} MB)",
                                     name=path.name, size=size / 1_048_576,
                                     limit=MAX_FILE_BYTES // 1_048_576))
        media_type = _guess_media_type(path)
        payload = path.read_bytes()
    except OSError as exc:
        # A document removed, locked or unreadable since it was chosen: an
        # application error naming the file, not a raw OSError the caller
        # would report as «Errore API» (review 2026-10-29, E8). The system
        # reason only, without the absolute path (it names the user).
        raise ExtractionError(tx("{label}: file non leggibile: {error}", label=path.name,
                                 error=exc.strerror or type(exc).__name__)) from exc
    if media_type.startswith("image/"):
        try:
            payload = _upright_image_bytes(payload)
            enlarged = _enlarged_image_bytes(payload, model)
            if enlarged is not payload:
                payload, media_type = enlarged, "image/png"
                # The enlarged PNG is built from a decoded image whose `info`
                # may still carry an ICC profile: the privacy boundary is
                # applied again on the bytes that actually leave the computer.
                payload = _image_without_metadata(payload)
        except _UncleanableImage as exc:
            # Fail closed: nothing that may carry the GPS position or a
            # sideways picture leaves the computer (review 2026-10-15, M1/L7).
            if exc.reason == "orientation":
                raise ExtractionError(tx(
                    "{name}: immagine danneggiata con un'indicazione di rotazione che il "
                    "programma non riesce ad applicare; non viene inviata all'AI, che la "
                    "leggerebbe ruotata. Salvala di nuovo come JPEG o PNG e riprova.",
                    name=path.name)) from exc
            raise ExtractionError(tx(
                "{name}: immagine che il programma non riesce a leggere; non viene inviata "
                "all'AI perche' i suoi metadati (posizione GPS, dispositivo) non si possono "
                "rimuovere. Salvala come JPEG o PNG e riprova.", name=path.name)) from exc
    elif media_type == "application/pdf":
        try:
            payload = _pdf_without_metadata(payload)
        except _UncleanablePdf as exc:
            # Fail closed, as for an image: a PDF whose author, producer and
            # dates cannot be removed never leaves the computer (review
            # 2026-10-27, E1). The message tells the user the program could
            # not read it as a PDF, which is what every failure of the
            # rebuild amounts to; saving it again (e.g. «Print to PDF»)
            # produces a file the program can rebuild.
            raise ExtractionError(
                f"{_log_name(path.name)}: " + tx("Il file non contiene un PDF riconoscibile.")
            ) from exc
        if len(payload) > MAX_FILE_BYTES:
            # Rebuilding can inflate a file that was compressed with object
            # streams: the cap guards what is sent, not what was on disk.
            raise ExtractionError(tx("{name} troppo grande ({size:.1f} MB, max {limit} MB)",
                                     name=path.name, size=len(payload) / 1_048_576,
                                     limit=MAX_FILE_BYTES // 1_048_576))
    data_b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:{media_type};base64,{data_b64}"

    if media_type == "application/pdf":
        return {"type": "file", "file": {"filename": path.name, "file_data": data_url}}
    return {"type": "image_url", "image_url": {"url": data_url}}


def _pin_map_seen_from_top(
    pin_map: Optional[Sequence[dict]], cols: int, view: Optional[str],
    rows: Optional[int] = None,
) -> Optional[list]:
    """Convert the positions read from the drawing into coordinates seen from above.

    The KiCad footprint is drawn from the component side, while module
    datasheets almost always show a BOTTOM VIEW: without mirroring, the
    footprint would be a mirror image, with holes in positions that do not
    match the real component.

    The transformation is done here and not in the prompt because it is an
    exact geometric reflection: the model reads (a perceptual task), the
    code flips (a deterministic task).

    @param pin_map Entries {"pin", "row", "col"} as read from the drawing,
        or None/empty when the model gave no map.
    @param cols Number of columns of the matrix.
    @param view View the drawing shows ("bottom" mirrors the columns); any
        other value or None keeps the positions as read.
    @param rows Number of rows of the matrix when known, used only to
        validate the entries; None skips that check.
    @return The entries seen from above, or None when `pin_map` is empty.

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
        # The first contact met along the edge becomes the last: the two
        # end quotes swap with the direction.
        first, last = side.get("first_contact_from_edge_mm"), side.get("last_contact_from_edge_mm")
        if first is not None or last is not None:
            side["first_contact_from_edge_mm"], side["last_contact_from_edge_mm"] = last, first


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
    # A raw answer never carries the program's own measurement block: the
    # photo stage writes it AFTER this conversion (HANDOFF 169), and an
    # answer already converted keeps it (the early return above).
    data.pop("_measured_lands", None)
    data.pop("_measured_holes", None)
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
    """Compatibility entry for callers that use the inventory in millimetres.

    The groups are inventories built by the program, so their internal flags
    are kept (trusted).
    """
    from .photo_inventory import merge_components
    return merge_components([_components_3d(g, trusted=True) or [] for g in groups])


#: Maximum number of characters with which a file name is shown in the
#: progress line. It is a PRESENTATION fallback, not a rule on documents: it
#: does not depend on extension, content or brand.
#:
#: The modal now puts each document on its own line
#: (`analysis_overlay.enumeration_lines`), so a long name no longer pushes the
#: following names out: it shortens only ITSELF. The limit stays because a
#: card line holds about eighty-eight characters (the width the card is sized
#: for), and an entry is «nome — ruolo» (name — role): with the forty
#: characters here plus the separator and the longest role label among those
#: declared in `document_roles.ROLES` (thirty characters) the line stays below
#: that width, i.e. the role is always readable. Beyond the limit the card
#: would shorten the line anyway, but by cutting the TAIL, hence the role:
#: here the name is shortened in the middle, and the extension survives.
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
          troppo attaccati al testo», the file names are not highlighted, too
          close to the text). It replaced the comma, which the card
          could not tell apart from a comma inside a file name without guessing
          what a file name looks like.

        Replacing the middle dot with a semicolon, dropping the colon, or going
        back to the comma would silently take the list or the highlighting away
        without breaking anything else -- hence this note. The role labels
        never contain " — ", so the first occurrence in an item always ends the
        name unless the name itself contains the sign.
    """
    if not doc_paths:
        # Defensive invariant of the helper: the line describes a submission,
        # and without documents there is no submission to describe. An
        # «Invio a AI 0 file» (sending 0 files to the AI) would suggest a call
        # that went out empty, hence paid. The extraction caller rejects the
        # empty list earlier («Nessun file selezionato.» (no file selected)),
        # so this is not an expected case in production: it is the defined
        # behaviour of the helper for whoever reuses it, so the branch stays
        # testable and does not produce a wrong sentence if a second caller arrives.
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
    # Singular and plural are two whole sentences: each language inflects
    # them its own way, a pasted «o»/«i» suffix only works for Italian.
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
    crop_model: Optional[str] = None,
    document_roles: Optional[dict] = None,
    component_family: Optional[str] = None,
    photo_detector: str = "ai",
    yolo_model_path: Optional[Path] = None,
    smd_model_path: Optional[Path] = None,
    yolo_confidence: Optional[float] = None,
    yolo_settings: Optional[dict] = None,
    phase: Optional[Callable[..., None]] = None,
    review_proposals: Optional[Callable[..., object]] = None,
    cancel: Optional[object] = None,
    numbering_prompt: Optional[str] = None,
    pad_model: Optional[str] = None,
) -> ExtractedParams:
    """@brief Extract dimensions and pinout, optionally using local YOLO photo proposals.

    @param doc_paths Documentation files to send (PDF or images), at least one
        and at most MAX_FILES; each is checked for format and size before any
        paid call.
    @param model Provider model identifier of the main document call (e.g.
        the OpenRouter id "anthropic/claude-sonnet-5"): it allows a more
        capable model on difficult datasheets without touching the global
        default. None means DEFAULT_MODEL.
    @param system_prompt Override of DEFAULT_SYSTEM_PROMPT (or of
        DEFAULT_SINGLE_SYSTEM_PROMPT for a single-component family): the
        GLOBAL setting (the «Configurazione» menu of the GUI), valid for every component.
        None keeps the default. The tool schema stays fixed regardless: it is
        what guarantees that the answer is always valid structured JSON.
    @param extra_instructions Notes specific to THIS component ("the case is
        metal", "ignore the side connector"), appended to the user message
        together with the documents and forwarded to the photo inventory.
        None or blank adds nothing.
    @param include_nc_pins Whether the pins marked NC in the pinout are
        generated anyway (default True): they are physically present on the
        component, and without their pad the part would not fit the board.
        False asks the model to leave them out of the populated pins.
    @param progress Optional callable(str) receiving the log lines (documents
        sent, warnings, cost); None discards them. A failing callback never
        aborts the extraction.
    @param vision_model Model of the paid localization calls on the board
        photograph (photo inventory and terminal observation); None means
        photo_grounding.DEFAULT_VISION_MODEL.
    @param photo_cache_dir Folder of the photo response cache and of the
        saved photo evidence; None writes nothing and caches nothing.
    @param grounding_prompt Custom notes of the photo localization call
        (profile field), forwarded to the photo inventory; None sends its
        factory text.
    @param verification_prompt Custom notes of the photo verification call
        (profile field), forwarded to the photo inventory; None sends its
        factory text.
    @param verification_model Model of the paid verification/classification
        call on the photograph; None falls back to `model`, then
        DEFAULT_MODEL.
    @param terminal_prompt System prompt of the terminal observation
        (photo_terminals.observe_terminals), used only on the legacy 'ai'
        detector path; None or blank keeps that module's default.
    @param component_family Declared family of the chosen component type
        (e.g. CASTELLATED_FAMILY, "aggregate_tht", one of
        SINGLE_COMPONENT_FAMILIES): it selects the system prompt, the edge
        enlargements of the photo and the pin-field check
        (`fit_body_to_pin_field`). None means no family is declared.
    @param review_proposals Optional callable(photo_png, proposals,
        length_mm, width_mm) -> proposals kept, run between the local
        detector and the paid reconciliation (see
        photo_inventory.extract_inventory); returning None aborts the
        inventory. None skips the review.
    @param numbering_prompt System prompt of the dedicated pin-numbering call
        (`read_pin_numbering`); None means DEFAULT_PIN_NUMBERING_PROMPT.
    @param pad_model Model of the dedicated pad-position reading of a single
        part (profile field 'pad_model', e.g. DEFAULT_PAD_POSITIONS_MODEL);
        None or blank uses `model`. When its provider has no key or credit
        the reading falls back to `model` and the log says so: the run is
        not refused for an optional, more capable reader.

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
    @param crop_model Model of that per-body crop reading (profile field
        'crop_model', HANDOFF 150), same paths as `crop_prompt`. None or
        blank reads the crops with the model of the paid call on the
        photograph (`verification_model`, else `model`); a model set here is
        used for the crop readings only, and its provider is checked for a
        key and credit before the run like the others.
    @param photo_detector Photo localization backend: ai (legacy), yolo (local
        proposals plus one AI reconciliation) or vlm (no local detector: the
        vision model enumerates the bodies in that same single request).
    @param yolo_model_path Optional compatible ONNX file; validated before paid calls.
    @param smd_model_path Optional checkpoint of the local SMD network
        (photo_detector 'yolo_smd'); None uses models/smd/smd_classifier.pt.
        Validated before paid calls.
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
    @return The validated ExtractedParams, with the photo inventory in
        `components_3d` when a photograph was available, and every
        conversion, doubt and dropped value written in `notes`.
    @throws ExtractionError For any application problem: missing key or
        credit, unsupported or too large files, too many files, unknown photo
        detector, local YOLO not available, network/API errors, an answer
        without the tool call or unreadable even after the one retry, or an
        answer rejected by the validation. A main answer rejected by the
        validation keeps its content in `ExtractionError.raw_response` and in
        the AI log.
    @throws ExtractionCancelled When `cancel` is set before a paid call: no
        further call is paid.
    @note Two levels of instructions, both optional: `system_prompt` is the
        GLOBAL setting valid for every component, `extra_instructions` are
        the notes of the SINGLE component.
    @note YOLO localization is local; datasheet, package and terminal review still use AI.
    """
    # The cost tally concerns this run: reset it BEFORE any check, so that
    # even a validation rejection (YOLO, roles, files) does not show the cost
    # of the previous run as if it were its own.
    reset_usage()
    if not doc_paths:
        raise ExtractionError(tx("Nessun file selezionato."))
    if photo_detector not in ("ai", "yolo", "vlm", "yolo_smd"):
        raise ExtractionError(tx("Rilevatore fotografico non supportato."))
    if photo_detector in ("yolo", "yolo_smd"):
        if photo_detector == "yolo_smd" and yolo_model_path is None:
            # The shared rule (review 2026-10-29, P3): the checkpoint when it
            # is there, else its ONNX export -- the installed program has
            # only the export, and forcing the checkpoint refused the run.
            from ..app_paths import default_yolo_smd_model
            yolo_model_path = default_yolo_smd_model()
        from .photo_yolo import operating_confidence, validate_yolo_setup
        from .photo_yolo import yolo_settings as check_yolo_settings
        try:
            # Threshold and parameters are also checked here, before paying.
            operating_confidence(yolo_confidence)
            check_yolo_settings(yolo_settings)
            validate_yolo_setup(yolo_model_path)
            if photo_detector == "yolo_smd":
                from .smd_component_classifier import validate_smd_setup
                validate_smd_setup(smd_model_path)
        except (ValueError, OSError, ImportError, RuntimeError) as exc:
            raise ExtractionError(tx("YOLO locale non disponibile: {exc}", exc=exc)) from exc
    from .ai_diagnostics import make_logged_call
    # The application analysis must be auditable: the persistent log keeps
    # the exact text of the prompts/tools and the image hashes (never the bytes).
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
        # The credit must be checked here, before the first content block:
        # the extraction is a chain of dozens of calls (main analysis, crops,
        # verification, terminals) and running dry halfway means having spent
        # without getting a component. Every provider of the models used is
        # checked (OpenRouter and/or DeepSeek direct, 27/09).
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
             verification_model or model or DEFAULT_MODEL,
             # Blank = the photo model above; an empty entry is skipped.
             (crop_model or '').strip()],
            progress, openrouter_check=openrouter_ready)
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    def announce(tool, detail):
        """Reporting the phase is informational: a broken modal must not lose a
        paid processing, so the error is logged and the run goes on -- the
        same treatment given to `progress`."""
        if phase is None:
            return
        try:
            phase(tool, detail)
        except Exception as exc:  # noqa: BLE001 - the GUI must not be able to abort the analysis
            if progress:
                progress(tx("Segnalazione della fase non riuscita ({exc}); elaborazione proseguita.",
                            exc=exc))

    # Which tool is working cannot be inferred from the progress messages,
    # and the difference matters: this is a remote call that is paid for,
    # local detection is not.
    announce("llm", model or DEFAULT_MODEL)
    if progress:
        # The user must be able to check WHICH of their documents went into
        # the paid call, and with which declared role.
        progress(_files_sent_message(doc_paths, document_roles))
    content_blocks = []
    for index, path in enumerate(doc_paths):
        description = f"FILE {index}: {path.name}"
        if document_roles is not None:
            description += "\n" + _role_description(document_roles[path.name])
        content_blocks.append({"type": "text", "text": description})
        content_blocks.append(_file_to_content_block(path, model or DEFAULT_MODEL))
    if component_family == CASTELLATED_FAMILY and document_roles is not None:
        # The AI recognises the number of contacts in THIS call (user,
        # 28/09, point 142): the photo edges enlarged, not a correction afterwards.
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

    single_part = component_family in SINGLE_COMPONENT_FAMILIES

    def main_call(correction=None):
        """The paid reading of the documents (same request on a retry).

        A single part gets its own prompt and tool (pads one by one); the
        answer is brought to the main shape right here, so a retry and
        every later step see one shape only. `correction`, when given, is
        one more text block at the end of the request telling the model
        which part of the schema its previous answer broke.
        """
        blocks = content_blocks if correction is None else [
            *content_blocks, {"type": "text", "text": correction}]
        answer = diagnostic_call(
            model=model or DEFAULT_MODEL,
            system_prompt=system_prompt or (DEFAULT_SINGLE_SYSTEM_PROMPT if single_part
                                            else _SYSTEM_PROMPT),
            content_blocks=blocks,
            tool=_single_component_tool() if single_part else _openai_tool(),
            max_tokens=16384,
            has_pdf=has_pdf,
        )
        return single_answer_to_data(answer) if single_part else answer
    try:
        try:
            data = main_call()
        except (ToolArgumentsError, AnswerShapeError) as exc:
            # The model answered with the right tool, a complete answer (a
            # truncated one does not get here), but unreadable arguments or
            # arguments whose shape contradicts the schema: the SAME request
            # may come back well formed. A single new paid attempt, like the
            # global inventory (HANDOFF 137; user, 2026-10-02: «ok procedi»
            # (ok, go ahead)). Refusals, network and credit do not pass here.
            _log_unreadable_arguments(exc, progress, 1)
            check_cancel()
            if progress:
                progress(tx("Analisi dei documenti: la risposta del modello non era un JSON "
                            "leggibile (testo nel log AI, riga AI_RESPONSE_UNREADABLE); un solo "
                            "nuovo tentativo a pagamento con la stessa richiesta.")
                         if isinstance(exc, ToolArgumentsError) else
                         tx("Analisi dei documenti: la risposta del modello non aveva la forma "
                            "richiesta ({exc}; testo nel log AI, riga AI_RESPONSE_UNREADABLE); un "
                            "solo nuovo tentativo a pagamento, con la richiesta e l'indicazione "
                            "dell'errore.", exc=exc))
            try:
                # A broken shape is repeated as often as not when the SAME
                # request is sent again (deepseek-flash, real calls of
                # 2026-10-31): the retry names the schema rule it broke.
                data = main_call(_SHAPE_CORRECTION if isinstance(exc, AnswerShapeError) else None)
            except ToolArgumentsError as second:
                _log_unreadable_arguments(second, progress, 2)
                raise ExtractionError(
                    tx("Anche il nuovo tentativo ha restituito argomenti illeggibili; il testo "
                       "delle due risposte e' nel log AI (righe AI_RESPONSE_UNREADABLE). {exc}",
                       exc=second)) from second
            except AnswerShapeError as second:
                _log_unreadable_arguments(second, progress, 2)
                raise ExtractionError(
                    tx("Anche il nuovo tentativo ha restituito una risposta senza la forma "
                       "richiesta; il testo delle due risposte e' nel log AI (righe "
                       "AI_RESPONSE_UNREADABLE). {exc}", exc=second)) from second
            if progress:
                progress(tx("Analisi dei documenti: il nuovo tentativo e' leggibile; si usa quello."))
    except OpenRouterError as exc:
        raise ExtractionError(str(exc)) from exc

    if not isinstance(data, dict):
        error = ExtractionError(tx("Risposta AI non strutturata."))
        _log_rejected_response(data, error, progress)
        raise error
    raw_main = copy.deepcopy(data)
    try:
        _validate_main_response(data, content_blocks, diagnostic_call, model, system_prompt,
                                has_pdf, progress,
                                numbering={"documents": numbering_documents(doc_paths, document_roles),
                                           "pdf_only": _numbering_only_in_pdf(doc_paths, document_roles),
                                           "component_family": component_family,
                                           "announce": announce, "check_cancel": check_cancel,
                                           "prompt": numbering_prompt,
                                           "pad_model": _pad_reading_model(
                                               pad_model, model or DEFAULT_MODEL,
                                               component_family, progress,
                                               openrouter_ready),
                                           "pad_model_requested": (pad_model or "").strip()})
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

    check_cancel()
    _recheck_missing_contact_styles(data, content_blocks, diagnostic_call, model,
                                   has_pdf, progress)
    check_cancel()
    if progress:
        progress(
            tx("Analisi principale completata: layout={get}, componenti "
               "iniziali={n_items}.",
               get=data.get('layout', 'matrix'),
               n_items=len(data.get('components_3d') or []))
        )
    return _extract_photo_stage(
        data, doc_paths, progress=progress, announce=announce, check_cancel=check_cancel,
        cancelled=cancelled,
        document_roles=document_roles, component_family=component_family,
        photo_detector=photo_detector, vision_model=vision_model, model=model,
        verification_model=verification_model, diagnostic_call=diagnostic_call,
        extra_instructions=extra_instructions, photo_cache_dir=photo_cache_dir,
        grounding_prompt=grounding_prompt, verification_prompt=verification_prompt,
        terminal_prompt=terminal_prompt, crop_prompt=crop_prompt, crop_model=crop_model,
        yolo_model_path=yolo_model_path, smd_model_path=smd_model_path,
        review_proposals=review_proposals, yolo_confidence=yolo_confidence,
        yolo_settings=yolo_settings,
        recount_contacts=lambda disagreements: _recount_sides_from_photo(
            data, disagreements, content_blocks, diagnostic_call, model, system_prompt,
            has_pdf, progress))


def _recheck_missing_contact_styles(data, content_blocks, diagnostic_call, model,
                                   has_pdf=False, progress=None):
    """One optional visual check; uncertain edges still require manual choice.

    Only missing styles can change. Counts, geometry, numbering and already
    observed styles are preserved. Replies use canonical top-view edge names.
    """
    if data.get("layout") != "castellated":
        return
    sides = data.get("castellated_sides") or []
    missing = {_side_edge(side): side for side in sides
               if isinstance(side, dict) and side.get("contact_style") is None
               and _side_edge(side) in ("top", "bottom", "left", "right")}
    if not missing:
        return
    # A text-only request cannot provide new visual evidence.
    if not has_pdf and not any(block.get("type") == "image_url" for block in content_blocks):
        return
    message = "Ricontrollo AI del tipo di contatto sui lati non riconosciuti: " + ", ".join(missing)
    if progress:
        progress(tx("Ricontrollo AI del tipo di contatto sui lati non riconosciuti: {sides}",
                    sides=", ".join(missing)))
    properties = {
        "edge": {"type": "string", "enum": list(missing)},
        "half_hole_visible": {"type": ["boolean", "null"]},
        "through_hole_visible": {"type": ["boolean", "null"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "evidence": {"type": "string"},
    }
    tool = {"type": "function", "function": {
        "name": "observe_contact_styles",
        "description": "Observe physical board contact construction, without changing geometry.",
        "parameters": {"type": "object", "properties": {
            "sides": {"type": "array", "items": {"type": "object",
                "properties": properties, "required": list(properties),
                "additionalProperties": False}}}, "required": ["sides"],
            "additionalProperties": False}}}
    prompt = (
        "Inspect only physical contact construction in the supplied photographs or drawings. "
        "Return edge names in canonical TOP VIEW (not the underside), matching the supplied sides. "
        "A half hole is a metallized opening cut by the board edge. A through hole is a "
        "complete enclosed plated hole with board material between it and the edge. "
        "Both can exist at the same contact. For each feature use true only if clearly visible; "
        "false only if its absence is clearly visible; otherwise null. If the edge is obscured, "
        "orientation uncertain, or contacts on that side differ, use null and low confidence. "
        "Do not infer style from component family, pin count, pad colour or previous defaults. "
        "Give high confidence only for clear evidence; cite the document and visible feature. "
        "Do not change counts, pitch, numbering or dimensions."
    )
    blocks = list(content_blocks) + [{"type": "text", "text":
        "Target sides in top view: " + repr(list(missing.values())) +
        ". Only return observe_contact_styles observations for these sides."}]
    try:
        reply = diagnostic_call(model=model or DEFAULT_MODEL, system_prompt=prompt,
                                content_blocks=blocks, tool=tool, max_tokens=1800,
                                has_pdf=has_pdf)
    except OpenRouterError:
        if progress:
            progress(tx("Ricontrollo tipo di contatto non riuscito: resta la scelta manuale."))
        return
    observations = reply.get("sides") if isinstance(reply, dict) else None
    if not isinstance(observations, list):
        return
    grouped = {}
    for row in observations:
        if isinstance(row, dict):
            grouped.setdefault(_side_edge(row), []).append(row)
    for edge, side in missing.items():
        rows = grouped.get(edge, [])
        if len(rows) != 1:
            continue
        row = rows[0]
        half, through = row.get("half_hole_visible"), row.get("through_hole_visible")
        evidence = row.get("evidence")
        if (row.get("confidence") != "high" or not isinstance(evidence, str)
                or not evidence.strip() or type(half) is not bool or type(through) is not bool
                or not (half or through)):
            continue
        style = "half_and_through" if half and through else "half_hole" if half else "through_hole"
        side["contact_style"] = style
        side["_contact_style_evidence"] = evidence.strip()
        note = f"Tipo di contatto ricontrollato: {edge} = {style}. {evidence.strip()}"
        data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
        if progress:
            progress(note)


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


def _exclude_declared_sides(data, items, say) -> bool:
    """Drop declared castellated sides the photo says are not there (HANDOFF point 164).

    @param data Validated main answer, updated in place.
    @param items EdgeCount list of the sides to drop; `reason` says why.
    @param say Progress callback; the exclusion is reported as a WARNING.
    @return True when at least one side was dropped.
    @note The remaining sides keep their own numbering when it stays
        contiguous; otherwise they are renumbered in order of their first
        pin, and the note says so, because a footprint with a gap in the pad
        numbers is refused downstream. The excluded sides are named in
        `notes`, so whoever reads the result can put one back by hand when
        the photo, not the model, is wrong.
    """
    from .messages import as_warning
    edges = {item.edge: item for item in items}
    remaining = [entry for entry in (data.get("castellated_sides") or ())
                 if _side_edge(entry) not in edges]
    dropped = [entry for entry in (data.get("castellated_sides") or ())
               if _side_edge(entry) in edges]
    if not dropped:
        return False
    for entry in dropped:
        item = edges[_side_edge(entry)]
        say(as_warning(tx(
            "Lato {label}: l'analisi dichiarava {declared} contatti, ma sulla foto il bordo "
            "non ha nessuna tacca ({reason}): lato escluso. Se la foto inganna, "
            "aggiungilo a mano nella tabella dei lati.",
            label=EDGE_LABELS.get(item.edge, item.edge), declared=entry.get("count"),
            reason=item.reason or tx("nessuna tacca visibile sul bordo"))))
    renumbered = False
    if remaining and not _numbering_is_contiguous(copy.deepcopy(remaining)):
        ordered = sorted(remaining, key=lambda entry: (
            _optional_number(entry.get("first_pin"), positive=True, integer=True) or 0))
        next_pin = 1
        for entry in ordered:
            count = _optional_number(entry.get("count"), positive=True, integer=True)
            if count is None:
                continue
            entry["first_pin"] = next_pin
            next_pin += int(count)
        renumbered = True
    data["castellated_sides"] = remaining
    note = tx("Lati esclusi dopo il confronto con la foto (bordo senza tacche): {edges}.",
              edges=", ".join(EDGE_LABELS.get(edge, edge) for edge in edges))
    if renumbered:
        note += " " + tx("I pad dei lati rimasti sono stati rinumerati di seguito: verificare "
                         "la tabella pinout.")
    data["notes"] = (str(data.get("notes") or "") + "\n" + note).strip()
    say(note)
    return True


def _keep_clean_sides_with_copper(data, items, say) -> bool:
    """Keep the declared sides of a clean edge along which the photo shows a row of lands.

    @param data Validated main answer, updated in place (notes only).
    @param items EdgeCount list of clean edges with `copper_found` > 0.
    @param say Progress callback; reported as a WARNING to verify.
    @return True when a note was added.
    @note The outline count has a blind spot: a shallow half-hole whose
        plated barrel hides the background shows no notch (ESP32 devkit,
        2026-10-08: 22 contacts per edge dropped as «no notch» all day). A
        periodic row of identical lands on the edge (edge_lands.
        count_edge_copper) is the presence of contacts, so the side stays as
        declared; style and count are left to a person, because the photo
        could not read the holes. Nothing is adopted from the copper count.
    """
    from .messages import as_warning
    changed = False
    for item in items:
        for entry in data.get("castellated_sides") or ():
            if _side_edge(entry) != item.edge:
                continue
            message = tx("Lato {label}: la foto non mostra tacche sul bordo, ma una fila "
                         "periodica di {found} elementi (piazzole e spazi) al passo dichiarato "
                         "({declared} contatti dichiarati): lato tenuto, tipo di contatto e "
                         "conteggio da verificare a mano.",
                         label=EDGE_LABELS.get(item.edge, item.edge), found=item.copper_found,
                         declared=entry.get("count"))
            say(as_warning(message))
            data["notes"] = (str(data.get("notes") or "") + "\n" + message).strip()
            changed = True
    return changed


def _recount_sides_attempt(data, disagreements, content_blocks, diagnostic_call, model,
                           system_prompt, has_pdf, say) -> bool:
    """Body of _recount_sides_from_photo; `data` is changed only at the very end.

    Three kinds of EdgeCount arrive (edge_notches.count_disagreements):
    * a CLEAN edge (`no_notch`): the declared side is excluded here, with no
      paid call -- the photo is the geometric reference of the project, and
      a straight outline on a photo able to show a notch is a measurement;
    * an edge the photo cannot count (`found` None): the model is asked to
      look at the enlargement of THAT edge and to answer count 0 (or leave
      the side out) when it sees no plated hole cut by the edge; 0 excludes
      the side, any other count keeps the declared side with a warning that
      the photo could not verify it (the recount never adopts a number the
      photo cannot check);
    * another number of notches: the recount of HANDOFF point 137.
    """
    clean = [item for item in disagreements if item.no_notch]
    unverifiable = [item for item in disagreements if item.found is None and not item.no_notch]
    disagreements = [item for item in disagreements
                     if item.found is not None and not item.no_notch]
    # A straight outline is not proof of absence: a shallow half-hole whose
    # barrel hides the background shows no notch (ESP32 devkit, 2026-10-08:
    # 22 contacts per edge dropped as «no notch»). A periodic row of lands
    # on the edge is presence: such a side stays, flagged; the others go.
    # copper_found None (strip not measurable) is not «no copper»: the side
    # goes as before, and the reason is said with the exclusion.
    absent = [item for item in clean if not item.copper_found]
    for item in absent:
        if item.copper_reason:
            say(tx("Lato {label}: rame sul bordo: {reason}.",
                   label=EDGE_LABELS.get(item.edge, item.edge), reason=item.copper_reason))
    changed = _exclude_declared_sides(data, absent, say) if absent else False
    changed = _keep_clean_sides_with_copper(
        data, [item for item in clean if item.copper_found], say) or changed
    if not disagreements and not unverifiable:
        return changed
    facts = "; ".join(
        f"lato {item.edge}: la foto calibrata vista dall'alto mostra {item.found} semi-fori "
        f"aperti sul bordo, con i centri a {', '.join(f'{c:.2f}' for c in item.centres_mm)} mm "
        f"dall'inizio del bordo (da sinistra per top/bottom, dall'alto per left/right); "
        f"la risposta ne dichiarava {item.declared}"
        for item in disagreements)
    doubts = "; ".join(
        f"lato {item.edge}: la foto calibrata non permette di contare i semi-fori "
        f"({item.reason}); la risposta ne dichiarava {item.declared}. Guarda l'ingrandimento "
        f"del bordo {item.edge}: se NON vedi fori metallizzati tagliati dal bordo, rispondi "
        f"count 0 per questo lato (o non elencarlo)"
        for item in unverifiable)
    summary = ", ".join(
        [f"{EDGE_LABELS.get(item.edge, item.edge)} {item.declared} -> {item.found}"
         for item in disagreements]
        + [f"{EDGE_LABELS.get(item.edge, item.edge)} {item.declared} -> ?"
           for item in unverifiable])
    say(tx("Contatti: la foto non conferma l'analisi ({summary}). Richiesta di ricontarli sugli "
           "stessi documenti: una chiamata in piu'.", summary=summary))
    comparison = "Confronto con la fotografia: " + "; ".join(part for part in (facts, doubts) if part)
    blocks = list(content_blocks) + [{"type": "text", "text": (
        comparison + ". Riconta sui documenti (pinout, disegno "
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
        return changed
    repaired = copy.deepcopy(repaired)
    repaired["layout"] = "castellated"
    # Private keys: a model answer is never already converted.
    repaired.pop("_canonical", None)
    repaired.pop("_canonical_notes", None)
    if repaired.get("contacts_view") is None and repaired.get("pin_map_view") is None:
        # The request asks for the top view: an answer that does not state
        # the view is read that way, without the DOUBT of the main answer.
        repaired["contacts_view"] = "top"
    for entry in repaired["castellated_sides"]:
        if isinstance(entry, dict):
            entry[_REVERSE_STATED] = isinstance(entry.get("reverse"), bool)
    _canonicalize_answer(repaired)
    conversion = [str(note) for note in repaired.get("_canonical_notes") or ()]
    for note in conversion:
        say(tx("Ricontrollo dei contatti: {note}", note=note))
    edges = [_side_edge(entry) for entry in repaired["castellated_sides"] if _side_edge(entry)]
    # A side listed twice does not say which count applies: it is not adopted.
    recounted = {_side_edge(entry): entry for entry in repaired["castellated_sides"]
                 if _side_edge(entry) and edges.count(_side_edge(entry)) == 1}
    merged = list(data.get("castellated_sides") or ())
    index = {_side_edge(entry): i for i, entry in enumerate(merged) if _side_edge(entry)}
    length = _optional_number(data.get("body_length_mm"), positive=True)
    width = _optional_number(data.get("body_width_mm"), positive=True)
    # Sides not verifiable on the photo: zero (or a missing side) excludes
    # them, a number leaves them declared with the warning. A new number is never adopted.
    from .messages import as_warning
    not_there = []
    for item in unverifiable:
        reply = recounted.get(item.edge)
        count = (_optional_number(reply.get("count"), integer=True)
                 if isinstance(reply, dict) else None)
        if reply is None or count == 0:
            not_there.append(EdgeCount(item.edge, 0, item.declared, [], reason=tx(
                "ricontrollo AI sull'ingrandimento del bordo: nessun foro tagliato dal bordo")))
        else:
            say(as_warning(tx(
                "Lato {label}: {declared} contatti dichiarati; la foto non permette di "
                "contarli ({reason}) e il ricontrollo AI li conferma: lato da verificare a "
                "mano.", label=EDGE_LABELS.get(item.edge, item.edge), declared=item.declared,
                reason=item.reason)))
    if not_there:
        changed = _exclude_declared_sides(data, not_there, say) or changed
        merged = list(data.get("castellated_sides") or ())
        index = {_side_edge(entry): i for i, entry in enumerate(merged) if _side_edge(entry)}
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
        return changed
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
    names, ends = {}, {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        edge = str(entry.get("edge") or "").strip().lower()
        if isinstance(entry.get("pin_names"), (list, tuple)) and entry["pin_names"]:
            names[edge] = len(entry["pin_names"])
        first = _optional_number(entry.get("first_contact_from_edge_mm"), positive=False)
        last = _optional_number(entry.get("last_contact_from_edge_mm"), positive=False)
        if first is not None and last is not None and first >= 0 and last >= 0:
            ends[edge] = (first, last)
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
        # A row of N contacts at pitch P spans (N-1)*P from centre to centre;
        # a drawing that quotes the first and the last contact from the two
        # ends of the edge (a, b) fixes that span to L-a-b. When the nearest
        # integer N to that span is not the declared count, the answer
        # contradicts its own quotes (ESP32 devkit, 2026-10-08: 21 declared
        # against 63.3-5.05-4.91 = 21 x 2.54, i.e. 22). Rounding boundary,
        # not a threshold: beyond half a pitch another integer is nearer.
        # Without both quotes nothing is checked, nothing is inferred.
        if edge in ends and edge_length is not None and side["pitch_mm"] > 0:
            first, last = ends[edge]
            quoted = edge_length - first - last
            if quoted < 0:
                problems.append((edge, f"lato {edge}: le quote del primo e dell'ultimo contatto "
                                       f"dai bordi ({first:g} + {last:g}) superano il bordo lungo "
                                       f"{edge_length:g}"))
            elif abs(quoted - span) > side["pitch_mm"] / 2:
                expected = round(quoted / side["pitch_mm"]) + 1
                problems.append((edge, f"lato {edge}: le quote del disegno danno "
                                       f"({edge_length:g} - {first:g} - {last:g}) / {side['pitch_mm']:g} "
                                       f"+ 1 = {expected} contatti, ma count={count}"))
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
        f"body_width_mm={dims['body_width']} per left e right). Se il disegno quota le "
        "distanze del primo e dell'ultimo contatto dai bordi (first_contact_from_edge_mm, "
        "last_contact_from_edge_mm), allora count = (bordo - prima - ultima) / pitch_mm + 1: "
        "riportale e rispettale. Usa la stessa unita' della "
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
        # Sides missing or in another unit: not comparable with the body and
        # pitch of the main answer, which stay as they are.
        raise ExtractionError(tx("Il ricontrollo dei contatti non ha restituito lati "
                                 "utilizzabili; analisi foto non avviata."))
    recounted_edges = [_side_edge(entry) for entry in sides if _side_edge(entry)]
    recounted = {_side_edge(entry): entry for entry in sides if _side_edge(entry)}
    previous = data.get("castellated_sides")
    previous = list(previous) if isinstance(previous, list) else []
    before = {_side_edge(entry): entry for entry in previous if _side_edge(entry)}
    # Coherent edges the recount would change: normally said, not adopted.
    # A coherent edge is one the first answer DESCRIBED without contradiction.
    # An edge the recount adds was not described at all: it is part of the
    # repair, not a change to something coherent (ESP32 devkit, 2026-10-08:
    # the first answer put the two 22-contact rows on left/right, where
    # 53.34 mm cannot fit in 25.4; the recount moved them to top/bottom and
    # was refused as «changing coherent sides», so the extraction failed).
    changed = sorted(edge for edge, entry in recounted.items()
                     if edge not in wrong_edges and edge in before and any(
                         entry.get(key) != before[edge].get(key)
                         for key in ("count", "pitch_mm", "first_pin", "reverse")))
    # A contradicted edge the recount no longer lists does not exist: the
    # recount is the answer to that contradiction, and keeping the impossible
    # row would only fail validation again.
    merged = [recounted[_side_edge(entry)] if _side_edge(entry) in wrong_edges
              else entry for entry in previous
              if _side_edge(entry) not in wrong_edges or _side_edge(entry) in recounted]
    # Edges absent from the first answer can only come from the recount.
    merged.extend(entry for edge, entry in recounted.items() if edge not in before)
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
                            has_pdf, progress, numbering=None) -> None:
    """Sanitize and validate the main answer in place; ExtractionError when unusable.

    `numbering`: None, or the inputs of the dedicated pin-numbering call
    (`read_pin_numbering`): documents, component_family, announce,
    check_cancel, prompt. The call is made for a matrix answer of a single
    part when there is a drawing to read.

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
    if (numbering and numbering.get("documents")
            and (data.get("layout") or "matrix") == "matrix" and not data.get("package_geometry")
            and numbering.get("component_family") != "aggregate_tht"):
        # Second, dedicated reading of the pin numbering (user, 2026-10-10):
        # the drawing alone, one question. Before the canonicalisation, so
        # both readings are in the drawing's own view and get mirrored once.
        documents = numbering["documents"]
        rows = _optional_number(data.get("rows"), positive=True, integer=True)
        cols = _optional_number(data.get("cols"), positive=True, integer=True)
        if rows and cols:
            numbering["check_cancel"]()
            numbering["announce"]("llm", model or DEFAULT_MODEL)
            if progress:
                progress(tx("Lettura dedicata della numerazione dei pin su {names}.",
                            names=", ".join(p.name for p in documents)))
            reading = read_pin_numbering(
                documents, rows=rows, cols=cols, model=model or DEFAULT_MODEL,
                call=diagnostic_call, prompt=numbering.get("prompt"), progress=progress)
        else:
            reading = {"view": "unknown", "pins": [], "notes": "",
                       "discarded": tx("griglia righe x colonne non valida, nessuna chiamata")}
        apply_dedicated_numbering(data, reading, progress)
    elif (numbering and numbering.get("documents") and _has_explicit_pads(data)
            and numbering.get("component_family") in SINGLE_COMPONENT_FAMILIES):
        # Explicit pads of a single part: a second, dedicated reading of the
        # hole positions, compared by distances (user, 2026-10-10, VE12H-K).
        documents = numbering["documents"]
        reader = numbering.get("pad_model") or model or DEFAULT_MODEL
        numbering["check_cancel"]()
        numbering["announce"]("llm", reader)
        if progress:
            progress(tx("Lettura dedicata delle posizioni dei pad su {names} con {model}.",
                        names=", ".join(p.name for p in documents), model=reader))
        reading = read_pad_positions(documents, model=reader,
                                     call=diagnostic_call, progress=progress)
        apply_dedicated_pads(data, reading, progress, check_layout=_layout_checker(documents))
        if reader != (numbering.get("pad_model_requested") or reader):
            # Not two different models any more: the result says so, not only the log.
            data["notes"] = (str(data.get("notes") or "") + "\n" + tx(
                "Posizioni dei pad lette dal modello principale {model} al posto di "
                "{requested}, non utilizzabile.", model=reader,
                requested=numbering["pad_model_requested"])).strip()
    elif (numbering and _has_explicit_pads(data)
            and numbering.get("component_family") in SINGLE_COMPONENT_FAMILIES):
        # No drawing image to read the positions from (only a PDF, or no
        # drawing role): nothing confirms them, so they block like any
        # unconfirmed reading (user, 2026-10-30; adversarial review G1).
        apply_dedicated_pads(data, {"view": "unknown", "pads": {}, "notes": "", "skipped": tx(
            "nessun disegno in immagine con ruolo pinout o vista dall'alto: la lettura dedicata "
            "non si puo' fare (un PDF non si ingrandisce)")}, progress)
    elif (numbering and numbering.get("pdf_only")
            and (data.get("layout") or "matrix") == "matrix" and not data.get("package_geometry")
            and numbering.get("component_family") != "aggregate_tht"):
        data["_numbering_note"] = tx(
            "Numerazione dei pin NON confermata da una lettura dedicata: il disegno e' "
            "disponibile solo in PDF, che non si puo' ingrandire. Verificare la posizione dei "
            "pin sul disegno.")
        if progress:
            progress(data["_numbering_note"])
    if (data.get("layout") or "matrix") in ("matrix", "castellated"):
        # After the numbering repair, which compares sides in the drawing's own
        # unit and view, and before the photo stage, which calibrates on the
        # body dimensions and masks the declared contacts: both must read
        # millimetres seen from above.
        _canonicalize_answer(data)
        if progress:
            for note in data["_canonical_notes"]:
                progress(note)
    # After the possible correction, so the lines describe the final sides:
    # unobserved contact style and estimated values go into the log at once,
    # and stay in the notes of the result (_params_from_tool_arguments).
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
                         recount_contacts=None, crop_prompt=None,
                         crop_model=None, cancelled=None,
                         smd_model_path=None) -> ExtractedParams:
    """Optional photo inventory and final assembly of a validated main answer.

    `cancelled` is the zero-argument cancellation predicate of extract_params
    (None: no cancellation channel); the SMD network polls it between crops.
    """
    # The photo needs a separate pass: in the global extraction the model
    # must also read dimensions, pinout and layout, and tends to sacrifice the
    # small passives. The crops are best-effort: if they fail, the global result stays.
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
        # The local detector phase is NOT announced here: the contract of
        # `extract_inventory` (see photo_inventory.extract_inventory, @param
        # phase) is that 'yolo' arrives immediately before the detector, and
        # here we are before the calibration. Announcing it now showed a phase
        # that might never start -- without a calibratable view the inventory
        # exits before loading the ONNX -- and the modal stayed on the local
        # detector for the rest of the run.
        # Nor is the paid call to the vision model ('ai', 'vlm') announced
        # here (point 139, C1): first comes the OpenCV calibration, which
        # `extract_inventory` announces itself, and the early announcement
        # stayed as an empty «✓» phase immediately covered by OpenCV.
        # 'llm_inventory' is announced by `extract_inventory` when the photo starts.
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
                    'recount_contacts': recount_contacts, 'crop_prompt': crop_prompt,
                    'crop_model': crop_model}
                   if photo_detector in ('yolo', 'vlm', 'yolo_smd') else {}),
                **({'cancel': cancelled} if photo_detector == 'yolo_smd' and cancelled is not None
                   else {}),
                **({'smd_model_path': smd_model_path}
                   if photo_detector == 'yolo_smd' and smd_model_path is not None else {}),
            )
        except ReviewCancelled as exc:
            # A cancellation is not an inventory failure: continuing here
            # would mean paying the reconciliation on the rejected proposals.
            raise ExtractionError(str(exc)) from exc
        except ExtractionCancelled:
            raise
        except CallCancelled as exc:
            # Cancellation raised outside a paid call (SMD network, KiCad
            # matching): same documented type as every other cancellation.
            raise ExtractionCancelled(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - the inventory is optional
            # Any inventory failure (even KeyError/TypeError from a malformed
            # answer) must not lose the main answer, already paid and valid:
            # it becomes a note, with the full traceback in the log for
            # whoever has to fix it.
            # The error is swallowed into the notes and processing continues
            # without inventory: the announced phase must be closed, otherwise
            # the modal stays for the whole rest of the run on the last tool
            # announced by the inventory (the local detector, if the ONNX file
            # vanished between validation and detection) while that tool is no
            # longer working. Empty tool = no phase claimed, not a wrong phase.
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
        if photo_report and photo_report.calibration.get('inventory_strategy') == 'review-kept-none':
            # An explicit empty review is authoritative, unlike a failed or
            # unavailable inventory: do not resurrect the initial AI bodies.
            data["components_3d"] = []
        if photo_report and photo_report.components:
            # The global YOLO reconciliation already reports terminal evidence.
            # Further paid crop calls are an explicit optional UI action only.
            if photo_detector not in ('yolo', 'vlm', 'yolo_smd') and isinstance(getattr(photo_report, "photo_png", None), bytes):
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


#: Tolerance, in mm, with which a pin centre on the edge counts as inside:
#: numerical rounding only, not a physical margin.
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
    """@brief Keep every pin of a populated through-hole board inside the board.

    @param params ExtractedParams of «matrix» layout; corrected in place.
    @param component_family Family of the chosen component type (a declared
        attribute, not the name of the part).
    @return The messages to show in the log: empty when there was nothing to say.
    @note Only for `aggregate_tht`: on a populated through-hole board the
        pins go through the holes OF the board, so every pin centre lies
        inside its outline. This is a physical constraint, not an estimate. A
        single THT component may instead have legs outside its body, and is
        left untouched.
    @note Case seen by the user on 2026-09-24 (TEST4): the AI gave
        `body_offset_y_mm = -12.7`, half the width, measuring from the centre
        of the board instead of from the first row, and the second row ended
        up outside. When the pin field leaves the board but would fit in it,
        the offset of THAT axis is re-centred: it is an ESTIMATE (the edge was
        not dimensioned consistently) and is written in the notes, in the log
        and in `params.value_sources` ("estimated"). When the board is
        smaller than the pin field no value is right: no correction, only the
        warning about inconsistent dimensions.
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
        # Field of the PRESENT pins (populated_pins, pin_map), not of the
        # declared matrix: an empty column at the end is not a pin off the
        # board (audit 25/09: a correct offset was being «corretto»
        # (corrected)). The pins occupy [low, high] on the axis; the board
        # [offset, offset + extent].
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


def _real_number(value) -> Optional[float]:
    """A finite real number as float, or None; numeric TEXT is never a number.

    Delegates to the shared `finite_number` (review D4: "12" used to pass as a
    coordinate). Real scalars that are not Python int/float, such as the
    numpy values the program's own photo pipeline may carry into trusted
    components, are converted first: they are numbers, unlike text.
    """
    if (isinstance(value, numbers.Real) and not isinstance(value, (bool, int, float))):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return finite_number(value)


#: Name given to an extracted package whose AI-proposed name is not a valid
#: file name. Never kept: the window replaces every package name with the
#: project folder's (the folder is the component's identity).
EXTRACTED_PACKAGE_PLACEHOLDER_NAME = "Package"

#: Pad shapes an older schema offered that PinSpec no longer accepts, mapped to
#: what KiCad actually drew for them (review 2026-10-27, G7: a trapezoid needs
#: `rect_delta`, which the AI never gave, so it was always written as a rectangle).
LEGACY_EXTRACTED_PAD_SHAPES = {"trapezoid": "rect"}


def _package_mapping_for_validation(package_geometry, notes: list):
    """Copy of an extracted package mapping that cannot discard a paid answer for a label.

    Two fields of an AI answer are not geometry and must not make
    `package_from_mapping` reject the whole extraction: the package NAME
    (validated as a file name since 2026-10-27, while the AI writes part
    numbers such as «LM358/LM2904»; the window replaces it with the project
    folder anyway) and a legacy pad SHAPE still present in cached answers.
    The name is swapped for a placeholder silently (it was never shown); each
    legacy shape is mapped and named in one note. Everything else is
    validated as before; the caller's mapping is never modified.
    """
    from ..core.output_names import is_valid_output_name

    if not isinstance(package_geometry, dict):
        return package_geometry
    mapping = dict(package_geometry)
    name = mapping.get("name")
    if not (isinstance(name, str) and is_valid_output_name(name)):
        mapping["name"] = EXTRACTED_PACKAGE_PLACEHOLDER_NAME
    pins = mapping.get("pins")
    if isinstance(pins, list):
        legacy = []
        migrated = []
        for pin in pins:
            if isinstance(pin, dict) and pin.get("shape") in LEGACY_EXTRACTED_PAD_SHAPES:
                legacy.append(str(pin.get("number", "?")))
                pin = {**pin, "shape": LEGACY_EXTRACTED_PAD_SHAPES[pin["shape"]]}
            migrated.append(pin)
        if legacy:
            mapping["pins"] = migrated
            notes.append(tx("Pad {pads}: forma «trapezio» proposta senza la geometria che KiCad "
                            "richiede; usati come rettangoli, come KiCad li avrebbe disegnati.",
                            pads=", ".join(legacy)))
    return mapping


def _optional_number(value, *, positive=False, integer=False):
    """Discard malformed optional numerics; signed offsets remain valid.

    Strings, booleans, non-finite values and integers too large for a float
    are malformed (see `_real_number`).
    """
    number = _real_number(value)
    if number is None or (positive and number <= 0):
        return None
    if integer and number != int(number):
        return None
    return int(number) if integer else number


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
        # The contact type is seen in the photo and cannot be derived from
        # the dimensions: a value outside the list is neither corrected nor
        # silently discarded, because any substitution would be exactly the
        # deduction the field exists to avoid. Missing becomes None, i.e.
        # «sconosciuto» (unknown): no default type (user decision 26/09, it was the
        # sample board's), the user chooses it (see
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
    """Normalize the sides of a castellated module, discarding the unusable ones.

    The model may return a side without pitch or with zero contacts: it is
    better to ignore it and say so (in `notes`, when given) than to
    propagate a side that would make the generation fail with an obscure
    geometric error. Two sides on the same edge, or a side whose contacts
    cannot fit along the known board edge (`body_length`/`body_width`, mm),
    raise ExtractionError; a pad number shared between sides raises
    _PinNumberConflict.

    Pad ranges are compared as intervals and never materialized: a huge
    declared count must not allocate memory proportional to it.
    """
    sides = _normalized_castellated_sides(raw, notes)
    _check_sides_fit_edges(sides, body_length, body_width)

    # Progressive numbering on the sides without first_pin, in the order in
    # which they were listed: better a consistent numbering to fix by hand
    # than pads without a number.
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
    "model_match", "footprint", "footprint_candidates",
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

    These are visual estimates: a block with a zero or negative dimension is
    not a badly measured component, it is noise, and in the 3D solid it would
    raise an error. A `kind` outside COMPONENT_KINDS becomes "generic", with
    a line in `notes` when given.

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
            # With long lists the model sometimes
            # derails from the schema and slips in an entry as a plain string
            # instead of an object: that entry is discarded, not the whole
            # extraction -- the other components stay valid.
            continue
        if not trusted:
            entry = {key: value for key, value in entry.items()
                     if not (isinstance(key, str) and key.startswith("_"))
                     and key not in _TRUSTED_ONLY_FIELDS}
        # Booleans, numeric strings, non-finite and overflowing values are not
        # measures (review D4): the entry is dropped, the others stay.
        geometry = {key: _real_number(entry.get(key)) for key in
                    ("x_mm", "y_mm", "length_mm", "width_mm", "height_mm")}
        if any(value is None for value in geometry.values()):
            continue
        component = {"name": str(entry.get("name") or "Componente"), **geometry}
        # The model also sees PCB elements that are not mounted components:
        # pads, vias, half-holes and edge contacts. If they get through the
        # schema, removing them here keeps them from ending up in the 3D table
        # as boxes. Real components must have a package above the board.
        label = component["name"].strip().lower()
        # Global component IDs are arbitrary display labels; retained detector
        # proposals are physical hypotheses, even when their class is unknown.
        # Preserve explicit PCB-feature rejection and the geometry checks below.
        physical_hypothesis = (
            entry.get("_global_inventory_status") in ("validated_hypotheses", "partial")
            or (isinstance(entry.get("_yolo_proposal_id"), str)
                and bool(entry["_yolo_proposal_id"].strip())
                and entry.get("_yolo_origin") in ("operating_threshold", "low_confidence_review")))
        if entry.get("object_type") == "pcb_feature":
            # The declared criterion: the schema asks for `object_type`, and
            # a declared PCB feature is never a body.
            continue
        if (not physical_hypothesis and "object_type" not in entry and re.match(
                r"^(?:via|pad|piazzola|terminal pad|contatto|castellated|semiforo|serigrafia)\b", label)):
            # DECLARED HEURISTIC (review 2026-10-27, E8), not a rule: when
            # `object_type` is absent -- which is always the case for a raw
            # AI answer, since `_TRUSTED_ONLY_FIELDS` strips it so that a
            # document cannot vouch for itself -- the only information left
            # is the display name, and a name that opens with the word of a
            # PCB feature is taken as one. It was tuned on the inventory
            # labels the models actually produce («pad 3», «via», «contatto
            # castellated»); a component the model chose to name «Pad di
            # test IC» is lost by it. It would become superfluous if the
            # answer carried a provenance the program could trust. It is
            # never silent: every time it fires a line is written, so the
            # user can bring the body back from the review.
            if notes is not None:
                notes.append(tx("{name}: scartato dai corpi 3D perche' il nome descrive un "
                                "elemento del PCB (via, pad, contatto, serigrafia) e il tipo "
                                "di oggetto non e' dichiarato; se e' un componente, "
                                "rinominalo o rimettilo nella revisione.",
                                name=component["name"]))
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
        # Mating direction declared by the model looking at the photo.
        # Without it, the 3D shape would have to guess it from the dimensions
        # alone, which for a connector are not enough. Other values are ignored.
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
                    '_smd_prediction', '_smd_class', '_smd_rotation_deg', '_smd_rotation_confidence',
                    '_smd_ai_recheck', '_smd_accepted', '_smd_body', '_smd_rotation_kind',
                    '_smd_views_agree', '_smd_terminal_count', '_smd_superseded',
                    'model_match', 'footprint', 'footprint_candidates', '_presence_reviewed_by_user',
                    'presence_confidence', 'class_confidence', '_yolo_proposal_id',
                    '_yolo_class_ambiguous', '_yolo_review_only', '_yolo_origin',
                    # Box drawn or retouched by a person: it is the reviewer's
                    # presence decision and must reach the final component,
                    # not stop at the submitted candidates.
                    '_yolo_manual_box',
                    # Body added by the model alone, excluded until the user
                    # keeps it (point 141): without this, the first extraction
                    # delivered it like any other body, into the 3D.
                    '_ai_only_excluded',
                    '_package_verified', '_orientation_verified', 'pin_count_confidence', 'pin_count_source',
                    'class', 'component_id', 'source_candidate_ids', 'terminal_count', 'terminal_layout',
                    'terminal_count_basis', 'lead_count', 'lead_count_complete', 'lead_count_confidence',
                    '_reported_rotation_deg', '_height_estimated', '_global_inventory_status',
                    # Measurement from the edges, estimated-height rule and the
                    # user's choice on weak proposals: the GUI shows them.
                    '_edges_measured', '_height_basis', '_yolo_low_confidence', '_yolo_user_kept',
                    # Detector's own weak/strong verdict before the reviewer
                    # changed `_yolo_review_only` (photo_yolo.set_review_only).
                    '_detector_review_only',
                    '_global_inventory_errors', '_global_inventory_uncertainties',
                    '_global_inventory_prompt_version', '_global_inventory_prompt_hash',
                    # The asymmetric reference lets the 3D builder establish
                    # the orientation of the native model. Without this line
                    # it was discarded here, and every component reached the
                    # matching declaring "no observed reference" even when
                    # the model had seen one with high confidence.
                    'landmark_feature', 'landmark_side', 'landmark_confidence',
                    # Crop re-reading (HANDOFF 144): outcome, the two readings
                    # and the replaced inventory values must reach the review,
                    # otherwise a «da rivedere» (to review) row would no longer
                    # say why.
                    'package_confidence', '_crop_reading', '_crop_readings',
                    '_crop_observations', '_crop_class_status', '_crop_class_decided',
                    '_crop_terminal_review_required', '_crop_image_evidence',
                    '_category_source', '_category_status', '_ai_class_proposal',
                    '_ai_class_confidence', '_terminal_status',
                    # Local detector evidence of the source proposal and
                    # provenance of a derived package (HANDOFF 149).
                    '_detector_evidence', 'package_source',
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
                             "pin_length_3d_mm", "pcb_thickness_mm", "lead_diameter_mm")


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


def _numbering_conventions(rows: int, cols: int) -> dict:
    """@brief The standard pin-numbering conventions of a rows x cols grid, seen from above.

    @param rows Rows of the grid.
    @param cols Columns of the grid.
    @return {name: {pin: (row, col)}} covering every position once:
        "column" (pin headers: 1,2 down the first column, then the next) and
        its mirror image "column_mirrored" (the same convention read on a
        bottom view), and for two rows "u" (DIP-style: 1..cols along row 0,
        then back along row 1) and "u_mirrored". A single row has the two
        reading directions only.
    @note These are the conventions a drawing implies when it prints only
        some numbers (a header prints «1», a DIP prints «1» and «N»); they
        are declared here, not tuned: completing a map with one of them is
        done only when exactly one fits every printed number.
    """
    column = {col * rows + row + 1: (row, col) for col in range(cols) for row in range(rows)}
    conventions = {
        "column": column,
        "column_mirrored": {pin: (row, cols - 1 - col) for pin, (row, col) in column.items()},
    }
    if rows == 2:
        u = {col + 1: (0, col) for col in range(cols)}
        u.update({2 * cols - col: (1, col) for col in range(cols)})
        conventions["u"] = u
        conventions["u_mirrored"] = {pin: (row, cols - 1 - col) for pin, (row, col) in u.items()}
    return conventions


def _resolve_pin_numbering(pin_map, raw_populated, rows, cols, max_pin, *, dedicated: bool,
                           notes: list):
    """@brief Decide the pin map and the populated pins of a matrix answer, map complete or not.

    @param pin_map Validated entries seen from above, or None.
    @param raw_populated The populated_pins of the answer, unvalidated.
    @param rows Rows of the grid (None when unknown).
    @param cols Columns of the grid (None when unknown).
    @param max_pin rows x cols, or None.
    @param dedicated True when the map comes from the dedicated numbering
        call (every entry is a number printed beside a position).
    @param notes Notes of the result.
    @return (pin_map, populated_pins, block_text): block_text is None, or the
        reason the automatic generation must stop (a partial map that no
        single convention completes).
    @note Rules, the same for every part (review A of 2026-10-10, finding 1:
        the prompts now ask for the PRINTED numbers only, so a map may be
        partial -- a header prints «1», a DIP «1» and «N» -- and a partial
        map must never define which pins exist):
        - COMPLETE map (it places every populated pin, or every position
          when no populated_pins is given): the map decides which numbers
          exist, whatever rows x cols (a U-numbered DIP keeps pin 16 on a
          2x3 grid of populated positions). Entries of a MAIN map for pins
          absent from populated_pins are a convention, not a reading, and
          are dropped with a note; entries of a DEDICATED map are printed
          numbers, so the pins are added to populated_pins with a note.
        - PARTIAL map (anchors): populated_pins is validated against
          1..rows x cols; the anchors are matched against the standard
          conventions (`_numbering_conventions`); exactly one fitting every
          anchor completes the map, declared «derived» in the notes; none or
          several: no map, automatic numbering, and a block text.
    """
    loose = _validated_populated_pins(raw_populated, None, [], None) or []
    mapped = {entry["pin"] for entry in pin_map} if pin_map else set()
    if pin_map:
        if loose:
            complete = set(loose) <= mapped
        else:
            complete = max_pin is not None and len(mapped) >= max_pin
    else:
        complete = False
    if pin_map and complete:
        populated = _validated_populated_pins(raw_populated, max_pin, notes, mapped=sorted(mapped))
        if populated:
            extra = sorted(pin for pin in mapped if pin not in populated)
            if extra and dedicated:
                populated = sorted(set(populated) | set(extra))
                notes.append(tx("pin_map: i pin {pins} hanno un numero stampato nel disegno ma "
                                "mancavano da populated_pins: aggiunti ai pin presenti. "
                                "Verificare sul disegno.", pins=", ".join(str(p) for p in extra)))
            elif extra:
                pin_map = [entry for entry in pin_map if entry["pin"] in populated]
                notes.append(tx("pin_map: scartate le posizioni dei pin {pins}, assenti da "
                                "populated_pins: nel disegno non c'e' un pin da leggere li', "
                                "quindi erano una convenzione e non una lettura. Verificare "
                                "sul disegno la posizione dei pin presenti.",
                                pins=", ".join(str(p) for p in extra)))
        return pin_map, populated, None
    populated = _validated_populated_pins(raw_populated, max_pin, notes, None)
    if not pin_map:
        return None, populated, None
    expected = len(populated) if populated else (max_pin or 0)
    anchors = {entry["pin"]: (entry["row"], entry["col"]) for entry in pin_map}
    fitting = [name for name, convention in _numbering_conventions(int(rows), int(cols)).items()
               if all(convention.get(pin) == position for pin, position in anchors.items())]
    summary = ", ".join(f"{pin}@({r},{c})" for pin, (r, c) in sorted(anchors.items()))
    if len(fitting) == 1:
        convention = _numbering_conventions(int(rows), int(cols))[fitting[0]]
        completed = [{"pin": pin, "row": row, "col": col}
                     for pin, (row, col) in sorted(convention.items())]
        if populated:
            missing = sorted(pin for pin in populated if pin not in convention)
            if missing:
                return None, populated, tx(
                    "Numerazione letta per {read} pin su {expected} ({anchors}); la convenzione "
                    "{name} che li completa non prevede i pin {missing}: numerazione da "
                    "verificare sul disegno prima di generare.",
                    read=len(anchors), expected=expected, anchors=summary, name=fitting[0],
                    missing=", ".join(str(p) for p in missing))
        notes.append(tx("Numerazione letta per {read} pin su {expected} ({anchors}): completata "
                        "con la convenzione {name}, l'unica coerente con i numeri stampati. "
                        "Valore DERIVATO, da verificare sul disegno.",
                        read=len(anchors), expected=expected, anchors=summary, name=fitting[0]))
        return completed, populated, None
    reason = (tx("nessuna convenzione standard e' coerente con i numeri stampati")
              if not fitting else
              tx("piu' convenzioni sono coerenti con i numeri stampati ({names})",
                 names=", ".join(fitting)))
    return None, populated, tx(
        "Numerazione letta per {read} pin su {expected} ({anchors}) e {reason}: le altre "
        "posizioni non hanno un numero stampato. Numerazione automatica colonna per colonna "
        "nei campi, da verificare sul disegno prima di generare.",
        read=len(anchors), expected=expected, anchors=summary, reason=reason)


def _validated_populated_pins(raw, max_pin: Optional[int], notes: list,
                              mapped: Optional[Sequence[int]] = None) -> Optional[list]:
    """Optional populated_pins: keep the valid, in-range pin numbers once each.

    Invalid entries (booleans, fractions, non-numbers, out of range) are
    dropped and reported in `notes`, like repeated numbers; the valid ones are
    kept, as for any partial response. None when nothing valid remains.

    @param max_pin Highest number of the automatic column-by-column numbering
        (rows x cols); None when the grid is unknown.
    @param mapped Pin numbers of an explicit, validated pin_map. With a map the
        numbering is a convention of the drawing (a DIP numbered in a U, empty
        positions), so a number is in range when the map places it, whatever
        rows x cols is: 1..max_pin only applies to the automatic numbering.
    """
    if raw is None or (isinstance(raw, (list, tuple)) and not raw):
        return None
    if not isinstance(raw, (list, tuple)):
        notes.append(tx("populated_pins scartato: valore non valido ({value!r}).", value=raw))
        return None
    allowed = set(mapped) if mapped else None
    kept, rejected, repeated = [], [], []
    for item in raw:
        number = _optional_number(item, positive=True, integer=True)
        if number is None:
            rejected.append(item)
        elif allowed is not None and number not in allowed:
            rejected.append(item)
        elif allowed is None and max_pin is not None and number > max_pin:
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

    Kept separate from the network call so that it can be tested without
    the API.

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
        # Optional field that came back empty ({} or «pins»: []): no explicit
        # pad, hence no package geometry. It is not an error of the answer
        # -- the schema imposes no minimum of pins -- and rejecting it would
        # throw away a paid extraction with valid matrix or module data.
        # The rule «senza pad non c'e' footprint» (no pads, no footprint)
        # stays in PackageGeometry.validate, where it is generated.
        if package_geometry:
            notes.append(tx("Geometria package senza pad espliciti ignorata: si usano i "
                            "parametri a matrice o del modulo."))
        package_geometry = None
    geometry_doubts = None
    if package_geometry is not None:
        package_geometry = _package_mapping_for_validation(package_geometry, notes)
        try:
            # Validate now, while the source response and its warnings are still
            # available; the GUI must never receive malformed pad coordinates.
            geometry = package_from_mapping(package_geometry)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(tx("Geometria package non valida: {exc}", exc=exc)) from exc
        # Structural contradictions (body over the pads, clockwise numbering,
        # bottom view also declared mirrored) are exposed, never repaired:
        # which reading is true is a question for the user (audit 2026-10-15).
        geometry_doubts = [doubt.as_dict() for doubt in package_doubts(geometry)] or None
        notes.extend(doubt["message"] for doubt in geometry_doubts or ())

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

    pin_map = None
    # The mirroring of pin_map has already been done (_canonicalize_answer):
    # here the map is seen from the top and is only checked again. For
    # castellated modules the positions come from the sides, not from rows/columns.
    if layout == "matrix" and data.get("pin_map") and cols:
        try:
            pin_map = _pin_map_seen_from_top(data.get("pin_map"), cols, "top", rows)
        except ExtractionError as exc:
            notes.append(tx("ATTENZIONE pin_map scartata ({exc}): numerazione automatica colonna "
                            "per colonna, da verificare sul pinout.", exc=exc))

    max_pin = rows * cols if layout == "matrix" and rows and cols else None
    pin_map_conflict = (data.get("_pin_map_conflict")
                        if isinstance(data.get("_pin_map_conflict"), str) else None)
    pin_map, populated, completion = _resolve_pin_numbering(
        pin_map, data.get("populated_pins"), rows, cols, max_pin,
        dedicated=bool(data.get("_dedicated_map")), notes=notes)
    if completion:
        # A partial map that no single convention completes: the GUI blocks
        # the automatic generation with this text, the user checks.
        pin_map_conflict = completion if not pin_map_conflict else pin_map_conflict + "\n" + completion

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
        edge_fillets=_validated_edge_fillets(data.get("edge_fillets"), notes),
        pinout=_validated_pinout(data.get("pinout"), notes),
        pin_map=pin_map,
        body_offset_x_mm=_opt_float("body_offset_x_mm"),
        body_color_hex=data.get("body_color_hex") or None,
        text_color_hex=data.get("text_color_hex") or None,
        package_geometry=package_geometry,
        lead_diameter_mm=_opt_float("lead_diameter_mm"),
        lead_exit=_validated_lead_exit(data.get("lead_exit"), notes),
        pin_map_conflict=pin_map_conflict,
        geometry_doubts=geometry_doubts,
    )
    if isinstance(data.get("_numbering_note"), str) and data["_numbering_note"]:
        notes.append(data["_numbering_note"])
    params.value_sources = _value_sources(data, params, sides, notes)
    if layout == "castellated":
        _drop_unread_row_spacing(params, notes)
        _apply_measured_lands(data, params, notes)
        _apply_measured_holes(data, params, notes)
        _downgrade_sizes_wider_than_the_pitch(params, notes)
    else:
        _lift_drill_above_the_lead(params, notes)
    notes.extend(_review_notes(sides, params.value_sources))
    if notes:
        params.notes = (params.notes + "\n" + "\n".join(notes)).strip()
    return params


def _validated_edge_fillets(raw, notes: list) -> Optional[list[dict]]:
    """Optional edge_fillets: only ``{"edge": <EDGE_NAMES>, "radius_mm": > 0}`` entries.

    A malformed entry is dropped with a note and the valid ones stay (review
    D2): the consumer reads ``entry["edge"]`` and ``float(entry["radius_mm"])``
    without checks, so nothing else may reach it. The radius must be a real
    finite number: numeric text is malformed like everywhere else.
    @return The valid fillets with only those two keys, or None when none is.
    """
    if raw is None or raw == []:
        return None
    entries = raw if isinstance(raw, (list, tuple)) else [raw]
    valid = []
    for index, entry in enumerate(entries):
        edge = entry.get("edge") if isinstance(entry, dict) else None
        radius = (_optional_number(entry.get("radius_mm"), positive=True)
                  if isinstance(entry, dict) else None)
        if isinstance(edge, str) and edge in EDGE_NAMES and radius is not None:
            valid.append({"edge": edge, "radius_mm": radius})
        else:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                            key=f"edge_fillets[{index}]", value=entry))
    return valid or None


def _validated_pinout(raw, notes: list) -> Optional[list[dict]]:
    """@brief Optional pinout: only ``{"pin": <number or label>, "name": <text>}`` entries.

    @param raw The ``pinout`` value of the model's answer.
    @param notes Extraction notes; every dropped or repaired entry adds one.
    @return The kept entries, each with only the two keys the schema
        declares, in the source order; None when none is kept.
    @note ``pin`` is a nonnegative integer (an integral float such as 3.0
        included; 0 is a real pad number on some packages and the symbol
        and pinout tables accept any pin text) or a nonempty label such as
        "A2" for grid packages. An entry without a usable pin id is dropped
        with a note: there is no pin to keep (review D2).
    @note A missing ``name``, or one that is not text, does not drop the pin: the symbol
        must keep every pin of the source pinout, so the pin stays with an
        empty name and a note names the discarded ``pinout[i].name`` (review G5).
    """
    if raw is None or raw == []:
        return None
    entries = raw if isinstance(raw, (list, tuple)) else [raw]
    valid = []
    for index, entry in enumerate(entries):
        pin = None
        if isinstance(entry, dict):
            raw_pin = entry.get("pin")
            if isinstance(raw_pin, str):
                pin = raw_pin.strip() or None
            else:
                pin = _optional_number(raw_pin, integer=True)
                if pin is not None and pin < 0:
                    pin = None
        if pin is None:
            notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                            key=f"pinout[{index}]", value=entry))
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            # Only the name is discarded (the note names the field): the pin stays.
            notes.append(tx("{key} scartato: valore non valido ({value!r}).",
                            key=f"pinout[{index}].name", value=name))
            name = ""
        valid.append({"pin": pin, "name": name})
    return valid or None


def _validated_lead_exit(raw, notes: list) -> Optional[str]:
    """Optional lead_exit: one of LEAD_EXITS, else None with a note ("unknown" is None too)."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().lower() in LEAD_EXITS:
        value = raw.strip().lower()
        return None if value == "unknown" else value
    notes.append(tx("lead_exit scartato: valore non valido ({value!r}).", value=raw))
    return None


#: Clearance, in mm, between a lead and the hole it goes through when the
#: hole has to be estimated from the lead: the usual 0.2-0.3 mm of the
#: project rules for through-hole parts (middle of the range, declared).
LEAD_TO_HOLE_CLEARANCE_MM = 0.25
#: Annular copper, in mm, added to an estimated hole for an estimated pad.
HOLE_TO_PAD_ANNULUS_MM = 0.8
#: Smallest copper ring per side, in mm, a through-hole pad must keep around
#: a lifted hole for the pad to be solderable: a declared manufacturing
#: minimum (IPC-7251 level B order of magnitude), not a value tuned to a part.
MIN_ANNULAR_RING_MM = 0.15


def _lift_drill_above_the_lead(params: "ExtractedParams", notes: list) -> None:
    """A hole not wider than the lead is not a measurement: it becomes an estimate that fits.

    Structural, not a threshold: a lead cannot pass through a hole of its
    own diameter or smaller, whatever the part. When the drawing quotes the
    lead (`lead_diameter_mm`) and the matrix drill is not larger (TEL8 run of
    2026-10-10, 11:38: lead Ø0.5 and drill 0.5 «estimated»), the drill
    becomes lead + LEAD_TO_HOLE_CLEARANCE_MM, marked «estimated» in
    `value_sources` whatever the model declared -- a «read» hole smaller
    than the lead was read wrong, and the note says so. A pad that no longer
    clears the lifted drill is lifted too (drill + HOLE_TO_PAD_ANNULUS_MM)
    and marked «estimated». Nothing happens without a lead diameter.

    @param params Result under construction; drill, pad and sources updated.
    @param notes Notes of the result.
    """
    lead, drill = params.lead_diameter_mm, params.drill_mm
    if not (isinstance(lead, (int, float)) and isinstance(drill, (int, float))):
        return
    if drill > lead:
        return
    sources = dict(params.value_sources or {})
    lifted = round(lead + LEAD_TO_HOLE_CLEARANCE_MM, 3)
    notes.append(tx(
        "ATTENZIONE foro {drill:.2f} mm non maggiore del terminale {lead:.2f} mm "
        "(provenienza dichiarata: {source}): un terminale non passa in un foro del suo "
        "diametro. Foro portato a {lifted:.2f} mm (terminale + {clearance:.2f} mm), valore "
        "STIMATO da verificare.", drill=drill, lead=lead,
        source=sources.get("drill_mm", UNDECLARED_SOURCE), lifted=lifted,
        clearance=LEAD_TO_HOLE_CLEARANCE_MM))
    params.drill_mm = lifted
    sources["drill_mm"] = "estimated"
    pad = params.pad_diameter_mm
    if isinstance(pad, (int, float)) and pad < lifted + 2 * MIN_ANNULAR_RING_MM:
        params.pad_diameter_mm = round(lifted + HOLE_TO_PAD_ANNULUS_MM, 3)
        sources["pad_diameter_mm"] = "estimated"
        notes.append(tx("Piazzola {pad:.2f} mm non piu' larga del foro corretto: portata a "
                        "{new:.2f} mm, valore STIMATO da verificare.",
                        pad=pad, new=params.pad_diameter_mm))
    params.value_sources = sources


def _drop_unread_row_spacing(params: "ExtractedParams", notes: list) -> None:
    """Keep a castellated row spacing only when the model READ it in a document.

    The row spacing is the one number that moves the through-holes of a
    castellated module across the board: the generator places them at
    (extent - spacing) / 2 from the edge. A value the model derived or
    estimated is not that measurement -- on the RS485 run of 2026-09-30 it
    reported the pitch of one side as `row_spacing_mm` (its own words:
    «derivato, coincidente col passo, non e' una distanza fra file», derived,
    equal to the pitch, not a distance between rows) and the holes
    landed 18.86 mm in, with copper lands across half the board (HANDOFF
    point 167). Provenance is a declared attribute of the answer, not a
    threshold: only "read" keeps the value; derived, estimated and
    undeclared drop it, with a note that says the value, its provenance and
    where it would have put the holes. The generator then falls back to the
    minimum inset behind the half-hole (or to an offset typed by hand).

    @param params Result under construction; `row_spacing_mm` and its
        provenance are removed when not read.
    @param notes Notes of the result.
    """
    spacing = params.row_spacing_mm
    if spacing is None:
        return
    sources = dict(params.value_sources or {})
    source = sources.get("row_spacing_mm", UNDECLARED_SOURCE)
    if source == "read":
        return
    from ..generators.castellated_module import opposite_row_edges, through_hole_inset_from_rows
    edges = [side["edge"] for side in (params.castellated_sides or ()) if isinstance(side, dict)]
    pair = opposite_row_edges(edges)
    extent = None
    if pair == ("left", "right"):
        extent = params.body_length_mm
    elif pair == ("top", "bottom"):
        extent = params.body_width_mm
    inset = through_hole_inset_from_rows(extent, spacing) if extent else None
    where = (tx(" (avrebbe messo i fori passanti a {inset:.2f} mm dal bordo)", inset=inset)
             if inset is not None else "")
    notes.append(tx(
        "Distanza fra le file {spacing} mm riportata dal modello come «{source}», non letta in "
        "un documento: non usata per posizionare i fori passanti dei contatti castellated{where}. "
        "Restano la stima minima dietro al semi-foro o la distanza dal bordo indicata a mano.",
        spacing=spacing, source=_SOURCE_LABELS.get(source, source), where=where))
    params.row_spacing_mm = None
    sources.pop("row_spacing_mm", None)
    params.value_sources = sources


def _apply_measured_lands(data: dict, params: "ExtractedParams", notes: list) -> None:
    """Adopt the land widths the photo measured (`data["_measured_lands"]`, edge_lands).

    Per edge with a measured width: a `pad_width_mm` the model READ in a
    document is kept (a document measure outranks a photo measure) and the
    two are compared in a note; any other provenance -- derived, estimated,
    undeclared, or no value at all -- is replaced by the measured width with
    provenance MEASURED_SOURCE, which the GUI treats as declared. The inward
    extent is recorded on the side as `land_inward_mm` (informative: the
    footprint's pad length includes the overhang beyond the edge, which no
    photo of the module shows). The measurement block is a private key the
    photo stage writes; a raw answer never carries it (_canonicalize_answer
    drops it).

    @param data Answer the photo stage annotated.
    @param params Result under construction; sides and value_sources updated.
    @param notes Notes of the result.
    """
    lands = data.get("_measured_lands")
    if not isinstance(lands, dict) or not lands:
        return
    sources = dict(params.value_sources or {})
    for side in params.castellated_sides or ():
        if not isinstance(side, dict):
            continue
        edge = side.get("edge")
        land = lands.get(edge)
        if not isinstance(land, dict):
            continue
        width = _optional_number(land.get("width_mm"), positive=True)
        if width is None:
            continue
        label = EDGE_LABELS.get(edge, edge)
        tolerance = _optional_number(land.get("tolerance_mm")) or 0.0
        inward = _optional_number(land.get("inward_mm"), positive=True)
        if inward is not None:
            side["land_inward_mm"] = round(float(inward), 3)
        key = f"castellated_sides.{edge}.pad_width_mm"
        declared = _optional_number(side.get("pad_width_mm"), positive=True)
        if sources.get(key) == "read" and declared is not None:
            agreement = (tx("coerente") if abs(declared - width) <= tolerance + 1e-9
                         else tx("DIVERSA: verificare il disegno"))
            notes.append(tx(
                "Lato {label}: larghezza delle piazzole letta nei documenti {declared} mm, "
                "misurata sulla foto {width:.2f} mm (±{tolerance:.2f}): si tiene la lettura, "
                "{agreement}.", label=label, declared=declared, width=width,
                tolerance=tolerance, agreement=agreement))
            continue
        previous = (tx("stima del modello {value} mm", value=declared) if declared is not None
                    else tx("nessun valore del modello"))
        side["pad_width_mm"] = round(float(width), 3)
        sources[key] = MEASURED_SOURCE
        notes.append(tx(
            "Lato {label}: larghezza delle piazzole misurata sulla foto: {width:.2f} mm "
            "(±{tolerance:.2f}), al posto di: {previous}.",
            label=label, width=width, tolerance=tolerance, previous=previous))
    params.value_sources = sources


def _apply_measured_holes(data: dict, params: "ExtractedParams", notes: list) -> None:
    """Adopt the contact style the photo read (`data["_measured_holes"]`, edge_lands).

    Per edge with a verdict: when the style the photo read differs from the
    declared one, the side takes the photo's style, with a note that says
    the evidence (how many lands agreed, the opening and the hole widths);
    an equal style is confirmed silently. Only the STYLE is adopted: the hole
    width read on the photo is the visible barrel, not the drill, and is
    reported in the note for a person, never written as a size. The
    measurement block is a private key the photo stage writes; a raw answer
    never carries it (_canonicalize_answer drops it).

    @param data Answer the photo stage annotated.
    @param params Result under construction; sides updated.
    @param notes Notes of the result.
    """
    holes = data.get("_measured_holes")
    if not isinstance(holes, dict) or not holes:
        return
    for side in params.castellated_sides or ():
        if not isinstance(side, dict):
            continue
        edge = side.get("edge")
        reading = holes.get(edge)
        if not isinstance(reading, dict) or reading.get("style") not in CONTACT_STYLES:
            continue
        declared = side.get("contact_style")
        if declared == reading["style"]:
            continue
        label = EDGE_LABELS.get(edge, edge)
        side["contact_style"] = reading["style"]
        side["_contact_style_evidence"] = tx(
            "letto sulla foto: {agreeing} piazzole su {contacts} concordi, foro {drill:.2f} mm, "
            "apertura sul bordo {chord:.2f} mm",
            agreeing=reading.get("agreeing", 0), contacts=reading.get("contacts", 0),
            drill=float(reading.get("drill_mm") or 0.0), chord=float(reading.get("chord_mm") or 0.0))
        notes.append(tx(
            "Lato {label}: tipo di contatto {measured} letto sulla foto ({evidence}), al posto di "
            "«{declared}» dichiarato dal modello.",
            label=label, measured=reading["style"], evidence=side["_contact_style_evidence"],
            declared=declared or "?"))


#: Per-side sizes measured ALONG the edge, across the pitch: two adjacent
#: contacts cannot share copper, so the pad width (the along-edge size on
#: every edge, see castellated_module.pad_positions) cannot reach the pitch.
#: The hole diameters have the same physical bound and are already DROPPED
#: by _add_hole_sizes when they reach it; the pad width is kept as an
#: estimate instead, because the generator knows how to shrink a land.
_ACROSS_PITCH_KEYS = ("pad_width_mm",)


def _downgrade_sizes_wider_than_the_pitch(params: "ExtractedParams", notes: list) -> None:
    """A side size that reaches the pitch is not a measurement: keep it, as an estimate.

    Structural, not a threshold: on a side with more than one contact, a pad
    width equal to or wider than the pitch means two adjacent contacts
    sharing copper -- no drawing shows that (the hole diameters, with the
    same bound, are dropped earlier by _add_hole_sizes). When
    the model still declares such a value «read» or «derived» (the RS485 run
    of 2026-09-30: «larghezza piazzola 2.54 mm derivata dal foro stimato»,
    on a 2.54 mm pitch, HANDOFF point 168), the value stays for the
    generator to reduce (an AssumedSize shrinks to the largest that fits,
    and says so), but its provenance becomes «estimated»: a measure is never
    touched, an impossible «measure» is not one. A value that fits the pitch
    keeps whatever provenance it has, including one the generator will
    refuse for the fabrication gap: that is a real disagreement to show.

    @param params Result under construction; `value_sources` is updated.
    @param notes Notes of the result.
    """
    sources = dict(params.value_sources or {})
    changed = False
    for side in params.castellated_sides or ():
        if not isinstance(side, dict):
            continue
        edge, count, pitch = side.get("edge"), side.get("count"), side.get("pitch_mm")
        if not (isinstance(count, int) and count > 1) or not isinstance(pitch, (int, float)):
            continue
        for key in _ACROSS_PITCH_KEYS:
            value = side.get(key)
            source_key = f"castellated_sides.{edge}.{key}"
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and value >= float(pitch) - 1e-9
                    and sources.get(source_key) in ("read", "derived")):
                notes.append(tx(
                    "Lato {label}: {field} = {value} mm dichiarato «{source}» ma pari o superiore "
                    "al passo di {pitch} mm dello stesso lato: due contatti adiacenti si "
                    "sovrapporrebbero, quindi non e' una misura. Trattato come stima, che il "
                    "generatore riduce e annota.",
                    label=EDGE_LABELS.get(edge, edge), field=key, value=value,
                    source=_SOURCE_LABELS.get(sources[source_key], sources[source_key]),
                    pitch=pitch))
                sources[source_key] = "estimated"
                changed = True
    if changed:
        params.value_sources = sources


#: Italian words for the provenances, for the notes.
_SOURCE_LABELS = {"read": "letta", "derived": "derivata", "estimated": "stimata",
                  UNDECLARED_SOURCE: "senza provenienza"}
