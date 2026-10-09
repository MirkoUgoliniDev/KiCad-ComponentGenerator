from pathlib import Path
import subprocess
import pytest
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts


def test_missing_files_and_cli_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(tmp_path, 'Demo', [{'pin': '1', 'name': 'GPIO1'}], pending=True)
    assert any(r.status == 'ERROR' and r.title == 'Footprint su disco' for r in results)
    assert any(r.status == 'ERROR' and r.title == 'Modello 3D' for r in results)
    assert any(r.status == 'SKIP' and 'lettura' in r.title for r in results)
    assert any(r.title == 'Modifiche non applicate' for r in results)


def test_mapping_excludes_npth_and_never_changes_sources(tmp_path, monkeypatch):
    art = _artifacts(tmp_path)
    original = art.footprint_path.read_text()
    art.footprint_path.write_text(original.rstrip()[:-1] +
        '(pad "" np_thru_hole circle (at 4 4) (size 1 1) (drill 1) (layers "*.Cu" "*.Mask")))')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.status == 'OK' and r.title == 'Corrispondenza pin-pad' for r in results)
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '2', 'name': 'GPIO1'}])
    assert any(r.status == 'ERROR' and r.title == 'Corrispondenza pin-pad' for r in results)


def test_timeout_does_not_hide_other_checks(tmp_path, monkeypatch):
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'fake')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('fake', 60)
    monkeypatch.setattr(check.subprocess, 'run', timeout)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert sum(r.status == 'ERROR' for r in results) == 3
    assert results[-1].title == 'Verifiche manuali'


def test_real_kicad_reads_files_and_rejects_broken_step(tmp_path):
    if not check.find_kicad_cli():
        pytest.skip('KiCad CLI not installed')
    art = _artifacts(tmp_path)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    for title in ('Lettura footprint in KiCad', 'Simbolo applicato: lettura in KiCad'):
        assert any(r.title == title and r.status == 'OK' for r in results), results
    assert any(r.status in ('ERROR', 'WARN') and r.title in ('Importazione 3D in KiCad', 'Solidi 3D') for r in results), results


def test_dialog_runs_without_blocking_ui(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QEventLoop, QTimer
    from kicadgen.gui.component_check_dialog import ComponentCheckDialog
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    dialog = ComponentCheckDialog(dict(directory=tmp_path, name='Demo', rows=[]))
    loop = QEventLoop()
    dialog.worker.finished.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    dialog.show()
    loop.exec()
    assert not dialog.worker.isRunning()
    assert dialog.close_button.isEnabled()
    assert 'Footprint' in dialog.report.toPlainText()
    dialog.close()
