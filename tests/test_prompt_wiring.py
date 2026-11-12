"""@brief Step P2 of the 2026-11-11 plan: every prompt the analysis sends comes from the profile.

User request (2026-11-11 evening): «dobbiamo evidenziare questi prompt
nell'interfaccia, non possiamo lasciarli embeddati nel codice» = the prompts
must be shown in the interface, not left embedded in the code, and their
status must be visible. Each test below pairs a positive and a negative case
that differ for a structural reason: an edited profile text reaches the fake
call while a blank one sends the default; an edited inventory prompt misses
the cache the default hit; the start-up notice names custom and outdated
texts but not current or dismissed ones; the log line has the status and the
digest but never the text.
"""
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import photo_global_inventory, photo_grounding, photo_inventory
from kicadgen.services import photo_markings, pin_type_ai
from kicadgen.services import prompt_catalog as pc
from kicadgen.services.analysis_profiles import (
    PROFILE_FIELDS, SCHEMA8_PROMPT_FIELDS, SCHEMA9_PROMPT_FIELDS, SCHEMA_VERSION, ProfileStore)
from kicadgen.services.component_types import text_digest

from test_single_component_prompt import _CHAINS_ANSWER, _drawing, _single, offline  # noqa: F401
from test_photo_global_inventory import candidate, response, run

SERVICES = Path(__file__).resolve().parents[1] / 'kicadgen' / 'services'

#: SHA-256 of the texts that were built inside a function at commit 1266fb7,
#: computed from `git show 1266fb7:<file>` when they were lifted: the module
#: constants must be the same bytes (no prompt changed by the lift).
LIFTED_DIGESTS = {
    (ext, 'CONTACT_STYLE_PROMPT'):
        'ea0e1707ab43a1b89b6bcb5082e17cba38713f4b94dec19d67d5ec8d2482ffdd',
    (photo_grounding, 'SEMANTIC_VERIFICATION_PROMPT'):
        'c9c3266b0d6b598b560f2ffa506eeba8ef1228930e81d27eedfe3b12b20a9b8c',
    (photo_inventory, 'LEGACY_STRIP_PROMPT'):
        '7a9ac490b72a13febcf6e3b35f0f6793eac221a3e0c133835ce41a864fb59031',
    (pin_type_ai, 'PIN_TYPE_PROMPT'):
        'd896ef4b07b6c8c11c1733d4f275c09f3b9bf3bb4b761c1f4205fa7a9634edfb',
}


# ── the lift and the registry ──

@pytest.mark.parametrize('owner,name', list(LIFTED_DIGESTS))
def test_a_lifted_prompt_is_byte_identical_to_the_inline_text_it_replaces(owner, name):
    text = getattr(owner, name)
    assert hashlib.sha256(text.encode()).hexdigest() == LIFTED_DIGESTS[(owner, name)]


def test_no_service_function_builds_a_long_literal_prompt_any_more():
    """Positive and negative of the P1 gap: a long literal `prompt = (...)` inside a function.

    A local name holding a literal of 200 characters or more is a prompt the
    registry cannot reach. Module-level constants (what the lift produced)
    are not inside a function and do not count.
    """
    found = []
    for path in sorted(SERVICES.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,
                                                                      ast.AsyncFunctionDef))):
            for node in ast.walk(function):
                if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.Constant,
                                                                                    ast.BinOp)):
                    continue
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if isinstance(value, str) and len(value) >= 200 and 'prompt' in names:
                    found.append(f'{path.stem}.{function.name}')
    assert found == []


def test_every_profile_prompt_has_one_field_and_the_code_texts_have_none():
    assert set(SCHEMA9_PROMPT_FIELDS) == {'contact_style_prompt', 'pin_type_prompt'}
    assert set(SCHEMA9_PROMPT_FIELDS) <= set(pc.fields()) <= set(PROFILE_FIELDS)
    assert not {e.field for e in pc.CODE_ONLY_PROMPTS} & set(PROFILE_FIELDS)
    assert pc.restore_default('contact_style_prompt') is ext.CONTACT_STYLE_PROMPT
    assert pc.restore_default('pin_type_prompt') is pin_type_ai.PIN_TYPE_PROMPT
    shown = {e.source for e in pc.CODE_ONLY_PROMPTS}
    assert shown == {'photo_grounding.SEMANTIC_VERIFICATION_PROMPT',
                     'photo_inventory.LEGACY_STRIP_PROMPT',
                     'photo_global_inventory._OMISSION_REMINDER'}


