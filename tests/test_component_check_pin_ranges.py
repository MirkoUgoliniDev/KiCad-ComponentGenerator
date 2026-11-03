"""@brief Pin ranges of «Verifica componente»: only canonical ASCII numbers join a range.

Review finding 2026-11-03 (O7): «01» collapsed into «1», and «²» passed
`str.isdigit` and then raised in `int`.
"""
import pytest
pytest.importorskip("PyQt6")

from kicadgen.gui.component_check_dialog import ComponentCheckDialog


def test_canonical_numbers_form_ranges():
    """@brief Positive: 1, 2, 3 and 5 read «1–3, 5»."""
    assert ComponentCheckDialog._pin_ranges(["3", "1", "2", "5"]) == "1–3, 5"


def test_padded_and_unicode_digits_are_listed_as_written():
    """@brief Negative: «01» stays apart from «1»; «²» and «١» do not raise."""
    assert ComponentCheckDialog._pin_ranges(["1", "01", "2"]) == "1–2, 01"
    assert ComponentCheckDialog._pin_ranges(["²", "1"]) == "1, ²"
    assert ComponentCheckDialog._pin_ranges(["١", "A1"]) == "A1, ١"
