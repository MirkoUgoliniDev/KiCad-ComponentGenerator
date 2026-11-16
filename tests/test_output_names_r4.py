"""@brief Output names refuse the characters KiCad mis-handles in footprint and symbol names (review 2026-11-15, round 4, item 2).

@details kicad-cli 10 passes a footprint or symbol NAME to the C runtime's
printf family as a format: «x%NOPE%y», «x%n», «x%N» end with 0xC0000409,
«x%Zy», «x%Sy» with 0xC0000005, on every run (scratch probes of
2026-11-15). Which «%» sequences crash is undefined behaviour of the
runtime, so the whole character is refused by the shared output-name rule,
with its own message; «$», «{», «}» read in every probe and stay accepted.
Positive and negative twins.
"""

import pytest

from kicadgen.cad3d.output_names import is_valid_output_name as cad_is_valid
from kicadgen.core.output_names import (KICAD_UNSAFE_NAME_CHARACTERS, is_valid_output_name,
                                        kicad_unsafe_characters, validate_output_name)


@pytest.mark.parametrize('name', ['Relay_x%NOPE%y', 'x%TEMP%y', 'x%', '%x', '50%', 'a%%b'])
def test_a_percent_is_refused_with_its_own_message(name):
    """@brief Positive: every name holding «%» -- a crashing sequence or one that happened to read
    -- is refused, with a message naming «%» (not «not a valid file name», which it is)."""
    assert not is_valid_output_name(name) and not cad_is_valid(name)
    assert kicad_unsafe_characters(name) == '%'
    with pytest.raises(ValueError, match='%') as caught:
        validate_output_name(name)
    assert 'nome di file valido' not in str(caught.value)


@pytest.mark.parametrize('name', ['Relay_x_NOPE_y', 'x${NOPE}y', 'x{y}', 'Cost$5', 'ESP32.module'])
def test_names_kicad_reads_stay_accepted(name):
    """@brief Negative twin: «$», braces and dots, which kicad-cli 10 read in every probe."""
    assert is_valid_output_name(name) and kicad_unsafe_characters(name) == ''
    validate_output_name(name)


@pytest.mark.parametrize('name', ['a/b%', 'x%.', '', '..'])
def test_a_name_wrong_for_other_reasons_keeps_the_generic_message(name):
    """@brief Negative: a name that is not a plain file name whatever its «%» keeps the generic
    message (the specific one would hide the separator or the trailing dot)."""
    with pytest.raises(ValueError, match='nome di file valido'):
        validate_output_name(name)


def test_the_unsafe_set_is_declared_and_non_string_names_are_safe():
    """@brief The rule reads one declared constant; a non-string name has no unsafe characters
    (and is refused by the file-name rule on its own)."""
    assert KICAD_UNSAFE_NAME_CHARACTERS == '%'
    assert kicad_unsafe_characters(None) == '' and not is_valid_output_name(None)
