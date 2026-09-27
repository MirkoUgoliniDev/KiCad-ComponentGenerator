"""@brief Qt's standard buttons follow the application language (utente, 2026-09-26).

La domanda «Inventario AI completato» era in italiano ma i pulsanti dicevano
«Yes / No»: l'app non installava il catalogo di Qt, quindi ogni pulsante
standard di QMessageBox e QDialogButtonBox restava in inglese in tutte le
lingue. Il controllo e' sul pulsante vero di un QMessageBox qualunque, non su
una finestra in particolare: la correzione vale per tutte.
"""
import pytest
from PyQt6.QtWidgets import QMessageBox

from test_photo_inventory_gui import gui
from kicadgen.gui.qt_translations import install_qt_translations


def yes_text() -> str:
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    text = box.button(QMessageBox.StandardButton.Yes).text().replace("&", "")
    box.deleteLater()
    return text


@pytest.fixture(autouse=True)
def english_afterwards():
    """Gli altri test sono nati senza catalogo di Qt: lo si toglie alla fine."""
    yield
    install_qt_translations("none")


def test_the_buttons_of_a_message_box_are_in_the_window_language(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    assert window.i18n.language == "it"
    assert yes_text() not in ("Yes", "") and yes_text().startswith("S")
    window.language_combo.setCurrentIndex(window.language_combo.findData("de"))
    assert yes_text() == "Ja"
    # Negativo: in inglese resta Yes, non l'ultima lingua installata.
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    assert yes_text() == "Yes"


def test_a_language_without_a_qt_catalogue_falls_back_to_english_not_the_previous_one(qapp):
    assert install_qt_translations("fr")
    assert yes_text() == "Oui"
    assert not install_qt_translations("none")
    assert yes_text() == "Yes"
