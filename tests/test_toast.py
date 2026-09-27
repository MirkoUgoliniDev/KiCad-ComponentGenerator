"""@brief The non-modal notification card (user, 2026-09-27: «popup professionale»)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QWidget

from kicadgen.gui import toast as toast_module
from kicadgen.gui.toast import Toast


@pytest.fixture
def card():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner.show()
    widget = Toast(owner, "format-painter.svg")
    yield widget, app
    widget.close()
    owner.close()


def test_the_card_informs_without_taking_the_focus(card):
    widget, app = card
    widget.show_message("Formato di Q1 copiato", "Misure e modello.", "Clic sugli altri.")
    app.processEvents()
    assert widget.isVisible() and not widget.isModal()
    assert widget.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert widget.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert (widget.title.text(), widget.body.text(), widget.hint.text()) == (
        "Formato di Q1 copiato", "Misure e modello.", "Clic sugli altri.")


def test_a_new_message_replaces_the_previous_one_and_hides_empty_lines(card):
    widget, app = card
    widget.show_message("Primo", "testo", "suggerimento")
    widget.show_message("Incollato su Q2")
    app.processEvents()
    assert widget.title.text() == "Incollato su Q2"
    assert widget.body.isHidden() and widget.hint.isHidden()


def test_the_card_goes_away_by_itself(card, monkeypatch):
    widget, app = card
    monkeypatch.setattr(toast_module, "TOAST_FADE_MS", 1)
    widget.show_message("Formato applicato")
    widget._fade_out()                       # the timer's action, without waiting
    widget._after_fade()
    assert not widget.isVisible()


def test_the_card_stays_inside_the_screen_near_its_corner(card):
    widget, _ = card
    screen = QGuiApplication.primaryScreen().availableGeometry()
    widget.show_message("Titolo", "Un testo un po' lungo per dare larghezza alla scheda.")
    corner = QPoint(screen.right() - 2, screen.bottom() - 2)
    position = widget._position(corner)
    assert screen.contains(position)
    assert position.x() + widget.width() <= screen.right() + 1
    assert position.y() + widget.height() <= screen.bottom() + 1
