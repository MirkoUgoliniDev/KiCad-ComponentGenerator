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
from PyQt6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QPointF, QRectF, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.theme import BASE
from kicadgen.gui.yolo_review_dialog import (ADDED_COLOR, BOARD_BACKDROP_COLOR,
                                             BOARD_BORDER_COLOR, HANDLE_RADIUS_PX,
                                             MIN_BOX_PX, MIN_NEW_BOX_MM, MOVE_HANDLE,
                                             PHOTO_DIMMED, PHOTO_DIMMED_OPACITY,
                                             PHOTO_FULL, PHOTO_HIDDEN, PHOTO_STATES,
                                             SELECTED_COLOR, SMALL_BOX_PX, STRONG_COLOR,
                                             MAX_ZOOM, MIN_ZOOM, TOOL_DRAW, TOOL_SELECT,
                                             WEAK_COLOR, YoloReviewDialog, box_pen)
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


def test_the_window_can_take_the_size_it_asks_for():
    """@brief La finestra puo' avere la misura che chiede, qualunque sia il carattere.

    Su Windows l'etichetta delle istruzioni, su una riga sola, fissava la
    larghezza minima a 1540 px. Dal 2026-09-24 le istruzioni stanno nel popup
    dell'icona «i» (scelta dell'utente); il contratto resta: nessun testo
    della finestra decide la sua larghezza minima.
    """
    app = QApplication.instance() or QApplication([])
    widget = YoloReviewDialog(b"", [_proposal(1, (.2, .2, .4, .4))], LENGTH_MM, WIDTH_MM,
                              detector_classes())
    requested = widget.size()
    assert widget.minimumSizeHint().width() <= requested.width()
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    assert widget.width() == 900
    widget.close()


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


def test_a_drag_with_the_draw_tool_adds_a_new_body(dialog):
    """@brief Con lo strumento Disegna, un trascinamento nel vuoto aggiunge un corpo.

    Contratto cambiato di proposito il 2026-09-24 (scelta dell'utente dopo il
    mockup): prima il trascinamento nel vuoto disegnava SEMPRE, e un clic nel
    vuoto non toglieva la scelta. Ora disegnare e' uno strumento (R), e con
    Seleziona lo stesso gesto sposta la vista (prova qui sotto). Le maniglie
    del riquadro scelto restano prendibili anche con Disegna.
    """
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    widget.set_tool(TOOL_DRAW)
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


def test_the_detector_category_reaches_the_caller_unchanged(dialog):
    """@brief Contratto cambiato il 2026-09-24 (utente): il menu per correggere la
    categoria e' stato tolto, perche' la categoria arriva al modello solo come
    indizio. Positivo: quella del rilevatore arriva intatta. Negativo: nessun
    comando della finestra la modifica piu' e un riquadro disegnato a mano non
    eredita quella dell'ultimo scelto.
    """
    widget, _ = dialog
    widget._select_row(1)
    assert [row['_yolo_class'] for row in widget.kept()] == ['resistor', 'capacitor']
    assert not hasattr(widget, 'combo_class') and not hasattr(widget, '_change_class')
    widget._add_box((.25, .40, .75, .50))
    assert widget.kept()[-1]['_yolo_class'] == ''


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
    tiny = MIN_NEW_BOX_MM / LENGTH_MM / 3
    end = QPointF(start.x() + frame.width() * tiny, start.y() + frame.height() * tiny)
    view._drag_from, view._drag_to = start, start
    view.mouseReleaseEvent(_Mouse(end))
    assert len(widget.kept()) == before


#: Colore della fotografia di prova. Serve che sia lontano da tutti e quattro
#: i tratti della finestra: un fondo vicino a un tratto farebbe contare come
#: "contorno" pixel che contorno non sono, e la prova direbbe il falso.
PHOTO_COLOR = "#243b28"

#: Tolleranza per canale con cui si riconosce un pixel del tratto. EURISTICA
#: DICHIARATA: i bordi sono disegnati con antialiasing, quindi i pixel di
#: frontiera sono miscele fra tratto e sfondo e cambiano quando lo sfondo
#: cambia -- e' proprio cio' che questa finestra sta modificando. A 20 la
#: miscela piu' vicina (mezzo tratto su fondo) resta fuori dal conteggio su
#: tutti e tre gli sfondi, cosi' si contano i pixel "pieni", che dallo sfondo
#: non dipendono e possono essere confrontati alla pari. Diventerebbe
#: superflua disegnando i contorni senza antialiasing, al prezzo di riquadri
#: piu' brutti su schermi ad alta densita'.
OUTLINE_TOLERANCE = 20


