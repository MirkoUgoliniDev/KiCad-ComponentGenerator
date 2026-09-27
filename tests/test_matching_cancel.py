"""@brief «Interrompi» durante l'abbinamento dei modelli KiCad: cosa promette.

L'utente ha chiesto che ogni passo della modale si possa fermare. Il pulsante
uccideva gia' il figlio che COSTRUISCE il solido, ma la fase che lo precede --
«abbinamento modelli KiCad» -- avvia a sua volta `freecadcmd` per misurare i
candidati non ancora in cache, e su una scheda con decine di componenti puo'
durare minuti: li' premere il pulsante non faceva niente di visibile.

Qui si verifica che la rinuncia arrivi fino a quel figlio, e soprattutto che
cosa lasci dietro di se':

* le misure gia' concluse restano in cache (sono valide e gia' pagate);
* il risultato parziale NON viene restituito come se fosse completo;
* un componente mai esaminato non riceve il verdetto «nessun candidato», che
  sarebbe una diagnosi inventata su un pezzo che nessuno ha guardato;
* un lotto FALLITO per una ragione vera resta un fallimento, non una rinuncia.

Nessun test pretende FreeCAD installato: al suo posto gira un misuratore
Python che parla lo stesso protocollo di file (in.json / progress.jsonl /
out.json) e la cui durata per modello e' decisa dal test.
"""

import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.process_log import ProcessCancelled


# ── Aiutanti: un finto misuratore la cui lentezza decide il test ──────────

#: Misuratore finto: stesso protocollo dello script FreeCAD vero (vedi
#: `kicad_models._MEASURE_SCRIPT`). Annota su `attempts` OGNI modello che
#: comincia -- prima di dormire -- cosi' il test puo' dire quali misure non
#: sono mai state tentate; scrive `flag` SOLO se arriva in fondo, che e' la
#: prova dell'uccisione. `chr(10)` al posto della sequenza di escape: questo
#: testo passa da `str.format`, e raddoppiare le barre lo renderebbe illeggibile.
_FAKE_MEASURER = """
import json, os, time
from pathlib import Path

folder = os.environ["KICADGEN_MEASURE_DIR"]
with open(os.path.join(folder, "in.json"), encoding="utf-8") as handle:
    paths = json.load(handle)
attempts = Path(r"{attempts}")
sizes = {{}}
with open(os.path.join(folder, "progress.jsonl"), "w", encoding="utf-8") as done:
    for path in paths:
        with attempts.open("a", encoding="utf-8") as trace:
            trace.write(path + chr(10))
        time.sleep({interval})
        sizes[path] = None if path.endswith("{broken}") else {{
            "total": [1.6, 0.8, 0.4], "body": [1.6, 0.8, 0.4]}}
        done.write(json.dumps({{"path": path, "size": sizes[path]}}) + chr(10))
        done.flush()
with open(os.path.join(folder, "out.json"), "w", encoding="utf-8") as handle:
    json.dump(sizes, handle)
Path(r"{flag}").write_text("completato", encoding="utf-8")
"""

#: Nome che nessun modello di prova porta: serve a disattivare il guasto
#: simulato quando il test non ne vuole.
_NO_FAILURE = "___nessun_modello_rotto___"


def _arm_fake_measurer(monkeypatch, tmp_path, *, interval=0.4, broken=_NO_FAILURE):
    """@brief Mette al posto di freecadcmd un misuratore Python lento a comando.

    @param interval Secondi spesi per ogni modello: e' la «misura lenta».
    @param broken Suffisso del modello che il misuratore dichiara illeggibile.
    @return Namespace con il file delle misure tentate e quello di fine lotto.
    """
    attempts = tmp_path / "attempts.txt"
    flag = tmp_path / "batch-completed.flag"
    monkeypatch.setattr(km, "_MEASURE_SCRIPT", _FAKE_MEASURER.format(
        attempts=attempts, interval=interval, flag=flag, broken=broken))
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: sys.executable)
    return SimpleNamespace(attempts=attempts, flag=flag)


