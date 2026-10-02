"""@brief The «Ragionamento DeepSeek» checkbox (HANDOFF 161) was removed (user, 2026-10-02).

DeepSeek's thinking mode stays off for every request: it refuses every
tool_choice and spends paid reasoning tokens. A project saved while the
checkbox existed must still open, without turning anything back on.
"""

from kicadgen.services import providers
from test_photo_inventory_gui import gui  # noqa: F401  (window fixture)


def test_the_checkbox_and_the_switch_are_gone(window):  # noqa: F811
    assert not hasattr(window, "chk_deepseek_reasoning")
    assert not hasattr(providers, "set_deepseek_reasoning")
    assert not hasattr(providers, "deepseek_reasoning_enabled")


def test_an_old_project_with_the_checkbox_ticked_still_opens(window):  # noqa: F811
    """@brief Negative: the stored widget value is ignored, not an error."""
    state = window._project_state()
    state.setdefault("widgets", {})["chk_deepseek_reasoning"] = True
    window._apply_project_state(state)
    assert "chk_deepseek_reasoning" not in window._project_state()["widgets"]
