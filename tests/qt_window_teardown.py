"""@brief Shared helpers that keep MainWindow tests away from native teardown crashes.

A MainWindow left to the garbage collector is destroyed at an arbitrary later
moment, possibly inside another test and possibly with the real VTK render
window still attached: a known source of random native crashes of the suite
(HANDOFF point 113). Tests that do not verify the 3D preview replace it with
the same Qt stub used by the shared `window` fixture of
`qt_fixtures.py`, and every window built by a test is destroyed
explicitly before the test ends.
"""
import gc

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication

from qt_fixtures import PreviewStub


def stub_3d_preview(monkeypatch):
    """@brief Replace the real VTK preview of MainWindow with the shared Qt stub.

    @param monkeypatch pytest fixture; the original class is restored at teardown.
    @note Only for tests that are not about the 3D preview itself.
    """
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)


def dispose_window(widget):
    """@brief Close a top-level widget and destroy it now, not at a later GC pass.

    @param widget Top-level Qt widget built by the test (typically a MainWindow).
    @throws AssertionError When the widget refuses to close (a worker still
        running): destroying it anyway would abort the process, so the test
        must fail loudly instead.
    @note Deferred deletions are delivered explicitly and a garbage collection
        runs afterwards, so no wrapper of the destroyed window survives into
        the next test.
    """
    app = QApplication.instance()
    assert widget.close(), "the window refused to close"
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    if app is not None:
        app.processEvents()
    gc.collect()
