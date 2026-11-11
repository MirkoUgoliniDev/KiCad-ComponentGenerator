"""@brief Component types declared before the AI extraction, each with its instruction block.

Knowing in advance *what* is being read completely changes which dimensions
the model must look for in the datasheet: an SMD component has no holes and
its pad sizes are read from the "recommended land pattern", a module with
castellated holes has half-holes on the PCB edge, a THT header has pad and
drill dimensioned directly.

Every type carries an instruction block that the GUI inserts in the notes of
the single component ("Progetto" tab): from there it stays freely editable
before the extraction is launched -- it is a starting point, not a locked
text.

The block is delimited by two markers, so that changing the type replaces
only the block and not what the user wrote on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

BLOCK_START = "--- Tipologia componente (generato, modificabile) ---"
BLOCK_END = "--- fine tipologia componente ---"


@dataclass(frozen=True)
class ComponentType:
    """@brief Selectable component type with the instruction passed to the model."""

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
        "- Terminali su una griglia regolare: layout \"matrix\" con rows, cols e "
        "pad_diameter_mm; pitch_mm solo con piu' di una colonna, row_spacing_mm "
        "solo con piu' di una riga. Se i terminali non stanno su una griglia "
        "regolare, descrivi ogni pad in package_geometry (pad_type "
        "\"thru_hole\").\n"
        "- FORI: trascrivi solo cio' che e' STAMPATO. Se la tabella o il disegno "
        "\"Recommended PCB layout\", \"PCB hole dimensions\" o simili stampa il "
        "foro, riportalo in drill_mm (maggiore di zero e minore della piazzola) "
        "e dichiaralo \"read\" in value_sources; per un pad di package_geometry "
        "ricopia anche il richiamo stampato in hole_callout. Se il foro NON e' "
        "stampato non stimarlo: ometti drill_mm e da' il terminale che entra nel "
        "foro, com'e' quotato -- in lead per ogni pad di package_geometry, in "
        "lead_diameter_mm per la griglia -- dichiarandone la provenienza in "
        "value_sources. Foro e piazzola non stampati li ricava il programma dal "
        "terminale.\n"
        "- PIAZZOLE: se il disegno stampa la piazzola trascrivila e dichiarala "
        "\"read\"; altrimenti la dimensione che scrivi e' una tua stima, da "
        "dichiarare \"estimated\" in value_sources.\n"
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
        "- Per ogni lato leggi dalla foto del bordo contact_style; se il bordo non "
        "si vede o il tipo non si distingue ometti contact_style: lo sceglie "
        "l'utente, non va dedotto. Se quotati o visibili riporta anche "
        "half_hole_diameter_mm (diametro del semi-foro sul bordo) e "
        "through_hole_drill_mm (diametro del foro intero), e dalla tabella "
        "\"Recommended PCB land pattern\" pad_length_mm e pad_width_mm della "
        "piazzola sulla scheda madre. Nel value_sources del lato dichiara per "
        "ciascuno se e' letto, ricavato o stimato dalla foto.\n"
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
#: product examples in a structural type. The texts of commit 8970c7e for
#: single_tht and aggregate_castellated were replaced the same day because
#: they let an estimated drill/pad or hole size go without declaring it as
#: such (value_sources) and did not say that an unobserved contact style is
#: left to the user. The text of single_tht with digest 0941635a... was
#: replaced on 2026-11-11: it still told the model to estimate the hole and
#: the pad from the terminal diameter («stima drill_mm = ...»), while
#: since 2026-11-10 the model transcribes the terminal and the PROGRAM
#: derives a hole that is not printed (datasheet_extractor
#: DEFAULT_SINGLE_SYSTEM_PROMPT, `_resolve_pad_terminals`); the instruction
#: is sent with every analysis and contradicted the system prompt.
SUPERSEDED_INSTRUCTION_DIGESTS: dict[str, frozenset[str]] = {
    "single_tht": frozenset({
        "b4ac3268f0ad952b7e6547d640017fba687cec112b7473c04464eaff04ef15a0",
        "a22b90207ad9a7c73c6cb09623cd0f7b9a1b948eda120ea1985dbc4780328555",
        "dfcb4dd38185961f7d5e72fef151657f8b22090d065917434e3ff1cd66de5155",
        "0941635ab39bec9b9fdf01f3f2286984ce34d7b6623dd476894ae2ea8b2c2518",
    }),
    "single_smd": frozenset({
        "8c8b7b749de39798ae1b1cf71b263b94dc34b3c82f338a828e7bcf6b8c7326b2",
    }),
    "aggregate_tht": frozenset({
        "dab44a80479988f9b9121cc0315ca88dd671f2ba7cf2b196ad6694a3604304df",
    }),
    "aggregate_castellated": frozenset({
        "46414d2eeb091695755cc41eaa9c1d387f7bfe03cf76932c138093596c4749c9",
        "2a307c67e5df3ff3b3150c69de731dc7828b4781949d47391f9b6fe903263de6",
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

    @param key Type key as stored in the catalogue; None or empty means no type.
    @return The matching ComponentType, or None.
    """
    return _BY_KEY.get(key) if key else None


