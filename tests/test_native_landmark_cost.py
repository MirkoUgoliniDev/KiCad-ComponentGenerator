"""@brief La misura del verso si paga una volta sola, e mai piu' del budget.

Il difetto riprodotto: su una corsa vera la generazione si e' fermata cinque
minuti sul modulo RF schermato, al tredicesimo componente su cinquantadue. Il
tempo non se ne andava nell'import ma nella misura del verso, cioe' in una
sottrazione booleana fra un assieme grande e la sua copia ruotata di mezzo
giro. Quella misura ha due difetti indipendenti, e servono due correzioni:

1. e' deterministica -- stesso file, stessa geometria, stessa risposta -- e
   veniva rifatta a ogni corsa: adesso si memorizza per identita' del file;
2. non aveva limite: adesso ha un budget derivato dal timeout del watchdog, e
   sforarlo produce l'esito «non lo so» che il chiamante gia' sa gestire.

In piu' il boolean non serve sempre: lo scarto del baricentro del residuo e'
quello del solido INTERO moltiplicato per un fattore positivo (vedi
`_cheap_landmark_verdict`), quindi segno e dominanza si leggono gratis.

Nessuna di queste prove richiede FreeCAD: `freecad_parts` viene caricato con
`FreeCAD`/`Part` sostituiti e i "boolean" sono attese controllabili.
"""

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.process_log import heartbeat_interval


ROOT = Path(__file__).resolve().parents[1]

#: Testo che apre la riga di rinuncia: e' il contratto della riga.
GIVEUP_HEAD = "Verso del modello STEP"


# --------------------------------------------------------------------------
# Ambiente finto: nessun FreeCAD, un "boolean" che dorme quanto vogliamo.
# --------------------------------------------------------------------------

class _Vector:
    def __init__(self, *values):
        self.values = tuple(values)

    def __neg__(self):
        return _Vector(*(-v for v in self.values))


class _Box:
    """BoundBox finto, con i soli campi letti dalla misura."""

    def __init__(self, x_min, x_max, y_min, y_max, z_min=0.0, z_max=1.0):
        self.XMin, self.XMax = x_min, x_max
        self.YMin, self.YMax = y_min, y_max
        self.ZMin, self.ZMax = z_min, z_max
        self.XLength, self.YLength = x_max - x_min, y_max - y_min
        self.ZLength = z_max - z_min


#: Ingombro 20 x 18 centrato nell'origine, lo stesso di test_native_model_axis.
FRAME = _Box(-10, 10, -9, 9)


class _Residual:
    def __init__(self, volume, x, y):
        self.Volume = volume
        self.CenterOfMass = SimpleNamespace(x=x, y=y, z=0.0)


class _Monolith:
    """Solido indivisibile: la sottrazione e' UNA chiamata, come in OpenCascade.

    Non sa dire baricentro ne' ingombro, quindi la scorciatoia gratuita non si
    applica e si passa davvero dal boolean: e' il caso che le prove sulla cache
    devono osservare.
    """

    def __init__(self, volume=400.0, residual=(64.0, 0.5, -1.8), delay=0.0):
        self.Volume = volume
        self._residual = residual
        self._delay = delay
        self.cuts = 0
        self.moves = []

    def copy(self):
        return self

    def rotate(self, *args):
        self.moves.append(("rotate", args))

    def translate(self, vector):
        self.moves.append(("translate", vector))

    def slice(self, *args):
        return []

    def cut(self, other):
        self.cuts += 1
        if self._delay:
            time.sleep(self._delay)
        return _Residual(*self._residual)


class _Piece:
    """Un solido dell'assieme: la sua sottrazione costa `delay` secondi."""

    def __init__(self, box, volume, centre, delay, log):
        self.BoundBox = box
        self.Volume = volume
        self.CenterOfMass = SimpleNamespace(x=centre[0], y=centre[1], z=0.0)
        self._delay = delay
        self._log = log

    def cut(self, other):
        self._log.append(self)
        if self._delay:
            time.sleep(self._delay)
        return _Residual(self.Volume, self.CenterOfMass.x, self.CenterOfMass.y)


