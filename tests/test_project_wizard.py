"""@brief Wizard navigation and persistence keep host mounting separate from 3D."""

from copy import deepcopy

import pytest

from kicadgen.core.package_geometry import PackageGeometry
from tests.test_host_mounting import module


def select_board(window):
    """@brief Select the populated castellated-board family."""
    index = window.combo_component_type.findData('aggregate_castellated')
    assert index >= 0
    window.combo_component_type.setCurrentIndex(index)


def test_wizard_revisits_pages_and_keeps_extract_outside_scroll(window, app, tmp_path):
    window.show()
    wizard = window.project_wizard
    wizard.set_page(0)
    assert wizard.next.isVisible() and not window.btn_extract_ai.isVisible()
    wizard.next.click()
    assert wizard.stack.currentIndex() == 1
    assert window.btn_extract_ai.isVisible()
    assert not window._project_scroll.widget().isAncestorOf(window.btn_extract_ai)
    assert not window.group_ai_tools.content.isVisible()
    window.group_ai_tools.toggle.click()
    assert window.group_ai_tools.content.isVisible()
    wizard.back.click()
    assert wizard.stack.currentIndex() == 0
    window._on_workflow_step_clicked('analysis')
    assert wizard.stack.currentIndex() == 1
    window.resize(1100, 820)
    app.processEvents()
    window.grab().save(str(tmp_path / 'wizard-analysis.png'))


def test_mounting_preview_and_project_roundtrip_preserve_board_mesh(window, monkeypatch, tmp_path):
    select_board(window)
    physical = module()
    monkeypatch.setattr(window, '_current_castellated_params', lambda *_args, **_kwargs: physical)
    before = deepcopy(physical)
    mesh = tmp_path / 'unchanged.preview.json'
    window._last_mesh_path = mesh
    revision = window._model_revision
    window.combo_mounting.setCurrentIndex(1)
    window.spin_header_pad.setValue(1.6)
    window.spin_header_drill.setValue(.9)
    host = window._preview_params()
    assert isinstance(host, PackageGeometry)
    assert all(p.shape == 'circle' and p.pad_width_mm == 1.6 for p in host.pins)
    assert physical == before
    assert window._last_mesh_path == mesh
    assert window._model_revision == revision
    assert not window.chk_header_pins.isChecked(), 'mounting must not toggle the 3D pins'
    state = window._project_state()
    window._restore_mounting_state(None)
    assert window.combo_mounting.currentData() == 'direct'
    window._apply_project_state(state)
    assert window.combo_mounting.currentData() == 'headers'
    assert window.spin_header_pad.value() == 1.6
    assert window.spin_header_drill.value() == .9
    assert window._current_castellated_params() == before


def test_back_from_documents_saves_and_returns_to_start_page(window):
    from kicadgen.gui.project_state import read_project_config
    name, folder = window._component_name(), window._project_dir()
    window.spin_header_drill.setValue(.85)
    wizard = window.project_wizard
    assert wizard.stack.count() == 2
    wizard.set_page(0)
    assert wizard.back.isEnabled()
    assert window.project_details.content.isHidden()
    wizard.back.click()
    assert not window._component_name()
    assert window.central_stack.currentWidget() is window.start_page
    saved = read_project_config(folder)
    assert saved['host_mounting']['drill'] == .85
    window._switch_project(name, saved)
    assert window.central_stack.currentWidget() is window.tabs
    assert window.spin_header_drill.value() == .85
    assert wizard.stack.currentIndex() == 0


def test_return_to_projects_keeps_project_open_if_save_fails(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    name = window._component_name()
    window._last_saved_project_state = window._project_state_text()
    window.spin_header_drill.setValue(.85)
    monkeypatch.setattr(window, '_flush_project_config', lambda: None)
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))
    window.project_wizard.set_page(0)
    window.project_wizard.back.click()
    assert window._component_name() == name
    assert window.central_stack.currentWidget() is window.tabs
    assert warnings


def test_return_to_projects_respects_running_work_guard(window, monkeypatch):
    name = window._component_name()
    monkeypatch.setattr(window, '_project_change_blocked', lambda: True)
    window.project_wizard.set_page(0)
    window.project_wizard.back.click()
    assert window._component_name() == name
    assert window.central_stack.currentWidget() is window.tabs