def _photo_png(color=PHOTO_COLOR):
    """@brief Una fotografia PNG di prova, in tinta unita e con quote note."""
    # 400 x 100 px per una scheda 40 x 20 mm: deliberatamente anisotropa, cosi'
    # uno scambio fra gli assi si vedrebbe.
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _make_dialog(photo_png):
    """@brief La stessa coppia di proposte della fixture, ma sopra una foto vera."""
    app = QApplication.instance() or QApplication([])
    proposals = [_proposal(1, FIRST_BOX, _source='YOLO local'),
                 _proposal(2, (.60, .10, .80, .30), _yolo_class='capacitor',
                           _yolo_confidence=.06, _yolo_review_only=True,
                           _source='YOLO local')]
    widget = YoloReviewDialog(photo_png, proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    return app, widget, proposals


@pytest.fixture
def photo_dialog():
    app, widget, proposals = _make_dialog(_photo_png())
    yield app, widget, proposals
    widget.close()


def _relative_luminance(color):
    """@brief Luminanza relativa WCAG di un colore, canali sRGB linearizzati."""
    channels = []
    for value in (QColor(color).redF(), QColor(color).greenF(), QColor(color).blueF()):
        channels.append(value / 12.92 if value <= .03928 else ((value + .055) / 1.055) ** 2.4)
    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]


def _contrast_ratio(first, second):
    """@brief Rapporto di contrasto WCAG fra due colori, da 1.0 a 21.0."""
    light, dark = sorted((_relative_luminance(first), _relative_luminance(second)))
    return (dark + .05) / (light + .05)


def _view_image(app, widget):
    """@brief Immagine della vista esattamente come la vede chi guarda."""
    app.processEvents()
    return widget.view.grab().toImage()


def _matching(image, rect, color, tolerance=OUTLINE_TOLERANCE):
    """@brief Quanti pixel di una regione sono entro `tolerance` da un colore, e dove.

    @return Coppia (conteggio, rettangolo che li contiene tutti), con None al
        posto del rettangolo se non ce n'e' nessuno.
    """
    color = QColor(color)
    if isinstance(rect, QRectF):
        rect = rect.toRect()
    found, bounds = 0, None
    for y in range(max(0, rect.top()), min(image.height() - 1, rect.bottom()) + 1):
        for x in range(max(0, rect.left()), min(image.width() - 1, rect.right()) + 1):
            pixel = image.pixelColor(x, y)
            if (abs(pixel.red() - color.red()) <= tolerance
                    and abs(pixel.green() - color.green()) <= tolerance
                    and abs(pixel.blue() - color.blue()) <= tolerance):
                found += 1
                bounds = ((x, y, x, y) if bounds is None else
                          (min(bounds[0], x), min(bounds[1], y),
                           max(bounds[2], x), max(bounds[3], y)))
    return found, bounds


def _outline_pixels(app, widget):
    """@brief Per ogni proposta: pixel disegnati nel suo colore, e dove stanno."""
    image = _view_image(app, widget)
    return [_matching(image, widget.view.to_widget(p["_box"]).adjusted(-6, -6, 6, 6).toRect(),
                      box_pen(p, index == widget.view.selected).color())
            for index, p in enumerate(widget.view.proposals)]


def _geometry(widget):
    """@brief Rettangoli in pixel di tutte le proposte: cio' che un ritocco muoverebbe."""
    return [widget.view.to_widget(p["_box"]).getCoords() for p in widget.view.proposals]


def test_the_backdrop_control_cycles_three_states_and_names_the_next_step(photo_dialog):
    """@brief Un'icona nella barra: disegna lo stato, il tooltip dice il passo successivo.

    Contratto cambiato di proposito il 2026-09-24 (barra di icone, scelta
    dell'utente): pulsante ed etichetta di stato sono diventati un'icona.
    Resta la lezione trovata sul campo -- un comando che enuncia solo uno
    stato non si legge come comando -- quindi il NOME del comando (prima riga
    del tooltip, nome accessibile) e' il verbo del passo successivo, e lo
    stato attuale e' nel disegno dell'icona, nella seconda riga del tooltip e
    nel nome accessibile. Il pulsante resta premuto finche' la fotografia non
    e' intera.
    """
    _app, widget, _ = photo_dialog
    button = widget.photo_button
    expected = [(PHOTO_FULL, "Attenua foto", "Foto: intera", "photo-full.svg"),
                (PHOTO_DIMMED, "Nascondi foto", "Foto: attenuata", "photo-dimmed.svg"),
                (PHOTO_HIDDEN, "Mostra foto", "Foto: nascosta", "photo-hidden.svg")]
    assert button.text() == "" and not button.icon().isNull()
    assert button.isCheckable() and not button.isChecked()
    assets = []
    for state, action, current, asset in expected + expected[:1]:
        assert widget.photo_state() == state
        first, second = button.toolTip().split("\n", 1)
        assert first == f"{action} (P)"
        # Negativo: il nome del comando non e' una didascalia di stato.
        assert not first.startswith("Foto:")
        assert second.startswith(current)
        assert button.accessibleName() == f"{action} (stato attuale: {current})"
        assert button.property("photo_asset") == asset
        assert button.isChecked() == (state != PHOTO_FULL)
        assets.append(asset)
        button.click()
    # Tre disegni diversi, uno per stato.
    assert len(set(assets)) == 3


