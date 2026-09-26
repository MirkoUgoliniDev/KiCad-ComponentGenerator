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
}

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

