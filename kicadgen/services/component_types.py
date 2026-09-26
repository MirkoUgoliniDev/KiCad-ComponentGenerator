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


# The instructions below describe only what the extraction tool schema can
# express (datasheet_extractor._EXTRACTION_TOOL): every field they name exists
# there, and tests/test_component_type_instructions.py checks it. They carry no
# example of a particular product: a type is a structural class of parts.

_SINGLE_THT = ComponentType(
    key="single_tht",
    label="Componente singolo a fori passanti (THT)",
    prompt=(
        "TIPOLOGIA: componente SINGOLO a FORI PASSANTI (THT). E' un singolo "
        "componente discreto o connettore, con terminali che attraversano il PCB "
        "e vanno in fori metallizzati.\n"
        "- Terminali su una griglia regolare: layout \"matrix\" con rows, cols, "
        "pad_diameter_mm e drill_mm; pitch_mm solo con piu' di una colonna, "
        "row_spacing_mm solo con piu' di una riga. Se i terminali non stanno su "
        "una griglia regolare, descrivi ogni pad in package_geometry (pad_type "
        "\"thru_hole\" con il suo drill_mm).\n"
        "- Ogni pad ha un foro: drill_mm e' maggiore di zero e minore di "
        "pad_diameter_mm. Leggi le due quote dalla tabella/disegno \"Recommended "
        "PCB layout\", \"PCB hole dimensions\" o simili; se il datasheet da' solo "
        "il diametro del terminale, stima drill_mm = diametro terminale + "
        "0.2..0.3 mm e pad_diameter_mm = drill_mm + 0.7..0.9 mm, scrivendolo "
        "nelle note.\n"
        "- pitch_mm e row_spacing_mm sono quote distinte e si leggono dalla vista "
        "dall'alto o dal basso del disegno meccanico quotato, non dalla vista "
        "laterale: dichiara in contacts_view quale vista hai letto (la "
        "specchiatura la fa il programma) e in drawing_unit l'unita' delle quote.\n"
        "- Le quote del corpo (body_*) sono l'ingombro del componente reale, "
        "non quello dei pad."
    ),
)

_SINGLE_SMD = ComponentType(
    key="single_smd",
    label="Componente singolo SMD",
    prompt=(
        "TIPOLOGIA: componente SINGOLO SMD (montaggio superficiale). I terminali "
        "appoggiano sulla superficie del PCB: nessun pad ha un foro.\n"
        "- Descrivi i pad in package_geometry, che per un componente SMD e' la "
        "fonte del footprint: un elemento per pin con number, centro (x_mm, "
        "y_mm), dimensioni del pad (pad_length_mm, pad_width_mm), pad_type "
        "\"smd\" e, se il pad e' ruotato, rotation_deg; family secondo il "
        "package, oppure \"custom\". Un pad esposto o termico e' un pin a se', "
        "con il numero che gli da' il datasheet.\n"
        "- Le dimensioni dei pad vengono dalla tabella \"Recommended Land "
        "Pattern\" / \"Recommended Solder Pad Layout\" / \"Footprint\", NON dalle "
        "quote del corpo o dei terminali: il land pattern e' di norma piu' grande "
        "dei terminali.\n"
        "- Indica in source_view di package_geometry la vista da cui leggi le "
        "coordinate (la specchiatura della vista dal basso la fa il programma) e "
        "in drawing_unit l'unita' delle quote.\n"
        "- Non compilare drill_mm: un pad SMD non ha foro. rows, cols, pitch_mm "
        "e row_spacing_mm sono facoltativi e solo informativi.\n"
        "- confidence al massimo \"medium\" se il datasheet non riporta un land "
        "pattern raccomandato."
    ),
)

_AGGREGATE_THT = ComponentType(
    key="aggregate_tht",
    label="Scheda con componenti · connettori passanti",
    prompt=(
        "TIPOLOGIA: SCHEDA CON COMPONENTI a FORI PASSANTI -- un modulo o una "
        "schedina assemblata che si monta come un unico componente tramite pin "
        "passanti.\n"
        "- Il corpo e' l'ingombro dell'intero modulo (case o PCB del modulo), non "
        "quello di un singolo componente montato sopra.\n"
        "- Pin su griglia regolare: layout \"matrix\". Se la numerazione non segue "
        "l'ordine colonna per colonna (per esempio a U: 1..N lungo una fila, poi "
        "a ritroso sull'altra), compila pin_map con le posizioni come appaiono "
        "nel disegno.\n"
        "- Dichiara in contacts_view da che lato e' visto il disegno dei pin (i "
        "pinout dei moduli sono spesso in BOTTOM VIEW): la specchiatura la fa il "
        "programma, anche quando la numerazione segue l'ordine delle colonne e "
        "pin_map e' omessa. body_offset_x_mm e body_offset_y_mm si leggono sullo "
        "stesso disegno.\n"
        "- row_spacing_mm (distanza tra file adiacenti) e pitch_mm (passo lungo la "
        "fila) sono quote distinte: leggile separatamente. Con una sola fila "
        "row_spacing_mm si omette.\n"
        "- Se la matrice non e' completamente popolata, elenca in populated_pins "
        "solo le posizioni con un pin fisico.\n"
        "- Dichiara in drawing_unit l'unita' delle quote del disegno."
    ),
)

