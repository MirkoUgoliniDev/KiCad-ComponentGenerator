"""@brief Review fixes of 2026-11-15 (fixer C3): the pin-type source chosen among the sent files, and
the contact view named in words in the pad and numbering messages.

A3/M7/F6/F7: the cited document of an AI pin-type proposal was found by a regex in free text, which
took other files, URLs, paths and denials for the sent document. Now the tool schema offers the sent
file names as an enum and a separate optional `location`; evidence is equality with a sent name.
P5: the pad-layout question showed «Risposta principale (unknown)», the raw view token.
No network: every paid call is faked.
"""
import pytest

from kicadgen.services import pin_type_ai as ai
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.openrouter import strict_tool

ROWS = [{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': 'GPIO1'}]


def _proposal(source='pinout.jpeg', reason='Pin 1 is the module supply return.', **extra):
    """One proposal for pin 1 as the tool returns it."""
    return dict(pin='1', electrical_type='power_in', source=source, reason=reason, **extra)


# -- the schema --------------------------------------------------------------------------------

def test_the_source_is_an_enum_of_the_sent_names_and_the_location_is_optional():
    tool = ai.pin_type_tool(['pinout.jpeg', 'sheet.pdf'])
    item = tool['function']['parameters']['properties']['pins']['items']
    assert item['properties']['source']['enum'] == ['pinout.jpeg', 'sheet.pdf']
    assert item['properties']['location']['type'] == 'string'
    assert 'location' not in item['required'] and 'source' in item['required']


def test_the_schema_still_converts_to_strict_mode():
    """The new enum and optional field keep the DeepSeek strict conversion lossless."""
    strict = strict_tool(ai.pin_type_tool(['pinout.jpeg']))
    assert strict is not None
    # Every property is nullable in strict mode: the array sits in anyOf[array, null].
    item = strict['function']['parameters']['properties']['pins']['anyOf'][0]['items']
    assert set(item['required']) == {'pin', 'electrical_type', 'source', 'location', 'reason'}


def test_the_call_sends_the_enum_of_the_documents_it_attaches(tmp_path, monkeypatch):
    first, second = tmp_path / 'pinout.jpeg', tmp_path / 'sheet.pdf'
    for path in (first, second):
        path.write_bytes(b'x')
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a, **k: None)
    monkeypatch.setattr(ai, '_file_to_content_block', lambda p: {'type': 'text', 'text': p.name})
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return {'pins': [_proposal(location='FUNCTION table')]}

    monkeypatch.setattr(ai, 'call_tool', call)
    result = ai.suggest_pin_types('Demo', ROWS, [first, second], 'model')
    item = calls[0]['tool']['function']['parameters']['properties']['pins']['items']
    assert item['properties']['source']['enum'] == ['pinout.jpeg', 'sheet.pdf']
    assert 'location' in calls[0]['system_prompt'] and 'nothing added' in calls[0]['system_prompt']
    assert result[0]['electrical_type'] == 'power_in'
    assert result[0]['reason'] == 'FUNCTION table: Pin 1 is the module supply return.'


# -- evidence ----------------------------------------------------------------------------------

def test_the_location_heads_the_rationale_but_is_not_evidence_alone():
    """Positive: location + reason -> kept, location shown. Negative: location without a reason
    -> 'unknown' (the location says where, only the reason says what supports the type)."""
    kept = ai.validate_suggestions({'pins': [_proposal(location='Table 2, page 3')]}, ROWS,
                                   ['pinout.jpeg'])[0]
    assert kept['electrical_type'] == 'power_in'
    assert kept['reason'] == 'Table 2, page 3: Pin 1 is the module supply return.'
    bare = ai.validate_suggestions({'pins': [_proposal(reason='  ', location='Table 2, page 3')]},
                                   ROWS, ['pinout.jpeg'])[0]
    assert bare['electrical_type'] == 'unknown' and bare['reason'] == 'Table 2, page 3'


def test_a_missing_location_is_allowed():
    """Strict mode drops a null optional field: no `location` key at all is a valid answer."""
    result = ai.validate_suggestions({'pins': [_proposal()]}, ROWS, ['pinout.jpeg'])[0]
    assert result['electrical_type'] == 'power_in'
    assert result['reason'] == 'Pin 1 is the module supply return.'


def test_an_enum_value_that_was_not_sent_is_unknown_and_keeps_its_text():
    result = ai.validate_suggestions({'pins': [_proposal(source='datasheet.pdf')]}, ROWS,
                                     ['pinout.jpeg'])[0]
    assert result['electrical_type'] == 'unknown' and result['source'] == 'datasheet.pdf'


def test_letter_case_picks_the_one_sent_name_and_never_guesses_between_two():
    """Positive: «PINOUT.JPEG» is the one name equal after casefold. Negative: a list with two
    names equal after casefold leaves the citation undecided (the call refuses such a list)."""
    one = ai.validate_suggestions({'pins': [_proposal(source='PINOUT.JPEG')]}, ROWS,
                                  ['pinout.jpeg', 'sheet.pdf'])[0]
    assert one['electrical_type'] == 'power_in' and one['source'] == 'pinout.jpeg'
    two = ai.validate_suggestions({'pins': [_proposal(source='PINOUT.JPEG')]}, ROWS,
                                  ['pinout.jpeg', 'Pinout.jpeg'])[0]
    assert two['electrical_type'] == 'unknown'
    exact = ai.validate_suggestions({'pins': [_proposal(source='Pinout.jpeg')]}, ROWS,
                                    ['pinout.jpeg', 'Pinout.jpeg'])[0]
    assert exact['source'] == 'Pinout.jpeg' and exact['electrical_type'] == 'power_in'


@pytest.mark.parametrize('names,distinct', [
    (['pinout.jpeg', 'sheet.pdf'], True),
    (['pinout.jpeg', 'PINOUT.jpeg'], False),
    (['a.pdf', 'a.pdf'], False),
])
def test_sent_names_must_be_distinct_after_casefold(names, distinct):
    assert ai._distinct_sources(names) is distinct


def test_distinct_names_in_two_folders_are_sent(tmp_path, monkeypatch):
    """Positive twin of the refused duplicates: two folders, two different names, one call."""
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a, **k: None)
    monkeypatch.setattr(ai, '_file_to_content_block', lambda p: {'type': 'text', 'text': p.name})
    monkeypatch.setattr(ai, 'call_tool', lambda **kw: {'pins': []})
    result = ai.suggest_pin_types('Demo', ROWS, [tmp_path / 'a' / 'same.pdf',
                                                  tmp_path / 'b' / 'other.pdf'], 'model')
    assert [p['electrical_type'] for p in result] == ['unknown', 'unknown']


