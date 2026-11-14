"""@brief «Strumenti AI»: one read-only «Modelli AI» line instead of the per-session combo (user, 2026-11-13).

@details The analysis always uses the saved default model plus the per-step
models of Configurazione → «Modello AI...». The Progetto tab shows them in ONE
row laid out like its neighbours: «Modelli AI:» in the label column, «predefinito
<model> · <N> passi con un modello proprio» as the value (the count left out
when no step has a model of its own), and an icon button in the button column
opening the existing dialog (user, 2026-11-13: an icon, not a text link).
Every case has its positive and negative twin.
"""

from PyQt6.QtWidgets import QDialog

from kicadgen.gui import main_window as gui
from kicadgen.services.prompt_models import STEPS


def _no_own_steps(window):
    """Save every step as following the default model (blank)."""
    window.profile_store.save_step_models({item.field: "" for item in STEPS})


def _set_language(window, language):
    """Switch the interface language as the user does."""
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))


def test_the_line_replaces_the_session_combo_for_every_type(window):
    """@brief Positive: label, value and button live in «Strumenti AI»; negative: no session combo or picker."""
    for key in ("", "aggregate_castellated", "single_tht"):
        index = window.combo_component_type.findData(key)
        if index >= 0:
            window.combo_component_type.setCurrentIndex(index)
        assert window.group_ai_tools.isAncestorOf(window.label_ai_models)
        assert window.group_ai_tools.isAncestorOf(window.btn_edit_ai_models)
        assert not hasattr(window, "combo_model") and not hasattr(window, "btn_pick_model")


def test_the_row_is_laid_out_like_its_neighbours(window):
    """@brief Positive: title in the label column, icon button sized like the folder buttons.

    @note Negative: the value is plain text, no link («Modifica…» was replaced by the button).
    """
    form = window._models_form
    label = form.itemAt(window._row_ai_model, form.ItemRole.LabelRole).widget()
    assert label is window.label_ai_models_title and label.text() == "Modelli AI:"
    button = window.btn_edit_ai_models
    assert button.size() == window.btn_pick_yolo_model.size()
    assert not button.icon().isNull() and button.text() == ""
    assert button.toolTip() == "Modifica i modelli AI (Configurazione → Modello AI...)"
    assert "<a" not in window.label_ai_models.text() and "Modifica" not in window.label_ai_models.text()


def test_the_count_is_shown_only_when_a_step_has_its_own_model(window):
    """@brief Negative: N=0 omits the count; positive: 1 is singular, 2 plural."""
    window.profile_store.save_defaults({"model": "vendor/default"})
    _no_own_steps(window)
    window._refresh_ai_models_line()
    text = window.label_ai_models.text()
    assert text == "predefinito vendor/default"
    assert "passo" not in text and "passi" not in text
    window.profile_store.save_step_models({"crop_prompt": "vendor/crops"})
    window._refresh_ai_models_line()
    assert "1 passo con un modello proprio" in window.label_ai_models.text()
    window.profile_store.save_step_models({"marking_prompt": "vendor/markings"})
    window._refresh_ai_models_line()
    assert "2 passi con un modello proprio" in window.label_ai_models.text()


def test_the_button_opens_the_models_dialog_and_the_line_follows_the_save(window, monkeypatch):
    """@brief Positive: the button opens «Modello AI...» and an OK refreshes the line.

    @note Negative: a cancelled dialog leaves the line and the saved model unchanged.
    """
    window.profile_store.save_defaults({"model": "vendor/default"})
    _no_own_steps(window)
    window._refresh_ai_models_line()
    opened = []

    class Stub:
        """Stand-in of LlmSettingsDialog: accepts or cancels as told."""
        answer = QDialog.DialogCode.Rejected

        def __init__(self, *, current_model, parent, current_step_models, **kwargs):
            opened.append(current_model)
            self.shown = dict(current_step_models)

        def exec(self):
            return Stub.answer

        def model(self):
            return "vendor/new"

        def vision_model(self):
            return "vendor/new"

        def verification_model(self):
            return "vendor/new"

        def step_models(self):
            return dict(self.shown, crop_prompt="vendor/crops")

    monkeypatch.setattr(gui, "LlmSettingsDialog", Stub)
    window.btn_edit_ai_models.click()
    assert opened == ["vendor/default"]
    assert "vendor/default" in window.label_ai_models.text()
    assert window.profile_store.defaults()["model"] == "vendor/default"
    Stub.answer = QDialog.DialogCode.Accepted
    window.btn_edit_ai_models.click()
    text = window.label_ai_models.text()
    assert "vendor/new" in text and "1 passo con un modello proprio" in text
    assert window._effective_model() == "vendor/new"


def test_the_line_and_the_summary_follow_the_language(window):
    """@brief Positive: in English the line is English; negative: back in Italian it is the source."""
    window.profile_store.save_defaults({"model": "vendor/default"})
    window.profile_store.save_step_models({item.field: "" for item in STEPS}
                                          | {"crop_prompt": "vendor/crops"})
    try:
        _set_language(window, "en")
        assert window.label_ai_models.text() == "default vendor/default · 1 step with a model of its own"
        assert window.label_ai_models_title.text() == "AI models:"
        assert window.btn_edit_ai_models.toolTip() == "Edit the AI models (Configuration → AI model...)"
    finally:
        _set_language(window, "it")
    assert window.label_ai_models.text() == "predefinito vendor/default · 1 passo con un modello proprio"
    assert window.label_ai_models_title.text() == "Modelli AI:"


def test_the_group_summary_names_the_saved_default_never_a_session(window):
    """@brief Positive: the «Strumenti AI» summary names the saved default; negative: no «sessione»."""
    window.profile_store.save_defaults({"model": "vendor/default"})
    window._refresh_ai_models_line()
    summary = window.group_ai_tools.summary.text()
    assert "vendor/default" in summary
    assert "sessione" not in summary.lower()


def test_an_older_project_models_block_is_ignored(window):
    """@brief Negative: a saved per-session model of an older project changes nothing."""
    window.profile_store.save_defaults({"model": "vendor/default"})
    state = window._project_state()
    state["models"] = {"session_model": "vendor/old-session", "vlm_model": "vendor/old-vision"}
    window._apply_project_state(state)
    assert window._effective_model() == window._photo_detector_model() == "vendor/default"
    assert "vendor/old-session" not in window.label_ai_models.text()
