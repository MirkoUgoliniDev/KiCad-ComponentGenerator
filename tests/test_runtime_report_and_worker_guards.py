"""@brief Review findings K1, K2, K4, K5 (2026-11-03): errors that must reach a signal or a file.

K1: a hostile update answer (a tag longer than Python's int-conversion
limit) raised outside the check's guard and ended the process through
sys.excepthook. K2: worker imports outside `run()`'s guard did the same on an
import failure. K4: `--check-runtime` printed to a windowed executable's
missing console, so a failed build smoke test said nothing. K5: the check
required the optional onnxruntime from the sources.
"""
import importlib.util
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from kicadgen.services import update_check as uc

ROOT = Path(__file__).resolve().parents[1]


def _load_main():
    """Load the launcher module from main.py without running it."""
    spec = importlib.util.spec_from_file_location("kicadgen_main_fg", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(io.BytesIO):
    """Minimal context-manager response, like the object urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# -- K1 -------------------------------------------------------------------------

def test_a_tag_beyond_the_int_conversion_limit_is_an_invalid_response(monkeypatch):
    """@brief Positive: 5000 digits in the tag become "invalid_response", never an exception."""
    tag = "v" + "9" * 5000 + ".0.0"
    body = json.dumps({"tag_name": tag, "html_url": uc.RELEASES_PAGE_URL}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(body))
    result = uc.check_latest_release(current_version="1.0.0")
    assert result.error_kind == uc.ERROR_INVALID_RESPONSE
    assert not result.update_available


def test_an_ordinary_tag_is_still_compared(monkeypatch):
    """@brief Negative: the guard does not swallow a valid answer."""
    body = json.dumps({"tag_name": "v9.0.0", "html_url": uc.RELEASES_PAGE_URL}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(body))
    result = uc.check_latest_release(current_version="1.0.0")
    assert result.ok and result.update_available


def test_the_update_worker_always_emits(qapp, monkeypatch):
    """@brief Positive: an unexpected error in the check still emits one result."""
    from kicadgen.gui import workers

    def broken(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(uc, "check_latest_release", broken)
    worker = workers.UpdateCheckWorker()
    seen = []
    worker.done.connect(seen.append)
    worker.run()
    worker._release()  # not started: drop the exit-time wait by hand
    assert len(seen) == 1 and seen[0].error_kind == uc.ERROR_NETWORK
    assert "boom" in seen[0].error


def test_the_update_worker_forwards_a_normal_result(qapp, monkeypatch):
    """@brief Negative: a normal answer is forwarded unchanged."""
    from kicadgen.gui import workers
    expected = uc.UpdateCheckResult("1.0.0", latest_version="1.0.0")
    monkeypatch.setattr(uc, "check_latest_release", lambda *a, **k: expected)
    worker = workers.UpdateCheckWorker()
    seen = []
    worker.done.connect(seen.append)
    worker.run()
    worker._release()  # not started: drop the exit-time wait by hand
    assert seen == [expected]


# -- K2 -------------------------------------------------------------------------

def _run_collecting_errors(worker):
    """Run the worker synchronously and return what reached `error` and `done`."""
    errors, done = [], []
    worker.error.connect(errors.append)
    worker.done.connect(done.append)
    worker.run()
    return errors, done


def test_terminal_detail_import_failure_reaches_the_error_signal(qapp, monkeypatch):
    """@brief Positive: a module that cannot be imported is reported, not raised."""
    from kicadgen.gui import workers
    monkeypatch.setitem(sys.modules, "kicadgen.services.photo_terminals", None)
    worker = workers.TerminalDetailWorker(b"", [], "model")
    errors, done = _run_collecting_errors(worker)
    assert len(errors) == 1 and not done


def test_reconcile_import_failure_reaches_the_error_signal(qapp, monkeypatch):
    """@brief Positive: same guard in the reconciliation re-run."""
    from kicadgen.gui import workers
    monkeypatch.setitem(sys.modules, "kicadgen.services.photo_inventory", None)
    worker = workers.ReconcileWorker(b"", [], 10, 10, "model", None)
    errors, done = _run_collecting_errors(worker)
    assert len(errors) == 1 and not done


def test_yolo_pretest_import_failure_reaches_the_error_signal(qapp, monkeypatch, tmp_path):
    """@brief Positive: same guard in the YOLO pretest, whose handler needs ReviewCancelled."""
    from kicadgen.gui import workers
    monkeypatch.setitem(sys.modules, "kicadgen.services.photo_inventory", None)
    worker = workers.YoloPretestWorker([], None, 10, 10, None, tmp_path)
    errors, done = _run_collecting_errors(worker)
    assert len(errors) == 1 and not done


def test_terminal_detail_without_import_failure_runs_the_operation(qapp, monkeypatch):
    """@brief Negative: with the modules importable, the operation's result is emitted."""
    from kicadgen.gui import workers
    from kicadgen.services import photo_terminals
    monkeypatch.setattr(photo_terminals, "observe_terminals",
                        lambda *a, **k: ["observed"])
    worker = workers.TerminalDetailWorker(b"", [], "model")
    errors, done = _run_collecting_errors(worker)
    assert done == [["observed"]] and not errors


# -- K4 -------------------------------------------------------------------------

def test_check_runtime_writes_every_line_to_the_report_file(tmp_path, capsys):
    """@brief Positive: a failing check leaves its lines and summary in the file."""
    main = _load_main()
    report = tmp_path / "check.txt"
    result = main.check_runtime(resource_path=lambda *parts: tmp_path.joinpath(*parts),
                                modules=("json", "kicadgen_module_that_does_not_exist"),
                                resources=(("LICENSE",),), qt_plugins=(),
                                report_file=report)
    assert result == 1
    text = report.read_text(encoding="utf-8")
    assert "json: OK" in text
    assert "LICENSE: MISSING" in text
    assert "Runtime check FAILED" in text and "kicadgen_module_that_does_not_exist" in text
    # The console output is unchanged.
    assert "Runtime check FAILED" in capsys.readouterr().err


def test_check_runtime_success_is_written_too(tmp_path):
    """@brief Positive: a passing check writes its OK line."""
    main = _load_main()
    report = tmp_path / "check.txt"
    assert main.check_runtime(resource_path=lambda *parts: tmp_path.joinpath(*parts),
                              modules=("json",), resources=(), qt_plugins=(),
                              report_file=report) == 0
    assert report.read_text(encoding="utf-8").rstrip().endswith("Runtime check OK.")


def test_an_unwritable_report_never_changes_the_result(tmp_path):
    """@brief Negative: a report path inside a missing folder loses the file, not the status."""
    main = _load_main()
    report = tmp_path / "missing-folder" / "check.txt"
    assert main.check_runtime(resource_path=lambda *parts: tmp_path.joinpath(*parts),
                              modules=("json",), resources=(), qt_plugins=(),
                              report_file=report) == 0
    assert not report.exists()


def test_the_report_path_comes_from_the_option_or_the_environment():
    """@brief Option first (both spellings), then the variable, else None."""
    main = _load_main()
    env = {main.CHECK_RUNTIME_REPORT_ENV: "from-env.txt"}
    assert main.check_runtime_report_path(["--check-runtime", "--check-runtime-report", "a.txt"],
                                          env) == "a.txt"
    assert main.check_runtime_report_path(["--check-runtime-report=b.txt"], env) == "b.txt"
    assert main.check_runtime_report_path(["--check-runtime"], env) == "from-env.txt"
    assert main.check_runtime_report_path(["--check-runtime"], {}) is None


def test_the_build_reads_the_same_variable_and_prints_the_report_on_failure():
    """@brief Structural: build-windows.ps1 sets the variable main.py reads and shows the file."""
    main = _load_main()
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert f"$env:{main.CHECK_RUNTIME_REPORT_ENV} = $checkReport" in build
    failure = build.split("if ($check.ExitCode -ne 0) {", 1)[1].split("\n}", 1)[0]
    assert "Get-Content $checkReport" in failure and "throw" in failure


def test_the_command_line_writes_the_report_file(tmp_path):
    """@brief End to end from the sources: main.py --check-runtime fills the file."""
    import subprocess
    report = tmp_path / "check.txt"
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--check-runtime",
         "--check-runtime-report", str(report)],
        capture_output=True, text=True, timeout=300, cwd=ROOT,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Runtime check OK." in report.read_text(encoding="utf-8")


