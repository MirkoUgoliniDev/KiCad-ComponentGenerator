"""La rigenerazione STEP mostra la modale di avanzamento come l'analisi AI."""

import pytest

from test_photo_inventory_gui import gui


def test_the_overlay_opens_when_the_regeneration_starts(window, monkeypatch):
    started = []
    monkeypatch.setattr(gui.Cad3DWorker, 'start', lambda self: started.append(self))
    window.analysis_overlay.start('Generazione modello STEP', 'Avvio di FreeCAD…')
    assert not window.analysis_overlay.isHidden()
    assert window.analysis_overlay.title.text()


def test_the_overlay_closes_when_the_worker_ends(window):
    """Anche in errore: finished di QThread scatta comunque."""
    # CONTRACT CHANGED 2026-10-15 (audit item 4): only DOCUMENTED body sizes build a body;
    # this component stands for one whose analysis declared them.
    window._body_size_declared = True
    window._analysis_tabs_enabled = window.tabs.isEnabled()
    window.tabs.setEnabled(False)
    window.analysis_overlay.start('Generazione modello STEP', 'Avvio di FreeCAD…')
    window._on_regen_3d_finished()
    assert window.analysis_overlay.isHidden()
    assert window.tabs.isEnabled(), 'le schede restano bloccate'
    # isEnabled() resta False finche' il pannello Annotazioni 3D e' nascosto
    # in modalita' standard: qui conta che il comando torni disponibile in se'.
    assert window.btn_regen_3d.isEnabledTo(window.btn_regen_3d.parentWidget())
    # Contratto aggiornato di proposito (scelta dell'utente, 2026-09-23): il
    # comando e' un'icona senza testo, quindi il ritorno allo stato di riposo
    # si legge nel nome accessibile e nel tooltip, non piu' in `text()`.
    assert 'Rigenera' in window.btn_regen_3d.accessibleName()
    assert window.btn_regen_3d.toolTip().startswith('Rigenera')
    assert not window.btn_regen_3d.property('workflow_busy')


def test_the_icon_only_command_shows_it_is_busy_and_then_recovers(window, monkeypatch):
    """Positivo: durante la generazione l'icona dice «in corso»; negativo: a riposo no.

    Con il pulsante a testo lo stato occupato era la scritta «Generazione in
    corso (FreeCAD)...». Un'icona non ha scritta: lo stato deve restare
    visibile in altro modo -- comando spento, icona della clessidra, nome e
    tooltip che dicono «in corso».
    """
    # CONTRACT CHANGED 2026-10-15 (audit item 4): only DOCUMENTED body sizes build a body;
    # this component stands for one whose analysis declared them.
    window._body_size_declared = True
    idle_icon = window.btn_regen_3d.icon().cacheKey()
    window._set_regen_busy(True)
    button = window.btn_regen_3d
    assert not button.isEnabledTo(button.parentWidget())
    assert button.property('workflow_busy')
    assert button.icon().cacheKey() != idle_icon
    assert 'in corso' in button.accessibleName()
    assert button.toolTip().startswith('Generazione in corso')
    window._on_regen_3d_finished()
    assert button.isEnabledTo(button.parentWidget())
    assert 'in corso' not in button.accessibleName()
    assert not button.property('workflow_busy')


def test_worker_messages_reach_the_overlay(window):
    window.analysis_overlay.start('Generazione modello STEP', 'Avvio di FreeCAD…')
    window._on_regen_3d_progress('Fillet non applicabile su uno spigolo')
    assert 'Fillet' in window.analysis_overlay.status.text()


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_every_language_has_the_new_overlay_strings(language):
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS

    for key in ("ui:Generazione modello STEP", "ui:Avvio di FreeCAD…"):
        assert DEFAULT_TRANSLATIONS[key].get(language), f"{key} manca in {language}"
