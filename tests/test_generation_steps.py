"""@brief Which program works in each generation step, shown step by step.

User request (2026-09-26): «voglio vedere bene quando entra in azione FreeCAD o
KiCad». The footprint is written by this program, the model choice reads the
KiCad 3D library files and measures candidates with FreeCAD only when they are
not cached, the solid is built by FreeCAD.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtTest import QSignalSpy

from kicadgen.cad3d import kicad_models
from kicadgen.gui.analysis_overlay import TOOLS
from tests.test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa


def test_freecad_measuring_is_announced_only_when_freecad_really_runs(monkeypatch, tmp_path):
    """@brief Positivo: un lotto da misurare avvia FreeCAD e lo dice, prima e dopo.

    Negativo: nessun file STEP da misurare, nessun FreeCAD, nessun annuncio.
    """
    calls = []
    monkeypatch.setattr("kicadgen.cad3d.step_generator.find_freecadcmd", lambda: "freecadcmd")
    monkeypatch.setattr(kicad_models, "_measure_batch",
                        lambda freecad, paths, timeout, cancel: ("done", {p: None for p in paths}, None))
    model = tmp_path / "part.step"
    with kicad_models.measuring_listener(lambda started, count: calls.append((started, count))):
        kicad_models.measure_models([model])
        assert calls == [(True, 1), (False, 1)]
        calls.clear()
        kicad_models.measure_models([tmp_path / "mesh.wrl"])
    assert calls == []


def test_a_failing_listener_never_stops_the_measurement(monkeypatch, tmp_path):
    monkeypatch.setattr("kicadgen.cad3d.step_generator.find_freecadcmd", lambda: "freecadcmd")
    monkeypatch.setattr(kicad_models, "_measure_batch",
                        lambda freecad, paths, timeout, cancel: ("done", {p: {"x": 1} for p in paths}, None))

    def broken(started, count):
        raise RuntimeError("modale rotta")

    with kicad_models.measuring_listener(broken):
        assert kicad_models.measure_models([tmp_path / "part.step"]) == {
            str(tmp_path / "part.step"): {"x": 1}}


def test_the_worker_shows_freecad_while_it_measures_then_the_kicad_library():
    """@brief Il passo 2 mostra FreeCAD solo per la durata della misura."""
    worker = gui.GenerateWorker.__new__(gui.GenerateWorker)
    gui.QThread.__init__(worker)
    spy = QSignalSpy(worker.phase)
    gui._announce_freecad_measuring(worker, True, 3)
    gui._announce_freecad_measuring(worker, False, 3)
    assert [spy[0][0], spy[0][1]] == ["freecad_measure", "misura di 3 modelli candidati"]
    assert spy[1][0] == "kicad"
    assert TOOLS["freecad_measure"][0].startswith("FreeCAD")
    assert TOOLS["freecad_measure"][1] == TOOLS["freecad"][1], "stesso colore: e' FreeCAD"
    assert TOOLS["freecad_measure"][2] != TOOLS["freecad"][2], "ma misura, non costruisce"


def test_each_phase_moves_the_step_row(window):  # noqa: F811
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello 3D")
    window._show_generation_steps(object(), body=object())
    assert "1 · Footprint 2D (KiCadGen)" in overlay.steps_plain_text()
    window._on_phase("footprint", "scrittura del file .kicad_mod", 0, 0)
    assert overlay._step_current == 0
    window._on_phase("freecad_measure", "misura di 2 modelli candidati", 0, 0)
    assert overlay._step_current == 1
    assert overlay.tool.text().startswith("FreeCAD (misura dei modelli)")
    window._on_phase("freecad", "costruzione del solido 3D", 1, 4)
    assert overlay._step_current == 2
    assert "\u2713 1" in overlay.steps_plain_text() and "\u2713 2" in overlay.steps_plain_text()
    overlay.stop()


def test_without_freecad_steps_two_and_three_say_why(window):  # noqa: F811
    """@brief Negativo: senza FreeCAD i passi 2 e 3 non spariscono in silenzio."""
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello 3D")
    window._freecad_available = False
    window._show_generation_steps(object(), body=None)
    text = overlay.steps_plain_text()
    assert text.count("FreeCAD non installato") == 2
    assert "1 · Footprint 2D (KiCadGen)" in text
    overlay.stop()


def test_a_single_phase_operation_has_no_step_row(window):  # noqa: F811
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello 3D")
    window._show_generation_steps(object(), body=object())
    overlay.start("Analisi AI in corso")
    assert not overlay.steps.isVisibleTo(overlay)
    window._on_phase("freecad", "costruzione del solido 3D", 0, 0)
    assert not overlay.steps.isVisibleTo(overlay)
    overlay.stop()


def test_a_finished_job_keeps_the_card_until_chiudi(window):  # noqa: F811
    """@brief Positivo: a lavoro riuscito tutti i passi spuntati, niente «Interrompi», chiusura solo al clic."""
    from PyQt6.QtTest import QSignalSpy
    overlay = window.analysis_overlay
    overlay.start("Generazione modello 3D", abortable=True)
    overlay.set_steps(["Modelli 3D dalla libreria KiCad", "Solido 3D con FreeCAD"])
    overlay.set_current_step(1)
    closed = QSignalSpy(overlay.closed)
    window._hold_completed_overlay("Completato: modello 3D rigenerato.")
    assert overlay.is_holding_completed()
    text = overlay.steps_plain_text()
    assert "✓ 1" in text and "✓ 2" in text, "anche il passo FreeCAD e' spuntato"
    assert not overlay.is_abortable() and not overlay.timer.isActive()
    assert "Completato" in overlay.status.text()
    overlay.close_button.click()
    assert overlay.isHidden() and len(closed) == 1
    # Una nuova operazione non riparte con il pulsante «Chiudi» a vista.
    overlay.start("Analisi AI in corso")
    assert overlay.close_button.isHidden()
    overlay.stop()


def test_a_single_phase_operation_is_not_held(window):  # noqa: F811
    """@brief Negativo: senza passi non c'e' nulla da mostrare spuntato, la scheda si chiude."""
    overlay = window.analysis_overlay
    overlay.start("Analisi AI in corso")
    window._hold_completed_overlay("Completato: footprint generato.")
    assert overlay.isHidden()


def test_the_steps_are_listed_one_under_the_other(window):  # noqa: F811
    """@brief Utente 26/09: un passo per riga, nell'ordine, senza frecce fra l'uno e l'altro."""
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello 3D")
    overlay.set_steps(["Footprint 2D (KiCadGen)", "Modelli 3D dalla libreria KiCad",
                       "Solido 3D con FreeCAD"])
    overlay.set_current_step(1)
    lines = [line for line in overlay.steps_plain_text().splitlines() if line.strip()]
    assert len(lines) == 3
    assert lines[0].startswith("✓ 1") and lines[1].startswith("● 2") and lines[2].startswith("○ 3")
    assert "›" not in overlay.steps_plain_text()
    assert overlay.steps.minimumHeight() >= 3 * overlay.steps.fontMetrics().lineSpacing()
    overlay.stop()