def test_hiding_the_photograph_keeps_every_outline_drawn_and_in_place(photo_dialog):
    """@brief Positivo: tolta la foto, ogni contorno c'e' ancora e sta dov'era.

    E' il motivo per cui il comando esiste -- su una foto scura e affollata i
    corpi piccoli sono illeggibili -- ma un contorno che si sposta, o che
    smette di essere disegnato, renderebbe la revisione peggiore del problema
    che risolve. Si misurano entrambe le cose: la geometria in pixel prima e
    dopo, e la vista disegnata davvero, contando i pixel di ogni contorno e il
    rettangolo che li contiene.
    """
    app, widget, _ = photo_dialog
    before_geometry = _geometry(widget)
    before_pixels = _outline_pixels(app, widget)
    assert all(count > 0 for count, _ in before_pixels)
    widget.set_photo_state(PHOTO_HIDDEN)
    assert _geometry(widget) == before_geometry
    assert _outline_pixels(app, widget) == before_pixels
    image = _view_image(app, widget)
    frame = widget.view._frame()
    # Negativo: della fotografia non resta un pixel dentro l'area della scheda.
    assert _matching(image, frame, PHOTO_COLOR, tolerance=6)[0] == 0
    # ...e al suo posto c'e' lo sfondo neutro dichiarato.
    assert _matching(image, frame, BOARD_BACKDROP_COLOR, tolerance=2)[0] > 0
    assert image.pixelColor(frame.center().toPoint()) == QColor(BOARD_BACKDROP_COLOR)
    widget.set_photo_state(PHOTO_FULL)
    assert _geometry(widget) == before_geometry
    assert _outline_pixels(app, widget) == before_pixels
    assert _view_image(app, widget).pixelColor(frame.center().toPoint()) == QColor(PHOTO_COLOR)


def test_the_attenuated_photograph_fades_into_the_neutral_backdrop(photo_dialog):
    """@brief Lo stato intermedio tiene la scheda come contesto invece di toglierla.

    Attenuare e' lo stato che lascia la revisione possibile -- se un riquadro
    poggi o no sul suo corpo si giudica solo contro la scheda -- quindi la
    fotografia c'e' ancora, ma composta sopra lo sfondo neutro e percio' molto
    piu' vicina a quello che a se stessa.
    """
    app, widget, _ = photo_dialog
    before_pixels = _outline_pixels(app, widget)
    widget.set_photo_state(PHOTO_DIMMED)
    assert _outline_pixels(app, widget) == before_pixels
    pixel = _view_image(app, widget).pixelColor(widget.view._frame().center().toPoint())

    def distance(color):
        color = QColor(color)
        return (abs(pixel.red() - color.red()) + abs(pixel.green() - color.green())
                + abs(pixel.blue() - color.blue()))

    assert distance(BOARD_BACKDROP_COLOR) * 3 < distance(PHOTO_COLOR), (
        "la scheda attenuata deve stare vicino allo sfondo neutro, non alla fotografia")
    # Negativo: attenuata non e' nascosta -- la scheda si vede ancora.
    assert distance(BOARD_BACKDROP_COLOR) > 0
    assert PHOTO_DIMMED_OPACITY < .5, "sotto meta' opacita', altrimenti non e' attenuazione"


