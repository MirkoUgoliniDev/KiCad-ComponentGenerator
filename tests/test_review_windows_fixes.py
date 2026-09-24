"""@brief Difetti 1-4 del revisore 1 (verifica del 2026-09-24) nelle due finestre di revisione.

Ogni regola ha un caso positivo e uno negativo che la distinguono per ragioni
strutturali: visibile o nascosto dal filtro, dentro o fuori dal riquadro
scelto, gesto in corso o no, fuoco su un combo o su un'altra parte.
"""
import pytest
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from test_yolo_review_dialog import LENGTH_MM, WIDTH_MM, _Mouse, _proposal
from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
from kicadgen.gui.review_tools import INFO_POPUP_MARGIN_PX, InfoPopup
from kicadgen.gui.yolo_review_dialog import (PHOTO_FULL, TOOL_DRAW, TOOL_SELECT,
                                             YoloReviewDialog)
from kicadgen.services.photo_yolo import detector_classes


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _yolo(app, proposals):
    dialog = YoloReviewDialog(b"", proposals, LENGTH_MM, WIDTH_MM, detector_classes())
    dialog.resize(900, 600)
    dialog.show()
    app.processEvents()
    return dialog


def _png():
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("#243b28"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def _component(name, kind, x, y, length, width):
    return dict(name=name, kind=kind, x_mm=x, y_mm=y, length_mm=length, width_mm=width,
                height_mm=1.0, rotation_deg=0.0, confidence="high")


def _focus(app, dialog, widget):
    """Porta il fuoco su `widget` con la finestra ATTIVA: le scorciatoie di
    finestra scattano solo li', e senza questa verifica il caso positivo
    passerebbe anche con le scorciatoie ancora prepotenti."""
    dialog.activateWindow()
    QTest.qWaitForWindowActive(dialog)
    widget.setFocus()
    app.processEvents()
    assert QApplication.focusWidget() is widget


def _photo(app, components):
    dialog = PhotoReviewDialog(_png(), components, 40, 20)
    dialog.show()
    app.processEvents()
    return dialog


# ── 1: dopo Canc la scelta va a un vicino visibile ───────────────────────

def test_yolo_delete_selects_a_visible_neighbour_or_nothing(app):
    """@brief Positivo: col filtro «resistor» il vicino scelto e' un resistore.

    Negativo: tolto l'ultimo resistore non si sceglie il condensatore che il
    filtro nascondeva; la categoria sparita lascia il filtro su «tutte».
    """
    dialog = _yolo(app, [_proposal(1, (.1, .1, .2, .2), _yolo_class="resistor"),
                         _proposal(2, (.3, .1, .4, .2), _yolo_class="capacitor"),
                         _proposal(3, (.5, .1, .6, .2), _yolo_class="resistor")])
    dialog.category_filter.setCurrentIndex(dialog.category_filter.findData("resistor"))
    dialog._select_row(0)
    dialog._delete_selected()
    chosen = dialog.view.selected
    assert dialog.proposals[chosen]["_id"] == 3
    dialog._delete_selected()
    assert dialog.view.selected == -1
    assert [p["_yolo_class"] for p in dialog.proposals] == ["capacitor"]
    assert dialog.category_filter.currentData() is None
    dialog._delete_selected()
    assert len(dialog.proposals) == 1
    dialog.close()


def test_yolo_delete_without_filter_keeps_selecting_the_next(app):
    """@brief Senza filtro resta il comportamento di prima: il successivo, poi il precedente."""
    dialog = _yolo(app, [_proposal(i, (.1 * i, .1, .1 * i + .05, .2)) for i in range(1, 4)])
    dialog._select_row(1)
    dialog._delete_selected()
    assert dialog.proposals[dialog.view.selected]["_id"] == 3
    dialog._delete_selected()
    assert dialog.proposals[dialog.view.selected]["_id"] == 1
    dialog.close()


def test_photo_delete_selects_a_visible_neighbour_or_nothing(app):
    dialog = _photo(app, [_component("R1", "resistor", -10, 0, 2, 1),
                          _component("C1", "capacitor", 0, 0, 2, 1),
                          _component("R2", "resistor", 10, 0, 2, 1)])
    dialog.category_filter.setCurrentIndex(dialog.category_filter.findData("resistor"))
    dialog._select_row(0)
    dialog.delete_selected()
    assert dialog._selected_item().component["name"] == "R2"
    capacitor = next(item for item in dialog._items if item.component["name"] == "C1")
    assert not capacitor.isVisible()
    dialog.delete_selected()
    assert dialog._selected_item() is None or dialog._selected_item().isVisible()
    assert [item.component["name"] for item in dialog._items] == ["C1"]
    dialog.close()


# ── 2: un riquadro piccolo dentro quello scelto si sceglie ───────────────

def _nested(app):
    return _yolo(app, [_proposal(1, (.1, .1, .8, .8), _yolo_class="ic"),
                       _proposal(2, (.40, .40, .46, .46))])


def test_yolo_a_small_box_inside_the_selected_one_can_be_chosen(app):
    """@brief Positivo: clic sul piccolo dentro il grande scelto -> il piccolo.

    Negativo: clic sull'interno del grande dove non c'e' altro -> resta il
    grande, e il trascinamento lo sposta.
    """
    dialog = _nested(app)
    view = dialog.view
    dialog._select_row(0)
    small = view.to_widget(dialog.proposals[1]["_box"]).center()
    view.mousePressEvent(_Mouse(small))
    view.mouseReleaseEvent(_Mouse(small))
    assert view.selected == 1

    dialog._select_row(0)
    empty = view.to_widget((.2, .2, .25, .25)).center()
    view.mousePressEvent(_Mouse(empty))
    moved = QPointF(empty.x() + 30, empty.y() + 20)
    view.mouseMoveEvent(_Mouse(moved))
    view.mouseReleaseEvent(_Mouse(moved))
    assert view.selected == 0
    assert dialog.proposals[0]["_box"] != (.1, .1, .8, .8)
    dialog.close()


def test_photo_order_is_by_area_even_for_the_selected_outline(app):
    """@brief Positivo: scelto il grande, il piccolo resta sopra e il clic lo prende.

    Negativo: fra due rettangoli disgiunti il clic prende quello sotto il puntatore.
    """
    dialog = _photo(app, [_component("BIG", "ic", 0, 0, 30, 14),
                          _component("SMALL", "resistor", 2, 1, 2, 1),
                          _component("FAR", "resistor", 17, 8, 2, 1)])
    dialog._select_row(0)
    big, small = dialog._items[0], dialog._items[1]
    assert small.zValue() > big.zValue()
    point = dialog.view.mapFromScene(small.mapToScene(small.polygon().boundingRect().center()))
    QTest.mouseClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point)
    assert dialog._selected_item().component["name"] == "SMALL"
    far = dialog._items[2]
    point = dialog.view.mapFromScene(far.mapToScene(far.polygon().boundingRect().center()))
    QTest.mouseClick(dialog.view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point)
    assert dialog._selected_item().component["name"] == "FAR"
    dialog.close()


