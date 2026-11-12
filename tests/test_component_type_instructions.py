"""@brief The component type instructions and the system prompt agree with the extraction schema.

Audit finding 4 (2026-09-26): the «single SMD» instruction told the model to
write drill_mm = 0 while the validation rejected every drill of 0, and the
castellated one said the schema described only a two-row pin matrix although
castellated_sides and package_geometry exist. These tests make such a
contradiction a failure, and check that a database still holding a former
default text is migrated while a text the user edited is kept.
"""
import re
import sqlite3

import pytest

from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.component_types import (
    COMPONENT_TYPES, SUPERSEDED_INSTRUCTION_DIGESTS, text_digest)

#: The former «single SMD» instruction, verbatim: the text the audit found in
#: the distributed database. Its digest must be a known superseded default.
OLD_SMD_INSTRUCTION = (
    'TIPOLOGIA: componente SINGOLO SMD (montaggio superficiale). I '
    'terminali appoggiano sulla superficie del PCB: NON esiste alcun foro.\n'
    '- Prendi le dimensioni dei pad dalla tabella "Recommended Land '
    'Pattern" / "Recommended Solder Pad Layout" / "Footprint", NON dalle '
    'quote del corpo del componente: sono valori diversi (il land pattern '
    "e' piu' grande dei terminali).\n"
    "- pitch_mm e' il passo tra i centri dei pad adiacenti sullo stesso "
    "lato; row_spacing_mm e' la distanza tra i centri dei pad delle due "
    'file opposte (quota spesso indicata come "span" tra i centri, non tra '
    'i bordi).\n'
    '- Lo schema di questo strumento descrive pad THT: imposta drill_mm = 0'
    ' (nessun foro) e usa pad_diameter_mm per la dimensione MAGGIORE del '
    'pad, riportando in notes le quote esatte del land pattern (larghezza x'
    " lunghezza di ogni pad) e segnalando che il componente e' SMD.\n"
    '- confidence al massimo "medium" se il datasheet non riporta un land '
    'pattern raccomandato.'
)

#: Statements that contradict the current schema or the project rules, with
#: the reason. A new instruction containing one of them is wrong by
#: construction, whatever else it says.
FORBIDDEN = {
    "drill_mm = 0": "a pin-grid pad without drill is rejected: SMD pads go in package_geometry",
    "descrive pad THT": "the schema also describes explicit SMD pads (package_geometry)",
    "solo una matrice": "castellated_sides and package_geometry describe non-matrix contacts",
    "due file": "the matrix has any number of rows and castellated modules up to four sides",
    "pin_map_view": "replaced by contacts_view, which covers every contact geometry",
    "ESP32": "a type is a structural class: no product examples",
    "nRF": "a type is a structural class: no product examples",
    # CONTRACT CHANGED 2026-11-12: prompts in English. The Italian fragments
    # above still catch the former texts (negative cases below); these are
    # the same contradictions as an English instruction would state them
    # («due file» as «on two rows», narrower than a bare «two rows», which an
    # English system prompt may legitimately use for a two-row header).
    "describes THT pads": "the schema also describes explicit SMD pads (package_geometry)",
    "only a matrix": "castellated_sides and package_geometry describe non-matrix contacts",
    "on two rows": "the matrix has any number of rows and castellated modules up to four sides",
}

#: The «single SMD» instruction as it was from 2026-09-26 to 2026-11-12, in
#: Italian, verbatim. Replaced by its English translation (every prompt in
#: English, user decision): a database still holding it is migrated.
ITALIAN_SMD_INSTRUCTION = (
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
)

#: The block markers written until 2026-11-12, verbatim, as they sit in
#: saved notes and project files.
ITALIAN_START = "--- Tipologia componente (generato, modificabile) ---"
ITALIAN_END = "--- fine tipologia componente ---"

#: snake_case words that are not schema identifiers (plain words of the text).
_NOT_FIELDS = {"body_"}


def _schema_words():
    """Every property name and every string enum value in the tool schema."""
    names = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(value)
                if key == "enum" and isinstance(value, list):
                    names.update(v for v in value if isinstance(v, str))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ext._EXTRACTION_TOOL["input_schema"])
    return names


def _violations(text):
    """What makes one instruction text contradict the schema; empty when coherent."""
    found = [f"{fragment!r}: {reason}" for fragment, reason in FORBIDDEN.items()
             if fragment in text]
    known = _schema_words()
    for word in set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text)) - _NOT_FIELDS:
        if word not in known:
            found.append(f"{word!r} is not a field or value of the tool schema")
    return found


@pytest.mark.parametrize("component_type", COMPONENT_TYPES, ids=lambda t: t.key)
def test_no_type_instruction_contradicts_the_schema(component_type):
    assert _violations(component_type.prompt) == []


def test_the_system_prompt_does_not_contradict_the_schema():
    assert _violations(ext.DEFAULT_SYSTEM_PROMPT) == []


