"""@brief La finestra About porta i link al repository e allo sviluppatore (utente, 2026-09-24)."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

from test_photo_inventory_gui import gui


def test_about_links_the_repository_and_the_developer(window, monkeypatch):  # noqa: F811
    """@brief Positivo: due link cliccabili. Negativo: nessun indirizzo email."""
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    window._show_about()
    box = window._about_box
    text = box.text()
    assert f"href='{gui.REPOSITORY_URL}'" in text
    assert f"href='{gui.DEVELOPER_URL}'" in text and gui.DEVELOPER_NAME in text
    assert box.textFormat() == Qt.TextFormat.RichText
    assert box.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse
    assert "@" not in text
    assert gui.REPOSITORY_URL.startswith("https://github.com/")