# -- P5: the view in words ---------------------------------------------------------------------

@pytest.mark.parametrize('view,label', [
    ('top', "vista dall'alto"), ('bottom', 'vista dal basso'),
    ('unknown', 'vista non dichiarata'), (None, 'vista non dichiarata'), ('', 'vista non dichiarata'),
])
def test_a_view_token_is_shown_in_words(view, label):
    assert ext._view_label(view) == label


def test_the_numbering_conflict_never_shows_the_raw_unknown_token():
    """Real run 2026-11-15: «Risposta principale (unknown): …». An undeclared main view is said in
    words; a declared one keeps its side, also in words."""
    main = [{"pin": 2 * c + r + 1, "row": r, "col": c} for c in range(3) for r in range(2)]
    dedicated = [{"pin": 1, "row": 0, "col": 0}, {"pin": 2, "row": 0, "col": 1}]
    data = {"pin_map": list(main), "contacts_view": "unknown"}
    ext.apply_dedicated_numbering(data, {"view": "bottom", "pins": dedicated, "notes": ""})
    conflict = data["_pin_map_conflict"]
    assert "unknown" not in conflict
    assert "Risposta principale (vista non dichiarata)" in conflict
    assert "(vista dal basso, usata)" in conflict
    declared = {"pin_map": list(main), "contacts_view": "top"}
    ext.apply_dedicated_numbering(declared, {"view": "bottom", "pins": dedicated, "notes": ""})
    assert "Risposta principale (vista dall'alto)" in declared["_pin_map_conflict"]
