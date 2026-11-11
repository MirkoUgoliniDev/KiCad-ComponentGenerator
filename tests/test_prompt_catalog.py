"""@brief Contract of the prompt registry (kicadgen/services/prompt_catalog.py, 2026-11-11).

The registry is the one place the profile store, the dialog and the log read
the prompts from: every prompt constant must be in it, every former-default
digest set must be reachable through it, and the status must tell a current
default, a known former default and a user text apart for a structural reason
(equal text, listed digest, anything else), not by coincidence.
"""
import importlib
from pathlib import Path
import re

import pytest

from kicadgen.services import prompt_catalog as pc
from kicadgen.services.analysis_profiles import MODEL_FIELDS, PROFILE_FIELDS
from kicadgen.services.component_types import COMPONENT_TYPES, text_digest

SERVICES = Path(__file__).resolve().parents[1] / 'kicadgen' / 'services'
#: Public module-level prompt constants: «NAME = (», «NAME = """» or an alias.
_PROMPT_CONSTANT = re.compile(r'^([A-Z][A-Z0-9_]*PROMPT[A-Z0-9_]*)\s*=', re.MULTILINE)
_DIGEST_SET = re.compile(r'^(SUPERSEDED_[A-Z0-9_]*DIGESTS)\b', re.MULTILINE)


def _module_constants(pattern):
    """(module, NAME) of every match of `pattern` at the top level of the service modules."""
    found = []
    for path in sorted(SERVICES.glob('*.py')):
        for name in pattern.findall(path.read_text(encoding='utf-8')):
            found.append((path.stem, name))
    return found


def test_no_field_is_registered_twice():
    fields = pc.fields()
    assert len(fields) == len(set(fields))


def test_every_profile_prompt_field_has_an_entry_and_every_entry_is_a_profile_field():
    """Positive and negative together: model fields are not prompts, every other field is."""
    prompt_fields = {field for field in PROFILE_FIELDS if field not in MODEL_FIELDS}
    assert prompt_fields == set(pc.fields())
    assert not set(pc.fields()) & set(MODEL_FIELDS)


def test_every_prompt_constant_of_the_services_is_the_default_of_an_entry():
    """A new prompt constant that nobody registered fails here, wherever it lives.

    CONTRACT CHANGED 2026-11-11 (step P2): a constant may also be the text of a
    CODE_ONLY_PROMPTS entry -- a text kept in the code on purpose, shown
    read-only with its reason -- because the two legacy-path prompts lifted out
    of their functions are registered there, not as profile fields.
    """
    defaults = {entry.current_default() for entry in (*pc.PROMPTS, *pc.CODE_ONLY_PROMPTS)}
    constants = [(module, name) for module, name in _module_constants(_PROMPT_CONSTANT)
                 if 'VERSION' not in name and module != 'prompt_catalog'
                 and not name.endswith('_FIELDS')]
    assert ('datasheet_extractor', 'DEFAULT_PAD_POSITIONS_PROMPT') in constants  # the scan sees them
    missing = []
    for module, name in constants:
        value = getattr(importlib.import_module(f'kicadgen.services.{module}'), name)
        if isinstance(value, str) and value not in defaults:
            missing.append(f'{module}.{name}')
    assert not missing, missing


def test_every_role_instruction_is_registered_once_per_role():
    from kicadgen.services.datasheet_extractor import DOCUMENT_ROLE_KEYS, _ROLE_PROMPTS
    assert set(_ROLE_PROMPTS) == set(DOCUMENT_ROLE_KEYS)
    for role, (_name, instruction) in _ROLE_PROMPTS.items():
        assert pc.restore_default(f'role_prompt_{role}') == instruction