def build_block(component_type: ComponentType) -> str:
    """@brief Return the type instruction wrapped in the block markers, ready for the notes.

    @param component_type Type whose `prompt` is wrapped.
    @return BLOCK_START, the instruction and BLOCK_END on separate lines.
    """
    return f"{BLOCK_START}\n{component_type.prompt}\n{BLOCK_END}"


def strip_block(text: str) -> str:
    """@brief Remove the component type block from the notes, keeping the user text.

    This is what lets the type change without erasing the hand-written notes:
    the previous block is removed and the new one inserted.

    @param text Notes text, possibly holding one marker-delimited block.
    @return The notes without the block, stripped of surrounding blank lines.
    @note A block whose closing marker was deleted by the user loses the rest
        of the block, never the text before it, which is certainly the user's.
    """
    if BLOCK_START not in text:
        return text.strip()

    before, _, rest = text.partition(BLOCK_START)
    _, end_found, after = rest.partition(BLOCK_END)
    # Block without a closing marker (the user may have deleted it): the
    # rest of the block is thrown away, not the preceding text, which is
    # certainly the user's.
    remainder = after if end_found else ""
    return "\n".join(part for part in (before.strip(), remainder.strip()) if part).strip()


def apply_block(text: str, component_type: Optional[ComponentType]) -> str:
    """@brief Return the notes with the block of the chosen type at the top (only removed when None).

    @param text Current notes text (None is treated as empty).
    @param component_type Chosen type, or None (no type declared): then the
        previous block, if any, is only removed.
    @return The notes with the new block first, then the user text.
    """
    cleaned = strip_block(text or "")
    if component_type is None:
        return cleaned
    block = build_block(component_type)
    return f"{block}\n\n{cleaned}".strip() if cleaned else block


#: Outcomes of :func:`refresh_block`.
BLOCK_ABSENT = "absent"
BLOCK_CURRENT = "current"
BLOCK_REFRESHED = "refreshed"
BLOCK_EDITED = "edited"


def block_text(text: str) -> Optional[str]:
    """@brief Return the instruction text inside the generated block of the notes, or None without a block.

    @param text Notes text (None is treated as empty).
    @return What lies between BLOCK_START and BLOCK_END, without the line
        breaks `build_block` puts around it; None when the notes hold no
        block or the block has lost its closing marker (it cannot be told
        apart from the user's text after it).
    """
    before, start, rest = (text or "").partition(BLOCK_START)
    inner, end, _after = rest.partition(BLOCK_END)
    if not start or not end:
        return None
    return inner.strip("\r\n")


def _block_payload(inner: str) -> str:
    """The instruction inside a block as compared and digested: LF line ends, no surrounding blanks.

    A notes editor may hand CRLF back (a project file edited on Windows), and
    `build_block` puts line breaks around the instruction: neither is part of
    what the program wrote.
    """
    return inner.replace("\r\n", "\n").replace("\r", "\n").strip()