def _mirrored(box):
    """Ingombro dopo mezzo giro attorno all'origine, che e' il centro di FRAME."""
    return _Box(-box.XMax, -box.XMin, -box.YMax, -box.YMin, box.ZMin, box.ZMax)


class _Mirror:
    """La copia da sottrarre: `rotate` specchia davvero gli ingombri.

    Serve a provare la potatura: senza un mezzo giro vero, ogni solido
    incontrerebbe se stesso e nessun pezzo risulterebbe mai libero.
    """

    def __init__(self, solids):
        self.Solids = list(solids)

    def rotate(self, *args):
        self.Solids = [SimpleNamespace(BoundBox=_mirrored(solid.BoundBox))
                       for solid in self.Solids]


class _Assembly:
    """Assieme scomponibile: e' la forma che un modulo reale ha davvero.

    Il baricentro dichiarato e' quello VERO del WROOM-1U -- scarto dello 0,9%
    su X e del 2,9% su Y del semilato -- cioe' la banda in cui la scorciatoia
    gratuita non puo' decidere: e' proprio il caso in cui il boolean si paga.
    """

    def __init__(self, count, delay, volume=400.0, centre=(0.5, -1.8)):
        self.cut_log = []
        self.Solids = [_Piece(_Box(-10, 10, -9, 9), volume / count, centre,
                              delay, self.cut_log) for _ in range(count)]
        self.Volume = volume
        self.BoundBox = _Box(-10, 10, -9, 9)
        self.CenterOfMass = SimpleNamespace(x=0.0567 * 0.1586 * 10,
                                            y=-0.1808 * 0.1586 * 9, z=0.0)
        self.moves = []

    def copy(self):
        return _Mirror(self.Solids)

    def rotate(self, *args):
        self.moves.append(("rotate", args))

    def translate(self, vector):
        self.moves.append(("translate", vector))

    def slice(self, *args):
        return []


class _Centroid:
    """Solido che sa dire baricentro e ingombro: la misura gratuita si applica."""

    def __init__(self, dx, dy, box=FRAME, volume=400.0, on_cut=None):
        half_x, half_y = box.XLength / 2, box.YLength / 2
        centre_x, centre_y = (box.XMin + box.XMax) / 2, (box.YMin + box.YMax) / 2
        self.CenterOfMass = SimpleNamespace(x=centre_x + dx * half_x,
                                            y=centre_y + dy * half_y, z=0.0)
        self.BoundBox = box
        self.Volume = volume
        self._on_cut = on_cut

    def copy(self):
        return self

    def rotate(self, *args):
        return None

    def cut(self, other):
        if self._on_cut is not None:
            return self._on_cut()
        raise AssertionError("il boolean non doveva servire")


