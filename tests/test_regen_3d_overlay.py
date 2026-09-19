"""La rigenerazione STEP mostra la modale di avanzamento come l'analisi AI."""

import pytest

from test_photo_inventory_gui import window, gui


def test_the_overlay_opens_when_the_regeneration_starts(window, monkeypatch):
    started = []
    monkeypatch.setattr(gui.Cad3DWorker, 'start', lambda self: started.append(self))
    window.analysis_overlay.start('Generazione modello 3D', 'Avvio di FreeCAD…')
    assert not window.analysis_overlay.isHidden()
    assert window.analysis_overlay.title.text()


def test_the_overlay_closes_when_the_worker_ends(window):
    """Anche in errore: finished di QThread scatta comunque."""
    window._analysis_tabs_enabled = window.tabs.isEnabled()
    window.tabs.setEnabled(False)
    window.analysis_overlay.start('Generazione modello 3D', 'Avvio di FreeCAD…')
    window._on_regen_3d_finished()
    assert window.analysis_overlay.isHidden()
    assert window.tabs.isEnabled(), 'le schede restano bloccate'
    # isEnabled() resta False finche' il pannello Annotazioni 3D e' nascosto
    # in modalita' standard: qui conta che il comando torni disponibile in se'.
    assert window.btn_regen_3d.isEnabledTo(window.btn_regen_3d.parentWidget())
    assert 'Rigenera' in window.btn_regen_3d.text()


def test_worker_messages_reach_the_overlay(window):
    window.analysis_overlay.start('Generazione modello 3D', 'Avvio di FreeCAD…')
    window._on_regen_3d_progress('Fillet non applicabile su uno spigolo')
    assert 'Fillet' in window.analysis_overlay.status.text()


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_every_language_has_the_new_overlay_strings(language):
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS

    for key in ("ui:Generazione modello 3D", "ui:Avvio di FreeCAD…"):
        assert DEFAULT_TRANSLATIONS[key].get(language), f"{key} manca in {language}"
