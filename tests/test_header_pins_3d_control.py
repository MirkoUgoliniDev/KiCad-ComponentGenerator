"""3D header controls share project state without editing host-footprint geometry."""

from copy import deepcopy

from tests.test_project_wizard import select_board
from tests.test_host_mounting import module


def test_3d_header_toggle_roundtrip_preserves_host_and_physical_contacts(window, monkeypatch):
    select_board(window)
    physical = module()
    before = deepcopy(physical)
    monkeypatch.setattr(window, '_current_castellated_params', lambda *_args, **_kwargs: physical)
    window.combo_mounting.setCurrentIndex(1)
    host = window._mounting_state()
    footprint = window._host_footprint_params(physical)
    revision = window._model_revision
    window.chk_header_pins_3d.setChecked(True)
    assert window.chk_header_pins.isChecked()
    assert window._pins_3d_kwargs() == {'show_pins_3d': True}
    assert window._model_revision == revision + 1
    assert window._mounting_state() == host
    assert window._host_footprint_params(physical) == footprint
    assert physical == before
    saved = window._project_state()
    window.chk_header_pins.setChecked(False)
    assert not window.chk_header_pins_3d.isChecked()
    window._apply_project_state(saved)
    assert window.chk_header_pins_3d.isChecked()
    assert window.chk_header_pins.isChecked()


def test_header_control_hidden_for_single_parts_and_disabled_during_regeneration(window):
    select_board(window)
    checkbox = window.chk_header_pins_3d
    assert not checkbox.isHidden()
    window._set_regen_busy(True)
    assert not checkbox.isEnabledTo(checkbox.parentWidget())
    window._set_regen_busy(False)
    assert checkbox.isEnabledTo(checkbox.parentWidget())
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData('single_tht'))
    assert checkbox.isHidden()
