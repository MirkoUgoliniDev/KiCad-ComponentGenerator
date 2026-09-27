"""@brief Tab 3D: barra di icone, niente testi fissi, stato solo per errori e avvisi.

Richiesta dell'utente (2026-09-23): via i testi che distolgono l'attenzione,
cubo al posto dei pulsanti «Vista», barra di icone con numeri pin e pinout;
i tooltip delle icone devono essere tradotti.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QLabel, QPushButton, QToolButton

from kicadgen.gui import main_window as gui
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES
from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


def _toolbar(window):
    # «Inquadra» e' passato accanto al cubo di navigazione (utente, 27/09).
    return [window.chk_pin_numbers, window.btn_pinout_3d, window.btn_open_3dshapes]


def test_view_tools_are_icons_with_translated_tooltips(window):
    for button in _toolbar(window):
        assert isinstance(button, QToolButton)
        assert not button.icon().isNull()
        assert button.text() == ""
        source = button.property("i18n_tooltip_source") or button.toolTip()
        entry = DEFAULT_TRANSLATIONS.get("ui:" + source)
        assert entry is not None, source
        assert all(entry.get(language) for language in LANGUAGES), source


def test_tooltips_follow_the_language(window):
    index = window.language_combo.findData("en")
    window._on_language_changed(index)
    assert window.btn_pinout_3d.toolTip().startswith("Pinout table")
    assert window.chk_pin_numbers.accessibleName().startswith("Pin numbers")


def test_pin_numbers_is_a_toggle_and_the_pinout_stays_reachable(window):
    assert window.chk_pin_numbers.isCheckable()
    window.chk_pin_numbers.setChecked(False)
    # Prima il pinout spariva con i numeri spenti: in una barra un'icona che
    # compare e scompare sposta le altre sotto il mouse.
    assert not window.btn_pinout_3d.isHidden()
    window.chk_pin_numbers.setChecked(True)
    assert not window.btn_pinout_3d.isHidden()


def test_the_view_buttons_and_the_fixed_hint_are_gone(window):
    tab = window.preview_3d.parentWidget()
    texts = {button.text() for button in tab.findChildren(QPushButton)}
    assert not texts & {"Iso", "Sopra", "Sotto", "Fronte", "Retro", "Sinistra",
                        "Destra", "Centra", "Apri cartella 3dshapes"}
    labels = [label.text() for label in tab.findChildren(QLabel)]
    assert not any(text.startswith("Anteprima 3D (ruotabile") for text in labels)
    assert not any(text == "Vista:" for text in labels)


def test_normal_outcomes_are_not_shown_under_the_model(window):
    window._set_3d_status("Modello STEP aggiornato: x.step", gui.STATUS_INFO)
    assert window.label_3d_status.isHidden()
    # Il testo resta: chi aggiunge una riga o controlla l'esito lo ritrova.
    assert window.label_3d_status.text() == "Modello STEP aggiornato: x.step"


@pytest.mark.parametrize("level", [gui.STATUS_WARNING, gui.STATUS_ERROR])
def test_warnings_and_errors_stay_visible(window, level):
    window._set_3d_status("Generazione STEP fallita: FreeCAD assente", level)
    assert not window.label_3d_status.isHidden()
    # Un esito normale successivo li toglie: lo stato e' cambiato.
    window._set_3d_status("ok", gui.STATUS_INFO)
    assert window.label_3d_status.isHidden()


def test_a_failed_generation_is_shown_and_a_good_one_is_not(window, monkeypatch):
    monkeypatch.setattr(window, "_cad_result_is_current", lambda: True)
    window._on_regen_3d_error("FreeCAD non trovato")
    assert not window.label_3d_status.isHidden()
    monkeypatch.setattr(window, "_show_generated_mesh", lambda path: None)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: True)
    window._on_regen_3d_done("modello.step")
    assert window.label_3d_status.isHidden()


def test_cube_labels_are_translated_with_the_language(window):
    seen = []

    class Recorder:
        def set_view_cube_labels(self, labels):
            seen.append(labels)

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    window.preview_3d = Recorder()
    window._on_language_changed(window.language_combo.findData("de"))
    assert seen[-1]["top"] == "Oben"
    assert seen[-1]["front"] == DEFAULT_TRANSLATIONS["ui:Fronte"]["de"]
