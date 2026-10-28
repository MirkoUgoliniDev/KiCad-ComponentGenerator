"""@brief Shared Qt fixtures of the test suite: one QApplication and the MainWindow harness.

`tests/conftest.py` imports the fixtures defined here, so every test module can
request `qapp`, `app` and `window` without importing them. Before, 36+ modules
imported `window` from `test_photo_inventory_gui.py`, which shadowed it with
their own test arguments (545 pyflakes «redefinition of unused 'window'»).

The conftest must stay importable in an environment without PyQt6: Qt is
imported inside the fixtures (only `PreviewStub` needs it at import time, and
is simply absent without Qt), and the fixtures skip their tests instead.
"""
import gc
import os

import pytest

#: The single QApplication of the process, kept alive by this module binding.
#:
#: `QApplication.instance() or QApplication([])` in a fixture is not enough:
#: the C++ object is owned by Python, so when the local reference disappears at
#: the end of the test the QApplication is DESTROYED and the next test builds a
#: new one. Destroying it makes sip invalidate every existing wrapper while the
#: Python event filters (AnalysisOverlay installs one) create new ones: sip's
#: internal table is reallocated during its own traversal and a freed pointer
#: remains. From then on `findChildren` may return destroyed objects, and the
#: first code that touches them in bulk -- the translation pass in
#: `MainWindow.__init__` -- died of a segmentation fault at random during the
#: session (measured: about 1 run in 2 of test_photo_inventory_gui.py, 0 in 12
#: after the module binding).
#:
#: Reusing `_APP` as a FALLBACK when `QApplication.instance()` is None would be
#: the opposite of a safety net: that case happens exactly when the C++ object
#: was destroyed under a live Python reference, and a dead sip wrapper is still
#: true in an `or`. A DESTROYED QApplication would be returned and every later
#: test would die with «wrapped C/C++ object ... has been deleted».
_APP = None


def application():
    """@brief Return the process QApplication, creating and pinning it when needed.

    @return The running QApplication, also stored in the module-level `_APP`.
    @note `QApplication.instance()` is always consulted first, never `_APP`
        (see the note on `_APP`). The offscreen platform is the default, as the
        GUI test modules set it at import time.
    """
    global _APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture(scope="session")
def qapp():
    """@brief The single QApplication of the test session.

    @return The QApplication, pinned by `_APP` for the whole session.
    @note Skips the requesting test when PyQt6 is not installed.
    """
    pytest.importorskip("PyQt6")
    return application()


@pytest.fixture
def app(qapp):
    """@brief Alias of `qapp` for the modules whose tests ask for `app`.

    @param qapp The session QApplication.
    @return The same QApplication object.
    """
    return qapp


try:
    from PyQt6.QtWidgets import QWidget
except ImportError:  # Suite without Qt: the Qt fixtures skip their tests.
    QWidget = None

if QWidget is not None:
    class PreviewStub(QWidget):
        """@brief Qt stand-in of `Preview3DVtk`: no VTK render window, every call is a no-op.

        @note `window` reads this module attribute at call time, so a test may
            monkeypatch `qt_fixtures.PreviewStub` to build the window with
            another viewer (see test_gui_block_d.py).
        """
        def fit_view(self): pass
        def clear_preview(self): pass
        def render_params(self, *args): pass
        def render_mesh(self, path): pass
        def set_pin_numbers(self, checked): pass


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    """@brief MainWindow with the 3D preview stubbed, as a user who already set up the project.

    @param qapp The session QApplication.
    @param monkeypatch pytest fixture; every patch is undone after the window is destroyed.
    @param tmp_path Library root of the window.
    @return Yields the MainWindow; it is destroyed inside the test by `shutdown_main_window`.
    @note Tests that need one of the real behaviours replaced here (API key
        request, type requirement, contact style question, board size known)
        restore it themselves; those rules have their own test modules.
    """
    pytest.importorskip("pyvistaqt")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.gui import main_window as gui
    app = qapp
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    # Questi test simulano chi ha gia' configurato l'AI: dal 26/09 i comandi a
    # pagamento chiedono la chiave prima di partire (`_ensure_api_key`). Una
    # chiave finta, mai usata in rete (i lavori AI sono sostituiti nei test);
    # il caso senza chiave ha i suoi test in test_ai_account_menu.py.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fixture")
    # The suite must not depend on what is installed on the machine that
    # runs it (CI review 2026-10-27, I1). `MainWindow.__init__` probes
    # FreeCAD once (`find_freecadcmd`) and, when it is missing, starts with
    # `_freecad_available = False` and the STEP group off: on the GitHub
    # runner, which has no FreeCAD, 19 tests of the generation flow then
    # exercised the footprint-only path instead of their contract. Here the
    # probe answers as on the developer's PC; FreeCAD is never run by these
    # tests (the CAD workers are replaced). The «FreeCAD missing» branch has
    # its own tests, which set `_freecad_available = False` themselves
    # (tests/test_board_pipeline.py, tests/test_generated_files_alignment.py).
    monkeypatch.setattr(gui, "find_freecadcmd", lambda: "freecadcmd")
    w = gui.MainWindow()
    # Geometry/lifecycle tests inject their document selection independently of
    # the guided uploader; uploader integration has dedicated tests.
    def selected_documents():
        paths = [w._docs_dir() / w.docs_list.item(i).text() for i in range(w.docs_list.count())
                 if w.docs_list.item(i).checkState() == Qt.CheckState.Checked]
        if not paths:
            raise ValueError('Nessun documento nel test.')
        return paths, None
    monkeypatch.setattr(w, '_selected_documents', selected_documents)
    # Dal 26/09 l'analisi non parte senza tipologia (_extract_block_reason).
    # Questi test riguardano altro e girano con la tipologia «(non
    # specificata)» di sempre: qui conta solo il nome. La regola ha i suoi
    # test in test_type_required.py, che rimettono il metodo vero.
    # Anche le quote della scheda: dal 26/09 sono «ignote» finche' un'analisi o
    # l'utente non le dichiarano, e il pretest le chiederebbe con una modale.
    # Questi test partono con le quote dei campi, come prima; il caso delle
    # quote ignote ha i suoi test in test_board_size_known.py.
    w._board_size_known = True
    # Dal 26/09 lo stile dei contatti castellated che la foto non mostra si
    # CHIEDE all'utente con una piccola finestra modale. Questi test simulano
    # chi risponde «Mezzo foro + foro passante», lo stile che prima veniva
    # messo da solo: i loro contratti restano quelli di prima. La domanda, la
    # rinuncia e il blocco hanno i loro test in test_contact_style_gui.py.
    monkeypatch.setattr(w, '_ask_contact_style', lambda sides: gui.default_contact_style())
    monkeypatch.setattr(w, '_extract_block_reason',
                        lambda: "" if w._component_name() else
                        "Crea o apri prima un progetto (menu File).")
    # Il campo del nome nasce vuoto e senza nome non si carica ne' si estrae
    # nulla: questi test riguardano altro, quindi partono come chi ha gia'
    # scritto il nome. Il blocco ha i suoi test dedicati.
    w.field_name.setText("Componente_di_prova")
    yield w
    shutdown_main_window(w, app)


