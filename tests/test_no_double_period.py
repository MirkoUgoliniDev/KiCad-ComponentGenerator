"""@brief A message never prints «..» after a value that already ends a sentence (HANDOFF point 133).

The class of defect: a template writes «{exc}.» and the exception text ends
with its own period («Numero pad duplicato tra lati: 2.»), so the log read
«2.. Richiesta correzione». Fixed once in `tx` for every template and every
language, and by `end_sentence` for texts built without `tx`.
"""
import pytest

from kicadgen.services import messages
from kicadgen.services.messages import end_sentence, ends_sentence, tx


@pytest.mark.parametrize("value", ["duplicato: 2.", "fine!", "davvero?", "attendi…", "spazio. "])
def test_a_value_that_ends_a_sentence_drops_the_template_period(value):
    assert tx("Errore: {exc}. Poi.", exc=value) == f"Errore: {value} Poi."


@pytest.mark.parametrize("value", ["duplicato: 2", "versione 1.2 caricata", ""])
def test_a_value_without_a_sentence_end_keeps_the_template_period(value):
    """Negative: a period inside the value, or none at all, is not a sentence end."""
    assert tx("Errore: {exc}. Poi.", exc=value) == f"Errore: {value}. Poi."


def test_literal_braces_and_format_specs_are_left_alone():
    assert tx("Testo {{exc}}. Poi.", exc="x.") == "Testo {exc}. Poi."
    assert tx("Passo {value:.2f}. Poi.", value=2.0) == "Passo 2.00. Poi."


def test_the_translated_template_is_corrected_too(monkeypatch):
    """Every language: the rule looks at the template actually used."""
    monkeypatch.setattr(messages, "_translator",
                        lambda key, default: "Inconsistent numbering: {exc}. Correction requested.")
    assert (tx("Numerazione incoerente: {exc}. Richiesta correzione.", exc="Pad 2.")
            == "Inconsistent numbering: Pad 2. Correction requested.")


def test_the_real_numbering_message_has_no_double_period():
    text = tx("Numerazione contatti incoerente: {exc}. Richiesta correzione prima "
              "dell'analisi foto.", exc=tx("Numero pad duplicato tra lati: {min}.", min=2))
    assert ".." not in text


def test_end_sentence_adds_exactly_one_period():
    assert end_sentence(ValueError("pad 2.")) == "pad 2."
    assert end_sentence("pad 2") == "pad 2."
    assert ends_sentence("ok?") and not ends_sentence("v1.2 ok")
