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
