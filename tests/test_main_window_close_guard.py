"""@brief Chiusura della finestra principale con un worker in corso.

Un QThread distrutto mentre gira abortisce il processo: `closeEvent` deve
rifiutare la chiusura per QUALUNQUE worker attivo (estrazione, rilancio,
generazione, CAD 3D, terminali), non solo per quello dei terminali. Un tempo
esistevano due `closeEvent` e il secondo oscurava il primo.
"""
import os
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox

from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


def _running():
    """@brief Un worker finto che dichiara di essere in esecuzione."""
    return SimpleNamespace(isRunning=lambda: True)


def _close(window):  # noqa: F811 - fixture importata
    """@brief Consegna un evento di chiusura gia' accettato e dice se e' sopravvissuto."""
    event = QCloseEvent()
    event.accept()
    window.closeEvent(event)
    return event.isAccepted()


@pytest.mark.parametrize('slot', ['_extract_worker', '_reconcile_worker', '_worker', '_cad3d_worker'])
def test_close_is_refused_while_a_worker_runs(window, monkeypatch, slot):  # noqa: F811
    """@brief Positivo: ogni slot di worker attivo blocca la chiusura con un avviso."""
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: shown.append(args[1]))
    setattr(window, slot, _running())
    try:
        assert not _close(window)
        assert shown == ['Operazione in corso']
        assert getattr(window, slot) is not None, 'il worker non va toccato'
    finally:
        setattr(window, slot, None)


def test_terminal_worker_reports_in_the_3d_status_before_the_general_guard(window, monkeypatch):  # noqa: F811
    """@brief Il worker terminali avvisa nella barra 3D e non apre la modale generale."""
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: shown.append(args[1]))
    window._terminal_worker = _running()
    try:
        assert not _close(window)
        assert 'Attendi' in window.label_3d_status.text()
        assert shown == [], 'nessuna modale: il messaggio sta nella barra di stato 3D'
    finally:
        window._terminal_worker = None


def test_close_is_accepted_when_nothing_runs(window, monkeypatch):  # noqa: F811
    """@brief Negativo: senza worker la chiusura passa, anche con worker gia' terminati."""
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: shown.append(args[1]))
    window._extract_worker = SimpleNamespace(isRunning=lambda: False)
    try:
        event = QCloseEvent()
        event.ignore()
        window.closeEvent(event)
        assert event.isAccepted()
        assert shown == []
    finally:
        window._extract_worker = None


def test_main_window_defines_close_event_once():
    """@brief Regressione: una seconda definizione oscurerebbe la prima in silenzio."""
    import inspect
    from kicadgen.gui import main_window as gui
    source = inspect.getsource(gui.MainWindow)
    assert source.count('def closeEvent(') == 1
