"""@brief Il cablaggio fra il worker fermo e la vista di revisione.

La vista e' gia' provata da sola; qui si verifica cio' che sta fra le due
sponde, che e' la parte in cui si sbaglia: il worker si ferma su un evento in
attesa di una risposta, e chi non gliela consegna lo lascia appeso per sempre.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QDialog

from kicadgen.gui import main_window as mw
from kicadgen.gui.main_window import ExtractWorker, MainWindow
import kicadgen.gui.yolo_review_dialog as review_module
from qt_window_teardown import dispose_window, stub_3d_preview


def _proposals(count=3):
    return [dict(_id=index, _box=(.1, .1, .2, .2), name=f'c{index}',
                 _yolo_class='resistor', _yolo_confidence=.5, _yolo_review_only=False)
            for index in range(1, count + 1)]


@pytest.fixture
def window(qapp, monkeypatch):
    """@brief Shown MainWindow with a stubbed 3D preview, destroyed at teardown.

    The wiring under test never touches the VTK preview, so the real render
    window is replaced and the window is destroyed explicitly instead of being
    left to the garbage collector.
    """
    app = qapp
    stub_3d_preview(monkeypatch)
    widget = MainWindow()
    widget.show()
    app.processEvents()
    widget._extract_worker = ExtractWorker([], photo_detector='yolo')
    widget.analysis_overlay.start()
    app.processEvents()
    yield widget, widget._extract_worker, app
    dispose_window(widget)


def _answer_with(monkeypatch, outcome, kept=None):
    """@brief Sostituisce la vista con una che risponde come si vuole."""
    class _Fixed(QDialog):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.given = args

        def exec(self):
            return outcome

        def kept(self):
            return list(kept or [])

    monkeypatch.setattr(review_module, 'YoloReviewDialog', _Fixed)


def test_the_worker_hands_over_the_photo_and_the_calibration_and_waits(window):
    """@brief Le quote viaggiano con la richiesta, non si leggono dai campi.

    Durante l'estrazione i campi dell'interfaccia non sono ancora compilati: un
    corpo aggiunto a mano finirebbe su una scala diversa da quelli del
    rilevatore.
    """
    _, worker, _ = window
    seen = {}

    def answer(png, proposals, length, width):
        seen.update(png=png, count=len(proposals), length=length, width=width)
        worker.deliver_review(proposals[:1])

    worker.review_requested.connect(answer)
    kept = worker._review_proposals(b'\x89PNG', _proposals(), 63.3, 25.4)
    assert seen == dict(png=b'\x89PNG', count=3, length=63.3, width=25.4)
    assert len(kept) == 1


def test_accepting_returns_the_kept_bodies_and_brings_the_modal_back(window, monkeypatch):
    widget, worker, app = window
    _answer_with(monkeypatch, QDialog.DialogCode.Accepted, kept=[{'_id': 1}])
    widget._on_review_requested(b'', _proposals(), 63.3, 25.4)
    app.processEvents()
    assert worker._review_result == [{'_id': 1}]
    assert widget.analysis_overlay.isVisible()


def test_cancelling_delivers_nothing_so_the_paid_call_never_starts(window, monkeypatch):
    """@brief Caso negativo del precedente: annullare non e' tenere tutto."""
    widget, worker, app = window
    _answer_with(monkeypatch, QDialog.DialogCode.Rejected)
    widget._on_review_requested(b'', _proposals(), 63.3, 25.4)
    app.processEvents()
    assert worker._review_result is None
    # Nulla prosegue, quindi la modale dell'analisi non deve tornare su.
    assert not widget.analysis_overlay.isVisible()


def test_a_broken_review_lets_the_analysis_finish_instead_of_hanging(window, monkeypatch):
    """@brief Un difetto della revisione non deve buttare via l'analisi pagata.

    Il worker e' fermo su un evento: senza una risposta non finirebbe mai.
    """
    widget, worker, app = window

    class _Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('vista rotta')

    monkeypatch.setattr(review_module, 'YoloReviewDialog', _Broken)
    proposals = _proposals()
    widget._on_review_requested(b'', proposals, 63.3, 25.4)
    app.processEvents()
    assert worker._review_result == proposals
    assert widget.analysis_overlay.isVisible()
