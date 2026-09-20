"""@brief Contratto della revisione dei corpi rilevati, prima della chiamata a pagamento.

Quello che si verifica qui non e' l'aspetto della finestra ma cio' che esce
dalla revisione: chi sta a valle riceve proposte nel formato del rilevatore, e
non deve poter distinguere un corpo aggiunto a mano da uno rilevato, se non
dalla provenienza dichiarata.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.yolo_review_dialog import MIN_NEW_BOX, YoloReviewDialog, box_pen
from kicadgen.services.photo_yolo import detector_classes

#: La scheda di prova: 40 mm lungo X, 20 mm lungo Y. Le quote servono a
#: convertire i riquadri tracciati a mano, quindi devono essere diverse fra
#: loro, altrimenti uno scambio fra i due assi passerebbe inosservato.
LENGTH_MM, WIDTH_MM = 40.0, 20.0


def _proposal(identity, box, **changes):
    x0, y0, x1, y1 = box
    row = dict(_id=identity, _box=box, name=f'detector hypothesis {identity}', kind='generic',
               x_mm=((x0 + x1) / 2 - .5) * LENGTH_MM, y_mm=((y0 + y1) / 2 - .5) * WIDTH_MM,
               length_mm=(x1 - x0) * LENGTH_MM, width_mm=(y1 - y0) * WIDTH_MM,
               height_mm=1.0, rotation_deg=0, color_hex='#303030',
               _yolo_class='resistor', _yolo_confidence=.72, _yolo_review_only=False)
    row.update(changes)
    return row


@pytest.fixture
def dialog():
    app = QApplication.instance() or QApplication([])
    proposals = [_proposal(1, (.10, .10, .30, .30)),
                 _proposal(2, (.60, .10, .80, .30), _yolo_class='capacitor',
                           _yolo_confidence=.06, _yolo_review_only=True)]
    widget = YoloReviewDialog(b"", proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    yield widget, proposals
    widget.close()


def test_the_caller_list_survives_whatever_happens_in_the_review(dialog):
    """@brief Annullare deve lasciare intatto cio' che ha prodotto il rilevatore."""
    widget, proposals = dialog
    original = [dict(row) for row in proposals]
    widget._select_row(0)
    widget._change_class('inductor')
    widget._delete_selected()
    assert proposals == original


def test_a_deleted_body_leaves_the_review_and_the_others_keep_their_shape(dialog):
    widget, proposals = dialog
    widget._select_row(0)
    widget._delete_selected()
    kept = widget.kept()
    assert [row['_id'] for row in kept] == [2]
    assert kept[0] == proposals[1]


def test_weak_and_strong_stay_distinguishable_to_the_eye_and_to_the_caller(dialog):
    """@brief Sotto soglia non vuol dire assente: chi guarda deve vederlo."""
    widget, _ = dialog
    assert widget.view.proposals[0]['_yolo_review_only'] is False
    assert widget.view.proposals[1]['_yolo_review_only'] is True
    assert 'debole' in widget.list.item(1).text()
    assert 'debole' not in widget.list.item(0).text()
    assert widget.list.item(0).foreground().color() != widget.list.item(1).foreground().color()
    assert '1 sicure' in widget.summary.text() and '1 deboli' in widget.summary.text()
    # La forma del tratto separa i due casi anche per chi non distingue i
    # colori: i deboli sono tratteggiati, gli altri no, e questo non cambia
    # quando la proposta e' quella scelta.
    strong, weak = widget.view.proposals
    assert box_pen(strong).style() == Qt.PenStyle.SolidLine
    assert box_pen(weak).style() == Qt.PenStyle.DashLine
    assert box_pen(weak, chosen=True).style() == Qt.PenStyle.DashLine
    assert box_pen(strong, chosen=True).color() != box_pen(strong).color()


def test_correcting_the_category_reaches_the_caller(dialog):
    widget, _ = dialog
    widget._select_row(1)
    widget._change_class('diode')
    assert [row['_yolo_class'] for row in widget.kept()] == ['resistor', 'diode']


def test_a_hand_drawn_body_is_measured_with_the_board_calibration(dialog):
    """@brief Un corpo aggiunto a mano deve stare sulla scala del rilevatore.

    Le quote in mm non si leggono dai campi dell'interfaccia -- durante
    l'estrazione non sono ancora compilati -- ma dalla calibrazione con cui la
    foto e' stata misurata, la stessa che ha prodotto gli altri riquadri.
    """
    widget, _ = dialog
    widget._add_box((.25, .40, .75, .50))
    added = widget.kept()[-1]
    assert added['_yolo_origin'] == 'user_added' and added['_source'] == 'revisione manuale'
    assert added['length_mm'] == pytest.approx(.5 * LENGTH_MM)   # 20 mm lungo X
    assert added['width_mm'] == pytest.approx(.1 * WIDTH_MM)     # 2 mm lungo Y
    assert added['rotation_deg'] == 0
    assert added['x_mm'] == pytest.approx(0) and added['y_mm'] == pytest.approx(-1)
    assert added['_id'] == 3 and added['_height_estimated'] and added['_uncertain']
    assert added['_height_source'] == 'estimated_placeholder_not_measured'
    # Stesse chiavi degli altri: chi sta a valle non deve avere casi speciali.
    assert set(_proposal(9, (0, 0, .1, .1))) <= set(added)


def test_a_tall_hand_drawn_body_reports_its_long_side_along_y(dialog):
    """@brief Caso negativo del precedente: la rotazione segue la geometria."""
    widget, _ = dialog
    widget._add_box((.40, .10, .45, .90))
    added = widget.kept()[-1]
    assert added['length_mm'] == pytest.approx(.8 * WIDTH_MM)
    assert added['width_mm'] == pytest.approx(.05 * LENGTH_MM)
    assert added['rotation_deg'] == 90


def test_a_click_that_slipped_is_not_a_component(dialog):
    """@brief Un rettangolo grande come un clic non entra nell'inventario."""
    widget, _ = dialog
    before = len(widget.kept())
    view = widget.view
    frame = view._frame()
    start = QPointF(frame.x() + frame.width() * .5, frame.y() + frame.height() * .9)
    tiny = MIN_NEW_BOX / 3
    end = QPointF(start.x() + frame.width() * tiny, start.y() + frame.height() * tiny)
    view._drag_from, view._drag_to = start, start
    view.mouseReleaseEvent(_Release(end))
    assert len(widget.kept()) == before


class _Release:
    """@brief Evento di rilascio minimo: serve solo la posizione."""

    def __init__(self, point):
        self._point = point

    def position(self):
        return self._point

    def button(self):
        return Qt.MouseButton.LeftButton
