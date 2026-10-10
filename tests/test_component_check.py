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


def test_applied_pin_review_is_checked_even_with_pending_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(tmp_path, 'Demo',
        [{'pin': '1', 'name': '', 'electrical_type': 'power_in', 'electrical_type_reviewed': False}],
        pending=True)
    assert any(r.detail == 'Nome mancante: 1' and r.status == 'WARN' for r in results)
    assert any(r.detail == 'Tipo elettrico da verificare: 1' and r.status == 'WARN' for r in results)


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


@pytest.mark.parametrize('replacement', [
    '(size -1 1)', '(size nan 1)', '(size 1)',
])
def test_bad_pad_dimensions_rejected_without_cli(tmp_path, monkeypatch, replacement):
    art = _artifacts(tmp_path)
    art.footprint_path.write_text(art.footprint_path.read_text().replace('(size 1 1)', replacement))
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.title == 'Footprint su disco' and r.status == 'ERROR' for r in results)
    assert not any(r.title == 'Corrispondenza pin-pad' and r.status == 'OK' for r in results)


def test_missing_drill_rejected(tmp_path, monkeypatch):
    art = _artifacts(tmp_path)
    art.footprint_path.write_text(art.footprint_path.read_text().replace('smd rect', 'thru_hole circle'))
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.title == 'Footprint su disco' and r.status == 'ERROR' for r in results)


@pytest.mark.parametrize('trailing', ['(footprint "Other")', 'corrupted data'])
def test_multiple_roots_and_trailing_junk_rejected(tmp_path, monkeypatch, trailing):
    art = _artifacts(tmp_path)
    art.footprint_path.write_text(art.footprint_path.read_text() + trailing)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.title == 'Footprint su disco' and r.status == 'ERROR' for r in results)


def test_original_missing_model_reference_not_hidden_by_preview(tmp_path, monkeypatch):
    from kicadgen.services.kicad_library import attach_3d_model
    art = _artifacts(tmp_path)
    art.footprint_path.write_text(attach_3d_model(art.footprint_path.read_text(), (tmp_path / 'missing.step').as_posix()))
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.title == 'Collegamento 3D originale' and r.status == 'ERROR' for r in results)


@pytest.mark.parametrize('message', ['Error: failed to load model', 'Errore nel creare il file svg',
                                   'Fallita creazione file', 'Fehler beim Laden', 'Erreur de lecture',
                                   '** Error writing STEP file. **'])
def test_exit_zero_with_error_log_is_not_success(tmp_path, monkeypatch, message):
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'fake')
    def fake_run(args, **kwargs):
        output = Path(args[args.index('-o') + 1])
        if output.suffix == '.step':
            output.write_text('MANIFOLD_SOLID_BREP()')
        else:
            output.mkdir(exist_ok=True, parents=True)
            (output / 'Demo.svg').write_text('<svg/>')
        return subprocess.CompletedProcess(args, 0, message, '')
    monkeypatch.setattr(check.subprocess, 'run', fake_run)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.title == 'Importazione 3D in KiCad' and r.status == 'ERROR' for r in results)


@pytest.mark.parametrize('name', ['../Demo', '..\\Demo', 'C:Demo', ''])
def test_invalid_component_name_is_rejected_before_io(tmp_path, monkeypatch, name):
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: pytest.fail('No external command expected'))
    results = check.verify_component(tmp_path, name, [])
    assert len(results) == 1 and results[0].status == 'ERROR'


@pytest.mark.parametrize('transform', ['(scale (xyz 0 1 1))', '(offset (xyz nan 0 0))', '(rotate (xyz 0 0))'])
def test_original_invalid_model_transform_is_not_hidden_by_import_copy(tmp_path, monkeypatch, transform):
    art = _artifacts(tmp_path)
    text = art.footprint_path.read_text()
    art.footprint_path.write_text(text.rstrip()[:-1] + f'(model "${{KIPRJMOD}}/Demo.step" {transform}))')
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.title == 'Collegamento 3D originale' and r.status == 'ERROR' and 'Trasformazione' in r.detail for r in results)