def test_the_backdrop_contrasts_with_every_pen_this_dialog_uses(photo_dialog):
    """@brief Contrasto misurato dello sfondo contro ogni tratto della finestra.

    I colori di stato portano significato -- il testo della finestra spiega i
    tratteggiati -- percio' si sceglie lo sfondo attorno a loro, mai il
    contrario. Tutti e quattro i tratti (forte, debole, aggiunto, scelto: che
    e' anche il colore delle maniglie e del rettangolo che si sta tracciando)
    e il bordo della PCB superano il rapporto WCAG di 3:1 contro lo sfondo, e
    lo sfondo e' piu' chiaro di quello dell'applicazione, cosi' l'area della
    scheda resta una regione leggibile.
    """
    _app, widget, _ = photo_dialog
    pens = {STRONG_COLOR, WEAK_COLOR, ADDED_COLOR, SELECTED_COLOR}
    # I colori provati sono davvero quelli che la finestra mette sullo schermo.
    assert {box_pen(p).color().name() for p in widget.view.proposals} <= pens
    assert box_pen(widget.view.proposals[0], chosen=True).color().name() == SELECTED_COLOR
    for colour in pens | {BOARD_BORDER_COLOR}:
        assert _contrast_ratio(colour, BOARD_BACKDROP_COLOR) >= 3.0, colour
    assert _relative_luminance(BOARD_BACKDROP_COLOR) > _relative_luminance(BASE)
    # Negativo: una fotografia non offre nessuna garanzia del genere -- una
    # serigrafia bianca o uno schermo metallico chiaro annullano il tratto
    # giallo proprio dove capita, ed e' il motivo per cui serve uno sfondo noto.
    assert _contrast_ratio(WEAK_COLOR, "#ffffff") < 3.0
    assert _contrast_ratio(STRONG_COLOR, "#d9e6d2") < 3.0


def test_the_pcb_border_is_traced_whenever_the_photograph_steps_back(photo_dialog):
    """@brief Tolta la foto, il bordo della scheda e' l'unico riferimento rimasto.

    Senza di esso un corpo che sta FUORI dalla scheda non sarebbe piu'
    giudicabile, e questa e' la finestra in cui si decide cosa mandare. Il
    bordo si disegna dentro il fotogramma, in un grigio che nessun tratto di
    stato usa, e in entrambi gli stati in cui la fotografia si fa da parte.
    """
    app, widget, _ = photo_dialog
    frame = widget.view._frame()
    edge = frame.adjusted(0, 0, 0, -frame.height() + 2 * HANDLE_RADIUS_PX)
    # Negativo: con la fotografia intera il bordo non viene disegnato, perche'
    # il bordo della foto E' gia' il bordo della scheda.
    assert _matching(_view_image(app, widget), edge, BOARD_BORDER_COLOR, tolerance=12)[0] == 0
    for state in (PHOTO_DIMMED, PHOTO_HIDDEN):
        widget.set_photo_state(state)
        assert _matching(_view_image(app, widget), edge,
                         BOARD_BORDER_COLOR, tolerance=12)[0] > 0, state
    assert BOARD_BORDER_COLOR not in {STRONG_COLOR, WEAK_COLOR, ADDED_COLOR, SELECTED_COLOR}


def test_what_the_review_returns_is_identical_in_every_state(photo_dialog):
    """@brief Negativo: lo stato della vista non deve trapelare in cio' che esce.

    E' un modo di guardare, non una decisione: `kept()` e le proposte devono
    essere identiche byte per byte nei tre stati, altrimenti chi paga la
    chiamata a valle riceverebbe qualcosa che dipende da come era regolata la
    finestra.
    """
    app, widget, _ = photo_dialog
    widget._select_row(0)
    _drag(widget.view, widget.view.to_widget(FIRST_BOX).center(), _pixels(widget.view, .10, .10))
    widget._add_box((.25, .40, .75, .50))
    reference = repr(widget.kept())
    for state in PHOTO_STATES:
        widget.set_photo_state(state)
        app.processEvents()
        assert repr(widget.kept()) == reference, state
        assert repr(widget.view.proposals) == repr(widget.proposals), state
    assert len(widget.kept()) == 3


