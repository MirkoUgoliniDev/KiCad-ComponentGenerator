"""@brief replace_file: il rifiuto transitorio di Windows si ritenta, gli altri errori no."""
import os

import pytest

from kicadgen import fsutil


def _flaky(monkeypatch, failures, error=PermissionError):
    calls = []
    real = os.replace

    def replace(source, target):
        calls.append(1)
        if len(calls) <= failures:
            raise error("simulated")
        real(source, target)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    return calls


def test_a_transient_refusal_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    source, target = tmp_path / "a.tmp", tmp_path / "a.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    calls = _flaky(monkeypatch, 2)
    pauses = []
    fsutil.replace_file(source, target, sleep=pauses.append)
    assert target.read_text(encoding="utf-8") == "new" and len(calls) == 3
    assert pauses == [0.05, 0.1]


def test_a_refusal_that_does_not_heal_is_raised_after_the_attempts(tmp_path, monkeypatch):
    source = tmp_path / "a.tmp"
    source.write_text("new", encoding="utf-8")
    calls = _flaky(monkeypatch, 99)
    with pytest.raises(PermissionError):
        fsutil.replace_file(source, tmp_path / "a.json", attempts=3, sleep=lambda s: None)
    assert len(calls) == 3


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    calls = _flaky(monkeypatch, 99, error=FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        fsutil.replace_file(tmp_path / "missing", tmp_path / "a.json", sleep=lambda s: None)
    assert len(calls) == 1


def test_document_roles_survive_a_transient_refusal(tmp_path, monkeypatch):
    """@brief Il caso visto nella suite: .document-roles.json rifiutato per un istante."""
    from kicadgen.services.document_roles import DocumentSet
    monkeypatch.setattr(fsutil, "REPLACE_FIRST_PAUSE_S", 0.0)
    store = DocumentSet(tmp_path / "docs")
    store.clear("pinout", unavailable=True)
    calls = _flaky(monkeypatch, 1)
    store.clear("hires", unavailable=True)
    assert len(calls) == 2
    assert DocumentSet(tmp_path / "docs").selected is not None
