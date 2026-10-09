from copy import deepcopy
import json
import pytest
from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QDialogButtonBox
from kicadgen.gui.symbol_tab import SymbolTab


def board():
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data('Board', [{'pin': str(i), 'name': f'PIN{i}'} for i in range(1, 7)])
    return tab


def positions(tab):
    return {p.pin.number: (p.x, p.y, p.angle) for p in tab._symbol.pins}


def test_ctrl_and_rectangle_selection_do_not_edit(qapp):
    tab = board(); tab.show(); qapp.processEvents()
    before = tab.draft_rows()
    for number in ('1', '2'):
        p = next(p for p in tab._symbol.pins if p.pin.number == number)
        point = tab.view.mapFromScene(QPointF(p.x*10, -p.y*10))
        QTest.mouseClick(tab.view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, point)
    assert tab._selection_ids() == {'1', '2'}
    tab._select_pins(set())
    pins = tab._symbol.pins[:3]
    x = pins[0].x * 10
    top, bottom = -pins[0].y*10-8, -pins[-1].y*10+8
    start = tab.view.mapFromScene(QPointF(x-8, top))
    end = tab.view.mapFromScene(QPointF(x+8, bottom))
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, start)
    QTest.mouseMove(tab.view.viewport(), end)
    QTest.mouseRelease(tab.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert tab._selection_ids() == {'1', '2', '3'}
    assert tab.draft_rows() == before
    tab.close(); tab.deleteLater()


def test_group_transform_collision_undo_rotation(qapp):
    tab = board()
    tab._select_pins({'1','2','3'}, '1')
    old = positions(tab)
    assert tab._transform_selected('move', (25.4, 5.08))
    new = positions(tab)
    for n in ('1','2','3'):
        assert new[n][:2] == pytest.approx((old[n][0]+25.4, old[n][1]+5.08))
    for n in ('4','5','6'):
        assert new[n] == old[n]
    tab.undo(); assert positions(tab) == old
    assert tab._transform_selected('rotate', (90,))
    assert positions(tab)['1'][:2] == old['1'][:2]
    assert all(positions(tab)[n][2] == 90 for n in ('1','2','3'))
    tab.undo()
    before = tab.draft_rows()
    # Move the left column onto the right: the entire batch is rejected.
    dx = old['4'][0]-old['1'][0]
    assert not tab._transform_selected('move', (dx, 0))
    assert tab.draft_rows() == before
    tab.deleteLater()


def test_alignment_distribution_grid_and_lock(qapp):
    tab = board()
    for number, x, y in [('1',25.4,25.4), ('2',30.48,40.64), ('3',35.56,60.96)]:
        assert tab.set_pin_geometry(number,x,y,0)
    tab._select_pins({'1','2','3'}, '2')
    assert tab._transform_selected('align_x')
    assert {positions(tab)[n][0] for n in ('1','2','3')} == {30.48}
    assert tab._transform_selected('distribute_y')
    ys = sorted(positions(tab)[n][1] for n in ('1','2','3'))
    assert ys[1]-ys[0] == pytest.approx(ys[2]-ys[1])
    assert all(y/2.54 == pytest.approx(round(y/2.54)) for y in ys)
    before = tab.draft_rows()
    tab.lock_button.defaultAction().setChecked(True)
    assert not tab.resize_body(60.96,60.96)
    assert not any(item.data(0) and item.data(0)[0] == 'resize' for item in tab.view.scene().items())
    assert tab.draft_rows() == before
    tab.lock_button.defaultAction().setChecked(False)
    assert tab.resize_body(60.96,60.96)
    tab.deleteLater()


def test_review_missing_roles_overlap_and_cancel_export(qapp, monkeypatch):
    tab = board()
    tab.table.item(0,1).setText('')
    assert any('Nome mancante' in issue for issue in tab.review_issues())
    assert any('Tipo elettrico' in issue for issue in tab.review_issues())
    tab.table.item(0,1).setText('LONG_LABEL_FOR_OVERLAP')
    tab.table.item(1,1).setText('OTHER_LONG_LABEL')
    tab.set_pin_geometry('1',50.8,25.4,0)
    tab.set_pin_geometry('2',55.88,25.4,0)
    assert any('Testi sovrapposti' in issue for issue in tab.review_issues())
    monkeypatch.setattr(QDialog, 'exec', lambda self: QDialog.DialogCode.Rejected)
    assert not tab.confirm_export_review()
    monkeypatch.setattr(QDialog, 'exec', lambda self: QDialog.DialogCode.Accepted)
    assert tab.confirm_export_review()
    tab.deleteLater()


def test_recovery_survives_restart_and_cancel_clears(qapp, tmp_path):
    path = tmp_path / '.symbol-draft.json'
    original = board()
    original.set_recovery_path(path)
    assert original.place_pin('1',50.8,25.4)
    assert path.exists()
    expected = original.draft_rows()
    restarted = board(); restarted.set_recovery_path(path)
    assert restarted.recovery_button.isEnabled()
    assert not restarted.has_pending_edits()
    restarted.recover_draft()
    assert restarted.draft_rows() == expected
    restarted.cancel_button.click()
    assert not path.exists()
    original.deleteLater(); restarted.deleteLater()


def test_recovery_rejects_changed_baseline_and_corrupt_data(qapp, tmp_path):
    from kicadgen.services.symbol_draft import save_draft, load_draft
    tab = board(); baseline = tab.rows()
    path = tmp_path / 'draft.json'
    changed = deepcopy(baseline); changed[0]['name'] = 'NEW'
    save_draft(path, baseline, changed)
    assert load_draft(path, changed) is None
    path.write_text('{bad')
    assert load_draft(path, baseline) is None
    tab.deleteLater()


def test_recovery_atomic_failure_retains_previous_draft(qapp, tmp_path, monkeypatch):
    import kicadgen.services.symbol_draft as store
    tab = board(); path=tmp_path/'draft.json'; tab.set_recovery_path(path)
    tab.place_pin('1',50.8,25.4)
    saved=path.read_text()
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(store,'replace_file',fail)
    assert tab.place_pin('1',55.88,25.4)
    assert path.read_text() == saved
    assert 'Bozza non salvata' in tab.status.text()
    assert not list(tmp_path.glob('*.tmp'))
    tab.deleteLater()


def test_group_mouse_drag_is_one_undo_step(qapp):
    tab = board(); tab.show(); qapp.processEvents()
    tab._select_pins({'1','2'}, '1')
    old = positions(tab)
    p = tab._symbol.pins[0]
    start = tab.view.mapFromScene(QPointF(p.x*10,-p.y*10))
    end = tab.view.mapFromScene(QPointF((p.x-25.4)*10,-p.y*10))
    QTest.mousePress(tab.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(tab.view.viewport(), end)
    QTest.mouseRelease(tab.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert positions(tab)['1'][0] == pytest.approx(old['1'][0]-25.4)
    assert positions(tab)['2'][0] == pytest.approx(old['2'][0]-25.4)
    assert positions(tab)['3'] == old['3']
    assert len(tab._history) == 2
    tab.undo(); assert positions(tab) == old
    tab.close(); tab.deleteLater()


def test_export_review_cancel_never_opens_file_dialog(window, monkeypatch):
    from tests.test_symbol_integration import setup_board
    from PyQt6.QtWidgets import QFileDialog
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window.symbol_tab, 'confirm_export_review', lambda: False)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: pytest.fail('cancelled review must stop export'))
    window._export_electrical_symbol()


def test_recovery_file_tracks_failed_save_then_retry(window, monkeypatch):
    from tests.test_symbol_integration import setup_board
    setup_board(window, monkeypatch)
    tab = window.symbol_tab
    tab.place_pin('1',50.8,25.4)
    path = window._project_dir()/'.symbol-draft.json'
    assert path.exists()
    save = window._save_project_config
    monkeypatch.setattr(window,'_save_project_config',lambda **kw:False)
    tab.apply()
    assert tab.has_pending_edits() and path.exists()
    monkeypatch.setattr(window,'_save_project_config',save)
    tab.apply()
    assert not tab.has_pending_edits() and not path.exists()
