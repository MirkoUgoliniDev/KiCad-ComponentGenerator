from copy import deepcopy
from PyQt6.QtWidgets import QFileDialog
from kicadgen.gui.main_window import TAB_SYMBOL, TAB_PREVIEW, TAB_MODEL_3D
from kicadgen.gui.project_state import read_project_config
from tests.test_host_mounting import module


def setup_board(window, monkeypatch):
    monkeypatch.setattr(window.symbol_tab, "confirm_export_review", lambda: True)
    params = module()
    monkeypatch.setattr(window, '_is_castellated', lambda: True)
    monkeypatch.setattr(window, '_current_castellated_params', lambda: params)
    window.pinout = [{'pin': n, 'name': 'GND' if n == 1 else f'GPIO{n}'} for n in range(1, 8)]
    window._open_project_folder = window._project_dir()
    window._restoring_project = False
    # An extraction happened in this session: the preview tabs stay open
    # through `_refresh_output_actions` (which the export now calls) even
    # before a footprint file exists.
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window._refresh_symbol_tab()
    return params


def test_symbol_tab_saves_pinout_types_and_exports_matching_ids(window, monkeypatch, tmp_path):
    setup_board(window, monkeypatch)
    assert TAB_PREVIEW < TAB_SYMBOL < TAB_MODEL_3D
    tab = window.symbol_tab
    before = deepcopy(window.pinout)
    tab.table.item(0, 1).setText('VCC')
    types = tab.table.cellWidget(0, 2)
    types.setCurrentIndex(types.findData('power_in'))
    assert window.pinout == before
    tab.apply()
    assert window.pinout[0]['name'] == 'VCC'
    assert read_project_config(window._project_dir())['pinout'][0]['electrical_type'] == 'power_in'
    target = tmp_path / 'test.kicad_sym'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    window._export_electrical_symbol()
    text = target.read_text()
    assert '(pin power_in line' in text and '(name "VCC"' in text
    assert all(f'(number "{number}"' in text for number in range(1, 8))
    assert tab.export_button.isEnabled()
    tab.table.item(0, 1).setText('Draft after apply')
    window._refresh_symbol_tab()
    assert tab.table.item(0, 1).text() == 'Draft after apply'


def test_save_failure_restores_pinout_and_export_rechecks_physical_mapping(window, monkeypatch, tmp_path):
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, '_can_leave_project', lambda *a, **kw: True)
    before = deepcopy(window.pinout)
    monkeypatch.setattr(window, '_save_project_config', lambda **kw: False)
    window.symbol_tab.table.item(0, 1).setText('Changed')
    window.symbol_tab.apply()
    assert window.pinout == before
    assert window.symbol_tab.rows()[0]['name'] == 'GND'
    assert window.symbol_tab.draft_rows()[0]['name'] == 'Changed'
    assert window.symbol_tab.has_pending_edits()
    assert 'non salvato' in window.symbol_tab.status.text()
    window.symbol_tab.cancel_button.click()
    monkeypatch.setattr(window, '_symbol_pad_ids', lambda: ['unexpected'])
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (_ for _ in ()).throw(AssertionError('invalid export')))
    window._export_electrical_symbol()
    assert 'corrispondere' in window.symbol_tab.status.text()


def test_symbol_identity_uses_electrical_pads_excluding_mechanical_holes(window):
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    window.package_geometry = PackageGeometry(name='Board', family='module',
        body_length_mm=10, body_width_mm=10, pins=[
            PinSpec(number='A2', x_mm=-2, y_mm=0, pad_length_mm=1,
                    pad_width_mm=1, pad_type='smd', shape='rect'),
            PinSpec(number='A2', x_mm=2, y_mm=0, pad_length_mm=1,
                    pad_width_mm=1, pad_type='smd', shape='rect'),
            PinSpec(number='', x_mm=0, y_mm=2, pad_length_mm=1,
                    pad_width_mm=1, drill_mm=1, pad_type='np_thru_hole', shape='circle')])
    assert window._symbol_pad_ids() == ['A2']


def test_custom_layout_persists_and_export_uses_applied_geometry(window, monkeypatch, tmp_path):
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    assert tab.place_pin('1', 50.8, 25.4)
    assert tab.rotate_pin('1', -90)
    assert tab.resize_body(60.96, 40.64)
    tab.apply()
    persisted = read_project_config(window._project_dir())['pinout']
    assert persisted[0]['symbol_body'] == [60.96, 40.64]
    assert persisted[0]['symbol_position'] == [50.8, 25.4]
    assert persisted[0]['symbol_angle'] == 270
    target = tmp_path / 'custom.kicad_sym'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    window._export_electrical_symbol()
    text = target.read_text()
    assert '(start -30.48 20.32) (end 30.48 -20.32)' in text
    assert '(at 50.8 25.4 270)' in text
    tab.set_data('Reloaded', persisted, force=True)
    assert tab._symbol.pins[0].angle == 270
    assert (tab._symbol.width, tab._symbol.height) == (60.96, 40.64)


def test_failed_geometry_save_keeps_retryable_draft_and_history(window, monkeypatch):
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    baseline = tab.rows()
    assert tab.place_pin('1', 50.8, 25.4)
    assert tab.rotate_pin('1', 90)
    draft = tab.draft_rows()
    save = window._save_project_config
    monkeypatch.setattr(window, '_save_project_config', lambda **kw: False)
    tab.apply()
    assert tab.rows() == baseline
    assert tab.draft_rows() == draft
    assert tab.apply_button.isEnabled()
    assert not tab.export_button.isEnabled()
    tab.undo()
    assert tab.draft_rows() != draft
    tab.redo()
    assert tab.draft_rows() == draft
    monkeypatch.setattr(window, '_save_project_config', save)
    tab.apply()
    assert not tab.has_pending_edits()
    assert read_project_config(window._project_dir())['pinout'] == draft


def test_export_rejects_unapplied_geometry_even_when_called_directly(window, monkeypatch):
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    assert tab.place_pin('1', 50.8, 25.4)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (
        _ for _ in ()).throw(AssertionError('must apply before exporting')))
    window._export_electrical_symbol()
    assert 'non applicate' in tab.status.text()


def test_saved_geometry_can_be_undone_and_saved_again(window, monkeypatch):
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    initial = tab.draft_rows()
    assert tab.place_pin('1', 50.8, 25.4)
    edited = tab.draft_rows()
    tab.apply()
    assert not tab.has_pending_edits()
    assert tab.undo_button.isEnabled()
    window._refresh_symbol_tab()
    assert tab.undo_button.isEnabled()
    tab.undo()
    assert tab.draft_rows() == initial
    assert tab.has_pending_edits()
    assert not tab.export_button.isEnabled()
    assert window.pinout == edited
    tab.apply()
    assert window.pinout == initial
    assert read_project_config(window._project_dir())['pinout'] == initial
    assert tab.export_button.isEnabled()
    tab.redo()
    assert tab.draft_rows() == edited
    assert tab.has_pending_edits()