def test_the_checker_rejects_the_old_smd_instruction():
    """Negative case: the text the audit found is caught, for the right reasons."""
    problems = "\n".join(_violations(OLD_SMD_INSTRUCTION))
    assert "drill_mm = 0" in problems and "descrive pad THT" in problems


def test_the_checker_rejects_the_same_contradictions_in_english():
    """CONTRACT CHANGED 2026-11-12: prompts in English; the checker must catch an English contradiction too."""
    english = ("The schema of this tool describes THT pads: set drill_mm = 0. "
               "It describes only a matrix of pins on two rows.")
    problems = "\n".join(_violations(english))
    for fragment in ("drill_mm = 0", "describes THT pads", "only a matrix", "on two rows"):
        assert fragment in problems
    # Negative: the current English instructions state none of them.
    for component_type in COMPONENT_TYPES:
        assert not any(f in component_type.prompt for f in FORBIDDEN), component_type.key


def test_the_type_instructions_are_written_in_english():
    """CONTRACT CHANGED 2026-11-12: prompts in English. No Italian marker word is left in a type instruction."""
    for component_type in COMPONENT_TYPES:
        text = component_type.prompt
        assert text.startswith("TYPE: "), component_type.key
        for italian in ("TIPOLOGIA", " e' ", " della ", " delle ", " non ", "Dichiara"):
            assert italian not in text, (component_type.key, italian)
    # Negative: the Italian text these replaced is caught by the same check.
    assert ITALIAN_SMD_INSTRUCTION.startswith("TIPOLOGIA") and " e' " in ITALIAN_SMD_INSTRUCTION


def test_the_checker_rejects_a_field_the_schema_does_not_have():
    assert any("hole_size_mm" in p for p in _violations("compila hole_size_mm"))


def test_each_type_names_the_fields_its_structure_needs():
    """The instruction points the model to the part of the schema that describes it."""
    by_key = {t.key: t.prompt for t in COMPONENT_TYPES}
    assert "package_geometry" in by_key["single_smd"] and '"smd"' in by_key["single_smd"]
    assert "drill_mm" in by_key["single_tht"] and "package_geometry" in by_key["single_tht"]
    assert "pin_map" in by_key["aggregate_tht"] and "populated_pins" in by_key["aggregate_tht"]
    for field in ("castellated_sides", "half_hole_diameter_mm", "through_hole_drill_mm"):
        assert field in by_key["aggregate_castellated"]
    for key, text in by_key.items():
        assert "drawing_unit" in text, key
        assert "contacts_view" in text or "source_view" in text, key


def test_the_schema_has_unit_view_and_hole_fields():
    schema = ext._EXTRACTION_TOOL["input_schema"]
    properties = schema["properties"]
    assert properties["drawing_unit"]["enum"] == ["mm", "inch", "mil"]
    assert properties["contacts_view"]["enum"] == ["top", "bottom", "unknown"]
    assert {"drawing_unit", "contacts_view"} <= set(schema["required"])
    assert "pin_map_view" not in properties
    side = properties["castellated_sides"]["items"]["properties"]
    for key in ("half_hole_diameter_mm", "through_hole_drill_mm"):
        assert side[key]["type"] == "number" and side[key]["exclusiveMinimum"] == 0
    assert "SMD" in properties["drill_mm"]["description"]


# ── migration of the stored defaults ─────────────────────────────────────────

def test_the_old_instruction_is_a_known_superseded_default():
    assert text_digest(OLD_SMD_INSTRUCTION) in SUPERSEDED_INSTRUCTION_DIGESTS["single_smd"]
    assert text_digest(ITALIAN_SMD_INSTRUCTION) in SUPERSEDED_INSTRUCTION_DIGESTS["single_smd"]


def test_a_stored_italian_instruction_becomes_the_english_one_and_an_edited_copy_is_kept(tmp_path):
    """Positive: the Italian default in a user's database is migrated. Negative: one added line keeps it."""
    _store_with(tmp_path / "it.sqlite3", "single_smd", ITALIAN_SMD_INSTRUCTION)
    assert (ProfileStore(tmp_path / "it.sqlite3").get_type("single_smd")["instructions"]
            == component_types.get("single_smd").prompt)
    edited = ITALIAN_SMD_INSTRUCTION + "\n- Pad in pollici."
    _store_with(tmp_path / "edited.sqlite3", "single_smd", edited)
    assert ProfileStore(tmp_path / "edited.sqlite3").get_type("single_smd")["instructions"] == edited


# ── block markers: the Italian pair written until 2026-11-12 ─────────────────

def _italian_block(instruction, user_text="Nota mia: case metallico."):
    """Notes as an earlier version saved them: Italian markers, then the user's text."""
    return f"{ITALIAN_START}\n{instruction}\n{ITALIAN_END}\n\n{user_text}"