def test_effective_text_falls_back_to_the_default_only_for_missing_or_blank():
    default = pc.restore_default('marking_prompt')
    assert pc.effective_text('marking_prompt', None) is default
    assert pc.effective_text('marking_prompt', {}) is default
    assert pc.effective_text('marking_prompt', {'marking_prompt': '  \n'}) is default
    assert pc.effective_text('marking_prompt', {'marking_prompt': 'mine'}) == 'mine'


def test_profile_prompts_leave_out_the_models_and_the_photo_notes():
    values = dict.fromkeys(PROFILE_FIELDS, 'x')
    prompts = pc.profile_prompts(values)
    assert 'model' not in prompts and 'image_instructions' not in prompts
    assert set(prompts) == set(pc.fields()) - {'image_instructions'}


# ── schema 9 ──

def test_v8_database_receives_the_two_lifted_prompts_and_keeps_a_row_already_there(tmp_path):
    path = tmp_path / 'ai.sqlite3'
    store = ProfileStore(path)
    store.save_defaults({'system_prompt': 'mine'})
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM ai_defaults WHERE name='contact_style_prompt'")
        db.execute("UPDATE ai_defaults SET value='kept pin prompt' WHERE name='pin_type_prompt'")
        db.execute('PRAGMA user_version=8')
    upgraded = ProfileStore(path).defaults()
    assert upgraded['contact_style_prompt'] == ext.CONTACT_STYLE_PROMPT    # inserted
    assert upgraded['pin_type_prompt'] == 'kept pin prompt'                # never replaced
    assert upgraded['system_prompt'] == 'mine'
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 9


# ── datasheet: pad and body readings, document roles ──

def _single_part_run(monkeypatch, tmp_path, prompts, logs=None):
    """The three calls of a single part (main, pad chains, body quotes), faked in order."""
    body = {"drawing_unit": "mm", "body_length": 20.5, "body_width": 10.5, "body_height": 16.9,
            "lead_length": 3.5, "printed_vertical_quotes": [16.9, 0.4, 3.5], "notes": ""}
    main = _single(body_height_mm=None)
    main.setdefault("printed_vertical_quotes", [16.9, 0.4, 3.5])
    call = Mock(side_effect=[main, _CHAINS_ANSWER, body])
    monkeypatch.setattr(ext, "call_tool", call)
    ext.extract_pin_header_params(
        [_drawing(tmp_path)], model="vendor/main", component_family="single_tht",
        pad_model="vendor/reader", document_roles={"quote.png": ["top"]},
        progress=(logs.append if logs is not None else None),
        **({"prompts": prompts} if prompts is not ... else {}))
    return call


def test_edited_pad_and_body_prompts_reach_their_dedicated_calls(offline, monkeypatch, tmp_path):
    call = _single_part_run(monkeypatch, tmp_path, {
        'pad_positions_prompt': 'MY PAD PROMPT', 'body_dimensions_prompt': 'MY BODY PROMPT'})
    assert call.call_args_list[1].kwargs['system_prompt'] == 'MY PAD PROMPT'
    assert call.call_args_list[2].kwargs['system_prompt'] == 'MY BODY PROMPT'


def test_blank_or_missing_pad_and_body_prompts_send_the_defaults(offline, monkeypatch, tmp_path):
    call = _single_part_run(monkeypatch, tmp_path, {'pad_positions_prompt': '   ',
                                                    'body_dimensions_prompt': ''})
    assert call.call_args_list[1].kwargs['system_prompt'] == ext.DEFAULT_PAD_POSITIONS_PROMPT
    assert call.call_args_list[2].kwargs['system_prompt'] == ext.DEFAULT_BODY_DIMENSIONS_PROMPT
    without = _single_part_run(monkeypatch, tmp_path, ...)
    assert without.call_args_list[1].kwargs['system_prompt'] == ext.DEFAULT_PAD_POSITIONS_PROMPT


def _first_text(call):
    """Text of the first content block of the main request (the file and its roles)."""
    return call.call_args_list[0].kwargs['content_blocks'][0]['text']


