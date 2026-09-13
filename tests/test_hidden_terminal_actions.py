"""User decision: manual refinement entry points hidden, implementation retained."""
from test_terminal_metadata_flow import window


def test_manual_terminal_actions_stay_explicitly_hidden(window):
    for button in (window.btn_terminal_detail, window.btn_lead_refinement):
        assert button.isHidden()
        # Worker completion toggles enabled state; it must not reveal them.
        button.setEnabled(False)
        button.setEnabled(True)
        assert button.isHidden()
        assert not button.sizePolicy().retainSizeWhenHidden()
    assert not window.btn_regen_3d.isHidden()
    assert callable(window._refine_photo_terminals)
    assert callable(window._refine_photo_leads)