def _attempted(fake):
    """@brief I modelli la cui misura e' stata almeno cominciata."""
    try:
        return fake.attempts.read_text(encoding="utf-8").split()
    except OSError:
        return []


def _cancel_after(fake, count, cancel, timeout=20.0):
    """@brief Chiede la fine quando il misuratore ha cominciato @p count modelli.

    @note Legato al LAVORO e non a un cronometro: un test tarato sui secondi
        mentirebbe su una macchina lenta (rinuncia arrivata a lotto finito) o
        su una veloce (rinuncia arrivata prima della partenza).
    """
    def watch():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(_attempted(fake)) >= count:
                cancel.set()
                return
            time.sleep(0.01)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def _record_children(monkeypatch):
    """Record every child process started from now on, as its Popen handle.

    The handle is what proves a child is gone: waiting on it returns only once
    the operating system has reaped the process, so a check made afterwards
    cannot be beaten by a child that is merely slow.
    """
    children = []

    class _Recording(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    monkeypatch.setattr(subprocess, "Popen", _Recording)
    return children


def _assert_all_killed(children, timeout=10.0):
    """Every recorded child has exited, and not by finishing its work (exit 0)."""
    assert children, "no child process was started"
    for child in children:
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(f"child {child.pid} still alive {timeout} s after the cancellation")
        assert child.returncode != 0, "the child ran to completion instead of being killed"


def _models(tmp_path, count, prefix="R"):
    """@brief Crea modelli STEP finti in una libreria KiCad di prova."""
    folder = tmp_path / "lib" / "Resistor_SMD.3dshapes"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = folder / f"{prefix}_{index}.step"
        path.write_text("STEP finto", encoding="utf-8")
        paths.append(path)
    return paths


# ── measure_models: la rinuncia uccide il figlio e salva il bottino ───────

def test_a_cancelled_batch_stops_promptly_and_never_tries_the_rest(monkeypatch, tmp_path):
    """@brief Positivo: la misura si ferma subito, non a fine lotto."""
    # 0.6 s per modello contro i 0.1 s del giro di guardia: sei volte il
    # ritardo massimo dichiarato, cosi' l'uccisione cade DENTRO la seconda
    # misura anche su una macchina carica, e il test non diventa un cronometro.
    fake = _arm_fake_measurer(monkeypatch, tmp_path, interval=0.6)
    models = _models(tmp_path, 6)
    cancel = threading.Event()
    _cancel_after(fake, 2, cancel)

    started = time.monotonic()
    with pytest.raises(km.MatchingCancelled) as stopped:
        km.measure_models(models, cancel=cancel)
    elapsed = time.monotonic() - started

    # Sei modelli a 0.6 s l'uno sono 3.6 s di lavoro: uscire in meno di due
    # significa non aver aspettato la fine del lotto.
    assert elapsed < 2.0, "la rinuncia ha aspettato la fine del lotto"
    attempted = _attempted(fake)
    assert attempted == [str(models[0]), str(models[1])]
    for never in models[2:]:
        assert str(never) not in attempted, "misurato un modello dopo la rinuncia"
    # La misura gia' conclusa viaggia nell'eccezione: e' pagata, non si butta.
    assert stopped.value.measured == {
        str(models[0]): {"total": [1.6, 0.8, 0.4], "body": [1.6, 0.8, 0.4]}}
    assert not fake.flag.exists(), "il lotto e' arrivato in fondo: non e' stato ucciso"


def test_the_child_of_an_in_flight_measurement_is_killed_not_abandoned(
        monkeypatch, tmp_path):
    """@brief Positivo: il processo muore, non continua a lavorare di nascosto.

    Un figlio soltanto abbandonato finirebbe comunque il lotto e scriverebbe
    il file di fine: qui si aspetta PIU' del lavoro che gli restava e quel
    file non deve comparire mai.
    """
    fake = _arm_fake_measurer(monkeypatch, tmp_path, interval=0.5)
    models = _models(tmp_path, 3)
    cancel = threading.Event()
    _cancel_after(fake, 1, cancel)
    children = _record_children(monkeypatch)

    with pytest.raises(km.MatchingCancelled):
        km.measure_models(models, cancel=cancel)

    # Proven on the process handle, not by sleeping: once the child is reaped
    # (killed, non-zero exit) nothing can write the end-of-batch file later.
    _assert_all_killed(children)
    assert not fake.flag.exists(), "il figlio ha continuato a lavorare dopo l'uccisione"
    assert _attempted(fake) == [str(models[0])]


def test_a_cancellation_asked_before_the_batch_starts_no_child_at_all(
        monkeypatch, tmp_path):
    """@brief Positivo: chi ha gia' rinunciato non paga nemmeno l'avvio."""
    fake = _arm_fake_measurer(monkeypatch, tmp_path, interval=0.1)
    models = _models(tmp_path, 3)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(km.MatchingCancelled) as stopped:
        km.measure_models(models, cancel=cancel)

    assert stopped.value.measured == {}
    assert not fake.attempts.exists(), "e' stato avviato un figlio da uccidere subito"


def test_the_cancellation_is_the_projects_one_so_a_single_clause_catches_it(
        monkeypatch, tmp_path):
    """@brief Positivo: stessa famiglia della rinuncia del solido.

    Chi costruisce il 3D deve poter scrivere UNA clausola per le due meta'
    della fase (abbinamento e costruzione), altrimenti la rinuncia
    dell'abbinamento passerebbe per un difetto della libreria.
    """
    _arm_fake_measurer(monkeypatch, tmp_path, interval=0.1)
    models = _models(tmp_path, 2)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(ProcessCancelled):
        km.measure_models(models, cancel=cancel)
    assert issubclass(km.MatchingCancelled, ProcessCancelled)


def test_without_a_channel_or_with_one_never_set_nothing_changes(monkeypatch, tmp_path):
    """@brief Negativo: senza rinuncia tutte le misure girano, e con lo stesso esito.

    E' il verso che rende utili i positivi: la differenza deve venire dal
    canale, non dal fatto che il codice nuovo misuri qualcosa di diverso.
    """
    fake = _arm_fake_measurer(monkeypatch, tmp_path, interval=0.01)
    models = _models(tmp_path, 4)

    without = km.measure_models(models)
    assert fake.flag.exists(), "il lotto non e' arrivato in fondo"
    assert _attempted(fake) == [str(p) for p in models]
    fake.attempts.unlink()
    fake.flag.unlink()

    with_channel = km.measure_models(models, cancel=threading.Event())

    assert fake.flag.exists()
    assert _attempted(fake) == [str(p) for p in models]
    assert json.dumps(with_channel, sort_keys=True) == json.dumps(without, sort_keys=True)
    assert without == {str(p): {"total": [1.6, 0.8, 0.4], "body": [1.6, 0.8, 0.4]}
                       for p in models}


def test_a_measurement_that_fails_for_a_real_reason_is_not_a_cancellation(
        monkeypatch, tmp_path):
    """@brief Negativo: un modello illeggibile resta un guasto del file."""
    models = _models(tmp_path, 3)
    _arm_fake_measurer(monkeypatch, tmp_path, interval=0.01,
                       broken=models[1].name)

    result = km.measure_models(models, cancel=threading.Event())

    assert result[str(models[1])] is None
    assert result[str(models[0])]["total"] == [1.6, 0.8, 0.4]
    assert result[str(models[2])]["total"] == [1.6, 0.8, 0.4]


def test_a_batch_that_cannot_even_start_is_a_failure_not_a_cancellation(
        monkeypatch, tmp_path):
    """@brief Negativo: FreeCAD assente da' il lotto vuoto di sempre.

    Nessuno ha chiesto di fermarsi: trasformare questo in una rinuncia
    fermerebbe la generazione al posto di ripiegare sulle forme approssimate.
    """
    _arm_fake_measurer(monkeypatch, tmp_path, interval=0.01)
    monkeypatch.setattr(step_generator, "find_freecadcmd",
                        lambda: str(tmp_path / "non-esiste" / "freecadcmd"))
    models = _models(tmp_path, 2)

    assert km.measure_models(models, cancel=threading.Event()) == {}


# ── model_sizes: cio' che e' stato misurato resta in cache ────────────────

def test_the_measurements_already_paid_for_stay_in_the_size_cache(monkeypatch, tmp_path):
    """@brief Positivo: la rinuncia non butta via il lavoro gia' fatto."""
    fake = _arm_fake_measurer(monkeypatch, tmp_path, interval=0.5)
    models = _models(tmp_path, 5)
    library = tmp_path / "lib"
    cancel = threading.Event()
    _cancel_after(fake, 2, cancel)

    with pytest.raises(km.MatchingCancelled):
        km.model_sizes(models, library, cancel=cancel)

    index = km.load_index(library)
    assert index[km.index_key(models[0])]["total"] == [1.6, 0.8, 0.4]
    for never in models[2:]:
        assert km.index_key(never) not in index, (
            "in cache una misura che nessuno ha mai eseguito")


def test_a_cancelled_model_sizes_returns_nothing_instead_of_a_partial_map(
        monkeypatch, tmp_path):
    """@brief Positivo: il parziale non esce dalla porta come se fosse completo.

    Restituirlo lascerebbe i modelli non misurati indistinguibili da quelli
    misurati e scartati, cioe' la firma di un verdetto inventato.
    """
    fake = _arm_fake_measurer(monkeypatch, tmp_path, interval=0.4)
    models = _models(tmp_path, 4)
    cancel = threading.Event()
    _cancel_after(fake, 2, cancel)

    with pytest.raises(km.MatchingCancelled) as stopped:
        km.model_sizes(models, tmp_path / "lib", cancel=cancel)
    assert isinstance(stopped.value.measured, dict)


def test_model_sizes_without_a_channel_keeps_its_old_signature(monkeypatch, tmp_path):
    """@brief Negativo: senza canale `measure_models` non lo vede nemmeno.

    Le sostituzioni scritte prima di questo parametro (`lambda paths: ...`)
    devono continuare a funzionare, o la novita' romperebbe chiamanti sani.
    """
    models = _models(tmp_path, 1)
    monkeypatch.setattr(km, "measure_models",
                        lambda paths: {str(p): {"total": [2.0, 1.0, 0.5]} for p in paths})

    sizes = km.model_sizes(models, tmp_path / "lib")

    assert sizes[str(models[0])]["total"] == [2.0, 1.0, 0.5]


# ── attach_models: nessun verdetto su un componente mai esaminato ─────────

def _component(name, **extra):
    return dict(dict(name=name, kind="resistor", package="0603",
                     length_mm=1.6, width_mm=0.8, bbox_basis="body"), **extra)


@pytest.fixture
def matching_library(tmp_path, monkeypatch):
    """@brief Libreria minima con un solido compatibile, senza toccare il disco vero."""
    folder = tmp_path / "lib" / "Resistor_SMD.3dshapes"
    folder.mkdir(parents=True)
    model = folder / "R_0603_1608Metric.step"
    model.write_text("STEP finto", encoding="utf-8")
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    return tmp_path / "lib", model


def test_components_never_examined_receive_no_verdict_at_all(matching_library, monkeypatch):
    """@brief Positivo: la rinuncia non scrive «nessun candidato» per nessuno."""
    library, model = matching_library
    calls = []

    def sizes(paths, root=None, *, cancel=None):
        calls.append(list(paths))
        if len(calls) > 1:
            # La seconda misura e' quella che il pulsante ha ucciso.
            raise km.MatchingCancelled(measured={})
        return {str(p): {"body": [1.6, .8, .4], "total": [1.6, .8, .4]} for p in paths}

    monkeypatch.setattr(km, "model_sizes", sizes)
    components = [_component("R1"),
                  _component("R2", model_path="vecchio.step", render_3d=False),
                  _component("R3")]
    cancel = threading.Event()

    with pytest.raises(km.MatchingCancelled):
        km.attach_models(components, library=library, cancel=cancel)

    assert components[0]["model_match"]["status"] != "unresolved"
    for unexamined in components[1:]:
        assert "model_match" not in unexamined, (
            "registrato un verdetto su un componente mai esaminato")
        assert "model_review" not in unexamined
        assert "render_placeholder" not in unexamined
    # Il componente su cui la richiesta e' caduta torna com'era: non a meta'.
    assert components[1]["model_path"] == "vecchio.step"
    assert components[1]["render_3d"] is False


def test_a_cancel_asked_before_the_phase_leaves_every_component_untouched(
        matching_library, monkeypatch):
    """@brief Positivo: canale gia' a uno = nessun componente esaminato."""
    library, _ = matching_library
    monkeypatch.setattr(km, "model_sizes",
                        lambda *a, **k: pytest.fail("misurato dopo la rinuncia"))
    components = [_component("R1"), _component("R2")]
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(km.MatchingCancelled):
        km.attach_models(components, library=library, cancel=cancel)

    assert all("model_match" not in component for component in components)
    assert all("model_path" not in component for component in components)


def test_model_candidates_refuses_to_report_on_a_cancelled_run(matching_library):
    """@brief Positivo: meglio un'eccezione che un rapporto «unresolved».

    Un rapporto vuoto e' esattamente cio' che il matcher produce quando NON
    trova modelli compatibili: usarlo anche per «non ho guardato» renderebbe
    le due cose indistinguibili per chi legge il JSON.
    """
    library, _ = matching_library
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(km.MatchingCancelled):
        km.model_candidates(_component("R1"), library=library, cancel=cancel)


def test_without_a_channel_every_component_is_examined_as_before(
        matching_library, monkeypatch):
    """@brief Negativo: senza canale l'abbinamento resta quello di sempre."""
    library, _ = matching_library
    monkeypatch.setattr(km, "model_sizes", lambda paths, root=None: {
        str(p): {"body": [1.6, .8, .4], "total": [1.6, .8, .4]} for p in paths})
    components = [_component("R1"), _component("R2"), _component("R3")]

    km.attach_models(components, library=library)

    assert all(component["model_match"]["candidates"] for component in components)
    assert all(component["model_review"] == "review_required" for component in components)


def test_a_channel_never_set_examines_everything_too(matching_library, monkeypatch):
    """@brief Negativo: il canale da solo non cambia nulla; conta chi lo alza."""
    library, _ = matching_library
    monkeypatch.setattr(km, "model_sizes", lambda paths, root=None, *, cancel=None: {
        str(p): {"body": [1.6, .8, .4], "total": [1.6, .8, .4]} for p in paths})
    components = [_component("R1"), _component("R2")]

    km.attach_models(components, library=library, cancel=threading.Event())

    assert all(component["model_match"]["candidates"] for component in components)
    assert all(component["model_review"] == "review_required" for component in components)


def test_attach_models_hands_the_very_same_channel_down(matching_library, monkeypatch):
    """@brief Positivo: il canale che arriva e' quello che scende, non una copia."""
    library, _ = matching_library
    seen = []

    def sizes(paths, root=None, *, cancel=None):
        seen.append(cancel)
        return {str(p): {"body": [1.6, .8, .4], "total": [1.6, .8, .4]} for p in paths}

    monkeypatch.setattr(km, "model_sizes", sizes)
    cancel = threading.Event()

    km.attach_models([_component("R1")], library=library, cancel=cancel)

    assert seen and all(channel is cancel for channel in seen)


# ── step_generator: una sola richiesta copre misura e costruzione ─────────

def _castellated():
    from kicadgen.generators.castellated_module import (CastellatedModuleParams,
                                                        CastellatedSide)
    return CastellatedModuleParams(
        name="Prova", body_length=20.0, body_width=10.0,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54)])