def test_an_edited_role_instruction_is_what_the_model_reads_for_that_file(offline, monkeypatch,
                                                                         tmp_path):
    default = ext._ROLE_PROMPTS['top'][1]
    edited = _single_part_run(monkeypatch, tmp_path, {'role_prompt_top': 'MY TOP ROLE'})
    text = _first_text(edited)
    # The role NAME stays the code's label; only the instruction is the profile's.
    assert f"- {ext._ROLE_PROMPTS['top'][0]}: MY TOP ROLE" in text and default not in text
    blank = _single_part_run(monkeypatch, tmp_path, {'role_prompt_top': ' '})
    assert default in _first_text(blank)


def test_the_reread_functions_send_the_profile_pad_and_body_prompts(monkeypatch):
    captured = {}

    def reader(name):
        def read(documents, *, model, call, prompt=None, **_kwargs):
            captured[name] = prompt
            raise ext.ExtractionCancelled('stop here')
        return read
    monkeypatch.setattr(ext, 'read_pad_positions', reader('pad'))
    monkeypatch.setattr(ext, 'read_body_dimensions', reader('body'))
    monkeypatch.setattr(ext, '_reread_start', lambda stored, progress: dict(
        stored, package_geometry={'pins': []}))
    monkeypatch.setattr(ext, '_has_explicit_pads', lambda data: True)
    monkeypatch.setattr(ext, '_reread_documents', lambda documents: [Path('d.png')])
    for function, field, name in ((ext.reread_pad_positions, 'pad_positions_prompt', 'pad'),
                                  (ext.reread_body_height, 'body_dimensions_prompt', 'body')):
        with pytest.raises(ext.ExtractionCancelled):
            function({}, ['d.png'], model='m', call=None, prompts={field: 'EDITED'})
        assert captured[name] == 'EDITED'
        with pytest.raises(ext.ExtractionCancelled):
            function({}, ['d.png'], model='m', call=None)
        assert captured[name] == pc.restore_default(field)


def test_contact_style_recheck_sends_the_profile_text_or_its_default():
    def data():
        return {"layout": "castellated", "castellated_sides": [
            {"edge": "left", "count": 3, "pitch_mm": 2.54, "first_pin": 1, "contact_style": None}]}
    call = Mock(return_value={"sides": []})
    ext._recheck_missing_contact_styles(data(), [{"type": "image_url"}], call, "m",
                                        prompt="MY CONTACT PROMPT")
    assert call.call_args.kwargs['system_prompt'] == "MY CONTACT PROMPT"
    ext._recheck_missing_contact_styles(data(), [{"type": "image_url"}], call, "m", prompt="  ")
    assert call.call_args.kwargs['system_prompt'] is ext.CONTACT_STYLE_PROMPT


# ── analysis log: one line per prompt sent, never the text ──

def _prompt_lines(logs):
    return [str(getattr(line, 'text', line)) for line in logs
            if 'Prompt inviato' in str(getattr(line, 'text', line))]


def test_the_log_names_each_prompt_sent_with_its_status_and_digest(offline, monkeypatch, tmp_path):
    logs = []
    secret = 'MY PAD PROMPT with a private note'
    _single_part_run(monkeypatch, tmp_path, {'pad_positions_prompt': secret}, logs)
    lines = _prompt_lines(logs)
    pad = [line for line in lines if 'Datasheet: posizioni dei pad' in line]
    assert len(pad) == 1 and pad[0].startswith("Prompt inviato «Datasheet: posizioni dei pad»: personalizzato, n. ")
    # The number turns back into the exact text sent (registry, 2026-11-12).
    from kicadgen.services import sent_prompts
    number = int(pad[0].rsplit("n. ", 1)[1].rstrip("."))
    row = sent_prompts.lookup(number)
    assert row["text"] == secret and row["field"] == "pad_positions_prompt" and row["status"] == "custom"
    body = [line for line in lines if 'Datasheet: quote del corpo' in line]
    assert len(body) == 1 and ': predefinito,' in body[0]
    single = [line for line in lines if 'Datasheet: componente singolo' in line]
    assert len(single) == 1 and ': predefinito,' in single[0]
    # A prompt this run never sends (the board datasheet prompt) has no line.
    assert not any('Datasheet: schede e moduli' in line for line in lines)
    assert not any(secret in str(getattr(line, 'text', line)) for line in logs
                   if 'AI_CALL' not in str(getattr(line, 'text', line)))


