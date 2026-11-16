"""@brief Round-3 fix of the sent-name comparison: lower case like NTFS, not casefold.

«Maße.pdf» and «Masse.pdf» are two files on NTFS (Windows compares names
letter by letter through a one-to-one upper-case table) but `str.casefold`
turns «ß» into «ss», so the two documents were refused as one source and the
citation of either was undecided. The comparison key is now NFC + `lower`,
in the distinctness check and in the citation match alike.
"""
import unicodedata

import pytest

from kicadgen.services import pin_type_ai as ai

ROWS = [{'pin': '1', 'name': 'VCC', 'electrical_type': 'passive'}]


def _proposal(source):
    """One proposal for pin 1 citing `source`."""
    return {'pin': '1', 'electrical_type': 'power_in', 'source': source, 'reason': 'Table 2'}


@pytest.mark.parametrize('names,distinct', [
    (['Maße.pdf', 'Masse.pdf'], True),        # two files on NTFS: «ß» is not «ss»
    (['MASSE.pdf', 'masse.pdf'], False),      # letter case only: one name on Windows
    (['Maße.pdf', 'MAßE.PDF'], False),        # «ß» has no one-letter capital: case only
    ([unicodedata.normalize('NFD', 'Relè.pdf'), 'RELÈ.PDF'], False),  # NFD + case
])
def test_sent_names_are_distinct_after_nfc_and_lower_case(names, distinct):
    """@brief Positive and negative: a different LETTER keeps two names apart, a case change does not."""
    assert ai._distinct_sources(names) is distinct


def test_the_two_sharp_s_files_are_sent_in_one_call(tmp_path, monkeypatch):
    """@brief Positive at the call: no refusal before the paid request for «Maße» and «Masse»."""
    monkeypatch.setattr(ai, 'ensure_providers_ready', lambda *a, **k: None)
    monkeypatch.setattr(ai, '_file_to_content_block', lambda p: {'type': 'text', 'text': p.name})
    calls = []
    monkeypatch.setattr(ai, 'call_tool', lambda **kw: calls.append(kw) or {'pins': []})
    ai.suggest_pin_types('Demo', ROWS, [tmp_path / 'Maße.pdf', tmp_path / 'Masse.pdf'], 'model')
    assert len(calls) == 1


@pytest.mark.parametrize('cited,expected', [
    ('MASSE.PDF', 'Masse.pdf'),      # case changed by a provider: «ss» names «Masse»
    ('maße.pdf', 'Maße.pdf'),        # «ß» names «Maße», never «Masse»
])
def test_a_case_changed_citation_names_the_one_sent_file(cited, expected):
    """@brief The citation match uses the same key: each spelling picks its own file."""
    result = ai.validate_suggestions({'pins': [_proposal(cited)]}, ROWS,
                                     ['Maße.pdf', 'Masse.pdf'])[0]
    assert result['source'] == expected and result['electrical_type'] == 'power_in'


def test_a_citation_equal_to_two_sent_names_after_lower_case_stays_undecided():
    """@brief Negative kept: when lower case cannot tell two sent names apart, nothing is guessed."""
    result = ai.validate_suggestions({'pins': [_proposal('PINOUT.JPEG')]}, ROWS,
                                     ['pinout.jpeg', 'Pinout.jpeg'])[0]
    assert result['electrical_type'] == 'unknown'
