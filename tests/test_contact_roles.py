"""@brief The shared switch-role words of pin names (kicadgen/core/contact_roles.py).

One rule for the extraction (names follow an answered role) and the
component check (a name contradicting its function is a finding). Positive
and negative cases; the vocabulary limit is declared in the module.
"""
import pytest

from kicadgen.core.contact_roles import renamed_role_word, role_conflicts, role_word_of


@pytest.mark.parametrize("name, read", [
    ("NC", ("nc", "")), ("n.o.", ("no", "")), ("Common", ("common", "")),
    ("NC1", ("nc", "1")), ("NO 2", ("no", " 2")), ("COM-1", ("common", "-1")),
    ("NA", ("no", "")), ("NF", ("nc", "")), ("Normally Closed", ("nc", ""))])
def test_role_words_with_or_without_a_contact_number_are_read(name, read):
    assert role_word_of(name) == read


@pytest.mark.parametrize("name", ["A1", "OUT_A", "GPIO19", "Coil", "N1C", "", None, "NC/NO"])
def test_other_names_are_no_role_word(name):
    assert role_word_of(name) is None


@pytest.mark.parametrize("name, role, renamed", [
    ("NC", "no", "NO"), ("N.C.", "no", "N.O."), ("nc1", "no", "no1"), ("NO 2", "nc", "NC 2"),
    ("NA", "nc", "NC"), ("NF", "no", "NO"), ("Normally Closed", "no", "Normally Open"),
    ("COM-1", "no", "NO-1"), ("com", "nc", "nc")])
def test_a_rename_keeps_style_number_and_family(name, role, renamed):
    assert renamed_role_word(name, role) == renamed


def test_a_rename_leaves_other_names_and_unchanged_roles():
    assert renamed_role_word("A1", "nc") == "A1"
    assert renamed_role_word("Common", "common") == "Common"


def test_conflicts_are_found_with_contact_numbers_and_only_for_switch_roles():
    entries = [{"pin": 2, "name": "NC1", "function": "no"},
               {"pin": 3, "name": "NO1", "function": "no"},
               {"pin": 4, "name": "COM", "function": "coil"},
               {"pin": 5, "name": "OUT", "function": "nc"}]
    assert role_conflicts(entries) == [{"pin": "2", "name": "NC1", "function": "no"}]