def test_an_outdated_text_is_logged_as_an_old_default(monkeypatch):
    old = 'pad instructions of a former release'
    monkeypatch.setattr(pc, 'PROMPTS', tuple(
        pc.PromptEntry(**{**e.__dict__, 'superseded': (lambda: frozenset({text_digest(old)}))})
        if e.field == 'pad_positions_prompt' else e for e in pc.PROMPTS))
    monkeypatch.setattr(pc, '_BY_FIELD', {e.field: e for e in pc.PROMPTS})
    logs = []
    log = pc.SentPromptLog({'pad_positions_prompt': old}, logs.append, register=lambda field, text: 7)
    log.observe({'system_prompt': old})
    log.observe({'system_prompt': old})                       # once per run
    assert logs == [f"AI_PROMPT_SENT number=7 field=pad_positions_prompt sha256={text_digest(old)}",
                    "Prompt inviato «Datasheet: posizioni dei pad»: vecchio predefinito, n. 7."]


def test_the_same_text_keeps_its_number_and_a_new_text_gets_the_next(monkeypatch):
    """@brief Positive and negative (registry, 2026-11-12): one number per exact text."""
    from kicadgen.services import sent_prompts
    first = sent_prompts.register('pad_positions_prompt', 'testo A', 'custom')
    assert sent_prompts.register('pad_positions_prompt', 'testo A', 'custom') == first
    second = sent_prompts.register('pad_positions_prompt', 'testo A ', 'custom')
    assert second != first, "one more blank is another text, another number"
    other_field = sent_prompts.register('numbering_prompt', 'testo A', 'custom')
    assert other_field not in (first, second)
    assert sent_prompts.lookup(first)['text'] == 'testo A'
    assert sent_prompts.lookup(second)['text'] == 'testo A '
    assert sent_prompts.lookup(10 ** 6) is None and sent_prompts.lookup('x') is None


def test_the_registry_is_not_the_committed_profile_database():
    """@brief Negative (review 2026-11-12): prompt texts never reach the published database."""
    import sqlite3
    from kicadgen.services import sent_prompts
    from kicadgen.services.analysis_profiles import SCHEMA_VERSION, ProfileStore
    store = ProfileStore()
    sent_prompts.register('pad_positions_prompt', 'nota privata', 'custom')
    assert sent_prompts.registry_path() != store.path
    assert sent_prompts.registry_path().name == 'sent_prompts.sqlite3'
    with sqlite3.connect(store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'sent_prompts' not in tables and SCHEMA_VERSION == 9


def test_a_real_registry_failure_falls_back_to_the_digest(monkeypatch):
    """@brief Negative on the real path: an unwritable registry gives the digest line, the run goes on."""
    import sqlite3
    from kicadgen.services import sent_prompts

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sent_prompts, 'register', broken)
    assert pc.register_sent('pad_positions_prompt', 'testo C') is None
    logs = []
    pc.SentPromptLog({'pad_positions_prompt': 'testo C'}, logs.append).observe({'system_prompt': 'testo C'})
    assert logs[-1].endswith(f"sha256 {text_digest('testo C')[:8]}.")


def test_a_registry_that_cannot_be_written_falls_back_to_the_digest():
    """@brief Negative: a failing registry never stops a run; the line names the digest."""
    logs = []
    log = pc.SentPromptLog({'pad_positions_prompt': 'testo B'}, logs.append,
                           register=lambda field, text: None)
    log.observe({'system_prompt': 'testo B'})
    assert logs == [f"AI_PROMPT_SENT number=- field=pad_positions_prompt sha256={text_digest('testo B')}",
                    f"Prompt inviato «Datasheet: posizioni dei pad»: personalizzato, "
                    f"sha256 {text_digest('testo B')[:8]}."]


def test_the_machine_line_stays_out_of_the_progress_card():
    """@brief The full digest is for the log file only (user, 2026-11-12: «l'impronta non mi serve»)."""
    from kicadgen.gui.analysis_overlay import is_machine_line
    assert is_machine_line(pc.prompt_machine_line('pad_positions_prompt', 'x', 3))
    assert not is_machine_line(pc.prompt_log_line('pad_positions_prompt', 'x', 3))


def test_without_a_profile_the_extraction_writes_no_prompt_line(offline, monkeypatch, tmp_path):
    logs = []
    _single_part_run(monkeypatch, tmp_path, ..., logs)
    assert _prompt_lines(logs) == []


# ── photo: inventory, markings, mechanical contract ──