# -- K5 -------------------------------------------------------------------------

def test_an_optional_module_missing_from_the_sources_does_not_fail(tmp_path, monkeypatch):
    """@brief Positive: from the sources a missing optional module is only reported."""
    main = _load_main()
    monkeypatch.delattr(sys, "frozen", raising=False)
    report = tmp_path / "check.txt"
    assert main.check_runtime(resource_path=lambda *parts: tmp_path.joinpath(*parts),
                              modules=(), optional_modules=("kicadgen_optional_absent",),
                              resources=(), qt_plugins=(), report_file=report) == 0
    assert "kicadgen_optional_absent: not installed (optional" in report.read_text(encoding="utf-8")


def test_an_optional_module_missing_from_the_executable_fails(tmp_path, monkeypatch):
    """@brief Negative: the package always ships it, so the executable requires it."""
    main = _load_main()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert main.check_runtime(resource_path=lambda *parts: tmp_path.joinpath(*parts),
                              modules=(), optional_modules=("kicadgen_optional_absent",),
                              resources=(), qt_plugins=()) == 1


def test_onnxruntime_is_optional_by_default_and_not_required_from_sources():
    """@brief The default lists keep onnxruntime out of the required modules."""
    main = _load_main()
    assert "onnxruntime" not in main.RUNTIME_MODULES
    assert "onnxruntime" in main.OPTIONAL_RUNTIME_MODULES