def block_digest(text: str) -> Optional[str]:
    """@brief Digest of the instruction inside the generated block of the notes, or None without a block.

    This is what the window stores with a project when the PROGRAM writes the
    block (review V5-1, 2026-11-11), so that a later open can tell «the text
    the program wrote» from «a text the user edited» without comparing with
    the profile database, whose texts may have changed in between.

    @param text Notes text (None is treated as empty).
    @return SHA-256 hex digest of the block's instruction with LF line ends
        and no surrounding blanks; None when `block_text` finds no complete block.
    """
    inner = block_text(text)
    return None if inner is None else text_digest(_block_payload(inner))


def refresh_block(text: str, component_type: Optional[ComponentType],
                  known_texts: Sequence[str] = (),
                  written_digest: Optional[str] = None) -> tuple[str, str]:
    """@brief Bring the generated block of saved notes up to date, unless the user edited it.

    The block is called «generato, modificabile» in the window: the user may
    rewrite it, and such a rewrite is the project's own instruction. Opening a
    project used to replace the whole block with the type's current text
    (`apply_block`), so an edit disappeared without a word (review V3-2,
    2026-11-11). The block is now replaced only when its text is one the
    PROGRAM wrote: the current or a superseded built-in instruction of ANY
    type (a stale block of another type -- castellated instructions left in a
    single THT project -- is generated text too, and is still resynchronised),
    or one of `known_texts` (the instructions the profile database holds, the
    user's chosen defaults). Text outside the markers is kept as
    `apply_block` keeps it.

    @param text Saved notes text.
    @param component_type The project's type as the profile defines it (its
        `prompt` is the current default), or None for no type.
    @param known_texts Further texts the program may have written into a
        block: the instructions of every type of the profile database. Used
        only when `written_digest` is None.
    @param written_digest `block_digest` of the block the program last wrote
        into these notes, as stored with the project; None for a project
        saved before the digest was stored.
    @return (notes, outcome): BLOCK_ABSENT (no complete block, notes
        unchanged), BLOCK_CURRENT (the block already holds the current
        default, notes unchanged), BLOCK_REFRESHED (a generated block,
        replaced as `apply_block` does) or BLOCK_EDITED (a block the user
        edited, notes unchanged).
    @note With `written_digest` the decision is exact: the block is the
        program's text when its digest equals the one stored when the program
        wrote it. Comparing with the CURRENT database texts (the rule for
        older projects) misjudges a project saved before the user changed or
        restored the type's instructions in the profile: its block was
        written by the program, yet matches no text the database holds any
        more, and was kept and reported as edited by hand (review V5-1).
    """
    inner = block_text(text)
    if inner is None:
        return text, BLOCK_ABSENT
    payload = _block_payload(inner)
    if component_type is not None and payload == _block_payload(component_type.prompt):
        return text, BLOCK_CURRENT
    if isinstance(written_digest, str) and written_digest:
        # Stored by the program when it wrote the block: exact provenance.
        if text_digest(payload) == written_digest:
            return apply_block(text, component_type), BLOCK_REFRESHED
        return text, BLOCK_EDITED
    # A project saved before the digest existed: the block is the program's
    # when it holds a text the program can have written -- the current or a
    # superseded built-in instruction of any type, or a database text.
    digests = {digest for digests in SUPERSEDED_INSTRUCTION_DIGESTS.values() for digest in digests}
    known = [ct.prompt for ct in COMPONENT_TYPES] + [t for t in known_texts if isinstance(t, str)]
    # Digests of the texts as they are and normalised: the block's own line
    # breaks and the line-end convention are not part of the instruction.
    digests |= {text_digest(t) for t in known} | {text_digest(_block_payload(t)) for t in known}
    if text_digest(inner) in digests or text_digest(payload) in digests:
        return apply_block(text, component_type), BLOCK_REFRESHED
    return text, BLOCK_EDITED