_AGGREGATE_CASTELLATED = ComponentType(
    key="aggregate_castellated",
    label="Scheda con componenti · connettori castellated (mezzo foro)",
    prompt=(
        "TIPOLOGIA: SCHEDA CON COMPONENTI con PIAZZOLE A MEZZO FORO (castellated "
        "holes) -- modulo su PCB i cui contatti sono metallizzazioni sul bordo "
        "della schedina, saldate come SMD sulla scheda madre.\n"
        "- layout \"castellated\": descrivi i contatti in castellated_sides, un "
        "elemento per ogni lato che li porta (fino a quattro), ciascuno con il "
        "suo count, il suo pitch_mm e, se il gruppo non e' centrato, offset_mm; "
        "lati diversi possono avere numero di contatti e passo diversi.\n"
        "- La numerazione fisica dei pad (first_pin, reverse) e' unica su tutti "
        "i lati; le sigle serigrafate vanno in pin_names, non nei numeri.\n"
        "- Per ogni lato leggi dalla foto del bordo contact_style e, se quotati o "
        "visibili, half_hole_diameter_mm (diametro del semi-foro sul bordo) e "
        "through_hole_drill_mm (diametro del foro intero); dalla tabella "
        "\"Recommended PCB land pattern\" pad_length_mm e pad_width_mm della "
        "piazzola sulla scheda madre.\n"
        "- Dichiara in contacts_view da che lato e' visto il disegno o la foto da "
        "cui leggi i lati: la specchiatura la fa il programma.\n"
        "- Contatti che non stanno sul bordo (per esempio un pad termico o una "
        "fila interna) non entrano in castellated_sides: segnalali in notes.\n"
        "- Il corpo e' l'intero modulo: body_height_mm include l'eventuale "
        "schermatura metallica, pcb_thickness_mm e' lo spessore della sola PCB.\n"
        "- Dichiara in drawing_unit l'unita' delle quote del disegno."
    ),
)

COMPONENT_TYPES: tuple[ComponentType, ...] = (
    _SINGLE_THT,
    _SINGLE_SMD,
    _AGGREGATE_THT,
    _AGGREGATE_CASTELLATED,
)

_BY_KEY = {ct.key: ct for ct in COMPONENT_TYPES}

#: Former labels of the built-in types, replaced in an existing profile
#: database only where the stored label is still exactly the old one (a label
#: the user edited is left alone). Names clarified by the user on 2026-09-26:
#: a «board with components» is analysed differently from a single component.
SUPERSEDED_LABELS: dict[str, tuple[str, ...]] = {
    "aggregate_tht": ("Componente aggregato a fori passanti (modulo/scheda)",),
    "aggregate_castellated": ("Componente aggregato con piazzole a mezzo foro (castellated holes)",),
}

#: SHA-256 (hex digest of the UTF-8 text) of the former built-in instructions
#: of each type. A profile database whose instruction for a built-in type is
#: still exactly one of them receives the current text (analysis_profiles);
#: an instruction the user edited never matches and is kept, as for
#: SUPERSEDED_LABELS. Digests keep the old texts, in the git history of this
#: file, out of the code. They were replaced on 2026-09-26 because they
#: contradicted the extraction schema: «imposta drill_mm = 0» for SMD pads the
#: pin grid cannot draw, «lo schema attuale descrive solo una matrice di pin su
#: due file» although castellated_sides and package_geometry exist, and
#: product examples in a structural type.
SUPERSEDED_INSTRUCTION_DIGESTS: dict[str, frozenset[str]] = {
    "single_tht": frozenset({
        "b4ac3268f0ad952b7e6547d640017fba687cec112b7473c04464eaff04ef15a0",
        "a22b90207ad9a7c73c6cb09623cd0f7b9a1b948eda120ea1985dbc4780328555",
    }),
    "single_smd": frozenset({
        "8c8b7b749de39798ae1b1cf71b263b94dc34b3c82f338a828e7bcf6b8c7326b2",
    }),
    "aggregate_tht": frozenset({
        "dab44a80479988f9b9121cc0315ca88dd671f2ba7cf2b196ad6694a3604304df",
    }),
    "aggregate_castellated": frozenset({
        "46414d2eeb091695755cc41eaa9c1d387f7bfe03cf76932c138093596c4749c9",
    }),
}


def text_digest(text: str) -> str:
    """@brief Return the SHA-256 hex digest of a text, the key of the superseded defaults.

    @param text Instruction or prompt text as stored.
    @return Hex digest of its UTF-8 encoding.
    """
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