def test_every_gesture_still_works_with_the_photograph_hidden(photo_dialog):
    """@brief Positivo: scelta, spostamento, maniglie e disegno funzionano senza foto.

    Lo sfondo neutro non prende il mouse: le coordinate normalizzate nascono
    da `_frame()`, che non dipende dallo stato, quindi un gesto deve produrre
    esattamente le stesse quote che produrrebbe sopra la fotografia. Si guida
    il widget vero, come fanno le altre prove di questo file.
    """
    _app, widget, _ = photo_dialog
    view = widget.view
    widget.set_photo_state(PHOTO_HIDDEN)
    # Scelta con un clic sul riquadro, senza passare dalla lista.
    _drag(view, view.to_widget(FIRST_BOX).center(), (0, 0))
    assert view.selected == 0 and widget.list.currentRow() == 0
    assert '_yolo_manual_box' not in widget.kept()[0]
    # Spostamento: un decimo del ritaglio vale 4 mm lungo X e 2 mm lungo Y.
    before = dict(widget.kept()[0])
    _drag(view, view.to_widget(FIRST_BOX).center(), _pixels(view, .10, .10))
    moved = widget.kept()[0]
    assert moved['x_mm'] == pytest.approx(before['x_mm'] + 4.0)
    assert moved['y_mm'] == pytest.approx(before['y_mm'] + 2.0)
    assert moved['_box'] == pytest.approx((.20, .20, .40, .40))
    # Ridimensionamento con la maniglia destra.
    rect = view.to_widget(moved['_box'])
    assert view.handle_at(QPointF(rect.right(), rect.center().y())) == (1, 0)
    _drag(view, QPointF(rect.right(), rect.center().y()), _pixels(view, .10, 0))
    resized = widget.kept()[0]
    assert resized['_box'] == pytest.approx((.20, .20, .50, .40))
    assert resized['length_mm'] == pytest.approx(.30 * LENGTH_MM)
    # Rettangolo nuovo tracciato sul vuoto, con lo strumento Disegna.
    widget.set_tool(TOOL_DRAW)
    frame = view._frame()
    dx, dy = _pixels(view, .40, .60)
    _drag(view, QPointF(frame.x() + dx, frame.y() + dy), _pixels(view, .20, .10))
    widget.set_tool(TOOL_SELECT)
    assert len(widget.kept()) == 3
    added = widget.kept()[-1]
    assert added['_yolo_origin'] == 'user_added'
    assert added['length_mm'] == pytest.approx(.20 * LENGTH_MM)
    # Eliminazione (la correzione della categoria non esiste piu', 2026-09-24).
    widget._select_row(1)
    widget._delete_selected()
    assert [row['_id'] for row in widget.kept()] == [1, 3]
    # Lo stato non e' cambiato per conto suo durante tutto questo.
    assert widget.photo_state() == PHOTO_HIDDEN
    assert widget.photo_button.property("photo_asset") == "photo-hidden.svg"


def test_a_click_that_slipped_is_still_not_a_component_without_the_photograph(photo_dialog):
    """@brief Negativo del precedente: lo sfondo neutro non abbassa la soglia.

    Un rettangolo grande come un clic resta un clic andato storto anche quando
    la fotografia non c'e': togliere la foto rende visibile, non permissivo.
    """
    _app, widget, _ = photo_dialog
    widget.set_photo_state(PHOTO_HIDDEN)
    view = widget.view
    before = len(widget.kept())
    frame = view._frame()
    start = QPointF(frame.x() + frame.width() * .5, frame.y() + frame.height() * .9)
    tiny = MIN_NEW_BOX_MM / LENGTH_MM / 3
    end = QPointF(start.x() + frame.width() * tiny, start.y() + frame.height() * tiny)
    view._drag_from, view._drag_to = start, start
    view.mouseReleaseEvent(_Mouse(end))
    assert len(widget.kept()) == before


def test_a_new_review_opens_on_the_photograph(photo_dialog):
    """@brief Ogni finestra nuova riparte dalla fotografia.

    La fotografia e' il riferimento da cui la revisione comincia: uno stato
    ereditato dalla finestra precedente farebbe aprire una revisione su una
    scheda che non si vede, senza che nessuno l'abbia chiesto.
    """
    _app, first, _ = photo_dialog
    first.set_photo_state(PHOTO_HIDDEN)
    assert first.photo_state() == PHOTO_HIDDEN
    _app2, second, _ = _make_dialog(_photo_png())
    try:
        assert second.photo_state() == PHOTO_FULL
        assert second.photo_button.property("photo_asset") == "photo-full.svg"
        assert second.photo_button.accessibleName().startswith("Attenua foto")
        assert not second.photo_button.isChecked()
    finally:
        second.close()


def test_an_unknown_state_is_refused_instead_of_being_ignored(photo_dialog):
    """@brief Negativo: uno stato sconosciuto non deve passare in silenzio.

    Ignorarlo lascerebbe la vista e il pulsante in disaccordo su cosa si sta
    guardando, che e' esattamente cio' che questo comando esiste per rendere
    inequivocabile.
    """
    _app, widget, _ = photo_dialog
    widget.set_photo_state(PHOTO_DIMMED)
    with pytest.raises(ValueError):
        widget.set_photo_state("transparent")
    assert widget.photo_state() == PHOTO_DIMMED
    assert widget.photo_button.property("photo_asset") == "photo-dimmed.svg"
    assert widget.photo_button.accessibleName().startswith("Nascondi foto")


