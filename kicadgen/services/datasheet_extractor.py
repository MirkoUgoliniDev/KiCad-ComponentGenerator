"""Estrazione parametri footprint da datasheet tramite l'API Claude.

Manda i file di documentazione (PDF/immagini) all'API Anthropic e
richiede di compilare uno strumento (tool use) con lo schema dei
parametri del pin header, cosi' la risposta e' sempre JSON strutturato
valido, mai testo libero da fare il parsing a mano.

Richiede la variabile d'ambiente ANTHROPIC_API_KEY (la libreria
`anthropic` la legge automaticamente). Nessuna chiave viene letta da
file o inserita nel codice.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..cad3d.annotations import EDGE_NAMES

DEFAULT_MODEL = "claude-sonnet-5"
MAX_FILES = 5
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
        "required": ["rows", "cols", "pitch_mm", "row_spacing_mm", "pad_diameter_mm", "drill_mm", "confidence"],
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
    "Rispondi SOLO chiamando lo strumento fornito."
)

# Alias pubblico: la GUI (tab "Prompt LLM") lo mostra come testo di partenza
# editabile, e passa la versione modificata a extract_pin_header_params().
DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT


@dataclass
class ExtractedParams:
    name: str
    description: str
    tags: str
    rows: int
    cols: int
    pitch_mm: float
    row_spacing_mm: float
    pad_diameter_mm: float
    drill_mm: float
    confidence: str
    notes: str
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
    if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ExtractionError(
            f"Formato non supportato: {path.name} (ammessi: {', '.join(sorted(_SUPPORTED_SUFFIXES))})"
        )
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ExtractionError(f"{path.name} troppo grande ({size / 1_048_576:.1f} MB, max 20 MB)")

    data_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    media_type = _guess_media_type(path)

    block_type = "document" if media_type == "application/pdf" else "image"
    return {
        "type": block_type,
        "source": {"type": "base64", "media_type": media_type, "data": data_b64},
    }


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


def extract_pin_header_params(
    doc_paths: list[Path],
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    include_nc_pins: bool = True,
) -> ExtractedParams:
    """Chiama l'API Claude sui file indicati e ritorna i parametri estratti.

    Due livelli di istruzioni, entrambi opzionali:
    - `system_prompt` sovrascrive DEFAULT_SYSTEM_PROMPT ed e' l'impostazione
      GLOBALE (menu Configurazione della GUI), valida per ogni componente;
    - `extra_instructions` sono note specifiche del SINGOLO componente
      ("il case e' metallico", "ignora il connettore laterale"), accodate al
      messaggio utente insieme ai documenti.

    `include_nc_pins` (default True) decide se i pin marcati NC nel pinout
    vanno comunque generati: sono fisicamente presenti sul componente, e senza
    il loro pad il pezzo non entrerebbe nella scheda.

    Lo schema del tool resta invece fisso: e' quello che garantisce che la
    risposta sia sempre JSON strutturato valido.

    Solleva ExtractionError per problemi applicativi (chiave mancante, file
    non supportati/troppo grandi, nessuna chiamata allo strumento nella
    risposta). Le eccezioni della libreria `anthropic` (rete, auth, rate
    limit) si propagano cosi' come sono.
    """
    if not doc_paths:
        raise ExtractionError("Nessun file selezionato.")
    if len(doc_paths) > MAX_FILES:
        raise ExtractionError(f"Troppi file selezionati (max {MAX_FILES} per chiamata).")

    try:
        import anthropic
    except ImportError as exc:
        raise ExtractionError(
            "Libreria 'anthropic' non installata. Esegui: pip install anthropic"
        ) from exc

    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ExtractionError(
            "Variabile d'ambiente ANTHROPIC_API_KEY non impostata. "
            "Imposta la tua API key Anthropic prima di usare l'estrazione AI."
        )

    content_blocks = [_file_to_content_block(p) for p in doc_paths]
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

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=1024,
        system=system_prompt or _SYSTEM_PROMPT,
        tools=[_EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": content_blocks}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            data = block.input
            populated = data.get("populated_pins")

            def _opt_float(key: str) -> Optional[float]:
                value = data.get(key)
                return float(value) if value is not None else None

            return ExtractedParams(
                name=data.get("name") or "",
                description=data.get("description") or "",
                tags=data.get("tags") or "",
                rows=int(data["rows"]),
                cols=int(data["cols"]),
                pitch_mm=float(data["pitch_mm"]),
                row_spacing_mm=float(data["row_spacing_mm"]),
                pad_diameter_mm=float(data["pad_diameter_mm"]),
                drill_mm=float(data["drill_mm"]),
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
                pin_map=_pin_map_seen_from_top(
                    data.get("pin_map"), int(data["cols"]), data.get("pin_map_view"),
                ),
                body_color_hex=data.get("body_color_hex") or None,
                text_color_hex=data.get("text_color_hex") or None,
            )

    raise ExtractionError("La risposta dell'API non contiene i parametri attesi.")
