"""@brief THT pad/drill spins: no project write per step, nothing lost (2026-10-17).

A held spin arrow wrote the whole project state on every step (~67 ms with a
photo). The value now reaches project_config.json through the autosave like
every other field, and at once when the edit ends.
"""
import json



def _project_file(window):
    return window._project_dir() / "project_config.json"


def _writes(window, monkeypatch):
    calls = []
    real = window._save_project_config

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(window, "_save_project_config", counted)
    return calls


def test_spin_steps_do_not_write_the_project(window, monkeypatch):
    window._save_project_config(force=True)
    calls = _writes(window, monkeypatch)
    spin = window.spin_single_tht_drill
    for _ in range(8):
        spin.stepUp()
    assert calls == []
    assert window.field_drill.value() == spin.value(), "the footprint value still follows each step"


def test_ending_the_edit_writes_it_and_the_autosave_never_loses_it(window, monkeypatch):
    """@brief Negative: the new value reaches the file, at the end of the edit or by the autosave."""
    window._save_project_config(force=True)
    spin = window.spin_single_tht_drill
    spin.setFocus()
    spin.stepUp()
    calls = _writes(window, monkeypatch)
    spin.editingFinished.emit()
    assert len(calls) == 1
    spin.stepUp()
    window._project_autosave.timeout.emit()
    written = _project_file(window).read_text(encoding="utf-8")
    assert window._last_saved_project_state == window._project_state_text()
    assert json.loads(written)["state"] == json.loads(window._project_state_text())
