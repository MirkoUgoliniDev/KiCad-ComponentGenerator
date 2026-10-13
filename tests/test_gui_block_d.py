"""@brief Block D of HANDOFF 130/131: workers module, off-thread mesh, project log handle, VTK close.

- T6/G5: the QThread workers live in `kicadgen.gui.workers`, are re-exported
  by `main_window`, and a collaborator replaced on `main_window` still reaches
  the running worker; GenerateWorker and Cad3DWorker share one 3D base.
- G8: `Preview3DVtk.prepare_mesh` parses the preview JSON in the thread pool;
  only the latest request is served, stale results are dropped, errors are
  reported; the window keeps its loading indicator until the answer.
- G8 (log): the project log is kept open while a worker streams lines and
  closed otherwise, on rename and on window close.
- G9: an accepted close closes the 3D viewer explicitly; a refused one does not.
"""
import json
import os
import threading
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import pyqtSignal  # noqa: E402
from PyQt6.QtTest import QSignalSpy, QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import qt_fixtures as shared  # noqa: E402 - its PreviewStub builds the shared window

from kicadgen.gui import main_window as gui  # noqa: E402
from kicadgen.gui import workers  # noqa: E402

WORKER_NAMES = ("ExtractWorker", "YoloPretestWorker", "GenerateWorker", "Cad3DWorker",
                "ReconcileWorker", "TerminalDetailWorker", "UpdateCheckWorker")


def _wait_until(condition, timeout_ms=5000):
    """Process events until @p condition holds or the time runs out; return the outcome."""
    waited = 0
    while not condition() and waited < timeout_ms:
        QTest.qWait(10)
        waited += 10
    return condition()


# ── T6/G5: workers module ────────────────────────────────────────────────

def test_every_worker_is_reexported_by_main_window():
    """@brief Positivo: `gui.<Worker>` e' lo stesso oggetto definito in workers."""
    for name in WORKER_NAMES:
        assert getattr(gui, name) is getattr(workers, name), name
        assert getattr(workers, name).__module__ == "kicadgen.gui.workers"


def test_the_two_cad_workers_share_one_base_and_do_not_redefine_it():
    """@brief Strutturale: i metodi comuni esistono una volta sola, nella base."""
    shared_methods = ("_cancel_kwargs", "request_abort", "_model_progress", "_cad_progress",
                      "_generate_model")
    for cls in (gui.GenerateWorker, gui.Cad3DWorker):
        assert issubclass(cls, workers._Cad3DRunWorker)
        for method in shared_methods:
            assert method not in vars(cls), f"{cls.__name__}.{method} duplicato"
    # Negativo: gli altri worker non ereditano la base del 3D.
    assert not issubclass(gui.ExtractWorker, workers._Cad3DRunWorker)


def _pin_header():
    from kicadgen.generators.pin_header import PinHeaderParams
    return PinHeaderParams(name="X", rows=1, cols=2, pitch=2.54, row_spacing=2.54,
                           pad_diameter=1.7, drill=1.0)


class _Footprint:
    """Stand-in for core.footprint.Footprint with the calls the worker makes."""
    def __init__(self):
        self.saved = []
        self.texts = []
        self.graphics = []

    def place_texts(self, positions):
        # The worker places Reference/Value before saving (since b99f835).
        self.texts.append(dict(positions))

    def save(self, path):
        self.saved.append(path)


def test_a_builder_replaced_on_main_window_reaches_the_worker(monkeypatch, tmp_path):
    """@brief Positivo: la sostituzione su main_window e' quella che il worker chiama.

    Negativo nello stesso test: senza corpo 3D il generatore 3D non viene chiamato.
    """
    footprint = _Footprint()
    monkeypatch.setattr(gui, "build_pin_header", lambda params: footprint)
    monkeypatch.setattr(gui, "_generate_3d",
                        lambda *a, **k: pytest.fail("3D chiamato senza corpo"))
    worker = gui.GenerateWorker(_pin_header(), tmp_path, body=None)
    done = QSignalSpy(worker.done)
    worker.run()  # stessa chiamata del thread, qui sincrona
    assert footprint.saved == [str(tmp_path / "X.pretty" / "X.kicad_mod")]
    assert [row[0] for row in done] == footprint.saved