# ── 3: Canc durante un trascinamento ─────────────────────────────────────

def test_yolo_delete_during_a_drag_leaves_the_next_box_alone(app):
    """@brief Positivo: il corpo che prende il posto del cancellato non si muove.

    Negativo: senza Canc lo stesso trascinamento sposta il corpo e lo marca.
    """
    dialog = _yolo(app, [_proposal(1, (.1, .1, .2, .2)), _proposal(2, (.5, .5, .7, .7)),
                         _proposal(3, (.3, .3, .4, .4))])
    view = dialog.view
    dialog._select_row(1)
    start = view.to_widget(dialog.proposals[1]["_box"]).center()
    view.mousePressEvent(_Mouse(start))
    dialog._delete_selected()
    end = QPointF(start.x() + 40, start.y() + 30)
    view.mouseMoveEvent(_Mouse(end))
    view.mouseReleaseEvent(_Mouse(end))
    assert dialog.proposals[1]["_id"] == 3
    assert dialog.proposals[1]["_box"] == (.3, .3, .4, .4)
    assert not dialog.proposals[1].get("_yolo_manual_box")

    dialog._select_row(1)
    start = view.to_widget(dialog.proposals[1]["_box"]).center()
    view.mousePressEvent(_Mouse(start))
    end = QPointF(start.x() + 40, start.y() + 30)
    view.mouseMoveEvent(_Mouse(end))
    view.mouseReleaseEvent(_Mouse(end))
    assert dialog.proposals[1]["_box"] != (.3, .3, .4, .4)
    assert dialog.proposals[1].get("_yolo_manual_box")
    dialog.close()


