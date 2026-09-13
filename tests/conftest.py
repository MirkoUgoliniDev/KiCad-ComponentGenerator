"""Keep configuration and SQLite tests away from the user's live preferences."""
import pytest


@pytest.fixture(autouse=True)
def isolated_profile_paths(monkeypatch, tmp_path):
    monkeypatch.setenv('KICADGEN_PROFILE_DB', str(tmp_path / 'profiles.sqlite3'))
    monkeypatch.setenv('KICADGEN_CONFIG_PATH', str(tmp_path / 'config.json'))