def test_the_3d_generator_replaced_on_main_window_reaches_both_workers(monkeypatch, tmp_path):
    """@brief Positivo per GenerateWorker e Cad3DWorker; negativo: l'errore CAD va al segnale d'errore."""
    from kicadgen.cad3d.body_params import Body3DParams
    from kicadgen.cad3d.step_generator import Cad3DError

    calls = []

    def generator(params, body, project_dir, fillets, face_texts, progress=None,
                  model_progress=None, cancel=None):
        calls.append(cancel)
        return {"step_path": tmp_path / "x.step", "notes": ["nota"], "warnings": []}

    monkeypatch.setattr(gui, "_generate_3d", generator)
    monkeypatch.setattr(gui, "build_pin_header", lambda params: _Footprint())
    body = Body3DParams(length_mm=10, width_mm=5, height_mm=2)
    full = gui.GenerateWorker(_pin_header(), tmp_path, body)
    full_done = QSignalSpy(full.cad3d_done)
    full.run()
    regen = gui.Cad3DWorker(_pin_header(), body, tmp_path, [], [])
    regen_done = QSignalSpy(regen.done)
    regen.run()
    assert calls == [full.cancel, regen.cancel], "ogni corsa col proprio canale"
    assert [row[0] for row in full_done] == [str(tmp_path / "x.step")]
    assert [row[0] for row in regen_done] == [str(tmp_path / "x.step")]

    def broken(*args, **kwargs):
        raise Cad3DError("FreeCAD assente")

    monkeypatch.setattr(gui, "_generate_3d", broken)
    failing = gui.Cad3DWorker(_pin_header(), body, tmp_path, [], [])
    errors = QSignalSpy(failing.error)
    done = QSignalSpy(failing.done)
    failing.run()
    assert [row[0] for row in errors] == ["FreeCAD assente"]
    assert len(done) == 0


# ── G8: off-thread preparation in the viewer ─────────────────────────────

_TRIANGLE = dict(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], triangles=[[0, 1, 2]])


def _mesh_file(path, shift=0.0):
    part = dict(name="Body", color="#123456", triangles=_TRIANGLE["triangles"],
                vertices=[[x + shift, y, z] for x, y, z in _TRIANGLE["vertices"]])
    path.write_text(json.dumps({"parts": [part]}), encoding="utf-8")
    return path


@pytest.fixture
def view(qapp):
    from kicadgen.gui.preview_3d_vtk import Preview3DVtk
    app = qapp
    widget = Preview3DVtk()
    widget.resize(300, 200)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


@pytest.fixture
def gated_prepare(monkeypatch):
    """Replace the scene preparation with one that waits for a per-file gate."""
    from kicadgen.gui import preview_3d_vtk as module
    real = module._prepare_mesh_scene
    gates, threads = {}, []

    def prepare(path):
        threads.append(threading.current_thread())
        gates.setdefault(Path(path).name, threading.Event()).wait(10)
        return real(path)

    monkeypatch.setattr(module, "_prepare_mesh_scene", prepare)
    gates["threads"] = threads
    yield gates
    for gate in gates.values():
        if isinstance(gate, threading.Event):
            gate.set()
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(10000)


def test_the_latest_request_wins_and_the_stale_result_is_dropped(view, gated_prepare, tmp_path):
    """@brief Positivo: la richiesta piu' recente viene servita. Negativo: quella superata no."""
    from kicadgen.gui.preview_3d_vtk import _mesh_file_key
    first, second = _mesh_file(tmp_path / "a.json"), _mesh_file(tmp_path / "b.json", 2.0)
    for name in ("a.json", "b.json"):
        gated_prepare[name] = threading.Event()
    ready = QSignalSpy(view.mesh_prepared)
    failed = QSignalSpy(view.mesh_prepare_failed)
    old = view.prepare_mesh(first)
    new = view.prepare_mesh(second)
    assert new > old
    assert len(ready) == 0, "la risposta arriva dopo, mai dentro prepare_mesh"
    gated_prepare["b.json"].set()
    assert _wait_until(lambda: len(ready) == 1)
    gated_prepare["a.json"].set()  # il risultato vecchio arriva DOPO il nuovo
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(10000)
    QTest.qWait(50)
    assert [row[0] for row in ready] == [new], "il risultato superato non si annuncia"
    assert len(failed) == 0
    assert _mesh_file_key(second) in view._mesh_cache
    assert _mesh_file_key(first) not in view._mesh_cache, "il risultato superato non si tiene"
    assert all(thread is not threading.main_thread() for thread in gated_prepare["threads"])


