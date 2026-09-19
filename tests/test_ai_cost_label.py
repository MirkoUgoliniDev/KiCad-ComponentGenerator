"""Il consuntivo resta visibile dopo la chiusura della modale di avanzamento."""

import pytest

from test_photo_inventory_gui import window, gui
from kicadgen.services import openrouter


def response(cost=None, prompt=0, completion=0):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        usage["cost"] = cost
    return {"usage": usage}


@pytest.fixture(autouse=True)
def clean_totals():
    openrouter.reset_usage()
    yield
    openrouter.reset_usage()


def test_the_cost_is_hidden_until_an_analysis_has_run(window):
    assert window.lbl_ai_cost.isHidden()


def test_the_cost_appears_when_the_analysis_ends(window):
    openrouter._record_usage(response(0.0417, prompt=14820, completion=530))
    window._on_extract_finished()
    text = window.lbl_ai_cost.text()
    assert not window.lbl_ai_cost.isHidden()
    assert "0.0417" in text and "15350" in text


def test_a_cached_run_says_so_instead_of_showing_zero(window):
    """Zero USD e zero chiamate non sono la stessa informazione."""
    window._on_extract_finished()
    assert "cache" in window.lbl_ai_cost.text().lower()


def test_an_incomplete_total_is_labelled_as_underestimated(window):
    openrouter._record_usage(response(0.02, prompt=10))
    openrouter._record_usage(response(None, prompt=10))
    window._on_extract_finished()
    assert "sottostimato" in window.lbl_ai_cost.text()


def test_small_amounts_keep_enough_digits(window):
    openrouter._record_usage(response(3.6e-06, prompt=16, completion=2))
    window._on_extract_finished()
    assert "0.000004" in window.lbl_ai_cost.text()


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_every_language_has_the_cost_strings(window, language):
    """Memoria di progetto: le traduzioni restano complete su IT/EN/FR/DE."""
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS

    for key in ("analysis.cost", "analysis.cost.cached", "analysis.cost.partial"):
        assert DEFAULT_TRANSLATIONS[key].get(language), f"{key} manca in {language}"