@pytest.fixture
def parts(monkeypatch, tmp_path):
    """@brief Load `freecad_parts` with FreeCAD stubbed and the cache isolated."""
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(
        Vector=_Vector, ActiveDocument=None,
        newDocument=lambda name: SimpleNamespace(Name=name),
        closeDocument=lambda name: None,
        setActiveDocument=lambda name: None,
    ))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        makeCompound=lambda shapes: SimpleNamespace(shapes=list(shapes)),
        read=lambda path: None,
    ))
    spec = importlib.util.spec_from_file_location(
        "landmark_cost_parts", ROOT / "kicadgen/cad3d/freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    index = tmp_path / "landmark-index.json"
    monkeypatch.setattr(module, "_landmark_cache_paths", lambda: [str(index)])
    module.index_path = index
    return module


def _model_file(tmp_path, name="ESP32-S3-WROOM-1U.STEP", payload="ISO-10303-21;"):
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# 1. La cache: si paga una volta, per QUEL file.
# --------------------------------------------------------------------------

def test_a_second_run_replays_the_landmark_instead_of_measuring_again(parts, tmp_path):
    """@brief Positivo: alla seconda corsa il boolean NON viene rifatto.

    E' la correzione che risolve il caso dell'utente: il verso di un file e'
    deterministico, quindi ripagarlo a ogni generazione e' tempo buttato.
    """
    shape, path = _Monolith(), _model_file(tmp_path)
    first = parts._cached_landmark_direction(shape, FRAME, path)
    second = parts._cached_landmark_direction(shape, FRAME, path)
    assert first == second == (0, -1)
    assert shape.cuts == 1


def test_the_cache_is_consulted_before_the_expensive_work(parts, tmp_path):
    """@brief Positivo: la replica non tocca nemmeno la geometria.

    Un solido che esplode a ogni operazione deve comunque rispondere, perche'
    la risposta arriva dall'indice e non da OpenCascade.
    """
    path = _model_file(tmp_path)
    assert parts._cached_landmark_direction(_Monolith(), FRAME, path) == (0, -1)

    class _Forbidden(_Monolith):
        def cut(self, other):
            raise AssertionError("la cache andava letta prima del boolean")

    assert parts._cached_landmark_direction(_Forbidden(), FRAME, path) == (0, -1)


def test_a_measured_absence_of_side_is_replayed_too(parts, tmp_path):
    """@brief Positivo: anche «non lo so» e' una misura, e non si ripaga."""
    shape = _Monolith(residual=(64.0, 0.0, 0.0))  # residuo centrato: nessun lato
    path = _model_file(tmp_path)
    assert parts._cached_landmark_direction(shape, FRAME, path) is None
    assert parts._cached_landmark_direction(shape, FRAME, path) is None
    assert shape.cuts == 1


def test_a_changed_file_is_measured_again(parts, tmp_path):
    """@brief Negativo: cambiare il file invalida la sua voce.

    La chiave porta dimensione e data del file, quindi un modello riscritto non
    puo' ereditare la misura della versione precedente.
    """
    path = _model_file(tmp_path)
    shape = _Monolith()
    assert parts._cached_landmark_direction(shape, FRAME, path) == (0, -1)
    Path(path).write_text("ISO-10303-21; UNA GEOMETRIA DIVERSA", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    other = _Monolith(residual=(64.0, 1.8, 0.5))
    assert parts._cached_landmark_direction(other, FRAME, path) == (1, 0)
    assert other.cuts == 1


def test_another_model_never_inherits_the_measurement(parts, tmp_path):
    """@brief Negativo: due file diversi sono due misure diverse."""
    first, second = _model_file(tmp_path), _model_file(tmp_path, "USB_C.STEP")
    assert parts._cached_landmark_direction(_Monolith(), FRAME, first) == (0, -1)
    other = _Monolith(residual=(64.0, 1.8, 0.5))
    assert parts._cached_landmark_direction(other, FRAME, second) == (1, 0)
    assert other.cuts == 1


def test_a_different_reference_is_a_different_question(parts, tmp_path):
    """@brief Negativo: il mezzo giro e' attorno al CENTRO del riferimento.

    Misurare sull'ingombro totale o sulla sezione del corpo sono due domande
    diverse: la seconda non puo' essere servita dalla risposta della prima.
    """
    path = _model_file(tmp_path)
    assert parts._cached_landmark_direction(_Monolith(), FRAME, path) == (0, -1)
    section = _Box(-6, 6, -5, 5)
    other = _Monolith(residual=(64.0, 1.8, 0.5))
    assert parts._cached_landmark_direction(other, section, path) == (1, 0)
    assert other.cuts == 1


@pytest.mark.parametrize("damage", ["truncated", "not-an-object", "directory"])
def test_an_unreadable_cache_recomputes_without_raising(parts, tmp_path, damage):
    """@brief Negativo: una cache rotta fa rimisurare, non fallire.

    Vale per tutti e tre i modi in cui un indice puo' essere inutilizzabile:
    JSON troncato, JSON valido ma della forma sbagliata, e percorso che non e'
    nemmeno un file.
    """
    index = parts.index_path
    if damage == "truncated":
        index.write_text('{"a": {"status": ', encoding="utf-8")
    elif damage == "not-an-object":
        index.write_text('["non un indice"]', encoding="utf-8")
    else:
        index.mkdir()
    shape = _Monolith()
    assert parts._cached_landmark_direction(shape, FRAME, _model_file(tmp_path)) == (0, -1)
    assert shape.cuts == 1


def test_a_record_that_does_not_name_this_file_is_not_used(parts, tmp_path):
    """@brief Negativo: la voce deve dichiarare DI CHI e' la misura.

    La chiave da sola non basta: un indice fuso male o modificato a mano non
    deve poter far ereditare a un modello il verso di un altro.
    """
    path = _model_file(tmp_path)
    assert parts._cached_landmark_direction(_Monolith(), FRAME, path) == (0, -1)
    index = json.loads(parts.index_path.read_text(encoding="utf-8"))
    key, record = next(iter(index.items()))
    record["model"] = "un altro file"
    parts.index_path.write_text(json.dumps({key: record}), encoding="utf-8")
    shape = _Monolith(residual=(64.0, 1.8, 0.5))
    assert parts._cached_landmark_direction(shape, FRAME, path) == (1, 0)
    assert shape.cuts == 1


def test_the_default_cache_lives_in_the_user_cache_directory(parts, monkeypatch, tmp_path):
    """@brief Positivo: l'indice non sporca la libreria 3D di KiCad.

    La libreria di sistema e' spesso in sola lettura; la cache dell'utente no.
    """
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # La fixture sostituisce la funzione per isolare le altre prove: qui serve
    # quella vera, quindi si ricarica il modulo con l'ambiente appena preparato.
    spec = importlib.util.spec_from_file_location(
        "landmark_cost_paths", ROOT / "kicadgen/cad3d/freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = Path(module._landmark_cache_paths()[0])
    assert tmp_path in destination.parents
    assert destination.name == module.LANDMARK_INDEX_FILENAME


# --------------------------------------------------------------------------
# 2. Il budget: derivato dal watchdog, e sforarlo non ferma la generazione.
# --------------------------------------------------------------------------

def test_the_budget_cannot_silently_outlive_the_watchdog(parts):
    """@brief Per costruzione: il budget E' l'intervallo del battito.

    Il numero esiste in un posto solo (`FREECAD_SILENCE_TIMEOUT_S`), e da li'
    discendono sia l'intervallo del battito sia questo budget. Se qualcuno
    desse alla misura una costante propria, questa prova fallirebbe; se
    cambiasse il timeout, i due si muoverebbero insieme, che e' il punto.
    """
    travelling = heartbeat_interval(step_generator.FREECAD_SILENCE_TIMEOUT_S)
    parts.set_heartbeat_interval(travelling)
    assert parts._landmark_budget_s() == travelling
    assert 0 < parts._landmark_budget_s() < step_generator.FREECAD_SILENCE_TIMEOUT_S


def test_a_measurement_over_the_budget_gives_up_and_says_so(parts, tmp_path, capsys):
    """@brief Positivo: sforare il budget da' «non lo so», non un guasto.

    L'assieme e' scomponibile, come quello vero: il limite si fa sentire fra
    un solido e l'altro, e i solidi non ancora toccati non vengono pagati.
    """
    parts.set_heartbeat_interval(0.15)
    shape = _Assembly(count=12, delay=0.05)
    path = _model_file(tmp_path)
    assert parts._cached_landmark_direction(shape, FRAME, path) is None
    assert len(shape.cut_log) < 12
    printed = capsys.readouterr().out
    assert printed.startswith(GIVEUP_HEAD)
    assert "ESP32-S3-WROOM-1U.STEP" in printed


def test_the_abandoned_measurement_is_not_paid_again_for_the_same_budget(parts, tmp_path):
    """@brief Positivo: una rinuncia si ricorda, ma resta legata al suo budget.

    Ripagare minuti per arrivare alla stessa rinuncia non aiuta nessuno; un
    budget piu' generoso invece merita un secondo tentativo, quindi la voce
    porta il budget con cui e' stata scritta.
    """
    parts.set_heartbeat_interval(0.15)
    path = _model_file(tmp_path)
    assert parts._cached_landmark_direction(_Assembly(12, 0.05), FRAME, path) is None
    again = _Assembly(12, 0.05)
    assert parts._cached_landmark_direction(again, FRAME, path) is None
    assert again.cut_log == []
    parts.set_heartbeat_interval(60.0)
    generous = _Assembly(3, 0.0)
    assert parts._cached_landmark_direction(generous, FRAME, path) == (0, -1)
    assert len(generous.cut_log) == 3


def test_a_cheap_measurement_is_not_cut_short(parts, tmp_path):
    """@brief Negativo: chi sta dentro il budget riceve la sua vera risposta.

    E' la meta' che impedisce al limite di diventare una scusa per non
    misurare: un 0603 o un ricettacolo USB-C costano millisecondi.
    """
    parts.set_heartbeat_interval(30.0)
    shape = _Assembly(count=4, delay=0.0)
    assert parts._cached_landmark_direction(shape, FRAME, _model_file(tmp_path)) == (0, -1)
    assert len(shape.cut_log) == 4


def test_the_heartbeat_keeps_beating_during_the_measurement(parts, capsys):
    """@brief Positivo: il tratto muto della misura non spegne il battito.

    Il battito esiste perche' il watchdog non scambi un lavoro lento per un
    lavoro fermo: una misura che lo tacesse riaprirebbe esattamente il buco
    che il battito ha chiuso.
    """
    parts.set_heartbeat_interval(0.02)
    heartbeat = parts._ImportHeartbeat("/lib/BIG.STEP", interval=0.02)
    heartbeat.start()
    try:
        parts._half_turn_residue(_Assembly(6, 0.03), FRAME, None, heartbeat)
    finally:
        heartbeat.stop()
    assert "Importazione del modello STEP" in capsys.readouterr().out


def test_the_give_up_line_reaches_the_progress_modal(parts):
    """@brief Positivo: la riga deve arrivare all'utente, non solo al log.

    Se finisse nel solo log, l'utente vedrebbe un componente orientato male
    senza alcuna spiegazione. I filtri della GUI si importano davvero: una
    modifica che rendesse la riga invisibile deve far fallire questa prova.
    """
    pytest.importorskip("PyQt6.QtWidgets")
    from kicadgen.gui.analysis_overlay import is_machine_line
    from kicadgen.gui.main_window import per_item_detail

    line = parts.landmark_budget_line("ESP32-S3-WROOM-1U.STEP", 60)
    assert "ESP32-S3-WROOM-1U.STEP" in line and "1 min 0 s" in line
    assert "45 s" in parts.landmark_budget_line("x.step", 45)
    for text in (line, "FreeCAD: " + line):
        assert not is_machine_line(text), text
        assert not per_item_detail(text), text


def test_the_generation_continues_to_the_next_component(parts, monkeypatch, tmp_path, capsys):
    """@brief Positivo: rinunciare alla misura non fa perdere il componente.

    `_verse_correction` gia' prevede l'esito «non lo so»: il componente resta
    orientato dal solo ingombro, che e' esattamente cio' che accadeva prima
    che la misura esistesse.
    """
    parts.set_heartbeat_interval(0.15)
    assembly = _Assembly(count=12, delay=0.05)
    monkeypatch.setattr(parts.Part, "makeCompound", lambda shapes: assembly)
    monkeypatch.setattr(parts, "_read_library_parts",
                        lambda path, color, heartbeat=None: [(assembly, color)])
    component = dict(name="U1", kind="module", x=0.0, y=0.0,
                     length_mm=20.0, width_mm=18.0, height_mm=3.0,
                     color_hex="#202020", rotation_deg=0,
                     landmark_feature="antenna", landmark_side="top",
                     model_path=_model_file(tmp_path))
    built = parts._library_model(component, 0.0, 0.0, 1.6, "#202020")
    assert built is not None and len(built) == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(line.startswith(GIVEUP_HEAD) for line in lines)
    assert lines[-1].startswith("KICADGEN_IMPORT_OK")


# --------------------------------------------------------------------------
# 3. La formulazione che non paga il boolean, e la sua equivalenza.
# --------------------------------------------------------------------------

#: Le stesse misure reali citate nei commenti delle costanti e in
#: tests/test_native_model_axis.py: (frazione, scarto X, scarto Y) del RESIDUO.
REAL_RESIDUES = [
    (0.1586, 0.0567, -0.1808),   # modulo schermato ESP32-S3-WROOM-1U
    (0.4599, -0.009, 0.514),     # ricettacolo USB-C
    (0.0485, -1e-6, 2e-6),       # SOT-223: residuo bilanciato fra i due capi
    (0.0, 0.0, 0.0),             # chip 0603: simmetrico
    (0.004, 0.40, 0.0),          # residuo sotto la soglia di frazione
    (0.30, 0.30, 0.28),          # feature in un angolo
]


@pytest.mark.parametrize("fraction,dx,dy", REAL_RESIDUES)
def test_the_free_verdict_never_contradicts_the_boolean(parts, fraction, dx, dy):
    """@brief Equivalenza: la scorciatoia o tace, o dice la stessa cosa.

    Lo scarto del baricentro del residuo e' quello del solido INTERO diviso per
    la frazione di volume residua (identita' esatta, vedi
    `_cheap_landmark_verdict`). Qui si ricostruisce il baricentro del solido
    intero dalle misure reali del residuo e si verifica che la scorciatoia non
    contraddica mai la misura completa: o risponde identico, o dichiara di non
    poter decidere e lascia fare al boolean.
    """
    expected = parts._asymmetry_direction(fraction, dx, dy)
    shape = _Centroid(dx * fraction, dy * fraction)
    verdict = parts._cheap_landmark_verdict(shape, FRAME)
    assert verdict is parts._UNDECIDED or verdict == expected


def test_a_symmetric_solid_costs_no_boolean_at_all(parts):
    """@brief Positivo: il caso piu' frequente si chiude gratis.

    Un corpo simmetrico ha il baricentro sull'asse del mezzo giro a meno del
    rumore numerico, e nessuna frazione ammissibile puo' trasformare quel
    rumore in un lato: il boolean non si esegue.
    """
    assert parts._cheap_landmark_verdict(_Centroid(1e-9, -3e-10), FRAME) is None
    assert parts._native_landmark_direction(_Centroid(1e-9, -3e-10), FRAME) is None


def test_a_strongly_offset_solid_costs_no_boolean_either(parts):
    """@brief Positivo: anche l'altro estremo si chiude gratis.

    Con lo scarto del solido intero gia' sopra la soglia di scarto, nessuna
    frazione ammissibile puo' far cambiare idea alla regola: la frazione e'
    al piu' uno, quindi lo scarto del residuo e' almeno quello del solido.
    """
    assert parts._cheap_landmark_verdict(_Centroid(0.2, 0.01), FRAME) == (1, 0)
    assert parts._native_landmark_direction(_Centroid(0.2, 0.01), FRAME) == (1, 0)


def test_the_undecided_band_still_pays_the_boolean(parts):
    """@brief Negativo: la scorciatoia non si prende decisioni che non le spettano.

    Il modulo dell'utente cade proprio qui: il baricentro del solido intero e'
    spostato del 2,9% del semilato, troppo poco per decidere e troppo per
    escludere. Li' il boolean si paga -- una volta, e dentro il budget.
    """
    shape = _Centroid(0.0567 * 0.1586, -0.1808 * 0.1586,
                      on_cut=lambda: _Residual(64.0, 0.5, -1.8))
    assert parts._cheap_landmark_verdict(shape, FRAME) is parts._UNDECIDED
    assert parts._native_landmark_direction(shape, FRAME) == (0, -1)


def test_a_solid_far_outside_its_reference_is_not_decided_for_free(parts):
    """@brief Negativo: la scorciatoia vale solo dove la disuguaglianza regge.

    Il riferimento puo' essere la SEZIONE del corpo, piu' piccola del solido:
    li' il baricentro del residuo puo' cadere fuori dal riferimento e il limite
    inferiore sulla frazione si allenta. La scorciatoia se ne accorge e rinvia
    al boolean invece di rispondere lo stesso.
    """
    shape = _Centroid(0.0, 0.0, box=FRAME)
    # Solido venti volte piu' esteso della sezione di riferimento: il residuo
    # puo' avere un baricentro molto lontano, quindi una frazione molto piccola
    # basta a spiegare lo scarto osservato.
    shape.BoundBox = _Box(-200, 200, -180, 180)
    shape.CenterOfMass = SimpleNamespace(x=0.0, y=-0.9, z=0.0)  # 0.1 semilati
    assert parts._cheap_landmark_verdict(shape, FRAME) is parts._UNDECIDED


# --------------------------------------------------------------------------
# 4. La scomposizione per solidi: stessa risposta, in pezzi interrompibili.
# --------------------------------------------------------------------------

def test_a_solid_that_meets_no_mirror_image_is_kept_without_any_boolean(parts):
    """@brief Positivo: cio' che non tocca la propria immagine sopravvive intero.

    Non e' un'approssimazione ma la definizione: se gli ingombri non si
    toccano, l'intersezione e' vuota e la sottrazione non ha nulla da togliere.
    Su un assieme sparso e' la maggior parte del lavoro risparmiato.
    """
    class _Sparse(_Assembly):
        def __init__(self):
            super().__init__(count=2, delay=0.0)
            # Entrambi dallo stesso lato: le loro immagini speculari cadono
            # sull'altro, dove non c'e' materiale da togliere.
            self.Solids[0].BoundBox = _Box(-10, -6, -9, -5)
            self.Solids[1].BoundBox = _Box(-5, -1, -4, 0)

    shape = _Sparse()
    flipped = shape.copy()
    flipped.rotate()
    pieces = parts._residue_pieces(shape, flipped)
    assert [cutter for _, cutter in pieces] == [None, None]
    fraction, _, _ = parts._half_turn_residue(shape, FRAME)
    assert fraction == pytest.approx(1.0)
    assert shape.cut_log == []


def test_an_indivisible_shape_still_uses_the_single_boolean(parts):
    """@brief Negativo: una forma non scomponibile non perde la misura.

    LIMITE DICHIARATO che questa prova fissa: li' il budget puo' solo impedire
    di INIZIARE la sottrazione, perche' una chiamata OpenCascade gia' partita
    Python non la puo' interrompere.
    """
    shape = _Monolith()
    assert parts._residue_pieces(shape, shape) is None
    assert parts._native_landmark_direction(shape, FRAME) == (0, -1)
    assert shape.cuts == 1
    expired = _Monolith()
    with pytest.raises(parts._MeasurementExpired):
        parts._half_turn_residue(expired, FRAME, time.monotonic() - 1.0)
    assert expired.cuts == 0