def test_a_prepared_mesh_renders_without_parsing_again(view, tmp_path, monkeypatch):
    """@brief Positivo: dopo prepare_mesh, render_mesh non rilegge il file sul thread GUI."""
    from kicadgen.gui import preview_3d_vtk as module
    path = _mesh_file(tmp_path / "m.json")
    ready = QSignalSpy(view.mesh_prepared)
    view.prepare_mesh(path)
    assert _wait_until(lambda: len(ready) == 1)
    monkeypatch.setattr(module, "_prepare_mesh_scene",
                        lambda p: pytest.fail("rilettura sul thread GUI"))
    view.render_mesh(path)
    assert view.is_showing(path)
    # Gia' preparata: risposta immediata (in coda), nessun nuovo lavoro.
    token = view.prepare_mesh(path)
    assert not view._prepare_jobs
    assert _wait_until(lambda: len(ready) == 2)
    assert ready[1][0] == token


def test_a_preparation_error_is_reported_and_the_scene_stays(view, tmp_path):
    """@brief Negativo: file senza triangoli o illeggibile -> mesh_prepare_failed, scena intatta."""
    good = _mesh_file(tmp_path / "good.json")
    view.render_mesh(good)
    shown = view._shown_mesh_key
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"parts": []}), encoding="utf-8")
    failed = QSignalSpy(view.mesh_prepare_failed)
    ready = QSignalSpy(view.mesh_prepared)
    token = view.prepare_mesh(empty)
    assert _wait_until(lambda: len(failed) == 1)
    assert failed[0][0] == token and failed[0][1]
    missing = view.prepare_mesh(tmp_path / "missing.json")
    assert _wait_until(lambda: len(failed) == 2)
    assert failed[1][0] == missing
    assert len(ready) == 0
    assert view._shown_mesh_key == shown


def test_a_closed_viewer_drops_the_result(view, gated_prepare, tmp_path):
    """@brief Negativo: il risultato di un visore chiuso non viene applicato ne' annunciato."""
    path = _mesh_file(tmp_path / "c.json")
    gated_prepare["c.json"] = threading.Event()
    ready = QSignalSpy(view.mesh_prepared)
    view.prepare_mesh(path)
    view.close()
    gated_prepare["c.json"].set()
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(10000)
    QTest.qWait(50)
    assert len(ready) == 0
    assert not view._mesh_cache


# ── G8: the window waits for the viewer with the indicator on ───────────