def test_the_control_works_even_without_a_photograph(dialog):
    """@brief Senza fotografia il comando resta coerente invece di rompersi.

    La finestra puo' essere costruita con `photo_png` vuoto (e le altre prove
    di questo file la costruiscono cosi'): in quel caso non c'e' niente da
    attenuare, ma lo sfondo neutro e il bordo devono comunque comparire,
    perche' e' sempre sopra di essi che si disegnano i riquadri.
    """
    widget, _ = dialog
    app = QApplication.instance()
    assert widget.view.pixmap.isNull()
    before = _geometry(widget)
    for state in PHOTO_STATES:
        widget.set_photo_state(state)
        app.processEvents()
        assert _geometry(widget) == before, state
    widget.set_photo_state(PHOTO_HIDDEN)
    image = _view_image(app, widget)
    assert _matching(image, widget.view._frame(), BOARD_BACKDROP_COLOR, tolerance=2)[0] > 0


class _Mouse:
    """@brief Evento del mouse minimo: servono solo posizione e tasto sinistro."""

    def __init__(self, point):
        self._point = point

    def position(self):
        return self._point

    def button(self):
        return Qt.MouseButton.LeftButton


# ── Barra di icone, strumenti, zoom, filtro (utente, 2026-09-24) ─────────


def _press_key(widget, key):
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    widget.keyPressEvent(event)


def _empty_point(view):
    """Un punto della foto lontano dai due riquadri di prova."""
    frame = view._frame()
    return QPointF(frame.x() + frame.width() * .45, frame.y() + frame.height() * .75)


def test_a_still_click_on_empty_board_clears_the_selection(dialog):
    """@brief Positivo: clic fermo nel vuoto -> nessun riquadro scelto (difetto segnalato).

    Negativo: un clic fermo DENTRO un riquadro lo lascia scelto, e nessuno
    dei due gesti cambia cio' che la revisione restituisce.
    """
    widget, _ = dialog
    view = widget.view
    before = widget.kept()
    _drag(view, view.to_widget(FIRST_BOX).center(), (0, 0))
    assert view.selected == 0 and widget.btn_delete.isEnabled()
    _drag(view, _empty_point(view), (0, 0))
    assert view.selected == -1
    assert widget.list.currentRow() == -1
    assert not widget.btn_delete.isEnabled()
    assert widget.kept() == before


def test_with_select_a_drag_on_empty_board_moves_the_view_and_adds_nothing(dialog):
    """@brief Scelta dell'utente: trascinare nel vuoto sposta la vista, come una mappa."""
    widget, _ = dialog
    view = widget.view
    view.zoom_at(3)
    before_frame = view._frame()
    before = widget.kept()
    _drag(view, _empty_point(view), (-40, -25))
    after_frame = view._frame()
    assert after_frame.x() == pytest.approx(before_frame.x() - 40)
    assert after_frame.y() == pytest.approx(before_frame.y() - 25)
    assert widget.kept() == before


def test_escape_clears_the_selection_and_never_closes_the_window(dialog):
    """@brief Esc toglie la scelta; in un QDialog valeva Annulla e perdeva la revisione."""
    widget, _ = dialog
    widget._select_row(0)
    _press_key(widget, Qt.Key.Key_Escape)
    assert widget.view.selected == -1
    assert widget.isVisible() and widget.result() == 0
    _press_key(widget, Qt.Key.Key_Escape)
    assert widget.isVisible()


def test_escape_leaves_the_draw_tool_before_touching_the_selection(dialog):
    widget, _ = dialog
    widget._select_row(0)
    widget.set_tool(TOOL_DRAW)
    assert widget.btn_draw.isChecked() and not widget.btn_select.isChecked()
    _press_key(widget, Qt.Key.Key_Escape)
    assert widget.view.tool == TOOL_SELECT and widget.btn_select.isChecked()
    assert widget.view.selected == 0


def test_zoom_keeps_the_board_point_under_the_pointer_and_changes_no_data(dialog):
    widget, _ = dialog
    view = widget.view
    before = widget.kept()
    frame = view._frame()
    anchor = QPointF(frame.x() + frame.width() * .25, frame.y() + frame.height() * .40)
    view.zoom_at(2.5, anchor)
    assert view.zoom() == pytest.approx(2.5)
    zoomed = view._frame()
    assert (anchor.x() - zoomed.x()) / zoomed.width() == pytest.approx(.25)
    assert (anchor.y() - zoomed.y()) / zoomed.height() == pytest.approx(.40)
    assert widget.zoom_label.text() == "250%"
    assert widget.kept() == before


