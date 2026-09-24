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

#: Tolleranza del box rispetto alla colonna di testo, vedi il test che la usa.
TOOLTIP_CHROME_ALLOWANCE_PX = 48

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
    # La colonna di testo e' al massimo TOOLTIP_MAX_WIDTH_PX; il box aggiunge
    # la cornice: padding e bordo del tema (18 px) piu' margine della
    # etichetta di Qt e del documento (26 px), 44 px misurati su Windows con
    # APP_QSS. 48 lascia qualche pixel di arrotondamento dei caratteri.
    assert box.width() <= TOOLTIP_MAX_WIDTH_PX + TOOLTIP_CHROME_ALLOWANCE_PX
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


def test_a_long_tooltip_in_a_narrow_window_stays_inside_it(app):
    """@brief La cornice vera del tooltip conta: in 300 px il box non sfora a destra.

    Negativo: in una finestra larga la colonna resta TOOLTIP_MAX_WIDTH_PX
    (nessun restringimento senza bisogno).
    """
    from kicadgen.gui.theme import APP_QSS
    previous = app.styleSheet()
    app.setStyleSheet(APP_QSS)
    try:
        window, button = _window_with_button(app, LONG, width=300)
        _hover(button)
        app.processEvents()
        box = _tip_label().frameGeometry()
        area = QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        assert box.left() >= area.left() + WINDOW_MARGIN_PX - 1
        assert box.right() <= area.right() - WINDOW_MARGIN_PX + 1, (box, area)
        window.close()
        QToolTip.hideText()
        window, button = _window_with_button(app, LONG, width=1400, at_right=False)
        _hover(button)
        app.processEvents()
        label = _tip_label()
        assert label.contentsRect().width() >= TOOLTIP_MAX_WIDTH_PX
        window.close()
    finally:
        app.setStyleSheet(previous)


def test_the_area_is_the_part_of_the_window_on_screen():
    """@brief Revisore 3, difetto 12: finestra in parte fuori schermo -> area ristretta.

    Negativi: finestra tutta dentro -> la finestra; tutta fuori -> lo schermo.
    """
    from kicadgen.gui.tooltips import visible_area
    screen = QRect(0, 0, 1920, 1080)
    assert visible_area(QRect(1700, 100, 600, 400), screen) == QRect(1700, 100, 220, 400)
    assert visible_area(QRect(100, 100, 600, 400), screen) == QRect(100, 100, 600, 400)
    assert visible_area(QRect(3000, 100, 600, 400), screen) == screen
    assert visible_area(QRect(100, 100, 600, 400), None) == QRect(100, 100, 600, 400)


def test_header_section_tooltips_go_through_the_filter(app):
    """@brief Revisore 3, difetto 11: il tooltip di un'intestazione e' preso dal filtro.

    Negativo: una sezione senza tooltip resta a Qt.
    """
    from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem
    table = QTableWidget(1, 2)
    table.setHorizontalHeaderLabels(["Tipo contatto", "Passo"])
    table.horizontalHeaderItem(0).setToolTip(LONG)
    table.resize(400, 200)
    table.show()
    app.processEvents()
    header = table.horizontalHeader()
    shown = []
    import kicadgen.gui.tooltips as tooltips
    original = tooltips.show_tooltip
    tooltips.show_tooltip = lambda widget, pointer, text: shown.append(text)
    try:
        for section in (0, 1):
            local = QPoint(header.sectionViewportPosition(section) + 5, 5)
            event = QHelpEvent(QEvent.Type.ToolTip, local, header.viewport().mapToGlobal(local))
            handled = ToolTipFilter().eventFilter(header.viewport(), event)
            assert handled is (section == 0)
    finally:
        tooltips.show_tooltip = original
    assert shown == [LONG]
    table.close()
