"""@brief The 3D viewer is built on demand; the window starts with a placeholder (user, 2026-10-12).

Importing the viewer (pyvista, VTK, matplotlib) took six of the eight
seconds between the click on the launcher and the window. Now the window
module no longer imports it: the 3D tab holds `Preview3DPending` until the
first entry into the tab, or until the background preload, builds the real
viewer in the same place. Tests that install a stub class before building
the window keep the old synchronous path (`Preview3DVtk is not None`).
"""
from pathlib import Path

import pytest
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QWidget

import qt_fixtures
from kicadgen.gui import main_window as gui
from kicadgen.gui.preview_3d_pending import Preview3DPending
from tests.test_photo_inventory_gui import gui as _gui  # noqa: F401 - window fixture


class FakeViewer(QWidget):
    """A viewer that records what the window asks of it."""
    built = 0

    def __init__(self):
        super().__init__()
        FakeViewer.built += 1
        self.calls = []
        self.show_pin_numbers = False

    def clear_preview(self): self.calls.append(('clear',))
    def render_params(self, *args): self.calls.append(('params',))
    def render_mesh(self, path): self.calls.append(('mesh', path))
    def set_pin_numbers(self, enabled): self.calls.append(('pins', enabled)); self.show_pin_numbers = enabled
    def set_pin_functions(self, pinout): self.calls.append(('functions',))
    def set_view_cube_labels(self, labels): self.calls.append(('labels', dict(labels)))
    def fit_view(self): pass


@pytest.fixture
def pending(monkeypatch):
    """Build the window without any viewer class: the placeholder path."""
    FakeViewer.built = 0
    monkeypatch.setattr(qt_fixtures, "PreviewStub", None)
    monkeypatch.setattr(gui, "_preview_3d_class", lambda: FakeViewer)
    yield


def test_the_window_module_does_not_import_the_viewer():
    """In a fresh interpreter (this session may have imported the viewer
    elsewhere): importing the window module must leave the viewer module
    out of sys.modules and the class attribute None (review, 2026-10-12:
    the former in-process assertion could not fail)."""
    import os
    import subprocess
    import sys
    code = ("import sys; import kicadgen.gui.main_window as m; "
            "assert 'kicadgen.gui.preview_3d_vtk' not in sys.modules, 'viewer imported eagerly'; "
            "assert m.Preview3DVtk is None, 'viewer class bound eagerly'; print('lazy')")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                            cwd=str(Path(__file__).resolve().parents[1]), timeout=120)
    assert result.returncode == 0 and "lazy" in result.stdout, result.stderr[-2000:]
    # The class attribute is still there for the fixtures to replace.
    assert hasattr(gui, "Preview3DVtk")


def test_without_a_viewer_class_the_tab_starts_with_the_placeholder(pending, window):
    assert isinstance(window.preview_3d, Preview3DPending)
    assert not window.preview_3d_loaded()
    assert FakeViewer.built == 0, 'nothing heavy was built at start-up'
    # Every call the window makes while on other tabs is accepted and lost
    # on purpose: the state lives in the window, not in the viewer.
    window.preview_3d.render_mesh("whatever")
    window.preview_3d.clear_preview()
    window.preview_3d.render_params(None, None)
    window.preview_3d.set_pin_functions([])
    window.preview_3d.set_pin_numbers(True)
    assert window.preview_3d.show_pin_numbers is True


def test_ensuring_the_viewer_replaces_the_placeholder_in_place(pending, window):
    pane = window.preview_3d.parentWidget()
    layout = window._preview_3d_layout
    index = layout.indexOf(window.preview_3d)
    window.chk_pin_numbers.setChecked(True)
    assert window._ensure_preview_3d() is True
    viewer = window.preview_3d
    assert isinstance(viewer, FakeViewer) and FakeViewer.built == 1
    assert viewer.parentWidget() is pane, 'same pane: the layout tests keep holding'
    assert layout.indexOf(viewer) == index, 'same slot in the layout'
    assert viewer.show_pin_numbers is True, 'the switch of the toolbar reached the viewer'
    assert any(call[0] == 'labels' and call[1]['top'] for call in viewer.calls), 'cube labels applied'
    assert window._ensure_preview_3d() is True and FakeViewer.built == 1, 'never built twice'


def test_entering_the_3d_tab_builds_the_viewer_then_draws(pending, window, tmp_path, monkeypatch):
    assert FakeViewer.built == 0
    # A mesh generated while the placeholder was in place: the placeholder
    # drew nothing, the window kept the path, the tab entry shows it.
    mesh = tmp_path / "board.preview.json"
    mesh.write_text("{}", encoding="utf-8")
    window.preview_3d.render_mesh(str(mesh))
    window._last_mesh_path = str(mesh)
    # The deferred render only serves the tab on screen. The tab gate moves
    # a project-less window off the 3D page (its own tests elsewhere): it is
    # silenced here so the tab change reaches `_enter_3d_tab` as in the app.
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: True)
    window.tabs.setTabEnabled(gui.TAB_MODEL_3D, True)
    window.tabs.setCurrentWidget(window.model_3d_page)
    assert window.tabs.currentWidget() is window.model_3d_page
    viewer = window.preview_3d
    assert isinstance(viewer, FakeViewer)
    # The render of an existing mesh is deferred by one event round, so the
    # loading indicator paints first (`_enter_3d_tab`).
    from PyQt6.QtWidgets import QApplication
    for _ in range(20):
        if ('mesh', str(mesh)) in viewer.calls:
            break
        QApplication.processEvents()
    assert ('mesh', str(mesh)) in viewer.calls, 'the mesh sent to the placeholder is drawn now'


