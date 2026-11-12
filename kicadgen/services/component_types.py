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

#: Markers written around the generated block. The block is sent to the model
#: with the notes, so since 2026-11-12 (all prompts in English, user decision)
#: the markers are English too.
BLOCK_START = "--- Component type (generated, editable) ---"
BLOCK_END = "--- end of component type ---"

#: Marker pairs written by earlier versions, still recognised in saved notes
#: and projects (project_config.json `field_component_prompt`): a block
#: between them is found, stripped and replaced exactly like a current one,
#: and every rewrite uses the current markers. The Italian pair was written
#: until 2026-11-12.
LEGACY_BLOCK_MARKERS: tuple[tuple[str, str], ...] = (
    ("--- Tipologia componente (generato, modificabile) ---", "--- fine tipologia componente ---"),
)

#: Every (start, end) marker pair a block may carry, the current one first.
#: Code that looks for a block on its own (a regular expression, an `in`
#: test) must try them all, or use :func:`has_block`.
BLOCK_MARKERS: tuple[tuple[str, str], ...] = ((BLOCK_START, BLOCK_END),) + LEGACY_BLOCK_MARKERS


def _locate_block(text: str) -> Optional[tuple[str, str, str]]:
    """Split `text` at the first block start marker of any known pair.

    @return (before, rest, end_marker): the text before the start marker,
        the text after it, and the end marker paired with the start marker
        found; None when no start marker occurs. The earliest occurrence
        wins, so a note holding both an old and a new block is handled one
        block at a time, as before with a single pair.
    """
    best = None
    for start, end in BLOCK_MARKERS:
        index = text.find(start)
        if index >= 0 and (best is None or index < best[0]):
            best = (index, start, end)
    if best is None:
        return None
    index, start, end = best
    return text[:index], text[index + len(start):], end


def has_block(text: Optional[str]) -> bool:
    """@brief Tell whether the notes hold the start marker of a generated block, current or legacy.

    @param text Notes text (None is treated as empty).
    @return True when any marker pair of BLOCK_MARKERS opens a block.
    """
    return _locate_block(text or "") is not None


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
        "TYPE: SINGLE THROUGH-HOLE (THT) component. It is a single discrete "
        "component or connector, with terminals that pass through the PCB "
        "into plated holes.\n"
        "- Terminals on a regular grid: \"matrix\" layout with rows, cols and "
        "pad_diameter_mm; pitch_mm only with more than one column, row_spacing_mm "
        "only with more than one row. If the terminals do not lie on a regular "
        "grid, describe each pad in package_geometry (pad_type "
        "\"thru_hole\").\n"
        "- HOLES: transcribe only what is PRINTED. If the table or the drawing "
        "\"Recommended PCB layout\", \"PCB hole dimensions\" or similar prints the "
        "hole, report it in drill_mm (greater than zero and smaller than the pad) "
        "and declare it \"read\" in value_sources; for a pad of package_geometry "
        "also copy the printed callout into hole_callout. If the hole is NOT "
        "printed do not estimate it: omit drill_mm and give the terminal that goes "
        "into the hole, as dimensioned -- in lead for each pad of package_geometry, "
        "in lead_diameter_mm for the grid -- declaring its provenance in "
        "value_sources. A hole and a pad that are not printed are derived by the "
        "program from the terminal.\n"
        "- PADS: if the drawing prints the pad, transcribe it and declare it "
        "\"read\"; otherwise the size you write is your own estimate, to be "
        "declared \"estimated\" in value_sources.\n"
        "- pitch_mm and row_spacing_mm are distinct dimensions and are read from "
        "the top or bottom view of the dimensioned mechanical drawing, not from the "
        "side view: declare in contacts_view which view you read (the "
        "mirroring is done by the program) and in drawing_unit the unit of the "
        "dimensions.\n"
        "- The body dimensions (body_*) are the outline of the real component, "
        "not that of the pads."
    ),
)

_SINGLE_SMD = ComponentType(
    key="single_smd",
    label="Componente singolo SMD",
    prompt=(
        "TYPE: SINGLE SMD component (surface mount). The terminals "
        "rest on the surface of the PCB: no pad has a hole.\n"
        "- Describe the pads in package_geometry, which for an SMD component is "
        "the source of the footprint: one element per pin with number, centre "
        "(x_mm, y_mm), pad size (pad_length_mm, pad_width_mm), pad_type "
        "\"smd\" and, if the pad is rotated, rotation_deg; family according to "
        "the package, or \"custom\". An exposed or thermal pad is a pin of its "
        "own, with the number the datasheet gives it.\n"
        "- The pad sizes come from the \"Recommended Land "
        "Pattern\" / \"Recommended Solder Pad Layout\" / \"Footprint\" table, NOT "
        "from the dimensions of the body or of the terminals: the land pattern is "
        "normally larger than the terminals.\n"
        "- State in source_view of package_geometry the view from which you read "
        "the coordinates (the mirroring of the bottom view is done by the program) "
        "and in drawing_unit the unit of the dimensions.\n"
        "- Do not fill in drill_mm: an SMD pad has no hole. rows, cols, pitch_mm "
        "and row_spacing_mm are optional and informative only.\n"
        "- confidence at most \"medium\" if the datasheet does not give a "
        "recommended land pattern."
    ),
)