class AsyncPreviewStub(shared.PreviewStub):
    """Test viewer with the off-thread API: answers only when the test says so."""
    mesh_prepared = pyqtSignal(int)
    mesh_prepare_failed = pyqtSignal(int, str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.rendered = []
        self.closed = 0

    def prepare_mesh(self, path):
        self.requests.append(Path(path))
        return len(self.requests)

    def render_mesh(self, path):
        self.rendered.append(Path(path))

    def close(self):
        self.closed += 1
        return super().close()


@pytest.fixture
def async_window(monkeypatch, request):
    """The shared window, built with the asynchronous test viewer."""
    monkeypatch.setattr(shared, "PreviewStub", AsyncPreviewStub)
    w = request.getfixturevalue("window")
    w._acquisition_completed = True
    w._set_acquisition_tabs_enabled(True)
    yield w
    while QApplication.overrideCursor() is not None:
        QApplication.restoreOverrideCursor()


def _indicator_shown(w):
    return w.loading_3d_indicator.isVisibleTo(w.model_3d_action_bar)


def _flush():
    for _ in range(5):
        QApplication.processEvents()


def test_tab_entry_waits_for_the_prepared_mesh(async_window, tmp_path):
    """@brief Positivo: indicatore acceso finche' la mesh non e' pronta, poi un solo rendering."""
    w = async_window
    mesh = tmp_path / "board.preview.json"
    mesh.write_text("{}", encoding="utf-8")
    w._last_mesh_path = mesh
    w.tabs.setCurrentIndex(gui.TAB_MODEL_3D)
    _flush()
    viewer = w.preview_3d
    assert viewer.requests == [mesh]
    assert viewer.rendered == [], "niente disegno prima che la mesh sia pronta"
    assert _indicator_shown(w)
    w._on_tab_changed(gui.TAB_MODEL_3D)  # un secondo ingresso non accoda nulla
    _flush()
    assert viewer.requests == [mesh]
    viewer.mesh_prepared.emit(99)  # token di un'altra richiesta: ignorato
    assert viewer.rendered == [] and _indicator_shown(w)
    viewer.mesh_prepared.emit(1)
    assert viewer.rendered == [mesh]
    assert not _indicator_shown(w)
    assert QApplication.overrideCursor() is None


def test_tab_entry_answer_after_a_data_change_is_ignored(async_window, tmp_path):
    """@brief Negativo: i dati cambiano durante la preparazione -> nessun disegno, indicatore spento."""
    w = async_window
    mesh = tmp_path / "board.preview.json"
    mesh.write_text("{}", encoding="utf-8")
    w._last_mesh_path = mesh
    w.tabs.setCurrentIndex(gui.TAB_MODEL_3D)
    _flush()
    w._invalidate_generated_mesh()
    assert not _indicator_shown(w)
    w.preview_3d.mesh_prepared.emit(1)
    assert w.preview_3d.rendered == []


def test_tab_entry_failure_is_logged_once_and_falls_back(async_window, tmp_path, monkeypatch):
    """@brief Negativo: mesh illeggibile -> una riga di log, nessun secondo tentativo sul thread GUI."""
    w = async_window
    mesh = tmp_path / "board.preview.json"
    mesh.write_text("{}", encoding="utf-8")
    w._last_mesh_path = mesh
    w.tabs.setCurrentIndex(gui.TAB_MODEL_3D)
    _flush()
    w.preview_3d.mesh_prepare_failed.emit(1, "mesh senza triangoli")
    assert w._log_plain_text().count("Anteprima mesh non disponibile: mesh senza triangoli") == 1
    assert w.preview_3d.rendered == [], "la mesh fallita non si rilegge in sincrono"
    assert not _indicator_shown(w)


def test_a_generated_mesh_is_shown_when_prepared(async_window, tmp_path):
    """@brief Positivo e negativo su `_show_generated_mesh` con un visore asincrono."""
    w = async_window
    step = tmp_path / "X.3dshapes" / "X.step"
    step.parent.mkdir()
    mesh = step.with_suffix("").with_suffix(".preview.json")
    mesh.write_text("{}", encoding="utf-8")
    previous = tmp_path / "old.preview.json"
    w._last_mesh_path = previous
    w._show_generated_mesh(step)
    assert w._last_mesh_path == mesh, "il solido scritto conta gia' come presente"
    assert w.preview_3d.rendered == [] and _indicator_shown(w)
    w.preview_3d.mesh_prepared.emit(1)
    assert w.preview_3d.rendered == [mesh]
    assert not _indicator_shown(w)
    # Negativo: la preparazione fallisce -> torna il percorso di prima, riga di log.
    w._show_generated_mesh(step)
    w.preview_3d.mesh_prepare_failed.emit(2, "file troncato")
    assert w._last_mesh_path == mesh
    w._last_mesh_path = previous
    w._show_generated_mesh(step)
    w.preview_3d.mesh_prepare_failed.emit(3, "file troncato")
    assert w._last_mesh_path == previous
    assert "Anteprima mesh non disponibile: file troncato" in w._log_plain_text()


# ── G8: project log handle ───────────────────────────────────────────────

def test_the_log_stays_open_while_a_worker_streams_lines(window, monkeypatch):  # noqa: F811
    """@brief Positivo: stesso file aperto per tutte le righe di una corsa, contenuto su disco a ogni riga."""
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window.LOG_HANDLE_IDLE_MS = 20  # la stessa politica, in un tempo da test
    window._append_log("riga-uno")
    handle = window._log_handle
    assert handle is not None
    window._append_log("riga-due")
    assert window._log_handle is handle, "nessuna riapertura a ogni riga"
    text = window._log_file().read_text(encoding="utf-8")
    assert "riga-uno" in text and "riga-due" in text, "flush a ogni riga"
    assert window._log_idle_timer.isActive()
    assert _wait_until(lambda: window._log_handle is None), "chiuso dopo l'inattivita'"
    assert handle.closed


def test_the_log_is_closed_after_each_line_when_nothing_runs(window, monkeypatch):  # noqa: F811
    """@brief Negativo: senza lavori in corso il file non resta aperto (cartella rinominabile)."""
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    window._append_log("riga-sola")
    assert window._log_handle is None
    folder = window._log_file().parent
    renamed = folder.with_name(folder.name + "-rinominata")
    folder.rename(renamed)  # su Windows fallirebbe con il log aperto
    assert (renamed / window.LOG_FILENAME).is_file()


def test_the_log_follows_a_project_change_and_a_vanished_folder(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: un altro progetto riapre sul suo file; un file sparito si riapre, cartella compresa."""
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._append_log("progetto-a")
    first = window._log_handle
    other = tmp_path / "Altro" / window.LOG_FILENAME
    monkeypatch.setattr(window, "_log_file", lambda: other)
    window._append_log("progetto-b")
    assert window._log_handle is not first and first.closed
    assert "progetto-b" in other.read_text(encoding="utf-8")

    class Vanished(type(other)):
        """Same path, reported as gone (a deleted folder, not reproducible on Windows)."""
        def exists(self, *args, **kwargs):
            return False

    second = window._log_handle
    monkeypatch.setattr(window, "_log_file", lambda: Vanished(other))
    window._append_log("dopo-la-sparizione")
    assert window._log_handle is not second and second.closed
    assert "dopo-la-sparizione" in other.read_text(encoding="utf-8")


def test_a_log_write_error_drops_the_handle(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negativo: un errore di scrittura non lascia un file a meta' da riusare."""
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._append_log("prima")
    blocker = tmp_path / "file-non-cartella"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(window, "_log_file", lambda: blocker / window.LOG_FILENAME)
    window._append_log("non scrivibile")
    assert window._log_handle is None
    assert window._log_write_failed is True


def test_rename_and_close_release_the_log(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: prima di spostare il diario e alla chiusura il file si chiude."""
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._append_log("prima-della-rinomina")
    assert window._log_handle is not None
    assert window._move_log_to(tmp_path / "Nuovo") is True
    assert "prima-della-rinomina" in (tmp_path / "Nuovo" / window.LOG_FILENAME).read_text(
        encoding="utf-8")
    window._append_log("ancora")
    assert window._log_handle is not None
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    assert window.close()
    assert window._log_handle is None


# ── G9: explicit close of the 3D viewer ─────────────────────────────────

def test_an_accepted_close_closes_the_viewer_once(async_window, monkeypatch):
    """@brief Negativo: chiusura rifiutata -> visore intatto. Positivo: accettata -> close() una volta."""
    w = async_window
    monkeypatch.setattr(w, "_background_work_running", lambda: True)
    assert not w.close()
    assert w.preview_3d.closed == 0
    monkeypatch.setattr(w, "_background_work_running", lambda: False)
    assert w.close()
    assert w.preview_3d.closed == 1


def test_a_second_request_for_the_same_file_waits_for_the_running_parse(view, gated_prepare,
                                                                        tmp_path):
    """Revisione del blocco D: la stessa mesh chiesta di nuovo mentre e' in
    preparazione non viene analizzata due volte; il risultato risponde alla
    richiesta piu' recente (negativo: un file diverso avvia un suo lavoro)."""
    path = _mesh_file(tmp_path / "d.json")
    other = _mesh_file(tmp_path / "e.json", 2.0)
    gated_prepare["d.json"] = threading.Event()
    gated_prepare["e.json"] = threading.Event()
    ready = QSignalSpy(view.mesh_prepared)
    view.prepare_mesh(path)
    latest = view.prepare_mesh(path)
    assert len(view._prepare_jobs) == 1
    gated_prepare["d.json"].set()
    assert _wait_until(lambda: len(ready) == 1)
    assert [row[0] for row in ready] == [latest]
    view.prepare_mesh(other)
    assert len(view._prepare_jobs) == 1
    assert _wait_until(lambda: len(gated_prepare["threads"]) == 2)
    gated_prepare["e.json"].set()
    assert _wait_until(lambda: len(ready) == 2)


def test_a_qt_close_event_leaves_pyvista_able_to_release_the_render_window(view):
    """Revisione del blocco D: closeEvent usava il flag `_closed` di pyvista, e
    il close() successivo di pyvistaqt tornava subito senza liberare la finestra
    di rendering. Ora il flag del visore e' suo."""
    from PyQt6.QtWidgets import QWidget
    QWidget.close(view)                       # a Qt close event, as for a top-level viewer
    assert view._viewer_closed is True
    assert not getattr(view, "_closed", False), "pyvista's own flag must stay untouched"
    view.close()                              # pyvistaqt finalisation
    assert getattr(view, "_closed", False) is True
