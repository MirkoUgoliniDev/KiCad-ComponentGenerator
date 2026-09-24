"""@brief Cover the import heartbeat that keeps the silence watchdog honest.

Il difetto riprodotto: su una corsa vera l'import di un modello di libreria
pesante (un modulo RF schermato con la sua antenna) e' rimasto muto per cinque
minuti e il watchdog di `run_logged` ha ucciso un processo sano al tredicesimo
componente su cinquantadue, buttando via anche le chiamate AI gia' pagate. La
correzione NON e' alzare la soglia -- sarebbe la soglia tarata sul caso
osservato, e un modello piu' grande la rifarebbe scattare -- ma far parlare il
passo muto, cosi' che "silenzio" torni a significare "fermo".

Queste prove non richiedono FreeCAD: il processo figlio e' un normale
interprete Python che stampa le stesse righe, e `freecad_parts` viene caricato
con `FreeCAD`/`Part` sostituiti.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from kicadgen.cad3d.process_log import (HEARTBEAT_BEATS_PER_TIMEOUT,
                                        heartbeat_interval, run_logged)


ROOT = Path(__file__).resolve().parents[1]

#: Testo che apre ogni battito: e' il contratto della riga, non un dettaglio.
BEAT_HEAD = "Importazione del modello 3D"


# --------------------------------------------------------------------------
# Caricamento di freecad_parts senza FreeCAD installato.
# --------------------------------------------------------------------------

class _Vector:
    """Vettore minimo: del vettore vero serve solo la negazione."""

    def __init__(self, *values):
        self.values = tuple(values)

    def __neg__(self):
        return _Vector(*(-v for v in self.values))


class _Box:
    """BoundBox finto, con i soli campi letti da _library_model."""

    def __init__(self, length, width, height):
        self.XMin, self.XMax = -length / 2, length / 2
        self.YMin, self.YMax = -width / 2, width / 2
        self.ZMin, self.ZMax = 0.0, height
        self.XLength, self.YLength, self.ZLength = length, width, height


class _Shape:
    """Solido finto: registra le trasformazioni e non fa geometria."""

    def __init__(self, box):
        self.BoundBox = box
        self.moves = []

    def copy(self):
        return self

    def translate(self, vector):
        self.moves.append(("translate", vector))

    def rotate(self, *args):
        self.moves.append(("rotate", args))

    def slice(self, *args):
        return []


@pytest.fixture
def parts(monkeypatch):
    """@brief Load `freecad_parts` with the FreeCAD environment stubbed out."""
    shape = _Shape(_Box(10.0, 4.0, 2.0))
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(
        Vector=_Vector, ActiveDocument=None,
        newDocument=lambda name: SimpleNamespace(Name=name),
        closeDocument=lambda name: None,
        setActiveDocument=lambda name: None,
    ))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        makeBox=lambda *a: shape,
        makeCylinder=lambda *a: shape,
        makeCompound=lambda shapes: shape,
        read=lambda path: shape,
    ))
    path = ROOT / "kicadgen/cad3d/freecad_parts.py"
    spec = importlib.util.spec_from_file_location("heartbeat_test_parts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _component(**updates):
    payload = dict(name="U1", kind="module", x=0.0, y=0.0,
                   length_mm=10.0, width_mm=4.0, height_mm=2.0,
                   color_hex="#202020", rotation_deg=0,
                   model_path=r"C:\KiCad\espressif.3dshapes\ESP32-S3-WROOM-1U.STEP")
    payload.update(updates)
    return payload


# --------------------------------------------------------------------------
# Il legame fra intervallo del battito e timeout del watchdog.
# --------------------------------------------------------------------------

def test_the_interval_is_derived_from_the_timeout_and_leaves_a_margin():
    """@brief Positivo: l'intervallo e' una frazione del timeout, non un numero a parte.

    E' un rapporto, quindi vale identico per qualunque timeout futuro: e'
    questo che impedisce alle due costanti di divergere in silenzio.
    """
    for timeout in (30, 120, 300, 1800):
        interval = heartbeat_interval(timeout)
        assert interval * HEARTBEAT_BEATS_PER_TIMEOUT == pytest.approx(timeout)
        assert interval < timeout
    # Margine dichiarato: serve piu' di una battuta persa perche' scatti.
    assert HEARTBEAT_BEATS_PER_TIMEOUT >= 2


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "x", None])
def test_a_timeout_without_meaning_gives_no_interval(bad):
    """@brief Negativo: da un timeout non valido non si ricava un intervallo."""
    with pytest.raises((ValueError, TypeError)):
        heartbeat_interval(bad)


def test_the_script_receives_the_interval_that_matches_the_watchdog(monkeypatch, tmp_path):
    """@brief Positivo per costruzione: chi cambia uno dei due numeri rompe questa prova.

    Si legge il file JSON dei parametri COSI' COME lo riceve freecadcmd e lo si
    confronta con il timeout passato al watchdog nella stessa chiamata. Alzare
    il timeout senza toccare l'intervallo, o viceversa, fallisce qui.
    """
    from kicadgen.cad3d import process_log, step_generator
    from kicadgen.cad3d.body_params import Body3DParams

    captured = {}

    def fake_run_logged(command, log_path, progress=None, timeout=None, creationflags=0):
        with open(command[-1], "r", encoding="utf-8") as handle:
            captured["payload"] = json.load(handle)
        captured["timeout"] = timeout
        raise RuntimeError("stop before freecadcmd")

    monkeypatch.setattr(process_log, "run_logged", fake_run_logged)
    with pytest.raises(RuntimeError):
        step_generator._run_freecad(
            "Test", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
            freecadcmd_path="/nonexistent/freecadcmd", progress=lambda _: None)

    assert captured["timeout"] == step_generator.FREECAD_SILENCE_TIMEOUT_S
    assert captured["payload"]["heartbeat_interval_s"] == heartbeat_interval(
        captured["timeout"])


def test_an_extra_payload_cannot_unhook_the_heartbeat(monkeypatch, tmp_path):
    """@brief Negativo: un chiamante non deve poter scollegare il battito.

    Il campo viene scritto DOPO l'unione con `extra_payload`: scrivendolo prima,
    un generatore che passasse una chiave omonima riporterebbe in vita il
    difetto senza che nulla lo segnali.
    """
    from kicadgen.cad3d import process_log, step_generator
    from kicadgen.cad3d.body_params import Body3DParams

    captured = {}

    def fake_run_logged(command, log_path, progress=None, timeout=None, creationflags=0):
        with open(command[-1], "r", encoding="utf-8") as handle:
            captured["payload"] = json.load(handle)
        raise RuntimeError("stop before freecadcmd")

    monkeypatch.setattr(process_log, "run_logged", fake_run_logged)
    with pytest.raises(RuntimeError):
        step_generator._run_freecad(
            "Test", [], Body3DParams(10.0, 4.0, 2.0), tmp_path,
            freecadcmd_path="/nonexistent/freecadcmd", progress=lambda _: None,
            extra_payload={"heartbeat_interval_s": 999999.0})

    assert captured["payload"]["heartbeat_interval_s"] == heartbeat_interval(
        step_generator.FREECAD_SILENCE_TIMEOUT_S)


@pytest.mark.parametrize("value,expected", [
    (12.5, 12.5), (0.25, 0.25),
    (None, 60.0), (0, 60.0), (-3, 60.0), ("x", 60.0),
    (float("nan"), 60.0), (float("inf"), 60.0),
])
def test_only_a_usable_interval_replaces_the_fallback(parts, value, expected):
    """@brief Positivo e negativo sul valore che arriva dal file dei parametri."""
    assert parts.set_heartbeat_interval(value) == expected


# --------------------------------------------------------------------------
# La riga: deve rompere il silenzio E raggiungere la modale.
# --------------------------------------------------------------------------

def test_the_heartbeat_line_names_the_file_and_the_elapsed_time(parts):
    """@brief Positivo: la riga dice QUALE modello e DA QUANTO."""
    line = parts.heartbeat_line("ESP32-S3-WROOM-1U.STEP", 312.4)
    assert line.startswith(BEAT_HEAD)
    assert "ESP32-S3-WROOM-1U.STEP" in line
    assert "5 min 12 s" in line
    assert "45 s" in parts.heartbeat_line("x.step", 45)


def test_the_heartbeat_line_carries_no_absolute_path(parts):
    """@brief Negativo: il percorso intero verrebbe accorciato, quindi non si stampa.

    `analysis_overlay.elide_paths` riduce comunque un percorso al nome del file:
    stamparlo per esteso costerebbe tre quarti della riga senza aggiungere
    nulla di leggibile.
    """
    # Il separatore lo decide la libreria KiCad, non il sistema su cui gira
    # lo script: entrambe le forme devono ridursi al nome del file.
    for path in (r"C:\Users\mirko\KiCad\espressif.3dshapes\ESP32-S3-WROOM-1U.STEP",
                 "/usr/share/kicad/3dmodels/espressif.3dshapes/ESP32-S3-WROOM-1U.STEP"):
        heartbeat = parts._ImportHeartbeat(path)
        assert heartbeat._name == "ESP32-S3-WROOM-1U.STEP"
        line = parts.heartbeat_line(heartbeat._name, 120)
        assert "\\" not in line and "/" not in line
    # Un nome senza percorso resta se stesso.
    assert parts._ImportHeartbeat("ESP32.STEP")._name == "ESP32.STEP"


def test_the_heartbeat_line_reaches_the_progress_modal(parts):
    """@brief Positivo: i filtri della GUI NON scartano la riga.

    E' meta' dello scopo del battito: se finisse nel solo log, l'utente
    continuerebbe a guardare una barra ferma su "12/52" senza sapere perche'.
    Le due funzioni vengono importate davvero, non riscritte qui: una modifica
    ai filtri che rendesse la riga invisibile deve far fallire questa prova.
    """
    pytest.importorskip("PyQt6.QtWidgets")
    from kicadgen.gui.analysis_overlay import is_machine_line
    from kicadgen.gui.main_window import per_item_detail

    line = parts.heartbeat_line("ESP32-S3-WROOM-1U.STEP", 300)
    for text in (line, "FreeCAD: " + line):
        assert not is_machine_line(text), text
        assert not per_item_detail(text), text


def test_a_marker_line_would_have_been_filtered_out():
    """@brief Negativo che giustifica la forma scelta.

    La stessa informazione scritta come marcatore `KICADGEN_*` romperebbe il
    silenzio ma sparirebbe dalla modale: e' l'alternativa scartata, e questa
    prova la tiene scartata.
    """
    pytest.importorskip("PyQt6.QtWidgets")
    from kicadgen.gui.analysis_overlay import is_machine_line

    assert is_machine_line("KICADGEN_IMPORT_ALIVE:ESP32-S3-WROOM-1U.STEP; elapsed=300")
    assert is_machine_line("FreeCAD: KICADGEN_IMPORT_ALIVE:ESP32-S3-WROOM-1U.STEP")


def test_the_elapsed_phrase_stays_readable_at_every_scale(parts):
    """@brief Positivo: la durata cresce e resta leggibile; nessun valore negativo."""
    assert [parts._elapsed_phrase(s) for s in (0, 1, 59, 60, 61, 599, 3600)] == [
        "0 s", "1 s", "59 s", "1 min 0 s", "1 min 1 s", "9 min 59 s", "60 min 0 s"]
    assert parts._elapsed_phrase(-5) == "0 s"


# --------------------------------------------------------------------------
# Il watchdog: lento con battito passa, fermo no.
# --------------------------------------------------------------------------

def _child(body):
    return [sys.executable, "-c", "import time, sys\n" + body]


def test_a_long_import_that_beats_is_not_killed(parts, tmp_path):
    """@brief Positivo: un import piu' lungo del timeout, ma che batte, arriva in fondo.

    E' il caso dell'utente. Il figlio stampa LA RIGA VERA del battito, non un
    segnaposto, quindi la prova copre anche il fatto che quella riga resetta il
    conto del silenzio.
    """
    line = parts.heartbeat_line("ESP32-S3-WROOM-1U.STEP", 60)
    log = tmp_path / "freecad.log"
    script = ("print('KICADGEN_IMPORT_START:/lib/ESP32-S3-WROOM-1U.STEP', flush=True)\n"
              "for _ in range(6):\n"
              "    time.sleep(0.4)\n"
              "    print(%r, flush=True)\n" % line +
              "print('KICADGEN_IMPORT_OK:/lib/ESP32-S3-WROOM-1U.STEP; parts=1', flush=True)\n")
    started = time.monotonic()
    result = run_logged(_child(script), log, progress=lambda _: None, timeout=2.0)
    assert time.monotonic() - started > 2.0  # ha davvero superato il timeout
    assert result.returncode == 0
    assert "KICADGEN_IMPORT_OK" in result.stdout
    assert result.stdout.count(BEAT_HEAD) == 6
    assert "timeout=False" in log.read_text(encoding="utf-8")


def test_a_completely_silent_child_is_still_killed(tmp_path):
    """@brief Negativo: il silenzio resta il segnale del lavoro fermo.

    Il battito non deve rendere il watchdog innocuo: senza NESSUN output il
    figlio va ucciso, con lo stesso messaggio di prima.
    """
    log = tmp_path / "freecad.log"
    with pytest.raises(subprocess.TimeoutExpired):
        run_logged(_child("time.sleep(30)\n"), log, progress=lambda _: None, timeout=0.5)
    written = log.read_text(encoding="utf-8")
    assert "timeout=True" in written
    assert "nessun output per 0.5 s" in written


def test_a_child_that_stops_beating_mid_import_is_killed(tmp_path):
    """@brief Negativo: il vero blocco, cioe' un battito che si spegne a meta'.

    E' la distinzione che il battito esiste per rendere possibile: prima
    "lento" e "fermo" apparivano identici (silenzio) e il primo veniva punito
    come il secondo; ora solo il secondo lo e'.
    """
    log = tmp_path / "freecad.log"
    beat = BEAT_HEAD + " BIG.STEP ancora in corso da 1 s, attendere."
    script = ("print('KICADGEN_IMPORT_START:/lib/BIG.STEP', flush=True)\n"
              "for _ in range(3):\n"
              "    time.sleep(0.3)\n"
              "    print(%r, flush=True)\n" % beat +
              "time.sleep(30)\n")
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_logged(_child(script), log, progress=lambda _: None, timeout=1.0)
    assert raised.value.output.count(BEAT_HEAD) == 3
    written = log.read_text(encoding="utf-8")
    assert "timeout=True" in written
    assert "nessun output per" in written


# --------------------------------------------------------------------------
# Il ciclo di vita del battito dentro lo script FreeCAD.
# --------------------------------------------------------------------------

def test_the_heartbeat_beats_while_the_import_is_stuck(parts, capsys):
    """@brief Positivo: durante un import muto escono righe a intervalli regolari."""
    parts.set_heartbeat_interval(0.05)
    with parts._ImportHeartbeat("/lib/ESP32-S3-WROOM-1U.STEP"):
        time.sleep(0.3)
    beats = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(beats) >= 3
    assert all(line.startswith(BEAT_HEAD) for line in beats)
    assert all("ESP32-S3-WROOM-1U.STEP" in line for line in beats)


def test_the_heartbeat_stops_when_the_import_finishes(parts, monkeypatch, capsys):
    """@brief Positivo: nessuna riga di attesa dopo il marcatore di esito.

    Un battito che sopravvive al passo che annunciava farebbe raccontare al log
    una bugia -- "ancora in corso" dopo KICADGEN_IMPORT_OK -- e terrebbe zitto
    il watchdog anche durante un blocco successivo.
    """
    parts.set_heartbeat_interval(0.05)

    def slow(path, color, heartbeat=None):
        time.sleep(0.25)
        return [(_Shape(_Box(10.0, 4.0, 2.0)), color)]

    monkeypatch.setattr(parts, "_read_library_parts", slow)
    assert parts._library_model(_component(), 0.0, 0.0, 1.6, "#202020") is not None
    time.sleep(0.2)  # il thread avrebbe tutto il tempo per battere ancora
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(line.startswith(BEAT_HEAD) for line in lines)
    assert lines[-1].startswith("KICADGEN_IMPORT_OK")
    after = lines[lines.index(next(l for l in lines if l.startswith("KICADGEN_IMPORT_OK"))):]
    assert not any(line.startswith(BEAT_HEAD) for line in after)


def test_the_heartbeat_stops_when_the_import_raises(parts, monkeypatch, capsys):
    """@brief Negativo: anche il fallimento ferma il battito.

    Entrambe le vie di lettura falliscono, quindi si esce dal passo per
    eccezione: il battito non deve sopravvivere al ripiego sulla forma
    sintetica.
    """
    parts.set_heartbeat_interval(0.05)

    def explode(path, color, heartbeat=None):
        time.sleep(0.2)
        raise RuntimeError("STEP illeggibile")

    def unreadable(path):
        raise RuntimeError("nemmeno Part.read")

    monkeypatch.setattr(parts, "_read_library_parts", explode)
    monkeypatch.setattr(parts.Part, "read", unreadable)
    component = _component()
    assert parts._library_model(component, 0.0, 0.0, 1.6, "#202020") is None
    assert component["_model_import_error"]
    time.sleep(0.2)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(line.startswith(BEAT_HEAD) for line in lines)
    # Dal 2026-09-24 il figlio manda l'avviso come modello + valori in JSON
    # (lo traduce il padre, che conosce la lingua): si guarda la riga come la
    # mostra il padre, non il suo formato di trasporto.
    from kicadgen.services.messages import render_child_line
    assert lines[-1].startswith("KICADGEN_WARN:")
    assert render_child_line(lines[-1]).startswith("KICADGEN_WARN:modello")


def test_a_fast_import_leaves_no_waiting_line(parts, capsys):
    """@brief Negativo: un import veloce non riempie ne' il log ne' la modale.

    Nella stessa corsa un ricettacolo USB-C e' stato importato in 6 s: quei
    componenti non devono produrre righe di attesa.
    """
    parts.set_heartbeat_interval(5.0)
    with parts._ImportHeartbeat("/lib/USB_C.STEP") as heartbeat:
        heartbeat.beat_if_due()
    assert capsys.readouterr().out == ""


def test_the_working_thread_can_beat_without_the_timer(parts, capsys):
    """@brief Positivo: il battito non dipende dal SOLO timer.

    LIMITE DICHIARATO che questa prova copre: un thread Python batte solo se
    l'interprete gira, cioe' se la chiamata C++ dell'import rilascia il GIL.
    Per i tratti Python lunghi dell'import (la copia dei pezzi di un assieme)
    il battito parte dal thread di lavoro, che gira sempre. Qui l'intervallo e'
    lungo, quindi il timer non puo' aver battuto: la riga viene da beat_if_due.
    """
    heartbeat = parts._ImportHeartbeat("/lib/BIG.STEP", interval=3600)
    heartbeat.start()
    try:
        heartbeat.beat_if_due()
        assert capsys.readouterr().out == ""
        heartbeat._last -= 7200  # il lavoro Python e' andato lungo
        heartbeat.beat_if_due()
        out = capsys.readouterr().out
    finally:
        heartbeat.stop()
    assert out.startswith(BEAT_HEAD) and "BIG.STEP" in out


def test_stopping_twice_is_harmless(parts, capsys):
    """@brief Negativo: stop() ripetuto non solleva e non lascia uscire altre righe."""
    heartbeat = parts._ImportHeartbeat("/lib/BIG.STEP", interval=0.02)
    heartbeat.start()
    time.sleep(0.07)
    heartbeat.stop()
    heartbeat.stop()
    emitted = capsys.readouterr().out
    assert emitted.count(BEAT_HEAD) >= 2
    time.sleep(0.08)
    heartbeat.beat_if_due()
    assert capsys.readouterr().out == ""