_AGGREGATE_THT = ComponentType(
    key="aggregate_tht",
    label="Scheda con componenti · connettori passanti",
    prompt=(
        "TYPE: BOARD WITH THROUGH-HOLE COMPONENTS -- a module or a small "
        "assembled board that is mounted as one single component through "
        "through-hole pins.\n"
        "- The body is the outline of the whole module (case or PCB of the module), "
        "not that of a single component mounted on it.\n"
        "- Pins on a regular grid: \"matrix\" layout. If the numbering does not "
        "follow the column-by-column order (for example U-shaped: 1..N along one "
        "row, then backwards along the other), fill in pin_map with the positions "
        "as they appear in the drawing.\n"
        "- Declare in contacts_view from which side the pin drawing is seen "
        "(module pinouts are often in BOTTOM VIEW): the mirroring is done by the "
        "program, even when the numbering follows the column order and "
        "pin_map is omitted. body_offset_x_mm and body_offset_y_mm are read on the "
        "same drawing.\n"
        "- row_spacing_mm (distance between adjacent rows) and pitch_mm (pitch along "
        "the row) are distinct dimensions: read them separately. With a single row "
        "row_spacing_mm is omitted.\n"
        "- If the matrix is not fully populated, list in populated_pins "
        "only the positions with a physical pin.\n"
        "- Declare in drawing_unit the unit of the drawing's dimensions."
    ),
)

_AGGREGATE_CASTELLATED = ComponentType(
    key="aggregate_castellated",
    label="Scheda con componenti · connettori castellated (mezzo foro)",
    prompt=(
        "TYPE: BOARD WITH COMPONENTS with HALF-HOLE PADS (castellated "
        "holes) -- a module on a PCB whose contacts are platings on the edge "
        "of the small board, soldered like SMD onto the motherboard.\n"
        "- \"castellated\" layout: describe the contacts in castellated_sides, one "
        "element for each side that carries them (up to four), each with its "
        "own count, its own pitch_mm and, if the group is not centred, offset_mm; "
        "different sides can have different numbers of contacts and pitches.\n"
        "- The physical numbering of the pads (first_pin, reverse) is one single "
        "numbering across all sides; the silkscreen labels go in pin_names, not in "
        "the numbers.\n"
        "- For each side read contact_style from the photo of the edge; if the edge "
        "is not visible or the type cannot be told apart, omit contact_style: the "
        "user chooses it, it must not be deduced. If dimensioned or visible, also "
        "report half_hole_diameter_mm (diameter of the half-hole on the edge) and "
        "through_hole_drill_mm (diameter of the full hole), and from the "
        "\"Recommended PCB land pattern\" table pad_length_mm and pad_width_mm of "
        "the pad on the motherboard. In the side's value_sources declare for "
        "each one whether it is read, derived or estimated from the photo.\n"
        "- Declare in contacts_view from which side the drawing or the photo you "
        "read the sides from is seen: the mirroring is done by the program.\n"
        "- Contacts that are not on the edge (for example a thermal pad or an "
        "inner row) do not go in castellated_sides: report them in notes.\n"
        "- The body is the whole module: body_height_mm includes any metal "
        "shield, pcb_thickness_mm is the thickness of the PCB alone.\n"
        "- Declare in drawing_unit the unit of the drawing's dimensions."
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
#: The Italian texts of all four types (the last digest of each set:
#: 2c530c27..., 44ab2ff7..., b1feb1a3..., f12d82a1...) were replaced on
#: 2026-11-12 by their faithful English translation: every prompt sent to a
#: model is in English (user decision), the rules themselves are unchanged.
SUPERSEDED_INSTRUCTION_DIGESTS: dict[str, frozenset[str]] = {
    "single_tht": frozenset({
        "b4ac3268f0ad952b7e6547d640017fba687cec112b7473c04464eaff04ef15a0",
        "a22b90207ad9a7c73c6cb09623cd0f7b9a1b948eda120ea1985dbc4780328555",
        "dfcb4dd38185961f7d5e72fef151657f8b22090d065917434e3ff1cd66de5155",
        "0941635ab39bec9b9fdf01f3f2286984ce34d7b6623dd476894ae2ea8b2c2518",
        "2c530c27fd10541e97f9d6ff0754d4451f5f7a54567491b0e17bc3e5c4e24c28",
    }),
    "single_smd": frozenset({
        "8c8b7b749de39798ae1b1cf71b263b94dc34b3c82f338a828e7bcf6b8c7326b2",
        "44ab2ff72c479a4ee66bf6f24c42e537fa29b14ba5d3e1c1c676010c35afa5ac",
    }),
    "aggregate_tht": frozenset({
        "dab44a80479988f9b9121cc0315ca88dd671f2ba7cf2b196ad6694a3604304df",
        "b1feb1a399e1631eef001b5e43176ab6a654dd6b3ad93c650a8de46b08810591",
    }),
    "aggregate_castellated": frozenset({
        "46414d2eeb091695755cc41eaa9c1d387f7bfe03cf76932c138093596c4749c9",
        "2a307c67e5df3ff3b3150c69de731dc7828b4781949d47391f9b6fe903263de6",
        "f12d82a1b9f68517a7e0ab67be3d14bed2e948fbd335a0273ade6b1edce173fb",
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
    @note A block between legacy markers (LEGACY_BLOCK_MARKERS) is removed
        the same way: its start marker is closed by its own end marker.
    """
    located = _locate_block(text)
    if located is None:
        return text.strip()

    before, rest, end_marker = located
    _, end_found, after = rest.partition(end_marker)
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
    @return What lies between BLOCK_START and BLOCK_END (or a legacy pair of
        LEGACY_BLOCK_MARKERS), without the line breaks `build_block` puts
        around it; None when the notes hold no block or the block has lost
        its closing marker (it cannot be told apart from the user's text
        after it).
    """
    located = _locate_block(text or "")
    if located is None:
        return None
    _before, rest, end_marker = located
    inner, end, _after = rest.partition(end_marker)
    if not end:
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