def test_an_edited_inventory_prompt_is_sent_and_misses_the_cache_the_default_hit(tmp_path):
    _result, calls, _logs = run(response(), cache=tmp_path)
    assert len(calls) == 1 and calls[0]['system_prompt'].startswith(photo_global_inventory.PROMPT)
    _result, calls, _logs = run(response(), cache=tmp_path)
    assert calls == []                                       # the default replays
    _result, calls, _logs = run(response(), cache=tmp_path, prompt='MY INVENTORY PROMPT')
    assert len(calls) == 1 and calls[0]['system_prompt'].startswith('MY INVENTORY PROMPT')
    assert photo_global_inventory.PROMPT not in calls[0]['system_prompt']
    result, calls, _logs = run(response(), cache=tmp_path, prompt='MY INVENTORY PROMPT')
    assert calls == []                                       # the edited text replays its own
    assert result[0]['_global_inventory_prompt_hash'] == hashlib.sha256(
        b'MY INVENTORY PROMPT').hexdigest()
    _result, calls, _logs = run(response(), cache=tmp_path, prompt='  ')
    assert calls == []                                       # blank = the default again


def test_an_old_cache_record_without_a_matching_key_is_a_miss_not_a_crash(tmp_path):
    (tmp_path / 'global-inventory-deadbeef.json').write_text('{"key": "other"}', encoding='utf-8')
    (tmp_path / 'global-inventory-broken.json').write_text('not json', encoding='utf-8')
    _result, calls, _logs = run(response(), cache=tmp_path, prompt='MY INVENTORY PROMPT')
    assert len(calls) == 1


def test_the_enumeration_prompts_are_chosen_by_what_the_detector_did():
    """Without candidates: 'enumerate' when no detector ran, 'enumerate_empty' when it found nothing."""
    texts = dict(prompt='WITH CANDIDATES', enumerate_prompt='NO DETECTOR',
                 enumerate_empty_prompt='EMPTY DETECTOR')
    for detector_ran, expected in ((False, 'NO DETECTOR'), (True, 'EMPTY DETECTOR')):
        calls = []

        def fake(**request):
            calls.append(request)
            return {'components': [], 'candidate_decisions': [], 'uncertainties': []}
        with pytest.raises(ValueError):
            photo_global_inventory.review_global_inventory(
                np.zeros((60, 120, 3), np.uint8), [], 40, 20, 'm', fake, lambda _l: None,
                detector_ran=detector_ran, **texts)
        assert calls and all(c['system_prompt'].startswith(expected) for c in calls)


def test_extract_inventory_forwards_the_profile_inventory_and_marking_prompts(monkeypatch):
    seen = {}
    monkeypatch.setattr(photo_markings, 'read_markings',
                        lambda *a, **k: seen.setdefault('marking', k.get('prompt')))
    report = photo_inventory.InventoryResult()
    report.components = []
    photo_inventory.read_retained_bodies(np.zeros((10, 10, 3), np.uint8), report, 1, 1, 'm', None,
                                         lambda _l: None, None, None, lambda *a: None,
                                         marking_prompt='MY MARKING')
    assert seen['marking'] == 'MY MARKING'
    assert photo_inventory._inventory_prompt_kwargs(None) == {}
    assert photo_inventory._inventory_prompt_kwargs({'inventory_prompt': 'A'}) == dict(
        prompt='A', enumerate_prompt=None, enumerate_empty_prompt=None)


LENGTH, WIDTH = 42.8, 15.2


def _marked_photo():
    return np.full((round(4000 * WIDTH / LENGTH), 4000, 3), 40, np.uint8)


def _marked_components():
    return [dict(name="U1", x_mm=-5.0, y_mm=0.0, length_mm=5.0, width_mm=4.0, rotation_deg=0.0,
                 marking="SP3?52", marking_confidence="medium")]


