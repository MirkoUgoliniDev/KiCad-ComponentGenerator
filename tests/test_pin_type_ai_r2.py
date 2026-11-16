"""@brief Review round 2 (2026-11-15) of the pin-type suggestion: null location, exact and NFC names.

R12: an OpenAI-style model sends an optional field it skips as an explicit
null; a null `location` raised and the whole paid answer was lost.
Low: the cited name was stripped before the exact comparison (a sent name with
a leading or trailing blank was demoted) and NFD/NFC spellings of one name
(a macOS zip) did not match. No network: no call is made.
"""
import unicodedata

import pytest

from kicadgen.services import pin_type_ai as ai

ROWS = [{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': 'GPIO1'}]


def _proposal(source='pinout.jpeg', reason='Pin 1 is the module supply return.', **extra):
    """One proposal for pin 1 as the tool returns it."""
    return dict(pin='1', electrical_type='power_in', source=source, reason=reason, **extra)


# -- R12: a null location ----------------------------------------------------------------------

# CONTRACT CHANGED 2026-11-15 (round 2): a null `location` raised «Fonte e
# motivazione AI devono essere testo.»; it now means «no location».
def test_a_null_location_is_no_location_and_keeps_the_paid_answer():
    result = ai.validate_suggestions({'pins': [_proposal(location=None)]}, ROWS, ['pinout.jpeg'])[0]
    assert result['electrical_type'] == 'power_in'
    assert result['reason'] == 'Pin 1 is the module supply return.'


@pytest.mark.parametrize('value', [3, 2.5, True, ['page 2'], {'page': 2}])
def test_a_location_of_another_type_is_still_refused(value):
    """Negative twin: only null is tolerated; a number, list or object is a malformed answer."""
    with pytest.raises(ValueError):
        ai.validate_suggestions({'pins': [_proposal(location=value)]}, ROWS, ['pinout.jpeg'])


@pytest.mark.parametrize('field', ['source', 'reason'])
def test_a_null_required_field_is_still_refused(field):
    """The tolerance is for the OPTIONAL location only: source and reason are required."""
    item = _proposal()
    item[field] = None
    with pytest.raises(ValueError):
        ai.validate_suggestions({'pins': [item]}, ROWS, ['pinout.jpeg'])


# -- exact comparison first --------------------------------------------------------------------

@pytest.mark.parametrize('sent', [' pinout.jpeg', 'pinout.jpeg ', ' pinout.jpeg '])
def test_a_sent_name_with_surrounding_blanks_is_cited_exactly(sent):
    """Positive: Windows allows a leading blank (and a trailing one inside a zip); the enum value
    IS the sent name, so it is evidence and the cited source is that very name."""
    result = ai.validate_suggestions({'pins': [_proposal(source=sent)]}, ROWS, [sent, 'sheet.pdf'])[0]
    assert result['electrical_type'] == 'power_in' and result['source'] == sent


def test_two_names_that_differ_only_by_blanks_are_told_apart_exactly_and_never_guessed():
    """Exact values pick their own file; a value that matches both only once stripped is
    ambiguous and stays unresolved."""
    sent = ['pinout.jpeg', ' pinout.jpeg']
    for name in sent:
        result = ai.validate_suggestions({'pins': [_proposal(source=name)]}, ROWS, sent)[0]
        assert result['source'] == name and result['electrical_type'] == 'power_in'
    loose = ai.validate_suggestions({'pins': [_proposal(source='pinout.jpeg  ')]}, ROWS, sent)[0]
    assert loose['electrical_type'] == 'unknown'


def test_blanks_added_by_the_model_still_find_the_one_sent_name():
    """The tolerant fallback is kept: a model that pads the value still cites the sent file."""
    result = ai.validate_suggestions({'pins': [_proposal(source='  pinout.jpeg ')]}, ROWS,
                                     ['pinout.jpeg'])[0]
    assert result['source'] == 'pinout.jpeg' and result['electrical_type'] == 'power_in'


# -- Unicode normalisation ---------------------------------------------------------------------

NFC_NAME = unicodedata.normalize('NFC', 'piedinatura_relè.pdf')
NFD_NAME = unicodedata.normalize('NFD', 'piedinatura_relè.pdf')


@pytest.mark.parametrize('sent,answered', [(NFD_NAME, NFC_NAME), (NFC_NAME, NFD_NAME)])
def test_nfc_and_nfd_spellings_of_one_name_cite_the_sent_file(sent, answered):
    """Positive both ways: a decomposed name from a macOS zip and the composed one a model
    writes are the same name; the cited source is the name as SENT."""
    assert sent != answered
    result = ai.validate_suggestions({'pins': [_proposal(source=answered)]}, ROWS, [sent])[0]
    assert result['electrical_type'] == 'power_in' and result['source'] == sent


def test_normalisation_does_not_merge_different_letters():
    """Negative twin: «relé» (acute) is not «relè» (grave) in any normal form."""
    other = unicodedata.normalize('NFC', 'piedinatura_relé.pdf')
    result = ai.validate_suggestions({'pins': [_proposal(source=other)]}, ROWS, [NFD_NAME])[0]
    assert result['electrical_type'] == 'unknown'


@pytest.mark.parametrize('names,distinct', [
    ([NFC_NAME, NFD_NAME], False),                       # one name for a model, two files on NTFS
    ([NFD_NAME, NFD_NAME.upper()], False),               # case folded after normalisation
    ([NFC_NAME, unicodedata.normalize('NFC', 'piedinatura_relé.pdf')], True),
])
def test_sent_names_are_distinct_after_nfc_and_casefold(names, distinct):
    assert ai._distinct_sources(names) is distinct


def test_two_spellings_of_one_name_are_refused_before_the_paid_call(tmp_path, monkeypatch):
    """The refusal happens before any call: the model could not tell the two sources apart."""
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a, **k: None)
    called = []
    monkeypatch.setattr(ai, 'call_tool', lambda **kw: called.append(kw))
    with pytest.raises(ValueError):
        ai.suggest_pin_types('Demo', ROWS, [tmp_path / 'a' / NFC_NAME, tmp_path / 'b' / NFD_NAME], 'model')
    assert called == []
