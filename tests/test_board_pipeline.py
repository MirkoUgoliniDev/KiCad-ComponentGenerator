"""Regression coverage across AI decoding, Qt state and actual CAD payloads."""
from pathlib import Path
import sys

import pytest
from test_photo_inventory_gui import window
from kicadgen.gui import main_window as gui
from kicadgen.services import datasheet_extractor as ext
from kicadgen.cad3d import step_generator as cad, kicad_models
from kicadgen.cad3d.process_log import run_logged


@pytest.mark.parametrize("layout", ["matrix", "castellated"])
def test_package_survives_gui_and_reaches_library_resolution(window, monkeypatch, tmp_path, layout):
    raw = dict(layout=layout, name="Module", body_length_mm=40, body_width_mm=20,
        body_height_mm=5, pcb_thickness_mm=1.5,
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=15,
        pad_diameter_mm=1.7, drill_mm=1,
        castellated_sides=[dict(edge="left", count=4, pitch_mm=2.54)],
        components_3d=[dict(name="R", package="R_0603", kind="resistor", x_mm=2,
            y_mm=-3, length_mm=1.6, width_mm=.8, height_mm=.5,
            bbox_basis="body", package_evidence="two metal ends", mating="none")])
    params = ext._params_from_tool_arguments(raw)
    # Questa prova riguarda il payload CAD, non il rilevamento di FreeCAD:
    # senza FreeCAD l'estrazione lascia spento il gruppo «Modello 3D» (per
    # scelta dichiarata) e non ci sarebbe alcun corpo da misurare.
    window._freecad_available = True
    window._on_extracted(params)
    boxes = window._components_from_table()
    assert boxes[0].package == "R_0603"
    assert boxes[0].bbox_basis == "body"
    captured = {}
    def resolve(components, **kwargs):
        assert components[0]["package"] == "R_0603"
        components[0]["model_path"] = "resolved.step"
        kwargs["log"]("resolved-live")
    monkeypatch.setattr(kicad_models, "attach_models", resolve)
    def build(*args, **kwargs):
        captured.update(kwargs)
        return {"warnings": []}
    monkeypatch.setattr(cad, "_run_freecad", build)
    progress = []
    body = window._current_body_params()
    if layout == "castellated":
        p = window._current_castellated_params()
        cad.generate_castellated_3d_model(p, body, tmp_path, progress=progress.append)
    else:
        p = window._current_params()
        cad.generate_3d_model(p, body, tmp_path, progress=progress.append)
        assert body.pcb_thickness_mm == 1.5
        assert len(captured["notches"]) == 8
    assert captured["extra_payload"]["components"][0]["model_path"] == "resolved.step"
    assert progress == ["resolved-live"]
    assert not captured["extra_payload"].get("silk_texts")


def _castellated_raw(**overrides):
    """@brief Estrazione minima di un modulo castellato, con quote sovrascrivibili."""
    raw = dict(layout="castellated", name="Module", body_length_mm=40, body_width_mm=20,
               pcb_thickness_mm=1.5,
               castellated_sides=[dict(edge="left", count=4, pitch_mm=2.54)])
    raw.update(overrides)
    return ext._params_from_tool_arguments(raw)