@pytest.fixture
def no_freecad(monkeypatch):
    """@brief Costruttore del solido sostituito: qui si prova la fase prima."""
    launched = []
    monkeypatch.setattr(step_generator, "_run_freecad",
                        lambda *a, **k: launched.append(k) or {"warnings": []})
    return launched


def test_a_cancel_during_the_matching_aborts_the_castellated_generation(
        monkeypatch, tmp_path, no_freecad):
    """@brief Positivo: la rinuncia esce dalla via dell'interruzione, non del guasto."""
    channels = []

    def attach(components, log=None, library=None, progress=None, *, cancel=None):
        channels.append(cancel)
        raise km.MatchingCancelled(measured={})

    monkeypatch.setattr(km, "attach_models", attach)
    cancel = threading.Event()

    with pytest.raises(step_generator.Cad3DError) as error:
        step_generator.generate_castellated_3d_model(
            _castellated(), Body3DParams(20.0, 10.0, 2.0), tmp_path, cancel=cancel)

    assert step_generator.CAD_ABORT_MESSAGE in str(error.value)
    assert channels == [cancel], "il canale non e' arrivato all'abbinamento"
    assert no_freecad == [], "il solido e' stato avviato dopo la rinuncia"


def test_a_cancel_during_the_matching_aborts_the_populated_generation(
        monkeypatch, tmp_path, no_freecad):
    """@brief Positivo: stessa promessa sull'altra via 3D (scheda popolata)."""
    from kicadgen.generators.pin_header import PinHeaderParams

    channels = []

    def attach(components, log=None, library=None, progress=None, *, cancel=None):
        channels.append(cancel)
        raise km.MatchingCancelled(measured={})

    monkeypatch.setattr(km, "attach_models", attach)
    cancel = threading.Event()
    body = Body3DParams(20.0, 10.0, 2.0, populated_board=True, components=[])

    with pytest.raises(step_generator.Cad3DError) as error:
        step_generator.generate_3d_model(
            PinHeaderParams(name="Prova"), body, tmp_path, cancel=cancel)

    assert step_generator.CAD_ABORT_MESSAGE in str(error.value)
    assert channels == [cancel]
    assert no_freecad == [], "il solido e' stato avviato dopo la rinuncia"