def test_legacy_project_does_not_inherit_host_mounting(window):
    select_board(window)
    state = window._project_state()
    state.pop('host_mounting')
    window.combo_mounting.setCurrentIndex(1)
    window.spin_header_pad.setValue(2)
    window._apply_project_state(state)
    assert window.combo_mounting.currentData() == 'direct'
    assert window.spin_header_pad.value() == 1.7


def test_single_component_does_not_apply_stale_board_mounting(window):
    window.combo_mounting.setCurrentIndex(1)
    index = window.combo_component_type.findData('single_tht')
    window.combo_component_type.setCurrentIndex(index)
    physical = module()
    assert window._host_footprint_params(physical) is physical
    assert window.group_mounting.isHidden()
    assert window.preview_mounting_group.isHidden()


def test_preview_mounting_edits_sync_and_preserve_physical_board(window, monkeypatch):
    select_board(window)
    physical = module()
    before = deepcopy(physical)
    monkeypatch.setattr(window, '_current_castellated_params', lambda *_args, **_kwargs: physical)
    revision = window._model_revision
    window.combo_preview_mounting.setCurrentIndex(1)
    window.spin_preview_header_pad.setValue(1.8)
    window.spin_preview_header_drill.setValue(.95)
    host = window._preview_params()
    assert all(p.pad_width_mm == 1.8 and p.drill_mm == .95 for p in host.pins)
    assert window._mounting_state() == {'mode': 'headers', 'pad_diameter': 1.8, 'drill': .95,
                                        'pad_overrides': {}}
    assert physical == before
    assert window._model_revision == revision
    window.spin_header_drill.setValue(.8)
    assert window.spin_preview_header_drill.value() == .8
    window._restore_mounting_state({'mode': 'headers', 'pad_diameter': 1.6, 'drill': .9})
    assert window.spin_preview_header_pad.value() == 1.6
    assert window.spin_preview_header_drill.value() == .9
    window.spin_preview_header_drill.setValue(2)
    assert window.label_preview_mounting_error.text()
    assert not window.label_preview_mounting_error.isHidden()
    window.spin_preview_header_drill.setValue(.9)
    assert window.label_preview_mounting_error.isHidden()


def test_update_only_footprint_keeps_existing_step_and_mesh(window, monkeypatch, tmp_path):
    from kicadgen.gui import main_window as gui
    select_board(window)
    window._acquisition_completed = True
    physical = module()
    physical.name = window._component_name()
    monkeypatch.setattr(window, '_current_castellated_params', lambda *_args, **_kwargs: physical)
    monkeypatch.setattr(window, '_missing_contact_style_message', lambda: '')
    monkeypatch.setattr(gui.GenerateWorker, 'start', lambda worker: worker.run())
    monkeypatch.setattr(gui.GenerateWorker, '_generate_model',
                        lambda worker: pytest.fail('footprint-only update must not rebuild 3D'))
    shapes = window._project_dir() / f'{physical.name}.3dshapes'
    shapes.mkdir(parents=True, exist_ok=True)
    step = shapes / f'{physical.name}.step'
    step.write_bytes(b'existing photo-faithful solid')
    mesh = shapes / f'{physical.name}.preview.json'
    mesh.write_bytes(b'existing mesh')
    window.combo_preview_mounting.setCurrentIndex(1)
    window._refresh_preview_tabs(True)
    window._refresh_mounting_actions()
    assert window.btn_update_host_footprint_preview.isEnabled()
    window.btn_update_host_footprint_preview.click()
    output = window._project_dir() / f'{physical.name}.pretty' / f'{physical.name}.kicad_mod'
    assert 'thru_hole circle' in output.read_text()
    assert step.read_bytes() == b'existing photo-faithful solid'
    assert mesh.read_bytes() == b'existing mesh'
    assert window._worker.body is None
    assert window._worker.params == physical
    window.combo_mounting.setCurrentIndex(0)
    window._generate(footprint_only=True)
    assert 'thru_hole oval' in output.read_text()
    assert step.read_bytes() == b'existing photo-faithful solid'
