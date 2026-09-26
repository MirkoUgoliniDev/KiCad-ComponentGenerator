"""@brief Test the dependency-free project launcher without starting a GUI."""
import importlib.util
from pathlib import Path
import pytest


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location('project_launcher', Path(__file__).resolve().parents[1] / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_environment_keeps_current_python(launcher, tmp_path):
    assert launcher._project_python(tmp_path) is None


def test_already_in_project_environment_never_relaunches(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.sys, 'prefix', str(tmp_path / '.venv'))
    assert launcher._project_python(tmp_path) is None


def test_local_python_selected_even_when_base_executable_is_global(launcher, tmp_path, monkeypatch):
    executable = tmp_path / '.venv' / ('Scripts/python.exe' if launcher.os.name == 'nt' else 'bin/python')
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(launcher.sys, 'prefix', str(tmp_path / 'global'))
    assert launcher._project_python(tmp_path) == executable


def test_relaunch_preserves_arguments_and_exit_status(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr(launcher, '_project_python', lambda root: tmp_path / 'Python with spaces.exe')
    monkeypatch.setattr(launcher.sys, 'argv', ['main.py', '--check-runtime', 'argument with spaces'])
    monkeypatch.delenv('KICADGEN_BOOTSTRAP_ROOT', raising=False)
    seen = {}
    def child(command, **kwargs):
        seen.update(command=command, **kwargs)
        return 7
    monkeypatch.setattr(launcher.subprocess, 'call', child)
    assert launcher.main() == 7
    assert seen['command'][2:] == ['--check-runtime', 'argument with spaces']
    assert seen['env']['KICADGEN_BOOTSTRAP_ROOT'] == str(tmp_path)
    assert 'KICADGEN_BOOTSTRAP_ROOT' not in launcher.os.environ


def test_broken_environment_cannot_relaunch_forever(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr(launcher, '_project_python', lambda root: tmp_path / 'python.exe')
    monkeypatch.setenv('KICADGEN_BOOTSTRAP_ROOT', str(tmp_path))
    monkeypatch.setattr(launcher.subprocess, 'call', lambda *a, **k: pytest.fail('loop'))
    assert launcher.main() == 1


def test_packaged_application_does_not_use_neighbouring_venv(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.sys, 'frozen', True, raising=False)
    assert launcher._project_python(tmp_path) is None


@pytest.fixture
def crash_hooks(launcher, monkeypatch, tmp_path):
    """Install the launcher's crash hooks into a throwaway data folder; always removed."""
    import sys
    import threading
    from kicadgen import crash_report
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    # Gancio precedente muto: quello di pytest trasformerebbe il thread fallito
    # apposta in un avviso.
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    exits = []
    hook = launcher.install_crash_reports(exit_process=exits.append)
    yield hook, exits, tmp_path / "logs" / "crash-reports"
    crash_report.uninstall()


def _raise_and_catch(exception):
    import sys
    try:
        raise exception
    except BaseException:
        return sys.exc_info()


def test_an_unhandled_exception_is_reported_then_the_program_stops(crash_hooks, capsys):
    """@brief Come prima del 26/09 il programma si ferma, ma ora lascia il rapporto."""
    hook, exits, reports = crash_hooks
    import sys
    assert sys.excepthook is hook
    hook(*_raise_and_catch(RuntimeError("guasto di prova")))
    assert exits == [1]
    files = list(reports.glob("crash-*.log"))
    assert len(files) == 1 and "guasto di prova" in files[0].read_text(encoding="utf-8")


def test_a_worker_thread_exception_is_reported_without_stopping_the_program(crash_hooks):
    """@brief Negativo: un'eccezione in un thread di lavoro chiudeva solo quel thread; resta cosi'."""
    import threading
    _hook, exits, reports = crash_hooks
    worker = threading.Thread(target=lambda: 1 / 0, name="lavoro-di-prova")
    worker.start()
    worker.join()
    assert exits == []
    assert len(list(reports.glob("crash-*.log"))) == 1
