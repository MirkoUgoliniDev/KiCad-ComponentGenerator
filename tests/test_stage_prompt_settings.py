"""@brief «Modello AI»: source, single model and the step table; no prompts (2026-11-12).

User decision 2026-10-02: the analysis used one model for every request, while
three tabs showed three models; the per-stage model fields were never read.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialogButtonBox, QLabel, QLineEdit, QTextEdit
from kicadgen.gui.settings_dialog import LlmSettingsDialog
from kicadgen.services.datasheet_extractor import DEFAULT_PAD_POSITIONS_MODEL
from kicadgen.services.openrouter import DEFAULT_MODEL
from kicadgen.services.providers import PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER


def test_the_window_holds_only_the_models_and_points_to_the_profile_manager(qapp):
    """@brief CONTRACT CHANGED 2026-11-12 (user: «continuo a vedere prompt qui», then «dobbiamo
    semplificare la UX»): no prompt is edited here; the help says where the prompts are; the
    restore button resets the step table only, never the single model."""
    d = LlmSettingsDialog("custom datasheet", "vendor/data", current_grounding_prompt="custom photo",
                          current_verification_prompt="custom verify", current_pad_model="")
    try:
        assert d.windowTitle() == "Modello AI"
        assert not d.findChildren(QTextEdit), "no prompt editor in the model window"
        assert not hasattr(d, "tabs") and not hasattr(d, "prompt") and not hasattr(d, "grounding_prompt")
        assert "Gestione tipologie e profili AI" in d._help_text
        d._restore_step_defaults()
        assert d.model() == "vendor/data" and d.pad_model() == DEFAULT_PAD_POSITIONS_MODEL
    finally:
        d.close()


def test_one_model_serves_every_stage(qapp, monkeypatch):
    """@brief The old per-stage arguments are accepted but no longer make a second model."""
    from kicadgen.gui import settings_dialog
    d = LlmSettingsDialog('prompt', 'vendor/data', current_vision_model='vendor/photo',
                          current_verification_model='vendor/verify')
    try:
        assert d.vision_model() == d.verification_model() == d.model() == 'vendor/data'
        seen = {}

        def pick(current, parent, **kwargs):
            seen.update(kwargs)
            return 'vendor/new'
        monkeypatch.setattr(settings_dialog, 'pick_model', pick)
        d._pick_model()
        # CONTRACT CHANGED 2026-11-12: no source combo; the picker offers the
        # sources that have a key (`providers`), never a fixed one.
        assert "provider" not in seen and "providers" in seen
        assert d.vision_model() == d.verification_model() == d.model() == 'vendor/new'
        # CONTRACT CHANGED 2026-11-12: one model field only, the single model;
        # the old «Posizioni pad» field became two rows of the step table.
        assert [field.objectName() for field in d.findChildren(QLineEdit)] == ["single_model"]
    finally:
        d.close()


def test_the_source_is_the_provider_of_the_model_shown_in_parentheses(qapp, monkeypatch):
    """@brief CONTRACT CHANGED 2026-11-12 (user: «dobbiamo semplificare la UX»): no «Sorgente» combo.

    @note Positive: the source label follows the model. Negative: there is no combo to change it.
    """
    from PyQt6.QtWidgets import QComboBox
    d = LlmSettingsDialog('prompt', 'vendor/data')
    try:
        assert d.source() == PROVIDER_OPENROUTER and d.label_source.text() == "(OpenRouter)"
        d._set_single_model("deepseek-api/deepseek-flash")
        assert d.source() == PROVIDER_DEEPSEEK and "DeepSeek" in d.label_source.text()
        assert not d.findChildren(QComboBox) and not hasattr(d, "combo_source")
    finally:
        d.close()


def test_the_picker_lists_only_the_sources_with_a_key(qapp, monkeypatch):
    """@brief User (2026-11-12): «se la chiave non è presente non dovrebbe neppure apparire il modello».

    @note Positive: with only DeepSeek's key the single-model and step pickers get DeepSeek
        only. Negative: with both keys both sources are offered.
    """
    from kicadgen.gui import settings_dialog
    from kicadgen.services import openrouter, providers
    present = openrouter.ApiKeySource(openrouter.KEY_SOURCE_DOTENV, ".env")
    absent = openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE)
    monkeypatch.setattr(providers, "deepseek_key_source", lambda: present)
    calls = []
    monkeypatch.setattr(settings_dialog, "pick_model",
                        lambda *a, **k: calls.append(k.get("providers")) or None)
    for openrouter_key, expected in ((False, (PROVIDER_DEEPSEEK,)),
                                     (True, (PROVIDER_OPENROUTER, PROVIDER_DEEPSEEK))):
        monkeypatch.setattr(openrouter, "api_key_source",
                            lambda key=openrouter_key: present if key else absent)
        d = LlmSettingsDialog('prompt', 'deepseek-api/deepseek-flash')
        try:
            calls.clear()
            d._pick_model()
            d._pick_step_model("crop_prompt")
            assert [set(c) for c in calls] == [set(expected), set(expected)]
        finally:
            d.close()


def test_with_no_key_only_the_account_row_is_shown(qapp, monkeypatch):
    """@brief Positive: no source has a key -> the model controls are hidden, one line and «Account AI...».

    @note Negative: one key present shows the model row again and no warning.
    """
    from kicadgen.services import openrouter, providers
    present = openrouter.ApiKeySource(openrouter.KEY_SOURCE_DOTENV, ".env")
    absent = openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE)
    monkeypatch.setattr(openrouter, "api_key_source", lambda: absent)
    monkeypatch.setattr(providers, "deepseek_key_source", lambda: absent)
    d = LlmSettingsDialog('prompt', 'deepseek-api/deepseek-flash', current_pad_model="")
    try:
        assert d.single_row.isHidden() and d.steps_box.isHidden()
        assert d.btn_restore_steps.isHidden()
        assert not d.keys_row.isHidden() and "Nessuna sorgente" in d.label_keys_used.text()
        monkeypatch.setattr(providers, "deepseek_key_source", lambda: present)
        d._refresh_key_state()
        assert not d.single_row.isHidden() and not d.steps_box.isHidden()
        assert d.keys_row.isHidden()
    finally:
        d.close()


def test_a_saved_single_model_on_a_keyless_source_is_named_in_the_warning(qapp, monkeypatch):
    """@brief Positive: the single model's source lost its key -> the warning names the model.

    @note Negative: with that key present no warning is shown.
    """
    from kicadgen.services import openrouter, providers
    present = openrouter.ApiKeySource(openrouter.KEY_SOURCE_DOTENV, ".env")
    absent = openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE)
    monkeypatch.setattr(providers, "deepseek_key_source", lambda: present)
    monkeypatch.setattr(openrouter, "api_key_source", lambda: absent)
    d = LlmSettingsDialog('prompt', 'vendor/data', current_pad_model="")
    try:
        assert not d.keys_row.isHidden()
        assert "modello unico vendor/data" in d.label_keys_used.text()
        monkeypatch.setattr(openrouter, "api_key_source", lambda: present)
        d._refresh_key_state()
        assert d.keys_row.isHidden()
    finally:
        d.close()


def test_the_model_explanation_is_only_in_the_info_modal(qapp):
    """@brief The help text stays out of the window and opens in a read-only modal (the prompt
    tabs and their explanations moved to the profile manager on 2026-11-12)."""
    app = qapp
    expected = "Il modello unico esegue ogni passo dell'analisi"
    d = LlmSettingsDialog("custom datasheet", "vendor/datasheet")
    observed = {}
    try:
        d.show()
        app.processEvents()
        assert all(expected not in label.text() for label in d.findChildren(QLabel))
        assert len(d.info_buttons) == 1
        button = d.info_buttons[0]
        assert not button.icon().isNull() and button.isVisible()
        assert "Modello AI" in button.accessibleName()

        def inspect_and_close():
            modal = app.activeModalWidget()
            observed["separate"] = modal is not None and modal is not d
            if not observed["separate"]:
                return
            observed["modal"] = modal.isModal()
            observed["parent"] = modal.parentWidget() is d
            observed["title"] = modal.windowTitle()
            editor = modal.findChild(QTextEdit)
            observed["text"] = editor.toPlainText()
            observed["readonly"] = editor.isReadOnly()
            modal.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Close).click()

        QTimer.singleShot(0, inspect_and_close)
        button.click()
        assert observed["separate"] and observed["modal"] and observed["parent"]
        assert "Modello AI" in observed["title"]
        assert expected in observed["text"] and observed["readonly"]
        assert d.isVisible() and d.model() == "vendor/datasheet"
    finally:
        d.close()


@pytest.mark.parametrize("provider", [PROVIDER_DEEPSEEK, None])
def test_the_picker_locks_the_source_only_when_the_caller_fixes_it(qapp, monkeypatch, provider):
    """@brief From the «Modello» tab the list is the chosen source's; elsewhere the user picks."""
    from kicadgen.gui import model_dialog
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models", lambda self, *a, **k: None,
                        raising=False)
    dialog = model_dialog.ModelPickerDialog("vendor/data", None, provider=provider)
    try:
        expected = provider or PROVIDER_OPENROUTER
        assert dialog.combo_provider.currentData() == expected
        assert dialog.combo_provider.isEnabled() is (provider is None)
    finally:
        dialog._closed = True
        dialog.close()


def test_the_provider_names_follow_the_language(qapp, tmp_path):
    """@brief Review B7 (2026-10-02): the source label is built translated.

    @note Negative: OpenRouter is a name and stays as it is.
    """
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    from kicadgen.services.messages import set_translator
    set_translator(I18n(ProfileStore(tmp_path / "p.sqlite3"), "en").raw)
    try:
        d = LlmSettingsDialog('prompt', 'deepseek-api/deepseek-flash')
        assert d.label_source.text() == "(DeepSeek (direct API))"
        d._set_single_model('vendor/data')
        assert d.label_source.text() == "(OpenRouter)"
        d.close()
    finally:
        set_translator(None)
