"""@brief KLC findings with a safe automatic fix carry a structured fix id (2026-11-13).

The GUI offers «Correggi» from `Finding.fix`, never from the translated
message. First fix: a component name with spaces -> `name_spaces_to_underscore`.
"""
import pytest

from kicadgen.services import klc_checks
from kicadgen.services.klc_checks import (FIX_NAME_SPACES, name_findings,
                                          proposed_name_without_spaces)


@pytest.mark.parametrize('name, expected', [
    ('My Board', 'My_Board'),
    ('  My   Board  ', 'My_Board'),
    ('My _ Board', 'My_Board'),
    ('My\tBoard-2.0', 'My_Board-2.0'),
    ('Keep__double here', 'Keep__double_here'),
    ('   ', ''),
])
def test_proposed_name_without_spaces(name, expected):
    """@brief Blanks (and the underscores touching them) become one «_»; other characters stay."""
    assert proposed_name_without_spaces(name) == expected


@pytest.mark.parametrize('code, kind', [('footprint_name', 'footprint'), ('symbol_name', 'symbol')])
def test_a_name_with_spaces_carries_the_fix_and_the_proposed_name(code, kind):
    """@brief Positive: the spaces warning carries `name_spaces_to_underscore` and the new name."""
    findings = name_findings('My Board', code=code, label='X', kind=kind)
    spaces = [f for f in findings if f.code == code + '_spaces']
    assert len(spaces) == 1
    assert spaces[0].fix == FIX_NAME_SPACES and spaces[0].fix_value == 'My_Board'


def test_a_name_without_spaces_has_no_fix():
    """@brief Negative: a clean name yields no finding, hence no fix."""
    assert name_findings('My_Board', code='footprint_name', label='X') == []


def test_no_fix_when_the_proposed_name_would_still_be_refused():
    """@brief Negative: spaces AND a forbidden character -> the warning stays, without a fix
    (the fix would not make the name valid, so it is not safe)."""
    findings = name_findings('My Board:2', code='footprint_name', label='X')
    spaces = [f for f in findings if f.code == 'footprint_name_spaces']
    assert len(spaces) == 1 and spaces[0].fix == '' and spaces[0].fix_value == ''


def test_only_the_name_spaces_findings_carry_a_fix():
    """@brief Every other finding of a full check has no fix id (structured, not by text)."""
    metadata = {'reference': 'U 1', 'description': '', 'keywords': '', 'datasheet': 'a b',
                'fp_filters': ''}
    findings = klc_checks.check_all(metadata, 'My Board')
    fixed = [f for f in findings if f.fix]
    assert fixed and all(f.code.endswith('_name_spaces') for f in fixed)
    assert all(f.fix in klc_checks.FIX_IDS for f in fixed)
    assert any(not f.fix for f in findings)