def test_every_superseded_digest_set_is_reachable_through_the_registry():
    sources = {entry.superseded_source for entry in pc.PROMPTS if entry.superseded_source}
    sources |= {pc.instruction_entry(t.key).superseded_source for t in COMPONENT_TYPES}
    scanned = {f'{module}.{name}' for module, name in _module_constants(_DIGEST_SET)}
    assert 'datasheet_extractor.SUPERSEDED_SINGLE_PROMPT_DIGESTS' in scanned
    # CONTRACT CHANGED (round 7, review V4-4): the registry's own table,
    # prompt_catalog.SUPERSEDED_DIGESTS, holds one set per profile prompt and
    # is read by every entry (merged with the owning module's set): it is
    # reachable through all of them, not through one superseded_source.
    assert 'prompt_catalog.SUPERSEDED_DIGESTS' in scanned
    assert set(pc.fields()) <= set(pc.SUPERSEDED_DIGESTS)
    scanned.discard('prompt_catalog.SUPERSEDED_DIGESTS')
    assert scanned <= sources, scanned - sources
    for entry in pc.PROMPTS:
        if entry.superseded_source:
            module, name = entry.superseded_source.split('.')
            listed = getattr(importlib.import_module(f'kicadgen.services.{module}'), name)
            assert entry.superseded() == frozenset(listed)


def test_every_default_is_reachable_and_only_the_photo_notes_are_blank():
    for entry in pc.PROMPTS:
        text = entry.current_default()
        assert isinstance(text, str)
        assert bool(text.strip()) == (entry.field != 'image_instructions'), entry.field


def test_no_current_default_is_listed_among_its_former_defaults():
    for entry in [*pc.PROMPTS, *(pc.instruction_entry(t.key) for t in COMPONENT_TYPES)]:
        assert text_digest(entry.current_default()) not in entry.superseded(), entry.source


def test_every_profile_prompt_is_wired_and_only_the_code_texts_are_not():
    """CONTRACT CHANGED 2026-11-11 (step P2 wired the 13 schema-8 fields).

    Before: in_profile False exactly for the prompts the analysis still took
    from the code. Now every PROMPTS entry is sent from the profile, and
    in_profile False marks only CODE_ONLY_PROMPTS, each with a reason and with
    no profile field (it can never be saved or edited).
    """
    assert all(entry.in_profile for entry in pc.PROMPTS)
    assert pc.CODE_ONLY_PROMPTS
    for entry in pc.CODE_ONLY_PROMPTS:
        assert not entry.in_profile and entry.reason.strip(), entry.field
        assert entry.field not in PROFILE_FIELDS and entry.field.startswith('code:')


# ── status: current / outdated / custom, each with its negative twin ──

def test_the_current_default_is_current_and_one_character_more_is_custom():
    text = pc.restore_default('system_prompt')
    assert pc.prompt_status('system_prompt', text) == pc.CURRENT
    assert pc.prompt_status('system_prompt', text + '.') == pc.CUSTOM


def test_surrounding_blanks_do_not_count_but_inner_ones_do():
    text = pc.restore_default('inventory_prompt')
    assert text != text.strip()                     # this default has a trailing newline
    assert pc.prompt_status('inventory_prompt', text.strip()) == pc.CURRENT
    assert pc.prompt_status('inventory_prompt', '  ' + text + '\n') == pc.CURRENT
    inner = text.replace(' ', '  ', 1)
    assert pc.prompt_status('inventory_prompt', inner) == pc.CUSTOM


def test_a_listed_former_default_is_outdated_and_an_edited_one_is_custom(monkeypatch):
    from kicadgen.services import photo_crop_reading
    old = 'crop reading instructions of a former release'
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    assert pc.prompt_status('crop_prompt', old) == pc.OUTDATED
    assert pc.prompt_status('crop_prompt', old + ' ') == pc.CUSTOM   # digest is exact
    assert pc.prompt_status('crop_prompt', old.upper()) == pc.CUSTOM


def test_the_current_default_wins_over_a_digest_listed_by_mistake(monkeypatch):
    from kicadgen.services import photo_crop_reading
    current = photo_crop_reading.SYSTEM_PROMPT
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(current)}))
    assert pc.prompt_status('crop_prompt', current) == pc.CURRENT


def test_a_prompt_without_known_former_defaults_is_never_outdated():
    assert pc.entry('lead_prompt').superseded() == frozenset()
    assert pc.prompt_status('lead_prompt', 'anything else') == pc.CUSTOM


def test_a_blank_text_is_custom_unless_the_default_is_blank():
    """Explicit decision: blank is never a non-blank prompt's default nor a former one."""
    assert pc.prompt_status('system_prompt', '') == pc.CUSTOM
    assert pc.prompt_status('system_prompt', '  \n') == pc.CUSTOM
    assert pc.prompt_status('image_instructions', '') == pc.CURRENT
    assert pc.prompt_status('image_instructions', 'only SMD parts') == pc.CUSTOM