def _click_3d_group(window):
    """@brief Inverte il gruppo «Modello 3D» come farebbe l'utente col mouse.

    Serve il clic vero e non `setChecked()`: e' proprio la differenza fra i due
    che la finestra usa per riconoscere una scelta umana.
    """
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    before = window.group_3d.isChecked()
    QTest.mouseClick(window.group_3d, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
    assert window.group_3d.isChecked() is not before, "il clic deve colpire l'indicatore"


def test_castellated_module_has_a_body_even_outside_automatic_generation(window):
    """@brief Il corpo 3D di un modulo castellato segue i dati, non il percorso di avvio.

    Positivo: un'estrazione castellata avviata a mano (`_auto_after_extraction`
    falso, come quando l'utente preme «Estrai» in modalita' avanzata) deve
    lasciare parametri del corpo utilizzabili: per un modulo il corpo *e'* il
    suo PCB, quindi ingombro e spessore bastano a costruirlo.

    Negativo: senza ingombro della scheda non c'e' nulla da costruire e il
    gruppo «Modello 3D» deve restare spento -- altrimenti la generazione
    partirebbe su misure inventate.

    Prima la condizione era `_auto_after_extraction`, cioe' «la generazione
    parte da sola»: un'estrazione castellata fatta a mano lasciava il gruppo
    spento e `_current_body_params()` restituiva None, che piu' a valle
    diventava `AttributeError: 'NoneType' object has no attribute
    'pin_color_hex'` dentro il generatore STEP.
    """
    # Presupposto dichiarato: l'estrazione accende il gruppo solo dove FreeCAD
    # c'e'. Nel contenitore di prova non c'e', e senza questa riga la prova
    # verificherebbe il caso «3D non generabile» invece di quello che vuole.
    window._freecad_available = True
    window.group_3d.setChecked(False)
    window._auto_after_extraction = False
    window._on_extracted(_castellated_raw())
    assert window.group_3d.isChecked()
    body = window._current_body_params()
    assert body is not None
    assert body.length_mm == pytest.approx(40)
    assert body.width_mm == pytest.approx(20)
    # Lo spessore della scheda fa da lunghezza del pin/pad passante del modulo.
    assert body.pin_length_mm == pytest.approx(1.5)
    assert body.pin_color_hex

    window.group_3d.setChecked(False)
    window._on_extracted(_castellated_raw(body_length_mm=None, body_width_mm=None))
    assert not window.group_3d.isChecked(), "senza ingombro non c'e' un solido da costruire"
    assert window._current_body_params() is None


def test_extraction_does_not_revoke_the_3d_choice_made_by_hand(window):
    """@brief Negativo: cio' che l'utente ha spento col clic resta spento.

    Un'estrazione descrive i dati del componente; non e' il permesso di
    rimettere in coda una generazione 3D esclusa apposta. Il clic umano si
    distingue dalle `setChecked()` del programma perche' solo il primo emette
    `QGroupBox.clicked`, che qui si simula con `click()`.

    Positivo (nella stessa prova): se l'utente il gruppo lo accende, una
    successiva estrazione lo lascia acceso.
    """
    window.group_3d.setChecked(True)
    # Un clic sull'indicatore di un QGroupBox spuntabile ne inverte lo stato.
    _click_3d_group(window)
    assert not window.group_3d.isChecked()
    window._on_extracted(_castellated_raw())
    assert not window.group_3d.isChecked()
    assert window._current_body_params() is None

    _click_3d_group(window)
    assert window.group_3d.isChecked()
    window._on_extracted(_castellated_raw())
    assert window.group_3d.isChecked()
    assert window._current_body_params() is not None


def test_all_gui_logs_are_flushed_once(window):
    window._on_ai_progress("analysis-proof")
    window._append_log("generation-proof")
    text = window._log_file().read_text(encoding="utf-8")
    assert text.count("analysis-proof") == text.count("generation-proof") == 1


@pytest.mark.parametrize("layout", ["matrix", "castellated"])
def test_verified_photo_family_cannot_invent_terminals(window, monkeypatch, tmp_path, layout):
    params = ext._params_from_tool_arguments(dict(
        layout=layout, body_length_mm=40, body_width_mm=20, body_height_mm=5,
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=15,
        pad_diameter_mm=1.7, drill_mm=1,
        castellated_sides=[dict(edge="left", count=2, pitch_mm=2.54)],
        components_3d=[dict(name="U", kind="ic_qfn", _detected_kind="ic_qfn",
            package="QFN", x_mm=0, y_mm=0, length_mm=5, width_mm=5, height_mm=1)]))
    # Vedi sopra: il gruppo «Modello 3D» serve acceso per avere un corpo.
    window._freecad_available = True
    window._on_extracted(params)
    captured = {}
    monkeypatch.setattr(kicad_models, "attach_models", lambda components, **kw: None)
    def build(*args, **kwargs):
        captured.update(kwargs["extra_payload"])
        return {"warnings": []}
    monkeypatch.setattr(cad, "_run_freecad", build)
    if layout == "castellated":
        cad.generate_castellated_3d_model(window._current_castellated_params(),
                                         window._current_body_params(), tmp_path)
    else:
        cad.generate_3d_model(window._current_params(), window._current_body_params(), tmp_path)
    component = captured["components"][0]
    assert component["kind"] == "ic"
    assert component["detected_kind"] == "ic_qfn"
    assert component["package"] == "QFN"


def test_process_log_is_readable_before_process_exits(tmp_path):
    path = tmp_path / "freecad.log"
    marker = tmp_path / "callback-seen"
    def on_line(message):
        assert "first" in path.read_text(encoding="utf-8")
        marker.touch()
    script = ("import pathlib,time; print('first',flush=True); "
              f"p=pathlib.Path({str(marker)!r}); "
              "deadline=time.monotonic()+3; "
              "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(.01)'); "
              "assert p.exists(), 'callback was delayed until process exit'")
    result = run_logged([sys.executable, "-c", script], path, progress=on_line, timeout=6)
    assert result.returncode == 0


def test_a_slow_but_talking_process_is_not_killed(tmp_path):
    """@brief Caso positivo: lavorare a lungo non è un guasto.

    Il limite di prima era sulla durata complessiva: su un modulo vero l'import
    del primo modello 3D si è mangiato i 300 secondi da solo e il processo è
    stato ucciso prima del secondo componente dei cinquanta. Qui il processo
    dura più del limite ma continua ad annunciare quello che fa: deve arrivare
    in fondo.
    """
    path = tmp_path / "freecad.log"
    # Silenzio massimo 1.5 s (0.4 s prima: l'avvio dell'interprete su una
    # macchina carica bastava a superarlo), durata totale 2.4 s: il processo
    # dura comunque piu' del limite e parla ogni 0.15 s.
    script = ("import time,sys\n"
              "for i in range(16):\n"
              "    print('KICADGEN_COMPONENT_START:%d/16' % (i + 1), flush=True)\n"
              "    time.sleep(0.15)\n")
    result = run_logged([sys.executable, "-c", script], path, progress=lambda _: None,
                        timeout=1.5)
    assert result.returncode == 0
    assert result.stdout.count("KICADGEN_COMPONENT_START") == 16
    assert "timeout=False" in path.read_text(encoding="utf-8")


def test_a_silent_process_is_still_killed(tmp_path):
    """@brief Caso negativo: il silenzio resta il segnale di un lavoro fermo.

    Distinguere «lento» da «fermo» è tutto il punto della regola: il primo va
    lasciato finire, il secondo va interrotto.
    """
    import subprocess
    path = tmp_path / "freecad.log"
    script = ("import time\n"
              "print('KICADGEN_COMPONENT_START:1/6', flush=True)\n"
              "time.sleep(30)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        run_logged([sys.executable, "-c", script], path, progress=lambda _: None,
                   timeout=0.5)
    written = path.read_text(encoding="utf-8")
    assert "timeout=True" in written
    # Il diario dice che a mancare era l'output, non il tempo speso.
    assert "nessun output per" in written


def _wait_for_worker(window, qt_app=None):
    """@brief Porta a termine il worker di generazione avviato dalla finestra.

    @param window Finestra con `_worker` gia' avviato.
    @note Il worker e' un QThread vero: si aspetta la fine e poi si smaltiscono
        gli eventi accodati, altrimenti i segnali `done`/`finished` non
        arriverebbero mai agli slot e la prova leggerebbe uno stato a meta'.
    """
    from PyQt6.QtWidgets import QApplication
    worker = window._worker
    if worker is not None:
        worker.wait(15000)
    QApplication.processEvents()
    QApplication.processEvents()


def test_a_deliberate_2d_only_run_still_produces_the_footprint(window, monkeypatch):
    """@brief Positivo: con il gruppo «Modello 3D» spento la generazione automatica scrive il .kicad_mod.

    Sequenza riprodotta dall'utente: utente avanzato, gruppo «Modello 3D»
    spento col mouse nel tab Footprint 2D, poi ritorno alla modalita' normale
    (che riaccende la generazione automatica) e nuova estrazione. Il gruppo
    spento fa restituire None a `_current_body_params()`, e quel None veniva
    trattato come dato mancante: l'esito era un avviso e NESSUN footprint.
    Un modello 3D non richiesto non puo' mancare.
    """
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        lambda *a, **k: pytest.fail("nessun avviso: il 2D e' un esito valido"))
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda *a, **k: gui.QMessageBox.StandardButton.Yes)
    window.group_3d.setChecked(False)
    window._user_3d_choice = False  # come dopo il clic umano sul titolo del gruppo
    params = _castellated_raw()
    window._auto_after_extraction = True
    window._on_extracted(params)
    assert not window.group_3d.isChecked(), "la scelta umana non si revoca"
    assert window._current_body_params() is None

    window._start_automatic_generation(window._auto_generation_params)
    _wait_for_worker(window)
    name = window._component_name()
    footprint = window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"
    assert footprint.is_file(), "il footprint 2D deve esistere anche senza modello 3D"
    assert window._automatic_footprint_done is True
    assert window._automatic_3d_expected is False