def test_an_import_failure_keeps_the_placeholder_and_says_so(pending, window, monkeypatch):
    def broken():
        raise ImportError("DLL load failed: vtk")
    attempts = []

    def counted():
        attempts.append(1)
        return broken()
    monkeypatch.setattr(gui, "_preview_3d_class", counted)
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text: logged.append(text))
    assert window._ensure_preview_3d() is False
    assert isinstance(window.preview_3d, Preview3DPending)
    assert window.label_3d_status.property("status_level") == gui.STATUS_ERROR
    assert "non disponibile" in window.label_3d_status.text()
    window._enter_3d_tab()  # must not raise, must not draw
    window._enter_3d_tab()
    assert isinstance(window.preview_3d, Preview3DPending)
    assert len(attempts) == 1, 'a failed import is not retried on every tab entry'
    assert sum("DLL load failed" in text for text in logged) == 1, 'logged once'
    assert window.label_3d_status.property("status_level") == gui.STATUS_ERROR, 'the status keeps saying so'


def test_a_broken_viewer_raising_any_exception_keeps_the_program_alive(pending, window, monkeypatch):
    """A pyvista/VTK mismatch raises AttributeError or RuntimeError at import,
    not ImportError: that too must stay inside the 3D tab."""
    def broken():
        raise AttributeError("module 'vtkmodules' has no attribute 'vtkRenderingOpenGL2'")
    monkeypatch.setattr(gui, "_preview_3d_class", broken)
    assert window._ensure_preview_3d() is False
    assert isinstance(window.preview_3d, Preview3DPending)


def test_a_failed_preload_is_remembered_and_the_tab_does_not_import_again(pending, window, monkeypatch):
    import importlib
    monkeypatch.setattr(importlib, "import_module", lambda name: (_ for _ in ()).throw(ImportError("no vtk")))
    attempts = []
    monkeypatch.setattr(gui, "_preview_3d_class", lambda: attempts.append(1) or FakeViewer)
    window._start_preview_3d_preload()
    loop = QEventLoop()
    window._preview_3d_preloader.failed.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert window._preview_3d_import_error == "no vtk"
    window._enter_3d_tab()
    assert attempts == [] and isinstance(window.preview_3d, Preview3DPending)


def test_the_preload_imports_off_thread_then_builds_on_the_gui_thread(pending, window, monkeypatch):
    import importlib
    import threading
    seen = {}

    def fake_import(name):
        seen['name'] = name
        seen['thread'] = threading.current_thread().name
        return object()
    monkeypatch.setattr(importlib, "import_module", fake_import)
    window._start_preview_3d_preload()
    loop = QEventLoop()
    window._preview_3d_preloader.loaded.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert 'name' in seen, 'the preload never reported within 5 s'
    assert seen['name'] == "kicadgen.gui.preview_3d_vtk"
    assert seen['thread'] == "preview-3d-preload", 'the import ran off the GUI thread'
    # The widget is not built by the preload: built inside a window on
    # screen it made the window blink for a frame (user, 2026-10-12). The
    # import is done; the first entry in the tab builds it at once.
    assert isinstance(window.preview_3d, Preview3DPending), 'preload imports, it does not build'
    assert FakeViewer.built == 0
    window._enter_3d_tab()
    assert isinstance(window.preview_3d, FakeViewer) and FakeViewer.built == 1


def test_a_stub_installed_before_the_window_keeps_the_synchronous_path(window):
    """The regular fixture: the viewer (a stub) is there from the start."""
    assert window.preview_3d_loaded()
    assert not isinstance(window.preview_3d, Preview3DPending)


def test_main_centres_shows_then_preloads_and_hands_the_splash_over(qapp, monkeypatch):
    """The entry point's order is the fix of the blink (review, 2026-10-12)."""
    calls = []

    class Account:
        balance_enabled = False

        def refresh_balance(self):
            calls.append("balance")

    class FakeWindow:
        def __init__(self):
            self.ai_account = Account()

        def centre_before_show(self): calls.append("centre")
        def show(self): calls.append("show")
        def schedule_preview_3d_preload(self): calls.append("preload")
        def _warn_missing_external_applications(self): calls.append("warn")
        def _sync_balance_provider(self): calls.append("sync")

    class FakeSplash:
        def finish(self, window): calls.append(("finish", type(window).__name__))

    scheduled = []
    monkeypatch.setattr(gui, "MainWindow", FakeWindow)
    monkeypatch.setattr(gui.QTimer, "singleShot", staticmethod(lambda ms, fn: scheduled.append((ms, fn))))
    monkeypatch.setattr(gui.QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(gui.sys, "exit", lambda code: calls.append(("exit", code)))
    sheet = qapp.styleSheet()
    try:
        gui.main(FakeSplash())
    finally:
        qapp.setStyleSheet(sheet)
    assert calls[:3] == ["centre", "show", "preload"], 'centred while hidden, shown, then the preload'
    assert ("exit", 0) in calls
    assert len(scheduled) == 1 and scheduled[0][0] == gui.SPLASH_HANDOVER_MS
    scheduled[0][1]()
    assert ("finish", "FakeWindow") in calls and "warn" in calls
    assert calls.index(("finish", "FakeWindow")) < calls.index("warn"), 'the splash goes before the warning'