def shutdown_main_window(w, app):
    """@brief Destroy a test MainWindow now, inside the test that owns it.

    @param w Window to dispose of (it may already be closed or deleted).
    @param app The running QApplication.
    @note Order matters (HANDOFF 113): the QThread workers the window owns are
        joined first, because closing refuses and destroying aborts while one
        runs; then the window is closed (signals of its children blocked by
        `closeEvent`), deleted with deleteLater and the deferred deletion is
        delivered, and a garbage collection frees the Python side. Otherwise
        the collector destroyed the window during a later test, whose fakes
        were then called by the dying widgets. A worker that does not stop in
        time keeps the window alive for the session instead of crashing it.
    @note The window is NOT frozen (2026-10-15). pytest still references it
        here (`item.funcargs`), so the old gc.freeze() pinned its whole Python
        graph, and the sip wrappers that graph kept, for the rest of the
        session. Instead the attributes of the destroyed window are dropped
        (`_release_destroyed_window`), so what is reachable ONLY through them
        is collected right here, while the test's fakes and guards are still
        installed. Not everything goes now: objects the test or its other
        fixtures still reference survive, and the `monkeypatch` fixture,
        torn down after this one, writes the attributes it patched on the
        window back onto the cleared wrapper (the class's bound methods, or
        whatever instance value the test replaced), which recreates small
        window -> value -> window cycles. Those, and the wrapper itself, are
        collected once pytest releases the test, by the guarded post-test
        collection (`conftest.request_post_test_collection`).
    """
    from PyQt6 import sip
    from PyQt6.QtCore import QEvent, QThread
    from PyQt6.QtWidgets import QApplication
    from conftest import (_GRAVEYARD, _THREAD_LEAKS, _current_test, join_qthreads,
                          request_post_test_collection)
    if sip.isdeleted(w):
        _release_destroyed_window(w)
        gc.collect()
        request_post_test_collection()
        return
    workers = [value for value in vars(w).values() if isinstance(value, QThread)]
    workers += [worker for worker in (getattr(w, "_abandoned_workers", None) or [])
                if isinstance(worker, QThread)]
    leaked = join_qthreads(workers)
    app.processEvents()
    if leaked:
        # Deliberately kept alive, graph included: freeing a running QThread
        # is a qFatal, and its slots may still reach the window's attributes.
        _GRAVEYARD.append(w)
        _GRAVEYARD.extend(leaked)
        _THREAD_LEAKS.append((_current_test(), "MainWindow workers: " + ", ".join(
            type(worker).__qualname__ for worker in leaked)))
        return
    w.close()
    app.processEvents()
    if not sip.isdeleted(w):
        w.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()
    _release_destroyed_window(w)
    del w
    gc.collect()
    request_post_test_collection()


def _release_destroyed_window(w) -> None:
    """Drop the Python attributes of a window whose C++ object is already destroyed.

    The attributes (dialogs, scenes, kept graphics items, workers, closures)
    are reachable from the wrapper, and the wrapper from pytest until the
    test is over: without this they could only be collected between tests,
    with the test's monkeypatches already undone and no network guard. Only
    what no other live object references is freed by the caller's
    collection; values monkeypatch restores later land back on the wrapper
    and are freed after the test (see `shutdown_main_window`). A window that
    still exists on the C++ side is left untouched, because its widgets may
    still call back into those attributes.
    """
    from PyQt6 import sip
    if not sip.isdeleted(w):
        return
    try:
        vars(w).clear()
    except TypeError:       # a wrapper without __dict__ holds nothing to drop
        pass
