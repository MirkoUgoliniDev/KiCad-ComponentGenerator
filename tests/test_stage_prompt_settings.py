import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication
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