def test_an_edited_marking_prompt_is_sent_recorded_and_cached_apart(tmp_path):
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return {'components': [dict(id=1, marking='SP3452', marking_confidence='high')]}
    components = _marked_components()
    photo_markings.read_markings(_marked_photo(), components, LENGTH, WIDTH,
                                 'anthropic/claude-opus-5', call, lambda _l: None, tmp_path)
    assert calls[-1]['system_prompt'] is photo_markings.PROMPT
    assert components[0]['_marking_prompt_sha256'] == hashlib.sha256(
        photo_markings.PROMPT.encode()).hexdigest()
    photo_markings.read_markings(_marked_photo(), _marked_components(), LENGTH, WIDTH,
                                 'anthropic/claude-opus-5', call, lambda _l: None, tmp_path)
    assert len(calls) == 1                                   # the default replays
    edited = _marked_components()
    photo_markings.read_markings(_marked_photo(), edited, LENGTH, WIDTH, 'anthropic/claude-opus-5',
                                 call, lambda _l: None, tmp_path, prompt='MY MARKING PROMPT')
    assert len(calls) == 2 and calls[-1]['system_prompt'] == 'MY MARKING PROMPT'
    assert edited[0]['_marking_prompt_sha256'] == hashlib.sha256(b'MY MARKING PROMPT').hexdigest()


def test_the_mechanical_contract_keeps_the_default_bytes_and_joins_an_edit_with_a_space():
    default = photo_grounding.MECHANICAL_EVIDENCE_PROMPT
    assert default.startswith(' ')
    for text in (None, '', '  ', default, default.strip()):
        assert photo_grounding.mechanical_evidence_tail(text) is default
    assert photo_grounding.mechanical_evidence_tail('Mine.') == ' Mine.'
    assert photo_grounding.mechanical_evidence_tail('\nMine.') == '\nMine.'


def test_the_mechanical_verification_request_carries_the_profile_contract(tmp_path):
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return {'components': [dict(id=1, present=True, confidence='high', package='')]}
    photo = np.full((100, 200, 3), 90, np.uint8)
    component = dict(_id=1, _box=[.2, .2, .4, .4], kind='generic', length_mm=2.0, width_mm=1.0,
                     x_mm=0.0, y_mm=0.0, rotation_deg=0.0)
    photo_grounding.mechanical_envelopes(photo, [dict(component)], 'm', call, lambda _l: None,
                                         None, mechanical_evidence_prompt='MY CONTRACT.')
    assert calls[-1]['system_prompt'] == photo_grounding.VERIFICATION_PROMPT + ' MY CONTRACT.'
    photo_grounding.mechanical_envelopes(photo, [dict(component)], 'm', call, lambda _l: None, None)
    assert calls[-1]['system_prompt'] == (photo_grounding.VERIFICATION_PROMPT
                                          + photo_grounding.MECHANICAL_EVIDENCE_PROMPT)


def test_the_pin_type_suggestion_sends_the_profile_text_and_logs_it(tmp_path, monkeypatch):
    source = tmp_path / 'sheet.pdf'
    source.write_bytes(b'PDF')
    monkeypatch.setattr(pin_type_ai, 'ensure_providers_ready', lambda *a: None)
    monkeypatch.setattr(pin_type_ai, '_file_to_content_block',
                        lambda p: {'type': 'file', 'file': {'filename': p.name}})
    calls, logs = [], []
    monkeypatch.setattr(pin_type_ai, 'call_tool',
                        lambda **kwargs: calls.append(kwargs) or {'pins': []})
    rows = [{'pin': '1', 'name': 'GND'}]
    pin_type_ai.suggest_pin_types('Demo', rows, [source], 'm', prompt='MY PIN PROMPT',
                                  progress=logs.append)
    assert calls[-1]['system_prompt'] == 'MY PIN PROMPT'
    lines = _prompt_lines(logs)
    assert len(lines) == 1 and 'personalizzato' in lines[0] and 'MY PIN PROMPT' not in lines[0]
    pin_type_ai.suggest_pin_types('Demo', rows, [source], 'm')
    assert calls[-1]['system_prompt'] is pin_type_ai.PIN_TYPE_PROMPT


# ── workers ──