def test_a_non_string_text_and_an_unknown_field_are_refused():
    with pytest.raises(TypeError):
        pc.prompt_status('system_prompt', None)
    with pytest.raises(KeyError):
        pc.prompt_status('model', 'x')
    with pytest.raises(KeyError):
        pc.restore_default('not_a_prompt')


def test_built_in_type_instructions_have_a_status_and_custom_types_have_none(monkeypatch):
    from kicadgen.services import component_types
    builtin = COMPONENT_TYPES[0]
    entry = pc.instruction_entry(builtin.key)
    assert pc.entry_status(entry, builtin.prompt) == pc.CURRENT
    assert pc.entry_status(entry, builtin.prompt + ' mine') == pc.CUSTOM
    old = 'former instruction'
    monkeypatch.setattr(component_types, 'SUPERSEDED_INSTRUCTION_DIGESTS',
                        {builtin.key: frozenset({text_digest(old)})})
    assert pc.entry_status(entry, old) == pc.OUTDATED
    assert pc.instruction_entry('custom_0123') is None


# ── diff and restore ──

def test_the_diff_is_empty_for_the_current_default_and_shows_the_changed_sentence():
    text = pc.restore_default('numbering_prompt')
    assert pc.prompt_diff('numbering_prompt', text) == []
    edited = text.replace('.', '. Frase aggiunta dall\'utente.', 1)
    lines = pc.prompt_diff('numbering_prompt', edited)
    assert lines[0].startswith('---') and lines[1].startswith('+++')
    assert any(line.startswith('-') and 'Frase aggiunta' in line for line in lines[2:])
    assert not any(line.startswith('+') and 'Frase aggiunta' in line for line in lines[2:])
    # Split by sentence: no diff line carries the whole prompt.
    assert max(len(line) for line in lines) < len(text)


def test_the_diff_is_bounded():
    full = pc.prompt_diff('system_prompt', 'x\n' * 2000, max_lines=10**6)
    lines = pc.prompt_diff('system_prompt', 'x\n' * 2000, max_lines=50)
    assert len(full) > 51 and len(lines) == 51
    assert lines[:50] == full[:50]
    assert lines[-1].startswith('…') and str(len(full) - 50) in lines[-1]


def test_restore_default_is_the_owning_module_text_byte_for_byte():
    from kicadgen.services import photo_grounding, datasheet_extractor
    assert pc.restore_default('mechanical_evidence_prompt') is photo_grounding.MECHANICAL_EVIDENCE_PROMPT
    assert pc.restore_default('system_prompt_single') == datasheet_extractor.DEFAULT_SINGLE_SYSTEM_PROMPT


def test_the_registry_reads_a_patched_default_at_once(monkeypatch):
    """Lazy reading: the owning module stays the source of the text."""
    from kicadgen.services import photo_leads
    monkeypatch.setattr(photo_leads, 'PROMPT', 'patched lead prompt')
    assert pc.restore_default('lead_prompt') == 'patched lead prompt'
    assert pc.prompt_status('lead_prompt', 'patched lead prompt') == pc.CURRENT


def test_every_status_has_a_label_and_every_title_a_four_language_entry():
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    sources = [pc.status_label(s) for s in (pc.CURRENT, pc.CUSTOM, pc.OUTDATED)]
    sources += [entry.title for entry in pc.PROMPTS] + [pc.INSTRUCTION_TITLE]
    # Step P2: the code-only texts' titles and reasons, and the sub-tab groups.
    sources += [entry.title for entry in pc.CODE_ONLY_PROMPTS]
    sources += [entry.reason for entry in pc.CODE_ONLY_PROMPTS]
    sources += [pc.GROUP_ROLES, pc.GROUP_INVENTORY]
    for source in sources:
        entry = DEFAULT_TRANSLATIONS['ui:' + source]
        assert set(entry) == {'it', 'en', 'fr', 'de'} and entry['it'] == source


def test_no_inline_prompt_is_left_outside_the_registry():
    """CONTRACT CHANGED 2026-11-11 (step P2 closed the gap P1 recorded).

    Before: the five inline prompts were listed here until P2 lifted them.
    Now the list is empty; tests/test_prompt_wiring.py scans the service
    functions for a long literal system prompt built inside a function.
    """
    assert pc.UNREGISTERED_INLINE_PROMPTS == ()
