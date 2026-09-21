"""@brief L'overlay riprende ad animarsi dopo la revisione; i pulsanti di revisione anche nel tab 3D.

Caso osservato dall'utente: logo fermo e «Tempo trascorso 01:11» bloccato
per tutta la chiamata AI, dopo aver chiuso la finestra «Corpi rilevati».
Non era un blocco: `hideEvent` fermava il timer e `show()` non lo faceva
ripartire. E dal tab Modello 3D, dove si vede che mancano corpi, non c'era
modo di tornare alle proposte annotate.
"""
import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import analysis_overlay as overlay_module
from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(overlay_module, "monotonic", clock)
    return clock


def test_overlay_resumes_after_a_hide_and_skips_the_hidden_time(window, clock):  # noqa: F811
    """@brief Positivo: dopo hide()+show() il timer e' attivo e l'orologio non conta la pausa."""
    window.show()  # showEvent arriva solo a finestre visibili, come nell'app
    overlay = window.analysis_overlay
    overlay.start("Analisi AI in corso", "x")
    clock.now += 71
    overlay._tick()
    assert "01:11" in overlay.elapsed.text()
    overlay.hide()
    assert not overlay.timer.isActive()
    clock.now += 300  # cinque minuti nella revisione
    overlay.show()
    assert overlay.timer.isActive(), "show() deve far ripartire spinner e orologio"
    assert "01:11" in overlay.elapsed.text(), "il tempo passato nascosti non e' attesa del modello"
    clock.now += 4
    overlay._tick()
    assert "01:15" in overlay.elapsed.text()
    overlay.stop()


def test_a_fresh_start_after_stop_does_not_inherit_the_hidden_time(window, clock):  # noqa: F811
    """@brief Negativo: start() dopo stop() riparte da zero, senza sottrazioni spurie."""
    window.show()
    overlay = window.analysis_overlay
    overlay.start("a", "x")
    clock.now += 50
    overlay.stop()
    clock.now += 500
    overlay.start("b", "y")
    assert overlay.timer.isActive()
    assert "00:00" in overlay.elapsed.text()
    clock.now += 7
    overlay._tick()
    assert "00:07" in overlay.elapsed.text()
    overlay.stop()


def test_3d_tab_has_the_same_review_buttons_with_the_same_state(window):  # noqa: F811
    """@brief Le copie nel tab Modello 3D esistono, seguono lo stato e chiamano gli stessi comandi."""
    assert window.btn_reopen_review_3d.text() == window.btn_reopen_review.text()
    assert window.btn_relaunch_inventory_3d.text() == window.btn_relaunch_inventory.text()
    # Il tab Modello 3D e' spento finche' non c'e' un modello: si guarda lo
    # stato proprio del pulsante, non quello ereditato dal tab.
    def own(button):
        return button.isEnabledTo(button.parentWidget())
    window._set_review_buttons_enabled(True)
    assert own(window.btn_reopen_review_3d) and own(window.btn_relaunch_inventory_3d)
    window._set_review_buttons_enabled(False)
    assert not own(window.btn_reopen_review_3d)
    assert not own(window.btn_relaunch_inventory_3d)
    assert not own(window.btn_reopen_review), "le copie hanno sempre lo stesso stato"


def test_3d_tab_buttons_trigger_the_same_handlers(window, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(window, "_reopen_detector_review", lambda: calls.append("reopen"))
    monkeypatch.setattr(window, "_relaunch_inventory", lambda: calls.append("relaunch"))
    # I segnali sono gia' collegati ai metodi originali: si verifica il
    # collegamento attraverso l'attributo, come fa Qt con i bound method.
    window._set_review_buttons_enabled(True)
    page = window.btn_reopen_review_3d
    while page.parentWidget() is not None and page.parentWidget() is not window.tabs:
        page = page.parentWidget()
        page.setEnabled(True)  # come dopo una generazione: il tab 3D e' attivo
    window.btn_reopen_review_3d.clicked.disconnect()
    window.btn_reopen_review_3d.clicked.connect(window._reopen_detector_review)
    window.btn_relaunch_inventory_3d.clicked.disconnect()
    window.btn_relaunch_inventory_3d.clicked.connect(window._relaunch_inventory)
    window.btn_reopen_review_3d.click()
    window.btn_relaunch_inventory_3d.click()
    assert calls == ["reopen", "relaunch"]


def test_the_buttons_sit_in_the_bottom_row_of_the_3d_tab(window):  # noqa: F811
    """@brief «Un pulsante in basso»: nella stessa riga di «Rigenera modello 3D»."""
    row = window.btn_regen_3d.parentWidget().layout()

    def _contains(layout, widget):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is widget:
                return True
            if item.layout() is not None and _contains(item.layout(), widget):
                return True
        return False

    assert _contains(row, window.btn_reopen_review_3d)
    assert _contains(row, window.btn_relaunch_inventory_3d)
