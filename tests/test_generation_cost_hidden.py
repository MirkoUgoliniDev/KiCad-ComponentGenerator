"""@brief La modale della generazione non mostra il costo dell'analisi AI.

Nota 4 dell'utente (2026-09-23), che ROVESCIA la scelta precedente: nella
generazione automatica che segue un'estrazione il consuntivo «Costo ultima
analisi: … USD» restava nella modale «Generazione footprint e modello 3D»,
perche' estrazione e generazione erano considerate un'unica operazione.
L'utente non e' d'accordo: footprint, abbinamento KiCad e FreeCAD non fanno
chiamate al modello, e un costo li' si legge come il prezzo di cio' che sta
succedendo. Il costo resta dove appartiene: nella modale dell'analisi AI e
nella label sotto il pulsante di estrazione.

Nessun test esistente fissava il vecchio comportamento automatico: era
dichiarato solo in un commento di `_regenerate_3d`, aggiornato insieme.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.services import openrouter
from test_automatic_generation import acquired, begin
from test_photo_inventory_gui import gui


@pytest.fixture(autouse=True)
def clean_totals():
    openrouter.reset_usage()
    yield
    openrouter.reset_usage()


def _bill(cost=0.05):
    openrouter._record_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 0,
                                        "cost": cost}})


def test_automatic_generation_modal_has_no_cost(window, monkeypatch):  # noqa: F811
    """@brief Positivo: la generazione automatica parte con la modale senza costo.

    Negativo per lo storico: il Log AI conserva il costo dell'analisi, che e'
    un dato vero e va tenuto (la label sotto il pulsante e' stata tolta).
    """
    begin(window, monkeypatch)
    _bill()
    window._on_extracted(acquired('castellated'))
    window._on_extract_finished()
    assert window._automatic_generation_active
    assert 'Generazione' in window.analysis_overlay.title.text()
    assert window.analysis_overlay.cost.text() == ''
    assert window.analysis_overlay.cost.isHidden()
    # La label sotto il pulsante non c'e' piu' (utente, 2026-09-24): lo
    # storico dell'analisi resta nel Log AI.
    assert '0.05' in _cost_line(window), "lo storico dell'analisi resta"


def test_a_late_extraction_finish_does_not_bring_the_cost_back(window, monkeypatch):  # noqa: F811
    """@brief Negativo sull'ordine dei segnali: `finished` dell'estrazione dopo l'avvio.

    Nella corsa vera `finished` dell'estrazione puo' arrivare quando la
    generazione e' gia' partita (qui: il secondo `_on_extract_finished`).
    Ricalcolare il consuntivo e' giusto per il log; rimetterlo nella modale
    della generazione no.
    """
    begin(window, monkeypatch)
    _bill()
    window._on_extracted(acquired('castellated'))
    window._on_extract_finished()
    window._on_extract_finished()
    assert window.analysis_overlay.cost.text() == ''
    assert window.analysis_overlay.cost.isHidden()
    assert '0.05' in _cost_line(window)


def test_the_analysis_modal_still_shows_the_cost(window):  # noqa: F811
    """@brief Positivo per contrasto: fuori dalla generazione il costo va nella modale."""
    _bill(0.0417)
    window.analysis_overlay.start('Analisi AI in corso', 'in corso…')
    window._on_extract_finished()
    assert '0.0417' in window.analysis_overlay.cost.text()
    assert not window.analysis_overlay.cost.isHidden()


def test_manual_generation_with_a_3d_body_has_no_cost(window, monkeypatch):  # noqa: F811
    """@brief Positivo: anche la generazione manuale con modello 3D parte senza costo."""
    monkeypatch.setattr(gui.GenerateWorker, 'start', lambda self: None)
    window._freecad_available = True
    window._on_extracted(acquired('castellated'))
    window.group_3d.setChecked(True)
    window.analysis_overlay.set_cost('Costo ultima analisi: 0.05 USD')
    window._generate()
    assert window._manual_cad_overlay, window._log_plain_text()
    assert window.analysis_overlay.cost.text() == ''
    assert window.analysis_overlay.cost.isHidden()


def test_generation_phase_is_recognised_only_while_it_runs(window):  # noqa: F811
    """@brief Positivo e negativo del criterio usato dal consuntivo."""
    assert not window._generation_phase_active()
    window._automatic_generation_active = True
    assert window._generation_phase_active()
    window._automatic_generation_active = False
    window._manual_cad_overlay = True
    assert window._generation_phase_active()
    window._manual_cad_overlay = False
    assert not window._generation_phase_active()



def _cost_line(window):  # noqa: F811
    """Riga del consuntivo nel Log AI: il valore va cercato li', non in tutto il
    log dove «0.05» potrebbe stare in una quota in mm (audit 25/09)."""
    prefix = window.i18n.tr("analysis.cost", amount="\x01", calls=0, tokens=0).split("\x01")[0]
    lines = [line for line in window.ai_progress_text.toPlainText().splitlines() if prefix in line]
    return lines[-1] if lines else ""