def test_a_missing_body_with_the_3d_group_on_still_blocks(window, monkeypatch):
    """@brief Negativo: con il gruppo ACCESO un corpo assente resta un guasto da segnalare.

    E' la meta' che non va persa: il rilassamento riguarda la scelta di non
    generare il 3D, non i dati mancanti quando il 3D e' stato chiesto.
    """
    blocked = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(window, "_automatic_generation_blocked", blocked.append)
    monkeypatch.setattr(window, "_current_body_params", lambda: None)
    window.group_3d.setChecked(True)
    window._start_automatic_generation(_castellated_raw())
    assert blocked and "modello 3D" in blocked[0]
    assert window._worker is None or not window._worker.isRunning()


def test_the_3d_group_stays_off_when_freecad_is_absent(window):
    """@brief Negativo: senza FreeCAD l'estrazione non accende il gruppo «Modello 3D».

    `_build_3d_group` lo spegne per una ragione dichiarata («Senza FreeCAD
    resta spento, cosi' la generazione non fallisce»), ma quello spegnimento e'
    programmatico e non lascia traccia in `_user_3d_choice`: l'estrazione lo
    riaccendeva, e ogni generazione finiva con un `cad3d_error`.

    Positivo: con FreeCAD disponibile la stessa estrazione lo accende.
    """
    window._freecad_available = False
    window.group_3d.setChecked(False)
    window._on_extracted(_castellated_raw())
    assert not window.group_3d.isChecked(), "senza FreeCAD non si accende"
    assert window._current_body_params() is None

    window._freecad_available = True
    window._on_extracted(_castellated_raw())
    assert window.group_3d.isChecked(), "con FreeCAD la stessa estrazione lo accende"
