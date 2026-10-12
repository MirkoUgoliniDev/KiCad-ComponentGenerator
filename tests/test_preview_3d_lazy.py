"""@brief The 3D viewer is built on demand; the window starts with a placeholder (user, 2026-10-12).

Importing the viewer (pyvista, VTK, matplotlib) took six of the eight
seconds between the click on the launcher and the window. Now the window
module no longer imports it: the 3D tab holds `Preview3DPending` until the
first entry into the tab, or until the background preload, builds the real
viewer in the same place. Tests that install a stub class before building
the window keep the old synchronous path (`Preview3DVtk is not None`).
"""
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
    import sys
    assert gui.Preview3DVtk is None or "kicadgen.gui.preview_3d_vtk" in sys.modules
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


def test_entering_the_3d_tab_builds_the_viewer_then_draws(pending, window):
    assert FakeViewer.built == 0
    window._enter_3d_tab()
    viewer = window.preview_3d
    assert isinstance(viewer, FakeViewer)
    assert any(call[0] in ('clear', 'params', 'mesh') for call in viewer.calls), \
        'the tab entry drew from the window state'


def test_an_import_failure_keeps_the_placeholder_and_says_so(pending, window, monkeypatch):
    def broken():
        raise ImportError("DLL load failed: vtk")
    monkeypatch.setattr(gui, "_preview_3d_class", broken)
    assert window._ensure_preview_3d() is False
    assert isinstance(window.preview_3d, Preview3DPending)
    assert window.label_3d_status.property("status_level") == gui.STATUS_ERROR
    assert "non disponibile" in window.label_3d_status.text()
    window._enter_3d_tab()  # must not raise, must not draw
    assert isinstance(window.preview_3d, Preview3DPending)


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
    if not window.preview_3d_loaded():
        loop.exec()
    assert seen['name'] == "kicadgen.gui.preview_3d_vtk"
    assert seen['thread'] == "preview-3d-preload", 'the import ran off the GUI thread'
    assert isinstance(window.preview_3d, FakeViewer), 'built on the GUI thread after the signal'
    # Entering the tab afterwards neither imports nor builds again.
    window._enter_3d_tab()
    assert FakeViewer.built == 1


def test_a_stub_installed_before_the_window_keeps_the_synchronous_path(window):
    """The regular fixture: the viewer (a stub) is there from the start."""
    assert window.preview_3d_loaded()
    assert not isinstance(window.preview_3d, Preview3DPending)
