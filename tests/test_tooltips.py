"""@brief Tooltip a capo e dentro la finestra dell'applicazione.

Richiesta dell'utente del 2026-09-24, con lo screenshot del tab 3D: la
spiegazione di «Rilancia inventario AI» stava su una riga sola e usciva dalla
finestra, sopra il desktop. Qt non manda mai a capo un tooltip di testo
semplice e lo tiene dentro lo SCHERMO, non dentro l'applicazione.

Ogni regola ha il suo negativo: un tooltip breve resta com'e' (nessuna
larghezza imposta), un widget senza tooltip non viene toccato.
"""
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QHelpEvent
from PyQt6.QtWidgets import (QApplication, QListWidget, QListWidgetItem, QPushButton,
                             QToolTip, QWidget)

from kicadgen.gui.tooltips import (TOOLTIP_MAX_WIDTH_PX, WINDOW_MARGIN_PX, ToolTipFilter,
                                   fitted_tooltip, install_tooltip_filter, place_inside,
                                   tooltip_html)
from PyQt6.QtCore import QRect

LONG = ("Rilancia inventario AI (a pagamento)...\n"
        "Passo 2 della pipeline — Rilancia solo l'inventario AI sulle proposte salvate "
        "in photo-review/: una richiesta A PAGAMENTO al modello che guarda la foto, poi "
        "abbinamento modelli e 3D come dopo un'estrazione. E' l'unico comando di questa "
        "riga che spende.")


_APP = None


@pytest.fixture(autouse=True)
def app():
    """Una sola QApplication viva per tutto il modulo: misurare un testo con
    QTextDocument senza applicazione fa cadere il processo."""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    install_tooltip_filter(_APP)
    yield _APP
    QToolTip.hideText()
    _APP.processEvents()


def _tip_label():
    for widget in QApplication.topLevelWidgets():
        if widget.metaObject().className() == "QTipLabel" and widget.isVisible():
            return widget
    return None


def _window_with_button(app, text, width=520, height=320, at_right=True):
    window = QWidget()
    window.resize(width, height)
    window.move(200, 200)
    button = QPushButton("x", window)
    button.move(width - 40 if at_right else 10, 10)
    button.setToolTip(text)
    window.show()
    app.processEvents()
    return window, button


def _hover(button):
    """Manda al filtro l'evento che Qt manda quando il mouse si ferma sul pulsante."""
    local = QPoint(5, 5)
    event = QHelpEvent(QEvent.Type.ToolTip, local, button.mapToGlobal(local))
    return ToolTipFilter().eventFilter(button, event)


def test_the_first_line_is_the_name_in_bold_and_text_is_escaped():
    body = tooltip_html("Nome <b>\nSpiegazione & resto")
    assert body == "<b>Nome &lt;b&gt;</b><br>Spiegazione &amp; resto"
    # Negativo: una riga sola non ha un nome da mettere in evidenza.
    assert tooltip_html("Solo una riga") == "Solo una riga"


def test_a_long_tooltip_wraps_and_stays_inside_the_window(app):
    """Positivo: tooltip lungo, pulsante sul bordo destro di una finestra stretta."""
    window, button = _window_with_button(app, LONG)
    assert _hover(button) is True
    app.processEvents()
    label = _tip_label()
    assert label is not None
    area = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
    box = label.frameGeometry()
    assert area.adjusted(WINDOW_MARGIN_PX - 1, WINDOW_MARGIN_PX - 1,
                         -WINDOW_MARGIN_PX + 1, -WINDOW_MARGIN_PX + 1).contains(box), (box, area)
    assert box.width() <= TOOLTIP_MAX_WIDTH_PX + 40
    # E' andato a capo: e' piu' alto di due righe.
    assert box.height() > 3 * label.fontMetrics().lineSpacing()
    window.close()


def test_a_short_tooltip_keeps_its_natural_size(app):
    """Negativo: nessuna larghezza imposta a un tooltip che ci sta."""
    text = fitted_tooltip("Adatta la foto", 1200)
    assert "<table" not in text
    window, button = _window_with_button(app, "Adatta la foto", at_right=False)
    _hover(button)
    app.processEvents()
    label = _tip_label()
    assert label is not None
    assert label.height() < 3 * label.fontMetrics().lineSpacing()
    window.close()


def test_the_width_never_exceeds_a_narrow_window():
    """La colonna si stringe con la finestra: su 300 px non puo' essere di 420."""
    text = fitted_tooltip(LONG, 300)
    width = int(text.split("width='")[1].split("'")[0])
    assert width <= 300 - 2 * WINDOW_MARGIN_PX
    wide = fitted_tooltip(LONG, 2000)
    assert int(wide.split("width='")[1].split("'")[0]) == TOOLTIP_MAX_WIDTH_PX


def test_the_box_goes_above_the_pointer_when_below_it_would_leave_the_window():
    area = QRect(100, 100, 600, 400)
    box = QRect(0, 0, 300, 120)
    below = place_inside(box, QPoint(200, 150), area)
    assert below.y() > 150
    above = place_inside(box, QPoint(200, 480), area)
    assert above.y() + box.height() < 480
    # Troppo a destra: rientra, non esce.
    right = place_inside(box, QPoint(690, 150), area)
    assert right.x() + box.width() <= area.right() - WINDOW_MARGIN_PX


def test_a_widget_without_tooltip_is_left_to_qt(app):
    """Negativo: senza testo il filtro non consuma l'evento (risponde il genitore)."""
    window, button = _window_with_button(app, "")
    assert _hover(button) is False
    window.close()


def test_list_items_get_the_same_treatment(app):
    """Le voci di una lista hanno il tooltip sull'elemento, non sul widget."""
    lst = QListWidget()
    item = QListWidgetItem("voce")
    item.setToolTip(LONG)
    lst.addItem(item)
    lst.resize(400, 300)
    lst.show()
    app.processEvents()
    viewport = lst.viewport()
    point = lst.visualItemRect(item).center()
    event = QHelpEvent(QEvent.Type.ToolTip, point, viewport.mapToGlobal(point))
    assert ToolTipFilter().eventFilter(viewport, event) is True
    lst.close()


def test_the_filter_is_installed_once(app):
    first = install_tooltip_filter(app)
    assert install_tooltip_filter(app) is first
