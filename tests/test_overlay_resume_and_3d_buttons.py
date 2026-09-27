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
    """@brief Le copie nel tab Modello 3D esistono, seguono lo stato e chiamano gli stessi comandi.

    Contratto aggiornato di proposito (scelta dell'utente, 2026-09-23, nota 6):
    nella scheda Modello 3D le copie sono icone senza testo. Prima si
    confrontava `text()` delle due copie; ora il testo dell'originale e' il
    NOME ACCESSIBILE della copia e la prima riga del suo tooltip, ed e' li'
    che si verifica che si tratti dello stesso comando.
    """
    for mirror, source in ((window.btn_reopen_review_3d, window.btn_reopen_review),
                           (window.btn_relaunch_inventory_3d, window.btn_relaunch_inventory)):
        assert mirror.text() == "", "copia a sola icona"
        assert not mirror.icon().isNull()
        assert mirror.accessibleName() == source.text()
        assert mirror.toolTip().splitlines()[0] == source.text()
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
    """@brief Il clic sulle copie del tab 3D arriva ai VERI gestori collegati.

    CONTRATTO CORRETTO il 2026-09-25: la versione precedente scollegava il
    segnale e lo ricollegava a mano al sensore, quindi passava anche con il
    pulsante collegato al comando sbagliato o a nessuno. Ora il collegamento
    resta quello creato dalla finestra; si osserva il gestore vero attraverso
    la prima cosa che fa, la guardia `_background_work_running()` (letta per
    attributo a ogni chiamata, quindi raggiungibile da monkeypatch). La
    guardia risponde «occupato», cosi' il gestore esce subito: nessuna
    revisione aperta, nessuna spesa. Il nome del chiamante dice QUALE gestore
    e' partito.
    """
    import sys
    calls = []

    def busy():
        calls.append(sys._getframe(1).f_code.co_name)
        return True

    monkeypatch.setattr(window, "_background_work_running", busy)
    window._set_review_buttons_enabled(True)
    page = window.btn_reopen_review_3d
    while page.parentWidget() is not None and page.parentWidget() is not window.tabs:
        page = page.parentWidget()
        page.setEnabled(True)  # come dopo una generazione: il tab 3D e' attivo
    for button in (window.btn_reopen_review_3d, window.btn_relaunch_inventory_3d):
        assert button.receivers(button.clicked) == 1, "un comando, un collegamento"
    window.btn_reopen_review_3d.click()
    window.btn_relaunch_inventory_3d.click()
    assert calls == ["_reopen_detector_review", "_relaunch_inventory"]
    # Negativo: le copie dello stesso comando nella scheda Progetto arrivano
    # agli stessi gestori, non ad altri (i due comandi non sono scambiati).
    calls.clear()
    window.btn_reopen_review.click()
    window.btn_relaunch_inventory.click()
    assert calls == ["_reopen_detector_review", "_relaunch_inventory"]


def test_the_buttons_sit_in_the_top_bar_of_the_3d_tab(window):  # noqa: F811
    """@brief Nella stessa barra di «Rigenera modello 3D», che ora sta in cima alla scheda.

    Contratto aggiornato di proposito (scelta dell'utente, 2026-09-23, nota 6):
    la riga in basso non esiste piu', i comandi sono icone nella barra unica
    sopra il modello. Resta il contratto di fondo -- le copie stanno accanto a
    «Rigenera» -- e si aggiunge dove sta la barra: sopra l'anteprima.
    """
    bar = window.btn_regen_3d.parentWidget()
    assert bar is window.model_3d_action_bar
    row = bar.layout()

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
    page = window.model_3d_page.layout()
    widgets = [page.itemAt(i).widget() for i in range(page.count())]
    splitter = next(w for w in widgets if w is not bar and w.isAncestorOf(window.preview_3d))
    assert widgets.index(bar) < widgets.index(splitter), "la barra sta sopra il modello"
