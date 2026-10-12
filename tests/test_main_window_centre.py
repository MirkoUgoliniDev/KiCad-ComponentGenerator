"""@brief The main window opens in the middle of its screen (user, 2026-09-27)."""
import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

from test_photo_inventory_gui import gui


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


def test_centring_before_the_show_leaves_nothing_to_move_at_the_show(window):
    """Centred while hidden, the window appears in place: moving a window
    already on screen played as a close-and-reopen in the desktop's open
    animation (user, 2026-10-12)."""
    window._centred_once = False
    window.move(0, 0)
    window.centre_before_show()
    assert window._centred_once is True
    offset, _ = _centre_offset(window)
    assert abs(offset.x()) <= 1 and abs(offset.y()) <= 1, "centred while still hidden"
    frame_before = window.frameGeometry()
    moves = []
    original = window.move

    def spying_move(*args):
        moves.append(args)
        return original(*args)
    window.move = spying_move
    window.show()
    QApplication.processEvents()
    assert moves == [], "showEvent found the window already centred"
    # The offscreen platform learns its frame margins only at the show (the
    # frame grows by a few pixels); what must not change is the position.
    assert window.frameGeometry().topLeft() == frame_before.topLeft()
