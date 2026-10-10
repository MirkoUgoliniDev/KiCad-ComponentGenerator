"""@brief config.json is written atomically and a failed save never escapes a Qt slot.

Finding G1: `save_config` used to truncate config.json in place and raise
OSError straight out of Qt slots, where the crash hook ends the process. Each
rule has a positive case and a negative case that differ structurally (the
write succeeds or the file system refuses it), not by a tuned number.
"""
import json
import sqlite3
import sys

import pytest

from kicadgen.gui import app_config
from kicadgen.services.analysis_profiles import ProfileStore, factory_defaults


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """@brief Point config.json at a private file holding a known previous content."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"show_grid": True, "grid_unit": "mm"}), encoding="utf-8")
    monkeypatch.setenv("KICADGEN_CONFIG_PATH", str(path))
    return path


def _refuse(*_args, **_kwargs):
    raise OSError(28, "No space left on device")


def test_normal_save_replaces_the_file(config_file):
    """@brief Positive: a successful save returns True and the file holds the new value."""
    assert app_config.save_config({"show_grid": False, "grid_unit": "inch"}) is True
    assert json.loads(config_file.read_text(encoding="utf-8")) == {"show_grid": False, "grid_unit": "inch"}
    assert sorted(p.name for p in config_file.parent.iterdir()) == ["config.json"]


def test_failed_replace_leaves_the_previous_file_intact(config_file, monkeypatch):
    """@brief Negative: when the final move is refused the old file survives whole, no temp is left."""
    before = config_file.read_bytes()
    monkeypatch.setattr("kicadgen.fsutil.replace_file", _refuse)
    assert app_config.save_config({"show_grid": False}) is False
    assert config_file.read_bytes() == before
    assert sorted(p.name for p in config_file.parent.iterdir()) == ["config.json"]


def test_failed_write_midway_never_truncates(config_file, monkeypatch):
    """@brief Negative: a write that dies after the file is opened does not touch config.json."""
    before = config_file.read_bytes()
    real_fdopen = app_config.os.fdopen

    class _DiesMidway:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def write(self, text):
            self._handle.write(text[: len(text) // 2])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(app_config.os, "fdopen",
                        lambda fd, *a, **k: _DiesMidway(real_fdopen(fd, *a, **k)))
    assert app_config.save_config({"show_grid": False}) is False
    assert config_file.read_bytes() == before
    assert sorted(p.name for p in config_file.parent.iterdir()) == ["config.json"]


def test_unserialisable_value_is_refused_before_touching_the_disk(config_file):
    """@brief Negative: a value JSON cannot encode returns False and the file is unchanged."""
    before = config_file.read_bytes()
    assert app_config.save_config({"show_grid": object()}) is False
    assert config_file.read_bytes() == before


# ── Qt slot: a failed save costs one log line, never the process ─────────

from test_photo_inventory_gui import gui  # noqa: E402


def _hook_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: calls.append(exc))
    return calls


def test_grid_toggle_with_unwritable_config_logs_and_does_not_crash(window, config_file, monkeypatch):  # noqa: F811
    """@brief Positive: the save fails inside the slot; the crash hook is never reached, one line is logged."""
    monkeypatch.setattr(gui, "save_config", app_config.save_config)
    monkeypatch.setattr("kicadgen.fsutil.replace_file", _refuse)
    hooked = _hook_recorder(monkeypatch)
    before = config_file.read_bytes()
    lines_before = len(window._log_lines)
    window.chk_grid.setChecked(not window.chk_grid.isChecked())
    assert hooked == []
    assert config_file.read_bytes() == before
    new_lines = window._log_lines[lines_before:]
    assert len(new_lines) == 1 and str(config_file) in new_lines[0]


def test_grid_toggle_with_writable_config_saves_silently(window, config_file, monkeypatch):  # noqa: F811
    """@brief Negative: the same slot with a writable file saves the choice and logs nothing."""
    monkeypatch.setattr(gui, "save_config", app_config.save_config)
    hooked = _hook_recorder(monkeypatch)
    lines_before = len(window._log_lines)
    target = not window.chk_grid.isChecked()
    window.chk_grid.setChecked(target)
    assert hooked == []
    assert json.loads(config_file.read_text(encoding="utf-8"))["show_grid"] is target
    assert window._log_lines[lines_before:] == []


def test_open_3dshapes_dir_reports_a_refused_folder(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positive: mkdir refused -> logged, no browser, no exception out of the slot."""
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: opened.append(url))
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed")
    with monkeypatch.context() as operation:
        operation.setattr(window, "_project_dir", lambda: blocker / "Project")
        lines_before = len(window._log_lines)
        window._open_3dshapes_dir()
    assert opened == []
    # The component log lives in the same refused folder, so it may add its own line.
    assert any(".3dshapes" in line for line in window._log_lines[lines_before:])


