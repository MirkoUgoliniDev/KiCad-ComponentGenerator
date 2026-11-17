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


def test_terminal_worker_reports_in_the_3d_status_and_with_the_general_box(window, monkeypatch):  # noqa: F811
    """@brief The terminal worker warns in the 3D bar AND with the box every worker shows.

    @note Contract changed by review 2026-11-16, R1-6: the 3D status bar
        alone is invisible from any other tab, so the close was refused
        without a word there. The box names the terminal pass.
    """
    shown = []
    # A pass that cannot stop (no `can_cancel`, as the lead refinement) gets
    # the plain box; the stop offer has its own tests (test_review_1116_c10.py).
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: shown.append(args[1:3]))
    window._terminal_worker = _running()
    try:
        assert not _close(window)
        assert 'Attendi' in window.label_3d_status.text()
        assert [title for title, _text in shown] == ['Operazione in corso']
        assert 'terminali' in shown[0][1]
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
