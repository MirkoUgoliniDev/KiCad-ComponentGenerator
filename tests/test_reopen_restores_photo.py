"""@brief A reopened project gets back its calibrated photo from the review bundle.

User report (2026-09-26): after reopening a project there was no way to change
the KiCad model of a body. The photo lived only in memory after an extraction;
the project's photo-review bundle keeps the calibrated photo the bodies are
measured on, so it is reloaded from there.
"""
from PyQt6.QtWidgets import QMessageBox

from kicadgen.services import photo_inventory as inv
from tests.test_photo_inventory_gui import gui
from tests.test_reopen_detector_review import LENGTH, WIDTH, _photo_png, _proposal


def test_the_bundle_photo_comes_back_and_the_review_is_offered(window):  # noqa: F811
    window.field_name.setText("Riaperto")
    png = _photo_png()
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], png, LENGTH, WIDTH)
    window._photo_png = None
    window._restore_saved_photo()
    assert window._photo_png == png
    assert window.btn_edit_component_review.isEnabled()


def test_a_photo_already_in_memory_is_not_replaced(window):  # noqa: F811
    """@brief Negativo: la foto di un'estrazione di questa sessione resta quella."""
    window.field_name.setText("Riaperto")
    inv.save_review_bundle(window._project_dir(), [_proposal(1)], _photo_png(), LENGTH, WIDTH)
    window._photo_png = b"foto della sessione"
    window._restore_saved_photo()
    assert window._photo_png == b"foto della sessione"


def test_without_a_bundle_nothing_is_invented(window):  # noqa: F811
    """@brief Negativo: nessun pacchetto, nessuna foto."""
    window.field_name.setText("SenzaPacchetto")
    window._photo_png = None
    window._restore_saved_photo()
    assert window._photo_png is None


def test_changing_the_model_without_a_photo_says_why(window, monkeypatch):  # noqa: F811
    """@brief Negativo: prima il clic restava muto; ora spiega e indica «Modifica componenti…»."""
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    window._photo_png = None
    window._show_photo_comparison(component_row=0)
    assert shown and "Modifica componenti" in shown[0]
