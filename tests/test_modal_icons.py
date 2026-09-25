"""@brief Icone con tooltip nella modale di avanzamento e nelle finestre successive.

Richiesta dell'utente del 2026-09-24: «icone anche qui, con mouse over, e in
tutte le modali successive». Le conferme in fondo alle finestre (Applica,
Usa il modello selezionato, Annulla, OK) restano scritte: sono la decisione
della finestra, e il negativo di ogni test lo verifica.
"""
import pytest
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QToolButton

from tests.test_photo_inventory_gui import window, gui  # noqa: F401
from tests.test_review_windows_fixes import _component, _photo


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _is_icon_command(button, name):
    return (isinstance(button, QToolButton) and button.text() == ""
            and not button.icon().isNull()
            and button.toolTip().splitlines()[0] == name
            and len(button.toolTip().splitlines()) >= 2)


def test_the_progress_modal_commands_are_icons_with_tooltips(window):  # noqa: F811
    """@brief Positivo: log e interruzione sono icone con nome e spiegazione, nella lingua scelta."""
    overlay = window.analysis_overlay
    assert _is_icon_command(overlay.log_button, "Apri log AI")
    assert _is_icon_command(overlay.abort_button, "Interrompi")
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    assert overlay.log_button.toolTip().splitlines()[0] == "Open AI log"
    assert overlay.abort_button.toolTip().splitlines()[0] == "Stop"


def test_the_component_review_actions_are_icons_and_apply_stays_text(app):
    dialog = _photo(app, [_component("R1", "resistor", -10, 0, 2, 1)])
    assert _is_icon_command(dialog.parameters_button, "Parametri…")
    assert _is_icon_command(dialog.model_button, "Scegli modello KiCad…")
    apply = dialog.buttons.button(QDialogButtonBox.StandardButton.Apply)
    assert apply.text() == "Applica correzioni"
    dialog.close()


def test_the_step_choice_actions_and_confirms_are_icons(app):
    """Contratto cambiato di proposito (utente, 2026-09-25): anche le conferme
    della scelta STEP sono icone con nome e spiegazione. Restano i pulsanti OK e
    Annulla della finestra (Invio/Esc). Negativo: le conferme delle ALTRE finestre
    restano scritte (vedi `test_the_component_review_actions_are_icons_and_apply_stays_text`)."""
    from kicadgen.gui import model_candidates_dialog as mcd
    dialog = mcd.ModelCandidatesDialog({"name": "U1", "length_mm": 3, "width_mm": 3,
                                        "height_mm": 1}, {"candidates": []})
    assert _is_icon_command(dialog.manual_button, "Scegli file STEP…")
    assert _is_icon_command(dialog.none_button, "Nessuno corrisponde — conserva il segnaposto")
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    cancel = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
    for button, name in ((ok, "Usa il modello selezionato"), (cancel, "Annulla")):
        assert button.text() == "" and not button.icon().isNull()
        first, _, rest = button.toolTip().partition("\n")
        assert first == name and rest
    assert not ok.isEnabled()  # nessun modello scelto: la conferma resta spenta
    dialog.close()


def test_the_progress_modal_icons_are_larger_than_the_toolbar_ones(window, app):  # noqa: F811
    """@brief Le icone della modale sono piu' grandi, solo nella modale (utente, 2026-09-24).

    Positivo: log e interruzione alla misura della modale. Negativo: le icone
    delle barre (qui la colonna della scelta STEP) restano alla misura comune.
    """
    from kicadgen.gui import analysis_overlay as ov
    from kicadgen.gui import model_candidates_dialog as mcd
    overlay = window.analysis_overlay
    for button in (overlay.log_button, overlay.abort_button):
        assert button.iconSize().width() == ov.OVERLAY_ICON_PX
        assert button.sizeHint().width() >= ov.OVERLAY_ICON_PX
    dialog = mcd.ModelCandidatesDialog({"name": "U1", "length_mm": 3, "width_mm": 3,
                                        "height_mm": 1}, {"candidates": []})
    assert dialog.rotate_ccw_button.iconSize().width() < ov.OVERLAY_ICON_PX
    dialog.close()
