import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QTextEdit
from kicadgen.gui.settings_dialog import LlmSettingsDialog
from kicadgen.services.photo_grounding import GROUNDING_PROMPT, VERIFICATION_PROMPT
from kicadgen.services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT


def test_stage_defaults_are_independent_and_empty_uses_fallback():
    app = QApplication.instance() or QApplication([])
    d = LlmSettingsDialog("custom datasheet", current_grounding_prompt="custom photo",
                          current_verification_prompt="custom verify")
    try:
        assert d.tabs.count() == 3
        d.tabs.setCurrentIndex(1)
        d._restore_defaults()
        assert d.grounding_prompt() == GROUNDING_PROMPT
        assert d.prompt() == "custom datasheet"
        assert d.verification_prompt() == "custom verify"
        d.tabs.setCurrentIndex(2)
        d._restore_defaults()
        assert d.verification_prompt() == VERIFICATION_PROMPT
        assert d.prompt() == "custom datasheet"
        d.tabs.setCurrentIndex(0)
        d._restore_defaults()
        assert d.prompt() == DEFAULT_SYSTEM_PROMPT
        d.grounding_editor.setPlainText("  ")
        d.verification_editor.clear()
        assert d.grounding_prompt() == GROUNDING_PROMPT
        assert d.verification_prompt() == VERIFICATION_PROMPT
    finally:
        d.close()


def test_verification_model_picker_is_independent(monkeypatch):
    from kicadgen.gui import settings_dialog
    app = QApplication.instance() or QApplication([])
    d = LlmSettingsDialog('prompt', 'vendor/data', current_vision_model='vendor/photo',
                          current_verification_model='vendor/verify')
    try:
        monkeypatch.setattr(settings_dialog, 'pick_model', lambda *a, **kw: 'vendor/new-data')
        d._pick_model()
        assert d.model() == 'vendor/new-data'
        assert d.verification_model() == 'vendor/verify'
        monkeypatch.setattr(settings_dialog, 'pick_model', lambda *a, **kw: 'vendor/new-verify')
        d._pick_verification_model()
        assert d.verification_model() == 'vendor/new-verify'
        assert d.model() == 'vendor/new-data'
        assert d.vision_model() == 'vendor/photo'
    finally:
        d.close()


@pytest.mark.parametrize("index,expected", [
    # Dal 2026-09-26 la chiave si gestisce da «Account AI...» (PIANO-FIX 2).
    (0, "«Account AI...»"),
    # Dal 2026-09-24 la cache e' dentro il progetto (decisione dell'utente).
    (1, "photo-cache del progetto"),
    (2, "Non modifica coordinate"),
])
def test_stage_explanations_are_only_in_info_modal(index, expected):
    app = QApplication.instance() or QApplication([])
    d = LlmSettingsDialog("custom datasheet", "vendor/datasheet",
                          current_vision_model="vendor/photo",
                          current_grounding_prompt="custom photo",
                          current_verification_prompt="custom verify")
    observed = {}
    try:
        d.tabs.setCurrentIndex(index)
        d.show()
        app.processEvents()
        page = d.tabs.widget(index)
        assert all(expected not in label.text() for label in page.findChildren(QLabel))
        assert len(page.findChildren(QTextEdit)) == 1  # Only the editable prompt.
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
        assert (d.model(), d.vision_model()) == ("vendor/datasheet", "vendor/photo")
    finally:
        d.close()