def test_a_library_failure_during_matching_is_still_not_an_abort(
        monkeypatch, tmp_path, no_freecad):
    """@brief Negativo: la libreria 3D resta un di piu', mai un'interruzione.

    E' il verso opposto del positivo: se ogni eccezione dell'abbinamento
    diventasse una rinuncia, un difetto della libreria fermerebbe una
    generazione che oggi ripiega sulle forme approssimate.
    """
    notes = []
    monkeypatch.setattr(km, "attach_models",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("libreria rotta")))

    step_generator.generate_castellated_3d_model(
        _castellated(), Body3DParams(20.0, 10.0, 2.0), tmp_path,
        progress=notes.append, cancel=threading.Event())

    assert no_freecad, "il solido doveva essere costruito lo stesso"
    assert any("non utilizzabile" in note for note in notes)
    assert not any(step_generator.CAD_ABORT_MESSAGE in note for note in notes)


def test_without_a_channel_the_matching_is_called_exactly_as_before(
        monkeypatch, tmp_path, no_freecad):
    """@brief Negativo: senza canale la chiamata non guadagna parametri.

    Le sostituzioni di `attach_models` scritte prima di questo lavoro
    (`lambda components, **kwargs`) devono continuare a ricevere quello che
    ricevevano, o la novita' romperebbe generazioni sane.
    """
    received = []
    monkeypatch.setattr(km, "attach_models",
                        lambda components, **kwargs: received.append(kwargs))

    step_generator.generate_castellated_3d_model(
        _castellated(), Body3DParams(20.0, 10.0, 2.0), tmp_path)

    assert received and "cancel" not in received[0]
    assert no_freecad