def test_the_reread_worker_forwards_the_profile_prompts_and_reports_them(qapp, monkeypatch):
    from kicadgen.gui import workers
    from kicadgen.services import openrouter, providers
    seen = {}
    monkeypatch.setattr(ext, "_pad_reading_model", lambda pad_model, main, family, p, c: main)

    def fake_reread(stored, documents, *, model, call, progress, prompts=None, **kwargs):
        seen['prompts'] = prompts
        call(model=model, system_prompt=prompts['body_dimensions_prompt'], content_blocks=[])
        return 'params'
    monkeypatch.setattr(ext, "reread_body_height", fake_reread)
    monkeypatch.setattr(openrouter, "call_tool", lambda **kwargs: {})
    monkeypatch.setattr(providers, "ensure_providers_ready", lambda models, progress=None: None)
    progress = []
    worker = workers.RereadWorker({}, ["a.png"], None, field="body_height_mm", model="m",
                                  prompts={'body_dimensions_prompt': 'MY BODY'})
    worker.progress.connect(progress.append)
    worker.run()
    assert seen['prompts'] == {'body_dimensions_prompt': 'MY BODY'}
    lines = _prompt_lines(progress)
    assert len(lines) == 1 and 'Datasheet: quote del corpo' in lines[0]
    seen.clear()
    plain = workers.RereadWorker({}, ["a.png"], None, field="body_height_mm", model="m")

    def default_reread(stored, documents, *, model, call, progress, prompts=None, **kwargs):
        seen['prompts'] = prompts
        return 'params'
    monkeypatch.setattr(ext, "reread_body_height", default_reread)
    plain.run()
    assert seen['prompts'] is None                           # nothing forwarded without a profile


def test_the_extract_worker_forwards_the_profile_prompts(qapp, monkeypatch):
    from kicadgen.gui import main_window, workers
    seen = {}

    def fake_extract(doc_paths, *, prompts=None, **kwargs):
        seen['prompts'] = prompts
        return 'params'
    monkeypatch.setattr(main_window, "extract_pin_header_params", fake_extract)
    worker = workers.ExtractWorker([Path('a.pdf')], prompts={'marking_prompt': 'MINE'})
    worker.run()
    assert seen['prompts'] == {'marking_prompt': 'MINE'}
    workers.ExtractWorker([Path('a.pdf')]).run()
    assert seen['prompts'] is None


# ── start-up notice ──

def test_the_notice_names_custom_and_outdated_texts_but_not_current_ones(tmp_path, monkeypatch):
    from kicadgen.services import photo_crop_reading
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    assert pc.notice_entries(store) == []                    # a fresh profile says nothing
    store.save_defaults({'marking_prompt': 'my marking'})
    old = 'crop reading of a former release'
    monkeypatch.setattr(photo_crop_reading, 'SUPERSEDED_CROP_PROMPT_DIGESTS',
                        frozenset({text_digest(old)}))
    key = store.save_type(None, label='own', family='single_smd', overrides={'crop_prompt': old})
    rows = {row['field']: row for row in pc.notice_entries(store, key)}
    assert set(rows) == {'marking_prompt', 'crop_prompt'}
    assert rows['marking_prompt']['status'] == pc.CUSTOM
    assert rows['crop_prompt']['status'] == pc.OUTDATED
    assert rows['marking_prompt']['digest'] == text_digest('my marking')


def test_a_dismissed_text_stays_quiet_and_a_new_difference_is_named_again(tmp_path):
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    store.save_defaults({'marking_prompt': 'my marking'})
    dismissed = {('marking_prompt', text_digest('my marking'))}
    assert pc.notice_entries(store, '', dismissed) == []
    store.save_defaults({'marking_prompt': 'my marking, edited again'})
    [row] = pc.notice_entries(store, '', dismissed)
    assert row['field'] == 'marking_prompt'


def test_the_window_shows_the_notice_once_per_text_and_remembers_dismissal(window, monkeypatch):
    """CONTRACT CHANGED (review V5-2, 2026-11-11): once per (field, digest) in
    the session (`_prompt_notice_seen`), no longer once per profile key
    (`_prompt_notice_checked`): the types inherit the global prompts, and a
    check per type named the same texts again on every type switch. A new
    digest is named again within the same session, without a restart."""
    from PyQt6.QtWidgets import QMessageBox
    window.profile_store.save_defaults({'marking_prompt': 'my marking'})
    monkeypatch.setattr(window, 'isVisible', lambda: True)
    shown = []
    real_show = window._show_prompt_notice
    monkeypatch.setattr(window, '_show_prompt_notice', lambda entries: shown.append(entries))
    window._check_prompt_notice()
    window._check_prompt_notice()                             # same profile, same session
    assert len(shown) == 1 and [r['field'] for r in shown[0]] == ['marking_prompt']
    # The real notice: non-modal, its text never carries the prompt, and
    # «Non mostrare più per questi testi» stores the (field, digest).
    real_show(shown[0])
    box = window._prompt_notice_box
    try:
        assert box.windowModality().name == 'NonModal'
        assert 'my marking' not in box.text() and 'Foto: lettura delle marcature' in box.text()
        [dismiss] = [b for b in box.buttons() if b.text() == 'Non mostrare più per questi testi']
        dismiss.click()
    finally:
        box.close()
    assert f"marking_prompt:{text_digest('my marking')}" in window._config['prompt_notice_dismissed']
    window._prompt_notice_seen.clear()                        # as on the next program start
    shown.clear()
    window._check_prompt_notice()
    assert shown == []
    window.profile_store.save_defaults({'marking_prompt': 'another text'})
    window._check_prompt_notice()                             # same session, a new digest
    assert len(shown) == 1
    assert QMessageBox  # imported for the box type above


