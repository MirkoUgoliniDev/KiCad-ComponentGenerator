"""@brief Ingresso nella scheda Modello 3D: indicatore di caricamento e rendering differito.

Nota 10 dell'utente (2026-09-23): entrando nella scheda con una mesh grande
(13 MB di JSON sulla sua scheda) la finestra restava ferma per secondi senza
alcun segno. Il contratto:

- se il visore mostra gia' quella mesh (`is_showing`), nessun lavoro pesante;
- altrimenti PRIMA compare l'indicatore, POI -- a evento successivo -- si
  disegna, e l'indicatore sparisce in ogni esito, anche in errore;
- la chiamata differita non disegna dati diventati vecchi nel frattempo, ne'
  una scheda che l'utente ha gia' lasciato;
- due ingressi ravvicinati non producono due rendering.

Il visore dei test (`PreviewStub`) non ha `is_showing`: e' proprio il caso
«visore che non sa dirlo», che deve funzionare come prima.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QApplication

from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


MODEL_TAB = 4


@pytest.fixture
def mesh(window, tmp_path):  # noqa: F811
    """@brief Una mesh generata sui dati correnti, con il visore che registra i rendering."""
    path = tmp_path / "board.preview.json"
    path.write_text("{}", encoding="utf-8")
    # Stato che lascia un'estrazione vera (2026-09-25): le schede di anteprima
    # ora seguono i dati a ogni cambio di scheda, non la sola chiamata interna.
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window._last_mesh_path = path
    window.rendered = []
    window.preview_3d.render_mesh = window.rendered.append
    yield path
    # Il cursore d'attesa e' globale all'applicazione: un test fallito a meta'
    # non deve lasciarlo ai successivi.
    while QApplication.overrideCursor() is not None:
        QApplication.restoreOverrideCursor()


def _indicator_shown(window):  # noqa: F811
    return window.loading_3d_indicator.isVisibleTo(window.model_3d_action_bar)


def _flush():
    for _ in range(5):
        QApplication.processEvents()


def test_the_indicator_appears_before_the_render_and_goes_after(window, mesh):  # noqa: F811
    """@brief Positivo: indicatore e cursore d'attesa subito, rendering solo a evento successivo."""
    window.tabs.setCurrentIndex(MODEL_TAB)
    assert _indicator_shown(window), "l'indicatore deve precedere il lavoro pesante"
    assert QApplication.overrideCursor() is not None
    assert window.rendered == [], "il rendering e' differito: prima si dipinge la scheda"
    _flush()
    assert window.rendered == [mesh]
    assert not _indicator_shown(window)
    assert QApplication.overrideCursor() is None


def test_the_indicator_goes_away_even_when_the_render_fails(window, mesh):  # noqa: F811
    """@brief Negativo sull'esito: un guasto nel disegno non lascia l'indicatore acceso."""
    def broken():
        raise RuntimeError("VTK non disponibile")

    window.tabs.setCurrentIndex(MODEL_TAB)
    assert _indicator_shown(window)
    window._draw_preview_3d = broken
    # Chiamata diretta: un'eccezione che esce da uno slot Qt chiuderebbe il
    # processo dei test invece di arrivare a pytest.
    with pytest.raises(RuntimeError):
        window._run_pending_3d_render()
    assert not _indicator_shown(window)
    assert QApplication.overrideCursor() is None
    assert window._rendering_3d is False


def test_a_render_error_inside_the_viewer_is_logged_and_hides_the_indicator(window, mesh):  # noqa: F811
    """@brief Il guasto del visore resta quello di sempre (log), e l'indicatore sparisce."""
    def broken(path):
        raise RuntimeError("mesh illeggibile")

    window.preview_3d.render_mesh = broken
    window.tabs.setCurrentIndex(MODEL_TAB)
    _flush()
    assert "Anteprima mesh non disponibile" in window.log_text.toPlainText()
    assert not _indicator_shown(window)
    assert QApplication.overrideCursor() is None


def test_no_indicator_and_no_render_when_the_mesh_is_already_showing(window, mesh):  # noqa: F811
    """@brief Negativo: il visore dice che quella mesh e' gia' a video, quindi nulla da fare."""
    asked = []
    window.preview_3d.is_showing = lambda path: asked.append(path) or True
    window.tabs.setCurrentIndex(MODEL_TAB)
    assert asked == [mesh]
    assert not _indicator_shown(window)
    assert window._pending_3d_render is None
    _flush()
    assert window.rendered == []


def test_a_viewer_that_says_not_showing_gets_the_render(window, mesh):  # noqa: F811
    """@brief Positivo gemello del precedente: `is_showing` falso porta al rendering."""
    window.preview_3d.is_showing = lambda path: False
    window.tabs.setCurrentIndex(MODEL_TAB)
    assert _indicator_shown(window)
    _flush()
    assert window.rendered == [mesh]


def test_a_failing_is_showing_means_render_it(window, mesh):  # noqa: F811
    """@brief Una risposta che fallisce non e' un «si'»: si disegna, come senza la funzione."""
    def failing(path):
        raise OSError("stat fallito")

    window.preview_3d.is_showing = failing
    window.tabs.setCurrentIndex(MODEL_TAB)
    _flush()
    assert window.rendered == [mesh]


def test_the_deferred_render_is_skipped_when_the_data_changed(window, mesh):  # noqa: F811
    """@brief Negativo: fra il clic e il rendering i dati sono cambiati (revisione nuova)."""
    window.tabs.setCurrentIndex(MODEL_TAB)
    assert _indicator_shown(window)
    window._model_revision += 1
    _flush()
    assert window.rendered == []
    assert not _indicator_shown(window)
    assert QApplication.overrideCursor() is None


def test_the_deferred_render_is_skipped_when_the_mesh_was_invalidated(window, mesh):  # noqa: F811
    """@brief Negativo: la mesh e' stata tolta prima che il rendering partisse."""
    window.tabs.setCurrentIndex(MODEL_TAB)
    window._last_mesh_path = None
    _flush()
    assert window.rendered == []
    assert not _indicator_shown(window)


def test_leaving_the_tab_skips_the_render_and_coming_back_does_it(window, mesh):  # noqa: F811
    """@brief Negativo: scheda lasciata prima del rendering; positivo: al ritorno si disegna."""
    window.tabs.setCurrentIndex(MODEL_TAB)
    window.tabs.setCurrentIndex(0)
    _flush()
    assert window.rendered == []
    assert not _indicator_shown(window)
    window.tabs.setCurrentIndex(MODEL_TAB)
    _flush()
    assert window.rendered == [mesh]


def test_two_quick_entries_render_once(window, mesh):  # noqa: F811
    """@brief Rientro: un secondo ingresso prima del rendering non ne accoda un altro."""
    window.tabs.setCurrentIndex(MODEL_TAB)
    window._on_tab_changed(MODEL_TAB)
    _flush()
    assert window.rendered == [mesh]


def test_without_a_mesh_the_cheap_preview_stays_synchronous(window, tmp_path):  # noqa: F811
    """@brief Negativo: senza mesh generata non c'e' nulla di pesante, niente indicatore."""
    # Stato che lascia un'estrazione vera (2026-09-25): le schede di anteprima
    # ora seguono i dati a ogni cambio di scheda, non la sola chiamata interna.
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window._last_mesh_path = None
    calls = []
    window._draw_preview_3d = lambda: calls.append("drawn")
    window.tabs.setCurrentIndex(MODEL_TAB)
    assert calls == ["drawn"], "disegnata subito, senza attendere"
    assert not _indicator_shown(window)
    assert window._pending_3d_render is None
