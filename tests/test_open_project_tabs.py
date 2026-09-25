"""@brief Aprendo un progetto finito si aprono anche le schede di anteprima (utente, 2026-09-25).

Prima «Anteprima FootPrint» e «Modello 3D» si accendevano solo dopo
un'estrazione della sessione: riaprire un progetto gia' generato le lasciava
spente. Criterio sui dati: c'e' qualcosa da mostrare se il progetto ha il suo
footprint generato, o se e' stato estratto in questa sessione.
"""
import pytest

from tests.test_photo_inventory_gui import window, raw_params, gui  # noqa: F401

PREVIEW, MODEL_3D = 3, 4


def _generated(root, name, step=True):
    folder = root / name
    (folder / f"{name}.pretty").mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.pretty" / f"{name}.kicad_mod").write_text("(footprint)")
    if step:
        shapes = folder / f"{name}.3dshapes"
        shapes.mkdir(parents=True, exist_ok=True)
        (shapes / f"{name}.step").write_text("ISO-10303-21;")
        (shapes / f"{name}.preview.json").write_text('{"vertices": [], "faces": []}')
    return folder


def _open(window, name):  # noqa: F811
    window._switch_project(name, window._general_project_state)


def test_opening_a_generated_project_opens_both_preview_tabs(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    folder = _generated(tmp_path, "FINITO")
    _open(window, "FINITO")
    assert window.tabs.isTabEnabled(PREVIEW) and window.tabs.isTabEnabled(MODEL_3D)
    # Il 3D mostra il modello salvato, non una scheda nera.
    assert window._last_mesh_path == folder / "FINITO.3dshapes" / "FINITO.preview.json"


def test_a_project_without_outputs_keeps_them_off(window, tmp_path):  # noqa: F811
    """Negativo: nulla da mostrare, solo valori predefiniti."""
    window.field_library_root.setText(str(tmp_path))
    (tmp_path / "VUOTO").mkdir()
    _open(window, "VUOTO")
    assert not window.tabs.isTabEnabled(PREVIEW) and not window.tabs.isTabEnabled(MODEL_3D)
    assert window._last_mesh_path is None


def test_footprint_without_step_opens_the_previews_but_shows_no_saved_solid(window,  # noqa: F811
                                                                          tmp_path):
    window.field_library_root.setText(str(tmp_path))
    _generated(tmp_path, "SOLO2D", step=False)
    _open(window, "SOLO2D")
    assert window.tabs.isTabEnabled(PREVIEW)
    assert window._last_mesh_path is None


def test_the_extraction_of_the_closed_project_does_not_open_the_next_one(window,  # noqa: F811
                                                                         tmp_path):
    """Negativo: l'estrazione della sessione valeva per il progetto chiuso."""
    window.field_library_root.setText(str(tmp_path))
    window._acquisition_completed = True
    (tmp_path / "ALTRO").mkdir()
    _open(window, "ALTRO")
    assert not window._acquisition_completed
    assert not window.tabs.isTabEnabled(PREVIEW)


def test_the_footprint_preview_draws_the_open_project_without_a_new_generation(window):  # noqa: F811
    """Positivo: nessuna generazione nella sessione, i campi del progetto bastano."""
    window._last_params = None
    params = window._preview_params()
    assert params is not None and params.name == window._component_name()
    window._draw_preview()
    assert window.preview_scene.items()


def test_fields_that_do_not_make_a_footprint_draw_nothing(window, monkeypatch):  # noqa: F811
    """Negativo: parametri non validi -> nessun disegno inventato."""
    window._last_params = None
    window.preview_scene.clear()

    def invalid():
        raise ValueError("pitch mancante")
    monkeypatch.setattr(window, "_current_params", invalid)
    monkeypatch.setattr(window, "_is_castellated", lambda: False)
    assert window._preview_params() is None
    window._draw_preview()
    assert not window.preview_scene.items()


def test_opening_another_project_forgets_the_previous_generation(window, tmp_path):  # noqa: F811
    """Il footprint generato del progetto chiuso non resta nell'anteprima del nuovo."""
    window.field_library_root.setText(str(tmp_path))
    window._last_params = object()
    (tmp_path / "NUOVO").mkdir()
    _open(window, "NUOVO")
    assert window._last_params is None


def test_a_closed_window_no_longer_reacts_to_tab_changes(window, monkeypatch):  # noqa: F811
    """Quando Qt distrugge la finestra, il QTabWidget emette currentChanged: il
    gestore non deve girare su widget distrutti (segfault 139, 2026-09-25).
    Positivo: aperta, un cambio di scheda viene gestito. Negativo: chiusa, no."""
    calls = []
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: calls.append(True))
    window.tabs.currentChanged.emit(0)
    assert calls == [True]
    window.close()
    window.tabs.currentChanged.emit(0)
    assert calls == [True]



# ── Audit 2026-09-25 ────────────────────────────────────────────────────────

