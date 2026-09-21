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

from kicadgen.gui.yolo_review_dialog import (HANDLE_RADIUS_PX, MIN_BOX_PX, MIN_NEW_BOX,
                                             MOVE_HANDLE, SMALL_BOX_PX, YoloReviewDialog,
                                             box_pen)
from kicadgen.services.photo_yolo import detector_classes

#: La scheda di prova: 40 mm lungo X, 20 mm lungo Y. Le quote servono a
#: convertire i riquadri tracciati a mano, quindi devono essere diverse fra
#: loro, altrimenti uno scambio fra i due assi passerebbe inosservato.
LENGTH_MM, WIDTH_MM = 40.0, 20.0

#: Il primo riquadro di prova, normalizzato, e la sua controparte in pixel
#: si leggono dalla vista; qui bastano le frazioni che i test spostano.
FIRST_BOX = (.10, .10, .30, .30)


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
    # Entrambe le proposte portano le marcature che una misura automatica
    # lascia dietro di se': la revisione manuale deve toglierle solo a chi
    # viene toccato.
    proposals = [_proposal(1, FIRST_BOX, _source='YOLO local', _edges_measured=True,
                           _yolo_clipped_sides=['right'], _yolo_clipped_recovery=True),
                 _proposal(2, (.60, .10, .80, .30), _yolo_class='capacitor',
                           _yolo_confidence=.06, _yolo_review_only=True,
                           _source='YOLO local', _edges_measured=True,
                           _yolo_clipped_sides=['left'], _yolo_clipped_recovery=False)]
    widget = YoloReviewDialog(b"", proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    yield widget, proposals
    widget.close()


def _drag(view, start, delta):
    """@brief Pressione in `start`, spostamento di `delta` pixel, rilascio."""
    end = QPointF(start.x() + delta[0], start.y() + delta[1])
    view.mousePressEvent(_Mouse(start))
    view.mouseMoveEvent(_Mouse(end))
    view.mouseReleaseEvent(_Mouse(end))


def _pixels(view, fraction_x, fraction_y):
    """@brief Quante pixel della vista valgono queste frazioni del ritaglio."""
    frame = view._frame()
    return frame.width() * fraction_x, frame.height() * fraction_y


def test_dragging_the_inside_moves_the_body_by_the_expected_millimetres(dialog):
    """@brief Spostare il riquadro sposta il centro in mm, senza cambiarne le quote.

    Un decimo del ritaglio lungo X vale 4 mm sulla scheda di prova, un
    decimo lungo Y vale 2 mm: se le quote fossero scambiate, lo spostamento
    tornerebbe diverso su un asse.

    Contratto corretto: prima uno spostamento azzerava `_yolo_clipped_sides`.
    Ma spostare non cambia l'estensione, quindi il lato destro che poggiava
    sul confine di una tessera continua a essere un artefatto e il modello
    deve continuare a saperlo: la lista resta. La cade solo la maniglia di
    quel lato (vedi test_resizing_a_clipped_side_drops_only_that_side).
    """
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    before = dict(widget.kept()[0])
    center = view.to_widget(FIRST_BOX).center()
    _drag(view, center, _pixels(view, .10, .10))
    moved = widget.kept()[0]
    assert moved['x_mm'] == pytest.approx(before['x_mm'] + 4.0)
    assert moved['y_mm'] == pytest.approx(before['y_mm'] + 2.0)
    assert moved['length_mm'] == pytest.approx(before['length_mm'])
    assert moved['width_mm'] == pytest.approx(before['width_mm'])
    assert moved['rotation_deg'] == before['rotation_deg']
    assert moved['_box'] == pytest.approx((.20, .20, .40, .40))
    # Provenienza: e' l'occhio dell'utente, non piu' una misura automatica.
    assert moved['_yolo_manual_box'] is True
    assert moved['_source'] == 'revisione manuale'
    assert moved['_yolo_source_before_manual'] == 'YOLO local'
    assert '_edges_measured' not in moved
    assert moved['_yolo_clipped_sides'] == ['right'] and moved['_yolo_clipped_recovery'] is True
    assert 'ritoccato' in widget.list.item(0).text()
    assert '8.0×4.0 mm' in widget.list.item(0).text()


def test_resizing_a_clipped_side_drops_only_that_side(dialog):
    """@brief Il lato trascinato e' stato guardato: solo quello smette di essere un artefatto.

    Positivo: la maniglia destra toglie 'right' dalla lista e, restando la
    lista vuota, anche la ricomposizione decade. Negativo: la maniglia
    sinistra sullo stesso riquadro lascia 'right' com'e', perche' il lato
    destro nessuno l'ha guardato.
    """
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(FIRST_BOX)
    _drag(view, QPointF(rect.left(), rect.center().y()), (-10, 0))
    left_edited = widget.kept()[0]
    assert left_edited['_yolo_manual_box'] is True
    assert left_edited['_yolo_clipped_sides'] == ['right']
    assert left_edited['_yolo_clipped_recovery'] is True
    rect = view.to_widget(left_edited['_box'])
    _drag(view, QPointF(rect.right(), rect.center().y()), (10, 0))
    right_edited = widget.kept()[0]
    assert right_edited['_yolo_clipped_sides'] == []
    assert right_edited['_yolo_clipped_recovery'] is False
    # La provenienza di partenza si registra alla prima modifica e non viene
    # sovrascritta dalla seconda, che parte gia' da "revisione manuale".
    assert right_edited['_yolo_source_before_manual'] == 'YOLO local'


def test_mark_manual_drops_exactly_the_sides_of_the_handle():
    """@brief Tabella maniglia -> lati guardati, senza passare dal mouse."""
    cases = {(-1, -1): ['right', 'bottom'], (1, 1): ['left', 'top'],
             (1, 0): ['left', 'top', 'bottom'], (0, -1): ['left', 'right', 'bottom'],
             MOVE_HANDLE: ['left', 'right', 'top', 'bottom']}
    for handle, remaining in cases.items():
        proposal = dict(_source='YOLO local', _yolo_clipped_recovery=True,
                        _yolo_clipped_sides=['left', 'right', 'top', 'bottom'])
        YoloReviewDialog.mark_manual(proposal, handle)
        assert proposal['_yolo_clipped_sides'] == remaining, handle
        assert proposal['_yolo_clipped_recovery'] is True, handle


def test_editing_a_weak_proposal_is_the_reviewers_presence_decision(dialog):
    """@brief Una proposta debole ritoccata a mano non e' piu' un'ipotesi da verificare.

    Positivo: la seconda proposta (0.06, `_yolo_review_only`) spostata dal
    revisore diventa `_yolo_review_only=False`, la sua origine di prima
    resta in `_yolo_origin_before_manual`, e la lista non la chiama piu'
    "debole". Negativo: la prima, forte e non toccata, non riceve nessuna
    chiave nuova; e una proposta debole senza `_yolo_origin` non si vede
    inventare un'origine precedente.
    """
    widget, _ = dialog
    view = widget.view
    widget.proposals[1]['_yolo_origin'] = 'low_confidence_review'
    widget._select_row(1)
    _drag(view, view.to_widget(widget.proposals[1]['_box']).center(), _pixels(view, -.10, .10))
    weak_edited = widget.kept()[1]
    assert weak_edited['_yolo_manual_box'] is True
    assert weak_edited['_yolo_review_only'] is False
    assert weak_edited['_yolo_origin_before_manual'] == 'low_confidence_review'
    assert 'debole' not in widget.list.item(1).text()
    assert box_pen(weak_edited).style() == Qt.PenStyle.SolidLine
    strong = widget.kept()[0]
    assert strong['_yolo_review_only'] is False
    assert '_yolo_origin_before_manual' not in strong and '_yolo_manual_box' not in strong
    without_origin = dict(_yolo_review_only=True, _source='YOLO local')
    YoloReviewDialog.mark_manual(without_origin)
    assert without_origin['_yolo_review_only'] is False
    assert '_yolo_origin_before_manual' not in without_origin


def test_dragging_a_handle_changes_only_that_side(dialog):
    """@brief La maniglia destra allarga verso destra: gli altri tre lati non si muovono."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(FIRST_BOX)
    assert view.handle_at(QPointF(rect.right(), rect.center().y())) == (1, 0)
    assert view.handle_at(rect.center()) == MOVE_HANDLE
    _drag(view, QPointF(rect.right(), rect.center().y()), _pixels(view, .10, 0))
    resized = widget.kept()[0]
    assert resized['_box'] == pytest.approx((.10, .10, .40, .30))
    assert resized['length_mm'] == pytest.approx(.30 * LENGTH_MM)
    assert resized['width_mm'] == pytest.approx(.20 * WIDTH_MM)
    assert resized['rotation_deg'] == 0
    assert resized['x_mm'] == pytest.approx((.25 - .5) * LENGTH_MM)
    assert resized['y_mm'] == pytest.approx((.20 - .5) * WIDTH_MM)
    assert resized['_yolo_manual_box'] is True


def test_a_body_nobody_touched_keeps_its_automatic_provenance(dialog):
    """@brief Caso negativo: la revisione manuale marca solo cio' che e' stato toccato."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    _drag(view, view.to_widget(FIRST_BOX).center(), _pixels(view, .10, .10))
    untouched = widget.kept()[1]
    assert '_yolo_manual_box' not in untouched
    assert untouched['_edges_measured'] is True
    assert untouched['_yolo_clipped_sides'] == ['left']
    assert untouched['_source'] == 'YOLO local'
    assert 'ritoccato' not in widget.list.item(1).text()


def test_a_click_without_movement_is_not_a_manual_edit(dialog):
    """@brief Scegliere un riquadro non e' modificarlo."""
    widget, _ = dialog
    view = widget.view
    _drag(view, view.to_widget(FIRST_BOX).center(), (0, 0))
    assert view.selected == 0
    assert '_yolo_manual_box' not in widget.kept()[0]
    assert widget.kept()[0]['_edges_measured'] is True


def test_shrinking_below_the_minimum_stops_at_the_minimum(dialog):
    """@brief La maniglia si ferma contro il lato opposto: niente riquadri degeneri o rovesciati."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(FIRST_BOX)
    frame = view._frame()
    # Trascinamento molto oltre il lato sinistro, fuori dalla foto.
    _drag(view, QPointF(rect.right(), rect.center().y()), (-2 * frame.width(), 0))
    shrunk = widget.kept()[0]
    x0, y0, x1, y1 = shrunk['_box']
    assert x1 > x0 and y1 > y0
    assert (x1 - x0) * frame.width() == pytest.approx(MIN_BOX_PX, abs=1e-6)
    assert (x0, y0, y1) == pytest.approx(FIRST_BOX[:1] + FIRST_BOX[1:2] + FIRST_BOX[3:])
    assert shrunk['width_mm'] > 0 and shrunk['length_mm'] >= shrunk['width_mm']


def test_a_corner_handle_changes_exactly_two_sides(dialog):
    """@brief L'angolo in basso a destra muove destra e basso; sinistra e alto restano."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(FIRST_BOX)
    assert view.handle_at(rect.bottomRight()) == (1, 1)
    _drag(view, rect.bottomRight(), _pixels(view, .10, .20))
    resized = widget.kept()[0]
    assert resized['_box'] == pytest.approx((.10, .10, .40, .50))
    assert resized['length_mm'] == pytest.approx(.30 * LENGTH_MM)
    assert resized['width_mm'] == pytest.approx(.40 * WIDTH_MM)
    assert resized['rotation_deg'] == 0
    # Lati guardati: destro e basso. Il destro era tagliato e decade; il
    # sinistro non e' stato toccato e non compare.
    assert resized['_yolo_clipped_sides'] == []


def test_moving_beyond_the_frame_keeps_the_size_and_stays_inside(dialog):
    """@brief Un trascinamento oltre il bordo della foto si ferma al bordo, senza schiacciare il riquadro."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    frame = view._frame()
    _drag(view, view.to_widget(FIRST_BOX).center(), (2 * frame.width(), 2 * frame.height()))
    moved = widget.kept()[0]
    x0, y0, x1, y1 = moved['_box']
    assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
    assert x1 == pytest.approx(1.0) and y1 == pytest.approx(1.0)
    assert x1 - x0 == pytest.approx(FIRST_BOX[2] - FIRST_BOX[0])
    assert y1 - y0 == pytest.approx(FIRST_BOX[3] - FIRST_BOX[1])
    assert moved['length_mm'] == pytest.approx(.20 * LENGTH_MM)
    assert moved['width_mm'] == pytest.approx(.20 * WIDTH_MM)
    # Verso opposto: stessa regola sull'altro bordo.
    _drag(view, view.to_widget(moved['_box']).center(), (-3 * frame.width(), -3 * frame.height()))
    x0, y0, x1, y1 = widget.kept()[0]['_box']
    assert x0 == pytest.approx(0.0) and y0 == pytest.approx(0.0)
    assert x1 - x0 == pytest.approx(FIRST_BOX[2] - FIRST_BOX[0])


#: Un corpo piccolo sullo schermo: mezzo centesimo del ritaglio lungo X e un
#: centesimo lungo Y, che nella finestra di prova vale meno di MIN_BOX_PX su
#: entrambi gli assi (un 0402 su una scheda intera). E' il caso in cui le
#: zone delle maniglie coprivano tutto l'interno. La fixture verifica la
#: precondizione, perche' dipende da quanto e' grande la vista.
SMALL_BOX = (.500, .500, .505, .510)


@pytest.fixture
def small_dialog():
    app = QApplication.instance() or QApplication([])
    proposals = [_proposal(1, SMALL_BOX, _source='YOLO local', _edges_measured=True,
                           _yolo_clipped_sides=['right'], _yolo_clipped_recovery=True)]
    widget = YoloReviewDialog(b"", proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    rect = widget.view.to_widget(SMALL_BOX)
    assert rect.width() < MIN_BOX_PX and rect.height() < MIN_BOX_PX, 'precondizione della prova'
    yield widget, proposals
    widget.close()


def test_clicking_anywhere_in_a_box_smaller_than_the_minimum_changes_nothing(small_dialog):
    """@brief Negativo di test_a_click_without_movement_is_not_a_manual_edit su un corpo minuscolo.

    Prima, ogni punto interno cadeva in una zona d'angolo e il solo prendere
    la maniglia allargava il riquadro a MIN_BOX_PX: la misura della macchina
    diventava "manuale" senza che nessuno avesse deciso nulla. Il clic si
    prova al centro e a un pixel dentro ogni angolo (i punti dove prima
    vinceva l'angolo).
    """
    widget, _ = small_dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(SMALL_BOX)
    before = dict(widget.kept()[0])
    points = [rect.center(),
              QPointF(rect.left() + 1, rect.top() + 1), QPointF(rect.right() - 1, rect.top() + 1),
              QPointF(rect.left() + 1, rect.bottom() - 1), QPointF(rect.right() - 1, rect.bottom() - 1)]
    for point in points:
        assert view.handle_at(point) == MOVE_HANDLE, point
        _drag(view, point, (0, 0))
        assert widget.kept()[0] == before, point
    assert view.selected == 0
    assert '_yolo_manual_box' not in widget.kept()[0]
    assert widget.kept()[0]['_edges_measured'] is True
    assert 'ritoccato' not in widget.list.item(0).text()


def test_a_box_smaller_than_the_minimum_can_still_be_moved_and_enlarged(small_dialog):
    """@brief Positivo: l'interno sposta senza cambiare le quote; le maniglie si prendono da fuori."""
    widget, _ = small_dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(SMALL_BOX)
    _drag(view, rect.center(), _pixels(view, .10, .10))
    moved = widget.kept()[0]
    assert moved['_box'] == pytest.approx((.600, .600, .605, .610))
    assert moved['_yolo_manual_box'] is True
    assert moved['_yolo_clipped_sides'] == ['right'], 'spostare non guarda nessun lato'
    # Le maniglie stanno fuori dal riquadro, a portata di HANDLE_RADIUS_PX.
    rect = view.to_widget(moved['_box'])
    outside = QPointF(rect.right() + HANDLE_RADIUS_PX - 1, rect.bottom() + HANDLE_RADIUS_PX - 1)
    assert view.handle_at(outside) == (1, 1)
    assert view.handle_at(QPointF(rect.right() - 1, rect.bottom() - 1)) == MOVE_HANDLE
    _drag(view, outside, (SMALL_BOX_PX, SMALL_BOX_PX))
    enlarged = widget.kept()[0]
    assert enlarged['_box'][:2] == pytest.approx(moved['_box'][:2])
    assert view.to_widget(enlarged['_box']).width() > rect.width()
    assert enlarged['_yolo_clipped_sides'] == []


def test_a_box_smaller_than_the_minimum_is_not_shrunk_by_its_own_handle(small_dialog):
    """@brief Il pavimento e' il lato che c'e', non MIN_BOX_PX: nessun riquadro cresce da solo."""
    widget, _ = small_dialog
    view = widget.view
    widget._select_row(0)
    rect = view.to_widget(SMALL_BOX)
    same = view._edited_rect(rect, (1, 1), QPointF(0, 0))
    assert (same.left(), same.top(), same.right(), same.bottom()) == pytest.approx(
        (rect.left(), rect.top(), rect.right(), rect.bottom()))
    # Trascinata verso l'interno, la maniglia si ferma al lato attuale: il
    # riquadro non si restringe sotto quel che era, ma nemmeno si allarga.
    pushed = view._edited_rect(rect, (1, 0), QPointF(-3 * SMALL_BOX_PX, 0))
    assert pushed.width() == pytest.approx(rect.width())
    assert pushed.left() == pytest.approx(rect.left())
    # Contrasto: un riquadro grande si ferma a MIN_BOX_PX, come prima.
    big = view.to_widget(FIRST_BOX)
    assert view._edited_rect(big, (1, 0), QPointF(-3 * big.width(), 0)).width() == pytest.approx(MIN_BOX_PX)


def test_a_drag_on_empty_board_still_adds_a_new_body(dialog):
    """@brief Le maniglie non hanno rubato il gesto di aggiunta."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    frame = view._frame()
    dx, dy = _pixels(view, .40, .60)
    _drag(view, QPointF(frame.x() + dx, frame.y() + dy), _pixels(view, .20, .10))
    assert len(widget.kept()) == 3
    assert widget.kept()[-1]['_yolo_origin'] == 'user_added'
    assert widget.kept()[-1]['_yolo_manual_box'] is True


def test_cancelling_after_moving_leaves_the_caller_list_untouched(dialog):
    """@brief Annullare non deve restituire nulla di modificato."""
    widget, proposals = dialog
    original = [dict(row) for row in proposals]
    view = widget.view
    widget._select_row(0)
    _drag(view, view.to_widget(FIRST_BOX).center(), _pixels(view, .10, .10))
    widget.reject()
    assert proposals == original
    assert widget.kept()[0]['_box'] != original[0]['_box']


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
    view.mouseReleaseEvent(_Mouse(end))
    assert len(widget.kept()) == before


class _Mouse:
    """@brief Evento del mouse minimo: servono solo posizione e tasto sinistro."""

    def __init__(self, point):
        self._point = point

    def position(self):
        return self._point

    def button(self):
        return Qt.MouseButton.LeftButton