def test_zoom_stays_between_the_fitted_photo_and_the_maximum(dialog):
    widget, _ = dialog
    view = widget.view
    view.zoom_at(1 / 10)
    assert view.zoom() == MIN_ZOOM
    view.zoom_at(1000)
    assert view.zoom() == MAX_ZOOM
    view.fit_view()
    assert view.zoom() == MIN_ZOOM and widget.zoom_label.text() == "100%"


def test_editing_at_high_zoom_measures_in_photo_fractions_not_screen_pixels(dialog):
    """@brief Stessa quota in mm a ogni ingrandimento; le maniglie restano in pixel dello schermo."""
    widget, _ = dialog
    view = widget.view
    widget._select_row(0)
    view.zoom_at(4, view.to_widget(FIRST_BOX).center())
    rect = view.to_widget(FIRST_BOX)
    assert view.handle_at(QPointF(rect.right() + HANDLE_RADIUS_PX - 1, rect.center().y())) == (1, 0)
    assert view.handle_at(QPointF(rect.right() + HANDLE_RADIUS_PX + 3, rect.center().y())) is None
    before = dict(widget.kept()[0])
    _drag(view, rect.center(), _pixels(view, .02, .02))
    moved = widget.kept()[0]
    assert moved['x_mm'] == pytest.approx(before['x_mm'] + .02 * LENGTH_MM)
    assert moved['y_mm'] == pytest.approx(before['y_mm'] + .02 * WIDTH_MM)


def test_the_category_filter_hides_rows_and_boxes_but_never_data(dialog):
    """@brief Il filtro filtra lista E foto (utente, 2026-09-24); i dati non cambiano.

    Positivo: fuori categoria la riga e' nascosta e il riquadro non si
    disegna ne' si sceglie. Negativo: cio' che la revisione restituisce resta
    identico, e togliendo il filtro tutto torna visibile.
    """
    widget, _ = dialog
    view = widget.view
    choices = [widget.category_filter.itemData(i) for i in range(widget.category_filter.count())]
    assert choices == [None, 'capacitor', 'resistor']
    before = widget.kept()
    widget.category_filter.setCurrentIndex(choices.index('capacitor'))
    assert widget.list.isRowHidden(0) and not widget.list.isRowHidden(1)
    assert view.hidden == {0}
    assert view.box_at(view.to_widget(FIRST_BOX).center()) is None
    assert widget.kept() == before and len(view.proposals) == 2
    widget.category_filter.setCurrentIndex(0)
    assert not widget.list.isRowHidden(0) and not widget.list.isRowHidden(1)
    assert view.hidden == set()


def test_changing_the_filter_deselects_a_box_outside_the_category(dialog):
    """@brief Negativo: una scelta dentro la categoria resta; una fuori si toglie."""
    widget, _ = dialog
    choices = [widget.category_filter.itemData(i) for i in range(widget.category_filter.count())]
    widget._select_row(0)
    widget.category_filter.setCurrentIndex(choices.index('resistor'))
    assert widget.view.selected == 0
    widget.category_filter.setCurrentIndex(choices.index('capacitor'))
    assert widget.view.selected == -1 and not widget.btn_delete.isEnabled()


def test_the_status_bar_shows_the_board_point_in_millimetres(dialog):
    widget, _ = dialog
    view = widget.view
    frame = view._frame()
    view.mouseMoveEvent(_Mouse(frame.center()))
    assert widget.pointer_label.text() == "X 0.00 mm · Y 0.00 mm"
    corner = QPointF(frame.right() - 0.001, frame.bottom() - 0.001)
    view.mouseMoveEvent(_Mouse(corner))
    assert widget.pointer_label.text() == f"X {LENGTH_MM / 2:.2f} mm · Y {WIDTH_MM / 2:.2f} mm"
    view.mouseMoveEvent(_Mouse(QPointF(frame.x() - 5, frame.y() - 5)))
    assert widget.pointer_label.text() == "X — · Y —"