def _settle(window):  # noqa: F811
    from PyQt6.QtWidgets import QApplication
    for _ in range(10):
        QApplication.processEvents()


def test_switching_project_on_the_preview_tab_replaces_the_drawing(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    _generated(tmp_path, "PRIMO")
    _generated(tmp_path, "SECONDO")
    _open(window, "PRIMO")
    window.tabs.setCurrentIndex(PREVIEW)
    # Nessun riferimento Python al segno: dopo lo svuotamento della scena un
    # elemento NUOVO (magari un cerchio) puo' nascere allo stesso indirizzo e
    # PyQt restituirebbe il vecchio oggetto «testo» (access violation vista il
    # 25/09). Si cerca un dato comune a ogni QGraphicsItem.
    window.preview_scene.addSimpleText("x").setData(0, "DISEGNO_DEL_PRIMO")
    _open(window, "SECONDO")
    assert window.tabs.currentIndex() == PREVIEW
    assert all(item.data(0) != "DISEGNO_DEL_PRIMO" for item in window.preview_scene.items())


def test_switching_project_on_the_3d_tab_shows_the_new_saved_model(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    _generated(tmp_path, "PRIMO")
    second = _generated(tmp_path, "SECONDO")
    _open(window, "PRIMO")
    window.tabs.setCurrentIndex(MODEL_3D)
    _settle(window)
    rendered = []
    window.preview_3d.render_mesh = rendered.append
    _open(window, "SECONDO")
    _settle(window)
    assert rendered and rendered[-1] == second / "SECONDO.3dshapes" / "SECONDO.preview.json"


def test_a_tab_that_is_no_longer_current_does_no_work(window, tmp_path, monkeypatch):  # noqa: F811
    """Dall'anteprima di un progetto generato a uno vuoto: il 3D non si prepara."""
    window.field_library_root.setText(str(tmp_path))
    _generated(tmp_path, "PIENO")
    (tmp_path / "VUOTO").mkdir()
    _open(window, "PIENO")
    window.tabs.setCurrentIndex(PREVIEW)
    entered = []
    monkeypatch.setattr(window, "_enter_3d_tab", lambda: entered.append(True))
    _open(window, "VUOTO")
    assert window.tabs.currentIndex() == 0 and entered == []


def test_files_deleted_outside_say_so_and_close_the_kicad_tab(window, tmp_path,  # noqa: F811
                                                            monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    window.field_library_root.setText(str(tmp_path))
    folder = _generated(tmp_path, "FINITO")
    _open(window, "FINITO")
    kicad = window.KICAD_TAB_INDEX
    window.tabs.setCurrentIndex(kicad)
    (folder / "FINITO.3dshapes" / "FINITO.step").unlink()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a))
    window._test_in_kicad()
    assert warnings, "il comando deve dire che cosa manca"
    assert not window.tabs.isTabEnabled(kicad) and window.tabs.currentIndex() != kicad


def test_coming_back_to_the_window_rechecks_the_files(window, tmp_path, monkeypatch):  # noqa: F811
    from PyQt6.QtCore import QEvent
    window.field_library_root.setText(str(tmp_path))
    folder = _generated(tmp_path, "FINITO")
    _open(window, "FINITO")
    assert window.tabs.isTabEnabled(window.KICAD_TAB_INDEX)
    (folder / "FINITO.3dshapes" / "FINITO.step").unlink()
    monkeypatch.setattr(window, "isActiveWindow", lambda: True)
    window.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert not window.tabs.isTabEnabled(window.KICAD_TAB_INDEX)


@pytest.mark.parametrize("step_is_older", [True, False])
def test_an_older_step_is_flagged_not_blocked(window, tmp_path, step_is_older):  # noqa: F811
    import os
    window.field_library_root.setText(str(tmp_path))
    folder = _generated(tmp_path, "FINITO")
    footprint = folder / "FINITO.pretty" / "FINITO.kicad_mod"
    step = folder / "FINITO.3dshapes" / "FINITO.step"
    old, new = 1_700_000_000, 1_700_000_100
    os.utime(step, (old, old) if step_is_older else (new, new))
    os.utime(footprint, (new, new) if step_is_older else (old, old))
    _open(window, "FINITO")
    assert window.tabs.isTabEnabled(window.KICAD_TAB_INDEX)  # avviso, mai blocco
    assert ("più vecchio" in window.label_kicad_status.text()) is step_is_older


def test_after_a_real_extraction_the_previews_stay_open_on_tab_changes(window,  # noqa: F811
                                                                      raw_params):
    """Positivo mancante (T3): senza footprint, l'estrazione della sessione basta."""
    window._on_extracted(raw_params)
    assert window.tabs.isTabEnabled(PREVIEW) and window.tabs.isTabEnabled(MODEL_3D)
    window.tabs.setCurrentIndex(0)
    window.tabs.setCurrentIndex(PREVIEW)
    assert window.tabs.isTabEnabled(PREVIEW) and window.tabs.isTabEnabled(MODEL_3D)
