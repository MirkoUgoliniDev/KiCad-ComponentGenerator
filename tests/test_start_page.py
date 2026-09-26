"""@brief Pagina iniziale senza progetto (PIANO-FIX, voce 1).

Si verifica cio' che l'utente vede e cio' che la pagina chiede alla finestra:
i due comandi grandi, l'elenco dei recenti con un clic che riapre, il tasto
destro che toglie dall'elenco, lo stato vuoto. Ogni segnale ha il suo
negativo (il gesto che NON deve emetterlo).
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QMenu

from kicadgen.gui.start_page import StartPage
from kicadgen.services import recent_projects as rp


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _project(root, name, key="", text=""):
    folder = root / name
    folder.mkdir(parents=True)
    widgets = {"combo_component_type": {"data": key, "text": text}}
    (folder / rp.PROJECT_CONFIG_NAME).write_text(
        json.dumps({"format": "kicadgen-project", "version": 1, "state": {"widgets": widgets}}),
        encoding="utf-8")
    return folder


@pytest.fixture
def page(app, tmp_path):
    widget = StartPage()
    widget.resize(900, 700)
    widget.show()
    QTest.qWaitForWindowExposed(widget)
    yield widget
    widget.close()
    widget.deleteLater()


def _row_center(page, row):
    return page.recent_list.visualItemRect(page.recent_list.item(row)).center()


# ── Stato vuoto ──────────────────────────────────────────────────────────

def test_empty_list_shows_the_friendly_line_and_no_empty_box(page):
    page.set_recent([])
    assert page.recent_empty.isVisible()
    assert page.recent_empty.text().startswith("Nessun progetto recente")
    assert not page.recent_list.isVisible()
    assert not page.recent_hint.isVisible()


def test_a_list_with_entries_hides_the_empty_line(page, tmp_path):
    page.set_recent([_project(tmp_path, "A")])
    assert page.recent_list.isVisible()
    assert not page.recent_empty.isVisible()
    # E tornare a vuoto la rimette.
    page.set_recent([])
    assert page.recent_empty.isVisible()


# ── Contenuto delle righe ────────────────────────────────────────────────

def test_rows_keep_the_order_and_carry_name_path_type(page, tmp_path):
    a = _project(tmp_path, "Alfa", "pcb_module", "Scheda con componenti")
    b = _project(tmp_path, "Beta")
    page.set_recent([str(b), a])
    assert page.recent_paths() == [str(b), str(a)]
    assert page.recent_list.item(0).text() == "Beta"
    assert page.recent_list.item(1).toolTip() == str(a)
    beta, alfa = (page.recent_list.item(row).data(Qt.ItemDataRole.UserRole + 1)
                  for row in (0, 1))
    assert page.component_type_text(alfa) == "Scheda con componenti"
    # Negativo: tipologia «non specificata» (chiave vuota) non occupa la colonna.
    assert page.component_type_text(beta) == ""


def test_the_type_label_goes_through_the_given_translator(app, tmp_path):
    folder = _project(tmp_path, "Alfa", "pcb_module", "Scheda con componenti")
    calls = []

    def label(key, fallback):
        calls.append((key, fallback))
        return "Board with components"
    widget = StartPage(type_label=label)
    widget.set_recent([folder])
    summary = widget.recent_list.item(0).data(Qt.ItemDataRole.UserRole + 1)
    assert widget.component_type_text(summary) == "Board with components"
    assert calls == [("pcb_module", "Scheda con componenti")]


def test_rows_are_painted_without_errors_including_a_very_long_path(page, tmp_path):
    deep = tmp_path.joinpath(*["cartella-molto-lunga"] * 12)
    page.set_recent([_project(deep, "Progetto"), _project(tmp_path, "Corto", "x", "Tipo")])
    image = page.grab()
    assert not image.isNull()
    hint = page.recent_list.sizeHintForRow(0)
    # Due righe di testo: piu' alta di una riga di testo sola.
    assert hint > page.fontMetrics().height() * 2


# ── Segnali: comandi grandi ──────────────────────────────────────────────

def test_the_two_large_buttons_emit_their_signals(page):
    new_spy, open_spy = QSignalSpy(page.new_requested), QSignalSpy(page.open_requested)
    QTest.mouseClick(page.btn_new, Qt.MouseButton.LeftButton)
    assert (len(new_spy), len(open_spy)) == (1, 0)
    QTest.mouseClick(page.btn_open, Qt.MouseButton.LeftButton)
    assert (len(new_spy), len(open_spy)) == (1, 1)


def test_the_large_buttons_have_icon_tooltip_and_catalogue_texts(page):
    for button in (page.btn_new, page.btn_open):
        assert not button.icon().isNull()
        assert button.toolTip()
    assert page.btn_new.text() == "Nuovo progetto..."
    assert page.btn_open.text() == "Apri progetto..."


# ── Segnali: elenco dei recenti ──────────────────────────────────────────

def test_one_left_click_reopens_the_project(page, tmp_path):
    a, b = _project(tmp_path, "A"), _project(tmp_path, "B")
    page.set_recent([a, b])
    spy = QSignalSpy(page.open_recent_requested)
    QTest.mouseClick(page.recent_list.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, _row_center(page, 1))
    assert [args[0] for args in spy] == [str(b)]


def test_a_right_click_does_not_open(page, tmp_path, monkeypatch):
    page.set_recent([_project(tmp_path, "A")])
    monkeypatch.setattr(QMenu, "exec", lambda *args, **kwargs: None)
    spy = QSignalSpy(page.open_recent_requested)
    QTest.mouseClick(page.recent_list.viewport(), Qt.MouseButton.RightButton,
                     Qt.KeyboardModifier.NoModifier, _row_center(page, 0))
    assert len(spy) == 0


def test_a_click_on_the_empty_area_below_the_rows_does_not_open(page, tmp_path):
    page.set_recent([_project(tmp_path, "A")])
    spy = QSignalSpy(page.open_recent_requested)
    below = page.recent_list.viewport().rect().bottomLeft() + QPoint(10, -5)
    assert page.recent_list.itemAt(below) is None
    QTest.mouseClick(page.recent_list.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, below)
    assert len(spy) == 0


def test_a_double_click_opens_once_not_twice(page, tmp_path):
    a = _project(tmp_path, "A")
    page.set_recent([a])
    spy = QSignalSpy(page.open_recent_requested)
    point = _row_center(page, 0)
    viewport = page.recent_list.viewport()
    # Un doppio clic vero: clic, poi doppio clic sulla stessa riga.
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert [args[0] for args in spy] == [str(a)]


def test_the_double_click_alone_opens(page, tmp_path):
    a = _project(tmp_path, "A")
    page.set_recent([a])
    spy = QSignalSpy(page.open_recent_requested)
    page.recent_list.doubleClicked.emit(page.recent_list.model().index(0, 0))
    assert [args[0] for args in spy] == [str(a)]


def test_the_open_guard_does_not_swallow_a_different_project(page, tmp_path):
    """@brief Negativo della guardia del doppio clic: un'altra riga si apre subito."""
    a, b = _project(tmp_path, "A"), _project(tmp_path, "B")
    page.set_recent([a, b])
    spy = QSignalSpy(page.open_recent_requested)
    viewport = page.recent_list.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     _row_center(page, 0))
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     _row_center(page, 1))
    assert [args[0] for args in spy] == [str(a), str(b)]


