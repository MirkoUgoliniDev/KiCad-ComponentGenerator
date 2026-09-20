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


def test_the_cost_also_appears_inside_the_progress_modal(window):
    """Durante la generazione la modale copre la label sotto il pulsante.

    Il costo era gia' calcolato al momento giusto ma finiva dietro la modale,
    e sembrava non esserci.
    """
    openrouter._record_usage(response(0.0417, prompt=14820, completion=530))
    window.analysis_overlay.start('Analisi AI in corso', 'in corso…')
    window._on_extract_finished()
    assert '0.0417' in window.analysis_overlay.cost.text()
    assert not window.analysis_overlay.cost.isHidden()


def test_the_progress_messages_do_not_overwrite_the_cost(window):
    """Sta in una riga propria: lo stato scorre, il consuntivo resta."""
    openrouter._record_usage(response(0.0417, prompt=100, completion=10))
    window.analysis_overlay.start('Analisi AI in corso', 'in corso…')
    window._on_extract_finished()
    window.analysis_overlay.set_status('Generazione STEP tramite FreeCAD…')
    assert '0.0417' in window.analysis_overlay.cost.text()


def test_the_cost_survives_the_modal_restarting_for_the_freecad_phase(window):
    """La modale riparte per la generazione, che non fa chiamate al modello.

    Azzerare il consuntivo in start() lo faceva sparire una frazione di
    secondo dopo averlo mostrato.
    """
    openrouter._record_usage(response(0.05, prompt=10))
    window.analysis_overlay.start('Analisi AI in corso', 'in corso…')
    window._on_extract_finished()
    window.analysis_overlay.start('Generazione modello 3D', 'Avvio di FreeCAD…')
    assert '0.05' in window.analysis_overlay.cost.text()


def test_a_new_analysis_clears_the_previous_cost(window):
    """Il consuntivo della corsa precedente non deve restare appeso."""
    openrouter._record_usage(response(0.05, prompt=10))
    window.analysis_overlay.start('Analisi AI in corso', 'in corso…')
    window._on_extract_finished()
    window.analysis_overlay.clear_cost()
    assert window.analysis_overlay.cost.text() == ''
    assert window.analysis_overlay.cost.isHidden()


def test_a_manual_regeneration_shows_no_cost_in_its_modal(window, monkeypatch):
    """"Rigenera modello 3D" non chiama il modello: mostrare li' il costo
    dell'ultima analisi lo fa leggere come il costo di adesso.

    Resta invece sulla label sotto il pulsante, che dice "ultima analisi"
    ed e' quindi un dato storico, non l'esito di questa operazione.
    """
    from kicadgen.services.datasheet_extractor import ExtractedParams

    params = ExtractedParams(
        name='Modulo', description='', tags='', confidence='medium', notes='',
        layout='castellated', body_length_mm=40, body_width_mm=20, body_height_mm=2.0,
        castellated_sides=[dict(edge='left', count=4, pitch_mm=2.54, first_pin=1)])
    window._on_extracted(params)
    window.group_3d.setChecked(True)
    monkeypatch.setattr(gui.Cad3DWorker, 'start', lambda self: None)

    openrouter._record_usage(response(0.05, prompt=10))
    window.analysis_overlay.start('Analisi AI in corso', 'in corso…')
    window._on_extract_finished()
    assert '0.05' in window.analysis_overlay.cost.text(), 'premessa: il costo era mostrato'

    window._regenerate_3d()
    assert window._cad3d_worker is not None, window.log_text.toPlainText()
    assert window.analysis_overlay.cost.text() == ''
    assert window.analysis_overlay.cost.isHidden()
    assert '0.05' in window.lbl_ai_cost.text(), 'lo storico non va perso'
