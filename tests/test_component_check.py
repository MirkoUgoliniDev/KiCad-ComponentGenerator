from pathlib import Path
import subprocess
import pytest
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts


def test_missing_files_and_cli_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(tmp_path, 'Demo', [{'pin': '1', 'name': 'GPIO1'}], pending=True)
    assert any(r.status == 'ERROR' and r.title == 'Footprint su disco' for r in results)
    # CONTRACT CHANGED 2026-11-12 (delivery without STEP): a missing STEP is a
    # warning, not an ERROR; a truncated STEP stays an ERROR (test below).
    assert any(r.status == 'WARN' and r.title == 'Modello STEP' for r in results)
    assert not any(r.status == 'ERROR' and r.title == 'Modello STEP' for r in results)
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
    monkeypatch.setattr(check, '_run_cli', timeout)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    # CONTRACT CHANGED 2026-11-15 (guided KiCad tab, review H1): a kicad-cli
    # that times out is a failure of the checking TOOL, not of the component:
    # the three file reads are «not run» (SKIP, with a remedy), never ERRORs --
    # an ERROR forbids the installation and a slow PC would block it for good.
    # CONTRACT CHANGED 2026-11-14 (automatic DRC/ERC): the DRC and the ERC SKIP too.
    assert sum(r.status == 'ERROR' for r in results) == 0
    reads = [r for r in results if r.status == 'SKIP' and '60 secondi' in r.detail
             and r.code not in ('drc', 'erc')]
    assert len(reads) == 3 and all(r.remedy for r in reads)
    skipped = {r.code: r.detail for r in results if r.status == 'SKIP'}
    assert skipped['drc'].startswith('DRC non eseguito') and skipped['erc'].startswith('ERC non eseguito')
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
    from kicadgen.gui.component_check_dialog import ComponentCheckPanel
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    # CONTRACT CHANGED 2026-11-15: the check is a panel of the KiCad tab.
    dialog = ComponentCheckPanel()
    dialog.run(dict(directory=tmp_path, name='Demo', rows=[]))
    loop = QEventLoop()
    dialog.worker.finished.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    dialog.show()
    loop.exec()
    assert not dialog.worker.isRunning()
    assert dialog.rerun_button.isEnabled() and not dialog.is_running()
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
    monkeypatch.setattr(check, '_run_cli', fake_run)
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


def test_a_footprint_without_step_is_warned_never_refused(tmp_path, monkeypatch):
    """@brief Positive: no STEP -> WARN «Modello STEP», 3D import skipped, no ERROR from the model."""
    art = _artifacts(tmp_path)
    art.step_path.unlink()
    art.wrl_path.unlink()
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.status == 'WARN' and r.title == 'Modello STEP' and 'senza vista 3D' in r.detail
               for r in results)
    assert any(r.status == 'SKIP' and r.title == 'Importazione 3D in KiCad' for r in results)
    assert not any(r.status == 'ERROR' for r in results), [r for r in results if r.status == 'ERROR']
    assert not any(r.title == 'Collegamento 3D originale' for r in results)


def test_a_truncated_step_is_still_an_error(tmp_path, monkeypatch):
    """@brief Negative: a STEP that exists but lacks its trailer stays an ERROR («NON USARE»)."""
    art = _artifacts(tmp_path)
    art.step_path.write_bytes(b"ISO-10303-21;\nHEADER;\n")
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert any(r.status == 'ERROR' and r.title == 'Modello STEP' for r in results)



def test_a_stop_kills_the_running_kicad_cli_and_ends_the_check():
    """Review 2026-11-15 M3: a stop (window closed, project switched) kills the command in flight.

    Negative twin: a command nobody stops returns its output as subprocess.run would.
    """
    import sys
    import threading
    import time
    done = check._run_cli([sys.executable, '-c', 'print("ok")'], timeout=30)
    assert done.returncode == 0 and done.stdout.strip() == 'ok'
    outcome = {}

    def slow():
        started = time.monotonic()
        try:
            check._run_cli([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=60)
            outcome['result'] = 'finished'
        except check.CliStopped:
            outcome['result'] = 'stopped'
        outcome['seconds'] = time.monotonic() - started
    thread = threading.Thread(target=slow)
    thread.start()
    deadline = time.monotonic() + 10
    while not check._CLI_PROCESSES and time.monotonic() < deadline:
        time.sleep(0.02)
    assert check.stop_cli_commands() == 1
    thread.join(15)
    assert outcome == {'result': 'stopped', 'seconds': outcome['seconds']} and outcome['seconds'] < 10
    assert not check._CLI_PROCESSES and not check._CLI_STOPPED
