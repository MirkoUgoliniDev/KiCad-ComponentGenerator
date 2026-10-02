"""@brief LLM settings: one «Modello» tab (source + single model), then the prompt tabs.

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
from kicadgen.services.photo_grounding import GROUNDING_PROMPT, VERIFICATION_PROMPT
from kicadgen.services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from kicadgen.services.openrouter import DEFAULT_MODEL
from kicadgen.services.providers import PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER


def test_the_model_tab_comes_first_and_the_prompt_tabs_restore_independently(qapp):
    d = LlmSettingsDialog("custom datasheet", "vendor/data", current_grounding_prompt="custom photo",
                          current_verification_prompt="custom verify")
    try:
        assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == [
            "Modello", "Datasheet", "Localizzazione foto", "Verifica foto"]
        d.tabs.setCurrentIndex(2)
        d._restore_defaults()
        assert d.grounding_prompt() == GROUNDING_PROMPT
        assert d.prompt() == "custom datasheet" and d.verification_prompt() == "custom verify"
        assert d.model() == "vendor/data", "restoring a prompt tab never touches the model"
        d.tabs.setCurrentIndex(3)
        d._restore_defaults()
        assert d.verification_prompt() == VERIFICATION_PROMPT
        d.tabs.setCurrentIndex(1)
        d._restore_defaults()
        assert d.prompt() == DEFAULT_SYSTEM_PROMPT and d.model() == "vendor/data"
        d.tabs.setCurrentIndex(0)
        d._restore_defaults()
        assert d.model() == DEFAULT_MODEL
        d.grounding_editor.setPlainText("  ")
        d.verification_editor.clear()
        assert d.grounding_prompt() == GROUNDING_PROMPT
        assert d.verification_prompt() == VERIFICATION_PROMPT
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
        assert seen["provider"] == PROVIDER_OPENROUTER, "the picker lists only the chosen source"
        assert d.vision_model() == d.verification_model() == d.model() == 'vendor/new'
        # Only the model tab holds a model field.
        for index in (1, 2, 3):
            assert not d.tabs.widget(index).findChildren(QLineEdit)
    finally:
        d.close()


def test_changing_the_source_proposes_that_source_s_model(qapp, monkeypatch):
    """@brief A model of the other source cannot stay; returning keeps a model of that source."""
    from kicadgen.gui import settings_dialog
    monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: None)
    d = LlmSettingsDialog('prompt', 'vendor/data')
    try:
        assert d.source() == PROVIDER_OPENROUTER
        d.combo_source.setCurrentIndex(d.combo_source.findData(PROVIDER_DEEPSEEK))
        assert d.model().startswith("deepseek-api/")
        d.combo_source.setCurrentIndex(d.combo_source.findData(PROVIDER_OPENROUTER))
        assert d.model() == DEFAULT_MODEL
        # Negative: a DeepSeek model opens on the DeepSeek source.
        other = LlmSettingsDialog('prompt', 'deepseek-api/deepseek-flash')
        try:
            assert other.source() == PROVIDER_DEEPSEEK
        finally:
            other.close()
    finally:
        d.close()


def test_the_key_state_names_the_source_and_never_the_key(qapp, monkeypatch):
    from kicadgen.services import openrouter, providers
    monkeypatch.setattr(openrouter, "api_key_source",
                        lambda: openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE))
    monkeypatch.setattr(providers, "deepseek_key_source",
                        lambda: openrouter.ApiKeySource(openrouter.KEY_SOURCE_DOTENV, ".env"))
    d = LlmSettingsDialog('prompt', 'vendor/data')
    try:
        assert "OpenRouter" in d.label_key.text() and "mancante" in d.label_key.text()
        d.combo_source.setCurrentIndex(d.combo_source.findData(PROVIDER_DEEPSEEK))
        assert "DeepSeek" in d.label_key.text() and "presente" in d.label_key.text()
    finally:
        d.close()


@pytest.mark.parametrize("index,expected", [
    (0, "Un solo modello per tutte le interrogazioni"),
    (1, "JSON strutturato"),
    # Dal 2026-09-24 la cache e' dentro il progetto (decisione dell'utente).
    (2, "photo-cache del progetto"),
    (3, "Non modifica coordinate"),
])
def test_stage_explanations_are_only_in_info_modal(qapp, index, expected):
    app = qapp
    d = LlmSettingsDialog("custom datasheet", "vendor/datasheet",
                          current_grounding_prompt="custom photo",
                          current_verification_prompt="custom verify")
    observed = {}
    try:
        d.tabs.setCurrentIndex(index)
        d.show()
        app.processEvents()
        page = d.tabs.widget(index)
        assert all(expected not in label.text() for label in page.findChildren(QLabel))
        # Prompt tabs hold only their editable prompt; the model tab none.
        assert len(page.findChildren(QTextEdit)) == (0 if index == 0 else 1)
        button = d.info_buttons[index]
        assert not button.icon().isNull()
        assert button.isVisible()
        assert d.tabs.tabText(index) in button.accessibleName()

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
        assert d.tabs.tabText(index) in observed["title"]
        assert expected in observed["text"]
        assert observed["readonly"]
        assert d.isVisible()
        assert (d.prompt(), d.grounding_prompt(), d.verification_prompt()) == (
            "custom datasheet", "custom photo", "custom verify")
        assert d.model() == "vendor/datasheet"
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