def test_open_3dshapes_dir_opens_a_creatable_folder(window, monkeypatch):  # noqa: F811
    """@brief Negative: a creatable folder is created and opened without log lines."""
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: opened.append(url))
    with monkeypatch.context() as operation:
        operation.setattr(window, "_project_dir", lambda: window._library_root() / "Shapes")
        operation.setattr(window, "_component_name", lambda: "Part")
        lines_before = len(window._log_lines)
        window._open_3dshapes_dir()
    assert len(opened) == 1 and (window._library_root() / "Shapes" / "Part.3dshapes").is_dir()
    assert window._log_lines[lines_before:] == []


# ── First start: a corrupt legacy config.json must not stop ProfileStore ──

@pytest.mark.parametrize("content", [b'{"system_prompt": "trunc', b'[1, 2]', b'\xff\xfe\x00garbage'])
def test_corrupt_legacy_config_at_first_start_does_not_crash(tmp_path, content):
    """@brief Positive: truncated, non-object or undecodable legacy JSON -> factory defaults, bytes backed up."""
    source = tmp_path / "config.json"
    source.write_bytes(content)
    store = ProfileStore(tmp_path / "ai.sqlite3", legacy_path=source)
    defaults = factory_defaults()
    assert (store.defaults()["model"], store.defaults()["system_prompt"]) == (
        defaults["model"], defaults["system_prompt"])
    assert source.read_bytes() == content
    assert next(tmp_path.glob("config-before-sqlite-*.json")).read_bytes() == content
    with sqlite3.connect(store.path) as db:
        metadata = dict(db.execute("SELECT name,value FROM metadata"))
    assert metadata["legacy_import_skipped"].startswith("corrupt")


def test_valid_legacy_config_is_still_imported(tmp_path):
    """@brief Negative: a well-formed legacy file is imported and no skip is recorded."""
    source = tmp_path / "config.json"
    source.write_text(json.dumps({"model": "vendor/custom"}), encoding="utf-8")
    store = ProfileStore(tmp_path / "ai.sqlite3", legacy_path=source)
    assert store.defaults()["model"] == "vendor/custom"
    with sqlite3.connect(store.path) as db:
        metadata = dict(db.execute("SELECT name,value FROM metadata"))
    assert "legacy_import_skipped" not in metadata


def test_a_value_that_cannot_be_encoded_is_refused_without_raising(tmp_path, monkeypatch):
    """Revisione del blocco A (131): un surrogato isolato (nome di file Windows non
    valido in UTF-16) faceva sollevare UnicodeEncodeError in uno slot."""
    from kicadgen.gui import app_config
    monkeypatch.setenv("KICADGEN_CONFIG_PATH", str(tmp_path / "config.json"))
    assert app_config.save_config({"last_document_dir": "C:/ok"}) is True
    assert app_config.save_config({"last_document_dir": "C:/\udc80"}) is False
    assert "C:/ok" in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]
