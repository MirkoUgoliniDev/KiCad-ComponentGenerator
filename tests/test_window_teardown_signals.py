"""@brief Chiudere la finestra principale ne rende sicura la distruzione (indagine del 25/09).

Segfault 139 intermittente all'uscita della suite: distruggendo la
MainWindow, i figli emettevano segnali (rowsRemoved, modelReset, textChanged,
currentChanged) collegati a slot Python che giravano su widget a meta'
distruzione. Riprodotto in modo deterministico distruggendo il «Log analisi
AI» con log ed elementi pieni. La distruzione vera avviene in un processo a
parte: un crash qui abbatterebbe l'intera suite invece di far fallire un test.
"""
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QObject


ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = r"""
import faulthandler, sys
faulthandler.enable()
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from kicadgen.gui import main_window as gui
w = gui.MainWindow()
w.ai_progress_text.setPlainText("passo 1\npasso 2")
for text in ("C1", "R1", "U1"):
    w.ai_elements_list.addItem(text)
w.close()
# L'ordine che all'uscita dell'interprete puo' capitare a caso: prima i
# dialoghi figli, poi la finestra.
for dialog in (w.ai_log_dialog, w.project_info_dialog, w.component_prompt_dialog):
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
w.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
app.processEvents()
print("DESTROYED-OK", flush=True)
"""


def test_a_closed_window_with_a_full_ai_log_is_destroyed_without_crashing(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT),
               KICADGEN_CONFIG_PATH=str(tmp_path / "config.json"),
               KICADGEN_PROFILE_DB=str(tmp_path / "profiles.sqlite3"),
               KICADGEN_MATCH_DB=str(tmp_path / "accepted.sqlite3"))
    result = subprocess.run([sys.executable, "-c", _SCRIPT], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "DESTROYED-OK" in result.stdout


def test_closing_blocks_the_signals_of_every_child(window):  # noqa: F811
    window.close()
    children = window.findChildren(QObject)
    assert children and all(child.signalsBlocked() for child in children)


def test_a_refused_close_keeps_the_window_working(window, monkeypatch):  # noqa: F811
    """@brief Negativo: con un lavoro in corso la chiusura e' rifiutata e nulla si blocca."""
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    monkeypatch.setattr("kicadgen.gui.main_window.QMessageBox.information", lambda *a: None)
    window.close()
    assert not window.ai_elements_list.model().signalsBlocked()
    assert not window.tabs.signalsBlocked()
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