def test_the_markers_are_english_and_the_italian_pair_is_still_known():
    assert component_types.BLOCK_START == "--- Component type (generated, editable) ---"
    assert component_types.BLOCK_END == "--- end of component type ---"
    assert (ITALIAN_START, ITALIAN_END) in component_types.LEGACY_BLOCK_MARKERS
    assert component_types.BLOCK_MARKERS[0] == (component_types.BLOCK_START, component_types.BLOCK_END)


def test_a_block_with_italian_markers_is_found_stripped_and_rewritten_with_english_ones():
    """Positive: the old block is recognised, its text read, and every rewrite uses the new markers."""
    notes = _italian_block(ITALIAN_SMD_INSTRUCTION)
    assert component_types.has_block(notes)
    assert component_types.block_text(notes) == ITALIAN_SMD_INSTRUCTION
    assert component_types.strip_block(notes) == "Nota mia: case metallico."
    smd = component_types.get("single_smd")
    applied = component_types.apply_block(notes, smd)
    assert applied == component_types.build_block(smd) + "\n\nNota mia: case metallico."
    assert ITALIAN_START not in applied and ITALIAN_END not in applied
    # A project saved before the digest was stored: the Italian default is
    # the program's own text, so the block is brought up to date.
    refreshed, outcome = component_types.refresh_block(notes, smd)
    assert outcome == component_types.BLOCK_REFRESHED
    assert refreshed == applied
    # The same with the digest the program stored when it wrote the block.
    written = component_types.block_digest(notes)
    assert written == text_digest(ITALIAN_SMD_INSTRUCTION)
    assert component_types.refresh_block(notes, smd, written_digest=written) == (
        applied, component_types.BLOCK_REFRESHED)


def test_an_edited_block_with_italian_markers_and_text_without_markers_are_left_alone():
    """Negative: a user-edited old block is kept as it is; words alone are not a block."""
    edited = _italian_block(ITALIAN_SMD_INSTRUCTION + "\n- Pad in pollici.")
    smd = component_types.get("single_smd")
    assert component_types.refresh_block(edited, smd) == (edited, component_types.BLOCK_EDITED)
    plain = "Tipologia componente scelta a mano; fine tipologia componente."
    assert not component_types.has_block(plain)
    assert component_types.block_text(plain) is None
    assert component_types.strip_block(plain) == plain
    # An Italian start closed only by the English end is not a complete
    # block: each start marker is closed by its own end marker.
    mixed = f"{ITALIAN_START}\ntesto\n{component_types.BLOCK_END}\nNota."
    assert component_types.has_block(mixed)
    assert component_types.block_text(mixed) is None


def test_current_texts_are_not_listed_as_superseded():
    """A current default listed as superseded would be a copy-paste error in the digests."""
    for component_type in COMPONENT_TYPES:
        assert text_digest(component_type.prompt) not in SUPERSEDED_INSTRUCTION_DIGESTS[component_type.key]
    assert text_digest(ext.DEFAULT_SYSTEM_PROMPT) not in ext.SUPERSEDED_SYSTEM_PROMPT_DIGESTS


def _store_with(path, key, instructions, builtin=1):
    ProfileStore(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE component_types SET instructions=?, builtin=? WHERE key=?",
                   (instructions, builtin, key))
    db.close()


def test_a_stored_former_default_is_replaced_on_open(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    _store_with(path, "single_smd", OLD_SMD_INSTRUCTION)
    store = ProfileStore(path)
    assert store.get_type("single_smd")["instructions"] == component_types.get("single_smd").prompt


def test_an_edited_instruction_is_kept(tmp_path):
    """Negative case: one character of difference is a user edit, never overwritten."""
    path = tmp_path / "profiles.sqlite3"
    edited = OLD_SMD_INSTRUCTION + "\n- Il mio fornitore quota i pad in pollici."
    _store_with(path, "single_smd", edited)
    assert ProfileStore(path).get_type("single_smd")["instructions"] == edited


def test_a_custom_copy_of_a_former_default_is_kept(tmp_path):
    """A non built-in type holding the old text was created by the user: left alone."""
    path = tmp_path / "profiles.sqlite3"
    store = ProfileStore(path)
    key = store.save_type(None, label="Copia", family="single_smd", instructions=OLD_SMD_INSTRUCTION)
    assert ProfileStore(path).get_type(key)["instructions"] == OLD_SMD_INSTRUCTION


def test_a_former_system_prompt_is_replaced_and_an_edited_one_kept(tmp_path, monkeypatch):
    old = "prompt di sistema di una versione precedente"
    monkeypatch.setattr(ext, "SUPERSEDED_SYSTEM_PROMPT_DIGESTS", frozenset({text_digest(old)}))
    for stored, expected in ((old, ext.DEFAULT_SYSTEM_PROMPT), (old + ".", old + ".")):
        path = tmp_path / f"{len(stored)}.sqlite3"
        ProfileStore(path)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE ai_defaults SET value=? WHERE name='system_prompt'", (stored,))
        db.close()
        assert ProfileStore(path).defaults()["system_prompt"] == expected

