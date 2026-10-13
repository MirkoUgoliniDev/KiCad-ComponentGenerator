"""@brief No widget other than the window itself may appear as a top-level window while the GUI is built.

A widget without a parent that receives `setVisible(True)` is, for the
platform, a window of its own: at start-up the user saw small windows flash
and vanish (2026-10-13). They were the rows of the models form, shown by
`setRowVisible` before the form had a widget, and the palette groups of the
symbol tab, shown before being added to their panel. The watcher below
catches the whole class: any Show event delivered to a widget that is a
window and is not one of the roots under test.
"""
import pytest

from qt_fixtures import shutdown_main_window

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEvent, QObject  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402


class StrayWindowWatcher(QObject):
    """@brief Application event filter recording every widget shown as a top-level window."""

    def __init__(self, app):
        super().__init__()
        self.shown = []
        app.installEventFilter(self)

    def eventFilter(self, obj, event):
        """@brief Record Show events of widgets that are windows; never consume anything."""
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            self.shown.append(obj)
        return False

    def strays(self, *roots):
        """@brief The recorded windows that are not one of `roots`, as readable descriptions."""
        return [f"{type(w).__name__} name={w.objectName()!r} size={w.width()}x{w.height()}"
                for w in self.shown if all(w is not root for root in roots)]


@pytest.fixture
def watcher(qapp):
    watcher = StrayWindowWatcher(qapp)
    yield watcher
    qapp.removeEventFilter(watcher)


def test_the_watcher_sees_an_orphan_widget_made_visible(watcher, qapp):
    """Negative control: the detector itself must notice an unparented widget shown."""
    orphan = QLabel("orphan")
    orphan.setVisible(True)
    QApplication.processEvents()
    try:
        assert watcher.strays() == [f"QLabel name='' size={orphan.width()}x{orphan.height()}"]
    finally:
        orphan.close()
        orphan.deleteLater()


def test_a_parented_widget_made_visible_is_not_a_window(watcher, qapp):
    """A child made visible inside a hidden parent is not shown as a window."""
    parent = QWidget()
    child = QLabel("child", parent)
    child.setVisible(True)
    QApplication.processEvents()
    assert watcher.strays() == []
    parent.deleteLater()


def test_building_the_main_window_shows_no_other_window(qapp, monkeypatch, tmp_path):
    """The whole MainWindow construction, start-up refreshes included, opens no stray window."""
    watcher = StrayWindowWatcher(qapp)
    try:
        from PyQt6.QtWidgets import QMessageBox
        import qt_fixtures
        from kicadgen.gui import main_window as gui
        monkeypatch.setattr(gui, "Preview3DVtk", qt_fixtures.PreviewStub)
        monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
        monkeypatch.setattr(gui, "save_config", lambda config: None)
        monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
        monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fixture")
        window = gui.MainWindow()
        try:
            QApplication.processEvents()
            assert watcher.strays(window) == []
        finally:
            shutdown_main_window(window, qapp)
    finally:
        qapp.removeEventFilter(watcher)


def test_building_the_symbol_tab_shows_no_other_window(watcher, qapp):
    """The palette groups are mounted before being shown, at construction and at retranslate."""
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    try:
        tab.retranslate()
        QApplication.processEvents()
        assert watcher.strays(tab) == []
    finally:
        tab.deleteLater()