def test_every_toolbar_command_is_an_icon_named_in_its_tooltip(dialog):
    widget, _ = dialog
    buttons = [widget.btn_select, widget.btn_draw, widget.btn_delete, widget.btn_zoom_out,
               widget.btn_zoom_in, widget.btn_fit, widget.photo_button, widget.btn_info]
    for button in buttons:
        assert button.text() == "" and not button.icon().isNull()
        name = button.accessibleName()
        assert name and button.toolTip().startswith(name.split(" (stato")[0])
    assert widget.btn_delete.toolTip().splitlines()[0] == "Elimina il riquadro scelto (Canc)"
    assert widget.btn_draw.toolTip().splitlines()[0] == "Disegna un riquadro nuovo (R)"


def test_the_instructions_open_in_a_popup_inside_the_window(dialog):
    """@brief Le istruzioni non sono piu' testo fisso: l'icona «i» le apre e le richiude."""
    widget, _ = dialog
    app = QApplication.instance()
    popup = widget.info_popup
    assert not popup.isVisible()
    widget.btn_info.click()
    app.processEvents()
    assert popup.isVisible()
    assert "confidenza bassa" in popup.label.text()
    area = widget.frameGeometry()
    assert area.contains(popup.frameGeometry())
    popup.hide()
    widget.btn_info.click()          # lo stesso clic che l'ha chiusa non la riapre
    assert not popup.isVisible()


# ── Scegliere le proposte spurie (utente, 2026-09-24 notte) ──────────────


def _nested_dialog():
    """Un riquadro piccolo DENTRO uno grande, con il grande DOPO nell'elenco."""
    app = QApplication.instance() or QApplication([])
    proposals = [_proposal(1, (.20, .20, .26, .26), _yolo_review_only=True, _yolo_confidence=.1),
                 _proposal(2, (.10, .10, .60, .60), _yolo_class='ic')]
    widget = YoloReviewDialog(b"", proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    widget.resize(900, 600)
    widget.show()
    app.processEvents()
    return app, widget


def test_a_small_box_inside_a_large_one_can_be_chosen():
    """@brief Positivo: vince il piu' piccolo che contiene il punto, non l'ultimo dell'elenco.

    Negativo: dove c'e' solo il grande, vince il grande.
    """
    _app, widget = _nested_dialog()
    view = widget.view
    try:
        _drag(view, view.to_widget((.20, .20, .26, .26)).center(), (0, 0))
        assert view.selected == 0
        _drag(view, view.to_widget((.45, .45, .55, .55)).center(), (0, 0))
        assert view.selected == 1
    finally:
        widget.close()


def test_with_the_draw_tool_a_still_click_chooses_and_a_drag_still_draws(dialog):
    """@brief Positivo: con Disegna un clic fermo sceglie il riquadro (e nel vuoto toglie la scelta).

    Negativo: un trascinamento con Disegna traccia ancora un riquadro nuovo.
    Prima con Disegna attivo le proposte non si potevano scegliere.
    """
    widget, _ = dialog
    view = widget.view
    widget.set_tool(TOOL_DRAW)
    before = widget.kept()
    _drag(view, view.to_widget(FIRST_BOX).center(), (0, 0))
    assert view.selected == 0 and widget.btn_delete.isEnabled()
    assert widget.kept() == before
    _drag(view, _empty_point(view), (0, 0))
    assert view.selected == -1
    frame = view._frame()
    _drag(view, QPointF(frame.x() + frame.width() * .40, frame.y() + frame.height() * .60),
          _pixels(view, .15, .10))
    assert len(widget.kept()) == len(before) + 1
    assert view.tool == TOOL_DRAW


def test_hand_drawn_boxes_have_their_own_filter_entry():
    """@brief Audit 2026-09-25: un riquadro a mano non ha categoria e, con un filtro
    attivo, spariva appena deselezionato. Positivo: voce «senza categoria» che
    lo mostra. Negativo: filtrando una categoria vera resta nascosto."""
    from kicadgen.gui import yolo_review_dialog as yolo
    app = QApplication.instance() or QApplication([])
    proposals = [_proposal(1, FIRST_BOX), _proposal(2, (.60, .10, .80, .30), _yolo_class='')]
    widget = YoloReviewDialog(b"", proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    choices = [widget.category_filter.itemData(i) for i in range(widget.category_filter.count())]
    assert choices == [None, 'resistor', yolo.NO_CLASS]
    widget.category_filter.setCurrentIndex(choices.index(yolo.NO_CLASS))
    assert widget.list.isRowHidden(0) and not widget.list.isRowHidden(1)
    widget.category_filter.setCurrentIndex(choices.index('resistor'))
    assert not widget.list.isRowHidden(0) and widget.list.isRowHidden(1)
    widget.close()
    widget.setParent(None)
    widget.deleteLater()
    app.processEvents()
