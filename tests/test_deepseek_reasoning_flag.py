"""@brief HANDOFF 161: the «Ragionamento DeepSeek» checkbox on the «Modello AI» row.

Off by default, it flips the provider switch that `openrouter.call_tool`
reads for the direct DeepSeek API (thinking enabled, tool_choice required),
and it is saved with the project like the other Project-tab controls.
"""

from kicadgen.services import providers
from test_photo_inventory_gui import gui  # noqa: F401  (window fixture)


def test_the_checkbox_is_off_by_default_and_drives_the_provider_switch(window):  # noqa: F811
    providers.set_deepseek_reasoning(False)
    box = window.chk_deepseek_reasoning
    assert not box.isChecked() and providers.deepseek_reasoning_enabled() is False
    assert box.text() == "Ragionamento DeepSeek"
    box.setChecked(True)
    assert providers.deepseek_reasoning_enabled() is True
    box.setChecked(False)
    assert providers.deepseek_reasoning_enabled() is False


def test_the_checkbox_is_saved_and_restored_with_the_project(window):  # noqa: F811
    window.chk_deepseek_reasoning.setChecked(True)
    state = window._project_state()
    assert state["widgets"]["chk_deepseek_reasoning"] is True
    window.chk_deepseek_reasoning.setChecked(False)
    assert providers.deepseek_reasoning_enabled() is False
    window._apply_project_state(state)
    assert window.chk_deepseek_reasoning.isChecked()
    assert providers.deepseek_reasoning_enabled() is True
    window.chk_deepseek_reasoning.setChecked(False)