# ── 4: un combo con il fuoco tiene le sue lettere ────────────────────────

def test_yolo_typing_in_a_combo_does_not_trigger_shortcuts(app):
    """@brief Positivo: R e P dentro il filtro non cambiano strumento ne' foto.

    Negativo: gli stessi tasti con il fuoco sulla lista li cambiano.
    """
    dialog = _yolo(app, [_proposal(1, (.1, .1, .2, .2))])
    _focus(app, dialog, dialog.category_filter)
    for key in "rp":
        QTest.keyClick(dialog.category_filter, key)
    assert dialog.view.tool == TOOL_SELECT and dialog.view.photo_state() == PHOTO_FULL
    _focus(app, dialog, dialog.list)
    for key in "rp":
        QTest.keyClick(dialog.list, key)
    assert dialog.view.tool == TOOL_DRAW and dialog.view.photo_state() != PHOTO_FULL
    dialog.close()


def test_photo_typing_in_a_combo_does_not_trigger_shortcuts(app):
    dialog = _photo(app, [_component("R1", "resistor", -10, 0, 2, 1),
                          _component("C1", "capacitor", 0, 0, 2, 1)])
    _focus(app, dialog, dialog.category_filter)
    state, zoom = dialog.photo_state(), dialog.view.zoom_percent()
    for key in "rpf+-":
        QTest.keyClick(dialog.category_filter, key)
    assert not dialog.draw_mode() and dialog.photo_state() == state
    assert dialog.view.zoom_percent() == zoom
    _focus(app, dialog, dialog.component_list)
    QTest.keyClick(dialog.component_list, "p")
    assert dialog.photo_state() != state
    dialog.close()


# ── sospetti: popup alto, tremolio con Disegna ───────────────────────────

def test_a_popup_taller_than_its_window_scrolls_inside_it(app):
    """@brief Positivo: testo lunghissimo -> popup alto quanto la finestra e barra attiva.

    Negativo: testo breve -> popup alto quanto serve, senza barra.
    """
    from PyQt6.QtWidgets import QToolButton, QVBoxLayout, QWidget
    window = QWidget()
    window.resize(500, 300)
    anchor = QToolButton(window)
    QVBoxLayout(window).addWidget(anchor)
    window.show()
    app.processEvents()
    long_popup = InfoPopup("<p>" + "riga di prova molto lunga " * 400 + "</p>", window)
    long_popup.open_under(anchor)
    area_top = window.mapToGlobal(window.rect().topLeft()).y()
    assert long_popup.height() <= window.height() - 2 * INFO_POPUP_MARGIN_PX
    assert long_popup.y() >= area_top + INFO_POPUP_MARGIN_PX
    assert long_popup.scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    long_popup.hide()

    short_popup = InfoPopup("<p>breve</p>", window)
    short_popup.open_under(anchor)
    assert short_popup.height() < window.height() - 2 * INFO_POPUP_MARGIN_PX
    assert short_popup.scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert isinstance(short_popup.label, QLabel)
    short_popup.hide()
    window.close()


def test_a_draw_tremor_is_a_click_and_a_real_drag_draws(app):
    """@brief Positivo: con Disegna un movimento sotto startDragDistance sceglie.

    Negativo: un trascinamento vero, anche corto ma oltre la soglia e sopra
    MIN_NEW_BOX, traccia un riquadro nuovo.
    """
    dialog = _yolo(app, [_proposal(1, (.1, .1, .5, .5))])
    dialog.set_tool(TOOL_DRAW)
    view = dialog.view
    added = []
    view.box_added.connect(added.append)
    dialog._select_row(-1)
    start = view.to_widget((.2, .2, .3, .3)).center()
    jitter = QApplication.startDragDistance() - 1
    end = QPointF(start.x() + jitter, start.y())
    view.mousePressEvent(_Mouse(start))
    view.mouseMoveEvent(_Mouse(end))
    view.mouseReleaseEvent(_Mouse(end))
    assert not added and view.selected == 0

    frame = view._frame()
    start = QPointF(frame.x() + .6 * frame.width(), frame.y() + .6 * frame.height())
    end = QPointF(start.x() + .1 * frame.width(), start.y() + .1 * frame.height())
    view.mousePressEvent(_Mouse(start))
    view.mouseMoveEvent(_Mouse(end))
    view.mouseReleaseEvent(_Mouse(end))
    assert len(added) == 1
    dialog.close()
