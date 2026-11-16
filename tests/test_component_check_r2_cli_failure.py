"""@brief The declared cause of a kicad-cli read that delivered nothing (review 2026-11-15, round 2, R5).

The window tells a persistent cause (a kicad-cli that never starts) from a
transient one (a timeout) by `Check.cli_failure`, never by translated text.
Positive: each tool-side failure carries its own cause. Negative: a read
that KiCad completed, or a check skipped because kicad-cli is missing,
carries none.
"""
import subprocess
from types import SimpleNamespace

from kicadgen.services import component_check as check
from tests.test_component_check import _artifacts

READ_CODES = ('kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read')


def _reads(results):
    """{code: cli_failure} of the KiCad read rows."""
    return {r.code: r.cli_failure for r in results if r.code in READ_CODES}


def _verify(tmp_path, monkeypatch, run_cli, cli='fake'):
    """Run the check on a generated demo with a faked kicad-cli."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: cli)
    monkeypatch.setattr(check, '_run_cli', run_cli)
    return check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])


def test_a_kicad_cli_that_cannot_start_is_declared_start(tmp_path, monkeypatch):
    """@brief Positive: an OSError at launch marks every read «start»."""
    def refuse(*args, **kwargs):
        raise OSError('blocked by policy')
    reads = _reads(_verify(tmp_path, monkeypatch, refuse))
    assert reads and set(reads.values()) == {'start'}


def test_a_timeout_is_declared_timeout_not_start(tmp_path, monkeypatch):
    """@brief Negative twin: a timeout is transient, never «start»."""
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired('kicad-cli', 60)
    reads = _reads(_verify(tmp_path, monkeypatch, slow))
    assert reads and set(reads.values()) == {'timeout'}


def test_another_tool_failure_is_declared_not_run(tmp_path, monkeypatch):
    """@brief Positive: a crash code (not a refusal) is «not_run»."""
    reads = _reads(_verify(tmp_path, monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=0xC0000005, stdout='', stderr='')))
    assert reads and set(reads.values()) == {'not_run'}


def test_no_cli_and_ordinary_rows_declare_no_failure(tmp_path, monkeypatch):
    """@brief Negative: kicad-cli missing (needs_cli) and every non-read row carry ''."""
    results = _verify(tmp_path, monkeypatch, lambda *a, **k: None, cli=None)
    assert all(r.cli_failure == '' for r in results)
    assert any(r.needs_cli for r in results)