def test_no_notice_while_the_window_is_hidden_or_the_profile_is_current(window, monkeypatch):
    shown = []
    monkeypatch.setattr(window, '_show_prompt_notice', lambda entries: shown.append(entries))
    window.profile_store.save_defaults({'marking_prompt': 'my marking'})
    window._check_prompt_notice()                             # hidden: not checked, not consumed
    assert shown == [] and not getattr(window, '_prompt_notice_seen', set())
    monkeypatch.setattr(window, 'isVisible', lambda: True)
    window.profile_store.save_defaults({'marking_prompt': pc.restore_default('marking_prompt')})
    window._check_prompt_notice()
    assert shown == []


# ── profile dialog ──

def test_the_dialog_groups_the_roles_and_the_inventory_and_lists_the_code_texts(app, tmp_path):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QTabWidget, QTextEdit
    from kicadgen.gui import profile_dialog
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    editor = profile_dialog.ProfileEditor(store)
    try:
        direct = Qt.FindChildOption.FindDirectChildrenOnly

        def captions(widget):
            return [widget.tabText(i) for i in range(widget.count())]
        [datasheet] = editor.tabs.widget(0).findChildren(QTabWidget, '', direct)
        [photo] = editor.tabs.widget(1).findChildren(QTabWidget, '', direct)
        assert pc.GROUP_ROLES in captions(datasheet)
        roles = datasheet.widget(captions(datasheet).index(pc.GROUP_ROLES))
        assert len(captions(roles)) == 6
        assert not any(c.startswith('Ruolo documento') for c in captions(datasheet))
        inventory = photo.widget(captions(photo).index(pc.GROUP_INVENTORY))
        assert len(captions(inventory)) == 3
        assert captions(photo)[-1] == 'Testi nel codice'
        page = photo.widget(photo.count() - 1)
        texts = {t.objectName(): t for t in page.findChildren(QTextEdit)}
        assert set(texts) == {e.field for e in pc.CODE_ONLY_PROMPTS}
        assert all(t.isReadOnly() for t in texts.values())
        assert not set(texts) & set(editor.fields)            # never saved
        for field in (*SCHEMA8_PROMPT_FIELDS, *SCHEMA9_PROMPT_FIELDS):
            assert field in editor.fields and field in editor.status_labels
    finally:
        editor.close()


def test_saving_the_dialog_unchanged_keeps_every_new_field_byte_identical(app, tmp_path):
    from kicadgen.gui import profile_dialog
    store = ProfileStore(tmp_path / 'ai.sqlite3')
    before = store.defaults()
    editor = profile_dialog.ProfileEditor(store)
    try:
        editor._save()
    finally:
        editor.close()
    after = store.defaults()
    for field in (*SCHEMA8_PROMPT_FIELDS, *SCHEMA9_PROMPT_FIELDS):
        assert after[field] == before[field] == pc.restore_default(field)
    # The leading blank of the mechanical contract survives the save.
    assert after['mechanical_evidence_prompt'].startswith(' ')


def test_the_new_visible_strings_have_four_languages():
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    sources = ["Prompt inviato «{title}»: {status}, sha256 {digest}.",
               "Prompt inviato «{title}»: {status}, n. {number}.", "personalizzato",
               "vecchio predefinito", "predefinito", "Prompt diversi dal predefinito",
               "Non mostrare più per questi testi", "Apri la gestione dei profili AI",
               "Testi nel codice", "{title}: {status}"]
    for source in sources:
        entry = DEFAULT_TRANSLATIONS['ui:' + source]
        assert set(entry) >= {'it', 'en', 'fr', 'de'}
        for text in entry.values():
            assert ('{digest}' in text) == ('{digest}' in source)
    assert json  # module used by the helpers imported above