def test_enter_opens_the_current_row_and_delete_asks_to_forget_it(page, tmp_path):
    a, b = _project(tmp_path, "A"), _project(tmp_path, "B")
    page.set_recent([a, b])
    open_spy = QSignalSpy(page.open_recent_requested)
    remove_spy = QSignalSpy(page.remove_recent_requested)
    page.recent_list.setFocus()
    page.recent_list.setCurrentRow(1)
    QTest.keyClick(page.recent_list, Qt.Key.Key_Return)
    assert [args[0] for args in open_spy] == [str(b)]
    QTest.keyClick(page.recent_list, Qt.Key.Key_Delete)
    assert [args[0] for args in remove_spy] == [str(b)]
    # Negativo: le frecce spostano la scelta, non aprono nulla.
    QTest.keyClick(page.recent_list, Qt.Key.Key_Up)
    assert page.recent_list.currentRow() == 0
    assert len(open_spy) == 1


def test_the_context_menu_offers_open_and_remove(page, tmp_path):
    a = _project(tmp_path, "A")
    page.set_recent([a])
    menu = page.context_menu_for(page.recent_list.item(0))
    texts = [action.text() for action in menu.actions()]
    assert texts == ["Apri progetto", "Rimuovi dall'elenco"]
    open_spy = QSignalSpy(page.open_recent_requested)
    remove_spy = QSignalSpy(page.remove_recent_requested)
    menu.actions()[1].trigger()
    assert [args[0] for args in remove_spy] == [str(a)]
    assert len(open_spy) == 0
    menu.actions()[0].trigger()
    assert [args[0] for args in open_spy] == [str(a)]


