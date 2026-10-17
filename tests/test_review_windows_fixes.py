"""@brief Difetti 1-4 del revisore 1 (verifica del 2026-09-24) nella finestra di revisione.

Ogni regola ha un caso positivo e uno negativo che la distinguono per ragioni
strutturali: visibile o nascosto dal filtro, dentro o fuori dal riquadro
scelto, fuoco su un combo o su un'altra parte.

@note I gemelli di questi test sulla vecchia finestra «Corpi rilevati»
      sono stati tolti con quel modulo, che era codice morto: la fase del
      rilevatore e' oggi `PhotoReviewDialog` con `stage=STAGE_DETECTOR`,
      coperta in `test_unified_review.py`.
"""
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from kicadgen.gui.photo_review_dialog import STAGE_DETECTOR, PhotoReviewDialog
from kicadgen.gui.review_tools import INFO_POPUP_MARGIN_PX, InfoPopup
from kicadgen.services import photo_yolo

#: La scheda di prova: 40 mm lungo X, 20 mm lungo Y, diverse fra loro perche'
#: uno scambio fra i due assi non passi inosservato.
LENGTH_MM, WIDTH_MM = 40.0, 20.0


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


def _proposal(identity, box, **changes):
    """@brief A detector proposal as `detect_yolo_components` writes it.

    @param identity Proposal id.
    @param box Normalised (x0, y0, x1, y1) box on the crop.
    @param changes Extra or overriding keys.
    @return Proposal dict with the geometry in mm derived from the box.
    """
    row = dict(_id=identity, _box=tuple(box), name=f"detector hypothesis {identity}",
               kind="generic", **photo_yolo.box_geometry_mm(box, LENGTH_MM, WIDTH_MM),
               height_mm=1.0, color_hex="#303030", _source="YOLO local",
               _yolo_class="resistor", _yolo_confidence=.72, _yolo_review_only=False,
               _yolo_origin="operating_threshold", _yolo_low_confidence=False)
    row.update(changes)
    return row


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
    dialog = PhotoReviewDialog(_png(), components, LENGTH_MM, WIDTH_MM)
    dialog.show()
    app.processEvents()
    return dialog


# ── 1: dopo Canc la scelta va a un vicino visibile ───────────────────────

def test_photo_delete_selects_a_visible_neighbour_or_nothing(app):
    """@brief Positivo: col filtro «resistor» il vicino scelto e' un resistore.

    Negativo: tolto l'ultimo resistore non si sceglie il condensatore che il
    filtro nascondeva.
    """
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


# ── 4: un combo con il fuoco tiene le sue lettere ────────────────────────

def test_photo_typing_in_a_combo_does_not_trigger_shortcuts(app):
    """@brief Positivo: R, P, F, +, - dentro il filtro non cambiano strumento, foto ne' zoom.

    Negativo: P con il fuoco sulla lista cambia lo stato della foto.
    """
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


# ── sospetti: popup alto ─────────────────────────────────────────────────

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


def test_the_detector_summary_follows_the_language(app, tmp_path):
    """@brief Utente, 2026-09-24: la riga di riepilogo restava italiana in inglese.

    Positivo: con un genitore in inglese la frase della fase del rilevatore e'
    inglese e porta i numeri. Negativo: senza traduttore resta la sorgente
    italiana.
    """
    from PyQt6.QtWidgets import QWidget
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    parent = QWidget()
    parent.i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    english = PhotoReviewDialog(_png(), [_proposal(1, (.1, .1, .2, .2))], LENGTH_MM, WIDTH_MM,
                                parent, stage=STAGE_DETECTOR)
    assert english.ai_only_label.text().startswith("The detector proposed 1 (")
    assert "You keep 1, plus 0 added by hand." in english.ai_only_label.text()
    italian = PhotoReviewDialog(_png(), [_proposal(1, (.1, .1, .2, .2))], LENGTH_MM, WIDTH_MM,
                                stage=STAGE_DETECTOR)
    assert italian.ai_only_label.text().startswith("Il rilevatore ne ha proposte 1 (")
    # Distruzione in ordine, prima la finestra e poi il genitore temporaneo:
    # lasciata al garbage collector, la coppia faceva cadere il processo a
    # fine suite (codice 139, dopo che tutti i test erano passati).
    for dialog in (english, italian):
        dialog.close()
        dialog.setParent(None)
        dialog.deleteLater()
    parent.deleteLater()
    app.processEvents()
