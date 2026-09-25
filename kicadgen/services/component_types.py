"""@brief Component types declared before the AI extraction, each with its instruction block.

Tipologie di componente da dichiarare prima dell'estrazione AI.

Sapere in anticipo *che cosa* si sta leggendo cambia completamente quali
quote il modello deve cercare nel datasheet: un componente SMD non ha
fori e le dimensioni dei pad si leggono dal "recommended land pattern",
un modulo con castellated holes ha i semi-fori sul bordo del PCB, un
header THT ha pad e drill quotati direttamente.

Ogni tipologia porta con se' un blocco di istruzioni che la GUI inserisce
nelle note del singolo componente (tab "Progetto"): da li' resta
liberamente modificabile prima di lanciare l'estrazione -- e' un punto di
partenza, non un testo bloccato.

Il blocco e' delimitato da due marcatori, cosi' cambiando tipologia si
sostituisce solo il blocco e non quello che l'utente ha scritto di suo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BLOCK_START = "--- Tipologia componente (generato, modificabile) ---"
BLOCK_END = "--- fine tipologia componente ---"


@dataclass(frozen=True)
class ComponentType:
    """@brief Selectable component type with the instruction passed to the model.

    Una tipologia selezionabile, con l'istruzione da passare al modello.
    """

    key: str
    label: str
    prompt: str


_SINGLE_THT = ComponentType(
    key="single_tht",
    label="Componente singolo a fori passanti (THT)",
    prompt=(
        "TIPOLOGIA: componente SINGOLO a FORI PASSANTI (THT). E' un singolo "
        "componente discreto o connettore, con terminali che attraversano il PCB "
        "e vanno in fori metallizzati.\n"
        "- Tutti i pad hanno un foro: compila sempre pad_diameter_mm e drill_mm.\n"
        "- Leggi queste due quote dalla tabella/disegno \"Recommended PCB layout\", "
        "\"PCB hole dimensions\" o simili; se il datasheet da' solo il diametro del "
        "terminale, stima drill_mm = diametro terminale + 0.2..0.3 mm e "
        "pad_diameter_mm = drill_mm + 0.7..0.9 mm, scrivendolo nelle note.\n"
        "- pitch_mm e row_spacing_mm si leggono dalla vista dall'alto/dal basso del "
        "disegno meccanico quotato del componente o da una misura diretta.\n"
        "- Le quote del corpo (body_*) sono l'ingombro del componente reale, "
        "non quello dei pad."
    ),
)

_SINGLE_SMD = ComponentType(
    key="single_smd",
    label="Componente singolo SMD",
    prompt=(
        "TIPOLOGIA: componente SINGOLO SMD (montaggio superficiale). I terminali "
        "appoggiano sulla superficie del PCB: NON esiste alcun foro.\n"
        "- Prendi le dimensioni dei pad dalla tabella \"Recommended Land Pattern\" / "
        "\"Recommended Solder Pad Layout\" / \"Footprint\", NON dalle quote del corpo "
        "del componente: sono valori diversi (il land pattern e' piu' grande dei "
        "terminali).\n"
        "- pitch_mm e' il passo tra i centri dei pad adiacenti sullo stesso lato; "
        "row_spacing_mm e' la distanza tra i centri dei pad delle due file opposte "
        "(quota spesso indicata come \"span\" tra i centri, non tra i bordi).\n"
        "- Lo schema di questo strumento descrive pad THT: imposta drill_mm = 0 "
        "(nessun foro) e usa pad_diameter_mm per la dimensione MAGGIORE del pad, "
        "riportando in notes le quote esatte del land pattern (larghezza x "
        "lunghezza di ogni pad) e segnalando che il componente e' SMD.\n"
        "- confidence al massimo \"medium\" se il datasheet non riporta un land "
        "pattern raccomandato."
    ),
)

_AGGREGATE_THT = ComponentType(
    key="aggregate_tht",
    label="Componente aggregato a fori passanti (modulo/scheda)",
    prompt=(
        "TIPOLOGIA: componente AGGREGATO a FORI PASSANTI -- un modulo o schedina "
        "assemblata (es. convertitore DC-DC, modulo radio, breakout) che si monta "
        "come un unico componente tramite pin passanti.\n"
        "- Il corpo e' l'ingombro dell'intero modulo (case o PCB del modulo), non "
        "quello di un singolo componente montato sopra.\n"
        "- Attenzione alla numerazione: i moduli DIP-style numerano i pin a U "
        "(1..N lungo una fila, poi a ritroso sull'altra), non a colonne come i pin "
        "header: se il disegno mostra la disposizione fisica, compila pin_map.\n"
        "- Il pinout dei moduli e' quasi sempre disegnato in BOTTOM VIEW: "
        "dichiaralo in pin_map_view (alla specchiatura pensa il programma).\n"
        "- row_spacing_mm (distanza tra le file, tipicamente 7.62/10.16/15.24 mm) "
        "e' molto maggiore di pitch_mm (di solito 2.54 mm): non confonderli.\n"
        "- Se la matrice non e' completamente popolata, elenca in populated_pins "
        "solo le posizioni con un pin fisico."
    ),
)

_AGGREGATE_CASTELLATED = ComponentType(
    key="aggregate_castellated",
    label="Componente aggregato con piazzole a mezzo foro (castellated holes)",
    prompt=(
        "TIPOLOGIA: componente AGGREGATO con PIAZZOLE A MEZZO FORO (castellated "
        "holes) -- modulo su PCB (es. modulo WiFi/BLE tipo ESP32, nRF) i cui "
        "contatti sono semi-fori metallizzati sul bordo della schedina, saldati "
        "come SMD sulla scheda madre.\n"
        "- NON sono fori passanti: sulla scheda madre si realizzano pad SMD "
        "rettangolari sotto il bordo del modulo. Usa la tabella \"Recommended PCB "
        "land pattern\" / \"PCB layout\" del datasheet del modulo.\n"
        "- Lo schema di questo strumento descrive pad THT: imposta drill_mm = 0 e "
        "usa pad_diameter_mm per la LARGHEZZA della piazzola, riportando in notes "
        "la lunghezza della piazzola, il diametro del semi-foro e il fatto che i "
        "contatti sono castellated.\n"
        "- pitch_mm e' il passo tra semi-fori adiacenti sullo stesso lato; "
        "row_spacing_mm e' la distanza tra i centri delle file opposte di semi-fori "
        "(spesso ricavabile come larghezza del modulo meno la profondita' dei "
        "semi-fori: se la ricavi cosi', scrivilo nelle note).\n"
        "- Se il modulo ha contatti su tre o quattro lati, o un pad termico "
        "centrale, segnalalo in notes: lo schema attuale descrive solo una matrice "
        "di pin su due file.\n"
        "- Il corpo e' l'intero modulo (PCB + schermatura metallica): l'altezza "
        "include la schermatura."
    ),
)

COMPONENT_TYPES: tuple[ComponentType, ...] = (
    _SINGLE_THT,
    _SINGLE_SMD,
    _AGGREGATE_THT,
    _AGGREGATE_CASTELLATED,
)

_BY_KEY = {ct.key: ct for ct in COMPONENT_TYPES}


def get(key: Optional[str]) -> Optional[ComponentType]:
    """@brief Return the component type for `key`, or None when unknown or not given.

    Tipologia dal suo `key`, oppure None se sconosciuta o non indicata.
    """
    return _BY_KEY.get(key) if key else None


def build_block(component_type: ComponentType) -> str:
    """@brief Return the type instruction wrapped in the block markers, ready for the notes.

    Blocco di testo delimitato dai marcatori, pronto per le note.
    """
    return f"{BLOCK_START}\n{component_type.prompt}\n{BLOCK_END}"


def strip_block(text: str) -> str:
    """@brief Remove the component type block from the notes, keeping the user text.

    Toglie dal testo il blocco tipologia, lasciando intatto il resto.

    Serve a cambiare tipologia senza cancellare le note scritte a mano:
    si rimuove il blocco precedente e si reinserisce quello nuovo.
    """
    if BLOCK_START not in text:
        return text.strip()

    before, _, rest = text.partition(BLOCK_START)
    _, end_found, after = rest.partition(BLOCK_END)
    # Blocco senza marcatore di chiusura (l'utente potrebbe averlo
    # cancellato): si butta via tutto il resto del blocco, non il testo
    # precedente, che e' sicuramente suo.
    remainder = after if end_found else ""
    return "\n".join(part for part in (before.strip(), remainder.strip()) if part).strip()


def apply_block(text: str, component_type: Optional[ComponentType]) -> str:
    """@brief Return the notes with the block of the chosen type at the top (only removed when None).

    Testo delle note con il blocco della tipologia scelta in testa.

    Con `component_type=None` (tipologia non indicata) si limita a
    rimuovere l'eventuale blocco precedente.
    """
    cleaned = strip_block(text or "")
    if component_type is None:
        return cleaned
    block = build_block(component_type)
    return f"{block}\n\n{cleaned}".strip() if cleaned else block