def test_right_click_shows_the_menu_of_that_row(page, tmp_path, monkeypatch):
    a, b = _project(tmp_path, "A"), _project(tmp_path, "B")
    page.set_recent([a, b])
    remove_spy = QSignalSpy(page.remove_recent_requested)

    def choose_remove(menu, *_args, **_kwargs):
        menu.actions()[1].trigger()
    monkeypatch.setattr(QMenu, "exec", choose_remove)
    page._on_context_menu(_row_center(page, 1))
    assert [args[0] for args in remove_spy] == [str(b)]
    assert page.recent_list.currentRow() == 1
    # Negativo: fuori dalle righe nessun menu, nessun segnale.
    shown = []
    monkeypatch.setattr(QMenu, "exec", lambda *args, **kwargs: shown.append(1))
    page._on_context_menu(page.recent_list.viewport().rect().bottomLeft() + QPoint(10, -5))
    assert shown == [] and len(remove_spy) == 1


def test_the_page_does_not_remove_rows_by_itself(page, tmp_path):
    """@brief La lista a video resta quella salvata: toglie la finestra, poi set_recent."""
    a = _project(tmp_path, "A")
    page.set_recent([a])
    page.context_menu_for(page.recent_list.item(0)).actions()[1].trigger()
    assert page.recent_paths() == [str(a)]


def test_context_menu_texts_use_the_translator(app, tmp_path):
    widget = StartPage(tr=lambda text: {"Apri progetto": "Open project",
                                        "Rimuovi dall'elenco": "Remove from list"}.get(text, text))
    widget.set_recent([_project(tmp_path, "A")])
    menu = widget.context_menu_for(widget.recent_list.item(0))
    assert [action.text() for action in menu.actions()] == ["Open project", "Remove from list"]


# ── Traduzione dei testi fissi ───────────────────────────────────────────

def test_fixed_texts_are_left_to_the_generic_retranslation(page):
    """@brief Etichette e pulsanti hanno il testo sorgente italiano; titolo e logo sono esclusi."""
    assert page.title.property("i18n_skip") is True
    assert page.logo.property("i18n_skip") is True
    assert page.recent_header.property("i18n_skip") is None
    assert page.subtitle.property("i18n_skip") is None
    assert page.btn_new.property("i18n_skip") is None


def test_retranslate_widgets_translates_the_page(app, tmp_path):
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    widget = StartPage(tr=i18n.text)
    i18n.retranslate_widgets(widget)
    # Testi gia' nel catalogo: devono uscire in inglese.
    assert widget.btn_new.text() == "New project..."
    assert widget.btn_open.text() == "Open project..."
    assert widget.btn_new.toolTip().startswith("Creates the folder of a new component")
    # Negativo: il nome del prodotto non si traduce.
    assert widget.title.text() == "KiCadGen"


def test_a_scrolling_list_keeps_rows_inside_the_viewport(page, tmp_path):
    """@brief Con la lista che scorre: nessuna barra orizzontale, righe dentro la vista.

    Prima la riga chiedeva la larghezza intera della lista: comparsa la barra
    verticale, ne compariva anche una orizzontale e le schede finivano sotto
    la verticale (visto a 200%). Negativo: con poche righe non scorre nulla.
    """
    page.set_recent([_project(tmp_path, f"P{i}") for i in range(2)])
    QApplication.processEvents()
    assert not page.recent_list.verticalScrollBar().isVisible()
    page.set_recent([_project(tmp_path / "molti", f"P{i}") for i in range(rp.MAX_RECENT)])
    page.resize(700, 420)
    QApplication.processEvents()
    assert page.recent_list.verticalScrollBar().isVisible()
    assert not page.recent_list.horizontalScrollBar().isVisible()
    viewport = page.recent_list.viewport().width()
    widths = {page.recent_list.visualItemRect(page.recent_list.item(row)).width()
              for row in range(page.recent_list.count())}
    assert widths and max(widths) <= viewport
