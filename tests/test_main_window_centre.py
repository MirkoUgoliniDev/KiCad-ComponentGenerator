"""@brief The main window opens in the middle of its screen (user, 2026-09-27)."""
import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa


def _centre_offset(window):
    area = window.screen().availableGeometry()
    return window.frameGeometry().center() - area.center(), area


def test_the_first_show_centres_the_frame_on_the_available_area(window):
    window._centred_once = False
    window.move(0, 0)
    window.show()
    QApplication.processEvents()
    offset, _ = _centre_offset(window)
    # QRect.center() rounds down: one pixel per axis is the integer rounding.
    assert abs(offset.x()) <= 1 and abs(offset.y()) <= 1


def test_later_shows_keep_the_position_the_user_chose(window):
    window.show()
    QApplication.processEvents()
    window.hide()
    window.move(QPoint(40, 30))
    window.show()
    QApplication.processEvents()
    assert window.pos() == QPoint(40, 30)


def test_a_window_larger_than_the_area_keeps_its_title_bar_on_screen(window, monkeypatch):
    """Negative: centring must never push the top-left corner off the screen."""
    area = window.screen().availableGeometry()
    frame = window.frameGeometry()
    frame.setSize(area.size() * 2)
    monkeypatch.setattr(window, "frameGeometry", lambda: frame)
    moved = []
    monkeypatch.setattr(window, "move", lambda x, y: moved.append((x, y)))
    window._centre_on_screen()
    assert moved == [(area.left(), area.top())]
